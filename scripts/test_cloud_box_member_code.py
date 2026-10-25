#!/usr/bin/env python3
"""The Cloud claim carries the network enrolment code (vm/cloud/overlay/usr/
local/sbin/cseries-unlock-agent, cseries_machine.write_member_enrol), with
the vault on the stand-in cryptsetup (scripts/cloud_box_fake_luks.py).

  1. A handoff whose form carries member_code keeps it with the handoff (in
     /run, 0600), upper case with the hyphens taken out; the claim ticket then
     writes it, with the handoff's email and end, into member-enrol.json
     (0640, the directory's group) beside owner-claim.json. The code is never
     in the state the page reads.
  2. A member_code that is not a code is dropped: the handoff and the claim go
     on as without one, and no member-enrol.json is written.
  3. A handoff with no member_code writes no member-enrol.json.
  4. A sign-in ticket writes none, even from a handoff that carried a code.

Container-free: python3 scripts/test_cloud_box_member_code.py
"""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import urllib.parse as up
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


MID = "3d2c8f61-0b4e-4a7d-9c15-6e8a2f9b4d70"           # invented
CFG = {"name": "willow", "address_id": "p4r8tz", "zone": "cloud.privatae.ai", "machine_id": MID,
       "heartbeat_url": "https://127.0.0.1:9/never"}
SETUP = "1f2e3d-4c5b6a-7f8e9d"                          # invented
CRED = "Q3JlZGVudGlhbC1pbnZlbnRlZC0wMg"
PP = cv.passkey_passphrase(bytes(range(1, 33)), MID, CRED)
UNLOCK_ORIGIN = "https://unlock-willow.p4r8tz.cloud.privatae.ai"
JOIN = "7KQM-2HXD-9RTV"                                 # invented
JOIN_CANON = "7KQM2HXD9RTV"
OWNER = {"has": False}


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


def box(tmp):
    paths = cc.Paths(tmp)
    cc.write_json(paths.cloud_json(), CFG)
    with open(paths.setup_code(), "w") as f:
        f.write(SETUP + "\n")
    os.makedirs(paths.p("run/cseries-door"))
    os.makedirs(paths.p("var/log/journal"))
    fake = FakeLuks(tmp)
    v = cv.Vault(paths, fake)
    v.create(MID, size=8 * cv.GIB)
    clock = Clock()
    a = ag.Agent(paths, v, fake, ag.Limiter(clock), now=clock, owner=lambda: OWNER["has"])
    return paths, fake, v, a, clock


def hand_over(a, fields):
    body = up.urlencode(fields).encode()
    heads = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://privatae.ai"}
    status, out, extra = a.respond("POST", "/api/setup", body, "203.0.113.9", heads)
    cookie = dict(extra).get("Set-Cookie")
    return status, (cookie.split(";")[0].split("=", 1)[1] if cookie else None)


def call(a, method, path, body=None, headers=None):
    return a.handle(method, path, json.dumps(body).encode() if body is not None else b"", "203.0.113.9", headers)


def with_cookie(value):
    return {"Cookie": f"{ag.HANDOFF_COOKIE}={value}", "Origin": UNLOCK_ORIGIN}


def claim_through(a, v, paths, clock, tok):
    """enroll, I saved it, the claim's re-encryption, then the claim ticket."""
    code, r = call(a, "POST", "/api/enroll", {"passkey": {"credential_id": CRED, "passphrase": PP}},
                   headers=with_cookie(tok))
    assert code == 200, (code, r)
    clock.t += 61
    code, r = call(a, "POST", "/api/enroll/confirm", {"saved": True, "recovery_key": r["recovery_key"]},
                   headers=with_cookie(tok))
    assert code == 200 and r.get("started"), (code, r)
    pending = json.load(open(paths.p(cm.PENDING)))
    v.claim(pending["passkey"], pending["recovery_pass"])
    os.unlink(paths.p(cm.PENDING))
    OWNER["has"] = False
    clock.t += 61
    return call(a, "POST", "/api/owner-ticket", {}, headers=with_cookie(tok))


check(ag.member_code(JOIN) == JOIN_CANON and ag.member_code(" 7kqm 2hxd-9rtv ") == JOIN_CANON,
      "member_code takes the code as the Host's own check does: spaces and hyphens out, upper case")
check(all(ag.member_code(x) is None for x in
          ("", "7KQM-2HXD-9RT", "7KQM-2HXD-9RTVX", "7KQM-2HXD-9RTI", "7KQM-2HXD-9RTU", None, 12, "A" * 70)),
      "member_code drops anything that is not 12 Crockford characters (no I, L, O, U)")

# 1. A handoff with a code: kept, then written with the claim ticket.
with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v, a, clock = box(tmp)
    door = paths.p("run/cseries-door")
    enrol = os.path.join(door, cm.MEMBER_ENROL_NAME)
    status, tok = hand_over(a, {"setup_code": SETUP, "email": "Owner@Example.com", "member_code": JOIN})
    check(status == 303 and tok, "a handoff with a member_code is taken like any other", status)
    kept = json.load(open(paths.p(cm.HANDOFF)))["handoffs"]
    rec = next(iter(kept.values()))
    check(rec.get("member_code") == JOIN_CANON and rec.get("email") == "owner@example.com" and
          stat.S_IMODE(os.stat(paths.p(cm.HANDOFF)).st_mode) == 0o600,
          "the code is kept with the handoff, in its canonical form, 0600 in /run", rec)
    code, st = call(a, "GET", "/api/state", headers={"Cookie": f"{ag.HANDOFF_COOKIE}={tok}"})
    check(JOIN_CANON not in json.dumps(st) and not os.path.exists(enrol),
          "the state the page reads never has the code, and nothing is written for the Host yet", st)
    code, r = claim_through(a, v, paths, clock, tok)
    check(code == 200 and r.get("kind") == "claim", "the claim ticket is made", (code, r))
    doc = json.load(open(enrol))
    check(doc == {"code": JOIN_CANON, "email": "owner@example.com", "until": int(rec["until"])},
          "member-enrol.json holds the code, the handoff's email and the handoff's end", doc)
    st_ = os.stat(enrol)
    check(stat.S_IMODE(st_.st_mode) == 0o640 and st_.st_gid == os.stat(door).st_gid,
          "member-enrol.json is 0640 and the directory's group, as the directory keeps it",
          (oct(st_.st_mode), st_.st_gid))
    check(os.path.exists(os.path.join(door, cm.OWNER_CLAIM_NAME)), "owner-claim.json is written as before")
    left = json.load(open(paths.p(cm.HANDOFF)))["handoffs"]
    check(left and all("member_code" not in r for r in left.values()) and
          JOIN_CANON not in open(paths.p(cm.HANDOFF)).read(),
          "once written for the Host, the code is out of the handoff store: one copy on the box", left)
    clock.t = doc["until"] - 1
    call(a, "GET", "/api/state", headers={"Cookie": f"{ag.HANDOFF_COOKIE}={tok}"})
    check(os.path.exists(enrol), "member-enrol.json stays while its code can still be used")
    clock.t = doc["until"]
    call(a, "GET", "/api/state")
    check(not os.path.exists(enrol), "member-enrol.json is removed once its code's handoff has ended")
    clock.t = doc["until"] - 120
    cm.write_member_enrol(paths, JOIN_CANON, "owner@example.com", doc["until"])

    # 4. A sign-in ticket afterwards writes no code again.
    os.unlink(enrol)
    OWNER["has"] = True
    clock.t += 61
    code, r = call(a, "POST", "/api/owner-ticket", {"auth": {"credential_id": CRED, "passphrase": PP}},
                   headers=with_cookie(tok))
    check(code == 200 and r.get("kind") == "sign-in" and not os.path.exists(enrol),
          "a sign-in ticket writes no member-enrol.json, even from a handoff that carried a code", (code, r))

# 1b. Two presses of the card, each with its own code: the claim keeps no code
# in the handoff store, from either.
with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v, a, clock = box(tmp)
    hand_over(a, {"setup_code": SETUP, "email": "owner@example.com", "member_code": "3MPQ-8RTV-WXYZ"})
    clock.t += 5
    status, tok = hand_over(a, {"setup_code": SETUP, "email": "owner@example.com", "member_code": JOIN})
    code, r = claim_through(a, v, paths, clock, tok)
    raw = open(paths.p(cm.HANDOFF)).read()
    check(code == 200 and "3MPQ8RTVWXYZ" not in raw and JOIN_CANON not in raw,
          "after the claim no handoff keeps a code, the earlier press's included", raw)
    check(json.load(open(paths.p("run/cseries-door", cm.MEMBER_ENROL_NAME)))["code"] == JOIN_CANON,
          "the Host gets the code of the handoff that made the claim")

# 2. A code that is not one is dropped; the claim goes on.
with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v, a, clock = box(tmp)
    enrol = paths.p("run/cseries-door", cm.MEMBER_ENROL_NAME)
    status, tok = hand_over(a, {"setup_code": SETUP, "email": "owner@example.com", "member_code": "not-a-code"})
    rec = next(iter(json.load(open(paths.p(cm.HANDOFF)))["handoffs"].values()))
    check(status == 303 and tok and "member_code" not in rec,
          "a member_code that is not a code is dropped, and the handoff is taken", (status, rec))
    code, r = claim_through(a, v, paths, clock, tok)
    check(code == 200 and r.get("kind") == "claim" and not os.path.exists(enrol),
          "the claim goes on without it, and no member-enrol.json is written", (code, r))

# 3. No code at all: no file.
with tempfile.TemporaryDirectory() as tmp:
    paths, fake, v, a, clock = box(tmp)
    enrol = paths.p("run/cseries-door", cm.MEMBER_ENROL_NAME)
    status, tok = hand_over(a, {"setup_code": SETUP, "email": "owner@example.com"})
    code, r = claim_through(a, v, paths, clock, tok)
    check(code == 200 and r.get("kind") == "claim" and not os.path.exists(enrol),
          "a handoff with no member_code: the claim as before, and no member-enrol.json", (code, r))

sys.exit(failed)
