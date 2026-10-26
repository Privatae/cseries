"""
Privatae Personal Computer — FastAPI Application

Serves:
  - Desktop dashboard SPA on / (static files with overlay)
  - File gateway on /api/files/*
  - App launcher on /api/apps/*
  - System info on /api/system/*
  - Screen capture on /api/screen/*
  - Input actions on /api/input/*
  - Clipboard on /api/clipboard/*
  - WebSocket on /ws (CAN-bus message protocol)

Security:
  - LocalhostOnlyMiddleware: rejects non-127.0.0.1 requests
  - CEIGASGateMiddleware: the operator kill switch on every mapped API request
  - Static files bypass CEIGAS (UI always served)
  - Health endpoint bypasses CEIGAS (Docker healthcheck)
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

# ---------------------------------------------------------------
# Root logging — configured HERE, before any app import builds a logger
# ---------------------------------------------------------------
# entrypoint.sh runs `python3 -m server.launch` (the dual-stack launcher),
# which builds a uvicorn Config with log_level="info".
# That setting configures UVICORN's own loggers only; nothing in server/ or brain/
# ever configured the ROOT logger, so every `log.info(...)` from mastercode.*
# fell through to Python's lastResort handler, which emits WARNING and above
# and drops the rest. Measured on a live Host: zero INFERENCE_USAGE lines
# (provider_gateway.py:124) in 2,500 log lines, while WARNINGs from that same
# module were present throughout — the per-call usage line was being written
# and thrown away. basicConfig() is a no-op once the root logger already has
# handlers, so this is safe under any harness that configured logging before
# importing the app, and it leaves uvicorn's own --log-level flag untouched.
def _resolve_log_level(name: str):
    """A LOG_LEVEL value -> its level int, or None when it names no level.

    logging.getLevelName() returns the INT for a known name and the string
    "Level <name>" for anything else (verified on this interpreter:
    getLevelName("BOGUS") -> 'Level BOGUS'), so the isinstance test is the
    whole check. Passing the raw string to basicConfig instead raises
    ValueError("Unknown level: 'BOGUS'") — an unhandled raise at module import,
    i.e. a pod that crash-loops at startup because someone typo'd an env var.
    Logging configuration must never be able to do that, so a value that
    doesn't resolve degrades to INFO and says so.
    """
    resolved = logging.getLevelName((name or "").strip().upper())
    return resolved if isinstance(resolved, int) else None


_log_level_name = os.environ.get("LOG_LEVEL", "INFO")
_log_level = _resolve_log_level(_log_level_name)
logging.basicConfig(
    level=_log_level if _log_level is not None else logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
if _log_level is None:
    # Warned AFTER basicConfig so the line rides the handler just installed.
    logging.getLogger("privatae.pc").warning(
        "LOG_LEVEL=%r names no logging level — falling back to INFO",
        _log_level_name,
    )
# The chatty third-party loggers this process actually pulls in: aiohttp (the
# provider gateway's HTTP client), httpx and its httpcore transport (brain/
# mastercode/teaching_deduplicator.py:28, brain/mastercode/runtime/
# kernel_ops.py:250) and asyncio. urllib3/requests are deliberately absent —
# neither is imported anywhere under server/ or brain/.
for _noisy in ("aiohttp", "httpx", "httpcore", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

from server.applications import app_origin
from server import static_paths
from server.middleware.localhost_only import LocalhostOnlyMiddleware
from server.middleware.ceigas_gate import CEIGASGateMiddleware
from server.routes import system, files, screen, input, clipboard, projects, ceigas, update, portal, entity, integrations, brain, security, inference, privatae_status, synaptive, mcp

logger = logging.getLogger("privatae.pc")

# Dashboard paths (overlay: custom/ overrides base/)
DASHBOARD_BASE = Path("/app/dashboard/base")
DASHBOARD_CUSTOM = Path("/app/dashboard/custom")

# Active WebSocket connections (CAN-bus subscribers)
_ws_connections: set[WebSocket] = set()

# Bridge client accessor — apps and routes call bridge operations through this.
_bridge_client_ref = None
_nac_controller_ref = None         # P0 — pool-bound host-local NAC PDP (bastion mesh)
_nac_egress_ref = None             # P6 — in-pod egress gate (forwards allowed outbound to the bastion)
_nac_egress_lane_ref = None        # v127 — the same gate off-loopback (:13129) for the entity's OTHER pods
_peer_trust_ref = None             # Workstream B — pinned bridge keys + CRL for offline slip verify
_peer_server_ref = None            # Workstream B — the :8444 direct peer-transport listener


def get_bridge_client():
    """The platform client: a NoPlatform on a standalone Host (server/bridge/no_platform.py),
    the member door's client once it has joined the network
    (server/bridge/member_client.py), or None before lifespan boot."""
    return _bridge_client_ref


def set_bridge_client(client) -> None:
    """Swap the platform client: server/bridge/membership.py calls this when the
    Host joins or leaves the network, so every caller of get_bridge_client()
    sees the change on its next call."""
    global _bridge_client_ref
    _bridge_client_ref = client



def get_nac_controller():
    """P0 — Return the pool-bound host-local NAC PDP for the bastion mesh
    (server/network/nac_controller.py). ceigas-local enforces capability; the NAC
    enforces network policy — sibling host-local enforcers. The MCP-lane PEP (P3)
    calls this before the capability gate. Returns None before lifespan boot."""
    return _nac_controller_ref


RELAY_CONFIG_PATH = Path("/etc/privatae/certs/relay.json")


_BACKFILL_INTERVAL_S = 900
# Rows that produced no vector on an earlier pass of this process. They are
# ORDERED LAST, never excluded: one row the endpoint always refuses would
# otherwise sit first in the fixed importance/recency order, trip the lane's
# 60s cooldown on every tick and starve every row behind it. Excluding them
# instead (the first cut of this) stranded every row that was merely tried
# during an endpoint OUTAGE, which cannot be told apart from a bad row here.
_backfill_fails: dict[int, int] = {}
# A failed row is not RE-TRIED by a periodic pass before this monotonic time:
# 15 min after its first failure, doubling, capped at an hour. Each failed
# call trips the lane's 60s cooldown and logs EMBEDDINGS DOWN at ERROR, so a
# row that always fails must not do that every tick; the cap bounds how late a
# row caught in an outage is recovered once the lane is back (the cap plus up
# to one pass interval, ~75 min; a restart's boot pass ignores the backoff).
_backfill_retry_at: dict[int, float] = {}
_BACKFILL_RETRY_CAP_S = 3600


async def _backfill_unembedded_loop(pool):
    """The boot pass, then the same pass every _BACKFILL_INTERVAL_S.

    Boot-only left a row that missed its vector while the pod was up stranded
    until the next restart, although _embed_mark_down tells the log such rows
    "will be backfilled by the unembedded sweep once the lane recovers".
    Measured on a live Host: three memories written 22:23-00:19 UTC
    stayed vector-less (invisible to recall, `embed-backend-up` red). The boot
    pass at 01:30 hit a 403 from the Hub peer-forward 16s after start; the lane
    answered 200 when tested two hours later, and nothing retried the rows.

    Periodic passes are quiet: they skip while the lane is in cooldown or has
    no model (the embed call would no-op anyway) and log only when they
    embedded something, so an unconfigured host is not warned at every tick.

    """
    try:
        await _backfill_unembedded_memories(pool)
    except Exception as e:   # the row SELECT has no guard of its own
        logger.warning("backfill: boot pass failed (non-fatal): %s", e)
    try:
        from mastercode.memory_engine import embedding_health
    except Exception as e:
        logger.warning("backfill loop: cannot import embedding_health (%s) — boot pass only", e)
        return
    while True:
        try:
            await asyncio.sleep(_BACKFILL_INTERVAL_S)
            if embedding_health().get("down"):
                continue
            await _backfill_unembedded_memories(pool, periodic=True)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("backfill loop: pass failed (non-fatal): %s", e)


async def _backfill_unembedded_memories(pool, *, periodic: bool = False):
    """Ship 2 (B8): re-embed entity_memory rows that landed without vectors.

    Bootstrap memories (genesis-seeded) are written without embeddings — the
    genesis INSERT omits the embedding column entirely. Transient Ollama
    failures during normal write also leave rows unembedded. Both cases make
    the row invisible to semantic recall. This pass repairs them, at boot and
    then every _BACKFILL_INTERVAL_S (_backfill_unembedded_loop).

    Bounded: caps work at 100 rows per pass so a large backlog can't stall
    the container. Remaining rows get picked up on later passes.
    Logs progress so operators have an audit trail.

    Dim-routed (2026-08-01). This pass used to hardcode the 1024d `embedding`
    column in all three statements — the count, the select, and the update.
    On an entity running a non-1024 model that is wrong twice over:

      * the UPDATE writes a vector of the model's dim into vector(1024), so
        postgres rejects every row ("expected 1024 dimensions, not 2560"), and
      * the predicate `embedding IS NULL` asks whether the *1024* column is
        empty, which stays true forever on such an entity — so even a fixed
        write would re-select the same rows on every boot and never converge.

    Both now route through active_embedding_column() / embedding_cast_type(),
    the same contract code_rag.embed_memory already follows. Measured on a
    live Host (qwen3-embedding:4b, 2560d): 714 memories had gone unembedded
    for months, burning a full BATCH_CAP of embed calls per boot and
    writing nothing.
    """
    try:
        from mastercode.memory_engine import (
            EMBEDDING_COLUMN_BY_DIM, _get_memory_embedding, active_embedding_column,
            embedding_cast_type, last_embed_model,
        )
    except Exception as e:
        logger.warning("backfill: cannot import memory_engine helpers (%s) — skipping", e)
        return

    # Resolve the pod's entity so the column matches its CONFIGURED model.
    #
    # brain_service._entity_id is NOT a safe "is it set yet" signal: it is a
    # module-level default of 1 (brain_service.py:27), never None. So an
    # `is None` guard here would be dead code, and the real hazard would go
    # unguarded — reading the default before initialize() ran resolves entity
    # 1, whose behavioral_params are absent from THIS pod's DB, which falls
    # back to the 1024d column and silently reintroduces the very bug this
    # function was fixed for.
    #
    # Ordering today is safe: lifespan awaits brain_service.initialize() at
    # main.py:438, long before this task is created at :608. But that is an
    # ordering guarantee, not a local one, so verify rather than assume:
    # require the resolved entity to actually exist in this pod's registry.
    try:
        from server import brain_service as _bs
        entity_id = getattr(_bs, "_entity_id", None)
    except Exception:
        entity_id = None
    known = None
    if entity_id is not None:
        try:
            known = await pool.fetchval(
                "SELECT 1 FROM entity_registry WHERE id = $1", entity_id)
        except Exception as e:
            logger.warning("backfill: registry probe failed (%s) — skipping", e)
            return
    if not known:
        logger.warning(
            "backfill: resolved entity_id=%r is not in this pod's entity_registry "
            "— brain_service.initialize() has not run, or ran for another entity. "
            "Skipping: picking a dim column from the wrong entity's config is how "
            "embeddings land in the wrong column.", entity_id)
        return

    try:
        col, dim = await active_embedding_column(pool, entity_id)
        cast = embedding_cast_type(dim)
    except Exception as e:
        logger.warning("backfill: could not resolve active embedding column (%s) — skipping", e)
        return
    # `col` comes from the EMBEDDING_COLUMN_BY_DIM whitelist — safe to f-string.
    if not periodic:
        logger.info("backfill: entity %s embeds at %dd -> %s (%s)", entity_id, dim, col, cast)

    # Only rows with NO vector in ANY dim column (2026-09-24). A row that has a
    # vector in another dim is not "unembedded": the host switched models, and
    # a switch NEVER re-embeds (routes/embedding.py apply_embedding_switch) — the
    # user fills the new dim with an explicit embed job, on their own provider
    # credit. Selecting on the active column alone turned every switch into an
    # unasked full re-embed. Column names come from the same whitelist.
    no_vec = " AND ".join(f"{c} IS NULL" for c in EMBEDDING_COLUMN_BY_DIM.values())
    failed_before = list(_backfill_fails)

    BATCH_CAP = 100
    try:
        total = await pool.fetchval(
            f"SELECT COUNT(*) FROM entity_memory WHERE {no_vec} "
            "AND status = 'active' AND content IS NOT NULL AND LENGTH(content) > 0"
        )
    except Exception as e:
        logger.warning("backfill: count query failed (%s) — skipping", e)
        return

    if total == 0:
        if not periodic:
            logger.info("backfill: 0 unembedded rows — nothing to do")
        return

    if not periodic:
        logger.info("backfill: %d unembedded rows found, processing up to %d this boot",
                    total, BATCH_CAP)

    rows = await pool.fetch(
        f"SELECT id, content FROM entity_memory "
        f"WHERE {no_vec} AND status = 'active' "
        "AND content IS NOT NULL AND LENGTH(content) > 0 "
        "ORDER BY (id = ANY($2::bigint[])), importance DESC NULLS LAST, "
        "created_at DESC "
        "LIMIT $1",
        BATCH_CAP, failed_before,
    )
    if total <= BATCH_CAP:
        # Every candidate was selected, so an id missing from `rows` has been
        # embedded elsewhere or archived: forget it.
        live = {r["id"] for r in rows}
        for i in [i for i in _backfill_fails if i not in live]:
            _backfill_fails.pop(i, None)
            _backfill_retry_at.pop(i, None)

    embedded = 0
    no_vector = 0   # the embed lane produced nothing (down, or no model configured)
    write_failed = 0  # a vector existed and the DB refused it (schema/dim fault)
    first_error = None
    for row in rows:
        if periodic and _backfill_retry_at.get(row["id"], 0.0) > time.monotonic():
            continue   # backing off this row; see _backfill_retry_at
        try:
            vec = await _get_memory_embedding(
                row["content"], pool=pool, entity_id=entity_id)
        except Exception as e:
            vec = None
            first_error = first_error or f"embed raised: {e}"
        if vec is None:
            # The common case, and the one that used to report
            # "first error: unknown": _get_memory_embedding returns None
            # WITHOUT raising when the entity has no embedding model
            # configured or the embed lane is in cooldown. Name it.
            first_error = first_error or (
                "embed returned no vector (no embedding model configured for "
                "this host, or the embed lane is down/cooling down)")
            no_vector += 1
            if last_embed_model():   # no model configured is not the row's fault
                n = _backfill_fails.get(row["id"], 0) + 1
                _backfill_fails[row["id"]] = n
                _backfill_retry_at[row["id"]] = time.monotonic() + min(
                    _BACKFILL_INTERVAL_S * 2 ** (n - 1), _BACKFILL_RETRY_CAP_S)
            if periodic:
                # The lane is now in cooldown (or has no model): every later row
                # would return None too. Stop, and try again next tick.
                break
            continue
        _backfill_fails.pop(row["id"], None)
        _backfill_retry_at.pop(row["id"], None)
        try:
            await pool.execute(
                f"UPDATE entity_memory SET {col} = $1::{cast}, "
                "embedding_model = $2, updated_at = now() "
                f"WHERE id = $3 AND {col} IS NULL",
                vec, last_embed_model() or None, row["id"],
            )
            embedded += 1
        except Exception as e:
            # Was logger.debug, which is why a two-month total failure ran
            # silent: the only trace was the DB-side ERROR, and pre-split
            # that sat in the entity container's log. Keep the per-row line
            # at debug (a full batch would be 100 lines) but surface the
            # first error in the summary below.
            logger.debug("backfill: update failed for memory %d (%s)", row["id"], e)
            first_error = first_error or f"write rejected: {e}"
            write_failed += 1

    failed = no_vector + write_failed
    pending = max(0, total - embedded)
    if periodic:
        # Quiet unless it did something. A standing fault is already reported
        # once at boot (below) and by EMBEDDINGS DOWN on the state change.
        if embedded:
            logger.info("backfill (periodic): %d embedded into %s, %d still pending",
                        embedded, col, pending)
        return
    if rows and not embedded:
        # Nothing landed. That is a standing fault, not the transient-Ollama
        # case this pass was written for, and it repeats identically on every
        # boot until someone looks — so it goes out at WARNING with the two
        # causes kept apart. Conflating them is what produced
        # "first error: unknown" on the single most likely case.
        logger.warning(
            "backfill: NOTHING embedded for entity %s — %d attempted "
            "(%d produced no vector, %d rejected on write to %s %s), "
            "%d still pending; first error: %s",
            entity_id, len(rows), no_vector, write_failed, col, cast,
            pending, first_error or "unknown")
    else:
        logger.info(
            "backfill: complete — %d embedded into %s, %d failed "
            "(%d no vector, %d write rejected), %d still pending",
            embedded, col, failed, no_vector, write_failed, pending)


def _schedule_desktop_snapshot():
    """Start the boot-time desktop snapshot publish (lifespan holds the task
    and cancels it on shutdown). None when it could not be scheduled: that is
    logged and boot carries on. Its own function so a test can drive the
    failure; the branch called `log`, which this module does not define, so a
    failure raised NameError out of startup instead of logging."""
    try:
        from server.routes.appearance import publish_desktop_snapshot_at_boot
        return asyncio.create_task(publish_desktop_snapshot_at_boot())
    except Exception as _ds_err:
        logger.warning("desktop snapshot boot task not scheduled: %s", _ds_err)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    # The Workbench projects dir /user-apps serves. Made here rather than at
    # import, so importing this module needs no write access to /var/lib.
    _USR_APPS.mkdir(parents=True, exist_ok=True)

    # Crash-dump housekeeping. Deliberately OUTSIDE any pool guard — it
    # touches the filesystem only and needs no database. The first version sat
    # inside `if brain_pool is not None:`, so a pod whose brain pool failed to
    # come up would ALSO silently stop bounding crash dumps — which is exactly
    # when a pod is most likely to be producing them.
    #
    # Two tasks: a sweep at boot (the only thing that catches a dump left by the
    # crash that killed the previous pod) and a periodic sweep, which covers
    # every CLI-spawning lane including ones added later. A hand-maintained list
    # of spawn sites goes stale — the first cut of this change hooked a function
    # with no production caller and left the real lane uncovered.
    import asyncio as _asyncio
    from server import crash as _crash
    _crash_tasks = [
        _asyncio.create_task(_crash.sweep_at_boot()),
        _asyncio.create_task(_crash.sweep_forever()),
    ]


    entity_id = int(os.environ.get("ENTITY_ID", "0"))
    logger.info("Personal computer online — entity %d", entity_id)

    # --- CEIGAS domain controller initialization ---
    # Read relay config for entity identity if available
    relay_entity_id = entity_id
    if RELAY_CONFIG_PATH.exists():
        try:
            relay_cfg = json.loads(RELAY_CONFIG_PATH.read_text())
            relay_entity_id = int(relay_cfg.get("entity_id", entity_id))
            logger.info(
                "Relay config loaded: entity_id=%d relay_id=%s",
                relay_entity_id, relay_cfg.get("relay_id", "unknown"),
            )
        except Exception as e:
            logger.warning("Failed to read relay config: %s — using env ENTITY_ID", e)

    # --- Brain service: local entity brain with embedded PG ---
    from server import brain_service
    brain_pool = None
    try:
        await brain_service.initialize(entity_id=relay_entity_id)
        brain_pool = brain_service.get_pool()
        logger.info("Brain service ACTIVE — entity %d, local PG", relay_entity_id)

        # The owner's login (server/owner_login.py): loads the owner, or prints
        # the one-time setup code when there is none yet.
        from server import owner_login
        await owner_login.init()

        # The git server the owner connected in Settings, back from its
        # settings row and Keys, and ~/.git-credentials written from it, before
        # anything reads the Host's coding forge (server/git_server.py).
        # Never raises.
        from server import git_server
        await git_server.load(brain_pool, relay_entity_id)

        # The credential audit's retention bound: routine reads older than 30
        # days become daily counts in vault_audit_rollup, counted and removed
        # in one statement per batch (server/vault/audit_rollup.py). Hourly,
        # first pass ten minutes after boot.
        from server.vault.audit_rollup import run_vault_audit_rollup_loop
        asyncio.create_task(run_vault_audit_rollup_loop(brain_pool))

        # Words the person sent into a running turn that the last process held
        # (pending_words) and no turn took — the process ended first: each
        # conversation's start a turn there, once. Only rows from before this
        # process (the database's clock, read now, before any request is
        # taken), so words this process receives are never handed twice.
        try:
            from mastercode import pending_words as _pending_words
            _pw_before = await brain_pool.fetchval("SELECT clock_timestamp()")
            asyncio.create_task(_pending_words.deliver_left_loop(
                brain_pool, relay_entity_id, _pw_before))
        except Exception as _pw_err:  # noqa: BLE001
            logger.error("pending words: the boot pass was not started: %s", _pw_err)

        # (F3 removed 2026-08-22.) This injected the pool-bound controller into
        # bot_executor so BOT execution's bots.execution crossing enforced real
        # grants in-pod. The bot lane is retired and that module is gone, so the
        # injection had no consumer — and being best-effort it would have logged
        # a warning on every startup forever. The bots.execution DOMAIN itself
        # is untouched: its grant still maps to meta-tool scopes
        # (brain/mastercode/runtime/meta_tool_caps.py). Its other caller,
        # ceigas_bash in server/ceigas/ceigas_mcp.py, was removed 2026-09-27
        # with the claude-code app's in-container session route.

        # Wire the memory-engine retrieval hook so live memory-firing events
        # flow to the dashboard WebSocket. Best-effort — never fatal.
        try:
            from mastercode import memory_engine as _me

            async def _on_memory_fire(ids: list, source: str, context: dict):
                await broadcast({
                    "type": "memory_firing",
                    "module": "memory",
                    "data": {
                        "ids": ids,
                        "source": source,
                        "context": context,
                    },
                })

            _me.set_retrieve_hook(_on_memory_fire)
            logger.info("Memory firing hook wired — dashboard WS will light up on recall")
        except Exception as e:
            logger.warning("Could not wire memory-firing hook: %s", e)
    except Exception as e:
        logger.error("Brain service failed to initialize: %s", e)
        logger.info("Continuing without local brain — relay proxy still available")

    # --- Process supervisor for sidecar apps ---
    from server.applications.supervisor import Supervisor, set_supervisor
    set_supervisor(Supervisor())

    # --- Kernel boot: trust store + factory install + native check ---
    # Outside the brain try/except: refuse-to-boot lives here. The boot
    # sequence is:
    #   1. seed the Privatae signing key into the trust store
    #   2. install every signed factory bundle (extracts to
    #      /var/lib/cseries/apps/<id>/, registers in installed_applications)
    #   3. verify every kernel-required native id is in the registry
    #   4. load the kernel secret used to sign per-window app tokens
    if brain_pool is not None:
        from server.applications import installer, trust_store
        from server.applications import registry as apps_registry
        from server.applications.kernel import NATIVE_APPS, RETIRED_APPS
        from server.applications.registry import list_active
        from server.applications.tokens import get_secret as get_kernel_secret

        await trust_store.seed_cseries_key(brain_pool)
        await trust_store.seed_community_key(brain_pool)

        boot = await installer.install_native_bundles(brain_pool)
        if boot["installed"]:
            logger.info("Factory installed: %s", boot["installed"])
        if boot["upgraded"]:
            logger.info("Factory upgraded: %s", boot["upgraded"])
        if boot["failed"]:
            logger.warning("Factory install failed: %s", boot["failed"])

        # Prune registry rows whose on-disk install dir is gone. After
        # install_native_bundles the installed_applications schema is
        # guaranteed to exist. Catches the drift between PG and the
        # filesystem that older pods exhibited (/var/lib/cseries/apps
        # was ephemeral, image rolls wiped installed apps while the rows
        # lived on in PG). The apps volume is mounted now, but the prune is
        # still a cheap safety net.
        drift = await installer.prune_orphan_registry_rows(brain_pool)
        if drift["pruned"]:
            logger.warning("Pruned orphan registry rows: %s", drift["pruned"])

        # Clean up `.stash-<epoch>` install-dir leftovers from `_force_clear`'s
        # rename-aside fallback. Without this the PVC accumulates them
        # forever.
        stash = installer.prune_stash_dirs()
        if stash["removed"]:
            logger.info("Pruned stale install-dir stashes: %s", stash["removed"])
        if stash["failed"]:
            logger.warning("Stash prune partial failure: %s", stash["failed"])

        # Retire culled apps (kernel.RETIRED_APPS). Without this, an app
        # deleted from the tree lingers as a zombie install on every
        # pre-existing entity — registry row + PVC files + launcher entry.
        # Uninstall only (no purge): config rows and workspace survive
        # orphaned, same as a user uninstall. Fail-soft per id — a
        # retirement error must never block boot; it retries next boot.
        retired: list[str] = []
        for retired_id in sorted(RETIRED_APPS):
            try:
                if await apps_registry.get(brain_pool, retired_id) is None:
                    continue
                await installer.uninstall_app(brain_pool, retired_id)
                retired.append(retired_id)
            except Exception:
                logger.warning(
                    "Retired-app sweep failed for %s (will retry next boot)",
                    retired_id, exc_info=True,
                )
        if retired:
            logger.info("Retired apps removed: %s", retired)

        # Resident (always-on) process apps come up with the kernel. After the
        # factory install + prune + retire passes so the registry rows and
        # install dirs they read are final. Fail-soft per app inside; a
        # failure here must never block boot (the sweep logs and moves on).
        # As a TASK, not awaited: ensure_resident_apps starts each app in turn
        # and waits for its readiness (up to ready_timeout_seconds each), so
        # awaiting it here would add N × that to kernel boot and hold the
        # server socket closed while daemons come up.
        try:
            from server.applications.supervisor import get_supervisor as _get_sup
            _sup = _get_sup()
            if _sup is not None:
                asyncio.create_task(_sup.ensure_resident_apps(brain_pool))
        except Exception:
            logger.warning("Resident-app boot sweep could not be scheduled "
                           "(apps start on demand)", exc_info=True)

        installed = await list_active(brain_pool)
        installed_ids = {a.id for a in installed}
        missing = sorted(n for n in NATIVE_APPS if n not in installed_ids)
        if missing:
            raise RuntimeError(
                f"Kernel refuse-to-boot: missing native apps {missing}"
            )

        app.state.kernel_secret = await get_kernel_secret(brain_pool)
        logger.info(
            "Applications kernel up — natives present, %d apps installed, "
            "trust store seeded, token secret loaded",
            len(installed),
        )

        # App SDK floor (Phase 0): install the image-baked, Privatae-signed SDK
        # if none is installed, then integrity self-check. Best-effort — if it
        # fails, isolated apps simply serve un-isolated (the injector skips
        # rather than load an unverifiable runtime).
        try:
            from server.applications import sdk_store
            sdk_meta = sdk_store.ensure_installed()
            if sdk_meta:
                logger.info(
                    "App SDK ready — v%s (sri %s…)",
                    sdk_meta["version"], sdk_meta["sri"][:16],
                )
            else:
                logger.warning("App SDK NOT available — isolated apps serve un-isolated")
        except Exception as e:
            logger.warning("App SDK ensure_installed error (non-fatal): %s", e)

        # Ship 2 (B8): backfill embeddings for memories that landed without
        # vectors (bootstrap-time cold-Ollama races, transient embed failures).
        # Fire-and-forget — does not block lifespan completion. Bounded so a
        # large backlog can't stall the container on every restart. Repeats
        # every _BACKFILL_INTERVAL_S (2026-09-24), not only at boot.
        asyncio.create_task(_backfill_unembedded_loop(brain_pool))

        # Not started on a standalone Host: the snapshot goes to the platform's
        # account grid, and there is no platform.
        _desktop_snapshot_task = None

        # Conversation-depth (2026-08-15): warm the model catalog once so the
        # turn path knows every model's context window from the first turn.
        # Before this the cache filled only when a browser opened a model
        # picker — voice/cron/peer/API traffic on a fresh pod ran unbudgeted.
        # Fire-and-forget; a failed fetch leaves 'window unknown' as before.
        try:
            from server.routes.inference import warm_model_catalog
            asyncio.create_task(warm_model_catalog())
        except Exception as _wc_err:
            logger.warning("model catalog warm dispatch failed (non-fatal): %s", _wc_err)

        # The thread audit (mastercode.thread_audit): deterministic, no model.
        # Memory forms in the turn (close_turn, memory_curate); the batch
        # consolidator and its loop are retired.
        try:
            from mastercode import thread_audit as _ta
            asyncio.create_task(_ta.run_audit_loop(brain_pool, 1800))
        except Exception as _ta_err:
            logger.warning("thread audit dispatch failed (non-fatal): %s", _ta_err)

        # (The constitution rule promoter's 6h loop was retired 2026-09-30:
        # it graduated recurring behavioral corrections to hard rules, and
        # nothing has written a correction since 2026-05-23.)

        # Embed JobRunner: processes embed_jobs rows — re-embeds on a model
        # switch (reembed_model_switch) and first-fill backfills, with durable
        # progress + resume. set_runner() exists and enqueue_job already calls
        # _runner.wake(), but nothing ever instantiated/started the runner, so
        # every embed job sat 'pending' forever (re-embeds + backfills never
        # processed). Wire it here, inside the brain-pool branch (it uses the
        # brain pool via brain_service.get_pool()).
        try:
            from server.routes.embed import JobRunner, set_runner
            _embed_runner = JobRunner()
            await _embed_runner.start()
            set_runner(_embed_runner)
        except Exception as _er_err:
            logger.warning("Embed JobRunner start failed (non-fatal): %s", _er_err)

        # B2: per-run Gitea token reaper. The harness lane mints a SCOPED token per run and
        # revokes it in a finally — but a crash between mint and revoke (or a failed revoke
        # HTTP) orphans a scoped token on Gitea forever. This sweeps orphans (the token name
        # carries its mint epoch) on a 30-min cycle. No-op on hosts with no gitea credential.
        try:
            from server.network.nac_harness import gitea_reap_orphans

            async def _harness_token_reaper():
                while True:
                    try:
                        await asyncio.sleep(1800)
                        n = await gitea_reap_orphans()
                        if n:
                            logger.info("harness gitea reaper: revoked %d orphaned per-run token(s)", n)
                    except asyncio.CancelledError:
                        return
                    except Exception as _re:
                        logger.warning("harness gitea reaper cycle error (non-fatal): %s", _re)

            asyncio.create_task(_harness_token_reaper())
        except Exception as _hr_err:
            logger.warning("harness gitea reaper dispatch failed (non-fatal): %s", _hr_err)

        # Claude SDK proactive token refresh (2026-07-06, the dead-chat
        # incident): the subscription token only refreshed LAZILY mid-turn, so
        # an expiry plus one slow refresh = dead chat on every surface while
        # Connections still read "connected". Refresh server-side while the
        # token is still fresh (<45 min left), 10-min cadence, same
        # non-fatal discipline as the reapers above. No-ops with no credential.
        try:
            async def _sdk_token_refresher():
                from pathlib import Path as _P
                import json as _j
                import time as _t
                _creds = _P("/home/privatae/.claude/.credentials.json")
                while True:
                    try:
                        await asyncio.sleep(600)
                        if not _creds.exists():
                            continue
                        oauth = (_j.loads(_creds.read_text()).get("claudeAiOauth") or {})
                        exp = oauth.get("expiresAt")
                        if not oauth.get("refreshToken") or not exp:
                            continue
                        if int(exp) - int(_t.time() * 1000) > 45 * 60 * 1000:
                            continue
                        from mastercode.runtime.tools import _exec_system_refresh_sdk_auth
                        out = await _exec_system_refresh_sdk_auth({})
                        logger.info("sdk token refresher: %s", (out or {}).get("status"))
                    except asyncio.CancelledError:
                        return
                    except Exception as _te:
                        logger.warning("sdk token refresher cycle error (non-fatal): %s", _te)

            asyncio.create_task(_sdk_token_refresher())
        except Exception as _tr_err:
            logger.warning("sdk token refresher dispatch failed (non-fatal): %s", _tr_err)

        # Workbench P4: harness_run curation sweep. Runs land in harness_run in
        # FULL (persist-all); this bounded pass keeps committed variants and
        # strips the junk's bulk after the grace window. Deterministic
        # thresholds only — no LLM in the loop. 30-min cycle, same discipline
        # as the token reaper above.
        try:
            from server.routes.compute import curate_harness_runs

            async def _harness_run_curator():
                while True:
                    try:
                        await asyncio.sleep(1800)
                        out = await curate_harness_runs(brain_service.get_pool())
                        if out.get("kept") or out.get("archived"):
                            logger.info("harness_run curator: kept %d, archived %d",
                                        out["kept"], out["archived"])
                    except asyncio.CancelledError:
                        return
                    except Exception as _ce:
                        logger.warning("harness_run curator cycle error (non-fatal): %s", _ce)

            asyncio.create_task(_harness_run_curator())
        except Exception as _cu_err:
            logger.warning("harness_run curator dispatch failed (non-fatal): %s", _cu_err)

        # S1 SURGICAL: offline-task executor. Runs the host's SCHEDULED tasks
        # (calendar_events event_type='scheduled_task') WITHOUT booting the full
        # autonomous brain — a single guarded poll loop that atomically claims
        # each due task, runs its prompt through the governed turn pipeline
        # (brain_service.send_message), reports to Circles, and re-arms recurring
        # tasks. Gated on a real entity (genesis entity-0 has no schedule).
        #
        # IF YOU ADD A CONDITION HERE, READ schedule_task's DESCRIPTION FIRST.
        # That description (brain/mastercode/public_tool_registry.py) tells the
        # Host — in the text a model reads before choosing the tool — that a
        # fired task IS auto-run and that there is no per-entity rollout flag.
        # It said the opposite for months and talked the Host out of a
        # capability it had. tests/test_schedule_task_doc_truth.py catches a
        # direct edit of the line below, but an OUTER wrapping gate, or one
        # inside the runner, sails past it — so this comment, not that test, is
        # the durable protection.
        if relay_entity_id > 0:
            try:
                from mastercode.brain.offline_task_runner import run_offline_tasks_loop  # offline_task_runner.py:366
                asyncio.create_task(run_offline_tasks_loop(brain_pool, relay_entity_id))
                logger.info("offline-task runner loop dispatched (entity %d)", relay_entity_id)
            except Exception as _ot_err:
                logger.warning("offline-task runner dispatch failed (non-fatal): %s", _ot_err)

            # The weekly "state of the host" changelog is a PLATFORM DEFAULT:
            # every host gets one standing op that
            # reads its own week and writes one Inbox message, no model. Seeded
            # here, idempotently, because a new user cannot ask for what they do
            # not know exists; a task the user stopped is never re-seeded
            # (runtime/host_changelog.py ensure_weekly_changelog_task).
            try:
                from mastercode.runtime.host_changelog import ensure_weekly_changelog_task
                asyncio.create_task(ensure_weekly_changelog_task(brain_pool, relay_entity_id))
                logger.info("weekly host changelog seed dispatched (entity %d)", relay_entity_id)
            except Exception as _hc_err:
                logger.warning("host changelog seed dispatch failed (non-fatal): %s", _hc_err)

            # The oversight ingest is the same kind of platform default:
            # one standing op, every five
            # minutes, folding what this host's machines already report into
            # the findings ledger. No model, no tokens. Seeded here for the
            # same reason the changelog is — a user managing machines cannot
            # ask for a ledger they do not know exists — and idempotently, so a
            # task they stopped from the Agents app is never re-seeded.
            #
            # NOT SEEDED HERE: the 15-minute model turn that correlates across
            # machines (§2c). That one spends, so it is an owner's decision
            # after a measured day, and nothing here creates it.
            try:
                from mastercode.runtime.oversight import ensure_oversight_ingest_task
                asyncio.create_task(ensure_oversight_ingest_task(brain_pool, relay_entity_id))
                logger.info("oversight ingest seed dispatched (entity %d)", relay_entity_id)
            except Exception as _ov_err:
                logger.warning("oversight ingest seed dispatch failed (non-fatal): %s", _ov_err)

            # The support queue's collect (2026-09-24, one ticket system two
            # shapes §3): the same standing-op shape, for an OPERATOR host
            # only — ensure_collect_task writes nothing for any other entity,
            # and the platform would refuse the op anyway. Replaces an operator
            # timer that mirrored platform_tickets into an operator ledger.
            try:
                from mastercode.runtime.support_collect import ensure_collect_task
                asyncio.create_task(ensure_collect_task(brain_pool, relay_entity_id))
                logger.info("support collect seed dispatched (entity %d)", relay_entity_id)
            except Exception as _sc_err:
                logger.warning("support collect seed dispatch failed (non-fatal): %s", _sc_err)

            # Reach-out: the
            # subscription on the inbox bus. Polls new inbox rows against this
            # host's enabled triggers and fires the matching channel once.
            # Beside the offline runner, for the same reason and with the same
            # guards: a background loop that never raises, gated on a real
            # entity (genesis entity-0 has no person to reach).
            #
            # A host with no triggers configured — every host until the user
            # connects Gmail (which builds the default channel and its three
            # triggers, reachout/defaults.py) or asks for one — costs one
            # indexed query per 30s tick and nothing else.
            try:
                from server.reachout.dispatcher import run_reachout_dispatcher_loop
                asyncio.create_task(run_reachout_dispatcher_loop(brain_pool, relay_entity_id))
                logger.info("reach-out dispatcher dispatched (entity %d)", relay_entity_id)
            except Exception as _ro_err:
                logger.warning("reach-out dispatcher dispatch failed (non-fatal): %s", _ro_err)

            # Code search moved from a SQLite file into this database (v200).
            # The old file's projects are indexed here once, then the file is
            # removed; with no file this is one stat() and nothing else. A lane
            # that is down now (the bridge below is not connected yet) is
            # retried in the background until it answers.
            try:
                from codesearch import indexer as _cs_indexer
                asyncio.create_task(_cs_indexer.retire_sqlite_index_until_done(brain_pool))
                logger.info("code search SQLite retirement dispatched (entity %d)", relay_entity_id)
            except Exception as _cs_err:
                logger.warning("code search SQLite retirement dispatch failed (non-fatal): %s", _cs_err)

            # Every repo this Host can read on its gitea is in its code index
            # (read access means index): a pass shortly after
            # start, then every few minutes, and at once on a grant or revoke
            # in the Gitea app. codesearch/repo_sync.py.
            try:
                from codesearch import repo_sync as _cs_repo_sync
                asyncio.create_task(_cs_repo_sync.run_forever(brain_pool))
                logger.info("code search repo sync dispatched (entity %d)", relay_entity_id)
            except Exception as _cs_err:
                logger.warning("code search repo sync dispatch failed (non-fatal): %s", _cs_err)

        # Process awareness: sample /proc so the host can answer "what is
        # running on my machine, and has it done anything lately" with a
        # measurement instead of an impression, and notify when something heavy
        # has gone idle that was never part of the baseline.
        #
        # Deliberately NOT gated on the pool or the entity id like the loops
        # around it: this reads /proc and one stats file and touches no
        # database, which is exactly what lets the support lane attach its
        # report when the database is the fault being reported.
        try:
            from server.process_awareness import run_sampler_loop
            asyncio.create_task(run_sampler_loop())
            logger.info("process awareness loop dispatched")
        except Exception as _pa_err:
            logger.warning("process awareness dispatch failed (non-fatal): %s", _pa_err)

        # Phase 8 (decentralized context network): replica-reconciling sweeper.
        # store_replicated does write-time fan-out ONLY — a replica box that was
        # offline at write time is skipped, so a 'replicate' file can hold fewer
        # copies than the policy wants. This guarded loop periodically reconciles
        # that drift: it scans context_backend_locations for incomplete replicate
        # files and ADDS the missing copies to now-online context-store bastions
        # (non-destructive, idempotent, best-effort per file — never crashes the
        # lifespan). The pg / single default path has no location rows, so it's
        # untouched. Gated on a real entity (genesis entity-0 has no mesh).
        if relay_entity_id > 0:
            try:
                from mastercode.brain.replica_reconciler import run_replica_reconciler_loop
                asyncio.create_task(run_replica_reconciler_loop(brain_pool, relay_entity_id))
                logger.info("replica reconciler loop dispatched (entity %d)", relay_entity_id)
            except Exception as _rr_err:
                logger.warning("replica reconciler dispatch failed (non-fatal): %s", _rr_err)

        # Ship each new backup to the destination the user
        # named (their S3, or their own machine via the bastion). Watches the
        # Host's own backup runs (server/host_backup.py) — so the cadence is the
        # user's own schedule and this loop needs to know nothing about
        # schedules. Idempotent on
        # (export_id, destination): a restart cannot re-upload bytes already
        # there. Same guarded shape as the reconciler above, gated on a real
        # entity for the same reason. Without it nothing shipped automatically
        # and the Backup tab's promise of a second copy was false.
        if relay_entity_id > 0:
            try:
                from server.backup_ship import run_backup_ship_loop
                asyncio.create_task(run_backup_ship_loop(brain_pool, relay_entity_id))
                logger.info("backup ship worker dispatched (entity %d)", relay_entity_id)
            except Exception as _bs_err:
                logger.warning("backup ship dispatch failed (non-fatal): %s", _bs_err)

        # The ship runner (server/ships/runner.py): every active run of a
        # pipeline moves on from the receipts its machines sign, with no model
        # in the path. Idle, it reads one short query a tick.
        if relay_entity_id > 0:
            try:
                from server.ships.runner import run_ships_loop
                asyncio.create_task(run_ships_loop(brain_pool, relay_entity_id))
                logger.info("ship runner dispatched (entity %d)", relay_entity_id)
            except Exception as _sr_err:
                logger.warning("ship runner dispatch failed (non-fatal): %s", _sr_err)

        # A restore swapped in at this boot (server/host_restore.py): the kernel
        # got this far on the restored data, so it is the Host now and is never
        # undone; finishing (dropping what was set aside, recording it) follows
        # in the background and is retried. Recorded HERE, not in the backup
        # loop: a Host that cannot back up (no age, no key) must still confirm a
        # restore, or its boot counter would put the old data back.
        try:
            from server import host_restore as _hr
            if _hr.mark_started():
                logger.info("restore: the kernel started on the restored data")
            if relay_entity_id > 0:
                asyncio.create_task(_hr.finish_soon(brain_pool, relay_entity_id))
        except Exception as _hr_err:
            logger.warning("restore: confirming a restore failed (non-fatal): %s", _hr_err)

        # The Host backs itself up (server/host_backup.py): a backup is a
        # Host op, encrypted on the Host to keys only the user
        # holds; the platform cannot open it. Same guarded shape as the ship
        # worker above, gated on a real entity for the same reason.
        if relay_entity_id > 0:
            try:
                from server.host_backup import run_host_backup_loop
                asyncio.create_task(run_host_backup_loop(brain_pool, relay_entity_id))
                logger.info("host backup loop dispatched (entity %d)", relay_entity_id)
            except Exception as _hb_err:
                logger.warning("host backup dispatch failed (non-fatal): %s", _hb_err)

        # Hub relay cutover: converge the pod-Hub's WireGuard peer set to this
        # host's enrolled bastions. The Hub's peer state is in-memory — a pod
        # roll or sidecar restart forgets every bastion and their handshakes get
        # dropped until re-pushed; this loop is the re-push (and it retires
        # peers whose bearer was revoked/expired). Quiet no-op when there is no
        # Hub to talk to (genesis, old pods without the sidecar).
        try:
            from server.network.hub_peers import reconcile_hub_peers_loop
            asyncio.create_task(reconcile_hub_peers_loop(brain_pool))
            logger.info("hub bastion-peer reconcile loop dispatched")
        except Exception as _hp_err:
            logger.warning("hub peer reconcile dispatch failed (non-fatal): %s", _hp_err)

        # Memory-mirror slots. A physical replication slot holds WAL
        # on this entity's volume on behalf of ONE bastion, so it has to die
        # with that bastion — a revoked box whose slot outlived it would keep
        # pinning WAL until the retention bound sheds it. Same reason and the
        # same shape as the hub-peer loop above. Gated on a real entity;
        # genesis entity-0 has no bastions and therefore no slots.
        if relay_entity_id > 0:
            try:
                from server.network.mirror import reconcile_mirror_loop
                asyncio.create_task(reconcile_mirror_loop(brain_pool, relay_entity_id))
                logger.info("mirror slot reconcile loop dispatched (entity %d)",
                            relay_entity_id)
            except Exception as _mr_err:
                logger.warning("mirror reconcile dispatch failed (non-fatal): %s", _mr_err)

        # Bastions move to each published version on their own, behind the
        # canary ring (server/network/bastion_autoupdate.py). Gated on a
        # real entity for the same reason as the two loops above.
        if relay_entity_id > 0:
            try:
                from server.network.bastion_autoupdate import run_loop as _bau_loop
                asyncio.create_task(_bau_loop(brain_pool, relay_entity_id))
                logger.info("bastion auto-update loop dispatched (entity %d)", relay_entity_id)
            except Exception as _bau_err:
                logger.warning("bastion auto-update dispatch failed (non-fatal): %s", _bau_err)
    else:
        logger.warning("Skipping applications boot — brain pool unavailable")

    # --- The platform client: none, or the network's member door ---
    # A Host with a passport on its state volume (it joined the network in
    # Settings -> Network) gets the member door's client, started here: outbound
    # only, the network's own ops only (server/bridge/member_client.py).
    # Without one, everything that reached the platform through
    # get_bridge_client() gets a NoPlatform (server/bridge/no_platform.py): it
    # answers every request with the not-connected envelope and accepts nothing
    # inbound. Joining or leaving later swaps it through set_bridge_client.
    # Either way the Host's own entity id stays what it is; the network's id
    # for it lives in the passport and is shown in Settings, never sent.
    from server.bridge import membership as _membership
    try:
        await _membership.boot(set_bridge_client)
    except Exception as _mem_err:
        logger.warning("network membership did not start (%s) — running standalone", _mem_err)
        from server.bridge.no_platform import NoPlatform
        set_bridge_client(NoPlatform())

    # The local runtime on this Host's machine starts its apps' and engines'
    # containers (server/applications/local_runtime.py). Re-send every
    # installed app's runtime descriptor and every connected engine to it once
    # it answers, as the platform's boot reconciler does for the operator.
    from server.applications.local_runtime import in_use as _rt_in_use
    if _rt_in_use():
        from server.applications import harness_sync as _harness_sync
        from server.applications.local_runtime import runtime_client as _rt_client
        asyncio.create_task(_harness_sync.run_loop(_rt_client()))

    # The App Store app is not baked into the image, and it is the only way a
    # person adds any other app, so the catalog marks it default and the Host
    # installs it at its first start. The same pass upgrades the apps installed
    # from the store. The client is looked up at each call (None here), so it
    # is the network's door for a member and the signed public catalog for a
    # standalone Host (applications/public_store.py).
    from server.applications import default_install as _default_install
    asyncio.create_task(_default_install.run_loop(None))

    # The first-run identity (server/local_identity.py) when the row is still
    # the init-db placeholder; a no-op afterwards. State surfaces in
    # /api/system/health/deep as the "identity" check.
    from server.bridge.identity_seed import run_identity_seed
    asyncio.create_task(run_identity_seed(relay_entity_id))

    # The in-pod MCP proxy (loopback socket): memory search, code search, and
    # the kill-switch answer for ceigas_authorize, all served in-process.
    from server.bridge import mcp_proxy
    from server import brain_service as _brain_service_module
    await mcp_proxy.start_proxy(brain_pool, _brain_service_module)
    logger.info("MCP proxy listening on /tmp/ceigas-bridge.sock (local-binding mode)")

    # P0 (2026-06-20): host-local NAC PDP for the bastion mesh. Sibling enforcer to
    # ceigas-local — ceigas gates CAPABILITY, the NAC gates NETWORK POLICY — both
    # bound to the host's own database pool. Pool-bound + initial policy load here;
    # the MCP-lane PEP reaches it via get_nac_controller(). Behavior-neutral
    # until the user authors policy (default-preserve). A failure here does not stop
    # the Host starting, but with no engine built the gate refuses every machine with
    # a device fingerprint (server/routes/mcp.py _nac_gate), and the Network app and
    # /api/system/health/deep say so.
    try:
        from server.network.nac_controller import NACController
        global _nac_controller_ref, _nac_egress_ref, _nac_egress_lane_ref
        _nac_controller_ref = NACController(pool=brain_pool, entity_id=relay_entity_id)
        _nac_bound = await _nac_controller_ref.attach_pool(brain_pool)
        _nac_status = await _nac_controller_ref.status()
        logger.info(
            "NAC PDP %s — %d policy rule(s), %s",
            "bound" if _nac_bound else "NOT bound (genesis/no pool — default-preserve)",
            _nac_status["rules"],
            "INTEGRITY FAIL — tampered policy, mesh fails closed" if _nac_status["tamper"]
            else ("default-preserve (no policy authored)" if _nac_status["default_preserve"]
                  else "policy active"),
        )
        # P5: start the liveness reaper (CoA — downgrade stale bastions' posture so the
        # PDP re-decides; non-destructive, recovers on the next heartbeat).
        from server.network.nac_controller import run_reaper_loop as _nac_reaper
        asyncio.create_task(_nac_reaper(brain_pool))
        # P6: start the in-pod egress gate — checks the user's signed egress policy, then
        # forwards allowed outbound to the host's bastion, which exits the user's OWN
        # connection. Default-open; the platform's lane (inference + its services over the
        # bridge) is untouched. Best-effort: a failure leaves egress un-gated, never cut.
        from server.network.nac_egress import (
            start_egress_proxy as _start_egress, EGRESS_PROXY_PORT as _egress_port,
            start_egress_lane as _start_lane, EGRESS_LANE_PORT as _lane_port)
        _nac_egress_ref = await _start_egress(brain_pool, relay_entity_id)
        logger.info(
            "NAC egress gate up (127.0.0.1:%d) — default-open; allowed outbound exits via the user's bastion",
            _egress_port)
        # v127 APP LANE: the same policy engine, off-loopback, for the entity's OTHER
        # pods (app runtimes and harnesses — the Browser is one, and can be on a
        # different node). Callers there must name themselves with a per-subject
        # credential; loopback callers are the pod itself and need none. Inert until a
        # rule names a subject, so a bind failure is logged and never fatal.
        _nac_egress_lane_ref = await _start_lane(brain_pool, relay_entity_id)
        if _nac_egress_lane_ref is not None:
            logger.info("NAC egress app lane up ([::]:%d) — subject-authenticated; "
                        "pinned rules route from other pods", _lane_port)
    except Exception as e:
        if _nac_controller_ref is None:
            logger.error("NAC PDP did not load, so the MCP gate refuses every machine with a "
                         "device fingerprint until a start where it loads: %r", e)
            # The log is not where the person looks: the inbox says it too, once per start.
            try:
                from server.routes.mcp import tell_no_engine
                await tell_no_engine()
            except Exception as _tell_err:
                logger.warning("NAC: the rules-not-loaded notice could not be sent: %r",
                               _tell_err)
        else:
            logger.warning("NAC startup step failed after the PDP was built (the PDP "
                           "runs): %r", e)

    # DESTRUCTIVE credential cutover (2026-06-25): retire the legacy plaintext
    # connections store (NaCl `*.bin` blobs + the PLAINTEXT master key under
    # /app/secrets) now that the asyncpg pool + host vault are up. migrate→VERIFY→
    # delete, with the verify-before-delete gate as the load-bearing safety: a host
    # with real legacy creds NEVER loses them — any unverified ref holds ALL deletion
    # and the legacy fallback keeps working. Idempotent + safe to re-run every boot;
    # an empty legacy store is a clean no-op. Never fatal — wrapped so a cutover hiccup
    # cannot block the lifespan (and it degrades to no-delete internally anyway).
    if brain_pool is not None and relay_entity_id > 0:
        # The integrations store moved from a SQLite file into this database
        # (v201). Any rows the file still holds are copied once, counted, and
        # the file removed; it is kept if anything did not arrive. Awaited, and
        # first: the cutover below and the sync scheduler read connections.
        # With no file this is one stat(); it never raises.
        from server.integrations.core import storage as _integ_storage
        logger.info("Integrations SQLite retirement: %s",
                    await _integ_storage.retire_sqlite_store(brain_pool))
        try:
            from server.integrations.core import credentials as _integ_creds
            _cut = await _integ_creds.cutover_legacy_cred_store(brain_pool, relay_entity_id)
            logger.info("Legacy cred-store cutover: %s", _cut)
        except Exception as e:
            logger.warning("Legacy cred-store cutover error (non-fatal; legacy kept): %s", e)

        # Harness-OAuth boot-materialize (2026-06-25): make the plaintext Claude creds
        # file the in-pod CLI reads exist after a restart, with the host-owned vault as
        # the cross-restart authority. If the plaintext is ABSENT but the vault has
        # "harness:claude_oauth", rebuild the plaintext from the vault; if the plaintext
        # is present but the vault is empty, prime the vault. NEVER deletes the runtime
        # file. This is the safety net the 'stop persisting plaintext' fork depends on.
        try:
            from server.network import nac_harness as _nac_harness
            _mat = await _nac_harness.materialize_agent_creds(brain_pool, relay_entity_id)
            logger.info("Harness OAuth boot-materialize: %s", _mat)
        except Exception as e:
            logger.warning("Harness OAuth boot-materialize error (non-fatal): %s", e)

        # SEAL-COVERAGE sweep (2026-06-26): boot-materialize only self-heals the vault at
        # the NEXT boot, so a host that connected by a path that misses the seal hooks — a
        # raw `claude` CLI login in the pod, or a CLI-native token refresh — can leave
        # "harness:claude_oauth" empty/stale between boots. This guarded background loop
        # re-runs the reconcile every HARNESS_SEAL_SWEEP_INTERVAL_S (default 300s), so the
        # vault primes/updates within one interval of ANY connect/refresh, regardless of
        # method. Prerequisite for dropping the PVC-persisted plaintext (Phase 5 Part B).
        # Mirrors the offline-task / replica-reconciler dispatch: guarded, non-fatal, the
        # loop self-heals + exits cleanly on shutdown — it can't crash the lifespan.
        try:
            from server.network.nac_harness import run_seal_sweep_loop as _seal_sweep
            asyncio.create_task(_seal_sweep(brain_pool, relay_entity_id))
            logger.info("Harness OAuth seal-sweep loop dispatched (entity %d)", relay_entity_id)
        except Exception as e:
            logger.warning("Harness OAuth seal-sweep dispatch failed (non-fatal): %s", e)

        # Non-Claude harness boot-materialize (2026-06-25): same cross-restart
        # vault authority for codex/cursor/grok. If a harness cred file is
        # ABSENT but the vault holds its envelope, rebuild the runtime file; if the
        # file is present but the vault is empty, seal-prime. NEVER deletes a file,
        # NEVER overwrites a present file. Best-effort; isolated per harness.
        try:
            from server.routes import harness_vault as _harness_vault
            _hmat = await _harness_vault.materialize_all_harness_creds()
            logger.info("Non-Claude harness boot-materialize: %s", _hmat)
            # Canvas pivot: Keys-app credential ingest. Read each harness's
            # conventioned Keys secret ('harness-<id>') and materialize it into
            # the shared creds PVC — the ingest that REPLACES the removed connect
            # flow. Runs AFTER the legacy materialize so Keys is authoritative; a
            # periodic loop propagates a mid-session key add without a pod roll.
            from server.network import harness_keys as _harness_keys
            _kmat = await _harness_keys.materialize_all_from_keys()
            logger.info("Harness Keys-ingest boot-materialize: %s", _kmat)
            asyncio.create_task(_harness_keys.run_keys_materialize_loop())
            logger.info("Harness Keys-ingest materialize loop dispatched")
            # Content-hash seal sweep: keeps the vault current with WHATEVER
            # rewrites the cred files (login subprocess finishing after the
            # connect-flow seal, gate auto-refresh, rotation write-back) — the
            # connect-flow race burned a grok grant on a pod roll (2026-07-02).
            asyncio.create_task(_harness_vault.run_seal_sweep_loop())
            logger.info("Harness cred seal-sweep loop dispatched")
            # Proactive OAuth refresh: keep refreshable harness tokens (grok)
            # fresh on boot + on a timer, so a short-lived vendor token never
            # silently expires between fan_outs (no manual reconnect). ON for
            # every host — the
            # scale-to-zero harness pods cannot self-refresh between runs.
            from server.routes import harnesses as _connections
            _cm = await _connections.maintain_refreshable_creds()
            if _cm:
                logger.info("Boot harness cred refresh: %s", _cm)
            asyncio.create_task(_connections.run_cred_maintenance_loop())
            logger.info("Harness cred maintenance loop dispatched")
        except Exception as e:
            logger.warning("Non-Claude harness boot-materialize error (non-fatal): %s", e)

        # Orchestrator → Multi-Agent Coding (2026-08-21): the ONE-TIME migration
        # of the old ladder / parallel-builders settings into protocol files
        # (+ the Default tick). A startup task, not a read path — a read-scoped
        # caller must never be what writes config. Retries until the pool is
        # up; never marks itself done on a failed attempt.
        # DISPATCHED HERE, AFTER the harness credential materialize + Keys
        # ingest above, and not earlier: the migration expands the old ladder's
        # '*' rung across the engines that read as CONNECTED, and "connected"
        # means a credential file on disk. Dispatched before this block (where
        # it once lived) it raced the restore and lost — measured on
        # a rollout, one Host's marker was written 20-65ms BEFORE every
        # credential file and its bench rung was dropped silently.
        try:
            from server.routes.coding_protocols import migrate_at_startup as _cp_migrate
            asyncio.create_task(_cp_migrate())
        except Exception as _cp_err:
            logger.warning("coding-protocols migration dispatch failed (non-fatal): %s", _cp_err)

    # Resume background sync loops for every ACTIVE integration connection.
    from server.integrations.core import scheduler as integration_scheduler
    try:
        started = await integration_scheduler.start_all()
        logger.info("Integration sync loops: %d resumed", started)
    except Exception as e:
        logger.warning("Integration scheduler startup failed: %s", e)

    # The app-update check, harness health and the SDK pull asked the platform
    # and are not started: a standalone Host has none to ask.

    # --- mTLS front terminator (:8443) — in-process ---
    # Owns :8443 exactly as the old standalone python3 process did (see
    # server/mtls_frontdoor.py docstring);
    # running it as a supervised task in THIS loop saves a full second
    # interpreter per pod. It waits for the server cert itself, so genesis
    # pods boot cleanly with just the :8800 listener.
    from server import mtls_frontdoor
    asyncio.create_task(mtls_frontdoor.run_supervised())
    logger.info("mTLS front terminator dispatched in-process (:8443)")

    # --- No platform: the bastion on this machine pairs and dials in over
    # unix sockets, under this Host's own CA (server/network/local_bastion.py).
    # Returns at once on a Host with a platform.
    from server.network import local_bastion
    asyncio.create_task(local_bastion.run_supervised(brain_service._entity_id))

    # --- Direct host↔host peer transport (:8444) — Workstream B (§6.5/§10.3) ---
    # The data plane for host↔host: peers connect DIRECTLY to this lane and present
    # a bridge-signed slip; we verify it OFFLINE against pinned bridge signing keys +
    # a synced CRL (no bridge round-trip in the data path). Gated on a server cert
    # (same as the :8443 terminator) and a real entity. All best-effort — any failure
    # leaves circles.send_message on the bridge-relay fallback.
    global _peer_trust_ref, _peer_server_ref
    if relay_entity_id > 0 and os.path.exists("/etc/privatae/certs/server.crt"):
        try:
            from server.peer.peer_trust import PeerTrust
            from server.peer.peer_server import PeerServer
            from server.peer.handlers import PEER_HANDLERS

            async def _peer_bridge_call(op, params):
                try:
                    c = get_bridge_client()
                    return await c.request(op, params or {}) if c is not None else None
                except Exception:
                    return None

            _peer_trust_ref = PeerTrust(_peer_bridge_call)
            asyncio.create_task(_peer_trust_ref.run_periodic(120))
            _peer_server_ref = PeerServer(
                trust=_peer_trust_ref, handlers=PEER_HANDLERS,
                my_entity_id=relay_entity_id)
            await _peer_server_ref.start()
            logger.info("Peer transport (:8444) up — slip-gated direct host↔host lane")
            # The app lane's sender side: retries queued app sends until the
            # peer answers or the row expires.
            # Beside the server on purpose — no lane, no outbox to drive.
            if brain_pool is not None:
                try:
                    from server.peer.app_lane import run_peer_app_outbox_loop
                    asyncio.create_task(run_peer_app_outbox_loop(brain_pool, relay_entity_id))
                except Exception as _ol_err:
                    logger.warning("peer app outbox loop dispatch failed (non-fatal): %s", _ol_err)
                # The group lane's driver: the same outbox doctrine for group
                # invites and votes, plus the roster resync that keeps a
                # member's replica current.
                try:
                    from server.groups.lane import run_group_lane_loop
                    asyncio.create_task(run_group_lane_loop(brain_pool, relay_entity_id))
                except Exception as _gl_err:
                    logger.warning("group lane loop dispatch failed (non-fatal): %s", _gl_err)
            else:
                logger.info("peer app outbox and group lane loops skipped — no brain pool")
        except Exception as e:
            logger.warning("Peer transport startup failed (non-fatal): %s", e)
    else:
        logger.info("Peer transport (:8444) skipped — entity 0 or no server cert")

    yield

    # Shutdown
    logger.info("Personal computer shutting down")
    # Stop crash housekeeping first. sweep_forever sleeps most of its
    # life, so without an explicit cancel it is a pending task at interpreter
    # exit and asyncio logs "Task was destroyed but it is pending!" on every
    # shutdown — noise that trains people to ignore shutdown logs.
    for _t in _crash_tasks:
        _t.cancel()
    await _asyncio.gather(*_crash_tasks, return_exceptions=True)
    # Same reasoning for the Host Manager tile publisher: it sleeps 5 minutes
    # at a time, so it is pending at exit unless cancelled. It re-raises
    # CancelledError rather than swallowing it, so this actually stops it.
    if _desktop_snapshot_task is not None:
        _desktop_snapshot_task.cancel()
        await _asyncio.gather(_desktop_snapshot_task, return_exceptions=True)
    await integration_scheduler.stop_all()
    # The member door's connection and its renewal loop, when this Host is a
    # network member (a no-op otherwise).
    try:
        from server.bridge import membership as _membership
        await _membership.shutdown()
    except Exception as _mem_err:
        logger.debug("network membership shutdown: %s", _mem_err)
    # Close pooled aiohttp sessions before tearing down DB
    from mastercode.provider_gateway import close_all_sessions
    await close_all_sessions()
    await brain_service.shutdown()
    if _peer_server_ref is not None and getattr(_peer_server_ref, "_server", None) is not None:
        try:
            _peer_server_ref._server.close()
        except Exception:
            pass


app = FastAPI(
    title="Privatae Personal Computer",
    docs_url=None,   # No Swagger in production
    redoc_url=None,   # No ReDoc in production
    openapi_url=None,  # No OpenAPI spec exposed
    lifespan=lifespan,
)

# --- Middleware stack (order matters: the LAST added is the outermost) ---
# From outside in: HostHeaderGuard, AppOriginGuard, FrontDoorProof,
# security_headers, backup_activity, OwnerSessionGate, the localhost gate's
# WebSocket half, LocalhostOnlyMiddleware, CEIGAS, ApplicationToken.
# Application-token middleware reads the per-window token and attaches
# it to request.state for permission-aware routes to inspect.
from server.applications.middleware import ApplicationTokenMiddleware
app.add_middleware(ApplicationTokenMiddleware)

# CEIGAS gate: the operator kill switch on every mapped /api/* request
app.add_middleware(
    CEIGASGateMiddleware,
    entity_id=int(os.environ.get("ENTITY_ID", "0")),
)
# The localhost gate, inside the owner session gate; its WebSocket half beside it (a
# BaseHTTPMiddleware never sees an upgrade).
app.add_middleware(LocalhostOnlyMiddleware)
from server.middleware.localhost_only import LocalhostOnlyWebSocketGate  # noqa: E402
app.add_middleware(LocalhostOnlyWebSocketGate)
# The owner's session (server/middleware/owner_session.py): marks a request
# that carries one, so the localhost gate lets it through. Inside
# FrontDoorProof, which strips any identity header the request brought.
from server.middleware.owner_session import OwnerSessionGate  # noqa: E402
app.add_middleware(OwnerSessionGate)


@app.middleware("http")
async def backup_activity(request, call_next):
    # When a person last used this Host: the backup it takes on going quiet,
    # before the idle freeze (server/host_backup.py), keys off this stamp.
    from server.host_backup import note_request
    note_request(request.url.path)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    # Framing policy. An isolated app is served from its own origin,
    # apps-<key>-<handle> (server/applications/app_origin.py), and MUST be
    # frameable by its Host's desktop — a different origin — so it gets a CSP
    # `frame-ancestors` naming exactly that desktop instead of the
    # framing-blocking X-Frame-Options. Exactly that desktop, not the zone:
    # frame-ancestors is checked against every ancestor, so no app's origin can
    # frame another app's origin. Everything else stays SAMEORIGIN. The browser's
    # name is in X-Forwarded-Host (the direct-router rewrites Host to the pod
    # SAN before proxying), which app_origin.browser_host reads first.
    origin = app_origin.classify(app_origin.browser_host(request.headers))
    if origin.kind == app_origin.APP:
        response.headers["Content-Security-Policy"] = app_origin.response_csp(
            origin, request.scope["path"],
            response.headers.get("content-security-policy") or "")
    elif "content-security-policy" in response.headers:
        # A per-app CSP is already present — never clobber it.
        pass
    else:
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


# Who a request may say sent it: X-Client-Cert-CN only on what the mTLS
# front door forwarded, X-Authentik-* only on what it forwarded under the
# router's cert. Anything else on the pod's loopback reached :8800 and was
# believed as whatever person it named. Outside every reader of those headers:
# the two middlewares above, the localhost gate and its WebSocket half, the
# bastion binding, every route. Inside the app-origin and Host header guards,
# which read neither header.
from server.middleware.router_identity import FrontDoorProof  # noqa: E402
app.add_middleware(FrontDoorProof)

# One origin per isolated app: an app's origin serves that app and
# nothing else. Inside the Host header gate, outside everything else, so a
# refused path never reaches a route, the token middleware or the overlay.
from server.middleware.app_origin_guard import AppOriginGuard  # noqa: E402
app.add_middleware(AppOriginGuard)

# Outermost of all (added last): refuse a Host header that could not be a host
# before anything reads request.url, which Starlette rebuilds from it.
from server.middleware.host_header import HostHeaderGuard  # noqa: E402
app.add_middleware(HostHeaderGuard)


def _enforce_bastion_binding(ws, row, lane_log) -> bool:
    """B1b (cert<->bearer binding) — ENFORCED. The mTLS frontdoor (server/mtls_frontdoor.py)
    OWNS :8443, verifies the CEIGAS client cert (CERT_REQUIRED), STRIPS any client-supplied
    value and injects the VERIFIED client-cert CN on the WS upgrade head (X-Client-Cert-CN).
    That header is trustworthy ONLY when the immediate peer is loopback — the frontdoor
    proxies to the dashboard on loopback (::1:8800); see middleware/localhost_only._mtls_client_cn.
    Loopback alone is not enough, since any caller on the pod's loopback can write the header;
    FrontDoorProof (middleware/router_identity.py) removes it from any request the front door
    did not forward, so a CN read here was put there by the front door.

    THE RULE: require `cn_trusted AND cn == 'operator'` — i.e. a bearer is admitted on these
    reverse-relay lanes only if it arrived bound to a VERIFIED operator client-cert presented
    through the trusted loopback terminator. A bearer replayed WITHOUT that terminator-injected
    cert (cn None, or seen from a non-loopback peer so cn_trusted is False) is rejected.

    WHY this exact rule and not a per-uuid CN match: the operator cert CN is the literal
    constant "operator" — the device builds its CSR with CN="operator" (scripts/check_fp_from_csr.py
    _csr) and the bridge's bastion.enroll SIGNS that CSR as-submitted (network/bastion_enroll.redeem),
    so a CA signature preserves the subject and the CN stays "operator". The per-token identity
    (operator_uuid) is carried OUT-OF-BAND in mcp_client_tokens.notes->>'operator_uuid' (set from
    the bridge's enroll DATA field), never in the cert CN, and no resolver parses a uuid from the
    CN anywhere. So a uuid-in-CN binding is structurally unavailable; `cn=='operator' AND cn_trusted`
    is the STRONGEST binding the cert actually carries. This was confirmed against 48 real samples
    on a live Host across all three lanes — every legit connection is cn_trusted=True, cn='operator',
    peer=::1 — so enforcing rejects none of them.

    cn_trusted==False fails CLOSED: these top-level WS paths are reached by the bastion over its
    tunnel -> the in-pod mTLS frontdoor (:8443) -> loopback upstream; the frontdoor is the ONLY
    ingress that injects X-Client-Cert-CN and it always proxies from loopback. There is no
    legitimate non-loopback path to these endpoints, so an untrusted-CN connection can only be a
    forgery or a misroute. Returns True to admit, False to reject. The telemetry line is kept."""
    try:
        peer = ws.client.host if ws.client else None
        cn = ws.headers.get("x-client-cert-cn")
        cn_trusted = peer in ("::1", "127.0.0.1", "::ffff:127.0.0.1")
        ok = bool(cn_trusted and cn == "operator")
        fp = row["hardware_fingerprint"]
        lane_log.info(
            "B1b bind-enforce: token_id=%s op_uuid=%s fp=%s cn=%r cn_trusted=%s peer=%s ok=%s",
            row["id"], row["op_uuid"], (fp[:12] if fp else None), cn, cn_trusted, peer, ok,
        )
        return ok
    except Exception:
        # Fail closed: if we cannot evaluate the binding we do not admit the lane.
        lane_log.warning("B1b bind-enforce: evaluation error — rejecting", exc_info=True)
        return False


# --- Bastion egress reverse-relay worker lane (NAC P6, option-2 transport) ---
# The host can't reach a bastion's tunnel IP (the pod has no WG iface), so a bastion opens
# worker WebSockets HERE (over its tunnel -> :8443 lane) and serves egress over them: the
# egress gate hands each allowed request to an idle worker, the bastion dials out the user's
# own connection and relays the bytes back. Top-level path (not /api/*) so it bypasses the
# CEIGAS gate exactly like /mcp; authed by the bastion's OWN bearer. One job per worker WS.
@app.websocket("/bastion-egress")
async def bastion_egress_relay(ws: WebSocket):
    from server.network.egress_relay import get_egress_relay, RelayWorker, log as egress_log

    async def safe_close(code: int = 1000):
        # Never let a close on an already-closed socket (idle bastion dropped) raise an
        # "Unexpected ASGI message 'websocket.close'" that masks the real flow.
        try:
            await ws.close(code=code)
        except Exception:
            pass

    await ws.accept()
    try:
        from server import brain_service
        brain_pool = brain_service.get_pool()
        if not brain_pool:
            await safe_close(1011)
            return
        try:
            hello = await asyncio.wait_for(ws.receive_text(), timeout=10)
        except Exception:
            await safe_close(4400)
            return
        if not hello.startswith("BEARER "):
            await safe_close(4401)
            return
        import hashlib
        th = hashlib.sha256(hello[len("BEARER "):].strip().encode()).digest()
        row = await brain_pool.fetchrow(
            "SELECT id, notes->>'operator_uuid' AS op_uuid, hardware_fingerprint "
            "FROM mcp_client_tokens WHERE token_hash=$1 "
            "AND operator_kind='bastion' AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now())", th)
        if not row:
            egress_log.info("worker auth REJECTED (token unknown/revoked)")
            await safe_close(4401)
            return
        if not _enforce_bastion_binding(ws, row, egress_log):
            await safe_close(4401)
            return
        worker = RelayWorker(ws, bastion_token_id=row["id"])
        relay = get_egress_relay()
        egress_log.info("worker authed (token %s)", row["id"])
        try:
            await worker.serve(relay)
        finally:
            relay.discard(worker)
    except Exception as e:
        egress_log.warning("endpoint exception: %r", e)
    finally:
        await safe_close()


# own connection runs commands and relays output back. Same reverse-relay trick as egress, a
# different job type (run-on-server). Authed by the bastion's OWN bearer, like /bastion-egress.
# Default-DENY + always-audited live in nac_mgmt + the route; the BASTION is the authoritative
# wall (it checks its local allowlist before running), so a compromised pod can't widen scope.
@app.websocket("/bastion-mgmt")
async def bastion_mgmt_relay(ws: WebSocket):
    from server.network.nac_mgmt import get_mgmt_relay, MgmtWorker, log as mgmt_log

    async def safe_close(code: int = 1000):
        try:
            await ws.close(code=code)
        except Exception:
            pass

    await ws.accept()
    try:
        from server import brain_service
        brain_pool = brain_service.get_pool()
        if not brain_pool:
            await safe_close(1011)
            return
        try:
            hello = await asyncio.wait_for(ws.receive_text(), timeout=10)
        except Exception:
            await safe_close(4400)
            return
        if not hello.startswith("BEARER "):
            await safe_close(4401)
            return
        import hashlib
        th = hashlib.sha256(hello[len("BEARER "):].strip().encode()).digest()
        row = await brain_pool.fetchrow(
            "SELECT id, notes->>'operator_uuid' AS op_uuid, hardware_fingerprint "
            "FROM mcp_client_tokens WHERE token_hash=$1 "
            "AND operator_kind='bastion' AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now())", th)
        if not row:
            mgmt_log.info("mgmt worker auth REJECTED (token unknown/revoked)")
            await safe_close(4401)
            return
        if not _enforce_bastion_binding(ws, row, mgmt_log):
            await safe_close(4401)
            return
        worker = MgmtWorker(ws, bastion_token_id=row["id"])
        relay = get_mgmt_relay()
        mgmt_log.info("mgmt worker authed (token %s)", row["id"])
        try:
            await worker.serve(relay)
        finally:
            relay.discard(worker)
    except Exception as e:
        mgmt_log.warning("mgmt endpoint exception: %r", e)
    finally:
        await safe_close()


# Harness lane (H, harness-dispatch): the bastion runs the EXISTING harness container on its own box
# and the host dispatches a prompt to it over this reverse-relay lane — same image, different
# location. Authed by the bastion's OWN bearer, like /bastion-mgmt. The bastion is transport +
# container-lifecycle + governor, NOT a harness; H4 adds the signed-grant gate + image allowlist.
# Memory mirror. NOT a reverse relay — the inverse of one.
# The other three lanes exist because the pod cannot reach into the bastion, so
# the pod dispatches jobs onto workers the bastion opened. Here the bastion is
# the CLIENT of a pod-side service: PostgreSQL's own replication protocol, with
# the bastion's standby dialing in. It rides a WebSocket for one reason — the
# hub splices the tunnel to :8443 and nothing else, so this is the only door.
#
# Auth is identical to /bastion-egress (bearer -> mcp_client_tokens, then the
# cert<->bearer binding), because the trust decision is identical. What differs
# is what happens next: pgwire_relay throws away the client's StartupMessage and
# writes its own with replication=true, so an authenticated bastion gets a
# walsender and CANNOT get a SQL session, even though the socket behind it is
# trust-auth. See that module's header for why a plain splice was not an option.
@app.websocket("/bastion-pgwire")
async def bastion_pgwire_relay(ws: WebSocket):
    from server.network import mirror, pgwire_relay

    async def safe_close(code: int = 1000):
        try:
            await ws.close(code=code)
        except Exception:
            pass

    await ws.accept()
    try:
        from server import brain_service
        brain_pool = brain_service.get_pool()
        if not brain_pool:
            await safe_close(1011)
            return
        try:
            hello = await asyncio.wait_for(ws.receive_text(), timeout=10)
        except Exception:
            await safe_close(4400)
            return
        if not hello.startswith("BEARER "):
            await safe_close(4401)
            return
        import hashlib
        th = hashlib.sha256(hello[len("BEARER "):].strip().encode()).digest()
        row = await brain_pool.fetchrow(
            "SELECT id, notes->>'operator_uuid' AS op_uuid, hardware_fingerprint "
            "FROM mcp_client_tokens WHERE token_hash=$1 "
            "AND operator_kind='bastion' AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now())", th)
        if not row:
            pgwire_relay.log.info("mirror lane auth REJECTED (token unknown/revoked)")
            await safe_close(4401)
            return
        if not _enforce_bastion_binding(ws, row, pgwire_relay.log):
            await safe_close(4401)
            return

        # A valid bearer is not enough: the mirror must be enabled, and enabled
        # for THIS bastion. Enabling took the user's 2FA, so this check is what
        # makes that step-up mean something on the wire — without it any
        # enrolled box could stream the database.
        eid = brain_service._entity_id
        cfg = await mirror.get_config(brain_pool, eid)
        if not cfg.get("enabled") or int(cfg.get("token_id") or 0) != int(row["id"]):
            pgwire_relay.log.info(
                "mirror lane REFUSED for token %s (no mirror enabled for it)", row["id"])
            await safe_close(4403)
            return

        try:
            await pgwire_relay.relay(
                ws,
                await mirror.db_name(brain_pool),
                mirror.application_name(str(cfg.get("bastion_name") or "")),
                "/var/run/postgresql/.s.PGSQL.5432",
            )
        except pgwire_relay.PgWireError as e:
            pgwire_relay.log.info("mirror lane refused: %s", e)
            await safe_close(4409)
            return
    except Exception as e:
        pgwire_relay.log.warning("mirror lane endpoint exception: %r", e)
    finally:
        await safe_close()


@app.websocket("/bastion-harness")
async def bastion_harness_relay(ws: WebSocket):
    from server.network.nac_harness import (get_harness_relay, HarnessWorker, parse_harness_hello,
                                            log as harness_log)

    async def safe_close(code: int = 1000):
        try:
            await ws.close(code=code)
        except Exception:
            pass

    await ws.accept()
    try:
        from server import brain_service
        brain_pool = brain_service.get_pool()
        if not brain_pool:
            await safe_close(1011)
            return
        try:
            hello = await asyncio.wait_for(ws.receive_text(), timeout=10)
        except Exception:
            await safe_close(4400)
            return
        # Hello: `BEARER <bearer>` (a harness worker) or `BEARER <bearer> lane=session`
        # (a terminal-session worker). The lane is announced by the box and
        # decides which jobs this socket may carry: an S frame only ever goes to a
        # lane=session worker, and a session worker is never taken for an H job —
        # a shell must not eat the workers a harness run is waiting for, or the
        # reverse. A bastion that announces no lane predates the split and is a
        # harness worker.
        parsed = parse_harness_hello(hello)
        if parsed is None:
            harness_log.info("harness worker REJECTED (bad hello or unknown lane)")
            await safe_close(4401)
            return
        bearer, lane = parsed
        import hashlib
        th = hashlib.sha256(bearer.encode()).digest()
        row = await brain_pool.fetchrow(
            "SELECT id, notes->>'operator_uuid' AS op_uuid, hardware_fingerprint "
            "FROM mcp_client_tokens WHERE token_hash=$1 "
            "AND operator_kind='bastion' AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now())", th)
        if not row:
            harness_log.info("harness worker auth REJECTED (token unknown/revoked)")
            await safe_close(4401)
            return
        if not _enforce_bastion_binding(ws, row, harness_log):
            await safe_close(4401)
            return
        worker = HarnessWorker(ws, bastion_token_id=row["id"], lane=lane)
        relay = get_harness_relay()
        harness_log.info("harness worker authed (token %s, lane %s)", row["id"], lane)
        try:
            await worker.serve(relay)
        finally:
            relay.discard(worker)
    except Exception as e:
        harness_log.warning("harness endpoint exception: %r", e)
    finally:
        await safe_close()


# Embed lane (V, embed-dispatch): the bastion serves the host's OWN embedding model on its
# GPU and the host dispatches {model,input} embed jobs over this reverse-relay lane, instead
# of forward-dialing the bastion's tunnel IP (port 11434) — which the entity pod has no
# route into. Authed by the bastion's OWN bearer, byte-for-byte like /bastion-mgmt. UNLIKE
# mgmt there is NO signed-grant gate and NO audit: an embed is a data-plane read the host
# already owns via its own embedding config, never billed, never audited.
@app.websocket("/bastion-embed")
async def bastion_embed_relay(ws: WebSocket):
    from server.network.embed_relay import get_embed_relay, EmbedWorker, log as embed_log

    async def safe_close(code: int = 1000):
        try:
            await ws.close(code=code)
        except Exception:
            pass

    await ws.accept()
    try:
        from server import brain_service
        brain_pool = brain_service.get_pool()
        if not brain_pool:
            await safe_close(1011)
            return
        try:
            hello = await asyncio.wait_for(ws.receive_text(), timeout=10)
        except Exception:
            await safe_close(4400)
            return
        if not hello.startswith("BEARER "):
            await safe_close(4401)
            return
        import hashlib
        th = hashlib.sha256(hello[len("BEARER "):].strip().encode()).digest()
        row = await brain_pool.fetchrow(
            "SELECT id, notes->>'operator_uuid' AS op_uuid, hardware_fingerprint "
            "FROM mcp_client_tokens WHERE token_hash=$1 "
            "AND operator_kind='bastion' AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now())", th)
        if not row:
            embed_log.info("embed worker auth REJECTED (token unknown/revoked)")
            await safe_close(4401)
            return
        if not _enforce_bastion_binding(ws, row, embed_log):
            await safe_close(4401)
            return
        worker = EmbedWorker(ws, bastion_token_id=row["id"])
        relay = get_embed_relay()
        embed_log.info("embed worker authed (token %s)", row["id"])
        try:
            await worker.serve(relay)
        finally:
            relay.discard(worker)
    except Exception as e:
        embed_log.warning("embed endpoint exception: %r", e)
    finally:
        await safe_close()


@app.websocket("/bastion-git")
async def bastion_git_relay(ws: WebSocket):
    """Bastion git lane (server/network/git_relay.py): the user's LOCAL git, spliced
    over their bastion, relayed out the PLATFORM path to the platform forge only. Same
    bearer + mTLS binding as the other lanes; one connection per socket; refused for a
    quarantined bastion. On for every paired bastion."""
    from server.network.git_relay import GitWorker, log as git_log

    async def safe_close(code: int = 1000):
        try:
            await ws.close(code=code)
        except Exception:
            pass

    await ws.accept()
    try:
        from server import brain_service
        brain_pool = brain_service.get_pool()
        if not brain_pool or _nac_egress_ref is None:
            await safe_close(1011)
            return
        try:
            hello = await asyncio.wait_for(ws.receive_text(), timeout=10)
        except Exception:
            await safe_close(4400)
            return
        if not hello.startswith("BEARER "):
            await safe_close(4401)
            return
        import hashlib
        th = hashlib.sha256(hello[len("BEARER "):].strip().encode()).digest()
        row = await brain_pool.fetchrow(
            "SELECT id, notes->>'operator_uuid' AS op_uuid, hardware_fingerprint "
            "FROM mcp_client_tokens WHERE token_hash=$1 "
            "AND operator_kind='bastion' AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now())", th)
        if not row:
            git_log.info("git lane auth REJECTED (token unknown/revoked)")
            await safe_close(4401)
            return
        if not _enforce_bastion_binding(ws, row, git_log):
            await safe_close(4401)
            return
        # A quarantined bastion (nac_mgmt.nac_bastion_quarantine, keyed by the same
        # token id) gets nothing — a tighten always holds, so an unreadable table refuses.
        try:
            q = await brain_pool.fetchval(
                "SELECT 1 FROM nac_bastion_quarantine WHERE token_id=$1", row["id"])
        except Exception as e:
            git_log.info("git lane: quarantine check unavailable (%r) — refusing", e)
            q = 1
        if q:
            git_log.info("git lane REFUSED: bastion token %s is quarantined", row["id"])
            await safe_close(4403)
            return
        await GitWorker(ws, bastion_token_id=row["id"], proxy=_nac_egress_ref).serve()
    except Exception as e:
        git_log.warning("endpoint exception: %r", e)
    finally:
        await safe_close()


# --- API routes ---
app.include_router(system.router)
from server.routes import owner as _owner_route  # noqa: E402
app.include_router(_owner_route.router)
from server.routes import remote_access as _remote_access_route  # noqa: E402
app.include_router(_remote_access_route.router)
app.include_router(files.router)
app.include_router(screen.router)
app.include_router(input.router)
app.include_router(clipboard.router)
app.include_router(projects.router)
app.include_router(ceigas.router)
app.include_router(update.router)
app.include_router(portal.router)
app.include_router(entity.router)
app.include_router(integrations.router)
app.include_router(brain.router)
app.include_router(security.router)

from server.routes import cseries_security
app.include_router(cseries_security.router)

app.include_router(inference.router)
from server.routes import workbench as _workbench_route
app.include_router(_workbench_route.router)    # Workbench: gitea PR surface (audit phase 7)
from server.routes import workbench_index as _workbench_index_route
app.include_router(_workbench_index_route.router)  # Workbench: the Index's send (its search is /api/projects/index)
from server.routes import ladder as _ladder_route
app.include_router(_ladder_route.router)      # the Ladder: routing-economics engine
from server.routes import coding_protocols as _coding_protocols_route
app.include_router(_coding_protocols_route.router)  # Orchestrator → Multi-Agent Coding: protocol files the host reads (2026-08-21)
from server.routes import gitea as _gitea_route
app.include_router(_gitea_route.router)         # Gitea app: repo browse over the forge
from server.routes import gitea_propose as _gitea_propose_route
app.include_router(_gitea_propose_route.router)  # fork → branch → PR to a repo the host does not own
from server.routes import github_mirror as _github_mirror_route
app.include_router(_github_mirror_route.router)  # Gitea app: GitHub pull-mirrors (device flow → Keys)
from server.routes import connector as _connector_route
app.include_router(_connector_route.router)     # messaging connectors: /api/connector/* (connector capability)
from server.routes import compute as _compute_route
app.include_router(_compute_route.router)   # ComputeView: cross-harness usage/spend
from server.routes import codesearch
app.include_router(codesearch.router)        # per-user hybrid code search (pgvector + tsvector)
from server.routes import search as _search_route
app.include_router(_search_route.router)     # the one index: POST /api/search over every store (2026-09-17; a POST since 2026-10-10)
from server.routes import world as _world_route
app.include_router(_world_route.router)      # the Host's own database for apps and the Data tab: /api/world, scope use:world (2026-10-16)
app.include_router(privatae_status.router)
app.include_router(synaptive.router)
app.include_router(mcp.router)
from server.routes import mcp_oauth as _mcp_oauth_route
app.include_router(_mcp_oauth_route.router)   # signing in to an MCP server (OAuth, migrations/v239)
# The design library — complete host-authored desktops shared across the hosts
# on one account (samples are host-made desktops, never a brand catalog). The
# borrowed design-system catalog it superseded was removed with migrations/v206.
from server.routes import designs
app.include_router(designs.router)
from server.routes import codespace
app.include_router(codespace.router)

from server.routes import review as review_routes
app.include_router(review_routes.router)

from server.routes import network, embed, context

# Community board — bridge pass-through to communityd (degrades to 503 when
# the bridge/service is away).
from server.routes import community as community_routes
app.include_router(community_routes.router)
app.include_router(network.router)
# Network membership: Settings -> Network joins and leaves the network with a
# code from the owner's account page (server/bridge/membership.py).
from server.routes import network_membership as network_membership_routes
app.include_router(network_membership_routes.router)
# The owner names the Host, in Settings -> About.
from server.routes import host_name as host_name_routes
app.include_router(host_name_routes.router)
# The owner connects a git server they run, in Settings -> Git server.
from server.routes import git_server as git_server_routes
app.include_router(git_server_routes.router)
# The oversight ledger: read the findings
# the host folded from what its machines reported, and resolve one. Separate
# from network.router because the scopes are separate — read:topology opens the
# whole network read side and this ledger is a narrower, more sensitive read.
from server.routes import oversight as oversight_routes
app.include_router(oversight_routes.router)
# Ships (server/routes/ships.py): the pipelines, their runs with every step's
# signed receipt, the machines and the keys; start and stop a run.
from server.routes import ships as ships_routes
app.include_router(ships_routes.router)
app.include_router(embed.router)
app.include_router(context.router)

# Context Manager (Part 5) — the user's control plane for USER CONTEXT: the
# where-it-lives index + per-prefix placement + single/replicate availability +
# embedding status. Reuses brain.mastercode.context_backends (read:context /
# write:context, + read:topology for the choosable-bastion list).
from server.routes import context_manager
app.include_router(context_manager.router)

# Ship 4d-1: decision substrate routes (host proposes, user resolves via inbox)
from server.routes import decisions as decisions_routes
app.include_router(decisions_routes.router)


from server.routes import applications as applications_routes
app.include_router(applications_routes.router)

# Artifacts — the disposable, lightweight LIVE-build primitive (Phase 6).
# DISTINCT from an app: stored project-style but not App-SDK-bound until the
# host converts it. See server/applications/artifacts.py.
from server.routes import artifacts as artifacts_routes
app.include_router(artifacts_routes.router)

from server.routes import backup as backup_routes
app.include_router(backup_routes.router)
# A file the owner uploads to restore from streams to disk as it arrives: a
# router of its own, without the backup router's whole-body read.
app.include_router(backup_routes.upload_router)
# The Host's own crash artifacts (list/delete, metadata only). Kernel
# route rather than a meta-tool because kernel_op already carries the scoped
# token + CEIGAS bits check; see server/routes/system_crash.py.
from server.routes import system_crash as system_crash_routes
app.include_router(system_crash_routes.router)

from server.routes import vault as vault_routes
app.include_router(vault_routes.router)          # identity-scoped credential vault

from server.routes import browser_forms as browser_forms_routes
app.include_router(browser_forms_routes.router)  # Browser app: "save these entries" -> vault
from server.routes import browser_profile as browser_profile_routes
app.include_router(browser_profile_routes.router)  # Browser app: stay signed in / sign out
from server.routes import browser_agents as browser_agents_routes
app.include_router(browser_agents_routes.router)  # Browser app: recorded agents — save / run / runs

from server.routes import training_memory as training_memory_routes
app.include_router(training_memory_routes.router)
from server.routes import skills as skills_routes
app.include_router(skills_routes.router)
from server.routes import embedding as embedding_routes
app.include_router(embedding_routes.router)
# The media_embedding role: pictures embed through an endpoint the
# user provides, exactly as text does. Separate surface because a media switch
# measures three things (width, request shape, and that the model's text tower
# shares the image space) where the text switch measures one.
from server.routes import media_embedding as media_embedding_routes
app.include_router(media_embedding_routes.router)
# Context extraction: a stored file becomes TEXT filed as ordinary rows the
# embedding job picks up. The host reads PDF / Office / text itself
# (server/readers, 2026-09-22); OCR, speech and video frames run on one of the
# user's machines.
from server.routes import context_extract as context_extract_routes
app.include_router(context_extract_routes.router)
# Connections OAuth-flow router REMOVED (canvas pivot 2026-07-16): the platform
# ships no login surface. The harness dispatch substrate moved to routes/harnesses.py
# (imported directly, no router); credential ingest is the Keys app (network/harness_keys.py).

from server.routes import circles as circles_routes
app.include_router(circles_routes.router)
# Turn types (2026-09-26): the Circles dropdown's read surface.
from server.routes import turn_types as turn_types_routes
app.include_router(turn_types_routes.router)

# Lane-B agent SDK surface: list / dispatch / veto in-container scoped agents.
# Reads the runtime-singleton AgentDispatch; the Workbench live view consumes GET /api/agents.
from server.routes import agents as agents_routes
app.include_router(agents_routes.router)

from server.routes import tickets as tickets_routes
app.include_router(tickets_routes.router)
from server.routes import limits as _limits_route
app.include_router(_limits_route.router)    # Settings > Limits: the person's limits (runtime/user_limits)
# Tickets pane, support half: platform-help tickets (draft -> user approves the
# shared context -> bridge). server/routes/support.py.
from server.routes import support as support_routes
app.include_router(support_routes.router)

from server.routes import inbox as inbox_routes
app.include_router(inbox_routes.router)

from server.routes import calendar as calendar_routes
app.include_router(calendar_routes.router)

# Reach-out: read the host's channels,
# triggers and fire log; delete one; fire a test. The subscription itself is
# the dispatcher loop started in lifespan; these routes are what the Circles
# "How your Host reaches you" pane will read (Phase 4, not built).
from server.routes import reachout as reachout_routes
app.include_router(reachout_routes.router)

# Peer app lane: an installed app
# grants, sends to, and reads from the same app on a peer host. The wire is
# the :8444 lane (app.grant / app.deliver); the store-and-forward driver is
# the outbox loop started in lifespan beside the peer server.
from server.routes import peer_apps as peer_apps_routes
app.include_router(peer_apps_routes.router)

# Groups: a roster and a
# signed vote. This is the CREATOR-HELD tier — a group circle whose roster
# lives in this pod and is replicated to members over the same :8444 lane
# (group.grant / group.roster_fetch / group.vote). The platform-held tier
# (communityd groups, where moderators are assigned) is in the platform's source.
from server.routes import groups as groups_routes
app.include_router(groups_routes.router)

# Circles upgrade (2026-06-11): contacts address book + domain policy —
# backs the previously-dead Contacts/Policy rail tabs.
from server.routes import contacts as contacts_routes
app.include_router(contacts_routes.router)
from server.routes import domain_policy as domain_policy_routes
app.include_router(domain_policy_routes.router)
from server.routes import phone_alias as phone_alias_routes
app.include_router(phone_alias_routes.router)
# Contact cards for other Hosts (2026-09-22): one resolver behind every
# surface that names another Host — server/hosts/card.py.
from server.routes import host_cards as host_cards_routes
app.include_router(host_cards_routes.router)
from server.routes import messages as messages_routes
app.include_router(messages_routes.router)

from server.routes import appearance as appearance_routes
app.include_router(appearance_routes.router)

# Generated images a provider returned inline (b64_json), kept on this Host.
from server.routes import images as images_routes
app.include_router(images_routes.router)

# Realtime voice: server-side ephemeral-token mint for browser voice apps
# (POST /api/realtime/client_secrets). The provider key stays sealed in the
# host vault; only the short-lived client_secret reaches the browser. Missing
# server half of the open-voice-agent substrate — see server/routes/realtime.py.
from server.routes import realtime as realtime_routes
app.include_router(realtime_routes.router)

# Static mounts for system apps (baked into image, read-only) and user
# apps (entity-authored, writable volume). Each app directory contains
# its own index.html, icon, JS, CSS. The dashboard loads them via iframe.
#
# These routes sit BEFORE the OverlayStaticFiles catch-all so the SPA
# router doesn't swallow them.
from pathlib import Path as _P
from starlette.types import Scope
class _NoCacheStatic(StaticFiles):
    """StaticFiles with Cache-Control: no-cache so iframes don't serve
    stale system-app bundles after a deploy. Browsers still revalidate
    via ETag/Last-Modified — this only forces them to ASK."""
    async def get_response(self, path: str, scope: Scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

# _USR_APPS moved from /app/projects (image-baked
# platform space, wiped on every pod restart and shared across entities
# running the same image) to /var/lib/cseries/projects (per-entity persistent
# volume). The /app/projects path was the same class of leak as the
# original local_grep default — entity-written project files would have
# landed in platform space. /var/lib/cseries/ is the entity's own writable,
# persistent volume — the correct home for Workbench projects.
# Made at server start (lifespan), never at import.
_USR_APPS = _P("/var/lib/cseries/projects")

# /system-apps/<id>/... is served by the application proxy router:
# kind=static apps stream from /var/lib/cseries/apps/<id>/, kind=process
# apps proxy through their sidecar's localhost port. Decision is made
# per request based on the registry row.
from server.applications.proxy import router as _apps_proxy_router
app.include_router(_apps_proxy_router)

# check_dir=False: the dir is made at start, after this mount is registered.
app.mount("/user-apps", _NoCacheStatic(directory=str(_USR_APPS), check_dir=False),
          name="user-apps")


# --- WebSocket (CAN-bus message protocol) ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Bidirectional real-time communication.

    Entity sends: file updates, notifications, app state, status.
    Dashboard sends: user actions, file operations, app interactions.

    Message format:
    {
        "type": "file_update|notification|app_state|status|user_action",
        "module": "dock-menu|taskbar|entity",
        "persist": true|false,
        "data": { ... }
    }
    """
    # Verify the operator identity BEFORE accepting the socket. The router
    # injects the verified X-Authentik-Email; require it (no more anonymous
    # WebSockets) and, when the per-pod operator allowlist is provisioned,
    # require the caller be an operator of THIS entity. Defense-in-depth behind
    # the router's own operator_entities check — see server/operator_identity.py.
    from server.operator_identity import check_operator
    _ws_ok, _ws_email = check_operator(websocket.headers)
    if not _ws_ok:
        logger.warning(
            "WS /ws rejected: email=%r not authorized for entity %s (peer=%s)",
            _ws_email, os.environ.get("ENTITY_ID"), websocket.client,
        )
        await websocket.close(code=1008)  # policy violation
        return
    await websocket.accept()
    _ws_connections.add(websocket)
    logger.info("WebSocket client connected: %s (total: %d)", _ws_email, len(_ws_connections))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"error": "invalid JSON"})
                continue

            # Echo for now — Phase 4 wires to CAN-bus dispatch
            msg_type = msg.get("type", "unknown")
            logger.debug("WS message: type=%s module=%s", msg_type, msg.get("module"))

            # Broadcast to all connected clients (CAN-bus pattern)
            for ws in _ws_connections.copy():
                if ws != websocket:
                    try:
                        await ws.send_json(msg)
                    except Exception:
                        _ws_connections.discard(ws)

    except WebSocketDisconnect:
        pass
    finally:
        _ws_connections.discard(websocket)
        logger.info("WebSocket client disconnected (total: %d)", len(_ws_connections))


async def broadcast(msg: dict):
    """Broadcast a message to all WebSocket clients."""
    for ws in _ws_connections.copy():
        try:
            await ws.send_json(msg)
        except Exception:
            _ws_connections.discard(ws)


# --- Dashboard SPA (overlay filesystem) ---
class OverlayStaticFiles(StaticFiles):
    """
    Serves static files with overlay: custom/ first, base/ fallback.

    Entity writes to custom/ via CEIGAS-governed file API.
    Base is baked into the image (read-only rootfs) — untouchable.

    Cache policy: every base response carries `Cache-Control: no-cache,
    must-revalidate`. The shell (index.html) injects `?v=<PC_TAG>` busters
    on its <script>/<link> refs (Dockerfile sed at build) — but those only
    bust the assets if the browser actually re-fetches index.html. The
    shell shipped with NO Cache-Control, so browsers served a heuristically
    cached old shell after an image bump, pinning the OLD `?v=` strings →
    stale js/css forever (until a hard refresh). That single gap caused
    both reported regressions: stale ws-client.js / boot-screen.css left the
    boot video hung instead of transitioning on WS-open, and stale
    app-runner.js made the redesigned Memory app appear "reverted". no-cache
    forces an ETag revalidation — a cheap 304 on these tiny pod-local files —
    so a rolled image's fresh shell + assets always load. Mirrors
    `_NoCacheStatic` (the system-app mount) which already did this.
    """

    def __init__(self):
        # Primary: custom overlay (entity modifications)
        # Fallback: base dashboard (immutable)
        super().__init__(directory=str(DASHBOARD_BASE), html=True)
        self.custom_dir = DASHBOARD_CUSTOM

    async def get_response(self, path: str, scope):
        # App SDK runtime: serve the host's INSTALLED, Privatae-signed SDK
        # (sdk_store), NOT the static image copy, so the served bytes match the
        # SRI hash the injector emits. This overlay handler owns /js/* (a plain
        # @app.get route loses to the catch-all "/" mount), so intercept here.
        # Never overridden by the entity's custom/ overlay. Fails closed: 503 if
        # no verified SDK is installed.
        if path == "js/app-sdk.js":
            from starlette.responses import Response as _Resp
            from server.applications import sdk_store
            try:
                sdk = sdk_store.current()
            except Exception:
                return _Resp(
                    "// no verified SDK installed", status_code=503,
                    media_type="application/javascript",
                    headers={"Cache-Control": "no-store"},
                )
            return _Resp(
                sdk["runtime_js"], media_type="application/javascript",
                headers={"Cache-Control": "no-store"},
            )

        # Design tokens: the ONE stylesheet the shell and every system app
        # load. When the live desktop document declares cseries-tokens, its
        # accepted pairs are appended as a :root override so the whole system
        # follows the desktop (server/routes/appearance.py design_tokens_css).
        # Same interception
        # reason as app-sdk.js above: a plain route loses to this mount. The
        # custom/ overlay still wins for the base file, except on an app's own
        # origin (overlay_allowed below), where the image's copy is
        # read; no override → the file is served byte-identical. The same
        # base read feeds the sheet host-made artifacts wear
        # (appearance.token_sheet).
        if path == "css/tokens.css":
            from starlette.responses import Response as _Resp
            from server.routes.appearance import base_tokens_css, design_tokens_css
            base = base_tokens_css(overlay=app_origin.overlay_allowed(scope))
            if base is None:
                # Unreadable or not UTF-8 (a hand-written custom overlay):
                # serve the bytes as before rather than 500 the whole shell
                # stylesheet; the override is skipped for that one file.
                return await super().get_response(path, scope)
            extra = await design_tokens_css()
            return _Resp(
                base + extra, media_type="text/css; charset=utf-8",
                headers={"Cache-Control": "no-cache, must-revalidate"},
            )

        # Check custom overlay first — except on an app's own origin:
        # the shared files an app loads there come from the image only, so
        # nothing the entity writes runs with an app's origin.
        # inside(): the request path can climb out of custom/ ('/%2e%2e/x'),
        # and only StaticFiles' own directory gets starlette's check.
        custom_path = static_paths.inside(self.custom_dir, path)
        if custom_path is not None and custom_path.is_file() and app_origin.overlay_allowed(scope):
            resp = FileResponse(str(custom_path))
        else:
            # Fall back to base
            resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


# Unmatched /api/* must fail as a clean 404, not fall through to the SPA static
# mount below. StaticFiles answers only GET/HEAD, so a POST to an unknown /api
# path returned a misleading 405 (this is exactly what once made a voice
# token mint fail opaquely). A real router still matches first — every
# include_router above is registered before this — so this only catches paths
# no router claimed, and only for HTTP (WebSocket /api routes are unaffected).
from starlette.responses import JSONResponse as _JSONResponse


@app.api_route(
    "/api/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def _api_route_not_found(rest: str, request: Request) -> _JSONResponse:
    return _JSONResponse(
        {"detail": f"no such API route: /api/{rest}", "method": request.method},
        status_code=404,
    )


# Mount dashboard SPA last (catch-all for client-side routing)
app.mount("/", OverlayStaticFiles())
