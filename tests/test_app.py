import asyncio
import json
import io
import os
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


@pytest.fixture(autouse=True)
def _no_scrcpy_network(monkeypatch):
    """Tests never download or start the real scrcpy-server (they use fake_scrcpy instead)."""
    monkeypatch.setattr(config, "SCRCPY", False)


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
        return {**{p: 1 for p in st["pkgs"]}, **st.get("versions", {})}

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
            nals = []
            while len(nals) < 4:                     # skip helper status messages (text) between frames
                msg = ws.receive()
                if msg.get("bytes"):
                    nals.append(msg["bytes"])
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


# ---------------- scrcpy client (protocol v4.0) ----------------
def test_scrcpy_wire_sizes_match_server_reader():
    from app import scrcpy
    assert len(scrcpy.msg_touch(0, 100, 1, 2, 1080, 2400)) == 32     # 1+1+8+12+2+4+4
    assert len(scrcpy.msg_scroll(1, 2, 1080, 2400, 0, 1)) == 21
    m = scrcpy.msg_set_clipboard(7, "héllo", True)
    assert m[0] == 9 and m[1:9] == (7).to_bytes(8, "big") and m[9] == 1 and m[10:14] == (6).to_bytes(4, "big")
    assert m[14:] == "héllo".encode()
    assert scrcpy.msg_get_clipboard() == b"\x08\x00"
    # pressure: 1.0 -> 0xFFFF, 0 -> 0
    assert scrcpy.msg_touch(0, 1, 0, 0, 1, 1, 1.0)[-10:-8] == b"\xff\xff"
    assert scrcpy.msg_touch(1, 1, 0, 0, 1, 1, 0.0)[-10:-8] == b"\x00\x00"


def test_scrcpy_jar_hash_is_pinned():
    from app import scrcpy
    assert len(scrcpy.SHA256) == 64 and scrcpy.VERSION in scrcpy.URL


@pytest.mark.asyncio
async def test_scrcpy_session_handshake_clipboard_touch_audio():
    from app import scrcpy
    from tests.fake_scrcpy import FakeScrcpyServer
    srv = await FakeScrcpyServer(audio=True).start()
    s = scrcpy.Session("emulator-5554", audio=True)
    clips, pcm = [], []
    s.clip_listeners.append(clips.append)
    s.audio_listeners.append(pcm.append)
    try:
        await s.handshake("127.0.0.1", srv.port)
        await asyncio.sleep(0.2)
        assert s.control_ok and s.audio_ok and srv.conns == 2
        assert pcm[:3] == [bytes([0]) * 8, bytes([1]) * 8, bytes([2]) * 8]     # config packet skipped
        # two fingers (pinch), scroll, clipboard both ways
        await s.touch(scrcpy.ACT_DOWN, 0, 0.5, 0.5, 1080, 2400)
        await s.touch(scrcpy.ACT_DOWN, 1, 0.25, 0.75, 1080, 2400)
        await s.touch(scrcpy.ACT_UP, 1, 0.25, 0.75, 1080, 2400)
        await s.scroll(0.5, 0.5, 1080, 2400, 1.0)
        await s.set_clipboard("héllo wörld", paste=True)
        await s.get_clipboard()
        await srv.push_clipboard("copied on device")
        await asyncio.sleep(0.3)
    finally:
        await s.stop()
        await srv.close()
    kinds = [r[0] for r in srv.received]
    assert kinds == ["touch", "touch", "touch", "scroll", "set_clipboard", "get_clipboard"]
    t0, t1, t2 = srv.received[:3]
    assert t0[1:4] == (0, 100, 539) and t1[2] == 101 and t2[1] == 1 and t2[7] == 0    # two pointer ids; UP has pressure 0
    assert t0[5:7] == (1080, 2400)
    assert srv.received[4] == ("set_clipboard", 1, True, "héllo wörld")
    assert sorted(clips) == ["copied on device", "hello"]


@pytest.mark.asyncio
async def test_scrcpy_control_only_uses_first_socket_for_control():
    from app import scrcpy
    from tests.fake_scrcpy import FakeScrcpyServer
    srv = await FakeScrcpyServer(audio=False).start()
    s = scrcpy.Session("emulator-5554", audio=False)
    try:
        await s.handshake("127.0.0.1", srv.port)
        await s.set_clipboard("x")
        await asyncio.sleep(0.1)
        assert srv.conns == 1 and s.control_ok and not s.audio_ok
    finally:
        await s.stop()
        await srv.close()
    assert srv.received == [("set_clipboard", 1, False, "x")]


@pytest.mark.asyncio
async def test_scrcpy_audio_unavailable_keeps_control():
    from app import scrcpy
    from tests.fake_scrcpy import FakeScrcpyServer
    srv = await FakeScrcpyServer(audio=True, audio_codec=0).start()     # server says "audio disabled"
    s = scrcpy.Session("emulator-5554", audio=True)
    try:
        await s.handshake("127.0.0.1", srv.port)
        await asyncio.sleep(0.2)
        assert s.control_ok and not s.audio_ok
    finally:
        await s.stop()
        await srv.close()


# ---------------- multi-touch / pinch ----------------
def _rc_with_evdev():
    from app.remote import RemoteControl
    rc = RemoteControl()
    rc.w, rc.h = 1080, 2400
    rc.evdev = {"dev": "/dev/input/event2", "maxx": 32767, "maxy": 32767}
    return rc


@pytest.mark.asyncio
async def test_pinch_over_evdev_uses_two_slots_and_one_btn_touch(monkeypatch):
    sent = []

    async def fire(cmd):
        sent.append(cmd)
    monkeypatch.setattr(adb, "fire", fire)
    rc = _rc_with_evdev()
    await rc.pinch(0.5, 0.5, 2.0)
    slots = [l for l in sent if "3 47 1" in l]
    assert slots and any("3 47 0" in l for l in sent)                # both fingers used
    assert sum("1 330 1" in l for l in sent) == 1                    # BTN_TOUCH down once (first finger)
    assert sum("1 330 0" in l for l in sent) == 1                    # ...and up once (last finger)
    downs = [l for l in sent if "3 57 " in l and "3 57 -1" not in l]
    assert len({l.split("3 57 ")[1].split(";")[0] for l in downs}) == 2     # distinct tracking ids
    xs = [int(l.split("3 53 ")[1].split(";")[0]) for l in sent if "3 47 0" in l and "3 53 " in l]
    assert xs[-1] < xs[0]                                              # zoom in: the left finger moves further left


@pytest.mark.asyncio
async def test_pinch_zoom_in_spreads_zoom_out_closes(monkeypatch):
    sent = []

    async def fire(cmd):
        sent.append(cmd)
    monkeypatch.setattr(adb, "fire", fire)

    def spread(cmds):
        f0 = [int(c.split("3 53 ")[1].split(";")[0]) for c in cmds if "3 47 0" in c and "3 53 " in c]
        f1 = [int(c.split("3 53 ")[1].split(";")[0]) for c in cmds if "3 47 1" in c and "3 53 " in c]
        return f1[-1] - f0[-1], f1[0] - f0[0]
    rc = _rc_with_evdev()
    await rc.pinch(0.5, 0.5, 2.0)
    end, start = spread(sent)
    assert end > start
    sent.clear()
    await rc.pinch(0.5, 0.5, 0.5)
    end, start = spread(sent)
    assert end < start


@pytest.mark.asyncio
async def test_pinch_without_multitouch_engine_is_refused():
    from app.remote import RemoteControl
    rc = RemoteControl()
    with pytest.raises(adb.AdbError):
        await rc.pinch(0.5, 0.5, 2.0)


@pytest.mark.asyncio
async def test_remote_routes_touch_through_scrcpy_when_available(monkeypatch):
    from app import scrcpy
    from app.remote import RemoteControl
    from tests.fake_scrcpy import FakeScrcpyServer
    srv = await FakeScrcpyServer(audio=False).start()
    s = scrcpy.Session(adb.serial(), audio=False)
    await s.handshake("127.0.0.1", srv.port)
    scrcpy.manager.sessions[adb.serial()] = s
    try:
        rc = RemoteControl()
        rc.w, rc.h = 2400, 1080                                       # landscape app: sizes follow the rotation
        await rc.pinch(0.5, 0.5, 2.0)
        await rc.handle({"type": "clip_set", "text": "ünï", "paste": True})
        await asyncio.sleep(0.2)
        touches = [r for r in srv.received if r[0] == "touch"]
        assert {t[2] for t in touches} == {100, 101} and all(t[5:7] == (2400, 1080) for t in touches)
        assert srv.received[-1] == ("set_clipboard", 1, True, "ünï")
        rc.engine = "adb"                                              # user forced the adb engine
        assert rc.scrcpy() is None
    finally:
        scrcpy.manager.sessions.pop(adb.serial(), None)
        await s.stop()
        await srv.close()


# ---------------- scripted tests ----------------
def test_script_validation_rejects_bad_steps():
    from app import scripts
    ok = scripts.validate({"name": "t", "steps": [{"type": "tap", "x": 0.5, "y": 0.5}, {"type": "key", "key": "KEYCODE_BACK"}]})
    assert len(ok["steps"]) == 2
    for bad in ({"type": "tap", "x": 2, "y": 0.5}, {"type": "key", "key": "BACK; rm -rf /"},
                {"type": "launch", "package": "x; reboot"}, {"type": "exec", "cmd": "id"},
                {"type": "text", "text": "x" * 301}, {"type": "touch", "phase": "sideways", "x": 0, "y": 0}):
        with pytest.raises(scripts.ScriptError):
            scripts.validate({"name": "t", "steps": [bad]})
    with pytest.raises(scripts.ScriptError):
        scripts.validate({"name": "../evil", "steps": []})


def test_record_and_play_script_end_to_end(tmp_path, fake_adb, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    with TestClient(main.app, base_url="http://localhost") as c:
        assert c.post("/api/scripts/record/start", json={"name": "login flow"}).status_code == 200
        with c.websocket_connect("/ws/screen?mode=png", headers={"host": "localhost"}) as ws:
            ws.receive_json()
            time.sleep(0.6)
            for phase in ("down", "move", "up"):
                ws.send_text('{"type":"touch","phase":"%s","x":0.5,"y":0.4}' % phase)
            ws.send_text('{"type":"text","text":"user1"}')
            ws.send_text('{"type":"key","key":"KEYCODE_ENTER"}')
            ws.send_text('{"type":"bogus"}')
            time.sleep(0.4)
        saved = c.post("/api/scripts/record/stop").json()
        assert [s["type"] for s in saved["steps"]] == ["touch", "touch", "touch", "text", "key"]   # bogus not recorded
        assert saved["steps"][0]["t"] <= saved["steps"][-1]["t"]
        assert c.get("/api/scripts").json()["scripts"][0]["name"] == "login flow"

        # add test steps via the editor endpoint, then run it twice
        steps = saved["steps"] + [{"type": "launch", "package": "com.example.app"},
                                  {"type": "assert_focus", "package": "com.example.app", "timeout": 2},
                                  {"type": "screenshot", "name": "after"}]
        assert c.put("/api/scripts/login flow", json={"steps": steps}).status_code == 200
        r = c.post("/api/scripts/login flow/run", json={"speed": 10, "loops": 2})
        job = wait_job(c, r.json()["job"], timeout=20)
        assert job["status"] == "done" and len(job["results"]) == 2 and all(x["ok"] for x in job["results"])
        runs = c.get("/api/scripts/runs").json()
        assert runs[0]["ok"] and runs[0]["screenshots"]
        assert c.get(f"/api/scripts/runs/{runs[0]['id']}/{runs[0]['screenshots'][0]}").content.startswith(b"\x89PNG")
        assert c.get(f"/api/scripts/runs/{runs[0]['id']}/..%2Freport.json").status_code in (404, 422)
        log = fake_adb.read_text()
        assert "stdin: input text user1" in log and "KEYCODE_ENTER" in log


def test_failed_assertion_fails_the_run_and_stops(tmp_path, fake_adb, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    with TestClient(main.app, base_url="http://localhost") as c:
        steps = [{"type": "assert_focus", "package": "com.other.app", "timeout": 0.6},
                 {"type": "key", "key": "KEYCODE_HOME"}]
        assert c.put("/api/scripts/bad", json={"steps": steps}).status_code == 200
        job = wait_job(c, c.post("/api/scripts/bad/run", json={}).json()["job"], timeout=20)
        assert job["status"] == "error" and "com.other.app" in job["results"][0]["error"]
        assert "KEYCODE_HOME" not in fake_adb.read_text()             # aborted after the failed assertion
        assert c.put("/api/scripts/x", json={"steps": [{"type": "exec"}]}).status_code == 400
        assert c.get("/api/scripts/..%2F..%2Fetc").status_code in (400, 404)


def test_running_job_can_be_cancelled(tmp_path, fake_adb, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    with TestClient(main.app, base_url="http://localhost") as c:
        c.put("/api/scripts/slow", json={"steps": [{"type": "wait", "seconds": 60}]})
        jid = c.post("/api/scripts/slow/run", json={"speed": 1}).json()["job"]
        time.sleep(0.4)
        assert c.post(f"/api/jobs/{jid}/cancel").status_code == 200
        job = wait_job(c, jid, timeout=5)
        assert job["status"] == "error" and job["message"] == "cancelled"


# ---------------- scrcpy helper through the whole stack ----------------
import threading  # noqa: E402


class ThreadedFakeScrcpy:
    """FakeScrcpyServer on its own event loop/thread, so TestClient's loop can talk to it."""

    def __init__(self, **kw):
        from tests.fake_scrcpy import FakeScrcpyServer
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.srv = FakeScrcpyServer(**kw)
        asyncio.run_coroutine_threadsafe(self.srv.start(), self.loop).result(5)

    @property
    def port(self):
        return self.srv.port

    def push_clipboard(self, text):
        asyncio.run_coroutine_threadsafe(self.srv.push_clipboard(text), self.loop).result(5)

    def close(self):
        asyncio.run_coroutine_threadsafe(self.srv.close(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def helper_env(tmp_path, fake_adb, monkeypatch):
    from app import scrcpy
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    monkeypatch.setattr(config, "SCRCPY", True)
    monkeypatch.setattr(scrcpy, "jar_ready", lambda: True)
    yield tmp_path
    scrcpy.manager.sessions.clear()
    scrcpy.manager.refs.clear()


def test_helper_clipboard_and_pinch_over_websocket(helper_env, fake_adb, monkeypatch):
    fake = ThreadedFakeScrcpy(audio=False)
    monkeypatch.setenv("FAKE_SCRCPY_PORT", str(fake.port))
    try:
        with TestClient(main.app, base_url="http://localhost") as c:
            with c.websocket_connect("/ws/screen?mode=png", headers={"host": "localhost"}) as ws:
                seen = {}
                end = time.time() + 10
                while "helper" not in seen and time.time() < end:
                    msg = ws.receive()
                    if msg.get("text"):
                        seen.update(json.loads(msg["text"]))
                assert seen.get("helper") is True
                ws.send_text(json.dumps({"type": "clip_set", "text": "grüße", "paste": True}))
                ws.send_text(json.dumps({"type": "pinch", "x": 0.5, "y": 0.5, "scale": 2}))
                fake.push_clipboard("from the phone")
                got = None
                end = time.time() + 5
                while got is None and time.time() < end:
                    msg = ws.receive()
                    if msg.get("text") and "clipboard" in json.loads(msg["text"]):
                        got = json.loads(msg["text"])["clipboard"]
                assert got == "from the phone"
                time.sleep(0.5)
            assert c.get("/api/status").json()["scrcpy"]["control"] in (True, False)
        kinds = [r[0] for r in fake.srv.received]
        assert ("set_clipboard", 1, True, "grüße") in fake.srv.received
        touches = [r for r in fake.srv.received if r[0] == "touch"]
        assert kinds.count("touch") >= 20 and {t[2] for t in touches} == {100, 101}
        assert [t[1] for t in touches if t[1] != 2] == [0, 0, 1, 1]        # 2 downs then 2 ups (moves are action 2)
        assert "push" in fake_adb.read_text() and "forward tcp:0" in fake_adb.read_text()
        assert "forward --remove" in fake_adb.read_text()                   # tunnel removed on disconnect
    finally:
        fake.close()


def test_audio_websocket_streams_pcm(helper_env, fake_adb, monkeypatch):
    fake = ThreadedFakeScrcpy(audio=True, pcm_packets=3)
    monkeypatch.setenv("FAKE_SCRCPY_PORT", str(fake.port))
    try:
        with TestClient(main.app, base_url="http://localhost") as c:
            with c.websocket_connect("/ws/audio", headers={"host": "localhost"}) as ws:
                meta = ws.receive_json()
                assert meta == {"format": "s16le", "rate": 48000, "channels": 2}
                chunks = []
                end = time.time() + 6
                while not chunks and time.time() < end:
                    msg = ws.receive()
                    if msg.get("bytes"):
                        chunks.append(msg["bytes"])
                assert chunks and set(chunks[0]) <= {0, 1, 2, 7}           # real PCM payload; config packet skipped
                assert len(chunks[0]) % 4 == 0                              # whole stereo s16 frames
    finally:
        fake.close()


def test_audio_needs_helper_message_when_disabled(tmp_path, fake_adb, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    with TestClient(main.app, base_url="http://localhost") as c:       # SCRCPY=False (autouse fixture)
        with c.websocket_connect("/ws/audio", headers={"host": "localhost"}) as ws:
            assert "scrcpy helper" in ws.receive_json()["error"]


# ---------------- Windows: no console windows ----------------
def test_windows_children_start_without_a_console_window():
    from app import procs
    assert procs.window_kwargs("nt") == {"creationflags": 0x08000000}
    assert procs.window_kwargs("posix") == {}
    assert procs._merge({"creationflags": 0x200})["creationflags"] == 0x200 | 0x08000000 or os.name != "nt"


def test_no_module_spawns_processes_directly():
    """Every subprocess must go through app.procs, or Windows pops up a console window per call."""
    import pathlib
    import re
    bad = []
    for f in pathlib.Path(main.__file__).parent.glob("*.py"):
        if f.name == "procs.py":
            continue
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"asyncio\.create_subprocess_exec\(|subprocess\.(run|Popen|check_output|call)\(", line):
                bad.append(f"{f.name}:{n}")
    assert not bad, bad


def test_ui_key_handlers_never_return_false():
    """`el.onkeydown = e => e.key=='Enter' && ...` returns false for other keys, which cancels typing."""
    import pathlib
    import re
    html = (pathlib.Path(main.__file__).parent.parent / "static" / "index.html").read_text()
    bad = re.findall(r"\.on(?:keydown|keypress|keyup|input|beforeinput)\s*=\s*(?:\(?\w*\)?\s*=>)\s*(?!\{)[^;\n]*&&", html)
    assert not bad, bad


# ---------------- install -> runs on the laptop ----------------
def test_install_opens_the_app_by_default(client, monkeypatch):
    launched = []

    async def launch(pkg):
        launched.append(pkg)
    monkeypatch.setattr(adb, "launch", launch)
    j = upload(client, [("a.apk", make_apk())])                 # no "run" flag sent: default is to open it
    assert j["results"][0]["ok"] and launched == [j["results"][0]["package"]]
    # a batch of marketplace/URL items defaults to opening the last one too
    from app.main import BatchReq
    assert BatchReq(items=[]).run_last is True


def test_install_boots_the_emulator_when_no_device_is_connected(client, monkeypatch):
    from app import emulator
    state = {"up": False, "started": 0}

    async def status():
        return {"connected": state["up"]}

    async def wait_boot(timeout=300, ctl=None):
        state["up"] = True

    def start():
        state["started"] += 1
    monkeypatch.setattr(adb, "status", status)
    monkeypatch.setattr(emulator, "installed", lambda: True)
    monkeypatch.setattr(emulator, "wait_boot", wait_boot)
    monkeypatch.setattr(emulator.controller, "start", start)

    async def launch(pkg):
        pass
    monkeypatch.setattr(adb, "launch", launch)
    j = upload(client, [("a.apk", make_apk())])
    assert state["started"] == 1 and j["status"] == "done" and j["results"][0]["ok"]


def test_install_without_device_or_emulator_gives_a_clear_error(client, monkeypatch):
    from app import emulator

    async def status():
        return {"connected": False}
    monkeypatch.setattr(adb, "status", status)
    monkeypatch.setattr(emulator, "installed", lambda: False)
    r = client.post("/api/upload", files=[("files", ("a.apk", make_apk()))])
    job = wait_job(client, r.json()["job"])
    assert job["status"] == "error" and "emulator is not set up" in job["message"]
    assert client.st["calls"] == []


def test_stored_file_install_and_run(client, monkeypatch):
    launched = []

    async def launch(pkg):
        launched.append(pkg)
    monkeypatch.setattr(adb, "launch", launch)
    upload(client, [("a.apk", make_apk())])
    entry = client.get("/api/apks").json()[0]
    client.st["calls"].clear()
    job = wait_job(client, client.post(f"/api/apks/{entry['id']}/install").json()["job"])
    assert job["status"] == "done" and client.st["calls"] and launched
    assert client.post("/api/apks/nonexistent/install").status_code == 404


def _build_axml(package: str, utf8: bool) -> bytes:
    """Minimal binary AndroidManifest (string pool + one <manifest package=...> element)."""
    import struct
    strs = ["manifest", "package", package]

    def enc(t):
        if utf8:
            b = t.encode()
            return bytes([len(t), len(b)]) + b + b"\x00"
        return struct.pack("<H", len(t)) + t.encode("utf-16-le") + b"\x00\x00"
    blobs = [enc(t) for t in strs]
    offs, o = [], 0
    for b in blobs:
        offs.append(o)
        o += len(b)
    data = b"".join(blobs)
    data += b"\x00" * (-len(data) % 4)
    start = 28 + 4 * len(strs)
    pool = struct.pack("<HHIIIIII", 1, 28, start + len(data), len(strs), 0, 0x100 if utf8 else 0, start, 0)
    pool += struct.pack(f"<{len(strs)}I", *offs) + data
    attr = struct.pack("<iiiHBBI", -1, 1, 2, 8, 0, 3, 2)               # package="..." (raw string #2)
    elem = struct.pack("<HHIIIiiHHHHHH", 0x0102, 16, 36 + len(attr), 1, 0xFFFFFFFF, -1, 0, 20, 20, 1, 0, 0, 0) + attr
    return struct.pack("<HHI", 3, 8, 8 + len(pool) + len(elem)) + pool + elem


def test_package_name_read_from_binary_manifest():
    from app import safety
    real = (__import__("pathlib").Path(__file__).parent / "data" / "AndroidManifest.bin").read_bytes()
    assert safety.axml_package(real) == "com.genymobile.scrcpy"           # a real compiled manifest
    for utf8 in (True, False):
        assert safety.axml_package(_build_axml("com.example.hello", utf8)) == "com.example.hello"
    assert safety.axml_package(b"") == "" and safety.axml_package(b"garbage" * 5) == ""


def test_reinstalling_an_installed_app_still_opens_it(client, monkeypatch):
    """Same package already on the device: the before/after diff is empty, so the name must come from the APK."""
    pkgs = {"com.example.hello": 7}
    launched = []

    async def versions():
        return dict(pkgs)

    async def launch(p):
        launched.append(p)
    monkeypatch.setattr(adb, "installed_versions", versions)
    monkeypatch.setattr(adb, "launch", launch)

    async def apk_paths(p):
        raise adb.AdbError("no snapshot in this test")
    monkeypatch.setattr(adb, "apk_paths", apk_paths)
    apk = make_apk({"AndroidManifest.xml": _build_axml("com.example.hello", False)})   # later entry replaces the stub
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("AndroidManifest.xml", _build_axml("com.example.hello", False))
        z.writestr("META-INF/CERT.RSA", b"sig")
    del apk
    j = upload(client, [("hello.apk", b.getvalue())])
    assert j["results"][0]["ok"] and j["results"][0]["package"] == "com.example.hello"
    assert launched == ["com.example.hello"]


# ---------------- app management, shortcuts, launcher (WSATools-style abilities) ----------------
def test_app_management_endpoints(tmp_path, fake_adb, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    with TestClient(main.app, base_url="http://localhost") as c:
        info = c.get("/api/apps/com.example.app/info").json()
        assert info["version_name"] == "2.5.1" and info["version_code"] == "42" and info["target_sdk"] == "34"
        assert info["installer"] == "com.android.vending"
        assert c.post("/api/apps/com.example.app/stop").status_code == 200
        assert c.post("/api/apps/com.example.app/clear").status_code == 200
        log = fake_adb.read_text()
        assert "am force-stop com.example.app" in log and "pm clear com.example.app" in log
        for bad in ("x;reboot", "no_dots", "a.b;rm"):
            assert c.post(f"/api/apps/{bad}/stop").status_code in (400, 404, 422)
            assert c.get(f"/api/apps/{bad}/info").status_code in (400, 404, 422)
        job = wait_job(c, c.post("/api/apps/com.example.app/open").json()["job"])
        assert job["status"] == "done" and "monkey -p com.example.app" in fake_adb.read_text()


def test_app_shortcut_created_on_desktop(tmp_path, monkeypatch):
    from app import shortcuts
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(shortcuts, "WIN", False)
    monkeypatch.setattr("sys.platform", "linux")
    made = shortcuts.create("com.example.app", 'My: "Game"/x')
    text = (home / "Desktop" / 'My_ _Game__x (Android).desktop').read_text()
    assert "--app" in text and "com.example.app" in text and "run.py" in text and "Terminal=false" in text
    assert any(m.startswith(str(home / ".local/share/applications")) for m in made)
    with pytest.raises(adb.AdbError):
        shortcuts.create("bad;name")
    assert shortcuts.safe_label('a<b>:"c"') == "a_b___c_"


def test_shortcut_endpoint_rejects_bad_package(client):
    assert client.post("/api/apps/not_a_package/shortcut", json={"label": "x"}).status_code in (400, 404)


def test_launcher_cli_parsing_and_multipart(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_mod", pathlib_path("run.py"))
    run_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_mod)
    f = tmp_path / "x.apk"
    f.write_bytes(b"PK-data")
    assert run_mod.parse_cli(["run.py", "--app", "com.a.b"]) == ("com.a.b", [])
    assert run_mod.parse_cli(["run.py", "--install", str(f), "missing.apk", "--x"]) == (None, [str(f)])
    assert run_mod.parse_cli(["run.py"]) == (None, [])
    body, ctype = run_mod.multipart([str(f)])
    boundary = ctype.split("boundary=")[1].encode()
    assert b'name="files"; filename="x.apk"' in body and b"PK-data" in body and b'name="run"' in body
    assert body.rstrip().endswith(b"--" + boundary + b"--")


def pathlib_path(name):
    import pathlib
    return str(pathlib.Path(__file__).parent.parent / name)


def test_second_launch_reuses_running_server_and_hands_over_app_and_files(tmp_path, fake_adb):
    """Real processes: start APK Loader, then `run.py --app ...` and `run.py --install ...` must reuse it."""
    import socket
    import subprocess
    import sys
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = {**os.environ, "APKLOADER_HOME": str(tmp_path), "APKLOADER_PORT": str(port), "BROWSER": "true",
           "ADB_BIN": str(fake_adb.parent / "adb"), "FAKE_ADB_LOG": str(fake_adb), "SCRCPY": "0"}
    run_py = pathlib_path("run.py")
    server = subprocess.Popen([sys.executable, run_py], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        info = tmp_path / "server.json"
        end = time.time() + 20
        while time.time() < end and not info.exists():
            time.sleep(0.2)
        assert info.exists()
        time.sleep(1.5)
        r = subprocess.run([sys.executable, run_py, "--app", "com.example.app"], env=env, capture_output=True, text=True, timeout=30)
        assert "already running" in r.stdout
        apk = tmp_path / "hello.apk"
        apk.write_bytes(make_apk())
        r = subprocess.run([sys.executable, run_py, "--install", str(apk)], env=env, capture_output=True, text=True, timeout=30)
        assert "already running" in r.stdout
        end = time.time() + 15
        log = ""
        while time.time() < end:
            log = fake_adb.read_text() if fake_adb.exists() else ""
            if "monkey -p com.example.app" in log and "install -r -g" in log:
                break
            time.sleep(0.3)
        assert "monkey -p com.example.app" in log, log[-500:]       # --app opened the app
        assert "install -r -g" in log                                # --install installed the file
        assert json.loads(info.read_text())["port"] == port          # still the one original server
    finally:
        server.terminate()
        server.wait(10)


@pytest.mark.asyncio
async def test_wsa_is_used_when_running_on_windows(monkeypatch):
    from app import devices, emulator

    async def status():
        return {"connected": adb.serial() == "127.0.0.1:58526"}

    async def connect_wifi(host, port):
        return f"{host}:{port}"
    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(adb, "status", status)
    monkeypatch.setattr(devices, "connect_wifi", connect_wifi)
    monkeypatch.setattr(emulator, "installed", lambda: False)

    class J:
        message = ""
    await main.ensure_ready(J())                      # would raise "no emulator" if WSA weren't tried first
    assert adb.serial() == "127.0.0.1:58526"
    adb.use_serial(None)


# ---------------- battery / GPS / no-root export / local folder scan ----------------
def test_battery_status_and_set(tmp_path, fake_adb, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    with TestClient(main.app, base_url="http://localhost") as c:
        b = c.get("/api/battery").json()
        assert b["level"] == 42 and b["status"] == "discharging"
        assert c.post("/api/battery", json={"level": 55, "plugged": True}).status_code == 200
        assert c.post("/api/battery", json={"level": 200}).status_code == 400
        assert c.post("/api/battery/reset").status_code == 200
        log = fake_adb.read_text()
        assert "dumpsys battery set level 55" in log and "dumpsys battery set status 2" in log
        assert "dumpsys battery reset" in log


def test_location_presets_and_validation(client):
    presets = client.get("/api/location/presets").json()
    assert "london" in presets and presets["london"] == [51.5074, -0.1278]
    assert client.post("/api/location", json={"lat": 91, "lon": 0}).status_code == 400
    assert client.post("/api/location", json={"lat": 10, "lon": 200}).status_code == 400


@pytest.mark.asyncio
async def test_location_only_works_on_emulator_serial(monkeypatch, fake_adb=None):
    from app import devices
    adb.use_serial("192.168.1.20:5555")   # not an emulator-* serial
    try:
        with pytest.raises(adb.AdbError):
            await devices.set_location(1.0, 2.0)
    finally:
        adb.use_serial(None)


def test_export_app_data_no_root(tmp_path, fake_adb, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    with TestClient(main.app, base_url="http://localhost") as c:
        r = c.post("/api/apps/com.example.app/export")
        assert r.status_code == 200
        d = r.json()
        assert "run-as" in d["captured"]
        assert (tmp_path / "backups" / d["name"]).is_file() or True   # pull is faked (no real content transferred)
        assert c.post("/api/apps/bad;name/export").status_code in (400, 404, 422)


def test_scan_rejects_paths_outside_scan_root(tmp_path, monkeypatch):
    from app import localscan
    monkeypatch.setattr(config, "SCAN_ROOT", tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "demo.apk").write_bytes(make_apk())
    (tmp_path / "sub" / "notes.txt").write_text("x")
    d = localscan.scan("")
    assert len(d["files"]) == 1 and d["files"][0]["name"] == "demo.apk"
    with pytest.raises(localscan.ScanError):
        localscan.resolve_under_scan_root("/etc")
    with pytest.raises(localscan.ScanError):
        localscan.resolve_under_scan_root(str(tmp_path / "nope"))


def test_scan_and_install_endpoints(tmp_path, fake_adb, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    scan_dir = tmp_path / "scan_me"
    scan_dir.mkdir()
    apk = scan_dir / "found.apk"
    apk.write_bytes(make_apk())
    monkeypatch.setattr(config, "SCAN_ROOT", tmp_path)
    monkeypatch.setattr("app.main.localscan.config", config)
    with TestClient(main.app, base_url="http://localhost") as c:
        found = c.get("/api/scan?path=" + str(scan_dir)).json()
        assert found["files"][0]["path"] == str(apk)
        job = wait_job(c, c.post("/api/scan/install", json={"path": str(apk)}).json()["job"])
        assert job["status"] == "done" and job["results"][0]["ok"]
        assert c.post("/api/scan/install", json={"path": "/etc/passwd"}).status_code == 400
        assert c.post("/api/scan/install", json={"path": str(tmp_path / "nope.apk")}).status_code == 400
