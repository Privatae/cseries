"""THE recall engine for entity_memory — the single source of truth for
filters, arms, fusion, and degradation.

Every lane routes here — the chat `recall` tool (via federated_recall),
GET /api/brain/memory/semantic, and brain_service.search_memory (which also
backs the external ceigas_memory_search MCP tool) — so the canonical filter
set exists in exactly ONE place. Before this module, four lanes carried four
divergent filter sets and the chat tool surfaced superseded/dismissed rows
(session.py's old _query_rag had no status or bitemporal predicate at all).

Two arms over entity_memory:
  semantic — embed the query with the entity's CONFIGURED model (config-driven,
             BYO/bastion first, then bridge), nearest-neighbour the matching
             dim column.
  lexical  — Postgres FTS over search_tsv (a GENERATED column, v096 — every
             writer covered by one schema-level definition, so the
             forgotten-write-site class user_context suffered v084→v095 is
             structurally impossible here).
RRF-fused, same shape and constant as user_context's search_context_service.

Degradation contract (solid with or without embeddings):
an arm that cannot RUN is reported in the result (`arms`, `degraded`,
per-arm unavailable reasons) and the other arm still answers. RecallUnavailable
raises ONLY when no arm ran — an arm that ran and matched nothing is a real
empty, never an error. The old ILIKE substring fallback is retired.

Audience scoping is FAIL-CLOSED: when a non-handler audience is supplied and
the scope clause cannot be built, the search returns nothing rather than
leaking handler-only memories (the old lane logged and silently widened).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("memory_retrieval")

# Same fusion constant the in-repo hybrid lanes ship (context search / code
# search) — the standard RRF damping term, not a tunable to ask about.
RRF_K = 60

# The one column list every arm selects — callers map from these keys.
_HIT_COLS = (
    "id, content, memory_type, importance, confidence, access_count, status, "
    "metadata, created_at, project_id, "
    "metadata->'source_context'->>'kind' AS sctx_kind, "
    "metadata->'source_context'->>'ref'  AS sctx_ref"
)

# Semantic-only hits below this cosine similarity are suppressed from
# formatted output — but COUNTED and reported, never silently dropped
# (the old lane discarded them invisibly). Lexical hits are never floored.
RELEVANCE_FLOOR = 0.3


def _turn_hit(d: dict) -> dict:
    """A hit as the recall tool and the search route hand it out. A Host
    reply that carried a platform notice (the spend-budget stop line, a stall
    notice) reads as the Host's own words in `content`, and the notice is named
    apart in `platform_notice` — the same split every reader uses
    (memory_engine.host_part). Any other row is untouched."""
    if d.get("memory_type") != "turn":
        return d
    md = d.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except ValueError:
            md = None
    if not isinstance(md, dict):
        return d
    from mastercode.memory_engine import host_part
    host, notes = host_part(d.get("content") or "", md)
    if notes:
        d["content"] = host
        d["platform_notice"] = notes
    return d


class AudienceScopeError(Exception):
    """A non-handler audience was supplied but its scope clause could not be
    built — fail closed, never widen to the full corpus."""


def _read_pool(fallback):
    """The pool every entity_memory READ in this module goes through.

    `host_recall` (migrations/v139) — NOSUPERUSER, NOBYPASSRLS — so a row
    security policy on entity_memory binds these queries. The kernel's own pool
    is a superuser and would ignore one.

    Resolved HERE rather than at the four call sites, and that is the whole
    point: a call site can be written to pass the wrong pool, and the next one
    someone adds will be. There is one lane into this module, so there is one
    place to get it right. Today the two lanes return identical rows (no policy
    exists), so this changes nothing observable.

    Falls back to the pool the caller handed us when the lane is absent — the
    bench (scripts/mem-bench), the standalone gates (scripts/test_memory_*.py)
    and anything running brain/ without server/ all connect for themselves, and
    they must keep working. That fallback stops being acceptable the day a
    policy is installed; see server.brain_service.get_recall_pool.
    """
    try:
        from server import brain_service
        return brain_service.get_recall_pool() or fallback
    except Exception:
        return fallback


def _priv_pool(fallback):
    """The kernel's own lane — the mirror of _read_pool, and it exists for one
    reachable case: a caller that passes no pool at all.

    `recall_memories(None, …)` inside a running server used to leave the
    privileged half as None, and `priv_pool or pool` in the arms below would
    then resolve it to the RESTRICTED pool — putting the vault broker and the
    AGE cypher straight back on host_recall, the exact hole the split closes.
    Falling
    back to brain_service.get_pool() (server/brain_service.py:293, returns the
    module-global `_pool`, None before initialize()) keeps the two halves from
    ever collapsing into one. Outside a server this is None, which is what the
    bench and the standalone gates already handle."""
    try:
        from server import brain_service
        return fallback or brain_service.get_pool()
    except Exception:
        return fallback


# The scope a row is read in: what the Host set it to be ABOUT
# (metadata.about, written by memory_scope — {kind: global} or {kind, ref})
# when it has one, else where it was said (metadata.source_context). The two
# are kept apart on purpose: source_context is provenance, and thread delete,
# split and project adoption move or forget rows by it; `about` is the Host's
# decision and nothing but memory_scope (and a restatement inheriting it)
# writes it. No scope at all = global.
#
# A row nobody has scoped is read wherever it was SAID: its own conversation
# and every conversation that said it again (metadata.held_in, written by
# MemoryConsolidator.note_held and kept current by thread delete, split and
# adoption). An entry for a conversation with no scope
# (memory_consolidation.UNSCOPED) makes it global. A decision (about)
# replaces all of that; it is what keeps a preference's reach when it is
# restated somewhere narrower.
#
# A held_in entry that carries its own `about` is PROVENANCE ONLY
# (MemoryConsolidator.note_held's `about`): a conversation said the words about
# a place the row is read in already. Reading skips it (HELD_IN_READS); the
# thread delete does not (circles_threads._keep_held, _forget).
EFFECTIVE_SCOPE = "COALESCE(metadata->'about', metadata->'source_context')"
UNSCOPED_JSON = '{"kind": "app", "ref": "memory"}'
_UNDECIDED = "NOT (COALESCE(metadata, '{}'::jsonb) ? 'about')"
# The held_in entries (as `e`) scope reading counts.
HELD_IN_READS = "NOT (e ? 'about')"


def _held_in_elems(m: str = "metadata") -> str:
    return (f"jsonb_array_elements(CASE WHEN jsonb_typeof({m}->'held_in') = 'array' "
            f"THEN {m}->'held_in' ELSE '[]'::jsonb END)")


# A held_in entry the person's own "Remember this:" wrote (note_held's
# `taught`; the first teaching writes one for its own conversation too). On a
# rule the Host decided about a place, only these widen it: where the person
# taught it, the rule rides; where the Host or a writer merely said it again,
# it does not.
HELD_IN_TAUGHT = "(e ? 'taught')"


def _said_anywhere(entry_sql: str, m: str = "metadata", *, taught_only: bool = False) -> str:
    """A reading held_in entry names the place `entry_sql` (a jsonb object
    with kind and ref). `taught_only`: one the person taught it in."""
    pred = f"{HELD_IN_READS} AND {HELD_IN_TAUGHT}" if taught_only else HELD_IN_READS
    return (f"EXISTS (SELECT 1 FROM {_held_in_elems(m)} e "
            f"WHERE {pred} AND e @> {entry_sql})")


def scope_global_sql() -> str:
    unscoped = "'" + UNSCOPED_JSON + "'::jsonb"
    return (f"({EFFECTIVE_SCOPE} IS NULL OR {EFFECTIVE_SCOPE}->>'kind' = 'global' "
            f"OR ({_UNDECIDED} AND {_said_anywhere(unscoped)}))")


def scope_match_sql(kind_param: int, ref_param: int) -> str:
    entry = (f"jsonb_build_object("
             f"'kind', ${kind_param}::text, 'ref', ${ref_param}::text)")
    return (f"(({EFFECTIVE_SCOPE}->>'kind' = ${kind_param} "
            f"AND {EFFECTIVE_SCOPE}->>'ref' = ${ref_param}) "
            f"OR ({_UNDECIDED} AND {_said_anywhere(entry)}))")


def place_key_sql(alias: str) -> str:
    """One jsonb value per set of turns a row is read in, for the types a turn
    reads by place (preference, goal). It is built from the three things a
    turn's read of such a row filters on (_canonical_where):
      - the turn scope: the decision (about), else where it was said plus
        held_in — read as scope_global_sql / scope_match_sql read them, with
        "everywhere" for a row read in every turn;
      - a typed turn's project filter: the project tag, plus the project a
        decision names (it admits a row by either), or "any" for a decision
        about the person;
      - the audience's share scope.
    Two rows with the same key are read in the same turns, so folding one
    into the other takes nothing from anywhere; equal keys are an
    equivalence, so a chain of folds or several rows folded into one cannot
    either. The key follows how a row was stored, so two rows read in the
    same turns can still differ: it only ever errs toward folding less.
    Not in the key: metadata.visibility_scope, cache_state and
    metadata.source, which the standing list and the pre-fire also read. No
    code in this repo writes any of them on a preference or a goal, and none
    of the 415 preference and goal rows on the eight live Hosts carried one
    (measured); a writer that starts to belongs in the key."""
    m = f"{alias}.metadata"
    eff = f"COALESCE({m}->'about', {m}->'source_context')"
    has_about = f"(COALESCE({m}, '{{}}'::jsonb) ? 'about')"
    undecided = f"NOT {has_about}"
    unscoped = "'" + UNSCOPED_JSON + "'::jsonb"
    everywhere = (f"({eff} IS NULL OR {eff}->>'kind' = 'global' OR ({undecided} "
                  f"AND {_said_anywhere(unscoped, m)}))")
    decided = (f"CASE WHEN {m}->'about'->>'kind' IS NOT NULL AND {m}->'about'->>'ref' IS NOT NULL "
               f"THEN jsonb_build_array(jsonb_build_object("
               f"'kind', {m}->'about'->>'kind', 'ref', {m}->'about'->>'ref')) "
               f"ELSE '[]'::jsonb END")
    said = (f"(SELECT COALESCE(jsonb_agg(DISTINCT p ORDER BY p), '[]'::jsonb) FROM ("
            f"SELECT jsonb_build_object('kind', {m}->'source_context'->>'kind', "
            f"'ref', {m}->'source_context'->>'ref') AS p "
            f"WHERE {m}->'source_context'->>'kind' IS NOT NULL "
            f"AND {m}->'source_context'->>'ref' IS NOT NULL "
            f"UNION ALL SELECT jsonb_build_object('kind', e->>'kind', 'ref', e->>'ref') "
            f"FROM {_held_in_elems(m)} e "
            f"WHERE jsonb_typeof(e->'kind') = 'string' AND jsonb_typeof(e->'ref') = 'string' "
            f"AND {HELD_IN_READS}) s)")
    # A rule's places are its REACH (memory_engine.rule_reach): everywhere
    # when undecided or decided global, else its decided place and every
    # place the person taught it — never where it was merely said again.
    from mastercode.memory_engine import rule_row_sql
    is_rule = rule_row_sql(alias)
    taught = (f"(SELECT COALESCE(jsonb_agg(DISTINCT p ORDER BY p), '[]'::jsonb) FROM ("
              f"SELECT jsonb_build_object('kind', {m}->'about'->>'kind', 'ref', {m}->'about'->>'ref') AS p "
              f"WHERE {m}->'about'->>'kind' IS NOT NULL AND {m}->'about'->>'ref' IS NOT NULL "
              f"UNION ALL SELECT jsonb_build_object('kind', e->>'kind', 'ref', e->>'ref') "
              f"FROM {_held_in_elems(m)} e "
              f"WHERE jsonb_typeof(e->'kind') = 'string' AND jsonb_typeof(e->'ref') = 'string' "
              f"AND {HELD_IN_READS} AND {HELD_IN_TAUGHT}) s)")
    places = (f"CASE WHEN {is_rule} AND ({undecided} OR {m}->'about'->>'kind' = 'global') "
              f"THEN '\"everywhere\"'::jsonb "
              f"WHEN {is_rule} THEN {taught} "
              f"WHEN {everywhere} THEN '\"everywhere\"'::jsonb "
              f"WHEN {has_about} THEN {decided} ELSE {said} END")
    # A typed turn reads a row by its project tag, or by the project its
    # decision names; a decision about the person passes whatever the tag.
    # The two ids as one sorted, de-duplicated text array, with no subquery
    # (the pair query evaluates this per row per pair).
    tag = f"{alias}.project_id::text"
    named = f"({m}->'about'->>'ref')"
    names_project = f"({m}->'about'->>'kind' = 'project' AND {named} IS NOT NULL)"
    project = (f"CASE WHEN {m}->'about'->>'kind' = 'global' THEN '\"any\"'::jsonb "
               f"WHEN {names_project} AND ({alias}.project_id IS NULL OR {tag} = {named}) "
               f"THEN jsonb_build_array({named}) "
               f"WHEN {names_project} AND {tag} < {named} THEN jsonb_build_array({tag}, {named}) "
               f"WHEN {names_project} THEN jsonb_build_array({named}, {tag}) "
               f"WHEN {alias}.project_id IS NOT NULL THEN jsonb_build_array({tag}) "
               f"ELSE '[]'::jsonb END")
    return (f"jsonb_build_object('places', {places}, 'project', {project}, "
            f"'share', {m}->'share_scope')")


def project_rows_sql(n: int, alias: str = "") -> str:
    """The rows a project-scoped typed turn reads (turn types),
    its project ids bound as $n: tagged with one of them (the `project_id`
    column, the tag the conversation scope reads), or set by the Host to be
    about one of them, or about the person (global: it rides here as
    anywhere). The one test: the pre-fire (_canonical_where) and the
    taught-rule readers (memory_engine) both apply it."""
    a = f"{alias}." if alias else ""
    return (f"({a}project_id = ANY(${n}::int[]) OR "
            f"({a}metadata->'about'->>'kind' = 'project' "
            f"AND {a}metadata->'about'->>'ref' = ANY(${n}::text[])) OR "
            f"{a}metadata->'about'->>'kind' = 'global')")


def _canonical_where(entity_id: int, *, memory_types=None,
                     include_superseded: bool = False,
                     audience=None, project_ids=None,
                     turn_scope=None, scope_everywhere_types=None,
                     exclude_rules: bool = False) -> tuple[str, list]:
    """The ONE filter truth: entity + live-status + bitemporal window
    (+ optional type filter + audience scope + project scope + turn scope).
    Returns (where_sql, params) with placeholders starting at $1.

    exclude_rules: leave out every rule (memory_engine
    rule_row_sql, both shapes). The pre-fire passes it: rules ride in "Things
    You Were Taught" by weight, so a rule the message matches must not take a
    pre-fire slot before its limit is applied. The `recall` tool does not: it
    finds rules like any other row.

    project_ids (turn types): a typed turn whose context names
    projects reads only rows tagged with one of them — the `project_id`
    column, the same tag the conversation scope reads. None = no clause.

    turn_scope (what is relevant in one place is a leak in another): when given as a dict — {kind, ref}, or {} for "no scope
    known" — or a list of them, a row is read only if its EFFECTIVE_SCOPE is
    global, or exactly one of these scopes, or it is of a type in
    scope_everywhere_types. None = no clause (the `recall` tool, the
    search route and every caller that is not volunteering memory to a turn).
    This clause is the only scope check: every recall arm reads through it."""
    clauses = ["entity_id = $1", "status = 'active'"]
    params: list = [entity_id]
    if not include_superseded:
        clauses.append("t_valid_until IS NULL")
    if memory_types:
        params.append(list(memory_types))
        clauses.append(f"memory_type = ANY(${len(params)}::text[])")
    if exclude_rules:
        from mastercode.memory_engine import rule_row_sql
        clauses.append(f"NOT {rule_row_sql()}")
    if project_ids is not None:
        params.append([int(p) for p in project_ids])
        clauses.append(project_rows_sql(len(params)))
    if turn_scope is not None:
        ors = [scope_global_sql()]
        for sc in (turn_scope if isinstance(turn_scope, (list, tuple)) else [turn_scope]):
            kind = str((sc or {}).get("kind") or "")
            if not kind:
                continue
            params.append(kind)
            k = len(params)
            params.append(str(sc.get("ref") or ""))
            r = len(params)
            ors.append(scope_match_sql(k, r))
        if scope_everywhere_types:
            params.append(list(scope_everywhere_types))
            ors.append(f"memory_type = ANY(${len(params)}::text[])")
        clauses.append("(" + " OR ".join(ors) + ")")
    if audience is not None:
        try:
            from server.applications.memory_scope import filter_clause
            clause, extra = filter_clause(audience, next_param_index=len(params) + 1)
        except Exception as e:
            raise AudienceScopeError(f"audience scope clause failed: {e}") from e
        if clause:
            clauses.append(clause)
            params.extend(extra)
    return " AND ".join(clauses), params


async def _embed_query(pool, entity_id, query) -> tuple[Optional[str], Optional[str]]:
    """Embed the query ONCE with the entity's configured model. Returns
    (vec, why_not) — vec is None with a quoted reason when the lane can't
    answer. Shared by the semantic arm and the graph scorer so a turn never
    pays for two embeddings of the same text. Never raises.

    `pool` MUST be the PRIVILEGED lane, and that is not a convenience. Nothing
    below this line reads an entity_memory row: it reads
    entity_registry.behavioral_params (memory_engine._load_behavioral_params,
    _byo_embedding_config) and, when embedding_cred_name is set, brokers a
    sealed credential — memory_engine:517 `_authz.use_credential(_pool, …)` →
    server/vault/api.py `open_secret` → server/vault/store.py `get_envelope`,
    and then on to the host key in `kernel_secrets` to actually open it. Neither
    of those is a row a memory policy should govern.

    WHERE THE WALL IS, AND WHERE IT MOVED. Until then
    `get_envelope` opened with `ensure_schema(pool)` — `CREATE TABLE IF NOT
    EXISTS vault_envelope` — so a restricted pool blew up immediately and
    loudly, because PostgreSQL checks permission BEFORE existence (measured on
    17.10 against a table that already existed: InsufficientPrivilegeError,
    permission denied for schema public). That DDL is gone; the table is
    declared in init-db.sql and created at boot.

    So the tripwire moved one statement later rather than disappearing, and it
    is worth naming the RIGHT statement. Measured as host_recall against the
    declared schema:

        SELECT ... FROM vault_envelope        -> succeeds, 0 rows (ciphertext)
        INSERT INTO vault_audit ...           -> permission denied
        SELECT ... FROM kernel_secrets        -> permission denied

    `get_envelope` audits unconditionally (server/vault/store.py:101 -> _audit
    at :26-33, a bare INSERT INTO vault_audit), so a restricted pool now fails
    THERE — one statement before the key read, not at it. Two helpers that
    neither audit nor need the key, `cred_type_of` and `list_names`, now succeed
    on a restricted pool where they used to raise. That is not new privilege —
    v139's blanket GRANT SELECT already allowed the raw read — but the kernel's
    own helpers no longer refuse when handed the wrong pool. Ciphertext without
    the key is not a leak; it IS a quieter failure than this contract used to
    get for free.

    What actually holds the contract is therefore the test, not the DDL:
    tests/test_bastion_control_pool_reserved.py asserts the lane at each call
    site hermetically, with recorders, and never needed the DDL to do it.

    And the failure is worse than a dead arm, which is why this is a hard
    contract and not a preference. _embed_via_byo swallows the error and returns
    None (memory_engine:543-548); _embed_text_config_driven:890 then calls
    _embed_mark_down, which sets the MODULE-GLOBAL _embed_down_until
    (memory_engine:714-723, 60s). Every embed caller in the process is down for
    that window — memory WRITES included, on the privileged pool that never
    touched the restricted lane. Meanwhile embedding_health() reports it below
    as "embedding endpoint not answering", so a host whose provider is perfectly
    fine tells its user the provider is down."""
    from mastercode.memory_engine import (
        _get_memory_embedding, embed_config_unreadable, embedding_health,
        last_embed_model,
    )
    try:
        vec = await _get_memory_embedding(query, pool=pool, entity_id=entity_id)
    except Exception as e:
        return None, f"query embedding failed: {e}"
    if not vec:
        # Order matters: UNCONFIGURED is checked FIRST. There is no platform
        # embedder — setting embeddings up is the user's step — so a host with
        # no model must read as "not set up yet", never as an outage. Checking
        # the health gate first produced "embedding lane down (model=:
        # circuit_open)" on a fresh host. The reason string is
        # host-facing and gets repeated to the user: keep it a setup prompt.
        # But an empty model from a FAILED config read is not a setup step —
        # never say "nothing is broken" over a DB error.
        if not last_embed_model() and embed_config_unreadable():
            return None, ("embedding configuration could not be read just now "
                          "(a transient error, not a setup problem) — retry")
        if not last_embed_model():
            return None, ("embeddings not set up yet — this host has no embedding "
                          "model. Nothing is broken: one can be set up on a machine "
                          "the user runs (embedding_route op=bastions) or with a "
                          "provider they hold a key for (Orchestrator → Memory)")
        health = embedding_health()
        if health.get("down"):
            return None, (f"embedding endpoint not answering "
                          f"({health.get('reason') or 'unknown'})")
        return None, "query embedding unavailable"
    return vec, None


async def _vector_arm(pool, entity_id, query, *, where_sql, params,
                      candidates, vec=None,
                      priv_pool=None) -> tuple[list[dict], bool, Optional[str]]:
    """Semantic arm. Returns (hits, ran, why_not). Never raises. Pass `vec`
    (from _embed_query) to reuse an embedding already computed this turn.

    `pool` reads entity_memory and is the restricted lane inside a running
    server. `priv_pool` is the privileged lane for the two things here that are
    NOT entity_memory reads: the re-embed fallback below (see _embed_query's
    contract — it can reach vault DDL) and the behavioral_params read that picks
    which dim column to search. Defaults to `pool` so the bench and the
    standalone gates, which have one pool and no server, are unchanged."""
    from mastercode.memory_engine import (
        active_embedding_column, embedding_cast_type,
    )
    priv_pool = priv_pool or pool
    if vec is None:
        # The re-embed fallback. It runs whenever the caller's own _embed_query
        # returned None, so routing the caller's call privileged and leaving
        # this one restricted would re-enter the identical path on the identical
        # turn and land in the identical permission error.
        vec, why = await _embed_query(priv_pool, entity_id, query)
        if vec is None:
            return [], False, why
    try:
        content_col, dim = await active_embedding_column(priv_pool, entity_id)
        cast = embedding_cast_type(dim)
        n = len(params)
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL ivfflat.probes = 10")
                rows = await conn.fetch(
                    f"""
                    SELECT {_HIT_COLS},
                           1 - ({content_col} <=> ${n + 1}::{cast}) AS relevance
                    FROM entity_memory
                    WHERE {where_sql} AND {content_col} IS NOT NULL
                    ORDER BY {content_col} <=> ${n + 1}::{cast}, id
                    LIMIT ${n + 2}
                    """,
                    *params, vec, candidates,
                )
    except Exception as e:
        return [], False, f"vector search failed: {e}"
    hits = []
    for r in rows:
        d = _turn_hit(dict(r))
        d["relevance"] = round(float(d["relevance"]), 4)
        hits.append(d)
    return hits, True, None


async def _lexical_arm(pool, entity_id, query, *, where_sql, params,
                       candidates) -> tuple[list[dict], bool, Optional[str]]:
    """FTS arm over search_tsv. Returns (hits, ran, why_not). A pod whose
    schema predates v096 simply reports the arm unavailable. Never raises."""
    n = len(params)
    # The Host's aliases (memory_curate op='alias', migrations/v234) match
    # beside the row's own text: words recall should have found it by. A
    # schema without the table (a pod before v234) searches the text alone.
    with_aliases = f"""
        SELECT {_HIT_COLS}, GREATEST(ts_rank(search_tsv, q), COALESCE(al.alias_rank, 0)) AS lex_rank
        FROM entity_memory
             CROSS JOIN plainto_tsquery('english', ${n + 1}) AS q
             LEFT JOIN LATERAL (
                 SELECT max(ts_rank(a.alias_tsv, q)) AS alias_rank
                   FROM memory_recall_aliases a
                  WHERE a.memory_id = entity_memory.id AND a.alias_tsv @@ q
             ) al ON true
        WHERE {where_sql} AND (search_tsv @@ q OR al.alias_rank IS NOT NULL)
        ORDER BY lex_rank DESC, id
        LIMIT ${n + 2}
        """
    text_only = f"""
        SELECT {_HIT_COLS}, ts_rank(search_tsv, q) AS lex_rank
        FROM entity_memory, plainto_tsquery('english', ${n + 1}) AS q
        WHERE {where_sql} AND search_tsv @@ q
        ORDER BY lex_rank DESC, id
        LIMIT ${n + 2}
        """
    try:
        try:
            rows = await pool.fetch(with_aliases, *params, query, candidates)
        except Exception as e:
            if "memory_recall_aliases" not in str(e):
                raise
            rows = await pool.fetch(text_only, *params, query, candidates)
    except Exception as e:
        return [], False, f"text search unavailable: {e}"
    hits = []
    for r in rows:
        d = _turn_hit(dict(r))
        d["lex_rank"] = round(float(d["lex_rank"]), 6)
        hits.append(d)
    return hits, True, None


def _rrf_fuse(vector_hits: list[dict], lexical_hits: list[dict],
              k: int = RRF_K) -> list[dict]:
    """Reciprocal-rank fusion by row id; a row hit by both arms keeps every
    signal (relevance + lex_rank) and matched lists each arm in order
    semantic, lexical."""
    scores: dict[int, float] = {}
    merged: dict[int, dict] = {}
    arms = [("semantic", vector_hits, 1.0), ("lexical", lexical_hits, 1.0)]
    for arm, hits, w in arms:
        for rank, h in enumerate(hits):
            hid = h["id"]
            scores[hid] = scores.get(hid, 0.0) + w / (k + rank + 1)
            if hid in merged:
                merged[hid].update({kk: vv for kk, vv in h.items()
                                    if merged[hid].get(kk) is None})
                merged[hid]["matched"].append(arm)
            else:
                d = dict(h)
                d["matched"] = [arm]
                merged[hid] = d
    out = []
    for hid in sorted(scores, key=lambda i: scores[i], reverse=True):
        d = merged[hid]
        d["_rrf"] = scores[hid]
        out.append(d)
    return out


def _composite_order(hits: list[dict]) -> list[dict]:
    """P3 ranking blend — retrieval signal dominates; importance and recency
    are secondary, the weighting production memory systems converge on
    (Zep/Mem0-class: relevance-first, ~10–25% metadata boost):

        final = 0.75·rrf_norm + 0.15·importance_norm + 0.10·recency

    importance_norm = min(importance, 2)/2 (schema range ≈0.5–1.5, pattern
    rows 1.5; 2 caps outliers). recency = exp(-age_days/45) — about half
    weight at six weeks, matching the consolidation decay horizon. NO
    personality vector. Deterministic: ties break by
    id (newest first)."""
    if not hits:
        return hits
    max_rrf = max((h.get("_rrf") or 0.0) for h in hits) or 1.0
    now = datetime.now(timezone.utc)

    def _key(h):
        rrf = (h.get("_rrf") or 0.0) / max_rrf
        imp = min(float(h.get("importance") or 1.0), 2.0) / 2.0
        rec = 0.0
        try:
            age_days = max((now - h["created_at"]).total_seconds() / 86400.0, 0.0)
            rec = math.exp(-age_days / 45.0)
        except Exception:
            pass
        return (0.75 * rrf + 0.15 * imp + 0.10 * rec, h.get("id") or 0)

    return sorted(hits, key=_key, reverse=True)


async def recall_memories(pool, entity_id: int, query: str, *, limit: int = 15,
                          memory_types=None, include_superseded: bool = False,
                          audience=None, project_ids=None,
                          turn_scope=None, scope_everywhere_types=None,
                          exclude_rules: bool = False, skip_words=None) -> dict:
    """Hybrid recall over entity_memory — THE entry point for every lane.
    `exclude_rules`: see _canonical_where (the pre-fire's; False finds rules).
    `skip_words` (the pre-fire's): same_words_key keys already in the turn —
    the words of the rules its taught block carries. A hit in those words,
    or in a hit's above it, is dropped BEFORE the limit, so the limit counts
    distinct words the turn does not carry yet. None: no such dedup.

    Returns {hits, arms, degraded, semantic_unavailable, lexical_unavailable}.
    Raises RecallUnavailable only when NO arm could run (both reasons quoted);
    raises AudienceScopeError when a supplied audience can't be scoped
    (fail closed). A blank query returns the empty shape.

    `pool` is the FALLBACK, not the lane. Inside a running server every
    entity_memory row this function returns is read over the restricted
    `host_recall` connection (_read_pool), so a row security policy governs the
    answer whether or not the caller remembered anything.

    THE LINE THE TWO LANES ARE DRAWN ON: a read of an entity_memory ROW goes on
    the restricted lane; everything else — the host's own CONFIG, the AGE
    cypher, brokering a credential — stays on the privileged one. Not because
    those are "trusted", but because they are not rows a memory policy is about,
    and putting them on the restricted lane buys nothing while breaking two
    things: the AGE extension refuses to LOAD for a non-superuser, and the vault
    broker executes DDL (see _embed_query)."""
    from mastercode.memory_engine import RecallUnavailable

    # The privileged pool stays in hand for everything that is not an
    # entity_memory row read; only the row reads move to the restricted lane.
    priv_pool = _priv_pool(pool)
    pool = _read_pool(pool)

    query = (query or "").strip()
    limit = max(1, min(int(limit or 15), 100))
    if not query:
        return {"hits": [], "arms": [], "degraded": False,
                "semantic_unavailable": None, "lexical_unavailable": None}

    where_sql, params = _canonical_where(
        entity_id, memory_types=memory_types,
        include_superseded=include_superseded, audience=audience,
        project_ids=project_ids, turn_scope=turn_scope,
        scope_everywhere_types=scope_everywhere_types, exclude_rules=exclude_rules)
    candidates = max(limit, 30)

    # One embedding per turn.
    # PRIVILEGED on purpose — _embed_query reads config and may broker a vault
    # credential, which is DDL. Its docstring carries the measurement.
    vec, vec_why = await _embed_query(priv_pool, entity_id, query)
    vec_hits, vec_ran, vec_why2 = await _vector_arm(
        pool, entity_id, query,
        where_sql=where_sql, params=params, candidates=candidates, vec=vec,
        priv_pool=priv_pool)
    vec_why = vec_why2 or vec_why
    lex_hits, lex_ran, lex_why = await _lexical_arm(
        pool, entity_id, query,
        where_sql=where_sql, params=params, candidates=candidates)

    if not vec_ran and not lex_ran:
        raise RecallUnavailable(
            f"no search arm could run — semantic: {vec_why}; lexical: {lex_why}")

    arms = ([a for a, ran in (("semantic", vec_ran), ("lexical", lex_ran)) if ran])
    if not vec_ran:
        log.warning("memory recall degraded to lexical-only: %s", vec_why)

    fused_all = _composite_order(_rrf_fuse(vec_hits, lex_hits))
    if skip_words is not None:
        from mastercode.memory_engine import same_words_key
        seen, distinct = set(skip_words), []
        for h in fused_all:
            key = same_words_key(h.get("content"))
            if key in seen:
                continue
            seen.add(key)
            distinct.append(h)
        fused_all = distinct
    fused = fused_all[:limit]

    for h in fused:
        h.pop("_rrf", None)
    return {
        "hits": fused,
        "arms": arms,
        "degraded": not vec_ran,
        "semantic_unavailable": vec_why,
        "lexical_unavailable": lex_why,
    }


async def verify_retrievable(pool, entity_id: int, memory_id: int) -> dict:
    """The write→retrievable probe: a write that cannot be recalled must fail LOUD at write
    time, never read as absence days later. Checks the row against the SAME
    canonical filters every recall lane uses, plus per-arm reachability.
    retrievable = reachable AND lexically indexed — the lexical arm needs no
    model, so it is the floor; embedding state is reported, not required
    (a missing embed backfills via the unembedded sweep)."""
    where_sql, params = _canonical_where(entity_id)
    n = len(params)
    try:
        row = await pool.fetchrow(
            f"""SELECT (search_tsv IS NOT NULL) AS lexically_indexed,
                       (embedding IS NOT NULL OR embedding_768 IS NOT NULL
                        OR embedding_2048 IS NOT NULL
                        OR embedding_2560 IS NOT NULL
                        OR embedding_4096 IS NOT NULL) AS embedded
                FROM entity_memory
                WHERE {where_sql} AND id = ${n + 1}""",
            *params, int(memory_id))
    except Exception as e:
        log.warning("write→retrievable probe errored for memory %s: %s",
                    memory_id, e)
        return {"retrievable": False, "reachable": False, "error": str(e)}
    if row is None:
        return {"retrievable": False, "reachable": False,
                "note": "row not reachable through the canonical recall filters"}
    out = {"retrievable": bool(row["lexically_indexed"]), "reachable": True,
           "lexically_indexed": bool(row["lexically_indexed"]),
           "embedded": bool(row["embedded"])}
    if not out["retrievable"]:
        log.warning("write→retrievable probe FAILED for memory %s: %s",
                    memory_id, out)
    return out


# ── The one front door (P1b): memory + the user's documents ─────────────────

async def federated_recall(pool, entity_id: int, query: str, *,
                           scope: str = "all", limit: int = 10,
                           offset: int | None = None,
                           context: dict | None = None,
                           memory_types=None,
                           turn_scope=None, scope_everywhere_types=None,
                           exclude_rules: bool = False, skip_words=None) -> dict:
    """ONE search across the stores the host can see — its own memory and the
    user's uploaded documents — each store searched by its own engine, each
    honest about availability. Raises RecallUnavailable only when EVERY
    searched store was unavailable; a store that ran and matched nothing is
    a real empty.

    `context` (turn types; runtime/turn_types.context_filter):
    what a TYPED turn may read — {"projects": [...] | None, "documents":
    bool, "memory": "all" | "projects" | "none"}. It only ever narrows the
    caller's `scope`: a store the type does not read is not searched and is
    reported under `narrowed`, so an empty answer says why. None = today.

    `memory_types` narrows the memory store's search to those types BEFORE
    its limit is applied (the pre-fire passes PROMPT_MEMORY_TYPES, so a
    matching preference is not crowded out by facts and turns it would drop).
    None searches every type — the `recall` tool's call.

    `turn_scope` / `scope_everywhere_types`: see _canonical_where — the
    pre-fire passes the turn's scope so a preference said about one project
    never rides a turn in another. None = unscoped (the `recall` tool).

    `exclude_rules`: see _canonical_where. The pre-fire passes True; the
    `recall` tool leaves it False and finds rules. `skip_words`: see
    recall_memories (the pre-fire's).

    `limit` + `offset` are one PAGE per store:
    each store is asked for limit+offset hits and the first `offset` are
    dropped, and `out["page"]` says per store whether a full page came back —
    i.e. whether more may match — so the recall tool can say "read on with
    offset=N" instead of the host never seeing hit 11."""
    from mastercode.memory_engine import RecallUnavailable

    scope = (scope or "all").strip().lower()
    if scope not in ("all", "memory", "documents"):
        scope = "all"
    try:
        limit = max(1, int(limit or 10))
    except (TypeError, ValueError):
        limit = 10
    # A caller that passes an offset can page; one that does not (None) is
    # told "more may match" without being promised an offset it cannot send.
    pageable = offset is not None
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    fetch = limit + offset
    out: dict = {"query": query, "scope": scope}
    unavailable = []
    narrowed: list[str] = []
    _mem_scope = (context or {}).get("memory", "all") if context else "all"
    _project_ids = (context or {}).get("projects") if _mem_scope == "projects" else None
    if _project_ids is not None and turn_scope is not None:
        # A type that reads named projects reads their rows from wherever the
        # turn runs: those projects join the turn's own scope.
        turn_scope = ([turn_scope] if isinstance(turn_scope, dict) else list(turn_scope)) + [
            {"kind": "project", "ref": str(int(p))} for p in _project_ids]
    _docs_ok = bool((context or {}).get("documents", True)) if context else True

    if scope in ("all", "memory"):
        if _mem_scope == "none":
            narrowed.append("memory: this turn's type reads no memory")
        else:
            try:
                out["memory"] = await recall_memories(
                    pool, entity_id, query, limit=fetch, project_ids=_project_ids,
                    memory_types=memory_types, turn_scope=turn_scope,
                    scope_everywhere_types=scope_everywhere_types,
                    exclude_rules=exclude_rules, skip_words=skip_words)
            except RecallUnavailable as e:
                out["memory"] = {"unavailable": str(e)}
                unavailable.append(f"memory: {e}")

    if scope in ("all", "documents") and not _docs_ok:
        narrowed.append("documents: this turn's type does not read the documents store")
    if narrowed:
        out["narrowed"] = narrowed
    if scope in ("all", "documents") and _docs_ok:
        try:
            from server.routes.context import search_context_service
            out["documents"] = await search_context_service(
                pool, entity_id, query=query, limit=fetch)
            if out["documents"].get("error"):
                out["documents"] = {"unavailable": out["documents"]["error"]}
                unavailable.append(f"documents: {out['documents']['unavailable']}")
        except Exception as e:
            out["documents"] = {"unavailable": str(e)}
            unavailable.append(f"documents: {e}")

    searched = [k for k in ("memory", "documents") if k in out]
    if searched and len(unavailable) == len(searched):
        raise RecallUnavailable("; ".join(unavailable))
    page: dict = {"limit": limit, "offset": offset, "pageable": pageable}
    for store, key in (("memory", "hits"), ("documents", "results")):
        got = out.get(store)
        if not isinstance(got, dict) or got.get("unavailable"):
            continue
        rows = list(got.get(key) or [])
        page[store] = {"more": len(rows) >= fetch}
        if offset:
            got[key] = rows[offset:]
    out["page"] = page
    return out


# ── The one index: what the host sees, plus the rest ────────────
#
# A single index surface for finding context, files and the rest: one
# search a PERSON (Context, Circles) or an APP (POST /api/search, scope
# read:search) runs, over the same stores the host runs its per-turn pre-fire
# over, and then the stores the pre-fire does not touch. The first two go
# through federated_recall UNCHANGED — same engine, same filters, same fusion,
# and the same RELEVANCE_FLOOR on semantic-only hits (memory_engine.py:3135,
# :3154) — so a hit the person sees is a hit the host's recall would have
# surfaced. What the pre-fire does ON TOP of that and this does NOT (stated
# so the copy never overclaims): the per-model visibility filter
# (no filter: those scopes hide a fact from a MODEL, never from the
# owner), the PROMPT_MEMORY_TYPES narrowing that shows only preferences and the
# persona's relationship notes (a person searching wants every matching type), and the 6-item / char-budget
# cut (a search box has `limit`). The others are the existing per-store
# engines (projects.py, brain.py, codesearch, and find_tools' own skill
# entries and BM25 for skills), called as-is: this function
# adds no ranking of its own across stores, because a cosine over memory and
# an FTS rank over a project name are not comparable numbers and pretending
# otherwise is how a search lies. Each store answers under its own section,
# honest about availability, and a store that ran and matched nothing is a
# real empty.

# `skills`: the Host's own active skills and the platform's. A
# Host wrote skills/port after one job and, on the next job of that kind,
# searched recall and never learned the skill existed: skills were reachable
# only through find_tools, and only if the Host thought to look there.
# `world`: what the Host built in its own schema, by name and
# purpose line (mastercode/world_recall).
SEARCH_STORES: tuple[str, ...] = ("memory", "documents", "projects", "code", "skills", "world")


class UnknownSearchStore(ValueError):
    """A `stores` name outside SEARCH_STORES. Raised, not silently widened: a
    typo ("projcts") must not quietly fan out over every store."""


def _apply_relevance_floor(hits: list[dict], *, store: str) -> tuple[list[dict], int]:
    """The pre-fire's own cut, per store exactly as it makes it: a MEMORY hit
    that neither the lexical nor the graph arm found is floored on `relevance`
    (memory_engine.py:3134-3138); a DOCUMENT hit matched by the semantic arm
    alone — `matched == ["semantic"]`, so a media-arm hit is never floored on
    a text cosine — is floored on `similarity` (memory_engine.py:3154-3157).
    Returns (kept, suppressed_count): counted and reported, never silently
    dropped, the contract recall_memories keeps for the chat tool."""
    kept: list[dict] = []
    suppressed = 0
    for h in hits:
        matched = h.get("matched") or []
        if store == "memory":
            floor_it = "lexical" not in matched and "graph" not in matched
            rel = h.get("relevance")
        else:
            floor_it = matched == ["semantic"]
            rel = h.get("similarity")
        if floor_it and (rel is None or rel < RELEVANCE_FLOOR):
            suppressed += 1
            continue
        kept.append(h)
    return kept, suppressed

# Hard ceiling per store. The pre-fire bounds its whole federated recall at 4s
# because a hanging embed lane must never stall a chat turn; a person waiting
# on a search box tolerates more, but not a minute — the embed lane's own
# timeout × retries is exactly that (memory_engine.py:3113).
SEARCH_STORE_TIMEOUT_S = 8.0


def _unavailable(reason) -> dict:
    return {"unavailable": str(reason)}


async def _search_projects_store(pool, entity_id: int, query: str, limit: int) -> dict:
    from server.routes.projects import search_projects_service
    r = await search_projects_service(pool, entity_id, query, limit=limit)
    return {"results": r.get("projects") or [], "semantic": bool(r.get("semantic"))}


async def _search_world_store(pool, entity_id: int, query: str, limit: int) -> dict:
    """What the Host built in its own schema `world`, by name and purpose
    (mastercode/world_recall.py)."""
    from mastercode import world_recall
    return await world_recall.search(pool, entity_id, query, limit)


async def _search_code_store(query: str, limit: int) -> dict:
    """Every project the in-pod code index holds, searched at once. The
    codesearch route takes ONE project per call (its tab has a picker); a
    person typing into the one box has no picker, so this fans out over the
    indexed projects. `projects` names what was searched so an empty answer
    can say 'nothing is indexed' rather than 'nothing matched'.

    ONE embed, ONE rerank. searcher.search embeds the
    query and reranks 30 candidates per call, so calling it per project made
    N identical embeds of the same string and N reranks of raw chunk content
    for every keystroke pause — enough projects and the store's timeout fires
    while the lane is healthy. This does what searcher.search does, but with
    the remote arms hoisted: embed once, run the two store arms per project
    (the pod's Postgres), RRF-fuse per project exactly as the searcher
    does, pool the top candidates ACROSS projects, rerank that pool once.
    Same arms, same fusion, same degradation (no vector → lexical-only; rerank
    refused on a BYO/bastion lane → RRF order stands)."""
    from server.routes.codesearch import _get_db
    from codesearch import bridge_embed, searcher, store
    db = _get_db()
    projects = sorted(s["project"] for s in await store.stats(db) if s.get("chunks"))
    if not projects:
        return {"results": [], "projects": []}
    # The Host's configured embedding model and the column of its size, once
    # for every project; none set up → lexical-only, as searcher.search.
    space, _no_space = await bridge_embed.active_space()
    qvec = await bridge_embed.embed(query, space=space) if space is not None else None
    per_project = searcher.RERANK_CANDIDATES
    ranked: list[list[dict]] = []   # one RRF-ordered list per project
    failed: list[str] = []
    for p in projects:
        try:
            vector_hits = (await store.search_vector(db, project=p, query_embedding=qvec, space=space,
                                                     limit=per_project)
                           if qvec else [])
            lexical_hits = await store.search_lexical(db, project=p, query=query, limit=per_project)
        except Exception as e:  # one project's index failing is that project's, not the search's
            failed.append(f"{p}: {e}")
            continue
        ranked.append([dict(h, project=p) for h in searcher.rrf_fuse(vector_hits, lexical_hits)[:per_project]])
    # The candidate pool the reranker sees is drawn ROUND-ROBIN across projects
    # — every project's best hit, then every project's second — not a flat
    # sort by rrf_score. RRF scores are rank-based, so a both-arms rank-0 hit
    # scores the same 2/61 in every project; a flat sort + cut tied on project
    # NAME and, past RERANK_CANDIDATES projects, dropped the later ones before
    # the reranker ever saw them (measured with 40
    # projects: the best rerank match in proj39 never surfaced). The pool is
    # RERANK_CANDIDATES or one-per-project, whichever is larger, so every
    # project's top hit is in front of the reranker however many there are;
    # the rerank payload grows with the project count and nothing else.
    pool_cap = max(per_project, len(ranked))
    pool_: list[dict] = []
    for depth in range(per_project):
        for lst in ranked:
            if depth < len(lst):
                pool_.append(lst[depth])
        if len(pool_) >= pool_cap:
            break
    pool_ = pool_[:pool_cap]
    if pool_:
        scores = await bridge_embed.rerank(query, [h["content"] for h in pool_])
        if scores is not None:
            for h, s in zip(pool_, scores):
                h["rerank_score"] = s
            pool_.sort(key=lambda h: -(h["rerank_score"] if h.get("rerank_score") is not None else -1.0))
    out = {"results": pool_[:limit], "projects": projects, "semantic": bool(qvec)}
    if failed:
        out["failed"] = failed
    return out


# The words that name the skills store's own kind (tool_search.KIND_WORDS
# indexes them on every skill). In a store that holds nothing but skills they
# match every entry and tell none apart, so "my port skill" would list every
# skill the Host has; they are taken out of the query here. A query that is
# nothing but those words ("skills", "what skills do I have", a skill named
# `skill`) is searched as written instead: it asks for the skills themselves,
# and taken out it left no word to search, so the store answered "no matches"
# over every skill the Host has.
_SKILL_KIND_WORDS = re.compile(r"(?<![\w-])skills?(?![\w-])", re.I)


# The stores that carry no project, and the rule a typed turn reads each
# under (turn_types.context_filter's {projects, documents, memory}). The
# skills store follows code's: a type's context names projects, the
# documents store and a memory scope, and no key names skills; a typed turn's
# instructions are its type's own skill, and the Host's skills
# are what it learned, so a type that narrows memory or projects does not
# read them. recall (unified_search) and the per-turn offer of the Host's own
# skills (turn_own_skills) both ask this, so the two never disagree.
_UNPROJECTED_STORES: tuple[str, ...] = ("code", "skills")


def _type_reads_store(store: str, context: dict | None) -> bool:
    """Whether a turn with this context filter reads `store` (one of
    _UNPROJECTED_STORES). None (untyped, or a type that lists nothing)
    reads every store."""
    if context is None:
        return True
    every_project = context.get("projects") is None
    if store == "code":
        return every_project
    return every_project and (context.get("memory") or "all") == "all"


async def _search_skills_store(pool, entity_id: int, query: str, limit: int | None,
                               skill_step=None, authors=None, skip_untrusted: bool = False) -> dict:
    """The Host's own active skills and the platform's, found by what each is
    for. The entries are find_tools' own (runtime/discovery_index._skills:
    host_skills.list_skills with include_retired=False, then the platform's
    folder, a name the Host wrote shadowing the platform's), ranked by
    find_tools' own BM25 (runtime/tool_search.ToolIndex) on the same fields:
    the name, its one line, its description and tools. Lexical and
    deterministic: no model, no network.

    A hit is the skill's name, its one line, who wrote it and the call that
    opens it (the entry's `next`), never the skill's text: the answer is the
    same size however long a skill is. A source that could not be read is
    named under `failed`; both failing makes the store unavailable.

    `skill_step` is the turn's own answer to "which of an entry's steps may
    this turn make" (tool_doors.DoorCatalog.admitted_step, the question
    find_tools asks of every entry). Given, a skill the turn could not open
    is left out before ranking, as find_tools leaves it out, and counted
    under `left_out`; a kept skill's `open` is the step the turn may make.
    None (the REST route; a turn with no catalog) keeps every skill.

    `authors` (the per-turn offer, turn_own_skills): only skills whose
    author is one of these are ranked and returned; every skill stays in the
    index, so a word is weighed against the same set it is in recall.
    `limit` None returns every match.

    A skill written in a turn that read untrusted content (host_skills,
    migrations/v232) carries `untrusted` on its hit, and returning it marks
    the turn reading the answer (turn_taint.mark_rows), as a recalled marked
    memory does. `skip_untrusted` (the per-turn offer) never ranks or returns
    one: an offer nobody asked for carries no untrusted text."""
    from mastercode.runtime import discovery_index as _di
    from mastercode.runtime.tool_search import ToolIndex, tokens as _tokens
    failed: list[str] = []
    entries = await _di._skills(pool, entity_id, failed=failed)
    if len(failed) >= 2:
        raise RuntimeError("; ".join(failed))
    left_out = 0
    if skill_step is not None:
        kept = []
        for e in entries:
            step = skill_step(e.get("steps") or [])
            if step is None:
                left_out += 1
                continue
            kept.append({**e, "next": _di.render_step(step)})
        entries = kept
    by_key = {e["name"]: e for e in entries}
    index = ToolIndex.from_schemas({}, entries=[
        {"key": e["name"], "title": e["title"], "kind": e["kind"],
         "line": e["line"], "words": e["words"]} for e in entries])
    words = _SKILL_KIND_WORDS.sub(" ", query)
    if not _tokens(words):
        words = query
    allowed = None if authors is None and not skip_untrusted else {
        e["name"] for e in entries
        if (authors is None or ((e.get("detail") or {}).get("author") or "platform") in authors)
        and not (skip_untrusted and (e.get("detail") or {}).get("untrusted"))}
    results = []
    for key, score in index.search(words, limit=len(by_key) if limit is None else limit,
                                   allowed=allowed):
        e = by_key[key]
        detail = e.get("detail") or {}
        hit = {"name": e["title"], "description": e["line"],
               "author": detail.get("author") or "platform",
               "open": e["next"], "score": round(score, 3)}
        if detail.get("untrusted"):
            hit["untrusted"] = detail["untrusted"]
        results.append(hit)
    from mastercode.runtime import turn_taint as _turn_taint
    _turn_taint.mark_rows(results, "a skill found by recall")
    out: dict = {"results": results, "searched": len(entries)}
    if left_out:
        out["left_out"] = left_out
    if failed:
        out["failed"] = failed
    return out


async def unified_search(pool, entity_id: int, query: str, *,
                         stores=None, limit: int = 10,
                         timeout_s: float = SEARCH_STORE_TIMEOUT_S,
                         offset: int | None = None,
                         context: dict | None = None,
                         skill_step=None) -> dict:
    """ONE search across every store a person or an app may look in.

    Returns {query, stores, limit, <store>: {...}, unavailable: [store, ...]}.
    memory/documents carry federated_recall's own sections (plus `suppressed`:
    how many semantic-only hits fell under RELEVANCE_FLOOR, the pre-fire's own
    cut); projects and code carry {results: [...]} in the shape their
    own routes return, so an app that already reads one of those routes reads
    the same keys here; skills carries {results: [{name, description, author,
    open}], searched}. Never raises for a store that failed — it is named in
    `unavailable` and its section says why. A blank query answers empty.
    Raises UnknownSearchStore for a `stores` name that is not a store.

    `offset` pages memory and documents (federated_recall's own paging; its
    `page` record is copied into the answer). `context` is a typed turn's
    filter (runtime/turn_types.context_filter; the Host's `recall` passes
    it): memory and documents narrow inside federated_recall, the projects
    store keeps only the listed projects, and the code and skills stores,
    which carry no project, are searched only when the type reads every
    project (skills: and all memory). A store the type does
    not read is named under `narrowed`. None searches every store, as
    before. `skill_step` (the Host's `recall` passes its turn's
    DoorCatalog.admitted_step) leaves out a skill the turn could not open;
    see _search_skills_store."""
    query = (query or "").strip()
    limit = max(1, min(int(limit or 10), 50))
    if stores:
        unknown = [s for s in stores if s not in SEARCH_STORES]
        if unknown:
            raise UnknownSearchStore(
                f"unknown store(s) {unknown}; stores are {', '.join(SEARCH_STORES)}")
        wanted = [s for s in SEARCH_STORES if s in stores]
    else:
        wanted = list(SEARCH_STORES)
    out: dict = {"query": query, "stores": wanted, "limit": limit, "unavailable": []}
    _narrowed: list[str] = []
    for _st in _UNPROJECTED_STORES:
        if _st in wanted and not _type_reads_store(_st, context):
            wanted.remove(_st)
            _narrowed.append(f"{_st} (this turn type does not read it)")
    # The Host's own database carries no project; a type reads it as it reads
    # the Host's memory: all of it, or not this store.
    if ("world" in wanted and context is not None
            and (context.get("memory") or "all") != "all"):
        wanted.remove("world")
        _narrowed.append("world (this turn type does not read it)")
    if not query:
        for s in wanted:
            out[s] = ({"hits": []} if s == "memory"
                      else {"results": [], "projects": []} if s == "code"
                      else {"results": []})
        return out

    async def bounded(name: str, coro):
        try:
            return name, await asyncio.wait_for(coro, timeout=timeout_s)
        except asyncio.TimeoutError:
            return name, _unavailable(f"timed out after {timeout_s:.0f}s")
        except Exception as e:  # a store's failure is its own, never the search's
            # The store and the exception's type, never its text: a store's
            # exception can carry the words it was asked for (asyncpg quotes
            # an argument it cannot encode, asyncpg/protocol/prepared_stmt.pyx),
            # and this log is kept by the pod. The reason goes back to the
            # person who searched, in the answer, and nowhere else.
            log.info("unified search: %s store unavailable (%s)", name, type(e).__name__)
            return name, _unavailable(e)

    async def host_stores():
        # The host's own recall, verbatim — memory and documents in one call so
        # the query is embedded once, exactly as the pre-fire does it.
        from mastercode.memory_engine import RecallUnavailable
        want_mem = "memory" in wanted
        want_doc = "documents" in wanted
        scope = "all" if (want_mem and want_doc) else ("memory" if want_mem else "documents")
        try:
            # offset/context only when given: the REST route's call is the
            # one it always was.
            _more = {k: v for k, v in (("offset", offset), ("context", context)) if v is not None}
            fed = await federated_recall(pool, entity_id, query, scope=scope, limit=limit, **_more)
        except RecallUnavailable as e:
            fed = {}
            if want_mem:
                fed["memory"] = _unavailable(e)
            if want_doc:
                fed["documents"] = _unavailable(e)
        # The turn reading these hits is marked by what they carry: a row
        # written in a marked turn, a cited fact, a document (turn_taint).
        from mastercode.runtime import turn_taint as _turn_taint
        _turn_taint.mark_recall(fed)
        # The pre-fire's relevance floor, applied the way it applies it:
        # memory hits by `relevance`, document hits by `similarity`
        # (memory_engine.py:3133-3158). Counted, never silent.
        mem = fed.get("memory")
        if isinstance(mem, dict) and "hits" in mem:
            mem["hits"], mem["suppressed"] = _apply_relevance_floor(mem["hits"], store="memory")
        docs = fed.get("documents")
        if isinstance(docs, dict) and "results" in docs:
            docs["results"], docs["suppressed"] = _apply_relevance_floor(docs["results"], store="documents")
        return {k: fed[k] for k in ("memory", "documents", "page", "narrowed") if k in fed}

    tasks = []
    if "memory" in wanted or "documents" in wanted:
        tasks.append(bounded("_host", host_stores()))
    if "projects" in wanted:
        tasks.append(bounded("projects", _search_projects_store(pool, entity_id, query, limit)))
    if "code" in wanted:
        tasks.append(bounded("code", _search_code_store(query, limit)))
    if "skills" in wanted:
        tasks.append(bounded("skills", _search_skills_store(pool, entity_id, query, limit,
                                                            skill_step=skill_step)))
    if "world" in wanted:
        tasks.append(bounded("world", _search_world_store(pool, entity_id, query, limit)))

    for name, section in await asyncio.gather(*tasks):
        if name == "_host":
            if "unavailable" in section:  # the whole host call timed out / raised
                for k in ("memory", "documents"):
                    if k in wanted:
                        out[k] = dict(section)
            else:
                _narrowed.extend(section.pop("narrowed", None) or [])
                out.update(section)
        else:
            out[name] = section
    _keep = (context or {}).get("projects") if context is not None else None
    if _keep is not None and isinstance(out.get("projects"), dict) and "results" in out["projects"]:
        out["projects"]["results"] = [r for r in out["projects"]["results"]
                                      if isinstance(r, dict) and r.get("id") in set(_keep)]
    if _narrowed:
        out["narrowed"] = _narrowed
    out["unavailable"] = [s for s in wanted if isinstance(out.get(s), dict) and out[s].get("unavailable")]
    return out


def _hit_line(h: dict, keys: tuple[str, ...], body_keys: tuple[str, ...], cap: int = 240) -> str:
    head = " · ".join(str(h[k]) for k in keys if h.get(k) not in (None, "", []))
    body = next((str(h[k]) for k in body_keys if h.get(k)), "")
    body = " ".join(body.split())
    if len(body) > cap:
        body = body[:cap] + f" …[+{len(body) - cap} chars]"
    return f"- {head}" + (f": {body}" if body else "")


# Who wrote a skill, as the skills store's `author` says it: host_skills keeps
# 'host' or 'person' (host_skills.AUTHORS); the platform's are 'platform'.
_SKILL_BY = {"host": "yours, written by you", "person": "yours, written by the user",
             "platform": "platform"}


def _skill_line(h: dict) -> str:
    """One skill: its name, who wrote it, its one line (discovery_index cuts
    it to one line already) and the call that opens it."""
    by = _SKILL_BY.get(str(h.get("author") or ""), f"yours, by {h.get('author')}")
    if h.get("untrusted"):
        by += f"; written in a turn that read untrusted content ({str(h['untrusted'])[:120]})"
    line = " ".join(str(h.get("description") or "").split())
    return f"- {h.get('name')} ({by})" + (f": {line}" if line else "") + f" — open: {h.get('open')}"


# ── The per-turn offer of the Host's own skills ────────────────
#
# A Host wrote skills/port after a porting job, got the same kind of job
# again, and planned at once: its lead turn called find_tools by exact names
# and never called recall, which on this branch would have listed the skill
# (benchmark run 2, two attempts). The store existed; the Host did not search
# it before planning. So the turn's context carries, beside the memories the
# pre-fire pushes, at most TURN_SKILLS_MAX lines for the Host's OWN skills
# that match the person's message: the skills store's own entries, BM25 and
# turn-type rule, and the turn's own DoorCatalog.admitted_step, so a skill
# the turn could not open is never offered. The platform's skills are not
# offered: the tool descriptions already point at them.
#
# A match is real when the message NAMES the skill (every word of its name
# is a word of the message: the name is the Host's own label for the kind of
# job) or scores at least TURN_SKILLS_FLOOR. Measured on the real
# default-skills/ folder (56 skills) plus the benchmark Host's own `port`
# skill (its own description and allowed tools): a single shared word scores
# up to 5.96 for `port` whatever the word is ("port", or "between" from its
# description), and two incidental words reach 6.83 on the 313 questions in
# docs/wiki/pages (none about porting). No score floor alone separates "Port
# the library to Go" (5.96) from "Does Privatae see messages between Hosts?"
# (5.96), hence the name rule; 7.0 sits above every wiki question and admits
# a description match of more than two words ("rewrite markdown-it-py in
# another language", 10.12). On 12 porting messages and 325 others: 8 and 4
# (three network-port questions, one "what language should I learn first?").
TURN_SKILLS_MAX = 2
TURN_SKILLS_FLOOR = 7.0
# The pre-fire's own cap on the message it searches with (prompt_schema
# _b_memory): a pasted blob costs both arms their hit rate.
_TURN_QUERY_MAX = 2000


async def turn_own_skills(pool, entity_id: int, message: str, *,
                          context: dict | None = None, skill_step=None) -> list[dict]:
    """The Host's own active skills (author 'host' or 'person',
    host_skills.AUTHORS) that really match this message, best first, at most
    TURN_SKILLS_MAX: each {name, description, author, open, score}, never
    the skill's text. [] for a blank message, a turn whose type does not read
    the skills store (_type_reads_store), or no real match. A skill written
    in a turn that read untrusted content is never one of them. `skill_step` is
    the turn's DoorCatalog.admitted_step (see _search_skills_store)."""
    from mastercode.runtime.host_skills import AUTHORS
    from mastercode.runtime.tool_search import tokens as _tokens
    query = (message or "").strip()[:_TURN_QUERY_MAX]
    if not query or not _type_reads_store("skills", context):
        return []
    # A skill written in a turn that read untrusted content is never offered
    # here (skip_untrusted): recall still finds it, and marks its turn.
    sec = await _search_skills_store(pool, entity_id, query, None,
                                     skill_step=skill_step, authors=AUTHORS, skip_untrusted=True)
    said = set(_tokens(query))
    out = []
    for h in sec.get("results") or []:
        name_words = set(_tokens(str(h.get("name") or "").replace("-", " ")))
        named = bool(name_words) and name_words <= said
        if named or float(h.get("score") or 0.0) >= TURN_SKILLS_FLOOR:
            out.append(h)
        if len(out) >= TURN_SKILLS_MAX:
            break
    return out


def format_turn_skills(hits: list[dict]) -> str:
    """The per-turn section for turn_own_skills' hits: one line each, as
    recall's skills store renders them. "" when there are none."""
    if not hits:
        return ""
    return ("## Your own skills that match this message\n"
            + "\n".join(_skill_line(h) for h in hits))


def format_unified_text(out: dict) -> str:
    """The `recall` tool's text for a unified_search answer: memory and
    documents exactly as format_recall_text renders them, then the projects,
    code, skills and world stores, each labeled, each honest about being
    unavailable, each naming how to open a hit."""
    host = {k: out[k] for k in ("memory", "documents", "page", "narrowed") if k in out}
    parts = [format_recall_text(host)] if any(k in host for k in ("memory", "documents", "narrowed")) else []
    for store, title, keys, body_keys, how in (
            ("projects", "Projects", ("id", "name", "status"), ("description",),
             "get_project(project_id) opens one"),
            ("code", "Indexed code", ("project", "file", "path", "symbol", "start_line"),
             ("content", "text", "snippet"), "code_read or search_my_code opens it"),
            ("skills", "Skills", ("name",), ("description",),
             "each opens with the call shown"),
            ("world", "Your own database", ("name", "kind", "rows_estimate"),
             ("summary",), "each line's `open` reads it; operate(tool='data', args={op:'sql', ...}, intent='...') queries it")):
        sec = out.get(store)
        if not isinstance(sec, dict):
            continue
        if sec.get("unavailable"):
            parts.append(f"## {title} — UNAVAILABLE\n{sec['unavailable']} — this store "
                         "did NOT run; do not treat it as 'no matches'.")
            continue
        rows = [r for r in (sec.get("results") or []) if isinstance(r, dict)]
        # A half of the skills store that could not be read is said, so a
        # shorter list is never taken for the whole one.
        note = ("\nNot searched: " + "; ".join(str(f) for f in sec["failed"])
                if store == "skills" and sec.get("failed") else "")
        # Skills this turn could not open are left out, as find_tools leaves
        # them out, and the count is said.
        _lo = int(sec.get("left_out") or 0) if store == "skills" else 0
        if _lo:
            note += f"\nLeft out: {_lo} skill{'' if _lo == 1 else 's'} this turn cannot open."
        if not rows:
            if store == "code" and not sec.get("projects"):
                parts.append(f"## {title}\nnothing is indexed")
            elif _lo and not sec.get("searched"):
                parts.append(f"## {title}\nnone this turn can open" + note)
            else:
                parts.append(f"## {title}\nno matches" + note)
            continue
        line = _skill_line if store == "skills" else (lambda r: _hit_line(r, keys, body_keys))
        parts.append(f"## {title} ({how})\n" + "\n".join(line(r) for r in rows) + note)
    return "\n\n".join(p for p in parts if p) or "No sources searched."



def _thread_label(h: dict) -> str:
    kind, ref = h.get("sctx_kind"), h.get("sctx_ref")
    if kind == "project" or h.get("project_id") is not None:
        return f"project {ref or h.get('project_id')} thread"
    if kind == "app":
        return f"{ref or 'app'} general chat"
    if kind == "peer":
        return f"peer {ref}"
    return "background/session"


def _arms_line(mem: dict) -> str:
    arms = mem.get("arms") or []
    if "semantic" in arms and "lexical" in arms:
        return "searched by meaning + words"
    if arms == ["lexical"]:
        return (f"searched by WORDS ONLY — "
                f"{mem.get('semantic_unavailable') or 'meaning search down'}")
    if arms == ["semantic"]:
        return (f"searched by MEANING ONLY — "
                f"{mem.get('lexical_unavailable') or 'text index missing'}")
    return "searched"


# A document hit is shown as a SNIPPET of this many characters, marked when
# cut and pointing at the whole read (context_ingest op='read'). The snippet is
# a preview; the document itself is never cut here.
DOC_SNIPPET_CHARS = 420


def format_recall_text(fed: dict, *, max_chars: int | None = None) -> str:
    """Render a federated result as the model-facing text the recall tool
    returns: sources labeled, arms honest, low-relevance suppression counted,
    per-store unavailability loud.

    No length cut by default: it used to slice
    the whole answer at 12,000 characters with no marker, mid-hit, which could
    drop the documents section and the "Not searched" lines entirely. The
    transcript wall bounds the result and names any cut; `limit`/`offset`
    page it. A caller that passes max_chars gets a MARKED cut."""
    parts: list[str] = []

    mem = fed.get("memory")
    if mem is not None:
        if mem.get("unavailable"):
            parts.append(
                f"## Your memory — UNAVAILABLE\n{mem['unavailable']} — the "
                "memory search did NOT run; do not treat this as 'no matches'.")
        else:
            hits, shown, suppressed = mem.get("hits") or [], [], 0
            # A rule is labelled one, and a plain preference in a rule's own
            # words, found beside it, is labelled as held by it
            # (memory_engine.same_words_key).
            from mastercode.memory_engine import is_rule, same_words_key
            rule_of = {}
            def _marked(h):  # a rule (rule_kind); a stored skill keeps its own type tag
                return h.get("memory_type") == "preference" and is_rule("preference", h.get("metadata"))
            for h in hits:
                if h.get("id") is not None and _marked(h):
                    rule_of.setdefault(same_words_key(h.get("content")), h["id"])
            for h in hits:
                only_semantic = h.get("matched") == ["semantic"]
                rel = h.get("relevance")
                if only_semantic and rel is not None and rel < RELEVANCE_FLOOR:
                    suppressed += 1
                    continue
                when = ""
                try:
                    when = h["created_at"].strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass
                tags = [h.get("memory_type") or "memory",
                        "+".join(h.get("matched") or [])]
                if h.get("memory_type") == "preference" and h.get("id") is not None:
                    tags.insert(0, f"#{h['id']}")  # memory_scope takes this id
                if _marked(h):
                    tags.insert(2, "rule")
                elif same_words_key(h.get("content")) in rule_of:
                    tags.append(f"held by rule #{rule_of[same_words_key(h.get('content'))]}")
                if rel is not None:
                    tags.append(f"relevance {rel:.2f}")
                if when:
                    tags.append(when)
                if h.get("via"):
                    tags.append(f"linked via {h['via']}")
                if h.get("platform_notice"):  # named apart, never in the Host's words
                    tags.append("platform notice: " + ", ".join(h["platform_notice"]))
                tags.append(_thread_label(h))
                body_text = h.get("content") or ""
                if h.get("memory_type") == "cited_fact":
                    # What a source said, fenced as quoted data (memory kind
                    # five, runtime/task_record.cited_fact_text).
                    from mastercode.runtime.task_record import cited_fact_text
                    body_text = cited_fact_text(body_text, h.get("metadata"))
                shown.append(f"[{' | '.join(tags)}]\n{body_text}")
            head = f"## Your memory ({_arms_line(mem)})"
            if shown:
                body = "\n\n".join(shown)
                if suppressed:
                    body += f"\n({suppressed} low-relevance hit(s) suppressed)"
                parts.append(f"{head}\n{body}")
            else:
                note = f" ({suppressed} low-relevance hit(s) suppressed)" if suppressed else ""
                parts.append(f"{head}\nno matches{note}")

    docs = fed.get("documents")
    if docs is not None:
        if docs.get("unavailable"):
            parts.append(
                f"## The user's documents — UNAVAILABLE\n{docs['unavailable']} — "
                "the document search did NOT run; do not treat this as 'no matches'.")
        else:
            results = docs.get("results") or []
            arms = docs.get("arms") or []
            arm_desc = ("meaning + words" if len(arms) > 1 else
                        "words only" if arms == ["lexical"] else
                        "meaning only" if arms == ["semantic"] else "searched")
            if results:
                lines = []
                for r in results:
                    meta = r.get("metadata")
                    if isinstance(meta, str):
                        try:
                            meta = json.loads(meta)
                        except Exception:
                            meta = {}
                    meta = meta or {}
                    fname = meta.get("from_file") or r.get("title") or f"entry {r.get('id')}"
                    if r.get("kind") == "file":
                        lines.append(f"[file | {fname}] (binary/file card — "
                                     f"read with context_ingest op='read' file='{fname}')")
                    else:
                        body = r.get("content") or ""
                        snippet = body[:DOC_SNIPPET_CHARS]
                        if len(body) > DOC_SNIPPET_CHARS:
                            snippet += (f" …[snippet; +{len(body) - DOC_SNIPPET_CHARS} chars "
                                        "in this entry]")
                        lines.append(f"[{fname}]\n{snippet}")
                parts.append(
                    f"## The user's documents (searched by {arm_desc})\n"
                    + "\n\n".join(lines)
                    + "\n(To read any document WHOLE: context_ingest op='read' "
                      "file='<name>'.)")
            else:
                parts.append(
                    f"## The user's documents (searched by {arm_desc})\nno matches")

    # A store the turn's TYPE does not read (turn types): said
    # out loud, so an empty answer says why and the host does not read "no
    # matches" as "nothing exists".
    for n in fed.get("narrowed") or []:
        parts.append(f"## Not searched — {n}")
    page = fed.get("page") or {}
    more = [s for s in ("memory", "documents") if (page.get(s) or {}).get("more")]
    if more:
        nxt = int(page.get("offset") or 0) + int(page.get("limit") or 0)
        how = (f"recall again with offset={nxt} to read on" if page.get("pageable")
               else "narrow the query to see past them")
        parts.append(f"## More may match — {', '.join(more)} returned a full page of "
                     f"{int(page.get('limit') or 0)}; {how}.")
    text = "\n\n".join(parts) if parts else "No sources searched."
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars] + f" …[+{len(text) - max_chars} chars truncated]"
    return text
