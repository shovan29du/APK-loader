#!/usr/bin/env python3
"""Build a self-contained package (no Python needed by the user) for the current OS.

    pip install pyinstaller && python packaging/build.py

Windows -> dist/APKLoader-Setup.exe   (needs Inno Setup: iscc on PATH; else a portable .zip)
macOS   -> dist/APKLoader.dmg         (unsigned; see README for notarization)
Linux   -> dist/apkloader_<ver>_<arch>.deb
Each bundle ships Google's adb (platform-tools); the emulator is set up on first use from the UI.
"""
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
SYSTEM = platform.system()
sys.path.insert(0, str(ROOT))
from app import __version__  # noqa: E402

PT = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}[SYSTEM]


def run(*cmd, **kw):
    print("+", " ".join(map(str, cmd)))
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def pyinstaller() -> Path:
    sep = ";" if SYSTEM == "Windows" else ":"
    args = ["--noconfirm", "--clean", "--name", "APKLoader", "--onedir",
            "--add-data", f"{ROOT / 'static'}{sep}static",
            "--add-data", f"{ROOT / 'plugins'}{sep}plugins",
            "--collect-submodules", "uvicorn", "--collect-submodules", "app",
            "--hidden-import", "multipart", "--hidden-import", "httpx",
            "--distpath", str(DIST / "pyi"), "--workpath", str(ROOT / "build"),
            "--specpath", str(ROOT / "build")]
    if SYSTEM != "Linux":
        args += ["--windowed"]  # no console window; the browser is the UI
    run(sys.executable, "-m", "PyInstaller", *args, ROOT / "run.py")
    out = DIST / "pyi" / ("APKLoader.app" if SYSTEM == "Darwin" else "APKLoader")
    bundle_adb(out / "Contents/MacOS" if SYSTEM == "Darwin" else out)
    return out


def bundle_adb(target: Path):
    if os.getenv("SKIP_ADB_BUNDLE"):
        return print("SKIP_ADB_BUNDLE set: not bundling adb")
    with tempfile.TemporaryDirectory() as t:
        z = Path(t) / "pt.zip"
        urllib.request.urlretrieve(f"https://dl.google.com/android/repository/platform-tools-latest-{PT}.zip", z)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(target)
    if SYSTEM != "Windows":
        for f in (target / "platform-tools").iterdir():
            if f.is_file() and not f.suffix:
                f.chmod(0o755)


def windows(app: Path):
    iss = ROOT / "packaging" / "windows.iss"
    if shutil.which("iscc"):
        run("iscc", f"/DAppVersion={__version__}", f"/DSourceDir={app}", f"/O{DIST}", iss)
    else:
        print("Inno Setup (iscc) not found; producing a portable zip instead.")
        shutil.make_archive(str(DIST / "APKLoader-portable"), "zip", app.parent, app.name)


def macos(app: Path):
    dmg = DIST / "APKLoader.dmg"
    dmg.unlink(missing_ok=True)
    stage = DIST / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copytree(app, stage / "APKLoader.app", symlinks=True)
    (stage / "Applications").symlink_to("/Applications")
    run("hdiutil", "create", "-volname", "APK Loader", "-srcfolder", stage, "-ov", "-format", "UDZO", dmg)


def linux(app: Path):
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    root = DIST / "deb"
    shutil.rmtree(root, ignore_errors=True)
    (root / "opt").mkdir(parents=True)
    shutil.copytree(app, root / "opt" / "apkloader", symlinks=True)
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/bin/apkloader").symlink_to("/opt/apkloader/APKLoader")
    (root / "usr/share/applications").mkdir(parents=True)
    (root / "usr/share/applications/apkloader.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=APK Loader\n"
        "Comment=Install and run Android APKs in your browser\n"
        "Exec=/opt/apkloader/APKLoader\nTerminal=false\nCategories=Utility;\n")
    (root / "DEBIAN").mkdir()
    (root / "DEBIAN/control").write_text(
        f"Package: apkloader\nVersion: {__version__}\nSection: utils\nPriority: optional\n"
        f"Architecture: {arch}\nMaintainer: APK Loader\n"
        "Description: Install and run Android APKs in your browser\n")
    run("dpkg-deb", "--build", "--root-owner-group", root, DIST / f"apkloader_{__version__}_{arch}.deb")


def main():
    DIST.mkdir(exist_ok=True)
    app = pyinstaller()
    {"Windows": windows, "Darwin": macos, "Linux": linux}[SYSTEM](app)
    print("Built:", *sorted(p.name for p in DIST.iterdir() if p.is_file()))


if __name__ == "__main__":
    main()
