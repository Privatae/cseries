#!/usr/bin/env python3
"""GET /api/network/observations' data, run on the kernel's schema.

The bastion sends, per agent, the file the process runs (`exe`), the unit that
owns it and the account facts behind it (owner_class, owner_admin,
owner_interactive), and per machine the tool servers its agent clients declare
(`tool_servers`, `tool_server_scan`). nac_agents.ingest and
topology.upsert_heartbeat dropped all of it. This drives the real ingest and
the real heartbeat upsert, then server/network/observations.for_app, and checks:

  K  what the bastion sent is kept (v256 columns), bounded, unknown keys dropped;
  T  a beat without the key keeps the stored tool servers, an empty list clears
     them, and the scan report is NULL on a beat that did not read;
  S  no delegation token, approval key or grant reaches the output, and the
     enforcement report is cut to its flags;
  W  a pairing that withholds `observe` is listed by name and watch state only,
     and agent rows stored for it are left out; a revoked pairing is absent;
  P  paging by `next_since` returns every row once when rows share a last_seen.

Why a real database: the paging key, `requested` and the COALESCE that keeps a
stored list are SQL. A stub pool would assert my reading of that SQL.

Run:  python3 scripts/test_observations_pg.py
      (TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN names a server; without one it
      starts a throwaway one through docker, as the other _pg gates do.)
"""
import asyncio
import contextlib
import json
import os
import subprocess
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]

import asyncpg  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database for the paging key and the kept-list SQL.")
    sys.exit(2)

from server.network import nac_agents, observations, topology  # noqa: E402
from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

ENTITY = 1
OPEN, WITHHELD, REVOKED, OFFLINE_WITHHELD = 90021, 90022, 90023, 90024
# Invented values only.
TAG = "del_INVENTEDobservationsTAG00"
DEVICE_KEY = "INVENTED-approval-device-key"
FAILS = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:500]) if extra else ""))
    if not cond:
        FAILS.append(msg)


TOOL_SERVER = {
    "client": "claude-code", "scope": "user", "path": "/home/invented/.claude.json",
    "name": "files", "transport": "stdio", "locality": "local_process",
    "command": "npx -y @invented/files-server /srv/invented", "package_source": "npx",
    "package_name": "@invented/files-server", "package_pinned": "no",
    "env_names": ["INVENTED_API_KEY"], "secret_in_plaintext": True,
    "auth_configured": "not_applicable", "bind_class": "not_applicable",
    "running_state": "declared_running", "pids": [4101, "x", True],
    "value_that_must_not_land": "INVENTED-secret-value",
}


def beat(token, **extra):
    return {"token_id": token, "hostname": f"box-{token}", "platform": "linux",
            "gateway_version": "0.54.0", **extra}


async def main(dsn: str) -> int:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        for tid in (OPEN, WITHHELD, REVOKED, OFFLINE_WITHHELD):
            await pool.execute(
                "INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind) "
                "VALUES ($1, $2, $3, $4, $3, 'bastion')", tid, ENTITY, f"obs-{tid}", tid.to_bytes(4, "big"))
        await pool.execute(
            "INSERT INTO nac_agent_delegations (token, server, expires_at) "
            "VALUES ($1, '*', now() + interval '1 hour')", TAG)

        r = await topology.upsert_heartbeat(pool, ENTITY, beat(
            OPEN, capabilities=["agent-observe", "scheduler-observe", "tool-server-observe"],
            schedulers=[{"kind": "cron", "name": "nightly", "command": "invented-agent run --token=INVENTED-sched-secret",
                         "automation": False}],
            scheduler_scan={"supported": True, "kinds": ["cron"], "entries": 1},
            tool_servers=[TOOL_SERVER, "not-a-dict"],
            tool_server_scan={"supported": True, "accounts": 2, "files_read": 3,
                              "process_scan": "ok", "bogus": "INVENTED-bogus"},
            enforcing={"write_gate": True, "anchored": False, "approval_device_key": DEVICE_KEY,
                       "grants": [{"family": "observe"}]},
            llm_models=["invented-model"], llm_port=8080))
        check(r is not None, "the heartbeat upsert accepts a beat carrying tool servers")
        await topology.upsert_heartbeat(pool, ENTITY, beat(
            WITHHELD, capabilities=["embed"],
            enforcing={"anchored": True, "is_anchor": False, "pairing_caps": ["inference"]}))

        agent = {"pid": 4101, "ppid": 1, "signature": "claude-code", "kind": "agent",
                 "user": "invented", "cmdline": "claude --invented",
                 "exe": "/usr/local/bin/claude", "unit": "user@1000.service",
                 "owner_class": "local_user", "owner_admin": "yes", "owner_interactive": "no",
                 "delegation_token": TAG, "origin": "interactive"}
        # A pairing that withheld observe and then went OFFLINE. What it stored
        # while it still could (schedulers, tool servers, agent rows) stays in
        # the tables; the box going quiet must not hand it on.
        await topology.upsert_heartbeat(pool, ENTITY, beat(
            OFFLINE_WITHHELD, capabilities=["agent-observe"],
            schedulers=[{"kind": "cron", "name": "INVENTED-offline-sched", "command": "x"}],
            tool_servers=[dict(TOOL_SERVER, name="INVENTED-offline-server")],
            tool_server_scan={"supported": True}))
        await nac_agents.ingest(pool, OFFLINE_WITHHELD, "box-offline",
                                [{"pid": 7101, "signature": "cursor", "kind": "agent", "cmdline": "cursor"}])
        await topology.upsert_heartbeat(pool, ENTITY, beat(
            OFFLINE_WITHHELD, capabilities=["embed"],
            enforcing={"anchored": True, "is_anchor": False, "pairing_caps": ["inference"]}))
        await pool.execute("UPDATE network_bastion_heartbeat SET last_seen_at = now() - interval '1 hour' "
                           "WHERE token_id = $1", OFFLINE_WITHHELD)

        n = await nac_agents.ingest(pool, OPEN, "box-open", [
            agent, {"pid": 4102, "signature": "aider", "kind": "agent",
                    "cmdline": "aider --api-key INVENTED-agent-secret",
                    "user": "svc", "owner_class": "service", "owner_admin": "no",
                    "owner_interactive": "no", "exe": "/opt/invented/aider" + "x" * 2000},
            {"pid": 4103, "signature": "codex", "kind": "agent", "cmdline": "codex"}])
        check(n == 3, "ingest stores the three observations", n)
        await nac_agents.ingest(pool, WITHHELD, "box-withheld",
                                [{"pid": 5101, "signature": "cursor", "kind": "agent", "cmdline": "cursor"}])
        await pool.execute(
            "INSERT INTO nac_agent_activity (token_id, hostname, pid, signature, cmdline, kind) "
            "VALUES ($1, 'box-revoked', 6101, 'cline', 'cline', 'agent')", REVOKED)
        await pool.execute("UPDATE mcp_client_tokens SET revoked_at = now() WHERE id = $1", REVOKED)

        # C: a row whose write began just now is not read yet. Its last_seen is
        # the writing statement's start, so a write still uncommitted when a
        # reader passes it would land BEHIND that reader's cursor and never be
        # read. Only rows older than the settle window come back.
        out = await observations.for_app(pool, ENTITY)
        check(4101 not in {a["pid"] for a in out["agents"]},
              "C a row written within the settle window is not returned yet",
              sorted(a["pid"] for a in out["agents"]))
        # Every later read sees the rows as written a minute ago.
        await pool.execute("UPDATE nac_agent_activity SET last_seen = last_seen - interval '1 minute', "
                           "first_seen = first_seen - interval '1 minute'")

        out = await observations.for_app(pool, ENTITY)
        wire = json.dumps(out, default=str)
        agents = {a["pid"]: a for a in out["agents"]}
        bast = {b["token_id"]: b for b in out["bastions"]}

        # K
        a = agents.get(4101, {})
        check((a.get("exe"), a.get("unit"), a.get("owner_class"), a.get("owner_admin"),
               a.get("owner_interactive")) ==
              ("/usr/local/bin/claude", "user@1000.service", "local_user", "yes", "no"),
              "K exe, unit and the owner facts are kept as the bastion sent them", a)
        long_exe = agents.get(4102, {}).get("exe") or ""
        check(len(long_exe) == 1024, "K an exe longer than the bastion's path limit is cut at 1024", len(long_exe))
        check(agents.get(4103, {}).get("exe") is None and agents.get(4103, {}).get("owner_admin") is None,
              "K a field the bastion did not send stays NULL, never guessed", agents.get(4103))
        ts = (bast.get(OPEN, {}).get("tool_servers") or [{}])[0]
        check(ts.get("command") == TOOL_SERVER["command"] and ts.get("env_names") == ["INVENTED_API_KEY"]
              and ts.get("pids") == [4101] and ts.get("secret_in_plaintext") is True,
              "K a declared tool server is kept field by field; a non-integer pid is dropped", ts)
        check("value_that_must_not_land" not in ts and "INVENTED-secret-value" not in wire,
              "K an unknown key on a tool server is not stored", sorted(ts))
        check(len(bast.get(OPEN, {}).get("tool_servers") or []) == 1,
              "K a list item that is not an object is dropped", bast.get(OPEN, {}).get("tool_servers"))
        scan = bast.get(OPEN, {}).get("tool_server_scan") or {}
        check(scan.get("files_read") == 3 and scan.get("process_scan") == "ok" and "bogus" not in scan,
              "K the scan report keeps its named fields only", scan)

        # N: the Network app's own reads carry the same facts, through the
        # paths its Agents view already loads (GET /api/network/agents and
        # GET /api/network/bastions), so the person sees what an app can.
        listed = {x["pid"]: x for x in await nac_agents.list_observations(pool, ENTITY)}
        la = listed.get(4101, {})
        check((la.get("exe"), la.get("unit"), la.get("owner_class"), la.get("owner_admin"),
               la.get("owner_interactive")) ==
              ("/usr/local/bin/claude", "user@1000.service", "local_user", "yes", "no"),
              "N the Agents list carries the file, the unit and the account facts", la)
        check(listed.get(4103, {}).get("owner_admin") is None and "owner_admin" in listed.get(4103, {}),
              "N an agent the bastion said nothing about carries None, not a guess", listed.get(4103))
        check(TAG not in json.dumps(list(listed.values()), default=str),
              "N the Agents list still never carries the delegation token")
        from server import brain_service as _bs
        from server.routes import network as _net
        _saved = (_bs._pool, _bs._entity_id)
        _bs._pool, _bs._entity_id = pool, ENTITY
        try:
            nb = {x["id"]: x for x in (await _net.get_bastions())["bastions"]}
        finally:
            _bs._pool, _bs._entity_id = _saved
        nts = (nb.get(OPEN, {}).get("tool_servers") or [{}])[0]
        check(nts.get("name") == "files" and nts.get("secret_in_plaintext") is True
              and (nb.get(OPEN, {}).get("tool_server_scan") or {}).get("files_read") == 3,
              "N GET /api/network/bastions carries each machine's tool servers and scan", nb.get(OPEN))
        check(nb.get(WITHHELD, {}).get("tool_servers") is None
              and "tool_server_scan" in nb.get(WITHHELD, {}),
              "N a machine that never sent tool servers carries None, which the view words as not reported",
              nb.get(WITHHELD))
        # What a machine stored before its pairing withheld observe is not handed
        # on by this list either (observations._bastion's rule, offline or not).
        check(OFFLINE_WITHHELD in nb and nb[OFFLINE_WITHHELD].get("tool_servers") is None
              and nb[OFFLINE_WITHHELD].get("tool_server_scan") is None
              and "INVENTED-offline-server" not in json.dumps(nb, default=str),
              "N a machine that withholds observe carries no stored tool servers",
              nb.get(OFFLINE_WITHHELD))

        # S
        check(TAG not in wire, "S no delegation token value appears in the output")
        check(DEVICE_KEY not in wire and '"grants"' not in wire,
              "S the approval device key and the grants never appear")
        check(all("delegation_token" not in x for x in out["agents"]), "S no agent row has a delegation_token key")
        check(a.get("carries_delegation") is True and a.get("requested") is True
              and agents.get(4102, {}).get("carries_delegation") is False,
              "S carries_delegation and requested say what the tag was, without it", a)
        check(bast.get(OPEN, {}).get("enforcing") == {"write_gate": True, "anchored": False},
              "S the enforcement report is cut to its flags", bast.get(OPEN, {}).get("enforcing"))
        b = bast.get(OPEN, {})
        check(b.get("llm_models") == ["invented-model"] and b.get("llm_port") == 8080
              and b.get("schedulers") and b.get("agent_watch") == "watching",
              "S the open bastion carries its model server, schedulers and watch state", b)

        # The bastion sends command lines unredacted outside its own console.
        check("INVENTED-agent-secret" not in wire and "INVENTED-sched-secret" not in wire
              and agents.get(4102, {}).get("cmdline") == "aider --api-key <redacted>"
              and (b.get("schedulers") or [{}])[0].get("command")
              == "invented-agent run --token=<redacted>",
              "S a key in an agent's command line or a scheduled job's command is redacted",
              (agents.get(4102), b.get("schedulers")))

        # W
        w = bast.get(WITHHELD, {})
        check(w.get("agent_watch") == "withheld" and "tool_servers" not in w and "schedulers" not in w,
              "W a withholding pairing is listed by name and watch state only", w)
        check(5101 not in agents, "W agent rows stored for a withholding pairing are left out", sorted(agents))
        check(REVOKED not in bast and 6101 not in agents, "W a revoked pairing and its rows are absent")
        ow = bast.get(OFFLINE_WITHHELD, {})
        check(ow.get("agent_watch") == "withheld" and "tool_servers" not in ow and "schedulers" not in ow
              and "INVENTED-offline" not in wire,
              "W an OFFLINE pairing that withholds observe still gives its name only", ow)
        check(7101 not in agents, "W agent rows stored for an offline withholding pairing are left out",
              sorted(agents))
        # Fail closed: a bastion list that could not be read hides every agent
        # row, never shows them unfiltered.
        real_list = topology.list_bastions

        async def _unreadable(_pool, _eid):
            return []
        topology.list_bastions = _unreadable
        try:
            blind = await observations.for_app(pool, ENTITY)
        finally:
            topology.list_bastions = real_list
        check(blind["agents"] == [], "W no bastion list read means no agent rows, never unfiltered ones",
              sorted(x["pid"] for x in blind["agents"]))

        # T
        await topology.upsert_heartbeat(pool, ENTITY, beat(OPEN, capabilities=["agent-observe"]))
        kb = {x["token_id"]: x for x in (await observations.for_app(pool, ENTITY))["bastions"]}[OPEN]
        check(len(kb.get("tool_servers") or []) == 1 and kb.get("tool_server_scan") is None,
              "T a beat without the key keeps the list and reads the scan as not read", kb)
        await topology.upsert_heartbeat(pool, ENTITY, beat(OPEN, tool_servers=[],
                                                           tool_server_scan={"supported": True}))
        kb = {x["token_id"]: x for x in (await observations.for_app(pool, ENTITY))["bastions"]}[OPEN]
        check(kb.get("tool_servers") == [], "T an empty list clears the stored servers", kb.get("tool_servers"))

        # P: every row of the open bastion at one instant, read one per page.
        await pool.execute("UPDATE nac_agent_activity SET last_seen = '2026-10-20 12:00:00+00' "
                           "WHERE token_id = $1", OPEN)
        seen, since, pages, page = [], None, 0, {}
        while pages < 10:
            page = await observations.for_app(pool, ENTITY, since=observations.parse_since(since), limit=1)
            seen += [x["pid"] for x in page["agents"]]
            pages += 1
            if not page["more"]:
                break
            since = page["next_since"]
        check(sorted(p for p in seen if p in (4101, 4102, 4103)) == [4101, 4102, 4103]
              and len(seen) == len(set(seen)),
              "P one row per page reads every row once when they share a last_seen", seen)
        last = await observations.for_app(pool, ENTITY, since=observations.parse_since(page["next_since"]))
        check(last["agents"] == [] and last["more"] is False,
              "P a read from the last cursor returns nothing new", last["agents"])
    finally:
        await pool.close()
    return 0


# The scope: registered, and not one a community app may request (it names a
# person's machines and what runs on them, as read:topology does).
from server.applications.build_contract import COMMUNITY_ALLOWED_SCOPES  # noqa: E402
from server.applications.scopes import PERMISSIONS  # noqa: E402
check("read:observations" in PERMISSIONS, "read:observations is a registered scope")
check("read:observations" not in COMMUNITY_ALLOWED_SCOPES,
      "read:observations is not a community scope, like read:topology")

# A cursor the reader pasted into a URL unencoded: the query parser turns '+'
# into a space. The cursor this endpoint hands out carries no '+', and a
# mangled offset is read back as the '+' it was.
def _parsed(raw):
    try:
        return observations.parse_since(raw)
    except ValueError as e:
        return e


check(_parsed("2026-10-20T12:00:00.5 00:00|7") == _parsed("2026-10-20T12:00:00.5+00:00|7"),
      "parse_since reads an offset whose '+' a URL turned into a space",
      _parsed("2026-10-20T12:00:00.5 00:00|7"))
_cursor_of = getattr(observations, "cursor_of", lambda r: None)
check(_cursor_of({"last_seen": "2026-10-20T14:00:00.500000+02:00", "id": 7})
      == "2026-10-20T12:00:00.500000Z|7",
      "the cursor handed out is UTC with Z, no '+' for a URL to eat",
      _cursor_of({"last_seen": "2026-10-20T14:00:00.500000+02:00", "id": 7}))
# The route reads for an app's process, never for a page in a browser: a page
# can navigate away or open a WebRTC connection, which no page rule stops.
from types import SimpleNamespace  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from server import brain_service  # noqa: E402
from server.routes import network as net_routes  # noqa: E402


def _call(headers):
    req = SimpleNamespace(headers=headers)
    try:
        return asyncio.run(net_routes.get_observations(req, since=None))
    except HTTPException as e:
        return e.status_code


_saved_pool, brain_service._pool = brain_service._pool, None
check(_call({"sec-fetch-site": "same-origin"}) == 403, "a request a browser page sent is refused")
check(_call({"origin": "https://apps-invented.example"}) == 403, "...including one that only carries Origin")
check(isinstance(_call({}), dict), "a request from an app's process is answered")
brain_service._pool = _saved_pool

for bad in ("2026-10-20T12:00:00", "2026-10-20T12:00:00+00:00|x", "yesterday",
            "2026-10-20T12:00:00+00:00|" + str(2 ** 63)):
    try:
        observations.parse_since(bad)
        check(False, f"parse_since refuses {bad!r}")
    except ValueError:
        check(True, f"parse_since refuses {bad!r}")
check(observations.parse_since(None) is None, "parse_since: no since is no cursor")

with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=ENTITY) as _dsn:
    rc = asyncio.run(main(_dsn))
print()
if FAILS:
    print(f"FAILED — {len(FAILS)} of {CHECKS}")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
if CHECKS == 0:
    print("FAILED — no checks ran; a gate that asserts nothing is not a pass")
    sys.exit(1)
print(f"observations for apps: {CHECKS}/{CHECKS} checks passed")
sys.exit(rc)
