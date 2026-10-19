"""
Artifacts router — HTTP surface for the ARTIFACT primitive.

An ARTIFACT is DISTINCT from an app and from a project: a disposable,
lightweight LIVE build the host writes into a project-style workspace but that
is NOT bound to the App SDK until the user explicitly converts it. See
server/applications/artifacts.py for the lifecycle + the design forks.

  POST   /api/artifacts                 create an artifact (+ optional files)
  GET    /api/artifacts                 list this host's artifacts
  POST   /api/artifacts/{id}/files      write/overwrite files in the workspace
  POST   /api/artifacts/{id}/promote    copy a root file over the entry (variant pick) — no model
  POST   /api/artifacts/{id}/import     copy files in from another project workspace — no model
  GET    /api/artifacts/{id}/render     the live build (self-contained HTML)
  GET    /api/artifacts/{id}/history            change history, newest first
  GET    /api/artifacts/{id}/history/{rev}/render  render one past revision
  POST   /api/artifacts/{id}/revert     restore a past revision (itself recorded)
  POST   /api/artifacts/{id}/convert    convert artifact -> app via the App SDK
  DELETE /api/artifacts/{id}            dispose of an artifact

All routes are entity-scoped to brain_service._entity_id — a host only ever
touches its OWN artifacts (the service refuses any row that isn't this entity's
kind='artifact').
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

from server import brain_service
from server.applications import artifacts as artifact_service
from server.applications.artifacts import ArtifactError
from server.applications.middleware import requires_permission

log = logging.getLogger("privatae.artifacts.routes")

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


def _entity_id() -> int:
    return int(brain_service._entity_id)


def _pool():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain pool down")
    return pool


async def _wearing_desktop(html: str) -> str:
    """The artifact as the user sees it: in the current desktop's palette and
    type (server/applications/artifact_style.py). Never fails a render — a
    sheet that cannot be read shows the artifact as written."""
    try:
        from server.applications.artifact_style import wear_desktop
        from server.routes.appearance import token_sheet
        return wear_desktop(html, await token_sheet())
    except Exception as e:  # noqa: BLE001 — the artifact must still show
        log.debug("artifact style unavailable: %s", e)
        return html


@router.post("", dependencies=[Depends(requires_permission("write:projects"))])
async def create_artifact(request: Request) -> dict:
    """Create a disposable artifact. Body: {name, files?: [{path, content}],
    entry?, description?}."""
    pool = _pool()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    try:
        return await artifact_service.create_artifact(
            pool, _entity_id(),
            name=body.get("name", ""),
            files=body.get("files"),
            entry=body.get("entry", "index.html"),
            description=body.get("description", ""),
        )
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None


@router.get("", dependencies=[Depends(requires_permission("read:projects"))])
async def list_artifacts(limit: int = Query(50, ge=1, le=200),
                         offset: int = Query(0, ge=0)) -> dict:
    """One page of artifacts with the true total and next_offset (None on the
    last page): every artifact is reachable, however many there are."""
    return await artifact_service.list_artifacts(_pool(), _entity_id(),
                                                  limit=limit, offset=offset)


@router.post("/{artifact_id}/files", dependencies=[Depends(requires_permission("write:projects"))])
async def write_files(artifact_id: int, request: Request) -> dict:
    """Body: {files: [{path, content}]}."""
    pool = _pool()
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    try:
        return await artifact_service.write_files(
            pool, _entity_id(), artifact_id, body.get("files") or [])
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None


@router.get("/{artifact_id}/render", dependencies=[Depends(requires_permission("read:projects"))])
async def render_artifact(artifact_id: int, entry: str = Query("index.html")):
    """The live build — a self-contained renderable HTML document for the
    artifact's workspace (asset-inlined, worktree-jailed). Served as an
    iframe srcdoc source by the host UI."""
    try:
        html = await artifact_service.render(_pool(), _entity_id(), artifact_id, entry)
    except ArtifactError as e:
        return _render_refusal(e)
    return HTMLResponse(await _wearing_desktop(html))


def _render_refusal(e: ArtifactError):
    """A render that did not produce a document. Several pages and none to
    open by default is 300 with the pages — the preview routes' shape
    ({detail, entries, basis}) — so the Artifacts window can offer them
    instead of reporting a failed render; anything else is the error."""
    if e.status == 300 and e.entries:
        return JSONResponse(status_code=300, content={
            "detail": e.detail, "entries": e.entries, "basis": e.basis})
    raise HTTPException(e.status, e.detail) from None


@router.post("/{artifact_id}/promote", dependencies=[Depends(requires_permission("write:projects"))])
async def promote_entry(artifact_id: int, request: Request) -> dict:
    """The variant pick as an OPERATION: body {from, to?='index.html'}. Copies
    the file server-side as one revision — the Artifacts window's "Use this
    one" calls this directly, so a pick costs no model turn (an
    agent re-typing a 37 KB file to copy it is waste). If the
    artifact is the live desktop, the shell re-renders on its own poll."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    try:
        return await artifact_service.promote_entry(
            _pool(), _entity_id(), artifact_id,
            str(body.get("from") or ""), str(body.get("to") or "index.html"),
            actor="user")
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None


@router.post("/{artifact_id}/import", dependencies=[Depends(requires_permission("write:projects"))])
async def import_files(artifact_id: int, request: Request) -> dict:
    """Copy files from another of this host's project workspaces into the
    artifact: body {project_id, files: [{from, to?}]}. Server-side, one
    revision, no model."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    try:
        return await artifact_service.import_files(
            _pool(), _entity_id(), artifact_id,
            body.get("project_id"), body.get("files") or [], actor="user")
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None


@router.get("/{artifact_id}/files", dependencies=[Depends(requires_permission("read:projects"))])
async def list_entry_files(artifact_id: int) -> dict:
    """Root *.html files — the variant tabs the Artifacts window shows."""
    try:
        return await artifact_service.list_entry_files(_pool(), _entity_id(), artifact_id)
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None


@router.post("/{artifact_id}/bridge", dependencies=[Depends(requires_permission("write:projects"))])
async def bridge_edit(artifact_id: int, request: Request) -> dict:
    """Tune-mode edit lane: body
    {op:'edit', entry?, before, after}. Server-side re-validation lives in
    the service — plain text only, exactly-once before-match, rate-capped."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    if (body.get("op") or "edit") != "edit":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "op must be 'edit'")
    try:
        return await artifact_service.apply_bridge_edit(
            _pool(), _entity_id(), artifact_id,
            entry=body.get("entry") or "index.html",
            before=body.get("before"),
            after=body.get("after"),
        )
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None


@router.get("/{artifact_id}/history", dependencies=[Depends(requires_permission("read:projects"))])
async def artifact_history(artifact_id: int,
                           limit: Optional[int] = Query(None, ge=1),
                           offset: int = Query(0, ge=0)) -> dict:
    """Change history, newest first — every create/write/tune-edit/revert is a
    revision (rev, ts, actor, op, note). Every revision is kept, so it pages:
    `limit` from `offset` (no limit = all), with total and next_offset."""
    try:
        return await artifact_service.list_history(_pool(), _entity_id(), artifact_id,
                                                   limit=limit, offset=offset)
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None


@router.get("/{artifact_id}/history/{rev}/render",
            dependencies=[Depends(requires_permission("read:projects"))])
async def render_artifact_revision(artifact_id: int, rev: str,
                                   entry: str = Query("index.html")):
    """One PAST revision as a self-contained document — same sandboxed-srcdoc
    lane as the live render, jailed to the revision snapshot."""
    try:
        html = await artifact_service.render_history(
            _pool(), _entity_id(), artifact_id, rev, entry)
    except ArtifactError as e:
        return _render_refusal(e)
    return HTMLResponse(await _wearing_desktop(html))


@router.post("/{artifact_id}/revert", dependencies=[Depends(requires_permission("write:projects"))])
async def revert_artifact(artifact_id: int, request: Request) -> dict:
    """Restore a past revision. Body: {rev}. The restore becomes the newest
    revision (a revert is always revertable) and the window live-refreshes."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body must be JSON") from None
    try:
        return await artifact_service.revert_artifact(
            _pool(), _entity_id(), artifact_id, str(body.get("rev") or ""))
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None


@router.post("/{artifact_id}/convert", dependencies=[Depends(requires_permission("execute:install"))])
async def convert_artifact(artifact_id: int) -> dict:
    """Convert the artifact into an installed app via the App SDK. Requires a
    provenance=custom app.yaml already written into the workspace."""
    try:
        return await artifact_service.convert_to_app(_pool(), _entity_id(), artifact_id)
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None


@router.delete("/{artifact_id}", dependencies=[Depends(requires_permission("write:projects"))])
async def delete_artifact(artifact_id: int) -> dict:
    try:
        return await artifact_service.delete_artifact(_pool(), _entity_id(), artifact_id)
    except ArtifactError as e:
        raise HTTPException(e.status, e.detail) from None
