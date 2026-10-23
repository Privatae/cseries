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

Container-free: python3 scripts/test_cloud_box_unlock_agent.py
"""
import http.client
import importlib.util
import json
import os
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
    fake = FakeLuks(tmp)
    v = cv.Vault(paths, fake)
    v.create(MID, size=8 * cv.GIB)
    clock = Clock()
    a = ag.Agent(paths, v, fake, ag.Limiter(clock))
    return paths, fake, v, a, clock


def call(a, method, path, body=None, xff="203.0.113.7"):
    return a.handle(method, path, json.dumps(body).encode() if body is not None else b"", xff)


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

sys.exit(failed)
