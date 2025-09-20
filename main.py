import os
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mysecret123")
PUBLIC_URL = os.getenv("PUBLIC_URL")

app = FastAPI()

# Создаём приложение Telegram
telegram_app = Application.builder().token(BOT_TOKEN).build()


@app.on_event("startup")
async def startup_event():
    # Устанавливаем вебхук для Telegram
    if PUBLIC_URL:
        webhook_url = f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"
        await telegram_app.bot.set_webhook(url=webhook_url)
        print(f"Webhook set: {webhook_url}")


@app.on_event("shutdown")
async def shutdown_event():
    # Удаляем вебхук при остановке
    await telegram_app.bot.delete_webhook()
    print("Webhook deleted")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post(f"/webhook/{{secret}}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        return {"error": "invalid secret"}

    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"status": "ok"}
