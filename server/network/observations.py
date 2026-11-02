"""What this Host's bastions observed, for an app holding read:observations.

GET /api/network/observations (server/routes/network.py). Built for an app
that analyses the machines (the security assessment is the first planned):
it reads the raw facts here, cleans them in its own process and writes only
cleaned rows to the Host's `world`; nothing here is copied into `world`. No
app in this tree holds read:observations yet.

What comes back, per bastion paired to this Host (the same live pairings
list_bastions reads): identity, whether it can watch itself (the get_network
watch state), its detection summary, the scheduler inventory, the tool servers
its agent clients declare, whether its own record has holes, the model server
it reports, and the enforcement flags it attests to. Per agent: the rows
nac_agents.list_app_observations returns.

What never comes back: a delegation token, a bastion's bearer token, a pairing
code, an update approval, a management grant, an encryption or device key, or
a secret in a command line. The bastion sends an agent's command line and a
scheduled job's command as it found them (it redacts them only on its own
console), so both are redacted here the way an outside agent's read is
(server/agent_access/redact.py): the marker says a secret was there, the
value never leaves the Host.
Each bastion is built from named fields below, so a field added to the
heartbeat later reaches an app only when it is added here.

A bastion whose pairing withholds `observe` from this Host (watch state
`withheld`) is listed with its identity and watch state only, and its agent
rows are left out: an inventory stored before the person turned observe off is
not this Host's to hand on.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from typing import Any, Optional

from server.agent_access.redact import redact_command
from server.network import nac_agents, topology

logger = logging.getLogger(__name__)

# One page of agent rows. An agent row is under 2 KB with every field full, so a
# page stays well under the kernel's JSON body limit; a reader with more pages
# follows `next_since`.
AGENT_PAGE = 2000

# The enforcement report's flags, the booleans topology.upsert_heartbeat keeps
# when it trims an oversized report. Keys, grants and the anchor host stay out.
_ENFORCING_FLAGS = ("write_gate", "device", "second_factor", "stepup", "anchored", "is_anchor",
                    "approval_device", "sandbox", "action_ops")


_MAX_ID = 2 ** 63 - 1   # the row id is a BIGSERIAL

# "…12:00:00.5 00:00": a '+' offset after a URL's query parser read the '+'
# as a space. Only a space right before a trailing HH:MM that follows a
# full time is put back, so a date-time separator is never touched.
_MANGLED_OFFSET = re.compile(r"^(.*\d\d:\d\d:\d\d(?:\.\d+)?) (\d\d:\d\d)$")


def parse_since(raw: Optional[str]) -> Optional[tuple]:
    """`since` as the reader got it in `next_since`: "<ISO time>|<row id>".

    A bare ISO time is accepted too and means "everything after that time".
    Raises ValueError on anything else, which the route turns into a 400."""
    if raw is None or raw == "":
        return None
    ts_part, sep, id_part = raw.partition("|")
    ts = _dt.datetime.fromisoformat(_MANGLED_OFFSET.sub(r"\1+\2", ts_part))
    if ts.tzinfo is None:
        raise ValueError("since needs a time zone")
    if not sep:
        # After every row at that instant: no id is larger.
        return ts, _MAX_ID
    if not (id_part.isascii() and id_part.isdigit()) or int(id_part) > _MAX_ID:
        raise ValueError("since: the part after '|' is a row id")
    return ts, int(id_part)


def cursor_of(row: dict) -> str:
    """The `next_since` for a reader whose last row is `row`: its last_seen in
    UTC written with Z, then '|', then its id. No '+' in it, so a reader that
    pastes it into a URL without encoding gets the same cursor back."""
    ts = _dt.datetime.fromisoformat(str(row["last_seen"])).astimezone(_dt.timezone.utc)
    return ts.isoformat().replace("+00:00", "Z") + f"|{int(row['id'])}"


def _withholds_observe(b: dict) -> bool:
    """The pairing on this machine does not hold `observe`, by the box's own
    last enforcing report: the rule _bastion_watch_state applies, read here
    whether or not the box is ONLINE. _bastion_watch_state answers "unknown"
    for any offline box before it looks, and what an offline box stored while
    it could still say is exactly what must not be handed on."""
    enf = b.get("enforcing")
    if not isinstance(enf, dict):
        return False
    pc = enf.get("pairing_caps")
    return bool(enf.get("anchored")) and not enf.get("is_anchor") and isinstance(pc, list) \
        and "observe" not in pc


def _bastion(b: dict, extra: dict) -> dict:
    from mastercode.runtime.tools import _bastion_detect_state, _bastion_watch_state
    watch, watch_reason = _bastion_watch_state(b)
    if watch != "withheld" and _withholds_observe(b):
        watch, watch_reason = "withheld", (
            "offline, and its last report says our pairing on that machine does not hold "
            "'observe': what it reported before that is not shared.")
    out = {
        "token_id": b.get("id"),
        "hostname": b.get("hostname"),
        "platform": b.get("platform"),
        "bastion_version": b.get("bastion_version"),
        "online": bool(b.get("online")),
        "last_seen_at": b.get("last_seen_at"),
        "agent_watch": watch,
        "agent_watch_reason": watch_reason,
    }
    if watch == "withheld":
        return out
    detect_state, detect_reason = _bastion_detect_state(b)
    enf = b.get("enforcing")
    out.update({
        "capabilities": [c for c in (b.get("capabilities") or []) if isinstance(c, str)],
        "detect_state": detect_state,
        "detect_reason": detect_reason,
        "detect": b.get("detect"),
        "schedulers": [dict(e, command=redact_command(e.get("command")))
                       for e in (b.get("schedulers") or []) if isinstance(e, dict)],
        "scheduler_scan": b.get("scheduler_scan"),
        "tool_servers": extra.get("tool_servers"),
        "tool_server_scan": extra.get("tool_server_scan"),
        "machine_record_dropped": b.get("record_dropped"),
        "llm_models": extra.get("llm_models") or [],
        "llm_port": extra.get("llm_port"),
        "enforcing": ({k: enf.get(k) is True for k in _ENFORCING_FLAGS if k in enf}
                      if isinstance(enf, dict) else None),
    })
    return out


def _json(v: Any, fallback):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return fallback
    return fallback if v is None else v


async def _heartbeat_extra(pool, token_ids: list) -> dict:
    """The heartbeat columns list_bastions does not select, by token id. Also
    read by GET /api/network/bastions for the Network app's tool servers."""
    if not token_ids:
        return {}
    try:
        rows = await pool.fetch(
            "SELECT token_id, tool_servers, tool_server_scan, llm_models, llm_port "
            "FROM network_bastion_heartbeat WHERE token_id = ANY($1::integer[])", token_ids)
    except Exception as e:
        logger.info("observation heartbeat read unavailable: %r", e)
        return {}
    return {r["token_id"]: {"tool_servers": _json(r["tool_servers"], None),
                            "tool_server_scan": _json(r["tool_server_scan"], None),
                            "llm_models": _json(r["llm_models"], []),
                            "llm_port": r["llm_port"]} for r in rows}


async def for_app(pool, entity_id: int, since: Optional[tuple] = None, limit: int = AGENT_PAGE) -> dict:
    """{bastions, agents, next_since, more}. Bastions are always whole (a few
    per Host); agents are one page from `since`, oldest first."""
    bastions = await topology.list_bastions(pool, entity_id)
    extra = await _heartbeat_extra(pool, [b["id"] for b in bastions if isinstance(b.get("id"), int)])
    shaped = [_bastion(b, extra.get(b.get("id"), {})) for b in bastions]
    # An ALLOWLIST of the bastions listed above and not withheld. A bastion
    # list that could not be read comes back empty (list_bastions returns []
    # on an error), and a denylist built from it would have let every row
    # through, withheld pairings' included.
    shown = {b["token_id"] for b in shaped if b["agent_watch"] != "withheld"}
    rows = await nac_agents.list_app_observations(pool, entity_id, since=since, limit=limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    # The cursor comes from the last row READ, kept or not, so a page that ends
    # on rows from a withheld bastion still moves the reader forward.
    next_since = cursor_of(rows[-1]) if rows else None
    agents = [dict(r, cmdline=redact_command(r.get("cmdline")))
              for r in rows if r.get("token_id") in shown]
    return {"bastions": shaped, "agents": agents, "next_since": next_since, "more": more}
