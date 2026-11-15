"""Admin UI data beyond accounts: overview stats, request journal, client keys,
models, media, system, playground.  All under ``/admin`` behind :func:`require_admin`.
"""

from __future__ import annotations

import platform
import shutil
import time
from importlib import metadata
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from gemini_webapi_proxy import __version__
from gemini_webapi_proxy.auth import require_admin
from gemini_webapi_proxy.client.pool import pinned, pool, registry
from gemini_webapi_proxy.config import get_settings
from gemini_webapi_proxy.keys import key_store
from gemini_webapi_proxy.schemas import ChatMessage
from gemini_webapi_proxy.services import gallery, video
from gemini_webapi_proxy.stats import current, journal, summary

router = APIRouter(prefix="/admin", tags=["admin-ui"], dependencies=[Depends(require_admin)])

STARTED_AT = time.time()


# ---- Overview and journal ---------------------------------------------------
@router.get("/stats")
async def stats(hours: int = 24) -> dict:
    hours = max(1, min(hours, 24 * 14))
    return summary(journal.since(hours * 3600), hours=hours)


@router.get("/requests")
async def requests(
    limit: int = 200,
    account: str | None = None,
    model: str | None = None,
    key: str | None = None,
    endpoint: str | None = None,
    outcome: str | None = None,
) -> dict:
    items = journal.recent(
        max(1, min(limit, 1000)),
        account=account,
        model=model,
        key=key,
        endpoint=endpoint,
        outcome=outcome,
    )
    return {"requests": items}


# ---- Client keys -------------------------------------------------------------
def _keys():
    return key_store(get_settings().data_dir)


def _key_usage() -> dict[str, dict]:
    usage: dict[str, dict] = {}
    for record in journal.records:
        name = record.get("key")
        if not name:
            continue
        entry = usage.setdefault(name, {"requests": 0, "last_used": None})
        entry["requests"] += 1
        entry["last_used"] = record.get("ts")
    return usage


@router.get("/keys")
async def list_keys() -> dict:
    usage = _key_usage()
    keys = [
        {**k.public(), **usage.get(k.name, {"requests": 0, "last_used": None})}
        for k in _keys().all()
    ]
    return {
        "master": {
            "set": bool(get_settings().api_key),
            **usage.get("master", {"requests": 0, "last_used": None}),
        },
        "keys": keys,
    }


class KeyIn(BaseModel):
    name: str


@router.post("/keys")
async def create_key(body: KeyIn) -> dict:
    key, secret = _keys().create(body.name)
    return {**key.public(), "secret": secret}


class KeyPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None


@router.patch("/keys/{key_id}")
async def update_key(key_id: str, body: KeyPatch) -> dict:
    try:
        return _keys().update(key_id, **body.model_dump(exclude_none=True)).public()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No key {key_id!r}") from exc


@router.delete("/keys/{key_id}")
async def delete_key(key_id: str) -> dict:
    try:
        _keys().remove(key_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No key {key_id!r}") from exc
    return {"ok": True}


# ---- Models --------------------------------------------------------------------
@router.get("/models")
async def models() -> dict:
    pool.configure(get_settings())
    by_account = {
        s.id: {m.model_name for m in s.models() if m.is_available and m.model_name}
        for s in pool.ordered()
    }
    out = []
    for entry in registry.entries:
        targets = {entry.id, *entry.aliases}
        out.append(
            {
                "id": entry.id,
                "name": entry.display_name,
                "description": entry.description,
                "kind": entry.kind,
                "chat": entry.chat_ok,
                "image": entry.image_ok,
                "aliases": entry.aliases,
                "accounts": {aid: bool(targets & names) for aid, names in by_account.items()},
            }
        )
    return {"models": out, "accounts": list(by_account)}


# ---- Media ---------------------------------------------------------------------
@router.get("/videos")
async def videos() -> dict:
    return {
        "videos": [
            {
                **j.to_openai(),
                "account": j.account,
                "node": j.node,
                "key": j.key,
                "has_file": bool(j.path),
            }
            for j in video.list_jobs()
        ]
    }


@router.get("/videos/{video_id}/content")
async def video_content(video_id: str):
    job = video.get_job(video_id)
    if not job or not job.path or not job.path.exists():
        raise HTTPException(status_code=404, detail="No such video")
    return FileResponse(job.path, media_type="video/mp4")


@router.get("/gallery")
async def gallery_list() -> dict:
    return {"images": gallery.entries(get_settings())}


def _gallery_file(name: str) -> Path:
    folder = gallery.gallery_dir(get_settings())
    path = folder / Path(name).name
    if path.suffix == ".json" or not path.is_file():
        raise HTTPException(status_code=404, detail="No such image")
    return path


@router.get("/gallery/{name}")
async def gallery_file(name: str):
    return FileResponse(_gallery_file(name), headers={"Cache-Control": "private, max-age=86400"})


@router.delete("/gallery/{name}")
async def gallery_delete(name: str) -> dict:
    path = _gallery_file(name)
    path.unlink(missing_ok=True)
    path.with_suffix(".json").unlink(missing_ok=True)
    return {"ok": True}


# ---- System --------------------------------------------------------------------
def _rss_mb() -> float | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


async def _probe(url: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as http:
            resp = await http.get(url)
        return {"ok": resp.status_code < 400, "status": resp.status_code, "body": resp.text[:300]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


@router.get("/system")
async def system() -> dict:
    settings = get_settings()
    browser = await _probe(settings.cdp_url.rstrip("/") + "/json/version")
    if browser.get("ok"):
        try:
            import json

            browser["version"] = json.loads(browser.pop("body")).get("Browser")
        except ValueError:
            pass
    browser.pop("body", None)
    vnc = await _probe(settings.vnc_url.rstrip("/") + "/vnc.html")
    vnc.pop("body", None)
    disk = shutil.disk_usage(settings.data_dir) if settings.data_dir.exists() else None
    return {
        "version": __version__,
        "gemini_webapi": metadata.version("gemini-webapi"),
        "python": platform.python_version(),
        "started_at": STARTED_AT,
        "rss_mb": _rss_mb(),
        "disk": disk and {"total": disk.total, "free": disk.free},
        "browser": browser,
        "vnc": vnc,
        "journal_records": len(journal.records),
        "settings": {
            "mode": settings.mode,
            "cookie_source": settings.cookie_source,
            "cdp_url": settings.cdp_url,
            "vnc_url": settings.vnc_url,
            "data_dir": str(settings.data_dir),
            "api_key": bool(settings.api_key),
            "admin_password": bool(settings.admin_password),
            "chat_timeout": settings.chat_timeout,
            "image_timeout": settings.image_timeout,
            "video_timeout": settings.video_timeout,
            "quota_cooldown": settings.quota_cooldown,
            "video_cooldown": settings.video_cooldown,
            "account_concurrency": settings.account_concurrency,
            "account_min_interval": settings.account_min_interval,
            "image_keep_hours": settings.image_keep_hours,
            "video_keep_hours": settings.video_keep_hours,
        },
    }


# ---- Playground ----------------------------------------------------------------
class PlaygroundTurn(BaseModel):
    role: str  # user | assistant
    content: str


class PlaygroundIn(BaseModel):
    prompt: str
    # Earlier turns of the playground chat; sent along so the model has the context.
    history: list[PlaygroundTurn] = []
    model: str = "gemini-flash"
    mode: str = "chat"  # chat | image | video
    account: str | None = None  # None: whichever account the pool picks


@router.post("/playground")
async def playground(body: PlaygroundIn):
    from gemini_webapi_proxy.routes._errors import map_api_error
    from gemini_webapi_proxy.services.chat import (
        create_chat_completion,
        create_image_generation,
    )

    settings = get_settings()
    record: dict = {"ts": time.time(), "endpoint": f"playground-{body.mode}", "key": "admin-ui"}
    tokens = (current.set(record), pinned.set(body.account or None))
    try:
        if body.mode == "video":
            # A background job (minutes); it inherits the pinned account. The UI
            # follows it through /admin/videos.
            job = video.create_job(settings, model=body.model, prompt=body.prompt)
            record["status"] = 200
            record["video"] = job.id
            return {"video": job.to_openai(), "model": body.model, "ms": 0}
        if body.mode == "image":
            resp = await create_image_generation(
                settings,
                model=body.model,
                prompt=body.prompt,
                n=1,
                size=None,
                response_format="b64_json",
            )
            gallery_names = record.get("gallery") or []
            # Links into the gallery keep the playground history small; without a
            # gallery (GOP_IMAGE_KEEP_HOURS=0) the images come inline.
            inline = [] if gallery_names else [d.b64_json for d in resp.data if d.b64_json]
            result = {"images": inline, "gallery": gallery_names, "text": ""}
        else:
            turns = [ChatMessage(role=t.role, content=t.content) for t in body.history[-20:]]
            resp = await create_chat_completion(
                settings,
                model=body.model,
                messages=[*turns, ChatMessage(role="user", content=body.prompt)],
            )
            result = {
                "text": resp.choices[0].message.get("content") or "",
                "images": [],
                "gallery": [],
            }
        record["status"] = 200
        return {
            **result,
            "account": record.get("account"),
            "tried": record.get("tried", []),
            "model": record.get("model"),
            "ms": round((time.time() - record["ts"]) * 1000),
        }
    except Exception as exc:
        err = await map_api_error(exc, settings)
        record["status"] = err.status_code
        return err
    finally:
        pinned.reset(tokens[1])
        current.reset(tokens[0])
        record["ms"] = round((time.time() - record["ts"]) * 1000)
        journal.add(record)
