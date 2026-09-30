"""Install pipeline: verify -> install (apk / bundle / aab) -> record source."""
import asyncio
import re
from pathlib import Path

from . import adb, bundles, registry, safety
from .providers import Resolved


async def install_artifact(path: Path, split_group: list[Path] | None = None) -> str:
    """Install one file (or a group of split APKs). Returns the package name if known."""
    before = await adb.third_party_packages()
    hint = ""
    if split_group:
        await adb.install_multiple([str(p) for p in split_group])
    else:
        ext = path.suffix.lower()
        if ext == ".apk":
            await adb.install(str(path))
        elif ext in (".xapk", ".apks"):
            hint = await bundles.install_bundle(path)
        elif ext == ".aab":
            hint = await bundles.install_aab(path)
        else:
            raise adb.AdbError(f"unsupported file type {ext}")
    new = sorted((await adb.third_party_packages()) - before)
    return new[0] if new else hint


async def verify_and_install(path: Path, meta: Resolved | None, allow_unsafe: bool,
                             provider: str = "", app_id: str = "") -> dict:
    report = await safety.verify(path, meta.sha256 if meta else "", meta.md5 if meta else "")
    result = {"ok": False, "safety": report.dict()}
    if report.blocked and not allow_unsafe:
        bad = "; ".join(f"{c.name}: {c.detail}" for c in report.checks if c.status == "fail")
        result["error"] = f"blocked by safety checks ({bad})"
        return result
    package = await install_artifact(path)
    package = package or (meta.package if meta else "")
    result.update(ok=True, package=package)
    if provider and meta:
        registry.record(package or meta.package, provider, app_id, meta.version_code, meta.version_name)
    return result
