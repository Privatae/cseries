#!/usr/bin/env python3
"""Tickets: one system, two shapes.

The join between a Circles ticket (something a PERSON does) and a Workbench
phase (something a BUILD does), in both directions, and the credential-ask
option shape. server/routes/tickets.py and server/applications/escalation.py
are EXECUTED here against a stub pool that answers the exact statements the
services issue; no database, no pod.

THE STUB DOES NOT SUPPLY THE BEHAVIOUR UNDER TEST. Every filter it applies
(status='open', the gate-ticket exclusion, gate='user') is applied only when
the SQL text carries that clause — the first version of this gate filtered on
its own and three mutations survived it. Phase ids are
compared as TEXT, the way the services now compare them.

What it pins, each by an input only that rule can move:
  - a credential ask is shape-checked at create, never carries a value, and
    may not name a key the vault already holds (the vault supersedes on name);
    an unanswerable vault refuses the ask rather than letting it through.
  - the route strips the platform's locks from caller metadata, kind=phase_gate
    included (approval authority never widens to write:tickets).
  - a phase link in metadata must be integers.
  - a gated phase raises ONE open ticket (user_only + no_pattern); asking again
    returns it; a lost insert race adopts the winner's row; an ungated,
    approved, or foreign phase raises none; a phase the user SENT BACK and has
    not changed since raises none and answers with their note.
  - 'approve' on the gate ticket writes approved_at with COALESCE and answers
    any sibling card; 'send back' writes nothing; the host may not answer it.
  - a phase landing 'complete' decides its joined ticket with 'built'; 'failed'
    lands as phase_last; the gate ticket is never mistaken for a joined ticket;
    a user_only joined ticket is left for the user.
  - an approval made in the Workbench decides the dangling gate ticket.
  - the host's `escalate` applies the same ask checks and only joins a phase of
    this host's own project; the tool schema offers the shapes.
  - the build path, the approve route and the Circles card call these —
    source-pinned where there is no container-free execution path.

Run: APP_ROOT=<repo> python3 -B scripts/test_tickets_phase_join.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]

from server.routes import tickets as T  # noqa: E402

FAILED = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global FAILED
    if cond:
        print(f"ok    {label}")
    else:
        FAILED += 1
        print(f"FAIL  {label} {extra}")


class UniqueViolationError(Exception):
    """Named like asyncpg's; the service matches on the class NAME."""


class Pool:
    """asyncpg-shaped stub over an in-memory ticket + phase table. Each SQL is
    matched on its distinguishing fragment; the filters it applies are read
    off the SQL text, never assumed."""

    def __init__(self):
        self.tickets: dict[int, dict] = {}
        self.phases: dict[int, dict] = {}
        self.executed: list[tuple[str, tuple]] = []
        self.fetched: list[tuple[str, tuple]] = []
        self.support_landed_asks: list[tuple] = []
        self.fail_next_insert = False
        self._next = 1
        self._ts = 0

    def clock(self) -> str:
        self._ts += 1
        return f"2026-09-24T00:00:{self._ts:02d}+00:00"

    def add_phase(self, pid: int, *, gate="user", approved_at=None, project_id=9,
                  entity_id=9003, title="Web UI", spec="Frame the bot UI."):
        self.phases[pid] = {"id": pid, "phase_number": 2, "title": title, "spec": spec,
                            "description": "", "gate": gate, "approved_at": approved_at,
                            "updated_at": self.clock(), "project_id": project_id,
                            "entity_id": entity_id, "project_name": "Trading"}

    def add_ticket(self, *, options, metadata, status="open", eid=9003):
        tid = self._next
        self._next += 1
        self.tickets[tid] = {"id": tid, "entity_id": eid, "circle_id": None,
                             "status": status, "options": json.dumps(options),
                             "metadata": json.dumps(metadata), "decision": None,
                             "decided_at": None}
        return tid

    def _open_gate_rows(self, s, args):
        eid, kind, phase_key = args[0], args[1], args[2]
        need_open = "status = 'open'" in s
        out = []
        for t in self.tickets.values():
            md = json.loads(t["metadata"])
            if (t["entity_id"] == eid and (not need_open or t["status"] == "open")
                    and md.get("kind") == kind and str(md.get("phase_id")) == str(phase_key)):
                out.append({"id": t["id"]})
        return out

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        self.fetched.append((s, args))
        if "SELECT id FROM entity_tickets" in s and "metadata->>'kind'" in s:
            rows = self._open_gate_rows(s, args)
            return rows[0] if rows else None
        if "decision->>'option_id' = 'send_back'" in s:
            eid, kind, phase_key = args
            cands = [t for t in self.tickets.values()
                     if t["entity_id"] == eid and t["status"] == "decided"
                     and json.loads(t["metadata"]).get("kind") == kind
                     and str(json.loads(t["metadata"]).get("phase_id")) == str(phase_key)
                     and (t["decision"] or {}).get("option_id") == "send_back"]
            if not cands:
                return None
            t = max(cands, key=lambda r: r["decided_at"])
            return {"id": t["id"], "decided_at": t["decided_at"], "note": t["decision"].get("note")}
        if "FROM project_phases ph JOIN projects p" in s and "p.name AS project_name" in s:
            phase_id, project_id = args
            ph = self.phases.get(phase_id)
            return dict(ph) if ph and ph["project_id"] == project_id else None
        if "INSERT INTO entity_tickets" in s:
            if self.fail_next_insert:
                self.fail_next_insert = False
                raise UniqueViolationError("duplicate key value violates unique constraint")
            auto = "'auto_resolved'" in s
            tid = self.add_ticket(options=json.loads(args[6]),
                                  metadata=json.loads(args[14] if auto else args[10]),
                                  eid=args[0], status="auto_resolved" if auto else "open")
            self.tickets[tid]["sitrep"] = args[5]
            self.tickets[tid]["priority"] = args[8]
            return {"id": tid, "ticket_uuid": "00000000-0000-0000-0000-%012d" % tid,
                    "opened_at": None}
        if "SELECT id, circle_id, status, options, metadata" in s:
            tid, eid = args
            t = self.tickets.get(tid)
            return dict(t) if t and t["entity_id"] == eid else None
        raise AssertionError(f"unexpected fetchrow: {s[:90]}")

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        self.fetched.append((s, args))
        if "SELECT 1 FROM project_phases ph JOIN projects p" in s and "p.entity_id" in s:
            phase_id, project_id, eid = args
            ph = self.phases.get(phase_id)
            return 1 if ph and ph["project_id"] == project_id and ph["entity_id"] == eid else None
        raise AssertionError(f"unexpected fetchval: {s[:90]}")

    # An approved pattern for one action_class, when a scenario sets it.
    pattern: dict | None = None

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        self.fetched.append((s, args))
        if "FROM decision_patterns" in s:
            eid, action_class, circle_id = args
            p = self.pattern
            if p and p["action_class"] == action_class and p["entity_id"] == eid:
                return [{"id": p["id"], "circle_id": None, "conditions": {},
                         "decision_template": p["decision_template"], "observations": 1}]
            return []
        if "SELECT id, options, metadata FROM entity_tickets" in s:
            eid, phase_key = args[0], args[1]
            need_open = "status = 'open'" in s
            excludes = "COALESCE(metadata->>'kind', '') <> $3" in s
            kind = args[2] if excludes else None
            out = []
            for t in self.tickets.values():
                md = json.loads(t["metadata"])
                if (t["entity_id"] == eid and (not need_open or t["status"] == "open")
                        and str(md.get("phase_id")) == str(phase_key)
                        and (not excludes or md.get("kind", "") != kind)):
                    out.append({"id": t["id"], "options": t["options"], "metadata": t["metadata"]})
            return out
        if "SELECT id FROM entity_tickets" in s and "metadata->>'kind'" in s:
            return self._open_gate_rows(s, args)
        if ("SELECT id, metadata FROM entity_tickets" in s and "status = 'decided'" in s
                and "metadata->>'kind' = $2" in s and "metadata->>'phase_id' = $3" in s):
            # support_collect.support_landed: the decided support cards joined
            # to the phase that landed (this gate's fixture has none).
            self.support_landed_asks.append(args)
            eid, kind, phase_key = args
            return [{"id": t["id"], "metadata": t["metadata"]} for t in self.tickets.values()
                    if t["entity_id"] == eid and t["status"] == "decided"
                    and json.loads(t["metadata"]).get("kind") == kind
                    and str(json.loads(t["metadata"]).get("phase_id")) == str(phase_key)]
        raise AssertionError(f"unexpected fetch: {s[:90]}")

    async def execute(self, sql, *args):
        s = " ".join(sql.split())
        self.executed.append((s, args))
        if "UPDATE entity_tickets SET status = 'decided'" in s:
            decision, eid, tid = args
            t = self.tickets.get(tid)
            if not t or (t["status"] != "open" and "status = 'open'" in s):
                return "UPDATE 0"
            t["status"] = "decided"
            t["decision"] = json.loads(decision)
            t["decided_at"] = self.clock()
            return "UPDATE 1"
        if "UPDATE entity_tickets SET options = " in s:
            opts, tid = args
            self.tickets[tid]["options"] = opts
            return "UPDATE 1"
        if "jsonb_build_object('phase_last'" in s:
            last, tid = args
            md = json.loads(self.tickets[tid]["metadata"])
            md["phase_last"] = json.loads(last)
            self.tickets[tid]["metadata"] = json.dumps(md)
            return "UPDATE 1"
        if "UPDATE project_phases" in s and "approved_at = COALESCE(approved_at, now())" in s:
            phase_id, project_id = args
            ph = self.phases.get(phase_id)
            need_gate = "gate = 'user'" in s
            if not ph or ph["project_id"] != project_id or (need_gate and ph["gate"] != "user"):
                return "UPDATE 0"
            ph["approved_at"] = ph["approved_at"] or "2026-09-24T00:00:00Z"
            ph["updated_at"] = self.clock()
            return "UPDATE 1"
        if "UPDATE entity_tickets SET pattern_id" in s:
            return "UPDATE 1"
        if "UPDATE decision_patterns SET observations" in s:
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {s[:90]}")


def run(coro):
    # One fresh loop per call (3.12+ has no default loop on the main thread);
    # the stub pool is plain Python state, so it survives across loops.
    return asyncio.run(coro)


# The vault, as the service asks it: a set of names. Replaced per scenario.
# VAULT_CALLS counts the asks, so "never asks the vault" is a number that
# moves, not a string that never appears (the fake lives
# outside the pool, so the pool's log could not see it).
VAULT: set[str] = set()
VAULT_DOWN = False
VAULT_CALLS = 0


async def _fake_vault_names(pool, eid):
    global VAULT_CALLS
    VAULT_CALLS += 1
    if VAULT_DOWN:
        raise RuntimeError("vault unreachable")
    return set(VAULT)


T._active_vault_names = _fake_vault_names

# ── 1. credential_request shape, and the vault-name rule ─────────────────────
bad = T._bad_credential_request
check("credential ask: a good one passes",
      bad({"name": "smtp_sites", "cred_type": "api_key", "purpose": "tenant mail"}) == "")
check("credential ask: opaque is a type", bad({"name": "x", "cred_type": "opaque"}) == "")
check("credential ask: needs a name", "name" in bad({"cred_type": "api_key"}))
check("credential ask: cred_type is closed", "cred_type" in bad({"name": "x", "cred_type": "ssh"}))
check("credential ask: never carries a value",
      "value" in bad({"name": "x", "cred_type": "api_key", "value": "hunter2"}))
check("credential ask: must be an object", "object" in bad("smtp_sites"))

pool = Pool()
try:
    run(T.create_ticket_service(
        pool, 9003, source="manual", sitrep="do the thing",
        options=[{"id": "done", "label": "Done",
                  "credential_request": {"name": "x", "cred_type": "nope"}}]))
    check("create refuses a malformed credential ask", False)
except T.TicketError as e:
    check("create refuses a malformed credential ask", e.status == 400 and "cred_type" in e.detail,
          e.detail)
check("create with a bad ask wrote nothing", not pool.tickets)

VAULT = {"openrouter", "github"}
try:
    run(T.create_ticket_service(
        pool, 9003, source="manual", sitrep="paste your GitHub token",
        options=[{"id": "done", "label": "Done",
                  "credential_request": {"name": "github", "cred_type": "api_key"}}]))
    check("create refuses an ask that names an existing key (the vault supersedes on name)", False)
except T.TicketError as e:
    check("create refuses an ask that names an existing key (the vault supersedes on name)",
          e.status == 400 and "existing key" in e.detail and "github" in e.detail, e.detail)
r = run(T.create_ticket_service(
    pool, 9003, source="manual", sitrep="paste the mail account key",
    options=[{"id": "done", "label": "Done",
              "credential_request": {"name": "smtp_sites", "cred_type": "api_key"}}]))
check("create accepts an ask under a NEW name", r["id"] in pool.tickets)
VAULT_DOWN = True
try:
    run(T.create_ticket_service(
        pool, 9003, source="manual", sitrep="x",
        options=[{"id": "done", "label": "Done",
                  "credential_request": {"name": "new_name", "cred_type": "api_key"}}]))
    check("an unanswerable vault refuses the ask (fail closed)", False)
except T.TicketError as e:
    check("an unanswerable vault refuses the ask (fail closed)", e.status == 503, e.detail)
VAULT_DOWN = False
n_before = len(pool.tickets)
calls_before = VAULT_CALLS
run(T.create_ticket_service(pool, 9003, source="manual", sitrep="a plain decision",
                            options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]))
check("a ticket with no ask never asks the vault",
      len(pool.tickets) == n_before + 1 and VAULT_CALLS == calls_before)

# ── 2. the route strips the platform's locks; a phase link must be integers ──
strip = T._strip_platform_locks
check("route strip: user_only / no_pattern / route_change_proposal never pass",
      strip({"user_only": True, "no_pattern": True, "route_change_proposal": {}, "x": 1}) == {"x": 1})
check("route strip: kind=phase_gate never passes (approval authority stays where it is)",
      strip({"kind": "phase_gate", "phase_id": 7, "project_id": 9}) == {"phase_id": 7, "project_id": 9})
check("route strip: another kind passes untouched",
      strip({"kind": "note", "phase_id": 7}) == {"kind": "note", "phase_id": 7})
check("route strip: a non-object is empty", strip("x") == {} and strip(None) == {})
check("phase link: integers pass", T._bad_phase_link({"project_id": 9, "phase_id": 7}) == "")
check("phase link: a string is refused", "phase_id" in T._bad_phase_link({"phase_id": "p-7"}))
check("phase link: a float is refused", "phase_id" in T._bad_phase_link({"phase_id": 5.0}))
check("phase link: a bool is refused", "project_id" in T._bad_phase_link({"project_id": True}))
try:
    run(T.create_ticket_service(pool, 9003, source="manual", sitrep="x",
                                options=[{"id": "a", "label": "A"}], metadata={"phase_id": "p-7"}))
    check("create refuses a non-integer phase link", False)
except T.TicketError as e:
    check("create refuses a non-integer phase link", e.status == 400, e.detail)

# ── 3. a gated phase raises ONE ticket ───────────────────────────────────────
pool = Pool()
pool.add_phase(42)
r1 = run(T.raise_phase_gate_ticket(pool, 9003, project_id=9, phase_id=42))
check("gate ticket raised for a gated, unapproved phase", r1["created"] and r1["ticket_id"])
t = pool.tickets[r1["ticket_id"]]
md = json.loads(t["metadata"])
check("gate ticket carries kind/project/phase", md.get("kind") == T.PHASE_GATE_KIND
      and md.get("project_id") == 9 and md.get("phase_id") == 42, str(md))
check("gate ticket is user_only and never a pattern",
      md.get("user_only") is True and md.get("no_pattern") is True)
opts = json.loads(t["options"])
check("gate ticket offers approve (recommended) and send back",
      [o["id"] for o in opts] == ["approve", "send_back"] and opts[0].get("recommend") is True)
# The click IS the go-ahead. Approving starts the phase when its
# turn has come (tools.start_approved_phase); the card no longer tells the
# person to type a word ("Say build to start it") to make the Host act.
check("approve's consequence says the phase starts, and asks for no word",
      "starts building" in opts[0]["consequence"]
      and not re.search(r"(?i)\b(?:say|type|reply)\b", opts[0]["consequence"] + t["sitrep"]))
check("gate ticket sitrep names the phase and the project",
      "Phase 2" in t["sitrep"] and "Trading" in t["sitrep"] and "Web UI" in t["sitrep"])
check("the phase id travels as text in the lookups",
      all(isinstance(a[2], str) for s, a in pool.fetched
          if "metadata->>'phase_id' = $3" in s))
r2 = run(T.raise_phase_gate_ticket(pool, 9003, project_id=9, phase_id=42))
check("asking again returns the same open ticket, no second card",
      r2["created"] is False and r2["ticket_id"] == r1["ticket_id"] and len(pool.tickets) == 1)
check("only an OPEN card counts as existing",
      any("status = 'open'" in s for s, a in pool.fetched
          if "SELECT id FROM entity_tickets" in s and "metadata->>'kind'" in s))
pool.add_phase(43, gate="none")
check("an ungated phase raises nothing",
      run(T.raise_phase_gate_ticket(pool, 9003, project_id=9, phase_id=43))["ticket_id"] is None)
pool.add_phase(44, approved_at="2026-09-01T00:00:00Z")
check("an already-approved phase raises nothing",
      run(T.raise_phase_gate_ticket(pool, 9003, project_id=9, phase_id=44))["ticket_id"] is None)
fresh = Pool()
fresh.add_phase(42)
check("a phase outside the project raises nothing",
      run(T.raise_phase_gate_ticket(fresh, 9003, project_id=8, phase_id=42))["ticket_id"] is None
      and not fresh.tickets)
# The insert race: two callers passed the "no open card" read; the unique
# index (v177) lets one land. The loser adopts the winner's ticket.
racy = Pool()
racy.add_phase(60)
winner = racy.add_ticket(options=[{"id": "approve", "label": "A"}],
                         metadata={"kind": T.PHASE_GATE_KIND, "phase_id": 60, "project_id": 9},
                         status="decided")       # invisible to the FIRST read
racy.fail_next_insert = True                       # the other caller's insert landed first
async def _race():
    # first lookup sees nothing open; the insert fails; the re-read sees the winner
    orig = racy.fetchrow
    calls = {"n": 0}
    async def fr(sql, *args):
        s = " ".join(sql.split())
        if "SELECT id FROM entity_tickets" in s and "metadata->>'kind'" in s:
            calls["n"] += 1
            if calls["n"] == 2:
                racy.tickets[winner]["status"] = "open"
        return await orig(sql, *args)
    racy.fetchrow = fr
    return await T.raise_phase_gate_ticket(racy, 9003, project_id=9, phase_id=60)
rr = run(_race())
check("a lost insert race adopts the winner's open ticket",
      rr == {"ticket_id": winner, "created": False}, str(rr))
check("the unique index migration exists and is partial on open phase_gate cards",
      "WHERE status = 'open' AND metadata->>'kind' = 'phase_gate'" in
      (ROOT / "migrations/v177_phase_gate_ticket_unique.sql").read_text())

# ── 4. deciding the gate ticket ──────────────────────────────────────────────
gate_tid = r1["ticket_id"]
try:
    run(T.decide_ticket_service(pool, 9003, gate_tid, option_id="approve", actor="host"))
    check("the host may not approve its own gate", False)
except T.TicketError as e:
    check("the host may not approve its own gate", e.status == 403, e.detail)
check("a refused decide left the phase unapproved", pool.phases[42]["approved_at"] is None)

# a sibling card for the same phase (pre-v177 race leftover)
sibling = pool.add_ticket(options=[{"id": "approve", "label": "A"}, {"id": "send_back", "label": "B"}],
                          metadata={"kind": T.PHASE_GATE_KIND, "phase_id": 42, "project_id": 9,
                                    "user_only": True, "no_pattern": True})
out = run(T.decide_ticket_service(pool, 9003, gate_tid, option_id="approve", note="looks right",
                                  actor="user", and_similar=True))
check("approve on the ticket writes approved_at",
      pool.phases[42]["approved_at"] is not None and out["phase_approve"]["applied"] is True)
check("the approval SQL is COALESCE-guarded and gate-scoped",
      any("approved_at = COALESCE(approved_at, now())" in s and "gate = 'user'" in s
          for s, _ in pool.executed))
check("approve answers the sibling card for the same phase",
      out["phase_approve"]["also_decided"] == [sibling]
      and pool.tickets[sibling]["status"] == "decided")
check("no_pattern dropped and_similar: no pattern row, none asked for",
      out["pattern_id"] is None and pool.tickets[gate_tid]["decision"]["and_similar"] is False)
check("the person's note rides the decision",
      pool.tickets[gate_tid]["decision"]["note"] == "looks right")
pool.add_phase(46, gate="none")
ungated = pool.add_ticket(options=[{"id": "approve", "label": "A"}],
                          metadata={"kind": T.PHASE_GATE_KIND, "phase_id": 46, "project_id": 9})
out = run(T.decide_ticket_service(pool, 9003, ungated, option_id="approve", actor="user"))
check("approve on a card for an UNGATED phase writes nothing (gate='user' in the UPDATE)",
      out["phase_approve"]["applied"] is False and pool.phases[46]["approved_at"] is None)

pool.add_phase(45)
r3 = run(T.raise_phase_gate_ticket(pool, 9003, project_id=9, phase_id=45))
before = len(pool.executed)
out = run(T.decide_ticket_service(pool, 9003, r3["ticket_id"], option_id="send_back",
                                  note="split it first", actor="user"))
check("send back decides the ticket but writes nothing on the phase",
      pool.phases[45]["approved_at"] is None and out["phase_approve"] == {"applied": False}
      and not any("UPDATE project_phases" in s for s, _ in pool.executed[before:]))
r4 = run(T.raise_phase_gate_ticket(pool, 9003, project_id=9, phase_id=45))
check("sent back and unchanged since: no new card, the note is the answer",
      r4["created"] is False and r4["ticket_id"] == r3["ticket_id"]
      and r4.get("sent_back") == "split it first"
      and sum(1 for t in pool.tickets.values()
              if json.loads(t["metadata"]).get("phase_id") == 45) == 1)
pool.phases[45]["updated_at"] = pool.clock()   # what set_phase_spec's UPDATE now writes
r5 = run(T.raise_phase_gate_ticket(pool, 9003, project_id=9, phase_id=45))
check("a phase changed after the send-back earns a fresh card",
      r5["created"] is True and r5["ticket_id"] != r3["ticket_id"])
# The guard reads project_phases.updated_at, so the one host-side write the
# refusal tells the host to make (set_phase_spec) MUST move it — when it did
# not, the guard was a dead end.
_tools_src = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
_sps = _tools_src[_tools_src.index('if op == "set_phase_spec":'):]
_sps_update = _sps[_sps.index('"""UPDATE project_phases SET spec=$1,'):]
_sps_update = _sps_update[:_sps_update.index('"""', 3)]
check("set_phase_spec's UPDATE bumps updated_at (the send-back guard reads it)",
      "updated_at = now()" in _sps_update)
# A credential ask is never a pattern, from either side.
ask_tid = pool.add_ticket(options=[{"id": "done", "label": "Done",
                                    "credential_request": {"name": "n1", "cred_type": "api_key"}}],
                          metadata={})
out = run(T.decide_ticket_service(pool, 9003, ask_tid, option_id="done", note="stored in Keys as n1",
                                  actor="user", and_similar=True))
check("decide drops 'and similar' on a credential ask: no pattern crystallises",
      out["pattern_id"] is None and pool.tickets[ask_tid]["decision"]["and_similar"] is False
      and not any("INSERT INTO decision_patterns" in s for s, _ in pool.executed))

# ── 5. a phase landing reaches the joined ticket ─────────────────────────────
pool = Pool()
pool.add_phase(50)
work_tid = pool.add_ticket(options=[{"id": "done", "label": "Done"}],
                           metadata={"project_id": 9, "phase_id": 50})
locked = pool.add_ticket(options=[{"id": "done", "label": "Done"}],
                         metadata={"project_id": 9, "phase_id": 50, "user_only": True})
gate = run(T.raise_phase_gate_ticket(pool, 9003, project_id=9, phase_id=50))
res = run(T.on_phase_outcome(pool, 9003, phase_id=50, status="failed", note="tests red"))
t = pool.tickets[work_tid]
check("failed: recorded on the card, ticket still open",
      sorted(res["noted"]) == sorted([work_tid, locked]) and res["decided"] == []
      and t["status"] == "open"
      and json.loads(t["metadata"])["phase_last"]["status"] == "failed"
      and json.loads(t["metadata"])["phase_last"]["note"] == "tests red")
check("failed: a support landing is not looked for (only a complete landing asks)",
      pool.support_landed_asks == [] and "support_landed" not in res, str(pool.support_landed_asks))
check("failed: the gate ticket is not treated as a joined ticket",
      pool.tickets[gate["ticket_id"]]["status"] == "open"
      and "phase_last" not in json.loads(pool.tickets[gate["ticket_id"]]["metadata"]))
res = run(T.on_phase_outcome(pool, 9003, phase_id=50, status="complete", note="landed"))
t = pool.tickets[work_tid]
check("complete: the support lane is asked about this phase (support_collect.support_landed), "
      "and with no support card joined it offers nothing",
      pool.support_landed_asks == [(9003, "support", "50")] and res.get("support_landed") == {},
      f"{pool.support_landed_asks} {res.get('support_landed')}")
check("complete: the joined ticket is decided 'built'",
      res["decided"] == [work_tid] and t["status"] == "decided"
      and t["decision"]["option_id"] == T.BUILT_OPTION_ID and t["decision"]["note"] == "landed")
check("complete: 'built' was added to the options the writer left without it",
      any(o["id"] == T.BUILT_OPTION_ID for o in json.loads(t["options"])))
check("complete: a user_only joined ticket is left for the user (host may not decide it)",
      pool.tickets[locked]["status"] == "open")
check("complete: the gate ticket is untouched by the outcome",
      pool.tickets[gate["ticket_id"]]["status"] == "open")
check("a second landing finds nothing it may decide",
      run(T.on_phase_outcome(pool, 9003, phase_id=50, status="complete"))["decided"] == [])
check("no pool / no entity: a quiet no-op, never a raise",
      run(T.on_phase_outcome(None, 9003, phase_id=50, status="complete")) == {"decided": [], "noted": []}
      and run(T.raise_phase_gate_ticket(pool, None, project_id=9, phase_id=50))["ticket_id"] is None)

# ── 6. approval in the Workbench decides the dangling gate ticket ────────────
done = run(T.resolve_phase_gate_tickets(pool, 9003, phase_id=50))
check("resolve decides the open gate ticket with approve",
      done == [gate["ticket_id"]] and pool.tickets[gate["ticket_id"]]["status"] == "decided"
      and pool.tickets[gate["ticket_id"]]["decision"]["option_id"] == "approve")
check("resolve on a phase with no gate ticket is empty",
      run(T.resolve_phase_gate_tickets(pool, 9003, phase_id=51)) == [])

# ── 7. the host's escalate applies the same checks ───────────────────────────
from server import brain_service  # noqa: E402
from server.applications import escalation as E  # noqa: E402

epool = Pool()
epool.add_phase(70, project_id=9, entity_id=9003)
epool.add_phase(71, project_id=9, entity_id=4)      # someone else's
brain_service.get_pool = lambda: epool
brain_service._entity_id = 9003
VAULT = {"github"}
try:
    run(E.escalate(source="runtime", sitrep="x",
                   options=[{"id": "done", "label": "Done",
                             "credential_request": {"name": "github", "cred_type": "api_key"}}]))
    check("escalate refuses an ask naming an existing key", False)
except ValueError as e:
    check("escalate refuses an ask naming an existing key", "existing key" in str(e), str(e))
try:
    run(E.escalate(source="runtime", sitrep="x", options=[{"id": "done", "label": "Done"}],
                   metadata={"project_id": 9, "phase_id": 71}))
    check("escalate refuses a join to a phase this host does not own", False)
except ValueError as e:
    check("escalate refuses a join to a phase this host does not own", "own projects" in str(e), str(e))
res = run(E.escalate(source="runtime", sitrep="x", options=[{"id": "done", "label": "Done"}],
                     metadata={"project_id": 9, "phase_id": 70, "opened_via": "llm_tool"}))
check("escalate joins a ticket to this host's own phase",
      res["status"] == "open"
      and json.loads(epool.tickets[res["ticket_id"]]["metadata"]).get("phase_id") == 70)
mark = len(epool.fetched)
calls_before = VAULT_CALLS
run(E.escalate(source="runtime", sitrep="plain", options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]))
check("a plain escalate never asks the vault or the phase table",
      VAULT_CALLS == calls_before
      and not any("project_phases" in s for s, _ in epool.fetched[mark:]))
# An approved pattern auto-resolves an ordinary ask of its class — and never a
# credential ask of the same class.
epool.pattern = {"id": 5, "entity_id": 9003, "action_class": "share_specs",
                 "decision_template": {"option_id": "yes", "response_template": "sent"}}
VAULT = set()
auto = run(E.escalate(source="runtime", sitrep="x", action_class="share_specs",
                      options=[{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No"}]))
check("the stub pattern auto-resolves an ordinary ask (the control)",
      auto["auto_resolved"] is True and auto["pattern_id"] == 5)
cred = run(E.escalate(source="runtime", sitrep="x", action_class="share_specs",
                      options=[{"id": "done", "label": "Done",
                                "credential_request": {"name": "n2", "cred_type": "api_key"}}]))
check("a credential ask of the same class is NOT auto-resolved: the user answers it",
      cred["auto_resolved"] is False and cred["status"] == "open"
      and epool.tickets[cred["ticket_id"]]["status"] == "open")
epool.pattern = None

# ── 8. the call sites and the tool surface ───────────────────────────────────
tools_src = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
land = re.search(r"await _set_phase_status\(pool, phase_id, phase_final\)\n"
                 r"(?:\s*#.*\n)*\s*await _ticket_phase_outcome\(pool, entity_id, phase_id, phase_final, out\)",
                 tools_src)
check("tools: the landing block reports the outcome right after the phase write", land is not None)
check("tools: the build-door refusal raises the gate ticket and relays a send-back note",
      re.search(r"if _phase_awaits_approval\(prow\):\n(?:\s*#.*\n)*\s*_gate = await _raise_phase_gate_tickets\(pool, entity_id, project_id, \[prow\]\)",
                tools_src) is not None and "SENT BACK" in tools_src)
check("tools: both idle paths raise gate tickets for held 'approval' phases",
      tools_src.count("await _raise_phase_gate_tickets(") >= 3)
check("tools: helpers import the services lazily and are fail-soft",
      "from server.routes.tickets import on_phase_outcome" in tools_src
      and "from server.routes.tickets import raise_phase_gate_ticket" in tools_src
      and tools_src.count("ticket join:") >= 2)
check("tools: the escalate handler passes project_id/phase_id into metadata as integers",
      'for _k in ("project_id", "phase_id"):' in tools_src and "metadata=_md," in tools_src)
check("tools: no comment still claims the Workbench route is the only approved_at writer",
      "only the Workbench approve route writes approved_at" not in tools_src)
# The schema the model fills and the ticket service parses, read as a schema
# (not its wording, which is trimmed): options carry id + label (the
# service refuses an option without them), consequence and recommend, a
# credential_request with its name and type, and the ticket joins a phase
# through integer project_id + phase_id.
from mastercode.runtime.triage import _BOT_TOOL_SCHEMAS as _BTS  # noqa: E402
_esc = _BTS["_meta_escalate"]["function"]
_ep = _esc["parameters"]["properties"]
_opt = (_ep.get("options") or {})
_oi = _opt.get("items") or {}
_cr = (_oi.get("properties") or {}).get("credential_request") or {}
check("escalate schema: sitrep + options required; options are objects needing id + label",
      set(_esc["parameters"].get("required") or []) >= {"sitrep", "options"}
      and _opt.get("type") == "array" and set(_oi.get("required") or []) >= {"id", "label"})
check("escalate schema: an option carries consequence and recommend",
      {"consequence", "recommend"} <= set((_oi.get("properties") or {})))
check("escalate schema: the 1-4 bound is stated where the model fills options",
      re.search(r"\b1-4\b", _opt.get("description") or "") is not None, _opt.get("description") or "")
check("escalate schema: credential_request needs name + cred_type (api_key|opaque)",
      set(_cr.get("required") or []) >= {"name", "cred_type"}
      and set(((_cr.get("properties") or {}).get("cred_type") or {}).get("enum") or []) == {"api_key", "opaque"})
check("escalate schema: project_id + phase_id are integers (the phase join)",
      (_ep.get("project_id") or {}).get("type") == "integer"
      and (_ep.get("phase_id") or {}).get("type") == "integer")
proj_src = (ROOT / "server/routes/projects.py").read_text()
approve = proj_src[proj_src.index("async def approve_phase("):]
approve = approve[:approve.index("\n@router") if "\n@router" in approve else len(approve)]
check("projects: the review->complete approval reports 'complete' to the joined ticket",
      'status="complete"' in approve and "on_phase_outcome(" in approve)
check("projects: the gate approval resolves the gate ticket",
      "resolve_phase_gate_tickets(" in approve)
check("projects: the docstring names both writers of approved_at",
      "the only writer of approved_at" not in approve and "phase-gate ticket" in approve)

# ── 9. the Circles card sends what the services accept ───────────────────────
# The Tickets pane's decision in full is Circles' Decisions sheet now
# (circles-web/src/cards/tickets.tsx).
pane = (ROOT / "circles-web/src/cards/tickets.tsx").read_text()
api = (ROOT / "circles-web/src/lib/api.ts").read_text()
check("pane: the decision carries the person's note",
      "onDecide(o.id, note.trim())" in pane
      and "decideTicket(active.id, optionId, andSimilar, note, supportMark(active), stepupCode)" in pane)
check("api: decideTicket sends note",
      re.search(r"\{ option_id: optionId, and_similar: andSimilar, note \}", api) is not None)
# The note and the refusal line live in the shared kit, so the
# ask card over either chat seals a credential ask the same way
# (app-web-kit/ask.ts sealedNote / sealErrorText; circles-web/test/
# in-chat-cards.test.ts runs them).
kit = (ROOT / "app-web-kit/ask.ts").read_text()
overlay = (ROOT / "app-web-kit/ui/ask-overlay.tsx").read_text()
check("pane: a credential ask seals into Keys before deciding",
      "storeVaultSecret(req.name, req.cred_type, value, true)" in pane
      and "onDecide(o.id, sealedNote(req.name, note))" in pane
      and 'const stored = `stored in Keys as "${name}"`;' in kit)
check("ask card: a credential ask seals into Keys, then decides with the name only",
      "const why = await onSealCredential(req, value)" in overlay
      and "onAnswer(o, sealedNote(req.name, note))" in overlay
      and 'type="password"' in overlay)
check("pane: the sealed field is a password input, never echoed",
      'type="password"' in pane)
check("pane: a one-option ticket reads as something to do",
      "ticket.options.length === 1" in pane and "When you have done it" in pane)
check("pane: the paste refuses to replace a key that appeared since the ask (if_absent)",
      "storeVaultSecret(req.name, req.cred_type, value, true)" in pane
      and "sealErrorText(r.status, r.message, req.name)" in pane
      and "status === 409" in kit and "if_absent: ifAbsent" in api)
wb_api = (ROOT / "workbench-web/src/lib/api.ts").read_text()
for app, pane_src in (
        ("circles", (ROOT / "circles-web/src/cards/use-host-thread.ts").read_text()),
        ("workbench", (ROOT / "workbench-web/src/components/workbench/chat-pane.tsx").read_text())):
    check(f"{app} ask card: the paste refuses to replace an existing key (if_absent)",
          re.search(r'req\.cred_type === "api_key" \? "api_key" : "opaque",\s*value,\s*true,?\s*\)', pane_src)
          is not None and "sealErrorText(r.status, r.message, req.name)" in pane_src)
check("workbench api: storeVaultSecret sends if_absent", "if_absent: ifAbsent" in wb_api)
check("pane: 'and similar' is hidden on a credential ask",
      "const hasAsk = ticket.options.some((o) => Boolean(o.credential_request))" in pane
      and "{!hasAsk && (" in pane)
vault_src = (ROOT / "server/routes/vault.py").read_text()
check("vault route: if_absent rides into the store and NameTaken is a 409",
      "if_absent: bool = False" in vault_src and "if_absent=req.if_absent" in vault_src
      and "except vault.store.NameTaken:" in vault_src
      and "409" in vault_src[vault_src.index("except vault.store.NameTaken:"):][:400])
store_src = (ROOT / "server/vault/store.py").read_text()
put_src = store_src[store_src.index("async def put("):]
put_src = put_src[:put_src.index("\nasync def ", 10)]
check("vault store: with if_absent the supersede is skipped and the unique index's collision is the refusal",
      "class NameTaken(Exception)" in store_src
      and "if not if_absent:" in put_src
      and put_src.index("if not if_absent:") < put_src.index("SET superseded_at = now()")
      < put_src.index("INSERT INTO vault_envelope")
      < put_src.index("except asyncpg.UniqueViolationError:") < put_src.index("raise NameTaken(")
      and "FOR UPDATE" not in put_src)
_idx = re.compile(r"CREATE UNIQUE INDEX IF NOT EXISTS idx_vault_envelope_active\s+"
                  r"ON vault_envelope \(owner_identity, logical_name\)\s+"
                  r"WHERE superseded_at IS NULL")
check("vault schema: the UNIQUE partial index on active names — the rule if_absent relies on — "
      "is declared whole in init-db.sql and in the v147 migration",
      _idx.search((ROOT / "init-db.sql").read_text()) is not None
      and _idx.search((ROOT / "migrations/v147_band_c_kernel_vault_apps.sql").read_text()) is not None)
api_src = (ROOT / "server/vault/api.py").read_text()
check("vault api: store_secret passes if_absent through", "if_absent=if_absent," in api_src)

# ── 7. approving a gated phase STARTS it when its turn has come ──────────────
# "Say build to start it" was the card asking for a typed word to make the
# Host act. The click is the go-ahead: tools.start_approved_phase starts the
# phase through the build door by phase_id — never ahead of what it waits
# for, never over a stop — and both approval paths call it.
from mastercode.runtime import tools as TOOLS  # noqa: E402


class StartPool:
    def __init__(self, rows, halted=False):
        self.rows, self.halted = rows, halted

    async def fetchval(self, sql, *a):
        if "build_chain_halt" in sql:
            return self.halted
        raise AssertionError(sql)

    async def fetch(self, sql, *a):
        if "FROM project_phases" in sql:
            return self.rows
        raise AssertionError(sql)


def _ph(pid, n, status="ready", gate="user", approved="2026-09-30T21:00:00Z", deps=None):
    return {"id": pid, "phase_number": n, "title": f"Phase {n}", "spec": "brief",
            "description": "", "status": status, "gate": gate, "approved_at": approved,
            "acceptance": None, "module_id": None, "depends_on": deps, "harness": None,
            "model": None, "integration_error": None}


_built: list = []


async def _fake_build(args, entity_id, pool, session_id, interaction_context=None, thread_id=None):
    _built.append((args, entity_id, session_id, interaction_context))
    return {"status": "ok", "result": {"task_id": 777, "started": [{"task_id": 777}]}}

_saved_build = TOOLS._exec_build
TOOLS._exec_build = _fake_build
try:
    r = run(TOOLS.start_approved_phase(StartPool([_ph(1, 1, "complete", "none"), _ph(2, 2)]), 9003, 9, 2))
    check("an approved phase whose turn has come starts through the build door, by phase_id",
          r.get("started") is True and _built
          and _built[-1][0] == {"action": "build", "project_id": 9, "phase_id": 2}
          and _built[-1][3] == "project:9", str(r))
    n0 = len(_built)
    r = run(TOOLS.start_approved_phase(StartPool([_ph(1, 1, "in_progress", "none"), _ph(2, 2)]), 9003, 9, 2))
    check("...never ahead of what it waits for: it says when it will start instead",
          r.get("started") is False and "phase 1" in (r.get("why") or "") and len(_built) == n0, str(r))
    r = run(TOOLS.start_approved_phase(StartPool([_ph(2, 2, deps=[])], halted=True), 3, 9, 2))
    check("...never over a stop the user set", r.get("started") is False and len(_built) == n0, str(r))
    r = run(TOOLS.start_approved_phase(StartPool([_ph(2, 2, approved=None, deps=[])]), 9003, 9, 2))
    check("...and never an unapproved phase", r.get("started") is False and len(_built) == n0, str(r))
    r = run(TOOLS.start_approved_phase(StartPool([_ph(2, 2, "in_progress", deps=[])]), 9003, 9, 2))
    check("...nor one already building", r.get("started") is False and len(_built) == n0, str(r))

    # continue_plan: the user's Approve on a phase
    # that landed in 'review' goes on with the plan — every phase that can
    # start now starts through the build door, as when a phase completes on
    # its own; never over a stop; a refusal is reported, never raised.
    r = run(TOOLS.continue_plan(StartPool([_ph(2, 2, "review")]), 9003, 9))
    check("continue_plan: the plan goes on through the build door, for the whole project",
          r.get("started") is True and len(_built) == n0 + 1
          and _built[-1][0] == {"action": "build", "project_id": 9}
          and _built[-1][3] == "project:9", str(r))
    n1 = len(_built)
    r = run(TOOLS.continue_plan(StartPool([_ph(2, 2, "review")], halted=True), 9003, 9))
    check("continue_plan: never over a stop the user set",
          r.get("started") is False and "stopped" in (r.get("why") or "") and len(_built) == n1, str(r))

    async def _refusing_build(args, entity_id, pool, session_id, interaction_context=None, thread_id=None):
        _built.append((args, entity_id, session_id, interaction_context))
        return {"status": "error", "result": "phase 3 ('Web UI') is gated on the user's approval"}
    TOOLS._exec_build = _refusing_build
    r = run(TOOLS.continue_plan(StartPool([_ph(2, 2, "review")]), 9003, 9))
    check("continue_plan: what the door refuses is reported as not started, with its reason",
          r.get("started") is False and "gated on the user's approval" in str(r.get("result")), str(r))

    async def _raising_build(*a, **kw):
        raise RuntimeError("pool gone")
    TOOLS._exec_build = _raising_build
    r = run(TOOLS.continue_plan(StartPool([_ph(2, 2, "review")]), 9003, 9))
    check("continue_plan: never raises — the completion already stands",
          r.get("started") is False and "pool gone" in (r.get("why") or ""), str(r))
finally:
    TOOLS._exec_build = _saved_build
_pj_src = (ROOT / "server/routes/projects.py").read_text()
check("the Workbench's Approve on a phase in review goes on with the plan (continue_plan)",
      "went_on = await continue_plan(pool, _eid(), project_id)" in _pj_src
      and _pj_src.index("if remaining:") < _pj_src.index("went_on = await continue_plan("))
_tk_src = (ROOT / "server/routes/tickets.py").read_text()
_apply = _tk_src[_tk_src.index("async def _apply_phase_gate_decision("):]
_apply = _apply[:_apply.index("\nasync def ")]
check("the ticket's approve starts the phase (start_approved_phase) before its siblings are decided",
      "start_approved_phase(pool, eid, project_id, phase_id)" in _apply
      and _apply.index("start_approved_phase(") < _apply.index("resolve_phase_gate_tickets("))
_ap = proj_src[proj_src.index("async def approve_phase("):]
_ap = _ap[:_ap.index("\n@router") if "\n@router" in _ap else len(_ap)]
check("the Workbench Approve starts it too, before deciding the gate ticket, and says no 'say build'",
      "start_approved_phase(pool, _eid(), project_id, phase_id)" in _ap
      and _ap.index("start_approved_phase(") < _ap.index("resolve_phase_gate_tickets(")
      and "say build" not in _ap.lower())

print()
if FAILED:
    print(f"{FAILED} check(s) FAILED")
    sys.exit(1)
print("all ok")
