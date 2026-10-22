"""Search courts for any party across all of India, a state or one district, on request from the dashboard.

    python -m haazir.lookup run              # every waiting search, oldest first
    python -m haazir.lookup run --id <uuid>  # one search

A search lists every case the courts show for the name: district courts (each court complex, each
year), the High Court (each bench, each year) and consumer commissions, in one state, several states or
all of India. Results are kept apart from the cases Haazir watches. Each search keeps its list of court
searches (lookup_tasks), so a run that runs out of time stops and the next run carries on where it left off.

District courts are taken one court from each state in turn, a few at a time, so results from every state
arrive together. Each state's district search stops at the search's district_cap ("Load more" raises it).
High Courts and consumer commissions are searched in full.
"""

import argparse
import asyncio
import re
import sys
import time
from datetime import datetime, timezone

from bharat_courts.courts import get_court

from .consumer import EJagriti
from .db import DB
from .ecourts import Blocked, ECourts
from .highcourt import HighCourt
from .matching import is_company, normalise

DISTRICT_WORKERS = 3   # eCourts sessions searching at once; each still waits between its own requests
HC_WORKERS = 2

# eCourts state name -> High Court codes in the court library. One High Court can serve several states.
STATE_HC = {
    "Andhra Pradesh": ["andhra"], "Assam": ["gauhati"], "Arunachal Pradesh": ["gauhati"], "Nagaland": ["gauhati"], "Mizoram": ["gauhati"],
    "Bihar": ["patna"], "Chandigarh": ["punjab"], "Punjab": ["punjab"], "Haryana": ["punjab"], "Chhattisgarh": ["chhattisgarh"],
    "Delhi": ["delhi"], "Goa": ["bombay-goa"], "Gujarat": ["gujarat"], "Himachal Pradesh": ["himachal"], "Jammu and Kashmir": ["jammu"],
    "Ladakh": ["jammu"], "Jharkhand": ["jharkhand"], "Karnataka": ["karnataka"], "Kerala": ["kerala"], "Lakshadweep": ["kerala"],
    "Madhya Pradesh": ["mp"], "Maharashtra": ["bombay", "bombay-nagpur", "bombay-aurangabad"], "Manipur": ["manipur"],
    "Meghalaya": ["meghalaya"], "Odisha": ["orissa"], "Rajasthan": ["rajasthan"], "Sikkim": ["sikkim"], "Tamil Nadu": ["madras"],
    "Puducherry": ["madras"], "Telangana": ["telangana"], "Tripura": ["tripura"], "Uttar Pradesh": ["allahabad", "allahabad-lucknow"],
    "Uttarakhand": ["uttarakhand"], "West Bengal": ["calcutta"], "Andaman and Nicobar": ["calcutta"],
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def side_of(term: str, petitioner: str, respondent: str, case_type: str = "") -> str:
    """Which side the searched name is on, as far as the court's list shows it.

    The list sometimes shows only the matched party ("SBI General Insurance vs"), and then its position says
    nothing about the side: "unknown". A motor accident claim names the insurer as respondent by law."""
    on_r, on_p = is_company(respondent or "", [term]), is_company(petitioner or "", [term])
    if (on_r or on_p) and re.search(r"m\.?\s?a\.?\s?c|motor", case_type or "", re.I) and re.search(r"insur|assur", f"{petitioner} {respondent}", re.I):
        return "respondent"
    if not (petitioner or "").strip() or not (respondent or "").strip():
        return "unknown"
    return "respondent" if on_r else "petitioner" if on_p else "unknown"


def year_of(number: str | None) -> int | None:
    m = re.search(r"(\d{4})\s*$", number or "")
    return int(m.group(1)) if m else None


class Job:
    def __init__(self, db: DB, row: dict) -> None:
        self.db, self.row = db, row
        self.term = row["party"].strip()
        # eCourts matches inside words, so "SBI" also returns "Jasbir". Short one-word names keep only whole-word hits.
        n = normalise(self.term)
        self.short = " " not in n and len(n) <= 5
        self.progress = {"done": 0, "total": 0, "found": 0, "failed": 0, "now": "", **(row.get("progress") or {})}
        self.last_save = 0.0
        self.stopped = False

    def save_progress(self, force: bool = False) -> None:
        if force or time.time() - self.last_save > 8:
            # Only while it is still ours to run: a person may have stopped it from the dashboard.
            saved = self.db.update("lookups", f"id=eq.{self.row['id']}&status=eq.running", {"progress": self.progress, "heartbeat": now()})
            self.stopped = self.stopped or not saved
            self.last_save = time.time()

    def keep(self, rows: list[dict], state: str | None) -> int:
        rows = [{**r, "lookup_id": self.row["id"], "state": state} for r in rows if not self.short or r["side"] != "unknown"]
        n = 0
        # Count only rows new to this search: a case can appear in two courts' lists.
        for i in range(0, len(rows), 500):
            n += len(self.db.insert("lookup_results", rows[i:i + 500], upsert_on="lookup_id,cnr", ignore_duplicates=True))
        self.progress["found"] += n
        return n

    def finish_task(self, task: dict, found: int, failed: bool) -> None:
        self.db.update("lookup_tasks", f"id=eq.{task['id']}", {"done": True, "found": found})
        self.progress["done"] += 1
        self.progress["failed"] += int(failed)

    async def district(self, ec: ECourts, task: dict) -> int:
        t, year = task["payload"], task["year"]
        self.progress["now"] = f"{t['name']}, {t['district']} · {year}"
        self.save_progress()
        failed = False
        try:
            res = await ec.search(t["court"], self.term, year, "Pending")
        except Blocked:
            raise
        except Exception as exc:  # one court failing (layout, timeout) must not stop the rest
            failed, res = True, []
            print(f"  failed {t['name']} {year}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        n = self.keep([{"forum": "district", "court": t["name"], "district": t["district"], "cnr": r.cnr_number, "case_number": r.case_number or None,
                        "case_type": r.case_type or None, "year": year, "petitioner": r.petitioner, "respondent": r.respondent,
                        "side": side_of(self.term, r.petitioner, r.respondent, r.case_type)} for r in res if r.cnr_number], t.get("state"))
        self.finish_task(task, n, failed)
        print(f"  {t['name']} ({t['district']}) · {year}: {len(res)}", flush=True)
        return n

    async def high_court(self, hc: HighCourt, task: dict) -> None:
        t, year = task["payload"], task["year"]
        self.progress["now"] = f"{t['name']} · {year}"
        self.save_progress()
        failed = False
        try:
            res = await hc.search(t["code"], t["bench"], self.term, year)
        except Exception as exc:
            failed, res = True, []
            print(f"  failed {t['name']} {year}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        n = self.keep([{"forum": "high_court", "court": t["name"], "district": None, "cnr": r.cnr_number,
                        "case_number": f"{r.case_type}/{r.case_number}" if r.case_type else r.case_number, "case_type": r.case_type or None,
                        "year": year, "petitioner": r.petitioner, "respondent": r.respondent,
                        "side": side_of(self.term, r.petitioner, r.respondent, r.case_type)} for r in res if r.cnr_number], t.get("state"))
        self.finish_task(task, n, failed)
        print(f"  {t['name']} · {year}: {len(res)}", flush=True)

    def consumer(self, ej: EJagriti, task: dict) -> None:
        t, year = task["payload"], task["year"]
        self.progress["now"] = f"{t['name']} · {year}"
        self.save_progress()
        rows, page, failed = [], 0, False
        try:
            while page < 20:
                body = {"commissionId": t["cid"], "page": page, "size": 30, "fromDate": f"{year}-01-01", "toDate": f"{year}-12-31",
                        "dateRequestType": 1, "serchType": 8, "serchTypeValue": self.term, "orderType": 2}
                data = ej._call("POST", "/case/caseFilingService/v2/getCaseDetailsBySearchType", json=body).get("data") or []
                rows += data
                if len(data) < 30:
                    break
                page += 1
        except Exception as exc:
            failed = True
            print(f"  failed {t['name']} {year}: {exc}", flush=True)
        out = []
        for r in rows:
            resp = [r.get("respondentName") or ""] + [x.get("additional_respondent_name", "") for x in r.get("additionalRespondantList") or []]
            comp = [r.get("complainantName") or ""] + [x.get("additional_complainant_name", "") for x in r.get("additionalComplainantList") or []]
            on_r, on_c = any(is_company(n, [self.term]) for n in resp), any(is_company(n, [self.term]) for n in comp)
            if r.get("caseNumber"):
                out.append({"forum": "consumer", "court": t["name"], "district": None, "cnr": r["caseNumber"], "case_number": r["caseNumber"],
                            "case_type": "Consumer complaint", "year": year_of(r["caseNumber"]) or year,
                            "petitioner": (r.get("complainantName") or "").strip(), "respondent": (r.get("respondentName") or "").strip(),
                            "side": "respondent" if on_r else "petitioner" if on_c else "unknown"})
        n = self.keep(out, t.get("state"))
        self.finish_task(task, n, failed)
        print(f"  {t['name']} · {year}: {len(rows)}", flush=True)


async def area(ec: ECourts, ej: EJagriti, row: dict) -> dict:
    """Every court in a search's area (one district, one state, or all of India when state_code is "all"):
    district court complexes, High Court benches and consumer commissions, each with its state."""
    dist, forums = row.get("district_code"), row["forums"]
    whole_india = row["state_code"] == "all"
    picked = row.get("state_codes") or ([row["state_code"]] if not whole_india else [])
    names = [n.strip() for n in (row["state_name"] or "").split(",")] if len(picked) > 1 else [row["state_name"]]
    wanted = {normalise(n) for n in names}
    out: dict = {"district": [], "high_court": [], "consumer": []}
    if "district" in forums:
        listed = await ec.client.list_states()
        states = listed if whole_india else ({k: v for k, v in listed.items() if normalise(v) in wanted} or dict(zip(picked, names)))
        seen: set[tuple[str, str]] = set()
        for state, sname in sorted(states.items(), key=lambda x: x[1]):
            sname = sname.strip()
            try:
                districts = await ec.client.list_districts(state)
            except Exception as exc:
                print(f"  districts of {sname} not listed: {exc}", flush=True)
                continue
            for dc, dname in districts.items():
                if dist and dc != dist:
                    continue
                try:
                    complexes = await ec.list_complexes(state, dc)
                except Blocked:
                    raise
                except Exception as exc:
                    print(f"  courts of {dname}, {sname} not listed: {exc}", flush=True)
                    continue
                for value, cname in complexes.items():
                    code, ests, flag = (value.split("@") + ["", ""])[:3]
                    # One code is searched once. Two districts can list complexes with the same name but
                    # different codes (Karkardooma for East and North East): those hold different cases.
                    if (state, code) in seen or re.search(r"test|railway", cname, re.I):
                        continue
                    seen.add((state, code))
                    court = {"state_code": state, "dist_code": dc, "complex_code": code, "est_codes": ests, "needs_est": flag == "Y"}
                    out["district"].append({"court": court, "name": cname.strip(), "district": dname.strip(), "state": sname})
    if "high_court" in forums:
        async with HighCourt() as hc:
            benches_seen: set[tuple[str, str]] = set()  # Bombay's benches share one portal list
            pairs = ([(s, c) for s, codes in STATE_HC.items() for c in codes] if whole_india
                     else [(n, c) for n in names for c in STATE_HC.get(n, [])])
            for sname, code in pairs:
                court = get_court(code)
                try:
                    benches = await hc.client.list_benches(court)
                except Exception as exc:
                    print(f"  benches of {court.name} not listed: {exc}", flush=True)
                    continue
                for b, bn in benches.items():
                    if (court.state_code, b) not in benches_seen:
                        benches_seen.add((court.state_code, b))
                        out["high_court"].append({"code": code, "bench": b, "name": f"{court.name.split(',')[0]}, {bn}", "state": sname})
    if "consumer" in forums:
        for st in ej.state_commissions():
            sname = st["commissionNameEn"].strip().title()
            if not whole_india and normalise(st["commissionNameEn"]) not in wanted:
                continue
            if not dist:
                out["consumer"].append({"cid": st["commissionId"], "name": f"State Commission, {sname}", "is_state": True,
                                        "st_id": st["commissionId"], "state": sname})
            for d in ej.district_commissions(st["commissionId"]):
                if not dist or normalise(row.get("district_name") or "") in normalise(d["commissionNameEn"]):
                    out["consumer"].append({"cid": d["commissionId"], "name": f"District Commission, {d['commissionNameEn']}", "is_state": False,
                                            "st_id": st["commissionId"], "state": sname})
    return out


def district_found(db: DB, lookup_id: str) -> dict[str, int]:
    """District court cases already found per state in this search."""
    out: dict[str, int] = {}
    for r in db.select("lookup_results", f"select=state&lookup_id=eq.{lookup_id}&forum=eq.district"):
        out[r["state"] or ""] = out.get(r["state"] or "", 0) + 1
    return out


def tasks_of(db: DB, lookup_id: str) -> list[dict]:
    return db.select("lookup_tasks", f"select=*&lookup_id=eq.{lookup_id}&order=seq")


async def plan(db: DB, row: dict) -> list[dict]:
    """The search's court searches, listed once: High Courts first (few, and most asked for), then consumer
    commissions, then district courts state by state. Kept in lookup_tasks so a long search can resume."""
    async with ECourts() as ec:
        courts = await area(ec, EJagriti(), row)
    rows, seq = [], 0
    years = sorted(row["years"], reverse=True)
    for forum in ("high_court", "consumer", "district"):
        # One court from each state in turn, so every state's cases start arriving at once.
        by_state: dict[str, list[dict]] = {}
        for c in courts[forum]:
            by_state.setdefault(c.get("state") or "", []).append(c)
        queues = list(by_state.values())
        for i in range(max((len(q) for q in queues), default=0)):
            for q in queues:
                if i < len(q):
                    for y in years:
                        rows.append({"lookup_id": row["id"], "seq": seq, "forum": forum, "year": y, "payload": q[i]})
                        seq += 1
    for i in range(0, len(rows), 500):
        db.insert("lookup_tasks", rows[i:i + 500], upsert_on="lookup_id,seq", ignore_duplicates=True)
    return tasks_of(db, row["id"])


async def run_one(db: DB, row: dict, deadline: float) -> None:
    """Run a search until it is done or the time runs out; then it waits in the queue and the next run carries on."""
    # Start only if nobody stopped it in the meantime.
    if not db.update("lookups", f"id=eq.{row['id']}&status=in.(queued,running)", {"status": "running", "started_at": row.get("started_at") or now(), "error": None, "heartbeat": now()}):
        return
    job = Job(db, row)
    try:
        tasks = tasks_of(db, row["id"])
        if not tasks:
            db.http.delete(f"/lookup_results?lookup_id=eq.{row['id']}")  # a fresh start
            job.progress.update({"done": 0, "found": 0, "failed": 0, "now": "Listing the courts to search"})
            job.save_progress(force=True)
            tasks = await plan(db, row)
            print(f"{row['party']} · {row['state_name']} {row.get('district_name') or ''}: {len(tasks)} searches", flush=True)
        job.progress["total"] = len(tasks)
        job.progress["done"] = sum(1 for t in tasks if t["done"])
        job.progress["courts"] = len({(t["forum"], str(t["payload"].get("name")), str(t["payload"].get("district")), str(t["payload"].get("state")))
                                      for t in tasks})
        job.save_progress(force=True)
        pending = [t for t in tasks if not t["done"]]
        hc_tasks = [t for t in pending if t["forum"] == "high_court"]
        cons = [t for t in pending if t["forum"] == "consumer"]
        dist_tasks = [t for t in pending if t["forum"] == "district"]
        cap = int(row.get("district_cap") or 500)
        found = district_found(db, row["id"])

        async def hc_worker(queue: list[dict]) -> None:
            async with HighCourt() as hc:
                while queue and time.time() < deadline and not job.stopped:
                    await job.high_court(hc, queue.pop(0))

        async def dc_worker(queue: list[dict]) -> None:
            async with ECourts() as ec:
                while queue and time.time() < deadline and not job.stopped:
                    t = queue.pop(0)
                    st = t["payload"].get("state") or ""
                    if found.get(st, 0) >= cap:
                        continue  # this state has its share for now; "Load more" carries on from here
                    # Count only this search's own finds: other workers add to the shared total at the same time.
                    found[st] = found.get(st, 0) + await job.district(ec, t)

        async def consumer_worker(queue: list[dict]) -> None:
            ej = EJagriti()
            while queue and time.time() < deadline and not job.stopped:
                await asyncio.to_thread(job.consumer, ej, queue.pop(0))

        # High Courts, consumer commissions and district courts are different portals: search them side by side.
        jobs = [hc_worker(hc_tasks) for _ in range(min(HC_WORKERS, len(hc_tasks)))]
        jobs += [consumer_worker(cons)] if cons else []
        jobs += [dc_worker(dist_tasks) for _ in range(min(DISTRICT_WORKERS, len(dist_tasks)))]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        if any(isinstance(r, Blocked) for r in results):
            raise Blocked()
        for r in results:
            if isinstance(r, BaseException):
                raise r
        if job.stopped:
            print(f"{row['party']}: stopped from the dashboard", flush=True)
            return
        found = district_found(db, row["id"])
        open_tasks = [t for t in tasks_of(db, row["id"]) if not t["done"]]
        held = [t for t in open_tasks if t["forum"] == "district" and found.get(t["payload"].get("state") or "", 0) >= cap]
        job.progress["more"] = len(held)
        job.progress["capped"] = sorted({t["payload"].get("state") or "" for t in held})
        left = len(open_tasks) - len(held)
        if left:
            # Out of time: back in the queue; the next run (on demand or with a scheduled scan) carries on.
            job.progress["now"] = f"Paused with {left} court searches left; it carries on with the next run"
            db.update("lookups", f"id=eq.{row['id']}&status=eq.running", {"status": "queued", "progress": job.progress})
            return
        job.progress["now"] = ""
        if row["state_code"] == "all":
            # Watching an all-India search adds only the courts where cases were found.
            summary = db._check(db.http.post("/rpc/lookup_summary", json={"lid": row["id"]})) or {}
            job.progress["courts"] = len(summary.get("courts") or [])
        db.update("lookups", f"id=eq.{row['id']}&status=eq.running", {"status": "done", "finished_at": now(), "progress": job.progress})
    except Blocked:
        job.progress["now"] = "eCourts paused the search for a while; it carries on with the next run"
        db.update("lookups", f"id=eq.{row['id']}&status=eq.running", {"status": "queued", "progress": job.progress})
    except Exception as exc:
        db.update("lookups", f"id=eq.{row['id']}&status=eq.running", {"status": "failed", "finished_at": now(), "progress": job.progress,
                                                   "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--id", default="")
    r.add_argument("--max-minutes", type=int, default=150, help="stop starting new court searches after this long")
    n = sub.add_parser("next", help="print what the next run should carry on with: the search id, 'all', or nothing")
    n.add_argument("--id", default="")
    args = ap.parse_args()
    db = DB()
    if args.cmd == "next":
        # A search paused for time goes back to "queued"; the workflow starts itself again with this answer.
        if args.id and db.select("lookups", f"select=id&id=eq.{args.id}&status=eq.queued"):
            print(args.id)
        elif db.select("lookups", "select=id&status=eq.queued&limit=1"):
            print("all")
        return 0
    deadline = time.time() + args.max_minutes * 60
    tried: set[str] = set()  # a search paused for time is not started again in the same run
    while time.time() < deadline:
        # Waiting searches, and ones whose worker died (no heartbeat for 30 minutes). A search running elsewhere
        # (fresh heartbeat, or none at all) is left to whoever runs it.
        stale = datetime.fromtimestamp(time.time() - 1800, timezone.utc).isoformat().replace("+00:00", "Z")
        q = f"select=*&id=eq.{args.id}" if args.id else f"select=*&or=(status.eq.queued,and(status.eq.running,heartbeat.lt.{stale}))&order=created_at"
        rows = [x for x in db.http.get(f"/lookups?{q}").json() if x["id"] not in tried]
        if not rows:
            break
        tried.add(rows[0]["id"])
        asyncio.run(run_one(db, rows[0], deadline))
    return 0


if __name__ == "__main__":
    sys.exit(main())
