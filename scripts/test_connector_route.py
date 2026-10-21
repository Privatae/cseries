#!/usr/bin/env python3
"""Gate: the messaging connector's message route, end to end on a real Host
turn.

WHAT RUNS FOR REAL: the connector routes served by uvicorn over HTTP on
loopback (the connector's own wire: NDJSON read line by line as it streams);
the grant, the pairing and the capability (nac_connector: propose, the wake's
creds, the code, the person's approval with the authenticator code);
brain_service.initialize and brain_service.send_message, the turn registry,
the runtime's whole turn (session.py) and its turn rows; the placement of a
message no reply or platform thread places (thread_pick.place_connector, no
model call) and the Host's own pick in its turn (runtime/thread_choice,
pick_thread through the provider gateway); the reply builder and the picture
renderer's own
refusals; Circles' split and the project adopt; the kernel's schema
(scripts/kernel_schema.py, migrations included) on a real PostgreSQL.

WHAT IS FAKED: the model provider (a local HTTP server speaking the
Anthropic Messages SSE wire, as scripts/test_host_turn_real_call_path_pg.py
runs it) and, as in test_connector_grants.py, the app lane the capability's
delivery wakes and the installed app's manifest. A network guard refuses
every other connection. session.HOLD_S is lowered for the card turn so a
turn holding for its card ends in a second instead of four minutes.

  A  the picture guidance the Host reads names only what the renderer draws
  B  a stranger's message runs no turn, writes no row and reaches no model
  C  a message that points nowhere from a chat the map has never seen makes
     NO pick call: it starts a thread of its own, titled from its first line,
     with the placement line in it; the thread and turn parts arrive while the
     model has not answered; the reply's first text names the thread; the turn
     is the person's own chat turn in that thread (app circles, the thread's
     descriptor) and the message carries the visible header
  D  /sent records each message once; a reattach streams the same parts from
     the start without those reported sent
  E  a reply to a mapped message and a mapped native thread need no pick; a
     native thread the map has never seen lands in the chat's last thread
  F  a re-delivered message starts no second turn and no second pick
  G  the Host's own pick in its turn: its frame lists the open threads and
     its tools hold pick_thread; a pick of another thread moves the exchange
     there (both rows, a marker each side, the map and the reply follow); a
     thread not in the list is refused in the turn and the exchange stays
  G2 two quick messages that point nowhere are placed in order: the second
     lands where the first did and joins its turn as a nudge
  H  a message while the same thread runs in Circles is a nudge into that
     turn: one turn, the running turn's id, `nudged`, mine false and a note
  I  a second connector message while its own turn runs is a nudge (mine);
     the reply comes once, on the first stream
  I2 ...and when that turn ends before it had a session, the nudge goes on as
     a turn of its own, which the first stream follows to its reply
  J  a card waiting at the turn's end is named in the reply with its choices
     and a link to answer it in the Host
  J2 the card's notice goes out when the turn starts holding on it, with what
     the turn wrote before; what it writes after the hold follows, once
  K  a turn the person's approval click starts (a Circles send in the
     thread) is never handed to the connector
  L  commands: /threads lists, /thread <n> switches, /new and /new <text>
     start a new thread with no pick
  M  a closed thread is not a landing place: a reply into it is placed as any
     message is (the chat's last thread), and the Host may move it in its turn
  N  the map follows a Split and a project adopt; a project turn is the
     person's own Workbench-visible turn
  O  no turn ever writes an app:<connector> scope or the connector's app id;
     no picture leaves a row; no reply wakes the browser
  P  past the registry's window a reattach answers gone, with a thread link
  Q  revoking cancels the connector's running turn, ends its stream, and
     refuses reattach, /sent and the next message
  R  a re-pair of the same app for another account reaches none of the old
     grant's replies (/sent and reattach are 404); its own are mapped under it

Server: TEST_DSN / PEER_TEST_DSN, else a throwaway cseries-postgres (docker).
Run: python3 scripts/test_connector_route.py
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import types

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path[:0] = [str(ROOT), str(ROOT / "brain"), str(ROOT / "scripts")]
logging.basicConfig(level=logging.ERROR)
_TMP = tempfile.TemporaryDirectory(prefix="connector-route-")
os.environ.setdefault("TM_JOBS_ROOT", os.path.join(_TMP.name, "tm-jobs"))
# Read by turn_registry at import: a finished turn stays reattachable this
# long. Short, so P can watch it go; long enough for D's reattach.
TTL_S = 8
os.environ["CSERIES_TURN_DONE_TTL_S"] = str(TTL_S)
# The Host's own address, for the links (reachout.resolve_host_origin).
os.environ["CSERIES_URL"] = "http://home.cseries.localhost:8800"
HOST_ORIGIN = "http://home.cseries.localhost:8800/"

EID = 1
MODEL = "claude-opus-5-5"
API_KEY = "sk-ant-gate-connector-route"
APP = "tg-conn"
ACCOUNT = "123456789"
fails: list[str] = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + (("   [" + str(detail)[:1500] + "]") if detail and not cond else ""))
    if not cond:
        fails.append(label)


# ── nothing leaves the box ────────────────────────────────────────────────
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_ALLOWED_PORTS: set[int] = set()
_DB = {"host": "", "port": 0}
BLOCKED: list[str] = []


def _net_guard(event, args):
    if event == "socket.connect":
        addr = args[1]
        if not isinstance(addr, tuple):
            return
        host, port = str(addr[0]), int(addr[1])
        if (host in _LOOPBACK and port in _ALLOWED_PORTS) or port == _DB["port"]:
            return
        BLOCKED.append(f"connect {host}:{port}")
        raise ConnectionRefusedError(111, f"refused by the gate's network guard: {host}:{port}")
    if event == "socket.getaddrinfo":
        host = args[0].decode() if isinstance(args[0], bytes) else str(args[0] or "")
        if host and host not in _LOOPBACK and host != _DB["host"]:
            BLOCKED.append(f"lookup {host}")
            raise socket.gaierror(socket.EAI_NONAME, f"refused by the gate's network guard: {host}")


def _guard_network(dsn: str, *ports: int) -> None:
    from urllib.parse import urlsplit
    u = urlsplit(dsn)
    host, port = u.hostname or "", u.port or 5432
    if not host or host.startswith("/") or host in _LOOPBACK:
        _ALLOWED_PORTS.add(port)
    else:
        _DB["host"], _DB["port"] = host, port
    _ALLOWED_PORTS.update(ports)
    sys.addaudithook(_net_guard)


# ── the fake provider ─────────────────────────────────────────────────────
REQUESTS: list[dict] = []
PICKS: list = []      # one entry per pick call
TURNS: list = []      # one entry per turn model call; ("hold", Event, step) holds it


def _sse_text(*pieces: str, stop="end_turn") -> list[dict]:
    ev = [{"type": "message_start", "message": {"id": "msg_gate", "model": MODEL,
                                                "usage": {"input_tokens": 50, "output_tokens": 1}}},
          {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}]
    ev += [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p}}
           for p in pieces]
    ev += [{"type": "content_block_stop", "index": 0},
           {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 9}},
           {"type": "message_stop"}]
    return ev


def _sse_tool(tool_id: str, name: str, args: dict) -> list[dict]:
    raw = json.dumps(args)
    return [{"type": "message_start", "message": {"id": "msg_gate_t", "model": MODEL,
                                                  "usage": {"input_tokens": 50, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": raw}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 20}},
            {"type": "message_stop"}]


def _tool_names(body: dict) -> list:
    out = []
    for t in (body or {}).get("tools") or []:
        out.append(t.get("name") or (t.get("function") or {}).get("name"))
    return out


def _is_pick(body: dict) -> bool:
    return _tool_names(body) == ["pick_thread"]


def picks_made() -> int:
    return sum(1 for r in REQUESTS if r["pick"])


def turns_asked() -> int:
    return sum(1 for r in REQUESTS if not r["pick"])


async def _provider(request):
    from aiohttp import web
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if request.method != "POST" or request.path != "/v1/messages":
        return web.Response(status=404, text="not served by the gate's provider")
    pick = _is_pick(body)
    REQUESTS.append({"pick": pick, "body": body})
    queue = PICKS if pick else TURNS
    step = queue.pop(0) if queue else (
        (500, '{"type":"error","error":{"type":"api_error","message":"no pick scripted"}}')
        if pick else _sse_text("(an unscripted turn call)"))
    if isinstance(step, tuple) and step and step[0] == "hold":
        await step[1].wait()
        step = step[2]
    if callable(step):          # an answer that depends on what this call was shown
        step = step(body)
    if isinstance(step, tuple):
        return web.Response(status=step[0], text=step[1], content_type="application/json")
    resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    for e in step:
        await resp.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())
        await asyncio.sleep(0)
    await resp.write_eof()
    return resp


def pick_new(title: str) -> list[dict]:
    return _sse_tool(f"toolu_pick_{time.monotonic_ns()}", "pick_thread",
                     {"new": True, "title": title, "reason": f"a new subject: {title}"})


def pick_thread(key: str) -> list[dict]:
    return _sse_tool(f"toolu_pick_{time.monotonic_ns()}", "pick_thread",
                     {"thread": key, "reason": "it continues that conversation"})


# ── the connector's side of the wire ──────────────────────────────────────

class Stream:
    """One NDJSON response, read line by line as it arrives."""

    def __init__(self, client, method: str, path: str, headers: dict, body=None):
        self.q: asyncio.Queue = asyncio.Queue()
        self.status = None
        self.text = ""
        self.error = None
        self.lines: list[dict] = []
        self.closed = asyncio.Event()
        self._task = asyncio.create_task(self._run(client, method, path, headers, body))

    async def _run(self, client, method, path, headers, body):
        try:
            async with client.stream(method, path, headers=headers,
                                     json=body if method == "POST" else None) as r:
                self.status = r.status_code
                if r.status_code != 200:
                    self.text = (await r.aread()).decode()
                    return
                async for ln in r.aiter_lines():
                    if ln.strip():
                        obj = json.loads(ln)
                        if obj.get("type") != "alive":
                            self.lines.append(obj)
                            await self.q.put(obj)
        except Exception as e:  # noqa: BLE001
            self.error = e
        finally:
            self.closed.set()

    async def next(self, timeout=20.0):
        try:
            return await asyncio.wait_for(self.q.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def all(self, timeout=90.0) -> list[dict]:
        await asyncio.wait_for(self.closed.wait(), timeout)
        return self.lines


def parts(lines: list[dict]) -> list[dict]:
    return [x for x in lines if x.get("type") in ("text", "image", "link")]


def of(lines, t):
    return next((x for x in lines if x.get("type") == t), None) or {}


async def wait_for(cond, timeout=20.0, step=0.05):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        got = cond()
        if asyncio.iscoroutine(got):
            got = await got
        if got:
            return True
        await asyncio.sleep(step)
    return False


async def install_row(pool, app_id: str, provenance: str = "custom") -> None:
    from server.applications import registry
    from server.applications.manifest import BuildSpec, LicenseSpec, SourceSpec
    await registry.register(pool, types.SimpleNamespace(
        id=app_id, name=app_id.title(), version="1.0.0", provenance=provenance,
        entrypoint="index.html", permissions=[], source_dir=None, license=LicenseSpec(),
        source=SourceSpec(), build=BuildSpec()))


async def _drain(agen) -> None:
    async for _ in agen:
        pass


async def main(dsn: str) -> int:
    import asyncpg
    import httpx
    import uvicorn
    from gate_loopback import IDLE_KEEP_ALIVE_S
    from aiohttp import web
    from fastapi import FastAPI

    # the provider
    papp = web.Application()
    papp.router.add_route("*", "/{tail:.*}", _provider)
    prunner = web.AppRunner(papp)
    await prunner.setup()
    psite = web.TCPSite(prunner, "127.0.0.1", 0)
    await psite.start()
    pport = psite._server.sockets[0].getsockname()[1]
    # the Host's HTTP side
    hsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    hsock.bind(("127.0.0.1", 0))
    hport = hsock.getsockname()[1]
    _guard_network(dsn, pport, hport)

    os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{pport}"
    os.environ["ANTHROPIC_API_KEY"] = API_KEY
    os.environ["ENTITY_ID"] = str(EID)
    os.environ["DATABASE_URL"] = dsn
    os.environ.pop("RECALL_DATABASE_URL", None)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

    c = await asyncpg.connect(dsn)
    try:
        await c.execute(
            "UPDATE entity_registry SET behavioral_params = coalesce(behavioral_params, '{}'::jsonb) "
            "|| jsonb_build_object('model_routing', jsonb_build_object('chat', $2::jsonb)), "
            "permission_overrides = $3::jsonb WHERE id = $1",
            EID, json.dumps({"provider": "anthropic", "model": MODEL}),
            json.dumps({"bot_capabilities": 255}))
    finally:
        await c.close()

    from server import brain_service as BS
    from server.turn_registry import get_turn_registry, DONE_TTL_S
    from server.network import nac_connector as nc, nac_stepup
    from server.applications import harness_backend as hb
    from server.applications.entity_signing import ensure_entity_signing_key
    from server.connectors import picture, turns as ct, thread_pick as tp
    from server.routes import connector as conn_routes
    from mastercode import circles_threads as cth
    from mastercode.runtime import session as S
    from mastercode.runtime import app_verify

    check("the registry keeps a finished turn for the gate's short window", DONE_TTL_S == TTL_S, DONE_TTL_S)

    # No reply may wake the browser: the one way to it is recorded.
    BROWSER: list = []

    async def _no_browser(*a, **k):
        BROWSER.append((a, k))
        raise RuntimeError("the browser was woken")
    app_verify.browser_sidecar_post = _no_browser

    await BS.initialize(EID)
    server = serve = None
    try:
        pool = BS.get_pool()
        # ── the grant, through the real pairing ──
        for app_id, prov in ((APP, "custom"), ("circles", "native"), ("network", "native")):
            await install_row(pool, app_id, prov)
        manifest = types.SimpleNamespace(
            runtime=types.SimpleNamespace(kind="process", image="python", lifecycle="resident"),
            network=["api.telegram.org"])
        nc._load_manifest = lambda app_id: manifest

        async def _lane(*a, **k):
            return False
        hb.recycle_app = hb._release_and_bill_app = hb.ensure_awake_app = _lane
        secret = await nac_stepup.ensure_stepup_secret(pool)
        await nac_stepup.mark_enrolled(pool)
        await ensure_entity_signing_key(pool, EID)
        draft = await nc.propose_connector(pool, app_id=APP, platform="telegram")
        gid = int(draft["draft_id"])
        creds, h = nc.pending_creds(APP)
        await nc.on_wake_answer(APP, h, True)
        CAP = creds[nc.CAP_CREDS_KEY]
        await nc.submit_code(pool, app_id=APP, code=nc._codes[gid][0], account_id=ACCOUNT,
                             display_name="Alex R")
        await pool.execute("DELETE FROM kernel_secrets WHERE id = 'nac_stepup_last_counter'")
        await nc.approve_connector(pool, EID, gid, stepup_code=nac_stepup.current_code(secret), via="ui")
        H = {"X-CSeries-App-Id": APP, "X-CSeries-Connector-Cap": CAP}

        api = FastAPI()
        api.include_router(conn_routes.router)
        server = uvicorn.Server(uvicorn.Config(api, log_level="warning", lifespan="off",
                                               timeout_keep_alive=IDLE_KEEP_ALIVE_S))
        serve = asyncio.create_task(server.serve(sockets=[hsock]))
        await wait_for(lambda: server.started, 10)
        http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{hport}", timeout=httpx.Timeout(120.0))
        reg = get_turn_registry()

        def msg(mid, text, **kw):
            return {"chat_id": "42", "message_id": str(mid), "text": text, "account_id": ACCOUNT,
                    "display_name": "Alex R", **kw}

        def post(body):
            return Stream(http, "POST", "/api/connector/messages", H, body)

        async def sent(turn_id, pairs, **kw):
            return await http.post("/api/connector/messages/sent", headers=H, json={
                "turn_id": turn_id, "chat_id": "42",
                "sent": [{"seq": s, "message_id": str(m)} for s, m in pairs], **kw})

        async def map_rows(where="TRUE", *args):
            return await pool.fetch(f"SELECT * FROM connector_messages WHERE {where} ORDER BY id", *args)

        async def turn_row(content_like: str, wait_s=30.0):
            row = None

            async def _got():
                nonlocal row
                row = await pool.fetchrow(
                    "SELECT id, content, project_id, metadata FROM entity_memory WHERE entity_id=$1 "
                    "AND memory_type='turn' AND content LIKE $2 ORDER BY id DESC LIMIT 1",
                    EID, content_like)
                return row is not None
            await wait_for(_got, wait_s)
            if row is None:
                return None
            md = row["metadata"]
            return {**dict(row), "metadata": json.loads(md) if isinstance(md, str) else (md or {})}

        # ── A ──
        print("[A] the picture guidance names only what the renderer draws")
        g = ct.PICTURE_GUIDANCE
        named_ok = ["div", "p", "span", "strong", "b"] + [f"h{i}" for i in range(1, 7)]
        check("A every tag it allows is in picture.ALLOWED_TAGS",
              all(t in picture.ALLOWED_TAGS for t in named_ok), sorted(picture.ALLOWED_TAGS))
        check("A what it forbids is not: lists, tables, SVG, images, italics, script",
              not {"ul", "ol", "li", "table", "tr", "td", "svg", "img", "em", "i", "script"}
              & picture.ALLOWED_TAGS)
        check("A display:flex draws and grid does not",
              bool(picture._PROPERTY_RE["display"].match("flex"))
              and not picture._PROPERTY_RE["display"].match("grid"))
        check("A backgrounds are solid or linear-gradient, never url()",
              bool(picture._PROPERTY_RE["background-image"].match("linear-gradient(#ffffff, #000000)"))
              and not picture._PROPERTY_RE["background-image"].match("url(/x.png)")
              and "background-color" in picture.ALLOWED_PROPERTIES)
        check("A borders and radii draw", "border" in picture.ALLOWED_PROPERTIES
              and "border-radius" in picture.ALLOWED_PROPERTIES)
        check("A the guidance says all of that, in words",
              all(w in g for w in ("display:flex", "linear-gradient", "No lists",
                                   "tables", "SVG", "grid", "images", "italics", "script")), g)
        # The spellings it names are the ones check_page passes, because the
        # natural ones are refused: an inline style= (every one), the
        # `background` shorthand, a compound selector, rem, line-height and a
        # named font. Measured here on picture.check_page itself.

        def _passes(page: str) -> bool:
            try:
                picture.check_page(page, {})
                return True
            except picture.Refused:
                return False
        named = ("background-color", "background-image", "color", "border", "border-radius")
        check("A each CSS property it names as drawable is on the allowlist",
              all(p in g and p in picture.ALLOWED_PROPERTIES for p in named)
              and "line-height" in g and "line-height" not in picture.ALLOWED_PROPERTIES
              and "font-family" in g, g)
        card = ('<style>.card{display:flex;padding:16px;background-color:#123456;'
                'background-image:linear-gradient(#123456, #345678);color:#ffffff;'
                'border:1px solid #cccccc;border-radius:8px} h2{color:#ffffff} p{display:flex}'
                '</style><div class="card"><h2>Rome</h2><p>Four days, <strong>2,400 EUR</strong>'
                '</p></div>')
        check("A a card written the way it says passes the allowlist", _passes(card))
        check("A ...and text beside a strong in a box that is not flex is drawn too, so the "
              "guidance asks for no flex there",
              _passes(card.replace(" p{display:flex}", "")) and "text sits beside" not in g, g)
        check("A it says where the CSS goes: a <style> block by tag, .class or #id, no style=",
              "<style> block" in g and "no style= attributes" in g and ".class" in g and "#id" in g, g)
        check("A ...because an inline style, the background shorthand, a compound selector, rem "
              "and a named font are each refused",
              not _passes('<div style="display:flex"><h2>Rome</h2></div>')
              and not _passes('<style>.c{background:#123456}</style><div class="c">x</div>')
              and not _passes('<style>.c h2{color:#ffffff}</style><div class="c"><h2>x</h2></div>')
              and not _passes('<style>.c{border-radius:0.5rem}</style><div class="c">x</div>')
              and _passes('<style>.c{border-radius:0.5em}</style><div class="c">x</div>')
              and not _passes('<style>.c{font-family:sans-serif}</style><div class="c">x</div>'))
        mfh = ct.message_for_host("telegram", "Hello there")
        check("A the message names the platform, then the person's words, then the picture note, "
              "all visible text",
              ct.message_header("telegram") == "[Telegram message]"
              and mfh == f"[Telegram message]\n\nHello there\n\n[{g}]", mfh)
        long_words = "Please " + "plan the whole trip " * 22 + "END-OF-WORDS"
        check("A a running turn's nudge frame (its first 500 characters) still holds the "
              "person's words up to 470 characters",
              len(long_words) <= 470
              and "END-OF-WORDS" in ct.message_for_host("telegram", long_words)[:500],
              len(long_words))

        # ── B ──
        print("[B] a stranger")
        n_req = len(REQUESTS)
        s = post({**msg(1, "hello"), "account_id": "999"})
        await s.all()
        check("B another account is refused, 403", s.status == 403, (s.status, s.text))
        check("B ...and writes no row", not await map_rows())
        check("B ...and no model call, no turn", len(REQUESTS) == n_req and not reg.list_turns(),
              reg.list_turns())
        s = post({k: v for k, v in msg(2, "hello").items() if k != "account_id"})
        await s.all()
        check("B no account at all is refused the same way", s.status == 403, s.status)
        s = Stream(http, "POST", "/api/connector/messages",
                   {"X-CSeries-App-Id": APP, "X-CSeries-Connector-Cap": CAP + "x"}, msg(3, "hi"))
        await s.all()
        check("B a wrong capability is refused by the gate", s.status == 403, s.status)

        # ── C ──
        print("[C] a message that points nowhere: placed with no pick call; the parts come early")
        E1 = asyncio.Event()
        VISUAL = ('<div style="display:flex;padding:16px;background-color:#123456;border-radius:8px">'
                  '<h2 style="color:#ffffff">Rome budget</h2></div>')
        PICKS[:] = []
        TURNS[:] = [("hold", E1, _sse_text("Here is the plan.\n\n```visual\n", VISUAL, "\n```\n\nEnjoy."))]
        n_pick = picks_made()
        s1 = post(msg(100, "Plan my trip to Rome"))
        th = await s1.next(60) or {}
        tu = await s1.next(10) or {}
        asked = await wait_for(lambda: turns_asked() >= 1, 30)
        early = await s1.next(1.5)
        check("C the thread part comes first: a new thread, titled from the message",
              th.get("type") == "thread" and th.get("kind") == "thread"
              and th.get("title") == "Plan my trip to Rome"
              and th.get("changed") is True, (th, s1.status, s1.text, s1.error))
        check("C the turn part comes at once: started, with a turn id",
              tu.get("type") == "turn" and tu.get("state") == "started" and tu.get("turn_id"), tu)
        check("C both arrived while the model had been asked and had NOT answered",
              asked and not E1.is_set() and early is None, (asked, early))
        check("C no pick call: the message was placed in software", picks_made() == n_pick,
              picks_made() - n_pick)
        T_B = th.get("ref") or "00000000-0000-4000-8000-000000000001"
        TURN1 = tu.get("turn_id") or ""
        trow = await pool.fetchrow("SELECT title, origin FROM circles_threads WHERE id=$1::uuid", T_B)
        check("C the new thread is a circles_threads row, origin user, titled from the message",
              trow and trow["title"] == "Plan my trip to Rome" and trow["origin"] == "user", trow)
        marker = await pool.fetchrow(
            "SELECT content FROM entity_memory WHERE metadata->>'source_kind' = $1 "
            "AND metadata->'source_context'->>'ref' = $2", tp.PICK_MARKER_KIND, T_B)
        check("C the placement line is in that thread",
              marker and marker["content"].startswith("Telegram message, started this thread"), marker)
        E1.set()
        lines1 = await s1.all()
        p1 = parts(lines1)
        check("C the stream ends with done", lines1 and lines1[-1].get("type") == "done", lines1[-2:])
        check("C the first text names the thread", p1 and p1[0]["type"] == "text"
              and p1[0]["text"].startswith("New thread: Plan my trip to Rome\n\nHere is the plan."), p1[:1])
        check("C parts are numbered in order", [p["seq"] for p in p1] == list(range(len(p1))), p1)
        drew = any(p["type"] == "image" for p in p1)
        undrawn = any(p["type"] == "text" and "picture I couldn't draw" in p["text"] for p in p1)
        link = next((p for p in p1 if p["type"] == "link"), {})
        check("C the visual is a picture, or a line and a link to its thread on the Host's own origin",
              drew or (undrawn and link.get("url", "").startswith(HOST_ORIGIN) and T_B in link["url"]),
              p1)
        check("C the fence's surrounding text stays text", any("Enjoy." in p.get("text", "") for p in p1), p1)
        urow = await turn_row("[Telegram message]%Plan my trip to Rome%") or {}
        check("C the person's turn row carries the visible header, their words and the note",
              str(urow.get("content", "")).startswith("[Telegram message]\n\nPlan my trip to Rome")
              and str(urow.get("content", "")).endswith(f"[{ct.PICTURE_GUIDANCE}]"), urow)
        md = urow.get("metadata") or {}
        check("C it is the person's own chat turn in that thread: app circles, the thread's scope",
              md.get("app_id") == "circles" and md.get("source_context") == {"kind": "thread", "ref": T_B}
              and urow.get("project_id") is None, md)
        inb = await map_rows("external_message_id = '100'")
        check("C the message is in the map: inbound, that thread, that turn",
              len(inb) == 1 and inb[0]["direction"] == "in" and inb[0]["scope_kind"] == "thread"
              and inb[0]["scope_ref"] == T_B and inb[0]["turn_id"] == TURN1, [dict(r) for r in inb])

        # ── D ──
        print("[D] /sent, and a reattach that skips what was sent")
        r = await sent(TURN1, [(0, 500)])
        check("D /sent records the message", r.status_code == 200 and r.json().get("recorded") == 1, r.text)
        r = await sent(TURN1, [(0, 500)])
        check("D the same report again records nothing",
              r.status_code == 200 and r.json().get("recorded") == 0, r.text)
        re1 = await Stream(http, "GET", f"/api/connector/turns/{TURN1}", H).all()
        check("D the reattach starts with the same thread and the turn, done",
              of(re1, "thread") == th
              and of(re1, "turn") == {"type": "turn", "turn_id": TURN1, "state": "done"}, re1[:2])
        check("D ...and the same parts, without the one reported sent", parts(re1) == p1[1:],
              (parts(re1), p1))
        rest = [(p["seq"], 501 + i) for i, p in enumerate(p1[1:])]
        if rest:
            r = await sent(TURN1, rest)
            check("D the rest are recorded",
                  r.status_code == 200 and r.json().get("recorded") == len(rest), r.text)
        re2 = await Stream(http, "GET", f"/api/connector/turns/{TURN1}", H).all()
        check("D once all are sent, a reattach sends no part again",
              not parts(re2) and re2 and re2[-1] == {"type": "done"}, re2)
        r = await http.post("/api/connector/messages/sent", headers=H,
                            json={"turn_id": TURN1, "chat_id": "43", "sent": [{"seq": 0, "message_id": "9"}]})
        check("D a report for another chat is refused", r.status_code == 400, r.text)
        r = await sent("f" * 32, [(0, 9)])
        check("D a reply this connector never had is 404", r.status_code == 404, r.text)

        # ── E ──
        print("[E] a reply to a mapped message, a mapped native thread: no pick")
        n_pick = picks_made()
        TURNS[:] = [_sse_text("Second answer.")]
        l2 = await post(msg(101, "And hotels?", reply_to_message_id="500")).all()
        check("E a reply to the Host's message lands in its thread, no pick call",
              of(l2, "thread").get("ref") == T_B and picks_made() == n_pick, l2[:2])
        check("E the thread did not change, so no line names it",
              of(l2, "thread").get("changed") is False and parts(l2)
              and parts(l2)[0].get("text") == "Second answer.", parts(l2))
        TURN2 = of(l2, "turn").get("turn_id") or ""
        await sent(TURN2, [(0, 503)])
        # A native thread the map has never seen is placed like any message:
        # the chat's last thread (T_B), with no pick call.
        TURNS[:] = [_sse_text("Native one.")]
        n_pick = picks_made()
        l3 = await post(msg(104, "a note for the trip", native_thread_id="nt-1")).all()
        check("E a native thread the map has never seen lands in the chat's last thread, no pick call",
              picks_made() == n_pick and of(l3, "thread").get("kind") == "thread"
              and of(l3, "thread").get("ref") == T_B, l3[:2])
        check("E ...the chat was already in that thread, so no line names it",
              of(l3, "thread").get("changed") is False and parts(l3)
              and parts(l3)[0].get("text") == "Native one.", parts(l3)[:1])
        o_row = await turn_row("[Telegram message]%a note for the trip%") or {}
        md = o_row.get("metadata") or {}
        check("E the turn is that thread's, with no project",
              md.get("source_context") == {"kind": "thread", "ref": T_B}
              and o_row.get("project_id") is None, md)
        check("E ...and nothing is written in the legacy app:circles scope",
              await pool.fetchval(
                  "SELECT count(*) FROM entity_memory WHERE content ILIKE '%a note for the trip%'"
                  " AND metadata->'source_context'->>'kind' = 'app'"
                  " AND metadata->'source_context'->>'ref' = 'circles'") == 0)
        n_pick = picks_made()
        TURNS[:] = [_sse_text("Native two.")]
        l4 = await post(msg(105, "more for the trip", native_thread_id="nt-1")).all()
        check("E the mapped native thread lands there with no pick",
              picks_made() == n_pick and of(l4, "thread").get("ref") == T_B, l4[:2])

        # ── F ──
        print("[F] a re-delivered message")
        n_req, n_turns = len(REQUESTS), len(reg.list_turns())
        l5 = await post(msg(101, "And hotels?", reply_to_message_id="500")).all()
        check("F no second turn, no model call",
              len(REQUESTS) == n_req and len(reg.list_turns()) == n_turns)
        check("F the answer is that message's own reply (its turn id)",
              of(l5, "turn").get("turn_id") == TURN2, l5)
        check("F ...answered as a reattach (done), never as a turn started for it",
              of(l5, "turn").get("state") == "done", l5)

        # ── G ──
        print("[G] the Host's own pick, in its turn")
        # A second thread, so the chat's last thread has another to move to.
        TURNS[:] = [_sse_text("Budget noted.")]
        lnb = await post(msg(108, "/new Budget sheet for the office")).all()
        T_N = of(lnb, "thread").get("ref") or ""
        check("G fixture: /new opened a second thread for the chat", T_N and T_N != T_B, lnb[:2])

        def _turn_tools(body):
            return [t.get("name") for t in (body or {}).get("tools") or []]

        seen_g: dict = {}

        def _pick_trip(body):
            seen_g["tools"] = _turn_tools(body)
            seen_g["text"] = json.dumps(body)
            return _sse_tool(f"toolu_pick_{time.monotonic_ns()}", "pick_thread",
                             {"thread": f"thread:{T_B}", "reason": "it is about the Rome trip"})
        TURNS[:] = [_pick_trip, _sse_text("Booked the train to Rome.")]
        n_pick = picks_made()
        lg = await post(msg(110, "Also book the train for that week")).all()
        TURN_G = of(lg, "turn").get("turn_id") or ""
        check("G no pick call before the turn: the message landed in the chat's last thread",
              picks_made() == n_pick and of(lg, "thread").get("ref") == T_N, lg[:2])
        _gt = seen_g.get("text", "")
        _gi = _gt.find("This Turn")
        check("G the turn holds pick_thread, and its frame lists the other open threads",
              "pick_thread" in seen_g.get("tools", []) and "Thread choice" in _gt
              and f"thread:{T_B}" in _gt, (seen_g.get("tools"), _gt[_gi:_gi + 900] if _gi >= 0 else _gt[:600]))
        urow_g = await turn_row("[Telegram message]%book the train for that week%") or {}
        check("G the person's message moved to the picked thread",
              (urow_g.get("metadata") or {}).get("source_context") == {"kind": "thread", "ref": T_B},
              urow_g.get("metadata"))
        arow_g = await pool.fetchrow(
            "SELECT metadata FROM entity_memory WHERE memory_type = 'turn' AND content = 'Booked the train to Rome.'")
        amd = json.loads(arow_g["metadata"]) if arow_g and isinstance(arow_g["metadata"], str) else (
            dict(arow_g["metadata"]) if arow_g else {})
        check("G the reply was written there too",
              amd.get("source_context") == {"kind": "thread", "ref": T_B}, amd)
        mk_to = await pool.fetchval(
            "SELECT content FROM entity_memory WHERE metadata->>'source_kind' = $1"
            " AND metadata->'source_context'->>'ref' = $2 AND content LIKE '%picked this thread%'",
            tp.PICK_MARKER_KIND, T_B)
        mk_from = await pool.fetchval(
            "SELECT content FROM entity_memory WHERE metadata->>'source_kind' = 'thread_marker'"
            " AND metadata->'source_context'->>'ref' = $1", T_N)
        check("G a marker in each: the pick in the thread it went to, the move where it landed",
              bool(mk_to) and "it is about the Rome trip" in mk_to and bool(mk_from)
              and mk_from.startswith("→ This exchange moved to"), (mk_to, mk_from))
        rows_g = await map_rows("turn_id = $1", TURN_G)
        check("G the map follows: this turn's messages are in the picked thread",
              rows_g and all((r["scope_kind"], r["scope_ref"]) == ("thread", T_B) for r in rows_g),
              [dict(r) for r in rows_g])
        pg_ = parts(lg)
        check("G the reply says where it went", pg_ and pg_[0].get("text", "").startswith("Moved to: "),
              pg_[:1])
        # A pick of a thread the frame did not list is refused in the turn.
        TURNS[:] = [lambda body: _sse_tool(f"toolu_pick_{time.monotonic_ns()}", "pick_thread",
                                           {"thread": "thread:00000000-0000-4000-8000-000000000000",
                                            "reason": "a guess"}),
                    _sse_text("Staying here.")]
        lq = await post(msg(111, "/thread 1")).all()  # back in a thread with others listed
        T_Q = of(lq, "thread").get("ref") or ""
        lr = await post(msg(112, "a quick word on something else")).all()
        urow_r = await turn_row("[Telegram message]%a quick word on something else%") or {}
        check("G a thread not in the list: refused in the turn, the exchange stays where it landed",
              (urow_r.get("metadata") or {}).get("source_context") == {"kind": "thread", "ref": T_Q}
              and of(lr, "thread").get("ref") == T_Q, (urow_r.get("metadata"), lr[:2]))

        # ── G2 ──
        print("[G2] two quick messages that point nowhere are placed in order")
        EB = asyncio.Event()
        TURNS[:] = [("hold", EB, _sse_text("Burst answer.")), _sse_text(" With the second line.")]
        n_pick = picks_made()
        n_threads = await pool.fetchval("SELECT count(*) FROM circles_threads")
        sga = post(msg(120, "Plan a burst"))
        tga = (of(sga.lines, "turn") if await wait_for(lambda: of(sga.lines, "turn"), 30) else {})
        sgb = post(msg(121, "in two lines"))
        await wait_for(lambda: of(sgb.lines, "turn") or sgb.closed.is_set(), 30)
        lgb = list(sgb.lines)
        check("G2 no pick call for either", picks_made() == n_pick, picks_made() - n_pick)
        check("G2 the second landed where the first did and joined its running turn (nudged, mine)",
              of(lgb, "turn").get("state") == "nudged" and of(lgb, "turn").get("mine") is True
              and of(lgb, "turn").get("turn_id") == tga.get("turn_id")
              and of(lgb, "thread").get("ref") == of(sga.lines, "thread").get("ref"), lgb)
        check("G2 ...and no thread was made for either",
              await pool.fetchval("SELECT count(*) FROM circles_threads") == n_threads)
        EB.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await sgb.all(60)
        with contextlib.suppress(asyncio.TimeoutError):
            await sga.all(60)
        lga = list(sga.lines)
        said_g = "".join(p.get("text", "") for p in parts(lga) if p["type"] == "text")
        check("G2 one reply, on the first stream, with both lines in it",
              "Burst answer." in said_g and "With the second line." in said_g
              and lga and lga[-1] == {"type": "done"}, parts(lga))

        # ── H ──
        print("[H] Circles is running that thread: a nudge, not a second turn")
        E2 = asyncio.Event()
        n_asked = turns_asked()
        TURNS[:] = [("hold", E2, _sse_text("Circles answer.")), _sse_text("Folded in.")]
        circ = BS.send_message(entity_id=EID, message="From Circles: what about Florence?",
                               app_id="circles", thread_id=T_B)
        first = await circ.__anext__()
        circ_turn = json.loads(first[2:])[0]["turn_id"]
        drain = asyncio.create_task(_drain(circ))
        await wait_for(lambda: turns_asked() >= n_asked + 1, 30)
        ln = await post(msg(130, "Also Venice?", reply_to_message_id="500")).all()
        tl = of(ln, "turn")
        running = [t for t in reg.list_turns() if t["status"] == "running"]
        check("H the answer is nudged into the running turn: its id, mine false, a note",
              tl.get("state") == "nudged" and tl.get("turn_id") == circ_turn and tl.get("mine") is False
              and "Its reply is in your Host" in (tl.get("note") or ""), ln)
        check("H one turn runs in that thread",
              len(running) == 1 and running[0]["turn_id"] == circ_turn, running)
        rec = reg.get(circ_turn)
        check("H the running turn's stream carries the nudge",
              rec is not None and any('"nudge"' in f and "Also Venice?" in f for f in rec.frames),
              rec.frames[-5:] if rec else None)
        check("H no parts and done", not parts(ln) and ln and ln[-1] == {"type": "done"}, ln)
        E2.set()
        await drain
        s = Stream(http, "GET", f"/api/connector/turns/{circ_turn}", H)
        await s.all()
        check("H the Circles turn is never handed to the connector", s.status == 404, s.status)

        # ── I ──
        print("[I] its own turn running: a nudge, and one reply on the first stream")
        E3 = asyncio.Event()
        TURNS[:] = [("hold", E3, _sse_text("Own answer.")), _sse_text(" With the nudge.")]
        n_asked = turns_asked()
        s6 = post(msg(140, "Book the train", reply_to_message_id="500"))
        await s6.next(30)
        own = await s6.next(10) or {}
        await wait_for(lambda: turns_asked() >= n_asked + 1, 30)
        l7 = await post(msg(141, "First class please", reply_to_message_id="500")).all()
        t7 = of(l7, "turn")
        check("I the second message is a nudge into its own turn: mine, no note, no parts",
              t7.get("state") == "nudged" and t7.get("turn_id") == own.get("turn_id")
              and t7.get("mine") is True and "note" not in t7 and not parts(l7), l7)
        E3.set()
        l6 = await s6.all()
        said = "".join(p.get("text", "") for p in parts(l6) if p["type"] == "text")
        check("I the first stream gets the reply once, with what the nudge brought, then done",
              said.count("Own answer.") == 1 and "With the nudge." in said
              and l6[-1] == {"type": "done"}, parts(l6))
        inb = await map_rows("external_message_id = '141'")
        check("I the nudge is in the map under that turn",
              inb and inb[0]["turn_id"] == own.get("turn_id"), [dict(x) for x in inb])

        # ── I2 ──
        print("[I2] a nudge into its own turn that ended before it had a session")
        # The turn ends before the runtime names its session (the identity
        # hold refused it, or it failed first): the registry sends the nudge
        # on as the next turn (turn_registry._requeue_undelivered). Only the
        # first turn's inner generator is replaced; the turn the nudge goes on
        # as runs the real one.
        GATE = asyncio.Event()
        real_inner = BS._send_message_inner

        async def _no_session(*_a, **_k):
            yield 'f:{"messageId": "gate"}\n'
            await GATE.wait()
            yield "3:" + json.dumps("the Host could not take this one yet") + "\n"
        # brain_service._followup_maker binds the send's arguments against
        # this function's signature: it must be the real one's.
        import inspect
        _no_session.__signature__ = inspect.signature(real_inner)
        BS._send_message_inner = _no_session
        try:
            s8 = post(msg(145, "Check the trains", reply_to_message_id="500"))
            await s8.next(30)
            t8 = await s8.next(10) or {}
            await wait_for(lambda: reg.get(t8.get("turn_id") or "") is not None
                           and reg.get(t8["turn_id"]).frames, 10)
        finally:
            BS._send_message_inner = real_inner
        TURNS[:] = [_sse_text("Trains: the 9:10 is best.")]
        l9 = await post(msg(146, "Prefer mornings", reply_to_message_id="500")).all()
        t9 = of(l9, "turn")
        check("I2 the second message is a nudge into its own turn (mine)",
              t9.get("state") == "nudged" and t9.get("turn_id") == t8.get("turn_id")
              and t9.get("mine") is True, l9)
        GATE.set()
        l8 = await s8.all(60)
        said8 = "\n".join(p.get("text", "") for p in parts(l8) if p["type"] == "text")
        rec8 = reg.get(t8.get("turn_id") or "")
        went = next((json.loads(f[2:])[0].get("turn_id") for f in (rec8.frames if rec8 else [])
                     if f.startswith("2:") and "nudge_requeued" in f), None)
        check("I2 the nudge went on as a turn of its own (the registry's requeue)",
              went is not None and went != t8.get("turn_id"), rec8.frames if rec8 else None)
        check("I2 the first stream follows it: the nudge's reply arrives there, then done",
              "Trains: the 9:10 is best." in said8 and "could not take this one yet" in said8
              and l8 and l8[-1] == {"type": "done"}, l8)
        check("I2 parts are numbered in order across both turns",
              [p["seq"] for p in parts(l8)] == list(range(len(parts(l8)))), parts(l8))
        if went:
            r = await sent(went, [(0, 545)])
            check("I2 /sent naming the followed turn records under the reply's own id",
                  r.status_code == 200 and r.json().get("turn_id") == t8.get("turn_id")
                  and r.json().get("recorded") == 1, r.text)

        # ── J ──
        print("[J] a card waiting at the turn's end")
        S.HOLD_S = 1.0
        TURNS[:] = [_sse_tool("toolu_card", "escalate", {
                        "sitrep": "Approve the Rome budget of 2,400 EUR?\nIt covers the hotel.",
                        "options": [{"id": "yes", "label": "Approve"}, {"id": "no", "label": "Not now"}]}),
                    _sse_text("I put the budget on a card for you.")]
        lj = await post(msg(150, "Can you confirm the budget?", reply_to_message_id="500")).all()
        pj = parts(lj)
        card = next((p for p in pj if p["type"] == "text" and p["text"].startswith("A card is waiting")), {})
        check("J the reply says a card waits on the person's device and names what it asks",
              "“Approve the Rome budget of 2,400 EUR?”" in card.get("text", "")
              and "Approve / Not now" in card.get("text", "")
              and "cannot be answered from Telegram" in card.get("text", ""), pj)
        alink = next((p for p in pj if p["type"] == "link" and p.get("label") == ct.CARD_LINK_LABEL), {})
        check("J ...with a link to answer it in the Host", alink.get("url", "").startswith(HOST_ORIGIN), pj)
        check("J the reply's own text comes first",
              pj and "I put the budget on a card" in pj[0].get("text", ""), pj)

        # ── J2 ──
        print("[J2] a card's notice goes out when the turn starts holding on it")
        S.HOLD_S = 8.0
        TURNS[:] = [_sse_tool("toolu_card2", "escalate", {
                        "sitrep": "Book the Florence hotel for 3 nights?",
                        "options": [{"id": "yes", "label": "Book it"}, {"id": "no", "label": "Not yet"}]}),
                    _sse_text("The hotel is on a card for you."),
                    _sse_text("Booked, since you said so.")]
        sj = post(msg(155, "Find me a hotel in Florence", reply_to_message_id="500"))
        await sj.next(30)
        tj = await sj.next(10) or {}
        recj = reg.get(tj.get("turn_id") or "")
        early_j: list = []
        while True:
            x = await sj.next(6)        # well inside the 8 s hold
            if x is None or x.get("type") == "done":
                break
            early_j.append((x, recj is not None and recj.done.is_set()))
            if x.get("type") == "text" and x.get("text", "").startswith("A card is waiting"):
                break
        check("J2 the card's notice arrives while the turn still holds on it",
              any(p.get("text", "").startswith("A card is waiting") and not ended
                  for p, ended in early_j), early_j)
        check("J2 ...after what the turn wrote before it holds",
              early_j and "The hotel is on a card for you." in early_j[0][0].get("text", ""), early_j)
        # The person answers in the Host: the turn reads it and goes on.
        reg.nudge(tj.get("turn_id") or "", "Yes, book it")
        lj2 = await sj.all(60)
        pj2 = parts(lj2)
        tj2 = [p.get("text", "") for p in pj2 if p["type"] == "text"]
        check("J2 what it writes after the hold follows once; the card is named once; seqs run on",
              sum("A card is waiting" in t for t in tj2) == 1
              and sum("The hotel is on a card for you." in t for t in tj2) == 1
              and sum("Booked, since you said so." in t for t in tj2) == 1
              and [p["seq"] for p in pj2] == list(range(len(pj2)))
              and lj2 and lj2[-1] == {"type": "done"}, pj2)
        rej = await Stream(http, "GET", f"/api/connector/turns/{tj.get('turn_id')}", H).all()
        check("J2 a reattach replays the same parts", parts(rej) == pj2, (parts(rej), pj2))
        S.HOLD_S = 1.0

        # ── K ──
        print("[K] the approval click's turn sends nothing to the connector")
        TURNS[:] = [_sse_text("Approved, booking now.")]
        click = BS.send_message(entity_id=EID, message="Approve", app_id="circles", thread_id=T_B)
        kt = json.loads((await click.__anext__())[2:])[0]["turn_id"]
        await _drain(click)
        s = Stream(http, "GET", f"/api/connector/turns/{kt}", H)
        await s.all()
        check("K a reattach to it is 404 (never this connector's)", s.status == 404, s.status)
        check("K the map has no row for it", not await map_rows("turn_id = $1", kt))

        # ── L ──
        print("[L] commands")
        n_pick, n_asked = picks_made(), turns_asked()
        ll = await post(msg(160, "/threads")).all()
        lt = parts(ll)[0].get("text", "") if parts(ll) else ""
        check("L /threads lists the open threads, numbered, newest first, with no turn",
              lt.startswith("Your open threads, newest first:\n1. ") and "Plan my trip to Rome" in lt
              and turns_asked() == n_asked, lt)
        check("L ...and the old original conversation is not one of them",
              "The original conversation" not in lt, lt)
        num = next((x.split(".")[0] for x in lt.splitlines() if "Plan my trip to Rome" in x), "1")
        ls = await post(msg(161, f"/thread {num}")).all()
        check("L /thread <n> switches and says where",
              parts(ls) and parts(ls)[0].get("text") == "Back in: Plan my trip to Rome"
              and of(ls, "thread").get("ref") == T_B and turns_asked() == n_asked, ls)
        lb = await post(msg(162, "/new")).all()
        check("L a bare /new says the next message starts a thread",
              parts(lb) and parts(lb)[0].get("text") == "Your next message starts a new thread.", lb)
        TURNS[:] = [_sse_text("Tax noted.")]
        lnew = await post(msg(163, "Tax questions for 2026")).all()
        check("L ...and it does, titled from the message, with no pick",
              of(lnew, "thread").get("kind") == "thread"
              and of(lnew, "thread").get("title") == "Tax questions for 2026"
              and parts(lnew) and parts(lnew)[0].get("text", "").startswith("New thread: Tax questions for 2026")
              and picks_made() == n_pick, lnew[:3])
        T_TAX = of(lnew, "thread").get("ref") or ""
        TURNS[:] = [_sse_text("Garden noted.")]
        lg2 = await post(msg(164, "/new Garden plan")).all()
        check("L /new <text> starts a thread for that text, no pick",
              of(lg2, "thread").get("title") == "Garden plan" and picks_made() == n_pick
              and await turn_row("[Telegram message]%Garden plan%") is not None, lg2[:3])
        check("L commands never reached the Host as the person's message",
              not await pool.fetchval("SELECT count(*) FROM entity_memory WHERE memory_type = 'turn' "
                                      "AND metadata->>'role' = 'user' AND (content LIKE '%/thread%' "
                                      "OR content LIKE '%/new%')"))

        # ── M ──
        print("[M] a closed thread is not a landing place")
        await cth.patch_thread(pool, EID, T_TAX, closed=True)
        TURNS[:] = [lambda body: _sse_tool(f"toolu_pick_{time.monotonic_ns()}", "pick_thread",
                                           {"thread": f"thread:{T_B}", "reason": "back to the trip"}),
                    _sse_text("Back to the trip.")]
        n_pick = picks_made()
        lm = await post(msg(170, "one more tax thing", reply_to_message_id="163")).all()
        rows_m = await map_rows("external_message_id = '170'")
        check("M a reply into a closed thread is placed with no pick call, never in the closed one",
              picks_made() == n_pick and of(lm, "thread").get("ref") != T_TAX, lm[:2])
        check("M ...and the Host's pick in its turn moved it to the trip",
              rows_m and (rows_m[0]["scope_kind"], rows_m[0]["scope_ref"]) == ("thread", T_B),
              [dict(r) for r in rows_m])

        # ── N ──
        print("[N] the map follows a Split and an adopt")
        anchor = await pool.fetchval(
            "SELECT id FROM entity_memory WHERE content LIKE '%And hotels?%' AND memory_type='turn' "
            "AND metadata->>'role' = 'user'")
        split = await cth.split_thread(pool, EID, T_B, from_turn_id=int(anchor), title="Hotels")
        T_S = str(split["id"])
        moved_503 = dict((await map_rows("external_message_id = '503'"))[0])
        stay_500 = dict((await map_rows("external_message_id = '500'"))[0])
        moved_101 = dict((await map_rows("external_message_id = '101'"))[0])
        check("N after a Split, the moved exchange's messages (in and out) are in the new thread",
              (moved_101["scope_kind"], moved_101["scope_ref"]) == ("thread", T_S)
              and (moved_503["scope_kind"], moved_503["scope_ref"]) == ("thread", T_S),
              (moved_101, moved_503))
        check("N ...and the earlier exchange's stay", stay_500["scope_ref"] == T_B, stay_500)
        n_pick = picks_made()
        TURNS[:] = [_sse_text("Hotels again.")]
        lh = await post(msg(180, "the cheaper one", reply_to_message_id="503")).all()
        check("N a reply to a moved message lands in the new thread, no pick",
              of(lh, "thread").get("ref") == T_S and picks_made() == n_pick, lh[:2])
        pid = await pool.fetchval("INSERT INTO projects (entity_id, name, status) VALUES ($1, 'Italy', "
                                  "'active') RETURNING id", EID)
        from server.routes.projects import adopt_thread_core
        await adopt_thread_core(pool, EID, pid, "thread", T_S)
        r503 = dict((await map_rows("external_message_id = '503'"))[0])
        check("N after an adopt, the thread's messages are in the project",
              (r503["scope_kind"], r503["scope_ref"]) == ("project", str(pid)), r503)
        TURNS[:] = [_sse_text("Project answer.")]
        lp = await post(msg(181, "book it", reply_to_message_id="503")).all()
        # The chat's last message (180) moved into the project with the adopt,
        # so the chat is already there: not a change.
        check("N a reply lands in the project, no pick, and the chat was already there",
              of(lp, "thread") == {"type": "thread", "kind": "project", "ref": str(pid),
                                   "title": "Project “Italy”", "changed": False}
              and picks_made() == n_pick, lp[:2])
        check("N the chat's last message followed the adopt",
              dict((await map_rows("external_message_id = '180'"))[0])["scope_kind"] == "project")
        prow = await turn_row("[Telegram message]%book it%") or {}
        check("N the project turn is the person's own: project_id set, app circles",
              prow.get("project_id") == pid and (prow.get("metadata") or {}).get("app_id") == "circles", prow)
        from server.routes.brain import chat_history
        hist = await chat_history(project_id=pid, app_id=None, cc_thread_id=None, artifact_id=None,
                                  sctx_kind=None, sctx_ref=None, since=None, until=None,
                                  limit=200, order="asc")
        hist = hist if isinstance(hist, dict) else {}
        check("N the Workbench's project read shows it",
              any("book it" in (t.get("content") or "") for t in (hist.get("turns") or [])),
              [str(t.get("content", ""))[:40] for t in (hist.get("turns") or [])][-4:])

        print("[N] the follow rule on its own: each map row goes with the turn row it belongs to")
        from server.connectors.message_map import _move_set

        def row(i, d, t, turn):
            return {"id": i, "app_id": APP, "direction": d, "created_at": t, "turn_id": turn}
        # turn A: in@1, rows 2 and 2.5, out@3; turn B: in@4, rows 5 and 5.5, out@6
        rows = [row(1, "in", 1, "A"), row(2, "out", 3, "A"), row(3, "in", 4, "B"), row(4, "out", 6, "B")]
        check("N split at B: B's messages move, A's stay",
              sorted(_move_set(rows, [5, 5.5], [2, 2.5])) == [3, 4], _move_set(rows, [5, 5.5], [2, 2.5]))
        late = [row(1, "in", 1, "A"), row(2, "out", 5.2, "A"), row(3, "in", 4, "B")]
        check("N A's reply reported after B's message was said still stays with A (by its turn)",
              _move_set(late, [5, 5.5], [2, 2.5]) == [3], _move_set(late, [5, 5.5], [2, 2.5]))
        running = rows + [row(5, "in", 7, "C")]
        check("N a message whose turn has written nothing yet goes with the tail that moved",
              5 in _move_set(running, [5, 5.5], [2, 2.5]))
        check("N an adopt window that keeps the tail moves A's messages and leaves B's and C's",
              sorted(_move_set(running, [2, 2.5], [5, 5.5])) == [1, 2],
              _move_set(running, [2, 2.5], [5, 5.5]))
        check("N nothing moved, nothing follows", _move_set(rows, [], [2, 2.5, 5, 5.5]) == [])

        # ── O ──
        print("[O] scopes, rows, the browser")
        bad = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE (metadata->'source_context'->>'kind' = 'app' "
            "AND metadata->'source_context'->>'ref' <> 'circles') OR metadata->>'app_id' = $1", APP)
        check("O no row was written in an app:<connector> scope or under the connector's app id",
              bad == 0, bad)
        try:
            await pool.execute(
                "INSERT INTO connector_messages (entity_id, app_id, grant_id, platform, chat_id, "
                "external_message_id, scope_kind, scope_ref, direction) "
                "VALUES (1, $1, $2, 'telegram', '42', 'x', 'app', $1, 'in')", APP, gid)
            check("O the map cannot hold an app:<connector> scope", False)
        except asyncpg.CheckViolationError:
            check("O the map cannot hold an app:<connector> scope", True)
        png = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE content LIKE '%iVBORw0KGgo%' "
            "OR metadata::text LIKE '%iVBORw0KGgo%'")
        gen = await pool.fetchval("SELECT count(*) FROM generated_images")
        check("O no picture left a row (no PNG in a turn row, nothing in generated_images)",
              png == 0 and gen == 0, (png, gen))
        cols = {r["column_name"] for r in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'connector_messages'")}
        check("O the map holds ids and scopes, no text and no picture",
              not cols & {"text", "content", "data", "data_b64", "body"}, sorted(cols))
        check("O no reply woke the browser", not BROWSER, BROWSER)

        # ── P ──
        print("[P] past the registry's window")
        gone = await wait_for(lambda: reg.get(TURN1) is None and TURN1 not in ct._REPLIES, TTL_S + 30)
        lp_ = await Stream(http, "GET", f"/api/connector/turns/{TURN1}", H).all()
        check("P the reply left memory with the registry's window", gone)
        check("P a reattach answers gone, with a link to the thread on the Host's origin",
              of(lp_, "turn") == {"type": "turn", "turn_id": TURN1, "state": "gone"}
              and parts(lp_) and parts(lp_)[0]["type"] == "link"
              and parts(lp_)[0]["url"].startswith(HOST_ORIGIN) and lp_[-1] == {"type": "done"}, lp_)
        r = await sent(TURN1, [(0, 999)])
        check("P /sent still records against the map once the reply is gone",
              r.status_code == 200 and r.json().get("recorded") == 1, r.text)

        # ── Q ──
        print("[Q] revoke cancels the turn in flight")
        E4 = asyncio.Event()
        n_asked = turns_asked()
        TURNS[:] = [("hold", E4, _sse_text("never sent"))]
        sq = post(msg(190, "long job", reply_to_message_id="500"))
        await sq.next(30)
        tq = await sq.next(10) or {}
        await wait_for(lambda: turns_asked() >= n_asked + 1, 30)
        await nc.revoke_connector(pool, app_id=APP, reason="the gate revokes", actor="user")
        recq = reg.get(tq.get("turn_id") or "")
        await wait_for(lambda: recq is not None and recq.done.is_set(), 15)
        check("Q the running turn is cancelled", recq is not None and recq.status == "cancelled",
              recq.status if recq else None)
        lq = await sq.all(15)
        check("Q its stream ends with nothing more: no parts, no done",
              not parts(lq) and not any(x.get("type") == "done" for x in lq), lq)
        s = Stream(http, "GET", f"/api/connector/turns/{tq.get('turn_id')}", H)
        await s.all()
        check("Q a reattach is refused", s.status == 403, s.status)
        r = await sent(tq.get("turn_id") or "x", [(0, 1)])
        check("Q /sent is refused", r.status_code == 403, r.status_code)
        s = post(msg(191, "hello?"))
        await s.all()
        check("Q the next message is refused", s.status == 403, s.status)
        E4.set()

        # ── R ──
        print("[R] a re-pair of the same app, for another account, cannot reach the old grant's replies")
        ACCOUNT2 = "555000111"
        draft2 = await nc.propose_connector(pool, app_id=APP, platform="telegram")
        gid2 = int(draft2["draft_id"])
        creds2, h2 = nc.pending_creds(APP)
        await nc.on_wake_answer(APP, h2, True)
        await nc.submit_code(pool, app_id=APP, code=nc._codes[gid2][0], account_id=ACCOUNT2,
                             display_name="Alex's other account")
        await pool.execute("DELETE FROM kernel_secrets WHERE id = 'nac_stepup_last_counter'")
        await nc.approve_connector(pool, EID, gid2, stepup_code=nac_stepup.current_code(secret), via="ui")
        H2 = {"X-CSeries-App-Id": APP, "X-CSeries-Connector-Cap": creds2[nc.CAP_CREDS_KEY]}
        check("R the new grant is another grant", gid2 != gid, (gid, gid2))
        r = await http.post("/api/connector/messages/sent", headers=H2, json={
            "turn_id": TURN1, "chat_id": "42", "sent": [{"seq": 0, "message_id": "7001"}]})
        check("R /sent against the old grant's reply is 404, and records nothing",
              r.status_code == 404 and not await map_rows("external_message_id = '7001'"),
              (r.status_code, r.text))
        s = Stream(http, "GET", f"/api/connector/turns/{TURN1}", H2)
        await s.all()
        check("R a reattach to the old grant's reply is 404, not its thread link", s.status == 404,
              (s.status, s.lines))
        TURNS[:] = [_sse_text("Hello from the new pairing.")]
        lr = await Stream(http, "POST", "/api/connector/messages", H2,
                          {**msg(200, "hello again", reply_to_message_id="500"),
                           "account_id": ACCOUNT2}).all()
        tr = of(lr, "turn").get("turn_id") or ""
        r = await http.post("/api/connector/messages/sent", headers=H2, json={
            "turn_id": tr, "chat_id": "42", "sent": [{"seq": 0, "message_id": "7002"}]})
        rows2 = await map_rows("external_message_id IN ('200', '7002')")
        check("R the new grant's own message and its send are mapped under the new grant",
              of(lr, "turn").get("state") == "started" and r.status_code == 200
              and r.json().get("recorded") == 1 and len(rows2) == 2
              and all(x["grant_id"] == gid2 for x in rows2), (lr, r.text, [dict(x) for x in rows2]))

        # ── S ──
        print("[S] a message whose turn never starts can be delivered again")
        real_send = BS.send_message

        async def _refusing(**kw):
            yield '3:"the Host is at its turn ceiling"'

        async def _raising(**kw):
            raise RuntimeError("send path failed")
            yield  # noqa: unreachable - makes this an async generator

        for label, fake, mid in (("refused", _refusing, "300"), ("raised", _raising, "301")):
            BS.send_message = fake
            try:
                lr = await Stream(http, "POST", "/api/connector/messages", H2,
                                  {**msg(mid, "retry me", reply_to_message_id="500"),
                                   "account_id": ACCOUNT2}).all()
            finally:
                BS.send_message = real_send
            check(f"S a turn that could not start ({label}) is answered refused, with a reason, "
                  "and the stream ends with done",
                  of(lr, "turn").get("state") == "refused"
                  and any((x.get("text") or "").strip() for x in lr if x.get("type") == "text")
                  and lr and lr[-1].get("type") == "done", lr)
            check(f"S ...({label}) and leaves no row behind",
                  not await map_rows("external_message_id = $1", mid))
            TURNS[:] = [_sse_text("Placed on the second try.")]
            lr = await Stream(http, "POST", "/api/connector/messages", H2,
                              {**msg(mid, "retry me", reply_to_message_id="500"),
                               "account_id": ACCOUNT2}).all()
            check(f"S ...({label}) and the same message delivered again is placed, not a duplicate",
                  of(lr, "turn").get("state") == "started", lr)
        await http.aclose()
    finally:
        if server is not None:
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(serve, 10)
        await BS.shutdown()
        with contextlib.suppress(Exception):
            from mastercode.provider_gateway import close_all_sessions
            await close_all_sessions()
        await prunner.cleanup()

    print()
    if BLOCKED:
        print("network guard refused (each answered as connection refused): "
              + ", ".join(sorted(set(BLOCKED))))
    print("ALL GATES PASSED" if not fails else f"FAILED ({len(fails)}): " + repr(fails))
    return 1 if fails else 0


def _no_database() -> int:
    lane = os.environ.get("GATE_LANE")
    if lane:
        print(f"FAIL — the {lane} lane gave no test server (TEST_DSN/PEER_TEST_DSN) and docker is "
              "unavailable; this gate never writes to a database it did not create.")
        return 1
    print("SKIP — no test server and docker unavailable; this gate drives real Host turns "
          "against a database it creates, and a skip proves nothing.")
    return 2


if __name__ == "__main__":
    import signal

    from kernel_schema import kernel_schema_db, throwaway_server

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    SERVER = os.environ.get("TEST_DSN") or os.environ.get("PEER_TEST_DSN")
    if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        sys.exit(_no_database())
    with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
            kernel_schema_db(_srv, entity_id=EID) as _db:
        print("# the connector route on real Host turns; only the model provider is fake\n")
        _rc = asyncio.run(main(_db))
    sys.exit(_rc)
