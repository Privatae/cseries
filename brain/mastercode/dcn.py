"""
DCN — the Distributed Context Network route map (CN-0 groundwork).

The route map is the index layer over the USER's own stores — directories on
machines they run (machine class), buckets they own (cabinet class), and the
platform-resident data they created on / imported to C-Series (platform class).
It answers "what context exists, where it physically lives, and who may touch
it".

This module is deliberately NOT a placement backend. Placement
(context_backends.py: VALID_BACKENDS, ContextBackend store/retrieve/REMOVE)
answers "where do the HOST's context bytes go" and its contract includes
delete. The DCN surface has no delete operation, by design — so 'dcn'
never enters the placement vocabulary, and the two layers meet only in the
read-side view this module composes ("two systems, one route map view").

Tier honesty: machine-class stores are locators only — the schema has no
preview column to fill (see migrations/v091, trigger-enforced). Cabinet and
platform classes carry previews.

CN-0 ships the vocabulary: tables exist (inert), and list_stores() composes the
store-class view from grant rows PLUS derived cards — absorbed portal shared
folders (the pre-mesh bind-mount concept this vocabulary supersedes) and the
already-configured S3 placement targets surfaced as the filing cabinet
("S3 out of hiding"). CN-1 writes real grant rows; CN-2
writes files/chunks.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger("cseries.mastercode.dcn")

# The per-class index tier — a FIXED property of the class, never per-file.
# Rendered verbatim on every store card.
TIER_BY_CLASS = {
    "machine": "locations only",
    "cabinet": "locations + previews",
    "platform": "locations + previews",
}

# The DCN tables, the preview-tier guard function and its trigger are all
# declared in migrations/v091_dcn_route_map.sql (tables :39 onward, function
# :143, trigger :164) and applied at boot. This module carried a defensive
# mirror of every one of them, calling itself self-healing, and it was
# removed. Measured before deleting: all six tables are present on all 8
# live hosts with shapes IDENTICAL to the declared schema — columns, types,
# nullability, defaults, indexes and constraints — and pg_proc/pg_trigger show
# the guard function and trigger on every one of them too. The mirror had
# nothing left to heal, and it was DDL on a request path (list_stores is
# reached from server/routes/context_manager.py), which is the whole reason
# the kernel's database role could not be narrowed.


def _store_card(row) -> dict[str, Any]:
    """One granted store row → the card the Context Manager / tools render."""
    mask = row["mask"]
    if isinstance(mask, str):
        try:
            mask = json.loads(mask)
        except (json.JSONDecodeError, TypeError):
            mask = []
    return {
        "id": int(row["id"]),
        "class": row["class"],
        "tier": TIER_BY_CLASS.get(row["class"], "locations only"),
        "server": row["server"],
        "root": row["root"],
        "bucket": row["bucket"],
        "prefix": row["prefix"],
        "endpoint": row["endpoint"],
        "mask": mask,
        "sensitive": bool(row["sensitive"]),
        "sweep": row["sweep"],
        "status": row["status"],
        "display_name": row["display_name"],
        "legacy_portal_id": row["legacy_portal_id"],
        "derived": False,
    }


async def _granted_stores(pool, entity_id: int) -> list[dict[str, Any]]:
    try:
        rows = await pool.fetch(
            "SELECT * FROM dcn_stores WHERE entity_id = $1 AND status != 'revoked' "
            "ORDER BY class, display_name NULLS LAST, id", entity_id)
    except Exception as e:
        log.info("dcn: granted-store read failed: %s", e)
        return []
    cards = [_store_card(r) for r in rows]
    await annotate_freeagent(pool, cards)
    return cards


async def annotate_freeagent(pool, cards) -> None:
    """Managed bastion agent (v118): the bastion installs and runs a bastion agent for
    every machine store whose grant carries 'search' and reports, per store,
    what state it is in. Adds to each machine card: `freeagent` (bool = serving
    now) and `freeagent_state` (the bastion's {state, serving, version, detail}
    or None), so a card can say the one thing to fix ("needs Python") without
    a probe. Only heartbeats from the last 10 minutes count — a silent box
    reports nothing, not stale truth. Shared by list_stores (the tool/route-map
    view) and nac_commit.dcn_stores_list (the Context Manager grant view)."""
    try:
        hb = await pool.fetch(
            "SELECT hostname, capabilities, freeagent_stores FROM network_bastion_heartbeat "
            "WHERE last_seen_at > now() - interval '10 minutes'")
    except Exception as e:
        log.info("dcn: freeagent state read failed: %s", e)
        return
    by_host = {}
    for h in hb:
        caps, stores = h["capabilities"], h["freeagent_stores"]
        if isinstance(caps, str):
            try:
                caps = json.loads(caps)
            except (json.JSONDecodeError, TypeError):
                caps = []
        if isinstance(stores, str):
            try:
                stores = json.loads(stores)
            except (json.JSONDecodeError, TypeError):
                stores = {}
        by_host[h["hostname"]] = (caps if isinstance(caps, list) else [],
                                  stores if isinstance(stores, dict) else {})
    for c in cards:
        if c.get("class") != "machine" or c.get("id") is None:
            continue
        caps, stores = by_host.get(c.get("server"), ([], {}))
        st = stores.get(str(c["id"]))
        c["freeagent"] = bool(st and st.get("serving")) or ("freeagent" in caps and not stores)
        c["freeagent_state"] = st


def _absorbed_portal_stores(superseded_ids: set[str]) -> list[dict[str, Any]]:
    """Portal shared folders (server/portal/sources.py — the pre-mesh bind-mount
    grants) rendered as legacy machine-class store cards. Absorbed into one
    folder-grant vocabulary. A portal source vanishes
    from this list the moment a real dcn_stores row supersedes it
    (legacy_portal_id) — CN-1's upgrade path."""
    try:
        from server.portal import sources as portal_sources
        portal = portal_sources.list_sources()
    except Exception as e:
        log.info("dcn: portal absorption read failed: %s", e)
        return []
    out = []
    for s in portal:
        if s.get("id") in superseded_ids or s.get("type") != "folder":
            continue
        out.append({
            "id": None,
            "class": "machine",
            "tier": TIER_BY_CLASS["machine"],
            "server": "host machine",          # bind-mount era: the compose host
            "root": s.get("host_path"),
            "mask": ["read"],                   # live-read was all portal offered
            "sensitive": False,
            "sweep": None,                      # no indexer — that's WHY it's legacy
            "status": "legacy",
            "legacy_status": s.get("status"),
            "legacy_portal_id": s.get("id"),
            "mounted": bool(s.get("mounted")),
            "display_name": s.get("display_name") or s.get("name") or s.get("id"),
            "derived": True,
        })
    return out


async def _derived_cabinet_stores(pool, entity_id: int,
                                  granted: list[dict]) -> list[dict[str, Any]]:
    """The filing cabinet, out of hiding: every S3 target the placement
    layer already knows — the host-wide selection, per-prefix routes, and any
    bucket actually holding bytes (context_backend_meta) — surfaced as cabinet
    cards. indexed=False until a CN-1 grant + CN-2 sweep make it a real store."""
    from brain.mastercode import context_backends

    granted_keys = {(g.get("bucket"), g.get("endpoint") or None, g.get("prefix") or "")
                    for g in granted if g["class"] == "cabinet"}
    seen: dict[tuple, dict[str, Any]] = {}

    def _add(bucket: Optional[str], endpoint: Optional[str], prefix: str,
             cred_name: Optional[str]) -> None:
        if not bucket:
            return
        key = (bucket, endpoint or None, prefix or "")
        if key in granted_keys or key in seen:
            return
        seen[key] = {
            "id": None,
            "class": "cabinet",
            "tier": TIER_BY_CLASS["cabinet"],
            "bucket": bucket,
            "prefix": prefix or "",
            "endpoint": endpoint or None,
            "cred_name": cred_name or "ctx:s3",
            "status": "unindexed",
            "indexed": False,
            "files": 0,
            "bytes": 0,
            "display_name": bucket if not prefix else f"{bucket}/{prefix}",
            "derived": True,
        }

    try:
        cfg = await context_backends.get_backend_config(pool, entity_id)
        if (cfg or {}).get("backend") == "s3":
            _add(cfg.get("bucket"), cfg.get("endpoint_url"), cfg.get("prefix") or "",
                 cfg.get("cred_name"))
    except Exception as e:
        log.info("dcn: cabinet cfg read failed: %s", e)
    try:
        for route in await context_backends.get_routes(pool, entity_id):
            if route.get("backend") == "s3":
                _add(route.get("bucket"), route.get("endpoint_url"),
                     route.get("prefix_key") or "", route.get("cred_name"))
    except Exception as e:
        log.info("dcn: cabinet routes read failed: %s", e)
    # Buckets that HOLD bytes count even if no config points at them anymore
    # (files keep the backend they were written with — v077).
    try:
        rows = await pool.fetch(
            "SELECT bucket, endpoint, count(*)::int AS files, "
            "       COALESCE(sum(size_bytes),0)::bigint AS bytes "
            "FROM context_backend_meta "
            "WHERE entity_id = $1 AND backend = 's3' AND bucket IS NOT NULL "
            "GROUP BY bucket, endpoint", entity_id)
        for r in rows:
            key = (r["bucket"], r["endpoint"] or None, "")
            if key in seen:
                seen[key]["files"] = int(r["files"])
                seen[key]["bytes"] = int(r["bytes"])
            else:
                _add(r["bucket"], r["endpoint"], "", None)
                if key in seen:
                    seen[key]["files"] = int(r["files"])
                    seen[key]["bytes"] = int(r["bytes"])
    except Exception as e:
        log.info("dcn: cabinet usage read failed: %s", e)
    return list(seen.values())


async def list_stores(pool, entity_id: int) -> list[dict[str, Any]]:
    """The one route-map view: granted stores + derived cards
    (absorbed portal folders, unindexed cabinets, the platform class). Every
    card carries its class and its fixed tier line."""
    granted = await _granted_stores(pool, entity_id)
    superseded = {g.get("legacy_portal_id") for g in granted if g.get("legacy_portal_id")}
    stores = list(granted)
    stores += _absorbed_portal_stores(superseded)
    stores += await _derived_cabinet_stores(pool, entity_id, granted)
    stores.append({
        "id": None,
        "class": "platform",
        "tier": TIER_BY_CLASS["platform"],
        "status": "live",
        "indexed": True,          # write-through: indexed at write
        "display_name": "Platform",
        "derived": True,
    })
    return stores


async def list_cabinet_stores(pool, entity_id: int) -> list[dict[str, Any]]:
    """Cabinet cards only — the context_backend op='cabinets' surface."""
    return [s for s in await list_stores(pool, entity_id) if s["class"] == "cabinet"]
