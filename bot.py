import os
import logging
import httpx
from typing import Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from telegram.error import BadRequest

# ========= ENV =========
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
# URL бэкенда с твоими эндпоинтами /api/upload_photo /api/train /api/status /api/generate
# Если бэкенд на этом же сервисе — просто укажи этот же PUBLIC_URL.
BACKEND_ROOT = (os.getenv("BACKEND_ROOT") or "").rstrip("/")

# ========= LOG =========
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg-bot")

if not BOT_TOKEN:
    log.warning("BOT_TOKEN пуст — бот не стартует.")
if not BACKEND_ROOT:
    log.warning("BACKEND_ROOT пуст — кнопки train/upload/status/generate работать не будут.")

# ========= APP =========
tg_app = Application.builder().token(BOT_TOKEN).build()

# ========= STATE =========
user_jobs: Dict[int, str] = {}   # user_id -> job_id

# ========= UI =========
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Загрузить фото", callback_data="upload")],
        [InlineKeyboardButton("✅ Фотографии загружены", callback_data="photos_done")],
        [InlineKeyboardButton("📊 Проверить прогресс", callback_data="status")],
        [InlineKeyboardButton("✨ Сгенерировать фото", callback_data="generate")],
    ])

def kb_upload_done() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Фотографии загружены", callback_data="photos_done")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")],
    ])

async def safe_edit(q, text: str, reply_markup=None, parse_mode=None):
    try:
        await q.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

# ========= HANDLERS =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Привет! 👋 Я помогу загрузить фото, обучить модель и сгенерировать портреты.",
        reply_markup=kb_main()
    )

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Принимаем фото из TG, отправляем на /api/upload_photo (multipart form).
    Важно: backend ждёт form-data: user_id и файл.
    """
    if not BACKEND_ROOT:
        await update.message.reply_text("⚠️ BACKEND_ROOT не настроен.")
        return

    tgf = await update.message.photo[-1].get_file()
    local_path = await tgf.download_to_drive()

    async with httpx.AsyncClient(timeout=60) as cl:
        with open(local_path, "rb") as fp:
            r = await cl.post(
                f"{BACKEND_ROOT}/api/upload_photo",
                data={"user_id": update.effective_user.id},
                files={"file": ("photo.jpg", fp, "image/jpeg")},
            )
            r.raise_for_status()

    await update.message.reply_text(
        "Фото загружено ✅\nЗагрузите ещё или нажмите «Фотографии загружены», чтобы запустить обучение.",
        reply_markup=kb_upload_done()
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "upload":
        await safe_edit(
            q,
            "Пришлите 10–30 фото для обучения. Когда закончите — нажмите кнопку ниже.",
            reply_markup=kb_upload_done()
        )
        return

    if q.data == "photos_done":
        if not BACKEND_ROOT:
            await safe_edit(q, "⚠️ BACKEND_ROOT не настроен.", reply_markup=kb_main())
            return
        # /api/train ждёт form-data, НЕ json
        async with httpx.AsyncClient(timeout=60) as cl:
            r = await cl.post(f"{BACKEND_ROOT}/api/train", data={"user_id": uid})
            r.raise_for_status()
            data = r.json()

        job_id = data.get("job_id")
        if not job_id:
            await safe_edit(q, "❌ Бэкенд не вернул job_id. Попробуйте ещё раз.", reply_markup=kb_main())
            return

        user_jobs[uid] = job_id
        await safe_edit(
            q,
            f"🚀 Обучение запущено!\nID задачи: `{job_id}`\n\nНажмите «Проверить прогресс».",
            reply_markup=kb_main(),
            parse_mode="Markdown"
        )
        return

    if q.data == "status":
        job_id = user_jobs.get(uid)
        if not job_id:
            await safe_edit(q, "❌ У вас нет активной задачи. Сначала обучите модель.", reply_markup=kb_main())
            return

        if not BACKEND_ROOT:
            await safe_edit(q, "⚠️ BACKEND_ROOT не настроен.", reply_markup=kb_main())
            return

        async with httpx.AsyncClient(timeout=60) as cl:
            r = await cl.get(f"{BACKEND_ROOT}/api/status/{job_id}")
            if r.status_code == 404:
                await safe_edit(q, "❌ Задача не найдена (сервер перезапускался?). Запустите обучение заново.", reply_markup=kb_main())
                return
            r.raise_for_status()
            data = r.json()

        status = data.get("status", "unknown")
        progress = data.get("progress", 0)
        model_id = data.get("model_id") or "—"
        await safe_edit(
            q,
            f"📊 Статус: *{status}*\nПрогресс: *{progress}%*\nМодель: `{model_id}`",
            reply_markup=kb_main(),
            parse_mode="Markdown"
        )
        return

    if q.data == "generate":
        if not BACKEND_ROOT:
            await safe_edit(q, "⚠️ BACKEND_ROOT не настроен.", reply_markup=kb_main())
            return
        # Простейшая генерация по дефолтному промпту
        payload = {
            "user_id": uid,
            "prompt": "studio portrait, cinematic lighting, 85mm, f/1.8, ultra-detailed skin",
            "num_images": 1
        }
        async with httpx.AsyncClient(timeout=120) as cl:
            r = await cl.post(f"{BACKEND_ROOT}/api/generate", json=payload)
            r.raise_for_status()
            data = r.json()

        urls = data.get("images") or []
        if not urls:
            await safe_edit(q, "❌ Бэкенд не вернул изображения.", reply_markup=kb_main())
            return

        await safe_edit(q, "Готово ✅ Вот результат:", reply_markup=kb_main())
        for u in urls:
            await q.message.reply_photo(photo=u)
        return

    if q.data == "back_home":
        await safe_edit(q, "Главное меню:", reply_markup=kb_main())
        return


# ========= REGISTER =========
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(MessageHandler(filters.PHOTO, on_photo))
tg_app.add_handler(CallbackQueryHandler(on_button))
