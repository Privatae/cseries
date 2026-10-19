#!/usr/bin/env python3
"""A nudge that arrives as a turn ends reaches the Host exactly once
(Workbench book of pages).

Container-free: APP_ROOT=<repo> python3 scripts/test_late_nudge_once.py. No
pod, no DB, no model: EntityRuntime.send_message runs for real with a scripted
model and a stub pool (the harness of scripts/test_go_ahead_guard.py, section
3), and the words go in through the REAL turn registry's nudge
(turn_registry.TurnRegistry.nudge: its stopped-reading gate, then
session.push_nudge), on the turn's own session id.

The page's rule (workbench-web use-workbench-chat nudgeTurn / drainPending):
words the registry refused (409) go as the next message; words it took (200)
are held until the turn's nudge_read receipt settles them, and go as the next
message if the turn ends without one. So the words reach the Host once per
model call that injects them, plus once more if the page sends them again.

  1. while the turn's LAST model call streams (after its top-of-loop drain):
     the registry takes them; the loop's exit looks once more
     (session.py, "late nudge(s) at loop exit"), goes round, injects them
     and sends the receipt: once, in this turn, and never again
  2. just after the turn released its holds (it reads nothing more): the
     registry refuses them (turn_stopped_reading), so they go as the next
     message: once, in the next turn
  3. the receipt carries the words stripped (push_nudge), as the page's held
     copy now is (nudgeTurn trims): words typed with a leading newline are
     settled by it, so they are not sent a second time
  4. during the round backstop's LAST round (max_tool_rounds): the loop goes
     round once more for them, as at the text exit, so that turn reads them
     and sends the receipt: once, and nothing is left on the queue
  5. in the round the turn allowance stops (an exit that reads no more): no
     receipt, and the exit takes them off the queue, so the page sends them
     as the next message once and no later turn on a session that keeps its
     id reads them a second time

EVERY EXIT x BOTH SENDERS. The exits: the reply
(normal), the round backstop, the allowance's stop, a model error
(_end_on_error) and a crisis-check hit at the turn's start (its governance
frame; the turn runs on). The
senders: the page (keeps a copy; the rule above) and a connector (Telegram,
Slack, Discord: keeps NO copy and never sends the words again). Connector
words go in with exactly the kwargs connectors/turns.start_turn hands
brain_service.send_message, and those it hands the registry (both captured
by running that code). At every exit each word reaches the Host exactly
once: read by the turn, or sent again by the page, or — a connector's — sent
on by the registry as the thread's next turn (session.end_turn_input ->
turn_registry._requeue_undelivered); nothing is left on the queue, and every
exit releases the turn's holds (it reads no more).

I. THE SENDER'S ID ON THE PERSON'S ROW (the Workbench Index's send). A
message the Index sends goes in under a request id (routes/workbench_index
.start_page_turn -> brain_service.send_message client_id), and a retry after a
restart tells the words were delivered by the user row that carries it. The
real chain runs: brain_service._send_message_inner into the real
EntityRuntime.send_message, with only the model scripted and the row writer
recording what it is asked to write. The id is on the row of a turn that ran
and on the row an identity hold saves for a request it refuses (not on the
assistant's note beside it); a send without one writes a row as before; a
malformed id is not kept; and send_message hands the registry the id to keep
with the words if they land in a running turn as a nudge.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
logging.basicConfig(level=logging.ERROR)

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[late-nudge] FAIL {name} {extra}")


from mastercode.runtime import session as S  # noqa: E402
from mastercode import provider_gateway as PG  # noqa: E402
from mastercode import inference_config as IC  # noqa: E402
from server import turn_registry as TR  # noqa: E402
from mastercode.runtime import turn_allowance as TA  # noqa: E402

S.HOLD_S = 0.3


class Pool:
    """Every read is empty and every write lands nowhere (the turn's own
    bookkeeping is fail-soft), as in test_go_ahead_guard.py."""

    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return None

    async def execute(self, q, *a):
        return "OK"

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *e):
                return False
        return _C()


class Harness:
    def __init__(self, script):
        self.script = list(script)
        self.calls: list[list[dict]] = []
        self.events: list[dict] = []
        self.sessions = 0
        # The Host's behavioral_params (max_tool_rounds: the round backstop).
        self.params: dict = {}
        # Called with each progress event as the turn sends it.
        self.on_event = None

    async def model(self, messages=None, *a, **kw):
        msgs = messages if messages is not None else (a[0] if a else [])
        self.calls.append([dict(m) for m in msgs])
        r = self.script.pop(0) if self.script else {"content": "(the script ran out)"}
        if callable(r):
            r = r(self)
        cb = kw.get("stream_cb")
        if cb and r.get("content"):
            await cb(r["content"])
        return {"tokens_used": 1, "prompt_tokens": 1, "completion_tokens": 1, "tool_calls": None, **r}

    def patch(self):
        for d in (S._pending_nudges, S._nudge_times, S._turn_events, S._task_holds,
                  getattr(S, "_nudge_orders", {}), getattr(S, "_nudge_tags", {}),
                  getattr(S, "_nudge_ids", {}), getattr(S, "_nudge_keeps_copy", getattr(S, "_nudge_no_copy", {})),
                  getattr(S, "_unread_for_next_turn", {})):
            d.clear()
        self._saved = (S._call_entity_model, PG.call_model, IC.resolve_role_route,
                       S._AVAILABLE.get("ceigas_domain_controller"), S._run_close_turn,
                       S._execute_meta_tool, S._build_tool_schemas, S.release_holds)

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
        self.rt = S.EntityRuntime(Pool())
        self._check = TA.TurnSpend.check
        self.spend_check = None
        harness = self

        def check_(self_, new_tokens, output_reserve):
            if harness.spend_check is not None:
                return harness.spend_check(self_, new_tokens, output_reserve)
            return harness._check(self_, new_tokens, output_reserve)
        TA.TurnSpend.check = check_

        async def ctx(entity_id, **kw):
            # A chat turn's session id is minted per turn
            # (_build_chat_context_transient: uuid4), so no later turn reads
            # this one's queue.
            self.sessions += 1
            return S.EntitySession(
                session_id=f"late-sess-{self.sessions}", entity_id=entity_id,
                entity={"id": entity_id, "name": "Sage", "behavioral_params": dict(self.params),
                        "permission_overrides": {"bot_capabilities": 255}},
                interaction_context="app:workbench")
        self.rt._build_chat_context_transient = ctx

    def unpatch(self):
        (S._call_entity_model, PG.call_model, IC.resolve_role_route,
         S._AVAILABLE["ceigas_domain_controller"], S._run_close_turn,
         S._execute_meta_tool, S._build_tool_schemas, S.release_holds) = self._saved
        TA.TurnSpend.check = self._check

    async def turn(self, message):
        async def cb(e):
            self.events.append(e)
            if self.on_event is not None:
                self.on_event(e)
        return await self.rt.send_message(entity_id=9003, user_message=message, progress_cb=cb)

    def reached(self, words):
        """Model calls that carried the words as their newest user message:
        the injection (or the message) that call answers."""
        n = 0
        for msgs in self.calls:
            last = msgs[-1] if msgs else {}
            if last.get("role") == "user" and words in str(last.get("content") or ""):
                n += 1
        return n


async def _registry_turn(session_id, make_followup=None, with_session=True):
    """The turn registry's record for the running turn, on the turn's own
    session id (its session frame), so TurnRegistry.nudge reads the real
    stopped-reading mark and pushes into the real queue. `make_followup`:
    what builds the thread's next turn (brain_service passes one for every
    turn it starts)."""
    reg = TR.TurnRegistry()
    gate = asyncio.Event()

    async def inner():
        if with_session:
            yield "2:" + json.dumps([{"type": "session", "session_id": session_id, "turn_token": "tk"}]) + "\n"
        else:
            # The turn ends before it ever had a session (the identity hold
            # refused it, or it failed first).
            yield "f:{}\n"
        await gate.wait()
    rec, _ = reg.start_or_attach(thread_key="project|3|251", entity_id=9003, app_id="workbench",
                                 message="Tidy the checkout.", make_inner=inner,
                                 make_followup=make_followup,
                                 room={"app_id": "workbench", "project_id": 251, "thread_id": None,
                                       "history_context": None})
    await asyncio.sleep(0.05)
    return reg, rec, gate


def _runner(h, ran):
    """The thread's next turn, as brain_service builds it (_followup_maker /
    send_message's make_inner): here a real turn of the harness's runtime on
    the words. `ran` collects the words each such turn started with."""
    def make(text, frame=None):
        async def gen():
            ran.append(text)
            await h.turn(text)
            yield "e:{}\n"
        return gen()
    return make


async def _connector_kw():
    """What a connector's message hands the turn registry, from the real
    code: connectors/turns.start_turn's kwargs to brain_service.send_message,
    then send_message's to start_or_attach. Only the message map's two
    writes and the entity id are stood in (no database here)."""
    import types
    from server import brain_service as BS
    from server.connectors import turns as CT
    from server.connectors import message_map as MM
    sent: dict = {}

    async def record_send(**kw):
        sent.update(kw)
        yield '3:"recorded"\n'

    async def write_inbound(*a, **k):
        return 1

    async def forget_inbound(*a, **k):
        return None
    saved = (BS.send_message, MM.write_inbound, MM.forget_inbound, BS._entity_id)
    BS.send_message, MM.write_inbound, MM.forget_inbound = record_send, write_inbound, forget_inbound
    BS._entity_id = 9003
    try:
        caller = types.SimpleNamespace(app_id="telegram-connector", grant_id=1, platform="telegram")
        msg = CT.Incoming(chat_id="42", message_id="m-1", text="and make the dock blue",
                          reply_to=None, native_thread_id=None, display_name="Alex")
        await CT.start_turn(None, caller, msg, kind="project", ref="251", title="Checkout",
                            changed=False, new=False)
    finally:
        BS.send_message, MM.write_inbound, MM.forget_inbound, BS._entity_id = saved
    reg_kw: dict = {}

    class _Reg:
        def started_with(self, *a):
            return None             # no turn started on these words yet

        def delivered_in(self, *a):
            return None             # no turn has these words yet

        def start_or_attach(self, **kw):
            reg_kw.update(kw)
            raise TR.TurnBusyError("recorded")
    tr_get = TR.get_turn_registry
    TR.get_turn_registry = lambda: _Reg()      # send_message imports it at call time
    try:
        async for _f in BS.send_message(**sent):
            pass
    finally:
        TR.get_turn_registry = tr_get
    return sent, reg_kw


async def _plain_send_kw():
    """What a plain send (another tab's, the Circles composer's: POST
    /api/brain/messages -> brain_service.send_message) hands the registry."""
    from server import brain_service as BS
    reg_kw: dict = {}

    class _Reg:
        def started_with(self, *a):
            return None             # no turn started on these words yet

        def delivered_in(self, *a):
            return None             # no turn has these words yet

        def start_or_attach(self, **kw):
            reg_kw.update(kw)
            raise TR.TurnBusyError("recorded")
    tr_get = TR.get_turn_registry
    TR.get_turn_registry = lambda: _Reg()
    try:
        async for _f in BS.send_message(entity_id=9003, message="and make the dock blue",
                                        app_id="workbench", project_id=251):
            pass
    finally:
        TR.get_turn_registry = tr_get
    return reg_kw


CONNECTOR_SENT, CONNECTOR_REG = asyncio.run(_connector_kw())
PLAIN_REG = asyncio.run(_plain_send_kw())
PLAIN_NUDGE_KW = {k: v for k, v in PLAIN_REG.items() if k.startswith("nudge_") and v is not None}


def _step(coro):
    """Run a route coroutine that never suspends here (its awaits are stood
    in) to its end, from inside a synchronous hook of the turn."""
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    coro.close()
    raise RuntimeError("the route suspended")
# What the registry is handed about the words when they land in a running
# turn (the rest — thread key, make_inner, room — belongs to the thread).
CONNECTOR_NUDGE_KW = {k: v for k, v in CONNECTOR_REG.items()
                      if k.startswith("nudge_") and v is not None}
CONNECTOR_WORDS = CONNECTOR_SENT.get("message") or ""


def _tc(name, i):
    return {"id": f"call_{name}_{i}", "type": "function",
            "function": {"name": name, "arguments": "{}"}}


def page_rule(typed, accepted, events, cid=None):
    """use-workbench-chat: the held copy is the typed words trimmed (nudgeTurn);
    a receipt that carries the copy's id settles it (pending-sends readIndex);
    one with no id settles it when it starts with the copy's first 48
    characters. Refused, or never settled: the next message."""
    held = typed.strip()
    if cid:
        settled = any(e.get("event") == "nudge_read" and e.get("client_id") == cid for e in events)
    else:
        settled = any(e.get("event") == "nudge_read" and not e.get("client_id")
                      and (e.get("text") or "").startswith(held[:48]) for e in events)
    return (not accepted) or (not settled)


def _card_settles(path, opener):
    """Whether the shipped ask card treats the decide's answered_into_turn as
    settled: a branch for it before the card's resend, holding no copy and
    sending nothing. Read from the source, not
    modelled: the browser gates drive the cards themselves."""
    src = open(os.path.join(APP_ROOT, path)).read()
    body = src[src.index(opener):]
    body = body[:body.index("setAskBusy(false);\n", body.index("answer_turn_started"))]
    at = body.find("else if (r.data.answered_into_turn)")
    if at < 0:
        return False
    branch = body[at:body.index("} else {", at)]
    return not any(k in branch for k in ("sendWhenIdle(", "tellHost(", "send(", "nudgeRunningTurn("))


WB_CARD_SETTLES = _card_settles("workbench-web/src/components/workbench/chat-pane.tsx",
                                "const onAskAnswer = async")
CI_CARD_SETTLES = _card_settles("circles-web/src/cards/use-host-thread.ts",
                                "const answerAsk = useCallback(")


async def _scenario(when, typed, cid=None, origin="page", marker=None, merge=False,
                    end="gate", followup=True, cap_fail=False):
    """`end`: "gate" — the registry's record ends with the turn; "stop" — the
    person presses Stop on it. `followup`: False — the record has no builder
    for the thread's next turn. `cap_fail`: the next turn cannot start (the
    Host's memory holds no more)."""
    words = typed.strip()
    h = Harness([])
    h.patch()
    ran: list[str] = []
    make = _runner(h, ran)
    reg, rec, gate = await _registry_turn("late-sess-1", make if followup else None,
                                          with_session=(when != "no-session"))
    took: list[bool] = []
    from mastercode.runtime import crisis as SC
    _detect = SC.detect_crisis
    if "server.main" not in sys.modules:
        # The routes import the app module; a stub stands in for it (as in
        # test_caps_routes.py), so no app is mounted here.
        import types as _types
        import server as _srvpkg
        _stub = _types.ModuleType("server.main")
        sys.modules["server.main"] = _stub
        _srvpkg.main = _stub
    from server.routes import circles as CR
    from server.routes import tickets as TK
    from server.connectors import turns as CT
    from fastapi import HTTPException
    from mastercode import pending_words as PW
    saved_routes = (CR._audit, CR._entity_id, TR.get_turn_registry, S.answer_after_turn, PW.hold)
    after_turn: list = []          # answer_after_turn calls: (keeps_copy,)
    ask_out: dict = {}
    circles_out: dict = {}

    async def no_audit(**kw):
        return None

    async def held(pool, entity_id, session_id, text, **kw):
        # pending_words.hold's write (no database here; scripts/
        # test_pending_words_pg.py proves it on a real one): the routes hold
        # the words and answer held, so the server owns them.
        import uuid as _uuid
        return str(_uuid.uuid4())

    async def answer_after(entity_id, room, text, *, asked_session=None, max_chars=None,
                           keeps_copy=False, pending_key=None):
        # session.answer_after_turn's own send (brain_service.send_message ->
        # start_or_attach), with what that send hands the registry.
        after_turn.append(keeps_copy)
        reg.start_or_attach(thread_key=rec.thread_key, entity_id=9003, app_id="workbench",
                            message=text, make_inner=lambda: make(text), make_followup=make,
                            room=rec.room, **({"nudge_keeps_copy": True} if keeps_copy else {}))
        return True
    CR._audit, CR._entity_id = no_audit, (lambda: 3)
    TR.get_turn_registry = lambda: reg
    S.answer_after_turn = answer_after
    PW.hold = held

    class _Req:
        async def json(self):
            return {"session_id": "late-sess-1", "text": typed}

    def words_in() -> bool:
        """The words go to the running turn: the page's through the nudge
        route (True = taken, False = refused, 409); a connector's as a
        message on the thread with the kwargs its real send path hands the
        registry (True = a nudge into this turn; else a turn of its own)."""
        if origin in ("connector", "other-tab"):
            _r, mode = reg.start_or_attach(
                thread_key=rec.thread_key, entity_id=9003, app_id="circles", message=typed,
                make_inner=lambda: make(typed), make_followup=make,
                **(CONNECTOR_NUDGE_KW if origin == "connector" else PLAIN_NUDGE_KW))
            return mode == "nudged"
        if origin == "circles":
            # The real Circles nudge route (routes/circles.nudge).
            try:
                out = _step(CR.nudge(_Req()))
            except HTTPException:
                return False
            circles_out.update(out)
            return bool(out.get("ok")) and not out.get("own_turn")
        if origin in ("ask-card", "tickets-pane"):
            # The real ticket decide's delivery (routes/tickets._answer_to_turn):
            # the ask cards and the Circles Tickets pane call the same route.
            ask_out.update(_step(TK._answer_to_turn(
                3, 77, {"asked_in_session": "late-sess-1", "asked_in": rec.room},
                {"label": "Blue"}, "blue", "", "user")))
            return bool(ask_out.get("answered_into_turn"))
        return reg.nudge(rec.turn_id, typed, client_id=cid) if cid else reg.nudge(rec.turn_id, typed)
    frames: list[str] = []
    rows: list[tuple] = []
    _append = reg._append

    def append(rec_, frame):
        frames.append(frame)
        return _append(rec_, frame)
    reg._append = append
    from mastercode import memory_engine as ME
    _wtr = ME.write_turn_row

    async def write_row(pool, eid, sid, role, content, **kw):
        rows.append((role, content, kw.get("tags") or {}))
        return 1 if (kw.get("tags") or {}).get("origin") == "undelivered" else None
    ME.write_turn_row = write_row
    try:
        first = "Tidy the checkout."
        if when == "last-call":
            def last_call(_h):
                # The registry takes the words while this, the turn's last
                # call, streams: after its top-of-loop drain.
                took.append(words_in())
                return {"content": "The checkout is tidy."}
            h.script = [last_call, {"content": "Blue it is."}]
        elif when == "error":
            # A model call fails and the turn ends on its error
            # (_end_on_error): the registry takes the words while that call
            # streams, after the loop's drain, so no call of the turn reads
            # them.
            def failing_call(_h):
                took.append(words_in())
                return {"content": "", "error": "the provider answered 529 (overloaded)"}
            h.script = [{"content": "", "tool_calls": [_tc("noop_probe", 1)]}, failing_call,
                        {"content": "Blue it is."}, {"content": "Blue it is."}]
        elif when == "crisis":
            # The entry crisis check flags the message: its governance frame
            # goes out before the first model call, and the turn runs on
            # (nothing ends it). The registry takes the words
            # as that frame goes out.
            first = "CRISIS-PROBE"

            def detect(text, *a, **k):
                return ({"category": "crisis_self_harm", "path": "lexical"}
                        if "CRISIS-PROBE" in str(text) else None)
            SC.detect_crisis = detect

            def on_event(e):
                if e.get("event") == "governance" and not took:
                    took.append(words_in())
            h.on_event = on_event
            h.script = [{"content": "Blue it is."}, {"content": "Blue it is."}]
        elif when == "backstop":
            # The round backstop (max_tool_rounds 2): the turn's first call and
            # both rounds ask for tools, and the registry takes the words while
            # the LAST round's call streams. The loop ends there, past its
            # last drain (session.py, the exits past the loop).
            h.params = {"max_tool_rounds": 2}

            def last_round(_h):
                took.append(words_in())
                return {"content": "", "tool_calls": [_tc("noop_probe", 3)]}
            h.script = [{"content": "", "tool_calls": [_tc("noop_probe", 1)]},
                        {"content": "", "tool_calls": [_tc("noop_probe", 2)]},
                        last_round, {"content": "Blue it is."}, {"content": "Blue it is."}]
        elif when == "allowance":
            # The turn allowance stops the turn in its first round: the
            # registry takes the words in that round, after its drain and
            # before the stop releases the holds (TurnSpend.check runs right
            # before it). The stop's last call reads nothing more (tools off),
            # so the words are not read by this turn.
            def stop_check(self_, new_tokens, output_reserve):
                if not took:
                    took.append(words_in())
                return "stop", "The turn allowance is reached. Answer now."
            h.spend_check = stop_check
            h.script = [{"content": "", "tool_calls": [_tc("noop_probe", 1)]},
                        {"content": "Here is where it stands."}, {"content": "Blue it is."}]
        elif when == "no-session":
            # The words go in while the turn has no session yet, and it ends
            # without ever having one: nothing of the runtime runs for it.
            took.append(words_in())
        else:
            h.script = [{"content": "The checkout is tidy."}]
            orig = h._saved[-1]

            def release_then_words(sid):
                orig(sid)
                if sid == "late-sess-1" and not took:
                    took.append(words_in())
            S.release_holds = release_then_words
        if when != "no-session":
            await h.turn(first)
        stranded = list(S._pending_nudges.get("late-sess-1") or [])
        # The turn reads no more input once it has ended (its holds went).
        stopped_reading = S.turn_stopped_reading("late-sess-1")
        accepted = bool(took and took[0])
        turn1_calls = len(h.calls)
        S.release_holds = h._saved[-1]
        h.spend_check = None
        h.on_event = None
        SC.detect_crisis = _detect
        # Each client's own rule for the words it holds. A connector and a
        # plain send keep no copy: they never send the words again.
        read_48 = any(e.get("event") == "nudge_read" and (e.get("text") or "").startswith(words[:48])
                      for e in h.events)
        if origin == "page":
            resend = page_rule(typed, accepted, h.events, cid)
        elif origin == "circles":
            # use-circles-chat nudgeRunningTurn: words the route
            # took (200, a nudge or a turn of their own) are the server's and
            # never sent again; only words it refused (409) go as the next
            # message.
            resend = not circles_out.get("ok")
        elif origin == "ask-card":
            # The cards: the answer's own turn started -> follow it, never
            # resend; answered into the turn -> settled, if the shipped cards
            # say so (both of them); else held (sendWhenIdle / tellHost)
            # until the receipt settles it.
            resend = (not ask_out.get("answer_turn_started")
                      and not (ask_out.get("answered_into_turn")
                               and WB_CARD_SETTLES and CI_CARD_SETTLES)
                      and not read_48)
        else:
            # A connector, a plain send, the Circles Tickets pane: never again.
            resend = False
        if merge:
            # Another tab's message after the turn stopped reading: a turn of
            # its own, registered now and run once this one is done.
            reg.start_or_attach(thread_key=rec.thread_key, entity_id=9003, app_id="workbench",
                                message="Also the footer.", make_inner=lambda: make("Also the footer."),
                                make_followup=make, room=rec.room)
        connector_reply = None
        if origin == "connector":
            # This chat's reply follows the turn its message started; the
            # words are nudged into it (connectors/turns.start_turn: `held`).
            connector_reply = CT.Reply(reply_id=rec.turn_id, app_id="telegram-connector",
                                       grant_id=1, platform="telegram", chat_id="42",
                                       state="started", rec=rec)
            connector_reply.nudges.append(CONNECTOR_WORDS.strip())

        async def settle():
            for _ in range(20):
                running = [r for r in list(reg._by_id.values()) if not r.done.is_set()]
                if not running:
                    break
                await asyncio.wait_for(running[0].done.wait(), 10)

        def send_again():
            # The client's next message: the ordinary send (send_message ->
            # start_or_attach); the same words as a running turn's reattach.
            reg.start_or_attach(thread_key=rec.thread_key, entity_id=9003, app_id="workbench",
                                message=words, make_inner=lambda: make(words), make_followup=make,
                                room=rec.room)
        if cap_fail:
            # The thread's next turn cannot start: the Host's memory holds no
            # more (start_or_attach raises TurnCapError for it).
            _soa = reg.start_or_attach

            def no_room(**kw):
                if kw.get("message") != rec.user_message:
                    raise TR.TurnCapError("the Host's memory holds no more turns right now")
                return _soa(**kw)
            reg.start_or_attach = no_room
        # The registry's record of the turn ends — or the person stops it —;
        # any turn it starts on the thread from here (the words sent on, or a
        # turn of their own) runs to its end.
        if end == "stop":
            await reg.cancel(rec.turn_id)
        gate.set()
        await asyncio.wait_for(rec.done.wait(), 5)
        if cap_fail:
            reg.start_or_attach = _soa
        if TR._PENDING_ROW_WRITES:
            await asyncio.gather(*list(TR._PENDING_ROW_WRITES), return_exceptions=True)
        followed = None
        if connector_reply is not None:
            nxt = await CT._followed_turn(connector_reply, rec)
            followed = nxt.turn_id if nxt is not None else None
        if resend and origin == "circles":
            send_again()               # at the ready edge of its stream
        await settle()
        if resend and origin in ("page", "ask-card"):
            send_again()               # when the thread is next idle
            await settle()
        left = list((getattr(S, "_unread_for_next_turn", {}) or {}).get("late-sess-1") or [])
        return {"accepted": accepted, "reached": h.reached(marker or words), "resend": resend,
                "stranded": stranded, "turn1_calls": turn1_calls,
                "stopped_reading": stopped_reading, "left": left, "next_turns": list(ran),
                "requeued": [json.loads(f[2:]) for f in rec.frames if "nudge_requeued" in f],
                "receipt": [e.get("text") for e in h.events if e.get("event") == "nudge_read"],
                "receipt_ids": [e.get("client_id") for e in h.events if e.get("event") == "nudge_read"],
                "frames": [json.loads(f[2:]) for f in frames if '"type": "nudge"' in f],
                "rows": [r for r in rows if r[2].get("nudge")], "followed": followed,
                "reply_turn_ids": connector_reply.turn_ids() if connector_reply else [],
                "reply_joined": list(connector_reply.joined) if connector_reply else [],
                "after_turn": list(after_turn), "ask": dict(ask_out),
                # The ended turn's stream lines (3:), and what a connector's
                # reply that follows it sends its chat as the turn's errors
                # (connectors/turns._segment_parts reads them with read_frames).
                "errors": CT.read_frames(list(rec.frames))[2],
                "undelivered_rows": [rw for rw in rows if rw[2].get("origin") == "undelivered"],
                "status": rec.status}
    finally:
        (CR._audit, CR._entity_id, TR.get_turn_registry, S.answer_after_turn, PW.hold) = saved_routes
        ME.write_turn_row = _wtr
        reg._append = _append
        SC.detect_crisis = _detect
        gate.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(rec.done.wait(), 2)
        h.unpatch()


r = asyncio.run(_scenario("last-call", "and make the dock blue"))
check("1: words the registry takes while the turn's LAST call streams are read by THAT turn "
      "(one more round), and the receipt says so", r["accepted"] and r["receipt"] == ["and make the dock blue"]
      and r["turn1_calls"] == 2, r)
check("1: ...so they reach the Host exactly once, and never again as a message",
      r["reached"] == 1 and not r["resend"] and not r["stranded"], r)

r = asyncio.run(_scenario("after-release", "and make the dock blue"))
check("2: words arriving after the turn released its holds are refused (it reads nothing more)",
      r["accepted"] is False and r["receipt"] == [] and not r["stranded"], r)
check("2: ...so the page sends them as the next message: they reach the Host exactly once",
      r["resend"] and r["reached"] == 1, r)

r = r3 = asyncio.run(_scenario("last-call", "\n  and make the dock blue"))
check("3: the receipt carries the words stripped (push_nudge strips them before the drain)",
      r["receipt"] == ["and make the dock blue"], r["receipt"])

r = asyncio.run(_scenario("backstop", "and make the dock blue"))
check("4: words the registry takes during the round backstop's LAST round are read by THAT "
      "turn (one more round, as at the text exit), and the receipt says so",
      r["accepted"] and r["receipt"] == ["and make the dock blue"], r)
check("4: ...so nothing is left in the session's queue for a later turn to read",
      not r["stranded"], r)
check("4: ...and they reach the Host exactly once, never again as a message",
      r["reached"] == 1 and not r["resend"], r)

r = asyncio.run(_scenario("allowance", "and make the dock blue"))
check("5: words the registry takes in the round the turn allowance stops are not read by that turn "
      "(its last call reads nothing more), so no receipt", r["accepted"] and r["receipt"] == [], r)
check("5: ...and they are taken off the session's queue at the exit, so no later turn on a session that "
      "keeps its id reads them a second time", not r["stranded"], r)
check("5: ...so the page sends them as the next message: they reach the Host exactly once",
      r["resend"] and r["reached"] == 1, r)

# 6. The words carry a pasted key (a fake hard shape in the first 48
# characters), sent with the page's id: the turn reads them with the key cut
# out, so their text never matches the page's copy; the frame, the receipt and
# the saved row carry the id, and the page settles its copy by it.
KEY = "AKIA" + "Q7XZ4RTY2M9PLK3W"
typed6 = f"use {KEY} for the deploy, then make the dock blue"
r = asyncio.run(_scenario("last-call", typed6, cid="u-1790000000000-7"))
check("6: (the key is cut from the words the turn reads: their text no longer matches the page's copy)",
      r["receipt"] and KEY not in r["receipt"][0] and "[REDACTED:" in r["receipt"][0]
      and not r["receipt"][0].startswith(typed6[:48]), r["receipt"])
check("6: the registry's nudge frame and the turn's receipt carry the page's id",
      r["frames"] == [[{"type": "nudge", "text": typed6, "client_id": "u-1790000000000-7"}]]
      and r["receipt_ids"] == ["u-1790000000000-7"], {"frames": r["frames"], "ids": r["receipt_ids"]})
# The saved row: this harness's turn runs on a lane that writes no user rows
# (no app), so the row's metadata is read in the drain's source; the browser
# gate (test_workbench_book.py scenario K2) reloads from a saved row with it.
_drain = open(os.path.join(APP_ROOT, "brain/mastercode/runtime/session.py")).read()
_drain = _drain[_drain.index("import write_turn_row as _wtr_n"):][:900]
check("6: ...and the nudge's saved row carries it in its metadata (the drain's row tags)",
      '"nudge": True,' in _drain and '**({"client_id": _cids[_ni]} if _cids[_ni] else {}),' in _drain
      and '**_sender_tags(_cids[_ni])}' in _drain)
check("6: ...so the page settles its copy by the id: the words reach the Host once, never again as a message",
      r["accepted"] and not r["resend"] and r["turn1_calls"] == 2 and not r["stranded"], r)
check("6: the session keeps only a well-formed id (1-64 of A-Z a-z 0-9 _ -)",
      S.nudge_id_ok("u-1790000000000-7") and not S.nudge_id_ok("") and not S.nudge_id_ok("a b")
      and not S.nudge_id_ok("x" * 65) and not S.nudge_id_ok(7) and not S.nudge_id_ok("<script>"))

# The page's held copy: words typed with a leading newline must be held as
# the receipt carries them, or the read words go again as the next message.
# This reads the shipped source, not a model of it (the browser gate drives
# it end to end: scripts/test_workbench_book.py scenario E, "Q:" checks).
_hook = open(os.path.join(APP_ROOT, "workbench-web/src/hooks/use-workbench-chat.ts")).read()
_nt = _hook[_hook.index("const nudgeTurn = useCallback("):]
_nt = _nt[:_nt.index("[patchThread, drainPending]")]
_trims = ("const text = raw.trim();" in _nt and "PENDING_SENDS.push({ key, projectId, text" in _nt
          and "nudgeTurnApi(turnId, text, clientId)" in _nt)
check("3: nudgeTurn holds, echoes and sends the words trimmed (the receipt's words)", _trims)
held_as_typed = not _trims
twice = r3["reached"] + (1 if held_as_typed and not any(
    t.startswith("\n  and make the dock blue"[:48]) for t in r3["receipt"]) else 0)
check("3: ...so words typed with a leading newline reach the Host once, not twice", twice == 1,
      {"reached_in_turn": r3["reached"], "held_as_typed": held_as_typed})

# ── Every sender x every exit ──
# The rule: words the server accepts as a nudge are the server's to deliver
# once, unless the ROUTE they came through is one whose client keeps a copy
# and sends it again (the page's nudge route, the Circles nudge route, the
# ask card) — decided on the server side, never by a flag a sender passes.
check("C: a connector's message reaches the registry with nothing said about copies: the server "
      "delivers it (connectors/turns.start_turn -> brain_service.send_message -> start_or_attach)",
      CONNECTOR_SENT.get("app_id") == "circles" and "and make the dock blue" in CONNECTOR_WORDS
      and not any(k.startswith("nudge_") and "copy" in k for k in CONNECTOR_SENT)
      and not CONNECTOR_NUDGE_KW.get("nudge_keeps_copy"),
      {"sent": {k: v for k, v in CONNECTOR_SENT.items() if k != "message"}, "kw": CONNECTOR_NUDGE_KW})
check("C: ...and so does a plain send (another tab's, the Circles composer's)",
      not PLAIN_NUDGE_KW.get("nudge_keeps_copy") and not PLAIN_NUDGE_KW.get("nudge_no_copy"),
      PLAIN_NUDGE_KW)

ASK_TEXT = "Ticket #77 — I chose “Blue”."
KEEPS = ("page", "circles", "ask-card")
SENDERS = ("page", "circles", "ask-card", "tickets-pane", "connector", "other-tab")
# Words the server delivers once: no client sends them again (the decide
# route's answer is the server's, so Circles'
# Decisions sheet, which never resends, is answered too).
SERVER_OWNS = ("connector", "other-tab", "tickets-pane", "ask-card", "circles")
check("T: the Workbench ask card treats answered_into_turn as settled: no copy held, nothing sent "
      "again (chat-pane.tsx onAskAnswer)", WB_CARD_SETTLES)
check("T: ...and so does the Circles ask card (cards/use-host-thread.ts answerAsk)",
      CI_CARD_SETTLES)
_tp = open(os.path.join(APP_ROOT, "circles-web/src/cards/tickets.tsx")).read()
_tp = _tp[_tp.index("const decide = async"):]
_tp = _tp[:_tp.index("\n  };\n")]
check("T: (Circles' Decisions sheet calls the same decide route and never sends the answer itself)",
      "decideTicket(" in _tp and not any(k in _tp for k in ("send(", "nudge", "sendWhenIdle")), _tp[:200])
EXITS = (("last-call", "the reply (words taken while its last call streams)"),
         ("after-release", "the reply (words sent after it stopped reading)"),
         ("backstop", "the round backstop"),
         ("allowance", "the allowance's stop"),
         ("error", "a model error (_end_on_error)"),
         ("crisis", "a crisis-check hit at the turn's start (the turn runs on)"),
         ("no-session", "a turn that ends with no session"))
for when, label in EXITS:
    for origin in SENDERS:
        if when == "no-session" and origin in ("circles", "ask-card", "tickets-pane"):
            # All need the turn's session to nudge (the Circles client has
            # no session id to send to, and a ticket's turn asked from one).
            continue
        typed = (ASK_TEXT if origin in ("ask-card", "tickets-pane")
                 else CONNECTOR_WORDS if origin == "connector" else "and make the dock blue")
        marker = "Ticket #77" if origin in ("ask-card", "tickets-pane") else "and make the dock blue"
        r = asyncio.run(_scenario(when, typed, origin=origin, marker=marker))
        brief = {k: r[k] for k in ("accepted", "reached", "resend", "stranded", "left",
                                   "stopped_reading", "next_turns", "receipt", "requeued",
                                   "followed", "after_turn", "ask")}
        tag = f"X {label} / {origin}"
        check(f"{tag}: the words reach the Host exactly once", r["reached"] == 1, brief)
        check(f"{tag}: nothing stays queued (the turn's queue, the words held for a next turn)",
              not r["stranded"] and not r["left"], brief)
        if when != "no-session":
            check(f"{tag}: the turn's holds are released at its end (it reads no more)",
                  r["stopped_reading"] is True, brief)
        # Only the Workbench page's route keeps a copy: the
        # Circles route's words are held by the server (pending_words).
        kept_by_client = origin == "page"
        if kept_by_client:
            check(f"{tag}: the server never sends on words its client keeps a copy of",
                  not r["requeued"], brief)
        if origin in (*KEEPS, "tickets-pane") and r["after_turn"]:
            check(f"{tag}: a turn of their own for the words is the server's to deliver "
                  "(no route asks it to leave them to a client)",
                  r["after_turn"] == [False], brief)
        if origin in SERVER_OWNS:
            check(f"{tag}: the sender never sends them again; at most one turn of the thread "
                  "carries them", not r["resend"] and len(r["next_turns"]) <= 1, brief)
            if r["accepted"] and not r["receipt"]:
                check(f"{tag}: the turn ended without reading them, so the registry sends them on "
                      "as the thread's next turn, once",
                      len(r["requeued"]) == 1 and len(r["next_turns"]) == 1
                      and marker in r["next_turns"][0], brief)
        if origin == "connector" and r["requeued"]:
            check(f"{tag}: the connector's reply follows the turn its words went on to",
                  r["followed"] == r["requeued"][0][0]["turn_id"], brief)
            check(f"{tag}: ...a turn started for its words, so it is the connector's own "
                  "(a revoke stops it)", r["followed"] in r["reply_turn_ids"], brief)

# M. The thread's next turn is already waiting when the words go on (another
# tab sent a message after this turn stopped reading): the words become a
# nudge into it, and the connector's reply still follows that turn.
r = asyncio.run(_scenario("allowance", CONNECTOR_WORDS, origin="connector",
                          marker="and make the dock blue", merge=True))
brief = {k: r[k] for k in ("accepted", "reached", "requeued", "followed", "next_turns", "left")}
check("M: words sent on into a next turn that was already running go there as a nudge "
      "(nudge_requeued names that turn)",
      len(r["requeued"]) == 1 and r["next_turns"][:1] == ["Also the footer."], brief)
check("M: ...and the connector's reply follows that turn", r["requeued"]
      and r["followed"] == r["requeued"][0][0]["turn_id"], brief)
check("M: ...and the words reach the Host exactly once, nothing left queued",
      r["reached"] == 1 and not r["left"], brief)
check("M: ...but that turn is the other tab's, never the connector's: a revoke of the "
      "connector does not stop it", r["followed"] and r["followed"] not in r["reply_turn_ids"]
      and r["reply_joined"] == [r["followed"]], {**brief, "ids": r["reply_turn_ids"]})


# N. Revoke stops the connector's own turns, and never a turn it only joined.
async def _revoke_scenario():
    from server.connectors import turns as CT2
    import server.turn_registry as TR2

    class _Rec:
        def __init__(self):
            self.done = asyncio.Event()

    recs = {"own-1": _Rec(), "own-2": _Rec(), "page-turn": _Rec()}
    cancelled: list = []

    class _Reg:
        def get(self, tid):
            return recs.get(tid)

        async def cancel(self, tid):
            cancelled.append(tid)

    saved = TR2.get_turn_registry
    TR2.get_turn_registry = lambda: _Reg()
    reply = CT2.Reply(reply_id="own-1", app_id="revoke-app", grant_id=9, platform="telegram",
                      chat_id="7", state="started", rec=None)
    reply.followed.append("own-2")
    reply.joined.append("page-turn")
    for tid in ("own-1", "own-2", "page-turn"):
        CT2._REPLIES[tid] = reply
    try:
        n = await CT2.cancel_for_revoke(app_id="revoke-app", grant_id=9, reason="test")
    finally:
        TR2.get_turn_registry = saved
        for tid in ("own-1", "own-2", "page-turn"):
            CT2._REPLIES.pop(tid, None)
    return n, cancelled


_n, _cancelled = asyncio.run(_revoke_scenario())
check("N: a revoke stops the connector's own turns and never one it only joined",
      sorted(_cancelled) == ["own-1", "own-2"] and _n == 2, _cancelled)

# ── U. Words the server owns but cannot deliver say so ──
# The three ends that cannot send them on: the person's Stop (only the Stop
# means "do not carry on": still not carried on, but said), a record with no
# builder for the thread's next turn, and a next turn that cannot start. Each
# leaves a line on the ended turn's stream and a row in the Host thread with
# the words; a connector's words get a line in plain words that its chat
# receives (the reply that follows the turn sends the stream's errors).
UNDELIVERABLE = (("the person's Stop", {"end": "stop"}, "stopped before the Host read"),
                 ("no builder for the next turn", {"followup": False}, "nothing could send"),
                 ("the next turn cannot start", {"cap_fail": True}, "could not start"))
for label, kw, why in UNDELIVERABLE:
    for origin in ("other-tab", "tickets-pane", "connector"):
        typed = (ASK_TEXT if origin == "tickets-pane"
                 else CONNECTOR_WORDS if origin == "connector" else "and make the dock blue")
        marker = "Ticket #77" if origin == "tickets-pane" else "and make the dock blue"
        r = asyncio.run(_scenario("allowance", typed, origin=origin, marker=marker, **kw))
        brief = {k: r[k] for k in ("accepted", "reached", "requeued", "next_turns", "left",
                                   "errors", "undelivered_rows", "status")}
        tag = f"U {label} / {origin}"
        check(f"{tag}: (the turn ended without reading the words)",
              r["accepted"] and not r["receipt"], brief)
        check(f"{tag}: they are not carried on: nothing reached the Host, nothing left queued",
              r["reached"] == 0 and not r["next_turns"] and not r["left"], brief)
        _rows = r["undelivered_rows"]
        check(f"{tag}: the Host thread gets a row with the words: not delivered, send them again",
              len(_rows) == 1 and marker in _rows[0][1]
              and _rows[0][1].startswith("(Not delivered to the Host: ") and why in _rows[0][1]
              and "Send it again." in _rows[0][1] and _rows[0][2].get("app_id") == "workbench",
              brief)
        if origin == "connector":
            check(f"{tag}: its chat is told in plain words that the message was not delivered "
                  "and should be sent again",
                  any(e.startswith("Your message was not delivered to the Host: ") and why in e
                      and e.endswith("Send it again.") for e in r["errors"]), r["errors"])
        else:
            check(f"{tag}: the ended turn's stream says so, with the words",
                  any(e.startswith("Not delivered to the Host: ") and marker in e and why in e
                      for e in r["errors"]), r["errors"])


# ── S. A second cancel while the registry waits for the runtime task ──
# brain_service._send_message_inner, run for real: its finally cancels the
# runtime task and awaits it. The registry's task is cancelled a second time
# while it waits there, and the runtime's teardown outlasts that cancel (a
# model call that takes 0.4 s to unwind whatever it is told). The question:
# can _requeue_undelivered then run before the runtime's TurnEnd
# (end_turn_input), leaving the words in _unread_for_next_turn?
async def _second_cancel(first: str, second: str):
    from server import brain_service as BS
    from server.connectors import turns as CT2
    from mastercode import memory_engine as ME
    h = Harness([])
    h.patch()
    in_model, tearing = asyncio.Event(), asyncio.Event()
    late: list = []

    async def blocking_model(messages=None, *a, **kw):
        h.calls.append([dict(m) for m in (messages or [])])
        in_model.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            tearing.set()
            loop = asyncio.get_running_loop()
            until = loop.time() + 0.4
            while loop.time() < until:
                try:
                    await asyncio.sleep(0.02)
                except asyncio.CancelledError:
                    late.append(loop.time())     # the second cancel reached the runtime
            raise
    S._call_entity_model = PG.call_model = blocking_model
    order: list = []
    _eti, _req = S.end_turn_input, TR.TurnRegistry._requeue_undelivered

    def end_input(sid):
        order.append(("end_turn_input", sid))
        return _eti(sid)

    def requeue(self_, rec_):
        order.append(("requeue", rec_.session_id))
        return _req(self_, rec_)
    S.end_turn_input, TR.TurnRegistry._requeue_undelivered = end_input, requeue
    reg = TR.TurnRegistry()
    saved = (BS._runtime, TR.get_turn_registry, ME.write_turn_row)
    BS._runtime, TR.get_turn_registry = h.rt, (lambda: reg)
    ran: list = []
    rows: list = []

    def follow(text, frame=None):
        async def gen():
            ran.append(text)
            yield "e:{}\n"
        return gen()

    async def write_row(pool, eid, sid, role, content, **kw):
        rows.append((role, content, kw.get("tags") or {}))
        return 1 if (kw.get("tags") or {}).get("origin") == "undelivered" else None
    ME.write_turn_row = write_row
    key = "project|3|251"
    room = {"app_id": "workbench", "project_id": 251, "thread_id": None, "history_context": None}
    words = "and make the dock blue"
    try:
        rec, _ = reg.start_or_attach(
            thread_key=key, entity_id=9003, app_id="workbench", message="Tidy the checkout.",
            make_inner=lambda: BS._send_message_inner(message="Tidy the checkout.", entity_id=9003,
                                                      app_id="workbench", project_id=251),
            make_followup=follow, room=room)
        await asyncio.wait_for(in_model.wait(), 5)
        for _ in range(200):
            if rec.session_id:
                break
            await asyncio.sleep(0.01)
        # Another tab's words: a nudge into the turn, the server's to deliver.
        _r, mode = reg.start_or_attach(thread_key=key, entity_id=9003, app_id="workbench",
                                       message=words, make_inner=lambda: follow(words),
                                       make_followup=follow, room=room)
        queued = list(S._pending_nudges.get(rec.session_id or "") or [])

        def hit(kind):
            if kind == "stop":
                return asyncio.ensure_future(reg.cancel(rec.turn_id))
            rec.task.cancel()
            return None
        t1 = hit(first)
        await asyncio.wait_for(tearing.wait(), 5)
        await asyncio.sleep(0.05)
        waiting = not rec.done.is_set()
        t2 = hit(second)
        await asyncio.wait_for(rec.done.wait(), 5)
        for t in (t1, t2):
            if t is not None:
                await t
        for _ in range(20):
            running = [x for x in list(reg._by_id.values()) if not x.done.is_set()]
            if not running:
                break
            await asyncio.wait_for(running[0].done.wait(), 5)
        if TR._PENDING_ROW_WRITES:
            await asyncio.gather(*list(TR._PENDING_ROW_WRITES), return_exceptions=True)
        sid = rec.session_id
        return {"mode": mode, "queued": queued, "order": order, "late": len(late),
                "waiting": waiting, "left": list(S._unread_for_next_turn.get(sid or "") or []),
                "ran": ran, "status": rec.status, "stopped_by_user": rec.stopped_by_user,
                "sid": sid, "errors": CT2.read_frames(list(rec.frames))[2],
                "undelivered_rows": [rw for rw in rows if rw[2].get("origin") == "undelivered"]}
    finally:
        S.end_turn_input, TR.TurnRegistry._requeue_undelivered = _eti, _req
        BS._runtime, TR.get_turn_registry, ME.write_turn_row = saved
        h.unpatch()


for first, second, label in (("stop", "stop", "the person's Stop pressed twice"),
                             ("cancel", "cancel", "the turn's task cancelled twice"),
                             ("cancel", "stop", "a cancel, then the person's Stop")):
    r = asyncio.run(_second_cancel(first, second))
    sid = r["sid"]
    tag = f"S {label}"
    check(f"{tag}: (the words went into the running turn as a nudge, and the second cancel came "
          "while the registry waited for the runtime task, which it reached)",
          r["mode"] == "nudged" and r["queued"] == ["and make the dock blue"] and r["waiting"]
          and r["late"] >= 1, r)
    check(f"{tag}: the runtime's TurnEnd (end_turn_input) runs before _requeue_undelivered",
          sid and ("end_turn_input", sid) in r["order"] and ("requeue", sid) in r["order"]
          and r["order"].index(("end_turn_input", sid)) < r["order"].index(("requeue", sid)),
          r["order"])
    check(f"{tag}: so nothing is left in _unread_for_next_turn", not r["left"], r)
    if r["stopped_by_user"]:
        check(f"{tag}: the Stop keeps the words back, and says so in the thread and the stream",
              r["ran"] == [] and len(r["undelivered_rows"]) == 1
              and "and make the dock blue" in r["undelivered_rows"][0][1]
              and any(e.startswith("Not delivered to the Host: ") for e in r["errors"]), r)
    else:
        check(f"{tag}: the words go on as the thread's next turn, once",
              r["ran"] == ["and make the dock blue"] and not r["undelivered_rows"], r)

# ── I. The sender's id on the person's row ──
async def _id_rows(cid, *, refuse=False, message="Check the north fence."):
    """The rows the real runtime is asked to write for a send of `message`
    from the Workbench on project 251 under `cid` (None: no id sent)."""
    from server import brain_service as BS
    from mastercode import memory_engine as ME
    h = Harness([{"content": "On it."}])
    h.patch()
    rows: list[tuple] = []
    _wtr = ME.write_turn_row

    async def write_row(pool, eid, sid, role, content, **kw):
        rows.append((role, content, dict(kw.get("tags") or {}), kw.get("project_id")))
        return 1
    ME.write_turn_row = write_row
    saved_rt = BS._runtime
    BS._runtime = h.rt
    if refuse:
        async def hold(entity_id, progress_cb=None, system=False):
            return True, {"response": "My tools have not arrived.", "identity_pending": True,
                          "identity": {"status": "pending", "detail": "", "retrying": True, "began": None},
                          "identity_waited_s": 0.1}
        h.rt._hold_turn_for_identity = hold
    try:
        kw = dict(entity_id=9003, message=message, app_id="workbench", project_id=251)
        if cid is not None:
            kw["client_id"] = cid
        try:
            async for _f in BS._send_message_inner(**kw):
                pass
        except TypeError as e:           # a tree whose turn takes no client_id: its checks fail, the run goes on
            rows.append(("error", str(e), {}, None))
        await asyncio.sleep(0.1)         # the user row is written in a task of its own
        return rows
    finally:
        BS._runtime = saved_rt
        ME.write_turn_row = _wtr
        h.unpatch()


async def _client_send_kw(cid):
    """What send_message hands the registry for a send under `cid`."""
    from server import brain_service as BS
    reg_kw: dict = {}

    class _Reg:
        def started_with(self, *a):
            return None             # no turn started on these words yet

        def delivered_in(self, *a):
            return None             # no turn has these words yet

        def start_or_attach(self, **kw):
            reg_kw.update(kw)
            raise TR.TurnBusyError("recorded")
    tr_get = TR.get_turn_registry
    TR.get_turn_registry = lambda: _Reg()
    try:
        async for _f in BS.send_message(entity_id=9003, message="Check the north fence.", app_id="workbench",
                                        project_id=251, **({"client_id": cid} if cid else {})):
            pass
    finally:
        TR.get_turn_registry = tr_get
    return reg_kw


ID = "0123456789abcdef0123456789abcdef"
rows = asyncio.run(_id_rows(ID))
users = [r for r in rows if r[0] == "user"]
tags = users[0][2] if users else {}
check("I: a turn that runs writes the person's row once, with the words and the sender's id on it",
      len(users) == 1 and users[0][1] == "Check the north fence." and tags.get("client_id") == ID
      and users[0][3] == 251 and "nudge" not in tags, rows)
check("I: ...beside the tags every such row has (the app, the page's scope, the turn's token)",
      tags.get("app_id") == "workbench" and (tags.get("source_context") or {}).get("ref") == "251"
      and tags.get("turn_token"), tags or rows)
rows = asyncio.run(_id_rows(None))
users = [r for r in rows if r[0] == "user"]
check("I: a send without an id writes its row as before: no client_id on it",
      len(users) == 1 and "client_id" not in users[0][2], rows)
for what, bad in (("two words", "two words"), ("65 characters", "x" * 65), ("empty", "")):
    rows = asyncio.run(_id_rows(bad))
    users = [r for r in rows if r[0] == "user"]
    check(f"I: an id that is not 1-64 of A-Z a-z 0-9 _ - ({what}) is not kept",
          len(users) == 1 and "client_id" not in users[0][2], rows)
rows = asyncio.run(_id_rows(ID, refuse=True))
users = [r for r in rows if r[0] == "user"]
notes = [r for r in rows if r[0] == "assistant"]
check("I: an identity hold that refuses the request saves the person's row with the sender's id on it "
      "(a retry finds the words kept, and the saved request runs by itself)",
      len(users) == 1 and users[0][1] == "Check the north fence." and users[0][2].get("client_id") == ID, rows)
check("I: ...and not on the assistant's note beside it",
      len(notes) == 1 and "client_id" not in notes[0][2], notes)


async def _late_attach_during_refused_row():
    """On the identity-hold path: words under another id attach to the
    turn while the row the hold saves is being written. Their marker may be
    marked only once that row is written, never before: a restart in between
    would leave a marker that says delivered and no row."""
    from server import brain_service as BS
    from server.connectors import thread_pick as TP
    from mastercode import memory_engine as ME
    h = Harness([{"content": "On it."}])
    h.patch()
    order: list = []
    reg = TR.TurnRegistry()
    rec = TR.TurnRecord(turn_id="t" * 32, thread_key="workbench|3|251", entity_id=9003,
                        app_id="workbench", user_message="Check the north fence.", started_at=0.0)
    _wtr, _mark = ME.write_turn_row, TP.mark_index_ids_reattached

    async def write_row(pool, eid, sid, role, content, **kw):
        if role == "user":
            reg._attach_ids(rec, "late-id")      # the Index's send, on the same words
            await asyncio.sleep(0.05)            # the row is still being written
            order.append("row")
        return 1

    async def mark(pool, eid, cid):
        order.append("mark")
    ME.write_turn_row, TP.mark_index_ids_reattached = write_row, mark
    saved_rt = BS._runtime
    BS._runtime = h.rt

    async def hold(entity_id, progress_cb=None, system=False):
        return True, {"response": "My tools have not arrived.", "identity_pending": True,
                      "identity": {"status": "pending", "detail": "", "retrying": True, "began": None},
                      "identity_waited_s": 0.1}
    h.rt._hold_turn_for_identity = hold
    tok = TR.CURRENT_TURN_RECORD.set(rec)
    try:
        async for _f in BS._send_message_inner(entity_id=9003, message="Check the north fence.",
                                               app_id="workbench", project_id=251, client_id=ID):
            pass
        await asyncio.sleep(0.1)
        return order
    finally:
        TR.CURRENT_TURN_RECORD.reset(tok)
        BS._runtime = saved_rt
        ME.write_turn_row, TP.mark_index_ids_reattached = _wtr, _mark
        h.unpatch()


order = asyncio.run(_late_attach_during_refused_row())
check("I: an id that attaches while the identity hold writes the person's row is marked only after that row "
      "is written", order == ["row", "mark"], order)

kw = asyncio.run(_client_send_kw(ID))
check("I: send_message hands the registry the id to keep with the words if they land in a running turn "
      "as a nudge (and it is not a copy the client keeps)",
      kw.get("nudge_client_id") == ID and not kw.get("nudge_keeps_copy"), {k: v for k, v in kw.items() if k.startswith("nudge")})
kw = asyncio.run(_client_send_kw(None))
check("I: ...and without an id it hands it nothing more than before",
      "nudge_client_id" not in kw, {k: v for k, v in kw.items() if k.startswith("nudge")})


# ── J. Words a turn ended without reading keep their senders' ids ──
# A Workbench Index send that lands in a running turn as a nudge, which that turn
# never reads, goes on as the thread's next turn (end_turn_input ->
# _requeue_undelivered). The id must reach that turn's user row, or a retry of the
# send finds no row with it and delivers the words again. Several such nudges are
# joined into one turn, whose row keeps all their ids as a list. When the thread's
# next turn already waits on those very words, they attach to it (start_or_attach,
# "reattached") and no row can carry the id: the marker of each id records that
# instead (thread_pick.mark_index_ids_reattached).
def _unread_ids():
    sid = "unread-ids-1"
    S.push_nudge(sid, "First words.", client_id="id-a")
    S.push_nudge(sid, "Second words.")
    S.push_nudge(sid, "Third words.", client_id="id-c")
    S.end_turn_input(sid)
    return S.take_unread_for_next_turn(sid)


got = _unread_ids()
check("J: words a turn ends without reading are held for the next turn with their senders' ids "
      "(end_turn_input)", [(u[0], u[2] if len(u) > 2 else "<none>") for u in got]
      == [("First words.", "id-a"), ("Second words.", None), ("Third words.", "id-c")], got)


async def _requeued_with(ids, *, with_session=True, tag="r"):
    """What the registry builds the thread's next turn with, for words sent
    into a turn under `ids` (None: no id) that it ended without reading."""
    sid = f"requeue-ids-{tag}"
    made: list = []

    def make(text, frame=None, **kw):
        made.append({"text": text, **kw})

        async def gen():
            yield "e:{}\n"
        return gen()
    reg, rec, gate = await _registry_turn(sid, make_followup=make, with_session=with_session)
    for i, cid in enumerate(ids):
        reg.start_or_attach(thread_key=rec.thread_key, entity_id=9003, app_id="workbench", message=f"Words {i}.",
                            make_inner=lambda: None, **({"nudge_client_id": cid} if cid else {}))
    if with_session:
        S.end_turn_input(sid)            # the runtime's TurnEnd, at the turn's exit
    gate.set()
    await asyncio.wait_for(rec.done.wait(), 5)
    await asyncio.sleep(0.05)
    return made


made = asyncio.run(_requeued_with(["id-1"], tag="one"))
check("J: one unread send's id goes to the next turn the registry builds for its words",
      made == [{"text": "Words 0.", "client_id": "id-1"}], made)
made = asyncio.run(_requeued_with(["id-1", None, "id-2"], tag="joined"))
check("J: several unread sends go on as ONE turn, built with all their ids as a list, in order",
      made == [{"text": "Words 0.\n\nWords 1.\n\nWords 2.", "client_id": ["id-1", "id-2"]}], made)
made = asyncio.run(_requeued_with([None, None], tag="none"))
check("J: ...and words sent without an id build the next turn exactly as before (no client_id)",
      made == [{"text": "Words 0.\n\nWords 1."}], made)
made = asyncio.run(_requeued_with(["id-9"], with_session=False, tag="nosess"))
check("J: a turn that ended before it had a session sends its unread words on with their id too "
      "(the registry's own pending_nudge_ids)", made == [{"text": "Words 0.", "client_id": "id-9"}], made)


async def _requeued_into_waiting(ids):
    """The thread's next turn is already waiting (a message came after the turn
    stopped reading): the unread words become a nudge into it. Returns the ids
    that nudge holds before that turn has a session, and in its session's queue
    after."""
    sid, sid2 = "requeue-wait-1", "requeue-wait-2"
    reg, rec, gate = await _registry_turn(sid, make_followup=lambda *a, **k: None)
    for i, cid in enumerate(ids):
        reg.start_or_attach(thread_key=rec.thread_key, entity_id=9003, app_id="workbench", message=f"Words {i}.",
                            make_inner=lambda: None, nudge_client_id=cid)
    S.release_holds(sid)                 # the turn's exit: it reads no more
    gate2 = asyncio.Event()

    async def waiting():
        yield "2:" + json.dumps([{"type": "session", "session_id": sid2, "turn_token": "tk2"}]) + "\n"
        await gate2.wait()
    nxt, mode = reg.start_or_attach(thread_key=rec.thread_key, entity_id=9003, app_id="workbench",
                                    message="The next message.", make_inner=waiting,
                                    make_followup=lambda *a, **k: None)
    S.end_turn_input(sid)
    gate.set()
    await asyncio.wait_for(rec.done.wait(), 5)
    before = list(nxt.pending_nudge_ids)
    for _ in range(100):
        if nxt.session_id == sid2:
            break
        await asyncio.sleep(0.01)
    queued = S.nudge_ids(sid2)
    gate2.set()
    await asyncio.wait_for(nxt.done.wait(), 5)
    S.consume_nudges(sid2)
    return mode, before, queued


mode, before, queued = asyncio.run(_requeued_into_waiting(["id-w1", "id-w2"]))
check("J: unread words that become a nudge into the thread's waiting next turn keep their ids, as a list, "
      "into that turn's queue (whose drain writes them on the nudge's row)",
      mode == "started" and before == [["id-w1", "id-w2"]] and queued == [["id-w1", "id-w2"]], (mode, before, queued))


async def _requeued_into_same(ids):
    """The thread's next turn already waits on the SAME words the unread words
    make (the same message came again after the turn stopped reading): they
    attach to it, before the nudge path. Their ids go on that turn, whose
    user row carries them. Returns what the registry asked the
    database to keep for the ids before that row (the lookups it made and the
    markers it marked: none), whether the requeue named the waiting turn and
    left it no nudge, and the ids the waiting turn carries."""
    from server import brain_service as BS
    from server.connectors import thread_pick as TP

    class IdPool(Pool):
        """Every lookup of a request id finds one marker; the writes are kept."""

        def __init__(self):
            self.looked, self.marked = [], []

        async def fetch(self, q, *a):
            if q != TP.INDEX_SEND_ROWS_SQL:
                return []
            self.looked.append(a[1])
            return [{"id": 700 + len(self.looked), "project_id": 251, "content": "marker",
                     "metadata": {"source_kind": TP.INDEX_PICK_MARKER_KIND, "index_send": {}}}]

        async def execute(self, q, *a):
            self.marked.append(a[0])
            return "OK"
    pool, real_pool = IdPool(), BS.get_pool
    BS.get_pool = lambda: pool
    sid, sid2 = "requeue-same-1", "requeue-same-2"
    try:
        reg, rec, gate = await _registry_turn(sid, make_followup=lambda *a, **k: None)
        texts = []
        for i, cid in enumerate(ids):
            texts.append(f"Same words {i}.")
            reg.start_or_attach(thread_key=rec.thread_key, entity_id=9003, app_id="workbench", message=texts[-1],
                                make_inner=lambda: None, **({"nudge_client_id": cid} if cid else {}))
        S.release_holds(sid)             # the turn's exit: it reads no more
        gate2 = asyncio.Event()

        async def waiting():
            yield "2:" + json.dumps([{"type": "session", "session_id": sid2, "turn_token": "tk2"}]) + "\n"
            await gate2.wait()
        nxt, _mode = reg.start_or_attach(thread_key=rec.thread_key, entity_id=9003, app_id="workbench",
                                         message="\n\n".join(texts), make_inner=waiting,
                                         make_followup=lambda *a, **k: None)
        S.end_turn_input(sid)
        gate.set()
        await asyncio.wait_for(rec.done.wait(), 5)
        await asyncio.sleep(0.05)        # the markers are written apart from the turn's teardown
        named = [(json.loads(f[2:]) or [{}])[0].get("turn_id") for f in rec.frames if "nudge_requeued" in f]
        attached = named == [nxt.turn_id] and not nxt.pending_nudges and not nxt.pending_nudge_ids
        gate2.set()
        await asyncio.wait_for(nxt.done.wait(), 5)
        S.consume_nudges(sid2)
        return attached, pool.looked, pool.marked, list(nxt.client_ids)
    finally:
        BS.get_pool = real_pool


attached, looked, marked, carried = asyncio.run(_requeued_into_same(["id-s1", "id-s2"]))
check("J: unread words that are the very words the thread's waiting next turn is on attach to that turn "
      "(the requeue names it) and are no nudge into it", attached, (attached, looked, marked))
check("J: ...their ids go on that turn, in order, for its user row to carry, and no marker is "
      "marked before that row exists",
      carried == ["id-s1", "id-s2"] and looked == [] and marked == [], (carried, looked, marked))
attached, looked, marked, carried = asyncio.run(_requeued_into_same([None, "id-s3", None]))
check("J: ...joined with words sent without an id, only the ids that were sent go on the turn",
      attached and carried == ["id-s3"] and looked == [] and marked == [], (attached, carried, looked, marked))
attached, looked, marked, carried = asyncio.run(_requeued_into_same([None]))
check("J: ...and words sent without an id attach as before: the database is not asked for anything",
      attached and carried == [] and looked == [] and marked == [], (attached, carried, looked, marked))


async def _followup_kw(cid):
    """What brain_service's follow-up builder asks _send_message_inner for."""
    import functools
    from server import brain_service as BS
    got: dict = {}
    real = BS._send_message_inner

    @functools.wraps(real)
    async def recording(*a, **k):
        got.update(k)
        yield "e:{}\n"
    BS._send_message_inner = recording
    try:
        mk = BS._followup_maker((), {"entity_id": 9003, "message": "First.", "app_id": "workbench",
                                     "project_id": 251, "client_id": "first-id"})
        async for _f in (mk("Words.", client_id=cid) if cid is not None else mk("Words.")):
            pass
    except TypeError as e:               # a tree whose builder takes no id: the check fails, the run goes on
        got["error"] = str(e)
    finally:
        BS._send_message_inner = real
    return got


got = asyncio.run(_followup_kw(["id-1", "id-2"]))
check("J: brain_service's follow-up builder hands the words' own ids to the turn, with the thread's place",
      got.get("client_id") == ["id-1", "id-2"] and got.get("message") == "Words." and got.get("project_id") == 251
      and got.get("app_id") == "workbench", got)
got = asyncio.run(_followup_kw(None))
check("J: ...and never the first message's id (none given, none passed)",
      "client_id" not in got and got.get("message") == "Words.", got)
ID2 = "fedcba9876543210fedcba9876543210"
rows = asyncio.run(_id_rows([ID, ID2]))
users = [r for r in rows if r[0] == "user"]
check("J: the real runtime keeps both ids, as a list, on the joined turn's user row",
      len(users) == 1 and users[0][2].get("client_id") == [ID, ID2], rows)
rows = asyncio.run(_id_rows([ID, "two words", ID]))
users = [r for r in rows if r[0] == "user"]
check("J: ...an id that is not 1-64 of A-Z a-z 0-9 _ - is left out of the list, and an id is kept once",
      len(users) == 1 and users[0][2].get("client_id") == ID, rows)


if FAILS:
    print(f"\nFAIL late_nudge_once: {len(FAILS)} check(s)")
    sys.exit(1)
print("\nPASS late_nudge_once")
