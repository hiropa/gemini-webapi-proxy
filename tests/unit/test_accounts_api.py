"""Admin UI API: session sign-in, accounts, sign-in through the server's browser."""

from __future__ import annotations

import stat

import pytest
from fastapi.testclient import TestClient
from gemini_webapi.constants import AccountStatus

from gemini_webapi_proxy.client import pool as pool_module
from gemini_webapi_proxy.client.accounts import AccountStore
from gemini_webapi_proxy.config import get_settings
from gemini_webapi_proxy.services import account_ops


class FakeClient:
    def __init__(self, psid: str) -> None:
        self.psid = psid
        self.account_status = AccountStatus.AVAILABLE
        self.quotas: dict = {}

    async def close(self) -> None:
        pass

    def list_models(self) -> list:
        return []


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GOP_API_KEY", "key")
    monkeypatch.setenv("GOP_ADMIN_PASSWORD", "secret")
    from gemini_webapi_proxy.config import reset_settings_cache

    reset_settings_cache()

    async def fake_new_client(settings, account):
        if account.source == "settings":
            raise RuntimeError("env cookies not set")
        return FakeClient(account.cookies["__Secure-1PSID"])

    monkeypatch.setattr(pool_module, "_new_client", fake_new_client)
    monkeypatch.setattr(pool_module, "pool", pool_module.AccountPool())
    monkeypatch.setattr(pool_module, "signing_in", None)
    from gemini_webapi_proxy.routes import accounts

    monkeypatch.setattr(accounts, "pool", pool_module.pool)
    monkeypatch.setattr(account_ops, "pool", pool_module.pool)
    from gemini_webapi_proxy.app import app

    with TestClient(app, base_url="https://testserver") as c:
        yield c


def test_needs_a_session_or_the_api_key(client: TestClient):
    assert client.get("/admin/accounts").status_code == 401
    assert client.post("/admin/session", json={"password": "nope"}).status_code == 403
    keyed = client.get("/admin/accounts", headers={"Authorization": "Bearer key"})
    assert keyed.status_code == 200

    resp = client.post("/admin/session", json={"password": "secret"})
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" in cookie
    assert client.get("/admin/accounts").status_code == 200
    client.delete("/admin/session")
    assert client.get("/admin/accounts").status_code == 401


def test_ui_page_is_served(client: TestClient):
    resp = client.get("/admin/ui")
    assert resp.status_code == 200 and "Gemini Proxy" in resp.text


def _signed_in(client: TestClient) -> TestClient:
    client.post("/admin/session", json={"password": "secret"})
    return client


def test_add_sign_in_through_the_browser_and_list(client, monkeypatch):

    opened: list[str] = []

    async def open_sign_in(url, proxy=None):
        opened.append(url)
        return "ctx1"

    async def take_session(url, context=None):
        return {"__Secure-1PSID": "sid-pro", "__Secure-1PSIDTS": "ts"}, "pro@example.com"

    monkeypatch.setattr(account_ops, "open_sign_in", open_sign_in)
    monkeypatch.setattr(account_ops, "take_session", take_session)
    c = _signed_in(client)

    added = c.post("/admin/accounts", json={"name": "Pro"}).json()
    assert added["id"] == "pro" and added["state"] == "idle"
    start = c.post("/admin/accounts/pro/login/start").json()
    assert start["vnc"].startswith("/admin/vnc/vnc.html")
    assert opened == [get_settings().cdp_url]
    assert c.get("/admin/accounts").json()["signing_in"] == "pro"
    # One browser: a second sign-in waits for this one.
    c.post("/admin/accounts", json={"name": "Free"})
    assert c.post("/admin/accounts/free/login/start").status_code == 409

    done = c.post("/admin/accounts/pro/login/finish").json()
    assert done["state"] == "ok" and done["email"] == "pro@example.com"
    assert "cookies" not in done
    listed = c.get("/admin/accounts").json()
    assert listed["signing_in"] is None
    assert [a["id"] for a in listed["accounts"]] == ["default", "pro", "free"]

    path = get_settings().data_dir / "accounts.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = AccountStore(path).get("pro")
    assert stored.cookies["__Secure-1PSID"] == "sid-pro" and stored.source == "stored"


def test_finish_before_signing_in_is_refused(client, monkeypatch):

    async def take_session(url, context=None):
        return {"NID": "n"}, None

    monkeypatch.setattr(account_ops, "take_session", take_session)
    c = _signed_in(client)
    assert c.post("/admin/accounts/default/login/finish").status_code == 400


def test_paste_cookies_patch_and_delete(client):
    c = _signed_in(client)
    resp = c.post(
        "/admin/accounts/default/cookies",
        json={"cookies": "__Secure-1PSID=sid-a; __Secure-1PSIDTS=ts-a; NID=x"},
    )
    assert resp.status_code == 200 and resp.json()["state"] == "ok"
    assert c.post("/admin/accounts/default/cookies", json={"cookies": "NID=x"}).status_code == 400

    patched = c.patch("/admin/accounts/default", json={"enabled": False, "name": "Main"}).json()
    assert patched["state"] == "disabled" and patched["name"] == "Main"
    pool_module.pool.slots["default"].cooldowns["gemini-flash"] = 4e9
    assert c.delete("/admin/accounts/default/cooldowns").json()["cooldowns"] == {}
    assert c.delete("/admin/accounts/default").status_code == 200
    assert c.get("/admin/accounts").json()["accounts"] == []
    assert c.delete("/admin/accounts/default").status_code == 404


def test_abandoned_sign_in_lapses(client, monkeypatch):

    async def open_sign_in(url, proxy=None):
        return "ctx1"

    monkeypatch.setattr(account_ops, "open_sign_in", open_sign_in)
    c = _signed_in(client)
    c.post("/admin/accounts", json={"name": "Pro"})
    assert c.post("/admin/accounts/default/login/start").status_code == 200
    assert c.post("/admin/accounts/pro/login/start").status_code == 409
    pool_module.signing_in_since -= pool_module.SIGN_IN_SECONDS + 1
    assert c.post("/admin/accounts/pro/login/start").status_code == 200


def test_proxy_is_set_checked_masked_and_used_for_sign_in(client, monkeypatch):
    from gemini_webapi_proxy.routes import accounts

    checked: list = []

    async def egress_check(proxy, cookies=None):
        checked.append(proxy)
        return {"ip": "203.0.113.10", "country": "NL", "checked_at": 1.0, "proxy": proxy}

    proxies: list = []

    async def open_sign_in(url, proxy=None):
        proxies.append(proxy)
        return "ctx1"

    monkeypatch.setattr(accounts, "egress_check", egress_check)
    monkeypatch.setattr(account_ops, "open_sign_in", open_sign_in)
    c = _signed_in(client)
    assert c.post("/admin/accounts/default/proxy", json={"proxy": "ftp://x"}).status_code == 400
    r = c.post("/admin/accounts/default/proxy", json={"proxy": "http://127.0.0.1:8118"}).json()
    assert r["proxy"] == "http://127.0.0.1:8118" and r["egress"]["country"] == "NL"
    assert r["proxy_set_at"]
    c.post("/admin/accounts/default/login/start")
    assert proxies == ["http://127.0.0.1:8118"]
    # A proxy with a login works for requests but is never shown back in full.
    store = AccountStore(get_settings().data_dir / "accounts.json")
    store.update("default", proxy="http://u:secret@1.2.3.4:8080")
    pool_module.pool.configure(get_settings())
    pool_module.pool.store.load()
    pool_module.pool.sync()
    listed = c.get("/admin/accounts").json()["accounts"][0]
    assert listed["proxy"] == "http://u:***@1.2.3.4:8080"
    assert (
        c.post(
            "/admin/accounts/default/proxy", json={"proxy": "http://u:p@1.2.3.4:8080"}
        ).status_code
        == 400
    )


def test_sorry_redirect_names_the_address_google_saw():
    from gemini_webapi_proxy.routes.accounts import _sorry_ip

    loc = "https://www.google.com/sorry/index?continue=https://gemini.google.com/app&q=EgTLAHEHGAFzeW50aGV0aWMtdGVzdC10b2tlbg"
    assert _sorry_ip(loc) == "203.0.113.7"
    assert _sorry_ip("") is None


def test_ui_vendor_scripts_are_served_from_the_package(client):
    resp = client.get("/admin/ui/vendor/marked.min.js")
    assert resp.status_code == 200 and "marked" in resp.text[:200]
    assert resp.headers["content-type"].startswith("text/javascript")
    assert client.get("/admin/ui/vendor/purify.min.js").status_code == 200
    assert client.get("/admin/ui/vendor/..%2F..%2Fconfig.py").status_code == 404
    assert client.get("/admin/ui/vendor/index.html").status_code == 404
