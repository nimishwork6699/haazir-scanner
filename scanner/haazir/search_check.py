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
  - how many workers start; cases Haazir already has appear at once; Stop is noticed; results pages load.
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
FORUM_SETS = [["district"], ["high_court"], ["consumer"], ["district", "high_court", "consumer"]]
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
                k = L.workers_for(len(rows))
                rep.check(k == min(L.MAX_WORKERS, len(rows)), f"{name}: {k} workers would start")
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
    return db.insert("lookups", {**base, **kw})[0]


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
    row = test_search(db)
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
        asyncio.run(check_plans(db, portals, rep))
        check_names(db, rep, made)
        check_workers(db, portals, rep, made)
        if not args.quick:
            check_big_write(db, portals, rep, made)
    except Exception as exc:
        rep.check(False, f"the check itself stopped: {type(exc).__name__}: {str(exc)[:200]}")
    finally:
        for lid in made:
            db.http.delete(f"/lookups?id=eq.{lid}")  # tasks and results go with it
    print(f"\n{rep.passed} checks passed, {len(rep.failed)} failed, in {time.time() - t:.0f} s"
          + ("" if not rep.failed else "\nFailed:\n  " + "\n  ".join(rep.failed)), flush=True)
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
