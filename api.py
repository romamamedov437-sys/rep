import os
import io
import zipfile
import uuid
import time
import shutil
import logging
from typing import Optional, Dict, Any, List

import httpx
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw

router = APIRouter()
log = logging.getLogger("api")
logging.basicConfig(level=logging.INFO)

# ========= ENV / CONFIG =========
# ВАЖНО: эти значения задай в Render → Environment
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").rstrip("/")         # https://your-service.onrender.com
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")           # r8_xxx...
REPLICATE_TRAIN_VERSION = os.getenv("REPLICATE_TRAIN_VERSION")   # version id тренера SDXL
REPLICATE_USERNAME = os.getenv("REPLICATE_USERNAME")             # твой username в Replicate (для destination)
REPLICATE_INFER_VERSION = os.getenv("REPLICATE_INFER_VERSION")   # версия инференса (если используешь общий SDXL)
REPLICATE_DEFAULT_MODEL = os.getenv("REPLICATE_DEFAULT_MODEL", "")  # опц.: owner/model (если используешь кастомную модель)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUTS_DIR = os.path.join(BASE_DIR, "outputs")
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(OUTPUTS_DIR, exist_ok=True)

# ========= IN-MEMORY JOBS =========
# Храним статусы задач обучения (Replicate trainings)
JOBS: Dict[str, Dict[str, Any]] = {}

# ========= UTILS =========
def _require_env(name: str):
    val = os.getenv(name)
    if not val:
        raise HTTPException(500, detail=f"ENV {name} is required")
    return val

def _public_url_for_local_path(local_path: str) -> str:
    """
    Преобразуем локальный путь (внутри uploads/outputs) в публичный URL,
    чтобы Replicate мог скачать ZIP/картинки.
    """
    if not PUBLIC_URL:
        raise HTTPException(500, detail="PUBLIC_URL is not set")
    # local_path должен лежать в uploads или outputs
    rel = None
    if local_path.startswith(UPLOADS_DIR):
        rel = local_path.replace(UPLOADS_DIR, "").lstrip("/\\")
        return f"{PUBLIC_URL}/uploads/{rel.replace(os.sep, '/')}"
    if local_path.startswith(OUTPUTS_DIR):
        rel = local_path.replace(OUTPUTS_DIR, "").lstrip("/\\")
        return f"{PUBLIC_URL}/outputs/{rel.replace(os.sep, '/')}"
    raise HTTPException(500, detail="File is not in uploads/outputs")

def _zip_user_photos(user_id: str) -> str:
    """
    Собираем все загруженные фото пользователя в zip-файл (хранится в uploads),
    чтобы передать в тренер Replicate по публичной ссылке.
    """
    user_photos_dir = os.path.join(UPLOADS_DIR, user_id, "photos")
    if not os.path.isdir(user_photos_dir):
        raise HTTPException(400, detail="No photos uploaded for this user")

    # имя zip
    zip_name = f"{user_id}_{int(time.time())}.zip"
    zip_path = os.path.join(UPLOADS_DIR, user_id, zip_name)
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(user_photos_dir):
            for fn in files:
                full_path = os.path.join(root, fn)
                arcname = os.path.relpath(full_path, user_photos_dir)
                zf.write(full_path, arcname)

    return zip_path

# ========= MODELS =========
class TrainResp(BaseModel):
    job_id: str
    status: str = "started"

class StatusResp(BaseModel):
    job_id: str
    status: str
    progress: int = 0
    model_id: Optional[str] = None
    raw: Optional[dict] = None

class GenReq(BaseModel):
    user_id: str | int
    prompt: str
    model_id: Optional[str] = None       # если есть готовая модель (выданная тренером)
    num_images: int = Field(default=1, ge=1, le=4)
    width: int = 1024
    height: int = 1024
    steps: int = 30
    guidance_scale: float = 7.5

class GenResp(BaseModel):
    images: List[str]

# ========= ROUTES =========
@router.post("/upload_photo")
async def upload_photo(user_id: str = Form(...), file: UploadFile = File(...)):
    """
    Сохраняем загруженное фото пользователя.
    """
    user_dir = os.path.join(UPLOADS_DIR, user_id, "photos")
    os.makedirs(user_dir, exist_ok=True)
    safe_name = f"{int(time.time())}_{uuid.uuid4().hex}_{file.filename}"
    dst = os.path.join(user_dir, safe_name)

    with open(dst, "wb") as f:
        content = await file.read()
        f.write(content)

    log.info(f"UPLOAD: user={user_id} saved {dst}")
    return {"ok": True, "file": _public_url_for_local_path(dst)}

@router.post("/train", response_model=TrainResp)
async def train(user_id: str = Form(...)):
    """
    Старт обучения на Replicate:
    1) собираем zip с фото пользователя
    2) создаём training через Replicate API
    3) возвращаем id задачи (job_id)
    """
    token = _require_env("REPLICATE_API_TOKEN")
    train_version = _require_env("REPLICATE_TRAIN_VERSION")
    username = _require_env("REPLICATE_USERNAME")

    # 1) архивируем фото
    zip_path = _zip_user_photos(str(user_id))
    dataset_url = _public_url_for_local_path(zip_path)

    # 2) дергаем Replicate Trainings API
    # Док: POST https://api.replicate.com/v1/trainings
    # payload может отличаться в зависимости от конкретного тренера.
    # Здесь — общий каркас для SDXL-тренера: указываем version и входы.
    destination = f"{username}/sdxl-user-{user_id}"

    payload = {
        "version": train_version,
        "destination": destination,
        "input": {
            # Поля input зависят от конкретного тренера!
            # Часто используется что-то вроде: "input_images": dataset_url
            # Для примера кладём в "input_images".
            "input_images": dataset_url,
            # Можешь добавить гиперпараметры тренировки, если тренер их принимает:
            # "steps": 800,
            # "learning_rate": 1e-4,
        },
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=120) as cl:
        r = await cl.post("https://api.replicate.com/v1/trainings", json=payload, headers=headers)
        if r.status_code >= 400:
            log.error(f"TRAIN ERROR {r.status_code}: {r.text}")
            raise HTTPException(r.status_code, detail=r.text)
        data = r.json()

    job_id = data.get("id") or f"job_{uuid.uuid4().hex[:8]}"
    # сохраняем грубый статус
    JOBS[job_id] = {
        "status": data.get("status", "starting"),
        "progress": 0,
        "user_id": str(user_id),
        "model_id": None,
        "raw": data,
    }
    log.info(f"TRAIN: job_id={job_id} user={user_id} -> created")
    return TrainResp(job_id=job_id)

@router.get("/status/{job_id}", response_model=StatusResp)
async def status(job_id: str):
    """
    Достаём статус обучения из Replicate.
    Если уже запросили ранее — обновляем.
    """
    if job_id not in JOBS:
        raise HTTPException(404, detail="job not found")

    token = _require_env("REPLICATE_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.get(f"https://api.replicate.com/v1/trainings/{job_id}", headers=headers)

    if r.status_code == 404:
        # не нашли на стороне Replicate — оставим локальный
        job = JOBS[job_id]
        return StatusResp(job_id=job_id, status=job["status"], progress=job["progress"], model_id=job.get("model_id"), raw=job.get("raw"))

    if r.status_code >= 400:
        raise HTTPException(r.status_code, detail=r.text)

    data = r.json()
    status = data.get("status", "unknown")
    # прогресс у Replicate бывает нечисловой — оставим грубо
    progress = 0
    model_id = None

    # когда тренинг завершён, Replicate возвращает информацию о выпущенной модели
    # см. поле "output" или "destination"
    if status in ("succeeded", "completed", "success"):
        # Иногда результат — это модель с тегом latest. Сохраним destination.
        model_id = data.get("destination") or (data.get("output") or {}).get("model")
        progress = 100

    JOBS[job_id].update({
        "status": status,
        "progress": progress,
        "model_id": model_id,
        "raw": data,
    })

    return StatusResp(job_id=job_id, status=status, progress=progress, model_id=model_id, raw=data)

@router.post("/generate", response_model=GenResp)
async def generate(req: GenReq):
    """
    Генерация через Replicate Predictions API.
    Варианты:
      - Если есть req.model_id (твой кастомный fine-tuned), вызываем его.
      - Иначе используем REPLICATE_INFER_VERSION (например, общий SDXL).
    """
    token = _require_env("REPLICATE_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # если прилетел model_id, пробуем вызвать его. Если нет — fallback на версию
    payload: Dict[str, Any] = {
        "input": {
            "prompt": req.prompt,
            "num_outputs": req.num_images,
            "width": req.width,
            "height": req.height,
            # имена параметров (steps/guidance_scale) могут отличаться у конкретной модели
            "num_inference_steps": req.steps,
            "guidance_scale": req.guidance_scale,
        }
    }

    async with httpx.AsyncClient(timeout=300) as cl:
        if req.model_id:
            # Запуск по модели (owner/model) — у некоторых моделей нужна версия.
            # Replicate принимает POST /v1/models/{owner}/{name}/predictions
            url = f"https://api.replicate.com/v1/models/{req.model_id}/predictions"
            r = await cl.post(url, json=payload, headers=headers)
        else:
            # Запуск по version (общая модель SDXL)
            infer_version = _require_env("REPLICATE_INFER_VERSION")
            payload["version"] = infer_version
            r = await cl.post("https://api.replicate.com/v1/predictions", json=payload, headers=headers)

        if r.status_code >= 400:
            log.error(f"GENERATE ERROR {r.status_code}: {r.text}")
            raise HTTPException(r.status_code, detail=r.text)

        pred = r.json()

        # Ждём завершения (простой опрос)
        prediction_id = pred.get("id")
        get_url = pred.get("urls", {}).get("get")
        final = pred
        for _ in range(120):  # до ~2 минут
            status = final.get("status")
            if status in ("succeeded", "failed", "canceled"):
                break
            awaitable = cl.get(get_url, headers=headers)
            res = await awaitable
            final = res.json()
            time.sleep(1)

        if final.get("status") != "succeeded":
            raise HTTPException(500, detail=f"prediction failed: {final.get('error') or final.get('status')}")

        # Replicate возвращает список url-ов (обычно CDN). Отдадим как есть.
        output = final.get("output") or []
        if not isinstance(output, list):
            output = [output]

        return GenResp(images=output or [])
