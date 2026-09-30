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

from . import config, hardware, settings
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


def flavor() -> str:
    """google_apis (rootable: backups work) or google_apis_playstore (real Google Play, no root)."""
    if os.getenv("EMU_PLAYSTORE") == "1" or settings.load().get("playstore"):
        return "google_apis_playstore"
    return "google_apis"


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
    if sys.platform.startswith("linux") and hardware.nvidia_gpu():
        e.setdefault("__NV_PRIME_RENDER_OFFLOAD", "1")      # hybrid laptop: use the RTX, not the iGPU
        e.setdefault("__GLX_VENDOR_LIBRARY_NAME", "nvidia")
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
    return emulator_bin().exists() and avd_exists(AVD_NAME)


def tune_avd(log=print, name: str = AVD_NAME):
    """Write performance settings for this machine into the AVD's config.ini."""
    cfg = Path.home() / ".android/avd" / f"{name}.avd" / "config.ini"
    if not cfg.exists():
        return
    r = hardware.recommend()
    want = {"hw.ramSize": r["emu_ram_mb"], "hw.cpu.ncore": r["emu_cores"],
            "vm.heapSize": r["emu_heap_mb"], "disk.dataPartition.size": f"{r['emu_data_mb']}M",
            "hw.gpu.enabled": "yes", "hw.gpu.mode": r["emu_gpu"],
            "hw.audioInput": "no", "hw.audioOutput": "no", "showDeviceFrame": "no",
            "fastboot.forceColdBoot": "no", "fastboot.forceFastBoot": "yes"}
    lines = [l for l in cfg.read_text().splitlines()
             if l.split("=", 1)[0].strip() not in want]
    lines += [f"{k}={v}" for k, v in want.items()]
    cfg.write_text("\n".join(lines) + "\n")
    log(f"AVD tuned: {r['emu_ram_mb']} MB RAM, {r['emu_cores']} cores, gpu={r['emu_gpu']}")


def prefer_dgpu(log=print):
    """Windows hybrid laptops: make Windows run the emulator on the NVIDIA GPU, not the iGPU."""
    if not WIN or not hardware.nvidia_gpu():
        return
    try:
        import winreg
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\DirectX\UserGpuPreferences")
        for exe in [emulator_bin(), *(SDK / "emulator").glob("qemu/windows-*/qemu-system-*.exe")]:
            winreg.SetValueEx(key, str(exe), 0, winreg.REG_SZ, "GpuPreference=2;")
        log("Windows graphics preference: emulator -> high performance (NVIDIA)")
    except Exception as e:
        log(f"Could not set GPU preference ({e}); set it in Settings > System > Display > Graphics")


def accel_status() -> tuple[bool, str]:
    """Is hardware virtualization usable? (emulator -accel-check)"""
    if not emulator_bin().exists():
        return False, "emulator not installed"
    try:
        p = subprocess.run([str(emulator_bin()), "-accel-check"], capture_output=True,
                           text=True, env=env(), timeout=60)
        out = (p.stdout + p.stderr).strip()
        if p.returncode == 0:  # -accel-check exits 0 only when acceleration is usable
            return True, out.splitlines()[-1] if out else "ok"
    except Exception as e:
        out = str(e)
    hint = ("Enable 'Windows Hypervisor Platform' (admin PowerShell: "
            "Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All) and reboot; "
            "also enable Intel VT-x/virtualization in BIOS." if WIN else
            "Enable Intel VT-x in BIOS and make sure /dev/kvm is usable (add your user to the 'kvm' group)." if OS_TAG == "linux"
            else "Hypervisor.framework should be available; update macOS.")
    return False, f"hardware acceleration unavailable: {out[:200]} — {hint}"


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
    image = f"system-images;android-{API_LEVEL};{flavor()};{abi()}"
    log("Installing emulator + system image (large download)…")
    _run([sdkmanager(), f"--sdk_root={SDK}", "platform-tools", "emulator",
          f"build-tools;{API_LEVEL}.0.0", image])
    log("Creating virtual device…")
    _run([avdmanager(), "create", "avd", "-n", AVD_NAME, "-k", image, "-d", "pixel_5", "--force"], "no\n")
    tune_avd(log)
    prefer_dgpu(log)
    ok, msg = accel_status()
    log(("Hardware acceleration OK: " if ok else "WARNING: ") + msg)
    if with_bundletool:
        tools = config.DATA_DIR / "tools"
        tools.mkdir(exist_ok=True)
        if not (tools / "bundletool.jar").exists():
            _download(BUNDLETOOL_URL, tools / "bundletool.jar", log)
    log("Emulator ready.")


# ---------------- runtime control ----------------

class Controller:
    """One emulator process (an AVD on its own console port; adb serial emulator-<port>)."""

    def __init__(self, name: str = AVD_NAME, port: int = 5554):
        self.name, self.port = name, port
        self.proc: subprocess.Popen | None = None

    @property
    def serial(self) -> str:
        return f"emulator-{self.port}"

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.running():
            return
        if not (emulator_bin().exists() and avd_exists(self.name)):
            raise RuntimeError("emulator is not installed (run full_install.py, or use Set up emulator)")
        tune_avd(lambda m: None, self.name)
        prefer_dgpu(lambda m: None)
        gpu = hardware.recommend()["emu_gpu"]
        # Quick-boot snapshots stay enabled: after the first run the emulator resumes in seconds from the SSD.
        self.proc = subprocess.Popen(
            [str(emulator_bin()), "-avd", self.name, "-port", str(self.port), "-no-window", "-no-audio",
             "-no-boot-anim", "-gpu", gpu],
            env=env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if self.name == AVD_NAME:
            config.ADB_SERIAL = self.serial
        if sdk_adb().exists():
            config.ADB_BIN = str(sdk_adb())

    def stop(self):
        if self.running():
            try:  # graceful kill lets the emulator save its quick-boot snapshot
                subprocess.run([config.ADB_BIN, "-s", self.serial, "emu", "kill"], timeout=20,
                               capture_output=True)
                self.proc.wait(30)
            except Exception:
                pass
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


def avd_exists(name: str) -> bool:
    return (Path.home() / ".android/avd" / f"{name}.avd").exists()


def max_instances() -> int:
    """Each emulator wants ~4 GB + host headroom: 16 GB -> 2 at once."""
    return max(1, hardware.total_ram_mb() // 6144)


controllers: dict[str, Controller] = {AVD_NAME: Controller(AVD_NAME, 5554)}
controller = controllers[AVD_NAME]  # the primary instance (kept for callers that only need one)


def list_avds() -> list[str]:
    if not emulator_bin().exists():
        return []
    try:
        out = subprocess.run([str(emulator_bin()), "-list-avds"], capture_output=True, text=True,
                             env=env(), timeout=30).stdout
    except Exception:
        return []
    return [l.strip() for l in out.splitlines() if l.strip() and " " not in l.strip()]


def get_controller(name: str) -> Controller:
    if name not in list_avds():
        raise RuntimeError(f"no such emulator: {name}")
    if name not in controllers:
        used = {c.port for c in controllers.values()}
        controllers[name] = Controller(name, next(p for p in range(5556, 5600, 2) if p not in used))
    return controllers[name]


def running_count() -> int:
    return sum(1 for c in controllers.values() if c.running())


def create_avd(name: str, log=print):
    """Clone-style: a fresh AVD from the installed system image, tuned like the primary."""
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_\-]{1,30}", name):
        raise RuntimeError("name: letters, digits, _ and - (max 30)")
    if name in list_avds():
        raise RuntimeError("an emulator with that name already exists")
    image = f"system-images;android-{API_LEVEL};{flavor()};{abi()}"
    _run([avdmanager(), "create", "avd", "-n", name, "-k", image, "-d", "pixel_5", "--force"], "no\n")
    tune_avd(log, name)


def delete_avd(name: str):
    if name == AVD_NAME:
        raise RuntimeError("the primary emulator cannot be deleted")
    if name in controllers and controllers[name].running():
        raise RuntimeError("stop it first")
    _run([avdmanager(), "delete", "avd", "-n", name])
    controllers.pop(name, None)


async def wait_boot(timeout: float = 300, ctl: Controller | None = None):
    from . import adb
    ctl = ctl or controller
    adb.use_serial(ctl.serial)
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if ctl.proc and ctl.proc.poll() is not None:
            raise RuntimeError(ctl.early_error() or "emulator exited (hardware acceleration missing?)")
        try:
            if (await adb._dev("shell", "getprop", "sys.boot_completed", timeout=10)).strip() == "1":
                return
        except adb.AdbError:
            pass
        await asyncio.sleep(3)
    raise RuntimeError("emulator boot timed out")
