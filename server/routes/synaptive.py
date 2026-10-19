"""
Synaptive — token CRUD for external connected chats.

Tokens authorize an MCP client (Claude Desktop, Cursor, etc.) to call
shape() against this user's container via the public synaptive endpoint.
Plaintext shown to the user once at creation. Only the sha256 is stored.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Depends
from server.applications.middleware import requires_permission
from pydantic import BaseModel

from server import brain_service


async def _push_handle_event(action: str, handle: str, entity_id: int) -> None:
    """Best-effort: tell the platform about a new/revoked handle so the
    edge can route <handle>.<SYNAPTIVE_PUBLIC_HOST> → this entity. Failure
    is non-fatal — local token CRUD is the source of truth.

    `action` is the bridge op tail: 'register' or 'revoke'. Maps to
    bridge ops `synaptive.handle.register` / `synaptive.handle.revoke`.
    """
    try:
        from server.main import get_bridge_client
    except Exception as e:
        logger.debug("bridge_client import unavailable: %s", e)
        return
    client = get_bridge_client()
    if client is None or not client.connected:
        logger.info(
            "Bridge not connected — handle %s %s deferred (will resync on next bridge op)",
            handle, action,
        )
        return
    op = "synaptive.handle.register" if action == "register" else "synaptive.handle.revoke"
    try:
        result = await client.request(op, {"handle": handle, "entity_id": entity_id})
        if result is None:
            logger.warning("Bridge %s returned no result for handle %s", op, handle)
        else:
            logger.info("Pushed %s for handle=%s entity=%s", op, handle, entity_id)
    except Exception as e:
        logger.warning("Failed to push %s for handle %s: %s", op, handle, e)

logger = logging.getLogger("privatae.pc.synaptive")

router = APIRouter(prefix="/api/synaptive", tags=["synaptive"])

# The public zone handles are served under. No default: unset, a token has no
# public URL.
PUBLIC_HOST = os.environ.get("SYNAPTIVE_PUBLIC_HOST", "").strip()


class CreateToken(BaseModel):
    name: str
    scopes: Optional[list[str]] = None


# Internal-only fields used by the bridge's anchor.redeem op to bind a
# gateway-class token at install time. Never accepted from a user-side
# dashboard call — the internal-auth header is required.
class _GatewayBinding(BaseModel):
    operator_kind:        str
    hardware_fingerprint: str
    machine_anchor_id:    Optional[int] = None


def _internal_auth_ok(presented: Optional[str]) -> bool:
    """Check `X-CSeries-Internal-Auth` against the per-container shared secret.

    The secret is env var `INTERNAL_API_SECRET`, made by this process at start
    (server/launch.py ensure_internal_api_secret) unless the deployment
    supplied one. If unset on the container, ALL
    internal calls are refused — fail closed.
    """
    expected = os.environ.get("INTERNAL_API_SECRET", "")
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected, presented)


def _entity_id() -> int:
    return int(os.environ.get("ENTITY_ID", "0"))


def _mint_token() -> tuple[str, bytes]:
    raw = "mcp_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw.encode()).digest()
    return raw, digest


def _mint_handle() -> str:
    return secrets.token_hex(8)


def _public_url(handle: str) -> str:
    return f"https://{handle}.{PUBLIC_HOST}" if PUBLIC_HOST else ""


def _claude_desktop_snippet(url: str, token: str) -> dict:
    return {
        "mcpServers": {
            "synaptive": {
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }


@router.get("/tokens", dependencies=[Depends(requires_permission("read:session"))])
async def list_tokens():
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain pool not ready")
    rows = await pool.fetch(
        """
        SELECT id, name, url_handle, scopes, created_at, last_used_at
          FROM mcp_client_tokens
         WHERE entity_id = $1 AND revoked_at IS NULL
         ORDER BY created_at DESC
        """,
        _entity_id(),
    )
    return {
        "tokens": [
            {
                "id": r["id"],
                "name": r["name"],
                "url": _public_url(r["url_handle"]),
                "scopes": list(r["scopes"]),
                "created_at": r["created_at"].isoformat(),
                "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
            }
            for r in rows
        ]
    }


@router.post("/tokens", dependencies=[Depends(requires_permission("write:session"))])
async def create_token(
    body: CreateToken,
    x_cseries_internal_auth:        Optional[str] = Header(None),
    x_cseries_operator_kind:        Optional[str] = Header(None),
    x_cseries_hardware_fingerprint: Optional[str] = Header(None),
    x_cseries_anchor_id:            Optional[int] = Header(None),
):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    if len(name) > 80:
        raise HTTPException(400, "name too long")

    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain pool not ready")

    # Allow `recall` scope for internal/gateway calls only (operators that need
    # active memory search). User-side dashboard calls keep the original
    # shape/signal allowlist.
    if x_cseries_internal_auth is not None:
        if not _internal_auth_ok(x_cseries_internal_auth):
            raise HTTPException(401, "internal auth invalid")
        scopes = body.scopes or ["shape", "recall", "signal"]
        allowed = {"shape", "recall", "signal"}
    else:
        scopes = body.scopes or ["shape", "signal"]
        allowed = {"shape", "signal"}
    bad = [s for s in scopes if s not in allowed]
    if bad:
        raise HTTPException(400, f"unsupported scope(s): {bad}")

    token, token_hash = _mint_token()
    handle = _mint_handle()
    url = _public_url(handle)

    if x_cseries_internal_auth is not None:
        is_bootstrap = not x_cseries_operator_kind and not x_cseries_hardware_fingerprint
        if is_bootstrap:
            # Bootstrap path — single-use at genesis, no operator identity.
            # Refuses if entity already has ANY token row (active OR revoked).
            # This is the only system-issued path; one shot per entity, period.
            existing = await pool.fetchval(
                "SELECT COUNT(*) FROM mcp_client_tokens WHERE entity_id = $1",
                _entity_id(),
            )
            if existing:
                logger.warning(
                    "synaptive.bootstrap.replay_refused: entity_id=%d existing_tokens=%d",
                    _entity_id(), existing,
                )
                raise HTTPException(
                    403,
                    "bootstrap exhausted: this host already has tokens; use operator path",
                )
            row = await pool.fetchrow(
                """
                INSERT INTO mcp_client_tokens
                  (entity_id, name, token_hash, url_handle, scopes)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, created_at
                """,
                _entity_id(), name, token_hash, handle, scopes,
            )
            logger.info(
                "synaptive.bootstrap.mint: entity_id=%d token_id=%s handle=%s",
                _entity_id(), row["id"], handle,
            )
        else:
            # Gateway bind path — populate operator-class columns.
            row = await pool.fetchrow(
                """
                INSERT INTO mcp_client_tokens
                  (entity_id, name, token_hash, url_handle, scopes,
                   operator_kind, hardware_fingerprint, machine_anchor_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id, created_at
                """,
                _entity_id(), name, token_hash, handle, scopes,
                x_cseries_operator_kind, x_cseries_hardware_fingerprint, x_cseries_anchor_id,
            )
            logger.info(
                "Synaptive gateway token bound: id=%s name=%s handle=%s operator_kind=%s",
                row["id"], name, handle, x_cseries_operator_kind,
            )
    else:
        # User dashboard path — original behavior.
        row = await pool.fetchrow(
            """
            INSERT INTO mcp_client_tokens (entity_id, name, token_hash, url_handle, scopes)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, created_at
            """,
            _entity_id(), name, token_hash, handle, scopes,
        )
        logger.info("Synaptive token created: id=%s name=%s handle=%s", row["id"], name, handle)

    await _push_handle_event("register", handle, _entity_id())

    return {
        "id": row["id"],
        "name": name,
        "url": url,
        "handle": handle,
        "token": token,
        "scopes": scopes,
        "created_at": row["created_at"].isoformat(),
        "claude_desktop_config": _claude_desktop_snippet(url, token),
    }


@router.delete("/tokens/{token_id}", dependencies=[Depends(requires_permission("write:session"))])
async def revoke_token(token_id: int):
    pool = brain_service.get_pool()
    if pool is None:
        raise HTTPException(503, "brain pool not ready")
    row = await pool.fetchrow(
        """
        UPDATE mcp_client_tokens
           SET revoked_at = now()
         WHERE id = $1 AND entity_id = $2 AND revoked_at IS NULL
         RETURNING url_handle
        """,
        token_id, _entity_id(),
    )
    if row is None:
        raise HTTPException(404, "token not found")
    logger.info("Synaptive token revoked: id=%s handle=%s", token_id, row["url_handle"])
    await _push_handle_event("revoke", row["url_handle"], _entity_id())
    return {"ok": True}
