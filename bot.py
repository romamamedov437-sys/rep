# bot.py
import os
import json
import time
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple

import httpx
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes,
    CallbackQueryHandler, MessageHandler, CommandHandler, filters
)

# ================== CONFIG ==================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
BACKEND_ROOT = (os.getenv("BACKEND_ROOT") or "").rstrip("/")  # напр., https://rep-wug0.onrender.com
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").rstrip("/")      # твой публичный базовый URL (необязательно)
DATA_DIR = os.path.join("/opt/render/project/src", "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "users.json")
PHOTOS_TMP = os.path.join(DATA_DIR, "tg_tmp")
os.makedirs(PHOTOS_TMP, exist_ok=True)

# цены (руб.)
PRICES = {
    "20": 429, "40": 590, "70": 719
}
# спец-оффер через 24h
FLASH_OFFER = {"qty": 50, "price": 379}

# промпты (название -> текст для генерации)
PROMPTS: Dict[str, str] = {
    "p_ny": "portrait photo in New York city street, urban candid, shallow depth of field, realistic lighting",
    "p_moscow": "portrait at Moscow-City skyline, modern architecture background, cinematic lighting",
    "p_studio_soft": "studio headshot, soft light, beauty dish, professional portrait photography, high detail",
    "p_golden_hour": "outdoor portrait at golden hour, warm sunlight, backlit hair, natural bokeh",
    "p_euro_casual": "european old town casual street, cobblestone, soft overcast light, lifestyle portrait",
    "p_business": "corporate business headshot, neutral background, clean lighting, professional attire",
    "p_nature": "portrait in forest clearing, soft diffused light, greenery, airy and fresh",
    "p_cyber": "futuristic cyberpunk portrait, neon lights, rain reflections, moody cinematic"
}

# ================== STORAGE ==================
@dataclass
class UserState:
    id: int
    balance: int = 0
    has_model: bool = False
    job_id: Optional[str] = None
    uploads: List[str] = field(default_factory=list)  # (мы храним просто имена/пути — опционально)
    referred_by: Optional[int] = None
    ref_code: Optional[str] = None
    ref_earn_total: float = 0.0
    ref_earn_ready: float = 0.0
    first_seen_ts: float = field(default_factory=lambda: time.time())
    flash_sent: bool = False           # отправляли ли оффер 24h
    paid_any: bool = False             # совершал ли покупки когда-либо

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
    # восстановим dataclass
    st = UserState(**s)
    return st

def save_user(st: UserState) -> None:
    DB[str(st.id)] = st.__dict__
    _save_db(DB)

# ================== TELEGRAM APP WRAPPER ==================
class TgApp:
    def __init__(self):
        self.app: Optional[Application] = None
        self._bg_tasks: List[asyncio.Task] = []

    async def initialize(self):
        logging.getLogger("tg-bot").setLevel(logging.INFO)
        if not BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN not set")
        # ВАЖНАЯ ПРАВКА: работаем без Updater (вебхуки)
        self.app = ApplicationBuilder().token(BOT_TOKEN).updater(None).build()

        self.app.add_handler(CommandHandler("start", self.on_start))
        self.app.add_handler(CallbackQueryHandler(self.on_button))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))

    async def start(self):
        assert self.app
        await self.app.start()
        # запустим «планировщик» для 24h оффера
        self._bg_tasks.append(asyncio.create_task(self._flash_offer_scheduler()))

    async def stop(self):
        assert self.app
        for t in self._bg_tasks:
            t.cancel()
        await self.app.stop()

    async def process_update(self, update: Update):
        assert self.app
        await self.app.process_update(update)

    # -------------- UI BUILDERS --------------
    def kb_home(self, has_paid: bool = False) -> InlineKeyboardMarkup:
        buttons = [
            [InlineKeyboardButton("🎯 Попробовать", callback_data="try")],
            [InlineKeyboardButton("🖼 Генерации", callback_data="gen_menu")],
            [InlineKeyboardButton("📸 Примеры", callback_data="examples")],
            [InlineKeyboardButton("🤝 Реферальная программа", callback_data="ref_menu")],
            [InlineKeyboardButton("🆘 Поддержка", callback_data="support")],
        ]
        return InlineKeyboardMarkup(buttons)

    def kb_tariffs(self, discounted: bool = False) -> InlineKeyboardMarkup:
        def price(v): return int(round(v * 0.9)) if discounted else v
        buttons = [
            [InlineKeyboardButton(f"20 генераций — {price(PRICES['20'])} ₽", callback_data="buy_20")],
            [InlineKeyboardButton(f"40 генераций — {price(PRICES['40'])} ₽", callback_data="buy_40")],
            [InlineKeyboardButton(f"70 генераций — {price(PRICES['70'])} ₽", callback_data="buy_70")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")],
        ]
        return InlineKeyboardMarkup(buttons)

    def kb_upload_fixed(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Фото загружены", callback_data="photos_done")],
                                     [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]])

    def kb_prompts(self) -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton("🗽 Нью-Йорк street", callback_data="p_ny")],
            [InlineKeyboardButton("🏙 Москва-Сити", callback_data="p_moscow")],
            [InlineKeyboardButton("🎞 Студийный (soft)", callback_data="p_studio_soft")],
            [InlineKeyboardButton("🌆 Золотой час", callback_data="p_golden_hour")],
            [InlineKeyboardButton("🧳 Europe casual", callback_data="p_euro_casual")],
            [InlineKeyboardButton("🧠 Business headshot", callback_data="p_business")],
            [InlineKeyboardButton("🌿 Природа", callback_data="p_nature")],
            [InlineKeyboardButton("💡 Киберпанк", callback_data="p_cyber")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")],
        ]
        return InlineKeyboardMarkup(rows)

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

        # разбор рефкод из /start
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
            "Что мы сделаем:\n"
            "• превратим твои обычные фото в профессиональные портреты\n"
            "• сгенерируем образы в разных стилях (Нью-Йорк, Москва-Сити, студийные сетапы и т.д.)\n"
            "• без долгого ожидания и сложностей\n\n"
            "Начнём?"
        )
        await update.effective_message.reply_text(text, reply_markup=self.kb_home(), parse_mode=ParseMode.HTML)

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        uid = q.from_user.id
        st = get_user(uid)

        data = q.data or ""
        if data == "back_home":
            await q.edit_message_text(
                "📍 Главное меню", reply_markup=self.kb_home(has_paid=st.paid_any)
            )
            return

        if data == "try":
            discounted = bool(st.referred_by)
            if discounted:
                text = (
                    "💎 <b>Тарифы генераций</b> <i>(скидка −10% по реферальной ссылке)</i>\n\n"
                    f"• 20 генераций — <s>{PRICES['20']} ₽</s> <b>{int(round(PRICES['20']*0.9))} ₽</b>\n"
                    f"• 40 генераций — <s>{PRICES['40']} ₽</s> <b>{int(round(PRICES['40']*0.9))} ₽</b>\n"
                    f"• 70 генераций — <s>{PRICES['70']} ₽</s> <b>{int(round(PRICES['70']*0.9))} ₽</b>\n\n"
                    "Выбери пакет — и сразу перейдём к загрузке фото."
                )
            else:
                text = (
                    "💎 <b>Тарифы генераций</b>\n\n"
                    f"• 20 генераций — <b>{PRICES['20']} ₽</b>\n"
                    f"• 40 генераций — <b>{PRICES['40']} ₽</b>\n"
                    f"• 70 генераций — <b>{PRICES['70']} ₽</b>\n\n"
                    "Выбери пакет — и сразу перейдём к загрузке фото."
                )
            await q.edit_message_text(text, reply_markup=self.kb_tariffs(discounted), parse_mode=ParseMode.HTML)
            return

        if data in ("buy_20", "buy_40", "buy_70"):
            qty = int(data.split("_")[1])
            price = PRICES[str(qty)]
            # скидка для рефералов
            if st.referred_by:
                price = int(round(price * 0.9))

            # MOCK-оплата
            st.balance += qty
            st.paid_any = True
            save_user(st)

            # реф-начисления 20%
            if st.referred_by:
                ref = get_user(st.referred_by)
                ref_gain = round(price * 0.20, 2)
                ref.ref_earn_total += ref_gain
                ref.ref_earn_ready += ref_gain
                save_user(ref)

            await q.edit_message_text(
                f"✅ <b>Оплата прошла успешно!</b>\n\n"
                f"Начислено на баланс: <b>{qty}</b> генераций.\n\n"
                "Дальше нам нужны твои фото, чтобы обучить модель.\n"
                "Пожалуйста, внимательно прочитай требования ниже 👇",
                parse_mode=ParseMode.HTML
            )
            await self._send_requirements(uid, context)
            return

        if data == "photos_done":
            # запуск обучения
            await q.edit_message_text("🚀 Обучение запущено!\n\nЭто может занять <b>10–30 минут</b>. Мы напишем, когда всё будет готово.", parse_mode=ParseMode.HTML)
            # реально запускаем train
            asyncio.create_task(self._launch_training_and_wait(uid, context))
            return

        if data == "gen_menu":
            if not st.paid_any:
                await q.edit_message_text(
                    "🖼 <b>Генерации</b>\n\nСначала приобретите пакет генераций.",
                    reply_markup=self.kb_buy_or_back(), parse_mode=ParseMode.HTML
                )
                return
            if not st.has_model:
                await q.edit_message_text("⏳ Модель обучается. Мы пришлём уведомление, как только всё будет готово.")
                return
            await q.edit_message_text("Выберите тему:", reply_markup=self.kb_prompts())
            return

        # выбор промпта
        if data in PROMPTS:
            if st.balance < 3:
                await q.edit_message_text(
                    "😕 У вас нет доступных генераций.\n\nПриобретите пакет — и продолжим.",
                    reply_markup=self.kb_buy_or_back()
                )
                return
            prompt_text = PROMPTS[data]
            await q.edit_message_text("🎨 Генерируем 3 изображения… это займёт ~30–60 секунд.")
            try:
                imgs = await self._generate(uid, st.job_id, prompt_text, 3)
            except Exception as e:
                await context.bot.send_message(chat_id=uid, text="❌ Упс, произошла ошибка при генерации. Попробуйте ещё раз.")
                return

            # списываем 3
            st.balance -= 3
            save_user(st)

            media = [InputMediaPhoto(imgs[0], caption=f"Готово! Баланс: {st.balance}")] + \
                    [InputMediaPhoto(u) for u in imgs[1:]]
            await context.bot.send_media_group(chat_id=uid, media=media)
            await context.bot.send_message(chat_id=uid, text="Хочешь другую тему? Выбери ещё:", reply_markup=self.kb_prompts())
            return

        if data == "examples":
            await q.edit_message_text(
                "📸 Примеры работ\n\nПодписывайся на наш канал с примерами и вдохновением:\n@PhotoFly_Examples",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Открыть канал", url="https://t.me/PhotoFly_Examples")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
                ])
            )
            return

        if data == "support":
            await q.edit_message_text(
                "🆘 <b>Поддержка</b>\n\nЕсли возник вопрос — мы на связи: @photofly_ai\n\nПишите коротко и по делу — так быстрее поможем.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Написать в поддержку", url="https://t.me/photofly_ai")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
                ]),
                parse_mode=ParseMode.HTML
            )
            return

        if data == "ref_menu":
            link = f"https://t.me/{(await context.bot.get_me()).username}?start={st.ref_code}"
            text = (
                "🤝 <b>Реферальная программа</b>\n\n"
                "• Делись своей ссылкой — получай <b>20%</b> с покупок друзей\n"
                "• Друзьям — <b>скидка 10%</b> на первый заказ\n"
                "• Вывод средств от <b>500 ₽</b>\n\n"
                f"Твоя ссылка:\n<code>{link}</code>"
            )
            await q.edit_message_text(text, reply_markup=self.kb_ref_menu(uid), parse_mode=ParseMode.HTML)
            return

        if data == "ref_income":
            text = (
                "📈 <b>Мои доходы</b>\n\n"
                f"Заработано всего: <b>{st.ref_earn_total:.2f} ₽</b>\n"
                f"Доступно к выводу: <b>{st.ref_earn_ready:.2f} ₽</b>\n"
                f"Выплачено: <b>{st.ref_earn_total - st.ref_earn_ready:.2f} ₽</b>\n\n"
                "Минимальная сумма вывода: <b>500 ₽</b>."
            )
            await q.edit_message_text(text, reply_markup=self.kb_ref_menu(uid), parse_mode=ParseMode.HTML)
            return

        if data == "ref_list":
            # простая заглушка-список
            # можно хранить отдельную таблицу покупок по user_id и строить отчёт
            await q.edit_message_text(
                "👥 <b>Мои рефералы</b>\n\n"
                "Список и детали покупок появятся здесь.\n"
                "Пока что эта секция в минимальной версии.",
                reply_markup=self.kb_ref_menu(uid),
                parse_mode=ParseMode.HTML
            )
            return

        if data == "ref_payout":
            await q.edit_message_text(
                "💳 <b>Вывод средств</b>\n\n"
                "Пожалуйста, напиши нам @photofly_ai — укажи:\n"
                "• сумму к выводу\n• свой @ник и ID в боте\n• удобный способ получения\n\n"
                "⚠️ Вывод доступен от <b>500 ₽</b>.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Написать поддержку", url="https://t.me/photofly_ai")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
                ]),
                parse_mode=ParseMode.HTML
            )
            return

        # спец-оффер покупка
        if data == "buy_flash_50":
            st.balance += FLASH_OFFER["qty"]
            st.paid_any = True
            save_user(st)
            await q.edit_message_text(
                f"✅ <b>Готово!</b> Начислено <b>{FLASH_OFFER['qty']}</b> генераций за <b>{FLASH_OFFER['price']} ₽</b>.\n\n"
                "Загружай фото — мы обучим модель и начнём творить!",
                parse_mode=ParseMode.HTML
            )
            await self._send_requirements(uid, context)
            return

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Принимаем любые фото: скачиваем и форвардим на бэкенд /api/upload_photo"""
        uid = update.effective_user.id
        st = get_user(uid)
        photo = update.message.photo[-1]  # лучшего качества
        file = await context.bot.get_file(photo.file_id)
        local_path = os.path.join(PHOTOS_TMP, f"{uid}_{int(time.time())}.jpg")
        await file.download_to_drive(local_path)

        # POST -> backend
        try:
            async with httpx.AsyncClient(timeout=120) as cl:
                with open(local_path, "rb") as f:
                    data = {"user_id": str(uid)}
                    files = {"file": ("photo.jpg", f, "image/jpeg")}
                    r = await cl.post(f"{BACKEND_ROOT}/api/upload_photo", data=data, files=files)
                    r.raise_for_status()
        except Exception:
            await update.effective_message.reply_text("⚠️ Не удалось загрузить фото. Повтори ещё раз, пожалуйста.")
            return

        # не спамим «фото загружено» — требования уже отправлены, у пользователя есть кнопка «Фото загружены»
        # Можно молча: pass. Но дадим аккуратный тик:
        await update.effective_message.reply_text("Фото принято ✅\nЗагрузи ещё и нажми «Фото загружены», когда будешь готов.")

    # -------------- HELPERS --------------
    async def _send_requirements(self, uid: int, context: ContextTypes.DEFAULT_TYPE):
        text = (
            "📥 <b>Загрузка фото для обучения</b>\n\n"
            "Загрузи <b>от 15 до 50</b> фотографий, где тебя хорошо видно. Лучше — 25–35 шт., разные ракурсы и сцены.\n\n"
            "<b>Требования:</b>\n"
            "• без очков, масок, кепок и сильных аксессуаров\n"
            "• без тяжёлых фильтров/ретуши, без коллажей\n"
            "• лицо и плечи — чётко; разные эмоции и свет\n"
            "• вертикальные кадры предпочтительнее (но не критично)\n"
            "• можно селфи и фото в полный рост\n"
            "• избегай размытия и пересвета\n\n"
            "Когда закончишь загрузку, нажми кнопку ниже 👇"
        )
        await context.bot.send_message(chat_id=uid, text=text, reply_markup=self.kb_upload_fixed(), parse_mode=ParseMode.HTML)

    async def _launch_training_and_wait(self, uid: int, context: ContextTypes.DEFAULT_TYPE):
        """Стартуем train и молча ждём готовности; пользователю уведомление только в конце."""
        try:
            async with httpx.AsyncClient(timeout=180) as cl:
                data = {"user_id": str(uid)}
                r = await cl.post(f"{BACKEND_ROOT}/api/train", data=data)
                r.raise_for_status()
                train_data = r.json()
                job_id = train_data.get("job_id")
                if not job_id:
                    raise RuntimeError("no job_id from backend")
        except Exception as e:
            await context.bot.send_message(chat_id=uid, text="❌ Не удалось запустить обучение. Попробуйте ещё раз.")
            return

        # сохраним job_id
        st = get_user(uid)
        st.job_id = job_id
        save_user(st)

        # поллим до успеха (без сообщений пользователю)
        status_url = f"{BACKEND_ROOT}/api/status/{job_id}"
        for _ in range(300):  # максимум ~10 минут (300*2с)
            try:
                async with httpx.AsyncClient(timeout=30) as cl:
                    rr = await cl.get(status_url)
                    rr.raise_for_status()
                    dd = rr.json()
                    status = (dd.get("status") or "").lower()
                    model_id = dd.get("model_id")
                    if status in ("succeeded", "completed", "complete"):
                        st.has_model = True
                        # model_id нам не нужен для UX — но сохраним job_id
                        save_user(st)
                        break
                    if status in ("failed", "canceled", "cancelled", "error"):
                        await context.bot.send_message(chat_id=uid, text="❌ Обучение не удалось. Попробуйте ещё раз загрузить фото.")
                        return
            except Exception:
                pass
            await asyncio.sleep(2)

        if not st.has_model:
            await context.bot.send_message(chat_id=uid, text="❌ Время ожидания вышло. Попробуйте ещё раз позже.")
            return

        # уведомление о готовности + меню тем
        await context.bot.send_message(
            chat_id=uid,
            text="✨ <b>Готово!</b> Модель обучена.\n\nВыбери тему — сгенерим сразу 3 варианта.",
            reply_markup=self.kb_prompts(), parse_mode=ParseMode.HTML
        )

    async def _generate(self, uid: int, job_id: Optional[str], prompt: str, n: int) -> List[str]:
        """POST /api/generate (с job_id, чтобы бэкенд понял кастомную модель)."""
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

    # ---------- FLASH OFFER SCHEDULER (24h) ----------
    async def _flash_offer_scheduler(self):
        """Фоновая задача: раз в 30 мин проверяем, кому пора прислать оффер 50 за 379."""
        while True:
            now = time.time()
            try:
                for k, v in list(DB.items()):
                    st = UserState(**v)
                    if st.flash_sent:
                        continue
                    # всем — и тем, кто платил, и тем, кто нет
                    if now - (st.first_seen_ts or now) >= 24 * 3600:
                        await self._send_flash_offer(st.id)
                        st.flash_sent = True
                        save_user(st)
            except Exception:
                pass
            await asyncio.sleep(1800)  # 30 минут

    async def _send_flash_offer(self, uid: int):
        # отдельное «предложение за 379 ₽»
        text = (
            f"🔥 <b>Только сейчас!</b>\n\n"
            f"Вам доступно <b>{FLASH_OFFER['qty']} генераций</b> за <b>{FLASH_OFFER['price']} ₽</b>.\n"
            "Предложение ограничено по времени.\n\n"
            "Нажмите ниже, чтобы приобрести и перейти к загрузке фото."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Приобрести", callback_data="buy_flash_50")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_home")]
        ])
        # отправим как новое сообщение
        try:
            await self.app.bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            pass


# глобальный экспорт для main.py
tg_app = TgApp()
