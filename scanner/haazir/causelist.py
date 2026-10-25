"""Tomorrow's cause list for the legal head, each evening on Telegram: court by court, who appears for us,
what the date is for, and which cases have nobody. Advocates get their own reminder (haazir.reminders).

    python -m haazir.causelist            # tomorrow, once per date
    python -m haazir.causelist --force    # send again
"""

import argparse
import html
import re
import sys
from collections import defaultdict
from datetime import date, timedelta

from . import config, telegram
from .db import DB

# Same reading as web/src/lib/story.ts nextAction(), kept short for a phone screen.
ACTIONS: list[tuple[str, str, str]] = [
    (r"awaiting service|service of (notices?|summons)|summons/bailable|issue notice", "notice being served on us: brief the lawyer", "check summons is served"),
    (r"cognizance|issuance of process", "process issued: appear", "get summons served"),
    (r"written statement|reply|steps by parties", "file our written statement / reply", "their reply due"),
    (r"appearance", "appear and file vakalatnama", "appear"),
    (r"\bissues\b", "framing of issues", "framing of issues"),
    (r"(plaintiff|petitioner|prosecution|complainant).*evidence", "their evidence: cross-examine", "our evidence: witnesses ready"),
    (r"(defendant|defence|respondent).*evidence", "our evidence: witnesses ready", "their evidence: cross-examine"),
    (r"arguments", "arguments: submissions ready", "arguments: submissions ready"),
    (r"^\s*(orders?|judg[e]?ment)\s*$|for orders|reserved", "judgment expected: deposit or appeal ready", "judgment expected"),
    (r"part[\s-]?heard", "part heard: attend", "part heard: attend"),
]


def action(purpose: str | None, role: str) -> str:
    p = (purpose or "").strip()
    for rx, defend, file in ACTIONS:
        if re.search(rx, p, re.I):
            return defend if role == "respondent" else file
    return f"listed for {p.lower().rstrip('.')}" if p and not re.search(r"moved to courtroom|misc", p, re.I) else "listed for hearing"


def chunks(lines: list[str], limit: int = 3800) -> list[str]:
    out, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += ln + "\n"
    return out + ([cur] if cur.strip() else [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    db = DB()
    day = (date.today() + timedelta(days=1)).isoformat()
    ops = next((r["value"] for r in db.select("settings", "select=value&key=eq.ops_telegram_chat_id")), None)
    sent = next((r["value"] for r in db.select("settings", "select=value&key=eq.causelist_date")), None)
    if not ops:
        print("causelist: no ops chat set")
        return 0
    if sent == day and not args.force:
        print(f"causelist: {day} already sent")
        return 0
    cases = db.select("cases", "select=id,title,case_number,company_role,stage,court:courts(complex_name),company:companies(short_name),"
                               f"advocate:advocates(name)&next_hearing_date=eq.{day}&company_role=in.(respondent,petitioner)"
                               "&status=not.in.(disposed,not_ours)")
    purpose: dict[str, str] = {}
    ids = [c["id"] for c in cases]
    for i in range(0, len(ids), 100):
        for h in db.select("hearings", f"select=case_id,business_date,next_purpose&case_id=in.({','.join(ids[i:i + 100])})"
                                       "&business_date=not.is.null&next_purpose=not.is.null&order=business_date.desc"):
            purpose.setdefault(h["case_id"], h["next_purpose"])
    lines = message(cases, purpose, day)
    for part in chunks(lines):
        telegram.send(int(ops), part)
    db.insert("settings", {"key": "causelist_date", "value": day}, upsert_on="key")
    print(f"causelist: {day} sent, {len(cases)} hearings, {sum(1 for c in cases if not c.get('advocate'))} without a lawyer")
    return 0


# Hearings written out one by one; past this, the rest are counted court by court. With tens of thousands of cases
# a day can have 150+ hearings, which was 7 or 8 Telegram messages every evening.
SHOW = 30


def message(cases: list[dict], purpose: dict[str, str], day: str) -> list[str]:
    by_court: dict[str, list[dict]] = defaultdict(list)
    for c in cases:
        by_court[(c.get("court") or {}).get("complex_name") or "Court not known"].append(c)
    nobody = sum(1 for c in cases if not c.get("advocate"))
    when = date.fromisoformat(day).strftime("%a %d %b")
    lines = [f"<b>Haazir · cause list for {when}</b>",
             f"{len(cases)} hearing{'s' if len(cases) != 1 else ''}" + (f" · <b>{nobody} with no lawyer</b>" if nobody else " · all have a lawyer")]
    # Courts with an unmanned case first.
    shown, rest = 0, []
    for court, rows in sorted(by_court.items(), key=lambda kv: (-sum(1 for c in kv[1] if not c.get("advocate")), kv[0])):
        rows = sorted(rows, key=lambda c: bool(c.get("advocate")))
        if shown >= SHOW:
            rest.append((court, rows))
            continue
        if shown + len(rows) > SHOW:  # a big court: the first ones written out, the others counted below
            rows, later = rows[:SHOW - shown], rows[SHOW - shown:]
            rest.append((court, later))
        lines.append(f"\n<b>{html.escape(court.title())}</b>")
        for c in rows:
            who = html.escape(c["advocate"]["name"]) if c.get("advocate") else "❗ NO LAWYER"
            side = "vs us" if c["company_role"] == "respondent" else "by us"
            lines.append(f"• {html.escape((c.get('title') or '')[:55])} ({side}, {html.escape((c.get('company') or {}).get('short_name', ''))})\n"
                         f"  {who} · {html.escape(action(purpose.get(c['id']) or c.get('stage'), c['company_role']))}")
            shown += 1
    if rest:
        more = sum(len(rows) for _, rows in rest)
        lines.append(f"\n<b>And {more} more hearing{'s' if more != 1 else ''} in {len(rest)} court{'s' if len(rest) != 1 else ''}</b>")
        for court, rows in rest:
            free = sum(1 for c in rows if not c.get("advocate"))
            lines.append(f"• {html.escape(court.title())}: {len(rows)}" + (f", {free} with no lawyer" if free else ""))
    if not cases:
        lines.append("\nNo hearings listed for tomorrow.")
    if config.APP_URL:
        lines.append(f"\n<a href=\"{config.APP_URL}/hearings?side=all\">Open the cause list</a>")
    return lines


if __name__ == "__main__":
    sys.exit(main())
