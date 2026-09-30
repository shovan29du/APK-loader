"""Fully automatic Android emulator: downloads JRE + SDK + system image, creates an AVD, runs it."""
import asyncio
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import config
from .util import safe_extract_zip

SDK = config.DATA_DIR / "android-sdk"
JRE = config.DATA_DIR / "jre"
AVD_NAME = "apkloader"
SERIAL = "emulator-5554"
API_LEVEL = 34
WIN = sys.platform == "win32"
CMDLINE_URL = "https://dl.google.com/android/repository/commandlinetools-{}-11076708_latest.zip"
BUNDLETOOL_URL = "https://github.com/google/bundletool/releases/download/1.17.2/bundletool-all-1.17.2.jar"
LICENSE_URL = "https://developer.android.com/studio/terms"
OS_TAG = "win" if WIN else "mac" if sys.platform == "darwin" else "linux"


def _exe(name: str, bat: bool = False) -> str:
    return name + ((".bat" if bat else ".exe") if WIN else "")


def abi() -> str:
    return "arm64-v8a" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"


def _download(url: str, dest: Path, log):
    log(f"Downloading {url.rsplit('/', 1)[-1].split('?')[0]}…")
    req = urllib.request.Request(url, headers={"User-Agent": "apk-loader"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)


# ---------------- Java ----------------

def _java_ok(exe: str) -> bool:
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=20)
        m = re.search(r'version "(\d+)', out.stderr + out.stdout)
        return bool(m) and int(m.group(1)) >= 17
    except Exception:
        return False


def java_exe() -> str | None:
    if JRE.is_dir():
        for p in JRE.rglob(_exe("java")):
            if p.parent.name == "bin":
                return str(p)
    w = shutil.which("java")
    return w if w and _java_ok(w) else None


def ensure_java(log=print) -> str:
    if j := java_exe():
        return j
    arch = "aarch64" if abi() == "arm64-v8a" else "x64"
    osn = {"win": "windows", "mac": "mac", "linux": "linux"}[OS_TAG]
    url = f"https://api.adoptium.net/v3/binary/latest/17/ga/{osn}/{arch}/jre/hotspot/normal/eclipse"
    with tempfile.TemporaryDirectory() as t:
        arc = Path(t) / "jre.arc"
        _download(url, arc, log)
        JRE.mkdir(parents=True, exist_ok=True)
        if zipfile.is_zipfile(arc):
            with zipfile.ZipFile(arc) as z:
                safe_extract_zip(z, JRE, 2 << 30)
        else:
            with tarfile.open(arc) as tf:
                if sys.version_info >= (3, 12):
                    tf.extractall(JRE, filter="data")
                else:
                    tf.extractall(JRE)
    j = java_exe()
    if not j:
        raise RuntimeError("could not install Java")
    return j


def env() -> dict:
    e = dict(os.environ)
    j = java_exe()
    if j:
        e["JAVA_HOME"] = str(Path(j).parent.parent)
    e["ANDROID_SDK_ROOT"] = e["ANDROID_HOME"] = str(SDK)
    return e


# ---------------- SDK ----------------

def sdkmanager() -> Path:
    return SDK / "cmdline-tools" / "latest" / "bin" / _exe("sdkmanager", bat=True)


def avdmanager() -> Path:
    return SDK / "cmdline-tools" / "latest" / "bin" / _exe("avdmanager", bat=True)


def emulator_bin() -> Path:
    return SDK / "emulator" / _exe("emulator")


def sdk_adb() -> Path:
    return SDK / "platform-tools" / _exe("adb")


def installed() -> bool:
    return emulator_bin().exists() and (Path.home() / ".android/avd" / f"{AVD_NAME}.avd").exists()


def _run(cmd, input_text=None):
    p = subprocess.run([str(c) for c in cmd], input=input_text, capture_output=True,
                       text=True, env=env(), timeout=3600)
    if p.returncode != 0:
        raise RuntimeError((p.stdout + p.stderr).strip()[-800:])
    return p.stdout


def setup(log=print, with_bundletool: bool = True):
    """Idempotent. Running this accepts the Android SDK license (see LICENSE_URL)."""
    ensure_java(log)
    if not sdkmanager().exists():
        with tempfile.TemporaryDirectory() as t:
            z = Path(t) / "cmdline.zip"
            _download(CMDLINE_URL.format(OS_TAG), z, log)
            tmp = Path(t) / "x"
            with zipfile.ZipFile(z) as zf:
                safe_extract_zip(zf, tmp, 2 << 30)
            dst = SDK / "cmdline-tools" / "latest"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(dst, ignore_errors=True)
            shutil.move(str(tmp / "cmdline-tools"), dst)
    log(f"Accepting Android SDK licenses ({LICENSE_URL})…")
    _run([sdkmanager(), f"--sdk_root={SDK}", "--licenses"], "y\n" * 30)
    image = f"system-images;android-{API_LEVEL};google_apis;{abi()}"
    log("Installing emulator + system image (large download)…")
    _run([sdkmanager(), f"--sdk_root={SDK}", "platform-tools", "emulator",
          f"build-tools;{API_LEVEL}.0.0", image])
    log("Creating virtual device…")
    _run([avdmanager(), "create", "avd", "-n", AVD_NAME, "-k", image, "-d", "pixel_5", "--force"], "no\n")
    if with_bundletool:
        tools = config.DATA_DIR / "tools"
        tools.mkdir(exist_ok=True)
        if not (tools / "bundletool.jar").exists():
            _download(BUNDLETOOL_URL, tools / "bundletool.jar", log)
    log("Emulator ready.")


# ---------------- runtime control ----------------

class Controller:
    def __init__(self):
        self.proc: subprocess.Popen | None = None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.running():
            return
        if not installed():
            raise RuntimeError("emulator is not installed (run full_install.py, or use Set up emulator)")
        self.proc = subprocess.Popen(
            [str(emulator_bin()), "-avd", AVD_NAME, "-port", "5554", "-no-window", "-no-audio",
             "-no-snapshot-save", "-no-boot-anim", "-gpu", "auto"],
            env=env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        config.ADB_SERIAL = SERIAL
        if sdk_adb().exists():
            config.ADB_BIN = str(sdk_adb())

    def stop(self):
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def early_error(self) -> str:
        if self.proc and self.proc.poll() not in (None, 0) and self.proc.stderr:
            return self.proc.stderr.read().decode(errors="replace").strip()[-500:]
        return ""


controller = Controller()


async def wait_boot(timeout: float = 300):
    from . import adb
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if controller.proc and controller.proc.poll() is not None:
            raise RuntimeError(controller.early_error() or "emulator exited (hardware acceleration missing?)")
        try:
            if (await adb._dev("shell", "getprop", "sys.boot_completed", timeout=10)).strip() == "1":
                return
        except adb.AdbError:
            pass
        await asyncio.sleep(3)
    raise RuntimeError("emulator boot timed out")
