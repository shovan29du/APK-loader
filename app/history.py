"""Install history. Each update snapshots the previous APK(s) so it can be rolled back."""
import json
import shutil
import threading
import time
import uuid

from . import config

_lock = threading.Lock()
MAX_ENTRIES = 200
ROLLBACK_KEEP_DAYS = 30


def _path():
    return config.DATA_DIR / "history.json"


def rollback_root():
    d = config.DATA_DIR / "rollbacks"
    d.mkdir(exist_ok=True)
    return d


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def load() -> list[dict]:
    try:
        return json.loads(_path().read_text())
    except (OSError, ValueError):
        return []


def _save(items: list[dict]):
    for old in items[MAX_ENTRIES:]:
        shutil.rmtree(rollback_root() / old["id"], ignore_errors=True)
    _path().write_text(json.dumps(items[:MAX_ENTRIES], indent=1))


def add(entry: dict):
    entry.setdefault("id", new_id())
    entry.setdefault("time", time.time())
    with _lock:
        _save([entry] + load())


def get(entry_id: str) -> dict | None:
    return next((e for e in load() if e["id"] == entry_id), None)


def delete(entry_id: str):
    with _lock:
        items = load()
        _save([e for e in items if e["id"] != entry_id])
    shutil.rmtree(rollback_root() / entry_id, ignore_errors=True)


def purge_old_rollbacks():
    cutoff = time.time() - ROLLBACK_KEEP_DAYS * 86400
    for d in rollback_root().iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
