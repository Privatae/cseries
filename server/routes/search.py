"""
search route — the one index. POST /api/search with {q, limit, stores}.

A single index surface for finding context, files and the rest.
Before this, a person or an app had five routes to search five stores
(/api/brain/context/search, /api/brain/memory, /api/brain/journal/search,
/api/projects/search, /api/codesearch/search), each behind its own scope, and
none of them was the search the HOST runs before a turn. This route wraps
memory_retrieval.unified_search: the host's own federated recall (memory +
the user's documents, the exact engine and filters of the per-turn pre-fire)
plus the project index, the in-pod code index and the Host's
skills (its own and the platform's) — each store under its own key, honest
about availability.

The words travel in a JSON body, never in a URL. A search
box searches as the person types, and a URL is kept by whatever sits in front
of the pod (a proxy's access log); a body is not (CWE-598). The client's and
the server's cuts find a secret only once it is whole, so a password or a key
typed a character at a time reached the URL a prefix at a time. The GET form,
with its `q` in the query string, is gone; the Workbench's two searches made
the same move in 4092bed46 (server/routes/projects.py, SearchProjects).

One scope, read:search, so an app declares one thing. No per-model visibility
filter (the caller is the owner; see scopes.py).
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from server import brain_service, secret_cut
from server.applications.middleware import requires_permission

router = APIRouter(prefix="/api/search", tags=["search"])

# JSON can carry half of a surrogate pair ("\ud800"), which a query string
# never could. Pydantic refuses one in a str (a 422) and the UTF-8 every store's
# outgoing call needs cannot encode it, so it becomes U+FFFD before anything
# reads the words and such a search is answered like any other. The same rule
# as server/routes/projects.py _well_formed.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


class UnifiedSearch(BaseModel):
    """POST /api/search: what to search for, how many per store, and which
    stores. `stores` empty = every store; an unknown name is a 400, never a
    silent search of everything."""
    q: str = Field(..., min_length=1, max_length=2000)
    limit: int = Field(10, ge=1, le=50)
    stores: list[str] = Field(
        default_factory=list,
        description="A subset of memory, documents, projects, code, skills, world. Empty = every store.",
    )

    @field_validator("q", mode="before")
    @classmethod
    def _well_formed_q(cls, q):
        return _LONE_SURROGATE.sub("�", q) if isinstance(q, str) else q


@router.post("", dependencies=[Depends(requires_permission("read:search"))])
async def unified_search(body: UnifiedSearch) -> dict:
    """ONE search across everything the host can see. Response: {query, stores,
    limit, memory: {hits, arms, suppressed, ...}, documents: {results, arms,
    suppressed}, projects: {results, semantic}, code:
    {results, projects, semantic}, skills: {results, searched},
    unavailable: [store, ...]}. A store that
    could not run carries {unavailable: reason} and is named in `unavailable`;
    a store that ran and matched nothing is an empty list. An unknown name in
    `stores` is a 400, never a silent search of everything.

    The typed query is cut of its secret shapes first (server/secret_cut.py):
    the memory, documents, projects and code stores embed it on the Host's
    embedding endpoint (and code search reranks with it), any of which can be
    a keyed outside provider."""
    q = secret_cut.search_words(body.q, door="search")
    limit = body.limit
    from mastercode.memory_retrieval import SEARCH_STORES, UnknownSearchStore, unified_search as _search
    wanted = [s.strip() for s in body.stores if s.strip()] or None
    unknown = [s for s in (wanted or []) if s not in SEARCH_STORES]
    if unknown:  # the same 400 whether or not the brain is up
        raise HTTPException(400, f"unknown store(s) {unknown}; stores are {', '.join(SEARCH_STORES)}")
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized", "query": q, "limit": limit,
                "stores": [s for s in SEARCH_STORES if not wanted or s in wanted],
                "unavailable": []}
    try:
        return await _search(pool, brain_service._entity_id, q, stores=wanted, limit=limit)
    except UnknownSearchStore as e:  # belt and braces: the service refuses too
        raise HTTPException(400, str(e))
