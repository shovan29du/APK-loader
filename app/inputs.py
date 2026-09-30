"""Low-latency touch injection straight into the device's touchscreen (evdev `sendevent`).

Real press / drag / release streaming, no JVM start per gesture. Used only when it is safe
(touch node found + writable, display not rotated); otherwise callers fall back to `input`.
"""
import re

from . import adb

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2F, 0x35, 0x36, 0x39
BTN_TOUCH = 0x14A


def parse_getevent(text: str) -> dict | None:
    """Pick the touchscreen from `getevent -lp`: {dev, maxx, maxy}. Needs multitouch type B (slots)."""
    best = None
    for block in re.split(r"(?m)^add device \d+:\s*", text)[1:]:
        dev = block.split("\n", 1)[0].strip()
        mx = re.search(r"ABS_MT_POSITION_X\s*:.*?max (\d+)", block)
        my = re.search(r"ABS_MT_POSITION_Y\s*:.*?max (\d+)", block)
        if not (dev.startswith("/dev/input/") and mx and my and "ABS_MT_SLOT" in block):
            continue
        name = re.search(r'name:\s*"([^"]*)"', block)
        info = {"dev": dev, "maxx": int(mx.group(1)), "maxy": int(my.group(1)),
                "direct": "INPUT_PROP_DIRECT" in block,
                "touchy": bool(name and re.search(r"touch|multi", name.group(1), re.I))}
        if best is None or (info["direct"], info["touchy"]) > (best["direct"], best["touchy"]):
            best = info
    return {k: best[k] for k in ("dev", "maxx", "maxy")} if best else None


def _ev(dev: str, t: int, c: int, v: int) -> str:
    return f"sendevent {dev} {t} {c} {v}"


def touch_command(info: dict, phase: str, nx: float, ny: float, tid: int,
                  slot: int = 0, first: bool = True, last: bool = True) -> str:
    """Shell line for one touch phase (down|move|up) of finger `slot` at fractional (nx, ny).
    `first`/`last`: this is the first finger down / the last finger up (BTN_TOUCH toggles then)."""
    d = info["dev"]
    x = int(min(max(nx, 0.0), 1.0) * info["maxx"])
    y = int(min(max(ny, 0.0), 1.0) * info["maxy"])
    ev = [_ev(d, EV_ABS, ABS_MT_SLOT, slot)]
    if phase == "down":
        ev += [_ev(d, EV_ABS, ABS_MT_TRACKING_ID, tid), _ev(d, EV_ABS, ABS_MT_POSITION_X, x),
               _ev(d, EV_ABS, ABS_MT_POSITION_Y, y)]
        if first:
            ev.append(_ev(d, EV_KEY, BTN_TOUCH, 1))
    elif phase == "move":
        ev += [_ev(d, EV_ABS, ABS_MT_POSITION_X, x), _ev(d, EV_ABS, ABS_MT_POSITION_Y, y)]
    elif phase == "up":
        ev.append(_ev(d, EV_ABS, ABS_MT_TRACKING_ID, -1))
        if last:
            ev.append(_ev(d, EV_KEY, BTN_TOUCH, 0))
    else:
        raise adb.AdbError("bad touch phase")
    ev.append(_ev(d, EV_SYN, 0, 0))
    return "; ".join(ev)


_info: dict[str, dict | None] = {}


async def touch_info() -> dict | None:
    """Probe once per device: find the touchscreen and confirm we may write to it."""
    dev = adb.serial()
    if dev in _info:
        return _info[dev]
    info = None
    try:
        info = parse_getevent(await adb._dev("shell", "getevent", "-lp", timeout=20))
        if info:
            probe = await adb._dev("shell", f"{_ev(info['dev'], EV_SYN, 0, 0)} && echo OK", timeout=10)
            if "OK" not in probe:
                info = None
    except adb.AdbError:
        info = None
    _info[dev] = info
    return info


def forget(dev: str | None = None):
    _info.pop(dev or adb.serial(), None)
