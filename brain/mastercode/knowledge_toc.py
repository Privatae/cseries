"""The knowledge table of contents — one pushed block, one pulled payload.

The problem this answers: context is PUSHED. Every turn gets the rolling
summary, then verbatim turns to fill the budget, then pre-fired memories, and
the host has to think to reach for anything else. The nearest index it had was
`chat_threads op='list'` — a list of scopes, not one scope's contents.

Two halves, deliberately asymmetric:

  render_block(...)   the PUSHED block. Twelve lines at most, COUNTS and
                      never contents. It exists to make the host reach for
                      the pull; it is not itself the knowledge.
  build_toc(...)      the PULLED payload behind `chat_threads op='toc'`:
                      episodes, artifacts, the tool trail, related threads,
                      open/resolved goals, the live summary. Every item
                      carries its row id, so the existing `read` ops open
                      any of them and provenance is answerable by id.

THREE RULES THE CODE HERE ENFORCES:

  * **Weight ranks, it never recites**. thread_weight() is
    counts over rows that already exist — goals resolved, goals open,
    unresolved items, superseded typed rows, turns, summed est_cost_usd. It
    orders RELATED, and that is its ONE consumer: the artifact list is
    `ORDER BY id DESC` (scope_artifacts) and nothing else here or in the
    pre-fire lane reads a weight. No number it produces ever reaches the
    rendered text: a model narrates the numbers it is handed, and that is
    what reads as a robotic reminder that it knows everything.
  * **Frequency is not importance**. The rank key leads with
    OUTCOMES — resolved + open + unresolved + supersessions. Turn count and
    cost are tie-breaks underneath, never the lead, so a thread does not
    climb the list by being chatty.
  * **One scope truth**. Every turn-row read goes through
    memory_engine._turn_row_where (directly, or via count_scope_turns).
    A hand-written scope clause here is exactly how the consolidator's
    per-scope stats and the recall ladder drift apart.

Typed memory rows (goals, unresolved items) and artifacts are NOT turn rows,
so _turn_row_where does not apply to them — they are matched on the same
{kind, ref} descriptor they were stamped with when they were derived
(memory_engine.py:2460-2461 for entity_memory; thread_consolidator.py:499-505
for user_context).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

log = logging.getLogger("privatae.knowledge_toc")

# The pushed block's hard cap: twelve lines, anything more belongs in the
# pull.
BLOCK_MAX_LINES = 12

# How much of the pulled payload each lane returns. Bounded because the TOC is
# an index, not a dump — the `read` ops open any row it names.
EPISODES_LIMIT = 40
ARTIFACTS_LIMIT = 40
GOALS_LIMIT = 40
RELATED_LIMIT = 12
TRAIL_TURNS = 40

# Episodes: the dormancy gap that starts a new conversation, unchanged from
# chat_threads op='episodes' (runtime/tools.py).
EPISODE_GAP_MINUTES = 30

# Hosts whose URL paths are `<owner>/<repo>`. The persisted tool-call chip
# does NOT carry a repo field (see repos_from_trail), so a repo is only
# recoverable when a result handed back a URL on one of these. The Host's own
# forge is GITEA_URL's host when that is set; there is no default forge.
_REPO_HOSTS = ("github.com", "gitlab.com")


def _repo_re():
    hosts = list(_REPO_HOSTS)
    forge = (os.environ.get("GITEA_URL") or "").strip()
    forge = forge.split("://", 1)[-1].split("/", 1)[0]
    if forge and forge not in hosts:
        hosts.insert(0, forge)
    return re.compile(
        r"https?://(?:" + "|".join(re.escape(h) for h in hosts)
        + r")/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)")


# ── helpers ───────────────────────────────────────────────────────────


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _as_dict(md):
    """jsonb as a dict — asyncpg hands it back as str on some connections."""
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:  # noqa: BLE001
            md = {}
    return md if isinstance(md, dict) else {}


def _descriptor(thread_id):
    """The {kind, ref} descriptor for a Circles thread id, legacy included.

    circles_threads.descriptor_for routes through
    memory_engine.derive_source_context, so the scope this module reads is
    byte-identical to the one send_message wrote. Imported lazily: the two
    modules are landed by different agents in the same pass.
    """
    from mastercode.circles_threads import descriptor_for
    return descriptor_for(thread_id)


def _is_legacy(thread_id) -> bool:
    from mastercode.circles_threads import is_legacy
    return is_legacy(thread_id)


def _turn_scope(args: list, *, session_id: str, src_ctx: dict,
                live_only: bool = True) -> list[str]:
    """The turn-row WHERE clause for a thread scope — _turn_row_where and
    nothing else (two scope truths drift apart).

    `live_only=False` is the TRANSCRIPT reading (memory_engine.py:1099-1104):
    a superseded turn row is hidden from recall, not from what was said.
    """
    from mastercode.memory_engine import _turn_row_where
    return _turn_row_where(args, session_id=session_id, source_context=src_ctx,
                           live_only=live_only)


def _typed_scope(args: list, *, src_ctx: dict) -> list[str]:
    """The scope clause for NON-turn rows (typed memories, summaries).

    _turn_row_where cannot serve these — it pins memory_type = 'turn'. The
    descriptor match is the same shape it uses for a thread
    (memory_engine.py:1148-1151); the rows carry it because the derivation
    pass copies the turn's source_context onto everything it extracts
    (memory_engine.py:2460-2461, batch_consolidator.py:755).
    """
    args.append(str(src_ctx.get("kind") or ""))
    kind_i = len(args)
    args.append(str(src_ctx.get("ref") or ""))
    return [
        "entity_id = $1",
        f"metadata->'source_context'->>'kind' = ${kind_i}",
        f"metadata->'source_context'->>'ref' = ${len(args)}",
    ]


# ── episodes (factored out of chat_threads op='episodes') ─────────────


async def scope_episodes(
    pool,
    entity_id: int,
    src_ctx: dict,
    *,
    session_id: str = "",
    gap_minutes: int = EPISODE_GAP_MINUTES,
    limit: int = EPISODES_LIMIT,
) -> list[dict]:
    """One scope split into conversations by a dormancy gap, newest first.

    THE one implementation: `chat_threads op='episodes'` calls this, and so
    does build_toc, so the host's two ways of asking the same question cannot
    give two answers. The scope clause is _turn_row_where's, never
    hand-written here.

    Each episode carries `id`, the row id of its FIRST turn. An episode is a
    SQL grouping, not a row, and "every item carries its row id" has
    to mean something openable: first_turn_id / last_turn_id are real
    entity_memory ids, and started_at / ended_at are the since / until an
    `op='read'` takes.

    TRANSCRIPT-STYLE, so live_only=False. An episode is a window of the
    conversation and its count is what an `op='read'` of that window returns
    — and `read` has never applied recall's supersession filter (runtime/
    tools.py:6900-6902 builds its own descriptor-exact clause). With the
    filter on, an episode over superseded rows reported a turn_count the
    read of the same window disagreed with; older Hosts hold such rows in
    the original conversation (circles_threads.legacy_conversation_where).
    """
    try:
        gap = max(5, min(1440, int(gap_minutes)))
    except (TypeError, ValueError):
        gap = EPISODE_GAP_MINUTES
    args: list = [entity_id]
    where = _turn_scope(args, session_id=session_id, src_ctx=src_ctx,
                        live_only=False)
    args.append(gap)
    gap_i = len(args)
    args.append(max(1, int(limit)))
    sql = f"""
        WITH t AS (
            SELECT id, created_at, content, metadata, metadata->>'role' AS role,
                   CASE WHEN created_at - LAG(created_at) OVER (ORDER BY created_at)
                             > make_interval(mins => ${gap_i})
                        THEN 1 ELSE 0 END AS brk
              FROM entity_memory
             WHERE {' AND '.join(where)}
        ), seg AS (
            SELECT *, SUM(brk) OVER (ORDER BY created_at) AS ep FROM t
        )
        SELECT min(created_at) AS started_at, max(created_at) AS ended_at,
               count(*) AS turn_count,
               (array_agg(id ORDER BY created_at))[1] AS first_turn_id,
               (array_agg(id ORDER BY created_at DESC))[1] AS last_turn_id,
               COALESCE((array_agg(content ORDER BY created_at)
                             FILTER (WHERE role = 'user'))[1],
                        (array_agg(content ORDER BY created_at))[1]) AS preview,
               CASE WHEN bool_or(role = 'user') THEN NULL
                    ELSE (array_agg(metadata ORDER BY created_at))[1] END AS preview_md
          FROM seg GROUP BY ep ORDER BY max(created_at) DESC LIMIT ${len(args)}
    """
    try:
        rows = await pool.fetch(sql, *args)
    except Exception as exc:  # noqa: BLE001
        log.warning("episodes failed (entity=%s, scope=%s): %s",
                    entity_id, src_ctx, exc)
        return []
    from mastercode.memory_engine import host_part

    def _episode(r) -> dict:
        preview = r["preview"] or ""
        ep = {
            "id": r["first_turn_id"],
            "first_turn_id": r["first_turn_id"],
            "last_turn_id": r["last_turn_id"],
            "started_at": _iso(r["started_at"]),
            "ended_at": _iso(r["ended_at"]),
            "turn_count": int(r["turn_count"] or 0),
            "preview": preview[:200],
        }
        # An episode with no message from the person previews a reply. What
        # the person saw stays the preview; a platform notice on that reply
        # is named apart, with the Host's own words, as `chat_threads
        # op='read'` does (memory_engine.host_part).
        md = r.get("preview_md") if hasattr(r, "get") else None
        if md is not None:
            host, notes = host_part(preview, _as_dict(md))
            if notes:
                ep["platform_notice"] = notes
                ep["host_preview"] = host[:200]
        return ep

    return [_episode(r) for r in rows]


# ── artifacts ─────────────────────────────────────────────────────────


async def scope_artifacts(pool, entity_id: int, src_ctx: dict,
                          *, limit: int = ARTIFACTS_LIMIT) -> list[dict]:
    """The thread's captured tool results — user_context rows stashed by
    stash_tool_artifacts (thread_consolidator.py:446), tagged with this
    scope's descriptor (:499-505) under source 'tool:<name>'.

    Dropped rows are excluded: the janitor sets status='dismissed' on a drop
    (thread_consolidator.py:_curate_artifacts), which is the predicate every
    user_context recall lane already filters on. `kept` is the janitor's
    verdict, metadata.artifact_verdict == 'keep' (written alongside
    artifact_processed in the same update).
    """
    args: list = [entity_id]
    where = _typed_scope(args, src_ctx=src_ctx)
    where.append("source LIKE 'tool:%'")
    where.append("COALESCE(metadata->>'artifact', 'false') = 'true'")
    where.append("status <> 'dismissed'")
    args.append(max(1, int(limit)))
    sql = f"""
        SELECT id, title, source, status, created_at,
               metadata->>'artifact_verdict' AS verdict
          FROM user_context
         WHERE {' AND '.join(where)}
         ORDER BY id DESC
         LIMIT ${len(args)}
    """
    try:
        rows = await pool.fetch(sql, *args)
    except Exception as exc:  # noqa: BLE001
        log.warning("toc artifacts failed (entity=%s): %s", entity_id, exc)
        return []
    return [{
        "id": r["id"],
        "title": r["title"],
        "source": r["source"],
        "status": r["status"],
        "kept": (r["verdict"] == "keep"),
        "created_at": _iso(r["created_at"]),
    } for r in rows]


# ── the tool trail ────────────────────────────────────────────────────


def _digest_values(call: dict, keys: tuple) -> list:
    """Values for `keys` from one persisted chip, chip-level first then its
    digest. tool_call_chip (session.py:180-230) puts task_id / session_id at
    the top level and everything digest_tool_action collected under
    `digest` (tool_trail.py:73-133)."""
    out = []
    d = call.get("digest")
    d = d if isinstance(d, dict) else {}
    for k in keys:
        for src in (call, d):
            v = src.get(k)
            if v not in (None, "", [], {}):
                out.append(v)
    return out


def trail_index(turns: list[dict]) -> dict:
    """Tasks, repos and urls across a thread's assistant turns.

    `turns` is [{created_at, tool_calls}] — memory_engine.recall_tool_trail's
    shape, the bounded per-call chips session.py persists on the assistant
    turn row (metadata.tool_calls).

    REPOS. A newer chip carries `repo` in its digest when the result named
    one (tool_trail._DIGEST_KEYS); older chips only kept
    ids and URLs, so for those the repo is recovered from an owner/name path
    in a result URL. A chip with neither contributes no repo, and the TOC
    lists fewer rather than guessing.
    """
    tasks: dict = {}
    urls: list[str] = []
    repos: list[str] = []
    for t in (turns or []):
        at = _iso((t or {}).get("created_at"))
        for c in ((t or {}).get("tool_calls") or []):
            if not isinstance(c, dict):
                continue
            for tid in _digest_values(c, ("task_id",)):
                key = str(tid)
                if key not in tasks:
                    tasks[key] = {"id": tid, "tool": c.get("tool"),
                                  "op": c.get("op") or c.get("operate_tool"),
                                  "status": c.get("status") or "ok", "at": at}
            for r in _digest_values(c, ("repo",)):
                r = str(r).strip()
                if r and r not in repos:
                    repos.append(r)
            for u in _digest_values(c, ("url", "preview_url", "files_preview_url")):
                u = str(u)
                if u not in urls:
                    urls.append(u)
                m = _repo_re().match(u)
                if m and m.group(1) not in repos:
                    repos.append(m.group(1))
    return {"tasks": list(tasks.values()), "repos": repos, "urls": urls}


async def scope_tool_trail(pool, entity_id: int, src_ctx: dict,
                           *, session_id: str, limit: int = TRAIL_TURNS) -> dict:
    """The thread's tool trail, read through memory_engine.recall_tool_trail
    (memory_engine.py:1305-1358) so the scope clause is _turn_row_where's and
    the chip parsing is the one the per-turn trail block already uses."""
    try:
        from mastercode.memory_engine import recall_tool_trail
        turns = await recall_tool_trail(
            pool, entity_id, session_id, source_context=src_ctx, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log.warning("toc tool trail failed (entity=%s): %s", entity_id, exc)
        return {"tasks": [], "repos": [], "urls": []}
    return trail_index(turns)


# ── goals ─────────────────────────────────────────────────────────────


async def scope_goals(pool, entity_id: int, src_ctx: dict,
                      *, limit: int = GOALS_LIMIT) -> dict:
    """Open and resolved goals / unresolved items derived from this thread.

    THE EXIT CONDITION IS `resolved_at`, and it has exactly one writer:
    batch_consolidator._mark_resolved (:401-416) sets resolved_at = NOW() for
    the ids the pass reported under `resolves`, bounded to the ids it offered
    and to memory_type IN ('goal','unresolved') — the two types init-db.sql
    declares that exit for. `_open_goals` (:374-398) is the mirror predicate:
    status active, resolved_at IS NULL, not superseded. This reads both sides
    of the same predicate, scoped to the thread.

    `unresolved` is a memory_type, not a flag (the type list is in the
    extraction prompt, batch_consolidator.py:301), so an open unresolved item
    is a row of that type with resolved_at IS NULL.
    """
    out = {"open": [], "resolved": []}
    args: list = [entity_id]
    where = _typed_scope(args, src_ctx=src_ctx)
    where.append("memory_type = 'goal'")
    where.append("status = 'active'")
    where.append("(t_valid_until IS NULL OR t_valid_until > NOW())")
    args.append(max(1, int(limit)) * 2)
    sql = f"""
        SELECT id, content, memory_type, created_at, resolved_at
          FROM entity_memory
         WHERE {' AND '.join(where)}
         ORDER BY created_at DESC
         LIMIT ${len(args)}
    """
    try:
        rows = await pool.fetch(sql, *args)
    except Exception as exc:  # noqa: BLE001
        log.warning("toc goals failed (entity=%s): %s", entity_id, exc)
        return out
    for r in rows:
        item = {"id": r["id"], "content": r["content"],
                "memory_type": r["memory_type"],
                "created_at": _iso(r["created_at"]),
                "resolved_at": _iso(r["resolved_at"])}
        bucket = "resolved" if r["resolved_at"] is not None else "open"
        if len(out[bucket]) < limit:
            out[bucket].append(item)
    return out


# ── weight: counts, for ORDER only, never rendered ─────────────

# Module-level predicate strings shared between thread_weight (single
# candidate) and _batch_thread_weight (N candidates in one query) so the two
# forms cannot drift. Each predicate is copied verbatim from the SQL
# thread_weight built inline before this refactor.
_GOALS_RESOLVED_PRED = "memory_type = 'goal' AND resolved_at IS NOT NULL"
_GOALS_OPEN_PRED = (
    "memory_type = 'goal' AND resolved_at IS NULL AND status = 'active' "
    "AND (t_valid_until IS NULL OR t_valid_until > NOW())")
_UNRESOLVED_PRED = (
    "memory_type = 'unresolved' AND resolved_at IS NULL AND status = 'active' "
    "AND (t_valid_until IS NULL OR t_valid_until > NOW())")
# Typed rows RETIRED by a later row. Named for what it counts and not for
# why: MemoryConsolidator._supersede (memory_consolidation.py:848-865) writes
# this pair for a correction AND for a dedup merge, and the predicate cannot
# tell them apart. It is an ordering input, so "this thread settled things"
# is all it has to mean; calling it corrections claimed a judgement the rows
# do not carry.
_SUPERSEDED_PRED = (
    "memory_type NOT IN ('summary', 'turn') AND resolution_pointer IS NOT NULL "
    "AND t_valid_until IS NOT NULL")
# The turn predicate reproduces _turn_row_where's `thread` branch
# (memory_engine.py:1153-1164) minus the exact-ref match, which the caller
# applies separately (a per-thread WHERE for the single form, an ANY($)
# grouped WHERE for the batch form).
_TURN_PRED = (
    "memory_type = 'turn' AND status = 'active' "
    "AND (t_valid_until IS NULL OR t_valid_until > NOW()) AND project_id IS NULL")
_COST_PRED = (
    _TURN_PRED + " AND metadata->>'role' = 'assistant' "
    "AND jsonb_typeof(metadata->'est_cost_usd') = 'number'")

_ZERO_WEIGHT = {"goals_resolved": 0, "goals_open": 0, "unresolved": 0,
               "superseded": 0, "turns": 0, "cost_usd": 0.0}


async def thread_weight(pool, entity_id: int, thread_id, *,
                        count_turns: bool = True) -> dict:
    """Outcome counts for one thread. Ordering input only.

    Every field is a count or a sum over rows that already exist:

      goals_resolved   entity_memory memory_type='goal', resolved_at set
                       (batch_consolidator._mark_resolved:401-416)
      goals_open       same type, resolved_at IS NULL, live
      unresolved       memory_type='unresolved', resolved_at IS NULL, live
      superseded       typed rows RETIRED by a later row: t_valid_until set
                       AND resolution_pointer set, which is what
                       MemoryConsolidator._supersede writes
                       (memory_consolidation.py:848-865). That writer runs
                       for a correction (the extraction reply's
                       `corrects_earlier`, batch_consolidator.py:818) and for
                       a dedup merge alike, and the two columns cannot tell
                       them apart — so this counts supersessions, which is
                       all an ordering input needs to mean. Summaries use the
                       same two columns for their own rolling supersede
                       (thread_consolidator._store_summary:305-312) and are
                       excluded: counting them would score a thread by how
                       often its summary was rewritten.
      turns            memory_engine.count_scope_turns (→ _turn_row_where).
                       `count_turns=False` skips that full-scope count and
                       leaves the field None — for a caller that does not
                       RANK (render_block renders the thread row's own
                       transcript count and never calls rank_key), where the
                       count was a third scan of the same scope per turn.
                       rank_key coerces None to 0, so a weight taken this way
                       must not be ranked against one that was counted.
      cost_usd         summed metadata.est_cost_usd on this thread's assistant
                       turn rows. session.py:5880 stamps it from
                       server.routes.inference.estimate_turn_cost; circles.py:
                       385 is the UI reading the same key.

    Never raises: an unavailable count is 0, and a zero weight orders last
    rather than failing the caller.
    """
    src = _descriptor(thread_id)
    out = dict(_ZERO_WEIGHT)

    args: list = [entity_id]
    where = _typed_scope(args, src_ctx=src)
    try:
        row = await pool.fetchrow(
            f"""
            SELECT
              count(*) FILTER (WHERE {_GOALS_RESOLVED_PRED}) AS goals_resolved,
              count(*) FILTER (WHERE {_GOALS_OPEN_PRED}) AS goals_open,
              count(*) FILTER (WHERE {_UNRESOLVED_PRED}) AS unresolved,
              count(*) FILTER (WHERE {_SUPERSEDED_PRED}) AS superseded
              FROM entity_memory WHERE {' AND '.join(where)}
            """, *args)
        if row is not None:
            for k in ("goals_resolved", "goals_open", "unresolved",
                      "superseded"):
                out[k] = int(row[k] or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("thread weight (typed rows) failed (entity=%s): %s",
                    entity_id, exc)

    if count_turns:
        try:
            from mastercode.memory_engine import count_scope_turns
            stats = await count_scope_turns(pool, entity_id, "",
                                            source_context=src)
            out["turns"] = int(stats.get("count") or 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("thread weight (turns) failed (entity=%s): %s",
                        entity_id, exc)
    else:
        out["turns"] = None

    cargs: list = [entity_id]
    cwhere = _turn_scope(cargs, session_id="", src_ctx=src)
    cwhere.append("metadata->>'role' = 'assistant'")
    cwhere.append("jsonb_typeof(metadata->'est_cost_usd') = 'number'")
    try:
        val = await pool.fetchval(
            "SELECT COALESCE(sum((metadata->>'est_cost_usd')::numeric), 0) "
            f"FROM entity_memory WHERE {' AND '.join(cwhere)}", *cargs)
        out["cost_usd"] = float(val or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("thread weight (cost) failed (entity=%s): %s", entity_id, exc)
    return out


async def _batch_thread_weight(pool, entity_id: int,
                               thread_ids: list) -> dict:
    """thread_weight for N thread-kind candidates in ONE query, sharing this
    module's predicate constants with the single-thread form so they cannot
    drift (N4b). Every candidate here is a real Circles thread (`kind` is
    always 'thread' for a related-threads candidate — the legacy scope is
    excluded from `reasons` before this is called), so one grouped WHERE on
    `metadata->'source_context'->>'ref'` covers all of them exactly the way
    `_typed_scope` / `_turn_scope` cover one.

    BEFORE: N candidates cost thread_weight's 3 queries each = 3N queries
    (up to RELATED_LIMIT*3 = 36 candidates -> up to 108 queries).
    AFTER: 1 query for all N.
    """
    ids = [str(t) for t in dict.fromkeys(thread_ids) if t]
    if not ids:
        return {}
    sql = f"""
        SELECT metadata->'source_context'->>'ref' AS ref,
               count(*) FILTER (WHERE {_GOALS_RESOLVED_PRED}) AS goals_resolved,
               count(*) FILTER (WHERE {_GOALS_OPEN_PRED}) AS goals_open,
               count(*) FILTER (WHERE {_UNRESOLVED_PRED}) AS unresolved,
               count(*) FILTER (WHERE {_SUPERSEDED_PRED}) AS superseded,
               count(*) FILTER (WHERE {_TURN_PRED}) AS turns,
               COALESCE(sum((metadata->>'est_cost_usd')::numeric)
                        FILTER (WHERE {_COST_PRED}), 0) AS cost_usd
          FROM entity_memory
         WHERE entity_id = $1
           AND project_id IS NULL
           AND metadata->'source_context'->>'kind' = 'thread'
           AND metadata->'source_context'->>'ref' = ANY($2::text[])
         GROUP BY metadata->'source_context'->>'ref'
    """
    out: dict = {i: dict(_ZERO_WEIGHT) for i in ids}
    try:
        rows = await pool.fetch(sql, entity_id, ids)
    except Exception as exc:  # noqa: BLE001
        log.warning("batch thread weight failed (entity=%s): %s", entity_id, exc)
        return out
    for r in rows:
        ref = r["ref"]
        if ref not in out:
            continue
        out[ref] = {
            "goals_resolved": int(r["goals_resolved"] or 0),
            "goals_open": int(r["goals_open"] or 0),
            "unresolved": int(r["unresolved"] or 0),
            "superseded": int(r["superseded"] or 0),
            "turns": int(r["turns"] or 0),
            "cost_usd": float(r["cost_usd"] or 0),
        }
    return out


async def _batch_thread_rows(pool, entity_id: int, thread_ids: list) -> dict:
    """id/title/topic for N circles_threads candidates in ONE query
    (N4b). Keyed by str(id); a candidate row that no longer exists is simply
    absent, matching what a per-candidate ct.get_thread(...) is None would do."""
    ids = [str(t) for t in dict.fromkeys(thread_ids) if t]
    if not ids:
        return {}
    try:
        rows = await pool.fetch(
            "SELECT id, title, topic FROM circles_threads "
            " WHERE entity_id = $1 AND id = ANY($2::uuid[]) AND deleted_at IS NULL",
            entity_id, ids)
    except Exception as exc:  # noqa: BLE001
        log.warning("batch thread rows failed (entity=%s): %s", entity_id, exc)
        return {}
    return {str(r["id"]): {"id": str(r["id"]), "title": r["title"],
                           "topic": r["topic"]} for r in rows}


def rank_key(weight: dict) -> tuple:
    """The sort key weight feeds, highest first.

    OUTCOMES LEAD ("frequency is not importance"): resolved goals, open
    goals, unresolved items and supersessions are what a thread produced. Turn
    count and cost sit underneath as tie-breaks only, so a long chatty thread
    cannot outrank a short one that settled something.

    An uncounted turn field (thread_weight(count_turns=False)) reads as 0
    here — the tie-break degrades, the lead does not.
    """
    w = weight or {}
    outcomes = (int(w.get("goals_resolved") or 0)
                + int(w.get("goals_open") or 0)
                + int(w.get("unresolved") or 0)
                + int(w.get("superseded") or 0))
    return (outcomes, float(w.get("cost_usd") or 0.0), int(w.get("turns") or 0))


# ── related threads ────────────────────────────────────────────


async def _segment_topics(pool, entity_id: int, src_ctx: dict) -> list[str]:
    """Every topic this thread has been about — the segment_topic on each
    summary row in its chain (thread_consolidator._store_summary:278-279
    writes it; the chain survives as superseded rows, :305-312). The CURRENT
    topic is the live summary's; the rest are what co-occurred with it here.
    """
    args: list = [entity_id]
    where = _typed_scope(args, src_ctx=src_ctx)
    where.append("memory_type = 'summary'")
    where.append("metadata->>'source_kind' = 'thread_consolidator'")
    where.append("metadata->>'segment_topic' IS NOT NULL")
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT metadata->>'segment_topic' AS topic "
            f"FROM entity_memory WHERE {' AND '.join(where)}", *args)
    except Exception as exc:  # noqa: BLE001
        log.warning("toc segment topics failed (entity=%s): %s", entity_id, exc)
        return []
    return [r["topic"] for r in rows if r["topic"]]


async def related_threads(pool, entity_id: int, thread_id, *,
                          own_row: dict | None = None) -> list[dict]:
    """Threads related to this one, ordered by weight. The weight is
    NOT in the output — it decides the order and is then gone.

    Three sources, all counted events or exact lookups, never the graph
    (a topic node would be high-degree by design and memory_graph's hub
    rule would drop it):

      links   circles_thread_links in EITHER direction — a read of another
              thread on the user's behalf, or a Split. reason is the
              row's own: 'recall' or 'split'.
      topic   another thread whose circles_threads.topic is this thread's
              topic. Exact match, a lookup and not a score.
      topic   (co-occurrence) another thread whose topic is any topic THIS
              thread has been about across its summary chain.

    The legacy scope has no row and no links (circles_threads.write_link is a
    no-op for it), so its relations come from the topic lookup alone — via
    the live summary's segment_topic, which the legacy scope does have.

    `own_row` is this thread's own list-shape row when the caller already
    read it (render_block does, one line above): ct.get_thread costs the row
    read AND a full-scope turn count (circles_threads._turn_count), and only
    `topic` is wanted here.
    """
    from mastercode import circles_threads as ct

    reasons: dict = {}

    def _note(tid, reason):
        tid = str(tid)
        if ct.is_legacy(tid):
            return
        if reason == "split" or tid not in reasons:
            reasons[tid] = reason

    if not ct.is_legacy(thread_id):
        try:
            rows = await pool.fetch(
                """
                SELECT to_thread AS other, reason FROM circles_thread_links
                 WHERE entity_id = $1 AND from_thread = $2::uuid
                UNION ALL
                SELECT from_thread AS other, reason FROM circles_thread_links
                 WHERE entity_id = $1 AND to_thread = $2::uuid
                """, entity_id, str(thread_id))
            for r in rows:
                _note(r["other"], r["reason"])
        except Exception as exc:  # noqa: BLE001
            log.warning("toc links failed (entity=%s, thread=%s): %s",
                        entity_id, thread_id, exc)

    topics = await _segment_topics(pool, entity_id, _descriptor(thread_id))
    own = own_row
    if own is None:
        try:
            own = await ct.get_thread(pool, entity_id, thread_id)
        except Exception:  # noqa: BLE001
            own = None
    if own and own.get("topic"):
        topics.append(own["topic"])
    topics = [t for t in dict.fromkeys(topics) if t]
    if topics:
        try:
            # Bounded before weighting: the batched weight query below is
            # one round-trip however many candidates, but its GROUP BY still
            # scans every candidate's rows, so a topic shared by a long tail
            # of threads is cut to the newest few times the output size.
            rows = await pool.fetch(
                "SELECT id FROM circles_threads "
                " WHERE entity_id = $1 AND topic = ANY($2::text[]) AND deleted_at IS NULL"
                " ORDER BY created_at DESC LIMIT $3",
                entity_id, topics, RELATED_LIMIT * 3)
            for r in rows:
                if str(r["id"]) != str(ct.canonical_id(thread_id) or thread_id):
                    _note(r["id"], "topic")
        except Exception as exc:  # noqa: BLE001
            log.warning("toc topic lookup failed (entity=%s): %s", entity_id, exc)

    # Batched (N4b): every id in `reasons` is a real thread (legacy is
    # excluded by `_note` above), so id/title/topic and weight are each ONE
    # query for all candidates rather than one `ct.get_thread` +
    # `thread_weight` (= 4 queries) PER candidate.
    ids = list(reasons.keys())
    rows_by_id = await _batch_thread_rows(pool, entity_id, ids)
    weight_by_id = await _batch_thread_weight(pool, entity_id, ids)

    out: list[tuple[tuple, dict]] = []
    for tid, reason in reasons.items():
        row = rows_by_id.get(tid)
        if not row:
            continue
        out.append((rank_key(weight_by_id.get(tid, _ZERO_WEIGHT)),
                    {"id": row["id"], "title": row["title"],
                     "topic": row["topic"], "reason": reason}))
    out.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _key, item in out[:RELATED_LIMIT]]


# ── the pulled payload ─────────────────────────────────────────


async def build_toc(pool, entity_id: int, thread_id: str | None,
                    *, session_id: str) -> dict:
    """`chat_threads op='toc'` — one thread's contents as an index.

    thread_id None, "original" or the client's historic "self" all mean the
    un-migrated app:circles scope (circles_threads.is_legacy), which is a
    real scope with real rows and is served here exactly like a thread.

    Every item carries its row id: an episode its first/last turn id, an
    artifact its user_context id, a goal its entity_memory id, the summary
    its row id, a related thread its circles_threads id. That is what makes
    provenance answerable on demand rather than recited by default —
    the host reads by id when asked where something came from.
    """
    from mastercode import circles_threads as ct
    from mastercode.thread_consolidator import get_live_summary

    src = _descriptor(thread_id)
    thread = await ct.get_thread(pool, entity_id, thread_id)

    summary = None
    try:
        # get_live_summary(pool, entity_id, src_ctx) -> dict | None
        # (thread_consolidator.py:199) — {id, content, version, segment_topic,
        # created_at, created_iso}. 'Live' = active and not superseded.
        row = await get_live_summary(pool, entity_id, src)
        if row:
            summary = {"id": row.get("id"), "content": row.get("content") or "",
                       "version": row.get("version"),
                       "topic": row.get("segment_topic"),
                       "created_at": row.get("created_iso")}
    except Exception as exc:  # noqa: BLE001
        log.warning("toc summary failed (entity=%s): %s", entity_id, exc)

    return {
        "thread": thread,
        "episodes": await scope_episodes(pool, entity_id, src,
                                         session_id=session_id),
        "artifacts": await scope_artifacts(pool, entity_id, src),
        "tool_trail": await scope_tool_trail(pool, entity_id, src,
                                             session_id=session_id),
        "related": await related_threads(pool, entity_id, thread_id),
        "goals": await scope_goals(pool, entity_id, src),
        "summary": summary,
    }


# ── the pushed block ───────────────────────────────────────────


def _age_phrase(iso_or_dt, *, now: datetime | None = None) -> str | None:
    """"2h old" / "3d old" — an age, never a wall clock."""
    if not iso_or_dt:
        return None
    dt = iso_or_dt
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = max(0.0, ((now or datetime.now(timezone.utc)) - dt).total_seconds())
    if secs < 3600:
        return f"{int(secs // 60)}m old"
    if secs < 86400:
        return f"{int(secs // 3600)}h old"
    return f"{int(secs // 86400)}d old"


async def _topic_reach(pool, entity_id: int, thread_id, topic: str | None) -> int:
    """How many OTHER threads carry this topic name. The depth signal:
    a fact about the thread history, never a judgement of the person —
    explanation_level stays user-set.

    It excludes `thread_id` BY ID in the SQL rather than subtracting 1 from
    the total. Subtracting 1 assumed the current thread's topic came from its
    own `circles_threads.topic` column and so was always in the count(*) —
    but `topic` here can also come from the LIVE SUMMARY's segment_topic
    (render_block passes `topic = topic or srow.get("segment_topic")`), which
    the thread's own row may not have adopted yet. In that case the thread is
    not in the count at all, and subtracting 1 undercounted every other
    thread sharing that topic by one.
    """
    if not topic:
        return 0
    from mastercode.circles_threads import is_legacy
    args: list = [entity_id, topic]
    where = "entity_id = $1 AND topic = $2 AND deleted_at IS NULL"
    if not is_legacy(thread_id):
        args.append(str(thread_id))
        where += f" AND id <> ${len(args)}::uuid"
    try:
        n = await pool.fetchval(
            f"SELECT count(*) FROM circles_threads WHERE {where}", *args)
    except Exception as exc:  # noqa: BLE001
        log.warning("toc topic reach failed (entity=%s): %s", entity_id, exc)
        return 0
    return max(0, int(n or 0))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _flat(value) -> str:
    """One line, whatever arrives. EVERY value interpolated into the block
    goes through this.

    The twelve-line cap is applied by slicing `lines` (BLOCK_MAX_LINES), and
    a title or a segment_topic is free text — the user types the first and
    the consolidator's model writes the second. One embedded newline turns
    one element of that list into two rendered lines and the cap stops
    holding, so the cap is only real if no value can carry a newline.
    """
    return " ".join(str(value or "").split())


# The rendered block, per (entity_id, scope), valid while the scope's newest
# row is the one it was rendered from. Shape mirrors runtime/diagnostics.py's
# _CACHE (dict keyed by entity -> (validity token, value)); the token is the
# scope's max(created_at) rather than a clock, because this block is a
# function of the rows and not of the time.
#
# WHY A STAMP AND NOT A TTL: a TTL either serves a block that a turn has
# already invalidated or re-reads a scope nothing has touched. The stamp is
# exact in the direction that matters — a new turn in the scope always misses.
#
# WHAT THE STAMP CANNOT SEE: a summary, goal or artifact written with NO new
# turn row in the scope. That does not happen in practice — every one of those
# writers runs as a post-turn task OF a turn in that same scope
# (thread_consolidator, batch_consolidator, stash_tool_artifacts), so the row
# that triggered them has already moved the stamp, and the next render in this
# scope is a miss anyway. A user RENAME is the one edit with no row behind it,
# and it reaches the block on the next message in that thread — which is the
# next time the block is rendered at all.
#
# The same holds for the one clock-derived string in
# the block, the summary's age: it is re-derived on every miss, and a hit
# means no message has been sent in this thread since it was derived.
_BLOCK_CACHE: dict[tuple, tuple] = {}
_BLOCK_CACHE_MAX = 512


async def _scope_stamp(pool, entity_id: int, src_ctx: dict):
    """max(created_at) over this scope's transcript rows — the cache's
    validity token. Returns a fresh object() when the read fails, which can
    never equal a stored token, so an unreadable stamp means a MISS rather
    than a stale block."""
    args: list = [entity_id]
    where = _turn_scope(args, session_id="", src_ctx=src_ctx, live_only=False)
    try:
        return await pool.fetchval(
            f"SELECT max(created_at) FROM entity_memory "
            f"WHERE {' AND '.join(where)}", *args)
    except Exception as exc:  # noqa: BLE001
        log.warning("toc block: scope stamp unavailable (entity=%s): %s",
                    entity_id, exc)
        return object()


def _cache_put(key: tuple, stamp, block: str) -> None:
    _BLOCK_CACHE[key] = (stamp, block)
    while len(_BLOCK_CACHE) > _BLOCK_CACHE_MAX:
        _BLOCK_CACHE.pop(next(iter(_BLOCK_CACHE)), None)


async def render_block(pool, entity_id: int, thread_id, *, session_id: str) -> str:
    """The PUSHED block — at most twelve lines, counts and never contents.

    WHY COUNTS. The host is never handed a number it could narrate as
    knowledge: no score, no cost, no importance, no weight appears
    here, and the ordering weight that picked the RELATED titles is not in
    the text. What IS here is how MUCH there is — and that is the point: the
    counts are what make the host reach for `chat_threads op='toc'`, which is
    the answer to tools that must be remembered going uncalled.

    WHEN IT IS EMPTY. A real thread always renders: its identity and size
    are facts the host has nowhere else, and a brand-new thread saying
    "0 messages, nothing yet" is exactly a first turn. The LEGACY scope
    renders only when it has something to say — a live summary, an open goal
    or unresolved item, an artifact, a related thread, or a tool-trail item.
    Turn count alone does not count as something to say there: every Circles
    chat ever written is in that scope, so a line reporting its size would
    ride every legacy turn forever and tell the host nothing it can act on.

    WHAT IT COSTS, per turn, on a threaded scope (measured by reading the
    call graph; every query is one round-trip):

      BEFORE  13-17 queries. get_thread 2 (row + a full-scope count) ·
              thread_weight 3 (typed counts, a SECOND full-scope count via
              count_scope_turns, cost) · artifacts 1 · tool trail 1 ·
              summary 1 · related_threads 4-8 (its own get_thread = a THIRD
              full-scope count) · topic reach 1.
      AFTER   11-15 on a miss: the thread row and its count are read ONCE
              and passed through (thread_weight count_turns=False,
              related_threads own_row=), and the stamp query is +1.
              1 on a HIT — the stamp query alone.

    The legacy scope pays the same shape over the entity's whole Circles
    history, which is why the counts are deduplicated there too even though
    that scope usually renders nothing.
    """
    from mastercode.circles_threads import is_legacy
    legacy = is_legacy(thread_id)
    src = _descriptor(thread_id)

    cache_key = (int(entity_id), str(src.get("kind") or ""),
                 str(src.get("ref") or ""))
    stamp = await _scope_stamp(pool, entity_id, src)
    cached = _BLOCK_CACHE.get(cache_key)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    try:
        from mastercode import circles_threads as ct
        thread = await ct.get_thread(pool, entity_id, thread_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("toc block: thread row unavailable (entity=%s): %s",
                    entity_id, exc)
        thread = None
    if thread is None:
        return ""
    # The transcript count the Circles list shows, read once here
    # (circles_threads._turn_count, through get_thread) and never recounted.
    turn_count = int(thread.get("turn_count") or 0)

    # Every read here is bounded: weight, artifacts, trail and the summary
    # are single-scope reads, and related_threads is batched (two queries
    # for all candidates, never one per candidate), so all of them run
    # before the legacy scope decides whether it has anything to say.
    weight = await thread_weight(pool, entity_id, thread_id, count_turns=False)
    artifacts = await scope_artifacts(pool, entity_id, src)
    trail = await scope_tool_trail(pool, entity_id, src, session_id=session_id)

    summary_age = None
    topic = thread.get("topic")
    try:
        # Only while the consolidator runs: with it off no
        # summary rides the turn, and naming a frozen one's age would point
        # the Host at context it does not have.
        from mastercode import summary_policy as _tcm
        if _tcm.summary_on(src):
            from mastercode.thread_consolidator import get_live_summary
            srow = await get_live_summary(pool, entity_id, src)
            if srow:
                summary_age = _age_phrase(srow.get("created_at"))
                topic = topic or srow.get("segment_topic")
    except Exception as exc:  # noqa: BLE001
        log.warning("toc block: summary unavailable (entity=%s): %s",
                    entity_id, exc)

    open_n = int(weight.get("goals_open") or 0)
    unres_n = int(weight.get("unresolved") or 0)
    has_anything = bool(summary_age or open_n or unres_n or artifacts
                        or trail.get("tasks") or trail.get("repos"))
    # Related threads count as something to say too — a legacy scope whose
    # summaries are all superseded can still share topics with real threads.
    # Cheap now that related_threads is batched (four queries, not per
    # candidate), so it is only skipped when the rest already decided.
    related = await related_threads(pool, entity_id, thread_id,
                                    own_row=thread)
    if legacy and not has_anything and not related:
        _cache_put(cache_key, stamp, "")
        return ""

    # A thread's title is null until the user sets one or the consolidator's
    # first segment_topic fills it in (circles_threads.set_thread_topic), so
    # "untitled" is a real and common state — and it is NOT the same thing as
    # the original conversation, which is the one scope that has no row.
    if thread.get("title"):
        head = [f'"{_flat(thread["title"])}"']
    elif legacy:
        head = ["the original conversation"]
    else:
        head = ["untitled thread"]
    head.append(f"topic: {_flat(topic)}" if topic else "no topic named yet")
    # "N messages", NOT "turn N". FRAME renders `turn <user turns + 1>` — the
    # position of the message being answered (runtime/frame.py:228-229) —
    # while this is the whole transcript, user and assistant rows together.
    # Two adjacent blocks saying "turn N" about the same thread with two
    # different N is a number the host cannot use, so this one names what it
    # counts and leaves the turn index to FRAME, which owns it.
    #
    # The LEGACY scope gets no count line at all: it holds every Circles
    # message the entity ever sent, so its size is a constant the host cannot
    # act on — the same reason that scope renders nothing when it has nothing
    # else to say.
    if not legacy:
        head.append(_plural(turn_count, "message"))
    if summary_age:
        head.append(f"summary {summary_age}")

    lines = [
        "## This thread, by the numbers",
        "Counts from rows, not a recollection. To open any of it, call "
        "chat_threads op='toc' — it returns each item with its id, and "
        "op='read' opens one. Do not recite these numbers back; use them to "
        "decide what to look at.",
        "THIS THREAD  " + " · ".join(head),
    ]

    reach = await _topic_reach(pool, entity_id, thread_id, topic)
    if topic:
        lines.append("TOPIC        " + (
            f"seen in {_plural(reach, 'other thread')}" if reach
            else "new to this host"))

    if open_n or unres_n:
        bits = []
        if open_n:
            bits.append(_plural(open_n, "open goal"))
        if unres_n:
            bits.append(_plural(unres_n, "unresolved item"))
        lines.append("OPEN         " + " · ".join(bits))

    if related:
        titles = [_flat(r.get("title")) or "untitled" for r in related[:3]]
        lines.append("RELATED      " + " · ".join(titles))

    if artifacts:
        kept = sum(1 for a in artifacts if a.get("kept"))
        tail = f" ({kept} kept by the janitor)" if kept else ""
        lines.append(f"ARTIFACTS    {len(artifacts)} in this thread{tail}")

    tasks, repos = trail.get("tasks") or [], trail.get("repos") or []
    if tasks or repos:
        bits = []
        if tasks:
            bits.append(_plural(len(tasks), "task"))
        if repos:
            bits.append(_plural(len(repos), "repo") + " touched")
        lines.append("CODE         " + " · ".join(bits)
                     + " (from the tool trail)")

    block = "\n".join(lines[:BLOCK_MAX_LINES])
    _cache_put(cache_key, stamp, block)
    return block
