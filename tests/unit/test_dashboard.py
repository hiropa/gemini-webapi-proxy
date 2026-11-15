"""Request journal, client keys, playground pinning and the gallery."""

from __future__ import annotations

import base64
import json
import time
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from gemini_webapi.constants import AccountStatus

from gemini_webapi_proxy import stats
from gemini_webapi_proxy.client import pool as pool_module
from gemini_webapi_proxy.client.accounts import AccountStore
from gemini_webapi_proxy.client.base import ModelDescriptor
from gemini_webapi_proxy.config import get_settings, reset_settings_cache
from gemini_webapi_proxy.keys import KeyStore


def test_summary_buckets_and_breakdowns():
    now = 1_800_001_800.0  # mid-hour
    records = [
        {
            "ts": now - 10,
            "endpoint": "chat",
            "model": "gemini-flash",
            "account": "a",
            "key": "bot",
            "status": 200,
            "ms": 1000,
        },
        {
            "ts": now - 20,
            "endpoint": "chat",
            "model": "gemini-flash",
            "account": "b",
            "key": "bot",
            "status": 200,
            "ms": 3000,
        },
        {
            "ts": now - 3600 * 2,
            "endpoint": "image",
            "model": "gemini-pro",
            "account": "a",
            "key": "master",
            "status": 429,
        },
    ]
    s = stats.summary(records, hours=24, now=now)
    assert s["totals"] == {
        "requests": 3,
        "ok": 2,
        "errors": 1,
        "limited": 1,
        "p50_ms": 2000.0,
        "p95_ms": 2900.0,
    }
    assert len(s["hourly"]) == 24 and s["hourly"][-1] == {
        "ts": int(now // 3600) * 3600,
        "ok": 2,
        "error": 0,
    }
    assert s["hourly"][-3]["error"] == 1
    assert s["models"][0] == {"name": "gemini-flash", "requests": 2, "errors": 0}
    assert {"name": "a", "requests": 2, "errors": 1} in s["accounts"]


def test_journal_persists_and_filters(tmp_path):
    log = stats.RequestLog()
    log.open(tmp_path)
    log.add({"ts": time.time(), "endpoint": "chat", "status": 200, "account": "a"})
    log.add({"ts": time.time(), "endpoint": "chat", "status": 502, "account": "b"})
    again = stats.RequestLog()
    again.open(tmp_path)
    assert len(again.records) == 2
    assert [r["account"] for r in again.recent(10, outcome="error")] == ["b"]
    assert [r["account"] for r in again.recent(10, account="a")] == ["a"]


def test_journal_compacts(tmp_path, monkeypatch):
    monkeypatch.setattr(stats, "MAX_RECORDS", 3)
    log = stats.RequestLog()
    log.records = stats.deque(maxlen=3)
    log.open(tmp_path)
    log.records = stats.deque(maxlen=3)
    for i in range(10):
        log.add({"ts": i, "status": 200})
    lines = (tmp_path / stats.REQUESTS_FILE).read_text().splitlines()
    assert len(lines) <= 7 and json.loads(lines[-1])["ts"] == 9


def test_key_store_keeps_only_a_hash(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    key, secret = store.create("openclaw")
    assert secret.startswith("gop-") and key.prefix == secret[:10]
    assert secret not in (tmp_path / "keys.json").read_text()
    assert KeyStore(tmp_path / "keys.json").match(secret).name == "openclaw"
    store.update(key.id, enabled=False)
    assert store.match(secret) is None


class FakeClient:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.account_status = AccountStatus.AVAILABLE
        self.quotas: dict = {}
        self.cookies: dict = {}
        self.timeout = self.watchdog_timeout = 120

    async def close(self) -> None:
        pass

    def list_models(self):
        return [ModelDescriptor(name="gemini-flash", display_name="Flash", description="")]

    prompts: ClassVar[list] = []

    async def generate_content(self, prompt, files=None, model=None, **kw):
        from types import SimpleNamespace

        FakeClient.prompts.append(prompt)

        return SimpleNamespace(text=f"answer from {self.account_id}", images=[])


@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setenv("GOP_API_KEY", "master-key")
    reset_settings_cache()
    store = AccountStore(get_settings().data_dir / "accounts.json")
    store.remove("default")
    for name in ("a", "b"):
        store.add(name)
        store.set_cookies(name, {"__Secure-1PSID": name}, None)

    async def fake_new_client(settings, account):
        return FakeClient(account.id)

    monkeypatch.setattr(pool_module, "_new_client", fake_new_client)
    monkeypatch.setattr(pool_module, "pool", pool_module.AccountPool())
    from gemini_webapi_proxy.routes import accounts, dashboard
    from gemini_webapi_proxy.services import chat as chat_service

    monkeypatch.setattr(accounts, "pool", pool_module.pool)
    monkeypatch.setattr(dashboard, "pool", pool_module.pool)
    monkeypatch.setattr(chat_service, "pool", pool_module.pool)
    monkeypatch.setattr(stats, "journal", stats.RequestLog())
    from gemini_webapi_proxy import app as app_module

    monkeypatch.setattr(app_module, "journal", stats.journal)
    monkeypatch.setattr(dashboard, "journal", stats.journal)
    monkeypatch.setattr(pool_module, "journal", stats.journal)
    from gemini_webapi_proxy.services import video as video_service

    monkeypatch.setattr(video_service, "journal", stats.journal)
    monkeypatch.setattr(video_service, "pool", pool_module.pool)
    with TestClient(app_module.app) as c:
        yield c


AUTH = {"Authorization": "Bearer master-key"}


def _chat(c, headers):
    return c.post(
        "/openai/v1/chat/completions",
        headers=headers,
        json={"model": "gemini-flash", "messages": [{"role": "user", "content": "hi"}]},
    )


def test_chat_lands_in_the_journal_with_key_and_account(app_client):
    assert _chat(app_client, AUTH).status_code == 200
    created = app_client.post("/admin/keys", headers=AUTH, json={"name": "bot"}).json()
    assert _chat(app_client, {"Authorization": f"Bearer {created['secret']}"}).status_code == 200
    assert _chat(app_client, {"Authorization": "Bearer nope"}).status_code == 403

    rows = app_client.get("/admin/requests", headers=AUTH).json()["requests"]
    ok = [r for r in rows if r["status"] == 200]
    assert [r["key"] for r in ok] == ["bot", "master"]
    assert ok[0]["account"] == "a" and ok[0]["model"] == "gemini-flash"
    assert ok[0]["prompt_chars"] > 0 and ok[0]["answer_chars"] == len("answer from a")
    st = app_client.get("/admin/stats", headers=AUTH).json()
    assert st["totals"]["requests"] == 3 and st["totals"]["errors"] == 1
    keys = app_client.get("/admin/keys", headers=AUTH).json()
    assert keys["master"]["requests"] == 1 and keys["keys"][0]["requests"] == 1
    assert "secret" not in keys["keys"][0] and "sha256" not in keys["keys"][0]


def test_playground_pins_the_account(app_client):
    r = app_client.post(
        "/admin/playground", headers=AUTH, json={"prompt": "hi", "account": "b"}
    ).json()
    assert r["account"] == "b" and r["text"] == "answer from b"
    r = app_client.post("/admin/playground", headers=AUTH, json={"prompt": "hi"}).json()
    assert r["account"] == "a"
    rows = app_client.get("/admin/requests?endpoint=playground-chat", headers=AUTH).json()[
        "requests"
    ]
    assert [x["account"] for x in rows] == ["a", "b"]


def test_models_and_system(app_client):
    app_client.post("/admin/accounts/a/check", headers=AUTH)
    models = app_client.get("/admin/models", headers=AUTH).json()
    flash = next(m for m in models["models"] if m["id"] == "gemini-flash")
    assert flash["accounts"]["a"] is True
    system = app_client.get("/admin/system", headers=AUTH).json()
    assert system["settings"]["api_key"] is True and "admin_password" in system["settings"]
    assert system["browser"]["ok"] is False  # no browser in tests


def test_gallery_keeps_images(app_client, tmp_path):
    from gemini_webapi_proxy.services import gallery

    settings = get_settings()
    png = base64.b64encode(b"\x89PNG fake").decode()
    gallery.save_to_gallery(settings, [png], prompt="a cat")
    items = app_client.get("/admin/gallery", headers=AUTH).json()["images"]
    assert len(items) == 1 and items[0]["prompt"] == "a cat" and items[0]["name"].endswith(".png")
    assert (
        app_client.get(f"/admin/gallery/{items[0]['name']}", headers=AUTH).content
        == b"\x89PNG fake"
    )
    assert app_client.get("/admin/gallery/..%2Faccounts.json", headers=AUTH).status_code == 404
    assert app_client.delete(f"/admin/gallery/{items[0]['name']}", headers=AUTH).status_code == 200
    assert app_client.get("/admin/gallery", headers=AUTH).json()["images"] == []


def test_gallery_tags_node_and_skips_copies_when_forwarding(app_client):
    from gemini_webapi_proxy.services import gallery

    settings = get_settings()
    png = base64.b64encode(b"\x89PNG fake").decode()
    token = stats.current.set(
        {"ts": time.time(), "account": "box1::acc-a", "model": "gemini-flash"}
    )
    try:
        names = gallery.save_to_gallery(settings, [png], prompt="remote cat")
    finally:
        stats.current.reset(token)
    assert len(names) == 1
    items = app_client.get("/admin/gallery", headers=AUTH).json()["images"]
    assert items[0]["node"] == "box1"  # node id as the label when not linked
    with gallery.forwarding():
        assert gallery.save_to_gallery(settings, [png], prompt="no local copy") == []
    assert len(app_client.get("/admin/gallery", headers=AUTH).json()["images"]) == 1


def test_playground_video_job_runs_on_the_pinned_account(app_client):
    r = app_client.post(
        "/admin/playground", headers=AUTH, json={"prompt": "a cat", "mode": "video", "account": "b"}
    ).json()
    job_id = r["video"]["id"]
    for _ in range(50):
        job = next(
            j
            for j in app_client.get("/admin/videos", headers=AUTH).json()["videos"]
            if j["id"] == job_id
        )
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)
    # The fake account answers with text and no video: a refusal, on the account asked for.
    assert job["status"] == "failed" and job["account"] == "b" and job["key"] == "admin-ui"
    rows = app_client.get("/admin/requests?endpoint=video-job", headers=AUTH).json()["requests"]
    assert rows[0]["account"] == "b" and rows[0]["video"] == job_id


def test_playground_sends_the_chat_history(app_client):
    FakeClient.prompts.clear()
    r = app_client.post(
        "/admin/playground",
        headers=AUTH,
        json={
            "prompt": "and in Danish?",
            "history": [
                {"role": "user", "content": "say hi"},
                {"role": "assistant", "content": "hi"},
            ],
        },
    ).json()
    assert r["text"].startswith("answer from") and r["gallery"] == []
    prompt = FakeClient.prompts[-1]
    assert (
        prompt.index("USER: say hi")
        < prompt.index("ASSISTANT: hi")
        < prompt.index("USER: and in Danish?")
    )
