"""In-pod operator-identity checks (defense-in-depth).

Context: the direct router (`cseries-direct-router`) is the authority — it verifies
the caller is an operator of THIS entity (`operator_entities`) and injects the
verified `X-Authentik-Email` over mTLS; the in-pod mTLS front terminator
(`server/mtls_frontdoor.py`) + `LocalhostOnlyMiddleware` already ensure only the
router's own cert can reach the dashboard. This module adds the LAST layer: the
pod independently checks the human identity before it opens a sensitive surface
(the realtime WebSocket, the vault), so a router bug or misroute can never hand
one operator a live channel into another entity's pod.

Operator allowlist source: the `ENTITY_OPERATOR_EMAILS` env (comma-separated,
provisioned per pod from `operator_entities`). Rollout-safe by design:

  * A verified, non-empty email is ALWAYS required by callers (closes the
    "anonymous WebSocket" gap — the old handler accepted with no identity).
  * MEMBERSHIP is enforced only when `ENTITY_OPERATOR_EMAILS` is provisioned.
    When it is unset (e.g. the image rolled ahead of provisioning), membership
    is not checked and the router stays the sole authority — so shipping this
    can never lock an operator out of a pod that hasn't been provisioned yet.
    A one-time CRITICAL log flags the unprovisioned state so it's visible.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("privatae.pc.operator_identity")

_EMAIL_HEADER = "x-authentik-email"
_warned_unprovisioned = False


def request_email(headers) -> str:
    """The verified operator email the router injected, lowercased. `headers` is
    any case-insensitive mapping (Starlette `Headers`, or a dict). Empty string
    if absent.

    Reading the header is safe because of what runs before any route:
    FrontDoorProof (server/middleware/router_identity.py) removes every
    X-Authentik-* header from a request the mTLS front door did not forward
    under the router's cert. Before that, any caller on the pod's
    loopback could name any person here."""
    try:
        val = headers.get(_EMAIL_HEADER) or headers.get("X-Authentik-Email") or ""
    except Exception:
        val = ""
    return val.strip().lower()


def operator_allowlist() -> set[str] | None:
    """This entity's operator emails, or None when unprovisioned.

    Public because the None is load-bearing for callers that must NOT inherit
    is_operator's fail-open. It answers True on an unprovisioned pod so a
    provisioning lag can never lock an operator out of a surface the router has
    already vouched for. Nothing vouches for the peer lane (server/peer), so a
    caller there has to see "unknown" as its own case and refuse. Returning the
    set itself is what lets each caller pick its own direction.
    """
    raw = (os.environ.get("ENTITY_OPERATOR_EMAILS") or "").strip()
    if raw:
        return {e.strip().lower() for e in raw.split(",") if e.strip()}
    # A standalone Host's operator is its owner (server/owner_login.py).
    from server.owner_login import owner_email
    owner = owner_email()
    return {owner} if owner else None


def _allowlist() -> set[str] | None:
    return operator_allowlist()


def is_operator(email: str) -> bool:
    """True if `email` is an operator of THIS entity. When the allowlist is
    unprovisioned, returns True (the router remains the authority); callers must
    still require a non-empty email themselves."""
    global _warned_unprovisioned
    if not email:
        return False
    allow = _allowlist()
    if allow is None:
        if not _warned_unprovisioned:
            _warned_unprovisioned = True
            logger.critical(
                "ENTITY_OPERATOR_EMAILS is unset for entity %s — in-pod operator "
                "membership is NOT being enforced (router remains sole authority). "
                "Provision it to activate defense-in-depth.",
                os.environ.get("ENTITY_ID"),
            )
        return True
    return email in allow


def check_operator(headers) -> tuple[bool, str]:
    """Gate a sensitive surface. Returns (ok, email). ok is False when there is
    no verified email at all, or when membership is provisioned and the email is
    not a member of this entity."""
    email = request_email(headers)
    if not email:
        return False, ""
    return is_operator(email), email
