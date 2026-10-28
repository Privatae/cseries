"""
runtime/steering.py — the switch for the turn loop's mid-turn steering.

Steering is the platform's advice injected into a running turn: the
go-ahead guard's repair round (go_ahead_guard, the deferred-work rule
included) and the per-failure "say what failed / report the refusal" lines
(tool_failures.note_round). None of it is a wall: gates, approvals,
untrusted fences, spend limits and the round backstop are not steering and
do not read this switch. Nor is the false-claim check (claim_guard): it
holds a reply that says work started against the rows that would show it
did ("phase 39 is running" with no build row behind it), costs a round only
when a reply claims a start nothing backs, and runs whatever this switch
says.

OFF FOR EVERY HOST. Measured on one Host, the same six demo scenes twice
each way: with it off the scenes ran ~19% faster and ~20% shorter, with no
"these lookups failed" lines (5 with it on) and no forced repair rounds
(4 of 12 turns with it on), on the same number of model calls, and with as
many asks left in the final replies either way: the repair round did not
take the asks out, it added a round after them.

On again is one row in the Host's own database, written by hand (no route
writes it): app_config (app_id 'host', key 'steering', value "on"). The
Host cannot write it: its data tool reaches only its `world` schema
(migrations/v233_host_world.sql leaves app_config closed to host_world);
the one app_config writer, PUT /api/applications/{app_id}/config/{key},
takes only that app's own token, 'host' is an id no manifest may carry
(manifest.RESERVED_APP_IDS), and kernel_op's 'host' token reaches no
config route (scripts/test_claim_guard.py 3c). Read once per turn;
anything absent or unreadable reads as off.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("cseries.mastercode.runtime.steering")

APP_ID = "host"
KEY = "steering"


async def is_off(pool) -> bool:
    """True unless the Host's row says "on". Never raises."""
    if pool is None:
        return True
    try:
        v = await pool.fetchval(
            "SELECT value FROM app_config WHERE app_id = $1 AND key = $2", APP_ID, KEY)
    except Exception as e:  # noqa: BLE001 — unreadable reads as off
        log.debug("steering switch unreadable (off): %r", e)
        return True
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (TypeError, ValueError):
            pass
    return v != "on"
