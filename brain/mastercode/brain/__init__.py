"""Background lanes that run beside the chat turn.

What is left here is the scheduled/offline half of the host, not a reasoning
loop: offline_task_runner (the scheduled-turn lane), entity_clock (recurrence),
task_manager (the entity_tasks registry) and replica_reconciler.

This file re-exported the UnifiedBrain package (brain/lens/synthesis/
constitutional/watchdog_lens/task_executor/model_client and friends) until
2026-09-19. Those modules were deleted: nothing called them, and because the
re-export lived here every import of offline_task_runner executed the whole
package at boot, which is why they looked loaded while never running. Keep this
module free of re-exports so an import of one lane cannot drag in another.
"""
