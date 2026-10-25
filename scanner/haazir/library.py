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
    a = ap.parse_args()
    if a.cmd == "judgments":
        load(a.first, a.last or a.first)
    elif a.cmd == "acts":
        load_acts(a.only or None)
    else:
        with httpx.Client(follow_redirects=True) as client:
            rows = year_rows(client, a.year)
        print(f"{len(rows)} judgments")
        for r in rows[:5]:
            print(json.dumps(r, ensure_ascii=False, indent=1)[:1200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
