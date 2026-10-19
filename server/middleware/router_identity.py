"""Who a request may say sent it.

Two headers carry identity into this kernel, and both are only true when the
in-pod mTLS front door (server/mtls_frontdoor.py) put them there:

  X-Client-Cert-CN   the client cert the front door verified on :8443.
  X-Authentik-*      the signed-in person the direct router vouches for. The
                     edge strips any client copy, the router sets its own, and
                     it reaches this pod only over :8443 under the router's
                     cert (DASHBOARD_ROUTER_CN).

The front door forwards to the plain :8800 listener over loopback. So did
every other in-pod caller, and the kernel could not tell them apart: anything
on the pod's loopback could write either header and be believed. The failure
this closes is the Host's own model doing exactly that through its credential broker, sending
Circles and contacts actions as its person.

A header value is not proof, because the forger chooses it. Neither is the
source address or a second port: every caller in the pod shares the loopback.
What the front door has and nothing else in the pod has is this process's
memory. The front door runs inside the kernel's own process (server/main.py
lifespan, mtls_frontdoor.run_supervised, one uvicorn process from
server/launch.py), so the proof is a random value made at import, held only
here. It is never put in the environment (children and /proc/<pid>/environ
would see it), never written to disk, never logged, and taken off the request
before any route can read it, so no response can echo it.

FrontDoorProof is the one place that decides. On every HTTP request and
WebSocket upgrade:
  - the proof header itself is always removed;
  - X-Client-Cert-CN survives only on a request carrying the proof;
  - X-Authentik-* survives only on a request carrying the proof whose CN is
    the router's.
Everything else passes untouched, so a loopback caller keeps every access it
had and loses only the right to name a person or a cert.
"""
from __future__ import annotations

import hmac
import secrets

from server.middleware.localhost_only import ROUTER_CN

# What the front door adds. Lowercase: ASGI header names are lowercase.
PROOF_HEADER = b"x-cseries-frontdoor-proof"
_CN_HEADER = b"x-client-cert-cn"
_PERSON_PREFIX = b"x-authentik-"

_PROOF = secrets.token_urlsafe(32).encode("ascii")


def proof() -> bytes:
    """The value the front door puts on what it forwards. In-process only."""
    return _PROOF


def _one(headers, name: bytes) -> bytes | None:
    """The header's value when it appears exactly once, else None. Two copies
    are refused rather than one picked: a forger who can add a second copy
    must not get to choose which one is read."""
    vals = [v for k, v in headers if k == name]
    return vals[0] if len(vals) == 1 else None


def decide(headers) -> tuple[bool, bool]:
    """(via_front_door, via_router) for a raw ASGI header list."""
    p = _one(headers, PROOF_HEADER)
    via_front_door = p is not None and hmac.compare_digest(p, _PROOF)
    cn = _one(headers, _CN_HEADER) if via_front_door else None
    via_router = bool(ROUTER_CN) and cn is not None and cn.decode("latin-1") == ROUTER_CN
    return via_front_door, via_router


def scrub(headers) -> tuple[list, bool, bool]:
    """The header list a route may see, and the two decisions."""
    via_front_door, via_router = decide(headers)
    out = []
    for k, v in headers:
        if k == PROOF_HEADER:
            continue
        if k == _CN_HEADER and not via_front_door:
            continue
        if k.startswith(_PERSON_PREFIX) and not via_router:
            continue
        out.append((k, v))
    return out, via_front_door, via_router


class FrontDoorProof:
    """Pure ASGI, so it sees WebSocket upgrades too (a BaseHTTPMiddleware
    does not). Must sit outside everything that reads either header: the
    localhost gate and its WebSocket half, the bastion binding, every route."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            # A new header list on the SAME scope, not a copied scope: the
            # router fills the matched endpoint into the scope it is handed,
            # and code outside this layer reads it there afterwards (no other
            # layer in this stack copies; scripts/test_ceigas_gate_routes.py
            # reads it that way). Nothing outside needs the old headers.
            scope["headers"] = scrub(scope.get("headers") or ())[0]
        await self.app(scope, receive, send)
