"""National Company Law Tribunal (NCLT) on efiling.nclt.gov.in: its public case status, no captcha, no login.

    python -m haazir.nclt party "ICICI Bank" --bench 9 --year 2026     # try a party search
    python -m haazir.nclt cin L65190GJ1994PLC021012                     # every case tagged with a company's CIN
    python -m haazir.nclt name L65190GJ1994PLC021012                    # the company's name for a CIN
    python -m haazir.nclt scan [--company ICICI]                        # watched companies -> Our cases

Searches go to every bench: a company's NCLT cases follow where it is registered, not where a search is aimed, and
there are only 15 benches. The site also returns filers' login ids, emails and phone numbers: never kept.
"""

import argparse
import re
import sys
import time
from datetime import date, datetime, timezone

import httpx

from .db import DB
from .matching import is_company, title_of

BASE = "https://efiling.nclt.gov.in"
# The case-status page's own list (Principal and New Delhi benches share 10).
BENCHES = {1: "Ahmedabad", 2: "Allahabad", 3: "Bengaluru", 4: "Chandigarh", 5: "Chennai", 6: "Guwahati", 7: "Hyderabad", 8: "Kolkata",
           9: "Mumbai", 10: "New Delhi", 11: "Jaipur", 12: "Amaravati", 13: "Cuttack", 14: "Kochi", 15: "Indore"}
BENCH_ID = {v: k for k, v in BENCHES.items()}
PAUSE = 2.0  # seconds between requests


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso(d: str | None) -> str | None:
    """'01-12-2026' or '2026-12-01' -> '2026-12-01'; anything else (NA, '') -> None."""
    d = (d or "").strip()
    try:
        if len(d) == 10 and d[2] == "-":
            return datetime.strptime(d, "%d-%m-%Y").date().isoformat()
        if len(d) == 10 and d[4] == "-":
            return date.fromisoformat(d).isoformat()
    except ValueError:
        return None
    return None


class NCLT:
    def __init__(self) -> None:
        self.http = httpx.Client(base_url=BASE, timeout=60, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (Haazir case tracker)", "X-Requested-With": "XMLHttpRequest"})
        self.ready = False
        self.last = 0.0

    def _wait(self) -> None:
        gap = time.time() - self.last
        if gap < PAUSE:
            time.sleep(PAUSE - gap)
        self.last = time.time()

    def _session(self) -> None:
        if not self.ready:
            self._wait()
            self.http.get("/casehistorybeforeloginmenutrue.drt").raise_for_status()
            self.ready = True

    def _post(self, path: str, body: dict) -> dict:
        self._session()
        self._wait()
        r = self.http.post(path, json=body, headers={"Content-Type": "application/json"})
        r.raise_for_status()
        return r.json() if r.content else {}

    def party(self, name: str, bench: int, year: int, status: str = "P", side: str = "0") -> list[dict]:
        """Cases at one bench with a party whose name contains `name`. status P pending, D disposed, 0 both;
        side P petitioner, R respondent, 0 either."""
        body = {"wayofselection": "partyname", "i_party_search": "W", "i_bench_id_party": str(bench), "party_type_party": side,
                "party_name_party": name, "i_case_year_party": str(year), "status_party": status,
                "i_bench_id": "0", "filing_no": "", "i_bench_id_case_no": "0", "i_case_type_caseno": "0", "i_case_year_caseno": "0",
                "case_no": "", "i_adv_search": "E", "i_bench_id_lawyer": "0", "party_lawer_name": "", "i_case_year_lawyer": "0",
                "bar_council_advocate": ""}
        return [norm(r) for r in (self._post("/caseHistoryoptional.drt", body).get("mainpanellist") or [])]

    def advocate(self, name: str, bench: int, year: int, status: str = "P") -> list[dict]:
        """Cases at one bench where an advocate whose name contains `name` appears."""
        body = {"wayofselection": "advocatename", "i_adv_search": "W", "i_bench_id_lawyer": str(bench), "party_lawer_name": name,
                "i_case_year_lawyer": str(year), "bar_council_advocate": "", "status_party": status,
                "i_bench_id": "0", "filing_no": "", "i_bench_id_case_no": "0", "i_case_type_caseno": "0", "i_case_year_caseno": "0",
                "case_no": "", "i_party_search": "E", "i_bench_id_party": "0", "party_type_party": "0", "party_name_party": "", "i_case_year_party": "0"}
        return [norm(r) for r in (self._post("/caseHistoryoptional.drt", body).get("mainpanellist") or [])]

    def by_cin(self, cin: str) -> list[dict]:
        """Every case at every bench whose filer tagged the company's CIN (tags are typed by filers: some are missing)."""
        return [norm(r) for r in (self._post("/caseHistoryoptionalCIN.drt", {"cin_number": cin}).get("byCinNumber") or [])]

    def company_name(self, cin: str) -> str | None:
        name = ((self._post("/getCompanyName.drt", {"cin": cin}) or {}).get("companyName") or "").strip()
        # An unknown CIN comes back as a message in the name field.
        return name if name and not re.search(r"valid cin|not found|no record|invalid", name, re.I) else None


def party_name(v: str | None) -> str:
    """A party as listed; the site writes NA where there is none."""
    v = (v or "").strip()
    return "" if v.upper().strip(".") in ("NA", "N.A", "NIL", "-") else v


def norm(r: dict) -> dict:
    """One case as Haazir keeps it. Party search and CIN search name their fields differently."""
    status = r.get("status") or {"P": "Pending", "D": "Disposed"}.get(r.get("case_status") or "", "")
    return {
        "filing_no": r.get("filing_no"),
        "case_number": r.get("case_no") or r.get("caseNo1") or None,
        "case_type": r.get("case_type_desc_cis") or None,
        "bench": (r.get("bench_location_name") or "").strip(),
        "court_no": r.get("court_no") or None,
        "petitioner": party_name(r.get("case_title1") or r.get("petName")),
        "respondent": party_name(r.get("case_title2") or r.get("resName")),
        "filed": iso(r.get("date_of_filing")),
        "registered": iso(r.get("regis_date")),
        "last_listed": iso(r.get("listing_date")),
        "next_date": iso(r.get("next_list_date")) or iso(r.get("case_next_list_date")),
        "disposed": iso(r.get("disposal_date")),
        "pending": status.lower().startswith("p") if status else None,
        "cin_side": {"P": "petitioner", "R": "respondent"}.get(r.get("partyFlag") or ""),
    }


def side_for(c: dict, names: list[str]) -> str:
    """Which side the company is on: the CIN tag's own side, else where its name appears."""
    if c.get("cin_side"):
        return c["cin_side"]
    on_p, on_r = is_company(c["petitioner"], names), is_company(c["respondent"], names)
    return "respondent" if on_r else "petitioner" if on_p else "unknown"


def bench_court(db: DB, bench: str, cache: dict) -> str | None:
    """The courts row for an NCLT bench (made the first time a case there is saved)."""
    if bench not in cache:
        rows = db.select("courts", f"select=id&forum=eq.nclt&complex_name=eq.NCLT {bench}")
        if not rows and bench:
            rows = db.insert("courts", {"forum": "nclt", "state_code": "nclt", "dist_code": "nclt", "complex_code": str(BENCH_ID.get(bench, bench)),
                                        "complex_name": f"NCLT {bench}", "district_name": bench, "city": bench, "active": True}, returning="id")
        cache[bench] = rows[0]["id"] if rows else None
    return cache[bench]


def case_row(c: dict, company_id: str, side: str, court_id: str | None, baseline: bool) -> dict:
    # NCLT has no CNR: its 16-digit filing number is the case's lasting id.
    return {
        "cnr": f"NCLT{c['filing_no']}", "forum": "nclt", "company_id": company_id, "court_id": court_id,
        "case_type": c["case_type"], "case_number": c["case_number"], "filing_date": c["filed"], "registration_date": c["registered"],
        "next_hearing_date": c["next_date"], "decision_date": c["disposed"], "court_judge": f"Court {c['court_no']}" if c["court_no"] else None,
        "petitioners": [{"name": c["petitioner"], "advocate": ""}] if c["petitioner"] else [],
        "respondents": [{"name": c["respondent"], "advocate": ""}] if c["respondent"] else [],
        "title": title_of(c["petitioner"], c["respondent"]), "company_role": side,
        "role_source": "case_page" if c.get("cin_side") else ("search_list" if side != "unknown" else "unknown"),
        "category": "insolvency" if "IB" in (c["case_number"] or "") else "company_law", "is_baseline": baseline,
        "status": "disposed" if c["pending"] is False else "new",
        "raw_search": {"source": "nclt", "filing_no": c["filing_no"], "bench": c["bench"]},
        "detail_fetched_at": now(),
    }


def scan(db: DB, company: str = "", years: int = 2, dry: bool = False) -> dict:
    """Watched companies' NCLT cases into Our cases: by CIN (every year) where the company has one, and by name
    (pending cases of this year and last, every bench). Next dates of cases already saved are refreshed."""
    nc, stats, courts = NCLT(), {"searches": 0, "found": 0, "new": 0, "updated": 0}, {}
    comps = db.select("companies", "select=id,name,short_name,cin")
    if company:
        comps = [c for c in comps if company.lower() in (c["short_name"].lower(), c["name"].lower())]
    terms: dict[str, list[str]] = {}
    for t in db.select("watch_terms", "select=company_id,term,confirm&active=eq.true"):
        terms.setdefault(t["company_id"], []).append(t["term"])
        terms.setdefault(f"confirm:{t['company_id']}", []).extend(t["confirm"])
    have = {r["cnr"]: r for r in db.select("cases", "select=id,cnr,next_hearing_date,status&forum=eq.nclt")}
    this_year = date.today().year
    for co in comps:
        names = terms.get(f"confirm:{co['id']}") or [co["name"]]
        found: dict[str, dict] = {}
        if co.get("cin"):
            for c in nc.by_cin(co["cin"]):
                found[c["filing_no"]] = c
            stats["searches"] += 1
        for term in terms.get(co["id"], []):
            for bench in BENCHES:
                for year in range(this_year - years + 1, this_year + 1):
                    try:
                        rows = nc.party(term, bench, year)
                    except Exception as exc:
                        print(f"  {term} · {BENCHES[bench]} {year}: failed ({type(exc).__name__})", flush=True)
                        continue
                    stats["searches"] += 1
                    for c in rows:
                        if c["filing_no"] and (is_company(c["petitioner"], names) or is_company(c["respondent"], names)):
                            found.setdefault(c["filing_no"], c)
        stats["found"] += len(found)
        new = []
        for c in found.values():
            row = case_row(c, co["id"], side_for(c, names), None if dry else bench_court(db, c["bench"], courts), baseline=True)
            old = have.get(row["cnr"])
            if old and not dry:
                if old["next_hearing_date"] != row["next_hearing_date"] or (row["status"] == "disposed") != (old["status"] == "disposed"):
                    db.update("cases", f"id=eq.{old['id']}", {"next_hearing_date": row["next_hearing_date"], "decision_date": row["decision_date"],
                                                            "detail_fetched_at": now(), **({"status": "disposed"} if row["status"] == "disposed" else {})})
                    stats["updated"] += 1
            elif row["status"] != "disposed":
                new.append(row)
        if dry:  # nothing written: show what would be saved
            for r in new[:5]:
                print(f"   would add {r['case_number']} · {r['title']} · {r['company_role']}", flush=True)
            stats["new"] += len(new)
            continue
        for i in range(0, len(new), 200):
            stats["new"] += len(db.insert("cases", new[i:i + 200], upsert_on="cnr", ignore_duplicates=True, returning="id"))
        print(f"{co['short_name']}: {len(found)} NCLT cases, {len(new)} new to Haazir", flush=True)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("party")
    p.add_argument("name")
    p.add_argument("--bench", type=int, default=10)
    p.add_argument("--year", type=int, default=date.today().year)
    c = sub.add_parser("cin")
    c.add_argument("cin")
    n = sub.add_parser("name")
    n.add_argument("cin")
    s = sub.add_parser("scan")
    s.add_argument("--company", default="")
    s.add_argument("--years", type=int, default=2)
    s.add_argument("--dry", action="store_true", help="search and show, save nothing")
    args = ap.parse_args()
    if args.cmd == "party":
        for r in NCLT().party(args.name, args.bench, args.year):
            print(r)
    elif args.cmd == "cin":
        rows = NCLT().by_cin(args.cin)
        print(f"{len(rows)} cases")
        for r in rows[:10]:
            print(r)
    elif args.cmd == "name":
        print(NCLT().company_name(args.cin))
    else:
        print(scan(DB(), args.company, args.years, args.dry))
    return 0


if __name__ == "__main__":
    sys.exit(main())
