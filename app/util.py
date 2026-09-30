import stat
import zipfile
from pathlib import Path


def safe_extract_zip(zf: zipfile.ZipFile, dest: Path, max_total: int, only=None) -> list[Path]:
    """Extract with zip-slip and zip-bomb protection; keeps unix exec bits."""
    dest = dest.resolve()
    infos = [i for i in zf.infolist() if not i.is_dir() and (only is None or only(i.filename))]
    if len(infos) > 5000:
        raise ValueError("archive has too many files")
    if sum(i.file_size for i in infos) > max_total:
        raise ValueError("archive expands beyond the size limit")
    out = []
    for i in infos:
        target = (dest / i.filename).resolve()
        if dest != target and dest not in target.parents:
            raise ValueError(f"unsafe path in archive: {i.filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(i) as s, open(target, "wb") as d:
            while chunk := s.read(1 << 20):
                d.write(chunk)
        mode = i.external_attr >> 16
        if mode & stat.S_IXUSR:
            target.chmod(target.stat().st_mode | 0o111)
        out.append(target)
    return out
