"""A fake scrcpy-server (protocol v4.0, forward tunnel) for testing the client.

Accept order with audio+control enabled: audio socket first (dummy byte, 64-byte device meta,
codec id, packets), then control socket. Parses control messages exactly like ControlMessageReader.
"""
import asyncio
import struct


class FakeScrcpyServer:
    def __init__(self, audio=True, audio_codec=0x00726177, pcm_packets=3):
        self.audio, self.audio_codec, self.pcm_packets = audio, audio_codec, pcm_packets
        self.received: list[tuple] = []
        self.conns = 0
        self.ctl_writer = None
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self._on_conn, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    async def _on_conn(self, r, w):
        self.conns += 1
        first = self.conns == 1
        is_audio = self.audio and first
        if first:
            w.write(b"\x00")                               # dummy byte on the first socket only
            w.write(b"Fake Device".ljust(64, b"\x00"))     # device meta on the first socket only
        if is_audio:
            w.write(struct.pack(">I", self.audio_codec))
            for i in range(self.pcm_packets):
                pcm = bytes([i]) * 8
                w.write(struct.pack(">QI", i, len(pcm)) + pcm)      # media packet: pts=i, no flags
            w.write(struct.pack(">QI", 1 << 62, 2) + b"\x01\x02")   # config packet must be skipped
            await w.drain()
            try:                                                      # keep "playing" so late subscribers hear it
                for i in range(200):
                    await asyncio.sleep(0.05)
                    pcm = b"\x07\x00" * 4
                    w.write(struct.pack(">QI", 100 + i, len(pcm)) + pcm)
                    await w.drain()
            except ConnectionError:
                pass
            return
        self.ctl_writer = w
        await w.drain()
        try:
            while True:
                t = (await r.readexactly(1))[0]
                if t == 2:     # touch: action u8, id i64, x,y i32, w,h u16, pressure u16, buttons i32 x2
                    a, pid, x, y, sw, sh, pr, ab, bt = struct.unpack(">BqiiHHHii", await r.readexactly(31))
                    self.received.append(("touch", a, pid, x, y, sw, sh, pr))
                elif t == 3:
                    x, y, sw, sh, hs, vs, bt = struct.unpack(">iiHHhhi", await r.readexactly(20))
                    self.received.append(("scroll", x, y, sw, sh, vs))
                elif t == 9:
                    seq, paste, n = struct.unpack(">QBI", await r.readexactly(13))
                    self.received.append(("set_clipboard", seq, bool(paste), (await r.readexactly(n)).decode()))
                elif t == 8:
                    self.received.append(("get_clipboard", (await r.readexactly(1))[0]))
                    w.write(b"\x00" + struct.pack(">I", 5) + b"hello")
                    await w.drain()
                elif t == 0:
                    a, kc, rep, meta = struct.unpack(">Biii", await r.readexactly(13))
                    self.received.append(("key", a, kc))
                elif t == 1:
                    (n,) = struct.unpack(">I", await r.readexactly(4))
                    self.received.append(("text", (await r.readexactly(n)).decode()))
                else:
                    raise AssertionError(f"unknown control type {t}")
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    async def push_clipboard(self, text: str):
        raw = text.encode()
        self.ctl_writer.write(b"\x00" + struct.pack(">I", len(raw)) + raw)
        await self.ctl_writer.drain()
