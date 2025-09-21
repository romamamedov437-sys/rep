import os
import logging
import tempfile
from typing import Optional, Dict, Any

import replicate
import httpx

# ========= ENV =========
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
REPLICATE_TRAIN_ENDPOINT = os.getenv("REPLICATE_TRAIN_ENDPOINT", "https://api.replicate.com/v1/trainings").strip()
REPLICATE_TRAIN_OWNER = os.getenv("REPLICATE_TRAIN_OWNER", "replicate").strip()
REPLICATE_TRAIN_MODEL = os.getenv("REPLICATE_TRAIN_MODEL", "fast-flux-trainer").strip()
REPLICATE_TRAIN_VERSION = (os.getenv("REPLICATE_TRAIN_VERSION") or "").strip()

REPLICATE_GEN_MODEL = os.getenv("REPLICATE_GEN_MODEL", "black-forest-labs/FLUX.1-schnell").strip()
REPLICATE_GEN_VERSION = os.getenv("REPLICATE_GEN_VERSION", "latest").strip()

log = logging.getLogger("replicate_api")

# SDK клиент
client = replicate.Client(api_token=REPLICATE_API_TOKEN) if REPLICATE_API_TOKEN else None


# ---------- авто-определение latest версии тренера ----------
async def _get_latest_trainer_version_id() -> Optional[str]:
    """
    Вернёт полное имя версии тренера owner/model:<version_id>.
    Если REPLICATE_TRAIN_VERSION задан — вернёт его как есть (поддерживаю и 'owner/model:<vid>', и просто '<vid>').
    """
    if REPLICATE_TRAIN_VERSION:
        return (
            REPLICATE_TRAIN_VERSION
            if ":" in REPLICATE_TRAIN_VERSION
            else f"{REPLICATE_TRAIN_OWNER}/{REPLICATE_TRAIN_MODEL}:{REPLICATE_TRAIN_VERSION}"
        )

    if not REPLICATE_API_TOKEN:
        return None

    url = f"https://api.replicate.com/v1/models/{REPLICATE_TRAIN_OWNER}/{REPLICATE_TRAIN_MODEL}"
    headers = {"Authorization": f"Token {REPLICATE_API_TOKEN}"}

    try:
        async with httpx.AsyncClient(timeout=30) as cl:
            r = await cl.get(url, headers=headers)
            if r.status_code != 200:
                log.error("trainer-model fetch %s: %s", r.status_code, r.text)
                return None
            j = r.json() or {}
            versions = j.get("versions") or []
            if not versions:
                return None
            latest = versions[0]
            vid = latest.get("id") or latest.get("version")
            if not vid:
                return None
            return f"{REPLICATE_TRAIN_OWNER}/{REPLICATE_TRAIN_MODEL}:{vid}"
    except Exception as e:
        log.error("latest-trainer-version error: %r", e)
        return None


# ---------- ТВОИ БАЗОВЫЕ ФУНКЦИИ ----------
async def generate_image(prompt: str) -> Optional[str]:
    """Генерация по REPLICATE_GEN_MODEL/REPLICATE_GEN_VERSION."""
    try:
        if not client:
            raise RuntimeError("REPLICATE_API_TOKEN not set")
        model_pointer = (
            REPLICATE_GEN_MODEL
            if REPLICATE_GEN_VERSION == "latest"
            else f"{REPLICATE_GEN_MODEL}:{REPLICATE_GEN_VERSION}"
        )
        output = client.run(model_pointer, input={"prompt": prompt})
        return output[0] if output else None
    except Exception as e:
        print(f"Ошибка генерации: {e}")
        return None


async def start_training(photo) -> Optional[str]:
    """Примитивная тренировка по одной фотке — версия тренера берётся автоматически."""
    try:
        if not client:
            raise RuntimeError("REPLICATE_API_TOKEN not set")

        file = await photo.get_file()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        await file.download_to_drive(tmp.name)

        trainer_version = await _get_latest_trainer_version_id()
        if not trainer_version:
            raise RuntimeError("Не удалось получить версию тренера")

        training = client.trainings.create(
            version=trainer_version,
            input={"instance_prompt": "photo of person", "images": [tmp.name]},
        )
        return training.id
    except Exception as e:
        print(f"Ошибка обучения: {e}")
        return None


# ---------- VERBOSE ----------
async def generate_image_verbose(prompt: str) -> Dict[str, Any]:
    if not REPLICATE_API_TOKEN:
        return {"ok": False, "where": "env", "detail": "REPLICATE_API_TOKEN not set"}
    try:
        if not client:
            raise RuntimeError("Client not initialized")
        model_pointer = (
            REPLICATE_GEN_MODEL
            if REPLICATE_GEN_VERSION == "latest"
            else f"{REPLICATE_GEN_MODEL}:{REPLICATE_GEN_VERSION}"
        )
        output = client.run(model_pointer, input={"prompt": prompt})
        return {"ok": True, "images": output}
    except Exception as e:
        return {"ok": False, "where": "sdk", "error": repr(e)}


async def start_training_verbose_from_zip(images_zip_url: str) -> Dict[str, Any]:
    """POST на /models/{owner}/{model}/trainings. Если версия не задана — берём latest автоматически."""
    if not REPLICATE_API_TOKEN:
        return {"ok": False, "where": "env", "detail": "REPLICATE_API_TOKEN not set"}

    trainer_version = await _get_latest_trainer_version_id()
    if not trainer_version:
        return {"ok": False, "where": "env", "detail": "Cannot resolve trainer version automatically"}

    payload = {
        "version": trainer_version,
        "input": {"images_zip": images_zip_url, "steps": 800},
    }
    headers = {
        "Authorization": f"Token {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    url = f"https://api.replicate.com/v1/models/{REPLICATE_TRAIN_OWNER}/{REPLICATE_TRAIN_MODEL}/trainings"

    try:
        async with httpx.AsyncClient(timeout=120) as cl:
            r = await cl.post(url, headers=headers, json=payload)
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


# ---------- Генерация по готовой версии ----------
async def get_training_status_simple(training_id: str) -> Dict[str, Any]:
    if not REPLICATE_API_TOKEN:
        return {"ok": False, "where": "env", "detail": "REPLICATE_API_TOKEN not set"}
    url = f"https://api.replicate.com/v1/trainings/{training_id}"
    headers = {"Authorization": f"Token {REPLICATE_API_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=60) as cl:
            r = await cl.get(url, headers=headers)
            out = {"ok": r.status_code == 200, "status_code": r.status_code, "raw_text": r.text}
            try:
                out["json"] = r.json()
            except Exception:
                pass
            return out
    except Exception as e:
        return {"ok": False, "where": "exception", "error": repr(e)}


def _pick_model_pointer(training_json: Dict[str, Any]) -> Optional[str]:
    out = training_json.get("output")
    if isinstance(out, str) and ":" in out:
        return out
    if isinstance(out, dict):
        cand = out.get("version") or out.get("id") or out.get("model")
        if isinstance(cand, str) and ":" in cand:
            return cand
    dest = training_json.get("destination")
    if isinstance(dest, str) and "/" in dest:
        return dest
    return None


async def generate_with_model_version(model_pointer: str, prompt: str, **extra_input) -> Dict[str, Any]:
    if not REPLICATE_API_TOKEN:
        return {"ok": False, "where": "env", "detail": "REPLICATE_API_TOKEN not set"}
    try:
        if not client:
            raise RuntimeError("Client not initialized")
        inputs = {"prompt": prompt}
        if extra_input:
            inputs.update(extra_input)
        output = client.run(model_pointer, input=inputs)
        return {"ok": True, "images": output}
    except Exception as e:
        return {"ok": False, "where": "sdk", "error": repr(e)}


async def try_generate_from_training_id(training_id: str, prompt: str, **extra_input) -> Dict[str, Any]:
    st = await get_training_status_simple(training_id)
    if not st.get("ok"):
        return {"ok": False, "where": "status", "detail": st}
    tj = st.get("json") or {}
    status = tj.get("status")
    if status != "succeeded":
        return {"ok": False, "where": "training", "status": status, "detail": tj}
    model_ptr = _pick_model_pointer(tj)
    if not model_ptr:
        return {"ok": False, "where": "parse", "detail": "Cannot extract model version from training json", "training": tj}
    return await generate_with_model_version(model_ptr, prompt, **extra_input)
