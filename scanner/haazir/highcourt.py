"""Watch High Court benches (hcservices.ecourts.gov.in) for cases naming our companies.

Same approach as district courts: a party-name search per bench, search name and year finds the cases;
the first search records what is already on file; each case page is read for side, dates and the
"Subordinate Court Information" that links an appeal to the district case it arose from.

    python -m haazir.highcourt add --court rajasthan --benches Jaipur --zone "Jaipur Zone"
    python -m haazir.highcourt scan --detail-budget 60
"""

import argparse
import asyncio
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup
from bharat_courts.casedetail import parse_case_detail
from bharat_courts.config import BharatCourtsConfig
from bharat_courts.courts import get_court
from bharat_courts.hcservices import endpoints
from bharat_courts.hcservices.client import HCServicesClient
from bharat_courts.hcservices.parser import parse_case_status

from . import config, relay
from .captcha import MultiReadSolver
from .classify import categorize
from .db import DB
from .ecourts import CNR_RE
from .matching import is_company, role, title_of
from .scan import disposal_of, refresh_suggestions
from . import orders as court_orders
from . import places

# Stay applications are filed inside the main appeal and get their own number; the main case is enough.
SKIP_TYPES = {"CSTAY"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso(d) -> str | None:
    return d.isoformat() if d else None


def lower_court(html: str) -> dict | None:
    """'Subordinate Court Information': the court and case an appeal or writ arose from."""
    text = re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" "))
    i = text.find("Subordinate Court Information")
    if i < 0:
        return None
    t = text[i:i + 600]
    grab = lambda label, stop: (m.group(1).strip() if (m := re.search(re.escape(label) + r"\s*:\s*(.*?)\s*(?:" + stop + r"|$)", t)) else "")
    court = grab("Court Number and Name", "Case Number and Year")
    number = grab("Case Number and Year", "Case Decision Date")
    decided = grab("Case Decision Date", "State")
    # The block is followed by other sections of the case page; stop at any of their headings.
    district = grab("District", "Sub Matters|Document Details|Order|Objection|IA Details|Ia Details|Case History|Cause List|"
                                "Category Details|Act Details|FIR Details|Petitioner and Advocate|Respondent and Advocate|Connected|"
                                "Linked Cases|Main Matters")
    m = re.search(r"(\d+)\s*-\s*(\d{4})\s*$", number)
    return {"court": court, "number": number, "decided": decided, "district": district,
            "no": int(m.group(1)) if m else None, "year": int(m.group(2)) if m else None}


def link_lower(db: DB, lc: dict) -> str | None:
    """Our district case matching the lower court's number, year and district, if exactly one."""
    if not lc or not lc["no"]:
        return None
    rows = db.select("cases", f"select=id,case_number,court:courts(district_name,complex_name)&forum=eq.district"
                              f"&case_number=like.*/{lc['no']}/{lc['year']}")
    words = [w for w in re.split(r"\W+", lc["district"].lower()) if len(w) > 3]
    rows = [r for r in rows if not words or any(w in (r["court"]["district_name"] + " " + r["court"]["complex_name"]).lower() for w in words)]
    return rows[0]["id"] if len(rows) == 1 else None


class HighCourt:
    def __init__(self) -> None:
        self.client = HCServicesClient(config=BharatCourtsConfig(request_delay=config.REQUEST_DELAY, max_retries=2),
                                       captcha_solver=MultiReadSolver())
        relay.attach(self.client._http)  # through the Mumbai relay when ECOURTS_RELAY is set (GitHub); else direct

    async def __aenter__(self):
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *a):
        await self.client.__aexit__(*a)

    async def search(self, court_code: str, bench: str, term: str, year: int):
        court = get_court(court_code)
        resp = await self.client._post_with_captcha_retry(endpoints.SHOW_RECORDS_URL, lambda cap: endpoints.case_status_by_party_form(
            state_code=court.state_code, court_code=bench, petres_name=term, rgyear=str(year), captcha=cap, status_filter="Pending"))
        rows = parse_case_status(resp.text)
        bad = [r for r in rows if not CNR_RE.match(r.cnr_number or "")]
        if bad:
            raise RuntimeError(f"High Court results not read cleanly: {len(bad)} of {len(rows)} rows without a CNR")
        return rows

    async def search_advocate(self, court_code: str, bench: str, name: str):
        """An advocate's pending cases at one High Court bench (no year)."""
        court = get_court(court_code)
        resp = await self.client._post_with_captcha_retry(endpoints.SHOW_RECORDS_URL, lambda cap: endpoints.case_status_by_advocate_form(
            state_code=court.state_code, court_code=bench, captcha=cap, advocate_name=name, status_filter="Pending"))
        rows = parse_case_status(resp.text)
        bad = [r for r in rows if not CNR_RE.match(r.cnr_number or "")]
        if bad:
            raise RuntimeError(f"High Court results not read cleanly: {len(bad)} of {len(rows)} rows without a CNR")
        return rows

    async def detail(self, cnr: str):
        resp = await self.client._post_with_captcha_retry(endpoints.INDEX_QRY_URL, lambda cap: endpoints.case_status_by_cnr_form(cnr=cnr, captcha=cap))
        return parse_case_detail(resp.text, cnr=cnr, base_url=endpoints.BASE_URL), resp.text


async def scan(db: DB, budget: int, year: int, company: str = "", city: str = "") -> dict:
    stats = {"searches": 0, "found": 0, "new_to_us": 0, "details": 0, "linked": 0, "failures": 0}
    courts = db.select("courts", "select=*&forum=eq.high_court&active=eq.true")
    terms = db.select("watch_terms", "select=*&active=eq.true")
    if city:
        courts = [c for c in courts if (c.get("city") or "").lower() == city.lower()]
    if company:
        ids = {c["id"] for c in db.select("companies", "select=id,short_name,name") if company.lower() in (c["short_name"].lower(), c["name"].lower())}
        terms = [t for t in terms if t["company_id"] in ids]
    confirm: dict[str, list[str]] = {}
    for t in db.select("watch_terms", "select=company_id,confirm"):
        confirm.setdefault(t["company_id"], []).extend(t["confirm"])
    coverage = {(r["court_id"], r["company_id"], r["year"], r["term"]) for r in db.select("scan_coverage")}
    known = {r["cnr"] for r in db.select("cases", "select=cnr")}
    new_orders: list[str] = []
    async with HighCourt() as hc:
        for court in courts:
            for t in terms:
                if t.get("court_ids") and court["id"] not in t["court_ids"]:
                    continue  # this name is watched in other courts
                key = (court["id"], t["company_id"], year, t["term"])
                baseline = key not in coverage
                stats["searches"] += 1
                try:
                    rows = await hc.search(court["state_code"], court["complex_code"], t["term"], year)
                except Exception as exc:
                    stats["failures"] += 1
                    print(f"  search failed {court['complex_name']} {t['term']}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
                    continue
                rows = [r for r in rows if r.case_type not in SKIP_TYPES]
                new = []
                for r in rows:
                    if r.cnr_number in known:
                        continue
                    known.add(r.cnr_number)
                    new.append({"cnr": r.cnr_number, "forum": "high_court", "company_id": t["company_id"], "court_id": court["id"],
                                "case_type": r.case_type or None, "case_number": f"{r.case_type}/{r.case_number}" if r.case_type else r.case_number,
                                "title": title_of(r.petitioner, r.respondent), "company_role": "unknown",
                                "petitioners": [{"name": r.petitioner, "advocate": ""}] if r.petitioner else [],
                                "respondents": [{"name": r.respondent, "advocate": ""}] if r.respondent else [],
                                "category": categorize(r.case_type), "is_baseline": baseline,
                                "raw_search": {"petitioner": r.petitioner, "respondent": r.respondent}})
                if new:
                    for i in range(0, len(new), 500):
                        db.insert("cases", new[i:i + 500], upsert_on="cnr", ignore_duplicates=True)
                stats["found"] += len(rows)
                stats["new_to_us"] += len(new)
                db.insert("scan_coverage", {"court_id": court["id"], "company_id": t["company_id"], "year": year, "term": t["term"],
                                            "last_scanned_at": now(), "last_count": len(rows)}, upsert_on="court_id,company_id,year,term")
                print(f"  {court['complex_name']} · {t['term']} · {year}: {len(rows)} cases, {len(new)} new to us", flush=True)

            # Case pages: never read (newest first), then passed hearings.
            cols = "select=id,cnr,company_id,raw_search,next_hearing_date,detail_fetched_at,company_role"
            fresh = db.select("cases", f"{cols}&forum=eq.high_court&court_id=eq.{court['id']}&detail_fetched_at=is.null")
            fresh.sort(key=lambda c: c["cnr"][6:12], reverse=True)
            today = date.today()
            passed = db.select("cases", f"{cols}&forum=eq.high_court&court_id=eq.{court['id']}&status=not.in.(disposed,not_ours)"
                                        f"&next_hearing_date=lt.{today.isoformat()}&next_hearing_date=gte.{(today - timedelta(days=60)).isoformat()}")
            passed = [c for c in passed if c["detail_fetched_at"] and c["detail_fetched_at"][:10] <= c["next_hearing_date"]]
            for c in (fresh + passed)[:budget]:
                try:
                    d, html = await hc.detail(c["cnr"])
                except Exception as exc:
                    db.update("cases", f"id=eq.{c['id']}", {"detail_error": f"{type(exc).__name__}: {str(exc)[:200]}"})
                    continue
                values, docs = await save_case(hc, db, c, d, html, confirm.get(c["company_id"], []))
                if values is None:
                    continue
                stats["linked"] += "lower_case_id" in values
                new_orders += docs
                stats["details"] += 1
    stats["orders"] = len(new_orders)
    stats["orders_read"] = court_orders.read_with_ai(new_orders)
    refresh_suggestions(db)
    court_orders.analyse_orders()
    court_orders.refresh_deadlines()
    return stats


async def save_case(hc: HighCourt, db: DB, c: dict, d, html: str, names: list[str]) -> tuple[dict | None, list[str]]:
    """Write one High Court case page (from hc.detail) to the case row: dates, coram, parties and their advocates,
    side, decided, the court below; its hearings; and its order PDFs. c needs id and cnr. Returns the values written
    (None for the empty page the portal sometimes sends: read again next run) and the new order documents.
    Shared by the High Court scan and the nightly coming-week check (haazir.week)."""
    if not d.petitioners and not d.respondents and not d.filing_date and not d.registration_date:
        # The portal sometimes answers with an empty page; read it again next run.
        db.update("cases", f"id=eq.{c['id']}", {"detail_error": "empty case page", "updated_at": now()})
        return None, []
    pets = [{"name": p.name, "advocate": p.advocate} for p in d.petitioners]
    resps = [{"name": p.name, "advocate": p.advocate} for p in d.respondents]
    side = role([p["name"] for p in pets], [p["name"] for p in resps], names)
    acts = [{"act": a.act, "sections": a.sections} for a in d.acts]
    lead_pet = next((p["name"] for p in pets if is_company(p["name"], names)), pets[0]["name"] if pets else "") if side == "petitioner" else (pets[0]["name"] if pets else "")
    lead_resp = next((p["name"] for p in resps if is_company(p["name"], names)), resps[0]["name"] if resps else "") if side != "petitioner" else (resps[0]["name"] if resps else "")
    lc = lower_court(html)
    values = {
        "case_type": d.case_type or None, "filing_number": d.filing_number or None, "filing_date": iso(d.filing_date),
        "registration_number": d.registration_number or None, "registration_date": iso(d.registration_date),
        "first_hearing_date": iso(d.first_hearing_date), "next_hearing_date": iso(d.next_hearing_date),
        "decision_date": iso(d.decision_date), "stage": d.case_stage or None,
        "court_judge": d.coram or d.court_number_and_judge or None, "petitioners": pets, "respondents": resps,
        "petitioner_advocate": (pets[0]["advocate"] or None) if pets else None, "acts": acts,
        "title": title_of(lead_pet, lead_resp),
        "category": "motor_accident" if lc and "mact" in lc["court"].lower() else categorize(d.case_type, " ".join(a["act"] for a in acts)),
        "detail_fetched_at": now(), "detail_error": None, "updated_at": now(),
    }
    if side != "unknown":
        values["company_role"], values["role_source"] = side, "case_page"
    elif pets or resps:
        values["status"] = "not_ours"
    if d.decision_date:
        values["status"] = "disposed"
        values["disposal"] = disposal_of(html)
    if lc:
        values["lower_court_text"] = f"{lc['court']} · {lc['number']}" + (f" · decided {lc['decided']}" if lc["decided"] else "") + (f" · {lc['district'].title()}" if lc["district"] else "")
        linked = link_lower(db, lc)
        if linked:
            values["lower_case_id"] = linked
    db.update("cases", f"id=eq.{c['id']}", values)
    new_orders: list[str] = []
    if side != "unknown" and d.orders:
        new_orders = await court_orders.high_court_orders(hc, db, c["id"], d.orders)
    hearings = [{"case_id": c["id"], "hearing_date": iso(h.hearing_date), "business_date": iso(h.business_date),
                 "purpose": h.purpose or None, "judge": h.judge or None} for h in d.history if h.hearing_date]
    if hearings:
        db.insert("hearings", hearings, upsert_on="case_id,hearing_date,business_date", ignore_duplicates=True)
    return values, new_orders


async def add(db: DB, court_code: str, benches: list[str], zone_name: str) -> None:
    court = get_court(court_code)
    zone = db.insert("zones", {"name": zone_name, "city": zone_name.replace(" Zone", "")}, upsert_on="name", returning="*")[0]
    async with HighCourt() as hc:
        options = await hc.client.list_benches(court)
    for b in benches:
        code = next((k for k, v in options.items() if b.lower() in v.lower()), None)
        if not code:
            raise SystemExit(f"bench {b!r} not found. Options: {sorted(options.values())}")
        db.insert("courts", {"state_code": court_code, "dist_code": "hc", "complex_code": code, "est_codes": "", "needs_est": False,
                             "forum": "high_court", "district_name": "High Court", "complex_name": f"{court.name}, {options[code]}",
                             "zone_id": zone["id"], "active": True}, upsert_on="state_code,dist_code,complex_code")
        print(f"  added {court.name}, {options[code]}")
    places.fill(db)
    places.add_to_terms(db, [c["id"] for c in db.select("courts", f"select=id&forum=eq.high_court&state_code=eq.{court_code}")])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--court", required=True, help="library court code, e.g. rajasthan, delhi, bombay")
    a.add_argument("--benches", nargs="+", required=True)
    a.add_argument("--zone", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--detail-budget", type=int, default=60)
    s.add_argument("--year", type=int, default=date.today().year)
    s.add_argument("--company", default="", help="only this company (Scan now)")
    s.add_argument("--city", default="", help="only benches in this city (Scan now)")
    args = ap.parse_args()
    db = DB()
    if args.cmd == "add":
        asyncio.run(add(db, args.court, args.benches, args.zone))
        return 0
    run = db.insert("scan_runs", {"trigger": "high_court"}, returning="*")[0]
    t0 = time.time()
    try:
        stats, status = asyncio.run(scan(db, args.detail_budget, args.year, args.company, args.city)), "ok"
    except Exception as exc:
        stats, status = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, "failed"
    if status == "ok" and stats["failures"]:
        status = "partial"
    db.update("scan_runs", f"id=eq.{run['id']}", {"status": status, "finished_at": now(), "searches": stats.get("searches", 0),
                                                  "search_failures": stats.get("failures", 0), "results": stats.get("found", 0),
                                                  "details_fetched": stats.get("details", 0),
                                                  "log": [{"at": now(), "level": "info" if status == "ok" else "error", "msg": f"high court: {stats}"}]
                                                         + ([{"at": now(), "level": "info", "msg": relay.summary()}] if relay.summary() else [])})
    print(f"high court scan: {status} {stats} in {time.time() - t0:.0f}s")
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
