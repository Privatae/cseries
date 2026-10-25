#!/usr/bin/env python3
"""A C-Series Cloud Host joins the network with the code its claim carried
(server/bridge/membership.py join_from_claim, server/cloud_machine.py
member_enrol), through the same Membership.join as Settings -> Network.

  1. A Host with no machine directory (one on a person's own computer) reads
     nothing and starts no pickup at boot; a cloud box starts one.
  2. On a cloud box it joins only when member-enrol.json holds a code, is
     not past its end, the Host is not a member yet, and the Host has its
     owner and that owner is the file's email. Each other case joins nothing.
  3. The join is Membership.join with the code and no name (the door names
     the Host, as for a join from Settings); once, whatever the door answers:
     a code that joined, or that the door refused, is never tried again,
     and the codes tried are kept 0600 in the passport directory, which
     leaving the network keeps.
  4. A network that could not be reached, or a door that could not answer
     now, is tried again; a refusal is shown the way a refused join from
     Settings is (status: not a member, the door's words).
  5. The pickup looks every 30 s while the Host waits for its owner or the
     network, and at the hourly check otherwise.

No network, no container: the enrolment round trip and the member client are
stand-ins.
    python3 scripts/test_member_claim_join_unit.py   (repo root, image's deps)
"""
import asyncio
import datetime as dt
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path[:0] = [ROOT]
from server import cloud_machine  # noqa: E402
from server.bridge import membership as M  # noqa: E402
from server.bridge import passport as pp  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


CODE = "7KQM2HXD9RTV"                                   # invented
EMAIL = "owner@example.com"
NOW = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)


class Passport:
    """What status() reads of a passport."""
    entity_id, host_uuid, network, door = 4127, "0f3a6c1e-9b2d-4e8f-a1c5-7d6e2b9f0a34", "privatae", "net.example"
    not_after, renew_after = NOW + dt.timedelta(days=7), NOW + dt.timedelta(days=1)
    enrolled_at = NOW.isoformat()


PASSPORT = Passport()


class Door:
    """The enrolment round trip (membership.enrol), as join() calls it."""

    def __init__(self):
        self.calls = []
        self.answers = []

    async def __call__(self, code, *, directory, anchor, host_name=None, host=None, port=None):
        self.calls.append({"code": code, "host_name": host_name})
        if self.answers:
            a = self.answers.pop(0)
            if isinstance(a, Exception):
                raise a
        return PASSPORT


def started(self, p):
    """Membership._start without the member client's connection."""
    self.passport = p
    return None


M.Membership._start = started


def setup(tmp, *, owner=EMAIL, doc=None, machine=True):
    mdir = Path(tmp) / "machine"
    mdir.mkdir(exist_ok=True)
    if machine:
        os.environ[cloud_machine.DIR_ENV] = str(mdir)
    else:
        os.environ.pop(cloud_machine.DIR_ENV, None)
    if doc is not None:
        (mdir / cloud_machine.MEMBER_ENROL).write_text(json.dumps(doc))
    who = {"email": owner}
    m = M.Membership(directory=Path(tmp) / "passport", anchor=Path(tmp) / "root.crt",
                     clock=lambda: NOW, owner_email=lambda: who["email"])
    door = Door()
    M.enrol = door
    return m, door, who, mdir


def run(coro):
    return asyncio.run(coro)


GOOD = {"code": "7KQM-2HXD-9RTV", "email": "Owner@Example.com", "until": int(NOW.timestamp()) + 3600}

# 1. Not a cloud machine.
with tempfile.TemporaryDirectory() as tmp:
    m, door, who, _ = setup(tmp, doc=GOOD, machine=False)
    check(run(m.join_from_claim()) == "not a cloud machine" and door.calls == [],
          "a Host on a person's own computer reads no code and joins nothing")

    async def boot_local():
        await m.boot()
        return m._claim_task

    check(run(boot_local()) is None, "and starts no pickup at boot")

with tempfile.TemporaryDirectory() as tmp:
    m, door, who, _ = setup(tmp, doc=None)

    async def boot_cloud():
        await m.boot()
        task = m._claim_task
        await m.shutdown()
        return task

    check(run(boot_cloud()) is not None, "a Host on a cloud machine starts the pickup at boot")

# 2. The cases that join nothing.
cases = [
    ("no file", None, EMAIL, "no code"),
    ("a file with no code", {**GOOD, "code": "nope"}, EMAIL, "no code"),
    ("a file with no end", {k: v for k, v in GOOD.items() if k != "until"}, EMAIL, "no code"),
    ("past its end", {**GOOD, "until": int(NOW.timestamp())}, EMAIL, "expired"),
    ("no owner yet", GOOD, None, "no owner"),
]
for what, doc, owner, want in cases:
    with tempfile.TemporaryDirectory() as tmp:
        m, door, who, _ = setup(tmp, doc=doc, owner=owner)
        got = run(m.join_from_claim())
        check(got == want and door.calls == [], f"{what}: {want}, and no join", got)

with tempfile.TemporaryDirectory() as tmp:
    m, door, who, _ = setup(tmp, doc=GOOD, owner="someone@example.com")
    first, second = run(m.join_from_claim()), run(m.join_from_claim())
    check(first == "not the owner" and second == "tried" and door.calls == [],
          "a code for another email than the owner's never joins, and is not looked at again",
          (first, second))

with tempfile.TemporaryDirectory() as tmp:
    m, door, who, _ = setup(tmp, doc=GOOD)
    m.passport = "already"
    check(run(m.join_from_claim()) == "member" and door.calls == [],
          "a Host that is a member already joins nothing")

# 3. The join, once.
with tempfile.TemporaryDirectory() as tmp:
    m, door, who, _ = setup(tmp, doc=GOOD)
    got = run(m.join_from_claim())
    check(got == "joined" and door.calls == [{"code": CODE, "host_name": None}] and m.passport is PASSPORT,
          "owner and email match: Membership.join with the code and no name, as from Settings", (got, door.calls))
    tried = Path(tmp) / "passport" / M.CLAIM_TRIED
    check(tried.is_file() and stat.S_IMODE(tried.stat().st_mode) == 0o600 and CODE not in tried.read_text(),
          "the code tried is kept by its hash, 0600 in the passport directory")
    pp.delete(Path(tmp) / "passport")
    m.passport = None
    again = run(m.join_from_claim())
    check(again == "tried" and len(door.calls) == 1 and tried.is_file(),
          "after leaving the network the same code is not tried again (leave keeps the record)", again)

with tempfile.TemporaryDirectory() as tmp:
    m, door, who, _ = setup(tmp, doc=GOOD)
    door.answers = [M.EnrolmentRefused("this code is not valid", 403)]
    first = run(m.join_from_claim())
    st = m.status()
    second = run(m.join_from_claim())
    check(first == "refused" and second == "tried" and len(door.calls) == 1,
          "a code the door refused is tried once only", (first, second))
    check(st["member"] is False and st["last_error"] == "this code is not valid",
          "and the refusal shows as a refused join from Settings does: not a member, the door's words", st)

# 4. Tried again when the network could not take it.
for what, err in (("the network could not be reached", M.NetworkUnreachable("could not reach the network: x")),
                  ("the door could not answer now", M.EnrolmentRefused("try later", 503)),
                  ("the door asked to wait", M.EnrolmentRefused("too many", 429))):
    with tempfile.TemporaryDirectory() as tmp:
        m, door, who, _ = setup(tmp, doc=GOOD)
        door.answers = [err]
        first, second = run(m.join_from_claim()), run(m.join_from_claim())
        check(first == "waiting" and second == "joined" and len(door.calls) == 2,
              f"{what}: the code is tried again, and joins", (first, second))

# 5. How long the pickup waits.
def waits_for(m, rounds):
    seen = []

    async def sleep(s):
        seen.append(s)
        if len(seen) >= rounds:
            raise asyncio.CancelledError

    real, M._sleep = M._sleep, sleep
    try:
        try:
            run(m._claim_loop())
        except asyncio.CancelledError:
            pass
    finally:
        M._sleep = real
    return seen


with tempfile.TemporaryDirectory() as tmp:
    m, door, who, mdir = setup(tmp, doc=None, owner=None)
    seen = waits_for(m, 2)
    check(seen == [M.CLAIM_POLL_S, M.CLAIM_POLL_S], "before the claim (no owner, no code): every 30 s", seen)
    (mdir / cloud_machine.MEMBER_ENROL).write_text(json.dumps(GOOD))
    seen = waits_for(m, 1)
    check(seen == [M.CLAIM_POLL_S] and door.calls == [], "a code and still no owner: every 30 s", seen)
    who["email"] = EMAIL
    door.answers = [M.NetworkUnreachable("could not reach the network: x")]
    seen = waits_for(m, 2)
    check(seen == [M.CLAIM_POLL_S, M.RENEW_CHECK_S] and len(door.calls) == 2 and m.passport is PASSPORT,
          "the owner made: joins within 30 s, tries again 30 s after a network that could not be reached, "
          "then waits for the hourly check", seen)

with tempfile.TemporaryDirectory() as tmp:
    m, door, who, _ = setup(tmp, doc=None, owner=EMAIL)
    check(waits_for(m, 1) == [M.RENEW_CHECK_S], "a Host with its owner and no code: the hourly check only")

sys.exit(failed)
