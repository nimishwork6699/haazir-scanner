"""Re-run side detection on every fetched case after the matcher changes. No eCourts requests.

    python -m haazir.rematch            # report only
    python -m haazir.rematch --apply    # write corrections
"""

import re
import sys
from collections import Counter

from .db import DB
from .matching import is_company, role


def decide(c: dict, confirm: list[str]) -> tuple[str, str, bool]:
    """Return (company_role, role_source, not_ours) using the same rules as the scanner."""
    pets = [p["name"] for p in c["petitioners"] or []]
    resps = [p["name"] for p in c["respondents"] or []]
    side = role(pets, resps, confirm)
    if side != "unknown":
        return side, "case_page", False
    rs = c.get("raw_search") or {}
    if is_company(rs.get("petitioner", ""), confirm) or is_company(rs.get("respondent", ""), confirm):
        main_mact = c["category"] == "motor_accident" and not re.search(r"misc|exe|ex\b|execution|appeal", (c["case_type"] or "").lower())
        return ("respondent", "inferred_mact", False) if main_mact else ("unknown", "unknown", False)
    return "unknown", "unknown", bool(pets or resps)


def main() -> int:
    apply = "--apply" in sys.argv
    db = DB()
    confirm: dict[str, list[str]] = {}
    for t in db.select("watch_terms", "select=company_id,confirm"):
        confirm.setdefault(t["company_id"], []).extend(t["confirm"])
    cases = db.select("cases", "select=id,cnr,company_id,case_type,category,status,company_role,role_source,"
                               "petitioners,respondents,raw_search&detail_fetched_at=not.is.null")
    changes = Counter()
    for c in cases:
        side, source, not_ours = decide(c, confirm[c["company_id"]])
        new_status = "not_ours" if not_ours else ("new" if c["status"] == "not_ours" else c["status"])
        if (side, source, new_status) == (c["company_role"], c["role_source"], c["status"]):
            continue
        changes[f"{c['company_role']}->{side} ({source}){' restored' if c['status']=='not_ours' and new_status!='not_ours' else ''}"] += 1
        if apply:
            db.update("cases", f"id=eq.{c['id']}", {"company_role": side, "role_source": source, "status": new_status})
            db.log(c["id"], "side_rechecked", {"from": c["company_role"], "to": side, "source": source})
    print(f"checked {len(cases)} cases; changes: {dict(changes) or 'none'}{'' if apply else ' (dry run)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
