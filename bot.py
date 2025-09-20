from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

from replicate_api import start_training, generate_image

def setup_handlers(application):
    # Главное меню
    def get_main_keyboard():
        keyboard = [
            [InlineKeyboardButton("📸 Сгенерировать фото", callback_data="generate")],
            [InlineKeyboardButton("🎓 Обучить модель", callback_data="train")],
            [InlineKeyboardButton("ℹ️ О боте", callback_data="about"),
             InlineKeyboardButton("❓ Помощь", callback_data="help")]
        ]
        return InlineKeyboardMarkup(keyboard)

    # /start
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Привет 👋! Я бот для генерации фото.\nВыбери действие:",
            reply_markup=get_main_keyboard()
        )

    # /help
    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Доступные команды:\n"
            "/start — открыть меню\n"
            "/help — помощь\n"
            "/about — информация\n\n"
            "Кнопки:\n📸 — генерация фото\n🎓 — обучение модели"
        )

    # /about
    async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "ℹ️ Этот бот обучает модели и генерирует фото через Replicate."
        )

    # Ответ на кнопки
    async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if query.data == "generate":
            await query.edit_message_text("📸 Введи промпт для генерации:")
            context.user_data["mode"] = "generate"
        elif query.data == "train":
            await query.edit_message_text("🎓 Отправь фото для обучения (3-10 изображений)")
            context.user_data["mode"] = "train"
        elif query.data == "about":
            await query.edit_message_text("ℹ️ Я бот для генерации фото через Replicate.")
        elif query.data == "help":
            await query.edit_message_text("❓ Используй кнопки или команды /start /help /about")

    # Обработка текста (промпт для генерации)
    async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        mode = context.user_data.get("mode")

        if mode == "generate":
            prompt = update.message.text
            await update.message.reply_text("⌛ Генерация фото...")
            image_url = await generate_image(prompt)
            if image_url:
                await update.message.reply_photo(photo=image_url, caption="✅ Сгенерировано")
            else:
                await update.message.reply_text("❌ Ошибка генерации")
            context.user_data["mode"] = None
        else:
            await update.message.reply_text(f"Ты написал: {update.message.text}")

    # Обработка фото (обучение модели)
    async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        mode = context.user_data.get("mode")
        if mode == "train":
            await update.message.reply_text("📤 Фото получено. Запускаю обучение...")
            job_id = await start_training(update.message.photo[-1])
            if job_id:
                await update.message.reply_text(f"✅ Обучение запущено!\nID задачи: `{job_id}`")
            else:
                await update.message.reply_text("❌ Ошибка при запуске обучения")
            context.user_data["mode"] = None

    # Подключаем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("about", about_cmd))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
