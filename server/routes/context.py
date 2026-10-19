"""
User Context API Routes — External intake layer for the memory architecture.

User context is the raw material the entity can draw from but hasn't
internalized. The USER owns this, not the entity. The entity processes
user context and decides what's worth promoting to entity memory.

This separation prevents external tools from polluting the entity's identity.

Endpoints:
  GET  /api/brain/context         — list user context (paginated, filtered)
  POST /api/brain/context/import  — bulk import external context
  POST /api/brain/context/embed   — embed all unembedded user context
  POST /api/brain/context/{id}/promote — promote to entity memory
  DELETE /api/brain/context/{id}  — dismiss / delete
  GET  /api/brain/context/stats   — counts by source and status
  POST /api/brain/context/search  — semantic search within user context
"""

import asyncio
import base64
import json
import logging
import mimetypes
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from fastapi import APIRouter, Header, HTTPException, Query, Request, Depends
from fastapi.responses import Response
from server.applications.middleware import requires_permission

from server import brain_service, secret_cut

logger = logging.getLogger("privatae.brain.context")

router = APIRouter(prefix="/api/brain/context", tags=["brain", "context"])

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
# NO model constant: embed jobs resolve the entity's CONFIGURED model
# (behavioral_params.embedding_model) inside embed.enqueue_job — a literal here
# would embed context with a model the entity's endpoint may not serve.

# Part 4: pointer left in user_context.content when the row's bytes live on a
# non-pg backend (s3 / home bastion). The metadata + embedding stay in pg; the
# raw content is rehydrated from the backend on read. Mirrors the entity_context
# marker in brain/mastercode/context_store.py.
#
# NO NUL BYTE. This was "\x00__user_context_external__\x00" on the reasoning that
# a \x00-delimited string never collides with real content. True, and irrelevant:
# user_context.content is TEXT (migrations/v090_user_context_create.sql:33) and
# PostgreSQL rejects \x00 in a text value outright — asyncpg raises
# CharacterNotInRepertoireError, "invalid byte sequence for encoding UTF8: 0x00".
# So every write of this marker failed, and the external-storage feature has
# never once worked. Reproduced against PG 14.19 through asyncpg before changing
# it, rather than reasoning about it.
#
# U+0000 is the only character PostgreSQL forbids in TEXT. U+241E (␞, SYMBOL FOR
# RECORD SEPARATOR) is an ordinary printable character that stores fine, and no
# real document begins and ends with it.
#
# NOTHING TO MIGRATE: no row has ever carried the old value, because no write of
# it could commit. The reader at :224 compares against this constant, so it moves
# with it.
_UC_EXTERNAL_MARKER = "␞__user_context_external__␞"

# The ONE "is this row embedded at any dim?" SQL fragment. Every embedded?
# surface (list, stats, the Context app's index + coverage) must use this —
# per-column ad-hoc checks have twice shipped blind spots (the 1024-only
# has_embedding; 2560 missing from the four-column checks everywhere).
# Keep in lockstep with memory_engine.EMBEDDING_COLUMN_BY_DIM.
ANY_EMBEDDED_SQL = (
    "(embedding IS NOT NULL OR embedding_768 IS NOT NULL "
    "OR embedding_2048 IS NOT NULL OR embedding_2560 IS NOT NULL "
    "OR embedding_4096 IS NOT NULL)"
)

# Rows that are not language, and therefore are not the TEXT lane's business.
# Every embed selector/denominator and every unembedded count must exclude them
# with this ONE fragment, or they sit in "unembedded" forever and embed jobs try
# to vectorize something that is not a sentence.
#
#   'file'  — binary file-lane rows (pictures/PDFs/anything not text). They hold
#             base64 bytes: STORED, never text-embedded.
#   'frame' — a video frame (server/routes/context_extract.py). It holds
#             NO bytes at all: the media vector and the timestamp ARE the row,
#             because the clip is already stored and any frame reproduces by
#             seeking to its offset (measured 416 kB against 4.97 MB per clip,
#             12x). A frame row with an empty content column would otherwise be
#             "unembedded text" for ever and the unembedded lens could never
#             clear again — the exact failure this fragment exists to prevent,
#             which is why 'frame' goes HERE rather than into each call site.
#
# It stays ONE fragment on purpose. The media counts go the other way and count
# only their own kinds, so no row is ever in both and neither total can borrow
# the other's denominator.
NOT_FILE_KIND_SQL = "COALESCE(metadata->>'kind', '') NOT IN ('file', 'frame')"

# A video frame row's kind. Named once so the selectors, the coverage counts and
# the writer cannot drift on a string literal.
FRAME_KIND = "frame"

# tsvector source text, computed IN SQL from the insert's own params so the
# lexical index always reflects the real bytes (title + content), capped so a
# pathological import can't blow tsvector limits. $title/$content are format
# slots for the caller's positional params.
# Filename findability: the FTS parser treats 'budget.csv' as a
# single file token, so a search for "budget" can never match it — while
# 'spend account.csv' matches "spend" only because of the space. Index the
# title twice: verbatim (exact-token search) AND separator-normalized so the
# filename's words are real lexemes.
_TSV_SQL = ("to_tsvector('english', left(coalesce({title}, '') || ' ' || "
            "regexp_replace(coalesce({title}, ''), '[._\\-/]+', ' ', 'g') || ' ' || "
            "{content}, 200000))")

# ── The table is DECLARED, not auto-created ─────────────────────────
#
# user_context is declared in init-db.sql:1164 and migrations/
# v090_user_context_create.sql:30; its 15 later columns and 13 indexes are
# spread across v049, v071, v078, v084, v095, v096 and v135. The 29-statement
# USER_CONTEXT_SCHEMA that used to sit here, and the ensure_table() that ran
# it at boot and again on a cold first upload, were a mirror of all of that.
#
# WHY THE SELF-HEAL GOES WITH IT, since it was added deliberately: it existed
# for entities whose pgdata predated user_context in init-db.sql. v090 covers
# exactly that case and runs in entrypoint.sh's migration loop under
# ON_ERROR_STOP=1 — a host where it did not apply does not come up at all, so
# there is no reachable state where the table is missing and the process is
# serving requests. The read guards (user_context_exists) stay; they are
# SELECTs, and cheap.


async def user_context_exists(pool) -> bool:
    """True when the lazily-created user_context table exists. Read paths guard
    on this (the same to_regclass check the v049/v071 migrations and embed.py's
    health lens use): a host that never ingested any context must read as an
    EMPTY corpus, never as an UndefinedTableError 500."""
    try:
        return bool(await pool.fetchval(
            "SELECT to_regclass('public.user_context') IS NOT NULL"))
    except Exception:
        return False


# ── Helpers ──────────────────────────────────────────────────────────

async def _generate_embedding(text: str) -> str | None:
    """Generate a content embedding for the entity's active model on its
    CONFIGURED backend — the same config-driven lane entity_memory writes use
    (memory_engine._embed_text_config_driven): BYO/bastion when the host set
    one, the platform bridge only when it didn't; NO cross-lane fallback.
    Pod-local Ollama is removed; the old direct POST to
    127.0.0.1:11434 always failed in k8s pods, silently leaving user_context
    rows unembedded. Returns a pgvector literal string at whatever dim the
    model produced, or None on failure."""
    try:
        from mastercode.memory_engine import (
            _embed_text_config_driven, _entity_embedding_config,
        )
        pool = brain_service.get_pool()
        eid = brain_service._entity_id
        _, model = await _entity_embedding_config(pool, eid)
        if not model:
            # Unconfigured — a setup state, not a failure. Never dial with an
            # empty model (that tripped the bridge circuit and read as an
            # outage on hosts that had not set embeddings up).
            return None
        # The WHOLE text: past one embed window it is
        # embedded window by window and combined (mastercode.embed_windows),
        # never sliced at 8,000 characters. A text inside one window is the
        # same single call as before.
        from mastercode.embed_windows import embed_whole, to_literal

        async def _one(part: str):
            return await _embed_text_config_driven(
                part, model, entity_id=eid, pool=pool)
        return to_literal(await embed_whole(text, _one))
    except Exception as e:
        logger.warning("Context embedding failed: %s", e)
    return None


async def _generate_query_embedding(text: str) -> str | None:
    """Query-mode embedding for semantic search — the entity's active model on
    its configured backend (same lane / dim as _generate_embedding)."""
    try:
        from mastercode.memory_engine import (
            _embed_text_config_driven, _entity_embedding_config,
        )
        pool = brain_service.get_pool()
        eid = brain_service._entity_id
        _, model = await _entity_embedding_config(pool, eid)
        if not model:
            return None  # unconfigured = setup state; see _generate_embedding
        # The whole query, the same way a row is embedded: it was once cut
        # at 2,000 characters, so the end of a long pasted query never took
        # part in the search.
        from mastercode.embed_windows import embed_whole, to_literal

        async def _one(part: str):
            return await _embed_text_config_driven(
                part, model, entity_id=eid, pool=pool)
        return to_literal(await embed_whole(text, _one))
    except Exception as e:
        logger.warning("Context query embedding failed: %s", e)
    return None


# ── Part 4: external-content rehydration ─────────────────────────────

async def _rehydrate_uc_content(pool, entity_id, row: dict) -> None:
    """In-place: if a user_context result row's bytes live on a non-pg backend,
    replace its marker `content` with the real bytes fetched from the backend.
    pg rows (the default) are untouched. An offline/unfetchable holder leaves the
    content as None + flags content_offline, never leaking the internal marker."""
    backend = (row.get("storage_backend") or "pg")
    if backend == "pg":
        return
    # Only rehydrate a row whose content is actually the marker (defensive: a row
    # that was externalized then overwritten inline would have real content).
    if row.get("content") not in (_UC_EXTERNAL_MARKER, None, ""):
        return
    try:
        from brain.mastercode import context_backends
        fetched = await context_backends.retrieve_user_context_content(
            pool, entity_id, row.get("id"), backend)
    except Exception as e:
        logger.info("user_context %s rehydrate failed: %s", row.get("id"), e)
        fetched = None
    if fetched is not None:
        row["content"] = fetched
    else:
        # Graceful offline: don't surface the internal marker.
        row["content"] = None
        row["content_offline"] = True


# ── Routes ───────────────────────────────────────────────────────────

@router.get("", dependencies=[Depends(requires_permission("read:memory"))])
async def list_context(
    source: Optional[str] = None,
    status: str = "raw",
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    fields: str = Query("full", pattern="^(full|meta)$"),
    upload_batch: Optional[str] = Query(None),
    from_file: Optional[str] = Query(None),
):
    """List user context entries, paginated and optionally filtered.
    fields=meta returns metadata only (content_size instead of content; no
    external-byte fetches) — what a browsing UI should call. upload_batch /
    from_file scope the list to ONE file group (the inspector's chunk list)."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    # Thin adapter: the paginate/filter logic lives in the service so the
    # context_ingest tool's list op shares it without faking a Request.
    return await list_context_service(
        pool, brain_service._entity_id,
        source=source, status=status, page=page, per_page=per_page, fields=fields,
        upload_batch=upload_batch, from_file=from_file)


@router.post("/import", dependencies=[Depends(requires_permission("write:memory"))])
async def import_context(request: Request):
    """Bulk import external context.

    Body: {
        "source": "chatgpt",         // required — origin system
        "items": [                    // required — array of context entries
            {
                "content": "...",     // required
                "title": "...",       // optional
                "source_id": "...",   // optional — ID from source system
                "tags": ["tag1"],     // optional
                "metadata": {},       // optional — any extra data
                "created_at": "..."   // optional — ISO timestamp from source
            }
        ],
        "embed": false                // optional — embed immediately (default: queue)
    }
    """
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    body = await request.json()
    # Thin adapter: the stage-into-user_context logic (multi-dim column routing +
    # optional inline embed) lives in the service so the context_ingest tool shares
    # it without faking a Request. Response shape unchanged.
    return await stage_context_service(
        pool, brain_service._entity_id,
        source=body.get("source", "manual"),
        items=body.get("items", []),
        embed_now=body.get("embed", False))


@router.post("/embed", dependencies=[Depends(requires_permission("write:memory"))])
async def embed_context(request: Request):
    """Enqueue a durable embed_job for unembedded user_context rows.

    Body: { "force": false }  — force=true re-embeds everything.
    Returns the job_id; progress is broadcast on the dashboard WS as
    {type: "embed_job", ...} events. Job survives container restarts.
    """
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}

    eid = brain_service._entity_id
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    force = body.get("force", False)

    from server.routes import embed as _embed
    try:
        job = await _embed.enqueue_job(
            entity_id=eid,
            job_type="embed_context",
            target="user_context",
            metadata={"force": bool(force)},
        )
    except ValueError as e:
        return {"error": str(e)}
    if job is None:
        return {"error": "Brain not initialized"}

    if (job.get("total") or 0) == 0:
        return {"queued": 0, "job_id": job["id"], "message": "All context already embedded"}

    return {
        "queued": job["total"],
        "job_id": job["id"],
        "model": job["model"],
        "force": bool(force),
    }


@router.post("/search", dependencies=[Depends(requires_permission("read:memory"))])
async def search_context(request: Request):
    """Semantic search within user context.

    Body: { "query": "...", "limit": 20 }
    """
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    body = await request.json()
    # Thin adapter: the query-embed + active-dim nearest-neighbour search lives in
    # the service so the context_ingest tool's search op shares it.
    # The typed query is cut of its secret shapes first (server/secret_cut.py):
    # the service embeds it on the Host's embedding endpoint and the media
    # model's text tower, either of which can be a keyed outside provider.
    return await search_context_service(
        pool, brain_service._entity_id,
        query=secret_cut.search_words(body.get("query", ""), door="documents search", log=logger),
        limit=body.get("limit", 20))


@router.post("/{ctx_id}/promote", dependencies=[Depends(requires_permission("write:memory"))])
async def promote_context(ctx_id: int, request: Request):
    """Promote a user context entry to entity memory.

    This is the internalization step — the entity decides this external
    context is worth becoming part of its identity.

    Body: {
        "memory_type": "research",    // optional: research | cited_fact | preference | relationship
        "importance": 1.0,            // optional (default 1.0)
    }
    """
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    body = await request.json()
    # Thin adapter: the internalization logic (fetch, multi-dim vector reuse/gen,
    # entity_memory insert, mark promoted) lives in the service so the
    # context_ingest tool's promote op shares it. ContextError maps back to the
    # SAME {error} bodies the old route returned, per-case.
    from server.routes.context import ContextError
    try:
        return await promote_context_service(
            pool, brain_service._entity_id, ctx_id,
            memory_type=body.get("memory_type", "research"),
            importance=body.get("importance", 1.0))
    except ContextError as e:
        if e.status == 404:
            return {"error": "Context entry not found"}
        if e.status == 409:
            ctx = await pool.fetchrow(
                "SELECT promoted_to FROM user_context WHERE id = $1 AND entity_id = $2",
                ctx_id, brain_service._entity_id)
            return {"error": "Already promoted",
                    "promoted_to": ctx["promoted_to"] if ctx else None}
        raise HTTPException(e.status, e.detail)


@router.delete("/{ctx_id}", dependencies=[Depends(requires_permission("write:memory"))])
async def dismiss_context(ctx_id: int):
    """Dismiss a user context entry — marks it as reviewed and not worth keeping."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}

    eid = brain_service._entity_id
    result = await pool.execute(
        "UPDATE user_context SET status = 'dismissed', updated_at = now() WHERE id = $1 AND entity_id = $2",
        ctx_id, eid,
    )

    return {"dismissed": "UPDATE 1" in result, "id": ctx_id}


# ── Explorer: tags/collections + on-demand content ───────────────────
# A "collection" in the Context Explorer IS a tag on user_context rows —
# organizing never moves bytes. These are the only mutable fields; content/
# source/status stay immutable here (dismiss and promote have their own paths).

MAX_TAGS_PER_ROW = 50
MAX_TAG_LEN = 64


def _clean_tags_noted(raw) -> tuple[Optional[list[str]], Optional[str]]:
    """Validate + normalize a tags payload: list of non-empty strings, trimmed,
    length-capped, case-insensitively deduped (first spelling wins), count-capped.
    Returns (None, None) if the payload isn't a list of strings.

    The caps are a SHAPE guard on the tags[] column (a hostile header or body),
    far above a person's collections. What changed:
    a trim is never silent — the note says how many were left out or shortened,
    and an upload and a row follow the same rule (an upload was cut at 24)."""
    if not isinstance(raw, list):
        return None, None
    out: list[str] = []
    seen: set[str] = set()
    shortened = 0
    for t in raw:
        if not isinstance(t, str):
            return None, None
        t = t.strip()
        if len(t) > MAX_TAG_LEN:
            t = t[:MAX_TAG_LEN]
            shortened += 1
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    dropped = max(0, len(out) - MAX_TAGS_PER_ROW)
    out = out[:MAX_TAGS_PER_ROW]
    parts = []
    if dropped:
        parts.append(f"{dropped} collection(s) past the first {MAX_TAGS_PER_ROW} were "
                     f"not applied (an entry holds at most {MAX_TAGS_PER_ROW})")
    if shortened:
        parts.append(f"{shortened} collection name(s) longer than {MAX_TAG_LEN} "
                     f"characters were shortened to {MAX_TAG_LEN}")
    return out, ("; ".join(parts) or None)


def _clean_tags(raw) -> Optional[list[str]]:
    """_clean_tags_noted without the note (callers that report it read that)."""
    return _clean_tags_noted(raw)[0]


@router.get("/tags", dependencies=[Depends(requires_permission("read:memory"))])
async def list_tags():
    """Distinct tags + row counts across the live corpus — the Explorer's
    collections rail. Dismissed rows don't count."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    # user_context is created lazily on first intake — absent on hosts that never
    # ingested context, where an unguarded read raised
    # UndefinedTableError into the request. No context yet ⇒ no tags.
    if not await user_context_exists(pool):
        return {"tags": []}
    rows = await pool.fetch("""
        SELECT t AS tag, COUNT(*) AS count
        FROM user_context, unnest(tags) AS t
        WHERE entity_id = $1 AND status != 'dismissed'
        GROUP BY t ORDER BY count DESC, t
    """, brain_service._entity_id)
    return {"tags": [dict(r) for r in rows]}


@router.post("/tags", dependencies=[Depends(requires_permission("write:memory"))])
async def bulk_tags(request: Request):
    """Bulk add/remove tags — one call tags a whole multi-chunk file.

    Body: {
      "add":    ["tag", ...],        // optional
      "remove": ["tag", ...],        // optional (matched case-insensitively)
      // target — exactly one of:
      "ids": [1, 2, ...],                        // explicit row ids
      "upload_batch": "<uuid>",                  // one upload's chunk group
      "file": {"source": "...", "from_file": "..."}   // legacy group key
    }
    """
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    eid = brain_service._entity_id
    body = await request.json()
    add, tags_note = _clean_tags_noted(body.get("add") or [])
    remove = _clean_tags(body.get("remove") or [])
    if add is None or remove is None:
        return {"error": "'add'/'remove' must be lists of strings"}
    if not add and not remove:
        return {"error": "nothing to do — pass 'add' and/or 'remove'"}

    where = "entity_id = $3 AND status != 'dismissed'"
    params: list = [add, [t.lower() for t in remove], eid]
    ids = body.get("ids")
    batch = (body.get("upload_batch") or "").strip() if isinstance(body.get("upload_batch"), str) else ""
    file_key = body.get("file")
    if isinstance(ids, list) and ids:
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            return {"error": "'ids' must be integers"}
        params.append(ids)
        where += f" AND id = ANY(${len(params)}::int[])"
    elif batch:
        params.append(batch)
        where += f" AND metadata->>'upload_batch' = ${len(params)}"
    elif isinstance(file_key, dict) and file_key.get("from_file"):
        params.append((file_key.get("source") or "").strip())
        where += f" AND source = ${len(params)}"
        params.append(str(file_key["from_file"]))
        where += f" AND metadata->>'from_file' = ${len(params)}"
    else:
        return {"error": "target required — one of 'ids', 'upload_batch', 'file'"}

    # Set semantics in one statement: (tags ∪ add) − remove. Dedupe is
    # CASE-INSENSITIVE with the earliest spelling winning (an existing 'infra'
    # absorbs an added 'Infra' — plain DISTINCT kept both); remove also matches
    # case-insensitively.
    result = await pool.execute(f"""
        UPDATE user_context SET
            tags = (
                SELECT COALESCE(array_agg(t ORDER BY ord), '{{}}')
                FROM (
                    SELECT DISTINCT ON (lower(t)) t, ord
                    FROM unnest(tags || $1::text[]) WITH ORDINALITY AS u(t, ord)
                    WHERE lower(t) <> ALL($2::text[])
                    ORDER BY lower(t), ord
                ) dedup
            ),
            updated_at = now()
        WHERE {where}
    """, *params)
    try:
        updated = int(result.split()[-1])
    except (ValueError, IndexError):
        updated = 0
    out = {"updated": updated, "added": add, "removed": remove}
    if tags_note:
        out["tags_note"] = tags_note
    return out


@router.patch("/{ctx_id}", dependencies=[Depends(requires_permission("write:memory"))])
async def update_context(ctx_id: int, request: Request):
    """Update ONE entry's mutable fields: tags and/or title. Everything else is
    immutable through this route by design."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    eid = brain_service._entity_id
    body = await request.json()

    sets: list[str] = []
    params: list = []
    tags_note = None
    if "tags" in body:
        tags, tags_note = _clean_tags_noted(body.get("tags"))
        if tags is None:
            return {"error": "'tags' must be a list of strings"}
        params.append(tags)
        sets.append(f"tags = ${len(params)}")
    if "title" in body:
        title = body.get("title")
        if title is not None and not isinstance(title, str):
            return {"error": "'title' must be a string or null"}
        params.append(title.strip()[:300] if isinstance(title, str) else None)
        # Title feeds the lexical index — recompute it for pg-resident rows so
        # a rename is searchable (an external row's content column holds only
        # the marker, so its tsv must not be rebuilt from it).
        sets.append(f"title = ${len(params)}")
        sets.append(
            "search_tsv = CASE WHEN COALESCE(storage_backend, 'pg') = 'pg' "
            f"THEN to_tsvector('english', left(coalesce(${len(params)}, '') || ' ' || "
            f"regexp_replace(coalesce(${len(params)}, ''), '[._\\-/]+', ' ', 'g') || ' ' || "
            "content, 200000)) "
            "ELSE search_tsv END")
    if not sets:
        return {"error": "nothing to update — pass 'tags' and/or 'title'"}

    params.extend([ctx_id, eid])
    row = await pool.fetchrow(f"""
        UPDATE user_context SET {', '.join(sets)}, updated_at = now()
        WHERE id = ${len(params) - 1} AND entity_id = ${len(params)}
        RETURNING id, title, tags, source, status, updated_at
    """, *params)
    if not row:
        return {"error": "Context entry not found"}
    d = dict(row)
    d["updated_at"] = d["updated_at"].isoformat() if d["updated_at"] else None
    out = {"updated": d}
    if tags_note:
        out["tags_note"] = tags_note
    return out


@router.get("/{ctx_id}/content", dependencies=[Depends(requires_permission("read:memory"))])
async def get_context_content(ctx_id: int):
    """ONE entry's content, rehydrated on demand — the Explorer inspector's
    preview call. This is the only read that should pull external bytes; list
    surfaces stay metadata-only (fields=meta) so paging never drags S3/bastion
    content around. An offline holder returns content=null + content_offline
    (with the machine when known), never an error dump."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}
    eid = brain_service._entity_id
    row = await pool.fetchrow(
        "SELECT id, title, content, source, status, storage_backend, metadata, updated_at "
        "FROM user_context WHERE id = $1 AND entity_id = $2", ctx_id, eid)
    if not row:
        return {"error": "Context entry not found"}
    d = dict(row)
    meta = d.get("metadata")
    meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
    if meta.get("kind") == "file":
        # Binary file lane: never hand the base64 blob to a preview — return
        # the file card (mime + true size); the bytes come from GET /{id}/file.
        return {
            "id": d["id"],
            "title": d["title"],
            "kind": "file",
            "mime": meta.get("mime") or "application/octet-stream",
            "size_bytes": meta.get("size_bytes"),
            "filename": meta.get("from_file") or d["title"],
            "content": None,
            "content_offline": False,
            "storage_backend": d.get("storage_backend") or "pg",
            "source": d["source"],
            "status": d["status"],
            "updated_at": d["updated_at"].isoformat() if d["updated_at"] else None,
        }
    await _rehydrate_uc_content(pool, eid, d)
    machine = None
    if (d.get("storage_backend") or "pg") != "pg":
        try:
            from brain.mastercode import context_backends
            meta = await context_backends._read_meta(
                pool, eid, context_backends.user_context_key(ctx_id))
            if meta:
                machine = meta.get("server") or meta.get("bucket")
        except Exception:
            machine = None
    return {
        "id": d["id"],
        "title": d["title"],
        "content": d.get("content"),
        "content_offline": bool(d.get("content_offline")),
        "storage_backend": d.get("storage_backend") or "pg",
        "machine": machine,
        "source": d["source"],
        "status": d["status"],
        "updated_at": d["updated_at"].isoformat() if d["updated_at"] else None,
    }


@router.get("/{ctx_id}/file", dependencies=[Depends(requires_permission("read:memory"))])
async def get_context_file(ctx_id: int):
    """Serve a binary-lane row's ORIGINAL bytes (the Explorer's image preview
    and download link). Rehydrates from the row's backend (S3/bastion) exactly
    like /content, base64-decodes, and streams with the stored mime type. An
    offline holder is a 503 with an honest message, never a broken decode."""
    pool = brain_service.get_pool()
    if not pool:
        raise HTTPException(status_code=503, detail="Brain not initialized")
    eid = brain_service._entity_id
    row = await pool.fetchrow(
        "SELECT id, title, content, storage_backend, metadata "
        "FROM user_context WHERE id = $1 AND entity_id = $2", ctx_id, eid)
    if not row:
        raise HTTPException(status_code=404, detail="Context entry not found")
    d = dict(row)
    meta = d.get("metadata")
    meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
    if meta.get("kind") != "file":
        raise HTTPException(status_code=404,
                            detail="Not a file entry — use /content for text")
    await _rehydrate_uc_content(pool, eid, d)
    if d.get("content_offline") or not d.get("content"):
        raise HTTPException(
            status_code=503,
            detail="This file's holding machine isn't checked in right now")
    try:
        data = base64.b64decode(d["content"])
    except Exception:
        raise HTTPException(status_code=500, detail="Stored file is unreadable")
    fname = meta.get("from_file") or d.get("title") or f"context-{ctx_id}"
    # RFC 5987 filename* so non-ASCII names survive; the plain filename is an
    # ASCII-safe fallback.
    from urllib.parse import quote
    ascii_name = fname.encode("ascii", errors="replace").decode("ascii").replace('"', "'")
    disposition = (f'inline; filename="{ascii_name}"; '
                   f"filename*=UTF-8''{quote(fname)}")
    return Response(
        content=data,
        media_type=meta.get("mime") or "application/octet-stream",
        headers={"Content-Disposition": disposition})


@router.get("/stats", dependencies=[Depends(requires_permission("read:memory"))])
async def context_stats():
    """Get counts by source and status for the dashboard."""
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}

    eid = brain_service._entity_id

    # user_context is lazily created — absent on hosts that never ingested any.
    # No context yet ⇒ report zeros instead of a 500.
    if not await user_context_exists(pool):
        return {"total": 0, "embedded": 0, "unembedded": 0,
                "by_status": [], "by_source": []}

    total = await pool.fetchval(
        "SELECT COUNT(*) FROM user_context WHERE entity_id = $1", eid
    )

    by_status = await pool.fetch("""
        SELECT status, COUNT(*) as count
        FROM user_context WHERE entity_id = $1
        GROUP BY status ORDER BY count DESC
    """, eid)

    by_source = await pool.fetch("""
        SELECT source, COUNT(*) as count
        FROM user_context WHERE entity_id = $1
        GROUP BY source ORDER BY count DESC
    """, eid)

    # ANY dim counts as embedded — the 1024-only check here shipped blind to
    # every non-1024 model (same bug class fixed in list_context_service).
    embedded = await pool.fetchval(
        f"SELECT COUNT(*) FROM user_context WHERE entity_id = $1 AND {ANY_EMBEDDED_SQL}",
        eid,
    )
    # File-lane rows are stored, never embedded — counting them "unembedded"
    # would show coverage debt no job can ever clear.
    unembedded = await pool.fetchval(
        f"SELECT COUNT(*) FROM user_context WHERE entity_id = $1 AND NOT {ANY_EMBEDDED_SQL} AND status != 'dismissed' AND {NOT_FILE_KIND_SQL}",
        eid,
    )

    return {
        "total": total,
        "embedded": embedded,
        "unembedded": unembedded,
        "by_status": [dict(r) for r in by_status],
        "by_source": [dict(r) for r in by_source],
    }


# ── Upload pipeline ──────────────────────────────────────────────

# ONE size bound for a file the Host keeps — an upload into Documents (either
# lane) and a chat attachment kept on the Host (circles._keep_attachments_on_host)
# read this same number. The text lane once had its
# own 10 MiB, below the file lane's 25 MiB, though it is chunked and guards
# nothing smaller, so a 12 MB CSV bank statement was refused while a 25 MB PDF
# was taken; and a chat took 30 MB files the Host then could not keep.
#
# What it protects is the Host pod's own MEMORY: the file lane holds the whole
# file, its base64 form (4/3x) and the database parameter carrying it at once
# (_ingest_binary_file writes the base64 into one user_context value before it
# moves to the selected backend), and the Host's reader then reads the bytes
# again, inside a pod whose memory its plan sizes. Raising it needs the upload
# streamed to the backend instead of held whole, not a bigger number here.
MAX_FILE_UPLOAD_BYTES = 25 * 1024 * 1024

# The text lane takes what the file lane takes (it used to stop at 10 MiB).
# Kept as a name for callers that import it.
MAX_UPLOAD_BYTES = MAX_FILE_UPLOAD_BYTES


def upload_too_large_message(size: int, cap: int = MAX_FILE_UPLOAD_BYTES) -> str:
    """The one plain sentence every lane refuses an oversize file with."""
    return (f"This file is about {max(1, size >> 20)} MB. A file kept on this Host "
            f"is at most {cap >> 20} MB, because the Host holds the whole file in "
            f"memory while it stores and reads it. Split it, or keep it on one of "
            f"your machines and read it from there.")

# Extensions ingested as embeddable TEXT (decoded, chunked, embedded).
# Anything else lands in the file lane: ONE row per upload, base64 bytes,
# metadata.kind='file' — stored on the host's selected backend (platform /
# S3 / home bastion), findable by name and collection, never embedded.
# csv/tsv are in the TEXT lane (reversing an earlier choice): the
# live use case is "upload statements, have the host read them", and file-lane
# CSVs were invisible to Documents AND unsearchable (base64, no embeddings) —
# the worst of both. They embed via _chunk_csv (header repeated per chunk so
# every chunk stays interpretable), NOT the prose windows the earlier note
# rightly rejected.
TEXT_UPLOAD_EXTS = {"txt", "md", "markdown", "json", "log", "yaml", "yml"}
CSV_UPLOAD_EXTS = {"csv", "tsv"}
_CSV_MIMES = ("text/csv", "text/tab-separated-values")

# How aggressively to chunk plain text uploads. ~1500 chars is small enough
# that the 1024-d embedding still has signal but big enough that a typical
# document doesn't explode into thousands of fragments.
TEXT_CHUNK_CHARS = 1500
TEXT_CHUNK_OVERLAP = 150


def _chunk_csv(text: str) -> list[str]:
    """Chunk delimited data by ROW GROUPS with the header line repeated at the
    top of every chunk — a bare window of rows 400-430 embeds as noise, but
    header+rows reads as a table. No overlap: rows are atomic records."""
    lines = text.strip().splitlines()
    if not lines:
        return []
    header, rows = lines[0], lines[1:]
    if not rows:
        return [header]
    chunks: list[str] = []
    cur: list[str] = [header]
    cur_len = len(header)
    for row in rows:
        if cur_len + len(row) + 1 > TEXT_CHUNK_CHARS and len(cur) > 1:
            chunks.append("\n".join(cur))
            cur, cur_len = [header], len(header)
        cur.append(row)
        cur_len += len(row) + 1
    if len(cur) > 1:
        chunks.append("\n".join(cur))
    return chunks


def _chunk_text(text: str) -> list[str]:
    """Split a large text into overlapping windows for embedding."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= TEXT_CHUNK_CHARS:
        return [text]

    chunks: list[str] = []
    step = TEXT_CHUNK_CHARS - TEXT_CHUNK_OVERLAP
    i = 0
    while i < len(text):
        chunk = text[i:i + TEXT_CHUNK_CHARS]
        chunks.append(chunk)
        if i + TEXT_CHUNK_CHARS >= len(text):
            break
        i += step
    return chunks


def _parse_json_payload(raw: bytes, filename: str) -> tuple[list[dict], Optional[str]]:
    """Parse a JSON upload into context items.

    Recognized shapes:
      - {"items": [{"content": ..., "title": ..., ...}, ...]}  — same as /import
      - [{"content": ...}, ...]                                — bare array
      - ChatGPT export {"messages": [...]} (best-effort)
      - Anthropic Claude export {"messages": [...]}            — best-effort
      - Anything else with a string "content" or "text" field
    Returns (items, detected_source) where items are
    {content, title?, source_id?, tags?, metadata?, created_at?} and
    detected_source is 'chatgpt'/'claude' when the messages shape identifies a
    conversation export (so a dragged-in export lands under the SAME source
    vocabulary /import uses, not a generic json_upload), else None.
    """
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        raise ValueError(f"Invalid JSON: {e}")

    items: list[dict] = []

    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            return [x for x in data["items"] if isinstance(x, dict)], None
        if isinstance(data.get("messages"), list):
            detected = None
            for m in data["messages"]:
                if not isinstance(m, dict):
                    continue
                content = m.get("content") or m.get("text") or ""
                if isinstance(content, list):  # Claude content blocks
                    detected = detected or "claude"
                    parts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            parts.append(c.get("text") or "")
                    content = "\n".join(parts)
                if not isinstance(content, str) or not content.strip():
                    continue
                role = m.get("role") or m.get("author", {}).get("role") or "unknown"
                # Export fingerprints: ChatGPT exports carry create_time and/or
                # an author.role object; Claude exports carry uuid ids or the
                # content-block shape handled above.
                if detected is None:
                    if m.get("create_time") is not None or isinstance(m.get("author"), dict):
                        detected = "chatgpt"
                    elif m.get("uuid"):
                        detected = "claude"
                items.append({
                    "content": content,
                    "title": f"{role} message",
                    "source_id": m.get("id") or m.get("uuid"),
                    "metadata": {"role": role, "from_file": filename},
                    "created_at": m.get("create_time") or m.get("created_at"),
                })
            return items, detected
        # Single-doc shape: {"content": "..."}
        if isinstance(data.get("content"), str):
            return [data], None

    if isinstance(data, list):
        for x in data:
            if isinstance(x, dict) and isinstance(x.get("content") or x.get("text"), str):
                items.append({
                    "content": x.get("content") or x.get("text"),
                    "title": x.get("title"),
                    "source_id": x.get("id") or x.get("source_id"),
                    "tags": x.get("tags") or [],
                    "metadata": {**(x.get("metadata") or {}), "from_file": filename},
                    "created_at": x.get("created_at") or x.get("timestamp"),
                })
        return items, None

    raise ValueError("Unrecognized JSON shape")


async def _supersede_prior_copies(pool, eid: int, fname: str, batch_id: str,
                                  new_ids: list, upload_tags: Optional[list] = None) -> int:
    """Re-upload = REPLACE (found in a live test: re-uploading a CSV
    stacked a second full copy — duplicate search hits, and a filename-scoped
    read interleaved both copies). Supersede prior RAW copies of this same
    file: carry their collections (tags) onto the new rows, then soft-dismiss
    them. 'promoted' rows are left alone (they are referenced from
    entity_memory); nothing is ever hard-deleted. Returns how many rows were
    dismissed; a failure here never fails the upload.

    SCOPED TO THE PROJECT. A file is tagged with the project it
    was uploaded to, and that is where it lives: re-uploading spec.pdf into
    project B must not dismiss project A's spec.pdf. So only prior copies in
    the same project scope are replaced — sharing a project:<id> tag with this
    upload, or, for an upload with no project, carrying none."""
    if not fname:
        return 0
    projects = [t for t in (upload_tags or []) if t.startswith("project:")]
    # $4: this upload's project tags. Empty → match rows with no project tag.
    # Text filed before batch stamps carries no tags of its own, so its scope
    # is its FILE's: it is never matched here directly, and follows its file
    # row below instead.
    scope_sql = ("AND (CASE WHEN cardinality($4::text[]) > 0 THEN tags && $4::text[] "
                 "ELSE NOT EXISTS (SELECT 1 FROM unnest(tags) t WHERE t LIKE 'project:%') END) "
                 "AND NOT (source = 'extracted' AND metadata->>'upload_batch' IS NULL) ")
    try:
        old_tags = [
            r["t"] for r in await pool.fetch(
                "SELECT DISTINCT unnest(tags) AS t FROM user_context "
                "WHERE entity_id = $1 AND metadata->>'from_file' = $2 "
                "AND COALESCE(metadata->>'upload_batch','') != $3 "
                "AND status = 'raw' " + scope_sql,
                eid, fname, batch_id, projects)
            if r["t"]
        ]
        if old_tags and new_ids:
            await pool.execute(
                "UPDATE user_context SET tags = ("
                "SELECT ARRAY(SELECT DISTINCT x FROM unnest(tags || $1::text[]) x)) "
                "WHERE id = ANY($2::int[]) AND entity_id = $3",
                old_tags, new_ids, eid)
        res_sup = await pool.execute(
            "UPDATE user_context SET status = 'dismissed', updated_at = now() "
            "WHERE entity_id = $1 AND metadata->>'from_file' = $2 "
            "AND COALESCE(metadata->>'upload_batch','') != $3 "
            "AND status = 'raw' " + scope_sql,
            eid, fname, batch_id, projects)
        # ...and that legacy text goes with the file row it hangs off.
        res_legacy = await pool.execute(
            "UPDATE user_context SET status = 'dismissed', updated_at = now() "
            "WHERE entity_id = $1 AND source = 'extracted' AND status = 'raw' "
            "AND metadata->>'upload_batch' IS NULL "
            "AND media_parent_id IN (SELECT f.id FROM user_context f "
            "  WHERE f.entity_id = $1 AND f.status = 'dismissed' "
            "  AND f.metadata->>'from_file' = $2 "
            "  AND COALESCE(f.metadata->>'upload_batch','') != $3)",
            eid, fname, batch_id)
        superseded = 0
        for res in (res_sup, res_legacy):
            try:
                superseded += int(str(res).rsplit(" ", 1)[-1])
            except (ValueError, IndexError):
                pass
        if superseded:
            logger.info("upload_context: superseded %d prior row(s) of %r "
                        "(re-upload = replace)", superseded, fname)
        return superseded
    except Exception as e:
        logger.warning("upload_context: supersede of prior %r copies failed "
                       "(new upload unaffected): %s", fname, e)
        return 0


async def _ingest_binary_file(pool, eid: int, raw: bytes, fname: str,
                              ftype: str, upload_tags: list[str],
                              batch_id: str, *, supersede: bool = True) -> dict:
    """The binary file lane: ONE user_context row per upload. content holds the
    base64 bytes (a str, so the existing pg/S3/bastion store pipeline carries it
    unchanged); metadata records kind='file', the mime type, and the TRUE byte
    count (content length is the base64 inflation, never the file size). The
    lexical index gets the FILENAME only — base64 must never enter search_tsv.
    No embed job: these rows are stored, not vectorized (NOT_FILE_KIND_SQL keeps
    them out of every embed surface). Bytes move to the host's selected backend
    exactly like /import's stage path — a store failure keeps the row pg, never
    a half-write."""
    mime = (ftype.split(";")[0].strip() if ftype else "")
    if not mime or mime == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(fname)
        mime = guessed or "application/octet-stream"
    content = base64.b64encode(raw).decode("ascii")
    meta = {"from_file": fname, "upload_batch": batch_id, "kind": "file",
            "mime": mime, "size_bytes": len(raw), "encoding": "base64"}
    tsv = _TSV_SQL.format(title="$4", content="''")
    row_id = await pool.fetchval(f"""
        INSERT INTO user_context
            (entity_id, content, source, title, tags, metadata, search_tsv)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, {tsv})
        RETURNING id
    """, eid, content, "file_upload", fname, upload_tags, json.dumps(meta))

    backend = "pg"
    try:
        from brain.mastercode import context_backends
        stored = await context_backends.store_user_context_content(
            pool, eid, row_id, content)
        if stored and stored != "pg":
            await pool.execute(
                "UPDATE user_context SET content = $1, storage_backend = $2 "
                "WHERE id = $3 AND entity_id = $4",
                _UC_EXTERNAL_MARKER, stored, row_id, eid)
            backend = stored
    except Exception as se:
        logger.warning("file upload %s externalize failed, kept pg: %s",
                       row_id, se)

    # THE HOST READS WHAT IT CAN, NOW. A file the host has a reader for (PDF,
    # Office, RTF, email, HTML — server/readers) is read the moment it lands,
    # by a plain function: its text is filed as rows carrying this upload's
    # tags (a Workbench upload is tagged project:<id>, so is its text) and an
    # embed job is queued. A file uploaded to a Host lives
    # there and is processed there, as a read op — never by dispatching an AI.
    # A read that fails leaves the file stored and says why; the upload itself
    # never fails for it.
    read = None
    try:
        from server.routes.context_extract import HOST_TOOL, extract_context_file
        from server.readers.docread import reader_for
        if reader_for(mime):
            # tool=HOST_TOOL: the upload reads with the host's own reader
            # only. A job on one of the user's machines (OCR of a scan) is
            # an explicit act — op='extract' — never something an upload
            # starts and holds the request open for.
            read = await extract_context_file(pool, eid, row_id, data=raw,
                                              tool=HOST_TOOL)
    except Exception as e:
        logger.warning("file upload %s: host read failed (%s)", row_id, e)
        read = {"error": f"the host could not read this file ({type(e).__name__})"}

    # Re-upload = REPLACE, the same rule as the text lane: the
    # prior copies of this filename — the file row AND the text read from it —
    # hand their collections to this upload and are dismissed, so a read or a
    # search never mixes two versions of one document.
    #
    # supersede=False: a file dropped into a CHAT is its own copy. Chat names
    # are not chosen (every pasted picture is "image.png"), so replacing by
    # name would dismiss an unrelated upload and take its collections.
    new_ids = [int(row_id)] + [int(i) for i in ((read or {}).get("row_ids") or [])]
    superseded = (await _supersede_prior_copies(pool, eid, fname, batch_id, new_ids,
                                                upload_tags) if supersede else 0)

    # Media lane: a PICTURE gets an embed job so it becomes searchable
    # by what is in it, not just its filename.
    #
    # A CLIP IS NOT QUEUED HERE, and that is not the same as "video is not
    # supported". Frames and speech come out of a recording on one of the
    # user's machines (POST /api/context-extract/run) — the host carries no
    # decoder — so until that runs the clip is stored and findable by
    # filename, and the coverage lens counts it as a clip with no frames
    # rather than as a failure.
    #
    # job_id stays None when media embedding is not set up, which is both the
    # honest answer (nothing was queued) and what the binary-file-lane smoke
    # gate asserts for an unconfigured host (scripts/smoke_test.sh, the
    # "PNG upload stored (no embed job)" check).
    job_id = None
    try:
        from mastercode.media_engine import media_config, media_kind
        if media_kind(mime) == "picture" and await media_config(pool, eid):
            # One live sweep is enough: the job selects every picture missing a
            # vector, so a second one behind it would find nothing. Cheaper to
            # check than to queue thirty no-op jobs when someone drops thirty
            # screenshots at once.
            live = await pool.fetchval("""
                SELECT id FROM embed_jobs
                 WHERE entity_id = $1 AND target = 'user_context_media'
                   AND status IN ('pending', 'running', 'paused')
                 LIMIT 1
            """, eid)
            if live:
                job_id = int(live)
            else:
                from server.routes.embed import enqueue_job
                job = await enqueue_job(eid, "fill", "user_context_media")
                job_id = (job or {}).get("id")
    except Exception as e:
        # A picture that is stored but not queued is a coverage gap the lens
        # reports and a later fill closes. It is never a failed upload.
        logger.info("file upload %s: media embed not queued (%s)", row_id, e)

    return {"imported": 1, "skipped": 0, "errors": [], "filename": fname,
            "id": row_id, "kind": "file", "mime": mime,
            "size_bytes": len(raw), "backend": backend, "job_id": job_id,
            "read": read, "superseded": superseded}


@router.post("/upload", dependencies=[Depends(requires_permission("write:memory"))])
async def upload_context(
    request: Request,
    x_filename: Optional[str] = Header(None),
    x_tags: Optional[str] = Header(None),
):
    """Drag-and-drop file ingestion into user_context.

    Client sends the raw file bytes as the request body and the original
    filename in the X-Filename header. This avoids the python-multipart
    dependency that FastAPI's UploadFile requires; works fine with
    `fetch(url, {method:'POST', body: file, headers:{'X-Filename': name}})`
    from a drag-drop handler.

    X-Tags (optional): comma-separated, each tag URI-encoded like X-Filename
    (so a tag containing a comma survives) — applied to every imported row.
    The Explorer's drop-on-collection uses this.

    Every row of one upload carries metadata.upload_batch (a uuid minted per
    request) — the collision-proof file-group key: re-uploading notes.txt must
    NOT merge into an earlier notes.txt's chunk group.

    TEXT_UPLOAD_EXTS files are the text lane: decoded, chunked, embedded
    (.json single-doc, items array, ChatGPT/Claude export — a recognized
    conversation export lands as source 'chatgpt'/'claude', the same
    vocabulary /import uses). EVERYTHING ELSE — pictures, PDFs, spreadsheets,
    any binary — is the file lane: one row, bytes stored as-is on the host's
    backend (platform / S3 / bastion). A file the host has a reader for (PDF,
    Office, RTF, email) is then READ in the same request: its text lands as
    rows carrying the same tags, and is embedded.
    Returns: {imported, skipped, errors, job_id} — plus `read` for the file
    lane (what the host's reader did, or why it could not).
    """
    pool = brain_service.get_pool()
    if not pool:
        return {"error": "Brain not initialized"}

    eid = brain_service._entity_id
    # One bound for both lanes (MAX_FILE_UPLOAD_BYTES), refused with a 413
    # BEFORE the body is read when Content-Length already says it is over, and
    # as it arrives otherwise (it used to read the whole body, then
    # answer 200 with an error inside).
    from server.harness_input import HarnessInputRefused, read_request
    try:
        _buf = await read_request(request, MAX_FILE_UPLOAD_BYTES, "upload")
    except HarnessInputRefused:
        try:
            _size = int(request.headers.get("content-length") or 0)
        except ValueError:
            _size = 0
        raise HTTPException(status_code=413, detail=upload_too_large_message(
            max(_size, MAX_FILE_UPLOAD_BYTES + 1))) from None
    raw = bytes(_buf)
    del _buf
    if not raw:
        return {"error": "Empty file"}

    # Filename arrives URI-encoded so non-ASCII names survive the header.
    from urllib.parse import unquote
    fname = unquote((x_filename or "upload").strip()) or "upload"
    ftype = (request.headers.get("content-type") or "").lower()
    ext = (fname.rsplit(".", 1)[-1] if "." in fname else "").lower()

    # Lane split: known text/csv extensions (or an extensionless body the
    # browser says is text/json/csv) embed; everything else stores as-is.
    base_mime = ftype.split(";")[0].strip()
    is_csv = (ext in CSV_UPLOAD_EXTS
              or (not ext and base_mime in _CSV_MIMES))
    is_text = (is_csv
               or ext in TEXT_UPLOAD_EXTS
               or (not ext
                   and (ftype.startswith("text/") or "json" in ftype)))
    # X-Tags: comma-separated, per-tag URI-encoded. The same rule as a row's
    # tags (_clean_tags_noted: MAX_TAGS_PER_ROW, MAX_TAG_LEN — it was 24 here
    # against 50 on a row), and a trim is said in the answer.
    upload_tags: list[str] = []
    tags_note = None
    if x_tags:
        upload_tags, tags_note = _clean_tags_noted(
            [unquote(part) for part in x_tags.split(",")])
        upload_tags = upload_tags or []

    batch_id = uuid.uuid4().hex

    if not is_text:
        _res = await _ingest_binary_file(pool, eid, raw, fname, ftype,
                                         upload_tags, batch_id)
        if tags_note:
            _res["tags_note"] = tags_note
        return _res

    items: list[dict] = []
    parse_error: Optional[str] = None

    try:
        if is_csv:
            text = raw.decode("utf-8", errors="replace")
            chunks = _chunk_csv(text)
            items = [
                {
                    "content": chunk,
                    "title": f"{fname} ({i + 1}/{len(chunks)})" if len(chunks) > 1 else fname,
                    "metadata": {"from_file": fname, "chunk": i, "chunk_count": len(chunks)},
                }
                for i, chunk in enumerate(chunks)
            ]
            source = "csv_upload"
        elif ext == "json" or "json" in ftype:
            items, detected_source = _parse_json_payload(raw, fname)
            source = detected_source or "json_upload"
        else:
            # Treat as plain text / markdown
            text = raw.decode("utf-8", errors="replace")
            chunks = _chunk_text(text)
            items = [
                {
                    "content": chunk,
                    "title": f"{fname} ({i + 1}/{len(chunks)})" if len(chunks) > 1 else fname,
                    "metadata": {"from_file": fname, "chunk": i, "chunk_count": len(chunks)},
                }
                for i, chunk in enumerate(chunks)
            ]
            source = "text_upload" if ext != "md" else "markdown_upload"
    except Exception as e:
        parse_error = str(e)
        items = []

    if not items:
        return {
            "imported": 0,
            "skipped": 0,
            "errors": [parse_error] if parse_error else ["No content"],
            "filename": fname,
        }

    # Resolve the host's content backend ONCE for the batch — /upload rows
    # honor the same pg/S3/bastion selection /import's stage path does (the
    # drag-drop lane used to silently pin everything to pg). A resolver
    # failure degrades to pg.
    backend_name = "pg"
    try:
        from brain.mastercode import context_backends
        backend_name, _b = await context_backends.resolve_backend(pool, eid)
    except Exception as e:
        logger.debug("upload_context backend resolve failed (%s) — pg", e)
        backend_name = "pg"

    imported = 0
    skipped = 0
    errors: list[str] = []
    row_ids: list[int] = []
    tsv = _TSV_SQL.format(title="$5", content="$2")
    for item in items:
        content = (item.get("content") or "").strip()
        if not content:
            skipped += 1
            continue
        title = item.get("title")
        source_id = item.get("source_id")
        tags = item.get("tags") or []
        if upload_tags:
            have = {t.lower() for t in tags}
            tags = tags + [t for t in upload_tags if t.lower() not in have]
        meta = item.get("metadata") or {}
        meta.setdefault("from_file", fname)
        meta["upload_batch"] = batch_id
        ts = item.get("created_at")
        ts_dt = None
        if isinstance(ts, str):
            try:
                ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                ts_dt = None
        try:
            if ts_dt:
                row_id = await pool.fetchval(f"""
                    INSERT INTO user_context
                        (entity_id, content, source, source_id, title, tags, metadata, created_at, search_tsv)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, {tsv})
                    RETURNING id
                """, eid, content, source, source_id, title, tags, json.dumps(meta), ts_dt)
            else:
                row_id = await pool.fetchval(f"""
                    INSERT INTO user_context
                        (entity_id, content, source, source_id, title, tags, metadata, search_tsv)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, {tsv})
                    RETURNING id
                """, eid, content, source, source_id, title, tags, json.dumps(meta))

            # Non-pg backend → move the bytes external, leave the marker (same
            # contract as stage_context_service: search_tsv above was computed
            # from the real bytes; a store failure keeps the row pg).
            if backend_name != "pg" and row_id is not None:
                try:
                    from brain.mastercode import context_backends
                    stored = await context_backends.store_user_context_content(
                        pool, eid, row_id, content)
                    if stored and stored != "pg":
                        await pool.execute(
                            "UPDATE user_context SET content = $1, storage_backend = $2 "
                            "WHERE id = $3 AND entity_id = $4",
                            _UC_EXTERNAL_MARKER, stored, row_id, eid)
                except Exception as se:
                    logger.warning("user_context %s externalize failed, kept pg: %s",
                                   row_id, se)
            imported += 1
            if row_id is not None:
                row_ids.append(int(row_id))
        except Exception as e:
            errors.append(str(e))

    # Re-upload = REPLACE (found in a live test: re-uploading a CSV
    # stacked a second full copy — duplicate search hits, and a filename-scoped
    # read interleaved both copies). Supersede prior RAW copies of this same
    # file: carry their collections (tags) onto the new rows, then soft-dismiss
    # them. 'promoted' rows are left alone (they are referenced from
    # entity_memory); nothing is ever hard-deleted.
    superseded = 0
    if imported > 0 and fname:
        superseded = await _supersede_prior_copies(pool, eid, fname, batch_id, row_ids,
                                                   upload_tags)

    # Enqueue an embed job for the freshly imported rows. metadata.ids scopes
    # the job to THIS upload (per-ids denominator, _job_ids), so the app's
    # progress bar reports this file's chunks — not the whole corpus backlog.
    job_id: Optional[int] = None
    embed_skipped: Optional[str] = None
    if imported > 0:
        from server.routes import embed as _embed
        try:
            job = await _embed.enqueue_job(
                entity_id=eid,
                job_type="upload_context",
                target="user_context",
                metadata={"filename": fname, "imported": imported,
                          "source": source, "ids": row_ids},
            )
        except ValueError as e:
            # Rows imported fine; embedding just can't start until a model is
            # configured. Report, don't fail the upload.
            logger.warning("upload_context: embed job not enqueued: %s", e)
            job = None
            embed_skipped = str(e)
        if job is not None:
            job_id = job["id"]

    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors[:10],
        "filename": fname,
        "job_id": job_id,
        # Text rows imported but NOT embedded (no embedding model). The UI must
        # say so — a bare "stored" here read as "ready" while semantic search
        # over the file silently returned nothing.
        "embed_skipped": embed_skipped,
        # Prior raw copies of this filename soft-dismissed by this upload
        # (re-upload = replace).
        "superseded": superseded,
        # How many errors there were when more than the ten listed.
        "errors_total": len(errors),
        # Collections over the per-row rule, said rather than dropped quietly.
        "tags_note": tags_note,
    }


# ── Context-intake service layer ─────────────────────────────────
# Extracted from the /import, /search, /{id}/promote, GET '' routes so the
# context_ingest host tool shares the EXACT staging/search/promote/list logic +
# multi-dim embedding routing WITHOUT faking a Request. The routes are thin
# adapters over these; the tool calls them directly.


class ContextError(Exception):
    """A context-intake failure the caller surfaces. `status` is the HTTP code the
    route maps it to; the context_ingest tool reports `detail`. Used only for the
    not-found / already-promoted cases the old /{id}/promote route returned as an
    {"error": ...} body — behavior preserved, just raised so one path serves both
    the route (mapped per-case) and the tool ({status: error})."""
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def stage_context_service(pool, entity_id, *, source="manual", items=None,
                                embed_now=False):
    """Stage external material into user_context — the SINGLE source of truth for
    POST /import AND the context_ingest tool's stage/bulk ops. Each item lands in
    the embedding column matching the entity's active model's dim (whitelisted
    name, mirrors entity_memory). embed_now embeds inline via the bridge; else the
    row is left unembedded for a later durable embed job. Returns
    {imported, skipped, errors, source, embedded}; an empty batch returns the old
    route's {error, imported:0} body (no raise). Reads nothing from a Request."""
    items = items or []
    if not items:
        return {"error": "No items to import", "imported": 0}

    # Multi-dim: imported embeddings land in the column matching the entity's
    # active model's dim (mirrors entity_memory). Whitelisted name.
    from mastercode.memory_engine import active_embedding_column
    content_col, _ = await active_embedding_column(pool, entity_id)

    # Part 4: resolve the host's content backend ONCE for this batch. pg (default)
    # → content stays inline, the original path byte-for-byte. Non-pg → after the
    # row is inserted, the content bytes are moved to the backend and the inline
    # column becomes the external marker. A resolver failure degrades to pg.
    backend_name = "pg"
    try:
        from brain.mastercode import context_backends
        backend_name, _b = await context_backends.resolve_backend(pool, entity_id)
    except Exception as e:
        logger.debug("user_context backend resolve failed (%s) — pg", e)
        backend_name = "pg"

    imported = 0
    skipped = 0
    errors = []
    externalized = 0

    for i, item in enumerate(items):
        content = (item.get("content") or "").strip()
        if not content:
            skipped += 1
            continue

        title = item.get("title")
        source_id = item.get("source_id")
        tags = item.get("tags", [])
        metadata = item.get("metadata", {})
        created_at = item.get("created_at")

        # Parse created_at if provided
        ts = None
        if created_at:
            try:
                if isinstance(created_at, str):
                    ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                ts = None

        try:
            embedding = None
            if embed_now:
                embedding = await _generate_embedding(content)

            # Insert (pg path unchanged) and capture the id so a non-pg backend can
            # move the bytes out under the synthetic key. search_tsv is computed
            # HERE, from the insert's own params — before any externalization
            # replaces content with the marker (the lexical index must always
            # reflect the real bytes).
            tsv = _TSV_SQL.format(title="$5", content="$2")
            if ts:
                row_id = await pool.fetchval(f"""
                    INSERT INTO user_context
                        (entity_id, content, source, source_id, title, {content_col}, tags, metadata, created_at, search_tsv)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, {tsv})
                    RETURNING id
                """, entity_id, content, source, source_id, title, embedding,
                    tags, json.dumps(metadata), ts)
            else:
                row_id = await pool.fetchval(f"""
                    INSERT INTO user_context
                        (entity_id, content, source, source_id, title, {content_col}, tags, metadata, search_tsv)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, {tsv})
                    RETURNING id
                """, entity_id, content, source, source_id, title, embedding,
                    tags, json.dumps(metadata))

            # Part 4: non-pg backend → move the raw bytes external, leave a marker.
            # The embedding (above) STAYS in pg so search never touches the backend.
            # A store failure leaves the row as a normal pg row (no half-write) and
            # is recorded as an error, not raised — the import keeps going.
            if backend_name != "pg" and row_id is not None:
                try:
                    stored = await context_backends.store_user_context_content(
                        pool, entity_id, row_id, content)
                    if stored and stored != "pg":
                        await pool.execute(
                            "UPDATE user_context SET content = $1, storage_backend = $2 "
                            "WHERE id = $3 AND entity_id = $4",
                            _UC_EXTERNAL_MARKER, stored, row_id, entity_id)
                        externalized += 1
                except Exception as se:
                    logger.warning("user_context %s externalize failed, kept pg: %s",
                                   row_id, se)

            imported += 1
        except Exception as e:
            errors.append({"index": i, "error": str(e)})
            logger.warning("Failed to import context item %d: %s", i, e)

    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors[:10],  # Cap error list
        "source": source,
        "embedded": embed_now,
        "backend": backend_name,
        "externalized": externalized,
    }


# RRF constant — same value the in-pod code-search engine uses
# (brain/codesearch/searcher.py); each arm contributes 1/(K + rank).
_RRF_K = 60


def _rrf_fuse_by_id(vector_hits: list[dict], lexical_hits: list[dict],
                    media_hits: list[dict] | None = None,
                    k: int = _RRF_K) -> list[dict]:
    """Reciprocal Rank Fusion keyed by row id (port of codesearch.searcher.rrf_fuse,
    which keys by (file_path, chunk_index)). A row found by several arms sums its
    contributions; each row carries matched=['semantic'|'lexical'|'media'...].

    The media arm is a THIRD leg, not a replacement: a query runs
    against the text corpus and the picture corpus at once, and the two are
    fused by rank rather than by score — which is the whole reason RRF is used
    here, since a text cosine and a picture cosine are not comparable numbers."""
    scores: dict[int, float] = {}
    rows: dict[int, dict] = {}
    matched: dict[int, list] = {}
    for arm_name, arm in (("semantic", vector_hits), ("lexical", lexical_hits),
                          ("media", media_hits or [])):
        for rank, hit in enumerate(arm):
            key = hit["id"]
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            rows.setdefault(key, hit)
            matched.setdefault(key, []).append(arm_name)
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    return [dict(rows[key], rrf_score=round(sc, 6), matched=matched[key])
            for key, sc in ordered]


async def search_context_service(pool, entity_id, *, query="", limit=20):
    """HYBRID search within user_context — the SINGLE source of truth for
    POST /search AND the context_ingest tool's search op.

    Two arms, RRF-fused (the same shape as the in-pod code-search engine):
      semantic — embed the query via the bridge with the entity's active model,
                 nearest-neighbour the matching dim column (whitelisted name).
      lexical  — Postgres FTS over search_tsv (populated at write time from the
                 real bytes, so external rows match without touching a backend).
    Degrades gracefully: a failed query embed runs lexical-only (flagged via
    `arms`); rows predating the tsv backfill simply don't match lexically.
    Returns {results, query, arms}; each result carries matched=[...] +
    group_key (upload_batch / from_file) so a UI can group hits by file.
    A blank query returns the old {error, results: []} body (no raise)."""
    query = (query or "").strip()
    limit = min(int(limit or 20), 100)

    if not query:
        return {"error": "Query required", "results": []}

    # user_context is lazily created — a host that never ingested context has
    # no table; searching an empty corpus finds nothing (never a 500).
    if not await user_context_exists(pool):
        return {"results": [], "query": query, "arms": []}

    candidates = max(limit, 30)  # per-arm candidate pool pre-fusion

    vector_hits: list[dict] = []
    arms = []
    query_vec = await _generate_query_embedding(query)
    if query_vec:
        arms.append("semantic")
        # Multi-dim: search the column matching the entity's active model's dim.
        # Whitelisted name (injection-safe).
        from mastercode.memory_engine import active_embedding_column, embedding_cast_type
        content_col, dim = await active_embedding_column(pool, entity_id)
        cast = embedding_cast_type(dim)
        rows = await pool.fetch(f"""
            SELECT id, content, source, source_id, title, tags, metadata, status,
                   created_at, storage_backend,
                   1 - ({content_col} <=> $1::{cast}) AS similarity
            FROM user_context
            WHERE entity_id = $2
              AND {content_col} IS NOT NULL
              AND status != 'dismissed'
            ORDER BY {content_col} <=> $1::{cast}
            LIMIT $3
        """, query_vec, entity_id, candidates)
        vector_hits = [dict(r) for r in rows]
        for d in vector_hits:
            d["similarity"] = round(float(d["similarity"]), 4)

    lexical_hits: list[dict] = []
    try:
        lex_rows = await pool.fetch("""
            SELECT id, content, source, source_id, title, tags, metadata, status,
                   created_at, storage_backend,
                   ts_rank(search_tsv, q) AS lex_rank
            FROM user_context, plainto_tsquery('english', $1) AS q
            WHERE entity_id = $2
              AND status != 'dismissed'
              AND search_tsv @@ q
            ORDER BY lex_rank DESC
            LIMIT $3
        """, query, entity_id, candidates)
        lexical_hits = [dict(r) for r in lex_rows]
        for d in lexical_hits:
            d["lex_rank"] = round(float(d["lex_rank"]), 6)
        arms.append("lexical")
    except Exception as e:
        # Pre-migration pod (no search_tsv yet): semantic-only, never a 500.
        logger.info("context lexical search unavailable: %s", e)

    # Media arm: the SAME typed words, sent through the media model's
    # TEXT tower, nearest-neighboured against the picture column. This is what
    # makes "the whiteboard photo" findable — the switch-time probe already
    # proved both towers land in one space, so a text query and an image vector
    # are comparable here. Silent no-op when media is not set up: pictures are
    # still findable by filename through the lexical arm above.
    media_hits: list[dict] = []
    try:
        from mastercode.media_engine import (
            active_media_target, embed_media, media_cast_type, media_config,
            media_fresh_sql)
        media_col, media_dim, media_model = await active_media_target(pool, entity_id)
        if media_col:
            mcfg = await media_config(pool, entity_id)
            qvec = await embed_media(mcfg, text=query, entity_id=entity_id, pool=pool)
            if qvec:
                mcast = media_cast_type(media_dim)
                # content is NULL::text ON PURPOSE, not an oversight. A row with
                # a media vector is a picture row or a video FRAME row: the
                # picture holds the full base64 of an image in content — up to
                # 25MB each, inflated ~33% — and the frame holds nothing at all
                # (a frame keeps the vector and the timestamp, never the bytes).
                # Selecting the column is wasted either way, and the assembly
                # below sets it to None regardless. Selecting it pulled as much
                # as 30 pictures out of Postgres and materialised them in the
                # pod on EVERY context search once media was configured, to
                # throw them all away. The bytes have a route of their own (GET
                # /{id}/file); a search result never needs them.
                mrows = await pool.fetch(f"""
                    SELECT id, NULL::text AS content, source, source_id, title,
                           tags, metadata,
                           status, created_at, storage_backend,
                           1 - ({media_col} <=> $1::{mcast}) AS media_similarity
                    FROM user_context
                    WHERE entity_id = $2
                      AND {media_fresh_sql(media_col, '$4')}
                      AND status != 'dismissed'
                    ORDER BY {media_col} <=> $1::{mcast}
                    LIMIT $3
                """, "[" + ",".join(str(x) for x in qvec) + "]", entity_id,
                     candidates, media_model)
                media_hits = [dict(r) for r in mrows]
                for d in media_hits:
                    d["media_similarity"] = round(float(d["media_similarity"]), 4)
                arms.append("media")
    except Exception as e:
        # The user's media endpoint being down must never take the text search
        # with it. Degrade to the arms that answered.
        logger.info("context media search unavailable: %s", e)

    if not arms:
        return {"error": "Embedding generation failed", "results": []}

    fused = _rrf_fuse_by_id(vector_hits, lexical_hits, media_hits)[:limit]

    # Part 4: rehydrate external rows' content (the marker → the real bytes from the
    # backend). The ranking already happened in pg, so this only touches the
    # returned results. An offline holder yields None → the result keeps its
    # marker-free placeholder rather than leaking the internal marker.
    results = []
    for d in fused:
        meta = d.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        meta = meta or {}
        if meta.get("kind") == "file":
            # Binary-lane row (matched by filename): never hand the base64
            # blob to a result list — the file card is the result. No
            # rehydrate either; a search must not drag S3/bastion bytes.
            d["content"] = None
            d["kind"] = "file"
            d["mime"] = meta.get("mime")
            d["size_bytes"] = meta.get("size_bytes")
        else:
            await _rehydrate_uc_content(pool, entity_id, d)
        d["group_key"] = meta.get("upload_batch") or meta.get("from_file") or f"row:{d['id']}"
        results.append(d)
    return {"results": results, "query": query, "arms": arms}


PROMOTE_TYPES = ("research", "cited_fact", "preference", "relationship")


async def promote_context_service(pool, entity_id, ctx_id, *, memory_type="research",
                                  importance=1.0):
    """Promote a staged user_context entry into entity_memory — the internalization
    step, the SINGLE source of truth for POST /{id}/promote AND the context_ingest
    tool's promote op. Reads the context vector from, and writes the entity_memory
    vector into, the column matching the entity's active model's dim (whitelisted).
    Raises ContextError(404) if the entry is absent and ContextError(409) if it's
    already promoted (the old route returned these as {error} bodies; raising lets
    both callers map them). Returns {promoted, context_id, memory_id, memory_type}.
    Reads nothing from a Request.

    A promoted entry becomes one of PROMOTE_TYPES: research (what a document
    says, the default), a cited fact, or a preference or relationship note
    about the person.
    The other kept types have their own writers (migrations/v235_one_memory.sql).
    Raises ContextError(400) for any other type."""
    memory_type = (memory_type or "research").strip()
    if memory_type not in PROMOTE_TYPES:
        raise ContextError(400, f"memory_type must be one of {', '.join(PROMOTE_TYPES)}")
    importance = float(importance)

    # Fetch the context entry
    ctx = await pool.fetchrow(
        "SELECT * FROM user_context WHERE id = $1 AND entity_id = $2",
        ctx_id, entity_id,
    )
    if not ctx:
        raise ContextError(404, "Context entry not found")

    if ctx["status"] == "promoted":
        raise ContextError(409, f"Already promoted (memory {ctx['promoted_to']})")

    # Binary-lane rows are stored bytes, not language — promoting one would
    # internalize base64 into entity_memory. Refuse honestly (the app disables
    # the button; this guards the context_ingest tool path too).
    _pmeta = ctx["metadata"]
    _pmeta = json.loads(_pmeta) if isinstance(_pmeta, str) else (_pmeta or {})
    if _pmeta.get("kind") == "file":
        raise ContextError(
            409, "This is a stored file (picture/PDF/binary) — files are kept "
                 "as-is and can't be promoted into the host's memory")

    # Part 4: an external (non-pg) row holds a marker in content — rehydrate the
    # real bytes from the backend before internalizing into entity_memory. If the
    # holder is offline the bytes are unavailable; refuse rather than promote a
    # marker into the entity's identity.
    content = ctx["content"]
    storage_backend = None
    try:
        storage_backend = ctx["storage_backend"]
    except (KeyError, TypeError):
        storage_backend = "pg"
    if storage_backend and storage_backend != "pg":
        from brain.mastercode import context_backends
        rehydrated = await context_backends.retrieve_user_context_content(
            pool, entity_id, ctx_id, storage_backend)
        if rehydrated is None:
            raise ContextError(
                503, f"context content lives on backend '{storage_backend}' which is "
                     f"currently unreachable — cannot promote (try again when online)")
        content = rehydrated

    from mastercode.memory_engine import active_embedding_column

    # Multi-dim: read the user_context vector from, and write the entity_memory
    # vector into, the column matching the entity's active model's dim.
    # Whitelisted name (injection-safe).
    content_col, _ = await active_embedding_column(pool, entity_id)

    # Re-use the embedding if it exists in the active column, otherwise generate
    # (from the REHYDRATED content — for a pg row `content` == ctx["content"]).
    embedding = ctx[content_col]
    if not embedding:
        embedding = await _generate_embedding(content)

    # Preserve source metadata
    metadata = {}
    if ctx["metadata"]:
        try:
            metadata = json.loads(ctx["metadata"]) if isinstance(ctx["metadata"], str) else dict(ctx["metadata"])
        except (ValueError, TypeError):
            pass
    metadata["promoted_from_context"] = ctx_id
    metadata["original_source"] = ctx["source"]
    if ctx["source_id"]:
        metadata["original_source_id"] = ctx["source_id"]
    if ctx["tags"]:
        metadata["import_tags"] = list(ctx["tags"])

    # Insert into entity_memory (content embedding into the active dim column)
    mem_id = await pool.fetchval(f"""
        INSERT INTO entity_memory
            (entity_id, content, memory_type, importance, confidence,
             {content_col}, metadata,
             visibility, status, source_session)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb,
                'context', 'active', 'user_context_import')
        RETURNING id
    """,
        entity_id, content, memory_type, importance, 0.8,
        embedding, json.dumps(metadata),
    )

    # Mark context as promoted
    await pool.execute("""
        UPDATE user_context
        SET status = 'promoted', promoted_to = $1, updated_at = now()
        WHERE id = $2
    """, mem_id, ctx_id)

    # P4 write→retrievable probe: an internalization that cannot be recalled
    # fails loud HERE, not as absence days later.
    from mastercode.memory_retrieval import verify_retrievable
    probe = await verify_retrievable(pool, entity_id, mem_id)

    return {
        "promoted": True,
        "context_id": ctx_id,
        "memory_id": mem_id,
        "memory_type": memory_type,
        "retrievable": probe,
    }


async def list_context_service(pool, entity_id, *, source=None, status="raw",
                               page=1, per_page=50, fields="full",
                               upload_batch=None, from_file=None,
                               order="desc", parent_batch=None):
    """List staged user_context entries — the SINGLE source of truth for GET ''
    (list_context) AND the context_ingest tool's list op. Paginated, optionally
    filtered by source/status (status='all' drops the status filter). Returns
    {items, total, page, per_page, pages}. Pure read; raises nothing. Reads
    nothing from a Request.

    fields='meta' skips content entirely — no bytes selected, NO external-row
    rehydration (the full mode awaits a backend fetch per external row per page,
    which is the wrong cost model for a browser list; the Explorer lists with
    meta and pulls one entry's bytes via GET /{id}/content when opened)."""
    page = max(int(page or 1), 1)
    per_page = min(max(int(per_page or 50), 1), 200)
    offset = (page - 1) * per_page
    meta_only = (fields == "meta")

    # user_context is lazily created — a host that never ingested context has
    # no table; list it as an EMPTY corpus (this function raises nothing).
    if not await user_context_exists(pool):
        return {"items": [], "total": 0, "page": page,
                "per_page": per_page, "pages": 0}

    where = "WHERE entity_id = $1"
    params = [entity_id]

    # 'active' = everything except dismissed (raw + promoted) — the read lane's
    # filter: a superseded/dismissed copy must never leak into a document read.
    if status == "active":
        where += " AND status != 'dismissed'"
    elif status and status != "all":
        params.append(status)
        where += f" AND status = ${len(params)}"

    if source:
        params.append(source)
        where += f" AND source = ${len(params)}"

    # File-group filters — the Explorer inspector's chunk list for ONE file.
    if upload_batch:
        params.append(str(upload_batch))
        where += f" AND metadata->>'upload_batch' = ${len(params)}"
    if from_file:
        params.append(str(from_file))
        where += f" AND metadata->>'from_file' = ${len(params)}"
    # Text rows filed before batch stamps: scoped to ONE upload through the
    # file row they hang off (media_parent_id), so a same-named file in
    # another project never mixes in.
    if parent_batch:
        params.append(str(parent_batch))
        where += (f" AND media_parent_id IN (SELECT f.id FROM user_context f "
                  f"WHERE f.entity_id = $1 AND f.metadata->>'upload_batch' = ${len(params)})")

    count = await pool.fetchval(
        f"SELECT COUNT(*) FROM user_context {where}", *params
    )

    params.extend([per_page, offset])
    content_sel = "length(content) AS content_size" if meta_only else "content"
    # has_embedding: ANY dim column counts (the 1024-only check shipped blind
    # to 768/2048/2560/4096 models — every dim surface uses ANY_EMBEDDED_SQL).
    rows = await pool.fetch(f"""
        SELECT id, {content_sel}, source, source_id, title, tags, metadata, status,
               promoted_to, created_at, updated_at, storage_backend,
               {ANY_EMBEDDED_SQL} AS has_embedding
        FROM user_context
        {where}
        ORDER BY created_at {"ASC, id ASC" if order == "asc" else "DESC"}
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
    """, *params)

    # Part 4 (full mode only): rehydrate external rows' content (the marker →
    # real bytes); pg rows are untouched. Offline holders flag content_offline
    # rather than leak the marker.
    items = []
    for r in rows:
        d = dict(r)
        if not meta_only:
            await _rehydrate_uc_content(pool, entity_id, d)
        items.append(d)

    return {
        "items": items,
        "total": count,
        "page": page,
        "per_page": per_page,
        "pages": (count + per_page - 1) // per_page if count else 0,
    }

