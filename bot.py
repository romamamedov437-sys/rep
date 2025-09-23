# bot.py
import os
import json
import time
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List

import httpx
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, CallbackQueryHandler, MessageHandler, CommandHandler, filters
from telegram.error import BadRequest

# ================== CONFIG ==================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
BACKEND_ROOT = (os.getenv("BACKEND_ROOT") or "").rstrip("/")
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").rstrip("/")
DATA_DIR = os.path.join("/opt/render/project/src", "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "users.json")
PHOTOS_TMP = os.path.join(DATA_DIR, "tg_tmp")
os.makedirs(PHOTOS_TMP, exist_ok=True)

PRICES = {"20": 429, "40": 590, "70": 719}
FLASH_OFFER = {"qty": 50, "price": 379}

# Единственный промпт
PROMPT_ID = "p_main"
PROMPT_TEXT = (
    "At the heart of a bustling urban jungle, a man gazes directly into the camera, "
    "his eyes radiating a confident allure. He's leaning on a graffiti-covered brick wall, "
    "the vibrant colors serving as a dynamic backdrop. Dressed in an artistic fusion of streetwear "
    "and futurism, he wears a distressed denim jacket adorned with metallic patches and a holographic "
    "shirt shimmering subtly beneath. Strands of his tousled hair dance freely in the chilly city breeze. "
    "Sunset paints the sky in shades of crimson and gold, casting a warm, ethereal glow on his chiseled features. "
    "The fading daylight reflects off his aviator sunglasses hanging unbuttoned from his shirt. "
    "His pose, casual and relaxed, mirrors the cool street vibe. His smile, a cryptic smirk, hints at an"
)

# ================== LOG ==================
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg-bot")

# ================== STORAGE ==================
from dataclasses import dataclass

@dataclass
class UserState:
    id: int
    balance: int = 0
    has_model: bool = False
    job_id: Optional[str] = None
    uploads: List[str] = field(default_factory=list)
    referred_by: Optional[int] = None
    ref_code: Optional[str] = None
    ref_earn_total: float = 0.0
    ref_earn_ready: float = 0.0
    first_seen_ts: float = field(default_factory=lambda: time.time())
    flash_sent: bool = False
    paid_any: bool = False

def _load_db() -> Dict[str, Any]:
    if not os.path.exists(DB_PATH):
        return {}
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_db(db: Dict[str, Any]) -> None:
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_PATH)

DB = _load_db()

def get_user(uid: int) -> UserState:
    s = DB.get(str(uid))
    if s is None:
        st = UserState(id=uid, ref_code=f"ref_{uid}")
        DB[str(uid)] = st.__dict__
        _save_db(DB)
        return st
    return UserState(**s)

def save_user(st: UserState) -> None:
    DB[str(st.id)] = st.__dict__
    _save_db(DB)

# ================== SAFE SEND ==================
async def safe_edit(q, text: str, reply_markup=None, parse_mode=None):
    try:
        if q and getattr(q, "message", None):
            await q.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
    except Exception:
        pass
    try:
        chat_id = q.message.chat.id if getattr(q, "message", None) and getattr(q.message, "chat", None) else q.from_user.id
        await q.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        pass

# ================== TELEGRAM APP WRAPPER ==================
class TgApp:
    def __init__(self):
        self.app: Optional[Application] = None
        self._bg_tasks: List[asyncio.Task] = []

    @property
    def bot(self):
        return self.app.bot if self.app else None

    async def initialize(self):
        if not BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN not set")
        self.app = Application.builder().token(BOT_TOKEN).updater(None).build()

        self.app.add_handler(CommandHandler("start", self.on_start))
        self.app.add_handler(CallbackQueryHandler(self.on_button))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))

        self.app.add_handler(MessageHandler(filters.ALL, log_any), group=-1)
        self.app.add_error_handler(on_error)
        await self.app.initialize()

    async def start(self):
        assert self.app
        await self.app.start()
        self._bg_tasks.append(asyncio.create_task(self._flash_offer_scheduler()))

    async def stop(self):
        if not self.app:
            return
        for t in self._bg_tasks:
            t.cancel()
        try: await self.app.stop()
        except Exception: pass
        try: await self.app.shutdown()
        except Exception: pass

    async def process_update(self, update: Update):
        assert self.app
        await self.app.process_update(update)

    # -------------- UI --------------
    def kb_home(self, has_paid: bool = False) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 Попробовать", callback_data="try")],
            [InlineKeyboardButton("🖼 Генерация", callback_data="gen_menu")],
            [InlineKeyboardButton("📸 Примеры", callback_data="examples")],
            [InlineKeyboardButton("🤝 Реферальная программа", callback_data="ref_menu")],
            [InlineKeyboardButton("👤 Мой аккаунт", callback_data="account")],
            [InlineKeyboardButton("🆘 Поддержка", callback_data="support")],
        ])

    def kb_tariffs(self, discounted: bool = False) -> InlineKeyboardMarkup:
        def price(v): return int(round(v * 0.9)) if discounted else v
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(f"20 генераций — {price(PRICES['20'])} ₽", callback_data="buy_20")],
            [InlineKeyboardButton(f"40 генераций — {price(PRICES['40'])} ₽", callback_data="buy_40")],
            [InlineKeyboardButton(f"70 генераций — {price(PRICES['70'])} ₽", callback_data="buy_70")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")],
        ])

    def kb_upload_fixed(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Фото загружены", callback_data="photos_done")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
        ])

    def kb_prompt_single(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🏙 Urban portrait", callback_data=PROMPT_ID)],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
        ])

    def kb_buy_or_back(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Купить генерации", callback_data="try")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
        ])

    def kb_ref_menu(self, uid: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📈 Мои доходы", callback_data="ref_income")],
            [InlineKeyboardButton("👥 Мои рефералы", callback_data="ref_list")],
            [InlineKeyboardButton("💳 Вывести средства", callback_data="ref_payout")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
        ])

    # -------------- HANDLERS --------------
    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        st = get_user(u.id)

        if context.args:
            arg = context.args[0]
            if arg.startswith("ref_"):
                try:
                    owner = int(arg.replace("ref_", "").strip())
                    if owner != u.id and not st.referred_by:
                        st.referred_by = owner
                        save_user(st)
                except Exception:
                    pass

        text = (
            "👋 <b>Привет!</b> Это <b>PhotoFly</b> — твоя персональная фотостудия с ИИ.\n\n"
            "Загрузи фото, обучим твою модель и будем генерить образы."
        )
        await update.effective_message.reply_text(text, reply_markup=self.kb_home(), parse_mode=ParseMode.HTML)

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        uid = q.from_user.id
        st = get_user(uid)
        data = q.data or ""

        if data == "back_home":
            await safe_edit(q, "📍 Главное меню", reply_markup=self.kb_home(has_paid=st.paid_any))
            return

        if data == "try":
            discounted = bool(st.referred_by)
            if discounted:
                text = (
                    "💎 <b>Тарифы генераций</b> <i>(скидка −10% по реферальной ссылке)</i>\n\n"
                    f"• 20 — <s>{PRICES['20']} ₽</s> <b>{int(round(PRICES['20']*0.9))} ₽</b>\n"
                    f"• 40 — <s>{PRICES['40']} ₽</s> <b>{int(round(PRICES['40']*0.9))} ₽</b>\n"
                    f"• 70 — <s>{PRICES['70']} ₽</s> <b>{int(round(PRICES['70']*0.9))} ₽</b>\n"
                )
            else:
                text = (
                    "💎 <b>Тарифы генераций</b>\n\n"
                    f"• 20 — <b>{PRICES['20']} ₽</b>\n"
                    f"• 40 — <b>{PRICES['40']} ₽</b>\n"
                    f"• 70 — <b>{PRICES['70']} ₽</b>\n"
                )
            await safe_edit(q, text, reply_markup=self.kb_tariffs(discounted), parse_mode=ParseMode.HTML)
            return

        if data in ("buy_20", "buy_40", "buy_70"):
            qty = int(data.split("_")[1])
            price = PRICES[str(qty)]
            if st.referred_by:
                price = int(round(price * 0.9))

            st.balance += qty
            st.paid_any = True
            save_user(st)

            if st.referred_by:
                ref = get_user(st.referred_by)
                ref_gain = round(price * 0.20, 2)
                ref.ref_earn_total += ref_gain
                ref.ref_earn_ready += ref_gain
                save_user(ref)

            await safe_edit(
                q,
                f"✅ Оплата прошла. Начислено: <b>{qty}</b> генераций.\n\n"
                "Теперь загрузи 15–50 фото для обучения модели.\n"
                "Когда закончишь — нажми «Фото загружены».",
                parse_mode=ParseMode.HTML
            )
            await self._send_requirements(uid, context)
            return

        if data == "photos_done":
            await safe_edit(q, "🚀 Обучение запущено!\n\nЭто может занять <b>10–30 минут</b>. Напишем, когда всё будет готово.", parse_mode=ParseMode.HTML)
            asyncio.create_task(self._launch_training_and_wait(uid, context))
            return

        if data == "gen_menu":
            if not st.paid_any:
                await safe_edit(q, "Сначала приобретите пакет генераций.", reply_markup=self.kb_buy_or_back())
                return
            if not st.has_model:
                await safe_edit(q, "⏳ Модель ещё обучается. Напишем, когда будет готово.")
                return
            await safe_edit(q, "Выбери стиль:", reply_markup=self.kb_prompt_single())
            return

        if data == PROMPT_ID:
            if st.balance < 3:
                await safe_edit(q, "Нет доступных генераций. Пополните баланс.", reply_markup=self.kb_buy_or_back())
                return
            await safe_edit(q, "🎨 Генерируем 3 изображения… это займёт ~30–60 секунд.")
            try:
                imgs = await self._generate(uid, st.job_id, PROMPT_TEXT, 3)
            except Exception:
                await context.bot.send_message(chat_id=uid, text="❌ Ошибка при генерации. Попробуйте ещё раз.")
                return

            st.balance -= 3
            save_user(st)

            media = [InputMediaPhoto(imgs[0], caption=f"Готово! Баланс: {st.balance}")] + [InputMediaPhoto(u) for u in imgs[1:]]
            await context.bot.send_media_group(chat_id=uid, media=media)
            await context.bot.send_message(chat_id=uid, text="Сгенерировать ещё?", reply_markup=self.kb_prompt_single())
            return

        if data == "examples":
            await safe_edit(
                q,
                "📸 Примеры работ: @PhotoFly_Examples",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Открыть канал", url="https://t.me/PhotoFly_Examples")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
                ])
            ); return

        if data == "support":
            await safe_edit(
                q,
                "🆘 Поддержка: @photofly_ai",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Написать в поддержку", url="https://t.me/photofly_ai")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
                ]),
                parse_mode=ParseMode.HTML
            ); return

        if data == "account":
            text = (
                "👤 <b>Мой аккаунт</b>\n\n"
                f"ID: <code>{uid}</code>\n"
                f"Генераций доступно: <b>{st.balance}</b>\n"
                f"Модель обучена: <b>{'да' if st.has_model else 'нет'}</b>"
            )
            await safe_edit(q, text, reply_markup=self.kb_home(has_paid=st.paid_any), parse_mode=ParseMode.HTML); return

        if data == "ref_menu":
            link = f"https://t.me/{(await context.bot.get_me()).username}?start={st.ref_code}"
            text = (
                "🤝 <b>Реферальная программа</b>\n"
                "• 20% с покупок друзей\n• друзьям −10% на первый заказ\n"
                f"Твоя ссылка:\n<code>{link}</code>"
            )
            await safe_edit(q, text, reply_markup=self.kb_ref_menu(uid), parse_mode=ParseMode.HTML); return

        if data == "ref_income":
            text = (
                "📈 <b>Мои доходы</b>\n\n"
                f"Всего: <b>{get_user(uid).ref_earn_total:.2f} ₽</b>\n"
                f"Доступно к выводу: <b>{get_user(uid).ref_earn_ready:.2f} ₽</b>\n"
                "Минимум к выводу: 500 ₽."
            )
            await safe_edit(q, text, reply_markup=self.kb_ref_menu(uid), parse_mode=ParseMode.HTML); return

        if data == "ref_list":
            await safe_edit(q, "Список рефералов появится позже.", reply_markup=self.kb_ref_menu(uid)); return

        if data == "ref_payout":
            await safe_edit(
                q,
                "Напиши @photofly_ai для вывода средств (от 500 ₽).",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Написать поддержку", url="https://t.me/photofly_ai")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
                ])
            ); return

        if data == "buy_flash_50":
            st.balance += FLASH_OFFER["qty"]; st.paid_any = True; save_user(st)
            await safe_edit(q, f"✅ Начислено {FLASH_OFFER['qty']} генераций за {FLASH_OFFER['price']} ₽.", parse_mode=ParseMode.HTML)
            await self._send_requirements(uid, context); return

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        _ = get_user(uid)
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        local_path = os.path.join(PHOTOS_TMP, f"{uid}_{int(time.time())}.jpg")
        await file.download_to_drive(local_path)

        try:
            async with httpx.AsyncClient(timeout=120) as cl:
                with open(local_path, "rb") as f:
                    data = {"user_id": str(uid)}
                    files = {"file": ("photo.jpg", f, "image/jpeg")}
                    r = await cl.post(f"{BACKEND_ROOT}/api/upload_photo", data=data, files=files)
                    r.raise_for_status()
        except Exception:
            await update.effective_message.reply_text("⚠️ Не удалось загрузить фото. Повтори ещё раз.")
            return

        await update.effective_message.reply_text("Фото принято ✅\nЗагрузи ещё и нажми «Фото загружены», когда будешь готов.")

    # ---------- HELPERS ----------
    async def _send_requirements(self, uid: int, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "📥 Загрузка фото для обучения\n\n"
            "Загрузи 15–50 фотографий (лучше 25–35), разные ракурсы и сцены.\n"
            "Когда закончишь — нажми «Фото загружены»."
        )
        await context.bot.send_message(chat_id=uid, text=text, reply_markup=self.kb_upload_fixed(), parse_mode=ParseMode.HTML)

    async def _launch_training_and_wait(self, uid: int, context: ContextTypes.DEFAULT_TYPE):
        try:
            async with httpx.AsyncClient(timeout=180) as cl:
                r = await cl.post(f"{BACKEND_ROOT}/api/train", data={"user_id": str(uid)})
                r.raise_for_status()
                job_id = r.json().get("job_id")
                if not job_id:
                    raise RuntimeError("no job_id from backend")
        except Exception:
            await context.bot.send_message(chat_id=uid, text="❌ Не удалось запустить обучение. Попробуйте ещё раз.")
            return

        st = get_user(uid); st.job_id = job_id; save_user(st)

        status_url = f"{BACKEND_ROOT}/api/status/{job_id}"
        for _ in range(300):
            try:
                async with httpx.AsyncClient(timeout=30) as cl:
                    rr = await cl.get(status_url); rr.raise_for_status()
                    dd = rr.json()
                    status = (dd.get("status") or "").lower()
                    model_id = dd.get("model_id")
                    if model_id:
                        st.has_model = True
                        save_user(st)
                        break
                    if status in ("failed", "canceled", "cancelled", "error"):
                        await context.bot.send_message(chat_id=uid, text="❌ Обучение не удалось. Попробуйте ещё раз.")
                        return
            except Exception:
                pass
            await asyncio.sleep(2)

        if not st.has_model:
            await context.bot.send_message(chat_id=uid, text="❌ Время ожидания вышло. Попробуйте позже.")
            return

        await context.bot.send_message(chat_id=uid, text="✨ Готово! Модель обучена.\nВыбери стиль:", reply_markup=self.kb_prompt_single())

    async def _generate(self, uid: int, job_id: Optional[str], prompt: str, n: int) -> List[str]:
        body = {"user_id": str(uid), "prompt": prompt, "num_images": n}
        if job_id:
            body["job_id"] = job_id
        async with httpx.AsyncClient(timeout=240) as cl:
            r = await cl.post(f"{BACKEND_ROOT}/api/generate", json=body)
            r.raise_for_status()
            data = r.json()
            urls = data.get("images") or []
            if not urls:
                raise RuntimeError("empty images")
            return urls

    # ---------- FLASH OFFER ----------
    async def _flash_offer_scheduler(self):
        while True:
            now = time.time()
            try:
                for k, v in list(DB.items()):
                    st = UserState(**v)
                    if st.flash_sent:
                        continue
                    if now - (st.first_seen_ts or now) >= 24 * 3600:
                        await self._send_flash_offer(st.id)
                        st.flash_sent = True
                        save_user(st)
            except Exception:
                pass
            await asyncio.sleep(1800)

    async def _send_flash_offer(self, uid: int):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Приобрести", callback_data="buy_flash_50")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
        ])
        try:
            await self.app.bot.send_message(chat_id=uid, text=f"🔥 Только сейчас! {FLASH_OFFER['qty']} генераций за {FLASH_OFFER['price']} ₽.", reply_markup=kb)
        except Exception:
            pass

# ========= ГЛОБАЛЬНЫЕ ЛОГ/ОШИБКИ =========
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    msg = str(getattr(context, "error", ""))
    if ("query is too old" in msg) or ("query ID is invalid" in msg) or ("response timeout expired" in msg):
        log.warning(f"Ignored old callback error: {msg}")
        return
    log.exception("Unhandled error in handler", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Упс, произошла ошибка. Уже чиним.")
    except Exception:
        pass

async def log_any(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kind = ("callback_query" if update.callback_query else "message" if update.message else "channel_post" if update.channel_post else "other")
    uid = update.effective_user.id if update.effective_user else "-"
    log.info(f"Update: kind={kind} from={uid}")

# ========= EXPORT =========
tg_app = TgApp()

_init_started = False
async def ensure_initialized() -> None:
    global _init_started
    if getattr(tg_app, "app", None) and tg_app.app.initialized:
        if not tg_app.app.running:
            await tg_app.start()
        return
    if _init_started:
        return
    _init_started = True
    await tg_app.initialize()
    await tg_app.start()
    log.info("✅ Telegram Application initialized & started (webhook mode)")
