"""The bastion control-plane lane must still hold a connection after it goes idle.

The 502 that broke ~40% of one day's fleet pairing was traced to this
pod's postgres running max_connections=16 while the brain pool alone could hold 10 of
them at the time (it is 6 now — the recall lane took 4 out of it, not on top of it), so
enrolment competed with every app in the host's Workbench for what was left. The fix
carved out a separate two-connection lane with min_size=1 so the connection
would EXIST before it was needed.

It did not, for most of the day. The lane was created with asyncpg's default
max_inactive_connection_lifetime=300, and asyncpg honours min_size only inside
Pool._initialize: once PoolConnectionHolder._deactivate_inactive_connection terminates
an idle connection, nothing re-establishes it. Enrolment is bursty and rare by nature,
so five quiet minutes is its NORMAL state — which means the reservation was gone at
almost exactly the moments it was supposed to cover, and the next redeem opened a fresh
connection against the same exhausted server the lane exists to escape.

Two properties, because the fix rests on both and they fail independently:

  1. OURS  — the lane asks for no pruning, and reserves exactly one of the sixteen.
  2. THEIRS — asyncpg still reads 0 as "no timer". That is a dependency contract, not
             our code, so an upgrade could change it silently and re-open the bug with
             our source untouched. Pin it here, where the failure names itself.

Run: pytest tests/test_bastion_control_pool_reserved.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "brain"))

from server import brain_service  # noqa: E402


def test_control_lane_is_never_pruned():
    """0 = asyncpg's "disable the inactivity mechanism"; the asyncpg tests below pin
    that reading of the value."""
    assert brain_service.CONTROL_POOL_KWARGS["max_inactive_connection_lifetime"] == 0


def test_control_lane_reserves_exactly_one_connection():
    """min_size=1 is what makes it a RESERVATION rather than just a size cap, and
    max_size=2 is what stops the lane becoming a second competitor for the 16."""
    assert brain_service.CONTROL_POOL_KWARGS["min_size"] == 1
    assert brain_service.CONTROL_POOL_KWARGS["max_size"] == 2


def test_every_pool_together_stays_inside_max_connections():
    """entrypoint-postgres.sh writes max_connections. Every pool this module opens
    must together leave real room for the harness sidecar, diagnostics and the
    Workbench — if a future change eats that, it should surface here and not as a 502.

    max_connections is READ from the entrypoint rather than hardcoded: the property is
    that the numbers agree, and a test carrying its own copy of 16 would keep
    passing while they diverged.

    The pool LIST is derived from the module for the same reason. This assertion used
    to name the brain pool and the control lane one by one, and when the recall lane
    was added it would have gone on passing while the real total climbed — the exact
    drift the paragraph above claims it prevents. Add a *_POOL_KWARGS constant and it
    is counted here whether or not anyone remembered to come back.
    """
    conf = (REPO / "entrypoint-postgres.sh").read_text()
    line = next(ln for ln in conf.splitlines() if ln.strip().startswith("max_connections"))
    max_connections = int(line.split("=", 1)[1].strip())
    pools = {n: v for n, v in vars(brain_service).items()
             if n.endswith("_POOL_KWARGS") and isinstance(v, dict)}
    assert len(pools) >= 3, f"expected at least brain/control/recall, found {sorted(pools)}"
    reserved = sum(v["max_size"] for v in pools.values())
    assert reserved < max_connections, (
        f"{len(pools)} pools ({', '.join(sorted(pools))}) reserve {reserved} of "
        f"{max_connections} connections, leaving {max_connections - reserved} for the "
        f"harness sidecar, diagnostics and every app in the Workbench combined"
    )


def test_the_recall_lane_is_a_different_role_not_just_a_different_pool():
    """The whole point of the recall lane is the ROLE on the far end of it. A second
    pool on `entity` would be pure cost: same superuser, same rolbypassrls, same rows,
    one more connection held. If this ever reads `entity`, the split is decoration.
    """
    url = brain_service._recall_db_url(
        "postgresql://entity@/hostmem?host=/var/run/postgresql")
    assert url.startswith("postgresql://host_recall@"), url
    assert "/hostmem" in url and "host=/var/run/postgresql" in url, url


def test_the_recall_url_keeps_the_database_the_entrypoint_resolved():
    """Never re-hardcode `hostmem`. A host whose rename has not happened is still on
    `parallax` (entrypoint.sh probes for both names), and a recall lane pointed at a
    database that does not exist would fail on exactly the oldest hosts.
    """
    url = brain_service._recall_db_url(
        "postgresql://entity@/parallax?host=/var/run/postgresql")
    assert url == "postgresql://host_recall@/parallax?host=/var/run/postgresql", url


def test_get_recall_pool_does_not_silently_fall_back():
    """get_control_pool returns `_control_pool or _pool` because its fallback costs a
    connection slot and nothing else. This one must NOT: falling back hands the read
    path to the superuser lane, which is the one lane a row security policy cannot
    bind. The caller has to be able to see that it happened.
    """
    saved_recall, saved_brain = brain_service._recall_pool, brain_service._pool
    try:
        brain_service._recall_pool = None
        brain_service._pool = object()
        assert brain_service.get_recall_pool() is None
    finally:
        brain_service._recall_pool, brain_service._pool = saved_recall, saved_brain


def test_initialize_actually_hands_the_kwargs_to_asyncpg():
    """The constants above are only worth asserting if initialize() uses them. Without
    this, every assertion in this file would still pass after someone reverted the
    create_pool() calls to literal kwargs and left the constants orphaned.

    Only the pool-creation half of initialize() is exercised: everything downstream
    (provider_gateway, EntityRuntime) is already wrapped in its own try/except and
    logs rather than raises, so it cannot mask a failure here.
    """
    import asyncio

    calls = []

    class FakePool:
        pass

    async def fake_create_pool(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return FakePool()

    import asyncpg

    real = asyncpg.create_pool
    asyncpg.create_pool = fake_create_pool
    try:
        asyncio.run(brain_service.initialize(entity_id=9003))
    finally:
        asyncpg.create_pool = real

    assert len(calls) == 4, (
        f"expected the brain pool, the control lane, the recall lane and the world "
        f"lane, got {len(calls)}")
    assert calls[0][1] == brain_service.BRAIN_POOL_KWARGS
    assert calls[1][1] == brain_service.CONTROL_POOL_KWARGS
    assert calls[2][1] == brain_service.RECALL_POOL_KWARGS
    assert calls[3][1] == brain_service.WORLD_POOL_KWARGS
    # The Host's own SQL runs as host_world, never as the superuser.
    assert calls[3][0].startswith("postgresql://host_world@"), calls[3][0]

    # The DSNs, not just the kwargs: the first two lanes differ only in sizing and
    # must stay on the same connection; the third differs in the ONE way that
    # matters, and asserting the kwargs alone would not notice it regressing.
    assert calls[0][0] == calls[1][0], "the control lane must be the same connection"
    assert calls[2][0].startswith("postgresql://host_recall@"), calls[2][0]


def test_asyncpg_still_treats_zero_as_no_inactivity_timer():
    """THEIR half of the contract. If this fails, asyncpg changed the meaning of 0 and
    the lane is being pruned again — raise max_connections or hold the connection some
    other way, but do not simply delete this test."""
    import asyncpg.pool as apool

    holder = apool.PoolConnectionHolder.__new__(apool.PoolConnectionHolder)
    holder._max_inactive_time = 0
    holder._inactive_callback = None
    holder._pool = None  # only touched when a timer is actually scheduled

    holder._setup_inactive_callback()

    assert holder._inactive_callback is None, (
        "asyncpg scheduled an inactivity timer for max_inactive_connection_lifetime=0 — "
        "the bastion control lane is no longer reserved"
    )


def test_asyncpg_does_schedule_a_timer_for_a_nonzero_lifetime():
    """The negative control. Without it the test above would also pass if
    _setup_inactive_callback stopped scheduling timers at all, which would make it
    prove nothing about the value 0 in particular."""
    import asyncpg.pool as apool

    scheduled = []

    class FakeLoop:
        def call_later(self, delay, cb):
            scheduled.append(delay)
            return object()

    class FakePool:
        _loop = FakeLoop()

    holder = apool.PoolConnectionHolder.__new__(apool.PoolConnectionHolder)
    holder._max_inactive_time = 300
    holder._inactive_callback = None
    holder._pool = FakePool()

    holder._setup_inactive_callback()

    assert scheduled == [300]
    assert holder._inactive_callback is not None


# ── WHICH LANE EACH HALF OF recall_memories GETS ────────────────────────────
# The role split is only worth the connection if the right work goes down the
# right lane, and the first version of this branch got it backwards in one
# place. `_embed_query` reads no entity_memory row: it reads
# entity_registry.behavioral_params and, when embedding_cred_name is set, brokers
# a sealed credential — memory_engine:517 use_credential → vault/api open_secret →
# vault/store get_envelope, and on to the host key in kernel_secrets to open it.
# Handed the restricted pool it raised InsufficientPrivilegeError,
# _embed_via_byo swallowed that and returned None, and _embed_mark_down then took
# EVERY embed in the process down for 60s — writes included — while
# embedding_health() reported the user's provider as not answering.
#
# WHAT RAISES HAS CHANGED, AND THAT IS WHY THESE ASSERTIONS MATTER MORE NOW.
# get_envelope used to open with `CREATE TABLE IF NOT
# EXISTS vault_envelope`, so the wrong pool failed on the FIRST statement —
# PostgreSQL checks permission before existence, so IF NOT EXISTS did not save
# it. That DDL is gone; the table is declared and created at boot. A restricted
# pool now reads vault_envelope fine (host_recall holds SELECT, and the row is
# ciphertext) and fails one statement later, at get_envelope's unconditional
# audit INSERT (store.py:101 -> _audit :26-33) — measured, and NOT at the
# kernel_secrets read, which is one further on again. cred_type_of and
# list_names audit nothing and now succeed on a restricted pool where they used
# to raise. The wall still holds; the tripwire is quieter, and it is these
# assertions that pin the lane now.
#
# Hermetic on purpose: the arms are replaced with recorders, so these run in the
# pre-merge lane rather than only in the docker gate
# (scripts/test_role_split_rls.sh), which is where the SQL half is proved.
#
# AND THE LANE IS NOT OPTIONAL NOW. tests/ was once invoked by nothing — not
# smoke, not pre_merge_gates.sh, not either gates list — so the contract below
# was enforced in production by an accident (the CREATE TABLE in get_envelope)
# and by nothing else at all. tests/ now runs in the pre-merge lane, with a
# ratchet in scripts/test_gate_coverage.py so it cannot fall back out, and the
# accident is gone. If those assertions ever stop running, this contract has no
# enforcement left anywhere.


def _recall_lane_recorder(monkeypatch):
    """Replace the three arms with recorders and return the record."""
    from mastercode import memory_retrieval as mr

    seen = {}

    async def fake_embed_query(pool, entity_id, query):
        seen["embed"] = pool
        return None, "no embedder in this test"

    async def fake_vector_arm(pool, entity_id, query, *, where_sql, params,
                              candidates, vec=None, priv_pool=None):
        seen["vector_rows"] = pool
        seen["vector_priv"] = priv_pool
        return [], False, "no embedder in this test"

    # The real arm's keywords (memory_retrieval._lexical_arm), so the
    # recorder takes every call recall_memories makes of it.
    async def fake_lexical_arm(pool, entity_id, query, *, where_sql, params,
                               candidates, any_words=False):
        seen["lexical_rows"] = pool
        return [{"id": 1, "content": "a row", "importance": 1.0}], True, None

    monkeypatch.setattr(mr, "_embed_query", fake_embed_query)
    monkeypatch.setattr(mr, "_vector_arm", fake_vector_arm)
    monkeypatch.setattr(mr, "_lexical_arm", fake_lexical_arm)
    return seen


def test_the_embed_lane_is_privileged_and_the_row_reads_are_not(monkeypatch):
    """The contract in one assertion per call site."""
    from mastercode import memory_retrieval as mr

    priv, restricted = object(), object()
    monkeypatch.setattr(brain_service, "_pool", priv)
    monkeypatch.setattr(brain_service, "_recall_pool", restricted)
    seen = _recall_lane_recorder(monkeypatch)

    asyncio.run(mr.recall_memories(priv, 1, "anything"))

    assert seen["embed"] is priv, (
        "_embed_query got the restricted pool — it brokers a vault credential, "
        "and get_envelope's audit INSERT into vault_audit is refused to "
        "host_recall — as is the kernel_secrets read behind it")
    assert seen["vector_priv"] is priv, (
        "_vector_arm's re-embed fallback would re-enter the same vault path on "
        "the restricted lane")
    assert seen["vector_rows"] is restricted
    assert seen["lexical_rows"] is restricted


def test_a_caller_with_no_pool_does_not_collapse_the_two_lanes(monkeypatch):
    """recall_memories(None, …) used to leave the privileged half None, and
    `priv_pool or pool` in the arms then resolved it back to the RESTRICTED pool —
    putting the vault broker exactly where it must not be."""
    from mastercode import memory_retrieval as mr

    priv, restricted = object(), object()
    monkeypatch.setattr(brain_service, "_pool", priv)
    monkeypatch.setattr(brain_service, "_recall_pool", restricted)
    seen = _recall_lane_recorder(monkeypatch)

    asyncio.run(mr.recall_memories(None, 1, "anything"))

    assert seen["embed"] is priv and seen["embed"] is not restricted
    assert seen["vector_priv"] is priv


def test_no_score_from_the_privileged_lane_leaves_recall(monkeypatch):
    """A number computed on the PRIVILEGED lane, which no policy binds, would
    read off how many rows the policy hid (the graph arm's 1/degree score did,
    until the arm went with the memory graph). Recall's hits carry
    neither that score nor the fusion's own rank."""
    from mastercode import memory_retrieval as mr

    priv, restricted = object(), object()
    monkeypatch.setattr(brain_service, "_pool", priv)
    monkeypatch.setattr(brain_service, "_recall_pool", restricted)
    _recall_lane_recorder(monkeypatch)

    res = asyncio.run(mr.recall_memories(priv, 1, "anything"))

    assert res["hits"], "the fake lexical arm returned a row; fusion dropped it"
    for h in res["hits"]:
        assert "graph_score" not in h, h
        assert "_rrf" not in h, h
    assert "graph" not in (res.get("arms") or []), res.get("arms")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
