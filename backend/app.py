import os, uuid, time, asyncio, logging, glob
from typing import Optional, Dict, Any, List

import httpx
import replicate
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import BadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from store import get_user, set_user, update_user
from prompts import PROMPTS

# ====== ENV ======
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "")
TRAINER_ENDPOINT    = os.getenv("TRAINER_ENDPOINT", "").strip()
BOT_TOKEN           = os.getenv("BOT_TOKEN", "")
WEBHOOK_URL         = os.getenv("WEBHOOK_URL", "")
PUBLIC_BASE         = (os.getenv("PUBLIC_BASE") or "").rstrip("/")
OUT_DIR             = os.getenv("OUT_DIR", "./outputs")
MAX_PHOTOS_PER_USER = int(os.getenv("MAX_PHOTOS_PER_USER", "80"))

os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs("./data", exist_ok=True)

replicate.Client(api_token=REPLICATE_API_TOKEN)

# ====== LOG ======
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

# ====== FASTAPI ======
web_app = FastAPI(title="PhotoFly + Replicate")
web_app.mount("/outputs", StaticFiles(directory=OUT_DIR), name="outputs")

# ====== TG APP ======
tg_app = Application.builder().token(BOT_TOKEN).build()
scheduler = AsyncIOScheduler()

# ====== UTILS ======
def user_dir(uid: str) -> str:
    p = os.path.join("./data", "users", uid)
    os.makedirs(p, exist_ok=True)
    return p

def photos_dir(uid: str) -> str:
    p = os.path.join(user_dir(uid), "photos")
    os.makedirs(p, exist_ok=True)
    return p

def list_user_photos(uid: str) -> List[str]:
    return sorted(glob.glob(os.path.join(photos_dir(uid), "*")))

def url_for_output(name: str) -> str:
    return f"{PUBLIC_BASE}/outputs/{name}" if PUBLIC_BASE else f"/outputs/{name}"

async def call_replicate_json(method: str, url: str, payload: Dict[str, Any] = None) -> Dict[str, Any]:
    headers = {"Authorization": f"Token {REPLICATE_API_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=300) as cl:
        if method == "POST":
            r = await cl.post(url, headers=headers, json=payload or {})
        else:
            r = await cl.get(url, headers=headers)
        r.raise_for_status()
        return r.json()

# ====== MODELS ======
class TrainResp(BaseModel):
    user_id: str
    job_id: str
    status: str = "started"

class StatusResp(BaseModel):
    user_id: str
    status: str
    progress: int
    training_id: Optional[str] = None
    lora_url: Optional[str] = None
    model: Optional[str] = None
    message: Optional[str] = None

class GenReq(BaseModel):
    user_id: str | int
    prompt: str
    num_images: int = Field(default=1, ge=1, le=4)
    width: int = 768
    height: int = 1024
    steps: int = 28
    cfg: float = 3.5

class GenResp(BaseModel):
    images: List[str]

# ====== API ======

@web_app.get("/healthz")
def healthz():
    return {"ok": True}

@web_app.post("/api/upload_photo")
async def upload_photo(user_id: str = Form(...), file: UploadFile = File(...)):
    # сохраняем как есть; Replicate-тренер возьмёт из папки
    pdir = photos_dir(user_id)
    files = list_user_photos(user_id)
    if len(files) >= MAX_PHOTOS_PER_USER:
        raise HTTPException(429, f"Already uploaded {len(files)} photos. Limit {MAX_PHOTOS_PER_USER}.")

    fname = f"{int(time.time())}_{uuid.uuid4().hex[:6]}_{file.filename}"
    fpath = os.path.join(pdir, fname)
    with open(fpath, "wb") as f:
        f.write(await file.read())

    update_user(str(user_id), photos=len(list_user_photos(user_id)))
    log.info(f"[UPLOAD] user={user_id} -> {fpath}")
    return {"ok": True, "count": len(list_user_photos(user_id))}

@web_app.post("/api/train", response_model=TrainResp)
async def api_train(user_id: str = Form(...)):
    """
    Старт обучения через Replicate trainer.
    Мы создаём TRAINING (или prediction у trainer), сохраняем его id и начинаем фоновой опрос статуса.
    """
    uid = str(user_id)
    photos = list_user_photos(uid)
    if len(photos) < 8:
        raise HTTPException(400, "Need at least 8 photos.")

    # 1) Заливаем фото в Replicate Files (simple way: передаём публичные URLs — но у нас локально)
    # Поэтому для fast-flux-trainer используем прямую загрузку файлов как multipart недоступно из бекенда.
    # Решение: подаём ссылки, которые доступны с PUBLIC_BASE. Сохраним фото и отдаём HTTP-URL:
    # Уже сохранено, а статика у нас не отдаёт /data. Для тренера с реплики из блога — достаточно дать список URL’ов.
    # Переместим фотки в OUT_DIR/public_photos для статической раздачи:
    pub_dir = os.path.join(OUT_DIR, "public_photos", uid)
    os.makedirs(pub_dir, exist_ok=True)

    urls = []
    for ph in photos:
        base = os.path.basename(ph)
        dst = os.path.join(pub_dir, base)
        if not os.path.exists(dst):
            try:
                Image.open(ph).save(dst)  # простой копипаст
            except Exception:
                # если не картинка — пропускаем
                continue
        urls.append(f"{PUBLIC_BASE}/outputs/public_photos/{uid}/{base}")

    if len(urls) < 8:
        raise HTTPException(400, "Not enough valid images after filtering.")

    # 2) Создаём training через Replicate (fast-flux-trainer)
    # По их блогу достаточно POST на /v1/predictions с версией модели тренера,
    # но у нас есть удобная “trainer” ссылка. Воспользуемся официальным endpoint’ом:
    # https://api.replicate.com/v1/predictions  с версией fast-flux-trainer.
    # У trainer версии стабильно меняются. Поэтому используем “trainer endpoint” из .env
    # Универсально: дергаем универсальный endpoint Replicate Predictions.
    trainer_version = None  # можно парсить из TRAINER_ENDPOINT, но проще указать input schema ниже.
    create_url = "https://api.replicate.com/v1/predictions"
    payload = {
        "version": "replicate/fast-flux-trainer",  # абстрактный alias у них поддерживается
        "input": {
            "input_images": urls,
            "steps": 800,
            "resolution": 768,
            "captioning": False,
            # можно добавить class_prompt, seed и т.д. по нуждам
        }
    }
    data = await call_replicate_json("POST", create_url, payload)
    training_id = data.get("id")

    job_id = f"job_{uuid.uuid4().hex[:8]}"
    state = {
        "job_id": job_id,
        "status": "running",
        "progress": 0,
        "training_id": training_id,
        "photos": len(urls),
        "lora_url": None,
        "model": None,
        "message": None,
        "started_at": int(time.time())
    }
    set_user(uid, state)
    log.info(f"[TRAIN] user={uid} training_id={training_id} photos={len(urls)}")

    # фоновый таск опроса статуса
    asyncio.create_task(poll_training(uid))

    return TrainResp(user_id=uid, job_id=job_id)

async def poll_training(uid: str):
    """Периодически опрашиваем Replicate по training_id и обновляем прогресс."""
    try:
        for _ in range(60*120):  # ~2 часа при шаге 1с (условно)
            st = get_user(uid)
            tid = st.get("training_id")
            if not tid:
                return
            url = f"https://api.replicate.com/v1/predictions/{tid}"
            data = await call_replicate_json("GET", url)
            status = data.get("status")  # starting/processing/succeeded/failed/canceled
            logs   = (data.get("logs") or "")[:5000]

            # простая эвристика прогресса
            prog = st.get("progress", 0)
            if status in ("starting", "queued"):
                prog = max(prog, 3)
            elif status == "processing":
                prog = max(prog, min(95, prog + 1))
            elif status == "succeeded":
                prog = 100
            elif status in ("failed", "canceled"):
                prog = st.get("progress", 0)

            # забираем LoRA/model ссылку из output
            out = data.get("output")
            lora_url = None
            model_id = None
            if isinstance(out, dict):
                lora_url = out.get("lora_weights") or out.get("lora_url")
                model_id = out.get("model")  # вдруг trainer вернёт готовый model ref

            update_user(uid,
                        status=("done" if status=="succeeded" else ("error" if status in ("failed","canceled") else "running")),
                        progress=prog,
                        lora_url=lora_url or st.get("lora_url"),
                        model=model_id or st.get("model"),
                        message=logs[-800:] if logs else None)

            if status in ("succeeded","failed","canceled"):
                # если есть tg chat — отправим уведомление (если бота знаем)
                return

            await asyncio.sleep(1)
    except Exception as e:
        log.exception(f"[POLL] error: {e}")

@web_app.get("/api/status/{user_id}", response_model=StatusResp)
def api_status(user_id: str):
    st = get_user(str(user_id))
    if not st:
        raise HTTPException(404, "no job")
    return StatusResp(
        user_id=str(user_id),
        status=st.get("status","unknown"),
        progress=int(st.get("progress",0)),
        training_id=st.get("training_id"),
        lora_url=st.get("lora_url"),
        model=st.get("model"),
        message=st.get("message")
    )

@web_app.post("/api/generate", response_model=GenResp)
async def api_generate(req: GenReq):
    uid = str(req.user_id)
    st = get_user(uid)
    lora_url = st.get("lora_url")
    model_id = st.get("model")

    if not (lora_url or model_id):
        raise HTTPException(400, "No trained LoRA/model yet. Train first.")

    # Вариант A: использовать LoRA поверх flux-inference
    # многие модели на Replicate принимают параметр "lora_weights"
    # Для надёжности используем flux-schnell или flux-dev (проверь в своей учётке)
    inference_url = "https://api.replicate.com/v1/predictions"
    payload = {
        "version": "black-forest-labs/flux-schnell",  # можно заменить на доступную у тебя версию
        "input": {
            "prompt": req.prompt,
            "width": req.width,
            "height": req.height,
            "num_outputs": req.num_images,
            "lora_weights": lora_url,  # ключ
            "num_inference_steps": req.steps,
            "guidance_scale": req.cfg
        }
    }
    data = await call_replicate_json("POST", inference_url, payload)

    # ждём завершения предсказания
    pred_id = data.get("id")
    for _ in range(60*15):
        d = await call_replicate_json("GET", f"{inference_url}/{pred_id}")
        if d.get("status") == "succeeded":
            outs = d.get("output") or []
            if not isinstance(outs, list):
                outs = [outs]
            # скачивать не надо — отдадим ссылки как есть
            return GenResp(images=outs)
        if d.get("status") in ("failed","canceled"):
            raise HTTPException(500, f"inference failed: {d.get('error')}")
        await asyncio.sleep(1)

    raise HTTPException(504, "inference timeout")

# ====== TELEGRAM PART ======

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Загрузить фото", callback_data="upload")],
        [InlineKeyboardButton("✅ Фотографии загружены", callback_data="photos_done")],
        [InlineKeyboardButton("📊 Прогресс", callback_data="status")],
        [InlineKeyboardButton("✨ Сгенерировать", callback_data="generate")],
    ])

def kb_prompts() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, min(len(PROMPTS), 10)):  # кнопок много не делаем; подробный выбор через /prompt N
        rows.append([InlineKeyboardButton(f"Сцена {i+1}", callback_data=f"p:{i}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_home")])
    return InlineKeyboardMarkup(rows)

async def safe_edit(q, text: str, reply_markup=None):
    try:
        await q.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Привет! Я помогу загрузить фото, обучить модель (Flux) и сгенерировать портреты.\n"
        "Загрузите 10–30 фото с лицом. Когда будете готовы — нажмите «Фотографии загружены».",
        reply_markup=kb_main()
    )

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = await update.message.photo[-1].get_file()
    path = await f.download_to_drive()
    with open(path, "rb") as fp:
        async with httpx.AsyncClient(timeout=120) as cl:
            r = await cl.post(
                f"{PUBLIC_BASE}/api/upload_photo",
                data={"user_id": update.effective_user.id},
                files={"file": ("photo.jpg", fp, "image/jpeg")}
            )
            r.raise_for_status()
    await update.message.reply_text(
        "Фото загружено ✅\nКогда закончите — нажмите «Фотографии загружены».",
        reply_markup=kb_main()
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "upload":
        await safe_edit(q, "Пришлите 10–30 фото. Когда будете готовы — нажмите «Фотографии загружены».", reply_markup=kb_main())
        return

    if q.data == "photos_done":
        async with httpx.AsyncClient(timeout=300) as cl:
            r = await cl.post(f"{PUBLIC_BASE}/api/train", data={"user_id": uid})
            if r.status_code >= 400:
                await safe_edit(q, f"❌ Ошибка запуска обучения: {await r.aread()}", reply_markup=kb_main())
                return
            data = r.json()
        await safe_edit(q, f"🚀 Обучение запущено!\nID: `{data.get('job_id')}`\nЖмите «Прогресс» для проверки.", reply_markup=kb_main())
        return

    if q.data == "status":
        async with httpx.AsyncClient(timeout=60) as cl:
            r = await cl.get(f"{PUBLIC_BASE}/api/status/{uid}")
            if r.status_code == 404:
                await safe_edit(q, "Задача не найдена. Сначала запустите обучение.", reply_markup=kb_main())
                return
            st = r.json()
        await safe_edit(q, f"📊 Статус: *{st['status']}*\nПрогресс: *{st['progress']}%*",
                        reply_markup=kb_main())
        return

    if q.data == "generate":
        # покажем быстрый выбор сцен
        await safe_edit(q, "Выберите сцену или отправьте свой текст /prompt <номер>.", reply_markup=kb_prompts())
        return

    if q.data.startswith("p:"):
        idx = int(q.data.split(":")[1])
        prompt = PROMPTS[idx]
        payload = {"user_id": uid, "prompt": prompt, "num_images": 1}
        async with httpx.AsyncClient(timeout=900) as cl:
            r = await cl.post(f"{PUBLIC_BASE}/api/generate", json=payload)
            if r.status_code >= 400:
                await safe_edit(q, f"❌ Ошибка генерации: {await r.aread()}", reply_markup=kb_main())
                return
            data = r.json()
        await safe_edit(q, "Готово ✅", reply_markup=kb_main())
        for u in data.get("images", []):
            await q.message.reply_photo(photo=u)
        return

    if q.data == "back_home":
        await safe_edit(q, "Главное меню:", reply_markup=kb_main())
        return

@web_app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

@web_app.on_event("startup")
async def on_startup():
    await tg_app.initialize()
    await tg_app.start()
    if WEBHOOK_URL:
        try:
            await tg_app.bot.set_webhook(WEBHOOK_URL)
            log.info(f"Webhook set: {WEBHOOK_URL}")
        except Exception as e:
            log.error(f"Webhook error: {e}")
    scheduler.start()
    log.info("✅ App started")

@web_app.on_event("shutdown")
async def on_shutdown():
    scheduler.shutdown(wait=False)
    await tg_app.stop()
    log.info("🛑 App stopped")
