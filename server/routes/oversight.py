"""Oversight ledger routes.

The read surface over `oversight_findings`, and what the Oversight app
(system-apps/oversight) is built on: the findings, one finding, the resolve
verb, and `/machines` — every watched machine's present-tense state beside what
the ledger holds open against it. The inbox notification does not link here: it
carries `/?open=oversight&view=findings`, which the deep-link contract
(server/deep_link.py, read by dashboard/base/js/deep-link.js) turns into the
app opening on its findings.

READ-ONLY EXCEPT ONE VERB. There is no create and no edit: every row is folded
deterministically from what the machines reported (brain/mastercode/runtime/
oversight.py), so a route that let an app write one would be a second, unaudited
source for the same table. The single mutation is `resolve`, which is a person
saying "I have dealt with this" — the one fact the fold can never derive.

SCOPES. `read:oversight` and `write:oversight`, both new, and NEITHER is in
build_contract.COMMUNITY_ALLOWED_SCOPES. The ledger names a customer's machines,
their firewall posture and what is firing on them; that is the most sensitive
read on this host after the vault, and an untrusted community app must not be
able to request it at all. Being absent from that allowlist is what makes the
refusal structural rather than a review habit.

RESOLVING IS NOT DELETING. `resolve` stamps `resolved_at` and nothing else. The
row, its `first_seen` and its evidence stay, because "this was wrong for six
days in August" is the history the ledger exists to hold, and a finding that
recurs REOPENS the same row (oversight.merge_findings) rather than starting a
fresh one with a lying first_seen.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from server import brain_service
from server.applications.middleware import requires_permission

logger = logging.getLogger("privatae.pc.oversight")

router = APIRouter(prefix="/api/oversight", tags=["oversight"])

# The ledger's vocabulary, mirrored from runtime/oversight.SEVERITIES. Stated
# here rather than imported so a route cannot pull brain/ into the request path;
# scripts/test_oversight_ledger.py pins the two together, which is what keeps a
# mirror from being a copy that drifts.
SEVERITIES = ("notice", "warn", "high", "critical")
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000

_SELECT = """
SELECT id, key, severity, source, machine, first_seen, last_seen, resolved_at,
       evidence, recommendation, pr_url
  FROM oversight_findings
"""


def kind_of(key: str, machine: str) -> str:
    """The finding's KIND, read back out of its key.

    The ledger key is `<machine>|<kind>|<fingerprint>` (runtime/oversight
    .finding_key) and the table has no kind column, so the kind is recovered by
    stripping the row's own machine name off the front — never by splitting on
    the first `|`, which would break for any machine name that contains one.
    The kind is what decides what a RESOLVE does to a row (see the Oversight
    app's resolveNote): a store row of kind `detect:<x>` comes from the box's
    journal and stays resolved until the machine records a change, while the
    other store kinds are rebuilt on every store read and reopen.
    scripts/test_oversight_app.py round-trips this against finding_key.
    """
    key, machine = str(key or ""), str(machine or "")
    rest = key[len(machine) + 1:] if key.startswith(machine + "|") else key
    return rest.split("|", 1)[0]


def _row(r) -> dict:
    ev = r["evidence"]
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except Exception:
            ev = None
    return {
        "id": r["id"],
        "key": r["key"],
        "kind": kind_of(r["key"], r["machine"]),
        "severity": r["severity"],
        "source": r["source"],
        "machine": r["machine"],
        "first_seen": r["first_seen"].isoformat() if r["first_seen"] else None,
        "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
        "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
        "evidence": ev,
        "recommendation": r["recommendation"],
        "pr_url": r["pr_url"],
    }


def _pool():
    pool = brain_service.get_pool()          # brain_service.py:149
    if pool is None:
        raise HTTPException(status_code=503, detail="pod database not ready")
    return pool


@router.get("/findings", dependencies=[Depends(requires_permission("read:oversight"))])
async def list_findings(
    severity: Optional[str] = Query(None, description="notice|warn|high|critical"),
    resolved: Optional[bool] = Query(None, description="omit for both"),
    machine: Optional[str] = Query(None),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
):
    """Findings, worst first, then most recently seen.

    `resolved` is TRI-STATE and omitting it returns both. A default of
    open-only would be the friendlier API and the wrong one: the caller that
    forgets the parameter is the caller who will report "nothing is wrong"
    without knowing they asked a narrower question than they think.
    """
    if severity is not None and severity not in SEVERITIES:
        raise HTTPException(status_code=400,
                            detail=f"severity must be one of {list(SEVERITIES)}")
    where, args = [], []
    if severity is not None:
        args.append(severity)
        where.append(f"severity = ${len(args)}")
    if resolved is True:
        where.append("resolved_at IS NOT NULL")
    elif resolved is False:
        where.append("resolved_at IS NULL")
    if machine:
        args.append(machine)
        where.append(f"machine = ${len(args)}")
    args.append(limit)
    sql = (_SELECT
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY array_position(ARRAY['notice','warn','high','critical']::text[],"
             " severity) DESC, last_seen DESC"
           + f" LIMIT ${len(args)}")
    try:
        rows = await _pool().fetch(sql, *args)
    except Exception as e:
        logger.info("oversight findings unavailable: %s", e)
        # 503, never an empty list. An empty list from a missing table is the
        # ledger reporting an all-clear it never computed, which is the exact
        # failure the whole lane is shaped against.
        raise HTTPException(status_code=503,
                            detail=f"oversight ledger unavailable: {e}") from e
    return {"findings": [_row(r) for r in rows], "count": len(rows)}


@router.get("/findings/{finding_id}",
            dependencies=[Depends(requires_permission("read:oversight"))])
async def get_finding(finding_id: int):
    try:
        row = await _pool().fetchrow(_SELECT + " WHERE id = $1", finding_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503,
                            detail=f"oversight ledger unavailable: {e}") from e
    if row is None:
        raise HTTPException(status_code=404, detail="no such finding")
    return _row(row)


@router.post("/findings/{finding_id}/resolve",
             dependencies=[Depends(requires_permission("write:oversight"))])
async def resolve_finding(finding_id: int):
    """Mark one finding dealt with. Idempotent: resolving a resolved finding
    returns the row unchanged rather than moving its resolved_at, so a double
    click cannot rewrite when the work was actually done."""
    try:
        row = await _pool().fetchrow(
            """
            UPDATE oversight_findings
               SET resolved_at = COALESCE(resolved_at, now())
             WHERE id = $1
            RETURNING id, key, severity, source, machine, first_seen, last_seen,
                      resolved_at, evidence, recommendation, pr_url
            """,
            finding_id,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503,
                            detail=f"oversight ledger unavailable: {e}") from e
    if row is None:
        raise HTTPException(status_code=404, detail="no such finding")
    return {"ok": True, "finding": _row(row)}


# ── the machines, as the ledger sees them (§2f) ─────────────────────────────

_OPEN_BY_MACHINE_SQL = """
SELECT machine, severity, count(*) AS n
  FROM oversight_findings
 WHERE resolved_at IS NULL
 GROUP BY machine, severity
"""

# The standing op that writes the ledger, and how its last day went. Bounded to
# 24 hours on purpose: the op fires every five minutes, so an all-time count
# grows by ~100k rows a year and answers a question nobody asks — "is the ledger
# being written right now" is the question, and the last run plus the last day's
# failures answer it.
_INGEST_SQL = """
SELECT e.status, e.start_time, e.recurrence,
       r.ran_at AS last_ran_at, r.ok AS last_ok, r.error AS last_error,
       (SELECT count(*) FROM calendar_event_runs c
         WHERE c.event_id = e.id AND c.entity_id = e.entity_id
           AND c.ran_at > now() - INTERVAL '24 hours') AS runs_24h,
       (SELECT count(*) FROM calendar_event_runs c
         WHERE c.event_id = e.id AND c.entity_id = e.entity_id
           AND c.ran_at > now() - INTERVAL '24 hours'
           AND c.ok IS FALSE) AS failures_24h
  FROM calendar_events e
  LEFT JOIN LATERAL (
       SELECT ran_at, ok, error FROM calendar_event_runs c
        WHERE c.event_id = e.id AND c.entity_id = e.entity_id
        ORDER BY ran_at DESC LIMIT 1
  ) r ON TRUE
 WHERE e.entity_id = $1
   AND e.event_type = 'scheduled_task'
   AND e.payload->'op'->>'tool' = $2
 ORDER BY (e.status IN ('scheduled', 'running')) DESC, e.start_time DESC NULLS LAST
 LIMIT 1
"""


def _iso(v):
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else v


def _open_counts(rows) -> dict:
    """machine -> {severity: n} over the OPEN rows, every severity present as a
    key so a reader can add them up without guarding for absence."""
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        sev = r["severity"]
        if sev not in SEVERITIES:
            continue
        per = out.setdefault(r["machine"], {s: 0 for s in SEVERITIES})
        per[sev] += int(r["n"] or 0)
    return out


def _worst_open(counts: dict) -> int:
    """Index into SEVERITIES of the worst open finding, -1 for none."""
    for i in range(len(SEVERITIES) - 1, -1, -1):
        if int((counts or {}).get(SEVERITIES[i]) or 0) > 0:
            return i
    return -1


def _record_state(holes) -> str:
    # The beat's three values, kept three (v163): {} whole, a map of shelves with
    # holes, absent/None not reported. "not reported" is never rendered as whole.
    if isinstance(holes, dict):
        return "holes" if holes else "whole"
    return "not reported"


# The names the ledger files a finding under that are NOT a machine:
#   "this host"   the pod itself (oversight_store._host_ca_finding — no CA to
#                 check accountings against is a fact about this host)
#   "store:<id>"  a machine record whose store row names no server
#                 (oversight_store, `st["server"] or f"store:{...}"`)
# Everything else under `other` is a machine name no paired bastion carries —
# a revoked one, a renamed box. The pane labels the three differently, because
# "machines no longer paired" is false for the first two.
HOST_MACHINE_NAME = "this host"
STORE_NAME_PREFIX = "store:"


def _other_kind(name: str) -> str:
    if name == HOST_MACHINE_NAME:
        return "host"
    if name.startswith(STORE_NAME_PREFIX):
        return "store"
    return "unpaired"


@router.get("/machines", dependencies=[Depends(requires_permission("read:oversight"))])
async def list_machines(
    machine: Optional[str] = Query(None, description="one machine by the name the ledger files it under"),
):
    """Every machine the host watches, with what it reports RIGHT NOW beside what
    the ledger holds open against it — the ops app's machines pane (§2f).

    THE STATES ARE THE HOST'S OWN DERIVATIONS, imported rather than mirrored.
    `alerts_state` and `detect_state` come from runtime/tools.py, the same two
    functions get_network uses, because the pane and the host must never
    disagree about the same machine: a pane that re-derived "stale" in its own
    code would be a second opinion that drifts. That is the one reason this
    route reaches into brain/, lazily, the way server/routes/projects.py does.

    WHAT IT NEVER SAYS. An empty `machines` list is only returned when the host
    genuinely has no paired machine. list_bastions returns [] on a read failure
    too (topology.py) — with the SAME token filter the count below uses — so an
    empty list is checked against the token table: a count above zero beside
    an empty list can only mean the machine read failed, and that is a 503, as
    is a token table that cannot be read. "You have no machines" and "the
    machine list could not be read" are different sentences.

    SUMMARY FIRST. `ingest`, `count`, `open_total` and `other` come before
    `machines`: the host reads this through kernel_op, which cuts a result at
    20,000 characters (kernel_ops.run_kernel_op), and a fleet of twenty-odd
    machines passes that. What survives the cut is then the summary and the
    WORST machines (the list is sorted worst first). `machine=` narrows the
    answer to one machine for a host that needs the rest.
    """
    pool = _pool()
    from server.network import topology
    from mastercode.runtime.tools import (
        _alerts_counts, _bastion_alerts_state, _bastion_detect_state, _detect_counts)
    from mastercode.runtime.oversight import OVERSIGHT_TOOL, machine_of

    eid = int(brain_service._entity_id)          # brain_service.py:27
    bastions = await topology.list_bastions(pool, eid)
    if not bastions:
        try:
            paired = await pool.fetchval(
                "SELECT count(*) FROM mcp_client_tokens WHERE entity_id = $1 "
                "AND operator_kind = 'bastion' AND revoked_at IS NULL", eid)
        except Exception as e:
            raise HTTPException(status_code=503,
                                detail=f"machine list unavailable: {e}") from e
        if int(paired or 0) > 0:
            raise HTTPException(
                status_code=503,
                detail=f"machine list unavailable: {int(paired)} paired machine(s) "
                       "on file, and the list of them could not be read")
    try:
        open_rows = await pool.fetch(_OPEN_BY_MACHINE_SQL)
    except Exception as e:
        # Same rule as list_findings: no open counts is never zero open counts.
        raise HTTPException(status_code=503,
                            detail=f"oversight ledger unavailable: {e}") from e
    open_by = _open_counts(open_rows)

    # ONE COUNT PER NAME. Findings are filed by machine NAME, and two bastions
    # can report the same hostname (two boxes both called `raspberrypi`), so a
    # per-card count is the NAME's count and adding the cards up double-counts
    # it. `open_total` is summed over the ledger's distinct names instead, and
    # every card that shares its name is marked, so the pane can say so.
    names = [machine_of(b) for b in bastions]
    shared = {n for n in names if names.count(n) > 1}

    machines, seen = [], set()
    for b, m in zip(bastions, names):
        seen.add(m)
        if machine is not None and m != machine:
            continue
        a_state, a_reason = _bastion_alerts_state(b)
        d_state, d_reason = _bastion_detect_state(b)
        holes = b.get("record_dropped")
        posture = b.get("posture")
        machines.append({
            "machine": m,
            "token_id": b.get("id"),
            "hostname": b.get("hostname"),
            "shares_name": m in shared,
            "online": bool(b.get("online")),
            "last_seen_at": b.get("last_seen_at"),
            "bastion_version": b.get("bastion_version"),
            "platform": b.get("platform"),
            "alerts_state": a_state,
            "alerts_reason": a_reason,
            "alerts": _alerts_counts(b.get("alerts")),
            "detect_state": d_state,
            "detect_reason": d_reason,
            "detect": _detect_counts(b.get("detect")),
            "posture": posture if isinstance(posture, dict) else None,
            "record": _record_state(holes),
            "record_holes": holes if isinstance(holes, dict) and holes else None,
            "open": open_by.get(m) or {s: 0 for s in SEVERITIES},
        })
    machines.sort(key=lambda x: (-_worst_open(x["open"]), x["machine"].lower()))

    # Findings filed under a name no paired machine carries: the host itself,
    # a store that names no server, or a machine no longer paired (_other_kind).
    # Listed, never dropped: their rows are still open.
    other = [{"machine": m, "kind": _other_kind(m), "open": c}
             for m, c in sorted(open_by.items())
             if m not in seen and _worst_open(c) >= 0
             and (machine is None or m == machine)]

    open_total = {s: 0 for s in SEVERITIES}
    for m, c in open_by.items():
        if machine is None or m == machine:
            for s in SEVERITIES:
                open_total[s] += int(c.get(s) or 0)

    ingest = None
    try:
        r = await pool.fetchrow(_INGEST_SQL, eid, OVERSIGHT_TOOL)
    except Exception as e:  # noqa: BLE001 — the pane shows "unknown", not a 503
        logger.info("oversight ingest status unavailable: %s", e)
        r, ingest = None, {"status": "unknown", "reason": str(e)[:200]}
    if r is not None:
        ingest = {
            "status": r["status"],
            "recurrence": r["recurrence"],
            "next_fire_at": _iso(r["start_time"]),
            "last_ran_at": _iso(r["last_ran_at"]),
            "last_ok": r["last_ok"],
            "last_error": (r["last_error"] or "")[:300] or None,
            "runs_24h": int(r["runs_24h"] or 0),
            "failures_24h": int(r["failures_24h"] or 0),
        }
    # Key ORDER is the contract here (see SUMMARY FIRST above): dicts keep
    # insertion order through json.dumps, which is what kernel_op measures.
    return {"ingest": ingest, "count": len(machines), "open_total": open_total,
            "other": other, "machines": machines}
