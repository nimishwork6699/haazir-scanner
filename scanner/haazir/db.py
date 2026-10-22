"""Minimal PostgREST client for Supabase, authenticated with the secret key."""

from typing import Any

import httpx

from . import config


# Tables without an id column: page by their primary key instead.
_ORDER = {"scan_coverage": "court_id,company_id,year,term", "settings": "key", "number_watch": "court_id,series,year", "tg_prompts": "chat_id,message_id", "connections": "kind", "app_users": "email", "case_stories": "case_id", "case_briefs": "case_id", "order_reads": "document_id"}


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

    def insert(self, table: str, rows: dict | list[dict], *, upsert_on: str | None = None,
               ignore_duplicates: bool = False) -> list[dict]:
        prefer = ["return=representation"]
        params = ""
        if upsert_on:
            prefer.append("resolution=ignore-duplicates" if ignore_duplicates else "resolution=merge-duplicates")
            params = f"?on_conflict={upsert_on}"
        r = self.http.post(f"/{table}{params}", json=rows, headers={"Prefer": ",".join(prefer)})
        return self._check(r) or []

    def update(self, table: str, match: str, values: dict) -> list[dict]:
        r = self.http.patch(f"/{table}?{match}", json=values, headers={"Prefer": "return=representation"})
        return self._check(r) or []

    def log(self, case_id: str | None, action: str, meta: dict | None = None, actor: str = "scanner") -> None:
        self.insert("activity", {"case_id": case_id, "actor": actor, "action": action, "meta": meta or {}})
