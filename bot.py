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

# ================== CONFIG ==================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
BACKEND_ROOT = (os.getenv("BACKEND_ROOT") or "").rstrip("/")
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").rstrip("/")
ADMIN_ID = int((os.getenv("ADMIN_ID") or "0").strip() or "0")

# Персистентное хранилище (Render): можно переопределить через DATA_DIR, по умолчанию /var/data
DATA_DIR = os.getenv("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "users.json")
PHOTOS_TMP = os.path.join(DATA_DIR, "tg_tmp")
os.makedirs(PHOTOS_TMP, exist_ok=True)

PRICES = {"20": 429, "40": 590, "70": 719}

# ⚡ Акция через 24 часа после первого входа
FLASH_OFFER = {"qty": 50, "price": 390}  # 50 генераций — 390₽

# ================== PROMPTS ==================
# Генератор реалистичных промптов: базовый «реализм» и шаблоны,
# затем вариативные дополнения + стилистические тэги по разделам.

BASE_REAL = (
    "ultra-realistic photographic portrait, natural skin texture preserved, "
    "subtle pores and tiny imperfections visible, accurate color science, "
    "no cartoonish artifacts, cinema-grade lighting, shallow depth of field, "
    "shot on full-frame camera, 50mm or 85mm prime lens, RAW development look"
)

CORE_TEMPLATES = [
    "Tight headshot, glossy lip, mascara detail, tiny skin imperfections preserved, no over-smoothing; ",
    "Half-body seated on apple box, cotton tank, gentle shoulder highlight, subtle film grain; ",
    "Profile portrait, rim light outlining hair, matte background; ",
    "Three-quarter beauty shot, silk scarf around neck, gentle color gel accents; ",
    "Editorial portrait, soft window light, catchlights visible in the eyes; ",
    "Studio clamshell lighting, beauty dish reflections, precise edge highlight; ",
    "Cinematic close-up, Rembrandt lighting triangle on cheek, moody background; ",
    "Outdoor golden hour, backlight flare, lifted shadows, authentic tones; ",
]

# Варианты техник/оптики/плёночности (мешаются для разнообразия)
TECH_VARIANTS = [
    "captured on Canon EOS R5 with 85mm f/1.2; ",
    "shot on Sony A7R IV with 50mm prime; ",
    "Leica SL2-S look, APO-Summicron 90mm micro-contrast; ",
    "medium-format vibe, razor-thin depth of field; ",
    "subtle Kodak Portra 400 film latitude; ",
    "neutral color grade, ACES-like response; ",
    "studio grade diffusion filter effect; ",
    "natural window softbox simulation; ",
]

def _mix_prompts(base_count: int, style_tags: List[str]) -> List[str]:
    """Собираем нужное количество промптов, комбинируя ядра + техники + стилистические тэги."""
    out: List[str] = []
    i = 0
    while len(out) < base_count:
        core = CORE_TEMPLATES[i % len(CORE_TEMPLATES)]
        tech = TECH_VARIANTS[i % len(TECH_VARIANTS)]
        tag = style_tags[i % len(style_tags)] if style_tags else ""
        prompt = f"{core}{tech}{tag}; {BASE_REAL}"
        out.append(prompt)
        i += 1
    return out

# Стилистические тэги по разделам (муж/жен). Сохраняем ваши группы и названия.
MEN_STYLE_TAGS = {
    "business": [
        "executive presence, tailored suit, polished shoes, subtle tie knot",
        "glass tower reflections, corporate office backdrop",
        "sleek boardroom, panoramic city skyline",
        "luxury watch close-up, cufflinks detail",
        "rooftop lounge, sunset over financial district",
    ],
    "fitness": [
        "athletic definition, sweat sheen, gym backplates",
        "boxing ring ropes bokeh, chalk dust particles",
        "outdoor run breath in cold air, motion blur tastefully",
        "yoga rooftop sunrise, balanced pose",
        "swimming pool water beads, wet hair detail",
    ],
    "luxury lifestyle": [
        "penthouse interior bokeh, city lights at night",
        "private jet cabin, champagne glass highlight",
        "supercar reflections, glossy paint",
        "villa terrace, infinity pool horizon",
        "marble textures, designer accessories",
    ],
    "travel": [
        "Eiffel Tower distant bokeh, Parisian street",
        "Brooklyn Bridge cables lines, sunset haze",
        "Swiss Alps snow caps, crisp air clarity",
        "Istanbul old town textures, morning light",
        "Mediterranean yacht deck, wind in hair",
    ],
    "studio portrait": [
        "dark gray seamless background, three-point lighting",
        "classic low-key portrait, high contrast edges",
        "black-and-white conversion, tonal richness",
        "headshot crop for corporate profile",
        "traditional attire, warm key light",
    ],
}

WOMEN_STYLE_TAGS = {
    "fashion": [
        "Fifth Avenue stride, designer dress flow",
        "Milan Duomo stones, editorial posture",
        "Parisian beret and trench, chic stance",
        "Dubai Marina neon reflections, evening glow",
        "glossy lips and subtle eyeliner, couture vibe",
    ],
    "beach": [
        "Maldives turquoise, wet hair sheen",
        "Miami sunrise walk, sand texture",
        "Bali palms sway, towel pattern",
        "Santorini whites and blues, breeze",
        "infinity pool mirror water, sun-kissed skin",
    ],
    "luxury lifestyle": [
        "Rolls-Royce grill reflection, gold gown",
        "private jet aisle, designer handbag",
        "LA villa palms, golden hour",
        "Monaco yachts, shallow DOF",
        "NYC penthouse balcony, city bokeh",
    ],
    "fitness": [
        "Dubai luxury gym, tight sportswear",
        "Central Park runner glow, motion hint",
        "Bali cliff yoga, ocean backdrop",
        "dim boxing gym, gritty rim light",
        "pool exit droplets, slicked hair",
    ],
    "party": [
        "neon club haze, reflective sequins",
        "rooftop party skyline, champagne",
        "Dubai lounge, warm amber lights",
        "NYC bar counter, glass highlights",
        "villa balloons, glitter makeup",
    ],
    "travel": [
        "Istanbul Grand Bazaar colors, textiles",
        "Brooklyn Bridge sunset, flowing hair",
        "Swiss Alps trek, crisp blue air",
        "Paris café cup steam, bistro chairs",
        "Venice gondola wake, romantic tone",
    ],
    "studio portrait": [
        "beauty dish catchlights, smooth gradient",
        "dramatic split light, smoky eye",
        "macro lashes detail, 85mm look",
        "BW fashion angle, sharp cheekbones",
        "cinematic palette, soft roll-off",
    ],
    "luxury cars": [
        "Lamborghini side panel gloss, stance",
        "Ferrari badge close-up, golden hour",
        "Rolls-Royce interior stitch detail",
        "Porsche street scene, clean lines",
        "driver seat portrait, dashboard glow",
    ],
    "villa lifestyle": [
        "Bali villa breakfast, morning sun",
        "garden dappled light, linen dress",
        "Santorini balcony rail, sea view",
        "poolside champagne, ripple highlights",
        "terrace wicker furniture, calm vibe",
    ],
}

# Распределяем количество промптов по разделам.
# Мужчины: 5 разделов * 8 = 40
MEN_COUNTS = {k: 8 for k in MEN_STYLE_TAGS.keys()}

# Женщины: всего 250. Сделаем 7 разделов по 28 и 2 раздела по 27 (28*7 + 27*2 = 250)
_women_keys = list(WOMEN_STYLE_TAGS.keys())
WOMEN_COUNTS: Dict[str, int] = {}
for i, k in enumerate(_women_keys):
    WOMEN_COUNTS[k] = 28 if i < 7 else 27

# Сборка источника промптов в прежней структуре (men/women -> категории -> список строк)
def _build_prompts_source() -> Dict[str, Dict[str, List[str]]]:
    men: Dict[str, List[str]] = {}
    for cat, tags in MEN_STYLE_TAGS.items():
        men[cat] = _mix_prompts(MEN_COUNTS[cat], tags)

    women: Dict[str, List[str]] = {}
    for cat, tags in WOMEN_STYLE_TAGS.items():
        women[cat] = _mix_prompts(WOMEN_COUNTS[cat], tags)

    return {"men": men, "women": women}

PROMPTS_SOURCE = _build_prompts_source()

# Локализация заголовков меню для категорий (русские названия + эмодзи)
MEN_TITLES = {
    "business": "💼 Бизнес / офис",
    "fitness": "🏃‍♂️ Фитнес / спорт",
    "luxury lifestyle": "🏙 Лакшери лайфстайл",
    "travel": "✈️ Путешествия",
    "studio portrait": "📷 Студийный портрет",
}
WOMEN_TITLES = {
    "fashion": "👗 Fashion / мода",
    "beach": "🏖 Пляж",
    "luxury lifestyle": "💎 Лакшери лайфстайл",
    "fitness": "🧘‍♀️ Фитнес / wellness",
    "party": "🎉 Вечеринка / вечер",
    "travel": "🧳 Путешествия",
    "studio portrait": "📸 Студийный портрет",
    "luxury cars": "🚗 Люкс-авто",
    "villa lifestyle": "🏡 Вилла / lifestyle",
}

# Строим каталоги для кнопок
MEN_CATALOG: Dict[str, List[str]] = {MEN_TITLES[k]: v for k, v in PROMPTS_SOURCE["men"].items()}
WOMEN_CATALOG: Dict[str, List[str]] = {WOMEN_TITLES[k]: v for k, v in PROMPTS_SOURCE["women"].items()}

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
        [InlineKeyboardButton("📸 Примеры", callback_data="examples")],
        [InlineKeyboardButton("🆘 Поддержка", callback_data="support")],
    ])

def kb_tariffs(discounted: bool = False) -> InlineKeyboardMarkup:
    def price(v): return int(round(v * 0.9)) if discounted else v
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"20 генераций — {price(PRICES['20'])} ₽", callback_data="buy_20")],
        [InlineKeyboardButton(f"40 генераций — {price(PRICES['40'])} ₽", callback_data="buy_40")],
        [InlineKeyboardButton(f"70 генераций — {price(PRICES['70'])} ₽", callback_data="buy_70")],
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
    rows: List[List[InlineKeyboardButton]] = []
    for title in cats:
        rows.append([InlineKeyboardButton(title, callback_data=f"cat:{gender}:{title}")])
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

def kb_examples() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Открыть канал с примерами", url="https://t.me/PhotoFly_Examples")],
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
        self.app.add_handler(CommandHandler("stats", self.on_stats))  # 🔹 админская статистика
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

        # Реф-код
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
            "1) Покупаешь пакет генераций\n"
            "2) Загружаешь 20–50 фото для обучения\n"
            "3) Получаешь реалистичные портреты по темам и стилям\n\n"
            "Нажми «🎯 Попробовать», чтобы выбрать тариф."
        )
        await update.effective_message.reply_text(text, reply_markup=kb_home(st.paid_any), parse_mode=ParseMode.HTML)

    async def on_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Админская статистика: /stats (ADMIN_ID)"""
        u = update.effective_user
        if not u or u.id != ADMIN_ID:
            return  # молча игнорируем не-админов
        try:
            users_count = len(DB)
            balances = sum((DB[k].get("balance", 0) or 0) for k in DB)
            models = sum(1 for k in DB if DB[k].get("has_model"))
            paid = sum(1 for k in DB if DB[k].get("paid_any"))
            ref_total = sum(float(DB[k].get("ref_earn_total", 0.0) or 0.0) for k in DB)
            ref_ready = sum(float(DB[k].get("ref_earn_ready", 0.0) or 0.0) for k in DB)
            oldest_ts = min((DB[k].get("first_seen_ts") or time.time()) for k in DB) if DB else time.time()
            uptime_days = (time.time() - oldest_ts) / 86400.0

            msg = (
                "📊 <b>Статистика</b>\n\n"
                f"Пользователей: <b>{users_count}</b>\n"
                f"Всего генераций на балансах: <b>{balances}</b>\n"
                f"Обученных моделей: <b>{models}</b>\n"
                f"Покупавших (paid_any): <b>{paid}</b>\n\n"
                f"Реф. начислено всего: <b>{ref_total:.2f} ₽</b>\n"
                f"Реф. к выводу: <b>{ref_ready:.2f} ₽</b>\n\n"
                f"Uptime (по первой регистрации): ~<b>{uptime_days:.2f}</b> дней"
            )
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.effective_message.reply_text(f"⚠️ Ошибка статистики: {e!r}")

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        uid = q.from_user.id
        st = get_user(uid)
        data = q.data or ""

        # Навигация
        if data == "back_home":
            await q.message.reply_text("📍 Главное меню", reply_markup=kb_home(st.paid_any))
            return

        if data == "examples":
            await q.message.reply_text(
                "📸 <b>Примеры работ</b>\n\nВдохновляйся готовыми результатами и выбирай свой стиль:",
                reply_markup=kb_examples(), parse_mode=ParseMode.HTML
            ); return

        if data == "try":
            discounted = bool(st.referred_by)
            if discounted:
                text = (
                    "💎 <b>Тарифы генераций</b> <i>(−10% по реферальной ссылке)</i>\n\n"
                    f"• 20 генераций — <s>{PRICES['20']} ₽</s> <b>{int(round(PRICES['20']*0.9))} ₽</b>\n"
                    f"• 40 генераций — <s>{PRICES['40']} ₽</s> <b>{int(round(PRICES['40']*0.9))} ₽</b>\n"
                    f"• 70 генераций — <s>{PRICES['70']} ₽</s> <b>{int(round(PRICES['70']*0.9))} ₽</b>\n\n"
                    "Выбирай пакет и начинаем!"
                )
            else:
                text = (
                    "💎 <b>Тарифы генераций</b>\n\n"
                    f"• 20 генераций — <b>{PRICES['20']} ₽</b>\n"
                    f"• 40 генераций — <b>{PRICES['40']} ₽</b>\n"
                    f"• 70 генераций — <b>{PRICES['70']} ₽</b>\n\n"
                    "Выбирай пакет и начинаем!"
                )
            await q.message.reply_text(text, reply_markup=kb_tariffs(discounted), parse_mode=ParseMode.HTML)
            return

        if data in ("buy_20", "buy_40", "buy_70"):
            qty = int(data.split("_")[1])
            price = PRICES[str(qty)]
            if st.referred_by:
                price = int(round(price * 0.9))

            st.balance += qty
            st.paid_any = True
            save_user(st)

            # Реф-начисления
            if st.referred_by:
                ref = get_user(st.referred_by)
                ref_gain = round(price * 0.20, 2)
                ref.ref_earn_total += ref_gain
                ref.ref_earn_ready += ref_gain
                save_user(ref)

            # Если модель уже есть — сразу в генерации (без просьбы загрузить фото)
            if st.has_model:
                await q.message.reply_text(
                    f"✅ Оплата прошла. Начислено: <b>{qty}</b> генераций.\n\n"
                    "Готово! Переходим к генерациям — выбери раздел:",
                    reply_markup=kb_gender(), parse_mode=ParseMode.HTML
                )
            else:
                # Нет модели — отправляем требования и кнопку «Фото загружены»
                await q.message.reply_text(
                    "✅ Оплата прошла. Начислено: <b>{qty}</b> генераций.\n\n"
                    "📥 <b>Требования к фото для обучения</b>\n"
                    "• От <b>20</b> до <b>50</b> фотографий (лучше 25–35)\n"
                    "• Разные ракурсы: фронтально, 3/4, профиль, разные фоны и освещение\n"
                    "• <b>Без</b> солнцезащитных очков, кепок/шапок, масок, сильных фильтров\n"
                    "• Реальная мимика: с улыбкой и нейтрально\n"
                    "• Чистые фото, без сильного шума и размытий\n\n"
                    "Когда закончишь — нажми «Фото загружены».",
                    reply_markup=kb_upload_fixed(), parse_mode=ParseMode.HTML
                )
            return

        if data == "photos_done":
            # Одна модель на аккаунт — повторно не запускаем
            if st.has_model:
                await q.message.reply_text(
                    "ℹ️ На аккаунте уже есть обученная модель.\n"
                    "Можем сразу перейти к генерациям:", reply_markup=kb_gender()
                )
                return
            await q.message.reply_text("🚀 Запускаем обучение. Сообщим, когда всё будет готово.")
            asyncio.create_task(self._launch_training_and_wait(uid, context))
            return

        if data == "gen_menu":
            if not st.paid_any:
                await q.message.reply_text("Сначала приобретите пакет.", reply_markup=kb_buy_or_back()); return
            if not st.has_model:
                await q.message.reply_text("⏳ Модель ещё обучается или не создана. Мы напишем, когда она будет готова."); return
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
            items = MEN_CATALOG[cat] if gender == "men" else WOMEN_CATALOG[cat]
            prompt = items[idx]
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
            await q.message.reply_text(
                "🆘 <b>Поддержка</b>\n\n"
                "• По вопросам оплаты и генераций: @photofly_ai\n"
                "• График: ежедневно 10:00–22:00 (МСК)\n\n"
                "Мы отвечаем быстро и по делу. Нажми «Назад», чтобы вернуться в меню.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]]),
                parse_mode=ParseMode.HTML
            ); return

        if data == "ref_menu":
            link = f"https://t.me/{(await context.bot.get_me()).username}?start={get_user(uid).ref_code}"
            text = (
                "🤝 <b>Реферальная программа</b>\n\n"
                "Приглашай друзей и получай:\n"
                "• <b>20%</b> с их покупок — на твой баланс (руб.)\n"
                "• Друзьям — <b>−10%</b> на первый заказ\n\n"
                "Твоя персональная ссылка:\n"
                f"<code>{link}</code>\n\n"
                "Размести её в сторис, чатах или отправь лично — начисления придут автоматически."
            )
            await q.message.reply_text(text, reply_markup=kb_ref_menu(uid), parse_mode=ParseMode.HTML); return

        if data == "ref_income":
            u = get_user(uid)
            await q.message.reply_text(
                "📈 <b>Мои доходы</b>\n\n"
                f"Всего начислено: <b>{u.ref_earn_total:.2f} ₽</b>\n"
                f"Доступно к выводу: <b>{u.ref_earn_ready:.2f} ₽</b>\n"
                "Минимальная сумма к выводу: <b>500 ₽</b>.\n\n"
                "Начисления поступают после каждой оплаты приглашённых пользователей.",
                reply_markup=kb_ref_menu(uid), parse_mode=ParseMode.HTML
            ); return

        if data == "ref_list":
            await q.message.reply_text(
                "👥 <b>Мои рефералы</b>\n\n"
                "Отображение списка в разработке. Пока доступна статистика доходов и ссылка.\n"
                "Продолжай делиться — это окупает генерации! ✨",
                reply_markup=kb_ref_menu(uid), parse_mode=ParseMode.HTML
            ); return

        if data == "ref_payout":
            await q.message.reply_text(
                "💳 <b>Вывести средства</b>\n\n"
                "• Доступный баланс: см. раздел «Мои доходы»\n"
                "• Минимум к выводу: <b>500 ₽</b>\n"
                "• Способ: перевод по реквизитам, уточняем в чате\n\n"
                "Напиши нашему оператору — оформим выплату:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗣 Написать в поддержку", url="https://t.me/photofly_ai")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
                ]),
                parse_mode=ParseMode.HTML
            ); return

        if data == "buy_flash_50":
            st.balance += FLASH_OFFER["qty"]
            st.paid_any = True
            save_user(st)
            # Если модель есть — сразу к генерациям
            if st.has_model:
                await q.message.reply_text(
                    f"✅ Начислено {FLASH_OFFER['qty']} генераций за {FLASH_OFFER['price']} ₽.\n\n"
                    "Переходим к генерациям — выбери раздел:",
                    reply_markup=kb_gender(), parse_mode=ParseMode.HTML
                )
            else:
                await q.message.reply_text(
                    f"✅ Начислено {FLASH_OFFER['qty']} генераций за {FLASH_OFFER['price']} ₽.\n\n"
                    "📥 <b>Требования к фото для обучения</b>\n"
                    "• От <b>20</b> до <b>50</b> фотографий (лучше 25–35)\n"
                    "• Разные ракурсы и сцены, различные освещения\n"
                    "• <b>Без</b> очков/кепок/масок, без сильных фильтров\n"
                    "• Файлы чистые и чёткие\n\n"
                    "Когда закончишь — нажми «Фото загружены».",
                    reply_markup=kb_upload_fixed(), parse_mode=ParseMode.HTML
                )
            return

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Принимаем фото молча (без уведомлений)."""
        uid = update.effective_user.id
        _ = get_user(uid)
        if not update.message.photo:
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
            # Молча игнорируем единичные сбои, чтобы не спамить
            pass

    # ---------- HELPERS ----------
    async def _launch_training_and_wait(self, uid: int, context: ContextTypes.DEFAULT_TYPE):
        st = get_user(uid)
        # Одна модель на аккаунт — пресекаем повторный запуск
        if st.has_model:
            await context.bot.send_message(chat_id=uid, text="ℹ️ Модель уже обучена. Переходим к генерациям:", reply_markup=kb_gender())
            return
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

        st.job_id = job_id
        save_user(st)

        status_url = f"{BACKEND_ROOT}/api/status/{job_id}"
        for _ in range(300):
            try:
                async with httpx.AsyncClient(timeout=30) as cl:
                    rr = await cl.get(status_url)
                    rr.raise_for_status()
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

    # ---------- FLASH OFFER SCHEDULER ----------
    async def _flash_offer_scheduler(self):
        """Через ~24 часа после первого входа — разовая акция 50 генераций за 390₽."""
        while True:
            now = time.time()
            try:
                for k, v in list(DB.items()):
                    st = UserState(**v)
                    if st.flash_sent:
                        continue
                    # 24 часа после первого взаимодействия
                    if now - (st.first_seen_ts or now) >= 24 * 3600:
                        await self._send_flash_offer(st.id)
                        st.flash_sent = True
                        save_user(st)
            except Exception:
                pass
            await asyncio.sleep(1800)

    async def _send_flash_offer(self, uid: int):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔥 Купить 50 генераций — {FLASH_OFFER['price']} ₽", callback_data="buy_flash_50")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
        ])
        try:
            await self.app.bot.send_message(
                chat_id=uid,
                text=(
                    "⚡ <b>Акция на 24 часа</b>\n\n"
                    f"Для вас подготовлено предложение: <b>{FLASH_OFFER['qty']} генераций</b> всего за "
                    f"<b>{FLASH_OFFER['price']} ₽</b>.\n\n"
                    "Успей воспользоваться и пополнить баланс выгодно!"
                ),
                reply_markup=kb, parse_mode=ParseMode.HTML
            )
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
