#!/usr/bin/env python3
"""Gate: words the person types into a running turn are the server's from the
moment it receives them, in the Host's database (pending_words, migration v222).

Measured on one Host: a reply the user typed in Circles while
the Host was mid-turn disappeared when the turn ended. No chat_events
'received' row and no user turn row exist for it. POST /api/circles/nudge had
queued the words in process memory only (session.push_nudge keeps_copy), the
turn ended without reading them, and end_turn_input dropped them from the
server because the page was trusted to send them again from page memory —
which a reload, a closed tab or a dropped connection loses.

The real chain runs: the Circles nudge route (routes/circles.nudge), the turn
registry (server/turn_registry.TurnRegistry), the runtime's queue and its drain
(EntityRuntime.send_message with a scripted model: the harness of
scripts/test_late_nudge_once.py), and pending_words on a fresh clone of the
kernel schema. Only the model, the runtime's other database reads and writes
(answered empty, as in that harness), and the HTTP layer are stood in.

  A. typed mid-turn, the turn ends before reading them (it ends on a model
     error after the words arrive): they start the thread's next turn, on the
     server, whole, with their sender's id; nothing for the page to send.
  B. the page reloads before the turn ends: GET /api/circles/pending lists the
     words (with the id the page drew their line under) while the server holds
     them, and nothing once a turn has them. (The page's half — no copy sent
     again, the line drawn back after a reload — is test_circles_cards.py's
     scenario N.)
  C. the process ends with the words held (its queues and its turn registry
     gone): the boot pass (pending_words.deliver_left, through the real
     brain_service.send_message) starts one turn in the room they were typed
     into, with the words and their id.
  D. no duplicate anywhere: each word reaches the Host exactly once; a second
     boot pass starts nothing; the row is gone once a turn has the words.
  E. words the running turn reads are not delivered again: the row goes when
     the drain reads them, the turn's end sends nothing on, and a restart
     afterwards delivers nothing.

It also runs A-D for every other way words reach a running turn, each of which
once kept them only in memory or on the page:
  W. the Workbench page's route (routes/brain.brain_turn_nudge), and its list
     (GET /api/brain/pending);
  M. a message that lands in the running turn as a nudge through
     brain_service.send_message (another tab, a messaging connector);
  T. a ticket answer the decide pushes into the turn that asked
     (routes/tickets._answer_to_turn -> session.answer_into_turn);
and, in PII mode (PII_MODE=on: nothing may be written),
  P. both nudge routes write nothing and answer that they did not hold the
     words (held: false), the turn still gets them, and the server never
     sends them on itself: the page keeps its copy and sends it (the
     client half is test_circles_cards.py's and test_workbench_book.py's
     scenario N).

Fresh clone of the kernel schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN, else
a throwaway cseries-postgres through docker; no pod, no model, no network beyond
loopback.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pathlib
import subprocess
import sys
import types

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
logging.basicConfig(level=logging.ERROR)
import asyncpg  # noqa: E402

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database to prove the words are held.")
    sys.exit(2)

if "server.main" not in sys.modules:
    # The routes import the app module; a stub stands in for it (as in
    # test_late_nudge_once.py), so no app is mounted here.
    import server as _srvpkg
    _stub = types.ModuleType("server.main")
    sys.modules["server.main"] = _stub
    _srvpkg.main = _stub

from mastercode.runtime import session as S  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402
from mastercode import inference_config as IC  # noqa: E402
from server import turn_registry as TR  # noqa: E402
from server import brain_service as BS  # noqa: E402
from server.routes import circles as CR  # noqa: E402
from server.routes import brain as BR  # noqa: E402
from server.routes import tickets as TK  # noqa: E402

try:
    from mastercode import pending_words as PW  # noqa: E402
except ImportError:          # main: nothing holds the words
    PW = None

S.HOLD_S = 0.3
E = 3
FAILED = 0
# The room the words are typed into: a Circles topic. There is no original
# conversation, so every room names a thread.
ROOM_THREAD = "5d0e3a51-1c1b-4c4f-9a6e-2b7f3c9e1a10"
THREAD_KEY = f"thread|3|{ROOM_THREAD}"
ROOM = {"app_id": "circles", "project_id": None, "thread_id": ROOM_THREAD, "history_context": None}
# The Workbench's own page for its route: a project, which is what its
# GET /api/brain/pending keys by (compose_thread_key: project|E|N).
WB_PROJECT = 5
THREAD_KEY_WB = f"project|3|{WB_PROJECT}"
ROOM_WB = {"app_id": "workbench", "project_id": WB_PROJECT, "thread_id": None, "history_context": None}


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


class RoutedPool:
    """The runtime's pool: pending_words statements go to the real database,
    every other read is empty and every other write lands nowhere (the turn's
    own bookkeeping is fail-soft), as in test_late_nudge_once.py."""

    def __init__(self, real):
        self.real = real

    async def fetch(self, q, *a):
        return await self.real.fetch(q, *a) if "pending_words" in q else []

    async def fetchrow(self, q, *a):
        return await self.real.fetchrow(q, *a) if "pending_words" in q else None

    async def fetchval(self, q, *a):
        return await self.real.fetchval(q, *a) if "pending_words" in q else None

    async def execute(self, q, *a):
        return await self.real.execute(q, *a) if "pending_words" in q else "OK"

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *e):
                return False
        return _C()


class Harness:
    """EntityRuntime.send_message with a scripted model (test_late_nudge_once's)."""

    def __init__(self, pool):
        self.pool = pool
        self.script: list = []
        self.calls: list[list[dict]] = []
        self.events: list[dict] = []
        self.sessions = 0

    async def model(self, messages=None, *a, **kw):
        msgs = messages if messages is not None else (a[0] if a else [])
        self.calls.append([dict(m) for m in msgs])
        r = self.script.pop(0) if self.script else {"content": "Done."}
        if callable(r):
            r = r(self)
            if asyncio.iscoroutine(r):
                r = await r
        cb = kw.get("stream_cb")
        if cb and r.get("content"):
            await cb(r["content"])
        return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1, "tool_calls": None, **r}

    def patch(self):
        self._saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                       S._AVAILABLE.get("ceigas_domain_controller"), S._run_close_turn,
                       S._execute_meta_tool, S._build_tool_schemas)

        async def route(pool, eid, role, thread_override=None, **kw):
            return {"provider": "anthropic", "model": "fake-model"}

        async def run_close_turn(pool, sess, args, *, prompt_console=None):
            return {"status": "ok", "result": "recorded"}

        async def meta(name, args, **kw):
            return {"status": "ok", "result": f"{name} ran"}

        async def schemas(pool, eid, interaction_context=None, **kw):
            return []
        S._call_entity_model = self.model
        PG.call_model = self.model
        IC.resolve_role_route = route
        S._AVAILABLE["ceigas_domain_controller"] = None
        S._run_close_turn = run_close_turn
        S._execute_meta_tool = meta
        S._build_tool_schemas = schemas
        self.rt = S.EntityRuntime(RoutedPool(self.pool))

        async def ctx(entity_id, **kw):
            self.sessions += 1
            return S.EntitySession(
                session_id=f"pw-sess-{self.sessions}", entity_id=entity_id,
                entity={"id": entity_id, "name": "Host A", "behavioral_params": {},
                        "permission_overrides": {"bot_capabilities": 255}},
                interaction_context="circles")
        self.rt._build_chat_context_transient = ctx

    def unpatch(self):
        (S._call_entity_model, PG.call_model, IC.resolve_role_route,
         S._AVAILABLE["ceigas_domain_controller"], S._run_close_turn,
         S._execute_meta_tool, S._build_tool_schemas) = self._saved

    async def turn(self, message):
        async def cb(e):
            self.events.append(e)
        return await self.rt.send_message(entity_id=E, user_message=message, progress_cb=cb)

    def reached(self, words):
        """Model calls whose newest user message carries the words."""
        n = 0
        for msgs in self.calls:
            last = msgs[-1] if msgs else {}
            if last.get("role") == "user" and words in str(last.get("content") or ""):
                n += 1
        return n


def _tc(name, i):
    return {"id": f"call_{name}_{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def fresh_process():
    """What a process holds in memory goes with it: the runtime's queues, the
    words kept for a thread's next turn, the turn registry."""
    for name in ("_pending_nudges", "_nudge_times", "_nudge_orders", "_nudge_tags", "_nudge_ids",
                 "_nudge_keeps_copy", "_nudge_keys", "_unread_for_next_turn", "_turn_events",
                 "_task_holds", "_stopped_reading", "_live_turns"):
        d = getattr(S, name, None)
        if isinstance(d, dict):
            d.clear()
    if PW is not None:
        PW._handed.clear()
        if isinstance(getattr(PW, "_seen_cids", None), dict):
            PW._seen_cids.clear()
    reg = TR.TurnRegistry()
    TR.get_turn_registry = lambda: reg
    return reg


async def rows(pool):
    try:
        return [dict(r) for r in await pool.fetch(
            "SELECT id::text AS id, session_id, thread_key, room, text, client_id FROM pending_words"
            " ORDER BY received_at")]
    except asyncpg.UndefinedTableError:
        return None


async def held_list(thread=ROOM_THREAD, route="circles"):
    """GET /api/circles/pending (or the Workbench's GET /api/brain/pending),
    the route itself."""
    if route == "workbench":
        fn = getattr(BR, "brain_pending", None)
        if fn is None:
            return None

        class _St:
            app_token = types.SimpleNamespace(app_id="circles")

        class _R:
            state = _St()
        out = await fn(_R(), project_id=WB_PROJECT)
        return out.get("words")
    fn = getattr(CR, "pending", None)
    if fn is None:
        return None
    out = await fn(thread=thread)
    return out.get("words")


async def settle_tasks():
    for _ in range(3):
        pend = list(getattr(PW, "_settling", set()) if PW else set()) + list(TR._PENDING_ROW_WRITES)
        if pend:
            await asyncio.gather(*pend, return_exceptions=True)
        await asyncio.sleep(0.05)


async def wait_turns(reg):
    for _ in range(100):
        if all(r.done.is_set() for r in list(reg._by_id.values())):
            return
        await asyncio.sleep(0.05)


async def drain_stream(agen) -> list[str]:
    """A sender that follows its stream to the end (the Workbench send)."""
    out = []
    try:
        async for f in agen:
            out.append(f)
    except Exception as e:  # noqa: BLE001
        out.append(f"error:{e!r}")
    return out


async def scenario(pool, when, route="circles", pii=False, resend=None):
    tk, room = (THREAD_KEY_WB, ROOM_WB) if route == "workbench" else (THREAD_KEY, ROOM)
    """One turn on your Host's original page, with the words typed into it.
    when: "unread" (A, B, D) — they arrive while its failing last call
    streams, so it ends on the error without reading them; "read" (E) — they
    arrive while its first call streams, and the next round reads them;
    "restart" (C, D) — they are held, and the process ends before the turn
    does."""
    if await rows(pool) is not None:
        await pool.execute("DELETE FROM pending_words WHERE entity_id = $1", E)
    reg = fresh_process()
    h = Harness(pool)
    h.patch()
    ran: list[dict] = []
    out: dict = {"ran": ran}
    gate = asyncio.Event()
    words = ("Ticket #77 — I chose “Window”." if route == "ticket"
             else "Also book the window seat.")
    cid = None if route == "ticket" else "n-1760180000000-7"
    os.environ.pop("PII_MODE", None)
    if pii:
        os.environ["PII_MODE"] = "on"

    def make_next(text, frame=None, client_id=None):
        # The thread's next turn, as brain_service builds it: a real turn of
        # the runtime on the words, under its own session.
        async def gen():
            ran.append({"text": text, "client_id": client_id})
            yield "2:" + json.dumps([{"type": "session", "session_id": f"pw-sess-{h.sessions + 1}",
                                      "turn_token": f"tk-{len(ran)}"}]) + "\n"
            await h.turn(text)
            yield "e:{}\n"
        return gen()

    async def first_inner():
        yield "2:" + json.dumps([{"type": "session", "session_id": "pw-sess-1", "turn_token": "tk-0"}]) + "\n"
        await gate.wait()
    rec, _ = reg.start_or_attach(thread_key=tk, entity_id=E, app_id="circles",
                                 message="Plan the trip.", make_inner=first_inner,
                                 make_followup=make_next, room=room)
    await asyncio.sleep(0.05)
    saved = (CR._audit, CR._entity_id, BS.get_pool)

    async def no_audit(**kw):
        return None
    saved_eid = BS._entity_id
    CR._audit, CR._entity_id, BS.get_pool = no_audit, (lambda: E), (lambda: pool)
    BS._entity_id = E

    async def type_words(_h):
        try:
            if route == "circles":
                out["nudge"] = await CR.nudge(_Req({"session_id": "pw-sess-1", "text": words,
                                                    "client_id": cid}))
            elif route == "workbench":
                out["nudge"] = await BR.brain_turn_nudge(rec.turn_id, _Req({"text": words, "client_id": cid}))
            elif route == "send":
                # Another tab's message (or a connector's) on the thread: the
                # real send path, whose stream its sender follows and closes.
                agen = BS.send_message(entity_id=E, message=words, app_id="circles", client_id=cid, thread_id=ROOM_THREAD)
                first = await agen.__anext__()
                await agen.aclose()
                out["nudge"] = json.loads(first[2:])[0] if first.startswith("2:") else {"frame": first}
                if resend == "same_process":
                    # The Workbench send's silent retry, under the same id,
                    # while the words wait in the running turn's queue.
                    agen = BS.send_message(entity_id=E, message=words, app_id="circles", client_id=cid, thread_id=ROOM_THREAD)
                    first = await agen.__anext__()
                    await agen.aclose()
                    out["retry"] = json.loads(first[2:])[0] if first.startswith("2:") else {"frame": first}
            elif route == "ticket":
                out["nudge"] = await TK._answer_to_turn(
                    E, 77, {"asked_in_session": "pw-sess-1", "asked_in": room}, {"label": "Window"},
                    "window", "", "user")
        except Exception as e:  # noqa: BLE001
            out["nudge"] = {"error": repr(e)}
        out["rows_at_receipt"] = await rows(pool)
        out["listed_while_held"] = await held_list(route="workbench" if route == "workbench" else "circles")
    try:
        if when == "read":
            async def first(_h):
                await type_words(_h)
                return {"content": "", "tool_calls": [_tc("noop_probe", 1)]}
            h.script = [first, {"content": "Window seat it is."}]
        else:
            async def failing(_h):
                await type_words(_h)
                return {"content": "", "error": "the provider answered 529 (overloaded)"}
            h.script = [{"content": "", "tool_calls": [_tc("noop_probe", 1)]}, failing,
                        {"content": "Window seat booked."}, {"content": "Window seat booked."}]
        if when == "restart":
            # The process ends while the turn runs: nothing of it finishes.
            task = asyncio.ensure_future(h.turn("Plan the trip."))
            for _ in range(200):
                if "listed_while_held" in out:
                    break
                await asyncio.sleep(0.02)
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            reg = fresh_process()
            h.calls.clear()
            out["rows_after_restart"] = await rows(pool)
            # The boot pass, through the real brain_service.send_message; the
            # runtime's inner turn is the harness's.
            _inner = BS._send_message_inner

            async def inner(*a, **kw):
                ran.append({"text": kw.get("message"), "client_id": kw.get("client_id"),
                            "app_id": kw.get("app_id"), "thread_id": kw.get("thread_id")})
                yield "2:" + json.dumps([{"type": "session", "session_id": f"pw-sess-{h.sessions + 1}",
                                          "turn_token": "tk-boot"}]) + "\n"
                await h.turn(kw.get("message"))
                yield "e:{}\n"
            BS._send_message_inner = inner
            try:
                before = await pool.fetchval("SELECT clock_timestamp()")

                def retry():
                    # The Workbench send's silent retry after its stream died
                    # with the process: the same words, under the same id.
                    return drain_stream(BS.send_message(entity_id=E, message=words,
                                                        app_id="circles", client_id=cid, thread_id=ROOM_THREAD))
                if resend == "retry_first":
                    out["retry_frames"] = await retry()
                    await wait_turns(reg)
                    await settle_tasks()
                out["boot_started"] = await PW.deliver_left(pool, E, before) if PW else None
                await wait_turns(reg)
                await settle_tasks()
                if resend == "boot_first":
                    out["retry_frames"] = await retry()
                    await wait_turns(reg)
                    await settle_tasks()
                out["boot_again"] = await PW.deliver_left(pool, E, before) if PW else None
            finally:
                BS._send_message_inner = _inner
        else:
            await h.turn("Plan the trip.")
            gate.set()
            await asyncio.wait_for(rec.done.wait(), 5)
            await wait_turns(reg)
            await settle_tasks()
            # Then the process ends, and the next one's boot pass runs.
            out["rows_before_restart"] = await rows(pool)
            fresh_process()
            before = await pool.fetchval("SELECT clock_timestamp()")
            out["boot_after"] = await PW.deliver_left(pool, E, before) if PW else None
        out["rows_end"] = await rows(pool)
        out["listed_end"] = await held_list(route="workbench" if route == "workbench" else "circles")
        out["reached"] = h.reached(words)
        out["receipts"] = [e for e in h.events if e.get("event") == "nudge_read"]
        out["requeued"] = [json.loads(f[2:]) for f in rec.frames if "nudge_requeued" in f]
        out["words"] = words
        out["cid"] = cid
    finally:
        CR._audit, CR._entity_id, BS.get_pool = saved
        BS._entity_id = saved_eid
        os.environ.pop("PII_MODE", None)
        h.unpatch()
    return out


async def started_then_retry(pool) -> dict:
    """A Workbench send (client_id) on an idle page starts a turn through the
    real brain_service.send_message; the turn runs to its end; then the send's
    silent retry comes under the same id. Counts the turns the inner path
    started, and reads the retry's stream for the first turn's reply."""
    await pool.execute("DELETE FROM pending_words WHERE entity_id = $1", E)
    fresh_process()
    starts: list = []
    _inner = BS._send_message_inner
    saved = (BS.get_pool, BS._entity_id)

    async def inner(*a, **kw):
        starts.append(kw.get("client_id"))
        yield "2:" + json.dumps([{"type": "session", "session_id": f"st-sess-{len(starts)}",
                                  "turn_token": f"st-{len(starts)}"}]) + "\n"
        yield '0:"The cart is fixed."\n'
        yield "e:{}\n"
    BS._send_message_inner = inner
    BS.get_pool, BS._entity_id = (lambda: pool), E
    try:
        first = await drain_stream(BS.send_message(entity_id=E, message="Fix the cart.",
                                                   app_id="workbench", client_id="u-st-1"))
        await wait_turns(TR.get_turn_registry())
        retry = await drain_stream(BS.send_message(entity_id=E, message="Fix the cart.",
                                                   app_id="workbench", client_id="u-st-1"))
        await wait_turns(TR.get_turn_registry())
        n_after_retry = len(starts)
        await drain_stream(BS.send_message(entity_id=E, message="Fix the cart.",
                                           app_id="workbench", client_id="u-st-2"))
        await wait_turns(TR.get_turn_registry())
    finally:
        BS._send_message_inner = _inner
        BS.get_pool, BS._entity_id = saved
    def turn_of(frames):
        for f in frames:
            if f.startswith("2:") and '"type": "turn"' in f:
                return json.loads(f[2:])[0].get("turn_id")
        return None
    return {"starts": n_after_retry, "fresh_starts": len(starts) - n_after_retry,
            "first_turn": turn_of(first), "retry_turn": turn_of(retry),
            "retry_has_reply": any("The cart is fixed." in f for f in retry),
            "retry_frames": [f[:80] for f in retry][:6]}


def _turn_of(frames):
    for f in frames:
        if f.startswith("2:") and '"type": "turn"' in f:
            return json.loads(f[2:])[0].get("turn_id")
    return None


async def kept_words_resent(pool) -> dict:
    """K, PII mode: the page sends the words it
    kept in its tab as the next message, under the id it nudged them with
    (use-circles-chat / use-workbench-chat deliverKept), and may send them
    again: (a) it reloads before the answer to the first send came; (b) the
    turn READ them, but its replay no longer holds the receipt (its output
    passed MAX_BUFFER_BYTES), so the page cannot tell; (c) a duplicated tab
    has the same sessionStorage and sends them too. The real
    brain_service.send_message and turn registry run; the turn's runtime is a
    stand-in that counts the turns started. The user row a turn writes for
    its words carries the id (in PII mode its digest, session.py; proven by
    test_workbench_index_pg R) and is written here as that turn would."""
    from mastercode import pii_queue
    reg = fresh_process()
    starts: list = []
    out: dict = {}
    _inner = BS._send_message_inner
    saved = (BS.get_pool, BS._entity_id, TR.MAX_BUFFER_BYTES)
    os.environ["PII_MODE"] = "on"

    async def inner(*a, **kw):
        starts.append({"text": kw.get("message"), "client_id": kw.get("client_id")})
        yield "2:" + json.dumps([{"type": "session", "session_id": f"kw-sess-{len(starts)}",
                                  "turn_token": f"kw-{len(starts)}"}]) + "\n"
        yield '0:"Noted."\n'
        yield "e:{}\n"

    async def row_of(cid):
        # The user row the turn wrote for the words (session.py), PII mode.
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, metadata)"
            " VALUES ($1, $2, 'turn', $3::jsonb)", E, "[tokenized]",
            json.dumps({"role": "user", "client_id_digest": pii_queue.client_id_digest(cid),
                        **S._sender_tags(PW.SentId(cid, "circles")), "kw_gate": True}))

    def send(words, cid):
        return drain_stream(BS.send_message(entity_id=E, message=words, app_id="circles", client_id=cid, thread_id=ROOM_THREAD))
    BS._send_message_inner = inner
    BS.get_pool, BS._entity_id = (lambda: pool), E
    try:
        await pool.execute("DELETE FROM entity_memory WHERE entity_id = $1 AND metadata ? 'kw_gate'", E)
        # (a) the copy goes as the next message; the page reloads before the
        # answer and sends it again; later, after a restart, once more.
        cid = "n-1760190000000-4821337700"
        first = await send("Avoid the tolls.", cid)
        await wait_turns(reg)
        await row_of(cid)
        again = await send("Avoid the tolls.", cid)
        await wait_turns(reg)
        out["a_starts"] = len(starts)
        out["a_same_turn"] = bool(_turn_of(first)) and _turn_of(again) == _turn_of(first)
        reg = fresh_process()
        later = await send("Avoid the tolls.", cid)
        await wait_turns(reg)
        out["a_restart_starts"] = len(starts)
        out["a_restart_answer"] = [f[:90] for f in later]

        # (b) the turn read the nudge after its output passed the buffer's
        # bound: the replay a reloaded page reads has no receipt.
        TR.MAX_BUFFER_BYTES = 2000
        cid_b = "n-1760190000002-5551239887"

        async def long_turn(*a, **kw):
            starts.append({"text": kw.get("message"), "client_id": kw.get("client_id")})
            yield "2:" + json.dumps([{"type": "session", "session_id": "kw-sess-long",
                                      "turn_token": "kw-long"}]) + "\n"
            yield "0:" + json.dumps("x" * 3000) + "\n"
            yield "2:" + json.dumps([{"type": "nudge_read", "text": "Take the coast road.",
                                      **S._id_echo(PW.SentId(cid_b, "circles"))}]) + "\n"
            yield "e:{}\n"
        BS._send_message_inner = long_turn
        t = await send("Plan the trip.", "u-1760190000003-1234567890")
        await wait_turns(reg)
        rec = reg.get(_turn_of(t))
        out["b_replay_has_receipt"] = rec is not None and any("nudge_read" in f for f in rec.frames)
        out["b_truncated"] = rec is not None and rec.truncated
        BS._send_message_inner = inner
        n0 = len(starts)
        resend = await send("Take the coast road.", cid_b)
        await wait_turns(reg)
        out["b_new_starts"] = len(starts) - n0
        out["b_follows_reader"] = rec is not None and _turn_of(resend) == rec.turn_id
        TR.MAX_BUFFER_BYTES = saved[2]

        # (c) a duplicated tab: both tabs send the copy. At once, and the second
        # after the first turn has finished and the registry let it go.
        cid_c = "n-1760190000004-9081726354"
        n0 = len(starts)
        await asyncio.gather(send("Take the ferry.", cid_c), send("Take the ferry.", cid_c))
        await wait_turns(reg)
        out["c_together_starts"] = len(starts) - n0
        cid_c2 = "n-1760190000005-1928374655"
        n0 = len(starts)
        await send("Book the ferry.", cid_c2)
        await wait_turns(reg)
        await row_of(cid_c2)
        reg._by_id.clear()          # DONE_TTL_S has passed
        dup = await send("Book the ferry.", cid_c2)
        await wait_turns(reg)
        out["c_later_starts"] = len(starts) - n0
        out["c_later_answer"] = [f[:90] for f in dup]
        out["starts"] = starts
    finally:
        BS._send_message_inner = _inner
        BS.get_pool, BS._entity_id, TR.MAX_BUFFER_BYTES = saved
        os.environ.pop("PII_MODE", None)
        await pool.execute("DELETE FROM entity_memory WHERE entity_id = $1 AND metadata ? 'kw_gate'", E)
    return out


async def other_app_same_id(pool) -> dict:
    """X: an id counts as delivered only for the
    app that sent it. Circles' words went under an id (a turn started on
    them, a turn read them as a nudge, a row keeps them, the server holds
    them); Workbench words sent later under the SAME id are Workbench's own
    and run. The real brain_service.send_message, turn registry and
    pending_words run; the turn's runtime is a stand-in that counts the
    turns started (as in K). PII mode off and on for the row."""
    from mastercode import pii_queue
    from server.connectors import thread_pick as TP
    reg = fresh_process()
    starts: list = []
    out: dict = {}
    _inner = BS._send_message_inner
    saved = (BS.get_pool, BS._entity_id)

    async def inner(*a, **kw):
        starts.append({"text": kw.get("message"), "client_id": kw.get("client_id")})
        n = len(starts)
        yield "2:" + json.dumps([{"type": "session", "session_id": f"xa-sess-{n}",
                                  "turn_token": f"xa-{n}"}]) + "\n"
        if kw.get("message") == "Plan the trip.":
            # Circles' nudge under "1", read by this turn (session's receipt).
            yield "2:" + json.dumps([{"type": "nudge_read", "text": "Take the coast road.",
                                      **S._id_echo(PW.SentId("1", "circles"))}]) + "\n"
        yield '0:"Noted."\n'
        yield "e:{}\n"

    async def row_of(cid, app):
        await pool.execute(
            "INSERT INTO entity_memory (entity_id, content, memory_type, metadata)"
            " VALUES ($1, $2, 'turn', $3::jsonb)", E, "[tokenized]",
            json.dumps({"role": "user", **({"client_id_digest": pii_queue.client_id_digest(cid)}
                                           if pii_queue.enabled() else {"client_id": cid}),
                        **S._sender_tags(PW.SentId(cid, app)), "xa_gate": True}))

    def send(app, words, cid):
        return drain_stream(BS.send_message(entity_id=E, message=words, app_id=app, client_id=cid,
                                            **({"thread_id": ROOM_THREAD} if app == "circles" else {})))

    async def runs(app, words, cid) -> int:
        n0 = len(starts)
        frames = await send(app, words, cid)
        await wait_turns(reg)
        return len(starts) - n0, frames

    BS._send_message_inner = inner
    BS.get_pool, BS._entity_id = (lambda: pool), E
    try:
        await pool.execute("DELETE FROM entity_memory WHERE entity_id = $1 AND metadata ? 'xa_gate'", E)
        # (1) a turn STARTED on Circles' words under "1".
        out["circles_starts"], _ = await runs("circles", "Avoid the tolls.", "1")
        out["same_app_resend"], _ = await runs("circles", "Avoid the tolls.", "1")
        out["other_app_started"], f = await runs("workbench", "Ship the fix.", "1")
        out["other_app_started_frames"] = [x[:80] for x in f]
        # (2) a turn READ Circles' nudge under "1" (its receipt).
        reg = fresh_process()
        await runs("circles", "Plan the trip.", "u-plan")
        out["other_app_read"], _ = await runs("workbench", "Ship the fix.", "1")
        # (3) the registry let go; the user row of Circles' words keeps "1",
        # in both modes.
        for pii in (False, True):
            if pii:
                os.environ["PII_MODE"] = "on"
            try:
                reg = fresh_process()
                await row_of("1", "circles")
                out[f"other_app_row_pii={pii}"], f = await runs("workbench", "Ship the fix.", "1")
                out[f"other_app_row_frames_pii={pii}"] = [x[:80] for x in f]
                reg = fresh_process()
                out[f"same_app_row_pii={pii}"], f = await runs("circles", "Avoid the tolls.", "1")
                out[f"same_app_row_frames_pii={pii}"] = [x[:80] for x in f]
            finally:
                os.environ.pop("PII_MODE", None)
                await pool.execute(
                    "DELETE FROM entity_memory WHERE entity_id = $1 AND metadata ? 'xa_gate'", E)
        # (4) the server HOLDS Circles' words under "1" (a nudge into a turn).
        reg = fresh_process()
        k = await PW.hold(pool, E, "xa-held", "Take the coast road.", client_id=PW.SentId("1", "circles"),
                          room=ROOM, thread_key=THREAD_KEY)
        try:
            out["claim_other_app"] = await PW.claim_resend(pool, E, PW.SentId("1", "workbench"))
            out["claim_same_app"] = (await PW.claim_resend(pool, E, PW.SentId("1", "circles")))[0]
            out["index_other_app_held"] = await TP._held_for_id(pool, E, "1", "workbench")
            out["index_same_app_held"] = await TP._held_for_id(pool, E, "1", "circles")
        finally:
            await PW.settle(pool, k)
            PW.unclaim([k], PW.SentId("1", "circles"))
        out["starts"] = starts
    finally:
        BS._send_message_inner = _inner
        BS.get_pool, BS._entity_id = saved
        os.environ.pop("PII_MODE", None)
        await pool.execute("DELETE FROM entity_memory WHERE entity_id = $1 AND metadata ? 'xa_gate'", E)
    return out


def took(route, r) -> bool:
    """The sender's answer says the words went to the turn."""
    n = r.get("nudge") or {}
    if route == "send":
        return n.get("type") == "turn" and n.get("mode") == "nudged"
    if route == "ticket":
        return n.get("answered_into_turn") is True
    return n.get("ok") is True


async def main(dsn):
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        await pool.execute(
            "INSERT INTO circles_threads (id, entity_id, title) VALUES ($1::uuid, $2, 'Trip')"
            " ON CONFLICT (id) DO NOTHING", ROOM_THREAD, E)
        # A, B, D: the turn ends without reading the words.
        r = await scenario(pool, "unread")
        brief = {k: r.get(k) for k in ("nudge", "ran", "reached", "receipts", "rows_at_receipt",
                                       "rows_before_restart", "rows_end", "boot_after", "requeued")}
        held = r.get("rows_at_receipt") or []
        check("A the route answers once the words are written down, in the room of the turn they "
              "were typed into, with their id",
              isinstance(r.get("nudge"), dict) and r["nudge"].get("ok") is True
              and r["nudge"].get("held") is True
              and len(held) == 1 and held[0]["text"] == r["words"] and held[0]["client_id"] == r["cid"]
              and held[0]["thread_key"] == THREAD_KEY
              and json.loads(held[0]["room"]) == ROOM, brief)
        check("A the turn ended without reading them, so they start the thread's next turn, on the "
              "server, whole, with their sender's id",
              [x["text"] for x in r["ran"]] == [r["words"]] and r["ran"][0]["client_id"] == r["cid"]
              and bool(r["requeued"]) and r["requeued"][0][0].get("client_ids") == [r["cid"]], brief)
        check("D they reach the Host exactly once (in that next turn)", r["reached"] == 1, brief)
        check("D the row is gone once the next turn has them, and a restart afterwards starts nothing",
              r.get("rows_before_restart") == [] and r.get("boot_after") == 0 and r.get("rows_end") == [],
              brief)
        listed = r.get("listed_while_held") or []
        check("B while the server holds them, GET /api/circles/pending lists them for the page, under "
              "the id the page drew their line with",
              len(listed) == 1 and listed[0]["text"] == r["words"] and listed[0]["client_id"] == r["cid"],
              r.get("listed_while_held"))
        check("B ...and nothing once a turn has them", r.get("listed_end") == [], r.get("listed_end"))

        # C, D: the process ends with the words held.
        r = await scenario(pool, "restart")
        brief = {k: r.get(k) for k in ("nudge", "ran", "reached", "rows_after_restart", "boot_started",
                                       "boot_again", "rows_end")}
        check("C the words outlive the process: their row is there after it ended",
              bool(r.get("rows_after_restart")) and r["rows_after_restart"][0]["text"] == r["words"], brief)
        check("C the boot pass starts one turn in the room they were typed into, with the words and "
              "their id (brain_service.send_message)",
              r.get("boot_started") == 1 and len(r["ran"]) == 1 and r["ran"][0]["text"] == r["words"]
              and r["ran"][0]["client_id"] == r["cid"] and r["ran"][0]["app_id"] == "circles"
              and r["ran"][0]["thread_id"] == ROOM_THREAD, brief)
        check("D ...they reach the Host once, the row is gone once that turn started, and a second "
              "pass starts nothing",
              r["reached"] == 1 and r.get("rows_end") == [] and r.get("boot_again") == 0, brief)

        # E: the running turn reads the words.
        r = await scenario(pool, "read")
        brief = {k: r.get(k) for k in ("nudge", "ran", "reached", "receipts", "rows_at_receipt",
                                       "rows_before_restart", "boot_after", "requeued")}
        check("E held at receipt, as always",
              bool(r.get("rows_at_receipt")) and r["rows_at_receipt"][0]["text"] == r["words"], brief)
        check("E the running turn reads them once (its receipt, with the id) and their row goes then",
              r["reached"] == 1 and [e.get("client_id") for e in r["receipts"]] == [r["cid"]]
              and r.get("rows_before_restart") == [], brief)
        check("E the turn's end sends nothing on, and a restart afterwards delivers nothing",
              r["ran"] == [] and not r["requeued"] and r.get("boot_after") == 0, brief)

        # W, M, T: every other way words reach a running turn.
        for route, tag in (("workbench", "W"), ("send", "M"), ("ticket", "T")):
            r = await scenario(pool, "unread", route=route)
            brief = {k: r.get(k) for k in ("nudge", "ran", "reached", "rows_at_receipt",
                                           "rows_before_restart", "boot_after", "listed_while_held")}
            held = r.get("rows_at_receipt") or []
            check(f"{tag} the words are written down before the sender is answered, in the room of the "
                  "turn they went into",
                  took(route, r) and len(held) == 1 and held[0]["text"] == r["words"]
                  and held[0]["client_id"] == r["cid"]
                  and held[0]["thread_key"] == (THREAD_KEY_WB if route == "workbench" else THREAD_KEY)
                  and json.loads(held[0]["room"]) == (ROOM_WB if route == "workbench" else ROOM)
                  and (route != "workbench" or r["nudge"].get("held") is True), brief)
            if route == "workbench":
                listed = r.get("listed_while_held") or []
                check("W GET /api/brain/pending lists them for the page while the server holds them, "
                      "under the id the page drew their line with",
                      len(listed) == 1 and listed[0]["client_id"] == r["cid"], r.get("listed_while_held"))
            check(f"{tag} the turn ended without reading them: they start the thread's next turn, once, "
                  "and reach the Host once",
                  [x["text"] for x in r["ran"]] == [r["words"]] and r["reached"] == 1, brief)
            check(f"{tag} the row is gone once that turn has them; a restart afterwards starts nothing",
                  r.get("rows_before_restart") == [] and r.get("boot_after") == 0
                  and r.get("rows_end") == [], brief)
            r = await scenario(pool, "restart", route=route)
            brief = {k: r.get(k) for k in ("nudge", "ran", "reached", "rows_after_restart", "boot_started",
                                           "boot_again", "rows_end")}
            check(f"{tag} the process ends with the words held: the boot pass starts one turn with them "
                  "in that room, they reach the Host once, and a second pass starts nothing",
                  bool(r.get("rows_after_restart")) and r.get("boot_started") == 1
                  and [x["text"] for x in r["ran"]] == [r["words"]]
                  and r["ran"][0]["app_id"] == (ROOM_WB if route == "workbench" else ROOM)["app_id"]
                  and r["ran"][0]["thread_id"] == (ROOM_WB if route == "workbench" else ROOM)["thread_id"]
                  and r["reached"] == 1 and r.get("rows_end") == [] and r.get("boot_again") == 0, brief)
            r = await scenario(pool, "read", route=route)
            brief = {k: r.get(k) for k in ("nudge", "ran", "reached", "rows_before_restart", "boot_after")}
            check(f"{tag} words the running turn reads go once: the row goes then, nothing is sent on, "
                  "and a restart afterwards delivers nothing",
                  r["reached"] == 1 and r["ran"] == [] and r.get("rows_before_restart") == []
                  and r.get("boot_after") == 0, brief)

        # I: a Workbench Index send whose words went into the running turn
        # retries (thread_pick.find_index_send): words the server holds are
        # delivered, so the retry never sends them again.
        from server.connectors import thread_pick as TP
        await pool.execute("DELETE FROM pending_words WHERE entity_id = $1", E)
        k = await PW.hold(pool, E, "pw-sess-9", "Fix the cart.", client_id=PW.SentId("idx-req-1", "workbench"), room=ROOM,
                          thread_key=THREAD_KEY) if PW else None

        class _Marker:
            """entity_memory answers the send's marker (land_index_pick);
            pending_words is the real table."""
            async def fetch(self, q, *a):
                if "entity_memory" in q:
                    return [{"id": 1, "project_id": 21, "content": "Sent to Storefront.",
                             "metadata": {"source_kind": "index_pick", "client_id": "idx-req-1",
                                          "index_send": {"title": "Storefront"}}}]
                return await pool.fetch(q, *a)

            async def fetchval(self, q, *a):
                return await pool.fetchval(q, *a)
        found = await TP.find_index_send(_Marker(), E, "idx-req-1", "workbench")
        check("I an Index send's retry finds words the server holds delivered (into the running turn), "
              "so it never sends them again", isinstance(found, dict) and found.get("nudge") is True, found)
        if k:
            await PW.settle(pool, k)
        again = await TP.find_index_send(_Marker(), E, "idx-req-1", "workbench")
        check("I ...and a send nothing holds or delivered runs again", again is None, again)

        # R: the Workbench send's one silent retry (its stream died before
        # anything showed) carries the same id as its first attempt, whose
        # words went into the running turn and are held. Whichever of the
        # retry and the boot pass comes first delivers them; the other
        # delivers nothing.
        for order in ("retry_first", "boot_first"):
            r = await scenario(pool, "restart", route="send", resend=order)
            n = sum((x.get("text") or "").count(r["words"]) for x in r["ran"])
            brief = {k: r.get(k) for k in ("nudge", "ran", "rows_after_restart", "boot_started",
                                           "boot_again", "rows_end")}
            check(f"R ({order}) words held when the process ended reach the Host once, whether the "
                  "send's retry or the boot pass comes first",
                  bool(r.get("rows_after_restart")) and n == 1 and r.get("rows_end") == [], brief)
        r = await scenario(pool, "unread", route="send", resend="same_process")
        n = sum((x.get("text") or "").count(r["words"]) for x in r["ran"])
        brief = {k: r.get(k) for k in ("nudge", "retry", "ran", "rows_at_receipt", "rows_end")}
        check("R (same process) a retry while the words wait in the running turn adds nothing: one row, "
              "and they go on once when that turn ends without reading them",
              len(r.get("rows_at_receipt") or []) == 1 and n == 1 and r.get("rows_end") == []
              and isinstance(r.get("retry"), dict) and r["retry"].get("mode") != "nudged", brief)

        # S: the same retry when its first attempt STARTED a turn (nothing was
        # running) and that turn has finished by the time the retry arrives:
        # the retry follows that turn's result and starts no second turn
        # (turn_registry.started_with).
        r = await started_then_retry(pool)
        check("S a send's retry whose first attempt started a turn that has finished starts no second "
              "turn", r["starts"] == 1, r)
        check("S ...and the retry's stream is that turn's result",
              r["retry_has_reply"] and r["first_turn"] and r["retry_turn"] == r["first_turn"], r)
        check("S a send under a new id after it still starts its own turn", r["fresh_starts"] == 1, r)

        # K: PII mode, the page's kept copy sent twice under its id: the Host
        # has the words once.
        r = await kept_words_resent(pool)
        check("K (a) a reload before the answer: the copy sent again under its id follows the turn it "
              "started, and no second turn starts", r.get("a_starts") == 1 and r.get("a_same_turn"), r)
        check("K (a) ...and after a restart the send is answered from the row that holds the words "
              "(words_delivered): nothing runs",
              r.get("a_restart_starts") == 1
              and any("words_delivered" in f for f in r.get("a_restart_answer") or []), r)
        check("K (b) the turn read the words past its buffer's bound (the replay has no receipt), and the "
              "copy sent after it follows that turn and starts nothing",
              r.get("b_truncated") and not r.get("b_replay_has_receipt") and r.get("b_new_starts") == 0
              and r.get("b_follows_reader"), r)
        check("K (c) a duplicated tab: two sends at once start one turn", r.get("c_together_starts") == 1, r)
        check("K (c) ...and one after the registry let that turn go starts nothing",
              r.get("c_later_starts") == 1
              and any("words_delivered" in f for f in r.get("c_later_answer") or []), r)

        # X: an id counts as delivered only for the app that sent it.
        r = await other_app_same_id(pool)
        check("X another app's words under an id a turn STARTED on run: Circles' id never answers a "
              "Workbench send", r.get("circles_starts") == 1 and r.get("other_app_started") == 1, r)
        check("X ...while the same app's resend under it still follows that turn and starts nothing",
              r.get("same_app_resend") == 0, r)
        check("X another app's words under an id a turn READ as a nudge run", r.get("other_app_read") == 1, r)
        for pii in (False, True):
            check(f"X (pii={pii}) another app's words under an id a user row keeps run (no words_delivered)",
                  r.get(f"other_app_row_pii={pii}") == 1
                  and not any("words_delivered" in f for f in r.get(f"other_app_row_frames_pii={pii}") or []), r)
            check(f"X (pii={pii}) ...and the same app's resend is answered from that row (words_delivered)",
                  r.get(f"same_app_row_pii={pii}") == 0
                  and any("words_delivered" in f for f in r.get(f"same_app_row_frames_pii={pii}") or []), r)
        check("X words another app's id holds are neither followed nor taken by a send under the same id",
              r.get("claim_other_app") == ("none", []), r)
        check("X ...while the same app's resend still finds them held (it delivers nothing again)",
              r.get("claim_same_app") == "held", r)
        check("X an Index retry is not answered from another app's held words under its id",
              r.get("index_other_app_held") is False and r.get("index_same_app_held") is True, r)

        # P: PII mode — nothing written; the page keeps its copy.
        for route in ("circles", "workbench"):
            for when in ("unread", "read"):
                r = await scenario(pool, when, route=route, pii=True)
                brief = {k: r.get(k) for k in ("nudge", "ran", "reached", "rows_at_receipt", "requeued")}
                check(f"P ({route}, {when}) nothing is written, and the route says it did not hold the "
                      "words (held: false), so the page keeps its copy",
                      isinstance(r.get("nudge"), dict) and r["nudge"].get("ok") is True
                      and r["nudge"].get("held") is False and r.get("rows_at_receipt") == [], brief)
                if when == "read":
                    check(f"P ({route}) the running turn still reads them (once)",
                          r["reached"] == 1 and r["ran"] == [], brief)
                else:
                    check(f"P ({route}) a turn that ends without reading them sends nothing on: the "
                          "page's copy is the one that goes, so they cannot go twice",
                          r["ran"] == [] and not r["requeued"] and r["reached"] == 0, brief)
    finally:
        await pool.close()
    return 1 if FAILED else 0


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, \
        kernel_schema_db(_srv, entity_id=E) as _db:
    rc = asyncio.run(main(_db))
print("test_pending_words_pg: " + ("FAILED" if rc else "ok"))
sys.exit(rc)
