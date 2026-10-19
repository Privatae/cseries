#!/usr/bin/env python3
"""Gate: parallel work from the chat (migrations/v226,
runtime/task_record.py op='run' with items and op='item',
runtime/task_serial.py _drive_work, routes/circles.py GET /work, circles-web
activity.tsx).

  M. v226 applies again over the kernel's schema;
  R. one message carrying two tickets runs as two internal threads: the Host
     starts the run from a Circles topic with no project open, a project is
     made for it without asking, and each thread owns one work item;
  O. a thread writes only its own item, never a sibling's;
  S. at every turn start a thread reads its sibling's item (status, step,
     findings, links) from the record, and no turn's transcript;
  H. a thread's turn runs in the run's project, whose history holds every
     thread's replies: each woken turn carries its own lane, and its history
     is that lane's rows only, never a sibling thread's turns;
  P. the activity panel's read lists both threads, each with its current
     step, and the panel draws them;
  F. each thread's finish posts ONE line into the live conversation, in the
     topic the run was started from (the feed reads it), and the run ends
     when every item is done;
  T. an internal thread is never offered as a topic: its turns, in the run's
     project, are no candidate of the topic pick and never in the feed.

Fresh clone of the KERNEL's schema on TEST_PG_DSN / PEER_TEST_DSN / TEST_DSN,
else a throwaway cseries-postgres through docker. No model: the woken turns are
a stand-in runtime that does each thread's work against the real record.
"""
import asyncio
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import types

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import asyncpg  # noqa: E402

from kernel_schema import _psql, kernel_schema_db, throwaway_server  # noqa: E402

SERVER = os.environ.get("TEST_PG_DSN") or os.environ.get("PEER_TEST_DSN") or os.environ.get("TEST_DSN")
if not SERVER and subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
    print("SKIP-LOUD: no test server (TEST_PG_DSN/PEER_TEST_DSN/TEST_DSN) and docker unavailable — "
          "this gate needs a real database.")
    sys.exit(2)

FAILED = 0
E = 1
MIGRATION = ROOT / "migrations" / "v226_task_work_items.sql"
T1 = "Ticket 101: fix the login redirect loop on the members page"
T2 = "Ticket 102: update the wording of the billing reminder email"


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {str(detail)[:600]}"))
    if not cond:
        FAILED += 1


def src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ── P (source): the panel draws the threads with their steps ─────────────────
act = src("circles-web/src/cards/activity.tsx")
check("P the activity panel lists the internal threads, each with its step",
      "fetchWork()" in act and "data-activity-work={w.item_id}" in act
      and "data-activity-work-step" in act and "{workStep(w)}" in act)
check("P their turns are listed with their items, never as other conversations",
      "!/\\|task-\\d+-t\\d+$/.test(t.thread_key" in act)

if "server.main" not in sys.modules:
    import server as _srvpkg
    _stub = types.ModuleType("server.main")
    sys.modules["server.main"] = _stub
    _srvpkg.main = _stub


class Runtime:
    """Stands in for EntityRuntime: each woken thread turn does its work
    against the real record through task_record, as the Host's tools would."""

    def __init__(self, behave):
        self.behave, self.calls = behave, []

    async def _wake_host_turn(self, **kw):
        self.calls.append(kw)
        return await self.behave(kw)


async def main(dsn):
    r = _psql(dsn, "-v", "ON_ERROR_STOP=1", "-f", str(MIGRATION))
    check("M v226 applies again over the kernel's schema, failing nothing", r.returncode == 0, r.stderr[-300:])

    from mastercode import circles_threads as ct
    from mastercode.runtime import task_record as tr
    from mastercode.runtime import task_serial as ts
    from mastercode.runtime import host_memory as hm
    from server import brain_service
    from server.connectors import thread_pick as tp
    from server.routes import circles as rc

    ts.SKIP_PAUSE_S = 0.0
    ts.MEMORY_PAUSE_S = 0.05
    hm.CGROUP_ROOT = "/nonexistent-cgroup-for-gate"     # no readable limit: the width asked for
    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)

    async def _name():
        return "Host"
    rc._entity_display_name = _name
    try:
        topic = str((await ct.create_thread(pool, E, title="Two tickets"))["id"])
        projects_before = await pool.fetchval("SELECT count(*) FROM projects WHERE entity_id = $1", E)
        seen: dict = {"briefs": {}, "panel": None, "refused": None}
        box: dict = {}

        async def op(a):
            return await tr.run_task_record_op(pool, E, a, project_id=box["pid"], thread_key="lane",
                                               session_id="s")

        async def behave(kw):
            t = int(kw["lane"].rsplit("-t", 1)[1])
            n = sum(1 for c in rt.calls if c.get("lane") == kw["lane"])
            # What the woken turn would see as its own lane (task_serial
            # CURRENT_WORK_LANE): its history is read and tagged by it.
            _lv = getattr(ts, "CURRENT_WORK_LANE", None)
            seen.setdefault("lanes", []).append((kw["lane"], _lv.get() if _lv is not None else None))
            seen["briefs"].setdefault(t, []).append(kw["brief"])
            if n == 1:
                # Round 1: each thread writes its step and what it found.
                if t == 0:
                    await op({"op": "item", "step": "patching the auth middleware",
                              "note": "the redirect loop comes from the session cookie path"})
                    await op({"op": "item", "link": "https://git.example/pr/12"})
                else:
                    await op({"op": "item", "step": "reading the email templates"})
                    sibling = await pool.fetchval(
                        "SELECT id FROM task_work_items WHERE project_id = $1 AND thread_no = 0", box["pid"])
                    seen["refused"] = await op({"op": "item", "id": sibling, "status": "done"})
                await asyncio.sleep(0.05)
                seen["panel"] = await rc.read_work(pool, E)
                return True
            if t == 0:
                await op({"op": "item", "status": "done", "line": "Pull request 12 opened with the fix."})
            else:
                await op({"op": "item", "status": "done", "line": "The new wording is in the template."})
            return True

        rt = Runtime(behave)
        brain_service.get_runtime = lambda: rt
        res = await tr.run_task_record_op(
            pool, E, {"op": "run", "items": [T1, T2], "request": f"{T1}\n{T2}"},
            project_id=None, thread_key=topic, session_id="s-chat", interaction_context="circles")
        out = res.get("result") or {}
        box["pid"] = out.get("project_id")
        run_id = out.get("run")
        check("R a run with two items started from a Circles topic with no project open starts",
              res.get("status") == "ok" and out.get("mode") == "work" and out.get("threads") == 2, res)
        check("R ...a project is made for it, without asking",
              await pool.fetchval("SELECT count(*) FROM projects WHERE entity_id = $1", E) == projects_before + 1
              and box["pid"] is not None, box)
        items = await pool.fetch("SELECT * FROM task_work_items WHERE run_id = $1 ORDER BY thread_no", run_id)
        check("R ...and each of the two internal threads owns one work item",
              [(i["thread_no"], i["goal"]) for i in items] == [(0, T1), (1, T2)], [dict(i) for i in items])
        origin = json.loads(await pool.fetchval("SELECT origin FROM task_runs WHERE id = $1", run_id) or "{}")
        check("R the run remembers the topic it was started from", origin.get("thread_id") == topic, origin)

        # Let the run go to its end.
        loop = asyncio.get_running_loop()
        end = loop.time() + 30
        while loop.time() < end:
            if await pool.fetchval("SELECT status FROM task_runs WHERE id = $1", run_id) != "running":
                break
            await asyncio.sleep(0.05)
        status = await pool.fetchval("SELECT status FROM task_runs WHERE id = $1", run_id)
        check("F the run ends finished once every item is done", status == "finished", status)
        lanes = sorted({c.get("lane") for c in rt.calls})
        check("R the threads ran on their own lanes, side by side",
              lanes == [f"task-{run_id}-t0", f"task-{run_id}-t1"], lanes)

        # H: a thread's turn reads its own turns as history, never a
        # sibling's. Each turn runs in the run's project, whose history is
        # every thread's replies; the lane tag scopes it to the thread's own.
        check("H each woken thread turn carries its own lane (CURRENT_WORK_LANE), and only it",
              bool(seen.get("lanes")) and all(want == got for want, got in seen["lanes"]),
              seen.get("lanes"))
        from mastercode import memory_engine as me
        _pid = box["pid"]
        for _lane, _text in ((f"task-{run_id}-t0", "thread one reply: patched the middleware"),
                             (f"task-{run_id}-t1", "thread two reply: reading the templates")):
            await pool.execute(
                "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata, project_id)"
                " VALUES ($1, $2, 'turn', 'active', $3::jsonb, $4)", E, _text,
                json.dumps({"role": "assistant", "app_id": "workbench", "task_lane": _lane,
                            "source_context": {"kind": "project", "ref": str(_pid)}}), _pid)
        try:
            hist = await me.recall_recent_turns(pool, E, "s", limit=50,
                                                source_context={"kind": "project", "ref": str(_pid)},
                                                lane=f"task-{run_id}-t1")
        except TypeError as e:
            hist = [{"content": f"no lane scope: {e}"}]
        bodies_h = [h["content"] for h in hist]
        check("H thread 2's history is its own turns, never thread 1's",
              any("thread two reply" in b for b in bodies_h)
              and not any("thread one reply" in b for b in bodies_h), bodies_h)
        sess_src = src("brain/mastercode/runtime/session.py")
        check("H the turn tags its rows with its lane and reads history by it (session.py)",
              '_turn_tags["task_lane"] = _work_lane' in sess_src and "lane=_work_lane" in sess_src)

        check("O a thread writes only its own item, never a sibling's",
              isinstance(seen["refused"], dict) and seen["refused"].get("status") != "ok"
              and "own work item" in json.dumps(seen["refused"]), seen["refused"])

        b1 = seen["briefs"].get(1) or ["", ""]
        check("S at its next turn start, thread 2 reads thread 1's item: status, step, findings, links",
              len(b1) >= 2 and "thread 1: " + T1 in b1[1] and "[working]" in b1[1]
              and "patching the auth middleware" in b1[1]
              and "the redirect loop comes from the session cookie path" in b1[1]
              and "https://git.example/pr/12" in b1[1], b1[-1][:1500])
        check("S ...beside its own item, from the record and not from any turn's transcript",
              "Your work item" in b1[1] and T2 in b1[1] and "not from any thread's transcript" in b1[1],
              b1[1][:400])

        panel = (seen["panel"] or {}).get("threads") or []
        check("P the panel's read lists both threads, each with its current step",
              sorted((p["thread"], p["step"]) for p in panel) == [
                  (1, "patching the auth middleware"), (2, "reading the email templates")], panel)
        check("P ...and names the lane each one's turns run on",
              sorted(p["lane"] for p in panel) == [f"task-{run_id}-t0", f"task-{run_id}-t1"], panel)

        lines = await pool.fetch(
            "SELECT content, metadata FROM entity_memory WHERE entity_id = $1"
            " AND metadata->>'source_kind' = 'work_line' ORDER BY id", E)
        # The threads run side by side (R above), so which finish lands first
        # is theirs to race: one line per thread, read by its thread number.
        by_thread = sorted(r["content"] for r in lines)
        check("F each thread's finish posts ONE line",
              len(lines) == 2 and by_thread[0].startswith("Thread 1 of 2 finished: Ticket 101")
              and "Pull request 12 opened" in by_thread[0]
              and by_thread[1].startswith("Thread 2 of 2 finished: Ticket 102"),
              [r["content"] for r in lines])
        check("F ...into the topic the run was started from",
              all(json.loads(r["metadata"])["source_context"] == {"kind": "thread", "ref": topic}
                  for r in lines), [r["metadata"] for r in lines])
        again = await ts.post_item_line(pool, dict(items[0]))
        check("F a line is posted once: a second post of a done item writes nothing", again is False)

        feed = (await rc.read_feed(pool, E, limit=50))["messages"]
        bodies = [m["body"] for m in feed]
        check("F the live conversation's feed reads both lines, in that topic",
              sum(1 for b in bodies if b.startswith("Thread ") and " of 2 finished: " in b) == 2
              and all(m["feed_topic"]["id"] == topic for m in feed if m["body"].startswith("Thread ")),
              bodies[-5:])

        # O: a run started before there were topics everywhere (its origin
        # names no thread, as one from the old original conversation does).
        # There is no original conversation: its lines
        # land in ONE topic of its own, recorded on the run, even when two
        # items finish at the same moment.
        old_run = await pool.fetchval(
            "INSERT INTO task_runs (entity_id, project_id, request, threads, origin)"
            " VALUES ($1, $2, 'Old errands', 2, $3::jsonb) RETURNING id",
            E, box["pid"], json.dumps({"interaction_context": "chat", "session_id": "s-old"}))
        olds = [await pool.fetchrow(
            "INSERT INTO task_work_items (entity_id, project_id, run_id, thread_no, goal, status, outcome)"
            " VALUES ($1, $2, $3, $4, $5, 'done', 'done.') RETURNING *",
            E, box["pid"], old_run, n, f"Old errand {n + 1}") for n in range(2)]
        topics_before = await pool.fetchval("SELECT count(*) FROM circles_threads WHERE entity_id = $1", E)
        posted = await asyncio.gather(*(ts.post_item_line(pool, dict(r)) for r in olds))
        old_lines = await pool.fetch(
            "SELECT metadata FROM entity_memory WHERE metadata->>'task_run_id' = $1", str(old_run))
        refs = {json.loads(r["metadata"])["source_context"].get("ref") for r in old_lines}
        kinds = {json.loads(r["metadata"])["source_context"].get("kind") for r in old_lines}
        old_origin = json.loads(await pool.fetchval("SELECT origin FROM task_runs WHERE id = $1", old_run) or "{}")
        check("O a run whose origin names no thread posts both lines into ONE topic of its own, never the "
              "old original conversation",
              all(posted) and len(old_lines) == 2 and kinds == {"thread"} and len(refs) == 1, (posted, kinds, refs))
        check("O ...the run records that topic, and exactly one topic was made for it",
              old_origin.get("thread_id") in refs
              and await pool.fetchval("SELECT count(*) FROM circles_threads WHERE entity_id = $1", E)
              == topics_before + 1, (old_origin, refs))
        # T: the threads' own turns live in the project; none is a topic.
        pid = box["pid"]
        for i in range(3):
            await pool.execute(
                "INSERT INTO entity_memory (entity_id, content, memory_type, status, metadata, project_id)"
                " VALUES ($1, $2, 'turn', 'active', $3::jsonb, $4)", E,
                f"login redirect loop members page session cookie fix step {i}",
                json.dumps({"role": "assistant", "app_id": "workbench",
                            "source_context": {"kind": "project", "ref": str(pid)}}), pid)
        scores = await tp.topic_scores(pool, E, "login redirect loop on the members page")
        check("T the threads' turns (in the run's project) are no candidate of the topic pick",
              all(not k.startswith("project:") for k, _ in scores), scores)

        shown = {}

        async def model(messages, **kw):
            shown["system"] = messages[0]["content"]
            return {"tool_calls": [{"function": {"name": tp.TOPIC_PICK_TOOL_NAME, "arguments": json.dumps(
                {"new": True, "title": "Login redirect", "reason": "nothing matches"})}}]}
        land = await tp.pick_topic(pool, E, "how is the login redirect loop fix going",
                                   current_thread_id=topic, call_model=model,
                                   route={"provider": "anthropic", "model": "m"})
        check("T ...the Host is never shown one, and a message never lands in one",
              "project:" not in shown.get("system", "") and land.thread_id not in (str(pid), f"project:{pid}"),
              (shown.get("system", "")[:300], land))
        feed = (await rc.read_feed(pool, E, limit=200))["messages"]
        check("T ...and none of their turns is in the conversation's feed",
              not any("session cookie fix step" in m["body"] for m in feed))
    finally:
        await pool.close()


with (contextlib.nullcontext(SERVER) if SERVER else throwaway_server()) as _srv, kernel_schema_db(_srv) as _db:
    asyncio.run(main(_db))
print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else 0)
