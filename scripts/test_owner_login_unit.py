#!/usr/bin/env python3
"""The owner login's rules that an end-to-end run cannot pin down reliably:
races, and inputs that used to raise instead of refusing.

  1. A burst of wrong sign-ins from one client gets five tries, not one per
     request: every request used to pass the lockout check before the first
     failure was recorded.
  2. A sign-out that lands while a request is checking the same session is
     not undone by that request caching the session as live for 30 s.
  3. A non-ASCII email or setup code is a refusal, never a TypeError (str
     hmac.compare_digest takes ASCII only), and an owner whose email is not
     ASCII can sign in.
  4. The session cookie is found next to a cookie SimpleCookie rejects.
  5. A WebSocket carrying the owner's session is refused from another origin,
     including another port of the same host.
  6. The cookie's name carries the Host's own suffix once there is an owner.
  7. Behind CSERIES_TRUSTED_PROXIES the wrong-password backoff is keyed on
     the visitor, not the proxy; X-Forwarded-For from anyone else is ignored.
  8. The loopback lane: its Host must name this machine and an Origin must
     carry the dashboard's port.

No database or container: the pool is a stand-in.

    python3 scripts/test_owner_login_unit.py        (from the repo root, with
                                                     the image's Python deps)
"""
import asyncio
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from server import owner_login as L  # noqa: E402
from server.middleware import owner_session as S  # noqa: E402
from server.middleware import localhost_only as G  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


class Pool:
    def __init__(self, row=None, session=None):
        self.row = row
        self.session = session
        self.gate = None          # an Event the session read waits on
        self.reading = asyncio.Event()
        self.executed = []

    async def fetchrow(self, sql, *args):
        if "owner_sessions" in sql:
            self.reading.set()
            if self.gate is not None:
                await self.gate.wait()
            return self.session
        return self.row

    async def fetchval(self, sql, *args):
        return args[0] if "INSERT INTO owner_account" in sql else None

    async def execute(self, sql, *args):
        # asyncpg answers with the command status.
        self.executed.append(sql)
        return "DELETE 1" if sql.startswith("DELETE") else "UPDATE 1"


def reset(pool):
    L._pool = lambda: pool
    L._cache.clear()
    L._touched.clear()
    L._failures.clear()


async def burst():
    pool = Pool(row={"email": "owner@example.com", "password_hash": "x"})
    reset(pool)

    async def slow_wrong(password, stored):
        await asyncio.sleep(0.05)   # the hash takes time; the burst lands inside it
        return False
    real = L.check_password
    L.check_password = slow_wrong
    try:
        results = await asyncio.gather(
            *(L.sign_in("owner@example.com", "guess", "198.51.100.7", "h") for _ in range(12)),
            return_exceptions=True)
    finally:
        L.check_password = real
    wrong = sum(1 for r in results if isinstance(r, L.LoginError) and "Wrong" in str(r))
    held = sum(1 for r in results if isinstance(r, L.LoginError) and "Too many" in str(r))
    check(wrong == 5 and held == 7,
          "a burst of 12 wrong sign-ins from one client checks 5 passwords, refuses 7",
          f"(checked {wrong}, refused {held}: {results!r})")


async def sign_out_race():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    row = {"last_seen_at": now, "expires_at": now + L.SESSION_MAX, "origin": "h",
           "parent": None, "parent_seen": None, "parent_expires": None}
    pool = Pool(session=dict(row))
    pool.gate = asyncio.Event()
    reset(pool)
    L._owner_email = "owner@example.com"
    L._touched[L._token_hash("tok")] = __import__("time").monotonic()   # no touch write
    check_task = asyncio.create_task(L.session_email("tok", "h"))
    await pool.reading.wait()            # the check has read the row ...
    await L.sign_out("tok")              # ... the owner signs out ...
    pool.gate.set()                      # ... and the check finishes
    got = await check_task
    check(got is None, "a check racing a sign-out does not report the session live",
          f"(got {got!r})")
    pool.gate = None
    pool.session = dict(row)                                             # stale read
    again = await L.session_email("tok", "h")
    check(again is None, "a signed-out session is not served from the cache afterwards",
          f"(got {again!r})")

    # A session counts only on the host it was issued on, from the cache too.
    reset(pool)
    pool.session = dict(row)
    L._signed_out.clear()
    on_h = await L.session_email("tok2", "h")
    elsewhere = await L.session_email("tok2", "apps-x-h")
    check(on_h == "owner@example.com" and elsewhere is None,
          "a session is live on its own host and refused on any other, cached or not",
          f"(on h {on_h!r}, elsewhere {elsewhere!r})")


async def non_ascii():
    pool = Pool(row={"email": "owner@example.com", "password_hash": "x"})
    reset(pool)

    async def wrong(password, stored):
        return False
    real = L.check_password
    L.check_password = wrong
    try:
        try:
            await L.sign_in("öwner@example.com", "pw", "198.51.100.8", "h")
            outcome = "signed in"
        except L.LoginError:
            outcome = "refused"
        except Exception as e:  # noqa: BLE001
            outcome = repr(e)
        check(outcome == "refused", "a non-ASCII email at sign-in is a refusal", f"({outcome})")
    finally:
        L.check_password = real

    L._owner_email, L._setup_code = None, "abc123-def456-789abc"
    try:
        await L.create_owner("çode", "owner@example.com", "x" * 20)
        outcome = "created"
    except L.LoginError:
        outcome = "refused"
    except Exception as e:  # noqa: BLE001
        outcome = repr(e)
    check(outcome == "refused", "a non-ASCII setup code is a refusal", f"({outcome})")

    # An owner whose email is not ASCII signs in.
    pool = Pool(row={"email": "jösé@example.com",
                     "password_hash": await L.hash_password("a long password here")})
    reset(pool)
    try:
        tok = await L.sign_in("Jösé@Example.com", "a long password here", "198.51.100.9", "h")
        outcome = "signed in" if tok else "no token"
    except Exception as e:  # noqa: BLE001
        outcome = repr(e)
    check(outcome == "signed in", "an owner with a non-ASCII email can sign in", f"({outcome})")


def cookies():
    scope = {"headers": [(b"cookie", b'prefs={"a":1}; theme=dark mode; cseries_session=abc')]}
    check(S._cookie(scope) == "abc", "the session cookie is found beside cookies SimpleCookie rejects",
          f"(got {S._cookie(scope)!r})")
    scope = {"headers": [(b"cookie", b"a=1"), (b"cookie", b"cseries_session=def")]}
    check(S._cookie(scope) == "def", "the session cookie is found in a second Cookie header")
    scope = {"headers": [(b"cookie", b"xcseries_session=1; cseries_sessionx=2")]}
    check(S._cookie(scope) is None, "a cookie whose name only contains the session's is not it")


def cookie_suffix():
    saved = L._cookie_suffix
    try:
        L._cookie_suffix = "0a1b2c3d"
        check(L.cookie_name() == "cseries_session_0a1b2c3d", "the cookie is named for this Host")
        scope = {"headers": [(b"cookie", b"cseries_session=other; cseries_session_0a1b2c3d=mine")]}
        check(S._cookie(scope) == "mine", "a second Host's cookie on the same machine is not read")
    finally:
        L._cookie_suffix = saved


def trusted_proxies():
    from server.routes import owner as R

    class Req:
        def __init__(self, peer, xff=None):
            self.client = type("C", (), {"host": peer})()
            self.headers = {"x-forwarded-for": xff} if xff else {}

    saved = os.environ.get("CSERIES_TRUSTED_PROXIES")
    try:
        os.environ.pop("CSERIES_TRUSTED_PROXIES", None)
        check(R._client(Req("172.18.0.1", "203.0.113.5")) == "172.18.0.1",
              "with no trusted proxy, X-Forwarded-For is ignored")
        os.environ["CSERIES_TRUSTED_PROXIES"] = "172.18.0.0/16"
        check(R._client(Req("172.18.0.1", "203.0.113.5")) == "203.0.113.5",
              "from a trusted proxy, the visitor's address keys the backoff")
        check(R._client(Req("172.18.0.1", "198.51.100.7, 203.0.113.5")) == "203.0.113.5",
              "the nearest untrusted hop is used, not one the visitor wrote")
        check(R._client(Req("192.0.2.9", "203.0.113.5")) == "192.0.2.9",
              "from an untrusted peer, X-Forwarded-For is ignored")
    finally:
        if saved is None:
            os.environ.pop("CSERIES_TRUSTED_PROXIES", None)
        else:
            os.environ["CSERIES_TRUSTED_PROXIES"] = saved


def loopback_lane():
    check(G.loopback_host_ok("127.0.0.1:8800") and G.loopback_host_ok("localhost")
          and G.loopback_host_ok("[::1]:8800"), "loopback Host naming this machine passes")
    check(not G.loopback_host_ok("rebound.example:8800") and not G.loopback_host_ok(None),
          "loopback Host naming anything else is refused")
    check(G.origin_allowed("http://localhost:8800", "127.0.0.1:8800", own_origin=False),
          "a loopback Origin on the dashboard's port passes")
    check(not G.origin_allowed("http://localhost:3000", "127.0.0.1:8800", own_origin=False),
          "a loopback Origin on another port is refused")
    # The Host's own in-process calls (kernel_op's ASGI client, the loopback
    # base apps and sidecars are given) must pass that same rule.
    import re
    from server import loopback
    src = open(os.path.join(ROOT, "brain/mastercode/runtime/kernel_ops.py")).read()
    bases = re.findall(r'base_url="http://([^"/]+)"', src)
    check(bases and all(G.loopback_host_ok(b) for b in bases),
          "kernel_op calls the Host under a name the loopback rule passes", bases)
    check(G.loopback_host_ok(loopback.KERNEL_LOOPBACK_BASE.split("//", 1)[1]),
          "the loopback base given to apps and sidecars passes it too", loopback.KERNEL_LOOPBACK_BASE)


def ws_origin():
    def scope(origin):
        h = [(b"host", b"127.0.0.1:8800")]
        if origin is not None:
            h.append((b"origin", origin))
        return {"type": "websocket", "path": "/ws", "client": ("172.18.0.1", 5000),
                "headers": h, "state": {"owner_session": True}}
    check(G.websocket_refusal(scope(b"http://127.0.0.1:8800")) is None,
          "owner WebSocket from the dashboard's own origin opens")
    check(G.websocket_refusal(scope(None)) is None,
          "owner WebSocket with no Origin (not a browser) opens")
    check(G.websocket_refusal(scope(b"http://evil.example")) is not None,
          "owner WebSocket from another site is refused")
    check(G.websocket_refusal(scope(b"http://127.0.0.1:3000")) is not None,
          "owner WebSocket from another port of the same host is refused")
    check(G.websocket_refusal(scope(b"null")) is not None,
          "owner WebSocket from an opaque origin is refused")


async def main():
    await burst()
    await sign_out_race()
    await non_ascii()
    cookies()
    cookie_suffix()
    trusted_proxies()
    loopback_lane()
    ws_origin()


asyncio.run(main())
sys.exit(failed)
