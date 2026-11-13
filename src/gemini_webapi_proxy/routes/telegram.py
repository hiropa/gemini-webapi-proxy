"""Admin UI: the Telegram bot (token, admins, alerts). See telegram_bot.py."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from gemini_webapi_proxy import events
from gemini_webapi_proxy.auth import require_admin
from gemini_webapi_proxy.config import get_settings
from gemini_webapi_proxy.telegram_bot import bot, load_config

router = APIRouter(
    prefix="/admin/telegram", tags=["admin-ui"], dependencies=[Depends(require_admin)]
)


def _report() -> dict:
    return {
        **bot.status(),
        "kinds": events.KINDS,
        "events": [
            {"kind": e.kind, "title": e.title, "account": e.account, "text": e.text, "ts": e.ts}
            for e in reversed(list(events.recent)[-30:])
        ],
    }


@router.get("")
async def telegram_status() -> dict:
    if bot.data_dir is None:  # never started: show what is saved
        bot.config = load_config(get_settings().data_dir)
        bot.data_dir = get_settings().data_dir
    return _report()


class TelegramIn(BaseModel):
    token: str | None = None  # "" removes it
    enabled: bool | None = None
    notify: list[str] | None = None
    ui_url: str | None = None
    proxy: str | None = None


@router.post("")
async def telegram_update(body: TelegramIn) -> dict:
    settings = get_settings()
    config = load_config(settings.data_dir)
    changes = body.model_dump(exclude_none=True)
    if "token" in changes:
        token = changes.pop("token").strip()
        if token and (":" not in token or len(token) < 30):
            raise HTTPException(
                status_code=400, detail="This does not look like a bot token from @BotFather"
            )
        if token != config.token:
            config.admins = {}  # a new bot: its chats are new too
        config.token = token
    if "notify" in changes:
        changes["notify"] = [k for k in changes["notify"] if k in events.KINDS]
    for key, value in changes.items():
        setattr(config, key, value.strip() if isinstance(value, str) else value)
    bot.data_dir = settings.data_dir
    bot.config = config
    bot.save()
    await bot.start(settings)
    return _report()


@router.post("/link")
async def telegram_link() -> dict:
    if not bot.running or not bot.me:
        raise HTTPException(
            status_code=409, detail=bot.error or "The bot is not running: set the token first"
        )
    return bot.new_link()


@router.delete("/admins/{chat_id}")
async def telegram_remove_admin(chat_id: str) -> dict:
    if bot.config.admins.pop(chat_id, None) is None:
        raise HTTPException(status_code=404, detail="No such admin")
    bot.save()
    return _report()


@router.post("/test")
async def telegram_test() -> dict:
    if not bot.running:
        raise HTTPException(status_code=409, detail=bot.error or "The bot is not running")
    if not bot.config.admins:
        raise HTTPException(status_code=400, detail="No admins linked yet")
    sent, failed = 0, []
    for chat_id in list(bot.config.admins):
        try:
            await bot.send(chat_id, "✅ Test message from Gemini Proxy. Alerts will arrive here.")
            sent += 1
        except Exception as exc:
            failed.append(f"{chat_id}: {bot._safe(exc)[:120]}")
    return {"sent": sent, "failed": failed}
