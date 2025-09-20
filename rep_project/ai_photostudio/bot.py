import os
import telebot
import requests

BOT_TOKEN = os.getenv("BOT_TOKEN")
BACKEND_ROOT = os.getenv("BACKEND_ROOT")

bot = telebot.TeleBot(BOT_TOKEN)

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "Привет! Отправь мне промпт для генерации фото.")

@bot.message_handler(func=lambda message: True)
def generate(message):
    prompt = message.text
    r = requests.post(f"{BACKEND_ROOT}/generate", params={"prompt": prompt})
    if r.status_code == 200:
        url = r.json().get("url")
        bot.reply_to(message, f"Готово: {url}")
    else:
        bot.reply_to(message, "Ошибка при генерации")

bot.polling()
