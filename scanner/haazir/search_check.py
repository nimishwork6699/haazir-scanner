"""Practice run of Search courts: every kind of search is planned and checked without searching any court.

    python -m haazir.search_check          # all checks (about two minutes)
    python -m haazir.search_check --quick  # skip the large write-speed check

Nothing here contacts eCourts, High Court services or e-Jagriti: the court portals are replaced by the saved court
list (court_index) and stand-in High Court benches and consumer commissions. Searches made for the database checks are
marked as tests and never picked up by a worker, and are deleted at the end, so a search running for real is not
touched. Run before publishing any change to the scanner (scripts/publish-scanner.sh does).

What it checks, for each name x place x years x courts:
  - the list of court searches is ready in seconds, never stuck (a full All India, All years list is written fast);
  - only the asked-for courts and years, newest year first; one district stays in that district;
  - busy complexes are split one court per establishment, so workers can share them;
  - states take turns (the first court searches cover every state), and a state not listed yet is listed first;
  - how many workers start; cases Haazir already has appear at once; Stop is noticed; results pages load;
  - the likeliest courts go first; a court with no case since 2020 skips older years; a repeat search within a week
    takes the earlier answers and cases instead of asking the courts again.
"""

import argparse
import asyncio
import sys
import time
import uuid
from collections import Counter

from . import lookup as L
from .db import DB

YEARS_ONE = [2026]
YEARS_FEW = [2026, 2025, 2024]
YEARS_ALL = list(range(2026, 1999, -1))
FORUM_SETS = [["district"], ["high_court"], ["consumer"], ["nclt"], ["district", "high_court", "consumer", "nclt"]]
# The name test searches that save court answers use, so a real search never reuses a practice answer.
PRACTICE = "Haazir Practice Run Pvt Ltd"
NAMES = ["ICICI LOMBARD", "SBI", "United India Insurance Co. Ltd.", "M/s. A.B.C. (India) Pvt. Ltd.", "Rakesh Kumar", "O'Brien & Sons"]


class Report:
    def __init__(self) -> None:
        self.failed: list[str] = []
        self.passed = 0

    def check(self, ok: bool, what: str) -> None:
        if ok:
            self.passed += 1
        else:
            self.failed.append(what)
            print(f"  FAIL {what}", flush=True)


# ---------------------------------------------------------------- stand-ins for the court portals

class Portals:
    """The court portals as the saved court list knows them: no network."""

    def __init__(self, db: DB) -> None:
        self.rows = db.select("court_index", "select=*&order=state_code,dist_code,complex_code")
        self.states = {r["state_code"]: r["state_name"] for r in self.rows}

    def install(self) -> None:
        portals = self

        class Client:
            async def list_states(self):
                return dict(portals.states)

            async def list_districts(self, state):
                return {r["dist_code"]: r["district_name"] for r in portals.rows if r["state_code"] == state}

        class FakeECourts:
            def __init__(self):
                self.client = Client()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def list_complexes(self, state, dist):
                return {f"{r['complex_code']}@{r['est_codes']}@{'Y' if r['needs_est'] else 'N'}": r["complex_name"]
                        for r in portals.rows if r["state_code"] == state and r["dist_code"] == dist}

        class BenchClient:
            async def list_benches(self, court):
                return {"1": "Principal Seat"}

        class FakeHighCourt:
            def __init__(self):
                self.client = BenchClient()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

        names = sorted(set(portals.states.values()))

        class FakeEJagriti:
            # e-Jagriti names each district commission after its district, as the real one does.
            def state_commissions(self):
                return [{"commissionId": i + 1, "commissionNameEn": n} for i, n in enumerate(names)]

            def district_commissions(self, sid):
                state = names[sid - 1]
                districts = sorted({r["district_name"] for r in portals.rows if r["state_name"] == state})
                return [{"commissionId": sid * 1000 + j, "commissionNameEn": f"District Consumer Commission, {d}"} for j, d in enumerate(districts)]

        L.ECourts, L.HighCourt, L.EJagriti = FakeECourts, FakeHighCourt, FakeEJagriti


def places(portals: Portals) -> list[tuple[str, dict]]:
    by_name = {v: k for k, v in portals.states.items()}
    delhi, raj = by_name.get("Delhi"), by_name.get("Rajasthan")
    dists = Counter((r["state_code"], r["dist_code"], r["district_name"]) for r in portals.rows if r["state_code"] == delhi)
    (sc, dc, dname), _ = dists.most_common(1)[0]
    return [
        ("one district", {"state_code": sc, "state_name": "Delhi", "district_code": dc, "district_name": dname, "state_codes": None}),
        ("one state", {"state_code": delhi, "state_name": "Delhi", "district_code": None, "district_name": None, "state_codes": None}),
        ("two states", {"state_code": "multi", "state_name": "Delhi, Rajasthan", "district_code": None, "district_name": None, "state_codes": [delhi, raj]}),
        ("All India", {"state_code": "all", "state_name": "All India", "district_code": None, "district_name": None, "state_codes": None}),
    ]


def claim_order(rows: list[dict]) -> list[dict]:
    """The order claim_lookup_task hands court searches out (see migration 0030)."""
    return sorted(rows, key=lambda r: (r["forum"] != "index", r["forum"] == "district", r["rank"], r["seq"]))


# ---------------------------------------------------------------- planning checks (no database writes)

async def check_plans(db: DB, portals: Portals, rep: Report) -> None:
    print("Planning every kind of search (no court contacted, nothing written)…", flush=True)
    real_saved = L.saved_courts
    for label, place in places(portals):
        for years in (YEARS_ONE, YEARS_FEW, YEARS_ALL):
            for forums in FORUM_SETS:
                name = f"{label}, {len(years)} year{'s' if len(years) > 1 else ''}, {'+'.join(forums)}"
                row = {"id": str(uuid.uuid4()), "party": "ICICI LOMBARD", "years": years, "forums": forums, "district_cap": 500, **place}
                t = time.time()
                rows = L.plan_rows(row, await L.area(db, row))
                secs = time.time() - t
                rep.check(secs < 60, f"{name}: list ready in {secs:.0f} s (must be under a minute)")
                rep.check(bool(rows), f"{name}: has court searches")
                rep.check({r["forum"] for r in rows} <= set(forums) | {"index"}, f"{name}: only the asked-for courts")
                rep.check({r["year"] for r in rows} <= set(years), f"{name}: only the asked-for years")
                rep.check(len({r["seq"] for r in rows}) == len(rows), f"{name}: every court search numbered once")
                rep.check(all(r.get("rank") is not None for r in rows), f"{name}: every court search has its turn number")
                dist = [r for r in rows if r["forum"] == "district"]
                if "district" in forums:
                    rep.check(bool(dist), f"{name}: district courts included")
                    rep.check(all(not (r["payload"]["court"].get("needs_est") and "," in (r["payload"]["court"].get("est_codes") or ""))
                                  for r in dist), f"{name}: busy complexes split one court per establishment")
                    yrs = [r["year"] for r in sorted(dist, key=lambda r: r["seq"])]
                    rep.check(yrs == sorted(yrs, reverse=True), f"{name}: newest year first")
                    if place.get("district_code"):
                        rep.check(all(r["payload"]["court"]["dist_code"] == place["district_code"] for r in dist), f"{name}: stays in the district")
                    states = {r["payload"].get("state") for r in dist}
                    first = claim_order(dist)[:len(states)]
                    rep.check(len({r["payload"].get("state") for r in first}) == len(states),
                              f"{name}: states take turns ({len(states)} states in the first {len(states)} court searches)")
                if "nclt" in forums:
                    rep.check(len({r["payload"]["bench"] for r in rows if r["forum"] == "nclt"}) == 15, f"{name}: every NCLT bench")
                if "consumer" in forums and not place.get("district_code"):
                    rep.check(any(r["payload"].get("cid") == L.NCDRC for r in rows if r["forum"] == "consumer"), f"{name}: NCDRC included")
                k = L.workers_for(len(rows))
                rep.check(k == min(L.MAX_WORKERS, len(rows)), f"{name}: {k} workers would start")
    # Likeliest courts first: a complex where the name was found before leads its state's district court searches.
    row = {"id": str(uuid.uuid4()), "party": "ICICI LOMBARD", "years": YEARS_ONE, "forums": ["district"], "district_cap": 500,
           **dict(places(portals))["one state"]}
    courts = await L.area(db, row)
    plain = [r for r in L.plan_rows(row, courts) if r["forum"] == "district"]
    pick = plain[-1]["key"]
    ranked_rows = [r for r in L.plan_rows(row, courts, {pick: (5, 5)}) if r["forum"] == "district"]
    rep.check(L.complex_of(claim_order(ranked_rows)[0]["key"]) == L.complex_of(pick), "a court where the name was found before is searched first")
    rep.check(all(r.get("key") for r in ranked_rows), "every court search has its court key")
    # A state whose court list is not saved yet is listed by a worker first, while the others search.
    by_name = {v: k for k, v in portals.states.items()}
    missing = by_name.get("Rajasthan")
    L.saved_courts = lambda db_, code, dist: None if code == missing else real_saved(db_, code, dist)
    try:
        row = {"id": str(uuid.uuid4()), "party": "X", "years": YEARS_ONE, "forums": ["district"], "district_cap": 500,
               **dict(places(portals))["two states"]}
        rows = L.plan_rows(row, await L.area(db, row))
        idx = [r for r in rows if r["forum"] == "index"]
        rep.check(len(idx) == 1 and claim_order(rows)[0]["forum"] == "index", "a state not listed yet is listed first")
    finally:
        L.saved_courts = real_saved


# ---------------------------------------------------------------- database checks (throwaway test searches)

def test_search(db: DB, **kw) -> dict:
    """A search marked as a test: status "stopped" with a fresh heartbeat, so no worker or Worker run takes it."""
    base = {"party": "ICICI LOMBARD", "state_code": "26", "state_name": "Delhi", "years": YEARS_ONE, "forums": ["district"],
            "status": "stopped", "requested_by": "Search check (test)", "progress": {"now": "Practice run, deleted when done"}}
    return db.insert("lookups", {**base, **kw}, returning="*")[0]


def check_names(db: DB, rep: Report, made: list[str]) -> None:
    print("Names: short and long, with dots, brackets and quotes…", flush=True)
    rep.check(L.Job(db, {"id": "x", "party": "SBI"}, "t").short, "a short name (SBI) keeps only whole-word matches")
    rep.check(not L.Job(db, {"id": "x", "party": "ICICI LOMBARD"}, "t").short, "a long name keeps every match")
    for n in NAMES:
        row = test_search(db, party=n)
        made.append(row["id"])
        try:
            k = L.add_known(db, row)
            rep.check(True, n)
            print(f"  {n!r}: {k} cases already in Haazir shown at once", flush=True)
        except Exception as exc:  # a name must never break a search
            rep.check(False, f"name {n!r} breaks the search: {type(exc).__name__}: {str(exc)[:120]}")
    rep.check(L.side_of("ICICI LOMBARD", "Ramesh", "ICICI Lombard General Insurance", "MACT") == "respondent", "an insurer in an accident claim is the respondent")


def check_workers(db: DB, portals: Portals, rep: Report, made: list[str]) -> None:
    print("Workers sharing one search, progress, Stop and the results pages…", flush=True)
    row = test_search(db, party=PRACTICE)
    made.append(row["id"])
    row["district_code"], row["district_name"], row["state_codes"] = None, None, None
    rows = L.plan_rows(row, asyncio.run(L.area(db, row)))
    L.write_tasks(db, rows)
    db.update("lookups", f"id=eq.{row['id']}", {"status": "running", "heartbeat": L.now()})
    a, b = L.Job(db, row, "t1"), L.Job(db, row, "t2")
    t1, t2 = a.claim("lane1"), b.claim("lane1")
    rep.check(bool(t1 and t2) and t1["id"] != t2["id"], "two workers get different courts")
    a.finish(t1, 0, False)
    a.release(t2, held=True)
    c = L.counts(db, row["id"])
    rep.check(c.get("total") == len(rows) and c.get("done") == 1 and c.get("held") == 1, f"progress counts add up ({c.get('done')} done, {c.get('held')} held of {c.get('total')})")
    for fn, args in (("lookup_rows", {"lid": row["id"]}), ("lookup_summary", {"lid": row["id"]}), ("lookup_states", {"lid": row["id"]})):
        r = db.http.post(f"/rpc/{fn}", json=args)
        rep.check(r.status_code < 300, f"results page data loads ({fn})")
    db.update("lookups", f"id=eq.{row['id']}", {"status": "stopped"})
    a.report(force=True)
    rep.check(a.stopped, "Stop from the dashboard is noticed by the workers")
    # Finishing: the last worker closes the search, and its to-do list goes while the results stay.
    db.http.patch(f"/lookup_tasks?lookup_id=eq.{row['id']}", json={"done": True, "held": False})
    db.update("lookups", f"id=eq.{row['id']}", {"status": "running"})
    L.finish_if_done(db, row)
    left = db.http.get(f"/lookup_tasks?select=id&lookup_id=eq.{row['id']}", headers={"Prefer": "count=exact", "Range": "0-0"})
    status = db.select("lookups", f"select=status&id=eq.{row['id']}")[0]["status"]
    rep.check(status == "done" and left.headers.get("content-range", "").endswith("/0"),
              f"a finished search is marked done and its to-do list is cleared (status {status})")
    # Courts that did not answer stay listed after finishing, and "Search them again" queues just those.
    row = test_search(db, party=PRACTICE)
    made.append(row["id"])
    row["district_code"], row["district_name"], row["state_codes"] = None, None, None
    rows = L.plan_rows(row, asyncio.run(L.area(db, row)))[:6]
    for r in rows:
        r.pop("key", None)  # as an older search wrote them, without court keys
    L.write_tasks(db, rows)
    db.http.patch(f"/lookup_tasks?lookup_id=eq.{row['id']}", json={"done": True, "found": 0})
    db.http.patch(f"/lookup_tasks?lookup_id=eq.{row['id']}&seq=in.(0,1)", json={"failed": True})
    db.update("lookups", f"id=eq.{row['id']}", {"status": "running"})
    L.finish_if_done(db, row)
    kept = db.select("lookup_tasks", f"select=id,failed&lookup_id=eq.{row['id']}")
    rep.check(len(kept) == 2 and all(t["failed"] for t in kept), f"a finished search keeps only its {len(kept)} failed court searches")
    n = db._check(db.http.post("/rpc/retry_lookup", json={"lid": row["id"], "max_workers": 6}))
    after = db.select("lookups", "select=status,workers&id=eq." + row["id"])[0]
    keys = db.select("lookup_tasks", f"select=key&lookup_id=eq.{row['id']}")
    rep.check(n == 2 and after["status"] == "queued" and after["workers"] == 6, f"Search them again queues {n} court searches on {after['workers']} machines")
    rep.check(all(t["key"] for t in keys) and {t["key"] for t in keys} <= {L.task_key(r["forum"], r["payload"]) for r in rows},
              "older court searches get the same court keys as new ones")
    db.update("lookups", f"id=eq.{row['id']}", {"status": "stopped"})


def check_speed(db: DB, portals: Portals, rep: Report, made: list[str]) -> None:
    print("Older years skipped where a court has nothing since 2020; a repeat search reuses the answers…", flush=True)
    term = L.normalise(PRACTICE)
    db.http.delete(f"/court_answers?term=eq.{L.quote(term)}")
    try:
        place = dict(places(portals))["one district"]
        a = test_search(db, party=PRACTICE, years=[2026, 2019], **{k: v for k, v in place.items() if k != "state_codes"})
        made.append(a["id"])
        a["state_codes"] = None
        rows = L.plan_rows(a, asyncio.run(L.area(db, a)))
        L.write_tasks(db, rows)
        db.update("lookups", f"id=eq.{a['id']}", {"status": "running", "heartbeat": L.now()})
        job = L.Job(db, a, "t1")
        recent = [r for r in rows if r["year"] == 2026]
        busy = recent[-1]["key"]  # last in line, so every other court's 2019 search comes up (and is skipped) first
        cnr = "PRACTICE" + uuid.uuid4().hex[:8].upper()
        # Every 2026 court search answered: one court returned a case, the rest nothing.
        while (t := job.claim("lane1")) and t["year"] == 2026:
            if t["key"] == busy:
                n, cnrs = job.keep([{"forum": "district", "court": "Practice court", "district": place["district_name"], "cnr": cnr,
                                     "case_number": "MACT/1/2026", "case_type": "MACT", "year": 2026, "petitioner": "A",
                                     "respondent": PRACTICE, "side": "respondent"}], "Delhi")
                job.finish(t, n, False, 1, cnrs)
            else:
                job.finish(t, 0, False, 0, [])
        rep.check(bool(t) and t["key"] == busy and t["year"] == 2019,
                  f"only the court with a case since 2020 is searched for 2019 (got {t and (t['key'], t['year'])})")
        c = L.counts(db, a["id"], fresh=True)
        rep.check(int(c.get("skipped") or 0) == len(recent) - 1, f"{c.get('skipped')} older court searches skipped, expected {len(recent) - 1}")
        if t:
            job.finish(t, 0, False, 0, [])
        rep.check(job.claim("lane1") is None, "nothing left to search after that")
        db.update("lookups", f"id=eq.{a['id']}", {"status": "stopped"})

        b = test_search(db, party=PRACTICE, years=[2026, 2019], **{k: v for k, v in place.items() if k != "state_codes"})
        made.append(b["id"])
        b["state_codes"] = None
        L.write_tasks(db, L.plan_rows(b, asyncio.run(L.area(db, b))))
        n = L.reuse(db, b)
        rep.check(n == len(recent) + 1, f"a repeat search takes {n} answers from the first (expected {len(recent) + 1}: skipped ones are asked)")
        got = db.select("lookup_results", f"select=cnr&lookup_id=eq.{b['id']}")
        rep.check([r["cnr"] for r in got] == [cnr], "the first search's case appears in the repeat at once")
        c = L.counts(db, b["id"], fresh=True)
        rep.check(int(c.get("reused") or 0) == n, f"progress shows {c.get('reused')} court searches answered lately")
    finally:
        db.http.delete(f"/court_answers?term=eq.{L.quote(term)}")


def check_big_write(db: DB, portals: Portals, rep: Report, made: list[str]) -> None:
    print("Writing speed for the biggest search (All India, all years, every court)…", flush=True)
    row = test_search(db, state_code="all", state_name="All India", years=YEARS_ALL, forums=["district", "high_court", "consumer"])
    made.append(row["id"])
    row["district_code"], row["district_name"], row["state_codes"] = None, None, None
    rows = L.plan_rows(row, asyncio.run(L.area(db, row)))
    sample = rows[:20000]
    t = time.time()
    L.write_tasks(db, sample)
    secs = time.time() - t
    whole = secs * len(rows) / max(1, len(sample))
    print(f"  {len(rows):,} court searches; {len(sample):,} written in {secs:.0f} s, so about {whole / 60:.1f} minutes for all", flush=True)
    rep.check(whole < 15 * 60, f"the biggest search's list is written in under 15 minutes (about {whole / 60:.1f})")


# Which side a searched insurer is on when the court's list shows only its name. Case types as the courts spell
# them (ICICI all-India search). Claims decide "respondent" by law; appeals, executions and misc matters wait for
# the case page; both parties shown means the list's own order.
SIDES = [
    ("MCOP", "", "respondent"), ("M.V.C.", "", "respondent"), ("MVOP", "", "respondent"), ("OP(MV)", "", "respondent"),
    ("OP MV", "", "respondent"), ("MACP", "", "respondent"), ("M.A.C.P.", "", "respondent"), ("MACT Main", "", "respondent"),
    ("M A C C", "", "respondent"), ("M.a.c. Case", "", "respondent"), ("Motor Accident Claim Petition", "", "respondent"),
    ("WC-FATAL-LC", "", "respondent"), ("W.C.F.A.Case", "", "respondent"), ("ECA", "", "respondent"),
    ("Claim Petition", "", "respondent"), ("TS MAC", "", "respondent"),
    ("MACP. M.A.", "", "unknown"), ("MACP. Dkst.", "", "unknown"), ("MACMA", "", "unknown"), ("MACM", "", "unknown"),
    ("WC-MIS-LC", "", "unknown"), ("MJC MACC", "", "unknown"), ("Misc.Motor Accident Claim Misc.", "", "unknown"),
    ("EX MJC", "", "unknown"), ("EXECUTION CLAIM", "", "unknown"), ("Sum. Darkhast", "", "unknown"), ("EP", "", "unknown"),
    ("Appln. WCA C", "", "unknown"), ("OP", "", "unknown"), ("CS", "", "unknown"), ("MCP", "", "unknown"),
    ("FAO", "Ram Lal", "petitioner"), ("MACA", "Ram Lal", "petitioner"), ("CS", "Ram Lal", "petitioner"),
]


def check_sides(rep: Report) -> None:
    print("Sides by law when the list shows only our name…", flush=True)
    for case_type, other, want in SIDES:
        got = L.side_of("ICICI LOMBARD", "ICICI Lombard General Insruance Co. Ltd.", other, case_type)
        rep.check(got == want, f"side of an insurer in '{case_type}'{' vs ' + other if other else ''}: {got}, expected {want}")
    rep.check(L.side_of("ICICI LOMBARD", "Ram Lal", "ICICI Lombard", "MACT") == "respondent", "insurer listed as respondent")
    rep.check(L.side_of("TATA MOTORS", "Tata Motors Ltd", "", "MACT") == "unknown", "a vehicle owner is not decided by law")
    rep.check(L.placed("ICICI LOMBARD", "ICICI Lombard", "", "MCOP") == ("", "ICICI Lombard", "respondent"),
              "an insurer respondent by law is shown on the respondent side")
    rep.check(L.placed("ICICI LOMBARD", "ICICI Lombard", "", "CS") == ("ICICI Lombard", "", "unknown"), "an undecided one stays where the list put it")
    from .matching import title_of
    rep.check(title_of("Tata Motors", "") == "Tata Motors" and title_of("", "Ram Das") == "Ram Das" and title_of("A", "B") == "A vs B",
              "titles keep every letter of a name")


# Cause lists as the courts print them (shortened): Supreme Court, NCLAT, Rajasthan High Court.
LISTS = {
    "Supreme Court": ("CHIEF JUSTICE'S COURT\n7 Diary No. 50264-2026\nII-D\nNAND KUMAR YADAV RAMJEE PANDEY- 2376\nVersus\nSHAMSHAD AHMAD AND ANR.\n"
                      "8 SLP(C) No.\n20017/2026\nICICI LOMBARD GENERAL INSURANCE CO LTD RAJ KUMAR- 3898\nVersus\nSUNITA DEVI\nFOR ADMISSION\n",
                      "8", "SLP(C)/20017/2026", "petitioner"),
    "NCLAT": ("10.  IA No. 1303/2026\n(For Condone Delay)\nin\nComp App (AT) (CH)\n(Ins) No. 460/2026\n\nRamesh Traders\nVs\nState Bank of India & Anr.\n"
              "11.  Comp App (AT) (CH)\n(Ins) No. 489/2026\nAnil Kumar\nVs\nDr. Renuka Rani\n",
              "10", "Comp App (AT) (CH) (Ins)/460/2026", "respondent"),
    "Rajasthan High Court": ("154 CRL.M.(BAIL)\n16499/2026\nURN: CRLMB\n30463/2026\nAJAY S/O RAJU\nVS STATE OF RAJASTHAN\nSAMEER SHARMA-P\n"
                             "155 C.M.A. 880/2020\nURN: CMA 1730/2020\nDEVKARAN GURJAR\nVS AMAR SINGH\nPRATEEK SHARMA-P\n"
                             "[603-Fatal injury, permanent disability matters]\n[CLAIMANT/DC-UNITED INDIA INSURANCE CO/PD 60/KOTA]\n",
                             "155", "C.M.A/880/2020", "respondent"),
}


def check_causelists(rep: Report) -> None:
    from . import causelists as C
    print("Cause lists: entries split at their item numbers, names matched to the right entry…", flush=True)
    names = {"ICICI Lombard": ["icici lombard"], "State Bank": ["state bank of india"], "United India": ["united india insurance"]}
    for court, (text, item, number, side) in LISTS.items():
        hits = C.hits_in(text, names)
        rep.check(len(hits) == 1, f"{court}: one entry names a watched company (found {len(hits)})")
        if hits:
            h = hits[0]
            rep.check(h["item"] == item, f"{court}: the right item ({h['item']}, expected {item})")
            rep.check(h["case_number"] == number, f"{court}: case number {h['case_number']!r}, expected {number!r}")
            rep.check(h["side"] == side, f"{court}: side {h['side']}, expected {side}")


def check_evening_list(rep: Report) -> None:
    """The 7 pm Telegram cause list (haazir.causelist): at most 30 hearings written out, every other one counted by court."""
    import re
    from . import causelist as C
    print("Evening cause list: short on busy days, every hearing listed or counted…", flush=True)
    court = lambda i: {"complex_name": f"Court {i % 7}"}
    for n in (0, 5, 30, 31, 250):
        cases = [{"id": str(i), "title": f"Party {i} vs Company", "company_role": "respondent", "stage": "Arguments", "court": court(i),
                  "company": {"short_name": "Co"}, "advocate": {"name": "Lawyer"} if i % 3 else None} for i in range(n)]
        lines = C.message(cases, {}, "2026-10-26")
        parts = C.chunks(lines)
        listed = sum(1 for ln in lines if ln.startswith("• ") and "\n  " in ln)
        counted = sum(int(m.group(1)) for m in re.finditer(r"^• .*?: (\d+)", "".join(parts), re.M))
        rep.check(listed == min(n, C.SHOW) and listed + counted == n, f"{n} hearings: {listed} written out + {counted} counted")
        rep.check(len(parts) <= 2 and all(len(x) <= 4096 for x in parts), f"{n} hearings fit in {len(parts)} Telegram message(s)")


def check_library(rep: Report) -> None:
    """The judgment library's reader (haazir.library): acts and sections read from real headnote styles."""
    from .library import tag
    from .library_check import CASES
    print("Library: acts and sections read from Supreme Court headnotes…", flush=True)
    for text, acts, must, never in CASES:
        a, s = tag(text)
        rep.check(a == acts and all(x in s for x in must) and not any(x in s or x in a for x in never), f"headnote: {text[:60]}")


def check_lower(rep: Report) -> None:
    """The lower court decision fetcher (haazir.lower): lower court details read, complexes and names picked, final order stored once."""
    from .lower_check import run
    print("Lower court decisions: details read, courts and names picked, the final order stored once…", flush=True)
    run(rep.check)


def check_week(rep: Report) -> None:
    """The coming-week case reader (haazir.week): which cases are read tonight and why, in order, once, within the limits."""
    from .week_check import run
    print("Coming week: today's hearings, court dates, the new 7th day, gaps, the first run's whole week; once each, capped…", flush=True)
    run(rep.check)


def check_tribunals(rep: Report) -> None:
    """NCLAT, CAT and APTEL rows (haazir.tribunals): dates, parties, which cases are live, applications, sides."""
    from datetime import date, timedelta
    from . import tribunals as T
    print("Tribunals: dates read, parties split, live cases only, applications skipped, sides…", flush=True)
    rep.check(T.iso("24/02/2016") == "2016-02-24" and T.iso("17-01-2025") == "2025-01-17" and T.iso("2025-01-17") == "2025-01-17", "dates in the three tribunals' ways")
    rep.check(T.iso("N/A") is None and T.iso("") is None and T.iso(None) is None, "N/A and empty are no date")
    rep.check(T.split_title("M/S INNOVATIVE INDUSTRIES LTD. VS M/S ICICI BANK LTD.") == ("M/S INNOVATIVE INDUSTRIES LTD.", "M/S ICICI BANK LTD."), "NCLAT 'A VS B' split")
    rep.check(T.split_title("NTPC Limited Vs Central Electricity Regulatory Commission & Ors.")[1].startswith("Central Electricity"), "APTEL 'A Vs B' split")
    rep.check(T.text("Avani Towers Private Limited &#40;through its RP&#41;") == "Avani Towers Private Limited (through its RP)", "&#40; read as a bracket")
    today = date.today()
    ago = lambda days: (today - timedelta(days=days)).isoformat()
    rep.check(T.live({"pending": True, "next_date": (today + timedelta(days=5)).isoformat()}), "live: next date ahead")
    rep.check(T.live({"pending": True, "last_listed": ago(200)}), "live: heard in the last 2 years")
    rep.check(T.live({"pending": True, "filed": ago(400)}), "live: filed in the last 3 years")
    rep.check(not T.live({"pending": True, "filed": "2017-02-07"}), "not live: 'pending' since 2017, never heard")
    rep.check(not T.live({"pending": False, "next_date": (today + timedelta(days=5)).isoformat()}), "not live: disposed")
    rep.check(T.is_application({"case_number": "IA 6934/ND/2026"}) and T.is_application({"case_number": "M.A./389/2022"}), "IA and M.A. are applications")
    rep.check(not T.is_application({"case_number": "Company Appeal(AT)(Ins) 1903/ND/2026"}) and not T.is_application({"case_number": "O.A./143/2016"}), "appeals and O.A. are cases")
    names = ["state bank of india"]
    rep.check(T.side_for({"petitioner": "Kalarithara Michael Sebastine", "respondent": "STATE BANK OF INDIA"}, names) == "respondent", "side: against the company")
    rep.check(T.side_for({"petitioner": "State Bank of India.", "respondent": "Adhunik Steels Ltd."}, names) == "petitioner", "side: filed by the company")
    row = T.case_row("cat", {"filing_no": "250810009652026", "case_number": "O.A./680/2026", "case_type": "O.A.", "bench": "Jaipur", "petitioner": "KANTA DEVI",
                             "respondent": "NORTH WESTERN RAILWAY", "filed": "2026-07-09", "next_date": None, "disposed": None, "pending": True}, "co", "respondent", None)
    rep.check(row["cnr"] == "CAT250810009652026" and row["forum"] == "cat" and row["category"] == "service", "CAT case saved with its own id and type")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="skip the large write-speed check")
    args = ap.parse_args()
    db = DB()
    portals = Portals(db)
    portals.install()
    rep, made = Report(), []
    t = time.time()
    try:
        check_sides(rep)
        check_causelists(rep)
        check_evening_list(rep)
        check_tribunals(rep)
        check_library(rep)
        check_lower(rep)
        check_week(rep)
        asyncio.run(check_plans(db, portals, rep))
        check_names(db, rep, made)
        check_workers(db, portals, rep, made)
        check_speed(db, portals, rep, made)
        if not args.quick:
            check_big_write(db, portals, rep, made)
    except Exception as exc:
        rep.check(False, f"the check itself stopped: {type(exc).__name__}: {str(exc)[:200]}")
    finally:
        for lid in made:
            db.http.delete(f"/lookups?id=eq.{lid}")  # tasks and results go with it
        db.http.delete(f"/court_answers?term=eq.{L.quote(L.normalise(PRACTICE))}")
    print(f"\n{rep.passed} checks passed, {len(rep.failed)} failed, in {time.time() - t:.0f} s"
          + ("" if not rep.failed else "\nFailed:\n  " + "\n  ".join(rep.failed)), flush=True)
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
