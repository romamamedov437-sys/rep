# main.py
import os, io, zipfile, uuid, time, logging, asyncio, base64, json
from typing import Dict, Any, Optional, List, Tuple

import httpx
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Response
from fastapi.staticfiles import StaticFiles
from telegram import Update
from telegram.error import TelegramError

from bot import tg_app, get_user, save_user

# ---------- ENV ----------
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").rstrip("/")
WEBHOOK_SECRET = (os.getenv("WEBHOOK_SECRET") or "hook").strip()
BACKEND_ROOT = (os.getenv("BACKEND_ROOT") or "").rstrip("/")

REPLICATE_API_TOKEN = (os.getenv("REPLICATE_API_TOKEN") or "").strip()

# тренер
REPLICATE_TRAIN_OWNER = os.getenv("REPLICATE_TRAIN_OWNER", "replicate").strip()
REPLICATE_TRAIN_MODEL = os.getenv("REPLICATE_TRAIN_MODEL", "fast-flux-trainer").strip()

# фиксированная версия тренера
FAST_FLUX_VERSION_FIXED = "replicate/fast-flux-trainer:8b10794665aed907bb98a1a5324cd1d3a8bea0e9b31e65210967fb9c9e2e08ed"

REPLICATE_USERNAME = (os.getenv("REPLICATE_USERNAME") or "").strip()
TRAIN_STEPS_DEFAULT = int(os.getenv("TRAIN_STEPS_DEFAULT", "800"))

# ---------- ГЕНЕРАЦИЯ ----------
REPLICATE_GEN_MODEL = os.getenv("REPLICATE_GEN_MODEL", "black-forest-labs/flux-1.1-dev").strip()
REPLICATE_GEN_VERSION = os.getenv("REPLICATE_GEN_VERSION", "latest").strip()
FLUX_FAST_MODEL = "black-forest-labs/flux-1.1-dev"

# ---------- YOOKASSA (PAYMENTS) ----------
YOOKASSA_SHOP_ID = (os.getenv("YOOKASSA_SHOP_ID") or "").strip()
YOOKASSA_SECRET_KEY = (os.getenv("YOOKASSA_SECRET_KEY") or "").strip()
YOOKASSA_API_BASE = (os.getenv("YOOKASSA_API_BASE") or "https://api.yookassa.ru").rstrip("/")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("web")

# ---------- APP ----------
app = FastAPI()

# Персистентные директории (Render)
BASE_DIR = os.getenv("DATA_DIR", "/var/data")
DATA_DIR = BASE_DIR
USERS_DIR = os.path.join(DATA_DIR, "users")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
PAY_DB_PATH = os.path.join(DATA_DIR, "payments.json")
os.makedirs(USERS_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

def _pay_db_load() -> Dict[str, Any]:
    if not os.path.exists(PAY_DB_PATH):
        return {}
    try:
        with open(PAY_DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _pay_db_save(db: Dict[str, Any]) -> None:
    tmp = PAY_DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PAY_DB_PATH)

app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")

jobs: Dict[str, Dict[str, Any]] = {}
PAYMENTS: Dict[str, Any] = _pay_db_load()  # payment_id -> info

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

# 🔎 Отладочная статистика API
@app.get("/debug/stats")
async def debug_stats():
    try:
        users = 0
        photos_total = 0
        if os.path.isdir(USERS_DIR):
            for name in os.listdir(USERS_DIR):
                up = os.path.join(USERS_DIR, name)
                if os.path.isdir(up):
                    users += 1
                    pdir = os.path.join(up, "photos")
                    if os.path.isdir(pdir):
                        photos_total += sum(1 for fn in os.listdir(pdir) if os.path.isfile(os.path.join(pdir, fn)))

        uploads_files = sum(1 for _ in os.scandir(UPLOADS_DIR)) if os.path.isdir(UPLOADS_DIR) else 0
        jobs_count = len(jobs)
        by_status: Dict[str, int] = {}
        for j in jobs.values():
            st = (j.get("status") or "").lower()
            by_status[st] = by_status.get(st, 0) + 1

        def _dir_size(path: str) -> int:
            total = 0
            if not os.path.isdir(path):
                return 0
            for rootd, _, files in os.walk(path):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(rootd, f))
                    except Exception:
                        pass
            return total

        sizes = {
            "users_dir_bytes": _dir_size(USERS_DIR),
            "uploads_dir_bytes": _dir_size(UPLOADS_DIR),
        }

        return {
            "ok": True,
            "users": users,
            "photos_total": photos_total,
            "uploads_files": uploads_files,
            "jobs": jobs_count,
            "jobs_by_status": by_status,
            "sizes": sizes,
            "payments_total": len(PAYMENTS),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"stats_error: {e!r}")

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
        "GEN_FALLBACK": FLUX_FAST_MODEL,
        "YOOKASSA_SHOP_ID_SET": bool(YOOKASSA_SHOP_ID),
        "YOOKASSA_SECRET_SET": bool(YOOKASSA_SECRET_KEY),
        "YOOKASSA_API_BASE": YOOKASSA_API_BASE,
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
    if not pointer:
        return ""
    if ":" in pointer:
        return pointer.split(":")[-1].strip()
    return pointer.strip()

# ---- ТРЕНИРОВКА ----
async def call_replicate_training(images_zip_url: str, user_id: str) -> Dict[str, Any]:
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

    base_input: Dict[str, Any] = {
        "input_images": images_zip_url,
        "images_zip": images_zip_url,
        "steps": TRAIN_STEPS_DEFAULT,
    }

    DESTINATION_MODEL = "romamamedov437-sys/user-6064931063-lora"

    urls_and_payloads: List[Dict[str, Any]] = []
    p1: Dict[str, Any] = {"version": version_pointer, "input": dict(base_input), "destination": DESTINATION_MODEL}
    urls_and_payloads.append({"url": "https://api.replicate.com/v1/trainings", "payload": p1})
    p2: Dict[str, Any] = {"version": version_pointer, "input": dict(base_input), "destination": DESTINATION_MODEL}
    urls_and_payloads.append({"url": f"https://api.replicate.com/v1/models/{owner}/{model}/trainings", "payload": p2})
    p3: Dict[str, Any] = {"input": dict(base_input), "destination": DESTINATION_MODEL}
    urls_and_payloads.append({"url": f"https://api.replicate.com/v1/models/{owner}/{model}/versions/{version_hash}/trainings", "payload": p3})

    async with httpx.AsyncClient(timeout=180) as cl:
        for attempt, item in enumerate(urls_and_payloads, 1):
            try:
                r = await cl.post(item["url"], headers=headers, json=item["payload"])
                if r.status_code >= 400:
                    log.error("Replicate TRAIN attempt %d failed %s: %s", attempt, r.status_code, r.text)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                last_code = e.response.status_code
                if last_code == 404:
                    continue
                raise HTTPException(status_code=500, detail=f"replicate train failed ({last_code}): {e.response.text}")
            except Exception:
                continue

    raise HTTPException(status_code=500, detail=f"replicate train failed (exhausted urls)")

async def get_replicate_training_status(training_id: str) -> Dict[str, Any]:
    if not REPLICATE_API_TOKEN:
        raise HTTPException(status_code=500, detail="REPLICATE_API_TOKEN not set")
    url = f"https://api.replicate.com/v1/trainings/{training_id}"
    headers = {"Authorization": f"Token {REPLICATE_API_TOKEN}"}
    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.get(url, headers=headers)
        r.raise_for_status()
        return r.json()

# ------------- ГЕНЕРАЦИЯ -------------
def _split_model_and_version(model_path: str) -> Tuple[str, Optional[str]]:
    model_path = (model_path or "").strip()
    if ":" in model_path:
        m, v = model_path.split(":", 1)
        return m.strip(), v.strip()
    return model_path, None

async def _get_latest_version_hash(client: httpx.AsyncClient, model_name: str, headers: Dict[str, str]) -> str:
    url = f"https://api.replicate.com/v1/models/{model_name}/versions"
    r = await client.get(url, headers=headers)
    r.raise_for_status()
    data = r.json()
    results = data.get("results") or []
    if not results:
        raise HTTPException(status_code=500, detail=f"No versions found for model '{model_name}'")
    return results[0].get("id") or results[0].get("version")

async def _post_prediction_via_version(client: httpx.AsyncClient, version_hash: str, prompt: str, num_images: int, headers: Dict[str, str]) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "version": version_hash,
        "input": {"prompt": prompt, "num_outputs": int(num_images or 1)}
    }
    r = await client.post("https://api.replicate.com/v1/predictions", headers=headers, json=body)
    r.raise_for_status()
    return r.json()

async def _resolve_model_and_version(client: httpx.AsyncClient, base_model: str, headers: Dict[str, str]) -> Tuple[str, str]:
    model_wo_ver, ver_from_id = _split_model_and_version(base_model)
    version_hash = ver_from_id
    if not version_hash:
        env_ver = (REPLICATE_GEN_VERSION or "").strip()
        if env_ver and env_ver.lower() != "latest":
            version_hash = env_ver
    if not version_hash:
        version_hash = await _get_latest_version_hash(client, model_wo_ver, headers)
    return model_wo_ver, version_hash

async def call_replicate_generate(prompt: str, model_id: Optional[str], num_images: int) -> List[str]:
    if not REPLICATE_API_TOKEN:
        raise HTTPException(status_code=500, detail="REPLICATE_API_TOKEN not set")

    base_model = (model_id or REPLICATE_GEN_MODEL or FLUX_FAST_MODEL).strip()
    headers = {"Authorization": f"Token {REPLICATE_API_TOKEN}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=180) as cl:
        try:
            _, version_hash = await _resolve_model_and_version(cl, base_model, headers)
            data = await _post_prediction_via_version(cl, version_hash, prompt, int(num_images or 1), headers)
        except httpx.HTTPStatusError as e:
            log.warning("Primary model '%s' failed (%s). Trying fallback '%s'", base_model, e.response.status_code if e.response else "?", FLUX_FAST_MODEL)
            _, version_hash = await _resolve_model_and_version(cl, FLUX_FAST_MODEL, headers)
            data = await _post_prediction_via_version(cl, version_hash, prompt, int(num_images or 1), headers)

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
            elif status in ("failed", "canceled", "cancelled", "error"):
                err = dd.get("error") or status
                raise HTTPException(status_code=500, detail=f"replicate generation failed: {err}")
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

# ---------------- PAYMENTS (YooKassa) ----------------
def _yk_auth_header() -> str:
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        raise HTTPException(status_code=500, detail="YOOKASSA credentials not set in environment")
    raw = f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")

def _rub(v: int) -> Dict[str, str]:
    return {"value": f"{v:.2f}", "currency": "RUB"}

def _pay_store(payment_id: str, payload: Dict[str, Any]):
    PAYMENTS[payment_id] = payload
    _pay_db_save(PAYMENTS)

async def _notify_user_credit(user_id: int, qty: int, amount: int):
    try:
        await tg_app.bot.send_message(
            chat_id=user_id,
            text=f"✅ Оплата прошла: <b>{amount} ₽</b>. Начислено: <b>{qty}</b> генераций.",
            parse_mode="HTML"
        )
    except Exception as e:
        log.warning(f"notify user failed: {e!r}")

# принимает JSON/FORM + таймауты/ретраи/прокси
@app.post("/api/pay")
async def api_pay_create(request: Request):
    """
    Создать платёж YooKassa и вернуть confirmation_url.
    Принимает:
      - application/json: { user_id:int, qty:int, amount:int, title:str }
      - либо form-data с теми же полями
    """
    body: Dict[str, Any] = {}
    ctype = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in ctype:
            body = await request.json()
        else:
            form = await request.form()
            body = {k: form.get(k) for k in ("user_id", "qty", "amount", "title")}
    except Exception:
        body = {}

    user_id = int((body.get("user_id") or 0))
    qty = int((body.get("qty") or 0))
    amount_raw = body.get("amount") or 0
    try:
        amount = int(float(amount_raw))
    except Exception:
        amount = int(amount_raw or 0)
    title = (str(body.get("title") or "").strip()) or f"{qty} генераций"

    if not (user_id and qty and amount):
        raise HTTPException(400, "invalid payment params")

    idemp = f"yk_{uuid.uuid4().hex}"
    headers = {
        "Authorization": _yk_auth_header(),
        "Idempotence-Key": idemp,
        "Content-Type": "application/json",
    }
    payload = {
        "amount": _rub(amount),
        "capture": True,
        "description": f"User {user_id}: {title}",
        "confirmation": {"type": "redirect", "return_url": PUBLIC_URL or "https://t.me"},
        "metadata": {"user_id": user_id, "qty": qty, "amount": amount},
    }

    timeout = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=10.0)
    last_err = None
    url = f"{YOOKASSA_API_BASE}/v3/payments"

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=timeout, http2=False, trust_env=True) as cl:
                r = await cl.post(url, headers=headers, json=payload)
                if r.status_code >= 400:
                    raise HTTPException(r.status_code, f"yookassa create failed: {r.text}")
                data = r.json()
                payment_id = data.get("id")
                confirmation_url = (data.get("confirmation") or {}).get("confirmation_url")
                if not payment_id or not confirmation_url:
                    raise HTTPException(500, "yookassa response invalid")
                _pay_store(payment_id, {
                    "user_id": user_id,
                    "qty": qty,
                    "amount": amount,
                    "status": "pending",
                    "created_at": time.time(),
                })
                return {"payment_id": payment_id, "confirmation_url": confirmation_url}
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RequestError) as e:
            last_err = e
            wait = 1.5 * (attempt + 1)
            log.warning(f"YooKassa create timeout (try {attempt+1}/3), retry in {wait}s: {e!r}")
            await asyncio.sleep(wait)

    raise HTTPException(status_code=504, detail=f"yookassa create timeout: {last_err!r}")

@app.get("/api/pay/status")
async def api_pay_status(payment_id: str):
    headers = {"Authorization": _yk_auth_header()}
    async with httpx.AsyncClient(timeout=20, http2=False, trust_env=True) as cl:
        r = await cl.get(f"{YOOKASSA_API_BASE}/v3/payments/{payment_id}", headers=headers)
        if r.status_code >= 400:
            raise HTTPException(r.status_code, f"yookassa status failed: {r.text}")
        data = r.json()
    status = data.get("status")
    meta = (data.get("metadata") or {})
    if status == "succeeded":
        stored = PAYMENTS.get(payment_id) or {}
        if stored.get("status") != "succeeded":
            user_id = int(meta.get("user_id") or stored.get("user_id") or 0)
            qty = int(meta.get("qty") or stored.get("qty") or 0)
            amount = int(float((data.get("amount") or {}).get("value") or stored.get("amount") or 0))
            if user_id and qty:
                st = get_user(user_id)
                st.balance += qty
                st.paid_any = True
                save_user(st)
                if st.referred_by:
                    ref = get_user(st.referred_by)
                    ref_gain = round(amount * 0.20, 2)
                    ref.ref_earn_total += ref_gain
                    ref.ref_earn_ready += ref_gain
                    save_user(ref)
                PAYMENTS[payment_id] = {**stored, "status": "succeeded"}
                _pay_db_save(PAYMENTS)
                await _notify_user_credit(user_id, qty, amount)
    return {"payment_id": payment_id, "status": status}

# ============ TRAIN/STATUS/GENERATE ============
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
    jobs[job_id] = {
        "status": "running",
        "progress": 5,
        "user_id": user_id,
        "training_id": training_id,
        "model_id": None
    }
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
            model = out.get("version") or out.get("model") or out.get("id") or st.get("destination")
            if state:
                j["status"] = state
                j["progress"] = _pct_from_replicate_status(state)
            if model:
                j["model_id"] = model
        except Exception as e:
            logging.getLogger("web").warning(f"status fetch failed for {job_id}: {e!r}")

    return {"job_id": job_id, "status": j.get("status"), "progress": j.get("progress", 0), "model_id": j.get("model_id")}

@app.post("/api/ggenerate")
async def api_generate_alias(request: Request,
                             user_id: Optional[str] = Form(None),
                             prompt: Optional[str] = Form(None),
                             num_images: Optional[int] = Form(None),
                             job_id: Optional[str] = Form(None)):
    return await api_generate(request, user_id, prompt, num_images, job_id)

@app.post("/api/generate")
async def api_generate(
    request: Request,
    user_id: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    num_images: Optional[int] = Form(None),
    job_id: Optional[str] = Form(None),
):
    # поддержка application/json
    if (request.headers.get("content-type") or "").lower().startswith("application/json"):
        body = await request.json()
        user_id = body.get("user_id")
        prompt = body.get("prompt")
        num_images = body.get("num_images", 1)
        job_id = body.get("job_id")

    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    # 1) если есть job -> берём модель из jobs
    model_id = None
    if job_id and job_id in jobs:
        model_id = jobs[job_id].get("model_id")

    # 2) если job нет/утерян — пробуем модель пользователя из персистентной БД
    if not model_id and user_id:
        try:
            st = get_user(int(user_id))
            if st and getattr(st, "model_id", None):
                model_id = st.model_id
        except Exception:
            pass

    urls = await call_replicate_generate(prompt=prompt, model_id=(model_id or None), num_images=int(num_images or 1))
    return {"images": urls}
