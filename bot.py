import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from replicate_api import generate_image

BOT_TOKEN = os.getenv("BOT_TOKEN")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Пришли мне описание, и я сгенерирую картинку.")

async def generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши описание после команды.")
        return
    prompt = " ".join(context.args)
    image_url = generate_image(prompt)
    await update.message.reply_text(f"Вот твоя картинка: {image_url}")

def run_bot():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gen", generate))
    return app.run_polling()
