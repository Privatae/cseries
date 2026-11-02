#!/usr/bin/env python3
"""An outside agent's MCP sessions stay bounded in the Host's memory
(server/agent_access/sessions.py).

A client names its own session id on GET /mcp (claim takes one this process
does not know, so a restart never cuts an agent off). So the bound has to hold
for what it opens with them too:

  1. a session pushed out of SESSIONS_MAX ends its open stream, so open
     streams never outnumber SESSIONS_MAX;
  2. a stream opened for a session pushed out between its GET's claim and the
     stream's start ends at once and is not kept;
  3. one ask remembers at most ASK_SESSIONS_MAX sessions, the latest;
  4. the notification still reaches the session that was sent the ask.

No database, no network.

Run:  python3 scripts/test_mcp_sessions_bounded.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.agent_access import sessions as S  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"\n       {detail}"))
    if not ok:
        FAILS.append(name)


def reset():
    S._sessions.clear()
    S._asks.clear()
    S._streams.clear()
    S._pending.clear()


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


S.SESSIONS_MAX = 8
S.ASK_SESSIONS_MAX = 4

# 1. eviction ends the evicted session's stream
reset()
sids = [f"client-chosen-{i:04d}" for i in range(20)]
queues = {}
for sid in sids:
    assert S.claim(sid, 7)
    queues[sid] = S.open_stream(sid)
ended = [sid for sid in sids if None in drain(queues[sid])]
for sid in ended:
    S.close_stream(sid, queues[sid])
check("a session pushed out of the bound has its open stream ended",
      sorted(ended) == sorted(sids[:-S.SESSIONS_MAX]), ended)
check("so open streams never outnumber the bound",
      len(S._streams) <= S.SESSIONS_MAX, len(S._streams))

# 2. a stream for a session pushed out before it started is not kept
reset()
assert S.claim("late-stream-00000", 7)
for i in range(S.SESSIONS_MAX):
    S.claim(f"filler-session-{i:04d}", 8)
q = S.open_stream("late-stream-00000")
check("a stream for a session no longer held ends at once", drain(q) == [None])
S.close_stream("late-stream-00000", q)
check("...and is not kept", "late-stream-00000" not in S._streams)

# 3. one ask remembers a bounded number of sessions
reset()
for i in range(50):
    sid = f"ask-session-{i:04d}"
    S.claim(sid, 7)
    S.asked("ask-1", sid)
check("one ask remembers at most ASK_SESSIONS_MAX sessions",
      len(S._asks["ask-1"]) == S.ASK_SESSIONS_MAX, len(S._asks["ask-1"]))
check("...the latest ones", list(S._asks["ask-1"])[-1] == "ask-session-0049")

# 4. the notification still goes to the session that was sent the ask, only
reset()
S.SESSIONS_MAX = 1000
a, b = S.new_session(7), S.new_session(9)
qa, qb = S.open_stream(a), S.open_stream(b)
S.asked("ask-2", a)
check("the answer is told to one session", S.answered("ask-2") == 1)
check("...the one that was sent the ask", drain(qa) == ["ask-2"] and drain(qb) == [])

print()
if FAILS:
    print(f"FAILED: {len(FAILS)}")
    sys.exit(1)
print("OK: MCP sessions stay bounded")
