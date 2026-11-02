"""What an outside agent reads about one of the person's machines.

The tools in tools.py named after the bastion's own reads (privatae-bastion
`agents`, `schedules`, `tool-servers`, `posture`, `baseline diff`), so a skill
written against the bastion's CLI on the machine works against the Host's
/mcp with `machine` added. Each answers from what this Host already keeps off
the machine's heartbeat; nothing here reaches the machine.

THE MACHINE IS NAMED FROM THIS HOST'S OWN RECORDS. `machine` must be the
name `list_machines` gives (a bastion's hostname, else its pairing name); two
live bastions reporting one name are refused, as nac_mgmt.run_mgmt_op
refuses them, because a read of one and a contain of the other would be two
different machines.

WHAT IS NEVER HANDED OUT, beyond what the observations API already withholds
(server/network/observations.py): the file path of a tool server's settings
(an MCP caller gets the problem, never where the key is: a prompt-injected
agent must not be handed a map to the keys), and any secret in a command line (redact.py, the
bastion's own redaction). A machine whose pairing withholds `observe` from
this Host is answered with its watch state only, as for an app.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("cseries.agent_access")


class MachineProblem(Exception):
    """The machine cannot be read: `words` say why, `outcome` is the record's."""

    def __init__(self, words: str, outcome: str):
        super().__init__(words)
        self.words = words
        self.outcome = outcome


def machine_name(b: dict) -> Optional[str]:
    return b.get("hostname") or b.get("name")


async def bastions(pool, eid: int) -> list[dict]:
    from server.network import nac_commit
    data = await nac_commit.bastions_list(pool, eid)
    return [b for b in (data or {}).get("bastions") or [] if isinstance(b, dict)]


async def resolve(pool, eid: int, machine: str) -> dict:
    """The one bastion `machine` names in this Host's records, or MachineProblem."""
    found = [b for b in await bastions(pool, eid) if machine_name(b) == machine]
    if not found:
        raise MachineProblem(f"'{machine}' is not one of the person's machines. "
                             "`list_machines` lists them.", "refused: no such machine")
    if len(found) > 1:
        raise MachineProblem(f"{len(found)} of the person's machines report the name '{machine}', "
                             "so this Host cannot tell which one is meant. The person can tell them "
                             "apart in their Network app.", "refused: name shared")
    return found[0]


async def _shaped(pool, b: dict) -> dict:
    """observations._bastion for this one machine: identity, watch state, and
    (unless withheld) the heartbeat facts an app would get."""
    from server.network import observations
    tid = b.get("id")
    extra = await observations._heartbeat_extra(pool, [tid]) if isinstance(tid, int) else {}
    return observations._bastion(b, extra.get(tid, {}))


def _head(machine: str, s: dict) -> dict:
    return {"machine": machine, "online": s.get("online"), "last_seen_at": s.get("last_seen_at"),
            "agent_watch": s.get("agent_watch"), "agent_watch_reason": s.get("agent_watch_reason")}


def _withheld(s: dict) -> bool:
    return s.get("agent_watch") == "withheld"


# ── list_agents ─────────────────────────────────────────────────────────────

_AGENT_FIELDS = ("pid", "ppid", "kind", "signature", "family", "origin", "owner_class",
                 "owner_admin", "owner_interactive", "exe", "unit", "llm_endpoints", "evidence",
                 "first_seen", "last_seen", "active", "acknowledged", "carries_delegation",
                 "requested", "contain_pending_since")


async def list_agents(pool, eid: int, machine: str, *, include_inactive: bool = False) -> dict:
    from server.agent_access.redact import redact_command
    from server.network import nac_agents
    b = await resolve(pool, eid, machine)
    s = await _shaped(pool, b)
    out = _head(machine, s)
    if _withheld(s):
        out["agents"] = []
        return out
    try:
        rows, more = await nac_agents.list_machine_agents(pool, eid, int(b["id"]),
                                                          include_inactive=include_inactive)
    except Exception as exc:  # noqa: BLE001 — an unread list is never an empty one
        raise MachineProblem(f"the agents seen on '{machine}' could not be read right now",
                             "error: agents unread") from exc
    agents = []
    for r in rows:
        a = {k: r.get(k) for k in _AGENT_FIELDS}
        a["user"] = r.get("agent_user")
        a["cmdline"] = redact_command(r.get("cmdline"))
        agents.append(a)
    out["agents"] = agents
    out["more"] = more
    out["contain"] = ("contain(machine, pid) stops an agent or a client by its pid; apps are "
                      "shown, never contained, and the machine refuses any pid it did not "
                      "detect as an agent")
    return out


# ── list_scheduled ──────────────────────────────────────────────────────────

async def list_scheduled(pool, eid: int, machine: str) -> dict:
    from server.agent_access.redact import redact_command
    b = await resolve(pool, eid, machine)
    s = await _shaped(pool, b)
    out = _head(machine, s)
    if _withheld(s):
        out["schedules"] = []
        return out
    scan = s.get("scheduler_scan") if isinstance(s.get("scheduler_scan"), dict) else None
    out["schedulers_read"] = bool(scan and scan.get("supported"))
    out["scan"] = scan
    out["schedules"] = [{
        "kind": e.get("kind"), "name": e.get("name"), "schedule": e.get("schedule"),
        "user": e.get("user"), "agent": e.get("signature"), "automation": bool(e.get("automation")),
        "command": redact_command(e.get("command")),
    } for e in (s.get("schedulers") or []) if isinstance(e, dict)]
    if not out["schedules"] and not out["schedulers_read"]:
        out["note"] = ("this machine's schedulers were not read (an older bastion, a platform it "
                       "cannot read, or no report yet): an empty list means nothing looked")
    return out


# ── tool_server_configs ─────────────────────────────────────────────────────

def tool_problems(e: dict) -> list[str]:
    """One tool server's problems in the bastion console's words
    (client-go uimachine.go toolProblems), without the settings file's path."""
    if e.get("disabled"):
        return []
    p = []
    if e.get("secret_in_plaintext"):
        p.append("a key is written in plain text in its settings file")
    if e.get("package_pinned") == "no" and e.get("package_name"):
        p.append(f"runs {e['package_name']} with no fixed version, so whatever is published "
                 "next is what runs")
    if e.get("running_state") == "running_not_declared":
        p.append("running, but not in any settings file this machine can read")
    return p


# Every field narrow_tool_servers keeps except `path` (where the settings file
# with the key sits) and `pids`.
_TOOL_FIELDS = ("name", "scope", "transport", "locality", "url_host", "package_source",
                "package_name", "package_pinned", "auth_configured", "bind_class",
                "running_state", "env_names", "disabled")


async def tool_server_configs(pool, eid: int, machine: str) -> dict:
    from server.agent_access.redact import redact_command
    b = await resolve(pool, eid, machine)
    s = await _shaped(pool, b)
    out = _head(machine, s)
    if _withheld(s):
        out["clients"] = []
        return out
    servers = s.get("tool_servers")
    out["scan"] = s.get("tool_server_scan")
    if not isinstance(servers, list):
        out["clients"] = []
        out["note"] = ("this machine has not reported its agents' tool servers (an older bastion, "
                       "or no report yet): an empty list means nothing looked")
        return out
    by: dict[str, dict] = {}
    for e in servers:
        if not isinstance(e, dict):
            continue
        client = e.get("client") or "not in any settings file"
        g = by.setdefault(client, {"client": client, "servers": [], "with_problems": 0})
        row = {k: e.get(k) for k in _TOOL_FIELDS}
        row["command"] = redact_command(e.get("command"))
        row["problems"] = tool_problems(e)
        if row["problems"]:
            g["with_problems"] += 1
        g["servers"].append(row)
    for g in by.values():
        g["servers"].sort(key=lambda r: -len(r["problems"]))
    out["clients"] = [by[k] for k in sorted(by)]
    return out


# ── posture ─────────────────────────────────────────────────────────────────

_OPEN_SQL = ("SELECT severity, count(*) AS n FROM oversight_findings "
             "WHERE resolved_at IS NULL AND machine = $1 GROUP BY severity")


async def _open_findings(pool, machine: str) -> Optional[dict]:
    try:
        rows = await pool.fetch(_OPEN_SQL, machine)
    except Exception as exc:  # noqa: BLE001 — unknown, never zero
        log.info("oversight ledger unread for an agent's posture read: %r", exc)
        return None
    out = {s: 0 for s in ("critical", "high", "warn", "notice")}
    for r in rows:
        out[r["severity"]] = int(r["n"])
    return out


async def posture(pool, eid: int, machine: str) -> dict:
    """The machine as the Host's Oversight reads it (routes/oversight.py
    list_machines): host posture, alert and detection states, record holes,
    the enforcement it attests, and the ledger's open findings."""
    from mastercode.runtime.tools import (
        _alerts_counts, _bastion_alerts_state, _bastion_detect_state, _detect_counts)
    b = await resolve(pool, eid, machine)
    s = await _shaped(pool, b)
    out = _head(machine, s)
    out.update({"platform": s.get("platform"), "bastion_version": s.get("bastion_version")})
    if _withheld(s):
        return out
    a_state, a_reason = _bastion_alerts_state(b)
    d_state, d_reason = _bastion_detect_state(b)
    holes = b.get("record_dropped")
    p = b.get("posture")
    out.update({
        "posture": p if isinstance(p, dict) else None,
        "posture_note": (None if isinstance(p, dict) else
                         "this machine never reported its host posture (an older bastion, a "
                         "platform other than Linux, or no report yet): unknown, not fine"),
        "alerts_state": a_state, "alerts_reason": a_reason,
        "alerts": _alerts_counts(b.get("alerts")),
        "detect_state": d_state, "detect_reason": d_reason,
        "detect": _detect_counts(b.get("detect")),
        "record_holes": holes if isinstance(holes, dict) else None,
        "enforcing": s.get("enforcing"),
        "open_findings": await _open_findings(pool, machine),
    })
    return out


# ── baseline_delta ──────────────────────────────────────────────────────────

_DELTA_SQL = ("SELECT key, severity, evidence, first_seen, last_seen "
              "FROM oversight_findings WHERE resolved_at IS NULL AND machine = $1 "
              "AND key LIKE $2 ORDER BY last_seen DESC LIMIT 500")
_DELTA_EVIDENCE = ("fp", "kind", "level", "title", "reason", "exe", "unit", "user",
                   "machine_first_seen")


async def baseline_delta(pool, eid: int, machine: str) -> dict:
    """What runs that the machine's accounting does not hold, as far as this
    Host has it: the delta summary on the machine's last heartbeat (counts per
    level and the accounting's state), and the open findings Oversight filed
    from the machine's own record. The full delta is read on the machine
    (`privatae-bastion baseline diff`); the Host cannot ask the machine for it."""
    import json as _json
    from mastercode.runtime.tools import _bastion_detect_state, _detect_counts
    b = await resolve(pool, eid, machine)
    s = await _shaped(pool, b)
    out = _head(machine, s)
    if _withheld(s):
        return out
    d_state, d_reason = _bastion_detect_state(b)
    out.update({"detect_state": d_state, "detect_reason": d_reason,
                "summary": _detect_counts(b.get("detect"))})
    try:
        # oversight.py keys a row "<machine>|<kind>|<fingerprint>"; the delta's
        # are kind "detect:<class>" (oversight_store.line_to_finding).
        like = machine.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "|detect:%"
        rows = await pool.fetch(_DELTA_SQL, machine, like)
    except Exception as exc:  # noqa: BLE001 — unread, never empty
        log.info("oversight ledger unread for an agent's delta read: %r", exc)
        out["findings"] = None
        out["findings_note"] = "the Host's Oversight ledger could not be read right now"
        return out
    findings = []
    for r in rows:
        ev = r["evidence"]
        if isinstance(ev, str):
            try:
                ev = _json.loads(ev)
            except ValueError:
                ev = {}
        ev = ev if isinstance(ev, dict) else {}
        f = {k: ev.get(k) for k in _DELTA_EVIDENCE}
        f.update({"severity": r["severity"],
                  "host_first_seen": r["first_seen"].isoformat() if r["first_seen"] else None,
                  "host_last_seen": r["last_seen"].isoformat() if r["last_seen"] else None})
        findings.append(f)
    out["findings"] = findings
    out["findings_note"] = (
        "open findings in this Host's Oversight ledger, filed from the machine's own record of "
        "its delta. A finding stays open until someone resolves it, so one may describe a "
        "program that has since stopped; the summary is the machine's last pass. contain "
        "with finding=<fp> stops one by its fingerprint.")
    return out
