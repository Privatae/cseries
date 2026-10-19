"""Host events: the one way a scheduled task is woken early because the thing
it waits for has happened.

In a Telegram connector test the Host scheduled a turn instead of monitoring;
some cases need a monitor rather than a schedule. The Host had started a pairing and
set a fixed fifteen-minute
check, because a fixed time was the only wake it had. Nothing could bring it
back when the pairing matched or was approved.

schedule_task op='create' takes `wake_on: {event, match}` with its fire_at
(runtime/tools.py _exec_schedule_task). The row waits like any one-shot task.
emit() is called at the single place each state change happens, AFTER it has
committed, and it pulls every matching waiting task forward:

    start_time = NOW(), payload.woke_on = {event, attrs, at}, then kick_poll()

so the offline runner claims it on its next poll, and its pointer line
(offline_task_runner.pointer_line) says the thing happened. A task the event
never reaches runs at fire_at, which is its deadline, and its pointer says no
such event was recorded since it was set (never that it did not happen: a wake
emit() could not apply runs at the deadline too). The Host chooses the deadline.

Execution stays directed: nothing here runs a
model or looks for work. A wake is a row the Host wrote, and the platform moves
its time when the platform's own code changes state.

What emit() is given are ids and names only. Each event declares its keys
below, and emit() keeps those and drops anything else, so a value passed by
mistake (a key's secret, a message) never reaches the row or the model. A
vault event carries the key's NAME.

emit() never raises into the caller and never holds it: the state change it
reports has already happened, so emit() waits at most EMIT_WAIT_S for its one
statement (a free connection, a row another transaction holds), and a wake
that could not be applied leaves the task to run at its deadline.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("privatae.host_events")

# The longest emit() keeps the caller (a person's click, a connector's request,
# a key being stored) waiting for the wake. The statement itself takes
# milliseconds; past this it gives up and the task runs at its deadline.
EMIT_WAIT_S = 5.0

# Keys whose values the platform stores lower-cased (a site's slug and
# hostname, a connector's platform), so a match written as "MySite" or
# "Telegram" is the same value as the one the event carries.
_CASELESS = frozenset({"slug", "hostname", "platform"})


# The fixed set. Each event: the attribute keys it carries (a `match` may name
# only these), and where it is emitted, in words the skill repeats.
EVENTS: dict[str, dict[str, Any]] = {
    "connector_paired": {
        "keys": ("grant_id", "app_id", "platform", "ticket_id"),
        "when": "a messaging account sent the pairing code: the pairing now waits for "
                "the person's approval (ticket_id is the approval card)",
    },
    "connector_approved": {
        "keys": ("grant_id", "app_id", "platform"),
        "when": "the person let the paired account in: the connector grant is live",
    },
    "connector_declined": {
        "keys": ("grant_id", "app_id", "why"),
        "when": "a pairing closed before it was approved, other than by its code expiring; "
                "why is declined (the person said no), wrong_codes (five wrong codes), "
                "replaced (a newer pairing of the app) or revoked (the connector was revoked)",
    },
    "connector_expired": {
        "keys": ("grant_id", "app_id"),
        "when": "a pairing's code expired before any account sent it; recorded when the "
                "pairing is next read (expire_pairings: the Network list, the code shown) "
                "or a code arrives (submit_code), not when the clock runs out",
    },
    "connector_revoked": {
        "keys": ("grant_id", "app_id"),
        "when": "a live connector grant was revoked (Network, revoke_nac_rule, an "
                "uninstall, or a newer account approved for the same app)",
    },
    "nac_approved": {
        "keys": ("kind", "id"),
        "when": "a NAC draft was approved and signed; kind is policy, egress, mgmt_grant, "
                "dcn_store or dcn_move_plan, id is the draft's id",
    },
    "nac_revoked": {
        "keys": ("kind", "id"),
        "when": "a NAC rule was revoked or a NAC draft declined; kind and id as for nac_approved",
    },
    "ticket_decided": {
        "keys": ("ticket_id", "status", "option_id"),
        "when": "a ticket was answered: status decided (option_id is the option picked) "
                "or dismissed",
    },
    "key_added": {
        "keys": ("name",),
        "when": "a key was stored in Keys under this name (a new key, or a new value "
                "for an existing name); the value is never carried",
    },
    "app_proven": {
        "keys": ("app_id", "project_id", "task_id"),
        "when": "an app's post-ship proof passed: it is installed and running",
    },
    "app_proof_failed": {
        "keys": ("app_id", "project_id", "task_id", "verdict"),
        "when": "an app's post-ship proof did not pass; verdict says how",
    },
    "site_published": {
        "keys": ("project_id", "repo", "slug", "hostname"),
        "when": "a site was published (from a project, or from a repo) and is live at hostname",
    },
}

EVENT_NAMES: tuple[str, ...] = tuple(EVENTS)


def norm_value(v: Any, key: str = "") -> Optional[str]:
    """One spelling for a match value and an attribute, so `5` and `"5"` are
    the same id, and a caseless key's value is lower-cased (_CASELESS).
    Scalars only; anything else is None (not matchable)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        s = v.strip()
        if key in _CASELESS:
            s = s.lower()
        return s or None
    return None


def check_wake_on(raw: Any) -> tuple[Optional[dict], Optional[str]]:
    """(wake_on, None) for a valid schedule_task wake_on, else (None, why).
    The stored form is {"event": name, "match": {key: str}}."""
    if not isinstance(raw, dict):
        return None, ("wake_on must be an object {event, match}, e.g. "
                      '{"event": "key_added", "match": {"name": "TG_BOT_TOKEN"}}')
    unknown = sorted(set(raw) - {"event", "match"})
    if unknown:
        return None, f"wake_on takes event and match only (got {', '.join(unknown)})"
    event = str(raw.get("event") or "").strip()
    if event not in EVENTS:
        return None, f"wake_on.event must be one of: {', '.join(EVENT_NAMES)} (got {event!r})"
    match = raw.get("match")
    if match is None:
        match = {}
    if not isinstance(match, dict):
        return None, "wake_on.match must be an object of equality keys"
    keys = EVENTS[event]["keys"]
    out: dict[str, str] = {}
    for k, v in match.items():
        if k not in keys:
            return None, (f"{event} can be matched on {', '.join(keys)}; {k!r} is not one "
                          "of them, so it would never match")
        nv = norm_value(v, k)
        if nv is None:
            return None, f"wake_on.match.{k} must be a non-empty string or a whole number"
        out[k] = nv
    return {"event": event, "match": out}, None


def describe(event: str, attrs: Optional[dict]) -> str:
    """`event {"k": "v", …}` — how a wake's event is written into a pointer
    line and a plan. The attrs are ids and names (see the module docstring)."""
    a = attrs if isinstance(attrs, dict) else {}
    return f"{event} {json.dumps(a, sort_keys=True)}" if a else str(event)


async def emit(pool, entity_id: Optional[int], event: str, attrs: Optional[dict]) -> int:
    """The state change `event` has committed: wake every scheduled task of
    this entity waiting on it whose match is contained in `attrs`. Returns how
    many were woken. Never raises.

    `entity_id` None means this pod's own entity (brain_service._entity_id):
    a pod is one entity, and several choke points (the connector module) do
    not carry an id of their own. Only the event's declared keys are kept."""
    try:
        spec = EVENTS.get(event)
        if spec is None:
            log.warning("host event %r is not in the fixed set; nothing woken", event)
            return 0
        if pool is None:
            return 0
        if entity_id is None:
            from server import brain_service
            entity_id = brain_service._entity_id
        clean: dict[str, str] = {}
        for k in spec["keys"]:
            nv = norm_value((attrs or {}).get(k), k)
            if nv is not None:
                clean[k] = nv
        woke = {"event": event, "attrs": clean,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        # Only a row that waits on this event moves, and only its time: the
        # status stays 'scheduled' (the runner's claim is the one way to
        # running), and a row without wake_on (a saved request, a fixed-time
        # task) never matches. The saved request's state map lists this
        # statement as T24 (docs/DESIGN-saved-request-state-machine-2026-10-01.md).
        # Bounded (EMIT_WAIT_S): a row lock or an exhausted pool must not hold
        # the caller, whose change has already committed.
        rows = await asyncio.wait_for(pool.fetch(
            "UPDATE calendar_events SET start_time = NOW(), event_at = NOW(), updated_at = NOW(), "
            "payload = jsonb_set(payload, '{woke_on}', $3::jsonb) "
            "WHERE entity_id = $1 AND event_type = 'scheduled_task' AND status = 'scheduled' "
            "AND payload->'wake_on'->>'event' = $2 "
            "AND COALESCE(payload->'wake_on'->'match', '{}'::jsonb) <@ $4::jsonb "
            "AND NOT (payload ? 'woke_on') "
            "RETURNING id",
            int(entity_id), event, json.dumps(woke), json.dumps(clean)), EMIT_WAIT_S)
        n = len(rows or [])
        if n:
            log.info("host event %s: woke %d scheduled task(s) %s", describe(event, clean), n,
                     [r["id"] for r in rows])
            from mastercode.brain.offline_task_runner import kick_poll
            kick_poll()
        return n
    except Exception as e:  # noqa: BLE001 — the state change already stands
        log.warning("host event %s: waiting tasks not woken (they run at their deadline): %r",
                    event, e)
        return 0
