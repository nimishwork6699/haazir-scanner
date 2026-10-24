"""Email through Brevo (free, 300 a day), beside Telegram. Same card as the website's mails (web/src/lib/email.ts)."""

import html
from functools import cache

import httpx

from . import config


def card(heading: str, body: str, button: tuple[str, str] | None = None) -> str:
    """A plain-text heading, an HTML body, and an optional (label, link) button."""
    btn = (f'<p style="margin:24px 0 4px"><a href="{button[1]}" style="display:inline-block;background:#3730a3;color:#fff;'
           f'text-decoration:none;font-weight:600;padding:11px 20px;border-radius:10px">{html.escape(button[0])}</a></p>') if button else ""
    return (f'<div style="background:#f5f5fa;padding:24px 12px;font-family:\'DM Sans\',Segoe UI,Arial,sans-serif;color:#1c1b2e">'
            f'<div style="max-width:520px;margin:0 auto;background:#fff;border:1px solid #e6e6ef;border-radius:14px;padding:28px">'
            f'<p style="margin:0 0 18px;font-weight:700;color:#3730a3;font-size:15px">Haazir</p>'
            f'<h1 style="margin:0 0 12px;font-size:20px;line-height:1.3">{html.escape(heading)}</h1>'
            f'<div style="font-size:14.5px;line-height:1.6">{body}</div>{btn}</div>'
            f'<p style="max-width:520px;margin:14px auto 0;font-size:12px;color:#77768a;text-align:center">'
            f"Sent by Haazir, your legal team's case tracker.</p></div>")


def from_telegram(text: str) -> str:
    """Telegram HTML (b, i, a, code and new lines) as an email body."""
    return text.strip().replace("\n", "<br>")


@cache
def sender() -> str:
    """EMAIL_FROM, or the settings row email_from (the address stays out of this public code)."""
    if config.EMAIL_FROM:
        return config.EMAIL_FROM
    from .db import DB
    found = next((r["value"] for r in DB().select("settings", "select=value&key=eq.email_from")), None)
    if not found:
        raise RuntimeError("No sender: set EMAIL_FROM or the settings row email_from")
    return found


def send(to: str, subject: str, body_html: str, name: str | None = None) -> None:
    """Send one mail. Raises on failure."""
    if not config.BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY is not set")
    r = httpx.post("https://api.brevo.com/v3/smtp/email", timeout=30,
                   headers={"api-key": config.BREVO_API_KEY, "accept": "application/json"},
                   json={"sender": {"name": "Haazir", "email": sender()},
                         "to": [{"email": to, **({"name": name} if name else {})}], "subject": subject, "htmlContent": body_html})
    if r.status_code >= 300:
        raise RuntimeError(f"Email: {r.text[:200] or r.status_code}")
