"""The coming week: every evening, read the court pages of the cases heard in the next 7 days, so the lawyer, judge,
next date and new orders are the court's latest before the 7 pm cause list (plan: reports/After demo plan.md, part 1).

    python -m haazir.week                          # tonight's cases (at most 300, 50 minutes)
    python -m haazir.week --dry-run                # only list what would be read, by forum and reason; reads nothing
    python -m haazir.week --day 2026-11-02         # test one day: the open cases listed that day
    python -m haazir.week --all-week               # every case heard in the next 7 days
    python -m haazir.week --forum district --part 1/2   # one machine's share (week.yml runs three)

Which cases, most useful first, never one twice, until --limit cases or --minutes have gone:
  today       heard today, or listed for today before the date was moved: what happened, the next date, who appeared
  court date  an open court-ordered date (a deposit, a reply) due within 14 days or overdue, page not read in 3 days
  day 7       the day entering the window (today + 7)
  gap         heard in the window with our lawyer or the judge still unknown, page not read in 3 days
  week        heard in the window and not read since this check began (the first run reads the whole week once;
              after that only cases it could not reach). --all-week takes every case in the window.
Decided and not-ours cases are never read. Each case is read with its own forum's reader, the same code the scans use:
district (eCourts) scan.save_district, High Court highcourt.save_case, consumer (e-Jagriti) consumer.refresh_case,
NCLT nclt.refresh_case. A firewall block stops the run at once.
"""

import argparse
import asyncio
import re
import sys
import time
import zlib
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
WINDOW = 7          # days ahead
STALE_DAYS = 3      # a page read more recently than this is not read again for a gap or a court date
DEADLINE_DAYS = 14  # open court dates due within this many days (and overdue ones)
PASSED_DAYS = 120   # catch-up: cases whose date passed this recently and that were not read since (no new date known)
LIMIT, MINUTES = 300, 50
READERS = ("district", "high_court", "consumer", "nclt")
CLOSED = ("disposed", "not_ours")
REASONS = ("today", "court_date", "day7", "gap", "week", "passed", "day")
LABEL = {"today": "hearing today", "court_date": "court date due", "day7": "enters the week", "gap": "lawyer or judge missing",
         "week": "in the week", "passed": "date passed, no new date", "day": "that day"}
COLS = ("id,cnr,forum,status,company_id,company_role,case_number,court_id,raw_search,petitioners,respondents,"
        "next_hearing_date,detail_fetched_at,court_judge,judge_now,filing_date")

# Our lawyer as the website reads it (web/src/lib/court-lawyer.ts): our party's advocate on the case page, or who
# appeared for us in the latest order that names someone.
NOT_A_NAME = re.compile(r"^(state|na|n/a|nil|none|self|in person|party in person|-+|\.+)$", re.I)
IN_PERSON = re.compile(r"\bin person\b", re.I)
FOR_FILER = re.compile(r"\bfor (the )?(petitioners?|appellants?|plaintiffs?|complainants?|claimants?|applicants?)\b", re.I)
FOR_DEFENCE = re.compile(r"\bfor (the )?(respondents?|defendants?|opposite part(y|ies)|r-?\d|insur)", re.I)


def today_ist() -> date:
    return datetime.now(IST).date()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def phrases_of(watch_terms: list[dict]) -> dict[str, list[str]]:
    """Lowercase phrases a party name must contain to be our company: watch_terms.confirm, else the term itself."""
    out: dict[str, list[str]] = {}
    for t in watch_terms:
        out.setdefault(t["company_id"], []).extend(p.lower() for p in (t.get("confirm") or [t["term"]]))
    return out


def lawyer_on_page(c: dict, phrases: list[str]) -> str | None:
    side = c.get("respondents") if c.get("company_role") == "respondent" else c.get("petitioners") if c.get("company_role") == "petitioner" else []
    for p in side or []:
        adv = (p.get("advocate") or "").strip()
        if adv and not NOT_A_NAME.match(adv) and any(ph in (p.get("name") or "").lower() for ph in phrases):
            return adv
    return None


def lawyer_in_orders(role: str | None, reads: list[dict]) -> str | None:
    """reads: the case's order_reads with present_for_us, any order. The latest usable one wins."""
    found = None
    for r in sorted(reads, key=lambda r: r.get("order_date") or "9999"):
        name = (r.get("present_for_us") or "").strip()
        tags = r.get("tags") or []
        if "our_absent" in tags or "wrong_case" in tags or not name or IN_PERSON.search(name):
            continue
        if role == "respondent" and FOR_FILER.search(name) and not FOR_DEFENCE.search(name):
            continue
        if role == "petitioner" and FOR_DEFENCE.search(name) and not FOR_FILER.search(name):
            continue
        found = name
    return found


def our_lawyer(c: dict, phrases: dict[str, list[str]], reads: dict[str, list[dict]]) -> str | None:
    return lawyer_in_orders(c.get("company_role"), reads.get(c["id"], [])) or lawyer_on_page(c, phrases.get(c["company_id"], []))


def judge_of(c: dict) -> str | None:
    return (c.get("judge_now") or c.get("court_judge") or "").strip() or None


def stale(c: dict, now_utc: datetime) -> bool:
    """Never read, or read more than STALE_DAYS ago."""
    at = c.get("detail_fetched_at")
    return not at or datetime.fromisoformat(at.replace("Z", "+00:00")) < now_utc - timedelta(days=STALE_DAYS)


def read_before(c: dict, started_at: str | None) -> bool:
    """Not read since the coming-week check began (always true before its first run)."""
    at = c.get("detail_fetched_at")
    return not started_at or not at or datetime.fromisoformat(at.replace("Z", "+00:00")) < datetime.fromisoformat(started_at.replace("Z", "+00:00"))


def part_of(case_id: str, n: int) -> int:
    """Which of n machines reads this case: the same every night, about even."""
    return zlib.crc32(case_id.encode()) % n + 1


def pick(cases: list[dict], today: date, *, now_utc: datetime, phrases: dict[str, list[str]], reads: dict[str, list[dict]],
         moved: set[str] = frozenset(), court_dates: set[str] = frozenset(), started_at: str | None = None,
         all_week: bool = False, day: date | None = None, limit: int = LIMIT, forums=READERS,
         part: tuple[int, int] = (1, 1)) -> list[tuple[dict, str]]:
    """Tonight's cases with the reason each is read, most useful first, each once, at most `limit`.

    cases: candidate rows (any mix; the rules decide). moved: ids of cases listed for today (a hearings row with that
    hearing date) whatever their date is now. court_dates: ids of cases with an open court date due within
    DEADLINE_DAYS or overdue. day: test one day only (the cases listed that day, and those moved from it)."""
    k, n = part
    live = [c for c in cases if c.get("status") not in CLOSED and c.get("forum") in forums and (n == 1 or part_of(c["id"], n) == k)]
    nxt = lambda c: c.get("next_hearing_date") or ""
    first = lambda c: (c.get("company_role") != "respondent", nxt(c), c.get("case_number") or c["cnr"])
    groups: dict[str, list[dict]] = {r: [] for r in REASONS}
    if day:
        d = day.isoformat()
        groups["day"] = [c for c in live if nxt(c) == d or c["id"] in moved]
    else:
        t, end = today.isoformat(), (today + timedelta(days=WINDOW)).isoformat()
        week = [c for c in live if t <= nxt(c) <= end]
        groups["today"] = [c for c in live if nxt(c) == t or c["id"] in moved]
        groups["court_date"] = [c for c in live if c["id"] in court_dates and stale(c, now_utc)]
        groups["day7"] = [c for c in week if nxt(c) == end]
        groups["gap"] = [c for c in week if stale(c, now_utc) and not (our_lawyer(c, phrases, reads) and judge_of(c))]
        groups["week"] = [c for c in week if all_week or read_before(c, started_at)]
        # Last, with the time left: the hearing passed and the court page was not read since, so the next date is not
        # known and the case is on no cause list (1,616 on 29 Oct). The newest first; read once, they leave the list.
        since = (today - timedelta(days=PASSED_DAYS)).isoformat()
        groups["passed"] = sorted([c for c in live if since <= nxt(c) < t and (c.get("detail_fetched_at") or "")[:10] <= nxt(c)],
                                  key=lambda c: nxt(c), reverse=True)
    out, seen = [], set()
    for reason in REASONS:
        for c in (groups[reason] if reason == "passed" else sorted(groups[reason], key=first)):
            if c["id"] not in seen:
                seen.add(c["id"])
                out.append((c, reason))
    return out[:limit]


# --- reading from the database -----------------------------------------------------------------------------------

def _ids(ids, n: int = 150):
    ids = sorted(ids)
    for i in range(0, len(ids), n):
        yield ",".join(ids[i:i + n])


def candidates(db, today: date, day: date | None) -> tuple[list[dict], set[str], set[str], dict[str, list[dict]]]:
    """Rows pick() needs: the window's open cases, those moved off the day, those with a court date due; order lawyers."""
    lo, hi = (day, day) if day else (today - timedelta(days=PASSED_DAYS), today + timedelta(days=WINDOW))
    rows = db.select("cases", f"select={COLS}&status=not.in.(disposed,not_ours)&next_hearing_date=gte.{lo}&next_hearing_date=lte.{hi}")
    moved = {h["case_id"] for h in db.select("hearings", f"select=id,case_id&hearing_date=eq.{day or today}")}
    court_dates = set() if day else {d["case_id"] for d in db.select(
        "deadlines", f"select=id,case_id&status=eq.open&due_date=lte.{today + timedelta(days=DEADLINE_DAYS)}")}
    have = {c["id"] for c in rows}
    for part in _ids((moved | court_dates) - have):
        rows += db.select("cases", f"select={COLS}&status=not.in.(disposed,not_ours)&id=in.({part})")
    reads: dict[str, list[dict]] = {}
    for part in _ids({c["id"] for c in rows}):
        for r in db.select("order_reads", f"select=case_id,order_date,present_for_us,tags&present_for_us=not.is.null&case_id=in.({part})"):
            reads.setdefault(r["case_id"], []).append(r)
    return rows, moved, court_dates, reads


def started(db) -> str | None:
    rows = db.select("settings", "select=value&key=eq.week_started_at")
    return rows[0]["value"] if rows else None


# --- reading the courts ------------------------------------------------------------------------------------------

class Stop(Exception):
    """The court portal is refusing requests (firewall block): stop the run."""


class Readers:
    """Each forum's existing reader, opened the first time a case of that forum comes up. read(c) saves the court's
    page to the case and returns the new order documents; it raises Stop on a block and LookupError when the court
    has nothing for the case."""

    def __init__(self, db) -> None:
        self.db = db
        self.ec = self.hc = self.ej = self.nc = None
        self.confirm: dict[str, list[str]] = {}
        self.terms: dict[str, list[str]] = {}
        for t in db.select("watch_terms", "select=id,company_id,term,confirm,active"):
            self.confirm.setdefault(t["company_id"], []).extend(t["confirm"])
            if t.get("active"):
                self.terms.setdefault(t["company_id"], []).append(t["term"])
        self.courts: dict[str, dict] = {}
        self.cins: dict[str, str | None] = {}
        self.cache: dict = {}

    async def close(self) -> None:
        for x in (self.ec, self.hc):
            if x is not None:
                await x.__aexit__(None, None, None)

    async def read(self, c: dict) -> list[str]:
        return await getattr(self, c["forum"])(c)

    async def district(self, c: dict) -> list[str]:
        from .ecourts import Blocked, ECourts
        from .scan import save_district
        if self.ec is None:
            self.ec = await ECourts().__aenter__()
        try:
            d = await self.ec.detail(c["cnr"])
        except Blocked as exc:
            raise Stop("eCourts firewall block") from exc
        _, docs = await save_district(self.ec, self.db, c, d, self.confirm.get(c["company_id"], []))
        return docs

    async def high_court(self, c: dict) -> list[str]:
        from .ecourts import _is_block
        from .highcourt import HighCourt, save_case
        if self.hc is None:
            self.hc = await HighCourt().__aenter__()
        try:
            d, html = await self.hc.detail(c["cnr"])
        except Exception as exc:
            if _is_block(exc):
                raise Stop("High Court portal firewall block") from exc
            raise
        values, docs = await save_case(self.hc, self.db, c, d, html, self.confirm.get(c["company_id"], []))
        if values is None:
            raise LookupError("empty case page")
        return docs

    async def consumer(self, c: dict) -> list[str]:
        from .consumer import EJagriti, companies, refresh_case
        if self.ej is None:
            self.ej = EJagriti()
            self.comps = companies(self.db)
        court = self.courts.get(c["court_id"])
        if court is None and c.get("court_id"):
            court = self.courts[c["court_id"]] = self.db.select("courts", f"select=*&id=eq.{c['court_id']}")[0]
        if not court or not c.get("case_number"):
            raise LookupError("no commission or case number on file")
        if refresh_case(self.ej, self.db, c, court, self.comps) is None:
            raise LookupError("e-Jagriti has no record naming our company")
        return []

    async def nclt(self, c: dict) -> list[str]:
        from .nclt import NCLT, refresh_case
        if self.nc is None:
            self.nc = NCLT()
        if c["company_id"] not in self.cins:
            rows = self.db.select("companies", f"select=id,cin&id=eq.{c['company_id']}")
            self.cins[c["company_id"]] = rows[0].get("cin") if rows else None
        if refresh_case(self.nc, self.db, c, self.terms.get(c["company_id"], []), self.cins[c["company_id"]], self.cache) is None:
            raise LookupError("not found at NCLT")
        return []


def change(before: dict, after: dict, phrases, reads) -> dict:
    """What one read changed: our lawyer, the judge, the next date."""
    lb, la = our_lawyer(before, phrases, reads), our_lawyer(after, phrases, reads)
    jb, ja = judge_of(before), judge_of(after)
    nb, na = before.get("next_hearing_date"), after.get("next_hearing_date")
    return {"lawyer": la if la and la != lb else None, "judge": ja if ja and ja != jb else None,
            "next": na if na and na != nb else None, "was": nb, "decided": after.get("status") == "disposed" and before.get("status") != "disposed"}


def line(i: int, n: int, c: dict, reason: str, what: str) -> str:
    return f"{i}/{n} {c['forum']} {c.get('case_number') or c['cnr']} ({LABEL[reason]}): {what}"


async def read_all(db, picks: list[tuple[dict, str]], readers, minutes: float, phrases, reads, out=print) -> dict:
    """Read each picked case once, in order, until the time is up. Returns the counts."""
    stats = {"picked": len(picks), "read": 0, "lawyer": 0, "judge": 0, "next_date": 0, "decided": 0, "orders": 0,
             "failed": 0, "blocked": False, "not_reached": 0, "docs": [], "case_ids": []}
    end = time.time() + minutes * 60
    for i, (c, reason) in enumerate(picks, 1):
        if time.time() >= end:
            stats["not_reached"] = len(picks) - i + 1
            out(f"Time is up after {minutes:g} minutes: {stats['not_reached']} cases left for the next run.")
            break
        try:
            docs = await readers.read(c)
        except Stop as exc:
            stats["blocked"] = True
            stats["not_reached"] = len(picks) - i + 1
            out(line(i, len(picks), c, reason, f"stopped: {exc}. The rest wait for the next run."))
            break
        except Exception as exc:  # timeouts, captcha exhaustion, a page that did not read: the next run tries again
            stats["failed"] += 1
            try:
                db.update("cases", f"id=eq.{c['id']}", {"detail_error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            except Exception:
                pass
            out(line(i, len(picks), c, reason, f"failed ({type(exc).__name__}: {str(exc)[:100]})"))
            continue
        after = (db.select("cases", f"select={COLS}&id=eq.{c['id']}") or [c])[0]
        ch = change(c, after, phrases, reads)
        stats["read"] += 1
        stats["case_ids"].append(c["id"])
        stats["docs"] += docs
        stats["orders"] += len(docs)
        stats["lawyer"] += bool(ch["lawyer"])
        stats["judge"] += bool(ch["judge"])
        stats["next_date"] += bool(ch["next"])
        stats["decided"] += ch["decided"]
        bits = ([f"next date {ch['next']} (was {ch['was'] or 'none'})"] if ch["next"] else []) + \
               (["decided"] if ch["decided"] else []) + \
               ([f"our lawyer {ch['lawyer']}"] if ch["lawyer"] else []) + \
               ([f"judge {ch['judge']}"] if ch["judge"] else []) + \
               ([f"{len(docs)} new order{'s' if len(docs) != 1 else ''}"] if docs else [])
        out(line(i, len(picks), c, reason, "read; " + ", ".join(bits) if bits else "read, nothing new"))
    return stats


def summary(stats: dict) -> str:
    return (f"Coming week: read {stats['read']} of {stats['picked']}; updated lawyer {stats['lawyer']}, updated judge {stats['judge']}, "
            f"new next date {stats['next_date']}, decided {stats['decided']}, new orders {stats['orders']}, failed {stats['failed']}, "
            f"blocked {'yes' if stats['blocked'] else 'no'}" + (f", not reached {stats['not_reached']}" if stats["not_reached"] else ""))


def counts(picks: list[tuple[dict, str]]) -> list[str]:
    """Plain table: forum x reason."""
    forums = sorted({c["forum"] for c, _ in picks})
    head = f"{'forum':<12}" + "".join(f"{r:>12}" for r in REASONS) + f"{'total':>8}"
    rows = [head]
    for f in forums + ["all"]:
        mine = [r for c, r in picks if f == "all" or c["forum"] == f]
        rows.append(f"{f:<12}" + "".join(f"{sum(1 for r in mine if r == x):>12}" for x in REASONS) + f"{len(mine):>8}")
    return rows + ["(" + "; ".join(f"{r} = {LABEL[r]}" for r in REASONS) + ")"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=LIMIT)
    ap.add_argument("--minutes", type=float, default=MINUTES)
    ap.add_argument("--all-week", action="store_true")
    ap.add_argument("--day", type=date.fromisoformat, default=None, help="test one day: YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="only list what would be read; reads and writes nothing")
    ap.add_argument("--forum", nargs="*", choices=READERS, default=list(READERS))
    ap.add_argument("--part", default="1/1", help="this machine's share, k/n (cases split by id)")
    args = ap.parse_args()
    k, n = (int(x) for x in args.part.split("/"))

    from .db import DB
    db = DB()
    today = today_ist()
    rows, moved, court_dates, reads = candidates(db, today, args.day)
    phrases = phrases_of(db.select("watch_terms", "select=id,company_id,term,confirm"))
    since = started(db)
    picks = pick(rows, today, now_utc=datetime.now(timezone.utc), phrases=phrases, reads=reads, moved=moved, court_dates=court_dates,
                 started_at=since, all_week=args.all_week, day=args.day, limit=args.limit, forums=tuple(args.forum), part=(k, n))
    every = pick(rows, today, now_utc=datetime.now(timezone.utc), phrases=phrases, reads=reads, moved=moved, court_dates=court_dates,
                 started_at=since, all_week=args.all_week, day=args.day, limit=10 ** 9, forums=tuple(args.forum), part=(k, n))
    other = sorted({c["forum"] for c in rows if c["forum"] not in READERS})
    print(f"{today} (IST){' · day ' + str(args.day) if args.day else ''} · forums {' '.join(args.forum)} · part {k}/{n} · "
          f"{'first run: the whole week' if not since else 'check began ' + since[:10]}{' · --all-week' if args.all_week else ''}", flush=True)
    print(f"{len(every)} cases due a read, {len(picks)} within the limit of {args.limit}"
          + (f"; forums with no reader skipped: {', '.join(other)}" if other else ""), flush=True)

    if args.dry_run:
        for i, (c, reason) in enumerate(picks, 1):
            print(line(i, len(picks), c, reason, f"next {c.get('next_hearing_date') or 'none'}, last read {(c.get('detail_fetched_at') or 'never')[:10]}"))
        print("\nWould read (within the limit):")
        print("\n".join(counts(picks)))
        if len(every) > len(picks):
            print("\nAll due (no limit):")
            print("\n".join(counts(every)))
        print("\nDry run: nothing read, nothing written.")
        return 0

    run = db.insert("scan_runs", {"trigger": "week"}, returning="id")[0]
    if not since:
        db.insert("settings", {"key": "week_started_at", "value": now()}, upsert_on="key", ignore_duplicates=True)
    readers = Readers(db)
    stats, status = None, "failed"
    try:
        stats = asyncio.run(_run(db, picks, readers, args.minutes, phrases, reads))
        status = "partial" if stats["blocked"] or stats["failed"] or stats["not_reached"] else "ok"
    finally:
        msg = summary(stats) if stats else "coming week: the run stopped with an error"
        from . import relay
        db.update("scan_runs", f"id=eq.{run['id']}", {"status": status, "finished_at": now(), "results": len(picks),
                                                      "details_fetched": stats["read"] if stats else 0,
                                                      "log": [{"at": now(), "level": "info" if status == "ok" else "warn", "msg": msg}]
                                                             + ([{"at": now(), "level": "info", "msg": relay.summary()}] if relay.summary() else [])})
    print(summary(stats), flush=True)
    return 0


async def _run(db, picks, readers, minutes, phrases, reads) -> dict:
    from . import orders
    try:
        stats = await read_all(db, picks, readers, minutes, phrases, reads, out=lambda s: print(s, flush=True))
    finally:
        await readers.close()
    if stats["docs"]:
        # As the scan does: the first 20 now, the rest by the background order reader.
        print(f"{orders.read_with_ai(stats['docs'])} of {len(stats['docs'])} new orders read by AI now", flush=True)
    if stats["read"]:
        orders.analyse_orders()
        orders.refresh_deadlines(stats["case_ids"])
    return stats


if __name__ == "__main__":
    sys.exit(main())
