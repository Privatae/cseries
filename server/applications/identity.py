"""
AppIdentity — the single source of truth for "what is this app".

The recurring app confusion came from app identity being smeared across FIVE
gates in five files (kernel.KERNEL_REQUIRED_APPS, manifest.tier, manifest.platform,
manifest.target_entities, the launcher filter). This module consolidates them
into ONE record so every gate reads the same answer and identity is legible in
one place.

See docs/app-architecture.md (the 3 STATES: shipped/published/installed) and
docs/app-consolidation-plan.md (Phase 1).
"""
from __future__ import annotations

from dataclasses import dataclass

from server.applications.kernel import KERNEL_REQUIRED_APPS


@dataclass(frozen=True)
class AppIdentity:
    """What an app IS — independent of where it currently lives (shipped /
    published / installed). Derive via identity(manifest); never re-derive the
    individual flags at call sites."""
    id: str
    provenance: str                    # trust class {native,standard,community,custom} — renamed from `tier`
    native: bool                       # kernel-required (in KERNEL_REQUIRED_APPS) — refuse-to-boot if missing
    operator_only: bool                # operator-only: hidden from the user launcher + store-banned
    target_entities: tuple[int, ...]   # empty = installs on every entity

    @property
    def tier(self) -> str:
        """PERMANENT back-compat alias for `provenance` (renamed 2026-06-17)."""
        return self.provenance

    @property
    def user_visible(self) -> bool:
        """Whether a normal user should ever see this app in their launcher."""
        return not self.operator_only

    @property
    def store_eligible(self) -> bool:
        """Whether this app may be published to the central store. Operator-only
        and native apps never go to the store (they ship in the image)."""
        return not (self.operator_only or self.native)

    def installs_on(self, entity_id: int) -> bool:
        return (not self.target_entities) or (entity_id in self.target_entities)


def identity(manifest) -> AppIdentity:
    """Derive the canonical identity from a loaded AppManifest.

    operator_only reads BOTH the new `operator_only` field and the legacy
    `platform` flag (back-compat for any already-shipped manifests) — the two are
    merged here so no call site has to know about the rename.
    """
    operator_only = bool(
        getattr(manifest, "operator_only", False) or getattr(manifest, "platform", False)
    )
    return AppIdentity(
        id=manifest.id,
        provenance=manifest.provenance,
        native=manifest.id in KERNEL_REQUIRED_APPS,
        operator_only=operator_only,
        target_entities=tuple(manifest.target_entities or ()),
    )
