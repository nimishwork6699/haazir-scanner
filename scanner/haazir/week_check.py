"""Checks for the coming-week case reader (haazir.week), with no network and no database.

    python -m haazir.week_check

Made-up cases around one evening: which are read and why (today, a court date due, the day entering the week, a
missing lawyer or judge, the first run's whole week, one test day), in that order, each once, within the limit; split
across machines; and the reading loop: a firewall block stops it, a failure moves on, time up leaves the rest.
"""

import asyncio
import sys
from datetime import date, datetime, timedelta, timezone

from . import week as W

TODAY = date(2026, 10, 29)
NOW = datetime(2026, 10, 29, 11, 30, tzinfo=timezone.utc)
CO = "co1"
PHRASES = {CO: ["icici lombard"]}


def day(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def read_ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def case(i: str, nxt: int | None, *, forum="district", status="new", role="respondent", read=10.0, lawyer="Mr. A Sharma",
         judge="ADJ-1", number=None) -> dict:
    ours = {"name": "ICICI Lombard General Insurance Co. Ltd.", "advocate": lawyer or ""}
    other = {"name": "Ram Lal", "advocate": "Mr. Their Lawyer"}
    return {"id": i, "cnr": f"RJJP01{i:0>6}2026"[:16], "forum": forum, "status": status, "company_id": CO, "company_role": role,
            "case_number": number or f"MACT/{i}/2026", "court_id": "c1", "raw_search": {},
            "petitioners": [other] if role == "respondent" else [ours], "respondents": [ours] if role == "respondent" else [other],
            "next_hearing_date": day(nxt) if nxt is not None else None, "detail_fetched_at": read_ago(read) if read is not None else None,
            "court_judge": judge, "judge_now": None, "filing_date": "2026-01-01"}


def run(check) -> None:
    started = read_ago(30)  # the check began a month ago: made-up cases (read 10 days ago) were read since
    P = lambda cases, **kw: W.pick(cases, TODAY, now_utc=NOW, phrases=PHRASES, reads=kw.pop("reads", {}), started_at=kw.pop("started_at", started), **kw)
    ids = lambda picks: [c["id"] for c, _ in picks]
    why = lambda picks: {c["id"]: r for c, r in picks}

    # Dates: the window is today .. today + 7; a passed date not read since is the catch-up, last; one read since is left.
    cs = [case("past", -1), case("t0", 0), case("t3", 3), case("t7", 7), case("t8", 8), case("none", None), case("pastread", -5, read=1),
          case("old", -(W.PASSED_DAYS + 1))]
    got = why(P(cs, all_week=True))
    check(set(got) == {"t0", "t3", "t7", "past"}, f"window is today to today+7, plus the passed catch-up (got {sorted(got)})")
    check(got.get("past") == "passed" and ids(P(cs, all_week=True))[-1] == "past", f"the passed catch-up comes last: {got.get('past')}")
    check(got.get("t0") == "today" and got.get("t7") == "day7" and got.get("t3") == "week", f"reasons by date: {got}")

    # (a) today, and listed for today before the date moved.
    got = why(P([case("moved", 20), case("t0", 0), case("far", 30)], moved={"moved", "t0"}))
    check(got == {"moved": "today", "t0": "today"}, f"a case moved off today is read with today's (got {got})")

    # (e) an open court date: read if the page is 3+ days old, whatever the hearing date; not if read lately.
    got = why(P([case("dl_old", -37, read=20), case("dl_fresh", -37, read=1), case("dl_never", 40, read=None)],
                court_dates={"dl_old", "dl_fresh", "dl_never"}))
    check(got == {"dl_old": "court_date", "dl_never": "court_date"}, f"court dates: stale or never read only (got {got})")

    # (c) gaps: our lawyer or the judge missing, page 3+ days old.
    cs = [case("nolaw", 2, lawyer=""), case("nojudge", 2, judge=None), case("full", 2), case("nolaw_fresh", 2, lawyer="", read=1),
          case("na", 2, lawyer="NA"), case("orderlaw", 2, lawyer=""), case("theirs", 2, lawyer=""), case("absent", 2, lawyer="")]
    reads = {"orderlaw": [{"order_date": "2026-10-01", "present_for_us": "Mr. B Gupta for the insurer", "tags": []}],
             "theirs": [{"order_date": "2026-10-01", "present_for_us": "Mr. C for the claimants", "tags": []}],
             "absent": [{"order_date": "2026-10-01", "present_for_us": "Mr. D", "tags": ["our_absent"]}]}
    got = why(P(cs, reads=reads))
    check(got == {"nolaw": "gap", "nojudge": "gap", "na": "gap", "theirs": "gap", "absent": "gap"},
          f"gaps: no lawyer, no judge, 'NA', the other side's counsel, an absent note; not a fresh read or an order's lawyer (got {got})")
    pet = case("petside", 2, role="petitioner", lawyer="")
    check(W.our_lawyer(pet, PHRASES, {"petside": [{"order_date": "2026-10-01", "present_for_us": "Mr. E for the respondent insurer", "tags": []}]}) is None,
          "a petitioner case: counsel for the respondents is not ours")
    check(W.our_lawyer(case("x", 1), PHRASES, {}) == "Mr. A Sharma", "our lawyer from our party's row on the case page")
    check(W.our_lawyer(case("y", 1, lawyer="", role="respondent"), PHRASES, {}) is None, "the other party's advocate is not ours")
    check(W.our_lawyer(case("z", 1), PHRASES, {"z": [{"order_date": "2026-09-01", "present_for_us": "Old", "tags": []},
                                                     {"order_date": "2026-10-01", "present_for_us": "New", "tags": []}]}) == "New",
          "the latest order's lawyer wins")

    # (d) the first run reads the whole week; later runs only what was not read since the check began.
    cs = [case("w1", 2), case("w2", 4, read=0.5)]
    check(set(ids(P(cs, started_at=None))) == {"w1", "w2"}, "first run: the whole week")
    check(ids(P(cs, started_at=read_ago(1))) == ["w1"], "later runs: only cases not read since the check began")
    check(set(ids(P(cs, all_week=True))) == {"w1", "w2"}, "--all-week: the whole week again")

    # Closed cases and forums with no reader are never read.
    cs = [case("disp", 0, status="disposed"), case("notours", 0, status="not_ours"), case("cat", 0, forum="cat"), case("ok", 0)]
    check(ids(P(cs, started_at=None)) == ["ok"], "decided, not ours and forums without a reader are skipped")

    # Order a, e, b, c, d; each case once, under its first reason.
    cs = [case("w", 3), case("g", 2, lawyer=""), case("b7", 7), case("e", -5), case("a", 0, lawyer=""), case("ae", 0)]
    picks = P(cs, started_at=None, court_dates={"e", "ae"}, moved={"a"})
    check(ids(picks) == ["a", "ae", "e", "b7", "g", "w"], f"priority today, court date, day 7, gap, week (got {ids(picks)})")
    check(len(ids(picks)) == len(set(ids(picks))) and why(picks)["ae"] == "today" and why(picks)["a"] == "today",
          "a case due for several reasons is read once, under the first")
    check(ids(P(cs, started_at=None, court_dates={"e", "ae"}, moved={"a"}, limit=3)) == ["a", "ae", "e"], "the limit keeps the most useful")
    check(P(cs, started_at=None, limit=0) == [], "limit 0 reads nothing")
    resp_first = ids(P([case("p", 0, role="petitioner"), case("r", 0)], started_at=None))
    check(resp_first == ["r", "p"], f"cases against us first within a reason (got {resp_first})")

    # --day: only that day (and cases moved off it), whatever else is due.
    cs = [case("d2", 2), case("d3", 3), case("t0", 0), case("mv", 9)]
    got = why(P(cs, day=TODAY + timedelta(days=2), moved={"mv"}, court_dates={"t0"}))
    check(got == {"d2": "day", "mv": "day"}, f"--day reads that day only (got {got})")

    # Machines: each case on exactly one; forums split.
    cs = [case(f"c{i}", i % 8) for i in range(200)]
    a, b = set(ids(P(cs, started_at=None, part=(1, 2), limit=999))), set(ids(P(cs, started_at=None, part=(2, 2), limit=999)))
    check(not (a & b) and len(a | b) == 200 and 60 < len(a) < 140, f"two machines share the cases without overlap ({len(a)} + {len(b)})")
    cs = [case("hc", 0, forum="high_court"), case("dc", 0)]
    check(ids(P(cs, started_at=None, forums=("high_court", "consumer"))) == ["hc"], "--forum picks that forum only")

    # The reading loop.
    class DB:
        def __init__(self, rows):
            self.rows = {r["id"]: dict(r) for r in rows}
            self.updates = []

        def select(self, table, query):
            cid = query.split("id=eq.")[1]
            return [self.rows[cid]]

        def update(self, table, match, values, **kw):
            self.updates.append((match, values))
            self.rows[match.split("id=eq.")[1]].update(values)

    class Readers:
        def __init__(self, db, script):
            self.db, self.script, self.seen = db, script, []

        async def read(self, c):
            self.seen.append(c["id"])
            act = self.script.get(c["id"], "ok")
            if act == "block":
                raise W.Stop("eCourts firewall block")
            if act == "fail":
                raise TimeoutError("slow")
            self.db.rows[c["id"]].update(next_hearing_date=day(14), court_judge="ADJ-2",
                                         respondents=[{"name": "ICICI Lombard GIC", "advocate": "Mr. New"}])
            return ["doc1"] if act == "order" else []

    cs = [case("r1", 0, lawyer=""), case("r2", 0), case("r3", 1), case("r4", 2), case("r5", 3)]
    db = DB(cs)
    rd = Readers(db, {"r2": "fail", "r3": "order", "r4": "block"})
    lines = []
    stats = asyncio.run(W.read_all(db, [(c, "today") for c in cs], rd, 50, PHRASES, {}, out=lines.append))
    check(rd.seen == ["r1", "r2", "r3", "r4"], f"a block stops the run at once (read {rd.seen})")
    check(stats["blocked"] and stats["not_reached"] == 2 and stats["failed"] == 1 and stats["read"] == 2,
          f"counts after a block: {({k: v for k, v in stats.items() if k not in ('docs', 'case_ids')})}")
    check(stats["lawyer"] == 2 and stats["judge"] == 2 and stats["next_date"] == 2 and stats["orders"] == 1,
          "lawyer, judge, next date and new orders counted from what the read changed")
    check(any("detail_error" in v for _, v in db.updates), "a failed read is noted on the case")
    check(len(lines) == 4 and all(" (hearing today): " in ln for ln in lines), "one plain line per case")
    check("blocked yes" in W.summary(stats) and "not reached 2" in W.summary(stats), "the summary says it was blocked")
    stats = asyncio.run(W.read_all(DB(cs), [(c, "today") for c in cs], Readers(DB(cs), {}), 0, PHRASES, {}, out=lambda s: None))
    check(stats["read"] == 0 and stats["not_reached"] == 5, "no time left: nothing read, all left for the next run")


def main() -> int:
    failed, passed = [], [0]

    def check(ok: bool, what: str) -> None:
        if ok:
            passed[0] += 1
        else:
            failed.append(what)
            print(f"FAIL {what}")

    run(check)
    print(f"{passed[0]} of {passed[0] + len(failed)} coming-week checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
