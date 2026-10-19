"""Platform-tool gate.

Some apps are platform/dev tools — only a platform entity should be able to
see them in the App Store and install them. Production entities (paying user
containers) must not have access.

The `target_entities` field in app.yaml is the canonical mechanism for
per-entity gating; this module surfaces a hardcoded fallback for apps
whose source we don't own here (e.g., `cert-inspector` is published
externally and its manifest can't be edited from this repo).

Three enforcement points (defense in depth):
  - `routes/applications.py:store_catalog` — filter so platform tools
    don't appear in non-platform entities' app-store at all.
  - `routes/applications.py:store_purchase` — reject 403 if a
    non-platform entity tries to purchase.
  - `routes/applications.py:install_app` and the native-bundle path —
    reject 403 if the installed bundle's manifest has target_entities
    that the running entity is not in.
"""
from __future__ import annotations

import os
from typing import Iterable


# Apps whose published manifest may not carry `target_entities` (because
# their source isn't in this repo, e.g., cert-inspector ships from
# elsewhere). We hardcode the gate for them.
PLATFORM_TOOLS: frozenset[str] = frozenset({
    "cert-inspector",
    "training-memory",
})

# Entities that are allowed to install platform tools. None by default.
PLATFORM_ENTITIES: frozenset[int] = frozenset()


def current_entity_id() -> int:
    """Resolve the running pod's entity_id from env. Treat unset/0 as 'unknown'."""
    try:
        return int(os.environ.get("ENTITY_ID", "0"))
    except ValueError:
        return 0


def is_platform_tool(app_id: str) -> bool:
    return app_id in PLATFORM_TOOLS


def allowed_for_entity(app_id: str, entity_id: int) -> bool:
    """True iff `app_id` is installable in `entity_id` per the platform-tool gate.

    Apps not in PLATFORM_TOOLS pass. Apps in PLATFORM_TOOLS only pass for
    PLATFORM_ENTITIES. Used by catalog/purchase/install to enforce the
    gate when the manifest lacks `target_entities`.
    """
    if app_id not in PLATFORM_TOOLS:
        return True
    return entity_id in PLATFORM_ENTITIES


def assert_target_entities_allowed(
    target_entities: Iterable[int] | None,
    entity_id: int,
) -> bool:
    """Honor a manifest's `target_entities` field.

    True iff `target_entities` is empty (no gate) or `entity_id` is in it.
    """
    if not target_entities:
        return True
    return int(entity_id) in {int(x) for x in target_entities}
