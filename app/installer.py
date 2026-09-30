"""Install pipeline: verify -> snapshot old version -> install (apk / bundle / aab) -> history."""
import shutil
import time
from pathlib import Path

from . import adb, bundles, history, registry, safety
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


async def snapshot_current(package: str, entry_id: str) -> str:
    """Copy the installed APK(s) of `package` aside so this update can be rolled back."""
    dest = history.rollback_root() / entry_id
    dest.mkdir(parents=True, exist_ok=True)
    try:
        for i, remote in enumerate(await adb.apk_paths(package)):
            await adb.pull(remote, str(dest / f"{i}_{remote.rsplit('/', 1)[-1]}"))
        return str(dest)
    except adb.AdbError:
        shutil.rmtree(dest, ignore_errors=True)
        return ""


async def verify_and_install(path: Path, meta: Resolved | None, allow_unsafe: bool,
                             provider: str = "", app_id: str = "", label: str = "") -> dict:
    entry = {"id": history.new_id(), "time": time.time(), "label": label or path.name, "kind": "install",
             "provider": provider, "app_id": app_id, "file": str(path), "ok": False, "package": "",
             "prev_version_code": 0, "rollback_dir": "", "device": adb.serial()}
    report = await safety.verify(path, meta.sha256 if meta else "", meta.md5 if meta else "")
    result = {"ok": False, "safety": report.dict(), "history_id": entry["id"]}
    try:
        if report.blocked and not allow_unsafe:
            bad = "; ".join(f"{c.name}: {c.detail}" for c in report.checks if c.status == "fail")
            raise adb.AdbError(f"blocked by safety checks ({bad})")
        guess = (meta.package if meta and meta.package else "") or await safety.peek_package(path)
        if guess:
            prev = (await adb.installed_versions()).get(guess)
            if prev:
                entry["prev_version_code"] = prev
                entry["rollback_dir"] = await snapshot_current(guess, entry["id"])
        package = await install_artifact(path) or guess
        entry.update(ok=True, package=package,
                     version_code=meta.version_code if meta else 0,
                     version_name=meta.version_name if meta else "")
        result.update(ok=True, package=package)
        if provider and meta:
            registry.record(package or meta.package, provider, app_id, meta.version_code, meta.version_name)
    except Exception as e:
        entry["error"] = str(e)
        result["error"] = str(e)
        if not isinstance(e, adb.AdbError):
            history.add(entry)
            raise
    history.add(entry)
    return result


async def rollback(entry: dict) -> str:
    """Reinstall the version that was on the device before `entry` was installed. Keeps app data."""
    d = Path(entry.get("rollback_dir") or "")
    apks = sorted(d.glob("*.apk")) if d.is_dir() else []
    if not apks or not entry.get("package"):
        raise adb.AdbError("no rollback snapshot for this entry")
    paths = [str(p) for p in apks]

    async def put(downgrade: bool):
        if len(paths) == 1:
            await adb.install(paths[0], downgrade=downgrade)
        else:
            await adb.install_multiple(paths, downgrade=downgrade)
    try:
        await put(True)
    except adb.AdbError as e:
        if "DOWNGRADE" not in str(e).upper():
            raise
        await adb.uninstall(entry["package"], keep_data=True)   # data survives `pm uninstall -k`
        await put(False)
    history.add({"label": f"Rollback {entry['package']}", "kind": "rollback", "package": entry["package"],
                 "ok": True, "provider": "", "app_id": "", "file": "", "device": adb.serial(),
                 "version_code": entry.get("prev_version_code", 0)})
    return entry["package"]
