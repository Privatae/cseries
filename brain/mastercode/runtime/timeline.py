"""
runtime/timeline.py — the TIMELINE block.

Every turn opens on what the platform knows is going on across the Host's
work: what is running now, what just finished, and what is next. It is built
from the database and the turn registry, never inferred, and it is the ONLY
thing that crosses turn types (there are no hidden notes
between turns; a thread is the Host's chain of thought).

What crosses, exactly: each line is a platform fact — the item's type, what
kind of work it is, its id, its state and its time. An item's own words (a
scheduled task's title, an agent's title, the message a turn answers) are
shown only when the item is of the reading turn's own type. Across types,
those words would be a note one turn leaves for another, which is the threat
this design removes. A closed turn (its type could not be read) sees no
item's words at all. On a Host with no types, every item and every turn are
untyped, so every line shows its title, as a plain timeline would.

The block is absent when there is nothing to list.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

log = logging.getLogger("cseries.mastercode.runtime.timeline")

# How far back "just finished" reaches. Long enough to cover a build or a
# nightly task that ended while the user was away; the registry keeps a
# finished chat turn for less than this (it forgets them after ~15 minutes).
FINISHED_WINDOW_S = 6 * 3600
# Lines per section; the rest is counted, not listed. Three sections of five
# lines keep the block near the design's 1-2 KB.
PER_SECTION = 5
# How much of an item's own words a line carries (only ever for its own type).
TITLE_CHARS = 60
# Routine work: scheduled work that runs more often than hourly. Its successful
# runs are not news and its next run is always minutes away, so neither is
# listed; a routine run that FAILED is listed like any other. Measured: two ops every 5 minutes made 144 successful runs in
# the 6-hour window, and the finished read (LIMIT 40) was filled with them —
# a real build or wake-up that ended in the same window could not appear.
ROUTINE_PERIOD_S = 3600


def _is_routine(rule) -> bool:
    """Scheduled work that repeats more often than hourly, read the way the
    scheduler itself reads the schedule (entity_clock._recurrence_seconds) —
    the one rule, never re-expressed in SQL, so every form the scheduler
    accepts ("every: 300", "every:+300", an absurdly long number) is judged
    exactly as it will run."""
    try:
        from mastercode.brain.entity_clock import _recurrence_seconds
        secs = _recurrence_seconds(str(rule or ""))
    except Exception:  # noqa: BLE001
        return False
    return bool(secs) and secs < ROUTINE_PERIOD_S

_TASK_KIND = {
    "agent": "agent",
    "factory_build": "build",
    "fan_out": "compare",
    "app_verify": "app check",
}
_RUNNING_TASK = ("queued", "in_progress", "blocked")
_DONE_TASK = ("completed", "failed", "cancelled")


def _aware(ts):
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


async def timeline_facts(pool, sess, *, now: datetime | None = None,
                         turns: list[dict] | None = None) -> dict:
    """Gather the timeline. Every source is read on its own and degrades to
    absence. `turns` is the registry listing (list_turns), read here when
    not given."""
    now = now or datetime.now(timezone.utc)
    eid = sess.entity_id
    facts: dict = {"now": now, "running": [], "finished": [], "next": [],
                   "types": {}, "typed_host": False, "types_unreadable": False,
                   "counts": {"running": 0, "finished": 0, "next": 0}}
    since = now - timedelta(seconds=FINISHED_WINDOW_S)

    # The Host's types, by id: a line names its type, never infers it. Only a
    # type the person approved has a name here (approved_at is written only by
    # the approval card); a discarded draft never names a line.
    try:
        rows = await pool.fetch(
            "SELECT id::text AS id, name FROM turn_types "
            "WHERE entity_id=$1 AND approved_at IS NOT NULL", eid)
        facts["types"] = {str(r["id"]): r["name"] for r in rows}
        facts["typed_host"] = bool(rows)
    except Exception as e:  # noqa: BLE001
        # Whether this Host has types is then unknown, so no item's words may
        # be shown to anyone (render_timeline): never guessed as "no types".
        facts["types_unreadable"] = True
        log.debug("timeline: turn types unavailable (non-fatal): %s", e)

    running, finished, upcoming = [], [], []

    # Chat turns, from the turn registry. A wake-up's turn is listed through
    # its scheduled task below (one line per piece of work, with the id the
    # Host's tools take), so its registry lane is skipped here.
    if turns is None:
        try:
            from server.turn_registry import get_turn_registry
            turns = get_turn_registry().list_turns(entity_id=eid)
        except Exception as e:  # noqa: BLE001
            log.debug("timeline: turn registry unavailable (non-fatal): %s", e)
            turns = []
    own_session = getattr(sess, "session_id", None)
    try:
        from server.turn_registry import CURRENT_THREAD_KEY
        own_key = CURRENT_THREAD_KEY.get()
    except Exception:  # noqa: BLE001
        own_key = None
    for d in turns or []:
        if str(d.get("thread_key") or "").startswith("wakeup|"):
            continue
        status = d.get("status")
        # This turn: its registry key (set before its task started, so it is
        # known here), else its session id once the session frame has landed.
        if status == "running" and (
                (own_key and d.get("thread_key") == own_key)
                or (own_session and d.get("session_id") == own_session)):
            continue
        item = {
            "kind": "chat turn",
            "ref": None,
            "type_id": d.get("turn_type_id"),
            "closed": d.get("turn_type_status") == "closed",
            # The type arrives on the turn's 2:turn_type frame a moment after
            # it starts. Until then it is not known — and never guessed as
            # untyped, which would show a typed turn's words to an untyped
            # reader.
            "unknown": not d.get("turn_type_status"),
            # A platform-started turn's opening line is a label, not the
            # user's words; neither is shown across types.
            "text": d.get("user_message"),
            "at": _aware(d.get("started_at")),
        }
        if status == "running":
            running.append(item)
        else:
            fin = _aware(d.get("finished_at"))
            if fin is None or fin < since:
                continue
            item["at"] = fin
            item["outcome"] = {"done": "done", "error": "failed",
                               "cancelled": "stopped", "timeout": "timed out"}.get(
                                   str(status), str(status))
            finished.append(item)

    # Agents, builds, compares, app checks: entity_tasks. Two reads, each
    # through the (entity_id, status) index, each with its own total, so a
    # busy Host's counts stay exact ("…and N more").
    task_rows = []
    extra = {"running": 0, "finished": 0}
    for key, statuses, order, extra_where, extra_args in (
            ("running", list(_RUNNING_TASK), "COALESCE(started_at, created_at) DESC", "", []),
            ("finished", list(_DONE_TASK), "completed_at DESC", " AND completed_at > $3", [since])):
        try:
            rows = await pool.fetch(
                "SELECT task_id, title, task_type, status, created_at, started_at, "
                "       completed_at, turn_type_id::text AS type_id, "
                "       count(*) OVER () AS total "
                "  FROM entity_tasks "
                " WHERE entity_id=$1 AND status = ANY($2::text[])" + extra_where +
                f" ORDER BY {order} LIMIT {PER_SECTION}",
                eid, statuses, *extra_args)
            task_rows += list(rows)
            if rows:
                extra[key] += max(0, int(rows[0]["total"] or 0) - len(rows))
        except Exception as e:  # noqa: BLE001
            log.debug("timeline: %s work unavailable (non-fatal): %s", key, e)
    for r in task_rows:
        item = {
            "kind": _TASK_KIND.get(str(r["task_type"] or ""), str(r["task_type"] or "task")),
            "ref": f"task {r['task_id']}",
            "type_id": r["type_id"],
            "text": r["title"],
        }
        if r["status"] in _RUNNING_TASK:
            item["at"] = _aware(r["started_at"] or r["created_at"])
            item["state"] = "queued" if r["status"] == "queued" else (
                "blocked" if r["status"] == "blocked" else "running")
            running.append(item)
        else:
            item["at"] = _aware(r["completed_at"])
            item["outcome"] = {"completed": "done", "failed": "failed",
                               "cancelled": "cancelled"}.get(str(r["status"]), str(r["status"]))
            finished.append(item)

    # Routine work — scheduled work repeating more often than hourly — is
    # judged here, in Python, by the scheduler's own reading of each schedule,
    # and handed to both reads below as a list of event ids.
    routine_ids: list[str] = []
    try:
        rec_rows = await pool.fetch(
            "SELECT event_id::text AS event_id, recurrence FROM calendar_events "
            " WHERE entity_id=$1 AND event_type='scheduled_task' "
            "   AND recurrence IS NOT NULL AND recurrence <> ''",
            eid)
        routine_ids = [str(r["event_id"]) for r in rec_rows if _is_routine(r["recurrence"])]
    except Exception as e:  # noqa: BLE001
        log.debug("timeline: schedules unavailable (non-fatal): %s", e)

    # Scheduled work: running now, and next.
    try:
        # Routine work is left out of "Next" in SQL, so the totals below never
        # count it; one that is running right now is still shown as running.
        ev_rows = await pool.fetch(
            "SELECT event_id::text AS event_id, title, start_time, recurrence, status, "
            "       payload->>'type_id' AS type_id, (payload ? 'op') AS is_op, "
            "       count(*) OVER (PARTITION BY status) AS total "
            "  FROM calendar_events "
            " WHERE entity_id=$1 AND event_type='scheduled_task' "
            "   AND status IN ('scheduled', 'running') "
            "   AND (status = 'running' OR NOT (event_id::text = ANY($2::text[]))) "
            " ORDER BY start_time ASC LIMIT 40",
            eid, routine_ids)
    except Exception as e:  # noqa: BLE001
        log.debug("timeline: scheduled work unavailable (non-fatal): %s", e)
        ev_rows = []
    ev_seen = {"running": 0, "scheduled": 0}
    for r in ev_rows:
        ev_seen[r["status"]] = int(r["total"] or 0)
        item = {
            "kind": "scheduled op" if r["is_op"] else "wake-up",
            "ref": f"event_id {r['event_id']}",
            "type_id": r["type_id"],
            "text": r["title"],
            "at": _aware(r["start_time"]),
            "recurrence": r["recurrence"],
        }
        if r["status"] == "running":
            item["state"] = "running"
            running.append(item)
        else:
            upcoming.append(item)

    # Scheduled runs that just finished.
    try:
        from mastercode.brain.offline_task_runner import CEILING_SKIP_PREFIX
    except Exception:  # noqa: BLE001
        # Never matches a real error, and — unlike a NUL — is valid text for
        # the SQL parameter below.
        CEILING_SKIP_PREFIX = "\uffff"
    try:
        # Routine work, in SQL before the LIMIT so it can never crowd a real
        # result out of the read: its successful runs are left out, and of its
        # failed runs only the latest is kept, carrying how many there were.
        # A ceiling skip (stored ok=false) is its own group, so it neither
        # hides a real failure nor inflates the failure count.
        skip_expr = "(left(coalesce(r.error, ''), length($4)) = $4)"
        run_rows = await pool.fetch(
            "SELECT event_id, title, ran_at, ok, error, type_id, is_op, routine, n_same, "
            "       count(*) OVER () AS total "
            "  FROM (SELECT e.event_id::text AS event_id, e.title, r.ran_at, r.ok, r.error, "
            "               e.payload->>'type_id' AS type_id, (e.payload ? 'op') AS is_op, "
            "               (e.event_id::text = ANY($3::text[])) AS routine, "
            "               row_number() OVER (PARTITION BY r.event_id, r.ok, "
            f"                                 {skip_expr} ORDER BY r.ran_at DESC) AS rn, "
            "               count(*) OVER (PARTITION BY r.event_id, r.ok, "
            f"                             {skip_expr}) AS n_same "
            "          FROM calendar_event_runs r "
            "          JOIN calendar_events e ON e.id = r.event_id AND e.entity_id = r.entity_id "
            "         WHERE r.entity_id=$1 AND r.ran_at > $2) x "
            " WHERE NOT x.routine OR (NOT x.ok AND x.rn = 1) "
            " ORDER BY ran_at DESC LIMIT 40",
            eid, since, routine_ids, CEILING_SKIP_PREFIX)
    except Exception as e:  # noqa: BLE001
        log.debug("timeline: finished runs unavailable (non-fatal): %s", e)
        run_rows = []
    runs_total = int(run_rows[0]["total"] or 0) if run_rows else 0
    for r in run_rows:
        err = str(r["error"] or "")
        finished.append({
            "kind": "scheduled op" if r["is_op"] else "wake-up",
            "ref": f"event_id {r['event_id']}",
            "type_id": r["type_id"],
            "text": r["title"],
            "at": _aware(r["ran_at"]),
            "outcome": ("skipped (daily turn ceiling)" if err.startswith(CEILING_SKIP_PREFIX)
                        else ("done" if r["ok"] else
                              (f"failed ({int(r['n_same'])} times in "
                               f"{FINISHED_WINDOW_S // 3600} h)"
                               if r["routine"] and int(r["n_same"] or 0) > 1 else "failed"))),
        })

    _epoch = datetime.fromtimestamp(0, tz=timezone.utc)
    running.sort(key=lambda i: i.get("at") or _epoch, reverse=True)
    finished.sort(key=lambda i: i.get("at") or _epoch, reverse=True)
    upcoming.sort(key=lambda i: i.get("at") or _epoch)
    # Totals, not just what was read: each source was read with a limit, and
    # its own count says how many it holds.
    ev_running_read = sum(1 for i in running if i["kind"] in ("wake-up", "scheduled op"))
    facts["counts"] = {
        "running": len(running) + extra["running"]
        + max(0, ev_seen["running"] - ev_running_read),
        "finished": len(finished) + extra["finished"]
        + max(0, runs_total - len(run_rows)),
        "next": max(len(upcoming), ev_seen["scheduled"]),
    }
    facts["running"] = running[:PER_SECTION]
    facts["finished"] = finished[:PER_SECTION]
    facts["next"] = upcoming[:PER_SECTION]
    return facts


def _when(ts: datetime | None, now: datetime) -> str:
    if ts is None:
        return "time unknown"
    clock = ts.strftime("%H:%M UTC") if ts.date() == now.date() else ts.strftime("%b %d %H:%M UTC")
    delta = int((ts - now).total_seconds())
    ahead = delta > 0
    s = abs(delta)
    if s < 90:
        rel = "now"
    elif s < 5400:
        rel = f"{s // 60} min"
    elif s < 172800:
        rel = f"{s // 3600} h"
    else:
        rel = f"{s // 86400} days"
    if rel != "now":
        rel = f"in {rel}" if ahead else f"{rel} ago"
    return f"{clock} ({rel})"


def _every(rule) -> str | None:
    """A recurrence in the platform's own words: parsed, never printed as
    stored — the stored string is Host-written and crosses types here."""
    try:
        from mastercode.brain.entity_clock import _recurrence_seconds
        secs = _recurrence_seconds(str(rule or ""))
    except Exception:  # noqa: BLE001
        secs = None
    if not secs:
        return None
    named = {3600: "hourly", 86400: "daily", 604800: "weekly", 2592000: "monthly"}
    if secs in named:
        return named[secs]
    # The largest unit it fills, to two significant figures: a period, not a
    # number — a long exact integer could carry digits from one type to
    # another.
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        if secs >= n:
            val = secs / n
            rounded = float(f"{val:.2g}")
            k = rounded
            about = "" if abs(rounded - val) < 1e-9 else "about "
            return f"every {about}{k:g} {unit}{'s' if k != 1 else ''}"
    return None


def _clean(text) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip().replace('"', "'")
    return (t[:TITLE_CHARS - 1] + "…") if len(t) > TITLE_CHARS else t


def render_timeline(facts: dict, viewer_type: dict | None) -> str | None:
    """The "Timeline" block, for a turn of `viewer_type` (None = untyped)."""
    f = facts or {}
    now = f.get("now") or datetime.now(timezone.utc)
    sections = (("Running now", f.get("running") or [], "running"),
                ("Just finished (last 6 h)", f.get("finished") or [], "finished"),
                ("Next", f.get("next") or [], "next"))
    if not any(items for _, items, _k in sections):
        return None

    v = viewer_type if isinstance(viewer_type, dict) else None
    # Whether this Host has types could not be read: no item's words at all.
    no_words = bool(f.get("types_unreadable"))
    viewer_closed = bool(v) and v.get("status") != "active"
    viewer_id = str(v["id"]) if v and not viewer_closed and v.get("id") else None
    types = f.get("types") or {}
    labelled = bool(f.get("typed_host")) or bool(f.get("types_unreadable")) or any(
        i.get("type_id") for _, items, _k in sections for i in items)

    def _label(item) -> str:
        if not labelled:
            return ""
        tid = item.get("type_id")
        if item.get("unknown"):
            return "[type not known yet] "
        if item.get("closed"):
            return "[closed type] "
        if not tid:
            return "[no type] "
        return f"[{types.get(str(tid), 'a removed type')}] "

    def _words(item) -> str:
        if no_words or viewer_closed or item.get("closed"):
            return ""
        if item.get("unknown") and labelled:
            return ""   # its type is not known yet; its words cross nothing
        if (str(item.get("type_id")) if item.get("type_id") else None) != viewer_id:
            return ""
        t = _clean(item.get("text"))
        return f' "{t}"' if t else ""

    lines = ["## Timeline",
             "Platform records as of this turn, across all your work. Re-read an item "
             "before acting on it: schedule_task op='list' (event_id) for scheduled work, "
             "entity_tasks op='list' (task_id) for agents and builds."]
    if labelled and (viewer_id or viewer_closed):
        lines.append("An item's own words are shown only for work of this turn's type.")
    counts = f.get("counts") or {}
    for title, items, key in sections:
        if not items:
            continue
        lines.append(f"{title}:")
        for it in items:
            ref = f" {it['ref']}" if it.get("ref") else ""
            head = f"- {_label(it)}{it['kind']}{ref}{_words(it)}"
            if key == "running":
                state = it.get("state") or "running"
                lines.append(f"{head} — {state}, since {_when(it.get('at'), now)}")
            elif key == "finished":
                lines.append(f"{head} — {it.get('outcome') or 'finished'}, "
                             f"{_when(it.get('at'), now)}")
            else:
                rec = _every(it.get("recurrence"))
                lines.append(f"{head} — due {_when(it.get('at'), now)}"
                             + (f", repeats {rec}" if rec else ""))
        more = int(counts.get(key) or 0) - len(items)
        if more > 0:
            lines.append(f"- …and {more} more")
    return "\n".join(lines)
