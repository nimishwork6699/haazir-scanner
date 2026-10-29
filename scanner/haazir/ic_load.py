"""India Code in bulk (Phase 6 storage plan items 18, 22, 24, 25, 26, 27): every document of a collection, its
original file kept as published and its text read here, grouped into a few files per collection.

One group (e.g. the central Rules of 1994) is written as:
  <text prefix>/<group>.jsonl.gz       one row per document: id (handle), title, number, year, date, ministry,
                                       department, state, collection, parent act, text, source URL, and where its
                                       original file is
  <pdf prefix>/<group>.pdf-<k>.zip     the original files, stored uncompressed (zip "stored"): each row's
                                       files[].zip/offset/size point at the file's bytes inside the zip, so one PDF
                                       is a single ranged read (see original_file)
and each collection gets <text prefix>/index.jsonl.gz: every document without its text, and where it is.

Text is read from the PDF with pdftotext (India Code's own text copies stop at about 100 KB). A PDF with no text
layer (a scanned gazette) keeps its file, with text None and text_status "scan_only": nothing is guessed.

Runs are resumable: a group whose text file is already listed in library_files is skipped. Requests go one at a
time with a pause, and back off on 429/5xx. The collection listings are cached in the temp folder, so a restarted
run sees the same groups. Run through haazir.lawlib (rules, repealed, notifications, constitution-amendments,
hindi-acts, state-acts).
"""

import io
import json
import os
import re
import struct
import subprocess
import tempfile
import time
import zipfile
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import httpx

from .db import DB
from .lawlib import INDIA_CODE, PDFTOTEXT, UA, clean_block, now, record
from .library import _md, _text, clean
from .storage import B2, Drive, pack, unpack

API = "https://indiacode.gov.in/server/api"
SITE = "https://indiacode.gov.in"
PAUSE = 0.25                      # between India Code requests
GROUP_MAX = 300                   # documents per text file before a group is split finer (decade -> year -> month)
ZIP_MAX = 200_000_000             # bytes per zip part
CACHE = Path(tempfile.gettempdir()) / "haazir-indiacode"
DEVANAGARI = re.compile(r"[ऀ-ॿ]")


class Source(Exception):
    pass


# ---------------------------------------------------------------- India Code, politely

class IC:
    def __init__(self) -> None:
        self.http = httpx.Client(follow_redirects=True, headers=UA, timeout=240)
        self._tree: dict[str, dict[str, str]] | None = None

    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        wait = 5
        for attempt in range(8):
            r, err = None, None
            try:
                r = self.http.get(url, params=params)
            except httpx.HTTPError as e:
                err = e
            time.sleep(PAUSE)
            if r is not None and r.status_code < 400:
                return r
            if r is not None and r.status_code in (400, 401, 403, 404, 410):
                raise Source(f"{r.status_code} {url}")
            if r is not None and r.headers.get("Retry-After", "").isdigit():
                wait = max(wait, int(r.headers["Retry-After"]))
            print(f"    India Code {r.status_code if r is not None else type(err).__name__}; waiting {wait}s", flush=True)
            time.sleep(wait)
            wait = min(wait * 2, 600)
        raise Source(f"India Code kept failing: {url}")

    def tree(self) -> dict[str, dict[str, str]]:
        """{community ("CENTRAL", "Maharashtra", …): {collection name: uuid}}"""
        if self._tree is None:
            comms, page = [], 0
            while True:
                j = self.get(f"{API}/core/communities/search/top", {"size": 100, "page": page}).json()
                comms += j["_embedded"]["communities"]
                page += 1
                if page * 100 >= j["page"]["totalElements"]:
                    break
            self._tree = {}
            for cm in comms:
                cols = self.get(f"{API}/core/communities/{cm['uuid']}/collections", {"size": 100}).json()["_embedded"]["collections"]
                self._tree[cm["name"]] = {c["name"]: c["uuid"] for c in cols}
        return self._tree

    def count(self, uuid: str) -> int:
        j = self.get(f"{API}/discover/search/objects", {"scope": uuid, "dsoType": "ITEM", "size": 1}).json()
        return j["_embedded"]["searchResult"]["page"]["totalElements"]

    def listing(self, uuid: str, files: bool = True, fresh: bool = False) -> tuple[int, list[dict]]:
        """Every item of a collection (metadata, and its files when files=True), cached so a restart sees the same
        list. Returns India Code's count and the items."""
        CACHE.mkdir(parents=True, exist_ok=True)
        path = CACHE / f"{uuid}{'-files' if files else ''}.json"
        total = self.count(uuid)
        if path.exists() and not fresh:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached["total"] == total and len(cached["items"]) == total:
                return total, cached["items"]
        items: dict[str, dict] = {}
        size = 50 if files else 100
        for sort in ("dc.title,ASC", "dc.date.accessioned,ASC"):  # paging on a non-unique key can skip: a second pass fills gaps
            page = 0
            while True:
                params = {"scope": uuid, "dsoType": "ITEM", "size": size, "page": page, "sort": sort}
                if files:
                    params["embed"] = "bundles/bitstreams"
                sr = self.get(f"{API}/discover/search/objects", params).json()["_embedded"]["searchResult"]
                for o in sr["_embedded"]["objects"]:
                    it = o["_embedded"]["indexableObject"]
                    items[it["uuid"]] = _slim(it, files)
                page += 1
                if page * size >= sr["page"]["totalElements"] or not sr["_embedded"]["objects"]:
                    break
            if len(items) >= total:
                break
        out = sorted(items.values(), key=lambda i: i["uuid"])
        path.write_text(json.dumps({"total": total, "items": out}, ensure_ascii=False), encoding="utf-8")
        return total, out

    def files(self, uuid: str) -> dict[str, list[dict]]:
        it = self.get(f"{API}/core/items/{uuid}", {"embed": "bundles/bitstreams"}).json()
        return _slim(it, True)["files"]

    def content(self, bitstream: dict) -> bytes:
        # India Code stamps its watermark into a PDF as it serves it, so the size differs from the one it lists:
        # a PDF is checked by its header and its end marker instead.
        for attempt in range(3):
            data = self.get(f"{API}/core/bitstreams/{bitstream['uuid']}/content").content
            if not bitstream["name"].lower().endswith(".pdf") or (data[:4] == b"%PDF" and b"%%EOF" in data[-2048:]):
                return data
            time.sleep(10)
        raise Source(f"{bitstream['name']}: not a whole PDF ({len(data)} bytes, starts {data[:8]!r})")


def _slim(it: dict, files: bool) -> dict:
    out = {"uuid": it["uuid"], "handle": it.get("handle"), "name": it.get("name"),
           "metadata": {k: [v.get("value") for v in vals] for k, vals in it.get("metadata", {}).items()
                        if k not in ("dc.description.provenance",)}}
    if files:
        out["files"] = {}
        for b in it.get("_embedded", {}).get("bundles", {}).get("_embedded", {}).get("bundles", []):
            bs = b.get("_embedded", {}).get("bitstreams", {}).get("_embedded", {}).get("bitstreams", [])
            out["files"][b["name"]] = [{"name": x["name"], "bytes": x.get("sizeBytes"), "uuid": x["uuid"]} for x in bs]
    return out


def md(it: dict, key: str) -> str:
    """One metadata value of a slim item (or a raw API item)."""
    vals = it.get("metadata", {}).get(key) or []
    v = vals[0] if vals else ""
    return (v.get("value") if isinstance(v, dict) else v) or ""


# ---------------------------------------------------------------- a document's row

NUMBER_RX = [
    re.compile(r"\b((?:G\.?\s*S\.?\s*R\.?|S\.?\s*O\.?)\s*\d+\s*(?:\(\s*E\s*\))?)", re.I),           # G.S.R. 116(E), S.O. 6111(E)
    re.compile(r"\b(?:Notification|Circular|Order|Instruction)\s*No\.?\s*:?\s*([\w./()\-]+(?:\s*\([^)]{1,12}\))?)", re.I),
    re.compile(r"\bNo\.\s*([\w./()\-]+)", re.I),
]
REPEAL_TITLE = re.compile(r",?\s*(\d+)\s+of\s+(\d{4})\s*,?\s*\(\s*Rep\.?,?\s*(.*?)\)?\s*$", re.I)


def doc_row(it: dict, collection: str, state: str) -> dict:
    title = clean(it.get("name") or md(it, "dc.title"))
    date = md(it, "dc.date.issued") or md(it, "dc.date.enact_date") or md(it, "dc.date")
    year = int(date[:4]) if re.match(r"(1[6-9]|20)\d\d", date) else None
    number = md(it, "dc.identifier.act_number") if collection in ("Acts", "Act Amendments") else ""
    repealed_by = None
    m = REPEAL_TITLE.search(title)
    if collection == "Repeal Acts" and m:
        number, year, repealed_by = m.group(1), int(m.group(2)), clean(m.group(3)) or None
    if not number:
        for rx in NUMBER_RX:
            n = rx.search(title)
            if n:
                number = clean(n.group(1))
                break
    parent = None
    if md(it, "dc.identifier.act_id") and collection not in ("Acts",):
        parent = {"id": md(it, "dc.identifier.act_id"), "name": clean(md(it, "dc.identifier.act_name")) or None,
                  "number": md(it, "dc.identifier.act_number") or None, "year": md(it, "dc.date.act_year") or None}
    handle = it.get("handle") or ""
    return {"id": handle, "uuid": it["uuid"], "title": title, "number": number or None, "year": year,
            "date": date if re.match(r"\d{4}-\d\d-\d\d$", date) else None,
            "ministry": clean(md(it, "dc.identifier.ministry_name")) or None,
            "department": clean(md(it, "dc.identifier.department_name")) or None,
            "state": state, "collection": collection,
            "act_id": md(it, "dc.identifier.act_id") or None if collection == "Acts" else None,
            "act": parent, "repealed_by": repealed_by,
            "source_url": f"{SITE}/handle/{handle}" if handle else None}


# ---------------------------------------------------------------- reading a file's text

def read_pdf(data: bytes) -> dict:
    """pdftotext on one PDF. text None when the PDF has no text layer (scan_only) or could not be read."""
    with tempfile.TemporaryDirectory() as d:
        src, out = os.path.join(d, "a.pdf"), os.path.join(d, "a.txt")
        with open(src, "wb") as f:
            f.write(data)
        try:
            subprocess.run([PDFTOTEXT, "-enc", "UTF-8", src, out], check=True, capture_output=True, timeout=300)
            raw = open(out, encoding="utf-8", errors="replace").read()
        except (subprocess.SubprocessError, OSError):
            return {"text": None, "text_status": "unreadable", "pages": None, "scan_pages": None}
    pages = [p for p in raw.split("\f")]
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    letters = [sum(ch.isalpha() for ch in p) for p in pages]
    low = sum(1 for n in letters if n < 40)
    if not pages or low == len(pages):
        return {"text": None, "text_status": "scan_only", "pages": len(pages), "scan_pages": len(pages)}
    return {"text": clean_block(raw), "text_status": "ok" if not low else "partly_scanned", "pages": len(pages),
            "scan_pages": low or None}


def hindi_name(name: str) -> bool:
    """India Code names the Hindi copy of an act h<year><no>.pdf, H…, hh…, hH_…, Hi…, HFOOD.pdf, …hi.pdf, hindi…
    (its English copies are a<year>-<no>.pdf)."""
    n = (name or "").strip().lower()
    return n.startswith("h") or "hindi" in n or n.endswith("hi.pdf")


def _bare_number(name: str) -> bool:
    """'199336.pdf' beside 'a1993-36.pdf': one of an act's two copies, named by number only, often the Hindi one."""
    return bool(re.fullmatch(r"\d[\d-]*\.pdf", (name or "").strip().lower()))


# ---------------------------------------------------------------- the stores

class Stores:
    def __init__(self) -> None:
        self._b2: B2 | None = None
        self._drive: Drive | None = None
        self.db = DB()

    def of(self, store: str):
        if store == "b2":
            self._b2 = self._b2 or B2()
            return self._b2
        self._drive = self._drive or Drive()
        return self._drive

    @staticmethod
    def _again(fn, *a):
        """The store clients retry busy answers but not a dropped connection: a long load should not stop on one."""
        for attempt in range(6):
            try:
                return fn(*a)
            except (httpx.HTTPError, RuntimeError) as e:
                if attempt == 5:
                    raise
                print(f"    store: {type(e).__name__}; trying again", flush=True)
                time.sleep(10 * (attempt + 1))

    def put(self, store: str, path: str, data: bytes, ctype: str) -> None:
        self._again(self.of(store).put, path, data, ctype)

    def get(self, store: str, path: str) -> bytes | None:
        return self._again(self.of(store).get, path)

    def done(self, prefix: str) -> dict[str, dict]:
        rows = self.db.select("library_files", f"select=path,store,rows,bytes&path=like.{prefix}*&order=path")
        return {r["path"]: r for r in rows}


def original_file(stores: Stores, f: dict) -> bytes:
    """One original file back from its zip: a ranged read of f["size"] bytes at f["offset"]."""
    lo, hi = f["offset"], f["offset"] + f["size"] - 1
    if f["store"] == "b2":
        b2 = stores.of("b2")
        a = b2._authorize()
        r = b2.http.get(f"{a['apiInfo']['storageApi']['downloadUrl']}/file/{b2.bucket}/{f['zip']}",
                        headers={"Authorization": a["authorizationToken"], "Range": f"bytes={lo}-{hi}"})
    else:
        d = stores.of("drive")
        fid = d.file_id(f["zip"])
        r = d._call("GET", f"https://www.googleapis.com/drive/v3/files/{fid}", params={"alt": "media"}, headers={"Range": f"bytes={lo}-{hi}"})
    r.raise_for_status()
    return r.content


class Zips:
    """Original files of one group, packed into zip parts of at most ZIP_MAX bytes (stored, not compressed)."""

    def __init__(self, stores: Stores, store: str, base: str, item: int, kind: str, title: str) -> None:
        self.stores, self.store, self.base, self.item, self.kind, self.title = stores, store, base, item, kind, title
        self.part, self.buf, self.zf, self.pending, self.written, self.uploads = 0, None, None, [], [], []

    def add(self, member: str, data: bytes, ref: dict) -> None:
        if self.zf is not None and self.buf.tell() + len(data) > ZIP_MAX:
            self.flush()
        if self.zf is None:
            self.part += 1
            self.buf = io.BytesIO()
            self.zf = zipfile.ZipFile(self.buf, "w", zipfile.ZIP_STORED, allowZip64=True)
        if member in self.zf.namelist():
            stem, dot, ext = member.rpartition(".")
            member = f"{stem}-{len(self.pending)}.{ext}" if dot else f"{member}-{len(self.pending)}"
        self.zf.writestr(zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0)), data)
        self.pending.append((self.zf.getinfo(member), ref, data[:5]))

    def flush(self) -> None:
        if self.zf is None:
            return
        self.zf.close()
        blob = self.buf.getvalue()
        path = f"{self.base}.pdf-{self.part}.zip"
        for info, ref, head in self.pending:
            ho = info.header_offset
            n, m = struct.unpack("<HH", blob[ho + 26:ho + 30])
            ref.update({"store": self.store, "zip": path, "offset": ho + 30 + n + m, "size": info.file_size})
            if blob[ref["offset"]:ref["offset"] + 5] != head:
                raise RuntimeError(f"{path}: offset of {info.filename} is wrong")
        self.wait()  # one part uploading at a time, while the next one is fetched
        self.uploads.append(_UPLOAD.submit(self._upload, path, blob, len(self.pending), self.part))
        self.written.append(path)
        self.zf, self.buf, self.pending = None, None, []

    def _upload(self, path: str, blob: bytes, n: int, part: int) -> None:
        self.stores.put(self.store, path, blob, "application/zip")
        record(self.stores.db, path, self.store, f"{self.kind}_pdfs", self.item, blob, title=f"{self.title}: original files (part {part})",
               rows=n, source=INDIA_CODE)

    def wait(self) -> None:
        while self.uploads:
            self.uploads.pop(0).result()


# ---------------------------------------------------------------- groups

def _decade(r: dict) -> str:
    return f"{r['year'] // 10 * 10}s" if r.get("year") else "undated"


def groups(rows: list[tuple[dict, dict]]) -> dict[str, list[tuple[dict, dict]]]:
    """Documents into groups of at most GROUP_MAX: by decade, a big decade by year, a big year by month."""
    out: dict[str, list] = {}
    by_dec: dict[str, list] = {}
    for x in rows:
        by_dec.setdefault(_decade(x[1]), []).append(x)
    for dec, xs in by_dec.items():
        if len(xs) <= GROUP_MAX or dec == "undated":
            out[dec] = xs
            continue
        by_year: dict[str, list] = {}
        for x in xs:
            by_year.setdefault(str(x[1]["year"]), []).append(x)
        for y, ys in by_year.items():
            if len(ys) <= GROUP_MAX:
                out[y] = ys
                continue
            by_month: dict[str, list] = {}
            for x in ys:
                d = x[1].get("date") or ""
                by_month.setdefault(d[:7] if len(d) >= 7 else f"{y}-xx", []).append(x)
            out.update(by_month)
    # Neighbouring small decades share one file ("1920s-1990s"), so a thin collection is not many tiny files.
    merged: dict[str, list] = {}
    run: list[str] = []
    split = {d for d in by_dec if d not in out}  # decades written by year: a merge does not reach across them
    for k in sorted([d for d in out if re.fullmatch(r"\d{4}s", d)] + list(split)) + [None]:
        if k is not None and k not in split and sum(len(out[x]) for x in run) + len(out[k]) <= GROUP_MAX:
            run.append(k)
            continue
        if run:
            merged[run[0] if len(run) == 1 else f"{run[0]}-{run[-1]}"] = [x for r in run for x in out.pop(r)]
        run = [k] if k is not None and k not in split else []
    out.update(merged)
    for k in list(out):  # a still-too-big group is cut into numbered parts, in a fixed order
        xs = sorted(out[k], key=lambda x: x[0]["uuid"])
        if len(xs) > GROUP_MAX:
            del out[k]
            for i in range(0, len(xs), GROUP_MAX):
                out[f"{k}-p{i // GROUP_MAX + 1}"] = xs[i:i + GROUP_MAX]
        else:
            out[k] = xs
    return dict(sorted(out.items()))


def load_collection(ic: IC, stores: Stores, *, community: str, collection: str, item: int, kind: str,
                    text_store: str, text_prefix: str, pdf_store: str, pdf_prefix: str, label: str,
                    pick=None, want=None, post=None, group: str | None = None, fresh: bool = False) -> dict:
    """Every document of one India Code collection (or those pick(item) keeps) into groups. want picks which
    original files to take (default all), post(row) adjusts a row before it is saved, group puts all documents in
    one named group. Returns counts."""
    uuid = ic.tree()[community].get(collection)
    if not uuid:
        print(f"  {label}: no such collection on India Code", flush=True)
        return {"label": label, "source": 0, "taken": 0, "stored": 0}
    total, items = ic.listing(uuid, fresh=fresh)
    if pick:
        items = [it for it in items if pick(it)]
    if not items:
        print(f"  {label}: {total} at source, none taken", flush=True)
        return {"label": label, "source": total, "taken": 0, "stored": 0}
    rows = [(it, doc_row(it, collection, community)) for it in items]
    plan = {group: sorted(rows, key=lambda x: x[0]["uuid"])} if group else groups(rows)
    done = stores.done(text_prefix + "/")
    print(f"  {label}: {total} at source, {len(items)} taken, {len(plan)} groups ({sum(1 for k in plan if f'{text_prefix}/{k}.jsonl.gz' in done)} done)", flush=True)
    index, stats = [], Counter()
    for key, xs in plan.items():
        path = f"{text_prefix}/{key}.jsonl.gz"
        if path in done:
            got = unpack(stores.get(text_store, path))
        else:
            got = _write_group(ic, stores, xs, item=item, kind=kind, text_store=text_store, path=path,
                               pdf_store=pdf_store, pdf_base=f"{pdf_prefix}/{key}", title=f"{label} {key}", want=want, post=post)
        for r in got:
            stats[r["text_status"]] += 1
            if r.get("error"):
                stats["error"] += 1
            index.append({k: v for k, v in r.items() if k != "text"} | {"text_chars": len(r.get("text") or ""), "group": path})
    data = pack(index)
    stores.put(text_store, f"{text_prefix}/index.jsonl.gz", data, "application/gzip")
    record(stores.db, f"{text_prefix}/index.jsonl.gz", text_store, f"{kind}_index", item, data, title=f"{label}: index",
           rows=len(index), source=INDIA_CODE)
    print(f"  {label}: stored {len(index)} of {len(items)} ({dict(stats)})", flush=True)
    return {"label": label, "source": total, "taken": len(items), "stored": len(index), **stats}


def _write_group(ic: IC, stores: Stores, xs: list[tuple[dict, dict]], *, item: int, kind: str, text_store: str,
                 path: str, pdf_store: str, pdf_base: str, title: str, want=None, post=None) -> list[dict]:
    zips = Zips(stores, pdf_store, pdf_base, item, kind, title)
    out = []
    # Files are fetched one after another; their text is read alongside (local work only), and gathered after.
    pending = [(dict(row), fetch_files(ic, it, zips, want)) for it, row in xs]
    for row, finish in pending:
        row.update(finish())
        row.update({"source": INDIA_CODE, "read_at": now()})
        if post:
            post(row)
        out.append(row)
    zips.flush()
    zips.wait()
    data = pack(out)
    stores.put(text_store, path, data, "application/gzip")
    years = {r["year"] for r in out if r.get("year")}
    record(stores.db, path, text_store, kind, item, data, title=title, year=years.pop() if len(years) == 1 else None,
           rows=len(out), source=INDIA_CODE)
    print(f"    {path}: {len(out)} documents, {sum(1 for r in out if r['text_status'] == 'scan_only')} scan-only, "
          f"{sum(1 for r in out if r.get('error'))} errors", flush=True)
    return out


_POOL = ThreadPoolExecutor(max_workers=3)
_UPLOAD = ThreadPoolExecutor(max_workers=1)
_INFLIGHT: deque = deque()


def _read_later(data: bytes) -> Future:
    while len(_INFLIGHT) >= 12:  # keep few downloaded files waiting in memory
        _INFLIGHT.popleft().result()
    fut = _POOL.submit(read_pdf, data)
    _INFLIGHT.append(fut)
    return fut


def read_files(ic: IC, it: dict, zips: Zips | None, want=None) -> dict:
    return fetch_files(ic, it, zips, want)()


def fetch_files(ic: IC, it: dict, zips: Zips | None, want=None):
    """Download an item's original files into the zips; returns a function that, once their text is read, gives
    files, text (of the main file: not the Hindi copy, unless want picks it), text_status and any error."""
    files = it.get("files")
    if files is None:
        files = ic.files(it["uuid"])
    originals = files.get("ORIGINAL", [])
    if want:
        originals = [b for b in originals if want(b, originals)]
    out, texts, errors, jobs = [], {}, [], []
    folder = (it.get("handle") or it["uuid"]).split("/")[-1]
    for b in originals:
        f = {"name": b["name"], "bytes": b.get("bytes"), "url": f"{API}/core/bitstreams/{b['uuid']}/content"}
        out.append(f)
        try:
            data = ic.content(b)
        except Source as e:
            errors.append(str(e))
            f["error"] = str(e)
            continue
        f["bytes"] = len(data)
        jobs.append((f, _read_later(data) if data[:4] == b"%PDF" else None))
        if zips is not None:  # the zip fills in f's store, zip, offset and size when its part is written
            zips.add(folder + "/" + SAFE.sub("_", b["name"]), data, f)
    return lambda: _finish(ic, files, out, texts, errors, jobs, want)


def _finish(ic: IC, files: dict, out: list[dict], texts: dict, errors: list[str], jobs: list, want) -> dict:
    for f, fut in jobs:
        got = fut.result() if fut else {"text": None, "text_status": "not_pdf", "pages": None, "scan_pages": None}
        texts[id(f)] = got.pop("text")
        f.update(got)
    # The main file: one with text, English before Hindi (judged by the text itself; names vary by state).
    ok = [f for f in out if not f.get("error")]

    def hindi_text(f: dict) -> bool:
        t = texts.get(id(f)) or ""
        return len(DEVANAGARI.findall(t)) > 0.3 * max(1, sum(ch.isalpha() for ch in t))

    rank = sorted(ok, key=lambda f: (texts.get(id(f)) is None, not want and hindi_text(f), f["name"]))
    main = rank[0] if rank else None
    res = {"files": out}
    if main is None:
        # No original file: India Code's own text copy, when there is one, is the text.
        txt = files.get("TEXT", [])
        if txt:
            try:
                t = ic.get(f"{API}/core/bitstreams/{txt[0]['uuid']}/content").text
                res.update({"text": clean_block(t) or None, "text_status": "india_code_text" if clean_block(t) else "no_file"})
            except Source as e:
                res.update({"text": None, "text_status": "no_file"})
                errors.append(str(e))
        else:
            res.update({"text": None, "text_status": "no_file"})
    else:
        res.update({"text": texts.get(id(main)), "text_status": main.get("text_status") or "unreadable", "main_file": main["name"]})
    if errors:
        res["error"] = "; ".join(errors)
        if main is None:
            res["text_status"] = "failed"
    return res


SAFE = re.compile(r'[\\/:*?<>|"]+')


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ---------------------------------------------------------------- the plan's items

def rules(fresh: bool = False) -> list[dict]:
    """Item 24: central Rules and Regulations. Text in Backblaze; the original PDFs (about 1 GB) in Drive."""
    ic, st = IC(), Stores()
    return [load_collection(ic, st, community="CENTRAL", collection=c, item=24, kind=k, text_store="b2", text_prefix=f"{p}/central",
                            pdf_store="drive", pdf_prefix=f"library/{p}/central", label=f"Central {p}", fresh=fresh)
            for c, k, p in (("Rule", "rule", "rules"), ("Regulation", "regulation", "regulations"))]


def repealed(fresh: bool = False) -> list[dict]:
    """Item 25: old repealed central acts, as last published."""
    ic, st = IC(), Stores()
    return [load_collection(ic, st, community="CENTRAL", collection="Repeal Acts", item=25, kind="repealed_act", text_store="drive",
                            text_prefix="library/repealed-acts", pdf_store="drive", pdf_prefix="library/repealed-acts",
                            label="Repealed central acts", fresh=fresh)]


NOTICES = [("Notification", "notification"), ("Circular", "circular"), ("Order", "order"), ("Central Ordinance", "central_ordinance"),
           ("Act Amendments", "act_amendment"), ("Statute", "statute"), ("Annexure", "annexure")]


def notifications(only: list[str] | None = None, fresh: bool = False) -> list[dict]:
    """Item 22 (from India Code rather than the e-Gazette): central notifications, circulars, orders, ordinances,
    amending acts, statutes and annexures."""
    ic, st = IC(), Stores()
    out = []
    for c, k in NOTICES:
        if only and c not in only and k not in only:
            continue
        p = f"library/notifications/{slug(c)}"
        out.append(load_collection(ic, st, community="CENTRAL", collection=c, item=22, kind=k, text_store="drive", text_prefix=p,
                                   pdf_store="drive", pdf_prefix=p, label=f"Central {c}", fresh=fresh))
    return out


# "The Constitution (Scheduled Tribes) Order (Amendment) Act, 2003", "The Sixth Schedule to the Constitution (Amendment) Act",
# "The Constitution (One Hundred and Sixth Amendment) Act, 2023"; not "constitution of a Board".
AMENDS_CONSTITUTION = re.compile(r"\bconstitution\b[^,]*\bamendment\b[^,]*\bact\b|\bamendment\b[^,]*\bof\s+the\s+constitution\b[^,]*\bact\b", re.I)


def constitution(fresh: bool = False) -> list[dict]:
    """Item 27: the 27 items of India Code's "Constitution of India" collection (the 2026 English and Hindi texts and
    the other languages) and every act amending the Constitution or its Orders that India Code holds, in whichever
    collection. Text in Backblaze. The language editions' PDFs (about 440 MB) go to Drive; the acts' PDFs are small."""
    ic, st = IC(), Stores()
    out = [load_collection(ic, st, community="CENTRAL", collection="Constitution of India", item=27, kind="constitution_text",
                           text_store="b2", text_prefix="constitution/amendments/editions", pdf_store="drive",
                           pdf_prefix="library/constitution/editions", label="Constitution of India (editions)", group="all", fresh=fresh)]
    for c in ("Acts", "Repeal Acts", "Act Amendments", "Spent Acts", "Acts Not Enforced"):
        out.append(load_collection(ic, st, community="CENTRAL", collection=c, item=27, kind="constitution_amendment", text_store="b2",
                                   text_prefix=f"constitution/amendments/{slug(c)}", pdf_store="b2", pdf_prefix=f"constitution/amendments/{slug(c)}",
                                   label=f"Constitution amendment acts in {c}", group="all", fresh=fresh,
                                   pick=lambda it: bool(AMENDS_CONSTITUTION.search(clean(it.get("name") or md(it, "dc.title"))))))
    return out


def _hindi_text(row: dict) -> None:
    """pdftotext on a Hindi PDF made with an old (non-Unicode) font gives Latin letters, not Hindi: no text then."""
    t = row.get("text")
    row["language"] = "hi"
    if t and len(DEVANAGARI.findall(t)) < 0.3 * sum(ch.isalpha() for ch in t):
        if len(re.findall(r"\b(?:the|of|and|shall)\b", t, re.I)) > 0.02 * len(t.split()):
            # Plain English: a second English copy, not a Hindi one. Kept and marked, never called Hindi.
            row["text_status"], row["language"] = "not_hindi", "en"
        else:
            row["text"], row["text_status"] = None, "not_unicode"


def hindi_acts(fresh: bool = False) -> list[dict]:
    """Item 26 (acts): the Hindi copy of each central act that India Code holds (an ORIGINAL file named h….pdf)."""
    ic, st = IC(), Stores()
    def want(b: dict, originals: list[dict]) -> bool:
        pdfs = [x for x in originals if x["name"].lower().endswith(".pdf")]
        if any(hindi_name(x["name"]) for x in pdfs):
            return b in pdfs and hindi_name(b["name"])
        return b in pdfs and len(pdfs) > 1 and _bare_number(b["name"])  # checked by its text in _hindi_text
    return [load_collection(ic, st, community="CENTRAL", collection="Acts", item=26, kind="hindi_act", text_store="drive",
                            text_prefix="library/hindi/acts", pdf_store="drive", pdf_prefix="library/hindi/acts", label="Hindi central acts",
                            pick=lambda it: any(want(b, it.get("files", {}).get("ORIGINAL", [])) for b in it.get("files", {}).get("ORIGINAL", [])),
                            want=want, post=_hindi_text, fresh=fresh)]


def state_sections(ic: IC, st: Stores, state: str, fresh: bool = False) -> dict:
    """A state's Section collection (text in India Code's records, no files): one file of every section."""
    uuid = ic.tree()[state].get("Section")
    path = f"library/state/{slug(state)}/sections/sections.jsonl.gz"
    total = ic.count(uuid) if uuid else 0
    if not total:
        return {"label": f"{state} sections", "source": 0, "taken": 0, "stored": 0}
    done = st.done(path)
    if path in done and not fresh:
        return {"label": f"{state} sections", "source": total, "taken": total, "stored": done[path]["rows"]}
    total, items = ic.listing(uuid, files=False, fresh=fresh)
    rows = []
    for it in items:
        h = it.get("handle") or ""
        rows.append({"id": h, "uuid": it["uuid"], "act_id": md(it, "dc.identifier.act_id") or None,
                     "act": clean(md(it, "dc.identifier.act_name")) or None, "act_number": md(it, "dc.identifier.act_number") or None,
                     "act_year": md(it, "dc.date.act_year") or None, "number": md(it, "dc.identifier.section_number").strip() or None,
                     "title": clean(it.get("name") or md(it, "dc.title")) or None, "text": _text(md(it, "dc.identifier.section_page_note")) or None,
                     "notes": _text(md(it, "dc.identifier.section_footnote")) or None,
                     "order": float(md(it, "dc.identifier.order_number") or 0) if re.match(r"^-?\d+(\.\d+)?$", md(it, "dc.identifier.order_number") or "") else None,
                     "department": clean(md(it, "dc.identifier.department_name")) or None, "state": state, "collection": "Section",
                     "source_url": f"{SITE}/handle/{h}" if h else None, "source": INDIA_CODE, "read_at": now()})
    rows.sort(key=lambda r: (r["act"] or "", r["act_id"] or "", r["order"] if r["order"] is not None else 1e9))
    data = pack(rows)
    st.put("drive", path, data, "application/gzip")
    record(st.db, path, "drive", "state_section", 18, data, title=f"{state}: sections of state acts", rows=len(rows), source=INDIA_CODE)
    print(f"  {state} sections: {len(rows)} of {total}", flush=True)
    return {"label": f"{state} sections", "source": total, "taken": len(items), "stored": len(rows)}


def state_acts(only: list[str] | None = None, fresh: bool = False) -> list[dict]:
    """Item 18: every state's and UT's Acts and Rules (with their PDFs), and its sections where India Code has them."""
    ic, st = IC(), Stores()
    out = []
    for state in sorted(s for s in ic.tree() if s != "CENTRAL"):
        if only and state not in only and slug(state) not in only:
            continue
        base = f"library/state/{slug(state)}"
        for c, k, p in (("Acts", "state_act", "acts"), ("Rule", "state_rule", "rules")):
            out.append(load_collection(ic, st, community=state, collection=c, item=18, kind=k, text_store="drive", text_prefix=f"{base}/{p}",
                                       pdf_store="drive", pdf_prefix=f"{base}/{p}", label=f"{state} {p}", fresh=fresh))
        out.append(state_sections(ic, st, state, fresh=fresh))
    return out


def check(prefix: str, n: int = 3) -> int:
    """What is stored under prefix: files and bytes per store, documents by text status, and n random documents
    opened back from storage and compared with India Code (title, the original file byte for byte, first lines)."""
    import hashlib
    import random
    st, ic = Stores(), IC()
    files = st.done(prefix.rstrip("/") + "/")
    by_store = Counter()
    for f in files.values():
        by_store[f["store"]] += f["bytes"] or 0
    print(f"{prefix}: {len(files)} files · " + " · ".join(f"{s} {b / 1e6:.1f} MB" for s, b in by_store.items()))
    idx = next((p for p in files if p.endswith("index.jsonl.gz")), None) or next((p for p in files if p.endswith(".jsonl.gz")), None)
    if not idx:
        print("  nothing stored")
        return 1
    store = files[idx]["store"]
    rows = unpack(st.get(store, idx))
    print(f"  {len(rows)} documents in {idx} · " + ", ".join(f"{k} {v}" for k, v in Counter(r.get("text_status", "section") for r in rows).items())
          + f" · errors {sum(1 for r in rows if r.get('error'))}")
    bad = 0
    for r in random.sample(rows, min(n, len(rows))):
        group = r.get("group") or idx
        doc = next(x for x in unpack(st.get(store, group)) if x["uuid"] == r["uuid"])
        src = ic.get(f"{API}/core/items/{r['uuid']}").json()
        same_title = clean(src.get("name")) == doc["title"]
        line = f"  {doc['id']} {doc['title'][:70]!r}: title {'same' if same_title else 'DIFFERENT: ' + repr(clean(src.get('name')))[:80]}"
        ok = same_title
        if doc.get("files"):
            f = next((x for x in doc["files"] if x.get("name") == doc.get("main_file")), doc["files"][0])
            kept = original_file(st, f)
            fresh = ic.get(f["url"]).content
            # India Code writes a new trailer /ID (and for some files new dates) into a PDF each time it serves it.
            stamp = lambda b: re.sub(rb"/ID\s*\[\s*<[0-9A-Fa-f]*>\s*<[0-9A-Fa-f]*>\s*\]|/(?:Creation|Mod)Date\s*\([^)]*\)|<xmp:(?:Modify|Metadata)Date>[^<]*<", b"", b)
            same_file = len(kept) == len(fresh) and hashlib.sha1(stamp(kept)).digest() == hashlib.sha1(stamp(fresh)).digest()
            ok &= same_file
            line += f" · file {'same' if same_file else 'DIFFERENT'} ({len(kept):,} bytes)"
            if doc.get("text"):
                first = [ln for ln in (read_pdf(fresh)["text"] or "").splitlines()][:3]
                same_text = doc["text"].splitlines()[:3] == first
                ok &= same_text
                line += f" · first lines {'same' if same_text else 'DIFFERENT'}: {' / '.join(first)[:120]!r}"
            else:
                line += f" · no text ({doc['text_status']})"
        elif doc.get("text"):  # a section: its text is India Code's own record
            same_text = _text(md(src, "dc.identifier.section_page_note")) == doc["text"]
            ok &= same_text
            line += f" · text {'same' if same_text else 'DIFFERENT'}: {doc['text'][:100]!r}"
        bad += not ok
        print(line)
    return 1 if bad else 0


def _title_key(t: str) -> str:
    t = re.sub(r"\(\s*rep[^)]*\)", " ", (t or "").lower())
    t = re.sub(r"\b(?:1[6-9]|20)\d\d\b", " ", t)             # the year is compared apart
    t = re.sub(r"[^a-z0-9]+", " ", re.sub(r"^\s*the\s+", "", t))
    return re.sub(r"\s+", " ", t).strip()


def state_gaps() -> dict[str, list[dict]]:
    """Item 18 check: the state acts India Code has (our library/state/*/acts) that the section-by-section state acts
    in Backblaze (acts/state/index.jsonl.gz, from open-india-law) do not. Matched by state and title (year apart),
    exactly or nearly (difflib ratio >= 0.92). Saves the list to Drive and returns it by state."""
    import difflib
    st = Stores()
    theirs: dict[str, dict[str, list]] = {}
    for r in unpack(st.get("b2", "acts/state/index.jsonl.gz")):
        theirs.setdefault(r["state"], {}).setdefault(_title_key(r["act"]), []).append(r.get("year"))
    # Their state names are short slugs ("andaman-nicobar", "dadra-nagar-haveli"): matched on the first word.
    def their_state(name: str) -> str | None:
        s = slug(name)
        if s in theirs:
            return s
        return next((k for k in theirs if k.split("-")[0] == s.split("-")[0]), None)

    out: dict[str, list[dict]] = {}
    for path in sorted(p for p in st.done("library/state/") if p.endswith("/acts/index.jsonl.gz")):
        for r in unpack(st.get("drive", path)):
            ts = their_state(r["state"])
            keys = theirs.get(ts, {})
            k = _title_key(r["title"])
            if k in keys or (k and difflib.get_close_matches(k, keys.keys(), n=1, cutoff=0.92)):
                continue
            out.setdefault(r["state"], []).append({"state": r["state"], "title": r["title"], "year": r.get("year"), "number": r.get("number"),
                                                   "id": r["id"], "source_url": r.get("source_url"), "group": r.get("group")})
    rows = [x for v in out.values() for x in v]
    data = pack(rows)
    st.put("drive", "library/state/not-in-open-india-law.jsonl.gz", data, "application/gzip")
    record(st.db, "library/state/not-in-open-india-law.jsonl.gz", "drive", "state_act_gaps", 18, data,
           title="State acts on India Code that acts/state (open-india-law) lacks", rows=len(rows), source=INDIA_CODE)
    return out


COURT_FEES = re.compile(r"court[\s-]*fees?", re.I)
STAMP = re.compile(r"\bstamps?\b", re.I)


def fees_and_stamps() -> dict[str, dict]:
    """Item 9 check, after state-acts: each state's court-fees and stamp acts, read from the stored indexes."""
    st = Stores()
    out = {}
    for path in sorted(p for p in st.done("library/state/") if p.endswith("/acts/index.jsonl.gz")):
        state = None
        fees, stamps = [], []
        for r in unpack(st.get("drive", path)):
            state = r["state"]
            t = r["title"]
            if COURT_FEES.search(t):
                fees.append(t)
            if STAMP.search(t) and not re.search(r"\bstamp(?:s)?\s+(?:vendor|paper)s?\b", t, re.I):
                stamps.append(t)
        principal = lambda ts: [t for t in ts if not re.search(r"amendment|validation|repeal|\(amdt", t, re.I)]
        out[state] = {"court_fees": principal(fees), "court_fees_amendments": len(fees) - len(principal(fees)),
                      "stamp": principal(stamps), "stamp_amendments": len(stamps) - len(principal(stamps))}
    return out
