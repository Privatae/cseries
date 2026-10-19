"""Ships routes: the pipelines, their runs, the machines and the keys.

What the Ships app (system-apps/ships) and the Host read, and the two verbs a
run has: start and stop. Status comes from records only — the runner's rows
(v194) and the receipts the machines signed — never from a model.

START asks, it does not ship. POST /runs builds one plan for the whole ship
and files it on the person's approval device; nothing reaches a machine
until they approve it there (with their code when a step is root work), and
each machine checks the plan itself. So the Host may start a run (kernel_op
exposes /api/ships): what it can do is ring the person, which is what a Host
starting a ship should do.

STOP only narrows: no further line is sent. It is the Stop button in the app
(kernel_op exposes exact paths only, so the Host does not reach a run's
templated stop route).

CLEARING A MACHINE'S PINNED KEY is the person's alone (walled in
kernel_ops): the next key seen is trusted on first use, so clearing is a
trust decision about which machine answers.

SCOPES. read:ships and write:ships, neither in
build_contract.COMMUNITY_ALLOWED_SCOPES: the runs name the person's machines,
what runs on them and the credentials each step takes.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from server import brain_service
from server.applications.middleware import requires_permission

logger = logging.getLogger("privatae.pc.ships")

router = APIRouter(prefix="/api/ships", tags=["ships"])


def _pool():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="pod database not ready")
    return pool


def _eid() -> int:
    return int(brain_service._entity_id)


def _who(request: Request) -> str:
    tok = getattr(request.state, "app_token", None)
    app = getattr(tok, "app_id", "") if tok is not None else ""
    return "your Host" if app == "host" else ("you, in the Ships app" if app == "ships" else (app or ""))


def _err(e):
    from server.ships.runner import RunnerError
    if isinstance(e, RunnerError):
        return HTTPException(status_code=e.status, detail=e.detail)
    logger.info("ships: %r", e)
    return HTTPException(status_code=503, detail=f"ships unavailable: {e}")


@router.get("/pipelines", dependencies=[Depends(requires_permission("read:ships"))])
async def list_pipelines():
    """Every pipeline file beside the ops (ships/<name>/pipeline.yaml in a
    module with target op), parsed, with its latest run. A file that does not
    parse is listed with its error."""
    from server.ships import pipeline as pl
    pool = _pool()
    try:
        found = await pl.find_all(pool)
        latest = {r["name"]: dict(r) for r in await pool.fetch(
            "SELECT DISTINCT ON (p.name) p.name, r.id, r.state, r.commit_sha, r.first_seen "
            "  FROM ship_runs r JOIN ship_pipelines p ON p.id = r.pipeline_id "
            " ORDER BY p.name, r.id DESC")}
    except Exception as e:
        raise _err(e) from e
    out = []
    for f in found:
        item = {k: f.get(k) for k in ("name", "project_id", "module", "path", "error")}
        spec = f.get("spec")
        if spec:
            item.update({"repo": spec["repo"], "branch": spec["branch"], "trigger": spec["trigger"],
                         "mode": spec["mode"], "turn_type": spec.get("turn_type") or "",
                         "runbook": spec.get("runbook") or {},
                         "steps": [{"step": s["step"], "machine": s["machine"], "op": s["op"],
                                    "credentials": s["credentials"], "undo": s["undo"], "on_fail": s["on_fail"]}
                                   for s in spec["steps"]]})
        lr = latest.get(f["name"])
        item["latest_run"] = ({"id": lr["id"], "state": lr["state"], "commit": lr["commit_sha"],
                               "started": lr["first_seen"].isoformat()} if lr else None)
        out.append(item)
    return {"pipelines": out, "count": len(out)}


@router.get("/runs", dependencies=[Depends(requires_permission("read:ships"))])
async def list_runs(limit: int = Query(20, ge=1, le=100), pipeline: Optional[str] = Query(None),
                    before: Optional[int] = Query(None, ge=1)):
    """Recent runs, newest first, each with every step and its receipt.
    `before` (a run id; the previous answer's next_before) pages older runs."""
    from server.ships import runner
    pool = _pool()
    try:
        if pipeline:
            if before is not None:
                rows = await pool.fetch(
                    "SELECT r.id FROM ship_runs r JOIN ship_pipelines p ON p.id = r.pipeline_id "
                    " WHERE p.name = $1 AND r.id < $3 ORDER BY r.id DESC LIMIT $2",
                    pipeline, limit, before)
            else:
                rows = await pool.fetch(
                    "SELECT r.id FROM ship_runs r JOIN ship_pipelines p ON p.id = r.pipeline_id "
                    " WHERE p.name = $1 ORDER BY r.id DESC LIMIT $2", pipeline, limit)
            runs = [await runner.run_view(pool, r["id"]) for r in rows]
        else:
            runs = await runner.recent_runs(pool, limit, before=before)
    except Exception as e:
        raise _err(e) from e
    ids = [r.get("id") for r in runs if isinstance(r, dict) and isinstance(r.get("id"), int)]
    return {"runs": runs, "count": len(runs),
            "next_before": (min(ids) if ids and len(runs) == limit else None)}


@router.get("/runs/{run_id}", dependencies=[Depends(requires_permission("read:ships"))])
async def get_run(run_id: int):
    from server.ships import runner
    try:
        return await runner.run_view(_pool(), run_id)
    except Exception as e:
        raise _err(e) from e


class StartReq(BaseModel):
    pipeline: str
    commit: str


@router.post("/runs", dependencies=[Depends(requires_permission("write:ships"))])
async def start_run(req: StartReq, request: Request):
    """Start a ship: one plan for the whole ship, filed on the person's
    approval device. Refused, naming every reason at once, when a machine
    cannot run it (no grant, a bastion without action ops, no device pinned)."""
    from server.ships import runner
    try:
        return await runner.start_run(_pool(), _eid(), req.pipeline.strip(), req.commit.strip(),
                                      trigger="manual", by=_who(request))
    except Exception as e:
        raise _err(e) from e


@router.post("/runs/{run_id}/stop", dependencies=[Depends(requires_permission("write:ships"))])
async def stop_run(run_id: int):
    from server.ships import runner
    try:
        return await runner.stop_run(_pool(), run_id)
    except Exception as e:
        raise _err(e) from e


@router.get("/machines", dependencies=[Depends(requires_permission("read:ships"))])
async def list_machines():
    """Every machine a pipeline names: its bastion and version, whether it runs
    action ops, the approval devices pinned there, the grant each ship op
    needs, and the encryption key pinned for it."""
    from server.ships import pipeline as pl
    from server.ships import runner
    pool = _pool()
    try:
        hosts = []
        for f in await pl.find_all(pool):
            for s in (f.get("spec") or {}).get("steps") or []:
                if s["machine"] not in hosts:
                    hosts.append(s["machine"])
        machines = [await runner.machine_state(pool, _eid(), h) for h in hosts]
    except Exception as e:
        raise _err(e) from e
    return {"machines": machines, "count": len(machines)}


@router.get("/keys", dependencies=[Depends(requires_permission("read:ships"))])
async def list_keys():
    """The credentials each pipeline's ops declare, by name, and whether this
    Host's vault holds each. Never a value."""
    from server.ships import pipeline as pl
    from server.ships import runner
    pool = _pool()
    out = {}
    try:
        for f in await pl.find_all(pool):
            for s in (f.get("spec") or {}).get("steps") or []:
                try:
                    op = await runner.load_action_op(pool, s["op"])
                except runner.RunnerError as e:
                    out.setdefault(f"(op {s['op']})", {"name": None, "held": None, "used_by": [], "error": e.detail})
                    continue
                for name in op["manifest"].get("credentials") or []:
                    k = out.setdefault(name, {"name": name, "held": None, "used_by": []})
                    k["used_by"].append(f"{f['name']}:{s['step']}")
        held = set()
        if out:
            # A deactivated credential reads as absent to every consumer
            # (vault.api.open_secret), so it is not held for a ship either;
            # nor is an MCP server's sign-in, which only its server is handed.
            from server.vault import api as vault_api
            held = {s["logical_name"] for s in await vault_api.list_secrets(pool, entity_id=_eid())
                    if not s.get("deactivated_at") and not vault_api.is_sign_in_name(s["logical_name"])}
    except Exception as e:
        raise _err(e) from e
    for k in out.values():
        if k.get("name"):
            k["held"] = k["name"] in held
    return {"keys": list(out.values()), "count": len(out)}


@router.delete("/machines/{token_id}/pin", dependencies=[Depends(requires_permission("write:ships"))])
async def clear_pin(token_id: int):
    """Forget a machine's pinned encryption key; the next one it reports is
    pinned on first use. The person's act (walled for the Host in kernel_ops):
    use it when the machine was rebuilt and really has a new key."""
    pool = _pool()
    try:
        n = await pool.execute("DELETE FROM ship_machine_keys WHERE token_id = $1", token_id)
    except Exception as e:
        raise _err(e) from e
    return {"cleared": n.endswith(" 1"), "token_id": token_id}
