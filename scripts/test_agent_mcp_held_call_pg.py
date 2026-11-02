#!/usr/bin/env python3
"""An outside agent's tool call that needs the person is held open while they
answer, and ends with what ran (agent_access.held).

MCP 2025-11-25 URL-mode elicitation: on a call that needs the person's
approval, a client that declared URL elicitation and takes an event stream
gets the approval page as an `elicitation/create` request on the call's own
stream; once it accepts and the person answers on the Host, the stream says
`notifications/elicitation/complete` and ends with the call's result. Claude
Code acts on that by itself (its -32042 path waits for its Retry button).

Driven over real HTTP: the routes under uvicorn on a loopback port, read as a
stream the way an MCP client reads it (an ASGI test client returns a response
only once it ends). The kernel's schema on a throwaway database, so the
'just this once' that the held call spends is the real SQL. The machine side
is stubbed, as in test_agent_access_pg.py.

  * a change from such a client: elicitation/create with the page, then
    progress while it waits, then complete and the result once the person
    allows it; the op ran exactly once
  * the client's answer is taken with 202; decline ends the call, nothing ran
  * no answer in time: nothing ran, and a later answer still reaches the
    session's GET stream
  * a machine's card answered anywhere goes on the same way; a machine that
    waits on its device is not held (-32042, as before)
  * a client that takes no event stream, or past the bounds: -32042, never
    refused
  * a token revoked while held runs nothing; the Host stopping ends held calls

Run:  python3 scripts/test_agent_mcp_held_call_pg.py
      (TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN names a server; without one it
      starts a throwaway one through docker, as the other _pg gates do.)
"""
import asyncio
import contextlib
import html
import json
import os
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]

import asyncpg  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database for the asks a held call waits on.")
    sys.exit(2)

ENTITY = 9001
PERSON = "owner@example.com"  # invented
os.environ["ENTITY_OPERATOR_EMAILS"] = PERSON
os.environ["ENTITY_ID"] = str(ENTITY)

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from gate_loopback import IDLE_KEEP_ALIVE_S  # noqa: E402
from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402
from server import brain_service  # noqa: E402
from server.agent_access import asks as agent_asks, held, origin as origin_mod, sessions, store  # noqa: E402
from server.network import nac_commit, nac_mgmt, nac_stepup  # noqa: E402
from server.routes import mcp as mcp_route, tickets  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:600]) if extra else ""))
    if not cond:
        FAILS.append(msg)


# ── the machine side, stubbed ───────────────────────────────────────────────
RAN: list[tuple] = []
MACHINE_NEXT: list = []


async def fake_run_mgmt_op(pool, eid, server, op, args, *, token_id=None, note="",
                           approval_wait_s=None, origin="service", budget_s=None):
    if MACHINE_NEXT:
        nxt = MACHINE_NEXT.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt
    RAN.append((server, op, list(args or [])))
    return {"server": server, "op": op, "exit": 0, "stdout": f"{op} ok", "stderr": ""}


async def fake_bastions_list(pool, eid):
    return {"bastions": [{"hostname": "box-a", "online": True, "platform": "linux",
                          "bastion_version": "0.61.2", "enforcing": {"mgmt_ops": ["restart_service"]}}]}


async def fake_gate_op(pool, eid, server, op):
    return True, "", "ran"


async def fake_get_ticket(pool, eid, ticket_id):
    return {"id": ticket_id, "status": "open", "sitrep": "box-a asks to run restart_service once."}


async def fake_public_origin(request):
    return origin_mod.request_origin(request)


nac_mgmt.run_mgmt_op = fake_run_mgmt_op
nac_mgmt.gate_op = fake_gate_op
nac_commit.bastions_list = fake_bastions_list
tickets.get_ticket_service = fake_get_ticket
origin_mod.public_origin = fake_public_origin


class Clock:
    def __init__(self):
        self.now = float(int(time.time()) // 30 * 30) + 5

    def time(self) -> float:
        return self.now


CLOCK = Clock()
nac_stepup.time = CLOCK


def next_code(raw: bytes) -> str:
    CLOCK.now += 30
    return nac_stepup._hotp(raw, int(CLOCK.now) // 30)


# ── a client reading the held call's stream ─────────────────────────────────
class HeldCall:
    """One tools/call over a real stream: the messages as they arrive."""

    def __init__(self, c: httpx.AsyncClient, url: str, headers: dict, body: dict):
        self.messages: list = []
        self.status = None
        self.ctype = ""
        self.arrived = asyncio.Event()
        self.task = asyncio.create_task(self._run(c, url, headers, body))

    async def _run(self, c, url, headers, body):
        async with c.stream("POST", url, headers=headers, json=body) as r:
            self.status, self.ctype = r.status_code, r.headers.get("content-type", "")
            if not self.ctype.startswith("text/event-stream"):
                self.messages.append(json.loads(await r.aread()))
                self.arrived.set()
                return
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    self.messages.append(json.loads(line[6:]))
                    self.arrived.set()

    async def next_where(self, pred, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for m in self.messages:
                if pred(m):
                    return m
            self.arrived.clear()
            if self.task.done():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.arrived.wait(), max(0.01, end - time.monotonic()))
        return None

    async def done(self, timeout=10.0):
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(self.task), timeout)
        return self.task.done()


def is_elicit(m):
    return m.get("method") == "elicitation/create"


def response_to(rid):
    return lambda m: m.get("id") == rid and ("result" in m or "error" in m)


def _need(n: int):
    from server.agent_access.tools import NeedsApproval
    return NeedsApproval(f"ask-{n}", f"https://host.example/agent-access/ask/ask-{n}", "approve it",
                         person_words="approve it", answered_here=True)


def _stream_kw(n: int, connection_id: int) -> dict:
    async def rerun():
        return {"content": [{"type": "text", "text": "ran"}]}
    return dict(sid=f"sess-{n}", rid=n, connection_id=connection_id, need=_need(n), progress_token=None,
                rerun=rerun, holdable=lambda need: True, ok=lambda rid, r: {"id": rid, "result": r},
                told_later=lambda a, s: None,
                unheld=lambda need: {"id": n, "error": {"code": -32042, "message": need.message}})


async def bound_is_taken_with_its_check():
    """Two calls of one client pass routes/mcp.py's check before either
    stream starts: the second is answered unheld when its stream starts."""
    held._stopped = False
    held.HELD_PER_CONNECTION_MAX = 1
    a = held.run(**_stream_kw(31, 4242))
    b = held.run(**_stream_kw(32, 4242))
    try:
        first = json.loads((await a.__anext__()).split("data: ", 1)[1])
        second = json.loads((await b.__anext__()).split("data: ", 1)[1])
        check(first.get("method") == "elicitation/create" and held._held.get(4242) == 1
              and (second.get("error") or {}).get("code") == -32042,
              "past the bound when its stream starts: -32042 there, never a second held call",
              (first, second, held._held))
    finally:
        await a.aclose()
        await b.aclose()
        held.HELD_PER_CONNECTION_MAX = 16
    check(held.held_count() == 0 and not held._requests and not held._waiting and not held._stops,
          "a stream closed mid-hold leaves nothing held", (held._held, held._requests, held._waiting))


async def answered_before_the_stream():
    """The person answers between the ask being filed and the held call
    starting to wait on it (a machine's card shows on the Host as the machine
    makes it): the call goes on once the client accepts, never waits it out."""
    held._stopped = False
    kw = _stream_kw(33, 4343)
    held.answered(kw["need"].ask_id)
    old = held.HOLD_S
    held.HOLD_S = 3.0
    g = held.run(**kw)
    try:
        first = json.loads((await g.__anext__()).split("data: ", 1)[1])
        held.respond(kw["sid"], {"jsonrpc": "2.0", "id": first["id"], "result": {"action": "accept"}})
        t0 = time.monotonic()
        rest = [m async for m in g]
    finally:
        held.HOLD_S = old
        await g.aclose()
    check(any("elicitation/complete" in m for m in rest) and '"ran"' in rest[-1]
          and time.monotonic() - t0 < 1.5,
          "an answer given before the call waits on it is not missed: complete and the result at once",
          rest)


async def an_earlier_loop():
    """A held call that waits in another event loop of this process first
    (a test's earlier asyncio.run, a second server): the calls in main's loop
    must still wait, not end at once as if the Host were stopping."""
    old = held.HOLD_S
    held.HOLD_S = 0.05
    try:
        msgs = [m async for m in held.run(**_stream_kw(30, 4141))]
    finally:
        held.HOLD_S = old
    check(len(msgs) >= 2 and "had not answered" in msgs[-1], "a held call in an earlier event loop",
          msgs)


async def main(dsn: str) -> int:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    brain_service.get_pool = lambda: pool
    brain_service._entity_id = ENTITY
    secret = await nac_stepup.ensure_stepup_secret(pool)
    await nac_stepup.mark_enrolled(pool)
    raw = nac_stepup._b32decode(secret)
    app = FastAPI()
    app.include_router(mcp_route.router)
    app.include_router(agent_asks.router)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off",
                                           timeout_keep_alive=IDLE_KEEP_ALIVE_S))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.02)

    # A connected agent, as the connect page leaves one (test_agent_access_pg
    # drives that page itself).
    reg = await store.register_client(pool, ENTITY, "Gate agent", ["http://127.0.0.1:1/cb"])
    cid = (await store.client(pool, ENTITY, reg["client_id"]))["id"]
    await store.connect(pool, ENTITY, cid, read=True)
    code = await store.issue_code(pool, cid, "http://127.0.0.1:1/cb", "x" * 43, base + "/mcp")
    async with pool.acquire() as conn:
        tok = await store._tokens(conn, cid, store._sha(code))
    access = tok["access_token"]

    person = {"X-Authentik-Email": PERSON}
    try:
        async with httpx.AsyncClient(base_url=base, timeout=30) as c:
            r = await c.post("/mcp", headers={"Authorization": f"Bearer {access}"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {"elicitation": {"url": {}}},
                           "clientInfo": {"name": "gate", "version": "1"}}})
            sid = r.headers.get("mcp-session-id", "")
            check(r.status_code == 200 and sid, "initialize: a session", r.text)
            h = {"Authorization": f"Bearer {access}", "Mcp-Session-Id": sid,
                 "Accept": "application/json, text/event-stream"}

            def call(args, rid, token=None):
                params = {"name": "machine_op", "arguments": {"server": "box-a", "op": "restart_service",
                                                              "args": args}}
                if token is not None:
                    params["_meta"] = {"progressToken": token}
                return HeldCall(c, "/mcp", h, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                               "params": params})

            async def reply(req_id, action):
                return await c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": req_id,
                                                             "result": {"action": action}})

            async def allow(ask_url, decision="once"):
                path = urlsplit(ask_url).path
                page = (await c.get(path, headers=person)).text
                m = re.search(r'name="form" value="([^"]+)"', page)
                return await c.post(path, headers=person, data={
                    "form": html.unescape(m.group(1)) if m else "", "decision": decision,
                    "code": next_code(raw)})

            # ── held: the page, progress, then the result ──
            held.PROGRESS_S = 0.2
            hc = call(["nginx"], 11, token="tok-11")
            el = await hc.next_where(is_elicit)
            p = (el or {}).get("params") or {}
            check(hc.status == 200 and hc.ctype.startswith("text/event-stream") and el
                  and p.get("mode") == "url" and "/agent-access/ask/" in p.get("url", "")
                  and p.get("elicitationId") and p.get("url", "").endswith(p.get("elicitationId", "-"))
                  and "restart_service" in p.get("message", "") and "box-a" in p.get("message", ""),
                  "a change from a client that takes URL elicitation and a stream: the call's own stream "
                  "sends elicitation/create with the approval page, in words for the person", hc.messages)
            check(RAN == [], "and nothing ran", RAN)
            r = await reply(el["id"], "accept")
            check(r.status_code == 202, "the client's accept is taken with 202", (r.status_code, r.text))
            prog = await hc.next_where(lambda m: m.get("method") == "notifications/progress")
            check(prog and prog["params"].get("progressToken") == "tok-11",
                  "while it waits, progress on the call's own token (Claude Code's idle timeout)", hc.messages)
            r = await allow(p["url"])
            check(r.status_code == 200, "the person allows it once on their Host", r.text[-200:])
            done = await hc.next_where(lambda m: m.get("method") == "notifications/elicitation/complete")
            res = await hc.next_where(response_to(11))
            check(done and done["params"].get("elicitationId") == p["elicitationId"],
                  "the stream says the elicitation is complete", hc.messages)
            out = (res or {}).get("result") or {}
            check(res and not out.get("isError") and RAN == [("box-a", "restart_service", ["nginx"])]
                  and "restart_service ok" in json.dumps(out),
                  "and ends with the call's result: the op ran, once", (res, RAN))
            check(hc.messages.index(done) < hc.messages.index(res) and await hc.done(),
                  "complete comes before the result, and the stream ends", hc.messages)
            check(held.held_count() == 0 and not held._requests and not held._waiting,
                  "nothing is left held", (held._held, held._requests, held._waiting))

            # ── the rerun fails ──
            # An unheld call that raises gets -32603 (_handle_one); a held one
            # has already started its stream, so its failure is its result.
            hc = call(["haproxy"], 21)
            el = await hc.next_where(is_elicit)
            await reply(el["id"], "accept")
            MACHINE_NEXT.append(RuntimeError("the machine link broke"))
            await allow(el["params"]["url"])
            res = await hc.next_where(response_to(21))
            out = (res or {}).get("result") or {}
            check(out.get("isError") and "the machine link broke" in json.dumps(out) and await hc.done()
                  and held.held_count() == 0,
                  "a rerun that fails still ends the held call with its response", hc.messages)

            # ── decline ──
            n = len(RAN)
            hc = call(["redis"], 12)
            el = await hc.next_where(is_elicit)
            await reply(el["id"], "decline")
            res = await hc.next_where(response_to(12))
            out = (res or {}).get("result") or {}
            check(out.get("isError") and out.get("structuredContent", {}).get("state") == "needs_approval"
                  and "turned down" in out["content"][0]["text"] and len(RAN) == n and await hc.done(),
                  "the person turns down opening the page: the call ends, nothing ran, the link is in it",
                  hc.messages)

            # ── no answer in time ──
            held.HOLD_S = 1.0
            hc = call(["memcached"], 13)
            el = await hc.next_where(is_elicit)
            await reply(el["id"], "accept")
            res = await hc.next_where(response_to(13))
            out = (res or {}).get("result") or {}
            check(out.get("isError") and "had not answered" in out["content"][0]["text"] and len(RAN) == n,
                  "no answer within the hold: the call ends, nothing ran", hc.messages)
            late = el["params"]["elicitationId"]
            check(sid in sessions._asks.get(late, {}),
                  "and a later answer is still sent to the session's GET stream", sessions._asks.get(late))
            held.HOLD_S = 900.0

            # ── a machine's card, answered anywhere ──
            # The agent holds the op 'always', so the call reaches the machine,
            # which holds it on a card for the person.
            await pool.execute("INSERT INTO agent_grants (connection_id, kind, server, op) "
                               "VALUES ($1, 'op', 'box-a', 'restart_service')", cid)
            MACHINE_NEXT.append({"state": "asked", "ticket_id": 9})
            hc = call(["postgres"], 14)
            el = await hc.next_where(is_elicit)
            check(el and "holding" in el["params"]["message"],
                  "a machine holding the op on a card is held too, the page in the person's words", hc.messages)
            await reply(el["id"], "accept")
            told = await agent_asks.card_closed(pool, ENTITY, 9)
            res = await hc.next_where(response_to(14))
            check(told == 1 and res and not (res.get("result") or {}).get("isError")
                  and RAN[-1] == ("box-a", "restart_service", ["postgres"]),
                  "the card answered: the call goes on and runs", (told, hc.messages, RAN[-1:]))

            # ── not held: the device, no stream, past the bound ──
            MACHINE_NEXT.append({"state": "pending_approval", "request": {"via": "device"},
                                 "next": "Approve it on your approval device."})
            hc = call(["mysql"], 15)
            await hc.done()
            res = hc.messages[0] if hc.messages else {}
            check(hc.ctype.startswith("application/json") and (res.get("error") or {}).get("code") == -32042,
                  "a machine waiting on its device is not held: -32042 with the link, as before", res)
            await pool.execute("UPDATE agent_grants SET revoked_at = now() WHERE connection_id=$1 "
                               "AND kind='op'", cid)
            r = await c.post("/mcp", headers={**h, "Accept": "application/json"}, json={
                "jsonrpc": "2.0", "id": 16, "method": "tools/call",
                "params": {"name": "machine_op", "arguments": {"server": "box-a", "op": "restart_service",
                                                              "args": ["mongo"]}}})
            check((r.json().get("error") or {}).get("code") == -32042,
                  "a client that takes no event stream: -32042, as before", r.text)
            held.HELD_PER_CONNECTION_MAX = 1
            first = call(["a1"], 17)
            await first.next_where(is_elicit)
            second = call(["a2"], 18)
            await second.done()
            check(((second.messages or [{}])[0].get("error") or {}).get("code") == -32042,
                  "past the bound of held calls: -32042, never a refusal", second.messages)
            held.HELD_PER_CONNECTION_MAX = 16

            # ── revoked while held ──
            el = await first.next_where(is_elicit)
            await reply(el["id"], "accept")
            await store.revoke_token(pool, ENTITY, access)
            n = len(RAN)
            await allow(el["params"]["url"])
            res = await first.next_where(response_to(17))
            out = (res or {}).get("result") or {}
            check(out.get("isError") and "sign-in ended" in out["content"][0]["text"] and len(RAN) == n,
                  "a token revoked while the call waited runs nothing", first.messages)

            # ── the Host stops ──
            async with pool.acquire() as conn:
                access = (await store._tokens(conn, cid, store._sha(code)))["access_token"]
            h["Authorization"] = f"Bearer {access}"
            hc = call(["b1"], 19)
            el = await hc.next_where(is_elicit)
            await reply(el["id"], "accept")
            t0 = time.monotonic()
            sessions.end_all()
            res = await hc.next_where(response_to(19))
            out = (res or {}).get("result") or {}
            check(out.get("isError") and "restarting" in out["content"][0]["text"]
                  and time.monotonic() - t0 < 5 and await hc.done(),
                  "the Host stopping ends a held call at once: nothing ran", hc.messages)
        await bound_is_taken_with_its_check()
        await answered_before_the_stream()
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serving, 10)
        await pool.close()
    return 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=ENTITY) as _dsn:
    asyncio.run(an_earlier_loop())
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
print(f"an outside agent's held call: {CHECKS}/{CHECKS} checks passed")
sys.exit(rc)
