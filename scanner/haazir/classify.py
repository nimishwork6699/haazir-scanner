"""Map eCourts case types and acts to the categories we route on."""

CATEGORIES = {
    "motor_accident": "Motor accident claim",
    "cheque_bounce": "Cheque bounce (NI Act 138)",
    "consumer": "Consumer complaint",
    "civil": "Civil suit",
    "execution": "Execution",
    "arbitration": "Arbitration",
    "criminal": "Criminal",
    "other": "Other",
}


def categorize(case_type: str | None, acts_text: str = "") -> str:
    t = (case_type or "").lower()
    a = acts_text.lower()
    if "mact" in t or "motor vehicle" in a or "m.v. act" in a:
        return "motor_accident"
    if "negotiable instrument" in a or "n.i. act" in a or t.startswith("nia") or "n.i.a" in t:
        return "cheque_bounce"
    if "consumer" in t or "consumer protection" in a:
        return "consumer"
    if "execution" in t:
        return "execution"
    if "arbitration" in t or "arbitration" in a:
        return "arbitration"
    if "civil" in t or "code of civil procedure" in a:
        return "civil"
    if t.startswith("cr") or "complaint" in t or "criminal" in t or "nagarik" in a or "nyaya sanhita" in a \
            or "penal code" in a or "cr.p.c" in a:
        return "criminal"
    return "other"
