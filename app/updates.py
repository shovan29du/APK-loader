"""Update checks: installed marketplace apps, and APK Loader itself."""
import time

import httpx

from . import __version__, adb, config, registry
from .providers import PROVIDERS

_self_cache: dict = {"at": 0, "data": None}


async def app_updates() -> list[dict]:
    reg = registry.load()
    if not reg:
        return []
    installed = await adb.installed_versions()
    out = []
    async with httpx.AsyncClient(headers={"User-Agent": "apk-loader"}, timeout=20) as c:
        for pkg, src in reg.items():
            cur = installed.get(pkg)
            prov = PROVIDERS.get(src["provider"])
            if cur is None or not prov:
                continue
            try:
                latest = await prov.resolve(c, src["id"])
            except Exception:
                continue
            if latest.version_code and latest.version_code > cur:
                out.append({"package": pkg, "provider": src["provider"], "id": src["id"],
                            "installed": cur, "latest": latest.version_code,
                            "latest_name": latest.version_name})
    return out


def _tuple(v: str):
    return tuple(int(x) for x in v.lstrip("v").split(".") if x.isdigit())


async def self_update(force: bool = False) -> dict:
    now = time.time()
    if not force and _self_cache["data"] and now - _self_cache["at"] < 6 * 3600:
        return _self_cache["data"]
    data = {"current": __version__, "latest": None, "available": False, "url": ""}
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "apk-loader"}) as c:
            r = await c.get(f"https://api.github.com/repos/{config.GITHUB_REPO}/releases/latest")
        if r.status_code == 200:
            j = r.json()
            data.update(latest=j["tag_name"].lstrip("v"), url=j["html_url"])
            data["available"] = _tuple(data["latest"]) > _tuple(__version__)
    except Exception:
        pass
    _self_cache.update(at=now, data=data)
    return data
