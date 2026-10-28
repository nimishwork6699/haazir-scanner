"""Full judgment texts and district court summaries, from the public court records (AWS Open Data) into Google Drive.

Storage plan items (reports/Library storage plan.md):
  14  district court summaries, every state and year    -> Drive library/district/<year>/<state>.jsonl.gz
  15  Supreme Court judgments, full text (English)       -> Drive library/fulltext/sc/<year>/...
  16  High Court judgments and orders, full text         -> Drive library/fulltext/hc/<year>/...
  17  district court final judgments, full text          -> Drive library/fulltext/dc/<year>/...
  26  Supreme Court judgments in Indian languages        -> Drive library/fulltext/sc/<year>/regional...

The work is too big for one machine (High Court PDFs alone are about 1.5 TB), so it runs on many GitHub machines:
  python -m haazir.fulltext plan <set>        splits what is not done yet into up to 250 even units (by bytes),
                                               writes them to Backblaze work/<set>.json, makes the Drive folders,
                                               prints the unit count
  python -m haazir.fulltext run <set> <i>     does unit i (streams each bundle, reads every PDF with pdftotext)
  python -m haazir.fulltext status <set>      what is stored so far
Sets: sc, hc, dc (full texts) and district (summaries). A bundle counts as done once its files are in the library
map (library_files): they are recorded only after the whole bundle is stored, so a cut-short run simply redoes it.
Text is what pdftotext reads; a scanned page has no text, and is kept as such (scan: true), never guessed.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from .db import DB
from .storage import B2, Drive, pack

BUCKETS = {"sc": "https://indian-supreme-court-judgments.s3.amazonaws.com", "hc": "https://indian-high-court-judgments.s3.amazonaws.com",
           "dc": "https://indian-district-court-judgments.s3.amazonaws.com"}
BUCKETS["district"] = BUCKETS["dc"]
SOURCE = {"sc": "Supreme Court of India judgments (AWS Open Data, CC BY 4.0)",
          "hc": "High Courts of India, judgment records (AWS Open Data copy, CC BY 4.0)",
          "dc": "District courts of India, eCourts records (AWS Open Data copy, CC BY 4.0)"}
SOURCE["district"] = SOURCE["dc"]
PDFTOTEXT = next((p for p in (r"C:\Program Files\Git\mingw64\bin\pdftotext.exe", "/usr/bin/pdftotext") if os.path.exists(p)), "pdftotext")
CHUNK = 64_000_000      # text per stored file before gzip (about 15 MB gzip'd)
UNITS = 250             # GitHub runs at most 256 jobs from one list
SCAN = 200              # fewer characters than this: a scanned page with no text layer
EMPTY_TAR = 10_240      # a tar with no files in it is one 10 KB record of zeros


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def s3_keys(bucket: str, prefix: str, modified: bool = False) -> list[tuple]:
    """(key, size), or (key, size, last modified as ISO time) with modified=True."""
    ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
    out, token = [], None
    with httpx.Client(timeout=120) as c:
        while True:
            params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                params["continuation-token"] = token
            root = ET.fromstring(c.get(f"{bucket}/", params=params).content)
            out += [(k.find(f"{ns}Key").text, int(k.find(f"{ns}Size").text)) + ((k.find(f"{ns}LastModified").text,) if modified else ())
                    for k in root.findall(f"{ns}Contents")]
            if root.find(f"{ns}IsTruncated").text != "true":
                return out
            token = root.find(f"{ns}NextContinuationToken").text


class _Body(io.RawIOBase):
    """An HTTP download read as a file, so a tar of many GB is read as it arrives, never held whole."""

    def __init__(self, resp: httpx.Response) -> None:
        self.it, self.buf = resp.iter_bytes(1 << 20), b""

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        while not self.buf:
            try:
                self.buf = next(self.it)
            except StopIteration:
                return 0
        n = min(len(b), len(self.buf))
        b[:n], self.buf = self.buf[:n], self.buf[n:]
        return n


def pdf_text(data: bytes) -> str:
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "a.pdf")
        with open(src, "wb") as f:
            f.write(data)
        try:
            r = subprocess.run([PDFTOTEXT, "-enc", "UTF-8", "-q", src, "-"], capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            return ""
        return r.stdout.decode("utf-8", "replace").replace("\f", "\n").strip()


# ---- what each set's bundles are, and where their output goes

def _year(key: str) -> int:
    return int(re.search(r"year=(\d{4})", key).group(1))


def _stem(key: str) -> str:
    return key.rsplit("/", 1)[1].removesuffix(".gz").removesuffix(".tar")


def out_prefix(ds: str, key: str) -> str:
    """Where a bundle's output files go; every name starts with this, which is also how a done bundle is found."""
    y = _year(key)
    if ds == "sc":
        lang = re.search(r"/(english|regional)/", key).group(1)
        return f"library/fulltext/sc/{y}/{lang}_{_stem(key)}"
    if ds == "hc":
        m = re.search(r"court=([^/]+)/bench=([^/]+)/", key)
        return f"library/fulltext/hc/{y}/{m.group(1)}_{m.group(2)}_{_stem(key)}"
    m = re.search(r"state=([^/]+)/district=([^/]+)/complex=([^/]+)/", key)
    if ds == "dc":   # "all": every order, final and interim (the first load, 26 Oct, kept final judgments only)
        return f"library/fulltext/dc/{y}/all_{m.group(1)}_{m.group(2)}_{m.group(3)}_{_stem(key)}"
    return f"library/district/{y}/{m.group(1)}"   # district: the unit is a state's year, the key one of its bundles


def bundles(ds: str) -> list[tuple[str, int, str]]:
    """Every source bundle of a set: (key, size, last modified)."""
    if ds in ("sc", "hc", "dc"):
        return [b for b in s3_keys(BUCKETS[ds], "data/tar/", True) if b[0].endswith(".tar") and "part-mobile" not in b[0]]
    return [b for b in s3_keys(BUCKETS[ds], "metadata/tar/", True) if re.search(r"\.tar(\.gz)?$", b[0])]


def done_prefixes(db: DB, ds: str) -> dict[str, str]:
    """Output prefix -> when it was stored (the oldest of its files)."""
    root = "library/district/" if ds == "district" else f"library/fulltext/{ds}/"
    out: dict[str, str] = {}
    for r in db.select("library_files", f"select=path,updated_at&path=like.{root}*&order=path"):
        p = r["path"].removesuffix(".jsonl.gz") if ds == "district" else re.sub(r"_\d{3}\.jsonl\.gz$", "", r["path"])
        out[p] = min(out.get(p, r["updated_at"]), r["updated_at"])
    return out


def _stamp(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def plan(ds: str) -> int:
    """What is not stored yet, or changed at the source since it was stored (the monthly refresh relies on this)."""
    db, b2, drive = DB(), B2(), Drive()
    done = done_prefixes(db, ds)
    found = bundles(ds)
    newest: dict[str, str] = {}    # a district unit is redone when any of its state's bundles for that year changed
    for k, _, m in found:
        p = out_prefix(ds, k)
        newest[p] = max(newest.get(p, m), m)
    todo: dict[str, list] = {}     # unit name -> its bundles; a district unit is a state's year (one output file)
    for k, s, _ in found:
        p = out_prefix(ds, k)
        if p in done and _stamp(done[p]) >= _stamp(newest[p]):
            continue
        if s <= EMPTY_TAR:
            continue    # an empty bundle at the source (e.g. Bombay 'newos_spl' 1993–1999): nothing to read, never "done"
        todo.setdefault(p if ds == "district" else k, []).append((k, s))
    groups = sorted(todo.values(), key=lambda g: -sum(s for _, s in g))
    units: list[list] = [[] for _ in range(min(UNITS, len(groups)))]
    sizes = [0] * len(units)
    for g in groups:                               # largest first, each into the lightest unit
        i = sizes.index(min(sizes))
        units[i] += [k for k, _ in g]
        sizes[i] += sum(s for _, s in g)
    b2.put(f"work/{ds}.json", json.dumps(units).encode(), "application/json")
    # The folders every unit writes into, made once here (machines making the same folder at once would make two).
    for y in sorted({_year(k) for u in units for k in u}):
        drive.folder(f"library/district/{y}" if ds == "district" else f"library/fulltext/{ds}/{y}")
    total = sum(sizes)
    print(f"{ds}: {len(groups):,} to do in {len(units)} units, {total / 1e9:.1f} GB to read (biggest unit {max(sizes or [0]) / 1e9:.1f} GB)", file=sys.stderr, flush=True)
    print(len(units))
    return len(units)


# ---- reading

def _members(url: str):
    """Each file in a tar at this URL: (name, bytes), read as the download arrives."""
    with httpx.Client(timeout=httpx.Timeout(600, connect=60)) as c, c.stream("GET", url) as r:
        r.raise_for_status()
        mode = "r|gz" if url.endswith(".gz") else "r|"
        with tarfile.open(fileobj=io.BufferedReader(_Body(r), 1 << 22), mode=mode) as t:
            for m in t:
                if m.isfile():
                    yield m.name.rsplit("/", 1)[-1], t.extractfile(m).read()


class Writer:
    """Rows into ~64 MB text files on Drive; recorded in the library map only by finish(), once all are stored."""

    def __init__(self, drive: Drive, prefix: str, ds: str, item: int, **meta) -> None:
        self.drive, self.prefix, self.ds, self.item, self.meta = drive, prefix, ds, item, meta
        self.rows, self.size, self.n, self.files = [], 0, 0, []

    def add(self, row: dict) -> None:
        self.rows.append(row)
        self.size += len(row.get("text") or "") + 300
        if self.size >= CHUNK:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        self.n += 1
        path = f"{self.prefix}_{self.n:03d}.jsonl.gz"
        data = pack(self.rows)
        self.drive.put(path, data, "application/gzip")
        self.files.append({"path": path, "store": "drive", "kind": "fulltext", "item": self.item, "rows": len(self.rows), "bytes": len(data),
                           "source": SOURCE[self.ds], "updated_at": now(), **self.meta})
        self.rows, self.size = [], 0

    def finish(self, db: DB) -> int:
        self.flush()
        if self.files:
            db.insert("library_files", self.files, upsert_on="path")
        return sum(f["rows"] for f in self.files)


def _texts(items, workers: int):
    """(name, bytes, extra) in, (name, text, extra) out, read on every core at once."""
    with ThreadPoolExecutor(workers) as pool:
        pending = set()
        for name, data, extra in items:
            pending.add(pool.submit(lambda n=name, d=data, e=extra: (n, pdf_text(d), e)))
            if len(pending) >= workers * 4:
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for f in finished:
                    yield f.result()
        for f in pending:
            yield f.result()


def _orders(bucket: str, data_key: str) -> dict[str, dict]:
    """A district complex's orders, final and interim, by PDF file name ('orders_2022_203700000012018_1.pdf'), with their
    case details, from its case records."""
    folder = data_key.rsplit("/", 1)[0].replace("data/tar/", "metadata/tar/", 1) + "/"
    out = {}
    for k, _ in s3_keys(bucket, folder):
        if not re.search(r"\.tar(\.gz)?$", k):
            continue
        for _, raw in _members(f"{bucket}/{k}"):
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            h, cs, orders = d.get("history") or {}, d.get("case_summary") or {}, d.get("orders") or {}
            for final, o in [(True, o) for o in orders.get("final_orders") or []] + [(False, o) for o in orders.get("interim_orders") or []]:
                fn = (o.get("pdf_filename") or "").strip("/")
                if fn:
                    out[fn.replace("/", "_")] = {"final": final, "cnr": h.get("cino") or cs.get("cino"), "order_date": o.get("order_date"),
                                                 "order_type": o.get("order_type"), "case": f"{h.get('type_name') or cs.get('case_type') or ''} {h.get('reg_no') or cs.get('case_number') or ''}/{h.get('reg_year') or cs.get('reg_year') or ''}".strip(),
                                                 "petitioner": h.get("pet_name"), "respondent": h.get("res_name"), "disposal": h.get("disp_name")}
    return out


def run_fulltext(ds: str, key: str, workers: int) -> tuple[int, int]:
    db, drive, bucket = DB(), Drive(), BUCKETS[ds]
    y = _year(key)
    item = {"sc": 26 if "/regional/" in key else 15, "hc": 16, "dc": 17}[ds]
    court = "sc" if ds == "sc" else re.search(r"(?:court|state)=([^/]+)/", key).group(1)
    w = Writer(drive, out_prefix(ds, key), ds, item, title=f"{ds.upper()} {court} {y} full text", court=court, year=y)
    orders = _orders(bucket, key) if ds == "dc" else {}

    def items():
        for name, data in _members(f"{bucket}/{key}"):
            if not name.lower().endswith(".pdf"):
                continue
            # District: every order; marked final or interim where the case record says, unmarked if no record lists it.
            yield name, data, orders.get(name, {})

    scans = 0
    for name, text, extra in _texts(items(), workers):
        row = {"id": name.removesuffix(".pdf"), "court": court, "year": y, "text": text, "chars": len(text)}
        if ds == "sc":
            row["lang"] = "regional" if "/regional/" in key else "english"
        if ds == "hc":
            row["bench"] = re.search(r"bench=([^/]+)/", key).group(1)
        if len(text) < SCAN:
            row["scan"] = True
            scans += 1
        w.add({**row, **{k: v for k, v in extra.items() if v not in (None, "")}})
    return w.finish(db), scans


# ---- district summaries (item 14)

def _plain(html: str | None) -> str | None:
    if not html:
        return None
    t = re.sub(r"\s+", " ", BeautifulSoup(str(html), "html.parser").get_text(" ")).strip()
    return t or None


def district_row(d: dict) -> dict | None:
    h, cs, loc, orders = d.get("history") or {}, d.get("case_summary") or {}, d.get("location") or {}, d.get("orders") or {}
    cnr = h.get("cino") or cs.get("cino")
    if not cnr:
        return None
    finals = orders.get("final_orders") or []
    row = {
        "cnr": cnr, "case_type": h.get("type_name") or cs.get("case_type"), "number": h.get("reg_no") or cs.get("case_number"),
        "reg_year": h.get("reg_year") or cs.get("reg_year"), "petitioner": h.get("pet_name") or cs.get("petitioner"),
        "respondent": h.get("res_name") or cs.get("respondent"), "pet_adv": h.get("pet_adv") or None, "res_adv": h.get("res_adv") or None,
        "state": loc.get("state_name"), "district": loc.get("district_name"), "complex": loc.get("complex_name"),
        "court": h.get("court_name") or cs.get("establishment_name"), "judge": h.get("desgname") or cs.get("court_number_and_judge"),
        "filed": h.get("date_of_filing") or cs.get("filing_date"), "registered": h.get("dt_regis") or cs.get("registration_date"),
        "decided": h.get("date_of_decision"), "disposal": h.get("disp_name"), "status": cs.get("case_status"),
        "stage": (h.get("purpose_name") or "").strip() or None, "acts": re.sub(r"^Under Act\(s\)\s*Under Section\(s\)\s*", "", _plain(h.get("act")) or "") or None, "fir": _plain(h.get("fir_details")),
        "lower_court": h.get("lower_court") or None,
        "final_orders": [{"date": o.get("order_date"), "type": o.get("order_type"), "pdf": (o.get("pdf_filename") or "").strip("/")} for o in finals] or None,
        "interim_orders": len(orders.get("interim_orders") or []) or None,
    }
    return {k: v for k, v in row.items() if v not in (None, "", [], 0)}


def run_district(keys: list[str]) -> tuple[int, int]:
    """One state's year: every complex's case records into one summary file."""
    db, drive, bucket = DB(), Drive(), BUCKETS["district"]
    prefix = out_prefix("district", keys[0])
    rows, seen = [], set()
    for k in keys:
        for _, raw in _members(f"{bucket}/{k}"):
            try:
                r = district_row(json.loads(raw))
            except ValueError:
                continue
            if r and r["cnr"] not in seen:
                seen.add(r["cnr"]); rows.append(r)
    if not rows:
        return 0, 0
    data = pack(rows)
    path = f"{prefix}.jsonl.gz"
    drive.put(path, data, "application/gzip")
    y, state = _year(keys[0]), re.search(r"state=([^/]+)/", keys[0]).group(1)
    db.insert("library_files", {"path": path, "store": "drive", "kind": "district_cases", "item": 14, "title": f"{rows[0].get('state') or state} district courts {y}",
                                "court": state, "year": y, "rows": len(rows), "bytes": len(data), "source": SOURCE["district"], "updated_at": now()}, upsert_on="path")
    return len(rows), 0


def run(ds: str, index: int, workers: int) -> None:
    units = json.loads(B2().get(f"work/{ds}.json") or b"[]")
    if index >= len(units):
        print(f"unit {index}: nothing (there are {len(units)})"); return
    keys = units[index]
    if ds == "district":
        groups: dict[str, list] = {}
        for k in keys:
            groups.setdefault(out_prefix(ds, k), []).append(k)
        for p, ks in groups.items():
            n, _ = run_district(ks)
            print(f"{p}: {n:,} cases", flush=True)
        return
    for k in keys:
        n, scans = run_fulltext(ds, k, workers)
        print(f"{out_prefix(ds, k)}: {n:,} texts ({scans:,} scanned, no text)", flush=True)


def status(ds: str) -> None:
    root = "library/district/" if ds == "district" else f"library/fulltext/{ds}/"
    rows = DB().select("library_files", f"select=year,rows,bytes,path&path=like.{root}*&order=path")
    by: dict[int, list] = {}
    for r in rows:
        v = by.setdefault(r["year"] or 0, [0, 0, 0])
        v[0] += 1; v[1] += r["rows"] or 0; v[2] += r["bytes"] or 0
    for y, (f, n, b) in sorted(by.items()):
        print(f"{y}: {f} files, {n:,} documents, {b / 1e6:,.0f} MB")
    print(f"total: {sum(v[0] for v in by.values())} files, {sum(v[1] for v in by.values()):,} documents, {sum(v[2] for v in by.values()) / 1e9:.2f} GB")


def main() -> int:
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "plan":
        plan(a[1])
    elif len(a) >= 3 and a[0] == "run":
        run(a[1], int(a[2]), int(a[3]) if len(a) > 3 else (os.cpu_count() or 2))
    elif len(a) >= 2 and a[0] == "status":
        status(a[1])
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
