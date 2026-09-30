"""Thin async wrapper around the `adb` CLI."""
import asyncio
import re

from . import config


class AdbError(RuntimeError):
    pass


async def _run(*args: str, timeout: float = 120, binary: bool = False):
    proc = await asyncio.create_subprocess_exec(
        config.ADB_BIN, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise AdbError(f"adb {' '.join(args)} timed out")
    except FileNotFoundError:
        raise AdbError(f"adb binary not found: {config.ADB_BIN}")
    if proc.returncode != 0:
        raise AdbError((err or out).decode(errors="replace").strip())
    return out if binary else out.decode(errors="replace")


async def _dev(*args: str, **kw):
    return await _run("-s", config.ADB_SERIAL, *args, **kw)


async def connect() -> str:
    if ":" in config.ADB_SERIAL:
        await _run("connect", config.ADB_SERIAL, timeout=15)
    return (await _dev("get-state", timeout=15)).strip()


async def status() -> dict:
    try:
        state = await connect()
        size = await screen_size()
        return {"connected": state == "device", "serial": config.ADB_SERIAL, "screen": size}
    except AdbError as e:
        return {"connected": False, "serial": config.ADB_SERIAL, "error": str(e)}


async def screen_size() -> dict:
    out = await _dev("shell", "wm", "size")
    m = re.findall(r"(\d+)x(\d+)", out)
    if not m:
        raise AdbError("could not read screen size")
    w, h = map(int, m[-1])  # last entry is the override size if present
    return {"width": w, "height": h}


async def screenshot() -> bytes:
    return await _dev("exec-out", "screencap", "-p", binary=True, timeout=15)


async def third_party_packages() -> set[str]:
    out = await _dev("shell", "pm", "list", "packages", "-3")
    return {l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("package:")}


async def install(apk_path: str) -> str:
    """Install an APK (-r replace, -g grant permissions). Returns adb output."""
    out = await _dev("install", "-r", "-g", apk_path, timeout=300)
    if "Success" not in out:
        raise AdbError(out.strip())
    return out.strip()


_PKG_RE = re.compile(r"^[A-Za-z][\w]*(\.[A-Za-z_][\w]*)+$")


def valid_package(name: str) -> bool:
    return bool(_PKG_RE.match(name))


async def launch(package: str) -> None:
    if not valid_package(package):
        raise AdbError("invalid package name")
    out = await _dev("shell", "monkey", "-p", package,
                     "-c", "android.intent.category.LAUNCHER", "1")
    if "No activities found" in out:
        raise AdbError(f"{package} has no launcher activity")


async def uninstall(package: str) -> None:
    if not valid_package(package):
        raise AdbError("invalid package name")
    await _dev("uninstall", package)


async def tap(x: int, y: int):
    await _dev("shell", "input", "tap", str(int(x)), str(int(y)))


async def swipe(x1: int, y1: int, x2: int, y2: int, ms: int = 200):
    await _dev("shell", "input", "swipe", *(str(int(v)) for v in (x1, y1, x2, y2, ms)))


async def key(name: str):
    if not re.fullmatch(r"[A-Z_0-9]+|\d+", name):
        raise AdbError("invalid keycode")
    await _dev("shell", "input", "keyevent", name)


async def text(s: str):
    # `input text` treats space as %s; strip everything shell-unsafe.
    safe = re.sub(r"[^A-Za-z0-9 @._\-+,:/=]", "", s).replace(" ", "%s")
    if safe:
        await _dev("shell", "input", "text", safe)


async def install_multiple(apk_paths: list[str]) -> str:
    """Install several APKs of ONE app together (split APKs / base + config)."""
    out = await _dev("install-multiple", "-r", "-g", *apk_paths, timeout=600)
    if "Success" not in out:
        raise AdbError(out.strip())
    return out.strip()


async def apk_paths(package: str) -> list[str]:
    if not valid_package(package):
        raise AdbError("invalid package name")
    out = await _dev("shell", "pm", "path", package)
    paths = [l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("package:")]
    if not paths:
        raise AdbError(f"{package} is not installed")
    return paths


async def pull(remote: str, local: str) -> None:
    await _dev("pull", remote, local, timeout=300)
