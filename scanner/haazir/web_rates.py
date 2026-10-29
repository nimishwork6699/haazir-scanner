"""Item 30 of the library plan: the RBI policy repo rate and Bank Rate over time (decree interest, s.34 CPC, and the
like), read from RBI's own press releases. Each row quotes the sentence it was read from and links the press release.

    python -m haazir.web_rates list            # RBI press releases on policy rates, every year RBI lists (1990 on)
    python -m haazir.web_rates load            # their pages (B2 rates/rbi/press-releases/) + text, then the table
    python -m haazir.web_rates table           # rebuild rates/rbi.json.gz from the stored text only
    python -m haazir.web_rates show [--rate repo|bank]   # print the table's changes

Backblaze:
  rates/rbi.json.gz                       {"rows": [{date, rate, value, from_value, action, quote, title, source_url}, …],
                                           "current": what RBI's home page shows today, …}
  rates/rbi/press-releases/<file>.html    each press release as RBI publishes it (its HTML page: rbidocs PDFs are
                                          behind a bot challenge for scripts)
  rates/rbi/text.jsonl.gz                 their text, one row per press release
Figures are only ever read from the text with a pattern; a press release where no rate sentence is found adds nothing.
"""

import argparse
import gzip
import json
import re
import sys

from .storage import pack, unpack
from .web_circulars import RBI, _patch, _rbi_post, _rbi_rows, _rbi_years
from .web_common import Library, Site, html_text, index_file, now, run_docs

SOURCE = "Reserve Bank of India press releases (rbi.org.in)"
LIST_URL = RBI + "BS_PressReleaseDisplay.aspx"
# Press releases that announce policy rates. Notices of a coming policy ("… on June 7, 2016 at 2.30 pm"), minutes,
# speeches and research papers are left out.
WANT = re.compile(r"monetary policy statement|monetary and credit policy|policy statement|resolution of the monetary policy committee|"
                  r"mid[- ]quarter|quarter(?:ly)? review|bank rate|repo rate|policy rates?|liquidity adjustment facility|"
                  r"monetary policy review|annual policy|(?:monetary|liquidity|additional|policy) measures|"
                  r"rbi (?:cuts|raises|hikes|reduces|increases)", re.I)
SKIP = re.compile(r"minutes|governor.s (?:opening|statement at)|speech|working paper|technical advisory|press conference|"
                  r"\bon (?:\w+day, )?\w+ \d{1,2},? \d{4}|at \d{1,2}[.:]\d{2}\s*[ap]\.?m|to announce|will be announced|"
                  r"variable rate|auction|money market operations|webcast|schedule of|survey|draft|report on|bulletin|"
                  r"fixed rate (?:reverse )?repo operations|term repo|special (?:fixed )?repo|special fixed|reporting friday|"
                  r"trading hours|macroeconomic and monetary developments|results?\b", re.I)
STORE = "b2"
FOLDER = "rates/rbi/press-releases"
TEXT = "rates/rbi/text.jsonl.gz"
TABLE = "rates/rbi.json.gz"


def listing(site: Site) -> tuple[list[dict], dict]:
    first = site.get(LIST_URL).text
    seen: dict[str, dict] = {}
    per_year = {}
    for r in _rbi_rows(first, "BS_PressReleaseDisplay.aspx", "press-release"):
        seen.setdefault(r["page_url"], r)
    for y in _rbi_years(first):
        rows = _rbi_rows(_rbi_post(site, LIST_URL, first, y), "BS_PressReleaseDisplay.aspx", "press-release")
        keep = [r for r in rows if WANT.search(r["title"]) and not SKIP.search(r["title"])]
        per_year[y] = {"press_releases": len(rows), "on_policy_rates": len(keep)}
        for r in keep:
            seen.setdefault(r["page_url"], r)
        print(f"  {y}: {len(rows)} press releases, {len(keep)} on policy rates", flush=True)
    docs = [r for r in seen.values() if WANT.search(r["title"]) and not SKIP.search(r["title"])]
    for d in docs:
        d["url"] = d["page_url"]
        d["body"] = "Reserve Bank of India"
        d["number"] = f"PR {d['rbi_id']}" if d.get("rbi_id") else None
    return docs, per_year


# ---------------------------------------------------------------- reading rates out of the text

NUM = r"(\d{1,2}(?:\.\d{1,2})?)\s*(?:per\s*cent\b|percent\b|%)"  # not 'percentage point'
SENT = re.compile(r"(?<=[a-z0-9\)%])\.\s+(?=[A-Z(])|\n\s*\n|;\s+(?=consequently|and the|the )", re.I)
RATES = {
    # the policy repo rate (not the reverse repo, not a variable-rate repo auction)
    "repo": re.compile(r"(?<![Rr]everse )(?<![Rr]everse-)(?<!VRR )(?:policy\s+)?repo\s+rate", re.I),
    "bank": re.compile(r"\bBank\s+Rate\b"),
}
DECIDE = re.compile(r"decided|unchanged|increase|increased|raise|raised|reduce|reduced|cut|lower|hike|stands? adjusted|"
                    r"kept|keep|retain|retained|revised|with immediate effect|with effect from|effective|"
                    r"will accordingly (?:move|drop|go|come|rise|fall)|move (?:up|down) from", re.I)
NOT_DECISION = re.compile(r"\bprevious|last (?:policy|meeting|review)|since (?:january|february|march|april|may|june|july|august|"
                          r"september|october|november|december|\d{4})|cumulative|so far|in the past|over the year|"
                          r"had (?:been )?(?:raised|reduced|increased|cut|kept)|in the space of|the measures included|during (?:the )?(?:year|quarter)", re.I)


# A rate named only as the anchor of another ("MSF rate, 100 bps above the repo rate", "delinked from the Bank Rate"),
# or a market rate rather than the policy rate: the figure that follows is not this rate's.
NOT_THIS = re.compile(r"(?:above|below|over|than|linked to|delinked from|from|of|spread)\s+(?:the\s+)?(?:policy\s+)?$|"
                      r"(?:market|average|call|money market|weighted)\s+(?:policy\s+)?$", re.I)
ANY_RATE = re.compile(r"reverse\s+repo\s+rate|(?:policy\s+)?repo\s+rate|bank\s+rate|\bMSF\b(?:\s+rate)?|marginal standing facility(?:\s*\(MSF\))?\s+rate|"
                      r"\bSDF\b(?:\s+rate)?|standing deposit facility(?:\s*\(SDF\))?\s+rate|\bCRR\b|cash reserve ratio", re.I)


def _kind(name: str) -> str:
    n = name.lower()
    return ("reverse_repo" if "reverse" in n else "repo" if "repo" in n else "bank" if "bank" in n else
            "msf" if "msf" in n or "marginal" in n else "sdf" if "sdf" in n or "standing deposit" in n else "crr")


def _respectively(s: str, rate: str) -> tuple[str, None] | None:
    """'Reverse Repo Rate and Repo Rate kept unchanged at 5.5 per cent and 6.5 per cent, respectively'."""
    first = re.search(NUM, s, re.I)
    if not first:
        return None
    names = [_kind(m.group(0)) for m in ANY_RATE.finditer(s[: first.start()])]
    figures = [m.group(1) for m in re.finditer(NUM, s[first.start():], re.I)]
    if rate in names and names.count(rate) == 1 and len(figures) == len(names):
        return figures[names.index(rate)], None
    return None


def _value(sentence: str, m: re.Match) -> tuple[str, str | None] | None:
    """After the rate's name: 'unchanged at 6.50 per cent', 'by 25 basis points from 8.0 per cent to 7.75 per cent',
    'to 6.75 per cent', 'at 6.0 per cent'. Returns (value, from_value)."""
    if NOT_THIS.search(sentence[max(0, m.start() - 40): m.start()]):
        return None
    tail = sentence[m.end(): m.end() + 260]
    v = re.search(rf"(?<![A-Za-z])(?:unchanged\s+at|to|at)\s+{NUM}", tail, re.I)
    of = re.match(rf"\s*(?:\([^)]*\)\s*)?(?:under the [^,]{{0,60}}?\s)?of\s+{NUM}", tail, re.I)  # 'the policy repo rate of 5.50 per cent'
    if of and (not v or of.end() <= v.start()):
        return of.group(1), None
    if not v:
        return None
    # another rate (or a measure) named before the figure: the figure belongs to that one
    between = tail[: v.start()]
    if re.search(r"reverse repo|\bMSF\b|marginal standing|\bSDF\b|standing deposit|\bCRR\b|cash reserve|\bSLR\b|statutory liquidity|"
                 r"Bank Rate|repo rate|inflation|growth|GDP|CPI|remuneration|interest on", between, re.I):
        return None
    f = re.search(rf"from\s+{NUM}", between, re.I)
    return v.group(1), f.group(1) if f else None


def read_rates(row: dict) -> list[dict]:
    text = re.sub(r"[ \t\xa0]+", " ", row.get("text") or "")
    out = []
    for rate, pat in RATES.items():
        found = None
        for s in SENT.split(text):
            s = re.sub(r"\s+", " ", s).strip()  # 'reverse \n repo rate' is one phrase
            if len(s) > 900 or not DECIDE.search(s) or NOT_DECISION.search(s):
                continue
            if re.search(r"\brespectively\b", s, re.I):
                v = _respectively(s, rate)
                if v:
                    found = (s, v, None)
                    break
                continue
            for m in pat.finditer(s):
                v = _value(s, m)
                if v:
                    found = (s, v, m)
                    break
            if found:
                break
        if not found:
            continue
        s, (value, frm), m = found
        # the verb said of this rate: in the words just before its name, or between its name and the figure
        near = s[max(0, m.start() - 120): m.end() + 120] if m else s
        action = ("unchanged" if re.search(r"unchanged|kept|keep|retain|continuation|stable", near, re.I) else
                  "increased" if re.search(r"increase|raise|hike", near, re.I) else
                  "reduced" if re.search(r"reduce|\bcut|lower", near, re.I) else None)
        eff = re.search(r"(with immediate effect|with effect from [^,;]{4,60}|effective (?:from )?[^,;]{4,60})", s, re.I)
        out.append({"date": row.get("date"), "rate": rate, "value": float(value), "from_value": float(frm) if frm else None,
                    "action": action, "effective_text": eff.group(1).strip() if eff else None, "quote": s[:900],
                    "title": row.get("title"), "source_url": row.get("source_url"), "file": row.get("file")})
    return out


def current_rates(site: Site) -> dict:
    """What RBI's home page shows today, as printed there."""
    try:
        html = site.get("https://www.rbi.org.in/home.aspx").text
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    t = html_text(html)
    got = {}
    for label, key in (("Policy Repo Rate", "repo"), ("Bank Rate", "bank"), ("Standing Deposit Facility Rate", "sdf"),
                       ("Marginal Standing Facility Rate", "msf"), ("Fixed Reverse Repo Rate", "reverse_repo")):
        m = re.search(rf"{label}\s*:?\s*{NUM}", t, re.I)
        if m:
            got[key] = float(m.group(1))
    return {"read_at": now(), "source_url": "https://www.rbi.org.in/home.aspx", "rates": got}


def build_table(lib: Library, site: Site | None = None) -> dict:
    rows = unpack(lib.get(STORE, TEXT) or b"")
    table = []
    for r in rows:
        table += read_rates(r)
    table.sort(key=lambda x: (x["rate"], x["date"] or "", x["source_url"]))
    # One reading per rate per day (a day can have a statement and its resolution): keep the first; note disagreement.
    by_day: dict[tuple, dict] = {}
    for x in table:
        k = (x["rate"], x["date"])
        if k in by_day:
            if by_day[k]["value"] != x["value"]:
                by_day[k].setdefault("other_readings", []).append({"value": x["value"], "source_url": x["source_url"]})
            continue
        by_day[k] = x
    table = sorted(by_day.values(), key=lambda x: (x["rate"], x["date"] or ""))
    prev: dict[str, dict] = {}
    gaps = []
    for x in table:
        p = prev.get(x["rate"])
        x["change_from_previous_row"] = round(x["value"] - p["value"], 2) if p else None
        # A change this table did not read: the rate moved although this row says 'unchanged', or the row's own
        # 'from' figure is not the previous row's figure. The table is right on each row's date, not in between.
        if p and ((x["action"] == "unchanged" and x["change_from_previous_row"]) or
                  (x["from_value"] is not None and x["from_value"] != p["value"])):
            gaps.append({"rate": x["rate"], "between": [p["date"], x["date"]], "previous_value": p["value"],
                         "from_value": x["from_value"], "value": x["value"]})
        prev[x["rate"]] = x
    out = {"what": "RBI policy repo rate and Bank Rate as announced in RBI press releases (one row per announcement read)",
           "source": SOURCE, "built_at": now(), "press_releases_read": len(rows),
           "method": "Each figure is read by pattern from a sentence of the press release that states a decision; the sentence "
                     "is kept as 'quote'. A press release with no such sentence adds no row. Not every rate change is "
                     "necessarily announced in a press release this list picks up: check gaps against RBI's own data.",
           "current": current_rates(site) if site else None, "gaps": gaps, "rows": table}
    cur = (out["current"] or {}).get("rates", {})
    out["check_against_home_page"] = {r: {"home_page": cur.get(r), "last_row": prev[r]["value"] if r in prev else None,
                                          "match": cur.get(r) == (prev[r]["value"] if r in prev else None)} for r in RATES}
    data = gzip.compress(json.dumps(out, ensure_ascii=False, indent=0).encode(), 9)
    lib.put(STORE, TABLE, data, "rates", title="RBI policy repo rate and Bank Rate history", rows=len(table))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["list", "load", "table", "show"])
    ap.add_argument("--rate", choices=["repo", "bank"])
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    lib = Library(30, SOURCE)
    site = Site(pause=2.0)
    if a.cmd in ("list", "load"):
        docs, per_year = listing(site)
        print(f"  press releases on policy rates: {len(docs)}", flush=True)
        if a.cmd == "list":
            for d in docs:
                print(f"    {d['date']} {d['title'][:120]}")
            return 0
        _patch()  # RBI pages: the press release's own text (not the menus), and no challenge page taken for a PDF
        res = run_docs(lib, site, docs, store=STORE, folder=FOLDER, kind="rates_source", text_kind="rates_text", limit=a.limit,
                       group_of=lambda d: (STORE, TEXT, "RBI press releases on policy rates: text"), group_meta=lambda d: {})
        index_file(lib, STORE, "rates/rbi/index.jsonl.gz", docs, {**res, "per_year": per_year}, "RBI press releases on policy rates: index",
                   "rates_index")
        print(f"  new {res['new']}, already {res['already']}, failed {res['failed']}", flush=True)
        for f in res["fails"]:
            print(f"    failed {f['url']}: {f['why']}")
    if a.cmd in ("load", "table"):
        out = build_table(lib, site)
        print(f"  table: {len(out['rows'])} rows from {out['press_releases_read']} press releases; RBI home page today: {out['current']}")
    if a.cmd == "show":
        t = json.loads(gzip.decompress(lib.get(STORE, TABLE)))
        print(f"  built {t['built_at']}; current {t['current']}")
        for x in t["rows"]:
            if a.rate and x["rate"] != a.rate:
                continue
            if x["change_from_previous_row"] in (None,) or x["change_from_previous_row"] != 0:
                print(f"  {x['date']} {x['rate']:5} {x['value']:6.2f} ({x['change_from_previous_row']}) {x['action']}  {x['title'][:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
