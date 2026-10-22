"""e-Jagriti (consumer commissions) feasibility probe. Read-only.

e-Jagriti's public "advance case search" answers a free-text search per commission and filing-date
range without a login or CAPTCHA. This checks what it returns for our companies in Jaipur.

    python -m haazir.ejagriti_probe --since 2026-08-01
"""

import argparse
import json
import time
from collections import Counter
from datetime import date

import httpx

from .matching import is_company

BASE = "https://e-jagriti.gov.in/services"
HEADERS = {"User-Agent": "Mozilla/5.0", "Origin": "https://e-jagriti.gov.in",
           "Referer": "https://e-jagriti.gov.in/advance-case-search"}
COMMISSIONS = {11080000: "Rajasthan State Commission", 11080102: "Jaipur-I", 12080102: "Jaipur-II",
               13080102: "Jaipur-III", 14080102: "Jaipur-IV"}
COMPANIES = {"UNITED INDIA": ["united india insurance", "united india assurance"],
             "LOMBARD": ["icici lombard"], "BAJAJ FIN": ["bajaj finance"]}


def search(client: httpx.Client, commission: int, text: str, since: str, until: str) -> list[dict]:
    rows, page = [], 0
    while True:
        body = {"commissionId": commission, "page": page, "size": 30, "fromDate": since, "toDate": until,
                "dateRequestType": 1, "serchType": 8, "serchTypeValue": text, "orderType": 2}
        r = client.post(f"{BASE}/case/caseFilingService/v2/getCaseDetailsBySearchType", json=body)
        r.raise_for_status()
        data = r.json().get("data") or []
        rows += data
        if len(data) < 30 or page >= 20:
            return rows
        page += 1
        time.sleep(2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-01")
    ap.add_argument("--until", default=date.today().isoformat())
    ap.add_argument("--out", default="ejagriti_probe.json")
    args = ap.parse_args()
    out = []
    with httpx.Client(headers=HEADERS, timeout=40) as client:
        for cid, cname in COMMISSIONS.items():
            for text, confirm in COMPANIES.items():
                t0 = time.time()
                rows = search(client, cid, text, args.since, args.until)
                sides = Counter()
                for r in rows:
                    extra_r = [x.get("additional_respondent_name", "") for x in r.get("additionalRespondantList") or []]
                    extra_c = [x.get("additional_complainant_name", "") for x in r.get("additionalComplainantList") or []]
                    against = any(is_company(n or "", confirm) for n in [r.get("respondentName")] + extra_r)
                    by = any(is_company(n or "", confirm) for n in [r.get("complainantName")] + extra_c)
                    sides["against" if against else "by" if by else "not named"] += 1
                    r["_side"] = "against" if against else "by" if by else "not named"
                print(f"{cname:28} {text:13} {len(rows):4} cases  {dict(sides)}  {time.time() - t0:.1f}s", flush=True)
                out.append({"commission": cname, "term": text, "rows": rows})
                time.sleep(3)
    open(args.out, "w", encoding="utf-8").write(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
