"""
Brain Service — Bridge between PC container FastAPI and entity brain code.

Manages:
  - asyncpg pool to embedded PostgreSQL
  - EntityRuntime initialization
  - Session lifecycle (create, resume, close)
  - Message handling with SSE streaming
"""

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from typing import Optional, AsyncIterator

import asyncpg

logger = logging.getLogger("privatae.brain")

# Singleton
_pool: Optional[asyncpg.Pool] = None
# The bastion CONTROL-PLANE lane. This pod's postgres allows
# max_connections=16 (entrypoint-postgres.sh) and the brain pool alone held 10 of
# them when this lane was carved out (it holds 6 now — the recall lane below took 4
# out of it, not on top of it), so enrollment, heartbeats and the endpoint-rotation
# directive were competing
# with every app in the host's Workbench for what was left. Measured that day: a client
# opening a 10-connection pool exhausted the server and bastion enrollment began
# returning 502 at the same moment — one busy app can take a host's whole bastion fleet
# offline. Two connections held back is enough: these queries are small, serialised per
# beat, and must not fail while a noisy app is mid-burst. Deliberately a SEPARATE pool
# rather than a bigger shared one, because the property we want is isolation, not more
# capacity.
_control_pool: Optional[asyncpg.Pool] = None

# The AI's READ lane. Same server, same
# socket, DIFFERENT ROLE: `host_recall`, which migrations/v139 creates as
# NOSUPERUSER + NOBYPASSRLS. Everything else in this process keeps `entity`,
# because 38 CREATE TABLE IF NOT EXISTS statements across 23 files in server/ run
# at REQUEST time and a non-superuser cannot execute them — and `IF NOT EXISTS`
# does not soften it, because permission is checked BEFORE existence (measured on
# PG 17.10: the statement raises `permission denied for schema public` for a
# non-owner even when the table is already there). So a downgraded kernel fails on
# the first request touching any of those paths, on every host, at once.
#
# The count is 38/23 and not the "20+" this comment used to carry: a raw grep says
# 40 across 25, and two of those matches are prose — one a `#` comment, one inside
# a DOCSTRING, which is why a comment filter missed it and the number was wrong
# twice before an AST walk settled it. Retiring those sites is what unlocks the
# strong form of this design; it is tracked separately and this branch does not
# depend on it.
#
# What this buys, stated no larger than it is: a row security policy on
# entity_memory binds this connection and cannot bind the other one. Today no
# policy exists, so the two lanes return identical rows — that is the point, the
# plumbing lands with no behaviour change and the policies come later, one table
# at a time.
_recall_pool: Optional[asyncpg.Pool] = None

# The Host's own schema.
# Role `host_world` (migrations/v233): it owns `world`, reads only the kernel
# tables v233 lists as READABLE, and writes nothing else. The `data` tool runs
# the MODEL'S SQL here. Unlike the recall lane this one never falls back: a
# missing pool means the tool answers "unavailable", because running the model's
# SQL as `entity` would be running it as the superuser.
_world_pool: Optional[asyncpg.Pool] = None

# All three pools' shapes are hoisted out of their create_pool() calls so the
# sizing contract is testable in one place (tests/test_bastion_control_pool_reserved.py
# asserts the maxes together stay inside the max_connections the entrypoint
# writes). A test that carried its own copy of these numbers would keep passing
# while they diverged, which is the whole failure this set guards against.
BRAIN_POOL_KWARGS = {
    # min=1: each idle backend holds ~10-25MB unique RSS and this pod's postmaster
    # is exclusively ours — there's no cold-start penalty worth reserving a second
    # warm connection for.
    "min_size": 1,
    # 10 until the recall lane below existed. The four connections recall now
    # holds came OUT of this number rather than on top of it: the server still
    # allows 16 and the harness sidecar, diagnostics and the Workbench keep the
    # same 4 they had (the entrypoint's own arithmetic, entrypoint-postgres.sh:172-180).
    # Recall was the hottest single consumer of this pool, so what it gives up
    # here is close to what it stops spending.
    "max_size": 6,
    # 60s inactive-prune (down from asyncpg's 300s default) shrinks the boot-burst
    # backends back to the floor quickly. Correct HERE and wrong for the control
    # lane below: this pool is sized for burst and SHOULD hand memory back.
    "max_inactive_connection_lifetime": 60,
    "command_timeout": 30,
}

CONTROL_POOL_KWARGS = {
    "min_size": 1,
    "max_size": 2,
    # NEVER prune this lane. 0 disables asyncpg's inactivity mechanism (pool.py:
    # the `if self._max_inactive_time:` guard around the call_later timer).
    #
    # At the 300s default the reservation quietly evaporated on exactly the pods it
    # was built for: PoolConnectionHolder._deactivate_inactive_connection terminates
    # the idle connection, and min_size is honoured ONLY inside Pool._initialize —
    # nothing re-establishes it afterwards. A host that had been quiet for five
    # minutes therefore held ZERO reserved connections, and the next redeem opened a
    # fresh one against the same exhausted server (max_connections=16) this lane
    # exists to escape. Enrolment is bursty and rare BY NATURE, so idleness is its
    # normal state and must not disarm it.
    #
    # Cost: EVERY holder in this pool gets the same lifetime (asyncpg builds one per
    # max_size slot), so the steady state is one backend held for the pod's life and
    # up to two once a burst has ever used the second slot — ~10-25MB RSS each (the
    # density figure on BRAIN_POOL_KWARGS above). max_connections budgets both. That
    # cost IS the reservation.
    "max_inactive_connection_lifetime": 0,
    "command_timeout": 15,
}

RECALL_POOL_KWARGS = {
    # min=1 for the same reason the brain pool takes it: recall runs on the hot
    # path of essentially every turn (the per-turn pre-fire in
    # memory_engine.build_memory_context), so the connection should exist before
    # it is wanted.
    "min_size": 1,
    # 4 of the brain pool's former 10. Recall is serialised WITHIN a turn — one
    # embedding, then the arms — so this bounds concurrent TURNS, not queries per
    # turn. 2 (the control lane's size) would make two simultaneous chats queue on
    # each other; 4 leaves room for the circles/app surfaces that also recall.
    "max_size": 4,
    # Prunes like the brain pool rather than holding like the control lane: this
    # lane is frequent, not rare, so an idle host should hand the backends back.
    "max_inactive_connection_lifetime": 60,
    "command_timeout": 30,
}

WORLD_POOL_KWARGS = {
    # min=0: the Host's own SQL is occasional, so an idle Host holds no backend
    # for it. The role's CONNECTION LIMIT is 3 (v233): these 2, plus one for a
    # snapshot or restore (pg_dump / pg_restore connect on their own).
    "min_size": 0,
    "max_size": 2,
    "max_inactive_connection_lifetime": 60,
    # No pool-wide timeout: every call passes its own (the `data` tool's
    # timeout_s), and asyncpg cancels the statement from outside when it expires.
    "command_timeout": None,
}

_runtime = None
_entity_id: int = 1  # Always entity 1 on personal computer


async def initialize(entity_id: int = 1) -> None:
    """Create asyncpg pool and initialize EntityRuntime.

    Called once during FastAPI lifespan startup.
    """
    global _pool, _runtime, _entity_id
    _entity_id = entity_id

    # Connect to embedded PG via unix socket (no TCP, no password)
    db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://entity@/hostmem?host=/var/run/postgresql",
    )
    _pool = await asyncpg.create_pool(db_url, **BRAIN_POOL_KWARGS)
    # SIZES READ FROM THE KWARGS, never retyped. This line said max=10 after the
    # recall lane took four connections out of the brain pool and left it at 6 —
    # caught in review. A boot log that misreports the LARGEST pool by four is
    # worst exactly where it gets read: a connection-exhaustion debug, where the
    # arithmetic is the whole question. Interpolating removes the class.
    logger.info("Brain DB pool created (min=%s, max=%s, idle-prune=60s)",
                BRAIN_POOL_KWARGS["min_size"], BRAIN_POOL_KWARGS["max_size"])

    # The person's limits (runtime/user_limits), held for readers outside a
    # turn before anything that enforces one starts. Never raises: unread, the
    # coded defaults hold until the next read.
    try:
        from mastercode.runtime import user_limits
        await user_limits.load(_pool, _entity_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("limits not loaded at boot (defaults hold): %s", e)

    # The reserved bastion control-plane lane (see _control_pool above). min=1 so the
    # connection exists BEFORE it is needed — acquiring it under contention is exactly
    # the case this pool defends against. A failure here is not fatal: callers fall back
    # to the brain pool, which is the behaviour that shipped before this lane existed.
    global _control_pool
    try:
        _control_pool = await asyncpg.create_pool(db_url, **CONTROL_POOL_KWARGS)
        logger.info("Bastion control-plane pool created (min=%s, max=%s, never pruned) — reserved lane",
                    CONTROL_POOL_KWARGS["min_size"], CONTROL_POOL_KWARGS["max_size"])
    except Exception as e:
        _control_pool = None
        logger.warning("control-plane pool unavailable (%s) — falling back to the brain pool", e)

    # The AI read lane on `host_recall` (see _recall_pool above). The URL is
    # whatever entrypoint.sh resolved — never a literal, because the entity
    # database is `hostmem` on current clusters and still `parallax` on any host
    # whose rename has not happened (entrypoint.sh:196-262). When the env is
    # absent (a bench or a test harness that set only DATABASE_URL) the role is
    # swapped into that URL rather than guessed.
    global _recall_pool
    try:
        _recall_pool = await asyncpg.create_pool(
            _recall_db_url(db_url), **RECALL_POOL_KWARGS)
        logger.info("Recall pool created on role host_recall (min=%s, max=%s) — "
                    "the AI read lane",
                    RECALL_POOL_KWARGS["min_size"], RECALL_POOL_KWARGS["max_size"])
    except Exception as e:
        # Fail SOFT, deliberately, for as long as no policy exists: a host whose
        # v139 has not applied must still answer, and while the two lanes are
        # observationally identical there is nothing to lose by using the other
        # one. THE DAY A POLICY LANDS THIS MUST BECOME FATAL — falling back to
        # the superuser lane under a policy means the Host silently reads rows
        # its governing terms exclude, which is the exact failure the split
        # exists to remove. See get_recall_pool().
        _recall_pool = None
        logger.warning(
            "recall pool unavailable (%s) — the Host's read path is running on "
            "the privileged `entity` role. Harmless while no row security "
            "policy exists; NOT harmless once one does.", e)

    # The Host's own schema, on `host_world` (see _world_pool above). min_size is
    # 0, so creating the pool opens nothing; the first `data` call connects. A
    # failure leaves it None and the tool says so. There is no fallback.
    global _world_pool
    try:
        _world_pool = await asyncpg.create_pool(_world_db_url(db_url), **WORLD_POOL_KWARGS)
        logger.info("World pool created on role host_world (min=%s, max=%s) — the Host's own schema",
                    WORLD_POOL_KWARGS["min_size"], WORLD_POOL_KWARGS["max_size"])
    except Exception as e:
        _world_pool = None
        logger.warning("world pool unavailable (%s) — the data tool will answer unavailable", e)

    # Hand the pool to the provider gateway. Its module-level _inference_pool
    # (brain/mastercode/provider_gateway.py:91) starts as None and the setter
    # (:104) had no caller, so BOTH pool consumers were dead: the per-call
    # inference_cost_log write in _log_inference, and the local-model endpoint
    # lookup on the failover path (:1810, match_local_model(_inference_pool,
    # ...) — which silently resolved no endpoint). Wired the same way triage.py
    # wires set_prompt_console (runtime/triage.py:40-45). Non-fatal: telemetry
    # must never take the pod's DB down with it.
    try:
        from mastercode.provider_gateway import set_inference_pool
        set_inference_pool(_pool)
        logger.info("Inference pool wired into provider_gateway")
    except Exception as e:
        logger.warning("provider_gateway inference pool not wired (%s)", e)

    # Import brain code (PYTHONPATH includes /app/brain)
    try:
        from mastercode.entity_runtime import EntityRuntime

        _runtime = EntityRuntime(_pool)
        _runtime._primary_entity_id = _entity_id
        await _runtime.initialize()
        logger.info("EntityRuntime initialized for entity %d", _entity_id)

        # Lane-B agent SDK: one AgentDispatch per runtime so its in-flight map
        # (task_id -> background task) survives across chat turns — that map is
        # what lets a user veto (cancel) a running agent. The /api/agents route
        # and (later) the host turn share THIS instance.
        from mastercode.runtime.agent_dispatch import AgentDispatch
        _runtime.agent_dispatch = AgentDispatch(_pool)
        # Back-reference so a completed agent can wake the host to deliver its
        # result (the async lifecycle: dispatch → confirm → completion-wakes-host
        # → deliver). Without it, delivery + the monitor no-op (record still lands).
        _runtime.agent_dispatch.runtime = _runtime
        logger.info("AgentDispatch (Lane B) attached to runtime")

        # No-orphans safety net: a background loop that delivers any completed-
        # but-undelivered agent (covers a dropped in-process handler) and marks
        # + delivers any agent stuck in_progress past the timeout. Kept on the
        # runtime so it isn't GC'd. Robust: a tick error is logged, never fatal.
        # Restart safety FIRST: any agent/build row still live-status at boot
        # belongs to a dead process — fail + deliver it now, not hours later
        # when the stuck horizon happens to pass (batch-3 restart-safe sweep).
        try:
            await _runtime.agent_dispatch.reap_orphans_on_boot()
        except Exception as e:
            logger.warning("boot orphan reap failed (non-fatal): %s", e)

        try:
            # stuck_after is a NO-HEARTBEAT window, not a runtime cap (the scan
            # keys on liveness — scoped_agent stamps per tool call AND per model
            # round). 1800s: a healthy agent heartbeats at least once per round,
            # so 30 silent minutes means a true wedge, while the slowest
            # legitimate single inference call stays comfortably inside it.
            _runtime._agent_monitor_task = asyncio.create_task(
                _runtime.agent_dispatch.run_monitor_loop(
                    interval_seconds=30, stuck_after_seconds=1800,
                )
            )
            logger.info("AgentDispatch monitor loop started (30s cadence, 30m no-heartbeat reap)")
        except Exception as e:
            logger.warning("AgentDispatch monitor loop failed to start: %s", e)

        # A task's serial run (runtime/task_serial.py) is driven by
        # a loop in this process; a run still marked running lost its loop
        # with the last process. Drive each again. Kept on the runtime so the
        # resume task is not collected; a failure is logged, never fatal.
        try:
            from mastercode.runtime import task_serial
            _runtime._task_runs_resume = asyncio.create_task(
                task_serial.resume_running(_runtime, _pool, _entity_id))
        except Exception as e:
            logger.warning("task runs not resumed (non-fatal): %s", e)

        # The research store's sweep (runtime/research_store.py):
        # a saved page nobody has saved, searched or read for 30 days goes —
        # its row, its chunks, its file. Kept on the runtime so the task is
        # not collected; a failure is logged, never fatal.
        try:
            from mastercode.runtime import research_store
            _runtime._research_sweep = asyncio.create_task(research_store.sweep_forever())
        except Exception as e:
            logger.warning("research store sweep not started (non-fatal): %s", e)

        # Start the session lifecycle cleanup loop. Without this, sessions
        # never transition active → idle → stale → expired, so
        # _end_session_background never fires. (This loop was once defined
        # but never started.)
        try:
            await _runtime.session_handler.start_cleanup_loop(interval_seconds=900)
        except Exception as e:
            logger.warning("session_handler cleanup loop failed to start: %s", e)
    except Exception as e:
        logger.warning("EntityRuntime unavailable (%s) — memory browsing still works", e)
        _runtime = None


def _recall_db_url(db_url: str) -> str:
    """The same database over the same socket, as `host_recall` instead of
    `entity`.

    RECALL_DATABASE_URL (exported by entrypoint.sh beside DATABASE_URL) wins.
    Without it the role is swapped into `db_url` — which keeps the database name
    and socket path the entrypoint actually resolved, instead of re-hardcoding
    `hostmem` and stranding any host still on the legacy `parallax` name. Any
    password on the incoming URL is dropped with the user it belonged to.
    """
    env = os.environ.get("RECALL_DATABASE_URL")
    if env:
        return env
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(db_url)
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    return urlunsplit((p.scheme, f"host_recall@{host}", p.path, p.query, p.fragment))


def _world_db_url(db_url: str) -> str:
    """The same database over the same socket, as `host_world`.

    WORLD_DATABASE_URL (exported by entrypoint.sh) wins; otherwise the role is
    swapped into `db_url` the way _recall_db_url does it."""
    env = os.environ.get("WORLD_DATABASE_URL")
    if env:
        return env
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(db_url)
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    return urlunsplit((p.scheme, f"host_world@{host}", p.path, p.query, p.fragment))


async def shutdown() -> None:
    """Clean up pool on app shutdown."""
    global _pool, _runtime, _control_pool, _recall_pool, _world_pool
    if _world_pool:
        await _world_pool.close()
        _world_pool = None
    if _recall_pool:
        await _recall_pool.close()
        _recall_pool = None
    if _control_pool:
        await _control_pool.close()
        _control_pool = None
    if _runtime is not None:
        _mt = getattr(_runtime, "_agent_monitor_task", None)
        if _mt is not None and not _mt.done():
            _mt.cancel()
    if _pool:
        await _pool.close()
        _pool = None
    _runtime = None
    logger.info("Brain service shutdown")


def get_pool() -> Optional[asyncpg.Pool]:
    """Return the brain DB pool."""
    return _pool


def get_control_pool() -> Optional[asyncpg.Pool]:
    """The reserved bastion control-plane pool (enrollment, heartbeat, rotate).

    Falls back to the brain pool when the reserved lane could not be created, so a
    caller never has to branch: the worst case is the shared-pool behaviour, never a
    missing pool. Use this for bastion lifecycle queries ONLY — widening its callers
    is the same as not having reserved anything.
    """
    return _control_pool or _pool


def get_recall_pool() -> Optional[asyncpg.Pool]:
    """The pool the Host's READ path uses — role `host_recall`, NOSUPERUSER and
    NOBYPASSRLS (migrations/v139_host_recall_role.sql).

    Returns None when the lane could not be created, and the ONE caller
    (mastercode.memory_retrieval.recall_memories) then falls back to the pool it
    was handed. That fallback is correct only while entity_memory carries no row
    security policy: with a policy installed, falling back means answering from
    the lane the policy cannot bind, so the first change that installs a policy
    must also make this a hard failure rather than a fallback.

    Deliberately NOT `_recall_pool or _pool` — unlike get_control_pool, whose
    fallback costs a connection slot, this one would silently cost the property
    the whole split exists to create. The caller has to see the None.
    """
    return _recall_pool


def world_dsn() -> str:
    """The `host_world` DSN, for the snapshot and restore steps that run pg_dump
    and psql as their own connections (one of the role's three)."""
    return _world_db_url(os.environ.get(
        "DATABASE_URL", "postgresql://entity@/hostmem?host=/var/run/postgresql"))


def get_world_pool() -> Optional[asyncpg.Pool]:
    """The pool the `data` tool runs the Host's own SQL on — role `host_world`
    (migrations/v233_host_world.sql). None when it could not be created, and the
    caller answers "unavailable". Never `_world_pool or _pool`: the brain pool is
    the superuser."""
    return _world_pool


def get_runtime():
    """Return the EntityRuntime instance."""
    return _runtime


async def create_session(
    context: str = "chat",
    channel: str = "chat",
    user_id: Optional[str] = None,
) -> dict:
    """Create a new brain session and return session metadata."""
    if not _runtime:
        raise RuntimeError("Brain not initialized")

    session = await _runtime.start_session(
        entity_id=_entity_id,
        context=context,
        channel=channel,
        user_id=user_id or "local",
    )
    if isinstance(session, dict):
        if session.get("error"):
            raise RuntimeError(session["error"])
        return {
            "session_id": session.get("session_id") or session.get("id"),
            "entity_id": session.get("entity_id", _entity_id),
            "status": "active",
            "channel": session.get("channel", channel),
        }
    return {
        "session_id": session.session_id,
        "entity_id": session.entity_id,
        "status": "active",
        "channel": getattr(session, "channel", channel),
    }


async def get_session_messages(session_id: str) -> Optional[list[dict]]:
    """Return the full message history for a session, ordered as the
    session recorded them. Returns None if the session doesn't exist.

    Reads entity_sessions.messages (jsonb) directly — no need to
    materialize a full EntitySession just to render the UI's history
    pane. Filters to {role, content} per message; system/tool entries
    are pruned (the chat UI only renders user + assistant turns)."""
    if _pool is None:
        return None
    row = await _pool.fetchrow(
        "SELECT messages FROM entity_sessions WHERE session_id = $1",
        session_id,
    )
    if row is None:
        return None
    raw = row["messages"]
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    out: list[dict] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        # Some entries carry structured content blocks (e.g. attachments
        # serialized as Anthropic-shaped lists). Flatten to text where
        # possible — fall back to skipping the entry if there's nothing
        # the UI can display as a string.
        if isinstance(content, list):
            text_parts = [
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            content = "".join(text_parts)
        if not isinstance(content, str) or not content.strip():
            continue
        out.append({"role": role, "content": content})
    return out


async def resume_session(session_id: str) -> Optional[dict]:
    """Resume an existing session."""
    if not _runtime:
        raise RuntimeError("Brain not initialized")

    session = await _runtime._load_session_from_db(session_id)
    if not session:
        return None

    if isinstance(session, dict):
        return {
            "session_id": session.get("session_id", session_id),
            "entity_id": session.get("entity_id", _entity_id),
            "status": session.get("status", "active"),
            "message_count": session.get("message_count", 0),
            "channel": session.get("channel", "chat"),
        }
    return {
        "session_id": session.session_id,
        "entity_id": session.entity_id,
        "status": session.status,
        "message_count": session.message_count,
        "channel": getattr(session, "channel", "chat"),
    }


# ── The never-silent stream invariant ──────────────────────────────────
#
# A live edge test: a first-message-in-a-project turn sat silent
# 30-90s (context build + several tool rounds before any visible output)
# and something in the edge chain killed the quiet connection → 502. The
# old inline heartbeat had a hole: `last_emit` reset on EVERY runtime
# event, including events that map to NO frame (_frame_for_event → None,
# e.g. 'start'), so frameless event chatter silenced the wire indefinitely
# while the silence clock kept resetting. And it only covered the
# pre-result loop — post-processing stretches were unguarded.
#
# This wrapper holds the invariant for the WHOLE turn, no matter which
# lane serves it: the inner generator is pumped through a queue; any
# quiet stretch ≥ _STREAM_HEARTBEAT_S puts a `2:[]` keepalive on the wire
# (the documented no-op data frame all three shipped clients explicitly
# tolerate — workbench stream.ts, circles-stream.ts, dashboard
# chat-stream.js). Per-turn observability so this never needs guessing
# again: one HOST_SDK_STREAM summary line with time-to-first-frame, the
# max wire gap, and frame counts.

_STREAM_HEARTBEAT_S = 8.0  # max wire silence; well under the ~30s edge budget


async def _never_silent(inner: AsyncIterator[str]) -> AsyncIterator[str]:
    """Wrap any frame stream in the never-silent multiplexer: quiet stretches
    ≥ _STREAM_HEARTBEAT_S put a `2:[]` keepalive on the wire. Wire-level only —
    heartbeats are never buffered by the turn registry."""
    q: "asyncio.Queue" = asyncio.Queue()
    _SENTINEL = object()

    async def _pump() -> None:
        try:
            async for frame in inner:
                await q.put(frame)
        except Exception as e:  # surfaced on the wire as a terminal 3:
            await q.put(e)
        finally:
            await q.put(_SENTINEL)

    pump_task = asyncio.create_task(_pump())
    t0 = time.time()
    last_yield = t0
    ttfb: float | None = None
    max_gap = 0.0
    frames = 0
    heartbeats = 0
    try:
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=_STREAM_HEARTBEAT_S)
            except asyncio.TimeoutError:
                now = time.time()
                max_gap = max(max_gap, now - last_yield)
                last_yield = now
                heartbeats += 1
                if ttfb is None:
                    ttfb = now - t0
                yield "2:[]\n"
                continue
            if item is _SENTINEL:
                break
            now = time.time()
            max_gap = max(max_gap, now - last_yield)
            last_yield = now
            frames += 1
            if ttfb is None:
                ttfb = now - t0
            if isinstance(item, Exception):
                logger.exception("Error in brain message handling", exc_info=item)
                yield f'3:{json.dumps(str(item))}\n'
                break
            yield item
    finally:
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info(
            "HOST_SDK_STREAM ttfb=%.2fs max_gap=%.2fs frames=%d heartbeats=%d dur=%.1fs",
            (ttfb if ttfb is not None else time.time() - t0),
            max_gap, frames, heartbeats, time.time() - t0,
        )



def turn_type_frame(event: dict) -> str:
    """Turn types step 6: the type a turn resolved to, as a 2: data frame for
    the turn registry (the timeline names each running turn's type). A data
    item clients do not know is ignored (the same contract as "session").
    Ids and the status only — never a type's name or words; the
    status is always present ("untyped" included), because a missing one
    reads as "not known yet"."""
    return "2:" + json.dumps([{
        "type": "turn_type",
        "turn_type_id": event.get("turn_type_id"),
        "turn_type_status": event.get("turn_type_status") or "untyped",
    }]) + "\n"

def _lands_in_circles(kwargs: dict) -> bool:
    """Whether a send names no thread and would write into Circles: no
    project, and the conversation it derives (session.
    transient_interaction_context, the one derivation) is Circles chat.

    Never a hidden-lane send (a fired wake-up, a replay, an A/B variant;
    memory_engine.HIDDEN_LANE_APP_IDS): its caller owns its thread. A
    scheduled task gets its topic from offline_task_runner.
    ensure_origin_thread and records it; one picked here would be a topic
    the task never knows about."""
    if kwargs.get("thread_id") or kwargs.get("project_id") is not None:
        return False
    from mastercode.memory_engine import HIDDEN_LANE_APP_IDS
    if str(kwargs.get("app_id") or "") in HIDDEN_LANE_APP_IDS:
        return False
    from mastercode.runtime.session import transient_interaction_context
    ic, _ = transient_interaction_context(None, kwargs.get("app_id"),
                                          kwargs.get("history_context"))
    return str(ic or "") in ("", "chat", "circles")


async def _topic_for_threadless(entity_id: int, message: str) -> str | None:
    """The topic a Circles send that names no thread goes to: the topic the
    search clearly matches (thread_pick.clear_topic_for, the pick's own first
    step, with no model call), else a new topic titled from the message. The
    Circles page and the connectors pick before they send, so this is the
    net under every other caller. None only when no thread could be read or
    written; the turn then runs as before."""
    from mastercode.circles_threads import home_thread
    pool = get_pool()
    try:
        from server.connectors import thread_pick as _tp
        joined = await _tp.clear_topic_for(pool, entity_id, message)
        if joined:
            return joined
    except Exception as e:  # noqa: BLE001 - a new topic below
        logger.warning("topic search for a threadless send failed (entity %s): %s", entity_id, e)
    return await home_thread(pool, entity_id, None, title=message)


async def send_message(*args, **kwargs) -> AsyncIterator[str]:
    """Public stream. The turn itself runs DETACHED in the turn registry
    (server/turn_registry.py) — this generator is only a TAIL of the turn's
    frame buffer, wrapped in the never-silent multiplexer. Killing this
    response (phone sleep, tab switch, edge timeout) kills the tail; the turn
    keeps running, stays listed, and any client reattaches via the registry.

    Per-thread turn lock lives here too: an identical resend while the
    thread's turn runs is a pure reattach (never a second turn / double
    spend); a different text becomes a nudge into the live tool loop.

    `client_id` (optional, nudge_id_ok): the sender's id for the words. It is
    kept on the person's user row (session.py) and, when the words land in a
    running turn as a nudge, with the nudge (turn_registry.start_or_attach
    nudge_client_id), so the sender can tell later that they were delivered
    (the Workbench Index, routes/workbench_index.py).

    The lock is keyed by the conversation (turn_registry.compose_thread_key):
    `thread_id` (Circles threads), else `project_id`, else the
    `history_context` the turn thinks in, else the app's own conversation.
    All three ride through **kwargs to _send_message_inner, which declares
    them. So two open Circles threads are two lanes, and two apps in one
    thread, one project or the original conversation are one."""
    from server.turn_registry import (
        TurnBusyError, TurnCapError, compose_thread_key, get_turn_registry)

    entity_id = kwargs.get("entity_id")
    # The sender's id names the app that sent it (pending_words.SentId): the
    # route's app (app_id) unless the route already marked it (the nudge
    # routes, a boot-pass resend). An id counts as delivered only for that
    # app, so app A's id never answers app B's send.
    if kwargs.get("client_id"):
        from mastercode.pending_words import sent_ids
        kwargs["client_id"] = sent_ids(kwargs["client_id"], kwargs.get("app_id"))
    # Every Circles turn has a thread (there is no original conversation
    # thread; with all metadata written correctly none is needed). A turn that would
    # land in Circles with none named
    # (the brain API, a forwarded decision, a card answered after its turn, a
    # nudge replayed after its turn, an older client) goes through the same
    # topic pick a Circles message does: it joins a topic it clearly continues
    # or starts one. Nothing is written to the legacy app:circles scope.
    if (not kwargs.get("thread_id") and entity_id is not None and get_pool() is not None
            and _lands_in_circles(kwargs)):
        kwargs["thread_id"] = await _topic_for_threadless(
            entity_id, kwargs.get("message", "") or (args[1] if len(args) > 1 else ""))
    # A thread the person DELETED runs no turn (circles_threads.delete_thread).
    # The Circles send route has already refused a person's own message to it;
    # this refuses everything else addressed to it — above all a wake-up set
    # in it, which the delete cancelled. Run in the original conversation
    # instead, its turn would leave what it learned there, where no delete
    # reaches.
    if kwargs.get("thread_id") and entity_id is not None and get_pool() is not None:
        from mastercode.circles_threads import DELETED_THREAD_REFUSAL, live_thread_id
        _live = await live_thread_id(get_pool(), entity_id, kwargs["thread_id"])
        if _live is None:
            yield f'3:{json.dumps(DELETED_THREAD_REFUSAL)}\n'
            return
        # A topic folded into the thread the Host picked for its message
        # (runtime/thread_choice) sends to that thread.
        kwargs["thread_id"] = _live
    # The root thread takes only the person's own words (runtime/root_turn):
    # a send there, a new turn or a nudge into the running one, needs
    # `from_person` (set only by a route that checked its caller's app token)
    # and no forwarded or carried message. A platform-started turn (the Host
    # opening it, turn_type op='root') is the Host's own.
    from_person = bool(kwargs.pop("from_person", False))
    if kwargs.get("thread_id") and entity_id is not None and get_pool() is not None \
            and not kwargs.get("system_turn"):
        from mastercode.runtime.root_turn import words_refusal
        _why = await words_refusal(get_pool(), entity_id, kwargs["thread_id"],
                                   person=from_person, frame=kwargs.get("frame"))
        if _why:
            yield f'3:{json.dumps(_why)}\n'
            return
    # Thread identity: explicit key > legacy session lane (voice/demo callers
    # pass session_id — each session IS its own thread, never pooled) > the
    # conversation the chat surfaces compose (thread, project, the borrowed
    # history_context, else the app's own).
    thread_key = kwargs.pop("thread_key", None)
    if not thread_key:
        if kwargs.get("session_id"):
            thread_key = f"session|{kwargs['session_id']}"
        else:
            thread_key = compose_thread_key(
                entity_id, kwargs.get("app_id"), kwargs.get("project_id"),
                thread_id=kwargs.get("thread_id"),
                history_context=kwargs.get("history_context"))
    message = kwargs.get("message", "") or (args[1] if len(args) > 1 else "")
    # Turn types step 6: the turn's task (created inside start_or_attach
    # below) inherits this, so its timeline can leave itself out.
    from server.turn_registry import CURRENT_THREAD_KEY
    CURRENT_THREAD_KEY.set(thread_key)
    # Registry-only: the label a re-attaching client shows for a
    # platform-started turn (TurnRecord.display_message). Popped so the inner
    # generator never sees it.
    display_message = kwargs.pop("display_message", None)
    # Registry-only too, and set only by server code: the message came
    # through a route whose client keeps the words and sends them again itself
    # (the Circles nudge route, through session.answer_after_turn). Every
    # other message that lands in a running turn as a nudge is the server's to
    # deliver: if that turn never reads it, it goes on as the thread's next
    # turn (turn_registry.start_or_attach nudge_keeps_copy).
    nudge_keeps_copy = bool(kwargs.pop("nudge_keeps_copy", False))
    # Registry-only too: the pending_words rows holding the message's words
    # (mastercode/pending_words). They go with the words — settled once the
    # turn that has them as its message has started, or carried with the
    # nudge they become (turn_registry.start_or_attach pending_key).
    pending_keys = kwargs.pop("pending_keys", None)
    # Registry-only too, and set only by server code: False when the caller
    # has read the database for this client_id and found nothing delivered
    # under it (the Workbench Index's send, routes/workbench_index
    # .start_page_turn, after thread_pick.find_index_send). A turn the
    # registry still lists under the id then never had the words: it died
    # first, and a finished turn stays listed for DONE_TTL_S, longer than the
    # Index keeps its sends (SEND_REPEAT_S). So the send follows no turn by its
    # id (started_with below) and runs, and a dead turn is never taken for the
    # words' delivery.
    follow_by_id = kwargs.pop("follow_by_id", True) is not False
    # A connector's forwarded message that lands in a running turn keeps its
    # connector_origin on the row written for it, as it does when it starts a
    # turn (the frame; session.push_nudge).
    _frame = kwargs.get("frame")
    _origin = _frame.get("connector_origin") if isinstance(_frame, dict) else None
    # Likewise a message of a person's chat given to this turn (Ask Ivy,
    # routes/circles.py _carry_for_turn): its carry stays on the row.
    _carry = _frame.get("carry") if isinstance(_frame, dict) else None
    _nudge_tags = {k: v for k, v in (("connector_origin", _origin), ("carry", _carry)) if v}
    # Where the turn runs: a message for it after it has stopped reading
    # starts a turn of its own there (routes/circles.nudge). history_context
    # is part of where: with the lock keyed by the conversation, a message
    # from another surface can land on this turn, and its own turn must run
    # in the same conversation.
    _room = {"app_id": kwargs.get("app_id"), "project_id": kwargs.get("project_id"),
             "thread_id": kwargs.get("thread_id"),
             "history_context": kwargs.get("history_context")}
    # A message that lands in a running turn as a nudge (another tab's, a
    # connector's, an answer whose asking turn ended) is the server's to
    # deliver, so it is held in the Host's database as the nudge routes'
    # words are (pending_words): written before the turn
    # has it, gone once a turn has it, and sent on after a restart. A message
    # that is a turn's own (started, or the same words reattached) gives the
    # row back at once: its sender follows that turn, as before. Not in PII
    # mode (nothing is written there), and not for a caller that holds the
    # words itself (pending_keys) or whose client keeps a copy.
    # A resend of words the server may hold already: the Workbench send's one
    # silent retry after its stream died before anything showed, under the
    # same client_id as its first attempt (use-workbench-chat attemptTurn).
    # Held by a queue here or a turn the boot pass started -> this send
    # delivers nothing again and follows the thread's turn; left by a process
    # that has ended and not yet handed on -> claimed from the boot pass in
    # one step, and they go with this send's words, once (pending_words
    # .claim_resend).
    # A resend whose first attempt STARTED a turn (the words were that turn's
    # message, not a nudge): it follows that turn, running or finished, and
    # starts no second one (turn_registry.started_with).
    # And a send under an id whose words a turn already has, as its message or
    # as a nudge it READ: a page that sends its kept copy of words a second
    # time (PII mode: a reload before the answer to the first came, a
    # duplicated tab with the same sessionStorage, a receipt the replay no
    # longer holds past MAX_BUFFER_BYTES). It follows that turn and delivers
    # nothing new (turn_registry.delivered_in). Once the registry has let the
    # turn go (DONE_TTL_S, a restart), the user row the words became carries
    # the id (metadata.client_id, or its digest in PII mode; session.py), and
    # the send is answered from it (pending_words.delivered_row). Checked
    # again after the database read, with no await from there to
    # start_or_attach in PII mode, so two sends racing under one id start one
    # turn; outside PII mode the second, on the same words, is a reattach.
    if (follow_by_id and not pending_keys and not nudge_keeps_copy and entity_id is not None
            and isinstance(kwargs.get("client_id"), str)):
        _first = get_turn_registry().delivered_in(entity_id, kwargs["client_id"])
        if _first is None and get_pool() is not None:
            try:
                from mastercode import pending_words as _pw
                _row = await _pw.delivered_row(get_pool(), entity_id, kwargs["client_id"])
            except Exception as e:  # noqa: BLE001 — unknown: the send goes as before
                logger.warning("send: delivered words under this client_id not read (non-fatal): %s", e)
                _row = False
            _first = get_turn_registry().delivered_in(entity_id, kwargs["client_id"])
            if _first is None and _row:
                yield "2:" + json.dumps([{"type": "words_delivered",
                                          "client_id": kwargs["client_id"]}]) + "\n"
                return
        if _first is not None:
            async for frame in _never_silent(get_turn_registry().tail(_first, mode="reattached")):
                yield frame
            return
    claimed = None
    if (not pending_keys and not nudge_keeps_copy and entity_id is not None
            and isinstance(kwargs.get("client_id"), str) and get_pool() is not None):
        try:
            from mastercode import pending_words as _pw
            _state, _held_keys = await _pw.claim_resend(get_pool(), entity_id, kwargs["client_id"])
        except Exception as e:  # noqa: BLE001 — unknown: the send goes as before
            logger.warning("send: held words under this client_id not read (non-fatal): %s", e)
            _state, _held_keys = "none", []
        if _state == "held":
            async for frame in _follow_held(thread_key, kwargs["client_id"]):
                yield frame
            return
        if _state == "claimed":
            pending_keys = claimed = _held_keys
    own_keys = None
    if not pending_keys and not nudge_keeps_copy and get_pool() is not None:
        _live = get_turn_registry().would_nudge(thread_key, message,
                                                system=bool(kwargs.get("system_turn")))
        if _live is not None:
            try:
                from mastercode import pending_words as _pw
                from mastercode.runtime.session import nudge_words
                _k = await _pw.hold(
                    get_pool(), entity_id if entity_id is not None else _live.entity_id,
                    _live.session_id or f"turn|{_live.turn_id}", nudge_words(message),
                    client_id=kwargs.get("client_id"), tags=_nudge_tags or None,
                    room=_room, thread_key=thread_key)
                own_keys = [_k] if _k else None
            except Exception as e:  # noqa: BLE001 — the words still go in, held in memory
                logger.error("send: words landing in a running turn could not be held "
                             "(thread %s): %s", thread_key, e)
    try:
        rec, mode = get_turn_registry().start_or_attach(
            thread_key=thread_key,
            entity_id=entity_id,
            app_id=kwargs.get("app_id"),
            message=message,
            make_inner=lambda: _send_message_inner(*args, **kwargs),
            display_message=display_message,
            make_followup=_followup_maker(args, kwargs),
            # A platform-started turn is never made a nudge into a running
            # one (turn_registry.THREAD_BUSY): the caller waits it out.
            system=bool(kwargs.get("system_turn")),
            room=_room,
            nudge_tags=_nudge_tags or None,
            **({"nudge_keeps_copy": True} if nudge_keeps_copy else {}),
            # The sender's id for the words, when they land in a running turn
            # as a nudge: the nudge's receipt and its user row keep it
            # (session.push_nudge), as they do for the page's own nudges.
            **({"nudge_client_id": kwargs["client_id"]} if kwargs.get("client_id") else {}),
            **({"pending_key": pending_keys or own_keys} if (pending_keys or own_keys) else {}),
        )
    except (TurnCapError, TurnBusyError) as e:
        if own_keys:
            await _give_back(own_keys)
        if claimed:
            from mastercode import pending_words as _pw
            _pw.unclaim(claimed, kwargs.get("client_id"))
        yield f'3:{json.dumps(str(e))}\n'
        return
    if own_keys and mode != "nudged":
        # The words are this turn's own message: its sender follows it.
        rec.message_keys = [k for k in rec.message_keys if k not in own_keys]
        await _give_back(own_keys)
    async for frame in _never_silent(get_turn_registry().tail(rec, mode=mode)):
        yield frame


async def _follow_held(thread_key: str, client_id: str) -> AsyncIterator[str]:
    """The stream of a resend whose words the server already holds: the
    thread's running turn, which has them or reads them (a turn the boot pass
    is starting for them appears within moments). Nothing is delivered."""
    from server.turn_registry import get_turn_registry
    reg = get_turn_registry()
    rec = reg.active_for(thread_key)
    for _ in range(40):
        if rec is not None:
            break
        await asyncio.sleep(0.05)
        rec = reg.active_for(thread_key)
    if rec is None:
        yield "2:" + json.dumps([{"type": "words_held", "client_id": client_id}]) + "\n"
        return
    async for frame in _never_silent(reg.tail(rec, mode="reattached")):
        yield frame


async def _give_back(keys) -> None:
    """Rows send_message held for words that did not become a nudge: nothing
    holds them now. Never raises; a failure is logged with the count."""
    try:
        from mastercode import pending_words as _pw
        await _pw.settle(get_pool(), keys)
    except Exception as e:  # noqa: BLE001
        logger.error("PENDING_WORDS_NOT_SETTLED n=%d — a restart would deliver them: %s",
                     len(keys or []), e)


# What a follow-up turn keeps from the turn it follows: who and where (the
# thread, the conversation it thinks in, its chosen model, the app's
# permissions) and nothing of the first message — its attachments, frame,
# artifact, brief or origin belong to that message, and the nudged text came
# from someone else (another tab, or another surface in the same
# conversation: the lock is keyed by the conversation, compose_thread_key).
_FOLLOWUP_KEEPS = ("session_id", "entity_id", "app_id", "project_id", "thread_id",
                   "history_context", "thread_override", "app_permissions")


def _followup_maker(args, kwargs):
    """A builder for the NEXT turn on this thread, from the nudged text and the
    thread only (turn_registry._requeue_undelivered)."""
    import inspect
    try:
        bound = dict(inspect.signature(_send_message_inner).bind_partial(*args, **kwargs).arguments)
    except TypeError:
        bound = dict(kwargs)
    keep = {k: v for k, v in bound.items() if k in _FOLLOWUP_KEEPS}

    def make(text: str, frame: Optional[dict] = None, client_id=None):
        # `frame`: only what the nudged words themselves carried (a
        # connector's connector_origin, turn_registry._requeue_undelivered),
        # never the first message's frame. `client_id`: the nudged words'
        # own senders' ids (one, or a list for several joined), which the
        # turn's user row keeps (session.row_client_id) — never the first
        # message's.
        own = {"client_id": client_id} if client_id else {}
        if frame:
            return _send_message_inner(message=text, frame=frame, **own, **keep)
        return _send_message_inner(message=text, **own, **keep)
    return make


async def _send_message_inner(
    session_id: Optional[str] = None,
    message: str = "",
    attachments: Optional[list] = None,
    # No-sessions chat path: when entity_id is supplied
    # and session_id is omitted, the runtime builds a transient chat
    # context. The chat log is entity_memory turn rows tagged by
    # project_id + app_id; no entity_sessions row is created.
    entity_id: Optional[int] = None,
    # Per-app chat tagging. Forwarded to write_turn_row
    # so entity_memory.turn rows carry the calling app's context.
    app_id: Optional[str] = None,
    project_id: Optional[int] = None,
    # Circles threads: the circles_threads row this turn belongs to. Forwarded to the
    # runtime, which parks it on the session and derives
    # {kind: thread, ref: <id>} from it for every scope decision the turn
    # makes. None = the legacy `app:circles` scope, unchanged in every byte.
    # Turn types step 5: a scheduled task's type (runtime.send_message).
    turn_type_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    artifact_id: Optional[str] = None,
    # Calling app's declared scope list. Used by the
    # meta-tool gate downstream to narrow effective bits to
    # entity_bits ∩ app_bits. None = no narrowing (internal flows).
    app_permissions: Optional[list[str]] = None,
    # `accept` retained for call-site compat but
    # no longer consulted. There is only ONE wire format now: the AI
    # SDK Data Stream Protocol. The legacy `data: {"type":"token",...}`
    # branch was removed; all callers must parse f:/0:/9:/a:/d:/e:/3:
    # frames. Workbench and Circles clients, the smoke test and the A/B
    # benchmark all do.
    accept: Optional[str] = None,
    # Per-thread model override for THIS
    # turn. Shape {provider, model_id[, base_url, connection_id]}. Forwarded
    # verbatim to the runtime, which applies it to the `chat` role through the
    # leak-guard (a BYO/self pick on a prompt-bearing role is refused there
    # and resolution falls back to the persisted/default model). None = no
    # override (every existing caller is unchanged).
    thread_override: Optional[dict] = None,
    # A turn the PLATFORM starts (a dispatch round finished and the host is
    # woken to continue — runtime.continue_after_round): the message is the
    # wake-up brief, not something the user typed, so no user turn row is
    # written and the assistant row is tagged origin='continuation'.
    # Run the turn under ANOTHER surface's interaction_context while
    # `app_id` keeps its own write tag (session.py's transient builder
    # applies it). The offline task runner uses it so a fired timer
    # THINKS in the thread it was set in without writing rows into it.
    history_context: Optional[str] = None,
    system_turn: bool = False,
    # A request the person sent while the Host was starting, run now by the
    # offline runner (identity hold): still a system turn for its lane, but the
    # person's own words, never a woken turn (tools' phase-repair rule).
    saved_request: bool = False,
    # With system_turn: the user's ORIGINAL request the round started from,
    # so a dispatch made from the continuation carries that as its
    # origin_request instead of the (multi-report) brief.
    origin_request: Optional[str] = None,
    # The FRAME (host prompt map phase 4): what the front end sent about this
    # turn, already validated by the route (runtime.frame.validate_frame).
    frame: Optional[dict] = None,
    # A woken turn past its thread's chain of woken turns (session
    # WOKEN_TURNS_MAX): it reports and may ask, and starts no work.
    may_start_work: bool = True,
    # The sender's id for these words, kept on the person's row as a nudge's
    # is (session.push_nudge client_id, nudge_id_ok). The Workbench Index
    # sends its request id so a retry can tell the words were delivered
    # (routes/workbench_index.index_send_once). send_message hands it to the
    # registry too, for the case the words become a nudge into a running turn.
    # A list for a turn of several messages' words joined
    # (turn_registry._requeue_undelivered, through _followup_maker).
    client_id: Optional[str | list] = None,
    # A turn woken with a result from outside it (session._wake_host_turn):
    # the turn starts marked untrusted (runtime/turn_taint).
    untrusted_input: Optional[str] = None,
    # The brief is the Host's own request (root_turn.open_root): written as
    # a user row of the thread, marked requested_by='host'.
    host_request: bool = False,
) -> AsyncIterator[str]:
    """Send a message and yield AI SDK Data Stream Protocol frames.

    Frames (one per line, single LF terminator, no trailing blank line):
      f:{"messageId":"<uuid>"}            — start step
      9:{"toolCallId","toolName","args"}  — tool call dispatched
      a:{"toolCallId","result"}           — tool result
      0:"text content"                    — text delta
      2:[]                                — no-op data array (heartbeat)
      d:{"finishReason","usage"}          — finish step
      e:{"finishReason","usage","isContinued"} — finish message (terminal)
      3:"error message"                   — error (terminal)
    """
    if not _runtime:
        yield f'3:{json.dumps("Brain not initialized")}\n'
        return

    # Progress queue — the runtime's tool-loop pushes events here via
    # `_progress_cb`. The streaming loop drains it whenever the runtime
    # task is awaiting (so tool-call frames flush to the client in real
    # time, not after the final reply).
    progress_q: "asyncio.Queue[dict]" = asyncio.Queue()

    async def _cb(event: dict):
        await progress_q.put(event)

    def _frame_for_event(event: dict) -> str | None:
        """Map a runtime progress event to its Data Stream frame."""
        kind = event.get("event")
        if kind == "tool_call_start":
            return "9:" + json.dumps({
                "toolCallId": event["tool_call_id"],
                "toolName": event["tool"],
                "args": event.get("args") or {},
            }) + "\n"
        if kind == "tool_call_result":
            return "a:" + json.dumps({
                "toolCallId": event["tool_call_id"],
                "result": event.get("result"),
            }) + "\n"
        if kind == "session_started":
            # Circles upgrade: surface the transient chat
            # session id as a 2: data frame so the client can target
            # POST /api/circles/nudge at the live tool loop. Before
            # this the id never left the runtime and Nudge degraded
            # to a plain send.
            # turn_token identifies THIS turn where session_id
            # identifies the thread. turn_registry keys the live turn trace by
            # it, so a turn that died without clearing cannot have its tool
            # calls attributed to the next turn in the same session.
            return "2:" + json.dumps([{
                "type": "session",
                "session_id": event.get("session_id"),
                "turn_token": event.get("turn_token"),
            }]) + "\n"
        if kind == "turn_type":
            return turn_type_frame(event)
        if kind == "text_delta":
            # Live token from the orchestration model (Anthropic via the
            # inference.stream bridge op). Emitted as a 0: text-delta frame the
            # client appends. When any of these stream, the final buffered 0:
            # is suppressed below so the text isn't duplicated.
            txt = event.get("text") or ""
            return ("0:" + json.dumps(txt) + "\n") if txt else None
        if kind == "nudge_read":
            # A nudge the tool loop READ (session.py's drain). The nudge route
            # only says "queued"; this is the receipt. Its own type: the turn
            # registry's `nudge` frame is the bubble for another client's
            # message, and a second `nudge` would draw that twice. The ask
            # card nudges the person's answer into a running turn
            # and, if the turn ends without this frame, sends it as a message
            # instead. Clients that don't know the type ignore it.
            # It echoes the sender's id for the words (client_id), when the
            # sender gave one: the words as read may differ from those sent
            # (a secret shape cut), so the sender matches by the id.
            item = {"type": "nudge_read", "text": event.get("text") or ""}
            # One sender's id. Words of several messages read as one nudge
            # (turn_registry._requeue_undelivered) carry a list, which their
            # row keeps; the receipt names no id for them, as before.
            if isinstance(event.get("client_id"), str) and event["client_id"]:
                item["client_id"] = event["client_id"]
            return "2:" + json.dumps([item]) + "\n"
        if kind == "governance":
            # Governance floor (SB 243): a deterministic frame
            # marking a floor event (kind="crisis" — the platform's crisis
            # check flagged the person's words). It is the
            # ONLY thing the person is shown for it: the app draws the
            # platform's own dismissable notice with the crisis resources
            # (app-web-kit/ui/crisis-notice.tsx), and the reply is the
            # Host's, untouched. Clients that don't know the type ignore it
            # (same 2: contract as "session").
            return "2:" + json.dumps([{
                "type": "governance",
                "kind": event.get("kind"),
            }]) + "\n"
        if kind == "thread_moved":
            # Turn re-homing (runtime/turn_home.py): the turn's
            # work landed in a different project than the window it was typed
            # into, so the exchange was filed there and a marker left here.
            # Or the Host picked, in its turn, the thread the message belongs
            # to (runtime/thread_choice.py): from/to name both places as
            # {kind, ref}, beside the project fields the Workbench reads.
            # The client follows (switches to that project, reloads both
            # threads). Same 2: contract — older clients ignore the type.
            return "2:" + json.dumps([{
                "type": "thread_moved",
                "from_project_id": event.get("from_project_id"),
                "to_project_id": event.get("to_project_id"),
                "to_project_name": event.get("to_project_name"),
                "from": event.get("from"),
                "to": event.get("to"),
                "to_title": event.get("to_title"),
            }]) + "\n"
        if kind == "reasoning_delta":
            # Live thinking from the reasoning engine: while the three dots show
            # the model is reasoning, that reasoning feeds the chat. g: is the AI SDK
            # Data Stream
            # reasoning part; older clients ignore unknown prefixes,
            # so this is backward-safe on every shipped surface.
            txt = event.get("text") or ""
            return ("g:" + json.dumps(txt) + "\n") if txt else None
        return None

    runtime_task: Optional[asyncio.Task] = None
    try:
        # Start-step frame flushes response headers so any upstream
        # proxy doesn't treat the idle TCP socket as dead during model
        # latency before the first token / tool call.
        yield f'f:{json.dumps({"messageId": str(uuid.uuid4())})}\n'

        runtime_task = asyncio.create_task(_runtime.send_message(
            session_id=session_id,
            entity_id=entity_id,
            user_message=message,
            # Circles upgrade: attachments were declared on
            # this function from the start but never forwarded — the wire
            # to the runtime's multimodal path was severed here. Fixed.
            attachments=attachments,
            app_id=app_id,
            project_id=project_id,
            thread_id=thread_id,
            artifact_id=artifact_id,
            app_permissions=app_permissions,
            thread_override=thread_override,
            progress_cb=_cb,
            history_context=history_context,
            system_turn=system_turn,
            saved_request=saved_request,
            origin_request=origin_request,
            **({"client_id": client_id} if client_id else {}),
            **({"untrusted_input": untrusted_input} if untrusted_input else {}),
            **({"host_request": True} if host_request else {}),
            frame=frame,
            turn_type_id=turn_type_id,
            may_start_work=may_start_work,
        ))

        # While awaiting the runtime task, drain progress events as they
        # arrive (so tool-call frames flush mid-stream). Keepalive is NOT
        # handled here any more — the never-silent wrapper (send_message)
        # guards the WHOLE stream, including stretches this loop never
        # sees (post-result processing) and regardless of frameless events
        # (the old inline heartbeat reset its silence clock on events that
        # map to NO frame, so the wire could stay silent indefinitely).
        # Whether any text streamed live as 0: deltas. If so, the final buffered
        # 0: emit is skipped to avoid duplicating the message.
        streamed_any = False
        # Whether any tool frames reached the client (a tool-only turn is
        # visible-ish; a turn with NOTHING is a hard invariant violation).
        tools_emitted = False

        def _frames_for(event: dict) -> list[str]:
            """Frames for one runtime event, tracking the two stream facts
            the tail of this generator needs (text streamed? tools shown?)."""
            nonlocal streamed_any, tools_emitted
            kind = event.get("event")
            if kind == "narration":
                # Narrate-first dispatch (session.py): the host's statement of
                # what it just dispatched. When prose already streamed as 0:
                # deltas it is APPENDED (the stored turn is prose + narration,
                # so the screen ends up equal to the row); when nothing
                # streamed, the whole content goes out here. Either way the
                # final buffered emit below is skipped — this frame IS it.
                # Before this the narration was suppressed whenever prose had
                # streamed, so the bubble and the persisted row disagreed and
                # the Workbench's history merge kept a stale bubble below the
                # results that landed later.
                txt = event.get("text") or ""
                full = event.get("content") or txt
                # The separator the stored row joins with (session.REPLY_SEP),
                # so the screen, the row and the end-to-end test's text are one string.
                from mastercode.runtime.session import REPLY_SEP as _sep
                out = (_sep + txt) if streamed_any else full
                streamed_any = True
                return ["0:" + json.dumps(out) + "\n"] if out else []
            if kind == "text_delta":
                streamed_any = True
            frame = _frame_for_event(event)
            if not frame:
                return []
            if frame[0] in "9a":
                tools_emitted = True
            return [frame]

        while not runtime_task.done():
            getter = asyncio.create_task(progress_q.get())
            done, _ = await asyncio.wait(
                {runtime_task, getter},
                timeout=2.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if getter in done:
                for frame in _frames_for(getter.result()):
                    yield frame
            else:
                getter.cancel()
                try:
                    await getter
                except asyncio.CancelledError:
                    # The getter's own cancel, asked for just above, ends here
                    # on every quiet stretch. A cancel of THIS task (the
                    # person's Stop, the turn's time limit: the registry
                    # cancels the turn's task) that lands while it waits for
                    # the getter arrives as the same exception, because
                    # Task.cancel hands it to the task being awaited. Only
                    # this task's count of pending cancels tells the two
                    # apart. Dropping it let the turn run on after the
                    # person pressed Stop.
                    if asyncio.current_task().cancelling():
                        raise
                except Exception:
                    pass

        # Drain any remaining queued events after the task finishes.
        while not progress_q.empty():
            for frame in _frames_for(progress_q.get_nowait()):
                yield frame

        response = runtime_task.result()

        # A turn the identity hold refused (runtime/identity_hold.py): its text
        # goes out as the reply below, and this typed frame tells a caller that
        # is not a person reading it — a scheduled wake-up reading the stream —
        # that nothing ran, so it must not record the text as a result.
        if isinstance(response, dict) and response.get("identity_pending"):
            yield "2:" + json.dumps([{
                "type": "identity_pending",
                "identity": response.get("identity") or {},
            }]) + "\n"
        # Likewise a turn that could not run because no chat model is set up:
        # its text is the reply for a person; for a wake-up reading the stream
        # it is not a result, and the wake must stay held until a model exists.
        if isinstance(response, dict) and response.get("model_unconfigured"):
            yield "2:" + json.dumps([{"type": "model_unconfigured"}]) + "\n"

        # Extract content from whichever shape the runtime returned.
        if isinstance(response, str):
            content = response
        elif hasattr(response, '__aiter__'):
            parts = []
            async for chunk in response:
                if isinstance(chunk, str):
                    parts.append(chunk)
                elif isinstance(chunk, dict) and chunk.get("type") == "content_block_delta":
                    parts.append(chunk.get("delta", {}).get("text", ""))
            content = "".join(parts)
        elif isinstance(response, dict):
            content = response.get("content", "") or ""
        else:
            # None / unexpected shape — never stringify it into the reply
            # (str(None) once rendered a literal "None" bubble).
            content = ""

        # NO SILENT FAILURES: a contentless result must be
        # LOUD on the wire. `response` text (e.g. CHAT_UNCONFIGURED) is the
        # reply; `error` is a terminal 3: frame the client renders red.
        error_emitted = False
        if not content and isinstance(response, dict):
            content = str(response.get("response") or "")
            if not content and response.get("error"):
                yield "3:" + json.dumps(str(response["error"])) + "\n"
                error_emitted = True

        # Suppress the final buffered emit if the text already streamed live as
        # 0: deltas — otherwise the client would render the message twice. The
        # buffered path still fires for non-streaming providers / turns where no
        # delta was produced (streamed_any stays False).
        if content and not streamed_any:
            yield "0:" + json.dumps(content) + "\n"

        # THE INVARIANT: this stream never finishes with nothing visible.
        # Every turn ends in at least one of 0: text / 3: error / tool frames
        # + a note. Anything that would violate that is itself surfaced as an
        # error — the user always learns WHY there is no reply.
        if not (content or streamed_any or error_emitted):
            if tools_emitted:
                yield "0:" + json.dumps(
                    "(the turn ran tools but produced no reply — this is a "
                    "platform bug; the tool results above are real)") + "\n"
            else:
                yield "3:" + json.dumps(
                    "The turn produced no output — a platform error was "
                    "swallowed before reaching you. Check the host status "
                    "(Orchestrator) and report this.") + "\n"
        usage = {"promptTokens": 0, "completionTokens": 0}
        if isinstance(response, dict):
            # promptTokens = the lane-normalized TRUE context size (cached +
            # uncached). The raw wire prompt_tokens excludes the cached prefix
            # on the SDK lane — surfacing it alone is how the chat readout
            # claimed "4 in" on a 140K-token turn. cachedTokens/cacheWrite
            # carry the split so the UI can show what caching served.
            pt = response.get("input_total_tokens")
            if pt is None:
                pt = response.get("prompt_tokens")
            ct = response.get("completion_tokens")
            if pt is not None:
                usage["promptTokens"] = pt
            if ct is not None:
                usage["completionTokens"] = ct
            cr = response.get("cache_read_tokens")
            cw = response.get("cache_write_tokens")
            if cr is not None:
                usage["cachedTokens"] = cr
            if cw is not None:
                usage["cacheWriteTokens"] = cw
            # Conversation-depth: one typed-data frame per
            # assistant turn carrying the context accounting — the model
            # window, the budget in force, and the consolidator state. Same
            # 2: contract as "session": clients that don't know
            # "t":"context" ignore it.
            _ctx = response.get("context")
            if isinstance(_ctx, dict):
                yield "2:" + json.dumps([{
                    "t": "context",
                    "tokens_in": usage.get("promptTokens") or 0,
                    "window": _ctx.get("window"),
                    "budget": _ctx.get("budget"),
                    "summary_version": _ctx.get("summary_version"),
                    "consolidated_at": _ctx.get("consolidated_at"),
                    # Provenance: the served model/provider and
                    # any failover, so the client prints WHO ANSWERED under
                    # the live turn — same values the turn row persists.
                    "model": _ctx.get("model"),
                    "provider": _ctx.get("provider"),
                    "failed_over": _ctx.get("failed_over"),
                    # Price: the same estimate + basis word the
                    # turn row is stamped with, so the live meta line prints
                    # it without a reload.
                    "est_cost_usd": _ctx.get("est_cost_usd"),
                    "price_basis": _ctx.get("price_basis"),
                    # The context ledger (sizes per call/history/tools/results).
                    "ledger": _ctx.get("ledger"),
                    # The Host's close_turn signal (the Review button).
                    "close_turn": _ctx.get("close_turn"),
                }]) + "\n"
        yield "d:" + json.dumps({"finishReason": "stop", "usage": usage}) + "\n"
        yield "e:" + json.dumps({"finishReason": "stop", "usage": usage, "isContinued": False}) + "\n"

    except Exception as e:
        logger.exception("Error in brain message handling")
        # Error lane: a stream-layer crash is exactly the class
        # that used to vanish with the pod logs — land it in error_events.
        try:
            from server.error_log import record as _record_err
            await _record_err(_pool, source="stream", kind="crash",
                              message=str(e), session_id=session_id,
                              app_id=app_id, project_id=project_id)
        except Exception as _el_err:
            logger.debug("stream error-lane record failed (non-fatal): %s", _el_err)
        yield f'3:{json.dumps(str(e))}\n'
    finally:
        # Detached-turn teardown: this generator is only ever
        # closed by the turn registry (user cancel / turn timeout) or by its
        # own completion — a client disconnect kills the registry TAIL, never
        # the turn. Closing it must take the runtime task down with it, or a
        # cancelled turn would leave the engine (and the SDK's claude
        # subprocess) running invisibly — the exact orphan class seen live.
        if runtime_task is not None and not runtime_task.done():
            runtime_task.cancel()
            with contextlib.suppress(BaseException):
                await runtime_task


async def list_sessions(
    limit: int = 50,
    context: Optional[str] = None,
    status: Optional[str] = None,
    offset: int = 0,
) -> list[dict]:
    """List recent sessions.

    Optional filters:
      context: matches entity_sessions.interaction_context exactly. The
               workspace app passes "project:<id>" to find the chat tied to
               a specific project so a reopened project pulls its prior
               conversation off the server instead of relying on the
               browser's localStorage holding the session_id.
      status:  e.g. "active". Stale/closed sessions are excluded if set.
    """
    if not _pool:
        return []

    sql = """
        SELECT session_id, entity_id, status, message_count,
               channel, interaction_context, created_at, last_activity_at
        FROM entity_sessions
        WHERE entity_id = $1
    """
    params: list = [_entity_id]
    if context is not None:
        params.append(context)
        sql += f" AND interaction_context = ${len(params)}"
    if status is not None:
        params.append(status)
        sql += f" AND status = ${len(params)}"
    params.append(limit)
    # Order: prefer sessions that actually have a conversation, then by
    # recency. Without the message_count tiebreak, an empty "placeholder"
    # session (created when a project was opened but not chatted in) will
    # shadow the real ongoing conversation for that project, because the
    # placeholder was created later. The CASE form keeps NULLS LAST behavior
    # for last_activity_at.
    sql += (
        " ORDER BY "
        " (CASE WHEN message_count > 0 THEN 0 ELSE 1 END), "
        " last_activity_at DESC NULLS LAST "
        f" LIMIT ${len(params)}"
    )
    # A page past the first `limit`.
    params.append(max(0, int(offset or 0)))
    sql += f" OFFSET ${len(params)}"
    rows = await _pool.fetch(sql, *params)

    return [
        {
            "session_id": r["session_id"],
            "entity_id": r["entity_id"],
            "status": r["status"],
            "message_count": r["message_count"],
            "channel": r["channel"],
            "interaction_context": r["interaction_context"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "last_activity_at": r["last_activity_at"].isoformat() if r["last_activity_at"] else None,
        }
        for r in rows
    ]


async def search_memory(
    query: str,
    limit: int = 10,
    audience=None,
) -> list[dict]:
    """Search entity memory — THE recall engine underneath (P0/P1).

    This list-shaped contract backs the host-internal callers AND the external
    `ceigas_memory_search` MCP tool (bridge/mcp_proxy), so external clients
    upgraded to hybrid recall with zero surface change. Canonical filters
    (live + bitemporal + audience) come from mastercode.memory_retrieval —
    never re-derived here. The old ILIKE substring fallback is retired: with
    the embed lane down the engine's FTS arm answers (matched=['lexical'],
    relevance=null), which is a real search, not a substring scan.

    `audience` is a `server.applications.memory_scope.Audience`; omitted =
    handler (full corpus). Scoping now fails CLOSED: a supplied audience whose
    scope clause cannot build gets [] — the old lane logged and silently
    widened to the full corpus.
    """
    if not _pool:
        return []

    from mastercode.memory_retrieval import recall_memories, AudienceScopeError
    from mastercode.memory_engine import RecallUnavailable

    try:
        res = await recall_memories(
            _pool, _entity_id, query, limit=limit, audience=audience)
    except RecallUnavailable as e:
        logger.warning("search_memory unavailable (no arm ran): %s", e)
        return []
    except AudienceScopeError as e:
        logger.warning("search_memory audience scope failed closed: %s", e)
        return []

    return [
        {
            "id": h["id"],
            "content": h["content"],
            "memory_type": h["memory_type"],
            "importance": h["importance"],
            "relevance": h.get("relevance"),
            "matched": h.get("matched"),
            "created_at": h["created_at"].isoformat() if h["created_at"] else None,
        }
        for h in res["hits"]
    ]

