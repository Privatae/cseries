#!/usr/bin/env python3
"""A finding stop is cleared only by the box's answer or its age — never by silence.

Runs as pytest (`pytest scripts/test_contain_finding.py`) AND as a plain script
(`APP_ROOT=<repo> python3 scripts/…`). No DB: the real topology.upsert_heartbeat,
request_contain_finding, expire_lapsed_containments and nac_mgmt.run_mgmt_op are
driven against a recording pool.

WHAT THIS FILE IS DEFENDING (v172). A finding is named by
FINGERPRINT and an order id, never a pid. The agent reconcile infers "done"
from a pid missing from the agents list; a finding's pids are never on the
beat, so the same inference here would clear every order on the first beat
and deliver none. The order is cleared ONLY when the box answers that order's
id with that order's fingerprint, or when it lapses (CONTAIN_PENDING_TTL).
A bastion before 0.32.0 never answers; its orders are delivered and lapse.
"""
import asyncio
import os
import sys

_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from server.network import topology  # noqa: E402

FP = "0123456789abcdef0123456789abcdef"
FP2 = "fedcba9876543210fedcba9876543210"


class Row(dict):
    """asyncpg Record enough: subscript + .keys()."""


class Pool:
    """asyncpg enough for upsert_heartbeat's finding reconcile and
    request_contain_finding, holding the given pending finding orders."""

    def __init__(self, pending=None, insert_tag="INSERT 0 1", expired=None):
        self.sql: list = []
        self.insert_tag = insert_tag
        self.pending = [{"id": 11, "fp": FP, "lapsed": False}] if pending is None else pending
        # Orders already cleared as 'expired' within the late-answer window.
        self.expired = expired or []
        self.live_for_hostname = 1

    async def execute(self, q, *a):
        self.sql.append((q, a))
        if "INSERT INTO nac_finding_contain" in q:
            return self.insert_tag
        return None

    async def fetchval(self, q, *a):
        self.sql.append((q, a))
        if "FROM mcp_client_tokens" in q and "operator_kind" in q:
            return 1 if a in ((7, 9003), (9, 9003)) else None  # tokens 7 and 9 belong to one Host
        if "SELECT token_id FROM network_bastion_heartbeat" in q:
            return 7
        if "FROM network_bastion_heartbeat WHERE token_id=$1 AND hostname=$2" in q:
            return 1 if a == (7, "box") else None
        if "count(DISTINCT h.token_id)" in q:
            return self.live_for_hostname
        return None

    async def fetchrow(self, q, *a):
        self.sql.append((q, a))
        if "INSERT INTO network_bastion_heartbeat" in q:
            return {"token_id": 7, "last_seen_at": None, "detect": None, "alerts": None}
        return None

    async def fetch(self, q, *a):
        self.sql.append((q, a))
        if q.startswith("UPDATE nac_finding_contain SET cleared_at=now()"):
            return [Row({"id": a[0]})]
        if q.startswith("UPDATE nac_finding_contain SET outcome=$2"):
            return [Row({"id": a[0]})] if any(r["id"] == a[0] for r in self.expired) else []
        if "outcome='expired'" in q and q.lstrip().startswith("SELECT id, fp FROM nac_finding_contain"):
            return [Row(r) for r in self.expired if r["id"] in a[2]]
        if "FROM nac_finding_contain WHERE token_id=$1" in q:
            return [Row(r) for r in self.pending]
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
        """[(order id, outcome, stopped)] for every clear written."""
        return [a for q, a in self.sql if q.startswith("UPDATE nac_finding_contain SET cleared_at=now()")]

    def rewritten(self):
        """[(order id, outcome, stopped)] for every late-answer rewrite."""
        return [a for q, a in self.sql if q.startswith("UPDATE nac_finding_contain SET outcome=$2")]

    def audited_exit(self):
        return [(a[3], a[4]) for q, a in self.sql if "INSERT INTO nac_mgmt_audit" in q]


def _beat(**extra):
    return {"token_id": 7, "hostname": "box", "platform": "linux",
            "capabilities": ["agent-observe"], "agent_scan_ok": True, "agents": [], **extra}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _answer(oid=11, fp=FP, outcome="killed", stopped=3):
    return {"id": oid, "fp": fp, "outcome": outcome, "stopped": stopped, "at": "2026-09-23T12:00:00Z"}


# ── delivery: silence is never done ─────────────────────────────────────────

def test_a_pending_order_is_delivered_with_its_id_and_nothing_is_cleared():
    """An empty answer list is a box that has answered nothing yet — the
    agents-list inference ('absent means gone') must not leak in here."""
    pool = Pool()
    res = _run(topology.upsert_heartbeat(pool, 9003, _beat(contain_results=[])))
    assert pool.cleared() == [], f"an order was cleared off a beat that did not answer it: {pool.cleared()}"
    assert res.get("contain_findings") == [{"id": 11, "fp": FP}], res.get("contain_findings")


def test_a_box_that_predates_the_answer_key_gets_the_order_and_it_stays_open():
    """A bastion before 0.32.0 sends no `contain_results` and ignores the
    directive. The order is delivered (harmless) and waits out the TTL."""
    pool = Pool()
    res = _run(topology.upsert_heartbeat(pool, 9003, _beat()))
    assert pool.cleared() == [], pool.cleared()
    assert res.get("contain_findings") == [{"id": 11, "fp": FP}], res


# ── the answer clears, in the box's own words ───────────────────────────────

def test_the_boxs_answer_clears_the_order_with_its_outcome_and_count():
    pool = Pool()
    res = _run(topology.upsert_heartbeat(pool, 9003, _beat(contain_results=[_answer()])))
    assert pool.cleared() == [(11, "killed", 3)], pool.cleared()
    assert "contain_findings" not in res, f"an answered order was delivered again: {res}"
    assert pool.audited_exit() == [(f"killed finding {FP[:12]} (3 stopped)", 0)], pool.audited_exit()


def test_a_refusal_is_cleared_and_audited_as_not_carried_out():
    pool = Pool()
    _run(topology.upsert_heartbeat(pool, 9003, _beat(contain_results=[_answer(outcome="in_baseline", stopped=0)])))
    assert pool.cleared() == [(11, "in_baseline", 0)], pool.cleared()
    assert pool.audited_exit() == [(f"in-baseline finding {FP[:12]}", 1)], pool.audited_exit()


def test_an_answer_naming_another_fingerprint_clears_nothing():
    """The id matched but the fingerprint did not: that answer is not about
    this order. Clearing on it would record an outcome for something the
    user never asked about."""
    pool = Pool()
    res = _run(topology.upsert_heartbeat(pool, 9003, _beat(contain_results=[_answer(fp=FP2)])))
    assert pool.cleared() == [], pool.cleared()
    assert res.get("contain_findings") == [{"id": 11, "fp": FP}], res


def test_an_answer_for_another_order_clears_nothing():
    pool = Pool()
    res = _run(topology.upsert_heartbeat(pool, 9003, _beat(contain_results=[_answer(oid=12)])))
    assert pool.cleared() == [], pool.cleared()
    assert res.get("contain_findings") == [{"id": 11, "fp": FP}], res


def test_an_unknown_outcome_word_is_stored_as_unrecognised():
    """The box's word goes into the audit; a word this host does not know is
    not written through verbatim."""
    pool = Pool()
    _run(topology.upsert_heartbeat(pool, 9003, _beat(contain_results=[_answer(outcome="<script>", stopped=0)])))
    assert pool.cleared() == [(11, "unrecognised", 0)], pool.cleared()


def test_malformed_answers_clear_nothing():
    for bad in ([{"id": "11", "fp": FP, "outcome": "killed"}],   # id not an int
                [{"id": True, "fp": FP, "outcome": "killed"}],   # a bool is not an id
                [{"id": 11, "fp": "not-hex", "outcome": "killed"}],
                ["killed"], {"11": "killed"}, None):
        pool = Pool()
        _run(topology.upsert_heartbeat(pool, 9003, _beat(contain_results=bad)))
        assert pool.cleared() == [], (bad, pool.cleared())
    # True == 1 in Python: a bool must not answer order 1.
    pool = Pool(pending=[{"id": 1, "fp": FP, "lapsed": False}])
    _run(topology.upsert_heartbeat(pool, 9003, _beat(contain_results=[
        {"id": True, "fp": FP, "outcome": "killed", "stopped": 1}])))
    assert pool.cleared() == [], f"a boolean answered order 1: {pool.cleared()}"


def test_a_negative_or_non_int_stopped_count_is_zero():
    pool = Pool()
    _run(topology.upsert_heartbeat(pool, 9003, _beat(contain_results=[_answer(stopped=-4)])))
    assert pool.cleared() == [(11, "killed", 0)], pool.cleared()


# ── lapse ───────────────────────────────────────────────────────────────────

def test_an_unanswered_order_older_than_the_ttl_lapses_and_is_not_delivered():
    pool = Pool(pending=[{"id": 11, "fp": FP, "lapsed": True}])
    res = _run(topology.upsert_heartbeat(pool, 9003, _beat(contain_results=[])))
    assert pool.cleared() == [(11, "expired", 0)], pool.cleared()
    assert "contain_findings" not in res, res
    assert pool.audited_exit() == [(f"expired finding {FP[:12]}", 1)], pool.audited_exit()


def test_an_answer_wins_over_a_lapse_on_the_same_beat():
    """The box DID answer; the order's age does not rewrite what happened."""
    pool = Pool(pending=[{"id": 11, "fp": FP, "lapsed": True}])
    _run(topology.upsert_heartbeat(pool, 9003, _beat(contain_results=[_answer()])))
    assert pool.cleared() == [(11, "killed", 3)], pool.cleared()


def test_the_lapse_uses_the_one_ttl():
    pool = Pool()
    _run(topology.upsert_heartbeat(pool, 9003, _beat()))
    q, a = next((q, a) for q, a in pool.sql if "FROM nac_finding_contain WHERE token_id=$1" in q)
    assert a == (7, topology.CONTAIN_PENDING_TTL), a
    assert "cleared_at IS NULL" in q, q


def test_the_lapse_is_computed_by_the_database_against_the_one_ttl():
    """The fake pool hands `lapsed` back, so pin the statement that computes
    it: the database's clock, the same TTL as the reaper, open rows only."""
    pool = Pool()
    _run(topology.upsert_heartbeat(pool, 9003, _beat()))
    q = next(q for q, a in pool.sql if "FROM nac_finding_contain WHERE token_id=$1" in q)
    # MUTATION: compute lapse in Python, or against another interval.
    # RESULT: red.
    assert "(requested_at < now() - $2::interval) AS lapsed" in q, q


def test_a_late_answer_rewrites_an_expiry_within_the_window():
    """The reaper expired the order a minute before the box's answer landed:
    the answer is what happened, so it replaces 'expired' and says so."""
    pool = Pool(pending=[], expired=[{"id": 11, "fp": FP}])
    _run(topology.upsert_heartbeat(pool, 9003, _beat(contain_results=[_answer()])))
    # MUTATION: drop the late-answer pass.  RESULT: red.
    assert pool.rewritten() == [(11, "killed", 3)], pool.rewritten()
    assert pool.audited_exit() == [
        (f"late answer killed finding {FP[:12]} (3 stopped) — was recorded expired", 0)], pool.audited_exit()
    q, a = next((q, a) for q, a in pool.sql if "outcome='expired'" in q and q.lstrip().startswith("SELECT"))
    assert a[1] == topology.FINDING_LATE_ANSWER_WINDOW and "cleared_at > now() - $2::interval" in q, (q, a)


def test_a_late_answer_for_another_fingerprint_rewrites_nothing():
    pool = Pool(pending=[], expired=[{"id": 11, "fp": FP2}])
    _run(topology.upsert_heartbeat(pool, 9003, _beat(contain_results=[_answer()])))
    assert pool.rewritten() == [], pool.rewritten()


def test_run_mgmt_op_uses_the_token_the_caller_resolved():
    """Two bastions can share a hostname. A caller that resolved the token
    passes it; it is used only if that token reports this hostname."""
    from server.network import nac_mgmt
    restore = _mgmt_open_gate(nac_mgmt)
    try:
        pool = Pool()
        _run(nac_mgmt.run_mgmt_op(pool, 9003, "box", nac_mgmt.CONTAIN_OP, ["finding:" + FP], token_id=7))
        assert [a for q, a in pool.sql if "INSERT INTO nac_finding_contain" in q] == [(7, FP, None)]
        # MUTATION: ignore token_id and look up by hostname.  RESULT: red — no 404.
        # Token 9 IS this entity's bastion, but it does not report "box": the
        # order must not be queued against it.
        other = Pool()
        try:
            _run(nac_mgmt.run_mgmt_op(other, 9003, "box", nac_mgmt.CONTAIN_OP, ["finding:" + FP], token_id=9))
            raise AssertionError("a token that does not report this hostname was used")
        except nac_mgmt.MgmtError as e:
            assert e.status == 404 and "does not report" in e.detail, (e.status, e.detail)
        assert not any("INSERT INTO nac_finding_contain" in q for q, a in other.sql), \
            "an order was queued against the other box"
    finally:
        restore()


def test_an_ambiguous_hostname_is_refused_when_no_token_is_given():
    """The mgmt_run tool addresses a machine by hostname. Two live bastions with
    that hostname: refused with a way out, nothing queued, audited."""
    from server.network import nac_mgmt
    restore = _mgmt_open_gate(nac_mgmt)
    try:
        pool = Pool()
        pool.live_for_hostname = 2
        # MUTATION: drop the ambiguity check.  RESULT: red — the order is queued.
        try:
            _run(nac_mgmt.run_mgmt_op(pool, 9003, "box", nac_mgmt.CONTAIN_OP, ["finding:" + FP]))
            raise AssertionError("an ambiguous hostname was resolved by guessing")
        except nac_mgmt.MgmtError as e:
            assert e.status == 409 and "bastion card" in e.detail, (e.status, e.detail)
        assert not any("INSERT INTO nac_finding_contain" in q for q, a in pool.sql)
        assert ("ambiguous-hostname", 1) in pool.audited_exit(), pool.audited_exit()
        # One live bastion: unchanged behaviour.
        one = Pool()
        _run(nac_mgmt.run_mgmt_op(one, 9003, "box", nac_mgmt.CONTAIN_OP, ["finding:" + FP]))
        assert [a for q, a in one.sql if "INSERT INTO nac_finding_contain" in q] == [(7, FP, None)]
    finally:
        restore()


def test_the_routes_report_what_was_actually_queued():
    from server.routes import network as net
    # MUTATION: return "queued" unconditionally.  RESULT: red.
    assert net._queued_word({"stdout": '{"contain": "already queued"}'}) == "already queued"
    assert net._queued_word({"stdout": '{"contain": "queued"}'}) == "queued"
    assert net._queued_word({"stdout": "not json"}) == "queued"


def test_the_reaper_lapses_finding_orders_for_a_box_that_stopped_beating():
    class P(Pool):
        async def fetch(self, q, *a):
            self.sql.append((q, a))
            if q.startswith("UPDATE nac_finding_contain c SET cleared_at=now(), outcome='expired'"):
                return [Row({"fp": FP, "entity_id": 9003, "hostname": "box"})]
            return []
    pool = P()
    assert _run(topology.expire_lapsed_containments(pool)) == 1
    q, a = next((q, a) for q, a in pool.sql if q.startswith("UPDATE nac_finding_contain c"))
    assert a == (topology.CONTAIN_PENDING_TTL,) and "c.cleared_at IS NULL" in q, (q, a)
    assert pool.audited_exit() == [(f"expired finding {FP[:12]}", 1)], pool.audited_exit()


# ── placing the order ───────────────────────────────────────────────────────

def test_request_contain_finding_validates_the_fingerprint_and_the_bastion():
    pool = Pool()
    assert _run(topology.request_contain_finding(pool, 9003, 7, "abc")) == "bad_fp"
    assert _run(topology.request_contain_finding(pool, 9003, 7, FP.upper())) == "bad_fp", \
        "the fingerprint is lowercase hex; the caller normalises, the store does not guess"
    assert _run(topology.request_contain_finding(pool, 4, 7, FP)) is False, "another entity's bastion"
    assert _run(topology.request_contain_finding(pool, 9003, 7, FP)) == "queued"
    ins = [a for q, a in pool.sql if "INSERT INTO nac_finding_contain" in q]
    assert ins == [(7, FP, None)], ins
    assert _run(topology.request_contain_finding(Pool(insert_tag="INSERT 0 0"), 9003, 7, FP)) == "open"


def _mgmt_open_gate(nac_mgmt):
    """Swap the grant machinery for an open gate; returns a restore function."""
    from server.network import nac_stepup
    saved = (nac_mgmt.load_mgmt_grants, nac_mgmt.load_suspended_servers, nac_mgmt.load_quarantined,
             nac_mgmt.mgmt_decision, nac_stepup.entity_verify_pubkey)

    async def _none(*a, **k):
        return None

    async def _empty(*a, **k):
        return set()

    async def _grants(*a, **k):
        return []
    nac_mgmt.load_mgmt_grants, nac_mgmt.load_suspended_servers, nac_mgmt.load_quarantined = _grants, _empty, _empty
    nac_mgmt.mgmt_decision = lambda *a, **k: True
    nac_stepup.entity_verify_pubkey = _none

    def restore():
        (nac_mgmt.load_mgmt_grants, nac_mgmt.load_suspended_servers, nac_mgmt.load_quarantined,
         nac_mgmt.mgmt_decision, nac_stepup.entity_verify_pubkey) = saved
    return restore


def test_run_mgmt_op_routes_a_finding_arg_to_the_finding_queue():
    """`contain finding:<fp>` places a finding order under the same grant and
    op; a bare integer is still an agent pid."""
    from server.network import nac_mgmt
    restore = _mgmt_open_gate(nac_mgmt)
    try:
        pool = Pool()
        res = _run(nac_mgmt.run_mgmt_op(pool, 9003, "box", nac_mgmt.CONTAIN_OP, ["finding:" + FP.upper()]))
        assert res["exit"] == 0 and '"finding": "' + FP + '"' in res["stdout"], res
        assert [a for q, a in pool.sql if "INSERT INTO nac_finding_contain" in q] == [(7, FP, None)], \
            "the fingerprint must be normalised to lowercase before it is stored"
        assert not any("INSERT INTO nac_agent_contain" in q for q, a in pool.sql), \
            "a finding order must never be written as an agent pid order"
        try:
            _run(nac_mgmt.run_mgmt_op(Pool(), 9003, "box", nac_mgmt.CONTAIN_OP, ["finding:nothex"]))
            raise AssertionError("a malformed fingerprint was accepted")
        except nac_mgmt.MgmtError as e:
            assert e.status == 400, e.status
    finally:
        restore()


def test_a_finding_order_is_refused_without_the_grant():
    """The finding path sits behind the same default-deny gate: no grant, no
    order, exit 126 in the bastion-allowlist shape."""
    from server.network import nac_mgmt
    restore = _mgmt_open_gate(nac_mgmt)
    nac_mgmt.mgmt_decision = lambda *a, **k: False
    try:
        pool = Pool()
        res = _run(nac_mgmt.run_mgmt_op(pool, 9003, "box", nac_mgmt.CONTAIN_OP, ["finding:" + FP]))
        assert res["exit"] == 126, res
        assert not any("INSERT INTO nac_finding_contain" in q for q, a in pool.sql), \
            "an ungranted finding order was queued"
    finally:
        restore()


# ── the surfaces ────────────────────────────────────────────────────────────

def test_list_bastions_selects_open_finding_orders_and_the_row_reader_passes_them():
    consts = topology.list_bastions.__code__.co_consts
    assert any(isinstance(c, str) and "AS contain_findings_pending" in c and "FROM nac_finding_contain f" in c
               and "f.cleared_at IS NULL" in c for c in consts), \
        "list_bastions does not SELECT the open finding orders"
    base = {k: None for k in (
        "id", "name", "url_handle", "scopes", "hardware_fingerprint", "machine_anchor_id",
        "notes", "created_at", "last_used_at", "hostname", "gateway_version", "platform",
        "tunnel_status", "reported_ip", "wg_pubkey", "capabilities", "models", "last_seen_at",
        "online", "freeagent_ok", "freeagent_stores")}
    pend = [{"fp": FP, "requested_at": "2026-09-23T12:00:00+00:00"}]
    import json as _json
    row = topology._bastion_row(Row({**base, "contain_findings_pending": _json.dumps(pend)}))
    assert row["contain_findings_pending"] == pend, row
    assert topology._bastion_row(Row(dict(base)))["contain_findings_pending"] == []


def test_the_migration_indexes_one_open_order_per_fingerprint():
    sql = open(os.path.join(_ROOT, "migrations", "v172_nac_finding_contain.sql"), encoding="utf-8").read()
    assert "CREATE TABLE IF NOT EXISTS nac_finding_contain" in sql
    i = sql.find("CREATE UNIQUE INDEX IF NOT EXISTS nac_finding_contain_open_fp")
    assert i > 0 and "(token_id, fp) WHERE cleared_at IS NULL" in sql[i:], sql[i:]


def test_the_box_outcome_vocabulary_matches_the_skill_doc():
    """Every word the host accepts from the box is explained to the host model."""
    doc = open(os.path.join(_ROOT, "default-skills", "network.md"), encoding="utf-8").read()
    missing = [w for w in sorted(topology.FINDING_CONTAIN_OUTCOMES) + ["expired"] if f"`{w}`" not in doc]
    assert not missing, f"default-skills/network.md does not explain: {missing}"


def _tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    import re
    print("finding containment — cleared only by the box's answer or its age")
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
