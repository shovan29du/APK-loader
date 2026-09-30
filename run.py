"""Start the APK Loader server and open it in the default browser."""
import os
import sys
import threading
import time
import webbrowser

HOST = os.getenv("APKLOADER_HOST", "127.0.0.1")
PORT = int(os.getenv("APKLOADER_PORT", "8000"))


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)
    sys.path.insert(0, here)
    # Prefer the adb we installed (platform-tools) over PATH.
    pt = os.path.join(here, "platform-tools")
    if os.path.isdir(pt):
        os.environ["PATH"] = pt + os.pathsep + os.environ.get("PATH", "")
    os.environ.setdefault("DOWNLOAD_DIR", os.path.join(here, "downloads"))
    threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(f"http://{HOST}:{PORT}")),
                     daemon=True).start()
    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
