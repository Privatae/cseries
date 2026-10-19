"""The identity a new C-Series gives itself on first boot.

A Host's database starts with a placeholder row (init-db.sql). On the
platform the real identity arrived over the bridge; a standalone Host has no
one to ask, so it writes this bundle through the same guarded seeder
(server/bridge/identity_seed._seed -> routes.genesis._seed_codebook).

Everything here is a starting point the owner changes afterwards: the name,
the personality sliders and the response style all have settings.
"""

from __future__ import annotations

DEFAULT_NAME = "C-Series"

# The ten personality dimensions the seeder's shape check requires, all at the
# neutral midpoint.
NEUTRAL_WEIGHTS = {
    "warmth": 1.0, "directness": 1.0, "humor": 1.0, "formality": 1.0,
    "curiosity": 1.0, "assertiveness": 1.0, "patience": 1.0,
    "vulnerability": 1.0, "energy": 1.0, "depth": 1.0,
}

# The tools gate (brain/mastercode/runtime/tool_access.py) opens for any value
# above 3; the owner of a standalone Host holds the whole tool surface.
OWNER_CAPABILITIES = 255


def first_run_bundle() -> dict:
    """The bundle identity_seed._seed expects: a display name and a codebook."""
    return {
        "display_name": DEFAULT_NAME,
        "codebook": {
            "personality_mode": "sovereign",
            "master_directive": "",
            "seed_weights": dict(NEUTRAL_WEIGHTS),
            "current_weights": dict(NEUTRAL_WEIGHTS),
            "behavioral_params": {"response_style": "conversational"},
            "permission_overrides": {"bot_capabilities": OWNER_CAPABILITIES},
        },
    }
