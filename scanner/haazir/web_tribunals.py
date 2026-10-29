"""Item 20 of the library plan: NCLT and NCLAT orders under the IBC, as IBBI publishes them (no captcha, no login).

    python -m haazir.web_tribunals list nclt        # read IBBI's listing (every bench) -> judgments/nclt/<year>.jsonl.gz
    python -m haazir.web_tribunals load nclt [--limit N] [--year 2026]   # PDFs + text -> Drive library/tribunals/nclt/
    python -m haazir.web_tribunals list nclat
    python -m haazir.web_tribunals load nclat
    python -m haazir.web_tribunals status           # listed vs stored, per year

Backblaze judgments/<tribunal>/<year>.jsonl.gz: one summary row per order (date, bench, parties, case number, the
order's subject as IBBI words it, IBBI's remark, PDF URL, and where the PDF is kept). Google Drive
library/tribunals/<tribunal>/<year>/<file>.pdf (as published) and library/tribunals/<tribunal>/text/<year>.jsonl.gz
(the text of each order). A run skips orders already in the text file, so it can be stopped and started again.
"""

import argparse
import re
import sys
from collections import Counter, defaultdict

from bs4 import BeautifulSoup

from .web_common import Library, Site, iso_date, now, run_docs
from .storage import pack, unpack

IBBI = "https://ibbi.gov.in"
SOURCE = "Insolvency and Bankruptcy Board of India (ibbi.gov.in), orders of the NCLT/NCLAT"
# The bench filter of IBBI's NCLT list (its own values). Orders of the NCLAT carry no bench there.
NCLT_BENCHES = {"25": "Principal Bench, New Delhi", "26": "New Delhi Bench II", "27": "New Delhi Bench III", "28": "New Delhi Bench IV",
                "45": "New Delhi Bench V", "46": "New Delhi Bench VI", "29": "Ahmedabad", "30": "Allahabad", "49": "Amaravati",
                "31": "Bengaluru", "32": "Chandigarh", "33": "Chennai", "47": "Cuttack", "34": "Guwahati", "35": "Hyderabad",
                "48": "Indore", "36": "Jaipur", "39": "Kochi", "37": "Kolkata", "38": "Mumbai", "40": "Others"}


def _unescape(url: str) -> str:
    """'&amp;amp;' (escaped twice, or more) -> '&'."""
    while "&amp;" in url:
        url = url.replace("&amp;", "&")
    return url


def _rows(html: str) -> list[dict]:
    # Older orders are linked unquoted, with spaces in the file name (href=/webadmin/pdf/order/2018/Jan/4th Jan 2018 in
    # the matter of … .pdf download>); a parser cuts that at the first space, so quote each whole name up to .pdf first.
    html = re.sub(r"href=([^\"'\s>][^\"'>]*?\.pdf)(?=\s|>)", lambda m: f'href="{m.group(1)}"', html, flags=re.I)
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    out = []
    for tr in (table.find_all("tr") if table else [])[1:]:
        tds = tr.find_all("td")
        a = tr.find("a", href=True)
        if len(tds) < 3 or not a:
            continue
        label = a.get_text(" ", strip=True)
        size = re.search(r"\(([\d.]+\s*[KMG]B)\)\s*$", label)
        subject = re.sub(r"\s*\([\d.]+\s*[KMG]B\)\s*$", "", label).strip()
        out.append({"date_shown": tds[1].get_text(" ", strip=True), "subject": subject,
                    "remarks": tds[3].get_text(" ", strip=True) if len(tds) > 3 else None,
                    # the page double-escapes '&' in old file names ('&amp;amp;'); the file itself is at the plain '&'
                    "url": _unescape(IBBI + a["href"] if a["href"].startswith("/") else a["href"]),
                    "size_listed": size.group(1) if size else None})
    return out


def _last_page(html: str) -> int:
    pages = [int(x) for x in re.findall(r"(?:[?&]|&amp;)page=(\d+)", html)]  # a filtered list links '?nclt=38&amp;page=2'
    return max(pages) if pages else 1


def _parts(subject: str) -> tuple[str | None, str | None]:
    """IBBI's subject 'In the matter of A vs. B, [C.P. (IB) No. 706/MB/2026]' -> parties, case number (as printed)."""
    m = re.match(r"\s*In the matter of\s+(.*?)\s*,?\s*\[(.*)\]\s*$", subject, re.I | re.S)
    if m:
        return m.group(1).strip(" ,") or None, m.group(2).strip() or None
    m = re.match(r"\s*In the matter of\s+(.*)$", subject, re.I | re.S)
    return (m.group(1).strip(" ,") if m else None), None


def listing(tribunal: str) -> None:
    site = Site(pause=1.5)
    url = f"{IBBI}/orders/{tribunal}"
    first = site.get(url).text
    total_pages = _last_page(first)
    last = _rows(site.get(url, params={"page": total_pages}).text)
    total = (total_pages - 1) * 20 + len(last)
    print(f"  {tribunal}: IBBI lists {total} orders on {total_pages} pages", flush=True)
    # One entry per listed row (date, subject, PDF): IBBI lists some PDFs under more than one entry.
    found: dict[tuple, dict] = {}
    # NCLT: each bench's list (to know the bench), then the whole list for any order filed under no bench.
    filters = (list(NCLT_BENCHES.items()) + [(None, None)]) if tribunal == "nclt" else [(None, None)]
    seen_rows = bench_rows = 0
    for code, bench in filters:
        if code is None and tribunal == "nclt" and bench_rows >= total:
            # every listed order came under some bench: the whole list (another 1,600 slow pages) adds nothing
            print(f"    benches hold {bench_rows} rows of the {total} listed: whole list not walked", flush=True)
            seen_rows = bench_rows
            continue
        params = {"nclt": code} if code else {}
        html = site.get(url, params=params).text
        pages = _last_page(html)
        got = 0
        for p in range(1, pages + 1):
            if p > 1:
                html = site.get(url, params={**params, "page": p}).text
            rows = _rows(html)
            if not rows and p < pages:
                print(f"    {bench or tribunal} page {p}: empty, trying again", flush=True)
                rows = _rows(site.get(url, params={**params, "page": p}).text)
            if code is None:
                seen_rows += len(rows)
            else:
                bench_rows += len(rows)
            for r in rows:
                key = (r["url"], r["date_shown"], r["subject"])
                cur = found.get(key)
                if cur:
                    if bench and bench not in cur["benches"]:
                        cur["benches"].append(bench)
                    continue
                parties, case_no = _parts(r["subject"])
                found[key] = {"date": iso_date(r["date_shown"]), "date_shown": r["date_shown"], "tribunal": tribunal.upper(),
                              "benches": [bench] if bench else [], "subject": r["subject"], "parties": parties,
                              "case_number": case_no, "remarks": r["remarks"], "pdf_url": r["url"], "size_listed": r["size_listed"]}
                got += 1
        print(f"    {bench or 'whole list'}: {pages} pages, {got} new entries (so far {len(found)})", flush=True)
    print(f"  {tribunal}: rows read on the whole list {seen_rows}; distinct entries {len(found)}; distinct PDFs {len({k[0] for k in found})}", flush=True)
    by_year: dict[str, list] = defaultdict(list)
    for r in found.values():
        r["bench"] = "; ".join(r["benches"]) or None
        by_year[r["date"][:4] if r["date"] else "undated"].append(r)
    lib = Library(20, SOURCE)
    old = lib.known(f"judgments/{tribunal}/")
    for year, rows in sorted(by_year.items()):
        path = f"judgments/{tribunal}/{year}.jsonl.gz"
        prev = {}
        if path in old:
            prev = {r["pdf_url"]: r for r in unpack(lib.get("b2", path) or b"") if r.get("pdf_url")}
        for r in rows:  # keep where each PDF is already stored
            if prev.get(r["pdf_url"], {}).get("file"):
                r["file"] = prev[r["pdf_url"]]["file"]
        rows.sort(key=lambda r: (r["date"] or "", r["subject"]))
        data = pack(rows)
        lib.put("b2", path, data, "judgments", title=f"{tribunal.upper()} orders {year} (IBBI)", court=tribunal,
                year=int(year) if year.isdigit() else None, rows=len(rows))
    stamp = {"tribunal": tribunal, "listed_at": now(), "ibbi_total": total, "found": len(found), "whole_list_rows_read": seen_rows,
             "distinct_pdfs": len({k[0] for k in found}),
             "per_year": {y: len(v) for y, v in sorted(by_year.items())}}
    data = pack([stamp])
    lib.put("b2", f"judgments/{tribunal}/_listing.jsonl.gz", data, "judgment_index", title=f"{tribunal.upper()} orders: IBBI listing check",
            court=tribunal, rows=len(found))
    print(f"  {tribunal}: {len(found)} distinct entries found of {total} listed; years {dict(Counter({y: len(v) for y, v in by_year.items()}))}", flush=True)


def load(tribunal: str, limit: int | None, years: list[str] | None, workers: int) -> None:
    lib = Library(20, SOURCE)
    site = Site(pause=1.0, timeout=300)
    summaries = sorted(p for p in lib.known(f"judgments/{tribunal}/") if re.search(r"/(\d{4}|undated)\.jsonl\.gz$", p))
    total = Counter()
    for path in sorted(summaries, reverse=True):  # newest first: the most useful orders land first
        year = path.rsplit("/", 1)[1].split(".")[0]
        if years and year not in years:
            continue
        rows = unpack(lib.get("b2", path) or b"")
        docs = [{"url": r["pdf_url"], "title": r["subject"], "number": r.get("case_number"), "date": r.get("date"),
                 "body": f"{r['tribunal']}{', ' + r['bench'] if r.get('bench') else ''}", "category": r.get("remarks"),
                 "parties": r.get("parties"), "bench": r.get("bench"), "court": tribunal, "subfolder": year} for r in rows]
        print(f"  {tribunal} {year}: {len(docs)} orders", flush=True)
        res = run_docs(lib, site, docs, store="drive", folder=f"library/tribunals/{tribunal}", kind="tribunal_order",
                       text_kind="tribunal_text", workers=workers, save_every=100, limit=limit,
                       group_of=lambda d, y=year: ("drive", f"library/tribunals/{tribunal}/text/{y}.jsonl.gz", f"{tribunal.upper()} orders {y}: text"),
                       group_meta=lambda d, y=year: {"court": tribunal, "year": int(y) if y.isdigit() else None})
        # Mark in the summary file where each PDF now is.
        text = unpack(lib.get("drive", f"library/tribunals/{tribunal}/text/{year}.jsonl.gz") or b"")
        where = {r["source_url"]: r["file"] for r in text}
        changed = 0
        for r in rows:
            if where.get(r["pdf_url"]) and r.get("file") != where[r["pdf_url"]]:
                r["file"] = where[r["pdf_url"]]
                r["store"] = "drive"
                changed += 1
        if changed:
            data = pack(rows)
            lib.put("b2", path, data, "judgments", title=f"{tribunal.upper()} orders {year} (IBBI)", court=tribunal,
                    year=int(year) if year.isdigit() else None, rows=len(rows))
        for k in ("listed", "new", "already", "failed", "scan_only"):
            total[k] += res[k]
        print(f"  {tribunal} {year}: {res['new']} new, {res['already']} already, {res['failed']} failed, {res['scan_only']} scan-only", flush=True)
        for f in res["fails"][:20]:
            print(f"    failed {f['url']}: {f['why']}", flush=True)
    print(f"  {tribunal} total: {dict(total)}", flush=True)


def status(tribunal: str) -> None:
    lib = Library(20, SOURCE)
    known = lib.known(f"judgments/{tribunal}/")
    stored = lib.known(f"library/tribunals/{tribunal}/")
    pdfs = Counter(p.split("/")[3] for p in stored if p.endswith(".pdf") and "/text/" not in p)
    for p in sorted(known):
        if p.endswith("_listing.jsonl.gz"):
            print(f"  {tribunal} listing: {unpack(lib.get('b2', p))[0]}")
            continue
        y = p.rsplit("/", 1)[1].split(".")[0]
        rows = unpack(lib.get("b2", p) or b"")
        print(f"  {tribunal} {y}: listed {len(rows)}, PDFs stored {pdfs.get(y, 0)}, summary {known[p] / 1e3:.0f} KB")
    print(f"  {tribunal}: Drive bytes {sum(v for k, v in stored.items()) / 1e9:.2f} GB in {len(stored)} files")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["list", "load", "status"])
    ap.add_argument("tribunal", nargs="?", choices=["nclt", "nclat"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--year", nargs="*")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    tribunals = [a.tribunal] if a.tribunal else ["nclat", "nclt"]  # the smaller set first
    for t in tribunals:
        if a.cmd == "list":
            listing(t)
        elif a.cmd == "load":
            load(t, a.limit, a.year, a.workers)
        else:
            status(t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
