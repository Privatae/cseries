"""
runtime/agent_rules.py — the person's own rules for their Host.

`build_host_rules(pool, entity_id)` returns the per-entity `host_rules`
override from behavioral_params (user-editable), headed, or "" when there is
none. The platform rulebook that used to ride beside it (GLOBAL_RULES /
HOST_RULES) restated what the verb schemas say and was removed with the
brief-prompt slice (2026-10-11): the verb schemas are the one copy of the verb
rules (session._orchestration_verbs).

Dispatched agents do NOT share this rulebook: each agent's
system prompt is HOST-WRITTEN per task (dispatch tool `system_prompt`) under the
immutable governance wall in scoped_agent._SCOPED_AGENT_ROLE.
"""

import json
import logging

log = logging.getLogger("cseries.mastercode.agent_rules")


async def _load_override(pool, entity_id: int, key: str) -> str | None:
    """Return the per-entity rule override string at
    behavioral_params.<key> (`host_rules` / `agent_rules`), or None when absent.
    Never raises — a lookup failure falls back to the baked-in defaults."""
    try:
        bp = await pool.fetchval(
            "SELECT behavioral_params FROM entity_registry WHERE id=$1", entity_id,
        )
    except Exception as e:
        log.warning("agent_rules: behavioral_params read failed (eid=%s): %s", entity_id, e)
        return None
    if not bp:
        return None
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(bp, dict):
        return None
    val = bp.get(key)
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


async def build_host_rules(pool, entity_id: int) -> str:
    """The person's own rules for this Host (behavioral_params.host_rules),
    headed, or "" when they wrote none."""
    override = await _load_override(pool, entity_id, "host_rules")
    return f"## Rules the person set for you\n{override}" if override else ""
