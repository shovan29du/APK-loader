"""Pre-install checks: hashes, archive sanity, signatures, optional VirusTotal."""
import asyncio
import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import config, procs


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | info
    detail: str = ""


@dataclass
class Report:
    sha256: str = ""
    md5: str = ""
    size: int = 0
    checks: list[Check] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(c.status == "fail" for c in self.checks)

    def dict(self):
        return {"sha256": self.sha256, "md5": self.md5, "size": self.size,
                "blocked": self.blocked,
                "checks": [c.__dict__ for c in self.checks]}


def hashes(path: Path) -> tuple[str, str]:
    s, m = hashlib.sha256(), hashlib.md5()  # md5 only for marketplaces that publish it
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            s.update(chunk)
            m.update(chunk)
    return s.hexdigest(), m.hexdigest()


def _build_tool(name: str) -> str | None:
    if shutil.which(name):
        return shutil.which(name)
    for r in (config.DATA_DIR / "android-sdk", Path(os.getenv("ANDROID_HOME", "/nonexistent"))):
        bt = r / "build-tools"
        if bt.is_dir():
            for v in sorted(bt.iterdir(), reverse=True):
                for n in (name, name + ".exe", name + ".bat"):
                    if (v / n).exists():
                        return str(v / n)
    return None


def axml_package(data: bytes) -> str:
    """Package name from a compiled (binary XML) AndroidManifest.xml. Pure Python, no SDK tools needed."""
    import struct
    try:
        if struct.unpack_from("<H", data, 0)[0] != 0x0003:
            return ""
        pos, strings = 8, []
        while pos + 8 <= len(data):
            ctype, hsize, csize = struct.unpack_from("<HHI", data, pos)
            if ctype == 0x0001:                                    # string pool
                count, _styles, flags, start = struct.unpack_from("<IIII", data, pos + 8)
                offs = struct.unpack_from(f"<{count}I", data, pos + 28)
                utf8 = bool(flags & 0x100)
                for o in offs:
                    q = pos + start + o
                    if utf8:                                      # u8/u16 char count, then u8/u16 byte count
                        for _ in range(2):
                            first = data[q]
                            n, q = (((first & 0x7F) << 8) | data[q + 1], q + 2) if first & 0x80 else (first, q + 1)
                        strings.append(data[q:q + n].decode("utf-8", "replace"))
                    else:                                         # u16 (or u32 if high bit) char count
                        n = struct.unpack_from("<H", data, q)[0]
                        q += 2
                        if n & 0x8000:
                            n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, q)[0]
                            q += 2
                        strings.append(data[q:q + 2 * n].decode("utf-16-le", "replace"))
            elif ctype == 0x0102 and strings:                      # start element
                name, = struct.unpack_from("<i", data, pos + 20)
                attr_start, attr_size, attr_count = struct.unpack_from("<HHH", data, pos + 24)
                if 0 <= name < len(strings) and strings[name] == "manifest":
                    base = pos + 16 + attr_start
                    for i in range(attr_count):
                        _ns, aname, raw = struct.unpack_from("<iii", data, base + i * attr_size)
                        if 0 <= aname < len(strings) and strings[aname] == "package" and 0 <= raw < len(strings):
                            return strings[raw]
                    return ""
            pos += max(csize, 8)
    except (struct.error, IndexError):
        pass
    return ""


async def peek_package(path: Path) -> str:
    """Package name of an artifact before installing it (best effort, '' if unknown)."""
    ext = path.suffix.lower()
    try:
        if ext == ".xapk":
            with zipfile.ZipFile(path) as z:
                return json.loads(z.read("manifest.json").decode("utf-8-sig")).get("package_name", "")
        if ext == ".apk":
            with zipfile.ZipFile(path) as z:
                if name := axml_package(z.read("AndroidManifest.xml")):
                    return name
        if ext == ".apk" and (tool := _build_tool("aapt2")):
            proc = await procs.exec_async(
                tool, "dump", "packagename", str(path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out = (await proc.communicate())[0].decode(errors="replace").strip()
            return out if proc.returncode == 0 and " " not in out else ""
    except Exception:
        pass
    return ""


def find_apksigner() -> str | None:
    if shutil.which("apksigner"):
        return shutil.which("apksigner")
    roots = [config.DATA_DIR / "android-sdk", Path(os.getenv("ANDROID_HOME", "/nonexistent"))]
    for r in roots:
        bt = r / "build-tools"
        if bt.is_dir():
            for v in sorted(bt.iterdir(), reverse=True):
                for n in ("apksigner", "apksigner.bat"):
                    if (v / n).exists():
                        return str(v / n)
    return None


def _zip_checks(path: Path, r: Report):
    if not zipfile.is_zipfile(path):
        r.checks.append(Check("archive", "fail", "not a valid zip/APK file"))
        return
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        bad = z.testzip()
        if bad:
            r.checks.append(Check("archive", "fail", f"corrupt entry {bad}"))
            return
    ext = path.suffix.lower()
    if ext == ".apk":
        if "AndroidManifest.xml" not in names:
            r.checks.append(Check("archive", "fail", "no AndroidManifest.xml"))
        else:
            r.checks.append(Check("archive", "ok", "valid APK structure"))
        v1 = any(n.startswith("META-INF/") and n.endswith((".RSA", ".DSA", ".EC")) for n in names)
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - (4 << 20)))
            v2 = b"APK Sig Block 42" in f.read()
        r.checks.append(Check("signature-present", "ok" if (v1 or v2) else "warn",
                              "signed" if (v1 or v2) else "no signature found; Android will reject it"))
    elif ext == ".xapk":
        ok = "manifest.json" in names or any(n.endswith(".apk") for n in names)
        r.checks.append(Check("archive", "ok" if ok else "fail",
                              "XAPK bundle" if ok else "no manifest.json or .apk inside"))
    elif ext == ".apks":
        ok = any(n.endswith(".apk") for n in names)
        r.checks.append(Check("archive", "ok" if ok else "fail",
                              "APK set" if ok else "no .apk inside"))
    elif ext == ".aab":
        ok = "BundleConfig.pb" in names
        r.checks.append(Check("archive", "ok" if ok else "fail",
                              "app bundle" if ok else "not an app bundle"))


async def _apksigner(path: Path, r: Report):
    tool = find_apksigner()
    if not tool or path.suffix.lower() != ".apk":
        return
    proc = await procs.exec_async(
        tool, "verify", "--print-certs", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out = (await proc.communicate())[0].decode(errors="replace")
    if proc.returncode != 0:
        r.checks.append(Check("apksigner", "fail", out.strip()[:300]))
    else:
        cert = next((l.split(":", 1)[1].strip() for l in out.splitlines()
                     if "certificate SHA-256 digest" in l), "")
        r.checks.append(Check("apksigner", "ok", f"signature verified; cert {cert[:16]}…"))


async def _virustotal(sha256: str, r: Report):
    if not config.VT_API_KEY:
        r.checks.append(Check("malware-scan", "info", "not run (set VT_API_KEY to enable)"))
        return
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            resp = await c.get(f"https://www.virustotal.com/api/v3/files/{sha256}",
                               headers={"x-apikey": config.VT_API_KEY})
        if resp.status_code == 404:
            r.checks.append(Check("malware-scan", "warn", "file unknown to VirusTotal"))
        else:
            resp.raise_for_status()
            st = resp.json()["data"]["attributes"]["last_analysis_stats"]
            bad = st.get("malicious", 0)
            r.checks.append(Check("malware-scan", "fail" if bad else "ok",
                                  f"{bad} engines flag it malicious, {st.get('suspicious', 0)} suspicious"))
    except Exception as e:
        r.checks.append(Check("malware-scan", "warn", f"VirusTotal lookup failed: {e}"))


async def verify(path: Path, expected_sha256: str = "", expected_md5: str = "") -> Report:
    r = Report(size=path.stat().st_size)
    r.sha256, r.md5 = await asyncio.to_thread(hashes, path)
    for name, exp, got in (("sha256", expected_sha256, r.sha256), ("md5", expected_md5, r.md5)):
        if exp:
            same = exp.lower() == got
            r.checks.append(Check(f"{name}-match", "ok" if same else "fail",
                                  "matches marketplace hash" if same else f"expected {exp}, got {got}"))
    await asyncio.to_thread(_zip_checks, path, r)
    await _apksigner(path, r)
    await _virustotal(r.sha256, r)
    return r
