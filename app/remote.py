"""Turns UI events (touch, scroll, pinch, keys, text, clipboard) into device input.

Engines, best first: scrcpy control socket (rotation-aware, multi-touch, unicode clipboard) ->
raw touchscreen events (fast, multi-touch, portrait only) -> `input` commands (single finger).
The same class drives live sessions (WebSocket) and scripted playback.
"""
import asyncio
import time

from . import adb, inputs, scrcpy


class RemoteControl:
    def __init__(self):
        self.w, self.h = 1080, 1920        # current (rotated) display size
        self.rotated = False
        self.evdev: dict | None = None
        self.engine = "auto"               # auto | adb  (adb = skip scrcpy injection)
        self.gen = 0                       # bumps when the display size changes
        self._tid = 0
        self._tids: dict[int, int] = {}    # finger -> evdev tracking id
        self._down: dict[int, tuple] = {}  # finger -> (x, y, t) for the `input` fallback
        self._serial = adb.serial()

    # -- state
    async def refresh(self):
        try:
            d = await adb.display_info()
            if d["cur"] != (self.w, self.h):
                self.w, self.h = d["cur"]
                self.gen += 1
            self.rotated = d["rotated"]
            info = await inputs.touch_info()
            self.evdev = info if info and not d["rotated"] and await adb.user_rotation() == 0 else None
        except adb.AdbError:
            pass

    def scrcpy(self):
        return None if self.engine == "adb" else scrcpy.manager.get(self._serial)

    @property
    def multitouch(self) -> bool:
        return bool(self.scrcpy() or self.evdev)

    # -- touch
    async def touch(self, phase: str, nx: float, ny: float, finger: int = 0):
        if not 0 <= finger <= 9:
            return
        s = self.scrcpy()
        if s:
            await s.touch({"down": scrcpy.ACT_DOWN, "move": scrcpy.ACT_MOVE, "up": scrcpy.ACT_UP}[phase],
                          finger, nx, ny, self.w, self.h)
            return
        if self.evdev:
            if phase == "down":
                self._tid = (self._tid + 1) % 60000
                self._tids[finger] = self._tid
            others = any(f != finger for f in self._tids)
            cmd = inputs.touch_command(self.evdev, phase, nx, ny, self._tids.get(finger, self._tid),
                                       slot=finger, first=phase == "down" and not others,
                                       last=phase == "up" and not others)
            if phase == "up":
                self._tids.pop(finger, None)
            await adb.fire(cmd)
            return
        if finger != 0:
            raise adb.AdbError("multi-touch needs the scrcpy helper or fast input (portrait)")
        await self._fallback(phase, nx, ny)

    async def _fallback(self, phase: str, nx: float, ny: float):
        if phase == "down":
            self._down[0] = (nx, ny, time.time())
        elif phase == "up" and 0 in self._down:
            x0, y0, t0 = self._down.pop(0)
            ms = int((time.time() - t0) * 1000)
            if abs(nx - x0) * self.w < 12 and abs(ny - y0) * self.h < 12:
                await adb.tap(int(nx * (self.w - 1)), int(ny * (self.h - 1)))
            else:
                await adb.swipe(int(x0 * (self.w - 1)), int(y0 * (self.h - 1)),
                                int(nx * (self.w - 1)), int(ny * (self.h - 1)), max(ms, 100))

    async def scroll(self, nx: float, ny: float, dy: float):
        dy = max(-1.0, min(1.0, dy))
        s = self.scrcpy()
        if s:
            await s.scroll(nx, ny, self.w, self.h, -dy * 3)      # wheel down (dy>0) scrolls content up
            return
        x, y = int(nx * (self.w - 1)), int(ny * (self.h - 1))
        y2 = int(min(max(y - dy * self.h * 0.25, 1), self.h - 2))
        await adb.swipe(x, y, x, y2, 120)

    async def pinch(self, nx: float, ny: float, scale: float):
        """Two-finger pinch around (nx, ny): scale > 1 zooms in (fingers spread), < 1 zooms out."""
        if not self.multitouch:
            raise adb.AdbError("pinch needs the scrcpy helper or fast input (portrait)")
        scale = max(0.2, min(5.0, scale))
        # spread (zoom in) starts close together; pinch (zoom out) starts far apart
        r0, r1 = (0.05, min(0.05 * scale, 0.4)) if scale >= 1 else (0.2, max(0.2 * scale, 0.02))
        pos = lambda r, sign: (min(max(nx + sign * r, 0.0), 1.0), min(max(ny, 0.0), 1.0))  # noqa: E731
        steps = 8
        await self.touch("down", *pos(r0, -1), finger=0)
        await self.touch("down", *pos(r0, +1), finger=1)
        for i in range(1, steps + 1):
            r = r0 + (r1 - r0) * i / steps
            await self.touch("move", *pos(r, -1), finger=0)
            await self.touch("move", *pos(r, +1), finger=1)
            await asyncio.sleep(0.016)
        await self.touch("up", *pos(r1, +1), finger=1)
        await self.touch("up", *pos(r1, -1), finger=0)

    # -- everything else
    async def handle(self, m: dict) -> str | None:
        """Apply one UI/script event. Returns a warning string when the event can't be honoured."""
        t = m.get("type")
        frac = lambda k: min(max(float(m[k]), 0.0), 1.0)  # noqa: E731
        if t == "touch":
            await self.touch(m["phase"], frac("x"), frac("y"), int(m.get("id", 0)))
        elif t == "scroll":
            await self.scroll(frac("x"), frac("y"), float(m["dy"]))
        elif t == "pinch":
            await self.pinch(frac("x"), frac("y"), float(m["scale"]))
        elif t == "tap":
            await adb.tap(int(frac("x") * (self.w - 1)), int(frac("y") * (self.h - 1)))
        elif t == "swipe":
            await adb.swipe(int(frac("x1") * (self.w - 1)), int(frac("y1") * (self.h - 1)),
                            int(frac("x2") * (self.w - 1)), int(frac("y2") * (self.h - 1)),
                            int(m.get("ms", 200)))
        elif t == "key":
            await adb.key(m["key"])
        elif t == "text":
            await adb.text(m["text"])
        elif t == "rotate":
            await adb.rotate(int(m["rotation"]))
            inputs.forget()
        elif t == "clip_set":
            s = self.scrcpy()
            if not s:                                     # no helper: type ASCII instead
                await adb.text(str(m["text"]))
                return "clipboard sync needs the scrcpy helper; typed the text instead"
            await s.set_clipboard(str(m["text"]), bool(m.get("paste", False)))
        elif t == "clip_get":
            s = self.scrcpy()
            if s:
                await s.get_clipboard()
        return None
