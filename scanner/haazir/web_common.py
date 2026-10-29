"""Shared plumbing for the web loaders (web_*.py): items of reports/Library storage plan.md read from official public
websites. Each loader keeps BOTH the file as published (PDF/HTML/DOC) and a gzip JSON-lines text file (one row per
document: title, number, date, issuing body, category, extracted text, source URL), and lists every file it writes in
Supabase library_files (lawlib.record), so runs can be repeated: a document whose row is already in its text file is
skipped.

Politeness: one request at a time per site with a pause, robots.txt honoured where it disallows, retries with backoff.
A site that answers 403, asks for a captcha or a login, or cannot be reached is noted and left alone.
Scan-only PDFs are kept with empty text and scan_only = true: never OCR-guessed.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .db import DB
from .storage import B2, Drive, pack, unpack

PDFTOTEXT = r"C:\Program Files\Git\mingw64\bin\pdftotext.exe"
if not os.path.exists(PDFTOTEXT):
    PDFTOTEXT = "pdftotext"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HaazirLibrary/1.0 (+https://haazir-pi.vercel.app)"
TYPES = {".pdf": "application/pdf", ".html": "text/html", ".htm": "text/html", ".doc": "application/msword",
         ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".rtf": "application/rtf",
         ".xls": "application/vnd.ms-excel", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         ".odt": "application/vnd.oasis.opendocument.text", ".txt": "text/plain", ".zip": "application/zip"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Blocked(Exception):
    """The site refused us (403, captcha, login), robots.txt disallows the path, or it could not be reached."""


# ---------------------------------------------------------------- fetching, politely


class Site:
    """One site, one request at a time, with a pause between requests and robots.txt honoured."""

    def __init__(self, pause: float = 1.5, verify: bool = True, timeout: float = 120, robots: bool = True,
                 headers: dict | None = None) -> None:
        self.pause = pause
        self.http = httpx.Client(follow_redirects=True, verify=verify, timeout=timeout,
                                 headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9", **(headers or {})})
        self.robots_on = robots
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last: dict[str, float] = {}

    def allowed(self, url: str) -> bool:
        if not self.robots_on:
            return True
        host = urlparse(url)
        key = f"{host.scheme}://{host.netloc}"
        if key not in self._robots:
            rp = None
            try:
                self._wait(host.netloc)
                r = self.http.get(f"{key}/robots.txt", timeout=30)
                body = r.text if r.status_code == 200 else ""
                # Many government sites answer robots.txt with their HTML error page: that is not a robots file.
                if body and "<html" not in body[:500].lower() and re.search(r"(?im)^\s*(user-agent|disallow)\s*:", body):
                    rp = RobotFileParser()
                    rp.parse(body.splitlines())
            except httpx.HTTPError:
                rp = None
            self._robots[key] = rp
        rp = self._robots[key]
        return True if rp is None else rp.can_fetch(UA, url) and rp.can_fetch("*", url)

    def _wait(self, netloc: str) -> None:
        last = self._last.get(netloc, 0)
        gap = time.time() - last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last[netloc] = time.time()

    def get(self, url: str, method: str = "GET", tries: int = 5, **kw) -> httpx.Response:
        if not self.allowed(url):
            raise Blocked(f"robots.txt disallows {url}")
        err = ""
        for attempt in range(tries):
            self._wait(urlparse(url).netloc)
            try:
                r = self.http.request(method, url, **kw)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                err = f"{type(e).__name__}: {e}"
                time.sleep(min(60, 3 * 2 ** attempt))
                continue
            if r.status_code == 403:
                raise Blocked(f"403 at {url}")
            if r.status_code in (429, 500, 502, 503, 504, 520, 521, 522, 524):
                err = f"HTTP {r.status_code}"
                time.sleep(min(120, 5 * 2 ** attempt))
                continue
            return r
        raise Blocked(f"unreachable ({err}) {url}")


# ---------------------------------------------------------------- text out of files


def pdf_text(data: bytes) -> dict:
    """{text, pages, scan_only}. A PDF with almost no text layer is scan-only: kept with empty text, never OCR'd."""
    with tempfile.TemporaryDirectory() as d:
        src, out = os.path.join(d, "a.pdf"), os.path.join(d, "a.txt")
        with open(src, "wb") as f:
            f.write(data)
        try:
            subprocess.run([PDFTOTEXT, "-enc", "UTF-8", src, out], check=True, capture_output=True, timeout=600)
            with open(out, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return {"text": "", "pages": None, "scan_only": False, "unreadable": True}
    pages = max(1, text.count("\f"))
    text = re.sub(r"[ \t]+\n", "\n", text).replace("\f", "\n\f\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    visible = len(re.sub(r"\s", "", text))
    scan = visible < 60 * pages
    return {"text": "" if scan else text, "pages": pages, "scan_only": scan}


def html_text(html: str | bytes, keep: str | None = None) -> str:
    """The readable text of a page: scripts, styles, menus, headers and footers dropped. keep = a CSS selector for
    the main content where the page has one."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    # Not <form>: ASP.NET pages (RBI) wrap the whole page in one.
    for t in soup(["script", "style", "noscript", "nav", "header", "footer", "iframe", "svg"]):
        t.decompose()
    node = soup.select_one(keep) if keep else None
    text = (node or soup.body or soup).get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def docx_text(data: bytes) -> str:
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError):
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml).strip()


def text_of(data: bytes, ext: str) -> dict:
    ext = ext.lower()
    if ext == ".pdf" or data[:5] == b"%PDF-":
        return pdf_text(data)
    if ext in (".html", ".htm"):
        return {"text": html_text(data), "pages": None, "scan_only": False}
    if ext == ".docx":
        return {"text": docx_text(data), "pages": None, "scan_only": False}
    if ext == ".txt":
        return {"text": data.decode("utf-8", "replace"), "pages": None, "scan_only": False}
    return {"text": "", "pages": None, "scan_only": False, "no_text_format": ext}


def ext_of(url: str, content_type: str = "", data: bytes = b"") -> str:
    if data[:5] == b"%PDF-":
        return ".pdf"
    path = unquote(urlparse(url).path).lower()
    m = re.search(r"\.(pdf|html?|docx?|rtf|xlsx?|odt|txt|zip)$", path)
    if m:
        return "." + m.group(1)
    ct = content_type.split(";")[0].strip().lower()
    for e, t in TYPES.items():
        if t == ct:
            return e
    if data[:2] == b"PK":
        return ".docx" if b"word/" in data[:2000] else ".zip"
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return ".doc"
    return ".html" if b"<html" in data[:2000].lower() else ".bin"


def slug(s: str, n: int = 70) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-").lower()
    return s[:n].strip("-") or "doc"


def file_name(title: str, url: str, ext: str) -> str:
    """A stable, readable file name: a slug of the title plus a short hash of the source URL (titles repeat)."""
    return f"{slug(title)}-{hashlib.sha1(url.encode()).hexdigest()[:8]}{ext}"


def absolute(base: str, href: str) -> str:
    return urljoin(base, href.strip().replace(" ", "%20").replace("\\", "/"))


DATE_FORMATS = ("%d %b, %Y", "%d %b %Y", "%d %B %Y", "%d %B, %Y", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d",
                "%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%d-%B-%Y", "%d %b. %Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y")


def iso_date(s: str | None) -> str | None:
    """A date as the page prints it -> 'YYYY-MM-DD'; None when it does not read as a date (never guessed)."""
    s = re.sub(r"\s+", " ", (s or "").strip()).replace("Sept", "Sep")
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- the library: files written + listed


class Library:
    """Writes files to Backblaze ('b2') or Google Drive ('drive') and lists each in library_files."""

    def __init__(self, item: int, source: str) -> None:
        self.item, self.source = item, source
        self.db = DB()
        self._b2: B2 | None = None
        self._drive: Drive | None = None

    @property
    def b2(self) -> B2:
        if self._b2 is None:
            self._b2 = B2()
        return self._b2

    @property
    def drive(self) -> Drive:
        if self._drive is None:
            self._drive = Drive()
        return self._drive

    def store(self, name: str):
        return self.b2 if name == "b2" else self.drive

    def known(self, prefix: str) -> dict[str, int]:
        """Paths already listed under this prefix -> bytes."""
        rows = self.db.select("library_files", f"select=path,bytes&path=like.{prefix}*&order=path")
        return {r["path"]: r["bytes"] for r in rows}

    def put(self, store: str, path: str, data: bytes, kind: str, content_type: str | None = None, **kw) -> None:
        from .lawlib import record  # imported here: lawlib is being edited by others, so keep the coupling thin
        ct = content_type or TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        if path.endswith(".gz"):
            ct = "application/gzip"
        for attempt in range(4):
            try:
                self.store(store).put(path, data, ct)
                break
            except (httpx.HTTPError, RuntimeError):
                if attempt == 3:
                    raise
                time.sleep(10 * 2 ** attempt)
        kw.setdefault("source", self.source)
        for attempt in range(4):
            try:
                record(self.db, path, store, kind, self.item, data, **kw)
                return
            except (httpx.HTTPError, RuntimeError):
                if attempt == 3:
                    raise
                time.sleep(5 * 2 ** attempt)

    def get(self, store: str, path: str) -> bytes | None:
        return self.store(store).get(path)


class Group:
    """One gzip JSON-lines text file (rows keyed by source URL), read back on start so a run can resume."""

    def __init__(self, lib: Library, store: str, path: str, kind: str, title: str, **meta) -> None:
        self.lib, self.store, self.path, self.kind, self.title, self.meta = lib, store, path, kind, title, meta
        self.rows: dict[str, dict] = {}
        self.dirty = 0
        data = None
        try:
            data = lib.get(store, path)
        except (httpx.HTTPError, RuntimeError):
            data = None
        if data:
            for r in unpack(data):
                self.rows[r.get("source_url") or r.get("url")] = r

    def has(self, url: str) -> bool:
        return url in self.rows

    def add(self, row: dict) -> None:
        self.rows[row.get("source_url") or row.get("url")] = row
        self.dirty += 1

    def save(self, force: bool = False) -> None:
        if not self.dirty and not force:
            return
        rows = sorted(self.rows.values(), key=lambda r: (r.get("date") or "", r.get("title") or ""))
        data = pack(rows)
        self.lib.put(self.store, self.path, data, self.kind, title=self.title, rows=len(rows), **self.meta)
        self.dirty = 0


def fetch_doc(site: Site, url: str, max_bytes: int = 400_000_000) -> tuple[bytes, str, str]:
    """(data, ext, final url) for one published file."""
    r = site.get(url)
    if r.status_code != 200:
        raise Blocked(f"HTTP {r.status_code} at {url}")
    data = r.content
    if len(data) > max_bytes:
        raise Blocked(f"too large ({len(data)} bytes) {url}")
    low = data[:3000].lower()
    if b"captcha" in low and b"<html" in low:
        raise Blocked(f"captcha page at {url}")
    if urlparse(str(r.url)).path.lower().endswith(".pdf") and data[:1024].find(b"%PDF-") < 0:
        raise Blocked(f"not a PDF (a challenge or error page) at {url}")
    return data, ext_of(str(r.url), r.headers.get("content-type", ""), data), str(r.url)


def doc_row(meta: dict, data: bytes, ext: str, url: str, stored: str, store: str) -> dict:
    """The text row for one document: meta (title, number, date, body, category …) + its text."""
    t = text_of(data, ext)
    row = {"title": meta.get("title"), "number": meta.get("number"), "date": meta.get("date"), "body": meta.get("body"),
           "category": meta.get("category"), **{k: v for k, v in meta.items() if k not in ("title", "number", "date", "body", "category")},
           "text": t["text"], "pages": t.get("pages"), "scan_only": t.get("scan_only", False),
           "source_url": url, "file": stored, "store": store, "bytes": len(data),
           "sha1": hashlib.sha1(data).hexdigest(), "read_at": now()}
    for k in ("unreadable", "no_text_format"):
        if t.get(k):
            row[k] = t[k]
    return row


def log_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def run_docs(lib: Library, site: Site, docs: list[dict], *, store: str, folder: str, group_of, group_meta, kind: str,
             text_kind: str, save_every: int = 25, limit: int | None = None, workers: int = 1) -> dict:
    """The common loop: for each listed document (dict with url, title, …) not already in its text file, download the
    original, store it under folder, extract its text and add the row to its group's text file.
    group_of(doc) -> (store, path, title); group_meta(doc) -> extra library_files fields for that text file.
    Downloads stay one at a time (the source site's pace); with workers > 1, reading the text and writing to our own
    stores run beside them. Returns counts and the failures (url, reason)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    groups: dict[str, Group] = {}
    lock = threading.Lock()
    local = threading.local()
    counts = {"new": 0, "failed": 0, "already": 0}
    fails: list[dict] = []

    def worker_lib() -> Library:
        if workers == 1:
            return lib
        if not hasattr(local, "lib"):
            local.lib = Library(lib.item, lib.source)
            local.lib._drive = None
        return local.lib

    def fail(d: dict, why: str) -> None:
        with lock:
            counts["failed"] += 1
            fails.append({"url": d["url"], "title": d.get("title"), "why": why[:300]})
        print(f"    failed: {why[:200]}", flush=True)

    def finish(d: dict, g: Group, data: bytes, ext: str, path: str) -> None:
        meta = {k: v for k, v in d.items() if k not in ("url", "subfolder")}
        try:
            row = doc_row(meta, data, ext, d["url"], path, store)
            worker_lib().put(store, path, data, kind, title=(d.get("title") or "")[:300],
                             year=int(d["date"][:4]) if d.get("date") else None, court=d.get("court"))
        except Exception as e:  # noqa: BLE001 - a store hiccup must not end a long run; the doc is retried next run
            fail(d, f"store: {e}")
            return
        with lock:
            g.add(row)
            counts["new"] += 1
            if g.dirty >= save_every:
                g.save()

    pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    pending = []
    for n, d in enumerate(docs, 1):
        gstore, gpath, gtitle = group_of(d)
        with lock:
            g = groups.get(gpath)
            if g is None:
                g = groups[gpath] = Group(lib, gstore, gpath, text_kind, gtitle, **group_meta(d))
            seen = g.has(d["url"])
        if seen:
            counts["already"] += 1
            continue
        if limit is not None and n - counts["already"] > limit:
            break
        try:
            data, ext, _ = fetch_doc(site, d["url"])
        except Blocked as e:
            fail(d, str(e))
            continue
        sub = d.get("subfolder")
        fdir = f"{folder}/{sub}" if sub else folder
        path = f"{fdir}/{file_name(d.get('number') or d.get('title') or '', d['url'], ext)}"
        if pool:
            if store == "drive":
                lib.drive.folder(fdir)  # made here, once, so the workers only ever find it
            pending.append(pool.submit(finish, d, g, data, ext, path))
            while sum(1 for p in pending if not p.done()) > workers * 3:
                time.sleep(0.2)
            pending = [p for p in pending if not p.done()]
        else:
            finish(d, g, data, ext, path)
        if n % 50 == 0:
            print(f"    {n}/{len(docs)} (new {counts['new']}, already {counts['already']}, failed {counts['failed']})", flush=True)
    if pool:
        pool.shutdown(wait=True)
    for g in groups.values():
        g.save()
    return {"listed": len(docs), **counts, "fails": fails,
            "stored_rows": sum(len(g.rows) for g in groups.values()),
            "scan_only": sum(1 for g in groups.values() for r in g.rows.values() if r.get("scan_only"))}


def index_file(lib: Library, store: str, path: str, docs: list[dict], result: dict, title: str, kind: str) -> None:
    """What the source listed, and how each fared: the record that a load was complete (or what it missed)."""
    failed = {f["url"]: f["why"] for f in result.get("fails", [])}
    rows = [{**{k: v for k, v in d.items() if k != "subfolder"}, "status": "failed: " + failed[d["url"]] if d["url"] in failed else "ok"} for d in docs]
    data = pack([{"listed_at": now(), "listed": len(docs), "result": {k: v for k, v in result.items() if k != "fails"}}] + rows)
    lib.put(store, path, data, kind, title=title, rows=len(docs))
