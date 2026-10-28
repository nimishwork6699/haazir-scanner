"""Item 33 (and 18, 19): the open-india-law snapshot (Vaquill, CC BY 4.0, https://huggingface.co/datasets/vaquill/open-india-law),
taken from India Code and the regulators' own sites: every State and UT act section by section, central enactments, and
12 regulators' instruments (RBI, SEBI, IRDAI, MCA, TRAI, CBIC, DGFT, MoEFCC, CPCB, DFS, state GST, Law Commission).

  python -m haazir.oil load        download the files (needs HF_TOKEN) and write Backblaze:
                                   acts/state/<state>.jsonl.gz, acts/state/index.jsonl.gz, acts/oil-central.jsonl.gz,
                                   regulations/<body>.jsonl.gz
The snapshot stores each section in parts ("2(a)", "2(b)", the opening words …), each with a repeated header; they are
joined back into whole sections here, in order. Attribution, required by the licence, is kept on every file's source.
"""

from __future__ import annotations

import io
import os
import re
import sys
from collections import defaultdict

import httpx
import pyarrow.parquet as pq

from . import config
from .db import DB
from .lawlib import record
from .storage import B2, pack

REPO = "https://huggingface.co/datasets/vaquill/open-india-law"
CREDIT = "open-india-law (Vaquill, CC BY 4.0) from "


def _files(c: httpx.Client) -> list[str]:
    tree = c.get("https://huggingface.co/api/datasets/vaquill/open-india-law/tree/main").json()
    return [f["path"] for f in tree if f["path"].endswith(("_legislation.parquet", "_regulations.parquet"))]


def _body(text: str) -> str:
    """The part's own words: the 'Act: … | In Force' and 'Section 2(d): …' header lines removed."""
    parts = (text or "").split("\n\n", 1)
    return (parts[1] if len(parts) == 2 and parts[0].startswith("Act:") else text or "").strip()


def _order(chunk_id: str) -> tuple:
    """Parts of a section in reading order: the whole section, then the opening paragraphs (p0, p1 …), then clauses."""
    m = re.search(r"_s\d[^_]*(?:_(.+))?$", chunk_id)     # 'IND_state_16719_s002_d' -> 'd'
    tail = (m.group(1) or "") if m else ""
    if not tail:
        return (0,)
    kind = 1 if re.fullmatch(r"p\d+", tail) else 2
    return (kind,) + tuple((0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.findall(r"\d+|\D+", tail))


def _sections(table) -> tuple[list[dict], list[dict]]:
    """One row per section (parts joined), and one row per act."""
    parts: dict[tuple, list] = defaultdict(list)
    acts: dict[str, dict] = {}
    for r in table.to_pylist():
        parts[(r["act_id"], r["section_number"] or "")].append(r)
        a = acts.setdefault(r["act_id"], {"act_id": r["act_id"], "act": (r["title"] or "").rstrip(". "), "year": int(float(r["year"])) if r.get("year") not in (None, "", "None") else None,
                                          "state": r.get("state") or None, "status": r.get("act_status") or None, "source_url": r.get("source_url"), "sections": 0})
        a["sections"] = a["sections"]  # counted below
    rows = []
    for (act_id, number), ps in parts.items():
        ps.sort(key=lambda p: _order(p["chunk_id"]))
        whole = [p for p in ps if _order(p["chunk_id"]) == (0,)]
        texts, seen = [], set()
        for p in (whole or ps):
            b = _body(p["text"])
            if b and b not in seen:
                seen.add(b); texts.append(b)
        first = ps[0]
        title = (first.get("section_title") or "").strip()
        rows.append({k: v for k, v in {
            "act_id": act_id, "act": acts[act_id]["act"], "section": number, "chapter": first.get("chapter") or None,
            "title": None if title.rstrip(". ") == acts[act_id]["act"] else title, "text": "\n\n".join(texts),
            "subjects": first.get("legal_subject") if first.get("legal_subject") not in ("[]", "['general']", None) else None,
        }.items() if v not in (None, "")})
        acts[act_id]["sections"] += 1
    def key(r):
        n = re.match(r"(\d+)", r.get("section") or "")
        return (r["act_id"], int(n.group(1)) if n else 10**6, r.get("section") or "")
    rows.sort(key=key)
    return rows, sorted(acts.values(), key=lambda a: (a["state"] or "", a["act"]))


def load() -> None:
    token = config.env("HF_TOKEN")
    db, b2 = DB(), B2()
    index: list[dict] = []
    with httpx.Client(timeout=600, follow_redirects=True, headers={"Authorization": f"Bearer {token}"}) as c:
        for f in _files(c):
            r = c.get(f"{REPO}/resolve/main/{f}")
            r.raise_for_status()
            table = pq.read_table(io.BytesIO(r.content))
            rows, acts = _sections(table)
            name = f.removeprefix("in_").removesuffix(".parquet")
            if f.endswith("_regulations.parquet"):
                body = name.removesuffix("_regulations")
                path, kind, item, src = f"regulations/{body}.jsonl.gz", "regulations", 19, CREDIT + body.upper()
                title = f"{body.upper()}: regulations, directions and circulars"
            elif name == "central_legislation":
                path, kind, item, src = "acts/oil-central.jsonl.gz", "acts_sections", 33, CREDIT + "India Code"
                title = "Central enactments, section by section (open-india-law)"
            else:
                state = name.removesuffix("_legislation")
                path, kind, item, src = f"acts/state/{state}.jsonl.gz", "state_acts", 18, CREDIT + "India Code"
                title = f"{state.replace('-', ' ').title()}: state acts, section by section"
                index += acts
            data = pack(rows)
            b2.put(path, data, "application/gzip")
            record(db, path, "b2", kind, item, data, title=title, rows=len(rows), source=src)
            print(f"{path}: {len(acts):,} instruments, {len(rows):,} sections, {len(data) / 1e6:.1f} MB", flush=True)
    data = pack(index)
    b2.put("acts/state/index.jsonl.gz", data, "application/gzip")
    record(db, "acts/state/index.jsonl.gz", "b2", "state_acts_index", 18, data, title="State and UT acts: index", rows=len(index), source=CREDIT + "India Code")
    print(f"state index: {len(index):,} acts", flush=True)


def main() -> int:
    if sys.argv[1:2] == ["load"]:
        load()
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
