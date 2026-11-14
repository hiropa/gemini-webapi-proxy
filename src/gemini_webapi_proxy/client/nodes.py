"""Linked nodes: use another machine's accounts from this one (see docs/nodes.md).

A *slave* is an ordinary instance of this proxy with a node token set
(``GOP_NODE_TOKEN``); it serves ``/node/*`` (ping, accounts, generate, video jobs)
to whoever presents the token.  A *master* keeps the slaves it links to in
``<data_dir>/nodes.json`` (mode 0600) and turns each of their accounts into a
remote pool slot (:class:`pool.RemoteSlot`).  Generations for a remote account
are forwarded to the slave over HTTPS and run there through its own pool, pinned
to that account — so Google sees the slave's own address, not a proxy.

Nothing but application-level JSON crosses the link; there is no raw TCP/SOCKS
forwarding to break.  Put the slave behind TLS (or an SSH tunnel) like the rest
of the API.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import httpx
from gemini_webapi.constants import AccountStatus
from gemini_webapi.exceptions import AuthError

from gemini_webapi_proxy import events
from gemini_webapi_proxy.config import Settings
from gemini_webapi_proxy.errors import QuotaExceededError

log = logging.getLogger(__name__)

NODES_FILE = "nodes.json"
NODE_TOKEN_FILE = "node_token.txt"


# ---- Slave-side node token ---------------------------------------------------
def effective_node_token(settings: Settings) -> str | None:
    """``GOP_NODE_TOKEN``, else the token persisted in <data_dir> (created on demand)."""
    if settings.node_token:
        return settings.node_token
    path = settings.data_dir / NODE_TOKEN_FILE
    if path.is_file():
        return path.read_text().strip() or None
    return None


def new_node_token(settings: Settings) -> str:
    """Generate a fresh slave token and persist it (env var still wins if set)."""
    token = "gop-node-" + secrets.token_urlsafe(32)
    path = settings.data_dir / NODE_TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


def mask_token(token: str | None) -> str:
    if not token:
        return ""
    return f"{token[:8]}…{token[-4:]}" if len(token) > 12 else "***"


# ---- Master-side store of linked slaves --------------------------------------
@dataclass
class Node:
    id: str
    name: str
    url: str  # e.g. https://slave.example.com:4982
    token: str  # the slave's node token
    enabled: bool = True
    priority: int = 0  # lower is tried first (remote slots inherit it)
    # runtime state, not persisted:
    accounts: list[dict] = field(default_factory=list)  # last /node/accounts payload
    online: bool = False
    last_ok: float | None = None
    error: str | None = None

    def public(self) -> dict:
        out = asdict(self)
        out["token"] = mask_token(self.token)
        out["token_set"] = bool(self.token)
        out.pop("accounts")
        return out

    def with_accounts(self) -> dict:
        out = self.public()
        out["accounts"] = self.accounts
        return out


class NodeStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._nodes: dict[str, Node] = {}
        if path.is_file():
            raw = json.loads(path.read_text())
            self._nodes = {n["id"]: Node(**n) for n in raw.get("nodes", [])}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"nodes": [asdict(n) for n in self._nodes.values()]}, f, indent=2)
        tmp.replace(self.path)

    def all(self) -> list[Node]:
        return sorted(self._nodes.values(), key=lambda n: (n.priority, n.id))

    def get(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def add(self, name: str, url: str, token: str, *, priority: int | None = None) -> Node:
        base = name.lower()
        node_id, n = base, 2
        while node_id in self._nodes:
            node_id, n = f"{base}-{n}", n + 1
        if priority is None:
            priority = max((x.priority for x in self._nodes.values()), default=-1) + 1
        node = Node(
            id=node_id,
            name=name.strip() or node_id,
            url=url.rstrip("/"),
            token=token,
            priority=priority,
        )
        self._nodes[node_id] = node
        self._save()
        return node

    def update(self, node_id: str, **changes) -> Node:
        node = self._nodes[node_id]
        for key, value in changes.items():
            setattr(node, key, value)
        self._save()
        return node

    def remove(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)
        self._save()


class NodeError(Exception):
    """The slave answered with an error status; ``code`` says why."""

    def __init__(self, status: int, code: str, message: str, payload: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload or {}


class NodeClient:
    """HTTP client for one slave's ``/node/*`` API."""

    def __init__(self, node: Node) -> None:
        self.node = node
        self._http: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.node.url,
                timeout=httpx.Timeout(60.0, connect=10.0),
                trust_env=False,
                headers={"Authorization": f"Bearer {self.node.token}"},
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _call(self, method: str, path: str, **kwargs) -> Any:
        http = await self._client()
        resp = await http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                raise NodeError(
                    resp.status_code,
                    str(err.get("code") or "error"),
                    str(err.get("message") or resp.text[:300]),
                    body,
                )
            raise NodeError(
                resp.status_code,
                "error",
                (body.get("detail") if isinstance(body, dict) else None) or resp.text[:300],
                body if isinstance(body, dict) else None,
            )
        return resp.json() if resp.content else {}

    async def ping(self) -> dict:
        return await self._call("GET", "/node/ping")

    async def accounts(self) -> list[dict]:
        data = await self._call("GET", "/node/accounts")
        return data.get("accounts") or []

    async def generate(
        self,
        *,
        account: str,
        model: str,
        prompt: str,
        images: list[str] | None = None,
        mode: str = "chat",
        seconds: str | None = None,
        size: str | None = None,
        timeout: float = 900.0,
    ) -> dict:
        http = await self._client()
        resp = await http.post(
            "/node/generate",
            json={
                "account": account,
                "model": model,
                "prompt": prompt,
                "images": images or [],
                "mode": mode,
                "seconds": seconds,
                "size": size,
            },
            timeout=timeout,
        )
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                raise NodeError(
                    resp.status_code,
                    str(err.get("code") or "error"),
                    str(err.get("message") or resp.text[:300]),
                    body,
                )
            raise NodeError(
                resp.status_code,
                "error",
                (body.get("detail") if isinstance(body, dict) else None) or resp.text[:300],
                body if isinstance(body, dict) else None,
            )
        return resp.json()

    async def job(self, job_id: str) -> dict:
        return await self._call("GET", f"/node/jobs/{job_id}")

    async def job_content(self, job_id: str) -> bytes:
        http = await self._client()
        resp = await http.get(f"/node/jobs/{job_id}/content", timeout=300.0)
        if resp.status_code >= 400:
            raise NodeError(resp.status_code, "error", resp.text[:300])
        return resp.content


# ---- The remote "Gemini client": forwards generate_content to the slave -------
class RemoteModel:
    """Quacks like gemini-webapi's AvailableModel for the registry and pool."""

    def __init__(self, name: str) -> None:
        self.model_name = name
        self.name = name
        self.display_name = name
        self.description = ""
        self.is_available = True


class RemoteImage:
    """A generated image already in hand (base64 data URL from the slave)."""

    def __init__(self, b64: str, mime: str = "image/png") -> None:
        self.url = f"data:{mime};base64,{b64}"
        self.b64_json = b64


class RemoteVideo:
    """A video job on the slave; ``save`` downloads the mp4 onto the master."""

    def __init__(self, http: NodeClient, job_id: str) -> None:
        self.http = http
        self.job_id = job_id

    async def save(self, path: str, filename: str) -> dict:
        job = await self.http.job(self.job_id)
        if job.get("status") != "completed" or not job.get("has_file"):
            raise RuntimeError(f"remote video job {self.job_id} is {job.get('status')}")
        data = await self.http.job_content(self.job_id)
        out = Path(path)
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"{filename}.mp4"
        target.write_bytes(data)
        return {"video": str(target)}


class RemoteOutput:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.images: list[RemoteImage] = []
        self.videos: list[RemoteVideo] = []


class RemoteClient:
    """Stands in for a GeminiClient when the account lives on a linked node."""

    account_status = AccountStatus.AVAILABLE  # passes pool._client_authenticated
    abuse_status = None
    cookies: ClassVar[dict[str, str]] = {}
    usage_info: ClassVar[dict] = {}
    quotas: ClassVar[dict] = {}
    timeout: float = 0.0
    watchdog_timeout: float = 0.0

    def __init__(self, node: Node, account_id: str, models: list[RemoteModel]) -> None:
        self.node = node
        self.account_id = account_id
        self.http = NodeClient(node)
        self._models = models
        self.request_mode = "chat"  # set by the pool: chat | image | video

    def list_models(self) -> list[RemoteModel]:
        return self._models

    async def close(self) -> None:
        await self.http.close()

    async def generate_content(
        self, prompt: str, *, files: list | None = None, model: Any = None, **kwargs: Any
    ) -> RemoteOutput:
        images: list[str] = []
        for f in files or []:
            data = Path(f).read_bytes()
            mime = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            images.append(f"data:{mime};base64,{base64.b64encode(data).decode()}")
        model_name = getattr(model, "model_name", None) or (model if isinstance(model, str) else "")
        timeout = max(self.watchdog_timeout or 0.0, 0.0) or 900.0
        try:
            resp = await self.http.generate(
                account=self.account_id,
                model=model_name,
                prompt=prompt,
                images=images,
                mode=self.request_mode,
                timeout=timeout + 60,
            )
        except NodeError as exc:
            if exc.status == 429 or exc.code in {"quota", "limit", "unavailable"}:
                reset = (
                    exc.payload.get("error", {}).get("reset_at")
                    if isinstance(exc.payload.get("error"), dict)
                    else None
                )
                if reset:
                    self.quotas = {model_name or "remote": {"remaining": 0, "reset_time": reset}}
                raise QuotaExceededError(exc.args[0] if exc.args else "remote quota") from exc
            if exc.status in (401, 403) or exc.code == "auth":
                raise AuthError(exc.args[0] if exc.args else "remote session rejected") from exc
            raise RuntimeError(f"node {self.node.id}: {exc}") from exc
        out = RemoteOutput(text=resp.get("text") or "")
        out.images = [
            RemoteImage(i.get("b64") or "", i.get("mime") or "image/png")
            for i in resp.get("images") or []
        ]
        if resp.get("job"):
            out.videos = [RemoteVideo(self.http, resp["job"])]
        return out


# ---- Master-side manager: store, refresher task --------------------------------
class Nodes:
    def __init__(self) -> None:
        self.store: NodeStore | None = None
        self._clients: dict[str, NodeClient] = {}
        self._task: asyncio.Task | None = None

    def configure(self, settings: Settings) -> NodeStore:
        path = settings.data_dir / NODES_FILE
        if self.store is None or self.store.path != path:
            self.store = NodeStore(path)
        return self.store

    def client_for(self, node: Node) -> NodeClient:
        client = self._clients.get(node.id)
        if client is None or client.node.url != node.url or client.node.token != node.token:
            if client is not None:
                asyncio.get_running_loop().create_task(client.close())
            client = NodeClient(node)
            self._clients[node.id] = client
        return client

    async def refresh_node(self, node: Node) -> None:
        """Re-read one slave's accounts; update slots, registry and events."""
        client = self.client_for(node)
        try:
            await client.ping()
            accounts = await client.accounts()
        except Exception as exc:
            node.online = False
            node.error = str(exc)[:300]
            node.accounts = []
            log.warning("node %s (%s) unreachable: %s", node.id, node.url, node.error)
            if node.enabled:
                events.emit("node_down", node.id, f"{node.url}: {node.error}")
            self._sync_pool()
            return
        was_online = node.online
        node.online = True
        node.last_ok = time.time()
        node.error = None
        node.accounts = accounts
        if node.enabled and not was_online:
            events.emit("node_up", node.id, node.url)
        log.info("node %s: %d account(s)", node.id, len(accounts))
        self._sync_pool()

    async def refresh_all(self) -> None:
        if self.store is None:
            return
        for node in self.store.all():
            if node.enabled:
                await self.refresh_node(node)

    def _sync_pool(self) -> None:
        from gemini_webapi_proxy.client.pool import pool, registry
        from gemini_webapi_proxy.client.registry import ModelEntry, _guess_kind
        from gemini_webapi_proxy.config import get_settings

        pool.configure(get_settings())
        for node in self.store.all() if self.store else []:
            for acc in node.accounts:
                for name in acc.get("models") or []:
                    if name and name not in registry.entry_ids():
                        registry._entries[name] = ModelEntry(
                            id=name,
                            display_name=name,
                            description="",
                            kind=_guess_kind(name, ""),
                            chat_ok=_guess_kind(name, "") in {"chat", "both"},
                            image_ok=_guess_kind(name, "") == "image",
                        )
        registry._rebuild_aliases()

    async def _loop(self, settings: Settings) -> None:
        while True:
            try:
                await self.refresh_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("nodes refresh: %s", exc)
            await asyncio.sleep(max(5.0, settings.node_refresh_interval))

    async def start(self, settings: Settings) -> None:
        await self.stop()
        self.configure(settings)
        await self.refresh_all()
        self._task = asyncio.get_running_loop().create_task(self._loop(settings))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()


nodes = Nodes()
