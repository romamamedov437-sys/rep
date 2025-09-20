import os, io, zipfile, uuid, time, logging, asyncio
from typing import Dict, Any, Optional, List

import httpx
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from telegram import Update
from telegram.error import TelegramError

from bot import tg_app  # берем готовое Application (без polling)

# ---------- ENV ----------
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").rstrip("/")
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "hook").strip()
BACKEND_ROOT = (os.getenv("BACKEND_ROOT") or "").rstrip("/")

REPLICATE_API_TOKEN = (os.getenv("REPLICATE_API_TOKEN") or "").strip()
REPLICATE_TRAIN_ENDPOINT = os.getenv("REPLICATE_TRAIN_ENDPOINT", "https://api.replicate.com/v1/trainings").strip()
REPLICATE_TRAIN_OWNER = os.getenv("REPLICATE_TRAIN_OWNER", "replicate").strip()
REPLICATE_TRAIN_MODEL = os.getenv("REPLICATE_TRAIN_MODEL", "fast-flux-trainer").strip()
REPLICATE_TRAIN_VERSION = (os.getenv("REPLICATE_TRAIN_VERSION") or "").strip()

REPLICATE_GEN_MODEL = os.getenv("REPLICATE_GEN_MODEL", "black-forest-labs/FLUX.1-schnell").strip()
REPLICATE_GEN_VERSION = os.getenv("REPLICATE_GEN_VERSION", "latest").strip()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("web")

# ---------- APP ----------
app = FastAPI()

# Хранилище файлов/статуса
BASE_DIR = "/opt/render/project/src"
DATA_DIR = os.path.join(BASE_DIR, "data")
USERS_DIR = os.path.join(DATA_DIR, "users")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")   # для ZIP, доступных по HTTP
os.makedirs(USERS_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

# отдаем /data/uploads публично
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")

# in-memory задачи: job_id -> {status, progress, training_id, model_id, user_id}
jobs: Dict[str, Dict[str, Any]] = {}

# ============ TG WEBHOOK LIFECYCLE ============
@app.on_event("startup")
async def startup_event():
    await tg_app.initialize()
    await tg_app.start()
    if PUBLIC_URL:
        hook_url = f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"
        try:
            await tg_app.bot.delete_webhook(drop_pending_updates=True)
            await tg_app.bot.set_webhook(
                hook_url, allowed_updates=["message", "callback_query"]
            )
            log.info(f"Webhook set: {hook_url}")
        except TelegramError as e:
            log.error(f"Webhook error: {e!r}")
    else:
        log.warning("PUBLIC_URL не задан — вебхук не настроен.")

@app.on_event("shutdown")
async def shutdown_event():
    try:
        await tg_app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    await tg_app.stop()
    log.info("🛑 Telegram application stopped")

# ============ HEALTH & ROOT ============
@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/")
async def root():
    return {
        "ok": True,
        "service": "webhook+replicate backend",
        "routes": ["/healthz", "/webhook/{secret}", "/api/upload_photo", "/api/train", "/api/status/{job_id}", "/api/generate"]
    }

# ============ TG WEBHOOK ============
@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}

# ============ HELPERS ============
def user_dir(user_id: str) -> str:
    d = os.path.join(USERS_DIR, str(user_id))
    os.makedirs(d, exist_ok=True)
    return d

def user_photos_dir(user_id: str) -> str:
    d = os.path.join(user_dir(user_id), "photos")
    os.makedirs(d, exist_ok=True)
    return d

def build_zip_of_user_photos(user_id: str) -> str:
    """Собрать ZIP из /photos, положить в /data/uploads, вернуть ПОЛНЫЙ путь к файлу."""
    photos = []
    pdir = user_photos_dir(user_id)
    for name in os.listdir(pdir):
        path = os.path.join(pdir, name)
        if os.path.isfile(path):
            photos.append(path)

    if not photos:
        raise HTTPException(status_code=400, detail="no photos uploaded")

    zip_name = f"{user_id}_{uuid.uuid4().hex[:8]}.zip"
    zip_path = os.path.join(UPLOADS_DIR, zip_name)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in photos:
            zf.write(p, arcname=os.path.basename(p))
    return zip_path

def public_url_for_zip(zip_path: str) -> str:
    """Вернуть публичный URL на ранее созданный zip в /uploads."""
    if not PUBLIC_URL:
        raise HTTPException(status_code=500, detail="PUBLIC_URL not set")
    fname = os.path.basename(zip_path)
    return f"{PUBLIC_URL}/uploads/{fname}"

async def call_replicate_training(images_zip_url: str, user_id: str) -> Dict[str, Any]:
    """POST /v1/trainings — запускаем тренинг у Replicate fast-flux-trainer."""
    if not REPLICATE_API_TOKEN:
        raise HTTPException(status_code=500, detail="REPLICATE_API_TOKEN not set")
    if not REPLICATE_TRAIN_VERSION:
        raise HTTPException(status_code=500, detail="REPLICATE_TRAIN_VERSION not set")

    payload = {
        "version": REPLICATE_TRAIN_VERSION,
        "model": f"{REPLICATE_TRAIN_OWNER}/{REPLICATE_TRAIN_MODEL}",
        "input": {
            # поля зависят от конкретного тренера; ниже — типовой минимум:
            "images_zip": images_zip_url,
            "steps": 800
        }
    }
    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120) as cl:
        r = await cl.post(REPLICATE_TRAIN_ENDPOINT, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()

async def get_replicate_training_status(training_id: str) -> Dict[str, Any]:
    """GET /v1/trainings/{id} — получить статус."""
    if not REPLICATE_API_TOKEN:
        raise HTTPException(status_code=500, detail="REPLICATE_API_TOKEN not set")
    url = f"https://api.replicate.com/v1/trainings/{training_id}"
    headers = {"Authorization": f"Token {REPLICATE_API_TOKEN}"}
    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.get(url, headers=headers)
        r.raise_for_status()
        return r.json()

async def call_replicate_generate(prompt: str, model_id: Optional[str], num_images: int) -> List[str]:
    """Запуск inference на Replicate. Возвращает список URL изображений."""
    if not REPLICATE_API_TOKEN:
        raise HTTPException(status_code=500, detail="REPLICATE_API_TOKEN not set")

    # Если после тренировки у тебя есть свой кастомный model_id — подставим его.
    # Иначе используем базовую модель (REPLICATE_GEN_MODEL/REPLICATE_GEN_VERSION).
    body = {
        "version": REPLICATE_GEN_VERSION,
        "input": {
            "prompt": prompt,
            "num_outputs": num_images
        }
    }
    model_path = REPLICATE_GEN_MODEL
    if model_id:
        model_path = model_id  # например, "username/model-name"

    url = f"https://api.replicate.com/v1/models/{model_path}/predictions"
    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120) as cl:
        r = await cl.post(url, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()

    # Ожидаем завершения (простая опросная логика)
    prediction_url = data["urls"]["get"]
    outputs: List[str] = []
    async with httpx.AsyncClient(timeout=120) as cl:
        for _ in range(60):  # ~2 минуты
            rr = await cl.get(prediction_url, headers=headers)
            rr.raise_for_status()
            dd = rr.json()
            status = dd.get("status")
            if status == "succeeded":
                outs = dd.get("output") or []
                if isinstance(outs, list):
                    outputs = [str(x) for x in outs]
                elif isinstance(outs, str):
                    outputs = [outs]
                break
            elif status in ("failed", "canceled"):
                raise HTTPException(status_code=500, detail=f"replicate generation {status}")
            await asyncio.sleep(2)

    return outputs

# ============ API: UPLOAD / TRAIN / STATUS / GENERATE ============

@app.post("/api/upload_photo")
async def api_upload_photo(user_id: str = Form(...), file: UploadFile = File(...)):
    """Принимаем фотку от бота, кладем в /data/users/<id>/photos"""
    pdir = user_photos_dir(user_id)
    name = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{file.filename}"
    path = os.path.join(pdir, name)
    with open(path, "wb") as f:
        f.write(await file.read())
    log.info(f"UPLOAD user={user_id} -> {path}")
    return {"ok": True, "path": path}

@app.post("/api/train")
async def api_train(user_id: str = Form(...)):
    """Собираем ZIP фото, отдаем ссылку Replicate тренеру, сохраняем training_id в job."""
    # 1) zip
    zip_path = build_zip_of_user_photos(user_id)
    zip_url = public_url_for_zip(zip_path)

    # 2) call replicate
    train = await call_replicate_training(zip_url, user_id)
    training_id = train.get("id") or train.get("uuid")
    if not training_id:
        raise HTTPException(status_code=500, detail="no training_id from replicate")

    # 3) сохранить job
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    jobs[job_id] = {
        "status": "running",
        "progress": 0,
        "user_id": user_id,
        "training_id": training_id,
        "model_id": None,
    }
    log.info(f"TRAIN started job={job_id} training_id={training_id} user={user_id}")
    return {"job_id": job_id, "status": "started"}

@app.get("/api/status/{job_id}")
async def api_status(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")

    # подтягиваем свежий статус из Replicate
    training_id = j.get("training_id")
    if training_id:
        try:
            st = await get_replicate_training_status(training_id)
            state = st.get("status") or st.get("state")
            out = st.get("output") or {}
            model = out.get("model") or out.get("id")  # зависит от ответа тренера
            if state:
                j["status"] = state
                j["progress"] = 100 if state == "succeeded" else (0 if state in ("starting","queued") else 50)
            if model:
                j["model_id"] = model
        except Exception as e:
            log.warning(f"status fetch failed for {job_id}: {e!r}")

    return {
        "job_id": job_id,
        "status": j.get("status"),
        "progress": j.get("progress", 0),
        "model_id": j.get("model_id"),
    }

# ====== УНИВЕРСАЛЬНАЯ /api/generate (form + json) ======
class GenerateJSON(BaseModel):
    user_id: str | int
    prompt: str
    num_images: int = 1
    job_id: Optional[str] = None

@app.post("/api/generate")
async def api_generate(
    # form-вариант
    user_id_form: Optional[str] = Form(None),
    prompt_form: Optional[str] = Form(None),
    num_images_form: Optional[int] = Form(None),
    job_id_form: Optional[str] = Form(None),
    # json-вариант
    json_body: Optional[GenerateJSON] = Body(None),
):
    """
    Принимает либо form-data, либо JSON.
    """
    if json_body is not None:
        user_id = str(json_body.user_id)
        prompt = json_body.prompt
        num_images = json_body.num_images or 1
        job_id = json_body.job_id
    else:
        if not user_id_form or not prompt_form:
            raise HTTPException(status_code=400, detail="user_id and prompt are required")
        user_id = str(user_id_form)
        prompt = prompt_form
        num_images = num_images_form or 1
        job_id = job_id_form

    model_id = None
    if job_id and job_id in jobs:
        model_id = jobs[job_id].get("model_id")

    urls = await call_replicate_generate(prompt=prompt, model_id=model_id, num_images=num_images)
    return {"images": urls}
