"""Search courts for any party across all of India, some states, one state or one district, on request from the dashboard.

    python -m haazir.lookup plan [--id <uuid>]          # list the search's court searches; prints how many workers to start
    python -m haazir.lookup work --id <uuid> --worker 3  # one worker: takes court searches until none are left
    python -m haazir.lookup kick                         # start cloud runs for waiting searches (and ones whose workers died)
    python -m haazir.lookup next --id <uuid>             # what a finished run should carry on with
    python -m haazir.lookup run [--id <uuid>]            # plan and work in one process (no parallel workers)

A search is one list of court searches (lookup_tasks): each district court establishment for each year, each
High Court bench, each consumer commission. Up to ten cloud workers (lookup.yml) share that list: each claims the
next court search, does it and claims the next, so all of them stay busy whether the search covers all of India or
one district. A worker that dies leaves its claim, which is free again after 15 minutes.

India's district court complexes are listed once and kept in court_index. A state not listed yet (or listed more
than two weeks ago) becomes an "index" task: a worker lists it, adds its court searches, and the others are already
searching. Cases Haazir already has for the name are put in the results at once, so a search never opens empty.

eCourts blocks one address that asks too much: each worker keeps to two sessions and the usual delay between its
requests; separate cloud runners have separate addresses. Each state's district search stops at district_cap
("Load more" raises it). High Courts and consumer commissions are searched in full.
"""

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from bharat_courts.courts import get_court

from .consumer import EJagriti
from .db import DB
from .ecourts import Blocked, ECourts
from .highcourt import HighCourt
from .matching import is_company, normalise

MAX_WORKERS = 10       # cloud runners per search (lookup.yml), each with its own address
LANES = 2              # court sessions per runner at once; eCourts blocked an address at about six
INDEX_DAYS = 14        # a state's list of court complexes is listed again after this long
STALE_MINUTES = 30     # a running search with no heartbeat for this long has lost its workers

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


def ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


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


def counts(db: DB, lookup_id: str) -> dict:
    """The search's progress, counted from the tables so ten workers never overwrite each other's numbers."""
    return db._check(db.http.post("/rpc/lookup_counts", json={"lid": lookup_id})) or {}


def state_found(db: DB, lookup_id: str, state: str) -> int:
    """District court cases this search has found in one state (for its district_cap)."""
    r = db.http.get(f"/lookup_results?select=id&lookup_id=eq.{lookup_id}&forum=eq.district&known=eq.false&state=eq.{quote(state)}",
                    headers={"Prefer": "count=exact", "Range": "0-0"})
    return int((r.headers.get("content-range") or "*/0").split("/")[-1] or 0)


# ---------------------------------------------------------------- the court list

def district_rows(entries: list[dict], years: list[int], lookup_id: str, seq0: int) -> list[dict]:
    """Court searches for district court complexes: one per establishment where the portal needs one picked
    (so ten workers can share even a single busy complex), one per complex otherwise, for each year."""
    rows, seq = [], seq0
    # Newest year first across every court, so the most recent cases come in first on an "All years" search.
    for y in years:
        for e in entries:
            ests = [x for x in (e["court"].get("est_codes") or "").split(",") if x] if e["court"].get("needs_est") else [""]
            for est in ests:
                court = {**e["court"], "est_codes": est} if est else e["court"]
                rows.append({"lookup_id": lookup_id, "seq": seq, "forum": "district", "year": y, "payload": {**e, "court": court}})
                seq += 1
    return rows


def mixed(entries: list[dict]) -> list[dict]:
    """One court from each state, district and complex in turn, so every place's cases start arriving at once."""
    by_state: dict[str, list[dict]] = {}
    for e in entries:
        by_state.setdefault(e.get("state") or "", []).append(e)
    queues, out = list(by_state.values()), []
    for i in range(max((len(q) for q in queues), default=0)):
        out += [q[i] for q in queues if i < len(q)]
    return out


def saved_courts(db: DB, state_code: str, dist: str | None) -> list[dict] | None:
    """A state's district court complexes as listed in the last two weeks, or None when it needs listing."""
    q = f"select=*&state_code=eq.{state_code}&listed_at=gt.{ago(INDEX_DAYS * 24 * 60)}" + (f"&dist_code=eq.{dist}" if dist else "")
    rows = db.select("court_index", q + "&order=state_code,dist_code,complex_code")
    if not rows:
        return None
    return [{"court": {"state_code": r["state_code"], "dist_code": r["dist_code"], "complex_code": r["complex_code"],
                       "est_codes": r["est_codes"], "needs_est": r["needs_est"]},
             "name": r["complex_name"], "district": r["district_name"], "state": r["state_name"]}
            for r in rows if not re.search(r"test|railway", r["complex_name"], re.I)]


async def list_state(db: DB, ec: ECourts, state_code: str, state_name: str, dist: str | None) -> list[dict]:
    """List a state's districts and court complexes on eCourts and keep them in court_index."""
    out, keep, seen = [], [], set()
    districts = await ec.client.list_districts(state_code)
    for dc, dname in districts.items():
        if dist and dc != dist:
            continue
        try:
            complexes = await ec.list_complexes(state_code, dc)
        except Blocked:
            raise
        except Exception as exc:
            print(f"  courts of {dname}, {state_name} not listed: {exc}", flush=True)
            continue
        for value, cname in complexes.items():
            code, ests, flag = (value.split("@") + ["", ""])[:3]
            keep.append({"state_code": state_code, "dist_code": dc, "complex_code": code, "state_name": state_name, "district_name": dname.strip(),
                         "complex_name": cname.strip(), "est_codes": ests, "needs_est": flag == "Y", "listed_at": now()})
            # One code is searched once. Two districts can list complexes with the same name but different codes
            # (Karkardooma for East and North East): those hold different cases.
            if code in seen or re.search(r"test|railway", cname, re.I):
                continue
            seen.add(code)
            out.append({"court": {"state_code": state_code, "dist_code": dc, "complex_code": code, "est_codes": ests, "needs_est": flag == "Y"},
                        "name": cname.strip(), "district": dname.strip(), "state": state_name})
    for i in range(0, len(keep), 500):
        db.insert("court_index", keep[i:i + 500], upsert_on="state_code,dist_code,complex_code")
    return out


# ---------------------------------------------------------------- planning

def wanted_names(row: dict) -> list[str]:
    picked = row.get("state_codes") or ([] if row["state_code"] == "all" else [row["state_code"]])
    return [n.strip() for n in (row["state_name"] or "").split(",")] if len(picked) > 1 else [row["state_name"]]


def add_known(db: DB, row: dict) -> int:
    """Cases Haazir already has for the name, in the search's area, forums and years: shown at once, marked known."""
    words = [w for w in normalise(row["party"]).split() if len(w) >= 2][:6]
    if not words:
        return 0
    cond = ",".join(f"search_text.imatch.\\m{re.escape(w)}" for w in words)
    forums = ",".join(f for f in row["forums"] if f in ("district", "high_court", "consumer"))
    cases = db.select("cases", f"select=id,cnr,case_number,case_type,forum,petitioners,respondents,company_role,registration_date,"
                               f"court:courts(state,district_name,complex_name)&and=({quote(cond)})&forum=in.({forums})"
                               f"&status=not.in.(disposed,not_ours)")
    whole = row["state_code"] == "all"
    states = {normalise(n) for n in wanted_names(row)}
    years = set(row["years"])
    out = []
    for c in cases:
        court = c.get("court") or {}
        state = court.get("state")
        if not whole and normalise(state or "") not in states:
            continue
        if row.get("district_name") and normalise(row["district_name"]) not in normalise(court.get("district_name") or ""):
            continue
        year = int(c["registration_date"][:4]) if c.get("registration_date") else year_of(c.get("case_number"))
        if year not in years or not c.get("cnr"):
            continue
        pet = (c.get("petitioners") or [{}])[0].get("name") or ""
        resp = (c.get("respondents") or [{}])[0].get("name") or ""
        out.append({"lookup_id": row["id"], "forum": c["forum"], "known": True, "cnr": c["cnr"], "case_number": c.get("case_number"),
                    "case_type": c.get("case_type"), "year": year, "petitioner": pet, "respondent": resp, "state": state,
                    "court": court.get("complex_name") or ("High Court" if c["forum"] == "high_court" else "Court"),
                    "district": court.get("district_name"),
                    "side": c["company_role"] if c.get("company_role") in ("respondent", "petitioner") else "unknown"})
    for i in range(0, len(out), 500):
        db.insert("lookup_results", out[i:i + 500], upsert_on="lookup_id,cnr", ignore_duplicates=True)
    return len(out)


async def area(db: DB, row: dict, list_missing: bool = False) -> dict:
    """Every court in a search's area: district court complexes (from court_index; a state not listed yet goes in
    "index", or is listed now with list_missing), High Court benches and consumer commissions, each with its state."""
    forums, dist = row["forums"], row.get("district_code")
    whole = row["state_code"] == "all"
    names = wanted_names(row)
    wanted = {normalise(n) for n in names}
    out: dict = {"index": [], "district": [], "high_court": [], "consumer": []}
    if "district" in forums:
        async with ECourts() as ec:
            listed = await ec.client.list_states()
            states = {k: v.strip() for k, v in listed.items() if whole or normalise(v) in wanted}
            if not states and not whole:
                states = dict(zip(row.get("state_codes") or [row["state_code"]], names))
            for code, sname in sorted(states.items(), key=lambda x: x[1]):
                courts = saved_courts(db, code, dist)
                if courts is None and list_missing:
                    courts = await list_state(db, ec, code, sname, dist)
                if courts is None:
                    out["index"].append({"state_code": code, "state": sname, "district_code": dist})
                else:
                    out["district"] += courts
    if "high_court" in forums:
        async with HighCourt() as client:
            seen: set[tuple[str, str]] = set()  # Bombay's benches share one portal list
            pairs = [(s, c) for s, codes in STATE_HC.items() for c in codes] if whole else [(n, c) for n in names for c in STATE_HC.get(n, [])]
            for sname, code in pairs:
                court = get_court(code)
                try:
                    benches = await client.client.list_benches(court)
                except Exception as exc:
                    print(f"  benches of {court.name} not listed: {exc}", flush=True)
                    continue
                for b, bn in benches.items():
                    if (court.state_code, b) not in seen:
                        seen.add((court.state_code, b))
                        out["high_court"].append({"code": code, "bench": b, "name": f"{court.name.split(',')[0]}, {bn}", "state": sname})
    if "consumer" in forums:
        ej = EJagriti()
        for st in ej.state_commissions():
            sname = st["commissionNameEn"].strip().title()
            if not whole and normalise(st["commissionNameEn"]) not in wanted:
                continue
            if not dist:
                out["consumer"].append({"cid": st["commissionId"], "name": f"State Commission, {sname}", "is_state": True,
                                        "st_id": st["commissionId"], "state": sname})
            for d in ej.district_commissions(st["commissionId"]):
                if not dist or normalise(row.get("district_name") or "") in normalise(d["commissionNameEn"]):
                    out["consumer"].append({"cid": d["commissionId"], "name": f"District Commission, {d['commissionNameEn']}", "is_state": False,
                                            "st_id": st["commissionId"], "state": sname})
    return out


def tasks_of(db: DB, lookup_id: str) -> list[dict]:
    return db.select("lookup_tasks", f"select=*&lookup_id=eq.{lookup_id}&order=seq")


async def plan(db: DB, row: dict) -> int:
    """Write the search's court searches: states still to be listed first (so listing starts at once), then High
    Courts, consumer commissions and district courts, one place from each state in turn. Returns how many."""
    lid = row["id"]
    years = sorted(row["years"], reverse=True)
    courts = await area(db, row)
    index, district, hc, cons = courts["index"], courts["district"], courts["high_court"], courts["consumer"]
    rows, seq = [], 0
    for p in index:
        rows.append({"lookup_id": lid, "seq": seq, "forum": "index", "year": years[0], "payload": p})
        seq += 1
    for forum, entries in (("high_court", hc), ("consumer", cons)):
        for e in mixed(entries):
            for y in years:
                rows.append({"lookup_id": lid, "seq": seq, "forum": forum, "year": y, "payload": e})
                seq += 1
    rows += district_rows(mixed(district), years, lid, seq)
    for i in range(0, len(rows), 500):
        db.insert("lookup_tasks", rows[i:i + 500], upsert_on="lookup_id,seq", ignore_duplicates=True)
    return len(rows)


def start(db: DB, row: dict) -> bool:
    """Mark a search running, unless someone stopped it meanwhile."""
    return bool(db.update("lookups", f"id=eq.{row['id']}&status=in.(queued,running)",
                          {"status": "running", "started_at": row.get("started_at") or now(), "error": None, "heartbeat": now()}))


async def prepare(db: DB, row: dict) -> int:
    """First run of a search: a fresh results list with the cases Haazir already knows, then its court searches.
    Returns how many court searches are open."""
    if not db.select("lookup_tasks", f"select=id&lookup_id=eq.{row['id']}&limit=1"):
        db.http.delete(f"/lookup_results?lookup_id=eq.{row['id']}")
        db.update("lookups", f"id=eq.{row['id']}", {"progress": {"now": "Getting the list of courts ready", "done": 0, "total": 0, "found": 0}})
        known = add_known(db, row)
        n = await plan(db, row)
        print(f"{row['party']} · {row['state_name']} {row.get('district_name') or ''}: {n} court searches, {known} cases already known", flush=True)
    c = counts(db, row["id"])
    db.update("lookups", f"id=eq.{row['id']}&status=eq.running",
              {"progress": {**c, "now": "Searching the courts"}, "heartbeat": now()})
    return int(c.get("open") or 0)


# ---------------------------------------------------------------- working

class Job:
    """One worker's view of a search: it does court searches and reports shared progress."""

    def __init__(self, db: DB, row: dict, worker: str) -> None:
        self.db, self.row, self.worker = db, row, worker
        self.term = row["party"].strip()
        # eCourts matches inside words, so "SBI" also returns "Jasbir". Short one-word names keep only whole-word hits.
        n = normalise(self.term)
        self.short = " " not in n and len(n) <= 5
        self.cap = int(row.get("district_cap") or 500)
        self.stopped = False
        self.blocked = False
        self.last_save = 0.0
        self.now_text = ""
        self.capped: dict[str, float] = {}  # state -> when it was last seen at its cap

    def claim(self, lane: str) -> dict | None:
        rows = self.db._check(self.db.http.post("/rpc/claim_lookup_task", json={"lid": self.row["id"], "worker": f"{self.worker}/{lane}"})) or []
        return rows[0] if rows else None

    def release(self, task: dict, **extra) -> None:
        self.db.update("lookup_tasks", f"id=eq.{task['id']}", {"claimed_by": None, "claimed_at": None, **extra})

    def report(self, force: bool = False) -> None:
        """Shared progress and heartbeat; finds out when a person stopped the search from the dashboard."""
        if not force and time.time() - self.last_save < 6:
            return
        c = counts(self.db, self.row["id"])
        listing = int(c.get("listing") or 0)
        now_text = f"Listing courts in {listing} state{'s' if listing != 1 else ''} while searching" if listing else self.now_text
        saved = self.db.update("lookups", f"id=eq.{self.row['id']}&status=eq.running",
                               {"progress": {**c, "now": now_text, "more": c.get("held") or 0}, "heartbeat": now()})
        self.stopped = self.stopped or not saved
        self.last_save = time.time()

    def keep(self, rows: list[dict], state: str | None) -> int:
        rows = [{**r, "lookup_id": self.row["id"], "state": state} for r in rows if not self.short or r["side"] != "unknown"]
        n = 0
        # Count only rows new to this search: a case can appear in two courts' lists.
        for i in range(0, len(rows), 500):
            n += len(self.db.insert("lookup_results", rows[i:i + 500], upsert_on="lookup_id,cnr", ignore_duplicates=True))
        return n

    def finish(self, task: dict, found: int, failed: bool) -> None:
        self.db.update("lookup_tasks", f"id=eq.{task['id']}", {"done": True, "found": found, "failed": failed})

    def at_cap(self, state: str) -> bool:
        if self.cap >= 100000:
            return False
        if time.time() - self.capped.get(state, 0) < 30:
            return True
        if state_found(self.db, self.row["id"], state) >= self.cap:
            self.capped[state] = time.time()
            return True
        return False

    async def index(self, ec: ECourts, task: dict) -> None:
        p = task["payload"]
        self.now_text = f"Listing courts in {p['state']}"
        entries = await list_state(self.db, ec, p["state_code"], p["state"], p.get("district_code"))
        years = sorted(self.row["years"], reverse=True)
        rows = district_rows(mixed(entries), years, self.row["id"], 1_000_000 + task["seq"] * 20_000)
        for i in range(0, len(rows), 500):
            self.db.insert("lookup_tasks", rows[i:i + 500], upsert_on="lookup_id,seq", ignore_duplicates=True)
        self.finish(task, 0, False)
        print(f"  listed {p['state']}: {len(entries)} court complexes, {len(rows)} court searches", flush=True)

    async def district(self, ec: ECourts, task: dict) -> None:
        t, year = task["payload"], task["year"]
        if self.at_cap(t.get("state") or ""):
            self.release(task, held=True)  # this state has its share for now; "Load more" frees it
            return
        self.now_text = f"{t['name']}, {t['district']} · {year}"
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
        self.finish(task, n, failed)
        print(f"  {t['name']} ({t['district']}) · {year}: {len(res)}", flush=True)

    async def high_court(self, hc: HighCourt, task: dict) -> None:
        t, year = task["payload"], task["year"]
        self.now_text = f"{t['name']} · {year}"
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
        self.finish(task, n, failed)
        print(f"  {t['name']} · {year}: {len(res)}", flush=True)

    def consumer(self, ej: EJagriti, task: dict) -> None:
        t, year = task["payload"], task["year"]
        self.now_text = f"{t['name']} · {year}"
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
        self.finish(task, n, failed)
        print(f"  {t['name']} · {year}: {len(rows)}", flush=True)

    async def lane(self, name: str, deadline: float) -> None:
        """Take court searches one after another until none are left, time runs out, or the search is stopped."""
        async with contextlib.AsyncExitStack() as stack:
            ec = hc = ej = None
            while time.time() < deadline and not self.stopped and not self.blocked:
                task = self.claim(name)
                if not task:
                    return
                try:
                    if task["forum"] in ("district", "index"):
                        ec = ec or await stack.enter_async_context(ECourts())
                        await (self.index(ec, task) if task["forum"] == "index" else self.district(ec, task))
                    elif task["forum"] == "high_court":
                        hc = hc or await stack.enter_async_context(HighCourt())
                        await self.high_court(hc, task)
                    else:
                        ej = ej or EJagriti()
                        await asyncio.to_thread(self.consumer, ej, task)
                except Blocked:
                    # eCourts is refusing this runner's address: give the court back and stop; the others carry on.
                    self.release(task)
                    self.blocked = True
                    print(f"  {self.worker}/{name}: eCourts refused this address; leaving the rest to the other workers", flush=True)
                except Exception as exc:
                    self.release(task)
                    print(f"  {self.worker}/{name}: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
                self.report()


def finish_if_done(db: DB, row: dict) -> None:
    """The last worker to find nothing left closes the search."""
    c = counts(db, row["id"])
    if int(c.get("open") or 0) or int(c.get("listing") or 0):
        return
    held = int(c.get("held") or 0)
    progress = {**c, "now": "", "more": held, "capped": c.get("capped") or []}
    if row["state_code"] == "all":
        # Watching an all-India search adds only the courts where cases were found.
        summary = db._check(db.http.post("/rpc/lookup_summary", json={"lid": row["id"]})) or {}
        progress["courts"] = len(summary.get("courts") or [])
    db.update("lookups", f"id=eq.{row['id']}&status=eq.running", {"status": "done", "finished_at": now(), "progress": progress})


async def work(db: DB, row: dict, worker: str, deadline: float, lanes: int = LANES) -> None:
    job = Job(db, row, worker)
    job.report(force=True)
    if job.stopped:
        return
    await asyncio.gather(*(job.lane(f"lane{i + 1}", deadline) for i in range(lanes)))
    if job.stopped:
        print(f"{row['party']}: stopped from the dashboard", flush=True)
        return
    job.report(force=True)
    finish_if_done(db, db.select("lookups", f"select=*&id=eq.{row['id']}")[0])


# ---------------------------------------------------------------- commands

def waiting(db: DB, lookup_id: str = "") -> list[dict]:
    """Searches to run: waiting ones, and running ones whose workers died (no heartbeat for half an hour).
    A search running elsewhere (fresh heartbeat, or none at all from older code) is left to whoever runs it."""
    if lookup_id:
        return db.select("lookups", f"select=*&id=eq.{lookup_id}&status=in.(queued,running)")
    return db.select("lookups", f"select=*&or=(status.eq.queued,and(status.eq.running,heartbeat.lt.{ago(STALE_MINUTES)}))&order=created_at")


def output(**kv) -> None:
    """Values for the next GitHub Actions job (how many workers to start), also printed for people."""
    path = os.environ.get("GITHUB_OUTPUT")
    for k, v in kv.items():
        print(f"{k}={v}", flush=True)
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{k}={v}\n")


def dispatch(lookup_id: str, chain: int = 0) -> bool:
    """Start lookup.yml for a search (plan, then up to ten workers), from inside a GitHub Actions run."""
    token, repo = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("  no GH_TOKEN/GITHUB_REPOSITORY: cannot start a cloud run from here", flush=True)
        return False
    r = httpx.post(f"https://api.github.com/repos/{repo}/actions/workflows/lookup.yml/dispatches", timeout=30,
                   headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                   json={"ref": "main", "inputs": {"lookup_id": lookup_id, "chain": str(chain)}})
    return r.status_code < 300


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--id", default="")
    w = sub.add_parser("work")
    w.add_argument("--id", required=True)
    w.add_argument("--worker", default="1")
    w.add_argument("--max-minutes", type=int, default=330, help="stop taking new court searches after this long")
    sub.add_parser("kick")
    n = sub.add_parser("next", help="print the search id if it still has court searches left, else nothing")
    n.add_argument("--id", default="")
    r = sub.add_parser("run")
    r.add_argument("--id", default="")
    r.add_argument("--max-minutes", type=int, default=150)
    args = ap.parse_args()
    db = DB()

    if args.cmd == "plan":
        rows = waiting(db, args.id)
        if not rows or not start(db, rows[0]):
            output(id="", workers="[]")
            return 0
        open_tasks = asyncio.run(prepare(db, rows[0]))
        k = max(1, min(MAX_WORKERS, open_tasks))
        output(id=rows[0]["id"], workers=json.dumps(list(range(1, k + 1)) if open_tasks else []))
        if not open_tasks:
            finish_if_done(db, rows[0])
        return 0

    if args.cmd == "work":
        rows = db.select("lookups", f"select=*&id=eq.{args.id}&status=eq.running")
        if rows:
            asyncio.run(work(db, rows[0], f"w{args.worker}", time.time() + args.max_minutes * 60))
        return 0

    if args.cmd == "kick":
        for row in waiting(db):
            ok = dispatch(row["id"])
            print(f"{'started' if ok else 'could not start'}: {row['party']} · {row['state_name']}", flush=True)
        return 0

    if args.cmd == "next":
        rows = db.select("lookups", f"select=id&id=eq.{args.id}&status=in.(queued,running)") if args.id else []
        if rows and int(counts(db, args.id).get("open") or 0):
            print(args.id)
        return 0

    # run: plan and work in this one process, one search after another (no parallel workers).
    deadline = time.time() + args.max_minutes * 60
    tried: set[str] = set()
    while time.time() < deadline:
        rows = [x for x in waiting(db, args.id) if x["id"] not in tried]
        if not rows:
            break
        row = rows[0]
        tried.add(row["id"])
        if not start(db, row):
            continue
        if asyncio.run(prepare(db, row)):
            asyncio.run(work(db, db.select("lookups", f"select=*&id=eq.{row['id']}")[0], "local", deadline))
        else:
            finish_if_done(db, row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
