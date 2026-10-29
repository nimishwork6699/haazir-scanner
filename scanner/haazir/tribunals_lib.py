"""Item 33 of the library plan: the decisions of tribunals and quasi-judicial bodies that publish them openly (no captcha,
no login), every year the source offers.

    python -m haazir.tribunals_lib cat        [--years 2026 2025] [--limit N] [--list-only] [--workers 3]
    python -m haazir.tribunals_lib aptel | cci | tdsat | gst-aar | atfp | ibbi
    python -m haazir.tribunals_lib all        # every forum, one after another (smallest first, CAT last)
    python -m haazir.tribunals_lib status     # listed at source vs stored, bytes per store, scan-only, per forum
    python -m haazir.tribunals_lib verify cat # 3 random stored decisions read back and checked against the live list
    python -m haazir.tribunals_lib skipped    # write the bodies left out, and why, to Backblaze
    python -m haazir.tribunals_lib pause      # every running load stops cleanly after the decision in hand
    python -m haazir.tribunals_lib resume     # loads may run again (then start them again: they carry on)
    python -m haazir.tribunals_lib watch cat atfp   # keep these running: a crashed load is started again (backoff),
                                              # a paused one waits for resume, a finished one is left alone
    ... --relist                              # list again even the bench-years/days an earlier run found complete

A list page that fails (HTTP 500, timeout) after retries with backoff is logged and skipped, the run goes on, and the
page is tried again at the end of the run and on the next run (library/tribunals/<forum>/_progress.json keeps the
skipped pages and the listings found complete, so a restart does not list those again).

Where each forum's decisions come from (checked 26 Oct 2026):
  cat      Central Administrative Tribunal, all 20 benches: final and oral orders on CAT's e-filing site
           (efiling.cgat.gov.in/ecat/fiorder_detail.php, bench by bench, year by year, 2015 on). CAT's older
           judgment archive (catjudgements.nic.in, 1985-2021) does not answer: connection timed out on every try.
  aptel    Appellate Tribunal for Electricity: 'All Judgements/Orders', one page per year (2008 on).
  cci      Competition Commission of India: antitrust orders, combination orders (section 31, approved with
           modification) and penalty orders (sections 43A/44), from the Commission's own lists.
  tdsat    Telecom Disputes Settlement and Appellate Tribunal: final judgments, date-wise search, year by year.
  gst-aar  Advance rulings (AAR) and appellate advance rulings (AAAR) of every State, as the GST Council lists them.
  atfp     Appellate Tribunal for Forfeited Property: judgments (act, month and year) and orders (day by day), 2013 on.
  ibbi     Insolvency and Bankruptcy Board of India: its own orders (disciplinary committee, adjudicating authority,
           first appellate authority), orders of IPAs/RVOs, and the DRT/DRAT orders under the IBC that IBBI publishes.
           (NCLT/NCLAT orders from IBBI are loaded by web_tribunals.py, not here.)
Left out, and why: see SKIPPED below (captcha, login or unreachable). A captcha is never solved or worked around.

Stores:
  Google Drive  library/tribunals/<forum>/<year>/<file>.pdf          each decision as published
                library/tribunals/<forum>/text/<year>[-<part>].jsonl.gz  one row per decision: forum, bench, case
                    number, parties, decision date, judges/members, decision type, text (pdftotext), scan, source URL
                library/tribunals/<forum>/index.jsonl.gz            everything the source listed, and how each fared
  Backblaze     judgments/tribunals/<forum>.jsonl.gz               the stored decisions' rows without text (search)
                judgments/tribunals/_skipped.jsonl.gz              bodies left out and why
Every file is listed in Supabase library_files (item 33). A PDF with no text layer is kept with empty text and
scan = true: never OCR-guessed. A run skips decisions already in their text file (and reads a PDF already in Drive back
from Drive, not the source), so it can be stopped and started again.
"""

from __future__ import annotations

import argparse
import calendar
import gzip
import subprocess
import hashlib
import html as htmllib
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterator
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from .storage import unpack
from .web_common import Blocked, Group, Library, Site, absolute, file_name, iso_date, now, slug, text_of

ITEM = 33
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # scanner/
PAUSE_FILE = os.path.join(HERE, "_scan-tribunals-PAUSE.log")         # gitignored (scanner/_scan*.log)
THIS_YEAR = date.today().year


def clean(s: str | None) -> str:
    """Cell text: tags gone, entities read, spaces tidied."""
    s = re.sub(r"<br\s*/?>", " ", s or "", flags=re.I)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def dmy(s: str | None, last: bool = False) -> str | None:
    """A date printed in s as dd.mm.yyyy, dd/mm/yyyy, dd-mm-yyyy or '15th July, 2026' -> 'YYYY-MM-DD'; the first one
    (or the last, when an order number comes before its date), and the one after 'dated' when there is one.
    None when no date reads (never guessed)."""
    s = s or ""
    found: list[str] = []
    for m in re.finditer(r"(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{4})", s):
        try:
            found.append((m.start(), date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()))
        except ValueError:
            continue
    for m in re.finditer(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", s):
        d = iso_date(f"{m.group(1)} {m.group(2)} {m.group(3)}")
        if d:
            found.append((m.start(), d))
    found = [(pos, d) for pos, d in found if 1950 <= int(d[:4]) <= THIS_YEAR]  # '12.05.2202' is a typo, not a date
    if not found:
        return None
    found.sort()
    dated = re.search(r"dat(?:ed|e)|\bdt\b", s, re.I)
    if dated:
        after = [d for pos, d in found if pos >= dated.start()]
        if after:
            return after[0]
    return found[-1][1] if last else found[0][1]


def paused() -> bool:
    return os.path.exists(PAUSE_FILE)


class _Listing:
    """Listing requests for one run. A list page that fails (HTTP 500, timeout …) after Site's own retries with backoff
    gets one more try a minute later; then it is logged and skipped (the run goes on) and kept for a retry pass at the
    end of the run and in the forum's progress file for the next run. Units (a bench-year, an ATFP day …) that were
    listed and fully handled are remembered there too, so a restart does not list them again (--relist does)."""

    def __init__(self) -> None:
        self.skipped: dict[str, dict] = {}   # unit -> {unit, hint, why, at}
        self.done: set[str] = set()
        self.use_done = True
        self.retrying = False  # in the retry pass: one try per page (Site's own retries with backoff still apply)

    def is_done(self, unit: str) -> bool:
        return self.use_done and unit in self.done

    def get(self, site: Site, unit: str, hint: dict, url: str, **kw):
        """The page's response (HTTP 200), or None when it could not be had: then the unit is kept for a retry."""
        why = ""
        for attempt in range(1 if self.retrying else 2):
            try:
                r = site.get(url, **kw)
                if r.status_code == 200:
                    self.skipped.pop(unit, None)
                    return r
                why = f"HTTP {r.status_code}"
            except Blocked as e:
                why = str(e)
                if "robots.txt" in why:
                    break
            except Exception as e:  # noqa: BLE001 - JSON/parse errors, odd network failures
                why = f"{type(e).__name__}: {e}"
            if attempt == 0 and not self.retrying:
                time.sleep(60)
        self.skip(unit, hint, why)
        return None

    def skip(self, unit: str, hint: dict, why: str) -> None:
        print(f"    listing skipped for now ({unit}): {why[:200]}", flush=True)
        self.skipped[unit] = {"unit": unit, "hint": hint, "why": why[:300], "at": now()}

    def json(self, site: Site, unit: str, hint: dict, url: str, **kw):
        """As get, for a JSON answer: the parsed body, or None (unit kept for a retry) when it does not parse."""
        r = self.get(site, unit, hint, url, **kw)
        if r is None:
            return None
        try:
            return r.json()
        except ValueError as e:
            self.skip(unit, hint, f"not JSON: {e}")
            return None


LISTING = _Listing()


def _recent(d: date) -> bool:
    """Too new to call a listing complete: sources add decisions for a few weeks after their date."""
    return (date.today() - d).days < 30


# ================================================================ the forums: how each one lists its decisions
# A lister yields one dict per published document: forum, bench, case_number, parties, date (ISO or None as printed),
# date_shown, judges, decision_type, subject (as listed), source_url (the document; None when the list shows none),
# page_url (where the list is), year (the year the source files it under) and part (for the text file's name).


@dataclass
class Forum:
    key: str
    name: str
    source: str
    lister: Callable[..., Iterator[dict]]
    pause: float = 1.5
    verify: bool = True
    stream: bool = False         # the lister already yields part by part, newest first (big forums)
    first_year: int | None = None
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- CAT (efiling.cgat.gov.in)

EFILING = "https://efiling.cgat.gov.in/ecat"
CAT_BENCHES = [("100", "Principal Bench, New Delhi"), ("120", "Ahmedabad"), ("330", "Allahabad"), ("103", "Bengaluru"),
               ("60", "Chandigarh"), ("310", "Chennai"), ("260", "Cuttack"), ("180", "Ernakulam"), ("921", "Gangtok"),
               ("40", "Guwahati"), ("21", "Hyderabad"), ("200", "Jabalpur"), ("291", "Jaipur"), ("117", "Jammu"),
               ("111", "Jodhpur"), ("350", "Kolkata"), ("332", "Lucknow"), ("210", "Mumbai"), ("116", "Patna"),
               ("119", "Srinagar")]


def _cat_rows(html: str) -> list[dict]:
    out = []
    for tr in re.split(r"<tr[\s>]", html)[1:]:
        link = re.search(r"""href=["']\./pdf/judge\.php\?file=([^"'&\s]+)""", tr)
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if not link or len(cells) < 4:
            continue
        sides = [clean(x) for x in re.findall(r"<font color=['\"]red['\"]>(.*?)</font>", cells[2], re.S | re.I)]
        sides = [x for x in sides if x]
        out.append({"serial": clean(cells[0]), "case_number": clean(cells[1]) or None,
                    "parties": " vs ".join(sides) if sides else (clean(cells[2]) or None),
                    "date_shown": clean(cells[3]), "file": link.group(1)})
    return out


def cat_list(site: Site, years: list[int] | None = None, hint: dict | None = None) -> Iterator[dict]:
    benches = CAT_BENCHES
    if hint:
        years, benches = [hint["year"]], [b for b in CAT_BENCHES if b[0] == hint.get("bench_code")]
    for y in years or range(THIS_YEAR, 2009, -1):
        for code, bench in benches:
            unit, past = f"cat|{code}|{y}", not _recent(date(y, 12, 31))
            if not hint and LISTING.is_done(unit):
                continue
            url = f"{EFILING}/fiorder_detail.php?benchCode3={code}&from_date=01/01/{y}&to_date=31/12/{y}&id=partynamewise"
            r0 = LISTING.get(site, unit, {"year": y, "bench_code": code}, url)
            if r0 is None:
                continue
            rows = _cat_rows(r0.text)
            print(f"    CAT {bench} {y}: {len(rows)} listed", flush=True)
            if not rows:
                yield {"unit": unit, "unit_past": past, "empty_unit": True}
            for r in rows:
                yield {"forum": "CAT", "bench": bench, "bench_code": code, "case_number": r["case_number"],
                       "parties": r["parties"], "date": iso_date(r["date_shown"]), "date_shown": r["date_shown"],
                       "judges": None, "decision_type": "Oral / final order", "subject": r["parties"],
                       "source_url": f"{EFILING}/pdf/judge.php?file={r['file']}", "page_url": url,
                       "year": y, "part": slug(bench, 30), "unit": unit, "unit_past": past}


# ---------------------------------------------------------------- APTEL (aptel.gov.in)

APTEL = "https://www.aptel.gov.in"


def aptel_list(site: Site, years: list[int] | None = None, hint: dict | None = None) -> Iterator[dict]:
    for y in ([hint["year"]] if hint else years or range(THIS_YEAR, 2007, -1)):
        url = f"{APTEL}/en/old-judgement-data?field_judge_year_value={y}"
        r0 = LISTING.get(site, f"aptel|{y}", {"year": y}, url)
        if r0 is None:
            continue
        soup = BeautifulSoup(r0.text, "lxml")
        n = 0
        for tr in soup.select("tbody tr") or soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5 or clean(tds[0].get_text()).upper().startswith("S. NO"):
                continue
            serial = tds[0].get_text(" ", strip=True)
            links = []
            for a in tds[1].find_all("a", href=True):
                u = absolute(APTEL + "/", a["href"])
                if u not in links:
                    links.append(u)
            paras = [p.get_text(" ", strip=True) for p in tds[2].find_all("p")] or [tds[2].get_text(" ", strip=True)]
            paras = [re.sub(r"\s+", " ", p).strip() for p in paras if p and p.strip()]
            parties = " ".join("vs" if p.lower() in ("versus", "vs", "vs.") else p for p in paras) or None
            judges = re.sub(r"\s+", " ", " ".join(p.get_text(" ", strip=True) for p in tds[3].find_all("p")) or tds[3].get_text(" ", strip=True)).strip() or None
            when = tds[4].get_text(" ", strip=True)
            decided = re.split(r"Uploaded", when, flags=re.I)[0]
            up = re.search(r"Uploaded\s+On\s+([\d./-]+)", when, re.I)
            case = re.sub(r"\s+", " ", tds[1].get_text(" ", strip=True)).strip() or None
            base = {"forum": "APTEL", "bench": "New Delhi", "case_number": case, "parties": parties, "date": dmy(decided),
                    "date_shown": decided.strip(" -"), "uploaded_on": dmy(up.group(1)) if up else None, "judges": judges,
                    "decision_type": None, "subject": f"{case} - {parties}" if parties else case, "serial": serial,
                    "page_url": url, "year": y, "part": None}
            n += 1
            if not links:
                yield {**base, "source_url": None, "key": f"{url}#{serial}"}
            for i, u in enumerate(links, 1):
                yield {**base, "source_url": u, **({"document": f"{i} of {len(links)}"} if len(links) > 1 else {})}
        print(f"    APTEL {y}: {n} listed", flush=True)


# ---------------------------------------------------------------- CCI (cci.gov.in)

CCI = "https://www.cci.gov.in"
CCI_LISTS = [  # (category, page, ajax url, method, columns, file fields)
    ("antitrust", "/antitrust/orders", "/antitrust/orders/list", "POST",
     ["DT_RowIndex", "case_no", "description", "type", "main_order_date", "order_date", "files"], ["file_content"]),
    ("combination-s31", "/combination/orders-section31", "/combination/orders-section31", "GET",
     ["DT_RowIndex", "combination_no", "party_name", "form_type", "notification_date", "order_status", "decision_date",
      "summary_files", "order_files"], ["order_file_content"]),
    ("combination-approved-with-modification", "/combination/cases-approved-with-modification",
     "/combination/cases-approved-with-modification", "GET",
     ["DT_RowIndex", "combination_no", "party_name", "form_type", "notification_date", "order_status", "decision_date",
      "summary_files", "order_files"], ["order_file_content"]),
    ("combination-penalty-43a-44", "/combination/orders-section43a_44", "/combination/orders-section43a_44", "GET",
     ["DT_RowIndex", "combination_no", "description", "section", "decision_date", "order_files"], ["order_file_content"]),
]


def _cci_files(raw) -> list[dict]:
    if not raw:
        return []
    try:
        items = json.loads(htmllib.unescape(str(raw)))
    except ValueError:
        return []
    return [i for i in items if isinstance(i, dict) and i.get("file_name")]


def cci_list(site: Site, years: list[int] | None = None, hint: dict | None = None) -> Iterator[dict]:
    r0 = LISTING.get(site, "cci|token", dict(hint or {}), CCI + "/antitrust/orders")
    if r0 is None:
        return
    m = re.search(r'name="_token"[^>]*value="([^"]+)"', r0.text) or re.search(r'csrf-token"\s+content="([^"]+)"', r0.text)
    token = m.group(1) if m else ""
    for cat, list_page, ajax, method, cols, ffields in CCI_LISTS:
        if hint and hint.get("category") != cat:
            continue
        start, total, n = 0, None, 0
        unit, uhint = f"cci|{cat}", {"category": cat}  # a failed page: the whole list is read again on retry
        while total is None or start < total:
            params = [("draw", "1")]
            for i, c in enumerate(cols):
                params += [(f"columns[{i}][data]", c), (f"columns[{i}][name]", c)]
            params += [("order[0][column]", "0"), ("order[0][dir]", "desc"), ("start", str(start)), ("length", "100")]
            hdr = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json", "Referer": CCI + list_page}
            if method == "POST":
                params = [("_token", token)] + params + [("case_type", ""), ("case_no", ""), ("case_year", "")]
                j = LISTING.json(site, unit, uhint, CCI + ajax, method="POST", content=urlencode(params),
                                 headers={**hdr, "X-CSRF-TOKEN": token, "Content-Type": "application/x-www-form-urlencoded"})
            else:
                j = LISTING.json(site, unit, uhint, CCI + ajax + "?" + urlencode(params), headers=hdr)
            if j is None:
                break
            total = int(j.get("recordsTotal") or 0)
            data = j.get("data") or []
            if not data:
                break
            for d in data:
                n += 1
                case = d.get("case_no") or d.get("combination_no")
                parties = clean(d.get("description") if cat == "antitrust" or not d.get("party_name") else d.get("party_name")) or None
                if cat == "antitrust":
                    when, kind = d.get("order_date") or d.get("main_order_date"), " ".join(x for x in (clean(d.get("title")), d.get("type")) if x)
                else:
                    when = d.get("decision_date") or d.get("date_of_order") or d.get("order_date")
                    kind = " / ".join(x for x in (d.get("order_type"), d.get("order_status"), clean(d.get("section")) if d.get("section") else None) if x)
                detail = (f"{CCI}/antitrust/orders/details/{d.get('id')}/0" if cat == "antitrust" else CCI + list_page)
                files = [f for ff in ffields for f in _cci_files(d.get(ff))]
                base = {"forum": "CCI", "bench": "New Delhi", "category": cat, "case_number": case, "parties": parties,
                        "date": iso_date(when), "date_shown": when, "judges": None, "decision_type": kind or None,
                        "subject": f"{case} - {parties}" if parties else case, "cci_id": d.get("id"), "page_url": detail,
                        "notification_date": iso_date(d.get("notification_date")) if d.get("notification_date") else None,
                        "part": None}
                base["year"] = int(base["date"][:4]) if base["date"] else None
                if not files:
                    yield {**base, "source_url": None, "key": f"{CCI}{list_page}#{d.get('id')}"}
                for f in files:
                    u = CCI + "/" + f["file_name"].replace("\\/", "/").lstrip("/")
                    yield {**base, "source_url": u, "document": f.get("title"), "size_listed_kb": f.get("file_size")}
            start += len(data)
        print(f"    CCI {cat}: {n} listed of {total}", flush=True)


# ---------------------------------------------------------------- TDSAT (tdsat.gov.in)

TDSAT = "https://tdsat.gov.in"


def tdsat_list(site: Site, years: list[int] | None = None, hint: dict | None = None) -> Iterator[dict]:
    url = f"{TDSAT}/Delhi/services/judgment.php"
    for y in ([hint["year"]] if hint else years or range(THIS_YEAR, 1999, -1)):
        r0 = LISTING.get(site, f"tdsat|{y}", {"year": y}, url, method="POST",
                         data={"from_date1": f"01/01/{y}", "to_date1": f"31/12/{y}", "frm3": "", "submit11": "Go"},
                         headers={"Referer": url})
        if r0 is None:
            continue
        html = r0.text
        n = 0
        for tr in re.split(r"<tr[\s>]", html)[1:]:
            link = re.search(r'href="([^"]+\.pdf)"', tr, re.I)
            cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if not link or len(cells) < 5:
                continue
            sides = [clean(x) for x in re.findall(r"<font color='black'>(.*?)</font>", cells[3], re.S | re.I)]
            sides = [x for x in sides if x]
            case, shown = clean(cells[1]) or None, clean(cells[4])
            parties = " vs ".join(sides) if sides else clean(cells[3]) or None
            n += 1
            yield {"forum": "TDSAT", "bench": "New Delhi", "case_number": case, "parties": parties, "date": iso_date(shown),
                   "date_shown": shown, "judges": clean(cells[2]).strip(" ,") or None, "decision_type": "Judgment (final order)",
                   "subject": f"{case} - {parties}", "source_url": absolute(TDSAT + "/", link.group(1)),
                   "page_url": f"{url} (date-wise {y})", "year": y, "part": None}
        print(f"    TDSAT {y}: {n} listed", flush=True)


# ---------------------------------------------------------------- GST AAR / AAAR (gstcouncil.gov.in)

GST = "https://gstcouncil.gov.in"
GST_LISTS = [("AAR", "/authority-for-advance-ruling", "Advance ruling (AAR)"),
             ("AAAR", "/appellate-orders", "Appellate advance ruling (AAAR)")]


def _td(tr, key: str):
    return tr.find("td", headers=re.compile(key))


def gst_list(site: Site, years: list[int] | None = None, hint: dict | None = None) -> Iterator[dict]:
    for kind, path, label in GST_LISTS:
        if hint and hint.get("category") != kind:
            continue
        r0 = LISTING.get(site, f"gst|{kind}", {"category": kind}, GST + path)
        if r0 is None:
            continue
        first = r0.text
        last = max([int(x) for x in re.findall(r"[?&]page=(\d+)", first)] or [0])
        pages = range(hint["list_page"], min(last, hint["list_page"] + 3) + 1) if hint and "list_page" in hint else range(0, last + 1)
        n = 0
        for p in pages:
            if p == 0:
                html = first
            else:
                rp = LISTING.get(site, f"gst|{kind}|{p}", {"category": kind, "list_page": p}, GST + path, params={"page": p})
                if rp is None:
                    continue
                html = rp.text
            soup = BeautifulSoup(html, "lxml")
            for tr in soup.select("table tbody tr"):
                tds = tr.find_all("td")
                if not tds:
                    continue
                applicant = clean(str(_td(tr, "view-title") or "")) or None
                state = clean(str(_td(tr, "states-ut") or "")) or None
                brief = clean(str(_td(tr, "view-body") or "")) or None
                order = clean(str(_td(tr, "order-no-date") or "")) or None
                against = clean(str(_td(tr, "ar-order-no") or "")) or None
                category = clean(str(_td(tr, "field-category") or "")) or None
                fcell = _td(tr, "upload-file") or _td(tr, "orders-file") or tr
                links = []
                for a in fcell.find_all("a", href=True):
                    u = absolute(GST + "/", a["href"])
                    if re.search(r"\.(pdf|docx?)($|\?)", u, re.I) and u not in links:
                        links.append(u)
                d = dmy(order, last=True)
                n += 1
                base = {"forum": "GST AAR", "bench": state, "category": kind, "case_number": order, "parties": applicant,
                        "date": d, "date_shown": order, "judges": None, "decision_type": label, "subject": brief,
                        "goods_service_category": category, "against_ruling": against, "list_page": p,
                        "page_url": f"{GST}{path}?page={p}", "year": int(d[:4]) if d else None, "part": None}
                if not links:
                    yield {**base, "source_url": None, "key": f"{GST}{path}#{applicant}|{order}"}
                for i, u in enumerate(links, 1):
                    yield {**base, "source_url": u, **({"document": f"{i} of {len(links)}"} if len(links) > 1 else {})}
        print(f"    GST {kind}: {n} listed ({len(pages)} pages)", flush=True)


# ---------------------------------------------------------------- ATFP (atfp.gov.in)

ATFP = "https://atfp.gov.in"
ATFP_ACTS = ["PMLA/FPA-PMLA", "FEMA/FERA/FPA-FE", "FPA/BP", "NDPS/FPA/ND", "SAFEMA/FPA–1"]


def _atfp(site: Site, kind: str, unit: str, hint: dict, **filters) -> list[dict] | None:
    """Every row of one ATFP query (all its pages); None when a page could not be had (the unit is kept for a retry)."""
    ep = "judgementsdetails.aspx/GetJudgmentSearch" if kind == "judgments" else "Ordersdetails.aspx/GetOrders"
    model = {"DDDate": None, "DDMONTH": None, "DDYEAR": None, "ChairmanName": None, "ACTAPPEALTYPE": None,
             "AppeRespo": None, ("z" if kind == "judgments" else "q"): None, "PageNumber": 1, "PageSize": 10, **filters}
    out, page = [], 1
    while True:
        model["PageNumber"] = page
        j = LISTING.json(site, unit, hint, f"{ATFP}/{ep}", method="POST", content=json.dumps({"model": model}),
                         headers={"Content-Type": "application/json; charset=utf-8", "Referer": f"{ATFP}/{ep.split('/')[0]}"})
        if j is None:
            return None
        if not isinstance(j.get("d"), dict):
            LISTING.skip(unit, hint, f"unexpected answer: {str(j)[:150]}")
            return None
        d = j["d"]
        out += d.get("Data") or []
        if page >= int(d.get("TotalPages") or 1):
            return out
        page += 1


def atfp_list(site: Site, years: list[int] | None = None, hint: dict | None = None) -> Iterator[dict]:
    years = [hint["year"]] if hint else list(years or range(THIS_YEAR, 2012, -1))
    today = date.today()
    for kind in ("judgments", "orders"):
        if hint and hint.get("category") != kind:
            continue
        for y in years:
            n = 0
            if kind == "judgments":
                months = [int(hint["date"][5:7])] if hint and hint.get("date") else range(12, 0, -1)
                acts = [hint["act"]] if hint and hint.get("act") else ATFP_ACTS
                queries = [dict(ACTAPPEALTYPE=a, DDMONTH=f"{m:02d}", DDYEAR=str(y), act=a) for m in months for a in acts]
            else:
                if hint and hint.get("date"):
                    days = [date.fromisoformat(hint["date"])]
                else:
                    last = date(y, 12, 31) if y < today.year else today
                    days = [date.fromordinal(o) for o in range(last.toordinal(), date(y, 1, 1).toordinal() - 1, -1)]
                queries = [dict(DDDate=f"{d.day:02d}", DDMONTH=f"{d.month:02d}", DDYEAR=str(y), act=None) for d in days]
            for q in queries:
                act = q.pop("act")
                m = int(q["DDMONTH"])
                if (kind == "judgments" and y == today.year and m > today.month):
                    continue
                if kind == "judgments":
                    unit = f"atfp|j|{act}|{y}-{m:02d}"
                    uhint = {"category": kind, "year": y, "date": f"{y}-{m:02d}-01", "act": act}
                    past = not _recent(date(y, m, calendar.monthrange(y, m)[1]))
                else:
                    day = date(y, m, int(q["DDDate"]))
                    unit, uhint, past = f"atfp|o|{day.isoformat()}", {"category": kind, "year": y, "date": day.isoformat()}, not _recent(day)
                if not hint and LISTING.is_done(unit):
                    continue
                items = _atfp(site, kind, unit, uhint, **q)
                if items is None:
                    continue
                if not items:
                    yield {"unit": unit, "unit_past": past, "empty_unit": True}
                for it in items:
                    f = it.get("JudgmentFile") if kind == "judgments" else it.get("OrderFile")
                    shown = it.get("JudgmentDate") if kind == "judgments" else it.get("OrderDate")
                    title = clean(it.get("JudgementTitle") if kind == "judgments" else it.get("OrderTitle")) or None
                    folder = "Judgement" if kind == "judgments" else "Order"
                    n += 1
                    yield {"forum": "ATFP", "bench": "New Delhi", "category": kind, "act": act,
                           "case_number": clean(it.get("CaseNo")) or None, "parties": title, "date": iso_date(shown),
                           "date_shown": shown, "judges": None, "decision_type": "Judgment" if kind == "judgments" else "Order",
                           "subject": title, "source_url": f"{ATFP}/writereaddata/upload/{folder}/{f}" if f else None,
                           "key": None if f else f"{ATFP}#{kind}|{it.get('CaseNo')}|{shown}",
                           "page_url": f"{ATFP}/{'judgementsdetails' if kind == 'judgments' else 'Ordersdetails'}.aspx?" + urlencode({k: v for k, v in q.items() if v}),
                           "year": y, "part": kind, "unit": unit, "unit_past": past}
            print(f"    ATFP {kind} {y}: {n} listed", flush=True)


# ---------------------------------------------------------------- IBBI's own orders (ibbi.gov.in)

IBBI = "https://ibbi.gov.in"
IBBI_LISTS = [("ibbi", "IBBI"), ("ipa-rvo", "IPA / RVO"), ("drts", "DRT (IBC orders, as IBBI publishes them)"),
              ("drat", "DRAT (IBC orders, as IBBI publishes them)")]


def ibbi_list(site: Site, years: list[int] | None = None, hint: dict | None = None) -> Iterator[dict]:
    for cat, body in IBBI_LISTS:
        if hint and hint.get("category") != cat:
            continue
        url = f"{IBBI}/orders/{cat}"
        r0 = LISTING.get(site, f"ibbi|{cat}", {"category": cat}, url)
        if r0 is None:
            continue
        first = r0.text
        last = max([int(x) for x in re.findall(r"(?:[?&]|&amp;)page=(\d+)", first)] or [1])
        pages = range(hint["list_page"], min(last, hint["list_page"] + 2) + 1) if hint and "list_page" in hint else range(1, last + 1)
        n = 0
        for p in pages:
            if p == 1:
                html = first
            else:
                rp = LISTING.get(site, f"ibbi|{cat}|{p}", {"category": cat, "list_page": p}, url, params={"page": p})
                if rp is None:
                    continue
                html = rp.text
            # old orders are linked unquoted, with spaces in the file name: quote each whole name up to .pdf first
            html = re.sub(r"href=([^\"'\s>][^\"'>]*?\.pdf)(?=\s|>)", lambda m: f'href="{m.group(1)}"', html, flags=re.I)
            table = BeautifulSoup(html, "lxml").find("table")
            for tr in (table.find_all("tr") if table else [])[1:]:
                tds = tr.find_all("td")
                a = tr.find("a", href=True)
                if len(tds) < 3 or not a:
                    continue
                texts = [td.get_text(" ", strip=True) for td in tds]
                link_i = next(i for i, td in enumerate(tds) if td.find("a", href=True))
                date_i = next((i for i, t in enumerate(texts) if i not in (0, link_i) and iso_date(t)), None)
                rest = [t for i, t in enumerate(texts) if i not in (0, link_i, date_i) and t]
                label = a.get_text(" ", strip=True)
                subject = re.sub(r"\s*\(?[\d.]+\s*[KMG]B\)?\s*$", "", label).strip()
                m = re.match(r"\s*In the matter of\s+(.*?)\s*,?\s*\[(.*)\]\s*$", subject, re.I | re.S)
                parties = m.group(1).strip(" ,") if m else re.sub(r"^\s*(Order of .*? )?in the matter of\s+", "", subject, flags=re.I).strip() or None
                d = iso_date(texts[date_i]) if date_i is not None else None
                n += 1
                yield {"forum": "IBBI", "bench": body, "category": cat, "case_number": m.group(2).strip() if m else None,
                       "parties": parties, "date": d, "date_shown": texts[date_i] if date_i is not None else None,
                       "judges": None, "decision_type": rest[0] if rest else None, "subject": subject,
                       "source_url": absolute(IBBI + "/", a["href"]).replace("&amp;", "&").replace("gov.in//", "gov.in/"),
                       "list_page": p, "page_url": f"{url}?page={p}", "year": int(d[:4]) if d else None, "part": None}
        print(f"    IBBI {cat}: {n} listed ({len(pages)} pages)", flush=True)


FORUMS: dict[str, Forum] = {f.key: f for f in [
    Forum("ibbi", "Insolvency and Bankruptcy Board of India (own orders; IPA/RVO; DRT/DRAT under the IBC)",
          "Insolvency and Bankruptcy Board of India (ibbi.gov.in/orders)", ibbi_list),
    Forum("tdsat", "Telecom Disputes Settlement and Appellate Tribunal", "TDSAT (tdsat.gov.in), judgments", tdsat_list,
          verify=False),
    Forum("cci", "Competition Commission of India", "Competition Commission of India (cci.gov.in), orders", cci_list),
    Forum("aptel", "Appellate Tribunal for Electricity", "APTEL (aptel.gov.in), all judgements/orders", aptel_list,
          verify=False),
    Forum("gst-aar", "GST Authorities (and Appellate Authorities) for Advance Ruling",
          "GST Council (gstcouncil.gov.in), AAR and AAAR orders of the States", gst_list),
    Forum("atfp", "Appellate Tribunal for Forfeited Property", "ATFP (atfp.gov.in), judgments and orders", atfp_list,
          verify=False, stream=True),
    Forum("cat", "Central Administrative Tribunal", "CAT e-filing (efiling.cgat.gov.in), final/oral orders", cat_list,
          pause=1.0, verify=False, stream=True),
]}

SKIPPED = [
    {"forum": "SAT", "name": "Securities Appellate Tribunal", "site": "satweb.sat.gov.in/orders",
     "why": "captcha: every order search (appeal no., AL no., party, date-wise) asks for a captcha; no open list of orders"},
    {"forum": "ITAT", "name": "Income Tax Appellate Tribunal", "site": "itat.gov.in", "why": "captcha on the order search"},
    {"forum": "CESTAT", "name": "Customs, Excise and Service Tax Appellate Tribunal", "site": "cestat.gov.in",
     "why": "captcha: the order search requires a captcha code"},
    {"forum": "NGT", "name": "National Green Tribunal", "site": "greentribunal.gov.in", "why": "captcha on the judgment search"},
    {"forum": "DRT/DRAT", "name": "Debts Recovery Tribunals and Appellate Tribunals", "site": "drt.gov.in",
     "why": "captcha on DRT's own search; left out by the brief. (Their IBC orders that IBBI publishes are in 'ibbi'.)"},
    {"forum": "RCT", "name": "Railway Claims Tribunal", "site": "rct.indianrail.gov.in",
     "why": "captcha on the case/judgment search"},
    {"forum": "NCDRC / consumer commissions", "name": "National and State Consumer Commissions", "site": "e-jagriti.gov.in",
     "why": "captcha on e-Jagriti's judgment search"},
    {"forum": "AFT", "name": "Armed Forces Tribunal", "site": "aftdelhi.nic.in, aft.gov.in",
     "why": "unreachable: connection timed out on every try (26 Oct 2026)"},
    {"forum": "CAT (archive 1985-2021)", "name": "Central Administrative Tribunal judgment archive",
     "site": "catjudgements.nic.in",
     "why": "unreachable: connection timed out on every try (26 Oct 2026); CAT's orders from 2015 on are loaded from e-filing"},
    {"forum": "MahaRERA", "name": "Maharashtra Real Estate Regulatory Authority", "site": "maharera.maharashtra.gov.in",
     "why": "its rulings search lists parties only: no order document and no date in the results"},
    {"forum": "SEBI orders", "name": "Securities and Exchange Board of India", "site": "sebi.gov.in",
     "why": "its list's next-page call answers scripts 'Unauthorized Activity Has Been Detected' (HTTP 530)"},
    {"forum": "NCLT/NCLAT", "name": "National Company Law Tribunal / Appellate Tribunal", "site": "ibbi.gov.in",
     "why": "loaded separately by web_tribunals.py (item 20), not here"},
]


# ================================================================ loading

def _ext(url: str) -> str:
    """The extension a decision is stored under: Word files as published (.doc/.docx), everything else .pdf."""
    m = re.search(r"\.(docx?)$", url.split("?")[0], re.I)
    return "." + m.group(1).lower() if m else ".pdf"


def _fetch_pdf(site: Site, url: str) -> bytes:
    """The published decision: a PDF, or a Word file where the source publishes one (checked by its own bytes)."""
    r = site.get(url, timeout=300)
    if r.status_code != 200:
        raise Blocked(f"HTTP {r.status_code}")
    data = r.content
    word = (_ext(url) == ".doc" and data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") or (_ext(url) == ".docx" and data[:2] == b"PK")
    if data[:1024].find(b"%PDF-") < 0 and not word:
        low = data[:3000].lower()
        raise Blocked("captcha page" if b"captcha" in low else f"not a PDF ({r.headers.get('content-type')}, {len(data)} bytes)")
    if len(data) > 300_000_000:
        raise Blocked(f"too large ({len(data)} bytes)")
    return data


def _key(r: dict) -> str:
    return r.get("source_url") or r.get("key")


def _get(lib: Library, store: str, path: str) -> bytes | None:
    """Read a file back from our own store, riding out a passing network hiccup."""
    for attempt in range(6):
        try:
            return lib.get(store, path)
        except Exception:  # noqa: BLE001 - httpx timeouts, Drive/B2 5xx
            if attempt == 5:
                raise
            time.sleep(10 * 2 ** attempt)
    return None


def _lines(data: bytes | None) -> list[str]:
    return [x for x in gzip.decompress(data).decode().split("\n") if x.strip()] if data else []


def _gz(lines) -> bytes:
    return gzip.compress("\n".join(lines).encode(), 9)


class Loader:
    """One forum's run: list, download one at a time, read text and write to the stores beside the downloads."""

    def __init__(self, f: Forum, workers: int = 3) -> None:
        self.f, self.workers = f, workers
        self.lib = Library(ITEM, f.source)
        self.site = Site(pause=f.pause, verify=f.verify, timeout=300)
        self.base = f"library/tribunals/{f.key}"
        self.bpath = f"judgments/tribunals/{f.key}.jsonl.gz"
        self.ipath = f"{self.base}/index.jsonl.gz"
        self.lock = threading.Lock()
        self.local = threading.local()
        self.groups: dict[str, Group] = {}
        self.counts = {"listed": 0, "no_document": 0, "already": 0, "new": 0, "failed": 0, "recovered_from_drive": 0}
        self.fails: list[dict] = []
        print(f"  {f.key}: reading what is already stored …", flush=True)
        self.stored = set(self.lib.known(self.base + "/"))
        self.summary: dict[str, str] = {}   # source_url -> JSON row without text (Backblaze)
        for line in _lines(_get(self.lib, "b2", self.bpath) if self.bpath in self.lib.known(self.bpath) else None):
            self.summary[json.loads(line)["source_url"]] = line
        self.index: dict[str, str] = {}     # key -> JSON listed row (Drive)
        if self.ipath in self.stored:
            for line in _lines(_get(self.lib, "drive", self.ipath))[1:]:
                self.index[_key(json.loads(line))] = line
        self.summary_dirty = 0
        self.last_summary = time.time()
        print(f"  {f.key}: {len(self.stored)} files in Drive, {len(self.summary)} decisions in the summary, "
              f"{len(self.index)} in the last listing", flush=True)

    # -- per-thread library (own Drive and Supabase clients)
    def wlib(self) -> Library:
        if self.workers == 1:
            return self.lib
        if not hasattr(self.local, "lib"):
            self.local.lib = Library(ITEM, self.f.source)
        return self.local.lib

    def group(self, r: dict) -> tuple[str, Group]:
        y = str(r["year"]) if r.get("year") else "undated"
        name = f"{y}-{r['part']}" if r.get("part") else y
        path = f"{self.base}/text/{name}.jsonl.gz"
        g = self.groups.get(path)
        if g is None:
            g = Group(self.lib, "drive", path, "tribunal_text", f"{self.f.name}: decisions {name} (text)",
                      court=self.f.key, year=int(y) if y.isdigit() else None)
            if path in self.stored and not g.rows:
                # Group starts empty when its read fails; saving that would overwrite the stored rows. Read it again
                # (with retries): a lasting failure stops the run rather than lose what is stored.
                data = _get(self.lib, "drive", path)
                if not data:
                    raise RuntimeError(f"{path} is listed in library_files but could not be read back")
                for row in unpack(data):
                    g.rows[row.get("source_url")] = row
            self.groups[path] = g
        return path, g

    def close_groups(self, keep: str | None = None) -> None:
        for p in list(self.groups):
            if p != keep:
                with self.lock:
                    self.groups[p].save()
                    if self.groups[p].rows:
                        self.stored.add(p)
                del self.groups[p]

    def save_summary(self, force: bool = False) -> None:
        with self.lock:
            if not self.summary_dirty and not force:
                return
            lines = sorted(self.summary.values())
            self.summary_dirty = 0
        data = _gz(lines)
        self.lib.put("b2", self.bpath, data, "judgments", title=f"{self.f.name}: decisions (summary, no text)",
                     court=self.f.key, rows=len(lines))
        self.last_summary = time.time()

    def save_index(self) -> None:
        if not self.index:
            return
        head = json.dumps({"_listing": {"forum": self.f.key, "saved_at": now(), "this_run": {k: v for k, v in self.counts.items()},
                                        "fails": self.fails[-200:]}}, ensure_ascii=False)
        data = _gz([head] + list(self.index.values()))
        self.lib.put("drive", self.ipath, data, "tribunal_index", title=f"{self.f.name}: everything the source listed",
                     court=self.f.key, rows=len(self.index))

    def finish(self, r: dict, g: Group, data: bytes, path: str) -> None:
        try:
            t = text_of(data, os.path.splitext(path)[1])  # PDF: pdftotext; .docx: its words; .doc: none (not guessed)
            row = {k: v for k, v in r.items() if k not in ("key", "part", "list_page", "bench_code", "unit", "unit_past")}
            row.update({"text": t["text"], "scan": bool(t.get("scan_only")), "pages": t.get("pages"), "file": path,
                        "store": "drive", "bytes": len(data), "sha1": hashlib.sha1(data).hexdigest(), "read_at": now()})
            for k in ("unreadable", "no_text_format"):
                if t.get(k):
                    row[k] = t[k]
            if path not in self.stored:
                self.wlib().put("drive", path, data, "tribunal_decision", title=(r.get("subject") or r.get("case_number") or "")[:300],
                                court=self.f.key, year=r.get("year"))
        except Exception as e:  # noqa: BLE001 - a store hiccup must not end a long run; the decision is retried next run
            self.fail(r, f"store: {e}")
            return
        with self.lock:
            g.add(row)
            self.stored.add(path)
            self.summary[row["source_url"]] = json.dumps({k: v for k, v in row.items() if k != "text"}, ensure_ascii=False)
            self.summary_dirty += 1
            self.counts["new"] += 1
            if g.dirty >= 100:
                g.save()
                self.stored.add(g.path)

    def fail(self, r: dict, why: str) -> None:
        with self.lock:
            self.counts["failed"] += 1
            self.fails.append({"url": r.get("source_url"), "case_number": r.get("case_number"), "why": why[:300]})
            if r.get("unit"):  # a unit with a failed decision is listed again next run, so the decision is retried
                self.bad_units.add(r["unit"])
                LISTING.done.discard(r["unit"])
        print(f"    failed {r.get('case_number')} {r.get('source_url')}: {why[:160]}", flush=True)

    # -- progress (units done, listings skipped), kept in Drive so a restart neither lists again nor forgets
    def load_progress(self, relist: bool) -> None:
        self.ppath = f"{self.base}/_progress.json"
        LISTING.done, LISTING.skipped, LISTING.use_done = set(), {}, not relist
        if self.ppath in self.stored:
            p = json.loads(_get(self.lib, "drive", self.ppath) or b"{}")
            LISTING.done = set(p.get("done") or [])
            LISTING.skipped = {u["unit"]: u for u in p.get("skipped") or []}
        self.last_checkpoint = time.time()
        if LISTING.done or LISTING.skipped:
            print(f"  {self.f.key}: {len(LISTING.done)} listings already complete"
                  f"{' (listed again: --relist)' if relist else ' (not listed again)'}; "
                  f"{len(LISTING.skipped)} skipped last time, to retry", flush=True)

    def save_progress(self) -> None:
        with self.lock:
            body = {"forum": self.f.key, "saved_at": now(), "done": sorted(LISTING.done), "skipped": list(LISTING.skipped.values())}
        self.lib.put("drive", self.ppath, json.dumps(body, ensure_ascii=False).encode(), "tribunal_progress",
                     content_type="application/json", title=f"{self.f.name}: listings complete / skipped (for restarts)",
                     court=self.f.key, rows=len(body["done"]))
        self.stored.add(self.ppath)

    def drain(self) -> None:
        for p in self.pending:
            p.result()
        self.pending = []

    def checkpoint(self) -> None:
        """Everything handled so far written out (text files, summary, index, progress), in that order, so a hard stop
        (the laptop closing) loses nothing a restart cannot redo."""
        self.drain()
        with self.lock:
            for g in self.groups.values():
                g.save()
                if g.rows:
                    self.stored.add(g.path)
        self.save_summary()
        self.save_index()
        self.save_progress()
        self.last_checkpoint = time.time()

    def unit_seen(self, r: dict) -> None:
        u = r.get("unit")
        if u == self.cur_unit:
            return
        self.complete_unit()
        self.cur_unit, self.cur_past = u, bool(r.get("unit_past"))
        if time.time() - self.last_checkpoint > 900:
            self.checkpoint()

    def complete_unit(self) -> None:
        u = self.cur_unit
        if u and self.cur_past and u not in self.bad_units:
            with self.lock:
                LISTING.done.add(u)
        self.cur_unit = None

    def register(self, r: dict) -> None:
        self.counts["listed"] += 1
        r["listed_at"] = now()[:10]
        self.index[_key(r)] = json.dumps({x: v for x, v in r.items() if x not in ("part", "unit", "unit_past")}, ensure_ascii=False)

    def consume(self, rows: Iterator[dict], registered: bool) -> str | None:
        """Handle listed rows one by one; returns why it stopped early (pause, limit), None when the rows ran out."""
        f = self.f
        for r in rows:
            if r.get("empty_unit"):  # a listing that answered, with nothing in it
                self.unit_seen(r)
                continue
            if not registered:
                self.register(r)
            self.unit_seen(r)
            if self.list_only:
                continue
            if not r.get("source_url"):
                self.counts["no_document"] += 1
                continue
            gpath, g = self.group(r)
            if gpath != self.current:
                self.drain()
                self.close_groups(keep=gpath)
                self.current = gpath
                if time.time() - self.last_summary > 1200:
                    self.save_summary()
            if g.has(r["source_url"]):
                self.counts["already"] += 1
                if r["source_url"] not in self.summary:  # text row there but summary lost: rebuild its line
                    row = g.rows[r["source_url"]]
                    self.summary[r["source_url"]] = json.dumps({x: v for x, v in row.items() if x != "text"}, ensure_ascii=False)
                    self.summary_dirty += 1
                continue
            if paused():
                self.cur_unit = None  # the unit in hand is not complete
                return f"paused at {f.key} {r.get('year')} {r.get('bench') or ''} {r.get('case_number')} (listing row {self.counts['listed']})"
            if self.limit is not None and self.counts["new"] + self.counts["failed"] + len(self.pending) >= self.limit:
                self.cur_unit = None
                return f"limit {self.limit} reached"
            y = str(r["year"]) if r.get("year") else "undated"
            path = f"{self.base}/{y}/{file_name(r.get('case_number') or r.get('subject') or '', r['source_url'], _ext(r['source_url']))}"
            try:
                if path in self.stored:
                    data = _get(self.lib, "drive", path)  # stored by an earlier run that stopped before its text was saved
                    if not data:
                        raise Blocked("listed in library_files but missing in Drive")
                    self.counts["recovered_from_drive"] += 1
                else:
                    data = _fetch_pdf(self.site, r["source_url"])
            except Blocked as e:
                self.fail(r, str(e))
                continue
            except Exception as e:  # noqa: BLE001
                self.fail(r, f"{type(e).__name__}: {e}")
                continue
            if self.pool:
                self.lib.drive.folder(f"{self.base}/{y}")  # made once here, so workers only ever find it
                self.pending.append(self.pool.submit(self.finish, r, g, data, path))
                while sum(1 for p in self.pending if not p.done()) > self.workers * 3:
                    time.sleep(0.2)
                self.pending = [p for p in self.pending if not p.done()]
            else:
                self.finish(r, g, data, path)
            n = self.counts["new"] + self.counts["failed"]
            if n and n % 100 == 0:
                print(f"    {f.key}: {self.counts}", flush=True)
        return None

    def run(self, years: list[int] | None, limit: int | None, list_only: bool, relist: bool = False) -> dict:
        f = self.f
        self.limit, self.list_only = limit, list_only
        self.pool = ThreadPoolExecutor(max_workers=self.workers) if self.workers > 1 else None
        self.pending: list = []
        self.current = None
        self.cur_unit, self.cur_past, self.bad_units = None, False, set()
        stop = None
        try:
            self.load_progress(relist)
            rows: Iterator[dict] = f.lister(self.site, years)
            if f.stream:
                stop = self.consume(rows, registered=False)
            else:
                listed = [r for r in rows if not r.get("empty_unit")]  # small forums: list everything, then load newest first
                for r in listed:
                    self.register(r)
                listed.sort(key=lambda r: (-(r.get("year") or 0), r.get("part") or ""))
                stop = self.consume(iter(listed), registered=True)
            if stop is None:
                self.complete_unit()
                # Retry pass: listings that failed in this run (or the last), once more, through the same steps.
                LISTING.retrying = True
                for u in list(LISTING.skipped.values()):
                    if paused():
                        stop = f"paused in the retry pass, before {u['unit']}"
                        break
                    print(f"    retrying listing {u['unit']} (skipped: {u['why'][:100]})", flush=True)
                    stop = self.consume(f.lister(self.site, None, hint=u["hint"]), registered=False)
                    if stop:
                        break
                    self.complete_unit()
                LISTING.retrying = False
                if LISTING.skipped:
                    print(f"  {f.key}: {len(LISTING.skipped)} listings still failing, kept for the next run: "
                          f"{', '.join(list(LISTING.skipped)[:10])}", flush=True)
        except KeyboardInterrupt:
            stop = "interrupted (Ctrl-C)"
            self.cur_unit = None
        except Exception as e:  # noqa: BLE001 - whatever it was, write out what is done; the watcher starts it again
            import traceback
            traceback.print_exc()
            stop = f"crashed: {type(e).__name__}: {str(e)[:200]}"
            self.cur_unit = None
        finally:
            if self.pool:
                self.pool.shutdown(wait=True)
            for step in (self.close_groups, lambda: None if list_only else self.save_summary(force=self.summary_dirty > 0 or not self.summary),
                         self.save_index, self.save_progress):
                try:
                    step()
                except Exception as e:  # noqa: BLE001
                    print(f"  {f.key}: could not write out ({type(e).__name__}: {e})", flush=True)
                    stop = stop if (stop or "").startswith("crashed") else f"crashed while writing out: {type(e).__name__}: {e}"
        res = {**self.counts, "stopped": stop or "finished", "stored_in_summary": len(self.summary),
               "listings_skipped": len(LISTING.skipped)}
        print(f"  {f.key}: {res}", flush=True)
        return res


# ================================================================ status, verify, skipped

def status() -> None:
    lib = Library(ITEM, "status")
    for key, f in FORUMS.items():
        drive = lib.known(f"library/tribunals/{key}/")
        b2 = lib.known(f"judgments/tribunals/{key}.jsonl.gz")
        rows = [json.loads(x) for x in _lines(_get(lib, "b2", f"judgments/tribunals/{key}.jsonl.gz"))] if b2 else []
        listed, meta = 0, {}
        if f"library/tribunals/{key}/index.jsonl.gz" in drive:
            lines = _lines(_get(lib, "drive", f"library/tribunals/{key}/index.jsonl.gz"))
            meta, listed_rows = json.loads(lines[0]).get("_listing", {}), [json.loads(x) for x in lines[1:]]
            listed = sum(1 for r in listed_rows if r.get("source_url"))
            nodoc = len(listed_rows) - listed
        else:
            nodoc = 0
        years = sorted({r["date"][:4] for r in rows if r.get("date")})
        pdfs = sum(1 for p in drive if p.endswith(".pdf"))
        print(f"  {key:8} listed {listed:>7} (+{nodoc} without a document) | stored {len(rows):>7} | PDFs {pdfs:>7} | "
              f"scan-only {sum(1 for r in rows if r.get('scan')):>6} | Drive {sum(drive.values()) / 1e9:6.2f} GB | "
              f"Backblaze {sum(b2.values()) / 1e6:6.2f} MB | years {years[0] if years else '-'}-{years[-1] if years else '-'} | "
              f"last run: {meta.get('this_run', {}).get('new', '-')} new, {meta.get('this_run', {}).get('failed', '-')} failed, saved {meta.get('saved_at', '-')[:16]}")


def verify(key: str, n: int = 3) -> bool:
    f = FORUMS[key]
    lib = Library(ITEM, f.source)
    site = Site(pause=f.pause, verify=f.verify, timeout=300)
    rows = [json.loads(x) for x in _lines(_get(lib, "b2", f"judgments/tribunals/{key}.jsonl.gz"))]
    if not rows:
        print(f"  {key}: nothing stored yet")
        return False
    ok_all = True
    for r in random.sample(rows, min(n, len(rows))):
        pdf = _get(lib, "drive", r["file"])
        y = str(r["year"]) if r.get("year") else "undated"
        part = r.get("category") if key == "atfp" else (slug(r["bench"], 30) if key == "cat" else None)
        text_rows = {x["source_url"]: x for x in unpack(_get(lib, "drive", f"library/tribunals/{key}/text/{y}{'-' + part if part else ''}.jsonl.gz") or b"")}
        t = text_rows.get(r["source_url"])
        hint = dict(r)
        if key == "cat":
            hint["bench_code"] = next(c for c, b in CAT_BENCHES if b == r["bench"])
        if key in ("gst-aar", "ibbi"):
            idx = {json.loads(x).get("source_url"): json.loads(x) for x in _lines(_get(lib, "drive", f"library/tribunals/{key}/index.jsonl.gz"))[1:]}
            hint["list_page"] = (idx.get(r["source_url"]) or {}).get("list_page", 0 if key == "gst-aar" else 1)
        live = next((x for x in f.lister(site, None, hint=hint) if x.get("source_url") == r["source_url"]), None)
        checks = {
            "pdf in Drive matches": bool(pdf) and hashlib.sha1(pdf).hexdigest() == r["sha1"],
            "text row in Drive": bool(t) and (t["scan"] or len(t["text"]) > 0),
            "found on the live list": live is not None,
        }
        if live:
            for fld in ("case_number", "date", "parties"):
                checks[f"{fld} same as live"] = (live.get(fld) or "") == (r.get(fld) or "")
        ok = all(checks.values())
        ok_all &= ok
        print(f"  {key} {'OK  ' if ok else 'FAIL'} {r.get('case_number')} | {r.get('date')} | {(r.get('parties') or '')[:70]} | "
              f"scan={r.get('scan')} | {', '.join(k for k, v in checks.items() if not v) or 'all checks pass'}")
    return ok_all


def write_skipped() -> None:
    lib = Library(ITEM, "Haazir: tribunal sites checked 26 Oct 2026")
    rows = [{**s, "checked_at": "2026-10-26"} for s in SKIPPED]
    data = _gz(json.dumps(r, ensure_ascii=False) for r in rows)
    lib.put("b2", "judgments/tribunals/_skipped.jsonl.gz", data, "tribunal_skipped",
            title="Tribunals and bodies left out (captcha, login, unreachable) and why", rows=len(rows))
    for r in rows:
        print(f"  {r['forum']}: {r['why']}")


EXIT_PAUSED, EXIT_CRASHED = 4, 3


def watch(keys: list[str]) -> int:
    """Keep these loads running: each as its own process (one per site); a crashed one is started again after a pause
    that grows with each crash (1, 2, 4 … 30 min); a paused one waits for 'resume'; a finished one is left alone."""
    def log(m: str) -> None:
        print(f"  watch {now()[:19]} {m}", flush=True)
    procs: dict[str, subprocess.Popen] = {}
    crashes: dict[str, int] = {}
    not_before: dict[str, float] = {}
    finished: set[str] = set()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    log(f"watching {', '.join(keys)}")
    while True:
        for k in keys:
            p = procs.get(k)
            if k in finished or (p and p.poll() is None):
                continue
            if p is not None:
                code = procs.pop(k).returncode
                if code == 0:
                    finished.add(k)
                    log(f"{k} finished")
                    continue
                if code == EXIT_PAUSED:
                    log(f"{k} stopped for the pause; starts again on resume")
                else:
                    crashes[k] = crashes.get(k, 0) + 1
                    wait = min(1800, 60 * 2 ** (crashes[k] - 1))
                    not_before[k] = time.time() + wait
                    log(f"{k} CRASHED (exit {code}, crash {crashes[k]}): starting again in {wait // 60} min")
            if paused() or time.time() < not_before.get(k, 0):
                continue
            out = open(os.path.join(HERE, f"_scan-tribunals-{k}.log"), "a", encoding="utf-8")
            procs[k] = subprocess.Popen([sys.executable, "-W", "ignore", "-u", "-m", "haazir.tribunals_lib", k], cwd=HERE,
                                        stdout=out, stderr=subprocess.STDOUT, env=env)
            log(f"{k} started (pid {procs[k].pid})")
        if all(k in finished for k in keys):
            log("all finished")
            return 0
        time.sleep(30)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # party names in any script, logs never crash on them
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=list(FORUMS) + ["all", "status", "verify", "skipped", "pause", "resume", "watch"])
    ap.add_argument("forum", nargs="*", help="verify: one forum; watch: the forums to keep running")
    ap.add_argument("--years", nargs="*", type=int)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--relist", action="store_true", help="list again even what an earlier run found complete")
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    for k in a.forum:
        if k not in FORUMS:
            ap.error(f"unknown forum {k!r} (one of {', '.join(FORUMS)})")
    a.forum = a.forum or []
    if a.cmd == "watch":
        return watch(a.forum or ["atfp", "cat"])
    if a.cmd == "pause":
        with open(PAUSE_FILE, "w", encoding="utf-8") as fh:
            fh.write(f"pause asked {now()}\n")
        print(f"  pause asked: running tribunal loads stop after the decision in hand ({PAUSE_FILE})")
        return 0
    if a.cmd == "resume":
        if os.path.exists(PAUSE_FILE):
            os.remove(PAUSE_FILE)
        print("  loads may run again: start them as before and they carry on where they stopped")
        return 0
    if a.cmd == "status":
        status()
        return 0
    if a.cmd == "skipped":
        write_skipped()
        return 0
    if a.cmd == "verify":
        ok = True
        for k in (a.forum or list(FORUMS)):
            ok &= verify(k)
        return 0 if ok else 1
    if paused():
        print(f"  paused ({PAUSE_FILE}): run 'python -m haazir.tribunals_lib resume' first")
        return EXIT_PAUSED
    code = 0
    for k in (list(FORUMS) if a.cmd == "all" else [a.cmd]):
        print(f"== {k}: {FORUMS[k].name} ({now()[:19]})", flush=True)
        stopped = str(Loader(FORUMS[k], workers=a.workers).run(a.years, a.limit, a.list_only, a.relist).get("stopped", ""))
        if stopped.startswith(("paused", "interrupted")):
            return EXIT_PAUSED
        if stopped.startswith("crashed"):
            code = EXIT_CRASHED  # 'all' goes on to the next forum; the exit code still says one crashed
    return code


if __name__ == "__main__":
    sys.exit(main())
