"""eCourts through a relay in Mumbai.

eCourts (services.ecourts.gov.in, hcservices.ecourts.gov.in) answers only Indian addresses: from GitHub's US machines
every request times out. With ECOURTS_RELAY set, each eCourts request is sent to a small function running in Mumbai,
which makes it there and sends the answer back. Without it, nothing changes: requests go straight to the court.

    ECOURTS_RELAY      supabase | vercel | <relay URL> | off (blank = off)
    ECOURTS_RELAY_KEY  the relay's shared secret (header x-relay-key)

The relays (same contract, see relay/vercel/api/relay.js and supabase/functions/ecourts-relay/index.ts):
    POST {method, url, headers: [[k, v]...], body_b64, gzip}
      -> {status, headers: [[k, v]...] (each Set-Cookie on its own), body_b64, body_gz, region, ms}
They never follow redirects, so redirects, cookies and sessions stay with the scanner's own httpx client.

A run keeps one relay for its whole life (an eCourts session and its captcha may be tied to the caller). Each run
counts its relay calls and bytes, prints them at the end, and adds them to a monthly total in settings
(relay_calls:<yyyy-mm>:<relay>, {"calls", "bytes"}). A run starting when its relay has used 80% of its free month uses
the other relay; when both have, it goes direct (and on GitHub eCourts will then time out).
"""

from __future__ import annotations

import atexit
import base64
import gzip
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

VERCEL_URL = "https://haazir-relay.vercel.app/api/relay"
SUPABASE_PATH = "/functions/v1/ecourts-relay"
ALLOWED = re.compile(r"(^|\.)ecourts\.gov\.in$", re.I)

# Free monthly allowance of each relay. Supabase: 500,000 Edge Function calls, and its 5 GB of egress is shared with
# the live website, so the relay gets at most half. Vercel Hobby: 1,000,000 function calls, 10 GB Fast Origin Transfer.
LIMITS = {"supabase": {"calls": 500_000, "bytes": 2_500_000_000},
          "vercel": {"calls": 1_000_000, "bytes": 10_000_000_000}}
SHARE = 0.8          # stop using a relay once it has used this much of its month
FLUSH_EVERY = 100    # add this run's calls to the monthly total every so many calls (and at the end)
TIMEOUT = httpx.Timeout(45.0, connect=15.0)  # the relay gives eCourts 25 s; this leaves room for the relay itself


class RelayError(RuntimeError):
    """The relay refused the request (wrong key, address not allowed, answer too large): not worth retrying."""


@dataclass
class Relay:
    name: str                       # supabase | vercel | custom (the monthly counter's name)
    url: str
    key: str
    headers: dict = field(default_factory=dict)
    calls: int = 0
    bytes: int = 0
    errors: int = 0
    unflushed: list = field(default_factory=lambda: [0, 0])  # calls, bytes not yet in the monthly total

    def auth(self) -> dict:
        return {"x-relay-key": self.key, **self.headers}


def _supabase_url() -> str:
    base = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    if not base:
        raise RelayError("ECOURTS_RELAY=supabase needs SUPABASE_URL")
    return base + SUPABASE_PATH


def parse(value: str, key: str) -> Relay | None:
    """The relay named by ECOURTS_RELAY, or None for off."""
    v = (value or "").strip()
    if not v or v.lower() in ("off", "none", "direct", "0", "false"):
        return None
    if not key:
        raise RelayError("ECOURTS_RELAY is set but ECOURTS_RELAY_KEY is missing")
    low = v.lower()
    if low == "supabase":
        return Relay("supabase", _supabase_url(), key, {"x-region": "ap-south-1"})
    if low == "vercel":
        return Relay("vercel", VERCEL_URL, key)
    if low.startswith("http"):
        host = httpx.URL(v).host
        if host.endswith(".supabase.co"):
            return Relay("supabase", v, key, {"x-region": "ap-south-1"})
        return Relay("vercel" if host.endswith(".vercel.app") else "custom", v, key)
    raise RelayError(f"ECOURTS_RELAY must be supabase, vercel, a URL or off, not {v!r}")


def enabled() -> bool:
    """Whether this run is meant to use a relay (without choosing one or reading the monthly totals)."""
    return parse(os.environ.get("ECOURTS_RELAY", ""), "x") is not None


# ---------------------------------------------------------------- the monthly totals

def month_key(name: str, when: datetime | None = None) -> str:
    return f"relay_calls:{(when or datetime.now(timezone.utc)).strftime('%Y-%m')}:{name}"


def over(name: str, used: dict) -> bool:
    lim = LIMITS.get(name)
    if not lim:
        return False
    u = used.get(name) or {}
    return int(u.get("calls") or 0) >= SHARE * lim["calls"] or int(u.get("bytes") or 0) >= SHARE * lim["bytes"]


def pick(first: Relay, used: dict, make) -> tuple[Relay | None, str]:
    """The relay to use for this run: the one asked for, the other one when it has used 80% of its month, or None
    when both have. `make(name)` builds the other relay."""
    if not over(first.name, used):
        return first, ""
    other = {"supabase": "vercel", "vercel": "supabase"}.get(first.name)
    if other and not over(other, used):
        return make(other), f"{first.name} has used {SHARE:.0%} of its month; using {other}"
    return None, f"{first.name}{' and ' + other if other else ''} used {SHARE:.0%} of the month; going direct"


def _db():
    from .db import DB
    return DB()


def usage(db=None) -> dict:
    """This month's totals per relay, {name: {calls, bytes}}; empty when the database cannot be read."""
    try:
        db = db or _db()
        out = {}
        for name in LIMITS:
            rows = db.select("settings", f"select=value&key=eq.{month_key(name)}")
            out[name] = json.loads(rows[0]["value"]) if rows else {"calls": 0, "bytes": 0}
        return out
    except Exception as exc:  # never stop a run over the counter
        print(f"eCourts relay: could not read the monthly totals ({type(exc).__name__}); going ahead", flush=True)
        return {}


def add_usage(db, key: str, calls: int, nbytes: int, tries: int = 8) -> bool:
    """Add to a monthly total. Several machines add at once, so each write only lands if the row still holds the
    value just read (compare-and-swap through PostgREST); otherwise read again and retry."""
    for _ in range(tries):
        rows = db.select("settings", f"select=value&key=eq.{key}")
        if not rows:
            r = db.http.post("/settings", json={"key": key, "value": json.dumps({"calls": calls, "bytes": nbytes})},
                             headers={"Prefer": "return=minimal"})
            if r.status_code == 409:
                continue  # another machine created it first: read it and add
            db._check(r)
            return True
        old = rows[0]["value"]
        try:
            cur = json.loads(old)
        except ValueError:
            cur = {}
        new = {"calls": int(cur.get("calls") or 0) + calls, "bytes": int(cur.get("bytes") or 0) + nbytes}
        r = db.http.patch("/settings", params={"key": f"eq.{key}", "value": f"eq.{old}"},
                          json={"value": json.dumps(new), "updated_at": datetime.now(timezone.utc).isoformat()},
                          headers={"Prefer": "return=representation"})
        if db._check(r):
            return True
    return False


# ---------------------------------------------------------------- the run's relay (sticky)

_decided = False
_chosen: Relay | None = None


def chosen() -> Relay | None:
    """This run's relay, decided once and kept for the whole run."""
    global _decided, _chosen
    if _decided:
        return _chosen
    _decided = True
    key = os.environ.get("ECOURTS_RELAY_KEY", "").strip()
    first = parse(os.environ.get("ECOURTS_RELAY", ""), key)
    if first is None:
        return None
    used = usage()
    relay, why = pick(first, used, lambda name: parse(name, key))
    if why:
        print(f"eCourts relay: {why}", flush=True)
    if relay:
        u = used.get(relay.name) or {}
        print(f"eCourts relay: {relay.name} for this run (this month so far: {int(u.get('calls') or 0):,} calls, "
              f"{int(u.get('bytes') or 0) / 1e6:,.0f} MB)", flush=True)
        atexit.register(_at_exit, relay)
    _chosen = relay
    return relay


def _reset_for_tests(relay: Relay | None = None, decided: bool = False) -> None:
    global _decided, _chosen
    _decided, _chosen = decided, relay


def flush(relay: Relay | None = None) -> None:
    relay = relay or _chosen
    if not relay or not relay.unflushed[0]:
        return
    calls, nbytes = relay.unflushed
    relay.unflushed = [0, 0]
    try:
        if not add_usage(_db(), month_key(relay.name), calls, nbytes):
            print("eCourts relay: monthly total busy; this batch not counted", flush=True)
    except Exception as exc:
        print(f"eCourts relay: could not add to the monthly total ({type(exc).__name__}: {str(exc)[:100]})", flush=True)


def summary(relay: Relay | None = None) -> str:
    """One line for a run's log: which relay, how many calls and bytes. Blank without a relay."""
    relay = relay or _chosen
    if not relay:
        return ""
    return f"eCourts relay {relay.name}: {relay.calls:,} calls, {relay.bytes / 1e6:,.1f} MB" + (
        f", {relay.errors} failed" if relay.errors else "")


def _at_exit(relay: Relay) -> None:
    flush(relay)
    if relay.calls:
        print(summary(relay), flush=True)


def _count(relay: Relay, nbytes: int, failed: bool = False) -> None:
    relay.calls += 1
    relay.bytes += nbytes
    relay.errors += failed
    relay.unflushed[0] += 1
    relay.unflushed[1] += nbytes
    if relay.unflushed[0] >= FLUSH_EVERY and relay is _chosen:
        flush(relay)


# ---------------------------------------------------------------- request <-> JSON

def encode(request: httpx.Request, body: bytes) -> dict:
    return {"method": request.method, "url": str(request.url),
            "headers": [[k, v] for k, v in request.headers.multi_items()],
            "body_b64": base64.b64encode(body).decode() if body else "", "gzip": True}


def decode(data: dict, request: httpx.Request) -> httpx.Response:
    body = base64.b64decode(data.get("body_b64") or "")
    if data.get("body_gz"):
        body = gzip.decompress(body)
    headers = [(str(k), str(v)) for k, v in data.get("headers") or []]
    return httpx.Response(int(data["status"]), headers=headers, content=body, request=request)


def result(relay: Relay, r: httpx.Response, request: httpx.Request) -> httpx.Response:
    """The court's answer from the relay's reply, or the matching httpx error, so the library's retries still work
    (a court that does not answer is a ConnectTimeout, as it would be direct)."""
    if r.status_code == 200:
        _count(relay, len(r.content))
        return decode(r.json(), request)
    _count(relay, len(r.content), failed=True)
    try:
        info = r.json()
    except ValueError:
        info = {"error": r.text[:200]}
    msg = f"relay {relay.name} {r.status_code}: {info.get('error') or ''}"[:300]
    if r.status_code == 502 and info.get("timeout"):
        raise httpx.ConnectTimeout(msg, request=request)
    if r.status_code in (400, 401, 403, 405, 413):
        raise RelayError(msg)
    if r.status_code == 504:
        raise httpx.ReadTimeout(msg, request=request)
    raise httpx.ConnectError(msg, request=request)  # 502 (court refused the connection), 429, 5xx of the platform


class AsyncRelayTransport(httpx.AsyncBaseTransport):
    """An httpx transport that sends eCourts requests through the relay; other addresses go direct."""

    def __init__(self, relay: Relay, verify=False) -> None:
        self.relay = relay
        self._http = httpx.AsyncClient(timeout=TIMEOUT)
        self._direct = httpx.AsyncHTTPTransport(verify=verify)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not ALLOWED.search(request.url.host or ""):
            return await self._direct.handle_async_request(request)
        payload = encode(request, await request.aread())
        try:
            r = await self._http.post(self.relay.url, json=payload, headers=self.relay.auth())
        except httpx.TimeoutException as exc:
            _count(self.relay, 0, failed=True)
            raise httpx.ReadTimeout(f"relay {self.relay.name}: {type(exc).__name__}", request=request) from exc
        except httpx.TransportError as exc:
            _count(self.relay, 0, failed=True)
            raise httpx.ConnectError(f"relay {self.relay.name}: {type(exc).__name__}: {exc}", request=request) from exc
        return result(self.relay, r, request)

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._direct.aclose()


class RelayTransport(httpx.BaseTransport):
    """The same for synchronous httpx.Client."""

    def __init__(self, relay: Relay, verify=False) -> None:
        self.relay = relay
        self._http = httpx.Client(timeout=TIMEOUT)
        self._direct = httpx.HTTPTransport(verify=verify)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not ALLOWED.search(request.url.host or ""):
            return self._direct.handle_request(request)
        payload = encode(request, request.read())
        try:
            r = self._http.post(self.relay.url, json=payload, headers=self.relay.auth())
        except httpx.TimeoutException as exc:
            _count(self.relay, 0, failed=True)
            raise httpx.ReadTimeout(f"relay {self.relay.name}: {type(exc).__name__}", request=request) from exc
        except httpx.TransportError as exc:
            _count(self.relay, 0, failed=True)
            raise httpx.ConnectError(f"relay {self.relay.name}: {type(exc).__name__}: {exc}", request=request) from exc
        return result(self.relay, r, request)

    def close(self) -> None:
        self._http.close()
        self._direct.close()


def attach(rlc) -> None:
    """Send a bharat_courts RateLimitedClient's requests (DistrictCourtClient._http, HCServicesClient._http) through
    this run's relay. Does nothing without ECOURTS_RELAY. Call it before the client is first used."""
    relay = chosen()
    if relay is None:
        return
    library_client = rlc._ensure_client  # builds the library's own httpx client: its headers, timeout, redirects

    def ensure():
        c = rlc._client
        if c is not None and not c.is_closed and getattr(c, "_haazir_relay", False):
            return c
        plain = library_client()
        rlc._client = httpx.AsyncClient(timeout=plain.timeout, headers=plain.headers, follow_redirects=plain.follow_redirects,
                                        transport=AsyncRelayTransport(relay))
        rlc._client._haazir_relay = True
        return rlc._client

    rlc._ensure_client = ensure
