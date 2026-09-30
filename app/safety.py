"""Pre-install checks: hashes, archive sanity, signatures, optional VirusTotal."""
import asyncio
import hashlib
import os
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import config


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
    proc = await asyncio.create_subprocess_exec(
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
