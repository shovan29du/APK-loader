# APK Loader

Install APKs on an Android runtime and use the running app from your browser.
Python (FastAPI) backend drives the device over `adb`; the browser shows a live
screen stream and sends taps, swipes, keys and text back.

## Run
```
docker compose up --build     # redroid Android + web app
# open http://localhost:8000
```
Or without Docker: `pip install -r requirements.txt`, have `adb` on PATH and a
reachable device/emulator, then `ADB_SERIAL=host:5555 uvicorn app.main:app`.

## Features
- **Marketplaces**: search F-Droid and Aptoide, tick several apps, install them all at once.
- **Direct URLs**: one `.apk` URL per line (public hosts only; SSRF-guarded).
- **Upload multiple APKs**: each file installs as its own app, or tick *split APKs* to
  install several files as one app (`adb install-multiple`).
- **Installed apps**: run, uninstall, and *Copy APK* (pull the APK off the device; split apps come as a zip).
- **APK files**: stored downloads/uploads can be copied out or deleted.

## Config (env)
`ADB_SERIAL`, `ADB_BIN`, `DOWNLOAD_DIR`, `MAX_APK_MB`, `STREAM_FPS`, `API_TOKEN` (Bearer auth; set it if not on localhost).

## Notes
- Google Play, APKMirror and APKPure need login or forbid scraping, so they are not built in.
  Add a `Provider` subclass in `app/providers.py` for any source you have rights to use.
- Only install apps you are licensed to use. Streaming is screenshot-based (~4 fps); swap in scrcpy for smoother video.
- Tests: `pytest`.
