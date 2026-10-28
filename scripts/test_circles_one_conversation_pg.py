#!/usr/bin/env python3
"""Gate: Circles is one conversation (server/connectors/thread_pick.py "Circles: one conversation",
routes/circles.py feed/topic/send, circles-web's Host back).

  A. a blank login: the Host's back opens on a greeting, never on the page
     last open, and shows no thread tabs and no "new thread" button;
  B. two messages on different topics run at the same time (one running turn
     per topic), and words for the topic already running go into that turn;
  C. a message about a topic last discussed 14 days ago, behind more than a
     dozen newer topics, goes to that topic, and that topic's history is the
     history the turn loads;
  D. a message sent to the root thread never enters topic placement, and the
     root thread is never offered to the Host's pick;
  N. conservative placement, with no model call: a near miss (same broad
     area, another subject) starts a new topic and loads none of the other
     topic's turns; a clear continuation joins; a short follow-on reply stays
     on the topic in use; anything unclear starts a topic of its own and
     carries the topics it may continue for the Host to pick in its own turn
     (runtime/thread_choice); a switch leaves a marker saying which way it
     went;
  F. the feed reads every topic as one conversation, newest page last, each
     row naming its topic, and pages back by its cursor;
  R. related topics reach the turn only as one-line pointers (title, date);
  W. Workbench's "Where this stands" is one line until opened (closed by
     default on a page never opened), and open it is capped in height with
     its own scroll, as Circles' is;
  L. a reply on another topic, sent while a turn runs (held or streaming),
     streams live into the feed with its own working line, without taking
     the followed turn's stream state, and the activity panel lists it with
     its step; the cards read every topic's open questions at once (not the
     topic of the latest message), and "Where this stands" shows the topic the
     person last wrote to, naming it on its line;
  S. nothing writes a summary after a Circles turn: what fell out of a long
     topic's window rides as that topic's own close_turn notes
     (evicted_record), from that topic's rows only, and a root turn reads
     none.

Fresh clone of the KERNEL's schema (scripts/kernel_schema.py) on TEST_DSN /
PEER_TEST_DSN, else a throwaway cseries-postgres through docker.
"""
import asyncio
import contextlib
import json
import os
import pathlib
import re
import subprocess
import sys
from turn_source import turn_source  # session.py and the modules split out of it

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import asyncpg  # noqa: E402

FAILED = 0


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


def src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ── A: the page (source; the bundle is checked fresh by test_bundle_freshness)
back = src("circles-web/src/cards/own-back.tsx")
hook = src("circles-web/src/cards/use-host-thread.ts")
check("A the Host's back boots on no page: last-open is not read on login",
      "getLastOpenThread(" not in hook, "use-host-thread.ts still reads last-open on boot")
check("A the Host's back shows a greeting on a blank page", "HostGreeting" in back and "greeting" in back.lower())
check("A no thread tabs and no new-thread button on the Host's back",
      "hostTabs(" not in back and "newThread" not in back and "New thread" not in back)
check("A every send asks the Host for the topic (pick_topic)",
      "pickTopic" in hook and "pick_topic" in src("circles-web/src/lib/api.ts"))
check("A earlier messages load by scrolling up (the feed, a page at a time)",
      "fetchFeed(" in hook and "loadEarlier" in hook)

# ── L: a second topic streams live, and what the page follows (source)
chat_hook = src("circles-web/src/hooks/use-circles-chat.ts")
act = src("circles-web/src/cards/activity.tsx")
check("L a reply on another topic, sent while a turn runs, streams live into the feed "
      "(its stream is read, never let go once the server has the words)",
      "consumeCirclesStream(res, makeHandlers(asstId, t0, false, true))" in chat_hook
      and "chat.sendBeside(SELF, text," in hook and "ctrl.abort();\n      setThreadId(dest);" not in hook)
check("L ...without taking the followed turn's session, turn id, status or depth frame",
      "if (side) besideSessionRef.current.set(asstId, it.session_id);" in chat_hook
      and "if (!side) setStatus(\"ready\");" in chat_hook and "if (!side) setContextFrame(cf);" in chat_hook)
check("L ...with its own working line under it, and the panel lists it with its step",
      "{m.beside && m.streaming && <BesideWorking hostName={hostName} message={m} />}" in back
      and "export function BesideWorking" in act and "data-activity-beside-step" in act
      and "beside={besideReplies}" in back)
check("L words typed while a turn streams get the Host's topic pick too (not always a nudge)",
      "whileRunning(text, opts, true)" in hook and ".nudge(text, { model: currentModel" in back)
check("L cards follow the whole conversation, not the topic of the latest message",
      "listRoomTickets(CONVERSATION_ROOM)" in hook and "raiseTickets(r.data.tickets, CONVERSATION_KEY)" in hook
      and "conversationPairing(c.data.connectors)" in hook)
check("L Where this stands shows the topic the person last wrote to, named on its one line",
      "standsTopicId: threadId ?? lastWritten" in hook and "topic={standsId ? titleOf(standsId) : null}" in back
      and "data-stands-topic" in src("circles-web/src/cards/stands.tsx"))

# ── W: Workbench's briefing (source; the bundle is checked fresh elsewhere)
wtop = src("workbench-web/src/book/page-top.tsx")
wapp = src("workbench-web/src/book/book-app.tsx")
wcss = src("workbench-web/src/book/book.css")
check("W Workbench's briefing starts closed on a page never opened: one line, label and first line",
      "useState(() => !!key && !!readStandsOpen()[key])" in wtop
      and "{!open && <span className=\"first\">{first}</span>}" in wtop
      and "{open && <KitStands" in wtop and "pageId={openId}" in wapp)
_cap = re.search(r"\.book-stands-box\[data-open\]\s*\{([^}]*)\}", wcss)
check("W open, it is capped in height with its own scroll (the conversation stays in view)",
      bool(_cap) and "max-height: 34vh" in _cap.group(1) and "overflow: auto" in _cap.group(1))
_line = re.search(r"\.book-stands-toggle \.first\s*\{([^}]*)\}", wcss)
check("W the closed line is one line, cut to the width",
      bool(_line) and "white-space: nowrap" in _line.group(1) and "text-overflow: ellipsis" in _line.group(1))
_ccss = src("circles-web/src/cards/cards.css")
check("W Circles' briefing keeps both: one line until opened, capped when open",
      "max-height: 34%" in _ccss and "{!open && <span className=\"first\">{first}</span>}"
      in src("circles-web/src/cards/stands.tsx"))

from kernel_schema import kernel_schema_db, throwaway_server  # noqa: E402
from mastercode import circles_threads as ct  # noqa: E402
from mastercode.memory_engine import recall_recent_turns  # noqa: E402
from server.connectors import thread_pick as tp  # noqa: E402
from server.turn_registry import TurnRegistry, compose_thread_key  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server and docker unavailable — this gate needs a real database.")
    sys.exit(2)

E = 1
ROUTE = {"provider": "anthropic", "model": "claude-opus-5-5"}


async def turn(pool, content, role, *, mins=0, days=0, thread=None, app_id="circles", extra=None, eid=E):
    md = {"role": role, "source_context": ct.descriptor_for(thread), "app_id": app_id}
    md.update(extra or {})
    return await pool.fetchval(
        "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata, created_at)"
        " VALUES ($1, $2, 'turn', 'active', $3::jsonb,"
        " now() - make_interval(days => $4, mins => $5)) RETURNING id",
        eid, content, json.dumps(md), days, mins)


def offered(landing):
    """The topics a landing hands the Host's turn to pick from."""
    return [c["ref"] for c in (landing.choice or ())]


async def history_of(pool, thread_id):
    rows = await recall_recent_turns(pool, E, "s", limit=200, source_context=ct.descriptor_for(thread_id))
    return [r.get("content") or "" for r in rows]


async def main(dsn):
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        # Topics: an old one (14 days), a tomato one (2 days), and 14 newer.
        old = str((await ct.create_thread(pool, E, title="Greenhouse heater"))["id"])
        await turn(pool, "Set the greenhouse heater thermostat settings to 12C at night", "user", days=14, thread=old)
        await turn(pool, "Done: the greenhouse heater holds 12C overnight.", "assistant", days=14, thread=old)
        tom = str((await ct.create_thread(pool, E, title="Tomato blight"))["id"])
        await turn(pool, "My tomato plants in the vegetable garden have blight", "user", days=2, thread=tom)
        await turn(pool, "Tomato blight treatment options: copper spray, or remove the lower leaves.",
                   "assistant", days=2, thread=tom)
        newer = []
        for i in range(14):
            t = str((await ct.create_thread(pool, E, title=f"Errand {i}"))["id"])
            newer.append(t)
            await turn(pool, f"errand number {i} about invoices and receipts", "user", mins=60 + i, thread=t)
            await turn(pool, f"filed errand {i}", "assistant", mins=59 + i, thread=t)
        await turn(pool, "hello from the original conversation", "user", days=20, thread=None)
        root = await pool.fetchval(
            "INSERT INTO circles_threads (entity_id, title, builtin_type) VALUES ($1, 'Root', 'root') RETURNING id::text", E)
        await turn(pool, "restart the nginx service on mgmt", "user", mins=5, thread=root)

        # C: 14 days ago, behind 14 newer topics.
        land = await tp.pick_topic(pool, E, "greenhouse heater thermostat settings",
                                   current_thread_id=newer[0])
        check("C a topic last active 14 days ago, behind 14 newer ones, is the clear match: the message "
              "goes there, nothing offered", land.thread_id == old and not land.new and not land.choice, land)
        hist = await history_of(pool, land.thread_id)
        check("C the turn's history is that topic's turns",
              any("12C at night" in h for h in hist) and not any("tomato" in h.lower() or "errand" in h for h in hist),
              hist)
        check("N the switch leaves a marker saying it continues the topic, with its date",
              land.marker.startswith("continuing: Greenhouse heater, ") and land.moved, land.marker)
        shown = [r["content"] for r in await pool.fetch(
            "SELECT content FROM entity_memory WHERE metadata->>'source_kind' = $1"
            " AND metadata->'source_context'->>'ref' = $2", tp.TOPIC_MARKER_KIND, old)]
        check("N the marker is written in the topic it went to", shown == [land.marker], shown)

        # N: a clear continuation joins; a near miss starts a new topic.
        join = await tp.pick_topic(pool, E, "tomato blight treatment options", current_thread_id=old)
        check("N a clear continuation joins its topic", join.thread_id == tom and not join.new, join)
        near = await tp.pick_topic(pool, E, "plan a new fence around the vegetable garden",
                                   current_thread_id=tom)
        check("N a near miss (same garden, another subject) starts a topic of its own, offered none to join",
              near.new and near.thread_id not in (tom, old) and near.moved and near.marker == "new topic"
              and not near.choice, near)
        check("N ...whose history holds none of the other topic's turns",
              not any("tomato" in h.lower() for h in await history_of(pool, near.thread_id)))
        stay = await tp.pick_topic(pool, E, "yes do that", current_thread_id=tom)
        check("N a short follow-on reply stays on the topic in use, with no marker and nothing offered",
              stay.thread_id == tom and stay.follow_on and not stay.marker and not stay.choice, stay)
        stay2 = await tp.pick_topic(pool, E, "ok go ahead with the copper spray", current_thread_id=tom)
        check("N an acknowledging reply starts a topic of its own and offers the Host only the topic in use",
              stay2.new and stay2.thread_id != tom and offered(stay2) == [tom], stay2)
        check("N ...kept on that topic's marker, for a send that asked first",
              [c["ref"] for c in await tp.pending_choice(pool, E, stay2.thread_id)] == [tom])
        # Longer than a bare reply and opening like one, but about something
        # else: never assumed to continue the topic in use. The Host decides
        # in its turn; until then it is a topic of its own.
        aside = await tp.pick_topic(pool, E, "also remind me to call mom tomorrow", current_thread_id=tom)
        check("N a subject of its own after an opener ('also ...') is no follow-on: a topic of its own, "
              "the topic in use offered", aside.new and aside.thread_id != tom and offered(aside) == [tom], aside)
        aside2 = await tp.pick_topic(pool, E, "this weekend plan a trip to Rome", current_thread_id=tom)
        check("N ...as is one opening 'this ...': the Host decides whether it continues the topic in use",
              aside2.new and aside2.thread_id != tom and offered(aside2) == [tom], aside2)
        alone = await tp.pick_topic(pool, E, "book a table for four on Friday evening", current_thread_id=tom)
        check("N a message with nothing to continue starts a topic of its own with nothing offered",
              alone.new and alone.thread_id != tom and not alone.choice, alone)
        blank = await tp.pick_topic(pool, E, "yes", current_thread_id=None, current_known=False)
        check("N on a blank page there is nothing to follow on from: a topic of its own",
              blank.new and not blank.follow_on, blank)
        again = await tp.pick_topic(pool, E, "tomato blight treatment options", current_thread_id=tom)
        check("N a clear match for the topic in use stays there with no marker",
              again.thread_id == tom and not again.new and not again.marker, again)
        check("N a score at the bar with no lead over the runner-up joins nothing",
              tp.clear_match([("thread:a", 0.9), ("thread:b", 0.85)]) is None
              and tp.clear_match([("thread:a", 0.55)]) is None
              and tp.clear_match([("thread:a", 0.7), ("thread:b", 0.5)]) == "thread:a")
        src_tp = src("server/connectors/thread_pick.py")
        check("N placement calls no model", "call_model" not in src_tp and "_call_entity_model" not in src_tp)

        # D: the root thread.
        r = await tp.pick_topic(pool, E, "and now restart postgres too", current_thread_id=root)
        check("D a message sent to the root thread stays there: no placement, no marker, nothing offered",
              r.root and r.thread_id == root and not r.marker and not r.choice, r)
        r2 = await tp.pick_topic(pool, E, "restart the nginx service on mgmt", current_thread_id=tom)
        check("D the root thread is never joined or offered, even when its words match",
              r2.thread_id != root and root not in offered(r2), r2)
        n_root = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE metadata->'source_context'->>'ref' = $1", root)
        check("D nothing is written into the root thread by placement", n_root == 1, n_root)

        # R: related topics are pointers.
        rel = await tp.related_topics(pool, E, "invoices and receipts errand", thread_id=newer[0])
        check("R related topics are one-line pointers: id, title, date, never turns",
              rel and all(set(x) == {"id", "title", "last_active"} for x in rel)
              and all(x["id"] != newer[0] for x in rel) and len(rel) <= tp.TOPIC_RELATED_MAX, rel)

        # F: the feed.
        # circles.py imports server.main, which mounts the routers at import
        # time: the stand-in module every Circles route test uses.
        import types
        sys.modules.setdefault("server.main", types.ModuleType("server.main"))
        from server.routes import circles as rc

        async def _name():
            return "Host"
        rc._entity_display_name = _name
        page = await rc.read_feed(pool, E, limit=10)
        msgs = page["messages"]
        check("F the feed's newest page ends with the newest message",
              msgs and all(a["created_at"] <= b["created_at"] for a, b in zip(msgs, msgs[1:])),
              [m_["body"] for m_ in msgs][-3:])
        check("F every row names its topic", all(m_.get("feed_topic", {}).get("title") for m_ in msgs))
        everything = list(msgs)
        cur = page["next"]
        while cur:
            from datetime import datetime
            page = await rc.read_feed(pool, E, before=datetime.fromisoformat(cur["before"]),
                                      before_id=cur["before_id"], limit=10)
            everything = page["messages"] + everything
            cur = page["next"]
        bodies = [m_["body"] for m_ in everything]
        check("F scrolling up reaches every topic, 14 days ago included",
              "Set the greenhouse heater thermostat settings to 12C at night" in bodies
              and len(bodies) == len(set(m_["id"] for m_ in everything)), len(bodies))
        # There is no original conversation: a row left in
        # the legacy app:circles scope is in the database but in no feed.
        check("F ...and a row left in the old original conversation is in no page of the feed",
              "hello from the original conversation" not in bodies
              and await pool.fetchval("SELECT count(*) FROM entity_memory WHERE content ="
                                      " 'hello from the original conversation'") >= 1, len(bodies))
        check("F a topic marker is flagged as one", any(m_.get("topic_marker") for m_ in everything))
        check("F the root thread's rows are in the feed, named as root",
              any(m_["feed_topic"].get("root") for m_ in everything))
        # F2: one topic's pages (the back shows the thread
        # the message went to, and its earlier messages only).
        from datetime import datetime
        for _ in range(12):
            await turn(pool, "another greenhouse note", "assistant", days=15, thread=old)
        one = await rc.read_feed(pool, E, thread=old, limit=5)
        mine = list(one["messages"])
        cur = one["next"]
        while cur:
            one = await rc.read_feed(pool, E, before=datetime.fromisoformat(cur["before"]),
                                     before_id=cur["before_id"], limit=5, thread=old)
            mine = one["messages"] + mine
            cur = one["next"]
        whole, cur = [], {"before": None}
        while cur:
            pg = await rc.read_feed(pool, E, before=datetime.fromisoformat(cur["before"]) if cur["before"] else None,
                                    before_id=cur.get("before_id"), limit=50)
            whole = pg["messages"] + whole
            cur = pg["next"]
        theirs = [m_["id"] for m_ in whole if m_["feed_topic"]["id"] == old]
        check("F2 a topic's own pages are the feed's rows of that topic: each once, oldest first, no other",
              [m_["id"] for m_ in mine] == theirs and len(theirs) >= 14
              and all(m_["feed_topic"]["id"] == old for m_ in mine)
              and all(a["created_at"] <= b["created_at"] for a, b in zip(mine, mine[1:])),
              (len(mine), len(theirs), [m_["body"][:30] for m_ in mine][-3:]))
        check("F2 ...and a topic with no rows has none",
              (await rc.read_feed(pool, E, thread="99999999-9999-9999-9999-999999999999"))["messages"] == [])
        # Another Host's thread is not this Host's (both entity conditions of
        # _feed_where): a real second Host, a live thread of its own with rows.
        # Seeded here, after F2, so no check above reads them.
        E2 = E + 1
        await pool.execute("INSERT INTO entity_registry (id, display_name) VALUES ($1, 'Neighbour Host')"
                           " ON CONFLICT (id) DO NOTHING", E2)
        theirs_t = str((await ct.create_thread(pool, E2, title="Greenhouse heater"))["id"])
        for i in range(3):
            await turn(pool, f"neighbour greenhouse note {i}", "user", mins=30 - i, thread=theirs_t, eid=E2)
        check("F2 a second Host's thread is real: its own feed reads its rows",
              len((await rc.read_feed(pool, E2, thread=theirs_t))["messages"]) == 3)
        check("F2 ...and this Host's feed, asked for that thread, has none",
              (await rc.read_feed(pool, E, thread=theirs_t))["messages"] == [])
        # Each condition alone. The second Host's rows naming THIS Host's
        # thread are not this Host's (entity_memory.entity_id = $1) ...
        for i in range(2):
            await turn(pool, f"neighbour note filed under ours {i}", "user", mins=20 - i, thread=old, eid=E2)
        ours = (await rc.read_feed(pool, E, thread=old))["messages"] + (await rc.read_feed(pool, E))["messages"]
        check("F2 ...nor are the second Host's rows that name this Host's thread, in its pages or the feed",
              ours and not any("neighbour" in m_["body"] for m_ in ours),
              [m_["body"][:40] for m_ in ours if "neighbour" in m_["body"]])
        # ... and this Host's rows naming a thread that is not its own are on
        # no page (circles_threads.entity_id = $1).
        await turn(pool, "our note naming the neighbour thread", "user", mins=10, thread=theirs_t)
        mine2 = (await rc.read_feed(pool, E, thread=theirs_t))["messages"] + (await rc.read_feed(pool, E))["messages"]
        check("F2 ...and this Host's rows that name another Host's thread are on no page",
              not any(m_["body"] == "our note naming the neighbour thread" for m_ in mine2),
              [m_["body"][:40] for m_ in mine2][-3:])

        # S: no summary writer; what fell out rides as the topic's own notes.
        await record_not_summary(pool, root)

        # L: the cards' read is the whole conversation (tickets.CONVERSATION_ROOM).
        from server.routes import tickets as tk

        async def ticket(asked_in):
            return await pool.fetchval(
                "INSERT INTO entity_tickets (entity_id, source, sitrep, status, metadata)"
                " VALUES ($1, 'manual', 'Pick one', 'open', $2::jsonb) RETURNING id",
                E, json.dumps({"asked_in": asked_in}))
        t_old = await ticket({"app_id": "circles", "project_id": None, "thread_id": old})
        t_tom = await ticket({"app_id": "circles", "project_id": None, "thread_id": tom.upper()})
        t_orig = await ticket({"app_id": "circles", "project_id": None, "thread_id": None})
        t_wb = await ticket({"app_id": "workbench", "project_id": 7, "thread_id": None})
        t_other = await ticket({"app_id": "circles", "project_id": None,
                                "thread_id": "99999999-9999-9999-9999-999999999999"})
        got = {t["id"] for t in (await tk.list_tickets_service(
            pool, E, status_filter="open", asked_in=tk.CONVERSATION_ROOM, limit=200))["tickets"]}
        check("L the conversation's cards read every topic's open questions at once",
              {t_old, t_tom, t_orig} <= got, (got, t_old, t_tom, t_orig))
        check("L ...and nothing of a Workbench project or a thread not this Host's",
              t_wb not in got and t_other not in got, got)
    finally:
        await pool.close()


async def record_not_summary(pool, root):
    """S: no summary is written after a turn; a long topic's evicted turns
    ride as their own close_turn notes."""
    from mastercode import evicted_record as er
    from mastercode.runtime import session as S

    check("S no summary is dispatched after a turn", not hasattr(S, "dispatch_topic_summary"))
    kiln = str((await ct.create_thread(pool, E, title="Kiln firing"))["id"])
    ids = []
    for i in range(12):
        ids.append(await turn(pool, f"kiln firing step {i}: ramp and hold", "user", mins=200 - 2 * i, thread=kiln))
        ids.append(await turn(pool, f"noted kiln step {i}", "assistant", mins=199 - 2 * i, thread=kiln,
                              extra={"close_turn": {"handoff": {"next_step": f"fire step {i + 1}"}}}))
    n_summ = await pool.fetchval("SELECT count(*) FROM entity_memory WHERE memory_type = 'summary'")
    rec = await er.evicted_record(pool, E, {"kind": "thread", "ref": kiln}, "s-sum", oldest_kept_id=ids[12])
    check("S the turns that fell out ride as the topic's own notes, newest first",
          rec and rec.splitlines()[1].endswith("next step: fire step 6") and "fire step 7" not in rec, rec)
    check("S ...from that topic's rows only", rec and "tomato" not in rec.lower() and "errand" not in rec)
    check("S ...and nothing was written to build it", n_summ == await pool.fetchval(
        "SELECT count(*) FROM entity_memory WHERE memory_type = 'summary'") == 0, n_summ)
    hist = turn_source(ROOT)
    check("S a root turn reads no record",
          "_record_ok = bool(_src_ctx) and not getattr(sess, \"history_before\", None) \\\n"
          "                and not _work_lane and not _root_turn" in hist)


async def concurrency():
    """B: one running turn per topic; topics run side by side."""
    reg = TurnRegistry()
    gate = asyncio.Event()

    def gen(text):
        async def g():
            yield f'0:"{text}"\n'
            await gate.wait()
            yield "e:{}\n"
        return g
    ka = compose_thread_key(E, "circles", None, thread_id="11111111-1111-1111-1111-111111111111")
    kb = compose_thread_key(E, "circles", None, thread_id="22222222-2222-2222-2222-222222222222")
    ra, ma = reg.start_or_attach(thread_key=ka, entity_id=E, app_id="circles", message="topic A",
                                 make_inner=gen("a"))
    rb, mb = reg.start_or_attach(thread_key=kb, entity_id=E, app_id="circles", message="topic B",
                                 make_inner=gen("b"))
    await asyncio.sleep(0.05)
    check("B two messages on two topics both start, and run at the same time",
          (ma, mb) == ("started", "started") and ra is not rb
          and not ra.done.is_set() and not rb.done.is_set(), (ma, mb))
    _, mn = reg.start_or_attach(thread_key=ka, entity_id=E, app_id="circles", message="more on A",
                                make_inner=gen("x"))
    check("B words for the topic already running go into that turn as a nudge", mn == "nudged", mn)
    gate.set()
    await asyncio.wait_for(asyncio.gather(ra.done.wait(), rb.done.wait()), 5)


asyncio.run(concurrency())
with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, kernel_schema_db(_srv) as _db:
    asyncio.run(main(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else 0)
