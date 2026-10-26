"""The judgment library (Phase 6): every Supreme Court judgment from 1950, indexed from the court's own records.

The source is the public AWS Open Data copy of the Supreme Court's judgments (bucket indian-supreme-court-judgments,
CC BY 4.0): one small file of details per year, and one PDF per judgment. Each judgment's record carries the court's
own headnote ("Motor Vehicles Act, 1988: Section 166 ..."), so the acts and sections a judgment deals with are read by
code from the court's words, with no AI and no PDF download. A PDF is opened only when a draft needs that judgment.

    python -m haazir.library judgments 1950 2026      # load or refresh those years
    python -m haazir.library show 1990                 # read one year, save nothing
    python -m haazir.library acts ["MV Act" CPC]       # bare acts from India Code, section by section
"""

import argparse
import html as htmllib
import io
import json
import re
import sys
import tarfile
import time
from datetime import date, datetime, timezone
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from .acts import ACTS
from .db import DB

BUCKET = "https://indian-supreme-court-judgments.s3.amazonaws.com"
SOURCE = "Supreme Court of India judgments (AWS Open Data, CC BY 4.0)"


def pdf_url(year: int, path: str) -> str:
    return f"{BUCKET}/data/pdf/year={year}/english/{path}_EN.pdf"


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(s or "").replace("\xad", "")).strip()


def iso(d: str | None) -> str | None:
    m = re.match(r"\s*(\d{1,2})-(\d{1,2})-(\d{4})", d or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def _act_patterns() -> list[tuple[re.Pattern, str]]:
    """Every way an act is written (full name with or without its year, and its short forms), longest first."""
    def words(phrase: str) -> str:
        return r"\s+".join(re.escape(w) for w in phrase.split())

    out = []
    for short, full, aliases in ACTS:
        name, year = full.lower().rsplit(", ", 1)
        ways = [words(name) + r"\s*,?\s*" + year] + [words(a) for a in aliases]
        for w in ways:
            out.append((re.compile(r"(?<![\w])" + w + r"(?![\w])"), short))
    out.append((re.compile(r"\barticles?\s+\d+[a-z]?(?:\s*\(\w+\))*(?:\s*(?:,|and)\s*\d+[a-z]?(?:\s*\(\w+\))*)*\s+of\s+the\s+constitution\b"), "Constitution"))
    return sorted(out, key=lambda p: -len(p[0].pattern))


# One provision number: 166, 166(3), 13(1)(b), 5A, 437-A.
# A letter after the number belongs to it only when no word follows ("197-Sanction" is s. 197).
NUM = r"\d+(?:[A-Z]{1,2}(?![a-z]))?(?:-[A-Z](?![a-z]))?(?:\s*\(\s*[\w]+\s*\))*"
# Old scans put full stops where commas belong: "ss. 163-A. 166 and 168".
NUMS = rf"{NUM}(?:\s*(?:,|\.(?=\s*\d)|/|&|and|to|read with|r/w)\s*(?:(?:sections?|ss?\.|s\.)\s*)?{NUM})*"
AFTER = re.compile(rf"^\s*[,:\-–]?\s*(?:\(\s*)?(sections?|ss?\.|s\.|articles?|arts?\.|art\.|rules?|r\.|orders?|o\.)\s*({NUMS})", re.I)
BEFORE = re.compile(rf"(sections?|ss?\.|s\.|articles?|art\.|rules?|r\.)\s*({NUMS})\s+(?:of\s+)?(?:the\s+)?(?:said\s+)?$", re.I)
KIND = {"s": "s.", "ss": "s.", "section": "s.", "sections": "s.", "article": "Art.", "articles": "Art.", "art": "Art.", "arts": "Art.",
        "rule": "r.", "rules": "r.", "r": "r.", "order": "O.", "orders": "O.", "o": "O."}


def _numbers(kind: str, nums: str) -> list[str]:
    label = KIND.get(kind.lower().rstrip("."), "s.")
    parts = re.split(r"\s*(?:,|\.(?=\s*\d)|/|&|\band\b|\bread with\b|\br/w\b)\s*", nums)
    out = []
    for p in parts:
        p = re.sub(r"^(?:sections?|ss?\.|s\.)\s*", "", p.strip(), flags=re.I)
        if re.search(r"\bto\b", p):  # a range ("81 to 86") is not one provision
            continue
        # "163-A" and "163a" are s. 163A; clauses keep their case: 13(1)(b).
        p = re.sub(r"^(\d+)-?([a-z]{1,2})(?=\(|$)", lambda m: m.group(1) + m.group(2).upper(), re.sub(r"\s+", "", p), flags=re.I)
        if not re.match(r"\d", p):
            continue
        out.append(f"{label} {p}")
    return out


def tag(text: str) -> tuple[list[str], list[str]]:
    """The acts a headnote names (only acts in haazir.acts, so a tag is always a real act) and the provisions named
    beside each: (["MV Act"], ["MV Act s. 166", "MV Act s. 168"])."""
    low = (text or "").lower()
    taken: list[tuple[int, int]] = []
    hits: list[tuple[int, int, str]] = []
    for rx, short in PATTERNS:
        for m in rx.finditer(low):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue  # inside a longer name already found ("Hindu Succession Act" is not also "Succession Act")
            taken.append((m.start(), m.end()))
            # A different year beside the name is a different act: "Maharashtra Stamp Act, 1958" is not the
            # Indian Stamp Act, 1899; "Income Tax Act, 1922" is not the 1961 Act. "Ranbir Penal Code" is not the IPC.
            y = YEAR_AFTER.match(low[m.end():m.end() + 14])
            if y and re.sub(r"\D", "", y.group(1).replace("i", "1").replace("l", "1")) != YEARS[short]:
                continue
            if short == "IPC" and low[max(0, m.start() - 7):m.start()] == "ranbir ":
                continue
            hits.append((m.start(), m.end() + (len(y.group(0)) if y else 0), short))
    acts, sections = [], []
    for s, e, short in sorted(hits):
        if short not in acts:
            acts.append(short)
        found = []
        a = AFTER.match(text[e:e + 160])
        if a:
            found += _numbers(a.group(1), a.group(2))
        b = BEFORE.search(text[max(0, s - 120):s])
        if b:
            found += _numbers(b.group(1), b.group(2))
        if short == "Constitution" and low[s:e].startswith("article"):  # "Articles 14 and 21 of the Constitution"
            found += _numbers("article", " ".join(re.findall(r"\d+[A-Za-z]?(?:\s*\(\w+\))*", text[s:e])).replace(" ", ","))
        for f in found:
            item = f"{short} {f}"
            if item not in sections:
                sections.append(item)
    return acts[:20], sections[:40]


PATTERNS = _act_patterns()
YEARS = {short: full.rsplit(", ", 1)[1] for short, full, _ in ACTS}
# The year written after an act's name, also as old scans print it ("I 860").
YEAR_AFTER = re.compile(r"\s*[,.]?\s*((?:1[89]|[il]\s?[89]|20)\s?\d\d)\b")


def parse(raw: dict) -> dict | None:
    """One record of the court's list -> a library row."""
    soup = BeautifulSoup(raw.get("raw_html") or "", "html.parser")
    button = soup.find("button", id=re.compile(r"^link_"))
    if not button:
        return None
    strong = button.find("strong")
    parts = [clean(x) for x in re.split(r"\bversus\b", clean(strong.get_text(" ")) if strong else "", maxsplit=1)]
    petitioner = parts[0] if parts else ""
    respondent = parts[1] if len(parts) > 1 else ""
    citation = clean((button.find("span", class_="escrText") or soup.new_tag("x")).get_text())
    nc = clean((button.find("span", class_="ncDisplay") or soup.new_tag("x")).get_text())
    cnr_input = button.find("input", id="cnr")
    # Coram: the judges; the one marked * wrote the judgment.
    coram = next((s for s in soup.find_all("strong") if clean(s.get_text()).startswith("Coram")), None)
    judges, author = "", None
    if coram:
        sup = coram.find("sup")
        if sup:
            author = clean(sup.find_previous(string=True))
            sup.replace_with("")
        judges = clean(coram.get_text()).removeprefix("Coram :").removeprefix("Coram:").strip(" ,")
        author = author.removeprefix("Coram :").strip(" ,") if author else None
    # The headnote: the court's words between the coram and the case details.
    details = soup.find("strong", class_="caseDetailsTD")
    head = []
    node = coram.next_sibling if coram else None
    while node is not None and node is not details:
        head.append(node.get_text(" ") if hasattr(node, "get_text") else str(node))
        node = node.next_sibling
    headnote = clean(" ".join(head))
    facts: dict[str, str] = {}
    if details:
        spans = details.find_all("span")
        for s in spans:
            label = clean(s.get_text()).strip("| :")
            val = s.find_next("font")
            if label and val:
                facts[label] = clean(val.get_text())
    bench = re.match(r"(\d+)", facts.get("Bench", ""))
    acts, sections = tag(headnote)
    year = int(raw.get("citation_year") or raw.get("year") or 0) or None
    return {
        "court": "sc", "path": raw["path"], "year": year,
        "nc": re.sub(r"(\d{4})\s*(INSC)\s*(\d+)", r"\1 \2 \3", nc or raw.get("nc_display") or "") or None,
        "citation": citation or None, "title": f"{petitioner} v. {respondent}" if respondent else petitioner,
        "petitioner": petitioner or None, "respondent": respondent or None, "judges": judges or None, "author": author or None,
        "decided": iso(facts.get("Decision Date")), "case_no": facts.get("Case No") or None,
        "disposal": facts.get("Disposal Nature") or None, "bench": int(bench.group(1)) if bench else None,
        "headnote": headnote or None, "acts": acts, "sections": sections,
        "cnr": cnr_input.get("value") if cnr_input else None,
    }


def year_rows(client: httpx.Client, year: int) -> list[dict]:
    for attempt in range(3):
        try:
            r = client.get(f"{BUCKET}/metadata/tar/year={year}/metadata.tar", timeout=180)
            if r.status_code == 404:
                return []
            r.raise_for_status()
            break
        except httpx.HTTPError:
            if attempt == 2:
                raise
            time.sleep(10 * (attempt + 1))
    rows = []
    with tarfile.open(fileobj=io.BytesIO(r.content)) as t:
        for m in t.getmembers():
            if m.name.endswith(".json"):
                row = parse(json.load(t.extractfile(m)))
                if row and row["path"]:
                    rows.append(row)
    return rows


def load(first: int, last: int) -> None:
    db = DB()
    with httpx.Client(follow_redirects=True, headers={"User-Agent": "Haazir library (+https://haazir-pi.vercel.app)"}) as client:
        for year in range(first, last + 1):
            rows = year_rows(client, year)
            for i in range(0, len(rows), 500):
                db.insert("judgments", rows[i:i + 500], upsert_on="path")
            with_head = sum(1 for r in rows if r["headnote"])
            print(f"{year}: {len(rows)} judgments, {with_head} with a headnote, "
                  f"{sum(1 for r in rows if r['sections'])} with sections named", flush=True)


# ---------------------------------------------------------------- bare acts, from India Code

INDIA_CODE = "https://indiacode.gov.in/server/api"


def _md(item: dict, key: str) -> str:
    vals = item.get("metadata", {}).get(key) or []
    return (vals[0]["value"] if vals else "") or ""


def _norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"^\s*the\s+", "", (s or "").lower())).strip()


def _ic_search(client: httpx.Client, query: str, collection: str, page: int = 0, size: int = 100) -> tuple[int, list[dict]]:
    for attempt in range(4):
        try:
            r = client.get(f"{INDIA_CODE}/discover/search/objects",
                           params={"query": query, "size": size, "page": page, "f.identifier_collection": f"{collection},equals"}, timeout=90)
            r.raise_for_status()
            sr = r.json()["_embedded"]["searchResult"]
            return sr["page"]["totalElements"], [o["_embedded"]["indexableObject"] for o in sr["_embedded"]["objects"]]
        except (httpx.HTTPError, KeyError, ValueError):
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"India Code did not answer: {query}")


def _text(html: str) -> str:
    """India Code's section HTML -> plain text, one paragraph per line."""
    soup = BeautifulSoup(html or "", "html.parser")
    lines = [clean(p.get_text(" ")) for p in soup.find_all(["p", "li"])] or [clean(soup.get_text(" "))]
    return "\n".join(x for x in lines if x)


def central_act(client: httpx.Client, full: str) -> dict | None:
    """The central act on India Code whose title is this act's name ("Motor Vehicles Act, 1988")."""
    want = _norm_title(full)
    _, items = _ic_search(client, f'dc.identifier.state_name:CENTRAL AND dc.title:"{full}"', "ACT", size=50)
    for it in items:
        if _md(it, "dc.identifier.state_name").upper() == "CENTRAL" and _norm_title(it.get("name")) == want:
            return it
    return None


def load_acts(only: list[str] | None = None) -> None:
    db = DB()
    with httpx.Client(follow_redirects=True, headers={"User-Agent": "Haazir library (+https://haazir-pi.vercel.app)"}) as client:
        for short, full, _ in ACTS:
            if only and short not in only:
                continue
            act = central_act(client, full)
            if not act:
                print(f"{short}: not found on India Code as a central act", flush=True)
                continue
            act_id = _md(act, "dc.identifier.act_id")
            rows, page = [], 0
            while True:
                total, items = _ic_search(client, f'dc.identifier.act_id:"{act_id}"', "SECTION", page=page)
                for it in items:
                    if _md(it, "dc.identifier.act_id") != act_id:
                        continue
                    rows.append({"number": _md(it, "dc.identifier.section_number").strip() or "?",
                                 "title": clean(_md(it, "dc.title")) or None,
                                 "body": _text(_md(it, "dc.identifier.section_page_note")) or None,
                                 "notes": _text(_md(it, "dc.identifier.section_footnote")) or None,
                                 "order": float(_md(it, "dc.identifier.order_number") or 0)})  # "35.1": inserted sections
                page += 1
                if page * 100 >= total or not items:
                    break
                time.sleep(1)
            rows.sort(key=lambda r: r["order"])
            name, year = full.rsplit(", ", 1)
            db.insert("bare_acts", {"short": short, "name": full, "year": int(year), "act_id": act_id,
                                    "act_number": _md(act, "dc.identifier.act_number") or None,
                                    "ministry": _md(act, "dc.identifier.ministry_name") or None, "sections": len(rows),
                                    "loaded_at": datetime.now(timezone.utc).isoformat()}, upsert_on="short")
            db._check(db.http.delete(f"/act_sections?act=eq.{quote(short)}"))
            for i in range(0, len(rows), 200):
                db.insert("act_sections", [{"act": short, "ord": n, "number": r["number"], "title": r["title"], "body": r["body"], "notes": r["notes"]}
                                           for n, r in enumerate(rows[i:i + 200], start=i + 1)])
            print(f"{short}: {len(rows)} sections", flush=True)
            time.sleep(1)


# ---------------------------------------------------------------- summaries as files (items 12 and 13, Backblaze)

HC_BUCKET = "https://indian-high-court-judgments.s3.amazonaws.com"
HC_SOURCE = "High Courts of India, from their judgment records (AWS Open Data copy, CC BY 4.0)"


def sc_to_files() -> None:
    """Item 12: the Supreme Court summaries (read from the court's records into Supabase by `judgments`) written to
    Backblaze: one file per year, one file with all of them (the website searches it in memory), and a section index."""
    from .storage import B2, pack
    db, b2 = DB(), B2()
    rows = db.select("judgments", "select=path,year,nc,citation,title,petitioner,respondent,judges,author,decided,case_no,disposal,bench,headnote,acts,sections,cnr&order=path")
    by_year: dict[int, list] = {}
    for r in rows:
        r = {k: v for k, v in r.items() if v not in (None, [], "")}
        r["court"] = "sc"
        by_year.setdefault(r.get("year") or 0, []).append(r)
    for y, rs in sorted(by_year.items()):
        data = pack(rs)
        path = f"judgments/sc/{y}.jsonl.gz"
        b2.put(path, data, "application/gzip")
        _record(db, path, "judgments", 12, data, title=f"Supreme Court {y}", court="sc", year=y, rows=len(rs), source=SOURCE)
    everything = pack([r for rs in by_year.values() for r in rs])
    b2.put("judgments/sc/all.jsonl.gz", everything, "application/gzip")
    _record(db, "judgments/sc/all.jsonl.gz", "judgments", 12, everything, title="Supreme Court, all years", court="sc", rows=len(rows), source=SOURCE)
    index: dict[str, list] = {}
    for r in rows:
        for s in r.get("sections") or []:
            index.setdefault(s, []).append(r["path"])
    data = pack([{"section": s, "judgments": ids} for s, ids in sorted(index.items())])
    b2.put("judgments/sc/sections.jsonl.gz", data, "application/gzip")
    _record(db, "judgments/sc/sections.jsonl.gz", "judgment_index", 12, data, title="Supreme Court: judgments by section", court="sc", rows=len(index), source=SOURCE)
    print(f"Supreme Court: {len(rows):,} summaries in {len(by_year)} year files ({len(everything) / 1e6:.1f} MB all), {len(index):,} sections indexed", flush=True)


def _record(db: DB, path: str, kind: str, item: int, data: bytes, **kw) -> None:
    db.insert("library_files", {"path": path, "store": "b2", "kind": kind, "item": item, "bytes": len(data),
                                "updated_at": datetime.now(timezone.utc).isoformat(), **kw}, upsert_on="path")


def _hc_row(raw: dict, name: str, court: str, bench: str) -> dict | None:
    soup = BeautifulSoup(raw.get("raw_html") or "", "html.parser")
    text = clean(soup.get_text(" | "))
    parts = [clean(p) for p in text.split("|") if clean(p)]
    if not parts:
        return None
    first = parts[0]
    m = re.match(r"(.+?)\s+of\s+(.+?)\s+Vs\.?\s+(.+)$", first)
    facts = {}
    labels = ("CNR", "Date of registration", "Decision Date", "Disposal Nature")
    for i, p in enumerate(parts[:-1]):
        nxt = parts[i + 1]
        # A label with no value is followed straight by the next label ("Disposal Nature : | Court : …").
        if p.rstrip(" :") in labels and not nxt.startswith(("Court :", "Judge")) and nxt.rstrip(" :") not in labels:
            facts[p.rstrip(" :")] = nxt
    judge = next((p.split(":", 1)[1].strip() for p in parts if p.startswith("Judge")), None)
    court_part = next((p for p in parts if p.startswith("Court :")), "")
    snippet = next((p for p in parts[1:] if len(p) > 60 and not p.startswith(("Judge", "Court :"))), None)
    stem = (raw.get("pdf_link") or "").rsplit("/", 1)[-1].removesuffix(".pdf")
    return {"id": stem, "court": court, "court_name": name or court_part.removeprefix("Court :").strip() or None, "bench": bench,
            "case_no": m.group(1).strip() if m else first[:120], "petitioner": m.group(2).strip() if m else None,
            "respondent": m.group(3).strip() if m else None, "judges": judge, "cnr": facts.get("CNR"),
            "registered": iso(facts.get("Date of registration")), "decided": iso(facts.get("Decision Date")),
            "disposal": facts.get("Disposal Nature"), "snippet": snippet[:500] if snippet else None, "pdf": raw.get("pdf_link")}


def hc_keys(year: int) -> list[str]:
    """The High Court record files for a year (one per court bench; some benches split into parts)."""
    import xml.etree.ElementTree as ET
    ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
    out, token = [], None
    with httpx.Client(timeout=120) as c:
        while True:
            params = {"list-type": "2", "prefix": f"metadata/tar/year={year}/", "max-keys": "1000"}
            if token:
                params["continuation-token"] = token
            root = ET.fromstring(c.get(f"{HC_BUCKET}/", params=params).content)
            out += [k.find(f"{ns}Key").text for k in root.findall(f"{ns}Contents")]
            if root.find(f"{ns}IsTruncated").text != "true":
                return [k for k in out if re.search(r"\.tar(\.gz)?$", k) and "part-mobile" not in k] or [k for k in out if k.endswith(".tar")]


def hc_to_files(years: list[int], dry: bool = False) -> None:
    """Item 13: summaries of every High Court judgment and order in the public copy, one file per court per year."""
    from .storage import B2, pack
    db, b2 = DB(), B2()
    for year in years:
        by_court: dict[str, list] = {}
        names: dict[str, str] = {}
        total, courts = 0, 0

        def flush(court: str) -> None:
            """A court's year written as soon as its benches are read (the list comes court by court), so a busy year
            is never held in memory all at once."""
            nonlocal total, courts
            seen, uniq = set(), []
            for row in by_court.pop(court, []):  # a bench split into parts can repeat a record
                if row["id"] not in seen:
                    seen.add(row["id"]); uniq.append(row)
            total += len(uniq); courts += 1
            if dry or not uniq:
                return
            data = pack(uniq)
            path = f"judgments/hc/{court}/{year}.jsonl.gz"
            b2.put(path, data, "application/gzip")
            _record(db, path, "judgments", 13, data, title=f"{names.get(court, court)} {year}", court=court, year=year, rows=len(uniq), source=HC_SOURCE)

        with httpx.Client(timeout=600) as c:
            for key in hc_keys(year):
                m = re.search(r"court=([\d_]+)/bench=([^/]+)/", key)
                if not m:
                    continue
                court, bench = m.group(1), m.group(2)
                for done in [k for k in by_court if k != court]:
                    flush(done)
                r = c.get(f"{HC_BUCKET}/{key}")
                if r.status_code != 200:
                    print(f"  {year} {key}: {r.status_code}", flush=True)
                    continue
                mode = "r:gz" if key.endswith(".gz") else "r:"
                try:
                    with tarfile.open(fileobj=io.BytesIO(r.content), mode=mode) as t:
                        for mem in t.getmembers():
                            if not mem.isfile() or not mem.name.endswith(".json"):
                                continue
                            raw = json.load(t.extractfile(mem))
                            row = _hc_row(raw, raw.get("court_name") or "", court, bench)
                            if row:
                                by_court.setdefault(court, []).append(row)
                                names[court] = row["court_name"] or names.get(court, court)
                except tarfile.TarError as exc:
                    print(f"  {year} {key}: {exc}", flush=True)
            for done in list(by_court):
                flush(done)
        print(f"{year}: {total:,} High Court judgments and orders in {courts} courts", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("judgments")
    j.add_argument("first", type=int)
    j.add_argument("last", type=int, nargs="?")
    s = sub.add_parser("show")
    s.add_argument("year", type=int)
    b = sub.add_parser("acts")
    b.add_argument("only", nargs="*", help="short names, e.g. \"MV Act\" CPC (all when none)")
    sub.add_parser("sc-files")
    h = sub.add_parser("hc-files")
    h.add_argument("first", type=int)
    h.add_argument("last", type=int, nargs="?")
    h.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if a.cmd == "judgments":
        load(a.first, a.last or a.first)
    elif a.cmd == "acts":
        load_acts(a.only or None)
    elif a.cmd == "sc-files":
        sc_to_files()
    elif a.cmd == "hc-files":
        hc_to_files(list(range(a.first, (a.last or a.first) + 1)), a.dry)
    else:
        with httpx.Client(follow_redirects=True) as client:
            rows = year_rows(client, a.year)
        print(f"{len(rows)} judgments")
        for r in rows[:5]:
            print(json.dumps(r, ensure_ascii=False, indent=1)[:1200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
