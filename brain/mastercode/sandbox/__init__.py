"""Bot sandbox primitives (Ship G3, 2026-05-27).

Submodules:
    sql_proxy — wraps asyncpg.Pool to enforce capability-bit restrictions
                on SQL statements executed via ctx.pool inside a bot.

The bot lane is retired; no signed bot code reaches a pool. SandboxedPool
wraps first-party callers (runtime/context_ops.py, runtime/link_ops.py,
runtime/task_record.py) with a constant 1|2|4 mask.
"""
