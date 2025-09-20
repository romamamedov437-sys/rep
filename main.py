import os
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application
from bot import setup_handlers

TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mysecret123")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

app = FastAPI()
telegram_app = Application.builder().token(TOKEN).build()
setup_handlers(telegram_app)

@app.on_event("startup")
async def startup_event():
    if RENDER_URL:
        webhook_url = f"{RENDER_URL}/webhook/{WEBHOOK_SECRET}"
        await telegram_app.bot.set_webhook(webhook_url)
        print(f"✅ Webhook set: {webhook_url}")
    else:
        print("⚠️ RENDER_EXTERNAL_URL не найден")

@app.post(f"/webhook/{{secret}}")
async def webhook(request: Request, secret: str):
    if secret != WEBHOOK_SECRET:
        return {"error": "invalid secret"}

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"message": "🚀 Бот работает. Напиши ему в Telegram /start"}
