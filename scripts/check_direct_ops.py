#!/usr/bin/env python3
"""Invariant: direct-ops registry integrity (the deterministic ACT lane).

DIRECT_OP_TOOLS (runtime/direct_ops.py) routes single deterministic mutations
through `operate` with no model in the loop. Four things must stay true or the
lane either dead-ends or crosses a wall it must never cross:

  1. Every registry name is in META_TOOL_CAPS — a name outside it is
     default-denied at the caps gate, i.e. a visible-but-dead op (the exact
     bug class the schema⊆dispatch⊆caps smoke gate exists to end).
  2. No registry name is a code-exec/harness-pool tool (_is_code_exec_tool) —
     `operate` must be structurally unable to run code.
  3. No registry name is a code door, a read, or untrusted work — the doors
     that are not `operate` (_NEVER_DIRECT). A model or a
     wait inside the tool is not a reason to stay out: every in-platform
     tool is listed (the Host calls an agent only for untrusted work).
  4. Every registry name resolves to an executor: a meta-tool, a bridge op,
     or a mapped bot — otherwise run_direct_op returns 'Unknown tool'.

Run in-pod: docker exec <c> python3 /app/scripts/check_direct_ops.py
"""
import os
import sys

sys.path.insert(0, os.environ.get("CSERIES_BRAIN", "/app/brain"))

from mastercode.code_exec_guard import _is_code_exec_tool
from mastercode.runtime.direct_ops import DIRECT_OP_TOOLS
from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS
from mastercode.runtime.triage import (
    _BRIDGE_TOOL_NAMES,
    _META_TOOL_NAMES,
)

# Names that must never be a direct op. Mirrors the exclusion list documented
# in direct_ops.py: reads go through `read`, untrusted work through an agent,
# code through the Host's own doors.
_NEVER_DIRECT = frozenset({
    # reads of the web: the `read` door (a marked turn), never `operate`
    "research", "web_search", "read_url",
    # untrusted work outside the platform (tool_lanes.UNTRUSTED_WORK_TOOLS)
    "github",
    "build", "review.compare", "ladder.run", "mesh_harness_run",
    "review.fan_out",   # removed (build action='run' is the door)
    # retired names, kept so a revival can never land as a direct op:
    "github_search", "github_repo",   # both folded into `github`
    "codex.run", "cursor.run", "sdk_build", "sdk_continue",
    "compute_job", "bot_dispatch",
    # the controller ship primitives, retired (tool_lanes.RETIRED_SHIP_TOOLS)
    "docker_build", "docker_push", "git_tag", "k8s_set_image", "k8s_set_env", "systemctl_run",
    # a Host holds no shell on a machine:
    "session",
    # NOT here, deliberately: machine_exec (standing root on a
    # granted machine), mgmt_run and image_generate. Each acts
    # inside the platform; machine_exec and mgmt_run writes are refused on a turn
    # that read untrusted content (runtime/turn_taint), and none is schedulable.
})

fails: list[str] = []

missing_caps = sorted(DIRECT_OP_TOOLS - set(META_TOOL_CAPS))
if missing_caps:
    fails.append(f"not in META_TOOL_CAPS (dead at the caps gate): {missing_caps}")

code_exec = sorted(t for t in DIRECT_OP_TOOLS if _is_code_exec_tool(t))
if code_exec:
    fails.append(f"code-exec tools in the registry (harness-pool wall breach): {code_exec}")

banned = sorted(DIRECT_OP_TOOLS & _NEVER_DIRECT)
if banned:
    fails.append(f"reads, untrusted work or code doors in the registry: {banned}")

# The scheduling wall is UNATTENDED_OP_TOOLS, not absence from this registry.
# The tools that spend the user's money, broker a credential or act on a
# machine are in the registry, and must stay off a schedule.
from mastercode.runtime.direct_ops import UNATTENDED_OP_TOOLS  # noqa: E402
_NEVER_UNATTENDED = frozenset({"keys_manage", "connect_compute", "reachout_channel",
                               "image_generate", "mgmt_run", "machine_exec"})
scheduled = sorted(UNATTENDED_OP_TOOLS & _NEVER_UNATTENDED)
if scheduled:
    fails.append(f"credential/spend/machine tools in UNATTENDED_OP_TOOLS: {scheduled}")

executable = set(_META_TOOL_NAMES) | set(_BRIDGE_TOOL_NAMES)
unresolvable = sorted(DIRECT_OP_TOOLS - executable)
if unresolvable:
    fails.append(f"no executor resolves these (run_direct_op would say Unknown tool): {unresolvable}")

if fails:
    for f in fails:
        print(f"FAIL: {f}")
    sys.exit(1)

print(f"OK: {len(DIRECT_OP_TOOLS)} direct ops — all capped, executable, none a web read, "
      "untrusted work or a code door; no credential/spend/machine tool is schedulable")
