"""Thin async wrapper around the `adb` CLI. Every call targets serial() (per-request device)."""
import asyncio
import contextvars
import posixpath
import re
import shlex

from . import config, procs

_serial: contextvars.ContextVar = contextvars.ContextVar("adb_serial", default=None)
_SERIAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$")


def serial() -> str:
    return _serial.get() or config.ADB_SERIAL


def use_serial(value: str | None) -> None:
    """Select the device for the current request/task (contextvar, so concurrent requests differ)."""
    if value and not _SERIAL_RE.match(value):
        raise AdbError("invalid device serial")
    _serial.set(value or None)


class AdbError(RuntimeError):
    pass


async def _run(*args: str, timeout: float = 120, binary: bool = False):
    try:
        proc = await procs.exec_async(
            config.ADB_BIN, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise AdbError(f"adb binary not found: {config.ADB_BIN}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise AdbError(f"adb {' '.join(args)} timed out")
    if proc.returncode != 0:
        raise AdbError((err or out).decode(errors="replace").strip())
    return out if binary else out.decode(errors="replace")


async def _dev(*args: str, **kw):
    return await _run("-s", serial(), *args, **kw)


async def connect() -> str:
    if ":" in serial():
        await _run("connect", serial(), timeout=15)
    return (await _dev("get-state", timeout=15)).strip()


async def status() -> dict:
    try:
        state = await connect()
        size = await screen_size()
        return {"connected": state == "device", "serial": serial(), "screen": size}
    except AdbError as e:
        return {"connected": False, "serial": serial(), "error": str(e)}


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


async def install(apk_path: str, downgrade: bool = False) -> str:
    """Install an APK (-r replace, -g grant permissions). Returns adb output."""
    flags = ["-r", "-g"] + (["-d"] if downgrade else [])
    out = await _dev("install", *flags, apk_path, timeout=300)
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


async def uninstall(package: str, keep_data: bool = False) -> None:
    if not valid_package(package):
        raise AdbError("invalid package name")
    if keep_data:  # used by rollback: lets an older version replace a newer one without wiping data
        await _dev("shell", "pm", "uninstall", "-k", package)
    else:
        await _dev("uninstall", package)


class ShellChannel:
    """One long-lived `adb shell` per device: commands are written to stdin, so there is no
    adb process spawn per tap/key (the main source of input lag)."""

    def __init__(self, dev: str):
        self.dev = dev
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()

    async def send(self, cmd: str):
        async with self.lock:
            for attempt in (0, 1):
                if self.proc is None or self.proc.returncode is not None:
                    try:
                        self.proc = await procs.exec_async(
                            config.ADB_BIN, "-s", self.dev, "shell",
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL)
                    except FileNotFoundError:
                        raise AdbError(f"adb binary not found: {config.ADB_BIN}")
                try:
                    self.proc.stdin.write(cmd.encode() + b"\n")
                    await self.proc.stdin.drain()
                    return
                except (BrokenPipeError, ConnectionResetError):
                    self.proc = None
                    if attempt:
                        raise AdbError("device shell closed")

    def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
        self.proc = None


_channels: dict[str, ShellChannel] = {}


async def fire(cmd: str):
    """Run a shell command on the current device without waiting for it (low latency)."""
    dev = serial()
    ch = _channels.get(dev)
    if ch is None:
        ch = _channels[dev] = ShellChannel(dev)
    await ch.send(cmd)


def close_channels():
    for ch in _channels.values():
        ch.close()
    _channels.clear()


async def tap(x: int, y: int):
    await fire(f"input tap {int(x)} {int(y)}")


async def swipe(x1: int, y1: int, x2: int, y2: int, ms: int = 200):
    await fire("input swipe " + " ".join(str(int(v)) for v in (x1, y1, x2, y2, max(int(ms), 1))))


async def key(name: str):
    if not re.fullmatch(r"[A-Z_0-9]+|\d+", name):
        raise AdbError("invalid keycode")
    await fire(f"input keyevent {name}")


def text_command(s: str) -> str:
    """`input text` for printable ASCII; space becomes %s. Quoted for the device shell."""
    safe = "".join(c for c in s if 0x20 <= ord(c) < 0x7F)[:200].replace(" ", "%s")
    return f"input text {shlex.quote(safe)}" if safe else ""


async def text(s: str):
    if cmd := text_command(s):
        await fire(cmd)


async def install_multiple(apk_paths: list[str], downgrade: bool = False) -> str:
    """Install several APKs of ONE app together (split APKs / base + config)."""
    flags = ["-r", "-g"] + (["-d"] if downgrade else [])
    out = await _dev("install-multiple", *flags, *apk_paths, timeout=600)
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


# ---------- device info / controls ----------

async def device_info() -> dict:
    abis = (await _dev("shell", "getprop", "ro.product.cpu.abilist")).strip()
    abis = [a for a in abis.split(",") if a] or [(await _dev("shell", "getprop", "ro.product.cpu.abi")).strip()]
    dens = re.findall(r"(\d+)", await _dev("shell", "wm", "density"))
    return {"abis": abis, "density": int(dens[-1]) if dens else 320}


async def rotate(n: int):
    if n not in (0, 1, 2, 3):
        raise AdbError("rotation must be 0-3")
    await _dev("shell", "settings", "put", "system", "accelerometer_rotation", "0")
    await _dev("shell", "settings", "put", "system", "user_rotation", str(n))


async def installed_versions() -> dict[str, int]:
    out = await _dev("shell", "pm", "list", "packages", "-3", "--show-versioncode")
    res = {}
    for l in out.splitlines():
        m = re.match(r"package:(\S+)\s+versionCode:(\d+)", l.strip())
        if m:
            res[m.group(1)] = int(m.group(2))
    return res


async def _stream_stdout_to_file(args: list[str], out_path: str, timeout: float = 900):
    with open(out_path, "wb") as f:
        try:
            proc = await procs.exec_async(
                config.ADB_BIN, "-s", serial(), *args,
                stdout=f, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            raise AdbError(f"adb binary not found: {config.ADB_BIN}")
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise AdbError("timed out")
    if proc.returncode != 0:
        raise AdbError(err.decode(errors="replace").strip())


async def push(local: str, remote: str):
    await _dev("push", local, remote, timeout=900)


# ---------- device file access (restricted to config.FILE_ROOTS) ----------

def safe_remote(path: str) -> str:
    p = posixpath.normpath("/" + path.lstrip("/")) if not path.startswith("/") else posixpath.normpath(path)
    if any(c in p for c in "\0\n") or not any(p == r or p.startswith(r + "/") for r in config.FILE_ROOTS):
        raise AdbError(f"path must be under {', '.join(config.FILE_ROOTS)}")
    return p


_LS = re.compile(r"^([\-dl])\S+\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\S+\s+\S+)\s+(.+)$")


async def list_dir(path: str) -> list[dict]:
    p = safe_remote(path)
    out = await _dev("shell", f"ls -lA {shlex.quote(p + '/')}")
    items = []
    for l in out.splitlines():
        m = _LS.match(l.strip())
        if m:
            kind, size, mtime, name = m.groups()
            name = name.split(" -> ")[0]
            items.append({"name": name, "dir": kind in "dl", "size": int(size), "mtime": mtime})
    return sorted(items, key=lambda i: (not i["dir"], i["name"].lower()))


async def remote_delete(path: str):
    p = safe_remote(path)
    if p in config.FILE_ROOTS:
        raise AdbError("refusing to delete a root folder")
    await _dev("shell", f"rm -rf {shlex.quote(p)}")


async def remote_mkdir(path: str):
    await _dev("shell", f"mkdir -p {shlex.quote(safe_remote(path))}")


# ---------- app data backup / restore (needs root adbd: emulator google_apis, redroid) ----------

async def _root():
    try:
        await _run("-s", serial(), "root", timeout=20)
        await asyncio.sleep(1.5)
        await connect()
    except AdbError:
        pass
    who = (await _dev("shell", "id", "-u")).strip()
    if who != "0":
        raise AdbError("backup/restore needs a rootable device (use the bundled emulator or redroid)")


async def backup_app(package: str, out_path: str):
    if not valid_package(package):
        raise AdbError("invalid package name")
    await _root()
    await _stream_stdout_to_file(["exec-out", "tar", "-cf", "-", "-C", "/data/data", package], out_path)


async def restore_app(package: str, tar_path: str):
    if not valid_package(package):
        raise AdbError("invalid package name")
    await _root()
    uid = (await _dev("shell", "stat", "-c", "%u", f"/data/data/{package}")).strip()
    if not uid.isdigit():
        raise AdbError(f"{package} must be installed before restoring its data")
    tmp = "/data/local/tmp/apkloader-restore.tar"
    await push(tar_path, tmp)
    await _dev("shell", "am", "force-stop", package)
    await _dev("shell", f"tar -xf {tmp} -C /data/data && chown -R {uid}:{uid} /data/data/{package} "
                        f"&& (restorecon -R /data/data/{package} || true); rm -f {tmp}")


# ---------- display geometry / emulator console ----------

def parse_display(dump: str) -> dict | None:
    """From `dumpsys window displays`: natural (init) and current (rotated) size."""
    m = re.search(r"init=(\d+)x(\d+)\b.*?\bcur=(\d+)x(\d+)", dump)
    if not m:
        return None
    iw, ih, cw, ch = map(int, m.groups())
    return {"init": (iw, ih), "cur": (cw, ch), "rotated": (iw, ih) != (cw, ch)}


async def display_info() -> dict:
    try:
        d = parse_display(await _dev("shell", "dumpsys window displays | grep -m1 'cur='"))
        if d:
            return d
    except AdbError:
        pass
    s = await screen_size()
    wh = (s["width"], s["height"])
    return {"init": wh, "cur": wh, "rotated": False}


async def user_rotation() -> int:
    try:
        return int((await _dev("shell", "settings", "get", "system", "user_rotation")).strip())
    except (ValueError, AdbError):
        return 0


async def emu(*args: str) -> str:
    """Emulator console command through adb (snapshots, network shaping)."""
    if not serial().startswith("emulator-"):
        raise AdbError("only available for emulators")
    out = await _dev("emu", *args, timeout=60)
    if re.search(r"^KO", out, re.M):
        raise AdbError(out.strip())
    return out


# ---------- per-app management (WSA-Toolbox style) ----------

async def force_stop(package: str):
    if not valid_package(package):
        raise AdbError("invalid package name")
    await _dev("shell", "am", "force-stop", package)


async def clear_data(package: str):
    """Wipe an app's data and cache (like Settings > Storage > Clear). The app stays installed."""
    if not valid_package(package):
        raise AdbError("invalid package name")
    out = await _dev("shell", "pm", "clear", package)
    if "Success" not in out:
        raise AdbError(out.strip() or "could not clear data")


def parse_package_info(dump: str) -> dict:
    def grab(key):
        m = re.search(rf"{key}=(\S+)", dump)
        return m.group(1) if m else ""
    return {"version_name": grab("versionName"), "version_code": grab("versionCode"),
            "target_sdk": grab("targetSdk"), "installed": grab("firstInstallTime"),
            "updated": grab("lastUpdateTime"), "installer": grab("installerPackageName")}


async def package_info(package: str) -> dict:
    if not valid_package(package):
        raise AdbError("invalid package name")
    out = await _dev("shell", f"dumpsys package {package} | grep -E "
                              "'versionName=|versionCode=|targetSdk=|firstInstallTime=|lastUpdateTime=|installerPackageName='")
    info = parse_package_info(out)
    if not info["version_name"] and not info["version_code"]:
        raise AdbError(f"{package} is not installed")
    return info
