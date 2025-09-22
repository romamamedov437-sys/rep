# main.py

import os, io, zipfile, uuid, time, logging, asyncio
from typing import Dict, Any, Optional, List

import httpx
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Response
from fastapi.staticfiles import StaticFiles
from telegram import Update
from telegram.error import TelegramError

from bot import tg_app

# ---------- ENV ----------
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").rstrip("/")
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "hook").strip()
BACKEND_ROOT = (os.getenv("BACKEND_ROOT") or "").rstrip("/")

REPLICATE_API_TOKEN = (os.getenv("REPLICATE_API_TOKEN") or "").strip()

# Тренер fast-flux-trainer
REPLICATE_TRAIN_OWNER = os.getenv("REPLICATE_TRAIN_OWNER", "replicate").strip()
REPLICATE_TRAIN_MODEL = os.getenv("REPLICATE_TRAIN_MODEL", "fast-flux-trainer").strip()

# ❗ ЖЁСТКО фиксируем ДЛИННУЮ версию (commit) — то, что давал 8b1079...
FAST_FLUX_VERSION_FIXED = "replicate/fast-flux-trainer:8b10794665aed907bb98a1a5324cd1d3a8bea0e9b31e65210967fb9c9e2e08ed"

REPLICATE_USERNAME = (os.getenv("REPLICATE_USERNAME") or "").strip()

# qwen-путь (оставляем на будущее — НЕ используется сейчас)
REPLICATE_TRAINER_VERSION_ID = (os.getenv("REPLICATE_TRAINER_VERSION_ID") or "").strip()
TRAIN_STEPS_DEFAULT = int(os.getenv("TRAIN_STEPS_DEFAULT", "800"))

# Генерация — как у тебя
REPLICATE_GEN_MODEL = os.getenv("REPLICATE_GEN_MODEL", "black-forest-labs/FLUX.1-schnell").strip()
REPLICATE_GEN_VERSION = os.getenv("REPLICATE_GEN_VERSION", "latest").strip()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("web")

# ---------- APP ----------
app = FastAPI()

# storage
BASE_DIR = "/opt/render/project/src"
DATA_DIR = os.path.join(BASE_DIR, "data")
USERS_DIR = os.path.join(DATA_DIR, "users")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(USERS_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")

jobs: Dict[str, Dict[str, Any]] = {}

# ============ TG WEBHOOK ============
@app.on_event("startup")
async def startup_event():
    await tg_app.initialize()
    await tg_app.start()
    if PUBLIC_URL:
        hook_url = f"{PUBLIC_URL}/webhook/{WEBHOOK_SECRET}"
        try:
            await tg_app.bot.delete_webhook(drop_pending_updates=True)
            await tg_app.bot.set_webhook(hook_url, allowed_updates=["message", "callback_query"])
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

@app.get("/")
async def root():
    return {"ok": True, "service": "replicate-bridge", "has_public": bool(PUBLIC_URL)}

@app.head("/")
async def head_root():
    return Response(status_code=200)

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/debug/env")
async def debug_env():
    def mask(v: Optional[str]) -> Optional[str]:
        if not v: return v
        return v[:4] + "*" * max(0, len(v)-8) + v[-4:] if len(v) > 8 else "*"*len(v)
    return {
        "OWNER": REPLICATE_TRAIN_OWNER,
        "MODEL": REPLICATE_TRAIN_MODEL,
        "FIXED_VERSION": FAST_FLUX_VERSION_FIXED,
        "USERNAME": REPLICATE_USERNAME,
        "API_TOKEN": mask(REPLICATE_API_TOKEN),
        "GEN_MODEL": REPLICATE_GEN_MODEL,
        "GEN_VERSION": REPLICATE_GEN_VERSION,
    }

# ============ WEBHOOK ============
@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
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
    return sum(1 for name in os.listdir(pdir) if os.path.isfile(os.path.join(pdir, name)))

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
    if state in ("starting", "queued", "pending"): return 5
    if state in ("processing", "running"): return 50
    if state in ("succeeded", "completed", "complete"): return 100
    if state in ("failed", "canceled", "cancelled", "error"): return 100
    return 0

def _extract_version_hash_from_pointer(pointer: str) -> str:
    """
    pointer может быть:
      - 'replicate/fast-flux-trainer:56cb4a64'  -> '56cb4a64'
      - 'replicate/fast-flux-trainer:<longhash>' -> '<longhash>'
      - просто '<hash>'
    """
    if not pointer:
        return ""
    if ":" in pointer:
        return pointer.split(":")[-1].strip()
    return pointer.strip()

# ---- ЗАПУСК ТРЕНИРОВКИ (надёжный с fallbacks) ----
async def call_replicate_training(images_zip_url: str, user_id: str) -> Dict[str, Any]:
    """
    Порядок попыток:
      1) POST https://api.replicate.com/v1/trainings                        (payload с "version")
      2) POST https://api.replicate.com/v1/models/{owner}/{model}/trainings (payload с "version")
      3) POST https://api.replicate.com/v1/models/{owner}/{model}/versions/{version_hash}/trainings (без "version" в теле)
    """
    if not REPLICATE_API_TOKEN:
        raise HTTPException(500, detail="REPLICATE_API_TOKEN not set")

    owner = REPLICATE_TRAIN_OWNER
    model = REPLICATE_TRAIN_MODEL
    version_pointer = FAST_FLUX_VERSION_FIXED
    version_hash = _extract_version_hash_from_pointer(version_pointer)

    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }

    # базовый инпут
    base_input: Dict[str, Any] = {
        "input_images": images_zip_url,
        "images_zip": images_zip_url,
        "steps": TRAIN_STEPS_DEFAULT,
    }

    # кандидатные URL'ы
    urls_and_payloads: List[Dict[str, Any]] = []

    # 1) Global /v1/trainings
    p1: Dict[str, Any] = {"version": version_pointer, "input": dict(base_input)}
    if REPLICATE_USERNAME:
        p1["destination"] = f"{REPLICATE_USERNAME}/user-{user_id}-fluxlora"
    urls_and_payloads.append({"url": "https://api.replicate.com/v1/trainings", "payload": p1})

    # 2) Model-scoped /models/{owner}/{model}/trainings
    p2: Dict[str, Any] = {"version": version_pointer, "input": dict(base_input)}
    if REPLICATE_USERNAME:
        p2["destination"] = f"{REPLICATE_USERNAME}/user-{user_id}-fluxlora"
    urls_and_payloads.append({"url": f"https://api.replicate.com/v1/models/{owner}/{model}/trainings", "payload": p2})

    # 3) Version-scoped /models/{owner}/{model}/versions/{hash}/trainings (без "version")
    p3: Dict[str, Any] = {"input": dict(base_input)}
    if REPLICATE_USERNAME:
        p3["destination"] = f"{REPLICATE_USERNAME}/user-{user_id}-fluxlora"
    urls_and_payloads.append({"url": f"https://api.replicate.com/v1/models/{owner}/{model}/versions/{version_hash}/trainings", "payload": p3})

    async with httpx.AsyncClient(timeout=180) as cl:
        last_text = ""
        last_code = 0
        for attempt, item in enumerate(urls_and_payloads, 1):
            url = item["url"]
            payload = item["payload"]
            try:
                r = await cl.post(url, headers=headers, json=payload)
                if r.status_code >= 400:
                    log.error("Replicate TRAIN attempt %d failed %s: %s", attempt, r.status_code, r.text)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                last_text = e.response.text
                last_code = e.response.status_code
                # 404 — пробуем следующий вариант
                if last_code == 404:
                    continue
                # прочие ошибки — сразу бросаем
                raise HTTPException(status_code=500, detail=f"replicate train failed ({last_code}): {last_text}")
            except Exception as e:
                last_text = repr(e)
                last_code = 0
                # идём дальше
                continue

    # если все три варианта не прошли
    raise HTTPException(status_code=500, detail=f"replicate train failed (exhausted urls), last={last_code} {last_text}")

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

    body = {"input": {"prompt": prompt, "num_outputs": num_images}}
    model_path = model_id or REPLICATE_GEN_MODEL
    url = f"https://api.replicate.com/v1/models/{model_path}/predictions"
    headers = {"Authorization": f"Token {REPLICATE_API_TOKEN}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=180) as cl:
        r = await cl.post(url, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()

        prediction_url = data["urls"]["get"]
        outputs: List[str] = []
        for _ in range(60):
            rr = await cl.get(prediction_url, headers=headers)
            rr.raise_for_status()
            dd = rr.json()
            status = dd.get("status")
            if status == "succeeded":
                outs = dd.get("output") or []
                outputs = [str(x) for x in (outs if isinstance(outs, list) else [outs])]
                break
            elif status in ("failed", "canceled"):
                raise HTTPException(status_code=500, detail=f"replicate generation {status}")
            await asyncio.sleep(2)

    return outputs

# ============ API ============
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

    train = await call_replicate_training(zip_url, str(user_id))
    training_id = train.get("id") or train.get("uuid")
    if not training_id:
        raise HTTPException(status_code=500, detail="no training_id from replicate")

    job_id = f"job_{uuid.uuid4().hex[:8]}"
    jobs[job_id] = {"status": "running", "progress": 5, "user_id": user_id, "training_id": training_id, "model_id": None}
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
            model = out.get("model") or out.get("id") or st.get("destination")
            if state:
                j["status"] = state
                j["progress"] = _pct_from_replicate_status(state)
            if model:
                j["model_id"] = model
        except Exception as e:
            log.warning(f"status fetch failed for {job_id}: {e!r}")

    return {"job_id": job_id, "status": j.get("status"), "progress": j.get("progress", 0), "model_id": j.get("model_id")}

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

    urls = await call_replicate_generate(prompt=prompt, model_id=model_id, num_images=int(num_images or 1))
    return {"images": urls}
