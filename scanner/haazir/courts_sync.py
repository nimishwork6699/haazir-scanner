"""Add the court complexes of a state's districts to the watch list, by name. No CAPTCHA needed.

    python -m haazir.courts_sync --state Rajasthan --districts "Jaipur Metro I" "Jaipur Metro II" "Jaipur District" --zone "Jaipur Zone"
    python -m haazir.courts_sync --state Delhi --districts "South West" Central --zone "Delhi Zone"
    python -m haazir.courts_sync --state Delhi --list           # show the state's districts as eCourts names them
    python -m haazir.courts_sync --state Delhi --districts Central --off   # stop scanning (cases are kept)

New courts start with a baseline scan: their cases already on file are not reported as new.
"""

import argparse
import asyncio
import re
import sys

from . import places
from .db import DB
from .ecourts import ECourts

# Forums where our companies are not litigants, and placeholder entries eCourts lists in some districts.
SKIP = re.compile(r"\b(family|jjb|juvenile|waqf|nagar nigam|gram ny[a]?[a]?yalaya|test|railway)\b", re.I)


def pick(options: dict[str, str], wanted: str, what: str) -> str:
    """eCourts code for a name: exact match first, then a unique partial match."""
    exact = [k for k, v in options.items() if v.strip().lower() == wanted.lower()]
    part = [k for k, v in options.items() if wanted.lower() in v.lower()]
    found = exact or (part if len(part) == 1 else [])
    if not found:
        raise SystemExit(f"{what} {wanted!r} not found or ambiguous. Options: {sorted(v.strip() for v in options.values())}")
    return found[0]


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True)
    ap.add_argument("--districts", nargs="*", default=[])
    ap.add_argument("--zone", help="zone the courts belong to (created if new)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--off", action="store_true")
    args = ap.parse_args()
    db = DB()

    async with ECourts() as ec:
        states = await ec.client.list_states()
        state = pick(states, args.state, "state")
        districts = await ec.client.list_districts(state)
        if args.list or not args.districts:
            print("\n".join(sorted(v.strip() for v in districts.values())))
            return 0
        if args.off:
            for d in args.districts:
                n = db.update("courts", f"state_code=eq.{state}&dist_code=eq.{pick(districts, d, 'district')}", {"active": False})
                print(f"  {d}: {len(n)} courts switched off")
            return 0
        if not args.zone:
            raise SystemExit("--zone is required when adding courts")
        city = args.zone.replace(" Zone", "")
        zone = db.insert("zones", {"name": args.zone, "city": city}, upsert_on="name")[0]
        rows = []
        for d in args.districts:
            dc = pick(districts, d, "district")
            complexes = await ec.list_complexes(state, dc)
            for value, name in complexes.items():
                if SKIP.search(name):
                    print(f"  skipped  {name.strip()}")
                    continue
                code, ests, flag = (value.split("@") + ["", ""])[:3]
                rows.append({"state_code": state, "dist_code": dc, "complex_code": code, "est_codes": ests,
                             "needs_est": flag == "Y", "district_name": districts[dc].strip(),
                             "complex_name": name.strip(), "zone_id": zone["id"], "active": True})
    db.insert("courts", rows, upsert_on="state_code,dist_code,complex_code")
    for r in rows:
        print(f"  added    {r['district_name']:18} {r['complex_name']}")
    print(f"{len(rows)} courts in {args.zone}")
    places.fill(db)
    ids = [c["id"] for c in db.select("courts", f"select=id,state_code,dist_code,complex_code&state_code=eq.{state}")
           if any(c["dist_code"] == r["dist_code"] and c["complex_code"] == r["complex_code"] for r in rows)]
    places.add_to_terms(db, ids)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
