#!/usr/bin/env python3
"""A Host with no platform backs itself up (server/host_backup.py).

The parts that decide, for a Host that runs standalone or as a network member:

  * WHERE: the place the owner chose in Settings (the network's storage, their
    own bucket, or a folder on this machine), else the network's storage for a
    member and a folder on this machine for a standalone Host. A Host that is
    told to use the network and is not on one says so once per scheduled
    time, and writes nothing anywhere else.
  * THE KEY: the owner makes their recovery key on the Host (only its public
    half arrives); it is the active key from then on, and a key the platform
    later offers never displaces it: it waits as an offer. A transfer still
    retires it, and adopting an offer replaces it.
  * A RUN to a folder or a bucket: one encrypted file and its key file, the
    recovery key opens both, nothing goes over the bridge, the run is recorded
    for the heartbeat and the restore list, and the folder keeps the newest
    backups only, never one a restore is reading.
  * REPORTING: backup.report goes out for a member, never for a standalone Host.
  * EXPORT: a whole-Host file in exports/, recorded apart from the backups.

Real `age`; a stand-in pg_dump and a stand-in database pool.

    python3 scripts/test_standalone_backups.py
"""
import asyncio
import datetime as dt
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))      # as the image's PYTHONPATH has it
for v in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
    os.environ.pop(v, None)
if not Path(os.environ.get("AGE_BIN", "/usr/local/bin/age")).is_file():
    os.environ["AGE_BIN"] = shutil.which("age") or "/usr/local/bin/age"

from server import age_keys, backup_key, backup_stores  # noqa: E402
from server import host_backup as hb  # noqa: E402
from server.bridge.no_platform import NoPlatform  # noqa: E402
from server.vault import identity as vault_identity  # noqa: E402

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILED.append(label)


def run(coro):
    return asyncio.run(coro)


def utc(*a):
    return dt.datetime(*a, tzinfo=dt.timezone.utc)


def raw(recipient):
    return age_keys.recipient_bytes(recipient)


# ── stand-ins: the bridge, pg_dump, the apps tree, the database ──────────────
class Member:
    """The member door's client, as host_backup sees it."""
    network_member = True
    _connected = True

    def __init__(self):
        self.calls = []

    async def request_full(self, op, params=None):
        self.calls.append(op)
        return {"ok": False, "error": f"not offered by the network: {op}", "error_kind": "infra"}

    async def request(self, op, params=None, **_):
        self.calls.append(op)
        return {}


class Recorder(NoPlatform):
    """A NoPlatform that remembers what was asked of it."""

    def __init__(self):
        self.calls = []

    async def request_full(self, op, params=None):
        self.calls.append(op)
        return await super().request_full(op, params)

    async def request(self, op, params=None, **kw):
        self.calls.append(op)
        return await super().request(op, params, **kw)


BRIDGE = {"bc": Recorder()}
hb._raw_bridge = lambda: BRIDGE["bc"]
hb._bridge = lambda: BRIDGE["bc"]

tmp = Path(tempfile.mkdtemp(prefix="sb-test-"))
fake_dump = tmp / "pg_dump"
fake_dump.write_text('#!/bin/sh\nexec python3 -c "import sys; sys.stdout.buffer.write(b\'PGDMP\' + bytes(range(256)) * 8)"\n')
fake_dump.chmod(fake_dump.stat().st_mode | stat.S_IEXEC)
hb.PG_DUMP = str(fake_dump)
apps = tmp / "apps"
(apps / "app-state/user-files").mkdir(parents=True)
(apps / "app-state/user-files/notes.txt").write_text("my notes\n")
hb.APPS_ROOT = str(apps)
hb.APPS_EXCLUDES = tuple(p.replace("/var/lib/cseries", str(apps)) for p in hb.APPS_EXCLUDES)
hb.WORKDIR = tmp / "work"
hb.LOCAL_DIR = apps / ".host-backups"
hb.PART_BYTES = 4096


class Pool:
    def __init__(self):
        self.bp = {}
        self.runs = []
        self.state = {}
        self.secrets = {}

    def _run(self, rid):
        return self.runs[rid - 1]

    async def fetchval(self, q, *a):
        if q.startswith("SELECT secret FROM kernel_secrets"):
            return self.secrets.get(a[0])
        if "behavioral_params" in q:
            return json.dumps(self.bp)
        if "current_database" in q:
            return "hostmem"
        if q.startswith("INSERT INTO host_backup_runs"):
            cols = q.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
            row = {"id": len(self.runs) + 1, "started_at": dt.datetime.now(dt.timezone.utc), "parts": {}}
            vals = list(a)
            for c in cols:
                if c == "reason":
                    row["reason"] = vals.pop(0)
                elif c == "state":
                    row["state"] = "running" if "'running'" in q else vals.pop(0)
                elif c == "stored":
                    row["stored"] = vals.pop(0)
                elif c == "error":
                    row["error"] = vals.pop(0) if vals else None
                elif c == "recipients" and "'[]'" not in q:
                    row["recipients"] = vals.pop(0) if vals else None
            if "'failed'" in q and "VALUES ($1, 'failed'" in q:
                row["state"] = "failed"
            self.runs.append(row)
            return row["id"]
        if q.startswith("UPDATE host_backup_runs SET finished_at"):
            r = self._run(a[0])
            r.update(state=a[1], object_key=a[2], sidecar_key=a[3], size_bytes=a[4], sha256=a[5],
                     parts=json.loads(a[8]), error=a[9])
            return dt.datetime.now(dt.timezone.utc)
        if "max(started_at)" in q:
            states = [s for s in ("ok", "partial", "no_recipient", "no_destination") if f"'{s}'" in q]
            if "state = $1" in q:
                states.append(a[0])
            got = [r["started_at"] for r in self.runs if r.get("state") in states]
            return max(got) if got else None
        if "max(id)" in q:
            ids = [r["id"] for r in self.runs
                   if r.get("state") in ("ok", "partial") and r.get("reason") != "restore"]
            return max(ids) if ids else None
        if "host_backup_state" in q:
            return self.state.get(a[0])
        raise AssertionError(q)

    async def fetchrow(self, q, *a):
        if "FROM host_backup_runs" in q:
            skip = [s for s in ("running", "no_recipient", "no_destination") if f"'{s}'" in q]
            rows = [r for r in self.runs if r.get("state") not in skip]
            if not rows:
                return None
            r = rows[-1]
            return {"started_at": r["started_at"], "state": r["state"]}
        raise AssertionError(q)

    async def execute(self, q, *a):
        if q.startswith("INSERT INTO kernel_secrets"):
            self.secrets.setdefault(a[0], a[1])
            return
        if "host_backup_state" in q:
            if q.startswith("DELETE"):
                self.state.pop(a[0], None)
            else:
                self.state[a[0]] = a[1]
            return
        if q.startswith("UPDATE host_backup_runs SET stamp"):
            self._run(a[0])["stamp"] = a[1]
            return
        if q.startswith("UPDATE host_backup_runs SET state='failed'") and "WHERE state='running'" in q:
            # The loop's start: a run the Host restarted during is failed.
            for r in self.runs:
                if r.get("state") == "running":
                    r.update(state="failed", error="interrupted: the Host restarted during this run")
            return
        raise AssertionError(q)

    async def fetch(self, q, *a):
        if "FROM kernel_secrets" in q:
            return [{"id": k, "secret": v} for k, v in self.secrets.items()
                    if k == a[0] or k.startswith(a[1].rstrip("%"))]
        if "push:%" in q:
            return []
        if "FROM host_backup_runs" in q:
            return [{"parts": r.get("parts")} for r in reversed(self.runs)]
        raise AssertionError(q)


rec_ident, rec_rec = age_keys.new_pair()        # the owner's recovery key, made in their browser
host_ident, host_rec = age_keys.new_pair()      # this Host's own vault key
keyrows: list = []


async def _list_active(pool, ident):
    return [r for r in keyrows if not r.get("gone")]


async def _get_active(pool, ident, kind="host-recovery", label=None):
    for r in reversed(keyrows):
        if r["kind"] == kind and not r.get("gone") and (label is None or r["label"] == label):
            return r
    return None


async def _list_all(pool, ident, kind):
    return [dict(r, superseded_at=utc(2026, 9, 1) if r.get("gone") else None)
            for r in reversed(keyrows) if r["kind"] == kind]


async def _put_active(pool, *, ident, pub_raw, sealed, origin, kind="bastion", label=""):
    for r in keyrows:
        if r["kind"] == kind and (kind == "host-recovery" or r["label"] == label):
            r["gone"] = True
    keyrows.append({"kind": kind, "label": label, "pubkey": pub_raw, "origin": origin,
                    "created_at": dt.datetime.now(dt.timezone.utc)})


async def _retire_recovery(pool, ident):
    hit = False
    for r in keyrows:
        if r["kind"] == "host-recovery" and not r.get("gone"):
            r["gone"] = hit = True
    return hit


async def _retire_registered_before(pool, ident, when):
    out = []
    for r in keyrows:
        if not r.get("gone") and r["created_at"] < when:
            r["gone"] = True
            out.append({"kind": r["kind"], "label": r["label"], "fingerprint": backup_key.fingerprint(r["pubkey"])})
    return out


async def _host_pubkey(pool):
    return raw(host_rec)

backup_key.list_active = _list_active
backup_key.get_active = _get_active
backup_key.list_all = _list_all
backup_key.put_active = _put_active
backup_key.retire_recovery = _retire_recovery
backup_key.retire_registered_before = _retire_registered_before
vault_identity.host_pubkey = _host_pubkey
from server.routes import gitea_client  # noqa: E402
gitea_client.gitea_creds = lambda: None

REC_FP = backup_key.fingerprint(raw(rec_rec))


def age_open(identity, data):
    argv, fd = hb.age_decrypt_args(identity)
    try:
        return subprocess.run(argv, input=data, capture_output=True, pass_fds=(fd,))
    finally:
        os.close(fd)


def members(identity, blob):
    got = age_open(identity, blob)
    if got.returncode != 0:
        return None
    from compression import zstd
    with tarfile.open(fileobj=io.BytesIO(zstd.decompress(got.stdout)), mode="r:") as t:
        return {m.name: t.extractfile(m).read() for m in t if m.isfile()}


print("== who this Host is: standalone, a member, or on the platform")
check("no bridge yet, or NoPlatform: standalone", hb.host_mode(None) == "standalone"
      and hb.host_mode(NoPlatform()) == "standalone")
check("the member door's client: a member", hb.host_mode(Member()) == "member")
check("any other client: the platform", hb.host_mode(object()) == "platform")

print("== where backups go")
check("a standalone Host that chose nothing: a folder on this machine",
      hb.destination_of({}, "standalone") == "local")
check("a member that chose nothing, on a network that keeps backups: the network's storage",
      hb.destination_of({}, "member", True) == "network")
check("a member that chose nothing, on a network that keeps none or has not said: a folder on this machine",
      hb.destination_of({}, "member", False) == "local" and hb.destination_of({}, "member", None) == "local"
      and hb.destination_of({}, "member") == "local")
check("on the platform, as before: platform storage", hb.destination_of({}, "platform") == "network")
check("the owner's choice wins, whatever the Host is",
      hb.destination_of({"backup": {"where": "s3"}}, "member") == "s3"
      and hb.destination_of({"backup": {"where": "network"}}, "standalone") == "network"
      and hb.destination_of({"backup": {"where": "local"}}, "platform") == "local")
check("a choice that is not one of the three is not taken",
      hb.destination_of({"backup": {"where": "nfs"}}, "standalone") == "local")
check("the folder keeps a week of backups unless told otherwise",
      hb.local_keep({}) == 7 and hb.local_keep({"backup": {"local_keep": 3}}) == 3
      and hb.local_keep({"backup": {"local_keep": 0}}) == 1
      and hb.local_keep({"backup": {"local_keep": 10_000}}) == hb.LOCAL_KEEP_MAX
      and hb.local_keep({"backup": {"local_keep": "x"}}) == 7)
check("the folder is left out of the backups it holds", hb.excluded(".host-backups")
      and hb.excluded(".host-backups/20261001T020000Z.tar.zst.age"))

print("== the owner makes the recovery key on this Host")
pool = Pool()
for bad, why in ((("age1" + "q" * 58, REC_FP), "not a key"),
                 ((rec_rec, "0" * 16), "fingerprint"),
                 ((host_rec, backup_key.fingerprint(raw(host_rec))), "this Host's own")):
    try:
        run(hb.make_own_recovery_key(pool, 1, bad[0], bad[1], "my key"))
        check(f"refused: {why}", False)
    except hb.BackupError as e:
        check(f"refused: {why}", True, str(e))
check("  and nothing was kept", keyrows == [] and "recovery_own" not in pool.state)
got = run(hb.make_own_recovery_key(pool, 1, rec_rec, REC_FP, "Recovery key made on this Host, 2026-10-19"))
check("the public half becomes the active recovery key, made in the owner's browser",
      got["fingerprint"] == REC_FP and got["first"] is True
      and [(r["kind"], r["origin"], r["pubkey"]) for r in keyrows if not r.get("gone")]
      == [("host-recovery", "owner-browser", raw(rec_rec))], keyrows)
pin = run(hb.pin_recovery_key(pool, 1))
check("the Host says the active key was made here", pin["pinned"]["fingerprint"] == REC_FP
      and pin["pinned"]["made_on_host"] is True, pin)
check("backups are encrypted to it", [r["fingerprint"] for r in run(hb.recipients(pool, 1))] == [REC_FP])
try:
    run(hb.make_own_recovery_key(pool, 1, age_keys.new_pair()[1], None, "x"))
    check("a second key is not slipped in: replacing must be asked for", False)
except hb.BackupError as e:
    check("a second key is not slipped in: replacing must be asked for", "already" in str(e), str(e))
label_bad = run(hb.make_own_recovery_key(pool, 1, rec_rec, REC_FP, "AGE-SECRET-KEY-1" + "Q" * 58))
check("a label carrying a private key is never kept",
      all("AGE-SECRET-KEY" not in (r["label"] or "").upper() for r in keyrows), keyrows)

print("== the owner's own key wins over a key the platform offers")
BRIDGE["bc"] = type("P", (), {})()        # the platform's own client


class PlatformBridge:
    _connected = True

    def __init__(self, answer):
        self.answer = answer

    async def request_full(self, op, params=None):
        if op == "backup.recovery_key":
            return {"ok": True, "data": dict(self.answer)}
        return {"ok": True, "data": {}}

    async def request(self, op, params=None, **_):
        return {}


BRIDGE["bc"] = PlatformBridge({})
pin = run(hb.pin_recovery_key(pool, 1))
check("the platform saying the account holds no key does not retire a key made on this Host",
      pin["pinned"] and pin["pinned"]["fingerprint"] == REC_FP and pin["offer"] is None, pin)
acct_ident, acct_rec = age_keys.new_pair()
ACCT_FP = backup_key.fingerprint(raw(acct_rec))
BRIDGE["bc"] = PlatformBridge({"recipient": acct_rec, "fingerprint": ACCT_FP, "label": "Host 1 recovery key",
                               "saved_at": "2026-10-19T10:00:00Z"})
pin = run(hb.pin_recovery_key(pool, 1))
check("a different key on the account page waits as an offer; the owner's own stays active",
      pin["pinned"]["fingerprint"] == REC_FP and pin["pinned"]["made_on_host"]
      and pin["offer"]["fingerprint"] == ACCT_FP, pin)
check("  and backups are still encrypted to the owner's own key only",
      [r["fingerprint"] for r in run(hb.recipients(pool, 1))] == [REC_FP])
run(hb.adopt_recovery_offer(pool, 1, ACCT_FP))
pin = run(hb.pin_recovery_key(pool, 1))
check("adopting the offer (the owner's act, with their code) replaces it",
      pin["pinned"]["fingerprint"] == ACCT_FP and pin["pinned"]["made_on_host"] is False, pin)
got = run(hb.make_own_recovery_key(pool, 1, rec_rec, REC_FP, "mine", replace=True))
pin = run(hb.pin_recovery_key(pool, 1))
check("making a key on the Host again, asked for, puts it back in place of the account page's",
      got["replaced"] == ACCT_FP and pin["pinned"]["fingerprint"] == REC_FP and pin["pinned"]["made_on_host"]
      and pin["offer"]["fingerprint"] == ACCT_FP, (got, pin))
moved = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
BRIDGE["bc"] = PlatformBridge({"owner_since": dt.datetime.now(dt.timezone.utc).isoformat()})
for r in keyrows:
    r["created_at"] = utc(2026, 9, 1)
pin = run(hb.pin_recovery_key(pool, 1))
check("a transfer still retires it: the previous owner's key seals nothing more",
      pin["pinned"] is None and run(hb.recipients(pool, 1)) == [], pin)
keyrows.clear()
pool.state.clear()

print("== no recovery key, and nothing to fetch one from: said once, not every minute")
# The backup loop asks for the key every minute while a backup is due and
# there is none (next_action), so each ask that cannot be answered was a log
# line: on a standalone Host, one a minute, forever.
import logging  # noqa: E402


class _Said(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        if "recovery key" in record.getMessage():
            self.records.append((record.levelno, record.getMessage()))


class FailingBridge(PlatformBridge):
    async def request_full(self, op, params=None):
        return {"ok": False, "error": "the platform did not answer in time"}


_said = _Said()
_level = hb.log.level
hb.log.addHandler(_said)
hb.log.setLevel(logging.DEBUG)
hb._recovery_fetch = None
said_by_state = []
for label, bc, connected in (("standalone", Recorder(), True),
                             ("the platform, not connected", PlatformBridge({}), False),
                             ("the platform, failing", FailingBridge({}), True),
                             ("the platform, answering", PlatformBridge({}), True),
                             ("the platform, answering still", PlatformBridge({}), True),
                             ("standalone again", Recorder(), True)):
    BRIDGE["bc"] = bc
    hb._bridge = (lambda: BRIDGE["bc"]) if connected else (lambda: None)
    _said.records.clear()
    for _minute in range(5):
        run(hb.pin_recovery_key(pool, 1))
    said_by_state.append((label, list(_said.records)))
hb._bridge = lambda: BRIDGE["bc"]
hb.log.removeHandler(_said)
hb.log.setLevel(_level)
said = dict(said_by_state)
check("a standalone Host asked five times says it once, at INFO, and why: there is no platform",
      len(said["standalone"]) == 1 and said["standalone"][0][0] == logging.INFO
      and "without the platform" in said["standalone"][0][1], said["standalone"])
check("a platform Host not connected says it once, at INFO",
      [lv for lv, _m in said["the platform, not connected"]] == [logging.INFO], said["the platform, not connected"])
check("a platform that is there and does not answer: once, as a warning",
      [lv for lv, _m in said["the platform, failing"]] == [logging.WARNING], said["the platform, failing"])
check("answered again: said once that it is fetched again, then nothing while it answers",
      [lv for lv, _m in said["the platform, answering"]] == [logging.INFO]
      and "again" in said["the platform, answering"][0][1] and said["the platform, answering still"] == [],
      (said["the platform, answering"], said["the platform, answering still"]))
check("and a change back is said again, once", len(said["standalone again"]) == 1, said["standalone again"])

print("== a backup to a folder on this machine")
BRIDGE["bc"] = Recorder()
run(hb.make_own_recovery_key(pool, 1, rec_rec, REC_FP, "Recovery key made on this Host"))
res = run(hb.run_once(pool, 1, "manual"))
check("the run ended ok, stored in the folder", res["state"] == "ok" and res["stored"] == "local", res)
stamp = res["stamp"]
data_file = hb.LOCAL_DIR / f"{stamp}.tar.zst.age"
side_file = hb.LOCAL_DIR / f"{stamp}.key.age"
check("one encrypted file and its key file, named by the Host's own clock",
      data_file.is_file() and side_file.is_file() and hb.STAMP_RE.match(stamp), list(hb.LOCAL_DIR.iterdir()))
got = members(rec_ident, data_file.read_bytes())
check("the recovery key opens it, and the owner's files are in it",
      got is not None and got.get("apps/app-state/user-files/notes.txt") == b"my notes\n")
check("the folder is not inside its own backup",
      got is not None and not any(".host-backups" in n for n in got), [n for n in (got or {}) if "host-backups" in n])
body = hb.open_sidecar(side_file.read_bytes(), rec_ident, stamp=stamp)
check("the key file opens with the recovery key and names this backup and its bytes",
      body["data_bytes"] == data_file.stat().st_size and body["identity"].startswith("AGE-SECRET-KEY-1"))
row = pool.runs[-1]
check("the run is recorded with where it went (the heartbeat and the restore list read it)",
      row["state"] == "ok" and row["stored"] == "local" and row["object_key"] == f"{stamp}.tar.zst.age"
      and row["sidecar_key"] == f"{stamp}.key.age", row)
check("nothing went over the bridge: no upload, no report, standalone",
      not any(c.startswith("backup.upload") or c == "backup.put_url" or c == "backup.report"
              for c in BRIDGE["bc"].calls), BRIDGE["bc"].calls)

print("== the folder keeps the newest")
pool.bp = {"backup": {"where": "local", "local_keep": 2}}
stamps = [stamp]
for _ in range(3):
    import time as _t
    _t.sleep(1.05)
    stamps.append(run(hb.run_once(pool, 1, "manual"))["stamp"])
kept = sorted(backup_stores.LocalStore(hb.LOCAL_DIR).pairs())
check("only the newest two are kept", kept == sorted(stamps)[-2:], (kept, stamps))
hb.restore_in_progress = lambda: False
from server import host_restore  # noqa: E402
host_restore.read_plan = lambda: {"state": "staged", "stamp": kept[0]}
_t.sleep(1.05)
newest = run(hb.run_once(pool, 1, "manual"))["stamp"]
kept2 = sorted(backup_stores.LocalStore(hb.LOCAL_DIR).pairs())
check("never the one a restore is reading", kept[0] in kept2 and newest in kept2, kept2)
host_restore.read_plan = lambda: None

print("== a Host told to use the network, with no network")
pool.bp = {"backup": {"where": "network"}}
now = dt.datetime.now(dt.timezone.utc)
act = run(hb.next_action(pool, 1, now + dt.timedelta(days=2), now - dt.timedelta(days=1)))
check("the loop does not run a backup it cannot send: it says so", act and act[0] == "no_destination", act)
res = run(hb.run_once(pool, 1, "manual"))
check("a run says where its backups were meant to go and how to fix it",
      res["state"] == "no_destination" and "Settings" in res["error"] and "network" in res["error"], res)
check("  and writes nothing anywhere else",
      sorted(backup_stores.LocalStore(hb.LOCAL_DIR).pairs()) == kept2)
pool.runs[-1]["started_at"] = now + dt.timedelta(days=2)
act = run(hb.next_action(pool, 1, now + dt.timedelta(days=2, minutes=1), now - dt.timedelta(days=1)))
check("once per scheduled time, not every minute", act is None, act)

print("== a member reports its runs; a standalone Host does not")
pool.bp = {"backup": {"where": "local", "local_keep": 3}}
BRIDGE["bc"] = Member()
_t.sleep(1.05)
res = run(hb.run_once(pool, 1, "manual"))
check("a member's backup to its own folder is reported to the network",
      res["state"] == "ok" and "backup.report" in BRIDGE["bc"].calls, BRIDGE["bc"].calls)
check("  and nothing else about it went to the network",
      not any(c.startswith("backup.upload") or c == "backup.put_url" for c in BRIDGE["bc"].calls))
BRIDGE["bc"] = Recorder()

print("== a member's backups go to the network's storage only when the network keeps them")


class Door(Member):
    """A member door. One without the backup ops answers every one of them as
    the member door answers any op it does not have (code 400, "Unknown
    operation"); `listed` is its answer to backup.list."""

    def __init__(self, listed):
        super().__init__()
        self.listed = listed

    async def request_full(self, op, params=None):
        self.calls.append(op)
        if op == "backup.list":
            return dict(self.listed)
        return {"ok": False, "error": f"Unknown operation: {op}", "code": 400}


OLD_DOOR = {"ok": False, "error": "Unknown operation: backup.list", "code": 400}
NO_STORE = {"ok": False, "error": "the member door has no platform credential for backups", "code": 503}
KEEPS = {"ok": True, "data": {"objects": []}}
NO_ANSWER = {"ok": False, "error": "not connected", "error_kind": "infra"}


def fresh(door):
    hb._network_answer.update(client=None, at=None, offers=None)
    BRIDGE["bc"] = door
    return door


door = fresh(Door(OLD_DOOR))
check("a door without the backup ops: the network's storage does not take this Host's backups",
      run(hb.network_backups()) is False and door.calls == ["backup.list"], door.calls)
check("  asked once, not on every look", run(hb.network_backups()) is False and door.calls == ["backup.list"],
      door.calls)
fresh(Door(NO_STORE))
check("a door with no storage behind them (503): it does not either", run(hb.network_backups()) is False)
door = fresh(Door(KEEPS))
check("a door that lists this Host's backups: it does", run(hb.network_backups()) is True, door.calls)
door.listed = NO_ANSWER
hb._network_answer["at"] -= hb.NETWORK_ASK_S + 1
check("no answer from the door later: the last answer stands",
      run(hb.network_backups()) is True and door.calls == ["backup.list", "backup.list"], door.calls)
door.listed = {"ok": False, "error": "platform storage unreachable: ClientError", "code": 502}
hb._network_answer["at"] -= hb.NETWORK_ASK_S + 1
check("  and so does an error that says nothing about what the door serves", run(hb.network_backups()) is True)
door.listed = OLD_DOOR
hb._network_answer["at"] -= hb.NETWORK_ASK_S + 1
check("asked again once the answer is old: a door that stopped keeping them is seen",
      run(hb.network_backups()) is False)
door = fresh(Door(NO_ANSWER))
check("a door that has not answered yet: not known, and the default is the folder",
      run(hb.network_backups()) is None and hb.destination_of({}, "member", None) == "local")


class SilentDoor(Door):
    async def request_full(self, op, params=None):
        self.calls.append(op)
        await asyncio.sleep(30)


fresh(SilentDoor(KEEPS))
_ask, hb.NETWORK_ASK_TIMEOUT_S = hb.NETWORK_ASK_TIMEOUT_S, 0.2
_t0 = _t.monotonic()
check("a door that is connected and silent is not waited on: nothing learned, within the ask's own limit",
      run(hb.network_backups()) is None and _t.monotonic() - _t0 < 5)


class SlowDoor(Door):
    """A door that answers its heartbeat while this question waits on storage
    that hangs (so it stays connected), until `slow` is turned off."""
    slow = True

    async def request_full(self, op, params=None):
        if self.slow:
            self.calls.append(op)
            await asyncio.sleep(30)
        return await super().request_full(op, params)


RETRY_S = getattr(hb, "NETWORK_RETRY_S", 60.0)
door = fresh(SlowDoor(KEEPS))
_t0 = _t.monotonic()
check("a door that stays silent is not asked again on the next look: one wait, not one per look",
      run(hb.network_backups()) is None and run(hb.network_backups()) is None
      and door.calls == ["backup.list"] and _t.monotonic() - _t0 < 0.39, door.calls)


async def _looks(n):
    return await asyncio.gather(*(hb.network_backups() for _ in range(n)))


door = fresh(SlowDoor(KEEPS))
_t0 = _t.monotonic()
check("  nor by five looks at once (Settings opens several): they share the one ask",
      run(_looks(5)) == [None] * 5 and door.calls == ["backup.list"] and _t.monotonic() - _t0 < 1,
      door.calls)
door.slow = False
check("  and within a minute of it the door is not asked again", run(hb.network_backups()) is None
      and door.calls == ["backup.list"], door.calls)
hb._network_answer["at"] -= RETRY_S + 1
check("  after it, asked again, and its answer is heard", run(hb.network_backups()) is True
      and door.calls == ["backup.list", "backup.list"], door.calls)
hb._network_answer["at"] -= RETRY_S + 1
check("an answer still stands for the full ten minutes, not one",
      run(hb.network_backups()) is True and door.calls == ["backup.list", "backup.list"], door.calls)
hb.NETWORK_ASK_TIMEOUT_S = _ask
BRIDGE["bc"] = Door(KEEPS)
check("another door (the Host joined again): asked again at once", run(hb.network_backups()) is True)
check("the platform and a standalone Host are never asked",
      run(hb.network_backups("platform")) is True and run(hb.network_backups("standalone")) is False
      and BRIDGE["bc"].calls == ["backup.list"], BRIDGE["bc"].calls)

pool.bp = {}
door = fresh(Door(OLD_DOOR))
before = sorted(backup_stores.LocalStore(hb.LOCAL_DIR).pairs())
now = dt.datetime.now(dt.timezone.utc)
act = run(hb.next_action(pool, 1, now + dt.timedelta(days=3), now - dt.timedelta(days=1)))
check("a member that chose nothing, on a network that keeps no backups, still backs up",
      act and act[0] == "run", act)
_t.sleep(1.05)
res = run(hb.run_once(pool, 1, "manual"))
check("  to the folder on this machine",
      res["state"] == "ok" and res["stored"] == "local"
      and len(backup_stores.LocalStore(hb.LOCAL_DIR).pairs()) == len(before) + 1, res)
check("  and nothing was sent to the network's storage",
      not any(c.startswith("backup.upload") or c == "backup.put_url" for c in door.calls), door.calls)
door = fresh(Door(KEEPS))
check("the same member, once its network keeps backups, defaults to the network's storage",
      run(hb.destination_problem(pool, 1)) is None
      and hb.destination_of(pool.bp, "member", run(hb.network_backups())) == "network")

pool.bp = {"backup": {"where": "network"}}
door = fresh(Door(OLD_DOOR))
act = run(hb.next_action(pool, 1, now + dt.timedelta(days=4), now - dt.timedelta(days=1)))
check("a member that chose the network's storage, which keeps no backups: the loop says so",
      act and act[0] == "no_destination", act)
res = run(hb.run_once(pool, 1, "manual"))
check("  and a run says the network does not keep backups yet, and what to choose instead",
      res["state"] == "no_destination" and "does not keep backups yet" in res["error"]
      and "Settings, Backup" in res["error"], res)
check("  writing nothing, here or on the network",
      not any(c.startswith("backup.upload") or c == "backup.put_url" for c in door.calls), door.calls)
check("  and Settings is told why (destination_problem)",
      run(hb.destination_problem(pool, 1)) == hb.NO_NETWORK_STORAGE_TEXT)
door = fresh(Door(NO_ANSWER))
check("a door that has not answered is not taken for one that keeps no backups",
      run(hb.destination_problem(pool, 1)) is None)


async def _one_tick(acts):
    """The backup loop, for as many minutes as `acts` has answers from
    next_action (each minute a millisecond); returns the rows it wrote."""
    saved = hb.LOOP_SECONDS, hb.next_action
    left = list(acts)

    async def next_action(*_a):
        if not left:
            raise asyncio.CancelledError
        return left.pop(0)
    hb.LOOP_SECONDS, hb.next_action = 0.001, next_action
    n = len(pool.runs)
    try:
        await hb.run_host_backup_loop(pool, 1)
    except asyncio.CancelledError:
        pass
    finally:
        hb.LOOP_SECONDS, hb.next_action = saved
    return pool.runs[n:]


fresh(Door(KEEPS))
wrote = run(_one_tick([("no_destination", "scheduled")]))
check("the loop found the network's storage refused, and the network answered before it wrote the row: "
      "no row says this member is not on the network",
      not any(r.get("error") == hb.NO_DESTINATION_TEXT for r in wrote), wrote)
fresh(Door(OLD_DOOR))
wrote = run(_one_tick([("no_destination", "scheduled")]))
check("  while the refusal holds, the row gives the member's own reason",
      [r.get("error") for r in wrote] == [hb.NO_NETWORK_STORAGE_TEXT], wrote)
pool.bp = {"backup": {"where": "local", "local_keep": 3}}
hb._network_answer.update(client=None, at=None, offers=None)
BRIDGE["bc"] = Recorder()

print("== the owner's own bucket")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
pool.bp = {"backup": {"where": "s3", "target": {"kind": "s3", "address": "my-backups",
                                                "cred_name": "backup-storage", "prefix": "host-a"}}}


class FakeS3:
    def __init__(self):
        self.objects, self.uploads = {}, {}

    def create_multipart_upload(self, Bucket, Key):
        self.uploads[Key] = []
        return {"UploadId": Key}

    def upload_part(self, Bucket, Key, PartNumber, UploadId, Body, ContentMD5):
        self.uploads[Key].append(bytes(Body))
        import hashlib
        return {"ETag": '"' + hashlib.md5(bytes(Body)).hexdigest() + '"'}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        self.objects[Key] = b"".join(self.uploads.pop(Key))

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.uploads.pop(Key, None)

    def put_object(self, Bucket, Key, Body, ContentMD5):
        self.objects[Key] = bytes(Body)

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.objects[Key])}


fake = FakeS3()
creds_asked = []


async def _creds(pool_, eid, name):
    creds_asked.append(name)
    return {"access_key_id": "AKEXAMPLE", "secret_access_key": "example-secret"}
from server import backup_ship  # noqa: E402
backup_ship._s3_credential = _creds
hb._s3_client_for = lambda creds, target: fake
_t.sleep(1.05)
res = run(hb.run_once(pool, 1, "manual"))
check("the run ended ok, stored in the owner's bucket", res["state"] == "ok" and res["stored"] == "s3", res)
key = f"host-a/{res['stamp']}.tar.zst.age"
check("the file and its key file are in the bucket under the folder chosen",
      key in fake.objects and f"host-a/{res['stamp']}.key.age" in fake.objects, list(fake.objects))
check("the bucket's key was read from the vault by the name the settings hold",
      creds_asked == ["backup-storage"], creds_asked)
check("the recovery key opens what landed in the bucket", members(rec_ident, fake.objects[key]) is not None)
pool.bp = {"backup": {"where": "s3"}}
res = run(hb.run_once(pool, 1, "manual"))
check("a bucket chosen with none saved: the run fails saying so",
      res["state"] == "failed" and "bucket" in (res["error"] or ""), res)

print("== an export: one whole-Host file to take away")
pool.bp = {"backup": {"where": "local"}}
before = len(pool.runs)
_t.sleep(1.05)
got = run(hb.export_to_file(pool, 1))
ex = backup_stores.LocalStore(hb.LOCAL_DIR, sub="exports").pairs()
check("an export is a backup and its key file in exports/", got["state"] == "ok" and list(ex) == [got["stamp"]], (got, ex))
check("  that the recovery key opens", members(rec_ident, (hb.LOCAL_DIR / ex[got["stamp"]]["key"]).read_bytes()) is not None)
check("  recorded apart from the backups (no run row: the heartbeat counts backups, not exports)",
      len(pool.runs) == before and json.loads(pool.state["local_export"])["stamp"] == got["stamp"], pool.state)
_t.sleep(1.05)
got2 = run(hb.export_to_file(pool, 1))
ex = backup_stores.LocalStore(hb.LOCAL_DIR, sub="exports").pairs()
check("a new export replaces the last one", list(ex) == [got2["stamp"]], ex)
keyrows.clear()
try:
    run(hb.export_to_file(pool, 1))
    check("no recovery key: no export", False)
except hb.BackupError as e:
    check("no recovery key: no export", "recovery key" in str(e), str(e))

print("== a Host whose backups paused for want of a recovery key tells its owner, once per pause")
import mastercode.context_store as _cs  # noqa: E402
written = []


class _Context:
    """The Host's own message list, read into its next turn (runtime/session.py)."""

    def __init__(self, pool_, eid):
        self.eid = eid

    async def append_to_list(self, path, item, max_items=100):
        written.append((path, item))
_real_ctx = _cs.ContextStore
_cs.ContextStore = _Context
try:
    p2 = Pool()
    check("a Host that never backed up is not told it is paused (its verdict says it has no key yet)",
          run(hb.tell_host_paused(p2, 1)) is False and written == [])
    p2.runs = [{"id": 1, "state": "ok", "reason": "scheduled"}, {"id": 2, "state": "ok", "reason": "restore"}]
    told = run(hb.tell_host_paused(p2, 1, why=hb.RESTORED_WITHOUT_KEY))
    msg = written[-1][1] if written else {}
    check("one that has backed up is told: a message in its own list, for its next turn",
          told is True and written[-1][0] == "inbox/messages.json" and msg.get("read") is False
          and msg.get("source") == "backups", written)
    check("  saying backups are paused, why, and what the owner does (the Backups app, their key)",
          "paused" in msg.get("content", "") and hb.RESTORED_WITHOUT_KEY in msg.get("content", "")
          and "Backups app" in msg.get("content", "") and "Make a recovery key" in msg.get("content", ""), msg)
    check("  once: not again on the next scheduled time of the same pause",
          run(hb.tell_host_paused(p2, 1)) is False and len(written) == 1, written)
    p2.runs.append({"id": 3, "state": "ok", "reason": "scheduled"})
    check("  and again after the next pause, once a backup has worked in between",
          run(hb.tell_host_paused(p2, 1)) is True and len(written) == 2, written)
finally:
    _cs.ContextStore = _real_ctx

shutil.rmtree(tmp, ignore_errors=True)
print()
print("FAILED: " + ", ".join(FAILED) if FAILED else "all ok")
sys.exit(1 if FAILED else 0)
