"""Example plugin: a private/company APK catalog.

Set CUSTOM_REPO_URL to a JSON file shaped like:
  [{"package": "com.acme.app", "name": "Acme", "url": "https://.../acme.apk",
    "sha256": "<hex>", "versionCode": 12, "versionName": "1.2", "icon": "", "summary": ""}]
Internal (non-public) hosts additionally need ALLOW_PRIVATE_URLS=1.
"""
import os

from app.providers import AppInfo, Provider, Resolved

URL = os.getenv("CUSTOM_REPO_URL", "")


class CustomRepo(Provider):
    name = "custom"

    async def _catalog(self, client):
        r = await client.get(URL)
        r.raise_for_status()
        return r.json()

    async def search(self, client, q):
        q = q.lower()
        return [AppInfo(self.name, a["package"], a.get("name", a["package"]),
                        a.get("summary", ""), a.get("icon", ""), a.get("versionName", ""))
                for a in await self._catalog(client)
                if q in a["package"].lower() or q in a.get("name", "").lower()]

    async def resolve(self, client, app_id):
        for a in await self._catalog(client):
            if a["package"] == app_id:
                url = a["url"]
                ext = next((e for e in (".xapk", ".apks", ".aab") if url.lower().endswith(e)), ".apk")
                return Resolved(url, app_id, ext, sha256=a.get("sha256", ""),
                                version_code=int(a.get("versionCode", 0)),
                                version_name=a.get("versionName", ""))
        raise ValueError("not in catalog")


PROVIDERS = [CustomRepo()] if URL else []
