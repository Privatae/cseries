#!/usr/bin/env python3
"""Fleet bastion update — one decision moves every out-of-date machine.

One user ran 21 bastions and the only way to update them was a per-card button
behind a confirm and an alert: 42 clicks for one intention. This pins the
behaviour that replaced it, because the failure modes are all quiet ones — a
machine silently skipped, someone else's machine updated, or an "ok" that
directed nothing.

Runs as pytest AND as a plain script in the container-free gate lane
(APP_ROOT=<repo> python3 scripts/test_bastion_update_all.py). No DB, no
network: the pool is a recording stub.
"""
from __future__ import annotations

import asyncio
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from server.network import nac_commit  # noqa: E402

EID = 3
LATEST = "0.17.0"

# Three machines behind, one already current, one that has never checked in.
FLEET = [
    {"token_id": 11, "hostname": "server-a", "version": "0.13.13", "update_to_version": None},
    {"token_id": 12, "hostname": "server-b", "version": "0.13.13", "update_to_version": None},
    {"token_id": 13, "hostname": "edge-a", "version": "0.16.0", "update_to_version": None},
    {"token_id": 14, "hostname": "server-c", "version": LATEST, "update_to_version": None},
    {"token_id": 15, "hostname": "", "version": "", "update_to_version": None},
]


class FakePool:
    """Records every write. `rows` is what the fleet SELECT returns.

    `no_row` is the set of token_ids that have NO heartbeat row, so the UPDATE
    reports "UPDATE 0" the way asyncpg does — the enrolled-but-never-connected
    case measured on the live fleet.
    """

    def __init__(self, rows, no_row=()):
        self.rows = rows
        self.no_row = set(no_row)
        self.updates = []          # (token_id, version)
        self.clears = []           # (token_id, version) — stale-ask clears
        self.other_sql = []

    async def fetch(self, sql, *args):
        assert "operator_kind = 'bastion'" in sql, "the fleet read must be scoped to bastions"
        assert "entity_id = $1" in sql, "the fleet read must be scoped to ONE entity"
        assert "revoked_at IS NULL" in sql, "a revoked bastion is not part of the fleet"
        assert args[0] == EID
        return [dict(r) for r in self.rows]

    async def execute(self, sql, *args):
        if "SET update_to_version = NULL" in sql:
            self.clears.append((args[0], args[1]))
            return "UPDATE 0"
        if "SET update_to_version" in sql:
            self.updates.append((args[0], args[1]))
            return "UPDATE 0" if args[0] in self.no_row else "UPDATE 1"
        self.other_sql.append(sql)
        return "UPDATE 1"

    async def fetchval(self, *a, **k):
        return 1


def run(coro):
    return asyncio.run(coro)


def test_a_machine_that_never_checked_in_is_reported_not_counted_as_directed():
    # Measured on the live fleet: the directive lives on the heartbeat
    # row, and an enrolled bastion that has never spoken has no row — so the write
    # touches nothing. Counting it as "directed" would be the reply lying, which is
    # the exact failure this feature exists to remove.
    pool = FakePool(FLEET, no_row={15})
    res = _call(pool)
    assert [d["token_id"] for d in res["directed"]] == [11, 12, 13]
    assert res["unreachable_count"] == 1
    assert res["unreachable"][0]["token_id"] == 15
    assert "never checked in" in res["unreachable"][0]["reason"]
    assert res["directed_count"] == 3


def _call(pool, version=LATEST, token_ids=None):
    # update_bastions used to call topology.ensure_table() first and this helper
    # stubbed it out so the recording pool never saw the DDL. That call is
    # gone — the schema is declared — so there is nothing left to patch.
    return run(nac_commit.update_bastions(pool, EID, version, token_ids))


def test_every_out_of_date_machine_is_directed_and_current_ones_are_named():
    pool = FakePool(FLEET)
    res = _call(pool)
    # 11, 12, 13 are behind; 15 has never checked in and gets the directive too —
    # it costs nothing and is exactly what a machine coming online should pick up.
    assert sorted(t for t, _ in pool.updates) == [11, 12, 13, 15]
    assert all(v == LATEST for _, v in pool.updates)
    assert res["directed_count"] == 4
    # The one already on the version is REPORTED, not silently counted as done.
    assert res["skipped_count"] == 1
    assert res["skipped"][0]["hostname"] == "server-c"
    assert LATEST in res["skipped"][0]["reason"]


def test_a_second_run_directs_nothing_new_once_the_fleet_reports_the_version():
    landed = [{**r, "version": LATEST} for r in FLEET]
    pool = FakePool(landed)
    res = _call(pool)
    assert pool.updates == [], "a fleet already on the version must be written to zero times"
    assert res["directed_count"] == 0 and res["skipped_count"] == len(FLEET)


def test_token_ids_narrows_and_a_foreign_id_is_refused_before_anything_is_written():
    pool = FakePool(FLEET)
    res = _call(pool, token_ids=[11, 13])
    assert sorted(t for t, _ in pool.updates) == [11, 13]
    assert res["directed_count"] == 2

    # A machine this entity does not own must not be updatable by naming its id.
    pool2 = FakePool(FLEET)
    try:
        _call(pool2, token_ids=[11, 999])
    except nac_commit.CommitError as e:
        assert e.status == 404 and "999" in str(e.detail)
    else:
        raise AssertionError("a foreign token_id must be refused")
    # The test's own name is the assertion: refused BEFORE anything was written.
    # Without this line the check passed while machine 11 had already been directed.
    assert pool2.updates == [], "a refusal must not leave some machines directed"


def test_empty_token_ids_is_refused_rather_than_read_as_everything():
    # [] and "omitted" mean opposite things, and reading [] as "all" would update
    # the whole fleet when the caller meant to update nothing.
    pool = FakePool(FLEET)
    try:
        _call(pool, token_ids=[])
    except nac_commit.CommitError as e:
        assert e.status == 400
    else:
        raise AssertionError("an empty token_ids must be refused")
    assert pool.updates == []


def test_a_missing_version_is_refused():
    pool = FakePool(FLEET)
    for bad in ("", "   ", None):
        try:
            _call(pool, version=bad)
        except nac_commit.CommitError as e:
            assert e.status == 400
        else:
            raise AssertionError(f"version {bad!r} must be refused")
    assert pool.updates == []


def test_the_app_asks_once_and_never_alerts():
    """The UI half of the same decision, checked as text because it is a click count.

    A regression here is someone re-adding a per-machine popup, which is the exact
    thing this work removed.
    """
    app = open(os.path.join(_ROOT, "system-apps", "network", "app.js"), encoding="utf-8").read()
    html = open(os.path.join(_ROOT, "system-apps", "network", "index.html"), encoding="utf-8").read()
    assert 'id="gw-update-all"' in html, "the fleet button must exist in the Bastions toolbar"
    assert "async function updateAllBastions" in app
    # Exactly one confirmation on the fleet path, and it names the machines.
    body = app[app.index("async function updateAllBastions"):]
    body = body[:body.index("\n    }")]
    assert body.count("confirm(") == 1, "the fleet update asks once, not per machine"
    assert "names" in body, "the confirmation must list which machines it covers"
    assert "alert(" not in body, "no second popup on the fleet path"
    # And the single-machine path lost its trailing alert too.
    single = app[app.index("async function updateBastion(g)"):]
    single = single[:single.index("\n    }")]
    assert "alert(" not in single, "the per-machine update must not alert after acting"



# ── ONE machine gets the same two rules as the fleet ──
# The single-machine route answered "ok" to a directive at the box's own version,
# and then nothing visible happened: the box ignores it (bastion
# client-go/update.go: `target == clientVersion` returns) and the next heartbeat
# clears it server-side. Nothing lingered — the defect was the REPLY. The
# end-to-end test's bastion self-update check caught it.

class OnePool:
    """One bastion: owned, with `version` on its heartbeat row (None = no row)."""

    def __init__(self, version, owned=True):
        self.version = version
        self.owned = owned
        self.updates = []
        self.clears = []

    async def fetchval(self, sql, *args):
        if "mcp_client_tokens" in sql:
            return 1 if self.owned else None
        if "gateway_version" in sql:
            return self.version
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql, *args):
        if "SET update_to_version = NULL" in sql:
            assert "IS DISTINCT FROM $2" in sql, "a clear must never drop the ask being made"
            self.clears.append((args[0], args[1]))
            return "UPDATE 1"
        assert "SET update_to_version" in sql, sql
        self.updates.append((args[0], args[1]))
        return "UPDATE 0" if self.version is None else "UPDATE 1"


def test_one_machine_already_on_the_version_is_skipped_and_named():
    pool = OnePool(LATEST)
    out = run(nac_commit.update_bastion(pool, EID, 14, LATEST))
    assert out.get("skipped") is True and out.get("reason") == "already on " + LATEST, out
    assert out.get("update_to_version") is None, out
    assert not pool.updates, "a no-op directive must not be parked on the heartbeat row"


def test_one_out_of_date_machine_is_directed():
    pool = OnePool("0.13.13")
    out = run(nac_commit.update_bastion(pool, EID, 11, LATEST))
    assert out == {"token_id": 11, "update_to_version": LATEST, "ok": True}, out
    assert pool.updates == [(11, LATEST)]


def test_one_machine_that_never_checked_in_is_refused_not_answered_ok():
    pool = OnePool(None)
    try:
        run(nac_commit.update_bastion(pool, EID, 15, LATEST))
    except nac_commit.CommitError as e:
        assert e.status == 409 and "never checked in" in e.detail, (e.status, e.detail)
    else:
        raise AssertionError("an UPDATE that wrote nothing was answered ok")


def test_one_machine_that_is_not_yours_is_refused_before_anything():
    pool = OnePool("0.13.13", owned=False)
    try:
        run(nac_commit.update_bastion(pool, EID, 99, LATEST))
    except nac_commit.CommitError as e:
        assert e.status == 404, e.status
    else:
        raise AssertionError("a foreign bastion was directed")
    assert not pool.updates


def test_an_explicit_ask_for_the_current_version_clears_an_older_ask():
    """A leftover directive to some other version is never acted on (the box skips
    a target the manifest does not publish) and is resent every heartbeat. Asking
    for the version the box already runs used to overwrite it; the skip must too."""
    pool = OnePool(LATEST)
    run(nac_commit.update_bastion(pool, EID, 14, LATEST))
    assert pool.clears == [(14, LATEST)], pool.clears
    fleet = FakePool(FLEET)
    run(nac_commit.update_bastions(fleet, EID, LATEST))
    assert (14, LATEST) in fleet.clears, fleet.clears
    assert all(tid == 14 for tid, _ in fleet.clears), "only the skipped machine is cleared"


def test_a_leading_v_is_the_same_version():
    """"v0.31.2" from the host tool was parked as-is, refused by the box on every
    heartbeat (the manifest says "0.31.2") and never cleared."""
    pool = OnePool(LATEST)
    out = run(nac_commit.update_bastion(pool, EID, 14, "v" + LATEST))
    assert out.get("skipped") is True and out["reason"] == "already on " + LATEST, out
    pool = OnePool("0.13.13")
    out = run(nac_commit.update_bastion(pool, EID, 11, "V" + LATEST))
    assert out["update_to_version"] == LATEST and pool.updates == [(11, LATEST)], out
    fleet = FakePool(FLEET)
    out = run(nac_commit.update_bastions(fleet, EID, "v" + LATEST))
    assert out["version"] == LATEST and all(v == LATEST for _, v in fleet.updates), out
    assert nac_commit._norm_bastion_version("vnext") == "vnext", "only v+digit is a prefix"

if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name} -- {type(e).__name__}: {e}")
    print(f"\nFAILURES: {fails} / {sum(1 for n in globals() if n.startswith('test_'))}")
    sys.exit(1 if fails else 0)
