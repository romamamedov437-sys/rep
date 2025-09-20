import os
import logging
from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.error import TelegramError

from bot import tg_app, ensure_initialized

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("web")

app = FastAPI()

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").rstrip("/")
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "hook").strip()

if not BOT_TOKEN:
    log.warning("BOT_TOKEN не задан — бот не сможет работать.")
if not PUBLIC_URL:
    log.warning("PUBLIC_URL не задан — вебхук не будет установлен автоматически.")

@app.on_event("startup")
async def startup_event():
    # Инициализируем и запускаем Telegram Application БЕЗ polling
    await ensure_initialized()

    if PUBLIC_URL:
        hook_url = f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"
        try:
            await tg_app.bot.delete_webhook(drop_pending_updates=True)
            await tg_app.bot.set_webhook(
                hook_url,
                allowed_updates=["message", "callback_query"]
            )
            log.info(f"✅ Webhook установлен: {hook_url}")
        except TelegramError as e:
            log.error(f"Webhook error: {e!r}")

@app.on_event("shutdown")
async def shutdown_event():
    try:
        await tg_app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    await tg_app.stop()
    log.info("🛑 Telegram application stopped")

@app.get("/")
async def root():
    return {"ok": True}

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    # На случай горячего рестарта — ещё раз убеждаемся, что инициализированы
    await ensure_initialized()

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}
