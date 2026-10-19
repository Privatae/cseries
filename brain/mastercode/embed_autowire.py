"""
Embedding auto-wire.

The host that first met a freshly connected bastion had everything it needed
— no embeddings configured, a box online advertising `embed`, one embedding
model served — and OFFERED instead of acting ("say the word"). When those
conditions are met, hitting that embedder is the default. The
user connected a machine running an embedder; that is the request.

This is a deterministic standing op: no model runs, the scope is the host's own embedding config, and it completes
with nobody present. It fires from the bastion heartbeat (the moment the
condition becomes true) rather than from a conversation, so the host never
has to remember to do it.

Exactly ONE transition is automatic — unconfigured → configured. A host that
already has a model is never re-pointed: changing model changes the vector
dim, and that is a decision, not plumbing. The wire itself goes through
set_embed_bastion, so the live probe (a real embed must come back at the
expected dim) stays the gate; an unpulled model is refused, never assumed.

Permission: the same CEIGAS caps bits the embedding_route tool needs
(META_TOOL_CAPS["embedding_route"]). A host that could not call use_bastion
itself does not get it done for free here.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("privatae.embed_autowire")

# After a failed attempt (probe refused, endpoint unreachable) leave the box
# alone for a while: the fix is on the user's side (pull the model, start
# ollama) and re-probing every 60s beat only fills the log. Ten minutes is the
# cadence the bastion itself already uses to retry a missing dependency
# (the gateway's faReconcileEvery), so the two lanes
# recover on the same clock.
RETRY_AFTER_S = 600

_last_failed: dict[str, float] = {}
_lock = asyncio.Lock()


def autowire_decision(configured: bool, bastion: dict | None, caps_ok: bool) -> tuple[str | None, str]:
    """The pure decision: (model to wire, reason). model None = do nothing.

    Kept free of I/O so the conditions are asserted by a test rather than
    inferred from a log line."""
    if configured:
        return None, "embeddings already configured"
    if not caps_ok:
        return None, "host lacks the embedding_route capability"
    if not isinstance(bastion, dict) or not bastion.get("online"):
        return None, "bastion offline"
    from mastercode.embed_backends import EMBED_CAPABILITY, choose_bastion_embed_model
    if EMBED_CAPABILITY not in (bastion.get("capabilities") or []):
        return None, "bastion does not advertise embed"
    pick = choose_bastion_embed_model(bastion.get("models") or [])
    if not pick:
        return None, "bastion serves no embedding model this host knows"
    return pick[0], f"unconfigured host, {bastion.get('hostname')} serves {pick[0]} ({pick[1]}d)"


async def _caps_ok(pool, entity_id: int) -> bool:
    try:
        from mastercode.runtime.meta_tool_caps import (
            META_TOOL_CAPS, _resolve_entity_scopes, scopes_to_bits,
        )
        need = int(META_TOOL_CAPS["embedding_route"]["required_bits"])
        have = scopes_to_bits(await _resolve_entity_scopes(pool, entity_id))
        return (have & need) == need
    except Exception as e:  # pragma: no cover - defensive
        log.info("embed autowire: caps check failed (%s); not wiring", e)
        return False


_tasks: set = set()


async def maybe_autowire(pool, entity_id: int, hostname: str | None) -> dict:
    """Called on every bastion heartbeat (best-effort; the caller swallows
    errors). Schedules the wire OFF the request path and returns at once: the
    probe inside apply_embedding_switch can wait up to its connect + read
    timeouts on a slow embedder, and the heartbeat reply carries the contain /
    update / rotate directives — none of which should queue behind it."""
    task = asyncio.create_task(_autowire_now(pool, entity_id, hostname))
    _tasks.add(task)

    def _done(t: "asyncio.Task") -> None:
        _tasks.discard(t)
        # A detached task's exception is otherwise only ever seen as asyncio's
        # "Task exception was never retrieved" at GC time. Retrieve it, log it,
        # and back off the hostname so the next beat does not raise again.
        try:
            exc = t.exception() if not t.cancelled() else None
        except Exception:  # pragma: no cover - defensive
            exc = None
        if exc is not None:
            _last_failed[(hostname or "").strip()] = time.monotonic()
            log.warning("embed autowire: %s raised: %r", hostname, exc)

    task.add_done_callback(_done)
    return {"scheduled": True}


async def _autowire_now(pool, entity_id: int, hostname: str | None) -> dict:
    """The wire itself. Returns {wired: bool, reason: str, ...} for logs and tests."""
    hostname = (hostname or "").strip()
    if pool is None or not entity_id or not hostname:
        return {"wired": False, "reason": "no pool/host id/hostname"}
    last = _last_failed.get(hostname)
    if last is not None and (time.monotonic() - last) < RETRY_AFTER_S:
        return {"wired": False, "reason": "recent failure, waiting"}
    async with _lock:
        try:
            from server.routes.embedding import current as _current
            cur = await _current()
        except Exception as e:
            return {"wired": False, "reason": f"embedding config unreadable: {e}"}
        if cur.get("configured"):
            return {"wired": False, "reason": "embeddings already configured"}
        try:
            from server.network import topology
            rows = await topology.list_bastions(pool, entity_id)
        except Exception as e:
            return {"wired": False, "reason": f"bastions unreadable: {e}"}
        bastion = next((b for b in rows if (b.get("hostname") or "") == hostname), None)
        model, reason = autowire_decision(False, bastion, await _caps_ok(pool, entity_id))
        if not model:
            return {"wired": False, "reason": reason}

        from mastercode.embed_backends import set_embed_bastion
        try:
            res = await set_embed_bastion(pool, entity_id, hostname, model=model)
        except Exception as e:
            # A probe path that RAISES (a DB error inside apply_embedding_switch)
            # rather than returning {"error"}: back off exactly like a refusal,
            # so the wire is neither retried every beat nor lost silently.
            _last_failed[hostname] = time.monotonic()
            log.warning("embed autowire: %s raised while wiring %s: %s", hostname, model, e)
            return {"wired": False, "reason": f"wire raised: {e}", "model": model}
        if not isinstance(res, dict) or res.get("error"):
            _last_failed[hostname] = time.monotonic()
            err = res.get("error") if isinstance(res, dict) else str(res)
            log.warning("embed autowire: %s refused %s: %s", hostname, model, err)
            return {"wired": False, "reason": f"probe refused: {err}", "model": model}
        _last_failed.pop(hostname, None)
        log.info("embed autowire: embeddings now on %s serving %s", hostname, model)

        jobs = []
        try:
            from server.routes.embed import enqueue_job
            for target in ("entity_memory", "user_context"):
                row = await enqueue_job(entity_id=entity_id, job_type="fill",
                                        target=target, metadata={}, model=model)
                if row:
                    jobs.append({"target": target, "id": row.get("id"), "total": row.get("total")})
        except Exception as e:  # pragma: no cover - defensive
            log.info("embed autowire: fill jobs not enqueued: %s", e)

        try:
            from server.bridge.memory_handlers import handle_inbox_append
            filled = ", ".join(f"{j['target'].replace('_', ' ')} ({j.get('total') or 0} rows)"
                               for j in jobs) or "nothing to fill yet"
            await handle_inbox_append({
                "subject": f"Embeddings now run on {hostname}",
                "body": (
                    f"Your machine **{hostname}** is serving embeddings for this host with "
                    f"`{model}`. Memory and document search now match on meaning, not just "
                    f"words, and the text never leaves your network.\n\n"
                    f"- **filling now** — {filled}\n"
                    f"- **change it** — Network → Bastions.\n"
                ),
                "body_format": "text/markdown",
                "message_kind": "system_notification",
                "payload": {"app": "network", "lens": "bastions", "kind": "embed_autowire",
                            "hostname": hostname, "model": model, "jobs": jobs},
            })
        except Exception as e:  # pragma: no cover - defensive
            log.info("embed autowire: inbox note skipped: %s", e)
        return {"wired": True, "reason": reason, "model": model, "jobs": jobs}
