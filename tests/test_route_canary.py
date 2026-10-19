"""The canary on a route switch (runtime/route_canary.py).

Proven here with a stub pool:
  1. Only a disqualifying fault (unknown_name / not_advertised) on a canaried
     role reads the route ledger at all; args faults and ok calls never do.
  2. A fault inside the window on the route under test reverts to the
     previous approved route with set_by='canary' and announces it; the
     window counts only the route's own model's calls; the same fault on a
     different model, past the window, on a route the canary itself set,
     with no previous approved route, with an unreadable count, or after the
     route changed during the check reverts nothing.
  3. A revert the writer refuses is announced as such, and the tripped
     route stays.
  4. state()/summary_line() report an open window, an unknown count, and a
     canary-set route for the routing status.
  5. The ledger hook: record() calls observe() after its INSERT, never on
     a call that was not recorded.

Run: pytest tests/test_route_canary.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "brain"))

from mastercode.runtime import route_canary as C  # noqa: E402

_AT = dt.datetime(2026, 9, 18, 12, 0, tzinfo=dt.timezone.utc)


class _Pool:
    def __init__(self, route=None, calls=5, prev=None, route_after=None):
        self.route, self.calls = route, calls
        self.prev = prev
        self.route_after = route_after      # what the re-read sees (None = same)
        self.reads = []
        self.count_args = None

    async def fetchrow(self, sql, *args):
        self.reads.append(sql)
        if "FROM model_route_log" in sql and "LIMIT 1" in sql:
            n = sum(1 for s in self.reads if "FROM model_route_log" in s and "LIMIT 1" in s)
            if n >= 2 and self.route_after is not None:
                return self.route_after
            return self.route
        return None

    async def fetchval(self, sql, *args):
        self.reads.append(sql)
        self.count_args = args
        return self.calls

    async def fetch(self, sql, *args):
        self.reads.append(sql)
        return self.prev or []


def _route(provider="anthropic", model="claude-x", set_by="user", rid=9):
    return {"id": rid, "at": _AT, "provider": provider, "model": model, "set_by": set_by}


def _prev_rows():
    return [{"provider": "anthropic", "model": "claude-x", "effort": None, "fallback_provider": None,
             "fallback_model": None, "at": _AT, "set_by": "user", "id": 9},
            {"provider": "kimi", "model": "k3", "effort": "high", "fallback_provider": None,
             "fallback_model": None, "at": _AT - dt.timedelta(days=1), "set_by": "user", "id": 8}]


@pytest.fixture
def applied(monkeypatch):
    from server.routes import inference as inf
    out = {"applied": [], "notes": [], "refuse": False}

    async def _apply_role_route(body, *, set_by="user", decision_id=None, rejected=None):
        if out["refuse"]:
            from fastapi import HTTPException
            raise HTTPException(409, "no machine on your network is serving 'k3' right now")
        out["applied"].append((body.role, body.provider, body.model, body.effort, set_by))
        out["rejected"] = rejected
        return {"set": True}
    monkeypatch.setattr(inf, "apply_role_route", _apply_role_route)

    async def _announce(rec):
        out["notes"].append(rec)
    monkeypatch.setattr(C, "_announce", _announce)
    return out


def _obs(pool, reason="unknown_name", provider="anthropic", model="claude-x", role="chat"):
    return asyncio.run(C.observe(pool, 7, role=role, provider=provider, model=model, reason=reason))


def test_cheap_path_reads_nothing_for_ordinary_calls(applied):
    for reason in ("ok", "error", "args_schema", "bad_args_json", "capability_denied"):
        p = _Pool(route=_route())
        assert _obs(p, reason=reason) is None and p.reads == []
    p = _Pool(route=_route())
    # A role outside CANARIED_ROLES never touches the ledger. chat is the
    # only canaried role now, so consolidation is the real second case.
    assert _obs(p, role="consolidation") is None and p.reads == []
    assert applied["applied"] == []


def test_a_fault_inside_the_window_reverts_and_announces(applied):
    p = _Pool(route=_route(), calls=3, prev=_prev_rows())
    rec = _obs(p)
    assert rec["ok"] is True and rec["reverted_to"] == {"provider": "kimi", "model": "k3"}
    assert rec["reason"] == "unknown_name" and rec["call_index"] == 3 and rec["window"] == C.WINDOW_CALLS
    assert applied["applied"] == [("chat", "kimi", "k3", "high", "canary")]
    # The rejection is written on the canary row itself.
    assert applied["rejected"] == ("anthropic", "claude-x")
    assert applied["notes"] and applied["notes"][0]["ok"] is True
    # The window counts only the route's own model's calls.
    assert p.count_args[3:] == ("anthropic", "claude-x")
    # not_advertised trips it too.
    p = _Pool(route=_route(), calls=1, prev=_prev_rows())
    assert _obs(p, reason="not_advertised")["reason"] == "not_advertised"


def test_no_revert_off_route_past_window_on_canary_route_without_fallback_or_after_a_change(applied):
    # A different model made the call (per-thread pick / failover).
    assert _obs(_Pool(route=_route(), calls=2, prev=_prev_rows()), model="other") is None
    # Past the window.
    assert _obs(_Pool(route=_route(), calls=C.WINDOW_CALLS + 1, prev=_prev_rows())) is None
    # The route the canary itself set is the fallback, not a candidate.
    assert _obs(_Pool(route=_route(set_by="canary"), calls=1, prev=_prev_rows())) is None
    # No previous approved route to go back to.
    assert _obs(_Pool(route=_route(), calls=1, prev=[])) is None
    # An unreadable call count is never a blind revert.
    class _Boom(_Pool):
        async def fetchval(self, sql, *args):
            raise RuntimeError("db down")
    assert _obs(_Boom(route=_route(), calls=1, prev=_prev_rows())) is None
    # The user changed the route while we looked: never overwrite it.
    assert _obs(_Pool(route=_route(), calls=1, prev=_prev_rows(),
                      route_after=_route(provider="openai", model="gpt-x", rid=10))) is None
    assert applied["applied"] == [] and applied["notes"] == []


def test_a_refused_revert_is_announced_and_the_tripped_route_stays(applied):
    applied["refuse"] = True
    rec = _obs(_Pool(route=_route(), calls=2, prev=_prev_rows()))
    assert rec["ok"] is False and "no machine" in rec["error"]
    assert applied["applied"] == []
    assert applied["notes"][0]["ok"] is False


def test_announce_bodies_for_both_outcomes(monkeypatch):
    sent = []

    async def _append(payload):
        sent.append(payload)
        return {"ok": True}
    import types, sys as _sys
    mh = types.ModuleType("server.bridge.memory_handlers")
    mh.handle_inbox_append = _append
    monkeypatch.setitem(_sys.modules, "server.bridge.memory_handlers", mh)
    base = {"role": "chat", "reverted_from": {"provider": "openai", "model": "gpt-x", "set_by": "user"},
            "reverted_to": {"provider": "kimi", "model": "k3"}, "reason": "unknown_name",
            "call_index": 3, "window": C.WINDOW_CALLS}
    asyncio.run(C._announce({**base, "ok": True}))
    asyncio.run(C._announce({**base, "ok": False, "error": "no key"}))
    assert sent[0]["message_kind"] == "system_notification" and sent[0]["payload"]["event"] == "route_canary_revert"
    assert "fresh window" in sent[0]["body"] and "not fit for tool use" in sent[0]["body"]
    assert sent[1]["payload"]["event"] == "route_canary_revert_failed" and "stays" in sent[1]["subject"]


def test_state_and_summary_report_the_window_and_a_canary_set_route():
    st = asyncio.run(C.state(_Pool(route=_route(), calls=4), 7, "chat"))
    assert st["open"] is True and st["calls_since_switch"] == 4 and st["window"] == C.WINDOW_CALLS
    assert "canary open: 4 of" in C.summary_line(st)
    assert asyncio.run(C.state(_Pool(route=_route(), calls=C.WINDOW_CALLS), 7, "chat"))["open"] is False
    st = asyncio.run(C.state(_Pool(route=_route(set_by="canary"), calls=1), 7, "chat"))
    assert st["open"] is False and "the canary reverted this role" in C.summary_line(st)
    class _Boom(_Pool):
        async def fetchval(self, sql, *args):
            raise RuntimeError("db down")
    st = asyncio.run(C.state(_Boom(route=_route(), calls=1), 7, "chat"))
    assert st["open"] is None and "unknown" in C.summary_line(st)
    assert asyncio.run(C.state(_Pool(route=None), 7, "chat")) is None
    assert C.summary_line(None) is None


def test_the_ledger_hook_observes_after_the_insert(monkeypatch):
    from mastercode.runtime import tool_call_ledger as L
    seen = []

    async def _observe(pool, entity_id, **kw):
        seen.append(kw)
    monkeypatch.setattr(C, "observe", _observe)

    class _P:
        def __init__(self):
            self.executed = []

        async def execute(self, sql, *args):
            self.executed.append(sql)
    asyncio.run(L.record(_P(), entity_id=7, lane="host", role="chat", provider="kimi", model="k3",
                         tool="weather_lookup", reason="unknown_name", status="denied"))
    assert seen == [{"role": "chat", "provider": "kimi", "model": "k3", "reason": "unknown_name"}]

    class _Fail(_P):
        async def execute(self, sql, *args):
            raise RuntimeError("no table")
    seen.clear()
    asyncio.run(L.record(_Fail(), entity_id=7, lane="host", role="chat", provider="kimi", model="k3",
                         tool="t", reason="unknown_name", status="denied"))
    assert seen == [], "no row, no canary — the canary never runs on a call that was not recorded"
