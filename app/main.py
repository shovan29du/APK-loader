import asyncio
import json
import logging
import os
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import (__version__, adb, config, devices, emulator, history, inputs, installer, plugins, registry,
               safety, settings, updates, video, watcher)
from .jobs import manager
from .providers import ARCHIVE_EXTS, PROVIDERS, check_public_url, safe_filename

log = logging.getLogger("apkloader")
STATIC = config.APP_DIR / "static"
MAX_BYTES = config.MAX_APK_MB * 1024 * 1024
KEEP_DAYS = 7  # stored APK files older than this are purged automatically
state = {"updates": [], "checked": 0, "emulator_msg": ""}


# ---------- automation: emulator, update checks, cleanup ----------

def purge_old_downloads():
    cutoff = time.time() - KEEP_DAYS * 86400
    for d in config.DOWNLOAD_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)


async def ensure_device():
    """If nothing answers on adb, boot the bundled emulator (when installed)."""
    if (await adb.status())["connected"] or not config.AUTO_EMULATOR or not emulator.installed():
        return
    state["emulator_msg"] = "starting emulator…"
    try:
        await asyncio.to_thread(emulator.controller.start)
        await emulator.wait_boot()
        state["emulator_msg"] = ""
    except Exception as e:
        state["emulator_msg"] = f"emulator failed: {e}"


async def watch_loop():
    """Auto-install new APK downloads from the watched folder (opt-in)."""
    w = watcher.Watcher(watcher.watch_dir())
    w.baseline()
    was_on = False
    while True:
        await asyncio.sleep(3)
        on = bool(settings.load().get("watch"))
        if on and not was_on:
            w.baseline()
        was_on = on
        if not on:
            continue
        for f in w.scan():
            d = scratch_dir()
            dest = d / safe_filename(f.name)
            try:
                shutil.copy2(f, dest)
            except OSError:
                continue

            async def work(job, dest=dest, name=f.name):
                job.message = f"Installing {name}…"
                try:
                    job.results.append({"label": name, **await installer.verify_and_install(dest, None, False)})
                except Exception as e:
                    _fail(job, name, e)
            manager.start("install", f"Install downloaded {f.name}", work)


async def background_loop():
    while True:
        try:
            purge_old_downloads()
            history.purge_old_rollbacks()
            if (await adb.status())["connected"]:
                state["updates"] = await updates.app_updates()
                state["checked"] = time.time()
            await updates.self_update(force=True)
        except Exception as e:
            log.warning("background check failed: %s", e)
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(app):
    config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    plugins.load_plugins()
    tasks = [asyncio.create_task(ensure_device()), asyncio.create_task(background_loop()),
             asyncio.create_task(watch_loop())]
    yield
    for t in tasks:
        t.cancel()
    adb.close_channels()
    for c in list(emulator.controllers.values()):
        await asyncio.to_thread(c.stop)


app = FastAPI(title="APK Loader", version=__version__, lifespan=lifespan)


# Browser-origin protection: without a token the API listens on localhost only, so any web page
# could otherwise POST to it (CSRF) or reach it via DNS rebinding.
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"} | {
    h.strip() for h in os.getenv("APKLOADER_ALLOWED_HOSTS", "").split(",") if h.strip()}


def request_allowed(headers, method: str) -> bool:
    host = headers.get("host", "")
    if not config.API_TOKEN and (urlsplit("//" + host).hostname or "") not in ALLOWED_HOSTS:
        return False
    origin = headers.get("origin")
    if origin and method not in ("GET", "HEAD", "OPTIONS") and urlsplit(origin).netloc != host:
        return False
    if origin and method == "WS" and urlsplit(origin).netloc != host:
        return False
    return True


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    if not request_allowed(request.headers, request.method):
        return JSONResponse({"detail": "forbidden origin or host"}, status_code=403)
    try:  # which device this request targets (header from the UI, or ?device= for downloads)
        adb.use_serial(request.headers.get("x-device") or request.query_params.get("device"))
    except adb.AdbError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    return await call_next(request)


def token_ok(supplied: str) -> bool:
    return secrets.compare_digest(supplied.encode(), config.API_TOKEN.encode())


async def auth(request: Request):
    if config.API_TOKEN:
        supplied = request.headers.get("authorization", "")
        if not token_ok(supplied.removeprefix("Bearer ")):
            raise HTTPException(401, "unauthorized")


# ---------- helpers ----------

async def download(url: str, dest: Path, on_progress=None) -> None:
    async with httpx.AsyncClient(follow_redirects=False, timeout=None,
                                 headers={"User-Agent": "apk-loader/1.0"}) as c:
        for _ in range(5):  # manual redirects so each hop is SSRF-checked
            check_public_url(url)
            async with c.stream("GET", url) as r:
                if r.is_redirect:
                    url = str(r.url.join(r.headers["location"]))
                    continue
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                size = 0
                with dest.open("wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError(f"file exceeds {config.MAX_APK_MB} MB")
                        f.write(chunk)
                        if on_progress and total:
                            on_progress(size / total)
                return
        raise ValueError("too many redirects")


def scratch_dir() -> Path:
    d = config.DOWNLOAD_DIR / uuid.uuid4().hex
    d.mkdir(parents=True)
    return d


async def _launch_last(job, run: bool):
    if not run:
        return
    ok = [r for r in job.results if r.get("ok") and r.get("package")]
    if ok:
        try:
            await adb.launch(ok[-1]["package"])
        except adb.AdbError as e:
            ok[-1]["launch_error"] = str(e)


def _fail(job, label, e):
    job.results.append({"label": label, "ok": False, "error": str(e)})


# ---------- models ----------

class Item(BaseModel):
    provider: str = "direct"
    id: str  # package name for marketplaces, URL for "direct"


class BatchReq(BaseModel):
    items: list[Item]
    run_last: bool = False
    allow_unsafe: bool = False


class Pkg(BaseModel):
    package: str


# ---------- status / meta ----------

@app.get("/api/status", dependencies=[Depends(auth)])
async def status():
    s = await adb.status()
    s["emulator"] = {"installed": emulator.installed(), "running": emulator.controller.running(),
                     "message": state["emulator_msg"]}
    s["version"] = __version__
    s["evdev"] = bool(await inputs.touch_info()) if s.get("connected") else False
    return s


@app.get("/api/providers", dependencies=[Depends(auth)])
async def providers():
    return [n for n in PROVIDERS if n != "direct"] + ["direct"]


BROWSE = [("Google Play", "https://play.google.com/store/search?c=apps&q={q}"),
          ("APKMirror", "https://www.apkmirror.com/?post_type=app_release&searchtype=apk&s={q}"),
          ("APKPure", "https://apkpure.com/search?q={q}"),
          ("F-Droid", "https://search.f-droid.org/?q={q}"),
          ("Aptoide", "https://en.aptoide.com/search?query={q}")]


@app.get("/api/browse", dependencies=[Depends(auth)])
async def browse(q: str = ""):
    """Search-page links for stores without a public download API. Download in your browser;
    with the watcher on, the file is installed automatically."""
    from urllib.parse import quote
    return [{"name": n, "url": u.format(q=quote(q))} for n, u in BROWSE]


@app.get("/api/watch", dependencies=[Depends(auth)])
async def watch_get():
    return {"enabled": bool(settings.load().get("watch")), "folder": str(watcher.watch_dir())}


@app.post("/api/watch", dependencies=[Depends(auth)])
async def watch_set(enabled: bool):
    settings.save(watch=enabled)
    return await watch_get()


@app.get("/api/version", dependencies=[Depends(auth)])
async def version():
    return await updates.self_update()


@app.get("/api/search", dependencies=[Depends(auth)])
async def search(q: str, provider: str = "fdroid"):
    p = PROVIDERS.get(provider)
    if not p:
        raise HTTPException(404, "unknown provider")
    async with httpx.AsyncClient(headers={"User-Agent": "apk-loader/1.0"}, timeout=30) as c:
        try:
            return [a.dict() for a in await p.search(c, q)]
        except (httpx.HTTPError, ValueError) as e:
            raise HTTPException(502, f"{provider} error: {e}")


# ---------- jobs ----------

@app.get("/api/jobs", dependencies=[Depends(auth)])
async def jobs():
    return [j.dict() for j in sorted(manager.jobs.values(), key=lambda j: -j.created)]


@app.get("/api/jobs/{job_id}", dependencies=[Depends(auth)])
async def job(job_id: str):
    j = manager.jobs.get(job_id)
    if not j:
        raise HTTPException(404, "no such job")
    return j.dict()


def _install_job(items: list[Item], run_last: bool, allow_unsafe: bool):
    async def work(job):
        n = len(items)
        for i, item in enumerate(items):
            base = i / n
            span = 1 / n
            label = item.id
            try:
                prov = PROVIDERS.get(item.provider)
                if not prov:
                    raise ValueError("unknown provider")
                job.message = f"Resolving {label}…"
                async with httpx.AsyncClient(headers={"User-Agent": "apk-loader/1.0"}, timeout=30) as c:
                    meta = await prov.resolve(c, item.id)
                dest = scratch_dir() / safe_filename((meta.package or item.id.rsplit("/", 1)[-1]) + meta.ext)
                job.message = f"Downloading {label}…"

                def prog(f, base=base, span=span):
                    job.progress = base + span * 0.6 * f
                await download(meta.url, dest, prog)
                job.progress = base + span * 0.7
                job.message = f"Checking and installing {label}…"
                res = await installer.verify_and_install(dest, meta, allow_unsafe, item.provider, item.id, label)
                job.results.append({"label": label, **res})
            except Exception as e:
                _fail(job, label, e)
            job.progress = (i + 1) / n
        await _launch_last(job, run_last)
        job.message = ""
    return work


@app.post("/api/install", dependencies=[Depends(auth)])
async def install_batch(req: BatchReq):
    """Download + install one or many apps in the background. Poll /api/jobs/{id}."""
    if not req.items:
        raise HTTPException(400, "no items")
    j = manager.start("install", f"Install {len(req.items)} app(s)",
                      _install_job(req.items, req.run_last, req.allow_unsafe))
    return {"job": j.id}


@app.post("/api/upload", dependencies=[Depends(auth)])
async def upload(files: list[UploadFile] = File(...), split: bool = Form(False),
                 run: bool = Form(False), allow_unsafe: bool = Form(False)):
    """Install uploaded .apk/.xapk/.apks/.aab files.

    split=false: each file is its own app.  split=true: the .apk files are the
    split APKs of a single app (installed together).
    """
    d = scratch_dir()
    saved: list[Path] = []
    for i, f in enumerate(files):
        name = f.filename or ""
        if not name.lower().endswith(ARCHIVE_EXTS):
            shutil.rmtree(d, ignore_errors=True)
            raise HTTPException(400, f"{name}: only {', '.join(ARCHIVE_EXTS)} files are accepted")
        dest = d / f"{i}_{safe_filename(name)}"
        size = 0
        with dest.open("wb") as out:
            while chunk := await f.read(1 << 16):
                size += len(chunk)
                if size > MAX_BYTES:
                    shutil.rmtree(d, ignore_errors=True)
                    raise HTTPException(413, f"{name} exceeds {config.MAX_APK_MB} MB")
                out.write(chunk)
        saved.append(dest)
    if split and not all(p.suffix.lower() == ".apk" for p in saved):
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, "split mode only takes .apk files")

    async def work(job):
        groups = [saved] if split else [[p] for p in saved]
        for gi, g in enumerate(groups):
            label = ", ".join(p.name.split("_", 1)[1] for p in g)
            job.message = f"Installing {label}…"
            try:
                if len(g) > 1:
                    reports = [await safety.verify(p) for p in g]
                    blocked = [r for r in reports if r.blocked]
                    if blocked and not allow_unsafe:
                        raise adb.AdbError("blocked by safety checks")
                    pkg = await installer.install_artifact(g[0], split_group=g)
                    res = {"ok": True, "package": pkg, "safety": reports[0].dict()}
                else:
                    res = await installer.verify_and_install(g[0], None, allow_unsafe)
                job.results.append({"label": label, **res})
            except Exception as e:
                _fail(job, label, e)
            job.progress = (gi + 1) / len(groups)
        await _launch_last(job, run)
        job.message = ""

    j = manager.start("install", f"Install {len(saved)} file(s)", work)
    return {"job": j.id}


# ---------- installed apps ----------

@app.get("/api/apps", dependencies=[Depends(auth)])
async def installed_apps():
    return sorted(await adb.third_party_packages())


@app.post("/api/launch", dependencies=[Depends(auth)])
async def launch(p: Pkg):
    try:
        await adb.launch(p.package)
    except adb.AdbError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/uninstall", dependencies=[Depends(auth)])
async def uninstall(p: Pkg):
    try:
        await adb.uninstall(p.package)
    except adb.AdbError as e:
        raise HTTPException(400, str(e))
    registry.forget(p.package)
    return {"ok": True}


@app.get("/api/apps/{package}/apk", dependencies=[Depends(auth)])
async def copy_from_device(package: str):
    """Copy an installed app's APK(s) off the device. Split apps come back as a zip."""
    try:
        remote = await adb.apk_paths(package)
        tmp = scratch_dir()
        for i, r in enumerate(remote):
            await adb.pull(r, str(tmp / (f"{i}_" + safe_filename(r.rsplit("/", 1)[-1]))))
    except adb.AdbError as e:
        raise HTTPException(400, str(e))
    files = sorted(tmp.iterdir())
    if len(files) == 1:
        return FileResponse(files[0], filename=f"{package}.apk",
                            background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True))
    archive = shutil.make_archive(str(tmp.parent / (tmp.name + "_bundle")), "zip", tmp)
    return FileResponse(archive, filename=f"{package}.zip", background=BackgroundTask(
        lambda: (shutil.rmtree(tmp, ignore_errors=True), Path(archive).unlink(missing_ok=True))))


# ---------- updates ----------

@app.get("/api/updates", dependencies=[Depends(auth)])
async def get_updates(refresh: bool = False):
    if refresh or not state["checked"]:
        state["updates"] = await updates.app_updates()
        state["checked"] = time.time()
    return {"checked": state["checked"], "updates": state["updates"]}


@app.post("/api/updates/apply", dependencies=[Depends(auth)])
async def apply_updates(packages: list[str] | None = None):
    """Update the given packages (or all with updates) via their original marketplace."""
    todo = [u for u in state["updates"] if packages is None or u["package"] in packages]
    if not todo:
        raise HTTPException(400, "nothing to update")
    items = [Item(provider=u["provider"], id=u["id"]) for u in todo]
    j = manager.start("update", f"Update {len(items)} app(s)", _install_job(items, False, False))
    state["updates"] = [u for u in state["updates"] if u not in todo]
    return {"job": j.id}


# ---------- stored APK library ----------

def _lib_dir(apk_id: str) -> Path:
    d = (config.DOWNLOAD_DIR / apk_id).resolve()
    if d.parent != config.DOWNLOAD_DIR.resolve() or not d.is_dir():
        raise HTTPException(404, "not found")
    return d


@app.get("/api/apks", dependencies=[Depends(auth)])
async def list_apks():
    return [{"id": d.name, "files": [{"name": f.name, "size": f.stat().st_size}
                                     for f in sorted(d.iterdir()) if f.is_file()]}
            for d in sorted(config.DOWNLOAD_DIR.iterdir()) if d.is_dir()]


@app.get("/api/apks/{apk_id}/{name}", dependencies=[Depends(auth)])
async def get_apk(apk_id: str, name: str):
    d = _lib_dir(apk_id)
    f = (d / name).resolve()
    if f.parent != d or not f.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(f, filename=name)


@app.delete("/api/apks/{apk_id}", dependencies=[Depends(auth)])
async def delete_apk(apk_id: str):
    shutil.rmtree(_lib_dir(apk_id))
    return {"ok": True}


# ---------- device controls: screenshot, rotate, files ----------

@app.get("/api/screenshot", dependencies=[Depends(auth)])
async def screenshot():
    try:
        png = await adb.screenshot()
    except adb.AdbError as e:
        raise HTTPException(400, str(e))
    d = scratch_dir()
    f = d / f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"
    f.write_bytes(png)
    return FileResponse(f, filename=f.name, background=BackgroundTask(shutil.rmtree, d, ignore_errors=True))


class Rot(BaseModel):
    rotation: int


@app.post("/api/rotate", dependencies=[Depends(auth)])
async def rotate(r: Rot):
    try:
        await adb.rotate(r.rotation)
    except adb.AdbError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/files", dependencies=[Depends(auth)])
async def files_list(path: str = "/sdcard"):
    try:
        return {"path": adb.safe_remote(path), "items": await adb.list_dir(path)}
    except adb.AdbError as e:
        raise HTTPException(400, str(e))


@app.get("/api/files/download", dependencies=[Depends(auth)])
async def files_download(path: str):
    try:
        remote = adb.safe_remote(path)
        d = scratch_dir()
        local = d / safe_filename(remote.rsplit("/", 1)[-1] or "file")
        await adb.pull(remote, str(local))
    except adb.AdbError as e:
        raise HTTPException(400, str(e))
    return FileResponse(local, filename=local.name,
                        background=BackgroundTask(shutil.rmtree, d, ignore_errors=True))


@app.post("/api/files/upload", dependencies=[Depends(auth)])
async def files_upload(path: str = Form(...), file: UploadFile = File(...)):
    d = scratch_dir()
    try:
        remote = adb.safe_remote(path.rstrip("/") + "/" + (file.filename or "upload"))
        local = d / safe_filename(file.filename or "upload")
        size = 0
        with local.open("wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > MAX_BYTES * 4:
                    raise HTTPException(413, "file too large")
                out.write(chunk)
        await adb.push(str(local), remote)
    except adb.AdbError as e:
        raise HTTPException(400, str(e))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


@app.delete("/api/files", dependencies=[Depends(auth)])
async def files_delete(path: str):
    try:
        await adb.remote_delete(path)
    except adb.AdbError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ---------- app data backups ----------

def _backups() -> Path:
    d = config.DATA_DIR / "backups"
    d.mkdir(exist_ok=True)
    return d


@app.get("/api/backups", dependencies=[Depends(auth)])
async def list_backups():
    return [{"name": f.name, "size": f.stat().st_size, "mtime": f.stat().st_mtime}
            for f in sorted(_backups().glob("*.tar"), reverse=True)]


@app.post("/api/backups", dependencies=[Depends(auth)])
async def create_backup(p: Pkg):
    dest = _backups() / f"{p.package}-{time.strftime('%Y%m%d-%H%M%S')}.tar"
    try:
        await adb.backup_app(p.package, str(dest))
    except adb.AdbError as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, str(e))
    return {"name": dest.name}


def _backup_file(name: str) -> Path:
    f = (_backups() / name).resolve()
    if f.parent != _backups().resolve() or not f.is_file() or f.suffix != ".tar":
        raise HTTPException(404, "not found")
    return f


@app.post("/api/backups/{name}/restore", dependencies=[Depends(auth)])
async def restore_backup(name: str):
    f = _backup_file(name)
    package = f.name.rsplit("-", 2)[0]
    try:
        await adb.restore_app(package, str(f))
    except adb.AdbError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "package": package}


@app.get("/api/backups/{name}", dependencies=[Depends(auth)])
async def download_backup(name: str):
    f = _backup_file(name)
    return FileResponse(f, filename=f.name)


@app.delete("/api/backups/{name}", dependencies=[Depends(auth)])
async def delete_backup(name: str):
    _backup_file(name).unlink()
    return {"ok": True}


# ---------- devices, wireless pairing ----------

class Endpoint(BaseModel):
    host: str
    port: int
    code: str = ""


@app.get("/api/devices", dependencies=[Depends(auth)])
async def get_devices():
    try:
        devs = await devices.list_devices()
    except adb.AdbError:
        devs = []
    ems = [{"name": n, "running": n in emulator.controllers and emulator.controllers[n].running(),
            "serial": emulator.controllers[n].serial if n in emulator.controllers else ""}
           for n in emulator.list_avds()]
    return {"devices": devs, "emulators": ems, "max_emulators": emulator.max_instances(),
            "selected": adb.serial()}


@app.get("/api/devices/mdns", dependencies=[Depends(auth)])
async def get_mdns():
    return await devices.mdns_services()


@app.post("/api/devices/pair", dependencies=[Depends(auth)])
async def pair_device(e: Endpoint):
    try:
        return {"message": await devices.pair(e.host, e.port, e.code)}
    except adb.AdbError as err:
        raise HTTPException(400, str(err))


@app.post("/api/devices/connect", dependencies=[Depends(auth)])
async def connect_device(e: Endpoint):
    try:
        return {"serial": await devices.connect_wifi(e.host, e.port)}
    except adb.AdbError as err:
        raise HTTPException(400, str(err))


@app.post("/api/devices/disconnect", dependencies=[Depends(auth)])
async def disconnect_device():
    dev = adb.serial()
    if ":" not in dev:
        raise HTTPException(400, "only wireless devices can be disconnected")
    await adb._run("disconnect", dev, timeout=15)
    return {"ok": True}


# ---------- emulators (several at once) ----------

class EmuName(BaseModel):
    name: str


def _emu_ctl(name: str):
    try:
        return emulator.get_controller(name)
    except RuntimeError as e:
        raise HTTPException(404, str(e))


@app.post("/api/emulators", dependencies=[Depends(auth)])
async def emulator_create(n: EmuName):
    async def work(job):
        job.message = f"Creating {n.name}…"
        await asyncio.to_thread(emulator.create_avd, n.name)
        job.results.append({"ok": True, "label": n.name})
        job.message = ""
    return {"job": manager.start("emulator", f"Create emulator {n.name}", work, serial=False).id}


@app.post("/api/emulators/{name}/start", dependencies=[Depends(auth)])
async def emulator_start_named(name: str):
    ctl = _emu_ctl(name)
    if not ctl.running() and emulator.running_count() >= emulator.max_instances():
        raise HTTPException(400, f"this machine can run {emulator.max_instances()} emulator(s) at once")

    async def work(job):
        job.message = f"Booting {name}…"
        await asyncio.to_thread(ctl.start)
        await emulator.wait_boot(ctl=ctl)
        job.results.append({"ok": True, "label": name, "serial": ctl.serial})
        job.message = ""
    return {"job": manager.start("emulator", f"Start {name}", work, serial=False).id, "serial": ctl.serial}


@app.post("/api/emulators/{name}/stop", dependencies=[Depends(auth)])
async def emulator_stop_named(name: str):
    await asyncio.to_thread(_emu_ctl(name).stop)
    return {"ok": True}


@app.delete("/api/emulators/{name}", dependencies=[Depends(auth)])
async def emulator_delete(name: str):
    try:
        await asyncio.to_thread(emulator.delete_avd, name)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# legacy single-emulator endpoints (primary instance)
@app.post("/api/emulator/start", dependencies=[Depends(auth)])
async def emulator_start():
    return await emulator_start_named(emulator.AVD_NAME)


@app.post("/api/emulator/stop", dependencies=[Depends(auth)])
async def emulator_stop():
    return await emulator_stop_named(emulator.AVD_NAME)


@app.post("/api/emulator/setup", dependencies=[Depends(auth)])
async def emulator_setup():
    """Download Java, the Android SDK, a system image and create the virtual device."""
    async def work(job):
        loop = asyncio.get_running_loop()

        def log(msg):
            loop.call_soon_threadsafe(setattr, job, "message", msg)
        await asyncio.to_thread(emulator.setup, log)
        job.results.append({"ok": True, "label": "emulator setup"})
        job.message = ""
    return {"job": manager.start("emulator", "Set up Android emulator", work, serial=False).id}


# ---------- snapshots / network / perf / logs ----------

class Snap(BaseModel):
    name: str


@app.get("/api/snapshots", dependencies=[Depends(auth)])
async def snapshots_list():
    try:
        return await devices.snapshots()
    except adb.AdbError as e:
        raise HTTPException(400, str(e))


@app.post("/api/snapshots", dependencies=[Depends(auth)])
async def snapshots_save(s: Snap):
    try:
        await devices.snapshot_save(s.name)
    except adb.AdbError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/snapshots/{name}/load", dependencies=[Depends(auth)])
async def snapshots_load(name: str):
    try:
        await devices.snapshot_load(name)
    except adb.AdbError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/snapshots/{name}", dependencies=[Depends(auth)])
async def snapshots_delete(name: str):
    try:
        await devices.snapshot_delete(name)
    except adb.AdbError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class Net(BaseModel):
    speed: str | None = None
    delay: str | None = None
    proxy: str | None = None
    offline: bool | None = None


@app.get("/api/network", dependencies=[Depends(auth)])
async def network_get():
    return await devices.network_status()


@app.post("/api/network", dependencies=[Depends(auth)])
async def network_set(n: Net):
    try:
        await devices.set_network(n.speed, n.delay, n.proxy, n.offline)
    except adb.AdbError as e:
        raise HTTPException(400, str(e))
    return await devices.network_status()


@app.get("/api/perf", dependencies=[Depends(auth)])
async def perf():
    try:
        return await devices.perf()
    except adb.AdbError as e:
        raise HTTPException(400, str(e))


@app.get("/api/logs/crashes", dependencies=[Depends(auth)])
async def log_crashes():
    try:
        return {"text": await devices.crashes()}
    except adb.AdbError as e:
        raise HTTPException(400, str(e))


@app.websocket("/ws/logs")
async def logs_ws(ws: WebSocket):
    if not request_allowed(ws.headers, "WS") or (
            config.API_TOKEN and not token_ok(ws.query_params.get("token", ""))):
        await ws.close(code=1008)
        return
    await ws.accept()
    proc = None
    try:
        adb.use_serial(ws.query_params.get("device"))
        args = await devices.logcat_args(ws.query_params.get("level", "W"), ws.query_params.get("package") or None)
        proc = await devices.spawn_logcat(args)
        batch: list[str] = []

        async def flush_loop():
            while True:
                await asyncio.sleep(0.15)
                if batch:
                    lines = batch[:]
                    batch.clear()
                    await ws.send_text(json.dumps({"lines": lines}))
        flusher = asyncio.create_task(flush_loop())
        try:
            while line := await proc.stdout.readline():
                batch.append(line.decode(errors="replace").rstrip())
                if len(batch) > 2000:
                    del batch[:1000]
        finally:
            flusher.cancel()
    except adb.AdbError as e:
        await ws.send_text(json.dumps({"error": str(e)}))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if proc and proc.returncode is None:
            proc.kill()


# ---------- install history / retry / rollback ----------

@app.get("/api/history", dependencies=[Depends(auth)])
async def history_list():
    out = []
    for e in history.load():
        d = dict(e)
        d["can_rollback"] = bool(e.get("rollback_dir")) and Path(e["rollback_dir"]).is_dir() and e.get("ok")
        d["can_retry"] = e.get("kind") == "install" and (bool(e.get("provider")) or Path(e.get("file") or "x").is_file())
        out.append(d)
    return out


@app.post("/api/history/{entry_id}/retry", dependencies=[Depends(auth)])
async def history_retry(entry_id: str):
    e = history.get(entry_id)
    if not e or e.get("kind") != "install":
        raise HTTPException(404, "no such install")
    if e.get("provider"):
        return {"job": manager.start("install", f"Retry {e['label']}", _install_job(
            [Item(provider=e["provider"], id=e["app_id"])], False, False)).id}
    f = Path(e.get("file") or "")
    if not f.is_file():
        raise HTTPException(400, "the stored file has been cleaned up; upload it again")

    async def work(job):
        job.message = f"Installing {f.name}…"
        job.results.append({"label": e["label"], **await installer.verify_and_install(f, None, False, label=e["label"])})
        job.message = ""
    return {"job": manager.start("install", f"Retry {e['label']}", work).id}


@app.post("/api/history/{entry_id}/rollback", dependencies=[Depends(auth)])
async def history_rollback(entry_id: str):
    e = history.get(entry_id)
    if not e or not e.get("ok"):
        raise HTTPException(404, "no such install")

    async def work(job):
        job.message = f"Rolling back {e.get('package')}…"
        try:
            pkg = await installer.rollback(e)
            job.results.append({"label": f"rollback {pkg}", "ok": True, "package": pkg})
        except adb.AdbError as err:
            _fail(job, e.get("package", "rollback"), err)
        job.message = ""
    return {"job": manager.start("rollback", f"Roll back {e.get('package')}", work).id}


@app.delete("/api/history/{entry_id}", dependencies=[Depends(auth)])
async def history_delete(entry_id: str):
    history.delete(entry_id)
    return {"ok": True}


# ---------- live screen (H.264 via WebCodecs, PNG fallback) + low-latency input ----------

@app.websocket("/ws/screen")
async def screen(ws: WebSocket):
    if not request_allowed(ws.headers, "WS") or (
            config.API_TOKEN and not token_ok(ws.query_params.get("token", ""))):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        adb.use_serial(ws.query_params.get("device"))
    except adb.AdbError:
        await ws.close(code=1008)
        return
    mode = "png" if ws.query_params.get("mode") == "png" else "h264"
    geo = {"w": 1080, "h": 1920, "gen": 0, "evdev": None}  # current (rotated) size, stream generation

    async def monitor():
        """Keep the display size fresh (apps can rotate it) and decide if raw touch is safe."""
        while True:
            try:
                d = await adb.display_info()
                if d["cur"] != (geo["w"], geo["h"]):
                    geo["w"], geo["h"] = d["cur"]
                    geo["gen"] += 1  # new size -> restart the video stream
                info = await inputs.touch_info()
                geo["evdev"] = info if info and not d["rotated"] and await adb.user_rotation() == 0 else None
            except adb.AdbError:
                pass
            await asyncio.sleep(2)

    try:
        d = await adb.display_info()
        geo["w"], geo["h"] = d["cur"]
    except adb.AdbError:
        pass

    async def pump():
        try:
            await ws.send_text(json.dumps({"mode": mode}))
            if mode == "h264":
                while True:
                    try:
                        gen = geo["gen"]
                        stream = video.h264_stream(geo["w"], geo["h"])
                        async for nal in stream:
                            if nal is None:
                                await ws.send_text(json.dumps({"reset": True}))
                            else:
                                await ws.send_bytes(nal)
                            if geo["gen"] != gen:
                                break
                        await stream.aclose()
                        await ws.send_text(json.dumps({"reset": True}))
                    except adb.AdbError as e:
                        await ws.send_text(json.dumps({"error": str(e)}))
                        await asyncio.sleep(3)
            else:
                while True:
                    try:
                        await ws.send_bytes(await adb.screenshot())
                    except adb.AdbError as e:
                        await ws.send_text(json.dumps({"error": str(e)}))
                        await asyncio.sleep(2)
                    await asyncio.sleep(1 / config.STREAM_FPS)
        except (WebSocketDisconnect, RuntimeError):
            pass

    tasks = [asyncio.create_task(pump()), asyncio.create_task(monitor())]
    touch = {"tid": 0, "down": None, "t0": 0.0}
    try:
        while True:
            try:
                m = json.loads(await ws.receive_text())
                if not isinstance(m, dict):
                    continue
            except ValueError:
                continue
            try:
                t = m.get("type")
                # coordinates are fractions (0..1) of the picture, so any stream size works
                frac = lambda k: min(max(float(m[k]), 0.0), 1.0)  # noqa: E731
                px = lambda k, dim: int(frac(k) * (geo[dim] - 1))  # noqa: E731
                if t == "touch":
                    phase, nx, ny = m["phase"], frac("x"), frac("y")
                    if geo["evdev"]:
                        if phase == "down":
                            touch["tid"] = (touch["tid"] + 1) % 60000
                        await adb.fire(inputs.touch_command(geo["evdev"], phase, nx, ny, touch["tid"]))
                    elif phase == "down":
                        touch["down"], touch["t0"] = (nx, ny), time.time()
                    elif phase == "up" and touch["down"]:
                        (x0, y0), ms = touch["down"], int((time.time() - touch["t0"]) * 1000)
                        touch["down"] = None
                        if abs(nx - x0) * geo["w"] < 12 and abs(ny - y0) * geo["h"] < 12:
                            await adb.tap(int(nx * (geo["w"] - 1)), int(ny * (geo["h"] - 1)))
                        else:
                            await adb.swipe(int(x0 * (geo["w"] - 1)), int(y0 * (geo["h"] - 1)),
                                            int(nx * (geo["w"] - 1)), int(ny * (geo["h"] - 1)), max(ms, 100))
                elif t == "scroll":
                    # mouse wheel: a short swipe in the scroll direction
                    dy = max(-1.0, min(1.0, float(m["dy"])))
                    x, y = px("x", "w"), px("y", "h")
                    y2 = int(min(max(y - dy * geo["h"] * 0.25, 1), geo["h"] - 2))
                    await adb.swipe(x, y, x, y2, 120)
                elif t == "tap":
                    await adb.tap(px("x", "w"), px("y", "h"))
                elif t == "swipe":
                    await adb.swipe(px("x1", "w"), px("y1", "h"), px("x2", "w"), px("y2", "h"),
                                    int(m.get("ms", 200)))
                elif t == "key":
                    await adb.key(m["key"])
                elif t == "text":
                    await adb.text(m["text"])
                elif t == "rotate":
                    await adb.rotate(int(m["rotation"]))
                    inputs.forget()
            except (adb.AdbError, KeyError, ValueError, TypeError):
                pass
    except WebSocketDisconnect:
        pass
    finally:
        for tk in tasks:
            tk.cancel()


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
