#!/usr/bin/env python3
"""The owner of a C-Series Cloud Host, made and signed in from the machine's
unlock page (server/owner_login.py claim_owner, box_sign_in; the routes
/api/owner/claim and /api/owner/box-sign-in in server/routes/owner.py).

  1. The ticket the box writes (the box's own cseries_machine.
     write_owner_claim) is the one the Host reads: a claim ticket posted as a
     form from the unlock page's origin makes the owner with the email the
     box holds, no password, signs them in (the Host's __Host- cookie,
     SameSite=Strict) and answers the desktop by a 303.
  2. A ticket is taken once: the same claim again, or a claim once there is
     an owner, is refused. A sign-in ticket signs the owner in once; a new
     one works, the used one again does not.
  3. Refused, each with a page that leads back to the unlock page and no
     cookie: another Origin (another site, an app of the Host, none), another
     host name than the Host's own, a ticket of the other kind, a wrong
     ticket, an expired one, a Host not on a cloud machine with a vault, a
     claim before the disk's keys are set up. Wrong tickets count like wrong
     passwords: five, then a wait.
  4. The gate lets the two routes through without a session and without its
     own Origin rule, which would refuse the unlock page's origin; every other
     public route keeps that rule.
  5. An owner without a password: a password sign-in says to use the unlock
     page and runs no hash; setup with the setup code is refused on a cloud
     machine with a vault, even with the right code; a session from the
     unlock page may start adding the Host's first passkey, as after a
     password.
  6. /setup and /login on a cloud machine point to its unlock page; /login
     offers the Host's own passkey or the password when the owner has one.

No database or container: the pool is a stand-in.

    python3 scripts/test_owner_box_claim_unit.py   (repo root, image's deps)
"""
import json
import os
import sys
import tempfile
import time

os.environ["CSERIES_URL"] = "https://orchard.k7x2m9.cloud.privatae.ai"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "vm/cloud/overlay/usr/local/lib/cseries-cloud")]

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import cseries_cloud as cc  # noqa: E402  (the box's own library)
import cseries_machine as cm  # noqa: E402
from server import cloud_machine as M  # noqa: E402
from server import owner_login as L  # noqa: E402
from server import owner_passkeys as P  # noqa: E402
from server.middleware import localhost_only as G  # noqa: E402
from server.routes import owner as R  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


HOST = "orchard.k7x2m9.cloud.privatae.ai"
UNLOCK = "https://unlock-orchard.k7x2m9.cloud.privatae.ai"
SIBLING = "https://apps-3f9a1c-orchard.k7x2m9.cloud.privatae.ai"   # invented app origin


class Pool:
    """owner_account and owner_sessions, as far as these routes use them."""

    def __init__(self):
        self.owner = None
        self.sessions = []
        self.passkeys = 0

    async def fetchval(self, sql, *a):
        if "INSERT INTO owner_account" in sql:
            if self.owner is not None:
                return None
            self.owner = {"email": a[0], "password_hash": None, "cookie_suffix": a[1], "box_ticket_hash": a[2]}
            return a[0]
        if "UPDATE owner_account SET box_ticket_hash" in sql:
            if self.owner is not None and self.owner["box_ticket_hash"] != a[0]:
                self.owner["box_ticket_hash"] = a[0]
                return 1
            return None
        if "password_hash IS NOT NULL" in sql:
            return bool(self.owner and self.owner["password_hash"])
        if "FROM owner_passkeys" in sql:
            return self.passkeys if "count" in sql.lower() else self.passkeys > 0
        return None

    async def fetchrow(self, sql, *a):
        if "FROM owner_account" in sql:
            return dict(self.owner) if self.owner else None
        return None

    async def fetch(self, sql, *a):
        return []

    async def execute(self, sql, *a):
        if "INSERT INTO owner_sessions" in sql:
            self.sessions.append(a)
        return "OK"


def reset(pool):
    L._pool = lambda: pool
    L._owner_email = pool.owner["email"] if pool.owner else None
    L._cookie_suffix = pool.owner["cookie_suffix"] if pool.owner else None
    L._setup_code = None if pool.owner else "0a1b2c-3d4e5f-6a7b8c"
    L._failures.clear()


def machine(root, enrolled=True, vault="open"):
    door = os.path.join(root, cm.DOOR_DIR)
    os.makedirs(door, exist_ok=True)
    with open(os.path.join(door, M.STATE), "w") as f:
        json.dump({"vault": {"state": vault, "since": None, "enrolled": enrolled if vault != "absent" else None},
                   "unlock_host": "unlock-orchard.k7x2m9.cloud.privatae.ai",
                   "access_log": {"count": 0, "head": "0" * 64, "mismatch": False},
                   "patches": {"reboot_required": False}}, f)
    os.environ[M.DIR_ENV] = door
    return cc.Paths(root)


def ticket_for(paths, kind, email="owner@example.com", ttl=120, now=None):
    t = "invented-ticket-" + kind + "-" + str(time.monotonic_ns())
    cm.write_owner_claim(paths, kind, email, t, ttl, now=now)
    return t


app = FastAPI()
app.include_router(R.router)
app.add_middleware(G.LocalhostOnlyMiddleware)
client = TestClient(app, base_url="https://" + HOST, follow_redirects=False)


def post(route, ticket, origin=UNLOCK, host=HOST):
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Host": host}
    if origin is not None:
        headers["Origin"] = origin
    return client.post(route, content=f"ticket={ticket}", headers=headers)


def session_cookie(r):
    return [v for k, v in r.headers.multi_items() if k == "set-cookie" and "cseries_session" in v]


with tempfile.TemporaryDirectory() as root:
    paths = machine(root)
    pool = Pool()
    reset(pool)

    # 1. the claim
    t = ticket_for(paths, "claim")
    raw = open(os.path.join(root, cm.DOOR_DIR, cm.OWNER_CLAIM_NAME)).read()
    check(t not in raw, "the box keeps the ticket's hash only")
    r = post("/api/owner/claim", t)
    cookies = session_cookie(r)
    check(r.status_code == 303 and r.headers.get("location") == "/" and pool.owner is not None and
          pool.owner["email"] == "owner@example.com" and pool.owner["password_hash"] is None and
          L.owner_email() == "owner@example.com",
          "a claim ticket from the unlock page makes the owner with the box's email, no password",
          (r.status_code, r.text[:200], pool.owner))
    check(len(cookies) == 1 and cookies[0].startswith("__Host-cseries_session_") and "Secure" in cookies[0] and
          "HttpOnly" in cookies[0] and "SameSite=strict" in cookies[0] and "Path=/" in cookies[0] and
          len(pool.sessions) == 1 and pool.sessions[0][3] == HOST,
          "and signs them in on the Host's own name, its __Host- cookie, SameSite=Strict", cookies)
    tok = cookies[0].split(";")[0].split("=", 1)[1] if cookies else ""
    check(P.fresh_step_up(tok, P.SIGNED_IN) and not P.fresh_step_up(tok),
          "that session may start adding the first passkey (as after a password), and is no passkey check")

    # 2. once
    r = post("/api/owner/claim", t)
    check(r.status_code == 403 and not session_cookie(r) and "already has its owner" in r.text,
          "the same claim ticket again is refused", r.status_code)
    s1 = ticket_for(paths, "sign-in", email=None)
    r = post("/api/owner/box-sign-in", s1)
    check(r.status_code == 303 and len(session_cookie(r)) == 1 and len(pool.sessions) == 2,
          "a sign-in ticket signs the owner in", r.status_code)
    r = post("/api/owner/box-sign-in", s1)
    check(r.status_code == 403 and not session_cookie(r) and "used already" in r.text,
          "the same sign-in ticket again is refused", r.status_code)
    s2 = ticket_for(paths, "sign-in", email=None)
    r = post("/api/owner/box-sign-in", s2)
    check(r.status_code == 303 and len(session_cookie(r)) == 1, "a new ticket works", r.status_code)
    c3 = ticket_for(paths, "claim", email="someone@example.com")
    r = post("/api/owner/claim", c3)
    check(r.status_code == 403 and pool.owner["email"] == "owner@example.com",
          "a claim once the Host has its owner is refused, and changes nothing", r.status_code)

    # 3. refusals
    L._failures.clear()
    s = ticket_for(paths, "sign-in", email=None)
    before = len(pool.sessions)
    for origin, why in (("https://privatae.ai", "the account site"), (SIBLING, "an app of the Host"),
                        ("null", "a form with no referrer"), (None, "no Origin")):
        r = post("/api/owner/box-sign-in", s, origin=origin)
        check(r.status_code == 403 and not session_cookie(r) and UNLOCK + "/" in r.text,
              f"from {why}: refused, with the way back to the unlock page", (r.status_code, r.text[-300:]))
    r = post("/api/owner/box-sign-in", s, host=SIBLING[len("https://"):])
    check(r.status_code == 404 and not session_cookie(r), "on another name than the Host's own: refused",
          r.status_code)
    r = post("/api/owner/claim", s)
    check(r.status_code == 403 and len(pool.sessions) == before, "a sign-in ticket does not claim", r.status_code)
    c = ticket_for(paths, "claim")
    r = post("/api/owner/box-sign-in", c)
    check(r.status_code == 403 and len(pool.sessions) == before, "nor a claim ticket sign in", r.status_code)
    old = ticket_for(paths, "sign-in", email=None, ttl=120, now=time.time() - 600)
    r = post("/api/owner/box-sign-in", old)
    check(r.status_code == 403 and "too long" in r.text, "an expired ticket is refused", r.status_code)
    L._failures.clear()
    ticket_for(paths, "sign-in", email=None)
    codes = [post("/api/owner/box-sign-in", f"invented-wrong-{i}").status_code for i in range(5)]
    r = post("/api/owner/box-sign-in", "invented-wrong-5")
    check(codes == [403] * 5 and "Too many wrong tries" in r.text,
          "wrong tickets count like wrong passwords: five, then a wait", (codes, r.text[-200:]))
    L._failures.clear()

    # 4. the gate
    check(G.OWNER_BOX_PATHS <= G.OWNER_PUBLIC_PATHS and
          G.OWNER_BOX_PATHS == {"/api/owner/claim", "/api/owner/box-sign-in"},
          "the two routes need no session (the door passes them too)")
    r = client.post("/api/owner/login", json={"email": "x@example.com", "password": "y"},
                    headers={"Origin": UNLOCK, "Host": HOST})
    check(r.status_code == 403 and "invalid origin" in r.text,
          "every other public route keeps the gate's Origin rule (the unlock page's origin is refused there)",
          (r.status_code, r.text[:200]))

    # 5. no password
    L._failures.clear()
    started = time.monotonic()
    try:
        import asyncio
        asyncio.run(L.sign_in("owner@example.com", "a password of some length", "198.51.100.20", HOST))
        check(False, "a password sign-in for an owner without one is refused")
    except L.LoginError as e:
        check("unlock page" in str(e) and time.monotonic() - started < 0.1,
              "a password sign-in for an owner without one says to use the unlock page, and hashes nothing",
              str(e))

    # 6. the pages
    r = client.get("/login", headers={"Host": HOST})
    check(r.status_code == 200 and f'href="{UNLOCK}/"' in r.text and 'id="password"' not in r.text and
          'id="passkey"' not in r.text,
          "/login on a cloud machine: its unlock page; no password form for an owner without one", r.text[-400:])
    pool.owner["password_hash"] = "scrypt$invented"
    r = client.get("/login", headers={"Host": HOST})
    check(f'href="{UNLOCK}/"' in r.text and 'id="password"' in r.text,
          "an owner with a password from before gets the password form too")
    pool.owner["password_hash"] = None
    pool.passkeys = 1
    saved = R._passkeys_and_codes

    async def one():
        return 1, 3
    R._passkeys_and_codes = one
    try:
        r = client.get("/login", headers={"Host": HOST})
    finally:
        R._passkeys_and_codes = saved
    pool.passkeys = 0
    check(f'href="{UNLOCK}/"' in r.text and 'id="passkey"' in r.text,
          "an owner with a Host passkey gets that button too")
    check("#code=" not in R._SCRIPT and "replaceState" not in R._SCRIPT,
          "the pages' script reads no code from an address")

with tempfile.TemporaryDirectory() as root:
    paths = machine(root, enrolled=False)
    pool = Pool()
    reset(pool)
    r = client.get("/setup", headers={"Host": HOST})
    check("Set up your machine" in r.text and f'href="{UNLOCK}/"' in r.text and "/api/owner/setup" not in r.text,
          "/setup before the disk's keys: the account site's button and the unlock page, no form")
    t = ticket_for(paths, "claim")
    r = post("/api/owner/claim", t)
    check(r.status_code == 403 and pool.owner is None, "no claim before the disk's keys are set up", r.status_code)
    paths = machine(root, enrolled=True)
    r = client.get("/setup", headers={"Host": HOST})
    check(f'href="{UNLOCK}/"' in r.text and "/api/owner/setup" not in r.text and "npx" not in r.text,
          "/setup once they are: the unlock page makes the owner; no code form, no command")
    import asyncio
    try:
        asyncio.run(L.create_owner("0a1b2c-3d4e5f-6a7b8c", "owner@example.com", "x" * 20, client="198.51.100.30"))
        check(False, "the setup code makes no owner on a cloud machine with a vault")
    except L.LoginError as e:
        check(pool.owner is None and UNLOCK in str(e),
              "the setup code, right as it is, makes no owner on a cloud machine with a vault", str(e))
    paths = machine(root, vault="absent")
    os.unlink(os.path.join(root, cm.DOOR_DIR, cm.OWNER_CLAIM_NAME))
    t = ticket_for(paths, "claim")
    r = post("/api/owner/claim", t)
    check(r.status_code == 404 and pool.owner is None,
          "a machine without a vault (no unlock page) takes no ticket", r.status_code)
    os.environ.pop(M.DIR_ENV, None)
    r = post("/api/owner/claim", t)
    check(r.status_code == 404 and pool.owner is None, "nor a Host with no machine", r.status_code)

sys.exit(failed)
