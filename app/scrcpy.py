"""Client for scrcpy-server (protocol of scrcpy 4.0): clipboard sync, audio and multi-touch injection.

The server is Genymobile's unmodified `scrcpy-server` (Apache-2.0), downloaded from the official
release and verified against a pinned SHA-256, pushed to /data/local/tmp and run with `app_process`
(as the `shell` user, like scrcpy itself). Protocol reference: scrcpy doc/develop.md and
ControlMessageReader.java at tag v4.0. The client and server versions must match exactly.
"""
import asyncio
import hashlib
import random
import struct
import urllib.request
from pathlib import Path

from . import adb, config

VERSION = "4.0"
URL = f"https://github.com/Genymobile/scrcpy/releases/download/v{VERSION}/scrcpy-server-v{VERSION}"
SHA256 = "84924bd564a1eb6089c872c7521f968058977f91f5ff02514a8c74aff3210f3a"
REMOTE_JAR = "/data/local/tmp/apkloader-scrcpy-server.jar"
CODEC_RAW = 0x00726177          # "raw": 48 kHz, 16-bit little-endian, stereo PCM
SAMPLE_RATE, CHANNELS = 48000, 2
ACT_DOWN, ACT_UP, ACT_MOVE = 0, 1, 2
POINTER_BASE = 100              # our finger ids 0..9 -> scrcpy pointer ids (keeps clear of the mouse id -1)
CLIPBOARD_MAX = (1 << 18) - 14
TEXT_MAX = 300


class ScrcpyError(RuntimeError):
    pass


def jar_path() -> Path:
    return config.DATA_DIR / "tools" / f"scrcpy-server-v{VERSION}.jar"


def jar_ready() -> bool:
    p = jar_path()
    return p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == SHA256


def fetch_server(log=print) -> Path:
    """Download the pinned server once; refuses anything whose hash differs."""
    p = jar_path()
    if jar_ready():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    log(f"Downloading scrcpy-server v{VERSION}…")
    req = urllib.request.Request(URL, headers={"User-Agent": "apk-loader"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read(32 << 20)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ScrcpyError("scrcpy-server download failed its SHA-256 check; not installed")
    p.write_bytes(data)
    return p


# ---------------- wire format (big endian) ----------------

def _fixed_u16(v: float) -> int:
    return 0xFFFF if v >= 1 else max(0, int(v * 0x10000))


def msg_touch(action: int, pointer_id: int, x: int, y: int, w: int, h: int,
              pressure: float = 1.0, action_button: int = 0, buttons: int = 0) -> bytes:
    return struct.pack(">BBqiiHHHii", 2, action, pointer_id, x, y, w, h, _fixed_u16(pressure),
                       action_button, buttons)


def msg_scroll(x: int, y: int, w: int, h: int, hscroll: float, vscroll: float, buttons: int = 0) -> bytes:
    def fx(v):  # server range is [-16, 16] mapped onto i16
        return int(max(-1.0, min(1.0, v / 16)) * 32767)
    return struct.pack(">BiiHHhhi", 3, x, y, w, h, fx(hscroll), fx(vscroll), buttons)


def msg_set_clipboard(sequence: int, text: str, paste: bool) -> bytes:
    raw = text.encode("utf-8")[:CLIPBOARD_MAX].decode("utf-8", "ignore").encode("utf-8")
    return struct.pack(">BQBI", 9, sequence, 1 if paste else 0, len(raw)) + raw


def msg_get_clipboard(copy_key: int = 0) -> bytes:
    return struct.pack(">BB", 8, copy_key)


def msg_keycode(action: int, keycode: int, repeat: int = 0, meta: int = 0) -> bytes:
    return struct.pack(">BBiii", 0, action, keycode, repeat, meta)


def msg_text(text: str) -> bytes:
    raw = text[:TEXT_MAX].encode("utf-8")
    return struct.pack(">BI", 1, len(raw)) + raw


# ---------------- session ----------------

class Session:
    """One scrcpy-server on one device: control socket (+ optional audio socket)."""

    def __init__(self, serial: str, audio: bool):
        self.serial, self.audio = serial, audio
        self.port = 0
        self.proc: asyncio.subprocess.Process | None = None
        self.ctl_r = self.ctl_w = self.aud_r = self.aud_w = None
        self.control_ok = False
        self.audio_ok = False
        self.seq = 0
        self.tail = b""
        self.clip_listeners: list = []
        self.audio_listeners: list = []
        self.tasks: list[asyncio.Task] = []
        self.write_lock = asyncio.Lock()

    # -- lifecycle
    async def start(self):
        if not jar_ready():
            await asyncio.to_thread(fetch_server, lambda m: None)
        scid = random.randint(1, 0x7FFFFFFF)
        name = f"scrcpy_{scid:08x}"
        await adb._run("-s", self.serial, "push", str(jar_path()), REMOTE_JAR, timeout=120)
        self.port = int((await adb._run("-s", self.serial, "forward", "tcp:0", f"localabstract:{name}")).strip())
        args = (f"CLASSPATH={REMOTE_JAR} app_process / com.genymobile.scrcpy.Server {VERSION} "
                f"scid={scid:08x} log_level=info tunnel_forward=true video=false "
                f"audio={'true' if self.audio else 'false'} audio_codec=raw control=true "
                f"cleanup=false clipboard_autosync=true")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                config.ADB_BIN, "-s", self.serial, "shell", args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except FileNotFoundError:
            raise ScrcpyError(f"adb binary not found: {config.ADB_BIN}")
        self.tasks.append(asyncio.create_task(self._drain_output()))
        try:
            await self.handshake("127.0.0.1", self.port)
        except BaseException:
            await self.stop()
            raise

    async def _drain_output(self):
        try:
            while chunk := await self.proc.stdout.read(1024):
                self.tail = (self.tail + chunk)[-2000:]
        except Exception:
            pass

    async def _open_first(self, host: str, port: int, timeout: float = 20.0):
        """Forward tunnel: connect until the server accepts and sends its dummy byte."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if self.proc and self.proc.returncode is not None:
                break
            try:
                r, w = await asyncio.open_connection(host, port)
                await asyncio.wait_for(r.readexactly(1), 3)      # dummy byte: server accepted us
                return r, w
            except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                await asyncio.sleep(0.3)
        out = self.tail.decode(errors="replace").strip()
        raise ScrcpyError("scrcpy-server did not start" + (f": {out[-300:]}" if out else ""))

    async def handshake(self, host: str, port: int):
        first_r, first_w = await self._open_first(host, port)
        await first_r.readexactly(64)                              # device meta (name), first socket only
        if self.audio:
            self.aud_r, self.aud_w = first_r, first_w
            self.ctl_r, self.ctl_w = await asyncio.open_connection(host, port)
        else:
            self.ctl_r, self.ctl_w = first_r, first_w
        self.control_ok = True
        self.tasks.append(asyncio.create_task(self._read_control()))
        if self.audio:
            self.tasks.append(asyncio.create_task(self._read_audio()))

    async def stop(self):
        self.control_ok = self.audio_ok = False
        for t in self.tasks:
            t.cancel()
        for w in (self.ctl_w, self.aud_w):
            if w:
                w.close()
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
        if self.port:
            try:
                await adb._run("-s", self.serial, "forward", "--remove", f"tcp:{self.port}", timeout=10)
            except adb.AdbError:
                pass
        self.tasks.clear()

    # -- readers
    async def _read_control(self):
        try:
            while True:
                t = (await self.ctl_r.readexactly(1))[0]
                if t == 0:                                          # clipboard text from the device
                    (n,) = struct.unpack(">I", await self.ctl_r.readexactly(4))
                    text = (await self.ctl_r.readexactly(n)).decode("utf-8", "replace")
                    for cb in list(self.clip_listeners):
                        cb(text)
                elif t == 1:                                        # ack for a set_clipboard we sent
                    await self.ctl_r.readexactly(8)
                elif t == 2:                                        # uhid output (unused)
                    _, n = struct.unpack(">HH", await self.ctl_r.readexactly(4))
                    await self.ctl_r.readexactly(n)
                else:
                    break
        except (asyncio.IncompleteReadError, OSError, asyncio.CancelledError):
            pass
        finally:
            self.control_ok = False

    async def _read_audio(self):
        try:
            (codec,) = struct.unpack(">I", await self.aud_r.readexactly(4))
            if codec != CODEC_RAW:                                   # 0 = disabled, 1 = error (e.g. Android < 11)
                return
            self.audio_ok = True
            while True:
                pts_flags, size = struct.unpack(">QI", await self.aud_r.readexactly(12))
                data = await self.aud_r.readexactly(size)
                if pts_flags & (1 << 62):                            # config packet: nothing to decode for PCM
                    continue
                for cb in list(self.audio_listeners):
                    cb(data)
        except (asyncio.IncompleteReadError, OSError, asyncio.CancelledError):
            pass
        finally:
            self.audio_ok = False

    # -- commands
    async def send(self, data: bytes):
        if not self.control_ok:
            raise ScrcpyError("scrcpy control is not connected")
        async with self.write_lock:
            try:
                self.ctl_w.write(data)
                await self.ctl_w.drain()
            except (OSError, ConnectionError):
                self.control_ok = False
                raise ScrcpyError("scrcpy connection lost")

    async def touch(self, action: int, finger: int, nx: float, ny: float, w: int, h: int):
        x, y = int(min(max(nx, 0.0), 1.0) * (w - 1)), int(min(max(ny, 0.0), 1.0) * (h - 1))
        await self.send(msg_touch(action, POINTER_BASE + finger, x, y, w, h, 0.0 if action == ACT_UP else 1.0))

    async def scroll(self, nx: float, ny: float, w: int, h: int, vscroll: float):
        await self.send(msg_scroll(int(nx * (w - 1)), int(ny * (h - 1)), w, h, 0.0, vscroll))

    async def set_clipboard(self, text: str, paste: bool = False):
        self.seq += 1
        await self.send(msg_set_clipboard(self.seq, text, paste))

    async def get_clipboard(self):
        await self.send(msg_get_clipboard())


class Manager:
    """One shared session per device; audio is only captured while somebody listens."""

    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.refs: dict[str, dict[str, int]] = {}
        self.lock = asyncio.Lock()

    def get(self, serial: str) -> Session | None:
        s = self.sessions.get(serial)
        return s if s and s.control_ok else None

    async def acquire(self, serial: str, audio: bool = False) -> Session | None:
        if not config.SCRCPY:
            return None
        async with self.lock:
            refs = self.refs.setdefault(serial, {"control": 0, "audio": 0})
            refs["audio" if audio else "control"] += 1
            return await self._reconcile(serial)

    async def release(self, serial: str, audio: bool = False):
        async with self.lock:
            refs = self.refs.setdefault(serial, {"control": 0, "audio": 0})
            k = "audio" if audio else "control"
            refs[k] = max(0, refs[k] - 1)
            await self._reconcile(serial)

    async def _reconcile(self, serial: str) -> Session | None:
        refs = self.refs[serial]
        need_audio = refs["audio"] > 0
        s = self.sessions.get(serial)
        if refs["control"] + refs["audio"] == 0:
            if s:
                await s.stop()
                self.sessions.pop(serial, None)
            return None
        if s and (not s.control_ok or s.audio != need_audio):
            await s.stop()
            listeners = (s.clip_listeners, s.audio_listeners)
            s = None
        else:
            listeners = None
        if s is None:
            s = Session(serial, need_audio)
            if listeners:
                s.clip_listeners, s.audio_listeners = listeners
            try:
                await s.start()
            except (ScrcpyError, adb.AdbError, OSError, ValueError):
                self.sessions.pop(serial, None)
                return None
            self.sessions[serial] = s
        return s

    async def stop_all(self):
        for s in list(self.sessions.values()):
            await s.stop()
        self.sessions.clear()
        self.refs.clear()


manager = Manager()
