import os, uuid, time, logging, zipfile, io
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import httpx

from store import memory
from prompts import PROMPTS

# ---------- CONFIG ----------
BASE_URL = (os.getenv("PUBLIC_BASE") or "").rstrip("/")   # например https://rep-xxxxx.onrender.com
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN", "")
if not REPLICATE_API_TOKEN:
    print("⚠️  REPLICATE_API_TOKEN is empty – set it in Render ENVs")

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="PhotoFly Backend")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

log = logging.getLogger("backend")
logging.basicConfig(level=logging.INFO)

HEADERS = {"Authorization": f"Token {REPLICATE_API_TOKEN}"}

# -------- MODELS --------
class TrainResp(BaseModel):
    job_id: str
    status: str = "started"

class StatusResp(BaseModel):
    job_id: str
    status: str
    progress: int
    model_id: Optional[str] = None
    replicate_training_id: Optional[str] = None

class GenReq(BaseModel):
    user_id: int | str
    model_id: str | None = None      # если нет — возьмём из памяти для пользователя
    prompt: str
    num_images: int = Field(1, ge=1, le=4)
    width: int = 768
    height: int = 1024
    steps: int = 28
    guidance: float = 3.5

class GenResp(BaseModel):
    images: List[str]

# ---------- HELPERS ----------
def public_url(path_rel: str) -> str:
    if BASE_URL:
        return f"{BASE_URL}{path_rel}"
    # локально
    return path_rel

def save_upload(user_id: str, uf: UploadFile) -> str:
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    name = f"{int(time.time())}_{uuid.uuid4().hex[:6]}_{uf.filename}"
    fpath = os.path.join(user_dir, name)
    with open(fpath, "wb") as f:
        f.write(uf.file.read())
    return fpath

def list_user_imgs(user_id: str) -> List[str]:
    user_dir = os.path.join(UPLOAD_DIR, str(user_id))
    if not os.path.isdir(user_dir):
        return []
    files = sorted([os.path.join(user_dir, x) for x in os.listdir(user_dir) if x.lower().endswith((".jpg",".jpeg",".png",".webp"))])
    return files

def files_to_urls(files: List[str]) -> List[str]:
    urls = []
    for p in files:
        # отдаём как статику
        rel = p.split("/uploads")[-1]
        rel = "/uploads" + rel
        urls.append(public_url(rel))
    return urls

# ---------- ROUTES ----------
@app.get("/")
def root():
    return {"ok": True, "env_ok": bool(REPLICATE_API_TOKEN)}

@app.post("/api/upload_photo")
async def upload_photo(user_id: str = Form(...), file: UploadFile = File(...)):
    fpath = save_upload(user_id, file)
    return {"ok": True, "path": fpath}

@app.post("/api/train", response_model=TrainResp)
async def api_train(user_id: str = Form(...)):
    """
    Старт обучения через Replicate fast-flux-trainer.
    Сохраняем training_id и держим прогресс в памяти.
    """
    uid = str(user_id)
    imgs = list_user_imgs(uid)
    if len(imgs) < 10:
        raise HTTPException(400, detail="Нужно минимум 10 фото")

    job_id = f"job_{uuid.uuid4().hex[:8]}"
    memory.jobs[job_id] = {
        "status": "running",
        "progress": 0,
        "user_id": uid,
        "model_id": None,
        "replicate_training_id": None,
    }

    image_urls = files_to_urls(imgs)

    payload = {
        # официальный тренер FLUX от Replicate
        "version": "replicate/fast-flux-trainer",
        "input": {
            "input_images": image_urls,
            "steps": 800,                  # можешь менять
            "cache_latents": True,
            "caption_prefix": "",          # при желании
            "lr": 1e-4,
        }
    }

    async with httpx.AsyncClient(timeout=300) as cl:
        r = await cl.post("https://api.replicate.com/v1/trainings", json=payload, headers=HEADERS)
        if r.status_code >= 400:
            raise HTTPException(r.status_code, detail=r.text)
        data = r.json()

    training_id = data.get("id")
    memory.jobs[job_id]["replicate_training_id"] = training_id
    log.info(f"[train] job={job_id} training_id={training_id}")
    return TrainResp(job_id=job_id)

@app.get("/api/status/{job_id}", response_model=StatusResp)
async def api_status(job_id: str):
    job = memory.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")

    # если ещё нет training_id — просто вернём текущее
    tid = job.get("replicate_training_id")
    if not tid:
        return StatusResp(job_id=job_id, **job)

    # опрос Replicate
    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.get(f"https://api.replicate.com/v1/trainings/{tid}", headers=HEADERS)
        if r.status_code >= 400:
            # не валим, а возвращаем то что знаем
            return StatusResp(job_id=job_id, **job)
        data = r.json()

    # переводим их статусы в наш прогресс
    rep_status = data.get("status")
    if rep_status == "starting":
        job["status"] = "running"
        job["progress"] = max(job["progress"], 3)
    elif rep_status == "processing":
        job["status"] = "running"
        job["progress"] = max(job["progress"], 50)
    elif rep_status == "succeeded":
        job["status"] = "done"
        job["progress"] = 100
        job["model_id"] = data.get("output", {}).get("model") or data.get("output")  # у Replicate разные ответы по версиям
    elif rep_status in ("failed", "canceled"):
        job["status"] = "failed"
        job["progress"] = 100

    return StatusResp(job_id=job_id, **job)

@app.post("/api/generate", response_model=GenResp)
async def api_generate(req: GenReq):
    """
    Генерация через обученную модель (если model_id в памяти),
    иначе можно передать req.model_id вручную.
    """
    # найдём сохранённую модель для пользователя, если не передали явно
    model_id = req.model_id
    if not model_id:
        # поищем последнюю job по user_id
        last = None
        for k, v in memory.jobs.items():
            if v.get("user_id") == str(req.user_id) and v.get("model_id"):
                last = v
        if last:
            model_id = last["model_id"]
    if not model_id:
        raise HTTPException(400, "Модель не найдена. Сначала обучите её.")

    generate_payload = {
        "input": {
            "prompt": req.prompt,
            "width": req.width,
            "height": req.height,
            "num_outputs": req.num_images,
            "num_inference_steps": req.steps,
            "guidance_scale": req.guidance,
        }
    }

    async with httpx.AsyncClient(timeout=None) as cl:
        r = await cl.post(f"https://api.replicate.com/v1/models/{model_id}/predictions",
                          headers=HEADERS, json=generate_payload)
        if r.status_code >= 400:
            raise HTTPException(r.status_code, detail=r.text)
        pred = r.json()

        # ждём завершение
        pred_url = pred.get("urls", {}).get("get")
        imgs = []
        while True:
            pr = await cl.get(pred_url, headers=HEADERS)
            data = pr.json()
            st = data.get("status")
            if st in ("starting", "processing"):
                await httpx.AsyncClient().aclose()
                time.sleep(2)
                continue
            if st == "succeeded":
                imgs = data.get("output") or []
            else:
                raise HTTPException(500, f"prediction failed: {data}")
            break

    return GenResp(images=imgs)

# удобный список промптов
@app.get("/api/prompts")
def api_prompts():
    return {"items": PROMPTS}
