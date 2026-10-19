"""runtime/identity_hold.py — a turn never runs toolless while the Host's
tools grant is still arriving.

The tools gate (tool_access.host_orchestration_enabled) reads
permission_overrides.bot_capabilities off the Host's own entity_registry row,
fresh on every turn. On a Host whose database was just created, that column
is NULL until the boot identity seed (server/bridge/identity_seed.py) writes
the first-run identity — and a NULL reads as the enrollment tier, chat-only.
A turn that arrived before the seed finished would run with tools=None and a
prompt that never mentioned a tool, and the Host would answer "I can't create
files ... would you like instructions?".

This module is the turn's side of that: when the seed for this Host is still
unresolved and the row grants no tools, the turn holds (bounded by
identity_seed.TURN_WAIT_S) with a visible status line, then re-reads the row.
Tools arrived → the turn runs with them. The seed finished and the grant
really is chat-only → the turn runs as that tier, which is the real answer.
Neither → the turn does not reach the model at all: the person gets a plain,
fixed reply saying nothing was done and why. No model is asked to explain a
state it cannot see (decision: diagnostics are deterministic).

A refused turn is not lost. The chat path saves the person's message as its
user row and the refusal as a system note, and schedules the message to run by
itself, once, in that thread, when the tools arrive (save_refused_request).
Nothing here ever asks the person to send anything again: the request already
is the go-ahead.

The refusal only follows a seed that has SEEN the placeholder row. A seed that
crashed before reading the row is "unknown" (identity_seed.identity_unresolved),
and an unknown seed never refuses a turn.

It runs before the session is built so the prompt and `tools=` are both
derived from the row the turn will actually run on — the one-gate invariant
scripts/test_tool_surface_one_gate.py pins.
"""

from __future__ import annotations

import logging
import uuid
import time
from typing import Awaitable, Callable, Optional

from mastercode.runtime.tool_access import host_orchestration_enabled

log = logging.getLogger("cseries.mastercode.identity_hold")

# A Host that has been up less than this has "just started"; past it the
# wording stops saying so (the first-run write takes milliseconds, so five
# minutes without it is not "just started").
JUST_STARTED_S = 300


def _seed_module():
    """The pod's identity seed, or None where there is no pod (an offline
    harness that imports the runtime without the server package)."""
    try:
        from server.bridge import identity_seed
    except Exception:
        return None
    return identity_seed


def _up_s(state: dict) -> Optional[float]:
    began = state.get("began")
    return None if began is None else max(0.0, time.time() - float(began))


def _up_phrase(up_s: Optional[float]) -> str:
    if up_s is None:
        return "this Host is still setting up"
    if up_s < JUST_STARTED_S:
        return "this Host has just started and is still setting up"
    mins = int(round(up_s / 60.0))
    if mins < 120:
        span = f"{mins} minutes"
    else:
        span = f"{int(round(up_s / 3600.0))} hours"
    return f"this Host has been running for {span} and still has not set up"


def hold_status(state: Optional[dict] = None) -> str:
    """Shown in the reasoning lane while the turn holds — the one live channel
    every shipped chat client already renders (brain_service maps
    reasoning_delta to the `g:` frame)."""
    up = _up_s(state or {})
    if up is not None and up >= JUST_STARTED_S:
        return ("Loading my tools — this Host is still setting up its identity "
                "and permissions.\n")
    return ("Loading my tools — this Host has just started and is still "
            "setting up its identity and permissions.\n")


# Kept for callers that read the constant; the wording a turn shows is
# hold_status(state).
HOLD_STATUS = hold_status({})


def refusal_text(state: dict, waited_s: float, *, saved: bool = False,
                 queued: bool = False) -> str:
    """The fixed reply for a turn that could not get its tools. Plain words;
    states what was done (nothing), why, and what happens next. `saved` is True
    when the request was kept and scheduled to run on its own: the reply then
    says so, and never asks the person to send it again. `queued` is True when
    the person's row is still on its way to the thread (PII mode lands it
    through a queue), so the reply says it is being saved, not that it is."""
    kept = "is being saved in this thread" if queued else "is saved in this thread"
    carry = (" If you send something newer in this thread first, that message "
             "carries this one forward instead.")
    status = state.get("status")
    detail = str(state.get("detail") or "")
    if status == "failed" and not state.get("retrying"):
        text = (
            "Nothing has been done with this yet. My tools have not loaded: this "
            f"Host could not set up its identity and permissions ({detail}) and "
            "has stopped retrying. That is a fault in this Host, recorded in its "
            "log as IDENTITY_PLACEHOLDER — not something you did."
        )
        if saved:
            text += (f" Your request {kept} and will run if my tools arrive, "
                     "for example after this Host restarts." + carry)
        return text
    if status == "failed":
        why = f"the last try failed ({detail})"
    else:
        why = f"it has not come back after {int(round(waited_s))} seconds"
    text = (
        "Nothing has been done with this yet. My tools have not loaded: "
        f"{_up_phrase(_up_s(state))} its identity and permissions, and "
        f"{why}. It keeps retrying on its own."
    )
    if saved:
        text += (f" Your request {kept} and will run by itself, once, as soon "
                 "as my tools arrive." + carry)
    return text


async def hold_for_identity(
    entity_id: int,
    *,
    load_entity: Callable[[], Awaitable[Optional[dict]]],
    progress_cb=None,
    system: bool = False,
) -> tuple[bool, Optional[dict]]:
    """Hold a turn for the boot identity seed when it may still bring this
    Host's tools grant.

    Returns (held, refusal). `refusal` is the reply dict that ends the turn,
    or None when the turn may run. `held` is True when the turn waited, so a
    caller holding an entity row from before the wait knows to re-read it.

    `system` is a scheduled turn (a wake-up retrying on its own schedule): it
    logs a refusal at DEBUG, because the caller puts it back and tries again,
    and says so once, itself.

    Costs nothing in steady state: the first test is an in-memory read of the
    seed's state, and the row is only read when that seed is unresolved.
    """
    seed = _seed_module()
    if seed is None or not entity_id or not seed.identity_unresolved(entity_id):
        return False, None
    entity = await load_entity()
    if entity is None or host_orchestration_enabled(entity):
        # No row: the normal path reports it. Tools already granted: nothing
        # to wait for, whatever the seed is still doing.
        return False, None

    t0 = time.monotonic()
    if progress_cb is not None:
        try:
            await progress_cb({"event": "reasoning_delta", "text": hold_status(dict(seed.SEED_STATE))})
        except Exception as exc:  # noqa: BLE001 — a dead stream must not stop the hold
            log.debug("identity hold status frame failed (non-fatal): %s", exc)
    state = await seed.wait_for_identity()
    waited = time.monotonic() - t0
    try:
        fresh = await load_entity()
    except Exception as exc:  # noqa: BLE001
        log.warning("identity hold: re-read of entity %s failed: %s", entity_id, exc)
        fresh = None

    if fresh is not None and host_orchestration_enabled(fresh):
        log.warning("TURN_HELD_FOR_IDENTITY eid=%s waited=%.1fs outcome=tools",
                    entity_id, waited)
        return True, None
    if state.get("status") in ("ok", "genesis"):
        # Seeded, and the grant itself is chat-only (enrollment tier): that is
        # the Host's real tier, not a pending one.
        log.warning("TURN_HELD_FOR_IDENTITY eid=%s waited=%.1fs outcome=seeded_chat_only",
                    entity_id, waited)
        return True, None

    if not state.get("placeholder_seen"):
        # The seed never saw the placeholder row (its boot read is failing or
        # still in flight): whether a grant is owed is UNKNOWN, and an unknown
        # state never refuses a person. The turn runs as the row reads.
        log.warning("TURN_HELD_FOR_IDENTITY eid=%s waited=%.1fs outcome=seed_unknown "
                    "status=%s detail=%s", entity_id, waited, state.get("status"),
                    state.get("detail"))
        return True, None

    (log.debug if system else log.error)(
        "TURN_HELD_FOR_IDENTITY eid=%s waited=%.1fs outcome=refused "
        "status=%s retrying=%s detail=%s", entity_id, waited,
        state.get("status"), state.get("retrying"), state.get("detail"))
    return True, {
        "response": refusal_text(state, waited),
        "identity_pending": True,
        "identity": {k: state.get(k) for k in ("status", "detail", "retrying", "began")},
        "identity_waited_s": waited,
    }


# ── the refused request is kept, and runs by itself when the tools arrive ────

# A turn's own rows must not stretch the answer by more than this: each write
# (the person's row, then the note) is bounded, and a write that does not land
# in time is reported as not saved rather than waited for.
ROW_WRITE_BOUND_S = 4.0


def _thread_scope(project_id, app_id, thread_id, history_context, session_id):
    """(interaction_context, row_project_id, source_context) for a no-sessions
    chat turn, derived exactly as the transient builder does."""
    from mastercode.memory_engine import derive_source_context
    from mastercode.runtime.session import transient_interaction_context
    ic, borrowed = transient_interaction_context(project_id, app_id, history_context)
    row_project = project_id
    if row_project is None and ic.startswith("project:"):
        try:
            row_project = int(ic.split(":", 1)[1])
        except (ValueError, IndexError):
            row_project = None
    if borrowed:
        row_project = None
    src = derive_source_context(ic, project_id=project_id, session_id=session_id,
                                thread_id=thread_id)
    return ic, row_project, src


# ── a newer message takes over a saved request ──────────────────────────────

# A saved request is only worth keeping in memory when this process saved one,
# or a boot-time look found one waiting; a seeded Host otherwise pays an
# in-memory check per person turn. _SAVES counts saves so a clear never
# swallows a save that landed while it was looking.
_SAVES = 0
_MAYBE = False
_BOOT_CHECKED = False

# How long a person's turn waits for a saved request of theirs that is RUNNING
# in its thread to finish (its result then sits in the history the turn is
# built from), and how often it looks.
WAKE_WAIT_S = 300.0
WAKE_POLL_S = 0.5

_BG: set = set()


def reset_saved_state() -> None:
    """Forget what this process knows about saved requests (tests)."""
    global _SAVES, _MAYBE, _BOOT_CHECKED
    _SAVES, _MAYBE, _BOOT_CHECKED = 0, False, False


# The saved requests of an entity. Two fragments, not one: an UPDATE takes only the
# WHERE (`UPDATE calendar_events SET ... WHERE ...`), and a SELECT takes the FROM too.
# One string starting `FROM calendar_events WHERE` was appended to UPDATEs, which
# Postgres rejects ("table name "calendar_events" specified more than once"): no
# takeover worked on a real database until scripts/test_saved_request_sql_real_db.py
# ran these statements against one.
_SAVED_WAKES_WHERE = (
    "WHERE entity_id=$1 AND event_type='scheduled_task' "
    "AND payload->>'saved_request'='true' ")
_SAVED_WAKES_SQL = "FROM calendar_events " + _SAVED_WAKES_WHERE      # SELECTs only
# A saved request that still needs looking for: waiting or running, or kept for
# the person's next message.
_PENDING_SQL = ("AND (status IN ('scheduled','running') OR "
                "(status='cancelled' AND cancelled_reason='held_for_person')) ")
_THREAD_SQL = ("AND COALESCE(payload->'origin'->>'thread_id','') = $2 "
               "AND COALESCE(payload->'origin'->>'interaction_context','') = $3")


async def _saved_requests_possible(pool, entity_id: int) -> bool:
    global _BOOT_CHECKED, _MAYBE
    if _MAYBE:
        return True
    if _BOOT_CHECKED:
        return False
    try:
        found = await pool.fetchval(
            "SELECT 1 " + _SAVED_WAKES_SQL + _PENDING_SQL + " LIMIT 1",
            entity_id)
    except Exception as exc:  # noqa: BLE001 — unknown: look this turn, ask again next
        log.debug("identity hold: boot check for saved requests failed: %s", exc)
        return True
    _BOOT_CHECKED = True
    _MAYBE = bool(found)
    return _MAYBE


def _jsonb_dict(v) -> dict:
    import json
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


async def saved_request_text(pool, entity_id: int, locator: dict) -> Optional[str]:
    """The words of a saved request. A wake keeps them only as a reference to
    the person's own saved row (so PII mode never copies raw text into the
    calendar); the text itself is copied only when that row could not be
    written. None when the row has not landed (yet)."""
    if locator.get("prompt"):
        return str(locator["prompt"])
    ref = locator.get("user_row") or {}
    sid = ref.get("session_id")
    if not sid:
        return None
    try:
        text = await pool.fetchval(
            "SELECT content FROM entity_memory WHERE entity_id=$1 AND source_session=$2 "
            "AND memory_type='turn' AND metadata->>'role'='user' "
            "ORDER BY created_at LIMIT 1", entity_id, str(sid))
    except Exception as exc:  # noqa: BLE001
        log.warning("identity hold: reading a saved request's row failed: %s", exc)
        return None
    return str(text) if text else None


def _locator(payload: dict) -> dict:
    loc = {k: payload[k] for k in ("prompt", "user_row") if payload.get(k)}
    if loc and payload.get("crash_unknown"):
        loc["interrupted"] = True      # the turn that had it died with its process
    return loc


HANDOVER_LINE = ("(An earlier request of yours, held until my tools arrived, was not "
                 "run on its own: your newer message carries it forward.)")
FOLD_LINE = ("(An earlier request of yours, held until my tools arrived, was added to "
             "your newest held request, which runs once my tools are here.)")
_TAKEN_REASON = "a newer message in this thread took it over"
# What a taken-over row says once its turn has ended (Takeover.settle). While the
# turn runs it carries _TAKEN_REASON; a row still carrying it long after the turn
# limit belonged to a process that died (sweep_taken_over).
CARRIED_REASON = "carried forward by a newer message"
PARTIAL_REASON = "the turn that had it acted; it may be partly done"
STOPPED_REASON = "the person stopped the turn that had it"
from mastercode.circles_threads import HELD_FOR_PERSON_REASON as _HELD_REASON


async def reschedule(pool, entity_id: int, *, ids=None, how: str = "backoff") -> None:
    """THE ONE PLACE a saved request becomes a scheduled wake again.

    A request whose payload carries `crash_unknown` — the process that was
    running the turn that had it died, so whether it acted cannot be known — can
    never become a scheduled wake: scheduled work runs by itself, and nothing
    may redo what might be half done without the person's say. Every such row is
    returned to `held_for_person` (status 'cancelled', flag kept), where only the
    person's next message can take it, and the model is told it was interrupted.
    The statements that schedule or release also carry the guard in their own
    WHERE, so the rule does not depend on a statement before or after them.

      how="backoff"  rows `ids` taken over by a turn that did not act: back to
                     scheduled with the normal deferral and backoff (60 s doubling
                     to 300 s, the first deferral counted), not due now;
      how="release"  the grant landed or a chat model was saved: every scheduled
                     saved request becomes due now. A flagged row found
                     'scheduled' (it should never be) is put back to held.
    """
    not_flagged = "AND COALESCE(payload->>'crash_unknown', '') <> 'true' "
    if how == "backoff":
        # flagged rows: held, not scheduled
        await pool.execute(
            "UPDATE calendar_events SET cancelled_reason=$3, updated_at=NOW() "
            "WHERE entity_id=$1 AND id = ANY($2::bigint[]) AND status='cancelled' "
            "AND cancelled_reason=$4 AND payload->>'crash_unknown'='true'",
            entity_id, list(ids or []), _HELD_REASON, _TAKEN_REASON)
        await pool.execute(
            "UPDATE calendar_events SET status='scheduled', "
            "start_time = NOW() + make_interval(secs => LEAST(60 * power(2, "
            "COALESCE((payload->>'identity_defers')::int, 0)), 300)), "
            "event_at = NOW() + make_interval(secs => LEAST(60 * power(2, "
            "COALESCE((payload->>'identity_defers')::int, 0)), 300)), "
            "updated_at=NOW(), cancelled_reason=NULL, "
            "payload = jsonb_set(COALESCE(payload, '{}'::jsonb), '{identity_defers}', "
            "to_jsonb(COALESCE((payload->>'identity_defers')::int, 0) + 1)) "
            "WHERE entity_id=$1 AND id = ANY($2::bigint[]) AND status='cancelled' "
            "AND cancelled_reason=$3 " + not_flagged,
            entity_id, list(ids or []), _TAKEN_REASON)
    elif how == "release":
        # Due now: the guard keeps a flagged row from ever being made due...
        res = await pool.execute(
            "UPDATE calendar_events SET start_time=NOW(), event_at=NOW(), updated_at=NOW() "
            "WHERE entity_id=$1 AND event_type='scheduled_task' AND status='scheduled' "
            "AND payload->>'saved_request'='true' AND start_time > NOW() " + not_flagged,
            entity_id)
        if not str(res).endswith(" 0"):
            log.info("identity hold: held requests due now (%s)", res)
        # ...and a flagged row found 'scheduled' at all (it should never be) is
        # put back to held, so it cannot run on a later poll either.
        await pool.execute(
            "UPDATE calendar_events SET status='cancelled', cancelled_reason=$2, updated_at=NOW() "
            "WHERE entity_id=$1 AND event_type='scheduled_task' AND status='scheduled' "
            "AND payload->>'saved_request'='true' AND payload->>'crash_unknown'='true'",
            entity_id, _HELD_REASON)
    else:  # pragma: no cover
        raise ValueError(f"reschedule: unknown how={how!r}")


# This process's id, written into a row's payload (`taken_boot`) at the moment a
# turn takes the row over. A row still 'taken over' whose boot id is not this
# process's was taken by a process that is gone: no turn here can be settling it.
# Exact, and independent of clocks.
PROCESS_BOOT_ID = uuid.uuid4().hex

_TAKEN_BOOT_SET = ("payload = jsonb_set(COALESCE(payload, '{}'::jsonb), "
                   "'{taken_boot}', to_jsonb($BOOT::text))")


def _in_flight_takeover_ids() -> list[int]:
    """Row ids a turn of THIS process is carrying right now: every unsettled
    Takeover still referenced (a live turn holds its handle; a handle no turn
    holds any more is collected and drops out)."""
    out: list[int] = []
    for h in list(_LIVE_TAKEOVERS):
        if not getattr(h, "_settled", True):
            for i in getattr(h, "ids", []) or []:
                try:
                    out.append(int(i))
                except (TypeError, ValueError):
                    continue
    return out


async def sweep_taken_over(pool, entity_id: int, older_than_s: float) -> int:
    """A takeover is settled when its turn ends. A row still carrying the
    'taken over' reason was taken by a turn that did not finish: its process died
    (the row's `taken_boot` is not this process's id — exact, at once, after a
    restart), or, as a backstop, it has been 'taken over' for longer than a turn
    can go without a sign of life — and no turn of this process is carrying it
    now. That last clause is what the age alone used to stand for: a turn runs while it shows life, with no total
    unless a Host sets one, so a live turn can hold a takeover past any fixed
    age, and its row must not be read as crashed. Whether such a turn acted cannot be known, so the request is
    never run on its own: it is kept for the person's next message, flagged
    `crash_unknown`, and that message's takeover says it may have been partly
    done before a restart. Run at start and periodically with the orphan sweep."""
    global _MAYBE
    try:
        res = await pool.execute(
            "UPDATE calendar_events SET cancelled_reason=$3, updated_at=NOW(), "
            "payload = jsonb_set(COALESCE(payload, '{}'::jsonb), '{crash_unknown}', 'true') "
            "WHERE entity_id=$1 AND event_type='scheduled_task' "
            "AND payload->>'saved_request'='true' AND status='cancelled' "
            "AND cancelled_reason=$2 AND (COALESCE(payload->>'taken_boot', '') <> $5 "
            "OR (updated_at < NOW() - make_interval(secs => $4) "
            "    AND NOT (id = ANY($6::bigint[]))))",
            entity_id, _TAKEN_REASON, _HELD_REASON, float(older_than_s), PROCESS_BOOT_ID,
            _in_flight_takeover_ids())
        n = int(str(res).rsplit(" ", 1)[-1])
    except Exception as exc:  # noqa: BLE001
        log.warning("identity hold: sweep of unsettled takeovers failed (non-fatal): %s", exc)
        return 0
    if n:
        _MAYBE = True
        log.warning("identity hold: %d held request(s) were taken over by a turn that never "
                    "finished (the process died); kept for the person's next message, "
                    "flagged as possibly partly done", n)
    return n


async def sweep_stuck_running(pool, entity_id: int, older_than_s: float, mine_ids) -> int:
    """The runner's orphan sweep for rows stuck 'running' (their process died).

    THE INVARIANT for a saved request: it leaves 'running' or 'taken over' only
    with a recorded 'nothing acted' outcome, or it goes to held_for_person with
    crash_unknown — never to scheduled. A row found stuck has no recorded outcome
    (the turn may have acted before the process died), so a saved request is held
    and flagged; every other task is reset to scheduled as before. The generic
    statement carries `<> 'true'` so it cannot take a saved request even if the
    first one is skipped.

    Returns how many saved requests were held."""
    global _MAYBE
    mine = list(mine_ids or [])
    # Every other task first: reset to scheduled as before. The statement carries
    # `<> 'true'` itself, so it cannot take a saved request...
    n = await pool.execute(
        "UPDATE calendar_events SET status='scheduled', updated_at=NOW() "
        "WHERE entity_id=$1 AND event_type='scheduled_task' AND status='running' "
        "AND COALESCE(payload->>'saved_request', '') <> 'true' "
        "AND updated_at < NOW() - make_interval(secs => $2) "
        "AND NOT (id = ANY($3::bigint[]))",
        entity_id, float(older_than_s), mine)
    # ...and a saved request still 'running' has no recorded outcome: held, flagged.
    res = await pool.execute(
        "UPDATE calendar_events SET status='cancelled', cancelled_reason=$4, updated_at=NOW(), "
        "payload = jsonb_set(COALESCE(payload, '{}'::jsonb), '{crash_unknown}', 'true') "
        "WHERE entity_id=$1 AND event_type='scheduled_task' AND status='running' "
        "AND payload->>'saved_request'='true' "
        "AND updated_at < NOW() - make_interval(secs => $2) "
        "AND NOT (id = ANY($3::bigint[]))",
        entity_id, float(older_than_s), mine, _HELD_REASON)
    held = int(str(res).rsplit(" ", 1)[-1])
    if held:
        _MAYBE = True
        log.warning("identity hold: %d saved request(s) were running when the process died; "
                    "kept for the person's next message, flagged as possibly partly done", held)
    log.info("offline-task: orphan sweep — %s (+%d saved request(s) held)", n, held)
    return held


def held_line(entry) -> str:
    """The sentence that puts an earlier held request in front of the model as
    data: a plain string is a request nothing has touched; a dict with
    `interrupted` is one whose turn died with its process."""
    if isinstance(entry, dict):
        text = str(entry.get("text") or "")[:2000]
        if entry.get("interrupted"):
            return ("\nAn earlier message from the person: \u00ab" + text + "\u00bb. The turn that "
                    "had it was interrupted by a restart, so it may have been partly done "
                    "before: check before redoing it. It will not run on its own now.")
    else:
        text = str(entry)[:2000]
    return ("\nAn earlier message from the person, not yet run: \u00ab" + text +
            "\u00bb. It will not run on its own now.")


def spawn(coro) -> None:
    """Run a coroutine in the background, holding the task so it is not
    garbage-collected mid-run."""
    import asyncio
    try:
        t = asyncio.get_running_loop().create_task(coro)
        _BG.add(t)
        t.add_done_callback(_BG.discard)
    except RuntimeError:
        coro.close()


def post_line(pool, entity_id: int, origin, text: str = HANDOVER_LINE) -> None:
    """A visible line in a held request's thread. A background write: the
    person's turn is not held up by it."""
    import asyncio
    async def _go():
        try:
            from mastercode.brain import offline_task_runner as otr
            await otr.post_to_origin_thread(
                pool, entity_id, origin, text,
                extra_tags={"saved_request_handover": True},
                fallback_session="scheduled_task:saved_request")
        except Exception as exc:  # noqa: BLE001
            log.warning("identity hold: handover line failed (non-fatal): %s", exc)
    try:
        t = asyncio.get_running_loop().create_task(_go())
        _BG.add(t)
        t.add_done_callback(_BG.discard)
    except RuntimeError:
        pass


STOPPED_LINE = ("(Not run: you stopped the turn that had your earlier request, so it was "
                "not run on its own.)")
PARTIAL_LINE = ("(The turn that had your earlier request failed after it had already acted, "
                "so it may be partly done. It is not run again on its own.)")
BLOCKED_PARTIAL_LINE = ("(The turn that had your earlier request was blocked after it had already "
                        "acted, so it may be partly done. It is not run again on its own.)")
STOPPED_PARTIAL_LINE = ("(You stopped the turn that had your earlier request after it had already "
                        "acted, so it may be partly done. It is not run again on its own.)")
TIMEOUT_PARTIAL_LINE = ("(The turn that had your earlier request hit its time limit after it had "
                        "already acted, so it may be partly done. It is not run again on its own.)")
TIMEOUT_HELD_AGAIN_LINE = ("(The turn that was to carry your earlier request hit its time limit before "
                           "anything was done, so the request is held again."
                           " It runs once more after a short wait; if the problem is still there "
                           "then, it is reported as failed and not tried again.)")
HELD_AGAIN_LINE = ("(The turn that was to carry your earlier request failed before anything "
                   "was done, so the request is held again."
                   " It runs once more after a short wait; if the problem is still there "
                   "then, it is reported as failed and not tried again.)")
CRASH_HELD_LINE = ("(Your earlier request was interrupted by a restart and the turn that was to "
                   "carry it failed, so it may be partly done. It stays held until your next "
                   "message; it is not run on its own.)")
CRASH_HELD_NOTE = ("A request interrupted by a restart stays held until your next message "
                   "instead; it is not run on its own.")
NOT_RUN_LINES = {
    "model_unconfigured": ("(Your earlier request has not run yet: this Host has no chat model "
                           "set up. It stays held and runs once one is; if none is set up within about "
                           "five hours it is reported as not run.)"),
    "blocked": ("(Your earlier request has not run yet: the turn that was to carry it was "
                "blocked. It stays held until your next message, which takes it over.)"),
}


# Every Takeover handle alive in this process (weak: a handle no turn holds any
# more is collected and leaves). sweep_taken_over reads it so the age backstop
# never marks a row a live turn is still carrying.
import weakref as _weakref
_LIVE_TAKEOVERS: "_weakref.WeakSet" = _weakref.WeakSet()


class Takeover:
    """The held requests a person's newer turn has taken over, kept until that
    turn's outcome is known.

    Taking a request over only CANCELS its wake. What happens next depends on
    whether the turn ACTED (`acted`: at least one tool call that is not
    read-only executed in it — a text reply, a search or a failed model call
    changes nothing in the world) and on
    how it ended (settle):

      "ok"      a model call ran and answered (`model_saw`): the carried-forward
                line;
      "stopped" the person stopped the turn: never put back (Stop means stop,
                and the turn may already have acted), an honest line;
      "failed"  an error or exception;
      "timeout" the turn hit its time limit (not a Stop);
      "not_run" no model ran on it (no chat model, blocked).
    The FIRST rule, for every outcome but a carried success: if the turn had
    acted, the request may be partly done, so it is never put back and a line
    says so. Only when nothing acted: failed / timeout / not_run put it back
    with the normal deferral and backoff (a route save or the grant landing
    releases it sooner), except a blocked turn (a governance block),
    which keeps it for the person's next ordinary message instead (the row
    stays 'cancelled' with the reason `held_for_person`);
      "unqueued" the wake the words were folded into was not scheduled: put
                back, quietly (the refusal note already says so)."""

    def __init__(self, pool, entity_id: int):
        self.pool, self.entity_id = pool, entity_id
        self.ids: list = []
        self.origins: list = []
        self.locators: list = []
        self.model_saw = False
        self.acted = False
        self.flagged: list = []       # rows whose turn died with its process (crash_unknown)
        self._settled = False
        _LIVE_TAKEOVERS.add(self)

    def __bool__(self) -> bool:
        return bool(self.locators)

    def add(self, row_id, payload: dict) -> None:
        self.ids.append(row_id)
        if payload.get("crash_unknown"):
            self.flagged.append(row_id)
        self.origins.append(payload.get("origin"))
        self.locators.extend(payload.get("earlier") or [])
        loc = _locator(payload)
        if loc:
            self.locators.append(loc)

    def _say(self, text: str) -> None:
        for o in self.origins:
            post_line(self.pool, self.entity_id, o, text)

    async def settle(self, outcome: str, why: str = "") -> None:
        if self._settled or not self.ids:
            self._settled = True
            return
        self._settled = True
        # FIRST, and for every outcome: a turn that acted (a tool call that
        # changes something executed) may have done part of the request, so it
        # is never put back — whatever ended the turn (an error, a block, a
        # time limit, a Stop). Only a carried success is reported as carried.
        if self.acted and not (outcome == "ok" and self.model_saw):
            self._say(STOPPED_PARTIAL_LINE if outcome == "stopped"
                      else TIMEOUT_PARTIAL_LINE if outcome == "timeout"
                      else BLOCKED_PARTIAL_LINE if (outcome == "not_run" and why == "blocked")
                      else PARTIAL_LINE)
            await self._set_reason(STOPPED_REASON if outcome == "stopped" else PARTIAL_REASON)
            return
        if outcome == "ok" and self.model_saw:
            self._say(HANDOVER_LINE)
            await self._set_reason(CARRIED_REASON)
            return
        # Nothing acted from here on.
        if outcome == "stopped":
            self._say(STOPPED_LINE)       # Stop means stop: never put back
            await self._set_reason(STOPPED_REASON)
            return
        if outcome == "unqueued":         # the wake it was folded into was not scheduled
            await self._put_back()
            return
        if outcome == "ok":               # a result with no model call behind it
            outcome = "not_run"
        if self.flagged and len(self.flagged) == len(self.ids) and why != "blocked":
            # Every request here was interrupted by a restart: it will not be
            # retried on its own whatever the failure was.
            self._say(CRASH_HELD_LINE)
        elif outcome == "timeout":
            self._say(TIMEOUT_HELD_AGAIN_LINE + (" " + CRASH_HELD_NOTE if self.flagged else ""))
        else:
            self._say((NOT_RUN_LINES.get(why) or HELD_AGAIN_LINE)
                      + (" " + CRASH_HELD_NOTE if self.flagged and why != "blocked" else ""))
        if why == "blocked":
            await self._set_reason(_HELD_REASON)
            log.warning("identity hold: %d held request(s) kept for the person's next message",
                        len(self.ids))
            global _MAYBE
            _MAYBE = True
        else:
            await self._put_back()

    async def _set_reason(self, reason: str) -> None:
        """Replace the 'taken over' reason on this takeover's rows with the final
        one, so a row is never left saying a turn is still running."""
        try:
            await self.pool.execute(
                "UPDATE calendar_events SET cancelled_reason=$4, updated_at=NOW() "
                "WHERE entity_id=$1 AND id = ANY($2::bigint[]) AND status='cancelled' "
                "AND cancelled_reason=$3", self.entity_id, list(self.ids), _TAKEN_REASON, reason)
        except Exception as exc:  # noqa: BLE001
            log.error("identity hold: settling held requests (%s) failed: %s", reason, exc)

    async def _put_back(self) -> None:
        """Back to scheduled with the normal deferral — through reschedule(), the
        one owner, which keeps a crash-flagged row held instead."""
        global _MAYBE
        try:
            await reschedule(self.pool, self.entity_id, ids=self.ids, how="backoff")
            _MAYBE = True
            log.warning("identity hold: %d held request(s) put back: the turn that took "
                        "them over did not act on them (%d kept held: interrupted by a "
                        "restart)", len(self.ids), len(self.flagged))
        except Exception as exc:  # noqa: BLE001
            log.error("identity hold: putting held requests back failed: %s", exc)


async def _take_scheduled(pool, entity_id, thread, ic, handle: Takeover) -> None:
    rows = await pool.fetch(
        "UPDATE calendar_events SET status='cancelled', updated_at=NOW(), "
        "cancelled_reason='" + _TAKEN_REASON + "', "
        + _TAKEN_BOOT_SET.replace("$BOOT", "$4") + " "
        + _SAVED_WAKES_WHERE + "AND status='scheduled' " + _THREAD_SQL
        + " RETURNING id, payload", entity_id, thread, ic or "", PROCESS_BOOT_ID)
    for r in rows or []:
        handle.add(r["id"], _jsonb_dict(r["payload"]))
    # Requests kept for the person's next message (after a blocked turn): this
    # is that message. They become ordinary takeovers again.
    held = await pool.fetch(
        "UPDATE calendar_events SET cancelled_reason='" + _TAKEN_REASON + "', updated_at=NOW(), "
        + _TAKEN_BOOT_SET.replace("$BOOT", "$4") + " "
        + _SAVED_WAKES_WHERE + "AND status='cancelled' AND cancelled_reason=$5 "
        + _THREAD_SQL + " RETURNING id, payload", entity_id, thread, ic or "", PROCESS_BOOT_ID,
        _HELD_REASON)
    for r in held or []:
        handle.add(r["id"], _jsonb_dict(r["payload"]))


def _live_wake(entity_id: int, event_id: int) -> bool:
    """True when THIS process is running the wake. A pod is one process, so a
    row that says 'running' while nothing here runs it is the leftover of a
    process that died (the orphan sweep resets it much later)."""
    try:
        from mastercode.brain import offline_task_runner as otr
        return otr.wake_is_live(entity_id, event_id)
    except Exception:  # noqa: BLE001
        return True   # unknown: wait rather than risk a second run


async def hand_over_saved_requests(pool, entity_id: int, *, project_id=None, app_id=None,
                                   thread_id=None, history_context=None,
                                   wait_running: bool = True, progress_cb=None,
                                   register=None) -> "Takeover":
    """A person's newer message takes over the requests of theirs still held
    in its thread. Returns a Takeover (falsy when nothing was held) so the
    caller can put the words in front of the model; nothing is dropped:

      * pending saved wakes of the thread are cancelled — and so is a wake
        whose row says 'running' but that nothing in this process runs (the
        leftover of a dead process) — with the visible line posted only when
        the turn that took them completes (Takeover.settle);
      * one that is LIVE in this process is waited for (bounded), so the newer
        turn answers with its result in the history rather than running the
        request a second time; afterwards the takeover is repeated, because a
        wake put back while we waited (its pre-check deferred it) is
        scheduled again.

    `register` is called with the Takeover as soon as it exists.
    A seeded Host with nothing saved pays one in-memory check.
    """
    global _MAYBE
    handle = Takeover(pool, entity_id)
    if pool is None or not await _saved_requests_possible(pool, entity_id):
        return handle
    if register is not None:
        # Registered BEFORE anything is taken or waited for, so a Stop while
        # the turn waits for a running wake still settles what was already taken.
        register(handle)
    import asyncio
    saves_at_start = _SAVES
    from mastercode.runtime.session import transient_interaction_context
    ic, _ = transient_interaction_context(project_id, app_id, history_context)
    thread = str(thread_id or "").strip()

    async def take_all():
        try:
            await _take_scheduled(pool, entity_id, thread, ic, handle)
        except Exception as exc:  # noqa: BLE001
            log.warning("identity hold: taking over saved requests failed (non-fatal): %s", exc)

    async def running():
        rows = await pool.fetch(
            "SELECT id " + _SAVED_WAKES_SQL + "AND status='running' " + _THREAD_SQL,
            entity_id, thread, ic or "")
        return [r["id"] for r in rows or []]

    await take_all()

    if wait_running:
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + WAKE_WAIT_S
            told = False
            stale_done = set()
            while True:
                ids = await running()
                live = [i for i in ids if _live_wake(entity_id, i)]
                stale = [i for i in ids if i not in live and i not in stale_done]
                if stale:
                    # Nothing here runs these: take them over like a scheduled one.
                    stale_done.update(stale)
                    # A row 'running' that nothing here runs belonged to a process
                    # that died: it may have acted. Taking it over records that
                    # (crash_unknown), so it is framed as interrupted and can never
                    # be scheduled again — not as "not yet run".
                    rows = await pool.fetch(
                        "UPDATE calendar_events SET status='cancelled', updated_at=NOW(), "
                        "cancelled_reason='" + _TAKEN_REASON + "', "
                        "payload = jsonb_set(jsonb_set(COALESCE(payload, '{}'::jsonb), "
                        "'{taken_boot}', to_jsonb($3::text)), '{crash_unknown}', 'true') "
                        + _SAVED_WAKES_WHERE + "AND status='running' AND id = ANY($2::bigint[]) "
                        "RETURNING id, payload", entity_id, stale, PROCESS_BOOT_ID)
                    for r in rows or []:
                        handle.add(r["id"], _jsonb_dict(r["payload"]))
                if not live:
                    break
                if not told and progress_cb is not None:
                    told = True
                    try:
                        await progress_cb({"event": "reasoning_delta", "text":
                                           "Finishing your earlier request first.\n"})
                    except Exception:  # noqa: BLE001
                        pass
                if loop.time() >= deadline:
                    log.warning("identity hold: a saved request is still running after "
                                "%ds; the newer turn goes on", int(WAKE_WAIT_S))
                    break
                await asyncio.sleep(WAKE_POLL_S)
        except Exception as exc:  # noqa: BLE001
            log.warning("identity hold: waiting for a running saved request failed: %s", exc)
        # A wake that was in its pre-check (or finished deferred) while we
        # waited is scheduled again: take it too.
        await take_all()

    try:
        pending = await pool.fetchval(
            "SELECT 1 " + _SAVED_WAKES_SQL + _PENDING_SQL + " LIMIT 1",
            entity_id)
        # The count is read AFTER the query: a save that landed while it ran
        # must keep the flag on.
        if _SAVES == saves_at_start and not pending:
            _MAYBE = False
    except Exception:  # noqa: BLE001
        pass
    if handle:
        log.info("identity hold: %d held request(s) taken over by a newer message", len(handle.ids))
    return handle


async def resolve_locators(pool, entity_id: int, locators) -> list:
    out = []
    for loc in (locators.locators if isinstance(locators, Takeover) else locators):
        t = await saved_request_text(pool, entity_id, loc)
        if t:
            out.append({"text": t, "interrupted": True} if loc.get("interrupted") else t)
    return out


def _sender_tags(client_id) -> dict:
    """Which app sent each id (session._sender_tags: client_keys)."""
    if not client_id:
        return {}
    from mastercode.runtime.session import _sender_tags as _st
    return _st(client_id)


async def save_refused_request(
    pool,
    entity_id: int,
    refusal: dict,
    *,
    user_message: str,
    app_id: Optional[str] = None,
    project_id: Optional[int] = None,
    thread_id: Optional[str] = None,
    history_context: Optional[str] = None,
    had_attachments: bool = False,
    app_permissions: Optional[list] = None,
    thread_override: Optional[dict] = None,
    # The sender's id (session.row_client_id: one id, or the list of several
    # messages joined into this request), kept on the person's row.
    client_id=None,
) -> dict:
    """Keep a refused chat request.

    Writes the person's message as its user row, schedules the message to run
    by itself, once, through the scheduled-task wake path (offline_task_runner),
    then writes the refusal as a system note (the origin='cancelled' shape
    turn_registry writes for a stopped turn) saying only what actually
    happened.

    The wake does not copy the person's words: it points at the saved user row
    (so PII mode, which lands that row tokenized through a queue, never has raw
    text in the calendar). Only when the row could not be written, and PII mode
    is off, are the words copied so the request is not lost. It also carries
    the sending app's `app_permissions` (recorded by the tool gate for audit —
    the gate does not narrow on them) and the thread's chosen model
    (`thread_override`), so the woken turn runs on the model the person chose,
    a privacy-chosen local one included. Requests of theirs still held in this
    thread are folded in: cancelled, and carried on this wake so the woken turn
    sees them as earlier messages, never lost. Each write is bounded by
    ROW_WRITE_BOUND_S so the answer is not held up by the database.

    Mutates `refusal["response"]` to the final wording and returns
    {"rows": bool, "wake": bool}. Never raises.
    """
    import asyncio
    import uuid
    from datetime import datetime, timedelta, timezone

    out = {"rows": False, "wake": False}
    state = {**(refusal.get("identity") or {})}
    waited = float(refusal.get("identity_waited_s") or 0.0)
    session_id = str(uuid.uuid4())
    ic, row_project, src = "", project_id, None
    try:
        ic, row_project, src = _thread_scope(project_id, app_id, thread_id,
                                             history_context, session_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("identity hold: thread scope for the refused request failed: %s", exc)

    from mastercode import pii_queue as pii_guard
    from mastercode.memory_engine import write_turn_row
    queued = pii_guard.enabled()
    base = {"app_id": app_id, "source_context": src}
    t_user = datetime.now(timezone.utc)

    # 1. the person's own message, in the thread. In PII mode write_turn_row
    # returns None because the row is QUEUED, not because it failed: queued
    # counts as saved (the note says it is being saved).
    try:
        rid = await asyncio.wait_for(
            write_turn_row(pool, entity_id, session_id, "user", user_message,
                           project_id=row_project,
                           # The sender's id for the words (session.push_nudge
                           # client_id) is on the person's row only: it is
                           # where the Index's retry finds them delivered, as
                           # it finds the row of a turn that ran
                           # (session.py, _send_message_core).
                           tags={**base, **({"client_id": client_id} if client_id else {}),
                                 **_sender_tags(client_id)},
                           created_at=t_user),
            timeout=ROW_WRITE_BOUND_S)
        out["rows"] = bool(rid) or queued
    except Exception as exc:  # noqa: BLE001 — incl. the bound
        log.warning("identity hold: saving the refused request's row failed (non-fatal): %r", exc)

    # 2. requests still held in this thread are folded into this one.
    taken = Takeover(pool, entity_id)
    earlier: list = []
    try:
        taken = await hand_over_saved_requests(
            pool, entity_id, project_id=project_id, app_id=app_id, thread_id=thread_id,
            history_context=history_context, wait_running=False)
        earlier = list(taken.locators)
    except Exception as exc:  # noqa: BLE001
        log.warning("identity hold: folding earlier saved requests failed (non-fatal): %s", exc)

    # 3. the wake.
    try:
        from server.bridge.memory_handlers import handle_calendar_append
        origin = {k: v for k, v in (
            ("interaction_context", ic or None),
            ("project_id", row_project),
            ("thread_id", str(thread_id).strip() or None if thread_id else None),
        ) if v is not None}
        payload = {"saved_request": True, "origin": origin,
                   # Anything newer than this, said by the person in this
                   # thread, supersedes the request (the runner checks).
                   "saved_at": t_user.isoformat(), "source_context": src}
        if out["rows"] or queued:
            # Point at the saved row. In PII mode that holds even if the write
            # call raised: raw words are never copied into the calendar there.
            payload["user_row"] = {"session_id": session_id}
        else:
            # No row to point at and no PII queue to keep raw text out of:
            # the words travel on the wake so the request is not lost.
            payload["prompt"] = user_message
        if earlier:
            payload["earlier"] = earlier
        if app_permissions is not None:
            payload["app_permissions"] = list(app_permissions)
        if thread_override:
            payload["thread_override"] = dict(thread_override)
        fire_at = (datetime.now(timezone.utc) + timedelta(seconds=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        env = await handle_calendar_append({
            "event_id": str(uuid.uuid4()),
            "event_type": "scheduled_task",
            "summary": "Your request, held until my tools arrived",
            "event_at": fire_at,
            "status": "scheduled",
            "source": "platform",
            "related_project_id": row_project,
            "payload": payload,
        })
        out["wake"] = bool(env.get("ok"))
        if out["wake"]:
            global _SAVES, _MAYBE
            _SAVES += 1
            _MAYBE = True
        else:
            log.warning("identity hold: the saved request was not scheduled: %s",
                        env.get("error"))
    except Exception as exc:  # noqa: BLE001
        log.warning("identity hold: scheduling the saved request failed (non-fatal): %s", exc)

    # The folded requests were handed to this wake: if it was not scheduled,
    # they are put back instead of being lost.
    try:
        # The folded words ride the new wake, whose turn is what the model will
        # answer: nothing has to have run for them to be carried.
        taken.model_saw = True
        await taken.settle("ok" if out["wake"] else "unqueued")
    except Exception as exc:  # noqa: BLE001
        log.warning("identity hold: settling folded requests failed: %s", exc)

    # 4. the note says only what is true.
    if out["wake"]:
        text = refusal_text(state, waited, saved=out["rows"], queued=queued)
        if not out["rows"]:
            text += (" Your message could not be written into this thread, but "
                     "it will still run once my tools arrive.")
        elif had_attachments:
            text += (" Files you attached are not kept for this; attach them "
                     "again if the request needs them.")
    else:
        text = refusal_text(state, waited, saved=False)
        text += (" Your message is in this thread, but it could not be queued to "
                 "run on its own." if out["rows"] else
                 " Your message could not be saved, and it will not run on its own.")
    refusal["response"] = text

    try:
        await asyncio.wait_for(
            write_turn_row(pool, entity_id, session_id, "assistant", text,
                           project_id=row_project,
                           tags={**base, "origin": "cancelled",
                                 "stopped_reason": "identity_pending"},
                           created_at=t_user + timedelta(milliseconds=1)),
            timeout=ROW_WRITE_BOUND_S)
    except Exception as exc:  # noqa: BLE001
        log.warning("identity hold: saving the refusal note failed (non-fatal): %r", exc)
    return out
