import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
# Read-only resources (static/, plugins/, bundled platform-tools).
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def _data_dir() -> Path:
    if os.getenv("APKLOADER_HOME"):
        return Path(os.environ["APKLOADER_HOME"])
    if not FROZEN:
        return APP_DIR
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.getenv("LOCALAPPDATA", home / "AppData/Local")) / "APKLoader"
    if sys.platform == "darwin":
        return home / "Library/Application Support/APKLoader"
    return Path(os.getenv("XDG_DATA_HOME", home / ".local/share")) / "apkloader"


# Writable state: downloads, registry, backups, SDK, JRE.
DATA_DIR = _data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)

ADB_BIN = os.getenv("ADB_BIN", "adb")
# Device to control. Emulator started by the app overrides this at runtime.
ADB_SERIAL = os.getenv("ADB_SERIAL", "localhost:5555")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", DATA_DIR / "downloads")).resolve()
MAX_APK_MB = int(os.getenv("MAX_APK_MB", "500"))
API_TOKEN = os.getenv("API_TOKEN", "")
STREAM_FPS = float(os.getenv("STREAM_FPS", "4"))          # screenshot fallback
VIDEO_MAX = int(os.getenv("VIDEO_MAX", "1280"))           # longest side of H.264 stream
VIDEO_BITRATE = int(os.getenv("VIDEO_BITRATE", "4000000"))
# Optional VirusTotal hash lookup (only the SHA-256 is sent).
VT_API_KEY = os.getenv("VT_API_KEY", "")
# Admins with an internal repo can disable the SSRF guard.
ALLOW_PRIVATE_URLS = os.getenv("ALLOW_PRIVATE_URLS", "") == "1"
# Start the bundled emulator automatically when no device is reachable.
AUTO_EMULATOR = os.getenv("AUTO_EMULATOR", "1") == "1"
GITHUB_REPO = os.getenv("APKLOADER_REPO", "shovan29du/APK-loader")
FILE_ROOTS = ["/sdcard", "/storage/emulated/0", "/data/local/tmp"]
