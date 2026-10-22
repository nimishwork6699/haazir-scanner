import httpx

from . import config


def send(chat_id: int, html: str, buttons: list[list[tuple[str, str]]] | None = None) -> int:
    """Send an HTML message, optionally with rows of (label, callback data) buttons. Returns the message id."""
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    payload = {"chat_id": chat_id, "text": html, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
    r = httpx.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=30)
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram: {body.get('description', r.text[:200])}")
    return body["result"]["message_id"]
