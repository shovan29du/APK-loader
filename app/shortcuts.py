"""Desktop / Start-menu shortcuts that open one Android app on the laptop (like WSATools).

The shortcut starts APK Loader (or reuses the running one) with `--app <package>`: it boots the
emulator if needed, launches the app and shows the phone view in the browser.
"""
import os
import re
import sys
from pathlib import Path

from . import adb, procs

WIN = sys.platform == "win32"


def launcher_command(package: str) -> list[str]:
    """argv that starts APK Loader and opens `package`."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--app", package]
    exe = Path(sys.executable)
    py = exe.with_name("pythonw.exe") if WIN and exe.with_name("pythonw.exe").exists() else exe
    return [str(py), str(Path(__file__).resolve().parent.parent / "run.py"), "--app", package]


def safe_label(label: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", label).strip(" .")[:60] or "Android app"


def desktop_dirs() -> list[Path]:
    """Normal Desktop and the OneDrive Desktop (Windows folder backup), whichever exist."""
    cands = [Path.home() / "Desktop"]
    if WIN:
        for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            if os.environ.get(var):
                cands.append(Path(os.environ[var]) / "Desktop")
        try:
            out = procs.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                            capture_output=True, text=True, timeout=30).stdout.strip()
            if out:
                cands.append(Path(out))
        except Exception:
            pass
    elif not sys.platform == "darwin":
        try:
            out = procs.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=10).stdout.strip()
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


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def create(package: str, label: str = "") -> list[str]:
    """Create the shortcut on every Desktop (+ the Start menu / app menu). Returns the paths."""
    if not adb.valid_package(package):
        raise adb.AdbError("invalid package name")
    cmd = launcher_command(package)
    name = safe_label(label or package)
    made: list[Path] = []
    targets = desktop_dirs()
    if WIN:
        start = Path(os.environ.get("APPDATA", Path.home())) / "Microsoft/Windows/Start Menu/Programs"
        targets = targets + ([start] if start.is_dir() else [])
        for d in targets:
            lnk = d / f"{name} (Android).lnk"
            args = " ".join(f'"{a}"' for a in cmd[1:])
            ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({_q(str(lnk))});"
                  f"$s.TargetPath={_q(cmd[0])};$s.Arguments={_q(args)};$s.Description={_q('Open ' + name + ' in APK Loader')};$s.Save()")
            procs.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=60)
            made.append(lnk)
    elif sys.platform == "darwin":
        for d in targets:
            f = d / f"{name} (Android).command"
            f.write_text("#!/bin/bash\nexec " + " ".join(f'"{a}"' for a in cmd) + "\n")
            f.chmod(0o755)
            made.append(f)
    else:
        apps = Path.home() / ".local/share/applications"
        apps.mkdir(parents=True, exist_ok=True)
        for d in targets + [apps]:
            f = d / f"{name} (Android).desktop"
            f.write_text("[Desktop Entry]\nType=Application\nName=" + f"{name} (Android)\n"
                         + "Exec=" + " ".join(f'"{a}"' for a in cmd) + "\nTerminal=false\nCategories=Utility;\n")
            f.chmod(0o755)
            made.append(f)
    return [str(p) for p in made]
