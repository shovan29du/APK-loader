"""APK marketplaces. Each provider can search and resolve a direct APK URL.

Only sources with public, permitted APIs are included (F-Droid, Aptoide) plus
arbitrary direct URLs. Google Play / APKMirror / APKPure need auth or forbid
scraping, so they are deliberately not implemented; add a Provider subclass
if you have rights to use one.
"""
import ipaddress
import re
import socket
from dataclasses import dataclass, asdict
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx

from . import config

TIMEOUT = httpx.Timeout(30.0, connect=10.0)
HEADERS = {"User-Agent": "apk-loader/1.0"}


@dataclass
class AppInfo:
    provider: str
    id: str  # package name (or URL for direct)
    name: str
    summary: str = ""
    icon: str = ""
    version: str = ""

    def dict(self):
        return asdict(self)


def check_public_url(url: str) -> None:
    """SSRF guard: http(s) only, and the host must not resolve to a private address."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("only http(s) URLs are allowed")
    if config.ALLOW_PRIVATE_URLS:
        return
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror:
        raise ValueError("cannot resolve host")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("URL resolves to a non-public address")


@dataclass
class Resolved:
    url: str
    package: str = ""
    ext: str = ".apk"
    sha256: str = ""
    md5: str = ""
    version_code: int = 0
    version_name: str = ""


class Provider:
    name = ""

    async def search(self, client: httpx.AsyncClient, q: str) -> list[AppInfo]:
        raise NotImplementedError

    async def resolve(self, client: httpx.AsyncClient, app_id: str) -> "Resolved":
        raise NotImplementedError


class FDroid(Provider):
    name = "fdroid"

    async def search(self, client, q):
        r = await client.get("https://search.f-droid.org/api/search_apps",
                             params={"q": q, "lang": "en"})
        r.raise_for_status()
        out = []
        for a in r.json().get("apps", [])[:20]:
            pkg = a.get("url", "").rstrip("/").rsplit("/", 1)[-1]
            if pkg:
                out.append(AppInfo(self.name, pkg, a.get("name", pkg),
                                   a.get("summary", ""), a.get("icon", "")))
        return out

    async def resolve(self, client, app_id):
        r = await client.get(f"https://f-droid.org/api/v1/packages/{app_id}")
        r.raise_for_status()
        data = r.json()
        code = data.get("suggestedVersionCode")
        if not code:
            raise ValueError("no version available")
        name = next((v.get("versionName", "") for v in data.get("packages", [])
                     if v.get("versionCode") == code), "")
        return Resolved(f"https://f-droid.org/repo/{app_id}_{code}.apk", app_id,
                        version_code=int(code), version_name=name)


class Aptoide(Provider):
    name = "aptoide"

    async def search(self, client, q):
        r = await client.get("https://ws75.aptoide.com/api/7/apps/search",
                             params={"query": q, "limit": 20})
        r.raise_for_status()
        return [AppInfo(self.name, a["package"], a.get("name", a["package"]),
                        (a.get("developer") or {}).get("name", ""),
                        a.get("icon", ""), (a.get("file") or {}).get("vername", ""))
                for a in r.json().get("datalist", {}).get("list", [])]

    async def resolve(self, client, app_id):
        r = await client.get("https://ws75.aptoide.com/api/7/app/get",
                             params={"package_name": app_id})
        r.raise_for_status()
        f = r.json().get("nodes", {}).get("meta", {}).get("data", {}).get("file", {})
        if not f.get("path"):
            raise ValueError("no download available")
        return Resolved(f["path"], app_id, md5=f.get("md5sum", ""),
                        version_code=int(f.get("vercode") or 0),
                        version_name=f.get("vername", ""))


class Direct(Provider):
    name = "direct"

    async def search(self, client, q):
        return []

    async def resolve(self, client, app_id):
        check_public_url(app_id)
        ext = PurePosixPath(urlparse(app_id).path).suffix.lower()
        return Resolved(app_id, "", ext if ext in ARCHIVE_EXTS else ".apk")


ARCHIVE_EXTS = (".apk", ".xapk", ".apks", ".aab")
PROVIDERS: dict[str, Provider] = {p.name: p for p in (FDroid(), Aptoide(), Direct())}


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:120] or "app.apk"
