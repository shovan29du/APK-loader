"""H.264 screen streaming from `adb exec-out screenrecord` (no device-side agent needed)."""
import asyncio

from . import adb, config, procs


class NalSplitter:
    """Splits an Annex-B byte stream into NAL units (each keeps its start code)."""

    def __init__(self):
        self.buf = bytearray()

    def _starts(self) -> list[int]:
        out, i, b = [], 0, self.buf
        while (i := b.find(b"\x00\x00\x01", i)) != -1:
            out.append(i - 1 if i > 0 and b[i - 1] == 0 else i)
            i += 3
        return out

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        starts = self._starts()
        if len(starts) < 2:
            return []
        units = [bytes(self.buf[a:b]) for a, b in zip(starts, starts[1:])]
        del self.buf[:starts[-1]]
        return units

    def flush(self) -> list[bytes]:
        """Emit the pending tail (its next start code may never arrive on a still screen)."""
        starts = self._starts()
        if len(starts) == 1 and len(self.buf) > starts[0] + 4:
            unit = bytes(self.buf[starts[0]:])
            self.buf.clear()
            return [unit]
        return []


def video_size(w: int, h: int) -> tuple[int, int]:
    scale = min(1.0, config.VIDEO_MAX / max(w, h))
    return max(16, int(w * scale) // 8 * 8), max(16, int(h * scale) // 8 * 8)


async def h264_stream(width: int, height: int):
    """Yield NAL units; yields None each time screenrecord's 3-minute limit forces a restart."""
    w, h = video_size(width, height)
    fails = 0
    while True:
        try:
            proc = await procs.exec_async(
                config.ADB_BIN, "-s", adb.serial(), "exec-out", "screenrecord",
                "--output-format=h264", f"--size={w}x{h}", f"--bit-rate={config.VIDEO_BITRATE}",
                "--time-limit", "180", "-",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            raise adb.AdbError(f"adb binary not found: {config.ADB_BIN}")
        splitter, got = NalSplitter(), False
        try:
            while True:
                try:
                    data = await asyncio.wait_for(proc.stdout.read(1 << 16), 0.04)
                except asyncio.TimeoutError:
                    for u in splitter.flush():
                        yield u
                    continue
                if not data:
                    break
                got = True
                for u in splitter.feed(data):
                    yield u
        finally:
            if proc.returncode is None:
                proc.kill()
            err = (await proc.stderr.read()).decode(errors="replace").strip() if proc.stderr else ""
            await proc.wait()
        fails = 0 if got else fails + 1
        if fails >= 3:
            raise adb.AdbError(err or "screenrecord produced no video")
        yield None
        await asyncio.sleep(0.2)
