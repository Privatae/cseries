"""A scheduled PROMPT task has a per-task ceiling on governed turns.

THE HOLE THIS CLOSES. offline_task_runner's prompt/steps lane runs a full
governed turn — model, tools, tokens — on every fire. The only limits were
PER_PASS_MAX_TASKS (offline_task_runner.py:61), which caps how many tasks ONE
TICK claims and says nothing about how often one task fires, and the bridge
wallet floor, which is the end of the money rather than a governor. One row set
to every:60 is 1,440 model turns a day.

WHAT IS REAL HERE. `runs_in_last_24h` is NOT mocked — the injection point is
BELOW it, at the pool: `_Pool` answers the real COUNT SQL by applying the real
filters (the 24-hour window and the skip-row exclusion) to rows the test
inserted. A test that stubbed the counter would be asserting its own arithmetic.
`_run_one`, `max_runs_per_day`, `_first_skip_in_window` and the re-arm are all
the production functions. Only the model turn itself and the Circles report are
stood in for.

MUTATION — each one was run against this file, and this is what went red:
  - Delete the `if ran >= ceiling:` block in offline_task_runner._run_one:
    6 failed / 10 passed (at_the_ceiling, payload_ceiling_overrides,
    skip_is_recorded, skip_re_arms, skip_rows_do_not_latch, repeated_skip).
  - Move that block ABOVE the `if op_tool:` return: 1 failed / 15 passed —
    test_the_op_lane_is_not_subject_to_the_ceiling, and only that one.
  - Delete the `if _implied > _ceiling:` refusal in tools._exec_schedule_task:
    2 failed / 14 passed (both authoring-refusal tests).
  - Invert the skip-row exclusion in runs_in_last_24h (NOT LIKE -> LIKE):
    5 failed / 11 passed, test_skip_rows_do_not_latch_the_ceiling_on among
    them. That mutation is not isolated to the latch test, because breaking
    the counter at all breaks every count-dependent assertion — which is the
    honest result, not a claim that one test guards one line.

Run: pytest tests/test_scheduled_turn_ceiling.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import json

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

from mastercode.brain import offline_task_runner as R  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


NOW = datetime.now(timezone.utc)


class _Pool:
    """A calendar_event_runs table, in memory, that answers the runner's own SQL.

    Rows are dicts {event_id, entity_id, ran_at, ok, error}. The two COUNT
    queries are recognised by their text and evaluated with the SAME predicates
    the SQL states, so the function under test does its own filtering decision.
    """

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.updates = []          # every calendar_events UPDATE, in order

    # -- the runner's writes ------------------------------------------------
    async def execute(self, sql, *args):
        if "INSERT INTO calendar_event_runs" in sql:
            # cost_usd and priced_usd (v251) ride last: the run's whole price
            # (None when any turn had none) and what its priced turns cost.
            event_id, entity_id, ok, excerpt, error, *money = args
            cost_usd, priced_usd = (list(money) + [None, None])[:2]
            self.rows.append({"event_id": event_id, "entity_id": entity_id,
                              "ran_at": datetime.now(timezone.utc),
                              "ok": ok, "error": error, "excerpt": excerpt,
                              "cost_usd": cost_usd, "priced_usd": priced_usd})
        elif "UPDATE calendar_events" in sql:
            self.updates.append((sql, args))
        return None

    async def fetch(self, sql, *args):
        return []

    async def fetchrow(self, sql, *args):
        return None

    # -- the two COUNTs -----------------------------------------------------
    async def fetchval(self, sql, *args):
        if "FROM calendar_event_runs" in sql and "sum(" in sql:
            # budget_state's spend, evaluated the way its SQL states it.
            event_id, entity_id = args
            mine = [r for r in self.rows
                    if r["event_id"] == event_id and r["entity_id"] == entity_id]
            if "COALESCE(priced_usd, cost_usd)" in sql:
                vals = [r.get("priced_usd") if r.get("priced_usd") is not None
                        else r.get("cost_usd") for r in mine]
            else:
                vals = [r.get("cost_usd") for r in mine]
            return sum(v for v in vals if v is not None)
        if "FROM calendar_event_runs" not in sql or "COUNT(*)" not in sql:
            return None
        event_id, entity_id, like = args
        prefix = like[:-1] if like.endswith("%") else like
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        hit = [r for r in self.rows
               if r["event_id"] == event_id and r["entity_id"] == entity_id
               and r["ran_at"] >= cutoff]
        if "NOT LIKE" in sql:      # real runs: skips excluded
            return len([r for r in hit
                        if not (r["error"] or "").startswith(prefix)])
        return len([r for r in hit  # the skips themselves
                    if (r["error"] or "").startswith(prefix)])

    def acquire(self):
        p = self

        class _C:
            async def __aenter__(self):
                return p

            async def __aexit__(self, *e):
                return False
        return _C()


class _Row(dict):
    """A claimed calendar_events row — __getitem__ is all _run_one uses."""


def _task(payload, *, event_id=11, recurrence="every:900"):
    return _Row({"id": event_id, "title": "check the bot", "recurrence": recurrence,
                 "start_time": NOW - timedelta(minutes=15), "payload": payload})


def _ran_rows(n, event_id=11, entity_id=9003, ok=True):
    return [{"event_id": event_id, "entity_id": entity_id,
             "ran_at": NOW - timedelta(minutes=i + 1), "ok": ok, "error": None}
            for i in range(n)]


@pytest.fixture()
def turns(monkeypatch):
    """Every governed turn the runner actually started, and every op it ran."""
    started: list[str] = []

    class _BS:
        @staticmethod
        def get_runtime():
            return object()

        @staticmethod
        async def send_message(**kw):
            # A fired turn opens on the platform's pointer line, then the plan
            # (turn types step 5: the thread is the chain of thought). This
            # file is about WHICH turns run, so it records the plan text and
            # insists the pointer is there.
            import re
            msg = kw.get("message") or ""
            lead, sep, plan = msg.partition("\nThe plan: ")
            if not sep:
                m = re.search(r"\nStep \d+ of \d+ in the plan: ", msg)
                assert m, msg
                lead, plan = msg[:m.start()], msg[m.end():]
            assert lead.startswith("(") and "is due" in lead, msg
            started.append(plan)
            yield '0:"done"\n'

        @staticmethod
        def get_pool():
            return None

    import server.brain_service as real_bs
    monkeypatch.setattr(real_bs, "get_runtime", _BS.get_runtime)
    monkeypatch.setattr(real_bs, "send_message", _BS.send_message)

    # The report write is a Circles/bridge concern, not this ceiling's.
    async def _quiet_report(*a, **kw):
        return None
    monkeypatch.setattr(R, "_report", _quiet_report, raising=False)
    return started


# ── the ceiling itself ───────────────────────────────────────────────────────

def test_the_default_is_documented_and_clears_a_quarter_hourly_schedule():
    # 15 minutes is 96 fires a day; the default must not refuse what the user
    # actually chose. (Caps protect the platform, not the user.)
    assert R.DEFAULT_MAX_RUNS_PER_DAY >= 96 * 2
    assert R.DEFAULT_MAX_RUNS_PER_DAY == 200


def test_max_runs_per_day_reads_the_payload_and_falls_back_on_junk():
    assert R.max_runs_per_day({"max_runs_per_day": 5}) == 5
    assert R.max_runs_per_day({"max_runs_per_day": "7"}) == 7
    for junk in ({}, {"max_runs_per_day": None}, {"max_runs_per_day": "soon"},
                 {"max_runs_per_day": 0}, {"max_runs_per_day": -3}):
        assert R.max_runs_per_day(junk) == R.DEFAULT_MAX_RUNS_PER_DAY, junk


def test_below_the_ceiling_the_turn_runs(turns):
    pool = _Pool(_ran_rows(R.DEFAULT_MAX_RUNS_PER_DAY - 1))
    _run(R._run_one(pool, 9003, _task({"prompt": "how is it going"})))
    assert turns == ["how is it going"], turns


def test_at_the_ceiling_the_turn_is_skipped(turns):
    pool = _Pool(_ran_rows(R.DEFAULT_MAX_RUNS_PER_DAY))
    _run(R._run_one(pool, 9003, _task({"prompt": "how is it going"})))
    assert turns == [], f"the model was called past the ceiling: {turns}"


def test_a_payload_ceiling_overrides_the_default(turns):
    pool = _Pool(_ran_rows(4))
    _run(R._run_one(pool, 9003, _task({"prompt": "go", "max_runs_per_day": 4})))
    assert turns == []
    # ...and one under it still runs.
    pool2 = _Pool(_ran_rows(3))
    _run(R._run_one(pool2, 9003, _task({"prompt": "go", "max_runs_per_day": 4})))
    assert turns == ["go"], turns


def test_runs_outside_the_24h_window_do_not_count(turns):
    old = [{"event_id": 11, "entity_id": 9003,
            "ran_at": NOW - timedelta(hours=25, minutes=i), "ok": True, "error": None}
           for i in range(50)]
    pool = _Pool(old + _ran_rows(3))
    _run(R._run_one(pool, 9003, _task({"prompt": "go", "max_runs_per_day": 4})))
    assert turns == ["go"], "yesterday's runs were counted against today"


def test_the_skip_is_recorded_naming_the_ceiling(turns):
    pool = _Pool(_ran_rows(4))
    _run(R._run_one(pool, 9003, _task({"prompt": "go", "max_runs_per_day": 4})))
    skips = [r for r in pool.rows if (r["error"] or "").startswith(R.CEILING_SKIP_PREFIX)]
    assert len(skips) == 1, pool.rows
    msg = skips[0]["error"]
    assert skips[0]["ok"] is False
    assert "used 4 of the 4 model turns" in msg, msg
    assert "max_runs_per_day" in msg


def test_the_skip_re_arms_on_the_recurrence_not_faster(turns):
    pool = _Pool(_ran_rows(4))
    task = _task({"prompt": "go", "max_runs_per_day": 4}, recurrence="every:900")
    _run(R._run_one(pool, 9003, task))
    assert turns == [], "this asserts the SKIP's re-arm, so no turn may have run"
    rearms = [a for sql, a in pool.updates if "status='scheduled'" in sql]
    assert len(rearms) == 1, pool.updates
    new_start = rearms[0][1]
    # Strictly future, and advanced by whole 900s periods from start_time —
    # never sooner than the recurrence the user chose.
    assert new_start > datetime.now(timezone.utc)
    delta = (new_start - task["start_time"]).total_seconds()
    assert delta >= 900 and delta % 900 == 0, delta


def test_skip_rows_do_not_latch_the_ceiling_on(turns):
    """A skip writes a run row. If those counted, the count would stay at the
    ceiling forever and the task would never run again."""
    pool = _Pool(_ran_rows(4))
    task = _task({"prompt": "go", "max_runs_per_day": 4})
    _run(R._run_one(pool, 9003, task))          # first fire: skipped, writes a row
    assert turns == []
    # Age the four real runs out of the window; only the skip row is left.
    for r in pool.rows:
        if not (r["error"] or "").startswith(R.CEILING_SKIP_PREFIX):
            r["ran_at"] = NOW - timedelta(hours=30)
    _run(R._run_one(pool, 9003, task))
    assert turns == ["go"], "the ceiling latched on its own skip rows"


def test_a_repeated_skip_speaks_once_but_records_every_time(turns):
    pool = _Pool(_ran_rows(4))
    task = _task({"prompt": "go", "max_runs_per_day": 4})
    for _ in range(3):
        _run(R._run_one(pool, 9003, task))
    skips = [r for r in pool.rows if (r["error"] or "").startswith(R.CEILING_SKIP_PREFIX)]
    assert len(skips) == 3, "every skip must leave a run record"
    assert turns == []


# ── the budget is TURNS, not fires (one governed turn PER STEP) ──────────────

def test_turns_per_fire_counts_steps_and_falls_back_to_one():
    assert R.turns_per_fire({"prompt": "go"}) == 1
    assert R.turns_per_fire({}) == 1
    assert R.turns_per_fire({"steps": []}) == 1
    assert R.turns_per_fire({"steps": ["a", "  ", "b"]}) == 2
    assert R.turns_per_fire({"steps": [f"s{i}" for i in range(20)]}) == 20


def test_a_20_step_task_trips_the_ceiling_after_10_fires_not_200(turns):
    """THE BUG THIS CLOSES. _run_one runs one governed turn per step, so a
    20-step fire costs 20 turns. Counting FIRES let a 20-step task at every:900
    run 96 fires = 1,920 turns a day under a ceiling that says 200."""
    steps = [f"step {i}" for i in range(20)]
    payload = {"steps": steps}                      # default ceiling: 200
    # 9 fires = 180 turns spent. The 10th costs 20 and lands exactly on 200.
    pool = _Pool(_ran_rows(9))
    _run(R._run_one(pool, 9003, _task(payload)))
    assert len(turns) == 20, f"the 10th fire should run all 20 steps: {len(turns)}"

    # 10 fires = 200 turns spent. The 11th would make 220 — refused.
    turns.clear()
    pool2 = _Pool(_ran_rows(10))
    _run(R._run_one(pool2, 9003, _task(payload)))
    assert turns == [], f"fire 11 spent {len(turns)} turns past the ceiling"


def test_the_ceiling_refuses_a_fire_it_cannot_AFFORD_not_just_one_past_it(turns):
    """A 20-turn fire with 10 turns of budget left must not start: it would
    overshoot by 10. Fires, not turns, is exactly the arithmetic that hid this."""
    payload = {"steps": [f"s{i}" for i in range(20)], "max_runs_per_day": 50}
    pool = _Pool(_ran_rows(2))   # 40 of 50 turns spent; 10 left, fire costs 20
    _run(R._run_one(pool, 9003, _task(payload)))
    assert turns == [], f"started a fire it could not afford: {len(turns)} turns"


def test_the_first_fire_of_a_window_always_runs(turns):
    """A single fire costing more than the whole ceiling must not make the task
    permanently dead. Authoring refuses that combination; this is the floor under
    payloads written straight through POST /api/calendar."""
    payload = {"steps": [f"s{i}" for i in range(20)], "max_runs_per_day": 5}
    _run(R._run_one(_Pool([]), 9003, _task(payload)))
    assert len(turns) == 20, "a task was left permanently unable to run"


def test_the_skip_message_counts_turns_and_names_the_per_step_cost(turns):
    payload = {"steps": [f"s{i}" for i in range(20)], "max_runs_per_day": 200}
    pool = _Pool(_ran_rows(10))
    _run(R._run_one(pool, 9003, _task(payload)))
    skips = [r for r in pool.rows if (r["error"] or "").startswith(R.CEILING_SKIP_PREFIX)]
    assert len(skips) == 1, pool.rows
    msg = skips[0]["error"]
    assert "200 of the 200 model turns" in msg, msg
    assert "20 turns, one per step" in msg, msg


def test_authoring_counts_steps_so_20_steps_quarter_hourly_is_refused():
    """96 fires is comfortably under 200 — and 96 x 20 steps is 1,920 turns."""
    res = _schedule(prompt="", steps=[f"s{i}" for i in range(20)],
                    recurrence="every:900")
    assert res["status"] == "error", res
    assert "1920" in res["result"] and "200" in res["result"], res["result"]
    assert "20 steps" in res["result"], res["result"]
    assert APPENDED == []


def test_authoring_still_accepts_20_steps_at_a_schedule_that_fits():
    # every:14400 = 6 fires/day x 20 steps = 120 turns, under 200.
    res = _schedule(prompt="", steps=[f"s{i}" for i in range(20)],
                    recurrence="every:14400")
    assert res["status"] == "ok", res
    assert len(APPENDED) == 1 and len(APPENDED[0]["payload"]["steps"]) == 20


def test_authoring_refuses_a_single_fire_bigger_than_the_whole_ceiling():
    # No recurrence at all: a one-shot that could never run once.
    res = _schedule(prompt="", steps=[f"s{i}" for i in range(20)],
                    max_runs_per_day=5)
    assert res["status"] == "error", res
    assert "would never run at all" in res["result"], res["result"]
    assert APPENDED == []


# ── a skip reads as a SKIP, and the sentinel never reaches the user ──────────

def test_the_reported_prose_says_skipped_and_carries_no_machine_prefix(monkeypatch):
    written: list[str] = []

    import mastercode.memory_engine as ME

    async def _write_turn_row(pool, entity_id, session, role, text, **kw):
        written.append(text)

    monkeypatch.setattr(ME, "write_turn_row", _write_turn_row)
    monkeypatch.setattr(ME, "derive_source_context", lambda *a, **k: "circles")
    # A task that names no thread reports into a topic of its own (there is
    # no original conversation); the fake pool cannot create one.
    import mastercode.circles_threads as CT

    async def _home(pool, eid, thread_id, *, title):
        return "11111111-2222-4333-8444-555555555555"
    monkeypatch.setattr(CT, "home_thread", _home)

    pool = _Pool(_ran_rows(4))
    _run(R._run_one(pool, 9003, _task({"prompt": "go", "max_runs_per_day": 4})))

    assert len(written) == 1, written
    said = written[0]
    assert "was skipped" in said, said
    assert "failed" not in said, said
    assert R.CEILING_SKIP_PREFIX not in said, "the machine sentinel leaked into prose"
    # ...while the STORED row keeps the sentinel, because two queries key on it.
    stored = [r["error"] for r in pool.rows if r["error"]]
    assert any(e.startswith(R.CEILING_SKIP_PREFIX) for e in stored), stored


# ── the Agents panel does not count a skip as a failure ──────────────────────

def test_agents_panel_excludes_ceiling_skips_from_the_failure_count():
    """The predicate itself is asserted at the source (the query is one large
    SQL statement with correlated subselects; there is no pool stand-in that
    could execute it honestly, and a fake that "evaluated" it would be testing
    the fake). What IS executed is the drift guard below and _plain_error."""
    src = (_ROOT / "server/routes/agents.py").read_text()
    failures = src[src.index("AS failures") - 700:src.index("AS failures")]
    assert "c.ok IS FALSE" in failures
    assert "NOT LIKE 'ScheduledTurnCeiling:%'" in failures, failures[-400:]
    assert "AS skipped" in src, "the skip count is not exposed"


def test_the_agents_sql_literal_cannot_drift_from_the_runner_constant():
    """agents.py writes the sentinel as a SQL literal and the runner owns the
    constant. They are in different languages and nothing else ties them."""
    src = (_ROOT / "server/routes/agents.py").read_text()
    assert f"'{R.CEILING_SKIP_PREFIX}%'" in src, R.CEILING_SKIP_PREFIX


def test_plain_error_strips_the_sentinel_and_leaves_everything_else():
    from server.routes.agents import _plain_error
    assert _plain_error(None) is None
    assert _plain_error("") == ""
    assert _plain_error("RuntimeError: boom") == "RuntimeError: boom"
    marked = R._ceiling_error("it has already used 200 of the 200 model turns")
    assert _plain_error(marked) == "it has already used 200 of the 200 model turns"
    assert R.CEILING_SKIP_PREFIX not in _plain_error(marked)


# ── the op lane is NOT subject to it ─────────────────────────────────────────

def test_the_op_lane_is_not_subject_to_the_ceiling(monkeypatch, turns):
    """An op runs no model and spends no tokens, so a SPEND ceiling has nothing
    to protect there. Far past any ceiling, the op still fires."""
    fired: list[tuple] = []

    import mastercode.runtime.direct_ops as D

    async def _run_direct_op(tool, args, *, entity_id, pool, origin_context=None,
                             turn_type=None):
        fired.append((tool, args))
        return {"status": "ok", "result": "done"}

    monkeypatch.setattr(D, "run_direct_op", _run_direct_op)
    monkeypatch.setattr(D, "is_unattended_op_tool", lambda t: True)

    pool = _Pool(_ran_rows(5000))
    _run(R._run_one(pool, 9003, _task({"op": {"tool": "system.start_app",
                                           "args": {"app_id": "x"}},
                                    "max_runs_per_day": 1})))
    assert fired == [("system.start_app", {"app_id": "x"})], fired
    skips = [r for r in pool.rows if (r["error"] or "").startswith(R.CEILING_SKIP_PREFIX)]
    assert skips == [], "the op lane was ceilinged"


# ── authoring-time refusal ───────────────────────────────────────────────────

class _AuthoringPool(_Pool):
    async def fetchrow(self, sql, *args):
        return None


# Every payload a successful create actually wrote. A refusal must leave this
# empty; an acceptance must put exactly one entry in it. Without this, an
# "accepted" assertion passes on ANY unrelated failure downstream.
APPENDED: list[dict] = []


def _schedule(*, approval=False, **kw):
    import mastercode.runtime.tools as T
    import mastercode.runtime.scheduled_approval as SA
    import server.bridge.memory_handlers as MH
    import server.brain_service as BS
    APPENDED.clear()

    async def _append(ev):
        APPENDED.append(ev)
        return {"ok": True, "event_id": "evt-1", "event_at": ev.get("event_at")}

    # The plan's write into the thread is turn types' concern
    # (scripts/test_turn_types_wakeups.py), not this ceiling's: it lands.
    async def _posted(*a, **k):
        return True

    # A recurring prompt task waits for the person's card
    # (runtime/scheduled_approval); this file is about the ceiling and the
    # payload, so the card is skipped here unless a test asks for it.
    _real = (BS.get_pool, BS._entity_id, MH.handle_calendar_append, R.post_to_origin_thread,
             SA.needs_approval)
    BS.get_pool = lambda: _AuthoringPool()
    BS._entity_id = 9003
    MH.handle_calendar_append = _append
    R.post_to_origin_thread = _posted
    if not approval:
        SA.needs_approval = lambda **k: False
    try:
        return _run(T._exec_schedule_task(
            {"op": "create", "title": "t", "fire_at": "2026-10-01T00:00:00Z", **kw}))
    finally:
        (BS.get_pool, BS._entity_id, MH.handle_calendar_append,
         R.post_to_origin_thread, SA.needs_approval) = _real


def test_authoring_refuses_a_recurrence_that_outruns_the_ceiling():
    res = _schedule(prompt="go", recurrence="every:60", max_runs_per_day=10)
    assert res["status"] == "error", res
    msg = res["result"]
    # BOTH numbers, in plain English: what the schedule asks for, and the cap.
    assert "1440" in msg and "10" in msg, msg
    assert "max_runs_per_day" in msg


def test_authoring_refuses_against_the_DEFAULT_ceiling_too():
    # every:60 = 1440/day, past the 200 default with no explicit ceiling set.
    res = _schedule(prompt="go", recurrence="every:60")
    assert res["status"] == "error", res
    assert "1440" in res["result"] and str(R.DEFAULT_MAX_RUNS_PER_DAY) in res["result"]


def test_a_quarter_hourly_schedule_is_accepted_on_the_default():
    """The POSITIVE assertion. `status != "error"` on its own would pass on any
    unrelated downstream failure, which is no test at all — so this asserts the
    create SUCCEEDED and the row was actually written."""
    res = _schedule(prompt="go", recurrence="every:900")
    assert res["status"] == "ok", res
    assert len(APPENDED) == 1, APPENDED
    assert APPENDED[0]["event_type"] == "scheduled_task"
    assert APPENDED[0]["recurrence"] == "every:900"
    assert APPENDED[0]["payload"]["prompt"] == "go"


def test_an_explicit_ceiling_is_written_into_the_payload():
    res = _schedule(prompt="go", recurrence="every:900", max_runs_per_day=150)
    assert res["status"] == "ok", res
    assert APPENDED[0]["payload"]["max_runs_per_day"] == 150, APPENDED


def test_the_op_lane_is_not_refused_at_authoring_either(monkeypatch):
    import mastercode.runtime.direct_ops as D
    monkeypatch.setattr(D, "is_unattended_op_tool", lambda t: True)
    monkeypatch.setattr(D, "UNATTENDED_OP_TOOLS", {"system.start_app"}, raising=False)
    res = _schedule(op_tool="system.start_app", op_args={"app_id": "x"},
                    recurrence="every:60")
    assert res["status"] == "ok", res
    assert len(APPENDED) == 1 and APPENDED[0]["payload"]["op"]["tool"] == "system.start_app"
    assert "max_runs_per_day" not in APPENDED[0]["payload"]


def test_a_ceiling_passed_with_op_tool_is_REFUSED_not_dropped(monkeypatch):
    """It used to be validated and then silently not written (the payload body
    only carries it on the turn lane), so the host was told a limit was set that
    nothing would ever enforce. Say no instead."""
    import mastercode.runtime.direct_ops as D
    monkeypatch.setattr(D, "is_unattended_op_tool", lambda t: True)
    monkeypatch.setattr(D, "UNATTENDED_OP_TOOLS", {"system.start_app"}, raising=False)
    res = _schedule(op_tool="system.start_app", op_args={"app_id": "x"},
                    max_runs_per_day=50)
    assert res["status"] == "error", res
    assert "op lane runs no model" in res["result"], res["result"]
    assert APPENDED == [], "a refused create still wrote a row"


def test_a_nonsense_ceiling_is_refused_not_silently_ignored():
    for bad in ("soon", 0, -1):
        res = _schedule(prompt="go", max_runs_per_day=bad)
        assert res["status"] == "error", (bad, res)
        assert "max_runs_per_day" in res["result"]
        assert APPENDED == [], (bad, APPENDED)


# ── the job's own budget (payload budget_usd, 2026-10-20) ───────────────────

@pytest.fixture()
def priced(monkeypatch):
    """Turns that report a price: each started turn takes the next cost from
    `costs` and sends it on the context frame the way brain_service does
    (2:[{"t": "context", ..., "est_cost_usd": …}]). None = an unpriced turn."""
    state = {"costs": [], "started": []}

    async def send_message(**kw):
        state["started"].append(kw.get("message") or "")
        c = state["costs"].pop(0) if state["costs"] else None
        yield '0:"done"\n'
        yield "2:" + json.dumps([{"t": "context", "est_cost_usd": c,
                                   "price_basis": "list" if c is not None else "plan"}]) + "\n"

    import server.brain_service as real_bs
    monkeypatch.setattr(real_bs, "get_runtime", lambda: object())
    monkeypatch.setattr(real_bs, "send_message", send_message)
    said: list[str] = []

    async def _capture(pool_, entity_id, origin, text, **kw):
        said.append(text)
        return True
    monkeypatch.setattr(R, "post_to_origin_thread", _capture)

    async def _origin(pool_, entity_id, origin, **kw):
        return origin or {}
    monkeypatch.setattr(R, "ensure_origin_thread", _origin)
    state["said"] = said
    return state


def _armed(pool):
    return any("status='scheduled'" in sql for sql, _ in pool.updates)


def test_a_reached_budget_stops_between_steps_for_good(priced):
    priced["costs"] = [0.30, 0.30, 0.30]
    pool = _Pool([])
    _run(R._run_one(pool, 9310, _task({"steps": ["one", "two", "three"],
                                       "budget_usd": 0.5})))
    assert len(priced["started"]) == 2, priced["started"]       # step three never ran
    run = pool.rows[-1]
    # Stopped at the person's budget is the job done as asked: the run is
    # recorded ok with the reason beside it, and the task completes.
    assert run["ok"] is True and abs(run["cost_usd"] - 0.6) < 1e-9, run
    assert "budget" in str(run.get("error") or ""), run
    assert not _armed(pool), pool.updates                         # not re-armed
    assert any("completed" in (args or ()) for _, args in pool.updates), pool.updates
    assert "stopped:" in priced["said"][-1] and "failed" not in priced["said"][-1]


def test_a_partly_priced_run_still_counts_toward_the_budget(priced):
    # Fire 1: the first step's turn has no price (a failover onto a plan), the
    # second costs $0.40. The run's cost_usd is null — a partial sum never
    # reads as the whole — but the $0.40 was spent all the same.
    priced["costs"] = [None, 0.40]
    pool = _Pool([])
    payload = {"steps": ["one", "two"], "budget_usd": 0.35}
    _run(R._run_one(pool, 9310, _task(payload)))
    assert len(priced["started"]) == 2
    assert pool.rows[-1]["cost_usd"] is None, pool.rows[-1]
    # Fire 2: the job has spent $0.40 of its $0.35, so nothing runs.
    priced["started"].clear()
    priced["costs"] = [0.40, 0.40]
    _run(R._run_one(pool, 9310, _task(payload)))
    assert priced["started"] == [], "a job ran past its budget: the priced part " \
        "of a partly priced run was not counted"
    assert "stopped:" in priced["said"][-1]


# ── the person's card covers a recurring op that schedules a model turn ─────
# schedule_task is itself an unattended op (direct_ops.UNATTENDED_OP_TOOLS),
# so a recurring OP-lane task whose op is schedule_task create of a one-time
# PROMPT task runs a model on every fire just as a recurring prompt task does.
# Ungated, it was the way round the card (review of 91ad90a9c).

def _proposed(monkeypatch):
    import mastercode.runtime.scheduled_approval as SA
    seen: list[dict] = []

    async def _propose(pool, eid, **kw):
        seen.append(kw)
        return {"status": "ok", "result": {"scheduled": None, "awaiting_approval": True,
                                           "ticket_id": 1}}
    monkeypatch.setattr(SA, "propose", _propose)
    return seen


def test_a_recurring_op_that_schedules_a_model_turn_waits_for_the_card(monkeypatch):
    seen = _proposed(monkeypatch)
    res = _schedule(approval=True, recurrence="daily", op_tool="schedule_task",
                    op_args={"op": "create", "title": "inner", "fire_at": "2026-10-01T00:00:00Z",
                             "prompt": "go", "steps": ["a", "b"]})
    assert res["result"].get("awaiting_approval"), res
    assert APPENDED == [], "the wrapped model task was set without the person"
    assert seen and seen[0]["turns_per_fire"] == 2, seen


def test_a_nested_wrap_is_followed_down_to_the_model_turn(monkeypatch):
    seen = _proposed(monkeypatch)
    inner = {"op": "create", "title": "inner", "fire_at": "2026-10-01T00:00:00Z",
             "op_tool": "schedule_task",
             "op_args": {"op": "create", "title": "innermost",
                         "fire_at": "2026-10-01T00:00:00Z", "prompt": "go"}}
    res = _schedule(approval=True, recurrence="daily", op_tool="schedule_task", op_args=inner)
    assert res["result"].get("awaiting_approval") and APPENDED == [], res
    assert seen


def test_a_recurring_op_that_runs_no_model_is_still_set_at_once(monkeypatch):
    seen = _proposed(monkeypatch)
    res = _schedule(approval=True, recurrence="daily", op_tool="schedule_task",
                    op_args={"op": "list"})
    assert res["status"] == "ok" and not res["result"].get("awaiting_approval"), res
    assert len(APPENDED) == 1 and not seen


# ── a card decided after its thread was deleted sets nothing ────────────────
# schedule_task refuses a create in a deleted thread (the delete cancelled
# what was set there); the person's click on a card filed there before the
# delete must not set what the create would have refused.

def test_apply_after_the_thread_was_deleted_sets_nothing(monkeypatch):
    import mastercode.circles_threads as CT
    import mastercode.runtime.scheduled_approval as SA
    import server.bridge.memory_handlers as MH
    wrote: list = []

    async def _gone(pool, eid, thread_id):
        return None

    async def _post(*a, **k):
        wrote.append(("plan", a, k))
        return True

    async def _append(ev):
        wrote.append(("row", ev))
        return {"ok": True, "event_id": "e", "event_at": "x"}

    monkeypatch.setattr(CT, "live_thread_id", _gone)
    monkeypatch.setattr(R, "post_to_origin_thread", _post)
    monkeypatch.setattr(MH, "handle_calendar_append", _append)
    md = {"recurring_task": {"title": "t", "plan_msg": "Scheduled **t**",
                             "origin": {"thread_id": "11111111-1111-1111-1111-111111111111"},
                             "append": {"event_id": "22222222-2222-2222-2222-222222222222",
                                        "event_type": "scheduled_task", "summary": "t",
                                        "event_at": "2026-10-01T00:00:00Z",
                                        "recurrence": "daily", "payload": {"prompt": "go"}}}}
    out = _run(SA.apply_proposal(object(), 9003, md, option_id=SA.OPT_APPLY, ticket_id=5))
    assert out["applied"] is False and "deleted" in out["why"], out
    assert wrote == [], wrote


def test_the_card_names_no_daily_number_when_the_person_set_no_limit(monkeypatch):
    # user_limits.person_or answers "no limit" as 10**9; the card is the
    # person's to read, and "at most 1000000000 model turns a day" is not a bound.
    import mastercode.runtime.scheduled_approval as SA
    from server.routes import tickets as TK
    filed: list = []

    async def _create(pool, eid, **kw):
        filed.append(kw)
        return {"id": 9}
    monkeypatch.setattr(TK, "create_ticket_service", _create)
    _run(SA.propose(object(), 9003, title="t", recurrence="daily", fire_at="x", plan_msg="p",
                    append={}, origin=None, turns_per_fire=1, turns_per_day=10 ** 9,
                    budget_usd=None))
    assert "1000000000" not in filed[0]["sitrep"], filed[0]["sitrep"]
    _run(SA.propose(object(), 9003, title="t", recurrence="daily", fire_at="x", plan_msg="p",
                    append={}, origin=None, turns_per_fire=1, turns_per_day=200,
                    budget_usd=None))
    assert "at most 200 model turns a day" in filed[1]["sitrep"], filed[1]["sitrep"]


# ── a recurring bug_review that wakes the validation turn is model work ─────
# bug_review op='pending' with wake=true starts a validation turn (a model)
# when reports are waiting (runtime/bug_review.run); on a recurrence that is
# repeating model work, so it waits for the card like any recurring prompt.

def test_a_recurring_bug_review_wake_waits_for_the_card(monkeypatch):
    seen = _proposed(monkeypatch)
    res = _schedule(approval=True, recurrence="daily", op_tool="bug_review",
                    op_args={"op": "pending", "wake": True})
    assert res["status"] == "ok" and res["result"].get("awaiting_approval"), res
    assert not APPENDED and len(seen) == 1, (APPENDED, seen)


def test_a_recurring_bug_review_read_without_wake_is_set_at_once(monkeypatch):
    seen = _proposed(monkeypatch)
    res = _schedule(approval=True, recurrence="daily", op_tool="bug_review",
                    op_args={"op": "pending"})
    assert res["status"] == "ok" and not res["result"].get("awaiting_approval"), res
    assert len(APPENDED) == 1 and not seen


# ── the click adds the trigger to the channel the card was filed for ────────
# A name is looked up again at the click, and by then it can name another
# channel; the card carries the id it was checked under (review of 7bb1b4557).

def test_the_runbook_card_adds_its_trigger_by_the_checked_channel_id(monkeypatch):
    import server.reachout.service as ro
    from mastercode.runtime import scheduled_approval as sa
    seen = []

    async def fake_add_trigger(pool, eid, **kw):
        seen.append(kw)
        return {"id": 3}
    monkeypatch.setattr(ro, "add_trigger", fake_add_trigger)
    md = {"runbook_trigger": {"channel": "invented-runbook", "channel_id": 9, "label": "invented",
                              "match_kind": "system_notification", "match_event": None,
                              "match_jsonb": None}}
    out = asyncio.run(sa.apply_trigger(None, 1, md, option_id=sa.OPT_APPLY, ticket_id=1))
    assert out["applied"] and seen and seen[0]["channel"] == 9, (out, seen)
    assert seen[0]["label"] == "invented" and seen[0]["match_kind"] == "system_notification"
