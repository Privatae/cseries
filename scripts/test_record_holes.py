#!/usr/bin/env python3
"""Holes in a machine's own record reach the host (v163).

Runs as pytest (`pytest scripts/test_record_holes.py`) AND as a plain script
(`APP_ROOT=<repo> python3 scripts/test_record_holes.py`). No DB, no network:
the narrower and the fold are pure, and the storage half drives the REAL
topology.upsert_heartbeat against a recording pool — the same shape
scripts/test_alerts_summary.py uses, for the same defect class.

WHAT THIS FILE IS DEFENDING. The bastion counts, per shelf, every result it
could not write into its machine store and sends the counts on every beat as
`machine_record_dropped`. The heartbeat writer takes a FIXED column list and
drops what it does not name — the defect v146 fixed for detect and v150 for
alerts — and this field had no reader anywhere on the host. So:

  * the column must be in the INSERT and the value must reach the arguments;
  * three values must stay three: {} is whole, a populated map is holes, NULL
    is not reported — and the update path COALESCEs like its neighbours. (A
    first version overwrote, on the premise that absent meant whole; the
    observe switch in the bastion console made a populated column read as
    whole while the holes persisted.)
  * the ledger must raise ONE row per machine with holes, keyed on the machine,
    and NO row for a machine without.
"""
import asyncio
import json
import os
import sys

_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from mastercode.runtime import oversight as ov      # noqa: E402
from server.network import pairing_grants as pg     # noqa: E402
from server.network import topology                 # noqa: E402


class Pool:
    """Enough asyncpg surface for upsert_heartbeat, recording every statement."""

    def __init__(self):
        self.sql: list = []

    async def execute(self, q, *a):
        self.sql.append((q, a))

    async def fetchval(self, q, *a):
        self.sql.append((q, a))
        if "FROM mcp_client_tokens" in q and "operator_kind = 'bastion'" in q:
            return 1
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
        return []

    def acquire(self):
        pool = self

        class _Cx:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *exc):
                return False
        return _Cx()

    def insert(self):
        for q, a in self.sql:
            if "INSERT INTO network_bastion_heartbeat" in q:
                return q, a
        raise AssertionError("upsert_heartbeat never wrote the heartbeat row")


def _beat(**extra):
    return {"token_id": 7, "hostname": "box", "platform": "linux", **extra}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── storage ─────────────────────────────────────────────────────────────────

def test_a_beat_with_holes_reaches_the_row():
    pool = Pool()
    assert _run(topology.upsert_heartbeat(pool, 3, _beat(
        machine_record_dropped={"observe-host": 3, "findings": 1}))) is not None
    q, args = pool.insert()
    cols = q.split("(", 1)[1].split(")", 1)[0]
    assert "record_dropped" in cols, "`record_dropped` is not in the INSERT column list — dropped on the floor"
    stored = [a for a in args if isinstance(a, str) and '"observe-host"' in a]
    assert stored, f"the narrowed counts never reached the arguments: {args!r}"
    assert json.loads(stored[0]) == {"observe-host": 3, "findings": 1}


def test_the_update_path_preserves_like_its_neighbours_and_stores_whole_as_empty():
    pool = Pool()
    _run(topology.upsert_heartbeat(pool, 3, _beat(machine_record_dropped={"observe-host": 1})))
    q, _ = pool.insert()
    assert "record_dropped  = COALESCE(EXCLUDED.record_dropped, network_bastion_heartbeat.record_dropped)" in q, \
        "an absent value (a pairing no longer entitled to observe) must not erase the last reading"
    # {} from the box is WHOLE and must be stored as {}, never collapsed to NULL.
    pool = Pool()
    _run(topology.upsert_heartbeat(pool, 3, _beat(machine_record_dropped={})))
    _, args = pool.insert()
    assert args[-1] == "{}", f"a whole record was stored as {args[-1]!r}, want '{{}}'"


def test_an_absent_or_malformed_value_is_stored_as_null():
    for beat in (_beat(), _beat(machine_record_dropped="junk"), _beat(machine_record_dropped=[1])):
        pool = Pool()
        assert _run(topology.upsert_heartbeat(pool, 3, beat)) is not None
        q, args = pool.insert()
        assert "record_dropped" in q
        assert args[-1] is None, f"a malformed/absent value was stored as {args[-1]!r}, want NULL"


def test_every_column_lines_up_with_its_argument():
    """THE SWAP. A swap of two adjacent jsonb arguments passed the suite: the
    holes test scanned every argument for its value, and the NULL test
    asserted args[-1] on beats that carried no posture. This zips the parsed
    column list against the argument tuple with a DISTINCT value in every
    jsonb column that can carry one, so a swap anywhere is caught."""
    pool = Pool()
    _run(topology.upsert_heartbeat(pool, 3, _beat(
        machine_record_dropped={"observe-host": 7},
        posture={"ufw": "active"}, findings={"baseline": "ok", "critical": 1},
        alerts={"firing": 2, "critical": 1, "warning": 1, "at": "2026-09-19T10:00:00Z",
                "since": None, "fingerprints": ["fp"]})))
    q, args = pool.insert()
    cols = [c.strip() for c in q.split("(", 1)[1].split(")", 1)[0].split(",")]
    assert cols[-1] == "last_seen_at" and len(cols) == len(args) + 1, (len(cols), len(args))
    by = dict(zip(cols, args))
    assert json.loads(by["record_dropped"]) == {"observe-host": 7}, by["record_dropped"]
    assert json.loads(by["posture"])["ufw"] == "active", by["posture"]
    assert json.loads(by["alerts"])["firing"] == 2, by["alerts"]
    assert json.loads(by["detect"])["baseline"] == "ok", by["detect"]
    assert by["hostname"] == "box"


# ── narrowing ───────────────────────────────────────────────────────────────

def test_narrowing_keeps_the_shapes_the_box_emits_and_refuses_the_rest_whole():
    got = pg.narrow_record_dropped({"observe-host": 3, "observe-alerts": 2.0, "huge": 10 ** 12,
                                    "OBSERVE-K8S": 4, "quiet": 0})
    assert got == {"observe-host": 3, "observe-alerts": 2, "huge": 1_000_000_000,
                   "observe-k8s": 4}, got
    assert pg.narrow_record_dropped({}) == {}, "an empty object is WHOLE and must survive as {}"
    assert pg.narrow_record_dropped({"zero": 0}) == {}, "only-zero counts are the box saying whole"
    assert pg.narrow_record_dropped([1, 2]) is None
    assert pg.narrow_record_dropped(None) is None
    assert pg.narrow_record_dropped("junk") is None


def test_an_unreadable_entry_makes_the_beat_not_reported_never_whole():
    """`{}` wins the COALESCE and clears stored holes. A map the host could not
    read said "I lost results" in a form it cannot use — returning {} for it
    would overwrite real holes with an all-clear. Every such class must come
    back None."""
    for bad in ({"BAD NAME": 5, "../etc": 9}, {"x" * 65: 7},
                {"observe-host": "7", "observe-net": None},
                {"observe-host": float("nan"), "observe-net": float("inf")},
                {"-observe-host": 3}, {"": 4}, {"observe-host": -1},
                {"observe-host": 3, "BAD NAME": 5}):  # one good, one unreadable -> still None
        assert pg.narrow_record_dropped(bad) is None, bad


def test_a_lowercase_collision_keeps_the_larger_count():
    assert pg.narrow_record_dropped({"OBSERVE-K8S": 4, "observe-k8s": 9}) == {"observe-k8s": 9}
    assert pg.narrow_record_dropped({"observe-k8s": 9, "OBSERVE-K8S": 4}) == {"observe-k8s": 9}


def test_nan_and_infinity_do_not_take_the_beat_down():
    """Starlette's request.json accepts NaN and Infinity. int(nan) raises;
    upsert_heartbeat is called unwrapped, so the whole beat would 500 and
    every other field on it be lost. Through the REAL writer: the beat lands,
    and this field is NULL (unreadable -> not reported), never {}."""
    pool = Pool()
    assert _run(topology.upsert_heartbeat(pool, 3, _beat(
        machine_record_dropped={"observe-host": float("nan"), "observe-net": float("inf"),
                                "observe-units": 2}))) is not None
    _, args = pool.insert()
    assert args[-1] is None, f"an unreadable beat stored {args[-1]!r}; {{}} would clear real holes"


def test_narrowing_caps_the_shelf_count_keeping_the_largest():
    many = {f"shelf-{i:03d}": i + 1 for i in range(50)}
    got = pg.narrow_record_dropped(many)
    assert len(got) == 32, len(got)
    assert min(got.values()) == 19, "the cap must keep the largest counts, not the first keys"


# ── the reader ──────────────────────────────────────────────────────────────

class Row(dict):
    """asyncpg Record enough: subscript + .keys()."""


def test_the_reader_selects_the_column_and_reads_it_back():
    """Driven, not grepped: _bastion_row is what list_bastions maps every row
    through (topology.list_bastions has the single call site)."""
    consts = topology.list_bastions.__code__.co_consts
    assert any(isinstance(c, str) and "h.record_dropped" in c for c in consts), \
        "list_bastions does not SELECT h.record_dropped, so nothing can ever read it"
    base = {k: None for k in (
        "id", "name", "url_handle", "scopes", "hardware_fingerprint",
        "machine_anchor_id", "notes", "created_at", "last_used_at", "hostname",
        "gateway_version", "platform", "tunnel_status", "reported_ip",
        "wg_pubkey", "capabilities", "models", "last_seen_at", "freeagent_ok",
        "freeagent_stores", "enforcing", "nac_posture", "nac_decision",
        "schedulers", "scheduler_scan", "peers", "owner_claim", "prep_tools",
        "prep_scan", "mode", "anchor", "pairings",
        "unapproved_pairing_attempts", "other_bastions",
        "other_bastions_scanned_at", "detect", "alerts", "posture", "online")}
    row = topology._bastion_row(Row({**base, "record_dropped": json.dumps({"observe-host": 3})}))
    assert row["record_dropped"] == {"observe-host": 3}
    assert topology._bastion_row(Row({**base, "record_dropped": "{}"}))["record_dropped"] == {}
    # and a row from a database that predates v163 reads as never-reported
    assert topology._bastion_row(Row(dict(base)))["record_dropped"] is None


# ── the fold ────────────────────────────────────────────────────────────────

def _bastion(**extra):
    return {"hostname": "box-1", "last_seen_at": None, "online": True, **extra}


def test_a_machine_with_holes_gets_one_high_row_keyed_on_the_machine():
    out = ov.derive_findings([_bastion(record_dropped={"observe-host": 3, "observe-units": 1})])
    rows = [f for f in out if f["kind"] == "record-holes"]
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["severity"] == "high" and r["machine"] == "box-1"
    assert r["key"] == ov.finding_key("box-1", "record-holes", ""), r["key"]
    assert r["evidence"]["record_dropped"] == {"observe-host": 3, "observe-units": 1}
    assert "4 result(s)" in r["title"], r["title"]
    # Different counts next pass: SAME key, moving evidence — one row, not a pile.
    out2 = ov.derive_findings([_bastion(record_dropped={"observe-host": 9})])
    assert [f for f in out2 if f["kind"] == "record-holes"][0]["key"] == r["key"]


def test_a_machine_without_holes_gets_no_row():
    """{} is whole and None is not reported; neither is a finding."""
    for b in (_bastion(), _bastion(record_dropped=None), _bastion(record_dropped={})):
        assert not [f for f in ov.derive_findings([b]) if f["kind"] == "record-holes"], b


def _tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    import re
    print("record holes reach the host — v163")
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
