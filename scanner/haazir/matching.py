"""Decide whether a party name is one of our companies, and which side the company is on.

Court records have no company ID, so this is name matching. Names arrive as
"UNITED INDIA INSURANCE CO.LTD.", "United India Ins. Co. Ltd. Jaipur", "M/s Bajaj Finance Ltd.",
"S.B.I. through Branch Manager" and with typos. We normalise, then accept a whole-word phrase hit,
the phrase's words in order with a small gap, or a close fuzzy hit for long phrases.
"""

import re

from rapidfuzz import fuzz

_ABBREV = [
    (r"\bins\b", "insurance"), (r"\bco\b", "company"), (r"\bcomp\b", "company"),
    (r"\bltd\b", "limited"), (r"\bpvt\b", "private"),
]


# Words that say nothing about which company a party is.
_GENERIC = {"the", "company", "limited", "private", "and", "ors", "others", "other", "through", "its", "branch", "manager"}


def _join_initials(s: str) -> str:
    """'s b i' -> 'sbi', 'u i i c' -> 'uiic': dotted abbreviations arrive as single letters once
    punctuation is stripped. Runs of two or more single letters become one word."""
    out, run = [], []
    for w in s.split() + [""]:
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        if run:
            out.append("".join(run))
            run = []
        if w:
            out.append(w)
    return " ".join(out)


def normalise(name: str) -> str:
    s = name.lower().replace("&", " and ")
    s = re.sub(r"\bm\s*/\s*s\b\.?", " ", s)  # "M/s" before initials are joined, or "M/s S.B.I." becomes "mssbi"
    s = re.sub(r"[^a-z0-9ऀ-ॿ]+", " ", s)
    s = _join_initials(s)
    for pat, rep in _ABBREV:
        s = re.sub(pat, rep, s)
    return re.sub(r"\s+", " ", s).strip()


def _same(word: str, target: str) -> bool:
    """One word against one word: exact, or a small typo in a longer word ("finanance" ~ "finance",
    but not "finserv")."""
    return word == target or (len(target) >= 5 and fuzz.ratio(word, target) >= 80)


# Words that may sit inside a company's name without making it a different company:
# "United India General Insurance" is United India Insurance; "Bajaj Housing Finance" is not Bajaj Finance.
_FILLER = {"general", "the", "of", "and", "company", "limited", "co", "india", "insurance"}


def _in_order(words: list[str], name_words: list[str], max_gap: int = 2) -> bool:
    """True if `words` appear in `name_words` in order, with at most `max_gap` filler words between
    consecutive ones. "united india insurance" matches "united india general insurance company"."""
    for start, w in enumerate(name_words):
        if not _same(w, words[0]):
            continue
        pos, ok = start, True
        for target in words[1:]:
            window = name_words[pos + 1: pos + 2 + max_gap]
            hit = next((i for i, x in enumerate(window) if _same(x, target)), None)
            if hit is None or any(x not in _FILLER for x in window[:hit]):
                ok = False
                break
            pos = pos + 1 + hit
        if ok:
            return True
    return False


def is_company(name: str, confirm: list[str]) -> bool:
    """Whole words only: "sbi" matches "SBI Jaipur" and "S.B.I." but not "Jasbir"."""
    n = normalise(name)
    if not n:
        return False
    padded = f" {n} "
    for phrase in confirm:
        p = normalise(phrase)
        if not p:
            continue
        if f" {p} " in padded:
            return True
        # Words in order, each allowed a small typo: "unitd india insurance", "bajaj finanance".
        if len(p.split()) >= 2 and _in_order(p.split(), n.split()):
            return True
        # Words run together: "BAJAJFINANCE LTD".
        if len(p.split()) >= 2 and p.replace(" ", "") in n.replace(" ", ""):
            return True
        # The name cut short: a MACT respondent written as just "United India" (two words at least, and
        # nothing else, so "United India Traders" stays out).
        # Cut mid-word too, as portals truncate long names: "UNITED INDIA IN".
        core = [w for w in n.split() if w not in _GENERIC]
        pw = p.split()
        if 2 <= len(core) <= len(pw) and pw[: len(core) - 1] == core[:-1] and pw[len(core) - 1].startswith(core[-1]):
            return True
    return False


def title_of(petitioner: str | None, respondent: str | None) -> str | None:
    """ "A vs B", or the one name the court listed. """
    p, r = (petitioner or "").strip(), (respondent or "").strip()
    return f"{p} vs {r}" if p and r else (p or r or None)


def role(petitioners: list[str], respondents: list[str], confirm: list[str]) -> str:
    on_pet = any(is_company(p, confirm) for p in petitioners)
    on_resp = any(is_company(r, confirm) for r in respondents)
    if on_resp and not on_pet:
        return "respondent"
    if on_pet and not on_resp:
        return "petitioner"
    if on_pet and on_resp:
        # Insurer named on both sides happens in cross-claims; being sued is the case we must not miss.
        return "respondent"
    return "unknown"
