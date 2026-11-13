"""Telegram bot for the operator: account status, controls and alerts.

Set up from the admin UI (Telegram tab): the token, then "link an admin" gives a
one-time ``t.me/<bot>?start=<code>`` link; whoever opens it becomes an admin of the
bot.  Settings live in ``<data_dir>/telegram.json`` (mode 0600).

The bot long-polls ``getUpdates`` from inside the proxy process: one process per
token (a second one would steal the updates).  Admins get the pool's events
(:mod:`gemini_webapi_proxy.events`) as messages; anyone else is told it has no access.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from gemini_webapi_proxy import events
from gemini_webapi_proxy.config import Settings

log = logging.getLogger(__name__)

CONFIG_FILE = "telegram.json"
LINK_TTL = 10 * 60.0
API = "https://api.telegram.org"


@dataclass
class TelegramConfig:
    token: str = ""
    enabled: bool = True
    admins: dict[str, str] = field(default_factory=dict)  # chat id -> name
    notify: list[str] = field(default_factory=lambda: list(events.KINDS))
    ui_url: str = ""  # public address of the admin UI, for links in messages
    proxy: str = ""  # for api.telegram.org, if it is not reachable directly

    def public(self) -> dict:
        out = asdict(self)
        out["token"] = f"{self.token[:6]}…{self.token[-4:]}" if self.token else ""
        out["token_set"] = bool(self.token)
        return out


def load_config(data_dir: Path) -> TelegramConfig:
    path = data_dir / CONFIG_FILE
    if not path.is_file():
        return TelegramConfig()
    raw = json.loads(path.read_text())
    known = {k: v for k, v in raw.items() if k in TelegramConfig.__dataclass_fields__}
    return TelegramConfig(**known)


def save_config(data_dir: Path, config: TelegramConfig) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / CONFIG_FILE
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(config), f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _e(text: Any) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


STATE = {
    "ok": "🟢 working",
    "idle": "⚪ not started",
    "disabled": "⚫ disabled",
    "needs_login": "🔴 login required",
    "logged_out": "🔴 logged out",
    "error": "🟠 error",
}


def _rel(ts: Any) -> str:
    if not ts:
        return "—"
    if isinstance(ts, str):
        from datetime import datetime

        try:
            ts = datetime.fromisoformat(ts).timestamp()
        except ValueError:
            return ts
    delta = ts - time.time()
    a = abs(delta)
    unit = (
        f"{round(a)}s"
        if a < 90
        else f"{round(a / 60)}min"
        if a < 5400
        else f"{round(a / 3600)}h"
        if a < 129600
        else f"{round(a / 86400)}d"
    )
    return f"in {unit}" if delta >= 0 else f"{unit} ago"


class TelegramBot:
    def __init__(self) -> None:
        self.config = TelegramConfig()
        self.data_dir: Path | None = None
        self.me: dict | None = None
        self.error: str | None = None
        self.started_at: float | None = None
        self._tasks: list[asyncio.Task] = []
        self._queue: asyncio.Queue[events.Event] = asyncio.Queue(maxsize=200)
        self._links: dict[str, float] = {}  # one-time admin link code -> expiry
        self._client: httpx.AsyncClient | None = None

    # ---- lifecycle ---------------------------------------------------------
    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    async def start(self, settings: Settings) -> None:
        await self.stop()
        self.data_dir = settings.data_dir
        self.config = load_config(settings.data_dir)
        self.error, self.me = None, None
        if not self.config.token or not self.config.enabled:
            return
        self._client = httpx.AsyncClient(
            base_url=f"{API}/bot{self.config.token}/",
            timeout=httpx.Timeout(45.0, connect=15.0),
            proxy=self.config.proxy or None,
            trust_env=False,
        )
        try:
            self.me = await self.call("getMe")
        except Exception as exc:
            self.error = f"getMe: {self._safe(exc)}"
            log.warning("telegram: %s (retrying in a minute)", self.error)
            await self._client.aclose()
            self._client = None
            self._tasks = [asyncio.get_running_loop().create_task(self._retry_start(settings))]
            return
        await self._commands()
        self.started_at = time.time()
        events.subscribe(self._on_event)
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._poll()), loop.create_task(self._sender())]
        log.info("telegram: bot @%s started", self.me.get("username"))

    async def _retry_start(self, settings: Settings) -> None:
        await asyncio.sleep(60)
        self._tasks = []  # start() must not cancel this very task
        await self.start(settings)

    async def stop(self) -> None:
        events.unsubscribe(self._on_event)
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def status(self) -> dict:
        return {
            **self.config.public(),
            "running": self.running,
            "username": (self.me or {}).get("username"),
            "error": self.error,
            "started_at": self.started_at,
        }

    def save(self) -> None:
        if self.data_dir is not None:
            save_config(self.data_dir, self.config)

    def new_link(self) -> dict:
        code = secrets.token_urlsafe(9)
        now = time.time()
        self._links = {c: t for c, t in self._links.items() if t > now}
        self._links[code] = now + LINK_TTL
        username = (self.me or {}).get("username")
        return {
            "code": code,
            "expires_at": now + LINK_TTL,
            "url": f"https://t.me/{username}?start={code}" if username else None,
        }

    # ---- Telegram API ------------------------------------------------------
    def _safe(self, exc: Exception) -> str:
        text = str(exc)
        return text.replace(self.config.token, "***") if self.config.token else text

    async def call(self, method: str, **params: Any) -> Any:
        assert self._client is not None
        resp = await self._client.post(method, json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method}: {data.get('description') or resp.status_code}")
        return data.get("result")

    async def send(self, chat_id: str | int, text: str, buttons: list | None = None) -> None:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        await self.call("sendMessage", **params)

    async def _commands(self) -> None:
        try:
            await self.call(
                "setMyCommands",
                commands=[
                    {"command": "status", "description": "Accounts and limits"},
                    {"command": "accounts", "description": "Manage accounts"},
                    {"command": "stats", "description": "Requests in the last 24 hours"},
                    {"command": "events", "description": "Recent events"},
                    {"command": "help", "description": "What the bot can do"},
                ],
            )
        except Exception as exc:
            log.debug("telegram: setMyCommands: %s", self._safe(exc))

    async def _poll(self) -> None:
        offset = None
        backoff = 1.0
        while True:
            try:
                params: dict[str, Any] = {
                    "timeout": 30,
                    "allowed_updates": ["message", "callback_query"],
                }
                if offset is not None:
                    params["offset"] = offset
                started = time.monotonic()
                updates = await self.call("getUpdates", **params)
                backoff = 1.0
                self.error = None
                if not updates and time.monotonic() - started < 1.0:
                    # Long polling should have waited; something in between answers at
                    # once. Do not spin (it would also starve the event loop).
                    await asyncio.sleep(1.0)
                for update in updates:
                    offset = update["update_id"] + 1
                    try:
                        await self._handle(update)
                    except Exception as exc:
                        log.warning("telegram: update failed: %s", self._safe(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = self._safe(exc)[:300]
                log.warning("telegram: polling: %s", self.error)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ---- alerts ------------------------------------------------------------
    def _on_event(self, event: events.Event) -> None:
        if event.kind in self.config.notify and self.config.admins:
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def _sender(self) -> None:
        while True:
            event = await self._queue.get()
            text = self.alert_text(event)
            for chat_id in list(self.config.admins):
                try:
                    await self.send(chat_id, text, self._ui_button())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("telegram: alert to %s: %s", chat_id, self._safe(exc))

    def alert_text(self, event: events.Event) -> str:
        icon = "✅" if event.kind == "recovered" else "⚠️"
        who = f" · <b>{_e(event.account)}</b>" if event.account else ""
        body = f"\n{_e(event.text)}" if event.text else ""
        return f"{icon} {_e(event.title)}{who}{body}"

    def _ui_button(self) -> list | None:
        url = self.config.ui_url.strip()
        if not url.startswith("https://"):  # Telegram refuses plain-http / local links
            return None
        return [[{"text": "Open admin UI", "url": url}]]

    # ---- updates -----------------------------------------------------------
    def _is_admin(self, chat_id: Any) -> bool:
        return str(chat_id) in self.config.admins

    async def _handle(self, update: dict) -> None:
        if "callback_query" in update:
            await self._callback(update["callback_query"])
            return
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return  # commands only in a private chat with the bot
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        user = message.get("from") or {}
        if text.startswith("/start"):
            code = text.split(maxsplit=1)[1].strip() if " " in text else ""
            if code and self._links.get(code, 0) > time.time():
                self._links.pop(code, None)
                name = user.get("username") or user.get("first_name") or str(chat_id)
                self.config.admins[str(chat_id)] = name
                self.save()
                log.info("telegram: %s (%s) linked as admin", name, chat_id)
                await self.send(
                    chat_id, "Done: you are an admin of this proxy. /help — what the bot can do."
                )
                return
        if not self._is_admin(chat_id):
            await self.send(chat_id, "No access. An admin links the bot in the proxy's admin UI.")
            return
        command = text.split()[0].split("@")[0].lower() if text else ""
        handler = {
            "/start": self._help,
            "/help": self._help,
            "/status": self._status,
            "/accounts": self._accounts,
            "/stats": self._stats,
            "/events": self._events,
        }.get(command)
        if handler is None:
            await self.send(chat_id, "Unknown command. /help")
            return
        await handler(chat_id)

    async def _help(self, chat_id: Any) -> None:
        await self.send(
            chat_id,
            "<b>Gemini Proxy</b>\n"
            "/status — accounts, limits, cooldowns\n"
            "/accounts — enable/disable, reset limit, check\n"
            "/stats — requests in the last 24 hours\n"
            "/events — recent events\n\n"
            "Alerts arrive on their own: logout, abuse flag, limits, 429, all accounts down.",
            self._ui_button(),
        )

    def _pool(self):
        from gemini_webapi_proxy.client.pool import pool
        from gemini_webapi_proxy.config import get_settings

        pool.configure(get_settings())
        return pool

    def status_text(self) -> str:
        lines = ["<b>Accounts</b>"]
        for slot in self._pool().ordered():
            r = slot.report()
            q = r.get("quota") or {}
            usage = q.get("usage") or {}
            head = f"\n<b>{_e(r['name'])}</b> {STATE.get(r['state'], _e(r['state']))}"
            if q.get("tier"):
                head += f" · {_e(q['tier'])}"
            lines.append(head)
            if r.get("email"):
                lines.append(_e(r["email"]))
            for key, name in (("current_5h", "5 h"), ("weekly", "week")):
                u = usage.get(key)
                if u and u.get("usage_percentage") is not None:
                    lines.append(
                        f"{name}: {u['usage_percentage']}% · reset {_rel(u.get('reset_at'))}"
                    )
            for model, until in (r.get("cooldowns") or {}).items():
                lines.append(f"⏸ {_e(model)} until {_rel(until)}")
            if r.get("error") and r["state"] != "ok":
                lines.append(f"<i>{_e(r['error'][:200])}</i>")
        return "\n".join(lines)

    async def _status(self, chat_id: Any) -> None:
        await self.send(chat_id, self.status_text(), self._ui_button())

    def _account_buttons(self) -> list:
        rows = []
        for slot in self._pool().ordered():
            a = slot.account
            rows.append(
                [
                    {
                        "text": f"{a.name}: {'disable' if a.enabled else 'enable'}",
                        "callback_data": f"t:{a.id}",
                    },
                    {"text": "reset limit", "callback_data": f"c:{a.id}"},
                    {"text": "check", "callback_data": f"k:{a.id}"},
                ]
            )
        return rows

    async def _accounts(self, chat_id: Any) -> None:
        await self.send(chat_id, self.status_text(), self._account_buttons())

    async def _stats(self, chat_id: Any) -> None:
        from gemini_webapi_proxy.stats import journal, summary

        s = summary(journal.since(24 * 3600), hours=24)
        t = s["totals"]
        p50 = f"{t['p50_ms'] / 1000:.0f}s" if t.get("p50_ms") else "—"
        lines = [
            "<b>Last 24 hours</b>",
            f"requests: {t['requests']}, errors: {t['errors']} (429 limit: {t['limited']})",
            f"response time, median: {p50}",
        ]
        for title, key in (("Models", "models"), ("Accounts", "accounts"), ("Clients", "keys")):
            top = ", ".join(f"{_e(r['name'])} {r['requests']}" for r in s[key][:5])
            if top:
                lines.append(f"{title}: {top}")
        await self.send(chat_id, "\n".join(lines))

    async def _events(self, chat_id: Any) -> None:
        last = list(events.recent)[-10:]
        if not last:
            await self.send(chat_id, "No events yet.")
            return
        lines = ["<b>Recent events</b>"]
        for e in reversed(last):
            who = f" · {_e(e.account)}" if e.account else ""
            lines.append(f"{time.strftime('%d.%m %H:%M', time.localtime(e.ts))} {_e(e.title)}{who}")
        await self.send(chat_id, "\n".join(lines))

    async def _callback(self, query: dict) -> None:
        chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
        if not self._is_admin(chat_id):
            await self.call("answerCallbackQuery", callback_query_id=query["id"], text="No access")
            return
        action, _, account_id = (query.get("data") or "").partition(":")
        pool = self._pool()
        slot = pool.slots.get(account_id)
        note = "No such account"
        if slot is not None:
            from gemini_webapi_proxy.config import get_settings

            if action == "t":
                assert pool.store is not None
                pool.store.update(account_id, enabled=not slot.account.enabled)
                pool.sync()
                if not slot.account.enabled:
                    await slot.close()
                note = "Enabled" if slot.account.enabled else "Disabled"
            elif action == "c":
                slot.cooldowns.clear()
                note = "Limits reset"
            elif action == "k":
                await self.call(
                    "answerCallbackQuery", callback_query_id=query["id"], text="Checking…"
                )
                await pool.drop(account_id)
                await slot.ensure(get_settings())
                note = None
        if note:
            await self.call("answerCallbackQuery", callback_query_id=query["id"], text=note)
        message = query.get("message") or {}
        try:
            await self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message.get("message_id"),
                text=self.status_text(),
                parse_mode="HTML",
                reply_markup={"inline_keyboard": self._account_buttons()},
            )
        except Exception as exc:  # "message is not modified" and the like
            log.debug("telegram: edit: %s", self._safe(exc))


bot = TelegramBot()
