"""Split-APK containers: .xapk, .apks (bundletool) and .aab."""
import asyncio
import json
import os
import posixpath
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import adb, config
from .util import safe_extract_zip

ABI_RE = re.compile(r"(?:^|[._-])(arm64[_-]v8a|armeabi[_-]v7a|x86[_-]64|x86|armeabi|mips64|mips)$")
DPI_RE = re.compile(r"(?:^|[._-])(ldpi|mdpi|hdpi|xhdpi|xxhdpi|xxxhdpi|tvdpi|nodpi)$")
DPI_VALUES = {"ldpi": 120, "mdpi": 160, "tvdpi": 213, "hdpi": 240, "xhdpi": 320,
              "xxhdpi": 480, "xxxhdpi": 640}


def _norm(s: str) -> str:
    return s.replace("-", "_")


def select_splits(names: list[str], abis: list[str], density: int) -> list[str]:
    """Keep the base + config splits that fit the device (best ABI, nearest density)."""
    abis = [_norm(a) for a in abis]
    abi_of, dpi_of = {}, {}
    for n in names:
        stem = posixpath.basename(n)[:-4]
        if m := ABI_RE.search(stem):
            abi_of[n] = _norm(m.group(1))
        elif m := DPI_RE.search(stem):
            dpi_of[n] = m.group(1)
    keep_abi = next((a for a in abis if a in abi_of.values()), None)
    dpis = {v for v in dpi_of.values() if v in DPI_VALUES}
    keep_dpi = min(dpis, key=lambda d: abs(DPI_VALUES[d] - density), default=None)
    out = []
    for n in names:
        if n in abi_of and abi_of[n] != keep_abi:
            continue
        if n in dpi_of and dpi_of[n] != keep_dpi and dpi_of[n] != "nodpi":
            continue
        out.append(n)
    return out


@dataclass
class Bundle:
    apks: list[Path]
    obbs: list[tuple[Path, str]] = field(default_factory=list)  # (local, remote path)
    package: str = ""


def extract_bundle(path: Path, dest: Path, abis: list[str], density: int) -> Bundle:
    limit = config.MAX_APK_MB * 1024 * 1024 * 4
    with zipfile.ZipFile(path) as zf:
        files = safe_extract_zip(zf, dest, limit)
    rel = {str(f.relative_to(dest.resolve())).replace("\\", "/"): f for f in files}
    package, obbs, chosen = "", [], None
    if "manifest.json" in rel:
        try:
            man = json.loads(rel["manifest.json"].read_text(encoding="utf-8-sig"))
            package = man.get("package_name", "")
            listed = [s["file"] for s in man.get("split_apks", []) if s.get("file") in rel]
            chosen = listed or None
            for e in man.get("expansions", []):
                if e.get("file") in rel and e.get("install_path"):
                    remote = posixpath.normpath("/sdcard/" + e["install_path"].lstrip("/"))
                    if remote.startswith("/sdcard/Android/obb/"):
                        obbs.append((rel[e["file"]], remote))
        except (ValueError, KeyError):
            pass
    if chosen is None:
        if "universal.apk" in rel:
            chosen = ["universal.apk"]
        else:
            chosen = [n for n in rel if n.endswith(".apk") and not n.startswith("standalones/")]
    if not obbs and package:
        for n, f in rel.items():
            if n.lower().endswith(".obb"):
                obbs.append((f, f"/sdcard/Android/obb/{package}/{posixpath.basename(n)}"))
    chosen = select_splits(sorted(chosen), abis, density) if len(chosen) > 1 else chosen
    if not chosen:
        raise ValueError("no APKs found inside the archive")
    return Bundle([rel[n] for n in chosen], obbs, package)


async def install_bundle(path: Path) -> str:
    info = await adb.device_info()
    dest = path.parent / (path.stem + "_x")
    bundle = await asyncio.to_thread(extract_bundle, path, dest, info["abis"], info["density"])
    if len(bundle.apks) == 1:
        await adb.install(str(bundle.apks[0]))
    else:
        # base APK first, as some Android versions require
        apks = sorted(bundle.apks, key=lambda p: (not re.search(r"base|^0_", p.name), p.name))
        await adb.install_multiple([str(p) for p in apks])
    for local, remote in bundle.obbs:
        await adb._dev("shell", f"mkdir -p '{posixpath.dirname(remote)}'")
        await adb.push(str(local), remote)
    shutil.rmtree(dest, ignore_errors=True)
    return bundle.package


# ---------- .aab via bundletool ----------

def bundletool_jar() -> Path | None:
    for c in (os.getenv("BUNDLETOOL_JAR"), config.DATA_DIR / "tools" / "bundletool.jar"):
        if c and Path(c).is_file():
            return Path(c)
    return None


def java_bin() -> str | None:
    from . import emulator
    return emulator.java_exe()


async def _exec(*args: str, timeout: float = 900):
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out = (await asyncio.wait_for(proc.communicate(), timeout))[0].decode(errors="replace")
    if proc.returncode != 0:
        raise adb.AdbError(out.strip()[-600:])
    return out


async def install_aab(path: Path) -> str:
    jar, java = bundletool_jar(), java_bin()
    if not jar or not java:
        raise adb.AdbError(".aab needs Java and bundletool.jar; run full_install.py to set them up")
    ks = config.DATA_DIR / "tools" / "apkloader.keystore"
    if not ks.exists():
        keytool = str(Path(java).with_name("keytool.exe" if os.name == "nt" else "keytool"))
        ks.parent.mkdir(parents=True, exist_ok=True)
        await _exec(keytool, "-genkeypair", "-keystore", str(ks), "-alias", "apkloader",
                    "-storepass", "android", "-keypass", "android", "-dname", "CN=APK Loader",
                    "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000")
    apks = path.with_suffix(".apks")
    adb_bin = shutil.which(config.ADB_BIN) or config.ADB_BIN
    common = [f"--adb={adb_bin}", f"--device-id={config.ADB_SERIAL}"]
    await _exec(java, "-jar", str(jar), "build-apks", f"--bundle={path}", f"--output={apks}",
                "--overwrite", "--connected-device", f"--ks={ks}", "--ks-pass=pass:android",
                "--ks-key-alias=apkloader", "--key-pass=pass:android", *common)
    await _exec(java, "-jar", str(jar), "install-apks", f"--apks={apks}", *common)
    return ""
