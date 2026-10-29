"""Where the library and the backups live (Phase 6, agreed 26 Oct 2026; reports/Library storage plan.md):

  Backblaze B2 (bucket B2_BUCKET, 10 GB)  core law every draft reads: acts, criminal-law tables, rules, formats,
                                          court rules, Supreme Court and High Court summaries
  Google Drive (the founder's 5 TB)       the wide library: district summaries, full judgment texts, state acts,
                                          circulars, … and the daily database backups

Both are plain file stores: a file is written whole and read whole by its name. What is where is kept in Supabase
(library_files), so the website and drafting ask the database first, then fetch only the files they need.

    python -m haazir.storage check      # both stores answer: a test file written, read back and removed
"""

import gzip
import hashlib
import json
import sys
import time

import httpx

from . import config

# ---------------------------------------------------------------- Backblaze B2 (its own API)


class B2:
    def __init__(self) -> None:
        self.key_id = config.env("B2_KEY_ID")
        self.key = config.env("B2_APP_KEY")
        self.bucket = config.env("B2_BUCKET")
        self.http = httpx.Client(timeout=300)
        self._auth: dict | None = None
        self._bucket_id: str | None = None
        self._upload: dict | None = None

    def _authorize(self, again: bool = False) -> dict:
        if self._auth is None or again:
            r = self.http.get("https://api.backblazeb2.com/b2api/v3/b2_authorize_account", auth=(self.key_id, self.key))
            r.raise_for_status()
            self._auth = r.json()
            self._upload = None
        return self._auth

    def _api(self, method: str, body: dict) -> dict:
        for attempt in range(4):
            a = self._authorize(again=attempt > 0)
            r = self.http.post(f"{a['apiInfo']['storageApi']['apiUrl']}/b2api/v3/{method}", json=body, headers={"Authorization": a["authorizationToken"]})
            if r.status_code == 401:
                continue
            if r.status_code in (408, 429, 500, 503):
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Backblaze {method} kept failing")

    def bucket_id(self) -> str:
        if self._bucket_id is None:
            a = self._authorize()
            r = self.http.get(f"{a['apiInfo']['storageApi']['apiUrl']}/b2api/v3/b2_list_buckets",
                              params={"accountId": a["accountId"], "bucketName": self.bucket}, headers={"Authorization": a["authorizationToken"]})
            r.raise_for_status()
            self._bucket_id = r.json()["buckets"][0]["bucketId"]
        return self._bucket_id

    def put(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> dict:
        """Write (or replace) one file. Files here stay under 100 MB each (the library is split into parts)."""
        sha = hashlib.sha1(data).hexdigest()
        for attempt in range(5):
            if self._upload is None:
                self._upload = self._api("b2_get_upload_url", {"bucketId": self.bucket_id()})
            try:
                r = self.http.post(self._upload["uploadUrl"], content=data, headers={
                    "Authorization": self._upload["authorizationToken"], "X-Bz-File-Name": httpx.URL("/" + name).raw_path.decode()[1:],
                    "Content-Type": content_type, "X-Bz-Content-Sha1": sha})
            except httpx.HTTPError:
                self._upload = None
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return r.json()
            self._upload = None  # a busy upload point: ask for another one
            time.sleep(2 ** attempt)
        raise RuntimeError(f"Backblaze upload of {name} kept failing")

    def get(self, name: str) -> bytes | None:
        for attempt in range(4):
            a = self._authorize(again=attempt > 0)
            r = self.http.get(f"{a['apiInfo']['storageApi']['downloadUrl']}/file/{self.bucket}/{name}", headers={"Authorization": a["authorizationToken"]})
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                return r.content
            time.sleep(2 ** attempt)
        raise RuntimeError(f"Backblaze download of {name} kept failing")

    def names(self, prefix: str) -> list[dict]:
        out, start = [], None
        while True:
            body = {"bucketId": self.bucket_id(), "prefix": prefix, "maxFileCount": 1000}
            if start:
                body["startFileName"] = start
            page = self._api("b2_list_file_names", body)
            out += [{"name": f["fileName"], "size": f["contentLength"], "id": f["fileId"]} for f in page["files"]]
            start = page.get("nextFileName")
            if not start:
                return out

    def delete(self, name: str) -> None:
        for f in self.names(name):
            if f["name"] == name:
                self._api("b2_delete_file_version", {"fileName": name, "fileId": f["id"]})


# ---------------------------------------------------------------- Google Drive (the founder's account)

DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
FOLDER = "application/vnd.google-apps.folder"


class Drive:
    """Files under the 'Haazir' folder of the founder's Drive (scope drive.file: Haazir sees only what it made).
    A name with slashes is a path of folders: 'backups/daily/2026-10-26.sql.gz'."""

    def __init__(self) -> None:
        self.client_id = config.env("GDRIVE_CLIENT_ID")
        self.secret = config.env("GDRIVE_CLIENT_SECRET")
        self.refresh = config.env("GDRIVE_REFRESH_TOKEN")
        self.root = config.env("GDRIVE_FOLDER_ID")
        self.http = httpx.Client(timeout=600)
        self._token: tuple[str, float] | None = None
        self._folders: dict[str, str] = {"": self.root}

    def _headers(self) -> dict:
        if self._token is None or self._token[1] < time.time() + 60:
            r = self.http.post("https://oauth2.googleapis.com/token", data={"client_id": self.client_id, "client_secret": self.secret,
                                                                              "refresh_token": self.refresh, "grant_type": "refresh_token"})
            r.raise_for_status()
            j = r.json()
            self._token = (j["access_token"], time.time() + int(j.get("expires_in", 3600)))
        return {"Authorization": f"Bearer {self._token[0]}"}

    def _call(self, method: str, url: str, **kw) -> httpx.Response:
        for attempt in range(6):
            r = self.http.request(method, url, headers={**self._headers(), **kw.pop("headers", {})}, **kw)
            if r.status_code in (429, 500, 502, 503) or (r.status_code == 403 and "rateLimit" in r.text):
                time.sleep(2 ** attempt)
                continue
            return r
        return r

    def _child(self, parent: str, name: str, folder: bool) -> str | None:
        q = f"name = '{name.replace(chr(39), chr(92) + chr(39))}' and '{parent}' in parents and trashed = false"
        q += f" and mimeType {'=' if folder else '!='} '{FOLDER}'"
        r = self._call("GET", f"{DRIVE}/files", params={"q": q, "fields": "files(id)", "pageSize": 1})
        r.raise_for_status()
        files = r.json()["files"]
        return files[0]["id"] if files else None

    def folder(self, path: str) -> str:
        """The folder at this path under Haazir, made if missing."""
        path = path.strip("/")
        if path in self._folders:
            return self._folders[path]
        parent_path, _, name = path.rpartition("/")
        parent = self.folder(parent_path)
        fid = self._child(parent, name, folder=True)
        if not fid:
            r = self._call("POST", f"{DRIVE}/files", json={"name": name, "mimeType": FOLDER, "parents": [parent]}, params={"fields": "id"})
            r.raise_for_status()
            fid = r.json()["id"]
        self._folders[path] = fid
        return fid

    def put(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Write (or replace) one file; returns its Drive id. Resumable upload, so big files are fine."""
        folder_path, _, fname = name.strip("/").rpartition("/")
        parent = self.folder(folder_path)
        existing = self._child(parent, fname, folder=False)
        meta = {"name": fname} if existing else {"name": fname, "parents": [parent]}
        url = f"{UPLOAD}/files/{existing}" if existing else f"{UPLOAD}/files"
        r = self._call("PATCH" if existing else "POST", url, params={"uploadType": "resumable", "fields": "id"}, json=meta,
                       headers={"X-Upload-Content-Type": content_type, "X-Upload-Content-Length": str(len(data))})
        r.raise_for_status()
        session = r.headers["Location"]
        for attempt in range(5):
            u = self.http.put(session, content=data, headers={"Content-Type": content_type})
            if u.status_code in (200, 201):
                return u.json()["id"]
            time.sleep(2 ** attempt)
        raise RuntimeError(f"Drive upload of {name} failed: {u.status_code} {u.text[:200]}")

    def file_id(self, name: str) -> str | None:
        folder_path, _, fname = name.strip("/").rpartition("/")
        try:
            parent = self.folder(folder_path) if folder_path in self._folders else self._find_folder(folder_path)
        except LookupError:
            return None
        return self._child(parent, fname, folder=False)

    def _find_folder(self, path: str) -> str:
        fid = self.root
        for part in [p for p in path.split("/") if p]:
            nxt = self._child(fid, part, folder=True)
            if not nxt:
                raise LookupError(path)
            fid = nxt
        self._folders[path] = fid
        return fid

    def get(self, name: str) -> bytes | None:
        fid = self.file_id(name)
        if not fid:
            return None
        r = self._call("GET", f"{DRIVE}/files/{fid}", params={"alt": "media"})
        r.raise_for_status()
        return r.content

    def delete(self, name: str) -> None:
        fid = self.file_id(name)
        if fid:
            self._call("DELETE", f"{DRIVE}/files/{fid}")

    def names(self, folder_path: str) -> list[dict]:
        try:
            parent = self._find_folder(folder_path) if folder_path.strip("/") else self.root
        except LookupError:
            return []
        out, token = [], None
        while True:
            params = {"q": f"'{parent}' in parents and trashed = false", "fields": "nextPageToken, files(id, name, size, mimeType)", "pageSize": 1000}
            if token:
                params["pageToken"] = token
            r = self._call("GET", f"{DRIVE}/files", params=params)
            r.raise_for_status()
            j = r.json()
            out += j["files"]
            token = j.get("nextPageToken")
            if not token:
                return out

    def space(self) -> tuple[int, int]:
        r = self._call("GET", f"{DRIVE}/about", params={"fields": "storageQuota"})
        r.raise_for_status()
        q = r.json()["storageQuota"]
        return int(q["usage"]), int(q.get("limit") or 0)


# ---------------------------------------------------------------- helpers for library files


def pack(rows: list[dict]) -> bytes:
    """Rows as gzip'd JSON lines: small, and readable by both the scanner and the website."""
    return gzip.compress("\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows).encode(), 9)


def unpack(data: bytes) -> list[dict]:
    # Split on "\n" only: str.splitlines() also breaks at U+2028, U+0085 … which JSON leaves unescaped inside text.
    return [json.loads(line) for line in gzip.decompress(data).decode().split("\n") if line.strip()]


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        stamp = str(time.time())
        b2 = B2()
        b2.put("test/check.txt", stamp.encode(), "text/plain")
        ok_b2 = b2.get("test/check.txt") == stamp.encode()
        b2.delete("test/check.txt")
        d = Drive()
        d.put("test/check.txt", stamp.encode(), "text/plain")
        ok_d = d.get("test/check.txt") == stamp.encode()
        d.delete("test/check.txt")
        used, limit = d.space()
        print(f"Backblaze: {'ok' if ok_b2 else 'FAILED'} · Google Drive: {'ok' if ok_d else 'FAILED'} ({used / 1e9:.1f} GB of {limit / 1e12:.1f} TB used)")
        return 0 if ok_b2 and ok_d else 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
