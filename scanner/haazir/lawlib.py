"""The core law that drafts read (Phase 6, reports/Library storage plan.md), written to Backblaze as files, with each
file listed in Supabase (library_files). Sources: reports/Drafting sources.md.

    python -m haazir.lawlib acts [--only "Motor Vehicles Act, 1988" ...]   # item 4: every central act, section by section
    python -m haazir.lawlib tables                                         # item 8: IPC/CrPC/Evidence <-> BNS/BNSS/BSA (NCRB)
    python -m haazir.lawlib whole [--only IPC CrPC "Evidence Act" Constitution]   # items 5, 7: acts kept as one PDF
    python -m haazir.lawlib cpc                                            # item 9: CPC Orders and Rules
    python -m haazir.lawlib schedules                                      # item 9: schedules of central acts
    python -m haazir.lawlib formats                                        # item 10: appendices (CPC model pleadings) and forms
    python -m haazir.lawlib gap-acts [--only NAME|ID ...]                  # item 4: acts India Code has no sections for, from their PDFs

India Code in bulk (haazir.ic_load: original PDFs kept in zips, text per document, resumable):
    python -m haazir.lawlib rules                                          # item 24: central Rules and Regulations
    python -m haazir.lawlib repealed                                       # item 25: repealed central acts
    python -m haazir.lawlib notifications [--only Notification Circular …] # item 22: notifications, circulars, orders, ordinances …
    python -m haazir.lawlib constitution-amendments                        # item 27: Constitution editions and amending acts
    python -m haazir.lawlib hindi-acts                                     # item 26: Hindi copies of central acts
    python -m haazir.lawlib state-acts [--only Maharashtra …]              # item 18: state Acts, Rules and sections
    python -m haazir.lawlib fees-stamps                                    # item 9: which states have a court-fees and a stamp act
    python -m haazir.lawlib state-gaps                                     # item 18: India Code state acts missing from acts/state
    python -m haazir.lawlib ic-check PREFIX [--n 3]                        # counts, sizes and 3 random documents vs India Code
  (add --fresh to list a collection again instead of using the cached listing)
"""

import argparse
import re
import sys
import time
from datetime import datetime, timezone

import httpx

from .acts import ACTS
from .db import DB
from .library import _ic_search, _md, _norm_title, _text, clean
from .storage import B2, pack, unpack

INDIA_CODE = "India Code (indiacode.gov.in), Legislative Department"
UA = {"User-Agent": "Haazir library (+https://haazir-pi.vercel.app)"}
SHORT = {_norm_title(full): short for short, full, _ in ACTS}  # Haazir's short names for the acts it already knows


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(db: DB, path: str, store: str, kind: str, item: int, data: bytes, **kw) -> None:
    db.insert("library_files", {"path": path, "store": store, "kind": kind, "item": item, "bytes": len(data), "updated_at": now(), **kw},
              upsert_on="path")


def central_acts(client: httpx.Client) -> list[dict]:
    """Every central act India Code lists (848 on 26 Oct 2026)."""
    out, page = [], 0
    while True:
        total, items = _ic_search(client, "dc.identifier.state_name:CENTRAL", "ACT", page=page)
        out += [it for it in items if _md(it, "dc.identifier.state_name").upper() == "CENTRAL"]
        page += 1
        if page * 100 >= total or not items:
            return out
        time.sleep(1)


def sections_of(client: httpx.Client, act_id: str) -> list[dict]:
    rows, page = [], 0
    while True:
        total, items = _ic_search(client, f'dc.identifier.act_id:"{act_id}"', "SECTION", page=page)
        for it in items:
            if _md(it, "dc.identifier.act_id") != act_id:
                continue
            rows.append({"number": _md(it, "dc.identifier.section_number").strip() or "?", "title": clean(_md(it, "dc.title")) or None,
                         "body": _text(_md(it, "dc.identifier.section_page_note")) or None,
                         "notes": _text(_md(it, "dc.identifier.section_footnote")) or None,
                         "order": float(_md(it, "dc.identifier.order_number") or 0)})
        page += 1
        if page * 100 >= total or not items:
            break
        time.sleep(0.5)
    rows.sort(key=lambda r: r["order"])
    for i, r in enumerate(rows, start=1):
        r["n"] = i
        del r["order"]
    return rows


def load_acts(only: list[str] | None = None) -> None:
    db, b2 = DB(), B2()
    with httpx.Client(follow_redirects=True, headers=UA) as client:
        acts = central_acts(client)
        print(f"{len(acts)} central acts on India Code", flush=True)
        index = []
        for it in acts:
            title = clean(it.get("name") or _md(it, "dc.title"))
            name = re.sub(r"^\s*the\s+", "", title, flags=re.I)
            if only and name not in only:
                continue
            act_id = _md(it, "dc.identifier.act_id")
            year = re.search(r"(1[789]\d\d|20\d\d)\s*$", name)
            meta = {"id": act_id, "name": name, "short": SHORT.get(_norm_title(name)), "year": int(year.group(1)) if year else None,
                    "number": _md(it, "dc.identifier.act_number") or None, "ministry": _md(it, "dc.identifier.ministry_name") or None,
                    "long_title": clean(_md(it, "dc.title.long_title")) or None, "enacted": _md(it, "dc.date.enact_date") or None,
                    "source": INDIA_CODE, "read_at": now()}
            secs = sections_of(client, act_id)
            path = f"acts/central/{act_id}.json.gz"
            data = pack([{**meta, "sections": secs}])
            b2.put(path, data, "application/gzip")
            record(db, path, "b2", "act", 4, data, title=name, act=meta["short"] or name, rows=len(secs), source=INDIA_CODE)
            index.append({**{k: meta[k] for k in ("id", "name", "short", "year", "number", "ministry")}, "sections": len(secs), "path": path})
            print(f"  {name}: {len(secs)} sections", flush=True)
        if not only:
            data = pack(sorted(index, key=lambda a: a["name"].lower()))
            b2.put("acts/central/index.jsonl.gz", data, "application/gzip")
            record(db, "acts/central/index.jsonl.gz", "b2", "act_index", 4, data, title="Central acts", rows=len(index), source=INDIA_CODE)
        print(f"done: {len(index)} acts, {sum(a['sections'] for a in index):,} sections", flush=True)


# ---------------------------------------------------------------- items 5 and 7: acts kept as one PDF (IPC, CrPC, Evidence, Constitution)

PDFTOTEXT = next((p for p in (r"C:\Program Files\Git\mingw64\bin\pdftotext.exe", "/usr/bin/pdftotext") if __import__("os").path.exists(p)), "pdftotext")
# A section starts its line: "420. Cheating and dishonestly inducing delivery of property.—Whoever cheats …",
# also when added later inside brackets: "1[376D. Gang rape.—…". Articles of the Constitution the same way.
# The PDF's text often runs sections on from the line before, so a heading is looked for anywhere, and kept only when
# its number follows the one before (that drops "section 2. The \u2026" inside a text).
# A long title wraps onto the next line (the watermark may leave a stray letter there).
# Also: a title wrapped across a blank line, a title opening with a footnote bracket ("368. 1[Power of \u2026].\u2014"), and
# a heading right after the asterisks that mark omitted text.
HEAD = re.compile(r"(?:^|(?<=[\s\]\.;:*\uf02a]))(?:\d{1,2}\s*\[\s*)?(\d{1,4}[A-Z]{0,3})\.\s+((?:\d{1,2}\s*\[)?[A-Z\"\u201c\u2018'(][^\u2014\u2013\u2015\n]{1,250}?(?:\n\n?[^\u2014\u2013\u2015\n]{1,160}?)?)\s*[.:\]]*\s*[\u2014\u2013\u2015]+\s*", re.M)


INNER = re.compile(r"(?<![\d(])\d{1,3}[A-Z]{0,3}\.\s+[A-Z][a-z]")
SCHEDULES = re.compile(r"\n\s*(?:\d{1,2}\s*\[\s*)?THE\s+(?:FIRST\s+|SECOND\s+|THIRD\s+)?SCHEDULE\b")


def clean_block(s: str) -> str:
    return "\n".join(clean(ln) for ln in s.splitlines() if clean(ln))


def _fill_gaps(flat: str, heads: list[tuple]) -> list[tuple]:
    """A section whose heading has no dash ("86. Presumption as to … records. The Court may …"): looked for only
    between the two sections around its number, so a stray "86." elsewhere cannot be taken for it."""
    out = list(heads)
    for i in range(len(heads) - 1):
        a, b = _key(heads[i][2]), _key(heads[i + 1][2])
        if b[0] - a[0] < 2:
            continue
        lo, hi = heads[i][1], heads[i + 1][0]
        for n in range(a[0] + 1, b[0]):
            m = re.compile(rf"(?:^|(?<=[\s\]]))(?:\d{{1,2}}\s*\[\s*)?{n}\.\s+([A-Z][^.\n]{{2,200}}(?:\n[^.\n]{{1,120}})?)\.\s+(?=[A-Z(])").search(flat, lo, hi)
            if m:
                out.append((m.start(), m.end(), str(n), m.group(1)))
    return sorted(out, key=lambda h: h[0])


def _rising(cands: list[tuple]) -> list[tuple]:
    """The longest run of candidates (in text order) whose section numbers strictly rise, each step at most 40 on.
    Candidates at the same spot (other readings of one number) can't both be taken: they're offered high first."""
    import bisect
    n = len(cands)
    if not n:
        return []
    best = [1] * n   # length of the best run ending at i
    prev = [-1] * n
    keys = [_key(c[2]) for c in cands]
    # O(n²) is fine: an act has at most a few thousand heading-like spots.
    for i in range(n):
        for j in range(max(0, i - 400), i):
            if cands[j][0] < cands[i][0] and keys[j] < keys[i] and keys[i][0] - keys[j][0] <= 40 and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    i = max(range(n), key=lambda k: best[k])
    run = []
    while i != -1:
        run.append(cands[i])
        i = prev[i]
    return run[::-1]


def _key(number: str) -> tuple[int, str]:
    m = re.match(r"(\d+)([A-Z]*)", number)
    return int(m.group(1)), m.group(2)
# Footnotes print at the foot of each page: "1. Subs. by Act 26 of 1955, s. 117 …"; page numbers and India Code's mark.
FOOT = re.compile(r"^\s*\d{1,2}\.\s+(?:Subs\.|Ins\.|Omitted|Added|Rep\.|The words|Certain words|Now|See|Cf\.|Ss\.|S\.\s|Earlier|This|Vide|Renumbered|Sub-s|Cl\.|Clause|Section|Proviso|Explanation|Illustration\s*\(|Sec\.|Original)", re.I)
NOISE = re.compile(r"^\s*(?:IndiaCode|\d{1,4})\s*$")


def pdf_text(data: bytes) -> str:
    import subprocess, tempfile, os
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "a.pdf")
        open(src, "wb").write(data)
        subprocess.run([PDFTOTEXT, "-enc", "UTF-8", src, os.path.join(d, "a.txt")], check=True, capture_output=True)
        return open(os.path.join(d, "a.txt"), encoding="utf-8", errors="replace").read()


def split_sections(text: str) -> list[dict]:
    """A whole act's text -> its sections in order, each with its footnotes. The list of sections at the top of the
    act (no dash after the title) is skipped by the pattern itself."""
    return split_act(text)[0]


def split_act(text: str) -> tuple[list[dict], str | None]:
    """The sections, and the schedules printed after the last one (e.g. CrPC First Schedule: each offence,
    cognizable or not, bailable or not, triable by which court)."""
    body, notes = [], []
    lines = []
    for line in text.replace("\f", "\n").splitlines():
        if NOISE.match(line):
            continue
        if FOOT.match(line):
            notes.append((len(lines), clean(line)))
            continue
        lines.append(line)
    flat = "\n".join(lines)
    # Every heading-like spot, then the longest run of them whose numbers rise: the act's body. A few heading-like
    # lines in the list of sections at the top, or a "section 2." in a text, fall outside that run.
    cands, pos = [], 0
    while True:
        m = HEAD.search(flat, pos)
        if not m:
            break
        if INNER.search(m.group(2)):  # the "title" runs over another heading ("352. … 359. Kidnapping.—"): look again inside it
            pos = m.start() + 1
            continue
        number = m.group(1)
        # A footnote mark printed against the number ("1145." is s. 145 with footnote 1): the shorter readings are
        # offered too, the longer first, so the rising run can take only one of them.
        for cut in (0, 1, 2):
            rest = number[cut:]
            if cut and (len(number) <= cut + 1 or not rest[:1].isdigit() or rest.startswith("0")):
                continue
            cands.append((m.start(), m.end(), rest, m.group(2)))
        pos = m.end()
    heads = _rising(cands)
    heads = _fill_gaps(flat, heads)
    # Footnote positions are counted in lines: map each to the section it falls under.
    line_at = [0]
    for ln in lines:
        line_at.append(line_at[-1] + len(ln) + 1)
    out, schedules = [], None
    for i, (start, stop, number, title) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(flat)
        if i + 1 == len(heads):  # the schedules after the last section are not part of it
            sch = SCHEDULES.search(flat, stop)
            if sch:
                end, schedules = sch.start(), clean_block(flat[sch.start():])
        title = re.sub(r"^\d{1,2}\s*\[\s*", "", clean(title)).rstrip("].").strip()
        out.append({"n": i + 1, "number": number, "title": title,
                    "body": clean(flat[stop:end]).strip(" –—") or None, "notes": None, "_start": start, "_end": end})
    for ln, note in notes:
        pos = line_at[min(ln, len(line_at) - 1)]
        owner = next((s for s in reversed(out) if s["_start"] <= pos), None)
        if owner:
            owner["notes"] = (owner["notes"] + "\n" if owner["notes"] else "") + note
    for s in out:
        del s["_start"], s["_end"]
    return out, schedules


# Acts before about 1860 number their sections in Roman figures at the start of a line: "III. And it is hereby
# enacted …", "II. What is a public embankment within this Act.—The …". pdftotext often runs them on after the sentence
# before ("… as follows:— I. Sections …"), and India Code's watermark can leave a few of its letters first ("iaC III.").
# A number inside a sentence ("Section III. of Regulation XI. 1831") has no full stop or dash before it.
ROMAN_HEAD = re.compile(r"(?:^|(?<=[.:;—–-] )|(?<=[.:;—–-]))[ \t]*(?:(?:In|d|ia|iaC|o|C|de|e|Co|od|dia|aC|India|IndiaC|Code)[ \t]+)?"
                        r"([IVXLC]{1,8})\.[ \t]+(?=[A-Z\[(\"“])", re.M)


def _names_match(name: str, text: str) -> bool:
    """The PDF is this act: most words of the act's name are in the PDF's first lines (India Code has attached
    another act's PDF to some old acts)."""
    words = [w for w in re.findall(r"[a-z]{3,}", name.lower()) if w not in ("the", "and", "act")]
    head = re.sub(r"[^a-z]+", " ", text[:1500].lower())
    return bool(words) and sum(1 for w in words if w in head) >= 0.6 * len(words)


def roman_sections(text: str) -> list[dict]:
    """An old act's sections when they are numbered I, II, III …: the longest rising run of such headings."""
    lines = [ln for ln in text.replace("\f", "\n").splitlines() if not NOISE.match(ln) and not FOOT.match(ln)]
    flat = "\n".join(lines)
    cands = []
    for m in ROMAN_HEAD.finditer(flat):
        r = m.group(1)
        if not re.fullmatch(r"C{0,3}(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})", r) or not r:
            continue
        cands.append((m.start(), m.end(), str(_roman(r)), r))
    heads = _rising(cands)
    out = []
    for i, (start, stop, number, printed) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(flat)
        body = clean(flat[stop:end])
        t = re.match(r"([^—–―]{2,200}?)\s*[.:]?\s*[—–―]+\s*", body)
        title = clean(t.group(1)).rstrip(".") if t and not re.search(r"\b(?:enacted|whereas)\b", t.group(1), re.I) else None
        out.append({"n": i + 1, "number": number, "title": title, "body": (body[t.end():] if title else body) or None,
                    "notes": None, "printed": printed})
    return out


def gap_acts(only: list[str] | None = None) -> None:
    """Item 4 gap: the central acts India Code has no section records for (sections = 0 in the index), read from
    the act's own PDF with the splitter, and their act files rewritten in the same format."""
    from .ic_load import IC, hindi_name, md
    db, b2 = DB(), B2()
    index = unpack(b2.get("acts/central/index.jsonl.gz"))
    gaps = [r for r in index if (r["name"] in only or r["id"] in only)] if only else [r for r in index if r["sections"] == 0]
    print(f"{len(gaps)} central acts with no sections", flush=True)
    problems = []
    ic = IC()
    _, items = ic.listing(ic.tree()["CENTRAL"]["Acts"])
    by_id = {md(it, "dc.identifier.act_id"): it for it in items}
    for r in gaps:
        it = by_id.get(r["id"])
        pdfs = [b for b in (it or {}).get("files", {}).get("ORIGINAL", []) if b["name"].lower().endswith(".pdf") and not hindi_name(b["name"])]
        if not pdfs:
            print(f"  {r['name']}: no English PDF on India Code", flush=True)
            continue
        url = IC_FILE.format(pdfs[0]["uuid"])
        text = pdf_text(ic.content(pdfs[0]))
        if not _names_match(re.sub(r"\d{4}", "", r["name"]), text):
            problems.append(f"{r['name']}: India Code's PDF ({pdfs[0]['name']}) is another act: {clean(text[:80])!r}; left with no sections")
            act = unpack(b2.get(r["path"]))[0]
            act.update({"sections": [], "read_at": now(), "sections_from": None,
                        "note": f"India Code has no section records for this act, and its PDF ({url}) is of another act."})
            data = pack([act])
            b2.put(r["path"], data, "application/gzip")
            record(db, r["path"], "b2", "act", 4, data, title=r["name"], act=r.get("short") or r["name"], rows=0, source=INDIA_CODE)
            r["sections"] = 0
            continue
        sections, _ = split_act(text)
        old_style = roman_sections(text)

        def whole(secs: list[dict]) -> tuple[float, int]:  # share of section numbers 1..last that were found, then count
            nums = {_key(s["number"])[0] for s in secs}
            return (len(nums) / max(nums) if nums else 0.0, len(secs))

        how = "numbered"
        if whole(old_style) > whole(sections):
            sections, how = old_style, "roman"
        act = unpack(b2.get(r["path"]))[0]
        act.update({"sections": sections, "read_at": now(), "sections_from": "pdf", "source_url": url})
        data = pack([act])
        b2.put(r["path"], data, "application/gzip")
        record(db, r["path"], "b2", "act", 4, data, title=r["name"], act=r.get("short") or r["name"], rows=len(sections), source=INDIA_CODE)
        r["sections"] = len(sections)
        nums = [s["number"] for s in sections]
        print(f"  {r['name']}: {len(sections)} sections ({how}): {', '.join(nums)[:150]}", flush=True)
    for p in problems:
        print("  " + p, flush=True)
    if gaps:
        data = pack(index)
        b2.put("acts/central/index.jsonl.gz", data, "application/gzip")
        record(db, "acts/central/index.jsonl.gz", "b2", "act_index", 4, data, title="Central acts", rows=len(index), source=INDIA_CODE)


IC_FILE = "https://indiacode.gov.in/server/api/core/bitstreams/{}/content"
REPEALED = "Repealed from 1 July 2024 by the {}; still applies to offences, cases and proceedings before that day."
# Each: short name, full name, where the official PDF is, the item number in the plan, a note, and the sections that
# are rightly absent (repealed or omitted). Any other gap means the PDF was read wrongly, and nothing is saved.
WHOLE_TEXTS = [
    ("IPC", "Indian Penal Code, 1860", IC_FILE.format("337c7a03-60c6-4953-8dd6-90e65c343629"), 5, REPEALED.format("Bharatiya Nyaya Sanhita, 2023"),
     {13, 15, 16, 17, 56, 58, 59, 61, 62, 161, 162, 163, 164, 165, 226, 478, 480, 490, 492}, "acts/repealed/ipc.json.gz"),
    ("Evidence Act", "Indian Evidence Act, 1872", IC_FILE.format("4a3677bf-e488-4689-bf30-6ae29255a3ba"), 5, REPEALED.format("Bharatiya Sakshya Adhiniyam, 2023"),
     {2}, "acts/repealed/evidence-act.json.gz"),
    # India Code's current CrPC file is the 1974 text as enacted; this is the official India Code PDF as amended
    # (amendments to 2019), as kept by the Internet Archive.
    ("CrPC", "Code of Criminal Procedure, 1973",
     "https://web.archive.org/web/20251206113418id_/https://www.indiacode.nic.in/bitstream/123456789/13624/1/the_code_of_criminal_procedure%2C_1973.pdf",
     5, REPEALED.format("Bharatiya Nagarik Suraksha Sanhita, 2023") + " Text as amended up to 2019.", set(), "acts/repealed/crpc.json.gz"),
    ("Constitution", "Constitution of India", IC_FILE.format("2bd776ff-891e-444c-9344-f63df7b299fc"), 7, "As in India Code's 2026 English edition.",
     {232, 238, 242, 259, 272, 278, 291, 306, 314, 362, *range(379, 392)}, "constitution/constitution.json.gz"),
]


def load_whole_texts(only: list[str] | None = None) -> None:
    db, b2 = DB(), B2()
    with httpx.Client(follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (Haazir legal library)"}, timeout=300) as client:
        for short, name, url, item, note, absent, path in WHOLE_TEXTS:
            if only and short not in only:
                continue
            for attempt in range(4):
                r = client.get(url)
                if r.status_code == 200 and r.content[:4] == b"%PDF":
                    break
                time.sleep(30 * (attempt + 1))  # the Internet Archive asks callers to slow down
            else:
                raise RuntimeError(f"{short}: could not fetch the PDF ({r.status_code})")
            sections, schedules = split_act(pdf_text(r.content))
            found = {_key(s["number"])[0] for s in sections}
            top = max(found)
            gaps = {n for n in range(1, top + 1) if n not in found}
            if gaps != absent:
                raise RuntimeError(f"{short}: read wrongly, nothing saved. Missing but not repealed: {sorted(gaps - absent)}; "
                                   f"found though listed as repealed: {sorted(absent - gaps)}")
            unit = "articles" if short == "Constitution" else "sections"
            data = pack([{"id": short.lower().replace(" ", "-"), "name": name, "short": short, "note": note, "source_url": url,
                          "source": INDIA_CODE, "read_at": now(), unit: sections, "schedules": schedules}])
            b2.put(path, data, "application/gzip")
            record(db, path, "b2", "constitution" if short == "Constitution" else "act", item, data, title=name, act=short,
                   rows=len(sections), source=INDIA_CODE)
            print(f"  {name}: {len(sections)} {unit}, {len(absent)} repealed or omitted, schedules {len(schedules or '') // 1000} KB", flush=True)


# ---------------------------------------------------------------- items 9, 10: CPC orders and rules; schedules, appendices, forms

API = "https://indiacode.gov.in/server/api"


def _all(client: httpx.Client, collection: str, query: str = "dc.identifier.state_name:CENTRAL") -> list[dict]:
    out, page = [], 0
    while True:
        total, items = _ic_search(client, query, collection, page=page)
        out += items
        page += 1
        if page * 100 >= total or not items:
            return out
        time.sleep(0.5)


def _rule_no(title: str) -> tuple[int, str] | None:
    m = re.match(r"\s*Rule\s+(\d+)\s*([A-Z]{0,3})", title or "", re.I)
    return (int(m.group(1)), m.group(2).upper()) if m else None


ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}
CPC_PDF = IC_FILE.format("fc9dad3f-97a9-4216-843b-934665693468")  # India Code's PDF of the whole Code, as amended
# An Order's heading is in capitals ("ORDER XXXIX"); the text refers to them as "Order XXXIX".
ORDER = re.compile(r"(?<![A-Za-z])ORDER\s+([IVXLC]+)(?:\s*-?\s*([A-Z])\b)?")


def _roman(s: str) -> int:
    total = 0
    for i, ch in enumerate(s):
        v = ROMAN[ch]
        total += -v if i + 1 < len(s) and ROMAN[s[i + 1]] > v else v
    return total


ENTRY = re.compile(r"(?<![\d(\[])(\d{1,3}[A-Z]{0,2})\.\s+(\[?[A-Z][^\n]{2,220}?)\.(?=\s|$)")


def _checklist(block: str) -> list[tuple[str, str]]:
    """An Order's rules as its list of contents names them: [("1", "Who may be joined as plaintiffs"), …]. Omitted
    and repealed rules are left out (they have no text in the body)."""
    out, seen = [], set()
    for m in ENTRY.finditer(block):
        num, title = m.group(1), clean(m.group(2))
        if num in seen or re.match(r"\[?\s*(omitted|repealed)", title, re.I):
            seen.add(num)
            continue
        seen.add(num)
        out.append((num, title))
    return out


def split_by_checklist(block: str, expected: list[tuple[str, str]]) -> tuple[list[dict], list[str]]:
    """Rules of one Order, using its list of contents: headings the splitter found that are on the list, plus the
    listed ones it missed, found by their own title. Returns the rules and any listed rule still not found."""
    lines = [ln for ln in block.splitlines() if not NOISE.match(ln) and not FOOT.match(ln)]
    flat = "\n".join(lines)
    want = dict(expected)
    # The contents list is printed in jumbled columns, so it cannot decide alone: every heading the splitter finds
    # is kept, and the list only adds the rules it missed and corrects glued numbers.
    cands, pos = [], 0
    while True:
        m = HEAD.search(flat, pos)
        if not m:
            break
        if INNER.search(m.group(2)):
            pos = m.start() + 1
            continue
        for cut in (0, 1, 2):
            rest = m.group(1)[cut:]
            # "1[ 25." is rule 5 with footnote marks 1 and 2: read as one digit only when the list names that rule.
            if cut and (len(m.group(1)) <= cut or not rest[:1].isdigit() or rest.startswith("0") or (len(m.group(1)) <= cut + 1 and rest not in want)):
                continue
            cands.append((m.start(), m.end(), rest, m.group(2)))
        pos = m.end()
    heads = _rising(cands)
    # A glued number the list does not know ("13" where the list has 3): take the listed reading.
    for i, h in enumerate(heads):
        if h[2] not in want:
            for cut in (1, 2):
                if h[2][cut:] in want and h[2][cut:] not in {x[2] for x in heads}:
                    heads[i] = (h[0], h[1], h[2][cut:], h[3])
                    break
    have = {h[2] for h in heads}
    for num, title in expected:
        if num in have:
            continue
        words = r"\s+".join(re.escape(w) for w in re.sub(r"[\[\]]", "", title).split()[:5])
        m = re.search(rf"(?<![\d])(?:\d{{1,2}}\s*\[\s*)?{re.escape(num)}\.\s+(?:\d{{1,2}}\s*\[)?{words}[^—–―]{{0,200}}?[—–―]+", flat)
        if m:
            heads.append((m.start(), m.end(), num, title))
            have.add(num)
    heads.sort(key=lambda h: h[0])
    out = []
    for i, (s, e, num, title) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(flat)
        own = re.sub(r"^\d{1,2}\s*\[\s*", "", clean(title)).rstrip("].").strip()
        out.append({"n": i + 1, "number": num, "title": want.get(num) or own, "body": clean(flat[e:end]).strip(" –—") or None})
    return out, [n for n, _ in expected if n not in have]


def cpc_rules() -> None:
    """The CPC's First Schedule from the Code's own PDF: every Order (I to LI and the lettered ones such as XIII-A)
    and the Rules under it, in order. India Code's separate rule records are interleaved and incomplete, so the PDF
    is the source; each Order's rules are split like an act's sections."""
    db, b2 = DB(), B2()
    with httpx.Client(follow_redirects=True, headers=UA, timeout=300) as client:
        text = pdf_text(client.get(CPC_PDF).content)
        # Clean Order names from India Code's own Order records ("ORDER XXXIX:TEMPORARY INJUNCTIONS AND …").
        names = {}
        for o in _all(client, "SCHEDULEORDER"):
            if _md(o, "dc.identifier.act_id").startswith("AC_CEN_3_20_00051"):
                m = re.match(r"\s*ORDER\s+([IVXLC]+)\s*-?\s*([A-Z])?\s*:\s*(.+)", _md(o, "dc.title"))
                if m:
                    names[f"{_roman(m.group(1))}{m.group(2) or ''}"] = clean(m.group(3)).capitalize()
    lines = [ln for ln in text.replace("\f", "\n").splitlines() if not NOISE.match(ln)]
    flat = "\n".join(lines)
    marks = [m.start() for m in re.finditer(r"THE FIRST SCHEDULE", flat)]
    contents, start = marks[0], marks[-1]   # the list of contents, then the Schedule itself

    def orders_in(lo: int, hi: int) -> list[tuple]:
        return _rising([(m.start(), m.end(), f"{_roman(m.group(1))}{m.group(2) or ''}", m.group(0)) for m in ORDER.finditer(flat, lo, hi)])

    listed = {h[2]: h for h in orders_in(contents, start)}
    heads = orders_in(start, len(flat))
    if len(heads) < 50 or len(listed) < 50:
        raise RuntimeError(f"CPC: {len(heads)} Orders found ({len(listed)} in the contents); nothing saved")
    keys = [h[2] for h in orders_in(contents, start)]
    out, problems = [], []
    for i, (s, e, num, label) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(flat)
        block = flat[e:end]
        nxt = re.search(r"\n\s*(?:THE\s+)?(?:SECOND|APPENDIX)", block) if i + 1 == len(heads) else None
        block = block[:nxt.start()] if nxt else block
        # This Order's part of the list of contents is the checklist of its rules.
        if num in listed:
            k = keys.index(num)
            c_end = listed[keys[k + 1]][0] if k + 1 < len(keys) else start
            part = flat[listed[num][1]:c_end]
            tail = re.search(r"\bAPPENDI|\bSECOND SCHEDULE|\bTHIRD SCHEDULE", part)  # the last Order's list stops at the appendices
            expected = _checklist(part[:tail.start()] if tail else part)
        else:
            expected = []
        rules, missing = split_by_checklist(block, expected) if expected else (split_sections(block), [])
        if missing or not expected:
            problems.append(f"{clean(label)}: {'no list of contents' if not expected else 'not found: ' + ', '.join(missing)}")
        out.append({"order": clean(label), "number": num, "name": names.get(num), "rules": rules,
                    "checked": bool(expected) and not missing, "not_found": missing or None})
    if problems:
        print("  CPC: not complete against the list of contents:\n    " + "\n    ".join(problems), flush=True)
    data = pack([{"act": "CPC", "name": "Code of Civil Procedure, 1908 — First Schedule (Orders and Rules)", "source": INDIA_CODE,
                  "source_url": CPC_PDF, "read_at": now(), "orders": out}])
    data = pack([{"act": "CPC", "name": "Code of Civil Procedure, 1908 — First Schedule (Orders and Rules)", "source": INDIA_CODE,
                  "read_at": now(), "orders": out}])
    b2.put("cpc/orders-rules.json.gz", data, "application/gzip")
    record(db, "cpc/orders-rules.json.gz", "b2", "cpc_rules", 9, data, title="CPC Orders and Rules", act="CPC",
           rows=sum(len(o["rules"]) for o in out), source=INDIA_CODE)
    print(f"  CPC: {len(out)} Orders, {sum(len(o['rules']) for o in out)} Rules", flush=True)
    for o in out:
        print(f"    {o['order'][:14]:14s} {(o['name'] or '')[:40]:40s} {len(o['rules']):3d} rules: {', '.join(r['number'] for r in o['rules'])[:90]}", flush=True)


def _file_text(client: httpx.Client, item: dict) -> tuple[str | None, str | None]:
    """An India Code record's attached file as text: its text copy when whole, else the PDF read here."""
    b = client.get(f"{API}/core/items/{item['uuid']}/bundles").json()
    files = {}
    for bun in b.get("_embedded", {}).get("bundles", []):
        bs = client.get(bun["_links"]["bitstreams"]["href"]).json().get("_embedded", {}).get("bitstreams", [])
        files[bun["name"]] = bs
    for t in files.get("TEXT", []):
        if t.get("sizeBytes", 0) < 99_000:  # India Code cuts its text copies at about 100 KB: use the PDF beyond that
            return client.get(f"{API}/core/bitstreams/{t['uuid']}/content").text, t["uuid"]
    for p in files.get("ORIGINAL", []):
        if p["name"].lower().endswith(".pdf"):
            return pdf_text(client.get(f"{API}/core/bitstreams/{p['uuid']}/content").content), p["uuid"]
    return None, None


def attachments(collection: str, item: int, folder: str, kind: str) -> None:
    """Schedules (item 9), appendices and forms (item 10) of central acts: one file per act, its pieces in order."""
    db, b2 = DB(), B2()
    by_act: dict[str, dict] = {}
    with httpx.Client(follow_redirects=True, headers=UA, timeout=180) as client:
        records = _all(client, collection)
        print(f"  {collection}: {len(records)} records", flush=True)
        for n, it in enumerate(records, start=1):
            act_id = _md(it, "dc.identifier.act_id")
            text, file_id = _file_text(client, it)
            a = by_act.setdefault(act_id, {"act_id": act_id, "act": clean(_md(it, "dc.identifier.act_name")), "pieces": []})
            a["pieces"].append({"title": clean(_md(it, "dc.title")), "order": float(_md(it, "dc.identifier.order_number") or 0),
                                "text": clean_block(text) if text else None, "file": file_id})
            if n % 50 == 0:
                print(f"    {n}/{len(records)}", flush=True)
            time.sleep(0.3)
    for a in by_act.values():
        a["pieces"].sort(key=lambda p: p["order"])
        path = f"{folder}/{a['act_id']}.json.gz"
        data = pack([{**a, "kind": kind, "source": INDIA_CODE, "read_at": now()}])
        b2.put(path, data, "application/gzip")
        name = re.sub(r"^\s*the\s+", "", a["act"], flags=re.I)
        record(db, path, "b2", kind, item, data, title=f"{name}: {kind}s", act=SHORT.get(_norm_title(name)) or name, rows=len(a["pieces"]), source=INDIA_CODE)
    empty = sum(1 for a in by_act.values() for p in a["pieces"] if not p["text"])
    print(f"  {collection}: {len(by_act)} acts, {sum(len(a['pieces']) for a in by_act.values())} pieces, {empty} without text", flush=True)


# ---------------------------------------------------------------- item 8: old → new criminal law tables (NCRB)

NCRB = "https://www.ncrb.gov.in/uploads/SankalanPortal"
NCRB_SOURCE = "NCRB Sankalan portal (Ministry of Home Affairs)"
NUMBER = re.compile(r"^\s*(\d+[A-Z]{0,2})(?:\s*\.)?((?:\s*\(\s*\w+\s*\))*)")


def _side(text: str) -> dict:
    """'318. Cheating. 318(4)' -> section '318', its title, and the sub-section numbers named."""
    text = clean(text)
    m = NUMBER.match(text)
    subs = re.findall(r"\b(\d+[A-Z]{0,2}(?:\(\w+\))+)", text)
    return {"text": text, "section": m.group(1) if m else None, "parts": subs or None}


def criminal_tables() -> None:
    from bs4 import BeautifulSoup
    db, b2 = DB(), B2()
    out = {}
    with httpx.Client(follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}, timeout=180) as client:
        for page, new, old in (("SectionTableBNS", "BNS", "IPC"), ("SectionTableBNSS", "BNSS", "CrPC"), ("SectionTableBSA", "BSA", "Evidence Act")):
            soup = BeautifulSoup(client.get(f"{NCRB}/{page}.html").content, "html.parser")
            for t in soup.find_all("table"):
                rows = [[c.get_text(" ", strip=True) for c in r.find_all(["th", "td"])] for r in t.find_all("tr")]
                rows = [r for r in rows if len(r) == 2]
                if not rows:
                    continue
                left = rows[0][0].upper()
                direction = f"{new}->{old}" if new.split()[0] in left or "SANHITA" in left or "ADHINIYAM" in left else f"{old}->{new}"
                pairs = []
                for a, b in rows[1:]:
                    if a.upper().startswith(("CHAPTER", "PART")) and b.upper().startswith(("CHAPTER", "PART")):
                        pairs.append({"chapter": [clean(a), clean(b)]})
                        continue
                    pairs.append({"from": _side(a), "to": _side(b), "changed": "(Change)" in a + b or "New" in b})
                out[direction] = {"header": rows[0], "rows": pairs}
            done = ", ".join(f"{k} {len(v['rows'])}" for k, v in out.items() if new in k)
            print(f"  {page}: {done}", flush=True)
    data = pack([{"source": NCRB_SOURCE, "read_at": now(), "tables": out}])
    b2.put("tables/criminal-law.json.gz", data, "application/gzip")
    record(db, "tables/criminal-law.json.gz", "b2", "criminal_table", 8, data, title="Old and new criminal laws: section tables",
           rows=sum(len(v["rows"]) for v in out.values()), source=NCRB_SOURCE)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("acts")
    a.add_argument("--only", nargs="*")
    sub.add_parser("tables")
    w = sub.add_parser("whole")
    w.add_argument("--only", nargs="*")
    sub.add_parser("cpc")
    sub.add_parser("schedules")
    sub.add_parser("formats")
    g = sub.add_parser("gap-acts")
    g.add_argument("--only", nargs="*")
    for name in ("rules", "repealed", "notifications", "constitution-amendments", "hindi-acts", "state-acts"):
        p = sub.add_parser(name)
        p.add_argument("--fresh", action="store_true")
        if name in ("notifications", "state-acts"):
            p.add_argument("--only", nargs="*")
    sub.add_parser("fees-stamps")
    sub.add_parser("state-gaps")
    c = sub.add_parser("ic-check")
    c.add_argument("prefix")
    c.add_argument("--n", type=int, default=3)
    args = ap.parse_args()
    if args.cmd in ("rules", "repealed", "notifications", "constitution-amendments", "hindi-acts", "state-acts", "fees-stamps", "ic-check", "state-gaps"):
        from . import ic_load
        if args.cmd == "state-gaps":
            gaps = ic_load.state_gaps()
            for state, acts in sorted(gaps.items()):
                print(f"{state}: {len(acts)}: " + "; ".join(a["title"] for a in acts))
            print(f"{sum(len(v) for v in gaps.values())} state acts on India Code are not in acts/state (list: library/state/not-in-open-india-law.jsonl.gz)")
            return 0
        if args.cmd == "fees-stamps":
            for state, v in sorted(ic_load.fees_and_stamps().items()):
                print(f"{state}: court fees: {'; '.join(v['court_fees']) or '-'} (+{v['court_fees_amendments']} amending) | "
                      f"stamp: {'; '.join(v['stamp']) or '-'} (+{v['stamp_amendments']} amending)")
            return 0
        if args.cmd == "ic-check":
            return ic_load.check(args.prefix, args.n)
        run = {"rules": ic_load.rules, "repealed": ic_load.repealed, "notifications": ic_load.notifications,
               "constitution-amendments": ic_load.constitution, "hindi-acts": ic_load.hindi_acts, "state-acts": ic_load.state_acts}[args.cmd]
        res = run(args.only, fresh=args.fresh) if hasattr(args, "only") else run(fresh=args.fresh)
        for r in res:
            print("  " + " · ".join(f"{k} {v}" for k, v in r.items()), flush=True)
        return 0
    if args.cmd == "gap-acts":
        gap_acts(args.only)
        return 0
    if args.cmd == "acts":
        load_acts(args.only)
    elif args.cmd == "tables":
        criminal_tables()
    elif args.cmd == "whole":
        load_whole_texts(args.only)
    elif args.cmd == "cpc":
        cpc_rules()
    elif args.cmd == "schedules":
        attachments("SCHEDULE", 9, "schedules", "schedule")
    elif args.cmd == "formats":
        attachments("APPENDIX", 10, "formats/appendices", "appendix")
        attachments("FORM", 10, "formats/forms", "form")
    return 0


if __name__ == "__main__":
    sys.exit(main())
