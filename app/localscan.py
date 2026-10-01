"""Scan a folder on this computer for installable Android packages (own implementation;
the idea of pointing the app at a local folder full of APKs came from reviewing other
open-source APK installer tools, not any copied code)."""
from pathlib import Path

from . import config
from .providers import ARCHIVE_EXTS

MAX_RESULTS = 500


class ScanError(ValueError):
    pass


def resolve_under_scan_root(path: str) -> Path:
    p = Path(path).expanduser().resolve() if path else config.SCAN_ROOT
    try:
        p.relative_to(config.SCAN_ROOT)
    except ValueError:
        raise ScanError(f"folder must be under {config.SCAN_ROOT} (set SCAN_ROOT to allow another location)")
    if not p.is_dir():
        raise ScanError("not a folder")
    return p


def scan(path: str = "") -> dict:
    root = resolve_under_scan_root(path)
    found = []
    try:
        for f in root.rglob("*"):
            if f.is_file() and f.suffix.lower() in ARCHIVE_EXTS:
                try:
                    st = f.stat()
                except OSError:
                    continue
                found.append({"path": str(f), "name": f.name, "size": st.st_size, "mtime": st.st_mtime})
                if len(found) >= MAX_RESULTS:
                    break
    except PermissionError:
        pass
    found.sort(key=lambda x: x["mtime"], reverse=True)
    return {"root": str(root), "scan_root": str(config.SCAN_ROOT), "files": found,
            "truncated": len(found) >= MAX_RESULTS}
