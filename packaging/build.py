#!/usr/bin/env python3
"""Build a self-contained package (no Python needed by the user) for the current OS.

    pip install pyinstaller && python packaging/build.py

Windows -> dist/APKLoader-Setup.exe   (needs Inno Setup: iscc on PATH; else a portable .zip)
macOS   -> dist/APKLoader.dmg         (unsigned; see README for notarization)
Linux   -> dist/apkloader_<ver>_<arch>.deb
Each bundle ships Google's adb (platform-tools); the emulator is set up on first use from the UI.

Code signing (optional; skipped with a notice when the secrets are absent):
  Windows  WIN_CERT_PFX_B64 (base64 .pfx), WIN_CERT_PASSWORD           -> signtool, SHA-256 + timestamp
  macOS    MACOS_CERT_P12_B64 (base64 Developer ID .p12), MACOS_CERT_PASSWORD, MACOS_SIGN_IDENTITY
           ("Developer ID Application: Name (TEAMID)"), and for notarization
           APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD (app-specific password)
"""
from __future__ import annotations

import base64
import glob
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
            "--hidden-import", "multipart", "--hidden-import", "python_multipart", "--hidden-import", "httpx",
            "--distpath", str(DIST / "pyi"), "--workpath", str(ROOT / "build"),
            "--specpath", str(ROOT / "build")]
    if SYSTEM != "Linux":
        args += ["--windowed"]  # no console window; the browser is the UI
    if SYSTEM == "Darwin" and os.getenv("MACOS_SIGN_IDENTITY"):
        args += ["--codesign-identity", os.environ["MACOS_SIGN_IDENTITY"],
                 "--osx-entitlements-file", str(ROOT / "packaging" / "entitlements.plist")]
    run(sys.executable, "-m", "PyInstaller", *args, ROOT / "run.py")
    out = DIST / "pyi" / ("APKLoader.app" if SYSTEM == "Darwin" else "APKLoader")
    bundle_adb(out / "Contents/Resources" if SYSTEM == "Darwin" else out)
    return out


def bundle_adb(target: Path):
    if os.getenv("SKIP_ADB_BUNDLE"):
        return print("SKIP_ADB_BUNDLE set: not bundling adb")
    with tempfile.TemporaryDirectory() as t:
        z = Path(t) / "pt.zip"
        urllib.request.urlretrieve(f"https://dl.google.com/android/repository/platform-tools-latest-{PT}.zip", z)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(target)
    target.mkdir(parents=True, exist_ok=True)
    if SYSTEM != "Windows":
        for f in (target / "platform-tools").iterdir():
            if f.is_file() and not f.suffix:
                f.chmod(0o755)


# ------------------------------------------------------------------ code signing
def find_signtool() -> str | None:
    if shutil.which("signtool"):
        return shutil.which("signtool")
    hits = sorted(glob.glob(r"C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe"))
    return hits[-1] if hits else None


def sign_windows(files: list[Path]):
    pfx_b64, pwd = os.getenv("WIN_CERT_PFX_B64"), os.getenv("WIN_CERT_PASSWORD", "")
    if not pfx_b64:
        return print("Windows code signing skipped (WIN_CERT_PFX_B64 not set): SmartScreen will warn.")
    tool = find_signtool()
    if not tool:
        return print("signtool not found; skipping Windows signing")
    with tempfile.TemporaryDirectory() as t:
        pfx = Path(t) / "cert.pfx"
        pfx.write_bytes(base64.b64decode(pfx_b64))
        for f in files:
            run(tool, "sign", "/f", pfx, "/p", pwd, "/fd", "sha256", "/tr", "http://timestamp.digicert.com",
                "/td", "sha256", "/d", "APK Loader", f)
            run(tool, "verify", "/pa", f)


def macos_keychain() -> str | None:
    """Import the Developer ID certificate into a throwaway keychain (CI)."""
    b64 = os.getenv("MACOS_CERT_P12_B64")
    if not b64:
        return None
    kc, pw = str(ROOT / "build" / "signing.keychain-db"), "ci-" + os.urandom(8).hex()
    p12 = ROOT / "build" / "cert.p12"
    p12.parent.mkdir(exist_ok=True)
    p12.write_bytes(base64.b64decode(b64))
    run("security", "create-keychain", "-p", pw, kc)
    run("security", "set-keychain-settings", "-lut", "21600", kc)
    run("security", "unlock-keychain", "-p", pw, kc)
    run("security", "import", p12, "-k", kc, "-P", os.getenv("MACOS_CERT_PASSWORD", ""),
        "-T", "/usr/bin/codesign", "-T", "/usr/bin/security")
    run("security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k", pw, kc)
    run("security", "list-keychains", "-d", "user", "-s", kc, "login.keychain-db")
    p12.unlink()
    return kc


def sign_macos_app(app: Path):
    ident = os.getenv("MACOS_SIGN_IDENTITY")
    if not ident:
        return print("macOS signing skipped (MACOS_SIGN_IDENTITY not set): Gatekeeper will warn.")
    opts = ["--force", "--options", "runtime", "--timestamp", "--sign", ident]
    for f in (app / "Contents/Resources/platform-tools").glob("*"):       # adb etc. were added after the build
        if f.is_file() and os.access(f, os.X_OK):
            run("codesign", *opts, f)
    run("codesign", "--deep", *opts, "--entitlements", ROOT / "packaging/entitlements.plist", app)
    run("codesign", "--verify", "--deep", "--strict", "--verbose=2", app)


def notarize_dmg(dmg: Path):
    ident = os.getenv("MACOS_SIGN_IDENTITY")
    if not ident:
        return
    run("codesign", "--force", "--timestamp", "--sign", ident, dmg)
    if all(os.getenv(k) for k in ("APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_PASSWORD")):
        run("xcrun", "notarytool", "submit", dmg, "--apple-id", os.environ["APPLE_ID"],
            "--team-id", os.environ["APPLE_TEAM_ID"], "--password", os.environ["APPLE_APP_PASSWORD"], "--wait")
        run("xcrun", "stapler", "staple", dmg)
    else:
        print("Notarization skipped (APPLE_ID / APPLE_TEAM_ID / APPLE_APP_PASSWORD not set).")


def windows(app: Path):
    iss = ROOT / "packaging" / "windows.iss"
    sign_windows([app / "APKLoader.exe"])            # sign the program itself, then the installer around it
    if shutil.which("iscc"):
        run("iscc", f"/DAppVersion={__version__}", f"/DSourceDir={app}", f"/O{DIST}", iss)
        sign_windows([DIST / "APKLoader-Setup.exe"])
    else:
        print("Inno Setup (iscc) not found; producing a portable zip instead.")
        shutil.make_archive(str(DIST / "APKLoader-portable"), "zip", app.parent, app.name)


def macos(app: Path):
    sign_macos_app(app)
    dmg = DIST / "APKLoader.dmg"
    dmg.unlink(missing_ok=True)
    stage = DIST / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copytree(app, stage / "APKLoader.app", symlinks=True)
    (stage / "Applications").symlink_to("/Applications")
    run("hdiutil", "create", "-volname", "APK Loader", "-srcfolder", stage, "-ov", "-format", "UDZO", dmg)
    notarize_dmg(dmg)


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
    if SYSTEM == "Darwin":
        macos_keychain()     # PyInstaller signs collected binaries with the identity, so the key must be ready
    app = pyinstaller()
    {"Windows": windows, "Darwin": macos, "Linux": linux}[SYSTEM](app)
    import hashlib
    files = sorted(p for p in DIST.iterdir() if p.is_file() and p.name != "SHA256SUMS.txt")
    (DIST / "SHA256SUMS.txt").write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in files))
    print("Built:", *[p.name for p in files], "+ SHA256SUMS.txt")


if __name__ == "__main__":
    main()
