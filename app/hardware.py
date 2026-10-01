"""Detect the host and pick emulator/stream settings that suit it.

Target profile: modern laptop, 8+ threads, 16 GB RAM, NVIDIA dGPU (e.g. RTX 5050), SSD.
Override any value with EMU_RAM_MB, EMU_CORES, EMU_GPU, VIDEO_MAX, VIDEO_BITRATE.
"""
import ctypes
import os
import shutil

from . import procs
import sys


def total_ram_mb() -> int:
    try:
        if sys.platform == "win32":
            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                            ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                            ("ev", ctypes.c_ulonglong)]
            m = MS()
            m.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.total // (1 << 20))
        if sys.platform == "darwin":
            return int(procs.check_output(["sysctl", "-n", "hw.memsize"]).strip()) // (1 << 20)
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1 << 20)
    except Exception:
        return 8192


def nvidia_gpu() -> str:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return ""
    try:
        out = procs.run([exe, "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        return out.splitlines()[0] if out else ""
    except Exception:
        return ""


def detect() -> dict:
    return {"ram_mb": total_ram_mb(), "threads": os.cpu_count() or 4, "nvidia": nvidia_gpu()}


def recommend(hw: dict | None = None) -> dict:
    hw = hw or detect()
    ram, threads, gpu = hw["ram_mb"], hw["threads"], hw["nvidia"]
    strong = ram >= 12000 and threads >= 8
    emu_ram = int(os.getenv("EMU_RAM_MB") or (4096 if strong else 3072 if ram >= 8000 else 2048))
    return {
        "emu_ram_mb": emu_ram,                                   # ~25% of a 16 GB machine
        "emu_cores": int(os.getenv("EMU_CORES") or max(2, min(6, threads // 2))),
        "emu_gpu": os.getenv("EMU_GPU") or ("host" if gpu else "swiftshader_indirect"),
        "emu_heap_mb": 512 if strong else 256,
        "emu_data_mb": 16384 if strong else 8192,                # SSD: roomy userdata is cheap
        "video_max": int(os.getenv("VIDEO_MAX") or (1920 if strong else 1280)),
        "video_bitrate": int(os.getenv("VIDEO_BITRATE") or (12_000_000 if strong else 4_000_000)),
        "hw": hw,
    }


def describe() -> str:
    r = recommend()
    hw = r["hw"]
    return (f"{hw['ram_mb'] // 1024} GB RAM, {hw['threads']} threads, GPU: {hw['nvidia'] or 'none detected'} -> "
            f"emulator {r['emu_ram_mb']} MB / {r['emu_cores']} cores / gpu={r['emu_gpu']}, "
            f"video {r['video_max']}px @ {r['video_bitrate'] // 1_000_000} Mbps")
