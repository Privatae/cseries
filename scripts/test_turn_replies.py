#!/usr/bin/env python3
"""The turn's reply recorder (runtime/turn_replies.py, TurnReplies), driven on
its own: no pod, no model, no database.

  R1  two model rounds stream as they arrive; prose beside tool calls that go
      on to another round is narration, the closing text is the reply, and the
      stored reply is the pieces joined with REPLY_SEP, which the stream also
      sent before the second piece;
  R2  a completion that opens with a parroted Working Memory block streams
      nothing of it; what the strip leaves is sent when it settles;
  R3  a reasoning block that opens a completion is never sent or stored;
  R4  a platform notice is a piece of its own, never judged as the Host's
      words, and the turn's reply is not "written" by it;
  R5  empty text sends nothing;
  R6  the values the turn rebinds are read when needed: with a provider that
      does not stream, each round sends the completion the turn holds when
      that round settles, and close_turn's owed reply is the one the turn
      holds then.

Run: APP_ROOT=<repo> python3 scripts/test_turn_replies.py
"""
import asyncio
import os
import sys
import types

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

from mastercode.runtime.turn_replies import TurnReplies  # noqa: E402
from mastercode.runtime.turn_trace import REPLY_SEP  # noqa: E402

FAILS = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{str(detail)[:300]}]"))
    if not cond:
        FAILS.append(label)


def recorder(latest, owed=None):
    """A recorder for a turn with no live trace (empty token), whose latest
    completion is latest[0] and whose close_turn owed reply is owed[0]."""
    events = []
    owed = owed if owed is not None else [None]

    async def progress_cb(ev):
        events.append(ev)

    rec = TurnReplies(progress_cb=progress_cb, sess=types.SimpleNamespace(session_id="s-test", unseen_lane=False),
                      turn_token="", result=lambda: latest[0], ct_reply_result=lambda: owed[0],
                      row_project_id=lambda: None, turn_tags=lambda: {}, asked_in_box=lambda: False,
                      tools=lambda: [])
    return rec, events


def sent(events):
    return [e["text"] for e in events if e.get("event") == "text_delta"]


async def main():
    # R1
    latest = [{"content": "Looking at the logs now.", "tool_calls": [{"id": "c1"}]}]
    rec, events = recorder(latest)
    cb = await rec._next_stream_cb()
    await cb("Looking at the ")
    await cb("logs now.")
    check("R1: the first round streams as it arrives", sent(events) == ["Looking at the ", "logs now."], sent(events))
    cb = await rec._next_stream_cb()
    latest[0] = {"content": "All three checks pass."}
    await cb("All three checks pass.")
    await rec._settle()
    check("R1: the second piece goes out after the separator",
          sent(events)[2:] == [REPLY_SEP + "All three checks pass."], sent(events))
    check("R1: prose beside tool calls that went on is narration; the closing text is the reply",
          [p["label"] for p in rec._pieces] == ["narration", "reply"], [p["label"] for p in rec._pieces])
    check("R1: the stored reply is what the stream sent",
          rec._reply_all() == "".join(sent(events)) == "Looking at the logs now." + REPLY_SEP + "All three checks pass.",
          rec._reply_all())
    check("R4: a settled reply of the Host's own words is written", rec._reply_written())

    # R6: nothing streams, so each round's text comes from the completion the
    # turn holds when the round settles. The turn has none yet when the
    # recorder is made, and a new one each round.
    latest = [None]
    rec, events = recorder(latest)
    await rec._next_stream_cb()
    latest[0] = {"content": "Reading the config.", "tool_calls": [{"id": "c1"}]}
    await rec._next_stream_cb()
    latest[0] = {"content": "The port is 8080."}
    await rec._settle()
    check("R6: a provider that does not stream: each round sends the completion the turn holds as it settles",
          sent(events) == ["Reading the config.", REPLY_SEP + "The port is 8080."]
          and [p["label"] for p in rec._pieces] == ["narration", "reply"],
          (sent(events), [p["label"] for p in rec._pieces]))
    # close_turn's owed reply is set mid-turn, after a round that settled
    # with none owed: that round's prose is narration, the owed reply's
    # prose beside tool calls is the reply.
    latest, owed = [None], [None]
    rec, events = recorder(latest, owed)
    await rec._next_stream_cb()
    latest[0] = {"content": "Checking the tests.", "tool_calls": [{"id": "c2"}]}
    await rec._next_stream_cb()
    latest[0] = owed[0] = {"content": "Everything is in place.", "tool_calls": [{"id": "c3"}]}
    await rec._next_stream_cb()
    check("R6: close_turn's owed reply is the one the turn holds when its round settles",
          [p["label"] for p in rec._pieces][:2] == ["narration", "reply"], [p["label"] for p in rec._pieces])

    # R2
    parrot = "## Working Memory\n[HEAT: 3]\nan injected block\n\nThe answer the person asked for is this."
    latest = [{"content": parrot}]
    rec, events = recorder(latest)
    cb = await rec._next_stream_cb()
    await cb("## Working")
    await cb(parrot[len("## Working"):])
    check("R2: nothing of a parroted Working Memory block streams", sent(events) == [], sent(events))
    await rec._settle()
    check("R2: what the strip leaves is sent when the completion settles",
          sent(events) == ["The answer the person asked for is this."] and "Working Memory" not in rec._reply_all(),
          (sent(events), rec._reply_all()))

    # R3
    latest = [{"content": "<think>weighing two options</think>Done."}]
    rec, events = recorder(latest)
    cb = await rec._next_stream_cb()
    await cb("<think>weighing ")
    await cb("two options</think>Done.")
    await rec._settle()
    check("R3: a reasoning block that opens the completion is never sent or stored",
          "".join(sent(events)) == "Done." and rec._reply_all() == "Done.", (sent(events), rec._reply_all()))

    # R4, R5
    latest = [{"content": ""}]
    rec, events = recorder(latest)
    await rec._send_piece("Still working on it.", "reply", notice="stall")
    p = rec._pieces[-1]
    check("R4: a platform notice is its own piece, marked as the platform's",
          p["judged"] == "platform" and p.get("platform_notice") == "stall" and sent(events) == ["Still working on it."],
          (p, sent(events)))
    check("R4: the guard never reads it as the Host's words", rec._unjudged() == [])
    check("R4: and the Host's reply is not written by it", not rec._reply_written())
    before = list(events)
    await rec._send(rec._piece(sent=True), "")
    check("R5: empty text sends nothing", events == before)


asyncio.run(main())
if FAILS:
    print(f"test_turn_replies: {len(FAILS)} FAILED")
    sys.exit(1)
print("test_turn_replies: all checks passed")
