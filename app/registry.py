"""Remembers which marketplace each installed package came from (for update checks)."""
import json
import threading

from . import config

_lock = threading.Lock()


def _path():
    return config.DATA_DIR / "registry.json"


def load() -> dict:
    try:
        return json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}


def record(package: str, provider: str, app_id: str, version_code: int = 0, version_name: str = ""):
    if not package or provider == "direct":
        return
    with _lock:
        data = load()
        data[package] = {"provider": provider, "id": app_id,
                         "version_code": version_code, "version_name": version_name}
        _path().write_text(json.dumps(data, indent=1))


def forget(package: str):
    with _lock:
        data = load()
        if data.pop(package, None) is not None:
            _path().write_text(json.dumps(data, indent=1))
