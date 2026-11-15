"""Linked nodes (see docs/nodes.md): this instance as a slave (``/node/*``) or as a
master managing its slaves (``/admin/nodes``).

Slave side: ``/node/*`` is guarded by the node token (``GOP_NODE_TOKEN``, or one
generated in the admin UI and kept in ``<data_dir>/node_token.txt``).  It exposes
the local accounts and runs generations pinned to one account, so the master can
use them exactly like local ones.  Only public account data crosses the link —
never cookies.

Master side: ``/admin/nodes`` lists/links slaves; their accounts appear in the
pool as remote slots (``<node>::<account>``).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security.utils import get_authorization_scheme_param
from gemini_webapi.exceptions import AuthError
from pydantic import BaseModel

from gemini_webapi_proxy import __version__
from gemini_webapi_proxy.auth import require_admin
from gemini_webapi_proxy.client.nodes import (
    NodeError,
    effective_node_token,
    mask_token,
    new_node_token,
)
from gemini_webapi_proxy.client.nodes import (
    nodes as nodes_manager,
)
from gemini_webapi_proxy.client.pool import pinned, pool, registry
from gemini_webapi_proxy.config import get_settings
from gemini_webapi_proxy.downloaders.chain import generated_image_to_b64
from gemini_webapi_proxy.errors import NoAccountError, QuotaExceededError, UpstreamRefusalError
from gemini_webapi_proxy.services import account_ops, video
from gemini_webapi_proxy.services.chat import (
    _quota_message,
    _remove_refs,
    create_image_generation,
)
from gemini_webapi_proxy.utils.media import materialize_reference_images

log = logging.getLogger(__name__)

router = APIRouter(prefix="/node", tags=["node"])
admin_router = APIRouter(prefix="/admin", tags=["admin-ui"], dependencies=[Depends(require_admin)])


# ---- Slave side ---------------------------------------------------------------
def require_node_token(request: Request) -> None:
    token = effective_node_token(get_settings())
    if not token:
        raise HTTPException(status_code=404, detail="Node API is disabled: no node token set")
    scheme, param = get_authorization_scheme_param(request.headers.get("Authorization") or "")
    if (
        scheme.lower() != "bearer"
        or not param
        or not hmac.compare_digest(param.encode(), token.encode())
    ):
        raise HTTPException(status_code=401, detail="Invalid node token")


@router.get("/ping", dependencies=[Depends(require_node_token)])
async def node_ping() -> dict:
    return {"ok": True, "version": __version__}


@router.get("/accounts", dependencies=[Depends(require_node_token)])
async def node_accounts() -> dict:
    pool.configure(get_settings())
    return {"accounts": [s.report() for s in pool.ordered() if not s.is_remote]}


# ---- Slave side: the master drives this slave's accounts over the link --------
# The local admin API refuses account changes in slave mode (reject_slave_mode);
# these endpoints are their counterpart, guarded by the node token instead.


class NodeAccountIn(BaseModel):
    name: str


class NodeAccountPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    priority: int | None = None


@router.post("/accounts", dependencies=[Depends(require_node_token)])
async def node_add_account(body: NodeAccountIn) -> dict:
    return account_ops.add_account(body.name)


@router.patch("/accounts/{account_id}", dependencies=[Depends(require_node_token)])
async def node_update_account(account_id: str, body: NodeAccountPatch) -> dict:
    return await account_ops.update_account(account_id, body.model_dump(exclude_none=True))


@router.delete("/accounts/{account_id}", dependencies=[Depends(require_node_token)])
async def node_delete_account(account_id: str) -> dict:
    return await account_ops.delete_account(account_id)


@router.post("/accounts/{account_id}/cookies", dependencies=[Depends(require_node_token)])
async def node_set_cookies(account_id: str, body: account_ops.CookiesIn) -> dict:
    return await account_ops.set_cookies(account_id, body.cookies)


@router.post("/accounts/{account_id}/check", dependencies=[Depends(require_node_token)])
async def node_check_account(account_id: str) -> dict:
    return await account_ops.check_account(account_id)


@router.delete("/accounts/{account_id}/cooldowns", dependencies=[Depends(require_node_token)])
async def node_clear_cooldowns(account_id: str) -> dict:
    return account_ops.clear_cooldowns(account_id)


@router.post("/accounts/{account_id}/login/start", dependencies=[Depends(require_node_token)])
async def node_login_start(account_id: str) -> dict:
    return await account_ops.login_start(account_id)


@router.post("/accounts/{account_id}/login/finish", dependencies=[Depends(require_node_token)])
async def node_login_finish(account_id: str) -> dict:
    return await account_ops.login_finish(account_id)


@router.post("/accounts/{account_id}/login/cancel", dependencies=[Depends(require_node_token)])
async def node_login_cancel(account_id: str) -> dict:
    return await account_ops.login_cancel(account_id)


class NodeGenerateIn(BaseModel):
    account: str
    model: str = "gemini-flash"
    prompt: str
    images: list[str] = []  # data: or http(s) URLs of reference images
    mode: str = "chat"  # chat | image | video
    seconds: str | None = None
    size: str | None = None


def _err(status: int, code: str, message: str, **extra: object) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": str(message)[:1000], **extra}},
    )


async def _node_chat(settings, body: NodeGenerateIn) -> dict:
    resolved = registry.resolve_id(body.model)
    # Slave mode: reference images are fetched via this machine's own address too.
    ref_proxy = None if settings.is_slave else settings.http_proxy
    ref_paths = (
        await materialize_reference_images(body.images, proxy=ref_proxy) if body.images else []
    )

    async def call(client, runtime) -> dict:
        output = await asyncio.wait_for(
            client.generate_content(body.prompt, files=ref_paths or None, model=runtime),
            timeout=settings.chat_timeout,
        )
        text = (output.text or "").strip()
        images = [
            {
                "b64": await generated_image_to_b64(img, settings=settings, client=client),
                "mime": "image/png",
            }
            for img in (output.images or [])
        ]
        if _quota_message(text) and not images:
            raise QuotaExceededError(text)
        return {"text": text, "images": images}

    try:
        return await pool.run(settings, resolved, call)
    finally:
        _remove_refs(ref_paths)


async def _node_image(settings, body: NodeGenerateIn) -> dict:
    resp = await create_image_generation(
        settings,
        model=body.model,
        prompt=body.prompt,
        n=1,
        size=body.size,
        response_format="b64_json",
        reference_images=body.images or None,
    )
    return {
        "text": "",
        "images": [{"b64": d.b64_json, "mime": "image/png"} for d in resp.data if d.b64_json],
    }


async def _node_video(settings, body: NodeGenerateIn) -> dict:
    job = video.create_job(
        settings,
        model=body.model,
        prompt=body.prompt,
        seconds=body.seconds,
        size=body.size,
        references=body.images or None,
    )
    # Wait for the job here so the caller gets a finished answer (or a failure it
    # can fail over on); the mp4 itself is fetched via /node/jobs/{id}/content.
    deadline = time.monotonic() + settings.video_timeout
    while time.monotonic() < deadline:
        done = video.get_job(job.id)
        if done and done.status in ("completed", "failed"):
            job = done
            break
        await asyncio.sleep(5)
    if job.status == "failed" and (job.error or {}).get("code") == video.ERROR_CODES["limit"]:
        raise QuotaExceededError((job.error or {}).get("message") or "video limit reached")
    out = {"job": job.id, "status": job.status, "text": job.text}
    if job.error:
        out["error"] = job.error
    return out


@router.post("/generate", dependencies=[Depends(require_node_token)])
async def node_generate(body: NodeGenerateIn):
    settings = get_settings()
    pool.configure(settings)
    if not any(s.id == body.account and not s.is_remote for s in pool.ordered()):
        return _err(404, "no_account", f"no local account {body.account!r}")
    token = pinned.set(body.account)
    try:
        if body.mode == "video":
            return await _node_video(settings, body)
        if body.mode == "image":
            return await _node_image(settings, body)
        return await _node_chat(settings, body)
    except QuotaExceededError as exc:
        slot = pool.slots.get(body.account)
        reset = max(slot.cooldowns.values()) if slot and slot.cooldowns else None
        return _err(429, "quota", exc, reset_at=reset)
    except NoAccountError as exc:
        return _err(429, "unavailable", exc)
    except UpstreamRefusalError as exc:
        return _err(403, "refused", exc)
    except AuthError as exc:
        return _err(401, "auth", exc)
    except TimeoutError:
        return _err(504, "timeout", "generation timed out")
    except Exception as exc:
        log.warning("node generate failed: %s", exc)
        return _err(502, "error", exc)
    finally:
        pinned.reset(token)


@router.get("/jobs/{job_id}", dependencies=[Depends(require_node_token)])
async def node_job(job_id: str) -> dict:
    job = video.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"video {job_id} not found")
    out = job.to_openai()
    out["has_file"] = bool(job.path and job.path.exists())
    return out


@router.get("/jobs/{job_id}/content", dependencies=[Depends(require_node_token)])
async def node_job_content(job_id: str):
    job = video.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"video {job_id} not found")
    if job.status != "completed" or not job.path or not job.path.exists():
        raise HTTPException(status_code=409, detail=f"video {job_id} is {job.status}")
    return FileResponse(job.path, media_type="video/mp4", filename=f"{job.id}.mp4")


@router.delete("/jobs/{job_id}", dependencies=[Depends(require_node_token)])
async def node_job_delete(job_id: str) -> dict:
    """The master fetched the mp4: the slave keeps no copy (see docs/nodes.md)."""
    if not video.delete_job(job_id):
        raise HTTPException(status_code=404, detail=f"video {job_id} not found")
    return {"ok": True}


# ---- Master side: manage linked slaves -----------------------------------------
@admin_router.get("/nodes")
async def list_nodes() -> dict:
    store = nodes_manager.configure(get_settings())
    return {"nodes": [n.with_accounts() for n in store.all()]}


class NodeIn(BaseModel):
    name: str
    url: str
    token: str = ""
    priority: int | None = None


@admin_router.post("/nodes")
async def add_node(body: NodeIn) -> dict:
    settings = get_settings()
    url = body.url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url must start with http:// or https://")
    token = body.token.strip()
    if not token:
        raise HTTPException(
            status_code=400,
            detail="token is required — the slave's node token (admin UI there: System/Node, or GOP_NODE_TOKEN)",
        )
    store = nodes_manager.configure(settings)
    node = store.add(body.name, url, token, priority=body.priority)
    await nodes_manager.refresh_node(node)
    if not node.online:
        store.remove(node.id)
        raise HTTPException(status_code=502, detail=f"Cannot reach {url}: {node.error}")
    pool.configure(settings)
    return node.with_accounts()


class NodePatch(BaseModel):
    name: str | None = None
    url: str | None = None
    token: str | None = None
    enabled: bool | None = None
    priority: int | None = None


@admin_router.patch("/nodes/{node_id}")
async def update_node(node_id: str, body: NodePatch) -> dict:
    settings = get_settings()
    store = nodes_manager.configure(settings)
    node = store.get(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"No node {node_id!r}")
    changes = body.model_dump(exclude_none=True)
    if changes.get("url"):
        changes["url"] = str(changes["url"]).strip().rstrip("/")
        if not changes["url"].startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="url must start with http:// or https://")
    if changes:
        store.update(node_id, **changes)
        node = store.get(node_id)
    if node.enabled:
        await nodes_manager.refresh_node(node)
    pool.configure(settings)
    return node.with_accounts()


@admin_router.delete("/nodes/{node_id}")
async def delete_node(node_id: str) -> dict:
    settings = get_settings()
    store = nodes_manager.configure(settings)
    if store.get(node_id) is None:
        raise HTTPException(status_code=404, detail=f"No node {node_id!r}")
    store.remove(node_id)
    pool.configure(settings)
    return {"ok": True}


@admin_router.post("/nodes/{node_id}/refresh")
async def refresh_node(node_id: str) -> dict:
    settings = get_settings()
    store = nodes_manager.configure(settings)
    node = store.get(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"No node {node_id!r}")
    await nodes_manager.refresh_node(node)
    pool.configure(settings)
    return node.with_accounts()


# ---- Master side: manage a slave's accounts over the link ----------------------
# A slave cannot add or change accounts itself (slave mode); every configuration
# and status change goes through here, over the node token-guarded link.


def _managed_node(node_id: str):
    store = nodes_manager.configure(get_settings())
    node = store.get(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"No node {node_id!r}")
    return node


async def _forward(call, node, what: str) -> dict:  # type: ignore[no-untyped-def]
    """Run a NodeClient call, mapping link failures to a 502."""
    try:
        return await call
    except NodeError as exc:
        raise HTTPException(
            status_code=502 if exc.status >= 500 else exc.status,
            detail=f"{node.name}: {what} failed: {exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{node.name}: {what} failed: {exc}") from exc


@admin_router.post("/nodes/{node_id}/accounts")
async def master_add_account(node_id: str, body: NodeAccountIn) -> dict:
    node = _managed_node(node_id)
    client = nodes_manager.client_for(node)
    out = await _forward(client.account_add(body.name), node, "add account")
    await nodes_manager.refresh_node(node)
    return out


@admin_router.patch("/nodes/{node_id}/accounts/{account_id}")
async def master_update_account(node_id: str, account_id: str, body: NodeAccountPatch) -> dict:
    node = _managed_node(node_id)
    client = nodes_manager.client_for(node)
    out = await _forward(
        client.account_update(account_id, body.model_dump(exclude_none=True)),
        node,
        "update account",
    )
    await nodes_manager.refresh_node(node)
    return out


@admin_router.delete("/nodes/{node_id}/accounts/{account_id}")
async def master_delete_account(node_id: str, account_id: str) -> dict:
    node = _managed_node(node_id)
    client = nodes_manager.client_for(node)
    out = await _forward(client.account_delete(account_id), node, "delete account")
    await nodes_manager.refresh_node(node)
    return out


@admin_router.post("/nodes/{node_id}/accounts/{account_id}/cookies")
async def master_set_cookies(node_id: str, account_id: str, body: account_ops.CookiesIn) -> dict:
    node = _managed_node(node_id)
    client = nodes_manager.client_for(node)
    out = await _forward(client.account_cookies(account_id, body.cookies), node, "set cookies")
    await nodes_manager.refresh_node(node)
    return out


@admin_router.post("/nodes/{node_id}/accounts/{account_id}/check")
async def master_check_account(node_id: str, account_id: str) -> dict:
    node = _managed_node(node_id)
    client = nodes_manager.client_for(node)
    out = await _forward(client.account_check(account_id), node, "check account")
    await nodes_manager.refresh_node(node)
    return out


@admin_router.delete("/nodes/{node_id}/accounts/{account_id}/cooldowns")
async def master_clear_cooldowns(node_id: str, account_id: str) -> dict:
    node = _managed_node(node_id)
    client = nodes_manager.client_for(node)
    out = await _forward(client.account_clear_cooldowns(account_id), node, "reset limits")
    await nodes_manager.refresh_node(node)
    return out


@admin_router.post("/nodes/{node_id}/accounts/{account_id}/login/start")
async def master_login_start(node_id: str, account_id: str) -> dict:
    node = _managed_node(node_id)
    client = nodes_manager.client_for(node)
    return await _forward(client.login_start(account_id), node, "start sign-in")


@admin_router.post("/nodes/{node_id}/accounts/{account_id}/login/finish")
async def master_login_finish(node_id: str, account_id: str) -> dict:
    node = _managed_node(node_id)
    client = nodes_manager.client_for(node)
    out = await _forward(client.login_finish(account_id), node, "finish sign-in")
    await nodes_manager.refresh_node(node)
    return out


@admin_router.post("/nodes/{node_id}/accounts/{account_id}/login/cancel")
async def master_login_cancel(node_id: str, account_id: str) -> dict:
    node = _managed_node(node_id)
    client = nodes_manager.client_for(node)
    return await _forward(client.login_cancel(account_id), node, "cancel sign-in")


# ---- Slave side helper: the node token itself ------------------------------------
@admin_router.get("/node-token")
async def get_node_token() -> dict:
    token = effective_node_token(get_settings())
    return {"token_set": bool(token), "token": mask_token(token)}


@admin_router.post("/node-token")
async def regenerate_node_token() -> dict:
    """Generate a fresh node token, persisted in <data_dir>/node_token.txt (0600)."""
    token = new_node_token(get_settings())
    log.info("node token regenerated")
    return {"token_set": True, "token": token}
