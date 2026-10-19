"""
Genesis — what is left of the first-boot link flow: the guarded codebook seed.

`_seed_codebook` is the one path that writes an entity's codebook into the pod's
own database (Invariant-9 shape gate + audit rows). server/bridge/identity_seed.py
seeds through it at boot, and brain/mastercode/runtime/personality_validate.py
names it as one of the two writers it guards.

The HTTP half — GET /api/genesis/status and POST /api/genesis/link — was removed
on 2026-10-01. It linked an unlinked container to an entity by sending a Google
id_token to the platform's bootstrap service (TLS without verification), then
overwrote the pod's relay certs and identity, imported a memory bundle and
SIGTERMed PID 1. Nothing called it: the frontend that drove it is gone, and
Hosts are provisioned by the operator. It carried no permission check, so any
app on the Host could reach it. scripts/test_api_route_guards.py pins that it
stays gone.
"""

import json
import logging

from server import brain_service

logger = logging.getLogger("privatae.pc.genesis")


async def _seed_codebook(entity_id: int, display_name: str, codebook: dict):
    """Import codebook + context files into local PG."""
    pool = brain_service.get_pool()
    if not pool:
        logger.error("No DB pool — cannot seed codebook")
        return

    # Upsert entity_registry — row may not exist for this entity_id.
    # display_name is NOT NULL.
    display_name = display_name or f"Host #{entity_id}"

    # Ship 17 (2026-05-24) — Invariant 9 gate. Validate the
    # 10-dimension shape BEFORE the INSERT. Any seed/current weights
    # that don't match the named dimensions fail closed
    # with an audit row invariant9:personality denied; the upsert is
    # skipped so the row never lands in tampered shape.
    # 2026-07-12: routed through guard_personality_write — the gate now
    # also fail-closes on unregistered writer sources (a legacy writer once
    # bypassed the chokepoint and drifted a Host's personality).
    import sys as _sys
    _sys.path.insert(0, "/app/brain")
    from mastercode.runtime.personality_validate import guard_personality_write
    seed_w = codebook.get("seed_weights")
    curr_w = codebook.get("current_weights")
    for col, w in (("seed_weights", seed_w), ("current_weights", curr_w)):
        if w is None:
            continue
        ok, vios = await guard_personality_write(
            pool, entity_id,
            source="routes.genesis._seed_codebook",
            column=col,
            weights=w,
            detail="Invariant 9 violation at bootstrap",
        )
        if not ok:
            logger.warning(
                "genesis bootstrap rejected for entity %d on column %s: %s",
                entity_id, col, vios,
            )
            return

    try:
        # Envelope seeding (2026-07-21, review B4): genesis previously never
        # wrote envelope_bounds — every provisioned entity got '{}', so
        # sub-0.5-default drift never self-corrected. Seed real per-dim
        # bounds; consent sliders remain the per-user override.
        _dims = list((seed_w or curr_w or {}).keys()) or [
            "warmth", "directness", "humor", "formality", "curiosity",
            "assertiveness", "patience", "vulnerability", "energy", "depth",
        ]
        envelope_seed = codebook.get("envelope_bounds") or {d: 0.2 for d in _dims}
        await pool.execute("""
            INSERT INTO entity_registry (id, display_name, personality_mode, seed_weights, current_weights, behavioral_params, master_directive, envelope_bounds)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                personality_mode = EXCLUDED.personality_mode,
                seed_weights = EXCLUDED.seed_weights,
                current_weights = EXCLUDED.current_weights,
                behavioral_params = EXCLUDED.behavioral_params,
                master_directive = EXCLUDED.master_directive,
                envelope_bounds = CASE WHEN entity_registry.envelope_bounds IS NULL OR entity_registry.envelope_bounds::text IN ('{}', 'null') THEN EXCLUDED.envelope_bounds ELSE entity_registry.envelope_bounds END
        """,
            entity_id,
            display_name,
            codebook.get("personality_mode"),
            json.dumps(seed_w) if seed_w else None,
            json.dumps(curr_w) if curr_w else None,
            json.dumps(codebook.get("behavioral_params")) if codebook.get("behavioral_params") else None,
            codebook.get("master_directive"),
            json.dumps(envelope_seed),
        )
        logger.info("Codebook seeded for entity %d", entity_id)
    except Exception as e:
        logger.warning("Codebook seed failed: %s", e)

    # Seed context files
    context_files = codebook.get("context_files", [])
    for cf in context_files:
        try:
            await pool.execute("""
                INSERT INTO entity_context (entity_id, file_path, content, content_type)
                VALUES ($1, $2, $3, $4)
            """,
                entity_id,
                cf.get("file_path", ""),
                cf.get("content", ""),
                cf.get("content_type", "text/plain"),
            )
        except Exception:
            pass

    logger.info("Seeded %d context files for entity %d", len(context_files), entity_id)
