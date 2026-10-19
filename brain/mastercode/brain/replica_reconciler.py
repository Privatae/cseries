"""Replica-reconciling sweeper — Phase 8 (decentralized context network).

THE GAP THIS CLOSES. The single|replicate availability policy (context_backends.
store_replicated) does WRITE-TIME FAN-OUT ONLY: when a host writes a file under a
'replicate' route it stores the primary (must succeed) + best-effort fans the bytes
out to other ONLINE context-store bastions, recording each copy in
context_backend_locations (is_primary on the primary). A replica machine that was
OFFLINE at write time is simply skipped — so that file ends up with fewer copies
than the policy intends. The primary is always consistent; the replicas drift.

This sweeper RECONCILES that drift. A single guarded background loop (mirroring
brain/offline_task_runner.run_offline_tasks_loop) periodically scans the entity's
replicate files (context_backend_locations) and, for any whose replica set is
INCOMPLETE — a now-online intended target has no copy — READS the bytes from a live
copy and WRITES the missing replica, recording the new location row. All of the
backend logic is reused from context_backends (resolve_route_for_write,
list_context_store_bastions, retrieve_with_failover, BastionBackend.store,
_read_locations, _record_location) via the single entry point
context_backends.reconcile_replicas — this module is only the loop + scan.

SAFETY (verified against the design contract):
  - NON-DESTRUCTIVE: only ADDS missing replica copies. Never deletes the primary or
    any existing copy. If the primary (and every replica) is unreadable, the file is
    skipped — never lose data.
  - IDEMPOTENT: a file already at its full replica count is a no-op (reconcile_replicas
    returns added=0).
  - DEGRADE-NOT-BREAK: per-file try/except (one file's failure never stops the
    sweep) AND per-loop try/except (a DB blip never permanently stops or crashes the
    loop / the lifespan). asyncio.CancelledError exits cleanly on shutdown.
  - DEFAULT-PATH UNTOUCHED: the scan only enumerates files that have a
    context_backend_locations row — i.e. files written under 'replicate'. pg /
    single-backend files have NO such row, so the sweeper never touches them.
  - Gated on entity_id > 0 + a brain pool at dispatch (same guards as the
    offline-task runner) — genesis entity-0 has no mesh / no replicate files.
"""

import asyncio
import logging

log = logging.getLogger("cseries.brain.replica_reconciler")

# Sweep cadence. Researched default: production re-replication / anti-entropy sweeps run on the order
# of minutes, not seconds — Cassandra's hinted-handoff + read-repair and Ceph's
# scrub/recovery cycles operate on minute-scale, and there's no urgency here (the
# PRIMARY is always consistent; replicas are redundancy for the offline-read case).
# 5 minutes is frequent enough that a replica box rejoining is reconciled promptly,
# slow enough to be near-free (one cheap DISTINCT scan + work only on incomplete
# files). Overridable by env so ops can retune without a code change.
import os
SWEEP_INTERVAL_S = int(os.environ.get("CTX_REPLICA_SWEEP_INTERVAL_S", "300"))

# Cap files examined per pass so a host with a huge replicate corpus can't pin the
# loop doing many mesh round-trips back-to-back in one tick. The next pass picks up
# the rest; a fully-reconciled corpus is a no-op regardless.
PER_PASS_MAX_FILES = int(os.environ.get("CTX_REPLICA_SWEEP_MAX_FILES", "500"))


async def run_replica_reconciler_loop(pool, entity_id: int) -> None:
    """Periodically reconcile incomplete replica sets for this entity, forever.

    Dispatched once at lifespan (server/main.py) as a guarded background task,
    gated on relay_entity_id > 0. Cancellation (shutdown) exits cleanly; every
    other error is logged and the loop continues so a transient DB blip or an
    offline bastion never permanently stops reconciliation. NEVER raises out of
    the loop (it must not crash the lifespan)."""
    log.info("replica reconciler online — entity %d, sweep %ds",
             entity_id, SWEEP_INTERVAL_S)
    # Imported inside the loop body's try so an import hiccup is non-fatal too.
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_S)

            from brain.mastercode import context_backends

            # Candidate set: the files this entity has any replica footprint for
            # (the 'single'/pg default path has none, so they're never touched).
            try:
                paths = await context_backends.list_replicate_file_paths(
                    pool, entity_id, limit=PER_PASS_MAX_FILES)
            except Exception as e:  # noqa: BLE001
                log.warning("replica reconciler: scan failed (non-fatal): %s", e)
                continue
            if not paths:
                continue

            swept = 0
            added_total = 0
            for fp in paths:
                # Per-file guard: one file's failure (read error, offline replica,
                # unexpected exception) never stops the sweep or affects other files.
                try:
                    res = await context_backends.reconcile_replicas(
                        pool, entity_id, fp)
                    swept += 1
                    added_total += int(res.get("added") or 0)
                except Exception as e:  # noqa: BLE001
                    log.warning("replica reconciler: reconcile of %s failed "
                                "(skipped): %s", fp, e)
                    continue
            if added_total:
                log.info("replica reconciler: swept %d file(s), added %d missing "
                         "replica copy(ies) (entity %d)",
                         swept, added_total, entity_id)

        except asyncio.CancelledError:
            log.info("replica reconciler shutting down (entity %d)", entity_id)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("replica reconciler loop cycle error (non-fatal): %s", e)
