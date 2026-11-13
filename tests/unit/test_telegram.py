"""Telegram bot: admin linking, access, commands, alerts; the events it relays."""

from __future__ import annotations

import json
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

from gemini_webapi_proxy import events, telegram_bot
from gemini_webapi_proxy.client import pool as pool_module
from gemini_webapi_proxy.config import get_settings

TOKEN = "123456789:" + "A" * 35


@pytest.fixture(autouse=True)
def clean_events():
    events.reset()
    yield
    events.reset()


def test_events_are_throttled_per_account_and_recovery_resets():
    got = []
    events.subscribe(got.append)
    try:
        assert events.emit("quota", "a", "flash")
        assert events.emit("quota", "a", "flash again") is None  # same account, too soon
        assert events.emit("quota", "b", "flash")
        assert events.emit("recovered", "a")
        assert events.emit("quota", "a", "after recovery")  # news again
    finally:
        events.unsubscribe(got.append)
    assert [(e.kind, e.account) for e in got] == [
        ("quota", "a"),
        ("quota", "b"),
        ("recovered", "a"),
        ("quota", "a"),
    ]


class FakeTelegram:
    """Answers Bot API calls; records what the bot sent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        results = {"getMe": {"id": 1, "username": "gop_test_bot"}, "getUpdates": []}
        return httpx.Response(200, json={"ok": True, "result": results.get(method, True)})

    def sent(self) -> list[dict]:
        return [b for m, b in self.calls if m == "sendMessage"]


@pytest.fixture
def tg(monkeypatch):
    fake = FakeTelegram()
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        kwargs.pop("proxy", None)
        return real(*args, **kwargs)

    monkeypatch.setattr(telegram_bot.httpx, "AsyncClient", client)
    monkeypatch.setattr(telegram_bot, "bot", telegram_bot.TelegramBot())
    monkeypatch.setattr(pool_module, "pool", pool_module.AccountPool())
    return fake


def _message(chat_id: int, text: str, username: str = "op") -> dict:
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"username": username},
            "text": text,
        },
    }


async def test_linking_an_admin_with_a_one_time_code(tg):
    settings = get_settings()
    telegram_bot.save_config(settings.data_dir, telegram_bot.TelegramConfig(token=TOKEN))
    bot = telegram_bot.bot
    await bot.start(settings)
    try:
        assert bot.running and bot.me["username"] == "gop_test_bot"
        link = bot.new_link()
        assert link["url"] == f"https://t.me/gop_test_bot?start={link['code']}"

        await bot._handle(_message(42, "/status"))  # not an admin yet
        assert "No access" in tg.sent()[-1]["text"]
        await bot._handle(_message(42, f"/start {link['code']}"))
        assert bot.config.admins == {"42": "op"}
        await bot._handle(_message(43, f"/start {link['code']}", "other"))  # used up
        assert "43" not in bot.config.admins

        saved = telegram_bot.load_config(settings.data_dir)
        assert saved.admins == {"42": "op"}
        path = settings.data_dir / telegram_bot.CONFIG_FILE
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert TOKEN not in json.dumps(bot.status())  # masked for the UI

        await bot._handle(_message(42, "/status"))
        assert "Accounts" in tg.sent()[-1]["text"] and "default" in tg.sent()[-1]["text"]
        await bot._handle(_message(42, "/accounts"))
        buttons = tg.sent()[-1]["reply_markup"]["inline_keyboard"]
        assert buttons[0][0]["callback_data"] == "t:default"
    finally:
        await bot.stop()


async def test_account_buttons_switch_the_account(tg):
    settings = get_settings()
    telegram_bot.save_config(
        settings.data_dir, telegram_bot.TelegramConfig(token=TOKEN, admins={"42": "op"})
    )
    bot = telegram_bot.bot
    await bot.start(settings)
    try:
        query = {"id": "q1", "data": "t:default", "message": {"chat": {"id": 42}, "message_id": 7}}
        await bot._callback(query)
        assert pool_module.pool.slots["default"].account.enabled is False
        answers = [b for m, b in tg.calls if m == "answerCallbackQuery"]
        assert answers[-1]["text"] == "Disabled"
        stranger = {"id": "q2", "data": "t:default", "message": {"chat": {"id": 99}}}
        await bot._callback(stranger)
        assert pool_module.pool.slots["default"].account.enabled is False
        assert [b for m, b in tg.calls if m == "answerCallbackQuery"][-1]["text"] == "No access"
    finally:
        await bot.stop()


async def test_alerts_go_to_admins_for_chosen_kinds(tg):
    settings = get_settings()
    telegram_bot.save_config(
        settings.data_dir,
        telegram_bot.TelegramConfig(token=TOKEN, admins={"42": "op"}, notify=["abuse"]),
    )
    bot = telegram_bot.bot
    await bot.start(settings)
    try:
        events.emit("quota", "a", "not chosen")
        events.emit("abuse", "a", "{'is_clean': False}")
        event = bot._queue.get_nowait()
        assert event.kind == "abuse" and bot._queue.empty()
        text = bot.alert_text(event)
        assert text.startswith("⚠️") and "<b>a</b>" in text and "is_clean" in text
    finally:
        await bot.stop()


def test_pool_reports_lost_sessions_and_outages():
    got = []
    events.subscribe(got.append)
    try:
        slot = pool_module.Slot(pool_module.Account(id="a", name="a"))
        slot.logged_out("gone")
        slot.logged_out("gone again")  # throttled
    finally:
        events.unsubscribe(got.append)
    assert [(e.kind, e.account) for e in got] == [("logged_out", "a")]
    assert slot.down


def test_admin_routes(tg, monkeypatch):
    monkeypatch.setenv("GOP_API_KEY", "key")
    from gemini_webapi_proxy.config import reset_settings_cache

    reset_settings_cache()
    from gemini_webapi_proxy.routes import telegram as routes

    monkeypatch.setattr(routes, "bot", telegram_bot.bot)
    from gemini_webapi_proxy.app import app

    auth = {"Authorization": "Bearer key"}
    with TestClient(app) as c:
        assert c.get("/admin/telegram").status_code == 401
        assert c.get("/admin/telegram", headers=auth).json()["token_set"] is False
        assert c.post("/admin/telegram/link", headers=auth).status_code == 409
        assert c.post("/admin/telegram", headers=auth, json={"token": "nope"}).status_code == 400
        r = c.post(
            "/admin/telegram", headers=auth, json={"token": TOKEN, "notify": ["quota", "bogus"]}
        )
        assert r.status_code == 200 and r.json()["running"] and r.json()["notify"] == ["quota"]
        assert (
            c.post("/admin/telegram/link", headers=auth).json()["url"].startswith("https://t.me/")
        )
        assert c.post("/admin/telegram/test", headers=auth).status_code == 400  # nobody linked
        off = c.post("/admin/telegram", headers=auth, json={"token": ""}).json()
        assert off["token_set"] is False and off["running"] is False
