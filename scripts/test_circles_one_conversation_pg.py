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
  D. a message sent to the root thread never enters topic picking (no model
     call), and the root thread is never offered to a pick;
  N. conservative picking: a near miss (same broad area, another subject)
     starts a new topic and loads none of the other topic's turns; a clear
     continuation joins; a short follow-on reply stays on the topic in use;
     anything unclear (a failed pick) starts a new topic; a switch leaves a
     marker saying which way it went;
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
  S. after a Circles turn, its topic's summary of what overflowed is written
     to the database when the topic's history is past its budget
     (session.dispatch_topic_summary; the turn is the one writer since the
     batch consolidator went), from that topic's turns only, and is
     what the topic's next history read gets (summary_policy.summary_on);
     never for the root thread, a turn with no history or a Workbench project.

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


async def turn(pool, content, role, *, mins=0, days=0, thread=None, app_id="circles", extra=None):
    md = {"role": role, "source_context": ct.descriptor_for(thread), "app_id": app_id}
    md.update(extra or {})
    return await pool.fetchval(
        "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata, created_at)"
        " VALUES ($1, $2, 'turn', 'active', $3::jsonb,"
        " now() - make_interval(days => $4, mins => $5)) RETURNING id",
        E, content, json.dumps(md), days, mins)


class Model:
    """A scripted Host: joins the topic it is shown, else starts one titled
    `title`. Counts its calls and keeps what it was shown."""
    def __init__(self, title="Fresh topic", join=True, fail=False):
        self.calls, self.system, self.title, self.join, self.fail = 0, "", title, join, fail

    async def __call__(self, messages, **kw):
        self.calls += 1
        self.system = messages[0]["content"]
        if self.fail:
            return {"error": "the model is down"}
        ids = re.findall(r"^\[((?:thread|app):[^\]]+)\]", self.system, re.M)
        args = ({"topic": ids[0], "reason": "it continues it"} if ids and self.join
                else {"new": True, "title": self.title, "reason": "nothing matches"})
        return {"tool_calls": [{"function": {"name": tp.TOPIC_PICK_TOOL_NAME, "arguments": json.dumps(args)}}]}


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
        m = Model()
        land = await tp.pick_topic(pool, E, "greenhouse heater thermostat settings",
                                   current_thread_id=newer[0], call_model=m, route=ROUTE)
        check("C a topic last active 14 days ago, behind 14 newer ones, is the one the Host is shown",
              f"[thread:{old}]" in m.system and m.system.count("[thread:") == 1, m.system[:300])
        check("C the message goes to that topic", land.picked and land.thread_id == old and not land.new, land)
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
        m = Model()
        join = await tp.pick_topic(pool, E, "tomato blight treatment options", current_thread_id=old,
                                   call_model=m, route=ROUTE)
        check("N a clear continuation joins its topic", join.thread_id == tom and not join.new, join)
        m = Model(title="Garden fence")
        near = await tp.pick_topic(pool, E, "plan a new fence around the vegetable garden",
                                   current_thread_id=tom, call_model=m, route=ROUTE)
        check("N a near miss (same garden, another subject) is shown no topic to join",
              "[thread:" not in m.system and "[app:" not in m.system, m.system[:300])
        check("N ...and starts a new topic", near.new and near.thread_id not in (tom, old) and near.moved
              and near.marker == "new topic: Garden fence", near)
        check("N ...whose history holds none of the other topic's turns",
              not any("tomato" in h.lower() for h in await history_of(pool, near.thread_id)))
        m = Model()
        stay = await tp.pick_topic(pool, E, "yes do that", current_thread_id=tom, call_model=m, route=ROUTE)
        check("N a short follow-on reply stays on the topic in use, with no pick and no marker",
              stay.thread_id == tom and stay.follow_on and m.calls == 0 and not stay.marker, (stay, m.calls))
        m = Model()
        stay2 = await tp.pick_topic(pool, E, "ok go ahead with the copper spray", current_thread_id=tom,
                                    call_model=m, route=ROUTE)
        check("N an acknowledging reply stays too: the Host is shown only the topic in use, and joins it",
              stay2.thread_id == tom and m.calls == 1 and f"[thread:{tom}]" in m.system
              and len(re.findall(r"^\[(?:thread|app):", m.system, re.M)) == 1 and not stay2.new, (stay2, m.system[:300]))
        # Longer than a bare reply and opening like one, but about something
        # else: never assumed to continue the topic in use. The Host decides,
        # and unsure is a new topic.
        m = Model(title="Call mom", join=False)
        aside = await tp.pick_topic(pool, E, "also remind me to call mom tomorrow", current_thread_id=tom,
                                    call_model=m, route=ROUTE)
        check("N a subject of its own after an opener ('also ...') is no follow-on: the Host picks, and it is new",
              m.calls == 1 and aside.new and aside.thread_id != tom, (aside, m.calls))
        m = Model(fail=True)
        aside2 = await tp.pick_topic(pool, E, "this weekend plan a trip to Rome", current_thread_id=tom,
                                     call_model=m, route=ROUTE)
        check("N ...and with no answer from the Host it is a new topic, never the topic in use",
              aside2.new and aside2.thread_id != tom, aside2)
        m = Model()
        blank = await tp.pick_topic(pool, E, "yes", current_thread_id=None, current_known=False,
                                    call_model=m, route=ROUTE)
        check("N on a blank page there is nothing to follow on from: the Host picks", m.calls == 1, blank)
        m = Model(fail=True)
        bad = await tp.pick_topic(pool, E, "tomato blight treatment options", current_thread_id=tom,
                                  call_model=m, route=ROUTE)
        check("N a failed pick starts a new topic, never a guess",
              bad.new and bad.thread_id not in (tom, old) and bad.marker.startswith("new topic: "), bad)
        check("N a score at the bar with no lead over the runner-up joins nothing",
              tp.clear_match([("thread:a", 0.9), ("thread:b", 0.85)]) is None
              and tp.clear_match([("thread:a", 0.55)]) is None
              and tp.clear_match([("thread:a", 0.7), ("thread:b", 0.5)]) == "thread:a")

        # D: the root thread.
        m = Model()
        r = await tp.pick_topic(pool, E, "and now restart postgres too", current_thread_id=root,
                                call_model=m, route=ROUTE)
        check("D a message sent to the root thread stays there and no pick runs",
              r.root and r.thread_id == root and m.calls == 0 and not r.marker, (r, m.calls))
        m = Model()
        r2 = await tp.pick_topic(pool, E, "restart the nginx service on mgmt", current_thread_id=tom,
                                 call_model=m, route=ROUTE)
        check("D the root thread is never offered to a pick, even when its words match",
              f"thread:{root}" not in m.system and r2.thread_id != root, (m.system[:200], r2))
        n_root = await pool.fetchval(
            "SELECT count(*) FROM entity_memory WHERE metadata->'source_context'->>'ref' = $1", root)
        check("D nothing is written into the root thread by a pick", n_root == 1, n_root)

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

        # S: after a turn, the topic's summary of what overflowed is written
        # to the database (session.dispatch_topic_summary, the turn's own call;
        # nothing else writes one), and it is what the topic's next history
        # read gets (thread_consolidator.get_live_summary, which session's
        # history read calls when summary_policy.summary_on).
        await summary_after_turn(pool, root)

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


async def summary_after_turn(pool, root):
    """S: the overflow summary a Circles turn asks for once it ends."""
    import types
    from mastercode import thread_consolidator as tc
    from mastercode import summary_policy as sp
    from mastercode.runtime import session as S
    from mastercode.runtime.root_turn import ROOT as ROOT_BUILTIN

    shown = []

    async def model(pool_, eid, messages):
        # The Host's own model, scripted: it is shown the topic's turns and
        # answers with the summary in the consolidator's shape.
        shown.append("\n".join(str(m_.get("content") or "") for m_ in messages))
        return {"content": json.dumps({"summary": "The kiln firing schedule is cone 6, held 15 minutes.",
                                       "topic_changed": False, "topic": "Kiln firing"})}
    real_model = tc._default_model_fn
    tc._default_model_fn = model
    try:
        kiln = str((await ct.create_thread(pool, E, title="Kiln firing"))["id"])
        for i in range(12):
            await turn(pool, f"kiln firing step {i}: " + "ramp and hold detail " * 40, "user",
                       mins=200 - 2 * i, thread=kiln)
            await turn(pool, f"noted kiln step {i}: " + "the schedule as agreed " * 40, "assistant",
                       mins=199 - 2 * i, thread=kiln)
        ctx = {"kind": "thread", "ref": kiln}
        before = await tc.get_live_summary(pool, E, ctx)

        def sess(thread_id, **kw):
            base = dict(entity_id=E, session_id="s-sum", interaction_context="circles", thread_id=thread_id,
                        turn_type=None, _history_budget_tokens=2000)
            base.update(kw)
            return types.SimpleNamespace(**base)

        task = S.dispatch_topic_summary(pool, sess(kiln))
        res = await task if task is not None else None
        after = await tc.get_live_summary(pool, E, ctx)
        check("S after a Circles turn whose topic's history is past its budget, the turn asks for the summary",
              task is not None and isinstance(res, dict) and res.get("status") == "consolidated", res)
        check("S ...the summary is written to the database for that topic, from that topic's turns",
              before is None and after is not None and "cone 6" in (after.get("content") or "")
              and shown and "kiln firing step" in shown[0] and "tomato" not in shown[0].lower(),
              (before, after))
        check("S ...and the topic's next history read gets it (summary_policy.summary_on)",
              sp.summary_on(ctx) and not sp.summary_on(ctx, root=True))

        small = str((await ct.create_thread(pool, E, title="Short chat"))["id"])
        await turn(pool, "a short question", "user", mins=3, thread=small)
        task = S.dispatch_topic_summary(pool, sess(small))
        res = await task if task is not None else None
        check("S a topic well within its budget gets no summary written",
              isinstance(res, dict) and res.get("status") == "skipped"
              and await tc.get_live_summary(pool, E, {"kind": "thread", "ref": small}) is None, res)

        n0 = len(shown)
        check("S the root thread never asks for one",
              S.dispatch_topic_summary(pool, sess(root, turn_type={"builtin": ROOT_BUILTIN})) is None)
        check("S a turn that read no history (no budget) asks for none",
              S.dispatch_topic_summary(pool, sess(kiln, _history_budget_tokens=None)) is None)
        check("S a Workbench project turn asks for none",
              S.dispatch_topic_summary(pool, sess(None, interaction_context="project:7")) is None)
        check("S none of those called the model", len(shown) == n0, len(shown))
    finally:
        tc._default_model_fn = real_model


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
