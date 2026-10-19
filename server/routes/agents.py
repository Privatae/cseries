"""
Agents routes — the dispatched-agent surface.

Dispatched agents = in-container scoped agents that are the host's HANDS: the host
dispatches a scoped agent that runs in the background, and the user is GOVERNOR —
they watch it run, see output/cost/status, and VETO it. The Agents app, the
Workbench live view and Circles read GET /api/agents and post the cancel.

Under the hood every call delegates to the runtime-singleton AgentDispatch
(server.brain_service._runtime.agent_dispatch) so the in-flight map that makes
veto work is shared with the host turn:

  - GET  /api/agents                  → list the entity's dispatched agents
  - POST /api/agents/{task_id}/cancel → the veto

There is NO route that starts an agent. Only the host's own turn dispatches
(runtime.agent_dispatch, called in-process). POST /api/agents/dispatch existed
"for verification" from 2026-06-28 to 2026-10-01 with no caller and no
permission check, so any app on the Host — an origin-isolated community app
included, since its /api calls land on this pod — could start an agent with the
tool scope and capability grant of its choosing, acting as the Host. It was
removed rather than gated: a scope for it would be a scope to act as the Host.

Auth: every route here requires an app-token scope (scripts/
test_api_route_guards.py fails the build on any /api route that has neither a
scope nor an entry in server/applications/route_policy.py). Dispatched agents
are entity_tasks rows (read:brain_tasks / write:brain_tasks); scheduled work is
calendar_events rows (read:calendar / write:calendar), the same scopes the
calendar routes take. The CEIGAS and localhost-only middleware are not an app
boundary: every app's /api call arrives through the same router they admit.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from server import brain_service
from server.applications.middleware import requires_permission

log = logging.getLogger("privatae.pc.agents")

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _entity_id() -> int | None:
    # Same mechanism circles.py uses (server/routes/circles.py:106-110).
    try:
        return brain_service._entity_id
    except Exception:
        return None


def _dispatch():
    """The runtime-singleton AgentDispatch, or raise 503 if the runtime (and so
    its in-flight veto map) isn't up yet."""
    runtime = brain_service.get_runtime()
    if runtime is None or getattr(runtime, "agent_dispatch", None) is None:
        raise HTTPException(status_code=503, detail="Agent dispatch not ready")
    return runtime.agent_dispatch


# ── The scheduled lanes (2026-08-30) ────────────────────────────────────────
#
# Two more kinds of work belong on this surface, and they are the pair that
# makes the app worth opening:
#
#   lane 'op'   — a STANDING OP. payload {"op": {...}} fires one deterministic
#                 registry op. No model, no turn, and — the point — NO
#                 inference_cost_log row at all.
#   lane 'turn' — a SCHEDULED TURN. payload {"prompt"/"steps"} runs the full
#                 governed pipeline on a clock. A model runs on every fire.
#
# Same scheduler, same clock, often the same job. Showing them side by side is
# the argument: the user sees which of their recurring work actually needs a
# model, which is usually less of it than they assumed.
#
# COST HONESTY. An op is never rendered "$0". inference_cost_log.est_cost_usd is
# NOT NULL DEFAULT 0.0 and that is exactly why cost_method exists (v113): an
# UNPRICED call and a free one are the same number, and 256 rows of real spend
# on one Host rendered as $0, and a zero that is not a real zero is not
# shown. So an op reports cost_state='none' — no model ran — which is a
# stronger claim than a dollar sign and cannot be confused with a zero that
# means "we don't know".
#
# NOT ON THIS SURFACE: the pod's own background workers (the reapers,
# reconcilers and sweeps dispatched at server/main.py's lifespan). They are the
# host's plumbing, not the user's agents — 23 always-on rows would bury the
# handful of real tasks this app is for. The line this app draws is work the
# USER or the HOST started.


def _plain_error(err: str | None) -> str | None:
    """A run's error as the USER should read it.

    The offline runner marks a ceiling-skip row with a machine sentinel so two
    queries can key on it (offline_task_runner.CEILING_SKIP_PREFIX). That marker
    is not English and has no business in a panel, so it comes off here — the
    same way the fired report never renders it. Any other error passes through
    untouched.
    """
    if not err:
        return err
    from mastercode.brain.offline_task_runner import CEILING_SKIP_PREFIX
    return err[len(CEILING_SKIP_PREFIX):].lstrip() \
        if err.startswith(CEILING_SKIP_PREFIX) else err


def _lane_of(payload) -> tuple[str, str | None]:
    """('op', tool) for a standing op, ('turn', None) otherwise.

    The payload column is JSONB and asyncpg may hand it back as a string, so
    both shapes are read — same as the offline runner and schedule_task.
    """
    p = payload or {}
    if isinstance(p, str):
        import json as _json
        try:
            p = _json.loads(p)
        except Exception:  # noqa: BLE001
            return "turn", None
    spec = p.get("op") if isinstance(p, dict) else None
    if isinstance(spec, dict) and str(spec.get("tool") or "").strip():
        return "op", str(spec["tool"]).strip()
    return "turn", None


@router.get("/scheduled", dependencies=[Depends(requires_permission("read:calendar"))])
async def list_scheduled(limit: int = 100) -> dict:
    """The host's standing work — both lanes, newest fire first, each with its
    last run so 'is this actually working?' is answerable here.

    Read-only. The off switch is the sibling cancel route: because the host
    creates standing work WITHOUT asking the user (there is no approval step
    for deterministic, already-in-scope work), the user's control surface is
    seeing it and being able to stop it in one action.
    """
    eid = _entity_id()
    pool = brain_service.get_pool()
    if eid is None or pool is None:
        return {"scheduled": []}

    rows = await pool.fetch(
        """
        SELECT e.event_id, e.id AS pk, e.title, e.start_time, e.recurrence,
               e.status, e.payload, e.related_project_id,
               r.ran_at AS last_ran_at, r.ok AS last_ok, r.error AS last_error,
               (SELECT count(*) FROM calendar_event_runs c
                 WHERE c.event_id = e.id AND c.entity_id = e.entity_id) AS runs,
               -- A ceiling SKIP is not a failure. The offline runner writes a
               -- run row when a task is at its daily model-turn ceiling
               -- (offline_task_runner.CEILING_SKIP_PREFIX) so the history shows
               -- why nothing happened; those rows carry ok=false, and counting
               -- them here made a perfectly healthy task at its governor read
               -- as a task failing every 15 minutes. Counted separately instead,
               -- so the panel can say "skipped" and mean it.
               (SELECT count(*) FROM calendar_event_runs c
                 WHERE c.event_id = e.id AND c.entity_id = e.entity_id
                   AND c.ok IS FALSE
                   AND (c.error IS NULL
                        OR c.error NOT LIKE 'ScheduledTurnCeiling:%')) AS failures,
               (SELECT count(*) FROM calendar_event_runs c
                 WHERE c.event_id = e.id AND c.entity_id = e.entity_id
                   AND c.error LIKE 'ScheduledTurnCeiling:%') AS skipped
          FROM calendar_events e
          LEFT JOIN LATERAL (
               SELECT ran_at, ok, error FROM calendar_event_runs c
                WHERE c.event_id = e.id AND c.entity_id = e.entity_id
                ORDER BY ran_at DESC LIMIT 1
          ) r ON TRUE
         WHERE e.entity_id = $1
           AND e.event_type = 'scheduled_task'
           AND e.status IN ('scheduled', 'running')
         ORDER BY e.start_time ASC
         LIMIT $2
        """,
        int(eid), max(1, min(int(limit or 100), 500)),
    )

    out = []
    for r in rows:
        lane, tool = _lane_of(r["payload"])
        out.append({
            "event_id": str(r["event_id"]),
            "title": r["title"],
            "lane": lane,
            "op_tool": tool,
            "recurrence": r["recurrence"],
            "status": r["status"],
            # The thread this timer was set in, and the one it reports back
            # into (2026-09-04). The Workbench needs it to show the user the
            # pending check on the project they are actually looking at.
            "project_id": r["related_project_id"],
            "next_fire_at": r["start_time"].isoformat() if r["start_time"] else None,
            "runs": int(r["runs"] or 0),
            "failures": int(r["failures"] or 0),
            # Runs the daily model-turn ceiling stopped. Broken out from
            # `failures` so a governed task never reads as a broken one.
            "skipped": int(r["skipped"] or 0),
            "last_ran_at": r["last_ran_at"].isoformat() if r["last_ran_at"] else None,
            "last_ok": r["last_ok"],
            # The ceiling sentinel is a machine marker, never prose the user
            # should read — strip it the same way the fired report does.
            "last_error": _plain_error(r["last_error"]),
            # 'none' = no model ran, so no inference row exists. NEVER a dollar
            # amount: see the cost-honesty note above.
            "cost_state": "none" if lane == "op" else "model",
        })
    return {"scheduled": out}


@router.post("/scheduled/{event_id}/cancel",
             dependencies=[Depends(requires_permission("write:calendar"))])
async def cancel_scheduled(event_id: str) -> dict:
    """The off switch — one action, entity-scoped, idempotent-ish.

    The counterpart of not asking. A standing op is created on the host's own
    judgement, so the user must be able to end it without a conversation and
    without understanding what it does.
    """
    eid = _entity_id()
    pool = brain_service.get_pool()
    if eid is None or pool is None:
        raise HTTPException(status_code=503, detail="Brain not ready")
    try:
        res = await pool.execute(
            "UPDATE calendar_events SET status='cancelled', updated_at=NOW() "
            "WHERE event_id=$1::uuid AND entity_id=$2 "
            "  AND event_type='scheduled_task' AND status IN ('scheduled','running')",
            event_id, int(eid),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("cancel_scheduled failed: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))
    if str(res).endswith(" 0"):
        raise HTTPException(status_code=404, detail="no active scheduled task with that id")
    return {"cancelled": event_id}


@router.get("", dependencies=[Depends(requires_permission("read:brain_tasks"))])
async def list_agents(active_only: bool = False, limit: int = 20) -> dict:
    """List this entity's dispatched agents (newest first) for the Workbench live view:
    each carries status / output / tokens / est_cost_usd / failure.

    `limit` is caller-set (bounded 1..200): the fixed newest-20 window silently
    dropped older still-running agents from the bench AND from the chat card's
    status poll — a busy session pushed a live agent off the list and its card
    froze on "Running" forever (2026-07-20 audit)."""
    eid = _entity_id()
    if eid is None:
        return {"agents": []}
    agents = await _dispatch().list_agents(
        eid, active_only=active_only, limit=max(1, min(int(limit or 20), 200)))
    return {"agents": agents}


@router.get("/{task_id}/stream", dependencies=[Depends(requires_permission("read:brain_tasks"))])
async def stream_agent_events(task_id: int, from_seq: int = 0):
    """LIVE transcript tail for one dispatched agent — what the Agents app reads.

    Same contract and same transport decision as the harness run stream
    (review.py stream_session_harness_events):
    NDJSON over fetch/ReadableStream rather than SSE, because an EventSource
    cannot carry the app token and an agent transcript is the same sensitivity
    class as a harness one.

    progress_cb fires from inside _execute_one, so every dispatched agent
    writes the same frames to the same file (the deleted Claude-subscription
    agent loop used to share this path).
    """
    import asyncio, json
    from pathlib import Path
    from fastapi.responses import StreamingResponse

    path = Path("/var/lib/cseries/agent-runs") / ("%d.events.jsonl" % int(task_id))

    async def gen():
        sent = 0
        waited = 0.0
        while waited < 1800:
            if path.exists():
                with open(path, "r", encoding="utf-8") as fh:
                    for i, line in enumerate(fh):
                        if i < max(sent, from_seq):
                            continue
                        line = line.strip()
                        if not line:
                            continue
                        sent = i + 1
                        yield (line + "\n").encode()
            await asyncio.sleep(0.5)
            waited += 0.5
            # Stop tailing once the agent's record is terminal — the file stops
            # growing and an open stream would otherwise idle for 30 minutes.
            try:
                from server import brain_service
                pool = brain_service.get_pool()
                if pool is not None:
                    row = await pool.fetchrow(
                        "SELECT status FROM entity_tasks WHERE task_id=$1", int(task_id))
                    if row and str(row["status"]) not in ("queued", "in_progress", "running"):
                        return
            except Exception:
                pass

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store"})


@router.post("/{task_id}/cancel", dependencies=[Depends(requires_permission("write:brain_tasks"))])
async def cancel(task_id: int) -> dict:
    """User veto: cancel a running/queued dispatched agent. Idempotent."""
    eid = _entity_id()
    if eid is None:
        raise HTTPException(status_code=503, detail="Brain not ready")
    return await _dispatch().cancel(eid, task_id)
