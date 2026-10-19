"""
Context Manager API — the HTTP control plane for the Context app (Part 5).

The `context_backend` agent tool (brain/mastercode/runtime/tools.py) is the
AGENT-facing surface for WHERE the host's context CONTENT bytes live. This module
is the USER-facing HTTP mirror of that surface, scoped to read:context /
write:context, so the Context system app can render the same "what's where" map
and drive placement/availability without going through the agent tool.

DOCTRINE (load-bearing): HOST MEMORY (entity_memory — the AI's internalized
memory) is NOT user context. USER CONTEXT is what the USER owns and controls —
the user_context intake corpus PLUS the host's entity_context corpus (the bytes
the host fetches on demand and never keeps a copy of). This app is about USER
CONTEXT: where it lives, on which machine, with how much redundancy.

NO DUPLICATE LOGIC: every endpoint reuses brain.mastercode.context_backends
(the Part 2-4 functions) and, for the embed surface, the existing
server.routes.embed durable-job primitive — exactly the functions the agent tool
and the user_context intake routes already call.

Endpoints (all under /api/context-manager):
  GET    /files               — the Context Explorer's FILE-level rollup: chunk
                                rows grouped into files (upload_batch/from_file),
                                paginated with a real total, filter + sort.
  GET    /sources             — the Explorer's left rail: platform/S3/bastion
                                usage + online state + portal shared folders.
  GET    /index               — the per-file "what's where" map (entity_context
                                external rows UNION pg-resident rows UNION
                                user_context rows). Same shape the tool's list op
                                returns, plus user_context rows carry embedded?.
                                (Tool/API compat — the app now renders /files.)
  GET    /placement           — current host-wide backend + routes + availability.
  GET    /bastions            — choosable context-store machines (online + capable).
  POST   /route               — add/replace a per-prefix route.
  DELETE /route/{prefix}      — clear a per-prefix route.
  POST   /default             — set the host-wide default backend.
  GET    /embedding           — embedded vs unembedded user_context counts (status).
  POST   /embedding           — enqueue a durable embed job (all unembedded, or
                                per-ids: the Explorer's per-file re-embed).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from server import brain_service
from server.applications.middleware import requires_permission
from brain.mastercode import context_backends

logger = logging.getLogger("privatae.brain.context_manager")

router = APIRouter(prefix="/api/context-manager", tags=["brain", "context"])

# NO model constant — embed jobs resolve the entity's CONFIGURED model
# (behavioral_params.embedding_model) inside embed.enqueue_job.


def _pool_eid():
    """The (pool, entity_id) pair every endpoint runs against, or (None, None)
    if the brain isn't initialized yet. Mirrors server/routes/context.py."""
    pool = brain_service.get_pool()
    if pool is None:
        return None, None
    return pool, brain_service._entity_id


# ─────────────────────────────────────────────────────────────────────────────
# Read — the where-it-lives index
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/index", dependencies=[Depends(requires_permission("read:context"))])
async def context_index(
    prefix: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=2000),
):
    """The host-local context index — one row per piece of context: where its
    bytes live (backend + machine), how big, online/offline (for a bastion-held
    file), and whether it's embedded.

    Three sources are UNION'd (the tool's list op + the user_context corpus):
      1. entity_context EXTERNAL files — from context_backend_meta (authoritative
         for backend + physical location: server | bucket | endpoint).
      2. entity_context PG-RESIDENT files — from entity_context, backend='pg',
         machine='platform' (skipped where an index row already covers the path).
      3. user_context rows — the USER-OWNED intake corpus. backend comes from
         user_context.storage_backend (pg | s3 | bastion); these carry embedded?
         because user_context HAS embedding columns (entity_context does not).

    Returns {"files": [...], "count": N}, file_path-sorted then capped at `limit`.
    """
    pool, eid = _pool_eid()
    if pool is None:
        return {"error": "Brain not initialized"}
    prefix = (prefix or "").strip()

    # Online status per bastion machine (offline surfacing). hostname -> online.
    bastion_online: dict[str, bool] = {}
    try:
        from server.network import topology
        for b in await topology.list_bastions(pool, eid):
            if b.get("hostname"):
                bastion_online[b["hostname"]] = bool(b.get("online"))
    except Exception:
        bastion_online = {}

    # Replica fan-out per file (physical copies recorded for a replicate write).
    replicas_by_file: dict[str, int] = {}
    try:
        rep_rows = await pool.fetch(
            "SELECT file_path, count(*) AS n FROM context_backend_locations "
            "WHERE entity_id = $1 GROUP BY file_path", eid)
        replicas_by_file = {r["file_path"]: int(r["n"]) for r in rep_rows}
    except Exception:
        replicas_by_file = {}

    files: dict[str, dict] = {}

    # 1) entity_context EXTERNAL files (context_backend_meta is authoritative).
    #    user_context's synthetic keys also live here (user_context_key uses a
    #    '␞user_context␞/' prefix) — skip them so the user_context pass owns
    #    those rows (it can stamp owner + embedded + a friendly title).
    uc_key_prefix = context_backends._USER_CONTEXT_KEY_PREFIX
    ext_rows = await pool.fetch(
        "SELECT file_path, backend, object_key, size_bytes, "
        "       server, bucket, endpoint, updated_at "
        "FROM context_backend_meta WHERE entity_id = $1 "
        "ORDER BY file_path", eid)
    for r in ext_rows:
        fp = r["file_path"]
        if fp.startswith(uc_key_prefix):
            continue  # owned by the user_context pass below
        if prefix and not fp.startswith(prefix):
            continue
        backend = r["backend"]
        machine = (r["server"] or r["bucket"]
                   or ("platform" if backend == "pg" else None))
        if backend == "bastion" and r["server"]:
            online = bastion_online.get(r["server"], False)
        else:
            online = True
        files[fp] = {
            "file_path": fp,
            "owner": "context",          # entity_context corpus (host-held, user-owned)
            "backend": backend,
            "machine": machine,
            "online": online,
            "embedded": None,            # entity_context has no embedding column
            "replicas": replicas_by_file.get(fp, 0),
            "object_key": r["object_key"],
            "size_bytes": int(r["size_bytes"] or 0),
            "endpoint": r["endpoint"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }

    # 2) entity_context PG-RESIDENT files. Skip any path already covered by an
    #    index row (an external file keeps a pg pointer row; the index row wins).
    try:
        if prefix:
            pg_rows = await pool.fetch(
                "SELECT file_path, length(content) AS size, updated_at "
                "FROM entity_context WHERE entity_id = $1 AND file_path LIKE $2 "
                "ORDER BY file_path", eid, prefix + "%")
        else:
            pg_rows = await pool.fetch(
                "SELECT file_path, length(content) AS size, updated_at "
                "FROM entity_context WHERE entity_id = $1 "
                "ORDER BY file_path", eid)
    except Exception as e:
        logger.info("context index: entity_context read failed: %s", e)
        pg_rows = []
    for r in pg_rows:
        fp = r["file_path"]
        if fp in files:
            continue
        files[fp] = {
            "file_path": fp,
            "owner": "context",
            "backend": "pg",
            "machine": "platform",
            "online": True,
            "embedded": None,
            "replicas": 0,
            "object_key": fp,
            "size_bytes": int(r["size"] or 0),
            "endpoint": None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }

    # 3) user_context rows — the USER-OWNED intake corpus. backend from
    #    storage_backend; embedded? from the presence of any dim embedding. For a
    #    non-pg row the physical machine + online live in the synthetic index row
    #    (context_backend_meta under user_context_key); join it in so the user
    #    sees WHICH machine holds the bytes, not just the backend name.
    from server.routes.context import ANY_EMBEDDED_SQL
    try:
        uc_rows = await pool.fetch(
            f"SELECT id, title, source, status, storage_backend, "
            f"       length(content) AS size, updated_at, "
            f"       {ANY_EMBEDDED_SQL} AS embedded "
            f"FROM user_context WHERE entity_id = $1 AND status != 'dismissed' "
            f"ORDER BY id", eid)
    except Exception as e:
        logger.info("context index: user_context read failed: %s", e)
        uc_rows = []
    for r in uc_rows:
        backend = (r["storage_backend"] or "pg")
        title = r["title"] or f"entry #{r['id']}"
        # Synthetic index key for the friendly display path + (non-pg) location.
        synth_key = context_backends.user_context_key(r["id"])
        display = f"user_context/{r['id']} — {title}"
        if prefix and not (display.startswith(prefix) or synth_key.startswith(prefix)):
            continue
        machine = "platform"
        online = True
        endpoint = None
        replicas = 0
        size = int(r["size"] or 0)
        if backend != "pg":
            try:
                meta = await context_backends._read_meta(pool, eid, synth_key)
            except Exception:
                meta = None
            if meta:
                machine = (meta.get("server") or meta.get("bucket") or machine)
                endpoint = meta.get("endpoint")
                if meta.get("size_bytes"):
                    size = int(meta["size_bytes"])
                if backend == "bastion" and meta.get("server"):
                    online = bastion_online.get(meta["server"], False)
            replicas = replicas_by_file.get(synth_key, 0)
        files[display] = {
            "file_path": display,
            "owner": "user_context",     # the user's intake corpus
            "backend": backend,
            "machine": machine,
            "online": online,
            "embedded": bool(r["embedded"]),
            "replicas": replicas,
            "object_key": synth_key,
            "size_bytes": size,
            "endpoint": endpoint,
            "source": r["source"],
            "status": r["status"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }

    rows = sorted(files.values(), key=lambda d: d["file_path"])[:limit]
    return {"files": rows, "count": len(rows)}


# ── /files SQL — module-level so tests can run the EXACT production
#    statements against a real schema. $1 = the synthetic user_context key
#    prefix, $2 = entity_id. ANY dim column counts as embedded (the shared
#    fragment from context.py — 1024-only and four-column checks have both
#    shipped blind spots).

_UC_FILES_SQL_TMPL = """
    WITH g AS (
        SELECT u.id,
               COALESCE(NULLIF(u.metadata->>'upload_batch', ''),
                        u.source || '|' || NULLIF(u.metadata->>'from_file', ''),
                        'row:' || u.id::text) AS gkey,
               u.source, u.title, u.tags, u.updated_at,
               COALESCE(u.storage_backend, 'pg') AS backend,
               NULLIF(u.metadata->>'upload_batch', '') AS upload_batch,
               NULLIF(u.metadata->>'from_file', '')    AS from_file,
               (u.status = 'promoted') AS is_promoted,
               {any_embedded} AS is_embedded,
               (COALESCE(u.metadata->>'kind', '') = 'file') AS is_file,
               NULLIF(u.metadata->>'mime', '') AS mime,
               -- File-lane rows record the TRUE byte count in metadata (the
               -- content column holds base64, ~4/3× the file); text rows fall
               -- through to the old inline-length / backend-meta logic.
               COALESCE((u.metadata->>'size_bytes')::bigint,
                        CASE WHEN COALESCE(u.storage_backend, 'pg') = 'pg'
                             THEN length(u.content)
                             ELSE COALESCE(m.size_bytes, 0) END) AS size_bytes,
               COALESCE(m.server, m.bucket,
                        CASE WHEN COALESCE(u.storage_backend, 'pg') = 'pg'
                             THEN 'platform' END) AS machine,
               COALESCE(l.n, 0) AS replicas
        FROM user_context u
        LEFT JOIN context_backend_meta m
               ON m.entity_id = u.entity_id AND m.file_path = $1 || u.id::text
        LEFT JOIN (SELECT file_path, count(*) AS n FROM context_backend_locations
                   WHERE entity_id = $2 GROUP BY file_path) l
               ON l.file_path = $1 || u.id::text
        WHERE u.entity_id = $2 AND u.status != 'dismissed'
    ),
    g_tags AS (
        SELECT gkey, array_agg(DISTINCT t) AS tags
        FROM g, unnest(g.tags) AS t
        GROUP BY gkey
    )
    SELECT g.gkey,
           'user_context'::text AS owner,
           COALESCE(max(g.from_file), max(g.title), 'entry #' || min(g.id)) AS name,
           min(g.source) AS source,
           count(*)::int AS chunks,
           COALESCE(sum(g.size_bytes), 0)::bigint AS size_bytes,
           CASE WHEN bool_or(g.is_file)     THEN NULL
                WHEN bool_and(g.is_embedded) THEN 'all'
                WHEN bool_or(g.is_embedded)  THEN 'some'
                ELSE 'none' END AS embedded,
           bool_or(g.is_file) AS is_file,
           max(g.mime) AS mime,
           array_agg(DISTINCT g.backend) AS backends,
           array_remove(array_agg(DISTINCT g.machine), NULL) AS machines,
           COALESCE(min(gt.tags), '{{}}') AS tags,
           max(g.updated_at) AS updated_at,
           min(g.id)::int AS first_id,
           max(g.upload_batch) AS upload_batch,
           max(g.from_file) AS from_file,
           bool_or(g.is_promoted) AS has_promoted,
           COALESCE(sum(g.replicas), 0)::int AS replicas
    FROM g LEFT JOIN g_tags gt ON gt.gkey = g.gkey
    GROUP BY g.gkey
"""


def _uc_files_sql() -> str:
    from server.routes.context import ANY_EMBEDDED_SQL
    # The fragment's columns are unqualified; inside the CTE the only relation
    # with embedding columns is user_context (aliased u), so they bind to u.
    return _UC_FILES_SQL_TMPL.format(any_embedded=ANY_EMBEDDED_SQL)


_EC_FILES_SQL = """
    SELECT m.file_path AS gkey, 'context'::text AS owner,
           m.file_path AS name, NULL::text AS source, 1::int AS chunks,
           COALESCE(m.size_bytes, 0)::bigint AS size_bytes,
           NULL::text AS embedded,
           false AS is_file, NULL::text AS mime,
           ARRAY[m.backend] AS backends,
           array_remove(ARRAY[COALESCE(m.server, m.bucket,
               CASE WHEN m.backend = 'pg' THEN 'platform' END)], NULL) AS machines,
           '{}'::text[] AS tags, m.updated_at,
           NULL::int AS first_id, NULL::text AS upload_batch, NULL::text AS from_file,
           false AS has_promoted,
           COALESCE(l.n, 0)::int AS replicas
    FROM context_backend_meta m
    LEFT JOIN (SELECT file_path, count(*) AS n FROM context_backend_locations
               WHERE entity_id = $2 GROUP BY file_path) l
           ON l.file_path = m.file_path
    WHERE m.entity_id = $2 AND m.file_path NOT LIKE $1 || '%'
    UNION ALL
    SELECT e.file_path, 'context', e.file_path, NULL, 1,
           length(e.content)::bigint, NULL,
           false, NULL,
           ARRAY['pg'], ARRAY['platform'], '{}'::text[], e.updated_at,
           NULL, NULL, NULL, false, 0
    FROM entity_context e
    WHERE e.entity_id = $2
      AND NOT EXISTS (SELECT 1 FROM context_backend_meta m2
                      WHERE m2.entity_id = $2 AND m2.file_path = e.file_path)
"""


@router.get("/files", dependencies=[Depends(requires_permission("read:context"))])
async def context_files(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort: str = Query("updated", pattern="^(name|size|updated|source)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    q: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    backend: Optional[str] = Query(None),
    machine: Optional[str] = Query(None),
    embedded: Optional[str] = Query(None, pattern="^(all|some|none)$"),
    owner: Optional[str] = Query(None, pattern="^(user_context|context)$"),
    exclude_source_prefix: Optional[str] = Query(None),
):
    """The Explorer's workhorse: the FILE-level view of the whole corpus, with a
    real total and real pagination (this replaces /index for the UI — /index's
    silent default-500 cap stays only for tool/API compat).

    user_context chunk rows roll up into files SQL-side — group key precedence:
    metadata.upload_batch (collision-proof, stamped by /upload) → (source,
    metadata.from_file) (legacy uploads) → the row itself. entity_context files
    (the host corpus) come in one row per file with embedded=NULL (not
    embeddable — shown honestly, not hidden). Set-based: one statement per
    corpus + one batched meta join; no per-row awaits (the /index N+1 is the
    anti-pattern this avoids).

    Returns {files, total, page, per_page, pages}."""
    pool, eid = _pool_eid()
    if pool is None:
        return {"error": "Brain not initialized"}
    # This was `_USER_CONTEXT_KEY_PREFIX.replace("\x00", "�")` — a substitute
    # chosen to match nothing, because the real prefix was NUL-delimited and
    # Postgres rejects NUL in a text value and in a bind parameter alike. That
    # reasoning was correct about the cause and wrong to treat it as a fact of
    # life: it was a bug two files over, and this line was the workaround routing
    # around it. The prefix no longer contains NUL (context_backends.py), so the
    # real value binds and this pass now excludes the rows it was written to
    # exclude instead of silently excluding none.
    uc_prefix = context_backends._USER_CONTEXT_KEY_PREFIX

    # user_context → file groups. ANY dim column counts as embedded (the
    # shared fragment from context.py — 1024-only and four-column checks have
    # both shipped blind spots).
    uc_err = ec_err = None
    from server.routes.context import user_context_exists
    if not await user_context_exists(pool):
        # The table is created lazily on first intake — a host that never
        # ingested context has an EMPTY user corpus, not a failed one (this
        # rollup used to log a warning per hit on such hosts).
        uc_rows = []
    else:
        try:
            uc_rows = await pool.fetch(_uc_files_sql(), uc_prefix, eid)
        except Exception as e:
            logger.warning("context files: user_context rollup failed: %s", e)
            uc_rows, uc_err = [], str(e)

    # entity_context → one row per file (external meta rows ∪ pg-resident rows).
    try:
        ec_rows = await pool.fetch(_EC_FILES_SQL, uc_prefix, eid)
    except Exception as e:
        logger.warning("context files: entity_context pass failed: %s", e)
        ec_rows, ec_err = [], str(e)

    # A failed corpus must render as an ERROR, never as an empty explorer over
    # a populated corpus (the empty-vs-error conflation the design forbids).
    if uc_err and ec_err:
        return {"error": f"context index unavailable: {uc_err}"}

    # Bastion online map — stamped onto the merged rows.
    bastion_online: dict[str, bool] = {}
    try:
        from server.network import topology
        for b in await topology.list_bastions(pool, eid):
            if b.get("hostname"):
                bastion_online[b["hostname"]] = bool(b.get("online"))
    except Exception:
        bastion_online = {}

    files = []
    for r in list(uc_rows) + list(ec_rows):
        d = dict(r)
        d["updated_at"] = d["updated_at"].isoformat() if d.get("updated_at") else None
        machines = list(d.get("machines") or [])
        d["machines"] = machines
        d["online"] = all(
            True if mn == "platform" else bastion_online.get(mn, True)
            for mn in machines) if machines else True
        files.append(d)

    # Filters run python-side over the merged FILE-level rows — already rolled
    # up, orders of magnitude smaller than the chunk rows.
    if owner:
        files = [f for f in files if f["owner"] == owner]
    if q:
        needle = q.lower()
        files = [f for f in files if needle in (f["name"] or "").lower()]
    if tag:
        files = [f for f in files if tag in (f.get("tags") or [])]
    if source:
        # Comma-separated = any-of (the rail's Documents/Conversations libraries
        # span several source values). A value ending in ':' matches by PREFIX
        # — 'tool:' covers every tool-stashed source ('tool:research',
        # 'tool:read_url', …) so the Staged-by-host library can own them
        # (those rows used to appear only under "All items", next to
        # the user's uploads, with nothing saying the host wrote them).
        wanted = {s.strip() for s in source.split(",") if s.strip()}
        prefixes = tuple(w for w in wanted if w.endswith(":"))

        def _src_match(f: dict) -> bool:
            s = f.get("source") or ""
            return s in wanted or (bool(prefixes) and s.startswith(prefixes))

        if "promoted" in wanted:
            # Virtual source: the Promoted library (group has a promoted chunk).
            files = [f for f in files if _src_match(f) or f.get("has_promoted")]
        else:
            files = [f for f in files if _src_match(f)]
    if exclude_source_prefix:
        # The host's op='files' passes 'tool:' here by default: what the host
        # itself stashed from tool results is never "what the user uploaded".
        _xp = exclude_source_prefix.strip()
        if _xp:
            files = [f for f in files if not (f.get("source") or "").startswith(_xp)]
    if backend:
        files = [f for f in files if backend in (f.get("backends") or [])]
    if machine:
        files = [f for f in files if machine in (f.get("machines") or [])]
    if embedded:
        files = [f for f in files if f.get("embedded") == embedded]

    sort_key = {
        "name": lambda f: (f["name"] or "").lower(),
        "size": lambda f: f.get("size_bytes") or 0,
        "updated": lambda f: f.get("updated_at") or "",
        "source": lambda f: ((f.get("source") or ""), (f["name"] or "").lower()),
    }[sort]
    files.sort(key=sort_key, reverse=(order == "desc"))

    total = len(files)
    start = (page - 1) * per_page
    out = {
        "files": files[start:start + per_page],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if total else 0,
    }
    # Partial failure: return what loaded, but say what didn't.
    if uc_err or ec_err:
        out["warning"] = (f"user files unavailable: {uc_err}" if uc_err
                          else f"host files unavailable: {ec_err}")
    return out


@router.get("/sources", dependencies=[Depends(requires_permission("read:context"))])
async def context_sources():
    """The Explorer's left-rail feed — every PHYSICAL place the user's context
    touches, in one call: platform (pg) usage across BOTH pg-resident corpora
    (the /placement usage counts only context_backend_meta, so it misses them),
    each S3 bucket, each bastion (with online state), and the DCN store-class
    view (machine stores incl. absorbed legacy portal folders, cabinet stores,
    platform — merged server-side so the app manifest needs no extra scope)."""
    pool, eid = _pool_eid()
    if pool is None:
        return {"error": "Brain not initialized"}
    uc_prefix = context_backends._USER_CONTEXT_KEY_PREFIX

    # Externally-held bytes per (backend, machine) — synthetic user_context keys
    # included, they are real stored bytes on that machine.
    usage_rows = []
    try:
        usage_rows = await pool.fetch("""
            SELECT backend,
                   COALESCE(server, bucket, 'platform') AS machine,
                   count(*)::int AS files,
                   COALESCE(sum(size_bytes), 0)::bigint AS bytes
            FROM context_backend_meta
            WHERE entity_id = $1
            GROUP BY backend, COALESCE(server, bucket, 'platform')
        """, eid)
    except Exception as e:
        logger.info("context sources: meta usage failed: %s", e)

    # Platform (pg-resident) usage the meta table can't see: inline
    # user_context rows + entity_context rows without an external index row.
    pg_files = pg_bytes = 0
    try:
        r = await pool.fetchrow("""
            SELECT count(*)::int AS files,
                   COALESCE(sum(length(content)), 0)::bigint AS bytes
            FROM user_context
            WHERE entity_id = $1 AND status != 'dismissed'
              AND COALESCE(storage_backend, 'pg') = 'pg'
        """, eid)
        pg_files += int(r["files"]); pg_bytes += int(r["bytes"])
    except Exception:
        pass
    try:
        r = await pool.fetchrow("""
            SELECT count(*)::int AS files,
                   COALESCE(sum(length(e.content)), 0)::bigint AS bytes
            FROM entity_context e
            WHERE e.entity_id = $1
              AND NOT EXISTS (SELECT 1 FROM context_backend_meta m
                              WHERE m.entity_id = $1 AND m.file_path = e.file_path)
        """, eid)
        pg_files += int(r["files"]); pg_bytes += int(r["bytes"])
    except Exception:
        pass

    bastion_online: dict[str, bool] = {}
    try:
        from server.network import topology
        for b in await topology.list_bastions(pool, eid):
            if b.get("hostname"):
                bastion_online[b["hostname"]] = bool(b.get("online"))
    except Exception:
        bastion_online = {}

    sources = [{"kind": "platform", "backend": "pg", "machine": "platform",
                "online": True, "files": pg_files, "bytes": pg_bytes}]
    for r in usage_rows:
        if r["backend"] == "pg":
            continue  # pg meta rows are pointer rows; the platform card owns pg
        machine = r["machine"]
        sources.append({
            "kind": r["backend"],                    # 's3' | 'bastion'
            "backend": r["backend"],
            "machine": machine,
            "online": (bastion_online.get(machine, False)
                       if r["backend"] == "bastion" else True),
            "files": int(r["files"]),
            "bytes": int(r["bytes"]),
        })

    # Choosable-but-empty context-store machines still belong on the rail.
    try:
        for b in await context_backends.list_context_store_bastions(pool, eid):
            hn = b.get("hostname")
            if hn and not any(s["machine"] == hn for s in sources):
                sources.append({"kind": "bastion", "backend": "bastion",
                                "machine": hn, "online": bool(b.get("online", True)),
                                "files": 0, "bytes": 0})
    except Exception:
        pass

    # DCN store classes — the route-map view: granted stores plus
    # derived cards. Portal shared folders are ABSORBED here as legacy
    # machine-class stores (one folder-grant vocabulary),
    # and the S3 filing cabinet surfaces first-class instead of hiding in the
    # placement dropdown.
    stores = []
    try:
        from brain.mastercode import dcn
        stores = await dcn.list_stores(pool, eid)
        for st in stores:
            if st["class"] == "platform":
                st["files"] = pg_files
                st["bytes"] = pg_bytes
    except Exception as e:
        logger.info("context sources: dcn stores read failed: %s", e)

    try:
        cfg = await context_backends.get_backend_config(pool, eid)
    except Exception:
        cfg = None

    return {"sources": sources, "stores": stores, "default": cfg}


@router.get("/placement", dependencies=[Depends(requires_permission("read:context"))])
async def context_placement():
    """The current host-wide backend + the per-prefix routes + the availability /
    replica policy + a per-backend usage rollup. The placement control surface the
    Placement + Availability lenses render. Reuses get_backend_config / get_routes
    (no duplicate logic) — the SAME state the tool's get op returns."""
    pool, eid = _pool_eid()
    if pool is None:
        return {"error": "Brain not initialized"}
    cfg = await context_backends.get_backend_config(pool, eid)
    routes = await context_backends.get_routes(pool, eid)
    usage = []
    try:
        rows = await pool.fetch(
            "SELECT backend, count(*) AS files, COALESCE(sum(size_bytes),0) AS bytes "
            "FROM context_backend_meta WHERE entity_id = $1 GROUP BY backend", eid)
        usage = [{"backend": r["backend"], "files": r["files"], "bytes": int(r["bytes"])}
                 for r in rows]
    except Exception:
        usage = []
    return {
        "current": cfg,
        "routes": routes,
        "usage": usage,
        "available": list(context_backends.VALID_BACKENDS),
        "availability_options": list(context_backends.VALID_AVAILABILITY),
        "replicate_extra_copies": context_backends.REPLICATE_EXTRA_COPIES,
    }


@router.get("/bastions", dependencies=[Depends(requires_permission("read:topology"))])
async def context_bastions():
    """The choosable context-store machines — the host's ONLINE bastions that
    advertise the 'context-store' capability. hostname is the value the route /
    default POSTs pass as `server`. Reuses list_context_store_bastions."""
    pool, eid = _pool_eid()
    if pool is None:
        return {"error": "Brain not initialized"}
    choosable = await context_backends.list_context_store_bastions(pool, eid)
    return {
        "bastions": choosable,
        "capability": context_backends.CONTEXT_STORE_CAPABILITY,
        "count": len(choosable),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Write — placement / availability mutations
# ─────────────────────────────────────────────────────────────────────────────

async def _validate_bastion_fields(pool, eid: int, body: dict) -> Optional[str]:
    """If the chosen backend is bastion (or replicas are named), confirm each
    target is a REAL online context-store bastion. Returns an error string to
    return to the client, or None if everything validates. Mirrors the tool's
    set op so a user can't target a typed/dead hostname."""
    backend = (body.get("backend") or "").strip().lower()
    if backend == "bastion":
        server = (body.get("server") or "").strip()
        ok, reason = await context_backends.validate_bastion_target(pool, eid, server)
        if not ok:
            return f"{reason}. See GET /api/context-manager/bastions for online targets."
    replicas = body.get("replicas")
    if isinstance(replicas, list) and replicas:
        bad = []
        for h in replicas:
            rok, _ = await context_backends.validate_bastion_target(pool, eid, str(h).strip())
            if not rok:
                bad.append(str(h).strip())
        if bad:
            return ("replica target(s) not online context-store bastions: "
                    f"{', '.join(bad)}.")
    return None


@router.post("/route", dependencies=[Depends(requires_permission("write:context"))])
async def set_context_route(request: Request):
    """Add or replace ONE per-prefix route — different context lives on different
    machines. Reuses context_backends.set_route (no duplicate validation logic).

    Body: {
      "prefix": "memory/journals/",      // required — the context path prefix
      "backend": "pg" | "s3" | "bastion",
      // s3:      "bucket", "prefix_key"?, "region"?, "endpoint_url"?, "cred_name"?
      // bastion: "server" (a hostname from /bastions), "root"?
      "availability": "single" | "replicate",   // optional
      "replicas": ["host-a", ...]                // optional explicit bastion targets
    }
    """
    pool, eid = _pool_eid()
    if pool is None:
        return {"error": "Brain not initialized"}
    body = await request.json()
    prefix = (body.get("prefix") or "").strip()
    if not prefix:
        return {"error": "'prefix' is required"}
    err = await _validate_bastion_fields(pool, eid, body)
    if err:
        return {"error": f"set route: {err}"}
    # Build the route dict in the shape _validate_route_cfg expects. The s3 KEY
    # prefix is 'prefix_key' (the route's own prefix is the context path).
    route: dict = {"prefix": prefix, "backend": (body.get("backend") or "pg")}
    for k in ("bucket", "prefix_key", "region", "endpoint_url", "cred_name",
              "server", "root", "availability"):
        if body.get(k) is not None:
            route[k] = body[k]
    if isinstance(body.get("replicas"), list):
        route["replicas"] = body["replicas"]
    try:
        stored = await context_backends.set_route(pool, eid, route)
    except ValueError as ve:
        return {"error": f"set route: {ve}"}
    return {"route_set": stored}


@router.delete("/route/{prefix:path}", dependencies=[Depends(requires_permission("write:context"))])
async def clear_context_route(prefix: str):
    """Remove the per-prefix route for `prefix` (it falls back to the host-wide
    default). Reuses context_backends.clear_route. {prefix:path} so a route prefix
    that contains slashes (e.g. 'memory/journals/') round-trips intact."""
    pool, eid = _pool_eid()
    if pool is None:
        return {"error": "Brain not initialized"}
    prefix = (prefix or "").strip()
    if not prefix:
        return {"error": "'prefix' is required"}
    removed = await context_backends.clear_route(pool, eid, prefix)
    return {"cleared": removed, "prefix": prefix}


@router.post("/default", dependencies=[Depends(requires_permission("write:context"))])
async def set_context_default(request: Request):
    """Set the HOST-WIDE default backend — where NEW context lands when no route
    matches. Reuses context_backends.set_backend_config (its validation owns the
    per-backend required fields). Existing files keep the backend they were
    written with (recorded per-file), so reads stay correct.

    Body: {
      "backend": "pg" | "s3" | "bastion",
      // s3:      "bucket" (req), "prefix"?, "region"?, "endpoint_url"?, "cred_name"?
      // bastion: "server" (req — a hostname from /bastions), "root"?
      "availability": "single" | "replicate",   // optional host-wide toggle
      "replicas": ["host-a", ...]                // optional explicit targets
    }
    """
    pool, eid = _pool_eid()
    if pool is None:
        return {"error": "Brain not initialized"}
    body = await request.json()
    backend = (body.get("backend") or "").strip().lower()
    if not backend:
        return {"error": "'backend' is required (pg|s3|bastion)"}
    err = await _validate_bastion_fields(pool, eid, body)
    if err:
        return {"error": f"set default: {err}"}
    cfg: dict = {"backend": backend}
    for k in ("bucket", "prefix", "region", "endpoint_url", "cred_name",
              "server", "root", "availability"):
        if body.get(k) is not None:
            cfg[k] = body[k]
    if isinstance(body.get("replicas"), list):
        cfg["replicas"] = body["replicas"]
    try:
        stored = await context_backends.set_backend_config(pool, eid, cfg)
    except ValueError as ve:
        return {"error": f"set default: {ve}"}
    return {"set": stored}


# ─────────────────────────────────────────────────────────────────────────────
# Embedding — surface + control what's embedded
# ─────────────────────────────────────────────────────────────────────────────
# WHAT'S CONTROLLABLE TODAY (verified against server/routes/context.py +
# server/routes/embed.py): user_context rows carry per-dim embedding columns and
# there is a DURABLE embed-job primitive (embed.enqueue_job, job_type
# 'embed_context', target 'user_context') the existing /api/brain/context/embed
# route already drives. So we expose:
#   GET  — embedded vs unembedded user_context counts (read-only status).
#   POST — enqueue an embed job for unembedded rows (force re-embeds all).
# entity_context content is NOT embedded (no embedding column — context embeddings
# live in user_context / entity_memory), so per-file embedding control for the
# entity_context corpus is OUT OF SCOPE here and FLAGGED as a follow-up.

@router.get("/embedding", dependencies=[Depends(requires_permission("read:context"))])
async def context_embedding_status():
    """Embedded vs unembedded counts for the user_context corpus — the read-only
    status the Embedding lens renders. Same counts server/routes/context.py's
    /stats computes (no duplicate semantics)."""
    pool, eid = _pool_eid()
    if pool is None:
        return {"error": "Brain not initialized"}
    from server.routes.context import ANY_EMBEDDED_SQL, NOT_FILE_KIND_SQL
    try:
        # NOT_FILE_KIND_SQL: binary file-lane rows can NEVER embed — counting
        # them made "unembedded" permanently positive with any picture/PDF in
        # the corpus, so the lens forever implied embedding wasn't done
        # (nobody could tell whether it was embedding).
        total = await pool.fetchval(
            f"SELECT COUNT(*) FROM user_context WHERE entity_id = $1 "
            f"AND status != 'dismissed' AND {NOT_FILE_KIND_SQL}",
            eid)
        # The shared any-dim fragment — the four-column list this used to carry
        # was blind to embedding_2560 (qwen3-embedding:4b).
        embedded = await pool.fetchval(
            f"SELECT COUNT(*) FROM user_context WHERE entity_id = $1 "
            f"AND status != 'dismissed' AND {NOT_FILE_KIND_SQL} AND {ANY_EMBEDDED_SQL}",
            eid)
    except Exception as e:
        logger.info("context embedding status read failed: %s", e)
        total, embedded = 0, 0
    total = int(total or 0)
    embedded = int(embedded or 0)

    # MEDIA coverage, counted SEPARATELY and never mixed into the text numbers
    # above. The text counts exclude file rows via NOT_FILE_KIND_SQL;
    # these count only file rows, so no row is ever in both and neither total
    # can borrow the other's denominator. A host with no media model reports
    # configured=false and every picture as not-searchable — a setup state, not
    # a fault.
    media = {"configured": False, "model": None, "unreadable": False,
             "pictures": 0,
             "pictures_embedded": 0, "pictures_unsearchable": 0,
             "clips": 0, "clips_searchable": 0, "clips_unsearchable": 0,
             "frames": 0, "frames_embedded": 0, "frames_unsearchable": 0}
    try:
        from mastercode.media_engine import (
            active_media_target, media_config_state, media_fresh_sql)
        mcfg, mcfg_unreadable = await media_config_state(pool, eid)
        # "could not be read just now" is not "not set up yet" — the same
        # distinction the recall arm makes (memory_retrieval.py:119-126). Saying
        # a user has no media model when the read merely failed sends them to
        # set up something they already have.
        media["unreadable"] = mcfg_unreadable
        media_col, _, media_model = await active_media_target(pool, eid)
        # The role's own state is settled BEFORE any table is touched. These
        # COUNTs raise UndefinedTableError on a host whose user_context has not
        # been created yet, and with one try around the whole block that landed
        # in the except and reported configured:false — telling a user who HAS
        # set a media model that they have not.
        media["configured"] = bool(mcfg)
        media["model"] = (mcfg or {}).get("model")
        pictures = await pool.fetchval(
            "SELECT COUNT(*) FROM user_context WHERE entity_id = $1 "
            "AND status != 'dismissed' AND metadata->>'kind' = 'file' "
            "AND metadata->>'mime' LIKE 'image/%'", eid)
        clips = await pool.fetchval(
            "SELECT COUNT(*) FROM user_context WHERE entity_id = $1 "
            "AND status != 'dismissed' AND metadata->>'kind' = 'file' "
            "AND metadata->>'mime' LIKE 'video/%'", eid)
        pictures = int(pictures or 0)
        clips = int(clips or 0)
        pics_embedded = 0
        if media_col:
            # Counted against the model configured NOW. A vector written by a
            # previous model of the same width is not searchable with this
            # one's queries, so counting it here would report coverage the
            # user does not have.
            pics_embedded = int(await pool.fetchval(
                f"SELECT COUNT(*) FROM user_context WHERE entity_id = $1 "
                f"AND status != 'dismissed' AND metadata->>'kind' = 'file' "
                f"AND metadata->>'mime' LIKE 'image/%' "
                f"AND {media_fresh_sql(media_col, '$2')}",
                eid, media_model) or 0)
        # FRAMES ARE THEIR OWN POPULATION. A frame row is kind='frame',
        # never 'file', so it is in neither count above and the picture numbers
        # cannot be inflated by a user who uploaded one long video.
        # NOT_FILE_KIND_SQL keeps it out of the text numbers at the same time,
        # so a frame sits in exactly one denominator: this one.
        frames = int(await pool.fetchval(
            "SELECT COUNT(*) FROM user_context WHERE entity_id = $1 "
            "AND status != 'dismissed' AND metadata->>'kind' = 'frame'", eid) or 0)
        frames_fresh = 0
        clips_searchable = 0
        if media_col:
            frames_fresh = int(await pool.fetchval(
                f"SELECT COUNT(*) FROM user_context WHERE entity_id = $1 "
                f"AND status != 'dismissed' AND metadata->>'kind' = 'frame' "
                f"AND {media_fresh_sql(media_col, '$2')}", eid, media_model) or 0)
            # A CLIP IS SEARCHABLE WHEN IT HAS FRAMES THAT ARE. Counted over the
            # clips themselves and never derived from the frame total: one clip
            # with 240 frames beside forty clips with none is not "240 of 240".
            #
            # The freshness predicate is WRITTEN OUT here instead of coming from
            # media_fresh_sql, and that is deliberate: the helper emits an
            # unqualified `media_embed_model`, and inside this subquery both
            # tables have that column. Postgres resolves it by scope rather than
            # raising, so it would be correct today and a silent trap the first
            # time anyone edits the query. It is the same predicate, qualified —
            # scripts/test_media_embedding_lane.py pins the two together so they
            # cannot drift apart.
            clips_searchable = int(await pool.fetchval(
                f"""SELECT COUNT(*) FROM user_context c
                     WHERE c.entity_id = $1 AND c.status != 'dismissed'
                       AND c.metadata->>'kind' = 'file'
                       AND c.metadata->>'mime' LIKE 'video/%'
                       AND EXISTS (SELECT 1 FROM user_context f
                                    WHERE f.entity_id = c.entity_id
                                      AND f.media_parent_id = c.id
                                      AND f.metadata->>'kind' = 'frame'
                                      AND f.{media_col} IS NOT NULL
                                      AND f.media_embed_model IS NOT DISTINCT FROM $2)""",
                eid, media_model) or 0)
        media = {
            "configured": bool(mcfg),
            "model": (mcfg or {}).get("model"),
            "unreadable": mcfg_unreadable,
            "pictures": pictures,
            "pictures_embedded": pics_embedded,
            "pictures_unsearchable": max(0, pictures - pics_embedded),
            # A clip becomes searchable by having FRAMES pulled on the user's
            # own machine and embedded here — the decoder is theirs, never the
            # platform's. A clip nobody has run extraction on is not a failure
            # and is not counted as one.
            "clips": clips,
            "clips_searchable": clips_searchable,
            "clips_unsearchable": max(0, clips - clips_searchable),
            "frames": frames,
            "frames_embedded": frames_fresh,
            "frames_unsearchable": max(0, frames - frames_fresh),
        }
    except Exception as e:
        logger.info("context media coverage read failed: %s", e)

    return {
        "scope": "user_context",
        "total": total,
        "embedded": embedded,
        "unembedded": max(0, total - embedded),
        "media": media,
        # Honest about the boundary — entity_context corpus is not embeddable here.
        "note": ("Embedding control covers the user_context corpus. The host's "
                 "on-demand context files are not embedded (their bytes are fetched "
                 "on demand); per-file embedding of that corpus is a follow-up."),
    }


@router.post("/embedding", dependencies=[Depends(requires_permission("write:context"))])
async def context_embedding_run(request: Request):
    """Enqueue a durable embed job for user_context rows. Reuses the SAME
    embed.enqueue_job primitive the existing /api/brain/context/embed route
    uses (no duplicate job logic); progress is broadcast on the dashboard WS as
    {type:'embed_job',...}.

    Body: {"force": false, "ids": [..]?} — no ids = fill everything unembedded
    (force re-embeds everything); ids = the Explorer's per-file "Re-embed":
    refresh EXACTLY those rows regardless of current vector state."""
    pool, eid = _pool_eid()
    if pool is None:
        return {"error": "Brain not initialized"}
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    force = bool(body.get("force", False))
    metadata: dict = {"force": force}
    ids = body.get("ids")
    if isinstance(ids, list) and ids:
        try:
            metadata["ids"] = [int(i) for i in ids[:1000]]
        except (TypeError, ValueError):
            return {"error": "'ids' must be integers"}
    from server.routes import embed as _embed
    try:
        job = await _embed.enqueue_job(
            entity_id=eid,
            job_type="embed_context",
            target="user_context",
            metadata=metadata,
        )
    except ValueError as e:
        return {"error": str(e)}
    if job is None:
        return {"error": "Brain not initialized"}
    if (job.get("total") or 0) == 0:
        return {"queued": 0, "job_id": job["id"], "message": "All context already embedded"}
    return {"queued": job["total"], "job_id": job["id"], "model": job["model"], "force": force}
