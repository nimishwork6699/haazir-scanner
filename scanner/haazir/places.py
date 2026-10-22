"""State and city of every court, so the dashboard can search by place across all forums.

    python -m haazir.places        # fill state and city for courts that have none

eCourts names districts "Jaipur Metro I", "Jaipur District"; e-Jagriti names commissions "District Commission,
Jaipur-II"; High Court benches are "..., High Court Bench at Jaipur". All of these are the city Jaipur.
"""

import re
import sys

from .db import DB

# eCourts state codes and e-Jagriti state commission ids seen so far. Extend when a state is added.
STATES = {"9": "Rajasthan", "11080000": "Rajasthan", "rajasthan": "Rajasthan"}


def city_of(district_name: str, complex_name: str, zone_city: str | None = None) -> str | None:
    """The city a court belongs to, from its district or commission name."""
    m = re.search(r"bench at ([a-z ]+)", complex_name, re.I)
    if m:
        return m.group(1).strip().title()
    m = re.match(r"district commission,\s*([a-z ]+?)(?:[-\s]+[ivx]+)?$", complex_name.strip(), re.I)
    if m:
        return m.group(1).strip().title()
    if district_name.lower().startswith("consumer") or district_name.lower() == "high court":
        return zone_city  # a state commission or principal seat sits in the zone's city
    name = re.sub(r"\b(metro(politan)?|district|rural|urban|city|[ivx]+)\b", " ", district_name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip()
    return name.title() or zone_city


def state_of(state_code: str, district_name: str, complex_name: str) -> str | None:
    if state_code in STATES:
        return STATES[state_code]
    m = re.search(r"consumer,\s*(.+)$", district_name, re.I)
    if m:
        return m.group(1).strip().title()
    m = re.match(r"(.+?) high court", complex_name, re.I)
    return m.group(1).strip().title() if m else None


def fill(db: DB) -> int:
    zones = {z["id"]: z["city"] for z in db.select("zones")}
    n = 0
    for c in db.select("courts", "select=id,state_code,district_name,complex_name,zone_id,state,city"):
        state = c["state"] or state_of(c["state_code"], c["district_name"], c["complex_name"])
        city = c["city"] or city_of(c["district_name"], c["complex_name"], zones.get(c["zone_id"]))
        if (state, city) != (c["state"], c["city"]):
            db.update("courts", f"id=eq.{c['id']}", {"state": state, "city": city})
            print(f"  {c['complex_name']:55} {city} · {state}")
            n += 1
    return n


def add_to_terms(db: DB, court_ids: list[str], company_id: str | None = None) -> None:
    """Search these courts for every watched name (or one company's names) from the next scan on."""
    q = "select=id,court_ids&active=eq.true" + (f"&company_id=eq.{company_id}" if company_id else "")
    for t in db.select("watch_terms", q):
        if t["court_ids"] is None:
            continue  # already every court
        merged = sorted(set(t["court_ids"]) | set(court_ids))
        if merged != sorted(t["court_ids"]):
            db.update("watch_terms", f"id=eq.{t['id']}", {"court_ids": merged})


def main() -> int:
    n = fill(DB())
    print(f"{n} courts updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
