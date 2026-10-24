"""Weekly quality check: re-read a random sample of stored cases from each source and compare.

A stored value that differs from the court's current record is either an update the next scan will
pick up (the court posted a new hearing date) or a reading error. Both are counted and reported on
Telegram and in scan_runs, so a silent fault in any source shows up within a week.

    python -m haazir.quality                 # 8 district, 4 High Court, 4 consumer cases
"""

import asyncio
import random
import sys
from datetime import date, datetime, timezone

from . import telegram
from .consumer import EJagriti, _date
from .db import DB
from .ecourts import ECourts
from .highcourt import HighCourt
from .matching import role


def iso(d) -> str | None:
    return d.isoformat() if d else None


def compare(stored: dict, live: dict) -> tuple[list[str], list[str]]:
    """(errors, updates). A newer next hearing date is an update, not an error."""
    errors, updates = [], []
    for k, v in live.items():
        s = stored.get(k)
        if v == s or v is None:
            continue
        if k == "next_hearing_date" and s and v and v > s:
            updates.append(f"{k} {s} -> {v}")
        else:
            errors.append(f"{k}: stored {s}, court {v}")
    return errors, updates


async def run(n_district: int, n_hc: int, n_consumer: int) -> dict:
    db = DB()
    rng = random.Random(date.today().isoformat())
    confirm: dict[str, list[str]] = {}
    for t in db.select("watch_terms", "select=company_id,confirm"):
        confirm.setdefault(t["company_id"], []).extend(t["confirm"])
    cols = "select=id,cnr,company_id,case_number,court_id,filing_date,next_hearing_date,case_type,company_role,forum"
    pick = lambda forum, n: rng.sample(c := db.select("cases", f"{cols}&forum=eq.{forum}&detail_fetched_at=not.is.null&status=neq.not_ours&company_role=neq.unknown"), min(n, len(c)))
    out = {"checked": 0, "errors": [], "updates": 0, "failed": 0}

    def record(case, live):
        errs, ups = compare(case, live)
        out["checked"] += 1
        out["updates"] += len(ups)
        out["errors"] += [f"{case['forum']} {case['case_number']}: {e}" for e in errs]

    async with ECourts() as ec:
        for c in pick("district", n_district):
            try:
                d = await ec.detail(c["cnr"])
            except Exception:
                out["failed"] += 1
                continue
            side = role([p.name for p in d.petitioners], [p.name for p in d.respondents], confirm.get(c["company_id"], []))
            record(c, {"filing_date": iso(d.filing_date), "next_hearing_date": iso(d.next_hearing_date),
                       "company_role": side if side != "unknown" else None})
    async with HighCourt() as hc:
        for c in pick("high_court", n_hc):
            try:
                d, _ = await hc.detail(c["cnr"])
            except Exception:
                out["failed"] += 1
                continue
            record(c, {"filing_date": iso(d.filing_date), "next_hearing_date": iso(d.next_hearing_date), "case_type": d.case_type or None})
    ej = EJagriti()
    courts = {x["id"]: x for x in db.select("courts", "select=id,complex_code&forum=eq.consumer")}
    for c in pick("consumer", n_consumer):
        rec = ej.case(c["case_number"], int(courts[c["court_id"]]["complex_code"])) if c["court_id"] in courts else None
        if not rec:
            out["failed"] += 1
            continue
        record(c, {"filing_date": _date(rec.get("caseFilingDate")), "next_hearing_date": _date(rec.get("dateOfNextearing"))})
    return out


def main() -> int:
    db = DB()
    row = db.insert("scan_runs", {"trigger": "quality"}, returning="*")[0]
    try:
        r = asyncio.run(run(8, 4, 4))
        status = "ok" if not r["errors"] else "partial"
    except Exception as exc:
        r, status = {"checked": 0, "errors": [f"{type(exc).__name__}: {exc}"], "updates": 0, "failed": 0}, "failed"
    parts = [f"{r['checked'] - len(r['errors'])} match"]
    if r["errors"]:
        parts.append(f"{len(r['errors'])} differ")
    if r["updates"]:
        parts.append(f"{r['updates']} have a newer hearing date (picked up by the next scan)")
    if r["failed"]:
        parts.append(f"{r['failed']} could not be read")
    msg = f"<b>Haazir weekly quality check</b>\n{r['checked']} cases re-read from the courts: {', '.join(parts)}."
    if r["errors"]:
        msg += "\n" + "\n".join(f"• {e}" for e in r["errors"][:6])
    db.update("scan_runs", f"id=eq.{row['id']}", {"status": status, "finished_at": datetime.now(timezone.utc).isoformat(),
                                                  "details_fetched": r["checked"], "log": [{"at": datetime.now(timezone.utc).isoformat(),
                                                  "level": "info" if status == "ok" else "warn", "msg": f"quality: {r}"}]})
    ops = next((x["value"] for x in db.select("settings", "select=value&key=eq.ops_telegram_chat_id")), None)
    if ops:
        try:
            telegram.send(int(ops), msg)
        except Exception as exc:
            print("telegram failed:", exc)
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
