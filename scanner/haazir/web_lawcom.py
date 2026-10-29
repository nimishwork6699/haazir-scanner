"""Item 21 of the library plan: every Law Commission of India report, from the Commission's own report listings
(lawcommissionofindia.nic.in/report_first/ … /report_twentysecond/; PDFs on cdnbbsr.s3waas.gov.in).

    python -m haazir.web_lawcom list              # what the site lists, per Commission (no download)
    python -m haazir.web_lawcom load [--limit N]  # PDFs -> Drive library/law-commission/<nn>-<commission>/, text ->
                                                  #   library/law-commission/text/commission-<nn>.jsonl.gz
    python -m haazir.web_lawcom status            # listed vs stored

Each PDF is one row (a report published in parts, or with a Hindi copy, has one row per file; 'part' says which).
Old reports are often scans: kept with empty text and scan_only = true, never OCR'd.
"""

import argparse
import random
import re
import sys
from collections import Counter

from bs4 import BeautifulSoup

from .storage import unpack
from .web_common import Library, Site, index_file, iso_date, run_docs

BASE = "https://lawcommissionofindia.nic.in"
SOURCE = "Law Commission of India (lawcommissionofindia.nic.in)"
COMMISSIONS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh",
               "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth",
               "twentieth", "twentyfirst", "twentysecond", "twentythird", "twentyfourth"]
ROOT = "library/law-commission"


def _date(cell: str) -> tuple[str | None, int | None]:
    """'1956' -> (None, 1956); '17 th March 2023' -> ('2023-03-17', 2023)."""
    cell = re.sub(r"(\d)\s*(st|nd|rd|th)\b", r"\1", cell.strip())
    d = iso_date(cell)
    if d:
        return d, int(d[:4])
    m = re.search(r"\b(19|20)\d{2}\b", cell)
    return None, int(m.group(0)) if m else None


def listing(site: Site) -> tuple[list[dict], dict]:
    docs, per = [], {}
    for n, name in enumerate(COMMISSIONS, 1):
        url = f"{BASE}/report_{name}/"
        r = site.get(url)
        if r.status_code == 404:
            per[name] = "no page (404)"
            continue
        table = BeautifulSoup(r.text, "lxml").find("table")
        rows = table.find_all("tr")[1:] if table else []
        reports = 0
        for tr in rows:
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            number = tds[0].get_text(" ", strip=True)
            subject = tds[1].get_text(" ", strip=True)
            date, year = _date(tds[2].get_text(" ", strip=True))
            links = [a for a in tds[3].find_all("a", href=True) if ".pdf" in a["href"].lower()]
            if not number and not links:
                continue
            reports += 1
            for a in links:
                label = re.sub(r"\(PDF[^)]*\)", "", a.get_text(" ", strip=True)).strip(" _")
                label = re.sub(r"^Accessible_?", "", label).strip(" _") or None
                if label and label.lower() == "click here":
                    label = None
                docs.append({"url": a["href"].strip(), "title": subject, "number": f"Report No. {number}", "report_no": number,
                             "date": date, "year": year, "body": f"Law Commission of India ({name.replace('twenty', 'twenty-')} Commission)",
                             "category": "Law Commission report", "commission": n, "part": label,
                             "language": "Hindi" if label and "hindi" in label.lower() else "English",
                             "listed_on": url, "subfolder": f"{n:02d}-{name}"})
        per[name] = f"{reports} reports, {sum(1 for d in docs if d['commission'] == n)} PDFs"
    return docs, per


def load(limit: int | None) -> None:
    site = Site(pause=1.5, timeout=300)
    lib = Library(21, SOURCE)
    docs, per = listing(site)
    numbers = sorted({int(d["report_no"]) for d in docs if d["report_no"].isdigit()})
    missing = [n for n in range(1, (numbers[-1] if numbers else 0) + 1) if n not in numbers]
    print(f"  listed {len(docs)} PDFs for {len(numbers)} reports; report numbers not listed: {missing}", flush=True)
    for k, v in per.items():
        print(f"    {k}: {v}", flush=True)
    res = run_docs(lib, site, docs, store="drive", folder=ROOT, kind="law_commission", text_kind="law_commission_text",
                   save_every=10, limit=limit,
                   group_of=lambda d: ("drive", f"{ROOT}/text/commission-{d['commission']:02d}.jsonl.gz",
                                       f"Law Commission reports, {d['subfolder'].split('-')[1]} Commission: text"),
                   group_meta=lambda d: {})
    res["missing_report_numbers"] = missing
    index_file(lib, "drive", f"{ROOT}/index.jsonl.gz", docs, res, "Law Commission reports: listing and load status", "law_commission_index")
    print(f"  done: {({k: v for k, v in res.items() if k != 'fails'})}", flush=True)
    for f in res["fails"]:
        print(f"    failed {f['url']}: {f['why']}", flush=True)


def status(sample: int = 3) -> None:
    lib = Library(21, SOURCE)
    known = lib.known(f"{ROOT}/")
    pdfs = {p: b for p, b in known.items() if not p.endswith(".gz")}
    texts = sorted(p for p in known if "/text/" in p)
    rows = [r for p in texts for r in unpack(lib.get("drive", p) or b"")]
    print(f"  PDFs stored: {len(pdfs)} ({sum(pdfs.values()) / 1e9:.2f} GB); text rows {len(rows)} in {len(texts)} files "
          f"({sum(known[p] for p in texts) / 1e6:.1f} MB); scan-only {sum(1 for r in rows if r.get('scan_only'))}; "
          f"per Commission {dict(Counter(r['commission'] for r in rows))}")
    if f"{ROOT}/index.jsonl.gz" in known:
        head = unpack(lib.get("drive", f"{ROOT}/index.jsonl.gz"))[0]
        print(f"  last load: {head}")
    for r in random.sample(rows, min(sample, len(rows))):
        print(f"  sample: {r['number']} | {r['title'][:70]} | {r.get('date') or r.get('year')} | {r['file']} | "
              f"scan_only={r['scan_only']} | {(r['text'] or '')[:150]!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["list", "load", "status"])
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    if a.cmd == "list":
        docs, per = listing(Site(pause=1.5))
        for k, v in per.items():
            print(f"  {k}: {v}")
        print(f"  {len(docs)} PDFs")
    elif a.cmd == "load":
        load(a.limit)
    else:
        status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
