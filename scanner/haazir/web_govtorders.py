"""Item 28 of the library plan: government orders for service matters.

    python -m haazir.web_govtorders dopt [--limit N]   # DoPT: Document Repository + documents of dopt.gov.in
    python -m haazir.web_govtorders cvc [--limit N]    # CVC: circulars of cvc.gov.in (all categories, archives too)
    python -m haazir.web_govtorders status [dopt|cvc]

Google Drive library/govt-orders/<dept>/<year>/<file> (as published) and library/govt-orders/<dept>/text/<year>.jsonl.gz
(one row per document: title, number, date, issuing body, category, extracted text, source URL).

Where each comes from:
  DoPT  dopt.gov.in is a JavaScript site; its content service (dopt.gov.in/cms/wp-json/wp/v2/) lists every document it
        publishes (central_documents = the Document Repository, documents = notices) with the file's own address.
        DoPT's older archive of OMs (doptcirculars.nic.in, "CCIS") is searchable only behind a captcha: not used.
  CVC   cvc.gov.in is a JavaScript site; its pages read cvc.gov.in/api/get-all-<category>, which lists each circular
        (date, title, order/circular number, file). The English file is kept; the Hindi copy's address is noted.
"""

import argparse
import html
import random
import re
import sys
from collections import Counter

from .storage import unpack
from .web_common import Blocked, Library, Site, index_file, run_docs

ROOT = "library/govt-orders"
DOPT_API = "https://dopt.gov.in/cms/wp-json/wp/v2"
DOPT_SOURCE = "Department of Personnel and Training (dopt.gov.in)"
CVC = "https://cvc.gov.in"
CVC_SOURCE = "Central Vigilance Commission (cvc.gov.in)"
# cvc.gov.in's own lists of circulars (its menu "Circulars - Vigilance Administration" and the others with orders).
CVC_LISTS = {"get-all-inquirymatters": "Inquiry matters", "get-all-pbs": "Public sector banks", "get-all-psus": "Public sector undertakings",
             "get-all-vigilence": "Instructions on vigilance", "get-all-vigilenceclearence": "Vigilance clearance",
             "get-all-cvoappointment": "CVO appointment", "get-all-masterclass-circular": "Master circulars",
             "get-all-archived-documents": None, "get-all-TraningReletedCircular": "Training related circulars",
             "get-all-pidpi": "PIDPI (whistle-blower) circulars", "get-all-iem": "Integrity pact / IEM circulars",
             "get-all-preventive-vigilance-initiatives": "Preventive vigilance", "get-all-complaintguide": "Complaint handling guidelines",
             "get-all-publicpro": "Public procurement", "get-all-cvcact": "CVC Act and rules", "get-all-public-notice": "Public notices"}


def _ymd(s: str | None) -> str | None:
    s = (s or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


# ---------------------------------------------------------------- DoPT


def _wp_all(site: Site, kind: str, fields: str | None = None) -> list[dict]:
    out, page = [], 1
    while True:
        params = {"per_page": 100, "page": page}
        if fields:
            params["_fields"] = fields
        r = site.get(f"{DOPT_API}/{kind}", params=params)
        if r.status_code == 400 and page > 1:  # past the last page
            return out
        if r.status_code != 200:
            raise Blocked(f"DoPT {kind} page {page}: HTTP {r.status_code}")
        out += r.json()
        pages = int(r.headers.get("x-wp-totalpages") or 1)
        if page >= pages:
            return out
        page += 1


def dopt_listing(site: Site) -> tuple[list[dict], dict]:
    media = {m["id"]: m for m in _wp_all(site, "media", "id,source_url,mime_type,post")}
    cats = {c["id"]: c for c in _wp_all(site, "cd_category", "id,name,parent")}
    dcats = {c["id"]: c for c in _wp_all(site, "documents_category", "id,name,parent")}

    def cat_name(ids: list[int], table: dict) -> str | None:
        names = []
        for i in ids or []:
            c = table.get(i)
            if not c:
                continue
            parent = table.get(c.get("parent") or 0)
            names.append(_clean(f"{parent['name']} / {c['name']}" if parent else c["name"]))
        return "; ".join(names) or None

    docs, notes = [], Counter()
    for it in _wp_all(site, "central_documents"):
        acf = it.get("acf") or {}
        fid = acf.get("pdf") or acf.get("pdf_both") or acf.get("pdf_hindi")
        m = media.get(fid) if isinstance(fid, int) else None
        if not m:
            notes["repository item without a file"] += 1
            continue
        date = _ymd(acf.get("file_date")) or _ymd(it.get("date"))
        hindi = media.get(acf.get("pdf_hindi")) if isinstance(acf.get("pdf_hindi"), int) else None
        docs.append({"url": m["source_url"], "title": _clean(acf.get("title") or it["title"]["rendered"]), "number": None, "date": date,
                     "body": "Department of Personnel and Training", "category": cat_name(it.get("cd_category"), cats) or "Document Repository",
                     "language": "Hindi" if fid == acf.get("pdf_hindi") else ("English and Hindi" if fid == acf.get("pdf_both") else "English"),
                     "hindi_url": hindi["source_url"] if hindi and hindi["source_url"] != m["source_url"] else None,
                     "listed_on": "https://dopt.gov.in (Document Repository)", "wp_id": it["id"], "court": "dopt",
                     "subfolder": (date or "undated")[:4]})
    for it in _wp_all(site, "documents"):
        acf = it.get("acf") or {}
        date = _ymd(acf.get("date")) or _ymd(it.get("date"))
        for f in acf.get("file") or []:
            ids = f.get("file") if isinstance(f.get("file"), list) else ([f["file"]] if isinstance(f.get("file"), int) else [])
            urls = [media[i]["source_url"] for i in ids if i in media]
            if not urls and (f.get("external_link") or "").strip():
                notes["notice that only links elsewhere (PIB etc.), not kept"] += 1
                continue
            for u in urls:
                docs.append({"url": u, "title": _clean(f.get("title") or acf.get("title") or it["title"]["rendered"]), "number": None,
                             "date": date, "body": "Department of Personnel and Training",
                             "category": cat_name(it.get("documents_category"), dcats) or "Documents", "listed_on": "https://dopt.gov.in (documents)",
                             "wp_id": it["id"], "court": "dopt", "subfolder": (date or "undated")[:4]})
    seen, out = set(), []
    for d in docs:
        if d["url"] not in seen:
            seen.add(d["url"])
            out.append(d)
    notes["duplicates dropped"] = len(docs) - len(out)
    return out, dict(notes)


# ---------------------------------------------------------------- CVC


def cvc_listing(site: Site) -> tuple[list[dict], dict]:
    docs, notes = {}, Counter()
    for ep, category in CVC_LISTS.items():
        r = site.get(f"{CVC}/api/{ep}", headers={"Referer": f"{CVC}/"})
        try:
            data = r.json().get("data") or []
        except ValueError:
            notes[f"{ep}: not JSON (HTTP {r.status_code})"] += 1
            continue
        notes[f"{ep}: {len(data)}"] += 1
        for x in data:
            fp = (x.get("filePath") or "").strip()
            if not fp:
                notes["entry without a file"] += 1
                continue
            url = fp if fp.startswith("http") else f"{CVC}/{fp.lstrip('/')}"
            if url in docs:
                continue
            hp = (x.get("hindiPdfPath") or x.get("hindiFilePath") or "").strip()
            number = " / ".join(v.strip() for v in (x.get("order_number") or x.get("Order_Number"), x.get("circular_number") or x.get("Circular_Number"),
                                                     x.get("file_number")) if v and str(v).strip()) or None
            date = _ymd(x.get("date"))
            if date and "T18:30:00" in (x.get("date") or ""):  # stored as UTC midnight IST: the IST date is the next day
                from datetime import date as _d, timedelta
                date = (_d.fromisoformat(date) + timedelta(days=1)).isoformat()
            docs[url] = {"url": url, "title": _clean(x.get("title")), "number": number, "date": date, "body": "Central Vigilance Commission",
                         "category": category or f"Archive: {x.get('source')}", "hindi_title": _clean(x.get("hindiTitle")) or None,
                         "hindi_url": (hp if hp.startswith("http") else f"{CVC}/{hp.lstrip('/')}") if hp else None,
                         "listed_on": f"{CVC}/api/{ep}", "court": "cvc", "subfolder": (date or "undated")[:4]}
    return list(docs.values()), dict(notes)


# ---------------------------------------------------------------- load + status


def load(dept: str, limit: int | None, workers: int = 4) -> None:
    site = Site(pause=1.5, timeout=300)
    if dept == "dopt":
        lib, (docs, notes) = Library(28, DOPT_SOURCE), dopt_listing(site)
    else:
        lib, (docs, notes) = Library(28, CVC_SOURCE), cvc_listing(site)
    print(f"  {dept}: {len(docs)} documents listed; {notes}", flush=True)
    res = run_docs(lib, site, docs, store="drive", folder=f"{ROOT}/{dept}", kind="govt_order", text_kind="govt_order_text",
                   save_every=25, limit=limit, workers=workers,
                   group_of=lambda d: ("drive", f"{ROOT}/{dept}/text/{d['subfolder']}.jsonl.gz", f"{dept.upper()} orders {d['subfolder']}: text"),
                   group_meta=lambda d: {"court": dept, "year": int(d["subfolder"]) if d["subfolder"].isdigit() else None})
    res["listing_notes"] = notes
    index_file(lib, "drive", f"{ROOT}/{dept}/index.jsonl.gz", docs, res, f"{dept.upper()} orders: listing and load status", "govt_order_index")
    print(f"  {dept} done: {({k: v for k, v in res.items() if k != 'fails'})}", flush=True)
    for f in res["fails"]:
        print(f"    failed {f['url']}: {f['why']}", flush=True)


def status(dept: str, sample: int = 3) -> None:
    lib = Library(28, "")
    known = lib.known(f"{ROOT}/{dept}/")
    files = {p: b for p, b in known.items() if not p.endswith(".gz")}
    texts = sorted(p for p in known if "/text/" in p)
    rows = [r for p in texts for r in unpack(lib.get("drive", p) or b"")]
    print(f"  {dept}: files {len(files)} ({sum(files.values()) / 1e6:.1f} MB); text rows {len(rows)} in {len(texts)} files; "
          f"scan-only {sum(1 for r in rows if r.get('scan_only'))}; years {dict(sorted(Counter((r.get('date') or 'undated')[:4] for r in rows).items()))}")
    if f"{ROOT}/{dept}/index.jsonl.gz" in known:
        print(f"  last load: {unpack(lib.get('drive', f'{ROOT}/{dept}/index.jsonl.gz'))[0]}")
    for r in random.sample(rows, min(sample, len(rows))):
        print(f"  sample: {r['title'][:80]} | {r.get('number')} | {r.get('date')} | {r['source_url']} | {r['file']} | "
              f"scan_only={r['scan_only']} | {(r['text'] or '')[:150]!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["dopt", "cvc", "status", "list"])
    ap.add_argument("dept", nargs="?", choices=["dopt", "cvc"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4, help="threads that read text and write to Drive (downloads stay one at a time)")
    a = ap.parse_args()
    if a.cmd in ("dopt", "cvc"):
        load(a.cmd, a.limit, a.workers)
    elif a.cmd == "list":
        for d in [a.dept] if a.dept else ["dopt", "cvc"]:
            docs, notes = (dopt_listing if d == "dopt" else cvc_listing)(Site(pause=1.5))
            print(f"  {d}: {len(docs)} documents; {notes}; years {dict(sorted(Counter(x['subfolder'] for x in docs).items()))}")
    else:
        for d in [a.dept] if a.dept else ["dopt", "cvc"]:
            status(d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
