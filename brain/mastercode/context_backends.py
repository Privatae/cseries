"""
Context CONTENT-storage backends.

Intent: user context has S3 and local bastion storage options. Users store
their context on their home bastion, Host-retrievable and user-owned; the S3
context store has metered usage.

The platform's context CONTENT layer (the bytes of an entity's context files)
lives in the `entity_context` table by default. This module makes that content
layer PLUGGABLE so a host can store the bytes elsewhere while the METADATA / index
stays in postgres (entity_context still holds the row; a non-pg backend leaves the
`content` column empty and records WHERE the bytes live in `context_backend_meta`).

Three backends:

  pg       (DEFAULT — current behaviour, UNCHANGED): content lives in
           entity_context.content. No external store, no metering, no migration
           dependency at read time. Selecting nothing => pg.

  s3       Content bytes are S3 objects. Bucket + credentials are SEALED IN THE
           VAULT (server.vault.api, host-owned — the platform can't read them).
           The bucket is the user's own, so the platform does not bill for it —
           their storage provider does. boto3 is LAZY-imported (never at module
           load) so a missing wheel can't brick boot — but boto3 is already in the image (1.40.x).

  bastion  Content bytes live on the user's designated HOME BASTION — a box the
           user owns that has joined their mesh. USER-OWNED: the host RETRIEVES on
           demand over the mesh and NEVER persists the bytes itself. The host-side
           store/retrieve calls dispatch storage ops over the EXISTING bastion
           management transport (server.network.nac_mgmt.run_mgmt_op). The
           bastion-side handler for those ops is a SEPARATE build (see the design
           fork in the module docstring of the bastion backend below).

Backend selection is per-host, stored at behavioral_params.context_backend
(a small JSON blob, same store coding_harnesses uses). The default is pg.

The whole module is import-safe with NO heavy deps at import time.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger("cseries.mastercode.context_backends")

# Valid backend ids. 'pg' is the default and must always work with zero config.
VALID_BACKENDS = ("pg", "s3", "bastion")
DEFAULT_BACKEND = "pg"

# Availability policy for a write (decentralization).
#   single    (DEFAULT): one location, today's behaviour, byte-for-byte.
#   replicate          : fan the bytes out to >1 online context-store location at
#                        write time; reads fail over primary -> replicas. Write-time
#                        fan-out only (no background re-sync) — see the consistency
#                        caveat on store_replicated().
VALID_AVAILABILITY = ("single", "replicate")
DEFAULT_AVAILABILITY = "single"

# The typed bastion capability a box must advertise (in its heartbeat) to be a
# choosable context-store target. Fork-A bastions report this in `capabilities`;
# topology.upsert_heartbeat stores it and list_bastions returns it.
CONTEXT_STORE_CAPABILITY = "context-store"

# How many EXTRA replica locations a replicate write targets beyond the primary.
# Researched default (operating-rule #4): classic distributed stores keep 3 total
# copies (HDFS/Ceph/S3 default RF=3); we target primary + up to 2 replicas = 3,
# capped by how many distinct online context-store bastions actually exist.
REPLICATE_EXTRA_COPIES = int(os.environ.get("CTX_REPLICATE_EXTRA_COPIES", "2"))


# ─────────────────────────────────────────────────────────────────────────────
# Backend selection (per-host, behavioral_params.context_backend)
# ─────────────────────────────────────────────────────────────────────────────

async def get_backend_config(pool, entity_id: int) -> dict:
    """Read this host's context-backend selection from behavioral_params.

    Returns a dict like {"backend": "pg"} (default) or
    {"backend": "s3", "bucket": "...", "prefix": "...", "cred_name": "ctx:s3"} or
    {"backend": "bastion", "server": "home-box", "root": "context/"}.
    Always returns at least {"backend": "pg"} — a missing/garbled config degrades
    to pg so the default path can never be broken by config.
    """
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id = $1", entity_id)
    except Exception as e:
        log.debug("get_backend_config: registry read failed (%s) -> pg", e)
        return {"backend": DEFAULT_BACKEND}
    bp = row["behavioral_params"] if row else None
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except (json.JSONDecodeError, TypeError):
            bp = None
    if not isinstance(bp, dict):
        return {"backend": DEFAULT_BACKEND}
    cfg = bp.get("context_backend")
    if not isinstance(cfg, dict):
        return {"backend": DEFAULT_BACKEND}
    backend = cfg.get("backend")
    if backend not in VALID_BACKENDS:
        return {"backend": DEFAULT_BACKEND}
    return cfg


async def set_backend_config(pool, entity_id: int, cfg: dict) -> dict:
    """Persist this host's context-backend selection into
    behavioral_params.context_backend (merging, not clobbering other params).
    Validates the backend id and the per-backend required fields. Returns the
    stored cfg. Raises ValueError on an invalid selection."""
    backend = (cfg or {}).get("backend", DEFAULT_BACKEND)
    if backend not in VALID_BACKENDS:
        raise ValueError(f"unknown backend '{backend}' (one of {', '.join(VALID_BACKENDS)})")
    clean: dict[str, Any] = {"backend": backend}
    if backend == "s3":
        bucket = (cfg.get("bucket") or "").strip()
        if not bucket:
            raise ValueError("s3 backend requires 'bucket'")
        clean["bucket"] = bucket
        clean["prefix"] = (cfg.get("prefix") or "").strip()
        clean["region"] = (cfg.get("region") or "").strip()
        clean["endpoint_url"] = (cfg.get("endpoint_url") or "").strip()  # for R2/B2/MinIO
        # The credential lives in the vault under this logical name; the agent
        # seals it first via keys_manage/connect_compute and references it here.
        clean["cred_name"] = (cfg.get("cred_name") or "ctx:s3").strip()
    elif backend == "bastion":
        server = (cfg.get("server") or "").strip()
        if not server:
            raise ValueError("bastion backend requires 'server' (a host in your mesh)")
        clean["server"] = server
        clean["root"] = (cfg.get("root") or "context/").strip()
    # Optional host-wide availability policy (single|replicate) + explicit replica
    # targets. 'single' is the default and is stored only when explicitly set, so
    # an existing pg/s3/bastion selection is byte-for-byte unchanged.
    avail = (cfg.get("availability") or "").strip().lower()
    if avail:
        if avail not in VALID_AVAILABILITY:
            raise ValueError(f"availability must be one of {VALID_AVAILABILITY}")
        clean["availability"] = avail
    reps = cfg.get("replicas")
    if reps is not None:
        if not isinstance(reps, list):
            raise ValueError("'replicas' must be a list of bastion hostnames")
        clean["replicas"] = [str(s).strip() for s in reps if str(s).strip()]
    # Merge into behavioral_params.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id = $1", entity_id)
        bp = row["behavioral_params"] if row else None
        if isinstance(bp, str):
            try:
                bp = json.loads(bp)
            except (json.JSONDecodeError, TypeError):
                bp = {}
        if not isinstance(bp, dict):
            bp = {}
        bp["context_backend"] = clean
        await conn.execute(
            "UPDATE entity_registry SET behavioral_params = $2::jsonb WHERE id = $1",
            entity_id, json.dumps(bp))
    return clean


# ─────────────────────────────────────────────────────────────────────────────
# Per-prefix routing (decentralization)
# ─────────────────────────────────────────────────────────────────────────────
# The host-wide selection above sends ALL context to ONE backend. Routing lets
# different context live on different machines: behavioral_params.context_routes
# is a list of {prefix, backend, <backend config>, availability?}. On a WRITE the
# file's path is matched LONGEST-PREFIX-FIRST against the routes; the first match
# wins; no match falls back to the host-wide context_backend (which itself falls
# back to pg). READS are unaffected — they dispatch per-file from the host-local
# index (resolve_backend_for_file), so a file always reads from where it was
# written, whatever the routing says now.

def _validate_route_cfg(route: dict) -> dict:
    """Validate ONE route entry. Returns a clean {prefix, backend, ...,
    availability} dict. Raises ValueError on a bad entry. The per-backend config
    re-uses set_backend_config's validation shape (bucket/cred for s3, server for
    bastion) so a route is configured exactly like the host-wide selection."""
    if not isinstance(route, dict):
        raise ValueError("each route must be an object")
    prefix = (route.get("prefix") or "").strip()
    if not prefix:
        raise ValueError("each route requires a non-empty 'prefix'")
    backend = (route.get("backend") or DEFAULT_BACKEND).strip().lower()
    if backend not in VALID_BACKENDS:
        raise ValueError(f"route '{prefix}': unknown backend '{backend}'")
    clean: dict[str, Any] = {"prefix": prefix, "backend": backend}
    if backend == "s3":
        bucket = (route.get("bucket") or "").strip()
        if not bucket:
            raise ValueError(f"route '{prefix}': s3 backend requires 'bucket'")
        clean["bucket"] = bucket
        clean["prefix_key"] = (route.get("prefix_key") or "").strip()  # s3 key prefix (NOT the route prefix)
        clean["region"] = (route.get("region") or "").strip()
        clean["endpoint_url"] = (route.get("endpoint_url") or "").strip()
        clean["cred_name"] = (route.get("cred_name") or "ctx:s3").strip()
    elif backend == "bastion":
        server = (route.get("server") or "").strip()
        if not server:
            raise ValueError(f"route '{prefix}': bastion backend requires 'server'")
        clean["server"] = server
        clean["root"] = (route.get("root") or "context/").strip()
    avail = (route.get("availability") or DEFAULT_AVAILABILITY).strip().lower()
    if avail not in VALID_AVAILABILITY:
        raise ValueError(f"route '{prefix}': availability must be one of {VALID_AVAILABILITY}")
    clean["availability"] = avail
    # Optional explicit replica targets (bastion hostnames) for a replicate route.
    reps = route.get("replicas")
    if reps is not None:
        if not isinstance(reps, list):
            raise ValueError(f"route '{prefix}': 'replicas' must be a list of bastion hostnames")
        clean["replicas"] = [str(s).strip() for s in reps if str(s).strip()]
    return clean


def _route_backend_cfg(route: dict) -> dict:
    """Map a stored route entry to a backend config dict make_backend() understands
    (translate the route's s3 'prefix_key' back to the backend's 'prefix' key)."""
    backend = route.get("backend", DEFAULT_BACKEND)
    cfg: dict[str, Any] = {"backend": backend}
    if backend == "s3":
        cfg.update({
            "bucket": route.get("bucket"),
            "prefix": route.get("prefix_key") or "",
            "region": route.get("region") or "",
            "endpoint_url": route.get("endpoint_url") or "",
            "cred_name": route.get("cred_name") or "ctx:s3",
        })
    elif backend == "bastion":
        cfg.update({
            "server": route.get("server"),
            "root": route.get("root") or "context/",
        })
    return cfg


async def get_routes(pool, entity_id: int) -> list[dict]:
    """Read this host's context routes (behavioral_params.context_routes), sorted
    LONGEST-PREFIX-FIRST so resolve_route_for_file matches the most specific. A
    missing/garbled config returns [] (→ everything uses the host-wide default)."""
    try:
        row = await pool.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id = $1", entity_id)
    except Exception as e:
        log.debug("get_routes: registry read failed (%s) -> []", e)
        return []
    bp = row["behavioral_params"] if row else None
    if isinstance(bp, str):
        try:
            bp = json.loads(bp)
        except (json.JSONDecodeError, TypeError):
            bp = None
    if not isinstance(bp, dict):
        return []
    routes = bp.get("context_routes")
    if not isinstance(routes, list):
        return []
    clean = []
    for r in routes:
        if isinstance(r, dict) and r.get("prefix"):
            clean.append(r)
    clean.sort(key=lambda r: len(r.get("prefix") or ""), reverse=True)
    return clean


async def set_route(pool, entity_id: int, route: dict) -> dict:
    """Add or replace ONE route (keyed by its prefix) in
    behavioral_params.context_routes. Validates the entry. Returns the stored
    route. To remove a route pass backend='pg' with no other config — it routes
    that prefix back to the platform store explicitly (or use clear_route)."""
    clean = _validate_route_cfg(route)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id = $1", entity_id)
        bp = row["behavioral_params"] if row else None
        if isinstance(bp, str):
            try:
                bp = json.loads(bp)
            except (json.JSONDecodeError, TypeError):
                bp = {}
        if not isinstance(bp, dict):
            bp = {}
        routes = bp.get("context_routes")
        if not isinstance(routes, list):
            routes = []
        routes = [r for r in routes
                  if not (isinstance(r, dict) and r.get("prefix") == clean["prefix"])]
        routes.append(clean)
        bp["context_routes"] = routes
        await conn.execute(
            "UPDATE entity_registry SET behavioral_params = $2::jsonb WHERE id = $1",
            entity_id, json.dumps(bp))
    return clean


async def clear_route(pool, entity_id: int, prefix: str) -> bool:
    """Remove the route for `prefix`. Returns True if one was removed."""
    prefix = (prefix or "").strip()
    if not prefix:
        return False
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT behavioral_params FROM entity_registry WHERE id = $1", entity_id)
        bp = row["behavioral_params"] if row else None
        if isinstance(bp, str):
            try:
                bp = json.loads(bp)
            except (json.JSONDecodeError, TypeError):
                bp = {}
        if not isinstance(bp, dict):
            return False
        routes = bp.get("context_routes")
        if not isinstance(routes, list):
            return False
        kept = [r for r in routes
                if not (isinstance(r, dict) and r.get("prefix") == prefix)]
        if len(kept) == len(routes):
            return False
        bp["context_routes"] = kept
        await conn.execute(
            "UPDATE entity_registry SET behavioral_params = $2::jsonb WHERE id = $1",
            entity_id, json.dumps(bp))
    return True


async def resolve_route_for_write(pool, entity_id: int, file_path: str) -> dict:
    """Pick the WRITE plan for a file: which backend + availability + replica
    targets. Longest-matching route wins; no match → the host-wide selection (with
    availability='single'). Returns {"backend": ContextBackend, "name": str,
    "availability": str, "replicas": [hostname,...], "cfg": dict}.

    The backend is BUILT here so put() doesn't need to know about routing. For a
    'pg' result the backend is a PgBackend (name 'pg') and put() short-circuits to
    its original SQL (zero new round-trips), exactly as today."""
    routes = await get_routes(pool, entity_id)
    for r in routes:
        if file_path.startswith(r["prefix"]):
            cfg = _route_backend_cfg(r)
            b = make_backend(cfg)
            return {"backend": b, "name": b.name,
                    "availability": (r.get("availability") or DEFAULT_AVAILABILITY),
                    "replicas": list(r.get("replicas") or []),
                    "cfg": cfg}
    # No route → host-wide selection. Availability for the host-wide default comes
    # from context_backend.availability if set (a host-level replicate toggle).
    host_cfg = await get_backend_config(pool, entity_id)
    b = make_backend(host_cfg)
    avail = (host_cfg.get("availability") or DEFAULT_AVAILABILITY)
    if avail not in VALID_AVAILABILITY:
        avail = DEFAULT_AVAILABILITY
    return {"backend": b, "name": b.name, "availability": avail,
            "replicas": list(host_cfg.get("replicas") or []), "cfg": host_cfg}


# ─────────────────────────────────────────────────────────────────────────────
# Bastion-picker — choose a REAL, online, context-store-capable target
# ─────────────────────────────────────────────────────────────────────────────

async def list_context_store_bastions(pool, entity_id: int) -> list[dict]:
    """The host's bastions that are ONLINE and advertise the 'context-store'
    capability — the choosable targets for a bastion backend / a replica. Returns
    [{hostname, name, handle, id, capabilities, last_seen_at}], hostname FIRST
    because hostname is the key run_mgmt_op resolves a bastion by (it looks up
    network_bastion_heartbeat.hostname). A bastion with no reported hostname can't
    be a target (run_mgmt_op couldn't address it) and is omitted.

    Best-effort: any failure (no topology, non-entity pod) returns [] so the
    picker degrades to 'no live targets' rather than raising."""
    try:
        from server.network import topology
        rows = await topology.list_bastions(pool, entity_id)
    except Exception as e:
        log.debug("list_context_store_bastions: topology unavailable (%s)", e)
        return []
    out = []
    for b in rows:
        if not b.get("online"):
            continue
        caps = b.get("capabilities") or []
        if CONTEXT_STORE_CAPABILITY not in caps:
            continue
        host = b.get("hostname")
        if not host:
            continue  # un-addressable by run_mgmt_op
        out.append({
            "hostname": host,
            "name": b.get("name"),
            "handle": b.get("handle"),
            "id": b.get("id"),
            "capabilities": caps,
            "last_seen_at": b.get("last_seen_at"),
        })
    return out


async def validate_bastion_target(pool, entity_id: int, server: str) -> tuple[bool, str]:
    """Is `server` (a hostname) an ONLINE context-store bastion right now? Returns
    (ok, reason). Used by the set path to warn/refuse a target that isn't a live,
    capable machine — so the user targets a real box, not a typed hostname."""
    server = (server or "").strip()
    if not server:
        return False, "no server given"
    choosable = await list_context_store_bastions(pool, entity_id)
    if any(b["hostname"] == server for b in choosable):
        return True, "online context-store bastion"
    # Distinguish 'exists but not eligible' from 'unknown' for a useful message.
    try:
        from server.network import topology
        allb = await topology.list_bastions(pool, entity_id)
    except Exception:
        allb = []
    match = next((b for b in allb if b.get("hostname") == server), None)
    if match is None:
        return False, (f"'{server}' is not a known bastion of this host "
                       f"(no heartbeat with that hostname)")
    if not match.get("online"):
        return False, f"bastion '{server}' is offline (no recent heartbeat)"
    if CONTEXT_STORE_CAPABILITY not in (match.get("capabilities") or []):
        return False, (f"bastion '{server}' does not advertise the "
                       f"'{CONTEXT_STORE_CAPABILITY}' capability")
    return False, f"bastion '{server}' is not a choosable context-store target"


# ─────────────────────────────────────────────────────────────────────────────
# context_backend_meta — index of WHERE non-pg content bytes live
# ─────────────────────────────────────────────────────────────────────────────
# entity_context keeps owning the row + content_type + metadata + updated_at (so
# list/search/status are UNCHANGED). For a non-pg backend, entity_context.content
# is left empty (a small pointer marker) and the real byte location is recorded
# here, keyed by (entity_id, file_path). This table is created by migration v076;
# we also CREATE IF NOT EXISTS defensively so a fresh pod is self-healing.

# THE TABLES ARE DECLARED, NOT ENSURED.
# context_backend_meta is migrations/v076_context_backend_meta.sql; its
# server/bucket/endpoint columns and the context_backend_locations child table
# with its three indexes are v077_context_backend_location.sql. This module
# carried a defensive mirror of all of it and called itself self-healing.
# Measured before deleting, rather than taken from those comments: both tables
# exist on all 8 live hosts with shapes IDENTICAL to the declared schema —
# columns, types, nullability, defaults, indexes and constraints. Nothing was
# left to heal. It mattered because _ensure_meta sat on a REQUEST path
# (server/routes/context_manager.py:98/394/531/650), so opening the Context
# Manager issued CREATE TABLE as the `entity` role.


async def _record_meta(pool, entity_id: int, file_path: str, backend: str,
                       object_key: str, size_bytes: int,
                       location: Optional[dict] = None):
    """Record the PRIMARY location of a file's bytes in the host-local index.

    location (v077): {"server": str?, "bucket": str?, "endpoint": str?} — WHICH
    machine (bastion `server`) or WHICH bucket+endpoint (s3) holds the bytes, so
    the index can say where each file lives even after the host switches backends.
    Omitted/None keeps the columns NULL (the v076 behaviour). The primary index
    stays one row per (entity_id, file_path); the child table (locations) is the
    seam for the future replicate policy and is not written here.
    """
    loc = location or {}
    server = loc.get("server") or None
    bucket = loc.get("bucket") or None
    endpoint = loc.get("endpoint") or None
    await pool.execute(
        "INSERT INTO context_backend_meta "
        "(entity_id, file_path, backend, object_key, size_bytes, "
        " server, bucket, endpoint, updated_at) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,now()) "
        "ON CONFLICT (entity_id, file_path) DO UPDATE SET "
        "backend=$3, object_key=$4, size_bytes=$5, "
        "server=$6, bucket=$7, endpoint=$8, updated_at=now()",
        entity_id, file_path, backend, object_key, size_bytes,
        server, bucket, endpoint)


async def _read_meta(pool, entity_id: int, file_path: str) -> Optional[dict]:
    row = await pool.fetchrow(
        "SELECT backend, object_key, size_bytes, server, bucket, endpoint "
        "FROM context_backend_meta "
        "WHERE entity_id = $1 AND file_path = $2", entity_id, file_path)
    return dict(row) if row else None


async def _delete_meta(pool, entity_id: int, file_path: str):
    await pool.execute(
        "DELETE FROM context_backend_meta WHERE entity_id = $1 AND file_path = $2",
        entity_id, file_path)


# ── context_backend_locations — 1..N physical copies per file (replicate) ──────

async def _record_location(pool, entity_id: int, file_path: str, backend: str,
                           object_key: str, size_bytes: int,
                           location: Optional[dict] = None, is_primary: bool = False):
    """Record ONE physical copy of a file in context_backend_locations. Upsert on
    the (entity, file, backend, server, bucket, endpoint) physical-tuple unique
    index so re-writing the same copy doesn't duplicate. The replicate policy
    writes the primary here AND each replica; single writes nothing here (the
    primary index alone is enough). Best-effort: a failure to record a replica's
    row never fails the write — the bytes are already stored, the worst case is a
    missing index row (the read path tolerates an unindexed replica by trying the
    primary)."""
    loc = location or {}
    server = loc.get("server") or None
    bucket = loc.get("bucket") or None
    endpoint = loc.get("endpoint") or None
    try:
        await pool.execute(
            "INSERT INTO context_backend_locations "
            "(entity_id, file_path, backend, server, bucket, endpoint, "
            " object_key, size_bytes, is_primary, updated_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now()) "
            "ON CONFLICT (entity_id, file_path, backend, "
            "             COALESCE(server,''), COALESCE(bucket,''), COALESCE(endpoint,'')) "
            "DO UPDATE SET object_key=$7, size_bytes=$8, is_primary=$9, updated_at=now()",
            entity_id, file_path, backend, server, bucket, endpoint,
            object_key, size_bytes, is_primary)
    except Exception as e:  # pragma: no cover - defensive
        log.warning("record location for %s failed: %s", file_path, e)


async def _read_locations(pool, entity_id: int, file_path: str) -> list[dict]:
    """All recorded physical copies of a file, PRIMARY FIRST (the read failover
    order). [] if none (→ the caller uses the primary index / pg)."""
    try:
        rows = await pool.fetch(
            "SELECT backend, server, bucket, endpoint, object_key, size_bytes, is_primary "
            "FROM context_backend_locations "
            "WHERE entity_id = $1 AND file_path = $2 "
            "ORDER BY is_primary DESC, updated_at DESC",
            entity_id, file_path)
    except Exception as e:
        log.debug("read locations for %s failed: %s", file_path, e)
        return []
    return [dict(r) for r in rows]


async def _delete_locations(pool, entity_id: int, file_path: str):
    try:
        await pool.execute(
            "DELETE FROM context_backend_locations WHERE entity_id = $1 AND file_path = $2",
            entity_id, file_path)
    except Exception as e:  # pragma: no cover - defensive
        log.debug("delete locations for %s failed: %s", file_path, e)


# ── Replicate write / read-failover (decentralization) ────────────────

async def store_replicated(pool, entity_id: int, file_path: str, content: str,
                           primary: "ContextBackend", *,
                           extra_targets: Optional[list[str]] = None,
                           host_cfg: Optional[dict] = None) -> dict:
    """Write `content` to the PRIMARY backend and fan it out to up to
    REPLICATE_EXTRA_COPIES additional ONLINE context-store bastions, recording
    EACH copy in context_backend_locations (is_primary on the primary). Returns the
    PRIMARY's store result ({object_key, size_bytes, location}) — the caller records
    that as the primary in context_backend_meta exactly as the single path does.

    Replica target selection:
      * `extra_targets` (explicit bastion hostnames from the route/host config) is
        honoured first, filtered to ONLINE context-store bastions.
      * otherwise auto-pick from list_context_store_bastions, excluding the primary
        bastion's own server (no point replicating onto the same box).

    CONSISTENCY CAVEAT (documented, v1): this is WRITE-TIME FAN-OUT only. There is
    NO background re-sync — a replica that is offline at write time simply isn't
    written (it's skipped, the write still succeeds as long as the PRIMARY wrote),
    so replicas can drift / be incomplete. A later read may therefore find fewer
    copies than the policy nominally wants. This is acceptable for v1 (the primary
    is always consistent; replicas are best-effort redundancy for the OFFLINE-read
    case). A reconciling sweeper is the follow-up.
    """
    # 1) PRIMARY write (this MUST succeed — its failure raises, no partial state).
    res = await primary.store(pool, entity_id, file_path, content)
    await _record_location(
        pool, entity_id, file_path, primary.name, res["object_key"],
        res["size_bytes"], location=res.get("location"), is_primary=True)

    # 2) Choose replica targets. Only bastions replicate (s3 is already durable; a
    #    pg primary doesn't replicate — it lives on the platform).
    if primary.name != "bastion":
        # Non-bastion primary: nothing to fan out to in v1 (the primary store is
        # itself redundant/durable). Record only the primary. (S3-cross-region
        # replication is a provider feature, out of scope here.)
        return res
    primary_server = getattr(primary, "server", None)
    choosable = await list_context_store_bastions(pool, entity_id)
    online_hosts = {b["hostname"] for b in choosable}
    targets: list[str] = []
    if extra_targets:
        for h in extra_targets:
            h = (h or "").strip()
            if h and h != primary_server and h in online_hosts and h not in targets:
                targets.append(h)
    else:
        for b in choosable:
            h = b["hostname"]
            if h != primary_server and h not in targets:
                targets.append(h)
    targets = targets[:max(0, REPLICATE_EXTRA_COPIES)]

    # 3) Fan out. Each replica is BEST-EFFORT — a failure is logged, never raised
    #    (the primary already holds the bytes). Record each successful copy.
    root = (host_cfg or {}).get("root") or "context/"
    for h in targets:
        try:
            rb = BastionBackend({"backend": "bastion", "server": h, "root": root})
            rres = await rb.store(pool, entity_id, file_path, content)
            await _record_location(
                pool, entity_id, file_path, "bastion", rres["object_key"],
                rres["size_bytes"], location=rres.get("location"), is_primary=False)
        except Exception as e:
            log.warning("replicate copy to '%s' for %s failed (skipped): %s",
                        h, file_path, e)
    return res


async def retrieve_with_failover(pool, entity_id: int, file_path: str,
                                 host_cfg: Optional[dict] = None) -> Optional[str]:
    """Read a file's bytes trying the PRIMARY first, then each replica, using the
    context_backend_locations table. Returns the content, or None if NO copy could
    be fetched (every location offline/missing) — never raises on an offline box.

    Used only when the file has >1 recorded location (replicate). For a single
    location the caller uses resolve_backend_for_file (the cheaper path)."""
    locs = await _read_locations(pool, entity_id, file_path)
    if not locs:
        return None
    host_cfg = host_cfg or await get_backend_config(pool, entity_id)
    for loc in locs:
        backend = loc.get("backend")
        try:
            if backend == "s3":
                cfg = {
                    "backend": "s3",
                    "bucket": loc.get("bucket") or host_cfg.get("bucket"),
                    "endpoint_url": loc.get("endpoint") or host_cfg.get("endpoint_url"),
                    "cred_name": host_cfg.get("cred_name") or "ctx:s3",
                    "region": host_cfg.get("region"),
                    "prefix": host_cfg.get("prefix"),
                }
                if not cfg["bucket"]:
                    continue
                b: ContextBackend = S3Backend(cfg)
            elif backend == "bastion":
                server = loc.get("server") or host_cfg.get("server")
                if not server:
                    continue
                b = BastionBackend({"backend": "bastion", "server": server,
                                    "root": host_cfg.get("root") or "context/"})
            else:
                continue
            fetched = await b.retrieve(pool, entity_id, file_path)
            if fetched is not None:
                return fetched
        except Exception as e:
            # This copy is offline/unreachable — try the next (failover, no crash).
            log.info("failover read of %s from %s '%s' failed, trying next: %s",
                     file_path, backend,
                     loc.get("server") or loc.get("bucket"), e)
            continue
    return None


# ── Reconcile (the follow-up to store_replicated's write-time caveat) ─
# store_replicated does WRITE-TIME FAN-OUT ONLY: a replica that was OFFLINE at
# write time is skipped, so a 'replicate' file can hold fewer copies than the
# policy wants. These two functions are the RECONCILING SWEEP that closes that gap
# AFTER the box comes back online — additive only (never deletes a copy), idempotent
# (a full-replica file is a no-op), best-effort per file (one file never stops the
# scan), and they ONLY touch files that already have a replicate footprint, so the
# pg / single-backend default path is completely unaffected.

async def list_replicate_file_paths(pool, entity_id: int,
                                    limit: int = 1000) -> list[str]:
    """The DISTINCT file_paths this entity has any context_backend_locations row
    for — i.e. the files written under a 'replicate' policy (the 'single' path
    writes nothing to that table and deletes its rows on overwrite, so a row's
    existence is the replicate marker). The sweep's candidate set. Best-effort:
    any failure (no table on a fresh pod, non-entity pod) returns [] so the sweep
    degrades to 'nothing to reconcile' rather than raising."""
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT file_path FROM context_backend_locations "
            "WHERE entity_id = $1 ORDER BY file_path LIMIT $2",
            entity_id, int(limit))
    except Exception as e:
        log.debug("list_replicate_file_paths failed for entity %d: %s", entity_id, e)
        return []
    return [r["file_path"] for r in rows]


async def reconcile_replicas(pool, entity_id: int, file_path: str) -> dict:
    """Bring ONE replicate file up to its intended replica set by ADDING any
    missing copies to now-online context-store bastions. Non-destructive (never
    deletes the primary or any existing copy), idempotent (a file already at full
    replica count is a no-op), and degrade-not-break (a read or a single replica
    write failing is logged, not raised). Returns a small status dict.

    Reconcile decision (mirrors store_replicated's target selection so the swept
    set matches a fresh replicate write):
      * Re-derive the file's CURRENT write plan from the route/host config
        (resolve_route_for_write). If it isn't 'replicate' any more, or its primary
        isn't a bastion, there is nothing to fan out — skip (no copies added).
      * Intended replica targets = explicit `replicas` (filtered to ONLINE
        context-store bastions) else auto-pick from the online set, excluding the
        primary's own server, capped at REPLICATE_EXTRA_COPIES — EXACTLY
        store_replicated's logic.
      * MISSING targets = intended targets that have no recorded location row for
        this file. If none are missing → no-op.
      * For each missing target: READ the bytes from a live copy
        (retrieve_with_failover — primary first, then any online replica), then
        WRITE via BastionBackend.store and record the new is_primary=False row. If
        the bytes can't be read from ANY copy, skip the whole file (log, never lose
        data — we don't delete or touch the primary)."""
    result = {"file_path": file_path, "added": 0, "skipped_reason": None}
    locs = await _read_locations(pool, entity_id, file_path)
    if not locs:
        result["skipped_reason"] = "no recorded locations (not a replicate file)"
        return result

    # Re-derive the CURRENT intent for this path (route/host availability config).
    try:
        plan = await resolve_route_for_write(pool, entity_id, file_path)
    except Exception as e:
        log.debug("reconcile: write-plan resolve failed for %s: %s", file_path, e)
        result["skipped_reason"] = "write-plan unresolved"
        return result
    if (plan.get("availability") or DEFAULT_AVAILABILITY) != "replicate":
        result["skipped_reason"] = "not replicate any more"
        return result
    if plan.get("name") != "bastion":
        # Only bastion primaries fan out (s3 is already durable; pg lives on the
        # platform) — same rule store_replicated enforces.
        result["skipped_reason"] = "primary is not a bastion"
        return result

    host_cfg = plan.get("cfg") or {}
    extra_targets = list(plan.get("replicas") or [])

    # The primary's server (from its location row) — never replicate onto it.
    primary_loc = next((l for l in locs if l.get("is_primary")), None)
    primary_server = (primary_loc or {}).get("server")
    # Servers that ALREADY hold a copy (any recorded bastion location).
    have_servers = {l.get("server") for l in locs
                    if l.get("backend") == "bastion" and l.get("server")}

    # Intended targets — store_replicated's exact selection.
    choosable = await list_context_store_bastions(pool, entity_id)
    online_hosts = {b["hostname"] for b in choosable}
    targets: list[str] = []
    if extra_targets:
        for h in extra_targets:
            h = (h or "").strip()
            if h and h != primary_server and h in online_hosts and h not in targets:
                targets.append(h)
    else:
        for b in choosable:
            h = b["hostname"]
            if h != primary_server and h not in targets:
                targets.append(h)
    targets = targets[:max(0, REPLICATE_EXTRA_COPIES)]

    # MISSING = intended online targets that don't already hold a copy. Idempotent:
    # a file already at full replica count has no missing target → no-op below.
    missing = [h for h in targets if h not in have_servers]
    if not missing:
        result["skipped_reason"] = "already at full replica set"
        return result

    # Read the bytes from a live copy (primary first, then any online replica).
    # If NO copy is readable, skip — we never delete or touch the primary, so the
    # file keeps whatever copies it has; we just can't add more this pass.
    content = None
    try:
        content = await retrieve_with_failover(pool, entity_id, file_path,
                                               host_cfg=host_cfg)
    except Exception as e:
        log.info("reconcile: failover read of %s failed: %s", file_path, e)
    if content is None:
        result["skipped_reason"] = "no readable copy (all offline) — skipped, no data lost"
        return result

    # Fan out to each missing target. Best-effort per replica (a single failure is
    # logged, never raised) — exactly store_replicated's replica loop, minus the
    # primary write (the primary already holds the bytes and is left untouched).
    root = host_cfg.get("root") or "context/"
    for h in missing:
        try:
            rb = BastionBackend({"backend": "bastion", "server": h, "root": root})
            rres = await rb.store(pool, entity_id, file_path, content)
            await _record_location(
                pool, entity_id, file_path, "bastion", rres["object_key"],
                rres["size_bytes"], location=rres.get("location"), is_primary=False)
            result["added"] += 1
        except Exception as e:
            log.warning("reconcile: replica copy to '%s' for %s failed (skipped): %s",
                        h, file_path, e)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Backend interface
# ─────────────────────────────────────────────────────────────────────────────
# A backend stores/retrieves the CONTENT bytes for an (entity_id, file_path).
# It never touches the metadata/index — the caller (ContextStore) owns that.

class ContextBackend:
    """Interface: store/retrieve/remove the content bytes for one file_path."""

    name = "base"

    async def store(self, pool, entity_id: int, file_path: str, content: str) -> dict:
        """Persist `content` for (entity_id, file_path). Returns
        {"object_key": str, "size_bytes": int, "location": {...}?} — location
        (v077) names WHICH machine/bucket holds the bytes: {"server": ...} for a
        bastion box, {"bucket": ..., "endpoint": ...} for s3. Raises on hard
        failure."""
        raise NotImplementedError

    async def retrieve(self, pool, entity_id: int, file_path: str) -> Optional[str]:
        """Return the content for (entity_id, file_path), or None if absent."""
        raise NotImplementedError

    async def remove(self, pool, entity_id: int, file_path: str) -> bool:
        """Delete the content for (entity_id, file_path). Returns True if removed."""
        raise NotImplementedError


# ── pg backend (DEFAULT) ──────────────────────────────────────────────────────
# The pg backend is INTENTIONALLY a no-op pass-through: ContextStore's existing
# SQL already writes/reads entity_context.content. This class exists so the
# selector has a uniform shape, but ContextStore SHORT-CIRCUITS to its original
# SQL when backend == 'pg' (see context_store.py) — so the default path runs the
# exact same code it always has, with ZERO new round-trips. These methods are a
# fallback only and operate directly on entity_context.content.

class PgBackend(ContextBackend):
    name = "pg"

    async def store(self, pool, entity_id: int, file_path: str, content: str) -> dict:
        return {"object_key": file_path, "size_bytes": len(content.encode("utf-8"))}

    async def retrieve(self, pool, entity_id: int, file_path: str) -> Optional[str]:
        row = await pool.fetchrow(
            "SELECT content FROM entity_context WHERE entity_id = $1 AND file_path = $2",
            entity_id, file_path)
        return row["content"] if row else None

    async def remove(self, pool, entity_id: int, file_path: str) -> bool:
        return True  # entity_context row deletion is the caller's job


# ── S3 backend ────────────────────────────────────────────────────────────────

class S3Backend(ContextBackend):
    """Store context content as S3 objects. Bucket + creds from the host vault.
    boto3 is lazy-imported inside the call (never at module load). Not billed by
    the platform: the bucket and its costs are the user's own."""

    name = "s3"

    def __init__(self, cfg: dict):
        self.bucket = cfg["bucket"]
        self.prefix = (cfg.get("prefix") or "").strip("/")
        self.region = cfg.get("region") or None
        self.endpoint_url = cfg.get("endpoint_url") or None
        self.cred_name = cfg.get("cred_name") or "ctx:s3"

    def _key(self, entity_id: int, file_path: str) -> str:
        # Namespaced by entity so one bucket can serve many hosts without collision.
        parts = [p for p in (self.prefix, f"entity-{entity_id}", file_path) if p]
        return "/".join(parts)

    async def _client(self, pool, entity_id: int):
        """Build a boto3 S3 client from vault-sealed creds. Creds are a JSON blob
        {"access_key_id","secret_access_key","session_token"?} sealed host-owned
        in the vault under self.cred_name. Lazy-imports boto3 + reads the vault
        in-pod; the agent never sees the raw bytes here either (they go straight
        into the client and stay in-process)."""
        from server.vault import api as vault_api  # in-pod, host-owned open
        raw = await vault_api.open_secret(pool, entity_id=entity_id, name=self.cred_name)
        if not raw:
            raise RuntimeError(
                f"s3 backend: no vault credential '{self.cred_name}' — seal one first "
                f"(keys_manage op='add' name='{self.cred_name}' "
                f"value='{{\"access_key_id\":\"...\",\"secret_access_key\":\"...\"}}')")
        try:
            creds = json.loads(raw.decode("utf-8"))
        except Exception:
            raise RuntimeError(f"s3 backend: vault credential '{self.cred_name}' is not valid JSON")
        import boto3  # LAZY — already in the image; never imported at module load
        session = boto3.session.Session(
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
            aws_session_token=creds.get("session_token") or None,
            region_name=self.region,
        )
        return session.client("s3", endpoint_url=self.endpoint_url)

    async def store(self, pool, entity_id: int, file_path: str, content: str) -> dict:
        import asyncio
        body = content.encode("utf-8")
        key = self._key(entity_id, file_path)
        client = await self._client(pool, entity_id)
        # boto3 is sync; run it off the event loop.
        await asyncio.to_thread(
            client.put_object, Bucket=self.bucket, Key=key, Body=body)
        return {"object_key": key, "size_bytes": len(body),
                "location": {"bucket": self.bucket, "endpoint": self.endpoint_url}}

    async def retrieve(self, pool, entity_id: int, file_path: str) -> Optional[str]:
        import asyncio
        meta = await _read_meta(pool, entity_id, file_path)
        key = meta["object_key"] if meta else self._key(entity_id, file_path)
        client = await self._client(pool, entity_id)
        try:
            resp = await asyncio.to_thread(
                client.get_object, Bucket=self.bucket, Key=key)
            body = await asyncio.to_thread(resp["Body"].read)
        except Exception as e:
            # A missing object reads as absent (None), not a hard error.
            if "NoSuchKey" in repr(e) or "404" in repr(e):
                return None
            raise
        return body.decode("utf-8", "replace")

    async def remove(self, pool, entity_id: int, file_path: str) -> bool:
        import asyncio
        meta = await _read_meta(pool, entity_id, file_path)
        key = meta["object_key"] if meta else self._key(entity_id, file_path)
        client = await self._client(pool, entity_id)
        await asyncio.to_thread(
            client.delete_object, Bucket=self.bucket, Key=key)
        return True

    # ── DCN cabinet reads (CN-1) — additive, raw-key surface ──────────────────
    # The DCN reads the USER's OWN objects (their filing habits), so these take the
    # raw object key, not the blob-vault's entity-namespaced file_path. Read-only:
    # the DCN surface has no delete; `remove` above belongs to the
    # blob-vault lifecycle and stays OFF the DCN surface.

    async def list_keys(self, pool, entity_id: int, key_prefix: str = "",
                        limit: int = 500) -> dict:
        """List objects under a key prefix. Returns {objects: [{key,size,mtime}],
        truncated}."""
        import asyncio
        client = await self._client(pool, entity_id)
        limit = max(1, min(int(limit or 500), 1000))
        base = self.prefix
        want = "/".join(p for p in (base, (key_prefix or "").strip("/")) if p)
        resp = await asyncio.to_thread(
            client.list_objects_v2, Bucket=self.bucket,
            **({"Prefix": want} if want else {}), MaxKeys=limit)
        objects = [{"key": o["Key"], "size": int(o.get("Size") or 0),
                    "mtime": o["LastModified"].isoformat() if o.get("LastModified") else None}
                   for o in resp.get("Contents") or []]
        return {"objects": objects, "truncated": bool(resp.get("IsTruncated"))}

    async def stat_key(self, pool, entity_id: int, key: str) -> Optional[dict]:
        """HEAD one object. None when absent (the caller's typed 'gone'), never a
        hard error for a missing key."""
        import asyncio
        client = await self._client(pool, entity_id)
        try:
            resp = await asyncio.to_thread(
                client.head_object, Bucket=self.bucket, Key=key)
        except Exception as e:
            if "404" in repr(e) or "NoSuchKey" in repr(e) or "Not Found" in repr(e):
                return None
            raise
        return {"size": int(resp.get("ContentLength") or 0),
                "mtime": resp["LastModified"].isoformat() if resp.get("LastModified") else None,
                "etag": (resp.get("ETag") or "").strip('"')}

    async def get_range(self, pool, entity_id: int, key: str, start: int,
                        end: int) -> Optional[bytes]:
        """Ranged GetObject — the DCN's ONE scoped range-read. Returns
        the bytes, or None when the key is absent (typed 'gone' upstream). An
        InvalidRange past EOF returns b'' (a valid empty read, not an error)."""
        import asyncio
        client = await self._client(pool, entity_id)
        start = max(0, int(start)); end = max(start, int(end))
        try:
            resp = await asyncio.to_thread(
                client.get_object, Bucket=self.bucket, Key=key,
                Range=f"bytes={start}-{max(start, end - 1)}")
            body = await asyncio.to_thread(resp["Body"].read)
        except Exception as e:
            text = repr(e)
            if "NoSuchKey" in text or "404" in text:
                return None
            if "InvalidRange" in text:
                return b""
            raise
        return body


# ── Bastion (home-box) backend ────────────────────────────────────────────────

class BastionBackend(ContextBackend):
    """Store/retrieve context content on the user's HOME BASTION — a box the user
    owns that joined their mesh. USER-OWNED: the host retrieves on demand over the
    mesh and NEVER persists the bytes itself (no local copy is kept).

    HOST SIDE (built here): dispatches storage ops over the EXISTING bastion
    management transport — server.network.nac_mgmt.run_mgmt_op(pool, eid, server,
    op, args) — which streams (stdout, stderr, exit) back from the bastion's
    management worker. Content rides as base64 in args (store) / stdout (retrieve)
    for binary safety.

    ───────────────────────────────────────────────────────────────────────────
    DESIGN FORK — BASTION-SIDE STORAGE PROTOCOL (flagged, not built here):

    The bastion's management worker today runs SHELL ops (the mgmt-allowlist of
    named ops the user signed a grant for). There is NO storage op on the bastion
    side yet. Two ways to close it:

      (A) New bastion-side handler (RECOMMENDED): teach the bastion worker three
          dedicated ops — ctx_put / ctx_get / ctx_del — that write/read/remove a
          file under a sandboxed context root the user controls. Clean contract,
          no shell, base64 payloads, the user grants exactly these three ops.
          This is a SEPARATE BUILD in the bastion agent repo (NOT this repo) — the
          ops below assume these op names.

      (B) Reuse shell ops over the allowlist: implement ctx_put as e.g.
          `tee <root>/<path>` and ctx_get as `cat <root>/<path>`. No bastion build,
          but it leans on shell + the user granting cat/tee + path-injection care.

    This module emits the (A) op names; if the deployment only has (B), the
    bastion worker maps the op names to the shell equivalents. Either way the
    bastion-side piece is OUTSIDE this repo and must be built/granted there.
    ───────────────────────────────────────────────────────────────────────────
    """

    name = "bastion"

    # Op names the host dispatches. The bastion worker must recognise these
    # (fork A) or map them to shell (fork B).
    OP_PUT = "ctx_put"
    OP_GET = "ctx_get"
    OP_DEL = "ctx_del"

    def __init__(self, cfg: dict):
        self.server = cfg["server"]
        self.root = (cfg.get("root") or "context/").strip()

    def _remote_path(self, entity_id: int, file_path: str) -> str:
        root = self.root.rstrip("/")
        return f"{root}/entity-{entity_id}/{file_path}"

    async def _dispatch(self, pool, entity_id: int, op: str, args: list) -> tuple[int, str, str]:
        from server.network import nac_mgmt
        res = await nac_mgmt.run_mgmt_op(pool, entity_id, self.server, op, args)
        return res.get("exit", 1), res.get("stdout", ""), res.get("stderr", "")

    async def store(self, pool, entity_id: int, file_path: str, content: str) -> dict:
        rpath = self._remote_path(entity_id, file_path)
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        exit_code, _out, err = await self._dispatch(
            pool, entity_id, self.OP_PUT, [rpath, b64])
        if exit_code is None:
            raise RuntimeError(f"bastion store on '{self.server}' is waiting on your approval: {err[:200]}")
        if exit_code != 0:
            raise RuntimeError(
                f"bastion store failed (exit {exit_code}) on '{self.server}': {err[:200]} "
                f"— the bastion may not implement the '{self.OP_PUT}' op (see the "
                f"BASTION-SIDE STORAGE PROTOCOL fork) or the grant may not cover it")
        return {"object_key": rpath, "size_bytes": len(content.encode("utf-8")),
                "location": {"server": self.server}}

    async def retrieve(self, pool, entity_id: int, file_path: str) -> Optional[str]:
        rpath = self._remote_path(entity_id, file_path)
        exit_code, out, err = await self._dispatch(
            pool, entity_id, self.OP_GET, [rpath])
        if exit_code == 2:  # convention: 2 == not found on the bastion
            return None
        if exit_code != 0:
            raise RuntimeError(
                f"bastion retrieve failed (exit {exit_code}) on '{self.server}': {err[:200]}")
        try:
            return base64.b64decode(out.strip()).decode("utf-8", "replace")
        except Exception:
            # If the bastion returns raw text instead of base64, pass it through.
            return out

    async def remove(self, pool, entity_id: int, file_path: str) -> bool:
        rpath = self._remote_path(entity_id, file_path)
        exit_code, _out, _err = await self._dispatch(
            pool, entity_id, self.OP_DEL, [rpath])
        return exit_code in (0, 2)  # gone or already-absent both count as removed


# ─────────────────────────────────────────────────────────────────────────────
# Resolver
# ─────────────────────────────────────────────────────────────────────────────

def make_backend(cfg: dict) -> ContextBackend:
    """Build the backend object for a config dict. Unknown/garbled -> pg."""
    backend = (cfg or {}).get("backend", DEFAULT_BACKEND)
    if backend == "s3":
        return S3Backend(cfg)
    if backend == "bastion":
        return BastionBackend(cfg)
    return PgBackend()


async def resolve_backend(pool, entity_id: int) -> tuple[str, ContextBackend]:
    """Read this host's selection and build the backend. Returns (name, backend)."""
    cfg = await get_backend_config(pool, entity_id)
    b = make_backend(cfg)
    return b.name, b


# ─────────────────────────────────────────────────────────────────────────────
# user_context parity (Part 4) — store the USER-OWNED corpus's content bytes on
# the same pluggable backends (s3 / home bastion), indexed in context_backend_meta
# under a synthetic file_path so the EXACT same store/retrieve/remove machinery is
# reused. pg stays the default (content lives inline in user_context.content,
# unchanged). Only non-pg user_context content goes external.
# ─────────────────────────────────────────────────────────────────────────────

# Synthetic file_path namespace for a user_context row's external bytes. Keyed by
# the row id so it never collides with an entity_context path (those are caller
# paths like 'memory/...').
#
# NO NUL BYTE. This was "\x00user_context\x00/" and it is bound as a parameter
# into context_backend_meta.file_path (:541-550), a TEXT column — PostgreSQL
# rejects \x00 in a text value, so every write of this key failed and the
# user_context external lane has never worked. Reproduced through asyncpg, the
# driver this code uses, before changing it.
#
# The byte also could not survive the home-bastion path in any case: the
# receiving handler rejects any path containing it before touching the
# filesystem (the gateway's ctx handler, cleanRel), so ctx_put
# exits non-zero and BastionBackend.store raises. Nothing has ever been stranded
# on a user's machine.
#
# U+241E (␞) is printable, stores fine, and cannot appear in a caller path.
#
# NOTHING TO MIGRATE: no row carries the old prefix, because no write of it could
# commit. UNVERIFIED and deliberately left so: whether cloud object storage would
# have accepted \x00 in an OBJECT KEY. S3Backend.store writes the object and
# books a charge (:984-994) BEFORE the index write that fails, so if it did
# accept the byte, an orphan object plus a charge was possible. No host is
# configured with cloud storage, so there was nothing to test against.
_USER_CONTEXT_KEY_PREFIX = "␞user_context␞/"


def user_context_key(row_id: int) -> str:
    """The synthetic context_backend_meta file_path for a user_context row's bytes."""
    return f"{_USER_CONTEXT_KEY_PREFIX}{int(row_id)}"


async def store_user_context_content(pool, entity_id: int, row_id: int,
                                     content: str) -> Optional[str]:
    """Store ONE user_context row's content on the host's current backend (if
    non-pg), recording its location in context_backend_meta under the synthetic
    key. Returns the backend name ('s3'|'bastion') if the bytes went external, or
    None if pg (→ the caller keeps content inline, the default, unchanged). Raises
    on a hard external-store failure (the caller leaves the row pg, no half-write).

    user_context is corpus content the USER owns — the v076 intent named the
    home bastion as where users store their context; this is the wiring that lets
    a user_context row actually live there (entity_context already had it)."""
    name, backend = await resolve_backend(pool, entity_id)
    if name == "pg":
        return None
    key = user_context_key(row_id)
    res = await backend.store(pool, entity_id, key, content)
    await _record_meta(pool, entity_id, key, backend.name,
                       res["object_key"], res["size_bytes"],
                       location=res.get("location"))
    return backend.name


async def retrieve_user_context_content(pool, entity_id: int, row_id: int,
                                        backend_name: Optional[str]) -> Optional[str]:
    """Rehydrate a user_context row's external content. backend_name is the row's
    storage_backend marker. Returns the bytes, None if pg (caller uses the inline
    content) or if the external bytes can't be fetched (offline → graceful None,
    never raises). Mirrors ContextStore.get's per-file resolve + failover."""
    if not backend_name or backend_name == "pg":
        return None
    key = user_context_key(row_id)
    try:
        backend = await resolve_backend_for_file(pool, entity_id, key)
        if backend is not None:
            fetched = await backend.retrieve(pool, entity_id, key)
            if fetched is not None:
                return fetched
    except Exception as e:
        log.info("user_context %s primary retrieve failed: %s", row_id, e)
    # Failover to any recorded replica (if a replicate route covered this key).
    try:
        return await retrieve_with_failover(pool, entity_id, key)
    except Exception as e:
        log.debug("user_context %s failover retrieve failed: %s", row_id, e)
        return None


async def remove_user_context_content(pool, entity_id: int, row_id: int,
                                      backend_name: Optional[str]) -> None:
    """Best-effort delete of a user_context row's external bytes + its index rows."""
    if not backend_name or backend_name == "pg":
        return
    key = user_context_key(row_id)
    try:
        backend = await resolve_backend_for_file(pool, entity_id, key)
        if backend is not None:
            await backend.remove(pool, entity_id, key)
    except Exception as e:
        log.warning("user_context %s external remove failed: %s", row_id, e)
    try:
        await _delete_meta(pool, entity_id, key)
        await _delete_locations(pool, entity_id, key)
    except Exception as e:
        log.debug("user_context %s index cleanup failed: %s", row_id, e)


async def resolve_backend_for_file(pool, entity_id: int,
                                   file_path: str) -> Optional[ContextBackend]:
    """Build the backend that holds THIS file's bytes, from the host-local index
    (context_backend_meta), NOT the host's current host-wide selection (v077).

    This is the per-file retrieve path: a file keeps the backend + physical
    location it was WRITTEN with, recorded in the index, so a read stays correct
    even after the host switches its host-wide backend (the marker-leak fix).

    Returns None when there is no index row (→ the file is pg-resident, read it
    straight from entity_context) OR when the recorded backend is 'pg'.

    The file's PHYSICAL LOCATION (bucket/endpoint/server + object_key) comes from
    the index row; the host-level CREDENTIAL + region come from the host's current
    backend config (the vault cred_name / region are host-owned config, not a
    per-file property). S3Backend.retrieve / BastionBackend.retrieve read the
    object_key back from the index, so prefix/root aren't needed to read.
    """
    meta = await _read_meta(pool, entity_id, file_path)
    if not meta:
        return None
    backend = meta.get("backend")
    if not backend or backend == "pg":
        return None
    host_cfg = await get_backend_config(pool, entity_id)
    if backend == "s3":
        cfg = {
            "backend": "s3",
            # physical location of THIS file (from the index)
            "bucket": meta.get("bucket") or host_cfg.get("bucket"),
            "endpoint_url": meta.get("endpoint") or host_cfg.get("endpoint_url"),
            # host-level credential + region (from the host's current config)
            "cred_name": host_cfg.get("cred_name") or "ctx:s3",
            "region": host_cfg.get("region"),
            "prefix": host_cfg.get("prefix"),
        }
        if not cfg["bucket"]:
            # No recorded bucket and no host bucket — can't locate the bytes.
            log.warning("context retrieve: s3 file %s has no recorded bucket and "
                        "the host has no s3 bucket configured", file_path)
            return None
        return S3Backend(cfg)
    if backend == "bastion":
        server = meta.get("server") or host_cfg.get("server")
        if not server:
            log.warning("context retrieve: bastion file %s has no recorded server "
                        "and the host has no bastion server configured", file_path)
            return None
        return BastionBackend({"backend": "bastion", "server": server,
                               "root": host_cfg.get("root") or "context/"})
    return None
