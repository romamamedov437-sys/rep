# bot.py
import os
import json
import time
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple

import httpx
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, CallbackQueryHandler, MessageHandler, CommandHandler, filters

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

# ---------- PROMPTS ----------
# базовый реалистичный «клей» для фотореализма
BASE_REAL = (
    "Ultra-photorealistic, DSLR look, natural skin texture (visible pores, micro-blemishes), "
    "subtle film grain, cinematic color grading, soft falloff, correct perspective, "
    "accurate shadows, realistic reflections, no plastic smoothing, 50mm f/1.8 depth-of-field."
)

# 20 мужских — по 5–7 в стиле
MEN_CATALOG: Dict[str, List[str]] = {
    "Бизнес / офис": [
        "Hyper-realistic portrait of a confident man in a glass-corner boardroom at golden hour, navy polo, beige tailored trousers, suede loafers, Rolex steel on wrist, skyline bokeh behind; seated in leather armchair, relaxed posture; whiskey decanter on side table; " + BASE_REAL,
        "Executive portrait in a marble lobby with floor-to-ceiling windows, charcoal suit without tie, pocket square, subtle cufflinks, hands loosely clasped; soft rim light from windows; " + BASE_REAL,
        "Close-up half-body portrait in a minimal CEO office, matte black shelves, a single bonsai, silver laptop closed; crisp white oxford shirt, open collar; contemplative look; " + BASE_REAL,
        "Standing near panoramic window in private office, city sunset haze outside, brown cashmere blazer over knit polo, leather belt, watch peeking; hands in pockets; " + BASE_REAL,
        "Seated at modern conference table, tablet and pen neatly aligned, neutral tones, soft overhead practicals; gentle smile; " + BASE_REAL,
    ],
    "Лакшери интерьер": [
        "Man lounging on low velvet sofa in penthouse lounge, dark wood, brass accents, warm practical lamps, muted skyline; black knit polo, tailored trousers; " + BASE_REAL,
        "Full-body portrait beside grand bookshelf with art books, textured plaster wall, herringbone wood floor; cream cashmere sweater over shoulders; " + BASE_REAL,
        "Portrait at a private bar with backlit crystal, amber reflections on face, dark polo; hands around glass tumbler (no logo); " + BASE_REAL,
        "Man leaning on marble kitchen island, integrated lighting under cabinets, soft specular highlights; open collar shirt, sleeves rolled; " + BASE_REAL,
        "Seated in Eames lounge chair by window, leg crossed, watch detail sharp, city bokeh; " + BASE_REAL,
    ],
    "Casual город / улица": [
        "Street portrait near modern skyscrapers at blue hour, denim jacket over tee, subtle traffic bokeh, light drizzle sheen on asphalt; " + BASE_REAL,
        "Urban rooftop at sunset, wind in short hair, bomber jacket, minimal jewelry, muted skyline; " + BASE_REAL,
        "Concrete staircase with soft side light, monochrome palette, relaxed stance, hands in pockets; " + BASE_REAL,
        "Underpass with soft reflected light, techwear jacket, neon hints on wet ground; " + BASE_REAL,
        "Brick alleyway with shallow DOF, casual polo, gentle smile, authentic skin texture; " + BASE_REAL,
    ],
    "Спорт / улица": [
        "Athletic portrait on riverside promenade at dawn, track jacket half-zipped, cool mist, subtle breath in air; " + BASE_REAL,
        "Fitness look in minimalist gym, matte equipment, window light key, chalk dust particles; " + BASE_REAL,
        "Runner tying laces on city steps, early sunlight rim, motion-ready stance; " + BASE_REAL,
        "Casual bike near modern bridge, cross-body bag, wind ripples on water, soft highlights; " + BASE_REAL,
        "Outdoor portrait in city park, clean hoodie, realistic fabric folds, natural greenery bokeh; " + BASE_REAL,
    ],
}

# 80 женских — по стилям (в каждом 5–7)
WOMEN_CATALOG: Dict[str, List[str]] = {
    "Fashion editorial": [
        "Ultra-photorealistic portrait of a woman in a sunlit penthouse corner, silk blouse, tailored trousers, delicate gold earrings, soft backlight halo; " + BASE_REAL,
        "Editorial portrait against textured plaster wall, minimalist styling, linen blazer draped over shoulders, gentle wind in hair; " + BASE_REAL,
        "Runway-inspired pose near full-height window, monochrome outfit, subtle specular highlights on cheekbones; " + BASE_REAL,
        "Sitting on marble bench, pleated midi skirt, leather belt, hand on knee, soft side light; " + BASE_REAL,
        "Close-up beauty portrait with neutral makeup, fine baby hair flyaways retained, soft catchlights; " + BASE_REAL,
        "Standing beside sculpture pedestal, gallery ambiance, soft spot, shadows accurate; " + BASE_REAL,
        "Editorial three-quarter in hotel corridor, warm sconces, satin camisole under blazer; " + BASE_REAL,
    ],
    "Street style / город": [
        "Ultra-photorealistic portrait of a woman on a cobblestone street at golden hour, trench coat, crossbody bag, soft breeze; " + BASE_REAL,
        "City cafe terrace, latte on table, knit sweater, candid smile, bokeh pedestrians; " + BASE_REAL,
        "Rooftop sunset, denim jacket over white tee, hair lit from behind, skyline haze; " + BASE_REAL,
        "Underpass neon reflections on wet asphalt, oversized blazer, straight look to camera; " + BASE_REAL,
        "Crosswalk mid-step, light motion blur in background, pleated skirt, sunlight streaks; " + BASE_REAL,
    ],
    "Studio beauty": [
        "Ultra-photorealistic woman in studio, large softbox key, beauty dish fill, neutral grey seamless, natural skin texture, micro peach fuzz visible; " + BASE_REAL,
        "Tight headshot, glossy lip, mascara detail, tiny skin imperfections preserved, no over-smoothing; " + BASE_REAL,
        "Half-body seated on apple box, cotton tank, gentle shoulder highlight, subtle film grain; " + BASE_REAL,
        "Profile portrait, rim light outlining hair, matte background; " + BASE_REAL,
        "Three-quarter beauty shot, silk scarf around neck, gentle color gel accents; " + BASE_REAL,
        "Close crop of eyes and cheekbones, catchlight reflection, pores visible; " + BASE_REAL,
        "Studio portrait with negative fill on one side for depth; " + BASE_REAL,
    ],
    "Luxury interior": [
        "Woman in luxury living room, velvet sofa, brass floor lamp, marble coffee table with glass carafe, silk blouse, soft warm key; " + BASE_REAL,
        "Reading a book near panoramic window, city bokeh at dusk, knit dress, cozy but chic; " + BASE_REAL,
        "Standing by grand bookshelf, cashmere cardigan, delicate necklace, soft rim; " + BASE_REAL,
        "Sipping tea at marble kitchen island, pendant lights glowing, satin shirt; " + BASE_REAL,
        "Seated at piano in private salon, minimal jewelry, elegant posture; " + BASE_REAL,
        "By fireplace with stone surround, wool dress, warm practicals; " + BASE_REAL,
        "On balcony with subtle wind, tailored blazer over camisole, skyline haze; " + BASE_REAL,
    ],
    "Nature / сад": [
        "Woman in botanical garden, dappled sunlight through leaves, linen dress, true-to-life greens; " + BASE_REAL,
        "Meadow at golden hour, backlit hair strands glowing, flowy dress, authentic lens flare; " + BASE_REAL,
        "Forest path with soft fog, knit sweater, hands in pockets, grounded colors; " + BASE_REAL,
        "By lakeshore rocks, wind and water texture realistic, denim overshirt; " + BASE_REAL,
        "Among wildflowers, shallow DOF, natural freckles visible; " + BASE_REAL,
        "Wooden pier at sunset, long skirt, cardigan, gentle smile; " + BASE_REAL,
        "Orchard in bloom, basket with apples, cotton dress; " + BASE_REAL,
    ],
    "Travel / lifestyle": [
        "Old European street, stone facades, espresso in hand, trench coat, candid glance; " + BASE_REAL,
        "Hotel balcony view, silk robe, morning light, cup of coffee steam; " + BASE_REAL,
        "Airport lounge minimalism, carry-on suitcase, knit set, soft cool lighting; " + BASE_REAL,
        "Harbor promenade, linen set, sea breeze in hair, subdued colors; " + BASE_REAL,
        "Desert overlook at sunset, shawl blowing, warm tones; " + BASE_REAL,
        "Mountain viewpoint, puffer vest over sweater, rosy cheeks, crisp air; " + BASE_REAL,
        "Beach boardwalk at blue hour, light cardigan, natural tan, subtle highlights; " + BASE_REAL,
    ],
    "Fitness / wellness": [
        "Woman in clean boutique gym, matte dumbbells, window key light, seamless leggings and top, skin sheen realistic; " + BASE_REAL,
        "Yoga studio with wooden floor, warm sunlight stripes, balanced pose, barefoot; " + BASE_REAL,
        "Outdoor run at dawn, breathable jacket, mist, gentle breath visible; " + BASE_REAL,
        "Pilates reformer studio, neutral palette, tidy lines; " + BASE_REAL,
        "Stretching by large window, city haze backdrop; " + BASE_REAL,
    ],
    "Evening / party": [
        "Cocktail bar with amber backlight, satin slip dress, soft speculars on glassware, confident gaze; " + BASE_REAL,
        "Rooftop party blue hour, sequined blazer, hair moving in breeze, skyline bokeh; " + BASE_REAL,
        "Hotel corridor with warm sconces, little black dress, elegant stride; " + BASE_REAL,
        "Jazz lounge, velvet booth, martini glass, smokey ambience without haze; " + BASE_REAL,
        "Neon sign reflection in window, tailored suit set, cinematic shadows; " + BASE_REAL,
        "Private club library, dark wood, single lamp, pearl earrings; " + BASE_REAL,
        "Chandelier foyer, silk gown, realistic reflections on polished floor; " + BASE_REAL,
    ],
}

# подсчёт: 4 раздела мужчин (по 5) = 20, женщин 8 разделов (5–7 каждый) ≈ 80

# ================== LOG ==================
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg-bot")

# ================== STORAGE ==================
@dataclass
class UserState:
    id: int
    balance: int = 0
    has_model: bool = False
    job_id: Optional[str] = None
    model_id: Optional[str] = None
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

# ================== KEYBOARDS ==================
def kb_home(has_paid: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Попробовать", callback_data="try")],
        [InlineKeyboardButton("🖼 Генерации", callback_data="gen_menu")],
        [InlineKeyboardButton("👤 Мой аккаунт", callback_data="account")],
        [InlineKeyboardButton("🤝 Реферальная программа", callback_data="ref_menu")],
        [InlineKeyboardButton("🆘 Поддержка", callback_data="support")],
    ])

def kb_tariffs(discounted: bool = False) -> InlineKeyboardMarkup:
    def price(v): return int(round(v * 0.9)) if discounted else v
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"20 — {price(PRICES['20'])} ₽", callback_data="buy_20")],
        [InlineKeyboardButton(f"40 — {price(PRICES['40'])} ₽", callback_data="buy_40")],
        [InlineKeyboardButton(f"70 — {price(PRICES['70'])} ₽", callback_data="buy_70")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")],
    ])

def kb_upload_fixed() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Фото загружены", callback_data="photos_done")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
    ])

def kb_gender() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧔 Мужские разделы", callback_data="g:men")],
        [InlineKeyboardButton("👩 Женские разделы", callback_data="g:women")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="back_home")]
    ])

def kb_categories(gender: str) -> InlineKeyboardMarkup:
    cats = list(MEN_CATALOG.keys()) if gender == "men" else list(WOMEN_CATALOG.keys())
    rows = [[InlineKeyboardButton(f"{title}", callback_data=f"cat:{gender}:{title}") ] for title in cats]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="gen_menu")])
    return InlineKeyboardMarkup(rows)

def kb_prompts(gender: str, cat: str) -> InlineKeyboardMarkup:
    items = MEN_CATALOG[cat] if gender == "men" else WOMEN_CATALOG[cat]
    rows: List[List[InlineKeyboardButton]] = []
    for i, _ in enumerate(items):
        rows.append([InlineKeyboardButton(f"🎨 Вариант {i+1}", callback_data=f"p:{gender}:{cat}:{i}")])
    rows.append([InlineKeyboardButton("⬅️ Назад к разделам", callback_data=f"g:{gender}")])
    return InlineKeyboardMarkup(rows)

def kb_buy_or_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Купить генерации", callback_data="try")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
    ])

def kb_ref_menu(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Мои доходы", callback_data="ref_income")],
        [InlineKeyboardButton("👥 Мои рефералы", callback_data="ref_list")],
        [InlineKeyboardButton("💳 Вывести средства", callback_data="ref_payout")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
    ])

# ================== APP WRAPPER ==================
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

    # -------------- HANDLERS --------------
    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        st = get_user(u.id)

        # реф-код
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
            "👋 <b>Привет!</b> Это <b>PhotoFly</b> — персональная фотостудия с ИИ.\n\n"
            "1) Покупаешь пакет\n2) Загружаешь 15–50 фото\n3) Мы обучаем модель и выдаём реалистичные портреты по темам."
        )
        await update.effective_message.reply_text(text, reply_markup=kb_home(st.paid_any), parse_mode=ParseMode.HTML)

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        uid = q.from_user.id
        st = get_user(uid)
        data = q.data or ""

        # навигация
        if data == "back_home":
            await q.message.reply_text("📍 Главное меню", reply_markup=kb_home(st.paid_any))
            return

        if data == "try":
            discounted = bool(st.referred_by)
            if discounted:
                text = (
                    "💎 <b>Тарифы</b> <i>(скидка −10% по реф.ссылке)</i>\n\n"
                    f"• 20 — <s>{PRICES['20']} ₽</s> <b>{int(round(PRICES['20']*0.9))} ₽</b>\n"
                    f"• 40 — <s>{PRICES['40']} ₽</s> <b>{int(round(PRICES['40']*0.9))} ₽</b>\n"
                    f"• 70 — <s>{PRICES['70']} ₽</s> <b>{int(round(PRICES['70']*0.9))} ₽</b>"
                )
            else:
                text = (
                    "💎 <b>Тарифы</b>\n\n"
                    f"• 20 — <b>{PRICES['20']} ₽</b>\n"
                    f"• 40 — <b>{PRICES['40']} ₽</b>\n"
                    f"• 70 — <b>{PRICES['70']} ₽</b>"
                )
            await q.message.reply_text(text, reply_markup=kb_tariffs(discounted), parse_mode=ParseMode.HTML)
            return

        if data in ("buy_20", "buy_40", "buy_70"):
            qty = int(data.split("_")[1])
            price = PRICES[str(qty)]
            if st.referred_by: price = int(round(price * 0.9))
            st.balance += qty; st.paid_any = True; save_user(st)

            if st.referred_by:
                ref = get_user(st.referred_by)
                ref_gain = round(price * 0.20, 2)
                ref.ref_earn_total += ref_gain
                ref.ref_earn_ready += ref_gain
                save_user(ref)

            await q.message.reply_text(
                f"✅ Оплата прошла. Начислено: <b>{qty}</b> генераций.\n\n"
                "Теперь загрузи 15–50 фото (лучше 25–35). Когда закончишь — нажми «Фото загружены».",
                reply_markup=kb_upload_fixed(), parse_mode=ParseMode.HTML
            )
            return

        if data == "photos_done":
            await q.message.reply_text("🚀 Запускаем обучение. Сообщим, когда всё будет готово.")
            asyncio.create_task(self._launch_training_and_wait(uid, context))
            return

        if data == "gen_menu":
            if not st.paid_any:
                await q.message.reply_text("Сначала приобретите пакет.", reply_markup=kb_buy_or_back()); return
            if not st.has_model:
                await q.message.reply_text("⏳ Модель ещё обучается. Мы напишем, когда она будет готова."); return
            await q.message.reply_text("Выбери раздел:", reply_markup=kb_gender()); return

        if data.startswith("g:"):
            gender = data.split(":")[1]
            await q.message.reply_text(("🧔 Мужские разделы:" if gender=="men" else "👩 Женские разделы:"),
                                       reply_markup=kb_categories(gender)); return

        if data.startswith("cat:"):
            _, gender, cat = data.split(":", 2)
            await q.message.reply_text(f"Выбери стиль: {cat}", reply_markup=kb_prompts(gender, cat)); return

        if data.startswith("p:"):
            _, gender, cat, idx = data.split(":")
            idx = int(idx)
            prompt = (MEN_CATALOG if gender=="men" else WOMEN_CATALOG)[cat][idx]
            if st.balance < 3:
                await q.message.reply_text("Нет доступных генераций. Пополните баланс.", reply_markup=kb_buy_or_back()); return
            await q.message.reply_text("🎨 Генерируем 3 изображения… ~30–60 секунд.")
            try:
                imgs = await self._generate(uid, st.job_id, prompt, 3)
            except Exception:
                await context.bot.send_message(chat_id=uid, text="❌ Ошибка при генерации. Попробуйте ещё раз.")
                return
            st.balance -= 3; save_user(st)
            media = [InputMediaPhoto(imgs[0], caption=f"Готово! Баланс: {st.balance}")] + [InputMediaPhoto(u) for u in imgs[1:]]
            await context.bot.send_media_group(chat_id=uid, media=media)
            await context.bot.send_message(chat_id=uid, text="Ещё стиль?", reply_markup=kb_gender())
            return

        if data == "account":
            st = get_user(uid)
            text = (
                "👤 <b>Мой аккаунт</b>\n\n"
                f"ID: <code>{uid}</code>\n"
                f"Доступно генераций: <b>{st.balance}</b>\n"
                f"Модель обучена: <b>{'да' if st.has_model else 'нет'}</b>"
            )
            await q.message.reply_text(text, reply_markup=kb_home(st.paid_any), parse_mode=ParseMode.HTML); return

        if data == "support":
            await q.message.reply_text("🆘 Поддержка: @photofly_ai"); return

        if data == "ref_menu":
            link = f"https://t.me/{(await context.bot.get_me()).username}?start={get_user(uid).ref_code}"
            text = (
                "🤝 <b>Реферальная программа</b>\n"
                "• 20% с покупок друзей\n• Друзьям −10% на первый заказ\n\n"
                f"Твоя ссылка:\n<code>{link}</code>"
            )
            await q.message.reply_text(text, reply_markup=kb_ref_menu(uid), parse_mode=ParseMode.HTML); return

        if data == "ref_income":
            u = get_user(uid)
            await q.message.reply_text(
                f"📈 Всего: {u.ref_earn_total:.2f} ₽\nДоступно к выводу: {u.ref_earn_ready:.2f} ₽\nМинимум к выводу: 500 ₽",
                reply_markup=kb_ref_menu(uid)
            ); return

        if data == "ref_list":
            await q.message.reply_text("Список рефералов появится позже.", reply_markup=kb_ref_menu(uid)); return

        if data == "ref_payout":
            await q.message.reply_text("Для вывода напиши @photofly_ai (от 500 ₽).", reply_markup=kb_ref_menu(uid)); return

        if data == "buy_flash_50":
            st.balance += FLASH_OFFER["qty"]; st.paid_any = True; save_user(st)
            await q.message.reply_text(f"✅ Начислено {FLASH_OFFER['qty']} генераций за {FLASH_OFFER['price']} ₽.",
                                       reply_markup=kb_upload_fixed()); return

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Принимаем фото без ответных уведомлений."""
        uid = update.effective_user.id
        _ = get_user(uid)
        if not update.message.photo:  # защита
            return
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
            # молча игнорируем единичные сбои, чтобы не спамить
            pass

    # ---------- HELPERS ----------
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
                        st.model_id = model_id
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

        await context.bot.send_message(
            chat_id=uid,
            text=(
                "✨ <b>Модель обучена!</b>\n\n"
                "Теперь можно генерировать портреты. Сначала выбери раздел:"
            ),
            reply_markup=kb_gender(), parse_mode=ParseMode.HTML
        )

    async def _generate(self, uid: int, job_id: Optional[str], prompt: str, n: int) -> List[str]:
        body = {"user_id": str(uid), "prompt": prompt, "num_images": n}
        if job_id: body["job_id"] = job_id
        async with httpx.AsyncClient(timeout=240) as cl:
            r = await cl.post(f"{BACKEND_ROOT}/api/generate", json=body)
            r.raise_for_status()
            data = r.json()
            urls = data.get("images") or []
            if not urls:
                raise RuntimeError("empty images")
            return urls

    # ---------- FLASH OFFER SCHEDULER ----------
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
            await self.app.bot.send_message(chat_id=uid,
                text=f"🔥 Только сейчас! {FLASH_OFFER['qty']} генераций за {FLASH_OFFER['price']} ₽.",
                reply_markup=kb)
        except Exception:
            pass

# ========= ERRORS & LOGS =========
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    msg = str(getattr(context, "error", ""))
    if ("query is too old" in msg) or ("query ID is invalid" in msg) or ("response timeout expired" in msg):
        logging.getLogger("tg-bot").warning(f"Ignored old callback error: {msg}")
        return
    logging.getLogger("tg-bot").exception("Unhandled error in handler", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ Упс, произошла ошибка. Уже чиним.")
    except Exception:
        pass

async def log_any(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kind = ("callback_query" if update.callback_query else "message" if update.message else "channel_post" if update.channel_post else "other")
    uid = update.effective_user.id if update.effective_user else "-"
    logging.getLogger("tg-bot").info(f"Update: kind={kind} from={uid}")

# ========= EXPORT =========
tg_app = TgApp()
