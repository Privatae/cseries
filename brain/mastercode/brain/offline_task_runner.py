"""Offline-task executor.

Runs the host's scheduled tasks WITHOUT booting the full autonomous brain
(no UnifiedBrain, no .start()). A single guarded background
loop polls `calendar_events` for due scheduled tasks, atomically claims each
one, runs its prompt through the SAME governed host turn pipeline the live
chat uses (brain_service.send_message — identity + tools + CEIGAS gate), and
reports the result into the entity's Circles self-thread.

Scheduled tasks are written by schedule_task (runtime/tools.py
_exec_schedule_task -> server.bridge.memory_handlers.handle_calendar_append):
    event_type = 'scheduled_task'
    status     = 'scheduled'
    start_time = the fire time (event_at; both columns set to it)
    recurrence = NULL | hourly | daily | weekly | monthly | every:<seconds>
    payload    = {"prompt": "...", "steps": [...]}
                 (+ "wake_on": {event, match} for a task that runs when a platform
                 event happens; start_time is then its deadline, and
                 server/host_events.emit pulls it forward and records "woke_on")

Headless-run approach: the FULL governed turn entry,
brain_service.send_message(entity_id=..., message=<prompt>, session_id=None) —
a MODULE-LEVEL function that STREAMS AI-SDK Data Stream frames (it is an async
generator, not an awaitable). It builds the host identity + system prompt,
resolves the chat-role model, runs the CEIGAS inference gate (fail-closed) + the
tool loop. We reconstruct the assistant text from the 0:"<delta>" frames
(3:"<msg>" = terminal error), so an offline task is governed exactly like an
interactive turn. We then write the result into the Circles self-thread (the
streamed turn rows carry app_id='scheduled_task', not 'circles', so the
user-facing report write is explicit).

Re-arm reads a schedule with entity_clock._recurrence_seconds — the one reading
of a recurrence, shared with the scheduling tool and the Timeline.

Guarantees:
  - Atomic claim (UPDATE ... WHERE status='scheduled' RETURNING) so two
    overlapping ticks never double-run the same task.
  - Per-task try/except — one failing task never kills the loop.
  - Recurring tasks re-arm to their next occurrence strictly after now
    (catching up if the runner was down); one-shot tasks complete.
  - Every run appends a calendar_event_runs child row (ok / excerpt / error).

TWO LANES:
  - payload {"prompt": …} / {"steps": […]} — a governed turn per prompt/step.
    The host's judgement, running on a schedule. Costs tokens by design.
  - payload {"op": {"tool": …, "args": {…}}} — ONE deterministic registry op
    through run_direct_op. No model, no turn, no tokens. This is the lane the
    host should reach for whenever the work is the same every time: an LLM is
    never called to do a repetitive task, only to build the thing that does it.

THE SPEND CEILING applies to the first lane only, because it is the only one
that spends: payload {"max_runs_per_day": N} (default DEFAULT_MAX_RUNS_PER_DAY)
caps how many governed turns one task may fire in a rolling 24 hours. At the
ceiling the turn is skipped, a run row records why, and the task re-arms on its
own recurrence — never faster. The op lane is not subject to it.
"""

import asyncio
import math
import time
import logging
from datetime import datetime, timedelta, timezone

# The one reading of a schedule (the scheduling tool validates with it and the
# Timeline judges routine work by it); this module used to carry a copy.
from mastercode.brain.entity_clock import _recurrence_seconds

log = logging.getLogger("cseries.brain.offline_tasks")

# Poll cadence (30s) — fast enough
# that a task fires within a tick of its due time, slow enough to be cheap.
POLL_INTERVAL_S = 30

# Cap rows claimed per pass so a long-idle catch-up can't pin the loop
# running dozens of LLM turns back-to-back in one tick.
PER_PASS_MAX_TASKS = 10


def wakeup_concurrency() -> int | None:
    """How many more wake-ups fit at once: what the Host's memory holds now
    (runtime/host_memory), never a count of its own (the memory is the
    Host's to manage). None: the Host has no readable limit, so
    nothing is held back."""
    from mastercode.runtime import host_memory
    return host_memory.turns_that_fit()


def wakeup_thread_key(entity_id: int, event_id: int) -> str:
    """Each wake-up is its own turn lane: two wake-ups due
    together are two turns, and a wake-up beside a live chat in the same
    thread is a second turn — never a nudge folded into the chat's."""
    return f"wakeup|{entity_id}|{event_id}"


# The words turn_registry.TurnCapError carries ("N turns are already running
# (ceiling M) …") — how a wake-up that met a full process is told apart from
# one that failed.
from mastercode.runtime.host_memory import NO_ROOM as TURN_CAP_MARK  # noqa: E402

def _orphan_after_s() -> float:
    """A row left 'running' longer than a live turn can stay quiet plus a
    margin is an orphan of a process that died mid-run. A turn has no total
    by default — it is stopped after
    turn_registry.TURN_IDLE_S with no sign of life, or at the opt-in
    TURN_TIMEOUT_S, whichever is sooner — so that is the figure here. A row
    THIS process is running is never swept, however long it runs (the sweep
    excludes `mine`), and a steps task stays younger than this: each step
    touches its row (_touch)."""
    try:
        from server.turn_registry import TURN_IDLE_S, TURN_TIMEOUT_S
        quiet = min(float(TURN_IDLE_S) if TURN_IDLE_S and TURN_IDLE_S > 0 else math.inf,
                    float(TURN_TIMEOUT_S) if TURN_TIMEOUT_S and TURN_TIMEOUT_S > 0 else math.inf)
        return (quiet if math.isfinite(quiet) else 1800.0) + 300.0
    except Exception:  # noqa: BLE001
        return 2100.0


ORPHAN_AFTER_S = _orphan_after_s()


async def _touch(pool, entity_id: int, event_id: int) -> None:
    """Mark a running task alive (updated_at) so the orphan sweep never takes
    a long steps task for a dead one. Best-effort."""
    try:
        await pool.execute(
            "UPDATE calendar_events SET updated_at=NOW() "
            "WHERE id=$1 AND entity_id=$2 AND status='running'", event_id, entity_id)
    except Exception as e:  # noqa: BLE001
        log.debug("offline-task: touch failed (event=%s): %s", event_id, e)


async def _requeue(pool, entity_id: int, event_id: int) -> None:
    """Put a claimed task back to 'scheduled', unchanged: it runs on the next
    poll that has room. Best-effort."""
    try:
        await pool.execute(
            "UPDATE calendar_events SET status='scheduled', updated_at=NOW() "
            "WHERE id=$1 AND entity_id=$2 AND status='running'",
            event_id, entity_id)
    except Exception as e:  # noqa: BLE001
        log.warning("offline-task: requeue failed (event=%s): %s", event_id, e)


# A wake-up whose turn was refused because the Host's tools had not arrived
# (runtime/identity_hold.py) did not run. It is put back a minute later, moved
# off the head of the due queue, counted, and given up on after this many tries
# (about five hours at the 300 s cap): a Host whose seed has stopped for good must not be
# retried forever in silence.
IDENTITY_DEFER_S = 60
IDENTITY_DEFER_CAP_S = 300
IDENTITY_DEFER_MAX = 60


def identity_defer_delay(tries: int) -> float:
    """Wait before try number `tries` (1-based): 60 s, doubling to 300 s."""
    return float(min(IDENTITY_DEFER_S * (2 ** max(0, tries - 1)), IDENTITY_DEFER_CAP_S))


class IdentityPending(Exception):
    """The turn was refused by the identity hold: nothing ran."""


class ModelPending(IdentityPending):
    """The turn could not run because this Host has no chat model set up:
    nothing ran, and the no-model reply is not the wake's result. The wake is
    held (put back, backing off) until a model is configured, which also kicks
    the runner (release_saved_requests)."""


async def _defer_for_identity(pool, entity_id: int, event_id: int, tries: int) -> None:
    """Back to 'scheduled' later (identity_defer_delay) with the try counted. No run row, no
    report: a turn that did not run is neither a success nor a failure."""
    try:
        await pool.execute(
            "UPDATE calendar_events SET status='scheduled', "
            "start_time = NOW() + make_interval(secs => $3), "
            "event_at = NOW() + make_interval(secs => $3), "
            "payload = jsonb_set(COALESCE(payload, '{}'::jsonb), '{identity_defers}', to_jsonb($4::int)), "
            "updated_at=NOW() "
            "WHERE id=$1 AND entity_id=$2 AND status='running'",
            event_id, entity_id, identity_defer_delay(tries), int(tries))
    except Exception as e:  # noqa: BLE001
        log.warning("offline-task: identity defer failed (event=%s): %s", event_id, e)


async def _saved_request_superseded(pool, entity_id: int, payload: dict) -> bool:
    """True when the person said something newer in the thread a saved request
    came from. Replay / A-B / scheduled rows are not the person speaking. A
    failed read is False: a request is never dropped on a read error."""
    if not payload.get("saved_at"):
        return False      # a request with no stamp has nothing to be newer than
    try:
        from datetime import datetime
        saved_at = datetime.fromisoformat(str(payload.get("saved_at")))
        src = payload.get("source_context") or {}
        kind, ref = str(src.get("kind") or ""), str(src.get("ref") or "")
        if not kind:
            return False
        found = await pool.fetchval(
            "SELECT 1 FROM entity_memory WHERE entity_id=$1 AND memory_type='turn' "
            "AND metadata->>'role'='user' AND created_at > $2 "
            "AND metadata->'source_context'->>'kind' = $3 "
            "AND metadata->'source_context'->>'ref' = $4 "
            "AND COALESCE(metadata->>'app_id','') NOT IN "
            "('circles-replay','circles-ab','scheduled_task') LIMIT 1",
            entity_id, saved_at, kind, ref)
        return bool(found)
    except Exception as e:  # noqa: BLE001
        log.warning("offline-task: supersede check failed (non-fatal): %s", e)
        return False


async def _fold_into_newest(pool, entity_id: int, payload: dict, event_id: int) -> bool:
    """Add this held request to the newest OTHER pending saved wake of its
    thread, so the newest one carries it. False when there is none (or the
    write failed), and the caller runs this request itself instead."""
    from mastercode.runtime import identity_hold as _ih
    import json as _json
    loc = _ih._locator(payload)
    if not loc:
        return False
    o = payload.get("origin") or {}
    try:
        got = await pool.fetchval(
            "UPDATE calendar_events SET updated_at=NOW(), payload = jsonb_set(payload, "
            "'{earlier}', COALESCE(payload->'earlier','[]'::jsonb) || $5::jsonb) "
            "WHERE id = (SELECT id FROM calendar_events WHERE entity_id=$1 "
            "AND event_type='scheduled_task' AND status='scheduled' "
            "AND payload->>'saved_request'='true' AND id <> $4 "
            "AND COALESCE(payload->'origin'->>'thread_id','') = $2 "
            "AND COALESCE(payload->'origin'->>'interaction_context','') = $3 "
            "ORDER BY id DESC LIMIT 1) RETURNING id",
            entity_id, str(o.get("thread_id") or ""), str(o.get("interaction_context") or ""),
            event_id, _json.dumps([loc]))
        return bool(got)
    except Exception as e:  # noqa: BLE001
        log.warning("offline-task: folding a held request failed (non-fatal): %s", e)
        return False


async def _saved_request_tools_state(pool, entity_id: int) -> str:
    """'tools' (the grant is on the row), 'chat_only' (the seed resolved and the
    Host's real tier has no tools) or 'pending' (no grant, and the seed has not
    resolved — or its state is unknown)."""
    from mastercode.runtime.tool_access import host_orchestration_enabled
    try:
        row = await pool.fetchrow(
            "SELECT permission_overrides FROM entity_registry WHERE id=$1", entity_id)
    except Exception as e:  # noqa: BLE001
        log.warning("offline-task: grant read failed (non-fatal): %s", e)
        return "pending"
    if row is not None and host_orchestration_enabled(dict(row)):
        return "tools"
    try:
        from server.bridge import identity_seed
        st = identity_seed.SEED_STATE.get("status")
    except Exception:  # noqa: BLE001
        st = None
    return "chat_only" if st in ("ok", "genesis") else "pending"


async def _sweep_orphans(pool, entity_id: int) -> None:
    """Rows left 'running' past any turn's lifetime are orphans of a process
    that died mid-run (a crash, a kill). An ordinary task is put back so it runs
    again; a SAVED REQUEST is not — whether its turn acted before the process
    died is unknown, so identity_hold.sweep_stuck_running keeps it for the
    person's next message instead (the generic statement cannot take it). Rows
    'taken over' by a turn of a process that is gone are kept the same way
    (identity_hold.sweep_taken_over, by the boot id each takeover recorded). A
    row THIS process is running is excluded by id (`mine`), however long its
    turn runs: a turn is stopped for showing no sign of life, never for its
    age, unless a Host sets the opt-in total (turn_registry.TURN_TIMEOUT_S).
    The takeover sweep's age backstop is the same quiet window (+1 min), and
    it never takes a row a live turn of this process is still carrying
    (identity_hold._in_flight_takeover_ids)."""
    from mastercode.runtime.identity_hold import sweep_stuck_running, sweep_taken_over
    try:
        await sweep_taken_over(pool, entity_id, _orphan_after_s() - 300.0 + 60.0)
    except Exception as e:  # noqa: BLE001
        log.warning("offline-task: taken-over sweep failed (non-fatal): %s", e)
    # Never a row this process is running now.
    mine = [int(getattr(t, "_event_id", -1)) for t in _RUNNING if not t.done()]
    try:
        await sweep_stuck_running(pool, entity_id, float(ORPHAN_AFTER_S), mine)
    except Exception as e:  # noqa: BLE001
        log.warning("offline-task: orphan sweep failed (non-fatal): %s", e)


_POLL_KICK = None  # asyncio.Event, made by the loop


def kick_poll() -> None:
    """Make the runner's next poll happen now. Harmless when no loop runs."""
    if _POLL_KICK is not None:
        _POLL_KICK.set()


async def release_saved_requests(pool, entity_id: int) -> None:
    """The Host's tools have arrived (the identity seed resolved) or a chat model
    was just configured: make every request saved by the identity hold due now
    and poll now, so "as soon as my tools arrive" is true rather than "within
    the next deferral and poll". Done by identity_hold.reschedule — the one owner
    of turning a saved request back into scheduled work, which never does it for
    a request interrupted by a restart. A request kept for the person's next
    message is a 'cancelled' row, so this does not touch it."""
    try:
        from mastercode.runtime.identity_hold import reschedule
        await reschedule(pool, entity_id, how="release")
    except Exception as e:  # noqa: BLE001
        log.warning("offline-task: releasing saved requests failed (non-fatal): %s", e)
    kick_poll()


def wake_is_live(entity_id: int, event_id: int) -> bool:
    """True when THIS process is running wake `event_id` right now: a task of
    the runner's own, or a registered wake turn. A pod is one process
    (server/launch.py), so a calendar row that says 'running' with neither is
    the leftover of a process that died; the orphan sweep resets it only after
    ORPHAN_AFTER_S."""
    try:
        if any(int(getattr(t, "_event_id", -1)) == int(event_id) and not t.done()
               for t in _RUNNING):
            return True
        from server.turn_registry import get_turn_registry
        return get_turn_registry().active_for(wakeup_thread_key(entity_id, event_id)) is not None
    except Exception:  # noqa: BLE001
        return True


def free_turn_slots(running: int = 0) -> int | None:
    """Turns the Host's memory holds now: every turn in the registry and every
    wake-up this loop has claimed (`running`, not all registered yet) counted
    at a turn's cost (host_memory.turns_that_fit). A claimed wake-up whose turn
    has registered is counted twice: never too few. None: no readable limit."""
    from mastercode.runtime import host_memory
    return host_memory.turns_that_fit(host_memory.in_flight_turns() + int(running))


# How long a task waits for its queued plan to land (PII mode): one minute
# per wait, this many waits, then it is refused rather than run unseen.
PLAN_WAIT_S = 60
PLAN_WAIT_MAX = 30


async def _wait_for_plan(pool, entity_id: int, event_id: int, waits: int) -> None:
    """Back to 'scheduled', a minute later, with the wait counted. Moving
    start_time off the head of the due queue is what keeps a task waiting on
    a stalled PII queue from being re-claimed every poll ahead of newer work.
    A recurring task re-arms from the moved time, so
    its schedule shifts by the minutes it waited."""
    try:
        await pool.execute(
            "UPDATE calendar_events SET status='scheduled', "
            "start_time = NOW() + make_interval(secs => $3), "
            "event_at = NOW() + make_interval(secs => $3), "
            "payload = jsonb_set(COALESCE(payload, '{}'::jsonb), '{plan_waits}', to_jsonb($4::int)), "
            "updated_at=NOW() "
            "WHERE id=$1 AND entity_id=$2 AND status='running'",
            event_id, entity_id, float(PLAN_WAIT_S), int(waits))
    except Exception as e:  # noqa: BLE001
        log.warning("offline-task: plan wait failed (event=%s): %s", event_id, e)


async def _plan_confirmed(pool, entity_id: int, event_id: int) -> None:
    """The plan landed: drop plan_event_id so later fires never look again."""
    try:
        await pool.execute(
            "UPDATE calendar_events SET payload = payload - 'plan_event_id' - 'plan_waits' "
            "WHERE id=$1 AND entity_id=$2",
            event_id, entity_id)
    except Exception as e:  # noqa: BLE001
        log.debug("offline-task: plan confirm write failed (event=%s): %s", event_id, e)


async def plan_state(pool, entity_id: int, plan_event_id: str) -> str:
    """'landed' when the plan entry schedule_task wrote is in the entity's
    turn rows, 'pending' while the PII queue may still land it, 'unreadable'
    when the lookup itself failed, 'missing' otherwise."""
    try:
        found = await pool.fetchval(
            "SELECT 1 FROM entity_memory WHERE entity_id=$1 AND memory_type='turn' "
            "AND metadata->>'scheduled_event_id' = $2 LIMIT 1",
            entity_id, plan_event_id)
    except Exception as e:  # noqa: BLE001 — unreadable is not "missing"
        log.warning("offline-task: plan lookup failed (plan=%s): %s", plan_event_id, e)
        return "unreadable"
    if found:
        return "landed"
    from mastercode import pii_queue as _pii
    if _pii.enabled() and _pii.stats().get("pending"):
        return "pending"
    return "missing"


def pointer_line(title: str, payload: dict, saved_tools: str = "tools") -> str:
    """The platform's words that open a fired wake-up: which wake-up, where
    it was set, and that the thread holds the plan."""
    if payload.get("saved_request") and payload.get("crash_unknown"):
        # Second line of defence: a request interrupted by a restart is never
        # scheduled (identity_hold.reschedule), but if one ever reaches a turn
        # it says so, rather than "do it now".
        return ("(This is the person's own message from this thread. The turn that had it was "
                "interrupted by a restart, so it may have been partly done before: check what "
                "exists before redoing any of it. Do only what remains, and report briefly.)")
    if payload.get("saved_request"):
        # A person's own message that arrived before this Host's tools had
        # loaded (runtime/identity_hold.py). It is already in the thread as
        # their message; it is run now, once, as the request it always was.
        # Worded by the state actually found, never assumed.
        if saved_tools == "chat_only":
            return ("(This is the person's own message from this thread. It arrived "
                    "before your identity had loaded, so it was kept. It has loaded, "
                    "and this Host has no tools: do what you can without them, and "
                    "say plainly which part needs tools. Report briefly.)")
        return ("(This is the person's own message from this thread. It arrived "
                "before your tools had loaded, so it was kept; they have loaded "
                "now. Do what it asks, now, and report briefly.)")
    _wake = payload.get("wake_on")
    if isinstance(_wake, dict) and _wake.get("event"):
        # A wake set to run when something HAPPENS (server/host_events.py):
        # the platform says which of the two brought it, so the Host never
        # has to guess whether the thing it waited for came.
        #
        # The deadline line says only what the platform knows: that no such
        # event was RECORDED. It never says the thing did not happen, because
        # a wake that could not be applied (emit gave up, host_events.py)
        # also runs at the deadline, and a Host told "it had not happened"
        # would tell the person they had not done what they had.
        from server.host_events import describe
        _plan = ("When you set it you wrote the plan into this thread; it is quoted below. "
                 if payload.get("plan_in_thread") else "What it asks follows. ")
        _woke = payload.get("woke_on")
        if isinstance(_woke, dict) and _woke.get("event"):
            _at = f" at {_woke.get('at')}" if _woke.get("at") else ""
            return (f"(The thing you were waiting for happened: "
                    f"{describe(str(_woke['event']), _woke.get('attrs'))}{_at}. Your wake-up "
                    f"**{title}** runs now because of it. {_plan}"
                    "Continue from it and report briefly.)")
        return (f"(Your deadline for **{title}** arrived, and no "
                f"{describe(str(_wake['event']), _wake.get('match'))} was recorded since "
                "you set it. That is not proof it did not happen: read its state now, "
                f"before you tell the person anything about it. {_plan}Then decide what "
                "that means and report briefly.)")
    if not payload.get("plan_in_thread"):
        # Set by a runbook channel or the calendar route, not by you in a
        # thread: say so, so the text below is not read as your own plan.
        # Its text was not written into a thread when it was set (a runbook
        # channel, the calendar route, a platform wake-up, or a task set
        # before plans were written). Say only what is known.
        return (f"(Scheduled wake-up **{title}** is due. What it asks follows. "
                "Report briefly in this thread.)")
    return (f"(Your scheduled wake-up **{title}** is due. When you set it you wrote "
            "the plan into this thread; it is quoted below. Continue from it and "
            "report briefly.)")


def payload_type_id(payload: dict) -> str | None:
    """The turn type a task was scheduled under, or None (untyped)."""
    v = (payload or {}).get("type_id")
    v = str(v).strip() if v is not None else ""
    return v or None

# ── Per-task spend ceiling on the PROMPT/STEPS lane (the governed-turn lane) ──
#
# PER_PASS_MAX_TASKS above caps how many tasks one TICK may claim; it says
# nothing about how often ONE task may fire. A recurring prompt task ran a full
# governed turn — model, tools, tokens — on every single fire, with no ceiling
# at all. A task set to every:60 is 1,440 model
# turns a day off one row nobody looks at again.
#
# WHERE 200 COMES FROM. It is a runaway backstop for one row nobody watches,
# set from the user's own schedules, never below them.
#   - The floor is the user's own schedule. The tightest recurrence anyone
#     reaches for in practice is a quarter-hour, which is 96 fires a day, and a
#     ceiling must never refuse what the user actually chose (caps protect the
#     platform, they do not throttle the user). 200 clears 96 with
#     better than 2x headroom, so every recurrence down to every:432 fits
#     without the user meeting this number at all.
#   - There is NO entity-wide daily call budget behind it. The per-task
#     number below is the only bound.
# A task that genuinely needs to fire more often says so — max_runs_per_day is
# in the payload and the authoring path refuses a recurrence that would exceed
# whatever value it carries, naming both numbers, rather than silently clipping.
DEFAULT_MAX_RUNS_PER_DAY = 200

# Prefix on the `error` column of a ceiling-skip run row. It is a MACHINE
# SENTINEL and never reaches the user's eyes: the message written into the
# thread is plain English (see _report_and_finalize's `skipped` branch), and
# this marker rides only on the stored column. Two readers key on it:
#   1. runs_in_last_24h EXCLUDES its own skips. Count every row for the event
#      and the skips keep the trailing-24h count at the ceiling forever —
#      the task would never run again, which is a latch, not a ceiling.
#   2. The Agents panel (server/routes/agents.py) subtracts them from a task's
#      FAILURE count. A ceiling skip is the governor working, not the task
#      breaking, and counting it as a failure made a healthy task look sick.
CEILING_SKIP_PREFIX = "ScheduledTurnCeiling:"

# How much assistant text to retain on the run record (audit, not the full
# output — the full output lives in the Circles turn row).
OUTPUT_EXCERPT_MAX = 2000


def _next_occurrence(start_time: datetime, period: int) -> datetime:
    """Advance start_time by whole periods until strictly after now (bounded
    catch-up guard)."""
    now = datetime.now(timezone.utc)
    step = timedelta(seconds=period)
    new_start = start_time
    for _ in range(10000):  # bounded; >27y of hourly catch-up
        new_start = new_start + step
        if new_start > now:
            break
    return new_start


async def _record_run(pool, event_id: int, entity_id: int, ok: bool,
                      output_excerpt: str | None, error: str | None,
                      cost_usd: float | None = None,
                      priced_usd: float | None = None) -> None:
    """Append a calendar_event_runs child row. Best-effort — a failed run
    record must never propagate out of the per-task handler.

    cost_usd is the run's whole price, or None when any turn had none;
    priced_usd is what its priced turns cost even then (v251), the figure a
    job's budget counts (budget_state)."""
    try:
        await pool.execute(
            """
            INSERT INTO calendar_event_runs
                (event_id, entity_id, ran_at, ok, output_excerpt, error, cost_usd,
                 priced_usd)
            VALUES ($1, $2, NOW(), $3, $4, $5, $6, $7)
            """,
            event_id, entity_id, ok,
            (output_excerpt or None), (error or None), cost_usd, priced_usd,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("offline-task: run-record insert failed (event=%s): %s",
                    event_id, e)


def person_turns_per_day() -> int:
    """The person's limit `scheduled.turns_per_day` (runtime/user_limits):
    their value when they set one (0 = no limit), else DEFAULT_MAX_RUNS_PER_DAY.
    A new task may ask for less, never more (tools._exec_schedule_task)."""
    from mastercode.runtime import user_limits
    return user_limits.person_or("scheduled.turns_per_day", DEFAULT_MAX_RUNS_PER_DAY)


def max_runs_per_day(payload: dict) -> int:
    """This task's governed-turn ceiling: the value its payload carries, else
    the person's limit (person_turns_per_day).

    A payload value is honoured as it is, including one set above today's
    limit before the limit was the person's (2026-10-20): cutting a task the
    person relied on would break it without asking. New tasks cannot carry
    one above the limit: both writers of a task's payload refuse it (the
    Host's schedule_task, and the calendar append behind POST /api/calendar).

    A value at or below zero, or one that is not a number, means the payload
    never set one — fall back rather than treat garbage as "no runs allowed",
    which would silently stop a task the user is relying on.
    """
    raw = (payload or {}).get("max_runs_per_day")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return person_turns_per_day()
    return n if n > 0 else person_turns_per_day()


def _num_or_none(raw) -> int | None:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _ceiling_error(message: str) -> str:
    """The stored form of a ceiling-skip reason: the machine sentinel, then the
    plain sentence. The sentinel is for the two queries that key on it; the user
    never sees this string — the reported prose is the message alone."""
    return f"{CEILING_SKIP_PREFIX} {message}"


def turns_per_fire(payload: dict) -> int:
    """How many governed turns ONE fire of this task costs.

    This is the whole reason the ceiling counts turns rather than fires. The
    steps lane runs one full turn PER STEP (_run_one's `for i, step in
    enumerate(steps)` loop calls _run_turn once each), so a 20-step task at
    every:900 fires 96 times a day and spends 1,920 turns. A fire-counting
    ceiling of 200 waves that through; every user-facing text calls the budget
    "model turns", and it has to mean it.

    A prompt task, and a task that falls back to its title, cost 1.
    """
    raw = (payload or {}).get("steps")
    if isinstance(raw, list):
        n = len([s for s in raw if str(s).strip()])
        if n > 0:
            return n
    return 1


async def runs_in_last_24h(pool, event_id: int, entity_id: int) -> int:
    """How many times this task has ACTUALLY run in the trailing 24 hours.

    Counts real attempts only: a ceiling skip writes a run row too (so the user
    can see why nothing happened), and counting those would latch the ceiling on
    permanently. Fails OPEN — a counting error must not stop the user's task, so
    an unreadable count reads as 0 and the turn goes ahead.
    """
    try:
        n = await pool.fetchval(
            """
            SELECT COUNT(*)
              FROM calendar_event_runs
             WHERE event_id = $1
               AND entity_id = $2
               AND ran_at >= NOW() - INTERVAL '24 hours'
               AND (error IS NULL OR error NOT LIKE $3)
            """,
            event_id, entity_id, CEILING_SKIP_PREFIX + "%",
        )
        return int(n or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("offline-task: 24h run count failed (event=%s): %s — "
                    "letting the turn proceed", event_id, e)
        return 0


async def _first_skip_in_window(pool, event_id: int, entity_id: int) -> bool:
    """True when no ceiling skip has been recorded for this task in the trailing
    24 hours — i.e. this is the first skip of the current ceiling window, and the
    one the user should hear about. Fails QUIET (False) rather than risk turning
    a counting error into a message every poll."""
    try:
        n = await pool.fetchval(
            """
            SELECT COUNT(*)
              FROM calendar_event_runs
             WHERE event_id = $1
               AND entity_id = $2
               AND ran_at >= NOW() - INTERVAL '24 hours'
               AND error LIKE $3
            """,
            event_id, entity_id, CEILING_SKIP_PREFIX + "%",
        )
        return int(n or 0) == 0
    except Exception as e:  # noqa: BLE001
        log.warning("offline-task: skip-count failed (event=%s): %s", event_id, e)
        return False


async def _finalize(pool, event_id: int, entity_id: int,
                    recurrence: str | None, start_time, ok: bool) -> None:
    """Re-arm a recurring task to its next occurrence (status back to
    'scheduled'), or mark a one-shot task 'completed'/'failed'. Best-effort.
    A task cancelled while it ran stays cancelled: deleting the thread it was
    set in cancels it mid-run (circles_threads.delete_thread)."""
    period = _recurrence_seconds(recurrence) if recurrence else None
    try:
        if period and start_time is not None:
            new_start = _next_occurrence(start_time, period)
            res = await pool.execute(
                """
                UPDATE calendar_events
                   SET status='scheduled', start_time=$2, event_at=$2,
                       updated_at=NOW(),
                       payload = COALESCE(payload, '{}'::jsonb) - 'identity_defers'
                 WHERE id=$1 AND entity_id=$3 AND status <> 'cancelled'
                """,
                event_id, new_start, entity_id,
            )
            if str(res).endswith(" 0"):
                log.info("offline-task: recurring #%d was cancelled while it ran "
                         "— not re-armed", event_id)
            else:
                log.info("offline-task: re-armed recurring #%d -> %s (%s)",
                         event_id, new_start.isoformat(), recurrence)
        else:
            final = "completed" if ok else "failed"
            await pool.execute(
                "UPDATE calendar_events SET status=$2, updated_at=NOW() "
                "WHERE id=$1 AND entity_id=$3 AND status <> 'cancelled'",
                event_id, final, entity_id,
            )
    except Exception as e:  # noqa: BLE001
        # Leave the row 'running' rather than crash; an operator can re-set it.
        log.warning("offline-task: finalize failed (event=%s): %s", event_id, e)


class TaskCancelled(Exception):
    """The task's row went 'cancelled' while it was running."""


class BudgetReached(Exception):
    """The job's own budget (payload budget_usd) was reached between steps."""


async def budget_state(pool, event_id: int, entity_id: int,
                       payload: dict) -> tuple[float | None, float]:
    """(the job's budget in dollars or None, what its priced runs have spent).
    The budget is the one the person named for this job ("spend up to $5"),
    stored by schedule_task as payload budget_usd. Runs with no price (a
    subscription plan, a local model) add nothing: the budget can only count
    what was priced, and the skill says so to the person. A run with SOME
    priced turns counts them (priced_usd): its cost_usd is null so it never
    reads as whole, and dropping its priced turns too would let a job spend
    past the budget the person set."""
    try:
        budget = float((payload or {}).get("budget_usd"))
    except (TypeError, ValueError):
        return None, 0.0
    if not budget > 0:
        return None, 0.0
    try:
        spent = await pool.fetchval(
            "SELECT COALESCE(sum(COALESCE(priced_usd, cost_usd)), 0) FROM calendar_event_runs "
            "WHERE event_id = $1 AND entity_id = $2", event_id, entity_id)
    except Exception as e:  # noqa: BLE001 — unread, the run goes ahead
        log.warning("offline-task: budget spend unread (event=%d): %s", event_id, e)
        return budget, 0.0
    return budget, float(spent or 0.0)


async def _task_cancelled(pool, entity_id: int, event_id: int) -> bool:
    """True when the task's row reads 'cancelled'. A failed read is False: a
    task is never stopped on a read error."""
    try:
        return (await pool.fetchval(
            "SELECT status FROM calendar_events WHERE id=$1 AND entity_id=$2",
            event_id, entity_id)) == "cancelled"
    except Exception as e:  # noqa: BLE001
        log.debug("offline-task: status read failed (event=%s): %s", event_id, e)
        return False


async def _report_and_finalize(pool, entity_id: int, event_id: int, title: str,
                               recurrence, start_time, ok: bool, content: str,
                               error: str | None, *, report_on_success: bool,
                               origin: dict | None = None,
                               skipped: bool = False,
                               stopped: bool = False,
                               cost_usd: float | None = None,
                               priced_usd: float | None = None) -> None:
    """Shared tail for both lanes: tell the user, record the run, re-arm.

    `report_on_success` is the one difference between them, and it is a product
    decision rather than a technical one. A PROMPT task's output IS the
    deliverable, so it posts every time. A standing OP posts only when it
    FAILS: a deterministic op that fires hourly and works would otherwise write
    a chat message every hour, which is noise, and noise is how a user learns
    to stop reading. Success is visible where it belongs — the
    calendar_event_runs history, surfaced by `calendar_read op='runs'`.

    A failure always speaks. Never silent, in either lane.

    WHERE it speaks is `origin` (payload.origin, written by
    _exec_schedule_task from the thread the timer was set in). This was
    hardcoded to the Circles self-thread, so a host that said "I'll check on
    the bot in twenty minutes" in a Workbench project thread answered into a
    different room. A timer owes its answer to the conversation that asked for
    it. With no thread recorded (a task set in the old original conversation,
    the platform's own standing tasks, the Circles compose modal) the report
    starts a topic of its own, titled from the task, and the task keeps it
    (ensure_origin_thread): there is no original conversation to fall back to.

    A task cancelled while it ran speaks nowhere: deleting the thread it was
    set in cancels it (circles_threads.delete_thread). The run is still
    recorded.
    """
    if await _task_cancelled(pool, entity_id, event_id):
        log.info("offline-task: #%d was cancelled while it ran — no report", event_id)
    elif report_on_success or not ok:
        if ok and content:
            report = content
        elif stopped:
            # The job's own budget was reached (budget_state): not a failure,
            # and nothing more of it runs.
            report = f"Scheduled task **{title}** stopped: {error}"
        elif skipped:
            # A ceiling skip is the governor working, NOT the task breaking,
            # and the user is owed the difference in the word itself. This
            # also keeps CEILING_SKIP_PREFIX out of prose: the sentinel is a
            # machine marker for two queries, and `error` here is already the
            # plain sentence (the caller stores the marked form separately).
            report = f"Scheduled task **{title}** was skipped — {error}"
        else:
            report = (f"Scheduled task **{title}** failed: {error}"
                      if error else f"Scheduled task **{title}** produced no output.")
        # What the run spent, said where the person reads its result: work
        # that runs unwatched must not spend unseen (v251).
        if cost_usd is not None:
            report += f"\n\n(This run cost ${cost_usd:.4f}.)"
        await post_to_origin_thread(pool, entity_id, origin, report,
                                    extra_tags={"scheduled_task_id": event_id},
                                    fallback_session=f"scheduled_task:{event_id}",
                                    event_id=event_id, title=title)

    excerpt = (content or "")[:OUTPUT_EXCERPT_MAX] or None
    # The STORED reason carries the sentinel; the REPORTED prose above never
    # does. runs_in_last_24h and the Agents panel's failure count both key on it.
    stored_error = _ceiling_error(error) if (skipped and error) else error
    # A job stopped at the budget the person set did what they asked: the run
    # is recorded as done (the reason kept beside it) and the task completes
    # rather than reading as failed. The caller passes recurrence=None.
    done = ok or stopped
    await _record_run(pool, event_id, entity_id, done, excerpt, stored_error, cost_usd,
                      priced_usd)
    await _finalize(pool, event_id, entity_id, recurrence, start_time, done)


def lands_in_circles(origin: dict | None) -> bool:
    """Whether what the scheduler writes for `origin` goes to a Circles
    conversation: no project, and an interaction context of none, chat or
    circles, or a thread named. The same mapping post_to_origin_thread makes
    for the row's room, so the two cannot disagree."""
    o = origin if isinstance(origin, dict) else {}
    if o.get("project_id") is not None:
        return False
    ic = str(o.get("interaction_context") or "").strip()
    if ic.startswith("project:"):
        return False
    return ic in ("", "chat", "circles") or bool(str(o.get("thread_id") or "").strip())


async def ensure_origin_thread(pool, entity_id: int, origin: dict | None, *, title: str,
                               event_id=None) -> dict:
    """`origin` with a Circles thread in it, when what it names lands in
    Circles. There is no original conversation: a task
    that names no thread (set in the old original conversation, or seeded by
    the platform with no origin) gets a topic of its own, titled from the
    task, the first time it writes. With `event_id` the topic is recorded in
    the task's origin, so the turn, its report and every later run share
    that one thread. A task that names a thread keeps it, deleted or not: a
    deleted thread's tasks are cancelled (delete_thread, and the refusal at
    fire time), never re-homed. Anything that does not land in Circles comes
    back unchanged."""
    o = dict(origin) if isinstance(origin, dict) else {}
    if not lands_in_circles(o):
        return o
    if str(o.get("thread_id") or "").strip():
        return o
    from mastercode.circles_threads import home_thread
    tid = await home_thread(pool, entity_id, None, title=title)
    if tid is None:
        return o
    o["thread_id"] = tid
    if not str(o.get("interaction_context") or "").strip():
        o["interaction_context"] = "chat"
    if event_id is not None:
        try:
            import json as _json
            await pool.execute(
                "UPDATE calendar_events SET updated_at = NOW(),"
                "       payload = jsonb_set(COALESCE(payload, '{}'::jsonb), '{origin}', $3::jsonb, true)"
                " WHERE id = $1 AND entity_id = $2",
                event_id, entity_id, _json.dumps(o))
        except Exception as e:  # noqa: BLE001 - this run still lands in the topic
            log.warning("offline-task: recording task #%s's topic failed: %s", event_id, e)
    return o


async def post_to_origin_thread(pool, entity_id: int, origin: dict | None, text: str, *,
                                extra_tags: dict | None = None,
                                fallback_session: str = "scheduled_task",
                                event_id=None, title: str | None = None) -> bool:
    """Write one assistant row into the thread `origin` names: the Circles
    thread (origin.thread_id), a project, or the Workbench General thread.
    The one writer for everything the scheduler says in a thread: what is
    planned when a task is set (turn types: the thread is the chain of
    thought, never a hidden note) and what came of it when it fires. A Circles
    origin with no live thread gets a topic first (ensure_origin_thread; with
    `event_id` the task keeps it); nothing is written to the legacy app:circles
    scope. Best-effort; False on failure."""
    from mastercode.circles_threads import home_title
    _o = await ensure_origin_thread(pool, entity_id, origin,
                                    title=title or home_title(text, "Scheduled task"),
                                    event_id=event_id)
    _project_id = _o.get("project_id")
    try:
        _project_id = int(_project_id) if _project_id is not None else None
    except (TypeError, ValueError):
        _project_id = None
    _thread_id = str(_o.get("thread_id") or "").strip() or None
    if lands_in_circles(_o) and not _thread_id:
        log.warning("offline-task: no Circles thread for a write (entity %s); not written",
                    entity_id)
        return False
    # tags.app_id decides the room: circles' _read_self_thread reads
    # app_id="circles"; the project chat pane reads project_id +
    # source_context. scheduled_task_id ties the row to the task either way.
    #
    # Derived from the interaction context, NOT from "is there a project" —
    # the Workbench General thread is interaction_context="workbench" with
    # project_id NULL, and it reads back as app_id=workbench AND
    # project_id IS NULL, so keying on the project alone sent a timer set
    # in General to Circles instead. Same mapping derive_source_context
    # makes just below, so the tag and the descriptor cannot disagree.
    _ic = (_o.get("interaction_context") or "").strip()
    if _project_id is not None or _ic.startswith("project:"):
        _app_id = "workbench"
    elif _ic in ("", "chat", "circles") or _thread_id:
        _app_id = "circles"
    else:
        _app_id = _ic
    try:
        from datetime import datetime, timezone
        from mastercode.memory_engine import (
            derive_source_context, write_turn_row,
        )
        _tags = {"app_id": _app_id, **(extra_tags or {})}
        try:
            _tags["source_context"] = derive_source_context(
                _o.get("interaction_context")
                or (f"project:{_project_id}" if _project_id is not None
                    else "circles"),
                project_id=_project_id,
                session_id=_o.get("session_id"),
                thread_id=_thread_id,
            )
        except Exception as _se:  # noqa: BLE001 — the row still lands
            log.debug("offline-task: source_context derive failed: %s", _se)
        _row_id = await write_turn_row(
            pool, entity_id,
            _o.get("session_id") or fallback_session,
            "assistant", text,
            project_id=_project_id,
            tags=_tags,
            # The utterance time. write_turn_row's own contract: omitted,
            # created_at falls back to the INSERT clock after a
            # variable-latency embed, and the chat surfaces sort strictly
            # on that column.
            created_at=datetime.now(timezone.utc),
        )
        if _row_id is not None:
            return True
        # write_turn_row logs and returns None on a failed insert. In PII
        # mode it also returns None on success: the row is queued and lands
        # tokenized later (pii_queue.submit), so there None means "queued".
        from mastercode import pii_queue as _pii
        if _pii.enabled():
            return True
        log.warning("offline-task: thread write did not land (app=%s)", _app_id)
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("offline-task: thread write failed (app=%s): %s", _app_id, e)
        return False


async def _run_op(pool, entity_id: int, tool: str, args: dict,
                  origin_context: str | None = None,
                  turn_type: dict | None = None) -> tuple[bool, str, str | None]:
    """Fire ONE deterministic op — no model, no turn, no tokens.

    This is the standing-work lane. `run_direct_op` was already
    written for a caller that is not a chat surface — its own docstring says
    "no surface gate (these are background-caller ops, not a chat surface);
    CEIGAS still gates inside" — so the scheduler joining to it is the intended
    shape, not a bypass. Authorization is unchanged and still fires per call.

    THE FITNESS CHECK HERE IS THE REAL ONE, not the copy in schedule_task.
    `_exec_schedule_task` is not the only writer of a scheduled_task payload:
    POST /api/calendar (server/routes/calendar.py) wraps handle_calendar_append
    directly for the Circles compose modal and the privatae-core PME dispatch
    path, so a check placed only in the tool is bypassable by that route.
    Authoring-time validation is UX fail-fast; this is the guard.

    Returns (ok, content, error) in the same shape the turn path produces.
    """
    from mastercode.runtime.direct_ops import is_unattended_op_tool, run_direct_op

    if not is_unattended_op_tool(tool):
        # Not a refusal on safety grounds — see direct_ops.UNATTENDED_OP_TOOLS.
        return False, "", (
            f"UnattendedOpRefused: `{tool}` is not schedulable — it does not "
            "complete without someone present (its result needs a person to "
            "act on it). Schedule an op that finishes on its own, or run this "
            "one in a turn."
        )

    try:
        # The origin thread rides through: an op lane that schedules another
        # timer (schedule_task is itself a schedulable op) would otherwise
        # create an origin-less row and answer in the wrong room.
        # The task's turn type: the op meets the same wall a typed
        # turn's own call does. None = untyped, as before.
        res = await run_direct_op(tool, args, entity_id=entity_id, pool=pool,
                                  origin_context=origin_context, turn_type=turn_type)
    except Exception as e:  # noqa: BLE001
        return False, "", f"{type(e).__name__}: {e}"

    # run_direct_op returns {"status": ok|denied|error, "result": ...} — it
    # does not raise on a denial, so a denied op must become a FAILED run
    # rather than a silent success (that is the whole point of the run record).
    status = str((res or {}).get("status") or "").strip().lower()
    payload = (res or {}).get("result")
    if isinstance(payload, str):
        text = payload
    else:
        import json as _json
        try:
            text = _json.dumps(payload, default=str, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            text = repr(payload)

    if status == "ok":
        return True, f"Ran `{tool}` — ok.\n{text}".rstrip(), None
    return False, "", f"{status or 'error'}: {text}"


def _untrusted_kw(payload) -> dict:
    """send_message's untrusted_input for a task whose payload was written in
    a turn that read untrusted content (turn_taint.stamp at schedule_task), or
    nothing for a clean one."""
    src = (payload or {}).get("untrusted") if isinstance(payload, dict) else None
    if not src:
        return {}
    return {"untrusted_input": f"a task scheduled in a turn that read untrusted content ({str(src)[:120]})"}


async def _run_one(pool, entity_id: int, row) -> None:
    """Run a single CLAIMED task end-to-end. Guarded by the caller's loop, but
    fully self-contained: any failure is recorded + the task finalized so it
    never sticks in 'running'."""
    event_id = row["id"]
    title = row["title"] or "scheduled task"
    recurrence = row["recurrence"]
    start_time = row["start_time"]
    payload = row["payload"] or {}
    if isinstance(payload, str):
        try:
            import json as _json
            payload = _json.loads(payload)
        except Exception:  # noqa: BLE001
            payload = {}

    prompt = (payload.get("prompt") or payload.get("input") or "").strip()
    # Runbook STEPS (s1-runbook): if the payload carries a non-empty list of
    # steps, run them IN SEQUENCE — one governed turn per step — and the report
    # is the labelled concatenation of every step's output. Otherwise fall back
    # to the single-prompt behavior (unchanged).
    raw_steps = payload.get("steps")
    steps = [str(s).strip() for s in raw_steps if str(s).strip()] \
        if isinstance(raw_steps, list) else []

    # Standing-work payload: {"op": {"tool": …, "args": {…}}} runs
    # ONE deterministic op with no model at all. Read before the title fallback
    # below, and the fallback is guarded on it — otherwise an op payload landing
    # on a pod without this branch would fall through to `Run the
    # scheduled task: <title>`, burn a full governed turn, and record ok=true.
    # A green tick over exactly the anti-pattern this lane exists to remove.
    raw_op = payload.get("op")
    op_tool = ""
    op_args: dict = {}
    if isinstance(raw_op, dict):
        op_tool = str(raw_op.get("tool") or "").strip()
        _raw_args = raw_op.get("args")
        op_args = _raw_args if isinstance(_raw_args, dict) else {}

    if not op_tool and not prompt and not steps:
        # Fall back to the title so a bare scheduled task still does something
        # meaningful rather than being recorded as an empty no-op.
        prompt = f"Run the scheduled task: {title}"

    # WHERE this timer was set. Read once, used by both lanes and by the
    # report: the fired turn runs in this thread, an op the turn schedules
    # inherits it, and the answer is written back into it.
    _origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
    _origin_project = _origin.get("project_id")
    try:
        _origin_project = int(_origin_project) if _origin_project is not None else None
    except (TypeError, ValueError):
        _origin_project = None

    ok = False
    content = ""
    error = None

    # The turn type the task was scheduled under. Checked HERE, at
    # fire time, before either lane runs — the tool is not the only writer of
    # a scheduled_task payload (POST /api/calendar), so this is the guard. A
    # type that is no longer active fails the run with nothing spent: the task
    # was scheduled narrow, and running it with everything the entity holds is
    # the widening this step exists to prevent.
    _type_id = payload_type_id(payload)
    _task_type = None
    if _type_id:
        from mastercode.runtime.turn_types import load_turn_type_by_id
        _task_type = await load_turn_type_by_id(pool, entity_id, _type_id)
        if _task_type.get("status") != "active":
            error = (f"Nothing ran: {_task_type.get('closed_reason')}. It will not run "
                     "again — schedule it anew under an active type, or ask the user.")
            log.warning("offline-task: type not active (event=%d type=%s)", event_id, _type_id)
            # recurrence=None: a recurring task under a type that is gone is
            # stopped, not re-armed to fail and post again every period.
            await _report_and_finalize(pool, entity_id, event_id, title, None,
                                       start_time, False, "", error,
                                       report_on_success=False,
                                       origin=payload.get("origin"))
            return

    # The type this run works under, for anything it records (an agent or
    # build an op starts stamps it, v186). This is the wake-up's own task
    # (_guarded_run), so the value never reaches another wake-up. The turn
    # lane's session sets it again from the pinned type.
    try:
        from mastercode.runtime.turn_types import set_current_turn_type
        set_current_turn_type(_task_type)
    except Exception as _ct_err:  # noqa: BLE001 - a record never fails a run
        log.debug("offline-task: current turn type not recorded: %s", _ct_err)

    # The plan must be in the thread before the task runs (turn types: the
    # thread is the chain of thought). schedule_task writes it first; it sets
    # plan_event_id only when that write was QUEUED (PII mode), because the
    # queue can drop it, so the entry is looked for here. Landed: the id is
    # cleared and never looked up again. Not landed yet: the task waits a
    # minute, moved off the head of the due queue so it starves nothing, at
    # most PLAN_WAIT_MAX times. Gone, or never confirmed: nothing runs, the
    # thread says so, and it stops.
    _plan_eid = str(payload.get("plan_event_id") or "").strip()
    if _plan_eid:
        _plan = await plan_state(pool, entity_id, _plan_eid)
        _waits = int(payload.get("plan_waits") or 0)
        if _plan == "landed":
            await _plan_confirmed(pool, entity_id, event_id)
        elif _plan in ("pending", "unreadable") and _waits < PLAN_WAIT_MAX:
            log.info("offline-task: plan not confirmed yet (event=%d state=%s wait=%d) — "
                     "waiting", event_id, _plan, _waits + 1)
            await _wait_for_plan(pool, entity_id, event_id, _waits + 1)
            return
        else:
            error = ("Nothing ran: the plan for this task never reached the thread, "
                     "and a task runs only what the thread shows. Schedule it again."
                     if _plan != "unreadable" else
                     "Nothing ran: whether this task's plan reached the thread could not "
                     "be checked, and a task runs only what the thread shows. "
                     "Schedule it again.")
            await _report_and_finalize(pool, entity_id, event_id, title, None,
                                       start_time, False, "", error,
                                       report_on_success=False,
                                       origin=payload.get("origin"))
            return

    # A request saved by the identity hold (runtime/identity_hold.py): before it
    # runs, two checks. A newer message from the person in its thread
    # supersedes it, so it is skipped with one line saying so. And its words
    # about the Host's tools are chosen by the state actually found; if the
    # tools are still not there (or the seed has not resolved), it is put back.
    _saved_tools = "tools"
    _saved_earlier: list = []
    _saved_newer = False
    if payload.get("saved_request"):
        from mastercode.runtime import identity_hold as _ih
        if await _saved_request_superseded(pool, entity_id, payload):
            # The person said something newer in this thread. The takeover
            # normally cancels this wake before it gets here; reaching here
            # means that did not happen (it was mid-check, or the update
            # failed). Never claim the request was carried: fold it into the
            # newest held request if there is one, else run it with the newer
            # messages in front of the model, so it is never dropped.
            if await _fold_into_newest(pool, entity_id, payload, event_id):
                await _report_and_finalize(pool, entity_id, event_id, title, None,
                                           start_time, True, _ih.FOLD_LINE, None,
                                           report_on_success=True,
                                           origin=payload.get("origin"))
                return
            _saved_newer = True
        _saved_tools = await _saved_request_tools_state(pool, entity_id)
        # The words: a reference to the person's saved row (PII mode lands that
        # row tokenized, through a queue — until it has landed there is
        # nothing to run, so the wake waits like it does for the tools).
        _text = await _ih.saved_request_text(pool, entity_id, payload) \
            if _saved_tools != "pending" else None
        if _saved_tools != "pending" and _text is None:
            _saved_tools = "pending"
        if _saved_tools == "pending":
            _tries = int(payload.get("identity_defers") or 0)
            if _tries < IDENTITY_DEFER_MAX:
                log.info("offline-task: #%d waits — the Host's tools (or the saved "
                         "words) have not arrived (try %d, next in %ds)", event_id,
                         _tries + 1, identity_defer_delay(_tries + 1))
                await _defer_for_identity(pool, entity_id, event_id, _tries + 1)
                return
            await _report_and_finalize(
                pool, entity_id, event_id, title, None, start_time, False, "",
                "Nothing ran: this Host's tools never arrived after "
                f"{_tries} tries over several hours, so the request was not "
                "carried out.", report_on_success=False, origin=payload.get("origin"))
            return
        prompt = _text
        _saved_earlier = await _ih.resolve_locators(pool, entity_id,
                                                    list(payload.get("earlier") or []))

    if op_tool:
        # The deterministic lane. Never reaches brain_service.send_message, so
        # a refusal or a failure here must NOT fall through to a model turn —
        # that would turn "this op is not schedulable" into a token spend.
        ok, content, error = await _run_op(
            pool, entity_id, op_tool, op_args,
            origin_context=(_origin.get("interaction_context")
                            or (f"project:{_origin_project}"
                                if _origin_project is not None else None)),
            turn_type=_task_type)
        if not ok:
            log.warning("offline-task: op run failed (event=%d tool=%r): %s",
                        event_id, op_tool, error)
        await _report_and_finalize(pool, entity_id, event_id, title, recurrence,
                                   start_time, ok, content, error,
                                   report_on_success=False,
                                   origin=payload.get("origin"))
        return

    # ── The spend ceiling. TURN LANE ONLY, and deliberately below the op-lane
    # return above: the op lane runs no model and spends no tokens, so there is
    # nothing for a spend ceiling to protect there and capping it would only
    # break standing work that costs nothing.
    #
    # Enforced HERE, at FIRE time, rather than at authoring time alone, for the
    # reason _run_op's docstring already gives about its own fitness check:
    # _exec_schedule_task is not the only writer of a scheduled_task payload —
    # POST /api/calendar wraps handle_calendar_append directly — so a check that
    # lives only in the tool is bypassable by that route.
    # The budget is in TURNS, because that is what costs money and what every
    # text the user reads calls it. One fire of a steps task is len(steps)
    # turns, so the count of fires is multiplied up rather than used raw.
    from mastercode.runtime import user_limits
    await user_limits.fresh(pool, entity_id)
    ceiling = max_runs_per_day(payload)
    _own_ceiling = _num_or_none((payload or {}).get("max_runs_per_day")) is not None
    per_fire = turns_per_fire(payload)
    fires = await runs_in_last_24h(pool, event_id, entity_id)
    spent = fires * per_fire
    # "Don't START a fire you cannot afford": a 20-turn fire with 10 turns of
    # budget left would blow past the ceiling by 10. `spent` is used rather than
    # `spent + per_fire > ceiling` alone so the FIRST fire of a window always
    # runs — a task whose single fire costs more than its whole ceiling would
    # otherwise never run at all, which is a dead task, not a governed one. The
    # authoring path refuses that combination up front; this is the floor under
    # payloads that did not come through it.
    if spent and spent + per_fire > ceiling:
        _each = (f" Each time it fires it runs {per_fire} turns, one per step."
                 if per_fire > 1 else "")
        _msg = (f"it has already used {spent} of the {ceiling} model turns it is "
                f"allowed in a day.{_each} Nothing was run this time and nothing "
                f"was spent. It will try again at its next scheduled time" +
                (f" ({recurrence})." if recurrence else ".") +
                (" The task carries its own ceiling (max_runs_per_day); the Host can "
                 "set it again, up to the person's limit `scheduled.turns_per_day`."
                 if _own_ceiling else " " + user_limits.hit_line("scheduled.turns_per_day")))
        if not _own_ceiling:
            await user_limits.record_hit(pool, entity_id, "scheduled.turns_per_day",
                                         f"a run of the scheduled task {title!r}")
        log.warning("offline-task: ceiling reached (event=%d spent=%d/%d turns, "
                    "%d per fire) — skipping the governed turn",
                    event_id, spent, ceiling, per_fire)
        # Say it ONCE per ceiling window. A quarter-hourly task that has hit its
        # ceiling would otherwise post the same message every 15 minutes, and
        # noise is how a user learns to stop reading (_report_and_finalize's own
        # reasoning for report_on_success=False on the op lane).
        speak = await _first_skip_in_window(pool, event_id, entity_id)
        if speak:
            await _report_and_finalize(pool, entity_id, event_id, title, recurrence,
                                       start_time, False, "", _msg,
                                       report_on_success=False,
                                       origin=payload.get("origin"), skipped=True)
        else:
            await _record_run(pool, event_id, entity_id, False, None,
                              _ceiling_error(_msg))
            # Re-arm on the recurrence rule and nothing faster: a skipped fire
            # must not become a tighter poll on the next one.
            await _finalize(pool, event_id, entity_id, recurrence, start_time, False)
        return

    # The job's own budget (payload budget_usd), when the person named one:
    # reached, the task stops for good and says so, with what it spent.
    _budget, _budget_spent = await budget_state(pool, event_id, entity_id, payload)
    if _budget is not None and _budget_spent >= _budget:
        _msg = (f"it has spent ${_budget_spent:.2f} of the ${_budget:.2f} budget you set "
                "for it, so it stops here. Nothing was run this time.")
        await _report_and_finalize(pool, entity_id, event_id, title, None,
                                   start_time, False, "", _msg,
                                   report_on_success=False,
                                   origin=payload.get("origin"), stopped=True)
        return

    # Steps already run this fire. A full process may only REQUEUE a task
    # that ran nothing yet; once a step has run, requeueing would run it
    # again — messages, ops, uncounted turns.
    _steps_done: list[str] = []
    # What this fire's turns cost, from each turn's context frame (the
    # estimate its turn row is stamped with). Unpriced as soon as one turn
    # has no price: a partial sum would read as the whole.
    _fire_cost = {"usd": 0.0, "turns": 0, "priced": True}
    _budget_stop = False
    try:
        # Headless governed turn — the FULL host pipeline (identity + system
        # prompt + resolved chat-role model + CEIGAS fail-closed inference gate +
        # tool loop), no autonomous-brain boot. brain_service.send_message is a
        # MODULE-LEVEL function that STREAMS AI-SDK Data Stream frames (it is an
        # async generator, NOT an awaitable returning a dict): reconstruct the
        # assistant text from the 0:"<delta>" text frames; 3:"<msg>" is a terminal
        # error. session_id=None drives the transient no-sessions chat path.
        from server import brain_service
        if brain_service.get_runtime() is None:
            raise RuntimeError("EntityRuntime not initialized")
        import json as _json

        # The turn runs in a thread, always. A Circles task with no live
        # thread (set in the old original conversation, seeded by the
        # platform, or set in a thread since deleted) gets its topic here,
        # before its first turn, and keeps it: the turn, its report and every
        # later run share it.
        _origin = await ensure_origin_thread(pool, entity_id, _origin, title=title,
                                             event_id=event_id)
        payload["origin"] = _origin

        async def _run_turn(message: str) -> str:
            """One governed turn — IDENTICAL send_message call style to the
            single-prompt path; reconstruct text from the 0:/3: frames.

            Checked before EVERY turn, the first included: deleting the thread
            the task was set in cancels it (circles_threads.delete_thread),
            and a steps task must not run its remaining steps, nor a task
            claimed a moment before the delete start its turn at all."""
            if await _task_cancelled(pool, entity_id, event_id):
                raise TaskCancelled(f"#{event_id} was cancelled")
            parts: list[str] = []
            stream_err = None
            pending = False
            no_model = False
            # The fired turn runs IN the thread the timer was set in. Without
            # this it ran under app_id="scheduled_task" with no project, which
            # broke the chain on its second hop: the first check finds the app
            # still booting, the host says it will look again, and THAT timer
            # has no origin to inherit — so the follow-up reports into Circles
            # while the user waits in the project thread. It also scopes the
            # turn's own recall to the project, so the check is answered with
            # the conversation that asked for it.
            async for frame in brain_service.send_message(
                entity_id=entity_id,
                message=message,
                session_id=None,
                # The write tag STAYS "scheduled_task" — that is the hidden
                # lane this runner exists to keep the fired turn on. Running
                # it as app_id=workbench/project_id=N (the first cut of this
                # fix) put it on the normal chat write path, which wrote the
                # timer's own prompt into the thread as a USER message and
                # then the reply twice — once by the turn, once by the report.
                app_id="scheduled_task",
                # ...while the turn THINKS in the thread it was set in. This
                # is the replay/A-B mechanism (session.py's transient builder:
                # history_context overrides interaction_context while app_id
                # keeps its own write tag), which is exactly this shape. It
                # fixes the chain for EVERY origin, not just projects: a
                # Circles or General-thread timer got app_id="scheduled_task"
                # from the project-only branch, so its second hop reported
                # into a room no reader accepts.
                history_context=(_origin.get("interaction_context") or None),
                system_turn=True,
                # A task scheduled in a turn that read untrusted content
                # starts its turn marked; a clean one starts clean
                # (runtime/turn_taint).
                **_untrusted_kw(payload),
                # Its own lane (never a nudge into a live turn) and
                # its own type (never the thread's binding).
                thread_key=wakeup_thread_key(entity_id, event_id),
                turn_type_id=_type_id,
                # ...in the Circles thread it was set in, when it was.
                thread_id=(_origin.get("thread_id") or None),
                # A request saved by the identity hold keeps what it was sent
                # with: the sending app's permissions (the tool gate records
                # them for audit; it does not narrow on them) and the thread's
                # chosen model, a privacy-chosen local one included.
                app_permissions=payload.get("app_permissions"),
                thread_override=payload.get("thread_override"),
                # the person's own words, not a woken turn (phase repairs)
                saved_request=bool(payload.get("saved_request")),
            ):
                line = (frame or "").rstrip("\n")
                if line.startswith("0:"):          # text delta — JSON-encoded string
                    try:
                        parts.append(_json.loads(line[2:]))
                    except Exception:  # noqa: BLE001
                        pass
                elif line.startswith("2:") and "identity_pending" in line:
                    # The identity hold refused this turn: the tools had not
                    # arrived, so it did not run. The 0: text that follows is
                    # the refusal, not a result.
                    try:
                        if any(isinstance(i, dict) and i.get("type") == "identity_pending"
                               for i in _json.loads(line[2:])):
                            pending = True
                    except Exception:  # noqa: BLE001
                        pass
                elif line.startswith("2:") and '"t": "context"' in line:
                    try:
                        for _it in _json.loads(line[2:]):
                            if isinstance(_it, dict) and _it.get("t") == "context":
                                _fire_cost["turns"] += 1
                                _c = _it.get("est_cost_usd")
                                if isinstance(_c, (int, float)):
                                    _fire_cost["usd"] += float(_c)
                                else:
                                    _fire_cost["priced"] = False
                    except Exception:  # noqa: BLE001 — a cost line, never the run
                        _fire_cost["priced"] = False
                elif line.startswith("2:") and "model_unconfigured" in line:
                    try:
                        if any(isinstance(i, dict) and i.get("type") == "model_unconfigured"
                               for i in _json.loads(line[2:])):
                            no_model = True
                    except Exception:  # noqa: BLE001
                        pass
                elif line.startswith("3:"):        # terminal error frame
                    try:
                        stream_err = _json.loads(line[2:])
                    except Exception:  # noqa: BLE001
                        stream_err = line[2:]
            if pending:
                raise IdentityPending("this Host's tools had not arrived")
            if no_model:
                raise ModelPending("this Host has no chat model set up")
            out = "".join(parts)
            from mastercode.circles_threads import DELETED_THREAD_REFUSAL
            if stream_err == DELETED_THREAD_REFUSAL:
                # The thread it was set in is deleted: the task goes with it,
                # like a task whose turn type is gone — never re-armed to be
                # refused again (a task written just as the thread was deleted
                # escapes the delete's own cancel).
                await pool.execute(
                    "UPDATE calendar_events SET status = 'cancelled', updated_at = NOW(),"
                    " cancelled_reason = 'the thread it was set in was deleted'"
                    " WHERE id = $1 AND entity_id = $2", event_id, entity_id)
                raise TaskCancelled(f"#{event_id}: its thread was deleted")
            if stream_err and not out:
                raise RuntimeError(str(stream_err))
            return out

        # The fired turn is POINTED at its own thread (the thread is the
        # chain of thought; there are no hidden notes). What
        # it planned was written visibly into the thread when the task was
        # set, and is quoted here as that — its own earlier words, not an
        # instruction from anywhere else.
        _lead = pointer_line(title, payload, _saved_tools)

        def _as_sent(i: int, step: str) -> str:
            return f"{_lead}\nStep {i} of {len(steps)} in the plan: {step}"

        if steps:
            # Run each step in sequence, labelling + accumulating its output.
            seg = _steps_done
            for i, step in enumerate(steps, 1):
                step_out = await _run_turn(_as_sent(i, step))
                # Still alive: a long runbook's row must never look orphaned
                # to the sweep between steps.
                await _touch(pool, entity_id, event_id)
                seg.append(f"**Step {i}/{len(steps)}: {step}**\n{step_out}".rstrip())
                # The job's budget, between steps: the steps left do not start
                # once what it has spent reaches it.
                if (_budget is not None and i < len(steps)
                        and _budget_spent + _fire_cost["usd"] >= _budget):
                    raise BudgetReached(
                        f"the job reached the ${_budget:.2f} budget you set for it after step "
                        f"{i} of {len(steps)} (${_budget_spent + _fire_cost['usd']:.2f} spent), "
                        "so the remaining steps did not run")
            content = "\n\n".join(seg)
        else:
            _body = (f"{_lead}\nTheir message: {prompt}" if payload.get("saved_request")
                     else f"{_lead}\nThe plan: {prompt}")
            # Earlier messages of theirs this one took over (identity_hold):
            # data, with their newer words above it.
            from mastercode.runtime.identity_hold import held_line as _held_line
            for _e in _saved_earlier:
                _body += _held_line(_e)
            if _saved_newer:
                _body += ("\nThe person has sent newer messages in this thread since "
                          "this one. Read them first, and do this only if they have not "
                          "changed or withdrawn it.")
            content = await _run_turn(_body)
        ok = True
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        if TURN_CAP_MARK in str(e) and not content and not _steps_done:
            # The process was full of turns (turn_registry.TurnCapError): not
            # a failure of the task. Put it back; the next poll with a free
            # slot runs it.
            log.info("offline-task: no free turn slot (event=%d) — requeued", event_id)
            await _requeue(pool, entity_id, event_id)
            return
        if isinstance(e, IdentityPending) and not _steps_done:
            _tries = int(payload.get("identity_defers") or 0)
            if _tries < IDENTITY_DEFER_MAX:
                if (isinstance(e, ModelPending) and _tries == 0
                        and not payload.get("saved_request")):
                    # Said ONCE per outage (the counter resets when the task
                    # next runs or re-arms): a setting needs fixing, and the
                    # person should not hear nothing for hours.
                    await post_to_origin_thread(
                        pool, entity_id, payload.get("origin"),
                        f"(Scheduled task **{title}** has not run: this Host has no chat "
                        "model set up. It stays scheduled and runs once one is.)",
                        extra_tags={"scheduled_task_id": event_id},
                        fallback_session=f"scheduled_task:{event_id}",
                        event_id=event_id, title=title)
                # Not run, not failed: retried later, backing off (a wake-up's
                # hold never asks the seed for an early attempt).
                log.info("offline-task: #%d not run — the Host's tools have not "
                         "arrived (try %d) — retrying in %ds", event_id, _tries + 1,
                         identity_defer_delay(_tries + 1))
                await _defer_for_identity(pool, entity_id, event_id, _tries + 1)
                return
            error = ((f"Nothing ran: this Host still had no chat model set up after "
                      f"{_tries} tries over several hours, so the request was not "
                      "carried out.") if isinstance(e, ModelPending) else
                     ("Nothing ran: this Host's tools never arrived after "
                      f"{_tries} tries over several hours, so the request was not "
                      "carried out."))
        if isinstance(e, BudgetReached):
            # Stopped for good: a reached budget is the person's word.
            error = str(e)
            recurrence = None
            _budget_stop = True
        if isinstance(e, TaskCancelled):
            log.info("offline-task: #%d cancelled while it ran — %d step(s) had run",
                     event_id, len(_steps_done))
        else:
            log.warning("offline-task: run failed (event=%d title=%r): %s",
                        event_id, title, error)
        if _steps_done and not content:
            # The steps that did run said something the user is owed.
            error = f"{error}\n\nWhat ran before it stopped:\n\n" + "\n\n".join(_steps_done)

    await _report_and_finalize(pool, entity_id, event_id, title, recurrence,
                               start_time, ok, content, error,
                               report_on_success=True,
                               origin=payload.get("origin"),
                               stopped=_budget_stop,
                               cost_usd=(round(_fire_cost["usd"], 6)
                                         if _fire_cost["turns"] and _fire_cost["priced"]
                                         else None),
                               priced_usd=(round(_fire_cost["usd"], 6)
                                           if _fire_cost["turns"] else None))


# The wake-ups running now, so each poll knows the free slots. Held here so a
# running task is never garbage-collected mid-run.
_RUNNING: set = set()


async def _guarded_run(pool, entity_id: int, claimed) -> None:
    """One claimed task, guarded so one failure (even one outside _run_one's
    own handling) is recorded and finalized and never kills the loop or
    sticks the task in 'running'."""
    try:
        await _run_one(pool, entity_id, claimed)
    except asyncio.CancelledError:
        # Shutdown: the task did not finish, so it is put back rather than
        # left 'running' forever.
        try:
            await asyncio.shield(_requeue(pool, entity_id, claimed["id"]))
        except BaseException:  # noqa: BLE001 — the loop is going away
            pass
        raise
    except Exception as e:  # noqa: BLE001
        log.error("offline-task: unhandled run error (id=%s): %s",
                  claimed["id"], e, exc_info=True)
        try:
            await _record_run(pool, claimed["id"], entity_id,
                              False, None, f"{type(e).__name__}: {e}")
            await _finalize(pool, claimed["id"], entity_id,
                            claimed["recurrence"], claimed["start_time"], False)
        except Exception:  # noqa: BLE001
            pass


async def run_offline_tasks_loop(pool, entity_id: int) -> None:
    """Poll for due scheduled tasks and run them headlessly, forever.

    Dispatched once at lifespan (server/main.py) as a guarded background task,
    gated on relay_entity_id > 0. Cancellation (shutdown) exits cleanly; every
    other error is logged and the loop continues so a transient DB blip never
    permanently stops scheduled execution.
    """
    log.info("offline-task runner online — entity %d, poll %ds",
             entity_id, POLL_INTERVAL_S)
    # Rows still 'taken over' by a process that is gone (their taken_boot is not
    # this process's id) are kept at once, not after the turn limit.
    await _sweep_orphans(pool, entity_id)
    _last_sweep = time.monotonic()
    global _POLL_KICK
    _POLL_KICK = asyncio.Event()
    while True:
        try:
            # The poll interval, cut short by kick_poll() (the identity seed
            # kicks it when the Host's tools arrive).
            try:
                await asyncio.wait_for(_POLL_KICK.wait(), POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            _POLL_KICK.clear()

            # Due, unclaimed, entity-scoped scheduled tasks. We select ids only,
            # then claim each one atomically below so two overlapping ticks (or
            # two replicas) never both run the same task.
            # Orphans are swept every ORPHAN_AFTER_S too, not only at startup:
            # a process that crashed and came back quickly would otherwise
            # leave its rows stuck until the next restart.
            if time.monotonic() - _last_sweep >= ORPHAN_AFTER_S:
                await _sweep_orphans(pool, entity_id)
                _last_sweep = time.monotonic()
            # Parallel wake-ups: claim only as
            # many as there are free slots and run each as its own task; the
            # loop does not wait for them, so a long turn never holds the
            # next due task back.
            _RUNNING.difference_update({t for t in _RUNNING if t.done()})
            # As many as the Host's memory holds now (running turns are in the
            # registry, so they are counted); no readable limit: every due one.
            slots = free_turn_slots(len(_RUNNING))
            slots = PER_PASS_MAX_TASKS if slots is None else slots
            if slots <= 0:
                continue
            due = await pool.fetch(
                """
                SELECT id
                  FROM calendar_events
                 WHERE entity_id = $1
                   AND event_type = 'scheduled_task'
                   AND status = 'scheduled'
                   AND start_time <= NOW()
                 ORDER BY start_time ASC
                 LIMIT $2
                """,
                entity_id, min(PER_PASS_MAX_TASKS, slots),
            )
            if not due:
                continue

            for d in due:
                # Atomic CLAIM: only the tick that flips 'scheduled'->'running'
                # gets the RETURNING row; a racing tick sees 0 rows and skips.
                try:
                    claimed = await pool.fetchrow(
                        """
                        UPDATE calendar_events
                           SET status='running', updated_at=NOW()
                         WHERE id = $1
                           AND entity_id = $2
                           AND status = 'scheduled'
                        RETURNING id, title, recurrence, start_time, payload
                        """,
                        d["id"], entity_id,
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("offline-task: claim failed (id=%s): %s",
                                d["id"], e)
                    continue
                if claimed is None:
                    continue  # already claimed by another tick — skip
                t = asyncio.create_task(_guarded_run(pool, entity_id, claimed),
                                        name=f"wakeup-{claimed['id']}")
                t._event_id = int(claimed["id"])
                _RUNNING.add(t)

        except asyncio.CancelledError:
            log.info("offline-task runner shutting down (entity %d)", entity_id)
            # The wake-ups it started go with it, as the sequential loop's
            # awaited run did.
            for t in list(_RUNNING):
                t.cancel()
            return
        except Exception as e:  # noqa: BLE001
            log.warning("offline-task loop cycle error (non-fatal): %s", e)
