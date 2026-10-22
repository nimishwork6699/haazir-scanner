"""Add or change a watched company without touching code.

    python -m haazir.company list
    python -m haazir.company add --name "HDFC Bank Ltd." --short "HDFC Bank" --sector bank \
        --search "HDFC Bank" --names "hdfc bank"
    python -m haazir.company add --name "State Bank of India" --short "SBI" --sector bank \
        --search "State Bank" "SBI" --names "state bank of india" "sbi"
    python -m haazir.company check --short "SBI" "S.B.I. through Branch Manager" "Jasbir Singh"
    python -m haazir.company off --short "SBI"

--search  names sent to eCourts. eCourts matches them anywhere inside a party name, so a short one
          like "Lombard" also finds "I.C.I.C.I Lombard". Single words of five letters or fewer ("SBI")
          keep only rows that name the company as a whole word.
--names   how the company is written in court records. A party counts as the company if one of these
          appears as whole words, with small typos, run together, or cut short ("United India").
New search names start with a baseline scan: what is already on file is not reported as new.
"""

import argparse
import sys

from .db import DB
from .matching import is_company

SECTORS = ("insurer", "bank", "nbfc", "telecom", "other")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("--name", required=True, help="legal name, e.g. 'HDFC Bank Ltd.'")
    a.add_argument("--short", required=True, help="name shown on the dashboard")
    a.add_argument("--sector", choices=SECTORS, default="other")
    a.add_argument("--search", nargs="+", required=True)
    a.add_argument("--names", nargs="+", required=True)
    c = sub.add_parser("check", help="test how party names would be matched")
    c.add_argument("--short", required=True)
    c.add_argument("party", nargs="+")
    o = sub.add_parser("off")
    o.add_argument("--short", required=True)
    args = ap.parse_args()
    db = DB()

    if args.cmd == "list":
        comps = db.select("companies", "select=id,name,short_name,sector&order=short_name")
        terms = db.select("watch_terms", "select=company_id,term,confirm,active")
        for co in comps:
            print(f"{co['short_name']} ({co['name']}, {co['sector']})")
            for t in [t for t in terms if t["company_id"] == co["id"]]:
                print(f"   {'on ' if t['active'] else 'off'}  search \"{t['term']}\"  names {t['confirm']}")
        return 0

    if args.cmd == "add":
        names = [n.lower().strip() for n in args.names]
        row = db.insert("companies", {"name": args.name, "short_name": args.short, "sector": args.sector},
                        upsert_on="name")[0]
        for term in args.search:
            db.insert("watch_terms", {"company_id": row["id"], "term": term, "confirm": names, "active": True},
                      upsert_on="company_id,term")
        print(f"{args.short}: searching {args.search}, matching {names}")
        return 0

    co = next(iter(db.select("companies", f"select=id,short_name&short_name=eq.{args.short}")), None)
    if not co:
        print(f"no company with short name {args.short!r}")
        return 1
    if args.cmd == "off":
        db.update("watch_terms", f"company_id=eq.{co['id']}", {"active": False})
        print(f"{args.short}: all search names switched off (cases already found are kept)")
        return 0
    names = [n for t in db.select("watch_terms", f"select=confirm&company_id=eq.{co['id']}") for n in t["confirm"]]
    for p in args.party:
        print(f"  {'YES' if is_company(p, names) else 'no '}  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
