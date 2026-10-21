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
  9. On an https Host (CSERIES_URL) the cookie is __Host- named and always
     Secure, Path=/, no Domain; sign-out clears it with the same attributes.
     On a plain-http Host it is as before, and X-Forwarded-Proto adds Secure
     only from a trusted proxy.
 10. The wrong-try record stays bounded: an IPv6 visitor counts by its /64,
     an entry is forgotten an hour after its wait ends, and past the cap the
     oldest go.
 11. A wrong setup code counts like a wrong password.
 12. CSERIES_SETUP_CODE_FILE gives the setup code when it holds one of the
     usual shape; otherwise, or once an owner exists, it is not used.
 13. Every visitor's wrong passwords count together: past GLOBAL_FREE of
     them, from as many addresses, every password sign-in waits, without a
     hash, the right password included; the wait doubles to 15 minutes and is
     forgotten an hour after it ends. Before there is an owner nothing counts
     there.
 14. The setup page names npx cseries open (which prints the setup code on a
     machine installed with npx) before docker compose logs host.

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
    L._password_failures = (0, 0.0, 0.0)


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



def _request(peer, headers=(), cookie=None, scheme="http"):
    from starlette.requests import Request
    h = [(k.encode(), v.encode()) for k, v in headers]
    if cookie:
        h.append((b"cookie", cookie.encode()))
    return Request({"type": "http", "method": "POST", "path": "/api/owner/logout", "scheme": scheme,
                    "headers": h, "client": (peer, 5000), "server": ("10.88.0.5", 8800),
                    "query_string": b"", "root_path": ""})


def _set_cookies(resp):
    return [v.decode() for k, v in resp.raw_headers if k == b"set-cookie"]


async def https_cookie():
    from fastapi.responses import JSONResponse
    from server import home_origin
    from server.routes import owner as R
    saved = (L.HOME, L._cookie_suffix, os.environ.get("CSERIES_TRUSTED_PROXIES"))
    try:
        os.environ.pop("CSERIES_TRUSTED_PROXIES", None)
        L.HOME = home_origin.parse("https://home.box.example")
        L._cookie_suffix = None
        check(L.cookie_name() == "__Host-cseries_session", "an https Host's cookie is __Host- named",
              L.cookie_name())
        L._cookie_suffix = "0a1b2c3d"
        check(L.cookie_name() == "__Host-cseries_session_0a1b2c3d", "and carries the Host's suffix")
        sc = _set_cookies(R._with_cookie(JSONResponse({}), "tok", _request("203.0.113.5")))
        parts = [p.strip().lower() for p in sc[0].split(";")] if sc else []
        check(len(sc) == 1 and sc[0].startswith("__Host-cseries_session_0a1b2c3d=tok;")
              and "secure" in parts and "path=/" in parts and "httponly" in parts
              and "samesite=strict" in parts and not any(p.startswith("domain") for p in parts),
              "on an https Host the cookie is Secure, Path=/, HttpOnly, SameSite=Strict, no Domain", sc)
        pool = Pool()
        reset(pool)
        resp = await R.owner_sign_out(_request("203.0.113.5", cookie="__Host-cseries_session_0a1b2c3d=tok"))
        sc = _set_cookies(resp)
        parts = [p.strip().lower() for p in sc[0].split(";")] if sc else []
        check(len(sc) == 1 and sc[0].startswith("__Host-cseries_session_0a1b2c3d=")
              and "secure" in parts and "path=/" in parts and "max-age=0" in parts,
              "sign-out clears it with Secure and Path=/, which a __Host- name needs", sc)

        L.HOME = home_origin.parse("http://home.cseries.localhost:8800")
        check(L.cookie_name() == "cseries_session_0a1b2c3d", "a plain-http Host's cookie is unchanged")
        xfp = [("x-forwarded-proto", "https")]
        sc = _set_cookies(R._with_cookie(JSONResponse({}), "tok", _request("203.0.113.5", xfp)))
        check(sc and "secure" not in sc[0].lower(),
              "X-Forwarded-Proto from a peer that is not a trusted proxy adds no Secure", sc)
        sc = _set_cookies(R._with_cookie(JSONResponse({}), "tok",
                                         _request("203.0.113.5", [("forwarded", "for=x;proto=https")])))
        check(sc and "secure" not in sc[0].lower(), "nor does Forwarded", sc)
        os.environ["CSERIES_TRUSTED_PROXIES"] = "10.88.0.1"
        sc = _set_cookies(R._with_cookie(JSONResponse({}), "tok", _request("10.88.0.1", xfp)))
        check(sc and "secure" in sc[0].lower(), "from a trusted proxy it does", sc)
        sc = _set_cookies(R._with_cookie(JSONResponse({}), "tok", _request("203.0.113.5", scheme="https")))
        check(sc and "secure" in sc[0].lower(), "a request that came in over HTTPS gets Secure", sc)
    finally:
        L.HOME, L._cookie_suffix = saved[0], saved[1]
        if saved[2] is None:
            os.environ.pop("CSERIES_TRUSTED_PROXIES", None)
        else:
            os.environ["CSERIES_TRUSTED_PROXIES"] = saved[2]


def failure_record():
    import time as _time
    L._failures.clear()
    for i in range(5):
        L._note_failure(f"2001:db8:1:2::{i + 1:x}")
    check(L._locked_for("2001:db8:1:2:ffff::9") > 0,
          "five wrong tries from one IPv6 /64, each from another address, lock that /64")
    check(L._locked_for("2001:db8:1:3::1") == 0 and L._locked_for("198.51.100.2") == 0,
          "another /64 and an IPv4 visitor are not locked")
    L._note_failure("::ffff:198.51.100.40")
    check("198.51.100.40" in L._failures, "a v4-mapped address counts as its IPv4 address")

    L._failures.clear()
    old = _time.monotonic() - 2 * L._FAILURE_FORGET_S
    L._failures["198.51.100.50"] = (3, old, old)
    L._note_failure("198.51.100.51")
    check("198.51.100.50" not in L._failures and "198.51.100.51" in L._failures,
          "an entry is forgotten an hour after its wait ended")
    # The same holds for the visitor's own record when nobody else has failed
    # since: one typo weeks after a bad day starts the count afresh.
    L._failures.clear()
    L._failures["198.51.100.52"] = (9, old, old)
    L._note_failure("198.51.100.52")
    check(L._failures["198.51.100.52"][0] == 1 and L._locked_for("198.51.100.52") == 0,
          "a returning visitor's own record an hour past its wait starts again at one try, "
          "with no wait", L._failures.get("198.51.100.52"))

    saved = L._FAILURES_MAX
    try:
        L._FAILURES_MAX = 50
        L._failures.clear()
        for i in range(200):
            L._note_failure(f"198.18.{i // 250}.{i % 250}")
        check(len(L._failures) <= 50 and "198.18.0.199" in L._failures and "198.18.0.0" not in L._failures,
              "past the cap the oldest entries go and the newest stay", len(L._failures))
    finally:
        L._FAILURES_MAX = saved
        L._failures.clear()


async def setup_code_lockout():
    pool = Pool()
    reset(pool)
    L._owner_email, L._setup_code = None, "abc123-def456-789abc"
    results = []
    for _ in range(7):
        try:
            await L.create_owner("000000-000000-000000", "owner@example.com", "x" * 20,
                                 client="198.51.100.60")
            results.append("created")
        except L.LoginError as e:
            results.append("held" if "Too many" in str(e) else "wrong")
    check(results == ["wrong"] * 5 + ["held"] * 2,
          "wrong setup codes count: five tries, then a wait", results)
    try:
        await L.create_owner("abc123-def456-789abc", "owner@example.com", "x" * 20, client="198.51.100.61")
        outcome = "created"
    except L.LoginError as e:
        outcome = str(e)
    check(outcome == "created", "the right code from another visitor still works", outcome)
    L._owner_email, L._setup_code, L._cookie_suffix = None, None, None


async def setup_code_file():
    import tempfile

    class InitPool(Pool):
        def __init__(self, owner=None):
            super().__init__(row=owner)

    saved = os.environ.get("CSERIES_SETUP_CODE_FILE")
    try:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "setup-code")
            with open(path, "w") as f:
                f.write("  ABC123-DEF456-789ABC\n")
            os.environ["CSERIES_SETUP_CODE_FILE"] = path
            reset(InitPool())
            await L.init()
            check(L._setup_code == "abc123-def456-789abc",
                  "the setup code is the file's, stripped and lower-cased", L._setup_code)
            await L.init()
            check(L._setup_code == "abc123-def456-789abc", "and stays the same across restarts")
            with open(path, "w") as f:
                f.write("not-a-code\n")
            await L.init()
            check(L._setup_code != "not-a-code" and bool(L.SETUP_CODE_RE.fullmatch(L._setup_code or "")),
                  "a file without a code of the usual shape: a fresh code instead", L._setup_code)
            os.environ["CSERIES_SETUP_CODE_FILE"] = os.path.join(d, "missing")
            await L.init()
            check(bool(L.SETUP_CODE_RE.fullmatch(L._setup_code or "")), "an unreadable file: a fresh code")
            with open(path, "w") as f:
                f.write("abc123-def456-789abc")
            os.environ["CSERIES_SETUP_CODE_FILE"] = path
            reset(InitPool(owner={"email": "owner@example.com", "cookie_suffix": "0a1b2c3d"}))
            await L.init()
            check(L._setup_code is None and L._owner_email == "owner@example.com",
                  "once there is an owner the file is not used")
    finally:
        if saved is None:
            os.environ.pop("CSERIES_SETUP_CODE_FILE", None)
        else:
            os.environ["CSERIES_SETUP_CODE_FILE"] = saved
        L._owner_email, L._setup_code, L._cookie_suffix = None, None, None


async def password_budget():
    import time as _time
    hashed = []

    async def counting(password, stored):
        hashed.append(password)
        return password == "the right password"
    real = L.check_password
    L.check_password = counting
    try:
        reset(Pool(row={"email": "owner@example.com", "password_hash": "x"}))
        outcomes = []
        for i in range(L.GLOBAL_FREE + 5):
            try:
                await L.sign_in("owner@example.com", "guess", f"198.18.{i // 250}.{i % 250}", "h")
            except L.LoginError as e:
                outcomes.append("held" if "on this Host" in str(e) else "wrong")
        check(outcomes == ["wrong"] * L.GLOBAL_FREE + ["held"] * 5
              and len(hashed) == L.GLOBAL_FREE,
              f"{L.GLOBAL_FREE} wrong passwords from as many addresses, then every password "
              "sign-in waits, with no hash", (outcomes, len(hashed)))
        try:
            await L.sign_in("owner@example.com", "the right password", "203.0.113.200", "h")
            got = "signed in"
        except L.LoginError as e:
            got = str(e)
        check("on this Host" in got, "while it waits the right password from a fresh address waits too",
              got)
        now_m = _time.monotonic()
        L._password_failures = (L.GLOBAL_FREE + 30, now_m - 1, now_m - 1)
        L._note_password_failure()
        check(abs(L._password_locked_for() - 900) < 2, "the wait doubles up to 15 minutes, no further",
              L._password_locked_for())
        old = now_m - 2 * L._FAILURE_FORGET_S
        L._password_failures = (L.GLOBAL_FREE + 30, old, old)
        L._note_password_failure()
        check(L._password_failures[0] == 1 and L._password_locked_for() == 0,
              "an hour after its wait ended the count starts again", L._password_failures)
        L._password_failures = (0, 0.0, 0.0)
        hashed.clear()
        tok = await L.sign_in("owner@example.com", "the right password", "203.0.113.201", "h")
        check(bool(tok) and len(hashed) == 1, "with nothing held the right password signs in")

        # A burst from many addresses at once checks GLOBAL_FREE passwords.
        reset(Pool(row={"email": "owner@example.com", "password_hash": "x"}))
        hashed.clear()
        results = await asyncio.gather(*(L.sign_in("owner@example.com", "guess", f"198.19.0.{i}", "h")
                                         for i in range(L.GLOBAL_FREE + 15)), return_exceptions=True)
        held = sum(1 for r in results if isinstance(r, L.LoginError) and "on this Host" in str(r))
        check(len(hashed) == L.GLOBAL_FREE and held == 15,
              "a burst from many addresses at once checks only the free tries", (len(hashed), held))

        # Before there is an owner no password can be right; nothing counts.
        reset(Pool(row=None))
        for i in range(L.GLOBAL_FREE + 5):
            try:
                await L.sign_in("owner@example.com", "guess", f"198.20.0.{i}", "h")
            except L.LoginError:
                pass
        check(L._password_failures[0] == 0 and L._password_locked_for() == 0,
              "before there is an owner, wrong passwords do not count together", L._password_failures)
    finally:
        L.check_password = real
        L._password_failures = (0, 0.0, 0.0)


async def setup_page_names_npx_first():
    """The setup page says where the setup code is: npx cseries open for a
    machine installed with npx (the command that prints it), before the
    compose log, which stays for compose installs."""
    from server.routes import owner as O
    saved = L._owner_email
    L._owner_email = None
    try:
        page = (await O.setup_page()).body.decode()
    finally:
        L._owner_email = saved
    npx, compose = page.find("<code>npx cseries open</code>"), page.find("<code>docker compose logs host</code>")
    check(npx >= 0, "the setup page names npx cseries open")
    check(compose >= 0, "and still names docker compose logs host")
    check(0 <= npx < compose, "npx first, compose after", f"(npx at {npx}, compose at {compose})")


async def main():
    await setup_page_names_npx_first()
    await burst()
    await password_budget()
    await sign_out_race()
    await non_ascii()
    cookies()
    cookie_suffix()
    trusted_proxies()
    loopback_lane()
    ws_origin()
    await https_cookie()
    failure_record()
    await setup_code_lockout()
    await setup_code_file()


asyncio.run(main())
sys.exit(failed)
