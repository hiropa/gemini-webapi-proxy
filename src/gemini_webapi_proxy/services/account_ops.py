"""Account operations shared by the admin API and the node API (see docs/nodes.md).

In **master** mode the admin UI calls these directly.  In **slave** mode the local
admin API refuses account changes (:func:`reject_slave_mode`); the same operations
arrive over the link instead, driven by the master through ``/node/accounts``.
The sign-in flow stays available locally on a slave: someone must operate the
sign-in browser on that machine, but the account itself was created by the master.
"""

from __future__ import annotations

import logging
import time

from fastapi import HTTPException
from pydantic import BaseModel

from gemini_webapi_proxy.client import pool as pool_module
from gemini_webapi_proxy.client.pool import pool
from gemini_webapi_proxy.config import get_settings
from gemini_webapi_proxy.cookies.cdp import cancel_sign_in, open_sign_in, take_session
from gemini_webapi_proxy.utils.cookies import parse_cookie_string

log = logging.getLogger(__name__)


class CookiesIn(BaseModel):
    cookies: str  # "name=value; name2=value2" as copied from a browser


def reject_slave_mode() -> None:
    """FastAPI dependency: account management is reserved to the master."""
    if get_settings().is_slave:
        raise HTTPException(
            status_code=403,
            detail="This proxy runs in slave mode: accounts are managed by its master "
            "(docs/nodes.md). Sign-in to an existing account still happens here.",
        )


def _store():
    return pool.configure(get_settings())


def _slot(account_id: str):
    _store()
    slot = pool.slots.get(account_id)
    if slot is None:
        raise HTTPException(status_code=404, detail=f"No account {account_id!r}")
    return slot


def _reject_remote(slot) -> None:
    """Accounts that live on a linked node are managed through that node."""
    if slot.is_remote:
        raise HTTPException(
            status_code=409,
            detail=f"Remote account on node {slot.node.name!r}: manage it there ({slot.node.url}/admin/ui)",
        )


def add_account(name: str) -> dict:
    account = _store().add(name)
    pool.sync()
    return pool.slots[account.id].report()


async def update_account(account_id: str, changes: dict) -> dict:
    slot = _slot(account_id)
    _reject_remote(slot)
    if changes:
        _store().update(account_id, **changes)
        pool.sync()
    if changes.get("enabled") is False:
        await slot.close()
    return slot.report()


async def delete_account(account_id: str) -> dict:
    slot = _slot(account_id)
    _reject_remote(slot)
    await slot.close()
    _store().remove(account_id)
    pool.sync()
    return {"ok": True}


async def check_account(account_id: str) -> dict:
    slot = _slot(account_id)
    _reject_remote(slot)
    await pool.drop(account_id)
    await slot.ensure(get_settings())
    return slot.report()


def clear_cooldowns(account_id: str) -> dict:
    slot = _slot(account_id)
    _reject_remote(slot)
    slot.cooldowns.clear()
    return slot.report()


async def set_cookies(account_id: str, cookies: str, email: str | None = None) -> dict:
    jar = cookies if isinstance(cookies, dict) else parse_cookie_string(cookies)
    slot = _slot(account_id)
    _reject_remote(slot)
    if "__Secure-1PSID" not in jar:
        raise HTTPException(status_code=400, detail="No __Secure-1PSID: not signed in to Google")
    _store().set_cookies(account_id, jar, email)
    pool.sync()
    await pool.drop(account_id)
    await slot.ensure(get_settings())
    return slot.report()


# ---- Sign-in through this machine's browser ---------------------------------
async def login_start(account_id: str) -> dict:
    slot = _slot(account_id)
    _reject_remote(slot)
    busy = pool_module.current_sign_in()
    if busy not in (None, account_id):
        raise HTTPException(status_code=409, detail=f"Sign-in to {busy!r} is in progress")
    settings = get_settings()
    try:
        # The sign-in leaves through the account's proxy, like its requests will.
        context = await open_sign_in(settings.cdp_url, slot.account.proxy or settings.http_proxy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Sign-in browser: {exc}") from exc
    pool_module.signing_in, pool_module.signing_in_since = account_id, time.monotonic()
    pool_module.signing_in_context = context
    return {
        "ok": True,
        "vnc": "/admin/vnc/vnc.html?path=admin/vnc/websockify&autoconnect=true&resize=scale&reconnect=true",
    }


async def login_finish(account_id: str) -> dict:
    _slot(account_id)
    try:
        jar, email = await take_session(get_settings().cdp_url, pool_module.signing_in_context)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Sign-in browser: {exc}") from exc
    if "__Secure-1PSID" not in jar:
        raise HTTPException(status_code=400, detail="Not signed in yet: finish the sign-in first")
    pool_module.signing_in = pool_module.signing_in_context = None
    log.info("account %s: signed in through the browser (%s)", account_id, email or "?")
    return await set_cookies(account_id, jar, email)


async def login_cancel(account_id: str) -> dict:
    pool_module.signing_in = pool_module.signing_in_context = None
    try:
        await cancel_sign_in(get_settings().cdp_url)
    except Exception as exc:
        log.warning("sign-in browser: %s", exc)
    return {"ok": True}
