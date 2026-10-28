"""
searcher — hybrid retrieval over one project, or over every project and repo
in the index at once: vector (pgvector) + lexical
(tsvector), Reciprocal-Rank-Fusion fused, then cross-encoder reranked on the entity's
configured lane. Ported from the platform code index's search flow.

Note the rerank arm hands bridge_embed the full CONTENT of every candidate, so
it is the most sensitive call in this module. bridge_embed refuses it on a
BYO/bastion host; the `scores is not None` guard below is what makes that refusal
a graceful degradation (RRF order) instead of a failure.
"""
from __future__ import annotations

import os

from . import store, bridge_embed

RRF_K = int(os.environ.get("CODESEARCH_RRF_K", "60"))
RERANK_CANDIDATES = int(os.environ.get("CODESEARCH_RERANK_CANDIDATES", "30"))


def rrf_fuse(vector_hits: list[dict], lexical_hits: list[dict], k: int = RRF_K) -> list[dict]:
    """Reciprocal Rank Fusion. Each arm contributes 1/(k+rank); a chunk found by
    both arms sums its contributions. Keyed by (project, file_path,
    chunk_index): across projects two repos can hold the same path."""
    scores: dict[tuple, float] = {}
    rows: dict[tuple, dict] = {}
    for arm in (vector_hits, lexical_hits):
        for rank, hit in enumerate(arm):
            key = (hit.get("project"), hit["file_path"], hit["chunk_index"])
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            rows.setdefault(key, hit)
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    return [dict(rows[key], rrf_score=sc) for key, sc in ordered]


def collapse_copies(hits: list[dict]) -> list[dict]:
    """One hit per place in the code, naming every project it is in. A fork
    and its upstream hold the same file at the same path; over everything,
    each arm found both copies and the answer held the same text twice, once
    per project (measured on a Host indexing a repo and its own fork of
    it, near-identical, 33,000 chunks each). A copy is the same
    path, the same lines and the same text, so the place it names can be
    read in every project listed. Two hits of ONE project are never merged. Order kept:
    the first copy, the best ranked, is the hit; `projects` lists its own
    project first."""
    out: list[dict] = []
    seen: dict[tuple, dict] = {}
    for h in hits:
        key = (h["file_path"], h.get("start_line"), h.get("end_line"), h["content"])
        first = seen.get(key)
        if first is not None and h.get("project") not in first["projects"]:
            first["projects"].append(h.get("project"))
            continue
        h = dict(h, projects=[h.get("project")])
        seen.setdefault(key, h)
        out.append(h)
    return out


async def search(db, *, project: str | None, query: str, limit: int = 10,
                 meta: dict | None = None) -> list[dict]:
    """Hybrid search within one project (project None: across all of them —
    each hit names its `project`): query → embed + tsvector → RRF →
    cross-encoder rerank → top-`limit`, both remote arms on the entity's
    CONFIGURED lane. Degrades gracefully — if the embed fails we run
    lexical-only; if rerank fails OR is refused because a BYO/bastion lane is
    configured (bridge_embed.rerank returns None), we keep the RRF order.

    The vector arm uses the Host's CONFIGURED embedding model: the query is
    embedded with it and compared in the column of its size
    (bridge_embed.active_space). With no model set up it does not run, and
    the search is keyword-only — a setup step, never an outage.

    `meta`, when given, is filled with how the search ran, so a caller can say
    so instead of passing a keyword-only answer off as a semantic one:
    semantic (the query was embedded and the vector arm ran), reranked
    (the cross-encoder ordered the hits), — when there was no model to
    embed with — embeddings, the sentence saying why, and — when the vector
    arm ran — without_vectors / chunks / embedding_model: how many of the
    chunks searched it could not see (store.vector_coverage).

    Every hit carries `projects`: its own project, then every other project
    holding the same file at the same place (collapse_copies)."""
    space, no_space = await bridge_embed.active_space()
    qvec = await bridge_embed.embed(query, space=space) if space is not None else None
    if meta is not None:
        meta.update(semantic=bool(qvec), reranked=False)
        if space is None:
            meta["embeddings"] = no_space
    # Over everything a fork and its upstream fill each arm with pairs that
    # collapse into one hit (collapse_copies), so each arm is asked for twice
    # the reranker's count: after the merge it still gets RERANK_CANDIDATES
    # distinct places. Measured on a 131,940-chunk index: 60 rows
    # instead of 30 cost the vector arm nothing past noise (5-16 ms warm) and
    # the lexical arm at most ~150 ms (it ranks every match either way). One
    # project holds no copies; its arms stay at the reranker's count.
    per_arm = RERANK_CANDIDATES * (2 if project is None else 1)
    vector_hits = (await store.search_vector(db, project=project, query_embedding=qvec,
                                             space=space, limit=per_arm)
                   if qvec else [])
    if meta is not None and qvec:
        # How much of what was searched the vector arm saw: a chunk with no
        # vector in the configured model's column (indexed before this model
        # was set up or switched to, or while no embedder answered) is found
        # by its words only. Counted only when the caller asks how the search
        # ran, so it can say so instead of calling the answer one by meaning.
        missing, total = await store.vector_coverage(db, project=project, space=space)
        meta.update(without_vectors=missing, chunks=total, embedding_model=space.model)
    lexical_hits = await store.search_lexical(db, project=project, query=query, limit=per_arm)

    # Copies collapse before the cut, so the reranker sees distinct candidates.
    fused = collapse_copies(rrf_fuse(vector_hits, lexical_hits))[:RERANK_CANDIDATES]
    if not fused:
        return []

    scores = await bridge_embed.rerank(query, [h["content"] for h in fused])
    if meta is not None:
        meta["reranked"] = scores is not None
    if scores is not None:
        for h, s in zip(fused, scores):
            h["rerank_score"] = s
        # Reranked docs sort by score (desc); any unscored sink below.
        fused.sort(key=lambda h: -(h["rerank_score"] if h.get("rerank_score") is not None else -1.0))
    return fused[:limit]


def how_it_ran(meta: dict, results: list) -> dict:
    """What a caller says about how search() ran, from the `meta` it filled:
    `degraded` when it matched words only, `partly_by_meaning` when the
    vector arm could not see some chunks, `reranked: False` when hits came
    back in fusion order. Empty for a full search. search_my_code and recall
    both say it, in these words, so neither passes a keyword-only answer off
    as a semantic one."""
    out: dict = {}
    # With no embedding model set up the search is keyword-only and says
    # that: a setup step, never an outage (meta.embeddings).
    if meta.get("embeddings"):
        out["degraded"] = (f"keyword only: {meta['embeddings']}. These hits match the "
                           "query's words, not its meaning")
    elif meta.get("semantic") is False:
        out["degraded"] = ("keyword only: your embedding lane did not answer, so these "
                           "hits match the query's words, not its meaning")
    elif meta.get("without_vectors"):
        # The vector arm ran, but over part of the index: chunks indexed
        # before this model was set up or switched to (a switch keeps the
        # old model's vectors in their own column), or while no embedder
        # answered, have no vector of this model yet.
        out["partly_by_meaning"] = (
            f"{meta['without_vectors']} of the {meta.get('chunks')} chunks searched have no "
            f"vector from {meta.get('embedding_model')} yet, so the search by meaning did "
            "not see them; they match by their words only. A repo gets them on its next "
            "sync pass; a Workbench project when index_my_code runs on it")
    if results and meta.get("reranked") is False:
        out["reranked"] = False
    return out
