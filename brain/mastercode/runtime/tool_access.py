"""
runtime/tool_access.py — the READ/ACT split classifier (Feature 1).

The host READS directly with `read` and ACTS with `operate` (one listed op)
or its own doors; an agent is only for untrusted work outside the platform
(tool_lanes.UNTRUSTED_WORK_TOOLS). `is_read_safe_tool`
decides only the first question: is this a read.

CONSERVATIVE by design: it returns True ONLY when a tool is clearly read-only.
Anything uncertain falls through to "not read-safe", and tool_lanes.lane_for
places it. A tool is read-safe iff it is NOT a write/code-exec
tool, exposes NO mutating op, AND is positively identified as a read (curated
allowlist, a read-ish name shape, or an action enum whose ops are all read-ish).
"""

import copy
import re

# ── The ONE gate: may this host hold the orchestration tool surface? ─────────
# The rule is that a tool is callable iff it appears in the tool list sent
# to the model for that surface — one gate, decided in one place. Before this
# function there were two, and they disagreed:
#
#   * session.py built `tools=` iff behavioral_params.response_style ==
#     "conversational" AND the entity was above the enrollment tier, while
#   * prompt_schema rendered the "Your Tools" inventory unconditionally
#     (gate=always) off the CEIGAS authorization walk, which never reads
#     response_style.
#
# So any entity whose response_style was absent got a 20k-character prompt
# describing tools alongside an EMPTY tools array, and narrated tool calls it
# could not make. Measured on two Hosts
# whose boot identity seed had been failing: 6 prompt blocks
# rendered, tool_inventory 20,834 chars, tools=None, zero tool calls, turn over.
#
# response_style is a PROSE-STYLE field, not a capability. Its live values are
# conversational / concise / structured / NULL — none of which means "this
# entity may not hold tools", and two real hosts were tool-less purely for
# having a style word other than "conversational". The capability question is
# the enrollment tier, and `bot_capabilities` already answers it.
#
# DEFAULT-DENY on bot_capabilities is deliberate and stays: an absent
# permission_overrides is a capability statement (nothing granted yet), unlike
# an absent response_style, which is merely an unseeded prose preference.
ENROLLMENT_CAPS_CEILING = 3


def host_orchestration_enabled(entity: dict | None) -> bool:
    """True iff this entity may be handed the host orchestration tool surface.

    The single source of truth for that question. Call it from BOTH the site
    that builds `tools=` and the site that advertises tools in the prompt, so
    what is advertised and what is callable cannot drift apart.
    """
    caps = (entity or {}).get("permission_overrides") or {}
    if isinstance(caps, str):
        try:
            import json as _json
            caps = _json.loads(caps) or {}
        except (ValueError, TypeError):
            caps = {}
    raw = caps.get("bot_capabilities", 0)
    try:
        tier = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        tier = 0
    return tier > ENROLLMENT_CAPS_CEILING


# ── Curated read-only tools (explicit allowlist — always read-safe) ──────────
# The seed set plus the obvious information-gathering tools in the
# entity catalog. A name here that an entity isn't authorized for simply never
# matches (filtering happens against real, authorized schemas), so listing an
# absent name is harmless.
_READ_SAFE_TOOLS = frozenset({
    "get_network", "code_read", "calendar_read", "inbox_read",
    # `github` is absent on purpose: it is a MIXED tool (op='import' writes),
    # so it must never be whole-tool read-safe — its five read ops ride
    # read_safe_ops instead. github_search left this list along with the
    # name.
    "local_grep", "web_search", "research", "code_search",
    # coding_bench is a pure read (its executor only SELECTs harness_run +
    # reads routing/config; docstring: "Read-only.") but the classifier can't
    # see that — no op enum, no read-prefix name — so it default-denied and
    # the host burned turns on HOST_SDK_READ_DENIED for the exact tool the workbench briefing tells it to read.
    "coding_bench",
    # Self / environment reads — same miss as coding_bench: each is explicitly
    # "Read-only" in its own schema but the classifier can't see it (no op enum,
    # a name with no read-prefix), so it default-denied. Without these the host
    # can't even read its OWN personality or list its OWN apps directly: read()
    # denied them on the deleted host_sdk_engine (HOST_SDK_READ_DENIED) and the open lane never
    # bound them, forcing an absurd agent dispatch just to self-inspect.
    "personality_state", "explore_environment", "describe_app",
    # chat_threads is three SELECTs over entity_memory (threads / episodes /
    # turns) — read-only by construction, but its op enum contains "episodes",
    # which no read-verb heuristic recognizes.
    "chat_threads",
    # Same miss again, found by a catalog audit: each of these is
    # documented read-only, is a single SELECT / catalog fetch, and has neither
    # an op enum nor a read-prefixed name for the heuristics to latch onto — so
    # the host had to dispatch a governed agent to perform a pure read, which
    # host-full-read-rights says must never happen.
    #   harness_costs      — SELECT over priced runs (tools_projects.py:_exec_harness_costs)
    #   harness_transcript — SELECT one completed run's transcript
    #   harness_usage_windows — SELECT over harness_usage_event (v114): token
    #                      consumption per subscription refresh window. Same
    #                      shape as harness_costs; nothing dispatched, nothing
    #                      executed.
    #   inference_catalog  — inference.get_model_catalog(), no writes
    #   review_session     — review.get_session_service(), SELECT-only
    #   training_memory    — behavioral_corrections.load_corrections(), SELECT-only
    "harness_costs", "harness_transcript", "harness_usage_windows",
    "inference_catalog",
    "review_session", "training_memory",
    # Pure reads the heuristics could not see (a host-tooling audit of the
    # 116-tool catalog), so the host had to DISPATCH AN AGENT to
    # read them — the exact rule violation above. Each verified read-only at
    # its executor:
    #   code_grep / code_tree — bridge ops code.grep / code.tree (ripgrep +
    #     a directory walk over an allowlisted repo); sibling code_read was
    #     already listed.
    #   search_my_code — code_chunks (pgvector + tsvector) SELECTs over the entity's own
    #     index (the reranker is a model call that writes nothing).
    #   state_load — one SELECT on entity_context.
    #   capabilities — read_text() on the generated capabilities doc; only
    #     op='search' slipped through before, the other three ops dispatched.
    #   ladder.status — reads a ladder run's state.json (a bounded
    #     read-repair write only for a run stranded by a restart); the
    #     dotted name defeats the `_status` suffix rule.
    "code_grep", "code_tree", "search_my_code", "state_load", "capabilities",
    "ladder.status",
})

# Op verbs that are read-only. A tool whose action enum contains ONLY these is
# read-safe.
_READ_OPS = frozenset({
    "list", "get", "read", "search", "status", "describe", "view", "show", "fetch",
    # File-level discovery (context_ingest op='files'): a plural noun
    # enumerating existing state is a read — without this token the subset
    # rule denied the op on the deleted engine's read() gate.
    "files",
    # A contact card for another Host (contacts_manage op='card'):
    # read from this Host's own records, the same card its dashboard shows.
    # The only op in the registry with this token, so nothing else widens.
    "card",
    # Inspection nouns (a host couldn't read its own embed lane):
    # embedding_route's reads are named current/dims/bastions (its catalog
    # op is retired — "catalog" stays here for
    # model_route op='catalog'), and embed_jobs' stats op is coverage — none
    # are verbs, none mutate, and without them every op of those tools read as
    # an action (HOST_SDK_READ_DENIED).
    "current", "catalog", "dims", "bastions", "coverage",
    # Read ops of mixed tools the host could not reach
    # directly (verified read-only at their executors):
    #   harness_connections op='matrix' (one SELECT + credential-status
    #     reads; the same build the Orchestrator's Coding screen renders),
    #   circles_manage list_conversations / read_thread / list_circles,
    #   host_message conversations / thread (thread stamps a read receipt —
    #     same precedent as inbox_read, already allowlisted).
    # ("browse" and "detail" went with the catalog tool, the only one whose ops
    # carried them, migrations/v206.)
    "matrix", "conversations", "thread", "circles",
    # The `github` tool: its read ops are named for the THING they
    # return rather than a verb — op='repo' fetches one repository's facts,
    # op='code' searches code content, op='tree' lists files. Each is a single
    # GET that writes nothing. Without these tokens the host would have to mint
    # an agent grant just to look up a public repo — the same
    # HOST_SDK_READ_DENIED class every entry above was added to fix.
    "repo", "code", "tree",
    # The `gitea` tool: op='grep' searches text across a repo's
    # files — a bounded sequence of GETs that writes nothing. With this token
    # every op of `gitea` (list/repo/tree/read/grep) is a read and the whole
    # tool lands in the read lane; without it the host would dispatch an
    # agent to grep a repo the user granted it.
    "grep",
    # Two more ops named for what they RETURN rather than a
    # verb, each verified read-only at its executor:
    #   autofill op='saved' — the saved entry KEYS for a host plus a password
    #     flag and whether a standing grant exists. It cannot return a value
    #     (tools_vault.py:_exec_autofill states that as the tool's promise), so it is
    #     the read the host does before it proposes a mapping.
    #   image_generate op='gallery' — the host's own recent images. Its own
    #     schema calls it "free, local read"; generate stays an action, so the
    #     tool does not become whole-tool read-safe.
    "saved", "gallery",
    # image_generate op='look': the Host's own model looks at
    # pictures. It writes nothing: its own pictures are shown in its turn
    # (runtime/turn_pictures); a picture from the web is fetched and put to one
    # call of its own model that holds no tools, and the answer comes back.
    # Until now seeing a picture ('understand') was an action with no door
    # from the Host's own turn — read refused it, operate excluded it — so the
    # only way to see was to dispatch an agent. No other
    # tool's op carries this token (checked against every registry enum).
    "look",
    # The `mcp_connections` tool: op='servers' lists the Host's own
    # MCP connections and op='tools' one server's stored, screened tool list
    # (it may fetch the list again; it changes nothing the person approved).
    # No other tool's op carries either token (checked against every registry
    # enum when they were added). connect/disconnect stay actions.
    "servers", "tools",
})

# Reads of ONE tool whose op names carry words that are not read words in
# general. The gitea PR reads are named for what
# they return — pulls, pull, pull_files, pull_reviews, commit_status,
# branches — and each is GETs only (tools._gitea_pr_read). "pull" and
# "commit" are git WRITE verbs in any other tool, so they are listed here by
# tool and exact op, never added to _READ_OPS where every tool would match.
_TOOL_READ_OPS: dict[str, frozenset] = {
    "gitea": frozenset({"pulls", "pull", "pull_files", "pull_reviews",
                        "commit_status", "branches"}),
}

# Op verbs that MUTATE. Their presence anywhere (action enum OR tool name)
# makes a tool an action — never read-safe.
_WRITE_OPS = frozenset({
    "create", "update", "delete", "send", "log", "set", "write", "run",
    "enroll", "revoke", "cancel", "apply", "approve", "deploy", "patch",
    "exec", "execute", "remove", "add", "edit", "move", "build", "install",
    "restart", "start", "stop", "publish", "store", "save",
    # `github` op='import' CREATES a repository in the user's gitea.
    # It was not previously classified either way, and "uncertain" resolves to
    # not-read-safe, so nothing was ever wrongly allowed — but naming it as a
    # write is what keeps op='import' out of read_safe_ops now that the tool's
    # other ops are recognised reads.
    "import",
})

_READ_NAME_PREFIXES = ("get_", "list_", "read_")
_READ_NAME_SUFFIXES = ("_read", "_status")

# Permanent-state write tools — kept in sync with session._WRITE_TOOLS; the
# fallback is here so the classifier never depends on importing session.py
# (which imports this module). Lazy import below picks up any drift.
_FALLBACK_WRITE_TOOLS = frozenset({"context_writer", "state_store"})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _name_tokens(name: str) -> set:
    return set(_TOKEN_RE.findall(name))


def _is_write_tool(name: str) -> bool:
    """True if `name` writes permanent entity state (session._WRITE_TOOLS)."""
    try:
        from mastercode.runtime.session import _WRITE_TOOLS as _WT  # late: avoids cycle
        if name in _WT:
            return True
    except Exception:
        pass
    return name in _FALLBACK_WRITE_TOOLS


def _is_code_exec(name: str) -> bool:
    """True if `name` executes/builds code (harness pool only). Delegates to the
    scoped-agent guard so there is one definition; falls back to an inline
    mirror if that import is unavailable."""
    try:
        from mastercode.runtime.scoped_agent import _is_code_exec_tool
        return _is_code_exec_tool(name)
    except Exception:
        n = name
        if n in {"build", "sdk_build", "sdk_continue", "harness_run",
                 "mesh_harness_run", "lab_run", "code", "bash", "shell", "exec"}:
            return True
        if n in {"harness_connections", "harness_costs",
                 "harness_transcript",
                 "harness_usage_windows"}:  # mirrors _CODE_EXEC_NAME_EXCEPTIONS
            return False
        return n.endswith(".run") or n.startswith("sdk_") or n.startswith("harness")


def _schema_op_tokens(schema) -> set:
    """Op verbs declared by a tool's action/op enum, lowercased + tokenized.
    These are the tool's *actual* operations — the authoritative signal for the
    'only read-ish ops' test."""
    out: set = set()
    if not isinstance(schema, dict):
        return out
    params = (schema.get("function") or {}).get("parameters") or schema.get("parameters") or {}
    props = params.get("properties") if isinstance(params, dict) else None
    if not isinstance(props, dict):
        return out
    for key in ("action", "op", "operation", "method", "command", "mode"):
        spec = props.get(key)
        if isinstance(spec, dict):
            for val in (spec.get("enum") or []):
                out |= _name_tokens(str(val).lower())
    return out


def is_read_safe_tool(name, schema=None) -> bool:
    """Return True ONLY when `name` is clearly a read-only tool the host may
    execute itself. Conservative: uncertain ⇒ False ⇒ not a read; which lane
    it takes then is tool_lanes.lane_for's call (operate when listed, the
    agent lane only for untrusted work, else a gap).

    `schema` (the tool's native function schema) is optional; when present its
    action/op enum sharpens the decision. Passing None falls back to name-only
    heuristics (still safe — it can only make the classifier MORE conservative).
    """
    n = (name or "").strip().lower()
    if not n:
        return False

    # ── NEVER read-safe ──────────────────────────────────────────────────────
    if _is_write_tool(n) or _is_code_exec(n):
        return False
    # A remote MCP server's tool (mcp:<server>/<tool>) is never a read,
    # whatever its name or the server's own annotations say: every call takes
    # `operate` with its intent. Without this,
    # a server tool named `job_status` would pass the `_status` suffix rule.
    from mastercode.runtime.mcp_names import is_mcp_tool_name
    if is_mcp_tool_name(n):
        return False
    schema_ops = _schema_op_tokens(schema)
    if (schema_ops | _name_tokens(n)) & _WRITE_OPS:
        return False  # any mutating op exposed (enum or name) → an action, not a read

    # ── Clearly read-only ────────────────────────────────────────────────────
    if n in _READ_SAFE_TOOLS:
        return True
    if n.startswith(_READ_NAME_PREFIXES) or n.endswith(_READ_NAME_SUFFIXES):
        return True
    if schema_ops and schema_ops <= _READ_OPS:  # ALL declared ops are read-ish
        return True

    # ── Default: uncertain ⇒ an action (NOT read-safe); tool_lanes.lane_for
    #    places it, and an unlisted action is a gap, never an agent's ──────────
    return False


def read_safe_ops(name, schema=None) -> frozenset:
    """The verifiably read-only SUBSET of a mixed-op tool's op enum.

    A tool like workspace_file (op ∈ list|read|write|delete) can never be
    read-safe as a WHOLE — is_read_safe_tool rightly rejects it — but its
    `list`/`read` ops are pure reads the host should be able to `read()`
    without burning a dispatch (HOST_SDK_READ_DENIED).
    Returns the op values whose every token is in _READ_OPS, for tools that
    are not write-tools / code-exec. Empty set when the tool has no op enum
    or nothing verifiably read-only — callers must treat empty as
    not-readable. NO blanket loosening: `write`, `delete`, and anything
    uncertain never appear in the result."""
    n = (name or "").strip().lower()
    if not n or _is_write_tool(n) or _is_code_exec(n):
        return frozenset()
    if not isinstance(schema, dict):
        return frozenset()
    params = (schema.get("function") or {}).get("parameters") or schema.get("parameters") or {}
    props = params.get("properties") if isinstance(params, dict) else None
    if not isinstance(props, dict):
        return frozenset()
    out: set = set()
    own = _TOOL_READ_OPS.get(n, frozenset())
    for key in ("action", "op", "operation", "method", "command", "mode"):
        spec = props.get(key)
        if isinstance(spec, dict):
            for val in (spec.get("enum") or []):
                v = str(val).lower()
                tokens = _name_tokens(v)
                if (tokens and tokens <= _READ_OPS) or v in own:
                    out.add(v)
    return frozenset(out)


def read_only_variant(name, schema):
    """A deep-copied schema restricted to the tool's verifiably read-only ops —
    the ADVERTISED form of a mixed-op tool the host holds directly (full read
    rights: a read must never need a dispatched agent).

    Returns None when the tool has no read-safe ops (nothing to hold). The
    variant keeps ONLY the read-safe values in each op enum that has any (the
    mutating values vanish from what the model is shown) and annotates the
    description so the model knows which door the other ops take (operate,
    or an agent for untrusted work outside the platform). Advertisement only — execution stays gated per-op by
    sess._read_safe_ops / the SDK read() gate, and both derive from
    read_safe_ops() above, so the shown surface and the enforced surface
    cannot drift apart."""
    ops = read_safe_ops(name, schema)
    if not ops:
        return None
    out = copy.deepcopy(schema)
    fn = out.get("function") if isinstance(out.get("function"), dict) else out
    params = fn.get("parameters") or {}
    props = params.get("properties") if isinstance(params, dict) else None
    if isinstance(props, dict):
        for key in ("action", "op", "operation", "method", "command", "mode"):
            spec = props.get(key)
            if isinstance(spec, dict) and spec.get("enum"):
                keep = [v for v in spec["enum"] if str(v).lower() in ops]
                if keep:
                    spec["enum"] = keep
    listed = ", ".join(sorted(ops))
    # The note must name the lane the OTHER ops actually take on this
    # surface. It used to say "dispatch those" for every narrowed tool — for a
    # direct op (operate lane) that contradicted the tool's own description
    # ("call this tool YOURSELF, NEVER dispatch an agent") in the same
    # paragraph, and the host resolved the contradiction by dispatching
    # (artifact). Lazy import: direct_ops imports nothing
    # from here at module load, but keep the edge one-way anyway.
    #
    # "every other op" is a LIE for a per-op tool: an op in
    # DIRECT_OP_EXCLUDED_OPS (bug_review read/verdict) is in neither the read
    # subset nor
    # `operate`. Sending the host to operate for an op that refuses there is
    # the same class of contradiction this branch was written to fix, one
    # lane over — so the excluded ops are named, with their lane.
    from mastercode.runtime.direct_ops import (
        direct_op_excluded_ops, is_direct_op_tool,
    )
    if is_direct_op_tool(name):
        _excluded = sorted(direct_op_excluded_ops(name))
        if _excluded:
            _ex = ", ".join(_excluded)
            note = (
                f" READ-ONLY here: only op in [{listed}] runs as a direct "
                f"call. Every other op EXCEPT [{_ex}] runs as "
                f"operate(tool='{name}', args={{...}}) — one deterministic "
                "call in your own turn, no agent, result returns to you now. "
                f"[{_ex}] reads content another Host or person wrote, so it "
                "is the one part of this tool that needs a dispatched agent."
            )
        else:
            note = (
                f" READ-ONLY here: only op in [{listed}] runs as a direct "
                f"call; every other op runs as operate(tool='{name}', "
                "args={...}) — one deterministic call in your own turn, no "
                "agent, result returns to you now."
            )
    else:
        # Not an operate tool. The only such mixed tool with a
        # door is the agent lane's (tool_lanes.UNTRUSTED_WORK_TOOLS: github),
        # whose other ops act on content from outside the platform. "dispatch
        # those" was the note on context, keys_manage, app_probe and four more
        # of the Host's own tools while they sat in the old catch-all.
        from mastercode.runtime.tool_lanes import lane_for
        if lane_for(name, schema) == "dispatch":
            note = (
                f" READ-ONLY here: only op in [{listed}] runs as a read. Its other "
                "ops act on content from outside the platform, so an agent runs "
                "them: dispatch with work='foreign_repo'."
            )
        else:
            note = (
                f" READ-ONLY here: only op in [{listed}] runs as a read. Its other "
                "ops have no door from your turn yet: say so plainly; an agent is "
                "only for untrusted work outside the platform."
            )
    desc = fn.get("description")
    fn["description"] = (desc.rstrip() + note) if isinstance(desc, str) and desc else note.strip()
    return out
