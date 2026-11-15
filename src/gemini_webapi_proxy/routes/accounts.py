"""Account management for the admin UI (``/admin/ui``).

* ``GET  /admin/accounts`` — every account with its state, models, limits and cooldowns
* ``POST /admin/accounts`` — add an account (``{"name": ...}``), then sign in to it
* ``PATCH / DELETE /admin/accounts/{id}`` — rename, enable/disable, reorder, remove
* ``POST /admin/accounts/{id}/login/start|finish|cancel`` — sign in through the
  server's browser (shown in the UI over noVNC); finish hands the session to the proxy
* ``POST /admin/accounts/{id}/cookies`` — or paste a cookie string instead
* ``POST /admin/accounts/{id}/check`` — re-initialise the client (talks to Google)
* ``DELETE /admin/accounts/{id}/cooldowns`` — forget the limits noted for it

Reading the list never calls Google: the limits are what the library saw last.
"""

from __future__ import annotations

import asyncio
import logging
import time
from importlib import resources

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from gemini_webapi_proxy.auth import (
    SESSION_COOKIE,
    check_admin_password,
    new_session_cookie,
    require_admin,
    session_valid,
)
from gemini_webapi_proxy.client import pool as pool_module
from gemini_webapi_proxy.client.pool import pool
from gemini_webapi_proxy.config import get_settings
from gemini_webapi_proxy.cookies.cdp import chrome_proxy
from gemini_webapi_proxy.services import account_ops
from gemini_webapi_proxy.services.account_ops import reject_slave_mode

log = logging.getLogger(__name__)

ui_router = APIRouter(prefix="/admin", tags=["admin-ui"])
router = APIRouter(prefix="/admin", tags=["admin-ui"], dependencies=[Depends(require_admin)])


# ---- UI page and session ----------------------------------------------------
@ui_router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def ui_page() -> HTMLResponse:
    html = resources.files("gemini_webapi_proxy").joinpath("ui/index.html").read_text()
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


_VENDOR = {"marked.min.js", "purify.min.js"}  # Markdown for the playground, sanitised


@ui_router.get("/ui/vendor/{name}", include_in_schema=False)
async def ui_vendor(name: str) -> Response:
    if name not in _VENDOR:
        raise HTTPException(status_code=404)
    body = resources.files("gemini_webapi_proxy").joinpath(f"ui/vendor/{name}").read_bytes()
    return Response(
        body,
        media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


class SessionIn(BaseModel):
    password: str


@ui_router.post("/session")
async def sign_in(body: SessionIn, request: Request, response: Response) -> dict:
    if not check_admin_password(body.password):
        raise HTTPException(status_code=403, detail="Wrong password")
    value, max_age = new_session_cookie()
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=max_age,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
        path="/admin",
    )
    return {"ok": True}


@ui_router.delete("/session")
async def sign_out(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/admin")
    return {"ok": True}


# ---- Accounts ---------------------------------------------------------------
# Reading the list is always allowed; changing accounts is reserved to the master
# (reject_slave_mode).  The sign-in flow stays local: the browser lives here.


@router.get("/accounts")
async def list_accounts() -> dict:
    pool.configure(get_settings())
    return {
        "accounts": [s.report() for s in pool.ordered()],
        "signing_in": pool_module.current_sign_in(),
        "mode": get_settings().mode,
    }


class AccountIn(BaseModel):
    name: str


@router.post("/accounts", dependencies=[Depends(reject_slave_mode)])
async def add_account(body: AccountIn) -> dict:
    return account_ops.add_account(body.name)


class AccountPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    priority: int | None = None


@router.patch("/accounts/{account_id}", dependencies=[Depends(reject_slave_mode)])
async def update_account(account_id: str, body: AccountPatch) -> dict:
    return await account_ops.update_account(account_id, body.model_dump(exclude_none=True))


@router.delete("/accounts/{account_id}", dependencies=[Depends(reject_slave_mode)])
async def delete_account(account_id: str) -> dict:
    return await account_ops.delete_account(account_id)


@router.post("/accounts/{account_id}/check", dependencies=[Depends(reject_slave_mode)])
async def check_account(account_id: str) -> dict:
    return await account_ops.check_account(account_id)


@router.delete("/accounts/{account_id}/cooldowns", dependencies=[Depends(reject_slave_mode)])
async def clear_cooldowns(account_id: str) -> dict:
    return account_ops.clear_cooldowns(account_id)


class CookiesIn(BaseModel):
    cookies: str  # "name=value; name2=value2" as copied from a browser


@router.post("/accounts/{account_id}/cookies", dependencies=[Depends(reject_slave_mode)])
async def paste_cookies(account_id: str, body: CookiesIn) -> dict:
    return await account_ops.set_cookies(account_id, body.cookies)


# ---- Outbound proxy of an account --------------------------------------------
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


class ProxyIn(BaseModel):
    proxy: str | None = None  # http://host:port, socks5://host:port; empty: direct


_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


def _sorry_ip(location: str) -> str | None:
    """The address Google's "unusual traffic" redirect names (its ``q`` token)."""
    from base64 import urlsafe_b64decode
    from urllib.parse import parse_qs, urlsplit

    token = (parse_qs(urlsplit(location).query).get("q") or [""])[0]
    try:
        raw = urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:
        return None
    if len(raw) >= 6 and raw[0] == 0x12 and raw[1] == 4:
        return ".".join(str(b) for b in raw[2:6])
    return None


async def egress_check(proxy: str | None, cookies: dict[str, str] | None = None) -> dict:
    """Where Google sees requests from: the public address behind ``proxy``.

    With the account's session cookies the Gemini check is the one that matters:
    Google may let an anonymous visit through and still send a signed-in one to its
    "unusual traffic" page.
    """
    from curl_cffi.requests import AsyncSession

    started = time.monotonic()
    async with AsyncSession(proxy=proxy, timeout=20, impersonate="chrome") as http:
        info = (await http.get("https://ipinfo.io/json")).json()
        # Not the same thing: a VPN server may route Google domains elsewhere (WARP),
        # so whether Gemini lets this route in is checked on Gemini itself.
        session = {k: v for k, v in (cookies or {}).items() if k.startswith("__Secure-1PSID")}
        gemini = await http.get(
            "https://gemini.google.com/app", cookies=session or None, allow_redirects=False
        )
        location = gemini.headers.get("location") or ""
    return {
        "ip": info.get("ip"),
        "country": info.get("country"),
        "city": info.get("city"),
        "org": info.get("org"),
        "gemini_status": gemini.status_code,
        "gemini_signed_in": bool(session),
        "gemini_blocked": "/sorry/" in location,
        "gemini_seen_ip": _sorry_ip(location),
        "ms": round((time.monotonic() - started) * 1000),
        "checked_at": time.time(),
        "proxy": proxy,
    }


@router.post("/accounts/{account_id}/proxy", dependencies=[Depends(reject_slave_mode)])
async def set_proxy(account_id: str, body: ProxyIn) -> dict:
    """Set or clear the account's proxy, check it, and restart the account's client.

    The session stays signed in, but it was created from another address: sign in
    again through the new proxy so both match.
    """
    slot = _slot(account_id)
    _reject_remote(slot)
    proxy = (body.proxy or "").strip() or None
    if proxy:
        scheme = proxy.split("://", 1)[0].lower() if "://" in proxy else ""
        if scheme not in _PROXY_SCHEMES:
            raise HTTPException(
                status_code=400, detail="Proxy must start with http://, https:// or socks5://"
            )
        try:
            chrome_proxy(proxy)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        egress = await egress_check(proxy or get_settings().http_proxy, slot.account.cookies)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"The proxy does not work: {exc}") from exc
    changes: dict = {"proxy": proxy, "egress": egress}
    if proxy != slot.account.proxy:
        changes["proxy_set_at"] = time.time()
    _store().update(account_id, **changes)
    pool.sync()
    await pool.drop(account_id)
    return slot.report()


@router.post("/accounts/{account_id}/proxy/check", dependencies=[Depends(reject_slave_mode)])
async def check_proxy(account_id: str) -> dict:
    slot = _slot(account_id)
    _reject_remote(slot)
    try:
        egress = await egress_check(
            slot.account.proxy or get_settings().http_proxy, slot.account.cookies
        )
    except Exception as exc:
        egress = {"error": str(exc)[:300], "checked_at": time.time(), "proxy": slot.account.proxy}
    _store().update(account_id, egress=egress)
    return slot.report()


# ---- Sign-in through the server's browser ----------------------------------
@router.post("/accounts/{account_id}/login/start")
async def login_start(account_id: str) -> dict:
    return await account_ops.login_start(account_id)


@router.post("/accounts/{account_id}/login/finish")
async def login_finish(account_id: str) -> dict:
    return await account_ops.login_finish(account_id)


@router.post("/accounts/{account_id}/login/cancel")
async def login_cancel(account_id: str) -> dict:
    return await account_ops.login_cancel(account_id)


# ---- noVNC of the sign-in browser, behind the admin session ----------------
@router.get("/vnc/{path:path}", include_in_schema=False)
async def vnc_files(path: str) -> Response:
    url = f"{get_settings().vnc_url.rstrip('/')}/{path}"
    async with httpx.AsyncClient(timeout=30, trust_env=False) as http:
        upstream = await http.get(url)
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


@ui_router.websocket("/vnc/websockify")
async def vnc_socket(ws: WebSocket) -> None:
    if not session_valid(ws.cookies.get(SESSION_COOKIE)):
        await ws.close(code=4401)
        return
    requested = [p.strip() for p in (ws.headers.get("sec-websocket-protocol") or "").split(",")]
    subprotocol = "binary" if "binary" in requested else None
    target = get_settings().vnc_url.replace("http", "ws", 1).rstrip("/") + "/websockify"
    async with websockets.connect(
        target, subprotocols=[subprotocol] if subprotocol else None, max_size=None
    ) as upstream:
        await ws.accept(subprotocol=subprotocol)

        async def to_vnc() -> None:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                data = msg.get("bytes") if msg.get("bytes") is not None else msg.get("text")
                await upstream.send(data)

        async def to_browser() -> None:
            async for data in upstream:
                if isinstance(data, bytes):
                    await ws.send_bytes(data)
                else:
                    await ws.send_text(data)

        tasks = [asyncio.create_task(to_vnc()), asyncio.create_task(to_browser())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            task.exception()  # a closed tab ends one side with an error: expected
    try:
        await ws.close()
    except (RuntimeError, WebSocketDisconnect):
        pass
