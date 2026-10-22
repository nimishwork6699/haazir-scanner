"""Court records beyond the case page: daily proceedings and order PDFs.

District courts (eCourts): each hearing in the case history links to its "business" record (what was
done, why it was adjourned, what the next date is for). Order PDFs, where the court uploaded them, come
from home/display_pdf. High Courts: order PDFs are linked on the case page.

Orders are stored in the case file (Supabase Storage + documents) and handed to the dashboard's AI
reader, which pulls out the next date, directions and any deadline with the page they appear on.
"""

import hashlib
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from . import config
from .db import DB

ORDERS_PER_CASE = 3      # latest orders kept per case
PROCEEDINGS_PER_CASE = 3  # latest hearings whose business record is read per case
_VIEW_BUSINESS = re.compile(r"viewBusiness\(([^)]*)\)")
_DISPLAY_PDF = re.compile(r"displayPdf\(([^)]*)\)")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _args(call: str) -> list[str]:
    return [a.strip().strip("'\"") for a in call.split(",")]


def _ddmmyyyy(s: str) -> str | None:
    m = re.match(r"(\d{2})-(\d{2})-(\d{4})", s or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def parse_business(html: str) -> dict:
    """'Business : -- Reason for Adjournment : ... Next Purpose : ... Next Hearing Date : 12-10-2026'."""
    text = re.sub(r"\s+", " ", BeautifulSoup(html or "", "lxml").get_text(" ")).strip()
    grab = lambda label, stop: (m.group(1).strip(" :-") if (m := re.search(re.escape(label) + r"\s*:\s*(.*?)\s*(?:" + stop + r"|$)", text)) else "")
    business = grab("Business", r"Reason for Adjournment|Next Purpose|Next Hearing Date")
    return {
        # Some courts type the field name itself ("Business") or "--" when nothing was recorded.
        "business_text": business if business and business.lower() not in ("--", "-", "business", "nil", "na") else None,
        "adjournment_reason": grab("Reason for Adjournment", r"Next Purpose|Next Hearing Date") or None,
        "next_purpose": grab("Next Purpose", r"Next Hearing Date") or None,
    }


async def district_proceedings(ec, db: DB, case_id: str, html: str) -> int:
    """Read the business record of the latest hearings that do not have one yet."""
    done = {h["business_date"] for h in db.select("hearings", f"select=business_date&case_id=eq.{case_id}&proceedings_fetched_at=not.is.null")}
    calls = [_args(c) for c in _VIEW_BUSINESS.findall(html)]
    todo = [a for a in calls if len(a) >= 11 and _ddmmyyyy(a[6]) and _ddmmyyyy(a[6]) not in done][:PROCEEDINGS_PER_CASE]
    n = 0
    for a in todo:
        form = {"court_code": a[0], "state_code": a[4], "dist_code": a[1], "nextdate1": a[2], "case_number1": a[3],
                "disposal_flag": a[5], "businessDate": a[6], "national_court_code": a[8], "court_no": a[7],
                "search_by": a[9], "srno": a[10]}
        try:
            r = await ec.client._post_ajax("home/viewBusiness", form)
        except Exception as exc:
            print(f"    proceedings {a[6]} failed: {type(exc).__name__}", flush=True)
            continue
        values = {**parse_business(r.get("data_list") or ""), "proceedings_fetched_at": now()}
        bdate = _ddmmyyyy(a[6])
        updated = db.update("hearings", f"case_id=eq.{case_id}&business_date=eq.{bdate}", values)
        if not updated:
            db.insert("hearings", {"case_id": case_id, "business_date": bdate, "hearing_date": _ddmmyyyy(a[2][6:8] + "-" + a[2][4:6] + "-" + a[2][:4]), **values},
                      upsert_on="case_id,hearing_date,business_date", ignore_duplicates=True)
        n += 1
    return n


def store_order(db: DB, case_id: str, pdf: bytes, order_date: str | None, label: str, source_ref: str) -> str | None:
    """Put an order PDF in the case file. Returns the new document id, or None if it is already there."""
    if pdf[:3] == b"\xef\xbb\xbf":
        pdf = pdf[3:]  # some portals prefix a byte-order mark
    if pdf[:4] != b"%PDF":
        return None
    sha = hashlib.sha256(pdf).hexdigest()
    if db.select("documents", f"select=id&case_id=eq.{case_id}&sha256=eq.{sha}"):
        return None
    path = f"ecourts/{case_id}/{order_date or 'undated'}-{sha[:10]}.pdf"
    r = httpx.post(f"{config.SUPABASE_URL}/storage/v1/object/case-files/{path}", content=pdf, timeout=60,
                   headers={"Authorization": f"Bearer {config.SUPABASE_SECRET_KEY}", "apikey": config.SUPABASE_SECRET_KEY,
                            "Content-Type": "application/pdf", "x-upsert": "true"})
    if r.status_code >= 300:
        print(f"    order upload failed: {r.status_code} {r.text[:120]}", flush=True)
        return None
    doc = db.insert("documents", {"case_id": case_id, "kind": "order", "title": label, "source": "ecourts", "source_ref": source_ref,
                                  "storage_path": path, "mime": "application/pdf", "size_bytes": len(pdf), "sha256": sha,
                                  "received_at": now(), "received_by": "Haazir", "read_status": "pending", "order_date": order_date})[0]
    db.log(case_id, "document_added", {"source": "ecourts", "kind": "order", "order_date": order_date})
    return doc["id"]


async def district_orders(ec, db: DB, case_id: str, html: str) -> list[str]:
    """Order PDFs linked on a district case page (displayPdf), latest first."""
    soup = BeautifulSoup(html, "lxml")
    found = []
    for a in soup.find_all(attrs={"onclick": _DISPLAY_PDF}):
        args = _args(_DISPLAY_PDF.search(a["onclick"]).group(1))
        row = a.find_parent("tr")
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")] if row else []
        date = next((d for c in cells if (d := _ddmmyyyy(c))), None)
        found.append((date, args, " · ".join(cells[:3])))
    found.sort(key=lambda x: x[0] or "", reverse=True)
    ids = []
    for date, args, label in found[:ORDERS_PER_CASE]:
        if len(args) < 4:
            continue
        try:
            r = await ec.client._post_ajax("home/display_pdf", {"normal_v": args[0], "case_val": args[1], "court_code": args[2],
                                                                 "filename": args[3], "appFlag": args[4] if len(args) > 4 else ""})
            order_path = r.get("order") or ""
            resp = await ec.client._http.get("https://services.ecourts.gov.in/ecourtindia_v6/" + order_path.lstrip("/"))
            doc = store_order(db, case_id, resp.content, date, f"Court order {date or ''}".strip(), label[:200])
            if doc:
                ids.append(doc)
        except Exception as exc:
            print(f"    order PDF failed: {type(exc).__name__}: {str(exc)[:100]}", flush=True)
    return ids


async def high_court_orders(hc, db: DB, case_id: str, orders) -> list[str]:
    ids = []
    for o in sorted(orders, key=lambda o: o.order_date or "", reverse=True)[:ORDERS_PER_CASE]:
        if not o.pdf_url:
            continue
        try:
            resp = await hc.client._http.get(o.pdf_url, headers={"Referer": "https://hcservices.ecourts.gov.in/hcservices/main.php",
                                                                  "Accept": "application/pdf,*/*"})
            date = o.order_date.isoformat() if o.order_date else None
            doc = store_order(db, case_id, resp.content, date, f"High Court order {date or ''}".strip(), o.order_type or "")
            if doc:
                ids.append(doc)
        except Exception as exc:
            print(f"    HC order PDF failed: {type(exc).__name__}: {str(exc)[:100]}", flush=True)
    return ids


def read_with_ai(doc_ids: list[str], limit: int = 20) -> int:
    """Ask the dashboard to read new orders now (Gemini). The rest are read when someone opens the case."""
    if not config.APP_URL:
        return 0
    n = 0
    for doc in doc_ids[:limit]:
        try:
            r = httpx.post(f"{config.APP_URL.rstrip('/')}/api/documents/{doc}/read", timeout=120)
            n += r.status_code < 300
        except httpx.HTTPError:
            pass
    return n


def analyse_orders(limit: int = 15) -> dict:
    """Ask the dashboard to read the newest orders for the legal team: what happened, who appeared for us,
    what we must do. Its tags become case events, which haazir.events alerts on Telegram."""
    if not config.APP_URL:
        return {}
    try:
        r = httpx.post(f"{config.APP_URL.rstrip('/')}/api/orders/read", json={"limit": limit}, timeout=320)
        return r.json() if r.status_code < 300 else {"error": r.status_code}
    except httpx.HTTPError as exc:
        return {"error": str(exc)[:120]}


def refresh_deadlines() -> None:
    """Ask the dashboard to recompute deadlines after a scan changed dates, stages or orders."""
    if config.APP_URL:
        try:
            httpx.post(f"{config.APP_URL.rstrip('/')}/api/deadlines/refresh", json={}, timeout=90)
        except httpx.HTTPError:
            pass
