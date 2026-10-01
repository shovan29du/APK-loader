"""Device discovery, wireless pairing, network shaping, snapshots, perf and logs helpers."""
import asyncio
import ipaddress
import re
import socket

from . import adb, config, procs

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")


# ---------- listing / pairing ----------

def parse_devices(text: str) -> list[dict]:
    out = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        out.append({"serial": parts[0], "state": parts[1],
                    "model": info.get("model", "").replace("_", " "),
                    "kind": "emulator" if parts[0].startswith("emulator-") else
                            "wifi" if ":" in parts[0] or "_adb-tls" in parts[0] else "usb"})
    return out


async def list_devices() -> list[dict]:
    return parse_devices(await adb._run("devices", "-l", timeout=20))


def check_lan_endpoint(host: str, port: int) -> str:
    """Only allow private/loopback/link-local targets, so the server can't be used to probe the internet."""
    if not _HOST_RE.match(host) or not (1 <= int(port) <= 65535):
        raise adb.AdbError("invalid host or port")
    try:
        infos = socket.getaddrinfo(host, int(port))
    except socket.gaierror:
        raise adb.AdbError("cannot resolve host")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not (ip.is_private or ip.is_loopback or ip.is_link_local) or ip.is_global:
            raise adb.AdbError("only local-network addresses are allowed")
    return f"{host}:{int(port)}"


async def pair(host: str, port: int, code: str) -> str:
    if not re.fullmatch(r"\d{6}", code):
        raise adb.AdbError("pairing code is 6 digits")
    target = check_lan_endpoint(host, port)
    out = await adb._run("pair", target, code, timeout=30)
    if "Successfully paired" not in out:
        raise adb.AdbError(out.strip() or "pairing failed")
    return out.strip()


async def connect_wifi(host: str, port: int) -> str:
    target = check_lan_endpoint(host, port)
    out = await adb._run("connect", target, timeout=30)
    if "connected to" not in out.lower():
        raise adb.AdbError(out.strip() or "connect failed")
    return target


async def mdns_services() -> list[dict]:
    """Phones advertising wireless debugging / pairing on the LAN."""
    try:
        out = await adb._run("mdns", "services", timeout=15)
    except adb.AdbError:
        return []
    res = []
    for line in out.splitlines():
        m = re.match(r"(\S+)\s+(_adb[\w\-]*\._tcp\S*)\s+(\S+):(\d+)", line)
        if m:
            res.append({"name": m.group(1), "type": m.group(2), "host": m.group(3), "port": int(m.group(4)),
                        "pairing": "pairing" in m.group(2)})
    return res


# ---------- network shaping ----------
SPEEDS = {"gsm", "hscsd", "gprs", "edge", "umts", "hsdpa", "lte", "evdo", "full"}
DELAYS = {"gprs", "edge", "umts", "none"}
_PROXY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,200}:\d{1,5}$")


async def network_status() -> dict:
    st = {"proxy": "", "emulator": adb.serial().startswith("emulator-")}
    try:
        p = (await adb._dev("shell", "settings", "get", "global", "http_proxy")).strip()
        st["proxy"] = "" if p in ("null", ":0") else p
    except adb.AdbError:
        pass
    return st


# ---------- battery / GPS simulation (own implementation; same adb commands used by scrcpy,
# Android Studio's emulator controls, and similar tools) ----------
_BATTERY_RE = re.compile(r"^(AC|USB|level|status):", re.M)


async def battery_status() -> dict:
    out = await adb._dev("shell", "dumpsys battery")
    level = re.search(r"level:\s*(\d+)", out)
    status = re.search(r"status:\s*(\d+)", out)
    scale = re.search(r"scale:\s*(\d+)", out)
    overridden = "(override)" in out.lower() or "UPDATES STOPPED" in out
    return {"level": int(level.group(1)) if level else None,
            "scale": int(scale.group(1)) if scale else 100,
            "status": {1: "unknown", 2: "charging", 3: "discharging", 4: "not charging",
                      5: "full"}.get(int(status.group(1)), "unknown") if status else "unknown",
            "simulated": overridden}


async def battery_set(level: int, plugged: bool = False):
    if not 0 <= level <= 100:
        raise adb.AdbError("battery level must be 0-100")
    await adb._dev("shell", "dumpsys", "battery", "set", "level", str(level))
    await adb._dev("shell", "dumpsys", "battery", "set", "status", "2" if plugged else "3")


async def battery_reset():
    await adb._dev("shell", "dumpsys", "battery", "reset")


_LAT_RE = re.compile(r"^-?\d{1,2}(\.\d+)?$")
_LON_RE = re.compile(r"^-?\d{1,3}(\.\d+)?$")
GPS_PRESETS = {
    "san_francisco": (37.7749, -122.4194), "new_york": (40.7128, -74.0060),
    "london": (51.5074, -0.1278), "tokyo": (35.6762, 139.6503),
    "sydney": (-33.8688, 151.2093), "mumbai": (19.0760, 72.8777),
    "sao_paulo": (-23.5505, -46.6333), "berlin": (52.5200, 13.4050),
}


async def set_location(lat: float, lon: float):
    """Emulator only (adb emu geo fix). Real devices have no equivalent without installing a mock-location app."""
    if not (_LAT_RE.match(str(lat)) and -90 <= float(lat) <= 90):
        raise adb.AdbError("invalid latitude")
    if not (_LON_RE.match(str(lon)) and -180 <= float(lon) <= 180):
        raise adb.AdbError("invalid longitude")
    await adb.emu("geo", "fix", f"{lon:.6f}", f"{lat:.6f}")


async def set_network(speed: str | None = None, delay: str | None = None,
                      proxy: str | None = None, offline: bool | None = None):
    if speed is not None:
        if not (speed in SPEEDS or re.fullmatch(r"\d{1,6}(:\d{1,6})?", speed)):
            raise adb.AdbError("bad speed")
        await adb.emu("network", "speed", speed)
    if delay is not None:
        if not (delay in DELAYS or re.fullmatch(r"\d{1,5}(:\d{1,5})?", delay)):
            raise adb.AdbError("bad delay")
        await adb.emu("network", "delay", delay)
    if proxy is not None:
        if proxy == "":
            await adb._dev("shell", "settings", "put", "global", "http_proxy", ":0")
        elif _PROXY_RE.match(proxy):
            await adb._dev("shell", "settings", "put", "global", "http_proxy", proxy)
        else:
            raise adb.AdbError("proxy must look like host:port")
    if offline is not None:
        verb = "disable" if offline else "enable"
        await adb._dev("shell", f"svc wifi {verb}; svc data {verb}")


# ---------- emulator snapshots ----------
_SNAP_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def _snap_name(name: str) -> str:
    if not _SNAP_RE.match(name):
        raise adb.AdbError("snapshot name: letters, digits, _ and - (max 40)")
    return name


def parse_snapshots(text: str) -> list[dict]:
    snaps = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] not in ("ID", "List", "OK") and not set(parts[0]) <= {"-"}:
            snaps.append({"name": parts[1], "size": parts[2] + (" " + parts[3] if len(parts) > 3 and parts[3].isalpha() else "")})
        elif len(parts) >= 2 and parts[0] == "--":
            snaps.append({"name": parts[1], "size": ""})
    return snaps


async def snapshots() -> list[dict]:
    return parse_snapshots(await adb.emu("avd", "snapshot", "list"))


async def snapshot_save(name: str):
    await adb.emu("avd", "snapshot", "save", _snap_name(name))


async def snapshot_load(name: str):
    await adb.emu("avd", "snapshot", "load", _snap_name(name))
    adb.close_channels()  # shells died with the old state


async def snapshot_delete(name: str):
    await adb.emu("avd", "snapshot", "delete", _snap_name(name))


# ---------- performance ----------
_prev_cpu: dict[str, tuple[int, int]] = {}
_PERF_CMD = ("head -1 /proc/stat; echo ---; grep -E 'MemTotal|MemAvailable' /proc/meminfo; "
             "echo ---; ps -A -o RSS,NAME | sort -rn | head -9")


def parse_perf(text: str, prev: tuple[int, int] | None) -> tuple[dict, tuple[int, int]]:
    cpu_s, mem_s, ps_s = (text.split("---") + ["", "", ""])[:3]
    nums = [int(x) for x in cpu_s.split()[1:] if x.isdigit()]
    total, idle = sum(nums), (nums[3] + (nums[4] if len(nums) > 4 else 0)) if len(nums) > 3 else 0
    pct = None
    if prev and total > prev[0]:
        pct = round(100 * (1 - (idle - prev[1]) / (total - prev[0])), 1)
    mem = {k: int(v) for k, v in re.findall(r"(MemTotal|MemAvailable):\s+(\d+)", mem_s)}
    top = []
    for line in ps_s.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            top.append({"rss_kb": int(parts[0]), "name": parts[1].strip()})
    return ({"cpu_pct": pct, "mem_total_kb": mem.get("MemTotal"), "mem_avail_kb": mem.get("MemAvailable"),
             "top": top}, (total, idle))


async def perf() -> dict:
    out = await adb._dev("shell", _PERF_CMD, timeout=15)
    dev = adb.serial()
    res, cur = parse_perf(out, _prev_cpu.get(dev))
    _prev_cpu[dev] = cur
    return res


# ---------- logs ----------

async def logcat_args(level: str, package: str | None) -> list[str]:
    if level not in tuple("VDIWEF"):
        raise adb.AdbError("bad log level")
    args = ["logcat", "-v", "threadtime"]
    if package:
        if not adb.valid_package(package):
            raise adb.AdbError("invalid package name")
        pid = (await adb._dev("shell", "pidof", "-s", package)).strip()
        if pid.isdigit():
            args.append(f"--pid={pid}")
    args.append(f"*:{level}")
    return args


async def crashes() -> str:
    return await adb._dev("logcat", "-b", "crash", "-d", "-t", "300", timeout=30)


async def spawn_logcat(args: list[str]):
    try:
        return await procs.exec_async(
            config.ADB_BIN, "-s", adb.serial(), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except FileNotFoundError:
        raise adb.AdbError(f"adb binary not found: {config.ADB_BIN}")
