"""Lower court decision for High Court appeals: the MACT award or trial court judgment the appeal is against.

    python -m haazir.lower --case <uuid>     # one High Court case
    python -m haazir.lower --batch 20        # open appeals with lower court details and no decision on file yet
    python -m haazir.lower --sample 200      # read-only: how many saved lower court details the reader understands

A High Court case page names the court and case it arose from ("Subordinate Court Information", kept in
cases.lower_court_text, e.g. "MACT NO. 2 - JAIPUR · MACC - 0000202 - 2018 · decided 11-08-2021 · Jaipur Metro.").
That case is looked up on the eCourts district portal: a party name and the registration year are searched in the
court complexes of that place, the result with the same number and year is opened, and its decision (the final
order on the decision date) is saved in the High Court case's file and read by the website's order reader.

Saved documents carry source_ref "lower-court:<CNR> <type> <no>/<year> <date>", so a case is never fetched twice.
Every attempt is logged in activity (action lower_court_decision); a batch leaves a case alone for RETRY_DAYS after
an attempt that found nothing. eCourts' firewall stops the run at once (Blocked), as everywhere else.
"""

import argparse
import asyncio
import difflib
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from bs4 import BeautifulSoup

from .db import DB

MAX_NAMES = 2        # party names tried per case
MAX_SEARCHES = 8     # party-name searches per case (each is one CAPTCHA round trip at one establishment)
MAX_COMPLEXES = 3    # court complexes tried per case
RETRY_DAYS = 14      # a case where nothing was found is tried again after this long
ACTION = "lower_court_decision"
REF = "lower-court:"

# Forums that are not district courts on the eCourts portal.
_NOT_ECOURTS = re.compile(r"high court|board of revenue|revenue court|workm[ae]n|commiss|collector|debt|consumer|rent control|"
                          r"\bdrt\b|\bcat\b|\bnclt\b", re.I)
# Headings of the sections that follow "Subordinate Court Information" on the High Court page; rows saved before
# highcourt.lower_court stopped at them carry them after the district.
_JUNK = re.compile(r"\s*\b(?:IA Details|Case History|Cause List|Sub Matters|Document Details|Orders?|Objection|Category Details|"
                   r"Act Details|FIR Details|Petitioner and Advocate|Respondent and Advocate|Connected|Linked Cases|Main Matters)\b.*$",
                   re.I | re.S)
# "MACC - 0000202 - 2018" (Rajasthan); "2MACT4732019", "2W.P.(C)2583 2021", "2CS DJ501962016" (Delhi: type, number and
# year run together behind a leading 2).
_NUM_DASHED = re.compile(r"^(?P<type>[A-Za-z][^\d]*?)\s*-\s*0*(?P<no>\d+)\s*-\s*(?P<year>(?:19|20)\d{2})$")
_NUM_GLUED = re.compile(r"^2?(?P<type>[A-Za-z][^\d]*?)\s*0*(?P<no>\d{1,7}?)\s*(?P<year>(?:19|20)\d{2})$")
_DATE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")
# Words of a court's name that are not its town.
_COURT_WORDS = re.compile(r"^(?:m\.?a\.?c\.?t\.?|a\.?d\.?j\.?|no\.?|court|judge|additional|addl\.?|district|distt\.?|sessions?|special|spl\.?|"
                          r"c\.?j\.?m\.?|a\.?c\.?j\.?m\.?|civil|sr\.?|div\.?|j\.?m\.?|cum|&|[ivx]+|\d+)$", re.I)
_ACRONYMS = {"mact", "m.a.c.t.", "m.a.c.t", "adj", "adj.", "dj", "cj", "jm", "cjm", "acjm", "acj", "cmm", "acmm", "ec", "ni", "pla",
             "sc/st", "ndps", "pocso", "jd", "sd", "i", "ii", "iii", "iv", "v", "vi", "&"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date(s: str) -> date | None:
    m = _DATE.search(s or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _roman_title(s: str) -> str:
    """'JAIPUR METRO - II' -> 'Jaipur Metro II'; 'MACT NO. 2 - JAIPUR' -> 'MACT No. 2 Jaipur'."""
    words = re.sub(r"\s+-\s+|\s+", " ", s).strip(" .-").split(" ")
    return " ".join(w.upper() if w.lower() in _ACRONYMS else w[:1].upper() + w[1:].lower() for w in words if w)


def parse(text: str) -> dict:
    """cases.lower_court_text -> court_name, case_type, number, year, decided, district, place (blank/None if absent)."""
    parts = [p.strip() for p in (text or "").split(" · ")]
    court = parts[0] if parts and not re.fullmatch(r"[-\s]*", parts[0]) else ""
    out = {"court_name": court, "case_type": "", "number": None, "year": None, "decided": None, "district": "", "place": ""}
    raw_no = parts[1] if len(parts) > 1 else ""
    m = _NUM_DASHED.match(raw_no) or _NUM_GLUED.match(raw_no)
    if m:
        out.update(case_type=re.sub(r"\s+", " ", m.group("type")).strip(" -/"), number=int(m.group("no")), year=int(m.group("year")))
    rest = parts[2:]
    if rest and rest[0].startswith("decided"):
        out["decided"] = _date(rest[0])
        rest = rest[1:]
    district = _JUNK.sub("", " · ".join(rest)).strip(" .·-")
    out["district"] = _roman_title(district) if district else ""
    # The court's town: "MACT - SRIMADHOPUR" (Rajasthan), "ROHINI COURTS, DELHI" (Delhi).
    if " - " in court:
        place = court.rsplit(" - ", 1)[1]
    elif re.search(r"\bcourts?\b", court, re.I) and "," in court:
        place = re.sub(r"\bcourts?\b", " ", court.split(",")[0], flags=re.I)
    else:  # "MACT NASIRABAD", "MACT-Atru", "ADJ-Chirawa"; nothing left of "MACT 1" or "ADJ"
        place = " ".join(w for w in re.split(r"[\s,-]+", court) if w and not _COURT_WORDS.match(w))
    out["place"] = _roman_title(place) if place.strip(" .-") else ""
    return out


def full(p: dict) -> bool:
    return bool(p["court_name"] and p["case_type"] and p["number"] and p["year"] and p["decided"] and (p["district"] or p["place"]))


def unsearchable(p: dict) -> str:
    """Why this lower court cannot be looked up on eCourts ('' when it can)."""
    if not p["court_name"]:
        return "lower court not named on the High Court page"
    if not p["number"] or not p["year"]:
        return "lower case number not readable"
    c = p["court_name"]
    if _NOT_ECOURTS.search(c) or (re.search(r"tribunal", c, re.I) and not re.search(r"claim|accident|\bm\.?\s?a\.?\s?c\.?\s?t\b", c, re.I)):
        return f"lower forum ({_roman_title(c)}) is not a district court on eCourts"
    if not (p["district"] or p["place"]):
        return "lower court's district not given"
    return ""


def label(p: dict, order_date: str | None) -> str:
    """'Lower court decision: MACT No. 2 Jaipur, MACC 202/2018, 11-08-2021' (the website finds these by the prefix)."""
    court = _roman_title(p["court_name"])
    if p["district"] and not p["place"]:
        court += " " + p["district"]
    when = datetime.fromisoformat(order_date).strftime("%d-%m-%Y") if order_date else (p["decided"].strftime("%d-%m-%Y") if p["decided"] else "")
    return f"Lower court decision: {court}, {p['case_type']} {p['number']}/{p['year']}" + (f", {when}" if when else "")


# ---------------------------------------------------------------- which court complexes to search

_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6"}
_GENERIC = {"court", "courts", "complex", "district", "hq", "taluka", "premises", "campus", "the", "of", "and", "at", "no", "comple"}
# Complexes that hold other kinds of courts; searched only when the lower court's name says it is one of them.
_SPECIAL = [r"jjb|juvenile", r"family", r"waqf", r"nagar nigam", r"railway|\brly\b", r"labou?r", r"gram\s*n[a-z]*", r"pocso",
            r"commercial", r"\btest\b", r"\bjda\b"]


def _words(s: str) -> list[str]:
    return [_ROMAN.get(w, w) for w in re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()]


def _core(s: str) -> list[str]:
    return [w for w in _words(s) if w not in _GENERIC]


def _sim(a: str, b: str) -> bool:
    """Same name allowing a spelling slip: one letter changed ('Beawar' / 'Beawer', 'Newai' / 'Niwai') or a close
    spelling ('Jhunjhnu' / 'Jhunjhunu', 'Kotputali' / 'Kotputli'). Short names must match exactly ('Kota' is not 'Kotra')."""
    if a == b:
        return True
    if min(len(a), len(b)) < 5:
        return False
    if len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _found(word: str, words: list[str]) -> bool:
    """word (letters, may be several words run together) appears in words, allowing small spelling differences and
    names written as one word or two ('Sawai Madhopur' / 'Sawaimadhopur', 'Hindon City' / 'Hindauncity')."""
    for size in (1, 2, 3):
        for i in range(len(words) - size + 1):
            if _sim(word, "".join(words[i:i + size])):
                return True
    return False


def _numbers_agree(a: list[str], b: list[str]) -> bool:
    na, nb = {w for w in a if w.isdigit()}, {w for w in b if w.isdigit()}
    return not (na and nb and na != nb)


def _contains(a: list[str], b: list[str]) -> bool:
    """Every word of a in b (a's numbers, if both have numbers, the same)."""
    alpha = [w for w in a if not w.isdigit()]
    if not alpha or not _numbers_agree(a, b):
        return False
    b_alpha = [w for w in b if not w.isdigit()]
    return _found("".join(alpha), b_alpha) or all(_found(w, b_alpha) for w in alpha)


def _same(a: list[str], b: list[str]) -> bool:
    return _contains(a, b) and _contains(b, a)


def rank_complexes(p: dict, complexes: list[dict]) -> list[dict]:
    """Court complexes (court_index rows) most likely to hold the lower case, best first, at most MAX_COMPLEXES."""
    court_l = p["court_name"].lower()
    place, district = _core(p["place"]), _core(p["district"])
    mact = "mact" in re.sub(r"[^a-z]", "", court_l)
    scored = []
    for i, c in enumerate(complexes):
        name = c["complex_name"]
        if any(re.search(k, name, re.I) and not re.search(k, court_l, re.I) for k in _SPECIAL):
            continue
        core, dcore = _core(name), _core(c["district_name"])
        s = 0
        if place and _contains(place, core):
            s += 4
        if district and _same(district, dcore):
            s += 3
        elif district and _contains(district, dcore) and not set(dcore) & {"metro", "rural", "city"}:
            s += 2  # "Nagaur" in "Merta Nagaur"
        if place and _contains(place, dcore):
            s += 1
        if mact and "mact" in re.sub(r"[^a-z]", "", name.lower()):
            s += 2
        if core and _same(core, dcore):
            s += 1  # the district's main complex, where its tribunals and senior courts sit
        if s >= 3:
            scored.append((-s, i, c))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [c for _, _, c in scored[:MAX_COMPLEXES]]


def rank_establishments(p: dict, ests: dict[str, str]) -> list[str]:
    """Establishment codes of a split complex, likeliest first for the lower court ('District and Sessions Judge'
    for a MACT or district judge's suit, 'Civil Judge' for a civil judge's suit, magistrates for criminal cases)."""
    want = set(_words(p["court_name"] + " " + p["case_type"])) - _GENERIC
    kind = re.sub(r"[^a-z]", "", p["case_type"].lower())
    court = re.sub(r"[^a-z]", "", p["court_name"].lower())
    if re.match(r"mac|claim|mv|accident", kind) or "mact" in court or set(_words(p["case_type"])) & {"dj", "adj"}:
        want |= {"district", "sessions", "mact", "claims", "accident"}
    elif re.match(r"cr|ca|ct|ni|bail|sc", kind):
        want |= {"magistrate", "cmm", "acmm", "metropolitan", "sessions"}
    elif re.match(r"cs|civ|suit|misc|om|arb|ex", kind):
        want |= {"civil"}
    want.discard("judge")
    order = sorted(ests.items(), key=lambda kv: -len(want & set(_words(kv[1]))))
    return [k for k, _ in order]


# ---------------------------------------------------------------- party names to search by

_ORG = re.compile(r"\b(ltd|limited|insurance|company|co|bank|corporation|corp|state of|union of india|govt|government|authority|nigam|"
                  r"board|society|trust|m/s|branch|manager|department|director|officer|collector|pvt|private|llp|india|insur\w*)\b", re.I)
_HONORIFIC = re.compile(r"^(?:(?:smt|shrimati|shri\s*mati|shri|sri|sh|mr|mrs|ms|km|ku|kumari|kum|late|dr|master|baby|mst|mohd|md)\.?\s+)+", re.I)
_CUT = re.compile(r"\s+(?:s/o|w/o|d/o|h/o|r/o|son of|wife of|daughter of|husband of|widow of|through|thr|minor|aged|age)\b|[,&(]|"
                  r"\s+and\s+|\s+ors?\b|\s+anr\b", re.I)


def search_name(name: str) -> str:
    """'SMT. SUNITA CHOUDHARY W/O LATE VIJAYPAL SINGH' -> 'SUNITA CHOUDHARY'; '' for companies and the state."""
    cut = _CUT.split(_HONORIFIC.sub("", (name or "").strip()), 1)[0]
    if not cut or _ORG.search(cut):  # 'RENU RAI & ANR (ICICI LOMBARD ...)' is a person; the bracket is cut off first
        return ""
    words = [w for w in re.findall(r"[A-Za-z]+", cut) if len(w) >= 2]
    term = " ".join(words[:2])
    return term if len(term) >= 3 else ""


def party_terms(case: dict) -> list[str]:
    """People named in the High Court case, the side against our company first (in an insurer's appeal the claimants,
    who were the petitioners below)."""
    pets = [p.get("name") or "" for p in case.get("petitioners") or []]
    resps = [p.get("name") or "" for p in case.get("respondents") or []]
    names = resps + pets if case.get("company_role") == "petitioner" else pets + resps
    out = []
    for n in names:
        t = search_name(n)
        if t and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out[:MAX_NAMES]


# ---------------------------------------------------------------- matching and the decision on the case page

def number_of(case_number: str) -> tuple[int, int] | None:
    """'MACT Main/160/2022' -> (160, 2022)."""
    m = re.search(r"0*(\d+)\s*/\s*(\d{4})\s*$", case_number or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def type_fit(found_type: str, p: dict) -> int:
    a, b = re.sub(r"[^a-z]", "", (found_type or "").lower()), re.sub(r"[^a-z]", "", p["case_type"].lower())
    if a and b and (a == b or a.startswith(b) or b.startswith(a)):
        return 2
    motor = re.compile(r"mac|claim|mv|mcop|accident")
    return 1 if motor.search(a) and motor.search(b + re.sub(r"[^a-z]", "", p["court_name"].lower())) else 0


def matches(rows, p: dict) -> list:
    """Search results with the lower case's number and year, best case-type fit first."""
    hits = [r for r in rows if number_of(r.case_number) == (p["number"], p["year"])]
    return sorted(hits, key=lambda r: -type_fit(r.case_type, p))


def same_case(d, p: dict) -> str:
    """'' when the case page is the lower case, else why not (number, year or decision date differs)."""
    nums = [number_of(x) for x in (d.registration_number, d.filing_number) if x]
    if nums and (p["number"], p["year"]) not in nums:
        return f"number on eCourts is {d.registration_number or d.filing_number}"
    if d.decision_date and p["decided"] and d.decision_date != p["decided"]:
        return f"decided {d.decision_date:%d-%m-%Y} on eCourts, {p['decided']:%d-%m-%Y} per the High Court"
    return ""


_DISPLAY_PDF = re.compile(r"displayPdf\(([^)]*)\)")


def page_orders(html: str) -> list[dict]:
    """Order links on a district case page: date (ISO), displayPdf arguments, and whether the link is under
    'Final Orders / Judgements' (the links' own labels say 'Interim Orders' in both tables)."""
    soup = BeautifulSoup(html or "", "lxml")
    out = []
    for a in soup.find_all(attrs={"onclick": _DISPLAY_PDF}):
        args = [x.strip().strip("'\"") for x in _DISPLAY_PDF.search(a["onclick"]).group(1).split(",")]
        row = a.find_parent("tr")
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")] if row else []
        d = next((x for c in cells if (x := _date(c))), None)
        head = a.find_previous("h3")
        heading = head.get_text(" ", strip=True).lower() if head else ""
        out.append({"date": d.isoformat() if d else None, "args": args, "final": "final" in heading or "judg" in heading})
    return out


def pick_decision(orders: list[dict], decided: date | None) -> dict | None:
    """The decision: the order of the decision date (a final one first); else the final order nearest that date;
    else the last final order; else an interim-table order within 3 days of the decision date. None if none of these."""
    usable = [o for o in orders if len(o["args"]) >= 4 and o["date"]]
    finals = [o for o in usable if o["final"]]
    if decided:
        exact = [o for o in usable if o["date"] == decided.isoformat()]
        if exact:
            return sorted(exact, key=lambda o: not o["final"])[0]
        days = lambda o: abs((date.fromisoformat(o["date"]) - decided).days)
        if finals:
            near = min(finals, key=days)
            return near if days(near) <= 60 else max(finals, key=lambda o: o["date"])
        close = [o for o in usable if days(o) <= 3]
        return min(close, key=days) if close else None
    return max(finals, key=lambda o: o["date"]) if finals else None


# ---------------------------------------------------------------- one High Court case

def stored(db: DB, case_id: str) -> bool:
    return bool(db.select("documents", f"select=id&case_id=eq.{case_id}&source_ref=like.{quote(REF)}*"))


class Complexes:
    """A state's district court complexes (court_index), listed on eCourts if the state was never listed."""

    def __init__(self, db: DB) -> None:
        self.db, self.by_state = db, {}

    async def of(self, ec, state: str) -> list[dict]:
        if state not in self.by_state:
            rows = self.db.select("court_index", f"select=*&state_name=eq.{quote(state)}&order=state_code,dist_code,complex_code")
            if not rows:
                from .lookup import list_state
                codes = {v.strip().lower(): k for k, v in (await ec.client.list_states()).items()}
                if state.lower() in codes:
                    await list_state(self.db, ec, codes[state.lower()], state, None)
                    rows = self.db.select("court_index", f"select=*&state_name=eq.{quote(state)}&order=state_code,dist_code,complex_code")
            self.by_state[state] = rows
        return self.by_state[state]


async def targets(ec, p: dict, ranked: list[dict]):
    """(court dict for ECourts.search, establishment code) pairs to search, likeliest first. A split complex's
    establishments are listed (no CAPTCHA) only when the search gets to that complex."""
    from .ecourts import Blocked
    for c in ranked:
        court = {k: c[k] for k in ("state_code", "dist_code", "complex_code", "est_codes", "needs_est")}
        if not c["needs_est"]:
            yield court, ""
            continue
        ests = [e for e in c["est_codes"].split(",") if e]
        try:
            named = await ec.client.list_establishments(c["state_code"], c["dist_code"], c["complex_code"])
            ests = [e for e in rank_establishments(p, named) if e in ests] + [e for e in ests if e not in named]
        except Exception as exc:  # unnamed establishments are searched in the portal's order
            if _blocked(exc):
                raise Blocked() from exc
        for e in ests:
            yield court, e


def _blocked(exc: BaseException) -> bool:
    from .ecourts import Blocked, _is_block
    return isinstance(exc, Blocked) or _is_block(exc)


async def fetch(ec, db: DB, case: dict, complexes: Complexes) -> tuple[str, str]:
    """('found' | 'not found' | 'skipped', what happened) for one High Court case. Raises Blocked."""
    from . import orders
    from .ecourts import Blocked
    from .highcourt import link_lower

    p = parse(case.get("lower_court_text") or "")
    why = unsearchable(p)
    if why:
        return "skipped", why
    if stored(db, case["id"]):
        return "skipped", "lower court decision already in the case file"

    # A lower case Haazir already has needs no search.
    cnrs: list[str] = []
    known = case.get("lower_case_id")
    if not known:
        try:
            known = link_lower(db, {"no": p["number"], "year": p["year"], "district": p["district"] or p["place"]})
        except (TypeError, KeyError):  # a district case without its court row
            known = None
    if known:
        cnrs = [r["cnr"] for r in db.select("cases", f"select=cnr&id=eq.{known}") if r.get("cnr")]
    searches = errors = 0
    if not cnrs:
        terms = party_terms(case)
        if not terms:
            return "not found", "no party name to search by"
        state = ((case.get("court") or {}).get("state")) or ""
        ranked = rank_complexes(p, await complexes.of(ec, state))
        if not ranked:
            return "not found", f"no court complex in {state or 'the state'} matches {p['place'] or p['district']}"
        async for court, est in targets(ec, p, ranked):
            if searches >= MAX_SEARCHES or cnrs:
                break
            for term in terms:
                if searches >= MAX_SEARCHES or cnrs:
                    break
                searches += 1
                try:
                    rows = await ec.search({**court, "est_codes": est, "needs_est": bool(est)}, term, p["year"], "Both")
                except Blocked:
                    raise
                except Exception as exc:  # LayoutError, CAPTCHA failures, timeouts: try the next court
                    if _blocked(exc):
                        raise Blocked() from exc
                    print(f"    search {term!r} at {court['complex_code']}/{est or '-'} failed: {type(exc).__name__}", flush=True)
                    errors += 1
                    continue
                cnrs = [r.cnr_number for r in matches(rows, p)][:2]
        if not cnrs and errors == searches:
            return "failed", f"all {searches} searches failed (CAPTCHA or portal errors); tried again next run"
        if not cnrs:
            return "not found", f"no {p['case_type']} {p['number']}/{p['year']} in {searches} searches ({', '.join(terms)})"

    reasons = []
    for cnr in cnrs:
        try:
            d = await ec.detail(cnr)
        except Blocked:
            raise
        except Exception as exc:
            if _blocked(exc):
                raise Blocked() from exc
            reasons.append(f"{cnr} page failed: {type(exc).__name__}")
            continue
        differs = same_case(d, p)
        if differs:
            reasons.append(f"{cnr}: {differs}")
            continue
        o = pick_decision(page_orders(ec.last_html), p["decided"])
        if not o:
            return "not found", f"{cnr} found, but no final order is uploaded on its eCourts page"
        try:
            pdf = await orders.district_pdf(ec, o["args"])
        except Exception as exc:
            if _blocked(exc):
                raise Blocked() from exc
            return "not found", f"{cnr} order PDF failed: {type(exc).__name__}"
        title = label(p, o["date"])
        ref = f"{REF}{cnr} {p['case_type']} {p['number']}/{p['year']} {o['date']}"
        # Kept for drafting only (the writer reads the PDF itself): not read as the appeal's own order, so the award's
        # date, "decided" and payment directions never become the High Court case's hearings or deadlines.
        doc = orders.store_order(db, case["id"], pdf, o["date"], title, ref[:200], kind="lower_decision")
        if not doc:
            body = pdf[3:] if pdf[:3] == b"\xef\xbb\xbf" else pdf
            return "not found", f"{cnr}: " + ("that PDF is already in the case file" if body[:4] == b"%PDF" else "the court sent no PDF")
        return "found", f"{title} ({cnr})"
    return "not found", "; ".join(reasons) or "no match"


# ---------------------------------------------------------------- batch

def batch_cases(db: DB, n: int, today: date | None = None) -> list[dict]:
    """Open High Court cases with lower court details, no decision on file and no recent attempt; nearest upcoming
    hearing first, then the latest past hearings, then cases without a date."""
    rows = db.select("cases", "select=id,cnr,case_number,lower_court_text,lower_case_id,petitioners,respondents,company_role,"
                              "next_hearing_date,court:courts(state)&forum=eq.high_court&lower_court_text=not.is.null"
                              "&status=not.in.(disposed,not_ours)")
    have = {r["case_id"] for r in db.select("documents", f"select=case_id&source_ref=like.{quote(REF)}*")}
    since = (datetime.now(timezone.utc) - timedelta(days=RETRY_DAYS)).isoformat()
    tried = {r["case_id"] for r in db.select("activity", f"select=case_id&action=eq.{ACTION}&created_at=gt.{quote(since)}")}
    rows = [r for r in rows if r["id"] not in have and r["id"] not in tried and not unsearchable(parse(r["lower_court_text"]))]
    return order_for_batch(rows, today or date.today())[:n]


def order_for_batch(rows: list[dict], today: date) -> list[dict]:
    t = today.isoformat()
    upcoming = sorted((r for r in rows if (r.get("next_hearing_date") or "") >= t), key=lambda r: r["next_hearing_date"])
    past = sorted((r for r in rows if r.get("next_hearing_date") and r["next_hearing_date"] < t), key=lambda r: r["next_hearing_date"], reverse=True)
    return upcoming + past + [r for r in rows if not r.get("next_hearing_date")]


async def run(db: DB, cases: list[dict], minutes: float) -> dict:
    from .ecourts import Blocked, ECourts
    stats = {"found": 0, "not found": 0, "skipped": 0, "failed": 0, "blocked": 0}
    deadline = time.time() + minutes * 60
    complexes = Complexes(db)
    async with ECourts() as ec:
        for c in cases:
            if time.time() > deadline:
                print("time is up; the rest wait for the next run", flush=True)
                break
            name = c.get("case_number") or c["id"]
            try:
                result, what = await fetch(ec, db, c, complexes)
            except Blocked:
                stats["blocked"] += 1
                print(f"{name}: blocked (eCourts refused; stopping)", flush=True)
                break
            except Exception as exc:
                result, what = "failed", f"{type(exc).__name__}: {str(exc)[:120]}"
            stats[result] += 1
            print(f"{name}: {result}" + (f" ({what})" if result != "found" else f": {what}"), flush=True)
            if result in ("found", "not found"):  # a failed attempt is tried again next run
                db.log(c["id"], ACTION, {"result": result, "detail": what[:300]})
    return stats


def sample(db: DB, n: int) -> int:
    """Read-only: how many saved lower court details parse fully, and the ones that do not."""
    rows = db.select("cases", f"select=id,lower_court_text&forum=eq.high_court&lower_court_text=not.is.null&limit={n}")
    ok = [r for r in rows if full(parse(r["lower_court_text"]))]
    for r in rows:
        p = parse(r["lower_court_text"])
        if not full(p):
            print(f"  partial: {r['lower_court_text'][:90]!r} -> {unsearchable(p) or 'searchable'}")
    searchable = sum(not unsearchable(parse(r["lower_court_text"])) for r in rows)
    print(f"{len(ok)} of {len(rows)} parse fully; {searchable} can be looked up on eCourts")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--case", help="one High Court case id")
    g.add_argument("--batch", type=int, help="this many cases")
    g.add_argument("--sample", type=int, help="read-only parser coverage on this many saved rows")
    ap.add_argument("--minutes", type=float, default=50, help="stop taking new cases after this long")
    args = ap.parse_args()
    db = DB()
    if args.sample:
        return sample(db, args.sample)
    if args.case:
        cases = db.select("cases", f"select=id,cnr,case_number,lower_court_text,lower_case_id,petitioners,respondents,company_role,"
                                   f"next_hearing_date,court:courts(state)&id=eq.{args.case}&forum=eq.high_court")
        if not cases:
            print(f"{args.case}: not found (no High Court case with this id)")
            return 1
    else:
        cases = batch_cases(db, args.batch)
        print(f"{len(cases)} High Court cases to look up", flush=True)
    stats = asyncio.run(run(db, cases, args.minutes))
    print(f"lower court decisions: {stats}")
    return 1 if stats["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
