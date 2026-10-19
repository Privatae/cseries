#!/usr/bin/env python3
"""
Lane-B agent GRANT wall gate (G-001, smoke).

Structural verification that a dispatched scoped agent cannot execute a tool
its host did not grant it. Runs against the image (/app) or a source tree
(APP_ROOT=<repo>); no pod, no pool, no model — the loop is driven with a
scripted call_model so the walls are exercised deterministically.

WHY THIS EXISTS
`tool_scope` used to filter only the SCHEMA LIST — which tools the model was
OFFERED. Execution dispatched on membership in the GLOBAL `_META_TOOL_NAMES`,
so a name the agent was never offered still ran, gated only by the entity's
own CEIGAS bits (which a dispatched agent inherits wholesale). The host's
grant was advisory. An agent that named an ungranted tool — hallucinated, or
supplied by attacker-controlled content arriving in a tool RESULT — got it.

And that a granted tool cannot exceed the CAPABILITIES the host elevated it to
hold: effective bits are entity_bits & grant_bits, so "read-only, do not write
memory" is something the agent never held rather than a line in its prompt.

  1. in-grant call executes
  2. out-of-grant call is refused AND never reaches the executor  <- the wall
  3. negative control: that same name IS in the global dispatch set, so
     without the wall it would have executed (proves the gate does real work)
  4. empty grant is fail-closed, not fail-open
  5. code-exec still reports 'blocked' with the build-lane routing hint —
     the code-exec wall must stay AHEAD of the grant wall
  6. refusals are VISIBLE: a denied tool card reaches progress_cb
  7. refused names surface in denied_tools for the caller/verdict
  8. the capability floor is ZERO — nothing until the host elevates
  9. grants are capability NAMES; an unknown name is surfaced, never dropped
 10. a read grant masks WRITE_MEMORY off the effective bits
 11. a grant can never widen past the entity's own bits
 12. an incoherent dispatch (tool granted, its capability not) is refused
     BEFORE the agent runs, naming the tool and the missing capability

Exit 0 = all pass; exit 1 + [grant-wall] FAIL lines otherwise.
"""

import asyncio
import json
import os
import sys

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[grant-wall] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class _Pool:
    """Every DB touch in the loop is best-effort bookkeeping."""
    async def execute(self, *a, **k): return None
    async def fetch(self, *a, **k): return []
    async def fetchval(self, *a, **k): return None
    async def fetchrow(self, *a, **k): return None


def _schema(name):
    return {"type": "function",
            "function": {"name": name, "description": name,
                         "parameters": {"type": "object", "properties": {}}}}


def _tool_call(name, cid="c1", args="{}"):
    return {"id": cid, "function": {"name": name, "arguments": args}}


async def _run(sa, *, scope, calls, executed, frames, grant=("write_memory",),
               exec_result=None, meta_names=None):
    """Drive one agent run whose model emits `calls` on round 1, then stops.

    `grant` defaults to write_memory because both fixture tools (inbox_send,
    memory_curate) require it — without it the dispatch is refused up front as
    incoherent and the tool-name wall never gets exercised. That refusal is
    itself correct behaviour and is asserted separately below.
    """
    rounds = {"n": 0}

    async def fake_call_model(**kw):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return {"content": "", "tool_calls": calls, "raw_message": {},
                    "tokens_used": 0, "prompt_tokens": 0, "completion_tokens": 0}
        return {"content": "done", "tool_calls": None, "raw_message": {},
                "tokens_used": 0, "prompt_tokens": 0, "completion_tokens": 0}

    _names = set(meta_names or {"inbox_send", "memory_curate"})

    async def fake_exec_meta(name, args, **kw):
        executed.append(name)
        # `exec_result` stands in for the capability gate INSIDE the executor
        # (tools.py invariant 2), which this stub otherwise bypasses. A non-dict
        # stands in for a tool that answers with a bare value.
        if exec_result is None:
            return {"status": "ok", "result": f"{name} ran"}
        return dict(exec_result) if isinstance(exec_result, dict) else exec_result

    async def fake_schemas(pool, eid, **kw):
        # The entity is authorized for every fixture tool — the only thing
        # that should narrow the agent is the host's grant.
        return [_schema(n) for n in sorted(_names)]

    async def fake_route(pool, eid, role):
        # Route shape since orchestrator v2: the resolver returns
        # {provider, model} (+ optional base_url / failover pair). A
        # dispatched agent resolves the host's own chat route. The old
        # {lane: ...} form no longer carries the key scoped_agent reads
        # (_r["provider"]), so this stub used to KeyError before the wall
        # under test was ever exercised.
        return {"provider": "openrtr", "model": "test/model", "base_url": None,
                "fallback_provider": None, "fallback_model": None}

    async def progress_cb(frame):
        frames.append(frame)

    sa.call_model = fake_call_model
    sa._execute_meta_tool = fake_exec_meta
    sa._build_tool_schemas = fake_schemas
    sa.resolve_role_route = fake_route
    sa._META_TOOL_NAMES = set(_names)
    sa._BRIDGE_TOOL_NAMES = set()

    return await sa.run_scoped_agent(
        _Pool(), entity_id=1, task_prompt="t", tool_scope=scope,
        grant=list(grant), session_id=None, max_rounds=3, progress_cb=progress_cb)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "brain"))
    import mastercode.runtime.scoped_agent as sa
    from mastercode.runtime.triage import _META_TOOL_NAMES as REAL_META
    from mastercode.code_exec_guard import _is_code_exec_tool

    # ── 3. NEGATIVE CONTROL, asserted before anything else. ────────────────
    # The wall is only meaningful if the name it blocks is one the runtime
    # would otherwise dispatch. `memory_curate` is a real registered meta-tool
    # (WRITE_MEMORY, bit 8), so pre-fix it executed on any agent whose entity
    # held that bit — i.e. every agent, since bits are inherited wholesale.
    check("negative control: blocked name is really dispatchable",
          "memory_curate" in REAL_META and not _is_code_exec_tool("memory_curate"),
          "memory_curate must be a live meta-tool for this gate to mean anything")

    # ── 1/2/6/7. Grant honoured, ungranted refused, refusal visible. ───────
    executed, frames = [], []
    out = asyncio.run(_run(sa, scope=["inbox_send"],
                           calls=[_tool_call("inbox_send", "c1"),
                                  _tool_call("memory_curate", "c2")],
                           executed=executed, frames=frames))

    check("in-grant tool executes", executed == ["inbox_send"],
          f"executed={executed}")
    check("out-of-grant tool never reaches the executor",
          "memory_curate" not in executed, f"executed={executed}")

    statuses = {a["tool"]: a.get("result_status") for a in out["tool_actions"]}
    check("out-of-grant call recorded as denied_out_of_grant",
          statuses.get("memory_curate") == "denied_out_of_grant", f"{statuses}")
    check("refused name surfaces in denied_tools",
          "memory_curate" in out["denied_tools"], f"{out['denied_tools']}")

    denied_frames = [f for f in frames
                     if f.get("event") == "tool_call_result"
                     and f.get("status") == "denied"
                     and f.get("tool") == "memory_curate"]
    check("refusal is VISIBLE — denied tool card emitted", len(denied_frames) == 1,
          f"{len(denied_frames)} denied frames for memory_curate")
    check("denial explains the grant to the model",
          bool(denied_frames) and "not in your grant"
          in str(denied_frames[0]["result"]["result"]))

    # ── 4. Empty grant is fail-closed. ─────────────────────────────────────
    executed2, frames2 = [], []
    asyncio.run(_run(sa, scope=[], calls=[_tool_call("inbox_send")],
                     executed=executed2, frames=frames2))
    check("empty grant denies everything (fail-closed)", executed2 == [],
          f"executed={executed2}")

    # ── 5. Code-exec wall stays AHEAD of the grant wall. ───────────────────
    # A code-exec name is stripped from scope up front, so it is out-of-grant
    # too — grant-first would swallow it into the generic refusal and lose the
    # build-lane routing hint that keeps the model from dead-ending.
    executed3, frames3 = [], []
    out3 = asyncio.run(_run(sa, scope=["inbox_send"], calls=[_tool_call("codex.run")],
                            executed=executed3, frames=frames3))
    st3 = {a["tool"]: a.get("result_status") for a in out3["tool_actions"]}
    check("code-exec reports 'blocked', not 'denied_out_of_grant'",
          st3.get("codex.run") == "blocked", f"{st3}")
    hint = [f for f in frames3 if f.get("event") == "tool_call_result"
            and f.get("tool") == "codex.run"]
    check("code-exec refusal keeps the build-lane routing hint",
          bool(hint) and "build" in str(hint[0]["result"]["result"]).lower())
    check("code-exec never reaches the executor", executed3 == [],
          f"executed={executed3}")

    # ── 21. blocked_reason names the WALL that fired (2026-08-19). ─────────
    # The tray used to print one fixed caption ("code work runs on the host's
    # build lane") for EVERY blocked verdict — wrong whenever the grant wall,
    # not the code-exec wall, was the reason. The record
    # now carries scoped_agent's own sentence; assert it distinguishes.
    check("out-of-grant run's blocked_reason names the grant wall",
          "outside its grant" in (out.get("blocked_reason") or "")
          and "memory_curate" in (out.get("blocked_reason") or "")
          and "harness pool" not in (out.get("blocked_reason") or ""),
          f"{out.get('blocked_reason')!r}")
    check("code-exec run's blocked_reason names the harness pool",
          "harness pool" in (out3.get("blocked_reason") or "")
          and "codex.run" in (out3.get("blocked_reason") or ""),
          f"{out3.get('blocked_reason')!r}")

    # ── 8-13. The capability GRANT: bits, not just tool names. ────────────
    # The wall above stops an agent calling a tool it wasn't given. This stops
    # a GRANTED tool from doing more than the host authorized: effective bits
    # are entity_bits & grant_bits, so "read-only, do not write memory" is a
    # capability the agent never held rather than a line in its prompt.
    from mastercode.runtime.agent_grant import (
        parse_grant, unreachable_tools, effective_bits, describe, GRANT_FLOOR,
    )

    check("floor is zero — an agent holds nothing until elevated",
          GRANT_FLOOR == 0 and parse_grant([])[0] == 0)

    bits, unknown = parse_grant(["read_memory", "read_context"])
    check("grant parses capability NAMES to bits", bits == (1 | 2), f"bits={bits}")
    check("unknown capability names are surfaced, never silently dropped",
          parse_grant(["read_memory", "reed_memory"])[1] == ["reed_memory"])

    # admin / shell_exec are a WALL, not a sentence:
    # the parser refuses them, they contribute no bits, and the door's message
    # neither calls them typos nor lists them as valid.
    from mastercode.runtime.agent_grant import (
        NEVER_GRANTABLE, GRANTABLE_NAMES, grant_refusal, NAME_TO_BIT,
    )
    _adm_bits, _adm_unknown = parse_grant(["read_context", "admin", "SHELL_EXEC"])
    check("admin/shell_exec are refused by the parser and contribute no bits",
          _adm_bits == 1 and sorted(_adm_unknown) == ["SHELL_EXEC", "admin"],
          f"bits={_adm_bits} unknown={_adm_unknown}")
    check("the seven grantable names are exactly NAME_TO_BIT minus the forbidden two",
          set(GRANTABLE_NAMES) == set(NAME_TO_BIT) - NEVER_GRANTABLE
          and len(GRANTABLE_NAMES) == 7, f"{GRANTABLE_NAMES}")
    _ref = grant_refusal(["admin", "reed_memory"])
    check("refusal names the forbidden capability as never-holdable, not as a typo",
          "can never hold: admin" in _ref and "do not exist: reed_memory" in _ref, _ref)
    check("refusal's Valid: list never advertises admin or shell_exec",
          "admin" not in _ref.split("Valid:")[1] and "shell_exec" not in _ref.split("Valid:")[1],
          _ref)
    # The scope reads the web (an agent's work: agents are only for untrusted
    # work), so the grant is what the door judges.
    check("a dispatch granting admin is refused at the door",
          sa.dispatch_precheck(["read_url"], ["external_api", "admin"]) is not None
          and "can never hold" in (sa.dispatch_precheck(["read_url"], ["external_api", "admin"]) or ""))

    # The host's sentence, enforced: a read grant cannot write memory.
    ro = parse_grant(["read_context", "read_memory"])[0]
    ENTITY_ALL = 0xFFF
    check("read-only grant masks WRITE_MEMORY off the effective bits",
          not (effective_bits(ENTITY_ALL, ro) & 8),
          f"effective={describe(effective_bits(ENTITY_ALL, ro))}")
    check("grant can never widen past the entity",
          effective_bits(2, parse_grant(["write_memory", "read_memory"])[0]) == 2)

    # Contradiction caught at dispatch, not on turn 9. memory_curate needs
    # write_memory (bit 8); a read-only grant can never run it.
    bad = unreachable_tools(["memory_search", "memory_curate"], ro)
    check("contradictory grant is caught at dispatch with the missing capability",
          list(bad) == ["memory_curate"] and "write_memory" in bad.get("memory_curate", ""),
          f"{bad}")
    check("a coherent grant reports nothing unreachable",
          unreachable_tools(["memory_search"], ro) == {})

    # ── 14. End to end: an incoherent dispatch is refused before the loop. ──
    ex4, fr4 = [], []
    out4 = asyncio.run(_run(sa, scope=["inbox_send"], calls=[_tool_call("inbox_send")],
                            executed=ex4, frames=fr4, grant=["read_memory"]))
    check("incoherent dispatch refused before the agent runs",
          ex4 == [] and out4.get("rounds") == 0
          and "cannot succeed" in (out4.get("error") or ""),
          f"rounds={out4.get('rounds')} err={(out4.get('error') or '')[:90]}")
    check("refusal names the tool and the missing capability",
          "inbox_send" in (out4.get("error") or "")
          and "write_memory" in (out4.get("error") or ""))
    check("incoherent-dispatch blocked_reason names the grant, not the harness pool",
          "grant too narrow" in (out4.get("blocked_reason") or "")
          and "inbox_send" in (out4.get("blocked_reason") or "")
          and "harness pool" not in (out4.get("blocked_reason") or ""),
          f"{out4.get('blocked_reason')!r}")

    # ── 14b. The exact shape of one failure: a code-exec
    # tool AND an under-granted tool in one scope. The early grant refusal
    # used to report ONLY the grant problem — `build` (stripped) vanished from
    # denied_tools, so the record could not show the host's second mistake.
    ex5, fr5 = [], []
    out5 = asyncio.run(_run(sa, scope=["build", "inbox_send"],
                            calls=[_tool_call("inbox_send")],
                            executed=ex5, frames=fr5, grant=["read_memory"]))
    check("stripped code-exec name survives an early grant refusal",
          ex5 == [] and set(out5.get("denied_tools") or []) == {"build", "inbox_send"},
          f"denied={out5.get('denied_tools')}")
    check("that record's blocked_reason names BOTH walls",
          "harness pool" in (out5.get("blocked_reason") or "")
          and "grant too narrow" in (out5.get("blocked_reason") or ""),
          f"{out5.get('blocked_reason')!r}")

    # ── 15-20. PER-OP capability bits. ────────────────────────────────────
    # `required_bits` is declared per TOOL as the union of everything its ops
    # can do, so context_ingest(op='search') demanded WRITE_MEMORY. That forced
    # an over-grant on ~19 tools — and an over-grant is the signal a task was
    # not decomposed. Read ops now authorize as reads.
    from mastercode.runtime.agent_grant import (
        required_bits_for, min_required_bits_for,
    )
    from mastercode.runtime.meta_tool_caps import READ_SAFE_OPS, META_TOOL_CAPS

    # STRUCTURAL: no declared op may ever exceed its own tool's union. This is
    # the invariant that keeps the table tightening-only; without it a typo'd
    # entry would be a silent privilege grant.
    widened = [f"{tool}.{op}" for tool, ops in READ_SAFE_OPS.items()
               for op, b in ops.items()
               if b & ~int(META_TOOL_CAPS[tool]["required_bits"])]
    check("no declared op widens beyond its tool's union", not widened, f"{widened}")
    check("every tool with declared ops is a registered meta-tool",
          all(t in META_TOOL_CAPS for t in READ_SAFE_OPS),
          f"{[t for t in READ_SAFE_OPS if t not in META_TOOL_CAPS]}")

    check("a read op authorizes as a READ",
          required_bits_for("context_ingest", {"op": "search"}) == 2,
          f'{required_bits_for("context_ingest", {"op": "search"})}')
    check("a write op still demands the write bit",
          required_bits_for("context_ingest", {"op": "promote"}) == (2 | 8))
    check("an absent/unknown op falls back to the union (never looser)",
          required_bits_for("context_ingest", None) == (2 | 8)
          and required_bits_for("context_ingest", {"op": "nonsense"}) == (2 | 8))

    # The payoff: a read-only agent can hold context_ingest for searching, and
    # the dispatch is no longer refused as incoherent.
    ro2 = parse_grant(["read_memory"])[0]
    check("read-only grant makes a read/write tool REACHABLE for its read op",
          unreachable_tools(["context_ingest"], ro2) == {}
          and min_required_bits_for("context_ingest") == 2)

    # ── 13. The DOOR pre-check ─────────────────────────────────────────────────
    # The same refusal, computed synchronously at the dispatch tool call —
    # before any task row exists — so a mis-composed grant is the HOST's tool
    # error to fix in-turn, never a created-then-failed task delivered to the
    # user as "the task I dispatched didn't complete".
    # One such dispatch's scope was `artifact` alone, and since the 2026-09-19 sweep
    # that scope never reaches the grant wall: every op of `artifact` runs
    # through `operate`, so the door answers the earlier question first (see
    # the clerk-work block below). Since 2026-10-13 an agent's scope holds
    # only readers of untrusted content, so the grant gap is asserted on two
    # readers: inbox_read needs read_memory, which a web-only grant lacks.
    pre = sa.dispatch_precheck(["inbox_read", "research"], ["external_api"])
    check("door pre-check refuses a reader's grant gap, naming the tool and the cap",
          pre is not None and "inbox_read" in pre and "read_memory" in pre
          and "no task was created" in pre, str(pre))
    check("door pre-check surfaces unknown capability names",
          "do not exist" in (sa.dispatch_precheck(
              ["inbox_read", "research"], ["read_stuff"]) or ""))
    check("door pre-check passes a coherent dispatch",
          sa.dispatch_precheck(
              ["inbox_read", "research"], ["read_memory", "external_api"]) is None)
    # 2026-10-13: an in-platform tool beside a reader is refused, naming the
    # call to make and the reader-only scope to dispatch again with. Before,
    # one reader in the scope admitted everything beside it.
    _mixed = sa.dispatch_precheck(["artifact", "research"],
                                  ["read_context", "write_context", "external_api"])
    check("door refuses an in-platform tool beside a reader, with the reader-only scope",
          _mixed is not None and "artifact does your own platform's work" in _mixed
          and 'tool_scope: ["research"]' in _mixed, str(_mixed))
    check("door refuses a code-exec tool beside a reader, naming the build doors",
          "called directly" in (sa.dispatch_precheck(["build", "research"], ["external_api"]) or ""))
    check("a scope of only a code door reads nothing untrusted: refused, naming the build doors",
          "called directly" in (sa.dispatch_precheck(["build"], ["read_context"]) or ""))
    check("door refuses a write tool beside read_url (a dispatch's workspace_file write)",
          "workspace_file" in (sa.dispatch_precheck(["read_url", "workspace_file"],
                                                    ["external_api", "read_context"]) or ""))
    # 2026-10-13: an agent reads and reports, so a write capability is never
    # in its grant, whatever its scope.
    for _w in ("write_context", "write_memory", "execute_tool", "spawn_bot"):
        _wm = sa.dispatch_precheck(["web_search"], ["external_api", _w])
        check(f"door refuses a grant holding {_w}",
              _wm is not None and _w in _wm and "never holds a write capability" in _wm, str(_wm))
    check("the bug_review validator's own dispatch still passes the door",
          sa.dispatch_precheck(["bug_review"], ["external_api"]) is None)
    check("a research dispatch still passes the door",
          sa.dispatch_precheck(["research", "web_search", "read_url"], ["external_api"]) is None)
    check("an MCP server's tool still passes the door",
          sa.dispatch_precheck(["mcp:notion/search"], ["external_api"]) is None)
    # A read/write tool under a read grant stays reachable through its read
    # ops: the run-time wall still uses that for a caller that is not the door.
    from mastercode.runtime.agent_grant import unreachable_tools as _unreach
    check("read-op reachability still holds for the run-time wall",
          _unreach(["workspace_file"], 1) == {} and _unreach(["context_ingest"], 2) == {})
    check("a dispatch that only reads/extracts files is refused as clerk work",
          "deterministic op" in (sa.dispatch_precheck(["context_ingest"], ["read_memory"]) or ""))

    # ── 22. A capability denial AT EXECUTION scores 'blocked', never 'pass'.
    # The door passes workspace_file on its
    # read-safe minimum, so a write op under read_context reaches the gate
    # inside the executor. That denial used to come back as an ordinary tool
    # result: denied_tools stayed empty, the agent wrote "Blocked" in prose,
    # and _finalize_record scored the run pass/completed with nothing written.
    from mastercode.runtime.agent_dispatch import (
        _finalize_record, _blocked_error_text, AgentDispatch,
    )
    from mastercode.runtime.agent_grant import CAP_DENIAL_KEY
    import mastercode.runtime.tools as tools_mod

    # First bind the real gate's envelope: the capability wall in
    # tools._authorize_meta_tool must carry CAP_DENIAL_KEY, or everything
    # below tests scoped_agent against a shape the gate no longer emits.
    # The stub pool returns no entity_domain_access rows (entity bits = 0).
    # The kill switch is the real file check (absent on a build box), so
    # claim 2 is the claim that fires.
    _ok, _env = asyncio.run(tools_mod._authorize_meta_tool(
        "workspace_file", {"op": "write", "path": "docs/a.md"},
        entity_id=1, pool=_Pool(), session_id=None, grant_bits=1))
    check("the REAL capability gate denies a write op under read_context",
          _ok is False and isinstance(_env, dict) and _env.get("status") == "denied",
          f"{(_ok, _env)!r}"[:200])
    check("the real gate's denial envelope carries the bits the op needed",
          isinstance(_env, dict) and _env.get(CAP_DENIAL_KEY) == (1 | 4),
          f"{_env!r}"[:200])
    _gate_denial = {"status": "denied", CAP_DENIAL_KEY: 1 | 4, "result": (
        "'workspace_file' needs capability read_context, write_context, which "
        "your host did not grant you for this task. You hold: read_context.")}
    ex6, fr6 = [], []
    out6 = asyncio.run(_run(
        sa, scope=["workspace_file"],
        calls=[_tool_call("workspace_file", args='{"op": "write", "path": "docs/a.md"}')],
        executed=ex6, frames=fr6, grant=["read_context"],
        exec_result=_gate_denial, meta_names={"workspace_file"}))
    check("a workspace_file write passes the door (read-safe minimum) and reaches the gate",
          ex6 == ["workspace_file"], f"executed={ex6}")
    check("a gate denial lands in denied_tools",
          out6.get("denied_tools") == ["workspace_file"], f"{out6.get('denied_tools')}")
    check("blocked_reason names the wall AND the missing capability",
          "denied at execution" in (out6.get("blocked_reason") or "")
          and "workspace_file (missing write_context)" in (out6.get("blocked_reason") or ""),
          f"{out6.get('blocked_reason')!r}")
    # 2026-10-13: workspace_file is the Host's own work. Its denial inside an
    # agent tells the Host to do that part itself and never offers a wider
    # grant (until then: 'Retry with grant: ["read_context", "write_context"]').
    check("an in-platform tool's denial offers no retry grant",
          "Retry with grant" not in (out6.get("blocked_reason") or ""),
          f"{out6.get('blocked_reason')!r}")
    check("…and tells the Host to do that part itself",
          "workspace_file does your own platform's work: do that part yourself"
          in (out6.get("blocked_reason") or ""), f"{out6.get('blocked_reason')!r}")
    # A reader's denial still gets the additive retry, read capabilities only.
    ex6r, fr6r = [], []
    out6r = asyncio.run(_run(
        sa, scope=["research"], calls=[_tool_call("research", args='{"query": "x"}')],
        executed=ex6r, frames=fr6r, grant=["external_api"],
        exec_result={"status": "denied", CAP_DENIAL_KEY: 32 | 2 | 8,
                     "result": "needs read_memory, write_memory"},
        meta_names={"research"}))
    check("a reader's denial retries with the missing READ capability only",
          (out6r.get("blocked_reason") or "").endswith(
              'Retry with grant: ["read_memory", "external_api"]'),
          f"{out6r.get('blocked_reason')!r}")
    rec6 = _finalize_record(out6, "openrtr", "test/model")
    check("the record's verdict is blocked, not pass",
          rec6.get("verdict") == "blocked", f"verdict={rec6.get('verdict')}")
    check("the delivered error leads with the retry line, then the agent's report",
          _blocked_error_text(out6, rec6, "blocked").startswith("denied at execution")
          and "Agent's report: done" in _blocked_error_text(out6, rec6, "blocked"),
          _blocked_error_text(out6, rec6, "blocked")[:160])
    check("a run with no denial still scores pass (negative control for 22)",
          _finalize_record({"result": "done", "denied_tools": []}, "openrtr", "m")
          .get("verdict") == "pass")
    # A `denied` for a reason other than the capability wall (a path-jail
    # miss, a propose-mode editor, a trust floor) is an ordinary tool result
    # the agent can work around. It must NOT fail the run or blame the grant
    # — that would be the same lie pointed the other way.
    ex7, fr7 = [], []
    out7 = asyncio.run(_run(
        sa, scope=["inbox_send"], calls=[_tool_call("inbox_send")],
        executed=ex7, frames=fr7, grant=["write_memory"],
        exec_result={"status": "denied", "result": "path outside the codespace jail"}))
    check("a non-capability denial stays an ordinary result: not blocked, verdict pass",
          out7.get("denied_tools") == [] and not out7.get("blocked_reason")
          and _finalize_record(out7, "openrtr", "m").get("verdict") == "pass",
          f"denied={out7.get('denied_tools')} reason={out7.get('blocked_reason')!r}")
    # The gate's OTHER walls (kill switch, invariant, unregistered
    # tool, trust quarantine) stamp the key with 0: the run is blocked and
    # names the wall, but no grant would have passed it, so no retry line.
    ex9, fr9 = [], []
    out9 = asyncio.run(_run(
        sa, scope=["inbox_send"], calls=[_tool_call("inbox_send")],
        executed=ex9, frames=fr9, grant=["write_memory"],
        exec_result={"status": "denied", CAP_DENIAL_KEY: 0, "result": "kill switch active"}))
    check("a no-grant-can-fix wall is blocked, names the wall, offers no retry grant",
          out9.get("denied_tools") == ["inbox_send"]
          and "inbox_send (kill switch active)" in (out9.get("blocked_reason") or "")
          and "Retry with grant" not in (out9.get("blocked_reason") or "")
          and _finalize_record(out9, "openrtr", "m").get("verdict") == "blocked",
          f"{out9.get('blocked_reason')!r}")
    _ok_k, _env_k = asyncio.run(tools_mod._authorize_meta_tool(
        "not_a_registered_tool_xyz", {}, entity_id=1, pool=_Pool(), session_id=None,
        grant_bits=1))
    check("the real gate stamps an unregistered-tool wall with the key at 0",
          _ok_k is False and isinstance(_env_k, dict) and _env_k.get(CAP_DENIAL_KEY) == 0,
          f"{_env_k!r}"[:160])
    # Non-dict tool arguments must never crash the run on the denial path.
    ex8, fr8 = [], []
    out8 = asyncio.run(_run(
        sa, scope=["workspace_file"],
        calls=[_tool_call("workspace_file", args='["docs/a.md"]')],
        executed=ex8, frames=fr8, grant=["read_context"],
        exec_result=_gate_denial, meta_names={"workspace_file"}))
    check("a capability denial on non-dict args still records blocked, no crash",
          out8.get("denied_tools") == ["workspace_file"]
          and "missing write_context" in (out8.get("blocked_reason") or ""),
          f"{out8.get('blocked_reason')!r}")
    # The list row keeps blocked_reason beside error, so the error preview
    # shows the agent's report rather than repeating the reason.
    _row = {"error": rec6["blocked_reason"] + "\n\nAgent's report: done",
            "blocked_reason": rec6["blocked_reason"], "result": "done"}
    check("summary row's error preview drops the blocked_reason prefix",
          AgentDispatch.summarize_agent(_row).get("error") == "Agent's report: done",
          f"{AgentDispatch.summarize_agent(_row).get('error')!r}")

    # ── 23. Dedup mirrors the host lane. ────
    # A dispatched agent's image_generate died on a DNS miss; its brief said
    # "retry at most once"; the run-time dedup refused the retry as a repeat.
    # The host lane (session.py) caches a repeat of a SUCCESSFUL call and
    # refuses only the third — this lane now does the same, and a repeat of
    # an ERRORED call runs again once.
    same = '{"to": "self", "body": "hi"}'
    exd, frd = [], []
    outd = asyncio.run(_run(sa, scope=["inbox_send"],
                            calls=[_tool_call("inbox_send", "d1", same),
                                   _tool_call("inbox_send", "d2", same)],
                            executed=exd, frames=frd))
    d_results = [f for f in frd if f.get("event") == "tool_call_result"]
    check("a repeat of a SUCCESSFUL call is served from the first result, not re-executed",
          exd == ["inbox_send"] and len(d_results) == 2
          and d_results[1]["result"].get("cached") is True
          and d_results[1]["status"] == "ok",
          f"executed={exd} results={[r.get('result') for r in d_results]}")
    check("the cached repeat is recorded with the first call's status, not as an error",
          [a.get("result_status") for a in outd["tool_actions"]] == ["ok", "ok"],
          str(outd["tool_actions"]))

    exe, fre = [], []
    asyncio.run(_run(sa, scope=["inbox_send"],
                     calls=[_tool_call("inbox_send", "e1", same),
                            _tool_call("inbox_send", "e2", same)],
                     executed=exe, frames=fre,
                     exec_result={"status": "error", "result": "Cannot connect to host openrouter.ai:443"}))
    check("a repeat of an ERRORED call runs again once (a transient failure gets its retry)",
          exe == ["inbox_send", "inbox_send"], f"executed={exe}")

    exf, frf = [], []
    outf = asyncio.run(_run(sa, scope=["inbox_send"],
                            calls=[_tool_call("inbox_send", "f1", same),
                                   _tool_call("inbox_send", "f2", same),
                                   _tool_call("inbox_send", "f3", same)],
                            executed=exf, frames=frf,
                            exec_result={"status": "error", "result": "still down"}))
    f_results = [f for f in frf if f.get("event") == "tool_call_result"]
    check("the THIRD identical call is refused as dedup, whatever the first two returned",
          exf == ["inbox_send", "inbox_send"] and len(f_results) == 3
          and f_results[2]["result"].get("reason") == "dedup"
          and f_results[2]["result"].get("dedup_block") is True
          and "third time" in str(f_results[2]["result"].get("result")),
          f"executed={exf} last={f_results[-1]['result'] if f_results else None}")
    check("the refusal is recorded as an error on the run's tool_actions",
          [a.get("result_status") for a in outf["tool_actions"]] == ["error", "error", "error"],
          str(outf["tool_actions"]))
    # A tool that answers with a bare value (no envelope) still counts as a
    # success: its repeat is served from the cache, never re-executed.
    exg, frg = [], []
    asyncio.run(_run(sa, scope=["inbox_send"],
                     calls=[_tool_call("inbox_send", "g1", same),
                            _tool_call("inbox_send", "g2", same)],
                     executed=exg, frames=frg, exec_result="sent"))
    g_results = [f for f in frg if f.get("event") == "tool_call_result"]
    check("a bare (non-dict) successful result is cached on repeat, not re-executed",
          exg == ["inbox_send"] and len(g_results) == 2
          and g_results[1]["result"].get("cached") is True
          and g_results[1]["result"].get("result") == "sent",
          f"executed={exg} results={[r.get('result') for r in g_results]}")
    # A denied first call is cached too — the grant cannot change mid-run.
    exh, frh = [], []
    asyncio.run(_run(sa, scope=["inbox_send"],
                     calls=[_tool_call("inbox_send", "h1", same),
                            _tool_call("inbox_send", "h2", same)],
                     executed=exh, frames=frh,
                     exec_result={"status": "denied", "result": "path outside the jail"}))
    check("a denied first call is served from the cache on repeat (only an error earns a rerun)",
          exh == ["inbox_send"], f"executed={exh}")

    print()
    if FAILS:
        print(f"[grant-wall] {len(FAILS)} FAILED: {', '.join(FAILS)}")
        return 1
    print("[grant-wall] all gates pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
