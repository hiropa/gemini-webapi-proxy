"""Video generation (Veo through Gemini Web) as background jobs.

Gemini Web has no "video mode" switch in the RPC: the prompt asks for a video and the
answer carries ``output.videos``.  What the answer means when there is no video was
learned the hard way by a browser bot that queued clips through the same account:

* an image instead of a video — the daily video limit is used up (Gemini says nothing);
* "limit / quota / try again later" text — the limit too;
* "encountered an error / something went wrong" — a transient glitch, retry later;
* anything else (often "I'm having a hard time fulfilling…") — a refusal.

Re-sending a prompt that is still being generated burns the limit, so jobs run one at
a time and are never retried automatically.

Veo keeps the stream silent for minutes.  gemini-webapi treats 120 s of silence as a
dead stream: it closes the connection (Google drops the unfinished turn, so it never
shows up in the history) and ``@running(retry=5)`` sends the prompt again.  So a video
call runs with the stall timeouts raised to ``video_timeout`` and with no retries.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gemini_webapi_proxy.client.pool import pool, registry
from gemini_webapi_proxy.config import Settings
from gemini_webapi_proxy.downloaders.image_export import sanitize_gemini_client_cookies
from gemini_webapi_proxy.errors import QuotaExceededError
from gemini_webapi_proxy.stats import current, journal
from gemini_webapi_proxy.utils.media import materialize_reference_images

log = logging.getLogger("gemini_webapi_proxy.video")

LIMIT_RE = re.compile(
    r"лимит|limit|квот|quota|попробуйте (снова |ещё )?(позже|через)|"
    r"try again (later|in|after)|достигли|reached",
    re.I,
)
GLITCH_RE = re.compile(
    r"encountered an error|something went wrong|что-то пошло не так|произошла ошибка", re.I
)

# status -> HTTP-ish error code reported to clients in ``error.code``
ERROR_CODES = {"limit": "video_limit_reached", "glitch": "upstream_glitch", "refused": "refused"}


@dataclass
class VideoJob:
    id: str
    model: str
    prompt: str
    seconds: str | None = None
    size: str | None = None
    references: list[str] = field(default_factory=list)
    status: str = "queued"  # queued | in_progress | completed | failed
    created_at: int = field(default_factory=lambda: int(time.time()))
    completed_at: int | None = None
    path: Path | None = None
    error: dict[str, str] | None = None
    text: str = ""
    account: str | None = None
    key: str | None = None  # client key that queued it, for the request journal

    def to_openai(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "video",
            "model": self.model,
            "status": self.status,
            "progress": 100 if self.status == "completed" else 0,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "prompt": self.prompt,
            "seconds": self.seconds,
            "size": self.size,
            "error": self.error,
        }


_jobs: dict[str, VideoJob] = {}
_lock = asyncio.Lock()


def get_job(job_id: str) -> VideoJob | None:
    return _jobs.get(job_id)


def list_jobs() -> list[VideoJob]:
    return sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)


def classify(text: str, *, videos: int, images: int) -> str:
    """Map a Gemini answer to completed / limit / glitch / refused."""
    if videos:
        return "completed"
    if images:
        return "limit"
    if LIMIT_RE.search(text):
        return "limit"
    if GLITCH_RE.search(text):
        return "glitch"
    return "refused"


def video_prompt(prompt: str, *, has_reference: bool, seconds: str | None, size: str | None) -> str:
    if has_reference:
        body = (
            "Create a video (not an image) that animates the attached image. "
            "Keep the subject, style and composition of the image.\n\n"
            f"Video description: {prompt.strip()}"
        )
    else:
        body = f"Create a video (not an image): {prompt.strip()}"
    if seconds:
        body += f"\nDuration: about {seconds} seconds."
    if size:
        body += f"\nAspect/size: {size}."
    return body


@contextmanager
def _patient_stream(client: Any, seconds: float) -> Iterator[None]:
    """Let a silent stream live ``seconds`` before the library gives up on it."""
    saved = (client.timeout, client.watchdog_timeout)
    client.timeout = client.watchdog_timeout = seconds
    try:
        yield
    finally:
        client.timeout, client.watchdog_timeout = saved


def _cleanup(settings: Settings) -> None:
    cutoff = time.time() - settings.video_keep_hours * 3600
    for job_id, job in list(_jobs.items()):
        if job.created_at < cutoff:
            if job.path:
                job.path.unlink(missing_ok=True)
            _jobs.pop(job_id, None)


def create_job(
    settings: Settings,
    *,
    model: str,
    prompt: str,
    seconds: str | None = None,
    size: str | None = None,
    references: list[str] | None = None,
) -> VideoJob:
    _cleanup(settings)
    job = VideoJob(
        id=f"video_{uuid.uuid4().hex}",
        model=model,
        prompt=prompt,
        seconds=seconds,
        size=size,
        references=references or [],
        key=(current.get() or {}).get("key"),
    )
    _jobs[job.id] = job
    task = asyncio.get_running_loop().create_task(run_job(settings, job))
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    return job


class _UnfinishedError(Exception):
    """An answer without a video that is not the account's limit: reported, not retried."""

    def __init__(self, kind: str, text: str) -> None:
        super().__init__(text)
        self.kind = kind


async def run_job(settings: Settings, job: VideoJob) -> None:
    record = {"ts": time.time(), "endpoint": "video-job", "model": job.model, "key": job.key}
    token = current.set(record)
    try:
        await _run_job(settings, job)
    finally:
        current.reset(token)
        record["video"] = job.id
        record["status"] = (
            200
            if job.status == "completed"
            else (429 if (job.error or {}).get("code") == ERROR_CODES["limit"] else 502)
        )
        record["ms"] = round((time.time() - record["ts"]) * 1000)
        if job.error:
            record["error"] = job.error.get("message", "")[:300]
        journal.add(record)


async def _run_job(settings: Settings, job: VideoJob) -> None:
    async with _lock:
        job.status = "in_progress"
        ref_paths: list[Path] = []

        async def call(client, runtime) -> Path:
            if hasattr(client, "request_mode"):  # remote node account
                client.request_mode = "video"
            slot = pool.slot_of(client)
            job.account = slot.id if slot else None
            sanitize_gemini_client_cookies(client)
            with _patient_stream(client, settings.video_timeout):
                output = await asyncio.wait_for(
                    client.generate_content(
                        video_prompt(
                            job.prompt,
                            has_reference=bool(ref_paths),
                            seconds=job.seconds,
                            size=job.size,
                        ),
                        files=ref_paths or None,
                        model=runtime,
                        current_retry=0,  # read by gemini-webapi's @running decorator
                    ),
                    timeout=settings.video_timeout + 30,
                )
            job.text = (output.text or "").strip()
            videos = list(getattr(output, "videos", None) or [])
            kind = classify(job.text, videos=len(videos), images=len(output.images or []))
            if kind == "limit":
                # This account's daily videos are used up: the next account gets the job.
                raise QuotaExceededError(job.text or "video limit (Gemini answered with an image)")
            if kind != "completed":
                raise _UnfinishedError(kind, job.text or "Gemini returned no video")
            out_dir = settings.data_dir / "videos"
            saved = await asyncio.wait_for(
                videos[0].save(path=str(out_dir), filename=job.id), timeout=settings.video_timeout
            )
            return Path(saved["video"]) if isinstance(saved, dict) else Path(str(saved))

        try:
            if job.references:
                ref_paths = await materialize_reference_images(
                    job.references, proxy=settings.http_proxy
                )
            path = await pool.run(
                settings,
                registry.resolve_id(job.model),
                call,
                cooldown_key=f"video:{registry.resolve_id(job.model)}",
                cooldown_seconds=settings.video_cooldown,
            )
            job.path = path
            job.status = "completed"
            job.completed_at = int(time.time())
            log.info(
                "video %s done on %s: %s (%d bytes)",
                job.id,
                job.account,
                path.name,
                path.stat().st_size,
            )
        except _UnfinishedError as exc:
            _fail(job, exc.kind, str(exc))
        except QuotaExceededError as exc:
            _fail(job, "limit", str(exc))
        except TimeoutError:
            _fail(job, "glitch", "Video generation timed out")
        except Exception as exc:  # reported to the client through the job
            text = str(exc)
            _fail(job, "limit" if LIMIT_RE.search(text) else "glitch", text)
        finally:
            for p in ref_paths:
                p.unlink(missing_ok=True)
            if ref_paths and ref_paths[0].parent.name.startswith("gemini-ref-"):
                ref_paths[0].parent.rmdir()


def _fail(job: VideoJob, kind: str, message: str) -> None:
    job.status = "failed"
    job.completed_at = int(time.time())
    job.error = {"code": ERROR_CODES.get(kind, kind), "message": message[:1000]}
    log.warning("video %s failed (%s): %s", job.id, kind, message[:200])
