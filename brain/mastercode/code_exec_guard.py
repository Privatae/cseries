"""
mastercode.code_exec_guard — the code-execution boundary.

Single source of truth for "is this a code-execution / software-build tool?"
(the security boundary). A dispatched in-container agent may
NEVER hold or run one of these — all executable code goes through the harness
pool (a disposable, OCI-isolated pod), and only the HOST reaches the pool,
through its own doors (HOST_DOOR_TOOLS). Callers of this predicate:

  - runtime/scoped_agent.py   enforces the wall: strips code-exec tools from a
                              dispatched agent's scope and blocks them at exec.
  - runtime/direct_ops.py     refuses a code-exec name under `operate`.
  - runtime/tool_lanes.py     the lane table: a code-exec name is lane "host"
                              (held natively by the host) or "retired" (held
                              by nobody) — never a dispatch capability.

Deliberately stdlib-only with no package side effects, so it can be imported
straight-from-file by the smoke gates.
"""

# The host's OWN doors to the harness pool (tool_lanes.py lane
# "host"): the host holds these natively and NOTHING else may — a dispatched
# agent never receives one (scoped_agent strips them), operate never runs one
# (direct_ops refuses them). Listing them here is what makes that true: every
# host door is code-exec by definition, whatever its name looks like. Before
# this, the multi-harness door and review.compare / review.propose_commit were
# not code-exec by NAME and so leaked into the agent lane — the wall checked
# the name, not what the tool ultimately runs (an agent could start harness
# pods through it); and ladder.run was code-exec by suffix but held by nobody.
HOST_DOOR_TOOLS = frozenset({
    "build",                  # the one build door: run (a build, or a compare
                              # across variants), plan, phase build, continue
    "ladder.run",             # the escalation ladder (rung → verify → climb)
    "review.compare",         # a compare's candidates, side by side (no pick)
    "review.propose_commit",  # apply the user's per-file picks into the worktree
    "review.adopt",           # copy ONE run's files into its project, unchanged —
    "review.adopt_undo",      # and put them back. Same act as propose_commit
                              # (a run's output lands in the project) with no
                              # model in the loop; the host's own, like the rest
                              # of the review family — never an agent's.
    "mesh_harness_run",       # a harness on the user's OWN mesh server, under a
                              # signed grant (every Host; one
                              # other than the operator gets mode='service' only,
                              # tools._exec_mesh_harness_run) — remote code
                              # execution the host drives itself
    "lab_run",                # the Lab Harness: the same lane with mode=lab to a
                              # LAB server — builds hosts + runs a test runbook
                              # there; billed at the lab rate. Host-driven, never
                              # an agent's
})

# Names that used to run code and are RETIRED: refused everywhere, held by
# nobody, and (per scripts/check_tool_lanes.py) carrying no schema. sdk_* were
# the removed sdk_build/sdk_continue primitives; <id>.run POSTed to the in-pod
# sidecar the pool replaced; harness_run never had a schema;
# claude_code was the "run the Agent SDK in your sandbox" tool from before the
# plan gate existed. review.fan_out is build(action='run', variants=[…]) now
# — one build door, no alias.
# editor was the Coding Tutor's door into the user's VS Code pod (reads, edits
# and `run`, a shell in that pod), removed with the editor.
RETIRED_CODE_TOOLS = frozenset({
    "sdk_build", "sdk_continue",
    "codex.run", "cursor.run", "grok.run",
    "harness_run",
    "claude_code",
    "review.fan_out", "review_fan_out",
    "editor",
})

# Belt-and-suspenders: an explicit set PLUS name patterns, so a newly-added
# coding tool is denied by default rather than slipping through.
_CODE_EXEC_TOOLS = frozenset({
    *HOST_DOOR_TOOLS,
    *RETIRED_CODE_TOOLS,
    "code", "bash", "shell", "exec",    # direct code / shell execution
})

# Read-only tools whose NAME trips a deny-by-default pattern below but which
# execute nothing. Deliberately narrow and explicit: every entry must be a pure
# read, so the patterns stay aggressive for everything NOT listed here. The
# explicit _CODE_EXEC_TOOLS set above still wins, so a name in both is denied.
#
#   harness_connections — SELECTs harness install/credential state
#     (runtime/tools_projects.py::_exec_harness_connections); declared read-only in both
#     the capability table (runtime/meta_tool_caps.py) and the public registry.
#     The `harness` prefix marked it code-exec anyway, which made it
#     unreachable by EVERY route (violates host-full-read-rights: a read
#     must never need a dispatched agent).
#   harness_costs — one SELECT over the priced-run rows (tokens, billed USD,
#     compute seconds) at runtime/tools_projects.py::_exec_harness_costs.
#   harness_transcript — one SELECT of a completed run's stored transcript
#     (runtime/tools_projects.py::_exec_harness_transcript).
# All three READ the record of a pool run; none starts one.
_CODE_EXEC_NAME_EXCEPTIONS = frozenset({
    "harness_connections",
    "harness_costs",
    "harness_transcript",
    # Read-only like its siblings: one SELECT over harness_usage_event, no
    # dispatch, no worktree, nothing executed. Without this the bare
    # `startswith("harness")` rule above classifies it code-exec, which makes
    # lane_for() call it 'retired' and check_tool_lanes red-fail the ship for
    # "retired tools still carry a schema" — caught by that gate.
    "harness_usage_windows",
})


def _is_code_exec_tool(name: str) -> bool:
    """True if `name` is a code-execution / software-build tool — a host door
    or a retired code tool — that must never be handed to a dispatched
    in-container agent and never run through `operate`."""
    n = (name or "").strip().lower()
    if n in _CODE_EXEC_TOOLS:
        return True
    if n in _CODE_EXEC_NAME_EXCEPTIONS:          # verified read-only; see above
        return False
    if n.startswith("mcp:"):
        # A remote MCP server's tool (mcp:<server>/<tool>, runtime/mcp_names):
        # it runs on that server, never in this pod or the harness pool, and
        # only through `operate`. Its name is the server's, so a tool called
        # `deploy.run` must not read as a local code door.
        return False
    if n.endswith(".run"):                       # codex.run / cursor.run / grok.run …
        return True
    if n.startswith("sdk_") or n.startswith("harness"):
        return True
    return False
