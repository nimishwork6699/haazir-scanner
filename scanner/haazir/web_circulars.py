"""Item 19 of the library plan: regulator circulars, read from the regulators' own public websites.

    python -m haazir.web_circulars rbi   [--list-only] [--limit N] [--only md mc notification]
    python -m haazir.web_circulars sebi  [--list-only] [--limit N] [--only circular master-circular regulation]
    python -m haazir.web_circulars ibbi  [--list-only] [--limit N] [--only regulation circular]
    python -m haazir.web_circulars irdai                      # checks robots.txt (IRDAI disallows all crawling)
    python -m haazir.web_circulars status                     # listed vs stored, per regulator and category
    python -m haazir.web_circulars verify sebi                # 3 random stored documents checked against the live site

Google Drive library/circulars/<regulator>/<category>/<year>/<file>   the document as published (PDF; else the HTML page)
             library/circulars/<regulator>/text/<category>-<year>.jsonl.gz   one row per document: title, number,
                                       date, issuing body, category, extracted text, source URL
             library/circulars/<regulator>/index.jsonl.gz   what the source listed and how each document fared
A run skips documents already in their text file, so it can be stopped and started again.

What the sites allow (checked 26 Oct 2026):
  RBI   master directions, master circulars (by year) and notifications (by year, 1991 on): plain pages, no captcha.
        Their PDF copies on rbidocs.rbi.org.in answer scripts with a JavaScript/captcha challenge page (F5), so the
        document kept is RBI's own HTML page of it (www.rbi.org.in/Scripts/...?Id=), which carries the full text;
        the PDF link is kept in the row (pdf_url) for people to open.
  SEBI  the listing pages show only the newest 25; their 'next page' call (getnewslistinfo.jsp) answers HTTP 530
        "Unauthorized Activity Has Been Detected" to scripts, so it is not used. Loaded: page one of each list, every
        regulation (one page), and the circular / master-circular / regulation pages named in SEBI's sitemap.xml.
  IRDAI irdai.gov.in and its mirror policyholder.gov.in both say 'Disallow: /' to every robot: not crawled.
  IBBI  regulations (as amended) and circulars: plain paged lists, no captcha.
"""

import argparse
import re
import sys
from collections import Counter, defaultdict

from bs4 import BeautifulSoup

from . import web_common
from .storage import pack, unpack
from .web_common import Blocked, Group, Library, Site, absolute, iso_date, now, run_docs

# Two checks web_common does not make (yet), applied only while this loader runs:
#  - a URL ending .pdf that answers with an HTML page (a bot challenge) is a failure, not a document;
#  - the text of an HTML document: web_common.html_text drops <form>, and ASP.NET pages (RBI) wrap everything in one.
_fetch_doc, _text_of = web_common.fetch_doc, web_common.text_of


def _fetch_checked(site: Site, url: str, max_bytes: int = 400_000_000) -> tuple[bytes, str, str]:
    data, ext, final = _fetch_doc(site, url, max_bytes)
    if re.search(r"\.pdf(?:$|\?)", url, re.I) and b"%PDF" not in data[:1024]:
        low = data[:100_000].lower()
        why = "a JavaScript/captcha challenge page" if b"captcha" in low or b"enable javascript" in low else "a non-PDF answer"
        raise Blocked(f"{why} instead of the PDF at {url}")
    return data, ext, final


def page_text(data: bytes) -> str:
    soup = BeautifulSoup(data, "lxml")
    for t in soup(["script", "style", "noscript", "nav", "header", "footer", "iframe", "svg"]):
        t.decompose()
    node = None
    for sel in (".tablecontent2", "#doublescroll", ".main_full", "#member-wrapper", "body"):
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 200:
            break
    text = (node or soup).get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


# storage.unpack splits on str.splitlines(), which also breaks at U+2028, U+2029 and U+0085; json.dumps leaves those
# raw, so a text holding one would be unreadable. They are turned into plain line breaks before a row is written.
LINE_BREAKS = str.maketrans({0x2028: 10, 0x2029: 10, 0x85: 10})


def _text_checked(data: bytes, ext: str) -> dict:
    if ext.lower() in (".html", ".htm") and data[:5] != b"%PDF-":
        out = {"text": page_text(data), "pages": None, "scan_only": False}
    else:
        out = _text_of(data, ext)
    out["text"] = (out.get("text") or "").translate(LINE_BREAKS)
    return out


def _repair(lib: Library, reg: str) -> None:
    """Rewrite any text file of this regulator that storage.unpack cannot read (see LINE_BREAKS)."""
    import gzip
    import json
    for path in lib.known(f"{ROOT}/{reg}/"):
        if not path.endswith(".jsonl.gz"):
            continue
        data = lib.get("drive", path) or b""
        try:
            unpack(data)
            continue
        except ValueError:
            pass
        rows = [json.loads(line) for line in gzip.decompress(data).decode().split(chr(10)) if line]
        for r in rows:
            for k, v in r.items():
                if isinstance(v, str):
                    r[k] = v.translate(LINE_BREAKS)
        fixed = pack(rows)
        unpack(fixed)
        lib.put("drive", path, fixed, "circular_index" if path.endswith("index.jsonl.gz") else "circular_text",
                title=_group_title(reg, path) if "/text/" in path else f"{BODY[reg]}: circulars index", rows=len(rows),
                **(_group_meta(reg, path) if "/text/" in path else {"court": reg}))
        print(f"  repaired {path} ({len(rows)} rows)", flush=True)


def _patch() -> None:
    web_common.fetch_doc = _fetch_checked
    web_common.text_of = _text_checked

ROOT = "library/circulars"
BODY = {"rbi": "Reserve Bank of India", "sebi": "Securities and Exchange Board of India",
        "irdai": "Insurance Regulatory and Development Authority of India", "ibbi": "Insolvency and Bankruptcy Board of India"}
SOURCE = {"rbi": "Reserve Bank of India (rbi.org.in)", "sebi": "Securities and Exchange Board of India (sebi.gov.in)",
          "irdai": "IRDAI (irdai.gov.in)", "ibbi": "Insolvency and Bankruptcy Board of India (ibbi.gov.in)"}
NUMBER = {"rbi": re.compile(r"RBI/\s?(?:[A-Z&.]+\s?/\s?)?\d{4}\s?-\s?\d{2,4}\s?/\s?\d+"),
          "sebi": re.compile(r"(?:SEBI/HO/[A-Z0-9/()\-_.]+|HO/\(\d+\)\d{4}-[A-Z0-9/\-]+|CIR/[A-Z0-9/\-]+|SEBI/LAD-NRO/GN/\d{4}-\d{2}/\d+/?\w*)"),
          "ibbi": re.compile(r"IBBI/\d{4}-\d{2}/GN/REG\d+|IBBI/[A-Z]+(?:/[A-Z]+)?/\d+/\d{4}")}


# ---------------------------------------------------------------- RBI

RBI = "https://www.rbi.org.in/Scripts/"


def _rbi_rows(html: str, detail: str, category: str) -> list[dict]:
    """RBI's list tables: header rows carry a department or a date; each document row links its page and its PDF."""
    soup = BeautifulSoup(html, "lxml")
    out, date, dept = [], None, None
    for tr in soup.select("table.tablebg tr"):
        head = tr.select_one("td.tableheader")
        if head:
            text = head.get_text(" ", strip=True)
            d = iso_date(text)
            if d:
                date = d
            else:
                dept, date = text, None
            continue
        a = tr.find("a", href=re.compile(rf"^{detail}\?", re.I))
        if not a:
            continue
        page = absolute(RBI, a["href"])
        pdf = tr.find("a", href=re.compile(r"\.pdf$", re.I))
        doc_id = re.search(r"id=(\d+)", a["href"], re.I)
        out.append({"title": a.get_text(" ", strip=True), "date": date, "department": dept, "page_url": page,
                    "pdf_url": pdf["href"].strip().replace("http://", "https://") if pdf else None,
                    "rbi_id": doc_id.group(1) if doc_id else None, "category": category})
    return out


def _rbi_years(html: str) -> list[str]:
    return sorted(set(re.findall(r'GetYearMonth\("(\d{4})","0"\)', html)))


def _rbi_post(site: Site, url: str, html: str, year: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    form = {i["name"]: i.get("value", "") for i in soup.find_all("input") if i.get("name")}
    form.update({"hdnYear": year, "hdnMonth": "0"})
    return site.get(url, method="POST", data=form).text


def rbi_list(site: Site, only: list[str] | None) -> tuple[list[dict], dict]:
    docs, notes = [], {}
    if not only or "md" in only:
        rows = _rbi_rows(site.get(RBI + "BS_ViewMasterDirections.aspx").text, "BS_ViewMasDirections.aspx", "master-direction")
        notes["master-direction"] = {"listed": len(rows), "page": RBI + "BS_ViewMasterDirections.aspx"}
        docs += rows
        print(f"  rbi master directions: {len(rows)}", flush=True)
    for key, page, detail, cat in (("mc", "BS_ViewMasterCirculars.aspx", "BS_ViewMasterCirculars.aspx", "master-circular"),
                                   ("notification", "NotificationUser.aspx", "NotificationUser.aspx", "notification")):
        if only and key not in only:
            continue
        url = RBI + page
        first = site.get(url).text
        seen: dict[str, dict] = {}
        for r in _rbi_rows(first, detail, cat):
            seen.setdefault(r["page_url"], r)
        per_year = {}
        for y in _rbi_years(first):
            rows = _rbi_rows(_rbi_post(site, url, first, y), detail, cat)
            per_year[y] = len(rows)
            for r in rows:
                seen.setdefault(r["page_url"], r)
            print(f"  rbi {cat} {y}: {len(rows)}", flush=True)
        notes[cat] = {"listed": len(seen), "per_year": per_year, "page": url}
        docs += list(seen.values())
    for d in docs:
        d["url"] = d["page_url"]  # rbidocs PDFs are behind a bot challenge; RBI's HTML page carries the full text
    return docs, notes


# ---------------------------------------------------------------- SEBI

SEBI = "https://www.sebi.gov.in"
SEBI_LISTS = {"circular": 7, "master-circular": 6, "regulation": 3}
SEBI_PATH = {"circular": "/legal/circulars/", "master-circular": "/legal/master-circulars/", "regulation": "/legal/regulations/"}


def _sebi_detail(site: Site, page: str) -> dict:
    soup = BeautifulSoup(site.get(page).content, "lxml")
    out = {}
    h1, h5 = soup.find("h1"), soup.find("h5")
    if h1:
        out["title"] = h1.get_text(" ", strip=True)
    if h5 and iso_date(h5.get_text(strip=True)):
        out["date"] = iso_date(h5.get_text(strip=True))
    frame = soup.find("iframe", src=re.compile(r"file=", re.I))
    if frame:
        m = re.search(r"file=(https?://[^&\"']+\.pdf)", frame["src"], re.I)
        if m:
            out["pdf_url"] = m.group(1)
    if "pdf_url" not in out:
        a = soup.find("a", href=re.compile(r"sebi_data/.+\.pdf$", re.I))
        if a:
            out["pdf_url"] = absolute(page, a["href"])
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Circular No\.?\s*:?\s*([A-Z0-9][A-Za-z0-9/()\-_. ]{5,80}?)(?:\s{2,}|\s+[A-Z][a-z])", text)
    if m:
        out["number"] = m.group(1).strip()
    return out


def sebi_list(site: Site, only: list[str] | None, lib: Library) -> tuple[list[dict], dict]:
    notes: dict = {}
    pages: dict[str, dict] = {}
    for cat, ssid in SEBI_LISTS.items():
        if only and cat not in only:
            continue
        url = f"{SEBI}/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid={ssid}&smid=0"
        soup = BeautifulSoup(site.get(url).text, "lxml")
        total = soup.find(string=re.compile(r"of\s+\d+\s+records"))
        rows = soup.select("#sample_1 tbody tr")
        n = 0
        for tr in rows:
            tds = tr.find_all("td")
            a = tr.find("a", href=True)
            if not a or len(tds) < 2:
                continue
            pages.setdefault(a["href"], {"title": a.get("title") or a.get_text(" ", strip=True), "date_listed": tds[0].get_text(" ", strip=True),
                                         "category": cat, "page_url": a["href"]})
            n += 1
        m = re.search(r"of\s+(\d+)\s+records", total or "")
        notes[cat] = {"listed_at_source": int(m.group(1)) if m else n, "on_first_page": n, "list": url}
        print(f"  sebi {cat}: source says {notes[cat]['listed_at_source']}, first page {n}", flush=True)
    sm = site.get(f"{SEBI}/sitemap.xml").text
    for loc in re.findall(r"<loc>([^<]+)</loc>", sm):
        for cat, path in SEBI_PATH.items():
            if (not only or cat in only) and path in loc:
                if loc not in pages:
                    pages[loc] = {"title": None, "category": cat, "page_url": loc, "from": "sitemap"}
                    notes[cat]["from_sitemap"] = notes[cat].get("from_sitemap", 0) + 1
    # Pages already loaded need no second look at their detail page.
    done: dict[str, dict] = {}
    for f in lib.known(f"{ROOT}/sebi/text/"):
        g = unpack(lib.get("drive", f) or b"")
        for r in g:
            if r.get("page_url"):
                done[r["page_url"]] = r
    docs = []
    for i, (page, p) in enumerate(pages.items(), 1):
        if page in done:
            r = done[page]
            docs.append({**p, "title": r["title"], "date": r["date"], "number": r.get("number"), "url": r["source_url"]})
            continue
        try:
            det = _sebi_detail(site, page)
        except Blocked as e:
            print(f"    sebi page failed: {e}", flush=True)
            det = {}
        date = det.get("date") or iso_date(p.get("date_listed"))
        docs.append({**p, "title": det.get("title") or p["title"], "date": date, "number": det.get("number"),
                     "pdf_url": det.get("pdf_url"), "url": det.get("pdf_url") or page})
        if i % 25 == 0:
            print(f"    sebi pages {i}/{len(pages)}", flush=True)
    for d in docs:
        d.pop("date_listed", None)
    return docs, notes


# ---------------------------------------------------------------- IBBI

IBBI = "https://ibbi.gov.in"
IBBI_LISTS = {"regulation": "/legal-framework/updated", "circular": "/legal-framework/circulars"}


def ibbi_list(site: Site, only: list[str] | None) -> tuple[list[dict], dict]:
    docs, notes = [], {}
    for cat, path in IBBI_LISTS.items():
        if only and cat not in only:
            continue
        url = IBBI + path
        html = site.get(url).text
        last = max([int(x) for x in re.findall(r"[?&]page=(\d+)", html)] or [1])
        seen, rows_seen = {}, 0
        for p in range(1, last + 1):
            if p > 1:
                html = site.get(url, params={"page": p}).text
            # Older IBBI links are unquoted file names with spaces (href=.../14th Jun 17 … Regulations, 2017_….pdf),
            # which any HTML parser cuts at the first space: quote each whole name, up to .pdf, before parsing.
            html = re.sub(r"href=(https?://[^\"'>]+?\.pdf)(?=\s|>)", lambda m: f'href="{m.group(1)}"', html, flags=re.I)
            soup = BeautifulSoup(html, "lxml")
            table = soup.find("table")
            for tr in (table.find_all("tr") if table else [])[1:]:
                tds = tr.find_all("td")
                if len(tds) < 4:
                    continue
                a = tds[3].find("a", href=True) or tr.find("a", href=re.compile(r"\.pdf$", re.I))
                if not a:
                    continue
                pdf = absolute(url, a["href"]).replace("gov.in//", "gov.in/")
                hindi = tds[4].find("a", href=True) if len(tds) > 4 else None
                seen.setdefault(pdf, {"title": tds[2].get_text(" ", strip=True), "date": iso_date(tds[1].get_text(" ", strip=True)),
                                      "category": cat, "url": pdf, "page_url": url,
                                      "hindi_pdf_url": absolute(url, hindi["href"]).replace("gov.in//", "gov.in/") if hindi else None})
                rows_seen += 1
        notes[cat] = {"rows_at_source": rows_seen, "distinct_files": len(seen), "pages": last, "list": url}
        print(f"  ibbi {cat}: {rows_seen} rows on {last} pages, {len(seen)} distinct files", flush=True)
        docs += list(seen.values())
    return docs, notes


# ---------------------------------------------------------------- IRDAI: robots.txt says no


def irdai_check(lib: Library) -> None:
    site = Site(pause=2.0)
    notes = {}
    for host in ("https://irdai.gov.in", "https://policyholder.gov.in"):
        ok = site.allowed(f"{host}/circulars")
        notes[host] = "robots.txt allows" if ok else "robots.txt: Disallow / for every user agent - not crawled"
        print(f"  {host}: {notes[host]}", flush=True)
    data = pack([{"checked_at": now(), "regulator": "irdai", "loaded": 0, "why_not": notes,
                  "listed_at_source_per_drafting_sources": {"circulars": 605, "regulations": 201, "orders": 197}}])
    lib.put("drive", f"{ROOT}/irdai/index.jsonl.gz", data, "circular_index", title="IRDAI circulars: not loaded (robots.txt)",
            court="irdai", rows=0)


# ---------------------------------------------------------------- loading


def _fill_numbers(lib: Library, reg: str) -> int:
    """Circular numbers the listing does not give, read from each document's own first page (never guessed)."""
    pat = NUMBER.get(reg)
    if not pat:
        return 0
    filled = 0
    for path in lib.known(f"{ROOT}/{reg}/text/"):
        g = Group(lib, "drive", path, "circular_text", "")
        changed = 0
        for r in g.rows.values():
            if not r.get("number") and r.get("text"):
                m = pat.search(r["text"][:3000])
                if m:
                    r["number"] = re.sub(r"\s+", "", m.group(0))
                    changed += 1
        if changed:
            g.title = _group_title(reg, path)
            g.meta = _group_meta(reg, path)
            g.dirty = changed
            g.save()
            filled += changed
    return filled


def _group_title(reg: str, path: str) -> str:
    name = path.rsplit("/", 1)[1].replace(".jsonl.gz", "")
    cat, _, year = name.rpartition("-")
    return f"{reg.upper()} {cat.replace('-', ' ')}s {year}: text"


def _group_meta(reg: str, path: str) -> dict:
    y = path.rsplit("-", 1)[1].split(".")[0]
    return {"court": reg, "year": int(y) if y.isdigit() else None}


def load(reg: str, list_only: bool, limit: int | None, only: list[str] | None, workers: int) -> None:
    _patch()
    lib = Library(19, SOURCE[reg])
    if reg == "irdai":
        irdai_check(lib)
        return
    _repair(lib, reg)
    site = Site(pause=4.0 if reg == "ibbi" else 1.5, timeout=300)
    if reg == "rbi":
        docs, notes = rbi_list(site, only)
    elif reg == "sebi":
        docs, notes = sebi_list(site, only, lib)
    else:
        docs, notes = ibbi_list(site, only)
    for d in docs:
        d["body"] = BODY[reg]
        for k, v in list(d.items()):
            if isinstance(v, str):
                d[k] = v.translate(LINE_BREAKS)
        d["court"] = reg
        d["subfolder"] = f"{d['category']}/{d['date'][:4] if d.get('date') else 'undated'}"
    by = Counter(d["subfolder"] for d in docs)
    print(f"  {reg}: {len(docs)} documents listed; {dict(sorted(by.items()))}", flush=True)
    if list_only:
        return

    def group_of(d: dict) -> tuple[str, str, str]:
        y = d["date"][:4] if d.get("date") else "undated"
        path = f"{ROOT}/{reg}/text/{d['category']}-{y}.jsonl.gz"
        return "drive", path, _group_title(reg, path)

    res = run_docs(lib, site, docs, store="drive", folder=f"{ROOT}/{reg}", kind="circular", text_kind="circular_text",
                   group_of=group_of, group_meta=lambda d: _group_meta(reg, group_of(d)[1]), workers=workers, limit=limit)
    res["notes"] = notes
    print(f"  {reg}: new {res['new']}, already {res['already']}, failed {res['failed']}, stored rows {res['stored_rows']}, "
          f"scan-only {res['scan_only']}", flush=True)
    for f in res["fails"][:30]:
        print(f"    failed {f['url']}: {f['why']}", flush=True)
    print(f"  {reg}: numbers read from documents: {_fill_numbers(lib, reg)}", flush=True)
    write_index(lib, reg, docs, res)


def write_index(lib: Library, reg: str, docs: list[dict], res: dict) -> None:
    """What the source listed and how each document fared: stored, failed (why), or not loaded yet (a --limit run)."""
    stored = set()
    for p in lib.known(f"{ROOT}/{reg}/text/"):
        stored |= {r["source_url"] for r in unpack(lib.get("drive", p) or b"")}
    failed = {f["url"]: f["why"] for f in res.get("fails", [])}
    rows = [{**{k: v for k, v in d.items() if k != "subfolder"},
             "status": "stored" if d["url"] in stored else ("failed: " + failed[d["url"]]) if d["url"] in failed else "not loaded yet"}
            for d in docs]
    head = {"listed_at": now(), "regulator": reg, "listed": len(docs), "stored": sum(1 for r in rows if r["status"] == "stored"),
            "notes": res.get("notes"), "result": {k: v for k, v in res.items() if k not in ("fails", "notes")}}
    data = pack([head] + rows)
    lib.put("drive", f"{ROOT}/{reg}/index.jsonl.gz", data, "circular_index", title=f"{BODY[reg]}: circulars index", court=reg, rows=len(rows))


def status() -> None:
    lib = Library(19, "")
    for reg in ("rbi", "sebi", "irdai", "ibbi"):
        known = lib.known(f"{ROOT}/{reg}/")
        files = Counter()
        size = defaultdict(int)
        for p, b in known.items():
            parts = p.split("/")
            if parts[3] in ("text",) or p.endswith("index.jsonl.gz"):
                continue
            files[parts[3]] += 1
            size[parts[3]] += b or 0
        rows, scan = Counter(), Counter()
        for p in known:
            if "/text/" in p:
                for r in unpack(lib.get("drive", p) or b""):
                    rows[r.get("category")] += 1
                    scan[r.get("category")] += 1 if r.get("scan_only") else 0
        idx = unpack(lib.get("drive", f"{ROOT}/{reg}/index.jsonl.gz") or b"") if f"{ROOT}/{reg}/index.jsonl.gz" in known else []
        listed = Counter(r.get("category") for r in idx[1:]) if idx else Counter()
        failed = Counter(r.get("category") for r in idx[1:] if str(r.get("status", "")).startswith("failed")) if idx else Counter()
        print(f"{reg}: {sum(known.values()) / 1e6:.1f} MB in {len(known)} files" + (f"; {idx[0].get('why_not') or ''}" if idx and reg == "irdai" else ""))
        for cat in sorted(set(files) | set(rows) | set(listed)):
            print(f"  {cat}: listed {listed.get(cat, 0)}, originals {files.get(cat, 0)} ({size.get(cat, 0) / 1e6:.1f} MB), "
                  f"text rows {rows.get(cat, 0)}, scan-only {scan.get(cat, 0)}, failed last run {failed.get(cat, 0)}")


def verify(reg: str, n: int = 3) -> None:
    """n random stored documents: the kept file read back (its sha1 must match the row), and the row's title, date
    and first lines set beside what the live source shows today."""
    import hashlib
    import random
    lib = Library(19, "")
    rows = []
    for p in lib.known(f"{ROOT}/{reg}/text/"):
        rows += unpack(lib.get("drive", p) or b"")
    site = Site(pause=4.0 if reg == "ibbi" else 2.0)
    for r in random.sample(rows, min(n, len(rows))):
        data = lib.get("drive", r["file"]) or b""
        same = hashlib.sha1(data).hexdigest() == r["sha1"]
        live = site.get(r.get("page_url") or r["source_url"]).content
        live_text = page_text(live)
        title_live = (r["title"] or "")[:60] in re.sub(r"\s+", " ", live_text)
        first = next((ln.strip() for ln in (r.get("text") or "").splitlines() if len(ln.strip()) > 15), "")
        if r["source_url"] != r.get("page_url"):  # a PDF: the live copy itself, compared byte for byte
            live_doc = site.get(r["source_url"]).content
            first_live = f"live PDF identical {hashlib.sha1(live_doc).hexdigest() == r['sha1']}"
        else:
            first_live = f"on live page {first[:40] in re.sub(chr(92) + 's+', ' ', live_text)}"
        print(f"- {r['title'][:90]} | {r['date']} | {r.get('number')}\n  file {r['file']} ({len(data)} bytes, sha1 match {same}); "
              f"title on {'list' if reg == 'ibbi' else 'live'} page {title_live}; first line {first[:70]!r} {first_live}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("regulator", choices=["rbi", "sebi", "irdai", "ibbi", "status", "verify"])
    ap.add_argument("which", nargs="?", help="verify: the regulator")
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    if a.regulator == "status":
        status()
    elif a.regulator == "verify":
        verify(a.which or "rbi")
    else:
        load(a.regulator, a.list_only, a.limit, a.only, a.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
