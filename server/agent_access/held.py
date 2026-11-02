"""An outside agent's tool call held open while the person answers its ask.

MCP 2025-11-25, client/elicitation, URL mode: a server that needs the person
to do something on a page sends `elicitation/create` {mode: "url", url,
elicitationId, message} inside the tool call, on the call's own stream
(basic/transports, Streamable HTTP: the POST is answered with an event stream
that carries the server's requests related to it, then the response). The
client asks the person whether to open the page and answers accept, decline
or cancel; accept means the person went to the page, not that they answered
there. Once they have, the server sends `notifications/elicitation/complete`
and the call goes on.

Here: a call that needs the person's approval (agent_access.tools
NeedsApproval) from a client that declared URL elicitation is held. The
client is sent the approval page; on accept the call waits for the person's
answer (answered(), from the ask page or the machine's card, asks.py), then
runs again and its result is the call's response. Claude Code 2.1.312 keeps
a "waiting" dialog open from the accept to the completion, and only then
does the agent go on, without anyone pressing Retry. A decline, a cancel, no
answer within HOLD_S, or the Host stopping ends the call as before: nothing
ran, and the result carries the link and says to call again.

Only asks this Host learns the answer to are held: its own grant asks and a
machine's card. A machine that waits on the person's approval device or its
console tells nobody when it is answered, so that ask still ends the call at
once with its link (-32042).

Bounded so no client can hold the Host's memory: HELD_PER_CONNECTION_MAX
calls per connected agent and HELD_MAX in all. A call past either bound is
answered the old way (-32042 and the link), never refused. In this process's
memory, as sessions.py is: a restart ends every held call, and the agent's
next call finds what the person answered.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections import OrderedDict
from typing import Any, Awaitable, Callable

log = logging.getLogger("cseries.agent_access")

# How long a held call waits for the person: their answer to the client's
# prompt, then to the page. Fifteen minutes, the lifetime the OAuth device
# flow gives a person to approve on another screen (RFC 8628 §3.2's example
# expires_in is 1800 s; GitHub's and Google's device codes live 900 s). The
# ask itself stays open for a day (store.ASK_TTL_S), so a later answer still
# counts for the agent's next call.
HOLD_S = 900.0
# A progress notification this often while held: Claude Code ends an HTTP
# tool call that sends nothing for 5 minutes (its tool idle timeout, reset by
# progress), and the proxies on the way cut a silent stream at 60 to 100 s.
PROGRESS_S = 25.0
HELD_PER_CONNECTION_MAX = 16
HELD_MAX = 256
# One call can need more than one answer: the person allows it here, then the
# machine holds it on a card of its own. Past this many, the call ends with
# the link, as before.
ROUNDS_MAX = 3

_held: dict[int, int] = {}                                      # connection -> held calls
_requests: dict[tuple[str, str], asyncio.Future] = {}            # (session, request id) -> the client's answer
_waiting: dict[str, set[asyncio.Event]] = {}                     # ask id -> held calls waiting on it
# Asks answered lately, newest last. An answer can come before its held call
# waits on it: a machine's card shows on the Host as the machine makes it, and
# the stream starts after that. A call finds its ask here and goes on at once;
# its rerun reads where the ask stands, so an entry never lets anything run.
_answered: "OrderedDict[str, None]" = OrderedDict()
ANSWERED_MAX = 2000
# The Host is stopping. A flag and one event per wait, never one module-level
# asyncio.Event: an Event binds to the first loop that waits on it, and every
# wait from another loop (a second server in the process, a test's next
# asyncio.run) fails at once, which _until read as a stop.
_stopped = False
_stops: set[asyncio.Event] = set()


def held_count() -> int:
    return sum(_held.values())


def can_hold(connection_id: int) -> bool:
    if _stopped:
        return False
    return held_count() < HELD_MAX and _held.get(int(connection_id), 0) < HELD_PER_CONNECTION_MAX


def _take(connection_id: int) -> None:
    _held[connection_id] = _held.get(connection_id, 0) + 1


def _give(connection_id: int) -> None:
    left = _held.get(connection_id, 0) - 1
    if left > 0:
        _held[connection_id] = left
    else:
        _held.pop(connection_id, None)


def respond(sid: str, message: dict) -> bool:
    """A JSON-RPC response the client posted in session `sid`: the answer to
    an elicitation/create this Host sent it. True when a held call took it."""
    rid = message.get("id")
    fut = _requests.get((sid, rid)) if isinstance(rid, str) else None
    if fut is None or fut.done():
        return False
    fut.set_result(message)
    return True


def answered(ask_id: str) -> int:
    """The person answered `ask_id` (asks.py, through sessions.answered):
    every call held on it goes on. Returns how many."""
    _answered.pop(ask_id, None)
    _answered[ask_id] = None
    while len(_answered) > ANSWERED_MAX:
        _answered.popitem(last=False)
    events = _waiting.get(ask_id) or set()
    for ev in events:
        ev.set()
    return len(events)


def end_all() -> None:
    """The server is stopping (launch.HostServer, through sessions.end_all):
    every held call ends now with "nothing ran", as a stream does."""
    global _stopped
    _stopped = True
    for ev in list(_stops):
        ev.set()


def _sse(message: dict) -> str:
    return "event: message\ndata: " + json.dumps(message) + "\n\n"


def _not_run(need, why: str) -> dict:
    """The call's result when it ends before the person's answer: what the
    unheld call answers a client without URL elicitation, behind why."""
    text = f"{why} {need.message}" if why else need.message
    return {"content": [{"type": "text", "text": text}], "isError": True,
            "structuredContent": {"state": "needs_approval", "url": need.url, "ask": need.ask_id}}


async def _until(waits: list[Awaitable], deadline: float):
    """Wait for the first of `waits` (or the Host stopping) until `deadline`,
    yielding None each PROGRESS_S so the caller can say it is still there.
    Ends by yielding the index of the one that finished, -1 for a stop, or
    -2 when the deadline passed."""
    loop = asyncio.get_running_loop()
    tasks = [asyncio.ensure_future(w) for w in waits]
    stopping = asyncio.Event()
    if _stopped:
        stopping.set()
    _stops.add(stopping)
    stop = asyncio.ensure_future(stopping.wait())
    try:
        while True:
            left = deadline - loop.time()
            if left <= 0:
                yield -2
                return
            done, _ = await asyncio.wait([*tasks, stop], timeout=min(PROGRESS_S, left),
                                         return_when=asyncio.FIRST_COMPLETED)
            if stop in done:
                yield -1
                return
            for i, t in enumerate(tasks):
                if t in done:
                    yield i
                    return
            yield None
    finally:
        _stops.discard(stopping)
        for t in (*tasks, stop):
            if not t.done():
                t.cancel()


async def run(*, sid: str, rid: Any, connection_id: int, need, progress_token,
              rerun: Callable[[], Awaitable[dict]], holdable: Callable[[Any], bool],
              ok: Callable[[Any, dict], dict], told_later: Callable[[str, str], None],
              unheld: Callable[[Any], dict]):
    """The held call's event stream: elicitation/create for `need`, the
    person's answer, then the call's response. `rerun()` runs the call again
    (it may raise NeedsApproval for a further ask); `holdable(need)` says
    whether such an ask is held as well; `ok(rid, result)` makes the
    response; `told_later(ask_id, sid)` makes a later answer reach the
    session's GET stream (sessions.asked), for a call that ended unanswered
    while the client still waits on the page. `unheld(need)` is the
    response the call gets when it is not held (-32042 and the link)."""
    from server.agent_access.tools import NeedsApproval
    loop = asyncio.get_running_loop()
    deadline = loop.time() + HOLD_S
    beat = 0
    # The bounds are checked again here, with nothing awaited between the
    # check and the take: routes/mcp.py checked them before this stream
    # started, and concurrent calls of one client can all pass that check
    # before any of them is taken here.
    if not can_hold(connection_id):
        yield _sse(unheld(need))
        return
    _take(connection_id)

    def progress() -> str:
        nonlocal beat
        beat += 1
        if progress_token is None:
            return ": waiting for the person\n\n"
        return _sse({"jsonrpc": "2.0", "method": "notifications/progress",
                     "params": {"progressToken": progress_token, "progress": beat,
                                "message": "Waiting for the person's answer on their Host"}})

    try:
        for _round in range(ROUNDS_MAX):
            ask_id = need.ask_id
            answer_ev = asyncio.Event()
            _waiting.setdefault(ask_id, set()).add(answer_ev)
            if ask_id in _answered:
                answer_ev.set()
            req_id = "host-elicit-" + secrets.token_urlsafe(12)
            reply: asyncio.Future = loop.create_future()
            _requests[(sid, req_id)] = reply
            try:
                yield _sse({"jsonrpc": "2.0", "id": req_id, "method": "elicitation/create",
                            "params": {"mode": "url", "message": need.person_words or need.message,
                                       "url": need.url, "elicitationId": ask_id}})
                outcome = None
                async for got in _until([reply], deadline):
                    if got is None:
                        yield progress()
                    else:
                        outcome = got
                if outcome != 0:
                    yield _sse(ok(rid, _not_run(need, _ended_words(outcome))))
                    return
                msg = reply.result()
                action = ((msg.get("result") or {}).get("action")
                          if isinstance(msg.get("result"), dict) else None)
                if action != "accept":
                    yield _sse(ok(rid, _not_run(
                        need, "The person turned down opening the approval page."
                        if action == "decline" else "The approval prompt was closed.")))
                    return
                outcome = 0 if answer_ev.is_set() else None
                if outcome is None:
                    async for got in _until([answer_ev.wait()], deadline):
                        if got is None:
                            yield progress()
                        else:
                            outcome = got
                if outcome != 0:
                    # Unanswered: the client may still show the page as
                    # waiting, and an answer after this reaches it on its
                    # GET stream, as for a -32042.
                    told_later(ask_id, sid)
                    yield _sse(ok(rid, _not_run(need, _ended_words(outcome))))
                    return
            finally:
                _requests.pop((sid, req_id), None)
                evs = _waiting.get(ask_id)
                if evs is not None:
                    evs.discard(answer_ev)
                    if not evs:
                        _waiting.pop(ask_id, None)
            yield _sse({"jsonrpc": "2.0", "method": "notifications/elicitation/complete",
                        "params": {"elicitationId": ask_id}})
            try:
                result = await rerun()
            except NeedsApproval as again:
                if again.ask_id == ask_id or not holdable(again) or _round + 1 >= ROUNDS_MAX:
                    yield _sse(ok(rid, _not_run(again, "")))
                    return
                need = again
                continue
            yield _sse(ok(rid, result))
            return
    finally:
        _give(connection_id)


def _ended_words(outcome) -> str:
    if outcome == -1:
        return "This Host is restarting."
    if HOLD_S >= 120:
        return f"The person had not answered after {int(HOLD_S // 60)} minutes."
    return f"The person had not answered after {int(HOLD_S)} seconds."
