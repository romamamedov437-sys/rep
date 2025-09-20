import os
import logging
import httpx
from typing import Dict

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest

# ---------- ENV ----------
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
BACKEND_ROOT = (os.getenv("BACKEND_ROOT") or "").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not BACKEND_ROOT:
    raise RuntimeError("BACKEND_ROOT is not set")

# ---------- LOG ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg-bot")

# ---------- STATE ----------
user_jobs: Dict[int, str] = {}

# ---------- KEYBOARDS ----------
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📸 Загрузить фото", callback_data="upload")],
            [InlineKeyboardButton("✅ Фотографии загружены", callback_data="photos_done")],
            [InlineKeyboardButton("📊 Проверить прогресс", callback_data="status")],
            [InlineKeyboardButton("✨ Сгенерировать фото", callback_data="generate")],
        ]
    )

def kb_upload_done() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Фотографии загружены", callback_data="photos_done")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")],
        ]
    )

# ---------- UTILS ----------
async def safe_edit(q, text: str, reply_markup=None):
    try:
        await q.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise

# ---------- HANDLERS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Привет! 👋 Я помогу загрузить фото, обучить модель и сгенерировать портреты.\n"
        "Начнём? Жми кнопки ниже.",
        reply_markup=kb_main(),
    )

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        file = await update.message.photo[-1].get_file()
        local_path = await file.download_to_drive()

        async with httpx.AsyncClient(timeout=120) as cl:
            with open(local_path, "rb") as fp:
                r = await cl.post(
                    f"{BACKEND_ROOT}/api/upload_photo",
                    data={"user_id": update.effective_user.id},
                    files={"file": ("photo.jpg", fp, "image/jpeg")},
                )
                r.raise_for_status()

        await update.message.reply_text(
            "Фото загружено ✅\nКогда закончите — нажмите «Фотографии загружены».",
            reply_markup=kb_upload_done(),
        )
    except Exception as e:
        log.exception("upload failed")
        await update.message.reply_text(f"❌ Не удалось загрузить фото: {e}")

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "upload":
        await safe_edit(
            q,
            "Пришлите 10–30 фото для обучения.\n"
            "Когда закончите — нажмите «Фотографии загружены».",
            reply_markup=kb_upload_done(),
        )
        return

    if q.data == "photos_done":
        try:
            async with httpx.AsyncClient(timeout=60) as cl:
                r = await cl.post(f"{BACKEND_ROOT}/api/train", data={"user_id": uid})
                r.raise_for_status()
            data = r.json()
            job_id = data.get("job_id")
            if not job_id:
                await safe_edit(q, "❌ Бэкенд не вернул job_id.", reply_markup=kb_main())
                return
            user_jobs[uid] = job_id
            await safe_edit(
                q,
                f"🚀 Обучение запущено!\nID задачи: `{job_id}`\n\nНажмите «Проверить прогресс».",
                reply_markup=kb_main(),
            )
        except Exception as e:
            log.exception("train failed")
            await safe_edit(q, f"❌ Не удалось запустить обучение: {e}", reply_markup=kb_main())
        return

    if q.data == "status":
        job_id = user_jobs.get(uid)
        if not job_id:
            await safe_edit(
                q,
                "❌ У вас нет активной задачи. Сначала обучите модель.",
                reply_markup=kb_main(),
            )
            return
        try:
            async with httpx.AsyncClient(timeout=30) as cl:
                r = await cl.get(f"{BACKEND_ROOT}/api/status/{job_id}")
                if r.status_code == 404:
                    await safe_edit(
                        q,
                        "❌ Задача не найдена (возможно, сервер перезапускался).\nЗапустите обучение заново.",
                        reply_markup=kb_main(),
                    )
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
            )
        except Exception as e:
            log.exception("status failed")
            await safe_edit(q, f"❌ Ошибка запроса статуса: {e}", reply_markup=kb_main())
        return

    if q.data == "generate":
        payload = {
            "user_id": uid,
            "prompt": "studio portrait, cinematic lighting, 85mm, f/1.8, ultra-detailed skin",
            "num_images": 1,
        }
        try:
            async with httpx.AsyncClient(timeout=180) as cl:
                r = await cl.post(f"{BACKEND_ROOT}/api/generate", json=payload)
                r.raise_for_status()
            data = r.json()
            urls = data.get("images") or []
            await safe_edit(q, "Готово ✅ Вот результат:", reply_markup=kb_main())
            for u in urls:
                await q.message.reply_photo(photo=u)
        except Exception as e:
            log.exception("generate failed")
            await safe_edit(q, f"❌ Не удалось сгенерировать: {e}", reply_markup=kb_main())
        return

    if q.data == "back_home":
        await safe_edit(q, "Главное меню:", reply_markup=kb_main())
        return

# ---------- ENTRY ----------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_button))

    log.info("🤖 Bot is starting (long polling)…")
    # Блокирующий, без asyncio
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
