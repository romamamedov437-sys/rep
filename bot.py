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
# 🔁 Обновлено: 60 генераций за 390 ₽
FLASH_OFFER = {"qty": 60, "price": 390}  # 60 генераций — 390₽

# 🎯 Спец-офферы при исчерпании баланса
SPECIAL1 = {"qty": 60, "price": 329, "title": "60 генераций (Спец-оффер 1)"}
SPECIAL2 = {"qty": 100, "price": 419, "title": "100 генераций (Финальный оффер)"}

# ================== PROMPTS ==================
# Реализм без «пластика»: расширено — лучшее распознавание лица и правдоподобные фоны.
# NB: структура промптов не менялась: они всё так же собираются из фрейминга, тега стиля, света, оптики и RETREAL.
RETREAL = (
    "BASE REAL, ultra photorealistic aesthetic, natural color science, cinematic tonal range, "
    "precise facial landmark alignment, high-fidelity facial features (sharp eyes, detailed iris catchlights, "
    "well-defined eyelashes, realistic eyebrows), micro-texture skin with preserved pores and fine peach fuzz, "
    "subtle skin retouch (~50%) with even gradients, no plastic smoothing, "
    "true-to-life hair strands and flyaways, natural lip speculars, "
    "physically-plausible background rendering (realistic interiors/streets/nature, depth layering, lens-true bokeh), "
    "balanced contrast, gentle film-like grain"
)

OPTICS = [
    "full-frame prime 50mm", "full-frame prime 85mm", "studio 90mm macro look",
    "medium-format shallow depth", "neutral ACES-like grade", "soft diffusion filter",
    "window softbox simulation", "film-like gentle grain"
]
LIGHT = [
    "soft window light", "Rembrandt key light", "clamshell beauty light",
    "cinematic rim light", "golden hour backlight", "studio three-point light"
]

# ====== МУЖЧИНЫ (40) ======
MEN_STYLE_TAGS = {
    "business": [
        "adult male, masculine features, clean shave or short beard, tailored suit, tie/cufflinks",
        "male executive aura, corporate office backdrop, glass reflections",
        "male portrait, boardroom, skyline in background, luxury watch detail",
        "masculine posture, rooftop lounge near financial district",
        "male model, monochrome socks & polished oxford shoes subtle",
    ],
    "fitness": [
        "athletic adult male, defined musculature, sweat sheen, gym background",
        "male boxer stance, wraps visible, gritty ambience",
        "male runner outdoors, visible breath in cold air",
        "male yoga pose on rooftop at sunrise",
        "male swimmer exiting pool, water droplets, wet hair",
    ],
    "luxury lifestyle": [
        "male in penthouse, night city bokeh, whiskey glass",
        "adult male inside private jet, designer outfit",
        "male with supercar, glossy paint reflections",
        "male at villa terrace with infinity pool",
        "male entrepreneur on balcony with skyline",
    ],
    "travel": [
        "male tourist in Paris street, Eiffel bokeh",
        "male on Brooklyn Bridge at sunset",
        "male hiker in Swiss Alps, snow peaks",
        "male enjoying coffee in Istanbul, morning light",
        "male on yacht deck, Mediterranean wind",
    ],
    "studio portrait": [
        "male head & shoulders on dark seamless, crisp edge light",
        "male classic low-key portrait, high contrast",
        "male BW studio, strong jawline definition",
        "male corporate headshot frame",
        "male traditional attire, warm key",
    ],
}
# 👇 Обновлён фрейминг: чаще по пояс/¾/полный рост + анти-кроп, но лицо сохраняем чётким
MEN_FRAMING = [
    "head-and-shoulders portrait, chest-up, full head visible, no tight face crop, eyes in upper third, vertical 4:5, realistic background elements present",
    "half-body portrait (mid-shot), waist-up, head and shoulders fully in frame, natural hand placement, avoid extreme close-up, vertical 4:5 or 3:4, maintain facial sharpness",
    "three-quarter body portrait, knees-up, full head in frame, no forehead/chin crop, vertical 3:4, balanced perspective, face remains crisp with catchlights",
    "full-body fashion shot, head-to-toe visible including footwear, subject entirely inside frame, vertical 9:16 or 4:5, avoid cropping at ankles, retain eye detail and natural proportions"
]
def _build_men_prompts() -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for cat, tags in MEN_STYLE_TAGS.items():
        items: List[str] = []
        i = 0
        while len(items) < 8:
            t = tags[i % len(tags)]
            f = MEN_FRAMING[i % len(MEN_FRAMING)]
            l = LIGHT[i % len(LIGHT)]
            o = OPTICS[i % len(OPTICS)]
            prompt = (
                f"{f}, {t}, {l}, {o}, {RETREAL}. "
                "male subject only, masculine styling, no female figure."
            )
            items.append(prompt)
            i += 1
        out[cat] = items
    return out

# ====== ЖЕНЩИНЫ (250) ======
WOMEN_STYLE_TAGS = {
    "fashion": [
        "female fashion model, couture vibe, runway poise",
        "editorial female pose near Duomo/Milan",
        "Paris street chic, trench and beret (female)",
        "Dubai Marina evening glamour (female)",
        "female beauty accents: glossy lips, subtle eyeliner",
    ],
    "beach": [
        "female at Maldives shoreline, wet hair sheen",
        "female walk at Miami sunrise, sand texture",
        "female on Bali towel, palms swaying",
        "female in Santorini whites and blues",
        "female in infinity pool, sun-kissed skin",
    ],
    "luxury lifestyle": [
        "female with Rolls-Royce, evening gown",
        "female inside private jet, designer handbag",
        "female at LA villa, gold hour",
        "female near Monaco yachts, shallow DOF",
        "female on NYC penthouse balcony, city bokeh",
    ],
    "fitness": [
        "female in Dubai luxury gym, tight sportswear",
        "female runner in Central Park, motion hint",
        "female yoga on Bali cliff, ocean backdrop",
        "female boxer in dim gym, gritty rim light",
        "female exiting pool, slicked hair",
    ],
    "party": [
        "female in neon club haze, reflective sequins",
        "female at rooftop party, champagne",
        "female in Dubai lounge, warm amber lights",
        "female at NYC bar counter, glass highlights",
        "female villa party, glitter makeup",
    ],
    "travel": [
        "female at Istanbul Grand Bazaar, textiles",
        "female on Brooklyn Bridge at sunset",
        "female in Swiss Alps trek, crisp air",
        "female at Paris café, bistro ambiance",
        "female on Venice gondola, romantic tone",
    ],
    "studio portrait": [
        "female beauty dish catchlights, smooth gradient",
        "female dramatic split light, smoky eye",
        "female macro lashes detail, 85mm look",
        "female BW fashion angle, cheekbones",
        "female cinematic palette, soft roll-off",
    ],
    "luxury cars": [
        "female near Lamborghini gloss panel",
        "female near Ferrari badge at golden hour",
        "female in Rolls interior stitch detail",
        "female with Porsche street scene",
        "female in car interior, dashboard glow",
    ],
    "villa lifestyle": [
        "female Bali villa breakfast, morning sun",
        "female garden dappled light, linen dress",
        "female on Santorini balcony, sea view",
        "female poolside champagne, ripples",
        "female terrace wicker furniture, calm",
    ],
}
# 👇 Обновлён фрейминг: фокус на по пояс / ¾ / полный рост + анти-кроп, с приоритетом резкого лица
WOMEN_FRAMING = [
    "head-and-shoulders, chest-up, full head visible, no tight face crop, eyes in upper third, vertical 4:5, realistic background retained",
    "half-body (mid-shot), waist-up, natural hand placement, full head in frame, avoid extreme close-up, vertical 4:5 or 3:4, maintain facial sharpness and catchlights",
    "three-quarter body, knees-up, elegant posture, full head in frame, no forehead/ankle crop, vertical 3:4, crisp facial detail preserved",
    "full-body fashion shot, head-to-toe visible including footwear, subject fully inside frame, vertical 9:16 or 4:5, avoid ankle crop, keep eyes and face well-defined"
]
def _women_counts():
    keys = list(WOMEN_STYLE_TAGS.keys())
    counts: Dict[str, int] = {}
    for i, k in enumerate(keys):
        counts[k] = 28 if i < 7 else 27
    return counts
def _build_women_prompts() -> Dict[str, List[str]]:
    counts = _women_counts()
    out: Dict[str, List[str]] = {}
    for cat, tags in WOMEN_STYLE_TAGS.items():
        need = counts[cat]
        items: List[str] = []
        i = 0
        while len(items) < need:
            t = tags[i % len(tags)]
            f = WOMEN_FRAMING[i % len(WOMEN_FRAMING)]
            l = LIGHT[i % len(LIGHT)]
            o = OPTICS[i % len(OPTICS)]
            prompt = (
                f"{f}, {t}, {l}, {o}, {RETREAL}. "
                "female subject only, feminine styling, no male figure."
            )
            items.append(prompt)
            i += 1
        out[cat] = items
    return out

PROMPTS_SOURCE = {
    "men": _build_men_prompts(),
    "women": _build_women_prompts(),
}

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
    gender_pref: Optional[str] = None
    # 🔖 учёт спец-покупок
    bought_spec1: bool = False
    bought_spec2: bool = False
    purchases: Dict[str, str] = field(default_factory=dict)  # payment_id -> "spec1"|"spec2"|...

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

# ================== KEYБОАРДЫ ==================
def kb_home(has_paid: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Попробовать", callback_data="try")],
        [InlineKeyboardButton("🖼 Генерации", callback_data="gen_menu")],
        [InlineKeyboardButton("👤 Мой аккаунт", callback_data="account")],
        [InlineKeyboardButton("👯‍♀️ Поделись ссылкой с подругой — и получи 20% кэшбэка!", callback_data="ref_menu")],
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
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_home")])
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

def kb_pay_actions(payment_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Я оплатил(а)", callback_data=f"paycheck:{payment_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
    ])

def kb_special_buy(tag: str, title: str, price: int) -> InlineKeyboardMarkup:
    # tag: "spec1"|"spec2"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Купить — {title} за {price} ₽", callback_data=f"buy_{tag}")],
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

        # 🔥 Продающий текст на стартовом экране
        text = (
            "👋 <b>Привет!</b> Это <b>PhotoFly</b> — твоя персональная фотостудия с ИИ.\n\n"
            "✨ Создавай <b>реалистичные портреты</b> за минуту прямо в Telegram.\n"
            "Без студии и фотографа — всё в один клик.\n\n"
            "<b>Что получишь:</b>\n"
            "• 🌟 Живые эмоции и естественный свет\n"
            "• 💎 Качественный результат без «пластика»\n"
            "• 🔥 Десятки уникальных сценариев съёмки\n\n"
            "Выбирай пакет — и полетели! Нажми «🎯 Попробовать»."
        )
        await update.effective_message.reply_text(text, reply_markup=kb_home(st.paid_any), parse_mode=ParseMode.HTML)

    async def on_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Админская статистика: /stats (ADMIN_ID)"""
        u = update.effective_user
        if not u or u.id != ADMIN_ID:
            return
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

    async def _start_payment(self, uid: int, qty: int, amount_rub: int, title: str):
        """Создаём платёж через backend, получаем ссылку и показываем пользователю."""
        try:
            async with httpx.AsyncClient(timeout=30) as cl:
                r = await cl.post(f"{BACKEND_ROOT}/api/pay", json={
                    "user_id": int(uid),
                    "qty": int(qty),
                    "amount": int(amount_rub),
                    "title": str(title)
                })
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            return None, f"❌ Ошибка инициализации оплаты: {e!r}"
        url = data.get("confirmation_url")
        pid = data.get("payment_id")
        if not url or not pid:
            return None, "❌ Не удалось получить ссылку на оплату."
        return (url, pid), None

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        uid = q.from_user.id
        st = get_user(uid)
        data = q.data or ""

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
                    "Выбирай пакет и оформляй оплату — генерации начислим сразу после подтверждения."
                )
            else:
                text = (
                    "💎 <b>Тарифы генераций</b>\n\n"
                    f"• 20 генераций — <b>{PRICES['20']} ₽</b>\n"
                    f"• 40 генераций — <b>{PRICES['40']} ₽</b>\n"
                    f"• 70 генераций — <b>{PRICES['70']} ₽</b>\n\n"
                    "Выбирай пакет и оформляй оплату — генерации начислим сразу после подтверждения."
                )
            await q.message.reply_text(text, reply_markup=kb_tariffs(bool(st.referred_by)), parse_mode=ParseMode.HTML)
            return

        if data in ("buy_20", "buy_40", "buy_70"):
            qty = int(data.split("_")[1])
            base_price = PRICES[str(qty)]
            price = int(round(base_price * 0.9)) if st.referred_by else base_price
            info, err = await self._start_payment(uid, qty, price, f"{qty} генераций")
            if err:
                await q.message.reply_text(err); return
            pay_url, pid = info
            await q.message.reply_text(
                f"🧾 К оплате: <b>{price} ₽</b>\nПакет: <b>{qty}</b> генераций.\n\n"
                "Нажми «Оплатить», затем «✅ Я оплатил(а)» для проверки.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Оплатить", url=pay_url)],
                    [InlineKeyboardButton("✅ Я оплатил(а)", callback_data=f"paycheck:{pid}")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
                ]),
                parse_mode=ParseMode.HTML
            )
            return

        if data == "photos_done":
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
            if not st.paid_any and st.balance <= 0:
                # при нуле — показываем обычное меню покупки
                await q.message.reply_text("Сначала приобретите пакет.", reply_markup=kb_buy_or_back()); return
            if not st.has_model:
                await q.message.reply_text("⏳ Модель ещё обучается или не создана. Мы напишем, когда она будет готова."); return
            if st.gender_pref in ("men", "women"):
                await q.message.reply_text("Выберите стиль:", reply_markup=kb_categories(st.gender_pref)); return
            await q.message.reply_text("Выбери раздел:", reply_markup=kb_gender()); return

        if data.startswith("g:"):
            gender = data.split(":")[1]
            st.gender_pref = gender
            save_user(st)
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
                # 🔔 Баланс исчерпан — показываем спец-офферы
                if not st.bought_spec1:
                    msg = (
                        "⚠️ <b>Генерации закончились.</b>\n\n"
                        "Специальное предложение только для вас:\n"
                        f"• <b>{SPECIAL1['qty']} генераций — {SPECIAL1['price']} ₽</b>\n\n"
                        "Нажмите «Купить», генерации начислим сразу после подтверждения."
                    )
                    await q.message.reply_text(
                        msg, reply_markup=kb_special_buy("spec1", f"{SPECIAL1['qty']} генераций", SPECIAL1["price"]),
                        parse_mode=ParseMode.HTML
                    ); return
                elif not st.bought_spec2:
                    msg = (
                        "⚠️ <b>Генерации закончились.</b>\n\n"
                        "Такого предложения больше не будет:\n"
                        f"• <b>{SPECIAL2['qty']} генераций — {SPECIAL2['price']} ₽</b>\n\n"
                        "Нажмите «Купить», генерации начислим сразу после подтверждения."
                    )
                    await q.message.reply_text(
                        msg, reply_markup=kb_special_buy("spec2", f"{SPECIAL2['qty']} генераций", SPECIAL2["price"]),
                        parse_mode=ParseMode.HTML
                    ); return
                else:
                    await q.message.reply_text("Нет доступных генераций. Пополните баланс.", reply_markup=kb_buy_or_back()); return

            await q.message.reply_text("🎨 Генерируем 3 изображения… ~30–60 секунд.")
            try:
                imgs = await self._generate(uid, st.job_id, prompt, 3)
            except Exception:
                await context.bot.send_message(chat_id=uid, text="❌ Ошибка при генерации. Попробуйте ещё раз.")
                return
            st.balance -= 3; save_user(st)
            media = [InputMediaPhoto(imgs[0], caption=f"Готово! Списано: 3. Остаток: <b>{st.balance}</b>", parse_mode=ParseMode.HTML)] + [InputMediaPhoto(u) for u in imgs[1:]]
            await context.bot.send_media_group(chat_id=uid, media=media)
            if st.gender_pref in ("men", "women"):
                await context.bot.send_message(chat_id=uid, text="Ещё стиль?", reply_markup=kb_categories(st.gender_pref))
            else:
                await context.bot.send_message(chat_id=uid, text="Ещё стиль?", reply_markup=kb_gender())
            return

        # —— спец-офферы покупки
        if data in ("buy_spec1", "buy_spec2"):
            spec = SPECIAL1 if data.endswith("spec1") else SPECIAL2
            info, err = await self._start_payment(uid, spec["qty"], spec["price"], spec["title"])
            if err:
                await q.message.reply_text(err); return
            pay_url, pid = info
            # привяжем тип покупки к payment_id, чтобы по подтверждению отметить покупку корректно
            st.purchases[pid] = "spec1" if data.endswith("spec1") else "spec2"
            save_user(st)
            await q.message.reply_text(
                f"🧾 К оплате: <b>{spec['price']} ₽</b>\nПакет: <b>{spec['qty']}</b> генераций.\n\n"
                "Нажми «Оплатить», затем «✅ Я оплатил(а)» для проверки.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Оплатить", url=pay_url)],
                    [InlineKeyboardButton("✅ Я оплатил(а)", callback_data=f"paycheck:{pid}")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")],
                ]),
                parse_mode=ParseMode.HTML
            ); return

        if data.startswith("paycheck:"):
            payment_id = data.split(":", 1)[1]
            try:
                async with httpx.AsyncClient(timeout=20) as cl:
                    r = await cl.get(f"{BACKEND_ROOT}/api/pay/status", params={"payment_id": payment_id})
                    r.raise_for_status()
                    d = r.json()
            except Exception:
                await q.message.reply_text("⏳ Платёж ещё не подтверждён. Попробуйте через минуту."); return

            status = (d.get("status") or "").lower()
            if status != "succeeded":
                await q.message.reply_text("⏳ Платёж ещё не подтверждён. Попробуйте позже."); return

            # отметим, что это был спец-оффер (если да)
            st = get_user(uid)
            tag = (st.purchases or {}).pop(payment_id, None)
            if tag == "spec1":
                st.bought_spec1 = True
            elif tag == "spec2":
                st.bought_spec2 = True
            save_user(st)

            await q.message.reply_text(
                f"✅ Платёж подтверждён. Текущий баланс: <b>{st.balance}</b>.",
                parse_mode=ParseMode.HTML
            )
            if st.has_model:
                if st.gender_pref in ("men", "women"):
                    await q.message.reply_text("Выберите стиль:", reply_markup=kb_categories(st.gender_pref))
                else:
                    await q.message.reply_text("Выберите раздел:", reply_markup=kb_gender())
            else:
                await q.message.reply_text(
                    "📥 <b>Требования к фото для обучения</b>\n"
                    "• 20–50 фотографий (лучше 25–35)\n"
                    "• Разные ракурсы/фоны/освещение\n"
                    "• Без очков/кепок/масок/сильных фильтров\n\n"
                    "Когда закончишь — нажми «Фото загружены».",
                    reply_markup=kb_upload_fixed(), parse_mode=ParseMode.HTML
                )
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
                f"{link}\n\n"
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
            refs: List[Dict[str, Any]] = []
            for k, v in DB.items():
                try:
                    if int(v.get("referred_by") or 0) == uid:
                        refs.append({
                            "id": int(k),
                            "paid": bool(v.get("paid_any")),
                            "balance": int(v.get("balance") or 0),
                            "first_seen_ts": float(v.get("first_seen_ts") or 0.0),
                        })
                except Exception:
                    continue
            refs.sort(key=lambda x: x.get("first_seen_ts") or 0, reverse=True)

            if not refs:
                text = (
                    "👥 <b>Мои рефералы</b>\n\n"
                    "Пока приглашённых нет.\n\n"
                    "Продолжай делиться — это окупает генерации! ✨"
                )
            else:
                lines = [f"👥 <b>Мои рефералы</b>\n", f"Всего: <b>{len(refs)}</b>\n"]
                for i, r in enumerate(refs[:50], 1):
                    status = "оплатил(а)" if r["paid"] else "без оплаты"
                    lines.append(f"{i}. <code>{r['id']}</code> — {status}, баланс: {r['balance']}")
                lines.append("\nПродолжай делиться — это окупает генерации! ✨")
                text = "\n".join(lines)
            await q.message.reply_text(text, reply_markup=kb_ref_menu(uid), parse_mode=ParseMode.HTML); return

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

        # 🔁 Акция через 24 часа: 60 генераций за 390 ₽
        if data == "buy_flash_60":
            qty = FLASH_OFFER["qty"]
            price = FLASH_OFFER["price"]
            info, err = await self._start_payment(uid, qty, price, f"{qty} генераций (Акция 24ч)")
            if err:
                await q.message.reply_text(err); return
            pay_url, pid = info
            await q.message.reply_text(
                f"🧾 К оплате: <b>{price} ₽</b>\nПакет: <b>{qty}</b> генераций.\n\n"
                "Нажми «Оплатить», затем «✅ Я оплатил(а)» для проверки.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💳 Оплатить", url=pay_url)],
                    [InlineKeyboardButton("✅ Я оплатил(а)", callback_data=f"paycheck:{pid}")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
                ]),
                parse_mode=ParseMode.HTML
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
            pass

    # ---------- HELPERS ----------
    async def _launch_training_and_wait(self, uid: int, context: ContextTypes.DEFAULT_TYPE):
        st = get_user(uid)
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
        """Через ~24 часа после первого входа — разовая акция 60 генераций за 390₽."""
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
        # клавиатура акции
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔥 Купить 60 генераций — {FLASH_OFFER['price']} ₽", callback_data="buy_flash_60")],
            [InlineKeyboardButton("📸 Канал с примерами", url="https://t.me/PhotoFly_Examples")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
        ])
        try:
            await self.app.bot.send_message(
                chat_id=uid,
                text=(
                    "⏳ <b>Только 24 часа!</b>\n\n"
                    f"Специально для вас — <b>{FLASH_OFFER['qty']} генераций</b> всего за <b>{FLASH_OFFER['price']} ₽</b>.\n"
                    "Идеально, чтобы протестировать больше стилей и ракурсов.\n\n"
                    "Посмотреть результаты других можно в нашем канале с примерами: "
                    "https://t.me/PhotoFly_Examples"
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
