#!/usr/bin/env python3
"""One-shot installer for APK Loader on Windows, macOS and Linux (stdlib only).

    python full_install.py              install (or update) and create shortcuts
    python full_install.py --uninstall  remove the app and its shortcuts
    python full_install.py --dir PATH   custom install directory
    python full_install.py --no-adb     skip downloading Android platform-tools

It copies the app to a per-user directory, builds a virtualenv, installs the
dependencies, fetches adb from Google, and puts a launcher shortcut on the
normal Desktop *and* the OneDrive Desktop (Windows folder backup) if present.
"""
import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

APP_NAME = "APK Loader"
SRC = Path(__file__).resolve().parent
PAYLOAD = ["app", "static", "run.py", "requirements.txt", "README.md"]
SYSTEM = platform.system()  # Windows / Darwin / Linux
PT_URL = "https://dl.google.com/android/repository/platform-tools-latest-{}.zip"
PT_OS = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}


def say(msg):
    print(f"==> {msg}", flush=True)


def default_dir() -> Path:
    if SYSTEM == "Windows":
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
        f.write_text(desktop_entry(py, run_py, dest))
        made.append(f)
    return made


def desktop_entry(py: Path, run_py: Path, dest: Path) -> str:
    return ("[Desktop Entry]\nType=Application\n"
            f"Name={APP_NAME}\nComment=Install and run Android APKs in your browser\n"
            f'Exec="{py}" "{run_py}"\nPath={dest}\nTerminal=false\nCategories=Utility;\n')


# ---------------------------------------------------------------- install steps
def check_python():
    if sys.version_info < (3, 9):
        sys.exit("Python 3.9+ is required.")
    try:
        import venv  # noqa: F401
        import ensurepip  # noqa: F401
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


def build_venv(dest: Path):
    if not venv_python(dest).exists():
        say("Creating virtual environment…")
        subprocess.run([sys.executable, "-m", "venv", str(dest / "venv")], check=True)
    say("Installing dependencies…")
    py = str(venv_python(dest))
    subprocess.run([py, "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=True)
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


def install(dest: Path, with_adb: bool):
    check_python()
    say(f"Installing to {dest}")
    copy_payload(dest)
    build_venv(dest)
    if with_adb:
        try:
            install_adb(dest)
        except Exception as e:
            say(f"adb download failed ({e}). Install Android platform-tools manually.")
    shortcuts = make_shortcuts(dest)
    (dest / "shortcuts.txt").write_text("\n".join(map(str, shortcuts)))
    say(f"Done. Launch '{APP_NAME}' from your Desktop, or run: {venv_python(dest)} {dest / 'run.py'}")
    say("Note: you also need an Android device/emulator reachable by adb "
        "(default localhost:5555; set ADB_SERIAL). See README.md.")


def uninstall(dest: Path):
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
    a = ap.parse_args()
    dest = a.dir.expanduser().resolve()
    if a.uninstall:
        uninstall(dest)
    else:
        install(dest, not a.no_adb)


if __name__ == "__main__":
    main()
