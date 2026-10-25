#!/usr/bin/env python3
"""The cloud machine's unlock agent (vm/cloud/overlay/usr/local/sbin/
cseries-unlock-agent), with the vault on the stand-in cryptsetup
(scripts/cloud_box_fake_luks.py).

  1. GET /api/state names the machine, its unlock page's relying party, the
     PRF salt and the unlock passkeys, and never a secret.
  2. The claim: enrolling needs the vault open, its bootstrap key still
     there, and the setup code; it answers the recovery key once and keeps
     the waiting keys in /run, 0600; confirming needs that exact recovery
     key and then starts the claim in a unit of its own (systemd-run), which
     nothing stops half way; once claimed, enrolling is refused.
  3. Unlocking: a wrong key is refused and recorded outside the vault; the
     right one opens it, records the unlock inside it with the earlier
     attempts, opens the Host's ports and starts the customer's services.
  4. Five tries a minute per source (an IPv6 source counts by its /64);
     the source is the address Caddy names, never one a client adds. A
     refused try while locked is kept on the plain disk with the source's
     /24 (or /48) only, and those records are bounded.
  5. Changing the keys needs the vault open and one of its keys.
  6. Over the socket: a real HTTP request through the agent's own server
     on a unix socket answers JSON; a body over 16 KiB is refused.
  7. A claim a reset cut off after the bootstrap key went: either of the
     customer's keys unlocks it and starts the claim's unit with that key;
     open with the unit stopped, the state says resume and a key starts it.
  8. Unlock attempts kept on the plain disk are bounded.
  9. The handoff. POST /api/setup takes the setup code and the owner's email
     as a form, from the account site's origin or the page's own only; a
     right code answers a 303 to the page and a __Host- cookie (Secure,
     HttpOnly, SameSite=Strict), and the box keeps only the cookie's hash
     (0600, in /run). Anything else answers a page and sets nothing. The
     cookie stands in for the code for enroll and confirm, and only from
     the page's own origin (a sibling under the same site carries it too).
     "I saved it" sends back the key the page showed, and starts the claim
     only while that is the key waiting: a second tab's enroll replaces it,
     and the first tab's confirm is refused. The ticket (/api/owner-ticket):
     without a key only to the browser whose handoff started the claim that
     set up the keys (not one whose claim stopped before it), only to make
     the Host's owner; with a key, a sign-in once the Host has its owner. The
     box keeps the ticket's hash for the Host (owner-claim.json); neither the
     code nor a ticket is ever in the state. /api/setup-code is gone. Once
     the setup is done the code hands nothing over. The page's own calls
     with the handoff's cookie are no tries (two tabs in one minute finish
     the setup); a typed code and a key are.
 10. A stop (the updater's try-restart) waits for the request in hand: the
     answer is sent whole, then the agent exits; between requests it exits
     at once. main() makes that SIGTERM's handler, and the unit sends the
     SIGTERM to the agent alone (KillMode=mixed), so a cryptsetup it waits
     for is not killed under it. No real signal: the handler is called on
     the serving thread, where a signal's handler runs.

Container-free: python3 scripts/test_cloud_box_unlock_agent.py
"""
import http.client
import importlib.util
import json
import os
import signal
import socket
import stat
import sys
import tempfile
import threading
from importlib.machinery import SourceFileLoader

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LIB = os.path.join(ROOT, "vm/cloud/overlay/usr/local/lib/cseries-cloud")
sys.path[:0] = [LIB, os.path.dirname(os.path.abspath(__file__))]
import cseries_cloud as cc  # noqa: E402
import cseries_machine as cm  # noqa: E402
import cseries_vault as cv  # noqa: E402
from cloud_box_fake_luks import FakeLuks  # noqa: E402

loader = SourceFileLoader("unlock_agent", os.path.join(ROOT, "vm/cloud/overlay/usr/local/sbin/cseries-unlock-agent"))
spec = importlib.util.spec_from_loader("unlock_agent", loader)
ag = importlib.util.module_from_spec(spec)
loader.exec_module(ag)

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


MID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
CFG = {"name": "orchard", "address_id": "k7x2m9", "zone": "cloud.privatae.ai", "machine_id": MID,
       "heartbeat_url": "https://127.0.0.1:9/never"}
CODE = "0a1b2c-3d4e5f-6a7b8c"                        # invented
CRED = "Q3JlZGVudGlhbC1pbnZlbnRlZC0wMQ"
PP = cv.passkey_passphrase(bytes(range(32)), MID, CRED)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def box(tmp):
    paths = cc.Paths(tmp)
    cc.write_json(paths.cloud_json(), CFG)
    with open(paths.setup_code(), "w") as f:
        f.write(CODE + "\n")
    os.makedirs(paths.p("run/cseries-door"))
    # There on every box before its vault is made, as in test_cloud_box_vault.
    # Made here by the vault it would be 2755, which a runner with systemd's
    # RestrictSUIDSGID=yes (the forge's) refuses: mkdir with S_ISGID is EPERM.
    os.makedirs(paths.p("var/log/journal"))
    fake = FakeLuks(tmp)
    v = cv.Vault(paths, fake)
    v.create(MID, size=8 * cv.GIB)
    clock = Clock()
    a = ag.Agent(paths, v, fake, ag.Limiter(clock), now=clock, owner=lambda: OWNER["has"])
    return paths, fake, v, a, clock


# What the Host answers about its owner (cseries_machine.host_has_owner):
# True, False, or None while it does not answer.
OWNER = {"has": False}


def call(a, method, path, body=None, xff="203.0.113.7", headers=None):
    return a.handle(method, path, json.dumps(body).encode() if body is not None else b"", xff, headers)


UNLOCK_ORIGIN = "https://unlock-orchard.k7x2m9.cloud.privatae.ai"
SIBLING = "https://apps-3f9a1c-orchard.k7x2m9.cloud.privatae.ai"   # an app origin, invented


def hand_over(a, origin="https://privatae.ai", code=CODE, email="Owner@Example.com",
              ctype="application/x-www-form-urlencoded", xff="203.0.113.7"):
    """The card's form POST: (status, body, headers as a dict, the cookie's value or None)."""
    import urllib.parse as up
    body = up.urlencode({"setup_code": code, "email": email}).encode()
    heads = {"Content-Type": ctype}
    if origin is not None:
        heads["Origin"] = origin
    status, out, extra = a.respond("POST", "/api/setup", body, xff, heads)
    extra = dict(extra)
    cookie = extra.get("Set-Cookie")
    value = cookie.split(";")[0].split("=", 1)[1] if cookie else None
    return status, out, extra, value


def with_cookie(value, origin=UNLOCK_ORIGIN):
    h = {"Cookie": f"other=1; {ag.HANDOFF_COOKIE}={value}"}
    if origin is not None:
        h["Origin"] = origin
    return h


with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v, a, clock = box(tmp)

    # 1. state
    code, st = call(a, "GET", "/api/state")
    check(code == 200 and st["state"] == "open" and st["enrolled"] is False and st["passkeys"] == [] and
          st["rp_id"] == "unlock-orchard.k7x2m9.cloud.privatae.ai" and
          st["host_url"] == "https://orchard.k7x2m9.cloud.privatae.ai/" and st["salt"] == v.config()["salt"],
          "state: open, not enrolled, the unlock page's relying party, the Host's address, the salt", st)
    blob = json.dumps(st)
    check(open(paths.p(cv.BOOT_KEY)).read() not in blob and CODE not in blob, "state carries no secret")

    # 2. the claim
    code, r = call(a, "POST", "/api/enroll", {"setup_code": "ffffff-ffffff-ffffff", "passkey": None})
    check(code == 403, "enrolling with a wrong setup code is refused", (code, r))
    code, r = call(a, "POST", "/api/enroll", {"setup_code": CODE, "passkey": {"credential_id": CRED,
                                                                              "passphrase": "nothex"}})
    check(code == 400, "a passkey answer of the wrong form is refused", (code, r))
    code, r = call(a, "POST", "/api/enroll", {"setup_code": CODE.upper(),
                                               "passkey": {"credential_id": CRED, "passphrase": PP}})
    check(code == 200 and len(r.get("recovery_key", "").split("-")) == 13,
          "enrolling with the setup code answers a recovery key", (code, r))
    shown = r["recovery_key"]
    pend = paths.p(cm.PENDING)
    check(os.path.exists(pend) and stat.S_IMODE(os.stat(pend).st_mode) == 0o600 and
          json.load(open(pend))["passkey"]["credential_id"] == CRED,
          "the waiting keys are kept in /run, 0600")
    clock.t += 61
    code, r = call(a, "POST", "/api/enroll/confirm", {"setup_code": CODE, "recovery_key": cv.new_recovery_key()[0]})
    check(code == 403, "confirming with another recovery key is refused", (code, r))
    fake.calls.clear()
    code, r = call(a, "POST", "/api/enroll/confirm", {"setup_code": CODE, "recovery_key": shown.lower()})
    runs = [c[0] for c in fake.calls if os.path.basename(c[0][0]) == "systemd-run"]
    check(code == 200 and r.get("started") is True and len(runs) == 1 and
          "--unit=cseries-vault-claim" in runs[0] and "--property=SendSIGKILL=no" in runs[0] and
          "--property=TimeoutStopSec=infinity" in runs[0] and runs[0][-2:] == ["/usr/local/sbin/cseries-vault", "claim"],
          "the right recovery key starts the claim in a unit nothing stops half way", (code, r, runs))
    check(PP not in json.dumps(runs) and shown not in json.dumps(runs), "no key is on its command line")
    # The unit's work, as cseries-vault claim does it.
    pending = json.load(open(pend))
    v.claim(pending["passkey"], pending["recovery_pass"])
    os.unlink(pend)
    clock.t += 61
    code, r = call(a, "POST", "/api/enroll", {"setup_code": CODE, "passkey": None})
    check(code == 409, "once claimed, enrolling is refused", (code, r))
    code, st = call(a, "GET", "/api/state")
    check(st["enrolled"] is True and [k["credential_id"] for k in st["passkeys"]] == [CRED] and st["recovery"],
          "state lists the unlock passkey and the recovery key", st)

    # 3. unlocking
    fake.reboot()
    cm.record_state(paths, "locked")
    clock.t += 61
    code, r = call(a, "POST", "/api/unlock", {"credential_id": CRED, "passphrase": "0" * 64})
    check(code == 403 and v.state() == "locked", "a wrong key is refused", (code, r))
    outside = cm.read_lines(paths.p(cv.ATTEMPTS))
    check(len(outside) == 1 and outside[0]["opened"] is False and outside[0]["from"] == "203.0.113.0/24",
          "and recorded outside the vault, which is locked, with the source's /24 only", outside)
    raw = open(paths.p(cv.ATTEMPTS), encoding="utf-8").read()
    check("203.0.113.7" not in raw and set(outside[0]) <= {"at", "from", "with", "opened", "why"},
          "the plain disk never holds the full address: the time, the outcome and the /24", raw)
    check(cm.source_prefix("2001:db8:7:9::/64") == "2001:db8:7::/48" and
          cm.source_prefix("198.51.100.23") == "198.51.100.0/24" and cm.source_prefix("unknown") == "unknown",
          "an IPv6 source is kept as its /48, an IPv4 one as its /24")
    fake.calls.clear()
    code, r = call(a, "POST", "/api/unlock", {"credential_id": CRED, "passphrase": PP})
    check(code == 200 and r["next"] == "https://orchard.k7x2m9.cloud.privatae.ai/" and v.state() == "open",
          "the passkey's passphrase opens it", (code, r))
    record = cm.read_lines(os.path.join(v.mount, cv.RECORD, cm.RECORD_NAME))
    check([x["opened"] for x in record] == [False, True] and not os.path.exists(paths.p(cv.ATTEMPTS)),
          "the unlock is recorded inside the vault, after the attempt kept outside", record)
    check(record[0]["from"] == "203.0.113.0/24" and record[1]["from"] == "203.0.113.7",
          "inside the vault the unlock keeps its source; the attempt from outside keeps its /24", record)
    argvs = [c[0] for c in fake.calls]
    check(any(x[:2] == ["/usr/sbin/nft", "add"] and "lanes_open" in x for x in argvs),
          "the Host's ports open")
    check(any(x[:3] == ["/usr/bin/systemctl", "start", "--no-block"] and x[-1] == "cseries-unlocked.target"
              for x in argvs), "the customer's services start")
    pub = json.load(open(paths.p("run/cseries-door/machine-state.json")))
    check(pub["vault"]["state"] == "open", "what the Host reads says open")

    # 4. tries per source
    fake.reboot()
    clock.t += 61
    codes = [call(a, "POST", "/api/unlock", {"recovery_key": cv.new_recovery_key()[0]},
                  xff=f"2001:db8:7:9::{i}")[0] for i in range(6)]
    check(codes == [403] * 5 + [429], "five tries a minute from one /64, then a wait", codes)
    check(call(a, "POST", "/api/unlock", {"recovery_key": shown}, xff="2001:db8:7:a::1")[0] == 200,
          "another /64 has its own five")
    check(ag.source_key("198.51.100.1, 203.0.113.9") == "203.0.113.9",
          "the source is the address Caddy names (the last), not one a client wrote")

    # 5. the keys
    cred2 = "Q3JlZGVudGlhbC1pbnZlbnRlZC0wMg"
    pp2 = cv.passkey_passphrase(bytes(range(32, 64)), MID, cred2)
    clock.t += 61
    code, r = call(a, "POST", "/api/keys/add", {"auth": {"credential_id": CRED, "passphrase": "1" * 64},
                                                "credential_id": cred2, "passphrase": pp2})
    check(code == 403, "adding a passkey with a wrong key is refused", (code, r))
    code, r = call(a, "POST", "/api/keys/add", {"auth": {"recovery_key": shown},
                                                "credential_id": cred2, "passphrase": pp2})
    check(code == 200 and len(v.passkeys()) == 2, "with the recovery key a passkey is added", (code, r))
    code, r = call(a, "POST", "/api/keys/recovery", {"auth": {"credential_id": cred2, "passphrase": pp2}})
    check(code == 200 and r["recovery_key"] != shown, "a new recovery key, shown once", (code, r))
    fake.reboot()
    cm.record_state(paths, "locked")
    clock.t += 61
    code, r = call(a, "POST", "/api/keys/remove", {"auth": {"recovery_key": r["recovery_key"]},
                                                   "credential_id": CRED})
    check(code == 409, "changing keys needs the vault open", (code, r))

    # 6. over the socket
    sock_path = os.path.join(tmp, "agent.sock")
    srv_sock = socket.socket(socket.AF_UNIX)
    srv_sock.bind(sock_path)
    srv_sock.listen(4)
    ag.Handler.agent = a
    server = ag.Server(srv_sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class UnixConn(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.connect(sock_path)

    c = UnixConn("caddy")
    c.request("GET", "/api/state", headers={"X-Forwarded-For": "203.0.113.7"})
    resp = c.getresponse()
    body = json.loads(resp.read())
    check(resp.status == 200 and resp.getheader("Content-Type") == "application/json" and
          resp.getheader("Cache-Control") == "no-store" and body["state"] == "locked",
          "over the socket: JSON, not cached", (resp.status, body.get("state")))
    c = UnixConn("caddy")
    # The length alone: the agent answers before reading a body that large,
    # and a client still sending one could meet a closed socket.
    c.request("POST", "/api/unlock", headers={"Content-Length": str(16 * 1024 + 1)})
    resp = c.getresponse()
    check(resp.status == 413, "a body over 16 KiB is refused", resp.status)
    c = UnixConn("caddy")
    c.request("GET", "/api/nothing")
    check(c.getresponse().status == 404, "an unknown route is 404")
    server.shutdown()

# 7. A claim cut off after the bootstrap key went: a reset in its
# re-encryption. The page offers the customer's keys; the unlock with one
# answers "claiming" and starts the claim's unit with that key, and while
# the vault is open with the unit stopped, a key on the page starts it.
with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v, a, clock = box(tmp)
    shown, rpass = cv.new_recovery_key()
    fake.cut_reencrypt = True
    try:
        v.claim({"credential_id": CRED, "passphrase": PP}, rpass)
    except cv.VaultError:
        pass
    fake.reboot()
    cm.record_state(paths, "locked")
    code, st = call(a, "GET", "/api/state")
    check(code == 200 and st["state"] == "locked" and [k["credential_id"] for k in st["passkeys"]] == [CRED],
          "after the reset: locked, the passkey offered", st)
    fake.calls.clear()
    code, r = call(a, "POST", "/api/unlock", {"recovery_key": shown})
    runs = [c[0] for c in fake.calls if os.path.basename(c[0][0]) == "systemd-run"]
    check(code == 200 and r.get("claiming") is True and len(runs) == 1 and
          json.load(open(paths.p(cm.PENDING))) == {"resume_pass": rpass},
          "the recovery key unlocks it and starts the claim's unit with that key", (code, r, runs))
    # The unit stopped before it finished (invented): the vault is open, the
    # claim not done; the page asks for a key, and one starts it again.
    os.unlink(paths.p(cm.PENDING))
    code, st = call(a, "GET", "/api/state")
    check(st["state"] == "open" and st["resume"] is True and st["enrolled"] is False,
          "open with the claim unfinished: state says resume", st)
    clock.t += 61
    code, r = call(a, "POST", "/api/unlock", {"credential_id": CRED, "passphrase": "2" * 64})
    check(code == 403 and not os.path.exists(paths.p(cm.PENDING)), "a wrong key does not start it", (code, r))
    fake.calls.clear()
    code, r = call(a, "POST", "/api/unlock", {"credential_id": CRED, "passphrase": PP})
    check(code == 200 and r.get("claiming") is True and
          json.load(open(paths.p(cm.PENDING))) == {"resume_pass": PP},
          "the passkey starts it again", (code, r))
    fake.claim_active = True
    code, r = call(a, "POST", "/api/unlock", {"credential_id": CRED, "passphrase": PP})
    check(code == 200 and r.get("already") is True, "while the unit runs, an unlock only says it is open", r)

# 8. Unlock attempts on the plain disk are bounded.
with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v, a, clock = box(tmp)
    fake.reboot()
    cm.record_state(paths, "locked")
    real = cm.ATTEMPTS_MAX_BYTES, cm.ATTEMPTS_KEEP
    cm.ATTEMPTS_MAX_BYTES, cm.ATTEMPTS_KEEP = 4096, 10
    for i in range(200):
        cm.note_attempt(paths, v, "a passkey", f"198.51.{i}.9", False, "invented")
    cm.ATTEMPTS_MAX_BYTES, cm.ATTEMPTS_KEEP = real
    kept = cm.read_lines(paths.p(cv.ATTEMPTS))
    check(os.path.getsize(paths.p(cv.ATTEMPTS)) <= 4096 + 512 and kept[-1]["from"] == "198.51.199.0/24",
          "tries from many sources cannot fill the plain disk: the newest are kept",
          (os.path.getsize(paths.p(cv.ATTEMPTS)), len(kept)))

# 9. The handoff, the cookie in place of the code, and the owner's ticket.
with tempfile.TemporaryDirectory() as tmp:
    import hashlib
    paths, fake, v, a, clock = box(tmp)
    OWNER["has"] = False
    hpath = paths.p(cm.HANDOFF)

    for origin, why in ((None, "no Origin"), ("null", "Origin null (a form sent with no referrer)"),
                        ("https://elsewhere.example", "another site"), (SIBLING, "an app of the Host")):
        status, out, extra, value = hand_over(a, origin=origin)
        check(status == 403 and value is None and b"privatae.ai" in out and
              extra.get("Content-Type", "").startswith("text/html") and not os.path.exists(hpath),
              f"a handoff from {why} is refused with a page, and nothing is kept", (status, extra))
    status, out, extra, value = hand_over(a, ctype="application/json")
    check(status == 415 and value is None, "a handoff that is not a form is refused", status)
    status, out, extra, value = hand_over(a, email="not an email")
    check(status == 400 and value is None, "a handoff without a valid email is refused", status)
    status, out, extra, value = hand_over(a, code="ffffff-ffffff-ffffff")
    check(status == 403 and value is None and b"setup code" in out, "a wrong code is refused", status)
    clock.t += 61

    status, out, extra, tok = hand_over(a)
    cookie = extra.get("Set-Cookie", "")
    attrs = [x.strip() for x in cookie.split(";")[1:]]
    check(status == 303 and extra.get("Location") == "/" and out == b"" and tok and
          cookie.startswith(ag.HANDOFF_COOKIE + "=") and ag.HANDOFF_COOKIE.startswith("__Host-") and
          set(attrs) == {f"Max-Age={ag.HANDOFF_TTL_S}", "Path=/", "Secure", "HttpOnly", "SameSite=Strict"},
          "the card's form: a 303 to the page and a __Host- cookie, Secure, HttpOnly, SameSite=Strict",
          (status, extra))
    kept = open(hpath, encoding="utf-8").read()
    check(stat.S_IMODE(os.stat(hpath).st_mode) == 0o600 and tok not in kept and CODE not in kept and
          hashlib.sha256(tok.encode()).hexdigest() in kept and "owner@example.com" in kept,
          "the box keeps the cookie's hash and the email, 0600 in /run; neither the cookie nor the code")
    status, _out, _extra, own = hand_over(a, origin=UNLOCK_ORIGIN, email="owner@example.com")
    check(status == 303 and own, "the page's own form hands over too (a code given with the machine)")

    code, st = call(a, "GET", "/api/state", headers={"Cookie": f"{ag.HANDOFF_COOKIE}={tok}"})
    check(st["handoff"] is True and st["handoff_claim"] is False and st["host"] is None,
          "the state says this browser holds a handoff", st)
    code, st = call(a, "GET", "/api/state", headers={"Cookie": f"{ag.HANDOFF_COOKIE}=forged"})
    check(st["handoff"] is False, "a cookie the box did not make is no handoff", st)

    clock.t += 61
    code, r = call(a, "POST", "/api/enroll", {"passkey": None}, headers=with_cookie(tok, SIBLING))
    check(code == 403 and r.get("handoff") is False, "the cookie from an app of the Host is not taken", (code, r))
    code, r = call(a, "POST", "/api/enroll", {"passkey": None}, headers=with_cookie(tok, None))
    check(code == 403, "nor a POST without an Origin", (code, r))
    code, r = call(a, "POST", "/api/enroll", {"passkey": {"credential_id": CRED, "passphrase": PP}},
                   headers=with_cookie(tok))
    check(code == 200 and len(r.get("recovery_key", "").split("-")) == 13,
          "the cookie, from the page's own origin, stands in for the setup code", (code, r))
    first = r["recovery_key"]
    # The same browser in a second tab (the card's button pressed again, or
    # the page opened twice): a newer enroll replaces the key waiting.
    code, r = call(a, "POST", "/api/enroll", {"passkey": {"credential_id": CRED, "passphrase": PP}},
                   headers=with_cookie(tok))
    shown = r.get("recovery_key")
    check(code == 200 and shown and shown != first, "a second tab's enroll makes another recovery key", (code, r))
    runs = lambda: len([c for c in fake.calls if os.path.basename(c[0][0]) == "systemd-run"])  # noqa: E731
    before = runs()
    code, r = call(a, "POST", "/api/enroll/confirm", {"saved": True}, headers=with_cookie(tok))
    check(code == 400 and runs() == before,
          "I saved it alone starts nothing: the page sends back the key it showed", (code, r))
    clock.t += 61
    code, r = call(a, "POST", "/api/enroll/confirm", {"saved": True, "recovery_key": first},
                   headers=with_cookie(tok))
    check(code == 403 and r.get("replaced") is True and "another tab" in r.get("error", "") and runs() == before,
          "the first tab's I saved it is refused: the disk is never sealed with a key that tab did not show",
          (code, r))
    code, r = call(a, "POST", "/api/owner-ticket", {}, headers=with_cookie(tok))
    check(code == 409, "no ticket before the keys are set up", (code, r))
    clock.t += 61
    code, r = call(a, "POST", "/api/enroll/confirm", {"saved": True, "recovery_key": shown},
                   headers=with_cookie(tok))
    check(code == 200 and r.get("started") is True and runs() == before + 1,
          "I saved it from the tab that shows the waiting key: the claim starts, nothing typed", (code, r))
    pending = json.load(open(paths.p(cm.PENDING)))
    v.claim(pending["passkey"], pending["recovery_pass"])
    os.unlink(paths.p(cm.PENDING))

    code, st = call(a, "GET", "/api/state", headers={"Cookie": f"{ag.HANDOFF_COOKIE}={tok}"})
    check(st["enrolled"] is True and st["handoff_claim"] is True and st["host"] == {"up": True, "owner": False},
          "set up: this browser's handoff ran the claim; the Host answers and has no owner", st)
    door = paths.p("run/cseries-door", cm.OWNER_CLAIM_NAME)
    clock.t += 61
    code, r = call(a, "POST", "/api/owner-ticket", {}, headers=with_cookie(own))
    check(code == 403 and r.get("need_key") is True and not os.path.exists(door),
          "another handoff, which set up no key, gets no ticket without a key", (code, r))
    code, r = call(a, "POST", "/api/owner-ticket", {}, headers=with_cookie(tok, SIBLING))
    check(code == 403 and r.get("need_key") is True, "nor the claim's cookie sent by an app of the Host", (code, r))
    OWNER["has"] = None
    code, r = call(a, "POST", "/api/owner-ticket", {}, headers=with_cookie(tok))
    check(code == 503 and r.get("starting") is True and not os.path.exists(door),
          "while the Host does not answer: starting, and no ticket", (code, r))
    OWNER["has"] = False
    code, r = call(a, "POST", "/api/owner-ticket", {}, headers=with_cookie(tok))
    claim = json.load(open(door))
    check(code == 200 and r.get("kind") == "claim" and
          r.get("action") == "https://orchard.k7x2m9.cloud.privatae.ai/api/owner/claim" and
          claim["kind"] == "claim" and claim["email"] == "owner@example.com" and
          claim["ticket_sha256"] == hashlib.sha256(r["ticket"].encode()).hexdigest() and
          r["ticket"] not in open(door).read() and claim["expires_at"] == cm.iso(clock.t + ag.TICKET_TTL_S),
          "the browser that set up the keys gets the claim ticket; the Host reads its hash, email and expiry",
          (code, r, claim))
    clock.t += 61
    code, r = call(a, "POST", "/api/owner-ticket", {"auth": {"recovery_key": cv.new_recovery_key()[0]}},
                   headers=with_cookie(own))
    check(code == 403 and not r.get("ticket"), "a key that does not open the disk gets no ticket", (code, r))
    code, r = call(a, "POST", "/api/owner-ticket", {"auth": {"recovery_key": shown}}, headers=with_cookie(own))
    check(code == 200 and r.get("kind") == "claim" and json.load(open(door))["email"] == "owner@example.com",
          "with a key, another handoff's browser gets the claim ticket, with its email", (code, r))
    code, r = call(a, "POST", "/api/owner-ticket", {"auth": {"credential_id": CRED, "passphrase": PP}})
    check(code == 409 and r.get("handoff") is False,
          "with a key and no handoff, a Host with no owner waits for the email (the page shows the form)",
          (code, r))
    clock.t += 61
    OWNER["has"] = True
    code, r = call(a, "POST", "/api/owner-ticket", {}, headers=with_cookie(tok))
    check(code == 403 and r.get("need_key") is True,
          "once the Host has its owner the claim's cookie signs nobody in", (code, r))
    code, r = call(a, "POST", "/api/owner-ticket", {"auth": {"credential_id": CRED, "passphrase": PP}})
    claim = json.load(open(door))
    check(code == 200 and r.get("kind") == "sign-in" and
          r.get("action") == "https://orchard.k7x2m9.cloud.privatae.ai/api/owner/box-sign-in" and
          claim["kind"] == "sign-in" and claim["email"] is None,
          "with a key, a sign-in ticket for the Host's owner", (code, r, claim))
    check(cm.setup_done(paths), "and the box notes its setup done (the heartbeat's marker)")
    for _ in range(5):          # one key above; the cookie's call before it was no try
        last = call(a, "POST", "/api/owner-ticket", {"auth": {"credential_id": CRED, "passphrase": PP}})
    check(last[0] == 429, "five tries a minute, as for every key", last)
    clock.t += 61
    code, st = call(a, "GET", "/api/state", headers={"Cookie": f"{ag.HANDOFF_COOKIE}={tok}"})
    check(CODE not in json.dumps(st) and r["ticket"] not in json.dumps(st),
          "the state carries neither the code nor a ticket")
    code, r = call(a, "POST", "/api/setup-code", {"auth": {"recovery_key": shown}})
    check(code == 404, "the setup code is never answered again (/api/setup-code is gone)", (code, r))
    clock.t += ag.HANDOFF_TTL_S + 1
    code, st = call(a, "GET", "/api/state", headers={"Cookie": f"{ag.HANDOFF_COOKIE}={tok}"})
    check(st["handoff"] is False, "a handoff ends after its time", st)
    fake.reboot()
    cm.record_state(paths, "locked")
    code, r = call(a, "POST", "/api/owner-ticket", {"auth": {"recovery_key": shown}})
    check(code == 409, "while the vault is locked, no ticket", (code, r))
    OWNER["has"] = False

# Only the browser of the claim that set up the keys may make the owner
# without a key: one whose claim stopped, before another browser's claim,
# has nothing left.
with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v, a, clock = box(tmp)
    OWNER["has"] = False
    _s, _o, _e, ta = hand_over(a)
    _s, _o, _e, tb = hand_over(a, email="other@example.com")
    clock.t += 61
    code, r = call(a, "POST", "/api/enroll", {"passkey": None}, headers=with_cookie(ta))
    code, r = call(a, "POST", "/api/enroll/confirm", {"recovery_key": r.get("recovery_key")},
                   headers=with_cookie(ta))
    check(code == 200 and r.get("started") is True, "browser A starts a claim", (code, r))
    # It stops before the keys are set up: browser B sets them up.
    cc.write_json(paths.p(cm.CLAIM), {"phase": "failed", "error": "invented", "started_at": cm.iso()})
    clock.t += 61
    code, r = call(a, "POST", "/api/enroll", {"passkey": {"credential_id": CRED, "passphrase": PP}},
                   headers=with_cookie(tb))
    code, r = call(a, "POST", "/api/enroll/confirm", {"recovery_key": r.get("recovery_key")},
                   headers=with_cookie(tb))
    check(code == 200 and r.get("started") is True, "browser B starts the claim again", (code, r))
    pending = json.load(open(paths.p(cm.PENDING)))
    v.claim(pending["passkey"], pending["recovery_pass"])
    os.unlink(paths.p(cm.PENDING))
    clock.t += 61
    code, r = call(a, "POST", "/api/owner-ticket", {}, headers=with_cookie(ta))
    check(code == 403 and r.get("need_key") is True, "A, whose claim stopped, gets no ticket without a key",
          (code, r))
    code, r = call(a, "POST", "/api/owner-ticket", {}, headers=with_cookie(tb))
    check(code == 200 and r.get("kind") == "claim" and
          json.load(open(paths.p("run/cseries-door", cm.OWNER_CLAIM_NAME)))["email"] == "other@example.com",
          "B, whose claim set up the keys, gets the claim ticket, with B's email", (code, r))

# The page's own calls with the handoff's cookie are no guesses, so they do
# not count against the five tries a minute: two tabs (the card's button
# pressed twice) within one minute finish the setup. A typed code and a key
# still count.
with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v, a, clock = box(tmp)
    OWNER["has"] = False
    _s, _o, _e, t1 = hand_over(a)
    code, r = call(a, "POST", "/api/enroll", {"passkey": None}, headers=with_cookie(t1))
    k1 = r.get("recovery_key")
    _s, _o, _e, t2 = hand_over(a)              # the button again: the browser now holds t2
    code, r = call(a, "POST", "/api/enroll", {"passkey": {"credential_id": CRED, "passphrase": PP}},
                   headers=with_cookie(t2))
    k2 = r.get("recovery_key")
    code, r = call(a, "POST", "/api/enroll/confirm", {"recovery_key": k1}, headers=with_cookie(t2))
    check(code == 403 and r.get("replaced") is True, "the first tab's I saved it: refused, its key replaced",
          (code, r))
    code, r = call(a, "POST", "/api/enroll/confirm", {"recovery_key": k2}, headers=with_cookie(t2))
    check(code == 200 and r.get("started") is True,
          "the second tab's, in the same minute: the claim starts (the cookie's calls are no tries)", (code, r))
    pending = json.load(open(paths.p(cm.PENDING)))
    v.claim(pending["passkey"], pending["recovery_pass"])
    os.unlink(paths.p(cm.PENDING))
    code, r = call(a, "POST", "/api/owner-ticket", {}, headers=with_cookie(t2))
    check(code == 200 and r.get("kind") == "claim", "and its claim ticket, in that minute too", (code, r))
    for i in range(4):
        last = call(a, "POST", "/api/enroll", {"setup_code": "ffffff-ffffff-ffffff", "passkey": None})
    check(last[0] == 429, "a typed code still counts: two handoffs and three typed codes, and the fourth waits",
          last)
    clock.t += 61
    for i in range(5):
        last = call(a, "POST", "/api/owner-ticket", {"auth": {"recovery_key": cv.new_recovery_key()[0]}},
                    headers=with_cookie(t2))
    check(last[0] == 403, "five keys a minute, with the cookie or not", last)
    last = call(a, "POST", "/api/owner-ticket", {"auth": {"recovery_key": cv.new_recovery_key()[0]}},
                headers=with_cookie(t2))
    check(last[0] == 429, "and the sixth key waits", last)

# Once the setup is done (the keys set up and the Host's owner seen), the
# setup code hands nothing over: the code the platform held does nothing here.
with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v, a, clock = box(tmp)
    status, _o, _e, value = hand_over(a)
    check(status == 303 and value, "before the setup is done the code hands over", status)
    cm.note_setup_done(paths)
    clock.t += 61
    for origin in ("https://privatae.ai", UNLOCK_ORIGIN):
        status, out, extra, value = hand_over(a, origin=origin)
        check(status == 409 and value is None and extra.get("Content-Type", "").startswith("text/html"),
              f"after it, a right code from {origin} is refused with a page and no cookie", (status, out[:200]))

# The handoff over the socket: the redirect and the cookie as a browser gets them.
with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v, a, clock = box(tmp)
    sock_path = os.path.join(tmp, "agent.sock")
    srv_sock = socket.socket(socket.AF_UNIX)
    srv_sock.bind(sock_path)
    srv_sock.listen(4)
    ag.Handler.agent = a
    server = ag.Server(srv_sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    class UnixConn2(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.connect(sock_path)

    import urllib.parse as up
    c = UnixConn2("caddy")
    c.request("POST", "/api/setup", body=up.urlencode({"setup_code": CODE, "email": "owner@example.com"}),
              headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://privatae.ai",
                       "X-Forwarded-For": "203.0.113.7"})
    resp = c.getresponse()
    resp.read()
    check(resp.status == 303 and resp.getheader("Location") == "/" and
          (resp.getheader("Set-Cookie") or "").startswith(ag.HANDOFF_COOKIE + "=") and
          resp.getheader("Cache-Control") == "no-store",
          "over the socket: 303 to the page, the cookie, not cached", resp.getheaders())
    c = UnixConn2("caddy")
    c.request("POST", "/api/setup", body="setup_code=x&email=y",
              headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://elsewhere.example"})
    resp = c.getresponse()
    page = resp.read()
    check(resp.status == 403 and resp.getheader("Content-Type").startswith("text/html") and
          resp.getheader("Set-Cookie") is None and b'href="/"' in page,
          "a refused handoff over the socket: a page with the way on, no cookie", resp.status)
    server.shutdown()
    srv_sock.close()


# 10. A stop waits for the request in hand.
class StopMidRequest:
    """A stand-in agent: the stop comes while its request is under way."""

    def __init__(self):
        self.server = None
        self.answered = []

    def respond(self, method, path, body_bytes, xff, headers=None):
        self.server.stop(signal.SIGTERM, None)
        self.answered.append(path)
        return 200, {"ok": True, "recovery_key": "INVENTED-KEY-SHOWN-ONCE"}, []


def serve_in_thread(server, out):
    def run():
        try:
            server.serve_forever(poll_interval=0.05)
        except SystemExit as e:
            out["exit"] = e.code
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def post_over(sock_path, path):
    class Conn(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.connect(sock_path)
    c = Conn("caddy", timeout=10)
    c.request("POST", path, body=b"{}", headers={"Content-Type": "application/json",
                                                   "X-Forwarded-For": "203.0.113.7"})
    resp = c.getresponse()
    return resp.status, json.loads(resp.read())


saved_agent = ag.Handler.agent
try:
    with tempfile.TemporaryDirectory() as tmp:
        sock_path = os.path.join(tmp, "agent.sock")
        srv_sock = socket.socket(socket.AF_UNIX)
        srv_sock.bind(sock_path)
        srv_sock.listen(4)
        stub = StopMidRequest()
        ag.Handler.agent = stub
        server = ag.Server(srv_sock)
        stub.server = server
        out = {}
        t = serve_in_thread(server, out)
        status, body = post_over(sock_path, "/api/keys/recovery")
        t.join(5)
        check(status == 200 and body.get("recovery_key") == "INVENTED-KEY-SHOWN-ONCE" and
              stub.answered == ["/api/keys/recovery"],
              "a stop during a request: its answer is sent whole", (status, body))
        check(out.get("exit") == 0 and not t.is_alive(), "then the agent exits", out)
        srv_sock.close()

    with tempfile.TemporaryDirectory() as tmp:
        srv_sock = socket.socket(socket.AF_UNIX)
        srv_sock.bind(os.path.join(tmp, "agent.sock"))
        srv_sock.listen(4)
        server = ag.Server(srv_sock)
        server.service_actions = lambda: server.stop(signal.SIGTERM, None)
        out = {}
        t = serve_in_thread(server, out)
        t.join(5)
        check(out.get("exit") == 0 and not t.is_alive(), "a stop between requests: the agent exits at once", out)
        srv_sock.close()
finally:
    ag.Handler.agent = saved_agent

with open(os.path.join(ROOT, "vm/cloud/overlay/usr/local/sbin/cseries-unlock-agent"), encoding="utf-8") as f:
    agent_src = f.read()
main_src = agent_src[agent_src.index("def main():"):]
check("signal.signal(signal.SIGTERM, server.stop)" in main_src and
      main_src.index("signal.signal(") < main_src.index("serve_forever()"),
      "main() makes Server.stop SIGTERM's handler before it serves")
with open(os.path.join(ROOT, "vm/cloud/overlay/etc/systemd/system/cseries-unlock-agent.service"),
          encoding="utf-8") as f:
    unit = [ln.strip() for ln in f if not ln.lstrip().startswith("#")]
check("KillMode=mixed" in unit, "the unit sends a stop's SIGTERM to the agent alone (KillMode=mixed)", unit)

sys.exit(failed)
