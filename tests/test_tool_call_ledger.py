"""tool_call_log — one row per tool call, with the served model and the gate's
reason (runtime/tool_call_ledger.py, migrations/v157).

What is proven here, against stub pools and the REAL gates:
  1. The reason vocabulary is ONE list in three places — the module, the
     migration's CHECK, init-db.sql's CHECK — and they agree. A reason added
     to one and not the others is the class of drift that turns a count into
     silence (the writer swallows the CHECK failure).
  2. classify() reads the gate's stamp first, the status second, and an
     unparseable arguments string beats everything.
  3. schema_errors() reads the tool's own schema and reports only missing
     required fields; an unknown tool is not a schema finding.
  4. The real gates stamp: _authorize_meta_tool on a name no registry knows
     → unknown_name; _execute_meta_tool on a real tool in the wrong surface
     → surface_denied. These are the two counts the admission bench reads.
  5. record() writes the declared shape and never raises; rollup() folds rows
     into per-(role, provider, model) counts with model_fault summed over
     exactly MODEL_FAULT_REASONS.

Run: pytest tests/test_tool_call_ledger.py
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "brain"))

from mastercode.runtime import tool_call_ledger as L  # noqa: E402


class _Pool:
    """Just enough of asyncpg: records every execute, answers fetch from a
    per-table script."""

    def __init__(self, fetch_rows=None):
        self.executed: list[tuple[str, tuple]] = []
        self._fetch_rows = fetch_rows or {}

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return None

    async def fetch(self, sql, *args):
        for table, rows in self._fetch_rows.items():
            if f"FROM {table}" in sql:
                return rows
        return []

    async def fetchrow(self, sql, *args):
        return None


def _check_list(sql_text: str) -> list[str]:
    m = re.search(r"reason\s+TEXT\s+NOT NULL CHECK \(reason IN \((.*?)\)\)", sql_text, re.S)
    assert m, "reason CHECK not found"
    return re.findall(r"'([a-z_]+)'", m.group(1))


# ── 1. one vocabulary, three places ──────────────────────────────────────────

def test_reason_vocabulary_matches_migration_and_init_db():
    mig = (_REPO / "migrations" / "v157_tool_call_log.sql").read_text(encoding="utf-8")
    init = (_REPO / "init-db.sql").read_text(encoding="utf-8")
    assert sorted(_check_list(mig)) == sorted(L.REASONS)
    assert sorted(_check_list(init)) == sorted(L.REASONS)
    assert L.MODEL_FAULT_REASONS <= set(L.REASONS)
    # The bench's headline set is exactly the model-side misses.
    assert L.MODEL_FAULT_REASONS == {"unknown_name", "not_advertised", "bad_args_json", "args_schema"}


# ── 2. classify ──────────────────────────────────────────────────────────────

def test_classify_reads_the_stamp_first():
    assert L.classify({"status": "denied", "reason": "unknown_name"}) == "unknown_name"
    assert L.classify({"status": "error", "reason": "not_advertised"}) == "not_advertised"
    assert L.classify({"status": "denied", "reason": "surface_denied"}) == "surface_denied"
    # A stamp outside the vocabulary is ignored, not trusted.
    assert L.classify({"status": "error", "reason": "made_up"}) == "error"


def test_classify_falls_back_to_status():
    from mastercode.runtime.agent_grant import CAP_DENIAL_KEY
    assert L.classify({"status": "ok"}) == "ok"
    assert L.classify({"status": "error"}) == "error"
    assert L.classify({"status": "blocked"}) == "blocked"
    assert L.classify({"status": "error", "dedup_block": True}) == "dedup"
    assert L.classify({"status": "denied", CAP_DENIAL_KEY: 4}) == "capability_denied"
    assert L.classify({"status": "denied", CAP_DENIAL_KEY: 0}) == "policy_denied"
    assert L.classify({"status": "denied"}) == "policy_denied"
    # 'denied_out_of_grant' is a tool_actions result_status, never an envelope
    # status — the grant walls stamp reason='out_of_grant' explicitly.
    assert L.classify({"status": "denied_out_of_grant"}) == "policy_denied"
    # A status that is not a failure word
    # is an outcome, never an error — a started agent is not a platform fault.
    assert L.classify({"status": "dispatched", "task_id": 7}) == "ok"
    assert L.classify({"status": "scheduled"}) == "ok"
    assert L.classify({"status": "not_found", "result": "No state"}) == "ok"
    assert L.classify({"status": "unavailable"}) == "error"
    assert L.fault_class("error") == "platform" and L.fault_class("dedup") == "model"
    assert L.classify({"status": "denied", "reason": "out_of_grant"}) == "out_of_grant"
    assert L.classify("not a dict") == "ok"


def test_bad_json_beats_everything_and_schema_only_counts_when_executed():
    assert L.classify({"status": "ok"}, args_parsed=False) == "bad_args_json"
    assert L.classify({"status": "denied", "reason": "unknown_name"}, args_parsed=False) == "bad_args_json"
    assert L.classify({"status": "ok"}, schema_missing=["missing required field 'x'"]) == "args_schema"
    assert L.classify({"status": "error"}, schema_missing=["m"]) == "args_schema"
    # A denied call never reached an executor — its arguments were not the problem.
    assert L.classify({"status": "denied", "reason": "capability_denied"}, schema_missing=["m"]) == "capability_denied"


# ── 3. schema_errors against a REAL schema ───────────────────────────────────

def test_schema_errors_reads_the_tools_own_required_list():
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
    name, required = None, None
    for schema in PUBLIC_TOOL_SCHEMAS.values():
        fn = schema.get("function") or {}
        req = ((fn.get("parameters") or {}).get("required") or [])
        if fn.get("name") and len(req) >= 2:
            name, required = fn["name"], req
            break
    assert name, "no public tool with >=2 required fields — pick another fixture"
    errs = L.schema_errors(name, {})
    assert len(errs) == len(required)
    assert all("missing required field" in e for e in errs)
    assert L.schema_errors(name, {k: 1 for k in required}) == []
    assert L.schema_errors(name, "not an object") == ["arguments must be an object, got str"]
    assert L.schema_errors("definitely_not_a_tool_xyz", {}) == []


# ── 4. the real gates stamp ──────────────────────────────────────────────────

def test_capability_gate_stamps_unknown_name():
    from mastercode.runtime.tools import _authorize_meta_tool
    pool = _Pool()
    ok, env = asyncio.run(_authorize_meta_tool("definitely_not_a_tool_xyz", {}, 1, pool, None))
    assert ok is False
    assert env["status"] == "denied"
    assert env["reason"] == "unknown_name"
    assert L.classify(env) == "unknown_name"
    # The denial is the stamp itself; no second audit row (the behavioral log
    # went 2026-10-17 — tool_call_log is the one record of a tool call).
    assert not any("entity_behavioral_log" in sql for sql, _ in pool.executed)


def test_surface_gate_stamps_surface_denied():
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
    from mastercode.runtime.tools import _execute_meta_tool
    name = None
    for schema in PUBLIC_TOOL_SCHEMAS.values():
        surfaces = (schema.get("_meta") or {}).get("surfaces")
        if surfaces == ["workbench"]:
            name = schema["function"]["name"]
            break
    assert name, "no workbench-only tool in the registry — pick another fixture"
    pool = _Pool()
    env = asyncio.run(_execute_meta_tool(name, {}, 1, pool, interaction_context="chat"))
    assert env["status"] == "denied"
    assert env["reason"] == "surface_denied"
    assert L.classify(env) == "surface_denied"
    # Refused BEFORE the capability gate: no decision row was written.
    assert not any("entity_behavioral_log" in sql for sql, _ in pool.executed)


# ── 5. record + rollup ───────────────────────────────────────────────────────

def test_record_writes_the_declared_shape_and_never_raises():
    pool = _Pool()
    asyncio.run(L.record(pool, entity_id=7, lane="host", role="chat", provider="kimi",
                         model="k3", tool="recall", reason="unknown_name", status="denied",
                         session_id="s1", latency_ms=12, args={"query": "x" * 3000},
                         schema_missing=["missing required field 'q'"]))
    assert len(pool.executed) == 1
    sql, args = pool.executed[0]
    assert sql.startswith("INSERT INTO tool_call_log")
    assert args[0:9] == (7, "s1", "host", "chat", "kimi", "k3", "recall", "unknown_name", "denied")
    assert args[9] == 12
    assert json.loads(args[10]) == ["missing required field 'q'"]
    assert len(json.loads(args[11])["preview"]) <= L._ARGS_PREVIEW_CAP
    assert args[12] is None  # no project named in the call

    # The project the call named rides as its own column (v237), for the
    # project's activity feed; a value that is not a number is left out.
    pool = _Pool()
    asyncio.run(L.record(pool, entity_id=7, lane="host", role="chat", provider="kimi",
                         model="k3", tool="write", reason="ok", status="ok",
                         args={"project_id": "42", "path": "a.txt"}))
    assert pool.executed[0][1][12] == 42
    pool = _Pool()
    asyncio.run(L.record(pool, entity_id=7, lane="host", role="chat", provider="kimi",
                         model="k3", tool="write", reason="ok", status="ok",
                         args={"project_id": "all"}))
    assert pool.executed[0][1][12] is None


def test_record_never_binds_a_project_id_the_integer_column_refuses():
    """The args are the model's. A project_id wider than INTEGER fails the
    bind (asyncpg: "value out of int32 range"), and record() swallows the
    error, so the whole row, the one audit of the call, would be lost. Such a
    value is left out and the row still lands; so is a bool (int(True) == 1)."""
    for bad in ("99999999999", 2**31, -5, 0, True, 4.5, float("inf"), "1e3", [3]):
        pool = _Pool()
        asyncio.run(L.record(pool, entity_id=7, lane="host", role="chat", provider="kimi",
                             model="k3", tool="write", reason="capability_denied",
                             status="denied", args={"project_id": bad}))
        assert len(pool.executed) == 1, bad
        assert pool.executed[0][1][12] is None, bad
    for good, want in ((42, 42), ("42", 42), (7.0, 7), (2**31 - 1, 2**31 - 1)):
        pool = _Pool()
        asyncio.run(L.record(pool, entity_id=7, lane="host", role="chat", provider="kimi",
                             model="k3", tool="write", reason="ok", status="ok",
                             args={"project_id": good}))
        assert pool.executed[0][1][12] == want, good

    # Outside the vocabulary → recorded as 'error', never a CHECK failure.
    pool = _Pool()
    asyncio.run(L.record(pool, entity_id=7, lane="host", role="chat", provider=None,
                         model=None, tool="t", reason="nonsense", status="error"))
    assert pool.executed[0][1][7] == "error"
    # An unknown lane collapses to host; a None pool is a no-op.
    pool = _Pool()
    asyncio.run(L.record(pool, entity_id=7, lane="weird", role=None, provider=None,
                         model=None, tool="t", reason="ok", status="ok"))
    assert pool.executed[0][1][2] == "host"
    asyncio.run(L.record(None, entity_id=7, lane="host", role=None, provider=None,
                         model=None, tool="t", reason="ok", status="ok"))

    class _Boom(_Pool):
        async def execute(self, sql, *args):
            raise RuntimeError("db down")
    asyncio.run(L.record(_Boom(), entity_id=7, lane="host", role=None, provider=None,
                         model=None, tool="t", reason="ok", status="ok"))


def test_rollup_folds_reasons_and_joins_the_inference_ledger():
    # Since the three-role cut both writers stamp role 'chat' and only `lane`
    # tells them apart, so the host and its dispatched agents land on the SAME
    # (role, provider, model). Lane is part of the key for exactly that reason.
    rows = [
        {"lane": "host", "role": "chat", "provider": "kimi", "model": "k3", "reason": "ok", "n": 40},
        {"lane": "host", "role": "chat", "provider": "kimi", "model": "k3", "reason": "unknown_name", "n": 3},
        {"lane": "host", "role": "chat", "provider": "kimi", "model": "k3", "reason": "args_schema", "n": 2},
        {"lane": "host", "role": "chat", "provider": "kimi", "model": "k3", "reason": "capability_denied", "n": 5},
        {"lane": "agent", "role": "chat", "provider": "anthropic", "model": "claude-opus-5", "reason": "ok", "n": 9},
    ]
    irows = [
        {"model_id": "k3", "provider": "kimi", "calls": 60, "errors": 4,
         "avg_latency_ms": 2100.4, "est_cost_usd": 0.4321},
        {"model_id": "gpt-x", "provider": "openai", "calls": 3, "errors": 0,
         "avg_latency_ms": 900, "est_cost_usd": 0.01},
    ]
    # Same role, same model, other lane — a dispatched agent on the host's own
    # route, inventing tool names the host itself is not.
    rows.append({"lane": "agent", "role": "chat", "provider": "kimi", "model": "k3", "reason": "ok", "n": 4})
    rows.append({"lane": "agent", "role": "chat", "provider": "kimi", "model": "k3", "reason": "unknown_name", "n": 6})
    pool = _Pool(fetch_rows={"tool_call_log": rows, "inference_cost_log": irows})
    out = asyncio.run(L.rollup(pool, 7, days=7))
    assert out["window_days"] == 7
    by = {(m["role"], m["model"], m["lane"]): m for m in out["models"]}
    k3 = by[("chat", "k3", "host")]
    assert k3["tool_calls"] == 50
    # model_fault is exactly the model-side misses; the capability wall is not one.
    assert k3["model_fault"] == 5
    assert k3["reasons"]["capability_denied"] == 5
    # The agent's calls on the SAME model stay their own row. Folded together
    # the host would read 11 faults over 60 calls and have no way to see that
    # its own turns account for 5 of them and its agents for 6.
    k3_agent = by[("chat", "k3", "agent")]
    assert k3_agent["tool_calls"] == 10 and k3_agent["model_fault"] == 6
    assert k3["lane"] == "host" and k3_agent["lane"] == "agent"
    # Inference counts live in their OWN per-model list, never on a role row:
    # k3 serves two roles here and its $0.4321 must appear exactly once.
    assert "inference" not in k3
    inf = {(i["provider"], i["model"]): i for i in out["inference"]}
    assert len(out["inference"]) == 2
    assert inf[("kimi", "k3")] == {"provider": "kimi", "model": "k3", "calls": 60, "errors": 4,
                                   "avg_latency_ms": 2100, "est_cost_usd": 0.4321}
    assert inf[("openai", "gpt-x")]["errors"] == 0
    assert sum(i["est_cost_usd"] for i in out["inference"]) == pytest.approx(0.4421)
    # Busiest first.
    assert out["models"][0]["model"] == "k3"
    assert out["inference"][0]["model"] == "k3"
    # A None pool is an empty rollup, not an error.
    empty = asyncio.run(L.rollup(None, 7))
    assert empty["models"] == [] and empty["inference"] == []


def test_record_later_runs_beside_the_caller_and_flushes():
    async def _go():
        pool = _Pool()
        t = L.record_later(pool, entity_id=7, lane="agent", role="chat", provider="kimi",
                           model="k3", tool="recall", reason="ok", status="ok")
        assert t is not None
        # Returned before the row landed — that is the whole point.
        assert pool.executed == []
        await L.flush_pending()
        assert len(pool.executed) == 1 and pool.executed[0][1][2] == "agent"
        assert L.record_later(None, entity_id=7, lane="host", role=None, provider=None,
                              model=None, tool="t", reason="ok", status="ok") is None
    asyncio.run(_go())
    # No running loop: nothing scheduled, nothing raised.
    assert L.record_later(_Pool(), entity_id=7, lane="host", role=None, provider=None,
                          model=None, tool="t", reason="ok", status="ok") is None


def test_known_tool_schema_covers_the_platform_registry():
    from mastercode.public_tool_registry import OPERATOR_PLATFORM_TOOL_SCHEMAS
    from mastercode.runtime.tools import _find_tool_schema
    names = [(s.get("function") or {}).get("name") for s in OPERATOR_PLATFORM_TOOL_SCHEMAS.values()]
    names = [n for n in names if n]
    assert names, "platform registry is empty — pick another fixture"
    # A platform-only tool is unknown to the surface gate's lookup and known here.
    only = [n for n in names if _find_tool_schema(n) is None]
    assert only, "every platform tool is also public — the extra walk proves nothing"
    assert L.known_tool_schema(only[0]) is not None
    assert L.known_tool_schema("definitely_not_a_tool_xyz") is None


def test_known_tool_schema_covers_the_host_orchestration_verbs():
    """dispatch / operate / recall / agent_status / escalate are built inline
    by session.py and live in no registry; they are the busiest tools on the
    surface, so schema_errors must see them."""
    from mastercode.runtime.session import _build_host_orchestration_tools
    verbs = [(s.get("function") or {}).get("name") for s in _build_host_orchestration_tools()]
    assert "operate" in verbs and "dispatch" in verbs and "recall" in verbs
    for v in verbs:
        assert L.known_tool_schema(v) is not None, v
    # operate declares required fields; a call missing them is an args_schema miss.
    op_req = (L.known_tool_schema("operate")["function"]["parameters"].get("required") or [])
    assert op_req, "operate declares no required fields — fixture changed"
    errs = L.schema_errors("operate", {})
    assert len(errs) == len(op_req)
    assert L.schema_errors("operate", {k: "x" for k in op_req}) == []
