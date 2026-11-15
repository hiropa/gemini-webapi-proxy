"""Generated images kept for the admin UI (``<data_dir>/images``).

Clients get images as base64 and the proxy keeps nothing by default; the gallery
holds a copy for ``GOP_IMAGE_KEEP_HOURS`` (at most ``GOP_IMAGE_KEEP_MAX`` files)
with the prompt next to it, so a generation can be looked at afterwards.

A slave keeps no copies: its media is forwarded to the master over the link and
stored only there, attributed to the slave (``node`` in the entry metadata).
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from pathlib import Path

from gemini_webapi_proxy.config import Settings
from gemini_webapi_proxy.stats import current

log = logging.getLogger(__name__)


def gallery_dir(settings: Settings) -> Path:
    return settings.data_dir / "images"


def _ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def save_to_gallery(settings: Settings, images: list[str], *, prompt: str) -> list[str]:
    """Keep a copy of the images; returns their gallery names."""
    saved: list[str] = []
    if settings.is_slave:
        return saved  # a slave's media is stored on its master, not here
    if settings.image_keep_hours <= 0 or not images:
        return saved
    folder = gallery_dir(settings)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        record = current.get() or {}
        meta = {
            "ts": time.time(),
            "prompt": prompt[:2000],
            "model": record.get("model"),
            "account": record.get("account"),
            "key": record.get("key"),
            "node": record.get("node"),  # set for a remote account: its slave's name
        }
        for b64 in images:
            data = base64.b64decode(b64)
            name = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
            (folder / f"{name}.{_ext(data)}").write_bytes(data)
            (folder / f"{name}.json").write_text(json.dumps(meta, ensure_ascii=False))
            saved.append(f"{name}.{_ext(data)}")
        prune(settings)
    except Exception as exc:  # the gallery must never break a generation
        log.warning("gallery: %s", exc)
    return saved


def entries(settings: Settings) -> list[dict]:
    folder = gallery_dir(settings)
    if not folder.is_dir():
        return []
    out = []
    for meta_path in sorted(folder.glob("*.json"), reverse=True):
        image = next((p for p in folder.glob(f"{meta_path.stem}.*") if p.suffix != ".json"), None)
        if image is None:
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except ValueError:
            meta = {}
        out.append({"name": image.name, "size": image.stat().st_size, **meta})
    return out


def prune(settings: Settings) -> None:
    folder = gallery_dir(settings)
    if not folder.is_dir():
        return
    cutoff = time.time() - settings.image_keep_hours * 3600
    metas = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for i, meta_path in enumerate(metas):
        if i >= settings.image_keep_max or meta_path.stat().st_mtime < cutoff:
            for p in folder.glob(f"{meta_path.stem}.*"):
                p.unlink(missing_ok=True)
