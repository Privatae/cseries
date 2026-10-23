#!/usr/bin/env python3
"""
Turn types: the tool gate.

Structural verification that a Host turn bound to a TURN TYPE cannot reach
outside it, and — asserted FIRST — that a turn with no type is byte-for-byte
the Host of today. Container-free: APP_ROOT=<repo> python3 scripts/<this>.
No pod, no pool, no model; the capability gate and the agent runner are
driven with the same stubs the grant-wall gate uses.

  1. untyped is unchanged: check_call(None) never denies, grant_bits_for(None)
     is None, bound_groups(None) is identity, the gate authorizes a call with
     turn_type=None exactly as before and stamps nothing new on its audit
     row, an unbound thread costs ONE binding read and no type read, and no
     thread and no project costs no read at all
  2. a tool outside the type's groups is refused at the GATE with
     reason=type_denied — the schema was never sent and it does not matter
  3. the same refusal through `operate` (run_direct_op → _execute_meta_tool)
  4. the primitives that reach the gate (orient, escalate) pass the group
     check in any type
  5. keys: every credential-name door refuses a name not in the type; a door
     that defaults the name when omitted is checked against the default;
     autofill refuses a host not listed as autofill:<host>
  6. bastions: EVERY registry argument named `server` is a checked door
     (swept from the registry, not a hand list), and each refuses a server
     not in the type
 6b. doors whose arguments do not name what they reach, through the gate:
     a bastion addressed by token id is checked by the server behind the id
     (even beside a listed `server`), an id with no server needs '*',
     enrolling a new server needs '*', and a browser agent run or resume
     needs autofill:<host> for every host the agent signs in on; EVERY
     registry argument named `token_id` is a checked door. The same for a
     key or server a saved record names: a compute connection, an
     integration, the github sign-in, a harness mirror's push token, a dcn
     store or plan, a project whose code lives on a machine (build, ship,
     write, publish, adopt), a management grant, a local bench, a ship run
     and a backup target; and code that picks a machine itself (context
     extraction) picks only a listed one. Each refusal is authorized on main.
     The reads take every shape the door does: an adopt's run named by
     `run`, a project_id the door fills in from the thread (0 included), a
     pipeline sent as JSON text, a store whatever its entity_id column, and
     a model served on more than one server.
  7. an in-type call passes the type wall and reaches the bits check
  8. a type's grant is the seven names; admin / shell_exec contribute nothing
  9. effective = entity ∩ type: a tool needing a bit the type lacks is
     refused by the capability wall, worded to a typed Host about its type;
     an agent inside a typed turn is still told to report to its host
 10. a bound type that cannot be read runs CLOSED — everything refused, and
     the refusal says what could not be read (binding vs type), never
     claiming a binding that was not read; a binding to a draft is untyped
 11. the refusal names the active types that hold the item (holders)
 12. an agent dispatched from a typed turn is capped at the turn: the
     out-of-type tool is stripped, the reason names the TYPE (not code-exec),
     and the type rides into the agent's own tool calls
 13. session.py hands the type to each exec path by name (escalate, operate,
     host doors, read-safe tools, dispatch) and to the pick, the orient
     open, the fallback and the catalog
 14. the door tables name real tools with real argument names

Exit 0 = all pass; exit 1 + [turn-types] FAIL lines otherwise.
"""

import asyncio
import os
from turn_source import turn_source  # session.py and the modules split out of it
os.environ.setdefault("CSERIES_OPERATOR_ENTITY_ID", "9003")  # the operator Host in these cases
import re
import sys

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[turn-types] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class _Pool:
    """Records every read so a test can assert how many the code made. The
    holders lookup and the binding/type reads answer from small tables."""
    def __init__(self, holders_rows=None, binding=None, type_row=None):
        self._holders = holders_rows or []
        self._binding = binding          # what circles_threads/projects.type_id reads as
        self._type_row = type_row        # what turn_types reads as
        self.executed = []
        self.reads = []                  # (method, sql)

    async def execute(self, sql, *a, **k):
        # SQL and its parameters: the audit row's `extra` travels as a JSON
        # parameter, not in the SQL text, so a parity check must read both.
        self.executed.append((sql, [repr(x) for x in a]))
        return None

    async def fetch(self, sql, *a, **k):
        self.reads.append(("fetch", sql))
        if "FROM turn_types" in sql:
            return list(self._holders)
        return []

    async def fetchval(self, sql, *a, **k):
        self.reads.append(("fetchval", sql))
        if "type_id FROM circles_threads" in sql or "type_id FROM projects" in sql:
            return self._binding
        if "hostname FROM network_bastion_heartbeat WHERE token_id" in sql:
            return (getattr(self, "bastion_tokens", None) or {}).get(a[0])
        return None

    async def fetchrow(self, sql, *a, **k):
        self.reads.append(("fetchrow", sql))
        if "FROM turn_types WHERE id" in sql:
            return self._type_row
        if "FROM mcp_client_tokens WHERE id" in sql and a and a[0] in (
                getattr(self, "bastion_tokens", None) or {}):
            return {"id": a[0], "notes": "{}"}
        # A door's own record, answered by the test: (sql fragment, fn(params)).
        for frag, fn in getattr(self, "rows", None) or []:
            if frag in sql:
                return fn(a)
        return None


class _BindingRaisesPool(_Pool):
    async def fetchval(self, sql, *a, **k): raise RuntimeError("store unreadable")


class _TypeRaisesPool(_Pool):
    async def fetchrow(self, sql, *a, **k): raise RuntimeError("type row unreadable")



def _type(**over):
    base = {
        "id": "00000000-0000-0000-0000-000000000001", "name": "Finance",
        "status": "active", "keywords": ["invoice"], "context": {},
        "skill": "finance", "tool_groups": ["vault", "comms"],
        "grant_names": ["read_context", "write_context", "external_api"],
        "keys": ["stripe-live", "autofill:bank.example"],
        "bastions": ["books-1"], "model": None, "thread_id": None, "version": 1,
    }
    base.update(over)
    return base


def _schema(name):
    return {"type": "function",
            "function": {"name": name, "description": name,
                         "parameters": {"type": "object", "properties": {}}}}


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "brain"))
    # The gate reads a token door's server and a browser agent's hosts
    # through the server package (turn_types.resolve_targets).
    sys.path.insert(1, app_root)

    from mastercode.runtime import turn_types as tt
    from mastercode.runtime import tools as tools_mod
    from mastercode.runtime import meta_tool_caps as caps
    from mastercode.runtime.agent_grant import CAP_DENIAL_KEY, NAME_TO_BIT
    # ── Stubs: full entity scopes. Kill switch is the real file check
    # (absent on a build box).
    async def _all_scopes(pool, entity_id, **kw):
        return set(caps.ENTITY_SCOPE_TO_BITS.keys())
    caps._resolve_entity_scopes = _all_scopes

    auth = tools_mod._authorize_meta_tool
    pool = _Pool(holders_rows=[
        {"name": "Ops", "tool_groups": ["network", "system"], "keys": [],
         "bastions": ["web-1"]},
        {"name": "Finance", "tool_groups": ["vault", "comms"],
         "keys": ["stripe-live"], "bastions": ["books-1"]},
    ])
    THREAD = "11111111-1111-1111-1111-111111111111"

    # ── 1. UNTYPED IS UNCHANGED — first, because it is the promise the
    # design makes to every Host whose user has built no type.
    check("untyped: check_call never denies",
          all(tt.check_call(None, n, {"name": "x", "server": "y", "host": "z"}) is None
              for n in ("keys_manage", "mgmt_run", "autofill", "local_grep", "nonsense")))
    check("untyped: grant_bits_for is None (entity bits kept)",
          tt.grant_bits_for(None) is None)
    groups = {"local_grep": "codespace", "mgmt_run": "network", "keys_manage": "vault"}
    check("untyped: bound_groups is identity", tt.bound_groups(None, groups) == groups)
    ok, denial = asyncio.run(auth("local_grep", {"pattern": "x"}, 1, pool, None))
    check("untyped: gate authorizes as before (no turn_type kwarg needed)",
          ok is True and denial is None, f"got ok={ok} denial={denial}")
    p0 = _Pool()
    ok, denial = asyncio.run(auth("local_grep", {"pattern": "x"}, 1, p0, None,
                                  turn_type=None))
    check("untyped: gate authorizes with turn_type=None",
          ok is True and denial is None, f"got ok={ok} denial={denial}")
    # The gate writes no audit row of its own: the call is recorded once, on
    # tool_call_log, by the runtime that made it (the behavioral log that
    # carried a gate row is gone, migrations/v237_dead_tables.sql).
    check("untyped: the gate writes no audit row (tool_call_log is the record)",
          not any("INSERT" in sql for sql, _ in p0.executed), f"{p0.executed[:1]}")
    pT = _Pool()
    asyncio.run(auth("keys_manage", {"op": "list"}, 1, pT, None, turn_type=_type()))
    check("typed: the gate writes no audit row either",
          not any("INSERT" in sql for sql, _ in pT.executed), f"{pT.executed[:1]}")
    p1 = _Pool(binding=None)
    r = asyncio.run(tt.load_turn_type(p1, 7, thread_id=THREAD))
    check("untyped: an unbound thread is untyped after ONE binding read and no type read",
          r is None and [m for m, _ in p1.reads] == ["fetchval"], f"reads={p1.reads}")
    p2 = _Pool(binding=None)
    r = asyncio.run(tt.load_turn_type(p2, 7, thread_id=THREAD, project_id=5))
    check("untyped: an unbound thread in an unbound project reads two bindings, no type",
          r is None and [m for m, _ in p2.reads] == ["fetchval", "fetchval"], f"reads={p2.reads}")
    p3 = _Pool()
    r = asyncio.run(tt.load_turn_type(p3, 7, thread_id=None, project_id=None))
    check("untyped: no thread and no project makes no read at all",
          r is None and p3.reads == [], f"reads={p3.reads}")

    # ── 2. A tool outside the type's groups is refused at the gate.
    fin = _type()
    ok, denial = asyncio.run(auth("local_grep", {"pattern": "x"}, 1, pool, None,
                                  turn_type=fin))
    check("typed: out-of-group tool refused at the gate",
          ok is False and isinstance(denial, dict)
          and denial.get("reason") == tt.DENIAL_REASON
          and denial.get(CAP_DENIAL_KEY) == 0,
          f"got ok={ok} denial={denial}")
    check("typed: refusal names the type and what was outside",
          isinstance(denial, dict) and denial.get("turn_type") == "Finance"
          and "local_grep" in str(denial.get("outside")))

    # ── 3. The same refusal through `operate`.
    from mastercode.runtime.direct_ops import run_direct_op, DIRECT_OP_TOOLS
    op_tool = next((n for n in sorted(DIRECT_OP_TOOLS)
                    if tt.tool_category(n) not in fin["tool_groups"]
                    and n not in tt.ALWAYS_ALLOWED), None)
    check("operate: a registry op outside the type exists to test with",
          op_tool is not None, "DIRECT_OP_TOOLS has no tool outside vault/comms")
    if op_tool:
        res = asyncio.run(run_direct_op(op_tool, {}, entity_id=1, pool=pool,
                                        turn_type=fin))
        check("operate: out-of-type op refused with type_denied",
              isinstance(res, dict) and res.get("reason") == tt.DENIAL_REASON,
              f"{op_tool} → {res}")

    # ── 4. The primitives that reach the gate pass the group check.
    for prim in ("escalate",):
        check(f"primitive {prim} passes the group check",
              tt.check_call(_type(tool_groups=[]), prim, {}) is None)

    # ── 5. Keys.
    for door, arg in tt.TYPE_KEY_ARGS.items():
        d = tt.check_call(_type(tool_groups=[tt.tool_category(door)]), door,
                          {"op": "use", arg: "not-mine"})
        check(f"key door {door}: name outside the type refused",
              isinstance(d, dict) and d.get("kind") == "key" and d.get("item") == "not-mine",
              f"got {d}")
        d = tt.check_call(_type(tool_groups=[tt.tool_category(door)]), door,
                          {"op": "use", arg: "stripe-live"})
        check(f"key door {door}: listed name passes", d is None, f"got {d}")
    # Bypass by omission: the handler's default is the credential in use.
    d = tt.check_call(_type(tool_groups=["memory"]), "context_backend",
                      {"op": "set", "backend": "s3", "bucket": "b"})
    check("context_backend set backend=s3 with cred_name omitted is checked as 'ctx:s3'",
          isinstance(d, dict) and d.get("item") == "ctx:s3", f"got {d}")
    d = tt.check_call(_type(tool_groups=["memory"], keys=["ctx:s3"]), "context_backend",
                      {"op": "set", "backend": "s3", "bucket": "b"})
    check("context_backend set backend=s3 passes when the type lists 'ctx:s3'",
          d is None, f"got {d}")
    d = tt.check_call(_type(tool_groups=["memory"]), "context_backend", {"op": "status"})
    check("context_backend status names no credential and passes", d is None, f"got {d}")
    d = tt.check_call(_type(tool_groups=["context"]), "dcn",
                      {"op": "propose_store", "class": "cabinet", "bucket": "b"})
    check("dcn propose_store class=cabinet with cred_name omitted is checked as 'ctx:s3'",
          isinstance(d, dict) and d.get("item") == "ctx:s3", f"got {d}")
    d = tt.check_call(_type(tool_groups=["context"], bastions=["books-1"]), "dcn",
                      {"op": "propose_store", "class": "cabinet", "bucket": "b", "server": "books-1"})
    check("dcn propose_store class=cabinet beside a listed server is still checked as 'ctx:s3'",
          isinstance(d, dict) and d.get("item") == "ctx:s3", f"got {d}")
    d = tt.check_call(_type(tool_groups=["context"], bastions=["books-1"]), "dcn",
                      {"op": "propose_store", "class": "machine", "server": "books-1", "root": "/srv"})
    check("dcn propose_store class=machine names a server, not a credential",
          d is None, f"got {d}")
    d = tt.check_call(_type(tool_groups=["context"]), "dcn",
                      {"op": "propose_store", "class": "machine", "root": "/srv"})
    check("dcn propose_store class=machine with no server is not a key refusal",
          d is None, f"got {d}")
    # Replica targets are servers too; an unnamed fan-out needs '*'.
    d = tt.check_call(_type(tool_groups=["memory"], keys=["ctx:s3"]), "context_backend",
                      {"op": "set", "backend": "s3", "availability": "replicate",
                       "replicas": ["books-1", "web-9"]})
    check("context_backend replicas: a replica outside the type is refused",
          isinstance(d, dict) and d.get("kind") == "server" and d.get("item") == "web-9", f"got {d}")
    d = tt.check_call(_type(tool_groups=["memory"], keys=["ctx:s3"]), "context_backend",
                      {"op": "set", "backend": "s3", "availability": "replicate",
                       "replicas": ["books-1"]})
    check("context_backend replicas: listed replicas pass", d is None, f"got {d}")
    d = tt.check_call(_type(tool_groups=["memory"], keys=["ctx:s3"]), "context_backend",
                      {"op": "set", "backend": "s3", "availability": "replicate"})
    check("context_backend replicate with no replicas is refused unless the type holds '*'",
          isinstance(d, dict) and d.get("item") == "*", f"got {d}")
    d = tt.check_call(_type(tool_groups=["memory"], keys=["ctx:s3"], bastions=["*"]),
                      "context_backend",
                      {"op": "set", "backend": "s3", "availability": "replicate"})
    check("context_backend replicate with no replicas passes when the type holds '*'",
          d is None, f"got {d}")
    d = tt.check_call(_type(tool_groups=["vault"]), "autofill",
                      {"op": "fill", "host": "other.example", "session": "s", "mapping": []})
    check("autofill: host not listed refused",
          isinstance(d, dict) and d.get("item") == "autofill:other.example", f"got {d}")
    d = tt.check_call(_type(tool_groups=["vault"]), "autofill",
                      {"op": "fill", "host": "bank.example", "session": "s", "mapping": []})
    check("autofill: listed host passes", d is None, f"got {d}")
    d = tt.check_call(_type(tool_groups=["vault"]), "keys_manage", {"op": "list"})
    check("keys_manage op=list names nothing and passes", d is None, f"got {d}")

    # ── 6. Bastions: swept from the registry, not a hand list.
    from mastercode.public_tool_registry import (
        OPERATOR_PLATFORM_TOOL_SCHEMAS, PUBLIC_TOOL_SCHEMAS)
    all_schemas = {**PUBLIC_TOOL_SCHEMAS, **OPERATOR_PLATFORM_TOOL_SCHEMAS}
    by_name = {(s.get("function") or {}).get("name"): s for s in all_schemas.values()}
    server_doors = sorted(
        n for n, s in by_name.items()
        if "server" in ((s.get("function") or {}).get("parameters") or {}).get("properties", {}))
    missing = [n for n in server_doors if tt.TYPE_SERVER_ARGS.get(n) != "server"]
    check("bastions: every registry argument named `server` is a checked door",
          not missing, f"unchecked: {missing}")
    # The server argument alone: a door that also reads what it reaches is
    # handed a read that found nothing more (6b tests those reads).
    nothing_more = {"reach": {"servers": [], "keys": []}}
    for door in tt.TYPE_SERVER_ARGS:
        cat = tt.tool_category(door)
        d = tt.check_call(_type(tool_groups=[cat]), door, {"server": "web-1", "op": "uptime"},
                          targets=nothing_more)
        check(f"bastion door {door}: server outside the type refused",
              isinstance(d, dict) and d.get("kind") == "server" and d.get("item") == "web-1",
              f"got {d}")
        d = tt.check_call(_type(tool_groups=[cat]), door, {"server": "books-1", "op": "uptime"},
                          targets=nothing_more)
        check(f"bastion door {door}: listed server passes", d is None, f"got {d}")

    # ── 6b. Doors whose arguments do not name what they reach. Driven
    # through the GATE first, with the call shapes a typed turn could send
    # before the wall read the target (token 7 is web-1, outside the type;
    # 8 is books-1, inside it; 9 has never reported a hostname).
    tok_pool = _Pool()
    tok_pool.bastion_tokens = {7: "web-1", 8: "books-1", 9: ""}
    net = _type(tool_groups=sorted({tt.tool_category("quarantine_bastion"),
                                    tt.tool_category("manage_bastion")}))
    for args, why in (
            ({"op": "quarantine", "token_id": 7}, "an unlisted box by token id"),
            ({"op": "quarantine", "server": "books-1", "token_id": 7},
             "a listed server name beside an unlisted box's token id"),
            ({"op": "quarantine", "token_id": 9}, "a token with no server behind it"),
    ):
        ok, denial = asyncio.run(auth("quarantine_bastion", args, 1, tok_pool, None,
                                      turn_type=net))
        check(f"gate: quarantine_bastion refuses {why}",
              ok is False and (denial or {}).get("reason") == tt.DENIAL_REASON
              and (denial or {}).get("kind") == "server", f"got ok={ok} denial={denial}")
    ok, denial = asyncio.run(auth("quarantine_bastion", {"op": "quarantine", "token_id": 8},
                                  1, tok_pool, None, turn_type=net))
    check("gate: quarantine_bastion is authorized for a listed box's token id",
          ok is True, f"got ok={ok} denial={denial}")
    for args, why in (
            ({"op": "revoke", "token_id": 7}, "revoking an unlisted box"),
            ({"op": "update", "token_id": 7, "version": "0.31.2"}, "updating an unlisted box"),
            ({"op": "enroll", "name": "new-box"}, "enrolling a server the type cannot list"),
    ):
        ok, denial = asyncio.run(auth("manage_bastion", args, 1, tok_pool, None, turn_type=net))
        check(f"gate: manage_bastion refuses {why}",
              ok is False and (denial or {}).get("reason") == tt.DENIAL_REASON,
              f"got ok={ok} denial={denial}")
    for args in ({"op": "list"}, {"op": "latest"}, {"op": "revoke", "token_id": 8}):
        ok, denial = asyncio.run(auth("manage_bastion", args, 1, tok_pool, None, turn_type=net))
        check(f"gate: manage_bastion {args} is authorized",
              ok is True, f"got ok={ok} denial={denial}")
    ok, denial = asyncio.run(auth("manage_bastion", {"op": "enroll", "name": "new-box"}, 1,
                                  tok_pool, None, turn_type=_type(tool_groups=net["tool_groups"],
                                                                  bastions=["*"])))
    check("gate: manage_bastion enroll is authorized for a type that holds '*'",
          ok is True, f"got ok={ok} denial={denial}")

    import tempfile
    from server import browser_agents as ba
    state_root = tempfile.mkdtemp(prefix="tt-agents-", dir=os.environ.get("TMPDIR") or None)
    ba.STATE_ROOT = type(ba.STATE_ROOT)(state_root)
    agent_dir = os.path.join(state_root, ba.AGENTS_SUBDIR, "bank-export")
    os.makedirs(agent_dir)
    with open(os.path.join(agent_dir, "agent.yaml"), "w", encoding="utf-8") as f:
        f.write("name: bank-export\nsite: bank.example\nallow: [login.other.example]\n"
                "steps:\n  - go: https://bank.example/\n"
                "  - type: {field: User ID, key: user-id}\n"
                "  - secret: {field: Password, key: password, host: login.other.example}\n"
                "  - click: {role: button, name: Sign On}\n")
    hosts_of = getattr(ba, "sign_in_hosts", lambda spec: None)
    check("browser agent: sign_in_hosts is the site and every host a sealed entry is typed on",
          hosts_of(ba.load_agent("bank-export") or {}) == ["bank.example", "login.other.example"],
          f"got {hosts_of(ba.load_agent('bank-export') or {})}")
    # The type holds the bits browser_agent needs, so only the wall can refuse.
    agent_bits = caps.META_TOOL_CAPS["browser_agent"]["required_bits"]
    apps = _type(tool_groups=[tt.tool_category("browser_agent")],
                 grant_names=sorted(n for n, b in NAME_TO_BIT.items() if b & agent_bits))
    for op in ("run", "resume"):
        args = {"op": op, "name": "bank-export", "answer": "123456"}
        ok, denial = asyncio.run(auth("browser_agent", args, 1, pool, None, turn_type=apps))
        check(f"gate: browser_agent {op} refuses an agent signing in on a host the type lacks",
              ok is False and (denial or {}).get("reason") == tt.DENIAL_REASON
              and (denial or {}).get("item") == "autofill:login.other.example",
              f"got ok={ok} denial={denial}")
        both = _type(tool_groups=apps["tool_groups"], grant_names=apps["grant_names"],
                     keys=["autofill:bank.example", "autofill:login.other.example"])
        ok, denial = asyncio.run(auth("browser_agent", args, 1, pool, None, turn_type=both))
        check(f"gate: browser_agent {op} is authorized when the type holds every sign-in host",
              ok is True, f"got ok={ok} denial={denial}")
    ok, denial = asyncio.run(auth("browser_agent", {"op": "run", "name": "no-such-agent"}, 1,
                                  pool, None, turn_type=apps))
    check("gate: browser_agent run of an agent whose file cannot be read is refused",
          ok is False and (denial or {}).get("reason") == tt.DENIAL_REASON,
          f"got ok={ok} denial={denial}")
    for op in ("list", "get", "runs"):
        ok, denial = asyncio.run(auth("browser_agent", {"op": op, "name": "bank-export"}, 1,
                                      pool, None, turn_type=apps))
        check(f"gate: browser_agent {op} signs in nowhere and is authorized",
              ok is True, f"got ok={ok} denial={denial}")
    # The restored login cookies are for every host the session may visit,
    # the allow hosts included, though no step types into them.
    sso_dir = os.path.join(state_root, ba.AGENTS_SUBDIR, "app-export")
    os.makedirs(sso_dir)
    with open(os.path.join(sso_dir, "agent.yaml"), "w", encoding="utf-8") as f:
        f.write("name: app-export\nsite: app.example\nallow: [sso.idp.example]\n"
                "steps:\n  - go: https://app.example/\n"
                "  - click: {role: button, name: Export}\n")
    app_only = _type(tool_groups=apps["tool_groups"], grant_names=apps["grant_names"],
                     keys=["autofill:app.example"])
    ok, denial = asyncio.run(auth("browser_agent", {"op": "run", "name": "app-export"}, 1,
                                  pool, None, turn_type=app_only))
    check("gate: browser_agent run refuses restoring a login on an allow host the type lacks",
          ok is False and (denial or {}).get("item") == "autofill:sso.idp.example",
          f"got ok={ok} denial={denial}")

    # Doors that open a key the call picks by a saved record, or that the
    # door fills in. Each type holds the tool's group and the bits it needs,
    # so only the wall can refuse; each refused call is authorized on main.
    def _typed_for(tool, **over):
        need = caps.META_TOOL_CAPS[tool]["required_bits"]
        return _type(tool_groups=[tt.tool_category(tool)],
                     grant_names=sorted(n for n, b in NAME_TO_BIT.items() if b & need), **over)

    def _gate(tool, args, typ, eid, p):
        return asyncio.run(auth(tool, args, eid, p or pool, None, turn_type=typ))

    def _refused(tool, args, typ, item, why, eid=1, p=None):
        ok, denial = _gate(tool, args, typ, eid, p)
        check(f"gate: {tool} refuses {why}",
              ok is False and (denial or {}).get("reason") == tt.DENIAL_REASON
              and (denial or {}).get("item") == item, f"got ok={ok} denial={denial}")

    def _authorized(tool, args, typ, why, eid=1, p=None):
        ok, denial = _gate(tool, args, typ, eid, p)
        check(f"gate: {tool} is authorized {why}", ok is True, f"got ok={ok} denial={denial}")

    cc_pool = _Pool()
    cc_pool.rows = [("behavioral_params FROM entity_registry", lambda a: {"behavioral_params": {
        "compute_connections": [{"id": "cmp_a", "kind": "runpod", "cred_name": "runpod:cmp_a"},
                                {"id": "cmp_b", "kind": "local"}]}})]
    for op in ("dispatch", "revoke", "assign_role"):
        args = {"op": op, "connection_id": "cmp_a", "input": {}, "role": "embedding"}
        _refused("connect_compute", args, _typed_for("connect_compute"), "runpod:cmp_a",
                 f"{op} on a connection whose key the type lacks", p=cc_pool)
        _authorized("connect_compute", args, _typed_for("connect_compute", keys=["runpod:cmp_a"]),
                    f"for {op} when the type holds the connection's key", p=cc_pool)
    _authorized("connect_compute", {"op": "revoke", "connection_id": "cmp_b"},
                _typed_for("connect_compute"), "on a connection with no key", p=cc_pool)
    _authorized("connect_compute", {"op": "list"}, _typed_for("connect_compute"),
                "for list, which opens nothing", p=cc_pool)

    _refused("credential_wiring", {"op": "set", "consumer": "harness:opencode",
                                   "keys_name": "openai-other"},
             _typed_for("credential_wiring"), "openai-other", "wiring a key the type lacks")

    from server.integrations.core import storage as int_storage
    saved = (int_storage.get_connection, int_storage.get_oauth_client,
             int_storage.list_oauth_clients)
    # Async, as the store is (it lives in the Host's Postgres).
    async def _get_connection(cid):
        return {"id": cid, "credentials_ref": "cred_c1"} if cid == "c1" else None

    async def _get_oauth_client(fam):
        return {"family": fam, "credentials_ref": "cred_g"} if fam == "google" else None

    async def _list_oauth_clients():
        return [{"family": "google", "credentials_ref": "cred_g"},
                {"family": "microsoft", "credentials_ref": "cred_m"}]

    int_storage.get_connection = _get_connection
    int_storage.get_oauth_client = _get_oauth_client
    int_storage.list_oauth_clients = _list_oauth_clients
    try:
        for args, item, why in (
                ({"op": "revoke", "connection_id": "c1"}, "conn:cred_c1",
                 "revoking a connection whose key the type lacks"),
                ({"op": "grant_app", "connection_id": "c1", "app_id": "a"}, "conn:cred_c1",
                 "granting an app a connection whose key the type lacks"),
                ({"op": "set_client", "family": "google", "client_id": "i", "client_secret": "s"},
                 "conn:cred_g", "replacing a client whose key the type lacks"),
                ({"op": "families"}, "conn:cred_g", "families, which opens every client key"),
        ):
            _refused("integrations_manage", args, _typed_for("integrations_manage"), item, why)
        _authorized("integrations_manage", {"op": "families"},
                    _typed_for("integrations_manage", keys=["conn:cred_g", "conn:cred_m"]),
                    "for families when the type holds every client key")
        _authorized("integrations_manage", {"op": "list_connections"},
                    _typed_for("integrations_manage"), "for list_connections, which opens nothing")
    finally:
        (int_storage.get_connection, int_storage.get_oauth_client,
         int_storage.list_oauth_clients) = saved

    from server.vault import api as vault_api
    saved_ls = vault_api.list_secrets

    def _vault_names(*rows):
        async def _ls(pool, *, entity_id):
            return [{"logical_name": n, "deactivated_at": d} for n, d in rows]
        return _ls
    try:
        vault_api.list_secrets = _vault_names(("github", None), ("github-work", None))
        _refused("github", {"op": "search", "query": "x"}, _typed_for("github"), "github",
                 "a search with the github key the type lacks")
        _authorized("github", {"op": "search", "query": "x"}, _typed_for("github", keys=["github"]),
                    "when the type holds the github key")
        vault_api.list_secrets = _vault_names(("github", "2026-10-01"), ("github-work", None),
                                              ("stripe-live", None))
        _refused("github", {"op": "search", "query": "x"}, _typed_for("github"), "github-work",
                 "the fallback github-named key the type lacks")
        vault_api.list_secrets = _vault_names(("stripe-live", None))
        _authorized("github", {"op": "search", "query": "x"}, _typed_for("github"),
                    "with no github key stored (public only)")

        # The key opener itself, which deep research reaches without the gate.
        saved_open = vault_api.open_secret
        opened = []

        async def _open(pool, *, entity_id, name, actor=None):
            opened.append(name)
            return b"tok-" + name.encode()
        vault_api.open_secret = _open
        vault_api.list_secrets = _vault_names(("github", None), ("github-work", None))
        try:
            tok = asyncio.run(tools_mod._github_repo_token(pool, 1))
            check("github key: an untyped turn opens 'github' as before",
                  tok == "tok-github" and opened == ["github"], f"got {tok} {opened}")
            for typ, want, why in ((_type(), None, "a type holding no github key runs public-only"),
                                   (_type(keys=["github-work"]), "tok-github-work",
                                    "a type opens only the github key it holds"),
                                   (_type(status="closed"), None, "a closed type opens none")):
                opened.clear()
                ctx_tok = tt.set_current_turn_type(typ)
                try:
                    tok = asyncio.run(tools_mod._github_repo_token(pool, 1))
                finally:
                    tt.CURRENT_TURN_TYPE.reset(ctx_tok)
                check(f"github key: {why}",
                      tok == want and all(n in (typ.get("keys") or []) for n in opened),
                      f"got {tok} opened={opened}")
        finally:
            vault_api.open_secret = saved_open
    finally:
        vault_api.list_secrets = saved_ls

    mirror = {"server": "books-1", "harness_id": "h", "mode": "service",
              "service_op": "mirror", "service_name": "gitea"}
    _refused("mesh_harness_run", mirror, _typed_for("mesh_harness_run"),
             "service-push:books-1:gitea", "a mirror whose push token the type lacks")
    _authorized("mesh_harness_run", mirror,
                _typed_for("mesh_harness_run", keys=["service-push:books-1:gitea"]),
                "for a mirror when the type holds its push token")
    _authorized("mesh_harness_run", dict(mirror, service_op="status"),
                _typed_for("mesh_harness_run"), "for a service status, which opens nothing")
    _refused("mesh_harness_run", {"server": "books-1", "harness_id": "h"},
             _typed_for("mesh_harness_run"), "harness:claude_oauth",
             "an operator agent run without the harness sign-in", eid=9003)
    _refused("lab_run", {"server": "books-1"}, _typed_for("lab_run"), "harness:claude_oauth",
             "an operator lab run without the harness sign-in", eid=9003)
    _authorized("lab_run", {"server": "books-1"},
                _typed_for("lab_run", keys=["harness:claude_oauth"]),
                "for an operator lab run that holds the harness sign-in", eid=9003)

    # Doors that reach the server a saved record names: a store, a project
    # whose code lives on a machine, a grant, a pipeline, a backup target.
    machine_cfg = {"pipeline": {"format": "privatae.pipeline/v1", "source": {
        "provider": "machine-forge", "machine": "web-1", "service": "gitea", "repo": "u/r"}}}
    rec_pool = _Pool()
    rec_pool.rows = [
        ("SELECT repo, config FROM projects WHERE id=$1", lambda a: (
            {"repo": "u/r", "config": machine_cfg} if a[0] == 41
            else {"repo": "plat/r", "config": {}} if a[0] == 42 else None)),
        ("FROM harness_run WHERE id", lambda a: {"project_id": 41} if a[0] == 639 else None),
        ("FROM dcn_move_plans WHERE id", lambda a: (
            {"store_id": 5, "signed_terms": {"server": "web-1"}} if a[0] == 9 else None)),
        ("FROM dcn_stores WHERE id", lambda a: (
            {"class": "machine", "signed_terms": {"server": "web-1", "root": "/srv"}} if a[0] == 5
            else {"class": "cabinet", "signed_terms": {"class": "cabinet", "bucket": "b",
                                                       "cred_name": "s3-other"}} if a[0] == 6
            else None)),
        ("FROM nac_mgmt_grants WHERE id", lambda a: (
            {"server": "web-1", "approved_at": None, "signed_terms": None} if a[0] == 12
            else {"server": "books-1", "approved_at": "2026-10-01",
                  "signed_terms": {"server": "web-1"}} if a[0] == 13 else None)),
    ]
    for op in ("list", "stat", "read_range", "search", "catalog"):
        _refused("dcn", {"op": op, "store_id": 5, "path": "a"}, _typed_for("dcn"), "web-1",
                 f"{op} on a machine store outside the type", p=rec_pool)
    _refused("dcn", {"op": "read_range", "store_id": 6, "path": "a"}, _typed_for("dcn"),
             "s3-other", "a cabinet read with a key the type lacks", p=rec_pool)
    _refused("dcn", {"op": "run_plan", "plan_id": 9, "mode": "apply"}, _typed_for("dcn"),
             "web-1", "a plan on a store outside the type", p=rec_pool)
    _authorized("dcn", {"op": "read_range", "store_id": 5, "path": "a"},
                _typed_for("dcn", bastions=["web-1"]), "on a listed machine store", p=rec_pool)
    _authorized("dcn", {"op": "stores"}, _typed_for("dcn"), "for stores, which reaches nothing",
                p=rec_pool)

    forge_reach = "web-1"
    for tool, args, why, ctx in (
            ("build", {"action": "run", "project_id": 41}, "a build of a project on a machine", None),
            ("build", {"action": "run"}, "a build of the conversation's project on a machine",
             "project:41"),
            ("ship_project_app", {"project_id": 41}, "shipping a project on a machine", None),
            ("workspace_file", {"op": "write", "project_id": 41, "path": "a.md", "content": "x"},
             "a write that syncs to a machine", None),
            ("publish_app", {"op": "publish", "project_id": 41, "agreement_version": 1},
             "publishing a project on a machine", None),
            ("review.adopt", {"run_id": 639}, "adopting a run whose project is on a machine", None),
            ("review.adopt_undo", {}, "an undo in the conversation's project on a machine",
             "project:41"),
    ):
        ok, denial = asyncio.run(auth(tool, args, 1, rec_pool, None, turn_type=_typed_for(tool),
                                      interaction_context=ctx))
        check(f"gate: {tool} refuses {why} outside the type",
              ok is False and (denial or {}).get("reason") == tt.DENIAL_REASON
              and (denial or {}).get("item") == forge_reach, f"got ok={ok} denial={denial}")
        ok, denial = asyncio.run(auth(tool, args, 1, rec_pool, None, interaction_context=ctx,
                                      turn_type=_typed_for(tool, bastions=["web-1"],
                                                           keys=["service-push:web-1:gitea"])))
        check(f"gate: {tool} is authorized when the type holds the machine and its push token",
              ok is True, f"got ok={ok} denial={denial}")
    _authorized("workspace_file", {"op": "write", "project_id": 42, "path": "a.md", "content": "x"},
                _typed_for("workspace_file"), "for a write in a project on the platform forge",
                p=rec_pool)
    _authorized("workspace_file", {"op": "read", "project_id": 41, "path": "a.md"},
                _typed_for("workspace_file"), "for a read, which syncs nothing", p=rec_pool)
    _authorized("review.adopt", {"run_id": 639, "dry_run": True}, _typed_for("review.adopt"),
                "for a dry run, which writes nothing", p=rec_pool)

    # WHICH project an adopt door writes is the door's own derivation, every
    # shape of it (runtime/tools.py, the review.adopt / review.adopt_undo
    # branches and _exec_review_adopt): the run is named by `run` as well as
    # `run_id`, the run's project wins over the conversation's, and the
    # conversation's project fills in for ANY falsy project_id, 0 included.
    # Project 42 is on the platform forge, 41 on web-1; run 640 has no
    # project of its own, so the door takes its session's.
    rec_pool.rows.append(("FROM harness_run WHERE id", lambda a: (
        {"project_id": 41} if a[0] == 639 else {"project_id": None} if a[0] == 640 else None)))
    rec_pool.rows.insert(0, rec_pool.rows.pop())
    for tool, args, ctx, why in (
            ("review.adopt", {"run": 639}, "project:42",
             "a run named by `run` whose project is on a machine, from a platform project's thread"),
            ("review.adopt", {"run": 639}, None,
             "a run named by `run` whose project is on a machine"),
            ("review.adopt_undo", {"project_id": 0}, "project:41",
             "an undo with project_id 0, which the door fills in from the thread"),
            ("review.adopt", {"session_id": "s1", "harness": "codex", "project_id": 0}, "project:41",
             "a session adopt with project_id 0, which the door fills in from the thread"),
    ):
        ok, denial = asyncio.run(auth(tool, args, 1, rec_pool, None, turn_type=_typed_for(tool),
                                      interaction_context=ctx))
        check(f"gate: {tool} refuses {why}",
              ok is False and (denial or {}).get("reason") == tt.DENIAL_REASON
              and (denial or {}).get("item") == forge_reach, f"got ok={ok} denial={denial}")
    for args, ctx, why in (
            ({"run_id": 640}, "project:42", "a run with no project, which lands where its session says"),
            ({"session_id": "s1", "harness": "codex"}, None,
             "a session adopt naming no project, which lands where its session says"),
    ):
        ok, denial = asyncio.run(auth("review.adopt", args, 1, rec_pool, None,
                                      turn_type=_typed_for("review.adopt"), interaction_context=ctx))
        check(f"gate: review.adopt refuses {why} (unknown to the wall)",
              ok is False and (denial or {}).get("reason") == tt.DENIAL_REASON,
              f"got ok={ok} denial={denial}")

    # A dcn store's terms are read from the door's own row set: its grant
    # loader (nac_dcn._signed_dcn_grants) reads every approved store
    # whatever its entity_id column says, so the wall's read must too.
    other_pool = _Pool()
    other_pool.rows = [("FROM dcn_stores WHERE id", lambda a: (
        {"class": "machine", "signed_terms": {"server": "web-1"}}
        if a[0] == 5 and (len(a) == 1 or a[1] == 2) else None))]
    _refused("dcn", {"op": "list", "store_id": 5}, _typed_for("dcn"), "web-1",
             "a store the door's grant set holds, whatever entity_id its row carries",
             p=other_pool)

    for tool in ("approve_nac_draft", "revoke_nac_rule"):
        _refused(tool, {"kind": "mgmt_grant", "id": 12}, _typed_for(tool), "web-1",
                 "a draft grant for a server outside the type", p=rec_pool)
        _refused(tool, {"kind": "mgmt_grant", "id": 13}, _typed_for(tool), "web-1",
                 "an approved grant whose signed server is outside the type", p=rec_pool)
    _refused("propose_egress_rule", {"action": "allow", "destination": "x.example",
                                     "exit": "bastion:web-1"},
             _typed_for("propose_egress_rule"), "web-1", "an exit through a server outside the type")
    _refused("pipeline", {"op": "propose", "project_id": 41, "pipeline": machine_cfg["pipeline"]},
             _typed_for("pipeline"), "web-1", "a code server on a machine outside the type")
    # The door also takes the pipeline as JSON text (runtime/tools.py _exec_pipeline).
    import json as _json
    _refused("pipeline", {"op": "propose", "project_id": 41,
                          "pipeline": _json.dumps(machine_cfg["pipeline"])},
             _typed_for("pipeline"), "web-1",
             "a code server on a machine outside the type, sent as JSON text")

    # What the user's machines serve, in list_local_models' shape: the real
    # match_local_model / local_model_servers run over it.
    from mastercode import model_backends as mb
    saved_list = mb.list_local_models
    served: list = []

    async def _served(pool, entity_id, **kw):
        return [dict(m) for m in served]
    mb.list_local_models = _served
    bench = {"op": "run", "provider": "local", "model": "qwen3:8b"}
    try:
        served[:] = [{"id": "qwen3:8b", "bastion": "web-1", "endpoint": "http://x", "kind": "ollama"}]
        _refused("model_bench", bench, _typed_for("model_bench"), "web-1",
                 "a local bench on a server outside the type")
        # Served on a listed server AND an unlisted one, the listed one first.
        # The bench takes the first row of a read with no order, made again
        # after the wall's, so the wall cannot know which row that is.
        served[:] = [{"id": "qwen3:8b", "bastion": "books-1", "endpoint": "http://b", "kind": "ollama"},
                     {"id": "qwen3:8b", "bastion": "web-1", "endpoint": "http://x", "kind": "ollama"}]
        _refused("model_bench", bench, _typed_for("model_bench"), "web-1",
                 "a local bench of a model a server outside the type also serves")
        _authorized("model_bench", bench, _typed_for("model_bench", bastions=["books-1", "web-1"]),
                    "for a local bench when the type lists every server serving the model")
        _authorized("model_bench", {"op": "status", "provider": "local", "model": "qwen3:8b"},
                    _typed_for("model_bench"), "for status, which runs nothing")
    finally:
        mb.list_local_models = saved_list

    # The real list_local_models over a database whose heartbeat read fails:
    # its best-effort answer is "none", which the wall would read as "this
    # bench reaches no server". The wall's read must refuse instead.
    class _HeartbeatRaisesPool(_Pool):
        async def fetch(self, sql, *a, **k):
            if "network_bastion_heartbeat" in sql:
                self.heartbeat_read = True
                raise RuntimeError("heartbeat table unreadable")
            return await super().fetch(sql, *a, **k)
    hb_pool = _HeartbeatRaisesPool()
    ok, denial = _gate("model_bench", bench, _typed_for("model_bench"), 1, hb_pool)
    check("gate: model_bench refuses a local bench when the servers serving it could not be read",
          getattr(hb_pool, "heartbeat_read", False) and ok is False
          and (denial or {}).get("reason") == tt.DENIAL_REASON, f"got ok={ok} denial={denial}")

    from server.ships import pipeline as ship_pl
    from server.ships import runner as ship_runner
    saved_ship = (ship_pl.find, ship_runner.load_action_op)

    async def _find(pool, name):
        return {"spec": {"steps": [{"step": "deploy", "op": "deploy-app", "machine": "web-1"}]}}

    async def _load_op(pool, name):
        return {"manifest": {"kind": "action", "credentials": ["registry-push"]}}
    ship_pl.find, ship_runner.load_action_op = _find, _load_op
    try:
        ship = {"method": "POST", "path": "/api/ships/runs",
                "body": {"pipeline": "web", "commit": "a" * 40}}
        _refused("kernel_op", ship, _typed_for("kernel_op"), "web-1",
                 "a ship run to a machine outside the type")
        _refused("kernel_op", ship, _typed_for("kernel_op", bastions=["web-1"]), "registry-push",
                 "a ship run sealing a key the type lacks")
        _authorized("kernel_op", ship, _typed_for("kernel_op", bastions=["web-1"],
                                                  keys=["registry-push"]),
                    "for a ship run when the type holds its machines and keys")
    finally:
        ship_pl.find, ship_runner.load_action_op = saved_ship
    for target, item in (({"kind": "machine", "address": "web-1"}, "web-1"),
                         ({"kind": "s3", "address": "bkt", "cred_name": "s3-backup"}, "s3-backup")):
        _refused("kernel_op", {"method": "PUT", "path": "/api/backup/settings",
                               "body": {"target": target}},
                 _typed_for("kernel_op"), item, f"a backup target the type lacks ({item})")

    # Code that picks a server itself reads the running turn's scope.
    from server.routes.context_extract import type_scoped_gate
    gate_in = {"web-1": (True, "", "ran"), "books-1": (True, "", "ran")}
    ms = [{"server": "web-1"}, {"server": "books-1"}]
    check("context extract: an untyped turn keeps every approved machine",
          type_scoped_gate(gate_in, ms, named=False) == gate_in)
    tok = tt.set_current_turn_type(_type())
    try:
        g = type_scoped_gate(gate_in, ms, named=False)
        check("context extract: a typed pick may only take a machine the type lists",
              g["web-1"][0] is False and g["books-1"][0] is True, f"got {g}")
        tt.CURRENT_TURN_TYPE.set(_type(bastions=["*"]))
        g = type_scoped_gate(gate_in, ms, named=False)
        check("context extract: '*' lets the pick take any machine", g == gate_in, f"got {g}")
        g = type_scoped_gate(gate_in, ms, named=True)
        check("context extract: '*' never passes a named machine the type does not list",
              g["web-1"][0] is False and g["books-1"][0] is False, f"got {g}")
        tt.CURRENT_TURN_TYPE.set(_type(status="closed"))
        g = type_scoped_gate(gate_in, ms, named=False)
        check("context extract: a closed type reaches no machine",
              not any(v[0] for v in g.values()), f"got {g}")
    finally:
        tt.CURRENT_TURN_TYPE.reset(tok)

    # The reads are only for those doors: any other typed call reads nothing.
    p_none = _Pool()
    r = asyncio.run(tt.resolve_targets(p_none, 1, "mgmt_run", {"server": "books-1", "token_id": 7}))
    check("resolve_targets: a door that names its target reads nothing",
          r == {} and p_none.reads == [], f"got {r} reads={p_none.reads}")
    r = asyncio.run(tt.resolve_targets(tok_pool, 1, "quarantine_bastion", {"token_id": "x"}))
    check("resolve_targets: an id the door could not parse resolves to nothing",
          r == {}, f"got {r}")
    d = tt.check_call(net, "quarantine_bastion", {"token_id": 7})
    check("check_call without resolve_targets refuses a token door (stricter, never wider)",
          isinstance(d, dict) and d.get("reason") == tt.DENIAL_REASON, f"got {d}")
    swept = sorted(
        n for n, s in by_name.items()
        if "token_id" in ((s.get("function") or {}).get("parameters") or {}).get("properties", {}))
    missing = [n for n in swept if getattr(tt, "TYPE_SERVER_TOKEN_ARGS", {}).get(n) != "token_id"]
    check("bastions: every registry argument named `token_id` is a checked door",
          not missing, f"unchecked: {missing}")

    # ── 7. An in-type call passes the type wall and reaches the bits check.
    ok, denial = asyncio.run(auth("keys_manage", {"op": "list"}, 1, pool, None,
                                  turn_type=fin))
    check("typed: in-type call is authorized (bits are the type's)",
          ok is True, f"got ok={ok} denial={denial}")

    # ── 8. The grant vocabulary: admin / shell_exec contribute nothing.
    bits = tt.grant_bits_for(_type(grant_names=["read_context", "admin", "shell_exec"]))
    check("type grant: admin and shell_exec contribute no bits",
          bits == NAME_TO_BIT["read_context"], f"got {bits}")

    # ── 9. Effective = entity ∩ type, and the wording knows who is asking.
    narrow = _type(grant_names=["read_context"], tool_groups=["vault"])
    ok, denial = asyncio.run(auth("keys_manage", {"op": "use", "name": "stripe-live",
                                                  "action": "sign", "request": {}},
                                  1, pool, None, turn_type=narrow))
    check("typed: a tool needing bits the type lacks is refused by the capability wall",
          ok is False and isinstance(denial, dict)
          and denial.get("reason") == "capability_denied",
          f"got ok={ok} denial={denial}")
    check("typed: that refusal speaks to the Host about its TYPE",
          isinstance(denial, dict) and "turn type" in str(denial.get("result", "")).lower()
          and "report back" not in str(denial.get("result", "")).lower(),
          f"got {denial}")
    # An agent inside a typed turn whose own grant already covers the type's
    # bits: still an agent, still told to report to its host.
    ok, denial = asyncio.run(auth("keys_manage", {"op": "use", "name": "stripe-live",
                                                  "action": "sign", "request": {}},
                                  1, pool, None, grant_bits=NAME_TO_BIT["read_context"],
                                  turn_type=narrow))
    check("agent in a typed turn: the capability refusal still speaks to an agent",
          ok is False and isinstance(denial, dict)
          and "report back" in str(denial.get("result", "")).lower(),
          f"got {denial}")

    # ── 10. Closed, and honest about what was not read.
    closed = asyncio.run(tt.load_turn_type(_BindingRaisesPool(), 7, thread_id=THREAD))
    check("closed: an unreadable BINDING yields a closed type, not None",
          isinstance(closed, dict) and closed.get("status") == "closed")
    d = tt.check_call(closed, "escalate", {})
    check("closed: even a primitive is refused",
          isinstance(d, dict) and d.get("reason") == tt.DENIAL_REASON, f"got {d}")
    check("closed: a binding failure says it could not read WHETHER the thread is bound",
          isinstance(d, dict) and "could not read whether" in str(d.get("result"))
          and "is bound to turn type" not in str(d.get("result")), f"got {d}")
    closed2 = asyncio.run(tt.load_turn_type(
        _TypeRaisesPool(binding="00000000-0000-0000-0000-0000000000aa"), 7, thread_id=THREAD))
    d2 = tt.check_call(closed2, "escalate", {})
    check("closed: a type-row failure names the bound type it could not read",
          isinstance(closed2, dict) and closed2.get("status") == "closed"
          and isinstance(d2, dict) and "bound to turn type" in str(d2.get("result")),
          f"got {closed2} / {d2}")
    draft_row = dict(_type(status="draft"))
    r = asyncio.run(tt.load_turn_type(
        _Pool(binding=draft_row["id"], type_row=draft_row), 7, thread_id=THREAD))
    check("binding to a DRAFT type is untyped (the user has not approved it)", r is None)
    active_row = dict(_type())
    r = asyncio.run(tt.load_turn_type(
        _Pool(binding=active_row["id"], type_row=active_row), 7, thread_id=THREAD))
    check("binding to an ACTIVE type resolves it",
          isinstance(r, dict) and r.get("name") == "Finance" and r.get("status") == "active",
          f"got {r}")

    # ── 11. Holders.
    ok, denial = asyncio.run(auth("mgmt_run", {"server": "web-1", "op": "uptime"},
                                  1, pool, None, turn_type=fin))
    check("holders: the refusal names the active types that hold the item",
          isinstance(denial, dict) and denial.get("holders") == ["Ops"],
          f"got {denial}")

    # ── 12. An agent dispatched from a typed turn is capped at the turn.
    import mastercode.runtime.scoped_agent as sa
    executed_kw = []

    async def fake_call_model(**kw):
        return {"content": "done", "tool_calls": None, "raw_message": {},
                "tokens_used": 0, "prompt_tokens": 0, "completion_tokens": 0}

    async def fake_exec_meta(name, args, **kw):
        executed_kw.append((name, kw))
        return {"status": "ok", "result": f"{name} ran"}

    async def fake_schemas(pool, eid, **kw):
        return [_schema("local_grep"), _schema("keys_manage")]

    async def fake_route(pool, eid, role):
        return {"provider": "openrtr", "model": "test/model", "base_url": None,
                "fallback_provider": None, "fallback_model": None}

    sa.call_model = fake_call_model
    sa._execute_meta_tool = fake_exec_meta
    sa._build_tool_schemas = fake_schemas
    sa.resolve_role_route = fake_route
    sa._META_TOOL_NAMES = {"local_grep", "keys_manage"}
    sa._BRIDGE_TOOL_NAMES = set()
    out = asyncio.run(sa.run_scoped_agent(
        _Pool(), entity_id=1, task_prompt="t", tool_scope=["local_grep", "keys_manage"],
        grant=["read_context", "write_context", "external_api"], session_id=None,
        max_rounds=2, turn_type=fin))
    check("agent cap: the out-of-type tool is stripped from the agent's scope",
          "local_grep" in (out.get("denied_tools") or []), f"got {out}")
    check("agent cap: the reason names the TYPE, not code-exec",
          "Finance" in str(out.get("blocked_reason") or "")
          and "code" not in str(out.get("blocked_reason") or "").lower()
          and "code" not in str(out.get("result") or "").lower(),
          f"got blocked_reason={out.get('blocked_reason')!r} result={out.get('result')!r}")
    # Only the out-of-type tool in scope: the agent is never started, and the
    # sentence still names the type.
    out2 = asyncio.run(sa.run_scoped_agent(
        _Pool(), entity_id=1, task_prompt="t", tool_scope=["local_grep"],
        grant=["read_context"], session_id=None, max_rounds=2, turn_type=fin))
    check("agent cap: an agent left with no tool is not started, and says why by type",
          "not started" in str(out2.get("result") or "")
          and "Finance" in str(out2.get("result") or "")
          and "runs code" not in str(out2.get("result") or ""),
          f"got {out2.get('result')!r}")
    # The grant is intersected with the type AT DISPATCH: a tool the agent's
    # own grant reaches but the type's bits do not is refused before the
    # agent runs, naming the tool and the missing capability.
    sa._META_TOOL_NAMES = {"memory_curate", "keys_manage"}

    async def fake_schemas_mc(pool, eid, **kw):
        return [_schema("memory_curate"), _schema("keys_manage")]
    sa._build_tool_schemas = fake_schemas_mc
    out3 = asyncio.run(sa.run_scoped_agent(
        _Pool(), entity_id=1, task_prompt="t", tool_scope=["memory_curate"],
        grant=["write_memory", "read_context"], session_id=None, max_rounds=2,
        turn_type=_type(tool_groups=["memory"], grant_names=["read_context"])))
    check("agent cap: grant ∩ type is applied at dispatch (unreachable, not run)",
          out3.get("rounds") == 0 and "memory_curate" in (out3.get("denied_tools") or [])
          and "cannot succeed as written" in str(out3.get("result") or ""),
          f"got rounds={out3.get('rounds')} denied={out3.get('denied_tools')} result={str(out3.get('result'))[:160]!r}")
    sa._META_TOOL_NAMES = {"local_grep", "keys_manage"}
    sa._build_tool_schemas = fake_schemas
    # The type rides into the agent's own tool calls.
    calls = [{"id": "c1", "function": {"name": "keys_manage", "arguments": "{\"op\": \"list\"}"}}]
    rounds = {"n": 0}

    async def fake_call_model_2(**kw):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return {"content": "", "tool_calls": calls, "raw_message": {},
                    "tokens_used": 0, "prompt_tokens": 0, "completion_tokens": 0}
        return {"content": "done", "tool_calls": None, "raw_message": {},
                "tokens_used": 0, "prompt_tokens": 0, "completion_tokens": 0}
    sa.call_model = fake_call_model_2
    executed_kw.clear()
    asyncio.run(sa.run_scoped_agent(
        _Pool(), entity_id=1, task_prompt="t", tool_scope=["keys_manage"],
        grant=["read_context", "write_context", "external_api"], session_id=None,
        max_rounds=3, turn_type=fin))
    check("agent cap: the agent's tool calls carry the turn type into the gate",
          any(n == "keys_manage" and kw.get("turn_type") is fin for n, kw in executed_kw),
          f"got {[(n, sorted(kw)) for n, kw in executed_kw]}")

    # ── 13. session.py wiring (structural, per site by name).
    sess_path = os.path.join(app_root, "brain", "mastercode", "runtime", "session.py")
    src = turn_source(app_root)
    typed = 'turn_type=getattr(sess, "turn_type", None)'

    def _site(marker: str, window: int = 1600) -> bool:
        i = src.find(marker)
        return i >= 0 and typed in src[i:i + window]
    # (one escalate site, session._escalate,
    #  shared by the loop, the allowance stop's continue card and the exits
    #  past the loop)
    check("session: the escalate exec site passes the turn type",
          _site('_execute_meta_tool(\n                "escalate", args,')
          and src.count('_execute_meta_tool(\n                "escalate"') == 1
          and '_execute_meta_tool(\n                            "escalate"' not in src)
    check("session: the operate site passes the turn type to run_direct_op",
          _site("tool_result = await run_direct_op("))
    # One host-own helper: a door called
    # by name and a door run through operate both take _run_host_own.
    check("session: the host-doors exec site passes the turn type",
          _site("async def _run_host_own(_name, _args):")
          and "await _run_host_own(func_name, func_args)" in src
          and "await _run_host_own(_op_tool, _op_args)" in src)
    # The held-read site is one function since the search-mode `read` door:
    # a read by name and a read through the
    # door both run _run_held_read.
    check("session: the read-safe exec site passes the turn type",
          _site("if _name in _META_TOOL_NAMES or _name in _BRIDGE_TOOL_NAMES:\n"
                "                    return await _execute_meta_tool(")
          and "tool_result = await _run_held_read(func_name, func_args)" in src
          and "tool_result = await _run_held_read(_eff_name, _eff_args)" in src)
    check("session: the dispatch site passes the turn type",
          _site("_disp = await _disp_mgr.dispatch("))
    # The pick and orient are gone: the
    # fixed list's build doors and the door catalog are bound to the type.
    check("session: the fixed list's tools are bound to the type",
          "if _cm.get((s.get(\"function\") or {}).get(\"name\"), \"other\") in _type_groups_allowed" in src)
    check("session: the door catalog carries the turn type",
          "turn_type=sess.turn_type,\n                withheld=getattr(sess, \"_tools_withheld\", set()))" in src)
    check("session: the type is resolved from the thread and project before the tool list",
          "resolve_session_type(" in src
          # the tool list is built by _build_turn_tools, which the turn calls after
          and src.find("resolve_session_type(") < src.find("= await self._build_turn_tools(")
          and "tools = _build_host_orchestration_tools()" in src)
    check("session: the catalog text is narrowed to the type",
          "_orch_block = _build_orchestrator_prompt([" in src
          and "_type_groups_allowed is None or not isinstance(s, dict)" in src)
    scoped_src = open(os.path.join(app_root, "brain", "mastercode", "runtime",
                                   "scoped_agent.py"), encoding="utf-8").read()
    check("scoped_agent: type-stripped names never join the code-exec set",
          "_denied = set(_denied) | _outside_type" not in scoped_src)

    # ── 14. The door tables name real tools with real argument names.
    for table in (tt.TYPE_KEY_ARGS, tt.TYPE_SERVER_ARGS, tt.TYPE_SERVER_TOKEN_ARGS):
        for door, arg in table.items():
            s = by_name.get(door)
            props = ((s or {}).get("function") or {}).get("parameters", {}).get("properties", {})
            check(f"door table: {door}.{arg} is a real tool argument",
                  s is not None and arg in props,
                  f"{door} {'missing' if s is None else 'has ' + str(sorted(props))}")
    check("door table: autofill is a real tool and takes host",
          "autofill" in by_name and "host" in
          by_name["autofill"]["function"]["parameters"]["properties"])
    for door in tt.TYPE_KEY_DEFAULTS:
        check(f"door table: {door} (defaulted credential) is a key door too",
              door in tt.TYPE_KEY_ARGS)

    print()
    if FAILS:
        print(f"[turn-types] {len(FAILS)} FAILED: {FAILS}")
        return 1
    print("[turn-types] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
