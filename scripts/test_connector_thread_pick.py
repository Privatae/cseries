#!/usr/bin/env python3
"""Gate: which thread a message belongs to is placed in software, and chosen
by the Host in its own turn when that is unclear
(server/connectors/thread_pick.py, brain/mastercode/runtime/thread_choice.py).

  N. no model call places a message: thread_pick calls no model, and the
     separate pick call and its prompts are gone;
  P. place_connector: a clear score-based match joins its thread; otherwise
     the message lands in the chat's last open thread, else in a new thread
     made for it, and the other open threads go to the turn as candidates
     (scored ones first, never the one it landed in);
  F. the frame's thread choice is read only when well formed, from the
     platform's own routes; render_block lists each candidate by its id;
  T. pick_thread in the turn: the first valid call decides; a thread not in
     the list, both a thread and new, neither, a new thread with no title are
     refused and change nothing; a pick of a listed thread moves the rest of
     the turn's writes there (the turn's scope) and hands back that thread's
     state; a new-thread pick on a thread made for this message only titles
     it; from the Workbench Index (a page already) a new thread is refused;
  M. the markers say what happened.

Pure: the pool is never reached (the scoring, listing and landing helpers are
replaced; the SQL half is scripts/test_connector_thread_pick_pg.py).
"""
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from server.connectors import thread_pick as tp  # noqa: E402
from mastercode.runtime import thread_choice as tc  # noqa: E402

FAILED = 0


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


A = "6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"
B = "7a2d3b9f-4c5e-4f60-9bac-1d2e3f4a5b6c"
NEWT = "8b3e4c0a-5d6f-4071-8cbd-2e3f4a5b6c7d"

# ── N: no model call ─────────────────────────────────────────────────────────
src = (ROOT / "server/connectors/thread_pick.py").read_text()
check("N thread_pick calls no model", "call_model(" not in src and "_call_entity_model" not in src)
check("N the separate pick call and its prompts are gone",
      not any(hasattr(tp, n) for n in ("_ask_host", "choose_thread", "choose_page", "choose_topic",
                                       "build_messages", "build_index_messages", "build_topic_messages",
                                       "read_pick", "PICK_TOOL", "INDEX_PICK_TOOL", "TOPIC_PICK_TOOL")))

# ── P: place_connector ───────────────────────────────────────────────────────
SCOPES = [("thread", A, "Q3 plan", None, None, None, None),
          ("thread", B, "Garden", None, None, None, None),
          ("project", "12", "Project “Website”", None, None, None, None)]
calls: dict = {"land": [], "fit": []}


async def _scopes(pool, eid):
    return list(SCOPES)


async def _scores_none(pool, eid, message, pages=None):
    return []


async def _land(pool, eid, pick, *, platform):
    calls["land"].append(pick)
    return tp.Landing(kind="thread", ref=NEWT, title=pick.title, new=True)


async def _fit(pool, eid, scopes, *, budget_tokens, current, current_note):
    calls["fit"].append([f"{s[0]}:{s[1]}" for s in scopes])
    return [tp.Candidate(s[0], s[1], s[2], None, None, "u", "h") for s in scopes]


tp._scopes, tp.land_pick, tp._fit = _scopes, _land, _fit
tp.topic_scores, tp.page_scores = _scores_none, _scores_none


async def _place(message, current):
    return await tp.place_connector(None, 1, message=message, platform="telegram", current=current)

pl = asyncio.run(_place("Book the hotel", ("thread", B)))
check("P no clear match: it lands in the chat's last open thread, no new thread",
      pl.kind == "thread" and pl.ref == B and not pl.new and not calls["land"], pl)
check("P ...and the other open threads go to the turn, never the one it landed in",
      [c["ref"] for c in pl.choice] == [A, "12"] and not pl.provisional, pl.choice)

calls["land"].clear()
pl = asyncio.run(_place("First words here\nmore", None))
check("P a chat with no last thread: a new thread made for it, titled from the first line",
      pl.ref == NEWT and pl.new and pl.provisional and calls["land"]
      and calls["land"][0].title == "First words here", (pl, calls["land"]))
check("P ...with every open thread as a candidate", [c["ref"] for c in pl.choice] == [A, B, "12"],
      pl.choice)


async def _scores_clear(pool, eid, message, pages=None):
    return [(f"thread:{A}", 1.0)] if pages is None and "Q3" in message else []
tp.topic_scores = _scores_clear
calls["land"].clear()
pl = asyncio.run(_place("the Q3 plan draft", ("thread", B)))
check("P a clear match joins its thread, nothing offered", pl.kind == "thread" and pl.ref == A
      and not pl.choice and not calls["land"], pl)


async def _topic_close(pool, eid, message, pages=None):
    return [(f"thread:{B}", 0.65)]


async def _page_close(pool, eid, message, pages=None):
    return [("project:12", 0.7)]
tp.topic_scores, tp.page_scores = _topic_close, _page_close
pl = asyncio.run(_place("garden website", ("thread", A)))
check("P close scores (0.7 and 0.65) are no clear match: offered, the best first",
      not pl.new and pl.ref == A and [c["ref"] for c in pl.choice][:2] == ["12", B], pl)


async def _page_clear(pool, eid, message, pages=None):
    return [("project:12", 0.9)]
tp.page_scores = _page_clear
pl = asyncio.run(_place("garden website", ("thread", A)))
check("P a page that clearly leads (0.9 over 0.65) is joined, nothing offered",
      pl.kind == "project" and pl.ref == "12" and not pl.choice, pl)

# ── F: the frame ─────────────────────────────────────────────────────────────
fv = tc.frame_value(surface="connector", platform="telegram",
                    landed={"kind": "thread", "ref": B, "title": "Garden", "provisional": False},
                    candidates=[{"kind": "thread", "ref": A, "title": "Q3 plan", "last_user": "u"},
                                {"kind": "app", "ref": "circles", "title": "old"}])
ch = tc.from_frame({"thread_choice": fv})
check("F a well-formed choice is read; only threads and projects are candidates",
      ch is not None and ch.keys() == {f"thread:{A}"}, ch)
check("F a choice from no known surface is not read",
      tc.from_frame({"thread_choice": {**fv, "surface": "page"}}) is None)
check("F a frame with none offers none", tc.from_frame({"view": "x"}) is None and tc.from_frame(None) is None)
blk = tc.render_block(ch)
check("F the block names where it landed and each candidate by its id",
      "landed in 'Garden'" in blk and f"[thread:{A}] Q3 plan" in blk and "pick_thread" in blk, blk)
check("F the tool is pick_thread, its reason required",
      tc.TOOL_SCHEMA["function"]["name"] == "pick_thread"
      and tc.TOOL_SCHEMA["function"]["parameters"]["required"] == ["reason"])


# ── T: the pick in the turn ──────────────────────────────────────────────────
class _Sess:
    entity_id = 1
    session_id = "s-pick"
    thread_id = B
    recall_scope = {"kind": "thread", "ref": B}


class _Pool:
    async def fetchval(self, sql, *a):
        if "FROM circles_threads" in sql and "deleted_at IS NULL" in sql:
            return 1 if a[0] in (A, B) else None
        if "COALESCE(NULLIF(title" in sql:
            return {A: "Q3 plan", B: "Garden"}.get(a[0], "")
        return None


async def _ctx(pool, eid, sid, scope, query):
    return {"where_it_stands": f"state of {scope['ref']}"}
tc._context_of = _ctx


def _run(args, choice):
    return asyncio.run(tc.pick(_Pool(), _Sess(), args, choice, user_message="m"))

for label, args in (("a thread not in the list", {"thread": "thread:00000000-0000-4000-8000-000000000000",
                                                   "reason": "x"}),
                    ("both a thread and new", {"thread": f"thread:{A}", "new": True, "reason": "x"}),
                    ("neither", {"reason": "x"}),
                    ("a new thread with no title", {"new": True, "reason": "x"})):
    c = tc.from_frame({"thread_choice": fv})
    r = _run(args, c)
    check(f"T {label}: refused, nothing changes", r.get("status") == "error" and c.picked is None
          and not c.answered, r)

c = tc.from_frame({"thread_choice": fv})
from mastercode import memory_engine as ME  # noqa: E402
_seen_scope: dict = {}
_orig_ctx = tc._context_of


async def _ctx_seen(pool, eid, sid, scope, query):
    # The pick's own task context: the turn's scope as the rest of the turn reads it.
    _seen_scope["now"] = ME.CURRENT_TURN_SCOPE.get()
    return await _orig_ctx(pool, eid, sid, scope, query)
tc._context_of = _ctx_seen
r = _run({"thread": f"thread:{A}", "reason": "it is the Q3 plan"}, c)
check("T a listed thread: picked, with that thread's state handed back",
      r.get("status") == "ok" and c.picked == {"kind": "thread", "ref": A, "title": "Q3 plan", "new": False,
                                               "reason": "it is the Q3 plan"}
      and (r.get("result") or {}).get("where_it_stands") == f"state of {A}", (r, c.picked))
check("T ...and the rest of the turn's writes land there (the turn's scope moved)",
      _seen_scope.get("now") == {"session_id": "s-pick", "scope": {"kind": "thread", "ref": A}},
      _seen_scope)
r2 = _run({"thread": f"thread:{B}", "reason": "changed my mind"}, c)
check("T the first valid call decides; a second changes nothing",
      c.picked["ref"] == A and "already picked" in str(r2.get("result")), r2)

titled: list = []


async def _set_topic(pool, eid, thread, topic, title=None):
    titled.append((thread, topic, title))
    return True
import mastercode.circles_threads as CT  # noqa: E402
CT.set_thread_topic = _set_topic
pv = dict(fv, landed={"kind": "thread", "ref": NEWT, "title": "a new topic", "provisional": True})
c = tc.from_frame({"thread_choice": pv})
r = _run({"new": True, "title": "Garden lighting", "reason": "its own subject"}, c)
check("T new=true on a thread made for this message: it keeps it and only titles it",
      r.get("status") == "ok" and c.picked is None and titled == [(NEWT, None, "Garden lighting")], (r, titled))
c = tc.from_frame({"thread_choice": fv})
r = _run({"new": True, "title": "Garden lighting", "reason": "its own subject"}, c)
check("T new=true from a thread with its own history: a new thread is made when the turn ends",
      r.get("status") == "ok" and c.picked == {"kind": "thread", "ref": None, "title": "Garden lighting",
                                               "new": True, "reason": "its own subject"}, c.picked)

ix = tc.from_frame({"thread_choice": tc.frame_value(
    surface="index", landed={"kind": "project", "ref": "12", "title": "Website", "provisional": False},
    candidates=[{"kind": "project", "ref": "13", "title": "Shop"}])})
r = _run({"new": True, "title": "Garden lighting", "reason": "its own subject"}, ix)
check("T from the Workbench Index a new thread is refused: the message is on a page already",
      r.get("status") == "error" and ix.picked is None and not ix.answered, r)
blk = tc.render_block(ix)
check("T ...and the Index's block offers pages only, never a thread of its own",
      "[project:13] Shop" in blk and "new=true" not in blk and "stays on this page" in blk, blk)

# ── M: the markers ───────────────────────────────────────────────────────────
check("M the placement line for a new thread",
      tp.marker_text("telegram", tp.Pick(ok=True, kind="thread", new=True, title="T"))
      == "Telegram message, started this thread.")
check("M a new topic with no title yet says so", tp.topic_marker_text(new=True, title="") == "new topic")
check("M the move's marker names the surface", tc.SURFACE_WORDS["connector"] == "Messaging app"
      and tc._marker_kind("connector") == tp.PICK_MARKER_KIND)

print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else 0)
