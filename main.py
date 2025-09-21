import os, io, zipfile, uuid, time, logging, asyncio
from typing import Dict, Any, Optional, List

import httpx
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Response
from fastapi.staticfiles import StaticFiles
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
                hook_url, allowed_updates=["message","callback_query"]
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

# ============ ROOT & HEALTH ============
@app.get("/")
async def root():
    return {"ok": True, "service": "replicate-bridge", "has_public": bool(PUBLIC_URL)}

@app.head("/")
async def head_root():
    return Response(status_code=200)

@app.get("/healthz")
async def healthz():
    return {"ok": True}

# ============ ДОБАВЛЕНО: DEBUG РОУТЫ ============
@app.get("/debug/webhook_info")
async def debug_webhook_info():
    try:
        me = await tg_app.bot.get_me()
        info = await tg_app.bot.get_webhook_info()
        return {
            "bot": {"id": me.id, "username": me.username},
            "webhook_url": info.url,
            "has_custom_certificate": info.has_custom_certificate,
            "pending_update_count": info.pending_update_count,
            "ip_address": info.ip_address,
            "last_error_date": info.last_error_date,
            "last_error_message": info.last_error_message,
            "max_connections": info.max_connections,
            "allowed_updates": info.allowed_updates,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/debug/ping")
async def debug_ping(chat_id: int):
    try:
        msg = await tg_app.bot.send_message(chat_id=chat_id, text="pong ✅")
        return {"ok": True, "message_id": msg.message_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/debug/set_webhook")
async def debug_set_webhook():
    """Проставить вебхук = PUBLIC_URL/webhook/WEBHOOK_SECRET"""
    try:
        url = f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"
        await tg_app.bot.set_webhook(url, allowed_updates=["message", "callback_query"])
        info = await tg_app.bot.get_webhook_info()
        return {"set_to": url, "webhook_url": info.url, "pending_update_count": info.pending_update_count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/debug/delete_webhook")
async def debug_delete_webhook():
    """Сбросить вебхук (на всякий случай)"""
    try:
        await tg_app.bot.delete_webhook(drop_pending_updates=True)
        info = await tg_app.bot.get_webhook_info()
        return {"after_delete_webhook_url": info.url, "pending_update_count": info.pending_update_count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============ TG WEBHOOK ============
@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        from bot import ensure_initialized
        await ensure_initialized()
    except Exception as e:
        log.error(f"ensure_initialized failed: {e!r}")

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    log.info(f"Webhook update: keys={list(data.keys())}")

    try:
        await tg_app.process_update(update)
    except Exception as e:
        log.exception(f"process_update crashed: {e!r}")
        return {"ok": False, "error": "handler crashed"}

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

def count_user_photos(user_id: str) -> int:
    pdir = user_photos_dir(user_id)
    if not os.path.isdir(pdir):
        return 0
    return sum(
        1 for name in os.listdir(pdir)
        if os.path.isfile(os.path.join(pdir, name))
    )

def build_zip_of_user_photos(user_id: str) -> str:
    photos = []
    pdir = user_photos_dir(user_id)
    if os.path.isdir(pdir):
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
    if not PUBLIC_URL:
        raise HTTPException(status_code=500, detail="PUBLIC_URL not set")
    fname = os.path.basename(zip_path)
    return f"{PUBLIC_URL}/uploads/{fname}"

def _pct_from_replicate_status(state: str) -> int:
    state = (state or "").lower()
    if state in ("starting", "queued", "pending"):
        return 5
    if state in ("processing", "running"):
        return 50
    if state in ("succeeded", "completed", "complete"):
        return 100
    if state in ("failed", "canceled", "cancelled", "error"):
        return 100
    return 0

async def call_replicate_training(images_zip_url: str, user_id: str) -> Dict[str, Any]:
    if not REPLICATE_API_TOKEN:
        raise HTTPException(status_code=500, detail="REPLICATE_API_TOKEN not set")
    if not REPLICATE_TRAIN_VERSION:
        raise HTTPException(status_code=500, detail="REPLICATE_TRAIN_VERSION not set")

    payload = {
        "version": REPLICATE_TRAIN_VERSION,
        "model": f"{REPLICATE_TRAIN_OWNER}/{REPLICATE_TRAIN_MODEL}",
        "input": {"images_zip": images_zip_url, "steps": 800}
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
    if not REPLICATE_API_TOKEN:
        raise HTTPException(status_code=500, detail="REPLICATE_API_TOKEN not set")
    url = f"https://api.replicate.com/v1/trainings/{training_id}"
    headers = {"Authorization": f"Token {REPLICATE_API_TOKEN}"}
    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.get(url, headers=headers)
        r.raise_for_status()
        return r.json()

async def call_replicate_generate(prompt: str, model_id: Optional[str], num_images: int) -> List[str]:
    if not REPLICATE_API_TOKEN:
        raise HTTPException(status_code=500, detail="REPLICATE_API_TOKEN not set")

    body = {
        "version": REPLICATE_GEN_VERSION,
        "input": {"prompt": prompt, "num_outputs": num_images}
    }
    model_path = REPLICATE_GEN_MODEL
    if model_id:
        model_path = model_id

    url = f"https://api.replicate.com/v1/models/{model_path}/predictions"
    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120) as cl:
        r = await cl.post(url, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()

    prediction_url = data["urls"]["get"]
    outputs: List[str] = []

    async with httpx.AsyncClient(timeout=120) as cl:
        for _ in range(60):
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
    pdir = user_photos_dir(user_id)
    name = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{file.filename}"
    path = os.path.join(pdir, name)
    with open(path, "wb") as f:
        f.write(await file.read())
    log.info(f"UPLOAD user={user_id} -> {path}")
    return {"ok": True, "path": path}

@app.get("/api/debug/has_photos/{user_id}")
async def api_debug_has_photos(user_id: str):
    cnt = count_user_photos(user_id)
    return {"user_id": user_id, "count": cnt, "has_photos": cnt > 0}

@app.post("/api/train")
async def api_train(user_id: str = Form(...)):
    if count_user_photos(user_id) == 0:
        raise HTTPException(status_code=400, detail="no photos uploaded")

    zip_path = build_zip_of_user_photos(user_id)
    zip_url = public_url_for_zip(zip_path)

    train = await call_replicate_training(zip_url, user_id)
    training_id = train.get("id") or train.get("uuid")
    if not training_id:
        raise HTTPException(status_code=500, detail="no training_id from replicate")

    job_id = f"job_{uuid.uuid4().hex[:8]}"
    jobs[job_id] = {"status": "running", "progress": 5, "user_id": user_id,
                    "training_id": training_id, "model_id": None}
    log.info(f"TRAIN started job={job_id} training_id={training_id} user={user_id}")
    return {"job_id": job_id, "status": "started"}

@app.get("/api/status/{job_id}")
async def api_status(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")

    training_id = j.get("training_id")
    if training_id:
        try:
            st = await get_replicate_training_status(training_id)
            state = st.get("status") or st.get("state")
            out = st.get("output") or {}
            model = out.get("model") or out.get("id")

            if state:
                j["status"] = state
                j["progress"] = _pct_from_replicate_status(state)

            if model:
                j["model_id"] = model
        except Exception as e:
            log.warning(f"status fetch failed for {job_id}: {e!r}")

    return {"job_id": job_id, "status": j.get("status"),
            "progress": j.get("progress", 0), "model_id": j.get("model_id")}

@app.post("/api/generate")
async def api_generate(
    request: Request,
    user_id: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    num_images: Optional[int] = Form(None),
    job_id: Optional[str] = Form(None),
):
    if request.headers.get("content-type", "").lower().startswith("application/json"):
        body = await request.json()
        user_id = body.get("user_id")
        prompt = body.get("prompt")
        num_images = body.get("num_images", 1)
        job_id = body.get("job_id")

    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    model_id = None
    if job_id and job_id in jobs:
        model_id = jobs[job_id].get("model_id")

    urls = await call_replicate_generate(prompt=prompt, model_id=model_id,
                                         num_images=int(num_images or 1))
    return {"images": urls}
