"""Second audit pass: Madhya Pradesh raw results, real Lucknow and Ahmedabad complexes, and how
many SBI cases only a whole-word "SBI" search finds. Read-only.

    python -m haazir.audit2 --out audit2.json
"""

import argparse
import asyncio
import json
import random
import re

from bharat_courts.districtcourts import endpoints
from bharat_courts.districtcourts.parser import parse_case_status_html

from .audit import CNR_RE, check
from .ecourts import ECourts
from .matching import is_company

SKIP = ("test", "railway", "family", "juvenile", "jjb", "gram", "labour", "consumer", "nagar nigam")


async def raw_party(ec: ECourts, court: dict, term: str, year: int) -> str:
    c = ec.client
    code = c._bare_complex_code(court["complex_code"])
    est = court["est_codes"].split(",")[0] if court["needs_est"] else ""
    result = await c._post_with_captcha_retry(
        "casestatus/submitPartyName",
        lambda cap: endpoints.case_status_by_party_form(
            state_code=court["state_code"], dist_code=court["dist_code"], court_complex_code=code, est_code=est,
            party_name=term, year=str(year), status_filter="Pending", captcha=cap),
        state_code=court["state_code"], dist_code=court["dist_code"], court_complex_code=code, est_code=est)
    return result.get("party_data", "")


async def court_for(ec: ECourts, state: str, district: str, prefer: str) -> dict | None:
    states = await ec.client.list_states()
    sc = next(k for k, v in states.items() if v.strip().lower() == state.lower())
    dists = await ec.client.list_districts(sc)
    dc = next(k for k, v in dists.items() if v.strip().lower() == district.lower())
    cx = await ec.list_complexes(sc, dc)
    opts = [(v, n) for v, n in cx.items() if not any(s in n.lower() for s in SKIP)]
    opts.sort(key=lambda x: prefer.lower() not in x[1].lower())
    if not opts:
        return None
    value, name = opts[0]
    code, ests, flag = (value.split("@") + ["", ""])[:3]
    return {"state": state, "district": district, "state_code": sc, "dist_code": dc, "complex_code": code,
            "est_codes": ests, "needs_est": flag == "Y", "complex_name": name.strip(),
            "all_complexes": [n.strip() for _, n in cx.items()]}


async def run(args) -> dict:
    rng = random.Random(args.seed)
    rep = {"mp": {}, "regions": [], "sbi_word": {}}
    async with ECourts() as ec:
        # 1) Madhya Pradesh: what does the result page actually contain?
        indore = await court_for(ec, "Madhya Pradesh", "Indore", "indore")
        html = await raw_party(ec, indore, "State Bank of India", args.year)
        rows = parse_case_status_html(html)
        rep["mp"] = {"court": indore["complex_name"], "needs_est": indore["needs_est"], "est_codes": indore["est_codes"],
                     "all_complexes": indore["all_complexes"], "html_len": len(html), "parsed_rows": len(rows),
                     "html_head": re.sub(r"\s+", " ", html)[:3000]}
        print("MP html", len(html), "parsed", len(rows), flush=True)

        # 2) Real complexes in Lucknow and Ahmedabad.
        for state, district, prefer in (("Uttar Pradesh", "Lucknow", "district"), ("Gujarat", "Ahmedabad", "city civil"),
                                        ("Gujarat", "Ahmedabad", "rural")):
            court = await court_for(ec, state, district, prefer)
            if not court:
                continue
            entry = {"state": state, "complex": court["complex_name"], "options": court["all_complexes"], "searches": [], "cases": []}
            for term in ("State Bank of India", "HDFC Bank"):
                try:
                    found = await ec.search(court, term, args.year, "Pending")
                    err = None
                except Exception as exc:
                    found, err = [], f"{type(exc).__name__}: {str(exc)[:150]}"
                entry["searches"].append({"term": term, "hits": len(found), "error": err,
                                          "empty_cnr": sum(1 for r in found if not r.cnr_number)})
                print(f"  {state} {court['complex_name'][:40]} {term}: {len(found)} {err or ''}", flush=True)
                for row in rng.sample([r for r in found if CNR_RE.match(r.cnr_number or "")], min(2, len(found))):
                    try:
                        d = await ec.detail(row.cnr_number)
                        case, flags = check(row.cnr_number, term, args.year, row, d)
                    except Exception as exc:
                        case, flags = {"cnr": row.cnr_number, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}, ["detail_failed"]
                    case["flags"] = flags
                    entry["cases"].append(case)
                    print(f"    {row.cnr_number} {', '.join(flags) or 'ok'}", flush=True)
            rep["regions"].append(entry)

        # 3) "SBI" as a whole word vs the full name, Jaipur Metro I.
        jaipur = await court_for(ec, "Rajasthan", "Jaipur Metro I", "metro court complex i")
        full = {r.cnr_number for r in await ec.search(jaipur, "State Bank of India", args.year, "Pending")}
        short = await ec.search(jaipur, "SBI", args.year, "Pending")
        word = re.compile(r"\bS\.?\s?B\.?\s?I\b", re.I)
        real = [r for r in short if word.search(f"{r.petitioner} {r.respondent}")]
        missed = [r for r in real if r.cnr_number not in full]
        rep["sbi_word"] = {"short_hits": len(short), "whole_word": len(real), "missed_by_full_name": len(missed),
                           "examples": [f"{r.cnr_number}: {r.petitioner} vs {r.respondent}" for r in missed[:10]],
                           "matcher_accepts": [is_company(r.petitioner, ["State Bank of India"]) or
                                               is_company(r.respondent, ["State Bank of India"]) for r in missed[:10]]}
        print("SBI:", rep["sbi_word"]["short_hits"], "hits,", len(real), "whole word,", len(missed), "missed", flush=True)
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--seed", type=int, default=77)
    ap.add_argument("--out", default="audit2.json")
    args = ap.parse_args()
    rep = asyncio.run(run(args))
    open(args.out, "w", encoding="utf-8").write(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
    print("done")


if __name__ == "__main__":
    main()
