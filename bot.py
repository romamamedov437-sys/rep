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
DATA_DIR = os.path.join("/opt/render/project/src", "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "users.json")
PHOTOS_TMP = os.path.join(DATA_DIR, "tg_tmp")
os.makedirs(PHOTOS_TMP, exist_ok=True)

PRICES = {"20": 429, "40": 590, "70": 719}

# ⚡ Акция через 24 часа после первого входа
FLASH_OFFER = {"qty": 50, "price": 390}  # 50 генераций — 390₽

# ================== PROMPTS (Ровно как ты прислал) ==================
# 40 for MEN, 250 for WOMEN (grouped by style/theme) — ключи локализованы в заголовках меню,
# сами строки промптов оставлены без изменений.

PROMPTS_SOURCE = {
    "men": {
        "business": [
            "A confident businessman standing in front of Moscow City skyscrapers, wearing a tailored navy blue suit, polished black shoes, and a luxury wristwatch, captured with a Canon EOS R5 and 85mm f/1.2 lens, golden hour lighting reflecting on glass towers, highly realistic photo with natural skin texture.",
            "A successful man walking in Wall Street, New York, holding a leather briefcase, dressed in a charcoal grey suit and silk tie, cinematic composition, shallow depth of field, realistic photo with professional studio lighting.",
            "A corporate executive posing inside a modern office with panoramic windows, background showing London skyline, sunlight streaming in, Leica SL2-S shot with 50mm f/1.4 lens, detailed skin tones, hyperrealistic style.",
            "A charismatic entrepreneur leaning on a luxury black car in Dubai, wearing a crisp white shirt, slim fit trousers, expensive shoes, captured with Sony A7R IV, sunset desert vibes, photorealism emphasized.",
            "A serious businessman working on a laptop in a rooftop lounge, Shanghai skyline in the background, soft evening light, shallow DOF, professional portrait with rich colors and cinematic tone."
        ],
        "fitness": [
            "A muscular man lifting weights in a modern luxury gym in Dubai, sweat glistening on skin, detailed muscle definition, shot with Canon EOS R5, 35mm lens, studio lighting, ultra-realistic.",
            "A runner training on a Moscow street during winter morning, breath visible in the cold air, wearing sportswear, shot on Sony A7R IV, cinematic tone, photorealistic capture.",
            "A man practicing yoga on a rooftop in New York, Manhattan skyline behind him, sunrise golden light, 50mm lens, cinematic composition, realistic atmosphere.",
            "A boxer in a dimly lit training ring, sweat dripping, veins visible, high-contrast dramatic lighting, ultra-detailed realistic photo, Leica SL2.",
            "A swimmer walking out of the pool in Dubai luxury sports complex, water dripping off body, reflections in the water, Canon EOS R5, natural look, cinematic detail."
        ],
        "luxury lifestyle": [
            "A stylish man relaxing on a luxury villa terrace in Bali, infinity pool behind him, wearing designer sunglasses and linen shirt, cinematic golden hour, ultra-realistic.",
            "A man posing with a Lamborghini Aventador in Monaco, wearing black tuxedo, city lights reflecting in the car paint, cinematic hyperrealism, Leica 90mm lens.",
            "A man sitting inside a private jet, drinking champagne, dressed in designer clothes, cinematic luxury shot with Sony A7R IV, detailed textures, photorealistic realism.",
            "A rich businessman holding a glass of whiskey inside a skyscraper penthouse in Dubai, background city lights blurred, ultra-detailed realistic photo, professional lighting.",
            "A young man showing off dollar bills in front of a Ferrari, nighttime city background, cinematic neon lighting, ultra-realistic shot."
        ],
        "travel": [
            "A man exploring the streets of Paris, Eiffel Tower visible in the distance, casual outfit, DSLR realistic photo, cinematic atmosphere.",
            "A man standing on Brooklyn Bridge, New York, sunset lighting, wearing a leather jacket, photorealistic image with shallow DOF.",
            "A man hiking in the Swiss Alps, snow-capped mountains behind, cinematic natural light, Canon EOS R5 capture.",
            "A man enjoying Turkish coffee in Istanbul with Hagia Sophia in the background, natural morning sunlight, ultra-realistic photo.",
            "A man standing on a yacht in the Mediterranean, wind blowing his hair, dressed in linen shirt, photorealistic cinematic capture."
        ],
        "studio portrait": [
            "A professional studio portrait of a man in a black suit, dark grey background, three-point lighting setup, hyperrealistic style with detailed textures.",
            "A cinematic close-up of a man with a beard, dramatic Rembrandt lighting, ultra-realistic capture.",
            "A man in traditional Arabic attire photographed in a studio with golden lighting, Canon EOS R5 85mm lens, cinematic hyperrealism.",
            "A classic black-and-white studio portrait of a man in white shirt, sharp contrast lighting, realistic detail.",
            "A headshot of a businessman in corporate attire, professional studio setup, ultra-detailed realistic photography."
        ]
    },
    "women": {
        "fashion": [
            "A glamorous young woman walking in New York’s Fifth Avenue, wearing a designer red dress, holding a Louis Vuitton bag, cinematic shot with Canon EOS R5, golden hour lighting, highly detailed realistic textures, natural makeup with glossy lips, long straight hair styled to perfection.",
            "A model posing in front of Moscow City skyscrapers, wearing black leather jacket, professional portrait shot with Sony A7R IV, dramatic cinematic lighting, natural skin textures, smoky eye makeup and bold accessories.",
            "A woman wearing elegant evening gown in Dubai Marina, city lights reflecting on the water, photorealistic cinematic shot with Leica camera, flawless makeup and sparkling jewelry, detailed hairstyle.",
            "A stylish woman in Paris posing under the Eiffel Tower, wearing beret and trench coat, ultra-realistic cinematic lighting, 50mm f/1.4 lens, fashionable handbag, soft glowing skin detail.",
            "A fashion portrait of a woman in Milan, standing near Duomo cathedral, wearing luxury clothes, photorealistic photography, styled hair, luxury earrings, glossy makeup finish."
        ],
        "beach": [
            "A woman in bikini on Maldives beach, turquoise ocean behind her, golden hour light, Canon EOS R5, photorealistic detail of skin and hair, wet hair effect, shining skin tones.",
            "A woman walking along Miami Beach at sunrise, holding sandals in hand, cinematic realism, Sony A7R IV capture, natural wind in her hair, minimal makeup, detailed sand textures.",
            "A model lying on a beach towel in Bali, palm trees swaying in the background, cinematic hyperrealism, Leica 50mm lens, stylish sunglasses and glowing tan skin.",
            "A woman in summer dress near the sea in Santorini, Greece, white buildings and blue domes behind her, cinematic lighting, long flowing hair, stylish jewelry, photorealistic texture.",
            "A woman posing in a luxury infinity pool overlooking ocean, reflections in water, photorealistic capture, shining wet hair, luxury gold necklace, hyperrealistic realism."
        ],
        "luxury lifestyle": [
            "A glamorous woman posing with a Rolls-Royce in Dubai, wearing a gold evening gown, cinematic neon lights reflecting, ultra-realistic, sparkling earrings and luxury diamond ring visible.",
            "A woman sitting inside a private jet, sipping champagne, dressed in luxury clothes, cinematic photorealism, high-end handbag on seat, detailed hair and makeup style.",
            "A rich woman standing in front of her villa in Los Angeles, palm trees in background, golden hour light, ultra-realistic detail, luxury car visible behind, glowing skin.",
            "A woman with Chanel bag walking near luxury yachts in Monaco, cinematic photography with shallow DOF, stylish high heels, elegant long hair blowing in wind.",
            "A female entrepreneur sitting at a penthouse balcony in New York, skyscrapers behind, cinematic night lights, ultra-realistic photo, designer dress and gold necklace visible."
        ],
        "fitness": [
            "A woman working out in luxury Dubai gym, sweat glistening on body, photorealistic ultra detail, tight sportswear, ponytail hair style, focused expression.",
            "A runner girl training in Central Park, New York, cinematic golden hour, photorealism, stylish sports bra and leggings, glowing skin detail.",
            "A yoga woman meditating on Bali cliff, ocean behind her, cinematic natural light, long braided hair, detailed realistic textures.",
            "A female boxer training in dim gym, cinematic dramatic light, ultra-realistic photo, toned muscles, intense focus, sweat dripping on skin.",
            "A swimmer walking out of pool in luxury sports complex, water dripping, photorealistic textures, slicked back wet hair, stylish sporty look."
        ],
        "party": [
            "A woman dancing in night club with neon lights, photorealistic cinematic vibe, shiny black dress, styled hair, realistic glowing skin.",
            "A glamorous girl posing with friends at rooftop party in Moscow, city lights behind, cinematic realism, holding champagne glass, makeup shining.",
            "A woman in red dress celebrating in Dubai luxury club, champagne, cinematic light, elegant hairstyle, luxury necklace.",
            "A stylish woman in New York bar, holding cocktail, cinematic realistic photography, detailed makeup and jewelry, hyperrealism.",
            "A young woman with balloons in luxury villa party, photorealistic style, stylish short dress, glitter makeup visible."
        ],
        "travel": [
            "A woman exploring Istanbul’s Grand Bazaar, colorful lights and carpets around, photorealistic cinematic capture, styled casual clothes, detailed skin textures.",
            "A woman standing on Brooklyn Bridge, sunset golden hour, cinematic photorealism, long curly hair blowing, detailed makeup and photorealistic capture.",
            "A female traveler with backpack in Swiss Alps, snow mountains behind, natural cinematic lighting, glowing skin, stylish outfit detail.",
            "A woman enjoying coffee in Paris street café, Eiffel Tower blurred behind, ultra-realistic cinematic shot, natural makeup and stylish hair.",
            "A woman on Venice gondola, romantic cinematic detail, hyperrealism, elegant summer dress, photorealistic detail."
        ],
        "studio portrait": [
            "A professional beauty portrait of a woman in white dress, studio setup with soft lighting, photorealistic skin detail, glossy lips, luxury earrings.",
            "A cinematic headshot of a woman with long hair, dramatic studio light, ultra-realistic, smoky eyes, glossy skin detail.",
            "A close-up of woman face with natural makeup, Canon EOS R5, 85mm f/1.2 lens, hyperrealistic detail, styled eyelashes and lips.",
            "A black-and-white portrait of woman in fashion pose, studio lighting, ultra-realistic textures, sharp cheekbone detail.",
            "A fashion studio portrait with cinematic colors, photorealistic photography, detailed hairstyle and glowing skin."
        ],
        "luxury cars": [
            "A glamorous woman posing next to a Lamborghini in Dubai, photorealistic cinematic detail, wearing luxury dress and heels.",
            "A woman leaning on a Ferrari in Monaco, golden hour lighting, stylish black dress, photorealistic hyperrealism.",
            "A stylish woman opening door of Rolls-Royce, cinematic lighting, luxury jewelry detail, photorealism.",
            "A woman standing near Porsche on Los Angeles street, cinematic photo realism, stylish outfit.",
            "A glamorous woman inside luxury car interior, photorealistic detail, expensive accessories visible."
        ],
        "villa lifestyle": [
            "A woman enjoying luxury villa in Bali, infinity pool view, photorealistic golden hour, stylish outfit, glowing skin detail.",
            "A woman relaxing in villa garden, cinematic sunlight, wearing summer dress, ultra-realistic photo.",
            "A glamorous woman sitting on villa balcony in Santorini, sea behind her, styled fashion detail.",
            "A woman posing with champagne near luxury villa pool, photorealistic textures, cinematic vibe.",
            "A stylish woman enjoying breakfast at luxury villa terrace, photorealistic morning light."
        ]
    }
}

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
