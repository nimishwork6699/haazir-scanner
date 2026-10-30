"""Checks for the eCourts relay (haazir.relay), with no network and no database.

    python -m haazir.relay_check

A stand-in relay (the same contract as the Mumbai functions) in front of a stand-in court: requests and answers make the
round trip through JSON unchanged (form posts, binary PDFs, gzip), every cookie is kept and sent back, redirects are
followed by our own client, addresses other than eCourts never reach the relay, relay refusals and timeouts become the
errors the court library already retries or stops on, a run keeps one relay, a relay past 80% of its month hands over to
the other, the monthly total survives two machines adding at once, and with no ECOURTS_RELAY nothing changes.
"""

import asyncio
import base64
import gzip
import json
import json as _json
import os
import sys

import httpx

from . import relay as R

COURT = "https://services.ecourts.gov.in/ecourtindia_v6"
PDF = bytes(range(256)) * 64
KEY = "test-key"


def court(method: str, url: httpx.URL, headers: list, body: bytes):
    """A stand-in eCourts: (status, [[k, v]...], body) for a request."""
    h = {k.lower(): v for k, v in headers}
    path = url.path.rsplit("/", 1)[-1]
    if path == "home":
        return 200, [["content-type", "text/html"], ["set-cookie", "SERVICES_SESSID=abc; path=/"],
                     ["set-cookie", "JSESSION=xyz; path=/"]], b"<html>" + b"home " * 400 + b"</html>"
    if path == "go":
        return 302, [["location", COURT + "/landing"], ["set-cookie", "HOP=1; path=/"]], b""
    if path == "landing":
        return 200, [["content-type", "text/plain"]], ("cookies: " + h.get("cookie", "")).encode()
    if path == "form":
        return 200, [["content-type", "text/plain"]], (method + " " + h.get("content-type", "") + " ").encode() + body
    if path == "pdf":
        return 200, [["content-type", "application/pdf"]], PDF
    if path == "blocked":
        return 405, [["content-type", "text/html"]], b"Security Page"
    return 404, [], b"not found"


def stand_in_relay(seen: list, *, error: tuple | None = None):
    """The relay's contract in Python: key, allow-list, redirect manual, Set-Cookie kept apart, optional gzip."""
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if error:
            return httpx.Response(error[0], json=error[1])
        if req.headers.get("x-relay-key") != KEY:
            return httpx.Response(401, json={"error": "unauthorised"})
        p = json.loads(req.content)
        url = httpx.URL(p["url"])
        if not R.ALLOWED.search(url.host):
            return httpx.Response(403, json={"error": f"host not allowed: {url.host}"})
        body = base64.b64decode(p["body_b64"]) if p.get("body_b64") else b""
        status, headers, content = court(p["method"], url, p["headers"], body)
        gz = False
        if p.get("gzip") and len(content) > 512:
            z = gzip.compress(content)
            if len(z) < len(content):
                content, gz = z, True
        return httpx.Response(200, json={"status": status, "headers": headers, "body_b64": base64.b64encode(content).decode(),
                                         "body_gz": gz, "region": "test"})
    return handler


def transport(seen: list, direct: list, **kw) -> R.AsyncRelayTransport:
    relay = R.Relay("supabase", "https://relay.test/relay", KEY, {"x-region": "ap-south-1"})
    t = R.AsyncRelayTransport(relay)
    t._http = httpx.AsyncClient(transport=httpx.MockTransport(stand_in_relay(seen, **kw)))
    t._direct = httpx.MockTransport(lambda req: (direct.append(str(req.url)), httpx.Response(200, text="direct"))[1])
    return t


class FakeDB:
    """settings as PostgREST would keep it; `race` other machines add 5 calls between our read and our write."""

    def __init__(self, race: int = 0) -> None:
        self.rows: dict[str, str] = {}
        self.race = race
        self.http = self

    @staticmethod
    def _key(q: str) -> str:
        return q.split("key=eq.", 1)[1].split("&", 1)[0]

    def select(self, table: str, q: str) -> list:
        k = self._key(q)
        return [{"value": self.rows[k]}] if k in self.rows else []

    def post(self, path: str, json: dict, headers: dict) -> httpx.Response:
        if json["key"] in self.rows:
            return httpx.Response(409, json={})
        self.rows[json["key"]] = json["value"]
        return httpx.Response(201)

    def patch(self, path: str, params: dict, json: dict, headers: dict) -> httpx.Response:
        k, old, value = params["key"][3:], params["value"][3:], json["value"]
        if self.race:  # another machine adds 5 calls between our read and our write
            self.race -= 1
            cur = _json.loads(self.rows[k])
            self.rows[k] = _json.dumps({"calls": cur["calls"] + 5, "bytes": cur["bytes"]})
        if self.rows.get(k) != old:
            return httpx.Response(200, json=[])
        self.rows[k] = value
        return httpx.Response(200, json=[{"key": k}])

    def _check(self, r: httpx.Response):
        return r.json() if r.content else None


async def _round_trips(check) -> None:
    seen, direct = [], []
    t = transport(seen, direct)
    async with httpx.AsyncClient(transport=t, follow_redirects=True, headers={"User-Agent": "UA-test"}) as c:
        r = await c.get(COURT + "/home")
        check(r.status_code == 200 and r.text.startswith("<html>") and len(r.content) == 2013, "a page comes back whole (gzip undone)")
        sent = json.loads(seen[-1].content)
        check(sent["method"] == "GET" and sent["url"] == COURT + "/home" and sent["gzip"] is True, "the relay gets method, url and asks for gzip")
        check(["user-agent", "UA-test"] in [[k.lower(), v] for k, v in sent["headers"]], "our headers go to the relay")
        check(seen[-1].headers.get("x-relay-key") == KEY and seen[-1].headers.get("x-region") == "ap-south-1",
              "the key and Mumbai region headers are sent")
        check(r.headers.get_list("set-cookie") == ["SERVICES_SESSID=abc; path=/", "JSESSION=xyz; path=/"], "both Set-Cookie headers kept apart")
        check(c.cookies.get("SERVICES_SESSID") == "abc" and c.cookies.get("JSESSION") == "xyz", "both cookies land in the client's jar")
        r = await c.get(COURT + "/go")
        check(r.status_code == 200 and str(r.url) == COURT + "/landing" and len(r.history) == 1, "a redirect is followed by our own client")
        check(all(x in r.text for x in ("SERVICES_SESSID=abc", "JSESSION=xyz", "HOP=1")), "cookies, including one set on the redirect, are sent back")
        r = await c.post(COURT + "/form", data={"party_name": "ICICI Lombard", "captcha": "a1b2c3"})
        check(r.text == "POST application/x-www-form-urlencoded party_name=ICICI+Lombard&captcha=a1b2c3", "a form post arrives as sent")
        r = await c.get(COURT + "/pdf")
        check(r.content == PDF and r.headers["content-type"] == "application/pdf", "binary (a PDF) comes back byte for byte")
        r = await c.get(COURT + "/blocked")
        check(r.status_code == 405 and r.text == "Security Page", "the court's own status passes through (405 block page)")
        n = len(seen)
        r = await c.get("https://api.github.com/x")
        check(r.text == "direct" and direct == ["https://api.github.com/x"] and len(seen) == n, "other addresses go direct, never to the relay")
    check(t.relay.calls == 6 and t.relay.bytes > 0 and t.relay.errors == 0, f"each relay call counted ({t.relay.calls} calls)")

    # Relay refusals and failures, as the court library sees them.
    for status, info, want, what in [
            (401, {"error": "unauthorised"}, R.RelayError, "a wrong key stops (not retried)"),
            (403, {"error": "host not allowed"}, R.RelayError, "an address off the allow-list stops"),
            (413, {"error": "too large"}, R.RelayError, "an answer too large for the relay stops"),
            (502, {"error": "upstream: TimeoutError", "timeout": True}, httpx.ConnectTimeout, "the court not answering is a ConnectTimeout, retried"),
            (502, {"error": "upstream: ECONNRESET"}, httpx.ConnectError, "a refused connection is a ConnectError, retried"),
            (504, {"error": "gateway"}, httpx.ReadTimeout, "the relay timing out is a ReadTimeout, retried"),
            (546, {"error": "worker limit"}, httpx.ConnectError, "the platform failing is a ConnectError, retried")]:
        t = transport([], [], error=(status, info))
        async with httpx.AsyncClient(transport=t) as c:
            try:
                await c.get(COURT + "/home")
                got = None
            except Exception as exc:
                got = exc
        check(isinstance(got, want) and t.relay.errors == 1, f"{what} ({type(got).__name__})")
    # The real allow-list.
    ok = ["services.ecourts.gov.in", "hcservices.ecourts.gov.in", "ecourts.gov.in"]
    bad = ["ecourts.gov.in.evil.com", "notecourts.gov.in", "example.com", "api.ipify.org"]
    check(all(R.ALLOWED.search(h) for h in ok) and not any(R.ALLOWED.search(h) for h in bad), "allow-list: eCourts hosts only")


def _sync_round_trip(check) -> None:
    seen = []
    relay = R.Relay("vercel", "https://relay.test/relay", KEY)
    t = R.RelayTransport(relay)
    t._http = httpx.Client(transport=httpx.MockTransport(stand_in_relay(seen)))
    with httpx.Client(transport=t, follow_redirects=True) as c:
        r = c.get(COURT + "/go")
        check(r.status_code == 200 and "HOP=1" in r.text and c.get(COURT + "/pdf").content == PDF, "the sync transport does the same")


def _choice(check) -> None:
    saved = {k: os.environ.get(k) for k in ("ECOURTS_RELAY", "ECOURTS_RELAY_KEY", "SUPABASE_URL")}
    usage = R.usage
    try:
        os.environ["SUPABASE_URL"] = "https://abc.supabase.co"
        os.environ["ECOURTS_RELAY_KEY"] = KEY
        for blank in ("", "off", "none"):
            os.environ["ECOURTS_RELAY"] = blank
            check(R.parse(blank, KEY) is None and not R.enabled(), f"ECOURTS_RELAY={blank!r}: no relay")
        s, v = R.parse("supabase", KEY), R.parse("vercel", KEY)
        check(s.url == "https://abc.supabase.co/functions/v1/ecourts-relay" and s.headers == {"x-region": "ap-south-1"}, "supabase: the project's function, in Mumbai")
        check(v.url == R.VERCEL_URL and v.name == "vercel", "vercel: haazir-relay")
        check(R.parse("https://x.supabase.co/functions/v1/r", KEY).name == "supabase" and R.parse("https://r.vercel.app/api/relay", KEY).name == "vercel"
              and R.parse("https://relay.example.in/r", KEY).name == "custom", "a URL is counted against the right relay")
        try:
            R.parse("supabase", "")
            check(False, "a relay without its key is refused")
        except R.RelayError:
            check(True, "a relay without its key is refused")

        # Quota: 80% of calls or of bytes hands the run to the other relay; both past 80% goes direct.
        make = lambda name: R.parse(name, KEY)
        lim = R.LIMITS
        check(R.pick(s, {"supabase": {"calls": 399_999, "bytes": 0}}, make)[0].name == "supabase", "supabase under 80%: kept")
        check(R.pick(s, {"supabase": {"calls": 400_000, "bytes": 0}}, make)[0].name == "vercel", "supabase at 80% of calls: vercel instead")
        check(R.pick(s, {"supabase": {"calls": 0, "bytes": int(0.8 * lim['supabase']['bytes'])}}, make)[0].name == "vercel",
              "supabase at 80% of its transfer: vercel instead")
        check(R.pick(v, {"vercel": {"calls": 800_000, "bytes": 0}}, make)[0].name == "supabase", "vercel at 80%: supabase instead")
        both = {"supabase": {"calls": 450_000, "bytes": 0}, "vercel": {"calls": 900_000, "bytes": 0}}
        relay, why = R.pick(s, both, make)
        check(relay is None and "direct" in why, "both past 80%: no relay, said plainly")
        check(R.pick(s, {}, make)[0] is s, "totals unreadable: the asked-for relay")

        # Sticky: decided once per run, whatever changes later.
        R.usage = lambda db=None: {}
        R._reset_for_tests()
        os.environ["ECOURTS_RELAY"] = "supabase"
        first = R.chosen()
        os.environ["ECOURTS_RELAY"] = "vercel"
        check(first is not None and first.name == "supabase" and R.chosen() is first, "a run keeps its first relay")
        R._reset_for_tests()
        R.usage = lambda db=None: {"vercel": {"calls": 900_000, "bytes": 0}}
        check(R.chosen().name == "supabase", "a run starting with vercel past 80% takes supabase")
        R._reset_for_tests()
        os.environ["ECOURTS_RELAY"] = ""
        check(R.chosen() is None and R.summary() == "", "no ECOURTS_RELAY: no relay, nothing in the log")

        # With and without the relay, as the court readers build their clients.
        from bharat_courts.config import BharatCourtsConfig
        from bharat_courts.http import RateLimitedClient
        rlc = RateLimitedClient(BharatCourtsConfig())
        R.attach(rlc)
        c = rlc._ensure_client()
        check(not isinstance(c._transport, (R.AsyncRelayTransport,)) and c.headers["X-Requested-With"] == "XMLHttpRequest",
              "no ECOURTS_RELAY: the library's own client, untouched")
        R._reset_for_tests()
        os.environ["ECOURTS_RELAY"] = "vercel"
        R.usage = lambda db=None: {}
        rlc = RateLimitedClient(BharatCourtsConfig())
        R.attach(rlc)
        c = rlc._ensure_client()
        check(isinstance(c._transport, R.AsyncRelayTransport) and c.headers["X-Requested-With"] == "XMLHttpRequest"
              and c.follow_redirects and rlc._ensure_client() is c, "with the relay: the library's headers, redirects followed, one client")
        from .ecourts import ECourts
        from .highcourt import HighCourt
        ec, hc = ECourts(), HighCourt()
        check(isinstance(ec.client._http._ensure_client()._transport, R.AsyncRelayTransport)
              and isinstance(hc.client._http._ensure_client()._transport, R.AsyncRelayTransport), "district and High Court readers both use it")
        R._reset_for_tests()
        os.environ["ECOURTS_RELAY"] = ""
        ec = ECourts()
        check(not isinstance(ec.client._http._ensure_client()._transport, R.AsyncRelayTransport), "district reader without ECOURTS_RELAY: direct")
    finally:
        R.usage = usage
        R._reset_for_tests()
        for k, val in saved.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val


def _totals(check) -> None:
    db = FakeDB()
    key = R.month_key("supabase")
    check(R.add_usage(db, key, 3, 100) and json.loads(db.rows[key]) == {"calls": 3, "bytes": 100}, "the month's first calls start its total")
    db.race = 1
    check(R.add_usage(db, key, 2, 50) and json.loads(db.rows[key]) == {"calls": 10, "bytes": 150},
          "two machines adding at once: neither count is lost")
    db.race = 99
    check(not R.add_usage(db, key, 1, 1, tries=3), "a total that keeps changing gives up after a few tries (the run goes on)")
    check(key.startswith("relay_calls:") and key.endswith(":supabase") and len(key.split(":")[1]) == 7, "totals kept per month and relay")
    # Batches: the total is written every FLUSH_EVERY calls of the run's own relay, and the rest at the end.
    written = []
    add = R.add_usage
    R.add_usage = lambda db, key, calls, nbytes, tries=8: written.append((calls, nbytes)) or True
    R._db = lambda: None
    try:
        relay = R.Relay("supabase", "u", KEY)
        R._reset_for_tests(relay, decided=True)
        for _ in range(R.FLUSH_EVERY + 5):
            R._count(relay, 10)
        check(written == [(R.FLUSH_EVERY, R.FLUSH_EVERY * 10)] and relay.unflushed == [5, 50], f"written in batches of {R.FLUSH_EVERY}")
        R._at_exit(relay)
        check(written[-1] == (5, 50) and relay.unflushed == [0, 0], "the rest written when the run ends")
        check(R.summary(relay) == f"eCourts relay supabase: {R.FLUSH_EVERY + 5} calls, 0.0 MB", "the run's log line")
    finally:
        R.add_usage = add
        R._db = _real_db
        R._reset_for_tests()


_real_db = R._db


def run(check) -> None:
    asyncio.run(_round_trips(check))
    _sync_round_trip(check)
    _choice(check)
    _totals(check)


def main() -> int:
    failed, passed = [], [0]

    def check(ok: bool, what: str) -> None:
        if ok:
            passed[0] += 1
        else:
            failed.append(what)
            print(f"FAIL {what}")

    run(check)
    print(f"{passed[0]} of {passed[0] + len(failed)} relay checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
