"""Start APK Loader and open it in the browser.

    python run.py                    run the server (auto-starts the emulator if installed)
    python run.py --hardware         show detected hardware and the tuned settings
    python run.py --setup-emulator   download Java + Android SDK + emulator + bundletool
    python run.py --app com.x.y      open one Android app (what the app shortcuts run)
    python run.py --install a.apk    install the file(s) and open the app ("Open with" / double-click)

Only one APK Loader runs per user: starting it again reuses the running server.
"""
import atexit
import json
import mimetypes
import os
import socket
import uuid
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


def server_file():
    from app import config
    return config.DATA_DIR / "server.json"


def is_ours(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=1.5) as r:
            return json.load(r).get("app") == "apk-loader"
    except Exception:
        return False


def running_port(host: str):
    """Port of an APK Loader already running for this user, or None (stale files are ignored)."""
    try:
        port = json.loads(server_file().read_text())["port"]
    except (OSError, ValueError, KeyError):
        return None
    return port if is_ours(host, port) else None


def _request(host, port, method, path, body=None, headers=None):
    h = dict(headers or {})
    if os.getenv("API_TOKEN"):
        h["Authorization"] = "Bearer " + os.environ["API_TOKEN"]
    req = urllib.request.Request(f"http://{host}:{port}{path}", data=body, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def multipart(files):
    """Minimal multipart/form-data body: the files plus run=true (open the app afterwards)."""
    boundary = uuid.uuid4().hex
    parts = []
    for path in files:
        name = os.path.basename(path)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                         f"Content-Type: {ctype}\r\n\r\n".encode() + f.read() + b"\r\n")
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="run"\r\n\r\ntrue\r\n'.encode())
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def do_requests(host, port, app_pkg=None, install=()):
    """Tell the running server what the command line asked for."""
    try:
        if install:
            body, ctype = multipart(install)
            _request(host, port, "POST", "/api/upload", body, {"Content-Type": ctype})
        elif app_pkg:
            _request(host, port, "POST", f"/api/apps/{app_pkg}/open", b"")
    except Exception as e:
        print(f"Could not hand the request to APK Loader: {e}", flush=True)


def open_when_ready(host: str, port: int, app_pkg=None, install=()):
    """Open the browser only once /api/health proves this really is APK Loader."""
    url = f"http://{host}:{port}"
    for _ in range(120):
        if is_ours(host, port):
            print(f"APK Loader is running at {url}", flush=True)
            do_requests(host, port, app_pkg, install)
            webbrowser.open(url)
            return
        time.sleep(0.5)
    print(f"APK Loader did not answer at {url}", flush=True)


def parse_cli(argv):
    app_pkg, files = None, []
    if "--app" in argv and argv.index("--app") + 1 < len(argv):
        app_pkg = argv[argv.index("--app") + 1]
    if "--install" in argv:
        files = [os.path.abspath(a) for a in argv[argv.index("--install") + 1:]
                 if not a.startswith("--") and os.path.isfile(a)]
    return app_pkg, files


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
    app_pkg, files = parse_cli(sys.argv)
    if app_pkg and not __import__("re").fullmatch(r"[A-Za-z][\w]*(\.[A-Za-z_][\w]*)+", app_pkg):
        raise SystemExit(f"Not a valid Android package name: {app_pkg}")
    existing = running_port(HOST)
    if existing:                                  # one instance per user: reuse the running server
        print(f"APK Loader is already running at http://{HOST}:{existing}", flush=True)
        do_requests(HOST, existing, app_pkg, files)
        webbrowser.open(f"http://{HOST}:{existing}")
        return
    port = pick_port(HOST, PORT)
    if port != PORT:
        print(f"Port {PORT} is already in use by another program; using {port} instead.", flush=True)
    server_file().write_text(json.dumps({"port": port, "pid": os.getpid()}))
    atexit.register(lambda: server_file().unlink(missing_ok=True))
    threading.Thread(target=open_when_ready, args=(HOST, port, app_pkg, files), daemon=True).start()
    import uvicorn
    from app.main import app
    uvicorn.run(app, host=HOST, port=port, log_level="info",
                log_config=None if getattr(sys, "frozen", False) else uvicorn.config.LOGGING_CONFIG)


if __name__ == "__main__":
    main()
