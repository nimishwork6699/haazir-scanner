"""Item 31 of the library plan: bill history: the Statement of Objects and Reasons (SOR) of central bills.

    python -m haazir.web_bills sansad [--limit N] [--year 2023 ...]   # Government Bills of both Houses, as introduced
    python -m haazir.web_bills list                                  # what sansad.in lists (no download)
    python -m haazir.web_bills status

Source: Parliament's own site, sansad.in (Digital Sansad). Its bill pages are a JavaScript app; the list behind them is
sansad.in/api_rs/legislation/getBills (no login, no captcha): every bill with its number, House, ministry, dates,
status, the Act it became, and the PDF of the bill as introduced, which carries the SOR. Only Government Bills are
taken (Private Members' Bills rarely become Acts). PRS Legislative Research (prsindia.org/billtrack) was not used: its
robots.txt asks for a 10-second gap and it republishes the same Parliament PDFs.

Google Drive library/bills/<year>/<file>.pdf (as published) and library/bills/text/<year>.jsonl.gz: one row per bill
with title, number, House, date introduced, ministry, status, act number/year, the SOR text (from the heading
'STATEMENT OF OBJECTS AND REASONS' to the next heading) and the full text. A bill listed without a PDF is noted in
library/bills/index.jsonl.gz, not invented.
"""

import argparse
import random
import re
import sys
from collections import Counter

from .storage import pack, unpack
from .web_common import Library, Site, now, run_docs

ROOT = "library/bills"
API = "https://sansad.in/api_rs/legislation/getBills"
SOURCE = "Parliament of India, Digital Sansad (sansad.in)"
SOR = re.compile(r"S\s*T\s*A\s*T\s*E\s*M\s*E\s*N\s*T\s+O\s*F\s+O\s*B\s*J\s*E\s*C\s*T\s*S\s+A\s*N\s*D\s+R\s*E\s*A\s*S\s*O\s*N\s*S?")
# What follows the SOR in a bill as introduced.
AFTER = re.compile(r"^\s*(FINANCIAL\s+MEMORANDUM|MEMORANDUM\s+REGARDING\s+DELEGATED\s+LEGISLATION|ANNEXURE|NOTES\s+ON\s+CLAUSES|"
                   r"PRESIDENT['’]?S\s+RECOMMENDATION|RECOMMENDATION\s+OF\s+THE\s+PRESIDENT|EXTRACTS?\s+FROM|"
                   r"LOK\s+SABHA\s*$|RAJYA\s+SABHA\s*$|A\s*$\n\s*BILL)", re.M | re.I)
# The SOR is signed off: 'NEW DELHI; The 5th December, 2023.' then the Minister's name in capitals ending in a full stop.
SIGNED = re.compile(r"NEW\s+DELHI\s*[;:,.]?")
NAME = re.compile(r"^\s*[A-Z][A-Z.’' -]{3,}\.\s*$", re.M)


def sor_of(text: str) -> str | None:
    """The Statement of Objects and Reasons as printed in the bill, or None when the bill has no such heading."""
    m = SOR.search(text or "")
    if not m:
        return None
    rest = text[m.end():]
    end = AFTER.search(rest, 40)
    stop = end.start() if end else min(len(rest), 20000)
    s = SIGNED.search(rest, 0, stop)
    if s:
        n = NAME.search(rest, s.end(), min(stop, s.end() + 400))
        if n:
            stop = n.end()
    return re.sub(r"\n{3,}", "\n\n", rest[:stop]).strip()[:20000] or None


class BillSite(Site):
    """sansad.in answers a missing bill file with HTTP 500 {"errorCode":1001}: try such a file twice, not five times
    with long waits (a run of missing old files otherwise costs minutes each)."""

    def get(self, url: str, method: str = "GET", tries: int = 5, **kw):
        return super().get(url, method, 2 if "/getFile/" in url else tries, **kw)


def _d(s: str | None) -> str | None:
    s = (s or "").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})$", s)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def listing(site: Site) -> tuple[list[dict], list[dict], dict]:
    params = {"loksabha": "", "sessionNo": "", "billName": "", "house": "", "ministryName": "", "billType": "Government", "billCategory": "",
              "billStatus": "", "introductionDateFrom": "", "introductionDateTo": "", "passedInLsDateFrom": "", "passedInLsDateTo": "",
              "passedInRsDateFrom": "", "passedInRsDateTo": "", "page": 1, "size": 10000, "locale": "en", "sortOn": "billIntroducedDate", "sortBy": "desc"}
    j = site.get(API, params=params).json()
    recs = j.get("records") or []
    total = (j.get("_metadata") or {}).get("totalElements")
    docs, without, seen = [], [], set()
    for x in recs:
        # Square brackets in some file names must be percent-encoded (as a browser does), or the server answers 400.
        url = (x.get("billIntroducedFile") or "").strip().replace("[", "%5B").replace("]", "%5D")
        date = _d(x.get("billIntroducedDate"))
        year = str(x.get("billYear") or (date or "")[:4] or "undated")
        meta = {"title": (x.get("billName") or "").strip(), "number": f"Bill No. {x.get('billNumber')} of {x.get('billYear')} ({x.get('billIntroducedInHouse')})",
                "date": date, "body": f"Parliament of India, {x.get('billIntroducedInHouse')}", "category": f"{x.get('billType')} Bill",
                "bill_number": x.get("billNumber"), "bill_year": x.get("billYear"), "house": x.get("billIntroducedInHouse"),
                "ministry": x.get("ministryName"), "bill_category": x.get("billCategory"), "bill_status": x.get("status"),
                "act_number": (x.get("actNo") or "").strip() or None, "act_year": x.get("actYear"), "assented": _d(x.get("billAssentedDate")),
                "passed_ls": _d(x.get("billPassedInLSDate")), "passed_rs": _d(x.get("billPassedInRSDate")),
                "other_files": {k: x[k] for k in ("billPassedInLSFile", "billPassedInRSFile", "billPassedInBothHousesFile", "billGazettedFile",
                                                  "reportFile", "billSynopsisFile", "errataFile") if x.get(k)} or None,
                "listed_on": "https://sansad.in/ls/legislation/bills", "court": "parliament"}
        if not url:
            without.append(meta)
            continue
        if url in seen:
            continue
        seen.add(url)
        docs.append({"url": url, **meta, "subfolder": year})
    notes = {"listed_records": len(recs), "site_total": total, "with_pdf": len(docs), "duplicates": len(recs) - len(docs) - len(without),
             "without_pdf": len(without)}
    return docs, without, notes


def load(limit: int | None, years: list[str] | None, workers: int = 4) -> None:
    site = BillSite(pause=1.5, timeout=300)
    lib = Library(31, SOURCE)
    docs, without, notes = listing(site)
    print(f"  sansad.in Government Bills: {notes}", flush=True)
    todo = [d for d in docs if not years or d["subfolder"] in years]
    todo.sort(key=lambda d: d["subfolder"], reverse=True)
    res = run_docs(lib, site, todo, store="drive", folder=ROOT, kind="bill", text_kind="bill_text", save_every=25, limit=limit, workers=workers,
                   group_of=lambda d: ("drive", f"{ROOT}/text/{d['subfolder']}.jsonl.gz", f"Central bills {d['subfolder']}: text and SOR"),
                   group_meta=lambda d: {"year": int(d["subfolder"]) if d["subfolder"].isdigit() else None})
    sor_fill(lib, years)
    res["listing_notes"] = notes
    failed = {f["url"]: f["why"] for f in res["fails"]}
    rows = [{**{k: v for k, v in d.items() if k != "subfolder"}, "load": "failed: " + failed[d["url"]] if d["url"] in failed else "ok"} for d in docs]
    rows += [{**w, "url": None, "load": "no PDF listed at source"} for w in without]
    data = pack([{"listed_at": now(), "listing": notes, "result": {k: v for k, v in res.items() if k != "fails"}}] + rows)
    lib.put("drive", f"{ROOT}/index.jsonl.gz", data, "bill_index", title="Central bills: listing and load status", rows=len(rows))
    print(f"  done: {({k: v for k, v in res.items() if k != 'fails'})}", flush=True)
    for f in res["fails"]:
        print(f"    failed {f['url']}: {f['why']}", flush=True)


def sor_fill(lib: Library, years: list[str] | None = None, force: bool = False) -> None:
    """Add the SOR (cut from each bill's text) to the text rows that lack it."""
    for path in sorted(p for p in lib.known(f"{ROOT}/text/")):
        y = path.rsplit("/", 1)[1].split(".")[0]
        if years and y not in years:
            continue
        rows = unpack(lib.get("drive", path) or b"")
        changed = 0
        for r in rows:
            if force or "sor" not in r:
                r["sor"] = sor_of(r.get("text") or "")
                changed += 1
        if changed:
            data = pack(sorted(rows, key=lambda r: (r.get("date") or "", r.get("title") or "")))
            lib.put("drive", path, data, "bill_text", title=f"Central bills {y}: text and SOR", rows=len(rows), year=int(y) if y.isdigit() else None)


def status(sample: int = 3) -> None:
    lib = Library(31, SOURCE)
    known = lib.known(f"{ROOT}/")
    files = {p: b for p, b in known.items() if not p.endswith(".gz")}
    texts = sorted(p for p in known if "/text/" in p)
    rows = [r for p in texts for r in unpack(lib.get("drive", p) or b"")]
    print(f"  bill PDFs {len(files)} ({sum(files.values()) / 1e9:.2f} GB); text rows {len(rows)} in {len(texts)} files "
          f"({sum(known[p] for p in texts) / 1e6:.1f} MB); scan-only {sum(1 for r in rows if r.get('scan_only'))}; "
          f"with SOR {sum(1 for r in rows if r.get('sor'))}")
    if f"{ROOT}/index.jsonl.gz" in known:
        print(f"  last load: {unpack(lib.get('drive', f'{ROOT}/index.jsonl.gz'))[0]}")
    for r in random.sample(rows, min(sample, len(rows))):
        print(f"  sample: {r['title'][:80]} | {r['number']} | {r['date']} | {r['source_url']} | scan_only={r['scan_only']} | "
              f"SOR: {(r.get('sor') or '')[:200]!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["sansad", "list", "status", "sor"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--year", nargs="*")
    ap.add_argument("--workers", type=int, default=4, help="threads that read text and write to Drive (downloads stay one at a time)")
    ap.add_argument("--force", action="store_true", help="sor: cut the SOR again for every row")
    a = ap.parse_args()
    if a.cmd == "sansad":
        load(a.limit, a.year, a.workers)
    elif a.cmd == "list":
        docs, without, notes = listing(Site(pause=1.5))
        print(f"  {notes}; per decade {dict(sorted(Counter(d['subfolder'][:3] + '0s' for d in docs).items()))}")
    elif a.cmd == "sor":
        sor_fill(Library(31, SOURCE), a.year, a.force)
    else:
        status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
