"""Item 29 (part): the motor-accident compensation rule table, built only from what Haazir's library already holds:
the Motor Vehicles Act, 1988 as on India Code (B2 acts/central/…, schedules/…) and the Supreme Court summaries
(B2 judgments/sc/all.jsonl.gz: titles, neutral citations, SCR citations, headnotes). Nothing is typed in from memory:
statute wording and amounts are quoted as the Act prints them, and each judgment's citation is the one its own
record carries.

    python -m haazir.web_quantum build      # -> Backblaze compensation/quantum.json.gz
    python -m haazir.web_quantum show
"""

import gzip
import json
import re
import sys

from .storage import unpack
from .web_common import Library, now

TABLE = "compensation/quantum.json.gz"
SOURCE = "Haazir library: India Code (MV Act, 1988) and Supreme Court summaries (judgments/sc/all.jsonl.gz)"
SECTIONS = ("161", "163A", "164", "164A", "164B", "166", "168")
# The judgments asked for, found by their parties' names in the SC titles (the names are the search, not the citation).
JUDGMENTS = {"Sarla Verma": r"sarla verma", "Reshma Kumari": r"reshma kumari", "Pranay Sethi": r"pranay sethi",
             "Magma v. Nanu Ram": r"magma.*nanu ram", "Raj Kumar v. Ajay Kumar": r"raj kumar\b.*ajay kumar"}
POINTS = re.compile(r"multiplier|future prospects?|deduct|personal (?:and living )?expenses|conventional|consortium|funeral|"
                    r"loss of estate|loss of dependency|notional income|disability|loss of earning|interest", re.I)
AMOUNT = re.compile(r"\b(?:a\s+)?(?:fixed\s+)?sum\s+of\s+(?:[a-z]+\s+){1,6}?rupees|"
                    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|fifty|twenty)(?:\s+and\s+a\s+half)?\s+(?:lakh|thousand)\s+rupees", re.I)


def _mv_act(lib: Library) -> tuple[dict, dict | None, str, str]:
    rows = lib.db.select("library_files", "select=path,title&act=eq.MV%20Act&order=path")
    act_path = next(r["path"] for r in rows if r["path"].startswith("acts/central/"))
    sch_path = next((r["path"] for r in rows if r["path"].startswith("schedules/")), None)
    act = unpack(lib.get("b2", act_path))[0]
    sch = unpack(lib.get("b2", sch_path))[0] if sch_path else None
    return act, sch, act_path, sch_path


def build() -> dict:
    lib = Library(29, SOURCE)
    act, sch, act_path, sch_path = _mv_act(lib)
    statute = []
    for s in act["sections"]:
        if str(s.get("number")) in SECTIONS:
            body = s.get("body") or ""
            statute.append({"act": act["name"], "section": s["number"], "title": s.get("title"), "text": body,
                            "notes": s.get("notes"), "amounts_as_printed": [m.group(0).strip() for m in AMOUNT.finditer(body)],
                            "omitted": bool(re.match(r"\s*omitted", s.get("title") or "", re.I)),
                            "library_file": act_path, "source": act.get("source"), "read_at": act.get("read_at")})
    schedule = None
    if sch:
        for p in sch.get("pieces", []):
            if re.search(r"second schedule", p.get("title") or "", re.I):
                schedule = {"title": p["title"], "text": p.get("text"), "library_file": sch_path, "source": sch.get("source")}
    sc = unpack(lib.get("b2", "judgments/sc/all.jsonl.gz"))
    judgments = []
    for name, pat in JUDGMENTS.items():
        hits = [r for r in sc if re.search(pat, r.get("title") or "", re.I)]
        for r in sorted(hits, key=lambda r: r.get("decided") or ""):
            head = r.get("headnote") or ""
            points = [p.strip() for p in re.split(r"(?<=[.;])\s+|\s+-\s+", head) if POINTS.search(p)]
            judgments.append({"asked_as": name, "title": r.get("title"), "neutral_citation": r.get("nc"), "scr_citation": r.get("citation"),
                              "decided": r.get("decided"), "case_no": r.get("case_no"), "bench": r.get("bench"), "judges": r.get("judges"),
                              "author": r.get("author"), "headnote": head, "headnote_points_on_quantum": points[:25],
                              "library_file": "judgments/sc/all.jsonl.gz", "sc_path": r.get("path")})
        if not hits:
            judgments.append({"asked_as": name, "found": False})
    out = {"what": "Motor accident compensation: the MV Act's fixed sums (as printed) and the Supreme Court judgments on quantum, "
                   "with the citations their own records carry",
           "built_at": now(), "source": SOURCE,
           "note": "Citations are the neutral citation (INSC) and the Supreme Court Reports citation from the SC record; other "
                   "report citations (e.g. SCC) are not in Haazir's records and are left out. Where a name matches more than one "
                   "judgment (e.g. a referral order and the later decision), all are listed with their dates and benches.",
           "statute": statute, "mv_act_second_schedule": schedule, "judgments": judgments}
    data = gzip.compress(json.dumps(out, ensure_ascii=False, indent=0).encode(), 9)
    lib.put("b2", TABLE, data, "quantum_table", title="Motor accident compensation: MV Act sums and SC quantum judgments",
            rows=len(statute) + len(judgments), act="MV Act")
    return out


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "build":
        out = build()
    elif cmd == "show":
        out = json.loads(gzip.decompress(Library(29, SOURCE).get("b2", TABLE)))
    else:
        print(__doc__)
        return 0
    for s in out["statute"]:
        print(f"  MV Act s.{s['section']} {s['title'][:70]}: {s['amounts_as_printed']}")
    if out.get("mv_act_second_schedule"):
        print(f"  Second Schedule: {(out['mv_act_second_schedule']['text'] or '')[:160]!r}")
    for j in out["judgments"]:
        if j.get("found") is False:
            print(f"  {j['asked_as']}: not found in the SC library")
            continue
        print(f"  {j['asked_as']}: {j['title']} | {j['neutral_citation']} | {j['scr_citation']} | {j['decided']} | bench {j['bench']} | "
              f"{len(j['headnote_points_on_quantum'])} headnote points")
    return 0


if __name__ == "__main__":
    sys.exit(main())
