"""
Entity Clock — how a schedule's recurrence reads.

`_recurrence_seconds` is the one reading of a recurrence string, shared by the
scheduling tool's validation, the scheduled-turn runner and the Timeline.

(The EntityClock class — an adaptive-tick scanner that fired calendar signals —
was never instantiated anywhere and was removed 2026-09-30; scheduled work runs
through brain/offline_task_runner.py.)
"""


def _recurrence_seconds(rule: str) -> int | None:
    """Map an RRULE-lite recurrence string to a period in seconds, or None if the
    rule is empty/unrecognized (then the event is one-shot — it does not re-arm).
      hourly=3600  daily=86400  weekly=604800  monthly≈30d (2592000)  every:<seconds>
    'monthly' is an approximate 30-day period (v1; calendar-month precision later)."""
    r = (rule or "").strip().lower()
    table = {"hourly": 3600, "daily": 86400, "weekly": 604800, "monthly": 2592000}
    if r in table:
        return table[r]
    if r.startswith("every:"):
        try:
            n = int(r.split(":", 1)[1])
            return n if n > 0 else None
        except (ValueError, IndexError):
            return None
    return None
