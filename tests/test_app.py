import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import adb, bundles, config, main, safety, video
from app.providers import check_public_url


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

    async def versions():
        return dict(st.get("versions", {}))

    monkeypatch.setattr(adb, "installed_versions", versions)
    monkeypatch.setattr(adb, "third_party_packages", pkgs)
    monkeypatch.setattr(adb, "install", install)
    monkeypatch.setattr(adb, "install_multiple", install_multiple)
    monkeypatch.setattr(adb, "device_info", info)
    monkeypatch.setattr(adb, "status", status)
    monkeypatch.setattr(main, "state", {"updates": [], "checked": 0, "emulator_msg": ""})
    with TestClient(main.app, base_url="http://localhost") as c:
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


def test_csrf_and_dns_rebinding_blocked(client):
    # cross-site form post (Origin differs from Host)
    r = client.post("/api/upload", files=[("files", ("a.apk", make_apk()))],
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    # rebinding: attacker hostname resolving to 127.0.0.1
    assert client.get("/api/providers", headers={"Host": "evil.example:8000"}).status_code == 403
    assert client.get("/api/providers").status_code == 200


def test_token_required_when_set(client, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "s3cret")
    assert client.get("/api/providers").status_code == 401
    assert client.get("/api/providers", headers={"Authorization": "Bearer s3cret"}).status_code == 200


@pytest.fixture
def fake_adb(monkeypatch, tmp_path):
    import os
    import sys
    script = os.path.join(os.path.dirname(__file__), "fake_adb.py")
    wrapper = tmp_path / "adb"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setattr(config, "ADB_BIN", str(wrapper))
    monkeypatch.setenv("FAKE_ADB_LOG", str(tmp_path / "adb.log"))
    return tmp_path / "adb.log"


def test_real_subprocess_status_and_screenshot(tmp_path, fake_adb, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    with TestClient(main.app, base_url="http://localhost") as c:
        s = c.get("/api/status").json()
        assert s["connected"] and s["screen"] == {"width": 1080, "height": 2400}
        assert c.get("/api/screenshot").content.startswith(b"\x89PNG")


def test_websocket_h264_stream_and_fractional_taps(tmp_path, fake_adb, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    with TestClient(main.app, base_url="http://localhost") as c:
        with c.websocket_connect("/ws/screen?mode=h264", headers={"host": "localhost"}) as ws:
            assert ws.receive_json() == {"mode": "h264"}
            ws.send_text('{"type":"tap","x":0.5,"y":0.25}')
            nals = [ws.receive_bytes() for _ in range(4)]
        assert [n[4] & 0x1F for n in nals] == [7, 8, 5, 1]   # SPS, PPS, IDR, P-slice
        time.sleep(0.3)
        assert "stdin: input tap 539 599" in fake_adb.read_text()   # 0.5*1079, 0.25*2399


def test_websocket_rejects_foreign_origin(tmp_path, fake_adb, monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    with TestClient(main.app, base_url="http://localhost") as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws/screen", headers={"host": "localhost", "Origin": "https://evil.example"}) as ws:
                ws.receive_json()


# ---------------- v1.2 features ----------------
GETEVENT = '''add device 1: /dev/input/event0
  name:     "Power Button"
  events:
    KEY (0001): KEY_POWER
add device 2: /dev/input/event2
  name:     "virtio_input_multi_touch_1"
  events:
    ABS (0003): ABS_MT_SLOT           : value 0, min 0, max 9, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_X     : value 0, min 0, max 32767, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_Y     : value 0, min 0, max 32767, fuzz 0, flat 0, resolution 0
  input props:
    INPUT_PROP_DIRECT
'''


def test_touch_parse_and_commands():
    from app import inputs
    info = inputs.parse_getevent(GETEVENT)
    assert info == {"dev": "/dev/input/event2", "maxx": 32767, "maxy": 32767}
    down = inputs.touch_command(info, "down", 0.5, 0.25, 7)
    assert "sendevent /dev/input/event2 3 57 7" in down and "3 53 16383" in down and "3 54 8191" in down
    assert down.rstrip().endswith("0 0 0")
    up = inputs.touch_command(info, "up", 0, 0, 7)
    assert "3 57 -1" in up and "1 330 0" in up
    assert inputs.parse_getevent("add device 1: /dev/input/event5\n  name: \"x\"\n") is None


def test_text_command_quotes_and_filters():
    c = adb.text_command("hi there; rm -rf / $(x) é")
    assert c.startswith("input text '") and "é" not in c and "hi%sthere" in c
    assert adb.text_command("é") == ""


def test_devices_and_pairing_rules():
    from app import devices
    devs = devices.parse_devices(
        "List of devices attached\nemulator-5554 device product:sdk model:Pixel_5\n192.168.1.20:5555 device model:Pixel_8\nABC123 unauthorized\n")
    assert [d["kind"] for d in devs] == ["emulator", "wifi", "usb"]
    assert devs[2]["state"] == "unauthorized"
    assert devices.check_lan_endpoint("192.168.1.20", 5555) == "192.168.1.20:5555"
    for bad in ("8.8.8.8", "1.1.1.1"):
        with pytest.raises(adb.AdbError):
            devices.check_lan_endpoint(bad, 5555)
    with pytest.raises(adb.AdbError):
        devices.check_lan_endpoint("evil;rm", 5555)


def test_snapshot_and_perf_parsing():
    from app import devices
    snaps = devices.parse_snapshots(
        "List of snapshots present on all disks:\nID        TAG                 VM SIZE                DATE       VM CLOCK\n--        default_boot        1.2 GB     2026-09-30 10:00:00   00:01:00.000\n2         clean               1.1 GB     2026-09-30 11:00:00   00:02:00.000\nOK\n")
    assert [s["name"] for s in snaps] == ["default_boot", "clean"]
    with pytest.raises(adb.AdbError):
        devices._snap_name("bad name;rm")
    text = "cpu  100 0 100 700 100 0 0 0\n---\nMemTotal:  4000000 kB\nMemAvailable:  1000000 kB\n---\n 500000 system_server\n 200000 com.app\n"
    r1, cur = devices.parse_perf(text, None)
    assert r1["cpu_pct"] is None and r1["mem_total_kb"] == 4000000 and r1["top"][0]["name"] == "system_server"
    text2 = text.replace("cpu  100 0 100 700 100", "cpu  200 0 200 800 200")
    r2, _ = devices.parse_perf(text2, cur)
    assert r2["cpu_pct"] == 50.0   # 200 busy of 400 total since the last sample


def test_network_validation():
    import asyncio
    from app import devices
    for kw in ({"proxy": "host;rm:80"}, {"proxy": "nocolon"}):
        with pytest.raises(adb.AdbError):
            asyncio.run(devices.set_network(**kw))
    with pytest.raises(adb.AdbError):
        asyncio.run(devices.set_network(speed="warp"))


def test_device_header_selects_serial_and_bad_serial_rejected(tmp_path, fake_adb, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    with TestClient(main.app, base_url="http://localhost") as c:
        assert c.get("/api/status", headers={"X-Device": "emulator-5556"}).json()["serial"] == "emulator-5556"
        assert "serial:emulator-5556" in fake_adb.read_text()
        assert c.get("/api/status", headers={"X-Device": "--help;x"}).status_code == 400
        devs = c.get("/api/devices").json()
        assert [d["serial"] for d in devs["devices"]] == ["emulator-5554", "192.168.1.20:5555"]
        assert c.post("/api/devices/pair", json={"host": "192.168.1.20", "port": 37000, "code": "123456"}).status_code == 200
        assert c.post("/api/devices/pair", json={"host": "8.8.8.8", "port": 37000, "code": "123456"}).status_code == 400
        assert c.post("/api/devices/pair", json={"host": "192.168.1.20", "port": 37000, "code": "12"}).status_code == 400


def test_websocket_realtime_touch_and_wheel(tmp_path, fake_adb, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    with TestClient(main.app, base_url="http://localhost") as c:
        with c.websocket_connect("/ws/screen?mode=png", headers={"host": "localhost"}) as ws:
            ws.receive_json()
            time.sleep(0.6)   # let the monitor detect the touchscreen
            for phase, y in (("down", 0.5), ("move", 0.6), ("up", 0.6)):
                ws.send_text('{"type":"touch","phase":"%s","x":0.5,"y":%s}' % (phase, y))
            ws.send_text('{"type":"scroll","x":0.5,"y":0.5,"dy":1}')
            ws.send_text('{"type":"text","text":"hello world"}')
            time.sleep(0.6)
        log = fake_adb.read_text()
    assert "stdin: sendevent /dev/input/event2 3 47 0; sendevent /dev/input/event2 3 57" in log   # raw touch down
    assert "3 54 19660" in log                                        # move to y=0.6
    assert "stdin: input swipe 539 1199 539 599 120" in log           # wheel -> swipe up
    assert "stdin: input text hello%sworld" in log
    assert log.count("\nshell\nstdin:") == 1                          # one persistent shell, not one per event


@pytest.mark.asyncio
async def test_rollback_keeps_data_when_downgrade_blocked(tmp_path, monkeypatch):
    from app import history, installer
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    snap = history.rollback_root() / "e1"
    snap.mkdir(parents=True)
    (snap / "0_base.apk").write_bytes(make_apk())
    calls = []

    async def install(path, downgrade=False):
        calls.append(("install", downgrade))
        if downgrade:
            raise adb.AdbError("INSTALL_FAILED_VERSION_DOWNGRADE")
        return "Success"

    async def uninstall(pkg, keep_data=False):
        calls.append(("uninstall", keep_data))

    monkeypatch.setattr(adb, "install", install)
    monkeypatch.setattr(adb, "uninstall", uninstall)
    entry = {"id": "e1", "ok": True, "package": "com.x.app", "rollback_dir": str(snap), "prev_version_code": 3}
    assert await installer.rollback(entry) == "com.x.app"
    assert calls == [("install", True), ("uninstall", True), ("install", False)]
    assert history.load()[0]["kind"] == "rollback"


def test_history_records_update_with_snapshot_and_retry(client, monkeypatch):
    async def versions():
        return {"com.big.game": 5}

    async def apk_paths(pkg):
        return ["/data/app/base.apk"]

    async def pull(remote, local):
        open(local, "wb").write(b"old")

    monkeypatch.setattr(adb, "installed_versions", versions)
    monkeypatch.setattr(adb, "apk_paths", apk_paths)
    monkeypatch.setattr(adb, "pull", pull)
    import json
    x = io.BytesIO()
    with zipfile.ZipFile(x, "w") as z:
        z.writestr("manifest.json", json.dumps({"package_name": "com.big.game", "split_apks": [{"file": "base.apk"}]}))
        z.writestr("base.apk", make_apk())
    j = upload(client, [("game.xapk", x.getvalue())])
    assert j["results"][0]["ok"]
    h = client.get("/api/history").json()
    assert h[0]["prev_version_code"] == 5 and h[0]["can_rollback"] and h[0]["can_retry"]
    # retry re-installs the stored file
    r = client.post(f"/api/history/{h[0]['id']}/retry")
    assert r.status_code == 200 and wait_job(client, r.json()["job"])["status"] == "done"
    assert len(client.get("/api/history").json()) == 2
    assert client.delete(f"/api/history/{h[0]['id']}").status_code == 200
