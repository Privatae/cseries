"""The reach-out dispatcher — one guarded loop beside the offline task runner.

Poll the bus (`inbox_messages`), match each new row against this host's enabled
triggers, and fire the matching channel once. Dispatched from server/main.py's
lifespan next to `run_offline_tasks_loop`, gated on `relay_entity_id > 0`, and
built to the same three guarantees that loop has: cancellation exits cleanly,
every other error is logged and the loop continues, and one failing item never
stops the pass.

WHY THERE IS NO CURSOR TABLE. The design gives reach-out three tables and the
high-water mark is derived from one of them rather than stored in a fourth:

    the newest inbox row this host has ALREADY considered
      = MAX(m.sent_at) over reachout_log joined back to inbox_messages

That is a fact the log already records, so there is nothing to keep in step.
It is read once at start and carried in memory after that. Two consequences,
both deliberate:

  * A host that has never fired has no backlog owed. On a pod that starts with
    an empty log the mark begins at NOW(), so switching reach-out on does not
    page the user about a week of finished builds. The alternative — replaying
    history on first enable — is the worst possible first impression for a
    feature whose entire job is to interrupt someone.
  * Once anything has fired, a restart resumes from that point and the work
    that landed while the pod was down is caught up on the next tick.

A TRIGGER NEVER MATCHES A ROW OLDER THAN ITSELF. The mark alone could not keep
the first promise: it only moves when a pass reads the bus, and a pass with no
trigger never did, so on a pod that had run for a week the mark still said
"boot" when the first trigger arrived — connecting Gmail builds three at once —
and the next pass read the whole week and mailed the oldest match. Two rules
now, either of which closes it and both of which hold: a pass with no trigger
moves the mark to now, and a row whose sent_at is before a trigger's own
created_at is never that trigger's to fire.

The query uses `>=`, not `>`, so two rows sharing a `sent_at` cannot lose one.
Re-considering a row costs nothing: the UNIQUE (trigger_id, message_id) on
reachout_log is the idempotency gate, and a second attempt gets no row back
and stops (service.fire).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from server.deep_link import app_link
from server.reachout import service

log = logging.getLogger("privatae.reachout.dispatcher")

POLL_INTERVAL_S = service.POLL_INTERVAL_S

# Rows examined per pass. A bound on work per tick, not a cap on the user: the
# mark only advances past what was examined, so a backlog is worked off across
# consecutive ticks rather than dropped. Sized above any plausible burst of
# finished agents in one 30-second window.
PER_PASS_MAX_MESSAGES = 200


async def _initial_mark(pool, entity_id: int) -> datetime:
    """The newest inbox row already considered, or now."""
    try:
        mark = await pool.fetchval(
            """
            SELECT MAX(m.sent_at)
              FROM reachout_log l
              JOIN inbox_messages m ON m.message_id = l.message_id
             WHERE l.entity_id = $1
            """,
            int(entity_id))
        if mark is not None:
            return mark
    except Exception as e:                      # noqa: BLE001
        log.warning("reachout: could not derive the high-water mark (%s) — "
                    "starting from now", e)
    return datetime.now(timezone.utc)


async def run_once(pool, entity_id: int, mark: datetime) -> datetime:
    """One pass. Returns the new high-water mark.

    Separated from the loop so the tests drive a real pass against a pool
    without a sleep in the way — the loop below adds nothing but the timer and
    the try/except, which is the shape that makes a loop testable.
    """
    await service.sweep_interrupted(pool, entity_id)

    # Read BEFORE the trigger query: a trigger committed between the two, and
    # a row landing in those milliseconds, must not fall behind the mark.
    started = datetime.now(timezone.utc)
    triggers = await pool.fetch(
        """
        SELECT t.id, t.channel_id, t.label, t.match_kind, t.match_event,
               t.match_jsonb, t.created_at,
               c.id AS c_id, c.name AS c_name, c.kind AS c_kind, c.action,
               c.cooldown_s, c.quiet_from, c.quiet_to, c.quiet_tz,
               c.disabled_at AS c_disabled_at
          FROM reachout_triggers t
          JOIN reachout_channels c ON c.id = t.channel_id
         WHERE t.entity_id = $1
           AND t.disabled_at IS NULL
        """,
        int(entity_id))
    if not triggers:
        # No subscription — do not even read the bus. A host with reach-out
        # unconfigured costs one cheap query per tick and nothing else. The
        # mark still MOVES: nothing that lands while no trigger exists can be
        # owed to one created later (the module docstring has the case).
        return max(mark, started)

    messages = await pool.fetch(
        """
        SELECT message_id, subject, message_kind, payload, sent_at
          FROM inbox_messages
         WHERE to_entity_id = $1
           AND sent_at >= $2
         ORDER BY sent_at ASC
         LIMIT $3
        """,
        int(entity_id), mark, PER_PASS_MAX_MESSAGES)
    if not messages:
        return mark

    host_name, user_email = await service.host_identity(pool, entity_id)
    # Once per pass, not per message: a reach-out is read outside the host, so
    # its link needs the host's own address (service.doorbell_link), and the
    # resolver may ask the bridge on a cold cache.
    host_origin = await service.resolve_host_origin(entity_id)

    new_mark = mark
    for m in messages:
        msg = dict(m)
        if msg["sent_at"] > new_mark:
            new_mark = msg["sent_at"]
        for t in triggers:
            trig = dict(t)
            born = trig.get("created_at")
            if born is not None and msg["sent_at"] < born:
                # Older than the trigger: not this trigger's to fire, whatever
                # the mark says (the module docstring has the case).
                continue
            if not service.matches(trig, msg):
                continue
            if trig.get("c_disabled_at") is not None:
                # A switched-off channel is a decision, and it is recorded as
                # one: the row says the trigger matched and nothing was sent,
                # rather than leaving the user to infer it from an absence.
                await _log_disabled(pool, entity_id, trig, msg)
                continue
            channel = {
                "id": trig["c_id"], "name": trig["c_name"], "kind": trig["c_kind"],
                "action": trig["action"], "cooldown_s": trig["cooldown_s"],
                "quiet_from": trig["quiet_from"], "quiet_to": trig["quiet_to"],
                "quiet_tz": trig["quiet_tz"],
            }
            try:
                res = await service.fire(
                    pool, entity_id, channel=channel, message=msg,
                    trigger_id=trig["id"], host_name=host_name,
                    user_email=user_email, host_origin=host_origin)
            except Exception as e:              # noqa: BLE001
                # service.fire does not raise on a transport failure; this
                # catches the database going away mid-pass, and one channel
                # never stops the rest.
                log.warning("reachout: trigger %s failed hard: %s", trig["id"], e)
                continue
            if res.get("status"):
                log.info("reachout: %s -> %s (%s)", trig["label"],
                         res["status"], trig["c_name"])
    return new_mark


async def _log_disabled(pool, entity_id: int, trig: dict, msg: dict) -> None:
    """Record a match against a switched-off channel, idempotently."""
    import json as _json
    try:
        await pool.execute(
            """
            INSERT INTO reachout_log (entity_id, trigger_id, channel_id,
                                      message_id, status, detail)
            VALUES ($1, $2, $3, $4::uuid, 'failed', $5::jsonb)
            ON CONFLICT (trigger_id, message_id) DO NOTHING
            """,
            int(entity_id), trig["id"], trig["c_id"], str(msg["message_id"]),
            _json.dumps({"error": f"channel {trig['c_name']} is switched off",
                         "channel_disabled": True}))
    except Exception as e:                      # noqa: BLE001
        log.warning("reachout: could not log the disabled-channel skip: %s", e)


async def _report_missing_schema(pool, entity_id: int, exc: Exception) -> None:
    """A reach-out table is absent. Say so ONCE, loudly, where the user looks.

    THIS IS THE DEFECT CLASS THE MIGRATION NUMBER CREATES. entrypoint.sh keys
    schema_migrations on the version NUMBER, so two migrations sharing one
    number leave the second silently unapplied — the host boots clean, nothing
    errors, and the only symptom is this loop warning into a log nobody reads
    while every reach-out the user configured quietly does not happen.

    A warning was the original handling here and it is exactly how that would
    have stayed invisible. So: ERROR, not warning; ONCE per boot rather than
    every 30 seconds, because a line repeated 2,880 times a day is noise and
    noise is how the one that matters gets filtered out; and an inbox row,
    because the Inbox is where this host tells its person something is wrong
    and a failure in the "tell the person" machinery is precisely the one that
    must not depend on itself to be noticed.
    """
    log.error(
        "reach-out is NOT RUNNING: its tables are missing from this host's "
        "database (%s). The migration that declares them did not apply — check "
        "that migrations/v152_reachout.sql ran, and that no other migration "
        "shares its version number. Reach-out will stay off until it does.",
        exc)
    try:
        from server.bridge.memory_handlers import handle_inbox_append
        await handle_inbox_append({
            "subject": "Reach-out is not running",
            "body": ("The tables reach-out needs are not in this host's "
                     "database, so nothing can page you when work finishes. "
                     "Nothing you configured was lost — it cannot be read. "
                     "This needs the migration to be applied."),
            "message_kind": "system_notification",
            "payload": {"event": "reachout_schema_missing",
                        "detail": str(exc)[:300],
                        "link": app_link("circles")},
        })
    except Exception as e:                      # noqa: BLE001
        log.error("reach-out: could not even write the inbox row about it: %s", e)


async def run_reachout_dispatcher_loop(pool, entity_id: int) -> None:
    """Forever: poll the bus, fire what matches. Dispatched at lifespan."""
    try:
        mark = await _initial_mark(pool, entity_id)
    except Exception:                           # noqa: BLE001
        mark = datetime.now(timezone.utc)
    log.info("reach-out dispatcher online — entity %d, poll %ds, from %s",
             entity_id, POLL_INTERVAL_S, mark.isoformat())
    reported_missing = False
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_S)
            mark = await run_once(pool, entity_id, mark)
        except asyncio.CancelledError:
            log.info("reach-out dispatcher stopping")
            raise
        except Exception as e:                  # noqa: BLE001
            # Caught by NAME, not by class: asyncpg's UndefinedTableError is
            # only importable where asyncpg is, and this module is imported by
            # the container-free gate too.
            if type(e).__name__ == "UndefinedTableError":
                if not reported_missing:
                    reported_missing = True
                    await _report_missing_schema(pool, entity_id, e)
                continue
            log.warning("reach-out dispatcher pass failed (continuing): %s", e)
