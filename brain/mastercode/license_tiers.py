"""Authoritative license-tier vocabulary + helpers.

Single source of truth for `entity_registry.license_tier`. Read by:
  - bot_executor — gates builder tools
  - prompt_schema — gates admin-only prompt blocks (platform internals,
    platform update flow, sovereign-codebase, source-code listing)
  - genesis / provisioning — sets initial tier on new entities
  - bridge admin paths — anything that reads `license_tier`

Tiers (2026-05-28 — final collapse to four values, enforced by CHECK
in both the platform's entity registry and users tables):

  standard   — every signup. Default. Shared-infra community pod.
  pro        — reserved (future: cseries-image fork on platform network).
               No entities on it today.
  enterprise — reserved (future: custom deployments).
               No entities on it today (legacy rows kept the label but
               are tests).
  admin      — the platform operator + platform-role entities.
               Gets platform internals + platform update flow + full
               sovereign-codebase access in the system prompt. The
               legacy "network" / WireGuard tier collapsed into admin.
"""

from __future__ import annotations

# Canonical tier list. Adding a new value here means deciding which
# bucket functions below should include it, AND widening the CHECK
# constraint in entity_registry / users.
LICENSE_TIERS: tuple[str, ...] = (
    "standard",
    "pro",
    "enterprise",
    "admin",
)

# Buckets.
#
# Admin: gets platform internals in the system prompt + every code
# path that's gated behind "is this the operator or a platform-role entity".
_ADMIN_BUCKET: frozenset[str] = frozenset({"admin"})

# Builder: has build/ship tools (the build tool, k8s_*, code_*, etc.).
_BUILDER_BUCKET: frozenset[str] = frozenset({"pro", "enterprise", "admin"})


def _norm(tier: str | None) -> str:
    return (tier or "").strip().lower()


def is_known(tier: str | None) -> bool:
    """True if the tier is one of LICENSE_TIERS. Unknown values can
    happen for legacy rows (`network`, `professional`, etc.) — callers
    that want strict behavior should reject those; callers that just
    need to gate features should default to the safest interpretation
    (no admin, no builder)."""
    return _norm(tier) in LICENSE_TIERS


def is_admin(tier: str | None) -> bool:
    """Platform admin gate. Drives admin-only prompt blocks +
    platform-control surfaces. Only the platform operator."""
    return _norm(tier) in _ADMIN_BUCKET


def is_builder(tier: str | None) -> bool:
    """Builder-tools gate. Drives bot_executor's BUILDER_TIER_TOOLS
    access decision. Implies the entity can call build, docker_*,
    k8s_*, code_*, etc."""
    return _norm(tier) in _BUILDER_BUCKET


def tier_label(tier: str | None) -> str:
    """Normalized display value. Falls back to 'standard' for empty
    or unknown values so callers always have something printable.
    'unknown' here means a legacy value left over from before the
    2026-05-28 vocabulary collapse — the DB CHECK now blocks new
    writes outside LICENSE_TIERS."""
    n = _norm(tier)
    if n in LICENSE_TIERS:
        return n
    return "standard"
