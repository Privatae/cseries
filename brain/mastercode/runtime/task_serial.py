"""A task's serial run: one Host turn at a time, each started by the platform
with a brief built from the task record, never from the last turn's transcript.

The loop (_drive), per run (migrations/v209 task_runs):
  1. every check answered or unanswerable -> one last turn writes the finished
     work from the record (`task_record op='report'`); the run is finished.
  2. STALL_TURNS landed turns in a row wrote nothing to the record -> one last
     turn reports what is open and why; the run is stalled.
  3. otherwise wake the next turn (EntityRuntime._wake_host_turn: the governed
     pipeline, the registry, the background budget, the 40-turn backstop) and
     measure whether the record moved: any unit found, claimed or covered, any
     finding, any check set. A turn that moved it starts the stall count again.
A turn the platform could not start (the budget stayed full, every slot busy)
is not a turn that wrote nothing: it is tried again after a pause, and after
SKIP_LIMIT such tries in a row the run stops and says why.

The run ends when the Host stops it (`task_record op='stop'`), and survives a
restart: resume_running starts the loop again for every run still running.

A run with threads > 1 goes in rounds: each round starts that many thread turns at once, or as many as
the Host's memory holds then (runtime/host_memory), whichever is fewer — never
a count the platform sets. Each thread turn runs on its own registry lane
(EntityRuntime._wake_host_turn lane=), works its own slice of the next sources,
and claims as its run and thread. The round's progress is measured once, as a
serial turn's is. A thread whose turn did not land (a provider limit, the
memory full) sits out the next round; the others go on.

A run of kind 'work' (migrations/v226) is
parallel work the Host started from the chat: one internal thread per work
item, each owning its item (_drive_work). Every round wakes the threads whose
items are still open or working, side by side, each from a brief built from
the shared record: its own item, and its siblings' status and findings, never
their turns. A thread that finishes or is blocked posts one line into the
conversation the run was started from (post_item_line). The run ends when
every item is done or blocked, or after STALL_TURNS rounds that moved no
item, when the items still open are marked blocked and say so.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging

log = logging.getLogger("cseries.mastercode.task_serial")

STALL_TURNS = 3          # three turns that each wrote nothing
SKIP_LIMIT = 3           # turns the platform could not start, in a row


# Both are the person's limits (runtime/user_limits, 2026-10-20): the
# constants are the defaults, and the person's value wins when they set one.
def stall_max() -> int:
    from mastercode.runtime import user_limits
    return user_limits.person_or("task_run.stall_turns", STALL_TURNS)


def skip_max() -> int:
    from mastercode.runtime import user_limits
    return user_limits.person_or("task_run.skipped_turns", SKIP_LIMIT)


def _stall_hit_line() -> str:
    from mastercode.runtime import user_limits
    return user_limits.hit_line("task_run.stall_turns")


async def _skip_stop_said(pool, run: dict) -> None:
    """The stop after skip_max() turns the platform could not start, said in
    the run's conversation (no turn ran to say it) and noted for Settings >
    Limits. Everything the run recorded stays."""
    from mastercode.runtime import user_limits
    await user_limits.record_hit(pool, run.get("entity_id"), "task_run.skipped_turns",
                                 f"task run {run['id']}")
    await _say_run_stopped(pool, run, (
        f"This task's run stopped: {skip_max()} turns in a row could not be started or "
        "finished (the background budget stayed full, or a turn failed: a provider limit, "
        "an error). Everything it recorded is kept, and asking again starts it from there. "
        + user_limits.hit_line("task_run.skipped_turns")))


async def _say_run_stopped(pool, run: dict, text: str) -> None:
    """One line in the conversation the run was started from, for a stop no
    turn reports (the platform could not start one). Never raises."""
    try:
        from mastercode.brain.offline_task_runner import lands_in_circles, post_to_origin_thread
        origin = _origin(dict(run))
        if lands_in_circles(origin) and not str(origin.get("thread_id") or "").strip():
            origin = await _run_topic(pool, int(run["id"]), int(run["entity_id"]))
        await post_to_origin_thread(
            pool, run["entity_id"], origin, text,
            extra_tags={"source_kind": "task_run_stopped", "task_run_id": run["id"]},
            fallback_session=f"task-run-{run['id']}")
    except Exception as e:  # noqa: BLE001
        log.warning("task run %s: its stop line did not land: %s", run.get("id"), e)
SKIP_PAUSE_S = 120.0
DEFAULT_UNITS = 3        # sources a turn reads when the model's window is unknown
PAGE_CHARS = 40_000      # read_url's default page (url_reader._DEFAULT_MAX_CHARS)
MAX_UNITS = 10
BRIEF_FINDINGS = 30
BRIEF_FINDING_CHARS = 400

_DRIVERS: dict = {}
_WANTED: set = set()     # projects whose live loop must look for a running run again

# The run a turn works for, set around each wake (_wake). The registry creates
# the woken turn's task inside that call, so the task and every tool it runs
# carry this value, and nothing else does: task_record names the run as the
# owner of what such a turn claims (run_owner), and the loop releases only
# those claims. A person's turn on any thread of the project claims as itself.
CURRENT_TASK_RUN: "contextvars.ContextVar[int | None]" = contextvars.ContextVar(
    "current_task_run", default=None)
# The thread of a parallel run a turn works for (None in a serial run), set
# with CURRENT_TASK_RUN: two threads never share an owner, so neither is ever
# told it holds the other's claim.
CURRENT_TASK_THREAD: "contextvars.ContextVar[int | None]" = contextvars.ContextVar(
    "current_task_thread", default=None)
# The lane of an internal thread of a parallel run (a run of work, or
# a research run), set with the two above: "task-<run>-t<n>". Its turns run in
# the run's project, whose history is every thread's replies, so the turn tags
# its rows with this lane and reads only its own lane's rows as history (session.py, memory_engine
# .recall_recent_turns lane=): a thread reads its siblings' items, never their
# turns.
CURRENT_WORK_LANE: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "current_work_lane", default=None)
MEMORY_PAUSE_S = 30.0    # a round that found no room waits this long and looks again


def run_owner(run_id, thread: int | None = None) -> str:
    return f"run-{run_id}" if thread is None else f"run-{run_id}-t{thread}"


def _owners_sql(first: int) -> str:
    """The run's own claims, serial or any thread's: `run-<id>` and `run-<id>-t<n>`."""
    return f"(claimed_by = ${first} OR claimed_by LIKE ${first + 1})"


def _sandboxed(pool):
    from mastercode.runtime.task_record import _sandboxed as sb
    return sb(pool)


async def record_mark(pool, entity_id: int, project_id: int):
    """What the record holds, as one value, or None when it is empty: units
    found, units claimed or covered, units covered, distinct findings, and
    each check's status. A turn moved the record when it reached a value the
    run had not seen. Counted by content, not by write time: re-covering a
    covered source, re-claiming one's own claim or setting a check to the
    status it has rewrites a timestamp and adds nothing, and counted as
    progress it would keep a run going forever (a turn after progress is not
    chained, so the 40-turn backstop never applies to it)."""
    return await _sandboxed(pool).fetchval(
        "SELECT CASE WHEN cv.n + f.n + ck.n = 0 THEN NULL ELSE concat_ws('|', "
        "  cv.n, cv.taken, cv.covered, f.n, ck.states) END "
        "FROM (SELECT count(*) AS n, count(*) FILTER (WHERE status <> 'found') AS taken, "
        "             count(*) FILTER (WHERE status = 'covered') AS covered "
        "        FROM task_coverage WHERE entity_id = $1 AND project_id = $2) cv, "
        "     (SELECT count(DISTINCT (source_ref, md5(quote), check_ids)) AS n "
        "        FROM task_findings WHERE entity_id = $1 AND project_id = $2) f, "
        "     (SELECT count(*) AS n, string_agg(id || ':' || status, ',' ORDER BY id) AS states "
        "        FROM goal_checks WHERE entity_id = $1 AND project_id = $2) ck",
        entity_id, project_id)


async def _checks_state(sp, eid, pid) -> dict:
    rows = await sp.fetch("SELECT status, count(*) AS n FROM goal_checks WHERE entity_id = $1 "
                          "AND project_id = $2 GROUP BY status", eid, pid)
    st = {r["status"]: r["n"] for r in rows}
    return {"total": sum(st.values()), "open": st.get("open", 0)}


async def units_per_turn(pool, entity_id: int) -> int:
    """How many sources fit in one turn: half the chat model's window, read a
    page (PAGE_CHARS) at a time; DEFAULT_UNITS when the window is unknown."""
    try:
        from mastercode import context_budget
        from mastercode.inference_config import resolve_role_route
        route = await resolve_role_route(pool, entity_id, "chat") or {}
        win = await context_budget.model_context_window(route.get("model"),
                                                        provider=route.get("provider"))
    except Exception as e:  # noqa: BLE001
        log.debug("task run: window lookup failed (%s) — default units", e)
        win = None
    if not win:
        return DEFAULT_UNITS
    return max(1, min(MAX_UNITS, (int(win) * 4 // 2) // PAGE_CHARS))


def _fence(text: str, run_id, what: str = "task record", source: str = "task_record") -> str:
    from mastercode.runtime.session import agent_output_block
    return agent_output_block(f"run-{run_id}", text, what=what, source=source)


def _quoted(text) -> str:
    """The request, quoted so nothing in it can close the quote or start a
    line of the brief of its own."""
    return json.dumps(str(text or ""), ensure_ascii=False)


async def brief_next(pool, entity_id: int, project_id: int, run: dict,
                     thread: tuple[int, int] | None = None, k: int | None = None) -> str:
    """The next turn's brief: the checks, the coverage, the next sources and
    the findings for the open checks. Not the previous turn's transcript.
    `thread` (n, of): this thread's own slice of the next sources, so threads
    of one round start on different ones (their claims keep them apart)."""
    from mastercode.runtime.task_record import _read
    rec = (await _read(_sandboxed(pool), entity_id, project_id))["result"]
    # A round reads the sources a turn holds once and passes it to every
    # thread's brief, so the slices stay disjoint.
    k = k if k is not None else await units_per_turn(pool, entity_id)
    checks = "\n".join(f"- #{c['id']} [{c['status']}] {c['question']} ({c['findings']} finding(s))"
                       for c in rec["checks"])
    free = [u for u in rec["unread"] if not u.get("claimed_by")]
    if thread is None:
        nxt = rec["unread"][:k]
    else:
        n, of = thread
        nxt = free[n * k:(n + 1) * k]
    # A source's URL and title come from search results: web text, so they
    # enter the brief inside the data fence, with the coverage statement that
    # lists URLs too.
    units = ("\n".join(f"- {u['source']}" + (f" — {u['title']}" if u.get("title") else "")
                       + (f" (claimed by {u['claimed_by']})" if u.get("claimed_by") else "")
                       for u in nxt)
             or "- none: every source found so far is covered")
    units = _fence(f"Coverage: {rec['coverage_statement']}\n\n{units}", run["id"],
                   what="next sources", source="web_search")
    open_ids = {c["id"] for c in rec["checks"] if c["status"] == "open"}
    finds = [f for f in rec["findings"] if open_ids & set(f["checks"])][:BRIEF_FINDINGS]
    held = "\n".join(f"#{f['id']} (checks {', '.join(map(str, f['checks']))}) {f['claim']} — "
                     f"\"{f['quote'][:BRIEF_FINDING_CHARS]}\" ({f['source']}, read {f['read']})"
                     for f in finds)
    stall = ""
    if run["stall_turns"]:
        stall = (f"\n\nThe last {run['stall_turns']} turn(s) wrote nothing to the record; after "
                 f"{stall_max()} in a row the run stops and reports what is open.")
    who = ("one turn at a time" if thread is None else
           f"thread {thread[0] + 1} of {thread[1]} in this round, each thread on its own sources")
    return (
        "[Platform wake-up — not the user typing. Turn "
        f"{run['turns'] + 1} of this task's {'serial' if thread is None else 'parallel'} run: "
        f"{who}, each built from the task record, not from the last turn's transcript.]\n"
        f"The request: {_quoted(run['request'])}\n\n"
        f"Checks:\n{checks}\n\n"
        f"Coverage, and the next sources (about {k} fit in this turn):\n{units}\n\n"
        + (f"What the record holds for the open checks:\n{_fence(held, run['id'])}\n\n" if held else "")
        + "This turn: claim the next sources (task_record op='claim'), read each with read_url, "
        "write a finding for what bears on a check (op='finding', with its checks), and cover "
        "each source (op='covered'). Close a check its findings answer. When no unread source "
        "is left and a check is open, search (web_search or research): what a search returns "
        "becomes the next sources. End the turn when what you claimed is covered; the platform "
        "starts the next one." + stall)


def brief_final(run: dict, *, finished: bool) -> str:
    if finished:
        head = ("[Platform wake-up — not the user typing. Every check of this task is answered "
                "or unanswerable, so its serial run is finished.]")
        body = ("Write the finished work from the record: task_record(op='report') returns the "
                "research document, generated from the record. Write it to the project's "
                "docs/research.md with workspace_file, then answer the request. State the coverage "
                "statement as it is and cite findings by their source and date.")
    else:
        head = ("[Platform wake-up — not the user typing. This task's serial run has stopped: "
                f"{stall_max()} turns in a row wrote nothing to the record.]\n"
                + _stall_hit_line())
        body = ("Report what is still open and why: task_record(op='report') returns the research "
                "document with the open checks and the sources not read. Write it to the project's "
                "docs/research.md with workspace_file, then tell the person what is open, what was "
                "tried, and what would move it (a source, access, or a decision of theirs).")
    return f"{head}\nThe request: {_quoted(run['request'])}\n\n{body}"


async def _load(sp, entity_id, project_id):
    """The project's running run (at most one: uq_task_runs_one_running)."""
    row = await sp.fetchrow("SELECT id, entity_id, project_id, app_id, request, status, turns, "
                            "stall_turns, threads, kind, origin FROM task_runs WHERE entity_id = $1 "
                            "AND project_id = $2 AND status = 'running'", entity_id, project_id)
    return dict(row) if row else None


# ── a run of work ──────────────────────────────────────────────────

WORK_FINDINGS = 5          # each sibling's newest findings in a brief
WORK_CHARS = 300


def _origin(run: dict) -> dict:
    o = run.get("origin")
    if isinstance(o, str):
        try:
            o = json.loads(o)
        except Exception:  # noqa: BLE001
            o = None
    return o if isinstance(o, dict) else {}


async def _items(sp, run_id) -> list[dict]:
    return [dict(r) for r in await sp.fetch(
        "SELECT * FROM task_work_items WHERE run_id = $1 ORDER BY thread_no", run_id)]


async def work_mark(sp, run_id):
    """The run's work items as one value: each item's status, step, and how
    many links and findings it holds. A round moved the run when it reached a
    value the run had not been in."""
    return await sp.fetchval(
        "SELECT string_agg(concat_ws(':', thread_no, status, md5(COALESCE(step, '')), "
        "jsonb_array_length(links), jsonb_array_length(findings)), ',' ORDER BY thread_no) "
        "FROM task_work_items WHERE run_id = $1", run_id)


def item_line(item: dict, of: int) -> str:
    """The one line a thread's finish or blocker puts in the conversation."""
    goal = " ".join(str(item.get("goal") or "").split())
    goal = goal if len(goal) <= 120 else goal[:119].rstrip() + "…"
    said = " ".join(str(item.get("outcome") or "").split())
    head = (f"Thread {item['thread_no'] + 1} of {of} finished: {goal}" if item["status"] == "done"
            else f"Thread {item['thread_no'] + 1} of {of} is blocked: {goal}")
    return (head + (f". {said}" if said else ".")).replace("..", ".")[:600]


async def _run_topic(pool, run_id: int, entity_id: int) -> dict:
    """The run's origin with a Circles topic in it, recorded on the run once.
    There is no original conversation: a run started
    from a turn that named no thread (one in the old original conversation)
    gets one topic, titled from its request, the first time an item's line
    lands. The run row is locked while the topic is made, so items finishing
    at once share that one topic and every later line lands in it."""
    from mastercode.circles_threads import create_thread, home_title
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT origin, request FROM task_runs WHERE id = $1 FOR UPDATE", run_id)
            origin = _origin(dict(row)) if row else {}
            if str(origin.get("thread_id") or "").strip():
                return origin
            th = await create_thread(conn, entity_id,
                                     title=home_title((row or {}).get("request"), "Work run"))
            origin = {**origin, "thread_id": str(th["id"]),
                      "interaction_context": origin.get("interaction_context") or "chat"}
            await conn.execute("UPDATE task_runs SET origin = $2::jsonb WHERE id = $1",
                               run_id, json.dumps(origin))
    return origin


async def post_item_line(pool, item: dict) -> bool:
    """Post a done or blocked item's one line into the conversation its run
    was started from, once: the item is marked posted first, so two writers
    never post it twice, and unmarked again if the line did not land."""
    from mastercode.brain.offline_task_runner import post_to_origin_thread
    sp = _sandboxed(pool)
    took = await sp.fetchval("UPDATE task_work_items SET posted_at = now() WHERE id = $1 "
                             "AND posted_at IS NULL RETURNING id", item["id"])
    if took is None:
        return False
    run = await sp.fetchrow("SELECT id, entity_id, threads, origin FROM task_runs WHERE id = $1",
                            item["run_id"])
    ok = False
    try:
        origin = _origin(dict(run))
        from mastercode.brain.offline_task_runner import lands_in_circles
        if lands_in_circles(origin) and not str(origin.get("thread_id") or "").strip():
            origin = await _run_topic(pool, int(run["id"]), int(run["entity_id"]))
        ok = await post_to_origin_thread(
            pool, run["entity_id"], origin, item_line(item, int(run["threads"] or 1)),
            extra_tags={"source_kind": "work_line", "task_run_id": run["id"],
                        "work_item_id": item["id"], "work_status": item["status"]},
            fallback_session=f"task-run-{run['id']}")
    except Exception as e:  # noqa: BLE001
        log.warning("work item %s: its line did not land: %s", item["id"], e)
    if not ok:
        await sp.execute("UPDATE task_work_items SET posted_at = NULL WHERE id = $1", item["id"])
    return bool(ok)


def brief_work(run: dict, item: dict, items: list[dict]) -> str:
    """An internal thread's brief: its own item, and its siblings' items
    (status, step, findings, links), from the shared record. Never a turn."""
    def _f(it):
        fs = [str((f or {}).get("text") if isinstance(f, dict) else f)[:WORK_CHARS]
              for f in (_jl(it.get("findings")))[-WORK_FINDINGS:]]
        return fs
    of = len(items)
    sibs = []
    for it in items:
        if it["id"] == item["id"]:
            continue
        line = f"- thread {it['thread_no'] + 1}: {it['goal']} [{it['status']}]"
        if it.get("step"):
            line += f" step: {it['step']}"
        for f in _f(it):
            line += f"\n    found: {f}"
        for ln in _jl(it.get("links"))[-WORK_FINDINGS:]:
            line += f"\n    link: {str(ln)[:WORK_CHARS]}"
        sibs.append(line)
    mine = [f"Goal: {item['goal']}", f"Status: {item['status']}"]
    if item.get("step"):
        mine.append(f"Last step: {item['step']}")
    for f in _f(item):
        mine.append(f"Found: {f}")
    for ln in _jl(item.get("links"))[-WORK_FINDINGS:]:
        mine.append(f"Link: {str(ln)[:WORK_CHARS]}")
    stall = ""
    if run["stall_turns"]:
        stall = (f"\n\nThe last {run['stall_turns']} round(s) moved no work item; after {stall_max()} "
                 "in a row the run stops and the items still open are marked blocked.")
    return (
        "[Platform wake-up — not the user typing. Internal thread "
        f"{item['thread_no'] + 1} of {of} of work you started from the conversation, built from "
        "the shared record, not from any thread's transcript.]\n"
        f"The request: {_quoted(run['request'])}\n\n"
        f"Your work item (#{item['id']}):\n{_fence(chr(10).join(mine), run['id'], what='your work item')}\n\n"
        + (f"Your siblings' items (their status and findings; theirs to write, never yours):\n"
           f"{_fence(chr(10).join(sibs), run['id'], what='sibling work items')}\n\n" if sibs else "")
        + "This turn: work your item. As you go, write the step you are on (task_record op='item', "
        "step=...), each link you make (link=...) and what you find (note=...). When it is done, "
        "op='item' status='done' with line= one line for the conversation; when you cannot go on, "
        "status='blocked' with line= what is needed. That line is posted into the conversation. "
        "Leave your siblings' items to them; what they found is above." + stall)


def _jl(v) -> list:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:  # noqa: BLE001
            return []
    return v if isinstance(v, list) else []


async def _drive_work(runtime, pool, sp, run: dict, state: dict) -> None:
    """One round of a run of work: the threads whose items are open or
    working, side by side, as many as the Host's memory holds."""
    run_id = run["id"]
    items = await _items(sp, run_id)
    live = [i for i in items if i["status"] in ("open", "working")]
    if not live:
        await _end(sp, run_id, "finished", "every work item is done or blocked")
    elif run["stall_turns"] >= stall_max():
        await _end(sp, run_id, "stalled", f"{stall_max()} rounds in a row moved no work item")
        await sp.execute(
            "UPDATE task_work_items SET status = 'blocked', updated_at = now(), "
            "outcome = COALESCE(outcome, $2) WHERE run_id = $1 AND status IN ('open', 'working')",
            run_id, f"stopped: {stall_max()} rounds in a row moved nothing in its record "
                    "(the person's limit task_run.stall_turns)")
        from mastercode.runtime import user_limits
        await user_limits.record_hit(pool, run.get("entity_id"), "task_run.stall_turns",
                                     f"task run {run_id}")
    if not live or run["stall_turns"] >= stall_max():
        for it in await _items(sp, run_id):
            if it["status"] in ("done", "blocked") and it["posted_at"] is None:
                await post_item_line(pool, it)
        return
    before = await work_mark(sp, run_id)
    state["seen"].add(before)
    resting = state["resting"]
    order = [i for i in live if i["thread_no"] not in resting] + [i for i in live if i["thread_no"] in resting]
    width, mem = round_width({"threads": len(order)}, set())
    if width < 1:
        log.info("work run %s: the Host's memory holds no thread turn now (%s) — waiting %.0fs",
                 run_id, mem, MEMORY_PAUSE_S)
        await asyncio.sleep(MEMORY_PAUSE_S)
        return
    take = order[:width]
    await sp.execute(
        "UPDATE task_work_items SET status = 'working', step = COALESCE(step, 'starting'), "
        "updated_at = now() WHERE run_id = $1 AND thread_no = ANY($2::int[]) AND status = 'open'",
        run_id, [i["thread_no"] for i in take])
    items = await _items(sp, run_id)
    by_no = {i["thread_no"]: i for i in items}
    results = await asyncio.gather(*(
        _wake(runtime, run, brief_work(run, by_no[i["thread_no"]], items), thread=i["thread_no"],
              chain=run["stall_turns"] > 0,
              display=f"(work: thread {i['thread_no'] + 1} of {len(items)} — {str(i['goal'])[:60]})")
        for i in take))
    state["resting"] = {i["thread_no"] for i, ok in zip(take, results) if not ok}
    # A thread that set its item done or blocked without its line landing
    # (the post failed, or it wrote no line): posted now, once.
    for it in await _items(sp, run_id):
        if it["status"] in ("done", "blocked") and it["posted_at"] is None:
            await post_item_line(pool, it)
    if not any(results):
        state["skips"] += 1
        if state["skips"] >= skip_max():
            await _end(sp, run_id, "stopped",
                       "the platform could not start or finish a turn: the background budget "
                       "stayed full, or the turn failed (a provider limit, an error)")
            await _skip_stop_said(pool, run)
            return
        await asyncio.sleep(SKIP_PAUSE_S)
        return
    state["skips"] = 0
    after = await work_mark(sp, run_id)
    moved = after is not None and after not in state["seen"]
    state["seen"].add(after)
    await sp.execute(
        "UPDATE task_runs SET turns = turns + 1, "
        "stall_turns = CASE WHEN $2 THEN 0 ELSE stall_turns + 1 END, updated_at = now() "
        "WHERE id = $1 AND status = 'running'", run_id, moved)


async def _end(sp, run_id, status: str, reason: str) -> None:
    """Mark the run ended and release its claims in one transaction, so no
    reader ever sees the run ended while it still holds a claim. A stalled or
    finished run wakes one last turn, and that turn can claim again; a person
    who saw the run end and started the next one on the same source would be
    told it is claimed by this run. A statement that fails rolls both back
    and raises: the run is still running with its claims, start()'s failure
    path ends it again, and if that fails too, resume_running drives it after
    the next restart. Never ended with a claim left held."""
    async with sp.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE task_runs SET status = $1, end_reason = $2, ended_at = now(), "
                "updated_at = now() WHERE id = $3 AND status = 'running'", status, reason, run_id)
            # Released whether or not the UPDATE matched (op='stop' may have
            # ended the run first). Once this commits, task_record._claim
            # refuses the run any further claim.
            await release_claims(conn, run_id)


async def release_claims(conn, run_id) -> None:
    """Release the run's own claims, serial or any thread's, on `conn`: the
    connection of the transaction that ends the run (_end here, the Host's
    op='stop' in task_record._stop). The run's last turn may have claimed and
    not covered: a claim outlives no run, or the next run's first turn could
    never take the unit. Only the run's own claims: a person's claim in the
    project is not the run's."""
    await conn.execute(
        "UPDATE task_coverage c SET status = 'found', claimed_by = NULL, claimed_at = NULL "
        "FROM task_runs r WHERE r.id = $1 AND c.entity_id = r.entity_id "
        "AND c.project_id = r.project_id AND c.status = 'claimed' AND " + _owners_sql(2),
        run_id, run_owner(run_id), run_owner(run_id) + "-t%")


async def _wake(runtime, run: dict, brief: str, *, display: str, chain: bool,
                thread: int | None = None) -> bool:
    token = CURRENT_TASK_RUN.set(run["id"])
    ttoken = CURRENT_TASK_THREAD.set(thread)
    ltoken = CURRENT_WORK_LANE.set(f"task-{run['id']}-t{thread}" if thread is not None else None)
    try:
        landed = await runtime._wake_host_turn(
            entity_id=run["entity_id"], app_id=run["app_id"], project_id=run["project_id"],
            thread_id=None, brief=brief, origin_request=run["request"] or "",
            display_message=display, anchor=f"task-run-{run['id']}", title="task turn",
            ok=True, chain=chain, research=False,
            **({"lane": f"task-{run['id']}-t{thread}"} if thread is not None else {}))
    finally:
        CURRENT_WORK_LANE.reset(ltoken)
        CURRENT_TASK_THREAD.reset(ttoken)
        CURRENT_TASK_RUN.reset(token)
    return bool(landed)


def round_width(run: dict, resting: set) -> tuple[int, dict]:
    """How many thread turns this round starts: the width the Host asked for,
    less threads sitting out a round, or as many as the Host's memory holds
    now if that is fewer (host_memory.turns_that_fit). No count of its own."""
    from mastercode.runtime import host_memory
    want = max(0, int(run.get("threads") or 1) - len(resting))
    fit = host_memory.turns_that_fit()
    return (want if fit is None else min(want, fit)), host_memory.facts()


async def _drive(runtime, pool, entity_id: int, project_id: int, current: dict) -> None:
    """Drive the project's running run, whichever it is: a run stopped and a
    new one started while this loop is inside a turn is the next one this loop
    drives (start() finds the loop alive and leaves it to it). The loop ends
    when the project has no running run and none was started meanwhile."""
    sp = _sandboxed(pool)
    key = (entity_id, project_id)
    skips, seen, run_id, resting = 0, set(), None, set()
    work: dict = {}
    while True:
        _WANTED.discard(key)
        run = await _load(sp, entity_id, project_id)
        if run is None:
            if key in _WANTED:      # a run was started while this one loaded
                continue
            return
        if run["id"] != run_id:
            skips, seen, run_id, resting = 0, set(), run["id"], set()
            work = {"skips": 0, "seen": set(), "resting": set()}
        current["run"] = run_id
        if run.get("kind") == "work":
            await _drive_work(runtime, pool, sp, run, work)
            continue
        eid, pid = entity_id, project_id
        st = await _checks_state(sp, eid, pid)
        if st["total"] and not st["open"]:
            await _wake(runtime, run, brief_final(run, finished=True),
                        display="(task: every check closed — writing the finished work)", chain=False)
            await _end(sp, run_id, "finished", "every check is answered or unanswerable")
            continue
        if run["stall_turns"] >= stall_max():
            from mastercode.runtime import user_limits
            await user_limits.record_hit(pool, eid, "task_run.stall_turns", f"task run {run_id}")
            await _wake(runtime, run, brief_final(run, finished=False),
                        display="(task: stopped — nothing new in the record)", chain=False)
            await _end(sp, run_id, "stalled", f"{stall_max()} turns in a row wrote nothing to the record")
            continue
        before = await record_mark(pool, eid, pid)
        seen.add(before)
        if int(run.get("threads") or 1) > 1:
            # A parallel round: as many thread turns as asked for and as fit.
            # Every thread sat out (all failed last round, after the pause the
            # failure path took): they all try again.
            if len(resting) >= int(run["threads"]):
                resting = set()
            width, mem = round_width(run, resting)
            if width < 1:
                log.info("task run %s: the Host's memory holds no thread turn now (%s) — "
                         "waiting %.0fs", run_id, mem, MEMORY_PAUSE_S)
                resting.clear()
                await asyncio.sleep(MEMORY_PAUSE_S)
                continue
            ids = [t for t in range(int(run["threads"])) if t not in resting][:width]
            k = await units_per_turn(pool, eid)
            briefs = [await brief_next(pool, eid, pid, run, thread=(i, len(ids)), k=k)
                      for i in range(len(ids))]
            results = await asyncio.gather(*(
                _wake(runtime, run, briefs[i], thread=t, chain=run["stall_turns"] > 0,
                      display=f"(task: turn {run['turns'] + 1}, thread {t + 1} of {run['threads']})")
                for i, t in enumerate(ids)))
            # A thread whose turn did not land sits out the next round.
            resting = {t for t, ok in zip(ids, results) if not ok}
            landed = any(results)
        else:
            landed = await _wake(runtime, run, await brief_next(pool, eid, pid, run),
                                 display=f"(task: turn {run['turns'] + 1})",
                                 chain=run["stall_turns"] > 0)
        # A claim is a lease on the turn that made it: every turn of this
        # round has returned, landed or not, so what it claimed and did not
        # cover goes back to found for the next turn. A turn that
        # did not land included: a failed thread's claims would keep the next
        # round's slices off those sources. (A run stopped while its turn ran
        # has no claim here: op='stop' released them, and task_record._claim
        # refuses one after.) Released before the record is measured, so a
        # turn that only claimed is not progress.
        await sp.execute(
            "UPDATE task_coverage SET status = 'found', claimed_by = NULL, claimed_at = NULL "
            "WHERE entity_id = $1 AND project_id = $2 AND status = 'claimed' AND " + _owners_sql(3),
            eid, pid, run_owner(run_id), run_owner(run_id) + "-t%")
        if not landed:
            skips += 1
            if skips >= skip_max():
                await _end(sp, run_id, "stopped",
                           "the platform could not start or finish a turn: the background budget "
                           "stayed full, or the turn failed (a provider limit, an error)")
                await _skip_stop_said(pool, run)
                continue
            await asyncio.sleep(SKIP_PAUSE_S)
            continue
        skips = 0
        after = await record_mark(pool, eid, pid)
        # Progress is a state of the record this run has not been in: a check
        # set answered and back to open, or any other round trip, is not.
        moved = after is not None and after not in seen
        seen.add(after)
        await sp.execute(
            "UPDATE task_runs SET turns = turns + 1, "
            "stall_turns = CASE WHEN $2 THEN 0 ELSE stall_turns + 1 END, updated_at = now() "
            "WHERE id = $1 AND status = 'running'", run_id, moved)


def start(runtime, pool, entity_id: int, project_id: int, run_id: int | None = None) -> bool:
    """Drive the project's running run unless this process already drives the
    project. False when it does: that loop takes up the running run next,
    whichever it is (a run started while it is inside a turn included)."""
    key = (entity_id, project_id)
    t = _DRIVERS.get(key)
    if t is not None and not t.done():
        _WANTED.add(key)
        return False
    current: dict = {"run": run_id}

    async def _go():
        try:
            await _drive(runtime, pool, entity_id, project_id, current)
        except Exception as e:  # noqa: BLE001 — the run row says what happened
            log.exception("task run %s failed", current.get("run"))
            try:
                if current.get("run") is not None:
                    await _end(_sandboxed(pool), current["run"], "stopped",
                               f"the run failed: {e}"[:500])
            except Exception:  # noqa: BLE001
                pass
        finally:
            if _DRIVERS.get(key) is task:
                _DRIVERS.pop(key, None)

    task = asyncio.create_task(_go())
    _DRIVERS[key] = task
    return True


def driving(entity_id: int, project_id: int) -> bool:
    t = _DRIVERS.get((entity_id, project_id))
    return t is not None and not t.done()


async def resume_running(runtime, pool, entity_id: int) -> int:
    """At boot: drive every run still marked running (the process that drove it
    is gone). Returns how many were resumed."""
    rows = await _sandboxed(pool).fetch(
        "SELECT id, project_id FROM task_runs WHERE entity_id = $1 AND status = 'running'", entity_id)
    n = 0
    for r in rows:
        if start(runtime, pool, entity_id, r["project_id"], r["id"]):
            n += 1
    if n:
        log.info("task runs resumed after restart: %d", n)
    return n
