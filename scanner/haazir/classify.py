"""Map eCourts case types and acts to the categories we route on."""
import re

CATEGORIES = {
    "motor_accident": "Motor accident claim",
    "cheque_bounce": "Cheque bounce (NI Act 138)",
    "consumer": "Consumer complaint",
    "civil": "Civil suit",
    "execution": "Execution",
    "arbitration": "Arbitration",
    "writ": "Writ petition",
    "appeal": "Appeal",
    "contempt": "Contempt",
    "criminal": "Criminal",
    "other": "Other",
}

# Court abbreviations, matched at the start of the case type ("CW 21637/2026", "MAC.APP. 12/2025", "CS (COMM)").
WRIT = re.compile(r"^(c\.?\s?w\.?|w\.?\s?p\.?|cwp|s\.?a\.?w\.?|crl\.?\s?w\.?p?)(?![a-z])|\bwrit\b")
APPEAL = re.compile(r"appeal|^(mac\.?\s?app|r\.?f\.?a|c\.?f\.?a|f\.?a|l\.?p\.?a|s\.?a|c\.?m\.?a|cmap|crl\.?\s?a)(?![a-z])")
CONTEMPT = re.compile(r"contempt|^(c\.?c\.?p|cont\.?\s?cas|cont\.?\s?app)(?![a-z])")
SUIT = re.compile(r"^c\.?s(?![a-z])|\bsuit\b")


def categorize(case_type: str | None, acts_text: str = "") -> str:
    t = (case_type or "").lower().strip()
    a = acts_text.lower()
    if "mact" in t or "motor vehicle" in a or "m.v. act" in a:
        return "motor_accident"
    if CONTEMPT.search(t):
        return "contempt"
    if "negotiable instrument" in a or "n.i. act" in a or t.startswith("nia") or "n.i.a" in t or "ni act" in t:
        return "cheque_bounce"
    if "consumer" in t or "consumer protection" in a:
        return "consumer"
    if "execution" in t:
        return "execution"
    if "arbitration" in t or "arbitration" in a or t.startswith("arb"):
        return "arbitration"
    if WRIT.search(t):
        return "writ"
    if APPEAL.search(t):
        return "appeal"
    if "civil" in t or SUIT.search(t) or "code of civil procedure" in a:
        return "civil"
    if t.startswith("cr") or "complaint" in t or "criminal" in t or "nagarik" in a or "nyaya sanhita" in a \
            or "penal code" in a or "cr.p.c" in a:
        return "criminal"
    return "other"
