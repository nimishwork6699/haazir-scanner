"""Watch: turn a court search into watched cases.

    python -m haazir.watch run        # every waiting watch request, oldest first

From the names picked on a search's results (e.g. "sbi general" and "sbi gen", not "sbi card"):
1. the company is added (or found) and each picked name becomes a search name for the scan,
2. every court in the searched area is added to the courts the scan covers,
3. the cases found are copied into Our cases as "on file"; their case pages are read by later scans.
New filings then reach New cases through the normal twice-daily scan.
"""

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime, timezone

from . import places
from .classify import categorize
from .consumer import EJagriti, case_values, save, series_of
from .db import DB
from .ecourts import ECourts
from .lookup import area, tasks_of
from .matching import normalise


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def rows_of(db: DB, lookup_id: str, group: str) -> list[dict]:
    """Every result of a search whose matched name falls in one name group."""
    out, off = [], 0
    while True:
        page = db._check(db.http.post("/rpc/lookup_rows", json={"lid": lookup_id, "f_name": group, "off": off, "lim": 1000})) or []
        out += page
        if len(page) < 1000:
            return out
        off += 1000


def matched_name(r: dict) -> str:
    if r["side"] == "respondent" and (r.get("respondent") or "").strip():
        return r["respondent"]
    return (r.get("petitioner") or "").strip() or r.get("respondent") or ""


async def run_one(db: DB, w: dict) -> dict:
    lookup = db.select("lookups", f"select=*&id=eq.{w['lookup_id']}")[0]

    # 1. Company and search names.
    comp = db.insert("companies", {"name": w["company_name"], "short_name": w["short_name"], "sector": w["sector"]},
                     upsert_on="name")[0]
    picked: dict[str, list[dict]] = {g: rows_of(db, lookup["id"], g) for g in w["names"]}
    terms = []
    for group, rows in picked.items():
        # The name as courts write it most often, cut to its first two words: what eCourts is searched for.
        common = Counter(normalise(matched_name(r)) for r in rows).most_common(5)
        confirm = sorted({group} | {" ".join(n.split()[:2]) for n, _ in common if n} | {n for n, _ in common if n})
        term = " ".join((common[0][0] if common else group).split()[:2])
        terms.append((term, confirm))

    # 2. Courts of the searched area: from the search's own list of court searches (no need to ask eCourts
    # again), or listed afresh for searches made before that list was kept. For an all-India search only the
    # courts where cases were found are added, or every scan would search thousands of courts.
    ej = EJagriti()
    tasks = tasks_of(db, lookup["id"])
    if tasks:
        courts = {"district": [], "high_court": [], "consumer": []}
        seen = set()
        for t in tasks:
            key = (t["forum"], repr(sorted(t["payload"].items())))
            if key not in seen:
                seen.add(key)
                courts[t["forum"]].append(t["payload"])
    else:
        async with ECourts() as ec:
            courts = await area(ec, ej, lookup)
    if lookup["state_code"] == "all":
        found = {(r["forum"], r["court"], r.get("district") or "") for rows_ in picked.values() for r in rows_}
        courts = {f: [c for c in cs if (f, c["name"], c.get("district") or "") in found] for f, cs in courts.items()}
    rows = [{"state_code": c["court"]["state_code"], "dist_code": c["court"]["dist_code"], "complex_code": c["court"]["complex_code"],
             "est_codes": c["court"]["est_codes"], "needs_est": c["court"]["needs_est"], "forum": "district", "district_name": c["district"],
             "complex_name": c["name"], "state": c.get("state"), "active": True} for c in courts["district"]]
    rows += [{"state_code": c["code"], "dist_code": "hc", "complex_code": c["bench"], "est_codes": "", "needs_est": False, "forum": "high_court",
              "district_name": "High Court", "complex_name": c["name"], "state": c.get("state"), "active": True} for c in courts["high_court"]]
    known = {(c["state_code"], c["dist_code"], c["complex_code"]) for c in db.select("courts", "select=state_code,dist_code,complex_code")}
    for c in courts["consumer"]:
        if (str(c["st_id"]), "consumer", str(c["cid"])) in known:
            rows.append({"state_code": str(c["st_id"]), "dist_code": "consumer", "complex_code": str(c["cid"])})
            continue
        # The consumer scan reads complaints by number, so it needs this commission's numbering.
        series = series_of(ej.example_numbers(c["cid"]), ("CC", "A", "FA") if c["is_state"] else ("CC",))
        rows.append({"state_code": str(c["st_id"]), "dist_code": "consumer", "complex_code": str(c["cid"]), "est_codes": ",".join(series),
                     "needs_est": False, "forum": "consumer", "district_name": f"Consumer, {c.get('state') or lookup['state_name']}",
                     "complex_name": c["name"], "state": c.get("state"), "active": True})
    new_courts = [r for r in rows if (r["state_code"], r["dist_code"], r["complex_code"]) not in known]
    if new_courts:
        db.insert("courts", new_courts, upsert_on="state_code,dist_code,complex_code", ignore_duplicates=True)
    # A court that was switched off is switched back on: the company is watched there now.
    for r in rows:
        db.update("courts", f"state_code=eq.{r['state_code']}&dist_code=eq.{r['dist_code']}&complex_code=eq.{r['complex_code']}&active=eq.false", {"active": True})
    # Delhi's districts ("South West") are not cities; in a city-state the city is the state.
    keys = {(r["state_code"], r["dist_code"], r["complex_code"]) for r in rows}
    area_ids = []
    for c in db.select("courts", "select=id,state_code,dist_code,complex_code,state,city,district_name,complex_name,forum"):
        if (c["state_code"], c["dist_code"], c["complex_code"]) not in keys:
            continue
        area_ids.append(c["id"])
        if not c["state"] or not c["city"]:
            state = c["state"] or (lookup["state_name"] if lookup["state_code"] != "all" else None)
            city_state = state in ("Delhi", "Chandigarh", "Puducherry")
            city = state if city_state else (places.city_of(c["district_name"], c["complex_name"]) or lookup.get("district_name"))
            db.update("courts", f"id=eq.{c['id']}", {"state": state, "city": c["city"] or city})

    # The company's names are searched in this area's courts (and any courts they already had).
    for term, confirm in terms:
        old = db.select("watch_terms", f"select=court_ids&company_id=eq.{comp['id']}&term=eq.{term}")
        ids = sorted(set(area_ids) | set((old[0]["court_ids"] or []) if old else []))
        db.insert("watch_terms", {"company_id": comp["id"], "term": term, "confirm": confirm, "active": True, "court_ids": ids},
                  upsert_on="company_id,term")
    confirm_all = sorted({c for t in db.select("watch_terms", f"select=confirm&company_id=eq.{comp['id']}") for c in t["confirm"]})

    # 3. Cases found -> Our cases, as already on file. Case pages are read by later scans, cases against us first.
    by_name = {(c["forum"], c["complex_name"], c["district_name"] if c["forum"] == "district" else ""): c
               for c in db.select("courts", "select=id,forum,complex_name,district_name,complex_code,state_code,dist_code")}
    added = 0
    new_cases = []
    for r in (r for rows_ in picked.values() for r in rows_ if r["forum"] != "consumer"):
        court = by_name.get((r["forum"], r["court"], (r["district"] or "") if r["forum"] == "district" else ""))
        pet, resp = (r.get("petitioner") or "").strip(), (r.get("respondent") or "").strip()
        new_cases.append({
            "cnr": r["cnr"], "forum": r["forum"], "company_id": comp["id"], "court_id": court["id"] if court else None,
            "case_type": r.get("case_type"), "case_number": r.get("case_number"), "title": f"{pet} vs {resp}".strip(" vs") or None,
            "petitioners": [{"name": pet, "advocate": ""}] if pet else [], "respondents": [{"name": resp, "advocate": ""}] if resp else [],
            "company_role": "unknown", "category": categorize(r.get("case_type")), "is_baseline": True,
            "raw_search": {"petitioner": pet, "respondent": resp, "watch": w["id"]},
        })
    for i in range(0, len(new_cases), 500):
        added += len(db.insert("cases", new_cases[i:i + 500], upsert_on="cnr", ignore_duplicates=True))
    # Consumer complaints: read each one's record, as the consumer scan does, so it lands complete.
    for r in (r for rows_ in picked.values() for r in rows_ if r["forum"] == "consumer"):
        court = by_name.get(("consumer", r["court"], ""))
        if not court:
            continue
        try:
            rec = ej.case(r["case_number"], int(court["complex_code"]))
            if rec:
                hist = ej.history(r["case_number"])
                values = case_values(rec, hist, court, [(comp["id"], confirm_all)])
                if values:
                    save(db, values, hist, baseline=True)
                    added += 1
        except Exception as exc:  # one odd complaint must not stop the watch
            print(f"  consumer {r['case_number']}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
    return {"company_id": comp["id"], "terms": [t for t, _ in terms], "courts_added": len(new_courts), "cases_added": added,
            "cases_found": sum(len(v) for v in picked.values())}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    ap.parse_args()
    db = DB()
    for w in db.http.get("/watches?select=*&status=eq.queued&order=created_at").json():
        db.update("watches", f"id=eq.{w['id']}", {"status": "running"})
        try:
            result = asyncio.run(run_one(db, w))
            db.update("watches", f"id=eq.{w['id']}", {"status": "done", "result": result, "finished_at": now()})
            print("watch done:", result, flush=True)
        except Exception as exc:
            db.update("watches", f"id=eq.{w['id']}", {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}", "finished_at": now()})
            print("watch failed:", exc, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
