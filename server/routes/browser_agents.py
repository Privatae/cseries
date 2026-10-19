"""Browser agents — HTTP API for the Browser app and for any app.

    GET    /api/browser/agents                 list              read:agents
    GET    /api/browser/agents/{name}          the agent file    read:agents
    PUT    /api/browser/agents/{name}          save (+ schedule) write:agents
    DELETE /api/browser/agents/{name}          delete            write:agents
    POST   /api/browser/agents/{name}/run      run now           execute:agents
    GET    /api/browser/agents/{name}/runs     recent runs       read:agents
    POST   /api/browser/agents/{name}/resume   answer a pause    execute:agents
    GET    /api/browser/agents/{name}/runs/{run_id}/screenshot  read:agents

The Browser app records through the sidecar (/system-apps/browser/v1/record/*)
and seals the recorded sign-in entries through the existing
/api/browser/forms/save; this router only ever sees the STEPS. An app that
wants to trigger an agent declares execute:agents and posts to /run — it
needs no vault scope, because the agent's credentials are the vault's
business, not the caller's.

The logic is in server/browser_agents.py; these are the HTTP edges.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from server import brain_service
from server import browser_agents as agents
from server.applications.middleware import requires_permission
from server.operator_identity import request_email

logger = logging.getLogger("privatae.pc.browser_agents")

router = APIRouter(prefix="/api/browser/agents", tags=["browser-agents"])


def _actor(request: Request, action: str) -> str:
    email = request_email(request.headers) or "unknown"
    return f"user:{email} {action}"


def _entity_id() -> int:
    eid = os.environ.get("ENTITY_ID", "")
    if not eid.isdigit():
        raise HTTPException(503, "ENTITY_ID not set")
    return int(eid)


def _pool():
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(503, "brain_not_initialized")
    return pool


@router.get("", dependencies=[Depends(requires_permission("read:agents"))])
async def list_all() -> dict:
    return {"agents": agents.list_agents(), "running": agents.active_runs()}


@router.get("/{name}", dependencies=[Depends(requires_permission("read:agents"))])
async def get_one(name: str) -> dict:
    spec = agents.load_agent(name)
    if spec is None:
        raise HTTPException(404, f"no agent named {name!r}")
    return spec


class AgentBody(BaseModel):
    site: str
    steps: list[dict]
    allow: list[str] = Field(default_factory=list)
    description: Optional[str] = None
    schedule: Optional[dict] = None


@router.put("/{name}", dependencies=[Depends(requires_permission("write:agents"))])
async def save(name: str, body: AgentBody, request: Request) -> dict:
    spec: dict[str, Any] = {"name": name, "site": body.site, "steps": body.steps,
                            "allow": body.allow}
    if body.description:
        spec["description"] = body.description
    if body.schedule:
        spec["schedule"] = body.schedule
    try:
        return await agents.save_agent(_pool(), entity_id=_entity_id(), spec=spec,
                                       actor=_actor(request, "agent-save"))
    except agents.AgentError as e:
        raise HTTPException(400, str(e))


@router.delete("/{name}", dependencies=[Depends(requires_permission("write:agents"))])
async def delete(name: str) -> dict:
    try:
        gone = await agents.delete_agent(_pool(), entity_id=_entity_id(), name=name)
    except agents.AgentError as e:
        raise HTTPException(409, str(e))
    if not gone:
        raise HTTPException(404, f"no agent named {name!r}")
    return {"deleted": name}


class RunBody(BaseModel):
    wait_s: float = Field(default=120.0, ge=0, le=900,
                          description="how long to wait for the run before answering "
                                      "with its current state")


@router.post("/{name}/run", dependencies=[Depends(requires_permission("execute:agents"))])
async def run_now(name: str, request: Request, body: Optional[RunBody] = None) -> dict:
    # The run carries on in the background whatever the wait (agents.run), and
    # the answer is the run's state at the end of it. A request passes the
    # router in front of the Host, which gives it 30 s
    # (server/applications/long_request.py), so the wait stays inside that;
    # the Browser window's own 5 s list poll shows the rest of the run. The
    # Host's own tool calls agents.run directly and are not bound by this.
    from server.applications import long_request
    wait_s = min(body.wait_s if body else long_request.BUDGET_S, long_request.BUDGET_S)
    try:
        return await agents.run(_pool(), entity_id=_entity_id(), name=name,
                                actor=_actor(request, "agent-run"), trigger="api",
                                wait_s=wait_s)
    except agents.AgentError as e:
        raise HTTPException(400, str(e))


@router.get("/{name}/runs", dependencies=[Depends(requires_permission("read:agents"))])
async def runs(name: str, limit: int = 20) -> dict:
    return {"runs": agents.list_runs(name, limit=max(1, min(limit, 50)))}


class ResumeBody(BaseModel):
    answer: str


@router.post("/{name}/resume", dependencies=[Depends(requires_permission("execute:agents"))])
async def resume(name: str, body: ResumeBody) -> dict:
    ok, msg = agents.resume(name, body.answer)
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "message": msg}


@router.get("/{name}/runs/{run_id}/screenshot",
            dependencies=[Depends(requires_permission("read:agents"))])
async def screenshot(name: str, run_id: str):
    """The masked screenshot a failed run took. Password inputs were masked
    by the sidecar before the bytes ever left it."""
    try:
        n = agents._validate_name(name)
    except agents.AgentError as e:
        raise HTTPException(400, str(e))
    if not run_id.replace("-", "").replace("T", "").replace("Z", "").isalnum():
        raise HTTPException(400, "bad run id")
    p = (agents._runs_dir(n) / f"{run_id}.png").resolve()
    if agents._runs_dir(n).resolve() not in p.parents or not p.is_file():
        raise HTTPException(404, "no screenshot for that run")
    return FileResponse(str(p), media_type="image/png")
