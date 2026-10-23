#!/usr/bin/env python3
"""
Host build-door gate (open/generic host engine, smoke).

Structural verification that the host can REACH Lane A from a chat turn on the
generic (provider) engine — i.e. that `build` is a tool the host itself holds,
and that the orchestrator prompt tells it so instead of routing it into a wall.

WHY THIS EXISTS (the Kimi harness smoke test)
Code-exec tools are Lane A: scoped_agent strips them from every dispatched
agent's scope, and direct_ops refuses them under `operate`. The subscription
engine (host_sdk_engine) held `build` natively in HOST_SCOPE_TOOLS; when that
lane was cut the generic engine — now the ONLY engine — never
got it. The host was left with three walls each pointing at another:

  catalog: "Action capabilities (dispatch an agent with these)" listed `build`
  dispatch:  stripped `build` (correct — Lane B never runs code)
  operate:   "use your `build` tool" (which the host did not hold)
  direct:    "`build` is an action — dispatch it"

The Kimi host followed the catalog, dispatched `build`, and the run came back
'blocked'. No build had started from chat on one Host since the cut.

Gates (exit 0 = all pass; exit 1 + [build-door] FAIL lines otherwise):
  1. _HOST_OWN_ACTION_TOOLS is non-empty and contains `build`
  2. every host-own tool IS a Lane-A tool (the whole point of holding it)
  3. every host-own tool resolves to an executor and a caps entry (not dead)
  4. no host-own tool is ALSO a direct op (operate must never run code)
  5. the prompt renders `build` under the host's OWN lane, not as a dispatch
     capability, and advertises no other code-exec name anywhere
  6. the own-lane section is ABSENT when the entity is not authorized for it
  7. the generic tool loop dispatches host-own names to _execute_meta_tool
     (source-level: the branch exists and passes the session's context)
  8. no host-own tool is a read, wholly or for any op its schema declares,
     so neither the by-name read branch nor search mode's `read` door
     can run one without the session's context
  The host-own set comes from runtime/tool_lanes.py (lane
  "host": build, ladder.run, review.fan_out/compare/propose_commit,
  mesh_harness_run) — the fuller invariant set lives in check_tool_lanes.py.

Runs against the image (/app) or a source tree (APP_ROOT=<repo>); no pod, no
pool, no model.
"""

import inspect
import os
import re
import sys

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[build-door] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _schema(name, **props):
    return {"type": "function",
            "function": {"name": name, "description": f"{name} desc",
                         "parameters": {"type": "object", "properties": props}}}


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "brain"))
    import mastercode.runtime.session as s
    from mastercode.code_exec_guard import _is_code_exec_tool
    from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS
    from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS
    from mastercode.runtime.triage import _META_TOOL_NAMES
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS

    # A tree without the door at all must FAIL, not crash.
    own = set(getattr(s, "_HOST_OWN_ACTION_TOOLS", ()))

    # 1-4. The set itself.
    check("host holds a build door", "build" in own, f"own={sorted(own)}")
    not_lane_a = sorted(t for t in own if not _is_code_exec_tool(t))
    check("every host-own tool is Lane A (code-exec)", not not_lane_a,
          f"not code-exec: {not_lane_a}")
    dead = sorted(t for t in own if t not in _META_TOOL_NAMES or t not in META_TOOL_CAPS)
    check("every host-own tool has an executor and a caps entry", not dead,
          f"unresolvable/uncapped: {dead}")
    both = sorted(own & set(DIRECT_OP_TOOLS))
    check("no host-own tool is a direct op (operate never runs code)", not both,
          f"{both}")

    # 5. Prompt: build in the own lane, never as a dispatch capability; no
    #    other code-exec name advertised at all. The real `build` schema is
    #    keyed by its function name in the public registry.
    check("`build` schema is registered", "build" in PUBLIC_TOOL_SCHEMAS)
    build_schema = PUBLIC_TOOL_SCHEMAS["build"] if "build" in PUBLIC_TOOL_SCHEMAS else _schema("build")
    cat = [build_schema, _schema("list_projects"), _schema("inbox_send"),
           _schema("sdk_build"), _schema("harness_run"), _schema("codex.run"),
           _schema("model_route", op={"type": "string", "enum": ["current", "switch"]})]
    p = s._build_orchestrator_prompt(cat)
    # The brief prompt: the block names the own build
    # doors in one sentence and counts every other lane; no tool is listed
    # as a dispatch capability by name.
    i_own = p.find("Your own build tools")
    check("prompt names the own lane when build is authorized", i_own > 0)
    check("`build` is named once, in the own lane, never as a dispatch capability",
          p.count("`build`") == 1 and i_own >= 0 and "`build`" in p[i_own:])
    leaked = sorted(n for n in ("sdk_build", "harness_run", "codex.run") if n in p)
    check("no other code-exec name is advertised anywhere", not leaked, f"{leaked}")
    check("own-lane text forbids dispatching or operating build",
          i_own >= 0 and "never dispatched or operated" in p[i_own:])

    # 6. Not authorized → no section, no mention.
    p2 = s._build_orchestrator_prompt([_schema("list_projects"), _schema("inbox_send")])
    check("own-lane section absent when build is not in the catalog",
          "Your own build tools" not in p2 and "`build`" not in p2)

    # 7. The loop executes host-own names through the meta-tool executor with
    #    the session's interaction_context (so the workbench surface gate and
    #    the caps gate fire exactly as on the SDK engine's _handle_build).
    #    The read branch is `elif _read_admits(func_name, ...)` since tool search
    #    gave a read one admission rule (tool_doors.read_admits);
    #    before that it was `elif func_name in getattr(sess, "_read_safe_tools"`.
    #    Whitespace inside each anchor is free: a rewrap of either line read as
    #    "no branch at all", the same false red that once stopped a ship.
    # The turn's own code and the step that builds its tool surface.
    src = (inspect.getsource(s.EntityRuntime._send_message_core)
           + inspect.getsource(s.EntityRuntime._build_turn_tools))
    m = re.search(
        r"elif\s+func_name\s+in\s+getattr\(\s*sess\s*,\s*\"_host_own_tools\"\s*,\s*set\(\)\s*\)\s*:"
        r"(?P<body>.*?)elif\s+_read_admits\(\s*func_name\s*,\s*func_args\s*,",
        src, re.S)
    check("tool loop has a host-own branch ahead of the read branch", m is not None)
    body = m.group("body") if m else ""
    # The branch runs the ONE host-own helper (_run_host_own), which the
    # operate door also takes for a build door off the surface's list
    # (the brief prompt); the helper carries the gates.
    hm = re.search(r"async def _run_host_own\(_name, _args\):(?P<h>.*?)return _res", src, re.S)
    helper = hm.group("h") if hm else ""
    check("host-own branch executes via _execute_meta_tool",
          re.search(r"await _run_host_own\(\s*func_name\s*,\s*func_args\s*\)", body) is not None
          and re.search(r"_execute_meta_tool\(\s*_name\s*,\s*_args\s*,", helper) is not None)
    check("host-own branch passes the session's interaction_context",
          re.search(r"interaction_context\s*=\s*sess\.interaction_context\b", helper) is not None)
    check("host tool list is assembled from the CEIGAS catalog (_cap_schemas)",
          re.search(r"_own_tools = \[\s*s for s in _cap_schemas", src) is not None
          and "sess._host_own_tools" in src)

    # 8. No host-own tool is a read. The by-name read branch and search mode's
    #    `read(tool, args)` door both run their target through
    #    _run_held_read, which calls _execute_meta_tool with
    #    interaction_context=None, and the surface gate runs only when a
    #    context is given (tools._execute_meta_tool). A host-own tool admitted
    #    there would run off its surface: `editor` (the Coding Tutor's tool,
    #    since removed) was vscode-only and declared op read/list/search.
    #    session.py builds the held reads from
    #    lane_for(...) == "read" and the per-op map from
    #    tool_access.read_safe_ops. Measured on a tree with the
    #    code-exec exclusion taken out of read_safe_ops: a search-mode Circles
    #    turn's read(tool='editor', args={'op': 'read'}) ran editor through
    #    _execute_meta_tool with interaction_context=None, and checks 1-7
    #    still passed. So both predicates are checked
    #    for every host-own tool against its registry schema, and then the
    #    admission rule and the door, for every op each schema declares.
    from mastercode.runtime.tool_access import read_safe_ops
    from mastercode.runtime.tool_call_ledger import known_tool_schema
    from mastercode.runtime.tool_doors import _OP_KEYS, read_admits, read_refusal
    from mastercode.runtime.tool_lanes import lane_for
    sch = {t: PUBLIC_TOOL_SCHEMAS.get(t) or known_tool_schema(t) for t in sorted(own)}
    rst = {t for t in own if lane_for(t, sch[t]) == "read"}
    rso = {}
    for t in sorted(own):
        if t not in rst:
            ops = read_safe_ops(t, sch[t])
            if ops:
                rso[t] = ops
    check("no host-own tool is a whole-tool read (lane_for)", not rst, str(sorted(rst)))
    check("no host-own tool has a read-safe op (tool_access.read_safe_ops)", not rso,
          str({t: sorted(o) for t, o in rso.items()}))
    admitted = []
    for t in sorted(own):
        props = (((sch[t] or {}).get("function") or {}).get("parameters") or {}).get("properties") or {}
        calls = [{}] + [{k: v} for k in _OP_KEYS
                        if isinstance(props.get(k), dict) and isinstance(props[k].get("enum"), list)
                        for v in props[k]["enum"]]
        for a in calls:
            if read_admits(t, a, rst, rso):
                admitted.append(f"{t} {a} (by name)")
            if read_refusal({"tool": t, "args": a}, None, read_safe_tools=rst,
                            read_safe_ops=rso, host_own_tools=own) is None:
                admitted.append(f"{t} {a} (read door)")
    check("no read door admits a host-own tool, for any op it declares", not admitted,
          f"{admitted[:6]}")

    if FAILS:
        print(f"\n[build-door] {len(FAILS)} FAILED: {FAILS}")
        return 1
    print("\n[build-door] all gates pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
