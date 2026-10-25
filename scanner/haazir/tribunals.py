"""Three more tribunals, read from their own public case status (free, no captcha, no login):

  NCLAT  nclat.nic.in            appeals against NCLT orders (New Delhi and Chennai benches)
  CAT    cgat.gov.in             service cases of government employees (19 benches)
  APTEL  aptel.gov.in            appeals in electricity (and petroleum) regulation (New Delhi)

    python -m haazir.tribunals search nclat "ICICI Bank"     # try a name, save nothing
    python -m haazir.tribunals scan [--only nclat,cat,aptel] [--company ICICI] [--dry]

The same steps as the NCLT scanner: each watched company's names are searched, every case is checked against the
company's own confirm names (these sites match words loosely), new pending cases join Our cases, and saved ones get
their next date again. NCLAT's next date and bench come from each case's details page.
"""

import argparse
import html as htmllib
import re
import sys
import time
import warnings
from datetime import date, datetime, timezone

import httpx

from .db import DB
from .matching import is_company, title_of

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
PAUSE = 2.0  # seconds between requests to one site
DETAILS_PER_RUN = 600  # NCLAT case pages read per run (next date, bench): about 20 minutes


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso(d: str | None) -> str | None:
    """'24/02/2016', '17-01-2025' or '2025-01-17' -> '2025-01-17'; N/A and the rest -> None."""
    d = (d or "").strip()[:10]
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(d, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def text(s: str | None) -> str:
    """Cell text: tags gone, entities read (&#40; is '('), spaces tidied."""
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def split_title(t: str) -> tuple[str, str]:
    """'A VS B' -> ('A', 'B')."""
    parts = re.split(r"\s+v(?:s|ersus)?\.?\s+", t, maxsplit=1, flags=re.I)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (t.strip(), "")


class Site:
    """One tribunal's website: one session, a pause between requests."""
    forum = ""
    label = ""
    has_details = False  # a details page per case with the next date

    def __init__(self) -> None:
        self.last = 0.0
        self.missed: list[str] = []
        self.http = self._client()

    def _client(self) -> httpx.Client:
        return httpx.Client(headers=UA, timeout=90, follow_redirects=True)

    def wait(self) -> None:
        gap = time.time() - self.last
        if gap < PAUSE:
            time.sleep(PAUSE - gap)
        self.last = time.time()

    def search(self, name: str) -> list[dict]:
        raise NotImplementedError

    def tries(self, what: str, call):
        """These sites drop connections now and then: three tries, a longer pause each time. Gives up quietly on one
        bench or year (noted in self.missed) so the rest of the search still counts."""
        for attempt in range(3):
            try:
                return call()
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == 2:
                    self.missed.append(f"{what}: {type(exc).__name__}")
                    return None
                time.sleep(5 * (attempt + 1))

    def details(self, c: dict) -> dict:
        """More about one case (next date, bench). Nothing more by default."""
        return {}


# ---------------------------------------------------------------- NCLAT
class NCLAT(Site):
    forum, label, has_details = "nclat", "NCLAT", True
    BASE = "https://nclat.nic.in/display-board"
    BENCHES = {"delhi": "New Delhi", "chennai": "Chennai"}
    FIELDS = ["search_by", "location", "case_type", "exact_search_word", "text_name", "from_date", "to_date", "case_status",
              "select_party", "party_name", "diary_no", "case_number", "advocate_name", "case_year"]

    def __init__(self) -> None:
        super().__init__()
        self.token = ""

    def _token(self) -> str:
        if not self.token:
            self.wait()
            page = self.http.get(f"{self.BASE}/cases").text
            self.token = re.search(r'name="_token" value="([^"]+)"', page).group(1)
        return self.token

    def _post(self, path: str, data: dict) -> dict:
        self.wait()
        r = self.http.post(f"{self.BASE}/{path}", data={"_token": self._token(), **data},
                           headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{self.BASE}/cases"})
        r.raise_for_status()
        return r.json()

    def search(self, name: str) -> list[dict]:
        """Both benches; the site's two party lists (main and additional parties) together."""
        out: dict[str, dict] = {}
        for loc, bench in self.BENCHES.items():
            for which in ("1", "2"):
                form = {k: "" for k in self.FIELDS} | {"search_by": "party_wise", "location": loc, "case_status": "all", "select_party": which,
                                                       "exact_search_word": "1", "party_name": name}
                got = self.tries(f"{bench} list {which}", lambda: self._post("cases_details", form))
                for row in (got or {}).get("data") or []:
                    filing = text(str(row[1]))
                    pet, res = split_title(text(row[3]))
                    out.setdefault(filing, {
                        "filing_no": filing, "case_number": text(row[2]).replace(" - ", " "), "case_type": text(row[2]).split(" - ")[0] or None,
                        "bench": bench, "loc": loc, "petitioner": pet, "respondent": res, "filed": iso(text(row[4])), "registered": None,
                        "next_date": None, "disposed": None, "pending": text(row[5]).lower().startswith("pend"), "judges": None,
                    })
        return list(out.values())

    def details(self, c: dict) -> dict:
        d = self._post("view_details", {"search_type": "view_details", "filing_no": c["filing_no"], "bench_name": c.get("loc", "delhi")}).get("data") or {}
        nxt = d.get("next_hearing_details") or {}
        last = d.get("last_hearing_details") or {}
        case = (d.get("case_details") or [{}])[0]
        reps = d.get("legal_representative") or {}
        nd = iso(nxt.get("hearing_date")) if isinstance(nxt, dict) else None
        # The site repeats the last hearing as "next" until a new date is fixed: only a date still ahead is a next date.
        return {
            "next_date": nd if nd and nd >= date.today().isoformat() else None,
            "last_listed": iso(last.get("hearing_date")) if isinstance(last, dict) else None,
            "registered": iso(case.get("registration_date")), "filed": iso(case.get("date_of_filing")) or c.get("filed"),
            "pending": (case.get("status") or "P") == "P",
            "judges": re.sub(r"\s*,\s*$", "", text((nxt if isinstance(nxt, dict) and nxt else last if isinstance(last, dict) else {}).get("coram"))) or None,
            "court_no": (nxt if isinstance(nxt, dict) and nxt else {}).get("court_no"),
            "stage": (nxt if isinstance(nxt, dict) and nxt else {}).get("stage_of_case"),
            "pet_adv": ", ".join(reps.get("applicant_legal_representative_name") or []),
            "res_adv": ", ".join(reps.get("respondent_legal_representative_name") or []),
        }


# ---------------------------------------------------------------- CAT
class CAT(Site):
    forum, label = "cat", "CAT"
    API = "https://cgat.gov.in/catapi"
    # The site's own list of benches (bench id -> name, state).
    BENCHES = {21: ("Hyderabad", "Telangana"), 40: ("Guwahati", "Assam"), 60: ("Chandigarh", "Chandigarh"), 100: ("Principal Bench, New Delhi", "Delhi"),
               103: ("Bangalore", "Karnataka"), 111: ("Jodhpur", "Rajasthan"), 116: ("Patna", "Bihar"), 117: ("Jammu", "Jammu and Kashmir"),
               119: ("Srinagar", "Jammu and Kashmir"), 120: ("Ahmedabad", "Gujarat"), 180: ("Ernakulam", "Kerala"), 200: ("Jabalpur", "Madhya Pradesh"),
               210: ("Mumbai", "Maharashtra"), 260: ("Cuttack", "Odisha"), 291: ("Jaipur", "Rajasthan"), 310: ("Chennai", "Tamil Nadu"),
               330: ("Allahabad", "Uttar Pradesh"), 332: ("Lucknow", "Uttar Pradesh"), 350: ("Kolkata", "West Bengal")}

    def _client(self) -> httpx.Client:
        # cgat.gov.in sends an incomplete certificate chain; the data is public case status, read only.
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
        return httpx.Client(headers={**UA, "Origin": "https://cgat.gov.in", "Referer": "https://cgat.gov.in/"}, timeout=90, follow_redirects=True, verify=False)

    def search(self, name: str) -> list[dict]:
        out: dict[str, dict] = {}
        for bench, (bname, _state) in self.BENCHES.items():
            def one(bench=bench):
                self.wait()
                r = self.http.post(f"{self.API}/casedetail_party_name_wise",
                                   files={"catschemaId": (None, str(bench)), "partyType": (None, "3"), "partyName": (None, name.lower())})
                r.raise_for_status()
                return r.json() if r.content and r.text.strip().startswith("[") else []
            rows = self.tries(bname, one) or []
            for x in rows:
                out.setdefault(x["filing_no"], {
                    # A case not numbered yet is known by its diary number.
                    "filing_no": x["filing_no"], "case_number": x.get("caseno") or (f"Diary {x['diaryno']}" if x.get("diaryno") else None),
                    "case_type": x.get("casetype"), "bench": bname, "bench_id": bench,
                    "petitioner": text(x.get("applicant")), "respondent": text(x.get("respondent")), "filed": iso(x.get("dateoffiling")),
                    "registered": None, "next_date": iso(x.get("nextlistingdate")), "disposed": iso(x.get("dateofdisposal")),
                    # CAT's "stage" only says PENDING or DISPOSAL: not a hearing stage.
                    "pending": not x.get("dateofdisposal"), "judges": None,
                    "stage": x.get("stage") if (x.get("stage") or "").upper() not in ("", "PENDING", "DISPOSAL", "DISPOSED") else None,
                })
        return list(out.values())


# ---------------------------------------------------------------- APTEL
class APTEL(Site):
    forum, label, has_details = "aptel", "APTEL", True
    # aptel.gov.in's certificate chain is incomplete over https; the same public page is served over http.
    BASE = "http://www.aptel.gov.in"
    YEARS = 5  # appeals here run for years: filing years searched, this year back

    def search(self, name: str) -> list[dict]:
        out: dict[str, dict] = {}
        self.wait()
        page = self.http.get(f"{self.BASE}/en/casestatusapi/tab3").text
        build = re.search(r'id="case-order-partywise-form".*?name="form_build_id" value="([^"]+)"', page, re.S).group(1)
        for year in range(date.today().year, date.today().year - self.YEARS, -1):
            def one(year=year):
                self.wait()
                r = self.http.post(f"{self.BASE}/en/casestatusapi/tab3?ajax_form=1&_wrapper_format=drupal_ajax",
                                   data={"form_build_id": build, "form_id": "case_order_partywise_form", "party_type": "3", "party_name": name, "year_type": "1",
                                         "diary_year": str(year), "_triggering_element_name": "op", "_triggering_element_value": "Submit", "_drupal_ajax": "1"},
                                   headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
                r.raise_for_status()
                return " ".join(c.get("data", "") for c in r.json() if isinstance(c.get("data"), str))
            html = self.tries(str(year), one) or ""
            for tr in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", html):
                cells = [text(td) for td in re.findall(r"(?s)<td[^>]*>(.*?)</td>", tr)]
                link = re.search(r"caseapidetails/(\d+)", tr)
                if len(cells) < 7 or not re.match(r"\d+/\d{4}", cells[1]):
                    continue
                pet, res = split_title(cells[3])
                nxt = re.search(r"\d{2}-\d{2}-\d{4}", cells[5])
                out.setdefault(cells[1], {
                    "filing_no": cells[1], "case_number": cells[2] or None, "case_type": (cells[2].split("-")[0] or None) if cells[2] else None,
                    "bench": "New Delhi", "petitioner": pet, "respondent": res, "filed": iso(cells[4]), "registered": None,
                    "next_date": iso(nxt.group(0)) if nxt else None, "disposed": None, "pending": cells[6].lower().startswith("pend"),
                    "judges": None, "court": re.sub(r"\d{2}-\d{2}-\d{4}\s*", "", cells[5]).strip(" /") or None,
                    "detail_id": link.group(1) if link else None,
                })
        return list(out.values())

    def details(self, c: dict) -> dict:
        """The case page: next listing date, court and purpose, and both sides' lawyers. The party list says N/A."""
        if not c.get("detail_id"):
            return {}
        self.wait()
        page = text(re.sub(r"(?s)<(script|style).*?</\1>", " ", self.http.get(f"{self.BASE}/caseapidetails/{c['detail_id']}").text))
        # The case's own section only: the site's menu also has a "Case Status".
        page = page[page.find("DFR No/Year"):] if "DFR No/Year" in page else ""
        field = lambda name, nxt: (re.search(rf"{name}\s+(.*?)\s+{nxt}", page) or [None, ""])[1].strip()
        nd = iso(field("Next Listing Date", "Next Listing Purpose"))
        court = field("Next Court", "Next Listing Date")
        return {
            "next_date": nd if nd and nd >= date.today().isoformat() else None,
            "last_listed": nd if nd and nd < date.today().isoformat() else None,
            "stage": field("Next Listing Purpose", "PENDING IA DETAILS") or None,
            "judges": f"Court {court}" if court and court != "N/A" else None,
            "pending": field("Case Status", "CASE CURRENT PURPOSE").lower().startswith("pend") if "Case Status" in page else c.get("pending"),
            "pet_adv": re.sub(r"\s*\([A-Z]-?\d+\)", "", field("Pet. Advocate Name", "Additional Advocate")).replace("N/A", "").strip() or None,
            "res_adv": re.sub(r"\s*\([A-Z]-?\d+\)", "", field("Respondent Advocate", "Additional Advocate")).replace("N/A", "").strip() or None,
        }


SITES = {"nclat": NCLAT, "cat": CAT, "aptel": APTEL}
CATEGORY = {"nclat": lambda c: "insolvency" if "Ins" in (c.get("case_type") or "") else "company_law", "cat": lambda c: "service", "aptel": lambda c: "electricity"}


def live(c: dict) -> bool:
    """A case worth adding: pending, and with a sign of life. These sites still mark many cases from 2017 'pending' with no
    hearing ever recorded; a case counts only with a next date ahead, a hearing in the last 2 years or a filing in the last 3."""
    if c.get("pending") is False:
        return False
    today = date.today()
    return bool((c.get("next_date") and c["next_date"] >= today.isoformat())
                or (c.get("last_listed") and c["last_listed"] >= today.replace(year=today.year - 2).isoformat())
                or (c.get("filed") and c["filed"] >= today.replace(year=today.year - 3).isoformat()))


def is_application(c: dict) -> bool:
    """An application inside a main case (IA, MA): not a case of its own."""
    return bool(re.match(r"\s*(IA\b|I\.A\.|MA\b|M\.A\.|Interlocutory\b)", c.get("case_number") or c.get("case_type") or "", re.I))


def side_for(c: dict, names: list[str]) -> str:
    on_p, on_r = is_company(c["petitioner"], names), is_company(c["respondent"], names)
    return "respondent" if on_r and not on_p else "petitioner" if on_p and not on_r else "unknown"


def bench_court(db: DB, forum: str, c: dict, cache: dict) -> str | None:
    """The courts row for a tribunal bench (made the first time a case there is saved). CAT benches carry their state,
    so a bank's or department's zone office gets them; NCLAT and APTEL are national and stay with head office."""
    key = f"{forum}|{c['bench']}"
    if key not in cache:
        name = f"{SITES[forum].label} {c['bench']}"
        rows = db.select("courts", f"select=id&forum=eq.{forum}&complex_name=eq.{name}")
        if not rows:
            state = CAT.BENCHES.get(c.get("bench_id"), (None, None))[1] if forum == "cat" else None
            city = c["bench"].split(",")[-1].strip()
            rows = db.insert("courts", {"forum": forum, "state_code": forum, "dist_code": forum, "complex_code": str(c.get("bench_id") or c["bench"]),
                                        "complex_name": name, "district_name": c["bench"], "city": city, "state": state, "active": True}, returning="id")
        cache[key] = rows[0]["id"] if rows else None
    return cache[key]


def case_row(forum: str, c: dict, company_id: str, side: str, court_id: str | None) -> dict:
    # No CNR here: the tribunal's own filing (diary) number is the case's lasting id.
    return {
        "cnr": f"{forum.upper()}{re.sub(r'[^0-9A-Za-z]', '', c['filing_no'])}", "forum": forum, "company_id": company_id, "court_id": court_id,
        "case_type": c.get("case_type"), "case_number": c.get("case_number"), "filing_date": c.get("filed"), "registration_date": c.get("registered"),
        "next_hearing_date": c.get("next_date"), "decision_date": c.get("disposed"), "court_judge": c.get("judges"), "stage": c.get("stage"),
        "petitioners": [{"name": c["petitioner"], "advocate": c.get("pet_adv") or ""}] if c["petitioner"] else [],
        "respondents": [{"name": c["respondent"], "advocate": c.get("res_adv") or ""}] if c["respondent"] else [],
        "petitioner_advocate": c.get("pet_adv") or None,
        "title": title_of(c["petitioner"], c["respondent"]), "company_role": side, "role_source": "search_list" if side != "unknown" else "unknown",
        "category": CATEGORY[forum](c), "is_baseline": True, "status": "disposed" if c.get("pending") is False else "new",
        "raw_search": {"source": forum, "filing_no": c["filing_no"], "bench": c["bench"]}, "detail_fetched_at": now(),
    }


def scan(db: DB, only: list[str], company: str = "", dry: bool = False) -> dict:
    stats = {f: {"searches": 0, "found": 0, "new": 0, "updated": 0, "details": 0, "failed": 0} for f in only}
    comps = db.select("companies", "select=id,name,short_name")
    if company:
        comps = [c for c in comps if company.lower() in (c["short_name"].lower(), c["name"].lower())]
    terms: dict[str, list[str]] = {}
    for t in db.select("watch_terms", "select=company_id,term,confirm&active=eq.true"):
        terms.setdefault(t["company_id"], []).append(t["term"])
        terms.setdefault(f"confirm:{t['company_id']}", []).extend(t["confirm"])
    for forum in only:
        site, st, courts = SITES[forum](), stats[forum], {}
        have = {r["cnr"]: r for r in db.select("cases", f"select=id,cnr,next_hearing_date,status,court_judge&forum=eq.{forum}")}
        details_left = DETAILS_PER_RUN
        for co in comps:
            names = terms.get(f"confirm:{co['id']}") or [co["name"]]
            found: dict[str, dict] = {}
            for term in terms.get(co["id"], []) or [co["name"]]:
                try:
                    rows = site.search(term)
                except Exception as exc:
                    st["failed"] += 1
                    print(f"  {site.label} · {term}: failed ({type(exc).__name__}: {str(exc)[:120]})", flush=True)
                    continue
                st["searches"] += 1
                for c in rows:
                    if c["filing_no"] and not is_application(c) and (is_company(c["petitioner"], names) or is_company(c["respondent"], names)):
                        found.setdefault(c["filing_no"], c)
            st["found"] += len(found)
            new = []
            for c in found.values():
                cnr = f"{forum.upper()}{re.sub(r'[^0-9A-Za-z]', '', c['filing_no'])}"
                old = have.get(cnr)
                # Pending cases get their details page (NCLAT): new ones first, then saved ones whose date has passed.
                # A new NCLAT case waits for its details (its next date and last hearing decide whether it is live).
                needs = site.has_details and c.get("pending") and (forum == "nclat" or live(c))                     and (not old or not old["next_hearing_date"] or old["next_hearing_date"] < date.today().isoformat())
                if needs and details_left > 0:
                    try:
                        c.update({k: v for k, v in site.details(c).items() if v is not None or k == "next_date"})
                        c["read"] = True
                        details_left -= 1
                        st["details"] += 1
                    except Exception as exc:
                        print(f"  {site.label} details {c['filing_no']}: failed ({type(exc).__name__})", flush=True)
                if not old and forum == "nclat" and not c.get("read"):
                    if c.get("pending"):  # decided cases are never added: nothing to wait for
                        st["waiting"] = st.get("waiting", 0) + 1
                    continue
                row = case_row(forum, c, co["id"], side_for(c, names), None if dry else bench_court(db, forum, c, courts))
                if old and not dry:
                    # A details page not read this time says nothing about the date: the saved one stays.
                    if site.has_details and not c.get("read"):
                        row["next_hearing_date"], row["court_judge"] = old["next_hearing_date"], old["court_judge"]
                    if old["next_hearing_date"] != row["next_hearing_date"] or (row["status"] == "disposed") != (old["status"] == "disposed"):
                        db.update("cases", f"id=eq.{old['id']}", {"next_hearing_date": row["next_hearing_date"], "decision_date": row["decision_date"],
                                                                "court_judge": row["court_judge"], "detail_fetched_at": now(),
                                                                **({"status": "disposed"} if row["status"] == "disposed" else {})})
                        st["updated"] += 1
                elif not old and live(c):
                    new.append(row)
            if dry:
                for r in new[:5]:
                    print(f"   would add {r['case_number']} · {r['title']} · {r['company_role']} · next {r['next_hearing_date']}", flush=True)
                st["new"] += len(new)
                continue
            for i in range(0, len(new), 200):
                st["new"] += len(db.insert("cases", new[i:i + 200], upsert_on="cnr", ignore_duplicates=True, returning="id"))
            print(f"{site.label} · {co['short_name']}: {len(found)} cases, {len(new)} new to Haazir", flush=True)
        if site.missed:
            st["missed"] = len(site.missed)
            print(f"  {site.label}: {len(site.missed)} benches or years did not answer after 3 tries: {', '.join(site.missed[:8])}", flush=True)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("site", choices=list(SITES))
    s.add_argument("name")
    s.add_argument("--details", type=int, default=0, help="read this many case pages too")
    sc = sub.add_parser("scan")
    sc.add_argument("--only", default=",".join(SITES))
    sc.add_argument("--company", default="")
    sc.add_argument("--dry", action="store_true", help="search and show, save nothing")
    args = ap.parse_args()
    if args.cmd == "search":
        site = SITES[args.site]()
        rows = site.search(args.name)
        print(f"{len(rows)} cases")
        for c in rows[:8]:
            if args.details:
                c.update({k: v for k, v in site.details(c).items() if v is not None})
                args.details -= 1
            print(f"  {c['case_number']} · {c['bench']} · {c['petitioner'][:40]} vs {c['respondent'][:40]} · filed {c['filed']} · next {c['next_date']} · pending {c['pending']}")
    else:
        print(scan(DB(), [x for x in args.only.split(",") if x in SITES], args.company, args.dry))
    return 0


if __name__ == "__main__":
    sys.exit(main())
