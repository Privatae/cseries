"""Route changes as proposals (runtime/route_change.py, the one host door
tools._host_route_write, inference.apply_role_route + model_route_log v158,
tickets.decide_ticket_service applying an approved proposal).

Proven here without a database:
  1. classify(): unrouted applies, identical is a no-op, the ONE previously
     approved route reverts free, everything else proposes; effort/fallback
     count only when named; openrouter/X and local/X are one route.
  2. The set_by vocabulary is one list in the module, the migration's CHECK
     and init-db.sql's CHECK; only user/decision rows count as approved.
  3. previous_approved_route() returns the newest user/decision row that is
     not the current route, and a cold table as None.
  4. The host door: applies for an unrouted role (host), no-ops on the
     route in force, reverts free (host_revert), files a user-only,
     no-pattern ticket for a working role, and REFUSES (writes nothing,
     proposes nothing) when the routing table cannot be read. Both host
     tools — model_routing op='set' and model_route op='switch' — take it.
  5. Deciding that ticket: the user's 'apply' writes the route
     set_by='decision' with the ticket id; 'keep' writes nothing; the HOST
     deciding it is refused; and_similar can never crystallise it.

Run: pytest tests/test_route_change.py
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

from mastercode.runtime import route_change as RC  # noqa: E402


# ── 1. classify ──────────────────────────────────────────────────────────────

def test_classify_verbs():
    req = {"provider": "kimi", "model": "k3"}
    assert RC.classify(None, req, previous_approved=None) == RC.APPLY_UNROUTED
    assert RC.classify({"unconfigured": "no route"}, req, previous_approved={"provider": "a", "model": "b"}) == RC.APPLY_UNROUTED
    cur = {"provider": "kimi", "model": "k3", "effort": "high"}
    assert RC.classify(cur, req, previous_approved=None) == RC.UNCHANGED
    assert RC.classify(cur, {"provider": "Kimi", "model": "K3"}, previous_approved=None) == RC.UNCHANGED
    # A named effort/fallback that differs is a change; an omitted one is not.
    assert RC.classify(cur, {**req, "effort": "low"}, previous_approved=None) == RC.PROPOSE
    assert RC.classify(cur, {**req, "effort": "high"}, previous_approved=None) == RC.UNCHANGED
    assert RC.classify(cur, {**req, "fallback_provider": "openai"}, previous_approved=None) == RC.PROPOSE
    other = {"provider": "anthropic", "model": "claude-opus-5"}
    assert RC.classify(cur, other, previous_approved=None) == RC.PROPOSE
    # Only THE previously approved route reverts free — not any approved-ever.
    assert RC.classify(cur, other, previous_approved={"provider": "anthropic", "model": "claude-opus-5"}) == RC.APPLY_REVERT
    assert RC.classify(cur, other, previous_approved={"provider": "openai", "model": "gpt-x"}) == RC.PROPOSE
    # The resolver's legacy key spelling still reads as routed.
    assert RC.classify({"provider": "kimi", "model_id": "k3"}, other, previous_approved=None) == RC.PROPOSE


def test_openrouter_and_local_are_one_route():
    # The writer flips openrouter/X to local/X when the user's own machine
    # serves X — so a request for the openrouter spelling of the route in
    # force is a no-op, not a decision card for nothing.
    assert RC.same_route({"provider": "local", "model": "qwen3.5:9b"},
                         {"provider": "openrouter", "model": "qwen3.5:9b"})
    assert not RC.same_route({"provider": "local", "model": "qwen3.5:9b"},
                             {"provider": "kimi", "model": "qwen3.5:9b"})
    assert RC.classify({"provider": "local", "model": "qwen3.5:9b"},
                       {"provider": "openrouter", "model": "qwen3.5:9b"}, previous_approved=None) == RC.UNCHANGED


def test_proposal_prompt_quotes_the_hosts_reason_as_the_hosts():
    s = RC.proposal_prompt("review", {"provider": "kimi", "model": "k3"},
                           {"provider": "anthropic", "model": "claude-opus-5", "effort": "high"},
                           "review invented 3 tools in 40 calls this week")
    assert s.startswith("Move the review role from kimi / k3 to anthropic / claude-opus-5 (effort high)?")
    assert "Your host's reason:" in s
    assert RC.proposal_prompt("chat", None, {"provider": "a", "model": "b"}, None) == \
        "Move the chat role from no route to a / b?"


# ── 2. one set_by vocabulary ────────────────────────────────────────────────

def _check_list(sql_text: str) -> list[str]:
    m = re.search(r"set_by\s+TEXT\s+NOT NULL\s+CHECK \(set_by IN \((.*?)\)\)", sql_text, re.S)
    assert m, "set_by CHECK not found"
    return re.findall(r"'([a-z_]+)'", m.group(1))


def test_set_by_vocabulary_matches_migration_and_init_db():
    from server.routes import inference as inf
    mig = (_REPO / "migrations" / "v158_model_route_log.sql").read_text(encoding="utf-8")
    init = (_REPO / "init-db.sql").read_text(encoding="utf-8")
    assert sorted(_check_list(mig)) == sorted(inf.ROUTE_SET_BY)
    assert sorted(_check_list(init)) == sorted(inf.ROUTE_SET_BY)
    assert set(RC.APPROVED_SET_BY) == {"user", "decision"}
    assert set(RC.APPROVED_SET_BY) < set(inf.ROUTE_SET_BY)
    assert "canary" in inf.ROUTE_SET_BY and "canary" not in RC.APPROVED_SET_BY


# ── 3. the ledger read ──────────────────────────────────────────────────────

class _Pool:
    def __init__(self, rows=None, boom: bool = False):
        self.rows, self.boom = rows or [], boom
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        if self.boom:
            raise RuntimeError("relation does not exist")
        self.executed.append((sql, args))
        # Two reads: the approvals (user/decision) and the canary rows.
        if "set_by = 'canary'" in sql:
            return [r for r in self.rows if r.get("set_by") == "canary"]
        return [r for r in self.rows if r.get("set_by") in ("user", "decision")]

    async def fetchrow(self, sql, *args):
        self.executed.append((sql, args))
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def _row(provider, model, at, set_by="user", rejected=None):
    return {"provider": provider, "model": model, "effort": None,
            "fallback_provider": None, "fallback_model": None, "at": at, "set_by": set_by, "id": at,
            "rejected_provider": rejected[0] if rejected else None,
            "rejected_model": rejected[1] if rejected else None}


def test_previous_approved_route_is_one_step_back():
    from server.routes import inference as inf
    rows = [_row("kimi", "k3", 3), _row("anthropic", "claude-opus-5", 2), _row("openai", "gpt-x", 1)]
    p = _Pool(rows=rows)
    prev = asyncio.run(inf.previous_approved_route(p, 7, "review", "kimi", "k3"))
    assert prev["provider"] == "anthropic" and prev["model"] == "claude-opus-5"
    sql, args = p.executed[0]
    assert "FROM model_route_log" in sql and "set_by IN ('user', 'decision')" in sql
    assert args == (7, "review")
    assert any("set_by = 'canary'" in s for s, _ in p.executed)
    # Host rows never count as approved.
    rows_h = [_row("kimi", "k3", 3), _row("anthropic", "claude-opus-5", 2, set_by="host"),
              _row("openai", "gpt-x", 1)]
    assert asyncio.run(inf.previous_approved_route(_Pool(rows=rows_h), 7, "review", "kimi", "k3"))["model"] == "gpt-x"


def test_a_canary_rejection_retires_the_rejected_routes_approval():
    """user sets A, then B; B trips the canary (canary row → A). B's older
    approval no longer counts: the host cannot revert to B for free."""
    from server.routes import inference as inf
    rows = [_row("anthropic", "claude-opus-5", 4, set_by="canary",
                 rejected=("openai", "gpt-x")),                         # reverted from B to A
            _row("openai", "gpt-x", 3),                                 # B (user) — rejected
            _row("anthropic", "claude-opus-5", 2),                      # A (user)
            _row("kimi", "k3", 1)]                                      # older
    # Current route is A: the previous approved route skips B and lands on k3.
    prev = asyncio.run(inf.previous_approved_route(_Pool(rows=rows), 7, "chat", "anthropic", "claude-opus-5"))
    assert prev["model"] == "k3"
    # The user re-approving B (a newer user row) restores it.
    rows2 = [_row("openai", "gpt-x", 5)] + rows
    prev = asyncio.run(inf.previous_approved_route(_Pool(rows=rows2), 7, "chat", "anthropic", "claude-opus-5"))
    assert prev["model"] == "gpt-x"
    # Current route not in the ledger (host-set) → newest approved row, which
    # here is the re-approved B.
    assert asyncio.run(inf.previous_approved_route(_Pool(rows=rows2), 7, "chat", "local", "x"))["model"] == "gpt-x"
    # The rejection is READ off the canary row, never inferred from the row
    # beside it: a user apply that interleaved between B and the canary row
    # is not blamed, and B stays retired.
    rows3 = [_row("anthropic", "claude-opus-5", 5, set_by="canary", rejected=("openai", "gpt-x")),
             _row("kimi", "k3", 4),                                     # interleaved user apply
             _row("openai", "gpt-x", 3), _row("anthropic", "claude-opus-5", 2)]
    prev = asyncio.run(inf.previous_approved_route(_Pool(rows=rows3), 7, "chat", "anthropic", "claude-opus-5"))
    assert prev["model"] == "k3"
    # Same-tick tie: an approval written in the same instant as the revert
    # but AFTER it (higher id) is not retired.
    same_tick = [{**_row("openai", "gpt-x", 5), "id": 51},
                 {**_row("anthropic", "claude-opus-5", 5, set_by="canary", rejected=("openai", "gpt-x")), "id": 50},
                 _row("openai", "gpt-x", 3), _row("anthropic", "claude-opus-5", 2)]
    prev = asyncio.run(inf.previous_approved_route(_Pool(rows=same_tick), 7, "chat", "anthropic", "claude-opus-5"))
    assert prev["model"] == "gpt-x"


def test_the_ledger_row_carries_the_rejection(monkeypatch):
    from server.routes import inference as inf
    p = _Pool()
    asyncio.run(inf._log_route_apply(p, 7, "chat", {"provider": "kimi", "model": "k3"}, "canary", None,
                                     rejected=("openai", "gpt-x")))
    sql, args = p.executed[0]
    assert "rejected_provider, rejected_model" in sql and args[-2:] == ("openai", "gpt-x")
    p = _Pool()
    asyncio.run(inf._log_route_apply(p, 7, "chat", {"provider": "kimi", "model": "k3"}, "user", None))
    assert p.executed[0][1][-2:] == (None, None)
    assert asyncio.run(inf.previous_approved_route(_Pool(rows=[]), 7, "review", "kimi", "k3")) is None
    assert asyncio.run(inf.previous_approved_route(_Pool(boom=True), 7, "review", "a", "b")) is None


# ── 4. the host door ────────────────────────────────────────────────────────

@pytest.fixture
def door(monkeypatch):
    """Stub the routing read, the ledger read, the writer and the ticket
    filing so both host tools can be driven end to end without a database."""
    import sys as _sys
    import types as _types
    from server.routes import inference as inf
    from server.routes import tickets as tk
    from mastercode.runtime import tools as T

    calls: dict = {"applied": [], "tickets": []}
    state = {"effective": {}, "stored": {}, "prev": None, "boom": False}

    async def _get_current_routing():
        if state["boom"]:
            raise RuntimeError("db down")
        return {"effective": {"chat": state["effective"]}, "routing": {"chat": state["stored"]},
                "providers": []}

    async def _previous_approved_route(pool, eid, role, cp, cm):
        return state["prev"]

    async def _apply_role_route(body, *, set_by="user", decision_id=None):
        calls["applied"].append((body.role, body.provider, body.model, set_by, decision_id))
        return {"set": True, "role": body.role, "provider": body.provider,
                "model": body.model, "set_by": set_by}

    async def _create_ticket_service(pool, eid, **kw):
        calls["tickets"].append(kw)
        return {"id": 41, "ticket_uuid": "u", "opened_at": None}

    monkeypatch.setattr(inf, "get_current_routing", _get_current_routing)
    monkeypatch.setattr(inf, "previous_approved_route", _previous_approved_route)
    monkeypatch.setattr(inf, "apply_role_route", _apply_role_route)
    monkeypatch.setattr(tk, "create_ticket_service", _create_ticket_service)

    bs = _types.ModuleType("server.brain_service")
    bs._entity_id = 7
    bs.get_pool = lambda: _Pool()
    monkeypatch.setitem(_sys.modules, "server.brain_service", bs)
    import server as _server
    monkeypatch.setattr(_server, "brain_service", bs, raising=False)
    return T, calls, state


def _set(T, **kw):
    return asyncio.run(T._exec_model_routing({"op": "set", **kw}))


def test_door_refuses_the_retired_review_role(door):
    # `review` went 2026-10-20 with the diff reviewer: nothing resolves it, so
    # a route written under it would be a setting nothing reads.
    T, calls, state = door
    state["effective"] = {"provider": "", "model": "", "unconfigured": "no route"}
    out = _set(T, role="review", provider="kimi", model="k3")
    assert out["status"] != "ok" and "invalid role" in str(out.get("result")), out
    assert calls["applied"] == [] and calls["tickets"] == []


def test_door_applies_for_an_unrouted_role(door):
    T, calls, state = door
    state["effective"] = {"provider": "", "model": "", "unconfigured": "no chat route"}
    out = _set(T, role="chat", provider="kimi", model="k3")
    assert out["status"] == "ok" and out["result"]["set"] is True
    assert calls["applied"] == [("chat", "kimi", "k3", "host", None)]
    assert "no route" in out["result"]["why_applied"] and calls["tickets"] == []


def test_door_is_a_noop_on_the_route_in_force(door):
    T, calls, state = door
    state["effective"] = {"provider": "kimi", "model": "k3", "effort": "high"}
    out = _set(T, role="chat", provider="kimi", model="k3")
    assert out["result"]["unchanged"] is True and out["result"]["set"] is False
    assert calls["applied"] == [] and calls["tickets"] == []


def test_door_reverts_free_only_to_the_previous_approved_route(door):
    T, calls, state = door
    state["effective"] = {"provider": "kimi", "model": "k3"}
    state["prev"] = {"provider": "anthropic", "model": "claude-opus-5"}
    out = _set(T, role="chat", provider="anthropic", model="claude-opus-5")
    assert out["result"]["set"] is True
    assert calls["applied"] == [("chat", "anthropic", "claude-opus-5", "host_revert", None)]
    assert "approved before" in out["result"]["why_applied"]
    # A different approved-once route is not a revert: it proposes.
    out2 = _set(T, role="chat", provider="openai", model="gpt-x")
    assert out2["result"]["proposed"] is True and len(calls["applied"]) == 1


def test_door_proposes_a_user_only_no_pattern_ticket_for_a_working_role(door):
    T, calls, state = door
    state["effective"] = {"provider": "kimi", "model": "k3"}
    out = _set(T, role="chat", provider="anthropic", model="claude-opus-5",
               reason="review invented 3 tools in 40 calls this week")
    r = out["result"]
    assert r["proposed"] is True and r["set"] is False and r["ticket_id"] == 41
    assert r["from"] == {"provider": "kimi", "model": "k3"}
    assert r["to"] == {"provider": "anthropic", "model": "claude-opus-5"}
    assert calls["applied"] == []
    tk = calls["tickets"][0]
    assert tk["source"] == "runtime"
    assert [o["id"] for o in tk["options"]] == ["apply", "keep"]
    md = tk["metadata"]
    assert md["user_only"] is True and md["no_pattern"] is True
    prop = md["route_change_proposal"]
    assert prop["route"] == {"role": "chat", "provider": "anthropic", "model": "claude-opus-5",
                             "effort": None, "fallback_provider": None}
    assert prop["current"]["model"] == "k3" and prop["reason"].startswith("review invented")
    assert "Your host's reason" in tk["sitrep"]
    # The note states the fact the ticket's user_only lock enforces, and that
    # work which does not wait on the card goes on: not a script.
    assert "locked to them" in r["note"] and "apply" in r["note"]
    assert "goes on meanwhile" in r["note"] and "end your turn" not in r["note"]


def test_door_fallback_only_card_says_it_sets_a_fallback_and_keeps_the_effort(door):
    # The Host offering a fallback after a safety-filter decline (skill
    # 'routing'): the same route plus a fallback. The card's button says
    # what 'apply' does, and the applied route keeps the effort in force —
    # the writer replaces the whole entry.
    T, calls, state = door
    state["effective"] = {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "high"}
    out = _set(T, role="chat", provider="anthropic", model="claude-opus-5-5",
               fallback_provider="anthropic", fallback_model="claude-opus-4-8",
               reason="declined twice today (category cyber)")
    assert out["result"]["proposed"] is True and calls["applied"] == []
    tk = calls["tickets"][0]
    apply_opt, keep_opt = tk["options"]
    assert apply_opt["label"] == "Set chat fallback to anthropic / claude-opus-4-8", apply_opt
    assert "Move" not in apply_opt["label"] and "stays on anthropic / claude-opus-5-5" in apply_opt["consequence"]
    assert keep_opt["label"] == "Keep chat as it is"
    assert tk["sitrep"].startswith("Keep the chat role on anthropic / claude-opus-5-5 and set its "
                                   "fallback to anthropic / claude-opus-4-8?"), tk["sitrep"]
    route = tk["metadata"]["route_change_proposal"]["route"]
    assert route["effort"] == "high" and route["fallback_model"] == "claude-opus-4-8", route


def test_carry_unchanged_is_for_the_same_route_only():
    cur = {"provider": "kimi", "model": "k3", "effort": "high", "reasoning_inline": True,
           "fallback_provider": "openrouter", "fallback_model": "k3-mini"}
    # An effort change keeps the fallback and the inline flag.
    got = RC.carry_unchanged(cur, {"provider": "kimi", "model": "k3", "effort": "low"})
    assert got["effort"] == "low" and got["fallback_provider"] == "openrouter"
    assert got["fallback_model"] == "k3-mini" and got["reasoning_inline"] is True
    # A new fallback provider does not inherit the old fallback model.
    got = RC.carry_unchanged(cur, {"provider": "kimi", "model": "k3", "fallback_provider": "openai"})
    assert got["fallback_provider"] == "openai" and not got.get("fallback_model")
    # Another route keeps nothing of this one.
    got = RC.carry_unchanged(cur, {"provider": "openai", "model": "gpt-x"})
    assert got == {"provider": "openai", "model": "gpt-x"}


def test_a_move_card_still_says_move():
    opts = RC.proposal_options("chat", {"provider": "kimi", "model": "k3"},
                               {"provider": "anthropic", "model": "claude-opus-5",
                                "fallback_provider": "openrouter"})
    assert opts[0]["label"] == "Move chat to anthropic / claude-opus-5" and opts[1]["label"] == "Keep kimi / k3"


def test_door_fails_closed_when_the_table_cannot_be_read(door):
    T, calls, state = door
    state["boom"] = True
    out = _set(T, role="chat", provider="anthropic", model="claude-opus-5")
    assert out["status"] == "error" and "could not read the routing table" in out["result"]
    assert calls["applied"] == [] and calls["tickets"] == []


def test_door_trusts_the_stored_entry_when_the_resolver_hiccups(door):
    T, calls, state = door
    # The resolver said "unconfigured" for a role that IS stored — never
    # treat that as unrouted and overwrite it.
    state["effective"] = {"source": "unconfigured"}
    state["stored"] = {"provider": "kimi", "model": "k3"}
    out = _set(T, role="chat", provider="anthropic", model="claude-opus-5")
    assert out["result"]["proposed"] is True and calls["applied"] == []


def test_model_route_switch_takes_the_same_door(door):
    T, calls, state = door
    state["effective"] = {"provider": "kimi", "model": "k3"}
    out = asyncio.run(T._exec_model_route({"op": "switch", "role": "chat",
                                           "provider": "anthropic", "model_id": "claude-opus-5"}))
    assert out["result"]["proposed"] is True and calls["applied"] == []
    state["effective"] = {"provider": "", "model": ""}
    out = asyncio.run(T._exec_model_route({"op": "switch", "role": "chat",
                                           "provider": "kimi", "model_id": "k3"}))
    assert calls["applied"] == [("chat", "kimi", "k3", "host", None)]


# ── 5. deciding the ticket ──────────────────────────────────────────────────

class _TicketPool:
    def __init__(self, md):
        self.md = md
        self.executed = []

    async def fetchrow(self, sql, *args):
        if "FROM entity_tickets" in sql:
            return {"id": 41, "circle_id": None, "status": "open",
                    "options": json.dumps([{"id": "apply", "label": "Move"}, {"id": "keep", "label": "Keep"}]),
                    "metadata": json.dumps(self.md)}
        if "INSERT INTO decision_patterns" in sql:
            return {"id": 9}
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        # The compare-and-set: the first decide flips the row, a second finds
        # nothing open to flip.
        if "UPDATE entity_tickets" in sql and "status = 'open'" in sql:
            n = sum(1 for s, _ in self.executed if "UPDATE entity_tickets" in s)
            return "UPDATE 1" if n == 1 else "UPDATE 0"
        return "UPDATE 1"


@pytest.fixture
def decide_env(monkeypatch):
    from server.routes import inference as inf
    from server.routes import tickets as tk
    applied = []

    async def _apply_role_route(body, *, set_by="user", decision_id=None):
        applied.append((body.role, body.provider, body.model, body.effort, set_by, decision_id))
        return {"set": True}
    monkeypatch.setattr(inf, "apply_role_route", _apply_role_route)
    return tk, applied


_PROPOSAL_MD = {
    "user_only": True, "no_pattern": True,
    "route_change_proposal": {"role": "chat", "route": {
        "role": "chat", "provider": "anthropic", "model": "claude-opus-5",
        "effort": "high", "fallback_provider": None}},
}


def test_user_apply_writes_the_route_as_decision(decide_env):
    tk, applied = decide_env
    pool = _TicketPool(_PROPOSAL_MD)
    res = asyncio.run(tk.decide_ticket_service(pool, 7, 41, option_id="apply", actor="user"))
    assert res["route_apply"] == {"applied": True}
    assert applied == [("chat", "anthropic", "claude-opus-5", "high", "decision", 41)]
    assert any("UPDATE entity_tickets" in s for s, _ in pool.executed)


def test_user_apply_keeps_a_carried_reasoning_inline(monkeypatch):
    from server.routes import inference as inf
    from server.routes import tickets as tk
    bodies = []

    async def _apply_role_route(body, *, set_by="user", decision_id=None):
        bodies.append(body)
        return {"set": True}
    monkeypatch.setattr(inf, "apply_role_route", _apply_role_route)
    md = {"user_only": True, "no_pattern": True, "route_change_proposal": {"role": "chat", "route": {
        "role": "chat", "provider": "kimi", "model": "k3", "effort": None,
        "fallback_provider": "openrouter", "fallback_model": "k3-mini", "reasoning_inline": True}}}
    res = asyncio.run(tk.decide_ticket_service(_TicketPool(md), 7, 41, option_id="apply", actor="user"))
    assert res["route_apply"] == {"applied": True}
    assert bodies[0].reasoning_inline is True and bodies[0].fallback_model == "k3-mini"


def test_user_keep_writes_nothing(decide_env):
    tk, applied = decide_env
    res = asyncio.run(tk.decide_ticket_service(_TicketPool(_PROPOSAL_MD), 7, 41, option_id="keep", actor="user"))
    assert res["route_apply"] == {"applied": False} and applied == []


def test_host_cannot_decide_a_user_only_ticket(decide_env):
    tk, applied = decide_env
    pool = _TicketPool(_PROPOSAL_MD)
    with pytest.raises(tk.TicketError) as ei:
        asyncio.run(tk.decide_ticket_service(pool, 7, 41, option_id="apply", actor="host"))
    assert ei.value.status == 403
    assert applied == [] and not any("UPDATE entity_tickets" in s for s, _ in pool.executed)
    # An ordinary ticket is still the host's to decide.
    res = asyncio.run(tk.decide_ticket_service(_TicketPool({}), 7, 41, option_id="keep", actor="host"))
    assert res["ticket_id"] == 41 and "route_apply" not in res


def test_host_cannot_dismiss_a_user_only_ticket(decide_env):
    tk, applied = decide_env
    pool = _TicketPool(_PROPOSAL_MD)
    with pytest.raises(tk.TicketError) as ei:
        asyncio.run(tk.dismiss_ticket_service(pool, 7, 41, actor="host"))
    assert ei.value.status == 403
    assert not any("UPDATE entity_tickets" in s for s, _ in pool.executed)
    # The user may close it; an ordinary ticket the host may dismiss.
    assert asyncio.run(tk.dismiss_ticket_service(_TicketPool(_PROPOSAL_MD), 7, 41, actor="user"))["status"] == "dismissed"
    assert asyncio.run(tk.dismiss_ticket_service(_TicketPool({}), 7, 41, actor="host"))["status"] == "dismissed"


def test_a_second_concurrent_decide_loses_and_applies_nothing(decide_env):
    tk, applied = decide_env
    pool = _TicketPool(_PROPOSAL_MD)
    # Both requests read the ticket as open (the pool always says so); only
    # the one whose UPDATE flips the row goes on to apply the route.
    res = asyncio.run(tk.decide_ticket_service(pool, 7, 41, option_id="apply", actor="user"))
    assert res["route_apply"] == {"applied": True}
    with pytest.raises(tk.TicketError) as ei:
        asyncio.run(tk.decide_ticket_service(pool, 7, 41, option_id="apply", actor="user"))
    assert ei.value.status == 409
    assert len(applied) == 1


def test_no_pattern_ticket_never_crystallises(decide_env):
    tk, applied = decide_env
    pool = _TicketPool(_PROPOSAL_MD)
    res = asyncio.run(tk.decide_ticket_service(pool, 7, 41, option_id="apply", and_similar=True, actor="user"))
    assert res["pattern_id"] is None and res["decision"]["and_similar"] is False
    assert not any("decision_patterns" in s for s, _ in pool.executed)


def test_refused_apply_is_reported_not_raised(decide_env, monkeypatch):
    tk, applied = decide_env
    from server.routes import inference as inf

    async def _boom(body, *, set_by="user", decision_id=None):
        from fastapi import HTTPException
        raise HTTPException(409, "no machine on your network is serving that model")
    monkeypatch.setattr(inf, "apply_role_route", _boom)
    res = asyncio.run(tk.decide_ticket_service(_TicketPool(_PROPOSAL_MD), 7, 41, option_id="apply", actor="user"))
    assert res["route_apply"]["applied"] is False and "no machine" in res["route_apply"]["error"]
