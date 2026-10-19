#!/usr/bin/env python3
"""A containment the user ordered is never discarded because the box could not look.

Runs as pytest (`pytest scripts/test_contain_reconcile.py`) AND as a plain
script (`APP_ROOT=<repo> python3 scripts/…`). No DB: the real
topology.upsert_heartbeat is driven against a recording pool that holds one
pending containment, and the real request_contain against the same pool.

WHAT THIS FILE IS DEFENDING. The beat's `agents` list sits inside the
bastion's observe gate: a pairing without `observe` — the console's switch,
no restart — sends NO key, and a box with no enumerator for its platform
sends an EMPTY list without the `agent-observe` capability. The reconcile read
both as "the box looked and every agent is gone", so every pending
nac_agent_contain row was marked cleared_at=now(), and a kill the user ordered
was silently discarded and never delivered. Two sources of absence, one
reading — the shape the record-holes fix (v163, and the bastion's own) had
one beat field over.

And the hold is bounded: a pid is a number the OS reuses, so an order that
outlives its target (CONTAIN_PENDING_TTL) or whose pid now wears a different
signature is dropped with its reason written down, never delivered.
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from server.network import topology  # noqa: E402

PID = 4242
SIG = "claude-code"


class Pool:
    """asyncpg enough for upsert_heartbeat + request_contain, with ONE pending
    containment for PID unless told otherwise."""

    def __init__(self, pending=None, insert_tag="INSERT 0 1"):
        self.sql: list = []
        self.insert_tag = insert_tag
        self.pending = [{"pid": PID, "signature": SIG, "lapsed": False}] if pending is None else pending
        self.update_touches = True  # False: the reaper already cleared the row

    async def execute(self, q, *a):
        self.sql.append((q, a))
        if "INSERT INTO nac_agent_contain" in q:
            return self.insert_tag  # asyncpg's command tag
        return None

    async def fetchval(self, q, *a):
        self.sql.append((q, a))
        if "FROM mcp_client_tokens" in q and "operator_kind" in q:
            return 1 if a == (7, 3) else None  # token 7 belongs to one Host only
        return None

    async def fetchrow(self, q, *a):
        self.sql.append((q, a))
        if "SELECT detect, alerts FROM network_bastion_heartbeat" in q:
            return None
        if "INSERT INTO network_bastion_heartbeat" in q:
            return {"token_id": 7, "last_seen_at": None, "detect": None, "alerts": None}
        return None

    async def fetch(self, q, *a):
        self.sql.append((q, a))
        if q.startswith("UPDATE nac_agent_contain SET cleared_at=now(), outcome=$3"):
            return [Row({"pid": pid}) for pid in a[1]] if self.update_touches else []
        if "FROM nac_agent_contain" in q:
            return list(self.pending)
        return []

    def acquire(self):
        pool = self

        class _Cx:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *exc):
                return False
        return _Cx()

    def cleared(self):
        """[(pids, outcome)] for every clear the reconcile wrote. The outcome is
        what the SQL actually stores — a parameter the statement never binds
        to the column reads as None here, as it would be in the table."""
        return [(list(a[1]), a[2] if "outcome=$3" in q else None) for q, a in self.sql
                if "UPDATE nac_agent_contain SET cleared_at=now()" in q]

    def audited(self):
        """The decision strings written to the management audit."""
        return [a[3] for q, a in self.sql if "INSERT INTO nac_mgmt_audit" in q]

    def audited_exit(self):
        """(decision, exit_code) pairs written to the management audit."""
        return [(a[3], a[4]) for q, a in self.sql if "INSERT INTO nac_mgmt_audit" in q]

    def clear_sql(self):
        return [q for q, a in self.sql if "UPDATE nac_agent_contain SET cleared_at=now()" in q]

    def heartbeat_arg(self, n):
        """Positional parameter $n of the heartbeat upsert (1-based)."""
        for q, a in self.sql:
            if "INSERT INTO network_bastion_heartbeat" in q:
                return a[n - 1]
        raise AssertionError("no heartbeat upsert was written")


def _beat(caps=("agent-observe",), scan_ok=True, **extra):
    b = {"token_id": 7, "hostname": "box", "platform": "linux",
         "capabilities": list(caps), **extra}
    if scan_ok:
        b["agent_scan_ok"] = True  # the bastion says the walk happened
    return b


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Log(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _with_log(coro):
    h = _Log()
    lg = logging.getLogger("privatae.network.topology")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        return _run(coro), h.lines
    finally:
        lg.removeHandler(h)


# ── could not look: held ────────────────────────────────────────────────────

def test_an_absent_agents_key_holds_the_order():
    """The pairing is not entitled to observe (or the enumerator failed and the
    box withheld the key): nothing cleared, nothing delivered, and the hold is
    the one trace there is, so it must be written."""
    pool = Pool()
    res, lines = _with_log(topology.upsert_heartbeat(pool, 3, _beat()))
    assert res is not None
    assert pool.cleared() == [], f"a pending containment was cleared off a beat that could not look: {pool.cleared()}"
    assert "contain" not in res, f"a kill was delivered to a box that cannot see its agents: {res}"
    held = [l for l in lines if l.startswith("contain reconcile held for token 7") and "1 pending kept" in l]
    assert held, f"the hold left no trace in the log: {lines}"
    assert "no agents list" in held[0], held[0]


def test_a_null_agents_key_holds_the_order():
    """`agents: null` — a client marshalling a nil slice — is not a list the box
    measured. Same as absent: held, not read as 'all gone'."""
    pool = Pool()
    res = _run(topology.upsert_heartbeat(pool, 3, _beat(agents=None)))
    assert pool.cleared() == [], pool.cleared()
    assert "contain" not in res, res


def test_an_agents_value_that_is_not_a_list_holds_the_order():
    """Presence is not measurement: `agents: {}` (or any non-list) is a beat
    the host cannot read, and iterating it as if it were a list would find no
    pid and clear the order."""
    pool = Pool()
    res = _run(topology.upsert_heartbeat(pool, 3, _beat(agents={})))
    assert pool.cleared() == [], f"a non-list agents value was read as 'all gone': {pool.cleared()}"
    assert "contain" not in res, res


def test_an_empty_list_without_agent_observe_holds_the_order():
    """The box sends [] on every path where it could not enumerate
    (agents_other.go, a failed /proc walk) and advertises agent-observe only
    when it has an enumerator. [] without the capability is 'not looked'."""
    pool = Pool()
    res, lines = _with_log(topology.upsert_heartbeat(pool, 3, _beat(caps=("embed",), agents=[])))
    assert pool.cleared() == [], f"an unmeasured empty list cleared the containment: {pool.cleared()}"
    assert "contain" not in res, res
    assert any("agent-observe" in l and "not looked" in l for l in lines), lines


def test_an_empty_list_from_a_bastion_that_cannot_say_the_walk_happened_holds():
    """An older bastion build sent `agents: []` for a FAILED
    walk as well as for a quiet box, with agent-observe advertised either way.
    Without `agent_scan_ok` the list is unmeasured: held, and the reason says
    which door."""
    pool = Pool()
    res, lines = _with_log(topology.upsert_heartbeat(pool, 3, _beat(scan_ok=False, agents=[])))
    assert pool.cleared() == [], f"an empty list the box did not vouch for cleared the containment: {pool.cleared()}"
    assert "contain" not in res, res
    assert any("agent_scan_ok" in l for l in lines), lines


def test_a_non_empty_list_delivers_and_clears_without_the_marker():
    """An older bastion never sends agent_scan_ok, but a non-empty list
    from a box that can enumerate is proof the walk ran: the user's order is
    delivered while the agent is listed, and an agent gone from a non-empty
    list is done. Gating delivery on the marker made contain a no-op on every
    bastion shipped to date."""
    pool = Pool()
    res = _run(topology.upsert_heartbeat(pool, 3, _beat(scan_ok=False, agents=[{"pid": PID, "signature": SIG}])))
    assert pool.cleared() == [], pool.cleared()
    assert res.get("contain") == [PID], f"an older box listing the agent did not get the order: {res}"
    pool = Pool()
    res = _run(topology.upsert_heartbeat(pool, 3, _beat(scan_ok=False, agents=[{"pid": 1, "signature": "other"}])))
    assert pool.cleared() == [([PID], "done")], f"an agent gone from a measured non-empty list was not cleared: {pool.cleared()}"
    assert "contain" not in res, res


def test_agent_scan_ok_must_be_the_boolean_true():
    """A truthy string is not the box saying it looked."""
    pool = Pool()
    b = _beat(scan_ok=False, agents=[])
    b["agent_scan_ok"] = "yes"
    _run(topology.upsert_heartbeat(pool, 3, b))
    assert pool.cleared() == [], pool.cleared()


# ── looked: cleared or delivered ────────────────────────────────────────────

def test_an_empty_list_with_agent_observe_means_the_agent_is_gone():
    """The box looked and found nothing: the kill worked. Cleared as 'done',
    the audit says so, and no directive goes out for a pid that is gone."""
    pool = Pool()
    res = _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[])))
    assert pool.cleared() == [([PID], "done")], f"an observed absence did not clear the containment as done: {pool.cleared()}"
    assert pool.audited() == [f"done pid {PID}"], pool.audited()
    assert "contain" not in res, f"a directive was sent for an agent the box says is gone: {res}"


def test_a_clear_touches_only_open_rows():
    """The outcome UPDATE must be scoped to open rows: a recycled pid has
    closed rows behind it (including the 'superseded' ones v164 wrote), and
    rewriting their cleared_at/outcome would falsify the record."""
    pool = Pool()
    _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[])))
    assert pool.clear_sql() and all("AND cleared_at IS NULL" in q for q in pool.clear_sql()), pool.clear_sql()


def test_the_audit_exit_code_says_whether_the_order_was_carried_out():
    pool = Pool(pending=[{"pid": PID, "signature": SIG, "lapsed": True}])
    _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[{"pid": PID, "signature": SIG}])))
    assert pool.audited_exit() == [(f"expired pid {PID}", 1)], pool.audited_exit()
    pool = Pool()
    _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[{"pid": PID, "signature": "sshd"}])))
    assert pool.audited_exit() == [(f"pid-reused pid {PID}", 1)], pool.audited_exit()
    pool = Pool()
    _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[])))
    assert pool.audited_exit() == [(f"done pid {PID}", 0)], pool.audited_exit()


def test_a_still_present_agent_is_delivered_not_cleared():
    pool = Pool()
    res = _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[{"pid": PID, "signature": SIG}])))
    assert pool.cleared() == [], pool.cleared()
    assert res.get("contain") == [PID], res


def test_a_pid_sent_as_a_digit_string_still_counts_as_present():
    """A dropped pid reads as an agent that is GONE and clears the order; a
    forked client sending "4242" must be coerced, not discarded."""
    pool = Pool()
    res = _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[{"pid": str(PID), "signature": SIG}])))
    assert pool.cleared() == [], f"a string pid was read as absence: {pool.cleared()}"
    assert res.get("contain") == [PID], res


# ── the hold is bounded ─────────────────────────────────────────────────────

def test_a_pid_now_wearing_another_signature_is_not_killed():
    """The number the user pointed at now belongs to a different process: the
    order is dropped as 'pid_reused', never delivered."""
    pool = Pool()
    res = _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[{"pid": PID, "signature": "sshd"}])))
    assert pool.cleared() == [([PID], "pid_reused")], pool.cleared()
    assert pool.audited() == [f"pid-reused pid {PID}"], pool.audited()
    assert "contain" not in res, f"a reused pid was sent for termination: {res}"


def test_a_signature_compares_with_itself_as_stored():
    """nac_agents.ingest stores signatures stripped and cut at 64; the beat's
    raw value must be normalised the same way or a signature would mismatch
    itself and the order be dropped as a reuse that did not happen."""
    long = "Xx" * 40  # mixed case: stored as-is, never folded
    pool = Pool(pending=[{"pid": PID, "signature": long[:64], "lapsed": False}])
    res = _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[{"pid": PID, "signature": "  " + long + "  "}])))
    assert pool.cleared() == [], f"a signature mismatched itself: {pool.cleared()}"
    assert res.get("contain") == [PID], res


def test_a_reported_pid_with_no_signature_is_delivered_not_dropped():
    """The beat names the pid but no signature (a client that sends none):
    identity cannot be compared, and the order the user placed is delivered
    rather than dropped as pid_reused on a comparison that never happened."""
    pool = Pool()
    res = _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[{"pid": PID}])))
    assert pool.cleared() == [], f"a missing signature was read as a different process: {pool.cleared()}"
    assert res.get("contain") == [PID], res


def test_an_order_without_a_signature_relies_on_age_alone():
    """No signature recorded (an activity row from before kinds carried one):
    identity cannot be checked, so a present pid is delivered."""
    pool = Pool(pending=[{"pid": PID, "signature": None, "lapsed": False}])
    res = _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[{"pid": PID, "signature": "anything"}])))
    assert pool.cleared() == [], pool.cleared()
    assert res.get("contain") == [PID], res


def test_an_order_older_than_the_ttl_lapses_even_when_the_pid_is_present():
    pool = Pool(pending=[{"pid": PID, "signature": SIG, "lapsed": True}])
    res = _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[{"pid": PID, "signature": SIG}])))
    assert pool.cleared() == [([PID], "expired")], pool.cleared()
    assert pool.audited() == [f"expired pid {PID}"], pool.audited()
    assert "contain" not in res, f"a lapsed order was still delivered: {res}"


def test_an_order_older_than_the_ttl_lapses_on_a_beat_that_could_not_look():
    """Expiry is about the order's age, not the box's sight: a box that never
    looks again must not carry a dead order on its card forever."""
    pool = Pool(pending=[{"pid": PID, "signature": SIG, "lapsed": True}])
    res = _run(topology.upsert_heartbeat(pool, 3, _beat()))
    assert pool.cleared() == [([PID], "expired")], pool.cleared()
    assert "contain" not in res, res


def test_an_order_inside_the_ttl_is_still_delivered():
    pool = Pool(pending=[{"pid": PID, "signature": SIG, "lapsed": False}])
    res = _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[{"pid": PID, "signature": SIG}])))
    assert pool.cleared() == [], pool.cleared()
    assert res.get("contain") == [PID], res


def test_lapse_is_judged_on_the_databases_clock_with_the_one_ttl():
    """Two expiry paths (this reconcile and the reaper) on one TTL must read
    ONE clock. The pending SELECT asks PostgreSQL whether the order lapsed,
    with CONTAIN_PENDING_TTL as the interval; nothing here reads the pod's
    clock. The fake pool cannot evaluate SQL, so the text and the parameter
    are what is pinned, not the real-PG result."""
    pool = Pool()
    _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[])))
    q, a = next((q, a) for q, a in pool.sql if "FROM nac_agent_contain WHERE token_id=$1 AND cleared_at IS NULL" in q)
    assert "(requested_at < now() - $2::interval) AS lapsed" in q, q
    assert a == (7, topology.CONTAIN_PENDING_TTL), a


def test_a_null_lapsed_flag_does_not_expire():
    """A row the database could not date (NULL requested_at → NULL lapsed) is
    delivered, never expired on a guess."""
    pool = Pool(pending=[{"pid": PID, "signature": SIG, "lapsed": None}])
    res = _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[{"pid": PID, "signature": SIG}])))
    assert pool.cleared() == [] and res.get("contain") == [PID], (pool.cleared(), res)


def test_an_order_the_reaper_already_lapsed_is_not_audited_twice():
    """The reaper wins the race by a tick: this UPDATE touches nothing, so
    this path writes no second 'expired' line for the same order."""
    pool = Pool(pending=[{"pid": PID, "signature": SIG, "lapsed": True}])
    pool.update_touches = False
    _run(topology.upsert_heartbeat(pool, 3, _beat(agents=[])))
    assert pool.cleared() != [], "the UPDATE must still be attempted"
    assert pool.audited() == [], f"an order already lapsed by the reaper was audited again: {pool.audited()}"
    # The audit is fed by RETURNING, so the statement's shape is load-bearing
    # and the fake pool (which fabricates the rows) cannot pin it: the text is.
    q = next(q for q, _ in pool.sql if q.startswith("UPDATE nac_agent_contain SET cleared_at=now(), outcome=$3"))
    assert "WHERE token_id=$1 AND pid = ANY($2::int[]) AND cleared_at IS NULL" in q, q
    assert q.rstrip().endswith("RETURNING pid"), q


# ── placing the order ───────────────────────────────────────────────────────

def test_request_contain_records_the_signature_and_says_queued():
    pool = Pool()
    got = _run(topology.request_contain(pool, 3, 7, PID))
    assert got == "queued", got
    ins = [(q, a) for q, a in pool.sql if "INSERT INTO nac_agent_contain" in q]
    assert len(ins) == 1, ins
    q, a = ins[0]
    assert "signature" in q and "FROM nac_agent_activity" in q, q
    # One open order per pid is the table's rule (partial unique index, v164),
    # and the INSERT defers to it rather than racing a NOT EXISTS.
    assert "ON CONFLICT (token_id, pid) WHERE cleared_at IS NULL DO NOTHING" in q, q
    assert a == (7, PID, None), a  # (token, pid, approval — v179: NULL until the person taps)


def test_request_contain_refuses_a_bastion_that_is_not_this_entitys():
    pool = Pool()
    got = _run(topology.request_contain(pool, 99, 7, PID))
    assert got is False, got
    assert not [q for q, a in pool.sql if "INSERT INTO nac_agent_contain" in q], "a kill order was queued on another entity's bastion"


def test_request_contain_refuses_a_revoked_bastion():
    """The ownership query must exclude a revoked token: a kill order on a
    bastion the user already cut off would sit in the queue for a box that
    will never beat again, and the card would count it. The fake pool cannot
    evaluate SQL, so the predicate itself is pinned."""
    pool = Pool()
    _run(topology.request_contain(pool, 3, 7, PID))
    own = [q for q, a in pool.sql if "FROM mcp_client_tokens" in q]
    assert own and "revoked_at IS NULL" in own[0] and "operator_kind='bastion'" in own[0], own


def test_request_contain_says_open_when_the_order_already_exists():
    pool = Pool(insert_tag="INSERT 0 0")
    got = _run(topology.request_contain(pool, 3, 7, PID))
    assert got == "open", got


def test_the_management_audit_never_says_ran_for_a_click_that_queued_nothing():
    from server.network import nac_mgmt
    decision, result = nac_mgmt.contain_queue_result("box", PID, "queued")
    assert decision == "ran" and '"contain": "queued"' in result["stdout"], (decision, result)
    decision, result = nac_mgmt.contain_queue_result("box", PID, "open")
    assert decision == "already-queued", decision
    assert '"contain": "already queued"' in result["stdout"] and result["exit"] == 0, result
    decision, result = nac_mgmt.contain_queue_result("box", PID, False)
    assert decision == "no-bastion" and result is None, (decision, result)


def test_the_migration_closes_old_duplicates_before_it_indexes_them():
    """The index cannot be built over the duplicate open rows the old INSERT
    allowed; the migration must close them first, newest kept."""
    sql = open(os.path.join(_ROOT, "migrations", "v164_nac_agent_contain_outcome.sql"), encoding="utf-8").read()
    i_close = sql.find("outcome = 'superseded'")
    i_index = sql.find("CREATE UNIQUE INDEX IF NOT EXISTS nac_agent_contain_open_pid")
    assert 0 < i_close < i_index, "duplicates must be closed BEFORE the partial unique index is created"
    assert "d.id > c.id" in sql, "the newest open order must be the one kept"
    assert "WHERE cleared_at IS NULL" in sql[i_index:], "the index must be partial: closed rows may repeat a pid"


# ── the surfaces ────────────────────────────────────────────────────────────

class Row(dict):
    """asyncpg Record enough: subscript + .keys()."""


def test_list_bastions_selects_the_pending_orders_and_the_row_reader_passes_them_through():
    consts = topology.list_bastions.__code__.co_consts
    assert any(isinstance(c, str) and "AS contain_pending" in c and "FROM nac_agent_contain c" in c
               and "c.cleared_at IS NULL" in c for c in consts), \
        "list_bastions does not SELECT the open orders, so the card can never show them"
    base = {k: None for k in (
        "id", "name", "url_handle", "scopes", "hardware_fingerprint", "machine_anchor_id",
        "notes", "created_at", "last_used_at", "hostname", "gateway_version", "platform",
        "tunnel_status", "reported_ip", "wg_pubkey", "capabilities", "models", "last_seen_at",
        "online", "freeagent_ok", "freeagent_stores")}
    pend = [{"pid": PID, "signature": SIG, "requested_at": "2026-09-19T22:00:00+00:00"}]
    import json as _json
    row = topology._bastion_row(Row({**base, "contain_pending": _json.dumps(pend)}))
    assert row["contain_pending"] == pend, row["contain_pending"]
    assert topology._bastion_row(Row(dict(base)))["contain_pending"] == []


def test_list_observations_carries_when_the_order_was_placed():
    from server.network import nac_agents

    class P:
        def __init__(self):
            self.q = None

        async def fetch(self, q, *a):
            self.q = q
            return [Row({"token_id": 7, "hostname": "box", "pid": PID, "ppid": 1, "agent_user": "u",
                         "signature": SIG, "cmdline": "x", "llm_endpoints": "[]", "delegation_token": None,
                         "first_seen": None, "last_seen": None, "kind": "agent", "family": None,
                         "origin": None, "evidence": "[]", "active": True, "requested": False,
                         "acknowledged": False,
                         "contain_pending_since": datetime(2026, 9, 19, 22, 0, tzinfo=timezone.utc)})]
    p = P()
    out = _run(nac_agents.list_observations(p, 3))
    assert "AS contain_pending_since" in p.q and "FROM nac_agent_contain c" in p.q, p.q
    assert out[0]["contain_pending_since"] == "2026-09-19T22:00:00+00:00", out[0]["contain_pending_since"]


# ── a box that stopped beating ──────────────────────────────────────────────

def test_the_reaper_lapses_orders_no_beat_will_ever_expire():
    """Expiry in the reconcile needs a beat; a box powered off keeps its order
    open and its card says "containment waiting" forever. The reaper lapses
    it on the same TTL, with the same outcome and audit line."""
    class P(Pool):
        async def fetch(self, q, *a):
            self.sql.append((q, a))
            if q.startswith("UPDATE nac_agent_contain c SET cleared_at=now(), outcome='expired'"):
                return [Row({"pid": PID, "entity_id": 9003, "hostname": "box"})]
            return []
    pool = P()
    n = _run(topology.expire_lapsed_containments(pool))
    assert n == 1, n
    q, a = next((q, a) for q, a in pool.sql if q.startswith("UPDATE nac_agent_contain"))
    assert "c.cleared_at IS NULL" in q and "c.requested_at < now() - $1::interval" in q, q
    assert a == (topology.CONTAIN_PENDING_TTL,), "the reaper must lapse on the same TTL as the reconcile"
    # The audit is written from what the statement RETURNS: the ENTITY's id
    # (never the token's), the box's hostname, and a box with no heartbeat
    # row must still lapse (LEFT JOIN) — the fake pool returns fixed rows, so
    # the statement is pinned by its text.
    assert "LEFT JOIN network_bastion_heartbeat h ON h.token_id = t.id" in q, q
    assert q.rstrip().endswith("RETURNING c.pid, t.entity_id, h.hostname"), q
    assert pool.audited_exit() == [(f"expired pid {PID}", 1)], pool.audited_exit()
    audit = next(a for q, a in pool.sql if "INSERT INTO nac_mgmt_audit" in q)
    assert audit[0] == 9003 and audit[1] == "box", f"the audit must land under the order's entity and box: {audit}"
    assert _run(topology.expire_lapsed_containments(None)) == 0, "a no-pool pod must be a silent no-op, as reap_stale_endpoints is"


def test_the_reaper_loop_lapses_containments_and_survives_their_failure():
    """The real loop, three ticks: it calls the lapse each tick, and when the
    lapse raises the loop keeps reaping stale endpoints as before."""
    from server.network import nac_controller
    import server.loop_splay as splay_mod
    calls = {"reap": 0, "lapse": 0}

    async def fake_reap(pool):
        calls["reap"] += 1
        return 0

    async def failing_lapse(pool):
        calls["lapse"] += 1
        raise RuntimeError("db away")

    async def no_splay(*a, **k):
        return None
    saved = (nac_controller.reap_stale_endpoints, topology.expire_lapsed_containments, splay_mod.splay)
    nac_controller.reap_stale_endpoints = fake_reap
    topology.expire_lapsed_containments = failing_lapse
    splay_mod.splay = no_splay
    try:
        async def drive():
            task = asyncio.ensure_future(nac_controller.run_reaper_loop(None, interval_seconds=0.01))
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        _run(drive())
    finally:
        nac_controller.reap_stale_endpoints, topology.expire_lapsed_containments, splay_mod.splay = saved
    assert calls["lapse"] >= 3, f"the loop did not lapse containments each tick: {calls}"
    assert calls["reap"] >= calls["lapse"], f"a failing lapse stopped the reaper: {calls}"


# ── the sibling field: schedulers ───────────────────────────────────────────

def test_a_beat_without_a_schedulers_key_keeps_the_stored_inventory():
    """Same shape one field over: `schedulers` is inside the same observe gate.
    The upsert parameter is None for an absent key (the SQL COALESCEs it to
    the stored value) and "[]" for an explicit empty list."""
    pool = Pool(pending=[])
    _run(topology.upsert_heartbeat(pool, 3, _beat()))
    assert pool.heartbeat_arg(17) is None, f"an absent schedulers key was written as {pool.heartbeat_arg(17)!r}"
    pool = Pool(pending=[])
    _run(topology.upsert_heartbeat(pool, 3, _beat(schedulers=[])))
    assert pool.heartbeat_arg(17) == "[]", pool.heartbeat_arg(17)


def test_the_upsert_sql_keeps_schedulers_on_a_null_parameter():
    pool = Pool(pending=[])
    _run(topology.upsert_heartbeat(pool, 3, _beat()))
    q = next(q for q, a in pool.sql if "INSERT INTO network_bastion_heartbeat" in q)
    assert "schedulers      = COALESCE($17::jsonb, network_bastion_heartbeat.schedulers)" in q, q
    assert "COALESCE($17::jsonb, '[]'::jsonb)" in q, q


def _tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    import re
    print("containment reconcile — an order is held when the box cannot look, and never outlives its target")
    tests = _tests()
    declared = len(re.findall(r"^def (test_\w+)", open(__file__, encoding="utf-8").read(), re.M))
    if declared != len(tests):
        print(f"FAIL  {declared} tests defined, {len(tests)} collected")
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
