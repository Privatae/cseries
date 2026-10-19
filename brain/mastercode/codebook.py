"""
MastercodeBook — the Host's registry row and its permission grants.

What is left: reads of entity_registry, activation, the entity_permissions grants and the
behavioral-log reader. The `mastercode_book` table, the drift envelope with
its auto-reset and suspension, the public-directive enforcement and the
`entity_interactions` writer went with the simulated psyche.
"""

import json
import logging
from typing import Optional

import asyncpg

from mastercode.inference_config import DEFAULT_BEHAVIORAL_PARAMS

log = logging.getLogger("cseries.mastercode")


class MastercodeBook:
    """The active governance document for all entities."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    # ------------------------------------------------------------------
    # Activation
    # ------------------------------------------------------------------

    async def activate_entity(self, entity_id: int) -> dict:
        """Transition entity from enrolled to active."""
        row = await self.pool.fetchrow("""
            UPDATE entity_registry SET
                status = 'active',
                activated_at = NOW(),
                last_active_at = NOW()
            WHERE id = $1 AND status = 'enrolled'
            RETURNING id, entity_uuid, display_name, status
        """, entity_id)
        if not row:
            raise ValueError(f"Host #{entity_id} not found or not in 'enrolled' status")
        log.info("Activated entity %d: %s", entity_id, row["display_name"])
        return dict(row)

    # ------------------------------------------------------------------
    # Permission Checks
    # ------------------------------------------------------------------
    #
    # check_permission() + PERMISSION_DOMAIN_MAP removed.
    #
    # It had ZERO callers in cseries — the only remaining callers live in
    # platform code, which is not what the kernel ships. Worse, it was the one
    # place in the tree that FAILED OPEN: it asked CEIGAS whether the domain
    # crossing was allowed, logged a warning when the answer was no, and then
    # returned True anyway ("legacy permission still wins for now"). Every
    # other governance path fails CLOSED — see synaptive_handler's explicit
    # "DENY (fail-closed)" and the 403 in middleware/ceigas_gate.py.
    #
    # Permissions today are enforced in three live layers, none of them here:
    #   1. App-token scopes — applications/middleware.py requires_permission(),
    #      the dependency on ~377 route handlers; 403 if the app's token never
    #      declared the scope.
    #   2. CEIGAS domain crossings — middleware/ceigas_gate.py, on every
    #      /api/* request.
    #   3. Bot capability bits — ceigas_capabilities + runtime/meta_tool_caps,
    #      enforced in bot_executor.
    #
    # The entity_permissions table this read is still live; it is written and
    # read through grant_permission / revoke_permission /
    # get_entity_permissions below and bot_executor._get_entity_permissions.
    # If a check of that table is ever needed again, write it fresh with
    # CEIGAS as the authority — do not restore the fail-open shape.

    async def grant_permission(
        self, entity_id: int, domain: str, permission_key: str,
        granted_by: str, scope: Optional[dict] = None, reason: Optional[str] = None
    ):
        """Grant a permission to an entity."""
        await self.pool.execute("""
            INSERT INTO entity_permissions (
                entity_id, domain, permission_key, granted,
                scope, granted_by, reason
            ) VALUES ($1, $2, $3, TRUE, $4::jsonb, $5, $6)
            ON CONFLICT (entity_id, domain, permission_key)
            DO UPDATE SET granted = TRUE, scope = $4::jsonb,
                          granted_by = $5, reason = $6,
                          revoked_at = NULL, revoked_by = NULL
        """, entity_id, domain, permission_key,
            json.dumps(scope) if scope else None, granted_by, reason)

        # Calendar timeline: log permission_granted event. Best-effort.
        try:
            from server.main import get_bridge_client as _gbc
            bc = _gbc()
            if bc is not None and getattr(bc, "_connected", False):
                await bc.request("calendar.log", {
                    "event_type": "permission_granted",
                    "summary": f"Granted {domain}:{permission_key} (by {granted_by})",
                    "payload": {
                        "domain": domain,
                        "permission_key": permission_key,
                        "granted_by": granted_by,
                        "scope": scope or {},
                        "reason": reason,
                    },
                    "source": "system",
                })
        except Exception as e:
            log.debug("Calendar log (permission_granted) failed: %s", e)

    async def revoke_permission(
        self, entity_id: int, domain: str, permission_key: str,
        revoked_by: str, reason: Optional[str] = None
    ):
        """Revoke a permission from an entity."""
        await self.pool.execute("""
            UPDATE entity_permissions SET
                revoked_at = NOW(), revoked_by = $4, revoked_reason = $5
            WHERE entity_id = $1 AND domain = $2 AND permission_key = $3
        """, entity_id, domain, permission_key, revoked_by, reason)

    # ------------------------------------------------------------------
    # Entity Queries
    # ------------------------------------------------------------------

    @staticmethod
    def _with_behavioral_defaults(entity: Optional[dict]) -> Optional[dict]:
        """Apply DEFAULT_BEHAVIORAL_PARAMS under the entity's stored params.

        The constant declared a default for `response_style` since it was
        written but was imported here and never applied — dead code, so the key
        had no default anywhere and `.get("response_style")` returned None for
        any host whose row had not been seeded. Callers then read that absence
        as a deliberate setting. Stored values always win; this only fills keys
        the row does not have, and normalizes the jsonb-as-str case so every
        consumer sees a dict.
        """
        if not entity:
            return entity
        bp = entity.get("behavioral_params")
        if isinstance(bp, str):
            try:
                bp = json.loads(bp)
            except (ValueError, TypeError):
                bp = None
        if not isinstance(bp, dict):
            bp = {}
        entity["behavioral_params"] = {**DEFAULT_BEHAVIORAL_PARAMS, **bp}
        return entity

    async def get_entity(self, entity_id: int) -> Optional[dict]:
        """Get full entity record."""
        row = await self.pool.fetchrow(
            "SELECT * FROM entity_registry WHERE id = $1", entity_id
        )
        return self._with_behavioral_defaults(dict(row) if row else None)

    async def get_entity_by_uuid(self, entity_uuid: str) -> Optional[dict]:
        """Get entity by UUID."""
        row = await self.pool.fetchrow(
            "SELECT * FROM entity_registry WHERE entity_uuid = $1::uuid", entity_uuid
        )
        return self._with_behavioral_defaults(dict(row) if row else None)

    async def list_entities(
        self, entity_type: Optional[str] = None,
        status: Optional[str] = None,
        license_tier: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """List entities with optional filters."""
        conditions = []
        params = []
        idx = 1

        if entity_type:
            conditions.append(f"entity_type = ${idx}")
            params.append(entity_type)
            idx += 1
        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        if license_tier:
            conditions.append(f"license_tier = ${idx}")
            params.append(license_tier)
            idx += 1

        where = " AND ".join(conditions)
        if where:
            where = "WHERE " + where

        params.append(limit)
        rows = await self.pool.fetch(
            f"SELECT id, entity_uuid, entity_type, display_name, license_tier, "
            f"status, master_directive, created_at, activated_at, last_active_at "
            f"FROM entity_registry {where} ORDER BY created_at DESC LIMIT ${idx}",
            *params
        )
        return [dict(r) for r in rows]

    async def get_entity_permissions(self, entity_id: int) -> list[dict]:
        """Get all active permissions for an entity."""
        rows = await self.pool.fetch("""
            SELECT domain, permission_key, granted, scope, granted_by, created_at
            FROM entity_permissions
            WHERE entity_id = $1 AND granted = TRUE AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY domain, permission_key
        """, entity_id)
        return [dict(r) for r in rows]
