import pytest
from fastapi.testclient import TestClient

from app import adb, config, main
from app.providers import check_public_url


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(main.config, "DOWNLOAD_DIR", tmp_path)
    state = {"pkgs": set(), "calls": []}

    async def pkgs():
        return set(state["pkgs"])

    async def install(path):
        state["calls"].append(("install", path))
        state["pkgs"].add("com.example.app%d" % len(state["calls"]))
        return "Success"

    async def install_multiple(paths):
        state["calls"].append(("multi", paths))
        state["pkgs"].add("com.example.split")
        return "Success"

    monkeypatch.setattr(adb, "third_party_packages", pkgs)
    monkeypatch.setattr(adb, "install", install)
    monkeypatch.setattr(adb, "install_multiple", install_multiple)
    c = TestClient(main.app)
    c.state = state
    return c


def upload(c, n, split):
    files = [("files", (f"a{i}.apk", b"x", "application/vnd.android.package-archive")) for i in range(n)]
    return c.post("/api/upload", files=files, data={"split": str(split).lower()})


def test_upload_multiple_separate(client):
    r = upload(client, 3, False).json()["results"]
    assert len(r) == 3 and all(x["ok"] for x in r)
    assert [c[0] for c in client.state["calls"]] == ["install"] * 3


def test_upload_split_uses_install_multiple(client):
    r = upload(client, 3, True).json()["results"]
    assert len(r) == 1 and r[0]["package"] == "com.example.split"
    assert client.state["calls"][0][0] == "multi"


def test_rejects_non_apk(client):
    r = client.post("/api/upload", files=[("files", ("x.exe", b"x"))])
    assert r.status_code == 400


def test_library_list_and_delete(client):
    upload(client, 1, False)
    lib = client.get("/api/apks").json()
    assert len(lib) == 1
    assert client.get(f"/api/apks/{lib[0]['id']}/{lib[0]['files'][0]['name']}").status_code == 200
    assert client.delete(f"/api/apks/{lib[0]['id']}").status_code == 200
    assert client.get("/api/apks").json() == []
    assert client.delete("/api/apks/..").status_code in (404, 405)


def test_ssrf_blocked():
    for u in ("http://127.0.0.1/a.apk", "file:///etc/passwd", "http://10.0.0.1/x"):
        with pytest.raises(ValueError):
            check_public_url(u)


def test_package_validation():
    assert adb.valid_package("com.example.app")
    assert not adb.valid_package("com.x; rm -rf /")
