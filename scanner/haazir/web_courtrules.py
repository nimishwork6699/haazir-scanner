"""Items 11 and 10 of the library plan: court rules, practice directions and High Court downloadable forms, from the
courts' own websites (no captcha, no login).

    python -m haazir.web_courtrules list [court ...]     # what each court's pages list (nothing stored)
    python -m haazir.web_courtrules load [court ...]     # store new documents (all courts when none named)
    python -m haazir.web_courtrules status               # listed vs stored per court, bytes per store
    python -m haazir.web_courtrules rebalance            # drop unreferenced files; keep Backblaze court-rules/ under 180 MB
    python -m haazir.web_courtrules verify [n]           # n random documents per item read back and checked against the live file
    python -m haazir.web_courtrules probe <url> ...      # the document links a page carries (to add a court)
    python -m haazir.web_courtrules nav <url> ...        # menu links that look like rules / forms pages

Courts: sc, ngt, nclat and the High Courts by short name (delhi, bombay, allahabad, madras, …); see COURTS.

Where things go
  Backblaze court-rules/<court>/<file>           rules, practice directions, standing orders, SOPs (item 11)
  Backblaze formats/hc/<court>/<file>            High Court downloadable forms (item 10)
  Google Drive library/court-rules/<court>/      any file over 15 MB (Backblaze keeps only its text), and every
                                                 file once Backblaze court-rules/ passes 190 MB
  court-rules/<court>/text.jsonl.gz, formats/hc/<court>/text.jsonl.gz: one row per document (title, number, date,
  body, category, extracted text, source URL, where the file is); court-rules/<court>/index.jsonl.gz: what the
  court's pages listed and how each document fared (the check that a load was complete).

Many court sites send an incomplete certificate chain that Python's TLS rejects; those are read with curl, which on
Windows verifies certificates against the Windows store (fetching the missing intermediate, as browsers do).
"""

import argparse
import re
import subprocess
import sys
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from .storage import unpack
from .web_common import UA, Blocked, Group, Library, Site, absolute, doc_row, fetch_doc, file_name, index_file, iso_date

DOC = re.compile(r"\.(pdf|docx?|rtf|odt)(\?|#|$)", re.I)
BIG = 15_000_000           # bytes: a file larger than this goes to Drive, its text stays in Backblaze
B2_CAP = 180_000_000       # bytes: Backblaze court-rules/ is kept under ~200 MB (margin for courts loading side by side)
PRACTICE_CAP = 120_000_000  # practice directions and notices use Backblaze only up to here; the rest go to Drive
GENERIC = re.compile(r"^(download|view|pdf|docx?|odt|rtf|click here.*|here|english|hindi|open|file|link|read more|more|"
                     r"view\s*/\s*download|accessible version.*|download\s*\(.*\)|view\s*\(.*\)|\(?[\d.,]+\s*[kmg]b\)?|\d{1,3}\.?)$", re.I)
DATEISH = re.compile(r"^\s*\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\s*$")
# Not court rules or forms for litigation: staff administration, recruitment, notices of the day.
EXCLUDE = re.compile(r"telephone|directory|recruit|tender|holiday|calendar|newsletter|annual report|transfer (order|list|polic|guideline)|"
                     r"transfer and posting|retired|internship|guest house|canteen|cycle stand|medical (facilit|reimburse|attendance|bill)|"
                     r"accommodation|gpf|pension|leave travel|\bltc\b|panel advocates|telegram|\bresults?\b|candidates|admit card|syllabus|"
                     r"(written|preliminary|main|screening) examination|vacanc|advertisement|seniority list|cause ?list|statistics|"
                     r"^the bharatiya (nyaya|nagarik suraksha) sanhita, 2023$|^the bharatiya sakshya adhiniyam, 2023$|"
                     r"constitution ?of ?india|coi\.pdf|right to information|\brti\b|\bacr\b|confidential (report|roll)|"
                     r"vigilance|sexual harassment|she-?box|press release|vision document|procurement|furnishing|condemnation|"
                     r"drivers?\b|class-?iv|stenograph|ministerial establishment|staff (service|car)|private secretar|bench secretar|"
                     r"computer cadre|court managers?|system officer|research assistant|law clerk|journey|saga|souvenir|"
                     r"waste management|e-?waste|printer|vpn|site preparation|action plan|user manual|tutorial|feedback|"
                     r"list of (courts|judges|officers)|presiding officers|email registration|sms alert|sitting day|"
                     r"service rules|technical man ?power|conditions of service|\bbill\b|"
                     r"full court reference|condole|vacation judge|standing counsel|nomination of|inspecting judges|pool car|"
                     r"telephone|mediators of|creche|onam|christmas|dussehra|diwali|puja", re.I)

# One entry per court. pages: (url, category, options). category: rules | practice | forms.
# options: only = regex the document URL must match; titled = regex the title must match; follow = regex of HTML
# sub-pages to open (one level) for their documents; any = regex of links that are documents though they have no
# file extension; pages = how many '?page=N' pages to walk; key = 'title' when a site's download links change on
# every visit (signed links), so a document is known by its page and title instead.
SCI_CDN = "https://cdnbbsr.s3waas.gov.in/s3ec0490f1f4972d133619a60c30f3559e"
COURTS: dict[str, dict] = {
    # sci.gov.in answers 403 to this loader's own user agent, so its rules page is not read; the files it lists sit on
    # the government CDN (s3waas), which serves them, so the known files are read from there directly.
    "sc": {"name": "Supreme Court of India", "pages": [
        ("https://www.sci.gov.in/supreme-court-rules/", "rules", {"only": r"s3waas"})], "direct": [
        (f"{SCI_CDN}/uploads/2024/01/2024011691-1.pdf", "Supreme Court Rules, 2013", "rules"),
        (f"{SCI_CDN}/uploads/2024/07/2024073073.pdf", "Gazette Notification dated 16.07.2024 regarding amendment in Order II of Supreme Court Rules 2013", "rules"),
        (f"{SCI_CDN}/uploads/2024/11/2024110732.pdf", "Gazette Notification dated 05.11.2024 regarding Supreme Court (Second Amendment) Rules, 2024", "rules"),
        (f"{SCI_CDN}/uploads/2025/04/2025042231.pdf", "Gazette Notification dated 21.04.2025 regarding Supreme Court Amendment Rules, 2025", "rules"),
        (f"{SCI_CDN}/uploads/2025/06/2025061698.pdf", "Gazette Notification dated 14.06.2025 Supreme Court (Amendment) Rules, 2025", "rules"),
        (f"{SCI_CDN}/documents/misc/sc_rules1966.pdf", "Supreme Court Rules, 1966", "rules"),
        ("https://cdn.s3waas.gov.in/s3ec0490f1f4972d133619a60c30f3559e/uploads/2025/12/2025121262.pdf",
         "Practice and Procedure: a handbook of information (Supreme Court of India, December 2025)", "practice")]},
    "ngt": {"name": "National Green Tribunal", "fetch": "httpx", "pages": [
        ("https://www.greentribunal.gov.in/ngt-practise-procedure-rules", "rules", {"only": r"act_rules"})]},
    "nclat": {"name": "National Company Law Appellate Tribunal", "pages": [
        ("https://nclat.nic.in/act-rules", "rules", {"titled": r"rules|regulations"})]},
    "delhi": {"name": "High Court of Delhi", "chrome": "https://delhihighcourt.nic.in/web/contact-us", "pages": [
        ("https://delhihighcourt.nic.in/web/court-rules", "rules", {"pages": 40}),
        ("https://delhihighcourt.nic.in/web/notifications-practice-directions", "practice", {"pages": 80}),
        ("https://delhihighcourt.nic.in/web/vc-rules", "rules", {}),
        ("https://delhihighcourt.nic.in/web/live-streaming-rules", "rules", {}),
        ("https://delhihighcourt.nic.in/web/online-forms-downloadable-forms", "forms", {"pages": 10})]},
    "bombay": {"name": "High Court of Bombay", "pages": [
        ("https://bombayhighcourt.gov.in/bhc/libweb/bhcrule/OSRules/bhcosrules.html", "rules", {"forms_re": r"/forms/"}),
        ("https://bombayhighcourt.gov.in/bhc/libweb/bhcrule/bhcasrules/BHCASR.html", "rules", {"forms_re": r"/forms?/"}),
        ("https://bombayhighcourt.gov.in/bhc/PracticeNotes", "practice", {"any": r"/bhc/file/download/", "key": "title"})]},
    # The Rules of Court are not linked from any page a script can read (the menu is drawn by the browser); the file
    # named in reports/Drafting sources.md is read directly.
    "allahabad": {"name": "High Court of Judicature at Allahabad", "pages": [
        ("https://www.allahabadhighcourt.in/publications.html", "rules", {"only": r"\.pdf"})], "direct": [
        ("https://www.allahabadhighcourt.in/rules/hcrulespartIchItoVIII_27-03-2023.pdf", "Rules of the Court, 1952 (Allahabad High Court), Chapters I to VIII, as on 27.03.2023", "rules")]},
    "ap": {"name": "High Court of Andhra Pradesh", "chrome": "https://aphc.gov.in/formerjudges.php?type=former_cj", "pages": [
        ("https://aphc.gov.in/rules.php?type=2", "rules", {}),
        ("https://aphc.gov.in/rules.php?type=5", "practice", {}),
        ("https://aphc.gov.in/", "rules", {"only": r"/docs/notification_", "titled": r"rules|practice|procedure"})]},
    "madras": {"name": "High Court of Madras", "pages": [
        ("https://hcmadras.tn.gov.in/rules.php", "rules", {"post": r"view_pdf\.php", "post_key": ["pdf_id", "page"]}),
        ("https://hcmadras.tn.gov.in/efiling_notification.php", "practice", {"post": r"view_pdf\.php", "post_key": ["pdf_id", "page"]})]},
    "kerala": {"name": "High Court of Kerala",
               "exclude": r"sitting|today[- ]moved|order summary|adalat|reference in honour|retirement|lists? of|not sitting|"
                          r"chamber|pre-settlement|defect|mediators|ksmcc|panel|selection", "pages": [
        ("https://highcourt.kerala.gov.in/", "practice", {"post": r"download_file\.php", "post_key": ["file_name"], "scope": "#practice_direction_om_modal"}),
        ("https://highcourt.kerala.gov.in/", "rules", {"post": r"download_file\.php", "post_key": ["file_name"], "titled": r"\brules?\b|practice|procedure"})]},
    "calcutta": {"name": "High Court at Calcutta", "pages": [
        ("https://www.calcuttahighcourt.gov.in/Notices/PD", "practice", {"any": r"/Notice-Files/PD/\d+", "pages": 30}),
        ("https://www.calcuttahighcourt.gov.in/Notices/gazette-notification", "rules", {"any": r"/Notice-Files/[\w-]+/\d+", "pages": 30,
                                                                                         "titled": r"rules?|practice|procedure|amend"})]},
    "chhattisgarh": {"name": "High Court of Chhattisgarh", "chrome": "https://highcourt.cg.gov.in/about/contact_us.php",
                     "exclude": r"jo code|assets|^staff$|master_trainers|legal glossary|district system administrator", "pages": [
        ("https://highcourt.cg.gov.in/information/rule/rule.php", "rules", {}),
        ("https://highcourt.cg.gov.in/", "forms", {"only": r"/download/forms/"})]},
    "gujarat": {"name": "High Court of Gujarat", "chrome": "https://gujarathighcourt.nic.in/cntdtls", "pages": [
        ("https://gujarathighcourt.nic.in/rules", "rules", {"only": r"rules_files"}),
        ("https://gujarathighcourt.nic.in/form", "forms", {"only": r"/hcform/"})]},
    # Gauhati, J&K and Patna: Windows (curl) reports these sites' certificates as revoked; Python's TLS check (which does
    # not look up revocation) accepts them, so they are read with httpx.
    "gauhati": {"name": "Gauhati High Court", "fetch": "httpx", "pages": [
        ("https://ghconline.gov.in/index.php/gauhati-high-court-rules11/", "rules",
         {"only": r"/Document/", "name_file": True, "forms_re": r"Appendix-I-II-Forms|Crl-forms"}),
        ("https://ghconline.gov.in/", "forms", {"only": r"FilingForm\.(pdf|docx)"})]},
    "jk": {"name": "High Court of Jammu & Kashmir and Ladakh", "fetch": "httpx", "pages": [
        ("https://jkhighcourt.nic.in/", "rules", {"scope": "#collapseThirteen"}),
        ("https://jkhighcourt.nic.in/", "forms", {"scope": "#collapseEighteen"})],
        "exclude": r"leave rules|conduct rules|classification, control|staff \(conditions|employees"},
    "patna": {"name": "High Court of Judicature at Patna", "fetch": "httpx", "pages": [
        ("https://patnahighcourt.gov.in/notices/rules", "rules", {"any": r"/(getfile|getpdffile|viewpdf)/[^?]*$",
                                                                  "forms_re": r"TGlzdENyU3lub3BzaXMucGRm",
                                                                  "rewrite": ("/viewpdf/", "/getpdffile/")})],
        "exclude": r"committees|hospital|law assistant|corrigendum regarding notice|technical cadre|conduct|appelate authority|pio"},
    "jharkhand": {"name": "High Court of Jharkhand", "chrome": "https://jharkhandhighcourt.nic.in/contactus.php", "pages": [
        ("https://jharkhandhighcourt.nic.in/rules.php", "rules", {"any": r"pdfshow\.php"})]},
    "manipur": {"name": "High Court of Manipur", "pages": [
        ("https://hcmimphal.nic.in/courtrules.html", "rules", {})]},
    "meghalaya": {"name": "High Court of Meghalaya", "chrome": "https://meghalayahighcourt.nic.in/contact-us", "pages": [
        ("https://meghalayahighcourt.nic.in/standing-order", "practice", {}),
        ("https://meghalayahighcourt.nic.in/rules-and-regulation", "rules", {"follow": r"rules|amendment", "scope": "main, #main-content, .region-content"})]},
    "rajasthan": {"name": "High Court of Judicature for Rajasthan", "chrome": "https://hcraj.nic.in/hcraj/rti.php",
                  "exclude": r"allocation of judgeships|work assignment|legal service committee|judge incharge|administrative judge|"
                             r"inspection cell|quarterly returns|mediation centre", "pages": [
        ("https://hcraj.nic.in/hcraj/rules.php", "rules", {}),
        ("https://hcraj.nic.in/hcraj/standing-order.php", "practice", {}),
        ("https://hcraj.nic.in/hcraj/efiling.php", "practice", {})]},
    "telangana": {"name": "High Court for the State of Telangana", "pages": [
        ("https://tshc.gov.in/showChildDocTypes?id=21", "rules", {"titled": r"rule|practice|manual|guideline|procedure|sop|notice regarding party"})]},
    "tripura": {"name": "High Court of Tripura", "pages": [
        ("https://thc.nic.in/form.html", "forms", {})]},
    "uttarakhand": {"name": "High Court of Uttarakhand", "pages": [
        ("https://highcourtofuttarakhand.gov.in/document-category/policies-guidelines/", "practice", {}),
        ("https://highcourtofuttarakhand.gov.in/document-category/acts-rules/", "rules", {"follow": r"/document/(?!.*vision)", "scope": "main, #main, .content-area"})]},
    # Orissa: each rules page shows its volumes through a viewer page (…-pdf-view/N/) that embeds the PDF.
    "orissa": {"name": "High Court of Orissa", "pages": [
        *[(f"https://www.orissahighcourt.nic.in/rules/{slug}/", "rules",
           {"scope": "section.wrapper", "follow": r"pdf-view/\d+", "sub_scope": "section.wrapper"})
          for slug in ("orissa-highcourt-rules", "general-rules-and-circular-orders", "civil-procedure-mediation-rules",
                       "alternate-dispute-resolution-rules", "familycourt-rules", "pil", "DesignationSeniorAdvocateRules", "cglc-rules",
                       "rules/arbitration-rule")],
        ("https://www.orissahighcourt.nic.in/", "rules", {"only": r"uploads/rules/|vc_rules|live_streaming|Admiralty_Rules"}),
        ("https://www.orissahighcourt.nic.in/forms/", "forms", {"scope": "section.wrapper", "follow": r"pdf-view/\d+", "sub_scope": "section.wrapper"})]},
    "mp": {"name": "High Court of Madhya Pradesh", "chrome": "https://mphc.gov.in/former-cj", "pages": [
        ("https://mphc.gov.in/high-court-rules", "rules", {}),
        ("https://mphc.gov.in/rules", "rules", {})]},
}
# High Courts whose sites could not be read; filled in by `list`/`load` with the reason, so the report says why.
UNREACHABLE: dict[str, str] = {
    "ph": "Punjab & Haryana (highcourtchd.gov.in): robots.txt disallows all paths for every crawler; not read",
    "hp": "Himachal Pradesh (hphighcourt.nic.in): TLS certificate does not match the host name (also with www.); not read",
    "karnataka": "Karnataka (karnatakajudiciary.kar.nic.in): connection times out from this network",
    "sikkim": "Sikkim (hcs.gov.in/hcs/Rules): the Rules page only links to other High Courts; its own page holds staff establishment rules only",
}


class Resp:
    def __init__(self, status: int, content: bytes, url: str, ctype: str) -> None:
        self.status_code, self.content, self.url = status, content, url
        self.headers = {"content-type": ctype}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


class CurlSite:
    """Like web_common.Site (one request at a time per site, a pause, robots.txt honoured, retries), through curl."""

    def __init__(self, pause: float = 2.0, timeout: int = 300) -> None:
        import os
        import tempfile
        self.pause, self.timeout = pause, timeout
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        fd, self.jar = tempfile.mkstemp(suffix=".cookies")  # a site's session cookie, as a browser keeps it
        os.close(fd)

    def _wait(self, host: str) -> None:
        gap = time.time() - self._last.get(host, 0)
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last[host] = time.time()

    def _curl(self, url: str, timeout: int | None = None, data: dict | None = None, referer: str | None = None) -> Resp:
        self._wait(urlparse(url).netloc)
        post = [x for k, v in (data or {}).items() for x in ("--data-urlencode", f"{k}={v}")]
        post += ["-e", referer] if referer else []  # a form sent from the page carries the page as referer, as a browser does
        p = subprocess.run(["curl", "-s", "-L", "--max-redirs", "8", "--connect-timeout", "40", "-m", str(timeout or self.timeout),
                            "-A", UA, "-H", "Accept-Language: en-IN,en;q=0.9", "-b", self.jar, "-c", self.jar, *post,
                            "-w", "\n@@CURL@@%{http_code} %{content_type} %{url_effective}", url], capture_output=True)
        body, sep, tail = p.stdout.rpartition(b"\n@@CURL@@")
        if not sep:
            return Resp(0, b"", url, "")
        parts = tail.decode("utf-8", "replace").split(" ")
        code = int(parts[0]) if parts[0].isdigit() else 0
        ctype = parts[1] if len(parts) > 2 else ""
        return Resp(code, body, parts[-1] or url, ctype)

    def allowed(self, url: str) -> bool:
        u = urlparse(url)
        key = f"{u.scheme}://{u.netloc}"
        if key not in self._robots:
            rp = None
            r = self._curl(f"{key}/robots.txt", timeout=60)
            body = r.text if r.status_code == 200 else ""
            if body and "<html" not in body[:500].lower() and re.search(r"(?im)^\s*(user-agent|disallow)\s*:", body):
                rp = RobotFileParser()
                rp.parse(body.splitlines())
            self._robots[key] = rp
        rp = self._robots[key]
        return True if rp is None else rp.can_fetch(UA, url) and rp.can_fetch("*", url)

    def get(self, url: str, tries: int = 3, data: dict | None = None, referer: str | None = None, **_) -> Resp:
        if not self.allowed(url):
            raise Blocked(f"robots.txt disallows {url}")
        last = None
        for attempt in range(tries):
            r = self._curl(url, data=data, referer=referer)
            if r.status_code == 403:
                raise Blocked(f"403 at {url}")
            if r.status_code in (0, 429, 500, 502, 503, 504):
                last = r.status_code
                time.sleep(min(60, 5 * 2 ** attempt))
                continue
            return r
        raise Blocked(f"unreachable (HTTP {last or 'no answer'}) {url}")


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    s = re.sub(r"\b(Download|View)\s*(\([^)]*\))?", " ", s, flags=re.I)
    s = re.sub(r"Accessible Version\s*:?", " ", s, flags=re.I)
    s = re.sub(r"\(\s*[\d.,]+\s*[KMG]B\s*\)", " ", s, flags=re.I)
    s = re.sub(r"\s[\d.,]+\s*[KMG]B\s*$", " ", s, flags=re.I)
    s = re.sub(r"\s*Uploaded\s*:.*$", " ", s, flags=re.I)
    s = re.sub(r"\s*Click here to\s*/?\s*$", " ", s, flags=re.I)
    s = re.sub(r"\b(ODT|PDF|DOCX?)\b(\s+(ODT|PDF|DOCX?)\b)*\s*$", " ", s)
    s = re.sub(r"^\s*\d{1,3}\s*[.)]?\s+(?=\D)", "", s)
    return re.sub(r"\s+", " ", s).strip(" -|:")


def _title(a, row: str, url: str) -> str:
    text = _clean(a.get_text(" ", strip=True))
    if not text or GENERIC.match(text) or DATEISH.match(text) or len(text) < 6:
        img = a.find("img")
        alt = _clean(img.get("alt", "")) if img else ""
        r = _clean(row)
        text = r if len(r) >= 6 else alt if len(alt) >= 6 else (text or r)
        if DATEISH.match(text or ""):
            text = ""
    if not text:
        text = re.sub(r"(?:_|-|%20|\s)+", " ", urlparse(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]).strip()
    return text[:400]


def _date(*texts: str) -> str | None:
    for t in texts:
        for m in re.finditer(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})\b", t or ""):
            d = iso_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
            if d and "1950" <= d[:4] <= "2027":
                return d
        m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|September|October|November|December|"
                      r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?,?\s+(\d{4})", t or "", re.I)
        if m:
            d = iso_date(f"{m.group(1)} {m.group(2)[:3].title()} {m.group(3)}")
            if d:
                return d
    return None


def _number(title: str) -> str | None:
    m = re.search(r"\bNo\.?\s*([A-Z0-9][\w./()&-]*(?:\s*of\s*\d{4})?)", title, re.I)
    return m.group(0).strip() if m else None


def doc_links(site, page: str, opt: dict | None = None) -> tuple[list[dict], list[str], list[tuple[str, str]], str]:
    """Document links on one page: url, title, row text (and the POST fields for sites that hand files out through a
    form). Also the page's '?page=N' links, its sub-pages matching opt['follow'] (url, link words), and its final URL."""
    opt = opt or {}
    r = site.get(page)
    if r.status_code != 200:
        raise Blocked(f"HTTP {r.status_code} at {page}")
    soup = BeautifulSoup(r.content, "html.parser")
    root = soup.select_one(opt["scope"]) if opt.get("scope") else soup
    root = root or soup
    final = str(r.url)
    base_tag = soup.find("base", href=True)
    base = absolute(final, base_tag["href"]) if base_tag else final  # links resolve against <base href>, as in a browser
    out: dict[str, dict] = {}
    pager: list[str] = []
    subs: list[tuple[str, str]] = []
    any_re = opt.get("any")
    for a in root.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript", "mailto", "#", "tel:")) or "${" in href:
            continue
        url = absolute(base, href)
        if re.search(r"[?&]page=\d+", href) and not DOC.search(url):
            pager.append(url)
            continue
        if not (DOC.search(url) or (any_re and re.search(any_re, url))):
            words = _clean(a.get_text(" ", strip=True))
            if opt.get("follow") and re.search(opt["follow"], f"{words} {url}", re.I) and urlparse(url).netloc == urlparse(final).netloc:
                if len(words) < 6 or GENERIC.match(words):
                    holder = a.find_parent("tr") or a.find_parent("li") or a.parent
                    words = _clean(holder.get_text(" ", strip=True)) if holder else words
                subs.append((url, words))
            continue
        holder = a.find_parent("tr") or a.find_parent("li") or a.find_parent("p") or a.parent
        row = holder.get_text(" ", strip=True) if holder else ""
        title = _title(a, row, url)
        link_words = _clean(a.get_text(" ", strip=True))
        cur = out.get(url)
        if cur is None or (len(title) > len(cur["title"]) and not cur["title_from_link"]):
            out[url] = {"url": url, "title": title, "row": re.sub(r"\s+", " ", row)[:600],
                        "title_from_link": bool(link_words) and not GENERIC.match(link_words) and not DATEISH.match(link_words)}
    if opt.get("post"):
        for f in root.find_all("form"):
            action = f.get("action") or ""
            if not re.search(opt["post"], action):
                continue
            fields = {i.get("name"): i.get("value") or "" for i in f.find_all("input") if i.get("name") and i.get("type", "").lower() in ("hidden", "")}
            if not fields:
                continue
            target = absolute(base, action)
            keyed = {k: fields[k] for k in opt.get("post_key", fields) if k in fields}
            key = target + "?" + "&".join(f"{k}={v}" for k, v in sorted(keyed.items()))
            holder = f.find_parent("tr") or f.find_parent("li") or f.parent
            row = re.sub(r"\s+", " ", holder.get_text(" ", strip=True) if holder else "")
            trigger = None
            for name in filter(None, (f.get("id"), f.get("name"))):
                trigger = root.find("a", onclick=re.compile(re.escape(name) + r"\.submit"))
                if trigger:
                    break
            title = _clean(trigger.get_text(" ", strip=True)) if trigger else ""
            if len(title) < 6 and holder is not None and holder.name == "tr":
                cells = [_clean(td.get_text(" ", strip=True)) for td in holder.find_all("td")]
                cells = [x for x in cells if not DATEISH.match(x) and not GENERIC.match(x or "x") and not re.match(r"^[\d.]+$", x)]
                title = max(cells, key=len) if cells else ""
            out[key] = {"url": key, "post": (target, fields), "title": (title or row)[:400], "row": row[:600], "title_from_link": True}
    return list(out.values()), pager, subs, final


def listing(code: str, site) -> tuple[list[dict], list[str]]:
    """Every document a court's configured pages list (chrome links — the site's menu — removed)."""
    c = COURTS[code]
    notes: list[str] = []
    chrome: set[str] = set()
    court_ex = re.compile(c["exclude"], re.I) if c.get("exclude") else None
    if c.get("chrome"):
        try:
            chrome = {d["url"] for d in doc_links(site, c["chrome"])[0]}
        except Blocked as e:
            notes.append(f"chrome page: {e}")
    docs: dict[str, dict] = {}

    def take(d: dict, page: str, category: str, opt: dict, parent: str = "") -> None:
        if d["url"] in chrome:
            return
        title = d["title"]
        if parent and (not d["title_from_link"] or len(title) < 12):
            title = parent
        elif parent and parent.lower() not in title.lower():
            title = f"{parent}: {title}" if len(parent) < 120 else title
        if opt.get("only") and not re.search(opt["only"], d["url"]):
            return
        if opt.get("titled") and not re.search(opt["titled"], title, re.I):
            return
        path = urlparse(d["url"]).path
        if EXCLUDE.search(title) or EXCLUDE.search(path) or (court_ex and (court_ex.search(title) or court_ex.search(path))):
            return
        if opt.get("forms_re") and re.search(opt["forms_re"], d["url"]):
            category = "forms"
        if opt.get("name_file"):  # titles like 'Chapter 1' repeat across rule books: add the file's own name
            bare = re.sub(r"[(]\s*\.pdf\s*[)]", "", title).strip()
            title = f"{bare} ({path.rsplit('/', 1)[-1]})"
        if not d.get("post"):
            u = re.sub(r"(?<!:)//+", "/", d["url"])
            if u.startswith("http://") and urlparse(page).scheme == "https" and urlparse(u).netloc == urlparse(page).netloc:
                u = "https://" + u[len("http://"):]  # the court's own http:// links time out; the site serves https
            if opt.get("rewrite"):  # a viewer page whose file sits at a sibling address (Patna: viewpdf -> getpdffile)
                u = u.replace(*opt["rewrite"])
            d = {**d, "url": u}  # 'site.in//uploads/x.pdf' and 'site.in/uploads/x.pdf' are one file
        key = f"{page}#{title}" if opt.get("key") == "title" else d["url"]
        if key in docs:
            return
        docs[key] = {"key": key, "download": d["url"], "post": d.get("post"), "title": title, "row": d["row"], "category": category,
                     "page": page, "date": _date(title, d["row"]), "number": _number(title)}

    for page, category, opt in c["pages"]:
        todo, seen = [(page, "")], set()
        max_pages = opt.get("pages", 1)
        while todo:
            p, parent = todo.pop(0)
            if p in seen:
                continue
            seen.add(p)
            try:
                sub_opt = {k: v for k, v in opt.items() if k not in ("follow", "scope", "sub_scope")}
                if opt.get("sub_scope"):
                    sub_opt["scope"] = opt["sub_scope"]
                found, pager, subs, final = doc_links(site, p, opt if not parent else sub_opt)
            except Blocked as e:
                notes.append(str(e))
                continue
            if max_pages > 1:
                for q in pager:
                    m = re.search(r"[?&]page=(\d+)", q)
                    if m and int(m.group(1)) < max_pages and urlparse(q).path == urlparse(final).path and q not in seen:
                        todo.append((q, ""))
            for sub, words in subs:
                if sub not in seen and not EXCLUDE.search(words) and not (court_ex and court_ex.search(words)):
                    todo.append((sub, words))
            for d in found:
                take(d, page, category, opt, parent)
    for url, title, category in c.get("direct", []):
        take({"url": url, "title": title, "row": "", "title_from_link": True}, url, category, {})
    return list(docs.values()), notes


def fetch_post(site, url: str, fields: dict, referer: str | None = None) -> tuple[bytes, str]:
    """A file the site hands out through a form (POST), as Madras and Kerala do."""
    from .web_common import ext_of
    r = site.get(url, data=fields, referer=referer)
    if r.status_code != 200:
        raise Blocked(f"HTTP {r.status_code} at {url}")
    return r.content, ext_of(str(r.url) + "?", r.headers.get("content-type", ""), r.content)


def refresh_posts(code: str, site, docs: list[dict]) -> None:
    """Form downloads carry a session token that each new load of the page replaces (Kerala). After listing, read each
    such page once more and give every document the token of that latest load."""
    pages = {d["page"] for d in docs if d.get("post")}
    for page in pages:
        opts = [o for p, _, o in COURTS[code]["pages"] if p == page and o.get("post")]
        if not opts:
            continue
        fresh = {x["url"]: x["post"] for x in doc_links(site, page, {k: v for k, v in opts[0].items() if k in ("post", "post_key")})[0] if x.get("post")}
        for d in docs:
            if d.get("post") and d["key"] in fresh:
                d["post"] = fresh[d["key"]]


def _site(code: str):
    return Site(pause=2.0, timeout=300) if COURTS[code].get("fetch") == "httpx" else CurlSite(pause=2.0)


def load(code: str, dry: bool = False) -> dict:
    c = COURTS[code]
    site = _site(code)
    docs, notes = listing(code, site)
    print(f"  {code}: {len(docs)} documents listed{'; notes: ' + '; '.join(notes) if notes else ''}", flush=True)
    if dry:
        for d in docs:
            print(f"    [{d['category']}] {d['date'] or '          '} {d['title'][:110]} | {d['download'][-60:]}")
        return {"listed": len(docs), "notes": notes}
    lib = Library(11, f"{c['name']} (official website)")
    b2_used = sum(lib.known("court-rules/").values())
    docs.sort(key=lambda d: {"rules": 0, "forms": 1, "practice": 2}[d["category"]])  # rule books take Backblaze room first
    since = 0
    groups = {"rules": Group(lib, "b2", f"court-rules/{code}/text.jsonl.gz", "court_rules_text", f"{c['name']}: rules and practice directions (text)", court=code)}
    if any(d["category"] == "forms" for d in docs):
        flib = Library(10, lib.source)
        groups["forms"] = Group(flib, "b2", f"formats/hc/{code}/text.jsonl.gz", "format_text", f"{c['name']}: downloadable forms (text)", court=code)
    res = {"listed": len(docs), "new": 0, "already": 0, "failed": 0, "fails": [], "notes": notes}
    refresh_posts(code, site, docs)
    for d in docs:
        g = groups["forms" if d["category"] == "forms" else "rules"]
        if g.has(d["key"]):
            res["already"] += 1
            continue
        try:
            data, ext = fetch_post(site, *d["post"], referer=d["page"]) if d.get("post") else fetch_doc(site, d["download"])[:2]
        except Blocked as e:
            res["failed"] += 1
            res["fails"].append({"url": d["key"], "title": d["title"], "why": str(e)[:300]})
            print(f"    failed: {d['title'][:60]}: {e}", flush=True)
            continue
        if ext in (".html", ".bin"):
            res["failed"] += 1
            res["fails"].append({"url": d["key"], "title": d["title"], "why": f"not a document (got {ext})"})
            print(f"    failed: {d['title'][:60]}: not a document (got {ext})", flush=True)
            continue
        name = file_name(d["title"], d["key"], ext)
        since += 1
        if since >= 15:  # other courts load at the same time: re-read what Backblaze court-rules/ holds now
            b2_used, since = sum(lib.known("court-rules/").values()), 0
        cap = B2_CAP if d["category"] == "rules" else PRACTICE_CAP
        if d["category"] == "forms":
            store, path, kind, item = "b2", f"formats/hc/{code}/{name}", "format", 10
        elif len(data) > BIG or b2_used + len(data) > cap:
            store, path, kind, item = "drive", f"library/court-rules/{code}/{name}", "court_rules", 11
        else:
            store, path, kind, item = "b2", f"court-rules/{code}/{name}", "court_rules", 11
            b2_used += len(data)
        meta = {"title": d["title"], "number": d["number"], "date": d["date"], "body": c["name"], "category": d["category"],
                "court": code, "listed_on": d["page"], "listing_text": d["row"]}
        row = doc_row(meta, data, ext, d["key"], path, store)
        if d["key"] != d["download"]:
            row["download_url"] = d["download"]
        try:
            (g.lib).put(store, path, data, kind, title=d["title"][:300], court=code, year=int(d["date"][:4]) if d["date"] else None)
        except Exception as e:  # noqa: BLE001 - one store hiccup must not end the court; retried next run
            res["failed"] += 1
            res["fails"].append({"url": d["key"], "title": d["title"], "why": f"store: {e}"[:300]})
            continue
        g.add(row)
        res["new"] += 1
        if g.dirty >= 20:
            g.save()
    for g in groups.values():
        g.save()
    res["stored_rows"] = sum(len(g.rows) for g in groups.values())
    res["scan_only"] = sum(1 for g in groups.values() for r in g.rows.values() if r.get("scan_only"))
    listed = [{"url": d["key"], "download": d["download"], "title": d["title"], "date": d["date"], "category": d["category"], "page": d["page"]} for d in docs]
    index_file(lib, "b2", f"court-rules/{code}/index.jsonl.gz", listed, res, f"{c['name']}: what its pages list", "court_rules_index")
    print(f"  {code}: {res['new']} new, {res['already']} already, {res['failed']} failed, {res['scan_only']} scan-only; rows {res['stored_rows']}", flush=True)
    return res


def status() -> None:
    lib = Library(11, "")
    b2 = lib.known("court-rules/")
    fm = lib.known("formats/hc/")
    dr = lib.known("library/court-rules/")
    print(f"  Backblaze court-rules/ {sum(b2.values()) / 1e6:.1f} MB in {len(b2)} files; formats/hc/ {sum(fm.values()) / 1e6:.1f} MB in {len(fm)} files;"
          f" Drive library/court-rules/ {sum(dr.values()) / 1e6:.1f} MB in {len(dr)} files")
    for code in COURTS:
        idx = lib.get("b2", f"court-rules/{code}/index.jsonl.gz") if f"court-rules/{code}/index.jsonl.gz" in b2 else None
        if not idx:
            print(f"  {code}: not loaded")
            continue
        rows = unpack(idx)
        head, docs = rows[0], rows[1:]
        failed = [d for d in docs if d["status"] != "ok"]
        files = [p for p in list(b2) + list(fm) + list(dr) if f"/{code}/" in p and not p.endswith(".jsonl.gz")]
        print(f"  {code}: listed {len(docs)}, files {len(files)}, scan-only {head['result'].get('scan_only')}, failed {len(failed)}")
        for d in failed[:10]:
            print(f"     {d['title'][:70]}: {d['status'][:150]}")
    for code, why in UNREACHABLE.items():
        print(f"  {code}: not read: {why}")


def rebalance(limit: int = B2_CAP, skip: tuple = ()) -> None:
    """Keep Backblaze court-rules/ under the limit: move the largest originals (practice directions first, then
    rules) to Drive library/court-rules/<court>/, point their text rows there, and drop the Backblaze copies."""
    import httpx
    lib = Library(11, "")
    known = lib.known("court-rules/")
    drive = lib.known("library/court-rules/")
    b2 = {p: n for p, n in known.items() if not p.endswith(".jsonl.gz")}
    total = sum(known.values())
    rows_by_file: dict[str, tuple[str, dict]] = {}
    groups: dict[str, Group] = {}
    for p in [x for x in known if x.endswith("/text.jsonl.gz") and x.split("/")[1] not in skip]:
        g = groups[p] = Group(lib, "b2", p, "court_rules_text", "")
        g.title = f"{COURTS.get(p.split('/')[1], {}).get('name', p.split('/')[1])}: rules and practice directions (text)"
        g.meta = {"court": p.split("/")[1]}
        for r in g.rows.values():
            rows_by_file[r["file"]] = (p, r)
            # A move cut short last time: the Backblaze copy is gone but the Drive copy is there.
            dest = f"library/court-rules/{p.split('/')[1]}/{r['file'].rsplit('/', 1)[-1]}"
            if r["store"] == "b2" and r["file"] not in known and dest in drive:
                r["file"], r["store"] = dest, "drive"
                g.dirty += 1
                print(f"    repaired pointer to {dest}", flush=True)
    order = sorted((p for p in b2 if p in rows_by_file), key=lambda p: (rows_by_file[p][1].get("category") == "rules", -b2[p]))
    try:
        for p in order:
            if total <= limit:
                break
            gp, row = rows_by_file[p]
            try:
                data = lib.get("b2", p)
            except (httpx.HTTPError, RuntimeError):
                continue
            if data is None:
                continue
            code = p.split("/")[1]
            dest = f"library/court-rules/{code}/{p.rsplit('/', 1)[-1]}"
            lib.put("drive", dest, data, "court_rules", title=row["title"][:300], court=code, year=int(row["date"][:4]) if row.get("date") else None,
                    source=f"{COURTS.get(code, {}).get('name', code)} (official website)")
            row["file"], row["store"] = dest, "drive"
            groups[gp].dirty += 1
            lib.b2.delete(p)
            lib.db.http.delete(f"/library_files?path=eq.{p}")
            total -= b2[p]
            print(f"    moved {p} ({b2[p] / 1e6:.1f} MB) to Drive", flush=True)
    finally:
        for g in groups.values():
            if g.dirty:
                g.save()
    print(f"  Backblaze court-rules/ now {sum(lib.known('court-rules/').values()) / 1e6:.1f} MB", flush=True)


def tidy() -> None:
    """Drop stored originals no text row points to (left behind when a court renamed a document between runs)."""
    lib = Library(11, "")
    texts = [p for p in list(lib.known("court-rules/")) + list(lib.known("formats/hc/")) if p.endswith("/text.jsonl.gz")]
    used = {r["file"] for p in texts for r in unpack(lib.get("b2", p) or b"")}
    for prefix, store in (("court-rules/", "b2"), ("formats/hc/", "b2"), ("library/court-rules/", "drive")):
        for p in lib.known(prefix):
            if p.endswith(".jsonl.gz") or p in used:
                continue
            lib.store(store).delete(p)
            lib.db.http.delete(f"/library_files?path=eq.{p}")
            print(f"    removed unreferenced {store}:{p}", flush=True)


def verify(n: int = 3, seed: int | None = None) -> None:
    """n random stored documents per item (11: rules, 10: forms): read back from storage, checked byte for byte
    against the live file, and shown with their title, date and first lines."""
    import hashlib
    import random
    rnd = random.Random(seed)
    lib = Library(11, "")
    texts = [p for p in list(lib.known("court-rules/")) + list(lib.known("formats/hc/")) if p.endswith("/text.jsonl.gz")]
    pools: dict[str, list] = {"rules": [], "forms": []}
    for p in texts:
        code = p.split("/")[-2]
        for r in unpack(lib.get("b2", p) or b""):
            pools["forms" if p.startswith("formats/") else "rules"].append((code, r))
    for kind, pool in pools.items():
        print(f"== {kind}: {len(pool)} stored documents; checking {min(n, len(pool))}")
        for code, r in rnd.sample(pool, min(n, len(pool))):
            stored = lib.get(r["store"], r["file"])
            ok_store = stored is not None and hashlib.sha1(stored).hexdigest() == r["sha1"]
            site = _site(code) if code in COURTS else CurlSite()
            try:
                if r["source_url"].split("?")[0].endswith(("view_pdf.php", "download_file.php")):
                    d = next(x for x in listing(code, site)[0] if x["key"] == r["source_url"])
                    refresh_posts(code, site, [d])
                    live = fetch_post(site, *d["post"], referer=d["page"])[0]
                else:
                    live = fetch_doc(site, r.get("download_url") or r["source_url"])[0]
                same = hashlib.sha1(live).hexdigest() == r["sha1"]
                live_note = "live file identical" if same else f"live file differs ({len(live)} vs {r['bytes']} bytes)"
            except (Blocked, StopIteration) as e:
                live_note = f"live file not re-read: {e}"
            first = " / ".join(x.strip() for x in (r.get("text") or "").splitlines() if x.strip())[:200]
            print(f"  [{code}] {r['title'][:90]} | date {r.get('date')} | {r['store']}:{r['file']}")
            print(f"     stored copy {'matches its sha1' if ok_store else 'MISSING OR CHANGED'}; {live_note}; scan-only {r.get('scan_only')}")
            print(f"     first lines: {first or '(no text: scan-only)'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["list", "load", "status", "verify", "rebalance", "probe", "nav"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--httpx", action="store_true", help="probe/nav: read with Python's own TLS instead of curl")
    a = ap.parse_intermixed_args()
    if a.httpx:
        a.args.append("--httpx")
    if a.cmd in ("probe", "nav"):
        site = Site(pause=1.5) if "--httpx" in a.args else CurlSite(pause=1.5)
        for page in [x for x in a.args if x != "--httpx"]:
            try:
                if a.cmd == "probe":
                    found = doc_links(site, page)[0]
                    print(f"== {page}: {len(found)} document links")
                    for d in found:
                        print(f"   {d['title'][:90]!r} | {d['url']}")
                else:
                    soup = BeautifulSoup(site.get(page).content, "lxml")
                    print(f"== {page}")
                    for x in soup.find_all("a", href=True):
                        t = x.get_text(" ", strip=True)
                        if re.search(r"rule|practice|direction|form|manual|procedure|\bsop|standing order|e-?filing|handbook|notification", t + x["href"], re.I):
                            print(f"   {t[:60]!r} {absolute(page, x['href'])}")
            except Blocked as e:
                print(f"== {page}: {e}")
        return 0
    if a.cmd == "status":
        status()
        return 0
    if a.cmd == "verify":
        verify(int(a.args[0]) if a.args else 3)
        return 0
    if a.cmd == "rebalance":
        tidy() if not a.args else None  # tidy only when no court is still loading
        rebalance(skip=tuple(a.args))  # args: courts still loading, left alone
        return 0
    codes = a.args or list(COURTS)
    for code in codes:
        try:
            load(code, dry=a.cmd == "list")
        except Blocked as e:
            print(f"  {code}: {e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
