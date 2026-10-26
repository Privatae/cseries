#!/usr/bin/env python3
"""Remote access on the Host: the second address and adding a phone.

  1. CSERIES_REMOTE_URL is refused unless it is https on the default port,
     a name the internet can reach, <handle>.<address_id>.<zone>, outside the
     desktop's zone, with the desktop's own handle.
  2. The remote address signs in by passkey or recovery code only: the
     password is refused there by owner_login.sign_in and by the route, even
     with the right password, and /setup is refused; the door refuses both
     paths there and still lets them through on the desktop's own address.
  3. The cookie on the remote address is __Host- named and Secure, while a
     plain-http desktop keeps its cookie as it was.
  4. A passkey's relying party is the address it is made on.
  5. A join code: only from the desktop's own address, only with a passkey
     on the desktop and a fresh check; works once; refused after ten minutes;
     a wrong one counts as a wrong try; its ceremony finishes only with the
     code that started it and only on the remote address.
  6. The service worker and the offline page answer on the remote address
     only, and the worker answers page loads only.
  7. An app origin on the remote address joins from the remote desktop, and
     the remote desktop's session never makes one for the desktop's own.
  8. The door image's certificate job is the cloud box's own cseries-cert.
  9. The vault audit's `ip:` (vault and browser forms) believes an
     X-Forwarded-For only from a trusted proxy, and through remote access
     reads "remote", never the address the relay chose.

No database, no network: the store is a stand-in.

    python3 scripts/test_remote_access_unit.py        (from the repo root)
"""
import asyncio
import os
import sys

os.environ["CSERIES_URL"] = "http://home.cseries.localhost:8800"
os.environ["CSERIES_REMOTE_URL"] = "https://home.k7x2m9.remote.privatae.ai"
os.environ["CSERIES_TRUSTED_PROXIES"] = "10.89.79.3/32"
os.environ.pop("CSERIES_ALLOWED_ORIGINS", None)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import home_origin as H  # noqa: E402
from server import owner_login as L  # noqa: E402
from server import owner_passkeys as P  # noqa: E402
from server import remote_join as J  # noqa: E402
from server.applications import app_origin as AO  # noqa: E402
from server.routes import owner as R  # noqa: E402
from server.routes import remote_access as RR  # noqa: E402

failed = 0
REMOTE = "home.k7x2m9.remote.privatae.ai"
LOCAL = "home.cseries.localhost"
CLIENT = "198.51.100.10"


def check(cond, what, detail=""):
    global failed
    print(f"{'OK  ' if cond else 'FAIL'}  {what}" + ("" if cond or not detail else f"  ({detail})"))
    if not cond:
        failed = 1


class Store:
    def __init__(self):
        self.passkeys = {}
        self.sessions = {}

    async def fetchval(self, sql, *a):
        if sql.startswith("SELECT count(*) FROM owner_passkeys WHERE rp_id"):
            return sum(1 for r in self.passkeys.values() if r["rp_id"] in a[0])
        if sql.startswith("SELECT count(*) FROM owner_passkeys"):
            return len(self.passkeys)
        if sql.startswith("SELECT EXISTS"):
            return bool(self.passkeys)
        if sql.startswith("SELECT count(*) FROM owner_recovery_codes"):
            return 0
        raise AssertionError(sql)

    async def fetch(self, sql, *a):
        if sql.startswith("SELECT credential_id, transports FROM owner_passkeys"):
            return [r for r in self.passkeys.values() if r["rp_id"] in a[0]]
        raise AssertionError(sql)

    async def fetchrow(self, sql, *a):
        if "FROM owner_account" in sql:
            return {"email": "owner@example.com", "password_hash": "x"}
        if "FROM owner_sessions s" in sql:
            return self.sessions.get(a[0])
        raise AssertionError(sql)

    async def execute(self, sql, *a):
        if sql.startswith("INSERT INTO owner_sessions"):
            self.sessions[a[0]] = {"last_seen_at": a[1], "expires_at": a[2], "origin": a[3],
                                   "parent": None, "parent_seen": None, "parent_expires": None}
        return "OK"


STORE = Store()
L._pool = lambda: STORE
L._owner_email, L._cookie_suffix, L._setup_code = "owner@example.com", "0a1b2c3d", None


def refused(coro, kind=L.LoginError):
    try:
        asyncio.run(coro)
    except kind:
        return True
    except Exception as exc:  # noqa: BLE001
        print("      raised", repr(exc))
        return False
    return False


# 1 ─ the address
for bad in ("http://home.k7x2m9.remote.privatae.ai", "https://home.k7x2m9.remote.privatae.ai:8443",
            "https://home.k7x2m9.localhost", "https://home.remote.privatae.ai",
            "https://home.k7x2m9.cseries.localhost", "https://casa.k7x2m9.remote.privatae.ai"):
    try:
        H.parse_remote(bad, H.HOME)
        ok = False
    except ValueError:
        ok = True
    check(ok, f"CSERIES_REMOTE_URL={bad} is refused")
check(H.REMOTE is not None and H.REMOTE.host == REMOTE and H.ORIGINS == (H.HOME, H.REMOTE),
      "the remote address is the second origin")
check(H.is_remote_host(REMOTE) and H.is_remote_host(f"apps-{'a' * 20}-home.k7x2m9.remote.privatae.ai")
      and not H.is_remote_host(LOCAL) and not H.is_remote_host("k7x2m9.remote.privatae.ai.evil.example"),
      "names on the remote address are known as such, and only those")

# 2 ─ passkey or recovery code only
check(refused(L.sign_in("owner@example.com", "x" * 20, CLIENT, REMOTE)),
      "owner_login.sign_in refuses the password on the remote address")
app = FastAPI()
app.include_router(R.router)
app.include_router(RR.router)
tc = TestClient(app)
r = tc.post("/api/owner/login", json={"email": "owner@example.com", "password": "x" * 20},
            headers={"Host": REMOTE})
check(r.status_code == 403 and "passkey" in r.json()["error"], "the password route refuses on the remote address",
      r.status_code)
r = tc.post("/api/owner/setup", json={"code": "a", "email": "b@c.d", "password": "x" * 20}, headers={"Host": REMOTE})
check(r.status_code == 403, "setup is refused on the remote address", r.status_code)
r = tc.get("/setup", headers={"Host": REMOTE})
check(r.status_code == 403, "the setup page is refused on the remote address", r.status_code)
r = tc.get("/login", headers={"Host": REMOTE})
check(r.status_code == 200 and 'id="passkey"' in r.text and "/api/owner/recovery/login" in r.text
      and 'name="password"' not in r.text and 'id="remote-sw"' in r.text,
      "the remote sign-in page has the passkey and a recovery code, no password, and the offline worker")
r = tc.get("/login", headers={"Host": LOCAL + ":8800"}, follow_redirects=False)
check(r.status_code == 200 and 'id="remote-sw"' not in r.text, "the desktop's own sign-in page is as before")

from server.middleware import owner_session as OS  # noqa: E402
OS._door_key = lambda: b"k" * 40


def door(uri, host):
    scope = {"type": "http", "client": ("10.89.79.3", 1),
             "headers": [(b"host", host.encode()), (b"x-cseries-door", b"k" * 40),
                         (b"x-forwarded-method", b"POST"), (b"x-forwarded-uri", uri.encode())]}
    return asyncio.run(OS.door_decision(scope))[0]


for path in ("/api/owner/login", "/setup", "/api/owner/setup", "/api/owner/claim"):
    check(door(path, REMOTE) == 403 and door(path, LOCAL + ":8800") == 200,
          f"the door refuses {path} on the remote address only")
check(door("/api/owner/remote/join/options", REMOTE) == 200, "the door lets the join's post through")

# 3 ─ cookies
check(L.cookie_name(REMOTE) == "__Host-cseries_session_0a1b2c3d" and L.secure_cookie(REMOTE),
      "the remote address's cookie is __Host- named and Secure")
check(L.cookie_name(LOCAL) == "cseries_session_0a1b2c3d" and not L.secure_cookie(LOCAL)
      and L.cookie_name() == "cseries_session_0a1b2c3d", "the plain-http desktop keeps its cookie")

# 4 ─ relying parties
check(P._check_host(REMOTE) is H.REMOTE and P._check_host(LOCAL) is H.HOME,
      "a passkey's relying party is the address it is used on")
check(refused(P.login_options(CLIENT, f"apps-{'a' * 20}-home.k7x2m9.remote.privatae.ai")),
      "an app origin is never a relying party")
check(P.rp_ids(H.HOME) == [LOCAL, ""] and P.rp_ids(H.REMOTE) == [REMOTE],
      "passkeys from before keep working on the desktop only")


# 5 ─ join codes
def desk_session():
    tok = asyncio.run(L._new_session(STORE, LOCAL))
    return tok


tok = desk_session()
check(refused(J.new_code(tok, LOCAL)), "no phone before the desktop has a passkey of its own")
STORE.passkeys[b"d"] = {"credential_id": b"d", "transports": [], "rp_id": ""}
check(refused(J.new_code(tok, LOCAL), P.StepUpNeeded), "adding a phone needs a fresh check")
P._note_step_up(tok, P.PASSKEY)
check(refused(J.new_code(tok, REMOTE)), "a phone is added from the desktop's own address only")
made = asyncio.run(J.new_code(tok, LOCAL))
code = made["url"].split("#c=", 1)[1]
check(made["url"].startswith("https://" + REMOTE + "/remote/join#c=") and len(code) >= 43,
      "the code is 32 random bytes, after the # of the remote join page")
check(all(code not in repr(v) and code not in k for k, v in J._codes.items()), "only the code's hash is kept")
check(refused(J.start(code, CLIENT, LOCAL)), "the code is spent on the remote address only")
opts = asyncio.run(J.start(code, CLIENT, REMOTE))
check(opts["options"]["rp"]["id"] == REMOTE and opts["options"]["authenticatorSelection"]["userVerification"]
      == "required", "the phone makes a passkey for the remote address, with user verification")
check(refused(J.start(code, CLIENT, REMOTE)), "the code works once")
check(refused(J.finish(opts["ceremony"], "not-the-code", {}, REMOTE)),
      "the ceremony finishes only with the code that started it")
before = L._failures.get(L._failure_key(CLIENT), (0,))[0]
refused(J.start("x" * 43, CLIENT, REMOTE))
check(L._failures.get(L._failure_key(CLIENT), (0,))[0] == before + 1, "a wrong code counts as a wrong try")
L._forget_failures(CLIENT)

P._note_step_up(tok, P.PASSKEY)
late = asyncio.run(J.new_code(tok, LOCAL)).get("url").split("#c=", 1)[1]
real = J.time.monotonic
J.time.monotonic = lambda: real() + J.JOIN_TTL_S + 1
try:
    check(refused(J.start(late, CLIENT, REMOTE)), "a code is refused after ten minutes")
finally:
    J.time.monotonic = real
L._forget_failures(CLIENT)
P._note_step_up(tok, P.PASSKEY)
fresh = asyncio.run(J.new_code(tok, LOCAL)).get("url").split("#c=", 1)[1]
J.time.monotonic = lambda: real() + J.JOIN_TTL_S - 5
try:
    check(not refused(J.start(fresh, CLIENT, REMOTE)), "and still good just before")
finally:
    J.time.monotonic = real

# 6 ─ the offline page
for path in ("/remote-sw.js", "/remote/offline", "/remote/join"):
    a = tc.get(path, headers={"Host": REMOTE})
    b = tc.get(path, headers={"Host": LOCAL + ":8800"})
    check(a.status_code == 200 and b.status_code == 404, f"{path} answers on the remote address only")
check("event.request.mode !== 'navigate'" in RR.SERVICE_WORKER_JS, "the worker answers page loads only")
check("isn't reachable" in RR.OFFLINE_HTML and "asleep" in RR.OFFLINE_HTML, "the offline page says what happened")

# 7 ─ app origins on the remote address
remote_app = AO.app_host("settings", REMOTE)
local_app = AO.app_host("settings", LOCAL)
check(OS.own_app_origin(remote_app) and OS.own_app_origin(local_app), "both addresses' app origins are this Host's")
check(OS.join_url(remote_app, "/").startswith(f"https://{REMOTE}/__host/join?"),
      "a remote app origin joins from the remote desktop")
check(f"https://{REMOTE}" in AO.frame_ancestors_for("home") and "http://home.cseries.localhost:8800"
      in AO.frame_ancestors_for("home"), "either desktop may frame this Host's apps")
rtok = asyncio.run(L._new_session(STORE, REMOTE))
cookie = {L.cookie_name(REMOTE): rtok}
r = tc.get("/__host/join", params={"to": f"http://{local_app}:8800", "rd": "/"},
           headers={"Host": REMOTE}, cookies=cookie, follow_redirects=False)
check(r.status_code == 400, "the remote session makes no join for the desktop's own app origins", r.status_code)
r = tc.get("/__host/join", params={"to": f"https://{remote_app}", "rd": "/"},
           headers={"Host": REMOTE}, cookies=cookie, follow_redirects=False)
check(r.status_code == 303 and r.headers["location"].startswith(f"https://{remote_app}/__host/session?code="),
      "and makes one for its own", r.status_code)

# 8 ─ the door image's certificate job is the cloud box's own cseries-cert
import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402
import tempfile  # noqa: E402


def _load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


sys.path.insert(0, os.path.join(ROOT, "vm/cloud/overlay/usr/local/lib/cseries-cloud"))
door_cert = _load(os.path.join(ROOT, "sidecars/remote-door/door-cert"), "door_cert")
door_cert.CERT = os.path.join(ROOT, "vm/cloud/overlay/usr/local/sbin/cseries-cert")
cert = door_cert.load()
check(cert.TLS_GROUP is None and cert.RELOAD is None, "the door runs cseries-cert with no caddy group and no systemd")
import cseries_cloud as CC  # noqa: E402
with tempfile.TemporaryDirectory() as d:
    paths = CC.Paths(d)
    crt, key = os.path.join(d, "lego.crt"), os.path.join(d, "lego.key")
    for p, body in ((crt, b"CERT"), (key, b"KEY")):
        with open(p, "wb") as f:
            f.write(body)
    first = cert.install(paths, crt, key)
    again = cert.install(paths, crt, key)
    mode = os.stat(os.path.join(paths.tls, "key.pem")).st_mode & 0o777
    check(first and not again and mode == 0o600, "its install keeps the key 0600 for the door's own user, once", oct(mode))
fresh = _load(os.path.join(ROOT, "vm/cloud/overlay/usr/local/sbin/cseries-cert"), "cseries_cert_cloud")
check(fresh.TLS_GROUP == "caddy" and fresh.RELOAD == ["systemctl", "reload", "caddy.service"],
      "a cloud box keeps the caddy group and the systemd reload")

# 9 ─ the vault audit's `ip:` believes only a trusted proxy, and says "remote"
from starlette.requests import Request as _Req  # noqa: E402

from server.routes import browser_forms as BF  # noqa: E402
from server.routes import vault as V  # noqa: E402

PROXY = "10.89.79.3"          # CSERIES_TRUSTED_PROXIES above
CHOSEN = "203.0.113.66"         # whatever a page or a relay writes in the header


def _audit_req(host, peer, xff=None):
    headers = [(b"host", host.encode())]
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    return _Req({"type": "http", "method": "POST", "path": "/api/vault/secrets", "headers": headers,
                 "client": (peer, 40000) if peer else None, "query_string": b""})


def _ip(actor_fn, req):
    return actor_fn(req, "secret.put").split(" ")[1]


for name, fn in (("vault", V._actor), ("browser_forms", BF._actor)):
    got = _ip(fn, _audit_req(LOCAL, CLIENT, CHOSEN))
    check(got == f"ip:{CLIENT}", f"{name}: a forwarded address from a peer that is no proxy is not recorded", got)
    got = _ip(fn, _audit_req(LOCAL, PROXY, f"{CHOSEN}, {PROXY}"))
    check(got == f"ip:{CHOSEN}", f"{name}: through a trusted proxy, the address it forwarded for", got)
    got = _ip(fn, _audit_req(REMOTE, PROXY, CHOSEN))
    check(got == "ip:remote", f"{name}: on the remote address, 'remote', never the forwarded address", got)
    os.environ["CSERIES_REMOTE_DOOR"] = f"{PROXY}:8443"
    try:
        got = _ip(fn, _audit_req(LOCAL, PROXY, CHOSEN))
        check(got == "ip:remote", f"{name}: from the remote door's own address, whatever Host it names: 'remote'",
              got)
    finally:
        os.environ.pop("CSERIES_REMOTE_DOOR", None)
    got = _ip(fn, _audit_req(LOCAL, None, CHOSEN))
    check(got == "ip:unknown", f"{name}: no peer and an unbelieved header: unknown", got)

print("PASSED" if not failed else "FAILED")
sys.exit(failed)
