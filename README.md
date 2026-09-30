# APK Loader

Install APKs on an Android runtime and use the running app from your browser.
A Python (FastAPI) backend drives the device over `adb`; the browser shows a live
H.264 screen and sends taps, swipes, keys and text back.

## Install (fully automatic)
```
python full_install.py               # app + deps + adb + Java + Android emulator + bundletool + shortcuts, then launches
python full_install.py --update      # fetch the latest release and reinstall in place
python full_install.py --uninstall
```
Shortcuts go on the normal Desktop **and** the OneDrive Desktop. Flags: `--no-emulator`,
`--no-launch`, `--no-adb`, `--dir PATH`.

> Running the installer with the emulator enabled **accepts the Android SDK License Agreement**
> (https://developer.android.com/studio/terms) on your behalf and downloads ~2 GB. Use `--no-emulator` to opt out.

No Python? Release builds (`.exe` installer, `.dmg`, `.deb`) are produced by CI on `v*` tags
(`packaging/build.py`). They are unsigned, so Windows SmartScreen / macOS Gatekeeper will warn.
Packaged builds set the emulator up from **Device → Set up / repair**.

The emulator needs hardware virtualization (KVM on Linux, Hyper-V/WHPX on Windows; built in on macOS).

## Automatic behaviour
- Boots the bundled emulator when no device answers on adb.
- Checks installed marketplace apps and APK Loader itself for updates every 6 h (Updates tab, banner).
- Purges stored APK files older than 7 days.

## Features
- **Video**: H.264 via `adb screenrecord` decoded with WebCodecs (Chrome/Edge/Safari; PNG fallback elsewhere). No audio.
- **Marketplaces**: F-Droid, Aptoide, direct URLs, and plugins (see below). Multi-select, background jobs with progress bars.
- **Formats**: `.apk`, `.xapk` (incl. OBB), `.apks`, `.aab` (via bundletool) — split APKs are filtered for the device ABI/density and installed together.
- **Safety checks** before every install: SHA-256/MD5 vs marketplace hash, archive sanity, signature (`apksigner` when present),
  optional VirusTotal hash lookup (`VT_API_KEY`; only the hash is sent). Failures block the install unless you tick the override.
- **Installed apps**: run, uninstall, copy APK, back up app data. **Updates** tab updates apps from their original marketplace.
- **Device**: rotate, screenshot, file browser (`/sdcard`, `/data/local/tmp`), app-data backup/restore (needs a rootable device: the bundled emulator or redroid).
- **Upload** several files at once, or tick *split APKs* to treat them as one app.

## Plugins
Drop a `.py` file in `plugins/` (or `<data dir>/plugins`, or `$PLUGIN_DIR`) defining `PROVIDERS = [MyProvider()]`
(subclass `app.providers.Provider`). See `plugins/custom_repo.py` for a private JSON catalog (`CUSTOM_REPO_URL`).
Plugins run with full privileges — only use ones you trust.

## Config (env)
`ADB_SERIAL`, `ADB_BIN`, `APKLOADER_HOME`, `DOWNLOAD_DIR`, `MAX_APK_MB`, `API_TOKEN` (Bearer auth — set it if not on localhost),
`AUTO_EMULATOR`, `VT_API_KEY`, `VIDEO_MAX`, `VIDEO_BITRATE`, `ALLOW_PRIVATE_URLS`, `PLUGIN_DIR`, `BUNDLETOOL_JAR`.

## Docker (Linux only)
`docker compose up --build` runs redroid + the app on http://localhost:8000 (needs binder/ashmem kernel support).

## Notes
- Google Play, APKMirror and APKPure need login or forbid scraping, so they are not built in.
- Only install apps you are licensed to use. Clipboard sync is not implemented (Android has no stable adb clipboard API).
- Tests: `pytest`.
