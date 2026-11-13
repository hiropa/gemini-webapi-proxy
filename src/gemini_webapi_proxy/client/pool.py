"""Process-wide pool of Gemini clients, one per account.

Each account (see :mod:`.accounts`) gets its own lazily initialised
:class:`gemini_webapi.GeminiClient`.  A request takes the first usable account in
priority order that has the model; when an account runs out of quota or gets logged
out mid-request, :func:`run` repeats the call on the next one.

Quota cooldowns are kept per model (the credits are per model in Gemini Web) and end
at the reset time the library read from the account, or after
``GOP_QUOTA_COOLDOWN`` seconds when it did not report one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar

from gemini_webapi import GeminiClient
from gemini_webapi.constants import AccountStatus
from gemini_webapi.exceptions import AuthError, TemporarilyBlockedError, UsageLimitExceededError

from gemini_webapi_proxy.client import curl_compat
from gemini_webapi_proxy.client.accounts import ACCOUNTS_FILE, Account, AccountStore
from gemini_webapi_proxy.client.quota import quota_report
from gemini_webapi_proxy.client.registry import ModelRegistry
from gemini_webapi_proxy.config import Settings, get_settings
from gemini_webapi_proxy.cookies import CookieBundle, build_source
from gemini_webapi_proxy.cookies.session_bootstrap import bootstrap_browser_cookie_session
from gemini_webapi_proxy.downloaders.image_export import sanitize_gemini_client_cookies
from gemini_webapi_proxy.egress import egress
from gemini_webapi_proxy.errors import NoAccountError, QuotaExceededError
from gemini_webapi_proxy.events import emit
from gemini_webapi_proxy.stats import journal, note_account

logger = logging.getLogger(__name__)
curl_compat.install()

T = TypeVar("T")

_UNAUTH_RETRY_SECONDS = 60.0
# Google answered 429 / its "unusual traffic" page: retrying soon only prolongs it.
_BLOCKED_RETRY_SECONDS = 15 * 60.0
_MODELS_REFRESH_INTERVAL = 60.0
registry = ModelRegistry()
# The account being signed in through the sign-in browser (admin UI), if any.  While
# set, the browser holds that account's session, so no other account may read it.
# An abandoned sign-in (the UI tab was closed) lapses after SIGN_IN_SECONDS.
signing_in: str | None = None
signing_in_since = 0.0
signing_in_context: str | None = None  # the browser context of that sign-in
SIGN_IN_SECONDS = 15 * 60
# Set by the admin playground: serve the request on this account only.
pinned: ContextVar[str | None] = ContextVar("gop_pinned_account", default=None)


def current_sign_in() -> str | None:
    global signing_in
    if signing_in and time.monotonic() - signing_in_since > SIGN_IN_SECONDS:
        signing_in = None
    return signing_in


async def _load_cookie_bundle(settings: Settings):
    """Resolve the configured cookie source to a :class:`CookieBundle`."""
    if settings.cookie_source == "browser":
        bootstrap_browser_cookie_session()
        bundle = await build_source("browser", browser=settings.browser).load()
        bootstrap_browser_cookie_session(psid=bundle.psid)
        return bundle
    # env / file: build_source() raises if the source is mis-configured
    return await build_source(settings.cookie_source).load()


def _account_bundle(account: Account) -> CookieBundle:
    jar = account.cookies
    return CookieBundle(jar.get("__Secure-1PSID", ""), jar.get("__Secure-1PSIDTS", ""), dict(jar))


def _serves(model_id: str, runtime: Any) -> bool:
    """Whether ``runtime`` is the model asked for: a fuzzy match (gemini-flash ->
    gemini-flash-lite) is another model."""
    return not isinstance(runtime, str) and model_id in registry.entry_ids()


def _client_authenticated(client: GeminiClient) -> bool:
    return getattr(client, "account_status", None) != AccountStatus.UNAUTHENTICATED


async def _new_client(settings: Settings, account: Account) -> GeminiClient:
    if account.source == "settings":
        bundle = await _load_cookie_bundle(settings)
    else:
        bundle = _account_bundle(account)
    if not bundle.is_valid():
        raise RuntimeError("No session cookies (no __Secure-1PSID): sign in to this account")

    client = GeminiClient(bundle.psid, bundle.psidts, proxy=account.proxy or settings.http_proxy)
    if bundle.extras:
        client.cookies = dict(bundle.extras)
    # The proxy owns the session once it has the cookies: the sign-in browser forgets
    # them (see cookies/cdp.py), so there is one rotation chain.
    await client.init(timeout=settings.init_timeout, auto_refresh=True)
    sanitize_gemini_client_cookies(client)
    # The library drops a stream after 120 s of silence, even before the first byte
    # (upstream #367); Google often takes longer than that to start answering (Pro,
    # Argon, a busy queue).  The request's own timeout bounds the call instead.
    client.watchdog_timeout = max(client.watchdog_timeout, settings.chat_timeout)
    return client


def _quota_reset(client: Any, fallback: float) -> float:
    """When the exhausted credits come back, as the library last read them."""
    resets = [
        q["reset_time"]
        for q in (getattr(client, "quotas", None) or {}).values()
        if isinstance(q, dict) and q.get("remaining") == 0 and q.get("reset_time")
    ]
    future = [r for r in resets if isinstance(r, (int, float)) and r > time.time()]
    return max(future) if future else time.time() + fallback


class Slot:
    """Runtime state of one account."""

    def __init__(self, account: Account) -> None:
        self.account = account
        self.client: GeminiClient | None = None
        self.created_at = 0.0  # monotonic, last init attempt that produced a client
        self.failed_at = 0.0  # monotonic, last init attempt that raised
        self.error: str | None = None
        self.needs_login = False
        self.cooldowns: dict[str, float] = {}  # model id -> wall-clock end
        self.models_refreshed_at = 0.0
        self.seen_models: set[str] = set()  # every model the account has listed
        self.requests = 0
        self.last_used: float | None = None
        self.lock = asyncio.Lock()
        self._gate: asyncio.Semaphore | None = None
        self.next_start = 0.0  # monotonic
        self.active = 0
        self.down = False  # an outage was reported: the next good init reports recovery

    def gate(self, settings: Settings) -> asyncio.Semaphore:
        if self._gate is None:
            self._gate = asyncio.Semaphore(max(1, settings.account_concurrency))
        return self._gate

    async def paced(self, settings: Settings, call: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Run ``call`` within the account's concurrency and start-interval limits,
        through the account's proxy."""
        async with self.gate(settings):
            wait = self.next_start - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.next_start = time.monotonic() + settings.account_min_interval
            token = egress.set(self.account.proxy or settings.http_proxy)
            self.active += 1
            try:
                return await call(*args)
            finally:
                self.active -= 1
                egress.reset(token)

    def abuse_flag(self) -> dict | None:
        """The abuse status when Google flagged the account, else None."""
        status = getattr(self.client, "abuse_status", None) if self.client else None
        if isinstance(status, dict) and status.get("is_clean") is False:
            return status
        return None

    @property
    def id(self) -> str:
        return self.account.id

    def cooling(self, key: str) -> bool:
        until = self.cooldowns.get(key)
        if until is None:
            return False
        if until <= time.time():
            del self.cooldowns[key]
            return False
        return True

    def models(self) -> list:
        return (self.client.list_models() or []) if self.client else []

    def _sync_models(self) -> None:
        models = self.models()
        registry.sync_from_client(models)
        self.seen_models.update(m.model_name for m in models if m.is_available and m.model_name)

    def state(self) -> str:
        if not self.account.enabled:
            return "disabled"
        if self.needs_login:
            return "needs_login"
        if self.client is not None and _client_authenticated(self.client):
            return "ok"
        if self.client is not None:
            return "logged_out"
        if self.error:
            return "error"
        return "idle"

    def _retry_due(self) -> bool:
        """A logged-out (guest) or failed client is retried, at most once a minute.

        Only for accounts that read GOP_COOKIE_SOURCE: the source may have a new
        session by now.  A stored jar does not change until someone signs in again.
        """
        now = time.monotonic()
        if self.client is not None:
            return (
                not _client_authenticated(self.client)
                and self.account.source == "settings"
                and now - self.created_at >= _UNAUTH_RETRY_SECONDS
            )
        return now - self.failed_at >= _UNAUTH_RETRY_SECONDS

    async def ensure(self, settings: Settings) -> GeminiClient | None:
        if not self.account.enabled or self.needs_login:
            return self.client
        if (
            current_sign_in()
            and self.account.source == "settings"
            and settings.cookie_source == "cdp"
        ):
            return self.client
        if self.client is not None and not self._retry_due():
            return self.client
        async with self.lock:
            if self.client is not None and not self._retry_due():
                return self.client
            if self.client is None and not self._retry_due():
                return None
            old = self.client
            try:
                client = await _new_client(settings, self.account)
            except Exception as exc:
                logger.warning("account %s: Gemini client init failed: %s", self.id, exc)
                self.error, self.failed_at = str(exc)[:500], time.monotonic()
                if not self.down and self.account.source == "stored":
                    self.down = True
                if isinstance(exc, TemporarilyBlockedError):
                    emit("blocked", self.id, str(exc))
                    self.down = True
                    # failed_at in the future: _retry_due() waits the longer pause
                    self.failed_at += _BLOCKED_RETRY_SECONDS - _UNAUTH_RETRY_SECONDS
                if self.account.source == "stored" and "__Secure-1PSID" in str(exc):
                    self.needs_login = True
                if old is not None:
                    self.created_at = time.monotonic()
                return old
            self.client, self.created_at, self.error = client, time.monotonic(), None
            self.models_refreshed_at = time.monotonic()
            if old is not None:
                try:
                    await old.close()
                except Exception:
                    pass
            if not _client_authenticated(client):
                self.logged_out("the session is logged out (guest)")
            elif self.down:
                self.down = False
                emit("recovered", self.id, getattr(self.account, "email", None) or "")
            registry.load_file()
            self._sync_models()
            return client

    def logged_out(self, why: str) -> None:
        logger.warning("account %s: %s", self.id, why)
        self.error = why
        self.down = True
        emit("logged_out", self.id, why)
        if self.account.source == "stored":
            self.needs_login = True

    async def runtime(self, model_id: str):
        """The account's model for ``model_id`` (a string when it does not have it).

        gemini-webapi builds its model list only in ``init()``.  Models the account
        could not use at that moment (exhausted quota) are missing from it, so when a
        model the account had is missing, the list is re-read, at most once a minute.
        A model it never had (Pro on a free account) does not cost a call.
        """
        runtime = registry.resolve_runtime(model_id, self.models())
        if _serves(model_id, runtime) or self.client is None:
            return runtime
        if self.seen_models and registry.resolve_id(model_id) not in self.seen_models:
            return runtime
        refresh = getattr(self.client, "_fetch_user_status", None)
        now = time.monotonic()
        if refresh is None or now - self.models_refreshed_at < _MODELS_REFRESH_INTERVAL:
            return runtime
        self.models_refreshed_at = now
        try:
            await refresh()
        except Exception:  # best effort: keep the old list
            return runtime
        self._sync_models()
        return registry.resolve_runtime(model_id, self.models())

    async def close(self) -> None:
        if self.client is not None:
            try:
                await self.client.close()
            finally:
                self.client = None

    def report(self) -> dict[str, Any]:
        now = time.time()
        return {
            **self.account.public(),
            "state": self.state(),
            "error": self.error,
            "requests": self.requests,
            "active": self.active,
            "last_used": self.last_used,
            "cooldowns": {k: v for k, v in self.cooldowns.items() if v > now},
            "models": sorted(
                m.model_name for m in self.models() if m.is_available and m.model_name
            ),
            "quota": quota_report(self.client) if self.client is not None else None,
        }


@dataclass
class Lease:
    slot: Slot
    client: GeminiClient
    runtime: Any


class AccountPool:
    def __init__(self) -> None:
        self.store: AccountStore | None = None
        self.slots: dict[str, Slot] = {}

    def configure(self, settings: Settings) -> AccountStore:
        path = settings.data_dir / ACCOUNTS_FILE
        if self.store is None or self.store.path != path:
            self.store = AccountStore(path)
            self.slots = {}
        self.sync()
        return self.store

    def sync(self) -> None:
        """Match the slots to the store after an account was added, changed or removed."""
        assert self.store is not None
        accounts = {a.id: a for a in self.store.all()}
        for gone in set(self.slots) - set(accounts):
            slot = self.slots.pop(gone)
            if slot.client is not None:
                asyncio.get_running_loop().create_task(slot.close())
        for account in accounts.values():
            slot = self.slots.get(account.id)
            if slot is None:
                self.slots[account.id] = Slot(account)
            else:
                slot.account = account

    def ordered(self) -> list[Slot]:
        return sorted(self.slots.values(), key=lambda s: (s.account.priority, s.id))

    def slot_of(self, client: Any) -> Slot | None:
        return next((s for s in self.slots.values() if s.client is client), None)

    async def acquire(
        self,
        settings: Settings,
        model: str | None,
        *,
        exclude: set[str] = frozenset(),  # type: ignore[assignment]
        cooldown_key: str | None = None,
    ) -> Lease | None:
        """The first usable account that has ``model``.

        An account without the model only serves when no account has it: the library
        then gets the plain name and decides (this is how a single account behaves).
        """
        self.configure(settings)
        key = cooldown_key or model or ""
        only = pinned.get()
        fallback: Lease | None = None
        for slot in self.ordered():
            if only is not None and slot.id != only:
                continue
            if not slot.account.enabled or slot.id in exclude or slot.cooling(key):
                continue
            client = await slot.ensure(settings)
            if self.flag_if_abused(slot) or client is None or not _client_authenticated(client):
                continue
            if model is None:
                return Lease(slot, client, None)
            runtime = await slot.runtime(model)
            if _serves(model, runtime):
                return Lease(slot, client, runtime)
            fallback = fallback or Lease(slot, client, runtime)
        return fallback

    async def run(
        self,
        settings: Settings,
        model: str | None,
        call: Callable[[GeminiClient, Any], Awaitable[T]],
        *,
        cooldown_key: str | None = None,
        cooldown_seconds: float | None = None,
    ) -> T:
        """Run ``call(client, runtime)`` on an account, moving on when it is used up.

        Only quota exhaustion and a lost session move on: any other error is the
        answer (a retry elsewhere could repeat a generation that went through).
        """
        key = cooldown_key or model or ""
        tried: set[str] = set()
        last: Exception | None = None
        while True:
            lease = await self.acquire(settings, model, exclude=tried, cooldown_key=key)
            if lease is None:
                emit("all_down", None, self.summary() + (f" — {model}" if model else ""))
                if last is not None:
                    raise last
                raise NoAccountError(self.summary())
            slot = lease.slot
            tried.add(slot.id)
            note_account(slot.id)
            slot.requests += 1
            slot.last_used = time.time()
            try:
                return await slot.paced(settings, call, lease.client, lease.runtime)
            except (QuotaExceededError, UsageLimitExceededError) as exc:
                until = (
                    time.time() + cooldown_seconds
                    if cooldown_seconds
                    else _quota_reset(lease.client, settings.quota_cooldown)
                )
                slot.cooldowns[key] = until
                emit(
                    "quota",
                    slot.id,
                    f"{key} until {time.strftime('%d.%m %H:%M UTC', time.gmtime(until))}",
                )
                logger.warning(
                    "account %s: limit for %s until %s, trying the next account",
                    slot.id,
                    key,
                    time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(until)),
                )
                last = exc if isinstance(exc, QuotaExceededError) else QuotaExceededError(str(exc))
            except AuthError as exc:
                slot.logged_out(f"session rejected: {exc}")
                await slot.close()
                last = exc
            except Exception as exc:
                if _client_authenticated(lease.client):
                    raise
                slot.logged_out(f"session lost during a request: {exc}")
                last = exc

    def flag_if_abused(self, slot: Slot) -> bool:
        """Switch off an account Google flagged for abuse: more traffic only makes it worse."""
        status = slot.abuse_flag()
        if status is None or not slot.account.enabled:
            return status is not None
        logger.warning("account %s: flagged by Google (%s), switching it off", slot.id, status)
        emit("abuse", slot.id, str(status))
        slot.down = True
        slot.error = (
            f"Google flagged the account (abuse status {status}); switched off automatically"
        )
        if self.store is not None:
            self.store.update(slot.id, enabled=False)
        return True

    def summary(self) -> str:
        states = ", ".join(f"{s.id}={s.state()}" for s in self.ordered()) or "none configured"
        return f"No Gemini account can serve this request (accounts: {states})"

    async def drop(self, account_id: str) -> None:
        """Forget the account's client so the next request signs in afresh."""
        slot = self.slots.get(account_id)
        if slot is None:
            return
        async with slot.lock:
            await slot.close()
            slot.needs_login = False
            slot.error = None
            slot.failed_at = 0.0

    async def close(self) -> None:
        for slot in self.slots.values():
            await slot.close()


pool = AccountPool()


# ---- Single-client API (probe, admin, image download retries) --------------
async def get_client(settings: Settings | None = None) -> GeminiClient:
    """The client of the first usable account."""
    settings = settings or get_settings()
    lease = await pool.acquire(settings, None)
    if lease is not None:
        return lease.client
    # Nothing signed in: hand out a guest client if there is one (lists flash-lite only).
    for slot in pool.ordered():
        if slot.client is not None:
            return slot.client
    raise NoAccountError(pool.summary())


async def invalidate_client(client: Any = None) -> None:
    """Drop a client whose session went bad so the next request re-reads cookies.

    A client that came up unauthenticated is kept for a minute: re-initialising on
    every failing request only hammers Google with logged-out sessions.
    """
    for slot in list(pool.slots.values()):
        if slot.client is None or (client is not None and slot.client is not client):
            continue
        if client is None and _client_authenticated(slot.client):
            continue
        if (
            not _client_authenticated(slot.client)
            and time.monotonic() - slot.created_at < _UNAUTH_RETRY_SECONDS
        ):
            continue
        async with slot.lock:
            await slot.close()


async def close_client() -> None:
    await pool.close()


@asynccontextmanager
async def lifespan(app):  # type: ignore[no-untyped-def]
    settings = get_settings()
    registry.load_file()
    journal.open(settings.data_dir)
    pool.configure(settings)
    # Bring the accounts up during startup so the model registry is populated before
    # the first /openai/v1/models request.
    for slot in pool.ordered():
        try:
            await slot.ensure(settings)
        except Exception:
            # Cookie not yet configured, or Gemini is down.  The first real request
            # will retry (ensure() already logged why).
            pass
    from gemini_webapi_proxy.telegram_bot import bot

    try:
        await bot.start(settings)
    except Exception as exc:  # the proxy works without its bot
        logger.warning("telegram bot: %s", exc)
    yield
    await bot.stop()
    await close_client()
