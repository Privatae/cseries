"""
codesearch route — per-user hybrid code search over the entity's named projects.

Backend for the Codespace Search tab and the `search_my_code` MCP tool. The engine
(brain/codesearch) keeps its index in the Host's own Postgres (pgvector +
tsvector, code_chunks); the index and the user's code never leave the pod. Embed AND rerank run on the entity's
CONFIGURED lane (bridge_embed): a BYO/bastion backend when one is set — in which
case rerank is skipped entirely rather than sent to platform compute, since its
payload is raw chunk content — and the platform bridge only when the entity has
configured no backend of its own.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Query
from server import secret_cut
from server.applications.middleware import requires_permission

from codesearch import indexer, searcher, store

logger = logging.getLogger("privatae.codesearch")
router = APIRouter(prefix="/api/codesearch", tags=["codesearch"])

_index_tasks: dict[str, asyncio.Task] = {}


def _get_db():
    """The pod's Postgres pool (store.connect)."""
    return store.connect()


@router.get("/projects", dependencies=[Depends(requires_permission("read:codesearch"))])
async def list_indexed_projects() -> dict:
    """The entity's named projects + per-project index coverage."""
    db = _get_db()
    indexed = {s["project"]: s for s in await store.stats(db)}
    out = []
    for p in indexer.list_projects():
        s = indexed.get(p) or {}
        out.append({
            "project": p, "files": s.get("files", 0), "chunks": s.get("chunks", 0),
            "indexing": p in _index_tasks and not _index_tasks[p].done(),
        })
    return {"projects": out}


@router.get("/search", dependencies=[Depends(requires_permission("read:codesearch"))])
async def search(
    q: str = Query(..., min_length=1),
    project: str | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
) -> dict:
    """Hybrid (vector + BM25) search with cross-encoder rerank over one
    project, or over every project and readable repo when none is named.

    The typed query is cut of its secret shapes first (server/secret_cut.py):
    the searcher embeds it and reranks with it on the entity's lane, which can
    be the platform bridge or a keyed outside provider. A query that was only
    secrets answers empty, as a blank one does, without a call."""
    project = (project or "").strip() or None
    q = secret_cut.search_words(q, door="code search", log=logger)
    if not q.strip():
        return {"project": project, "query": q, "count": 0, "results": []}
    db = _get_db()
    results = await searcher.search(db, project=project, query=q, limit=limit)
    return {"project": project, "query": q, "count": len(results), "results": results}


@router.post("/index", dependencies=[Depends(requires_permission("write:codesearch"))])
async def reindex(project: str = Query(..., min_length=1)) -> dict:
    """Kick off an incremental (re)index of one project in the background."""
    if project in _index_tasks and not _index_tasks[project].done():
        return {"project": project, "status": "already running"}
    db = _get_db()

    async def _run() -> None:
        try:
            logger.info("codesearch index: %s", await indexer.index_project(db, project=project))
        except Exception as e:
            logger.warning("codesearch index failed for %s: %s", project, e)

    _index_tasks[project] = asyncio.create_task(_run())
    return {"project": project, "status": "indexing started"}
