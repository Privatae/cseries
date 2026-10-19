"""
Task Manager — CRUD on entity_tasks + dashboard kanban sync.

Extracted from brain_loop.py task handling. The dashboard kanban is the
source of truth for task state — this module provides async DB access.
"""

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("cseries.brain.task_manager")


class TaskManager:
    """CRUD operations on entity_tasks table."""

    def __init__(self, pool, entity_id: int):
        self.pool = pool
        self.entity_id = entity_id

    async def create_task(self, title: str, task_type: str = "code",
                          priority: str = "normal", assigned_to: str = None) -> int:
        """Create a task in entity_tasks. Returns task_id."""
        # The turn type of the turn that started it (v186, turn types step 6).
        from mastercode.runtime.turn_types import current_turn_type_id
        row = await self.pool.fetchrow("""
            INSERT INTO entity_tasks (entity_id, title, task_type, priority, status, created_at,
                                      turn_type_id)
            VALUES ($1, $2, $3, $4, 'queued', NOW(), $5::uuid)
            RETURNING task_id
        """, self.entity_id, title, task_type, priority, current_turn_type_id())

        task_id = row["task_id"]
        log.info("Created task #%d: %s [%s]", task_id, title, task_type)

        if assigned_to:
            await self.pool.execute(
                "UPDATE entity_tasks SET assigned_to = $1 WHERE task_id = $2",
                assigned_to, task_id,
            )

        return task_id

    async def update_task_status(self, task_id: int, status: str):
        """Update a task's status (queued, in_progress, completed, cancelled)."""
        now_col = ""
        if status == "in_progress":
            now_col = ", started_at = NOW()"
        elif status == "completed":
            now_col = ", completed_at = NOW()"

        await self.pool.execute(
            f"UPDATE entity_tasks SET status = $1{now_col} WHERE task_id = $2",
            status, task_id,
        )
        log.info("Task #%d → %s", task_id, status)

    async def get_pending_tasks(self, limit: int = 8) -> list[dict]:
        """Get pending/queued tasks ordered by priority."""
        try:
            rows = await self.pool.fetch("""
                SELECT task_id, title, task_type, priority, status
                FROM entity_tasks
                WHERE entity_id = $1 AND status = 'queued'
                ORDER BY
                    CASE priority
                        WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                        WHEN 'normal' THEN 2 WHEN 'low' THEN 3
                    END,
                    created_at ASC
                LIMIT $2
            """, self.entity_id, limit)
            return [dict(r) for r in rows]
        except Exception:
            return []

    async def get_in_progress_tasks(self, limit: int = 5) -> list[dict]:
        """Get tasks being actively worked on (not delegated)."""
        try:
            rows = await self.pool.fetch("""
                SELECT task_id, title, task_type, priority, status, started_at
                FROM entity_tasks
                WHERE entity_id = $1
                  AND status = 'in_progress'
                  AND (assigned_to IS NULL OR assigned_to = '')
                ORDER BY started_at DESC NULLS LAST
                LIMIT $2
            """, self.entity_id, limit)
            return [dict(r) for r in rows]
        except Exception:
            return []

    async def get_delegated_tasks(self, limit: int = 5) -> list[dict]:
        """Get tasks delegated to oracle roles or other agents."""
        try:
            rows = await self.pool.fetch("""
                SELECT task_id, title, task_type, priority, status,
                       assigned_to, started_at
                FROM entity_tasks
                WHERE entity_id = $1
                  AND assigned_to IS NOT NULL
                  AND assigned_to != ''
                  AND status NOT IN ('completed', 'cancelled')
                ORDER BY created_at DESC
                LIMIT $2
            """, self.entity_id, limit)
            return [dict(r) for r in rows]
        except Exception:
            return []

    async def get_recent_completed(self, limit: int = 3) -> list[dict]:
        """Get recently completed tasks."""
        try:
            rows = await self.pool.fetch("""
                SELECT task_id, title, status, completed_at
                FROM entity_tasks
                WHERE entity_id = $1 AND status = 'completed'
                ORDER BY completed_at DESC NULLS LAST
                LIMIT $2
            """, self.entity_id, limit)
            return [dict(r) for r in rows]
        except Exception:
            return []

    async def get_task(self, task_id: int) -> dict | None:
        """Get a single task by ID."""
        try:
            row = await self.pool.fetchrow(
                "SELECT * FROM entity_tasks WHERE task_id = $1 AND entity_id = $2",
                task_id, self.entity_id,
            )
            return dict(row) if row else None
        except Exception:
            return None

