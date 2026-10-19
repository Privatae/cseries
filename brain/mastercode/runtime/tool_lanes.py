"""mastercode.runtime.tool_lanes — ONE table for how the host reaches a tool.

Before this module four independent name-heuristics decided the
host's tool surface — tool_access.is_read_safe_tool (reads), direct_ops
.DIRECT_OP_TOOLS (operate), code_exec_guard._is_code_exec_tool (".run" /
"sdk_" / "harness" names), and a one-item host-own set — and nothing asserted
that every advertised tool landed somewhere the host could actually execute
it. Measured live on one Host (116 catalog tools): 4 tools reachable by
nobody (ladder.run, codex.run, cursor.run, mesh_harness_run), 10 pure reads
the host could only perform by dispatching an agent, and harness work
(the multi-harness door, review.compare/propose_commit) sitting in the agent lane because
the wall checked the NAME, not what the tool runs.

The lanes:

  read      the host calls it directly (a safe read; per-op reads of a mixed
            tool ride tool_access.read_safe_ops as before)
  operate   ONE deterministic registry op, host runs it directly (direct_ops)
  host      the host's OWN doors to the harness pool — build, the ladder, the
            review family. Held natively by the host; NEVER dispatchable to
            an agent (code_exec_guard treats every `host` tool as code-exec);
            never `operate`.
  dispatch  a scoped agent runs it under a host-minted grant. Only the tools
            in UNTRUSTED_WORK_TOOLS land here: work that reads content from
            outside the platform (the Host calls an agent
            only for untrusted work; everything in-platform it does itself)
  verb      the host's own turn verb (close_turn): rides tools= on EVERY turn
            beside the orchestration verbs, every surface and every turn type;
            the host calls it directly. NEVER an agent's (scoped_agent strips
            it from every tool_scope) and never `operate`'s.
  retired   advertised nowhere, refused everywhere — the dead engines' tools,
            the retired controller ship primitives and the old platform
            proposal tool
  gap       a tool nobody classified. No tool may land here:
            scripts/check_tool_lanes.py fails on it. Falling to `dispatch`
            made every new in-platform tool agent-only until someone listed it (19 registry tools on one Host,
            one of them untrusted work). A gap is said as a gap, never
            silently sent to an agent and never silently promoted.

`lane_for(name, schema)` is the single classifier: the prompt catalog, the
host's tool-list assembly and the tool loop all call it, so the shown surface
and the executed surface cannot drift. LANE_OVERRIDES pins the tools the
heuristics get wrong; everything else falls through to the same predicates as
before. scripts/check_tool_lanes.py is the ratchet: every registry tool must
resolve to a reachable lane (never `gap`), every read-bit tool must be readable
without an agent, every `host` tool must be code-exec, no `retired` name may
still carry a schema, the dispatch lane holds only UNTRUSTED_WORK_TOOLS, and
the capability cards carry the lane this function returns.
"""
from __future__ import annotations

# The host doors and the retired code tools are declared ONCE, in the
# stdlib-only guard (so scoped_agent / direct_ops / the smoke gates read the
# same sets); the lane table only restates them as lanes.
from mastercode.code_exec_guard import HOST_DOOR_TOOLS, RETIRED_CODE_TOOLS

# ── The agent lane — a closed list of untrusted work ─────────────────────
# A tool lands in `dispatch` only by being named here. Each one reads content
# from outside the platform as its point: github reads repositories nobody on
# the platform owns. Reading the web (web_search, read_url, research) is a
# read the Host may also make itself on a marked turn (runtime/turn_taint),
# so those stay in the read lane; what an agent may hold is decided by the
# dispatch door (scoped_agent.dispatch_precheck), not by this list.
UNTRUSTED_WORK_TOOLS: frozenset[str] = frozenset({"github"})

# The controller's ship primitives, retired. They built and tagged
# on the controller host beside the platform database, set env that the
# platform_release row overrides, and offered `restart` for the platform API; the
# Host ships through the same runbooks Claude Code runs, from its root thread.
# Pinned here so a schema that comes back fails check_tool_lanes.
RETIRED_SHIP_TOOLS: frozenset[str] = frozenset({
    "docker_build", "docker_push", "git_tag",
    "k8s_set_image", "k8s_set_env", "systemctl_run",
})

# Retired write paths to the platform source. gitea op='propose' is the one
# path: a fork, a branch and a PR the Host opens in its own turn. A name listed
# here is pinned so a schema that comes back fails check_tool_lanes.
RETIRED_PROPOSE_TOOLS: frozenset[str] = frozenset()

# ── The overrides — the tools the heuristics misplace ────────────────────
LANE_OVERRIDES: dict[str, str] = {
    # The host's own doors to the harness pool. build was already host-own;
    # the ladder and the review family were designed as host tools
    # (default-skills/build-workflow.md, review.md) but ladder.run was
    # unreachable (".run" ⇒ code-exec, not
    # held) and review.* leaked into the agent lane.
    **{n: "host" for n in HOST_DOOR_TOOLS},
    # Retired: refused everywhere; scripts/check_tool_lanes.py asserts none
    # of them still carries a schema.
    **{n: "retired" for n in RETIRED_CODE_TOOLS},
    **{n: "retired" for n in RETIRED_SHIP_TOOLS},
    **{n: "retired" for n in RETIRED_PROPOSE_TOOLS},
    # Pure reads the name heuristics could not see (a live audit).
    "ladder.status": "read",          # dotted name defeats the _status suffix rule
    "code_grep": "read",              # bridge op code.grep (ripgrep) — sibling code_read is listed
    "code_tree": "read",              # bridge op code.tree
    "search_my_code": "read",         # code_chunks SELECTs; model reranker writes nothing
    "state_load": "read",             # one SELECT on entity_context
    "capabilities": "read",           # read_text() on the generated capabilities doc
    # machine_map: 'read' is a table read; 'refresh' runs
    # two READ-ONLY bastion ops (computer:inventory / computer:windows) behind
    # the user's signed management grant. Nothing is written on the box, so
    # the host reads it itself — the 'refresh' token would otherwise send a
    # read to the agent lane.
    "machine_map": "read",
    # check_page: loads one of the host's OWN app pages in the
    # browser sidecar and reports what a browser sees. Nothing on the user's
    # side is written — it is a look. It must be a whole-tool read for the
    # same reason machine_map is: the moment a host has to dispatch an agent
    # to LOOK at a broken page, it stops looking and asks the user to
    # hard-refresh instead. That is the failure exactly.
    "check_page": "read",
    # diagnose: the faults-as-data read behind the "Your machine
    # needs attention" block. Reads routing/coverage/protocol/ledger state;
    # writes nothing — the fixes are the ordinary configuration tools.
    "diagnose": "read",
    # app_probe: every op reads one of the Host's OWN running
    # apps — an HTTP GET of a path on its own base_url, its log, its data
    # files — and writes nothing (same reason as check_page). Only `files`
    # was a read token, so request/logs fell to the agent lane and checking
    # its own app needed an agent.
    "app_probe": "read",
    # open_app carries a read BIT but is an ACTION (a window opens on the
    # user's screen) — one deterministic call, so it is an operate target.
    "open_app": "operate",
    # close_turn: the Host's end of turn. It
    # writes the Host's OWN thread topic, the user's preferences and the
    # thread goal from the turn it closes — an agent has no turn to close.
    "close_turn": "verb",
    # escalate rides every turn beside the orchestration verbs
    # (session._orchestration_verbs); the Host files its own ticket or
    # decision. It was shown with door 'dispatch' and counted among the
    # tools an agent can hold.
    "escalate": "verb",
}

HOST_TOOLS: frozenset[str] = frozenset(
    n for n, lane in LANE_OVERRIDES.items() if lane == "host")
RETIRED_TOOLS: frozenset[str] = frozenset(
    n for n, lane in LANE_OVERRIDES.items() if lane == "retired")
READ_OVERRIDES: frozenset[str] = frozenset(
    n for n, lane in LANE_OVERRIDES.items() if lane == "read")
VERB_TOOLS: frozenset[str] = frozenset(
    n for n, lane in LANE_OVERRIDES.items() if lane == "verb")

# Every name on a turn's list that is the Host's own verb rather than a
# registry tool: the orchestration verbs (session._orchestration_verbs), the
# two search doors (tool_doors.search_door_tools) and the registry verbs above.
# lane_for's name heuristics would call agent_status a read; a refusal that
# meets one of these names says to call it directly. Live, read(tool=
# 'agent_status') was once told to "dispatch an agent with it in tool_scope".
TURN_VERBS: frozenset[str] = frozenset({
    "dispatch", "operate", "recall", "agent_status", "find_tools", "read",
}) | VERB_TOOLS

# `gap` is a lane value lane_for can return, and the one no registry tool may
# hold; it is not a door anything is reached through.
LANES = ("read", "operate", "host", "dispatch", "verb", "retired", "gap")


def lane_for(name: str, schema: dict | None = None) -> str:
    """The lane the host reaches `name` through. Overrides first; then the
    same predicates the runtime always enforced (code-exec ⇒ nobody unless
    host-own; read-safe ⇒ read; direct op ⇒ operate; untrusted work ⇒
    dispatch). Anything else is a `gap`: no door, said as such."""
    n = (name or "").strip()
    if not n:
        return "gap"
    ov = LANE_OVERRIDES.get(n)
    if ov:
        return ov
    from mastercode.code_exec_guard import _is_code_exec_tool
    if _is_code_exec_tool(n):
        # A code-exec name nobody holds is unreachable by construction —
        # say so explicitly instead of letting it render as a dispatch
        # capability that dispatch then strips.
        return "retired"
    from mastercode.runtime.mcp_names import is_mcp_tool_name
    if is_mcp_tool_name(n):
        # A connected MCP server's tool runs through run_direct_op's MCP
        # branch, with its intent; it is never a read (tool_access).
        return "operate"
    from mastercode.runtime.tool_access import is_read_safe_tool
    if is_read_safe_tool(n, schema):
        return "read"
    from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS
    if n in DIRECT_OP_TOOLS:
        return "operate"
    if n in UNTRUSTED_WORK_TOOLS:
        return "dispatch"
    return "gap"


def is_host_tool(name: str) -> bool:
    return lane_for(name) == "host"


def is_retired_tool(name: str) -> bool:
    return lane_for(name) == "retired"
