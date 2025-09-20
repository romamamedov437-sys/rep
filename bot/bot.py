import os, logging, httpx, asyncio, json
from typing import Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN","")
BACKEND_ROOT = (os.getenv("BACKEND_ROOT") or "").rstrip("/")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

app = Application.builder().token(BOT_TOKEN).build()
user_jobs: Dict[int,str] = {}
user_prompt: Dict[int,str] = {}

def kb_home():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Загрузить фото", callback_data="upload")],
        [InlineKeyboardButton("✅ Фотографии загружены", callback_data="photos_done")],
        [InlineKeyboardButton("📊 Проверить прогресс", callback_data="status")],
        [InlineKeyboardButton("✨ Сгенерировать фото", callback_data="gen_menu")],
    ])

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Это фотостудия на ИИ.\nПришли 10–30 фото, затем нажми «Фотографии загружены».",
        reply_markup=kb_home()
    )

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "upload":
        await q.edit_message_text("Пришлите 10–30 фото. Когда закончите — нажмите «Фотографии загружены».",
                                  reply_markup=kb_home())
        return

    if q.data == "photos_done":
        async with httpx.AsyncClient(timeout=120) as cl:
            r = await cl.post(f"{BACKEND_ROOT}/api/train", data={"user_id": uid})
            r.raise_for_status()
            data = r.json()
        user_jobs[uid] = data["job_id"]
        await q.edit_message_text(f"🚀 Обучение запущено!\nID: `{data['job_id']}`\nНажимай «Проверить прогресс».",
                                  reply_markup=kb_home(), parse_mode="Markdown")
        return

    if q.data == "status":
        job = user_jobs.get(uid)
        if not job:
            await q.edit_message_text("У тебя нет активной задачи. Сначала загрузи фото и начни обучение.",
                                      reply_markup=kb_home())
            return
        async with httpx.AsyncClient(timeout=60) as cl:
            r = await cl.get(f"{BACKEND_ROOT}/api/status/{job}")
            r.raise_for_status()
            s = r.json()
        await q.edit_message_text(
            f"📊 Статус: *{s['status']}*\nПрогресс: *{s['progress']}%*\nМодель: `{s.get('model_id') or '—'}`",
            reply_markup=kb_home(), parse_mode="Markdown"
        )
        return

    if q.data == "gen_menu":
        # подтянем промпты
        async with httpx.AsyncClient(timeout=30) as cl:
            r = await cl.get(f"{BACKEND_ROOT}/api/prompts")
            r.raise_for_status()
            items = r.json()["items"][:10]   # покажем 10 штук в меню (остальные пользователь введёт вручную)
        buttons = [[InlineKeyboardButton(str(i+1), callback_data=f"gen_p_{i}")] for i in range(len(items))]
        buttons.append([InlineKeyboardButton("📝 Ввести свой промпт", callback_data="gen_custom")])
        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="home")])
        # временно запомним подрезанные промпты
        ctx.chat_data["short_prompts"] = items
        await q.edit_message_text("Выбери промпт (или введи свой):", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if q.data.startswith("gen_p_"):
        idx = int(q.data.split("_")[-1])
        items = update.callback_query.message.bot_data  # не нужен, берём из chat_data
        pr_list = ctx.chat_data.get("short_prompts", [])
        if idx >= len(pr_list):
            await q.answer("Неверный выбор", show_alert=True); return
        user_prompt[uid] = pr_list[idx]
        await do_generate(q, uid, ctx)
        return

    if q.data == "gen_custom":
        await q.edit_message_text("Пришли текст промпта одним сообщением. После — я сгенерирую фото.",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="home")]]))
        ctx.user_data["await_prompt"] = True
        return

    if q.data == "home":
        await q.edit_message_text("Главное меню:", reply_markup=kb_home())
        return

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("await_prompt"):
        ctx.user_data["await_prompt"] = False
        user_prompt[update.effective_user.id] = update.message.text.strip()
        msg = await update.message.reply_text("Генерирую… ⏳")
        # оборачиваем в CallbackQuery-like объект
        class Q: 
            message = msg
            async def edit_message_text(self, *a, **kw): await msg.edit_text(*a, **kw)
        await do_generate(Q(), update.effective_user.id, ctx)

async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tgf = await update.message.photo[-1].get_file()
    local = await tgf.download_to_drive()
    with open(local, "rb") as fp:
        async with httpx.AsyncClient(timeout=120) as cl:
            r = await cl.post(f"{BACKEND_ROOT}/api/upload_photo",
                              data={"user_id": update.effective_user.id},
                              files={"file": ("photo.jpg", fp, "image/jpeg")})
            r.raise_for_status()
    await update.message.reply_text("Фото сохранено ✅")

async def do_generate(q_or_cb, uid: int, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = user_prompt.get(uid)
    if not prompt:
        await q_or_cb.edit_message_text("Нет промпта. Выбери в меню.", reply_markup=kb_home()); return
    payload = {"user_id": uid, "prompt": prompt, "num_images": 1}
    async with httpx.AsyncClient(timeout=None) as cl:
        r = await cl.post(f"{BACKEND_ROOT}/api/generate", json=payload)
        if r.status_code >= 400:
            await q_or_cb.edit_message_text(f"Ошибка генерации:\n{r.text}", reply_markup=kb_home()); return
        data = r.json()
    await q_or_cb.edit_message_text("Готово ✅", reply_markup=kb_home())
    for u in data.get("images", []):
        await ctx.bot.send_photo(chat_id=uid, photo=u)

def main():
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()

if __name__ == "__main__":
    main()
