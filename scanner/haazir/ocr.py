"""OCR for library documents whose stored text is empty (a scanned PDF) or unreadable (old Gazette PDFs typed in
legacy Hindi fonts such as Krutidev, whose text layer reads "jftLVªh laö Mhö" instead of Hindi).

Which documents: only those whose text is empty or scan-only (scan_only / scan / text_status other than "ok"), or
fails the readability rule below (readable(), tested by haazir.ocr_check). Good text is never OCR'd.
How: pdftoppm (300 dpi) and tesseract -l hin+eng, page by page. A page whose own PDF text already reads well keeps that
text; only empty or garbled pages are OCR'd. Documents longer than MAX_PAGES are OCR'd up to that page and marked
ocr_capped (later pages keep their PDF text).
Writing back: the row's text is replaced and text_source "ocr", ocr_pages (pages OCR'd) and ocr_at are added; every
other field is kept. The text file is written again to the same path and store, and its library_files row updated.

Three row formats are understood:
  (a) website loaders (web_*.py)   file = the stored PDF's path, store = its store
  (b) India Code (ic_load.py)      files[] = {name, store, zip, offset, size}: the PDF is a ranged read of its zip
  (c) tribunals (tribunals_lib.py) as (a), with scan = true for scanned decisions

A spec picks the text files: item numbers, and item:folder for one folder of an item (33:ibbi is
library/tribunals/ibbi/...). Index and summary files are never touched. Like haazir.fulltext, the work runs on many
GitHub machines (.github/workflows/ocr.yml):
  python -m haazir.ocr plan "<spec>"           text files with documents still to OCR, bin-packed by pages into up to
                                                250 units (Backblaze work/ocr-<hash>.json); prints the unit count
  python -m haazir.ocr run "<spec>" <i>         does unit i
  python -m haazir.ocr status "<spec>"          per item: needing OCR, OCR'd, still failing, size before and now
  python -m haazir.ocr try <store> <path> [n]   OCR the first n documents of one text file that need it; the result goes
                                                to Backblaze work/ocr-try/... only, never to the library
  python -m haazir.ocr measure <item> [files]   empty / scan-only / garbled counts in a random sample (no OCR)
A rerun skips documents already marked text_source "ocr" (or ocr_error: the PDF itself could not be read), and a unit
saves its file every few minutes, so a cut-short run loses little. One bad PDF never stops a unit.

Languages: hin+eng, except a document in another Indian language (title "… in Tamil") or a state's act or rule (item 18,
row "state"), which is read in that language and English (ocr_langs).

Now:    10 11 18 21 22 24 25 27 28 29 31 33:ibbi 33:tdsat 33:cci 33:aptel 33:gst-aar
Later (still being loaded on the laptop): 20 33:cat 33:atfp
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from .db import DB
from .storage import B2, Drive

UNITS = 250             # GitHub runs at most 256 jobs from one list
MAX_PAGES = 300         # pages OCR'd per document at most
DPI = 300
LANGS = "hin+eng"
SAVE_EVERY = 15 * 60    # seconds between saves of a text file being worked on
UNIT_BUDGET = 320 * 60  # a unit stops starting new documents after this (the job is cut at 350 minutes)
SKIP_KINDS = {"judgments", "judgment_index", "tribunal_skipped", "acts_sections", "quantum_table"}


def _tool(name: str) -> str:
    for p in (rf"C:\Program Files\Git\mingw64\bin\{name}.exe", rf"C:\Program Files\Tesseract-OCR\{name}.exe", f"/usr/bin/{name}"):
        if os.path.exists(p):
            return p
    return shutil.which(name) or name


PDFTOTEXT, PDFTOPPM, TESSERACT = _tool("pdftotext"), _tool("pdftoppm"), _tool("tesseract")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- the readability rule

# Words of the commonest Hindi words as Krutidev-type fonts spell them in Latin letters (का, के, की, में, है, और, से,
# को, कि, यह, भी, लिए, पर, भारत, सरकार, अधिनियम, भाग, राजपत्र, द्वारा, किया, गया, अथवा, या, जो, इस, एवं, तथा, नियम …).
KRUTIDEV = {"dk", "ds", "dh", "esa", "gS", "vkSj", "ls", "dks", "fd", ";g", "Hkh", "fy,", "ij", "Hkkjr", "ljdkj", "vf/kfu;e",
            "Hkkx", "jkti=", "}kjk", "fd;k", "x;k", "vFkok", ";k", "tks", "bl", ",oa", "rFkk", "fu;e", "laö", "Øe",
            "fnYyh", "vlk/kj.k", "izkf/kdkj", "izdkf'kr", "eaf=ky;", "vf/klwpuk", "mi", "lkFk", "mDr", "le;", "dsUnz",
            "jkT;", "gksxk", "gksxh", "gSa", "fdlh", ",sls", "mlds", "dj", "fn;k", "uhps", "Ldhe", "vuqlkj"}
ENGLISH = set("""the of and to in a is that for be by with as on or any shall such this it are from at an not which
be been has have was were may under said all other no its their than if made where under before after into upon
section act rules order government central state india court date number notification within thereof
hereby following provided under being person whereas against between under""".split())
# A token's own letters, with any brackets, quotes and punctuation around it removed.
EDGE = "\"'`()[]{}<>.,;:!?|-–—_/\\*•·“”‘’«»…।॥"
LATIN_WORD = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
WORD_SHAPE = re.compile(r"[A-Z]?[a-z]+(?:['’-][A-Za-z][a-z]*)*|[A-Z]+(?:['’-][A-Z]+)*")
ABBREV = re.compile(r"(?:[A-Za-z]{1,5}\.){1,6}[A-Za-z]{0,5}")
NUMBERISH = re.compile(r"[\d.,/:()\-–%]+|[\d]+(?:st|nd|rd|th|ST|ND|RD|TH)|[ivxlcdmIVXLCDM]+|\(?[a-zA-Z]\)?|\d+[A-Za-z]{1,2}")
VOWEL = re.compile(r"[aeiouyAEIOUY]")
NO_VOWEL_OK = {"mr", "dr", "st", "ltd", "pvt", "vs", "nd", "th", "rd", "sh", "smt", "shri", "hrs", "kg", "km", "mm", "cm", "rs",
               "sq", "ft", "pp", "cr", "crpc", "cpc", "ipc", "cbdt", "sebi"}
DEVANAGARI = re.compile(r"[\u0900-\u097F\u200c\u200d]+(?:[-.][\u0900-\u097F\u200c\u200d]+)*\.?")
# Other Indian scripts (Bengali to Malayalam) and Urdu: read as words, never OCR'd with hin+eng. (Broken Hindi fonts
# put letters at Vedic, Sundanese or Ol Chiki code points, U+1B80-U+1CFF: those tokens are not words.)
OTHER_SCRIPT = re.compile(r"(?:[\u0980-\u0DFF\u200c\u200d]+|[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF\u200c]+)")
# India Code stamps "IndiaCode" across its PDFs; pdftotext reads pieces of it into the words ("iaC", "diaC", "IndiaCo").
WATERMARK = {"IndiaCode"[i:j] for i in range(9) for j in range(i + 3, 10)}
SPLIT = re.compile(r"[\u2014\u2013/]+")

MIN_LETTERS = 40        # a page with fewer letters than this has no real text layer (a scan)
BAD_SHARE = 0.35        # a page is garbled when more than this share of its words are not words
KRUTI_SHARE = 0.03      # … or when this share of its words (and at least KRUTI_MIN) are Krutidev spellings of
KRUTI_MIN = 12          #     common Hindi words
DOC_SHARE = 0.10        # a document needs OCR when this share of its words sit on garbled or empty pages


def _token_kind(tok: str) -> str:
    """'dev' (Hindi word), 'other' (another Indian script), 'latin' (an English-shaped word), 'kruti' (a Krutidev
    spelling), 'num' (a number, a numeral, a list letter), 'bad' (anything else), '' (punctuation only)."""
    if tok in KRUTIDEV:
        return "kruti"
    t = tok.strip(EDGE)
    if not any(ch.isalnum() for ch in t):
        return ""          # punctuation, symbols, bullets
    if t in KRUTIDEV:
        return "kruti"
    if t in WATERMARK:
        return ""
    if DEVANAGARI.fullmatch(t):
        # A word cannot start with a vowel sign or a virama ("ालय" for "मंत्रालय"): a font that lost its conjuncts.
        return "bad" if unicodedata.category(t[0]) in ("Mn", "Mc") else "dev"
    if OTHER_SCRIPT.fullmatch(t):
        return "other"
    if NUMBERISH.fullmatch(t):
        return "num"
    if any(ch.isdigit() for ch in t) and t.isascii():
        letters = re.sub(r"[\W\d_]+", "", t)
        if len(letters) <= 3 or (WORD_SHAPE.fullmatch(letters) and VOWEL.search(letters)):
            return "num"   # 3(i, 1/c, I1, +05'30, D-(D)-72, Delhi-110064
    if WORD_SHAPE.fullmatch(t):
        if VOWEL.search(t) or t.lower() in NO_VOWEL_OK or (t.isupper() and len(t) <= 5):
            return "latin"
        return "num" if len(t) <= 3 else "bad"   # Sl, Jt, Sd, Pt, dt: abbreviations, judged neither way
    if ABBREV.fullmatch(t):
        return "num"
    # 'Section 5(1)(a)', "Government's", "co-operative" fall in WORD_SHAPE; e-mail, web addresses and codes do not
    if re.fullmatch(r"[\w.%+-]+@[\w.-]+|(?:https?://|www\.)\S+|[A-Z0-9]+(?:[-/][A-Z0-9]+)+", t):
        return "num"
    return "bad"


def page_stats(text: str) -> Counter:
    c = Counter()
    for tok in text.split():
        if tok in KRUTIDEV or tok.strip(EDGE) in KRUTIDEV:
            c["kruti"] += 1
        else:   # "II—Section", "and/or", "Delhi-110064": each part judged
            c.update(_token_kind(t) for t in SPLIT.split(tok) if t)
    c.pop("", None)
    c["letters"] = sum(ch.isalpha() for ch in text)
    return c


def page_verdict(text: str) -> str:
    """'ok', 'empty' (no real text: a scanned page) or 'garbled' (a broken font: not words)."""
    c = page_stats(text or "")
    if c["letters"] < MIN_LETTERS:
        return "empty"
    words = c["dev"] + c["other"] + c["latin"] + c["kruti"] + c["bad"]
    if words < 8:
        return "ok"
    if (c["bad"] + c["kruti"]) / words > BAD_SHARE:
        return "garbled"
    # The Gazette's masthead alone has about 6 Krutidev words ("jftLVªh laö Mhö … izkf/dkj ls izdkf'kr"): not enough.
    if c["kruti"] >= KRUTI_MIN and c["kruti"] / words > KRUTI_SHARE:
        return "garbled"
    return "ok"


def chunks(text: str, size: int = 300) -> list[str]:
    """Pages when the text keeps its page breaks, otherwise runs of about `size` words."""
    if "\f" in text:
        return [p for p in text.split("\f")]
    toks = text.split(" ")
    if len(toks) < 2 * size:
        lines, out, cur, n = text.split("\n"), [], [], 0
        for ln in lines:
            cur.append(ln)
            n += len(ln.split())
            if n >= size:
                out.append("\n".join(cur)); cur, n = [], 0
        if cur:
            out.append("\n".join(cur))
        return out
    return [" ".join(toks[i:i + size]) for i in range(0, len(toks), size)]


def readable(text: str | None) -> bool:
    """The stored text of a document reads as words: no more than DOC_SHARE of it sits on garbled pages, and it is
    not empty."""
    text = text or ""
    if sum(ch.isalpha() for ch in text) < MIN_LETTERS:
        return False
    total = bad = 0
    for ch in chunks(text):
        n = len(ch.split())
        total += n
        if n and page_verdict(ch) == "garbled":
            bad += n
    return total > 0 and bad / total <= DOC_SHARE


# ---------------------------------------------------------------- rows: which need OCR, and where their PDF is

def pdf_ref(row: dict) -> dict | None:
    """Where the document's PDF is: {"store", "path"} (formats a, c) or {"store", "zip", "offset", "size", "name"} (b)."""
    if row.get("files"):
        fs = [f for f in row["files"] if isinstance(f, dict) and f.get("zip") and f.get("size") and (f.get("name") or "").lower().endswith(".pdf")]
        main = next((f for f in fs if f.get("name") == row.get("main_file")), fs[0] if fs else None)
        if main:
            return {"store": main.get("store") or "drive", "zip": main["zip"], "offset": main["offset"], "size": main["size"],
                    "name": main["name"], "pages": main.get("pages")}
        return None
    f = row.get("file")
    if isinstance(f, str) and f.lower().endswith(".pdf") and row.get("store") in ("b2", "drive"):
        return {"store": row["store"], "path": f, "pages": row.get("pages")}
    return None


def scan_flagged(row: dict) -> bool:
    if row.get("scan_only") or row.get("scan") or row.get("unreadable"):
        return True
    st = row.get("text_status")
    return st is not None and st != "ok"


def needs_ocr(row: dict) -> bool:
    if row.get("text_source") == "ocr" or row.get("ocr_error") or pdf_ref(row) is None or ocr_langs(row) is None:
        return False
    return scan_flagged(row) or not readable(row.get("text"))


# A document in another Indian language (the Constitution's editions: "The Constitution of India in Tamil") is read in
# that language (and English); OCR in Hindi would only make new garbage of it. Codes are tesseract's (apt
# tesseract-ocr-<code>, installed by the workflow). Devanagari languages read with Hindi.
LANGUAGES = {"assamese": "asm", "bengali": "ben", "bangla": "ben", "gujarati": "guj", "kannada": "kan", "malayalam": "mal",
             "marathi": "mar+hin", "nepali": "nep+hin", "odia": "ori", "oriya": "ori", "punjabi": "pan", "sanskrit": "san+hin",
             "sindhi": "snd", "tamil": "tam", "telugu": "tel", "urdu": "urd", "kashmiri": "urd", "manipuri": "ben",
             "bodo": "hin", "dogri": "hin", "konkani": "hin+mar", "maithili": "hin", "santhali devanagari": "hin",
             "santali devanagari": "hin", "hindi": "hin", "english": "hin"}
NO_OCR_LANGUAGES = ("santhali", "santali", "ol chiki")   # Ol Chiki script: no tesseract model
# A state's acts and rules (item 18, India Code's row "state") are in English, Hindi or the state's language; old
# state Gazettes use the state's legacy fonts (Nudi for Kannada, …), so each state is read in its own language.
STATE_LANGUAGES = {"karnataka": "kan", "tamil nadu": "tam", "puducherry": "tam", "andhra pradesh": "tel", "telangana": "tel",
                   "kerala": "mal", "lakshadweep": "mal", "west bengal": "ben", "tripura": "ben", "assam": "asm",
                   "gujarat": "guj", "dadra": "guj", "daman": "guj", "odisha": "ori", "orissa": "ori", "punjab": "pan",
                   "maharashtra": "mar", "goa": "mar", "manipur": "ben", "jammu": "urd", "ladakh": "urd", "sikkim": "nep"}


def ocr_langs(row: dict) -> str | None:
    """tesseract's -l for this document, or None when there is no model for its language."""
    lang = str(row.get("language") or "").lower()
    if lang in ("", "en", "hi", "english", "hindi", "bilingual"):
        m = re.search(r"\bin (?:the )?([A-Z][a-z]+(?: \(?Devanagari\)?)?)(?: (?:language|script))?\b", row.get("title") or "")
        lang = (m.group(1).lower().replace("(", "").replace(")", "") if m else "") or lang
    for name, code in sorted(LANGUAGES.items(), key=lambda kv: -len(kv[0])):
        if name in lang and name not in ("english", "hindi"):
            return code if code.endswith("eng") else code + "+eng"
    if any(n in lang for n in NO_OCR_LANGUAGES):
        return None
    state = str(row.get("state") or "").lower()
    if state and state != "central":
        for name, code in STATE_LANGUAGES.items():
            if name in state:
                return f"{code}+eng"
    return LANGS


def est_pages(row: dict) -> int:
    ref = pdf_ref(row) or {}
    p = ref.get("pages") or row.get("pages")
    if not p and ref.get("size"):
        p = ref["size"] // 60_000
    return max(1, min(MAX_PAGES, int(p or 5)))


# ---------------------------------------------------------------- stores

class Stores:
    def __init__(self) -> None:
        self._b2: B2 | None = None
        self._drive: Drive | None = None
        self._ids: dict[str, str | None] = {}

    def of(self, store: str):
        if store == "b2":
            self._b2 = self._b2 or B2()
            return self._b2
        self._drive = self._drive or Drive()
        return self._drive

    @staticmethod
    def again(fn, *a):
        """Store clients retry busy answers but not a dropped connection: a long run should not stop on one."""
        for attempt in range(6):
            try:
                return fn(*a)
            except (httpx.HTTPError, RuntimeError) as e:
                if attempt == 5:
                    raise
                print(f"    store: {type(e).__name__}; trying again", flush=True)
                time.sleep(10 * (attempt + 1))

    def get(self, store: str, path: str) -> bytes | None:
        return self.again(self.of(store).get, path)

    def put(self, store: str, path: str, data: bytes, ctype: str) -> None:
        self.again(self.of(store).put, path, data, ctype)

    def _range(self, ref: dict) -> bytes:
        lo, hi = ref["offset"], ref["offset"] + ref["size"] - 1
        if ref["store"] == "b2":
            b2 = self.of("b2")
            a = b2._authorize()
            r = b2.http.get(f"{a['apiInfo']['storageApi']['downloadUrl']}/file/{b2.bucket}/{ref['zip']}",
                            headers={"Authorization": a["authorizationToken"], "Range": f"bytes={lo}-{hi}"})
        else:
            d = self.of("drive")
            if ref["zip"] not in self._ids:
                self._ids[ref["zip"]] = d.file_id(ref["zip"])
            fid = self._ids[ref["zip"]]
            if not fid:
                raise FileNotFoundError(ref["zip"])
            r = d._call("GET", f"https://www.googleapis.com/drive/v3/files/{fid}", params={"alt": "media"}, headers={"Range": f"bytes={lo}-{hi}"})
        r.raise_for_status()
        if len(r.content) != ref["size"]:
            raise RuntimeError(f"{ref['zip']}: asked for {ref['size']} bytes, got {len(r.content)}")
        return r.content

    def pdf(self, ref: dict) -> bytes | None:
        if "zip" in ref:
            return self.again(self._range, ref)
        return self.get(ref["store"], ref["path"])


def unpack(data: bytes) -> list[dict]:
    # "\n" only: str.splitlines() also breaks at U+2028, U+0085 … which JSON leaves unescaped inside text.
    return [json.loads(line) for line in gzip.decompress(data).decode("utf-8").split("\n") if line.strip()]


def pack(rows: list[dict]) -> bytes:
    return gzip.compress("\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows).encode(), 9)


# ---------------------------------------------------------------- OCR of one PDF

def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, env={**os.environ, "OMP_THREAD_LIMIT": "1"})


def pdf_pages(path: str) -> list[str] | None:
    """The PDF's own text, page by page (None when pdftotext cannot read it)."""
    try:
        r = _run([PDFTOTEXT, "-enc", "UTF-8", "-q", path, "-"], 600)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0 and not r.stdout:
        return None
    pages = r.stdout.decode("utf-8", "replace").split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    return pages


def page_count(path: str) -> int | None:
    try:
        r = _run([_tool("pdfinfo"), path], 120)
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(rb"Pages:\s+(\d+)", r.stdout)
    return int(m.group(1)) if m else None


def ocr_page(path: str, n: int, work: str, langs: str = LANGS) -> str:
    """One page: rendered at DPI in grey, read by tesseract in Hindi and English."""
    base = os.path.join(work, f"p{n}")
    r = _run([PDFTOPPM, "-r", str(DPI), "-gray", "-png", "-f", str(n), "-l", str(n), "-singlefile", path, base], 300)
    img = base + ".png"
    if not os.path.exists(img):
        raise RuntimeError(f"pdftoppm page {n}: {r.stderr.decode('utf-8', 'replace')[:200]}")
    try:
        t = _run([TESSERACT, img, "stdout", "-l", langs, "--psm", "3"], 600)
        if t.returncode != 0:
            raise RuntimeError(f"tesseract page {n}: {t.stderr.decode('utf-8', 'replace')[:200]}")
        return t.stdout.decode("utf-8", "replace")
    finally:
        os.remove(img)


def tidy(s: str) -> str:
    s = re.sub(r"[ \t]+\n", "\n", s.replace("\r", ""))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


class BadPDF(Exception):
    """The PDF itself cannot be read or rendered: marked on the row (ocr_error), not tried again."""


def ocr_pdf(data: bytes, pool: ThreadPoolExecutor, langs: str = LANGS) -> dict:
    """{pages: [text per page], ocr_pages, pages_total, capped}. Pages whose own text reads well keep it."""
    with tempfile.TemporaryDirectory() as work:
        src = os.path.join(work, "a.pdf")
        with open(src, "wb") as f:
            f.write(data)
        own = pdf_pages(src) or []
        total = page_count(src) or len(own)
        if not total:
            raise BadPDF("no pages (not a readable PDF)")
        own += [""] * (total - len(own))
        upto = min(total, MAX_PAGES)
        todo = [i for i in range(upto) if page_verdict(own[i]) != "ok"]
        futs = {i: pool.submit(ocr_page, src, i + 1, work, langs) for i in todo}
        out, failed = list(own), 0
        for i, fu in futs.items():
            try:
                out[i] = fu.result()
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                failed += 1
                print(f"      page {i + 1}: {str(e)[:160]}", flush=True)
        if todo and failed == len(todo):
            raise BadPDF(f"no page could be OCR'd ({failed} tried)")
        return {"pages": [tidy(p) for p in out], "ocr_pages": len(todo) - failed, "pages_total": total,
                "capped": total > MAX_PAGES, "failed_pages": failed}


def apply(row: dict, got: dict) -> dict:
    """The row with its OCR text; every other field kept."""
    pages = got["pages"]
    india_code = bool(row.get("files"))   # India Code rows keep their text as cleaned lines, no page breaks
    if india_code:
        text = "\n".join(re.sub(r"\s+", " ", ln).strip() for p in pages for ln in p.splitlines() if ln.strip())
    else:
        text = "\n\f\n".join(pages).strip()
    out = dict(row)
    out.update({"text": text, "text_source": "ocr", "ocr_pages": got["ocr_pages"], "ocr_at": now()})
    if got["capped"]:
        out["ocr_capped"] = MAX_PAGES
        out["ocr_pages_total"] = got["pages_total"]
    if got["failed_pages"]:
        out["ocr_failed_pages"] = got["failed_pages"]
    if "chars" in row:
        out["chars"] = len(text)
    return out


# ---------------------------------------------------------------- which files: the spec

def parse_spec(spec: str) -> list[tuple[int, str | None]]:
    out = []
    for tok in spec.replace(",", " ").split():
        item, _, sub = tok.partition(":")
        out.append((int(item), sub or None))
    return out


def set_name(spec: str) -> str:
    return "ocr-" + hashlib.sha1(" ".join(sorted(spec.replace(",", " ").split())).encode()).hexdigest()[:10]


def text_files(db: DB, spec: str) -> list[dict]:
    """The library's text files the spec covers (never an index, a listing or a summary)."""
    out, seen = [], set()
    for item, sub in parse_spec(spec):
        for r in db.select("library_files", f"select=path,store,kind,item,rows,bytes,updated_at&item=eq.{item}&path=like.*.jsonl.gz&order=path"):
            p = r["path"]
            if r["kind"] in SKIP_KINDS or r["kind"].endswith("_index") or p.endswith("/index.jsonl.gz") or "_listing" in p or "/_" in p:
                continue
            if sub and f"/{sub}/" not in p:
                continue
            if p not in seen:
                seen.add(p)
                out.append(r)
    return out


# ---------------------------------------------------------------- plan, run, status

def plan(spec: str) -> int:
    db, st, b2 = DB(), Stores(), B2()
    name = set_name(spec)
    files = text_files(db, spec)
    work, before = [], {}
    for f in files:
        before[f["path"]] = f["bytes"]
        data = st.get(f["store"], f["path"])
        if not data:
            continue
        rows = unpack(data)
        need = [r for r in rows if needs_ocr(r)]
        if need:
            work.append((f["path"], f["store"], f["item"], len(need), sum(est_pages(r) for r in need)))
            print(f"  {f['path']}: {len(need)} of {len(rows)} to OCR, about {work[-1][4]} pages", file=sys.stderr, flush=True)
    work.sort(key=lambda w: -w[4])
    units: list[list] = [[] for _ in range(min(UNITS, len(work)))]
    sizes = [0] * len(units)
    for path, store, item, n, pages in work:     # largest first, each into the lightest unit
        i = sizes.index(min(sizes))
        units[i].append({"path": path, "store": store, "item": item})
        sizes[i] += pages
    b2.put(f"work/{name}.json", json.dumps({"spec": spec, "units": units}).encode(), "application/json")
    if b2.get(f"work/{name}-before.json") is None:
        b2.put(f"work/{name}-before.json", json.dumps(before).encode(), "application/json")
    print(f"{spec}: {sum(w[3] for w in work):,} documents in {len(work)} files to OCR, about {sum(sizes):,} pages, in "
          f"{len(units)} units (biggest {max(sizes or [0]):,} pages)", file=sys.stderr, flush=True)
    print(len(units))
    return len(units)


def _key(r: dict) -> tuple:
    return (r.get("source_url"), r.get("file"), r.get("id"), r.get("uuid"), r.get("main_file"), r.get("sha1"), r.get("title"))


class TextFile:
    """One text file being worked on: read once, saved every SAVE_EVERY seconds and at the end. If someone else
    rewrote it meanwhile, it is read again and this run's OCR'd rows are put into the newer copy."""

    def __init__(self, db: DB, st: Stores, f: dict) -> None:
        self.db, self.st, self.f = db, st, f
        self.stamp = self._stamp()
        data = st.get(f["store"], f["path"])
        if data is None:
            raise FileNotFoundError(f["path"])
        self.rows = unpack(data)
        self.done: dict[tuple, dict] = {}
        self.saved_at = time.time()

    def _stamp(self) -> tuple:
        r = self.db.select("library_files", f"select=bytes,updated_at&path=eq.{quote(self.f['path'], safe='/')}&order=path")
        return (r[0]["bytes"], r[0]["updated_at"]) if r else (None, None)

    def set(self, i: int, row: dict) -> None:
        self.rows[i] = row
        self.done[_key(row)] = row

    def save(self) -> int:
        if not self.done:
            return 0
        if self._stamp() != self.stamp:   # written by another loader since it was read: merge into the newer copy
            print(f"    {self.f['path']} changed since it was read: merging into the newer copy", flush=True)
            fresh = unpack(self.st.get(self.f["store"], self.f["path"]))
            self.rows = [self.done.get(_key(r), r) if r.get("text_source") != "ocr" else r for r in fresh]
        data = pack(self.rows)
        self.st.put(self.f["store"], self.f["path"], data, "application/gzip")
        self.db.update("library_files", f"path=eq.{quote(self.f['path'], safe='/')}", {"bytes": len(data), "updated_at": now()})
        self.stamp = self._stamp()
        self.saved_at = time.time()
        return len(data)


def run(spec: str, index: int, workers: int) -> None:
    t0 = time.time()
    b2 = B2()
    plan_ = json.loads(b2.get(f"work/{set_name(spec)}.json") or b"{}")
    units = plan_.get("units", [])
    if index >= len(units):
        print(f"unit {index}: nothing (there are {len(units)})"); return
    db, st = DB(), Stores()
    totals = Counter()
    with ThreadPoolExecutor(workers) as pool:
        for f in units[index]:
            if time.time() - t0 > UNIT_BUDGET:
                print(f"  out of time: {f['path']} left for the next run", flush=True)
                continue
            try:
                tf = TextFile(db, st, f)
            except Exception as e:  # noqa: BLE001 - one file that cannot be read must not stop the unit
                print(f"  {f['path']}: could not be read ({type(e).__name__}: {str(e)[:200]})", flush=True)
                totals["file_errors"] += 1
                continue
            todo = [i for i, r in enumerate(tf.rows) if needs_ocr(r)]
            print(f"  {f['path']}: {len(todo)} documents to OCR", flush=True)
            c = Counter()
            for i in todo:
                if time.time() - t0 > UNIT_BUDGET:
                    print("    out of time: the rest is left for the next run", flush=True)
                    break
                row = tf.rows[i]
                label = (row.get("title") or row.get("case_number") or row.get("file") or row.get("id") or "")[:80]
                ref = pdf_ref(row)
                try:
                    data = st.pdf(ref)
                    if not data:
                        raise FileNotFoundError(ref.get("path") or ref.get("zip"))
                except Exception as e:  # noqa: BLE001 - a store hiccup: left as it was, tried again next run
                    c["fetch_failed"] += 1
                    print(f"    {label}: PDF not fetched ({type(e).__name__}: {str(e)[:160]})", flush=True)
                    continue
                try:
                    got = ocr_pdf(data, pool, ocr_langs(row))
                    new = apply(row, got)
                    tf.set(i, new)
                    c["ocr"] += 1
                    c["pages"] += got["ocr_pages"]
                    if not readable(new["text"]):
                        c["still_unreadable"] += 1
                    print(f"    {label}: {got['ocr_pages']} of {got['pages_total']} pages OCR'd{' (capped)' if got['capped'] else ''}"
                          f"{'' if readable(new['text']) else ' - still not readable'}", flush=True)
                except Exception as e:  # noqa: BLE001 - one bad PDF never stops a unit
                    bad = dict(row)
                    bad["ocr_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                    tf.set(i, bad)
                    c["bad_pdf"] += 1
                    print(f"    {label}: {bad['ocr_error']}", flush=True)
                if time.time() - tf.saved_at > SAVE_EVERY:
                    try:
                        tf.save()
                    except Exception as e:  # noqa: BLE001 - kept in memory and saved again at the end
                        print(f"    save failed ({type(e).__name__}); will try again", flush=True)
            try:
                size = tf.save()
                print(f"  {f['path']}: {dict(c)}, saved ({size:,} bytes)", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"  {f['path']}: NOT saved ({type(e).__name__}: {str(e)[:200]}); its OCR is lost and redone next run", flush=True)
                c["save_failed"] += 1
            totals.update(c)
    print(f"unit {index}: {dict(totals)} in {(time.time() - t0) / 60:.0f} min", flush=True)


def status(spec: str) -> None:
    db, st, b2 = DB(), Stores(), B2()
    before = json.loads(b2.get(f"work/{set_name(spec)}-before.json") or b"{}")
    by: dict[int, Counter] = {}
    samples: dict[int, list] = {}
    for f in text_files(db, spec):
        c = by.setdefault(f["item"], Counter())
        rows = unpack(st.get(f["store"], f["path"]) or gzip.compress(b""))
        c["bytes_before"] += before.get(f["path"], f["bytes"])
        c["bytes_now"] += f["bytes"]
        for r in rows:
            if r.get("text_source") == "ocr":
                c["ocr"] += 1
                c["pages"] += r.get("ocr_pages") or 0
                if not readable(r.get("text")):
                    c["still_unreadable"] += 1
                samples.setdefault(f["item"], []).append((f["path"], r))
            elif r.get("ocr_error"):
                c["bad_pdf"] += 1
            elif needs_ocr(r):
                c["to_do"] += 1
    for item, c in sorted(by.items()):
        print(f"item {item}: needing OCR {c['ocr'] + c['bad_pdf'] + c['to_do']}, OCR'd {c['ocr']} ({c['pages']} pages), "
              f"still failing {c['still_unreadable'] + c['bad_pdf'] + c['to_do']} (unreadable after OCR {c['still_unreadable']}, "
              f"bad PDF {c['bad_pdf']}, not done {c['to_do']}), size {c['bytes_before'] / 1e6:.2f} -> {c['bytes_now'] / 1e6:.2f} MB")
        for path, r in random.sample(samples.get(item, []), min(3, len(samples.get(item, [])))):
            words = " ".join(r["text"].split())
            mid = len(words) // 3
            print(f"    {(r.get('title') or r.get('case_number') or r.get('file') or '')[:70]} | {words[mid:mid + 140]}")


def try_file(store: str, path: str, n: int, workers: int) -> None:
    """OCR the first n documents of one text file that need it, into work/ocr-try/ only (for reading back by hand)."""
    st = Stores()
    rows = unpack(st.get(store, path))
    picked = sorted((r for r in rows if needs_ocr(r)), key=scan_flagged)[:n]   # garbled text first, then scans
    out = []
    with ThreadPoolExecutor(workers) as pool:
        for r in picked:
            t = time.time()
            try:
                got = ocr_pdf(st.pdf(pdf_ref(r)), pool, ocr_langs(r))
                new = apply(r, got)
                new["ocr_seconds"] = round(time.time() - t)
                new["text_before"] = (r.get("text") or "")[:3000]
                out.append(new)
                print(f"  {(r.get('title') or r.get('file') or '')[:70]}: {got['ocr_pages']}/{got['pages_total']} pages in {time.time() - t:.0f}s, "
                      f"readable {readable(new['text'])}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"  {(r.get('title') or r.get('file') or '')[:70]}: {type(e).__name__}: {e}", flush=True)
    dest = "work/ocr-try/" + re.sub(r"[^A-Za-z0-9._-]+", "_", path)
    B2().put(dest, pack(out), "application/gzip")
    print(f"{len(out)} documents OCR'd -> Backblaze {dest}")


def measure(item: int, n_files: int) -> None:
    """Empty / scan-only and garbled documents in a random sample of an item's text files (no OCR)."""
    db, st = DB(), Stores()
    files = [f for f in text_files(db, str(item))]
    pick = random.sample(files, min(n_files, len(files)))
    c = Counter()
    for f in pick:
        for r in unpack(st.get(f["store"], f["path"]) or gzip.compress(b"")):
            c["docs"] += 1
            t = r.get("text") or ""
            if scan_flagged(r) or sum(ch.isalpha() for ch in t) < MIN_LETTERS:
                c["empty"] += 1
            elif not readable(t):
                c["garbled"] += 1
    print(f"item {item}: {len(pick)} of {len(files)} files, {c['docs']:,} documents: {c['empty']:,} empty or scan-only "
          f"({c['empty'] / max(1, c['docs']):.1%}), {c['garbled']:,} garbled ({c['garbled'] / max(1, c['docs']):.1%})")


def main() -> int:
    a = sys.argv[1:]
    workers = int(os.environ.get("OCR_WORKERS") or os.cpu_count() or 2)
    if len(a) >= 2 and a[0] == "plan":
        plan(a[1])
    elif len(a) >= 3 and a[0] == "run":
        run(a[1], int(a[2]), workers)
    elif len(a) >= 2 and a[0] == "status":
        status(a[1])
    elif len(a) >= 3 and a[0] == "try":
        try_file(a[1], a[2], int(a[3]) if len(a) > 3 else 3, workers)
    elif len(a) >= 2 and a[0] == "measure":
        measure(int(a[1]), int(a[2]) if len(a) > 2 else 20)
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
