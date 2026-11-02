"""An outside agent's MCP sessions on this Host, and the one message the Host
sends an agent without being asked: that the person answered its ask.

MCP 2025-11-25, client/elicitation ("Completion Notifications for URL Mode
Elicitation"): a server MAY send `notifications/elicitation/complete` with the
`elicitationId` once the interaction a URL elicitation started is done, and
MUST send it only to the client that started it. A client waiting on a
-32042 (agent_access.tools.NeedsApproval) then retries the call itself
instead of waiting for someone to press Retry.

basic/transports (Streamable HTTP): a message that answers no request in
flight goes on the stream the client opens with GET /mcp; the session id the
server gives on initialize (MCP-Session-Id) says which client a stream is.
So: initialize gives an agent a session id, a -32042 sent in that session
records the ask against it, the ask page's answer (asks.py) calls answered(),
and the session's open stream carries the notification. A session with no
stream open at that moment keeps it until its next one opens (the client
reopens a stream the Host closes, routes/mcp.py STREAM_LIFE_S).

In this process's memory: the server is one uvicorn process (server/launch.py)
and the agent's /mcp and the person's ask page are routes of the same app. A
restart forgets every session; the agent's next request names its session
again and it is taken back for the same connection (claim), so a restart
never cuts an agent off. What is lost is a completion for an ask answered
before the restart, and the client still has its own Retry for that.
Nothing here decides what an agent may do: every request is still
authenticated by its token, and the session only routes the notification.
"""
from __future__ import annotations

import asyncio
import re
import secrets
from collections import OrderedDict

# Bounded so no client can grow them without end. A session pushed out of
# SESSIONS is taken back on its next request (claim), and its open stream is
# ended (the client opens the next one), so open streams never outnumber
# SESSIONS_MAX either; an ask pushed out of ASKS only loses its notification,
# and an ask remembers the last ASK_SESSIONS_MAX sessions it was sent in.
SESSIONS_MAX = 1000
ASKS_MAX = 2000
# The sessions one ask was sent in, newest kept: an agent calling the same
# tool again gets the same ask (store.file_ask), and a client that does so
# from ever-new session ids must not grow it without end.
ASK_SESSIONS_MAX = 16
PENDING_MAX = 32

SESSION_ID = re.compile(r"[A-Za-z0-9_\-]{16,64}")

_sessions: "OrderedDict[str, int]" = OrderedDict()      # session -> connection id
_asks: "OrderedDict[str, dict[str, None]]" = OrderedDict()  # ask id -> sessions it was sent in (ordered)
_streams: dict[str, asyncio.Queue] = {}                 # session -> its one open stream
_pending: dict[str, list[str]] = {}                     # session -> answered, not yet sent


def complete_message(ask_id: str) -> dict:
    return {"jsonrpc": "2.0", "method": "notifications/elicitation/complete",
            "params": {"elicitationId": ask_id}}


def _remember(sid: str, connection_id: int) -> None:
    _sessions[sid] = int(connection_id)
    _sessions.move_to_end(sid)
    while len(_sessions) > SESSIONS_MAX:
        old, _ = _sessions.popitem(last=False)
        _pending.pop(old, None)
        q = _streams.get(old)
        if q is not None:
            q.put_nowait(None)      # the stream ends; its client opens the next one


def new_session(connection_id: int) -> str:
    sid = secrets.token_urlsafe(24)
    _remember(sid, connection_id)
    return sid


def claim(sid: str, connection_id: int) -> bool:
    """Whether `sid` may be this connection's session: its own, or one this
    process does not know (a session from before a restart, or pushed out of
    the bound), which is then taken as this connection's. False for a session
    of another connection, or a value that was never a session id."""
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        return False
    have = _sessions.get(sid)
    if have is not None and have != int(connection_id):
        return False
    _remember(sid, connection_id)
    return True


def asked(ask_id: str, sid: str) -> None:
    """A -32042 for `ask_id` went out in session `sid`."""
    if not sid or not ask_id:
        return
    sent_in = _asks.setdefault(ask_id, {})
    sent_in.pop(sid, None)
    sent_in[sid] = None
    while len(sent_in) > ASK_SESSIONS_MAX:
        del sent_in[next(iter(sent_in))]
    _asks.move_to_end(ask_id)
    while len(_asks) > ASKS_MAX:
        _asks.popitem(last=False)


def answered(ask_id: str) -> int:
    """The person answered `ask_id`: each session that was sent it is told,
    on its open stream or on the next one it opens. Returns how many."""
    told = 0
    for sid in _asks.pop(ask_id, {}):
        q = _streams.get(sid)
        if q is not None:
            q.put_nowait(ask_id)
        elif sid in _sessions:
            waiting = _pending.setdefault(sid, [])
            if ask_id not in waiting:
                waiting.append(ask_id)
                del waiting[:-PENDING_MAX]
        else:
            continue
        told += 1
    return told


def open_stream(sid: str) -> asyncio.Queue:
    """The session's stream, replacing any it had open (each message goes on
    one stream only, MCP transports "Multiple Connections"). What was answered
    while it had none is queued first."""
    old = _streams.get(sid)
    if old is not None:
        old.put_nowait(None)
    q: asyncio.Queue = asyncio.Queue()
    if sid not in _sessions:
        # Pushed out of SESSIONS between the GET's claim and now: no stream
        # is kept for it, and the client's next GET claims it again.
        q.put_nowait(None)
        return q
    for ask_id in _pending.pop(sid, []):
        q.put_nowait(ask_id)
    _streams[sid] = q
    return q


def end_all() -> None:
    """The server is stopping (launch.HostServer): every open stream ends
    now. A server's graceful stop waits for open responses before its own
    shutdown runs, and an agent's stream would hold it for up to
    STREAM_LIFE_S. The client reopens it on the server that comes back."""
    for q in list(_streams.values()):
        q.put_nowait(None)


def close_stream(sid: str, q: asyncio.Queue) -> None:
    """The stream ended. Anything it had not sent goes on the stream that
    replaced it, or waits for the next one."""
    if _streams.get(sid) is q:
        del _streams[sid]
    left = []
    while not q.empty():
        item = q.get_nowait()
        if item is not None:
            left.append(item)
    newer = _streams.get(sid)
    if left and newer is not None:
        for a in left:
            newer.put_nowait(a)
    elif left and sid in _sessions:
        waiting = _pending.setdefault(sid, [])
        waiting.extend(a for a in left if a not in waiting)
        del waiting[:-PENDING_MAX]
