"""mastercode.thread_audit — the background memory work audits each thread,
deterministically.

The background memory call maintains the integrity of each thread's chain of
thought: no cross-thread responses or context.

DETERMINISTIC ONLY. An earlier cut had the batch model judge
the Host's replies (contradictions; system state stated with no live read) and
show those verdicts to the Host. That is an LLM scoring the entity's judgement,
which the platform never does (no LLM judges an LLM). Those two kinds are
gone; the column's CHECK keeps their names so rows written before this read as
what they were, and every one still open is closed on the next pass
(close_judged). State claimed with no read is removed at the source instead:
live context puts the current state in the prompt, read by code.

What remains is exact:

  cross_thread   another thread's own words (a turn or summary row) reached
                 this thread's prompt — read from prompt_log, which records every
                 pre-fired row with its id and scope from the turn's own session.

A finding NAMES the row (id and thread) and never quotes it, so reporting a leak
cannot itself carry another thread's words into this thread's prompt. It is
shown in that thread's memory block so the Host knows not to rely on what it
saw, and ages out after EXPIRE_DAYS; it reopens only when the leak happens
again (evidence newer than the close).

Every function is best-effort: an audit failure costs a finding, never a turn
or a consolidation pass.
"""
from __future__ import annotations

import hashlib
import json
import logging

log = logging.getLogger("cseries.mastercode.thread_audit")

KINDS = ("cross_thread",)
# Written by an earlier cut that had a model judge the Host; closed, never written.
JUDGED_KINDS = ("contradiction", "unsupported_state")
SAID_MAX = 300
NOTE_MAX = 200
# Offered back to the audit call per thread (by id) and shown in the thread's
# memory block. Small on purpose: a finding is a correction to make, not a log.
MAX_OPEN = 5
SHOWN_MAX = 3
# A finding the thread never corrects stops being shown after this.
EXPIRE_DAYS = 14
# How far back the deterministic cross-thread scan reads prompt_log. The pass
# runs every 30 minutes; the fingerprint makes a re-read a no-op.
CROSS_THREAD_LOOKBACK_HOURS = 6

OWN_WORDS_TYPES = ("turn", "summary")


def _clip(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _screened(s, n: int) -> str:
    """Text headed back into a system prompt. A finding quotes a reply or a
    recalled row, which conversation text can shape — the same untrusted-to-
    prompt sink skill_events screens for learned rules (C9). Strip-and-flag
    through the live input guard; the flags are not needed here."""
    text = _clip(s, n)
    try:
        from mastercode.runtime.input_guard import sanitize
        text, _flags = sanitize(text)
    except Exception as e:  # noqa: BLE001 — an unscreened line is dropped, not shown
        log.warning("thread audit: screen failed, finding text withheld: %s", e)
        return "(finding text withheld: screen unavailable)"
    return _clip(text, n)


def _fingerprint(scope: dict, kind: str, said: str) -> str:
    key = f"{scope.get('kind')}|{scope.get('ref')}|{kind}|{' '.join(said.lower().split())}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _scope_ok(scope) -> bool:
    return isinstance(scope, dict) and bool(scope.get("kind")) and scope.get("ref") is not None


# ── reads ────────────────────────────────────────────────────────────────

async def open_findings(pool, entity_id: int, scope: dict | None,
                        limit: int = MAX_OPEN) -> list[dict]:
    if pool is None or not _scope_ok(scope):
        return []
    try:
        rows = await pool.fetch(
            "SELECT id, kind, said, note, created_at FROM memory_audit_findings "
            "WHERE entity_id = $1 AND scope_kind = $2 AND scope_ref = $3 "
            "AND kind = 'cross_thread' AND closed_at IS NULL "
            # Past its age a finding is not shown even before a pass closes
            # it (expire_findings): the prompt never waits on a clock.
            "AND last_seen_at >= NOW() - make_interval(days => $5) "
            "ORDER BY last_seen_at DESC LIMIT $4",
            int(entity_id), str(scope["kind"]), str(scope["ref"]), int(limit), EXPIRE_DAYS)
        return [dict(r) for r in rows or []]
    except Exception as e:  # noqa: BLE001 — a missing table (pre-v187 pod) is no findings
        log.debug("thread audit: open findings read failed: %s", e)
        return []


def render_findings_block(findings: list[dict]) -> str:
    """The section of the thread's memory block. Empty when nothing is open."""
    if not findings:
        return ""
    label = {"cross_thread": "context from another thread reached this one"}
    lines = ["\n### Audit of this thread"]
    for f in findings[:SHOWN_MAX]:
        note = f" — {_screened(f.get('note'), NOTE_MAX)}" if f.get("note") else ""
        lines.append(f"- [{label.get(f['kind'], f['kind'])}] \"{_screened(f.get('said'), 200)}\"{note}")
    lines.append("Do not rely on what that row said in this thread; it belongs to another "
                 "conversation.")
    return "\n".join(lines)


# ── writes ───────────────────────────────────────────────────────────────

async def _upsert(pool, entity_id: int, scope: dict, kind: str, said: str,
                  note: str, evidence: dict, *, seen_at=None) -> bool:
    """One row per thing. True when this is a NEW finding — first seen, or
    seen again after it was closed (a leak that happens again after the old
    finding aged out is a new instance and reopens). A
    still-open finding seen again only moves last_seen_at.

    `seen_at` is when the evidence happened: the cross-thread scan re-reads the
    same prompt_log rows every pass, and re-reading the leak that was already
    closed is not the leak happening again. It reopens a
    closed finding only when the evidence is NEWER than the close."""
    said = _clip(said, SAID_MAX)
    if not said or kind not in KINDS:
        return False
    fp = _fingerprint(scope, kind, said)
    prior = await pool.fetchrow(
        "SELECT id, closed_at FROM memory_audit_findings "
        "WHERE entity_id = $1 AND fingerprint = $2", int(entity_id), fp)
    if prior is None:
        await pool.execute(
            "INSERT INTO memory_audit_findings "
            "(entity_id, scope_kind, scope_ref, kind, said, note, evidence, fingerprint) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8) "
            "ON CONFLICT (entity_id, fingerprint) DO UPDATE SET last_seen_at = NOW()",
            int(entity_id), str(scope["kind"]), str(scope["ref"]), kind, said,
            _clip(note, NOTE_MAX) or None, json.dumps(evidence or {}), fp)
        return True
    if prior["closed_at"] is not None:
        if seen_at is not None and seen_at <= prior["closed_at"]:
            return False
        await pool.execute(
            "UPDATE memory_audit_findings SET closed_at = NULL, close_reason = NULL, "
            "last_seen_at = NOW(), note = $2, evidence = $3::jsonb WHERE id = $1",
            prior["id"], _clip(note, NOTE_MAX) or None, json.dumps(evidence or {}))
        return True
    await pool.execute(
        "UPDATE memory_audit_findings SET last_seen_at = NOW() WHERE id = $1", prior["id"])
    return False


async def audit_cross_thread(pool, entity_id: int) -> int:
    """The deterministic check: every recent per-turn prompt whose pre-fire
    carried an own-words row (turn / summary) from a scope other than the
    turn's own RECALL scope. Reads the entry prompt_schema logs from the
    turn's own session (`item_meta`, `turn_scope`) — not prompt_log's
    source_context, which is the display label and differs on purpose for
    replay / A-B. A cross-session pattern about the
    person is entity-wide by design and never a finding.

    The finding NAMES the row (id and thread); it never quotes its text, so
    reporting a leak cannot itself carry another thread's words into this
    thread's prompt."""
    if pool is None:
        return 0
    n = 0
    try:
        rows = await pool.fetch(
            "SELECT log_id, created_at, blk FROM prompt_log, "
            "LATERAL jsonb_array_elements(blocks) AS blk "
            "WHERE entity_id = $1 AND scope = 'per_turn' "
            "AND created_at > NOW() - make_interval(hours => $2) "
            "AND blk->>'name' = 'memory_prefire'",
            int(entity_id), CROSS_THREAD_LOOKBACK_HOURS)
        for r in rows or []:
            blk = r["blk"]
            blk = json.loads(blk) if isinstance(blk, str) else blk
            if not isinstance(blk, dict):
                continue
            sc = blk.get("turn_scope")
            if not _scope_ok(sc):
                continue
            for it in blk.get("item_meta") or []:
                if (not isinstance(it, dict) or it.get("type") not in OWN_WORDS_TYPES
                        or it.get("pattern")):
                    continue
                isc = it.get("scope") or {}
                if (str(isc.get("kind")), str(isc.get("ref"))) == (str(sc["kind"]), str(sc["ref"])):
                    continue
                where = f"{isc.get('kind')}:{isc.get('ref')}" if isc else "no thread"
                if await _upsert(pool, entity_id, sc, "cross_thread",
                                 f"memory #{it.get('id')} ({it.get('type')}) from {where}",
                                 "another thread's own words were in this thread's prompt",
                                 {"prompt_log": r["log_id"], "memory_id": it.get("id"),
                                  "from": isc},
                                 seen_at=r["created_at"]):
                    n += 1
    except Exception as e:  # noqa: BLE001
        log.debug("thread audit: cross-thread scan failed (entity=%s): %s", entity_id, e)
    return n


async def close_judged(pool, entity_id: int) -> int:
    """Close every open finding of the retired model-judged kinds.
    'expired' is the only close reason that does not claim the
    Host corrected anything."""
    if pool is None:
        return 0
    try:
        res = await pool.execute(
            "UPDATE memory_audit_findings SET closed_at = NOW(), close_reason = 'expired' "
            "WHERE entity_id = $1 AND closed_at IS NULL AND kind = ANY($2::text[])",
            int(entity_id), list(JUDGED_KINDS))
        return int(str(res).split()[-1]) if res else 0
    except Exception as e:  # noqa: BLE001
        log.debug("thread audit: closing judged findings failed (entity=%s): %s", entity_id, e)
        return 0


async def expire_findings(pool, entity_id: int) -> int:
    if pool is None:
        return 0
    try:
        res = await pool.execute(
            "UPDATE memory_audit_findings SET closed_at = NOW(), close_reason = 'expired' "
            "WHERE entity_id = $1 AND closed_at IS NULL "
            "AND last_seen_at < NOW() - make_interval(days => $2)",
            int(entity_id), EXPIRE_DAYS)
        return int(str(res).split()[-1]) if res else 0
    except Exception as e:  # noqa: BLE001
        log.debug("thread audit: expiry failed (entity=%s): %s", entity_id, e)
        return 0


# ── the audit's own clock ────────────────────────────────────────────────

async def audit_pass(pool) -> dict:
    """The three steps the consolidator loop ran for each entity it touched
    (close_judged, audit_cross_thread, expire_findings), for every entity
    with a turn inside the lookback or a finding still open. Deterministic:
    no model call anywhere in it."""
    out = {"entities": 0, "found": 0, "expired": 0}
    if pool is None:
        return out
    try:
        rows = await pool.fetch(
            "SELECT entity_id FROM prompt_log WHERE scope = 'per_turn' "
            "AND created_at > NOW() - make_interval(hours => $1) "
            "UNION SELECT entity_id FROM memory_audit_findings WHERE closed_at IS NULL",
            CROSS_THREAD_LOOKBACK_HOURS)
    except Exception as e:  # noqa: BLE001 — a missing table (pre-v187 pod) is no pass
        log.debug("thread audit: entity scan failed: %s", e)
        return out
    for r in rows or []:
        eid = int(r["entity_id"])
        await close_judged(pool, eid)
        out["found"] += await audit_cross_thread(pool, eid)
        out["expired"] += await expire_findings(pool, eid)
        out["entities"] += 1
    return out


async def run_audit_loop(pool, interval_s: int) -> None:
    """With the consolidator off the audit keeps
    its own clock, at the loop's cadence: whether another thread's own words
    reached a prompt is still worth knowing, and a finding still has to age
    out. Never raises; a failed pass is a log line."""
    import asyncio
    await asyncio.sleep(min(60, interval_s))
    while True:
        try:
            res = await audit_pass(pool)
            if res["found"] or res["expired"]:
                log.info("thread audit: %s", res)
        except Exception as e:  # noqa: BLE001
            log.warning("thread audit pass failed: %s", e)
        await asyncio.sleep(interval_s)
