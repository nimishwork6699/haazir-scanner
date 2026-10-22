"""Coverage probe: how well can we watch these companies in this place?

Searches pending cases for each term in a few court complexes, then opens a sample of case
pages and checks what came back: hit counts, speed, which side the company is on, whether the
search list agreed with the case page, and which dates are present. Read-only; writes nothing
to the database.

    python -m haazir.probe --state Rajasthan --districts "Jaipur Metro I" --complexes 2 \
        --terms "State Bank of India" "Bharti Airtel" --details 3 --out probe_jaipur.json
"""

import argparse
import asyncio
import json
import time
from collections import Counter
from datetime import date

from .ecourts import Blocked, ECourts
from .matching import role


async def run(args) -> dict:
    report = {"state": args.state, "year": args.year, "terms": {}, "complexes": [], "errors": []}
    async with ECourts() as ec:
        states = await ec.client.list_states()
        st = next((k for k, v in states.items() if args.state.lower() in v.lower()), None)
        if not st:
            raise SystemExit(f"state not found: {args.state}")
        dists = await ec.client.list_districts(st)
        courts = []
        for want in args.districts:
            dc = next((k for k, v in dists.items() if want.lower() == v.lower()), None) \
                or next((k for k, v in dists.items() if want.lower() in v.lower()), None)
            if not dc:
                report["errors"].append(f"district not found: {want} (have: {sorted(dists.values())})")
                continue
            cx = await ec.list_complexes(st, dc)
            for value, name in list(cx.items())[: args.complexes]:
                code, ests, flag = (value.split("@") + ["", ""])[:3]
                courts.append({"state_code": st, "dist_code": dc, "complex_code": code, "est_codes": ests,
                               "needs_est": flag == "Y", "district_name": dists[dc], "complex_name": name.strip()})
        report["complexes"] = [f"{c['district_name']} / {c['complex_name']}" for c in courts]

        samples: dict[str, list] = {t: [] for t in args.terms}
        for term in args.terms:
            stats = {"hits": 0, "searches": 0, "failed": 0, "seconds": 0.0, "case_types": Counter()}
            for court in courts:
                t0 = time.time()
                try:
                    rows = await ec.search(court, term, args.year, "Pending")
                except Blocked:
                    report["errors"].append("BLOCKED by eCourts firewall; stopped")
                    report["terms"][term] = stats
                    return report
                except Exception as exc:
                    stats["failed"] += 1
                    report["errors"].append(f"{court['complex_name']} / {term}: {type(exc).__name__}")
                    continue
                stats["searches"] += 1
                stats["seconds"] += time.time() - t0
                stats["hits"] += len(rows)
                for r in rows:
                    stats["case_types"][r.case_type or "?"] += 1
                # newest filings first (CNR serial), keep a few per term
                rows = sorted(rows, key=lambda r: r.cnr_number[6:12], reverse=True)
                samples[term] += [(court["complex_name"], r) for r in rows[: args.details]]
            stats["case_types"] = dict(stats["case_types"].most_common(5))
            stats["avg_search_s"] = round(stats["seconds"] / max(1, stats["searches"]), 1)
            report["terms"][term] = stats

        today = date.today().isoformat()
        for term, items in samples.items():
            checked = []
            for complex_name, r in items[: args.details]:
                t0 = time.time()
                try:
                    d = await ec.detail(r.cnr_number)
                except Blocked:
                    report["errors"].append("BLOCKED during case pages; stopped")
                    break
                except Exception as exc:
                    checked.append({"cnr": r.cnr_number, "error": type(exc).__name__})
                    continue
                confirm = [term.lower()]
                pets = [p.name for p in d.petitioners]
                resps = [p.name for p in d.respondents]
                side = role(pets, resps, confirm)
                list_side = role([r.petitioner], [r.respondent], confirm)
                checked.append({
                    "cnr": r.cnr_number, "court": complex_name, "type": d.case_type,
                    "title": f"{pets[0] if pets else '?'} vs {resps[0] if resps else '?'}",
                    "side": side, "list_side": list_side,
                    "filed": str(d.filing_date), "first_hearing": str(d.first_hearing_date),
                    "next_hearing": str(d.next_hearing_date), "stage": d.case_stage,
                    "acts": [a.act for a in d.acts][:2],
                    "dates_ok": bool(d.filing_date and d.registration_date and d.first_hearing_date),
                    "next_in_past": bool(d.next_hearing_date and str(d.next_hearing_date) < today),
                    "secs": round(time.time() - t0, 1),
                })
            report["terms"][term]["sample"] = checked
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--districts", nargs="+", required=True)
    ap.add_argument("--complexes", type=int, default=2, help="court complexes per district")
    ap.add_argument("--terms", nargs="+", required=True)
    ap.add_argument("--details", type=int, default=3, help="case pages to open per term")
    ap.add_argument("--year", type=int, default=date.today().year)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    t0 = time.time()
    report = asyncio.run(run(args))
    report["total_seconds"] = round(time.time() - t0)
    text = json.dumps(report, indent=1, ensure_ascii=False, default=str)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
    print(text)


if __name__ == "__main__":
    main()
