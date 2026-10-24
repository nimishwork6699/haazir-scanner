"""Scan eCourts for watched companies, record new cases, fetch their details, refresh suggestions.

    python -m haazir.scan                       # scheduled run: current year, all active courts
    python -m haazir.scan --trigger manual
    python -m haazir.scan --years 2025 2026 --detail-budget 300 --trigger backfill
    python -m haazir.scan --only-details        # skip searching, just work through the detail queue
"""

import argparse
import asyncio
import re
import sys
from datetime import date, datetime, timedelta, timezone

from . import config, telegram
from .classify import categorize
from .db import DB
from .ecourts import Blocked, ECourts, LayoutError
from .matching import is_company, normalise, role, title_of
from . import orders
from .suggest import rank


def telegram_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def case_no(case_number: str | None) -> int:
    """'MACT Main/1184/2026' -> 1184. Case numbers rise through the year, so higher means filed later."""
    m = re.search(r"/(\d+)/\d{4}$", case_number or "")
    return int(m.group(1)) if m else 0


def iso(d) -> str | None:
    return d.isoformat() if d else None


def disposal_of(html: str) -> str | None:
    """How a decided case ended, from the case page: "Uncontested--Withdrawn", "Contested--Allowed"."""
    # Tags become "|" so the label and its value stay separate cells.
    text = re.sub(r"<[^>]+>", "|", html or "")
    m = re.search(r"Nature of Disposal[\s|:]*([^|]+?)\s*\|", text, re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else None


class Run:
    def __init__(self, db: DB, trigger: str) -> None:
        self.db = db
        self.row = db.insert("scan_runs", {"trigger": trigger}, returning="*")[0]
        self.stats = {"searches": 0, "search_failures": 0, "results": 0, "new_cases": 0, "details_fetched": 0}
        self.log: list[dict] = []
        self.blocked = False
        self.layout_errors = 0
        self.skipped_quiet = 0
        self.started = datetime.now(timezone.utc)

    def note(self, level: str, msg: str, **extra) -> None:
        print(f"[{level}] {msg}", extra or "", flush=True)
        self.log.append({"at": now(), "level": level, "msg": msg, **extra})

    def finish(self) -> None:
        status = "partial" if (self.blocked or self.stats["search_failures"]) else "ok"
        if self.stats["searches"] and self.stats["search_failures"] == self.stats["searches"]:
            status = "failed"
        self.db.update("scan_runs", f"id=eq.{self.row['id']}",
                       {**self.stats, "status": status, "finished_at": now(), "log": self.log[-200:]})
        print("run finished:", status, self.stats, flush=True)
        if status != "ok" or self.layout_errors:
            self.alert(status)

    def alert(self, status: str) -> None:
        """Tell the operator on Telegram when a scan did not complete cleanly. Never fails the run."""
        try:
            chat = next((r["value"] for r in self.db.select("settings", "select=value&key=eq.ops_telegram_chat_id")), None)
            if not chat:
                return
            mins = round((datetime.now(timezone.utc) - self.started).total_seconds() / 60)
            problems = [m["msg"] for m in self.log if m["level"] in ("error", "warn")][-5:]
            lines = [f"<b>Haazir scan: {status}</b> ({mins} min)",
                     f"Searches {self.stats['searches']}, failed {self.stats['search_failures']}"
                     + (f", page layout errors {self.layout_errors}" if self.layout_errors else ""),
                     f"Case pages read {self.stats['details_fetched']}"]
            if self.blocked:
                lines.append("eCourts firewall blocked the scan. The next run continues where this one stopped.")
            lines += [f"• {telegram_escape(p)[:180]}" for p in problems]
            telegram.send(int(chat), "\n".join(lines))
        except Exception as exc:  # an alert must never break the scan
            print("alert failed:", exc, flush=True)


async def search_phase(run: Run, ec: ECourts, years: list[int], status: str, baseline_all: bool = False,
                       company: str = "", city: str = "") -> None:
    db = run.db
    companies = {c["id"]: c for c in db.select("companies")}
    terms = [t for t in db.select("watch_terms", "select=*&active=eq.true")]
    courts = db.select("courts", "select=*&active=eq.true&forum=eq.district&order=district_name,complex_name")
    # Scan now can be limited to one company or one city.
    if city:
        courts = [c for c in courts if (c.get("city") or "").lower() == city.lower()]
    if company:
        terms = [t for t in terms if company.lower() in (companies[t["company_id"]]["short_name"].lower(), companies[t["company_id"]]["name"].lower())]
    run.note("info", f"scope: {company or 'all companies'} · {city or 'all cities'} · {len(courts)} courts, {len(terms)} search names")
    coverage = {(r["court_id"], r["company_id"], r["year"], r["term"]): r for r in db.select("scan_coverage")}
    known = {r["cnr"] for r in db.select("cases", "select=cnr")}
    quiet_cutoff = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()

    for court in courts:
        for year in years:
            for t in terms:
                if t.get("court_ids") and court["id"] not in t["court_ids"]:
                    continue  # this name is watched in other courts
                comp = companies[t["company_id"]]
                # Baseline is per search name: a name added later must not turn its old cases into "new".
                key = (court["id"], comp["id"], year, t["term"])
                # eCourts matches the name anywhere inside a word, so a short name such as "SBI" also returns
                # "Jasbir Singh". For short single-word names, keep only rows that name the company as a word.
                short = " " not in normalise(t["term"]) and len(normalise(t["term"])) <= 5
                baseline = baseline_all or key not in coverage
                # A court where this name found nothing last time is searched once a day, not every run.
                cov = coverage.get(key) or {}
                if not baseline and cov.get("last_count") == 0 and cov.get("last_scanned_at", "") > quiet_cutoff:
                    run.skipped_quiet += 1
                    continue
                run.stats["searches"] += 1
                try:
                    results = await ec.search(court, t["term"], year, status)
                except Blocked:
                    run.blocked = True
                    run.note("error", "eCourts firewall blocked the scan; stopping to let it cool down",
                             court=court["complex_name"])
                    return
                except LayoutError as exc:
                    # The page was fetched but did not read cleanly (a state with a different layout).
                    # Nothing is saved from it; the failure is loud so the parser gets fixed.
                    run.stats["search_failures"] += 1
                    run.layout_errors += 1
                    run.note("error", f"results page not read cleanly: {exc}",
                             court=court["complex_name"], term=t["term"], year=year)
                    continue
                except Exception as exc:  # captcha exhaustion, timeouts, portal errors
                    run.stats["search_failures"] += 1
                    run.note("warn", f"search failed: {type(exc).__name__}: {str(exc)[:160]}",
                             court=court["complex_name"], term=t["term"], year=year)
                    continue

                hits = 0
                new_rows = []
                for r in results:
                    if not r.cnr_number:
                        continue
                    if short and not (is_company(r.petitioner, t["confirm"]) or is_company(r.respondent, t["confirm"])):
                        continue
                    # The list shows only lead parties. If our name is not among them we still keep the case:
                    # the detail page lists every party and decides (see detail_phase).
                    hits += 1
                    if r.cnr_number in known:
                        continue
                    known.add(r.cnr_number)
                    new_rows.append({
                        "cnr": r.cnr_number,
                        "company_id": comp["id"],
                        "court_id": court["id"],
                        "case_type": r.case_type or None,
                        "case_number": r.case_number or None,
                        "title": title_of(r.petitioner, r.respondent),
                        "petitioners": [{"name": r.petitioner, "advocate": ""}] if r.petitioner else [],
                        "respondents": [{"name": r.respondent, "advocate": ""}] if r.respondent else [],
                        # The search list sometimes shows only the matched party, so its order is not
                        # evidence of side. The detail page sets the real side.
                        "company_role": "unknown",
                        "category": categorize(r.case_type),
                        "is_baseline": baseline,
                        "raw_search": r.to_dict() if hasattr(r, "to_dict") else {"petitioner": r.petitioner, "respondent": r.respondent},
                    })
                if new_rows:
                    inserted = db.insert("cases", new_rows, upsert_on="cnr", ignore_duplicates=True,
                                         returning="id,is_baseline")
                    for c in inserted:
                        db.log(c["id"], "detected", {"court": court["complex_name"], "baseline": c["is_baseline"],
                                                    "term": t["term"]})
                    if not baseline:
                        run.stats["new_cases"] += len(inserted)
                run.stats["results"] += hits
                db.insert("scan_coverage", {"court_id": court["id"], "company_id": comp["id"], "year": year,
                                            "term": t["term"], "last_scanned_at": now(), "last_count": hits,
                                            **({"first_scanned_at": now()} if baseline else {})},
                          upsert_on="court_id,company_id,year,term")
                coverage[key] = {"seen": True}
                run.note("info", f"{court['complex_name']} · {t['term']} · {year}: {hits} cases, {len(new_rows)} new to us",
                         baseline=baseline)
        db.update("courts", f"id=eq.{court['id']}", {"last_scanned_at": now()})
    if run.skipped_quiet:
        run.note("info", f"{run.skipped_quiet} searches skipped: nothing found there last time, searched again within a day")


def newest_first(cases: list[dict]) -> list[dict]:
    """Order by recency of filing without knowing filing dates yet.

    A CNR is establishment code (6) + filing serial (6) + year (4), e.g. RJJM01|017646|2026. The
    serial rises with every filing in that establishment, so within one establishment a higher
    serial is a newer filing. Rank inside each establishment, then interleave the ranks so busy
    courts do not crowd out quiet ones."""
    groups: dict[str, list[dict]] = {}
    for c in cases:
        groups.setdefault(c["cnr"][:6], []).append(c)
    ranked = []
    for items in groups.values():
        items.sort(key=lambda c: (-int(c["cnr"][-4:]) if c["cnr"][-4:].isdigit() else 0,
                                  -int(c["cnr"][6:12]) if c["cnr"][6:12].isdigit() else 0))
        ranked += [(i, c) for i, c in enumerate(items)]
    ranked.sort(key=lambda x: x[0])
    return [c for _, c in ranked]


def detail_queue(db: DB, budget: int) -> list[dict]:
    """Which case pages to read this run, most useful first, up to the budget.

    1. Never read and found after the baseline: genuinely new cases.
    2. The hearing date has passed and the page was not read since: the court has set the next date.
       Cases against the company first. Re-checked every 3 days until the court posts a new date.
    3. A hearing in the next 3 days and not read in 20 hours: catch adjournments before the date.
    4. Never read and already on file at the baseline (the backlog), newest first, rows where the search
       list shows the company as respondent before the rest.
    Disposed and not-ours cases are never read again."""
    cols = "select=id,cnr,case_number,is_baseline,company_role,company_id,raw_search,next_hearing_date,detail_fetched_at"
    today = date.today()
    # Only district-court cases are read from eCourts here; consumer and High Court cases have their own readers.
    open_cases = "status=not.in.(disposed,not_ours)&forum=eq.district"
    fresh = db.select("cases", f"{cols}&detail_fetched_at=is.null&forum=eq.district")
    passed = db.select("cases", f"{cols}&{open_cases}&next_hearing_date=lt.{today.isoformat()}"
                                f"&next_hearing_date=gte.{(today - timedelta(days=60)).isoformat()}"
                                f"&detail_fetched_at=not.is.null")
    three_days_ago = (today - timedelta(days=3)).isoformat()
    passed = [c for c in passed
              if c["detail_fetched_at"][:10] <= c["next_hearing_date"] or c["detail_fetched_at"][:10] < three_days_ago]
    passed.sort(key=lambda c: (c["company_role"] != "respondent", c["next_hearing_date"]))
    stale = (datetime.now(timezone.utc) - timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    soon = db.select("cases", f"{cols}&{open_cases}&next_hearing_date=gte.{today.isoformat()}"
                              f"&next_hearing_date=lte.{(today + timedelta(days=3)).isoformat()}"
                              f"&detail_fetched_at=lt.{stale}")
    # Decided recently but how it ended is not known: award and appeal deadlines depend on it.
    decided = db.select("cases", f"{cols}&forum=eq.district&status=eq.disposed&disposal=is.null"
                                 f"&decision_date=gte.{(today - timedelta(days=120)).isoformat()}")

    confirm: dict[str, list[str]] = {}
    for t in db.select("watch_terms", "select=company_id,confirm"):
        confirm.setdefault(t["company_id"], []).extend(t["confirm"])
    backlog = newest_first([c for c in fresh if c["is_baseline"]])
    backlog.sort(key=lambda c: not is_company((c.get("raw_search") or {}).get("respondent", ""),
                                              confirm.get(c["company_id"], [])))

    queue, seen = [], set()
    for c in newest_first([c for c in fresh if not c["is_baseline"]]) + decided + passed + soon + backlog:
        if c["id"] not in seen:
            seen.add(c["id"])
            queue.append(c)
    return queue[:budget]


async def detail_phase(run: Run, ec: ECourts, budget: int) -> None:
    db = run.db
    terms_by_company: dict[str, list[str]] = {}
    for t in db.select("watch_terms", "select=company_id,confirm"):
        terms_by_company.setdefault(t["company_id"], []).extend(t["confirm"])

    new_orders: list[str] = []
    for c in detail_queue(db, budget):
        try:
            d = await ec.detail(c["cnr"])
        except Blocked:
            run.blocked = True
            run.note("error", "eCourts firewall blocked detail fetches; stopping")
            return
        except Exception as exc:
            db.update("cases", f"id=eq.{c['id']}", {"detail_error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            run.note("warn", f"detail failed for {c['cnr']}: {type(exc).__name__}")
            continue

        pets = [{"name": p.name, "advocate": p.advocate} for p in d.petitioners]
        resps = [{"name": p.name, "advocate": p.advocate} for p in d.respondents]
        acts = [{"act": a.act, "sections": a.sections} for a in d.acts]
        acts_text = " ".join(f"{a['act']} {a['sections']}" for a in acts)
        confirm = terms_by_company.get(c["company_id"], [])
        side = role([p["name"] for p in pets], [p["name"] for p in resps], confirm)
        lead_pet = pets[0]["name"] if pets else ""
        lead_resp = next((p["name"] for p in resps if is_company(p["name"], confirm)), resps[0]["name"] if resps else "")
        if side == "petitioner":
            lead_pet = next((p["name"] for p in pets if is_company(p["name"], confirm)), lead_pet)
            lead_resp = resps[0]["name"] if resps else ""
        values = {
            "case_type": d.case_type or None,
            "filing_number": d.filing_number or None,
            "filing_date": iso(d.filing_date),
            "registration_number": d.registration_number or None,
            "registration_date": iso(d.registration_date),
            "first_hearing_date": iso(d.first_hearing_date),
            "next_hearing_date": iso(d.next_hearing_date),
            "decision_date": iso(d.decision_date),
            "stage": d.case_stage or None,
            "court_judge": d.court_number_and_judge or d.court_name or None,
            "petitioners": pets,
            "respondents": resps,
            "petitioner_advocate": (pets[0]["advocate"] or None) if pets else None,
            "acts": acts,
            "title": title_of(lead_pet, lead_resp),
            "category": categorize(d.case_type, acts_text),
            "detail_fetched_at": now(),
            "detail_error": None,
            "updated_at": now(),
        }
        rs = c.get("raw_search") or {}
        named_in_search = is_company(rs.get("petitioner", ""), confirm) or is_company(rs.get("respondent", ""), confirm)
        is_main_mact = values["category"] == "motor_accident" and not re.search(
            r"misc|exe|ex\b|execution|appeal", (d.case_type or "").lower())
        if side != "unknown":
            values["company_role"] = side
            values["role_source"] = "case_page"
        elif named_in_search:
            # Some courts (Delhi MACT) list only claimants and the driver on the case page, while eCourts
            # still indexes the insurer as a party. The search hit is the evidence the company is a party.
            if is_main_mact:
                values["company_role"] = "respondent"  # the insurer is a respondent to a claim by law
                values["role_source"] = "inferred_mact"
                lead_resp = f"{lead_resp} and insurer" if lead_resp else ""
                values["title"] = title_of(lead_pet, lead_resp)
        elif pets or resps:
            # Neither the search row nor the case page names the company (e.g. "United India Traders").
            values["status"] = "not_ours"
        if d.decision_date:
            values["status"] = "disposed"
            values["disposal"] = disposal_of(ec.last_html)
        db.update("cases", f"id=eq.{c['id']}", values)
        hearings = [{"case_id": c["id"], "hearing_date": iso(h.hearing_date), "business_date": iso(h.business_date),
                     "purpose": h.purpose or None, "judge": h.judge or None} for h in d.history]
        if hearings:
            db.insert("hearings", hearings, upsert_on="case_id,hearing_date,business_date", ignore_duplicates=True)
        db.log(c["id"], "details_fetched", {"next_hearing": values["next_hearing_date"], "role": side})
        # Cases against us: what happened at each hearing, and any order PDFs the court uploaded.
        if values.get("company_role") == "respondent" and values.get("status") not in ("not_ours",) and ec.last_html:
            await orders.district_proceedings(ec, db, c["id"], ec.last_html)
            new_orders += await orders.district_orders(ec, db, c["id"], ec.last_html)
        if values.get("status") == "not_ours":
            db.log(c["id"], "auto_excluded", {"reason": "company not among parties"})
        run.stats["details_fetched"] += 1
    if new_orders:
        read = orders.read_with_ai(new_orders)
        run.note("info", f"{len(new_orders)} court orders added to case files, {read} read by AI")
    run.note("info", f"details fetched: {run.stats['details_fetched']}")


def refresh_suggestions(db: DB) -> None:
    advocates = db.select("advocates", "select=*")
    courts = {c["id"]: c for c in db.select("courts", "select=id,zone_id")}
    assigned = db.select("cases", "select=assigned_advocate_id,court_id&assigned_advocate_id=not.is.null"
                                  "&status=not.in.(disposed,not_ours)")
    load: dict[str, int] = {}
    court_count: dict[tuple[str, str], int] = {}
    for a in assigned:
        load[a["assigned_advocate_id"]] = load.get(a["assigned_advocate_id"], 0) + 1
        k = (a["assigned_advocate_id"], a["court_id"] or "")
        court_count[k] = court_count.get(k, 0) + 1
    open_cases = db.select("cases", "select=id,cnr,company_id,category,court_id,suggested&status=eq.new")
    changed = []
    for c in open_cases:
        zone = courts.get(c["court_id"], {}).get("zone_id")
        s = rank(c, advocates, load, court_count, zone)
        if s != c["suggested"]:
            changed.append({"id": c["id"], "cnr": c["cnr"], "company_id": c["company_id"], "suggested": s})
    # One upsert per 500 rows instead of one PATCH per case.
    for i in range(0, len(changed), 500):
        db.insert("cases", changed[i:i + 500], upsert_on="id")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger", default="schedule", choices=["schedule", "manual", "backfill"])
    ap.add_argument("--years", type=int, nargs="*", default=[date.today().year])
    ap.add_argument("--detail-budget", type=int, default=config.DETAIL_BUDGET)
    ap.add_argument("--only-details", action="store_true")
    # New filings are always pending. Including disposed cases makes the portal several times slower.
    ap.add_argument("--status", default="Pending", choices=["Pending", "Both"])
    # Once, after a parser fix: cases the old parser could not read are old filings, not new ones.
    # They are filed as already on file; any that started in the last 30 days still show as new.
    ap.add_argument("--baseline-all", action="store_true")
    ap.add_argument("--company", default="", help="only this company, by short name (Scan now)")
    ap.add_argument("--city", default="", help="only courts in this city (Scan now)")
    args = ap.parse_args()

    db = DB()
    run = Run(db, args.trigger)
    try:
        async with ECourts() as ec:
            if not args.only_details:
                await search_phase(run, ec, args.years, args.status, args.baseline_all, args.company, args.city)
            if not run.blocked:
                await detail_phase(run, ec, args.detail_budget)
        refresh_suggestions(db)
        orders.analyse_orders()
        orders.refresh_deadlines()
    except Exception as exc:
        run.note("error", f"run crashed: {type(exc).__name__}: {str(exc)[:300]}")
        run.stats["search_failures"] = max(run.stats["search_failures"], run.stats["searches"])
        run.finish()
        raise
    run.finish()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
