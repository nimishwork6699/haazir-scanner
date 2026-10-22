"""Find one case by its CNR, straight from eCourts, on request from the dashboard (Search courts -> Find one case).

    python -m haazir.find run            # every waiting request
    python -m haazir.find run --id <id>  # one request

A CNR is the 16-character number printed on every order ("DLSW010122612026"). Letters 3-4 are "HC" for a High
Court; anything else is a district court. The case as the court shows it is saved on the request; adding it to
Our cases is a separate step a person takes on the dashboard.
"""

import argparse
import asyncio
import re
import sys
from datetime import datetime, timezone

from .db import DB
from .ecourts import ECourts
from .highcourt import HighCourt
from .classify import categorize

CNR = re.compile(r"^[A-Z]{4}[0-9]{12}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso(d) -> str | None:
    return d.isoformat() if d else None


def case_fields(d, cnr: str) -> dict:
    """The case as the court page shows it, in the same columns the scanner fills for Our cases."""
    pets = [{"name": p.name, "advocate": p.advocate} for p in d.petitioners]
    resps = [{"name": p.name, "advocate": p.advocate} for p in d.respondents]
    acts = [{"act": a.act, "sections": a.sections} for a in d.acts]
    # "MACT - M.A.C.T." + "764/2026" -> "MACT/764/2026", the way the rest of Haazir writes case numbers.
    short = (d.case_type or "").split(" - ")[0].strip()
    reg = (d.registration_number or "").strip()
    # High Courts print the type inside the number already ("CMA /4189/2026").
    number = re.sub(r"\s*/\s*", "/", reg) if re.match(r"[A-Za-z]", reg) else f"{short}/{reg}".strip("/") if reg else None
    return {
        "cnr": cnr,
        "forum": "high_court" if cnr[2:4] == "HC" else "district",
        "case_type": d.case_type or None,
        "case_number": number,
        "filing_number": d.filing_number or None,
        "filing_date": iso(d.filing_date),
        "registration_number": d.registration_number or None,
        "registration_date": iso(d.registration_date),
        "first_hearing_date": iso(d.first_hearing_date),
        "next_hearing_date": iso(d.next_hearing_date),
        "decision_date": iso(d.decision_date),
        "stage": d.case_stage or None,
        "court_judge": d.court_number_and_judge or d.court_name or None,
        "court_name": d.court_name or None,
        "petitioners": pets,
        "respondents": resps,
        "petitioner_advocate": (pets[0]["advocate"] or None) if pets else None,
        "acts": acts,
        "title": f"{pets[0]['name'] if pets else ''} vs {resps[0]['name'] if resps else ''}".strip(" vs") or None,
        "category": categorize(d.case_type, " ".join(f"{a['act']} {a['sections']}" for a in acts)),
        "hearings": [{"hearing_date": iso(h.hearing_date), "business_date": iso(h.business_date), "purpose": h.purpose or None,
                      "judge": h.judge or None} for h in d.history][-30:],
    }


async def fetch(cnr: str) -> dict:
    if cnr[2:4] == "HC":
        async with HighCourt() as hc:
            d, _ = await hc.detail(cnr)
    else:
        async with ECourts() as ec:
            d = await ec.detail(cnr)
    if not d or not (d.petitioners or d.respondents or d.case_type):
        raise LookupError("The courts have no case with this CNR")
    return case_fields(d, cnr)


def run_one(db: DB, row: dict) -> None:
    if not db.update("case_finds", f"id=eq.{row['id']}&status=in.(queued,running)", {"status": "running"}):
        return
    try:
        result = asyncio.run(fetch(row["cnr"]))
        known = db.select("cases", f"select=id&cnr=eq.{row['cnr']}")
        db.update("case_finds", f"id=eq.{row['id']}", {"status": "done", "result": result, "finished_at": now(), "error": None,
                                                       **({"case_id": known[0]["id"]} if known else {})})
        print(f"{row['cnr']}: {result.get('title')}", flush=True)
    except Exception as exc:
        msg = str(exc) if isinstance(exc, LookupError) else f"Could not read the case from the court site ({type(exc).__name__}). Try again in a while."
        db.update("case_finds", f"id=eq.{row['id']}", {"status": "failed", "error": msg[:300], "finished_at": now()})
        print(f"{row['cnr']}: failed {type(exc).__name__}: {str(exc)[:200]}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--id", default="")
    args = ap.parse_args()
    db = DB()
    q = f"select=*&id=eq.{args.id}" if args.id else "select=*&status=in.(queued,running)&order=created_at&limit=50"
    for row in db.select("case_finds", q):
        run_one(db, row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
