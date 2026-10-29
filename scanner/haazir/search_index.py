"""Library search index (Phase 6; reports/Library search design.md): every stored judgment searchable from static files
read with HTTP range reads (no search server).

    python -m haazir.search_index plan [--build B] [--root search/v1] [--sample]   units to build -> plan.json (Drive)
    python -m haazir.search_index docs <unit> --build B        one unit: doc store + its term lists for every part
    python -m haazir.search_index postings <part> --build B    one part (of 16): merged postings, dictionaries, exact table
    python -m haazir.search_index manifest --build B           join up; the manifest pointer swapped last (atomic)
    python -m haazir.search_index status [--build B]           what is built so far (from library_files, item 34)
    python -m haazir.search_index query "<q>" [--level hc] [--court 9_13] [--state X] [--from Y] [--to Y] [--page N]
    python -m haazir.search_index cnr <CNR>                    exact lookup
    python -m haazir.search_index bench [--manifest NAME]      the 20 test queries, cold and warm, timed
    python -m haazir.search_index sample                       the local proof: plan + every unit + every part + manifest
    python -m haazir.search_index sample-docs                  the docs level alone (Library's Circulars & Formats tabs)
    (plan --with-docs adds the docs level to a full build)
    python -m haazir.search_index mirror [MB]                  copy Backblaze sources to Drive, at most MB of downloads
                                                               (default 700), so rebuilds stay under Backblaze's daily cap

How it is built (a map/reduce, so each source file is read once; see the report for why the design's 16 full passes
were changed):
  docs <unit>     a unit is a run of source files in doc order. It writes the unit's doc store (records in blocks of
                  128, zlib) and, for each of the 16 parts, the unit's sorted term -> local doc ids. Doc ids are only
                  local here; the manifest gives each unit its base, so nothing depends on row counts being exact.
  postings <p>    reads part p of every unit, merges them in unit order (global ids ascending = priority order) and
                  writes the postings (delta + varint, blocks of 4,096 ids, each block self-contained), each bucket's
                  dictionary, and the exact-lookup table (CNR, case numbers, citations) for its buckets.
  manifest        bases, id ranges per level and year, bucket directories, doc-store block lengths, file ids: one gzip
                  JSON written to the build folder and then to <root>/manifest.json.gz on Backblaze. That last write is
                  the swap: readers see the old build until it lands. Builds older than the previous one are removed.

Doc order (= doc id): level (sc, hc, tribunal, district), then year newest first, then source path, then row order.
Files: <root>/<build>/{plan.json, docs/uNNNN.bin, map/uNNNN.bin, meta/*.json, post/pNN.bin, manifest.json.gz}
Every file is listed in library_files (item 34, "Search index").
"""

from __future__ import annotations

import argparse
import base64
import bisect
import gzip
import heapq
import io
import json
import os
import re
import struct
import sys
import time
import unicodedata
import zlib
from array import array
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ITEM = 34
LEVELS = ["sc", "hc", "tribunal", "district", "docs"]  # docs: circulars, notifications, bills, … (the Library's other tabs)
NB = 4096          # term buckets
NIB = 8192         # exact-lookup buckets
PARTS = 16
BLOCK = 4096       # doc ids per postings block
DOCBLOCK = 128     # records per doc-store block
INLINE = 16        # postings this short are written into the dictionary line itself
UNIT_ROWS = 600_000
SNIPPET = 300
SOURCE = "Haazir search index, built from library items 12, 13, 14 and 33"

# ================================================================ terms (mirrored in web/src/lib/search/terms.ts)

_MARKS = re.compile("[̀-ͯ​-‍﻿]")
# Latin letters and digits, and the Indian scripts (Devanagari to Malayalam/Sinhala) and Urdu, with their vowel signs.
_WORD = re.compile("[0-9a-zऀ-ॣ०-෿ؠ-ٟ٠-٩ٮ-ۓە-ۿ]+")
_CID = re.compile("[^0-9a-z_ऀ-෿]+")


_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def fold(s: str | None) -> str:
    """Lower case, accents folded (NFKD, combining marks dropped), zero-width joiners removed, Devanagari digits as
    0-9."""
    return _MARKS.sub("", unicodedata.normalize("NFKD", (s or "").lower())).translate(_DIGITS)


STOP = {fold(w) for w in """
the of and a an in to for on by or is be as at with from that this it its are was were has have been which into
v vs versus ors anr others other another through ms ltd pvt smt smti shri sri kumari hon ble honble co no dated dt
का की के और में से है हैं को पर बनाम एवं तथा द्वारा या यह वह था थे ने श्री श्रीमती
""".split()}


def stem(w: str) -> str:
    """Plural 's' dropped from Latin words longer than 4 letters ('insurers' -> 'insurer'), never 'ss'."""
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss") and w.isascii() and w.isalpha():
        return w[:-1]
    return w


def words(text: str | None) -> list[str]:
    """Plain search words of a text: folded, split on anything not a letter or digit, stop words and 1-letter words
    dropped, light plural stemming."""
    out = []
    for w in _WORD.findall(fold(text)):
        if len(w) > 1 and len(w) <= 64 and w not in STOP:
            out.append(stem(w))
    return out


def cid(s: str | None) -> str:
    """A court, forum, district or state as one id: '9_13', 'kamrupmetro', 'uttarpradesh'."""
    return _CID.sub("", fold(s))


# Acts by their common names and abbreviations (on folded text). Order matters: the first pattern that matches a span
# takes it (BNSS before BNS, Hindu Succession before 'succession act').
ACTS: list[tuple[str, str]] = [
    ("ipc", r"\bindian penal code\b|\bpenal code\b|\bi\.?\s?p\.?\s?c\b\.?"),
    ("crpc", r"\bcode of criminal procedure\b|\bcriminal procedure code\b|\bcr\.?\s?p\.?\s?c\b\.?"),
    ("cpc", r"\bcode of civil procedure\b|\bcivil procedure code\b|\bc\.\s?p\.\s?c\b\.?|\bcpc\b"),
    ("ni", r"\bnegotiable instruments?(?: act)?\b|\bn\.?\s?i\.?\s?act\b"),
    ("mv", r"\bmotor vehicles?\s+act\b|\bm\.?\s?v\.?\s?act\b"),
    ("dp", r"\bdowry prohibition\b"),
    ("pocso", r"\bprotection of children from sexual offences?\b|\bpocso\b"),
    ("ndps", r"\bnarcotic drugs and psychotropic\b|\bn\.?\s?d\.?\s?p\.?\s?s\b\.?"),
    ("dv", r"\bprotection of women from domestic violence\b|\bdomestic violence act\b|\bd\.?\s?v\.?\s?act\b"),
    ("hma", r"\bhindu marriage act\b|\bh\.?\s?m\.?\s?act\b"),
    ("hsa", r"\bhindu succession act\b"),
    ("scst", r"\bscheduled castes? and (?:the )?scheduled tribes? \(?prevention of atrocities\)?|\bsc\s?/\s?st (?:\(?poa\)? )?act\b|\batrocities act\b"),
    ("arms", r"\barms act\b"),
    ("excise", r"\bexcise act\b"),
    ("evidence", r"\bindian evidence act\b|\bevidence act\b"),
    ("tpa", r"\btransfer of property act\b|\bt\.?\s?p\.?\s?act\b"),
    ("sra", r"\bspecific relief act\b"),
    ("arbitration", r"\barbitration and conciliation act\b|\barbitration act\b"),
    ("consumer", r"\bconsumer protection act\b"),
    ("pca", r"\bprevention of corruption act\b|\bp\.?\s?c\.?\s?act\b"),
    ("bnss", r"\bbharatiya nagarik suraksha sanhita\b|\bbnss\b"),
    ("bns", r"\bbharatiya nyaya sanhita\b|\bbns\b"),
    ("bsa", r"\bbharatiya sakshya adhiniyam\b"),
    ("incometax", r"\bincome[\s-]?tax act\b"),
    ("gst", r"\bgoods and services tax\b|\b[cs]?gst act\b"),
    ("companies", r"\bcompanies act\b"),
    ("ibc", r"\binsolvency and bankruptcy code\b|\bibc\b"),
    ("idact", r"\bindustrial disputes act\b|\bi\.?\s?d\.?\s?act\b"),
    ("landacq", r"\bland acquisition act\b|\bl\.?\s?a\.?\s?act\b"),
    ("succession", r"\bindian succession act\b|\bsuccession act\b"),
    ("guardians", r"\bguardians and wards act\b"),
    ("electricity", r"\belectricity act\b"),
    ("ec", r"\bemployees'? compensation act\b|\bworkmen'?s compensation act\b"),
    ("pmla", r"\bprevention of money[\s-]?laundering act\b|\bpmla\b"),
    ("itact", r"\binformation technology act\b"),
    ("jj", r"\bjuvenile justice\b"),
    ("ecact", r"\bessential commodities act\b"),
    ("fema", r"\bforeign exchange management act\b|\bfema\b"),
    ("sarfaesi", r"\bsarfaesi\b|\bsecuriti[sz]ation and reconstruction\b"),
    ("rbi", r"\breserve bank of india act\b|\brbi act\b"),
    ("limitation", r"\blimitation act\b"),
    ("registration", r"\bregistration act\b"),
    ("stamp", r"\bstamp act\b"),
    ("gaming", r"\bgaming act\b|\bgambling act\b|\bpublic gambling\b"),
    ("rent", r"\brent control act\b|\brent act\b"),
]
_ACTS = [(k, re.compile(p)) for k, p in ACTS]
_ANYACT = re.compile("|".join(f"(?:{p})" for _, p in ACTS))  # one pass to see whether any act is named at all

# 'u/s 438', 'section 138', 'sections 302/34', 's. 26', 'धारा 498-ए' -> the numbers. A letter suffix counts only when
# joined to the number ('498a', '498-a'), so 'u/s 302 r/w 34' is 302 and 34.
_SEC = re.compile(r"(?:\bu\s?/\s?s(?:ec)?\b\.?|\bsec(?:tion|tions|s)?\b\.?|\bs\.(?=\s?[0-9])|धारा(?:ओं)?)\s?[-:]?\s?"
                  r"((?:[0-9]{1,4}(?:-?[a-z]\b)?(?:\s?\([0-9a-z]{1,4}\))*(?:\s?(?:,|/|&|\band\b|\br\s?/\s?w\b|\bread with\b)\s?)?)+)")
_SECNUM = re.compile(r"([0-9]{1,4})(?:-?([a-z])\b)?")


def _sec_terms(group: str) -> list[str]:
    out = []
    for n, letter in _SECNUM.findall(re.sub(r"\([0-9a-z]{1,4}\)", " ", group)):
        n = str(int(n))
        out.append(f"sec:{n}")
        if letter:
            out.append(f"sec:{n}{letter}")
    return out


def act_terms(folded: str) -> tuple[list[str], str]:
    """act:<short> for every act named in folded text, and the text with those names blanked out."""
    found = []
    if not _ANYACT.search(folded):
        return found, folded
    for k, rx in _ACTS:
        if rx.search(folded):
            found.append(f"act:{k}")
            folded = rx.sub(" ", folded)
    return found, folded


def sec_terms(folded: str) -> tuple[list[str], str]:
    found = []
    for m in _SEC.finditer(folded):
        found += _sec_terms(m.group(1))
    return found, _SEC.sub(" ", folded)


def text_terms(text: str | None) -> list[str]:
    """Index-side: the words of a text, plus sec:/act: for sections and acts it names (the words stay too)."""
    if not text:
        return []
    f = fold(text)
    acts, _ = act_terms(f)
    secs, _ = sec_terms(f)
    return words(f) + acts + secs


# ---- case numbers, CNRs, citations

_CASE = re.compile(r"^\s*([a-z][a-z0-9.()&\s/-]*?)?[\s/.-]*(?:no\.?\s?)?([0-9]{1,7})\s?(?:/|-|\bof\b)\s?((?:19|20)[0-9]{2})\s*$")
_NUMYEAR = re.compile(r"\b([0-9]{1,7})\s?(?:/|\bof\b)\s?((?:19|20)[0-9]{2})\b")
_CNR = re.compile(r"\b[a-z]{4}[0-9]{12}\b")


def compact(s: str | None) -> str:
    return re.sub(r"[^0-9a-z]+", "", fold(s))


def case_key(ctype: str | None, num, year) -> str | None:
    """'CRL.A.', 1628, 2023 -> 'crla/1628/2023' (exact-lookup key)."""
    t = compact(ctype)
    t = re.sub(r"no$", "", t)
    try:
        n, y = int(str(num).strip()), int(str(year).strip())
    except (TypeError, ValueError):
        return None
    if not t or not (1900 <= y <= 2100):
        return None
    return f"{t}/{n}/{y}"


def parse_case(s: str | None) -> tuple[str | None, int, int] | None:
    """A case number written as '<type>/<number>/<year>', '<type> No. <n> of <year>', '<n>/<year>' -> (type, n, year)."""
    m = _CASE.match(fold(s))
    if not m:
        return None
    t = compact(m.group(1)) if m.group(1) else None
    if t:
        t = re.sub(r"no$", "", t) or None
    return t, int(m.group(2)), int(m.group(3))


def case_terms(ctype: str | None, num, year) -> tuple[list[str], list[str]]:
    """(terms, exact keys) of one case number."""
    terms, keys = [], []
    try:
        n, y = int(str(num).strip()), int(str(year).strip())
    except (TypeError, ValueError):
        return terms, keys
    if 1900 <= y <= 2100:
        terms.append(f"n:{n}/{y}")
    if n >= 10:
        terms.append(str(n))
    t = compact(ctype)
    t = re.sub(r"no$", "", t)
    if t:
        terms.append(f"t:{t}")
        k = case_key(t, n, y)
        if k:
            keys.append(k)
    return terms, keys


def cnr_key(s: str | None) -> str | None:
    c = re.sub(r"[^0-9A-Za-z]", "", s or "").upper()
    return c if re.fullmatch(r"[A-Z]{4}[0-9]{12}", c) else None


# ---- the query (mirrored in terms.ts)

def parse_query(q: str) -> dict:
    """Search words -> {terms: [...] (all must match), keys: [...] exact lookups, cnrs: [...]}."""
    f = fold(q)[:300]
    keys, cnrs, terms = [], [], []
    for m in _CNR.finditer(f):
        cnrs.append(m.group(0).upper())
    f = _CNR.sub(" ", f)
    whole = _CASE.match(f)
    if whole and whole.group(1):
        # The whole query is one case number: looked up exactly, and also searched as its number/year plus the
        # words of its type ('crl'), so a type written differently still finds it.
        k = case_key(whole.group(1), whole.group(2), whole.group(3))
        if k:
            keys.append(k)
        terms.append(f"n:{int(whole.group(2))}/{whole.group(3)}")
        f = whole.group(1)
    for m in _NUMYEAR.finditer(f):
        terms.append(f"n:{int(m.group(1))}/{m.group(2)}")
    f = _NUMYEAR.sub(" ", f)
    secs, f = sec_terms(f)
    acts, f = act_terms(f)
    terms += secs + acts + words(f)
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return {"terms": out, "keys": keys, "cnrs": cnrs}


# ================================================================ documents: terms, exact keys and display record

HC_STATES = {  # High Court -> the states and territories it hears (s:<state>)
    "allahabad": ["uttarpradesh"], "bombay": ["maharashtra", "goa", "dadraandnagarhavelianddamananddiu"],
    "punjab": ["punjab", "haryana", "chandigarh"], "patna": ["bihar"], "madras": ["tamilnadu", "puducherry"],
    "rajasthan": ["rajasthan"], "telangana": ["telangana"], "kerala": ["kerala", "lakshadweep"], "karnataka": ["karnataka"],
    "orissa": ["odisha"], "madhyapradesh": ["madhyapradesh"], "chhattisgarh": ["chhattisgarh"], "jharkhand": ["jharkhand"],
    "calcutta": ["westbengal", "andamanandnicobarislands"], "gujarat": ["gujarat"], "delhi": ["delhi"],
    "gauhati": ["assam", "nagaland", "mizoram", "arunachalpradesh"], "andhrapradesh": ["andhrapradesh"],
    "himachal": ["himachalpradesh"], "uttarakhand": ["uttarakhand"], "jammu": ["jammuandkashmir", "ladakh"],
    "tripura": ["tripura"], "manipur": ["manipur"], "meghalaya": ["meghalaya"], "sikkim": ["sikkim"],
}


def hc_states(court_name: str | None) -> list[str]:
    c = cid(court_name)
    for k, v in HC_STATES.items():
        if k in c:
            return v
    return []


_ACT_LEAD = re.compile(r"^\s*under act\(s\)\s*under section\(s\)\s*", re.I)
_ACT_TOKENS = re.compile(r"[a-z][a-z.'&]*|[0-9]+(?:-?[a-z]\b)?(?:\s?\([0-9a-z]{1,4}\))*")
_CONNECT = {"and", "r", "w", "rw", "rws", "read", "with", "sec", "section", "sections", "u", "s", "of", "or", "to"}


def parse_acts(s: str | None) -> list[tuple[str, list[str]]]:
    """A district court's 'acts' field -> [(act name, [sections])]:
    'INDIAN PENAL CODE 452,323,504 DOWRY PROHIBITION ACT 3 and 4' -> [('indian penal code', ['452','323','504']), ...].
    Numbers after 'order'/'rule' (CPC orders and rules) are not sections."""
    f = _ACT_LEAD.sub("", fold(s))
    out: list[tuple[str, list[str]]] = []
    name: list[str] = []
    secs: list[str] = []
    skip = False
    for t in _ACT_TOKENS.findall(f):
        if t[0].isdigit():
            if skip:
                skip = False
                continue
            if name:
                secs.append(t)
            continue
        w = t.strip(".")
        if w in ("order", "o", "rule", "r.", "rules"):
            skip = True
            continue
        skip = False
        if w in _CONNECT and (secs or not name):
            continue
        if secs:
            out.append((" ".join(name), secs))
            name, secs = [], []
        name.append(w)
    if name:
        out.append((" ".join(name), secs))
    return out


def act_short(name: str) -> str:
    acts, _ = act_terms(fold(name))
    if acts:
        return acts[0][4:]
    slug = compact(re.sub(r"\b(act|the|of|and|code|[0-9]{4})\b", " ", fold(name)))
    return slug[:30] or "unknown"


def _clip(s, n=SNIPPET):
    s = re.sub(r"\s+", " ", str(s or "")).replace("�", " ").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _rec(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def doc_sc(r: dict, year: int) -> tuple[set, list, dict]:
    t: list[str] = ["c:sc"]
    for fld in ("title", "petitioner", "respondent", "judges", "author"):
        t += words(r.get(fld))
    for fld in ("disposal", "headnote"):
        t += text_terms(r.get(fld))
    for a in r.get("acts") or []:
        t += words(a) + [f"act:{act_short(a)}"]
    for s in r.get("sections") or []:
        t += text_terms(s)
    keys = []
    pc = parse_case(r.get("case_no"))
    t += words(r.get("case_no"))
    if pc:
        tt, kk = case_terms(pc[0], pc[1], pc[2])
        t += tt
        keys += kk
    for c in (r.get("nc"), r.get("citation")):
        if c:
            keys.append("c:" + compact(c))
    if cnr_key(r.get("cnr")):
        keys.append(cnr_key(r.get("cnr")))
    rec = _rec({"l": "sc", "y": year, "c": "Supreme Court of India", "n": r.get("case_no"), "ti": r.get("title"),
                "p": r.get("petitioner"), "r": r.get("respondent"), "j": r.get("judges"), "d": r.get("decided"),
                "x": r.get("disposal"), "s": _clip(r.get("headnote")), "a": _clip("; ".join((r.get("sections") or []) or (r.get("acts") or [])), 200),
                "cnr": r.get("cnr"), "nc": r.get("nc"), "ci": r.get("citation"), "k": r.get("path")})
    return set(t), keys, rec


def doc_hc(r: dict, year: int) -> tuple[set, list, dict]:
    t: list[str] = []
    if r.get("court"):
        t.append(f"c:{cid(r['court'])}")
    t += [f"s:{s}" for s in hc_states(r.get("court_name"))]
    for fld in ("court_name", "petitioner", "respondent", "judges"):
        t += words(r.get(fld))
    t += text_terms(" ".join(x for x in (r.get("disposal"), r.get("snippet")) if x))
    keys = []
    pc = parse_case(r.get("case_no"))
    t += words(r.get("case_no"))
    if pc:
        tt, kk = case_terms(pc[0], pc[1], pc[2])
        t += tt
        keys += kk
    if cnr_key(r.get("cnr")):
        keys.append(cnr_key(r.get("cnr")))
    rec = _rec({"l": "hc", "y": year, "c": r.get("court_name"), "co": r.get("court"), "n": r.get("case_no"),
                "p": r.get("petitioner"), "r": r.get("respondent"), "j": r.get("judges"), "d": r.get("decided"),
                "x": r.get("disposal"), "s": _clip(r.get("snippet")), "cnr": r.get("cnr"), "k": r.get("pdf"), "hid": r.get("id")})
    return set(t), keys, rec


def doc_tribunal(r: dict, year: int, forum: str) -> tuple[set, list, dict]:
    t: list[str] = [f"c:{cid(forum)}"]
    if r.get("forum") == "GST AAR" and r.get("bench"):
        t.append(f"s:{cid(r['bench'])}")
    for fld in ("forum", "bench", "parties", "judges", "category"):
        t += words(r.get(fld))
    t += text_terms(" ".join(str(r.get(x)) for x in ("decision_type", "subject", "act") if r.get(x)))
    keys = []
    cn = r.get("case_number") or ""
    t += text_terms(cn)
    pc = parse_case(cn)
    if pc:
        tt, kk = case_terms(pc[0], pc[1], pc[2])
        t += tt
        keys += kk
    for m in _NUMYEAR.finditer(fold(cn)):
        t.append(f"n:{int(m.group(1))}/{m.group(2)}")
    parties = r.get("parties") or ""
    sides = re.split(r"\s+v(?:s|ersus)?\.?\s+", parties, maxsplit=1, flags=re.I)
    rec = _rec({"l": "tribunal", "y": year or None, "c": " ".join(x for x in (r.get("forum"), r.get("bench")) if x), "co": forum,
                "n": cn, "p": sides[0] if sides else None, "r": sides[1] if len(sides) > 1 else None, "j": r.get("judges"),
                "d": r.get("date"), "x": r.get("decision_type"), "s": _clip(r.get("subject")), "k": r.get("file"),
                "u": r.get("source_url"), "sc": True if r.get("scan") else None})
    return set(t), keys, rec


def doc_district(r: dict, year: int) -> tuple[set, list, dict]:
    t: list[str] = []
    if r.get("state"):
        t.append(f"s:{cid(r['state'])}")
    for fld in ("district", "complex"):
        if r.get(fld):
            t.append(f"c:{cid(r[fld])}")
    for fld in ("petitioner", "respondent", "state", "district", "complex", "court", "judge", "stage", "disposal", "fir"):
        t += words(r.get(fld))
    acts = r.get("acts")
    if acts:
        t += text_terms(_ACT_LEAD.sub("", acts))
        for name, secs in parse_acts(acts):
            t.append(f"act:{act_short(name)}")
            t += _sec_terms(" ".join(secs))
    keys = []
    num = r.get("number")
    if isinstance(num, str):
        m = re.search(r"([0-9]{1,7})\s*/\s*(?:19|20)[0-9]{2}\s*$", num) or re.search(r"([0-9]{1,7})\s*$", num)
        num = int(m.group(1)) if m else None
    tt, kk = case_terms(r.get("case_type"), num, r.get("reg_year"))
    t += tt + words(r.get("case_type"))
    keys += kk
    if cnr_key(r.get("cnr")):
        keys.append(cnr_key(r.get("cnr")))
    finals = [o.get("pdf") for o in r.get("final_orders") or [] if o.get("pdf")]
    rec = _rec({"l": "district", "y": year, "c": r.get("court"), "n": f"{r.get('case_type') or ''}/{num if num is not None else ''}/{r.get('reg_year') or ''}",
                "ty": r.get("case_type"), "p": r.get("petitioner"), "r": r.get("respondent"), "j": r.get("judge"),
                "d": r.get("decided") or r.get("registered") or r.get("filed"), "x": r.get("disposal") or r.get("status"),
                "sg": r.get("stage"), "a": _clip(_ACT_LEAD.sub("", acts or ""), 200), "st": r.get("state"), "di": r.get("district"),
                "cx": r.get("complex"), "cnr": r.get("cnr"), "k": finals[:3]})
    return set(t), keys, rec


# ---- the docs level

COURT_RULE_COURTS = {
    "delhi": "Delhi High Court", "kerala": "Kerala High Court", "bombay": "Bombay High Court", "rajasthan": "Rajasthan High Court",
    "chhattisgarh": "Chhattisgarh High Court", "patna": "Patna High Court", "madras": "Madras High Court", "mp": "Madhya Pradesh High Court",
    "orissa": "Orissa High Court", "gauhati": "Gauhati High Court", "meghalaya": "Meghalaya High Court", "calcutta": "Calcutta High Court",
    "gujarat": "Gujarat High Court", "ap": "Andhra Pradesh High Court", "jk": "Jammu and Kashmir High Court", "manipur": "Manipur High Court",
    "uttarakhand": "Uttarakhand High Court", "sc": "Supreme Court of India", "telangana": "Telangana High Court", "nclat": "NCLAT", "ngt": "National Green Tribunal",
}


def doc_year(r: dict) -> int:
    for v in (r.get("date"), r.get("year"), r.get("bill_year")):
        m = re.match(r"\s*((?:18|19|20)\d{2})", str(v or ""))
        if m:
            return int(m.group(1))
    return 0


def doc_issuer(r: dict, typ: str) -> tuple[str, str]:
    """(issuer as shown, issuer id for the filter)."""
    if typ == "courtrule":
        c = r.get("court") or ""
        return COURT_RULE_COURTS.get(c, c.upper()), cid(c)
    if typ in ("circular", "govtorder") and r.get("court"):
        return r.get("body") or r["court"].upper(), cid(r["court"])
    if typ == "bill":
        return r.get("body") or "Parliament of India", "parliament"
    if typ == "lcr":
        return "Law Commission of India", "lawcommission"
    shown = r.get("department") or r.get("ministry") or r.get("body") or "Government of India"
    return shown, cid(r.get("ministry") or r.get("body") or "indiacode")


def doc_open(r: dict) -> dict | None:
    """Where the stored original is: a Drive file, or a file inside an India Code zip (path, offset, size)."""
    if r.get("file"):
        return {"st": r.get("store") or "drive", "p": r["file"]}
    files = r.get("files") or []
    f = next((x for x in files if x.get("name") == r.get("main_file")), files[0] if files else None)
    if f and f.get("zip") and f.get("size"):
        return {"st": f.get("store") or "drive", "p": f["zip"], "o": f.get("offset"), "z": f["size"], "nm": f.get("name")}
    return None


def doc_docs(r: dict, typ: str, path: str, year: int) -> tuple[set, list, dict]:
    # some sources keep a field as an object (an act {id, name}); only text fields are read
    r = {k: (v.get("name") or v.get("title") if isinstance(v, dict) else v) for k, v in r.items()}
    r = {k: v for k, v in r.items() if v is None or isinstance(v, (str, int, float, bool, list))}
    issuer, iid = doc_issuer(r, typ)
    if typ == "circular":  # a regulator's page lists regulations and notifications too: the kind it gives decides
        cat = fold(r.get("category"))
        typ = "rule" if "regulation" in cat or "rules" in cat else "notification" if "notification" in cat else "circular"
    text = re.sub(r"\s+", " ", str(r.get("text") or "")).strip()
    t: list[str] = [f"dt:{typ}"] + ([f"is:{iid}"] if iid else [])
    for fld in ("title", "hindi_title", "number", "category", "collection", "ministry", "house", "act"):
        t += words(r.get(fld))
    t += words(issuer)
    t += text_terms(text[:SNIPPET])
    for m in _NUMYEAR.finditer(fold(r.get("number") or "")):
        t.append(f"n:{int(m.group(1))}/{m.group(2)}")
    rec = _rec({"l": "docs", "y": year or None, "ty": typ, "c": issuer, "ii": iid, "n": r.get("number"), "ti": _clip(r.get("title"), 300),
                "d": r.get("date"), "x": r.get("category") or r.get("collection"), "s": _clip(text, SNIPPET), "k": doc_open(r),
                "u": r.get("source_url"), "sc": True if r.get("scan_only") or r.get("text_status") == "scan_only" else None})
    return set(t), [], rec


# ================================================================ stores

DRIVE_API = "https://www.googleapis.com/drive/v3"


class Store:
    """Files of one index build in Google Drive or Backblaze, written whole and read by byte ranges."""

    def __init__(self, kind: str) -> None:
        from .storage import B2, Drive
        self.kind = kind
        self.drive = Drive() if kind == "drive" else None
        self.b2 = B2() if kind == "b2" else None

    def put(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Write a file; returns its handle (Drive file id, or the B2 file name)."""
        for attempt in range(5):
            try:
                if self.drive:
                    return self.drive.put(name, data, content_type)
                self.b2.put(name, data, content_type)
                return name
            except Exception:  # noqa: BLE001 - network hiccups on long runs
                if attempt == 4:
                    raise
                time.sleep(5 * 2 ** attempt)
        raise RuntimeError("unreachable")

    def get(self, name: str) -> bytes | None:
        for attempt in range(5):
            try:
                return (self.drive or self.b2).get(name)
            except Exception:  # noqa: BLE001
                if attempt == 4:
                    raise
                time.sleep(5 * 2 ** attempt)
        return None

    def read(self, handle: str, start: int, length: int) -> bytes:
        """Bytes [start, start+length) of a file by its handle."""
        if length <= 0:
            return b""
        rng = {"Range": f"bytes={start}-{start + length - 1}"}
        for attempt in range(6):
            try:
                if self.drive:
                    # Not Drive._call: its retry drops the Range header, so a retry would fetch the whole file.
                    r = self.drive.http.get(f"{DRIVE_API}/files/{handle}", params={"alt": "media"}, headers={**self.drive._headers(), **rng})
                else:
                    a = self.b2._authorize(again=attempt > 0)
                    r = self.b2.http.get(f"{a['apiInfo']['storageApi']['downloadUrl']}/file/{self.b2.bucket}/{handle}",
                                         headers={"Authorization": a["authorizationToken"], **rng})
                if r.status_code in (200, 206):
                    data = r.content
                    return data[start:start + length] if r.status_code == 200 else data
            except Exception:  # noqa: BLE001
                pass
            time.sleep(min(30, 2 ** attempt))
        raise RuntimeError(f"range read of {handle} kept failing")

    def folder(self, name: str) -> None:
        if self.drive:
            self.drive.folder(name)

    def delete(self, name: str) -> None:
        try:
            (self.drive or self.b2).delete(name)
        except Exception as e:  # noqa: BLE001
            print(f"  could not remove {name}: {e}", flush=True)


def _source(store: str):
    from .storage import B2, Drive
    return Drive() if store == "drive" else B2()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(db, path: str, store: str, kind: str, data_len: int, **kw) -> None:
    db.insert("library_files", {"path": path, "store": store, "kind": kind, "item": ITEM, "bytes": data_len, "source": SOURCE,
                                "updated_at": _now(), **kw}, upsert_on="path")


# ================================================================ plan

# ---- the docs level: what the Library's Circulars & notices and Formats tabs search (items 10, 11, 19, 21, 22, 24, 25,
# 28, 31). One row per document: title, issuer, number, date, the first ~300 characters of its text, and its type.
# Only Drive-stored files for now; the Backblaze-stored parts (formats, court-rule texts, central rules and
# regulations, regulators' regulations) are left for after the demo, when downloads from Backblaze are allowed again.
DOC_SOURCES = [  # (item, kind in library_files, type, path pattern)
    (19, "circular_text", "circular", r"library/circulars/[^/]+/text/.+\.jsonl\.gz"),
    (21, "law_commission_text", "lcr", r"library/law-commission/text/.+\.jsonl\.gz"),
    (22, None, None, r"library/notifications/(?!.*index)[^/]+/[^/]+\.jsonl\.gz"),
    (25, "repealed_act", "repealedact", r"library/repealed-acts/(?!index)[^/]+\.jsonl\.gz"),
    (28, "govt_order_text", "govtorder", r"library/govt-orders/[^/]+/text/.+\.jsonl\.gz"),
    (31, "bill_text", "bill", r"library/bills/text/.+\.jsonl\.gz"),
]
DOCS_LATER = ["item 10 formats (Backblaze)", "item 11 court-rule texts (Backblaze; the Drive PDFs are in, by title)",
              "item 19 regulators' regulations (Backblaze)", "item 24 central rules and regulations texts (Backblaze)"]
NOTIFICATION_TYPES = {"circular": "circular", "order": "govtorder"}  # item 22 collections; the rest are notifications


def doc_sources(db, sample: bool = False) -> dict:
    """The docs level's sources: Drive text files, and court rules known only by their PDF (title from library_files)."""
    files = []
    for item, kind, typ, rx in DOC_SOURCES:
        q = f"select=path,kind,rows&item=eq.{item}&store=eq.drive&path=like.*.jsonl.gz&order=path"
        for r in db.select("library_files", q):
            if (kind and r["kind"] != kind) or not re.fullmatch(rx, r["path"]):
                continue
            t = typ or NOTIFICATION_TYPES.get(r["path"].split("/")[2], "notification")
            files.append({"path": r["path"], "type": t, "rows": r["rows"] or 0})
    rules = [{"path": r["path"], "title": r.get("title"), "court": r.get("court")}
             for r in db.select("library_files", "select=path,title,court&item=eq.11&store=eq.drive&kind=eq.court_rules&order=path")]
    if sample:  # a few of each kind
        by: dict[str, list] = {}
        for f in files:
            parts = f["path"].split("/")
            by.setdefault("/".join(parts[:3] if len(parts) > 3 else parts[:2]), []).append(f)
        files = [f for fs in by.values() for f in fs[-2:]]
        rules = rules[:300]
    return {"level": "docs", "year": None, "files": sorted(files, key=lambda f: f["path"]), "rules": rules, "store": "drive",
            "rows": sum(f["rows"] for f in files) + len(rules)}


def sources(db, sample: bool = False, docs: bool = False, only_docs: bool = False) -> list[dict]:
    """Every source file in doc order: [{level, year, path, store, rows}] (tribunals: one entry, all forum files;
    docs, when asked for: one entry, read by unit_docs in year order)."""
    if only_docs:
        return [doc_sources(db, sample)]
    sc = [r for r in db.select("library_files", "select=path,store,rows,year&item=eq.12&order=path")
          if re.fullmatch(r"judgments/sc/\d{4}\.jsonl\.gz", r["path"])]
    hc = [r for r in db.select("library_files", "select=path,store,rows,year&item=eq.13&order=path")
          if re.fullmatch(r"judgments/hc/[^/]+/\d{4}\.jsonl\.gz", r["path"])]
    tr = [r for r in db.select("library_files", "select=path,store,rows,year&item=eq.33&store=eq.b2&order=path")
          if re.fullmatch(r"judgments/tribunals/[a-z0-9-]+\.jsonl\.gz", r["path"])]
    dc = [r for r in db.select("library_files", "select=path,store,rows,year&item=eq.14&order=path")
          if re.fullmatch(r"library/district/\d{4}/[^/]+\.jsonl\.gz", r["path"])]
    if sample:
        hc = [r for r in hc if r["path"] in SAMPLE["hc"]]
        tr = [r for r in tr if r["path"] in SAMPLE["tribunal"]]
        dc = [r for r in dc if r["path"] in SAMPLE["district"]]

    def yr(r):
        return int(re.search(r"/(\d{4})(?:/|\.jsonl)", r["path"]).group(1))

    out = []
    for level, rows in (("sc", sc), ("hc", hc)):
        for r in sorted(rows, key=lambda r: (-yr(r), r["path"])):
            out.append({"level": level, "year": yr(r), "path": r["path"], "store": r["store"], "rows": r["rows"] or 0})
    if tr:
        out.append({"level": "tribunal", "year": None, "paths": sorted(r["path"] for r in tr), "store": "b2", "rows": sum(r["rows"] or 0 for r in tr)})
    for r in sorted(dc, key=lambda r: (-yr(r), r["path"])):
        out.append({"level": "district", "year": yr(r), "path": r["path"], "store": r["store"], "rows": r["rows"] or 0})
    if docs:
        out.append(doc_sources(db, sample))
    return out


SAMPLE = {"hc": ["judgments/hc/9_13/2026.jsonl.gz", "judgments/hc/8_9/2025.jsonl.gz"],
          "tribunal": ["judgments/tribunals/aptel.jsonl.gz"], "district": ["library/district/2026/6.jsonl.gz"]}


def make_units(srcs: list[dict]) -> list[list[dict]]:
    units: list[list[dict]] = []
    cur, n, lvl = [], 0, None
    for s in srcs:
        if cur and (n + s["rows"] > UNIT_ROWS or s["level"] != lvl):
            units.append(cur)
            cur, n = [], 0
        cur.append(s)
        n += s["rows"]
        lvl = s["level"]
    if cur:
        units.append(cur)
    return units


def plan(build: str | None, root: str, store_kind: str, sample: bool, docs: bool = False, only_docs: bool = False) -> dict:
    from .db import DB
    db = DB()
    build = build or datetime.now(timezone.utc).strftime("b%Y%m%d-%H%M")
    st = Store(store_kind)
    base = f"{root}/{build}"
    old = st.get(f"{base}/plan.json") if store_kind == "drive" else None
    if old:
        p = json.loads(old)
        print(f"  {build}: plan already made ({len(p['units'])} units); carrying on", file=sys.stderr)
    else:
        units = make_units(sources(db, sample, docs, only_docs))
        p = {"build": build, "root": root, "store": store_kind, "created": _now(), "units": units}
        for sub in ("", "/docs", "/map", "/meta", "/post"):
            st.folder(base + sub)
        data = json.dumps(p).encode()
        st.put(f"{base}/plan.json", data, "application/json")
        _record(db, f"{base}/plan.json", store_kind, "search_plan", len(data), title=f"Search index {build}: plan", rows=len(units))
        print(f"  {build}: {len(units)} units, {sum(s['rows'] for u in units for s in u):,} rows by the library map", file=sys.stderr)
    done = done_names(db, base)
    p["todo"] = [i for i in range(len(p["units"])) if f"meta/u{i:04d}.json" not in done]
    p["parts_todo"] = [i for i in range(PARTS) if f"meta/p{i:02d}.json" not in done]
    return p


def done_names(db, base: str) -> set[str]:
    return {r["path"][len(base) + 1:] for r in db.select("library_files", f"select=path&path=like.{base}/*&order=path")}


# ================================================================ docs (the map step)

def _rows(data: bytes):
    """JSON lines of a gzip file, one at a time (split on '\\n' only: U+2028 inside text is not a line end)."""
    with io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(data)), encoding="utf-8", newline="\n") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


# ---- Drive copies of the Backblaze sources
# Backblaze's free tier caps downloads per day (the first full build hit "download_cap_exceeded" after ~1.8 GB of High
# Court summaries, 28 Oct 2026). A source read from Backblaze is copied to Drive (search/src/<path>, item 34) and later
# builds read the copy while it is of the same version (same bytes and time in library_files), so a rebuild downloads
# from Backblaze only what changed.

MIRROR = "search/src"
_MIRROR = {"drive": None, "db": None}


def _mirror_tools():
    if _MIRROR["drive"] is None:
        from .db import DB
        _MIRROR["drive"], _MIRROR["db"] = Store("drive"), DB()
    return _MIRROR["drive"], _MIRROR["db"]


def _version(db, path: str) -> str | None:
    r = db.select("library_files", f"select=bytes,updated_at&path=eq.{path}")
    return f"Drive copy of Backblaze {path} ({r[0]['bytes']} bytes, {r[0]['updated_at']})" if r else None


def mirror_get(path: str) -> bytes | None:
    drive, db = _mirror_tools()
    try:
        want = _version(db, path)
        have = db.select("library_files", f"select=source&path=eq.{MIRROR}/{path}")
        if want and have and have[0]["source"] == want:
            return drive.get(f"{MIRROR}/{path}")
    except Exception as e:  # noqa: BLE001 - no copy: read Backblaze
        print(f"  (no Drive copy of {path}: {e})", flush=True)
    return None


def mirror_put(path: str, data: bytes) -> None:
    drive, db = _mirror_tools()
    try:
        want = _version(db, path)
        drive.put(f"{MIRROR}/{path}", data, "application/gzip")
        db.insert("library_files", {"path": f"{MIRROR}/{path}", "store": "drive", "kind": "search_source", "item": ITEM,
                                    "bytes": len(data), "source": want, "title": f"Search index source copy: {path}",
                                    "updated_at": _now()}, upsert_on="path")
    except Exception as e:  # noqa: BLE001 - the build goes on without the copy
        print(f"  (could not copy {path} to Drive: {e})", flush=True)


def mirror(max_mb: float) -> None:
    """Copy Backblaze sources (items 12, 13, 33) to Drive, at most max_mb of downloads in this run (stay under the
    daily cap); newest first. Run on a few days to copy them all."""
    from .db import DB
    from .storage import B2
    db, b2 = DB(), B2()
    have = {r["path"][len(MIRROR) + 1:]: r["source"] for r in db.select("library_files", f"select=path,source&path=like.{MIRROR}/*&order=path")}
    todo = [s for s in sources(db) if s["store"] == "b2" and s["level"] != "tribunal"]
    spent = 0
    for s in todo:
        want = _version(db, s["path"])
        if have.get(s["path"]) == want:
            continue
        size = (db.select("library_files", f"select=bytes&path=eq.{s['path']}") or [{"bytes": 0}])[0]["bytes"] or 0
        if spent + size > max_mb * 1e6:
            break
        data = b2.get(s["path"])
        spent += len(data or b"")
        if data:
            mirror_put(s["path"], data)
    print(f"  copied {spent / 1e6:.0f} MB from Backblaze to Drive ({MIRROR}/)")


def unit_docs(unit: list[dict]):
    """(level, year, terms, keys, record) for every row of a unit, in doc order."""
    srcs = {"b2": None, "drive": None}

    cache = os.environ.get("HAAZIR_SEARCH_SOURCE_CACHE")  # local runs: keep source files, so a rerun reads no store

    def get(store, path):
        local = os.path.join(cache, path.replace("/", "_")) if cache else None
        if local and os.path.exists(local):
            with open(local, "rb") as fh:
                return fh.read()
        if store == "b2":
            d = mirror_get(path)  # the Drive copy, when it is of the same version (no Backblaze download)
            if d is not None:
                return d
        if srcs[store] is None:
            srcs[store] = _source(store)
        for attempt in range(5):
            try:
                d = srcs[store].get(path)
                if d is None:
                    raise RuntimeError(f"{path} is in library_files but not in {store}")
                if local:
                    with open(local, "wb") as fh:
                        fh.write(d)
                if store == "b2":
                    mirror_put(path, d)
                return d
            except Exception:  # noqa: BLE001
                if attempt == 4:
                    raise
                time.sleep(10 * 2 ** attempt)

    for s in unit:
        if s["level"] == "docs":
            # every document of the files (and the court rules known by title), newest year first, then file and row
            # order; each row made into its terms and record at once, so the texts are not all held in memory
            out = []
            for pi, f in enumerate(s["files"]):
                for ri, r in enumerate(_rows(get("drive", f["path"]))):
                    y = doc_year(r)
                    out.append((-y, pi, ri, doc_docs(r, f["type"], f["path"], y)))
            for ri, r in enumerate(s.get("rules") or []):
                out.append((0, len(s["files"]), ri, doc_docs({"title": r.get("title"), "file": r["path"], "court": r.get("court")},
                                                            "courtrule", r["path"], 0)))
            out.sort(key=lambda x: x[:3])
            for ny, _, _, (terms, keys, rec) in out:
                yield "docs", -ny, terms, keys, rec
            continue
        if s["level"] == "tribunal":
            rows = []
            for pi, path in enumerate(s["paths"]):
                forum = path.rsplit("/", 1)[1].removesuffix(".jsonl.gz")
                for ri, r in enumerate(_rows(get("b2", path))):
                    try:
                        y = int(r.get("year") or 0)
                    except (TypeError, ValueError):
                        y = 0
                    rows.append((-y, pi, ri, forum, r))
            rows.sort(key=lambda x: x[:3])
            for ny, _, _, forum, r in rows:
                terms, keys, rec = doc_tribunal(r, -ny, forum)
                yield "tribunal", -ny, terms, keys, rec
            continue
        fn = doc_sc if s["level"] == "sc" else doc_hc if s["level"] == "hc" else doc_district
        for r in _rows(get(s["store"], s["path"])):
            terms, keys, rec = fn(r, s["year"])
            yield s["level"], s["year"], terms, keys, rec


def crc(s: str) -> int:
    return zlib.crc32(s.encode())


def _segment(items: list[tuple[int, str, array]]) -> bytes:
    head, blobs = bytearray(), []
    for b, t, ids in items:
        tb = t.encode()
        head += struct.pack("<HHI", b, len(tb), len(ids)) + tb
        blobs.append(ids.tobytes())
    return zlib.compress(struct.pack("<II", len(items), len(head)) + bytes(head) + b"".join(blobs), 1)


def run_docs(build: str, root: str, store_kind: str, index: int, plan_data: dict | None = None) -> dict:
    from .db import DB
    db, st = DB(), Store(store_kind)
    base = f"{root}/{build}"
    p = plan_data or json.loads(st.get(f"{base}/plan.json"))
    unit = p["units"][index]
    t0 = time.time()
    post: dict[str, array] = {}
    idk: dict[str, array] = {}
    docs, block, lengths, years = bytearray(), [], [], []
    n = 0
    for level, year, terms, keys, rec in unit_docs(unit):
        for t in terms:
            a = post.get(t)
            if a is None:
                post[t] = a = array("I")
            a.append(n)
        for k in set(keys):
            a = idk.get(k)
            if a is None:
                idk[k] = a = array("I")
            a.append(n)
        block.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        if len(block) == DOCBLOCK:
            z = zlib.compress("\n".join(block).encode(), 6)
            docs += z
            lengths.append(len(z))
            block = []
        if years and years[-1][0] == level and years[-1][1] == year:
            years[-1][2] += 1
        else:
            years.append([level, year, 1])
        n += 1
    if block:
        z = zlib.compress("\n".join(block).encode(), 6)
        docs += z
        lengths.append(len(z))
    t1 = time.time()
    # the term lists, split into the 16 parts, each sorted by (bucket, term)
    byp: list[list] = [[] for _ in range(PARTS)]
    npost = 0
    for t, a in post.items():
        b = crc(t) % NB
        byp[b % PARTS].append((b, t, a))
        npost += len(a)
    idp: list[list] = [[] for _ in range(PARTS)]
    for k, a in idk.items():
        b = crc(k) % NIB
        idp[b % PARTS].append((b, k, a))
    mp, parts, idparts = bytearray(), [], []
    for lst, out in ((byp, parts), (idp, idparts)):
        for items in lst:
            items.sort(key=lambda x: (x[0], x[1]))
            seg = _segment(items)
            out.append([len(mp), len(seg)])
            mp += seg
    nterms = len(post)
    del post, idk, byp, idp
    name = f"u{index:04d}"
    dh = st.put(f"{base}/docs/{name}.bin", bytes(docs))
    _record(db, f"{base}/docs/{name}.bin", store_kind, "search_docs", len(docs), title=f"Search index {build}: doc store {name}", rows=n)
    mh = st.put(f"{base}/map/{name}.bin", bytes(mp))
    _record(db, f"{base}/map/{name}.bin", store_kind, "search_map", len(mp), title=f"Search index {build}: term lists {name} (build only)", rows=nterms)
    bl = array("I", lengths)
    meta = {"unit": index, "count": n, "years": years, "docs": {"h": dh, "size": len(docs), "bl": base64.b64encode(bl.tobytes()).decode()},
            "map": {"h": mh, "size": len(mp), "parts": parts, "idparts": idparts}, "terms": nterms, "postings": npost,
            "secs": [round(t1 - t0, 1), round(time.time() - t1, 1)]}
    data = json.dumps(meta).encode()
    st.put(f"{base}/meta/{name}.json", data, "application/json")
    _record(db, f"{base}/meta/{name}.json", store_kind, "search_meta", len(data), title=f"Search index {build}: unit {name} meta", rows=n)
    print(f"  {name}: {n:,} docs, {nterms:,} terms, {npost:,} postings, docs {len(docs) / 1e6:.1f} MB, map {len(mp) / 1e6:.1f} MB "
          f"({meta['secs'][0]}s read+terms, {meta['secs'][1]}s write)", flush=True)
    return meta


# ================================================================ postings (the reduce step)

def varints(vals) -> bytes:
    out = bytearray()
    for v in vals:
        while v >= 0x80:
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        out.append(v)
    return bytes(out)


def _varints_np(np, v):
    """numpy uint32 array -> varint bytes and each value's start offset."""
    v = v.astype(np.uint32)
    lens = np.ones(len(v), np.int64)
    for k in (7, 14, 21, 28):
        lens += v >= (1 << k)
    ends = np.cumsum(lens)
    starts = ends - lens
    out = np.zeros(int(ends[-1]) if len(v) else 0, np.uint8)
    for k in range(5):
        m = lens > k
        if not m.any():
            break
        byte = ((v[m] >> np.uint32(7 * k)) & np.uint32(0x7F)).astype(np.uint8)
        byte |= ((lens[m] > k + 1).astype(np.uint8) << np.uint8(7))
        out[starts[m] + k] = byte
    return out.tobytes(), starts


def encode_postings(ids) -> tuple[bytes, list[tuple[int, int]]]:
    """Ascending ids -> bytes of blocks of BLOCK ids (each: first id, then deltas, all varint) and the skip table
    [(first id, byte offset)] per block."""
    n = len(ids)
    if n < 2048:
        out, skips = bytearray(), []
        ids = ids.tolist() if hasattr(ids, "tolist") else list(ids)
        for i in range(0, n, BLOCK):
            blk = ids[i:i + BLOCK]
            skips.append((blk[0], len(out)))
            out += varints([blk[0]] + [blk[j] - blk[j - 1] for j in range(1, len(blk))])
        return bytes(out), skips
    import numpy as np
    a = np.asarray(ids, dtype=np.int64)
    vals = np.empty(n, np.int64)
    vals[0] = a[0]
    vals[1:] = a[1:] - a[:-1]
    heads = np.arange(0, n, BLOCK)
    vals[heads] = a[heads]
    data, starts = _varints_np(np, vals)
    return data, [(int(a[h]), int(starts[h])) for h in heads]


def dict_line(term: str, ids, off: int) -> tuple[str, bytes]:
    """The dictionary line of a term and its postings bytes (none when inlined)."""
    df = len(ids)
    if df <= INLINE:
        return f"{term}\t{df}\t=" + ",".join(str(int(i)) for i in ids), b""
    data, skips = encode_postings(ids)
    sk, pf, po = [], 0, 0
    for i, (f, o) in enumerate(skips):
        sk.append(str(f) if i == 0 else f"{f - pf}.{o - po}")
        pf, po = f, o
    return f"{term}\t{df}\t{off}\t{len(data)}\t{int(ids[-1])}\t{','.join(sk)}", data


def _seg_iter(raw: bytes, ui: int, base: int):
    n, hl = struct.unpack_from("<II", raw, 0)
    pos, off = 8, 8 + hl
    for _ in range(n):
        b, tl, c = struct.unpack_from("<HHI", raw, pos)
        pos += 8
        yield b, raw[pos:pos + tl].decode(), ui, base, off, c
        pos += tl
        off += 4 * c


def _merge_ids(np, raws, group):
    total = sum(g[5] for g in group)
    if total <= 256:
        out = []
        for _, _, ui, base, off, c in group:
            a = array("I")
            a.frombytes(raws[ui][off:off + 4 * c])
            out += [x + base for x in a]
        return out
    return np.concatenate([np.frombuffer(raws[ui], dtype=np.uint32, count=c, offset=off).astype(np.int64) + base
                           for _, _, ui, base, off, c in group])


def run_postings(build: str, root: str, store_kind: str, part: int) -> dict:
    import numpy as np
    from .db import DB
    db, st = DB(), Store(store_kind)
    base = f"{root}/{build}"
    p = json.loads(st.get(f"{base}/plan.json"))
    t0 = time.time()
    metas = _metas(st, base, len(p["units"]))
    bases = _bases(metas)

    def fetch(i):
        m = metas[i]["map"]
        o, ln = m["parts"][part]
        io_, iln = m["idparts"][part]
        return zlib.decompress(st.read(m["h"], o, ln)), zlib.decompress(st.read(m["h"], io_, iln))

    with ThreadPoolExecutor(8) as pool:
        got = list(pool.map(fetch, range(len(metas))))
    raws = [g[0] for g in got]
    idraws = [g[1] for g in got]
    t1 = time.time()
    out = bytearray()
    dir_ = {}
    nterms = 0
    streams = [_seg_iter(raws[i], i, bases[i]) for i in range(len(raws))]
    cur_b, lines, group, key = None, [], [], None

    def flush_term():
        nonlocal nterms
        ids = _merge_ids(np, raws, group)
        line, data = dict_line(key[1], ids, len(out))
        out.extend(data)
        lines.append(line)
        nterms += 1

    def flush_bucket():
        z = zlib.compress("\n".join(lines).encode(), 6)
        dir_[cur_b] = [len(out), len(z)]
        out.extend(z)

    for it in heapq.merge(*streams):
        k = (it[0], it[1])
        if k != key:
            if group:
                flush_term()
            if cur_b is not None and it[0] != cur_b:
                flush_bucket()
                lines = []
            cur_b = it[0]
            key, group = k, []
        group.append(it)
    if group:
        flush_term()
    if cur_b is not None:
        flush_bucket()
    del raws
    # the exact-lookup table: per bucket, sorted 'key<TAB>id,id' lines
    idir = {}
    nkeys = 0
    cur_b, lines, group, key = None, [], [], None
    streams = [_seg_iter(idraws[i], i, bases[i]) for i in range(len(idraws))]

    def flush_key():
        nonlocal nkeys
        ids = _merge_ids(np, idraws, group)
        lines.append(f"{key[1]}\t" + ",".join(str(int(x)) for x in ids[:200]))
        nkeys += 1

    def flush_idb():
        z = zlib.compress("\n".join(lines).encode(), 6)
        idir[cur_b] = [len(out), len(z)]
        out.extend(z)

    for it in heapq.merge(*streams):
        k = (it[0], it[1])
        if k != key:
            if group:
                flush_key()
            if cur_b is not None and it[0] != cur_b:
                flush_idb()
                lines = []
            cur_b = it[0]
            key, group = k, []
        group.append(it)
    if group:
        flush_key()
    if cur_b is not None:
        flush_idb()
    if len(out) >= 2 ** 32:
        raise RuntimeError(f"part {part} is {len(out)} bytes: more than 4 GB, add parts")
    t2 = time.time()
    name = f"p{part:02d}"
    h = st.put(f"{base}/post/{name}.bin", bytes(out))
    _record(db, f"{base}/post/{name}.bin", store_kind, "search_postings", len(out), title=f"Search index {build}: postings part {name}", rows=nterms)
    meta = {"part": part, "h": h, "size": len(out), "dir": dir_, "idir": idir, "terms": nterms, "keys": nkeys,
            "secs": [round(t1 - t0, 1), round(t2 - t1, 1), round(time.time() - t2, 1)]}
    data = json.dumps(meta).encode()
    st.put(f"{base}/meta/{name}.json", data, "application/json")
    _record(db, f"{base}/meta/{name}.json", store_kind, "search_meta", len(data), title=f"Search index {build}: part {name} meta", rows=nterms)
    print(f"  {name}: {nterms:,} terms, {nkeys:,} exact keys, {len(out) / 1e6:.1f} MB ({meta['secs']} s read/merge/write)", flush=True)
    return meta


def _metas(st: Store, base: str, n: int) -> list[dict]:
    def one(i):
        d = st.get(f"{base}/meta/u{i:04d}.json")
        if not d:
            raise RuntimeError(f"unit {i} is not built yet: run docs {i} first")
        return json.loads(d)
    with ThreadPoolExecutor(8) as pool:
        return list(pool.map(one, range(n)))


def _bases(metas: list[dict]) -> list[int]:
    out, b = [], 0
    for m in metas:
        out.append(b)
        b += m["count"]
    return out


# ================================================================ manifest (the swap)

def manifest_name(root: str) -> str:
    return f"{root}/manifest.json.gz"


def run_manifest(build: str, root: str, store_kind: str, pointer: str | None = None, keep_map: bool = False) -> dict:
    from .db import DB
    from .storage import B2
    db, st = DB(), Store(store_kind)
    base = f"{root}/{build}"
    p = json.loads(st.get(f"{base}/plan.json"))
    metas = _metas(st, base, len(p["units"]))
    pm = []
    for i in range(PARTS):
        d = st.get(f"{base}/meta/p{i:02d}.json")
        if not d:
            raise RuntimeError(f"part {i} is not built yet: run postings {i} first")
        pm.append(json.loads(d))
    bases = _bases(metas)
    total = bases[-1] + metas[-1]["count"] if metas else 0
    files, units = [], []
    for i, m in enumerate(metas):
        files.append({"n": f"docs/u{i:04d}.bin", "h": m["docs"]["h"], "size": m["docs"]["size"]})
        units.append({"f": len(files) - 1, "base": bases[i], "n": m["count"], "bl": m["docs"]["bl"]})
    parts = []
    for m in pm:
        files.append({"n": f"post/p{m['part']:02d}.bin", "h": m["h"], "size": m["size"]})
        parts.append(len(files) - 1)
    d = array("I", [0] * (NB * 2))
    for m in pm:
        for b, (o, ln) in m["dir"].items():
            d[int(b) * 2], d[int(b) * 2 + 1] = o, ln
    idd = array("I", [0] * (NIB * 2))
    for m in pm:
        for b, (o, ln) in m["idir"].items():
            idd[int(b) * 2], idd[int(b) * 2 + 1] = o, ln
    levels: dict[str, list[int]] = {}
    years: dict[str, dict[str, list[int]]] = {}
    for i, m in enumerate(metas):
        pos = bases[i]
        for level, year, c in m["years"]:
            lv = levels.setdefault(level, [pos, pos + c - 1])
            lv[1] = pos + c - 1
            yr = years.setdefault(level, {}).setdefault(str(year or 0), [pos, pos + c - 1])
            yr[1] = pos + c - 1
            pos += c
    man = {"v": 1, "build": build, "root": root, "store": store_kind, "created": _now(), "docs": total,
           "nb": NB, "nib": NIB, "parts_n": PARTS, "block": BLOCK, "docblock": DOCBLOCK,
           "levels": levels, "years": years, "files": files, "units": units, "parts": parts,
           "dir": base64.b64encode(d.tobytes()).decode(), "idir": base64.b64encode(idd.tobytes()).decode(),
           "stats": {"terms": sum(m["terms"] for m in pm), "keys": sum(m["keys"] for m in pm),
                     "postings": sum(m["postings"] for m in metas),
                     "bytes": {"docs": sum(m["docs"]["size"] for m in metas), "postings": sum(m["size"] for m in pm)},
                     "per_level": {k: v[1] - v[0] + 1 for k, v in levels.items()}}}
    data = gzip.compress(json.dumps(man, separators=(",", ":")).encode(), 9)
    st.put(f"{base}/manifest.json.gz", data, "application/gzip")
    _record(db, f"{base}/manifest.json.gz", store_kind, "search_manifest", len(data), title=f"Search index {build}: manifest", rows=total)
    # The swap: one write of the pointer file (a single small file, replaced whole, so readers see the old build or the
    # new one, never half). Readers take it from Drive (no daily download cap); the Backblaze copy is a fallback.
    ptr = pointer or manifest_name(root)
    Store("drive").put(ptr, data, "application/gzip")
    try:
        B2().put(ptr, data, "application/gzip")  # an upload: not counted against Backblaze's download cap
    except Exception as e:  # noqa: BLE001
        print(f"  (Backblaze copy of the pointer not written: {e})", flush=True)
    _record(db, ptr, "drive", "search_manifest", len(data), title=f"Search index manifest (live; also on Backblaze): build {build}", rows=total)
    print(f"  manifest {ptr}: build {build}, {total:,} docs, {man['stats']['terms']:,} terms, "
          f"docs {man['stats']['bytes']['docs'] / 1e9:.2f} GB, postings {man['stats']['bytes']['postings'] / 1e9:.2f} GB, "
          f"manifest {len(data) / 1e6:.2f} MB", flush=True)
    if not keep_map:
        cleanup(db, st, root, build)
    return man


def copy_to_b2(pointer: str, new_pointer: str) -> None:
    """A copy of a built (Drive) index on Backblaze, to time the same queries on both stores (the sample only:
    the full index does not fit in Backblaze's free 10 GB)."""
    from .db import DB
    from .storage import B2
    db, b2, drive = DB(), B2(), Store("drive")
    man = json.loads(gzip.decompress(b2.get(pointer)))
    base = f"{man['root']}/{man['build']}"
    for f in man["files"]:
        data = drive.get(f"{base}/{f['n']}")
        name = f"{base}/{f['n']}"
        b2.put(name, data)
        _record(db, name, "b2", "search_postings" if f["n"].startswith("post/") else "search_docs", len(data),
                title=f"Search index {man['build']}: {f['n']} (Backblaze copy, for timing)")
        f["h"] = name
    man["store"] = "b2"
    data = gzip.compress(json.dumps(man, separators=(",", ":")).encode(), 9)
    b2.put(new_pointer, data, "application/gzip")
    _record(db, new_pointer, "b2", "search_manifest", len(data), title=f"Search index manifest (Backblaze copy): build {man['build']}", rows=man["docs"])
    print(f"  copied {len(man['files'])} files to Backblaze; manifest {new_pointer}")


def cleanup(db, st: Store, root: str, build: str) -> None:
    """The build's term lists (needed only to build) and every build older than the one before this go."""
    rows = db.select("library_files", f"select=path,updated_at&item=eq.{ITEM}&path=like.{root}/b*&order=path")
    builds = sorted({r["path"][len(root) + 1:].split("/")[0] for r in rows})
    keep = set(b for b in builds if b <= build)
    keep = set(sorted(keep)[-2:]) | {b for b in builds if b > build}
    for r in rows:
        b = r["path"][len(root) + 1:].split("/")[0]
        rel = r["path"][len(root) + 1 + len(b) + 1:]
        if b not in keep or (b == build and rel.startswith("map/")):
            st.delete(r["path"])
            db.http.delete("/library_files", params={"path": f"eq.{r['path']}"})


# ================================================================ status

def status(root: str, build: str | None) -> None:
    from .db import DB
    db = DB()
    rows = db.select("library_files", f"select=path,kind,bytes,rows,updated_at&item=eq.{ITEM}&path=like.{root}/*&order=path")
    builds = sorted({r["path"][len(root) + 1:].split("/")[0] for r in rows if r["path"][len(root) + 1:].startswith("b")})
    for b in ([build] if build else builds):
        mine = [r for r in rows if r["path"].startswith(f"{root}/{b}/")]
        units = sum(1 for r in mine if re.search(r"/meta/u\d+\.json$", r["path"]))
        parts = sum(1 for r in mine if re.search(r"/meta/p\d+\.json$", r["path"]))
        planned = next((r["rows"] for r in mine if r["path"].endswith("/plan.json")), "?")
        docs = sum(r["rows"] or 0 for r in mine if r["kind"] == "search_docs")
        size = sum(r["bytes"] or 0 for r in mine if r["kind"] in ("search_docs", "search_postings"))
        man = any(r["path"].endswith("/manifest.json.gz") for r in mine)
        last = max((r["updated_at"] for r in mine), default="-")
        print(f"  {b}: units {units}/{planned}, parts {parts}/{PARTS}, {docs:,} docs, {size / 1e9:.2f} GB, "
              f"manifest {'yes' if man else 'no'}, last write {last[:19]}")
    for r in rows:
        if r["path"].endswith("manifest.json.gz") and not r["path"][len(root) + 1:].startswith("b"):
            print(f"  live pointer {r['path']}: {r['rows']:,} docs, written {r['updated_at'][:19]}")


# ================================================================ reading (the local query tool; same steps as the website's reader)

class Index:
    """A built index read the way the website reads it: manifest once, then byte ranges (cached)."""

    def __init__(self, manifest: bytes, store: Store | None = None) -> None:
        self.m = json.loads(gzip.decompress(manifest))
        self.st = store or Store(self.m["store"])
        self.files = self.m["files"]
        self.dir = array("I")
        self.dir.frombytes(base64.b64decode(self.m["dir"]))
        self.idir = array("I")
        self.idir.frombytes(base64.b64decode(self.m["idir"]))
        self.bases = [u["base"] for u in self.m["units"]]
        self.offsets = []
        for u in self.m["units"]:
            bl = array("I")
            bl.frombytes(base64.b64decode(u["bl"]))
            o, offs = 0, []
            for x in bl:
                offs.append(o)
                o += x
            offs.append(o)
            self.offsets.append(offs)
        self.cache: dict = {}
        self.reads = 0
        self.bytes = 0

    # -- raw reads
    def read(self, f: int, start: int, length: int) -> bytes:
        k = (f, start, length)
        if k not in self.cache:
            self.cache[k] = self.st.read(self.files[f]["h"], start, length)
            self.reads += 1
            self.bytes += length
        return self.cache[k]

    def reads_parallel(self, reqs: list[tuple[int, int, int]]) -> list[bytes]:
        todo = list({r for r in reqs if r not in self.cache})
        if todo:
            with ThreadPoolExecutor(16) as pool:
                list(pool.map(lambda r: self.read(*r), todo))
        return [self.cache[r] for r in reqs]

    # -- dictionaries
    def dictionary(self, bucket: int) -> dict[str, list]:
        k = ("dict", bucket)
        if k not in self.cache:
            o, ln = self.dir[bucket * 2], self.dir[bucket * 2 + 1]
            raw = self.read(self.m["parts"][bucket % PARTS], o, ln) if ln else b""
            d = {}
            for line in zlib.decompress(raw).decode().split("\n") if raw else []:
                x = line.split("\t")
                d[x[0]] = x[1:]
            self.cache[k] = d
        return self.cache[k]

    def term(self, t: str) -> dict | None:
        b = crc(t) % NB
        x = self.dictionary(b).get(t)
        if not x:
            return None
        df = int(x[0])
        if x[1].startswith("="):
            return {"t": t, "df": df, "ids": [int(i) for i in x[1][1:].split(",")]}
        off, ln, mx = int(x[1]), int(x[2]), int(x[3])
        firsts, offs, f, o = [], [], 0, 0
        for i, s in enumerate(x[4].split(",")):
            if i == 0:
                f, o = int(s), 0
            else:
                a, b2 = s.split(".")
                f, o = f + int(a), o + int(b2)
            firsts.append(f)
            offs.append(o)
        offs.append(ln)
        return {"t": t, "df": df, "part": self.m["parts"][b % PARTS], "off": off, "len": ln, "max": mx, "firsts": firsts, "offs": offs}

    def block(self, info: dict, i: int) -> list[int]:
        if "ids" in info:
            return info["ids"]
        raw = self.read(info["part"], info["off"] + info["offs"][i], info["offs"][i + 1] - info["offs"][i])
        return decode(raw)

    def blocks_for(self, info: dict, ids: list[int]) -> list[int]:
        """The blocks of a term that could hold these ids."""
        if "ids" in info:
            return [0]
        need = set()
        for x in ids:
            i = bisect.bisect_right(info["firsts"], x) - 1
            if i >= 0:
                need.add(i)
        return sorted(need)

    def fetch_blocks(self, wants: list[tuple[dict, list[int]]]) -> bool:
        """Reads every wanted block not yet cached in one parallel round; neighbouring blocks of a term (gap of at most
        2) are read as one range. True when anything was read."""
        reqs = []
        for info, blocks in wants:
            if "ids" in info or not blocks:
                continue
            blocks = [i for i in blocks if (info["part"], info["off"] + info["offs"][i], info["offs"][i + 1] - info["offs"][i]) not in self.cache]
            if not blocks:
                continue
            run = [blocks[0], blocks[0]]
            for b in blocks[1:] + [None]:
                if b is not None and b - run[1] <= 2:
                    run[1] = b
                    continue
                s, e = info["offs"][run[0]], info["offs"][run[1] + 1]
                reqs.append((info["part"], info["off"] + s, e - s, info, run[0], run[1]))
                if b is not None:
                    run = [b, b]
        raw = self.reads_parallel([r[:3] for r in reqs])
        for (f, start, ln, info, b0, b1), data in zip(reqs, raw):
            for i in range(b0, b1 + 1):
                s = info["offs"][i] - info["offs"][b0]
                e = info["offs"][i + 1] - info["offs"][b0]
                self.cache[(info["part"], info["off"] + info["offs"][i], e - s)] = data[s:e]
        return bool(reqs)

    # -- documents
    def docs(self, ids: list[int]) -> dict[int, dict]:
        want = {}
        for d in ids:
            u = bisect.bisect_right(self.bases, d) - 1
            local = d - self.bases[u]
            want.setdefault((u, local // DOCBLOCK), []).append((d, local))
        # neighbouring blocks of a unit (gap of at most 4) are read as one range
        runs, reqs = [], []
        for u in sorted({u for u, _ in want}):
            bs = sorted(b for uu, b in want if uu == u)
            run = [bs[0], bs[0]]
            for b in bs[1:] + [None]:
                if b is not None and b - run[1] <= 4:
                    run[1] = b
                    continue
                o = self.offsets[u]
                runs.append((u, run[0], run[1]))
                reqs.append((self.m["units"][u]["f"], o[run[0]], o[run[1] + 1] - o[run[0]]))
                if b is not None:
                    run = [b, b]
        raws = self.reads_parallel(reqs)
        out = {}
        for (u, b0, b1), raw in zip(runs, raws):
            o = self.offsets[u]
            for b in range(b0, b1 + 1):
                if (u, b) not in want:
                    continue
                lines = zlib.decompress(raw[o[b] - o[b0]:o[b + 1] - o[b0]]).decode().split("\n")
                for d, local in want[(u, b)]:
                    out[d] = json.loads(lines[local % DOCBLOCK])
        return out

    def lookup(self, key: str) -> list[int]:
        b = crc(key) % NIB
        o, ln = self.idir[b * 2], self.idir[b * 2 + 1]
        if not ln:
            return []
        k = ("ids", b)
        if k not in self.cache:
            raw = self.read(self.m["parts"][b % PARTS], o, ln)
            self.cache[k] = dict(line.split("\t", 1) for line in zlib.decompress(raw).decode().split("\n") if line)
        v = self.cache[k].get(key)
        return [int(x) for x in v.split(",")] if v else []


def decode(raw: bytes) -> list[int]:
    out, v, shift, prev, first = [], 0, 0, 0, True
    for byte in raw:
        v |= (byte & 0x7F) << shift
        if byte & 0x80:
            shift += 7
            continue
        prev = v if first else prev + v
        first = False
        out.append(prev)
        v, shift = 0, 0
    return out


def ranges(ix: Index, level: str | None, y_from: int | None, y_to: int | None) -> list[tuple[str, int, int]]:
    """Doc-id ranges to search, one per level, in priority order."""
    out = []
    for lv in LEVELS:
        if level and lv != level or lv not in ix.m["levels"]:
            continue
        ys = [(int(y), r) for y, r in ix.m["years"][lv].items() if (y_from is None or int(y) >= y_from) and (y_to is None or int(y) <= y_to)]
        if not ys:
            continue
        out.append((lv, min(r[0] for _, r in ys), max(r[1] for _, r in ys)))
    return out


def overlapping(info: dict, a: int, b: int) -> list[int]:
    """Blocks of a term overlapping ids [a, b]."""
    if "ids" in info:
        return [0]
    f = info["firsts"]
    return list(range(max(0, bisect.bisect_right(f, a) - 1), bisect.bisect_right(f, b)))


def intersect(ix: Index, infos: list[dict], lo: int, hi: int, need: int, max_rounds: int = 6) -> tuple[list[int], int, bool]:
    """The first `need` ids in [lo, hi] that every term has, an estimated total, and whether that total is exact.
    The rarest term drives; candidates are checked in chunks small enough that each other term needs few blocks.
    (Same steps as intersect() in web/src/lib/search/engine.ts.)"""
    def n_in(info):
        if "ids" in info:
            return sum(1 for x in info["ids"] if lo <= x <= hi)
        f = info["firsts"]
        a, b = bisect.bisect_right(f, lo) - 1, bisect.bisect_right(f, hi)
        return max(0, b - max(a, 0)) * BLOCK if len(f) > 1 else info["df"]
    infos = sorted(infos, key=n_in)
    driver, rest = infos[0], infos[1:]
    blocks = [0] if "ids" in driver else overlapping(driver, lo, hi)
    hits: list[int] = []
    checked = rounds = nxt = 0
    pending: list[int] = []
    while rounds < max_rounds:
        if not pending:
            if nxt >= len(blocks):
                break
            bi = blocks[nxt]
            nxt += 1
            # the other terms' blocks for this driver block's id window, read with it when they are few
            a = max(lo, driver["firsts"][bi] if "firsts" in driver else min(driver["ids"]))
            if "ids" in driver:
                b = min(hi, driver["ids"][-1])
            else:
                b = min(hi, driver["firsts"][bi + 1] - 1 if bi + 1 < len(driver["firsts"]) else driver["max"])
            spec = [(info, ob) for info in rest for ob in [overlapping(info, a, b)] if len(ob) <= 8]
            if ix.fetch_blocks([(driver, [bi])] + spec):
                rounds += 1
            pending = [x for x in ix.block(driver, bi) if lo <= x <= hi]
            if not pending:
                continue
        chunk = pending[:256]
        while True:
            needs = [(info, ix.blocks_for(info, chunk)) for info in rest]
            if len(chunk) <= 16 or all(len(bs) <= 48 for _, bs in needs):
                break
            chunk = chunk[: len(chunk) // 2]
        if ix.fetch_blocks(needs):
            rounds += 1
        pending = pending[len(chunk):]
        checked += len(chunk)
        cand = chunk
        for info, bs in needs:
            if not cand:
                break
            have = set()
            for b2 in bs:
                have.update(ix.block(info, b2))
            cand = [x for x in cand if x in have]
        hits += cand
        if len(hits) >= need:
            break
    if not pending and nxt >= len(blocks):
        return hits, len(hits), True
    est = round(len(hits) / max(checked, 1) * n_in(driver))
    return hits, max(est, len(hits)), False


WEIGHTS = [("parties", ("ti", "p", "r"), 3), ("acts", ("a",), 2), ("case", ("n", "ty", "nc", "ci"), 2),
           ("court", ("c", "st", "di", "cx"), 1), ("judges", ("j",), 1), ("text", ("s", "sg", "x"), 1)]


def field_terms(rec: dict) -> list[tuple[str, set, int]]:
    """The record's searchable terms by field group (for 'why' and the score)."""
    out = []
    for name, keys, w in WEIGHTS:
        have: set[str] = set()
        for k in keys:
            v = rec.get(k)
            if not v:
                continue
            have.update(text_terms(str(v)))
            if k == "a":  # an acts field lists bare section numbers ('NEGOTIABLE INSTRUMENTS ACT 138')
                have.update(_sec_terms(" ".join(re.findall(r"\b[0-9]{1,4}(?:-?[a-z]\b)?", fold(str(v))))))
            if k in ("n", "ty"):
                pc = parse_case(str(v))
                if pc:
                    have.update(case_terms(pc[0], pc[1], pc[2])[0])
                have.add("t:" + re.sub(r"no$", "", compact(str(v))))
        out.append((name, have, w))
    return out


def why(rec: dict, terms: list[str]) -> tuple[list[str], int]:
    """Which fields the words matched in (parties, acts, case, court, judges, text) and the score: parties x3,
    acts/sections and case number x2, the rest x1. A word found in no shown field matched the full indexed text."""
    fields: list[str] = []
    score = 0
    groups = field_terms(rec)
    for t in terms:
        if t.startswith(("c:", "s:")):
            continue
        best = next(((name, w) for name, have, w in groups if t in have), None)
        name, w = best or ("text", 1)
        score += w
        if name not in fields:
            fields.append(name)
    return fields, score


def search(ix: Index, q: str, level: str | None = None, court: str | None = None, state: str | None = None,
           y_from: int | None = None, y_to: int | None = None, page: int = 1, size: int = 25) -> dict:
    pq = parse_query(q)
    terms = list(pq["terms"])
    if court:
        terms.append(f"c:{cid(court)}")
    if state:
        terms.append(f"s:{cid(state)}")
    # round 1: every dictionary bucket and exact-lookup bucket the query needs, at once
    keys = pq["cnrs"] + pq["keys"]
    reqs = []
    for t in terms:
        b = crc(t) % NB
        if ix.dir[b * 2 + 1]:
            reqs.append((ix.m["parts"][b % PARTS], ix.dir[b * 2], ix.dir[b * 2 + 1]))
    for k in keys:
        b = crc(k) % NIB
        if ix.idir[b * 2 + 1]:
            reqs.append((ix.m["parts"][b % PARTS], ix.idir[b * 2], ix.idir[b * 2 + 1]))
    ix.reads_parallel(reqs)
    exact: list[int] = []
    for k in keys:
        exact += ix.lookup(k)
    rs = ranges(ix, level, y_from, y_to)
    exact = [d for d in exact if any(lo <= d <= hi for _, lo, hi in rs)]
    need = max(50, page * size)
    per_level: dict[str, dict] = {}
    hits_by: dict[str, list[int]] = {}
    infos = [ix.term(t) for t in terms] if terms else []
    if terms and all(infos):
        # each level's id range searched at once (as the website does)
        with ThreadPoolExecutor(4) as pool:
            got = list(pool.map(lambda r: intersect(ix, infos, r[1], r[2], need), rs))
        for (lv, _, _), (h, est, exact_n) in zip(rs, got):
            hits_by[lv] = h
            per_level[lv] = {"count": est, "exact": exact_n}
    ordered: list[int] = []
    seen = set()
    for d in sorted(set(exact)):
        seen.add(d)
        ordered.append(d)
    lv_of = {}
    for lv, _, _ in rs:
        for d in hits_by.get(lv, []):
            if d not in seen:
                seen.add(d)
                ordered.append(d)
                lv_of[d] = lv
    window = ordered[: need]
    recs = ix.docs(window) if window else {}
    scored = []
    for i, d in enumerate(window):
        r = recs[d]
        f, s = why(r, pq["terms"])
        if d in exact:
            f, s = ["exact"] + f, 100
        scored.append((0 if d in exact else 1, LEVELS.index(r["l"]), -s, i, d, f, s))
    # exact lookups first; then levels in priority order, best match first within a level
    scored.sort(key=lambda x: x[:4])
    if not per_level:
        for lv, lo, hi in rs:
            n = sum(1 for d in set(exact) if lo <= d <= hi)
            if n:
                per_level[lv] = {"count": n, "exact": True}
    pagehits = scored[(page - 1) * size: page * size]
    return {"q": q, "terms": pq["terms"], "keys": pq["keys"] + pq["cnrs"], "levels": per_level,
            "hits": [{"id": x[4], "why": x[5], "score": x[6], **recs[x[4]]} for x in pagehits]}


def lookup_cnr(ix: Index, cnr: str) -> list[dict]:
    k = cnr_key(cnr)
    if not k:
        return []
    ids = ix.lookup(k)
    recs = ix.docs(ids) if ids else {}
    return [{"id": d, **recs[d]} for d in ids]


BENCH = ["anticipatory bail 438", "section 138 negotiable instruments", "ICICI Lombard", "motor accident compensation",
         "United India Insurance", "UPHC015892432025", "BAIL/38919/2025", "दहेज", "u/s 438 crpc", "section 138 of NI Act",
         "demonetisation", "Union of India nursing allowance", "Tamil Nadu Generation and Distribution Corporation",
         "dowry death 304B", "quashing of FIR 482", "writ petition service termination", "electricity tariff appeal",
         "Shriram Finance", "land acquisition compensation", "रिट याचिका"]


def read_manifest(name: str) -> bytes:
    """The manifest pointer: from Drive (no download cap), else its Backblaze copy."""
    raw = Store("drive").get(name)
    if raw is None:
        from .storage import B2
        raw = B2().get(name)
    if raw is None:
        raise RuntimeError(f"no manifest at {name}")
    return raw


def bench(manifest: str, store_kind: str | None, queries: list[str] | None = None) -> list[dict]:
    out = []
    t0 = time.time()
    raw = read_manifest(manifest)
    tm = time.time() - t0
    ix = Index(raw, Store(store_kind) if store_kind else None)
    print(f"  manifest {manifest} ({len(raw) / 1e6:.2f} MB) read in {tm * 1000:.0f} ms; store {ix.st.kind}; build {ix.m['build']}")
    for q in queries or BENCH:
        row = {"q": q}
        for phase in ("cold", "warm"):
            if phase == "cold":
                ix.cache = {}
            r0, t = ix.reads, time.time()
            res = search(ix, q)
            row[phase] = round((time.time() - t) * 1000)
            row[phase + "_reads"] = ix.reads - r0
        top = res["hits"][0] if res["hits"] else None
        row["hits"] = {k: (v["count"], v["exact"]) for k, v in res["levels"].items()}
        row["top"] = f"[{top['l']}] {(top.get('ti') or ' v '.join(x for x in (top.get('p'), top.get('r')) if x) or '')[:60]} | {top.get('n') or ''} | {','.join(top['why'])}" if top else "-"
        out.append(row)
        print(f"  {q[:40]:40} cold {row['cold']:>6} ms ({row['cold_reads']:>3} reads)  warm {row['warm']:>5} ms  "
              f"hits {row['hits']}  top {row['top']}", flush=True)
    for phase in ("cold", "warm"):
        xs = sorted(r[phase] for r in out)
        print(f"  {phase}: p50 {xs[len(xs) // 2]} ms, p90 {xs[min(len(xs) - 1, int(len(xs) * 0.9))]} ms")
    return out


# ================================================================ main

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["plan", "docs", "postings", "manifest", "status", "query", "cnr", "bench", "sample", "sample-docs", "mirror"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--build")
    ap.add_argument("--root", default="search/v1")
    ap.add_argument("--store", default="drive", choices=["drive", "b2"])
    ap.add_argument("--sample", action="store_true")
    ap.add_argument("--with-docs", action="store_true", help="plan: also the docs level (circulars, notifications, bills, …)")
    ap.add_argument("--manifest", help="the Backblaze name of the manifest to read (default <root>/manifest.json.gz)")
    ap.add_argument("--level", choices=LEVELS)
    ap.add_argument("--court")
    ap.add_argument("--state")
    ap.add_argument("--from", dest="y_from", type=int)
    ap.add_argument("--to", dest="y_to", type=int)
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--github-output", action="store_true", help="plan: write build/units/parts for GitHub Actions")
    ap.add_argument("--save", help="bench: also write the timings to this Backblaze name")
    a = ap.parse_args()
    if a.cmd == "plan":
        p = plan(a.build, a.root, a.store, a.sample, docs=a.with_docs)
        print(json.dumps({"build": p["build"], "units": p["todo"], "parts": p["parts_todo"]}))
        if a.github_output and os.environ.get("GITHUB_OUTPUT"):
            # a part is built again whenever any unit is (re)built in this run
            parts = list(range(PARTS)) if p["todo"] else p["parts_todo"]
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
                fh.write(f"build={p['build']}\nunits={json.dumps(p['todo'])}\nparts={json.dumps(parts)}\n")
        return 0
    if a.cmd == "docs":
        run_docs(a.build, a.root, a.store, int(a.arg))
        return 0
    if a.cmd == "postings":
        run_postings(a.build, a.root, a.store, int(a.arg))
        return 0
    if a.cmd == "manifest":
        run_manifest(a.build, a.root, a.store)
        return 0
    if a.cmd == "status":
        status(a.root, a.build)
        return 0
    if a.cmd == "sample-docs":
        # the docs level alone, from Drive-stored files (a few of each kind unless --build carries a full plan)
        root = "search/sample-docs"
        p = plan(a.build, root, "drive", True, only_docs=True)
        for i in p["todo"]:
            run_docs(p["build"], root, "drive", i, p)
        for i in range(PARTS):
            run_postings(p["build"], root, "drive", i)
        run_manifest(p["build"], root, "drive", pointer=f"{root}/manifest-drive.json.gz")
        print("  left for after the demo (Backblaze):", "; ".join(DOCS_LATER))
        return 0
    if a.cmd == "sample":
        root = "search/sample"
        p = plan(a.build, root, a.store, True)
        for i in p["todo"]:
            run_docs(p["build"], root, a.store, i, p)
        for i in range(PARTS):
            run_postings(p["build"], root, a.store, i)
        run_manifest(p["build"], root, a.store, pointer=f"{root}/manifest-{a.store}.json.gz")
        if a.store == "drive":
            copy_to_b2(f"{root}/manifest-drive.json.gz", f"{root}/manifest-b2.json.gz")
        return 0
    if a.cmd == "mirror":
        mirror(float(a.arg or 700))
        return 0
    name = a.manifest or manifest_name(a.root)
    if a.cmd == "bench":
        rows = bench(name, None)
        if a.save:
            from .db import DB
            data = json.dumps({"manifest": name, "at": _now(), "where": "github-actions" if os.environ.get("GITHUB_ACTIONS") else "local",
                               "rows": rows}, ensure_ascii=False).encode()
            Store("drive").put(a.save, data, "application/json")
            _record(DB(), a.save, "drive", "search_bench", len(data), title=f"Search timing: {name}", rows=len(rows))
        return 0
    ix = Index(read_manifest(name))
    if a.cmd == "cnr":
        for r in lookup_cnr(ix, a.arg):
            print(json.dumps(r, ensure_ascii=False))
        return 0
    t = time.time()
    res = search(ix, a.arg, a.level, a.court, a.state, a.y_from, a.y_to, a.page)
    print(f"terms {res['terms']} keys {res['keys']} | {ix.reads} reads, {(time.time() - t) * 1000:.0f} ms | levels {res['levels']}")
    for h in res["hits"]:
        print(f"  [{h['l']} {h.get('y')}] {h.get('ti') or (h.get('p') or '') + ' v ' + (h.get('r') or '')} | {h.get('n') or ''} | "
              f"{h.get('c') or ''} | why {','.join(h['why'])} | {(h.get('s') or h.get('a') or '')[:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
