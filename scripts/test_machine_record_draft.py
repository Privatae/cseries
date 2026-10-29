#!/usr/bin/env python3
"""The machine's own record is DRAFTED from the beat, never granted.

Runs as pytest (`pytest scripts/test_machine_record_draft.py`) AND as a plain
script (`APP_ROOT=<repo> python3 scripts/…`). No DB, no network: the beat is
driven through the real topology.upsert_heartbeat against a recording pool, the
same shape scripts/test_oversight_ledger.py uses.

WHAT THIS FILE IS DEFENDING. A bastion reports `machine_record_root` on every
beat — the path of the directory holding its findings journal and its signed
accounting. The host files a DRAFT dcn_stores row there so the user can approve
it with the same one code as any folder they added. Two properties matter and
both are the kind that regress silently:

  * It is a DRAFT. "Creating a store is ALWAYS a widen" (nac_propose) is
    structural, and the plan's "granted automatically at pairing" would have
    made it policy. The row must be status='draft' with purpose='machine_record'
    and the machine-record mask, and NOTHING here may approve it.
  * It is IDEMPOTENT PER BEAT. The beat fires every 60 seconds per machine.
    One existence read, and an INSERT only the first time — a second row, or a
    revive of a row the user REVOKED, would be the host re-asking for something
    the user already answered.

And one hazard: the root is the box's own claim about its own disk. It is
narrowed like every reported string — absolute, no `..`, bounded — and a bad
shape makes NO draft rather than failing the beat, because the beat is
liveness.
"""
import asyncio
import os
import sys

_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from server.network import nac_propose as np_  # noqa: E402
from server.network import topology  # noqa: E402


class Pool:
    """Enough asyncpg surface for upsert_heartbeat + the draft path, recording
    every statement. `existing` is what the machine-store existence read
    returns (None = no row yet)."""

    def __init__(self, existing=None):
        self.sql: list = []
        self.existing = existing

    async def execute(self, q, *a):
        self.sql.append((q, a))
        return "UPDATE 0"

    async def fetchval(self, q, *a):
        self.sql.append((q, a))
        if "FROM mcp_client_tokens" in q and "operator_kind = 'bastion'" in q:
            return 1
        if "INSERT INTO dcn_stores" in q:
            return 4242
        return None

    async def fetchrow(self, q, *a):
        self.sql.append((q, a))
        if "SELECT detect, alerts FROM network_bastion_heartbeat" in q:
            return None
        if "INSERT INTO network_bastion_heartbeat" in q:
            return {"token_id": 7, "last_seen_at": None, "detect": None, "alerts": None}
        if "FROM dcn_stores" in q and "class='machine'" in q:
            return self.existing
        return None

    async def fetch(self, q, *a):
        self.sql.append((q, a))
        return []

    def acquire(self):
        pool = self

        class _Cx:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *exc):
                return False
        return _Cx()

    def store_inserts(self):
        return [(q, a) for q, a in self.sql if "INSERT INTO dcn_stores" in q]

    def store_lookups(self):
        return [(q, a) for q, a in self.sql if "FROM dcn_stores" in q and "class='machine'" in q]


def _beat(**extra):
    return {"token_id": 7, "hostname": "box-1", "platform": "linux", **extra}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


ROOT = "/var/lib/privatae-bastion/store"


def test_a_beat_with_a_root_drafts_the_store_once():
    pool = Pool(existing=None)
    assert _run(topology.upsert_heartbeat(pool, 9003, _beat(machine_record_root=ROOT))) is not None
    ins = pool.store_inserts()
    assert len(ins) == 1, f"{len(ins)} store inserts for one beat"
    q, a = ins[0]
    assert "'draft'" in q, "the row must be a DRAFT, never approved here"
    assert "purpose" in q and "machine_record" in a, (q, a)
    assert a[0] == "box-1" and a[1] == ROOT, a
    # The mask is the machine-record mask, and the sweep is manual: the journal
    # is not documents and must not be indexed as documents.
    assert '"read"' in a[2] and '"search"' in a[2] and '"index"' not in a[2], a[2]
    assert "manual" in a, a


def test_an_existing_row_makes_no_second_draft():
    """Idempotent per beat: the existence read answers, no INSERT follows."""
    pool = Pool(existing={"id": 99, "status": "draft"})
    _run(topology.upsert_heartbeat(pool, 9003, _beat(machine_record_root=ROOT)))
    assert pool.store_lookups(), "the draft path never checked for an existing row"
    assert pool.store_inserts() == [], "a second draft was filed for a store that exists"


def test_a_revoked_row_is_not_revived_by_the_beat():
    """A REVOKE is the user's answer. propose_dcn_store revives a revoked row on
    re-propose (a decision for a user re-adding a folder) — but the
    beat is not the user, and the beat re-asking every sixty seconds for a
    store the user revoked is the host nagging. The existence read sees the
    revoked row and stops."""
    pool = Pool(existing={"id": 99, "status": "revoked"})
    _run(topology.upsert_heartbeat(pool, 9003, _beat(machine_record_root=ROOT)))
    assert pool.store_inserts() == []
    assert not any("UPDATE dcn_stores SET status='draft'" in q for q, _ in pool.sql), (
        "the beat revived a store the user revoked")


def test_a_beat_without_a_root_drafts_nothing():
    """Older bastions send no key. Absent is absent; no draft, no lookup."""
    pool = Pool(existing=None)
    _run(topology.upsert_heartbeat(pool, 9003, _beat()))
    assert pool.store_lookups() == [] and pool.store_inserts() == []


def test_a_bad_root_shape_drafts_nothing_and_does_not_fail_the_beat():
    for bad in ("relative/store", "/root/../etc", "", "x" * 600, "../../store"):
        pool = Pool(existing=None)
        row = _run(topology.upsert_heartbeat(pool, 9003, _beat(machine_record_root=bad)))
        assert row is not None, f"the beat FAILED on a bad root {bad!r} — the beat is liveness"
        assert pool.store_inserts() == [], f"a draft was filed at {bad!r}"


def test_a_windows_root_is_accepted():
    pool = Pool(existing=None)
    _run(topology.upsert_heartbeat(pool, 9003, _beat(machine_record_root=r"C:\Users\c\AppData\Roaming\privatae-bastion\store")))
    assert len(pool.store_inserts()) == 1


def test_purpose_is_a_closed_vocabulary():
    """propose_dcn_store refuses any purpose but the one, and refuses it on a
    cabinet — the CHECK constraint in v162 says the same, and a caller must not
    learn it from a 500."""
    import contextlib
    pool = Pool(existing=None)
    for kwargs in ({"store_class": "machine", "server": "b", "root": "/r", "purpose": "documents"},
                   {"store_class": "cabinet", "bucket": "b", "purpose": "machine_record"}):
        with contextlib.suppress(ValueError):
            _run(np_.propose_dcn_store(pool, 9003, **kwargs))
            raise AssertionError(f"accepted {kwargs}")


def test_the_draft_path_never_approves():
    """Nothing in the draft path may touch approved_at, signed_terms or status
    other than the INSERT's own 'draft'."""
    pool = Pool(existing=None)
    _run(topology.upsert_heartbeat(pool, 9003, _beat(machine_record_root=ROOT)))
    for q, _ in pool.sql:
        if "dcn_stores" in q and "INSERT" not in q and "SELECT" not in q:
            raise AssertionError(f"the draft path wrote to dcn_stores outside the INSERT: {q[:80]}")
        assert "approved_at = now()" not in q and "signed_terms" not in q.replace("SELECT", ""), q[:80]


def test_a_folder_starts_from_excludes_that_leave_out_the_agent_store():
    """A folder the person adds starts from DCN_DEFAULT_EXCLUDES, and those
    leave out the bastion agent's own folder (.agent), where it keeps its store
    in the clear: a machine on a bastion older than 0.58.1 serves it otherwise.
    Matched as the box matches (client-go dcn.go dcnExcluded): each pattern
    against every path segment."""
    import fnmatch
    import json as _json
    pool = Pool(existing=None)
    _run(np_.propose_dcn_store(pool, 9003, store_class="machine", server="box-1", root="/home/p/notes"))
    ins = pool.store_inserts()
    assert len(ins) == 1, ins
    excludes = _json.loads(ins[0][1][3])
    assert ".agent" in excludes, excludes

    def excluded(rel):
        return any(fnmatch.fnmatchcase(seg, pat) for pat in excludes for seg in rel.split("/"))
    assert excluded(".agent/store.json") and excluded("projects/x/.agent/plans/1.json")
    assert not excluded("projects/agent/notes.md") and not excluded("projects/.agents/notes.md")


def _tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    import re
    print("machine record drafted from the beat")
    tests = _tests()
    declared = len(re.findall(r"^def (test_\w+)", open(__file__, encoding="utf-8").read(), re.M))
    if declared != len(tests):
        print(f"FAIL  {declared} tests defined, {len(tests)} collected — move the runner to the end")
        return 1
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok    {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
