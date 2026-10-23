#!/usr/bin/env python3
"""Gate for the host's own timer landing where it was promised.

A build had just finished and the Host, woken by hand, saw the bot was still
booting and wanted to check later. The Host can set a timer for itself, and
that timer is visible.

Measured before the change, on entity 9128:

  - The timer EXISTS. `schedule_task` op='create' writes a calendar_events
    row; brain/offline_task_runner.py polls every 30s, claims a due row and
    runs its prompt through the governed turn pipeline. The tool is offered
    on this surface (public_tool_registry _meta.surfaces includes
    "workbench").
  - The host had never used it. One scheduled_task row in the entity's whole
    life, and it is the platform's own weekly changelog — not one the host
    chose to set.
  - Where a fired task reported was HARDCODED: write_turn_row with
    tags={"app_id": "circles"} and no project_id. So a timer promised in a
    Workbench project thread answered into the Circles self-thread — a
    different room from the one the user was watching. `related_project_id`
    has been a column on calendar_events the whole time and nothing wrote it.

So: the origin rides the create (payload.origin + related_project_id), and
the runner reports through it. A timer owes its answer to the conversation
that asked for it.

Run: APP_ROOT=<repo> python3 scripts/test_timer_origin.py
"""
import re
import os
import sys
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it
from tools_source import tools_source  # tools.py and the modules split out of it

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[timer] {'ok  ' if cond else 'FAIL'} {label}"
          f"{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


tools_src = tools_source(_ROOT)
runner_src = (_ROOT / "brain/mastercode/brain/offline_task_runner.py").read_text()
route_src = (_ROOT / "server/routes/agents.py").read_text()
api_src = (_ROOT / "workbench-web/src/lib/api.ts").read_text()
ops_src = (_ROOT / "workbench-web/src/components/workbench/ops-pane.tsx").read_text()
handler_src = (_ROOT / "server/bridge/memory_handlers.py").read_text()
session_src = turn_source(_ROOT)
direct_src = (_ROOT / "brain/mastercode/runtime/direct_ops.py").read_text()
bs_src = (_ROOT / "server/brain_service.py").read_text()

# ── 1. the origin reaches the row ───────────────────────────────────────────
# The FIRST cut of this gate string-matched the chat dispatcher's call and
# went green on a path schedule_task never takes: create is a WRITE, so it
# arrives through `operate` -> run_direct_op, which passes
# interaction_context=None on purpose (found by tracing the call graph).
# So this gate now EXECUTES the door the host actually uses and reads
# the row that comes out.
check("operate hands the calling thread to run_direct_op",
      "origin_context=getattr(" in session_src
      and 'sess, "interaction_context", None)' in session_src)
check("run_direct_op forwards it WITHOUT touching the surface gate",
      "origin_context=origin_context," in direct_src
      and "interaction_context=None," in direct_src)
check("...and the meta-tool dispatcher prefers it over the chat context",
      "interaction_context=(origin_context or interaction_context)" in tools_src)
check("...and the handler accepts it",
      "async def _exec_schedule_task(args: dict, *, session_id: str | None = None,"
      in tools_src)
check("create writes related_project_id (the column existed and nothing set it)",
      '"related_project_id": _origin_project,' in tools_src)
check("...and stores the origin thread in the task payload",
      '_body["origin"] = _origin' in tools_src)
check("the project id is parsed from the interaction_context, not guessed",
      'if _ic.startswith("project:")' in tools_src)
check("the tool result says WHERE the answer will appear",
      '"reports_into"' in tools_src)
check("handle_calendar_append really does accept related_project_id",
      'payload.get("related_project_id")' in handler_src)

# ── 2. the runner reports THERE, not into Circles by default ────────────────
check("the report is no longer hardcoded to circles",
      'tags={"app_id": "circles", "scheduled_task_id": event_id}' not in runner_src)
# Derived from the interaction context, not from "is there a project": the
# Workbench GENERAL thread is interaction_context="workbench" with a NULL
# project and reads back as app_id=workbench, so keying on the project alone
# sent a General-thread timer to Circles. The behaviour
# itself is exercised in section 4 below; this holds the mechanism.
check("the report room is derived from the thread, not from project presence",
      'if _project_id is not None or _ic.startswith("project:")' in runner_src)
check("...with the source_context the project chat pane reads back",
      'derive_source_context(' in runner_src and 'project_id=_project_id' in runner_src)
check("...and the turn carries project_id",
      "project_id=_project_id," in runner_src)
check("no origin still lands in Circles (a topic of its own, ensure_origin_thread)",
      'else "circles"' in runner_src)
check("the fire time is stamped as the utterance time, not the insert clock",
      "created_at=datetime.now(timezone.utc)," in runner_src)
# EVERY reporting call passes the origin — read off the AST, so the check is
# "no call site omits the keyword" rather than a count of one exact literal.
# Two rewrites got us here and each fixed a real brittleness:
#   - It was `count('origin=payload.get("origin")') == 2`. A count of call
#     sites is not the property: the turn-spend ceiling added a third, correct
#     one and turned a green check red with nothing wrong.
#   - Matching that exact source text is also wrong: a call site that passed
#     `origin=_origin_dict` would be correct and would still fail. The property
#     is that the keyword is THERE, whatever expression fills it.
import ast as _ast

_report_calls = [
    n for n in _ast.walk(_ast.parse(runner_src))
    if isinstance(n, _ast.Call)
    and isinstance(n.func, _ast.Name) and n.func.id == "_report_and_finalize"
]
_with_origin = [c for c in _report_calls
                if any(k.arg == "origin" for k in c.keywords)]
check("EVERY report call passes the origin (a failing standing op reports too)",
      len(_report_calls) >= 2 and len(_report_calls) == len(_with_origin),
      f"{len(_report_calls)} call site(s), {len(_with_origin)} with origin="
      + (f" — missing at line(s) "
         + ", ".join(str(c.lineno) for c in _report_calls
                     if c not in _with_origin)
         if len(_with_origin) != len(_report_calls) else ""))

# ── 3. the time is VISIBLE where the promise was made ───────────────────────
check("the scheduled route exposes which project a timer belongs to",
      '"project_id": r["related_project_id"],' in route_src
      and "e.related_project_id," in route_src)
check("the workbench can read pending timers",
      "export function fetchScheduled(" in api_src)
check("...and cancel one",
      "export function cancelScheduled(" in api_src)
check("the bench polls them alongside runs and agents",
      "fetchScheduled()," in ops_src)
check("...shows this project's timers plus ONE-SHOT un-scoped ones",
      "t.project_id == null ? !t.recurrence : t.project_id === projectId"
      in ops_src)
# Verified against a live pod: entity 9128's only scheduled
# task is the platform's standing weekly changelog, related_project_id NULL.
# Without the recurrence test it would render a "Coming back to this" card on
# every project bench, for a job that has nothing to do with the project.
check("...so a standing platform job does not follow you onto every bench",
      "!t.recurrence" in ops_src)
check("...renders the actual fire TIME, which is the whole ask",
      "checks back at" in ops_src and "toLocaleTimeString()" in ops_src)
check("...and an overdue timer says so instead of showing a passed time",
      "running shortly" in ops_src)
check("the bench section is rendered, not just computed",
      "<TimerCard" in ops_src and "Coming back to this" in ops_src)


# ── 4. EXECUTE the path, don't just match strings on it ─────────────────────
# The whole reason this section exists: the first cut of this gate matched a
# dispatcher call and passed while the feature did nothing.
import asyncio  # noqa: E402
import sys as _sys  # noqa: E402
import types  # noqa: E402

_sys.path.insert(0, str(_ROOT / "brain"))
_sys.path.insert(0, str(_ROOT))


class _Pool:
    async def fetchrow(self, *a, **k):
        return None

    async def fetchval(self, *a, **k):
        return None

    async def fetch(self, *a, **k):
        return []

    async def execute(self, *a, **k):
        return "OK"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# 4a. create derives the origin from the thread it was called in.
from mastercode.runtime import tools as _tools  # noqa: E402
from server import brain_service as _bs  # noqa: E402
import server.bridge.memory_handlers as _mh  # noqa: E402

_bs.get_pool = lambda: _Pool()
_bs._entity_id = 9128
_seen: dict = {}


async def _fake_append(payload):
    _seen.clear()
    _seen.update(payload)
    return {"ok": True, "event_id": "evt-1", "event_at": payload.get("event_at")}


_mh.handle_calendar_append = _fake_append

# The plan is written into the thread at create (turn types step 5); record
# where, and restore the real writer before the report checks below.
from mastercode.brain import offline_task_runner as _otr_plan  # noqa: E402
_plan_posts: list = []
_real_post = _otr_plan.post_to_origin_thread


async def _rec_post(pool, eid, origin, text, **kw):
    _plan_posts.append(origin)
    return True
_otr_plan.post_to_origin_thread = _rec_post

res = _run(_tools._exec_schedule_task(
    {"op": "create", "title": "check the bot", "fire_at": "2026-09-04T14:00:00Z",
     "prompt": "read /app/status for trading-bot and say if it booted"},
    session_id="sess-abc", interaction_context="project:1"))
check("EXECUTED: a timer set in project 1 records related_project_id=1",
      _seen.get("related_project_id") == 1, repr(_seen.get("related_project_id")))
check("EXECUTED: ...and the origin thread rides the payload",
      (_seen.get("payload") or {}).get("origin", {}).get("project_id") == 1
      and (_seen.get("payload") or {}).get("origin", {}).get("session_id") == "sess-abc",
      repr((_seen.get("payload") or {}).get("origin")))
check("EXECUTED: ...and the host is told which thread will answer",
      (res.get("result") or {}).get("reports_into") == "project 1 thread",
      repr(res.get("result")))
check("EXECUTED: ...and the plan is written into that same thread now",
      _plan_posts and _plan_posts[-1] == (_seen.get("payload") or {}).get("origin"),
      repr(_plan_posts))

# A Circles-origin timer keeps the old destination.
_run(_tools._exec_schedule_task(
    {"op": "create", "title": "t", "fire_at": "2026-09-04T14:00:00Z", "prompt": "p"},
    session_id="s2", interaction_context="circles"))
check("EXECUTED: a Circles-origin timer records no project",
      _seen.get("related_project_id") is None)
_otr_plan.post_to_origin_thread = _real_post

# 4b. the report lands in the thread the origin names.
from mastercode.brain import offline_task_runner as _otr  # noqa: E402

_wrote: list = []


async def _fake_write(pool, entity_id, session_id, role, content, **kw):
    _wrote.append({"session_id": session_id, "content": content, **kw})
    return 1


import mastercode.memory_engine as _me  # noqa: E402
_me.write_turn_row = _fake_write
_otr._record_run = lambda *a, **k: asyncio.sleep(0)
_otr._finalize = lambda *a, **k: asyncio.sleep(0)

_wrote.clear()
_run(_otr._report_and_finalize(
    _Pool(), 128, 7, "check the bot", None, None, True, "it booted clean", None,
    report_on_success=True,
    origin={"project_id": 1, "session_id": "sess-abc",
            "interaction_context": "project:1"}))
check("EXECUTED: the fired report is written INTO the project thread",
      bool(_wrote) and _wrote[0].get("project_id") == 1
      and (_wrote[0].get("tags") or {}).get("app_id") == "workbench",
      repr(_wrote[:1]))
check("EXECUTED: ...with the source_context the project pane reads back",
      bool(_wrote) and (_wrote[0].get("tags") or {}).get("source_context")
      == {"kind": "project", "ref": "1"})

# The Workbench GENERAL thread: app_id workbench, project NULL. Keying on the
# project alone sent this to Circles.
_wrote.clear()
_run(_otr._report_and_finalize(
    _Pool(), 128, 8, "t", None, None, True, "done", None,
    report_on_success=True,
    origin={"session_id": "s3", "interaction_context": "workbench"}))
check("EXECUTED: a General-thread timer reports to workbench, not Circles",
      bool(_wrote) and (_wrote[0].get("tags") or {}).get("app_id") == "workbench"
      and _wrote[0].get("project_id") is None, repr(_wrote[:1]))

# No origin at all — the platform's own standing tasks, one set in the old
# original conversation. There is no original conversation any more:
# the report starts a topic of its own, titled from the task,
# and the task records it so every later run lands there too.
import mastercode.circles_threads as _ctm  # noqa: E402
_TOPIC = "11111111-2222-4333-8444-555555555555"
_homes: list = []
_home_answer = {"id": _TOPIC}


async def _fake_home(pool, eid, thread_id, *, title):
    _homes.append((thread_id, title))
    return _home_answer["id"]
_ctm.home_thread = _fake_home


class _RecPool(_Pool):
    def __init__(self):
        self.executed: list = []

    async def execute(self, *a, **k):
        self.executed.append(a)
        return "UPDATE 1"


_wrote.clear()
_rp = _RecPool()
_run(_otr._report_and_finalize(
    _rp, 128, 9, "weekly changelog", None, None, True, "done", None,
    report_on_success=True, origin=None))
check("EXECUTED: a standing task with no origin reports into a topic of its own",
      bool(_wrote) and (_wrote[0].get("tags") or {}).get("app_id") == "circles"
      and (_wrote[0].get("tags") or {}).get("source_context") == {"kind": "thread", "ref": _TOPIC}
      and _wrote[0].get("project_id") is None, repr(_wrote[:1]))
check("EXECUTED: ...the topic is titled from the task, not from the report",
      _homes and _homes[-1] == (None, "weekly changelog"), repr(_homes))
check("EXECUTED: ...and the task keeps it: its origin records the topic",
      any("UPDATE calendar_events" in str(a[0]) and a[1] == 9 and _TOPIC in str(a[3])
          for a in _rp.executed if len(a) >= 4), repr(_rp.executed))
# Negative control: no topic could be made (the database refused it). Nothing
# is written, rather than a row in the legacy app:circles scope.
_wrote.clear()
_home_answer["id"] = None
_run(_otr._report_and_finalize(
    _RecPool(), 128, 10, "weekly changelog", None, None, True, "done", None,
    report_on_success=True, origin=None))
check("EXECUTED: with no topic to be had, nothing lands in the old original conversation",
      not _wrote, repr(_wrote[:1]))
_home_answer["id"] = _TOPIC
# A task whose origin already names a live topic keeps it: no new one.
_wrote.clear()
_homes.clear()
_run(_otr._report_and_finalize(
    _RecPool(), 128, 11, "t", None, None, True, "done", None,
    report_on_success=True, origin={"interaction_context": "chat", "thread_id": _TOPIC}))
check("EXECUTED: a task set in a topic reports there and starts no other",
      bool(_wrote) and (_wrote[0].get("tags") or {}).get("source_context")
      == {"kind": "thread", "ref": _TOPIC} and _homes == [], repr((_wrote[:1], _homes)))


# ── 5. THE CHAIN. The scenario has a second hop ─────────────────────────────
# "check it later" fires, the first check finds the app STILL booting, the
# host says it will look again — and that second timer has to land in the
# same thread as the first. Before this the fired turn ran under
# app_id="scheduled_task" with no project, so the follow-up inherited nothing
# and answered in Circles while the user waited in the project thread.
# The fired turn stays on the HIDDEN write lane and only THINKS in the origin
# thread. Running it as app_id=workbench/project_id=N (the first cut) put it on
# the normal chat write path, which wrote the timer's own prompt into the
# project thread as a USER message and the reply twice.
check("the fired turn keeps the hidden scheduled_task write tag",
      'app_id="scheduled_task",' in runner_src
      and "project_id=_origin_project," not in runner_src)
check("...and carries the origin as history_context instead",
      'history_context=(_origin.get("interaction_context") or None)' in runner_src)
check("...and is a system turn, so no user row is written for the brief",
      "system_turn=True," in runner_src)
check("history_context is actually exposed on the send_message path",
      "history_context: Optional[str] = None," in bs_src
      and "history_context=history_context," in bs_src)
check("...so a timer the fired turn sets inherits the same origin",
      '_origin.get("interaction_context")' in runner_src)
check("the op lane carries it too (schedule_task is itself a schedulable op)",
      "origin_context=origin_context, turn_type=turn_type)" in runner_src
      and "async def _run_op(pool, entity_id: int, tool: str, args: dict,"
      in runner_src)

# EXECUTED: the fired turn is handed the project.
_sent: dict = {}


async def _fake_send(**kw):
    _sent.clear()
    _sent.update(kw)
    for f in ('0:"booted clean"',):
        yield f


_bs.send_message = _fake_send
_bs.get_runtime = lambda: object()
_otr._report_and_finalize = lambda *a, **k: asyncio.sleep(0)

_run(_otr._run_one(_Pool(), 128, {
    "id": 11, "title": "check the bot", "recurrence": None, "start_time": None,
    "payload": {"prompt": "did it boot?",
                "origin": {"project_id": 1, "session_id": "s",
                           "interaction_context": "project:1"}},
}))
check("EXECUTED: the fired turn thinks in the project thread, writes hidden",
      _sent.get("history_context") == "project:1"
      and _sent.get("app_id") == "scheduled_task"
      and _sent.get("project_id") is None
      and _sent.get("system_turn") is True,
      repr({k: _sent.get(k) for k in
            ("app_id", "project_id", "history_context", "system_turn")}))

# THE REGRESSION THAT SHIPPED GREEN: only the project origin was exercised,
# and the project origin was the one that worked. A Circles or General-thread
# timer fired as app_id="scheduled_task" with ic "scheduled_task", so the
# SECOND hop reported into a room no reader accepts — worse than main, where
# it landed in Circles.
for label, ic, want in (("Circles", "chat", "chat"),
                        ("Workbench General", "workbench", "workbench")):
    _run(_otr._run_one(_Pool(), 128, {
        "id": 13, "title": "t", "recurrence": None, "start_time": None,
        "payload": {"prompt": "p", "origin": {"session_id": "s",
                                              "interaction_context": ic}},
    }))
    check(f"EXECUTED: a {label} timer's fired turn keeps its own context",
          _sent.get("history_context") == want
          and _sent.get("app_id") == "scheduled_task",
          repr({k: _sent.get(k) for k in ("app_id", "history_context")}))

# A standing task with no origin keeps the hidden scheduled_task lane, and its
# turn now thinks in a topic of its own rather than in no conversation at all.
_run(_otr._run_one(_Pool(), 128, {
    "id": 12, "title": "weekly", "recurrence": "weekly", "start_time": None,
    "payload": {"prompt": "digest"},
}))
check("EXECUTED: an origin-less standing task's turn runs in a topic of its own",
      _sent.get("project_id") is None
      and _sent.get("app_id") == "scheduled_task"
      and _sent.get("thread_id") == _TOPIC
      and _sent.get("history_context") == "chat",
      repr({k: _sent.get(k) for k in ("app_id", "project_id", "thread_id", "history_context")}))


# ── 6. The ROW the fired turn writes ────────────────────────────────────
# The kwargs test above cannot see this: it stubs send_message entirely. The
# blocker lived one layer deeper. session.py derives project_id from the
# session's interaction_context when the caller passes none — correct for a
# real project chat, and wrong for a BORROWED context, because it stamped the
# fired timer's own reply with project_id=N. The project reader matches on
# em.project_id with no app_id exclusion (routes/brain.py), so that reply
# rendered in the thread beside the report the runner writes: the duplicate
# the hidden lane exists to prevent, through a new door.
sess_src = turn_source(_ROOT)
check("a borrowed context is recorded on the session",
      "context_borrowed: bool = False" in sess_src
      and "context_borrowed=_context_borrowed," in sess_src)
# The derivation lives in transient_interaction_context (one function, shared
# with the identity hold's saved-request rows); the builder takes both values
# from it, so a borrowed context is still set exactly where history_context
# overrides the context.
_borrow_marker = ("    if history_context:\n"
                  "        return history_context, True\n")
check("...set exactly where history_context overrides the context",
      _borrow_marker in sess_src
      and "interaction_context, _context_borrowed = transient_interaction_context(" in sess_src)
check("...and the turn ROWS drop the borrowed project",
      "_row_project_id = None if sess.context_borrowed else project_id" in sess_src)
# 4 → 5: publish_turn_trace is the fifth site. It is not itself a
# row write, but the project it publishes is what turn_registry stamps on the
# row a STOPPED turn leaves behind — so a borrowed context has to be dropped
# there for exactly the same reason, and the count is what keeps a sixth site
# from arriving without one.
# 5 → 8: a turn that holds writes its
# row before the hold and rewrites it in place at the end (held_turn_write, two
# sites), and notes the hold on its live trace (note_turn_held) — each drops a
# borrowed project the same way.
import ast as _ast_t
# The turn's body: send_message, the core it hands the turn to
# (_send_message_core, since the saved-request work split them), and the
# step that writes the turn's opening row (_write_opening_row).
_sm_src = "".join(_ast_t.get_source_segment(sess_src, n) for n in _ast_t.walk(_ast_t.parse(sess_src))
                  if isinstance(n, _ast_t.AsyncFunctionDef)
                  and n.name in ("send_message", "_send_message_core", "_write_opening_row"))
# ... and the turn's reply recorder (TurnReplies), which writes the live trace
# from the first text; it reads the row's project through the getter the turn
# hands it, so the borrowed context is dropped there the same way.
_rec_src = "".join(_ast_t.get_source_segment(sess_src, n) for n in _ast_t.walk(_ast_t.parse(sess_src))
                   if isinstance(n, _ast_t.ClassDef) and n.name == "TurnReplies")
check("...at EVERY write site, not just the assistant row",
      # 8 -> 9: the words a person sends into a
      # running turn are written as a user row when the turn reads them.
      # 9 -> 11: the live trace carries what the turn
      # sent from its first text (note_turn_reply), and an escalate made
      # outside the per-call loop publishes the trace too (_box_call) — the
      # project those name is what a stopped turn's rows are filed under.
      # 11 -> 12: crisis words during a hold rewrite
      # the turn's held row in place (held_turn_write) instead of a second row.
      # 12 -> 9: a crisis-check hit writes no row of its own any
      # more — no referral row at the turn's start, none on a nudge (held or
      # not); the turn's own rows carry what it said.
      # 9 -> 10: the request that opens a root turn
      # (turn_type op='root') is written as a user row of the Root thread.
      # (counted from a word start: the turn hands _row_project_id=_row_project_id
      # to the opening-row step, which is not a write site)
      len(re.findall(r"(?<![\w])project_id=_row_project_id", _sm_src))
      + _rec_src.count("project_id=self.row_project_id()") == 10
      and _rec_src.count("project_id=self.row_project_id()") == 1
      and "project_id=project_id, tags=" not in _sm_src + _rec_src)
# The opening-row step's two write sites read its _row_project_id parameter, so
# the count above holds only if the turn hands it the turn's own
# _row_project_id: a call passing project_id would file the person's row under
# the borrowed project with every write site unchanged.
_opening_calls = [n for n in _ast_t.walk(_ast_t.parse(sess_src))
                  if isinstance(n, _ast_t.Call) and isinstance(n.func, _ast_t.Attribute)
                  and n.func.attr == "_write_opening_row"]
check("...and the turn hands the opening-row step its own _row_project_id",
      len(_opening_calls) == 1
      and [_ast_t.unparse(k.value) for k in _opening_calls[0].keywords
           if k.arg == "_row_project_id"] == ["_row_project_id"])
# Recall scope and the dispatch origin must still follow the borrowed thread —
# the point is to think there, not to be cut off from it.
_recall_marker = ("_src_ctx = derive_source_context(\n"
                  "            sess.interaction_context,")
check("...while recall scope still follows the borrowed thread",
      _recall_marker in sess_src)

# EXECUTED: the dataclass really takes the field and defaults safely.
from mastercode.runtime.session import EntitySession as _ES  # noqa: E402
_plain = _ES(session_id="s", entity_id=1, entity={})
check("EXECUTED: sessions default to NOT borrowed, so nothing else changes",
      _plain.context_borrowed is False)
check("EXECUTED: and a borrowed one can be constructed",
      _ES(session_id="s", entity_id=1, entity={},
          context_borrowed=True).context_borrowed is True)

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("[timer] ALL OK")
