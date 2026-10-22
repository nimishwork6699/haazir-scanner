"""Independent re-check of the e-Jagriti and High Court data. Read-only.

    python -m haazir.verify_forums ejagriti
    python -m haazir.verify_forums highcourt
"""

import asyncio
import json
import random
import re
import sys

from .db import DB

LOOSE = re.compile(r"lomb|united\s*ind|bajaj|u\.?\s*i\.?\s*i\.?\s*c", re.I)


def ejagriti() -> None:
    from .consumer import EJagriti
    db, ej = DB(), EJagriti(pause=2.0)
    courts = {c["id"]: c for c in db.select("courts", "select=*&forum=eq.consumer")}
    stored = {c["case_number"] for c in db.select("cases", "select=case_number&forum=eq.consumer")}
    report = {"series": [], "missed": [], "extra_probe": {}}
    for w in db.select("number_watch", "select=*"):
        court = courts[w["court_id"]]
        cid, last = int(court["complex_code"]), w["last_number"]
        if last == 0:
            continue
        # 1) Anything filed after the last number we read?
        after = [n for n in range(last + 1, last + 11) if ej.case(f"{w['series']}/{n}/{w['year']}", cid)]
        # 2) Re-read the window independently with a loose keyword check, not our matcher.
        found, loose_hits, missing = 0, [], []
        for n in range(max(1, last - 59), last + 1):
            number = f"{w['series']}/{n}/{w['year']}"
            rec = ej.case(number, cid)
            if not rec:
                missing.append(n)
                continue
            found += 1
            text = f"{rec.get('complainant') or ''} | {rec.get('respondent') or ''}"
            if LOOSE.search(text):
                loose_hits.append({"number": number, "parties": text[:150], "stored": number in stored})
        missed = [h for h in loose_hits if not h["stored"]]
        report["series"].append({"commission": court["complex_name"], "series": w["series"], "last_read": last,
                                 "filed_after_last": after, "window_found": found, "window_missing": missing,
                                 "keyword_hits": len(loose_hits), "stored_of_those": len(loose_hits) - len(missed)})
        report["missed"] += missed
        print(f"{court['complex_name']:36} {w['series']:15} last {last}: {found}/60 exist, missing {missing}, "
              f"after last {after}; keyword hits {len(loose_hits)}, stored {len(loose_hits) - len(missed)}", flush=True)
        for m in missed:
            print(f"    NOT STORED: {m['number']}  {m['parties']}", flush=True)
    # 3) State Commission appeals: which numbering is used in 2026?
    for series in ("SC/8/A", "SC/8/FA", "SC/8/RP", "SC/8/EA", "SC/8/CC"):
        hits = [n for n in (1, 5, 10, 25, 50, 100) if ej.case(f"{series}/{n}/2026", 11080000)]
        report["extra_probe"][series] = hits
        print(f"State Commission {series}/n/2026 exists for n in {hits}", flush=True)
    json.dump(report, open("../research_notes/verify_ejagriti.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print(f"done, {ej.requests} requests")


async def highcourt() -> None:
    from .highcourt import HighCourt
    from .matching import role
    db = DB()
    conf: dict[str, list[str]] = {}
    for t in db.select("watch_terms", "select=company_id,confirm"):
        conf.setdefault(t["company_id"], []).extend(t["confirm"])
    read = db.select("cases", "select=*&forum=eq.high_court&detail_fetched_at=not.is.null&status=neq.not_ours")
    sample = random.Random(11).sample(read, min(10, len(read)))
    court = db.select("courts", "select=*&forum=eq.high_court")[0]
    async with HighCourt() as hc:
        mism = 0
        for c in sample:
            d, html = await hc.detail(c["cnr"])
            pets, resps = [p.name for p in d.petitioners], [p.name for p in d.respondents]
            now = {"filed": str(d.filing_date) if d.filing_date else None, "registered": str(d.registration_date) if d.registration_date else None,
                   "next": str(d.next_hearing_date) if d.next_hearing_date else None, "type": d.case_type or None,
                   "side": role(pets, resps, conf[c["company_id"]])}
            was = {"filed": c["filing_date"], "registered": c["registration_date"], "next": c["next_hearing_date"],
                   "type": c["case_type"], "side": c["company_role"]}
            diff = {k: (was[k], now[k]) for k in now if was[k] != now[k]}
            mism += bool(diff)
            print(f"{c['cnr']} {c['case_type']:6} {c['company_role']:10} next {c['next_hearing_date']} | {(c['title'] or '')[:50]} | "
                  f"{'MATCHES eCourts' if not diff else 'DIFFERS ' + str(diff)}", flush=True)
        print(f"{len(sample) - mism} of {len(sample)} re-read cases identical", flush=True)
        # Search stability: same search twice.
        t = next(t for t in db.select("watch_terms", "select=*&active=eq.true") if t["term"] == "Lombard")
        a = {r.cnr_number for r in await hc.search(court["state_code"], court["complex_code"], t["term"], 2026)}
        b = {r.cnr_number for r in await hc.search(court["state_code"], court["complex_code"], t["term"], 2026)}
        stored = {c["cnr"] for c in db.select("cases", f"select=cnr&forum=eq.high_court&company_id=eq.{t['company_id']}")}
        print(f"Lombard search twice: {len(a)} and {len(b)} cases, identical {a == b}; all stored: {a <= stored | {x for x in a if False}} "
              f"(not stored: {len(a - stored)})", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "ejagriti":
        ejagriti()
    else:
        asyncio.run(highcourt())
