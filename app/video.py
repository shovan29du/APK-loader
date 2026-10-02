"""H.264 screen streaming from `adb exec-out screenrecord` (no device-side agent needed)."""
import asyncio
import time

from . import adb, config, procs

TIME_LIMIT = 180         # screenrecord's own hard limit, in seconds
PRESTART_MARGIN = 12     # start the replacement this many seconds before the limit hits


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


def should_prestart(elapsed: float, time_limit: float = TIME_LIMIT, margin: float = PRESTART_MARGIN) -> bool:
    """True once a session is old enough that its replacement should already be starting."""
    return elapsed >= max(time_limit - margin, 0)


async def _spawn(w: int, h: int):
    try:
        return await procs.exec_async(
            config.ADB_BIN, "-s", adb.serial(), "exec-out", "screenrecord",
            "--output-format=h264", f"--size={w}x{h}", f"--bit-rate={config.VIDEO_BITRATE}",
            "--time-limit", str(TIME_LIMIT), "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise adb.AdbError(f"adb binary not found: {config.ADB_BIN}")


async def _stop(proc):
    if proc.returncode is None:
        proc.kill()
    err = (await proc.stderr.read()).decode(errors="replace").strip() if proc.stderr else ""
    await proc.wait()
    return err


async def h264_stream(width: int, height: int):
    """Yield NAL units; yields None whenever the encoding session changes (new SPS/PPS, so the
    decoder must reset). screenrecord has a hard 3-minute limit, so the replacement process is
    started a few seconds early and handed over to with no gap, instead of stopping, waiting, then
    starting a new one (which used to freeze the picture for up to a second every 3 minutes)."""
    w, h = video_size(width, height)
    fails = 0
    proc = await _spawn(w, h)
    started = time.monotonic()
    next_proc = next_started = None
    try:
        while True:
            splitter, got = NalSplitter(), False
            try:
                while True:
                    elapsed = time.monotonic() - started
                    if next_proc is None and should_prestart(elapsed):
                        next_proc, next_started = await _spawn(w, h), time.monotonic()
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
                err = await _stop(proc)
            fails = 0 if got else fails + 1
            if next_proc is not None:
                proc, started, next_proc, next_started = next_proc, next_started, None, None
            elif fails >= 3:
                raise adb.AdbError(err or "screenrecord produced no video")
            else:
                await asyncio.sleep(0.2)
                proc = await _spawn(w, h)
                started = time.monotonic()
            yield None
    finally:
        if next_proc is not None:
            await _stop(next_proc)
