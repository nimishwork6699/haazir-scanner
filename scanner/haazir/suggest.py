"""Rank panel advocates for a case. Weights: specialty 40, free capacity 30, court familiarity 20, reachable 10."""

from .classify import CATEGORIES


def rank(case: dict, advocates: list[dict], load: dict[str, int], court_count: dict[tuple[str, str], int],
         zone_id: str | None, top: int = 3) -> list[dict]:
    out = []
    for a in advocates:
        if not a["active"] or (zone_id and a["zone_id"] != zone_id):
            continue
        score, reasons = 0, []
        if case["category"] in (a["specialties"] or []):
            score += 40
            reasons.append(f"{CATEGORIES.get(case['category'], case['category'])} specialist")
        active = load.get(a["id"], 0)
        cap = max(a["max_load"], 1)
        score += round(30 * max(0.0, 1 - active / cap))
        reasons.append(f"Load {active} of {cap}")
        seen = court_count.get((a["id"], case.get("court_id") or ""), 0)
        if seen:
            score += min(20, seen * 4)
            reasons.append(f"{seen} case{'s' if seen != 1 else ''} in this court")
        if a.get("telegram_chat_id"):
            score += 10
            reasons.append("Telegram connected")
        out.append({"advocate_id": a["id"], "name": a["name"], "score": score, "reasons": reasons})
    out.sort(key=lambda x: -x["score"])
    return out[:top]
