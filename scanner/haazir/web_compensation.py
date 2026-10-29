"""Item 29 of the library plan: compensation inputs, as published by official sites.

    python -m haazir.web_compensation nalsa [--limit N]          # NALSA victim compensation scheme 2018 (amended), SC orders,
                                                                 #   victim schemes and NALSA's yearly victim-compensation returns
    python -m haazir.web_compensation states [code ...]          # state/UT victim compensation schemes from the SLSA sites
    python -m haazir.web_compensation clc                        # central minimum wages and VDA orders (Chief Labour Commissioner)
    python -m haazir.web_compensation wages [code ...] [--prune] # state minimum wage notifications from labour department sites
                                                                 #   (--prune: drop what the site no longer lists; only after a clean crawl)
    python -m haazir.web_compensation probe <url>                # what a page links that looks like a document, for adding a site
    python -m haazir.web_compensation status                     # listed vs stored per source, bytes per store
    python -m haazir.web_compensation verify [n]                 # n random documents read back and checked against the live files

Backblaze compensation/<source>/[<state>/]<file> keeps each document as published, compensation/<source>/[<state>/]text.jsonl.gz
one row per document (title, number, date, issuing body, category, text, source URL, where the file is kept, scan_only),
and index.jsonl.gz what the site listed and how each fared. A file over 10 MB, or any file once compensation/ nears its
50 MB budget, is kept in Google Drive library/compensation/… instead; its text row stays in Backblaze.
Wage rows carry the effective date only as printed in the notification ("with effect from …"), quoted; no wage figures
are lifted into tables. Scan-only PDFs are kept with empty text and scan_only = true, never OCR'd.
"""

import argparse
import random
import re
import sys
from collections import Counter
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .storage import pack, unpack
from .web_common import Blocked, Group, Library, Site, absolute, doc_row, fetch_doc, file_name, iso_date, now
from .web_courtrules import CurlSite, _clean, _date, _title

B2_BUDGET = 48_000_000  # compensation/ in Backblaze stays under about 50 MB
BIG = 10_000_000  # a single file this large goes to Drive

SOURCES = {
    "nalsa": "National Legal Services Authority (nalsa.gov.in)",
    "victim-schemes": "State Legal Services Authorities (sites listed by NALSA)",
    "central": "Chief Labour Commissioner (Central), Ministry of Labour and Employment (clc.gov.in)",
    "minimum-wages": "State labour departments",
}

# The SLSA sites, as nalsa.gov.in lists them (26 Oct 2026). Lakshadweep and Andaman & Nicobar are not listed there.
SLSA = {
    "ap": ("Andhra Pradesh", "https://andhrapradesh.nalsa.gov.in/"), "ar": ("Arunachal Pradesh", "https://arunachalpradesh.nalsa.gov.in/"),
    "as": ("Assam", "https://aslsa.assam.gov.in/"), "br": ("Bihar", "https://patnahighcourt.gov.in/bslsa/"),
    "ch": ("Chandigarh", "https://chandigarh.nalsa.gov.in/"), "cg": ("Chhattisgarh", "https://chhattisgarh.nalsa.gov.in/"),
    "dn": ("Dadra and Nagar Haveli", "https://dadraandnagarhaveli.nalsa.gov.in/"), "dd": ("Daman and Diu", "https://damananddiu.nalsa.gov.in/"),
    "dl": ("Delhi", "https://delhi.nalsa.gov.in/"), "ga": ("Goa", "https://goa.nalsa.gov.in/"), "gj": ("Gujarat", "https://gujarat.nalsa.gov.in/"),
    "hr": ("Haryana", "https://haryana.nalsa.gov.in/"), "hp": ("Himachal Pradesh", "https://himachalpradesh.nalsa.gov.in/"),
    "jk": ("Jammu and Kashmir", "https://jammukashmir.nalsa.gov.in/"), "jh": ("Jharkhand", "http://jhalsa.org/"),
    "ka": ("Karnataka", "https://karnataka.nalsa.gov.in/"), "kl": ("Kerala", "https://kerala.nalsa.gov.in/"),
    "la": ("Ladakh", "https://ladakh.nalsa.gov.in/"), "mp": ("Madhya Pradesh", "https://madhyapradesh.nalsa.gov.in/"),
    "mh": ("Maharashtra", "https://maharashtra.nalsa.gov.in/"), "mn": ("Manipur", "https://manipur.nalsa.gov.in/"),
    "ml": ("Meghalaya", "https://meghalaya.nalsa.gov.in/"), "mz": ("Mizoram", "https://mizoram.nalsa.gov.in/"),
    "nl": ("Nagaland", "https://nagaland.nalsa.gov.in/"), "od": ("Odisha", "http://www.oslsa.nic.in/"),
    "py": ("Puducherry", "https://puducherry.nalsa.gov.in/"), "pb": ("Punjab", "https://punjab.nalsa.gov.in/"),
    "rj": ("Rajasthan", "https://rajasthan.nalsa.gov.in/"), "sk": ("Sikkim", "https://sikkim.nalsa.gov.in/"),
    "tn": ("Tamil Nadu", "https://tamilnadu.nalsa.gov.in/"), "tg": ("Telangana", "https://telangana.nalsa.gov.in/"),
    "tr": ("Tripura", "https://slsa.tripura.gov.in/"), "uk": ("Uttarakhand", "https://uttarakhand.nalsa.gov.in/"),
    "up": ("Uttar Pradesh", "https://uttarpradesh.nalsa.gov.in/"), "wb": ("West Bengal", "https://westbengal.nalsa.gov.in/"),
}

# State labour department sites (the official home pages; the minimum wage pages are found from them).
LABOUR = {
    "dl": ("Delhi", ["https://labour.delhi.gov.in/"]),
    "mh": ("Maharashtra", ["https://mahakamgar.maharashtra.gov.in/"]),
    "up": ("Uttar Pradesh", ["https://uplabour.gov.in/"]),
    "ka": ("Karnataka", ["https://labour.karnataka.gov.in/"]),
    "tn": ("Tamil Nadu", ["https://labour.tn.gov.in/"]),
    "gj": ("Gujarat", ["https://col.gujarat.gov.in/"]),
    "wb": ("West Bengal", ["https://wblc.gov.in/"]),
    "hr": ("Haryana", ["https://hrylabour.gov.in/"]),
    "rj": ("Rajasthan", ["https://labour.rajasthan.gov.in/"]),
    "kl": ("Kerala", ["https://lc.kerala.gov.in/"]),
    "tg": ("Telangana", ["https://labour.telangana.gov.in/"]),
    "ap": ("Andhra Pradesh", ["https://labour.ap.gov.in/"]),
    "mp": ("Madhya Pradesh", ["https://labour.mp.gov.in/"]),
    "br": ("Bihar", ["https://state.bihar.gov.in/labour/"]),
    "pb": ("Punjab", ["https://pblabour.gov.in/"]),
    "od": ("Odisha", ["https://labdirodisha.gov.in/"]),
}

VICTIM = re.compile(r"victim\w*\W{0,3}(?:\w+\W+){0,4}compensat|compensat\w*\W+(?:\w+\W+){0,6}victim|357\s*-?\s*A|manodhairya|"
                    r"पीड़ित\s*प्रतिकर|पीडित\s*प्रतिकर|प्रतिकर\s*(?:स्कीम|योजना)|victim\s+assistance\s+(?:scheme|fund)", re.I)
VICTIM_FOLLOW = re.compile(r"victim|compensat|scheme|notification|प्रतिकर|योजना|पीड़ित", re.I)
WAGES = re.compile(r"minimum\s*-?\s*wage|min\.?\s*wage|\bV\.?D\.?A\b|variable\s+dearness|dearness\s+allowance|न्यूनतम\s*(?:मजदूरी|वेतन|पारिश्रमिक)|"
                   r"கூலி|ಕನಿಷ್ಠ\s*ವೇತನ|किमान\s*वेतन", re.I)
WAGES_FOLLOW = re.compile(r"minimum|wage|notification|\bact\b|acts|rules|न्यूनतम|अधिसूचना|download|publication|circular", re.I)
DOC = re.compile(r"\.(pdf|docx?|odt|rtf|xlsx?)(?:$|\?)", re.I)  # Telangana publishes its rates as .xls (kept; no text read)
EFFECTIVE = re.compile(r"(?:with\s+effect\s+from|\bw\.?\s*e\.?\s*f\b\.?|effective\s+from|shall\s+come\s+into\s+force\s+(?:on|from))\s*[:,]?\s*"
                       r"((?:the\s+)?\d{1,2}(?:st|nd|rd|th)?[\s./-]*(?:\d{1,2}|[A-Za-z]{3,9})[\s.,/-]*\d{2,4})", re.I)


# ---------------------------------------------------------------- fetching: httpx, or curl where Python's TLS fails


class Fetch:
    """One request at a time per site. Hosts whose certificate chain Python cannot complete are read through curl,
    which verifies against the Windows certificate store (verification stays on either way)."""

    def __init__(self, pause: float = 1.5) -> None:
        self.py = Site(pause=pause, timeout=90)
        self.curl = CurlSite(pause=pause, timeout=180)
        self.by_curl: set[str] = set()

    def get(self, url: str, tries: int = 3, **kw):
        host = urlparse(url).netloc
        if host in self.by_curl:
            return self.curl.get(url, tries=tries)
        try:
            return self.py.get(url, tries=tries, **kw)
        except Blocked as e:
            msg = str(e)
            if re.search(r"CERTIFICATE|SSL|ConnectError|handshake", msg, re.I) and "robots" not in msg:
                self.by_curl.add(host)
                return self.curl.get(url, tries=tries)
            raise


# ---------------------------------------------------------------- finding documents on a site


def _row_text(a) -> str:
    row = a.find_parent(["tr", "li"])
    return row.get_text(" ", strip=True)[:500] if row else a.get_text(" ", strip=True)


# A table row that reads like a notification (a number, a G.O., an order or a date), and rows plainly about something else.
NUMBERED = re.compile(r"\bNo\.|\bG\.?\s?O\b|notification|\border\b|अधिसूचना|आदेश|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", re.I)
OFFTOPIC = re.compile(r"vacanc|recruit|tender|newsletter|panel\s+lawyer|\bPLVs?\b|para.?legal|citizen\s+charter|cyber|\bRTI\b|holiday|"
                      r"transfer|internship|mediation|legal\s+services\s+authorities\s+act|guidelines\s+to\s+(?:user|fill|reply)|flowchart|"
                      r"checklist|gender", re.I)
CHROME = re.compile(r"header|logo|footer|menu|navbar|\bnav\b|sidebar|banner|slider|social", re.I)


def _chrome(a) -> bool:
    """A link in the site's header, logo strip, menus, footer or side panels: part of every page, not of this one."""
    for p in a.parents:
        if p.name in ("main", "article", "body", "html"):  # the page's own content (and the page's wrapper classes) end the search
            return False
        if p.name in ("header", "footer", "nav"):
            return True
        if CHROME.search(" ".join(p.get("class") or []) + " " + (p.get("id") or "")):
            return True
    return False


def _page_title(soup) -> str:
    h = soup.find("h1") or soup.find("h2")
    t = h.get_text(" ", strip=True) if h else ""
    if not t and soup.title:
        t = soup.title.get_text(" ", strip=True)
    return _clean(t)


def _same_site(url: str, home: str) -> bool:
    a, b = (urlparse(url).hostname or "").lower(), (urlparse(home).hostname or "").lower()  # 'host:443' is the same site
    a, b = a.removeprefix("www."), b.removeprefix("www.")
    return a == b


def crawl(f: Fetch, starts: list[str], topic: re.Pattern, follow: re.Pattern, max_pages: int = 40, depth: int = 2,
          path_prefix: str | None = None, take_all_on: list[str] | None = None) -> tuple[list[dict], list[str], list[str]]:
    """Pages reachable from starts (same site, links whose words match follow, up to depth) -> documents about the topic:
    a file linked from a page whose own title is on the topic, or whose link/row words are; a page on the topic with
    no file of its own is itself the document. Returns (docs, pages read, problems)."""
    queue = [(u, 0) for u in starts]
    seen_pages: set[str] = set()
    docs: dict[str, dict] = {}
    problems: list[str] = []
    while queue and len(seen_pages) < max_pages:
        url, d = queue.pop(0)
        key = url.split("#")[0].rstrip("/")
        if key in seen_pages:
            continue
        seen_pages.add(key)
        try:
            r = f.get(url, tries=2)
        except Blocked as e:
            problems.append(str(e)[:200])
            continue
        if r.status_code != 200:
            problems.append(f"HTTP {r.status_code} at {url}")
            continue
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and not r.content[:1000].lower().count(b"<html"):
            continue
        final = str(r.url)
        soup = BeautifulSoup(r.content, "lxml")
        title = _page_title(soup)
        titled = bool(topic.search(title))
        on_topic = titled or bool(topic.search(final)) or any(final.startswith(t) for t in (take_all_on or []))
        found_here = 0
        # On a page about the topic, every file in its main content counts; menus and side panels ('Cyber Dost') do not.
        main_part = soup.select_one("main, article, #main-content, #content, .entry-content, .content")
        in_main = set(map(id, main_part.find_all("a", href=True))) if main_part else None
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("javascript:", "mailto:", "#", "tel:")):
                continue
            u = absolute(final, href)
            words = a.get_text(" ", strip=True)
            row = _row_text(a)
            if DOC.search(urlparse(u).path) or "/uploads/" in u and DOC.search(u):
                # A file on an on-topic page counts when its link is a bare 'View'/'Download' or sits in the page's
                # table; a named link elsewhere ('Cyber Dost' in a side panel) must name the topic itself.
                generic = len(words) < 4 or re.match(r"(?i)^(view|download|click|open|read|pdf|english|hindi|हिंदी|अंग्रेज़ी)", words)
                numbered = a.find_parent("tr") is not None and NUMBERED.search(row)
                # A page whose own heading names the topic vouches for the files it lists; one that is on the topic
                # only by its address (TN's minimum-wages.php is headed 'Important G.Os') only for bare or numbered links.
                page_says = (on_topic and (in_main is None or id(a) in in_main) and not _chrome(a) and not OFFTOPIC.search(row)
                             and bool(titled or generic or numbered))
                if page_says or topic.search(words) or topic.search(row) or topic.search(u):
                    t = _title(a, row, u)
                    if not topic.search(t) and on_topic and title and t.lower() in ("view", "download", ""):
                        t = title
                    if t in ("View", "Download") or len(t) < 6:
                        t = title or t
                    docs.setdefault(u, {"url": u, "title": t, "date": _date(row, words), "page_url": final, "page_title": title})
                    found_here += 1
                continue
            if d >= depth or not _same_site(u, starts[0]) or (path_prefix and not urlparse(u).path.startswith(path_prefix)):
                continue
            if follow.search(words) or follow.search(urlparse(u).path) or topic.search(words) or re.search(r"[?&/]page[=/]\d", u):
                queue.append((u, d + 1))
        if on_topic and not found_here and final not in docs and d > 0:
            main = soup.select_one("main, article, #content, .content, .entry-content") or soup.body
            text = main.get_text(" ", strip=True) if main else ""
            if len(text) > 1500 and topic.search(title):
                docs[final] = {"url": final, "title": title, "date": _date(text[:3000]), "page_url": final, "page_title": title, "html_page": True}
    # Several files under one row title (one per half-year, say): tell them apart by the file's own path.
    same = Counter(d["title"] for d in docs.values())
    for d in docs.values():
        if same[d["title"]] > 1:
            d["title"] = f"{d['title']} [{'/'.join(urlparse(d['url']).path.split('/')[-2:])}]"
    return list(docs.values()), sorted(seen_pages), problems


# ---------------------------------------------------------------- storing


def _effective(text: str, listed: str | None = None) -> dict:
    """The first 'with effect from <date>' the notification prints, quoted as printed. The date is read only when it has a
    four-digit year within a year of the date the site lists for the document (old OCR text layers misread digits)."""
    for m in EFFECTIVE.finditer((text or "")[:30000]):
        phrase = re.sub(r"\s+", " ", m.group(0)).strip()
        if not re.search(r"(?:19|20)\d\d\s*$", m.group(1)):
            continue
        d = _date(m.group(1))
        if d and listed and abs(int(d[:4]) - int(listed[:4])) > 1:
            d = None
        return {"effective_text": phrase, "effective_from": d}
    return {}


def effective(title: str, text: str, listed: str | None) -> dict:
    """The site's own title first ('VDA … wef 01 April 2022'), else the first such phrase in the notification's text;
    effective_seen_in says which."""
    for where, s in (("title", title), ("text", text)):
        e = _effective(s, listed)
        if e:
            return {**e, "effective_seen_in": where}
    return {}


def _b2_used(lib: Library) -> int:
    return sum(v or 0 for p, v in lib.known("compensation/").items())


def store_docs(lib: Library, f: Fetch, docs: list[dict], *, prefix: str, kind: str, body: str, category: str, court: str,
               limit: int | None = None, wages: bool = False, prune: bool = False) -> dict:
    """Each listed document -> its original (Backblaze prefix/, or Drive library/prefix/ when big) + a row in prefix/text.jsonl.gz."""
    g = Group(lib, "b2", f"{prefix}/text.jsonl.gz", kind + "_text", f"{body}: {category} (text)", court=court)
    used = _b2_used(lib)
    counts = Counter()
    fails = []
    for d in docs:
        if g.has(d["url"]):
            counts["already"] += 1
            continue
        if limit is not None and counts["new"] >= limit:
            break
        try:
            data, ext, final = fetch_doc(f, d["url"])
        except Blocked as e:
            counts["failed"] += 1
            fails.append({"url": d["url"], "title": d.get("title"), "why": str(e)[:300]})
            print(f"    failed: {str(e)[:200]}", flush=True)
            continue
        if ext in (".html", ".htm") and not d.get("html_page"):
            counts["failed"] += 1
            fails.append({"url": d["url"], "title": d.get("title"), "why": "an HTML page where a file was linked (missing file)"})
            continue
        name = file_name(d.get("title") or "", d["url"], ext)
        if len(data) > BIG or used + len(data) > B2_BUDGET:
            store, path = "drive", f"library/{prefix}/{name}"
        else:
            store, path = "b2", f"{prefix}/{name}"
            used += len(data)
        year = int(d["date"][:4]) if d.get("date") else None
        try:
            lib.put(store, path, data, kind, title=(d.get("title") or "")[:300], court=court, year=year)
        except Exception as e:  # noqa: BLE001 - retried on the next run
            counts["failed"] += 1
            fails.append({"url": d["url"], "title": d.get("title"), "why": f"store: {e}"[:300]})
            continue
        meta = {"title": d.get("title"), "number": d.get("number"), "date": d.get("date"), "body": body, "category": d.get("category") or category,
                "page_url": d.get("page_url"), "state": d.get("state")}
        row = doc_row(meta, data, ext, d["url"], path, store)
        if wages:
            row.update(effective(d.get("title") or "", row["text"], d.get("date")))
        g.add(row)
        counts["new"] += 1
        if g.dirty >= 10:
            g.save()
    if prune and limit is None:
        # Documents the site no longer lists here (or an earlier, looser crawl took): file, text row and map row go.
        listed = {d["url"] for d in docs}
        for url in [u for u in g.rows if u not in listed]:
            r = g.rows.pop(url)
            lib.store(r["store"]).delete(r["file"])
            lib.db.http.delete("/library_files", params={"path": f"eq.{r['file']}"})
            g.dirty += 1
            counts["pruned"] += 1
            print(f"    pruned (no longer listed): {r.get('title')!r}", flush=True)
    g.save()
    counts["stored_rows"] = len(g.rows)
    counts["scan_only"] = sum(1 for r in g.rows.values() if r.get("scan_only"))
    return {"listed": len(docs), **counts, "fails": fails}


def write_index(lib: Library, prefix: str, kind: str, title: str, docs: list[dict], res: dict, pages: list[str], problems: list[str]) -> None:
    failed = {x["url"]: x["why"] for x in res.get("fails", [])}
    head = {"listed_at": now(), "listed": len(docs), "pages_read": pages, "problems": problems,
            "result": {k: v for k, v in res.items() if k != "fails"}}
    rows = [head] + [{**d, "status": ("failed: " + failed[d["url"]]) if d["url"] in failed else "listed"} for d in docs]
    data = pack(rows)
    lib.put("b2", f"{prefix}/index.jsonl.gz", data, kind + "_index", title=title, rows=len(docs))


def report(name: str, res: dict) -> None:
    print(f"  {name}: listed {res['listed']}, new {res.get('new', 0)}, already {res.get('already', 0)}, failed {res.get('failed', 0)}, "
          f"rows {res.get('stored_rows', 0)}, scan-only {res.get('scan_only', 0)}", flush=True)
    for x in res.get("fails", [])[:10]:
        print(f"    failed {x['url']}: {x['why']}", flush=True)


# ---------------------------------------------------------------- the sources


NALSA_PAGES = [
    ("https://nalsa.gov.in/victim-compensation/", None, "victim compensation scheme"),
    ("https://nalsa.gov.in/r-o-victim-compensation-schemes-report/", None, "victim compensation returns (yearly, by state)"),
    # Of NALSA's other schemes, only those for victims (acid attack, trafficking) bear on compensation.
    ("https://nalsa.gov.in/preventive-strategic-legal-services-schemes/", re.compile(r"acid|trafficking|victim|एसिड|पीड़ित", re.I), "victim schemes"),
]


def nalsa(limit: int | None) -> None:
    lib, f = Library(29, SOURCES["nalsa"]), Fetch(pause=2.0)
    docs, pages, problems = [], [], []
    for page, only, cat in NALSA_PAGES:
        try:
            r = f.get(page)
        except Blocked as e:
            problems.append(str(e))
            continue
        pages.append(page)
        soup = BeautifulSoup(r.content, "lxml")
        main = soup.select_one("main") or soup.select_one("#main-content") or soup.body
        for tr in main.find_all("tr"):
            a = tr.find("a", href=re.compile(r"\.pdf", re.I))
            if not a:
                continue
            row = tr.get_text(" ", strip=True)
            if only and not only.search(row):
                continue
            t = _clean(tr.find("td").get_text(" ", strip=True)) or _title(a, row, a["href"])
            docs.append({"url": absolute(page, a["href"]), "title": t, "date": _date(t), "page_url": page, "category": cat})
    seen = set()
    docs = [d for d in docs if not (d["url"] in seen or seen.add(d["url"]))]
    print(f"  nalsa: {len(docs)} documents listed on {len(pages)} pages", flush=True)
    res = {"listed": 0, "fails": []}
    by_cat = Counter(d["category"] for d in docs)
    res = store_docs(lib, f, docs, prefix="compensation/nalsa", kind="victim_scheme", body="National Legal Services Authority",
                     category="victim compensation", court="central", limit=limit)
    report("nalsa", res)
    write_index(lib, "compensation/nalsa", "victim_scheme", "NALSA victim compensation: what nalsa.gov.in lists", docs, res, pages, problems)
    print(f"  nalsa by category: {dict(by_cat)}", flush=True)


def states(codes: list[str] | None, limit: int | None, prune: bool = False) -> None:
    lib, f = Library(29, SOURCES["victim-schemes"]), Fetch(pause=1.5)
    summary = {}
    for code, (name, home) in SLSA.items():
        if codes and code not in codes:
            continue
        print(f"  {name} ({home})", flush=True)
        docs, pages, problems = crawl(f, [home], VICTIM, VICTIM_FOLLOW, max_pages=35, depth=2)
        for d in docs:
            d["state"] = name
        prefix = f"compensation/victim-schemes/{code}"
        res = store_docs(lib, f, docs, prefix=prefix, kind="victim_scheme", body=f"{name} State Legal Services Authority",
                         category="victim compensation scheme", court=code, limit=limit, prune=prune and not problems) if docs else {"listed": 0, "fails": []}
        if docs or problems:
            write_index(lib, prefix, "victim_scheme", f"{name}: victim compensation (SLSA site)", docs, res, pages, problems)
        report(name, res)
        if not docs:
            print(f"    nothing found on {len(pages)} pages; problems: {problems[:3]}", flush=True)
        summary[code] = {"listed": res["listed"], "rows": res.get("stored_rows", 0), "pages": len(pages), "problems": problems[:5]}
    print(f"  states: {summary}", flush=True)


def clc(limit: int | None) -> None:
    lib, f = Library(29, SOURCES["central"]), Fetch(pause=2.0)
    page = "https://clc.gov.in/?page_id=144"
    r = f.get(page)
    soup = BeautifulSoup(r.content, "lxml")
    docs = []
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=DOC)
        tds = tr.find_all("td")
        if not a or len(tds) < 2:
            continue
        cells = [td.get_text(" ", strip=True) for td in tds]
        title = _clean(cells[1])
        date = next((iso_date(c) for c in reversed(cells) if iso_date(c)), None)
        docs.append({"url": absolute(page, a["href"]), "title": title, "date": date, "page_url": page})
    print(f"  clc: {len(docs)} documents listed", flush=True)
    res = store_docs(lib, f, docs, prefix="compensation/minimum-wages/central", kind="minimum_wages", body="Chief Labour Commissioner (Central)",
                     category="minimum wages / VDA order", court="central", limit=limit, wages=True)
    report("clc", res)
    write_index(lib, "compensation/minimum-wages/central", "minimum_wages", "Central minimum wages and VDA orders: what clc.gov.in lists",
                docs, res, [page], [])


def wages(codes: list[str] | None, limit: int | None, prune: bool = False) -> None:
    lib, f = Library(29, SOURCES["minimum-wages"]), Fetch(pause=1.5)
    summary = {}
    for code, (name, starts) in LABOUR.items():
        if codes and code not in codes:
            continue
        print(f"  {name} ({starts[0]})", flush=True)
        docs, pages, problems = crawl(f, starts, WAGES, WAGES_FOLLOW, max_pages=40, depth=2)
        for d in docs:
            d["state"] = name
        prefix = f"compensation/minimum-wages/{code}"
        res = store_docs(lib, f, docs, prefix=prefix, kind="minimum_wages", body=f"Labour Department, {name}",
                         category="minimum wages notification", court=code, limit=limit, wages=True, prune=prune and not problems) if docs else {"listed": 0, "fails": []}
        if docs or problems:
            write_index(lib, prefix, "minimum_wages", f"{name}: minimum wages (labour department site)", docs, res, pages, problems)
        report(name, res)
        if not docs:
            print(f"    nothing found on {len(pages)} pages; problems: {problems[:3]}", flush=True)
        summary[code] = {"listed": res["listed"], "rows": res.get("stored_rows", 0), "pages": len(pages), "problems": problems[:5]}
    print(f"  wages: {summary}", flush=True)


# ---------------------------------------------------------------- status and checks


def status() -> None:
    lib = Library(29, "")
    b2 = lib.known("compensation/")
    drive = lib.known("library/compensation/")
    print(f"  Backblaze compensation/: {len(b2)} files, {sum(v or 0 for v in b2.values()) / 1e6:.1f} MB; "
          f"Drive library/compensation/: {len(drive)} files, {sum(v or 0 for v in drive.values()) / 1e6:.1f} MB")
    for p in sorted(x for x in b2 if x.endswith("/text.jsonl.gz")):
        rows = unpack(lib.get("b2", p) or b"")
        idx = p.replace("text.jsonl.gz", "index.jsonl.gz")
        listed = None
        if idx in b2:
            listed = unpack(lib.get("b2", idx) or b"")[0].get("listed")
        print(f"  {p.rsplit('/', 1)[0]}: listed {listed}, stored {len(rows)}, scan-only {sum(1 for r in rows if r.get('scan_only'))}, "
              f"in Drive {sum(1 for r in rows if r.get('store') == 'drive')}")


def verify(n: int = 3) -> None:
    import hashlib
    lib, f = Library(29, ""), Fetch(pause=2.0)
    texts = [p for p in lib.known("compensation/") if p.endswith("/text.jsonl.gz")]
    rows = [r for p in texts for r in unpack(lib.get("b2", p) or b"")]
    for r in random.sample(rows, min(n, len(rows))):
        stored = lib.get(r["store"], r["file"])
        same_store = stored is not None and hashlib.sha1(stored).hexdigest() == r["sha1"]
        try:
            live, _, _ = fetch_doc(f, r["source_url"])
            same_live = hashlib.sha1(live).hexdigest() == r["sha1"]
        except Blocked as e:
            same_live = f"live copy not read: {e}"
        first = " ".join((r.get("text") or "").split()[:25])
        print(f"  {r['title'][:90]!r} date {r.get('date')} ({r['store']}:{r['file']})\n    stored copy matches row: {same_store}; "
              f"live file identical: {same_live}; scan_only {r.get('scan_only')}\n    first words: {first[:200]}")


def probe(url: str) -> None:
    f = Fetch()
    r = f.get(url)
    soup = BeautifulSoup(r.content, "lxml")
    print(f"  {r.status_code} {r.url} title={_page_title(soup)!r}")
    for a in soup.find_all("a", href=True):
        t = a.get_text(" ", strip=True)
        print(f"    {t[:90]!r} -> {absolute(str(r.url), a['href'])}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["nalsa", "states", "clc", "wages", "probe", "status", "verify"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--prune", action="store_true", help="states/wages: drop stored documents the site no longer lists")
    a = ap.parse_args()
    if a.cmd == "nalsa":
        nalsa(a.limit)
    elif a.cmd == "states":
        states(a.args or None, a.limit, a.prune)
    elif a.cmd == "clc":
        clc(a.limit)
    elif a.cmd == "wages":
        wages(a.args or None, a.limit, a.prune)
    elif a.cmd == "probe":
        probe(a.args[0])
    elif a.cmd == "status":
        status()
    else:
        verify(int(a.args[0]) if a.args else 3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
