"""The thread consolidator (conversation depth) — grown into the context
janitor: summarize the window AND keep the bucket clean.

Chat history is a fixed verbatim window over entity_memory turn rows; what
falls off the back used to be silently forgotten. This module rewrites a
rolling summary for one thread scope so the window heals instead of
amputating.

WHO CALLS IT NOW. The routine writer is
batch_consolidator, on the turn-consolidator cron: the rolling summary comes
back from the SAME structured call that derives the batch's memories, and
_store_summary below is what writes it. maybe_consolidate is no longer
dispatched post-turn from session._post_message_tasks — two writers over two
different inputs produced two different summaries of one thread, each
superseding the other's row in whichever order they landed. It remains here as
a MANUAL entry point (force=True) and keeps the _INFLIGHT guard both paths
share, so a hand-run and a cron pass cannot collide. One post-turn caller
remains: session.dispatch_topic_summary,
for a Circles topic and only while the loop is OFF (turn_consolidator
.summary_written_in_turn), so there is still one writer at a time.

  Trigger (maybe_consolidate, manual):
    verbatim history for the scope > ~60% of the history budget (fill-to-
    fit: the budget is the model's window, so this means "the
    thread is about to stop fitting"), OR 60 turns since the last
    consolidation (the conv-bench sweep's cadence) — whichever first. The
    45-minute idle-gap trigger went with the post-turn lane: it asked "did
    this turn return from idle", which only a post-turn caller can know.
    A failed run backs the scope off for FAIL_BACKOFF_S.
  Input:  the previous rolling summary + the newest verbatim turns that
    fit the consolidation model's OWN window (corrections live in the
    newest turns; summarizing only evicted turns would fossilize the stale
    position — and what falls off the oldest end is already in the
    previous summary).
  Output: a rewritten ~250-word summary on the Host's own model (the chat
    route via resolve_role_route — the same pattern
    memory_consolidation uses), stored as memory_type='summary' tagged
    with the thread's source_context, superseding the prior summary row
    (t_valid_until + resolution_pointer) in ONE transaction. Turn rows are
    never modified.
  Next-turn assembly: session.py places the latest live summary row ahead
    of the verbatim turns (get_live_summary).

Additions:
  1. ARTIFACT CAPTURE — stash_tool_artifacts: a turn's substantive tool
     results (which used to die with the turn's message list) are stashed
     as user_context rows (source='tool:<name>', status 'raw', tagged with
     the thread's source_context + turn id) via the ONE staging service.
  2. JANITOR — when a consolidation runs, the cheap model also reviews
     the scope's unprocessed artifact rows: KEEP (durable, optionally
     condensed) or DROP (transient → status 'dismissed', out of recall).
     Kept rows get a per-ids embed job through the existing embed_jobs
     machinery (the consolidator submits, the worker embeds).
  4. TOPIC SEGMENTS — the consolidator answers in structured JSON
     {"summary", "topic_changed", "topic"}; the topic rides the summary
     row's metadata (segment_topic) and a flip notes the previous topic
     in one line ("Previously: X") so continuity survives the boundary.

Hard rules: never blocks or errors the chat turn (every failure logs and
degrades to the old behavior), never runs twice concurrently for the same
(entity, scope) — a second trigger while in flight is a no-op.
"""

import hashlib
import json
import logging
import time
from datetime import datetime, timezone

log = logging.getLogger("cseries.mastercode.thread_consolidator")

# Trigger defaults. Fill-to-fit: the consolidator engages
# when the thread stops fitting the REAL history budget (session.py sizes
# it to the model's window now, not a 2k slice), so pressure is the trigger
# that matters. Cadence follows the conv-bench sweep (scripts/conv-bench/
# README.md): 60-turn cadence won; 30 "bought nothing for +60% consolidator
# spend"; the earlier 8 was the pre-sweep proposal and, with a 2k budget,
# fired every ~2 exchanges. Summary target follows the sweep too (~250
# words), so it stays a small slice of the window it sits in.
PRESSURE_RATIO = 0.60        # of the history budget, in est. tokens
TURNS_SINCE_TRIGGER = 60     # turns since the last consolidation (sweep winner)
SUMMARY_TARGET_WORDS = 250   # the sweep's summary size
# Output cap. 700 → 8000. 700 was sized for the ANSWER alone —
# ~250 words in a three-key envelope — on the reasoning that the route's own
# effort would keep thinking short. Measured on one Host: the last summary that
# LANDED for scope app:circles is version 5; every attempt since
# logs "thread consolidator: empty summary … empty response from anthropic"
# with in=0 out=0, which is what provider_gateway.py:1346-1347 returns when the
# reply carries neither content nor tool_calls. A reasoning model spends
# max_tokens on thinking and answer alike, so the budget was consumed before
# any visible text existed. Sizing for the answer is the bug; the budget has to
# cover the thinking that precedes it. Headroom costs nothing when unused —
# output is billed on tokens produced, not on the cap.
# 8000 -> None: None is the route's OWN
# published output limit. provider_gateway resolves it from the catalog AND
# holds it to half the model's window (context_budget.clamp_output_reserve, the
# rule this module used to apply itself), so a small-window model still gets a
# valid request. A reply the limit still cuts off is reported as cut.
SUMMARY_MAX_TOKENS = None
WINDOW_TURN_LIMIT = 120      # the "full current verbatim window" read cap
# The consolidator's own input is budgeted against ITS model's window —
# it used to read 120 turns uncapped, so a small-window model
# overflowed every time and the summary silently stopped updating.
#
# This is the WINDOW reservation, and it is deliberately NOT SUMMARY_MAX_TOKENS
# (they were once one constant). The two answer different
# questions: the wire cap above says "do not cut the reply off mid-object", and
# it now has to cover a reasoning model's thinking as well as its answer; this
# says "leave room in the context window for the reply". Setting the reservation
# to the raised cap makes an 8k-window model degenerate — window
# minus reserve goes to zero and _fit_turns_to_consolidator_window falls back to
# a single turn, so a small model would consolidate from one turn of input. 1200
# was sized for the manual path's reply: ~250 words of prose plus the three-key
# JSON envelope, with slack. The routine batch reply is larger — memories and
# the summary — and a long batch can overrun it;
# the wire cap (the route's own output limit, resolved by the gateway) is what
# keeps that reply whole, so an overrun costs input turns, never a cut-off reply.
CONSOLIDATOR_OUTPUT_RESERVE = 1200
# The prompt's fixed instructions + labels, reserved before any turn is fitted.
# Measured on the batch prompt (build_batch_prompt, empty window, chars/4):
# ~920 tokens, before and again after the model-judged audit
# came and went. The old 400 under-reserved; this keeps slack.
CONSOLIDATOR_SCAFFOLD_TOKENS = 1300
# After a failed run (model error / empty / unparseable), the scope waits
# this long before trying again — without it every following turn re-fired
# a doomed call (the trigger conditions stay true until a summary lands).
FAIL_BACKOFF_S = 15 * 60

# Artifact capture / janitor bounds.
ARTIFACT_MIN_CHARS = 200     # below this a tool result is transient noise
ARTIFACT_MAX_CHARS = 4000    # per-result capture cap
JANITOR_BATCH = 20           # max artifact rows reviewed per consolidation

# WHICH tool results may become context rows at all.
# A stashed row lands in the user's corpus, where the Context Explorer and the
# host's own op='files' list it beside the user's uploads. The size floor alone
# admitted the host's SELF-STATE reads — a fresh entity's corpus held files
# named "personality_state result" and "model_route result" (272 / 448 bytes)
# that the host then described to a stranger as the user's documents. The
# rule is the registry's own `_meta.category`: only tools whose category names
# EXTERNAL content (research findings, pages read, code the user asked about,
# files worked on) are artifact-worthy. Every host-introspection category
# (brain, memory, projects, apps, environment, system, governance, vault,
# network, comms, review, build, …) is not — that output describes the host,
# not anything the user gave it. A tool with no registry entry is NOT worthy
# (unknown provenance never becomes a user document).
ARTIFACT_CATEGORIES: frozenset[str] = frozenset({
    "research",    # github, read_url, research, web_search
    "codespace",   # local_grep — code the user asked about
    "files",       # workspace_file — files the host worked on for the user
})

# The host's five permanent orchestration verbs (dispatch / operate / recall /
# agent_status / escalate) carry no registry schema — session.py builds them —
# so the category rule alone would drop them all. Exactly one of them returns
# EXTERNAL content: `dispatch`, whose result is a delegated agent's report (a
# research agent's findings are the canonical "the user may ask about this
# later" case, and they are stashed as tool:dispatch). The other
# four describe the host's own state or plumbing and stay out.
ARTIFACT_TOOLS: frozenset[str] = frozenset({"dispatch"})


def _tool_category(name: str) -> str | None:
    """The registry category of a host tool, or None when no registry carries
    a schema for it. Scans every schema dict the registry module exposes so a
    tool added to a new registry is classified by the same rule."""
    if not name:
        return None
    try:
        from mastercode import public_tool_registry as _reg
    except Exception:
        return None
    for attr in dir(_reg):
        obj = getattr(_reg, attr, None)
        if not isinstance(obj, dict):
            continue
        for v in obj.values():
            if not isinstance(v, dict) or "function" not in v:
                break  # not a schema dict
            fn = v.get("function") or {}
            if fn.get("name") == name:
                return ((v.get("_meta") or {}).get("category")) or None
    return None


def artifact_worthy(tool_name: str) -> bool:
    """May this tool's result be stashed into the user's context corpus?
    True for the external-content categories above and for the one
    orchestration verb that returns external content (ARTIFACT_TOOLS)."""
    if (tool_name or "") in ARTIFACT_TOOLS:
        return True
    return _tool_category(tool_name) in ARTIFACT_CATEGORIES

# Per-(entity, scope) in-flight guard: two rapid turns must not spawn two
# consolidators racing to supersede the same summary row. Module-level set;
# the runtime is single-process asyncio so membership check+add is atomic.
_INFLIGHT: set[tuple] = set()
# Per-(entity, scope) last-failure clock (time.monotonic) for FAIL_BACKOFF_S.
_LAST_FAIL: dict[tuple, float] = {}

_CORRECTIONS_RULE = (
    "Corrections rule: when a newer turn contradicts or replaces an earlier "
    "instruction, fact, or decision, the summary carries ONLY the current "
    "(newest) position, plus a one-line note that a correction happened and "
    "what it replaced. Never carry the stale position forward as if current."
)


def _scope_key(entity_id: int, src_ctx: dict) -> tuple:
    return (entity_id, str(src_ctx.get("kind") or ""), str(src_ctx.get("ref") or ""))


async def get_live_summary(pool, entity_id: int, src_ctx: dict | None) -> dict | None:
    """The latest live rolling-summary row for a thread scope, or None.

    'Live' = status active AND not superseded (t_valid_until). Read by the
    next-turn assembly (session.py) and the thread-context REST payload.
    Never raises — None on any failure (old behavior: no summary)."""
    if not src_ctx or not src_ctx.get("kind"):
        return None
    try:
        row = await pool.fetchrow(
            """
            SELECT id, content, metadata, created_at
              FROM entity_memory
             WHERE entity_id = $1
               AND memory_type = 'summary'
               AND status = 'active'
               AND (t_valid_until IS NULL OR t_valid_until > NOW())
               AND metadata->>'source_kind' = 'thread_consolidator'
               AND metadata->'source_context'->>'kind' = $2
               AND metadata->'source_context'->>'ref' = $3
             ORDER BY id DESC
             LIMIT 1
            """,
            entity_id, str(src_ctx.get("kind")), str(src_ctx.get("ref") or ""),
        )
    except Exception as e:
        log.warning("get_live_summary failed (entity=%d): %s", entity_id, e)
        return None
    if not row:
        return None
    md = row["metadata"]
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    md = md or {}
    created = row["created_at"]
    return {
        "id": row["id"],
        "content": row["content"] or "",
        "version": md.get("summary_version"),
        "segment_topic": md.get("segment_topic"),
        "created_at": created,
        "created_iso": created.isoformat() if created else None,
    }


async def supersede_live_summary(pool, entity_id: int, src_ctx: dict) -> int | None:
    """Retire this scope's live rolling summary with NO successor. Returns
    the id of the row retired, or None when the scope had none.

    The supersede half of _store_summary without the insert: t_valid_until =
    NOW(), which is the one predicate get_live_summary filters on (:215).
    resolution_pointer stays NULL on purpose — the convention it records is
    "the id of the row that replaced this one" (init-db.sql:181-182), and
    here nothing did. A reader walking the chain sees a summary that stopped
    being true rather than one that was rewritten, which is what happened.

    Called when the scope's content changes underneath the summary rather
    than growing past it — a Split moving turn rows out
    (circles_threads.split_thread). Errors PROPAGATE: every caller is inside
    a transaction that wants to roll back with this.

    `pool` may be a pool or a connection inside an open transaction.
    """
    if not src_ctx or not src_ctx.get("kind"):
        return None
    return await pool.fetchval(
        """
        UPDATE entity_memory
           SET t_valid_until = NOW(), updated_at = NOW()
         WHERE id = (
               SELECT id FROM entity_memory
                WHERE entity_id = $1
                  AND memory_type = 'summary'
                  AND status = 'active'
                  AND (t_valid_until IS NULL OR t_valid_until > NOW())
                  AND metadata->>'source_kind' = 'thread_consolidator'
                  AND metadata->'source_context'->>'kind' = $2
                  AND metadata->'source_context'->>'ref' = $3
                ORDER BY id DESC
                LIMIT 1)
        RETURNING id
        """,
        entity_id, str(src_ctx.get("kind")), str(src_ctx.get("ref") or ""),
    )


async def _flip_turn_id(pool, entity_id: int, src_ctx: dict) -> int | None:
    """The newest turn row the CIRCLES conversation for this scope shows —
    what a topic flip's marker anchors on (circles_threads.markers).

    The consolidator is handed role/content pairs and never the ids
    (recall_recent_turns returns role/content only), so the first
    turn of the new segment is not derivable here; the newest turn the
    window covered is, and it is a turn the new topic already contains.
    Resolved through circles_threads.shown_where so the anchor is a row the
    user can see, and only for the two scopes that surface has: a thread,
    and the un-migrated app:circles conversation. Every other scope (project,
    peer, session) has no marker reader, so it records nothing.

    Never raises — a missing anchor costs the marker its precise from_turn_id
    and nothing else.
    """
    try:
        from mastercode import circles_threads as _ct
        kind = src_ctx.get("kind")
        ref = str(src_ctx.get("ref") or "")
        if kind == "thread":
            thread_id: str | None = ref
        elif kind == "app" and ref == "circles":
            thread_id = None
        else:
            return None
        args: list = [entity_id]
        where = _ct.shown_where(args, thread_id)
        row_id = await pool.fetchval(
            f"SELECT id FROM entity_memory WHERE {' AND '.join(where)}"
            f" ORDER BY created_at DESC, id DESC LIMIT 1",
            *args,
        )
        return int(row_id) if row_id is not None else None
    except Exception as e:  # noqa: BLE001
        log.warning("flip anchor lookup failed (entity=%d scope=%s:%s): %s",
                    entity_id, src_ctx.get("kind"), src_ctx.get("ref"), e)
        return None


async def _store_summary(
    pool,
    entity_id: int,
    session_id: str,
    src_ctx: dict,
    content: str,
    prev: dict | None,
    turns_covered: int,
    *,
    segment_topic: str | None = None,
    prev_topic: str | None = None,
    topic_changed: bool = False,
) -> tuple[int | None, int]:
    """Insert the new summary row and supersede the prior one in ONE
    transaction (t_valid_until=NOW() + resolution_pointer — the same
    supersede-not-append rule memory_consolidation lives by). Turn rows
    are never touched. Returns (new_id, new_version).

    Topic segmentation: the segment's topic rides the row's metadata
    (segment_topic); a flip prepends a one-line "Previously: X" so thread
    continuity survives the segment boundary, and records `flip_turn_id` —
    the turn the new topic is anchored on, which circles_threads.markers
    hands the user as the place to Split."""
    version = int((prev or {}).get("version") or 0) + 1
    if topic_changed and prev_topic:
        content = f"Previously: {prev_topic}\n\n{content}"
    md = {
        "source_kind": "thread_consolidator",
        "source_context": {"kind": src_ctx.get("kind"), "ref": str(src_ctx.get("ref") or "")},
        "summary_version": version,
        "consolidated_at": datetime.now(timezone.utc).isoformat(),
        "turns_covered": turns_covered,
    }
    if segment_topic:
        md["segment_topic"] = segment_topic
    if topic_changed and segment_topic:
        # WHERE the new topic starts, recorded at the only moment anything
        # knows it. Without it circles_threads.markers has to anchor on this
        # row's created_at, and the consolidator runs minutes after the flip
        # — so a Split on the marker left the new topic's first turns behind
        # in the old thread. One query, only on a flip.
        flip_turn_id = await _flip_turn_id(pool, entity_id, src_ctx)
        if flip_turn_id is not None:
            md["flip_turn_id"] = flip_turn_id
    project_id = None
    if src_ctx.get("kind") == "project":
        try:
            project_id = int(src_ctx.get("ref"))
        except (TypeError, ValueError):
            project_id = None
    # No embedding and no dimension_vector on purpose: the summary is
    # fetched explicitly by scope (get_live_summary), and rows without
    # vectors stay out of every similarity scan / standing memory block.
    # It also keeps consolidation independent of the embed lane.
    async with pool.acquire() as conn:
        async with conn.transaction():
            new_id = await conn.fetchval(
                """
                INSERT INTO entity_memory
                  (entity_id, content, memory_type, importance, status,
                   source_session, metadata, project_id)
                VALUES
                  ($1, $2, 'summary', 1.0, 'active', $3, $4::jsonb, $5)
                RETURNING id
                """,
                entity_id, content, session_id, json.dumps(md), project_id,
            )
            if prev and prev.get("id") and new_id:
                await conn.execute(
                    """
                    UPDATE entity_memory
                    SET t_valid_until = NOW(),
                        resolution_pointer = $2,
                        updated_at = NOW()
                    WHERE id = $1
                      AND entity_id = $3
                    """,
                    prev["id"], new_id, entity_id,
                )
    # Circles threads: when this scope is a thread, the topic also lands on
    # circles_threads.topic so the thread list/UI can show it without
    # reading entity_memory. Lazy import + broad guard: a missing module or
    # a DB error here must never break summary storage, which just
    # committed above.
    if segment_topic and src_ctx.get("kind") == "thread":
        try:
            from mastercode.circles_threads import set_thread_topic
            await set_thread_topic(pool, entity_id, str(src_ctx.get("ref") or ""), segment_topic)
        except Exception as e:
            log.warning(
                "thread consolidator: set_thread_topic failed (entity=%d scope=thread:%s): %s",
                entity_id, src_ctx.get("ref"), e,
            )
    return new_id, version


def _build_summary_prompt(
    prev_summary: str,
    turns: list[dict],
    prev_topic: str | None = None,
) -> list[dict]:
    """The consolidator prompt: previous rolling summary + the FULL current
    verbatim window (retained AND evicted), with the corrections rule.
    The answer is structured JSON — the summary plus a topic-segmentation
    verdict (did the subject flip since the previous segment, and what is
    the new topic's short name)."""
    lines = [
        "Rewrite the rolling summary of this conversation thread.",
        "Carry forward: decisions made, open threads, facts established, "
        "corrections applied.",
        _CORRECTIONS_RULE,
        f"Target under {SUMMARY_TARGET_WORDS} words. Write in third person, dense prose with "
        "short section headers (Decisions / Open threads / Facts / Corrections).",
        "",
        "Topic segmentation: decide whether the conversation's subject has "
        "flipped since the previous segment"
        + (f" (previous topic: {prev_topic})." if prev_topic else ".")
        + " A long pause followed by a new subject is a flip; a continuation "
        "or refinement of the same subject is not.",
        "",
        'Respond with ONLY a JSON object: {"summary": "<the rewritten '
        'summary>", "topic_changed": true|false, "topic": "<a 3-6 word name '
        'for the current topic>"}.',
        "",
        "=== PREVIOUS SUMMARY ===",
        (prev_summary or "(none — this is the first consolidation)"),
        "",
        "=== CONVERSATION WINDOW (oldest to newest) ===",
    ]
    for t in turns:
        role = (t.get("role") or "user").upper()
        lines.append(f"[{role}]: {t.get('content') or ''}")
    return [
        {"role": "system", "content": "You are a precise conversation summarizer."},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _parse_consolidation_output(raw: str) -> dict:
    """Parse the consolidator's structured answer:
    {"summary": ..., "topic_changed": bool, "topic": str}.

    Tolerant by contract: a non-JSON reply is treated as a bare summary
    (the older shape — an older prompt, a chatty model), never an error."""
    text = (raw or "").strip()
    if not text:
        return {"summary": "", "topic_changed": False, "topic": None}
    candidate = text
    if not candidate.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            candidate = text[start:end + 1]
    try:
        obj = json.loads(candidate)
    except Exception:
        # A reply that STARTED as our JSON object but doesn't parse is a
        # truncated/garbled answer (output cap hit mid-object), not a bare
        # summary — storing it would put '{"summary": "...' in the window.
        # Empty → the caller logs model_empty and backs off.
        if candidate.lstrip().startswith("{"):
            return {"summary": "", "topic_changed": False, "topic": None}
        return {"summary": text, "topic_changed": False, "topic": None}
    summary = str(obj.get("summary") or "").strip() if isinstance(obj, dict) else ""
    if not summary:
        return {"summary": text, "topic_changed": False, "topic": None}
    topic = str(obj.get("topic") or "").strip()[:120] or None
    return {
        "summary": summary,
        "topic_changed": bool(obj.get("topic_changed")),
        "topic": topic,
    }


async def _default_model_fn(pool, entity_id: int, messages: list[dict]) -> dict:
    """The consolidator's model: the Host's own (the chat route; the
    Memory role is retired), resolved exactly the way
    memory_consolidation does
    (resolve_role_route → the single routing truth; full route so the
    base_url + failover pair reach the call).

    The route's `effort` rides along too (route_effort — the user's per-role
    pick, else an explicit "low"). It was dropped here, so the model decided
    for itself and reasoned into SUMMARY_MAX_TOKENS: measured, the pass logged "empty summary" — which is exactly what
    _parse_consolidation_output returns when the reply started as our JSON
    object and was cut mid-object, so the caller backs off rather than storing
    '{"summary": "…' into the window.

    Passing the effort was necessary and not sufficient. The text that stood
    here argued the budget should STAY at 700 because 700 is ample for the
    ANSWER — which is true and was the wrong question. Measured on one Host:
    the newest summary that landed for scope app:circles is
    version 5, and every attempt in the fortnight since logged
    "empty summary … empty response from anthropic" with in=0 out=0. Not a
    truncated envelope — NO visible text at all, the shape
    provider_gateway.py:1346-1347 returns when a reply carries neither content
    nor tool_calls. The budget is spent on thinking before the answer begins,
    so it has to cover both. See SUMMARY_MAX_TOKENS."""
    from mastercode.inference_config import chosen_effort, resolve_role_route, route_effort
    route = await resolve_role_route(pool, entity_id, "chat")
    if not route.get("model"):
        # Unconfigured — surface the resolver's reason, never guess a model.
        return {"error": route.get("unconfigured")
                or "no chat model configured", "content": ""}
    # The route's own output limit (SUMMARY_MAX_TOKENS is None). The window
    # clamp this call used to apply here (a fixed 8000 asked of a 4k-window
    # model is invalid before any input is counted) is the gateway's now: it
    # resolves the model's limit and holds it to half the window, by the same
    # context_budget.clamp_output_reserve rule.
    from mastercode.runtime.tools import _call_entity_model
    return await _call_entity_model(
        messages, temperature=0.3, max_tokens=SUMMARY_MAX_TOKENS,
        provider=route["provider"], provider_model=route["model"],
        base_url=route.get("base_url"),
        fallback_provider=route.get("fallback_provider"),
        fallback_model=route.get("fallback_model"),
        reasoning_effort=route_effort(route),
        chosen_effort=chosen_effort(route),
    )


# ── Artifact capture ─────────────────────────────────────

async def stash_tool_artifacts(
    pool,
    entity_id: int,
    session_id: str,
    src_ctx: dict | None,
    artifacts: list[dict],
    *,
    turn_id: str | None = None,
) -> dict:
    """Stash a turn's substantive tool results as user_context rows so a
    follow-up question can recall them instead of refetching (tool output
    used to die with the turn's message list).

    Rows land through stage_context_service — the ONE staging write path
    (search_tsv, multi-dim column routing, backend externalization all
    inherited) — with source='tool:<name>', the default 'raw' status every
    staged row carries, and metadata tagging the thread's source_context +
    turn id. Only tools in an ARTIFACT_CATEGORIES category are stashed (the
    host's self-state reads never become user documents — `not_worthy` counts
    them); trivially small results (< ARTIFACT_MIN_CHARS) and duplicates
    within the turn are skipped; content is capped at ARTIFACT_MAX_CHARS.
    NEVER raises — a capture failure is a log line, nothing more."""
    out = {"stashed": 0, "skipped": 0, "not_worthy": 0}
    if not src_ctx or not src_ctx.get("kind") or not artifacts:
        return out
    try:
        # user_context is declared in init-db.sql and migrations/v090; the
        # ensure_table() this used to call is gone. Worth knowing why
        # it mattered: this import sits inside the try whose handler is a bare
        # log.warning, so an ImportError here would not raise — it would make
        # stash_tool_artifacts return zeros on EVERY turn, silently.
        from server.routes.context import stage_context_service  # context.py:1267
        seen: set[str] = set()
        by_tool: dict[str, list[dict]] = {}
        for a in artifacts:
            if not artifact_worthy(str((a or {}).get("tool") or "")):
                out["not_worthy"] += 1
                continue
            content = str((a or {}).get("content") or "").strip()
            if len(content) < ARTIFACT_MIN_CHARS:
                out["skipped"] += 1
                continue
            content = content[:ARTIFACT_MAX_CHARS]
            digest = hashlib.sha1(content.encode("utf-8", "replace")).hexdigest()
            if digest in seen:
                out["skipped"] += 1
                continue
            seen.add(digest)
            name = str((a or {}).get("tool") or "unknown")[:80]
            by_tool.setdefault(name, []).append({
                "content": content,
                "title": f"{name} result ({turn_id or session_id})",
                "source_id": f"artifact:{session_id}:{digest[:16]}",
                "metadata": {
                    "source_context": {
                        "kind": src_ctx.get("kind"),
                        "ref": str(src_ctx.get("ref") or ""),
                    },
                    "turn_id": turn_id or session_id,
                    "artifact": True,
                },
            })
        for name, items in by_tool.items():
            res = await stage_context_service(
                pool, entity_id, source=f"tool:{name}", items=items)
            out["stashed"] += int((res or {}).get("imported") or 0)
    except Exception as e:
        log.warning("artifact stash failed (entity=%d): %s", entity_id, e)
    if out["stashed"] and src_ctx.get("kind") == "thread":
        # The thread deleted while its turn ran: the delete dismissed its
        # tool results, and these landed after it.
        from mastercode.circles_threads import forget_if_deleted
        await forget_if_deleted(pool, entity_id, src_ctx)
    return out


# ── Janitor curation + embed submission ──────────────────

_JANITOR_SYSTEM = (
    "You are the context janitor for a conversation thread. You review "
    "artifacts captured from tool results and decide which are durable "
    "(research findings, reference data the user may ask about later) and "
    "which are transient noise (directory listings, pings, statuses, "
    "one-off chatter). Durable beats big: condense when the value survives "
    "a rewrite."
)


async def _judge_artifact(
    model_fn,
    source: str,
    content: str,
) -> tuple[str | None, str | None]:
    """One cheap-model verdict per artifact. Returns (verdict, condensed):
    verdict 'keep' (condensed = optional rewrite) or 'drop'; (None, None)
    when the model could not answer — the caller leaves the row
    unprocessed so the next consolidation retries. A reply that doesn't
    parse is a KEEP: durable by default, never silently discarded."""
    messages = [
        {"role": "system", "content": _JANITOR_SYSTEM},
        {"role": "user", "content": (
            f"Artifact captured from `{source}`:\n\n"
            f"{(content or '')[:ARTIFACT_MAX_CHARS]}\n\n"
            'Respond with ONLY a JSON object: {"verdict": "keep"|"drop", '
            '"content": "<optional condensed rewrite when keeping; empty '
            'string to keep as-is>"}.'
        )},
    ]
    try:
        result = await model_fn(messages)
    except Exception:
        return None, None
    raw = str((result or {}).get("content") or "").strip()
    if not raw:
        return None, None
    candidate = raw
    if not candidate.startswith("{"):
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            candidate = raw[start:end + 1]
    try:
        obj = json.loads(candidate)
    except Exception:
        return "keep", None
    if not isinstance(obj, dict):
        return "keep", None
    verdict = str(obj.get("verdict") or "").strip().lower()
    if verdict not in ("keep", "drop"):
        return "keep", None
    if verdict == "drop":
        return "drop", None
    condensed = str(obj.get("content") or "").strip()[:ARTIFACT_MAX_CHARS] or None
    return "keep", condensed


async def _curate_artifacts(
    pool,
    entity_id: int,
    src_ctx: dict,
    model_fn,
) -> dict:
    """The janitor pass: review the scope's unprocessed artifact rows
    (source LIKE 'tool:%', this scope's tag, no artifact_processed flag).

    KEEP = mark processed (+ condensed content + rebuilt search_tsv when
    the model rewrote it); DROP = mark processed AND status 'dismissed' —
    the predicate every user_context recall lane filters on. Kept rows
    then ride ONE per-ids embed job through the existing embed_jobs
    machinery. Never raises; a model that can't answer leaves the row
    unprocessed for the next run."""
    stats = {"reviewed": 0, "kept": 0, "dropped": 0, "embed_job": None}
    try:
        from server.routes.context import _TSV_SQL, user_context_exists
        if not await user_context_exists(pool):
            return stats
        # pg-resident rows only: an externalized row's content column holds
        # the backend marker, and neither judging nor rewriting the marker
        # is meaningful (search rehydrates the real bytes on read).
        rows = await pool.fetch(
            """
            SELECT id, content, source
              FROM user_context
             WHERE entity_id = $1
               AND source LIKE 'tool:%'
               AND status = 'raw'
               AND COALESCE(storage_backend, 'pg') = 'pg'
               AND metadata->'source_context'->>'kind' = $2
               AND metadata->'source_context'->>'ref' = $3
               AND COALESCE(metadata->>'artifact_processed', 'false') != 'true'
             ORDER BY id
             LIMIT $4
            """,
            entity_id, str(src_ctx.get("kind")), str(src_ctx.get("ref") or ""),
            JANITOR_BATCH,
        )
    except Exception as e:
        log.warning("artifact janitor: fetch failed (entity=%d): %s", entity_id, e)
        return stats

    kept_ids: list[int] = []
    for r in rows:
        stats["reviewed"] += 1
        try:
            verdict, condensed = await _judge_artifact(
                model_fn, r["source"], r["content"])
            if verdict is None:
                continue  # model down — retry next consolidation
            if verdict == "drop":
                await pool.execute(
                    """
                    UPDATE user_context
                       SET status = 'dismissed',
                           metadata = metadata || $2::jsonb,
                           updated_at = now()
                     WHERE id = $1 AND entity_id = $3
                    """,
                    r["id"],
                    json.dumps({"artifact_processed": True,
                                "artifact_verdict": "drop"}),
                    entity_id,
                )
                stats["dropped"] += 1
                continue
            md = {"artifact_processed": True, "artifact_verdict": "keep"}
            if condensed and condensed != (r["content"] or ""):
                md["artifact_condensed"] = True
                # Content changed → rebuild the lexical index from the new
                # bytes (same guard as the PATCH route: never recompute a
                # tsv from an external row's marker).
                tsv = _TSV_SQL.format(title="title", content="$2")
                await pool.execute(
                    f"""
                    UPDATE user_context
                       SET content = $2,
                           search_tsv = CASE
                               WHEN COALESCE(storage_backend, 'pg') = 'pg'
                               THEN {tsv} ELSE search_tsv END,
                           metadata = metadata || $3::jsonb,
                           updated_at = now()
                     WHERE id = $1 AND entity_id = $4
                    """,
                    r["id"], condensed, json.dumps(md), entity_id,
                )
            else:
                await pool.execute(
                    """
                    UPDATE user_context
                       SET metadata = metadata || $2::jsonb,
                           updated_at = now()
                     WHERE id = $1 AND entity_id = $3
                    """,
                    r["id"], json.dumps(md), entity_id,
                )
            kept_ids.append(int(r["id"]))
            stats["kept"] += 1
        except Exception as e:
            log.warning("artifact janitor: row %s failed (kept for retry): %s",
                        r.get("id"), e)

    if kept_ids:
        try:
            from server.routes import embed as _embed
            job = await _embed.enqueue_job(
                entity_id=entity_id,
                job_type="tool_artifacts",
                target="user_context",
                metadata={"ids": kept_ids, "source": "thread_janitor"},
            )
            stats["embed_job"] = (job or {}).get("id")
        except Exception as e:
            # No embedding model configured, brain not ready, … — the rows
            # stay lexical-only, which is a real search arm, not an error.
            log.info("artifact janitor: embed enqueue skipped (entity=%d): %s",
                     entity_id, e)
    return stats


async def _consolidator_window(pool, entity_id: int) -> int | None:
    """The loop's model window (tokens), or None when the route/catalog
    can't say. Never raises."""
    try:
        from mastercode.inference_config import resolve_role_route
        from mastercode.context_budget import model_context_window
        route = await resolve_role_route(pool, entity_id, "chat")
        if not route.get("model"):
            return None
        return await model_context_window(route["model"], provider=route.get("provider"))
    except Exception as e:
        log.debug("consolidator window lookup failed (unbudgeted): %s", e)
        return None


def _fit_turns_to_consolidator_window(
    turns: list[dict], prev_summary: str, window: int | None,
) -> tuple[list[dict], int]:
    """Keep the NEWEST turns that fit the consolidator's input budget
    (window − output reserve − prompt scaffolding − previous summary) × 0.95,
    chars//4. Returns (kept_turns_oldest_first, dropped_count). Unknown
    window → everything, 0."""
    if not window or window <= 0:
        return turns, 0
    from mastercode.context_budget import CHARS_PER_TOKEN
    scaffold_tokens = CONSOLIDATOR_SCAFFOLD_TOKENS
    budget_tokens = int((window - CONSOLIDATOR_OUTPUT_RESERVE - scaffold_tokens
                         - len(prev_summary) // CHARS_PER_TOKEN) * 0.95)
    if budget_tokens <= 0:
        return turns[-1:], max(len(turns) - 1, 0)
    kept_rev: list[dict] = []
    used = 0
    for t in reversed(turns):
        n = (len(t.get("content") or "") + 16) // CHARS_PER_TOKEN
        if kept_rev and used + n > budget_tokens:
            break
        kept_rev.append(t)
        used += n
    kept = list(reversed(kept_rev))
    return kept, len(turns) - len(kept)


async def maybe_consolidate(
    pool,
    entity_id: int,
    session_id: str,
    src_ctx: dict | None,
    *,
    history_budget_tokens: int,
    model_fn=None,
    force: bool = False,
) -> dict | None:
    """Fire the consolidator for one thread scope when a trigger trips.

    Returns a status dict ({"status": "consolidated"|"skipped", ...}), or
    None when another consolidator is already in flight for this
    (entity, scope). NEVER raises — a consolidator failure must leave the
    chat turn exactly as it was (old window, old behavior)."""
    if not src_ctx or not src_ctx.get("kind"):
        return {"status": "skipped", "reason": "no_scope"}
    key = _scope_key(entity_id, src_ctx)
    if key in _INFLIGHT:
        return None
    _last_fail = _LAST_FAIL.get(key)
    if not force and _last_fail is not None and (time.monotonic() - _last_fail) < FAIL_BACKOFF_S:
        return {"status": "skipped", "reason": "backoff",
                "retry_in_s": int(FAIL_BACKOFF_S - (time.monotonic() - _last_fail))}
    _INFLIGHT.add(key)
    _failed = False
    try:
        from mastercode.memory_engine import count_scope_turns, recall_recent_turns

        prev = await get_live_summary(pool, entity_id, src_ctx)
        stats = await count_scope_turns(
            pool, entity_id, session_id, source_context=src_ctx,
            since=(prev or {}).get("created_at"),
        )
        verbatim_tokens = stats["chars"] // 4
        pressure = (
            history_budget_tokens > 0
            and verbatim_tokens > PRESSURE_RATIO * history_budget_tokens
        )
        cadence = stats["count"] >= TURNS_SINCE_TRIGGER
        # The idle-gap trigger went with the post-turn lane. It
        # existed to notice a return-from-idle from INSIDE a live turn, which
        # is a question only the post-turn lane could ask; the consolidation
        # cron sees the same thread every 30 minutes regardless. The topic-flip
        # decision it fed is unaffected — the model still makes it, from the
        # window, on every consolidation.
        if not force and not (pressure or cadence):
            return {
                "status": "skipped", "reason": "below_threshold",
                "verbatim_tokens": verbatim_tokens, "turns_since": stats["count"],
            }

        # The FULL current verbatim window — retained AND evicted turns
        # (corrections live in the newest turns while
        # the stale instruction is what's being evicted; summarizing only
        # evicted turns would fossilize the stale position as current).
        turns = await recall_recent_turns(
            pool, entity_id, session_id,
            limit=WINDOW_TURN_LIMIT, source_context=src_ctx,
        )
        if not turns:
            return {"status": "skipped", "reason": "no_turns"}
        # The Host's words plus a platform note where a turn stopped at its
        # budget or stalled — the summary knows a turn stopped, the same way
        # history does.
        from mastercode.memory_engine import history_messages as _hm
        turns = _hm(turns)

        # Budget the consolidator's OWN input against its model's window:
        # previous summary + newest turns that fit. Newest
        # first because that is where corrections live; what falls off the
        # oldest end is already inside the previous summary. Unknown window
        # → the read cap alone, as before.
        prev_content = (prev or {}).get("content") or ""
        turns, dropped_turns = _fit_turns_to_consolidator_window(
            turns, prev_content, await _consolidator_window(pool, entity_id))
        if dropped_turns:
            log.info("thread consolidator: input budget dropped %d oldest turns (entity=%d scope=%s:%s)",
                     dropped_turns, entity_id, key[1], key[2])

        prev_topic = (prev or {}).get("segment_topic")
        messages = _build_summary_prompt(prev_content, turns, prev_topic=prev_topic)
        fn = model_fn
        if fn is None:
            async def fn(msgs):
                return await _default_model_fn(pool, entity_id, msgs)
        result = await fn(messages)
        parsed = _parse_consolidation_output((result or {}).get("content"))
        content = parsed["summary"]
        if not content.strip():
            _failed = True
            if (result or {}).get("truncated"):
                # Cut off by the model's own output limit before the summary
                # closed: said as a cut, not as an empty answer.
                log.warning(
                    "thread consolidator: summary cut off at the model's output "
                    "limit (%s tokens) before it closed (entity=%d scope=%s:%s)",
                    (result or {}).get("output_limit"), entity_id, key[1], key[2],
                )
                return {"status": "skipped", "reason": "model_cut"}
            log.warning(
                "thread consolidator: empty summary (entity=%d scope=%s:%s): %s",
                entity_id, key[1], key[2], (result or {}).get("error"),
            )
            return {"status": "skipped", "reason": "model_empty"}
        # A flip is only meaningful against an existing segment.
        topic_changed = bool(parsed["topic_changed"] and prev)

        new_id, version = await _store_summary(
            pool, entity_id, session_id, src_ctx, content.strip(), prev,
            turns_covered=len(turns),
            segment_topic=parsed["topic"],
            prev_topic=prev_topic,
            topic_changed=topic_changed,
        )
        log.info(
            "thread consolidator: entity=%d scope=%s:%s summary v%d (id=%s, "
            "supersedes=%s, turns=%d, topic=%s, flip=%s)",
            entity_id, key[1], key[2], version, new_id,
            (prev or {}).get("id"), len(turns),
            parsed["topic"], topic_changed,
        )

        # Janitor pass: curate the scope's captured artifacts
        # and submit kept rows for embedding. Same cheap model, same
        # never-raises posture — a janitor failure must not misreport the
        # consolidation that already landed.
        janitor = None
        try:
            janitor = await _curate_artifacts(pool, entity_id, src_ctx, fn)
        except Exception as _j_err:
            log.warning("artifact janitor failed (non-fatal): %s", _j_err)

        return {
            "status": "consolidated", "summary_id": new_id, "version": version,
            "superseded_id": (prev or {}).get("id"), "turns_covered": len(turns),
            "topic": parsed["topic"], "topic_changed": topic_changed,
            "janitor": janitor,
        }
    except Exception as e:
        # Old behavior, log only — the chat turn must never see this.
        _failed = True
        log.warning(
            "thread consolidator failed (entity=%d scope=%s:%s): %s",
            entity_id, key[1], key[2], e,
        )
        return {"status": "skipped", "reason": "error", "error": str(e)}
    finally:
        _INFLIGHT.discard(key)
        if _failed:
            _LAST_FAIL[key] = time.monotonic()
        else:
            _LAST_FAIL.pop(key, None)
