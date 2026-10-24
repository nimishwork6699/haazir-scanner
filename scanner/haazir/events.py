"""Turn each case's court record into events, and alert the legal team about new ones.

District courts rarely upload order PDFs (and some uploads are blank), but every hearing records its
purpose, the next purpose and the reason for an adjournment. A case moving to a new step is the event:
summons issued, appearance, evidence, final arguments, judgment due, decided, stayed, warrant. Our own
side asking for an adjournment is an event too: it is how the legal head sees whether the lawyer is moving.

    python -m haazir.events            # derive events for all cases, alert new ones on Telegram
    python -m haazir.events --quiet    # derive only (first run: marks history as already seen)
"""

import argparse
import html
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from . import config, telegram
from .db import DB

# First match wins, so later steps come before earlier ones.
STEPS: list[tuple[str, re.Pattern]] = [
    ("decided", re.compile(r"\bdisposed\b|\bdecided\b|judgment pronounced|\bdismissed\b|\bdecreed\b|award passed", re.I)),
    ("stayed", re.compile(r"stayed by higher court|\bstayed\b", re.I)),
    ("ex_parte", re.compile(r"ex[\s-]?parte", re.I)),
    ("not_served", re.compile(r"unserved|not served|returned unserved|notice received back", re.I)),
    ("served", re.compile(r"notice served|summons served|\bserved\b", re.I)),
    # District courts label one stage "Service of Summons/bailable warrant": that is summons service, not a warrant.
    ("warrant", re.compile(r"non[\s-]?bailable warrant|\bnbw\b|warrant issued", re.I)),
    # High Court "FOR ORDERS- DEFECT/DEFAULT CASES": the filing is waiting on defects to be cured.
    ("defects", re.compile(r"defe?c?t cases|defcet|default cases|\bdefects?\b", re.I)),
    ("judgment_due", re.compile(r"^\s*(orders?|judg[e]?ment)\s*$|order reserved|reserved for|judgment reserved", re.I)),
    ("final_arguments", re.compile(r"final arguments?", re.I)),
    ("evidence", re.compile(r"evidence", re.I)),
    ("appearance", re.compile(r"appearance|written statement|\breply\b|steps by parties|\bissues\b|\badmit\b", re.I)),
    ("notice_issued", re.compile(r"awaiting service|service of (notices?|summons)|summons/bailable|issuance of process|issue notice|notice awaited|cognizance", re.I)),
]

LABEL = {  # also the order of the Telegram digest
    "notice_issued": "Court has issued notice / summons",
    "not_served": "Notice came back unserved",
    "served": "Notice served",
    "defects": "Filing waiting on defects to be cured",
    "appearance": "Served: case at appearance or reply stage",
    "evidence": "Case moved to evidence",
    "final_arguments": "Case moved to final arguments",
    "judgment_due": "Judgment or order expected on the next date",
    "decided": "Case decided",
    "stayed": "Case stayed by a higher court",
    "ex_parte": "Court proceeded ex parte",
    "warrant": "Warrant issued",
    "our_adjournment": "Our side asked for an adjournment",
    # Read from the order PDFs by AI (web/src/lib/orders.ts).
    "our_absent": "Order says nobody appeared for us",
    "costs_imposed": "Costs imposed",
    "last_chance": "Last opportunity given",
    "compliance_due": "Court told us to do something",
    "stay_granted": "Stay granted",
    "stay_vacated": "Stay vacated",
}
# Worth a Telegram alert; the rest are kept for the case story.
ALERT = {"notice_issued", "not_served", "served", "judgment_due", "decided", "stayed", "ex_parte", "warrant", "our_adjournment",
         "our_absent", "costs_imposed", "last_chance", "compliance_due", "stay_granted", "stay_vacated"}


def step(text: str | None) -> str | None:
    t = (text or "").strip()
    if not t or len(t) < 3:
        return None
    return next((k for k, rx in STEPS if rx.search(t)), None)


def our_adjournment(reason: str | None, role: str) -> bool:
    r = (reason or "").lower()
    if "adjournment request by" not in r:
        return False
    by_defence = "defedant" in r or "defendant" in r or "respondent" in r
    return by_defence if role == "respondent" else (not by_defence and role == "petitioner")


def derive(db: DB) -> list[dict]:
    cases = {c["id"]: c for c in db.select("cases", "select=id,company_role,stage,status,decision_date,disposal,detail_fetched_at&status=neq.not_ours")}
    hearings: dict[str, list[dict]] = defaultdict(list)
    for h in db.select("hearings", "select=case_id,business_date,hearing_date,purpose,next_purpose,adjournment_reason&business_date=not.is.null"):
        if h["case_id"] in cases:
            hearings[h["case_id"]].append(h)
    out = []
    for cid, c in cases.items():
        hs = sorted(hearings.get(cid, []), key=lambda h: h["business_date"])
        prev = None
        for h in hs:
            k = step(h["purpose"])
            if k and k != prev:
                out.append({"case_id": cid, "kind": k, "event_date": h["business_date"], "source": "hearing", "evidence": h["purpose"]})
                prev = k
            nk = step(h["next_purpose"])
            if nk == "judgment_due":
                out.append({"case_id": cid, "kind": "judgment_due", "event_date": h["business_date"], "source": "hearing",
                            "evidence": f"next date {h['hearing_date'] or ''} for {h['next_purpose']}".strip()})
            if our_adjournment(h["adjournment_reason"], c["company_role"]):
                out.append({"case_id": cid, "kind": "our_adjournment", "event_date": h["business_date"], "source": "hearing", "evidence": h["adjournment_reason"]})
        # The stage eCourts shows now, when the hearings above do not already say it.
        k = step(c["stage"])
        if k and k != prev and not (k == "decided" and c["decision_date"]):
            when = hs[-1]["business_date"] if hs else (c["detail_fetched_at"] or "")[:10]
            if when:
                out.append({"case_id": cid, "kind": k, "event_date": when, "source": "stage", "evidence": c["stage"]})
        if c["status"] == "disposed" and c["decision_date"]:
            out.append({"case_id": cid, "kind": "decided", "event_date": c["decision_date"], "source": "status",
                        "evidence": f"Nature of disposal: {c['disposal']}" if c["disposal"] else "Disposed"})
    # One row per case, kind and date.
    uniq = {(e["case_id"], e["kind"], e["event_date"]): e for e in out}
    return list(uniq.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="derive only; mark everything as already alerted")
    args = ap.parse_args()
    db = DB()
    events = derive(db)
    for i in range(0, len(events), 500):
        db.insert("case_events", events[i:i + 500], upsert_on="case_id,kind,event_date", ignore_duplicates=True)
    now = datetime.now(timezone.utc).isoformat()
    since = (date.today() - timedelta(days=3)).isoformat()
    # History is context, not news: only events of the last 3 days are alerted.
    db.update("case_events", f"alerted_at=is.null&event_date=lt.{since}", {"alerted_at": now})
    if args.quiet:
        db.update("case_events", "alerted_at=is.null", {"alerted_at": now})
        print(f"events: {len(events)} derived, all marked as seen")
        return 0
    fresh = db.select("case_events", "select=id,case_id,kind,event_date,evidence,source,case:cases(title,case_number,company_role,"
                                     "court:courts(complex_name),company:companies(short_name))&alerted_at=is.null")
    # Only real moves are news: a step recorded at a hearing, or a decision. The current stage alone can be months old.
    fresh = [e for e in fresh if e["kind"] in ALERT and e["source"] in ("hearing", "status", "order")
             and (e.get("case") or {}).get("company_role") in ("respondent", "petitioner")]
    ops = next((r["value"] for r in db.select("settings", "select=value&key=eq.ops_telegram_chat_id")), None)
    if fresh and ops:
        by_kind: dict[str, list[dict]] = defaultdict(list)
        for e in fresh:
            by_kind[e["kind"]].append(e)
        lines = [f"<b>Haazir · what happened in court</b> ({len(fresh)})"]
        for k in [k for k in LABEL if k in by_kind]:
            lines.append(f"\n<b>{LABEL[k]}</b> ({len(by_kind[k])})")
            for e in by_kind[k][:6]:
                c = e["case"]
                lines.append(f"• {html.escape((c.get('title') or '')[:60])} · {html.escape(c.get('case_number') or '')} · {html.escape((c.get('company') or {}).get('short_name', ''))}")
            if len(by_kind[k]) > 6:
                lines.append(f"  and {len(by_kind[k]) - 6} more")
        if config.APP_URL:
            lines.append(f"\n<a href=\"{config.APP_URL}/\">Open Haazir</a>")
        try:
            telegram.send(int(ops), "\n".join(lines)[:4000])
        except Exception as exc:
            print("telegram failed:", exc)
    db.update("case_events", "alerted_at=is.null", {"alerted_at": now})
    print(f"events: {len(events)} derived, {len(fresh)} new alerted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
