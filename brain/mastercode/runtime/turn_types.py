"""runtime/turn_types.py — the turn type a Host turn runs under, and the four
checks the tool gate makes against it.

A TURN TYPE is a user-approved profile
for one kind of turn: what it reads, which tools, keys and bastions it may
touch, which skill and model it runs with. The platform decides which type a
turn is before the model runs, in software, and builds the environment from
it. Inside a type, everything not listed is denied at `_execute_meta_tool`.

NO TYPE IS TODAY. A thread or project whose `type_id` is NULL runs as every
turn ran before this module existed: full prompt, every tool, every key and
bastion the entity holds. Every function here returns "no change" for
`turn_type is None`, and scripts/test_turn_types_gate.py asserts it first.

WHY A GROUP CHECK, NOT BITS ALONE. The capability grant is seven coarse names
(runtime/agent_grant.py): `write_context` opens every tool that needs only
`write_context`, so bits cannot say "ledger and invoices, nothing else".
Schema narrowing in session.py saves tokens; this check is the wall. It is
the Host-side twin of the name wall a dispatched agent already has
(runtime/scoped_agent.py, `if func_name not in scope_set`).

WHY THE GATE, NOT EACH DOOR. `_execute_meta_tool` is reached by the Host's
own tool loop, by `operate` (runtime/direct_ops.py) and by the agent runner
(runtime/scoped_agent.py), so a check placed in its authorize step walls all
three the same way. The key and bastion doors are named here by the argument
that carries the credential name or server name, so no door had to change
and a new door is one row in a table.

FAIL CLOSED. A bound type that cannot be read is not "no type": running
untyped would give the turn more than the user approved. `closed_type`
holds nothing, so every tool call is refused and the refusal says why.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger("cseries.mastercode.turn_types")

# The orchestration verbs ride every turn regardless of type: they are how a
# turn reads its own state and hands a decision to the person. `operate`,
# `dispatch`, `recall` and `agent_status` are run by session.py itself and
# never reach the gate by name (a nested operate(tool="operate") is refused
# as unknown_name there); `escalate` does reach it and passes the group check
# by name. None of them acts on anything a type scopes — the op `operate`
# runs, and the tools an agent is given, are each checked on their own.
# (`orient`, the groups-mode drill, is deleted.)
ALWAYS_ALLOWED: frozenset[str] = frozenset({
    "operate", "dispatch", "escalate", "recall", "agent_status",
    # A typed turn may ask the person about types whatever its tool groups —
    # a draft is inert and every change is the person's click
    # (turn_type_approval). It still needs read_context, the tool's one
    # capability bit, like any call.
    "turn_type",
    # Every turn ends with close_turn, whatever its type. It writes only the Host's own thread topic/title, the
    # user's preferences and the thread goal — nothing a type scopes — and
    # still needs its own capability bits like any call.
    "close_turn",
})

# Tool -> the argument that names a credential in the vault. A typed turn may
# only pass a name listed in its `keys`. keys_manage's `list` op names
# nothing and is not checked; every op that takes `name` is.
TYPE_KEY_ARGS: dict[str, str] = {
    "keys_manage": "name",
    "dcn": "cred_name",
    "embedding_route": "cred_name",
    "context_backend": "cred_name",
    # set points a consumer at the named entry, which the consumer opens on
    # its next use (server/vault/providers.py resolve_wired).
    "credential_wiring": "keys_name",
    # connect binds the named entry to an MCP server's URL (server/
    # mcp_connections.py); the person's approval in the Network app activates it.
    "mcp_connections": "key",
}

# A connected MCP server. A typed turn uses
# a server's tools — mcp:<server>/<tool>, through operate — and reads its tool
# list (mcp_connections op='tools') only when the type holds `mcp:<server>` in
# its keys. The key is the server, not a vault entry: what it unlocks is that
# server, whose own credential the kernel injects.
MCP_CONNECTIONS_TOOL = "mcp_connections"

# Bypass by omission: two doors fill in a credential name
# when the argument is left out — brain/mastercode/context_backends.py
# defaults `cred_name` to 'ctx:s3' for an S3-backed store. A typed turn that
# omits the name is then using that credential, so the check reads the
# default. Keyed by tool; the predicate says which call shapes take the
# default (a machine-backed store names a server, not a credential).
TYPE_KEY_DEFAULTS: dict[str, tuple[str, Any]] = {
    "context_backend": ("ctx:s3", lambda a: str(a.get("op") or "").strip().lower() == "set"
                        and str(a.get("backend") or "").strip().lower() == "s3"),
    # dcn propose_store: the class is required and chosen by the caller
    # (server/network/nac_propose.py:184-186); only a cabinet store carries
    # a credential (`:265-268` defaults it to 'ctx:s3').
    "dcn": ("ctx:s3", lambda a: str(a.get("op") or "").strip().lower() == "propose_store"
            and str(a.get("class") or a.get("store_class") or "").strip().lower() == "cabinet"),
}

# Tool -> the argument that names a server in the user's mesh. A typed turn
# may only pass a server listed in its `bastions`. Every schema argument
# named `server` in the registry is here (the gate test re-sweeps): the three bastion exec doors, the four bastion admin doors,
# the grant proposal (`'*'` is a server name a type must list to use), and
# the three doors that put a machine behind memory or context. The
# bastion's own grant and the person's tap still apply on top.
TYPE_SERVER_ARGS: dict[str, str] = {
    "mgmt_run": "server",
    "mesh_harness_run": "server",
    "lab_run": "server",
    "suspend_bastion": "server",
    "quarantine_bastion": "server",
    "manage_pairing": "server",
    "machine_map": "server",
    "propose_mgmt_grant": "server",
    "embedding_route": "server",
    "context_backend": "server",
    "dcn": "server",
    # One bastion's full record; the roster without it names
    # every machine, as nac_status always has. `server` may be a token name
    # or id too, so RESOLVED_DOORS also checks the hostname it picks.
    "nac_status": "server",
    # One bastion's whole card and the agents seen on it,
    # the same pick as nac_status's; the roster without it names every
    # machine, as get_network always has.
    "get_network": "server",
}

# Tool -> an argument that names a LIST of servers. `context_backend set
# availability='replicate'` fans writes out to the bastions in `replicas`;
# with `replicas` omitted it fans out to every online context-store bastion
# (brain/mastercode/context_backends.py:55-69), which a type cannot name, so
# a typed turn must list them — or hold `'*'` in its bastions.
TYPE_SERVER_LIST_ARGS: dict[str, str] = {
    "context_backend": "replicas",
}

# Tool -> the argument that names a bastion by its TOKEN id instead of its
# hostname. quarantine_bastion takes either; manage_bastion's revoke and
# update take only the id. A server name beside an id does not say which box
# the call reaches: quarantine resolves the id first and drops THAT box's
# peer (server/network/nac_commit.py:1563-1573). So the gate reads the server
# behind the id (resolve_targets) and the check tests that server; an id the
# platform cannot resolve names no server, and only a type that holds '*'
# may leave its target unnamed.
TYPE_SERVER_TOKEN_ARGS: dict[str, str] = {
    "quarantine_bastion": "token_id",
    "manage_bastion": "token_id",
}

# manage_bastion op='enroll' mints a pairing token for a server that is not
# on the mesh yet (nac_commit.py:1877-1878), so it has no name a type could
# list. Adding a server is outside every list but '*'.
MANAGE_BASTION_TOOL = "manage_bastion"
ENROLL_OPS = frozenset({"enroll", "setup", "create"})

# The autofill door fills a site from the vault by HOST, not by credential
# name, so its key entry is `autofill:<host>`.
AUTOFILL_TOOL = "autofill"
AUTOFILL_KEY_PREFIX = "autofill:"

# A browser agent run signs in with the same vault broker: the agent's sealed
# login cookies, and the entries its steps type, each by host
# (server/browser_agents.py:958-970, :1138). The call names only the agent,
# so the gate reads the hosts from the agent's file (resolve_targets) and a
# typed turn needs `autofill:<host>` for every one. `resume` types into the
# same signed-in session, so it is checked the same way.
BROWSER_AGENT_TOOL = "browser_agent"
BROWSER_AGENT_SIGN_IN_OPS = frozenset({"run", "resume"})

DENIAL_REASON = "type_denied"

_TYPE_COLUMNS = (
    "id, entity_id, name, status, keywords, context, skill, tool_groups, "
    "grant_names, keys, bastions, model, thread_id, version, cards"
)


def _row_to_type(row: Any) -> dict:
    """A turn_types row as the dict every function here reads."""
    d = dict(row)
    for k in ("keywords", "tool_groups", "grant_names", "keys", "bastions", "cards"):
        d[k] = [str(x) for x in (d.get(k) or [])]
    if isinstance(d.get("id"), (bytes, bytearray)):
        d["id"] = d["id"].decode()
    d["id"] = str(d["id"]) if d.get("id") is not None else None
    d["thread_id"] = str(d["thread_id"]) if d.get("thread_id") else None
    return d


# The context a CLOSED turn reads: nothing. Shared by closed_type and by
# session.py's fallback when even this module could not be imported.
CLOSED_CONTEXT: dict = {"memory": "none", "documents": False}


def closed_type(reason: str) -> dict:
    """A type that holds nothing — what a turn runs under when its binding
    named a type the platform could not read. Every call is refused and the
    refusal carries `reason` so the Host can say what happened."""
    return {
        "id": None, "name": "(unresolved)", "status": "closed",
        # Closed on context too: a closed turn reads no
        # memory and no documents, or the prompt would carry everything the
        # tools are being refused.
        "keywords": [], "context": dict(CLOSED_CONTEXT), "skill": None,
        "tool_groups": [], "grant_names": [], "keys": [], "bastions": [],
        "model": None, "thread_id": None, "version": 0, "closed_reason": reason,
    }


async def load_turn_type(pool, entity_id: int, *, thread_id: str | None = None,
                         project_id: int | None = None) -> dict | None:
    """The ACTIVE type bound to this turn, or None for an untyped turn.

    Binding order: the thread's `type_id`, then the
    project's. A binding to a type that is not active (draft, retired) is
    no binding — the user has not approved it — so the turn runs untyped.
    A read failure on a BOUND thread/project fails closed (module docstring).
    """
    if pool is None or not entity_id or (not thread_id and project_id is None):
        return None
    # Two reads, so a failure says what it could not read. The first is the
    # binding alone: for the common case — an unbound thread or project,
    # which is every one that exists before a user builds a type — that is
    # the only read, and a NULL ends it. Only a non-NULL binding reads the
    # type itself.
    try:
        type_id = None
        if thread_id:
            type_id = await pool.fetchval(
                "SELECT type_id FROM circles_threads WHERE id = $1::uuid AND entity_id = $2",
                str(thread_id), int(entity_id))
        if type_id is None and project_id is not None:
            type_id = await pool.fetchval(
                "SELECT type_id FROM projects WHERE id = $1 AND entity_id = $2",
                int(project_id), int(entity_id))
    except Exception as e:  # noqa: BLE001
        # Whether this turn is bound is the very thing that could not be
        # read. Fail closed: the cost of a wrongly-closed turn is one refused
        # call the Host can explain; the cost of a wrongly-open one is a
        # wall the user approved not being there. The sentence says exactly
        # this — it does not claim a binding exists.
        log.warning("turn type BINDING could not be read (eid=%s thread=%s project=%s): %s "
                    "— running this turn CLOSED", entity_id, thread_id, project_id, e)
        return closed_type("the platform could not read whether this thread or project "
                           f"is bound to a turn type ({e})")
    if type_id is None:
        return None
    try:
        row = await pool.fetchrow(
            f"SELECT {_TYPE_COLUMNS} FROM turn_types WHERE id = $1 AND entity_id = $2",
            type_id, int(entity_id))
    except Exception as e:  # noqa: BLE001
        log.warning("turn type %s could not be read (eid=%s): %s — running this turn CLOSED",
                    type_id, entity_id, e)
        return closed_type(f"this thread or project is bound to turn type {type_id}, "
                           f"which the platform could not read ({e})")
    if row is None:
        # Bound to a type that no longer exists (the FK is ON DELETE SET
        # NULL, so this is a race at most): no approved profile, untyped.
        return None
    tt = _row_to_type(row)
    if tt.get("status") != "active":
        return None
    return tt


def stamp_scheduled_work(turn_type: dict | None, name: str, args: dict) -> tuple[dict, dict | None]:
    """Work the offline runner will fire later is created ONE way, visibly.
    Checked here, at the one gate every
    call passes (direct, operate, a dispatched agent):

      calendar_log   may not write an event_type='scheduled_task' row — for any
                     Host turn. schedule_task is the one way to schedule.
      reachout_channel  a runbook channel built from a typed turn carries that
                     turn's type_id, which the runbook transport copies into
                     each task it fires; a closed turn builds none.

    Returns (args, denial)."""
    if not isinstance(args, dict):
        return args, None
    a = dict(args)
    # calendar_log is a LOG. A scheduled_task row it wrote would be a task
    # the runner fires with a prompt nobody saw — a hidden note. Refused for
    # every Host turn, typed or not: schedule_task is the one way to schedule,
    # and it writes what is planned into the thread.
    if name == "calendar_log" and str(a.get("event_type") or "").strip().lower() == "scheduled_task":
        return args, {"status": "denied", "reason": "use_schedule_task", "needs_bits": 0,
                      "result": ("calendar_log records events; it does not schedule work. "
                                 "Use schedule_task — it writes what you plan into the thread "
                                 "so the user sees it.")}
    if turn_type is None:
        return args, None
    if not (name == "reachout_channel" and isinstance(a.get("action"), dict)
            and str(a["action"].get("type") or "").strip() == "runbook"):
        return args, None
    if turn_type.get("status") != "active" or not turn_type.get("id"):
        return args, _denial(turn_type, f"scheduled work via {name!r}", name, "group")
    a["action"] = {**a["action"], "type_id": str(turn_type["id"])}
    return a, None


async def load_turn_type_by_id(pool, entity_id: int, type_id: str) -> dict:
    """The type a SCHEDULED task was set under. A task
    carries its type in its payload, so the fired turn runs under it rather
    than under whatever the thread is bound to. Anything but an ACTIVE type
    is CLOSED — never untyped: the task was scheduled narrow, and running it
    with everything the entity holds would be the widening this step exists
    to prevent."""
    try:
        row = await pool.fetchrow(
            f"SELECT {_TYPE_COLUMNS} FROM turn_types WHERE id = $1::uuid AND entity_id = $2",
            str(type_id), int(entity_id))
    except Exception as e:  # noqa: BLE001
        return closed_type(f"the turn type this task was scheduled under could not be read ({e})")
    if row is None:
        return closed_type("the turn type this task was scheduled under no longer exists")
    tt = _row_to_type(row)
    if tt.get("status") != "active":
        return closed_type(f"the turn type this task was scheduled under is {tt.get('status')}")
    return tt


async def resolve_session_type(pool, entity_id: int, *, pinned_type_id: str | None,
                               thread_id: str | None, project_id: int | None) -> dict | None:
    """The type a turn runs under. A PINNED type (a scheduled task's)
    wins over the thread's binding; otherwise the thread's, then the
    project's. Any failure while a pin or a binding exists runs
    CLOSED — never untyped."""
    try:
        if pinned_type_id:
            return await load_turn_type_by_id(pool, entity_id, pinned_type_id)
        # The built-in ROOT type (runtime/root_turn): a turn in the Host's root
        # thread runs under it. Never a pinned task's: a scheduled turn keeps
        # its own type, and the root type has no tasks of its own.
        if thread_id:
            from mastercode.runtime import root_turn as _root
            if await _root.is_root_thread(pool, entity_id, thread_id):
                return _root.root_type(thread_id)
        return await load_turn_type(pool, entity_id, thread_id=thread_id, project_id=project_id)
    except Exception as e:  # noqa: BLE001
        if pinned_type_id or thread_id or project_id is not None:
            return closed_type(f"turn type resolution failed: {e}")
        return None


def grant_bits_for(turn_type: dict | None) -> int | None:
    """The capability bits a typed turn holds — parse_grant over its names,
    so `admin` / `shell_exec` contribute nothing exactly as for an agent.
    None for an untyped turn (the gate then keeps the entity's bits)."""
    if turn_type is None:
        return None
    from mastercode.runtime.agent_grant import parse_grant
    bits, unknown = parse_grant(turn_type.get("grant_names") or [])
    if unknown:
        log.warning("turn type %r names capabilities it cannot hold: %s",
                    turn_type.get("name"), unknown)
    return int(bits)


def tool_category(name: str) -> str:
    """The registry group a tool belongs to (`_meta.category`), across every
    schema source a Host can call — including the platform schemas
    (OPERATOR_PLATFORM_TOOL_SCHEMAS) that tools._find_tool_schema does not
    search, which is where the bastion doors live. `other` when no registry
    knows the name: a type lists `other` only on purpose. A connected MCP
    server's tool (mcp:<server>/<tool>) is in `mcp`."""
    from mastercode.runtime.mcp_names import is_mcp_tool_name
    if is_mcp_tool_name(name):
        return "mcp"
    try:
        from mastercode.public_tool_registry import (
            OPERATOR_PLATFORM_TOOL_SCHEMAS, PUBLIC_TOOL_SCHEMAS)
        for src in (PUBLIC_TOOL_SCHEMAS, OPERATOR_PLATFORM_TOOL_SCHEMAS):
            for schema in src.values():
                if (schema.get("function") or {}).get("name") == name:
                    return str((schema.get("_meta") or {}).get("category") or "other")
    except Exception as e:  # noqa: BLE001
        log.debug("turn_types.tool_category registry read failed: %s", e)
    try:
        from mastercode.runtime.tools import _find_tool_schema
        schema = _find_tool_schema(name)
        if schema is not None:
            return str((schema.get("_meta") or {}).get("category") or "other")
    except Exception as e:  # noqa: BLE001
        log.debug("turn_types.tool_category schema lookup failed: %s", e)
    return "other"


_CATEGORY_MAP: dict[str, str] | None = None


def category_map() -> dict[str, str]:
    """{tool name: group} over every schema source a Host can call, built
    once. session.py narrows the loadable set with it before the pick, so
    the map and the failed-pick fallback see only the type's tools. Same sources as tool_category(); a name in neither is `other`."""
    global _CATEGORY_MAP
    if _CATEGORY_MAP is not None:
        return _CATEGORY_MAP
    out: dict[str, str] = {}
    sources: list = []
    try:
        from mastercode.public_tool_registry import (
            OPERATOR_PLATFORM_TOOL_SCHEMAS, PUBLIC_TOOL_SCHEMAS)
        sources += [PUBLIC_TOOL_SCHEMAS, OPERATOR_PLATFORM_TOOL_SCHEMAS]
    except Exception as e:  # noqa: BLE001
        log.debug("turn_types.category_map registry read failed: %s", e)
    try:
        # The bridge and bot schemas (inbox_send, calendar_log, state_store,
        # state_load, …) — the same sources tools._find_tool_schema walks, so
        # the loadable set and the gate agree on every name's group.
        from mastercode.runtime.tools import _BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS
        sources += [_BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS]
    except Exception as e:  # noqa: BLE001
        log.debug("turn_types.category_map bridge/bot read failed: %s", e)
    for src in sources:
        for schema in (src or {}).values():
            if not isinstance(schema, dict):
                continue
            n = (schema.get("function") or {}).get("name")
            if n and n not in out:
                out[n] = str((schema.get("_meta") or {}).get("category") or "other")
    _CATEGORY_MAP = out
    return out


def _denial(turn_type: dict, what: str, item: str | None = None,
            kind: str = "group") -> dict:
    """The gate's envelope for a call outside the type. `needs_bits` is 0:
    no grant would have passed this wall — the type is what the user
    approved, and only the user widens it. `kind` is which
    of the four checks refused (group | key | autofill | server) so the
    gate can look up `holders` — the active types that hold `item`."""
    from mastercode.runtime.agent_grant import CAP_DENIAL_KEY
    name = turn_type.get("name") or "(unresolved)"
    if turn_type.get("status") == "closed":
        result = (f"This turn holds nothing: {turn_type.get('closed_reason')}. "
                  f"Tell the user; a retry may work once the store is readable.")
    else:
        # No note to another turn (a note is the threat —
        # an ungoverned memory entry). The refusal is said in the thread,
        # the platform's timeline records it, and a turn of the type that
        # holds the work sees it there when the user takes it there.
        result = (f"Not in this turn's type. This is a {name!r} turn and {what} is "
                  f"outside it. You cannot widen the type yourself. Say in the thread "
                  f"what you could not do and which types hold it (listed below); the "
                  f"timeline records the refusal. If the user wants this type to hold "
                  f"it, they can widen it.")
    return {
        "status": "denied",
        CAP_DENIAL_KEY: 0,
        "reason": DENIAL_REASON,
        "turn_type": name,
        "outside": what,
        "kind": kind,
        "item": item,
        "holders": [],
        "result": result,
    }


def check_call(turn_type: dict | None, name: str, args: dict | None,
               targets: dict | None = None) -> dict | None:
    """The four checks for one tool call under `turn_type`.
    None = the call may proceed to the bits check. A dict = the denial
    envelope. Untyped (None) never denies.

    `targets` is what resolve_targets read for a door whose arguments do
    not name what it reaches. A door that needs one and finds it missing is
    refused, so a caller that skips the read gets a stricter wall, never a
    wider one."""
    if turn_type is None:
        return None
    a = args if isinstance(args, dict) else {}
    t = targets if isinstance(targets, dict) else {}

    # The built-in ROOT type (runtime/root_turn) holds exactly its allowlist,
    # the orchestration verbs included: anything else is refused by name.
    if turn_type.get("builtin") is not None:
        from mastercode.runtime import root_turn as _root
        if _root.is_root(turn_type) and _root.admits(name, a):
            return None
        return _denial(turn_type, f"tool {name!r}", name, "group")

    # 0. A closed type holds nothing, primitives included: the binding named
    # a type the platform could not read, and the only safe turn is one
    # that can say so and do nothing else.
    if turn_type.get("status") == "closed":
        return _denial(turn_type, f"tool {name!r}", name, "group")

    # A connected MCP server's tool: the type must hold that server's key,
    # `mcp:<server>`, checked on every call. Holding it is the whole grant —
    # the `mcp` group is connection management (connect, disconnect), which a
    # type that only USES a server need not hold.
    from mastercode.runtime import mcp_names
    if mcp_names.is_mcp_tool_name(name):
        parsed = mcp_names.parse_tool_name(name)
        if parsed is None:
            return _denial(turn_type, f"tool {name!r}", name, "group")
        key = mcp_names.type_key(parsed[0])
        if key not in set(turn_type.get("keys") or []):
            return _denial(turn_type, f"MCP server {parsed[0]!r}", key, "key")
        return None

    # A function the Host wrote in its own database, world:<name>: the type must hold
    # the key `world:`, checked on every call, as it holds `mcp:<server>`.
    from mastercode import world_db as _world
    if name.startswith(_world.WORLD_TOOL_PREFIX):
        if _world.parse_tool_name(name) is None:
            return _denial(turn_type, f"tool {name!r}", name, "group")
        if _world.WORLD_TYPE_KEY not in set(turn_type.get("keys") or []):
            return _denial(turn_type, "the Host's own database functions", _world.WORLD_TYPE_KEY, "key")
        return None

    # 1. Tool group.
    if name not in ALWAYS_ALLOWED:
        group = tool_category(name)
        if group not in set(turn_type.get("tool_groups") or []):
            return _denial(turn_type, f"tool {name!r} (group {group!r})", group, "group")

    # 2. Bits are checked by the gate with grant_bits_for(); not here.

    # 3. Keys.
    key_arg = TYPE_KEY_ARGS.get(name)
    if key_arg:
        key_name = a.get(key_arg)
        if not (isinstance(key_name, str) and key_name.strip()):
            _default = TYPE_KEY_DEFAULTS.get(name)
            if _default and _default[1](a):
                key_name = _default[0]
        if isinstance(key_name, str) and key_name.strip():
            if key_name.strip() not in set(turn_type.get("keys") or []):
                return _denial(turn_type, f"key {key_name.strip()!r}", key_name.strip(), "key")
    if name == AUTOFILL_TOOL:
        host = str(a.get("host") or "").strip().lower()
        op = str(a.get("op") or "saved").strip().lower()
        if host and op != "list":
            if f"{AUTOFILL_KEY_PREFIX}{host}" not in set(turn_type.get("keys") or []):
                return _denial(turn_type, f"autofill on {host!r}",
                               f"{AUTOFILL_KEY_PREFIX}{host}", "key")
    if name == MCP_CONNECTIONS_TOOL and str(a.get("op") or "").strip().lower() == "tools":
        # A server's tool list is what search returns only to a type that holds
        # the server; the groups-mode way to read it is held the same way.
        server = str(a.get("name") or "").strip()
        if server and mcp_names.type_key(server) not in set(turn_type.get("keys") or []):
            return _denial(turn_type, f"MCP server {server!r}", mcp_names.type_key(server), "key")

    # 4. Bastions.
    bastions = set(turn_type.get("bastions") or [])
    server_arg = TYPE_SERVER_ARGS.get(name)
    if server_arg:
        server = a.get(server_arg)
        if isinstance(server, str) and server.strip():
            if server.strip() not in bastions:
                return _denial(turn_type, f"server {server.strip()!r}", server.strip(), "server")
    if (name == MANAGE_BASTION_TOOL
            and str(a.get("op") or "list").strip().lower() in ENROLL_OPS
            and "*" not in bastions):
        return _denial(turn_type, "enrolling a new server", "*", "server")
    # Two drafts name a server in an argument of another name: an egress rule's
    # exit ('bastion:<hostname>', server/network/nac_propose.py) and a
    # pipeline whose code lives on a machine (pipeline.source.machine).
    exit_ = str(a.get("exit") or "").strip() if name == "propose_egress_rule" else ""
    if exit_.lower().startswith("bastion:"):
        host = exit_.split(":", 1)[1].strip()
        if host not in bastions:
            return _denial(turn_type, f"server {host!r} (egress exit)", host, "server")
    # The pipeline door takes the document as an object or as JSON text
    # (runtime/tools.py _exec_pipeline), so the check reads both.
    doc = a.get("pipeline") if name == "pipeline" else None
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except ValueError:
            doc = None   # the door refuses text that is not JSON
    if isinstance(doc, dict):
        src = doc.get("source")
        machine = str(src.get("machine") or "").strip() if isinstance(src, dict) else ""
        if machine and machine not in bastions:
            return _denial(turn_type, f"server {machine!r} (the pipeline's code server)",
                           machine, "server")
    list_arg = TYPE_SERVER_LIST_ARGS.get(name)
    if list_arg:
        reps = a.get(list_arg)
        if isinstance(reps, list):
            for s in reps:
                s = str(s or "").strip()
                if s and s not in bastions:
                    return _denial(turn_type, f"replica server {s!r}", s, "server")
        if (name == "context_backend"
                and str(a.get("op") or "").strip().lower() == "set"
                and str(a.get("availability") or "").strip().lower() == "replicate"
                and not [s for s in (reps if isinstance(reps, list) else []) if str(s or "").strip()]
                and "*" not in bastions):
            # An unnamed fan-out reaches every online context-store bastion;
            # only a type that holds '*' may leave the targets unnamed.
            return _denial(turn_type, "replicate to unnamed servers", "*", "server")

    # 5. What the call reaches without naming it, read by resolve_targets.
    door = RESOLVED_DOORS.get(name)
    if door is not None and door.needs(a):
        what = door.what(a)
        reach = t.get("reach")
        if not isinstance(reach, dict):
            return _denial(turn_type, f"{what}: what it reaches could not be read", None, "group")
        for s in reach.get("servers") or []:
            if s == "*":
                if "*" not in bastions:
                    return _denial(turn_type, f"{what}, which reaches a server the type cannot name",
                                   "*", "server")
            elif s not in bastions:
                return _denial(turn_type, f"server {s!r} ({what})", s, "server")
        keys = set(turn_type.get("keys") or [])
        for k in reach.get("keys") or []:
            if k not in keys:
                return _denial(turn_type, f"key {k!r} ({what})", k, "key")
    return None


async def resolve_targets(pool, entity_id: int, name: str, args: dict | None,
                          interaction_context: str | None = None) -> dict:
    """What a call reaches when its arguments do not name it, read for the
    gate before check_call: {"reach": {"servers": [...], "keys": [...]}} from
    the door's entry in RESOLVED_DOORS. A call no entry covers reads nothing
    and gets {}. A read that fails also gets {}, and check_call then refuses.
    A door that takes its project from the conversation reads the same
    project here, on the door's own rule for when it does."""
    a = args if isinstance(args, dict) else {}
    door = RESOLVED_DOORS.get(name)
    if door is None or not door.needs(a):
        return {}
    if (door.context_project is not None and door.context_project(a)
            and (interaction_context or "").startswith("project:")):
        # The door's own parse: an id that is not a number is left out, as
        # the door leaves it out.
        try:
            a = dict(a, project_id=int((interaction_context or "").split(":", 1)[1]))
        except (ValueError, IndexError):
            pass
    try:
        reach = await door.read(pool, int(entity_id), a)
    except Exception as e:  # noqa: BLE001
        log.debug("turn_types.resolve_targets %s read failed: %s", name, e)
        return {}
    if not isinstance(reach, dict):
        return {}

    def _names(xs) -> list[str]:
        return sorted({str(x).strip() for x in (xs or []) if str(x or "").strip()})
    return {"reach": {"servers": _names(reach.get("servers")), "keys": _names(reach.get("keys"))}}


# ── Doors whose call does not name what it reaches ──────────────────────────
#
# Each entry: `needs(args)` says whether this call shape reaches a server or
# key its arguments do not name; `read(pool, entity_id, args)` returns
# {"servers": [...], "keys": [...]} the way the door itself picks them, or
# None when it cannot tell; `what(args)` names the call in a refusal. '*' in
# servers means a server the platform cannot name; only a type that holds
# '*' may reach one. A read never opens a secret or changes anything.

@dataclass(frozen=True)
class ResolvedDoor:
    needs: Callable[[dict], bool]
    read: Callable[[Any, int, dict], Awaitable[dict | None]]
    what: Callable[[dict], str]
    # The door takes `project_id` from a project conversation on its own
    # rule (build, review.adopt, review.adopt_undo), so the read does too:
    # this is that rule, copied from the door, and None for a door that
    # never does. Each door's rule differs; one shared rule let a call the
    # door fills in (project_id 0) be read as project 0.
    context_project: Callable[[dict], bool] | None = None


def _op(a: dict, default: str = "") -> str:
    return str(a.get("op") or default).strip().lower()


async def _read_bastion_token(pool, eid: int, a: dict) -> dict | None:
    # The door's own parse (runtime/tools.py _exec_quarantine_bastion) and its
    # own entity-scoped lookup; a live token with no reported hostname, or no
    # live token, names no server.
    from server.network.nac_commit import resolve_bastion
    b = await resolve_bastion(pool, eid, token_id=int(a["token_id"]))
    server = str((b or {}).get("server") or "").strip()
    return {"servers": [server or "*"]}


async def _read_nac_status_server(pool, eid: int, a: dict) -> dict | None:
    # nac_status's `server` takes a hostname, a token name or a token id
    # (runtime/tools.py _nac_status_view), so the string a type lists can
    # pick a machine whose hostname is another. The door's own rows
    # (status_snapshot reads topology.list_bastions) and its own pick; the
    # machine whose record comes back is checked by its hostname. One with
    # no reported hostname, or no machine at all (an empty read included),
    # names no server.
    from server.network.topology import list_bastions, pick_bastion
    b = pick_bastion(await list_bastions(pool, eid), a.get("server"))
    return {"servers": [str((b or {}).get("hostname") or "").strip() or "*"]}


async def _read_browser_agent(pool, eid: int, a: dict) -> dict | None:
    from server import browser_agents
    spec = browser_agents.load_agent(str(a.get("name") or "").strip().lower())
    if spec is None or spec.get("invalid"):
        return None
    return {"keys": [f"{AUTOFILL_KEY_PREFIX}{h}" for h in browser_agents.sign_in_hosts(spec)]}


async def _read_compute_connection(pool, eid: int, a: dict) -> dict | None:
    # The connection's sealed key, from the same record the door reads
    # (runtime/tools.py _exec_connect_compute: behavioral_params
    # .compute_connections, matched by id). No such connection: the door
    # refuses and opens nothing.
    row = await pool.fetchrow(
        "SELECT behavioral_params FROM entity_registry WHERE id=$1", eid)
    bp = (row and row["behavioral_params"]) or {}
    if isinstance(bp, str):
        import json as _json
        bp = _json.loads(bp)
    conns = bp.get("compute_connections") if isinstance(bp, dict) else None
    cid = str(a.get("connection_id") or "").strip()
    conn = next((c for c in (conns if isinstance(conns, list) else [])
                 if isinstance(c, dict) and c.get("id") == cid), None)
    return {"keys": [conn["cred_name"]] if conn and conn.get("cred_name") else []}


async def _read_integration(pool, eid: int, a: dict) -> dict | None:
    # The vault entry behind an integration (server/integrations/core:
    # credentials._vault_name over the row's credentials_ref). revoke and
    # grant_app act on a connection's; set_client revokes the family's
    # previous client secret; families opens every client secret to report
    # where each comes from (oauth.client_source).
    from server.integrations.core import credentials as _creds
    from server.integrations.core import storage as _storage
    op = _op(a)
    if op in ("revoke", "grant_app"):
        row = await _storage.get_connection(str(a.get("connection_id") or "").strip())
        refs = [row.get("credentials_ref")] if row else []
    elif op == "set_client":
        row = await _storage.get_oauth_client(str(a.get("family") or "").strip().lower())
        refs = [row.get("credentials_ref")] if row else []
    else:  # families
        refs = [r.get("credentials_ref") for r in await _storage.list_oauth_clients()]
    return {"keys": [_creds._vault_name(r) for r in refs if r]}


async def _read_github(pool, eid: int, a: dict) -> dict | None:
    # The door's own ladder (runtime/tools.py _github_repo_token): the entry
    # named 'github', else the first other github-named api key that opens.
    # Which one opens is only known by opening, so a type must hold them
    # all. None stored: public-only, nothing opened.
    from server.vault import api as vault_api
    from server.vault.providers import matches_provider
    active = [str(r.get("logical_name") or "")
              for r in await vault_api.list_secrets(pool, entity_id=eid)
              if not r.get("deactivated_at")]
    if "github" in active:
        return {"keys": ["github"]}
    return {"keys": [n for n in active if matches_provider("github", "api_key", n)]}


async def _read_harness_run(pool, eid: int, a: dict, *, lab: bool = False) -> dict | None:
    # server/network/nac_harness.run_harness_op: a service mirror sends the
    # push token stored for that server and service; every other mode but
    # service hands the run the harness sign-in. A mode this Host may not
    # dispatch is refused before either is opened.
    from mastercode.runtime.tools import OPERATOR_ENTITY_ID
    from server.network import nac_harness
    if lab:
        mode = "lab"
    else:
        mode = str(a.get("mode") or ("echo" if eid == OPERATOR_ENTITY_ID else "service")).strip().lower()
    if nac_harness.host_mode_refusal(eid, mode):
        return {}
    if mode == "service":
        if str(a.get("service_op") or "").strip().lower() == "mirror":
            return {"keys": [nac_harness.service_push_key_name(
                str(a.get("server") or ""), str(a.get("service_name") or ""))]}
        return {}
    return {"keys": [nac_harness.agent_creds_vault_name()]}


async def _read_lab_run(pool, eid: int, a: dict) -> dict | None:
    return await _read_harness_run(pool, eid, a, lab=True)


def _json_obj(v) -> dict:
    if isinstance(v, str):
        import json as _json
        v = _json.loads(v)
    return v if isinstance(v, dict) else {}


async def _read_dcn(pool, eid: int, a: dict) -> dict | None:
    # A store op reaches what the store's approved terms name
    # (server/network/nac_dcn.py run_dcn_op): a machine store's server, a
    # cabinet's credential (default 'ctx:s3'). run_plan reaches its store's,
    # and the plan's own server if it names one. Read from the terms, never
    # the bare column, as the door does; no approved store, nothing reached.
    plan_server = None
    if _op(a) == "run_plan":
        plan = await pool.fetchrow(
            "SELECT store_id, signed_terms FROM dcn_move_plans WHERE id=$1 AND entity_id=$2",
            int(a["plan_id"]), eid)
        if plan is None:
            return {}
        store_id = int(plan["store_id"])
        plan_server = _json_obj(plan["signed_terms"]).get("server")
    else:
        store_id = int(a["store_id"])
    # The door's own row set: its grant loader (nac_dcn._signed_dcn_grants)
    # does not filter on entity_id, so neither does this read; a narrower
    # read could find no store where the door finds one, and reach nothing.
    row = await pool.fetchrow(
        "SELECT class, signed_terms FROM dcn_stores WHERE id=$1 "
        "AND approved_at IS NOT NULL AND revoked_at IS NULL", store_id)
    if row is None:
        return {}
    terms = _json_obj(row["signed_terms"])
    if (terms.get("class") or row["class"]) == "cabinet":
        return {"keys": [terms.get("cred_name") or "ctx:s3"]}
    return {"servers": [terms.get("server") or "*"] + ([plan_server] if plan_server else [])}


async def _forge_reach(pool, project_id) -> dict:
    # Where a project's code lives (server/routes/project_forge.py
    # project_forge, the door's own read): on a machine, every sync, merge
    # and review hold reaches that server with the push token stored for its
    # code server. The platform forge reaches neither; a setting that does
    # not hold together refuses before anything is written.
    from server.network.nac_harness import service_push_key_name
    from server.routes.project_forge import ForgeError, forge_from_config
    row = await pool.fetchrow("SELECT repo, config FROM projects WHERE id=$1", int(project_id))
    if row is None:
        return {}
    try:
        forge = forge_from_config(_json_obj(row["config"]) or None, row["repo"])
    except ForgeError:
        return {}
    if forge.kind != "machine":
        return {}
    return {"servers": [forge.machine],
            "keys": [service_push_key_name(forge.machine, forge.service)]}


async def _read_project_forge(pool, eid: int, a: dict) -> dict | None:
    return await _forge_reach(pool, a["project_id"])


async def _read_adopt(pool, eid: int, a: dict) -> dict | None:
    # review.adopt writes a run into the RUN's project (server/routes/review.py
    # adopt_run_service: harness_run.project_id wins over the argument, and a
    # different project_id is refused). The run is named by run_id or run,
    # as the door reads it (runtime/tools.py _exec_review_adopt). No such
    # run: the door writes nothing. A run with no project of its own, or a
    # session adopt naming none, lands in the project its session manifest
    # names, which this read does not open: unknown, so refused.
    run_id = a.get("run_id") or a.get("run")
    project_id = a.get("project_id")
    if run_id is not None:
        run = await pool.fetchrow("SELECT project_id FROM harness_run WHERE id = $1",
                                  int(run_id))
        if run is None:
            return {}
        if run["project_id"] is not None:
            project_id = run["project_id"]
    if project_id is None:
        return None
    return await _forge_reach(pool, project_id)


async def _read_mgmt_grant(pool, eid: int, a: dict) -> dict | None:
    # Approving or revoking a management grant acts for the server it names:
    # the signed terms once approved, the draft's column before
    # (server/network/nac_commit.py approve_mgmt_grant / revoke_mgmt_grant).
    row = await pool.fetchrow(
        "SELECT server, approved_at, signed_terms FROM nac_mgmt_grants "
        "WHERE id=$1 AND revoked_at IS NULL", int(a["id"]))
    if row is None:
        return {}
    signed = _json_obj(row["signed_terms"]) if row["approved_at"] is not None else {}
    return {"servers": [str(signed.get("server") or row["server"] or "").strip() or "*"]}


async def _read_model_bench(pool, eid: int, a: dict) -> dict | None:
    # A local bench runs on the first server serving the model
    # (runtime/model_bench.py, model_backends.match_local_model). "First" is
    # the first row of a read with no order, which the bench makes again
    # after this one, and a server can come online between the two: which
    # server it lands on is not known here, so a type must list every server
    # serving the model, as with the github keys above. A failed read raises
    # (strict), so the call is refused rather than read as reaching nothing.
    from mastercode.model_backends import local_model_servers
    return {"servers": await local_model_servers(pool, eid, str(a.get("model") or ""),
                                                 strict=True)}


_SHIP_RUNS = ("POST", "/api/ships/runs")
_BACKUP_SETTINGS = ("PUT", "/api/backup/settings")


def _kernel_route(a: dict) -> tuple[str, str]:
    return (str(a.get("method") or "GET").strip().upper(),
            str(a.get("path") or "").strip().rstrip("/"))


async def _read_kernel_op(pool, eid: int, a: dict) -> dict | None:
    # Two routes reach what a stored record names. A ship run goes to every
    # machine its pipeline's steps name, sealing each step op's credentials
    # to it (server/ships/runner.py start_run, :545-550). The backup
    # destination is a machine (target.address) or an S3 key
    # (target.cred_name) the background shipper later uses.
    body = a.get("body") if isinstance(a.get("body"), dict) else {}
    if _kernel_route(a) == _SHIP_RUNS:
        from server.ships import pipeline as _pl
        from server.ships.runner import load_action_op
        steps = (await _pl.find(pool, str(body.get("pipeline") or "").strip()))["spec"]["steps"]
        keys: list = []
        for s in steps:
            keys += (await load_action_op(pool, s["op"]))["manifest"].get("credentials") or []
        return {"servers": [s["machine"] for s in steps], "keys": keys}
    target = body.get("target")
    if not isinstance(target, dict):
        return {}
    kind = str(target.get("kind") or "").strip().lower()
    if kind == "s3":
        return {"keys": [str(target.get("cred_name") or "").strip()]}
    if kind == "machine":
        return {"servers": [str(target.get("address") or "").strip() or "*"]}
    return {}


_TOKEN_DOOR = ResolvedDoor(
    needs=lambda a: a.get("token_id") is not None,
    read=_read_bastion_token,
    what=lambda a: f"bastion token {a.get('token_id')!r}",
)

RESOLVED_DOORS: dict[str, ResolvedDoor] = {
    **{door: _TOKEN_DOOR for door in TYPE_SERVER_TOKEN_ARGS},
    "nac_status": ResolvedDoor(
        needs=lambda a: bool(str(a.get("server") or "").strip()),
        read=_read_nac_status_server,
        what=lambda a: f"the record of bastion {str(a.get('server') or '').strip()!r}",
    ),
    # get_network(server=) picks with the same topology.pick_bastion over the
    # same rows (nac_commit.bastions_list is list_bastions, decorated), so the
    # same read names the machine whose card comes back (tools._get_network_view).
    "get_network": ResolvedDoor(
        needs=lambda a: bool(str(a.get("server") or "").strip()),
        read=_read_nac_status_server,
        what=lambda a: f"the card of bastion {str(a.get('server') or '').strip()!r}",
    ),
    BROWSER_AGENT_TOOL: ResolvedDoor(
        needs=lambda a: (_op(a, "list") in BROWSER_AGENT_SIGN_IN_OPS
                         and bool(str(a.get("name") or "").strip())),
        read=_read_browser_agent,
        what=lambda a: f"browser agent {str(a.get('name') or '').strip().lower()!r}",
    ),
    "connect_compute": ResolvedDoor(
        needs=lambda a: (_op(a, "list") in ("dispatch", "revoke", "assign_role")
                         and bool(str(a.get("connection_id") or "").strip())),
        read=_read_compute_connection,
        what=lambda a: f"compute connection {str(a.get('connection_id') or '').strip()!r}",
    ),
    "integrations_manage": ResolvedDoor(
        needs=lambda a: _op(a) in ("revoke", "grant_app", "set_client", "families"),
        read=_read_integration,
        what=lambda a: f"integrations {_op(a)}",
    ),
    "github": ResolvedDoor(
        needs=lambda a: True,
        read=_read_github,
        what=lambda a: "the GitHub sign-in",
    ),
    "mesh_harness_run": ResolvedDoor(
        needs=lambda a: True,
        read=_read_harness_run,
        what=lambda a: f"a harness run on {str(a.get('server') or '').strip()!r}",
    ),
    "lab_run": ResolvedDoor(
        needs=lambda a: True,
        read=_read_lab_run,
        what=lambda a: f"a lab run on {str(a.get('server') or '').strip()!r}",
    ),
    "dcn": ResolvedDoor(
        needs=lambda a: ((_op(a) in ("list", "stat", "read_range", "search", "catalog")
                          and a.get("store_id") is not None)
                         or (_op(a) == "run_plan" and a.get("plan_id") is not None)),
        read=_read_dcn,
        what=lambda a: (f"dcn plan {a.get('plan_id')!r}" if _op(a) == "run_plan"
                        else f"dcn store {a.get('store_id')!r}"),
    ),
    "build": ResolvedDoor(
        needs=lambda a: str(a.get("action") or "").strip().lower() in ("build", "continue", "run"),
        read=_read_project_forge,
        what=lambda a: f"a build of project {a.get('project_id')!r}",
        # runtime/tools.py _exec_build: the conversation's project when
        # project_id is None or "".
        context_project=lambda a: a.get("project_id") in (None, ""),
    ),
    "ship_project_app": ResolvedDoor(
        needs=lambda a: True,
        read=_read_project_forge,
        what=lambda a: f"shipping project {a.get('project_id')!r}",
    ),
    "workspace_file": ResolvedDoor(
        needs=lambda a: (_op(a) in ("write", "delete")
                         and a.get("project_id") not in (None, "")),
        read=_read_project_forge,
        what=lambda a: f"a {_op(a)} in project {a.get('project_id')!r}",
    ),
    "publish_app": ResolvedDoor(
        needs=lambda a: _op(a) == "publish",
        read=_read_project_forge,
        what=lambda a: f"publishing project {a.get('project_id')!r}",
    ),
    "review.adopt": ResolvedDoor(
        needs=lambda a: a.get("dry_run") is not True,
        read=_read_adopt,
        what=lambda a: f"adopting run {(a.get('run_id') or a.get('run'))!r}",
        # runtime/tools.py, the review.adopt branch: the conversation's
        # project for any falsy project_id, and only when no run is named.
        context_project=lambda a: (not a.get("project_id") and not a.get("run_id")
                                   and not a.get("run")),
    ),
    "review.adopt_undo": ResolvedDoor(
        needs=lambda a: True,
        read=_read_project_forge,
        what=lambda a: f"undoing an adopt in project {a.get('project_id')!r}",
        # runtime/tools.py, the review.adopt_undo branch: the conversation's
        # project for any falsy project_id.
        context_project=lambda a: not a.get("project_id"),
    ),
    **{door: ResolvedDoor(
        needs=lambda a: (str(a.get("kind") or "").strip().lower() in ("mgmt_grant", "grant", "mgmt")
                         and a.get("id") is not None),
        read=_read_mgmt_grant,
        what=lambda a: f"management grant {a.get('id')!r}",
    ) for door in ("approve_nac_draft", "revoke_nac_rule")},
    "model_bench": ResolvedDoor(
        needs=lambda a: (_op(a, "status") == "run"
                         and str(a.get("provider") or "").strip().lower() == "local"),
        read=_read_model_bench,
        what=lambda a: f"a local bench of {str(a.get('model') or '').strip()!r}",
    ),
    "kernel_op": ResolvedDoor(
        needs=lambda a: _kernel_route(a) in (_SHIP_RUNS, _BACKUP_SETTINGS),
        read=_read_kernel_op,
        what=lambda a: " ".join(_kernel_route(a)),
    ),
}


async def holders_of(pool, entity_id: int, *, group: str | None = None,
                     key: str | None = None, server: str | None = None) -> list[str]:
    """Names of the entity's ACTIVE types that hold the refused item — a
    lookup the refusal carries so the Host can name where the work belongs.
    Best effort; an empty list on any failure."""
    if pool is None or not entity_id:
        return []
    try:
        rows = await pool.fetch(
            "SELECT name, tool_groups, keys, bastions FROM turn_types "
            "WHERE entity_id = $1 AND status = 'active' ORDER BY lower(name)",
            int(entity_id),
        )
    except Exception as e:  # noqa: BLE001
        log.debug("turn_types.holders_of failed: %s", e)
        return []
    out: list[str] = []
    for r in rows or []:
        d = dict(r)
        if group and group in (d.get("tool_groups") or []):
            out.append(str(d["name"]))
        elif key and key in (d.get("keys") or []):
            out.append(str(d["name"]))
        elif server and server in (d.get("bastions") or []):
            out.append(str(d["name"]))
    return out


# ── The environment from the type ─────────────────────
#
# Three more things a typed turn takes from its profile, each an identity for
# an untyped turn: the SKILL it reads (instead of the per-message vote), the
# CONTEXT the recall reads (which projects, whether the user's documents, and
# how much of the standing memory), and the MODEL it runs on (instead of the
# host's chat route).

CONTEXT_MEMORY_SCOPES: tuple[str, ...] = ("all", "projects", "none")


def context_filter(turn_type: dict | None) -> dict | None:
    """The recall filter a typed turn's `context` column means, normalized:

        {"projects": [int, ...] | None,   # None = every project
         "documents": bool,               # the user's uploaded documents store
         "memory": "all" | "projects" | "none"}

    `memory="projects"` keeps only rows tagged with one of the listed
    projects and drops the standing importance/recency list, which is by
    construction cross-project. An empty `context` on a type narrows nothing
    — the user who approved the type listed nothing to narrow to. Untyped
    (None) returns None, and every reader treats None as today."""
    if turn_type is None:
        return None
    if turn_type.get("status") == "closed":
        return {"projects": None, "documents": False, "memory": "none"}
    raw = turn_type.get("context")
    if not isinstance(raw, dict) or not raw:
        return None
    projects: list[int] | None = None
    if isinstance(raw.get("projects"), list):
        projects = []
        for p in raw["projects"]:
            try:
                projects.append(int(p))
            except (TypeError, ValueError):
                continue
    memory = str(raw.get("memory") or ("projects" if projects else "all")).strip().lower()
    if memory not in CONTEXT_MEMORY_SCOPES:
        memory = "all"
    if memory == "projects" and not projects:
        # "projects" with none listed is "none": a scope that names nothing
        # reads nothing, never everything.
        memory = "none"
    documents = raw.get("documents")
    if documents is None:
        documents = True
    return {"projects": projects, "documents": bool(documents), "memory": memory}


def type_skill(turn_type: dict | None) -> str | None:
    """The skill name a typed turn reads instead of the per-group vote; None
    when untyped or the type names none (then the vote runs as today)."""
    if turn_type is None:
        return None
    s = str(turn_type.get("skill") or "").strip()
    return s or None


def thread_model_pick(thread_override) -> bool:
    """True when a per-thread override names a MODEL — the same test the
    route resolver makes (inference_config.resolve_role_route, step 1: a
    normalized provider AND model, from `lane`/`provider` + `model`/
    `model_id`). A thread's override also carries the context budget and the
    spend allowance, so its truthiness is not a model pick: a type's model must not be skipped because the person set
    a depth dial."""
    if not isinstance(thread_override, dict):
        return False
    try:
        from mastercode.inference_config import normalize_provider
        p, m = normalize_provider(
            thread_override.get("lane") or thread_override.get("provider"),
            thread_override.get("model") or thread_override.get("model_id"))
        return bool(p and m)
    except Exception:  # noqa: BLE001
        return False


async def route_for_type(pool, entity_id: int, turn_type: dict | None,
                         chat_route: dict) -> dict:
    """The {provider, model, base_url, fallback_*} a typed turn runs on.

    No model on the type (or untyped): the host's chat route, unchanged. A
    model on the type: the same three readings the dispatch model lever uses
    (runtime/scoped_agent.py, the route section) — the host's own chat model
    runs where the host runs it; otherwise the ONE normalizer decides the
    provider (a Claude-shaped id → 'anthropic', else OpenRouter), and a model
    a machine on the user's network serves right now runs LOCAL. A route
    that cannot be built falls back to the chat route with a warning rather
    than failing the turn: the type's model is a preference the user
    approved, not a wall."""
    model = str((turn_type or {}).get("model") or "").strip() if turn_type else ""
    if not model:
        return chat_route
    try:
        chat_model = str((chat_route or {}).get("model") or "").strip().lower()
        if chat_model and chat_model == model.lower() and not (chat_route or {}).get("unconfigured"):
            return chat_route
        from mastercode.inference_config import normalize_provider, provider_allowed_for_role
        # `provider:model` names a provider outright (handed whole to the
        # normalizer, 'anthropic:claude-…' would become a model id no
        # provider serves). A prefix that is not a routable provider is
        # part of the model id (a local tag like 'qwen3:8b').
        provider_hint, bare = "openrouter", model
        if ":" in model:
            _pfx, _rest = model.split(":", 1)
            if _pfx and _rest and provider_allowed_for_role(_pfx, "chat"):
                provider_hint, bare = _pfx, _rest
        provider, eff_model = normalize_provider(provider_hint, bare)
        base_url = None
        if provider in ("openrouter", "local"):
            # A model a machine on the user's network serves right now runs
            # LOCAL at its live endpoint — never a stored one (the same rule
            # as the dispatch lever). `local:<model>` with nothing serving it
            # stays local with no endpoint, and the gateway says "nothing on
            # your network serves this" instead of dialing.
            try:
                from mastercode.model_backends import match_local_model
                hit = await match_local_model(pool, entity_id, bare)
            except Exception:  # noqa: BLE001
                hit = None
            if hit:
                provider, base_url = "local", hit["endpoint"]
        return {"provider": provider, "model": eff_model, "base_url": base_url,
                "fallback_provider": None, "fallback_model": None,
                # The person's reasoning-effort setting is theirs, not the
                # type's: it rides through unchanged.
                "effort": (chat_route or {}).get("effort"),
                "turn_type_model": True}
    except Exception as e:  # noqa: BLE001
        log.warning("turn type %r model route failed (%s); chat route kept",
                    (turn_type or {}).get("name"), e)
        return chat_route


# ── Understanding the turn ────────────────────────────
#
# The platform decides which type a message is BEFORE the turn, in software,
# with the scorer the pre-turn pick already uses for tool groups
# (prefire_tools: whole-word hits, strong 1 / everyday ½, a load threshold)
# run over each ACTIVE type's own words. Bound first (a typed thread or
# project is its type), scored second (a clear winner moves the message to
# that type's thread), held third (below the threshold the message stays
# where the person is). A dropdown pick is the person's own act; when the
# scorer would have chosen differently, the message's words are added to the
# picked type's keywords — the classifier learns from correction, in
# software. No model call, no embedding, no second pass.

KEYWORD_CAP = 400          # a type's keyword list never grows past this
CORRECTION_WORDS_CAP = 12  # words one correction may add


def _type_words(tt: dict) -> tuple[set[str], set[str]]:
    """(keywords, strong names) for one type. Keywords score like
    GROUP_KEYWORDS; the type's NAME (its words), its skill name and its key
    names (whole, as the tokenizer keeps them — `stripe-live` is one token,
    never `live`) are strong words: a person who typed them meant them. An
    everyday or stop word never becomes strong through a name (a key
    `stripe-live` must not make "live" a strong hit)."""
    from mastercode.runtime.prefire_tools import WEAK_WORDS, _STOP, _tokens
    kws = {str(k).strip().lower() for k in (tt.get("keywords") or []) if str(k).strip()}
    strong: set[str] = set()
    strong |= _tokens(str(tt.get("name") or ""))
    for whole in ([tt.get("skill") or ""] + list(tt.get("keys") or [])):
        w = str(whole).strip().lower()
        if w:
            strong.add(w)
    return kws, {w for w in strong if w not in WEAK_WORDS and w not in _STOP}


def classify(message: str, types: list[dict], *, trail_tools: list[str] | None = None,
             tool_groups: dict[str, str] | None = None) -> dict | None:
    """The ACTIVE type this message is, or None (held where it is).

    Per type: each keyword found as a whole token scores 1 (½ for an
    everyday word, prefire_tools.WEAK_WORDS); each strong name found scores
    STRONG_HIT; the thread's recent tool calls add ½ per tool whose group
    the type holds, at most 1 (a thread continues what it was doing). The
    highest type at or above LOAD_THRESHOLD wins; a tie at the top is no
    winner — the platform never guesses between two types."""
    from mastercode.runtime.prefire_tools import (
        LOAD_THRESHOLD, STRONG_HIT, WEAK_HIT, WEAK_WORDS, _tokens)
    toks = _tokens(message or "")
    if not toks and not trail_tools:
        return None
    scored: list[tuple[float, str, dict]] = []
    for tt in types or []:
        if tt.get("status") != "active":
            continue
        kws, strong = _type_words(tt)
        score = sum(WEAK_HIT if w in WEAK_WORDS else STRONG_HIT for w in toks & kws)
        score += STRONG_HIT * len(toks & strong)
        if trail_tools and tool_groups:
            held = set(tt.get("tool_groups") or [])
            trail = sum(WEAK_HIT for n in trail_tools if tool_groups.get(n) in held)
            score += min(trail, 1.0)
        if score > 0:
            scored.append((score, str(tt.get("name") or ""), tt))
    if not scored:
        return None
    scored.sort(key=lambda s: (-s[0], s[1]))
    best_score, _, best = scored[0]
    if best_score < LOAD_THRESHOLD:
        return None
    if len(scored) > 1 and scored[1][0] == best_score:
        return None
    return {"type": best, "score": best_score}


async def active_types(pool, entity_id: int) -> list[dict]:
    """Every ACTIVE type of this entity, in name order — the dropdown's list
    and the classifier's candidates."""
    if pool is None or not entity_id:
        return []
    try:
        rows = await pool.fetch(
            f"SELECT {_TYPE_COLUMNS} FROM turn_types "
            "WHERE entity_id = $1 AND status = 'active' ORDER BY lower(name)",
            int(entity_id))
    except Exception as e:  # noqa: BLE001
        log.warning("turn_types.active_types failed (eid=%s): %s", entity_id, e)
        return []
    return [_row_to_type(r) for r in rows or []]


async def ensure_type_thread(pool, entity_id: int, tt: dict, *, reopen: bool = False) -> str | None:
    """The type's one thread, created on first use: a
    circles_threads row titled with the type's name, bound both ways. None
    when it cannot be made — the caller then holds the message where it is.

    A thread the person CLOSED is not a place to land:
    the scorer holds the message instead; a dropdown pick of that type
    (`reopen=True`) is the person asking to talk there again and reopens it.

    Two sends creating the thread at once: the second `UPDATE … WHERE
    thread_id IS NULL` writes nothing, so the loser re-reads the winner,
    deletes its own row (no turn was written into it yet) and lands in the
    winner's thread — one thread per type, never an orphan."""
    if tt.get("thread_id"):
        try:
            row = await pool.fetchrow(
                "SELECT closed_at, deleted_at FROM circles_threads WHERE id = $1::uuid AND entity_id = $2",
                str(tt["thread_id"]), int(entity_id))
        except Exception as e:  # noqa: BLE001
            log.warning("turn_types.ensure_type_thread: closed check failed (type=%s): %s", tt.get("id"), e)
            return None
        if row is not None and row["deleted_at"] is not None:
            # The person DELETED the type's thread (circles_threads.delete_thread):
            # it is gone, never reopened. The pointer is cleared and the type
            # gets a new thread below, as on its first use.
            try:
                await pool.execute(
                    "UPDATE turn_types SET thread_id = NULL, updated_at = now() "
                    "WHERE id = $1::uuid AND entity_id = $2 AND thread_id = $3::uuid",
                    str(tt["id"]), int(entity_id), str(tt["thread_id"]))
            except Exception as e:  # noqa: BLE001
                log.warning("turn_types.ensure_type_thread: clearing a deleted thread failed "
                            "(type=%s): %s", tt.get("id"), e)
                return None
            tt = {**tt, "thread_id": None}
    if tt.get("thread_id"):
        closed_at = row["closed_at"] if row is not None else None
        if closed_at is not None:
            if not reopen:
                return None
            try:
                await pool.execute(
                    "UPDATE circles_threads SET closed_at = NULL WHERE id = $1::uuid AND entity_id = $2",
                    str(tt["thread_id"]), int(entity_id))
            except Exception as e:  # noqa: BLE001
                log.warning("turn_types.ensure_type_thread: reopen failed (type=%s): %s", tt.get("id"), e)
                return None
        return str(tt["thread_id"])
    try:
        from mastercode import circles_threads as _ct
        # No title: the list names an untitled type thread by its TYPE, which
        # stays current when the type is renamed; a person's own rename sets
        # the title and then wins.
        row = await _ct.create_thread(pool, int(entity_id), title=None)
        thread_id = str(row["id"])
        # BIND FIRST. session.py reads a turn's type from the thread's
        # binding, so the new thread is typed from its first turn even if
        # the type's own pointer below never lands (a failure between the
        # two statements used to leave the type's official thread untyped
        # for good). A loser's bind goes with its
        # row when the row is deleted.
        await pool.execute(
            "UPDATE circles_threads SET type_id = $1::uuid WHERE id = $2::uuid AND entity_id = $3",
            str(tt["id"]), thread_id, int(entity_id))
        await pool.execute(
            "UPDATE turn_types SET thread_id = $1::uuid, updated_at = now() "
            "WHERE id = $2::uuid AND entity_id = $3 AND thread_id IS NULL",
            thread_id, str(tt["id"]), int(entity_id))
        winner = await pool.fetchval(
            "SELECT thread_id FROM turn_types WHERE id = $1::uuid AND entity_id = $2",
            str(tt["id"]), int(entity_id))
        winner = str(winner) if winner else thread_id
        if winner != thread_id:
            await pool.execute(
                "DELETE FROM circles_threads WHERE id = $1::uuid AND entity_id = $2",
                thread_id, int(entity_id))
            tt["thread_id"] = winner
            return winner
        tt["thread_id"] = thread_id
        return thread_id
    except Exception as e:  # noqa: BLE001
        log.warning("turn_types.ensure_type_thread failed (type=%s): %s", tt.get("id"), e)
        return None


# Words a correction never teaches. A keyword classifier's
# standard tool is a standard stop list: the Glasgow IR list, as scikit-learn
# ships it (runtime/stop_words.py). On top of it, only what a CHAT surface
# adds and that list lacks: greetings and acknowledgements, and the stub the
# send route writes for an attachment-only message ("(sent an attachment)").
from mastercode.runtime.stop_words import ENGLISH_STOP_WORDS as _ENGLISH_STOP_WORDS

CHAT_NEVER_TEACH: frozenset[str] = frozenset({
    "sent", "attachment", "attachments",
    "hello", "hi", "hey", "thanks", "thank", "please", "ok", "okay", "yes", "sure",
})

NEVER_TEACH: frozenset[str] = _ENGLISH_STOP_WORDS | CHAT_NEVER_TEACH


def teachable_words(message: str, *, others: list[dict] | None = None,
                    cap: int | None = CORRECTION_WORDS_CAP) -> list[str]:
    """The words a correction may teach, in the order the person wrote them:
    real tokens (stop words out), not everyday words, not in NEVER_TEACH,
    at least three characters, and not already a keyword or strong word of
    ANOTHER active type — a word two types share tells nothing apart. At
    most `cap` per correction."""
    from mastercode.runtime.prefire_tools import WEAK_WORDS
    import re as _re
    shared: set[str] = set()
    for o in others or []:
        kws, strong = _type_words(o)
        shared |= kws | strong
    seen: set[str] = set()
    out: list[str] = []
    for raw in _re.findall(r"[a-z0-9][a-z0-9_.\-']*", (message or "").lower()):
        w = raw.strip(".-'")
        # A contraction ("i'm", "how's", "month's") is never a keyword, and
        # neither is anything with a digit in it ("3pm", "q3", "2026").
        if (len(w) < 3 or "'" in w or any(ch.isdigit() for ch in w) or w in seen
                or w in WEAK_WORDS or w in NEVER_TEACH or w in shared):
            continue
        from mastercode.runtime.prefire_tools import _STOP
        if w in _STOP:
            continue
        seen.add(w)
        out.append(w)
        if cap is not None and len(out) >= cap:
            break
    return out


async def add_keywords(pool, entity_id: int, type_id: str, message: str,
                       *, cap: int = CORRECTION_WORDS_CAP,
                       others: list[dict] | None = None) -> list[str]:
    """Learn from a correction: the message's teachable words (see
    teachable_words) join the type's keywords, in the order written. The
    Host-seeded words are never pushed out: at KEYWORD_CAP nothing more is
    learned. A row write, the person's own act — no approval card. Returns
    the words added."""
    # The per-correction cap counts words actually ADDED, so it is applied
    # after the dedupe against what the type already has.
    words = teachable_words(message, others=others, cap=None)
    if not words or pool is None:
        return []
    try:
        current = await pool.fetchval(
            "SELECT keywords FROM turn_types WHERE id = $1::uuid AND entity_id = $2",
            str(type_id), int(entity_id))
        have = [str(k) for k in (current or [])]
        room = max(0, min(cap, KEYWORD_CAP - len(have)))
        new = [w for w in words if w not in set(have)][:room]
        if not new:
            return []
        await pool.execute(
            "UPDATE turn_types SET keywords = $1::text[], updated_at = now() "
            "WHERE id = $2::uuid AND entity_id = $3",
            have + new, str(type_id), int(entity_id))
        return new
    except Exception as e:  # noqa: BLE001
        log.warning("turn_types.add_keywords failed (type=%s): %s", type_id, e)
        return []


async def resolve_thread_for_message(pool, entity_id: int, thread_id: str | None, message: str,
                                     *, selected_type_id: str | None = None,
                                     trail_tools: list[str] | None = None,
                                     tool_groups: dict[str, str] | None = None) -> dict:
    """WHERE a self-chat message lands, decided before the turn:

        {"thread_id": <uuid or None>, "type": <dict or None>,
         "moved": bool, "reason": "selected" | "bound" | "scored" | "held"}

    1. A dropdown pick names the type: its thread, made on first use. When
       the scorer would have chosen differently, the message's words are
       added to the picked type's keywords (correction).
    2. The thread the person is in is bound to a type: it stays.
    3. Otherwise the scorer picks among the active types; a clear winner
       moves the message to that type's thread.
    4. Otherwise it stays.
    A failure anywhere holds the message where it is: a send never fails
    because the classifier could not run."""
    held = {"thread_id": thread_id, "type": None, "moved": False, "reason": "held"}
    try:
        types = await active_types(pool, entity_id)
        if not types:
            return held
        by_id = {str(t["id"]): t for t in types}
        # The Host's root thread (runtime/root_turn) is its own: a message
        # written there stays there, and a pick never moves it out.
        if thread_id:
            from mastercode.runtime import root_turn as _root
            if await _root.is_root_thread(pool, entity_id, thread_id):
                return {**held, "reason": "root"}
        # 2 (read first so a selected type equal to the bound one is a no-op).
        bound = await load_turn_type(pool, entity_id, thread_id=thread_id) if thread_id else None
        if bound is not None and bound.get("status") == "closed":
            return held
        # 1.
        if selected_type_id and str(selected_type_id) in by_id:
            picked = by_id[str(selected_type_id)]
            if bound is not None and str(bound.get("id")) == str(picked["id"]):
                return {"thread_id": thread_id, "type": bound, "moved": False, "reason": "bound"}
            # A pick is the person asking to talk there: a closed thread reopens.
            dest = await ensure_type_thread(pool, entity_id, picked, reopen=True)
            if not dest:
                return held
            would = classify(message, types, trail_tools=trail_tools, tool_groups=tool_groups)
            if would is None or str(would["type"].get("id")) != str(picked["id"]):
                added = await add_keywords(
                    pool, entity_id, str(picked["id"]), message,
                    others=[t for t in types if str(t["id"]) != str(picked["id"])])
                if added:
                    log.info("turn type %r learned %d word(s) from a correction",
                             picked.get("name"), len(added))
            return {"thread_id": dest, "type": picked, "moved": dest != thread_id, "reason": "selected"}
        if bound is not None:
            return {"thread_id": thread_id, "type": bound, "moved": False, "reason": "bound"}
        # 3.
        win = classify(message, types, trail_tools=trail_tools, tool_groups=tool_groups)
        if win is None:
            return held
        # The scorer never lands in a thread the person closed (held).
        dest = await ensure_type_thread(pool, entity_id, win["type"], reopen=False)
        if not dest:
            return held
        return {"thread_id": dest, "type": win["type"], "moved": dest != thread_id, "reason": "scored"}
    except Exception as e:  # noqa: BLE001
        log.warning("turn_types.resolve_thread_for_message failed (eid=%s): %s — message held",
                    entity_id, e)
        return held


def bound_groups(turn_type: dict | None, tool_groups: dict[str, str]) -> dict[str, str]:
    """{tool: group} narrowed to the type's groups — what session.py hands
    the pre-turn pick and keeps as the loadable set, so a failed pick falls
    back to the type, never to everything. Untyped: unchanged."""
    if turn_type is None:
        return dict(tool_groups)
    allowed = set(turn_type.get("tool_groups") or [])
    return {n: g for n, g in tool_groups.items() if g in allowed}


__all__ = [
    "ALWAYS_ALLOWED", "TYPE_KEY_ARGS", "TYPE_KEY_DEFAULTS", "TYPE_SERVER_ARGS",
    "TYPE_SERVER_LIST_ARGS", "AUTOFILL_TOOL", "CONTEXT_MEMORY_SCOPES", "CLOSED_CONTEXT",
    "context_filter", "type_skill", "route_for_type", "thread_model_pick",
    "classify", "active_types", "ensure_type_thread", "add_keywords", "teachable_words",
    "NEVER_TEACH", "resolve_thread_for_message", "KEYWORD_CAP", "CORRECTION_WORDS_CAP",
    "AUTOFILL_KEY_PREFIX", "DENIAL_REASON", "closed_type", "load_turn_type",
    "grant_bits_for", "tool_category", "check_call", "resolve_targets", "RESOLVED_DOORS", "current_server_scope", "holders_of", "bound_groups",
    "load_turn_type_by_id", "stamp_scheduled_work", "resolve_session_type",
]


# ── The turn type of the work running now ──────────────────────
# Set where a turn's type is known — the session once it resolves the type,
# the offline runner for a scheduled op, and the Ship route for an app check
# it starts outside any turn (from the project's binding) — and read where
# work is RECORDED: an entity_tasks row stamps it
# (v186), so the timeline can say which type an agent or build runs under.
# A ContextVar, so a background task the turn starts carries the type it was
# started under, and two turns running side by side never see each other's.
import contextvars as _contextvars

CURRENT_TURN_TYPE: "_contextvars.ContextVar[dict | None]" = _contextvars.ContextVar(
    "current_turn_type", default=None)


def set_current_turn_type(turn_type: dict | None):
    """Record the type this execution context runs under; returns the token
    ContextVar.reset takes."""
    return CURRENT_TURN_TYPE.set(turn_type if isinstance(turn_type, dict) else None)


def current_turn_type_id() -> str | None:
    """The id of the ACTIVE type this context runs under, else None (untyped,
    or a closed turn, which has no type to name)."""
    t = CURRENT_TURN_TYPE.get()
    if not isinstance(t, dict) or t.get("status") != "active" or not t.get("id"):
        return None
    return str(t["id"])


def current_server_scope() -> set[str] | None:
    """The servers the work running now may reach, for code that picks a
    server itself rather than being handed one: None when untyped (any
    server), else the type's bastions, where '*' lets the pick take any
    server; a closed type holds none."""
    t = CURRENT_TURN_TYPE.get()
    if not isinstance(t, dict):
        return None
    if t.get("status") == "closed":
        return set()
    return {str(s) for s in (t.get("bastions") or [])}
