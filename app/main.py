import asyncio
import json
import shutil
import uuid
from pathlib import Path

import httpx
from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import adb, config
from .providers import PROVIDERS, check_public_url, safe_filename

app = FastAPI(title="APK Loader")
config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_BYTES = config.MAX_APK_MB * 1024 * 1024


async def auth(request: Request):
    if config.API_TOKEN and request.headers.get("authorization") != f"Bearer {config.API_TOKEN}":
        raise HTTPException(401, "unauthorized")


# ---------- helpers ----------

async def download(url: str, dest: Path) -> None:
    async with httpx.AsyncClient(follow_redirects=False, timeout=None,
                                 headers={"User-Agent": "apk-loader/1.0"}) as c:
        for _ in range(5):  # manual redirects so each hop is SSRF-checked
            check_public_url(url)
            async with c.stream("GET", url) as r:
                if r.is_redirect:
                    url = str(r.url.join(r.headers["location"]))
                    continue
                r.raise_for_status()
                size = 0
                with dest.open("wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError(f"file exceeds {config.MAX_APK_MB} MB")
                        f.write(chunk)
                return
        raise ValueError("too many redirects")


async def install_files(paths: list[Path], split: bool, hint: str = "") -> dict:
    before = await adb.third_party_packages()
    if split and len(paths) > 1:
        await adb.install_multiple([str(p) for p in paths])
    else:
        for p in paths:
            await adb.install(str(p))
    new = sorted((await adb.third_party_packages()) - before)
    package = new[0] if new else hint
    return {"installed": True, "package": package}


def scratch_dir() -> Path:
    d = config.DOWNLOAD_DIR / uuid.uuid4().hex
    d.mkdir()
    return d


# ---------- API ----------

class Item(BaseModel):
    provider: str = "direct"
    id: str  # package name for marketplaces, URL for "direct"


class BatchReq(BaseModel):
    items: list[Item]
    run_last: bool = False


class Pkg(BaseModel):
    package: str


@app.get("/api/status", dependencies=[Depends(auth)])
async def status():
    return await adb.status()


@app.get("/api/providers", dependencies=[Depends(auth)])
async def providers():
    return [n for n in PROVIDERS if n != "direct"] + ["direct"]


@app.get("/api/search", dependencies=[Depends(auth)])
async def search(q: str, provider: str = "fdroid"):
    p = PROVIDERS.get(provider)
    if not p:
        raise HTTPException(404, "unknown provider")
    async with httpx.AsyncClient(headers={"User-Agent": "apk-loader/1.0"}, timeout=30) as c:
        try:
            return [a.dict() for a in await p.search(c, q)]
        except httpx.HTTPError as e:
            raise HTTPException(502, f"{provider} error: {e}")


async def _install_item(item: Item) -> dict:
    p = PROVIDERS.get(item.provider)
    if not p:
        raise ValueError("unknown provider")
    async with httpx.AsyncClient(headers={"User-Agent": "apk-loader/1.0"}, timeout=30) as c:
        url, hint = await p.resolve(c, item.id)
    d = scratch_dir()
    dest = d / safe_filename((hint or item.id.rsplit("/", 1)[-1]) + ".apk")
    await download(url, dest)
    return await install_files([dest], split=False, hint=hint)


@app.post("/api/install", dependencies=[Depends(auth)])
async def install_batch(req: BatchReq):
    """Download + install one or many apps. Each item reports its own result."""
    if not req.items:
        raise HTTPException(400, "no items")
    results = []
    for item in req.items:
        try:
            res = await _install_item(item)
            results.append({"id": item.id, "ok": True, **res})
        except Exception as e:  # keep going: one failure must not block the rest
            results.append({"id": item.id, "ok": False, "error": str(e)})
    if req.run_last:
        ok = [r for r in results if r["ok"] and r.get("package")]
        if ok:
            try:
                await adb.launch(ok[-1]["package"])
            except adb.AdbError as e:
                ok[-1]["launch_error"] = str(e)
    return {"results": results}


@app.post("/api/upload", dependencies=[Depends(auth)])
async def upload(files: list[UploadFile] = File(...), split: bool = Form(False),
                 run: bool = Form(False)):
    """Install one or many uploaded APKs.

    split=false: each file is a separate app.  split=true: all files are the
    split APKs of a single app (installed together with install-multiple).
    """
    d = scratch_dir()
    saved: list[Path] = []
    for i, f in enumerate(files):
        if not (f.filename or "").lower().endswith(".apk"):
            raise HTTPException(400, f"{f.filename}: only .apk files are accepted")
        dest = d / f"{i}_{safe_filename(f.filename)}"
        size = 0
        with dest.open("wb") as out:
            while chunk := await f.read(1 << 16):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(413, f"{f.filename} exceeds {config.MAX_APK_MB} MB")
                out.write(chunk)
        saved.append(dest)

    results = []
    groups = [saved] if split else [[p] for p in saved]
    for g in groups:
        label = ", ".join(p.name.split("_", 1)[1] for p in g)
        try:
            results.append({"file": label, "ok": True, **await install_files(g, split)})
        except adb.AdbError as e:
            results.append({"file": label, "ok": False, "error": str(e)})
    if run:
        ok = [r for r in results if r["ok"] and r.get("package")]
        if ok:
            try:
                await adb.launch(ok[-1]["package"])
            except adb.AdbError as e:
                ok[-1]["launch_error"] = str(e)
    return {"results": results}


@app.get("/api/apps", dependencies=[Depends(auth)])
async def installed_apps():
    return sorted(await adb.third_party_packages())


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
    cleanup = BackgroundTask(shutil.rmtree, tmp, ignore_errors=True)
    if len(files) == 1:
        return FileResponse(files[0], filename=f"{package}.apk", background=cleanup)
    archive = shutil.make_archive(str(tmp / "bundle"), "zip", tmp)
    return FileResponse(archive, filename=f"{package}.zip", background=cleanup)


# ---------- stored APK library (downloaded / uploaded files) ----------

def _lib_dir(apk_id: str) -> Path:
    d = (config.DOWNLOAD_DIR / apk_id).resolve()
    if d.parent != config.DOWNLOAD_DIR or not d.is_dir():
        raise HTTPException(404, "not found")
    return d


@app.get("/api/apks", dependencies=[Depends(auth)])
async def list_apks():
    return [{"id": d.name, "files": [{"name": f.name, "size": f.stat().st_size}
                                     for f in sorted(d.iterdir())]}
            for d in sorted(config.DOWNLOAD_DIR.iterdir()) if d.is_dir()]


@app.get("/api/apks/{apk_id}/{name}", dependencies=[Depends(auth)])
async def get_apk(apk_id: str, name: str):
    f = (_lib_dir(apk_id) / name).resolve()
    if f.parent != _lib_dir(apk_id) or not f.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(f, filename=name)


@app.delete("/api/apks/{apk_id}", dependencies=[Depends(auth)])
async def delete_apk(apk_id: str):
    shutil.rmtree(_lib_dir(apk_id))
    return {"ok": True}


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
    return {"ok": True}


# ---------- live screen ----------

@app.websocket("/ws/screen")
async def screen(ws: WebSocket):
    if config.API_TOKEN and ws.query_params.get("token") != config.API_TOKEN:
        await ws.close(code=1008)
        return
    await ws.accept()

    async def pump():
        while True:
            try:
                await ws.send_bytes(await adb.screenshot())
            except adb.AdbError as e:
                await ws.send_text(json.dumps({"error": str(e)}))
                await asyncio.sleep(2)
            await asyncio.sleep(1 / config.STREAM_FPS)

    task = asyncio.create_task(pump())
    try:
        while True:
            m = json.loads(await ws.receive_text())
            try:
                t = m.get("type")
                if t == "tap":
                    await adb.tap(m["x"], m["y"])
                elif t == "swipe":
                    await adb.swipe(m["x1"], m["y1"], m["x2"], m["y2"], int(m.get("ms", 200)))
                elif t == "key":
                    await adb.key(m["key"])
                elif t == "text":
                    await adb.text(m["text"])
            except (adb.AdbError, KeyError, ValueError):
                pass
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
