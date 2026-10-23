#!/usr/bin/env python3
"""close_turn — the Host's structured end of turn
(brain/mastercode/runtime/close_turn.py).

Container-free: APP_ROOT=<repo> python3 scripts/test_close_turn.py. No pod, no
DB, no model; the pool is a stub that records every statement and RAISES on
SQL it does not know.

  1. schema: strict (additionalProperties false at every object level), the
     fields the plan names and no others, the enums
  2. registration: public schema + PUBLIC_META_TOOL_NAMES, META_TOOL_CAPS,
     lane 'verb', the every-turn verb list, every turn type, not operate's,
     not code-exec, stripped from agent scopes, a write tool for ephemeral
     sessions, the tools.py dispatch branch, the static-prompt rule
  3. normalize: keeps what the schema allows exactly as given (identifiers
     byte for byte), drops and names everything else, refuses over-long
     values instead of cutting them
  4. the terminal decision: terminal only with reply text AND close_turn as
     the only call
  5. the exit-guard path in session.py: the terminal block sits in front of
     the loop's text exit and turns the completion into a reply, so the
     claim guard and the late-nudge drain run on it; the non-terminal round
     tells the model its reply is owed; the STOP round still exists
  6. the metric: context_ledger.close_turn, and metadata.close_turn
  7. the writes, against a fake pool: topic + title, preferences per scope,
     the goal set / replaced / met, the agent wall

Exit 0 = all pass; exit 1 + [close-turn] FAIL lines otherwise.
"""

import asyncio
import inspect
import json
import os
import re
import sys
from tools_source import tools_source  # tools.py and the modules split out of it

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[close-turn] FAIL {name} {extra}")


from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS, PUBLIC_META_TOOL_NAMES  # noqa: E402
from mastercode.runtime import close_turn as ct  # noqa: E402

# ── 1. schema ─────────────────────────────────────────────────────────────
sch = PUBLIC_TOOL_SCHEMAS.get("close_turn") or {}
fn = sch.get("function") or {}
params = fn.get("parameters") or {}
check("schema: registered under its own name", fn.get("name") == "close_turn")
check("schema: the plan's fields and no others",
      set(params.get("properties") or {}) == set(ct._TOP_KEYS), sorted(params.get("properties") or {}))
check("schema: nothing is required at the top", params.get("required") == [])


def _objects(node, path="$"):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield path, node
        for k, v in node.items():
            yield from _objects(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _objects(v, f"{path}[{i}]")


_strict = [p for p, o in _objects(params) if "additionalProperties" in o]
check("schema: no additionalProperties on the wire (normalize drops unknown fields; ~230 chars)",
      not _strict, _strict)
props = params.get("properties") or {}
check("schema: faster.kind enum", props["faster"]["items"]["properties"]["kind"]["enum"]
      == ["prefetch", "skip", "path", "tool"])
check("schema: preference scope.kind enum",
      props["preferences"]["items"]["properties"]["scope"]["properties"]["kind"]["enum"]
      == ["global", "project", "app", "thread"])
check("schema: a preference needs text, reason, topic and scope (memory slots: the topic is "
      "the Host's own word for what it is about)",
      set(props["preferences"]["items"]["required"]) == {"text", "reason", "topic", "scope"})
check("schema: type.name required when type is sent", props["type"]["required"] == ["name"])
check("schema: handoff carries next_step / open_work / identifiers",
      set(props["handoff"]["properties"]) == {"next_step", "open_work", "identifiers"})
check("schema: goal carries set / met", set(props["goal"]["properties"]) == {"set", "met"})
check("schema: no surfaces key — exposed on every surface",
      "surfaces" not in (sch.get("_meta") or {}))

# ── 2. registration ───────────────────────────────────────────────────────
from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS  # noqa: E402
from mastercode.runtime.tool_lanes import lane_for, VERB_TOOLS, LANES  # noqa: E402
from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS  # noqa: E402
from mastercode.code_exec_guard import _is_code_exec_tool  # noqa: E402
from mastercode.runtime import turn_types as tt  # noqa: E402
from mastercode.runtime.triage import _META_TOOL_NAMES  # noqa: E402
from mastercode.runtime.tool_access import _is_write_tool, is_read_safe_tool  # noqa: E402
from mastercode.runtime import session as S  # noqa: E402

check("dispatch set: in PUBLIC_META_TOOL_NAMES (and the merged executor set)",
      "close_turn" in PUBLIC_META_TOOL_NAMES and "close_turn" in _META_TOOL_NAMES)
check("caps: write_context | write_memory",
      (META_TOOL_CAPS.get("close_turn") or {}).get("required_bits") == (4 | 8))
check("lane: 'verb' is a lane and close_turn is in it",
      "verb" in LANES and lane_for("close_turn", sch) == "verb" and "close_turn" in VERB_TOOLS)
_verbs = [((s.get("function") or {}).get("name")) for s in S._build_host_orchestration_tools()]
check("every turn: in the orchestration verb list", "close_turn" in _verbs, _verbs)
_ct_on_wire = next(s for s in S._build_host_orchestration_tools()
                   if (s.get("function") or {}).get("name") == "close_turn")
check("every turn: the wire schema carries no _meta", "_meta" not in _ct_on_wire)
check("every turn type: in turn_types.ALWAYS_ALLOWED", "close_turn" in tt.ALWAYS_ALLOWED)
check("never operate's", "close_turn" not in DIRECT_OP_TOOLS)
check("not code-exec", not _is_code_exec_tool("close_turn"))
check("a write tool (blocked in ephemeral sessions), never read-safe",
      _is_write_tool("close_turn") and not is_read_safe_tool("close_turn", sch))
_sa_src = open(os.path.join(APP_ROOT, "brain/mastercode/runtime/scoped_agent.py")).read()
check("agents: scoped_agent strips VERB_TOOLS from every scope",
      "scope_set & _VERB_TOOLS" in _sa_src and "scope_set -= _verbs_only_host" in _sa_src)
_tools_src = tools_source(APP_ROOT)
check("tools.py: dispatch branch runs close_turn.execute",
      'elif name == "close_turn":' in _tools_src and "_close_turn_execute(args" in _tools_src)

# The static prompt: the rule rides the cached orchestrator block, byte-stable.
p1 = S._build_orchestrator_prompt([sch])
p2 = S._build_orchestrator_prompt([sch])
check("prompt: the close_turn rule is in the orchestrator block", S._CLOSE_TURN_RULE in p1)
check("prompt: byte-stable across builds", p1 == p2)
check("prompt: the rule holds no per-turn value (no digits)", not re.search(r"\d", S._CLOSE_TURN_RULE))
check("prompt: a verb is not counted as an agent's tool",
      "0 of these read untrusted content" in p1, p1[-400:])
p3 = S._build_orchestrator_prompt([sch], withheld={"operate", "dispatch"},
                                  unvalidated_route=("x", "y"))
check("prompt: the rule rides the withheld-route branch too", S._CLOSE_TURN_RULE in p3)

# ── 3. normalize ──────────────────────────────────────────────────────────
full = {
    "type": {"name": "desktop edit", "is_new": True},
    "useful_calls": ["artifact", "call_7"], "dead_calls": ["orient"],
    "faster": [{"kind": "prefetch", "value": "artifact read of the desktop"}],
    "topic": "Desktop redesign", "title": "Desktop redesign",
    "preferences": [{"text": "Dark themes only", "reason": "said so twice",
                     "scope": {"kind": "global"}}],
    "goal": {"set": "Ship the new desktop", "met": False},
    "handoff": {"next_step": "wire the taskbar", "open_work": ["icons"],
                "identifiers": ["  /tmp/work/a b.txt", "artifact:9f2"]},
}
sig, errs = ct.normalize(full)
check("normalize: a full valid signal is kept as given", sig == full and errs == [], (sig, errs))
check("normalize: identifiers verbatim (leading spaces kept)",
      sig["handoff"]["identifiers"][0] == "  /tmp/work/a b.txt")
sig, errs = ct.normalize({"topic": "x", "mood": "happy", "faster": [{"kind": "guess", "value": "v"}],
                          "preferences": [{"text": "t", "scope": {"kind": "global"}},
                                          {"text": "t", "reason": "r", "scope": {"kind": "type"}},
                                          {"text": "t", "reason": "r", "scope": {"kind": "world"}}],
                          "goal": {"met": "yes"}, "type": {"is_new": True}})
check("normalize: unknown field dropped and named", "mood" not in sig and any("mood" in e for e in errs))
check("normalize: bad faster kind dropped", "faster" not in sig)
check("normalize: preference without reason / bad scope dropped", "preferences" not in sig)
check("normalize: non-bool goal.met dropped", "goal" not in sig)
check("normalize: type without name dropped", "type" not in sig)
check("normalize: the valid remainder stays", sig == {"topic": "x"}, sig)
sig, errs = ct.normalize({"topic": "t" * 500, "handoff": {"identifiers": ["x" * 5000]}})
check("normalize: over-long values refused, never cut", sig == {} and len(errs) == 2, (sig, errs))
check("normalize: non-object args", ct.normalize("nope") == ({}, ["arguments must be an object"]))


# ── 4. the terminal decision ──────────────────────────────────────────────
def _tc(name, args="{}"):
    return {"id": f"c_{name}", "type": "function", "function": {"name": name, "arguments": args}}


check("terminal: reply text + close_turn alone", ct.is_terminal("Done — here it is.", [_tc("close_turn")]))
check("not terminal: no reply text", not ct.is_terminal("", [_tc("close_turn")]))
check("not terminal: whitespace is not a reply", not ct.is_terminal("  \n ", [_tc("close_turn")]))
check("not terminal: other calls beside it",
      not ct.is_terminal("Here.", [_tc("recall"), _tc("close_turn")]))
check("not terminal: no close_turn", not ct.is_terminal("Here.", [_tc("recall")]))
check("arrival: None without close_turn", ct.arrival("x", [_tc("recall")]) is None)
check("arrival: shape",
      ct.arrival("", [_tc("recall"), _tc("close_turn")]) == {"prose": False, "alone": False, "terminal": False}
      and ct.arrival("hi", [_tc("close_turn")]) == {"prose": True, "alone": True, "terminal": True})
check("parse_args: JSON string", ct.parse_args(_tc("close_turn", '{"topic": "a"}')) == ({"topic": "a"}, True))
check("parse_args: broken JSON runs as {}", ct.parse_args(_tc("close_turn", "{nope")) == ({}, False))

# ── 5. the exit-guard path (session.py, structural: send_message needs a pod) ──
src = inspect.getsource(S)
# The terminal `if` (it also ends the turn on the go-ahead repair's
# own answer when that is close_turn alone, with no text — "adding nothing").
i_term = src.find("            if tool_calls and (\n                    _ct.is_terminal(result.get(\"content\"), tool_calls)")
i_exit = src.find("            if not tool_calls:\n                # This completion ends the reply", i_term)
i_claim = src.find("find_unbacked_start(", i_exit)
i_late = src.find("if has_pending_nudge(sess.session_id):", i_exit)
i_break = src.find("break  # Normal text response, exit loop", i_exit)
check("exit path: the terminal block sits directly in front of the text exit",
      0 < i_term < i_exit and src[i_term:i_exit].count("\n            if ") == 0, (i_term, i_exit))
term_block = src[i_term:i_exit]
check("exit path: it turns the completion into a reply (tool_calls cleared on result)",
      'result = {**result, "tool_calls": None}' in term_block and "tool_calls = None" in term_block)
check("exit path: it runs the writes and records arrival + signal",
      "_run_close_turn(" in term_block and "_close_turn_arrivals.append(" in term_block
      and "_ct.normalize(_ct_args)" in term_block)
check("exit path: it makes no model call and appends nothing to the transcript",
      not any(c in term_block for c in ("call_model", "_call_entity_model", "_call_with_overflow_retry"))
      and "grok_messages.append" not in term_block)
check("exit path: the claim guard and the late-nudge drain follow, before the break",
      i_exit < i_claim < i_late < i_break, (i_claim, i_late, i_break))
check("round path: close_turn's arrival is recorded before any gate can answer it",
      src.find("if func_name == _ct.TOOL_NAME:\n                    _close_turn_arrivals.append(")
      < src.find("# ── Dedup gate"))
check("round path: the result tells the model its reply is owed, in words that fit what went out",
      '"next": _ct.next_step(' in src
      and ct.next_step("") == ct.WRITE_YOUR_REPLY and "write your reply" in ct.WRITE_YOUR_REPLY.lower()
      and ct.next_step("Here is the plan.") == ct.FINISH_YOUR_REPLY
      and "already reached the user" in ct.FINISH_YOUR_REPLY)

# The exit path, on the syntax tree of send_message's loop: M2 (a `break`
# right after `tool_calls = None`, skipping the claim guard and the late-nudge
# drain) and M3 (the claim guard skipped when close_turn arrived) both went
# green against the string checks above in review; these catch them.
import ast  # noqa: E402
_tree = ast.parse(src)
_term_if = next(n for n in ast.walk(_tree) if isinstance(n, ast.If)
                and "_ct.is_terminal" in ast.unparse(n.test))
_exits = [type(n).__name__ for b in _term_if.body for n in ast.walk(b)
          if isinstance(n, (ast.Break, ast.Continue, ast.Return, ast.Raise))]
check("exit path: the terminal block has no break/continue/return of its own", not _exits, _exits)
_parent = next(n for n in ast.walk(_tree) for f in ("body", "orelse")
               if isinstance(getattr(n, f, None), list) and _term_if in getattr(n, f))
_sib = next(f for f in ("body", "orelse") if _term_if in getattr(_parent, f))
_after = getattr(_parent, _sib)[getattr(_parent, _sib).index(_term_if) + 1]
check("exit path: the very next statement is the text exit `if not tool_calls:`",
      isinstance(_after, ast.If) and ast.unparse(_after.test) == "not tool_calls", ast.unparse(_after)[:80])
_claim_if = next(n for n in _after.body if isinstance(n, ast.If)
                 and "find_unbacked_start" in ast.unparse(n))
check("exit path: the claim guard's condition knows nothing of close_turn",
      "_ct" not in ast.unparse(_claim_if.test) and "close_turn" not in ast.unparse(_claim_if.test),
      ast.unparse(_claim_if.test))
check("exit path: the late look for the person's words is inside the same exit (it leaves them queued "
      "for the drain)",
      "has_pending_nudge(sess.session_id)" in ast.unparse(_after))

# "One more call" is structural: writes once; the next round's call is the
# reply, and a tool call in it is never run. The tools array stays on that
# call, so the cached prefix holds.
def _ifs(node, test):
    return [n for n in ast.walk(node) if isinstance(n, ast.If) and ast.unparse(n.test) == test]


def _assigns(nodes, name, value):
    return [n for b in nodes for n in ast.walk(b) if isinstance(n, ast.Assign)
            and any(ast.unparse(t) == name for t in n.targets) and ast.unparse(n.value) == value]


_tw = _ifs(_term_if, "_ct_written")
check("once: the terminal path runs the writes only when none ran, and marks them run",
      len(_tw) == 1 and _assigns(_tw[0].orelse, "_ct_written", "True")
      and not _assigns(_tw[0].body, "_ct_written", "True"), ast.unparse(_term_if)[:300])
_round_if = next(n for n in ast.walk(_tree) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "func_name == _ct.TOOL_NAME"
                 and "_run_close_turn" in ast.unparse(n))
_rw = _ifs(_round_if, "_ct_written")
check("once: the round path runs the writes only when none ran, and marks them run",
      len(_rw) == 1 and _assigns(_rw[0].orelse, "_ct_written", "True"), ast.unparse(_round_if)[:300])
_arr_if = next(n for n in ast.walk(_tree) if isinstance(n, ast.If)
               and ast.unparse(n.test) == "func_name == _ct.TOOL_NAME"
               and "_close_turn_arrivals.append" in ast.unparse(n))
check("once: the reply is owed from the moment close_turn arrives (before any gate or cache)",
      [ast.unparse(x) for x in _arr_if.body][-1] == "_ct_reply_owed = True"
      and src.find("_ct_reply_owed = True") < src.find("# ── Dedup gate"))
_first = _ifs(_arr_if, "_close_turn_signal is None")
check("once: the first close_turn's signal is the turn's (a later one never replaces it)",
      len(_first) == 1 and "_close_turn_signal, _ = _ct.normalize(func_args)" in ast.unparse(_first[0]))
# Review r11 of no-go-ahead: the text was recorded where it was sent (the
# single recorder); close_turn's arrival marks that piece as the closing reply,
# whatever rode beside it, so the guard reads it with what follows.
check("once: text that came with a non-terminal close_turn is kept in the saved turn, as the closing reply",
      "_cur_piece[0]['force'] = 'reply'" in ast.unparse(_arr_if)
      and "_record_part" not in ast.unparse(_arr_if))
_owed_if = _ifs(_tree, "_ct_reply_owed")
check("once: the round's own model call is marked as the one that owes the reply",
      len(_owed_if) == 1 and _assigns([_owed_if[0]], "_ct_reply_result", "result")
      and _assigns([_owed_if[0]], "_ct_reply_owed", "False")
      and 0 < src.find("tools=_tools_for_round,") < src.find("            if _ct_reply_owed:\n"))
check("cache: close_turn never takes the tools off a call",
      "_tools_for_round = None" not in ast.unparse(_owed_if[0])
      and src.count("_tools_for_round = None") == 1)
_due = _ifs(_tree, "_ct_reply_result is not None and result is _ct_reply_result")
_due_src = ast.unparse(_due[0]) if _due else ""
_ask = _ifs(_due[0], "not _ct_closed_asked and _ct_room") if _due else []
_ask_src = ast.unparse(_ask[0]) if _ask else ""
check("once: a tool call in the reply is not run — with text the text is the reply",
      len(_due) == 1 and "tool_calls = None" in _due_src and "_ct_reply_result = None" in _due_src
      and "'tool_calls': None" in _due_src, _due_src[:400])
check("once: without text the reply is asked for once more in words, tools kept (cache), then the "
      "forced-text call",
      len(_ask) == 1 and "_ct.TURN_CLOSED" in _ask_src and "tools=tools" in _ask_src
      and "_ct_reply_result = result" in _ask_src and "continue" in _ask_src
      and "break" in _due_src.split("if not _ct_closed_asked and _ct_room")[1], _due_src[:600])
check("once: the second ask happens once — the latch is set in it, and the allowance is checked first",
      _assigns([_ask[0]], "_ct_closed_asked", "True") if _ask else False)
_room = [n for n in ast.walk(_due[0]) if isinstance(n, ast.If)
         and ast.unparse(n.test) == "_spend.would_stop(_ta.new_input_tokens(grok_messages, _msgs_mark[0]), _reply_reserve)"
         and _assigns([n], "_ct_room", "False") and _assigns([n], "_spend_stopped", "True")] if _due else []
check("once: out of allowance, no second ask (the spend check decides _ct_room first)",
      len(_room) == 1 and len(_ask) == 1
      and _due_src.find("_spend.would_stop(") < _due_src.find("if not _ct_closed_asked and _ct_room")
      and "_spend.check(" not in _due_src,
      _due_src[:300])
_err = _ifs(_ask[0], "'error' in result and (not result.get('content'))") if _ask else []
check("once: the second ask is metered like any call, and a provider error on it is returned",
      "_acc_usage(result)" in _ask_src and len(_err) == 1
      and [ast.unparse(x) for x in _err[0].body] == ["return await _end_on_error(result)"], _ask_src[-400:])
# would_stop itself is driven against check() in test_turn_allowance.py §0.
_ws_if = [n for n in ast.walk(_due[0]) if isinstance(n, ast.If)
          and ast.unparse(n.test).startswith("_spend.would_stop(")] if _due else []
check("once: an allowance stop before the extra ask is logged as one",
      len(_ws_if) == 1 and any(isinstance(n, ast.Call) and ast.unparse(n.func) == "log.warning"
                               and "turn allowance reached" in ast.unparse(n) for n in ast.walk(_ws_if[0])))
_blk = [n for n in ast.walk(_due[0]) if isinstance(n, ast.Call) and ast.unparse(n.func) == "_tcl_record"] if _due else []
check("once: every call after the turn closed is a tool-ledger row, reason blocked, status blocked",
      len(_blk) == 1 and "reason=_tcl_blocked" in ast.unparse(_blk[0])
      and "status='blocked'" in ast.unparse(_blk[0]) and "BLOCKED as _tcl_blocked" in _due_src)
_nr = _ifs(_due[0], "_dc_name == _ct.TOOL_NAME") if _due else []
check("once: a close_turn among them is an arrival marked not_run and never terminal",
      len(_nr) == 1 and "'not_run': True" in ast.unparse(_nr[0]) and "'terminal': False" in ast.unparse(_nr[0]))
check("honest STOP: leaving the loop because the turn closed is marked, so the wording follows",
      _assigns(_due, "_ct_left_closed", "True") if _due else False)
_stop = _ifs(_tree, "_ct_left_closed")
check("honest STOP: after close_turn the forced-text call says the turn is closed, not that rounds ran out",
      len(_stop) == 1 and "_stop_text = _ct.TURN_CLOSED" in ast.unparse(_stop[0].body)
      and "all available tool rounds" in ast.unparse(_stop[0].orelse),
      ast.unparse(_stop[0])[:300] if _stop else "")
# Every completion's streamed text is kept as part of the reply
# (the thread IS the chain of thought) — the owed reply's included, tool calls
# or not.
# The recorder (_send, _settle) is the turn's TurnReplies, in its own module;
# it reads close_turn's owed reply through the getter the turn hands it.
import mastercode.runtime.turn_replies as _TR  # noqa: E402
_rec_src = inspect.getsource(_TR)
check("nudge: a nudge that lands during the owed reply keeps its text as the reply",
      "_seen_reply = strip_wm(result.get('content') or '')" in src.replace('"', "'")
      and "elif more and res.get('tool_calls') and res is not self.ct_reply_result():"
      in _rec_src.replace('"', "'"))
_send_fn = next(n for n in ast.walk(ast.parse(_rec_src))
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send")
check("once: only text is kept (never None — the save joins strings): the recorder sends and records "
      "nothing for an empty piece",
      ast.unparse(_send_fn.body[1]) == "if not text:\n    return", ast.unparse(_send_fn)[:300])
check("live = reload: close_turn sends no tool-call frames (no chip that vanishes on reload)",
      src.count("if _pcb is not None and func_name != _ct.TOOL_NAME:") == 2)
check("once: that check runs before the terminal check reads the same completion",
      src.find("result is _ct_reply_result") < src.find("_ct.is_terminal(result.get(\"content\"), tool_calls)"))
check("wall: the write-tag is set once per turn and never reset",
      src.count("sess.write_app_id =") == 1)
check("once: a repeated close_turn is answered, never re-run",
      "was not run" in ct.ALREADY_RECORDED)
check("round path: no chip, no artifact",
      "if func_name != _ct.TOOL_NAME:\n                    tool_actions.append(ta_entry)" in src
      and "if func_name != _ct.TOOL_NAME:\n                        _turn_artifacts.append(" in src)
check("no silent turn: the STOP round on tool_calls without content is still there",
      'if result.get("tool_calls") and not _ct.has_prose(result.get("content")):' in src
      # (tool search step C: the call's tools ride in _exit_wire, which is
      # {"tools": _exit_tools} outside search mode)
      and "**_exit_wire,  # escalate alone, or none -- force a text response" in src
      and '_exit_wire: dict = {"tools": _exit_tools}' in src)

# ── 6. the metric ─────────────────────────────────────────────────────────
e = ct.ledger_entry([])
check("metric: a turn without close_turn", e == {"arrived": False, "with_prose": None, "terminal": False, "arrivals": []})
arrs = [{"prose": False, "alone": True, "terminal": False, "model": "k3", "round": 2},
        {"prose": True, "alone": True, "terminal": True, "model": "k3", "round": 3}]
e = ct.ledger_entry(arrs)
check("metric: first arrival without prose, then the terminal one",
      e["arrived"] and e["with_prose"] is False and e["terminal"] and e["arrivals"] == arrs, e)
check("metric: rides context_ledger", '"close_turn": _ct.ledger_entry(_close_turn_arrivals),' in src)
check("metric: each arrival names the served model",
      src.count('"model": _served.get("model") or _inference_model,\n') >= 2)
check("signal: stored on the assistant row as metadata.close_turn",
      '_assistant_tags["close_turn"] = _close_turn_signal' in src)


# ── 7. the writes, against a fake pool ────────────────────────────────────
THREAD = "0f8b1c3e-2d4a-4b5c-9e6f-7a8b9c0d1e2f"
SCOPE = {"kind": "thread", "ref": THREAD}


class FakePool:
    def __init__(self, *, open_goals=None, held_pref=None, threads=(THREAD,), apps=("circles",),
                 projects=(12,)):
        self.calls: list[tuple[str, tuple]] = []
        self._next = 100
        self.open_goals = open_goals or []
        # held_pref: None, one {"id", "about"} or a list of them — the same
        # words held live
        self.held_pref = held_pref
        self.held_rows = ([held_pref] if isinstance(held_pref, dict) else list(held_pref or []))
        self.threads, self.apps, self.projects = threads, apps, projects

    def _log(self, sql, args):
        self.calls.append((" ".join(sql.split()), args))

    async def execute(self, sql, *args):
        self._log(sql, args)
        s = " ".join(sql.split())
        if s.startswith("UPDATE circles_threads") or s.startswith("UPDATE entity_memory"):
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {s[:80]}")

    async def fetchval(self, sql, *args):
        self._log(sql, args)
        s = " ".join(sql.split())
        if "FROM projects" in s:
            return 1 if args[0] in self.projects else None
        if "FROM circles_threads" in s:
            return 1 if str(args[0]) in self.threads else None
        raise AssertionError(f"unexpected fetchval: {s[:80]}")

    async def fetchrow(self, sql, *args):
        self._log(sql, args)
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO entity_memory"):
            self._next += 1
            return {"id": self._next}
        if s.startswith("SELECT id, memory_type, status, t_valid_until, metadata, slot_topic FROM entity_memory"):
            h = next((r for r in self.held_rows if r["id"] == args[0]), {})
            return {"id": args[0], "memory_type": "preference", "status": "active", "slot_topic": None,
                    "t_valid_until": None, "metadata": json.dumps({"about": h.get("about")} if h.get("about") else {})}
        raise AssertionError(f"unexpected fetchrow: {s[:80]}")

    async def fetch(self, sql, *args):
        self._log(sql, args)
        s = " ".join(sql.split())
        if s.startswith("SELECT id, content FROM entity_memory WHERE entity_id = $1 AND memory_type = 'goal'"):
            return list(self.open_goals)
        if "SELECT DISTINCT metadata->'source_context'->>'ref'" in s:
            return [{"ref": a} for a in self.apps]
        if s.startswith("SELECT id, metadata->'about' AS about, metadata->'source_context' AS sc"):
            assert "t_valid_until IS NULL" in s and "status = 'active'" in s, s
            return [{"sc": None, "held_in": None, **r} for r in self.held_rows]
        raise AssertionError(f"unexpected fetch: {s[:80]}")

    def inserts(self):
        return [a for s, a in self.calls if s.startswith("INSERT INTO entity_memory")]


async def _no_embed(*_a, **_k):
    return None


import mastercode.memory_engine as ME  # noqa: E402
ME._get_memory_embedding = _no_embed


async def _run(pool, args, *, session_id="s1", thread_id=THREAD, turn_type=None, scope=SCOPE):
    tok = ME.CURRENT_TURN_SCOPE.set({"session_id": session_id, "scope": scope})
    try:
        return await ct.execute(args, entity_id=9003, pool=pool, session_id=session_id,
                                thread_id=thread_id, turn_type=turn_type)
    finally:
        ME.CURRENT_TURN_SCOPE.reset(tok)


import mastercode.circles_threads as _CTmod  # noqa: E402
_forgets: list = []


async def _forget_rec(pool, entity_id, scope):
    _forgets.append(dict(scope))
    return False
_CTmod.forget_if_deleted = _forget_rec
pool = FakePool(open_goals=[{"id": 40, "content": "Old goal"}])
res = asyncio.run(_run(pool, {
    "topic": "Desktop redesign", "title": "My desktop",
    "preferences": [
        {"text": "Dark themes only", "reason": "said it twice", "scope": {"kind": "global"}},
        {"text": "Use the grid layout", "reason": "asked for it", "scope": {"kind": "thread"}},
        {"text": "Short replies here", "reason": "asked", "scope": {"kind": "project"}},
        {"text": "Big icons in Circles", "reason": "asked", "scope": {"kind": "app", "ref": "Circles"}},
    ],
    "goal": {"set": "Ship the new desktop", "met": True},
    "handoff": {"identifiers": ["artifact:9f2"]},
}, turn_type={"id": "tt-1", "name": "desktop edit"}))
check("execute: ok", res.get("status") == "ok", res)
w = res["result"]["writes"]
topic_upd = [a for s, a in pool.calls if s.startswith("UPDATE circles_threads")]
check("topic: one UPDATE via set_thread_topic with topic + title",
      len(topic_upd) == 1 and topic_upd[0] == (THREAD, 9003, "Desktop redesign", "My desktop"), topic_upd)
check("topic: the COALESCE rule — a user's title is never overwritten",
      any("title = COALESCE(title, $4)" in s and "topic = COALESCE($3, topic)" in s
          for s, _ in pool.calls if s.startswith("UPDATE circles_threads")))
check("topic: reported written", w.get("topic") == "written", w)
ins = pool.inserts()
prefs = [a for a in ins if a[2] == "preference"]
check("preferences: three stored (the project one has no project here)", len(prefs) == 3, prefs)
m = [json.loads(a[-1]) for a in prefs]
check("preference global: the decision is about=global; where it was said is the turn's own thread",
      m[0].get("about") == {"kind": "global"} and m[0].get("source_context") == SCOPE
      and m[0]["reason"] == "said it twice" and m[0]["written_by"] == "close_turn"
      and m[0].get("scoped_by") == "host", m[0])
check("preference thread: a scope with no ref takes the turn's own thread, checked live",
      m[1].get("about") == {"kind": "thread", "ref": THREAD} and m[1].get("source_context") == SCOPE
      and any("FROM circles_threads" in c and "deleted_at IS NULL" in c for c, _ in pool.calls), m[1])
check("preference app: the ref is matched to an app with turns and stored as the turns carry it",
      m[2].get("about") == {"kind": "app", "ref": "circles"}, m[2])
check("preference: source_session is the turn's session", all(a[3] == "s1" for a in prefs))
check("preference project without a ref outside a project: refused, named",
      w["preferences"][2]["status"] == "error" and "project" in w["preferences"][2]["error"], w["preferences"])
goals = [a for a in ins if a[2] == "goal"]
check("goal: one row, scoped to the turn's own conversation",
      len(goals) == 1 and json.loads(goals[0][-1]) == {"source_context": SCOPE, "written_by": "close_turn"}, goals)
sup = [a for s, a in pool.calls if s.startswith("UPDATE entity_memory SET t_valid_until = NOW()")]
check("goal: the conversation's older open goal is superseded by the new one",
      len(sup) == 1 and sup[0][1] == 40 and sup[0][2] == w["goal"]["set"]["id"], (sup, w.get("goal")))
res_upd = [a for s, a in pool.calls if s.startswith("UPDATE entity_memory SET resolved_at = NOW()")]
check("goal met: resolved_at on the new goal only, stamped with the scope",
      len(res_upd) == 1 and res_upd[0][1] == [w["goal"]["set"]["id"]]
      and json.loads(res_upd[0][2])["ref"] == THREAD, res_upd)
check("a thread's writes are followed by forget_if_deleted (a delete during the turn takes them)",
      _forgets == [SCOPE], _forgets)
check("result: names what was recorded",
      res["result"]["recorded"] == ["goal", "handoff", "preferences", "title", "topic"], res["result"])

# met alone resolves the conversation's open goals; nothing inserted.
pool = FakePool(open_goals=[{"id": 41, "content": "A"}, {"id": 42, "content": "B"}])
res = asyncio.run(_run(pool, {"goal": {"met": True}}))
res_upd = [a for s, a in pool.calls if s.startswith("UPDATE entity_memory SET resolved_at = NOW()")]
check("goal met alone: resolves this conversation's open goals, writes no row",
      not pool.inserts() and len(res_upd) == 1 and sorted(res_upd[0][1]) == [41, 42], pool.calls)

# The same goal again: held, no second row.
pool = FakePool(open_goals=[{"id": 43, "content": "Ship it"}])
res = asyncio.run(_run(pool, {"goal": {"set": "Ship it"}}))
check("goal: the same open goal is held, not written twice",
      not pool.inserts() and res["result"]["writes"]["goal"]["set"] == {"status": "already_held", "id": 43},
      res)

# A preference already held (same words, same decision): no second row.
pool = FakePool(held_pref={"id": 77, "about": {"kind": "global"}})
res = asyncio.run(_run(pool, {"preferences": [{"text": "Dark", "reason": "r", "scope": {"kind": "global"}}]}))
check("preference: already held is not written again",
      not pool.inserts() and res["result"]["writes"]["preferences"] == [{"status": "already_held", "id": 77}], res)
# The same words held undecided in a project, said again as global: widened by the one writer.
pool = FakePool(held_pref={"id": 78, "about": None, "sc": {"kind": "project", "ref": "12"}})
res = asyncio.run(_run(pool, {"preferences": [{"text": "Dark", "reason": "r", "scope": {"kind": "global"}}]}))
upd = [(c, a) for c, a in pool.calls if c.startswith("UPDATE entity_memory SET metadata = COALESCE(metadata")]
wr = res["result"]["writes"]["preferences"][0]
check("preference: widening to global rescopes the held row through apply_memory_scope, never duplicated",
      not pool.inserts() and wr["status"] == "rescoped" and wr.get("from") == "where said"
      and len(upd) == 1 and json.loads(upd[0][1][2]).get("about") == {"kind": "global"}
      and "FOR SHARE" in upd[0][0], res)
# Several held rows, none reaching everywhere, said as global: the NEWEST widens (the merge
# pass keeps the newer row, so an older widened row would be folded away).
pool = FakePool(held_pref=[{"id": 120, "about": None, "sc": {"kind": "project", "ref": "12"}},
                           {"id": 119, "about": {"kind": "app", "ref": "circles"}}])
res = asyncio.run(_run(pool, {"preferences": [{"text": "Dark", "reason": "r", "scope": {"kind": "global"}}]}))
check("preference: widening picks the newest held row",
      res["result"]["writes"]["preferences"][0].get("id") == 120
      and res["result"]["writes"]["preferences"][0]["status"] == "rescoped", res)
# Held global: it already rides everywhere, so a narrower restatement writes nothing.
pool = FakePool(held_pref={"id": 79, "about": {"kind": "global"}})
res = asyncio.run(_run(pool, {"preferences": [{"text": "Dark", "reason": "r", "scope": {"kind": "thread"}}]}))
check("preference: held global already covers a narrower restatement — nothing written, nothing moved",
      not pool.inserts() and res["result"]["writes"]["preferences"][0]["status"] == "already_held"
      and not [c for c, _ in pool.calls if c.startswith("UPDATE entity_memory")], res)
# Held in another place: the new place gets a row of its own; the held row is not moved.
pool = FakePool(held_pref={"id": 80, "about": {"kind": "project", "ref": "12"}})
res = asyncio.run(_run(pool, {"preferences": [{"text": "Dark", "reason": "r", "scope": {"kind": "thread"}}]}))
check("preference: a restatement in another place adds that place and never moves the held row",
      len(pool.inserts()) == 1 and res["result"]["writes"]["preferences"][0]["status"] == "stored"
      and not [c for c, _ in pool.calls if c.startswith("UPDATE entity_memory SET metadata")], res)
# Held undecided where this turn is: it already reaches here — nothing written, no duplicate.
pool = FakePool(held_pref={"id": 81, "about": None, "sc": SCOPE})
res = asyncio.run(_run(pool, {"preferences": [{"text": "Dark", "reason": "r", "scope": {"kind": "thread"}}]}))
check("preference: held undecided in this very place already reaches it — no second row",
      not pool.inserts() and res["result"]["writes"]["preferences"][0] == {"status": "already_held", "id": 81}, res)
# Held undecided, said again where no scope applied (held_in's unscoped marker): it reaches everywhere.
pool = FakePool(held_pref={"id": 82, "about": None, "sc": {"kind": "project", "ref": "12"},
                           "held_in": [{"kind": "app", "ref": "memory", "at": "x"}]})
res = asyncio.run(_run(pool, {"preferences": [{"text": "Dark", "reason": "r", "scope": {"kind": "thread"}}]}))
check("preference: a held row said again with no scope already reaches everywhere — nothing written",
      not pool.inserts() and res["result"]["writes"]["preferences"][0]["status"] == "already_held", res)
# Held undecided with no provenance at all is global by absence.
pool = FakePool(held_pref={"id": 83, "about": None})
res = asyncio.run(_run(pool, {"preferences": [{"text": "Dark", "reason": "r", "scope": {"kind": "thread"}}]}))
check("preference: a held row with no scope at all is global — nothing written",
      not pool.inserts() and res["result"]["writes"]["preferences"][0]["status"] == "already_held", res)
# Several held rows: the one that already has this decision answers, whatever its age.
pool = FakePool(held_pref=[{"id": 109, "about": None, "sc": {"kind": "thread", "ref": "another-thread"}},
                           {"id": 108, "about": {"kind": "thread", "ref": THREAD}}])
res = asyncio.run(_run(pool, {"preferences": [{"text": "Dark", "reason": "r", "scope": {"kind": "thread"}}]}))
check("preference: among several held rows, the one with this decision is the answer",
      not pool.inserts() and res["result"]["writes"]["preferences"][0] == {"status": "already_held", "id": 108}, res)
# A ref that names nothing this Host has is refused before any write.
for sc, why in (({"kind": "app", "ref": "finanse"}, "an app with no turns"),
                ({"kind": "project", "ref": "999"}, "a project this Host lacks"),
                ({"kind": "thread", "ref": "0f8b1c3e-2d4a-4b5c-9e6f-7a8b9c0d1e30"}, "a thread this Host lacks")):
    pool = FakePool()
    res = asyncio.run(_run(pool, {"preferences": [{"text": "x", "reason": "r", "scope": sc}]}))
    check(f"preference: refused before any write — {why}",
          not pool.inserts() and res["result"]["writes"]["preferences"][0]["status"] == "error", res)

# No thread row (a project conversation): topic is not written, and says so.
pool = FakePool()
res = asyncio.run(_run(pool, {"topic": "x"}, thread_id=None, scope={"kind": "project", "ref": "12"}))
check("topic: a conversation with no thread row writes nothing and says so",
      not pool.calls and res["result"]["writes"]["topic"] == "no thread row for this conversation", res)

# A goal needs a conversation scope.
pool = FakePool()
res = asyncio.run(_run(pool, {"goal": {"set": "x"}}, session_id=None, scope=None))
check("goal: no conversation scope, no row",
      not pool.inserts() and res["result"]["writes"]["goal"]["status"] == "error", res)

# Dropped fields are reported to the model.
pool = FakePool()
res = asyncio.run(_run(pool, {"topic": "ok", "bogus": 1}))
check("result: dropped fields are named", any("bogus" in d for d in res["result"].get("dropped", [])), res)

# The agent wall.
pool = FakePool()
res = asyncio.run(_run(pool, {"topic": "x"}, session_id="agent:17"))
check("agents: a dispatched agent's session is refused, nothing written",
      res.get("status") == "denied" and not pool.calls, res)

# set_thread_topic itself: the legacy sentinel and an empty call write nothing.
from mastercode import circles_threads as CT  # noqa: E402
pool = FakePool()
check("set_thread_topic: legacy sentinel is a no-op",
      asyncio.run(CT.set_thread_topic(pool, 9003, CT.LEGACY_THREAD_ID, "x")) is False and not pool.calls)
check("set_thread_topic: nothing to write is a no-op",
      asyncio.run(CT.set_thread_topic(pool, 9003, THREAD, "  ", title=None)) is False and not pool.calls)
check("set_thread_topic: the consolidator's call is unchanged (title follows topic)",
      asyncio.run(CT.set_thread_topic(pool, 9003, THREAD, "billing")) is True
      and pool.calls[-1][1] == (THREAD, 9003, "billing", "billing"))

# ── 8. who may write: the session's own wall, driven for real ─────────────
_seen: list = []


async def _fake_meta(name, args, **kw):
    _seen.append((name, kw.get("session_id")))
    return {"status": "ok", "result": {"writes": {}}}


class _Sess:
    def __init__(self, ic="chat", tag="circles"):
        self.interaction_context, self.write_app_id = ic, tag
        self.entity, self.session_id = {"id": 3}, "s9"


_orig_meta = S._execute_meta_tool
S._execute_meta_tool = _fake_meta
try:
    for ic in sorted(S.EPHEMERAL_CONTEXTS):
        _seen.clear()
        r = asyncio.run(S._run_close_turn(None, _Sess(ic=ic), {"topic": "x"}))
        check(f"wall: ephemeral context {ic!r} writes nothing",
              r.get("status") == "blocked" and not _seen, r)
    for tag in ("circles-replay", "circles-ab", "scheduled_task"):
        _seen.clear()
        r = asyncio.run(S._run_close_turn(None, _Sess(tag=tag), {"goal": {"set": "x"}}))
        check(f"wall: a {tag} run keeps its signal and writes nothing to the conversation",
              r.get("status") == "ok" and not _seen and "none:" in str(r.get("result")), r)
    _seen.clear()
    r = asyncio.run(S._run_close_turn(None, _Sess(), {"topic": "x"}))
    check("wall: an ordinary turn runs the writes through the meta-tool door",
          _seen == [("close_turn", "s9")], _seen)
finally:
    S._execute_meta_tool = _orig_meta
check("wall: the turn's write-tag is set on the session each turn",
      "sess.write_app_id = app_id" in src)

# ── 9. the Review button reads what is stored ─────────────────────────────
check("review: the turn's response carries the signal",
      '"close_turn": _close_turn_signal,' in src)
_bs = open(os.path.join(APP_ROOT, "server/brain_service.py")).read()
_frame = _bs[_bs.index('"t": "context",'):_bs.index('}]) + "\\n"', _bs.index('"t": "context",'))]
check("review: the live context frame the clients parse carries it",
      '"close_turn": _ctx.get("close_turn"),' in _frame, _frame[-300:])
_circ = open(os.path.join(APP_ROOT, "server/routes/circles.py")).read()
check("review: history rows carry it (_normalize_turn_rows)", '"close_turn": md.get("close_turn"),' in _circ)
for _f, _needle in (("workbench-web/src/components/workbench/chat-pane.tsx", "<CloseTurnReview s={review} />"),
                    ("circles-web/src/components/chat/chat-message.tsx", "<CloseTurnReview s={review} />"),
                    ("workbench-web/src/lib/stream.ts", "close_turn: parseCloseTurn(it.close_turn)"),
                    ("circles-web/src/lib/circles-stream.ts", "close_turn: parseCloseTurn(it.close_turn)")):
    check(f"review: {_f.split('/')[0]} renders it", _needle in open(os.path.join(APP_ROOT, _f)).read())

# PII mode: the tool log keeps the shape of close_turn's (any tool's)
# arguments, never their words — only the keys its schema declares.
os.environ["PII_MODE"] = "on"
try:
    from mastercode.runtime.tool_call_ledger import _preview
    _pv = _preview({"preferences": [{"text": "prefers Alice", "reason": "Alice said so",
                                     "scope": {"kind": "global"}}], "goal": {"set": "call Alice", "met": True}},
                   "close_turn")
    _pv2 = _preview({"op": "tokens_set", "tokens": {"alice": "x"}, "lat": 40.712776, "n": 3,
                     "phone": 15551234567}, "preference")
finally:
    os.environ.pop("PII_MODE", None)
check("PII mode: close_turn's log row keeps its field names and no words",
      "Alice" not in _pv and '"preferences"' in _pv and '"text": "<13 chars>"' in _pv and '"met": true' in _pv, _pv)
check("PII mode: a free-form map's keys, precise decimals and long numbers are hidden",
      "alice" not in _pv2 and "40.71" not in _pv2 and "15551234567" not in _pv2 and '"op"' in _pv2, _pv2)

if FAILS:
    print(f"\n[close-turn] {len(FAILS)} FAILED")
    sys.exit(1)
print("\nclose_turn: all passed")
