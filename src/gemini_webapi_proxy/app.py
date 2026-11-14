"""FastAPI application factory.

Entry point: ``uvicorn gemini_webapi_proxy.app:app``.
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request

from gemini_webapi_proxy import __version__
from gemini_webapi_proxy.client.pool import lifespan
from gemini_webapi_proxy.logging import configure_logging
from gemini_webapi_proxy.routes import (
    accounts,
    admin,
    chat,
    dashboard,
    health,
    images,
    models,
    nodes,
    telegram,
    videos,
)
from gemini_webapi_proxy.stats import current, journal

# Initialise logging before anything else gets a chance to log.
configure_logging()
logger = logging.getLogger(__name__)

# Module-level ``app`` for uvicorn's ``--factory`` and default ``app:app`` lookups.
app = FastAPI(
    title="Gemini WebAPI Proxy",
    description=(
        "OpenAI-compatible API gateway for Google Gemini Web, "
        f"powered by gemini-webapi. v{__version__}"
    ),
    version=__version__,
    lifespan=lifespan,
)
logger.info("gemini-webapi-proxy v%s initialised", __version__)

_ENDPOINTS = {
    "/openai/v1/chat/completions": "chat",
    "/openai/v1/images/generations": "image",
    "/openai/v1/images/edits": "edit",
    "/openai/v1/videos": "video",
}


@app.middleware("http")
async def request_journal(request: Request, call_next):
    """One journal record per generation request (see stats.py)."""
    endpoint = _ENDPOINTS.get(request.url.path) if request.method == "POST" else None
    if endpoint is None:
        return await call_next(request)
    record: dict = {"ts": time.time(), "endpoint": endpoint}
    token = current.set(record)
    try:
        response = await call_next(request)
        record["status"] = response.status_code
        return response
    except Exception:
        record["status"] = 500
        raise
    finally:
        current.reset(token)
        record["ms"] = round((time.time() - record["ts"]) * 1000)
        journal.add(record)


# Mount routers. Order does not matter.
for r in (
    health.router,
    models.router,
    chat.router,
    images.router,
    videos.router,
    admin.router,
    accounts.ui_router,
    accounts.router,
    dashboard.router,
    nodes.router,
    nodes.admin_router,
    telegram.router,
):
    app.include_router(r)


def create_app() -> FastAPI:
    """Factory alternative to the module-level ``app`` (for tests)."""
    return app
