#!/usr/bin/env python3
"""Invariant: the deterministic lane holds the clerical work, and dispatch
refuses it.

WHAT THIS RATCHETS (measured on one Host, 2026-09-19). Every agent dispatched
on that host in the preceding 14 days — 6 agents, 15 opus-5 model calls — did
ONE deterministic bookkeeping op: project_phase add_phase, workspace_file
write, ship_project_app + publish_site + manage_site. All-time, every agent
ever dispatched there was scoped to tools of that kind. A model was being paid
to retype arguments the host was already holding, and the rule is the other way
round (the ops are deterministic and static functions that need no model;
there should only be model calls when there is reasoning to be done).

direct_ops.py had named most of those tools as "mixed/unswept ops — candidates
for a follow-up sweep once each op enum is read from its live schema". This
gate is what keeps that sweep swept. Three things, and only the first is about
the sweep's own result:

  1. LANE PER TOOL. Every tool the sweep classified resolves to the lane it was
     put in, by name. A table, not a heuristic — a tool silently falling back to
     `dispatch` is exactly the regression that costs a model call per call, and
     it looks like nothing.
  2. THE WALLS DID NOT MOVE. The credential tools (keys_manage,
     connect_compute, reachout_channel) and image_generate are the Host's own
     operate ops since 2026-10-12 (the Host calls an agent only for
     untrusted work), and none of them is schedulable: the scheduling wall is
     UNATTENDED_OP_TOOLS, which the offline runner checks before run_direct_op.
     No host door is in the lane. Widening this lane is cheap and that is the
     point — widening it THROUGH one of those walls is not, so the walls get
     asserted here beside the widening.
  3. THE PER-OP MAP MATCHES THE LIVE SCHEMA. An op named in
     DIRECT_OP_EXCLUDED_OPS must still exist in that tool's op enum. A rename
     upstream would otherwise turn an exclusion into a no-op silently, which is
     how a remote run would quietly join the no-model lane.

And the other half of the change: `dispatch` refuses a scope that is entirely
clerical, a scope holding the Host's own code reads, and a scope that reads
nothing untrusted (2026-10-12) — and does NOT refuse one that reads the web, a
foreign repo or another Host's message. Both directions are asserted, because
a refusal that fires on everything is the same outage as a refusal that fires
on nothing.

Run in-pod: docker exec <c> python3 /app/scripts/test_operate_sweep.py
Or against a tree: APP_ROOT=<repo> python3 scripts/test_operate_sweep.py
"""
import os
import sys

APP_ROOT = os.environ.get("APP_ROOT", "/app")
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

from mastercode.code_exec_guard import HOST_DOOR_TOOLS, _is_code_exec_tool  # noqa: E402
from mastercode.public_tool_registry import (  # noqa: E402
    OPERATOR_PLATFORM_TOOL_SCHEMAS, PUBLIC_TOOL_SCHEMAS,
)
from mastercode.runtime import scoped_agent as sa  # noqa: E402
from mastercode.runtime.direct_ops import (  # noqa: E402
    DIRECT_OP_EXCLUDED_OPS, DIRECT_OP_TOOLS, UNATTENDED_OP_TOOLS,
    excluded_op_refusal, is_fully_direct_op_tool,
)
from mastercode.runtime.tool_access import read_safe_ops  # noqa: E402
from mastercode.runtime.tool_lanes import LANES, UNTRUSTED_WORK_TOOLS, lane_for  # noqa: E402
from mastercode.runtime.triage import (  # noqa: E402
    _BRIDGE_TOOL_SCHEMAS, _BOT_TOOL_SCHEMAS,
)

FAILED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILED
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED += 1
        if detail:
            for line in str(detail).strip().splitlines():
                print(f"        {line}")


SCHEMAS: dict[str, dict] = {}
for _src in (PUBLIC_TOOL_SCHEMAS, OPERATOR_PLATFORM_TOOL_SCHEMAS,
             _BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS):
    for _sc in _src.values():
        _n = ((_sc or {}).get("function") or {}).get("name")
        if _n:
            SCHEMAS[_n] = _sc


def schema_ops(name: str) -> list[str]:
    """The tool's declared op enum values, verbatim (lowercased). Empty when
    the tool has no selector — several of these are one-shot tools."""
    sc = SCHEMAS.get(name) or {}
    params = ((sc.get("function") or {}).get("parameters")
              or sc.get("parameters") or {})
    props = params.get("properties") if isinstance(params, dict) else None
    if not isinstance(props, dict):
        return []
    for key in ("op", "action", "operation", "method", "command", "mode"):
        spec = props.get(key)
        if isinstance(spec, dict) and spec.get("enum"):
            return [str(v).lower() for v in spec["enum"]]
    return []


# ── 1. The sweep's own result, tool by tool ─────────────────────────────────
#
# Every tool the 2026-09-19 sweep looked at, and the lane it was put in. The
# left column is the complete set: the 27 registry tools that were on neither
# DIRECT_OP_TOOLS nor tool_access._READ_SAFE_TOOLS by name, plus the tools
# direct_ops had listed as deliberately unswept.
EXPECTED_LANE: dict[str, str] = {
    # ── operate: one deterministic call each ──
    "archive_project": "operate",
    "autofill": "operate",
    "circles_manage": "operate",
    "contacts_manage": "operate",
    "context_ingest": "operate",       # every op, extract included (2026-09-22)
    "dcn": "operate",                  # every op, run_plan included (2026-10-12)
    "host_message": "operate",
    "import_repo": "operate",
    "integrations_manage": "operate",
    "links": "operate",
    "model_routing": "operate",
    "project_adopt_thread": "operate",
    "project_phase": "operate",
    "publish_app": "operate",
    "publish_site": "operate",
    "report_crash": "operate",
    "set_project_manifest": "operate",
    "ship_project_app": "operate",
    "update_project": "operate",
    "workspace_file": "operate",
    # Already in the lane before the sweep; listed so a regression that drops
    # one shows up here beside the new arrivals rather than nowhere.
    "manage_site": "operate",
    "set_project_routing": "operate",
    "artifact": "operate",
    # ── read: whole-tool reads, no agent and no operate needed ──
    "get_project": "read",
    "get_project_activity": "read",
    "list_skills": "read",
    # ── in-platform tools that were agent-only by default (2026-10-12) ──
    # Before the agent lane became a closed list, these fell to `dispatch`
    # because nothing named them. Each acts only inside the platform, so the
    # Host runs it itself. The walls that matter are asserted in §2: none of
    # them is schedulable, keys_manage `use` and mgmt_run writes are refused on
    # a turn that read untrusted content, reachout_channel's step-up stays in
    # the service.
    "keys_manage": "operate",
    "connect_compute": "operate",
    "reachout_channel": "operate",
    "image_generate": "operate",
    "mgmt_run": "operate",
    "context": "operate",
    "propose_capability": "operate",
    "community_board": "operate",
    "state_store": "operate",
    # app_probe reads the Host's own app; escalate is a verb on every turn.
    "app_probe": "read",
    "escalate": "verb",
    # ── dispatch: untrusted work outside the platform, and nothing else ──
    "github": "dispatch",
}

print("operate sweep — lane per tool")
for _tool, _want in sorted(EXPECTED_LANE.items()):
    _sc = SCHEMAS.get(_tool)
    check(f"{_tool} has a live schema", _sc is not None,
          f"{_tool} is named in this gate's table but no registry carries it. "
          "Either it was retired (drop the row) or it was renamed (fix it).")
    if _sc is None:
        continue
    _got = lane_for(_tool, _sc)
    check(f"{_tool} -> {_want}", _got == _want,
          f"lane_for({_tool}) == {_got!r}, expected {_want!r}. A tool that "
          "falls back to 'dispatch' costs a model call per call and looks "
          "like nothing.")

check("every lane in the table is a real lane",
      set(EXPECTED_LANE.values()) <= set(LANES),
      f"{sorted(set(EXPECTED_LANE.values()) - set(LANES))}")

# LOOKING at what image_generate made is a read, never an operate call — the
# read-bit rule check_tool_lanes enforces generally.
check("image_generate op='gallery' is reachable as a direct read",
      "gallery" in read_safe_ops("image_generate", SCHEMAS.get("image_generate")),
      f"{sorted(read_safe_ops('image_generate', SCHEMAS.get('image_generate')))}")
check("autofill op='saved' is reachable as a direct read",
      "saved" in read_safe_ops("autofill", SCHEMAS.get("autofill")),
      f"{sorted(read_safe_ops('autofill', SCHEMAS.get('autofill')))}")

# ── 2. The walls did not move ───────────────────────────────────────────────
print("\nthe walls the sweep must not widen through")
_NOT_SCHEDULABLE = ("keys_manage", "connect_compute", "reachout_channel",
                    "image_generate", "mgmt_run")
_leaked = sorted(t for t in _NOT_SCHEDULABLE if t in UNATTENDED_OP_TOOLS)
check("credential / spend / machine tools are not in UNATTENDED_OP_TOOLS", not _leaked,
      f"{_leaked} — the offline runner refuses a scheduled op_tool outside "
      "UNATTENDED_OP_TOOLS before run_direct_op is reached; that set, not "
      "absence from DIRECT_OP_TOOLS, keeps these off a schedule.")
check("the agent lane is untrusted work only",
      {t for t, sc in SCHEMAS.items() if lane_for(t, sc) == "dispatch"} <= set(UNTRUSTED_WORK_TOOLS),
      f"{sorted(t for t, sc in SCHEMAS.items() if lane_for(t, sc) == 'dispatch')}")
check("no host door is in DIRECT_OP_TOOLS",
      not (set(HOST_DOOR_TOOLS) & set(DIRECT_OP_TOOLS)),
      f"{sorted(set(HOST_DOOR_TOOLS) & set(DIRECT_OP_TOOLS))} — operate never "
      "runs code.")
check("no code-exec name is in DIRECT_OP_TOOLS",
      not [t for t in DIRECT_OP_TOOLS if _is_code_exec_tool(t)],
      f"{sorted(t for t in DIRECT_OP_TOOLS if _is_code_exec_tool(t))}")

# ── 3. The per-op map still matches the live schema ─────────────────────────
print("\nper-op exclusions")
check("every tool with per-op exclusions is itself a direct op",
      set(DIRECT_OP_EXCLUDED_OPS) <= set(DIRECT_OP_TOOLS),
      f"{sorted(set(DIRECT_OP_EXCLUDED_OPS) - set(DIRECT_OP_TOOLS))} — an "
      "exclusion on a tool outside the lane is dead text.")
for _tool, _ops in sorted(DIRECT_OP_EXCLUDED_OPS.items()):
    _live = schema_ops(_tool)
    _gone = sorted(o for o in _ops if o not in _live)
    check(f"{_tool}: every excluded op still exists in its live enum", not _gone,
          f"{_gone} not in {_live}. An exclusion that names a renamed op is a "
          "silent no-op, and the thing it was excluding joins the no-model "
          "lane without anyone deciding that.")
    check(f"{_tool} is NOT fully direct (its scope can still need an agent)",
          not is_fully_direct_op_tool(_tool))
    for _op in sorted(_ops):
        _msg = excluded_op_refusal(_tool, {"op": _op})
        check(f"operate({_tool}, op={_op!r}) is refused and says where to go",
              _msg is not None and "dispatch" in _msg.lower()
              and _tool in _msg, str(_msg))
    _allowed = [o for o in _live if o not in _ops]
    check(f"{_tool}: its other {len(_allowed)} ops are not refused",
          all(excluded_op_refusal(_tool, {"op": o}) is None for o in _allowed),
          f"{[o for o in _allowed if excluded_op_refusal(_tool, {'op': o})]}")

check("a tool with no exclusions is fully direct",
      is_fully_direct_op_tool("project_phase")
      and excluded_op_refusal("project_phase", {"op": "add_phase"}) is None)

# ── 4. Dispatch refuses clerk work — and only clerk work ────────────────────
print("\ndispatch refuses clerk work")

# The exact shape measured on one Host: an agent dispatched to add a phase.
_msg = sa.clerk_scope_refusal(["project_phase"])
check("an all-deterministic scope is refused", _msg is not None, str(_msg))
check("the refusal names the operate call to make instead",
      _msg is not None and "operate(tool='project_phase'" in _msg, str(_msg))
check("the refusal says nothing was created, so the host does not apologise "
      "to the user for a task that never existed",
      _msg is not None and "no task was created" in _msg.lower(), str(_msg))

_msg2 = sa.clerk_scope_refusal(
    ["ship_project_app", "publish_site", "manage_site"])
check("the 14-day measured scope (ship + publish + manage_site) is refused",
      _msg2 is not None
      and all(f"operate(tool='{t}'" in _msg2
              for t in ("ship_project_app", "publish_site", "manage_site")),
      str(_msg2))

check("an all-operate scope of several tools is refused",
      sa.clerk_scope_refusal(
          ["project_phase", "workspace_file", "archive_project"]) is not None)

# ── The other direction, and the half that matters most ─────────────────────
#
# An agent is the isolation for content from OUTSIDE the platform — the web, a
# repo nobody here owns, another Host's message — read by something that cannot
# write memory. That holds, and those scopes must still dispatch. Until
# 2026-10-12 this block also kept platform-source reads, a machine look and the
# Host's own state as "well-built agents"; they are the Host's own reading
# (2026-10-12: agents only for untrusted work), and are refused now —
# asserted below with the door that refuses them.
for _scope, _why in (
    (["research", "web_search", "read_url"], "web research, isolated"),
    (["github"], "reading a foreign repository"),
    (["bug_review"], "the validator reading another Host's report"),
):
    check(f"an untrusted-reading scope is NOT refused — {_why}",
          sa.scope_work_refusal(_scope) is None, f"{_scope}: {sa.scope_work_refusal(_scope)}")

for _scope, _why in (
    (["code_grep", "code_read", "code_tree"], "reading the platform source"),
    (["search_my_code"], "searching its own repos"),
    (["diagnose", "machine_map"], "looking at its own machine"),
    (["orient", "list_projects", "get_project"], "reading its own state"),
    (["chat_threads"], "reading past conversation"),
    (["get_project", "list_skills"], "two plain reads"),
):
    _m = sa.scope_work_refusal(_scope)
    check(f"an in-platform read-only scope IS refused — {_why}", _m is not None, f"{_scope}")
    check(f"  …and the refusal says to read it yourself — {_why}",
          _m is not None and "read(tool=" in _m and "no task was created" in _m.lower(), str(_m))

check("own code reads beside a web read are refused (a mixed scope would let the agent "
      "read the platform source)",
      sa.own_code_scope_refusal(["code_grep", "web_search"]) is not None
      and sa.scope_work_refusal(["code_grep", "web_search"]) is not None)
# 2026-10-13: a reader in the scope no longer admits what sits beside it. An
# agent holds only readers of untrusted content; the operate half is the
# Host's, done after the report comes back.
for _scope in (["workspace_file", "read_url"], ["project_phase", "research"],
               ["workspace_file", "github"], ["code_grep", "web_search"],
               ["gitea", "web_search"], ["build", "research"]):
    _m = sa.scope_work_refusal(_scope)
    check(f"a MIXED in-platform+reader scope IS refused — {_scope}", _m is not None, str(_scope))
check("a MIXED operate+own-read scope IS refused (nothing untrusted in it)",
      sa.scope_work_refusal(["workspace_file", "get_project"]) is not None)

check("a scope holding a model-in-the-loop web tool is NOT refused",
      sa.clerk_scope_refusal(["research"]) is None
      and sa.scope_work_refusal(["research"]) is None)
check("the mixed refusal names the reader-only scope to dispatch again with",
      'tool_scope: ["research"]' in (sa.scope_work_refusal(["project_phase", "research"]) or ""))
check("readers alone, and an MCP server's tool, are NOT refused",
      sa.scope_work_refusal(["web_search", "read_url", "research", "github"]) is None
      and sa.scope_work_refusal(["inbox_read", "mcp:notion/search"]) is None)
# 2026-09-30: memory_curate lost its classifier (memory ops are
# plain tool calls) and is a direct op, so a scope of it alone is clerk work.
check("a scope that only curates memory IS refused — the host calls it itself",
      sa.clerk_scope_refusal(["memory_curate"]) is not None)
check("a scope of a credential tool IS refused — the host calls it itself (2026-10-12)",
      sa.clerk_scope_refusal(["keys_manage"]) is not None
      and sa.clerk_scope_refusal(["reachout_channel"]) is not None)
check("dcn is fully direct since run_plan left the per-op set: a dcn scope IS refused",
      sa.clerk_scope_refusal(["dcn"]) is not None)
check("a per-op tool is NOT refused as clerk work (bug_review: the validator's ops)",
      sa.clerk_scope_refusal(["bug_review"]) is None
      and sa.scope_work_refusal(["bug_review"]) is None)
# 2026-09-22: context_ingest left the per-op set — reading or extracting a
# file is a read function the host calls itself, so a dispatch whose whole
# scope is context_ingest is clerk work and is refused.
check("a scope that only reads/extracts user files IS refused — the host calls it itself",
      sa.clerk_scope_refusal(["context_ingest"]) is not None)
check("a code-exec scope is left to its own wall by the clerk rule",
      sa.clerk_scope_refusal(["build"]) is None
      and sa.clerk_scope_refusal(["build", "artifact"]) is None)
check("…and the door names the build doors for it, since it reads nothing untrusted",
      "build" in (sa.scope_work_refusal(["build"]) or "")
      and "called directly" in (sa.scope_work_refusal(["build"]) or ""))
check("an empty scope is not refused by the clerk rule",
      sa.clerk_scope_refusal([]) is None
      and sa.clerk_scope_refusal(None) is None)
check("…but an empty scope reads nothing untrusted, so the door refuses it",
      sa.scope_work_refusal([]) is not None)

# The door is where it fires, so the host gets it as its own tool error and
# fixes it in-turn. Checked through dispatch_precheck with a grant that would
# otherwise have passed, so this asserts ORDER too: the clerk answer comes
# before "your grant is too narrow", which would only send the host back to the
# same wall a round later.
_door = sa.dispatch_precheck(["project_phase"], ["read_context"])
check("the dispatch DOOR returns the clerk refusal",
      _door is not None and "operate(tool='project_phase'" in _door, str(_door))
_door_ok = sa.dispatch_precheck(["research"], ["external_api"])
check("the door still passes a coherent agent-shaped dispatch",
      _door_ok is None, str(_door_ok))
_door_own = sa.dispatch_precheck(["code_grep"], ["read_context"])
check("the DOOR refuses an agent that would read the platform source",
      _door_own is not None and "read(tool='code_grep'" in _door_own, str(_door_own))

print(f"\n{'PASS' if not FAILED else str(FAILED) + ' FAILED'}")
sys.exit(1 if FAILED else 0)
