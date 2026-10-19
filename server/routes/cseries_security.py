"""C-Series-Security service proxy — entity-side routes that forward mTLS
tool calls to the platform cseries-security service container.

The entity holds a role cert at /etc/cseries-security-client/ (provisioned
by the operator). Browser apps inside the entity dashboard can't speak
mTLS directly to cseries-security:8443, so they POST through these routes
and we relay over the entity's mTLS lane.

If the cert mount isn't present (entity wasn't granted cseries-security
access), routes return 503. This is the gate that keeps "everyone gets
the cert at provision" from becoming "everyone calls every tool" — apps
that depend on this proxy simply don't work on entities without the mount.
"""
from __future__ import annotations

import logging
import os
import ssl
from pathlib import Path

import aiohttp
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.applications.middleware import requires_permission

log = logging.getLogger("privatae.cseries_security")

router = APIRouter(prefix="/api/cseries-security", tags=["cseries-security"])

_CERT_DIR = Path(os.environ.get("CSERIES_SECURITY_CLIENT_DIR", "/etc/cseries-security-client"))
# No default address: a default only turned "not configured" into an 8 s
# timeout against a host that may not exist.
_UPSTREAM_URL = os.environ.get("CSERIES_SECURITY_URL", "").rstrip("/")


def _upstream() -> str:
    if not _UPSTREAM_URL:
        raise HTTPException(status_code=503, detail="cseries-security is not configured on this Host "
                                                    "(CSERIES_SECURITY_URL is not set)")
    return _UPSTREAM_URL


def _client_ssl() -> ssl.SSLContext:
    """Build the SSLContext for the outbound mTLS call to cseries-security.

    Two distinct chains in play, easy to mix up:
      - The cseries-security SERVER cert (:8443) is signed by CEIGAS Root
        directly, so `cafile` here points at `ceigas-root.crt`.
      - OUR client cert (`role.crt`) is signed by the C-Series-Security
        Intermediate. The server uses its own copy of the intermediate
        to verify us; we don't need the intermediate in our trust store.

    `intermediate.crt` is kept in the mount for tooling/inspection but
    not used as a trust anchor on this side.
    """
    role_crt = _CERT_DIR / "role.crt"
    role_key = _CERT_DIR / "role.key"
    trust_anchor = _CERT_DIR / "ceigas-root.crt"
    if not (role_crt.is_file() and role_key.is_file() and trust_anchor.is_file()):
        raise HTTPException(
            status_code=503,
            detail=(
                "cseries-security client cert not provisioned in this host. "
                f"Expected files in {_CERT_DIR}/."
            ),
        )
    ctx = ssl.create_default_context(cafile=str(trust_anchor))
    ctx.load_cert_chain(certfile=str(role_crt), keyfile=str(role_key))
    ctx.check_hostname = False  # server SAN is the in-cluster DNS name, not the IP we dial
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class ToolCallRequest(BaseModel):
    arguments: dict = {}


@router.get("/health", dependencies=[Depends(requires_permission("read:cseries_security"))])
async def health():
    """Reach the upstream cseries-security :8443/v1/health using the entity's
    role cert. Returns the upstream payload + the parsed caller context
    (entity_id/role/tier) so the dashboard can confirm cert provisioning."""
    ssl_ctx = _client_ssl()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{_upstream()}/v1/health", ssl=ssl_ctx, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                payload = await resp.json()
                payload["_upstream_status"] = resp.status
                return payload
        except aiohttp.ClientError as exc:
            raise HTTPException(status_code=502, detail=f"cseries-security unreachable: {exc}")


@router.get("/tools", dependencies=[Depends(requires_permission("read:cseries_security"))])
async def list_tools():
    """List the tools this entity's cert is authorized to call."""
    ssl_ctx = _client_ssl()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{_upstream()}/v1/tools", ssl=ssl_ctx, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                return await resp.json()
        except aiohttp.ClientError as exc:
            raise HTTPException(status_code=502, detail=f"cseries-security unreachable: {exc}")


@router.post("/tools/{name}", dependencies=[Depends(requires_permission("execute:cseries_security"))])
async def call_tool(name: str, body: ToolCallRequest):
    """Proxy a tool call upstream. Body shape mirrors the upstream contract:
    {"arguments": {...tool-specific...}}. Upstream returns {"result": {...}}."""
    ssl_ctx = _client_ssl()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                f"{_upstream()}/v1/tools/{name}",
                json={"arguments": body.arguments},
                ssl=ssl_ctx,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                payload = await resp.json()
                if resp.status >= 400:
                    raise HTTPException(status_code=resp.status, detail=payload)
                return payload
        except aiohttp.ClientError as exc:
            raise HTTPException(status_code=502, detail=f"cseries-security unreachable: {exc}")
