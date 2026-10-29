"""
runtime/scoped_agent.py — dispatched agents: in-container scoped agents.

The host is an orchestrator, not an agent (see docs/host-orchestrator-turn.md).
`dispatch` spawns an ephemeral agent that runs INSIDE the Host's container for
one read-only task: reading untrusted content from outside the platform (the
open web, a repository nobody on the platform owns, a message in the inbox)
and reporting back. It holds only the reader tools its scope names
(agent_reader_tools, or a connected MCP server's tool; scope_work_refusal turns
away any other work) and only the read capabilities its grant names
(agent_grant.AGENT_GRANT_NAMES); a grant naming a write is refused at the
dispatch door (dispatch_precheck, agent_grant.write_grant_refusal). Acting on
what it read is the Host's own work, in its own turn, after it has read the
report.

This is a clean, minimal re-use of the chat turn's engine — NOT a copy of
session.py's full loop (no nudge handling, no local-vLLM XML branch, no
fast/voice, no tier escalation). It reuses the real building blocks:

  - provider_gateway.call_model       — the model call (every registry
                                        provider + local; anthropic included,
                                        via its Messages-API dispatch)
  - _build_tool_schemas               — CEIGAS-authorized tool schemas
  - _execute_meta_tool                — the tool-exec branch
  - _json_safe                        — tool-result serialization guard
  - resolve_role_route("chat")        — a dispatched agent runs on the HOST's
                                        own route; there is no separate agents
                                        seat (three-role cut). A
                                        dispatch that names its own model
                                        still overrides it.

The Claude-subscription host engine (host_sdk_engine.py) and its agent loop
were DELETED; the generic loop below is the only agent loop.

Code never runs here. The host runs it itself, through its own doors
(code_exec_guard.HOST_DOOR_TOOLS: build, ladder.run, the review family,
mesh_harness_run, lab_run), machine_exec, and an op it wrote through mgmt_run;
this module's only part in that is the wall that keeps those tools out.
"""

import json
import logging
import time

from mastercode.provider_gateway import call_model

# inference_cost_log.source_module of every model call a dispatched agent
# makes (server/routes/inference.py _ACTIVITY_LABELS names it for the person).
AGENT_SOURCE_MODULE = "scoped_agent"
# The transcript wall (tool_result_wall): every tool result the agent feeds
# back is bounded and stays well-formed JSON — the same rule the host turn
# applies (session.py). Raw json.dumps let one
# oversized result overflow the provider with no marker.
from mastercode.runtime.tool_result_wall import _bound_tool_result
# Big results stay in the pod database, in an agent's loop as in the Host's
# (runtime/result_store): stdlib-only at import.
from mastercode.runtime import result_store as _result_store
from mastercode.inference_config import (
    normalize_provider,
    resolve_role_route,
)
from mastercode.runtime.tools import (
    _build_tool_schemas,
    _execute_meta_tool,
    _json_safe,
)
from mastercode.runtime.triage import (
    _META_TOOL_NAMES,
    _BRIDGE_TOOL_NAMES,
)
from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller

log = logging.getLogger("cseries.mastercode.scoped_agent")

# Tools whose one op is a spend the user must hear about first (the host's
# `operate` intent line). Stripped from every dispatch scope; the host runs
# them itself. See the strip below and runtime/model_bench.py.
HOST_STATED_SPEND_TOOLS: frozenset[str] = frozenset({"model_bench"})

# Tools only the Host runs, in its own turn, never an agent:
# a root command on a machine. Stripped from every dispatch scope and refused
# if an agent names it anyway; tools._exec_machine_exec refuses an agent's call
# too, and runtime/turn_taint gives an agent's run a state that never admits.
HOST_ONLY_MACHINE_TOOLS: frozenset[str] = frozenset({"machine_exec"})


# ── The safety boundary ───────────────────────────────────────────────────
# A dispatched in-container agent only reads and reports, and may NEVER execute
# or build code: the host runs code itself, through its own tools (see the
# module docstring). An agent can misread what it reads, but it cannot *break*
# anything, because changes and code execution are out of its reach.
# This separation is the security model, so it is enforced HERE, in code — not
# left to the host picking a good tool_scope.
#
# The predicate (explicit set PLUS name patterns) is the SINGLE source of truth
# in mastercode.code_exec_guard, shared with the host's own tool wall so the
# wall and the host's `build` routing hint can never drift apart.
from mastercode.code_exec_guard import _CODE_EXEC_TOOLS, _is_code_exec_tool  # noqa: F401

# The capability grant a host mints per dispatch. Floor is zero — see
# runtime/agent_grant.py for why bits are never derived from the tool list.
from mastercode.runtime.agent_grant import (
    AGENT_WRITE_BITS, CAP_DENIAL_KEY, NAME_TO_BIT, describe, grant_refusal, parse_grant,
    retry_grant, unreachable_tools, without_write, write_grant_refusal,
)


def _retry_grant_line(scope_set, grant_bits: int) -> str:
    """The one machine-readable line every grant refusal ends with.

    The refusal already knows the answer; before this it only stated the
    problem, and the host burned a whole tool round re-deriving it ("Tool round 2: ALL 1 tools failed — dispatch(error): ...
    manage_site (missing write_context); publish_site (missing writ…").
    The list is the UNION of granted + missing, less any write capability:
    the door refuses a grant holding one (agent_grant.write_grant_refusal),
    so a retry line offering it would only send the Host back to that wall.
    """
    return ("\nRetry with grant: "
            + json.dumps(without_write(retry_grant(scope_set, grant_bits))))


def _retry_for_denied(missing_bits: int, grant_bits: int) -> list[str] | None:
    """The grant that covers every read capability the gate found missing at
    execution for an untrusted-content reader — the run-time twin of
    retry_grant(). The caller passes only the bits its readers were missing;
    an in-platform tool's denial never reaches here, because the answer to it
    is the Host doing that part itself (_blocked_reason's `own_denied`). None
    when nothing a retry could add was missing."""
    missing = int(missing_bits) & ~AGENT_WRITE_BITS
    if not missing:
        return None
    bits = (int(grant_bits) | missing) & ~AGENT_WRITE_BITS
    return [n for n, b in sorted(NAME_TO_BIT.items(), key=lambda kv: int(kv[1]))
            if bits & int(b)]


def is_agent_reader(name) -> bool:
    """True for a tool an agent's scope may hold: one that reads untrusted
    content from outside the platform and runs in an agent rather than in the
    Host's own turn (agent_reader_tools), or a connected MCP server's tool.
    Everything else is in-platform work the Host does itself."""
    n = str(name or "").strip()
    return n.startswith("mcp:") or n in agent_reader_tools()


def unreachable_grant_refusal(unreachable: dict, scope_set, grant_bits: int, *,
                              at_door: bool) -> str:
    """The refusal for a scope holding tools whose capabilities the grant
    lacks: the door's (dispatch_precheck) and the run-time wall's, one text.

    It leads with the reason to stop rather than widen: a tool that does the
    Host's own platform work comes out of the scope and the Host does that
    part itself. Offering only "add the capabilities or drop the tools and
    dispatch a narrower agent" measured badly: four of five such refusals in
    a week were re-dispatched with a wider grant within
    25 seconds, three of them for in-platform work (a skill write, the
    platform-source proposal tool)."""
    detail = "; ".join(f"{k} ({v})" for k, v in sorted(unreachable.items()))
    tail = (" This refusal happened at the door — no task was created and the user "
            "has not been told anything." if at_door else "")
    text = (
        f"This dispatch cannot succeed as written: you scoped tools whose "
        f"capabilities you did not grant: {detail}. If such a tool does your own "
        f"platform's work (your files, your memory, your source, your operations), "
        f"do not widen the grant: drop it from `tool_scope` and do that part yourself "
        f"with read or operate in this turn. An agent holds only what reads untrusted "
        f"content from outside the platform; add a capability to `grant` only for "
        f"that. You gave the agent: {describe(grant_bits)}.{tail}"
    )
    # The machine-readable retry widens the grant only for a tool an agent
    # may hold (is_agent_reader, the set the dispatch door admits). An
    # in-platform tool comes out of the retried scope instead: a retry line
    # that added its capability was the one-step path back to dispatching the
    # Host's own work (four of five such refusals re-dispatched
    # with the wider grant within 25 seconds).
    own = sorted(t for t in unreachable if not is_agent_reader(t))
    retry_scope = scope_set
    if own:
        retry_scope = sorted(t for t in scope_set if t not in own)
        text += (f" {', '.join(own)} {'does' if len(own) == 1 else 'do'} your own "
                 "platform's work, so the retry below leaves "
                 f"{'it' if len(own) == 1 else 'them'} out: run "
                 f"{'it' if len(own) == 1 else 'them'} yourself.")
        if not retry_scope:
            return text
        text += "\nRetry with tool_scope: " + json.dumps(retry_scope)
    return text + _retry_grant_line(retry_scope, grant_bits)


def clerk_scope_refusal(tool_scope) -> str | None:
    """Refuse a dispatch whose ENTIRE scope already runs without a model.

    Origin (measured on one Host): every agent dispatched in the
    preceding 14 days — 6 agents, 15 opus-5 model calls — did ONE deterministic
    bookkeeping call. project_phase add_phase. workspace_file write.
    ship_project_app, publish_site, manage_site. All-time, every agent ever
    dispatched on that host was scoped to tools of exactly that kind. A model
    was paid to retype arguments the host was already holding, and the rule
    is the other way round: there should only be model calls when there is
    reasoning to be done.

    Those tools are in `operate`, which makes the cheaper lane available; it
    does not make the host take it. This is the wall
    that does — the same shape as the host's own "`kernel_op` is an operation,
    not a tool on your list: run it yourself through operate(...)" refusal
    (tool_doors.by_name_refusal), pointed the other way. It names the calls to make instead, so the answer
    arrives with the refusal rather than a round later.

    ROUTING, never authorization: it says WHERE the work runs, and every
    capability gate is untouched either way.

    It fires when every tool in scope is a FULLY-DIRECT operate tool, and names
    the operate call for each. A scope that is not all-operate goes on to the
    walls after it (scope_work_refusal): platform-source reads, a scope that
    reads nothing untrusted, and a scope with any in-platform tool in it.

    A scope of code_grep/code_read/code_tree, or diagnose/machine_map, is
    not "a well-built agent" on the reasoning that a read-only agent isolates
    untrusted material from the thread that holds write_memory. The isolation
    argument holds for content from outside the platform and only for it: the
    platform source, the Host's own repos and its own machines are not
    untrusted, and agents made 187 code_grep/code_read/code_tree calls on one
    Host in 14 days that were the Host's own reading (the Host calls an agent
    only for untrusted work).

    Two narrowings, both of which also make it fire less:

      - a scope holding a code-exec name is left alone. That request has its
        own wall with its own message (the build-door routing hint), and this
        refusal would mask it.
      - a tool with per-op exclusions (bug_review read/verdict) does NOT count
        as fully-direct, because a scope is a list of TOOLS and cannot say
        which op it means. Scoping `bug_review` means the validator.

    It lives at the DOOR only — dispatch_precheck, which the host's own
    dispatch tool call goes through — and deliberately not inside
    run_scoped_agent. The run-time walls there are safety walls, and a routing
    refusal fired in the background would strand a scheduled or standing
    dispatch with nobody present to re-issue it as an `operate`.
    """
    from mastercode.runtime.direct_ops import is_fully_direct_op_tool

    scope = [str(t).strip() for t in (tool_scope or []) if str(t).strip()]
    if not scope or any(_is_code_exec_tool(t) for t in scope):
        return None
    if not all(is_fully_direct_op_tool(t) for t in scope):
        return None  # a read to isolate, or something needing a model — dispatch

    lines = [f"operate(tool='{t}', args={{…}}, intent='…')"
             for t in sorted(set(scope))]
    return (
        "Every tool in this scope is a deterministic op that runs with no "
        "model in the loop. Dispatching an agent for it buys nothing and costs "
        "a model call, a grant you had to compose, and a transcription surface "
        "for arguments you are already holding. Run these yourself, now, in "
        "this turn: " + "; ".join(lines) + " — one exact call each, result "
        "returns to you now. No task was created and the user has not been "
        "told anything. " + _agent_reader_sentence()
    )


def agent_reader_tools() -> list[str]:
    """The tools a dispatch can scope to read untrusted content: the
    untrusted-source set (runtime/turn_taint) less the ones that are
    fully-direct operate ops. gitea, host_message and community_board read
    content from outside too, but they run in the Host's own turn (the turn
    is marked), so a scope of one of them is refused above as clerk work;
    naming them as an agent's tools would send the Host back to this door."""
    from mastercode.runtime.direct_ops import is_fully_direct_op_tool
    from mastercode.runtime.turn_taint import untrusted_source_tools
    return sorted(t for t in untrusted_source_tools() if not is_fully_direct_op_tool(t))


def _agent_reader_sentence() -> str:
    return ("An agent is only for reading content you do not own, with a tool that "
            "reads it: " + ", ".join(agent_reader_tools())
            + ", or a connected MCP server's tool (mcp:<server>/<tool>). Scope one of "
            "those, name the content with `work`, and dispatch again.")


# The Host's own code reads: the platform source (code_*: the operator's
# allowlisted platform repos) and the repos it owns or was granted
# (search_my_code, index_my_code). None of them reaches content from outside.
OWN_CODE_READ_TOOLS: frozenset[str] = frozenset({
    "code_grep", "code_read", "code_tree", "search_my_code", "index_my_code",
})


def own_code_scope_refusal(tool_scope) -> str | None:
    """Refuse a dispatch whose scope holds any of the Host's own code reads,
    whatever else it holds. A mixed scope (code_grep + web_search) would let
    an agent read the platform source beside the web, which is the Host's own
    reading done by someone else."""
    scope = {str(t).strip() for t in (tool_scope or []) if str(t).strip()}
    own = sorted(scope & OWN_CODE_READ_TOOLS)
    if not own:
        return None
    calls = "; ".join(f"read(tool='{t}', args={{…}})" for t in own)
    return (
        f"{', '.join(own)} read the platform source and your own or granted "
        "repos: that is your own reading, never an agent's. Call them yourself "
        f"in this turn: {calls}. If a part of the task reads content you do not "
        "own (the web, a repo you were not granted, another Host's or person's "
        "message), dispatch again with only the tools that read it. No task "
        "was created and the user has not been told anything."
    )


def untrusted_scope_refusal(tool_scope) -> str | None:
    """Refuse a dispatch whose scope reaches no untrusted content (the
    Host calls an agent only for untrusted work outside the
    platform). The set is runtime/turn_taint's, the one that marks a turn
    for what it read: the web, a foreign repo, MCP output, another Host's or
    person's message. bug_review counts as a message (the validator reads a
    report another Host filed). Code-exec names are left to their own wall
    and do not count."""
    from mastercode.runtime.turn_taint import is_untrusted_source_tool
    scope = [str(t).strip() for t in (tool_scope or []) if str(t).strip()]
    if any(is_untrusted_source_tool(t) for t in scope):
        return None
    ce = sorted(t for t in scope if _is_code_exec_tool(t))
    rest = sorted(t for t in scope if not _is_code_exec_tool(t))
    head = (f"Nothing in this scope ({', '.join(rest)}) reads content you do not own"
            if rest else "This scope reads nothing")
    tail = ""
    if ce:
        from mastercode.code_exec_guard import HOST_DOOR_TOOLS
        tail = (f" {', '.join(ce)} runs code, which only your own build tools do "
                f"({', '.join(sorted(HOST_DOOR_TOOLS))}), called directly.")
    return (
        head + ", so it is your own work, not an agent's. Do it yourself in this "
        "turn: read(tool=…) for a read, operate(tool=…) for one operation." + tail
        + " " + _agent_reader_sentence()
        + " No task was created and the user has not been told anything."
    )


def in_platform_scope_refusal(tool_scope) -> str | None:
    """Refuse a scope that holds any tool other than an untrusted-content
    reader (is_agent_reader), whatever readers sit beside it. Passing a
    scope once one tool in it reads something untrusted would let
    ['workspace_file', 'read_url'] dispatch an agent that wrote the
    Host's files beside reading the web: the Host's own work, done by
    something that had just read a page. The agent hands back its report as
    its final answer, and pages a long result of its own through the `read`
    the run adds for it (result_store.AGENT_READ_SCHEMA), so a reader scope
    is all it needs to return what it found."""
    scope = sorted({str(t).strip() for t in (tool_scope or []) if str(t).strip()})
    own = [t for t in scope if not is_agent_reader(t)]
    if not own:
        return None
    readers = [t for t in scope if is_agent_reader(t)]
    ce = [t for t in own if _is_code_exec_tool(t)]
    rest = [t for t in own if not _is_code_exec_tool(t)]
    parts: list[str] = []
    if rest:
        parts.append(
            f"{', '.join(rest)} {'does' if len(rest) == 1 else 'do'} your own platform's "
            "work, which you do yourself in this turn: read(tool=…) for a read, "
            "operate(tool=…) for one operation, after the agent's report is back if "
            "it depends on what the agent found")
    if ce:
        from mastercode.code_exec_guard import HOST_DOOR_TOOLS
        parts.append(f"{', '.join(ce)} {'runs' if len(ce) == 1 else 'run'} code, which only "
                     f"your own build tools do ({', '.join(sorted(HOST_DOOR_TOOLS))}), "
                     "called directly")
    return (
        "An agent holds only tools that read content from outside the platform. "
        + "; ".join(parts) + ". Dispatch again with tool_scope: " + json.dumps(readers)
        + ". No task was created and the user has not been told anything."
    )


def scope_work_refusal(tool_scope) -> str | None:
    """The door's answer about WHAT a dispatch's scope is for, before any
    question about its grant: clerk work, the Host's own code reads, no
    untrusted reading at all, or in-platform tools beside the readers. None
    when every tool in the scope reads untrusted content."""
    return (clerk_scope_refusal(tool_scope)
            or own_code_scope_refusal(tool_scope)
            or untrusted_scope_refusal(tool_scope)
            or in_platform_scope_refusal(tool_scope))


def dispatch_precheck(tool_scope, grant) -> str | None:
    """The grant wall's refusal, computed at the dispatch DOOR — synchronously,
    before any task row exists. Returns the refusal message, or None when the
    dispatch is composable.

    Origin: two dispatched tasks. The wall
    fired inside the background agent, so a mis-composed grant became a
    created-then-failed task delivered to the USER as "the task I dispatched
    didn't complete" — a host bookkeeping slip turned into a user-visible
    incident, twice. Checked here, the same refusal comes back as the dispatch
    tool call's own error and the host fixes its grant in the same turn; the
    user never sees anything. The run-time wall below stays — defense in
    depth, and dispatch_sync callers don't pass this door.

    The scope's purpose is judged FIRST (scope_work_refusal: clerk work, the
    Host's own code reads, a scope with no untrusted reading, any in-platform
    tool beside the readers; a code-exec name is one of those, so it never
    reaches the reachability check). Then the grant: a name that is no
    capability, then a write capability (agent_grant.write_grant_refusal: an
    agent reads and reports, the acting is the Host's). A scope that should not be dispatched at all
    gets that answer instead of a grant to fix, because fixing the grant and
    re-dispatching only arrives at the same wall a round later — and the whole
    cost this refusal exists to remove is rounds."""
    why = scope_work_refusal(tool_scope)
    if why is not None:
        return why
    scope_set = {str(t) for t in (tool_scope or [])
                 if not _is_code_exec_tool(str(t))}
    bits, unknown = parse_grant([str(g) for g in (grant or [])])
    if unknown:
        return grant_refusal(unknown)
    wrote = write_grant_refusal(bits)
    if wrote is not None:
        return wrote
    unreachable = unreachable_tools(scope_set, bits)
    if unreachable:
        return unreachable_grant_refusal(unreachable, scope_set, bits, at_door=True)
    return None


# A model that has seen its tools on the OpenAI wire sometimes writes the
# wire's namespace in front of a name. A dispatch with
# tool_scope=['functions.context_ingest'] matched no tool, the agent started
# with none, answered "I cannot access or read documents directly", and that
# sentence reached the user as the result.
_WIRE_PREFIXES = ("functions.",)


def unknown_scope_names(tool_scope, offered) -> dict[str, str | None]:
    """{name: the real tool it most likely meant, or None} for every scope name
    that is no tool at all. `offered` is the name set _build_tool_schemas
    returns for the entity, the same set run_scoped_agent filters against; it
    supplies the suggestion. A name some registry knows is never counted here
    even when `offered` lacks it: the builder reads the entity's bridge access
    from the DB and treats a failed read as "no access", so a real tool can be
    missing from `offered` for a reason that is not the host's. Those names
    reach run_scoped_agent, which logs them and refuses to start an agent left
    with nothing. Code-exec names and host-stated spend tools are left out:
    each has its own wall with its own message."""
    import difflib
    from mastercode.runtime.tool_call_ledger import known_tool_schema

    offered = {str(n) for n in (offered or ()) if n}
    out: dict[str, str | None] = {}
    for raw in tool_scope or []:
        name = str(raw)
        if (name in offered or _is_code_exec_tool(name) or name in HOST_STATED_SPEND_TOOLS
                or name in HOST_ONLY_MACHINE_TOOLS):
            continue
        if known_tool_schema(name) is not None:
            continue
        hint = next((name[len(p):] for p in _WIRE_PREFIXES
                     if name.startswith(p) and name[len(p):] in offered), None)
        if hint is None:
            close = difflib.get_close_matches(name, sorted(offered), n=1, cutoff=0.8)
            hint = close[0] if close else None
        out[name] = hint
    return out


def nearest_tools(name: str, schemas, *, intent: str = "", limit: int = 3) -> list[str]:
    """The offered tools find_tools' search ranks highest for `name` and the
    dispatch's stated intent, best first.

    unknown_scope_names only catches a spelling slip (a wire prefix, a
    0.8 difflib match). An invented name is not a slip: in a live test run a
    gpt-4o-mini Host dispatched tool_scope ['filesystem'] with intent "Create a
    file in the workspace", was refused with no tool named, and told the user
    it could not do it (two runs). The tool it needed
    (workspace_file) is what this search returns for that intent."""
    try:
        from mastercode.runtime.tool_lanes import lane_for
        from mastercode.runtime.tool_search import ToolIndex
        by_name = {(s.get("function") or {}).get("name"): s
                   for s in schemas or () if isinstance(s, dict)}
        by_name.pop(None, None)
        # Only what an agent can hold. `schemas` is the Host's whole
        # authorized catalog, which also carries its own build doors and
        # verbs; offering `build` for "build me a website" sends the Host from
        # this refusal into the code-exec wall (run_scoped_agent strips it),
        # and the spend and machine tools have walls of their own
        # (unknown_scope_names leaves them to those).
        by_name = {n: s for n, s in by_name.items()
                   if not _is_code_exec_tool(n) and n not in HOST_STATED_SPEND_TOOLS
                   and n not in HOST_ONLY_MACHINE_TOOLS
                   and lane_for(n, s) not in ("verb", "host", "retired", "gap")}
        idx = ToolIndex.from_schemas(by_name)
        return [n for n, _ in idx.search(f"{name} {intent}".strip(), limit=limit)]
    except Exception as e:  # noqa: BLE001 — a refusal without candidates still refuses
        log.info("scoped_agent: nearest-tool search failed for %r: %s", name, e)
        return []


def unknown_tools_refusal(unknown: dict[str, "str | list[str] | None"]) -> str | None:
    """The door's answer to a scope naming tools that do not exist here. A
    hint is the one tool a slip meant (str), or the closest tools an invented
    name's search found (list)."""
    if not unknown:
        return None

    def _part(n, h):
        if isinstance(h, list) and h:
            return f"'{n}' (closest: " + ", ".join(f"'{t}'" for t in h) + ")"
        if h:
            return f"'{n}' (the tool is '{h}')"
        return f"'{n}'"

    parts = [_part(n, h) for n, h in sorted(unknown.items())]
    return (
        "This dispatch names tools that do not exist: " + ", ".join(parts)
        + ". An agent only ever receives tools that exist, so it would have "
        "started without these. Write each name exactly as find_tools returns "
        "it (search by what the tool does), with nothing in front of it, and "
        "dispatch again now, in this turn. This refusal happened at the door — "
        "no task was created and the user has not been told anything."
    )


async def scope_name_refusal(pool, entity_id: int, tool_scope, *,
                             intent: str = "") -> str | None:
    """unknown_tools_refusal against the tools this entity's agents can hold.
    A name no slip explains gets the closest offered tools for it and the
    dispatch's `intent` (nearest_tools).

    A schema build that fails leaves the door open: the run-time check in
    run_scoped_agent still refuses to start an agent left with no tools."""
    try:
        schemas = await _build_tool_schemas(pool, entity_id, interaction_context=None)
    except Exception as e:  # noqa: BLE001
        log.warning("scoped_agent: door tool-name check skipped (eid=%s): %s", entity_id, e)
        return None
    offered = {(s.get("function") or {}).get("name") for s in schemas if isinstance(s, dict)}
    unknown: dict = unknown_scope_names(tool_scope, offered)
    for n, h in list(unknown.items()):
        if h is None:
            unknown[n] = nearest_tools(n, schemas, intent=intent) or None
    return unknown_tools_refusal(unknown)


def _model_catalog() -> dict[str, str]:
    """The ids OpenRouter itself lists, lowercased → OpenRouter's own
    spelling, from the catalog the Host already holds
    (server.routes.inference._CATALOG_CACHE, warmed at startup, never fetched
    here). Empty until that list is warm.

    Only OpenRouter's list, and every row of it. The door judges a bare id
    that run_scoped_agent will send to OpenRouter, so OpenRouter's own list is
    the authority: a bare first-party id from the route's direct list rides
    OpenRouter on that branch too, and a Host's Kimi id came back "not a
    valid model ID" exactly that way (see the chat-route branch there). Every row, not cached_context_lengths(): that drops a row with no
    context_length, and the door would refuse a model OpenRouter lists.

    Empty means unknown, never "nothing is runnable": warm_route_catalog fills
    the route's first-party rows first, and the startup build waits up to 30s
    and leaves the OpenRouter list empty when its fetch fails."""
    try:
        from server.routes.inference import _CATALOG_CACHE
        rows = (_CATALOG_CACHE.get("data") or {}).get("openrouter_models") or []
        return {str(r["id"]).strip().lower(): str(r["id"]).strip() for r in rows
                if isinstance(r, dict) and r.get("id")}
    except Exception as e:  # noqa: BLE001
        log.debug("scoped_agent: model catalog unavailable at the door: %s", e)
        return {}


def catalog_spelling(model: str, catalog) -> str:
    """`model` as OpenRouter spells it, when its catalog lists it in another
    case (a ':variant' kept as given), else `model` unchanged. The door
    compares lowercased, so what passes it is sent in OpenRouter's own form
    and never fails there on case alone."""
    m = (model or "").strip()
    if not catalog or not m:
        return m
    if m.lower() in catalog:
        return catalog[m.lower()]
    base, sep, variant = m.partition(":")
    if sep and base.lower() in catalog:
        return catalog[base.lower()] + sep + variant
    return m


def unknown_model_refusal(model: str, routed_model: str | None, catalog) -> str | None:
    """The refusal for a dispatch-named model the catalog (lowercased ids,
    _model_catalog) does not list, or None. An empty catalog refuses nothing:
    a cold cache is not evidence."""
    if not catalog:
        return None
    for m in (model, routed_model):
        m = (m or "").strip().lower()
        # OpenRouter takes a routing variant after a colon (":nitro",
        # ":online") on a model it lists only by its base id.
        if m and (m in catalog or m.split(":", 1)[0] in catalog):
            return None
    return (f"No agent was started: `{model}` is not a model this Host can run. It is not "
            "the model you run on, no machine on the user's network serves it, and the model "
            "catalog does not list it. Dispatch again with `model` left out, and the agent "
            "runs on the model you run on.")


async def model_name_refusal(pool, entity_id: int, model: str | None,
                             turn_type: dict | None = None) -> str | None:
    """Refuse, at the door, a dispatch that names a model this Host cannot run.

    Measured in a live test run: asked for one image,
    gpt-4o-mini dispatched with model='default'. A bare id rides
    OpenRouter unchecked (run_scoped_agent's per-task model branch), so the
    agent died on "default is not a valid model ID", and the provider's 400
    was all the Host heard. It guessed again: 'image', 'dall_e', 'midjourney',
    'stable-diffusion-v2' and on, 21 dispatches in 50 seconds, until the
    test's spend ceiling stopped the run. On a user's Host nothing stops it but the
    user's key. Same fault as an invented tool name (scope_name_refusal), one
    argument over.

    Passes: no model; the Host's own chat model; a name normalize_provider
    sends to a provider other than OpenRouter (that path has its own checks);
    a model a machine on the user's network serves, including one a typed
    turn will refuse (run_scoped_agent refuses that with its own reason); and
    an id the catalog lists."""
    m = (model or "").strip()
    if not m:
        return None
    if await _is_chat_route_model(pool, entity_id, m):
        return None
    try:
        provider, routed = normalize_provider("openrouter", m)
    except Exception:  # noqa: BLE001 — the run-time path decides
        return None
    if provider != "openrouter":
        return None
    try:
        hit, off_type = await _local_model_for(pool, entity_id, m, turn_type)
    except Exception:  # noqa: BLE001
        hit, off_type = None, []
    if hit or off_type:
        return None
    return unknown_model_refusal(m, routed, _model_catalog())


def attached_file_names(attachments) -> list[str]:
    """Names of the documents and text files attached to this turn, as
    send_message receives them: the stored name on the attachment (Circles
    writes it there), else a text file's content_block.filename. Images are
    left out — a model that reads no pictures gets a note, not the image."""
    names: list[str] = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        blk = att.get("content_block") or {}
        if not isinstance(blk, dict) or blk.get("type") not in ("document", "text_file"):
            continue
        n = str(att.get("name") or blk.get("filename") or "").strip()
        if n:
            names.append(n)
    return names


def attached_file_refusal(task: str, system_prompt: str, attached_names) -> str | None:
    """Refuse a dispatch that hands an agent a file attached to this turn.

    The Host reads a file uploaded to it itself (a *read* function, never an AI
    dispatch), and the attachment's content
    is already in the user's message this turn. An agent receives only the
    text its host writes for it, never the attachment, so it could not read
    the file anyway: an agent once answered "I cannot
    access or read documents directly". A name of the attached file in the
    agent's task or system prompt is the signal."""
    text = f"{task or ''}\n{system_prompt or ''}".lower()
    hits = sorted({str(n) for n in (attached_names or ()) if n and str(n).lower() in text})
    if not hits:
        return None
    names = ", ".join(f"'{n}'" for n in hits)
    return (
        f"{names} is attached to this turn, and its content is already in the "
        "user's message — its text, or the document itself, or a note saying "
        "why it could not be included. Read it there yourself. A dispatched "
        "agent receives only the text you write for it, never the attachment, "
        "so it cannot read this file. If the user asked a question about the "
        "file, answer it from the message. If an agent must act on what the "
        "file says, copy the part it needs into the task and dispatch again "
        "without naming the file. No task was created and the user has not "
        "been told anything."
    )


# Short role line the scoped agent runs under. The host writes the actual task
# (and any extra framing) into task_prompt / system_preamble.
# The immutable governance WALL wrapped around every dispatched agent. It is NOT
# the host's persona or rulebook (an agent must look nothing like the host): it states only the non-negotiable guardrails of an ephemeral
# scoped session. The task-specific SUBSTANCE is the host-written system prompt
# (system_preamble), composed in below — the host writes it fresh per task.
_SCOPED_AGENT_ROLE = (
    "You are an ephemeral, single-purpose agent — a fresh session with NO "
    "persistent identity, memory, or trust of your own. Your host dispatched you "
    "to complete ONE task and then stop. You are NOT the host and must not act "
    "like it.\n"
    "- You hold ONLY the tools granted for this task; every call is "
    "CEIGAS-permission-governed and anything outside your grant is refused.\n"
    "- You CANNOT execute or build code — that is a separate, walled lane.\n"
    "- Stay strictly inside the task your host wrote for you below: do not widen "
    "scope, do not act beyond it, do not ask the user follow-up questions.\n"
    "- Identifiers in your task (filenames, keys, ids) are RELAYED transcriptions "
    "and may be abbreviated or mangled: resolve them against a listing tool "
    "before use, and NEVER conclude something is missing from one exact-match "
    "miss — an empty result on a mangled key looks identical to absence.\n"
    "- When finished, report the result concisely; if blocked, say what you "
    "found and what blocks you.\n"
    "The instructions that follow were written by your host specifically for "
    "this task — they define exactly what you are for. Follow them."
)


def _blocked_reason(*, code_exec=(), out_of_grant=(), unreachable=None,
                    gate_denied=None, retry=None, unknown=(), host_spend=(),
                    outside_type=(), type_name=None) -> str:
    """One plain sentence saying WHY a run's verdict is 'blocked' — which of
    the four walls fired, naming the tools. Stored on the run record next to
    denied_tools and rendered verbatim by the tray. Empty when nothing was
    refused (the caller only stores it beside a non-empty denied_tools).

      code_exec    — build-tool names stripped from the scope / blocked at exec:
                     an ORCHESTRATION mistake (the host must call `build`).
      out_of_grant — names the agent CALLED but was never granted.
      unreachable  — {tool: 'missing cap, cap'} from unreachable_tools(): the
                     grant did not cover the tool's capabilities, refused at
                     dispatch before the agent ran.
      gate_denied  — {tool: 'missing cap'} for a GRANTED tool whose call the
                     capability gate refused at execution (the envelope
                     carries CAP_DENIAL_KEY; other denials never land here).
                     The door passes a
                     read/write tool on its read-safe minimum, so a write op
                     under a read-only grant is only stopped here (a
                     workspace_file write under
                     read_context — the run scored 'pass' with nothing written).
      retry        — the grant that would have covered every denied READER
                     call, rendered as the same machine-readable line the
                     door uses. A gate_denied tool that is not a reader
                     (is_agent_reader) is named as the Host's own work instead
                     and adds nothing to it: a workspace_file write denied
                     inside an agent must not come back "Retry with grant:
                     [read_context, write_context]", the path back to an agent
                     doing the Host's in-platform work.
      unknown      — scope names that are not tools this entity's agents can
                     hold; when none were left the agent was never started.
      host_spend   — HOST_STATED_SPEND_TOOLS stripped from the scope: the host
                     runs them itself through operate.
      outside_type — names stripped because the DISPATCHING TURN's type does
                     not hold them (runtime/turn_types.py): an agent can never
                     hold more than the turn that sent it. `type_name` names it.
    """
    parts: list[str] = []
    ce = sorted(code_exec or ())
    if ce:
        parts.append(
            f"{', '.join(ce)}: executes/builds code — that runs on the harness pool "
            "through the host's own build tools, never a dispatched agent")
    ot = sorted(outside_type or ())
    if ot:
        parts.append(
            f"{', '.join(ot)}: outside the dispatching turn's type {type_name!r} — "
            "an agent cannot hold more than the turn that sent it")
    un = sorted(unknown or ())
    if un:
        parts.append(f"not a tool this Host has: {', '.join(un)}")
    hs = sorted(host_spend or ())
    if hs:
        parts.append(f"{', '.join(hs)}: spends on the user's key — the host runs it "
                     "itself through operate")
    if unreachable:
        parts.append(
            "capability grant too narrow — "
            + "; ".join(f"{k} ({v})" for k, v in sorted(unreachable.items())))
    if gate_denied:
        parts.append(
            "denied at execution by the capability gate — "
            + "; ".join(f"{k} ({v})" for k, v in sorted(gate_denied.items())))
        own = sorted(t for t in gate_denied if not is_agent_reader(t))
        if own:
            parts.append(
                f"{', '.join(own)} {'does' if len(own) == 1 else 'do'} your own platform's "
                "work: do that part yourself with read or operate in this turn, and do not "
                "dispatch it again with a wider grant")
    og = sorted(out_of_grant or ())
    if og:
        parts.append(f"called outside its grant: {', '.join(og)}")
    text = "; ".join(parts)
    if retry:
        text += ". Retry with grant: " + json.dumps(list(retry))
    return text


async def _emit(progress_cb, kind: str, **payload) -> None:
    """Emit one non-tool progress frame (text / thinking / status).

    The activity recorder only ever saw tool_call_start / tool_call_result, so
    the Agents view could show WHICH tools an agent called and never WHAT it was
    thinking. These frames close that gap. Best-effort by contract — a progress
    consumer must never be able to fail a run (same rule the tool frames and the
    W5 heartbeat already follow).
    """
    if progress_cb is None:
        return
    try:
        await progress_cb({"event": kind, **payload})
    except Exception as _cb_err:                      # noqa: BLE001
        log.debug("scoped_agent progress_cb %s raised (non-fatal): %s",
                  kind, _cb_err)


async def agent_round_budget(pool, entity_id: int) -> dict:
    """How many tool rounds a dispatched agent may run: {rounds, backstop}.

    The agent gets the SAME runaway backstop as the Host's own turn
    (turn_allowance.backstop_rounds: the host's
    behavioral_params.max_tool_rounds, then env CSERIES_MAX_TOOL_ROUNDS, then
    BACKSTOP_TOOL_ROUNDS; <= 0 turns it off). It is the platform's, never the
    model's: when a dispatch could carry `max_rounds`, a Host set 8 on a real
    task and its agent stopped there (64 tool calls in 37 s). A cap on real
    work was never wanted, so the dispatch has no such argument.

    Never raises: a failed settings read uses the env/constant backstop."""
    from mastercode.runtime import turn_allowance as _ta
    bp = None
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id=$1", entity_id)
        bp = row["behavioral_params"] if row else None
        if isinstance(bp, str):
            bp = json.loads(bp)
        if not isinstance(bp, dict):
            bp = None
    except Exception as e:  # noqa: BLE001 — the default backstop still applies
        log.debug("agent_round_budget: settings read failed (default backstop): %s", e)
        bp = None
    backstop = _ta.backstop_rounds(bp)
    return {"rounds": backstop, "backstop": backstop}


def cut_call_text(func_name: str, limit, arg_chars: int) -> str:
    """The tool result for a call the model's output limit cut off mid-way
    through its arguments. Nothing ran — running it with {} would make the
    tool answer with a misleading "requires X" error and the model blame
    itself."""
    lim = f"{int(limit):,} tokens" if isinstance(limit, int) and limit > 0 else "its limit"
    return (f"NOT RUN: your output reached this model's output limit ({lim}) while you "
            f"were writing the arguments of this `{func_name}` call, so the call arrived "
            f"incomplete ({arg_chars:,} characters of arguments) and nothing ran. Write it "
            "in parts: for a large file, create it with the first part, then add the rest "
            "in further calls, each one small enough to finish.")


def bad_args_text(func_name: str, err: str, raw: str) -> str:
    """The tool result for a call whose arguments were not valid JSON (and
    were not cut off). Nothing ran."""
    head = (raw or "")[:200]
    return (f"NOT RUN: the arguments of this `{func_name}` call were not valid JSON "
            f"({err}), so nothing ran. They began: {head!r}. Send the call again with "
            "a JSON object as its arguments.")


def continue_text(limit, wrote_text: bool) -> str:
    """What the agent is told when its output limit stopped a plain-text
    answer: where it stopped, and to carry on from there."""
    lim = f"{int(limit):,} tokens" if isinstance(limit, int) and limit > 0 else "its limit"
    if wrote_text:
        return (f"Your answer stopped at this model's output limit ({lim}), part-way "
                "through. Continue from exactly where it stopped, without repeating "
                "what you already wrote.")
    return (f"This model's output limit ({lim}) was used up before you wrote any of "
            "your answer. Write it now, in parts if it is long.")


def rounds_exhausted_text(rounds: int) -> str:
    """The run's error when the loop ran out of rounds: the host's runaway
    backstop, the only round bound an agent has."""
    return (f"stopped at the runaway backstop of {rounds} tool rounds before "
            "producing a final answer — work is incomplete. The backstop exists "
            "only to end a loop that never finishes; real work this long is split "
            "into smaller dispatches, or the backstop is raised with "
            "behavioral_params.max_tool_rounds (0 or less turns it off).")


def agent_task_id(session_id) -> int | None:
    """The task id in a dispatched agent's session id ('agent:<task_id>',
    AgentDispatch._create_agent_task), or None for any other session."""
    sid = session_id if isinstance(session_id, str) else ""
    if not sid.startswith("agent:"):
        return None
    tail = sid.split(":", 1)[1]
    return int(tail) if tail.isdigit() else None


async def _is_chat_route_model(pool, entity_id: int, model: str) -> bool:
    """True when `model` names the host's own routed chat model."""
    try:
        _r = await resolve_role_route(pool, entity_id, "chat")
    except Exception:
        return False
    if _r.get("unconfigured"):
        return False
    return (_r.get("model") or "").strip().lower() == (model or "").strip().lower()


async def _local_model_for(pool, entity_id: int, model: str,
                           turn_type: dict | None) -> tuple[dict | None, list[str]]:
    """The machine on the user's network that runs `model` for this agent,
    and the servers serving it that the dispatching turn's type does not
    list. Untyped: the first server, as before. Typed: the first server the
    type lists ('*' = any). A model served only elsewhere returns those
    servers, so the caller refuses instead of sending the prompt to a cloud
    model in its place."""
    from mastercode.model_backends import local_model_servers, match_local_model
    if turn_type is None:
        return await match_local_model(pool, entity_id, model), []
    hit = await match_local_model(pool, entity_id, model,
                                  allowed=set(turn_type.get("bastions") or []))
    if hit:
        return hit, []
    return None, await local_model_servers(pool, entity_id, model)


def _off_type_server_refusal(entity_id: int, model: str, servers: list[str],
                             type_name) -> dict:
    """The agent is not started: its model runs only on servers the
    dispatching turn's type does not list. lane_unavailable, because the
    type refused the route and the run says nothing about the model."""
    _msg = (f"This agent was not started: {model!r} runs on your network only on "
            f"{', '.join(repr(s) for s in servers)}, which this turn's type "
            f"{type_name!r} does not list — an agent cannot reach more than the "
            "turn that dispatched it, and it is not moved to a cloud model in its "
            "place; say in the thread what you could not do and which types hold it.")
    log.warning("scoped_agent: %s (eid=%s)", _msg, entity_id)
    return {"result": _msg, "error": _msg, "lane_unavailable": True,
            "tool_actions": [], "denied_tools": [],
            "tokens": 0, "tokens_in": 0, "tokens_out": 0, "rounds": 0,
            "model": "", "lane": ""}


async def run_scoped_agent(
    pool, *,
    entity_id: int,
    task_prompt: str,
    tool_scope: list[str],          # explicit allowlist of tool names this agent may use
    grant: list[str] | None = None,  # capability NAMES the host elevates for this task
    model: str | None = None,       # explicit model (else the host's chat route)
    provider: str | None = None,    # explicit provider id (else the host's chat route)
    system_preamble: str | None = None,
    interaction_context: str = "scoped_agent",
    session_id: str | None = None,   # attributes inference cost + tool logs to this agent run
    # The round ceiling the PLATFORM resolved: AgentDispatch passes the host's
    # runaway backstop (agent_round_budget); None = read it here. Never the
    # model's — a dispatch has no round argument.
    max_rounds: int | None = None,
    progress_cb=None,
    # Turn types: the profile of the HOST turn that dispatched
    # this agent, or None. An agent can never exceed the turn that sent it:
    # tool_scope ∩ the type's groups, grant ∩ the type's bits, and every
    # tool call carries the type into the gate (runtime/turn_types.py).
    turn_type: dict | None = None,
) -> dict:
    """Run an ephemeral, in-container scoped (dispatched) agent. A clean, minimal tool
    loop — NOT a copy of session.py's. Reuses call_model + _execute_meta_tool +
    _build_tool_schemas + _json_safe. Returns
    {result: str, tool_actions: list, tokens: int, rounds: int}."""

    # ── 1. Build the entity's authorized tool schemas, then filter to the
    # explicit allowlist. A name in tool_scope that the entity isn't
    # authorized for (or that the surface hides) simply doesn't appear in the
    # built set, so it's silently omitted — never an error.
    _schemas_failed = False
    try:
        # A dispatched agent is a BACKGROUND caller, not a chat surface. Pass
        # interaction_context=None so _build_tool_schemas does NOT surface-filter
        # — a scoped agent's authorization is the explicit tool_scope plus the
        # entity's CEIGAS permissions (both preserved), never an app surface.
        # (`interaction_context` is retained only as a log label.)
        all_schemas = await _build_tool_schemas(
            pool, entity_id, interaction_context=None,
        )
    except Exception as e:
        log.warning("scoped_agent: tool schema build failed (eid=%s): %s", entity_id, e)
        all_schemas = []
        _schemas_failed = True

    scope_set = set(tool_scope or [])
    # Safety boundary: a dispatched agent never holds a code-exec/build tool, even if
    # one was requested in tool_scope. Strip them here and log it loudly — the
    # host should never ask, but the guard is the wall, not the host's good sense.
    _denied = {n for n in scope_set if _is_code_exec_tool(n)}
    if _denied:
        log.warning(
            "scoped_agent: refusing code-exec tools in a dispatch scope (eid=%s): %s "
            "— code runs only on the harness pool via the host's own build tools",
            entity_id, sorted(_denied),
        )
        scope_set -= _denied
    # A spend the USER must hear about before it fires never rides in an
    # agent: model_bench op='run' bills the user's key for a whole matrix,
    # and the only place that sentence is spoken is the host's own `operate`
    # intent. An agent holding the tool would spend with no sentence shown.
    _spend_only_host = scope_set & HOST_STATED_SPEND_TOOLS
    if _spend_only_host:
        log.warning(
            "scoped_agent: refusing host-stated spend tools in a dispatch scope (eid=%s): %s "
            "— the host runs these itself through operate, with an intent the user sees",
            entity_id, sorted(_spend_only_host),
        )
        scope_set -= _spend_only_host
    _machine_only_host = scope_set & HOST_ONLY_MACHINE_TOOLS
    if _machine_only_host:
        log.warning(
            "scoped_agent: refusing host-only machine tools in a dispatch scope (eid=%s): %s "
            "— a root command runs only in the host's own turn",
            entity_id, sorted(_machine_only_host),
        )
        scope_set -= _machine_only_host
    # The host's own turn verbs (tool_lanes lane 'verb': close_turn) close the
    # HOST's turn — its thread topic, the user's preferences, the thread goal.
    # An agent has no turn to close, so it never holds one.
    from mastercode.runtime.tool_lanes import VERB_TOOLS as _VERB_TOOLS
    _verbs_only_host = scope_set & _VERB_TOOLS
    if _verbs_only_host:
        log.warning(
            "scoped_agent: refusing host turn verbs in a dispatch scope (eid=%s): %s",
            entity_id, sorted(_verbs_only_host),
        )
        scope_set -= _verbs_only_host
    # ── The turn type's cap ────
    # A tool outside the dispatching turn's type is stripped from the scope
    # the same way a code-exec tool is: the wall, not the host's good sense.
    # Its OWN set and its own sentence — it is not a code-exec tool, and a
    # host told "local_grep builds code, call build" would be sent the wrong
    # way.
    _outside_type: set[str] = set()
    _type_name = (turn_type or {}).get("name")
    if turn_type is not None:
        from mastercode.runtime.turn_types import category_map as _tt_category_map
        _tt_groups = set(turn_type.get("tool_groups") or [])
        _tt_cm = _tt_category_map()
        _outside_type = {n for n in scope_set if _tt_cm.get(n, "other") not in _tt_groups}
        if _outside_type:
            log.warning(
                "scoped_agent: refusing tools outside the dispatching turn's type %r "
                "(eid=%s): %s", _type_name, entity_id, sorted(_outside_type),
            )
            scope_set -= _outside_type
    # ── The capability GRANT ────────────────────────────────────────────
    # An agent's floor is ZERO bits. It holds nothing until its host explicitly
    # elevates it for this one task. Bits are deliberately NOT derived from
    # tool_scope: derivation would make the grant a rubber stamp ("whatever
    # tools I picked, the bits follow") when the entire point is that the host
    # reasons about what the task needs and whether elevation is warranted.
    _grant_bits, _unknown_caps = parse_grant(grant)
    if _unknown_caps:
        # Never silently round a typo down to a narrower grant — that would
        # look like a working dispatch that mysteriously denies later.
        _msg = grant_refusal(_unknown_caps)
        log.warning("scoped_agent: %s (eid=%s)", _msg, entity_id)
        return {"result": _msg, "error": _msg, "tool_actions": [],
                "denied_tools": [], "tokens": 0, "tokens_in": 0, "tokens_out": 0,
                "rounds": 0, "model": "", "lane": ""}

    # An agent from a typed turn holds at most the type's bits, and the
    # reachability check below runs on the intersected grant, so a tool the
    # type cannot reach is refused at dispatch, not on turn 9.
    if turn_type is not None:
        from mastercode.runtime.turn_types import grant_bits_for as _tt_grant_bits
        _grant_bits = int(_grant_bits) & int(_tt_grant_bits(turn_type) or 0)

    # Contradictory grant = refuse at DISPATCH, not on turn 9. A tool whose
    # required bits are not a subset of the grant denies on every call; finding
    # that out mid-run burns the budget and teaches the host nothing.
    _unreachable = unreachable_tools(scope_set, _grant_bits)
    if _unreachable:
        _detail = "; ".join(f"{k} ({v})" for k, v in sorted(_unreachable.items()))
        _msg = unreachable_grant_refusal(_unreachable, scope_set, _grant_bits,
                                         at_door=False)
        log.warning("scoped_agent: unreachable tools in dispatch (eid=%s): %s",
                    entity_id, _detail)
        # denied_tools carries EVERY name this dispatch could not honour —
        # the code-exec names stripped above included, so the record does
        # not silently lose `build` because a grant problem was found first
        # (`build` stripped + create_project unreachable,
        # record showed only create_project).
        return {"result": _msg, "error": _msg, "tool_actions": [],
                "denied_tools": sorted(set(_unreachable) | _denied | _outside_type),
                "blocked_reason": _blocked_reason(
                    code_exec=_denied, unreachable=_unreachable,
                    outside_type=_outside_type, type_name=_type_name),
                "tokens": 0, "tokens_in": 0,
                "tokens_out": 0, "rounds": 0, "model": "", "lane": ""}

    tools = [
        s for s in all_schemas
        if isinstance(s, dict)
        and s.get("function", {}).get("name") in scope_set
    ]
    # local_grep's platform pointer names code_grep/code_read/code_tree; an
    # agent scoped to local_grep without them must not be sent to tools it
    # does not hold.
    from mastercode.runtime.tools import drop_unheld_platform_pointer as _dupp
    tools = _dupp(tools)
    _granted = [s["function"]["name"] for s in tools]
    log.info(
        "scoped_agent: eid=%s requested=%s granted=%s grant_caps=%s ctx=%s",
        entity_id, sorted(scope_set), _granted, describe(_grant_bits),
        interaction_context,
    )
    _not_offered = sorted(scope_set - set(_granted))
    if _not_offered and not _schemas_failed:
        log.warning("scoped_agent: scope names with no tool behind them (eid=%s): %s",
                    entity_id, _not_offered)
    if tool_scope and not _granted:
        # Asked for tools and holds none. Run, such an agent can only say it
        # cannot do the task, and that sentence was delivered to the user as
        # the result. The door refuses unknown names
        # for the host's own dispatches; this covers every other caller.
        # Each reason is named for what it is, so the host fixes the right thing.
        if _schemas_failed and scope_set:
            # Nothing is wrong with the names; this Host's tool list did not
            # load. (A scope that was only code-exec or spend tools is emptied
            # before the list matters, so it takes the named branch below.) An infrastructure miss, never counted against the model.
            _msg = ("This agent was not started: this Host's tool list could "
                    "not be loaded, so it would have run with no tools. Try "
                    "the dispatch again in a moment.")
            log.warning("scoped_agent: %s (eid=%s)", _msg, entity_id)
            return {"result": _msg, "error": _msg, "lane_unavailable": True,
                    "tool_actions": [], "denied_tools": [],
                    "tokens": 0, "tokens_in": 0, "tokens_out": 0, "rounds": 0,
                    "model": "", "lane": ""}
        _why: list[str] = []
        if _not_offered:
            _why.append(f"{', '.join(_not_offered)}: not a tool this Host has — "
                        "write each name exactly as find_tools returns it "
                        "(search by what the tool does)")
        if _denied:
            _why.append(f"{', '.join(sorted(_denied))}: builds or runs code, which "
                        "only your own build tools do")
        if _outside_type:
            _why.append(f"{', '.join(sorted(_outside_type))}: outside this turn's "
                        f"type {_type_name!r} — an agent cannot hold more than the "
                        "turn that dispatched it; say in the thread what you could "
                        "not do and which types hold it")
        if _spend_only_host:
            _why.append(f"{', '.join(sorted(_spend_only_host))}: spends on the "
                        "user's key, so you run it yourself through operate")
        _msg = ("This agent was not started: it was given no tool it can use ("
                + "; ".join(_why) + ").")
        log.warning("scoped_agent: %s (eid=%s)", _msg, entity_id)
        return {"result": _msg, "error": _msg, "tool_actions": [],
                "denied_tools": sorted(set(_not_offered) | _denied | _spend_only_host
                                       | _outside_type),
                "blocked_reason": _blocked_reason(code_exec=_denied, unknown=_not_offered,
                                                  host_spend=_spend_only_host,
                                                  outside_type=_outside_type,
                                                  type_name=_type_name),
                "tokens": 0, "tokens_in": 0, "tokens_out": 0, "rounds": 0,
                "model": "", "lane": ""}

    # ── 2. Messages: the immutable governance wall + the HOST-WRITTEN task
    # system prompt (system_preamble). The host authors system_preamble per task
    # (dispatch tool `system_prompt`); the task itself is the user turn.
    _system = _SCOPED_AGENT_ROLE
    if system_preamble:
        _system = f"{_system}\n\n{system_preamble}"
    messages: list[dict] = [
        {"role": "system", "content": _system},
        {"role": "user", "content": task_prompt},
    ]

    # ── 3. Route: the host's own chat route decides {provider, model}. Agents
    # have no seat of their own; they inherit the chat route, because a dispatched agent IS the host doing
    # a piece of work with a narrower tool grant.
    # An explicit (provider, model) argument still wins, normalized through the
    # ONE normalizer (the lane inference helpers are gone with the
    # 3-lane router; Claude-shaped picks coerce to provider 'anthropic' —
    # Claude never transits OpenRouter, and there is no subscription-backed host engine).
    route_base_url = None
    route_fb_provider = None
    route_fb_model = None
    if provider and model:
        eff_provider, eff_model = normalize_provider(provider, model)
        if not eff_provider:
            _msg = (f"dispatch named an unroutable provider {provider!r} — the "
                    "host runs on the routed provider only; pick a Keys "
                    "provider (or 'local') and model in the Orchestrator.")
            log.warning("scoped_agent: %s (eid=%s)", _msg, entity_id)
            return {"result": _msg, "error": _msg, "lane_unavailable": True,
                    "tool_actions": [], "denied_tools": [],
                    "tokens": 0, "tokens_in": 0, "tokens_out": 0, "rounds": 0,
                    "model": "", "lane": ""}
        if eff_provider == "local":
            # Live endpoint only — never a stored/typed one (pinned mesh
            # addresses go stale on re-enrollment). Miss → the gateway
            # surfaces "nothing on your network serves this" instead of dialing.
            # A typed turn runs it only on a server its type lists.
            try:
                _hit, _off_type = await _local_model_for(pool, entity_id, model, turn_type)
            except Exception:
                _hit, _off_type = None, []
            if _hit is None and _off_type:
                return _off_type_server_refusal(entity_id, model, _off_type, _type_name)
            route_base_url = _hit["endpoint"] if _hit else None
    elif model and await _is_chat_route_model(pool, entity_id, model):
        # A bare model id that IS the host's own chat model runs where the host
        # runs it. A host dispatched with model='k3' —
        # its own Kimi model — and this fell to the OpenRouter default below,
        # which answered "k3 is not a valid model ID"; every such dispatch on a
        # non-OpenRouter route failed before its first call.
        _r = await resolve_role_route(pool, entity_id, "chat")
        eff_provider = _r["provider"]
        eff_model = _r["model"]
        route_base_url = _r.get("base_url")
        route_fb_provider = _r.get("fallback_provider")
        route_fb_model = _r.get("fallback_model")
    elif model:
        # Per-task model pick — the dispatch MODEL LEVER: route the right
        # model to the right task. A bare model id rides
        # OpenRouter by default (legacy picker compat) — normalize_provider
        # coerces a Claude-shaped pick to provider 'anthropic' — and a model a
        # machine on the user's own network serves right now runs LOCAL
        # (local-first, the same rule as the routing write surface).
        eff_provider, eff_model = normalize_provider("openrouter", model)
        if eff_provider == "openrouter":
            # Under a typed turn, a model the user's machines serve only on
            # servers outside the type is refused, not sent to a cloud model
            # in its place: that would hand the prompt to a provider when it
            # was going to stay on the user's own hardware.
            try:
                hit, _off_type = await _local_model_for(pool, entity_id, model, turn_type)
            except Exception:
                hit, _off_type = None, []
            if hit is None and _off_type:
                return _off_type_server_refusal(entity_id, model, _off_type, _type_name)
            if hit:
                eff_provider, route_base_url = "local", hit["endpoint"]
            else:
                # The door matched this id against OpenRouter's list without
                # regard to case; send it the way OpenRouter spells it.
                eff_model = catalog_spelling(eff_model, _model_catalog())
    else:
        # CHAT. The `agents` role is gone: a dispatched agent
        # runs on the host's own route, because it IS the host doing a piece
        # of work with a narrower tool grant — there is no separate seat. The
        # old role drew 15 calls in 14 days on one Host, all of them clerk
        # work that the deterministic operate lane now handles, and its
        # unrouted state was the default on a fresh entity
        # — so the common case was a dispatch that could not start at all
        # while the host itself was routed and working. A dispatch that names
        # its own model still wins; that branch is above.
        _r = await resolve_role_route(pool, entity_id, "chat")
        if _r.get("unconfigured"):
            # No servable route. Surface the resolver's actionable reason
            # verbatim (infra miss, not a model failure, so the verdict must
            # not score against a model in model_stats). Without this the
            # empty provider/model fell through to the loop and died on a
            # generic gateway error, throwing away the one message that tells
            # the user what to actually do.
            _msg = str(_r["unconfigured"])
            log.warning("scoped_agent: host (chat) route unconfigured (eid=%s): %s",
                        entity_id, _msg)
            return {"result": _msg, "error": _msg, "lane_unavailable": True,
                    "tool_actions": [], "denied_tools": [],
                    "tokens": 0, "tokens_in": 0, "tokens_out": 0, "rounds": 0,
                    "model": "", "lane": ""}
        eff_provider = _r["provider"]
        eff_model = _r["model"]
        route_base_url = _r.get("base_url")
        route_fb_provider = _r.get("fallback_provider")
        route_fb_model = _r.get("fallback_model")

    # ── A standing ban (preference op='ban', "refuses a model or harness at
    # dispatch"). The Host's door refuses a banned model before any task
    # exists (session dispatch branch, agent_work.choose_agent_model); this
    # is the wall for every caller of run_scoped_agent. Only a model the
    # caller named, here or by a preference: the Host's own route is the
    # person's choice in the Orchestrator. lane_unavailable: a ban says
    # nothing about how the model works.
    if (model or "").strip():
        from mastercode.agent_work import ban_refusal_for as _ban_refusal_for
        _ban_msg = _ban_refusal_for(model, eff_model)
        if _ban_msg:
            log.warning("scoped_agent: %s (eid=%s)", _ban_msg, entity_id)
            return {"result": _ban_msg, "error": _ban_msg, "lane_unavailable": True,
                    "tool_actions": [], "denied_tools": [],
                    "tokens": 0, "tokens_in": 0, "tokens_out": 0, "rounds": 0,
                    "model": eff_model, "lane": ""}
        # The failover too: a model named here that is the Host's own chat
        # model runs on the Host's route, fallback included, and call_model
        # moves an errored round to that fallback. A fallback a ban covers is
        # dropped, so the round fails rather than running on a banned model.
        if route_fb_model and _ban_refusal_for(route_fb_model):
            log.warning("scoped_agent: fallback %s dropped, a standing ban covers it (eid=%s)",
                        route_fb_model, entity_id)
            route_fb_provider = route_fb_model = None

    # ── The VALIDATED gate (runtime/model_validation) ──────────
    # An agent is nothing but tool use, so a model that has not passed the
    # admission bench does not start one. An infra-shaped refusal
    # (lane_unavailable), not a model verdict, and it names the fix. The gate
    # reads the resolved pair, so it covers the host route and a
    # dispatch-named model alike.
    try:
        from mastercode.runtime import model_validation as _mv
        _mv_status = _mv.status_for(eff_provider, eff_model)
        # The failover pair answers when the primary errors, holding the same
        # tools — so it is gated too.
        if _mv_status.get("enforced") and _mv_status.get("validated") and route_fb_provider:
            _fb = _mv.status_for(route_fb_provider, route_fb_model or eff_model)
            if not _fb.get("validated"):
                _mv_status = {**_mv_status, "validated": False}
                eff_provider, eff_model = route_fb_provider, (route_fb_model or eff_model)
    except Exception as _mv_err:  # noqa: BLE001 — the gate's own failure refuses nothing
        log.debug("validated gate skipped (non-fatal): %s", _mv_err)
        _mv_status = {"enforced": False, "validated": True}
    if _mv_status.get("enforced") and not _mv_status.get("validated"):
        _msg = (f"this agent would run on {eff_provider} / {eff_model} (the host's own route, or "
                "the model this dispatch named), which is not a validated model for tool use — no "
                "agent starts on it. Pick a validated model for the host model in the Orchestrator "
                "or name one on the dispatch; the platform validates models with the admission bench.")
        log.warning("scoped_agent: %s (eid=%s)", _msg, entity_id)
        return {"result": _msg, "error": _msg, "lane_unavailable": True,
                "tool_actions": [], "denied_tools": [],
                "tokens": 0, "tokens_in": 0, "tokens_out": 0, "rounds": 0,
                "model": eff_model, "lane": ""}

    # Context budget for the agent's own loop: the same window
    # lookup, trim order and overflow retry the host turn uses. Before this
    # a dispatched agent accumulated raw tool results round after round with
    # nothing between it and the provider's 400 — one large result killed
    # the run with the error string as its output. Unknown window → the
    # trim is a no-op, exactly the old behavior.
    from mastercode import context_budget as _cb
    # OUTPUT: the model's OWN limit. Each call
    # passes max_tokens=None and the gateway sends the routed model's
    # published output limit (catalog), clamped to half the window — never a
    # fixed number. This was a constant 16,384 here: desktop
    # conversion agents wrote k3 calls that ended
    # at ~16.4k tokens, against a model that can write 943,718.
    #
    # The trim target keeps a REPLY RESERVE free — a realistic reply
    # (context_budget.reply_reserve), never the output limit.
    _agent_reserve = _cb.DEFAULT_OUTPUT_RESERVE
    _agent_window = None
    try:
        _agent_window = await _cb.model_context_window(eff_model, provider=eff_provider)
        if _agent_window:
            _agent_reserve = _cb.clamp_output_reserve(_agent_window, _agent_reserve)
    except Exception as _w_err:
        log.debug("scoped_agent: window lookup failed (unbudgeted): %s", _w_err)
    _agent_target = (_agent_window - _agent_reserve) if _agent_window else None
    # The tool-result wall and the readers' page sizes follow this agent's
    # model window for the whole run (tool_result_wall.TOOL_WALL_WINDOW).
    _cb.set_tool_wall_window(_agent_window)

    # ROUNDS: the host's runaway backstop (agent_round_budget). AgentDispatch
    # resolves it at the door and passes it; a direct caller with none gets it
    # read here.
    try:
        max_rounds = max(1, int(max_rounds)) if max_rounds else None
    except (TypeError, ValueError):
        max_rounds = None
    if max_rounds is None:
        max_rounds = (await agent_round_budget(pool, entity_id))["rounds"]

    tool_actions: list[dict] = []
    total_tokens = 0
    total_tokens_in = 0
    total_tokens_out = 0
    rounds = 0
    final_text = ""
    run_error = None            # set when the run ends on a model/lane error
    lane_unavailable = False    # infra miss (lane can't serve), not a model failure
    # Dedup guard — don't loop on identical (tool, args). fingerprint →
    # {count, status, result}. Second call: served from the first result,
    # except that a first call which ERRORED runs once more; third: refused.
    # The host lane (session.py) caches the second call whatever it returned
    # — this lane deliberately differs by that one rerun, because a
    # dispatched agent has no user in the loop to re-issue a call after a
    # transient failure. The cost is one extra
    # execution of a tool whose effect landed before it reported the error.
    _seen_calls: dict[str, dict] = {}
    # The statuses that earn that one rerun. Only "error" exists today; a
    # transient status added later is listed here, not matched by string.
    _RERUN_STATUSES = frozenset({"error"})
    _blocked_tools: set[str] = set()  # code-exec tools the agent tried to run and were blocked
    # Tools the agent CALLED but was never granted (the grant wall). Kept
    # separate from _blocked_tools: a code-exec attempt is an ORCHESTRATION
    # mistake (the host asked for a tool a dispatched agent may never hold), whereas an
    # out-of-grant call is the AGENT reaching past what it was given — the
    # signal the governance bench is built to measure.
    _out_of_grant: set[str] = set()
    # Granted tools whose CALL the capability gate refused (tools.py, invariant 2:
    # the op's required bits are not a subset of the effective bits). The door
    # lets a read/write tool through on its read-safe minimum, so a write op
    # under a read-only grant is stopped HERE and nowhere earlier. As an
    # ordinary tool result that refusal let the run score 'pass' (nothing
    # written, verdict pass).
    _gate_denied: dict[str, str] = {}
    # The bits those denials lacked, for the readers only: an in-platform
    # tool's denial is answered with "do it yourself", never a wider grant.
    _gate_missing = {"bits": 0}

    async def _heartbeat(tool_name: str) -> None:
        """Stamp last-activity on the agent's record (W5 liveness). session_id is
        'agent:<task_id>' for dispatched agents; a direct/sync run without that
        shape is a no-op. Best-effort — a heartbeat write must never fail a run."""
        sid = session_id or ""
        if not sid.startswith("agent:"):
            return
        try:
            _tid = int(sid.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        try:
            await pool.execute(
                "UPDATE entity_tasks SET result = COALESCE(result,'{}'::jsonb) || "
                "jsonb_build_object('last_heartbeat_at', to_jsonb(NOW()::text), "
                "'hb_tool', $1::text) WHERE task_id=$2 AND status IN ('queued','in_progress')",
                tool_name, _tid,
            )
        except Exception as e:
            log.debug("scoped_agent: heartbeat write skipped (eid=%s): %s", entity_id, e)

    async def _deny_frame(tc_id: str, name: str, args: dict, reason: str) -> None:
        """Surface a blocked attempt in the Agents view as a denied tool card.

        A silent wall and an absent wall look identical from the operator's
        chair — the exact defect `make_host_scope_gate` fixed for the host role
        (in the since-deleted Claude-subscription host engine, where no tool
        calls showed in the Circles UI).
        Dispatched agents had the same hole:
        the tool_call_start frame is only emitted AFTER the walls below, so a
        blocked call produced no card at all. Best-effort by contract — a
        progress consumer must never be able to fail a run.
        """
        if progress_cb is None:
            return
        try:
            await progress_cb({"event": "tool_call_start", "tool_call_id": tc_id,
                               "tool": name, "args": args or {}})
            await progress_cb({"event": "tool_call_result", "tool_call_id": tc_id,
                               "tool": name, "status": "denied",
                               "result": {"status": "denied", "result": reason}})
        except Exception as cb_err:
            log.debug("scoped_agent deny frame progress_cb raised (non-fatal): %s", cb_err)

    async def _short_frame(tc_id: str, name: str, args: dict, status: str, result: dict) -> None:
        """A start+result pair for a call that never reached the executor but
        was not a wall either — a dedup repeat served from the first result,
        or a third repeat refused. Same reason as _deny_frame: a call the
        agent made and the operator cannot see is indistinguishable from a
        hung agent. Best-effort by contract."""
        if progress_cb is None:
            return
        try:
            await progress_cb({"event": "tool_call_start", "tool_call_id": tc_id,
                               "tool": name, "args": args or {}})
            await progress_cb({"event": "tool_call_result", "tool_call_id": tc_id,
                               "tool": name, "status": status, "result": result})
        except Exception as cb_err:
            log.debug("scoped_agent short frame progress_cb raised (non-fatal): %s", cb_err)

    # The agent's task id (session_id 'agent:<task_id>', AgentDispatch), on
    # every model-call row this run writes. None for a run with no task.
    _agent_task_id = agent_task_id(session_id)
    _agent_log_meta = {"task_id": _agent_task_id} if _agent_task_id is not None else None

    # WHO ANSWERED, per round — the served model, not the routed one. The
    # route is fixed for the run, but call_model's failover can move a round
    # to the fallback provider/model and annotates the result; every tool call
    # that round produced is scored against whoever actually emitted it
    # (tool_call_log.model). Same contract as session.py `_served`.
    _agent_served = {"provider": eff_provider, "model": eff_model}

    # Pieces of an answer the output limit cut, in order (the loop continues
    # the answer rather than taking the first piece as the report).
    _cut_parts: list[str] = []

    def _ledger_not_run(func_name: str, why: str) -> None:
        """The tool_call_log row for a call the loop did not run. A call the
        OUTPUT LIMIT cut is the platform's limit, never the model's fault:
        reason BLOCKED, status 'truncated' (the reason vocabulary is pinned by
        a CHECK in migrations/v157_tool_call_log.sql). Arguments that are not
        JSON on their own are the model's miss: BAD_ARGS_JSON."""
        try:
            from mastercode.runtime.tool_call_ledger import (
                BAD_ARGS_JSON as _tcl_bad, BLOCKED as _tcl_blocked,
                record_later as _tcl_record)
            _tcl_record(
                pool, entity_id=entity_id, lane="agent", role="chat",
                provider=_agent_served["provider"], model=_agent_served["model"],
                tool=func_name or "?",
                reason=_tcl_blocked if why == "truncated" else _tcl_bad,
                status="truncated" if why == "truncated" else "error",
                session_id=session_id, latency_ms=0, args={}, schema_missing=[])
        except Exception as _le:  # noqa: BLE001
            log.debug("tool_call_ledger note (not run) failed (non-fatal): %s", _le)

    async def _execute_one(func_name: str, func_args: dict, tc_id: str | None = None,
                           args_parsed: bool = True) -> dict:
        """Execute ONE granted tool call: the code-exec wall, the GRANT wall,
        the dedup guard, the meta/bot execution branch, progress frames, and the
        tool_actions record. (Was shared with the deleted Claude-subscription
        agent loop so both enforced the SAME walls; the generic loop below is
        now the only caller.)

        `args_parsed`: kept for the ledger's schema check. The loop no longer
        sends a call whose arguments did not parse (or that the output limit
        cut) here at all — it answers those itself, and nothing runs with {}."""
        tc_id = tc_id or f"call_{int(time.time() * 1000)}"
        _t_tool0 = time.time()

        async def _ledger(res: dict, *, reason: str | None = None) -> None:
            """One tool_call_log row (runtime/tool_call_ledger), lane 'agent',
            role 'chat'. Every exit of this function passes through here —
            the walls above return early, so each one notes before it
            returns. The INSERT is scheduled as its own task (record_later),
            so the agent's next call never waits on the pool for it."""
            try:
                from mastercode.runtime.tool_call_ledger import (
                    classify as _tcl_classify, record_later as _tcl_record,
                    schema_errors as _tcl_schema_errors,
                )
                _miss = _tcl_schema_errors(func_name, func_args) if args_parsed else []
                _tcl_record(
                    # role='chat' because that is the route the agent ran on
                    # (three-role cut) — the canary counts calls
                    # per (role, model), so stamping a role that no longer
                    # exists would hide this call from the chat window it
                    # belongs to. `lane` still says these came from an agent.
                    pool, entity_id=entity_id, lane="agent", role="chat",
                    provider=_agent_served["provider"], model=_agent_served["model"],
                    tool=func_name,
                    reason=reason or _tcl_classify(res, args_parsed=args_parsed,
                                                   schema_missing=_miss),
                    status=(str(res.get("status") or "ok") if isinstance(res, dict) else "ok"),
                    session_id=session_id,
                    latency_ms=int((time.time() - _t_tool0) * 1000),
                    args=func_args, schema_missing=_miss)
            except Exception as _le:  # noqa: BLE001
                log.debug("tool_call_ledger note failed (non-fatal): %s", _le)

        # Safety boundary (defense-in-depth): even if a code-exec tool was
        # never advertised, refuse it at the point of execution. A dispatched
        # agent never runs code — that is the harness pool, via the host's
        # own build tools.
        #
        # This check stays FIRST, ahead of the grant wall below: `_denied`
        # already stripped code-exec names out of scope_set, so every code-exec
        # tool is by definition out of scope — grant-first would swallow them
        # into the generic refusal and throw away the build-lane routing hint
        # that keeps the model from dead-ending.
        if _is_code_exec_tool(func_name):
            log.warning("scoped_agent: blocked code-exec tool call %s (eid=%s)", func_name, entity_id)
            tool_actions.append({"tool": func_name, "args": func_args, "result_status": "blocked"})
            _blocked_tools.add(func_name)
            _reason = (
                f"'{func_name}' executes/builds code — that runs on the harness pool, "
                "through the host's OWN build tools, not something an in-container "
                "agent can do; a dispatched agent can never receive it. Report back "
                "that this needs the host's own `build` tool — do not attempt it here."
            )
            await _deny_frame(tc_id, func_name, func_args, _reason)
            _res = {"status": "blocked", "reason": "blocked", "result": _reason}
            await _ledger(_res)
            return _res

        # ── The GRANT wall ──────────────────────────────
        # `tool_scope` used to be an ADVERTISEMENT filter only. It chose which
        # schemas the model was OFFERED (§1 above) and was never consulted
        # again: execution dispatched on membership in the GLOBAL
        # `_META_TOOL_NAMES`. So a name the agent was
        # never offered still ran — whether hallucinated, or supplied by
        # attacker-controlled content arriving in a tool result — gated only by
        # the ENTITY's own CEIGAS bits, which a dispatched agent inherits
        # wholesale (`app_permissions=None` below; `tools.py:577`).
        #
        # That made the host's grant advisory. It is a wall now, in the same
        # place and for the same reason as the code-exec boundary above: the
        # guard is the wall, not the host's good sense.
        #
        # Fail-closed on an EMPTY scope is deliberate and correct: an empty
        # tool_scope builds an empty schema list, so the agent was granted
        # nothing and every call is out of grant. Verified no caller relies on
        # empty-means-everything — `routes/agents.py:99` rejects an empty scope
        # outright, and the host's `dispatch` path (agent_dispatch.py) always
        # passes the host's explicit list.
        if func_name not in scope_set:
            log.warning(
                "AGENT_SCOPE_DENIED tool=%s eid=%s session=%s (agent holds only %s)",
                func_name, entity_id, session_id or "-", sorted(scope_set) or "<none>",
            )
            tool_actions.append({"tool": func_name, "args": func_args,
                                 "result_status": "denied_out_of_grant"})
            _out_of_grant.add(func_name)
            _reason = (
                f"'{func_name}' is not in your grant. Your host dispatched you with "
                f"exactly these tools: {', '.join(sorted(scope_set)) or '(none)'}. "
                "Use one of those, or finish and report back what you needed and why — "
                "widening the grant is your host's decision, not yours."
            )
            await _deny_frame(tc_id, func_name, func_args, _reason)
            # For the ledger: a granted agent reaching past its grant. Counted
            # apart from an invented name — the name may be perfectly real.
            _res = {"status": "denied", "reason": "out_of_grant", "result": _reason}
            await _ledger(_res)
            return _res

        # Dedup guard (rule stated at _seen_calls above): a SECOND identical
        # (tool, args) call hands back the first call's result, runs again
        # only when the first call errored, and a THIRD is refused. Refusing
        # the second call outright, whatever the first had returned, blocked
        # an agent whose image_generate died on a DNS miss from the one retry
        # its own brief allowed.
        try:
            _fp = f"{func_name}::{json.dumps(func_args, sort_keys=True, default=str)}"
        except (TypeError, ValueError):
            _fp = f"{func_name}::{func_args}"
        _prior = _seen_calls.get(_fp)
        if _prior is not None:
            if _prior["count"] >= 2:
                tool_actions.append({"tool": func_name, "args": func_args, "result_status": "error"})
                _res = _refused(f"Refusing to call `{func_name}` a third time with the same args "
                        "this run. If the result isn't what you needed, try different args "
                        "or a different tool, or finish and report what blocked you.", reason="dedup", dedup_block=True)
                await _short_frame(tc_id, func_name, func_args, "error", _res)
                await _ledger(_res)
                return _res
            _prior["count"] += 1
            # isinstance is the local belt: the placeholder written below
            # holds result=None until the call returns, and calls run one at
            # a time, so it is never observed here — but the spread would
            # raise if it were.
            if _prior["status"] not in _RERUN_STATUSES and isinstance(_prior["result"], dict):
                # The first call did not fail: repeating it changes nothing,
                # so the prior result comes back marked as such. (A denied
                # or blocked first call is cached too: the grant is fixed for
                # the run, so a retry cannot change that answer.)
                _res = {**_prior["result"], "cached": True,
                        "note": ("Cached from this run — you already called this exact tool "
                                 "with these exact args. Use the prior result.")}
                tool_actions.append({"tool": func_name, "args": func_args,
                                     "result_status": _prior["status"]})
                await _short_frame(tc_id, func_name, func_args, _prior["status"], _res)
                await _ledger(_res)
                return _res
            # The first call ERRORED: one retry runs for real.
        else:
            _seen_calls[_fp] = {"count": 1, "status": None, "result": None}

        # Heartbeat (W5): stamp last activity on the record so the tray shows a
        # running agent is progressing, not hung. Fires from BOTH lanes (this
        # executor is the shared tool-exec point). Best-effort — never blocks work.
        await _heartbeat(func_name)

        if progress_cb is not None:
            try:
                await progress_cb({
                    "event": "tool_call_start",
                    "tool_call_id": tc_id,
                    "tool": func_name,
                    "args": func_args or {},
                })
            except Exception as _cb_err:
                log.debug("scoped_agent progress_cb start raised (non-fatal): %s", _cb_err)

        # Execute — two-branch, exactly like session.py: meta/bridge tools run
        # inline via _execute_meta_tool; everything else is a bot. Wrapped so an
        # errored tool feeds back as a structured error and never raises out.
        try:
            if func_name in _META_TOOL_NAMES or func_name in _BRIDGE_TOOL_NAMES:
                tool_result = await _execute_meta_tool(
                    func_name, func_args,
                    entity_id=entity_id,
                    pool=pool,
                    prompt_console=None,
                    session_id=session_id,
                    app_permissions=None,
                    # The host-minted grant for THIS agent. effective bits
                    # become entity_bits & grant_bits inside the gate.
                    grant_bits=_grant_bits,
                    # The dispatching turn's type: the gate's key and server
                    # checks apply to the agent exactly as to the turn.
                    turn_type=turn_type,
                    # None → skip the SURFACE gate (a dispatched agent is not a chat
                    # surface). The CEIGAS capability gate in _execute_meta_tool
                    # still runs, so real authorization is unchanged.
                    interaction_context=None,
                )
            else:
                # There is no bot-lane branch. Its grant narrowing lives on: the grant rides the meta call
                # above (grant_bits=), which is the only exec path now.
                tool_result = _refused(f"Unknown tool: {func_name}", reason="unknown_name")
        except Exception as e:
            log.error("scoped_agent tool error (%s): %s", func_name, e)
            tool_result = _refused_if_caller(e, str(e))

        # JSON-safe before it hits the progress frame + model tool message.
        tool_result = _json_safe(tool_result)
        _status = tool_result.get("status", "ok") if isinstance(tool_result, dict) else "ok"
        # A bare (non-dict) result is wrapped so the cached repeat is still an
        # envelope — session.py stores its cache the same way.
        _seen_calls[_fp].update({"status": _status, "result": (
            tool_result if isinstance(tool_result, dict) else {"result": tool_result})})

        _needs = tool_result.get(CAP_DENIAL_KEY) if isinstance(tool_result, dict) else None
        if _status == "denied" and _needs is not None:
            # The CAPABILITY gate refused a granted tool (the envelope says
            # so by carrying the bits the op needed). Name the capability the
            # grant lacked. For a reader of untrusted content the retry line
            # adds it; an in-platform tool is the Host's own work, and the
            # record tells the Host to do that part itself. Any other `denied`
            # (path jail, propose-mode editor, trust floor) stays an ordinary
            # tool result the agent can work around — it says nothing about
            # the grant and must not fail the run.
            _missing = int(_needs) & ~int(_grant_bits)
            if is_agent_reader(func_name):
                _gate_missing["bits"] |= _missing
            _gate_denied[func_name] = (
                f"missing {describe(_missing)}" if _missing
                else str(tool_result.get("result") or "denied")[:200])
            log.warning("AGENT_CAP_DENIED tool=%s eid=%s session=%s (%s)",
                        func_name, entity_id, session_id or "-", _gate_denied[func_name])

        if progress_cb is not None:
            try:
                await progress_cb({
                    "event": "tool_call_result",
                    "tool_call_id": tc_id,
                    "tool": func_name,
                    "result": tool_result,
                    "status": _status,
                })
            except Exception as _cb_err:
                log.debug("scoped_agent progress_cb result raised (non-fatal): %s", _cb_err)

        tool_actions.append({
            "tool": func_name,
            "args": func_args,
            "result_status": _status,
        })
        await _ledger(tool_result)
        return tool_result

    # Big results stay in the database (runtime/result_store), the same store,
    # threshold and handle as the Host's turn: an agent is where untrusted,
    # large content lands (web pages, foreign repos). A result over
    # STORE_OVER_CHARS is written whole and the agent's messages carry its
    # first part and its handle; from the round after the first one is stored
    # the agent holds `read` (handle, offset, length), for its own handles only.
    _own_handles: set[str] = set()
    _store_token = session_id or f"agent-run:{int(time.time() * 1000)}"
    _read_offered = False

    async def _agent_result_read(tc_id: str, func_args: dict) -> dict:
        await _heartbeat("read")
        _h = str(func_args.get("handle") or "").strip()
        if _h not in _own_handles:
            res = {"status": "error", "reason": "not_found",
                   "result": (f"`{_h}` is not a result this run stored: read pages only the "
                              "handles your own tool results came back with.")}
        else:
            res = await _result_store.read_page(pool, entity_id, func_args,
                                                turn_token=_store_token)
        tool_actions.append({"tool": "read", "args": func_args,
                             "result_status": str(res.get("status") or "ok")})
        await _short_frame(tc_id, "read", func_args, str(res.get("status") or "ok"), res)
        return res

    # ── 4. The generic tool-calling loop — EVERY provider runs here now
    # (anthropic included: call_model's Messages-API dispatch returns the same
    # tool_calls/raw_message shape). The Claude-subscription agent loop is
    # deleted — no route can resolve to it. call_model → no
    # tool_calls means we're done (final text); otherwise execute each tool
    # via _execute_one, feed results back, and go again.
    while rounds < max_rounds:
        rounds += 1
        # Round-start heartbeat: tool calls stamp liveness, but a long model
        # call between rounds is silent — stamping here bounds the no-heartbeat
        # gap to ONE inference call, so the monitor's liveness scan can catch
        # true wedges without wall-clock-killing agents that are just thinking.
        await _heartbeat("model_call")
        if _agent_target is not None:
            try:
                _pre = _cb.trim_messages_to_window(messages, _agent_target)
                if _pre is not messages:
                    log.info("scoped_agent: pre-send trim %d→%d messages (eid=%s)",
                             len(messages), len(_pre), entity_id)
                    messages[:] = _pre
            except Exception as _tr_err:
                log.debug("scoped_agent: pre-send trim failed (non-fatal): %s", _tr_err)
        _call_kw = dict(
            temperature=0.3,
            # None = the routed model's own output limit (see _agent_reserve).
            max_tokens=None,
            session_id=session_id,
            tools=tools or None,
            provider=eff_provider,
            provider_model=eff_model,
            base_url=route_base_url,   # local/BYO route endpoint (None otherwise)
            fallback_provider=route_fb_provider,
            fallback_model=route_fb_model,
            # The cost log's source_module: a dispatched agent's calls are
            # agent work in background activity, never 'unknown' (rows once
            # printed "Background · unknown").
            _source=AGENT_SOURCE_MODULE,
            # ...and each row names WHICH agent: its task id in the row's
            # metadata (provider_gateway._CALL_LOG_META).
            _log_meta=_agent_log_meta,
        )
        result = await call_model(messages=messages, **_call_kw)
        # Overflow retry-once, same shape as the host turn: the estimate is
        # chars//4, so trim tighter than the estimate said and go again.
        try:
            if _agent_target is not None and _cb.is_context_overflow(result):
                _trimmed = _cb.trim_messages_to_window(messages, int(_agent_target * 0.75))
                if _trimmed is not messages and len(_trimmed) <= len(messages):
                    log.warning("scoped_agent: provider context-overflow — trimmed %d→%d, retrying once (eid=%s)",
                                len(messages), len(_trimmed), entity_id)
                    messages[:] = _trimmed
                    result = await call_model(messages=messages, **_call_kw)
        except Exception as _ov_err:
            log.debug("scoped_agent: overflow retry failed (original result stands): %s", _ov_err)
        total_tokens += int(result.get("tokens_used") or 0)
        total_tokens_in += int(result.get("prompt_tokens") or 0)
        total_tokens_out += int(result.get("completion_tokens") or 0)
        # The trim keeps a realistic reply free (context_budget.reply_reserve),
        # never the output limit: the limit governs each request, clamped by
        # the gateway to the room that call's input leaves.
        # Note who answered this round (see _agent_served above).
        _m = result.get("model") if isinstance(result, dict) else None
        if isinstance(_m, str) and _m:
            _agent_served["model"] = _m
        _fo = result.get("failed_over") if isinstance(result, dict) else None
        if isinstance(_fo, dict):
            if _fo.get("to_provider"):
                _agent_served["provider"] = _fo["to_provider"]
            if _fo.get("to_model"):
                _agent_served["model"] = _fo["to_model"]

        if result.get("error") and not result.get("content"):
            final_text = result.get("error") or ""
            # Carry the error on the RETURN too — without it the run scores as a
            # PASS in _finalize_record (non-empty result, no error key), and the
            # host would learn "this model works" from a failed run.
            run_error = final_text
            if _cut_parts:
                # An answer the output limit cut, then an error on its
                # continuation: what was written is the result, the error says
                # why it is incomplete.
                final_text = "".join(_cut_parts).strip()
            # Infra-vs-model is STRUCTURAL now: call_model flags
            # a transport/lane miss with lane_unavailable at the gateway (bridge
            # unreachable, endpoint refused, unknown/unconfigured lane) — no more
            # error-string matching. An infra miss → verdict 'unavailable' (excluded
            # from model_stats); a model that answered with an error is a real fail.
            lane_unavailable = bool(result.get("lane_unavailable") or result.get("local_failed"))
            # Surface the round's prose/reasoning to the live feed.
            await _emit(progress_cb, "agent_text",
                        text=str(result.get("content") or "")[:4000])
            if result.get("reasoning"):
                await _emit(progress_cb, "agent_reasoning",
                            text=str(result["reasoning"])[:4000])
            log.warning("scoped_agent: model error (eid=%s, lane_unavailable=%s): %s",
                        entity_id, lane_unavailable, final_text)
            break

        tool_calls = result.get("tool_calls")
        _truncated = bool(result.get("truncated"))
        _limit = result.get("output_limit")
        if not tool_calls:
            _text = (result.get("content") or "").strip()
            if _truncated:
                # The model's output limit stopped the answer mid-way. It is
                # NOT the final report: what was
                # written is kept, and the model is told where it stopped
                # and carries on in the next round.
                _part = result.get("content") or ""
                if _part:
                    _cut_parts.append(_part)
                    messages.append({"role": "assistant", "content": _part})
                messages.append({"role": "user", "content": continue_text(_limit, bool(_part))})
                await _emit(progress_cb, "agent_text", text=_part[-4000:])
                log.info("scoped_agent: answer cut off at the output limit (%s) — "
                         "continuing (eid=%s, round %d)", _limit, entity_id, rounds)
                continue
            final_text = ("".join(_cut_parts) + (result.get("content") or "")).strip() \
                if _cut_parts else _text
            break

        # Calls the output limit cut off mid-arguments: the gateway names them
        # (truncated_tool_call_ids). A cut result that names none — a gateway
        # that could not tell — leaves the LAST call as the cut one: a model
        # writes its calls in order and the limit stops the one in progress.
        _cut_ids = result.get("truncated_tool_call_ids")
        if _truncated and not isinstance(_cut_ids, (list, tuple, set)):
            _cut_ids = [tool_calls[-1].get("id")] if tool_calls else []
        _cut_ids = {str(x) for x in (_cut_ids or []) if x}

        # Each call: run, or say exactly why not. Neither a cut call nor one
        # whose arguments are not JSON runs with {} — that is what turned a
        # truncated write into "operate requires `tool`" (session.py had the
        # same path).
        _plan: list[tuple] = []   # (tc, tc_id, func_name, func_args | None, not_run_text | None, status)
        for tc in tool_calls:
            func_name = tc.get("function", {}).get("name", "")
            tc_id = tc.get("id") or f"call_{int(time.time() * 1000)}"
            _raw = (tc.get("function") or {}).get("arguments")
            _raw_s = _raw if isinstance(_raw, str) else ("" if _raw is None else json.dumps(_raw))
            _err = None
            try:
                # An empty arguments string is a call with no arguments (the
                # streamed shape of a no-argument call), not a parse failure.
                func_args = json.loads(_raw_s) if _raw_s.strip() else {}
            except (json.JSONDecodeError, TypeError, ValueError) as _je:
                func_args = None
                _err = str(_je)
            if str(tc.get("id") or "") in _cut_ids or (_truncated and _err):
                _plan.append((tc, tc_id, func_name, None,
                              cut_call_text(func_name, _limit, len(_raw_s)), "truncated"))
            elif _err:
                _plan.append((tc, tc_id, func_name, None,
                              bad_args_text(func_name, _err, _raw_s), "bad_args"))
            else:
                _plan.append((tc, tc_id, func_name, func_args, None, None))

        # Append the assistant's tool-call message (native provider format —
        # this loop only runs the provider path, never the local-XML path).
        # A call that is not run goes into the history with `{}` as its
        # arguments: its broken text would otherwise be re-sent every round
        # (the Anthropic wire wraps it as {"_raw": ...} — up to a whole output
        # limit of it) and an OpenAI-wire provider may refuse invalid JSON.
        raw_msg = result.get("raw_message", {}) or {}
        _hist_calls = []
        for (tc, _tid, _fn, _fa, _nr, _st) in _plan:
            if _nr is None:
                _hist_calls.append(tc)
            else:
                _hist_calls.append({**tc, "function": {**(tc.get("function") or {}),
                                                       "arguments": "{}"}})
        messages.append({
            "role": "assistant",
            "content": raw_msg.get("content") or None,
            "tool_calls": _hist_calls,
        })

        for (tc, tc_id, func_name, func_args, _not_run, _st) in _plan:
            _is_page = False
            if _not_run is not None:
                tool_result = {"status": "error", "reason": _st, "result": _not_run}
                if _st == "truncated":
                    tool_result["truncated"] = True
                    if isinstance(_limit, int):
                        tool_result["output_limit"] = _limit
                tool_actions.append({"tool": func_name, "args": {},
                                     "result_status": _st})
                await _short_frame(tc_id, func_name, {}, "error", tool_result)
                _ledger_not_run(func_name, _st)
                log.info("scoped_agent: %s call not run (%s) eid=%s", func_name, _st, entity_id)
            elif _result_store.is_result_read(func_name, func_args):
                # A page of a result this run stored: never stored again.
                _is_page = True
                tool_result = await _agent_result_read(tc_id, func_args)
            else:
                tool_result = await _execute_one(func_name, func_args, tc_id,
                                                 args_parsed=True)
            _content = _bound_tool_result(tool_result, **_cb.wall_kwargs(_agent_window))
            if (_not_run is None and not _is_page
                    and len(_content) > _result_store.STORE_OVER_CHARS):
                _whole = _result_store.serialize(tool_result)
                _handle = await _result_store.put(
                    pool, entity_id=entity_id, session_id=session_id,
                    turn_token=_store_token, tool_call_id=str(tc_id),
                    tool=func_name, text=_whole)
                if _handle:
                    # A failed write keeps the result inline, as before.
                    _own_handles.add(_handle)
                    _content = _result_store.preview(func_name, _handle, _whole)
                    if not _read_offered:
                        tools.append(_result_store.AGENT_READ_SCHEMA)
                        _read_offered = True
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": _content,
            })
    else:
        # Loop exhausted the round backstop without a final text turn. Say so
        # — an empty result with no error read downstream as a silent shrug
        # ("final output not very helpful"); the failure
        # delivery needs the real cause.
        log.info("scoped_agent: hit the round backstop of %d (eid=%s)",
                 max_rounds, entity_id)
        if not final_text and _cut_parts:
            final_text = "".join(_cut_parts).strip()
        if not run_error and (not final_text or _cut_parts):
            run_error = rounds_exhausted_text(max_rounds)

    # The run is over: nothing holds its handles any more (the Host never
    # reads an agent's rows, result_store.read_page), so the untrusted content
    # it stored goes now. A run that raises is taken by the sweep once its
    # task is no longer running.
    if _own_handles:
        await _result_store.forget(pool, turn_token=_store_token)

    return {
        "result": final_text,
        "error": run_error,
        "lane_unavailable": lane_unavailable,
        "tool_actions": tool_actions,
        # Code-exec tools stripped from the scope or blocked at exec, and
        # names called outside the grant → verdict 'blocked', never 'pass'.
        # blocked_reason says WHICH of those it was — the tray used to print
        # one fixed sentence ("code work runs on the host's build lane") for
        # every blocked run, which was simply wrong for a grant refusal.
        "denied_tools": sorted(_denied | _blocked_tools | _out_of_grant
                               | set(_gate_denied) | _outside_type),
        "blocked_reason": _blocked_reason(
            code_exec=_denied | _blocked_tools, out_of_grant=_out_of_grant,
            gate_denied=_gate_denied,
            outside_type=_outside_type, type_name=_type_name,
            retry=_retry_for_denied(_gate_missing["bits"], _grant_bits)),
        "tokens": total_tokens,
        "tokens_in": total_tokens_in,
        "tokens_out": total_tokens_out,
        "rounds": rounds,
        "model": eff_model,
        "lane": eff_provider,   # back-compat key — carries the PROVIDER id
    }
