"""Marketplace plugins.

Drop a .py file into `plugins/` (next to the app) or `<data dir>/plugins/`
(or $PLUGIN_DIR). It must define `PROVIDERS = [<Provider instance>, ...]`,
built from `app.providers.Provider`. Plugins run with full app privileges,
so only install ones you trust.
"""
import importlib.util
import logging
import os
from pathlib import Path

from . import config
from .providers import PROVIDERS, Provider

log = logging.getLogger("apkloader.plugins")


def plugin_dirs() -> list[Path]:
    dirs = [config.APP_DIR / "plugins", config.DATA_DIR / "plugins"]
    if os.getenv("PLUGIN_DIR"):
        dirs.append(Path(os.environ["PLUGIN_DIR"]))
    return dirs


def load_plugins() -> list[str]:
    loaded = []
    seen = set()
    for d in plugin_dirs():
        if not d.is_dir() or d.resolve() in seen:
            continue
        seen.add(d.resolve())
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"apkloader_plugin_{f.stem}", f)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                for prov in getattr(mod, "PROVIDERS", []):
                    if isinstance(prov, Provider) and prov.name and prov.name not in PROVIDERS:
                        PROVIDERS[prov.name] = prov
                        loaded.append(prov.name)
            except Exception as e:  # a broken plugin must not stop the app
                log.warning("plugin %s failed: %s", f, e)
    return loaded
