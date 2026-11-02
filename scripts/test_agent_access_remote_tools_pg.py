#!/usr/bin/env python3
"""An outside agent's machine reads and `contain`, on the kernel's schema.

The Host's /mcp tools carry the bastion's read names with the
machine first (list_machines, list_agents, list_scheduled,
tool_server_configs, posture, baseline_delta, contain), under the permission
model machine_op already has. This drives server/agent_access/tools.call
against real rows (the real heartbeat upsert, the real agent ingest, the
oversight ledger), with the machine side (nac_mgmt.gate_op, run_mgmt_op)
stubbed as scripts/test_agent_access_pg.py stubs it, and checks:

  R  every read is refused to an agent connected without 'read', and the
     refusal is recorded; with 'read' each answers and is recorded
  N  a machine this Host has no record of is refused, and so is a name two
     machines share; a machine whose pairing withholds `observe` is answered
     with its watch state only
  S  no settings-file path, no secret in a command line and no delegation tag
     reaches the agent; a tool server's problem is said without the path
  C  contain without a permission files the ask machine_op files for
     ('contain', [pid]) and stops with NeedsApproval, nothing runs; after
     'just this once' it reaches run_mgmt_op as op contain; a finding's
     fingerprint goes as finding:<fp>
  A  a tool is callable only if tools/list gives it (`machines` is gone),
     and machine_op still runs

Run:  python3 scripts/test_agent_access_remote_tools_pg.py
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
          "this gate needs a real database for the agent rows, the heartbeat and the asks.")
    sys.exit(2)

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402
from server.agent_access import store, tools  # noqa: E402
from server.network import nac_agents, nac_mgmt, topology  # noqa: E402

ENTITY = 1
BOX, WITHHELD, TWIN1, TWIN2 = 91001, 91002, 91003, 91004
# Invented values only.
TAG = "del_INVENTEDremoteToolsTAG000"
KEY_IN_CMD = "sk-ant-INVENTEDinventedINVENTED00"
CRON_KEY = "INVENTEDcronSecret12345"
CFG_PATH = "/home/invented/.claude.json"
FP = "0123456789abcdef0123456789abcdef"
FAILS: list[str] = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:600]) if extra else ""))
    if not cond:
        FAILS.append(msg)


RAN: list[tuple] = []


async def fake_run_mgmt_op(pool, eid, server, op, args, *, token_id=None, note="",
                           approval_wait_s=None, origin="service", budget_s=None):
    RAN.append((server, op, list(args or []), origin))
    return {"server": server, "op": op, "exit": 0,
            "stdout": json.dumps({"contain": "queued"}), "stderr": ""}


async def fake_gate_op(pool, eid, server, op):
    return True, "", "ran"


nac_mgmt.run_mgmt_op = fake_run_mgmt_op
nac_mgmt.gate_op = fake_gate_op

TOOL_SERVER = {
    "client": "claude-code", "scope": "user", "path": CFG_PATH, "name": "files",
    "transport": "stdio", "locality": "local_process",
    "command": f"npx -y @invented/files-server --api-key {KEY_IN_CMD}", "package_source": "npx",
    "package_name": "@invented/files-server", "package_pinned": "no",
    "env_names": ["INVENTED_API_KEY"], "secret_in_plaintext": True,
    "running_state": "declared_running", "pids": [4101],
}


def beat(token, host, **extra):
    return {"token_id": token, "hostname": host, "platform": "linux", "gateway_version": "0.58.1",
            **extra}


async def call(pool, conn, name, args):
    """(result, NeedsApproval or None)."""
    try:
        return await tools.call(pool, ENTITY, conn, name, args, "http://host.test"), None
    except tools.NeedsApproval as need:
        return None, need


async def connection(pool, read: bool) -> dict:
    reg = await store.register_client(pool, ENTITY, "Invented agent", ["http://127.0.0.1:1/cb"])
    cid = await pool.fetchval("SELECT id FROM agent_connections WHERE client_id=$1", reg["client_id"])
    await store.connect(pool, ENTITY, cid, read=read)
    return {"id": cid}


async def main(dsn: str) -> int:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        for tid in (BOX, WITHHELD, TWIN1, TWIN2):
            await pool.execute(
                "INSERT INTO mcp_client_tokens (id, entity_id, name, token_hash, url_handle, operator_kind) "
                "VALUES ($1, $2, $3, $4, $3, 'bastion')", tid, ENTITY, f"rt-{tid}", tid.to_bytes(4, "big"))
        await pool.execute("INSERT INTO nac_agent_delegations (token, server, expires_at) "
                           "VALUES ($1, '*', now() + interval '1 hour')", TAG)
        await topology.upsert_heartbeat(pool, ENTITY, beat(
            BOX, "box-a", capabilities=["agent-observe"],
            schedulers=[{"kind": "cron", "name": "nightly", "schedule": "0 3 * * *", "user": "invented",
                         "signature": "aider", "command": f"aider --yes --api-key {CRON_KEY}"}],
            scheduler_scan={"supported": True, "kinds": ["cron"], "entries": 1},
            tool_servers=[TOOL_SERVER, dict(TOOL_SERVER, name="pinned", package_pinned="yes",
                                            secret_in_plaintext=False, command="npx x@1.0.0")],
            tool_server_scan={"supported": True, "files_read": 1},
            posture={"ufw": "active", "pubwall": "ok", "ssh22": "closed", "sec": 3, "diskpct": 40},
            findings={"baseline": "ok", "baseline_taken_at": "2026-10-01T00:00:00Z",
                      "critical": 0, "high": 1, "warn": 0, "notice": 0}))
        await topology.upsert_heartbeat(pool, ENTITY, beat(
            WITHHELD, "box-w", capabilities=["agent-observe"],
            schedulers=[{"kind": "cron", "name": "INVENTED-withheld-sched", "command": "x"}],
            enforcing={"anchored": True, "is_anchor": False, "pairing_caps": ["inference"]}))
        for tid in (TWIN1, TWIN2):
            await topology.upsert_heartbeat(pool, ENTITY, beat(tid, "box-twin", capabilities=["agent-observe"]))
        await nac_agents.ingest(pool, BOX, "box-a", [
            {"pid": 4101, "ppid": 1, "signature": "claude-code", "kind": "agent", "user": "invented",
             "cmdline": f"claude --token={KEY_IN_CMD} -p hello", "exe": "/usr/local/bin/claude",
             "unit": "user@1000.service", "owner_class": "local_user", "owner_admin": "no",
             "owner_interactive": "yes", "delegation_token": TAG, "origin": "interactive"},
            {"pid": 4102, "signature": "aider", "kind": "agent", "cmdline": "aider", "origin": "scheduler"}])
        await pool.execute("UPDATE nac_agent_activity SET last_seen = now() - interval '1 hour' "
                           "WHERE pid = 4102")
        await nac_agents.ingest(pool, WITHHELD, "box-w",
                                [{"pid": 5101, "signature": "cursor", "kind": "agent", "cmdline": "cursor"}])
        await pool.execute(
            "INSERT INTO oversight_findings (key, severity, source, machine, evidence) VALUES "
            "($1, 'high', 'store', 'box-a', $2::jsonb), "
            "('box-a|posture:ssh|', 'warn', 'heartbeat', 'box-a', '{}'::jsonb)",
            f"box-a|detect:process|{FP}",
            json.dumps({"fp": FP, "kind": "process", "level": "high", "exe": "/tmp/invented-miner",
                        "title": "A program the baseline does not hold runs on box-a",
                        "user": "invented", "reason": "not in the baseline"}))

        reader = await connection(pool, read=True)
        blind = await connection(pool, read=False)

        # ── R ──
        reads = {"list_agents": {"machine": "box-a"}, "list_scheduled": {"machine": "box-a"},
                 "tool_server_configs": {"machine": "box-a"}, "posture": {"machine": "box-a"},
                 "baseline_delta": {"machine": "box-a"}, "list_machines": {}}
        for name, args in reads.items():
            out, need = await call(pool, blind, name, args)
            check(need is None and out["isError"] is True and "not allowed to see" in out["content"][0]["text"],
                  f"R {name}: refused to an agent connected without 'read'", out)
        recorded = await pool.fetch("SELECT tool, outcome FROM agent_calls WHERE connection_id=$1", blind["id"])
        check(sorted(r["tool"] for r in recorded) == sorted(reads)
              and {r["outcome"] for r in recorded} == {"refused: no read"},
              "R each refusal is recorded under its tool", [dict(r) for r in recorded])
        answers = {}
        for name, args in reads.items():
            out, need = await call(pool, reader, name, args)
            answers[name] = out
            check(need is None and out["isError"] is False, f"R {name}: answers with 'read'", out)
        ok = await pool.fetch("SELECT tool, server, outcome FROM agent_calls WHERE connection_id=$1",
                              reader["id"])
        check(sorted(r["tool"] for r in ok) == sorted(reads) and {r["outcome"] for r in ok} == {"ok"}
              and {r["server"] for r in ok if r["tool"] in tools.READ_TOOLS} == {"box-a"},
              "R each read is recorded, naming the machine", [dict(r) for r in ok])
        names = [t["name"] for t in tools.TOOLS]
        check(names == ["list_machines", "list_agents", "list_scheduled", "tool_server_configs",
                        "posture", "baseline_delta", "contain", "machine_op"],
              "R the tool list carries the bastion's read names, contain and machine_op", names)
        check(all(t["inputSchema"].get("required", [None])[0] == "machine"
                  for t in tools.TOOLS if t["name"] in tools.READ_TOOLS + ("contain",)),
              "R every per-machine tool takes `machine` first")

        ag = answers["list_agents"]["structuredContent"]
        by_pid = {a["pid"]: a for a in ag["agents"]}
        a = by_pid.get(4101, {})
        check(set(by_pid) == {4101} and a.get("exe") == "/usr/local/bin/claude"
              and a.get("unit") == "user@1000.service" and a.get("owner_class") == "local_user"
              and a.get("user") == "invented" and a.get("origin") == "interactive"
              and a.get("active") is True and a.get("requested") is True,
              "R list_agents: the active agent with its owner, exe and unit", ag)
        out, _ = await call(pool, reader, "list_agents", {"machine": "box-a", "include_inactive": True})
        check({x["pid"] for x in out["structuredContent"]["agents"]} == {4101, 4102},
              "R list_agents include_inactive adds an agent not seen lately", out)
        sc = answers["list_scheduled"]["structuredContent"]
        check(sc.get("schedulers_read") is True and len(sc.get("schedules") or []) == 1
              and sc["schedules"][0]["agent"] == "aider" and sc["schedules"][0]["schedule"] == "0 3 * * *",
              "R list_scheduled: the cron job that starts an agent", sc)
        ts = answers["tool_server_configs"]["structuredContent"]
        grp = (ts.get("clients") or [{}])[0]
        check(grp.get("client") == "claude-code" and grp.get("with_problems") == 1
              and grp["servers"][0]["problems"][0] == "a key is written in plain text in its settings file"
              and any("no fixed version" in p for p in grp["servers"][0]["problems"])
              and grp["servers"][1]["problems"] == [],
              "R tool_server_configs: per agent, problems first, in the console's words", ts)
        po = answers["posture"]["structuredContent"]
        check((po.get("posture") or {}).get("ufw") == "active" and (po.get("posture") or {}).get("sec") == 3
              and po.get("open_findings") == {"critical": 0, "high": 1, "warn": 1, "notice": 0}
              and po.get("detect_state") and po.get("alerts_state"),
              "R posture: the host checks, the detect and alert states and the ledger's open counts", po)
        bd = answers["baseline_delta"]["structuredContent"]
        check((bd.get("summary") or {}).get("baseline") == "ok" and (bd.get("summary") or {}).get("high") == 1
              and [f.get("fp") for f in bd.get("findings") or []] == [FP]
              and bd["findings"][0].get("exe") == "/tmp/invented-miner",
              "R baseline_delta: the last pass's summary and the open delta findings, not other kinds", bd)

        # ── S ──
        wire = json.dumps(answers, default=str)
        check(CFG_PATH not in wire and '"path"' not in wire,
              "S no tool server settings-file path reaches the agent")
        check(KEY_IN_CMD not in wire and CRON_KEY not in wire and "<redacted>" in wire,
              "S secrets in command lines are replaced, as the machine's console does")
        check(TAG not in wire and "delegation_token" not in wire,
              "S no delegation tag value reaches the agent; carries_delegation says it was there")

        # ── N ──
        n_calls = await pool.fetchval("SELECT count(*) FROM agent_calls WHERE connection_id=$1", reader["id"])
        for name in tools.READ_TOOLS:
            out, _ = await call(pool, reader, name, {"machine": "box-invented"})
            check(out["isError"] is True and "not one of the person's machines" in out["content"][0]["text"],
                  f"N {name}: a machine this Host has no record of is refused", out)
            out, _ = await call(pool, reader, name, {"machine": "box-twin"})
            check(out["isError"] is True and "report the name" in out["content"][0]["text"],
                  f"N {name}: a name two machines share is refused, never guessed", out)
            out, _ = await call(pool, reader, name, {"machine": "*"})
            check(out["isError"] is True, f"N {name}: '*' is not a machine", out)
        outcomes = [r["outcome"] for r in await pool.fetch(
            "SELECT outcome FROM agent_calls WHERE connection_id=$1 ORDER BY at, id OFFSET $2",
            reader["id"], n_calls)]
        check(outcomes.count("refused: no such machine") == 5 and outcomes.count("refused: name shared") == 5
              and outcomes.count("refused: bad name") == 5,
              "N each refusal is recorded", outcomes)
        out, _ = await call(pool, reader, "list_agents", {"machine": "box-w"})
        sw = out["structuredContent"]
        check(sw.get("agent_watch") == "withheld" and sw.get("agents") == [],
              "N a pairing that withholds observe: its watch state, none of its stored agents", sw)
        out, _ = await call(pool, reader, "list_scheduled", {"machine": "box-w"})
        check("INVENTED-withheld-sched" not in json.dumps(out), "N ...nor its stored schedules", out)

        # ── C ──
        n = len(RAN)
        out, need = await call(pool, reader, "contain", {"machine": "box-a", "pid": 4101, "note": "stop it"})
        check(out is None and need is not None and need.url.endswith(f"/agent-access/ask/{need.ask_id}")
              and len(RAN) == n,
              "C contain without a permission stops with NeedsApproval and its link; nothing runs", out)
        ask = await pool.fetchrow("SELECT kind, server, op, args FROM agent_asks WHERE id=$1",
                                  need.ask_id if need else "")
        check(ask is not None and (ask["kind"], ask["server"], ask["op"], json.loads(ask["args"]))
              == ("grant", "box-a", "contain", ["4101"]),
              "C the ask is the one machine_op files for ('contain', [pid]) on that machine",
              dict(ask) if ask else None)
        _, need2 = await call(pool, reader, "machine_op", {"server": "box-a", "op": "contain", "args": ["4101"]})
        check(need2 is not None and need is not None and need2.ask_id == need.ask_id,
              "C machine_op contain with the same pid is the same open ask, the same link")
        out, need3 = await call(pool, reader, "contain", {"machine": "box-invented", "pid": 4101})
        check(need3 is None and out["isError"] is True
              and await pool.fetchval("SELECT count(*) FROM agent_asks WHERE server='box-invented'") == 0,
              "C contain on a machine this Host has no record of: refused, no ask filed", out)
        for bad in ({"machine": "box-a"}, {"machine": "box-a", "pid": 1, "finding": FP},
                    {"machine": "box-a", "pid": 0}, {"machine": "box-a", "pid": True},
                    {"machine": "box-a", "pid": "4101; reboot"}, {"machine": "box-a", "finding": "nope"}):
            out, nd = await call(pool, reader, "contain", bad)
            check(nd is None and out["isError"] is True, f"C contain refuses {bad}", out)
        await store.decide_ask(pool, ENTITY, need.ask_id, store.ONCE)
        out, need4 = await call(pool, reader, "contain", {"machine": "box-a", "pid": 4101})
        check(need4 is None and out["isError"] is False and RAN[-1] == ("box-a", "contain", ["4101"], "agent"),
              "C after 'just this once' contain reaches run_mgmt_op as op contain [pid], origin agent",
              (out, RAN[-1:]))
        out, need5 = await call(pool, reader, "contain", {"machine": "box-a", "pid": 4101})
        check(need5 is not None, "C and the once is spent: the next contain asks again")
        _, needf = await call(pool, reader, "contain", {"machine": "box-a", "finding": FP.upper()})
        fa = await pool.fetchrow("SELECT op, args FROM agent_asks WHERE id=$1", needf.ask_id if needf else "")
        check(fa is not None and json.loads(fa["args"]) == [f"finding:{FP}"],
              "C a finding goes as contain finding:<fp>, the op run_mgmt_op takes", dict(fa) if fa else None)
        rec = await pool.fetch("SELECT tool, server, op, outcome FROM agent_calls WHERE connection_id=$1 "
                               "AND tool='contain' ORDER BY at, id", reader["id"])
        check(rec and rec[0]["op"] == "contain" and rec[0]["outcome"] == "asked the person"
              and any(r["outcome"].startswith("ran (once)") for r in rec),
              "C contain calls are recorded under contain with op contain", [dict(r) for r in rec])

        # ── A ──
        check({m["name"] for m in answers["list_machines"]["structuredContent"]["machines"]}
              >= {"box-a", "box-w"}, "A list_machines names the person's machines",
              answers["list_machines"])
        listed = {t["name"] for t in tools.TOOLS}
        for name in ("machines", "run_command"):
            try:
                await tools.call(pool, ENTITY, reader, name, {}, "http://host.test")
                check(False, f"A `{name}` is not callable: it is not in tools/list")
            except KeyError:
                check(name not in listed, f"A `{name}` is not callable: it is not in tools/list")
        unanswered = []
        for name in sorted(listed):
            try:
                await call(pool, reader, name, {})
            except KeyError:
                unanswered.append(name)
        check(unanswered == [], "A and every tool tools/list gives is answered", unanswered)
        n = len(RAN)
        out, _ = await call(pool, reader, "machine_op", {"server": "box-a", "op": "uptime"})
        check(out["isError"] is False and len(RAN) == n + 1 and RAN[-1][:2] == ("box-a", "uptime"),
              "A machine_op still runs a read on 'read'", out)
    finally:
        await pool.close()
    return 0


# Pure: the port of the bastion's redaction (client-go mcpconfig.go).
from server.agent_access.redact import redact_command  # noqa: E402

for raw, want in (
        (f"aider --api-key {KEY_IN_CMD} --model m", "aider --api-key <redacted> --model m"),
        ("claude --token=abc -p hi", "claude --token=<redacted> -p hi"),
        ("curl https://u:pw@h.example/p?q=1", "curl https://<redacted>@h.example/<redacted>"),
        ("env OPENAI_API_KEY=whatever123 run", "env OPENAI_API_KEY=<redacted> run"),
        ("run --key ${env:KEY}", "run --key ${env:KEY}"),
        (f"x {KEY_IN_CMD}", "x <redacted>"),
        ("python run.py --verbose", "python run.py --verbose"),
        # A command line read back as one string has lost its quoting, so a
        # header arrives split over words; its value is still a secret.
        ('curl -H "Authorization: Bearer opaque0token" https://h.example',
         'curl -H "Authorization: Bearer <redacted> https://h.example'),
        ("curl -H X-Api-Key: opaque0token h.example", "curl -H X-Api-Key: <redacted> h.example"),
        ("curl -H Authorization: Basic dXNlcjpwYXNz", "curl -H Authorization: Basic <redacted>"),
        ("curl -H Authorization: Bearer ${TOKEN}", "curl -H Authorization: Bearer ${TOKEN}"),
        ("ssh host -o ServerAliveInterval: 30", "ssh host -o ServerAliveInterval: 30"),
        ("curl -H Authorization: Bearer invented0token9value https://h.example",
         "curl -H Authorization: Bearer <redacted> https://h.example"),
        ("curl -H 'Authorization: Bearer invented0token9value' https://h.example",
         "curl -H 'Authorization: Bearer <redacted> https://h.example"),
        ("curl -H Accept: application/json https://h.example",
         "curl -H Accept: application/json https://h.example"),
        ("curl --header=Authorization: Bearer opaque0token x",
         "curl --header=Authorization: Bearer <redacted> x"),
        # A header name quoted on its own word: the closing quote is not its value.
        ("curl -H 'X-Api-Key:' 'opaque0token' x", "curl -H 'X-Api-Key:' <redacted> x"),
        # curl's -u user:password, and a plain -u user that holds no secret.
        ("curl -u admin:invented0pw https://h.example", "curl -u admin:<redacted> https://h.example"),
        ("curl --user=admin:invented0pw x", "curl --user=admin:<redacted> x"),
        ("curl -u admin:${PW} x", "curl -u admin:${PW} x"),
        # A key given as the user name, with an empty password, is still a key.
        (f"curl -u {KEY_IN_CMD}: x", "curl -u <redacted> x"),
        ("sudo -u bob run", "sudo -u bob run")):
    check(redact_command(raw) == want, f"redact: {want}", redact_command(raw))

# The connect page's 'read' names every observe op the Host may be granted:
# read covers observe:* (tools.READ_GRANT_PREFIXES), so an op added to the
# observe family is one the person is told about when they tick 'read'.
from server.agent_access import pages as _pages, tools as _tools  # noqa: E402
from server.network import grant_vocab as _gv  # noqa: E402
_SAID = {"observe:folders": "filling its folders", "observe:journal": "a service logged",
         "observe:logins": "who tried to sign in", "observe:cert": "a certificate expires"}
for _op in _gv.OBSERVE_NEW:
    check(_tools.read_grant_covers(_op) and _SAID.get(_op, "\0") in _pages.READ_WORDS,
          f"the connect page's 'read' says what {_op} reads", _pages.READ_WORDS)

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
print(f"an outside agent's machine reads and contain: {CHECKS}/{CHECKS} checks passed")
sys.exit(rc)
