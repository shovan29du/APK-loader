import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import adb, bundles, config, main, safety, video
from app.providers import Resolved, check_public_url


def make_apk(extra=None):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("AndroidManifest.xml", b"x")
        z.writestr("META-INF/CERT.RSA", b"sig")
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return b.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "dl").mkdir()
    st = {"pkgs": set(), "calls": []}

    async def pkgs():
        return set(st["pkgs"])

    async def install(path):
        st["calls"].append(("install", path))
        st["pkgs"].add("com.example.app%d" % len(st["calls"]))
        return "Success"

    async def install_multiple(paths):
        st["calls"].append(("multi", paths))
        st["pkgs"].add("com.example.split")
        return "Success"

    async def info():
        return {"abis": ["arm64-v8a"], "density": 420}

    async def status():
        return {"connected": True}

    monkeypatch.setattr(adb, "third_party_packages", pkgs)
    monkeypatch.setattr(adb, "install", install)
    monkeypatch.setattr(adb, "install_multiple", install_multiple)
    monkeypatch.setattr(adb, "device_info", info)
    monkeypatch.setattr(adb, "status", status)
    monkeypatch.setattr(main, "state", {"updates": [], "checked": 0, "emulator_msg": ""})
    with TestClient(main.app) as c:
        c.st = st
        yield c


def wait_job(c, jid, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def upload(c, files, split=False):
    r = c.post("/api/upload", files=[("files", (n, d, "application/octet-stream")) for n, d in files],
               data={"split": str(split).lower()})
    assert r.status_code == 200, r.text
    return wait_job(c, r.json()["job"])


def test_upload_multiple_separate(client):
    j = upload(client, [(f"a{i}.apk", make_apk()) for i in range(3)])
    assert j["status"] == "done" and len(j["results"]) == 3 and all(r["ok"] for r in j["results"])
    assert [c[0] for c in client.st["calls"]] == ["install"] * 3


def test_upload_split_uses_install_multiple(client):
    j = upload(client, [(f"a{i}.apk", make_apk()) for i in range(3)], split=True)
    assert len(j["results"]) == 1 and j["results"][0]["package"] == "com.example.split"
    assert client.st["calls"][0][0] == "multi"


def test_corrupt_apk_blocked_by_safety(client):
    j = upload(client, [("bad.apk", b"not a zip")])
    assert j["status"] == "error" and "safety" in j["results"][0]["error"]
    assert client.st["calls"] == []


def test_rejects_unknown_extension(client):
    r = client.post("/api/upload", files=[("files", ("x.exe", b"x"))])
    assert r.status_code == 400


def test_xapk_installs_as_split(client):
    import json
    x = io.BytesIO()
    with zipfile.ZipFile(x, "w") as z:
        z.writestr("manifest.json", json.dumps({"package_name": "com.big.game", "split_apks": [
            {"file": "base.apk"}, {"file": "config.arm64_v8a.apk"}, {"file": "config.x86_64.apk"}]}))
        for n in ("base.apk", "config.arm64_v8a.apk", "config.x86_64.apk"):
            z.writestr(n, make_apk())
    j = upload(client, [("game.xapk", x.getvalue())])
    assert j["results"][0]["ok"], j
    kind, paths = client.st["calls"][0]
    assert kind == "multi" and len(paths) == 2 and not any("x86_64" in p for p in paths)


def test_library_list_and_delete(client):
    upload(client, [("a.apk", make_apk())])
    lib = client.get("/api/apks").json()
    assert len(lib) == 1
    assert client.get(f"/api/apks/{lib[0]['id']}/{lib[0]['files'][0]['name']}").status_code == 200
    assert client.delete(f"/api/apks/{lib[0]['id']}").status_code == 200
    assert client.get("/api/apks").json() == []


def test_select_splits():
    names = ["base.apk", "config.arm64_v8a.apk", "config.armeabi_v7a.apk", "config.x86.apk",
             "config.mdpi.apk", "config.xxhdpi.apk", "config.xxxhdpi.apk", "config.en.apk"]
    got = bundles.select_splits(names, ["arm64-v8a", "armeabi-v7a"], 420)
    assert got == ["base.apk", "config.arm64_v8a.apk", "config.xxhdpi.apk", "config.en.apk"]


def test_zip_slip_rejected(tmp_path):
    p = tmp_path / "evil.xapk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("../../evil.apk", b"x")
    with pytest.raises(ValueError):
        bundles.extract_bundle(p, tmp_path / "out", ["arm64-v8a"], 320)


@pytest.mark.asyncio
async def test_hash_mismatch_blocks(tmp_path):
    f = tmp_path / "a.apk"
    f.write_bytes(make_apk())
    r = await safety.verify(f, expected_sha256="0" * 64)
    assert r.blocked
    r = await safety.verify(f, expected_sha256=safety.hashes(f)[0])
    assert not r.blocked


def test_nal_splitter():
    s = video.NalSplitter()
    a, b, c = b"\x00\x00\x00\x01\x67aaa", b"\x00\x00\x00\x01\x68bb", b"\x00\x00\x01\x65cccc"
    out = s.feed(a + b[:3])
    assert out == []
    out = s.feed(b[3:] + c)
    assert out == [a, b]
    assert s.flush() == [c]


def test_ssrf_blocked():
    for u in ("http://127.0.0.1/a.apk", "file:///etc/passwd", "http://10.0.0.1/x"):
        with pytest.raises(ValueError):
            check_public_url(u)


def test_remote_path_restricted():
    assert adb.safe_remote("/sdcard/Download") == "/sdcard/Download"
    for bad in ("/data/data", "/sdcard/../data", "/etc/passwd"):
        with pytest.raises(adb.AdbError):
            adb.safe_remote(bad)


def test_package_validation():
    assert adb.valid_package("com.example.app")
    assert not adb.valid_package("com.x; rm -rf /")


def test_hardware_profile_for_target_laptop():
    from app import hardware
    r = hardware.recommend({"ram_mb": 16000, "threads": 12, "nvidia": "NVIDIA GeForce RTX 5050 Laptop GPU"})
    assert r["emu_ram_mb"] == 4096 and r["emu_cores"] == 6 and r["emu_gpu"] == "host"
    assert r["video_max"] == 1920
    low = hardware.recommend({"ram_mb": 4000, "threads": 4, "nvidia": ""})
    assert low["emu_gpu"] == "swiftshader_indirect" and low["emu_ram_mb"] == 2048


def test_watcher_installs_only_new_stable_downloads(tmp_path):
    from app.watcher import Watcher
    (tmp_path / "old.apk").write_bytes(b"old")
    w = Watcher(tmp_path)
    w.baseline()
    assert w.scan() == []
    (tmp_path / "new.apk").write_bytes(b"abc")
    (tmp_path / "half.apk.crdownload").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    assert w.scan() == []                       # first sighting: size not yet confirmed stable
    (tmp_path / "new.apk").write_bytes(b"abcd")  # still growing
    assert w.scan() == []
    assert w.scan() == [tmp_path / "new.apk"]    # unchanged across scans -> ready
    assert w.scan() == []                        # only once
