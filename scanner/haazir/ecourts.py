"""Polite access to the eCourts district-court portal via bharat-courts.

One session, one request at a time, a fixed gap between requests. If the portal's
firewall answers with its "Security Page" (HTTP 405) we stop the run instead of retrying,
because retrying only extends the block.
"""

import re

import httpx
from bs4 import BeautifulSoup
from bharat_courts.config import BharatCourtsConfig
from bharat_courts.casedetail import parse_case_detail
from bharat_courts.districtcourts import endpoints as dc_endpoints
from bharat_courts.districtcourts.client import DistrictCourtClient
from bharat_courts.districtcourts.parser import CaptchaError, MalformedResponseError, _extract_parties
from bharat_courts.models import CaseInfo

from . import config
from .captcha import MultiReadSolver


class Blocked(Exception):
    """The eCourts firewall is refusing our requests."""


class LayoutError(Exception):
    """A results page did not read cleanly: rows without a CNR, or fewer rows than the page's own total.
    Raised instead of returning partial data, so a new state's page layout cannot fail silently."""


# State (2 letters) + district (2, letters or digits: "JM", "09") + establishment (2, letters or digits:
# "01", "1C") + filing serial (6 digits) + year (4 digits). Examples: RJJM010276512026, MP09010638602026,
# RJJT1C0007402026. The library's parser required four letters then twelve digits, so it silently dropped
# MP cases and Rajasthan establishments with a letter in their code.
CNR_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{4}\d{10}$")
_CNR_IN_LINK = re.compile(r"'([A-Z]{2}[A-Z0-9]{4}\d{10})'")
_PAGE_TOTAL = re.compile(r"Total number of cases\s*:\s*(\d+)", re.I)


def parse_results(html: str) -> list[CaseInfo]:
    """Read a party-search results page.

    The library's parser only accepts CNRs starting with four letters, so states whose district code is
    numeric (Madhya Pradesh: "MP09...") came back with empty CNRs. Read the rows here and check them
    against the total the page itself states."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    rows: list[CaseInfo] = []
    unreadable = 0
    for tr in table.find_all("tr") if table else []:
        if tr.find("th"):
            continue
        cols = tr.find_all("td")
        if len(cols) < 3:
            continue
        number = " ".join(cols[1].get_text(" ").split())
        pet, resp = _extract_parties(cols[2])
        cnr = next((m.group(1) for a in tr.find_all("a") if (m := _CNR_IN_LINK.search(a.get("onclick", "")))), "")
        if not cnr:
            unreadable += 1
            continue
        rows.append(CaseInfo(case_number=number, case_type=number.split("/")[0] if "/" in number else "",
                             cnr_number=cnr, petitioner=pet, respondent=resp))
    totals = [int(n) for n in _PAGE_TOTAL.findall(html)]
    stated = totals[0] if totals else None
    if unreadable or (stated is not None and stated != len(rows)):
        raise LayoutError(f"read {len(rows)} rows, {unreadable} without a CNR; page says {stated} cases")
    return rows


def _is_block(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 405


class ECourts:
    def __init__(self) -> None:
        cfg = BharatCourtsConfig(request_delay=config.REQUEST_DELAY, max_retries=2)
        self.client = DistrictCourtClient(config=cfg, captcha_solver=MultiReadSolver())
        self._session_ready = False
        self.last_html = ""  # raw case page of the latest detail() call: proceedings and order links

    async def __aenter__(self) -> "ECourts":
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *args) -> None:
        await self.client.__aexit__(*args)

    async def list_complexes(self, state_code: str, dist_code: str) -> dict[str, str]:
        try:
            return await self.client.list_complexes(state_code, dist_code)
        except Exception as exc:
            if _is_block(exc):
                raise Blocked() from exc
            raise

    async def search(self, court: dict, term: str, year: int, status: str = "Both"):
        # Complexes flagged needs_est are split into separate establishments (e.g. JM, CJ JD, ADJ),
        # and the portal only searches the one you pick. Search each of them and merge.
        ests = [e for e in court["est_codes"].split(",") if e] if court["needs_est"] else [""]
        c = self.client
        code = c._bare_complex_code(court["complex_code"])
        merged, seen = [], set()
        for est in ests:
            try:
                result = await c._post_with_captcha_retry(
                    "casestatus/submitPartyName",
                    lambda captcha: dc_endpoints.case_status_by_party_form(
                        state_code=court["state_code"], dist_code=court["dist_code"], court_complex_code=code,
                        est_code=est, party_name=term, year=str(year), status_filter=status, captcha=captcha),
                    state_code=court["state_code"], dist_code=court["dist_code"], court_complex_code=code, est_code=est,
                )
                rows = parse_results(result.get("party_data", "") or "")
            except LayoutError:
                raise
            except Exception as exc:
                if _is_block(exc):
                    raise Blocked() from exc
                raise
            for r in rows:
                if r.cnr_number not in seen:
                    seen.add(r.cnr_number)
                    merged.append(r)
        return merged

    async def search_advocate(self, court: dict, name: str, status: str = "Pending"):
        """An advocate's cases at one court complex (no year: the advocate's whole book with that status)."""
        ests = [e for e in court["est_codes"].split(",") if e] if court["needs_est"] else [""]
        c = self.client
        code = c._bare_complex_code(court["complex_code"])
        merged, seen = [], set()
        for est in ests:
            try:
                result = await c._post_with_captcha_retry(
                    "casestatus/submitAdvName",
                    lambda captcha: {"radAdvt": "1", "advocate_name": name, "adv_bar_state": "", "adv_bar_code": "", "adv_bar_year": "",
                                     "case_status": status, "caselist_date": "", "adv_captcha_code": captcha, "state_code": court["state_code"],
                                     "dist_code": court["dist_code"], "court_complex_code": code, "est_code": est, "case_type": ""},
                    state_code=court["state_code"], dist_code=court["dist_code"], court_complex_code=code, est_code=est,
                )
                rows = parse_results(result.get("adv_data", "") or "")
            except LayoutError:
                raise
            except Exception as exc:
                if _is_block(exc):
                    raise Blocked() from exc
                raise
            for r in rows:
                if r.cnr_number not in seen:
                    seen.add(r.cnr_number)
                    merged.append(r)
        return merged

    async def detail(self, cnr: str):
        """CNR lookup that keeps one portal session across many cases.

        bharat-courts' case_status_by_cnr reloads the home page, its scripts and a token before
        every attempt (about five requests). The session stays valid between lookups, so we set
        it up once and afterwards only fetch a fresh CAPTCHA per case, re-initialising on failure.
        """
        c = self.client
        if not cnr:
            raise ValueError("empty CNR")
        try:
            if not self._session_ready:
                await c._init_session()
                self._session_ready = True
            for _ in range(4):
                captcha = await c._solve_captcha()
                if not captcha:
                    continue
                try:
                    result = await c._post_ajax("cnr_status/searchByCNR",
                                                dc_endpoints.case_status_by_cnr_form(cnr=cnr, captcha=captcha))
                except (CaptchaError, MalformedResponseError):
                    await c._init_session()
                    continue
                html = result.get("casetype_list") or result.get("historytable") or ""
                if html:
                    self.last_html = html
                    return parse_case_detail(html, cnr=cnr, base_url=dc_endpoints.BASE_URL)
                await c._init_session()
            # Fall back to the library's own flow (fresh session per attempt).
            self._session_ready = False
            self.last_html = ""
            return await c.case_status_by_cnr(cnr)
        except Exception as exc:
            # Any other failure can leave the portal session or its rotating token unusable;
            # start the next lookup from a fresh session instead of failing every case after it.
            self._session_ready = False
            if _is_block(exc):
                raise Blocked() from exc
            raise
