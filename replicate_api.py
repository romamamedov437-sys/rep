import os
import logging
import tempfile
from typing import Optional, Dict, Any

import replicate  # официальный SDK (синхронный)
import httpx      # для «шумных» прямых запросов

# ========= ENV =========
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
REPLICATE_TRAIN_ENDPOINT = os.getenv("REPLICATE_TRAIN_ENDPOINT", "https://api.replicate.com/v1/trainings").strip()
REPLICATE_TRAIN_OWNER = os.getenv("REPLICATE_TRAIN_OWNER", "replicate").strip()
REPLICATE_TRAIN_MODEL = os.getenv("REPLICATE_TRAIN_MODEL", "fast-flux-trainer").strip()
# Версию тренера можно НЕ задавать — я научил код автоматически брать latest
REPLICATE_TRAIN_VERSION = (os.getenv("REPLICATE_TRAIN_VERSION") or "").strip()

REPLICATE_GEN_MODEL = os.getenv("REPLICATE_GEN_MODEL", "black-forest-labs/FLUX.1-schnell").strip()
REPLICATE_GEN_VERSION = os.getenv("REPLICATE_GEN_VERSION", "latest").strip()

log = logging.getLogger("replicate_api")

# Инициализация клиента SDK
client = replicate.Client(api_token=REPLICATE_API_TOKEN) if REPLICATE_API_TOKEN else None


# ---------- ВСПОМОГАТЕЛЬНОЕ: получить последнюю версию тренера, если не задана ----------
async def _get_latest_trainer_version_id() -> Optional[str]:
    """
    Возвращает id последней версии модели-тренера вида
    'replicate/fast-flux-trainer:xxxxxxxx...' без необходимости задавать ENV.
    """
    if REPLICATE_TRAIN_VERSION:
        # Уже задана руками — используем как есть
        return REPLICATE_TRAIN_VERSION

    if not REPLICATE_API_TOKEN:
        return None

    url = f"https://api.replicate.com/v1/models/{REPLICATE_TRAIN_OWNER}/{REPLICATE_TRAIN_MODEL}"
    headers = {"Authorization": f"Token {REPLICATE_API_TOKEN}"}

    try:
        async with httpx.AsyncClient(timeout=30) as cl:
            r = await cl.get(url, headers=headers)
            if r.status_code != 200:
                return None
            j = r.json() or {}
            versions = j.get("versions") or []
            if not versions:
                return None
            latest = versions[0]  # у Replicate первый — самый свежий
            vid = latest.get("id") or latest.get("version")
            if not vid:
                return None
            return f"{REPLICATE_TRAIN_OWNER}/{REPLICATE_TRAIN_MODEL}:{vid}"
    except Exception:
        return None


# ---------- ТВОИ ИСХОДНЫЕ ФУНКЦИИ (оставлены, но чуть улучшены) ----------
# Генерация по промпту (простая)
async def generate_image(prompt: str) -> Optional[str]:
    """
    Теперь использует REPLICATE_GEN_MODEL/REPLICATE_GEN_VERSION вместо хардкода SDXL.
    """
    try:
        if not client:
            raise RuntimeError("REPLICATE_API_TOKEN not set")
        model_pointer = REPLICATE_GEN_MODEL if REPLICATE_GEN_VERSION == "latest" else f"{REPLICATE_GEN_MODEL}:{REPLICATE_GEN_VERSION}"
        output = client.run(model_pointer, input={"prompt": prompt})
        return output[0] if output else None
    except Exception as e:
        print(f"Ошибка генерации: {e}")
        return None


# Обучение модели (простая заглушка)
async def start_training(photo) -> Optional[str]:
    """
    Оставил логику, но версия тренера теперь берётся автоматически (latest), если не задана.
    """
    try:
        if not client:
            raise RuntimeError("REPLICATE_API_TOKEN not set")

        file = await photo.get_file()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        await file.download_to_drive(tmp.name)

        # Получаем версию тренера (latest), если ENV пуст
        trainer_version = await _get_latest_trainer_version_id()
        if not trainer_version:
            raise RuntimeError("Не удалось получить версию тренера (проверь токен/модель тренера)")

        training = client.trainings.create(
            version=trainer_version,
            input={"instance_prompt": "photo of person", "images": [tmp.name]}
        )
        return training.id
    except Exception as e:
        print(f"Ошибка обучения: {e}")
        return None


# ---------- «ШУМНЫЕ»/VERBOSE ВЕРСИИ (оставлены и допилены) ----------

async def generate_image_verbose(prompt: str) -> Dict[str, Any]:
    """
    Возвращает подробный результат/ошибку при генерации через SDK.
    """
    if not REPLICATE_API_TOKEN:
        return {"ok": False, "where": "env", "detail": "REPLICATE_API_TOKEN not set"}
    try:
        if not client:
            raise RuntimeError("Client not initialized")
        model_pointer = REPLICATE_GEN_MODEL if REPLICATE_GEN_VERSION == "latest" else f"{REPLICATE_GEN_MODEL}:{REPLICATE_GEN_VERSION}"
        output = client.run(model_pointer, input={"prompt": prompt})
        return {"ok": True, "images": output}
    except Exception as e:
        return {"ok": False, "where": "sdk", "error": repr(e)}


async def start_training_verbose_from_zip(images_zip_url: str) -> Dict[str, Any]:
    """
    Прямой POST в Replicate /v1/trainings.
    Если REPLICATE_TRAIN_VERSION не задан, автоматически берём последний id версии тренера.
    """
    if not REPLICATE_API_TOKEN:
        return {"ok": False, "where": "env", "detail": "REPLICATE_API_TOKEN not set"}

    # Получаем (или используем) версию тренера
    trainer_version = REPLICATE_TRAIN_VERSION
    if not trainer_version:
        trainer_version = await _get_latest_trainer_version_id()
        if not trainer_version:
            return {"ok": False, "where": "env", "detail": "Cannot resolve trainer version automatically"}

    # Если пришёл полный pointer 'owner/model:vid', оставляем как есть;
    # если только VID — дополним owner/model:
    version_pointer = (
        trainer_version
        if ":" in trainer_version
        else f"{REPLICATE_TRAIN_OWNER}/{REPLICATE_TRAIN_MODEL}:{trainer_version}"
    )

    payload = {
        "version": version_pointer,
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
            "ok": False, "where": "httpx.HTTPStatusError",
            "status_code": getattr(e.response, "status_code", None),
            "response_text": getattr(e.response, "text", None),
            "error": str(e),
        }
    except Exception as e:
        return {"ok": False, "where": "exception", "error": repr(e)}


# ---------- БЕЗ ЗАВИСИМОСТИ ОТ TRAINER VERSION: статус и генерация по готовой версии ----------

async def get_training_status_simple(training_id: str) -> Dict[str, Any]:
    """
    Сырые данные тренировки /v1/trainings/{id}.
    """
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
    """
    Достаёт строку 'owner/model:version' из успешной тренировки.
    """
    if not training_json:
        return None

    out = training_json.get("output")
    if isinstance(out, str) and ":" in out:
        return out

    if isinstance(out, dict):
        cand = out.get("version") or out.get("id")
        if isinstance(cand, str) and ":" in cand:
            return cand

    urls = training_json.get("urls") or {}
    for k in ("get", "web"):
        v = urls.get(k)
        if isinstance(v, str) and ":" in v and "/" not in v:
            return v

    return None


async def generate_with_model_version(model_pointer: str, prompt: str, **extra_input) -> Dict[str, Any]:
    """
    Генерация по конкретной версии модели (например,
    'romamamedov437-sys/user-xxxx-lora:18558ab...').
    """
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
    """
    Если тренировка уже 'succeeded' — достаём модель и генерим.
    Иначе возвращаем статус.
    """
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
