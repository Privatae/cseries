#!/usr/bin/env python3
"""Invariant: every tool the host can be offered lands in ONE reachable lane.

The host-tooling cleanup (2026-08-19) put the host's whole tool surface behind
one table — runtime/tool_lanes.lane_for — after a live audit of one Host's
116-tool catalog found 4 tools reachable by nobody, 10 pure reads the host
could only perform by dispatching an agent, and harness work in the agent
lane. This gate keeps that from regressing. Over EVERY registry schema
(public + operator-only + private bot + bridge):

  1. lane ∈ {read, operate, host, dispatch, verb} — a `retired` name must carry NO
     schema at all (delete the schema, don't just relabel it), and no name may
     resolve to "unreachable" (a code-exec name that is neither a host door
     nor retired).
  2. every `host` tool is code-exec (so a dispatched agent can never hold it)
     and is NOT in DIRECT_OP_TOOLS (operate never runs code); every code-exec
     name is `host` or `retired`.
  3. every non-retired tool resolves to an executor (meta / bridge / bot) and,
     for the meta/bridge lanes, has a META_TOOL_CAPS entry (else default-deny
     at call — a visible-but-dead tool).
  4. a tool whose CHEAPEST capability is a pure read bit (READ_CONTEXT=1 /
     READ_MEMORY=2) must be readable WITHOUT an agent: whole-tool read lane, or
     at least one op in read_safe_ops — unless it is a declared exception
     (an action that carries a read bit: open_app).
  5. host-facing text hygiene: no live schema description or purpose says
     "Lane A", "Lane B", "sdk_build", "sdk_continue" or "claude-sdk lane".
  6. the agent lane is untrusted work only (2026-10-12: the Host calls
     an agent ONLY for untrusted work outside the platform): no registry tool
     lands in `gap` (unclassified: list it in DIRECT_OP_TOOLS, a read, or
     UNTRUSTED_WORK_TOOLS — never sent to an agent by default), and every
     `dispatch` tool is in tool_lanes.UNTRUSTED_WORK_TOOLS.
  7. the capability cards carry lane_for's lane: generate_capabilities
     .host_op_lane equals lane_for for every public tool, and in the image the
     generated docs/capabilities.json says the same for every host op (a
     second classifier there once sent check_page, diagnose and close_turn to
     an agent).

Run in-pod: docker exec <c> python3 /app/scripts/check_tool_lanes.py
Or against a tree: APP_ROOT=<repo> python3 scripts/check_tool_lanes.py
"""
import os
import re
import sys

APP_ROOT = os.environ.get("APP_ROOT", "/app")
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

from mastercode.code_exec_guard import _is_code_exec_tool, HOST_DOOR_TOOLS, RETIRED_CODE_TOOLS  # noqa: E402
from mastercode.runtime.tool_lanes import (  # noqa: E402
    lane_for, LANES, HOST_TOOLS, RETIRED_TOOLS, RETIRED_SHIP_TOOLS, RETIRED_PROPOSE_TOOLS,
    UNTRUSTED_WORK_TOOLS,
)
from mastercode.runtime.tool_access import read_safe_ops  # noqa: E402
from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS  # noqa: E402
from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS  # noqa: E402
from mastercode.runtime.agent_grant import min_required_bits_for  # noqa: E402
from mastercode.runtime.triage import (  # noqa: E402
    _BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS, _BRIDGE_TOOL_NAMES,
    _META_TOOL_NAMES,
)
from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS, OPERATOR_PLATFORM_TOOL_SCHEMAS  # noqa: E402

# Actions that legitimately carry a read capability bit (they open/show
# something on the user's screen); they are operate/dispatch, not reads.
READ_BIT_ACTION_EXCEPTIONS = frozenset({"open_app"})
READ_BITS = 1 | 2

fails: list[str] = []
schemas: dict[str, dict] = {}
for src in (PUBLIC_TOOL_SCHEMAS, OPERATOR_PLATFORM_TOOL_SCHEMAS, _BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS):
    for _k, sc in src.items():
        n = ((sc or {}).get("function") or {}).get("name")
        if n:
            schemas[n] = sc

lanes = {n: lane_for(n, sc) for n, sc in schemas.items()}

# 1. lane values + retired-with-schema + unreachable
bad_lane = sorted(n for n, l in lanes.items() if l not in LANES)
if bad_lane:
    fails.append(f"unknown lane values: {bad_lane}")
retired_with_schema = sorted(n for n, l in lanes.items() if l == "retired")
if retired_with_schema:
    fails.append(f"retired tools still carry a schema (delete it): {retired_with_schema}")
if RETIRED_TOOLS != (RETIRED_CODE_TOOLS | RETIRED_SHIP_TOOLS | RETIRED_PROPOSE_TOOLS) or HOST_TOOLS != HOST_DOOR_TOOLS:
    fails.append("tool_lanes host/retired sets drifted from code_exec_guard's")

# 2. host doors are code-exec, never operate; every code-exec name is host or retired
not_ce = sorted(n for n in HOST_TOOLS if not _is_code_exec_tool(n))
if not_ce:
    fails.append(f"host doors that are NOT code-exec (an agent could hold them): {not_ce}")
in_direct = sorted(HOST_TOOLS & set(DIRECT_OP_TOOLS))
if in_direct:
    fails.append(f"host doors in DIRECT_OP_TOOLS (operate would run code): {in_direct}")
ce_leak = sorted(n for n in schemas if _is_code_exec_tool(n) and lanes[n] not in ("host", "retired"))
if ce_leak:
    fails.append(f"code-exec names not classed host/retired: {ce_leak}")

# 2b. verbs (close_turn): the host's own turn verbs ride tools= on EVERY turn
#     (session._build_host_orchestration_tools), never operate, never an
#     agent's (scoped_agent strips VERB_TOOLS from every tool_scope).
from mastercode.runtime.tool_lanes import VERB_TOOLS  # noqa: E402
from mastercode.runtime.session import _build_host_orchestration_tools  # noqa: E402
_every_turn = {((s or {}).get("function") or {}).get("name") for s in _build_host_orchestration_tools()}
verb_not_core = sorted(VERB_TOOLS - _every_turn)
if verb_not_core:
    fails.append(f"verb-lane tools not in the every-turn verb list: {verb_not_core}")
verb_operate = sorted(VERB_TOOLS & set(DIRECT_OP_TOOLS))
if verb_operate:
    fails.append(f"verb-lane tools in DIRECT_OP_TOOLS (operate would run them): {verb_operate}")
verb_ce = sorted(n for n in VERB_TOOLS if _is_code_exec_tool(n))
if verb_ce:
    fails.append(f"verb-lane tools that are code-exec: {verb_ce}")

# 3. executor + caps
# One executor lane since 2026-08-22 (the bot lane's set used to union in here).
executable = set(_META_TOOL_NAMES) | set(_BRIDGE_TOOL_NAMES)
no_exec = sorted(n for n, l in lanes.items() if l != "retired" and n not in executable)
if no_exec:
    fails.append(f"no executor resolves these (dead at call): {no_exec}")
no_caps = sorted(n for n, l in lanes.items()
                 if l != "retired" and n not in META_TOOL_CAPS
                 and (n in _META_TOOL_NAMES or n in _BRIDGE_TOOL_NAMES))
if no_caps:
    fails.append(f"meta/bridge tools with no META_TOOL_CAPS entry (default-deny at call): {no_caps}")

# 4. read-bit tools must be readable without an agent. A `host` door is held
#    natively by the host — every op, reads included, runs in the host's own
#    turn (session._host_own_tools → _execute_meta_tool) — so it is readable
#    without an agent by construction.
need_agent_to_read = []
for n, l in lanes.items():
    if l in ("read", "retired", "host", "verb") or n in READ_BIT_ACTION_EXCEPTIONS:
        continue
    bits = min_required_bits_for(n)
    if bits is None or bits == 0 or (bits & ~READ_BITS):
        continue   # not a pure read by capability
    if read_safe_ops(n, schemas[n]):
        continue   # its read ops are direct
    need_agent_to_read.append(f"{n} (lane={l}, bits={bits})")
if need_agent_to_read:
    fails.append("pure-read tools the host must dispatch an agent to read: " + ", ".join(sorted(need_agent_to_read)))

# 5. host-facing text hygiene
_BAD = re.compile(r"Lane[ -]A\b|Lane[ -]B\b|sdk_build|sdk_continue|claude-sdk lane|subscription engine", re.I)
dirty = []
for n, sc in schemas.items():
    if lanes[n] == "retired":
        continue
    fn = sc.get("function") or {}
    text = " ".join(str(x) for x in (fn.get("description"), (sc.get("_meta") or {}).get("purpose")) if x)
    m = _BAD.search(text)
    if m:
        dirty.append(f"{n}: …{text[max(0, m.start()-30):m.end()+30]}…")
if dirty:
    fails.append("dead-engine vocabulary in host-facing tool text: " + " | ".join(dirty[:12]))

# 6. the agent lane is a closed list of untrusted work; nothing is unclassified
gaps = sorted(n for n, l in lanes.items() if l == "gap")
if gaps:
    fails.append("unclassified tools (lane 'gap' — list each in DIRECT_OP_TOOLS, as a read, or in "
                 f"UNTRUSTED_WORK_TOOLS if it reads content from outside the platform): {gaps}")
stray_dispatch = sorted(n for n, l in lanes.items() if l == "dispatch" and n not in UNTRUSTED_WORK_TOOLS)
if stray_dispatch:
    fails.append(f"agent-lane tools that are not untrusted work: {stray_dispatch}")
in_both = sorted(UNTRUSTED_WORK_TOOLS & set(DIRECT_OP_TOOLS))
if in_both:
    fails.append(f"tools both untrusted work and direct ops (pick one): {in_both}")

# 7. the capability cards carry lane_for's lane
sys.path.insert(0, os.path.join(APP_ROOT, "scripts"))
try:
    import generate_capabilities as _gc  # noqa: E402
    _card_lane = getattr(_gc, "host_op_lane", None)
except Exception as e:  # noqa: BLE001 — a generator that cannot load is a failure here
    _card_lane = None
    fails.append(f"scripts/generate_capabilities.py does not import: {e!r}")
if _card_lane is None:
    fails.append("generate_capabilities has no host_op_lane (the card lane must come from lane_for)")
else:
    drift = sorted(f"{n}: card={_card_lane(n, {'function': (sc.get('function') or {})}, 'public')!r} "
                   f"lane_for={lane_for(n, sc)!r}"
                   for n, sc in ((((sc or {}).get("function") or {}).get("name"), sc)
                                 for sc in PUBLIC_TOOL_SCHEMAS.values())
                   if n and _card_lane(n, {"function": (sc.get("function") or {})}, "public")
                   != lane_for(n, sc))
    if drift:
        fails.append("capability card lanes differ from lane_for: " + "; ".join(drift[:12]))
_caps_json = os.path.join(APP_ROOT, "docs", "capabilities.json")
if os.path.isfile(_caps_json):
    import json as _json
    with open(_caps_json, encoding="utf-8") as _f:
        _ops = (_json.load(_f) or {}).get("host_ops") or []
    _doc_drift = sorted(f"{o.get('name')}: doc={o.get('lane')!r} lane_for={lanes.get(o.get('name'))!r}"
                        for o in _ops
                        if o.get("registry") != "orchestration" and o.get("name") in lanes
                        and o.get("lane") != lanes[o.get("name")])
    if _doc_drift:
        fails.append("docs/capabilities.json lanes differ from lane_for: " + "; ".join(_doc_drift[:12]))
elif APP_ROOT == "/app":
    fails.append("/app/docs/capabilities.json is missing in the image (it is generated at build)")
else:
    print("note: docs/capabilities.json is generated at image build; against a tree only the "
          "generator's own classifier is compared")

if fails:
    for f in fails:
        print(f"FAIL: {f}")
    sys.exit(1)
from collections import Counter
c = Counter(lanes.values())
print(f"OK: {len(schemas)} tools — lanes {dict(c)}; host doors {sorted(HOST_TOOLS)}; "
      f"retired names carry no schema; every read-bit tool readable without an agent; "
      f"the agent lane is {sorted(UNTRUSTED_WORK_TOOLS)} only; cards carry lane_for")
