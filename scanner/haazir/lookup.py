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
from .nclt import BENCHES as NCLT_BENCHES, NCLT
from .matching import is_company, normalise

MAX_WORKERS = 18       # cloud runners per search (lookup.yml), each with its own address; GitHub Free runs 20 jobs at once, so 2 stay free for Find one case and small searches
LANES = 2              # court sessions per runner at once; eCourts blocked an address at about six
INDEX_DAYS = 14        # a state's list of court complexes is listed again after this long
STALE_MINUTES = 30     # a running search with no heartbeat for this long has lost its workers
NCDRC = 11000000       # the National Commission's id on e-Jagriti (searched with the consumer commissions)
REUSE_DAYS = 7         # a court's answer for a name and year is used again for this long instead of asking again
SMALL_SEARCH = 2000    # court searches up to this many: 3 court sessions per runner instead of 2 (trial)
LANES_SMALL = int(os.environ.get("HAAZIR_LANES_SMALL", "3"))
LIKELY = re.compile(r"m\.?\s?a\.?\s?c\.?\s?t|motor|accident|claims", re.I)  # accident claim tribunals: insurers' busiest courts

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


# Claims where the law makes the insurer a respondent: motor accident claims (MV Act s.166: MACT, MACP, MCOP, M.V.C.,
# MVOP, OP(MV)…) and employees' compensation claims (EC Act: WC, WCA, ECA). The court's own spellings, from the ICICI
# all-India search.
CLAIM_TYPE = re.compile(r"m\.?\s?a\.?\s?c(?!\.?\s?a\b)|motor|mot\.?\s?acc|m\.?\s?c\.?\s?o\.?\s?p|m\.?\s?v\.?\s?(c|op)\b"
                        r"|o\.?\s?p\.?\s?\(?\s?m\.?\s?v|\bw\.?\s?c\b|w\.?c\.?a|w\.c\.f|\be\.?\s?c\.?\s?a\b|employees?'?\s?comp"
                        r"|workm[ae]n|claim\s?(petition|case)", re.I)
# Not the claim itself: appeals, executions, reviews and miscellaneous applications, where an insurer can be either
# side. These are read from the case page instead.
NOT_CLAIM = re.compile(r"app|fao|revis|review|misc|\bm\.?\s?a\b(?!\.?\s?c)|macma|macm\b|\bmis\b|mjc|exe|execution|darkhast|dkst|\bep\b|\bex\b"
                       r"|transfer|restor", re.I)
INSURER = re.compile(r"insu|insr|assur|\bins\b|\bins\.|lombard|ergo|allianz|tokio|sompo|generali|chola|iffco|sundaram"
                     r"|\bdigit\b|acko|\bgic\b|reliance gen|new india|united india|oriental|national ins|liberty gen|magma"
                     r"|kotak gen|sbi gen|tata aig|shriram gen|raheja|zuno|navi gen", re.I)


def insurer_by_law(term: str, petitioner: str, respondent: str, case_type: str) -> bool:
    """An insurer named in an accident or employees' compensation claim: always a respondent."""
    ct = case_type or ""
    return bool(CLAIM_TYPE.search(ct) and not NOT_CLAIM.search(ct)
                and (INSURER.search(term or "") or INSURER.search(f"{petitioner} {respondent}")))


def placed(term: str, petitioner: str, respondent: str, case_type: str = "") -> tuple[str, str, str]:
    """(petitioner, respondent, side) for a row of a court's list. The list puts the one name it shows in the
    petitioner column; when the law makes that name the respondent, it is moved there."""
    side = side_of(term, petitioner, respondent, case_type)
    pet, resp = (petitioner or "").strip(), (respondent or "").strip()
    if side == "respondent" and pet and not resp:
        pet, resp = "", pet
    return pet, resp, side


def side_of(term: str, petitioner: str, respondent: str, case_type: str = "") -> str:
    """Which side the searched name is on, as far as the court's list shows it.

    The list sometimes shows only the matched party ("SBI General Insurance vs"), and then its position says
    nothing about the side: "unknown", until the case page is read (haazir.fill). An insurer in an accident or
    employees' compensation claim is a respondent by law."""
    on_r, on_p = is_company(respondent or "", [term]), is_company(petitioner or "", [term])
    if (on_r or on_p) and insurer_by_law(term, petitioner, respondent, case_type):
        return "respondent"
    if not (petitioner or "").strip() or not (respondent or "").strip():
        return "unknown"
    return "respondent" if on_r else "petitioner" if on_p else "unknown"


def year_of(number: str | None) -> int | None:
    m = re.search(r"(\d{4})\s*$", number or "")
    return int(m.group(1)) if m else None


def counts(db: DB, lookup_id: str, fresh: bool = False) -> dict:
    """The search's progress, counted from the tables so the workers never overwrite each other's numbers. Shared
    and up to 20 seconds old, or counted afresh (fresh=True) where a decision depends on it: finishing, carrying on."""
    fn = "lookup_counts_now" if fresh else "lookup_counts"
    return db._check(db.http.post(f"/rpc/{fn}", json={"lid": lookup_id})) or {}


def state_found(db: DB, lookup_id: str, state: str) -> int:
    """District court cases this search has found in one state (for its district_cap)."""
    r = db.http.get(f"/lookup_results?select=id&lookup_id=eq.{lookup_id}&forum=eq.district&known=eq.false&state=eq.{quote(state)}",
                    headers={"Prefer": "count=exact", "Range": "0-0"})
    return int((r.headers.get("content-range") or "*/0").split("/")[-1] or 0)


# ---------------------------------------------------------------- the court list

def task_key(forum: str, p: dict) -> str | None:
    """The court a court search asks, the same in every search: its answers are kept and reused by this key."""
    if forum == "district":
        c = p["court"]
        est = (c.get("est_codes") or "") if c.get("needs_est") else ""
        return f"d|{c['state_code']}|{c['dist_code']}|{c['complex_code']}|{est}"
    if forum == "high_court":
        return f"h|{p['code']}|{p['bench']}"
    if forum == "consumer":
        return f"c|{p['cid']}"
    if forum == "nclt":
        return f"n|{p['bench']}"
    return None


def complex_of(key: str) -> str:
    """A district key without its establishment: the complex a court list names."""
    return key.rsplit("|", 1)[0] if key.startswith("d|") else key


def complex_scores(scores: dict[str, tuple[int, int]]) -> dict[str, tuple[int, int]]:
    """Answers are kept per establishment; courts are ordered per complex."""
    out: dict[str, tuple[int, int]] = {}
    for k, (m, e) in scores.items():
        c = complex_of(k)
        a, b = out.get(c, (0, 0))
        out[c] = (a + m, b + e)
    return out


def likely_first(entries: list[dict], forum: str, scores: dict[str, tuple[int, int]]) -> list[dict]:
    """Courts most likely to have the name's cases first (stable, so states keep their own order): where it was
    found before, then accident claim tribunals, then the courts busiest for every name searched lately."""
    def order(e: dict):
        k = task_key(forum, e)
        mine, everyone = scores.get(complex_of(k) if k else "", (0, 0))
        return (-mine, not LIKELY.search(e.get("name") or ""), -everyone)
    return sorted(entries, key=order)


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
                payload = {**e, "court": court}
                rows.append({"lookup_id": lookup_id, "seq": seq, "forum": "district", "year": y, "payload": payload,
                             "key": task_key("district", payload)})
                seq += 1
    return rows


def ranked(rows: list[dict]) -> list[dict]:
    """Each court search's turn within its state and kind of court (0, 1, 2...), in the order written. Workers take
    turn 0 of every state, then turn 1..., so all states fill in together (claim_lookup_task orders by rank)."""
    seen: dict[tuple, int] = {}
    for r in rows:
        key = (r["forum"], r["payload"].get("state"))
        r["rank"] = 0 if r["forum"] == "index" else seen.get(key, 0)
        seen[key] = r["rank"] + 1
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
    forums = ",".join(f for f in row["forums"] if f in ("district", "high_court", "consumer", "nclt")) + (",ncdrc" if "consumer" in row["forums"] else "")
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
    forums, dist = [f for f in row["forums"] if row.get("kind") != "advocate" or f != "consumer"], row.get("district_code")
    whole = row["state_code"] == "all"
    names = wanted_names(row)
    wanted = {normalise(n) for n in names}
    out: dict = {"index": [], "district": [], "high_court": [], "consumer": [], "nclt": []}
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
        if not dist:  # the National Commission (NCDRC) hears appeals from every state
            out["consumer"].append({"cid": NCDRC, "name": "National Commission (NCDRC)", "is_state": True, "st_id": NCDRC, "state": None})
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
    if "nclt" in forums:
        # Every bench, whatever the place: a company's NCLT cases follow where it is registered.
        out["nclt"] = [{"bench": b, "name": f"NCLT {n}", "state": None} for b, n in NCLT_BENCHES.items()]
    return out


def tasks_of(db: DB, lookup_id: str) -> list[dict]:
    return db.select("lookup_tasks", f"select=*&lookup_id=eq.{lookup_id}&order=seq")


def plan_rows(row: dict, courts: dict, scores: dict[str, tuple[int, int]] | None = None) -> list[dict]:
    """The search's court searches, in the order workers take them: states still to be listed first (so listing
    starts at once), then High Courts, consumer commissions and district courts, one place from each state in turn,
    the likeliest courts of each state first (scores: court_scores).
    Pure (no network, no database), so haazir.search_check can test every kind of search without running one."""
    lid = row["id"]
    years = sorted(row["years"], reverse=True)
    sc = complex_scores(scores or {})
    index, district, hc, cons, nclt = courts["index"], courts["district"], courts["high_court"], courts["consumer"], courts.get("nclt", [])
    rows, seq = [], 0
    for p in index:
        rows.append({"lookup_id": lid, "seq": seq, "forum": "index", "year": years[0], "payload": p, "key": None})
        seq += 1
    for forum, entries in (("high_court", hc), ("consumer", cons), ("nclt", nclt)):
        for e in mixed(likely_first(entries, forum, sc)):
            for y in years:
                rows.append({"lookup_id": lid, "seq": seq, "forum": forum, "year": y, "payload": e, "key": task_key(forum, e)})
                seq += 1
    return ranked(rows + district_rows(mixed(likely_first(district, "district", sc)), years, lid, seq))


def scores_for(db: DB, term: str) -> dict[str, tuple[int, int]]:
    rows = db._check(db.http.post("/rpc/court_scores", json={"t": normalise(term)})) or []
    return {r["key"]: (int(r["mine"] or 0), int(r["everyone"] or 0)) for r in rows}


def answer_term(row: dict) -> str:
    """What a court's answer is kept under: the name searched, and whether it was a party or an advocate."""
    return ("adv:" if row.get("kind") == "advocate" else "") + normalise(row["party"])


def reuse(db: DB, row: dict) -> int:
    """Court searches answered for this name in the last REUSE_DAYS days: done at once, their cases copied in."""
    return int(db._check(db.http.post("/rpc/reuse_tasks", json={"lid": row["id"], "t": answer_term(row),
                                                                "days": REUSE_DAYS})) or 0)


def write_tasks(db: DB, rows: list[dict]) -> None:
    for i in range(0, len(rows), 500):
        db.insert("lookup_tasks", rows[i:i + 500], upsert_on="lookup_id,seq", ignore_duplicates=True)


async def plan(db: DB, row: dict) -> int:
    """List the search's courts and write its court searches, then take the ones answered lately from earlier
    searches. Returns how many court searches were written."""
    rows = plan_rows(row, await area(db, row), scores_for(db, row["party"]))
    write_tasks(db, rows)
    n = reuse(db, row)
    if n:
        print(f"  {n} court searches answered by searches in the last {REUSE_DAYS} days", flush=True)
    return len(rows)


def expected(db: DB, row: dict, c: dict) -> int:
    """Open court searches, counting the ones states still to be listed will add (from their last listing, however
    old), so a search that starts by listing states still gets its full set of workers."""
    n = int(c.get("open") or 0)
    if not int(c.get("listing") or 0):
        return n
    years = len(row["years"])
    for t in db.select("lookup_tasks", f"select=id,payload&lookup_id=eq.{row['id']}&forum=eq.index&done=eq.false"):
        p = t["payload"]
        q = f"select=est_codes,needs_est&state_code=eq.{p['state_code']}" + (f"&dist_code=eq.{p['district_code']}" if p.get("district_code") else "")
        listed = db.select("court_index", q + "&order=state_code,dist_code,complex_code")
        n += sum(len([x for x in (r["est_codes"] or "").split(",") if x]) if r["needs_est"] else 1 for r in listed) * years if listed else MAX_WORKERS
    return n


def workers_for(open_tasks: int) -> int:
    """How many cloud workers to start: one per open court search, up to MAX_WORKERS."""
    return max(1, min(MAX_WORKERS, open_tasks)) if open_tasks else 0


def start(db: DB, row: dict) -> bool:
    """Mark a search running, unless someone stopped it meanwhile."""
    return bool(db.update("lookups", f"id=eq.{row['id']}&status=in.(queued,running)",
                          {"status": "running", "started_at": row.get("started_at") or now(), "error": None, "heartbeat": now()}, returning="id"))


async def prepare(db: DB, row: dict) -> int:
    """First run of a search: a fresh results list with the cases Haazir already knows, then its court searches.
    Returns how many court searches are open."""
    if not db.select("lookup_tasks", f"select=id&lookup_id=eq.{row['id']}&limit=1"):
        db.http.delete(f"/lookup_results?lookup_id=eq.{row['id']}")
        db.update("lookups", f"id=eq.{row['id']}", {"progress": {"now": "Getting the list of courts ready", "done": 0, "total": 0, "found": 0}})
        known = add_known(db, row)
        n = await plan(db, row)
        print(f"{row['party']} · {row['state_name']} {row.get('district_name') or ''}: {n} court searches, {known} cases already known", flush=True)
    c = counts(db, row["id"], fresh=True)
    db.update("lookups", f"id=eq.{row['id']}&status=eq.running",
              {"progress": {**c, "now": "Searching the courts"}, "heartbeat": now()})
    return expected(db, row, c)


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
        self.advocate = row.get("kind") == "advocate"  # an advocate's cases, not a party's
        self.answer_term = answer_term(row)
        self.scores: dict[str, tuple[int, int]] | None = None  # court_scores, fetched when a state is listed

    def claim(self, lane: str) -> dict | None:
        rows = self.db._check(self.db.http.post("/rpc/claim_lookup_task", json={"lid": self.row["id"], "worker": f"{self.worker}/{lane}"})) or []
        return rows[0] if rows else None

    def release(self, task: dict, **extra) -> None:
        self.db.update("lookup_tasks", f"id=eq.{task['id']}", {"claimed_by": None, "claimed_at": None, **extra})

    def report(self, force: bool = False) -> None:
        """Shared progress and heartbeat; finds out when a person stopped the search from the dashboard."""
        if not force and time.time() - self.last_save < 15:
            return
        try:
            c = counts(self.db, self.row["id"])
        except Exception as exc:  # the database is busy for a moment: report next time, keep searching
            print(f"  {self.worker}: progress not saved ({type(exc).__name__}); carrying on", flush=True)
            self.last_save = time.time()
            return
        listing = int(c.get("listing") or 0)
        now_text = f"Listing courts in {listing} state{'s' if listing != 1 else ''} while searching" if listing else self.now_text
        try:
            saved = self.db.update("lookups", f"id=eq.{self.row['id']}&status=eq.running",
                                   {"progress": {**c, "now": now_text, "more": c.get("held") or 0}, "heartbeat": now()}, returning="id")
            self.stopped = self.stopped or not saved
        except Exception as exc:
            print(f"  {self.worker}: progress not saved ({type(exc).__name__}); carrying on", flush=True)
        self.last_save = time.time()

    def keep(self, rows: list[dict], state: str | None) -> tuple[int, list[str]]:
        """Save a court's rows; returns how many are new to this search, and the CNRs of all it kept."""
        rows = [{**r, "lookup_id": self.row["id"], "state": state} for r in rows if self.advocate or not self.short or r["side"] != "unknown"]
        n = 0
        # Count only rows new to this search: a case can appear in two courts' lists.
        for i in range(0, len(rows), 500):
            n += len(self.db.insert("lookup_results", rows[i:i + 500], upsert_on="lookup_id,cnr", ignore_duplicates=True,
                                    returning="cnr"))
        return n, [r["cnr"] for r in rows]

    def finish(self, task: dict, found: int, failed: bool, hits: int = 0, cnrs: list[str] | None = None) -> None:
        """Mark a court search done and keep the court's answer for the name (reused for REUSE_DAYS days)."""
        self.db._check(self.db.http.post("/rpc/finish_lookup_task", json={
            "tid": task["id"], "n_found": found, "did_fail": failed, "n_hits": hits, "t": self.answer_term, "found_cnrs": cnrs or []}))

    def listing(self) -> bool:
        """A state's courts are being listed right now (by another lane or worker)."""
        try:
            return bool(self.db.select("lookup_tasks", f"select=id&lookup_id=eq.{self.row['id']}&forum=eq.index&done=eq.false&limit=1"))
        except Exception:
            return False

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
        if self.scores is None:
            self.scores = complex_scores(scores_for(self.db, self.term))
        rows = ranked(district_rows(mixed(likely_first(entries, "district", self.scores)), years, self.row["id"],
                                    1_000_000 + task["seq"] * 200_000))
        for i in range(0, len(rows), 500):
            self.db.insert("lookup_tasks", rows[i:i + 500], upsert_on="lookup_id,seq", ignore_duplicates=True)
        reused = reuse(self.db, self.row)
        self.finish(task, 0, False)
        print(f"  listed {p['state']}: {len(entries)} court complexes, {len(rows)} court searches"
              + (f", {reused} answered lately" if reused else ""), flush=True)

    async def district(self, ec: ECourts, task: dict) -> None:
        t, year = task["payload"], task["year"]
        if self.at_cap(t.get("state") or ""):
            self.release(task, held=True)  # this state has its share for now; "Load more" frees it
            return
        self.now_text = f"{t['name']}, {t['district']} · {year}"
        failed = False
        try:
            res = await (ec.search_advocate(t["court"], self.term) if self.advocate else ec.search(t["court"], self.term, year, "Pending"))
        except Blocked:
            raise
        except Exception as exc:  # one court failing (layout, timeout) must not stop the rest
            failed, res = True, []
            print(f"  failed {t['name']} {year}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        n, cnrs = self.keep([{"forum": "district", "court": t["name"], "district": t["district"], "cnr": r.cnr_number, "case_number": r.case_number or None,
                              "case_type": r.case_type or None, "year": year_of(r.case_number) or year,
                              **({"petitioner": r.petitioner, "respondent": r.respondent, "side": "unknown"} if self.advocate else
                                 dict(zip(("petitioner", "respondent", "side"), placed(self.term, r.petitioner, r.respondent, r.case_type))))}
                             for r in res if r.cnr_number], t.get("state"))
        self.finish(task, n, failed, len(res), cnrs)
        print(f"  {t['name']} ({t['district']}) · {year}: {len(res)}", flush=True)

    async def high_court(self, hc: HighCourt, task: dict) -> None:
        t, year = task["payload"], task["year"]
        self.now_text = f"{t['name']} · {year}"
        failed = False
        try:
            res = await (hc.search_advocate(t["code"], t["bench"], self.term) if self.advocate else hc.search(t["code"], t["bench"], self.term, year))
        except Exception as exc:
            failed, res = True, []
            print(f"  failed {t['name']} {year}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        n, cnrs = self.keep([{"forum": "high_court", "court": t["name"], "district": None, "cnr": r.cnr_number,
                              "case_number": f"{r.case_type}/{r.case_number}" if r.case_type else r.case_number, "case_type": r.case_type or None,
                              "year": year_of(r.case_number) or year,
                              **({"petitioner": r.petitioner, "respondent": r.respondent, "side": "unknown"} if self.advocate else
                                 dict(zip(("petitioner", "respondent", "side"), placed(self.term, r.petitioner, r.respondent, r.case_type))))}
                             for r in res if r.cnr_number], t.get("state"))
        self.finish(task, n, failed, len(res), cnrs)
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
                out.append({"forum": "ncdrc" if t["cid"] == NCDRC else "consumer", "court": t["name"], "district": None, "cnr": r["caseNumber"], "case_number": r["caseNumber"],
                            "case_type": "Consumer complaint", "year": year_of(r["caseNumber"]) or year,
                            "petitioner": (r.get("complainantName") or "").strip(), "respondent": (r.get("respondentName") or "").strip(),
                            "side": "respondent" if on_r else "petitioner" if on_c else "unknown"})
        n, cnrs = self.keep(out, t.get("state"))
        self.finish(task, n, failed, len(rows), cnrs)
        print(f"  {t['name']} · {year}: {len(rows)}", flush=True)

    def nclt(self, nc: NCLT, task: dict) -> None:
        t, year = task["payload"], task["year"]
        self.now_text = f"{t['name']} · {year}"
        failed = False
        try:
            rows = nc.advocate(self.term, t["bench"], year) if self.advocate else nc.party(self.term, t["bench"], year)
        except Exception as exc:
            failed, rows = True, []
            print(f"  failed {t['name']} {year}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
        out = []
        for r in rows:
            if not r["filing_no"]:
                continue
            on_p, on_r = (False, False) if self.advocate else (is_company(r["petitioner"], [self.term]), is_company(r["respondent"], [self.term]))
            out.append({"forum": "nclt", "court": t["name"], "district": None, "cnr": f"NCLT{r['filing_no']}", "case_number": r["case_number"],
                        "case_type": r["case_type"], "year": year, "petitioner": r["petitioner"], "respondent": r["respondent"],
                        "side": "respondent" if on_r else "petitioner" if on_p else "unknown"})
        n, cnrs = self.keep(out, None)
        self.finish(task, n, failed, len(rows), cnrs)
        print(f"  {t['name']} · {year}: {len(rows)}", flush=True)

    async def lane(self, name: str, deadline: float) -> None:
        """Take court searches one after another until none are left, time runs out, or the search is stopped."""
        async with contextlib.AsyncExitStack() as stack:
            ec = hc = ej = nc = None
            while time.time() < deadline and not self.stopped and not self.blocked:
                try:
                    task = self.claim(name)
                except Exception as exc:  # the database is busy for a moment: wait and ask again, never give up the lane
                    print(f"  {self.worker}/{name}: could not take a court ({type(exc).__name__}); trying again shortly", flush=True)
                    await asyncio.sleep(15)
                    continue
                if not task:
                    # States still being listed will add court searches: wait for them rather than leave (0.8).
                    if self.listing():
                        self.report()
                        await asyncio.sleep(10)
                        continue
                    return
                try:
                    if task["forum"] in ("district", "index"):
                        ec = ec or await stack.enter_async_context(ECourts())
                        await (self.index(ec, task) if task["forum"] == "index" else self.district(ec, task))
                    elif task["forum"] == "high_court":
                        hc = hc or await stack.enter_async_context(HighCourt())
                        await self.high_court(hc, task)
                    elif task["forum"] == "nclt":
                        nc = nc or NCLT()
                        await asyncio.to_thread(self.nclt, nc, task)
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
    c = counts(db, row["id"], fresh=True)
    if int(c.get("open") or 0) or int(c.get("listing") or 0):
        return
    held = int(c.get("held") or 0)
    progress = {**c, "now": "", "more": held, "capped": c.get("capped") or []}
    if row["state_code"] == "all":
        # Watching an all-India search adds only the courts where cases were found.
        summary = db._check(db.http.post("/rpc/lookup_summary", json={"lid": row["id"]})) or {}
        progress["courts"] = summary.get("court_count") or len(summary.get("courts") or [])
    if db.update("lookups", f"id=eq.{row['id']}&status=eq.running", {"status": "done", "finished_at": now(), "progress": progress}, returning="id") and not held:
        # The list of court searches was only the search's to-do list (often the biggest thing in the database):
        # once every court is done it goes, and the results and totals stay. Kept while "Load more" still needs it,
        # and the courts that did not answer stay for "Search them again" (retry_lookup).
        db.http.delete(f"/lookup_tasks?lookup_id=eq.{row['id']}&failed=eq.false")


def lanes_for(c: dict) -> int:
    """Court sessions per runner: 3 for small searches (a trial: eCourts blocked an address at about six), else 2."""
    return LANES_SMALL if int(c.get("total") or 0) <= SMALL_SEARCH else LANES


async def work(db: DB, row: dict, worker: str, deadline: float, lanes: int | None = None) -> None:
    job = Job(db, row, worker)
    job.report(force=True)
    if job.stopped:
        return
    lanes = lanes or lanes_for(counts(db, row["id"]))
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
        k = min(workers_for(open_tasks), rows[0].get("workers") or MAX_WORKERS)  # a retry runs on fewer machines
        output(id=rows[0]["id"], workers=json.dumps(list(range(1, k + 1))))
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
        if rows and int(counts(db, args.id, fresh=True).get("open") or 0):
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
