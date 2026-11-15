"""Linked nodes: the slave ``/node/*`` API and remote accounts in the master's pool."""

from __future__ import annotations

import re
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from gemini_webapi.constants import AccountStatus
from gemini_webapi.exceptions import UsageLimitExceededError

from gemini_webapi_proxy.client import nodes as nodes_module
from gemini_webapi_proxy.client import pool as pool_module
from gemini_webapi_proxy.client.accounts import AccountStore
from gemini_webapi_proxy.client.base import ModelDescriptor
from gemini_webapi_proxy.client.nodes import NodeStore, RemoteClient
from gemini_webapi_proxy.client.nodes import nodes as nodes_manager
from gemini_webapi_proxy.client.pool import RemoteSlot
from gemini_webapi_proxy.config import get_settings, reset_settings_cache
from gemini_webapi_proxy.errors import QuotaExceededError


def _model(name: str) -> ModelDescriptor:
    return ModelDescriptor(name=name, display_name=name, description="")


class FakeClient:
    """Stands in for a GeminiClient on the slave, like in test_pool."""

    def __init__(self, account_id: str, fail: str | None = None) -> None:
        self.account_id = account_id
        self.account_status = AccountStatus.AVAILABLE
        self.quotas: dict = {}
        self.fail = fail
        self.models = [_model("gemini-flash")]

    async def close(self) -> None:
        pass

    def list_models(self) -> list:
        return self.models

    async def generate_content(self, prompt, *, files=None, model=None, **kwargs):
        if self.fail == "quota":
            raise UsageLimitExceededError("Usage limit exceeded")
        return SimpleNamespace(text=f"answer on {self.account_id}", images=[], videos=[])


@pytest.fixture
def slave(monkeypatch):
    """A slave instance: node token set, one account, fake Gemini client."""
    monkeypatch.setenv("GOP_NODE_TOKEN", "node-secret")
    reset_settings_cache()
    settings = get_settings()
    store = AccountStore(settings.data_dir / "accounts.json")
    store.remove("default")
    store.add("free")
    store.set_cookies("free", {"__Secure-1PSID": "sid"}, None)
    state = {"fail": None}

    async def fake_new_client(settings, account):
        return FakeClient(account.id, fail=state["fail"])

    monkeypatch.setattr(pool_module, "_new_client", fake_new_client)
    monkeypatch.setattr(pool_module, "pool", pool_module.AccountPool())
    monkeypatch.setattr(pool_module, "registry", pool_module.ModelRegistry())
    from gemini_webapi_proxy.app import app

    client = TestClient(app)  # no `with`: lifespan (and its node polling) not started
    return client, state


NODE = {"Authorization": "Bearer node-secret"}


def test_node_api_requires_the_token(slave):
    client, _ = slave  # GOP_NODE_TOKEN is set in this fixture
    assert client.get("/node/ping").status_code == 401
    assert client.get("/node/ping", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.get("/node/ping", headers=NODE)
    assert r.status_code == 200 and r.json()["ok"]


def test_node_api_disabled_without_token(monkeypatch):
    monkeypatch.delenv("GOP_NODE_TOKEN", raising=False)
    reset_settings_cache()
    from gemini_webapi_proxy.app import app

    client = TestClient(app)
    r = client.get("/node/ping", headers=NODE)
    assert r.status_code == 404 and "disabled" in r.json()["detail"]


def test_node_accounts_lists_only_local_accounts(slave):
    client, _ = slave
    r = client.get("/node/accounts", headers=NODE)
    assert r.status_code == 200
    ids = [a["id"] for a in r.json()["accounts"]]
    assert ids == ["free"]


def test_node_generate_chat(slave):
    client, _ = slave
    r = client.post(
        "/node/generate",
        headers=NODE,
        json={"account": "free", "model": "gemini-flash", "prompt": "hi", "mode": "chat"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "answer on free"


def test_node_generate_unknown_account(slave):
    client, _ = slave
    r = client.post("/node/generate", headers=NODE, json={"account": "ghost", "prompt": "hi"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "no_account"


def test_node_generate_quota_maps_to_429(slave):
    client, state = slave
    state["fail"] = "quota"
    r = client.post("/node/generate", headers=NODE, json={"account": "free", "prompt": "hi"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "quota"


# ---- Master side ---------------------------------------------------------------
class FakeNodeTransport:
    """Emulates a slave's /node/* API for the master's NodeClient."""

    def __init__(self) -> None:
        self.mode = "ok"  # ok | quota | down
        self.calls: list[tuple[str, dict]] = []
        self.node_accounts: dict[str, dict] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = {}
        if request.content:
            import json

            body = json.loads(request.content)
        self.calls.append((path, body))
        if self.mode == "down":
            raise httpx.ConnectError("refused")
        if path == "/node/ping":
            return httpx.Response(200, json={"ok": True, "version": "0.1.0"})
        if path == "/node/accounts" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {
                            "id": "free",
                            "name": "free",
                            "enabled": True,
                            "priority": 0,
                            "state": "ok",
                            "models": ["gemini-flash"],
                            "quota": None,
                            "error": None,
                        }
                    ]
                },
            )
        if path == "/node/accounts" and request.method == "POST":
            name = body.get("name") or "acc"
            self.node_accounts[name] = {
                "id": name,
                "name": name,
                "enabled": True,
                "priority": 0,
                "state": "idle",
                "models": [],
                "quota": None,
                "error": None,
            }
            return httpx.Response(200, json=self.node_accounts[name])
        m = re.match(
            r"^/node/accounts/([^/]+)(/cookies|/check|/cooldowns|/login/start|/login/finish|/login/cancel)?$",
            path,
        )
        if m:
            acc = self.node_accounts.setdefault(
                m.group(1),
                {
                    "id": m.group(1),
                    "name": m.group(1),
                    "enabled": True,
                    "priority": 0,
                    "state": "ok",
                    "models": ["gemini-flash"],
                    "quota": None,
                    "error": None,
                },
            )
            tail = m.group(2) or ""
            if request.method == "PATCH":
                acc.update({k: v for k, v in body.items() if k in ("name", "enabled", "priority")})
            elif request.method == "DELETE" and not tail:
                self.node_accounts.pop(m.group(1), None)
            elif tail in {"/cookies", "/check"}:
                acc["state"] = "ok"
            return httpx.Response(
                200, json=acc if m.group(1) in self.node_accounts else {"ok": True}
            )
        if path == "/node/generate":
            if self.mode == "quota":
                return httpx.Response(
                    429,
                    json={
                        "error": {
                            "code": "quota",
                            "message": "limit",
                            "reset_at": time.time() + 600,
                        }
                    },
                )
            return httpx.Response(200, json={"text": "from-slave", "images": []})
        return httpx.Response(404, json={"detail": "nope"})


@pytest.fixture
def master(monkeypatch):
    """A master with one local account and one linked slave (mock transport)."""
    settings = get_settings()
    store = AccountStore(settings.data_dir / "accounts.json")
    store.remove("default")
    store.add("local")
    store.set_cookies("local", {"__Secure-1PSID": "sid-local"}, None)

    async def fake_new_client(settings, account):
        return FakeClient(account.id)

    monkeypatch.setattr(pool_module, "_new_client", fake_new_client)
    monkeypatch.setattr(pool_module, "pool", pool_module.AccountPool())
    monkeypatch.setattr(pool_module, "registry", pool_module.ModelRegistry())

    transport = FakeNodeTransport()
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(nodes_module.httpx, "AsyncClient", client_factory)
    nodes_manager._clients.clear()  # the singleton's client cache must not leak between tests
    monkeypatch.setattr(nodes_manager, "store", NodeStore(settings.data_dir / "nodes.json"))
    node = nodes_manager.store.add("slave", "http://slave.test", "tok")
    return settings, node, transport


async def test_remote_account_joins_the_pool(master):
    settings, node, _transport = master
    await nodes_manager.refresh_node(node)
    assert node.online and node.accounts[0]["id"] == "free"
    pool_module.pool.configure(settings)
    slot = pool_module.pool.slots["slave::free"]
    assert isinstance(slot, RemoteSlot) and slot.is_remote
    rep = slot.report()
    assert rep["remote"]["node"] == "slave" and rep["state"] == "ok"
    assert rep["models"] == ["gemini-flash"]
    # the remote model is published to clients too
    assert "gemini-flash" in pool_module.registry.entry_ids()


async def test_pool_fails_over_from_local_to_remote(master):
    settings, node, transport = master
    await nodes_manager.refresh_node(node)
    pool_module.pool.configure(settings)

    async def call(client, runtime):
        if isinstance(client, RemoteClient):
            out = await client.generate_content("hi", files=None, model=runtime)
            return out.text
        raise QuotaExceededError("local is out")

    result = await pool_module.pool.run(settings, "gemini-flash", call)
    assert result == "from-slave"
    assert transport.calls[-1][0] == "/node/generate"
    assert transport.calls[-1][1]["account"] == "free"


async def test_pool_fails_over_from_remote_quota_to_local(master):
    settings, node, transport = master
    await nodes_manager.refresh_node(node)
    pool_module.pool.configure(settings)
    transport.mode = "quota"

    async def call(client, runtime):
        if isinstance(client, RemoteClient):
            await client.generate_content("hi", files=None, model=runtime)
            raise AssertionError("should have raised")
        return "local answer"

    # remote slot has higher priority value? node priority 0 -> 0; "local" < "slave::free" so local first
    result = await pool_module.pool.run(settings, "gemini-flash", call)
    assert result == "local answer"


async def test_remote_quota_cools_the_remote_slot_down(master):
    settings, node, transport = master
    await nodes_manager.refresh_node(node)
    pool_module.pool.configure(settings)
    # force the remote slot first: give the local account a cooldown
    pool_module.pool.slots["local"].cooldowns["gemini-flash"] = time.time() + 600
    transport.mode = "quota"

    async def call(client, runtime):
        await client.generate_content("hi", files=None, model=runtime)
        return "?"

    with pytest.raises(QuotaExceededError):
        await pool_module.pool.run(settings, "gemini-flash", call)
    slot = pool_module.pool.slots["slave::free"]
    assert slot.cooling("gemini-flash")
    # and it recovers after the reset time passes
    slot.cooldowns["gemini-flash"] = time.time() - 1
    assert not slot.cooling("gemini-flash")


async def test_node_down_marks_slots_and_skips_them(master):
    settings, node, transport = master
    await nodes_manager.refresh_node(node)
    pool_module.pool.configure(settings)
    transport.mode = "down"
    await nodes_manager.refresh_node(node)
    assert not node.online and node.error
    pool_module.pool.configure(settings)
    # a down node's accounts leave the pool; the Nodes tab shows the error
    assert "slave::free" not in pool_module.pool.slots

    async def call(client, runtime):
        return "local answer" if not isinstance(client, RemoteClient) else "?"

    assert await pool_module.pool.run(settings, "gemini-flash", call) == "local answer"
    # and they come back when the node does
    transport.mode = "ok"
    await nodes_manager.refresh_node(node)
    pool_module.pool.configure(settings)
    assert isinstance(pool_module.pool.slots["slave::free"], RemoteSlot)


async def test_admin_nodes_api(monkeypatch):
    from gemini_webapi_proxy.app import app

    settings = get_settings()
    transport = FakeNodeTransport()
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(nodes_module.httpx, "AsyncClient", client_factory)
    nodes_manager._clients.clear()
    monkeypatch.setattr(nodes_manager, "store", NodeStore(settings.data_dir / "nodes.json"))
    monkeypatch.setattr(pool_module, "pool", pool_module.AccountPool())
    client = TestClient(app)
    auth = {"Authorization": "Bearer x"}
    monkeypatch.setenv("GOP_API_KEY", "x")
    reset_settings_cache()

    assert client.get("/admin/nodes", headers=auth).json() == {"nodes": []}
    # missing token
    r = client.post(
        "/admin/nodes", headers=auth, json={"name": "s", "url": "http://s.test", "token": ""}
    )
    assert r.status_code == 400
    # bad scheme
    r = client.post(
        "/admin/nodes", headers=auth, json={"name": "s", "url": "ftp://x", "token": "t"}
    )
    assert r.status_code == 400
    # linked ok
    r = client.post(
        "/admin/nodes",
        headers=auth,
        json={"name": "slave", "url": "http://slave.test", "token": "t"},
    )
    assert r.status_code == 200 and r.json()["online"] and r.json()["accounts"][0]["id"] == "free"
    assert r.json()["token"].endswith("…") or "***" in r.json()["token"]  # masked
    # unreachable -> 502 and not stored
    transport.mode = "down"
    r = client.post(
        "/admin/nodes", headers=auth, json={"name": "bad", "url": "http://bad.test", "token": "t"}
    )
    assert r.status_code == 502
    nodes = client.get("/admin/nodes", headers=auth).json()["nodes"]
    assert [n["name"] for n in nodes] == ["slave"]
    # remote account mutation is refused
    pool_module.pool.configure(get_settings())
    r = client.patch("/admin/accounts/slave::free", headers=auth, json={"enabled": False})
    assert r.status_code == 409
    # unlink
    nid = nodes[0]["id"]
    assert client.delete(f"/admin/nodes/{nid}", headers=auth).status_code == 200
    assert client.get("/admin/nodes", headers=auth).json() == {"nodes": []}


def test_node_token_endpoints(monkeypatch, tmp_path):
    monkeypatch.delenv("GOP_NODE_TOKEN", raising=False)
    reset_settings_cache()
    from gemini_webapi_proxy.app import app

    client = TestClient(app)
    r = client.get("/admin/node-token")
    assert r.status_code == 200 and r.json()["token_set"] is False
    r = client.post("/admin/node-token")
    token = r.json()["token"]
    assert token.startswith("gop-node-")
    r = client.get("/admin/node-token")
    assert r.json()["token_set"] is True and r.json()["token"] != token  # masked
    # persisted with 0600
    import stat

    path = get_settings().data_dir / "node_token.txt"
    assert path.read_text().strip() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # env var wins over the file
    monkeypatch.setenv("GOP_NODE_TOKEN", "env-token")
    reset_settings_cache()
    from gemini_webapi_proxy.client.nodes import effective_node_token

    assert effective_node_token(get_settings()) == "env-token"


# ---- Modes -----------------------------------------------------------------------
def test_mode_setting(monkeypatch):
    from gemini_webapi_proxy.config import Settings

    monkeypatch.delenv("GOP_MODE", raising=False)
    assert Settings().mode == "master" and not Settings().is_slave
    monkeypatch.setenv("GOP_MODE", "slave")
    assert Settings().mode == "slave" and Settings().is_slave


def test_slave_egress_is_always_direct(monkeypatch):
    """Slave mode: proxies are ignored so Google traffic leaves via the slave IP."""
    from gemini_webapi_proxy.client.accounts import Account
    from gemini_webapi_proxy.client.pool import outbound_proxy
    from gemini_webapi_proxy.config import Settings

    account = Account(id="a", name="a", proxy="socks5://127.0.0.1:1080")
    monkeypatch.setenv("GOP_HTTP_PROXY", "http://127.0.0.1:8118")
    monkeypatch.setenv("GOP_MODE", "slave")
    assert outbound_proxy(Settings(), account) is None
    monkeypatch.setenv("GOP_MODE", "master")
    assert outbound_proxy(Settings(), account) == "socks5://127.0.0.1:1080"


@pytest.fixture
def slave_mode(monkeypatch):
    """A slave-mode instance: node token set, one account, fake Gemini client."""
    monkeypatch.setenv("GOP_NODE_TOKEN", "node-secret")
    monkeypatch.setenv("GOP_MODE", "slave")
    monkeypatch.setenv("GOP_ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("GOP_API_KEY", "pw")
    reset_settings_cache()
    settings = get_settings()
    store = AccountStore(settings.data_dir / "accounts.json")
    store.remove("default")
    store.add("free")
    store.set_cookies("free", {"__Secure-1PSID": "sid"}, None)

    async def fake_new_client(settings, account):
        return FakeClient(account.id)

    monkeypatch.setattr(pool_module, "_new_client", fake_new_client)
    monkeypatch.setattr(pool_module, "registry", pool_module.ModelRegistry())
    fresh = pool_module.AccountPool()
    monkeypatch.setattr(pool_module, "pool", fresh)
    from gemini_webapi_proxy.routes import accounts as accounts_route
    from gemini_webapi_proxy.routes import nodes as nodes_route
    from gemini_webapi_proxy.services import account_ops

    for mod in (accounts_route, nodes_route, account_ops):
        monkeypatch.setattr(mod, "pool", fresh)
    from gemini_webapi_proxy.app import app

    client = TestClient(app)
    return client, settings


def test_slave_mode_blocks_local_account_management(slave_mode):
    """A slave cannot add or change accounts itself; the master does over the link."""
    client, _settings = slave_mode
    admin = {"Authorization": "Bearer pw"}
    assert client.get("/admin/accounts", headers=admin).json()["mode"] == "slave"
    r = client.post("/admin/accounts", headers=admin, json={"name": "new"})
    assert r.status_code == 403 and "master" in r.json()["detail"]
    assert (
        client.patch("/admin/accounts/free", headers=admin, json={"enabled": False}).status_code
        == 403
    )
    assert client.delete("/admin/accounts/free", headers=admin).status_code == 403
    assert (
        client.post(
            "/admin/accounts/free/cookies", headers=admin, json={"cookies": "x=y"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/admin/accounts/free/proxy", headers=admin, json={"proxy": "http://x"}
        ).status_code
        == 403
    )
    # the account list is still readable (status only)
    assert client.get("/admin/accounts", headers=admin).status_code == 200


def test_slave_mode_serves_no_clients(slave_mode):
    client, _ = slave_mode
    assert client.get("/openai/v1/models").status_code == 403


def test_node_account_management_over_the_link(slave_mode):
    """The master drives a slave's accounts through /node/accounts*."""
    client, _ = slave_mode
    r = client.post("/node/accounts", headers=NODE, json={"name": "pro"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "pro"
    r = client.patch("/node/accounts/pro", headers=NODE, json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    client.patch("/node/accounts/pro", headers=NODE, json={"enabled": True})
    r = client.post(
        "/node/accounts/pro/cookies", headers=NODE, json={"cookies": "__Secure-1PSID=sid2; other=1"}
    )
    assert r.status_code == 200
    r = client.post("/node/accounts/pro/check", headers=NODE)
    assert r.status_code == 200 and r.json()["state"] == "ok"
    ids = [a["id"] for a in client.get("/node/accounts", headers=NODE).json()["accounts"]]
    assert "pro" in ids
    assert client.delete("/node/accounts/pro", headers=NODE).status_code == 200
    ids = [a["id"] for a in client.get("/node/accounts", headers=NODE).json()["accounts"]]
    assert "pro" not in ids
    # without the token nothing is manageable
    assert client.post("/node/accounts", json={"name": "x"}).status_code == 401


async def test_master_manages_slave_accounts(master):
    """The master forwards account changes to the slave and re-reads its list."""
    settings, node, transport = master
    await nodes_manager.refresh_node(node)
    pool_module.pool.configure(settings)
    from gemini_webapi_proxy.app import app

    client = TestClient(app)
    r = client.post("/admin/nodes/slave/accounts", json={"name": "pro"})
    assert r.status_code == 200, r.text
    assert transport.calls[-1][0] == "/node/accounts"
    r = client.patch("/admin/nodes/slave/accounts/pro", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = client.post("/admin/nodes/slave/accounts/pro/cookies", json={"cookies": "__Secure-1PSID=s"})
    assert r.status_code == 200
    assert client.post("/admin/nodes/slave/accounts/pro/check").status_code == 200
    assert client.delete("/admin/nodes/slave/accounts/pro/cooldowns").status_code == 200
    assert client.delete("/admin/nodes/slave/accounts/pro").status_code == 200
    # the slave's list after the refresh reflects the deletion
    assert "pro" not in [a["id"] for a in node.accounts]


# ---- Media: stored on the master only --------------------------------------------
def test_media_retention_defaults(monkeypatch):
    """Images and videos are kept for 90 days by default (was 24 h)."""
    monkeypatch.delenv("GOP_IMAGE_KEEP_HOURS", raising=False)
    monkeypatch.delenv("GOP_VIDEO_KEEP_HOURS", raising=False)
    from gemini_webapi_proxy.config import Settings

    s = Settings()
    assert s.image_keep_hours == 90 * 24 and s.video_keep_hours == 90 * 24


def _b64_png() -> str:
    import base64

    return base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def test_slave_gallery_keeps_no_copies(monkeypatch, tmp_path):
    """Slave mode: images are stored on the master only, never here."""
    from gemini_webapi_proxy.services import gallery
    from gemini_webapi_proxy.stats import current

    monkeypatch.setenv("GOP_MODE", "slave")
    monkeypatch.setenv("GOP_DATA_DIR", str(tmp_path))
    reset_settings_cache()
    settings = get_settings()
    token = current.set({"model": "gemini-flash", "account": "slave::free", "node": "slave"})
    try:
        assert gallery.save_to_gallery(settings, [_b64_png()], prompt="hi") == []
        assert not gallery.gallery_dir(settings).exists()
    finally:
        current.reset(token)

    monkeypatch.setenv("GOP_MODE", "master")
    reset_settings_cache()
    settings = get_settings()
    token = current.set({"model": "gemini-flash", "account": "slave::free", "node": "slave"})
    try:
        saved = gallery.save_to_gallery(settings, [_b64_png()], prompt="hi")
        assert saved, "master keeps the gallery copy"
        entry = gallery.entries(settings)[0]
        assert entry["node"] == "slave"  # attributed to the slave for its Media tab
    finally:
        current.reset(token)


def test_node_job_delete_removes_the_slaves_copy(slave):
    """The master fetched the mp4: DELETE /node/jobs/{id} drops file and job."""
    from gemini_webapi_proxy.services import video

    client, _ = slave
    settings = get_settings()
    job = video.VideoJob(id="video_x", model="gemini-flash", prompt="p")
    job.status = "completed"
    job.path = settings.data_dir / "videos" / "video_x.mp4"
    job.path.parent.mkdir(parents=True, exist_ok=True)
    job.path.write_bytes(b"mp4")
    video._jobs[job.id] = job
    assert client.delete("/node/jobs/video_x").status_code == 401  # token required
    r = client.delete("/node/jobs/video_x", headers=NODE)
    assert r.status_code == 200 and r.json()["ok"]
    assert video.get_job("video_x") is None and not job.path.exists()
    assert client.delete("/node/jobs/video_x", headers=NODE).status_code == 404


async def test_remote_video_handoff_deletes_the_slave_copy(master, tmp_path):
    """RemoteVideo.save fetches the mp4 and tells the slave to drop its copy."""
    from gemini_webapi_proxy.client.nodes import Node, RemoteVideo

    class FakeNodeClient:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        @property
        def node(self) -> Node:
            return Node(id="slave", name="slave", url="http://x", token="t")

        async def job(self, job_id: str) -> dict:
            return {"status": "completed", "has_file": True}

        async def job_content(self, job_id: str) -> bytes:
            return b"mp4-bytes"

        async def job_delete(self, job_id: str) -> dict:
            self.deleted.append(job_id)
            return {"ok": True}

    http = FakeNodeClient()
    await RemoteVideo(http, "video_x").save(path=str(tmp_path / "videos"), filename="job1")
    assert (tmp_path / "videos" / "job1.mp4").read_bytes() == b"mp4-bytes"
    assert http.deleted == ["video_x"]  # the master holds the only copy now
