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

### Signing (removes the Windows SmartScreen / macOS Gatekeeper warnings)
`packaging/build.py` signs automatically when these repository secrets exist (otherwise it builds unsigned):
Windows: `WIN_CERT_PFX_B64`, `WIN_CERT_PASSWORD` (a code-signing certificate from a CA — you must buy this).
macOS: `MACOS_CERT_P12_B64`, `MACOS_CERT_PASSWORD`, `MACOS_SIGN_IDENTITY`, plus `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD`
for notarization (needs an Apple Developer Program membership). New Windows certificates can still show SmartScreen
warnings until they build reputation. Each release also ships `SHA256SUMS.txt`.

No Python? Release builds (`.exe` installer, `.dmg`, `.deb`) are produced by CI on `v*` tags
(`packaging/build.py`). They are unsigned, so Windows SmartScreen / macOS Gatekeeper will warn.
Packaged builds set the emulator up from **Device → Set up / repair**.

The emulator needs hardware virtualization (KVM on Linux, Hyper-V/WHPX on Windows; built in on macOS).

## Hardware tuning (automatic)
The app detects RAM, CPU threads and an NVIDIA GPU and tunes itself (`python run.py --hardware` shows the result).
For a 16 GB / 8+ thread / RTX laptop: emulator gets 4 GB RAM, up to 6 cores and `-gpu host` (the RTX; on Windows the
installer also sets the "high performance" graphics preference for the emulator), a 16 GB data partition,
quick-boot snapshots (resume in seconds from the SSD), and a 1920 px / 12 Mbps video stream.
Override with `EMU_RAM_MB`, `EMU_CORES`, `EMU_GPU`, `VIDEO_MAX`, `VIDEO_BITRATE`.
Keep your NVIDIA driver up to date (RTX 50-series needs a recent one). On Windows, enable *Windows Hypervisor Platform*
and BIOS virtualization (setup prints the exact command if acceleration is missing).

## Automatic behaviour
- Boots the bundled emulator when no device answers on adb.
- Checks installed marketplace apps and APK Loader itself for updates every 6 h (Updates tab, banner).
- Purges stored APK files older than 7 days.

## Features
- **Export app data without root**: *Installed → more → Export data (no root)* uses `run-as` (debuggable apps) and the app's
  public storage folder — works on ordinary, non-rooted phones, not just the bundled rootable emulator. The full tar **Backup**
  still needs a rootable device for private data on release-signed apps.
- **Battery simulation**: set a fake battery level/charging state (`dumpsys battery`) on the Device tab, for testing low-battery
  behaviour; works on the emulator and most real devices. **GPS location**: city presets or custom lat/lon (emulator only).
- **Scan a local folder**: point the Upload tab at a folder on this computer (default: your home folder; override with `SCAN_ROOT`)
  and install any `.apk/.xapk/.apks/.aab` found in it, without copying them into the app first.
- **App shortcuts** (like WSATools): *Installed → more → Desktop shortcut* puts a shortcut on your Desktop (and OneDrive Desktop) and
  Start menu that opens that one Android app: it starts APK Loader, boots the emulator if needed, and launches the app.
- **Open with / drag-and-drop**: drop `.apk/.xapk/.apks/.aab` files on the page, or right-click a file → *Open with → Install with APK Loader*
  (registered by the installer; the default app for APK files is not changed). `python run.py --install file.apk` does the same.
- **One instance**: starting APK Loader again reuses the running server instead of starting a second one.
- **App management** (like WSA Toolbox): force stop, clear data, version/install info, copy APK, back up data, uninstall.
- **WSA**: if the (discontinued) Windows Subsystem for Android is running, it is detected on `127.0.0.1:58526` and used.
- **Clipboard sync, audio, true multi-touch** through the official `scrcpy-server` (Genymobile, Apache-2.0). It is downloaded
  once from the upstream release, verified against a pinned SHA-256, pushed to the device and run with `app_process` (as the
  `shell` user, like scrcpy itself). Clipboard: the device clipboard flows to the browser automatically, Ctrl+V pastes any
  Unicode text, or use the Clipboard panel. Audio: press 🔊 (Android 11+; captured only while you listen; note that on real
  phones scrcpy's capture can mute the phone speaker while active). Pinch: Ctrl+drag, Ctrl+wheel / trackpad pinch, or two fingers on a
  touchscreen. `SCRCPY=0` disables the helper; *Input → adb only* skips its touch injection.
- **Scripted tests**: *Scripts* tab — record taps, drags, pinches, typing and keys, then replay at 0.5×–4× and N times.
  Scripts are JSON (fractional coordinates, so they work at any resolution) and can be edited, exported and imported. Add
  `launch`, `wait`, `assert_focus` and `screenshot` steps to turn a recording into a pass/fail test with a report and screenshots.
- **Responsive input**: one persistent `adb shell` per device (no process per tap) and, when the touchscreen node is writable,
  real-time press/drag/release via raw touch events (falls back to `input` for rotated screens). Click the screen, then type;
  Ctrl+V pastes text; the mouse wheel scrolls.
- **Several devices**: pick any emulator, USB or Wi-Fi phone from the device list; run more than one emulator (2 on 16 GB).
- **Wi-Fi pairing** (Android 11+ Wireless debugging, pairing code). Only local-network addresses are accepted.
- **History**: every install is logged; *Retry*, and *Roll back* (updates snapshot the old APK first; data is kept even when Android blocks downgrades).
- **Emulator snapshots**: save/restore the whole emulator state. **Network**: speed/latency presets (emulator), HTTP proxy and offline switch (any device).
- **Screen recording** to `.webm` in the browser; **Debug** tab with live CPU/RAM, top processes and a logcat viewer (level/package/text filter, crash buffer).
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
- **Google Play / APKMirror / APKPure**: no public download API, and scraping them breaks their terms, so there is no built-in downloader. Instead: one-click search links, plus an opt-in *auto-install APKs I download* watcher (Downloads folder, `WATCH_DIR`). For real Google Play, install with `python full_install.py --playstore` (Play Store emulator image; you sign in yourself; app-data backup is unavailable on that image).
- scrcpy-server is © Genymobile and is downloaded from its official release, not redistributed here. The client speaks scrcpy protocol 4.0 and the version must match exactly.
- Only install apps you are licensed to use.
- Tests: `pytest`.
