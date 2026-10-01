#!/usr/bin/env python3
"""One-shot installer for APK Loader on Windows, macOS and Linux (stdlib only).

    python full_install.py              fully automatic install, then launches the app
    python full_install.py --update     fetch the latest release and reinstall in place
    python full_install.py --uninstall  remove the app and its shortcuts
    python full_install.py --dir PATH   custom install directory
    python full_install.py --no-emulator  skip the Android emulator (bring your own device)
    python full_install.py --no-launch    do not start the app when finished

Everything is automatic: app files, virtualenv, dependencies, adb, Java, the
Android SDK + emulator + system image, bundletool, and a launcher shortcut on
the normal Desktop *and* the OneDrive Desktop (Windows folder backup).

NOTE: running the installer with the emulator enabled accepts the Android SDK
License Agreement on your behalf (https://developer.android.com/studio/terms).
Use --no-emulator to opt out. The emulator download is roughly 2 GB.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
import json
import tempfile
import urllib.request
import zipfile
from pathlib import Path

APP_NAME = "APK Loader"
SRC = Path(__file__).resolve().parent
PAYLOAD = ["app", "static", "plugins", "run.py", "requirements.txt", "README.md"]
REPO = "shovan29du/APK-loader"
PLAYSTORE = False
SYSTEM = platform.system()  # Windows / Darwin / Linux
PT_URL = "https://dl.google.com/android/repository/platform-tools-latest-{}.zip"
PT_OS = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}


def say(msg):
    print(f"==> {msg}", flush=True)


def is_store_python() -> bool:
    """Microsoft Store Python redirects writes under AppData to a private folder, which breaks
    virtual environments created there ("failed to locate pyvenv.cfg")."""
    return SYSTEM == "Windows" and "windowsapps" in (sys.executable + sys.base_prefix).lower()


def default_dir() -> Path:
    if SYSTEM == "Windows":
        if is_store_python():
            return Path.home() / "APKLoader"     # not under AppData, so not virtualized
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "APKLoader"
    if SYSTEM == "Darwin":
        return Path.home() / "Library/Application Support/APKLoader"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "apkloader"


def venv_python(dest: Path) -> Path:
    return dest / "venv" / ("Scripts/python.exe" if SYSTEM == "Windows" else "bin/python")


def pythonw(dest: Path) -> Path:
    """Windows: windowless interpreter so no console flashes; others: normal."""
    p = dest / "venv/Scripts/pythonw.exe"
    return p if SYSTEM == "Windows" and p.exists() else venv_python(dest)


# ---------------------------------------------------------------- desktops
def desktop_dirs() -> list[Path]:
    """Every distinct Desktop folder: the normal one AND the OneDrive one."""
    cands: list[Path] = [Path.home() / "Desktop"]
    if SYSTEM == "Windows":
        for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            if os.environ.get(var):
                cands.append(Path(os.environ[var]) / "Desktop")
        # Ask Windows where Desktop really is (follows OneDrive folder redirection).
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[Environment]::GetFolderPath('Desktop')"],
                capture_output=True, text=True, timeout=30).stdout.strip()
            if out:
                cands.append(Path(out))
        except Exception:
            pass
        try:  # any "OneDrive - Company" folders in the profile
            cands += [p / "Desktop" for p in Path.home().glob("OneDrive*")]
        except Exception:
            pass
    elif SYSTEM == "Linux":
        try:
            out = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True,
                                 text=True, timeout=10).stdout.strip()
            if out:
                cands.append(Path(out))
        except Exception:
            pass
    seen, result = set(), []
    for c in cands:
        try:
            key = c.resolve()
        except OSError:
            continue
        if c.is_dir() and key not in seen:
            seen.add(key)
            result.append(c)
    return result


# ---------------------------------------------------------------- shortcuts
def make_windows_shortcut(lnk: Path, target: Path, args: str, workdir: Path, icon: Path | None):
    def q(s):  # PowerShell single-quote escaping
        return "'" + str(s).replace("'", "''") + "'"
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({q(lnk)});"
          f"$s.TargetPath={q(target)};$s.Arguments={q(args)};"
          f"$s.WorkingDirectory={q(workdir)};$s.Description={q(APP_NAME)};"
          + (f"$s.IconLocation={q(icon)};" if icon else "") + "$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=60)


def make_shortcuts(dest: Path) -> list[Path]:
    made: list[Path] = []
    run_py = dest / "run.py"
    py = pythonw(dest)
    desktops = desktop_dirs()
    if not desktops:
        say("No Desktop folder found; skipping shortcuts.")
    for d in desktops:
        try:
            if SYSTEM == "Windows":
                lnk = d / f"{APP_NAME}.lnk"
                make_windows_shortcut(lnk, py, f'"{run_py}"', dest, None)
                made.append(lnk)
            elif SYSTEM == "Darwin":
                f = d / f"{APP_NAME}.command"
                f.write_text(f'#!/bin/bash\nexec "{py}" "{run_py}"\n')
                f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                made.append(f)
            else:
                f = d / f"{APP_NAME}.desktop"
                f.write_text(desktop_entry(py, run_py, dest))
                f.chmod(0o755)
                subprocess.run(["gio", "set", str(f), "metadata::trusted", "true"],
                               capture_output=True)
                made.append(f)
            say(f"Shortcut: {made[-1]}")
        except Exception as e:
            say(f"Could not create shortcut in {d}: {e}")
    if SYSTEM == "Linux":  # also register in the applications menu
        apps = Path.home() / ".local/share/applications"
        apps.mkdir(parents=True, exist_ok=True)
        f = apps / "apkloader.desktop"
        f.write_text(desktop_entry(py, run_py, dest, with_files=True))      # also offered for .apk files
        made.append(f)
    return made


def desktop_entry(py: Path, run_py: Path, dest: Path, with_files: bool = False) -> str:
    mime = "MimeType=application/vnd.android.package-archive;\n" if with_files else ""
    exec_line = f'"{py}" "{run_py}" --install %F' if with_files else f'"{py}" "{run_py}"'
    return ("[Desktop Entry]\nType=Application\n"
            f"Name={APP_NAME}\nComment=Install and run Android APKs in your browser\n"
            f"Exec={exec_line}\nPath={dest}\nTerminal=false\nCategories=Utility;\n{mime}")


APK_EXTS = (".apk", ".xapk", ".apks", ".aab")
PROG_ID = "APKLoader.AndroidPackage"


def register_open_with(dest: Path):
    """Windows: add APK Loader to the 'Open with' list of Android package files (does not change the default app)."""
    if SYSTEM != "Windows":
        return
    import winreg
    cmd = f'"{pythonw(dest)}" "{dest / "run.py"}" --install "%1"'
    base = r"Software\Classes"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{base}\{PROG_ID}") as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "Android package (APK Loader)")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{base}\{PROG_ID}\shell\open\command") as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, cmd)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{base}\{PROG_ID}\shell\open") as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "Install with APK Loader")
    for ext in APK_EXTS:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{base}\{ext}\OpenWithProgids") as k:
            winreg.SetValueEx(k, PROG_ID, 0, winreg.REG_NONE, b"")
    say("Registered 'Open with > Install with APK Loader' for .apk/.xapk/.apks/.aab files.")


def unregister_open_with():
    if SYSTEM != "Windows":
        return
    import winreg
    base = r"Software\Classes"
    for ext in APK_EXTS:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{base}\{ext}\OpenWithProgids", 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, PROG_ID)
        except OSError:
            pass
    for sub in ("shell\\open\\command", "shell\\open", "shell"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{base}\{PROG_ID}\{sub}")
        except OSError:
            pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{base}\{PROG_ID}")
    except OSError:
        pass


# ---------------------------------------------------------------- install steps
def check_python():
    if sys.version_info < (3, 10):
        sys.exit("Python 3.10+ is required (https://www.python.org/downloads/).")
    try:
        __import__("venv")
        __import__("ensurepip")
    except ImportError:
        hint = "sudo apt install python3-venv" if SYSTEM == "Linux" else "reinstall Python with pip/venv"
        sys.exit(f"Python venv support is missing. Try: {hint}")


def copy_payload(dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    for name in PAYLOAD:
        s, t = SRC / name, dest / name
        if not s.exists():
            sys.exit(f"Missing {s}; run full_install.py from the project folder.")
        if s.is_dir():
            if t.exists():
                shutil.rmtree(t)
            shutil.copytree(s, t, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(s, t)


def venv_healthy(dest: Path) -> bool:
    """A usable venv has pyvenv.cfg and an interpreter that actually starts."""
    py = venv_python(dest)
    if not (dest / "venv" / "pyvenv.cfg").is_file() or not py.is_file():
        return False
    try:
        return subprocess.run([str(py), "-c", "import sys"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def build_venv(dest: Path):
    if (dest / "venv").exists() and not venv_healthy(dest):
        say("Existing virtual environment is broken (an earlier install was interrupted); recreating it…")
        shutil.rmtree(dest / "venv", ignore_errors=True)
    if not venv_healthy(dest):
        say("Creating virtual environment…")
        subprocess.run([sys.executable, "-m", "venv", "--clear", str(dest / "venv")], check=True)
        if not venv_healthy(dest):
            hint = ("You are using the Microsoft Store build of Python, which cannot create a working environment here. "
                    "Install Python from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), "
                    "then run:  py -3 full_install.py" if is_store_python() else
                    "The new environment does not start. Try another Python 3.10+, or choose a folder you own with --dir.")
            sys.exit(f"Could not create a working virtual environment in {dest / 'venv'}.\n{hint}")
    say("Installing dependencies…")
    py = str(venv_python(dest))
    up = subprocess.run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    if up.returncode != 0:
        say("Could not upgrade pip (continuing with the bundled version).")
    # Runtime deps only (no test tooling).
    reqs = [l.strip() for l in (dest / "requirements.txt").read_text().splitlines()
            if l.strip() and not l.lower().startswith(("pytest",))]
    subprocess.run([py, "-m", "pip", "install", "--quiet", *reqs], check=True)


def install_adb(dest: Path):
    pt = dest / "platform-tools"
    exe = pt / ("adb.exe" if SYSTEM == "Windows" else "adb")
    if exe.exists():
        return say("adb already present.")
    if shutil.which("adb"):
        return say("Using adb found on PATH.")
    say("Downloading Android platform-tools (adb) from Google…")
    with tempfile.TemporaryDirectory() as tmp:
        z = Path(tmp) / "pt.zip"
        urllib.request.urlretrieve(PT_URL.format(PT_OS[SYSTEM]), z)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(dest)  # zip already contains a platform-tools/ folder
    if SYSTEM != "Windows":
        for f in pt.iterdir():
            if f.is_file() and f.suffix == "":
                f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def setup_emulator(dest: Path):
    if (dest / "android-sdk" / "emulator").exists():
        return say("Android emulator already set up.")
    say("Setting up the Android emulator (Java, SDK, system image, bundletool)…")
    subprocess.run([str(venv_python(dest)), str(dest / "run.py"), "--hardware"], cwd=dest)
    extra = ["--playstore"] if PLAYSTORE else []
    r = subprocess.run([str(venv_python(dest)), str(dest / "run.py"), "--setup-emulator", *extra], cwd=dest)
    if r.returncode != 0:
        say("Emulator setup failed. The app still works with your own adb device; "
            "retry later with:  python run.py --setup-emulator")


def launch(dest: Path):
    say("Starting APK Loader…")
    kw = {"creationflags": 0x00000008 | 0x00000200} if SYSTEM == "Windows" else {"start_new_session": True}
    subprocess.Popen([str(pythonw(dest)), str(dest / "run.py")], cwd=dest,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def cleanup_old_broken_install(dest: Path):
    """An earlier run on Store Python left a broken venv under AppData; remove just that venv."""
    if SYSTEM != "Windows":
        return
    old = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "APKLoader"
    if old.resolve() != dest.resolve() and (old / "venv").exists() and not venv_healthy(old):
        say(f"Removing the broken environment left by an earlier attempt: {old / 'venv'}")
        shutil.rmtree(old / "venv", ignore_errors=True)


def install(dest: Path, with_adb: bool, with_emulator: bool = True, do_launch: bool = True):
    check_python()
    if is_store_python():
        say("Microsoft Store Python detected: installing outside AppData to avoid its file virtualization.")
    cleanup_old_broken_install(dest)
    say(f"Installing to {dest}")
    copy_payload(dest)
    build_venv(dest)
    if with_adb:
        try:
            install_adb(dest)
        except Exception as e:
            say(f"adb download failed ({e}). Install Android platform-tools manually.")
    say("Downloading the scrcpy helper (clipboard, audio, multi-touch)…")
    r = subprocess.run([str(venv_python(dest)), str(dest / "run.py"), "--setup-helper"], cwd=dest)
    if r.returncode != 0:
        say("Helper download failed; it is fetched again on first use (features fall back meanwhile).")
    if with_emulator:
        setup_emulator(dest)
    shortcuts = make_shortcuts(dest)
    try:
        register_open_with(dest)
    except Exception as e:
        say(f"Could not register 'Open with' ({e}); everything else works.")
    (dest / "shortcuts.txt").write_text("\n".join(map(str, shortcuts)))
    say(f"Done. Launch '{APP_NAME}' from your Desktop, or run: {venv_python(dest)} {dest / 'run.py'}")
    if not with_emulator:
        say("No emulator installed: connect a device/emulator via adb (set ADB_SERIAL). See README.md.")
    if do_launch:
        launch(dest)


def update(dest: Path, ref: str | None):
    """Download the newest release (or branch/tag `ref`) and reinstall over the current install."""
    if not ref:
        try:
            req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                         headers={"User-Agent": "apk-loader"})
            ref = json.load(urllib.request.urlopen(req, timeout=30))["tag_name"]
        except Exception:
            sys.exit("No published release found. Use --ref BRANCH_OR_TAG to update from a specific ref.")
    say(f"Updating to {ref}…")
    with tempfile.TemporaryDirectory() as t:
        z = Path(t) / "src.zip"
        urllib.request.urlretrieve(f"https://github.com/{REPO}/archive/{ref}.zip", z)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(t)
        root = next(p for p in Path(t).iterdir() if p.is_dir())
        subprocess.run([sys.executable, str(root / "full_install.py"), "--dir", str(dest),
                        "--no-launch"], check=True)
    launch(dest)


def uninstall(dest: Path):
    unregister_open_with()
    say("Removing shortcuts…")
    listing = dest / "shortcuts.txt"
    if listing.exists():
        for line in listing.read_text().splitlines():
            Path(line).unlink(missing_ok=True)
    if dest.exists():
        say(f"Removing {dest} (downloaded APKs inside are deleted too)")
        shutil.rmtree(dest, ignore_errors=True)
    say("Uninstalled.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=default_dir())
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-adb", action="store_true")
    ap.add_argument("--no-emulator", action="store_true")
    ap.add_argument("--no-launch", action="store_true")
    ap.add_argument("--playstore", action="store_true",
                    help="use the emulator image that includes Google Play (you sign in yourself; no root)")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--ref", help="branch or tag for --update")
    a = ap.parse_args()
    global PLAYSTORE
    PLAYSTORE = a.playstore
    dest = a.dir.expanduser().resolve()
    if a.uninstall:
        uninstall(dest)
    elif a.update:
        update(dest, a.ref)
    else:
        install(dest, not a.no_adb, not a.no_emulator, not a.no_launch)


if __name__ == "__main__":
    main()
