"""Start APK Loader and open it in the browser.

    python run.py                    run the server (auto-starts the emulator if installed)
    python run.py --hardware         show detected hardware and the tuned settings
    python run.py --setup-emulator   download Java + Android SDK + emulator + bundletool
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

HOST = os.getenv("APKLOADER_HOST", "127.0.0.1")
PORT = int(os.getenv("APKLOADER_PORT", "8000"))


def port_in_use(host: str, port: int) -> bool:
    """True if anything already answers on the port (also catches programs bound to 0.0.0.0)."""
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def can_bind(host: str, port: int) -> bool:
    with socket.socket() as s:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):   # Windows would otherwise let two programs share a port
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(host: str, preferred: int) -> int:
    for port in range(preferred, preferred + 30):
        if not port_in_use(host, port) and can_bind(host, port):
            return port
    raise SystemExit(f"No free port found between {preferred} and {preferred + 29}. "
                     "Set APKLOADER_PORT to a free port.")


def open_when_ready(host: str, port: int):
    """Open the browser only once /api/health proves this really is APK Loader."""
    url = f"http://{host}:{port}"
    for _ in range(120):
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=1) as r:
                if json.load(r).get("app") == "apk-loader":
                    print(f"APK Loader is running at {url}", flush=True)
                    webbrowser.open(url)
                    return
        except Exception:
            pass
        time.sleep(0.5)
    print(f"APK Loader did not answer at {url}", flush=True)


def main():
    if sys.stdout is None:  # windowed (no-console) build: uvicorn logging needs real streams
        sys.stdout = sys.stderr = open(os.devnull, "w")
    here = os.path.dirname(os.path.abspath(__file__))
    if not getattr(sys, "frozen", False):
        os.chdir(here)
        sys.path.insert(0, here)
    from app import config, emulator

    if "--hardware" in sys.argv:
        from app import hardware
        print(hardware.describe())
        return
    if "--setup-helper" in sys.argv:       # scrcpy-server for clipboard / audio / multi-touch
        from app import scrcpy
        scrcpy.fetch_server(lambda m: print("==>", m, flush=True))
        return
    if "--setup-emulator" in sys.argv:
        if "--playstore" in sys.argv:  # image with the real Google Play Store (no root)
            from app import settings
            settings.save(playstore=True)
        emulator.setup(lambda m: print("==>", m, flush=True))
        return

    # Prefer our own adb (bundled or from the SDK) over whatever is on PATH.
    mac_res = os.path.join(os.path.dirname(sys.executable), "..", "Resources", "platform-tools")
    for d in (emulator.SDK / "platform-tools", config.APP_DIR / "platform-tools",
              config.DATA_DIR / "platform-tools", os.path.normpath(mac_res)):
        d = __import__("pathlib").Path(d)
        if d.is_dir():
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            break
    port = pick_port(HOST, PORT)
    if port != PORT:
        print(f"Port {PORT} is already in use by another program; using {port} instead.", flush=True)
    threading.Thread(target=open_when_ready, args=(HOST, port), daemon=True).start()
    import uvicorn
    from app.main import app
    uvicorn.run(app, host=HOST, port=port, log_level="info",
                log_config=None if getattr(sys, "frozen", False) else uvicorn.config.LOGGING_CONFIG)


if __name__ == "__main__":
    main()
