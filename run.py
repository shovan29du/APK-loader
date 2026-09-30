"""Start APK Loader and open it in the browser.

    python run.py                    run the server (auto-starts the emulator if installed)
    python run.py --setup-emulator   download Java + Android SDK + emulator + bundletool
"""
import os
import sys
import threading
import time
import webbrowser

HOST = os.getenv("APKLOADER_HOST", "127.0.0.1")
PORT = int(os.getenv("APKLOADER_PORT", "8000"))


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    if not getattr(sys, "frozen", False):
        os.chdir(here)
        sys.path.insert(0, here)
    from app import config, emulator

    if "--setup-emulator" in sys.argv:
        emulator.setup(lambda m: print("==>", m, flush=True))
        return

    # Prefer our own adb (bundled or from the SDK) over whatever is on PATH.
    for d in (emulator.SDK / "platform-tools", config.APP_DIR / "platform-tools",
              config.DATA_DIR / "platform-tools"):
        if d.is_dir():
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            break
    threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(f"http://{HOST}:{PORT}")),
                     daemon=True).start()
    import uvicorn
    from app.main import app
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
