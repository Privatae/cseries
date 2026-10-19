"""
Session Handler — Dedicated Session Lifecycle Management

tmux-style session lifecycle for entity conversations.
Sessions transition through states: ACTIVE → IDLE → STALE → EXPIRED
with configurable thresholds and automatic cleanup.
"""

import asyncio
import json
import logging
from typing import Optional

log = logging.getLogger("cseries.sessions")

# Default thresholds — can be overridden per entity via behavioral_params.session_config
SESSION_DEFAULTS = {
    "idle_threshold_minutes": 30,       # No activity → IDLE
    "stale_threshold_minutes": 240,     # 4 hours idle → STALE (prompt may be outdated)
    "expiry_threshold_minutes": 1440,   # 24 hours idle → auto-expire
    "max_active_per_entity": 3,         # Max concurrent active sessions per entity
    "max_active_per_channel": 1,        # Max 1 active session per channel type
    "prompt_refresh_on_resume": False,  # Disabled: caused infinite recursion   # Rebuild system prompt when resuming from IDLE
}


class SessionHandler:
    """
    Manages session lifecycle for all entities.
    Runs cleanup on a periodic interval as a background task.
    """

    def __init__(self, pool, runtime=None):
        """
        Args:
            pool: asyncpg connection pool
            runtime: EntityRuntime instance (for prompt rebuilding)
        """
        self.pool = pool
        self.runtime = runtime
        self._refreshing: set = set()  # Guard against recursive refresh
        self._cleanup_task: Optional[asyncio.Task] = None

    def set_runtime(self, runtime):
        """Set the runtime reference (for circular dependency resolution)."""
        self.runtime = runtime

    # ------------------------------------------------------------------
    # Session Creation
    # ------------------------------------------------------------------

    async def get_or_create_session(
        self,
        entity_id: int,
        channel: str = "chat",
        user_id: str = None,
        operator_id: int = None,
        context: str = None,
        strategy: str = "context_adaptive",
        agent_ratio: float = 0.6,
    ) -> dict:
        """
        Get an existing usable session or create a new one.

        Logic:
        1. Check for existing active/idle session on this entity+channel
        2. If ACTIVE and fresh → return it (reuse)
        3. If IDLE → refresh prompt, reactivate, return it
        4. If STALE or too old → close it, create new
        5. If nothing exists → create new
        6. Enforce max_active_per_entity limit
        """
        config = await self._get_config(entity_id)

        # Check for existing session on this entity + channel
        existing = await self.pool.fetchrow("""
            SELECT session_id, status,
                   EXTRACT(EPOCH FROM (NOW() - last_activity_at))/60 as idle_minutes
            FROM entity_sessions
            WHERE entity_id = $1 AND channel = $2 AND status IN ('active', 'idle')
            ORDER BY last_activity_at DESC LIMIT 1
        """, entity_id, channel)

        if existing:
            idle_min = float(existing["idle_minutes"] or 0)

            if existing["status"] == "active" and idle_min < config["idle_threshold_minutes"]:
                # Still active and fresh — reuse
                log.debug("Reusing active session %s (idle %.0fm)",
                          existing["session_id"][:8], idle_min)
                return await self._load_session_metadata(existing["session_id"])

            elif idle_min < config["stale_threshold_minutes"]:
                # IDLE but not stale — refresh prompt and reactivate
                log.info("Refreshing idle session %s (idle %.0fm)",
                         existing["session_id"][:8], idle_min)
                if config["prompt_refresh_on_resume"] and self.runtime and existing["session_id"] not in self._refreshing:
                    await self._refresh_session_prompt(
                        existing["session_id"], entity_id, operator_id, context, strategy, agent_ratio
                    )
                await self.pool.execute("""
                    UPDATE entity_sessions
                    SET status = 'active', last_activity_at = NOW()
                    WHERE session_id = $1
                """, existing["session_id"])
                return await self._load_session_metadata(existing["session_id"])

            else:
                # STALE — close and create new
                log.info("Closing stale session %s (idle %.0fm)",
                         existing["session_id"][:8], idle_min)
                await self._close_session(existing["session_id"], "stale_replacement")

        # Enforce max active sessions per entity
        active_count = await self.pool.fetchval("""
            SELECT COUNT(*) FROM entity_sessions
            WHERE entity_id = $1 AND status IN ('active', 'idle')
        """, entity_id)

        if active_count >= config["max_active_per_entity"]:
            oldest = await self.pool.fetchval("""
                SELECT session_id FROM entity_sessions
                WHERE entity_id = $1 AND status IN ('active', 'idle')
                ORDER BY last_activity_at ASC LIMIT 1
            """, entity_id)
            if oldest:
                log.info("Closing oldest session %s (max_active limit %d)",
                         oldest[:8], config["max_active_per_entity"])
                await self._close_session(oldest, "max_sessions_exceeded")

        # No reusable session — caller should create a new one via runtime.start_session()
        return None

    # ------------------------------------------------------------------
    # Session Activity
    # ------------------------------------------------------------------

    async def touch(self, session_id: str):
        """Update last_activity_at. Call on every message."""
        await self.pool.execute("""
            UPDATE entity_sessions
            SET last_activity_at = NOW(), status = 'active'
            WHERE session_id = $1
        """, session_id)

    async def check_session_health(self, session_id: str) -> Optional[str]:
        """
        Check if a session is still usable before inference.

        Returns:
            None if session is healthy.
            'stale' if session should be replaced.
            'expired' if session is gone.
        """
        row = await self.pool.fetchrow("""
            SELECT status,
                   EXTRACT(EPOCH FROM (NOW() - last_activity_at))/60 as idle_minutes
            FROM entity_sessions
            WHERE session_id = $1
        """, session_id)

        if not row:
            return "expired"

        if row["status"] in ("closed", "expired", "ended"):
            return "expired"

        idle_min = float(row["idle_minutes"] or 0)
        entity_id = None  # We don't need config for basic check

        if row["status"] == "stale" or idle_min > SESSION_DEFAULTS["stale_threshold_minutes"]:
            return "stale"

        return None

    # ------------------------------------------------------------------
    # Session Listing (tmux list-sessions)
    # ------------------------------------------------------------------

    # Contexts that are automated/eval — never shown in user-facing session lists
    # Mirrors runtime/session.EPHEMERAL_CONTEXTS — "chat"/"text_chat"/"text"
    # removed 2026-08-19 (Circles conversations run under "chat"; they are
    # real sessions, not eval probes).
    EPHEMERAL_CONTEXTS = (
        "eval_sandbox", "benchmark", "humaneval", "experiment_sandbox", "harmbench_sandbox",
        "diagnostic", "test", "text_test", "training",
    )

    async def list_sessions(self, entity_id: int = None, include_closed: bool = False) -> list:
        """List sessions, optionally filtered by entity. Excludes eval/automated sessions."""
        ephemeral_filter = (
            "AND (interaction_context IS NULL OR ("
            "interaction_context NOT IN ('eval_sandbox','benchmark','humaneval','experiment_sandbox','harmbench_sandbox',"
            "'diagnostic','test','text_test','training')"
            " AND interaction_context NOT LIKE 'autonomous_task:%'"
            "))"
        )
        if entity_id:
            status_filter = "" if include_closed else "AND status IN ('active', 'idle', 'stale')"
            rows = await self.pool.fetch(f"""
                SELECT session_id, entity_id, channel, status, created_at,
                       last_activity_at, close_reason, interaction_context,
                       EXTRACT(EPOCH FROM (NOW() - last_activity_at))/60 as idle_minutes,
                       message_count
                FROM entity_sessions
                WHERE entity_id = $1 AND message_count > 0 {status_filter} {ephemeral_filter}
                ORDER BY last_activity_at DESC
                LIMIT 30
            """, entity_id)
        else:
            status_filter = "WHERE" if include_closed else "WHERE status IN ('active', 'idle', 'stale') AND"
            rows = await self.pool.fetch(f"""
                SELECT s.session_id, s.entity_id, e.display_name,
                       s.channel, s.status, s.created_at,
                       s.last_activity_at, s.close_reason, s.interaction_context,
                       EXTRACT(EPOCH FROM (NOW() - s.last_activity_at))/60 as idle_minutes,
                       s.message_count
                FROM entity_sessions s
                LEFT JOIN entity_registry e ON s.entity_id = e.id
                {status_filter} (s.interaction_context IS NULL OR s.interaction_context NOT IN ('eval_sandbox','benchmark','humaneval','experiment_sandbox','harmbench_sandbox'))
                ORDER BY s.last_activity_at DESC
                LIMIT 100
            """)
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Session Closing
    # ------------------------------------------------------------------

    async def close_session(self, session_id: str, reason: str = "user_closed") -> dict:
        """Explicitly close a session. Triggers end_session on the runtime if available."""
        if self.runtime:
            try:
                return await self.runtime.end_session(session_id)
            except Exception as e:
                log.warning("Runtime end_session failed, force-closing: %s", e)
        await self._close_session(session_id, reason)
        return {"session_id": session_id, "status": "closed", "reason": reason}

    async def close_all_for_entity(self, entity_id: int, reason: str = "bulk_close") -> int:
        """Close all active/idle sessions for an entity. Returns count closed."""
        sessions = await self.pool.fetch("""
            SELECT session_id FROM entity_sessions
            WHERE entity_id = $1 AND status IN ('active', 'idle', 'stale')
        """, entity_id)
        for s in sessions:
            await self._close_session(s["session_id"], reason)
        if sessions:
            log.info("Closed %d sessions for entity %d (reason=%s)",
                     len(sessions), entity_id, reason)
        return len(sessions)

    # ------------------------------------------------------------------
    # Periodic Cleanup (Background Task)
    # ------------------------------------------------------------------

    async def cleanup(self) -> dict:
        """
        Run session lifecycle transitions.

        1. ACTIVE sessions idle > threshold → IDLE
        2. IDLE sessions idle > threshold → STALE
        3. STALE sessions idle > threshold → EXPIRED (closed)
        """
        idle_thresh = SESSION_DEFAULTS["idle_threshold_minutes"]
        stale_thresh = SESSION_DEFAULTS["stale_threshold_minutes"]
        expiry_thresh = SESSION_DEFAULTS["expiry_threshold_minutes"]

        # Active → Idle
        idle_result = await self.pool.execute("""
            UPDATE entity_sessions SET status = 'idle'
            WHERE status = 'active'
            AND EXTRACT(EPOCH FROM (NOW() - last_activity_at))/60 > $1
        """, idle_thresh)
        idle_count = int(idle_result.split()[-1]) if idle_result else 0

        # Idle → Stale
        stale_result = await self.pool.execute("""
            UPDATE entity_sessions SET status = 'stale'
            WHERE status = 'idle'
            AND EXTRACT(EPOCH FROM (NOW() - last_activity_at))/60 > $1
        """, stale_thresh)
        stale_count = int(stale_result.split()[-1]) if stale_result else 0

        # Stale → Expired.
        # MUST route through runtime.end_session so the background
        # close-out fires (relationship state update, the inference_run
        # log). The old code called _close_session, which only flipped the
        # DB row. Fall back to _close_session only if the
        # runtime isn't available or end_session raises.
        expired_rows = await self.pool.fetch("""
            SELECT session_id FROM entity_sessions
            WHERE status = 'stale'
            AND EXTRACT(EPOCH FROM (NOW() - last_activity_at))/60 > $1
        """, expiry_thresh)
        for s in expired_rows:
            handled = False
            if self.runtime:
                try:
                    await self.runtime.end_session(s["session_id"])
                    handled = True
                except Exception as e:
                    log.warning(
                        "runtime.end_session failed for %s (cleanup expire): %s",
                        s["session_id"], e,
                    )
            if not handled:
                await self._close_session(s["session_id"], "expired")

        # The big tool results the session's turns stored (runtime/
        # result_store.py, v223) go with the session: its message list was
        # the only thing holding their handles.
        from mastercode.runtime.result_store import sweep as _sweep_results
        results_removed = await _sweep_results(self.pool)

        stats = {
            "idled": idle_count,
            "staled": stale_count,
            "expired": len(expired_rows),
            "results_removed": results_removed,
        }

        if any(v > 0 for v in stats.values()):
            log.info("Session cleanup: idled=%d staled=%d expired=%d",
                     stats["idled"], stats["staled"], stats["expired"])

        return stats

    async def start_cleanup_loop(self, interval_seconds: int = 900):
        """Start the background cleanup loop (every 15 minutes by default)."""
        async def _loop():
            while True:
                try:
                    await asyncio.sleep(interval_seconds)
                    await self.cleanup()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.error("Session cleanup error: %s", e)

        self._cleanup_task = asyncio.create_task(_loop())
        log.info("Session cleanup loop started (interval=%ds)", interval_seconds)

    async def stop_cleanup_loop(self):
        """Stop the background cleanup loop."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
            log.info("Session cleanup loop stopped")

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    async def _close_session(self, session_id: str, reason: str):
        """Low-level session close — just updates the DB row."""
        await self.pool.execute("""
            UPDATE entity_sessions
            SET status = 'closed', closed_at = NOW(), close_reason = $1
            WHERE session_id = $2 AND status NOT IN ('closed', 'expired', 'ended')
        """, reason, session_id)

    async def _refresh_session_prompt(
        self,
        session_id: str,
        entity_id: int,
        operator_id: int = None,
        context: str = None,
        strategy: str = "context_adaptive",
        agent_ratio: float = 0.6,
    ):
        """Rebuild the system prompt for an existing session from current entity state."""
        if not self.runtime:
            log.warning("Cannot refresh prompt — no runtime reference")
            return

        # Guard against recursive refresh (start_session → get_or_create → refresh → start_session)
        if session_id in self._refreshing:
            log.debug("Skipping recursive refresh for session %s", session_id[:8])
            return
        self._refreshing.add(session_id)

        # Use the runtime's full prompt assembly pipeline
        try:
            new_session_data = await self.runtime.start_session(
                entity_id=entity_id,
                operator_id=operator_id,
                context=context,
                strategy=strategy,
                agent_ratio=agent_ratio,
            )
            if "error" in new_session_data:
                log.warning("Prompt refresh failed: %s", new_session_data["error"])
                return

            new_prompt = new_session_data.get("system_prompt", "")
            new_session_id = new_session_data.get("session_id")

            # Copy the new prompt to the existing session
            await self.pool.execute("""
                UPDATE entity_sessions SET system_prompt = $1
                WHERE session_id = $2
            """, new_prompt, session_id)

            # Clean up the temp session we created for the prompt
            if new_session_id and new_session_id != session_id:
                await self.pool.execute(
                    "DELETE FROM entity_sessions WHERE session_id = $1",
                    new_session_id,
                )

            log.info("Refreshed prompt for session %s (entity %d)",
                     session_id[:8], entity_id)
        except Exception as e:
            log.error("Prompt refresh failed for session %s: %s", session_id[:8], e)
        finally:
            self._refreshing.discard(session_id)

    async def _load_session_metadata(self, session_id: str) -> Optional[dict]:
        """Load session metadata (not the full EntitySession object)."""
        row = await self.pool.fetchrow("""
            SELECT session_id, entity_id, status, channel, interaction_context,
                   message_count, created_at, last_activity_at,
                   EXTRACT(EPOCH FROM (NOW() - last_activity_at))/60 as idle_minutes
            FROM entity_sessions
            WHERE session_id = $1
        """, session_id)
        if not row:
            return None
        result = dict(row)
        result["reused"] = True
        return result

    async def _get_config(self, entity_id: int) -> dict:
        """Get session config for an entity (falls back to defaults)."""
        config = dict(SESSION_DEFAULTS)
        try:
            row = await self.pool.fetchrow(
                "SELECT behavioral_params FROM entity_registry WHERE id = $1",
                entity_id,
            )
            if row and row["behavioral_params"]:
                bp = row["behavioral_params"]
                if isinstance(bp, str):
                    bp = json.loads(bp)
                entity_config = bp.get("session_config", {})
                config.update(entity_config)
        except Exception:
            pass
        return config
