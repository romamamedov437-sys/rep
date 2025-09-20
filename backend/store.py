import json, os, threading
from typing import Any, Dict, Optional

_PATH = os.environ.get("STORE_PATH", "./data/store.json")
_LOCK = threading.Lock()

os.makedirs(os.path.dirname(_PATH), exist_ok=True)
if not os.path.exists(_PATH):
    with open(_PATH, "w") as f:
        json.dump({}, f)

def _load() -> Dict[str, Any]:
    with open(_PATH, "r") as f:
        return json.load(f)

def _save(data: Dict[str, Any]) -> None:
    with open(_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_user(uid: str) -> Dict[str, Any]:
    with _LOCK:
        data = _load()
        return data.get(uid) or {}

def set_user(uid: str, payload: Dict[str, Any]) -> None:
    with _LOCK:
        data = _load()
        data[uid] = payload
        _save(data)

def update_user(uid: str, **fields) -> Dict[str, Any]:
    with _LOCK:
        data = _load()
        cur = data.get(uid) or {}
        cur.update({k: v for k, v in fields.items() if v is not None})
        data[uid] = cur
        _save(data)
        return cur
