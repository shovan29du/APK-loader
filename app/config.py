import os
from pathlib import Path

ADB_BIN = os.getenv("ADB_BIN", "adb")
# Device to control. For redroid / emulator containers this is host:port.
ADB_SERIAL = os.getenv("ADB_SERIAL", "localhost:5555")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads")).resolve()
MAX_APK_MB = int(os.getenv("MAX_APK_MB", "500"))
# If set, every /api request must send "Authorization: Bearer <token>".
API_TOKEN = os.getenv("API_TOKEN", "")
# Frames per second cap for the screen stream.
STREAM_FPS = float(os.getenv("STREAM_FPS", "4"))
