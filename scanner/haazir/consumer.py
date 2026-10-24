"""Watch consumer commissions on e-Jagriti for complaints naming our companies.

e-Jagriti's party-name search does not return recent filings, but its public case-status lookup does,
by case number, without a login or CAPTCHA. Commissions number complaints in order
(DC/102/CC/586/2026, 587, ...), so each run reads the numbers after the last one read, checks the
complainant and respondent against the watched companies, and saves the matches with their hearing dates.

    python -m haazir.consumer add --state Rajasthan --districts Jaipur-I Jaipur-II Jaipur-III Jaipur-IV \
        --state-commission --zone "Jaipur Zone"
    python -m haazir.consumer scan                 # new numbers + re-read cases whose hearing has passed
    python -m haazir.consumer scan --backfill 60   # first run: also read the latest 60 complaints as on file

Limits: case status gives the lead complainant and lead respondent only. Insurance complaints name the
insurer first; a company named only as a later respondent can be missed.
"""

import argparse
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from .classify import categorize  # noqa: F401  (kept for parity with district cases)
from . import places
from .db import DB
from .matching import is_company, title_of
from .scan import refresh_suggestions

BASE = "https://e-jagriti.gov.in/services"
HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://e-jagriti.gov.in",
           "Referer": "https://e-jagriti.gov.in/case-history-case-status"}
GAP = 5            # this many missing numbers in a row means we reached the latest complaint
RETRY_DAYS = 7     # numbers skipped inside the range are retried this long (some are numbered late)
SERIES_KIND = {"CC": "Consumer Complaint", "A": "First Appeal", "FA": "First Appeal", "EA": "Execution Application",
               "RP": "Revision Petition", "MA": "Miscellaneous Application", "IA": "Interlocutory Application"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EJagriti:
    """One request at a time with a pause; the server drops connections under load."""

    def __init__(self, pause: float = 2.5) -> None:
        self.http = httpx.Client(base_url=BASE, headers=HEADERS, timeout=60)
        self.pause = pause
        self.requests = 0

    def _call(self, method: str, path: str, **kw) -> dict:
        for attempt in range(3):
            time.sleep(self.pause if self.requests else 0)
            self.requests += 1
            try:
                r = self.http.request(method, path, **kw)
                return {"status": r.status_code, **(r.json() if r.content else {})}
            except (httpx.TransportError, ValueError):
                time.sleep(10 * (attempt + 1))
        raise RuntimeError(f"e-Jagriti did not answer {path} after 3 tries")

    def state_commissions(self) -> list[dict]:
        return self._call("GET", "/report/report/getStateCommissionAndCircuitBench").get("data") or []

    def district_commissions(self, state_id: int) -> list[dict]:
        return self._call("GET", "/report/report/getDistrictCommissionByCommissionId",
                          params={"commissionId": state_id}).get("data") or []

    def case(self, number: str, commission_id: int) -> dict | None:
        r = self._call("GET", "/case/caseFilingService/v2/getCaseStatus",
                       params={"caseNumber": number, "commissionId": commission_id})
        return r.get("data") if r["status"] == 200 and r.get("data") else None

    def history(self, number: str) -> list[dict]:
        r = self._call("GET", "/case/caseFilingService/v2/getCaseDetailForHistoryDashBoard", params={"caseNumber": number})
        return r.get("data") or [] if r["status"] == 200 else []

    def example_numbers(self, commission_id: int) -> list[str]:
        """Case numbers this commission has used, from the (old-cases-only) free-text search."""
        found: list[str] = []
        for since, until in (("2024-03-01", "2024-03-31"), ("2025-03-01", "2025-03-31"), ("2024-08-01", "2024-08-31")):
            for text in ("INSURANCE", "BANK", "LIMITED"):
                body = {"commissionId": commission_id, "page": 0, "size": 30, "fromDate": since, "toDate": until,
                        "dateRequestType": 1, "serchType": 8, "serchTypeValue": text, "orderType": 2}
                found += [d["caseNumber"] for d in self._call("POST", "/case/caseFilingService/v2/getCaseDetailsBySearchType",
                                                               json=body).get("data") or []]
                if len(found) >= 5:
                    return found
        return found


def series_of(numbers: list[str], kinds: tuple[str, ...]) -> list[str]:
    """'DC/AB1/102/CC/121/2024' -> 'DC/AB1/102/CC', keeping only the case kinds we watch."""
    out = []
    for n in numbers:
        parts = n.split("/")
        if len(parts) >= 4 and parts[-3] in kinds:
            s = "/".join(parts[:-2])
            if s not in out:
                out.append(s)
    return out


def latest_number(ej: EJagriti, series: str, year: int, commission_id: int) -> int:
    """Highest complaint number in use. Numbers have gaps, so 'exists' means any of n..n+GAP-1 exists."""
    def exists(n: int) -> bool:
        return any(ej.case(f"{series}/{k}/{year}", commission_id) for k in range(n, n + GAP))
    lo, hi = 0, 64
    while exists(hi):
        lo, hi = hi, hi * 2
        if hi > 20000:
            break
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if exists(mid):
            lo = mid
        else:
            hi = mid
    # lo is the start of a window holding the last number; find it exactly.
    last = lo
    for k in range(lo, lo + GAP):
        if k and ej.case(f"{series}/{k}/{year}", commission_id):
            last = k
    return last


def companies(db: DB) -> list[tuple[str, list[str]]]:
    confirm: dict[str, list[str]] = {}
    for t in db.select("watch_terms", "select=company_id,confirm"):
        confirm.setdefault(t["company_id"], []).extend(t["confirm"])
    return list(confirm.items())


def _date(v: str | None) -> str | None:
    return v[:10] if v else None


def advocates(xs) -> str:
    """Advocate names, whether e-Jagriti sends plain names or records."""
    if not isinstance(xs, list):
        return ""
    names = [x if isinstance(x, str) else (x.get("advocateName") or x.get("advocateNameEn") or "") for x in xs]
    return ", ".join(n.strip() for n in names if n and n.strip())


def case_values(rec: dict, hist: list[dict], court: dict, comps: list[tuple[str, list[str]]]) -> dict | None:
    """A case row from a case-status record, or None if no watched company is a party."""
    complainant = (rec.get("complainant") or "").strip()
    respondent = (rec.get("respondent") or "").strip()
    company, side = None, None
    for cid, names in comps:
        if is_company(respondent, names):
            company, side = cid, "respondent"
            break
        if is_company(complainant, names):
            company, side = cid, "petitioner"
    if not company:
        return None
    number = rec["caseNumber"]
    kind = number.split("/")[-3]
    hearings = sorted(h["dateOfNextHearing"] for h in hist if h.get("dateOfNextHearing"))
    stage = rec.get("caseStage") or ""
    values = {
        "cnr": f"EJ{rec['fillingReferenceNumber']}",
        "forum": "consumer",
        "company_id": company,
        "court_id": court["id"],
        "case_type": SERIES_KIND.get(kind, kind),
        "case_number": number,
        "filing_number": str(rec["fillingReferenceNumber"]),
        "filing_date": _date(rec.get("caseFilingDate")),
        "first_hearing_date": hearings[0] if hearings else _date(rec.get("dateOfNextearing")),
        "next_hearing_date": _date(rec.get("dateOfNextearing")),
        "stage": stage.title() or None,
        "petitioners": [{"name": complainant, "advocate": advocates(rec.get("complainantAdvocate"))}] if complainant else [],
        "respondents": [{"name": respondent, "advocate": advocates(rec.get("respondentAdvocate"))}] if respondent else [],
        "petitioner_advocate": advocates(rec.get("complainantAdvocate")) or None,
        "title": title_of(complainant, respondent),
        "company_role": side,
        "role_source": "case_page",
        "category": "consumer",
        "detail_fetched_at": now(),
        "detail_error": None,
        "updated_at": now(),
        "raw_search": {"complainant": complainant, "respondent": respondent, "stage": stage,
                       "commission": rec.get("filedInComissionName"), "cause_date": rec.get("dateOfCause")},
    }
    if "DISPOS" in stage.upper():
        values["status"] = "disposed"
        values["decision_date"] = next((_date(h.get("dateOfAction")) for h in hist if (h.get("actionTaken") or "").lower() == "disposed"), None)
    return values


def save(db: DB, values: dict, hist: list[dict], baseline: bool) -> str:
    existing = db.select("cases", f"select=id&cnr=eq.{values['cnr']}")
    if existing:
        case_id = existing[0]["id"]
        db.update("cases", f"id=eq.{case_id}", values)
    else:
        case_id = db.insert("cases", {**values, "is_baseline": baseline}, returning="id")[0]["id"]
        db.log(case_id, "detected", {"forum": "consumer", "court": values["raw_search"]["commission"], "baseline": baseline})
    rows = [{"case_id": case_id, "hearing_date": _date(h["dateOfAction"]), "business_date": _date(h["dateOfAction"]),
             "purpose": h.get("actionTaken"), "judge": h.get("judgesName")} for h in hist if h.get("dateOfAction")]
    if rows:
        db.insert("hearings", rows, upsert_on="case_id,hearing_date,business_date", ignore_duplicates=True)
    return case_id


def read_number(ej: EJagriti, db: DB, court: dict, series: str, year: int, n: int, comps, baseline: bool) -> str:
    """'match', 'other' or 'missing'."""
    number = f"{series}/{n}/{year}"
    rec = ej.case(number, int(court["complex_code"]))
    if not rec:
        return "missing"
    # Only complaints naming a watched company need their hearing history.
    try:
        if not case_values(rec, [], court, comps):
            return "other"
        hist = ej.history(number)
        save(db, case_values(rec, hist, court, comps), hist, baseline)
    except Exception as exc:  # one odd record must not stop the scan; it is reported and skipped
        print(f"    {number}: could not save ({type(exc).__name__}: {str(exc)[:120]})", flush=True)
        return "other"
    return "match"


def scan(db: DB, ej: EJagriti, backfill: int, city: str = "") -> dict:
    comps = companies(db)
    stats = {"read": 0, "matches": 0, "refreshed": 0, "commissions": 0}
    courts = db.select("courts", "select=*&forum=eq.consumer&active=eq.true")
    if city:
        courts = [c for c in courts if (c.get("city") or "").lower() == city.lower()]
    years = [date.today().year] + ([date.today().year - 1] if date.today().month == 1 else [])
    for court in courts:
        stats["commissions"] += 1
        for series in [s for s in (court["est_codes"] or "").split(",") if s]:
            for year in years:
                key = f"court_id=eq.{court['id']}&series=eq.{series}&year=eq.{year}"
                w = next(iter(db.select("number_watch", f"select=*&{key}")), None)
                first = w is None
                if first:
                    last = latest_number(ej, series, year, int(court["complex_code"]))
                    w = {"court_id": court["id"], "series": series, "year": year, "last_number": max(0, last - backfill), "missing": []}
                    print(f"  {court['complex_name']} {series}/{year}: latest complaint {last}", flush=True)
                n, misses, last_found = w["last_number"] + 1, 0, w["last_number"]
                missing = [m for m in w["missing"] if m["since"] >= (date.today() - timedelta(days=RETRY_DAYS)).isoformat()]
                still = []
                for m in missing:  # numbers skipped earlier
                    res = read_number(ej, db, court, series, year, m["n"], comps, False)
                    stats["read"] += 1
                    stats["matches"] += res == "match"
                    if res == "missing":
                        still.append(m)
                while misses < GAP:
                    res = read_number(ej, db, court, series, year, n, comps, first)
                    stats["read"] += 1
                    if res == "missing":
                        misses += 1
                    else:
                        stats["matches"] += res == "match"
                        for k in range(last_found + 1, n):  # gaps inside the range: retry later
                            if not any(x["n"] == k for x in still):
                                still.append({"n": k, "since": date.today().isoformat()})
                        last_found, misses = n, 0
                    n += 1
                db.insert("number_watch", {"court_id": court["id"], "series": series, "year": year, "last_number": last_found,
                                           "missing": still, "checked_at": now()}, upsert_on="court_id,series,year")
                print(f"  {court['complex_name']} {series}/{year}: read up to {last_found}, {len(still)} numbers to retry", flush=True)

    # Cases whose hearing date has passed: read again for the next date and outcome.
    today = date.today().isoformat()
    due = db.select("cases", f"select=id,case_number,court_id,detail_fetched_at,next_hearing_date&forum=eq.consumer"
                             f"&status=not.in.(disposed,not_ours)&next_hearing_date=lt.{today}")
    by_court = {c["id"]: c for c in courts}
    for c in due:
        if c["detail_fetched_at"][:10] > c["next_hearing_date"] and c["detail_fetched_at"][:10] >= (date.today() - timedelta(days=3)).isoformat():
            continue
        court = by_court.get(c["court_id"])
        if not court:
            continue
        rec = ej.case(c["case_number"], int(court["complex_code"]))
        if rec:
            hist = ej.history(c["case_number"])
            values = case_values(rec, hist, court, comps)
            if values:
                save(db, values, hist, False)
                stats["refreshed"] += 1
    refresh_suggestions(db)
    from .orders import refresh_deadlines
    refresh_deadlines()
    return stats


def add(db: DB, ej: EJagriti, state: str, districts: list[str], state_commission: bool, zone_name: str) -> None:
    st = next((s for s in ej.state_commissions() if s["commissionNameEn"].strip().lower() == state.lower()), None)
    if not st:
        raise SystemExit(f"state commission not found: {state}")
    zone = db.insert("zones", {"name": zone_name, "city": zone_name.replace(" Zone", "")}, upsert_on="name", returning="*")[0]
    targets = []
    if state_commission:
        targets.append((st["commissionId"], f"State Commission, {st['commissionNameEn'].title()}", ("CC", "A", "FA")))
    dists = ej.district_commissions(st["commissionId"])
    for d in districts:
        hit = next((x for x in dists if x["commissionNameEn"].strip().lower() == d.lower()), None)
        if not hit:
            raise SystemExit(f"district commission {d!r} not found. Options: {sorted(x['commissionNameEn'] for x in dists)}")
        targets.append((hit["commissionId"], f"District Commission, {hit['commissionNameEn']}", ("CC",)))
    for cid, name, kinds in targets:
        series = series_of(ej.example_numbers(cid), kinds)
        if not series:
            print(f"  {name}: no case numbers found to learn the numbering from; add --series by hand later")
        db.insert("courts", {"state_code": str(st["commissionId"]), "dist_code": "consumer", "complex_code": str(cid),
                             "est_codes": ",".join(series), "needs_est": False, "forum": "consumer",
                             "district_name": f"Consumer, {st['commissionNameEn'].title()}", "complex_name": name,
                             "zone_id": zone["id"], "active": True}, upsert_on="state_code,dist_code,complex_code")
        print(f"  added {name} (id {cid}), numbering {series or '?'}", flush=True)
    places.fill(db)
    places.add_to_terms(db, [c["id"] for c in db.select("courts", f"select=id&forum=eq.consumer&state_code=eq.{st['commissionId']}")])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--state", required=True)
    a.add_argument("--districts", nargs="*", default=[])
    a.add_argument("--state-commission", action="store_true")
    a.add_argument("--zone", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--backfill", type=int, default=60, help="on a commission's first scan, also read this many latest complaints")
    s.add_argument("--city", default="", help="only commissions in this city (Scan now)")
    se = sub.add_parser("series", help="set a commission's numbering by hand")
    se.add_argument("--commission", required=True, help="e.g. 'District Commission, Jaipur-III'")
    se.add_argument("--series", nargs="+", required=True, help="e.g. DC/AB2/102/CC")
    args = ap.parse_args()
    db, ej = DB(), EJagriti()
    if args.cmd == "add":
        add(db, ej, args.state, args.districts, args.state_commission, args.zone)
    elif args.cmd == "series":
        db.update("courts", f"forum=eq.consumer&complex_name=eq.{args.commission}", {"est_codes": ",".join(args.series)})
        print("ok")
    else:
        run = db.insert("scan_runs", {"trigger": "consumer"}, returning="*")[0]
        try:
            stats = scan(db, ej, args.backfill, args.city)
            status = "ok"
        except Exception as exc:
            stats, status = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, "failed"
        db.update("scan_runs", f"id=eq.{run['id']}", {"status": status, "finished_at": now(), "details_fetched": stats.get("read", 0),
                                                      "results": stats.get("matches", 0), "log": [{"at": now(), "level": "info" if status == "ok" else "error", "msg": f"consumer: {stats}"}]})
        print("consumer scan:", status, stats, f"({ej.requests} requests)")
        if status != "ok":
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
