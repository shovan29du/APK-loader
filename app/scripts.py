"""Record a session's input as a script and play it back (repeatable UI tests).

A script is JSON: {"version": 1, "name": ..., "steps": [...]}. Input steps carry `t` (seconds since the
recording started) and use fractional screen coordinates, so a script replays on any resolution.
Extra steps for tests: wait, launch, assert_focus, screenshot.
"""
import asyncio
import json
import re
import shutil
import time
import uuid
from pathlib import Path

from . import adb, config
from .remote import RemoteControl

NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ \-]{0,49}$")
KEY_RE = re.compile(r"^[A-Z_0-9]+$|^\d+$")
INPUT_TYPES = {"touch", "scroll", "pinch", "tap", "swipe", "key", "text", "rotate", "clip_set"}
TEST_TYPES = {"wait", "launch", "assert_focus", "screenshot"}
RECORDABLE = INPUT_TYPES
MAX_STEPS = 50000


class ScriptError(ValueError):
    pass


def scripts_dir() -> Path:
    d = config.DATA_DIR / "scripts"
    d.mkdir(exist_ok=True)
    return d


def runs_dir() -> Path:
    d = config.DATA_DIR / "script_runs"
    d.mkdir(exist_ok=True)
    return d


def check_name(name: str) -> str:
    if not NAME_RE.match(name or ""):
        raise ScriptError("script name: letters, digits, space, _ and - (max 50)")
    return name


def _num(step: dict, key: str, lo: float, hi: float) -> float:
    try:
        v = float(step[key])
    except (KeyError, TypeError, ValueError):
        raise ScriptError(f"step needs a numeric '{key}'")
    if not lo <= v <= hi:
        raise ScriptError(f"'{key}' must be between {lo} and {hi}")
    return v


def validate_step(step) -> dict:
    if not isinstance(step, dict) or step.get("type") not in INPUT_TYPES | TEST_TYPES:
        raise ScriptError(f"unknown step type: {step.get('type') if isinstance(step, dict) else step!r}")
    t, out = step["type"], {"type": step["type"]}
    if "t" in step:
        out["t"] = _num(step, "t", 0, 86400)
    if step.get("continue"):
        out["continue"] = True
    if t == "touch":
        if step.get("phase") not in ("down", "move", "up"):
            raise ScriptError("touch needs phase down|move|up")
        out.update(phase=step["phase"], x=_num(step, "x", 0, 1), y=_num(step, "y", 0, 1),
                   id=int(_num(step, "id", 0, 9)) if "id" in step else 0)
    elif t == "scroll":
        out.update(x=_num(step, "x", 0, 1), y=_num(step, "y", 0, 1), dy=_num(step, "dy", -1, 1))
    elif t == "pinch":
        out.update(x=_num(step, "x", 0, 1), y=_num(step, "y", 0, 1), scale=_num(step, "scale", 0.2, 5))
    elif t == "tap":
        out.update(x=_num(step, "x", 0, 1), y=_num(step, "y", 0, 1))
    elif t == "swipe":
        out.update({k: _num(step, k, 0, 1) for k in ("x1", "y1", "x2", "y2")},
                   ms=int(_num(step, "ms", 1, 10000)) if "ms" in step else 200)
    elif t == "key":
        if not KEY_RE.match(str(step.get("key", ""))):
            raise ScriptError("bad key code")
        out["key"] = step["key"]
    elif t in ("text", "clip_set"):
        text = str(step.get("text", ""))
        if not 0 < len(text) <= 300:
            raise ScriptError("text must be 1-300 characters")
        out["text"] = text
        if t == "clip_set":
            out["paste"] = bool(step.get("paste", True))
    elif t == "rotate":
        out["rotation"] = int(_num(step, "rotation", 0, 3))
    elif t == "wait":
        out["seconds"] = _num(step, "seconds", 0, 600)
    elif t in ("launch", "assert_focus"):
        pkg = str(step.get("package", ""))
        if not adb.valid_package(pkg):
            raise ScriptError("invalid package name")
        out["package"] = pkg
        if t == "assert_focus":
            out["timeout"] = _num(step, "timeout", 0, 120) if "timeout" in step else 5.0
    elif t == "screenshot":
        out["name"] = re.sub(r"[^A-Za-z0-9_\-]", "_", str(step.get("name", "screen")))[:40] or "screen"
    return out


def validate(script: dict, name: str | None = None) -> dict:
    if not isinstance(script, dict) or not isinstance(script.get("steps"), list):
        raise ScriptError("a script needs a 'steps' list")
    if len(script["steps"]) > MAX_STEPS:
        raise ScriptError(f"too many steps (max {MAX_STEPS})")
    return {"version": 1, "name": check_name(name or script.get("name", "")),
            "steps": [validate_step(s) for s in script["steps"]]}


def save(script: dict) -> dict:
    clean = validate(script)
    (scripts_dir() / f"{clean['name']}.json").write_text(json.dumps(clean, separators=(",", ":")))
    return clean


def load(name: str) -> dict:
    p = scripts_dir() / f"{check_name(name)}.json"
    if not p.is_file():
        raise ScriptError("no such script")
    return json.loads(p.read_text())


def delete(name: str):
    (scripts_dir() / f"{check_name(name)}.json").unlink(missing_ok=True)


def list_scripts() -> list[dict]:
    out = []
    for p in sorted(scripts_dir().glob("*.json")):
        try:
            d = json.loads(p.read_text())
            steps = d.get("steps", [])
            dur = max((s.get("t", 0) for s in steps), default=0)
            out.append({"name": d.get("name", p.stem), "steps": len(steps), "seconds": round(dur, 1)})
        except ValueError:
            continue
    return out


# ---------------- recording ----------------

class Recorder:
    def __init__(self):
        self.active: dict[str, dict] = {}     # serial -> {name, t0, steps}

    def start(self, serial: str, name: str):
        self.active[serial] = {"name": check_name(name), "t0": time.monotonic(), "steps": []}

    def recording(self, serial: str) -> dict | None:
        r = self.active.get(serial)
        return {"name": r["name"], "steps": len(r["steps"]), "seconds": round(time.monotonic() - r["t0"], 1)} if r else None

    def add(self, serial: str, m: dict):
        r = self.active.get(serial)
        if not r or m.get("type") not in RECORDABLE or len(r["steps"]) >= MAX_STEPS:
            return
        try:
            step = validate_step({**m, "t": round(time.monotonic() - r["t0"], 3)})
        except ScriptError:
            return
        r["steps"].append(step)

    def stop(self, serial: str) -> dict:
        r = self.active.pop(serial, None)
        if not r:
            raise ScriptError("not recording")
        return save({"name": r["name"], "steps": r["steps"]})


recorder = Recorder()


# ---------------- playback ----------------

async def _focused_package() -> str:
    out = await adb._dev("shell", "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -3")
    return out


async def run(job, script: dict, speed: float = 1.0, loops: int = 1):
    """Play `script`; fills job.results with one entry per run. Failed assert/step aborts the loop
    unless the step has "continue": true."""
    speed = max(0.1, min(speed, 10.0))
    loops = max(1, min(loops, 100))
    steps = script["steps"]
    rc = RemoteControl()
    await rc.refresh()
    total = max(1, len(steps) * loops)
    done = 0
    for loop_i in range(loops):
        run_id = uuid.uuid4().hex[:8]
        rdir = runs_dir() / run_id
        rdir.mkdir()
        rec = {"id": run_id, "script": script["name"], "loop": loop_i + 1, "time": time.time(), "ok": True,
               "steps_run": 0, "failures": [], "screenshots": []}
        started = time.monotonic()
        t_prev = 0.0
        for i, step in enumerate(steps):
            job.message = f"{script['name']}: step {i + 1}/{len(steps)}" + (f" (run {loop_i + 1}/{loops})" if loops > 1 else "")
            job.progress = done / total
            done += 1
            if "t" in step:
                await asyncio.sleep(min(max(step["t"] - t_prev, 0), 30) / speed)
                t_prev = step["t"]
            err = None
            try:
                ty = step["type"]
                if ty == "wait":
                    await asyncio.sleep(step["seconds"] / speed)
                elif ty == "launch":
                    await adb.launch(step["package"])
                elif ty == "assert_focus":
                    end = time.monotonic() + step["timeout"]
                    while True:
                        if step["package"] in await _focused_package():
                            break
                        if time.monotonic() >= end:
                            err = f"expected {step['package']} in focus"
                            break
                        await asyncio.sleep(0.5)
                elif ty == "screenshot":
                    png = await adb.screenshot()
                    (rdir / f"{i:04d}_{step['name']}.png").write_bytes(png)
                    rec["screenshots"].append(f"{i:04d}_{step['name']}.png")
                else:
                    await rc.handle(step)
                    if step["type"] in ("rotate",):
                        await rc.refresh()
            except Exception as e:  # noqa: BLE001 - every failure must land in the report
                err = str(e) or type(e).__name__
            rec["steps_run"] += 1
            if err:
                rec["ok"] = False
                rec["failures"].append({"step": i, "type": step["type"], "error": err})
                if not step.get("continue"):
                    break
        rec["seconds"] = round(time.monotonic() - started, 2)
        (rdir / "report.json").write_text(json.dumps(rec))
        job.results.append({"ok": rec["ok"], "label": f"{script['name']} run {loop_i + 1}", "run": run_id,
                            "error": "; ".join(f"step {f['step']} ({f['type']}): {f['error']}" for f in rec["failures"]) or None,
                            "seconds": rec["seconds"]})
        if not rec["ok"] and loops > 1:
            break
    job.message = ""


def list_runs(limit: int = 30) -> list[dict]:
    out = []
    for d in sorted(runs_dir().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            out.append(json.loads((d / "report.json").read_text()))
        except (OSError, ValueError):
            continue
    return out


def run_file(run_id: str, name: str) -> Path | None:
    d = (runs_dir() / run_id).resolve()
    f = (d / name).resolve()
    if d.parent != runs_dir().resolve() or f.parent != d or not f.is_file():
        return None
    return f


def purge_old_runs(days: int = 30):
    cutoff = time.time() - days * 86400
    for d in runs_dir().iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
