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

PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").rstrip("/")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
REPLICATE_TRAIN_OWNER = os.getenv("REPLICATE_TRAIN_OWNER", "replicate").strip()
REPLICATE_TRAIN_MODEL = os.getenv("REPLICATE_TRAIN_MODEL", "fast-flux-trainer").strip()
REPLICATE_TRAIN_VERSION = (os.getenv("REPLICATE_TRAIN_VERSION") or "").strip()
REPLICATE_USERNAME = os.getenv("REPLICATE_USERNAME") or ""
REPLICATE_INFER_VERSION = os.getenv("REPLICATE_INFER_VERSION")
REPLICATE_DEFAULT_MODEL = os.getenv("REPLICATE_DEFAULT_MODEL", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUTS_DIR = os.path.join(BASE_DIR, "outputs")
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(OUTPUTS_DIR, exist_ok=True)

JOBS: Dict[str, Dict[str, Any]] = {}

def _require_env(name: str):
    val = os.getenv(name)
    if not val:
        raise HTTPException(500, detail=f"ENV {name} is required")
    return val

def _public_url_for_local_path(local_path: str) -> str:
    if not PUBLIC_URL:
        raise HTTPException(500, detail="PUBLIC_URL is not set")
    if local_path.startswith(UPLOADS_DIR):
        rel = local_path.replace(UPLOADS_DIR, "").lstrip("/\\")
        return f"{PUBLIC_URL}/uploads/{rel.replace(os.sep, '/')}"
    if local_path.startswith(OUTPUTS_DIR):
        rel = local_path.replace(OUTPUTS_DIR, "").lstrip("/\\")
        return f"{PUBLIC_URL}/outputs/{rel.replace(os.sep, '/')}"
    raise HTTPException(500, detail="File is not in uploads/outputs")

def _zip_user_photos(user_id: str) -> str:
    user_photos_dir = os.path.join(UPLOADS_DIR, user_id, "photos")
    if not os.path.isdir(user_photos_dir):
        raise HTTPException(400, detail="No photos uploaded for this user")
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

# ---- авто-latest версия тренера ----
async def _resolve_trainer_version_pointer() -> str:
    if REPLICATE_TRAIN_VERSION:
        return (
            REPLICATE_TRAIN_VERSION
            if ":" in REPLICATE_TRAIN_VERSION
            else f"{REPLICATE_TRAIN_OWNER}/{REPLICATE_TRAIN_MODEL}:{REPLICATE_TRAIN_VERSION}"
        )
    url = f"https://api.replicate.com/v1/models/{REPLICATE_TRAIN_OWNER}/{REPLICATE_TRAIN_MODEL}"
    headers = {"Authorization": f"Token {_require_env('REPLICATE_API_TOKEN')}"}
    async with httpx.AsyncClient(timeout=30) as cl:
        r = await cl.get(url, headers=headers)
        r.raise_for_status()
        j = r.json()
        vid = (j.get("versions") or [{}])[0].get("id")
        if not vid:
            raise HTTPException(500, detail="Cannot get latest trainer version")
        return f"{REPLICATE_TRAIN_OWNER}/{REPLICATE_TRAIN_MODEL}:{vid}"

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
    model_id: Optional[str] = None
    num_images: int = Field(default=1, ge=1, le=4)
    width: int = 1024
    height: int = 1024
    steps: int = 30
    guidance_scale: float = 7.5

class GenResp(BaseModel):
    images: List[str]

@router.post("/upload_photo")
async def upload_photo(user_id: str = Form(...), file: UploadFile = File(...)):
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
    _require_env("REPLICATE_API_TOKEN")
    _require_env("REPLICATE_USERNAME")  # используется в destination

    zip_path = _zip_user_photos(str(user_id))
    dataset_url = _public_url_for_local_path(zip_path)

    version_pointer = await _resolve_trainer_version_pointer()
    destination = f"{REPLICATE_USERNAME}/sdxl-user-{user_id}"

    payload = {
        "version": version_pointer,
        "destination": destination,
        "input": {"images_zip": dataset_url, "steps": 800},
    }
    headers = {"Authorization": f"Token {REPLICATE_API_TOKEN}", "Content-Type": "application/json"}
    url = f"https://api.replicate.com/v1/models/{REPLICATE_TRAIN_OWNER}/{REPLICATE_TRAIN_MODEL}/trainings"

    async with httpx.AsyncClient(timeout=120) as cl:
        r = await cl.post(url, json=payload, headers=headers)
        if r.status_code >= 400:
            log.error(f"TRAIN ERROR {r.status_code}: {r.text}")
            raise HTTPException(r.status_code, detail=r.text)
        data = r.json()

    job_id = data.get("id") or f"job_{uuid.uuid4().hex[:8]}"
    JOBS[job_id] = {"status": data.get("status", "starting"), "progress": 0, "user_id": str(user_id), "model_id": None, "raw": data}
    log.info(f"TRAIN: job_id={job_id} user={user_id} -> created")
    return TrainResp(job_id=job_id)

@router.get("/status/{job_id}", response_model=StatusResp)
async def status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, detail="job not found")
    headers = {"Authorization": f"Token {_require_env('REPLICATE_API_TOKEN')}"}
    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.get(f"https://api.replicate.com/v1/trainings/{job_id}", headers=headers)

    if r.status_code == 404:
        job = JOBS[job_id]
        return StatusResp(job_id=job_id, status=job["status"], progress=job["progress"], model_id=job.get("model_id"), raw=job.get("raw"))
    if r.status_code >= 400:
        raise HTTPException(r.status_code, detail=r.text)

    data = r.json()
    status = data.get("status", "unknown")
    progress = 100 if status in ("succeeded", "completed", "success") else 0
    model_id = data.get("destination") or (data.get("output") or {}).get("model")

    JOBS[job_id].update({"status": status, "progress": progress, "model_id": model_id, "raw": data})
    return StatusResp(job_id=job_id, status=status, progress=progress, model_id=model_id, raw=data)

@router.post("/generate", response_model=GenResp)
async def generate(req: GenReq):
    headers = {"Authorization": f"Token {_require_env('REPLICATE_API_TOKEN')}", "Content-Type": "application/json"}
    payload: Dict[str, Any] = {
        "input": {
            "prompt": req.prompt,
            "num_outputs": req.num_images,
            "width": req.width,
            "height": req.height,
            "num_inference_steps": req.steps,
            "guidance_scale": req.guidance_scale,
        }
    }
    async with httpx.AsyncClient(timeout=300) as cl:
        if req.model_id:
            url = f"https://api.replicate.com/v1/models/{req.model_id}/predictions"
            r = await cl.post(url, json=payload, headers=headers)
        else:
            infer_version = _require_env("REPLICATE_INFER_VERSION")
            payload["version"] = infer_version
            r = await cl.post("https://api.replicate.com/v1/predictions", json=payload, headers=headers)

        if r.status_code >= 400:
            log.error(f"GENERATE ERROR {r.status_code}: {r.text}")
            raise HTTPException(r.status_code, detail=r.text)

        pred = r.json()
        get_url = pred.get("urls", {}).get("get")
        final = pred
        for _ in range(120):
            if final.get("status") in ("succeeded", "failed", "canceled"):
                break
            res = await cl.get(get_url, headers=headers)
            final = res.json()
            time.sleep(1)

        if final.get("status") != "succeeded":
            raise HTTPException(500, detail=f"prediction failed: {final.get('error') or final.get('status')}")
        output = final.get("output") or []
        if not isinstance(output, list):
            output = [output]
        return GenResp(images=output or [])
