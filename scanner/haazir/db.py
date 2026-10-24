"""Minimal PostgREST client for Supabase, authenticated with the secret key."""

from typing import Any

import httpx

from . import config


# Tables without an id column: page by their primary key instead.
_ORDER = {"scan_coverage": "court_id,company_id,year,term", "settings": "key", "number_watch": "court_id,series,year", "tg_prompts": "chat_id,message_id", "connections": "kind", "app_users": "email", "case_stories": "case_id", "case_briefs": "case_id", "order_reads": "document_id", "cnr_parties": "cnr"}


class DB:
    def __init__(self) -> None:
        self.http = httpx.Client(
            base_url=f"{config.SUPABASE_URL}/rest/v1",
            headers={
                "apikey": config.SUPABASE_SECRET_KEY,
                "Authorization": f"Bearer {config.SUPABASE_SECRET_KEY}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )

    def _check(self, r: httpx.Response) -> Any:
        if r.status_code >= 400:
            raise RuntimeError(f"Supabase {r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else None

    def select(self, table: str, query: str = "select=*") -> list[dict]:
        # Paging without a stable order can repeat or skip rows between pages.
        if "order=" not in query:
            query += "&order=" + _ORDER.get(table, "id")
        rows: list[dict] = []
        start = 0
        while True:
            r = self.http.get(f"/{table}?{query}", headers={"Range": f"{start}-{start + 999}"})
            page = self._check(r) or []
            rows.extend(page)
            if len(page) < 1000:
                return rows
            start += 1000

    # Writes send nothing back unless asked: a copy of every saved row was most of the scanner's data traffic.
    # returning="id" (or "*", or a column list) returns just those columns of the rows written.
    @staticmethod
    def _returning(returning: str | None) -> tuple[str, str]:
        if not returning:
            return "return=minimal", ""
        return "return=representation", f"select={returning}"

    def insert(self, table: str, rows: dict | list[dict], *, upsert_on: str | None = None,
               ignore_duplicates: bool = False, returning: str | None = None) -> list[dict]:
        ret, select = self._returning(returning)
        prefer = [ret]
        params = [select] if select else []
        if upsert_on:
            prefer.append("resolution=ignore-duplicates" if ignore_duplicates else "resolution=merge-duplicates")
            params.append(f"on_conflict={upsert_on}")
        q = f"?{'&'.join(params)}" if params else ""
        r = self.http.post(f"/{table}{q}", json=rows, headers={"Prefer": ",".join(prefer)})
        return self._check(r) or []

    def update(self, table: str, match: str, values: dict, *, returning: str | None = None) -> list[dict]:
        ret, select = self._returning(returning)
        r = self.http.patch(f"/{table}?{match}{'&' + select if select else ''}", json=values, headers={"Prefer": ret})
        return self._check(r) or []

    def log(self, case_id: str | None, action: str, meta: dict | None = None, actor: str = "scanner") -> None:
        self.insert("activity", {"case_id": case_id, "actor": actor, "action": action, "meta": meta or {}})
