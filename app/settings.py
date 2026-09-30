import json

from . import config


def _path():
    return config.DATA_DIR / "settings.json"


def load() -> dict:
    try:
        return json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}


def save(**kw):
    d = load()
    d.update(kw)
    _path().write_text(json.dumps(d, indent=1))
