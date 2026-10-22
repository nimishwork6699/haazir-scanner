"""Scraper audit: does the same search-and-read pipeline work outside Jaipur and for other companies?

For each region it searches pending cases filed this year for SBI plus two randomly picked
companies, opens a random sample of the case pages found, and checks every case page against
a list of rules (CNR format, the company is really named, side, date order, status, missing
fields). It also repeats a few searches and case lookups to see whether results are stable,
and compares "SBI" against "State Bank of India" as search terms. Read-only; writes nothing to
the database.

    python -m haazir.audit --out audit.json
"""

import argparse
import asyncio
import json
import random
import re
import time
from collections import Counter, defaultdict
from datetime import date

from .ecourts import CNR_RE, Blocked, ECourts
from .matching import is_company, role

REGIONS = [  # (state, district, complexes to sample)
    ("Rajasthan", "Jaipur Metro I", 1),      # control: the region the live scanner covers
    ("Himachal Pradesh", "Shimla", 2),
    ("Himachal Pradesh", "Kangra", 1),
    ("Madhya Pradesh", "Indore", 2),
    ("Madhya Pradesh", "Bhopal", 1),
    ("Delhi", "South West", 1),
    ("Maharashtra", "Pune", 1),
    ("Uttar Pradesh", "Lucknow", 1),
    ("Karnataka", "BENGALURU", 1),
    ("Gujarat", "Ahmedabad", 1),
]
FIXED = "State Bank of India"
POOL = ["HDFC Bank", "ICICI Lombard", "New India Assurance", "Bajaj Finance", "Bharti Airtel", "United India Insurance"]
SKIP = ("family", "jjb", "juvenile", "waqf", "nagar nigam", "gram nyayalaya", "gram nayalaya", "labour", "consumer")


def d2s(d) -> str | None:
    return d.isoformat() if d else None


def check(cnr: str, term: str, year: int, row, d) -> tuple[dict, list[str]]:
    """Rules every case page should pass. Returns the case summary and the flags it raised."""
    flags = []
    pets = [p.name for p in d.petitioners]
    resps = [p.name for p in d.respondents]
    side = role(pets, resps, [term])
    list_side = role([row.petitioner], [row.respondent], [term])
    today = date.today()

    if not CNR_RE.match(cnr):
        flags.append("cnr_format")
    if d.cnr_number and d.cnr_number.upper() != cnr.upper():
        flags.append("cnr_mismatch_on_page")
    if side == "unknown":
        flags.append("company_not_named_on_page")
    elif list_side != "unknown" and list_side != side:
        flags.append("list_side_disagrees")
    for f in ("case_type", "filing_date", "registration_date", "first_hearing_date"):
        if not getattr(d, f):
            flags.append(f"missing_{f}")
    if not pets or not resps:
        flags.append("missing_parties")
    if d.filing_date and d.filing_date.year != year:
        flags.append("filing_year_not_search_year")
    if d.filing_date and cnr[-4:].isdigit() and int(cnr[-4:]) != d.filing_date.year:
        flags.append("cnr_year_not_filing_year")
    if d.filing_date and d.registration_date and d.registration_date < d.filing_date:
        flags.append("registered_before_filed")
    if d.filing_date and d.first_hearing_date and d.first_hearing_date < d.filing_date:
        flags.append("first_hearing_before_filing")
    if d.first_hearing_date and d.next_hearing_date and d.next_hearing_date < d.first_hearing_date:
        flags.append("next_before_first_hearing")
    if d.filing_date and d.filing_date > today:
        flags.append("filed_in_future")
    if d.decision_date or (d.status or "").lower().startswith("disposed"):
        flags.append("searched_pending_but_disposed")
    elif d.next_hearing_date and d.next_hearing_date < today:
        flags.append("next_hearing_in_past")
    elif not d.next_hearing_date:
        flags.append("missing_next_hearing")
    hist = [h.hearing_date for h in d.history if h.hearing_date]
    if hist and d.next_hearing_date and max(hist) > d.next_hearing_date:
        flags.append("history_later_than_next_hearing")

    case = {
        "cnr": cnr, "type": d.case_type, "number": d.registration_number or row.case_number,
        "title": f"{pets[0] if pets else '?'} vs {resps[0] if resps else '?'}",
        "side": side, "list_side": list_side, "filed": d2s(d.filing_date), "registered": d2s(d.registration_date),
        "first_hearing": d2s(d.first_hearing_date), "next_hearing": d2s(d.next_hearing_date),
        "stage": d.case_stage, "court": d.court_number_and_judge, "flags": flags,
    }
    return case, flags


async def run(args) -> dict:
    rng = random.Random(args.seed)
    rep = {"seed": args.seed, "year": args.year, "started": time.strftime("%Y-%m-%d %H:%M"), "regions": [],
           "searches": [], "cases": [], "repeat_search": [], "repeat_detail": [], "name_variant": [], "errors": []}
    blocked_once = False

    async def guarded(coro_fn, what: str):
        nonlocal blocked_once
        try:
            return await coro_fn()
        except Blocked:
            if blocked_once:
                raise
            blocked_once = True
            rep["errors"].append(f"BLOCKED at {what}; waited 3 minutes")
            print(f"  BLOCKED at {what}; waiting 3 minutes", flush=True)
            await asyncio.sleep(180)
            return await coro_fn()

    async with ECourts() as ec:
        states = await ec.client.list_states()
        courts = []
        for st_name, dist_name, n in REGIONS:
            sc = next((k for k, v in states.items() if v.strip().lower() == st_name.lower()), None)
            if not sc:
                rep["errors"].append(f"state not found: {st_name}")
                continue
            dists = await ec.client.list_districts(sc)
            dc = next((k for k, v in dists.items() if v.strip().lower() == dist_name.lower()), None)
            if not dc:
                rep["errors"].append(f"district not found: {st_name}/{dist_name}")
                continue
            cx = await ec.list_complexes(sc, dc)
            usable = [(v, name) for v, name in cx.items() if not any(s in name.lower() for s in SKIP)]
            # The main district complex first (its name usually contains the district's), then random others.
            usable.sort(key=lambda x: (dist_name.split()[0].lower() not in x[1].lower(), rng.random()))
            for value, name in usable[:n]:
                code, ests, flag = (value.split("@") + ["", ""])[:3]
                courts.append({"state": st_name, "state_code": sc, "dist_code": dc, "district": dist_name,
                               "complex_code": code, "est_codes": ests, "needs_est": flag == "Y",
                               "complex_name": name.strip()})
            rep["regions"].append({"state": st_name, "district": dist_name, "complexes_available": len(usable),
                                   "sampled": [name.strip() for _, name in usable[:n]]})
        print(f"{len(courts)} court complexes", flush=True)

        # 1) Searches: SBI everywhere, plus two random companies per region.
        hits = defaultdict(list)  # (complex, term) -> rows
        region_terms = {}
        for c in courts:
            key = (c["state"], c["district"])
            if key not in region_terms:
                region_terms[key] = [FIXED] + rng.sample(POOL, 2)
            for term in region_terms[key]:
                t0 = time.time()
                try:
                    rows = await guarded(lambda: ec.search(c, term, args.year, "Pending"), f"search {c['complex_name']}/{term}")
                    err = None
                except Blocked:
                    rep["errors"].append("BLOCKED twice; stopped")
                    return rep
                except Exception as exc:
                    rows, err = [], f"{type(exc).__name__}: {str(exc)[:120]}"
                secs = round(time.time() - t0, 1)
                bad_cnr = [r.cnr_number for r in rows if not CNR_RE.match(r.cnr_number or "")]
                named = sum(1 for r in rows if is_company(r.petitioner, [term]) or is_company(r.respondent, [term]))
                types = Counter(r.case_type or "?" for r in rows).most_common(4)
                prefixes = sorted({(r.cnr_number or "")[:2] for r in rows})
                rep["searches"].append({"state": c["state"], "complex": c["complex_name"], "term": term,
                                        "establishments": len(c["est_codes"].split(",")) if c["needs_est"] else 1,
                                        "hits": len(rows), "named_in_list": named, "bad_cnr": bad_cnr,
                                        "cnr_prefixes": prefixes, "types": types, "secs": secs, "error": err})
                hits[(c["complex_name"], term)] = (c, rows)
                print(f"  {c['state']:16} {c['complex_name'][:34]:34} {term:22} {len(rows):4} hits {secs:5}s"
                      + (f"  ERROR {err}" if err else ""), flush=True)

        # 2) Random case pages from every search that found something.
        for (cx_name, term), (c, rows) in hits.items():
            for row in rng.sample(rows, min(args.per_search, len(rows))):
                t0 = time.time()
                try:
                    d = await guarded(lambda: ec.detail(row.cnr_number), f"detail {row.cnr_number}")
                except Blocked:
                    rep["errors"].append("BLOCKED twice; stopped")
                    return rep
                except Exception as exc:
                    rep["cases"].append({"cnr": row.cnr_number, "state": c["state"], "complex": cx_name, "term": term,
                                         "error": f"{type(exc).__name__}: {str(exc)[:120]}", "flags": ["detail_failed"]})
                    continue
                case, flags = check(row.cnr_number, term, args.year, row, d)
                case.update({"state": c["state"], "complex": cx_name, "term": term, "secs": round(time.time() - t0, 1)})
                rep["cases"].append(case)
                print(f"    {row.cnr_number} {case['side']:10} {', '.join(flags) or 'ok'}", flush=True)

        # 3) Stability: the same search again, and the same case page again.
        nonempty = [(k, v) for k, v in hits.items() if v[1]]
        for (cx_name, term), (c, rows) in rng.sample(nonempty, min(3, len(nonempty))):
            again = await guarded(lambda: ec.search(c, term, args.year, "Pending"), "repeat search")
            a, b = {r.cnr_number for r in rows}, {r.cnr_number for r in again}
            rep["repeat_search"].append({"complex": cx_name, "term": term, "first": len(a), "second": len(b),
                                         "only_first": sorted(a - b), "only_second": sorted(b - a)})
            print(f"  repeat search {cx_name[:30]} {term}: {len(a)} then {len(b)}", flush=True)
        good = [x for x in rep["cases"] if "error" not in x]
        for x in rng.sample(good, min(3, len(good))):
            d = await guarded(lambda: ec.detail(x["cnr"]), "repeat detail")
            same = {"filed": d2s(d.filing_date) == x["filed"], "next_hearing": d2s(d.next_hearing_date) == x["next_hearing"],
                    "type": d.case_type == x["type"]}
            rep["repeat_detail"].append({"cnr": x["cnr"], "identical": all(same.values()), "fields": same})

        # 4) Name variants: does searching the short form find cases the full name misses?
        for c in [c for c in courts if c["state"] in ("Rajasthan", "Madhya Pradesh")][:2]:
            for term in ("SBI", "S.B.I"):
                rows = await guarded(lambda: ec.search(c, term, args.year, "Pending"), f"variant {term}")
                full = {r.cnr_number for r in hits.get((c["complex_name"], FIXED), (c, []))[1]}
                extra = [r for r in rows if r.cnr_number not in full]
                rep["name_variant"].append({
                    "complex": c["complex_name"], "term": term, "hits": len(rows),
                    "not_found_by_full_name": len(extra),
                    "examples": [f"{r.petitioner} vs {r.respondent}" for r in extra[:5]],
                    "our_matcher_accepts": [is_company(r.petitioner, [FIXED]) or is_company(r.respondent, [FIXED]) for r in extra[:5]],
                })
                print(f"  variant {term} in {c['complex_name'][:30]}: {len(rows)} hits, {len(extra)} not found by full name", flush=True)
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=date.today().year)
    ap.add_argument("--per-search", type=int, default=2, help="random case pages opened per search")
    ap.add_argument("--seed", type=int, default=int(time.time()) % 10000)
    ap.add_argument("--out", default="audit.json")
    args = ap.parse_args()
    t0 = time.time()
    rep = asyncio.run(run(args))
    rep["total_seconds"] = round(time.time() - t0)
    flags = Counter(f for c in rep["cases"] for f in c.get("flags", []))
    rep["flag_counts"] = dict(flags.most_common())
    open(args.out, "w", encoding="utf-8").write(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
    print(f"\ndone in {rep['total_seconds']}s; {len(rep['searches'])} searches, {len(rep['cases'])} case pages")
    print("flags:", rep["flag_counts"])


if __name__ == "__main__":
    main()
