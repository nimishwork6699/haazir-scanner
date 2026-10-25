"""Fill blank parties and sides: "ICICI Lombard vs ___" and "Side not shown".

    python -m haazir.fill rules [--apply]   # once, after a rule change: decide sides by law and from search lists
    python -m haazir.fill run --minutes 6   # read case pages by CNR, slowly, on one machine (the Worker)

eCourts' search lists often show only the name that was searched for, so neither the other party nor the side
is known from the list. Two ways to fill them in:

1. By law (rules): an insurer in a motor accident or employees' compensation claim is a respondent
   (lookup.insurer_by_law). The same rules fix Our cases saved from a search before sides were copied.
2. From the case page (run): each CNR's page lists every petitioner and respondent. Read once per CNR
   (table cnr_parties), then written to every search result and Our case with that CNR. Our cases keep their
   full read (hearings, orders) for the scanner: only parties, title and side are filled here.
"""

import argparse
import asyncio
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from .db import DB
from .ecourts import Blocked, ECourts
from .highcourt import HighCourt
from .lookup import insurer_by_law, placed, side_of
from .matching import is_company, role, title_of

PAUSE = 2.0   # seconds between case pages, on top of the portal client's own delay between requests
BATCH = 20


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def chunks(ids: list[str], n: int = 200):
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def patch_many(db: DB, table: str, groups: dict[tuple, list[str]], fields: tuple[str, ...]) -> int:
    """One PATCH per group of rows getting the same values, instead of one per row."""
    n = 0
    for values, ids in groups.items():
        for part in chunks(ids):
            db.update(table, f"id=in.({','.join(part)})", dict(zip(fields, values)))
            n += len(part)
    return n


def confirm_terms(db: DB) -> dict[str, list[str]]:
    confirm: dict[str, list[str]] = {}
    for t in db.select("watch_terms", "select=company_id,confirm"):
        confirm.setdefault(t["company_id"], []).extend(t["confirm"])
    return confirm


def case_side(c: dict, confirm: list[str]) -> tuple[str, str]:
    """Side of an Our case from what the search list showed, or by law. ("unknown", "unknown") if neither."""
    rs = c.get("raw_search") or {}
    pet, resp = (rs.get("petitioner") or "").strip(), (rs.get("respondent") or "").strip()
    on_p, on_r = is_company(pet, confirm), is_company(resp, confirm)
    if (on_p or on_r) and insurer_by_law(" ".join(confirm), pet, resp, c.get("case_type") or ""):
        return "respondent", "by_law"
    if pet and resp and on_p != on_r:
        return ("petitioner" if on_p else "respondent"), "search_list"
    return "unknown", "unknown"


def rules(db: DB, apply: bool) -> None:
    # Search results.
    terms = {l["id"]: l["party"] for l in db.select("lookups", "select=id,party")}
    rows = db.select("lookup_results", "select=id,lookup_id,petitioner,respondent,case_type,side"
                                       "&or=(side.eq.unknown,and(side.eq.respondent,or(respondent.is.null,respondent.eq.),petitioner.neq.))")
    groups: dict[tuple, list[str]] = defaultdict(list)
    moved: dict[tuple, list[str]] = defaultdict(list)
    for r in rows:
        pet, resp, s = placed(terms.get(r["lookup_id"], ""), r["petitioner"], r["respondent"], r["case_type"] or "")
        if s == "unknown":
            continue
        if resp != (r["respondent"] or "").strip():
            moved[(pet, resp, s)].append(r["id"])
        elif s != r["side"]:
            groups[(s,)].append(r["id"])
    print(f"search results: {sum(len(v) for v in groups.values())} sides decided, "
          f"{sum(len(v) for v in moved.values())} names moved to the respondent side")
    if apply:
        patch_many(db, "lookup_results", groups, ("side",))
        patch_many(db, "lookup_results", moved, ("petitioner", "respondent", "side"))

    # Our cases saved from a search without their side.
    confirm = confirm_terms(db)
    cases = db.select("cases", "select=id,company_id,case_type,raw_search,company_role&company_role=eq.unknown"
                               "&role_source=eq.unknown&status=neq.not_ours&entry=eq.court")
    groups = defaultdict(list)
    log = []
    for c in cases:
        side, source = case_side(c, confirm.get(c["company_id"], []))
        if side != "unknown":
            groups[(side, source)].append(c["id"])
            log.append({"case_id": c["id"], "actor": "scanner", "action": "side_rechecked",
                        "meta": {"from": "unknown", "to": side, "source": source}})
    print(f"Our cases: {len(cases)} with side not known; decided now: "
          f"{ {f'{k[0]} ({k[1]})': len(v) for k, v in groups.items()} or 'none'}")
    if apply:
        patch_many(db, "cases", groups, ("company_role", "role_source"))
        for i in range(0, len(log), 500):
            db.insert("activity", log[i:i + 500])
    # Our cases a respondent by law whose one listed name sits on the petitioner side: move it.
    one_sided = db.select("cases", "select=id,petitioners,respondents&role_source=eq.by_law&respondents=eq.[]")
    print(f"Our cases: {len(one_sided)} names moved to the respondent side")
    if apply:
        for c in one_sided:
            name = ((c["petitioners"] or [{}])[0] or {}).get("name") or ""
            if name:
                db.update("cases", f"id=eq.{c['id']}", {"petitioners": [], "respondents": c["petitioners"], "title": title_of("", name)})
    if not apply:
        print("(dry run: nothing written; add --apply)")


def lead(names: list[str], term: list[str]) -> str:
    """The name to show for one side: the searched company if it is there, else the first party, "+ N more"."""
    if not names:
        return ""
    first = next((n for n in names if is_company(n, term)), names[0])
    return f"{first} + {len(names) - 1} more" if len(names) > 1 else first


def apply_parties(db: DB, cnr: str, pets: list[dict], resps: list[dict], confirm: dict[str, list[str]],
                  terms: dict[str, str]) -> int:
    """Write one case page's parties to every search result and unread Our case with this CNR."""
    pn, rn = [p["name"] for p in pets if p["name"]], [p["name"] for p in resps if p["name"]]
    n = 0
    for r in db.select("lookup_results", f"select=id,lookup_id,side,petitioner,respondent,case_type&cnr=eq.{cnr}"):
        term = [terms.get(r["lookup_id"], "")]
        side = role(pn, rn, term)
        if side == "unknown":
            side = r["side"] if r["side"] != "unknown" else side_of(term[0], r["petitioner"], r["respondent"], r["case_type"] or "")
        db.update("lookup_results", f"id=eq.{r['id']}", {"petitioner": lead(pn, term) or r["petitioner"],
                                                         "respondent": lead(rn, term) or r["respondent"], "side": side})
        n += 1
    for c in db.select("cases", f"select=id,company_id,company_role,role_source&cnr=eq.{cnr}&detail_fetched_at=is.null"):
        cf = confirm.get(c["company_id"], [])
        values = {"petitioners": pets, "respondents": resps, "title": title_of(lead(pn, cf), lead(rn, cf))}
        side = role(pn, rn, cf)
        if side != "unknown" and c["role_source"] in ("unknown", "search_list", "by_law"):
            values.update(company_role=side, role_source="case_page")
            if side != c["company_role"]:
                db.log(c["id"], "side_rechecked", {"from": c["company_role"], "to": side, "source": "case_page"})
        db.update("cases", f"id=eq.{c['id']}", values)
    return n


async def read_pages(db: DB, minutes: float) -> None:
    end = time.time() + minutes * 60
    confirm = confirm_terms(db)
    terms = {l["id"]: l["party"] for l in db.select("lookups", "select=id,party")}
    tries = {r["cnr"]: r["tries"] for r in db.select("cnr_parties", "select=cnr,tries&error=not.is.null")}
    done = failed = rows = 0
    async with ECourts() as ec:
        hc: HighCourt | None = None
        try:
            while time.time() < end:
                cnrs = db.http.post("/rpc/fill_next", json={"n": BATCH})
                cnrs = [x if isinstance(x, str) else x.get("fill_next") for x in (db._check(cnrs) or [])]
                if not cnrs:
                    print("Nothing left to fill.")
                    break
                for cnr in cnrs:
                    if time.time() >= end:
                        break
                    try:
                        if cnr[2:4] == "HC":
                            if hc is None:
                                hc = await HighCourt().__aenter__()
                            d, _ = await hc.detail(cnr)
                        else:
                            d = await ec.detail(cnr)
                        if not d or not (d.petitioners or d.respondents):
                            raise LookupError("no parties on the case page")
                    except Blocked:
                        print("eCourts is refusing requests: stopping until the next run.", flush=True)
                        return
                    except Exception as exc:
                        failed += 1
                        db.insert("cnr_parties", {"cnr": cnr, "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                                                  "tries": tries.get(cnr, 0) + 1, "fetched_at": now()}, upsert_on="cnr")
                        print(f"{cnr}: failed ({type(exc).__name__})", flush=True)
                        await asyncio.sleep(PAUSE)
                        continue
                    pets = [{"name": p.name, "advocate": p.advocate} for p in d.petitioners]
                    resps = [{"name": p.name, "advocate": p.advocate} for p in d.respondents]
                    db.insert("cnr_parties", {"cnr": cnr, "case_type": d.case_type or None, "petitioners": pets,
                                              "respondents": resps, "error": None, "fetched_at": now()}, upsert_on="cnr")
                    rows += apply_parties(db, cnr, pets, resps, confirm, terms)
                    done += 1
                    await asyncio.sleep(PAUSE)
        finally:
            if hc is not None:
                await hc.__aexit__(None, None, None)
            print(f"Case pages read: {done}, failed: {failed}; search results filled: {rows}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rules")
    r.add_argument("--apply", action="store_true")
    p = sub.add_parser("run")
    p.add_argument("--minutes", type=float, default=6)
    args = ap.parse_args()
    db = DB()
    if args.cmd == "rules":
        rules(db, args.apply)
    else:
        asyncio.run(read_pages(db, args.minutes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
