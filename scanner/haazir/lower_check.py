"""Checks for the lower court decision fetcher (haazir.lower), with no network and no database.

    python -m haazir.lower_check

The lower court details are real formats saved from High Court case pages (Rajasthan and Delhi), with the page text
that older rows carry after the district; party names in that text are replaced by made-up ones. The court complexes
are real eCourts names. The last checks run the whole lookup for one case against stand-ins for eCourts and the
database: the right complex and establishment are searched, the final order is stored once, with its label and
source_ref, and a firewall block stops the run.
"""

import asyncio
import sys
from datetime import date

from bharat_courts.models import CaseDetail, CaseInfo

from . import lower as L

D = date
# lower_court_text -> (court_name, case_type, number, year, decided, district, place)
PARSE = [
    ("MACT NO. 2 - JAIPUR · MACC - 0000202 - 2018 · decided 11-08-2021 · Jaipur Metro.",
     ("MACT NO. 2 - JAIPUR", "MACC", 202, 2018, D(2021, 8, 11), "Jaipur Metro", "Jaipur")),
    ("MACT - BHARATPUR · MACC - 0000228 - 2017 · decided 21-09-2024 · Bharatpur Ia Details Ia Number Party Date Of Filing Next Date "
     "Ia Status Applc/1/2024 Classification : Ram Lal S/O Shyam Lal 21-12-2024 -- Pending Case History",
     ("MACT - BHARATPUR", "MACC", 228, 2017, D(2024, 9, 21), "Bharatpur", "Bharatpur")),
    ("MACT 1 · MACC - 0000080 - 2022 · decided 11-07-2025 · Jaipur Metro - Ii Case History Cause List Type Judge Business On Date "
     "Hearing Date Purpose Of Hearing 06-11-2026 Incomplete",
     ("MACT 1", "MACC", 80, 2022, D(2025, 7, 11), "Jaipur Metro II", "")),
    ("MACT II - JAIPUR METRO 1 · MACC - 0000472 - 2020 · decided 08-09-2025 · Jaipur Metro - I",
     ("MACT II - JAIPUR METRO 1", "MACC", 472, 2020, D(2025, 9, 8), "Jaipur Metro I", "Jaipur Metro 1")),
    ("M.A.C.T. KEKRI - KEKRI · MACC - 0000034 - 2024 · decided 25-04-2026 · Ajmer",
     ("M.A.C.T. KEKRI - KEKRI", "MACC", 34, 2024, D(2026, 4, 25), "Ajmer", "Kekri")),
    ("MACT - DAUSA · MACC - 0000358 - 2017 · decided 11-09-2018 · Dausa Linked Cases Filing Number2 Case Number",
     ("MACT - DAUSA", "MACC", 358, 2017, D(2018, 9, 11), "Dausa", "Dausa")),
    ("M.A.C.T. SHAHPURA DISTT. JAIPUR - SHAHPURA · MACC - 0000012 - 2019 · decided 02-02-2022 · Jaipur District Main Matters "
     "Case Number : Cma/484/2019",
     ("M.A.C.T. SHAHPURA DISTT. JAIPUR - SHAHPURA", "MACC", 12, 2019, D(2022, 2, 2), "Jaipur District", "Shahpura")),
    ("MACT NEWAI · MACC - 0000101 - 2016 · decided 01-03-2019 · Tonk", ("MACT NEWAI", "MACC", 101, 2016, D(2019, 3, 1), "Tonk", "Newai")),
    ("PERMANENT LOK ADALAT - BHARATPUR · ARBC - 0000047 - 2025 · decided 23-03-2026 · Bharatpur",
     ("PERMANENT LOK ADALAT - BHARATPUR", "ARBC", 47, 2025, D(2026, 3, 23), "Bharatpur", "Bharatpur")),
    ("SR CIVIL JUDGE - KHATUSHYAM- SIKAR · CIS - 0000077 - 2015 · decided 10-10-2018 · Sikar",
     ("SR CIVIL JUDGE - KHATUSHYAM- SIKAR", "CIS", 77, 2015, D(2018, 10, 10), "Sikar", "Khatushyam- Sikar")),
    # Delhi: type, number and year run together behind a leading 2.
    ("ROHINI COURTS, DELHI · 2MACT4732019 · decided 03-07-2026 · Ia Details Ia Number Party Date Of Filing Next Date Ia Status "
     "Cm Appl./64933/2026 (64933 ) Classification : Ram Lal 20-09-2026",
     ("ROHINI COURTS, DELHI", "MACT", 473, 2019, D(2026, 7, 3), "", "Rohini")),
    ("KARKARDOOMA COURTS, DELHI · 2MACT 252021 · decided 29-04-2026", ("KARKARDOOMA COURTS, DELHI", "MACT", 25, 2021, D(2026, 4, 29), "", "Karkardooma")),
    ("SAKET COURTS, NEW DELHI · 2CS DJ501962016 · decided 27-08-2022", ("SAKET COURTS, NEW DELHI", "CS DJ", 50196, 2016, D(2022, 8, 27), "", "Saket")),
    ("ROHINI COURTS, DELHI · 2CIVIL SUIT1732022 · decided 03-01-2023", ("ROHINI COURTS, DELHI", "CIVIL SUIT", 173, 2022, D(2023, 1, 3), "", "Rohini")),
    ("PATIALA HOUSE COURTS, NEW DELHI · 2CR4462018 · decided 31-05-2019", ("PATIALA HOUSE COURTS, NEW DELHI", "CR", 446, 2018, D(2019, 5, 31), "", "Patiala House")),
    ("TIS HAZARI COURTS, DELHI · 2CS166452016 · decided -- · Ia Details Ia Number Party", ("TIS HAZARI COURTS, DELHI", "CS", 16645, 2016, None, "", "Tis Hazari")),
    ("DELHI HIGH COURT · 2CS(OS)12472008 · decided --", ("DELHI HIGH COURT", "CS(OS)", 1247, 2008, None, "", "Delhi High")),
    ("DELHI HIGH COURT · 2W.P.(C)2583 2021 · decided 28-04-2026 · Ia Details", ("DELHI HIGH COURT", "W.P.(C)", 2583, 2021, D(2026, 4, 28), "", "Delhi High")),
    ("--- · 2000 · decided --", ("", "", None, None, None, "", "")),
    ("--- · 20 · decided -- · Case History Cause List Type Judge Business On Date", ("", "", None, None, None, "", "")),
]

# lower_court_text -> searchable on eCourts?
SEARCHABLE = [
    ("MACT - TONK · MACC - 0000246 - 2016 · decided 10-07-2019 · Tonk", True),
    ("SAKET COURTS, NEW DELHI · 2MACT6182012 · decided 30-04-2025", True),
    ("PRESIDING OFFICER COOPERATIVE TRIBUNAL & MACT - JAIPUR · MACC - 0000003 - 2019 · decided 01-01-2020 · Jaipur Metro.", True),
    ("DELHI HIGH COURT · 2W.P.(C)25812021 · decided 28-04-2026", False),
    ("DEBT. RECOVERY TRIBUNAL · 2Misc Case492020 · decided 11-09-2025", False),
    ("BOARD OF REVENUE COURT - AJMER · ARBC - 0000966 - 2026 · decided 05-08-2026 · Ajmer", False),
    ("WORKMAN COMPANSETION COMMISSINOER - AJMER · WCC - 0000012 - 2019 · decided 05-08-2021 · Ajmer", False),
    ("COLLECTOR, JAIPUR - JAIPUR · RA - 0000001 - 2019 · decided 05-08-2021 · Jaipur Metro.", False),
    ("COURT OF WORKMEN COMPENSATION, NORTH DISTRICT · 2InjuryCEC/8/NDD2024 · decided 16-03-2026", False),
    ("--- · 2000 · decided --", False),
]

LABELS = [
    ("MACT NO. 2 - JAIPUR · MACC - 0000202 - 2018 · decided 11-08-2021 · Jaipur Metro.", "2021-08-11",
     "Lower court decision: MACT No. 2 Jaipur, MACC 202/2018, 11-08-2021"),
    ("MACT 1 · MACC - 0000080 - 2022 · decided 11-07-2025 · Jaipur Metro - Ii", "2025-07-11",
     "Lower court decision: MACT 1 Jaipur Metro II, MACC 80/2022, 11-07-2025"),
    ("ROHINI COURTS, DELHI · 2MACT4732019 · decided 03-07-2026", None, "Lower court decision: Rohini Courts, Delhi, MACT 473/2019, 03-07-2026"),
]

NAMES = [
    ("SMT. SUNITA DEVI W/O LATE RAM SINGH", "SUNITA DEVI"), ("SHRI MATI GUDDI DEVI W/O THANDI RAM", "GUDDI DEVI"),
    ("RAJMAN & ANR.", "RAJMAN"), ("RENU RAI  & ANR (ICICI LOMBARD GENERAL INSURANCE CO. LTD.)", "RENU RAI"),
    ("MOHD. SALEEM AHMAD & ORS.", "SALEEM AHMAD"), ("KUMARI POOJA   D/O SHRI LADU SINGH", "POOJA"),
    ("SMT. RAMOTI DEVI WIFE OF SHRI RAMESHWAR", "RAMOTI DEVI"), ("NISAR BANO W/O LATE NANNE KHAN,", "NISAR BANO"),
    ("UNITED INDIA INSURANCE CO. LTD. THROUGH BRANCH MANAGER", ""), ("M/S ICICI LOMBARD GENERAL INSURANCE CO. LTD.  & ORS.", ""),
    ("KU. ANITA D/O RAM", "ANITA"), ("STATE OF RAJASTHAN", ""), ("THE STATE BANK OF INDIA", ""),
]


def _ci(dist_code, district, code, name, ests="1,2", needs=False, state="Rajasthan", state_code="9"):
    return {"state_code": state_code, "dist_code": dist_code, "district_name": district, "complex_code": code, "complex_name": name,
            "est_codes": ests, "needs_est": needs, "state_name": state}


COMPLEXES = [
    _ci("4", "Jaipur Metro I", "1090042", "JAIPUR METRO COURT COMPLEX I"), _ci("4", "Jaipur Metro I", "1090034", "Sanganer Court Complex"),
    _ci("4", "Jaipur Metro I", "1090039", "Family Court Complex Jaipur"), _ci("4", "Jaipur Metro I", "1090041", "JJB Court Complex"),
    _ci("40", "Jaipur Metro II", "1090313", "JAIPUR METRO COURT COMPLEX II"), _ci("40", "Jaipur Metro II", "1090309", "Chomu Court Complex Jaipur Metro"),
    _ci("40", "Jaipur Metro II", "1090314", "Jaipur Railway Court Complex"), _ci("40", "Jaipur Metro II", "1090310", "Commercial Court Complex Jaipur Met"),
    _ci("38", "Jaipur District", "1090297", "Jaipur District Court Complex"), _ci("38", "Jaipur District", "1090302", "Chomu Court Complex"),
    _ci("109", "KOTPUTLI BEHROR", "1370118", "Kotputli Behror Court Complex"), _ci("106", "BEAWER", "1370126", "Beawar Court Complex"),
    _ci("14", "Ajmer", "1090137", "Ajmer District Court Complex"), _ci("14", "Ajmer", "1090135", "Kekri Court Complex"),
    _ci("20", "Kota", "1090168", "Kota District Court Complex"), _ci("20", "Kota", "1090169", "Kota MACT Court Complex"),
    _ci("9", "Udaipur", "1090091", "Kotra Court Complex"), _ci("35", "Sikar", "1090283", "Sikar District Court Complex"),
    _ci("108", "DEEG", "1370153", "Sikri Court Complex"), _ci("29", "Karauli", "1090229", "Hindauncity Court Complex"),
    _ci("29", "Karauli", "1090227", "Karauli District Court Complex"), _ci("5", "Tonk", "1090049", "Niwai Court Complex"),
    _ci("5", "Tonk", "1090043", "Tonk District Court Complex"), _ci("3", "Dausa", "1090032", "Dausa District Court Complex"),
    _ci("36", "Sawai Madhopur", "1090286", "Sawaimadhopur District Court Complex"), _ci("35", "Sikar", "1090277", "Srimadhopur Court Complex"),
    _ci("4", "North", "1260004", "Rohini Court Complex", "1,2,3,4", True, "Delhi", "26"),
    _ci("5", "North West", "1260005", "Rohini Court Complex", "1,2,3,4", True, "Delhi", "26"),
    _ci("1", "North East", "1260001", "Karkardooma Court Complex", "1,2,3,4", True, "Delhi", "26"),
]

# lower_court_text -> complex codes that must come first (in this order), and ones that must not be searched at all
RANK = [
    ("MACT 1 · MACC - 0000080 - 2022 · decided 11-07-2025 · Jaipur Metro - Ii", ["1090313"], ["1090042", "1090314", "1090310"]),
    ("MACT II - JAIPUR METRO 1 · MACC - 0000472 - 2020 · decided 08-09-2025 · Jaipur Metro - I", ["1090042"], ["1090039", "1090041", "1090313"]),
    ("MACT NO. 2 - JAIPUR · MACC - 0000202 - 2018 · decided 11-08-2021 · Jaipur Metro.", ["1090042", "1090313"], ["1090039", "1090314"]),
    ("MACT - KOTPUTALI · MACC - 0000340 - 2021 · decided 27-07-2023 · Jaipur District", ["1370118", "1090297"], []),
    ("MACT - BEAWAR · MACC - 0000202 - 2019 · decided 21-07-2022 · Ajmer", ["1370126", "1090137"], []),
    ("M.A.C.T. KEKRI - KEKRI · MACC - 0000034 - 2024 · decided 25-04-2026 · Ajmer", ["1090135"], []),
    ("MACT - KOTA · MACC - 0000217 - 2017 · decided 25-09-2019 · Kota", ["1090169", "1090168"], ["1090091"]),
    ("MACT - SIKAR · MACC - 0000387 - 2016 · decided 29-09-2021 · Sikar", ["1090283"], ["1370153"]),
    ("MACT - HINDON CITY · MACC - 0000017 - 2021 · decided 31-10-2023 · Karauli", ["1090229"], []),
    ("MACT NEWAI · MACC - 0000101 - 2016 · decided 01-03-2019 · Tonk", ["1090049"], []),
    ("MACT - SAWAI MADHOPUR · MACC - 0000197 - 2024 · decided 14-05-2025 · Sawai Madhopur", ["1090286"], []),
    ("M.A.C.T. - CHOMU · MACC - 0000015 - 2020 · decided 01-01-2023 · Jaipur District", ["1090302"], []),
    ("ROHINI COURTS, DELHI · 2MACT4732019 · decided 03-07-2026", ["1260004", "1260005"], ["1260001"]),
]

# A district case page's order tables as eCourts prints them (shortened). The links under "Final Orders / Judgements"
# are labelled "Interim Orders" too, so only the heading tells them apart.
PAGE = """<h3 class='h2class' align='center' id='orderheading'>Interim Orders </h3>
<table width="100%" class="order_table table " border="1"><thead><tr><th>Order Number</th><th>Order Date</th><th>Order Details</th></tr></thead>
<tr><td>&nbsp;&nbsp;1</td><td style='border-top:none;'>&nbsp;&nbsp;05-01-2021</font></td><td colspan='3'><a href='#' aria-label= 'Interim Orders | Order on Exhibit on 05-01-2021 ' onclick=displayPdf('n1','c1','k1','f1','') ><font> Copy of order </font></a></td></tr>
<tr><td>&nbsp;&nbsp;2</td><td style='border-top:none;'>&nbsp;&nbsp;11-08-2021</font></td><td colspan='3'><a href='#' aria-label= 'Interim Orders | Order on 11-08-2021 ' onclick=displayPdf('n2','c2','k2','f2','') ><font> Copy of order </font></a></td></tr>
</table><table id='orderheading' align='center'><tr><td><br/><h3 class='h2class' style='font-weight:bold;'>Final Orders / Judgements  </h3></td></tr></table>
<table width="100%" class="order_table table " border="1"><tr><th>Order Number</th><th>Order Date</th><th>Order Details</th></tr>
<tr><td>&nbsp;&nbsp;3</td><td style='border-top:none;'>&nbsp;&nbsp;11-08-2021</font></td><td colspan='3'><span><a href='#' aria-label= 'Interim Orders | Order on Exhibit on 2021-08-11 ' onclick=displayPdf('n3','c3','k3','f3','')><font> Copy of order </font></a></span></td></tr>
</table>"""
PAGE_INTERIM_ONLY = PAGE.split("</table><table id='orderheading'")[0] + "</table>"


def run(check) -> None:
    for text, want in PARSE:
        p = L.parse(text)
        got = (p["court_name"], p["case_type"], p["number"], p["year"], p["decided"], p["district"], p["place"])
        check(got == want, f"lower court text read: {text[:60]!r} -> {got}, expected {want}")
    for text, want in SEARCHABLE:
        why = L.unsearchable(L.parse(text))
        check((not why) == want, f"searchable on eCourts: {text[:50]!r} -> {why or 'yes'}")
    for text, order_date, want in LABELS:
        got = L.label(L.parse(text), order_date)
        check(got == want and got.startswith("Lower court decision"), f"label {got!r}, expected {want!r}")
    for name, want in NAMES:
        check(L.search_name(name) == want, f"party name to search: {name!r} -> {L.search_name(name)!r}, expected {want!r}")
    case = {"company_role": "petitioner", "petitioners": [{"name": "UNITED INDIA INSURANCE COMPANY LIMITED"}],
            "respondents": [{"name": "SMT. SUNITA DEVI W/O LATE RAM SINGH"}, {"name": "SUNITA DEVI"}, {"name": "KAMAL S/O RAM"}, {"name": "MOHAN LAL"}]}
    check(L.party_terms(case) == ["SUNITA DEVI", "KAMAL"], f"claimants searched first, once each, at most {L.MAX_NAMES}: {L.party_terms(case)}")
    case = {"company_role": "respondent", "petitioners": [{"name": "GUDDI W/O RAM"}], "respondents": [{"name": "HEMRAJ S/O JAI"}]}
    check(L.party_terms(case)[0] == "GUDDI", "insurer as respondent: the appellants (claimants) first")

    for text, first, never in RANK:
        p = L.parse(text)
        state = [c for c in COMPLEXES if c["state_name"] == ("Delhi" if "DELHI" in text else "Rajasthan")]
        codes = [c["complex_code"] for c in L.rank_complexes(p, state)]
        check(codes[:len(first)] == first and not set(codes) & set(never) and len(codes) <= L.MAX_COMPLEXES,
              f"complexes for {p['court_name']!r} / {p['district']!r}: {codes}, expected {first} first and never {never}")
    ests = {"1": "District and Sessions Judge, North West", "2": "Senior Civil Judge cum RC, North West", "3": "Chief Metropolitan Magistrate, North West"}
    check(L.rank_establishments(L.parse("ROHINI COURTS, DELHI · 2MACT4732019 · decided 03-07-2026"), ests)[0] == "1", "Delhi MACT: the Sessions establishment first")
    check(L.rank_establishments(L.parse("PATIALA HOUSE COURTS, NEW DELHI · 2CR4462018 · decided 31-05-2019"), ests)[0] == "3", "Delhi criminal case: the magistrates first")
    check(L.rank_establishments(L.parse("ROHINI COURTS, DELHI · 2CIVIL SUIT1732022 · decided 03-01-2023"), ests)[0] == "2", "Delhi civil suit: the civil judges first")
    check(L.rank_establishments(L.parse("SAKET COURTS, NEW DELHI · 2CS DJ501962016 · decided 27-08-2022"), ests)[0] == "1", "Delhi district judge's suit (CS DJ): the District Judge first")

    p = L.parse("MACT NO. 2 - JAIPUR · MACC - 0000202 - 2018 · decided 11-08-2021 · Jaipur Metro.")
    rows = [CaseInfo(case_number="MACT Main/202/2017", case_type="MACT Main", cnr_number="RJJP010000012017"),
            CaseInfo(case_number="Civil Misc/202/2018", case_type="Civil Misc", cnr_number="RJJP010000022018"),
            CaseInfo(case_number="MACT Main/0202/2018", case_type="MACT Main", cnr_number="RJJP010000032018"),
            CaseInfo(case_number="MACT Main/1202/2018", case_type="MACT Main", cnr_number="RJJP010000042018")]
    check([r.cnr_number for r in L.matches(rows, p)] == ["RJJP010000032018", "RJJP010000022018"], "same number and year only, the claim case first")
    check(L.same_case(CaseDetail(registration_number="202/2018", decision_date=D(2021, 8, 11)), p) == "", "case page with the same number and date is the lower case")
    check(L.same_case(CaseDetail(registration_number="202/2018", decision_date=D(2021, 8, 12)), p) != "", "a different decision date is not taken")
    check(L.same_case(CaseDetail(registration_number="203/2018"), p) != "", "a different number on the case page is not taken")

    orders = L.page_orders(PAGE)
    check([(o["date"], o["final"]) for o in orders] == [("2021-01-05", False), ("2021-08-11", False), ("2021-08-11", True)],
          f"order links read with their table: {[(o['date'], o['final']) for o in orders]}")
    pick = lambda html, d: (L.pick_decision(L.page_orders(html), d) or {}).get("args", [None])[0]
    check(pick(PAGE, D(2021, 8, 11)) == "n3", "decision date: the final order of that day")
    check(pick(PAGE, D(2021, 8, 20)) == "n3", "no order that day: the nearest final order")
    check(pick(PAGE, None) == "n3", "no decision date: the last final order")
    check(pick(PAGE_INTERIM_ONLY, D(2021, 8, 12)) == "n2", "no final table: the order within 3 days of the decision")
    check(pick(PAGE_INTERIM_ONLY, D(2021, 9, 30)) is None, "no final table and nothing near the decision date: nothing")

    rows = [{"id": "a", "next_hearing_date": "2026-11-05"}, {"id": "b", "next_hearing_date": None}, {"id": "c", "next_hearing_date": "2026-10-01"},
            {"id": "d", "next_hearing_date": "2026-10-30"}, {"id": "e", "next_hearing_date": "2026-09-01"}]
    check([r["id"] for r in L.order_for_batch(rows, D(2026, 10, 29))] == ["d", "a", "c", "e", "b"], "batch: nearest upcoming hearing first")

    asyncio.run(flow(check))


# ---------------------------------------------------------------- the whole lookup against stand-ins

class FakeDB:
    def __init__(self, complexes: list[dict]) -> None:
        self.complexes, self.docs, self.logs = complexes, [], []

    def select(self, table: str, query: str = "") -> list[dict]:
        if table == "court_index":
            return [c for c in self.complexes if "state_name=eq.Rajasthan" in query and c["state_name"] == "Rajasthan"
                    or "state_name=eq.Delhi" in query and c["state_name"] == "Delhi"]
        if table == "documents":
            return [d for d in self.docs if f"case_id=eq.{d['case_id']}" in query]
        return []

    def log(self, case_id, action, meta=None, actor="scanner") -> None:
        self.logs.append((case_id, action, meta))


class FakeClient:
    async def list_establishments(self, state, dist, complex_code):
        return {"1": "District and Sessions Judge, North", "2": "Senior Civil Judge cum RC, North"}

    async def list_states(self):
        return {"9": "Rajasthan", "26": "Delhi"}


class FakeEC:
    """eCourts holding one case at one establishment."""

    def __init__(self, complex_code: str, est: str, row: CaseInfo, detail: CaseDetail, html: str, block: bool = False) -> None:
        self.client, self.where, self.row, self.d, self.html, self.block = FakeClient(), (complex_code, est), row, detail, html, block
        self.searched: list[tuple[str, str, str, int]] = []
        self.last_html = ""

    async def search(self, court, term, year, status="Both"):
        from .ecourts import Blocked
        if self.block:
            raise Blocked()
        est = court["est_codes"] if court["needs_est"] else ""
        self.searched.append((court["complex_code"], est, term, year))
        return [self.row] if (court["complex_code"], est) == self.where and year == 2019 else []

    async def detail(self, cnr):
        self.last_html = self.html
        return self.d


async def flow(check) -> None:
    from . import highcourt, orders
    from .ecourts import Blocked
    saved = orders.store_order, orders.read_with_ai, orders.district_pdf, highcourt.link_lower
    stored, read = [], []

    def store(db, case_id, pdf, order_date, label, source_ref, kind="order"):
        if any(d["case_id"] == case_id for d in db.docs):
            return None
        db.docs.append({"case_id": case_id, "title": label, "source_ref": source_ref, "order_date": order_date, "kind": kind})
        stored.append(db.docs[-1])
        return f"doc{len(stored)}"

    async def pdf(ec, args):
        return b"%PDF-1.4 " + args[0].encode()

    orders.store_order, orders.read_with_ai, orders.district_pdf = store, (lambda ids, limit=20: read.extend(ids) or len(ids)), pdf
    highcourt.link_lower = lambda db, lc: None
    try:
        case = {"id": "hc1", "case_number": "MAC.APP./252/2026", "company_role": "petitioner", "court": {"state": "Delhi"},
                "lower_court_text": "ROHINI COURTS, DELHI · 2MACT4732019 · decided 11-08-2021 · Ia Details Ia Number Party",
                "petitioners": [{"name": "THE UNITED INDIA INSURANCE COMPANY LIMITED"}], "respondents": [{"name": "RAJMAN & ANR."}]}
        row = CaseInfo(case_number="MACT/473/2019", case_type="MACT", cnr_number="DLNW010012342019")
        page = CaseDetail(registration_number="473/2019", decision_date=D(2021, 8, 11))
        db = FakeDB(COMPLEXES)
        ec = FakeEC("1260005", "1", row, page, PAGE)
        result, what = await L.fetch(ec, db, case, L.Complexes(db))
        check(result == "found", f"Delhi MACT case found: {result} ({what})")
        check(ec.searched and ec.searched[0][1] == "1" and all(s[2] == "RAJMAN" and s[3] == 2019 for s in ec.searched),
              f"searched the claimant's name and the registration year, Sessions establishment first: {ec.searched}")
        check(len(ec.searched) <= L.MAX_SEARCHES, f"at most {L.MAX_SEARCHES} searches ({len(ec.searched)})")
        doc = stored[0] if stored else {}
        check(doc.get("title") == "Lower court decision: Rohini Courts, Delhi, MACT 473/2019, 11-08-2021", f"title {doc.get('title')!r}")
        check(doc.get("source_ref") == "lower-court:DLNW010012342019 MACT 473/2019 2021-08-11", f"source_ref {doc.get('source_ref')!r}")
        check(doc.get("order_date") == "2021-08-11" and doc.get("kind") == "lower_decision" and read == [],
              "the final order of the decision date stored for drafting, not read as the appeal's own order")
        result, what = await L.fetch(ec, db, case, L.Complexes(db))
        check(result == "skipped" and len(stored) == 1, f"second run stores nothing: {result} ({what})")

        db2 = FakeDB(COMPLEXES)
        ec2 = FakeEC("9999999", "", row, page, PAGE)
        result, what = await L.fetch(ec2, db2, case, L.Complexes(db2))
        check(result == "not found" and not db2.docs and len(ec2.searched) <= L.MAX_SEARCHES, f"not on eCourts: {result} ({what})")

        ec3 = FakeEC("1260005", "1", row, CaseDetail(registration_number="473/2019", decision_date=D(2022, 1, 1)), PAGE)
        result, what = await L.fetch(ec3, FakeDB(COMPLEXES), case, L.Complexes(FakeDB(COMPLEXES)))
        check(result == "not found" and "decided" in what, f"same number but another decision date is not stored: {what}")

        blocked = False
        try:
            await L.fetch(FakeEC("1260005", "1", row, page, PAGE, block=True), FakeDB(COMPLEXES), case, L.Complexes(FakeDB(COMPLEXES)))
        except Blocked:
            blocked = True
        check(blocked, "a firewall block stops the lookup")

        class Broken(FakeEC):
            async def search(self, court, term, year, status="Both"):
                self.searched.append((court["complex_code"], "", term, year))
                raise RuntimeError("CAPTCHA failed after 5 attempts")
        ec5 = Broken("1260005", "1", row, page, PAGE)
        result, what = await L.fetch(ec5, FakeDB(COMPLEXES), case, L.Complexes(FakeDB(COMPLEXES)))
        check(result == "failed" and ec5.searched, f"every search failing is a failure to retry, not 'not found': {result} ({what})")

        hc_writ = {**case, "id": "hc2", "lower_court_text": "DELHI HIGH COURT · 2W.P.(C)25812021 · decided 28-04-2026"}
        result, what = await L.fetch(ec, FakeDB(COMPLEXES), hc_writ, L.Complexes(FakeDB(COMPLEXES)))
        check(result == "skipped", f"an appeal from a High Court writ is not looked up on eCourts: {what}")
    finally:
        orders.store_order, orders.read_with_ai, orders.district_pdf, highcourt.link_lower = saved


def main() -> int:
    failed, passed = [], [0]

    def check(ok: bool, what: str) -> None:
        if ok:
            passed[0] += 1
        else:
            failed.append(what)
            print(f"FAIL {what}")

    run(check)
    print(f"{passed[0]} of {passed[0] + len(failed)} lower court checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
