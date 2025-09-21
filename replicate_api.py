import os
import logging
import tempfile
from typing import Optional, Dict, Any, List

import replicate  # официальный SDK (синхронный)
import httpx      # для «шумных» прямых запросов

# ========= ENV =========
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
REPLICATE_TRAIN_ENDPOINT = os.getenv("REPLICATE_TRAIN_ENDPOINT", "https://api.replicate.com/v1/trainings").strip()
REPLICATE_TRAIN_OWNER = os.getenv("REPLICATE_TRAIN_OWNER", "replicate").strip()
REPLICATE_TRAIN_MODEL = os.getenv("REPLICATE_TRAIN_MODEL", "fast-flux-trainer").strip()
REPLICATE_TRAIN_VERSION = (os.getenv("REPLICATE_TRAIN_VERSION") or "").strip()

REPLICATE_GEN_MODEL = os.getenv("REPLICATE_GEN_MODEL", "black-forest-labs/FLUX.1-schnell").strip()
REPLICATE_GEN_VERSION = os.getenv("REPLICATE_GEN_VERSION", "latest").strip()

log = logging.getLogger("replicate_api")

# Инициализация клиента SDK (синхронный, это ок — мы вызываем его из async через thread-невовлечённо)
client = replicate.Client(api_token=REPLICATE_API_TOKEN) if REPLICATE_API_TOKEN else None

# ---------- ТВОИ ИСХОДНЫЕ ФУНКЦИИ (оставлены как есть) ----------
# Генерация по промпту (простая)
async def generate_image(prompt: str) -> Optional[str]:
    try:
        output = client.run(
            "stability-ai/sdxl:latest",
            input={"prompt": prompt}
        )
        return output[0] if output else None
    except Exception as e:
        print(f"Ошибка генерации: {e}")
        return None

# Обучение модели (простая заглушка)
async def start_training(photo) -> Optional[str]:
    try:
        file = await photo.get_file()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        await file.download_to_drive(tmp.name)

        training = client.trainings.create(
            version="stability-ai/sdxl:latest",
            input={"instance_prompt": "photo of person", "images": [tmp.name]}
        )
        return training.id
    except Exception as e:
        print(f"Ошибка обучения: {e}")
        return None

# ---------- ДОБАВЛЕНО: «ШУМНЫЕ»/VERBOSE ВЕРСИИ ----------

async def generate_image_verbose(prompt: str) -> Dict[str, Any]:
    """
    Возвращает подробный результат/ошибку при генерации через SDK.
    """
    if not REPLICATE_API_TOKEN:
        return {"ok": False, "where": "env", "detail": "REPLICATE_API_TOKEN not set"}
    try:
        output = client.run(
            REPLICATE_GEN_MODEL if REPLICATE_GEN_VERSION == "latest" else f"{REPLICATE_GEN_MODEL}:{REPLICATE_GEN_VERSION}",
            input={"prompt": prompt}
        )
        return {"ok": True, "images": output}
    except Exception as e:
        return {"ok": False, "where": "sdk", "error": repr(e)}

async def start_training_verbose_from_zip(images_zip_url: str) -> Dict[str, Any]:
    """
    Прямой POST в Replicate /v1/trainings — как в main.py, но отдельной функцией.
    Возвращает status_code, raw_text и json (если есть).
    """
    if not REPLICATE_API_TOKEN:
        return {"ok": False, "where": "env", "detail": "REPLICATE_API_TOKEN not set"}
    if not REPLICATE_TRAIN_VERSION:
        return {"ok": False, "where": "env", "detail": "REPLICATE_TRAIN_VERSION not set"}

    payload = {
        "version": REPLICATE_TRAIN_VERSION,
        "model": f"{REPLICATE_TRAIN_OWNER}/{REPLICATE_TRAIN_MODEL}",
        "input": {"images_zip": images_zip_url, "steps": 800}
    }
    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=120) as cl:
            r = await cl.post(REPLICATE_TRAIN_ENDPOINT, headers=headers, json=payload)
            text = r.text
            status = r.status_code
            out = {"ok": 200 <= status < 300, "status_code": status, "raw_text": text}
            try:
                out["json"] = r.json()
            except Exception:
                pass
            return out
    except httpx.HTTPStatusError as e:
        return {
            "ok": False,
            "where": "httpx.HTTPStatusError",
            "status_code": getattr(e.response, "status_code", None),
            "response_text": getattr(e.response, "text", None),
            "error": str(e),
        }
    except Exception as e:
        return {"ok": False, "where": "exception", "error": repr(e)}
