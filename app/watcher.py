"""Opt-in downloads-folder watcher: download an APK from any site in your browser
(Google Play mirrors, APKMirror, APKPure, ...) and it is installed automatically."""
import os
from pathlib import Path

from .providers import ARCHIVE_EXTS

TEMP_EXTS = (".crdownload", ".part", ".tmp", ".download", ".opdownload")


def watch_dir() -> Path:
    return Path(os.getenv("WATCH_DIR") or Path.home() / "Downloads")


class Watcher:
    def __init__(self, folder: Path):
        self.folder = folder
        self.seen: set[Path] = set()
        self.pending: dict[Path, int] = {}

    def _candidates(self):
        try:
            for f in self.folder.iterdir():
                if f.suffix.lower() in ARCHIVE_EXTS and f.is_file() and not f.is_symlink():
                    yield f
        except OSError:
            return

    def baseline(self):
        """Ignore everything already in the folder (only new downloads get installed)."""
        self.seen = set(self._candidates())
        self.pending.clear()

    def scan(self) -> list[Path]:
        """Return files whose size stopped changing since the previous scan."""
        ready = []
        for f in self._candidates():
            if f in self.seen:
                continue
            size = f.stat().st_size
            if size and self.pending.get(f) == size:
                self.seen.add(f)
                self.pending.pop(f, None)
                ready.append(f)
            else:
                self.pending[f] = size
        return ready
