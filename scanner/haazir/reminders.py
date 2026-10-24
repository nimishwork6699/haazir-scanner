"""The Telegram and email side of the advocate loop, run after every scan and each morning. Safe to repeat: every
message is sent once per case, advocate, kind and date. An advocate with Telegram gets it there (with buttons);
one with only an email gets it by email.

  1. Hearing in the next 48 hours  -> reminder with "I will appear" / "I can't attend" buttons
  2. Hearing date has passed        -> "How did it go?" with outcome buttons (Telegram only; answers handled by the dashboard)
  3. Deadline due in 3 days or less -> reminder to the assigned advocate
  4. Once a day                     -> summary for the legal team: hearings, overdue deadlines, backups needed

    python -m haazir.reminders
"""

import html
import re
import sys
from datetime import date, datetime, timedelta, timezone

from . import config, mail, telegram
from .db import DB

OUTCOME_BUTTONS = [[("Adjourned", "out|adjourned"), ("Evidence", "out|evidence")],
                   [("Arguments", "out|arguments"), ("Order reserved", "out|reserved")],
                   [("Disposed", "out|disposed"), ("Other", "out|other")]]


def fmt(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%d %b %Y")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def link(case_id: str) -> str:
    return f"\n<a href=\"{config.APP_URL}/cases/{case_id}\">Open in Haazir</a>" if config.APP_URL else ""


def channel(adv: dict) -> str | None:
    """Telegram when linked (it has buttons), else email when known, else nothing."""
    return "telegram" if adv.get("telegram_chat_id") else "email" if adv.get("email") and config.BREVO_API_KEY else None


def claim(db: DB, case_id: str, advocate_id: str | None, kind: str, ref_date: str, via: str = "telegram") -> dict | None:
    """Reserve the right to send this message once. None when it was already sent."""
    rows = db.insert("notifications", {"case_id": case_id, "advocate_id": advocate_id, "kind": kind, "ref_date": ref_date, "channel": via},
                     upsert_on="case_id,advocate_id,kind,ref_date", ignore_duplicates=True, returning="*")
    return rows[0] if rows else None


def deliver(db: DB, note: dict, adv: dict, text: str, case_id: str, prompt_kind: str | None = None,
            hearing_date: str | None = None, buttons=None, subject: str = "") -> bool:
    """Telegram (with buttons) when the advocate has it; otherwise the same text by email, with a link to the case."""
    try:
        if note.get("channel") == "email":
            button = ("Open the case", f"{config.APP_URL}/cases/{case_id}") if config.APP_URL else None
            heading, _, body = text.partition("\n")
            mail.send(adv["email"], subject or re.sub(r"<[^>]+>", "", heading),
                      mail.card(re.sub(r"<[^>]+>", "", heading), mail.from_telegram(LINK.sub("", body)), button), adv.get("name"))
        else:
            mid = telegram.send(adv["telegram_chat_id"], text, buttons)
            if prompt_kind:
                db.insert("tg_prompts", {"chat_id": adv["telegram_chat_id"], "message_id": mid, "case_id": case_id, "kind": prompt_kind,
                                         "hearing_date": hearing_date}, upsert_on="chat_id,message_id", ignore_duplicates=True)
        db.update("notifications", f"id=eq.{note['id']}", {"status": "sent", "sent_at": now(), "text": text})
        return True
    except Exception as exc:
        db.update("notifications", f"id=eq.{note['id']}", {"status": "failed", "error": str(exc)[:300]})
        return False


# The "Open in Haazir" line: email shows a button instead.
LINK = re.compile(r"\n<a href=\"[^\"]*\">Open in Haazir</a>")


EMPTY_BUSINESS = re.compile(r"^\s*(fp|f\.p\.?|work done as per order ?sheet|as per order ?sheet|order ?sheet|-+)\s*\.?\s*$", re.I)


def last_hearings(db: DB, case_id: str) -> str:
    """The case's last 3 hearings in a few lines (database function last_hearings, the same as the case page)."""
    rows = db._check(db.http.post("/rpc/last_hearings", json={"cid": case_id, "n": 3})) or []
    lines = []
    for h in rows:
        business = h.get("business") if h.get("business") and not EMPTY_BUSINESS.match(h["business"]) else None
        what = h.get("order_summary") or business or (f"listed for {h['purpose'].lower()}" if h.get("purpose") else "no order or note yet")
        if len(what) > 160:
            what = what[:157].rsplit(" ", 1)[0] + "…"
        extra = " Nobody appeared for us." if h.get("nobody_for_us") else ""
        lines.append(f"• {fmt(h['date'])}: {html.escape(what)}{extra}")
    return ("\n\n<b>Last hearings</b>\n" + "\n".join(lines)) if lines else ""


def main() -> int:
    db = DB()
    today = date.today()
    cols = ("select=id,title,case_number,next_hearing_date,assigned_advocate_id,court:courts(complex_name),"
            "advocate:advocates(id,name,telegram_chat_id,email)")
    stats = {"reminders": 0, "outcome_asks": 0, "deadline_reminders": 0, "digest": 0}

    # 1) Hearings in the next 48 hours.
    for c in db.select("cases", f"{cols}&assigned_advocate_id=not.is.null&next_hearing_date=gte.{today.isoformat()}"
                                f"&next_hearing_date=lte.{(today + timedelta(days=2)).isoformat()}&status=not.in.(disposed,not_ours)"):
        adv = c.get("advocate") or {}
        if not (via := channel(adv)) or not (note := claim(db, c["id"], adv["id"], "reminder", c["next_hearing_date"], via)):
            continue
        days = (date.fromisoformat(c["next_hearing_date"]) - today).days
        when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
        court = (c.get("court") or {}).get("complex_name", "")
        text = (f"<b>Hearing {when}</b>\n{html.escape(c['title'] or '')}\n{html.escape(court)} · {html.escape(c['case_number'] or '')}\n"
                f"<b>{fmt(c['next_hearing_date'])}</b>" + last_hearings(db, c["id"])
                + ("\n\nWill you appear?" if via == "telegram" else "\n\nIf you cannot attend, tell the legal team today.") + link(c["id"]))
        if deliver(db, note, adv, text, c["id"], "attendance", c["next_hearing_date"],
                   [[("✅ I will appear", "att|y"), ("❌ I can't attend", "att|n")]], f"Hearing {when}: {c['title'] or ''}"):
            db.log(c["id"], "reminder_sent", {"advocate": adv["name"], "hearing": c["next_hearing_date"]}, actor="system")
            stats["reminders"] += 1

    # 2) Hearings that took place in the last 3 days, for which we sent a reminder: ask how it went.
    since = (today - timedelta(days=3)).isoformat()
    for n in db.select("notifications", f"select=case_id,advocate_id,ref_date&kind=eq.reminder&status=eq.sent"
                                        f"&ref_date=gte.{since}&ref_date=lt.{today.isoformat()}"):
        if db.select("hearing_reports", f"select=id&case_id=eq.{n['case_id']}&hearing_date=eq.{n['ref_date']}&outcome=not.is.null"):
            continue
        adv = next(iter(db.select("advocates", f"select=id,name,telegram_chat_id&id=eq.{n['advocate_id']}")), None)
        c = next(iter(db.select("cases", f"select=id,title,case_number,status&id=eq.{n['case_id']}")), None)
        if not adv or not adv.get("telegram_chat_id") or not c or c["status"] == "not_ours":
            continue
        if not (note := claim(db, c["id"], adv["id"], "outcome_ask", n["ref_date"])):
            continue
        text = (f"<b>How did the hearing go?</b>\n{html.escape(c['title'] or '')} · {html.escape(c['case_number'] or '')}\n"
                f"Hearing on {fmt(n['ref_date'])}. Tap the outcome, then reply with the next date or send a voice note.")
        if deliver(db, note, adv, text, c["id"], "outcome", n["ref_date"], OUTCOME_BUTTONS):
            stats["outcome_asks"] += 1

    # 3) Deadlines due within 3 days (or overdue by up to a week) on assigned cases.
    soon = (today + timedelta(days=3)).isoformat()
    for d in db.select("deadlines", f"select=id,case_id,title,due_date,basis,case:cases(title,case_number,assigned_advocate_id,"
                                    f"advocate:advocates(id,name,telegram_chat_id,email))&status=eq.open&due_date=lte.{soon}"
                                    f"&due_date=gte.{(today - timedelta(days=7)).isoformat()}"):
        c = d.get("case") or {}
        adv = c.get("advocate") or {}
        if not (via := channel(adv)) or not (note := claim(db, d["case_id"], adv["id"], "deadline", d["due_date"], via)):
            continue
        overdue = d["due_date"] < today.isoformat()
        text = (f"<b>{'Overdue' if overdue else 'Deadline'}: {html.escape(d['title'])}</b>\n{html.escape(c.get('title') or '')} · "
                f"{html.escape(c.get('case_number') or '')}\nDue <b>{fmt(d['due_date'])}</b>\n<i>{html.escape(d['basis'][:200])}</i>"
                + link(d["case_id"]))
        if deliver(db, note, adv, text, d["case_id"]):
            stats["deadline_reminders"] += 1

    # 4) Daily summary for the legal team, on Telegram and by email (whichever are set).
    setting = {r["key"]: r["value"] for r in db.select("settings", "select=key,value&key=in.(ops_telegram_chat_id,ops_email,ops_digest_date)")}
    ops = setting.get("ops_telegram_chat_id")
    ops_email = setting.get("ops_email") if config.BREVO_API_KEY else None
    if (ops or ops_email) and setting.get("ops_digest_date") != today.isoformat():
        hearings = db.select("cases", f"select=id&next_hearing_date=eq.{today.isoformat()}&status=not.in.(disposed,not_ours)"
                                      f"&company_role=eq.respondent")
        overdue = db.select("deadlines", f"select=id&status=eq.open&due_date=lt.{today.isoformat()}")
        week = db.select("deadlines", f"select=id&status=eq.open&due_date=gte.{today.isoformat()}"
                                      f"&due_date=lte.{(today + timedelta(days=7)).isoformat()}")
        new = db.select("cases", f"select=id&is_baseline=eq.false&status=eq.new&detected_at=gte.{(today - timedelta(days=1)).isoformat()}")
        declined = db.select("hearing_reports", f"select=id&attendance=eq.no&hearing_date=gte.{today.isoformat()}")
        lines = (f"Hearings today (against us): {len(hearings)}\nNew cases since yesterday: {len(new)}\n"
                 f"Deadlines overdue: {len(overdue)} · due this week: {len(week)}\n"
                 f"Advocates unable to attend upcoming hearings: {len(declined)}")
        if ops:
            try:
                telegram.send(int(ops), f"<b>Haazir · {today.strftime('%d %b')}</b>\n{lines}"
                              + (f"\n<a href=\"{config.APP_URL}/deadlines\">Open deadlines</a>" if config.APP_URL else ""))
                stats["digest"] += 1
            except Exception as exc:
                print("digest on Telegram failed:", exc)
        if ops_email:
            try:
                mail.send(ops_email, f"Haazir daily summary, {today.strftime('%d %b')}", mail.card(
                    "Today's summary", mail.from_telegram(lines), ("Open deadlines", f"{config.APP_URL}/deadlines") if config.APP_URL else None))
                stats["digest"] += 1
            except Exception as exc:
                print("digest by email failed:", exc)
        if stats["digest"]:
            db.insert("settings", {"key": "ops_digest_date", "value": today.isoformat()}, upsert_on="key")
    print("reminders:", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
