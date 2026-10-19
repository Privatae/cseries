"""
Context Store — Persistent memory per Mastercode entity.

Each entity gets a virtual filesystem of context files stored in the
entity_context table. This supports:
  - Conversation summaries (context/conversations/latest.md)
  - User preferences (context/preferences.json)
  - Goal progress (context/goals/current.json)
  - Session notes (context/sessions/<timestamp>.json)
  - Learned facts (context/facts.json)

The runtime reads relevant context files when building prompts
and writes new context after interactions.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

log = logging.getLogger("cseries.mastercode.context_store")


# Pointer marker left in entity_context.content when the bytes live in a non-pg
# backend (S3 / home bastion). The metadata/index row stays in pg; the real bytes
# live in the selected backend, located via context_backend_meta.
#
# NO NUL BYTE — see the matching note in server/routes/context.py. entity_context
# .content is TEXT (init-db.sql:437) and PostgreSQL rejects \x00 in a text value,
# so the old "\x00__context_external__\x00" could never commit and this lane has
# never worked either.
#
# THIS LANE FAILS IN THE WORSE ORDER, which is why it is fixed in the same pass:
# store() writes the bytes to the backend and records the location row (:179-190)
# BEFORE the pg write at :192-199. So each attempt left bytes on the backend plus
# an index row pointing at them, and no database row — a leak, not just a failure.
_EXTERNAL_MARKER = "␞__context_external__␞"


class ContextStore:
    """Persistent memory for a single entity.

    The CONTENT-bytes layer is PLUGGABLE (Phase 7): a host may
    select an 's3' or 'bastion' backend per behavioral_params.context_backend.
    The DEFAULT 'pg' backend is the original behaviour and runs the ORIGINAL SQL
    unchanged (no new round-trip, no metering, no migration dependency). The
    metadata/index (entity_context row + content_type + metadata + updated_at)
    always stays in pg, so list/search/status are unaffected by the backend.
    """

    def __init__(self, pool: asyncpg.Pool, entity_id: int):
        self.pool = pool
        self.entity_id = entity_id

    async def _backend(self):
        """Resolve this host's content backend (cached per call). Any failure
        degrades to None, which means 'use the original pg SQL path'."""
        try:
            from brain.mastercode import context_backends
            name, backend = await context_backends.resolve_backend(self.pool, self.entity_id)
            if name == "pg":
                return None  # original SQL path — zero change
            return backend
        except Exception as e:  # any resolver failure => safe pg default
            log.debug("context backend resolve failed (%s) — using pg", e)
            return None

    async def get(self, file_path: str) -> Optional[str]:
        """Read a context file."""
        row = await self.pool.fetchrow(
            "SELECT content FROM entity_context "
            "WHERE entity_id = $1 AND file_path = $2",
            self.entity_id, file_path,
        )
        if row is None:
            return None
        content = row["content"]
        # Non-pg backend: the pg row is a POINTER marker; the real bytes live in an
        # external store. Resolve the backend FROM THE PER-FILE INDEX (the backend +
        # physical location this file was WRITTEN with), NOT the host's current
        # host-wide selection — otherwise, after the host switches backends, get()
        # would consult the wrong backend (or None) and leak the raw marker instead
        # of the bytes (the v077 marker-leak fix). If the index can't resolve the
        # file (no row / unbuildable), fall back to the host-wide backend, and as a
        # last resort never return the internal marker to the caller.
        if content == _EXTERNAL_MARKER:
            from brain.mastercode import context_backends
            backend = None
            try:
                backend = await context_backends.resolve_backend_for_file(
                    self.pool, self.entity_id, file_path)
            except Exception as e:
                log.debug("per-file backend resolve failed for %s (%s)", file_path, e)
            if backend is None:
                # No usable per-file index → fall back to the host-wide backend
                # (covers a legacy row written before the index recorded location).
                backend = await self._backend()
            if backend is not None:
                try:
                    fetched = await backend.retrieve(self.pool, self.entity_id, file_path)
                    if fetched is not None:
                        return fetched
                except Exception as e:
                    # The PRIMARY location is offline/unreachable — don't crash; the
                    # failover below (if there are replicas) gets a chance.
                    log.info("primary retrieve of %s failed, trying replicas: %s",
                             file_path, e)
            # Primary missing/offline → if this file was REPLICATED, fail over to a
            # replica copy via the locations table. retrieve_with_failover returns
            # None (not raise) when every copy is unreachable — graceful offline.
            try:
                failover = await context_backends.retrieve_with_failover(
                    self.pool, self.entity_id, file_path)
                if failover is not None:
                    return failover
            except Exception as e:
                log.debug("failover retrieve failed for %s (%s)", file_path, e)
            # Couldn't fetch the external bytes from any copy — return None rather
            # than leak the internal pointer marker to the caller.
            return None
        return content

    async def get_json(self, file_path: str) -> Optional[dict]:
        """Read a context file as parsed JSON."""
        content = await self.get(file_path)
        if content is None:
            return None
        try:
            return json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return None

    async def put(
        self,
        file_path: str,
        content: str,
        content_type: str = "text/plain",
        metadata: Optional[dict] = None,
    ):
        """Write a context file (upsert).

        Backend is chosen PER-PREFIX (Phase 8): the file's path is matched against
        the host's context_routes (longest-prefix-first); no match falls back to the
        host-wide context_backend, which falls back to pg. So different context can
        live on different machines.

        Default (pg) backend: the original single-statement upsert, unchanged.
        s3 / bastion backend: the bytes go to the external store first (and the
        location is recorded in context_backend_meta); the pg row holds a pointer
        marker so list/search/status keep working. If the external store fails the
        whole put RAISES — no half-written state.

        Availability (Phase 8): a route/host may set availability='replicate' to fan
        the bytes out to >1 online context-store bastion at write time (each copy is
        recorded in context_backend_locations; reads fail over primary->replicas).
        'single' (default) is exactly the one-location behaviour above.
        """
        from brain.mastercode import context_backends
        # Resolve the WRITE plan (backend + availability + replica targets) by the
        # file's prefix. Any resolver failure degrades to the safe pg default.
        plan = None
        try:
            plan = await context_backends.resolve_route_for_write(
                self.pool, self.entity_id, file_path)
        except Exception as e:
            log.debug("write-route resolve failed for %s (%s) — using pg", file_path, e)
        backend = None
        availability = "single"
        replicas: list = []
        host_cfg = None
        if plan is not None and plan.get("name") != "pg":
            backend = plan["backend"]
            availability = plan.get("availability") or "single"
            replicas = plan.get("replicas") or []
            host_cfg = plan.get("cfg")

        stored_content = content
        if backend is not None:
            if availability == "replicate":
                # Fan out to the primary + online replicas; the primary store result
                # is what the primary index records (same as the single path). Each
                # copy (incl. primary) is recorded in context_backend_locations.
                res = await context_backends.store_replicated(
                    self.pool, self.entity_id, file_path, content, backend,
                    extra_targets=replicas, host_cfg=host_cfg)
            else:
                res = await backend.store(self.pool, self.entity_id, file_path, content)
                # 'single' overwrite: drop any stale replica rows from a prior
                # replicate write so the read path doesn't fail over to dead copies.
                await context_backends._delete_locations(
                    self.pool, self.entity_id, file_path)
            # v077: record WHICH machine/bucket holds the bytes (res["location"])
            # alongside the backend + object_key + size, so the host-local index
            # can map each file to its physical location even after a backend switch.
            await context_backends._record_meta(
                self.pool, self.entity_id, file_path,
                backend.name, res["object_key"], res["size_bytes"],
                location=res.get("location"))
            stored_content = _EXTERNAL_MARKER  # pg row becomes a pointer
        await self.pool.execute("""
            INSERT INTO entity_context (entity_id, file_path, content, content_type, metadata, updated_at)
            VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
            ON CONFLICT (entity_id, file_path) DO UPDATE SET
                content = $3, content_type = $4, metadata = $5::jsonb, updated_at = NOW()
        """,
            self.entity_id, file_path, stored_content, content_type,
            json.dumps(metadata) if metadata else None,
        )

    async def put_json(
        self,
        file_path: str,
        data: dict,
        metadata: Optional[dict] = None,
    ):
        """Write a context file as JSON."""
        await self.put(
            file_path,
            json.dumps(data, default=str),
            content_type="application/json",
            metadata=metadata,
        )

    async def delete(self, file_path: str) -> bool:
        """Delete a context file (and its external bytes, if any)."""
        # Remove the external bytes first (best-effort), then the pg row + meta.
        backend = await self._backend()
        if backend is not None:
            try:
                await backend.remove(self.pool, self.entity_id, file_path)
                from brain.mastercode import context_backends
                await context_backends._delete_meta(self.pool, self.entity_id, file_path)
                # Drop any replica copies' index rows too (best-effort — the bytes on
                # an offline replica box are cleaned up on its next reachable delete).
                await context_backends._delete_locations(self.pool, self.entity_id, file_path)
            except Exception as e:
                log.warning("context backend remove failed for %s: %s", file_path, e)
        result = await self.pool.execute(
            "DELETE FROM entity_context WHERE entity_id = $1 AND file_path = $2",
            self.entity_id, file_path,
        )
        return result.split()[-1] != "0"

    async def list_files(self, prefix: str = "") -> list[dict]:
        """List context files, optionally filtered by path prefix."""
        if prefix:
            rows = await self.pool.fetch(
                "SELECT file_path, content_type, metadata, updated_at "
                "FROM entity_context "
                "WHERE entity_id = $1 AND file_path LIKE $2 "
                "ORDER BY updated_at DESC",
                self.entity_id, f"{prefix}%",
            )
        else:
            rows = await self.pool.fetch(
                "SELECT file_path, content_type, metadata, updated_at "
                "FROM entity_context "
                "WHERE entity_id = $1 ORDER BY updated_at DESC",
                self.entity_id,
            )
        return [dict(r) for r in rows]

    async def append_to_list(self, file_path: str, item: dict, max_items: int = 100):
        """Append an item to a JSON list context file (capped at max_items)."""
        existing = await self.get_json(file_path) or []
        if not isinstance(existing, list):
            existing = [existing]
        existing.append(item)
        if len(existing) > max_items:
            existing = existing[-max_items:]
        await self.put_json(file_path, existing)
