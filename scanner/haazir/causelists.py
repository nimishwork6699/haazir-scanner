"""Watched companies' names in the courts' published cause lists: an alert the day before a case is heard, even for
cases Haazir has not found yet (Supreme Court, NCLAT, High Courts whose lists are public).

    python -m haazir.causelists run [--days 2] [--only sci,nclat,delhi_hc,raj_hc] [--no-alert]
    python -m haazir.causelists try <pdf file> "state bank"      # how a list reads, without saving

Lists are public PDFs, linked from each court's cause-list page, with no captcha. Each list is read once (cause_lists);
each entry naming a watched company is kept (cause_list_hits) with the list's own words, matched to the case in Our
cases where the case number agrees, and sent once on Telegram to the legal head.
"""

import argparse
import base64
import html
import io
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urljoin

import httpx
from pypdf import PdfReader

from . import mail, telegram
from .db import DB
from .matching import normalise

PAUSE = 2.0          # seconds between requests to one site
MAX_PDF = 40 << 20   # larger files are skipped (a list is a few MB)
UA = {"User-Agent": "Mozilla/5.0 (Haazir case tracker; cause-list alerts)"}

# An entry starts with its item number and then the case: "8 SLP(Crl) No.", "12.  Comp App (AT)", "155 C.M.A. 880/2020".
# Items run in order (1, 2, 3... within a court), which tells an entry's start from a number inside its text.
ENTRY = re.compile(r"^\s*(\d{1,4})[.)]?\s+([A-Z(\[].*)$")
CASE_WORD = re.compile(r"\b(no\.?|diary|slp|appeal|app|petition|pet|i\.?a\.?|w\.?p|c\.?a\.?|crl|civ|misc|suit|comp|fao|rfa|lpa|ref|review|"
                       r"contempt|transfer|writ|cma|cmp|crp|mac|mact|arb|o\.?s|e\.?p|s\.?b|d\.?b|cr|cw)\b", re.I)
VERSUS = re.compile(r"^\s*(v/?s\.?|versus|vs\.?)\s*$|\s(v/?s\.?|versus)\s", re.I)
PAGE_LINE = re.compile(r"cause\s*list\s*for\s*dated|^\s*page\s*\d+(\s*of\s*\d+)?\s*$|^\s*\d{1,4}\s*$", re.I)
CASE_NO = re.compile(r"([A-Za-z][A-Za-z.()\s/-]{0,40}?)\s*(?:No\.?\s*)?(\d{1,7})(?:\s*-\s*\d{1,7})?\s*[/-]\s*((?:19|20)\d{2})")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Site:
    """One court website, asked gently."""

    def __init__(self) -> None:
        self.http = httpx.Client(timeout=90, follow_redirects=True, headers=UA)
        self.last = 0.0
        self.opened = None  # the list page last opened (Rajasthan serves a PDF only after its bench and day's page)

    def _wait(self) -> None:
        gap = time.time() - self.last
        if gap < PAUSE:
            time.sleep(PAUSE - gap)
        self.last = time.time()

    def get(self, url: str, **kw) -> httpx.Response:
        self._wait()
        r = self.http.get(url, **kw)
        r.raise_for_status()
        return r

    def post(self, url: str, **kw) -> httpx.Response:
        self._wait()
        r = self.http.post(url, **kw)
        r.raise_for_status()
        return r


# ---------------------------------------------------------------- where each court publishes its lists

def links(html: str, base: str, pattern: str = r'href=["\']([^"\']+\.pdf)["\']') -> list[str]:
    return [urljoin(base, h) for h in re.findall(pattern, html, re.I)]


def sci(site: Site, day: date) -> list[dict]:
    """Supreme Court: daily lists (M_J_1.pdf...) for the day, else the advance list."""
    page = site.get("https://www.sci.gov.in/cause-list/").text
    all_ = [u for u in links(page, "https://www.sci.gov.in/") if "/jonew/cl/" in u]
    daily = [u for u in all_ if f"/cl/{day.isoformat()}/" in u]
    advance = [u for u in all_ if f"/cl/advance/{day.isoformat()}/" in u]
    return [{"forum": "supreme_court", "court": "Supreme Court", "url": u} for u in (daily or advance)]


def nclat(site: Site, day: date) -> list[dict]:
    page = site.get("https://nclat.nic.in/daily-cause-list").text
    stamp = day.strftime("%d.%m.%Y")
    return [{"forum": "nclat", "court": "NCLAT Chennai" if "_ch_" in u.lower() else "NCLAT New Delhi", "url": u}
            for u in dict.fromkeys(links(page, "https://nclat.nic.in/")) if stamp in u]


def delhi_hc(site: Site, day: date) -> list[dict]:
    page = site.get("https://delhihighcourt.nic.in/web/cause-lists/cause-list").text
    stamps = (day.strftime("%d%m%Y"), day.strftime("%d.%m.%Y"))
    return [{"forum": "high_court", "court": "Delhi High Court", "url": u}
            for u in dict.fromkeys(links(page, "https://delhihighcourt.nic.in/")) if any(s in u for s in stamps)]


def raj_hc(site: Site, day: date) -> list[dict]:
    """Rajasthan High Court, Jaipur and Jodhpur: one PDF per courtroom for the day."""
    out = []
    for bench, path in (("Jaipur", "quick-causelist-jp"), ("Jodhpur", "quick-causelist-jdp")):
        page = f"https://hcraj.nic.in/{path}/"
        table = site.post(page, data={"day": str(day.day), "month": str(day.month), "year": str(day.year)}).text
        for row in re.findall(r"<tr><td>(\d+)</td><td>[^<]*</td><td>(.*?)</td></tr>", table, re.S):
            for p in re.findall(r"data-pdfpath=['\"]([^'\"]+)['\"]", row[1]):
                name = base64.b64decode(p + "==").decode("utf-8", "ignore").rsplit("/", 1)[-1]
                out.append({"forum": "high_court", "court": f"Rajasthan High Court, {bench}, Court {row[0]}", "url": f"{page}?path={quote(p)}",
                            "name": name, "open": (page, {"day": str(day.day), "month": str(day.month), "year": str(day.year)})})
    return out


SOURCES = {"sci": sci, "nclat": nclat, "delhi_hc": delhi_hc, "raj_hc": raj_hc}


# ---------------------------------------------------------------- reading a list

def pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def entries(text: str) -> list[dict]:
    """The list split at its item numbers, each with the court heading it falls under."""
    out, cur, court, last = [], None, "", None
    for line in text.splitlines():
        if re.search(r"\b(court\s*no\.?\s*\d+|chief justice'?s court|court\s+\d+\b)", line, re.I) and len(line) < 80:
            court = line.strip()
        m = ENTRY.match(line)
        n = int(m.group(1)) if m else None
        # The next item in order (or a court's list starting again at 1), naming a case.
        if m and (last is None or n == last + 1 or n == 1) and (CASE_WORD.search(m.group(2)[:60]) or re.match(r"[A-Z][A-Z.()/\s-]{1,30}", m.group(2))):
            cur = {"item": m.group(1), "court": court, "lines": [m.group(2).strip()]}
            out.append(cur)
            last = n
        elif cur is not None and line.strip() and not PAGE_LINE.search(line):
            cur["lines"].append(line.strip())
    return out


def side_in(entry_lines: list[str], names: list[str]) -> str:
    """Which side of "Versus" the company's name is on, as the list prints it."""
    text = "\n".join(entry_lines)
    m = VERSUS.search(text)
    if not m:
        return "unknown"
    before, after = text[:m.start()], text[m.end():]
    on_p, on_r = named(before, names), named(after, names)
    return "respondent" if on_r and not on_p else "petitioner" if on_p and not on_r else "unknown"


def case_number_of(entry_lines: list[str]) -> str:
    head = " ".join(" ".join(entry_lines[:8]).split())
    head = re.split(r"\s(?:v/?s\.?|versus)\s", head, maxsplit=1, flags=re.I)[0]
    found = list(CASE_NO.finditer(head))
    # Not an application in the case (IA, MA, CM) nor the court's internal URN number.
    real = [m for m in found if "urn" not in m.group(1).lower()] or found
    main = next((m for m in real if re.sub(r"[^a-z]", "", m.group(1).lower()).removesuffix("no") not in ("ia", "ma", "cm")),
                real[0] if real else None)
    if not main:
        return ""
    kind = re.sub(r"^(?:.*\bin\s+)", "", " ".join(main.group(1).split()), flags=re.I).strip(" .-")
    return f"{kind}/{main.group(2)}/{main.group(3)}"


def key_of(number: str | None) -> str:
    """'SLP(C) No. 123/2026', 'SLP (C)/123/2026' -> 'slpc|123|2026' for matching a list entry to a case."""
    m = CASE_NO.search(number or "")
    return f"{re.sub(r'[^a-z]', '', m.group(1).lower())}|{int(m.group(2))}|{m.group(3)}" if m else ""


def named(text: str, names: list[str]) -> bool:
    """One of the company's names, word for word, in the text."""
    padded = f" {normalise(text)} "
    return any(f" {normalise(n)} " in padded for n in names if normalise(n))


def snippet(lines: list[str], names: list[str]) -> str:
    """The entry's first lines, and the line naming the company if it comes later (names wrap: two lines at a time)."""
    head = " ".join(" ".join(lines[:6]).split())
    for i in range(len(lines)):
        pair = " ".join(lines[i:i + 2])
        if named(pair, names):
            return head if i < 5 else f"{head} … {' '.join(pair.split())}"
    return head


def hits_in(text: str, companies: dict[str, list[str]]) -> list[dict]:
    out = []
    for e in entries(text):
        flat = normalise(" ".join(e["lines"]))
        for cid, names in companies.items():
            if named(flat, names):
                out.append({"company_id": cid, "item": e["item"], "court_line": e["court"], "case_number": case_number_of(e["lines"]),
                            "parties": snippet(e["lines"], names)[:600], "side": side_in(e["lines"], names),
                            "term": next((n for n in names if named(flat, [n])), names[0])})
    return out


# ---------------------------------------------------------------- the daily run

def watched(db: DB) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for t in db.select("watch_terms", "select=company_id,confirm&active=eq.true"):
        out.setdefault(t["company_id"], []).extend(n for n in t["confirm"] if len(normalise(n)) >= 5)
    return {k: sorted(set(v)) for k, v in out.items() if v}


def court_days(n: int) -> list[date]:
    out, d = [], date.today()
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() != 6:
            out.append(d)
    return out


def read_list(db: DB, site: Site, lst: dict, day: date, companies: dict[str, list[str]], by_number: dict, stats: dict) -> None:
    """One list: read once (a list that failed is tried again), its entries naming a watched company kept."""
    if db.select("cause_lists", f"select=id&forum=eq.{lst['forum']}&list_date=eq.{day}&url=eq.{quote(lst['url'], safe='')}&error=is.null"):
        return
    row = {"forum": lst["forum"], "court": lst["court"], "list_date": day.isoformat(), "url": lst["url"], "read_at": now()}
    try:
        if lst.get("open") and site.opened != lst["open"]:
            site.post(lst["open"][0], data=lst["open"][1])
            site.opened = lst["open"]
        data = site.get(lst["url"]).content
        if len(data) > MAX_PDF or not data.startswith(b"%PDF"):
            raise ValueError("the court site gave a page, not the list (not published there yet); tried again next run")
        text = pdf_text(data)
    except Exception as exc:
        stats["failed"] += 1
        db.insert("cause_lists", {**row, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}, upsert_on="forum,court,list_date,url")
        return
    found_hits = hits_in(text, companies)
    saved = db.insert("cause_lists", {**row, "entries": len(entries(text)), "error": None}, upsert_on="forum,court,list_date,url", returning="id")
    stats["lists"] += 1
    hits = [{"list_id": saved[0]["id"], "forum": lst["forum"],
             "court": f"Supreme Court, {h['court_line'].title().replace(chr(39) + 'S ', chr(39) + 's ')}" if lst["forum"] == "supreme_court" and h["court_line"] else lst["court"],
             "list_date": day.isoformat(), "item": h["item"], "case_number": h["case_number"], "parties": h["parties"],
             "company_id": h["company_id"], "term": h["term"], "case_id": by_number.get((h["company_id"], key_of(h["case_number"])))}
            for h in found_hits]
    if hits:
        db.insert("cause_list_hits", hits, upsert_on="forum,court,list_date,case_number,item,company_id", ignore_duplicates=True)
        stats["hits"] += len(hits)
    print(f"{lst['court']} {day}: {len(found_hits)} entries naming a watched company", flush=True)


def run(db: DB, days: int, only: list[str], alert: bool) -> dict:
    companies = watched(db)
    by_number = {}
    for c in db.select("cases", "select=id,case_number,company_id&status=not.in.(disposed,not_ours)&forum=in.(high_court,supreme_court,nclat)"):
        if key_of(c["case_number"]):
            by_number[(c["company_id"], key_of(c["case_number"]))] = c["id"]
    stats = {"lists": 0, "hits": 0, "failed": 0}
    site = Site()
    for day in court_days(days):
        for name in only:
            try:
                found = SOURCES[name](site, day)
            except Exception as exc:
                print(f"{name} {day}: list page not read ({type(exc).__name__}: {str(exc)[:120]})", flush=True)
                stats["failed"] += 1
                continue
            for lst in found:
                try:
                    read_list(db, site, lst, day, companies, by_number, stats)
                except Exception as exc:  # a dropped connection or a busy database: this list is tried again next run
                    stats["failed"] += 1
                    print(f"{lst['court']} {day}: not read ({type(exc).__name__}: {str(exc)[:120]})", flush=True)
    if alert:
        send_alerts(db)
    return stats


def send_alerts(db: DB) -> int:
    """New hits, one message per company and day, to the legal head's Telegram and email (settings ops_telegram_chat_id, ops_email)."""
    setting = {r["key"]: r["value"] for r in db.select("settings", "select=key,value&key=in.(ops_telegram_chat_id,ops_email)")}
    chat = setting.get("ops_telegram_chat_id")
    to = setting.get("ops_email") if mail.on() else None
    if not chat and not to:
        print("No legal-head Telegram or email set (ops_telegram_chat_id, ops_email): alerts kept, not sent.", flush=True)
        return 0
    rows = db.select("cause_list_hits", "select=id,forum,court,list_date,item,case_number,parties,company:companies(short_name)"
                                        f"&alerted_at=is.null&list_date=gte.{date.today().isoformat()}&order=list_date,court,item")
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["list_date"], (r.get("company") or {}).get("short_name") or ""), []).append(r)
    sent = 0
    for (day, company), items in groups.items():
        when = datetime.fromisoformat(day).strftime("%a %d %b")
        lines = [f"• {i['court']}, item {i['item']}: {i['case_number'] or ''} {i['parties'][:160]}".strip() for i in items[:15]]
        more = f"\n…and {len(items) - 15} more" if len(items) > 15 else ""
        heading = f"{company} in the cause lists for {when}"
        body = "\n".join(html.escape(x) for x in lines) + more + "\n\nAs the courts published them. Check the list before acting."
        done = False
        if chat:
            try:
                done = bool(telegram.send(int(chat), f"<b>{html.escape(heading)}</b>\n{body}"))
            except Exception as exc:
                print("cause-list alert on Telegram failed:", exc, flush=True)
        if to:
            try:
                mail.send(to, f"Haazir: {heading}", mail.card(heading, mail.from_telegram(body)))
                done = True
            except Exception as exc:
                print("cause-list alert by email failed:", exc, flush=True)
        if done:
            db.update("cause_list_hits", f"id=in.({','.join(i['id'] for i in items)})", {"alerted_at": now()})
            sent += 1
    return sent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--days", type=int, default=2, help="court days ahead (Sundays left out)")
    r.add_argument("--only", default=",".join(SOURCES))
    r.add_argument("--no-alert", action="store_true")
    t = sub.add_parser("try")
    t.add_argument("pdf")
    t.add_argument("name")
    args = ap.parse_args()
    if args.cmd == "try":
        text = pdf_text(open(args.pdf, "rb").read())
        print(f"{len(entries(text))} entries")
        for h in hits_in(text, {"x": [args.name]})[:10]:
            print(h)
        return 0
    print(run(DB(), args.days, [s.strip() for s in args.only.split(",") if s.strip() in SOURCES], not args.no_alert))
    return 0


if __name__ == "__main__":
    sys.exit(main())
