"""
Memory consolidation — the helpers every in-turn memory writer shares.

A memory is written in the Host's own turn (close_turn, memory_curate, a
taught rule, task_record) and nothing re-reads a conversation afterwards: no
batch extractor, no second model deciding add/update/delete, no decay or
merge pass. What this module keeps is the shape those writers share:

  - MemoryConsolidator: insert a row; supersede one bitemporally
    (t_valid_until + resolution_pointer, metadata.superseded_in); record that
    another conversation also said it (note_held); carry what a row is ABOUT
    to the row that replaces it; find the nearest live rows of a type.
  - the duplicate and supersession rules (_duplicate_of, _supersession_target)
    and their floors, which say when a new statement restates or corrects a
    row that already exists;
  - the scope helpers (scope_stamp, same_scope, UNSCOPED) and _mark_resolved,
    which closes a goal.

The batch consolidator, the ADD/UPDATE/DELETE classifier, the decay and merge
passes, the AGE graph extraction and the boot retype pass are gone; their
one-time effects are migration v235.
"""

import json
import logging
import uuid as _uuid
from typing import Optional

import asyncpg

log = logging.getLogger("cseries.mastercode.memory_consolidation")

# Nearest live rows examined per candidate.
NEAREST_PER_CANDIDATE = 5

# How close a nearest row must be before a candidate the model called a
# correction is allowed to CLOSE it. Neither number is new: both are the pair
# _merge_duplicates uses to decide two rows are the same
# memory, which is this question asked from the other side.
#
# Below the bar the model's "this corrects something" is believed but the
# TARGET is not identified, so the candidate is stored as new and the existing
# row is left standing. That is the safe direction: a wrong supersession
# silently retires a belief, while a duplicate is merged by the cron pass that
# already exists.
SUPERSEDE_SIM_FLOOR = 0.85

# The embed lane is remote and goes down, and when it does _find_candidates
# falls back to a keyword lookup that reports sim = 0.0 for every row — no
# cosine was computed, not "these are unrelated". Reading that as "too far
# apart" would silently disable corrections for the whole outage, which is the
# opposite of safe: the host would keep serving a belief the person had just
# told it was wrong. So when there is no vector, the corroborator
# _merge_duplicates pairs with its cosine carries the decision alone: word
# overlap over the LONGER text (dividing by the shorter one lets any summary
# count as a duplicate of the fact it abbreviates).
SUPERSEDE_OVERLAP_FLOOR = 0.5

# THE DUPLICATE RULE IS STRICTER THAN THE SUPERSEDE RULE, and the asymmetry is
# the whole point. Measured: this is a data-loss bug, not a tuning
# preference.
#
# The two branches carry different risk because they are different acts. A
# supersession happens only where the MODEL asserted "this corrects an earlier
# belief"; the row it closes stays in the corpus, superseded and auditable, and
# the new statement is stored either way. A deterministic DROP has no such
# assertion behind it and keeps nothing — the candidate is never written, and
# the caller then stamps the turn rows extracted_at, so the conversation it came
# from is never consolidated again. A wrong drop is permanent.
#
# So the drop branch uses the conjunction _merge_duplicates actually uses
# (memory_consolidation.py:138 and :156) — cosine > 0.85 AND overlap > 0.5 —
# and not either alone:
#   - cosine alone: "wants the report by monday" against a live "... by friday"
#     sits at 0.91. Semantically adjacent, factually opposite, silently gone.
#   - overlap alone: with the embed lane down, "the user wants the desktop
#     black" against "the user wants the report by friday" shares exactly
#     {the, user, wants} — 3 of 6 words, 0.5 on the nose — two unrelated
#     memories merged on stopwords.
#
# And, like _merge_duplicates, it runs ONLY where both rows carry embeddings:
# that query gates on `a.<col> IS NOT NULL AND b.<col> IS NOT NULL`. No vector
# means no drop at all — store the candidate and let the merge pass reconcile a
# duplicate later, which is recoverable in a way a drop is not.
DUPLICATE_SIM_FLOOR = 0.85
DUPLICATE_OVERLAP_FLOOR = 0.5


async def _mark_resolved(pool, entity_id: int, ids: list[int],
                         resolved_in: dict | None = None) -> int:
    """Close the goals the batch completed. Scoped to the ids we OFFERED and to
    the two types that have this exit condition, so a hallucinated id or a
    stray fact id cannot silently retire an unrelated row. Never raises.

    `resolved_in` (memory_consolidation.scope_stamp) is the scope whose
    conversation closed them, stamped as metadata.resolved_in: open goals
    reach every thread (_open_goals), so a goal can be closed from a thread
    the person later deletes, and circles_threads.delete_thread reopens
    exactly those."""
    if not ids:
        return 0
    try:
        res = await pool.execute(
            """
            UPDATE entity_memory
               SET resolved_at = NOW(), updated_at = NOW(),
                   metadata = CASE WHEN $3::jsonb IS NULL THEN metadata
                                   ELSE COALESCE(metadata, '{}'::jsonb)
                                        || jsonb_build_object('resolved_in', $3::jsonb) END
             WHERE entity_id = $1
               AND id = ANY($2::int[])
               AND memory_type IN ('goal', 'unresolved')
               AND resolved_at IS NULL
            """,
            entity_id, ids, json.dumps(resolved_in) if resolved_in else None,
        )
        try:
            return int(str(res).rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return len(ids)
    except Exception as e:
        log.warning("memory: resolve marking failed "
                    "(entity=%s ids=%s): %s", entity_id, ids, e)
        return 0


def _word_overlap(a: str, b: str) -> float:
    """Shared words over the LONGER text. _merge_duplicates' rule, and its
    reason: dividing by the shorter one lets a thin summary count as a
    duplicate of the rich fact it abbreviates, and eat it."""
    wa = set((a or "").lower().split())
    wb = set((b or "").lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


async def _same_type_neighbours(consolidator, entity_id: int, content: str,
                                mem_type: str, embed_str) -> list[dict]:
    """The live, supersedable rows nearest this candidate, same type only.

    ONE query, read by two different predicates below. Same TYPE is required
    here because a preference and a decision can be near neighbours in
    embedding space and still be different beliefs about different things.

    _find_candidates excludes NEVER_SUPERSEDED_TYPES, so a `turn` row can never
    appear. Never raises: an unavailable lookup means "no neighbour", which
    makes both callers store the candidate, and storing is the recoverable
    direction for both of them.
    """
    try:
        nearest = await consolidator._find_candidates(
            entity_id, content, embed_str, mem_type, k=NEAREST_PER_CANDIDATE)
    except Exception as e:
        log.warning("memory: nearest lookup failed (entity=%s, "
                    "storing as new): %s", entity_id, e)
        return []
    return [c for c in nearest if c.get("memory_type") == mem_type]


def _supersession_target(neighbours: list[dict], content: str,
                         embed_str) -> int | None:
    """The row a CORRECTION replaces, or None.

    The loose rule, and it is loose on purpose: the model has already asserted
    that this statement corrects an earlier belief, so the only open question
    is which row it means. Either signal alone is enough to answer that, and
    the loser is superseded rather than deleted — it stays in the corpus,
    auditable, with the replacement linked.

    With no embedding _find_candidates ran its keyword arm, which reports
    sim = 0.0 for every row because no cosine was computed, not because the
    rows are unrelated. Word overlap decides there instead: reading that zero
    as "too far apart" would silently disable corrections for the whole of an
    embed outage, and the host would keep serving a belief the person had just
    told it was wrong.
    """
    for c in neighbours:
        if embed_str:
            if float(c.get("sim") or 0.0) >= SUPERSEDE_SIM_FLOOR:
                return int(c["id"])
        elif _word_overlap(content, c.get("content") or "") >= SUPERSEDE_OVERLAP_FLOOR:
            return int(c["id"])
    return None


def _duplicate_of(neighbours: list[dict], content: str,
                  embed_str) -> tuple[int, float, float] | None:
    """The row this candidate merely restates, as (id, sim, overlap), or None.

    The STRICT rule — see DUPLICATE_SIM_FLOOR for why it must not be the loose
    one. FOUR conditions, all required:

      1. an embedding exists. No vector, no drop: _merge_duplicates gates on
         both rows having one, and dropping on lexical overlap alone merges
         unrelated memories that happen to share stopwords.
      2. cosine STRICTLY above the floor.
      3. word overlap STRICTLY above the floor, over the longer text.
      4. the candidate ADDS NO WORD the matched row lacks.

    Condition 4 is the one the first three could not supply, measured:
    "the user wants the report by monday" against a live "... by
    friday" clears 2 and 3 comfortably (cosine 0.91, overlap 0.83) — and it
    clears them BECAUSE only one word changed, which is the ordinary shape of
    an updated fact. Two nearly identical sentences are exactly what a revised
    detail looks like, so high scores on a near-identical pair are evidence FOR
    new information, not against it. The model cannot save us here either: it
    judged corrects_earlier without ever seeing this neighbour.

    A pure restatement adds nothing — "user wants report by friday" is a subset
    of "the user wants the report by friday" and is safe to drop. A candidate
    carrying any word the match lacks (monday) is stored: not superseded, not
    dropped. The merge pass reconciles it if it really was noise, and a later
    turn that actually corrects the fact arrives flagged.

    Tokenised exactly as _word_overlap tokenises — lowercase whitespace split,
    NO stopword list, because that function has none and two different notions
    of "word" between the overlap score and the subset test would drift apart.
    Carrying no stopword list makes this test STRICTER, not looser: a filler
    word the match lacks also blocks the drop. That errs toward storing a
    duplicate, which the merge pass already handles, over losing a fact, which
    nothing recovers.

    A fifth condition closes the one shape the subset test cannot see: the
    SAME words in a different order. "the user prefers tea over coffee"
    against a live "the user prefers coffee over tea" has an identical word
    set, cosine 0.93 and overlap 1.0, and the opposite meaning. When the two
    word sets are equal, the drop needs the token sequences to be identical
    too; a permutation is stored like any other new statement.

    Returns the scores as well as the id so the caller can say WHY in the log
    rather than only that something was dropped.
    """
    if not embed_str:
        return None
    cand_tokens = (content or "").lower().split()
    cand_words = set(cand_tokens)
    if not cand_words:
        return None
    for c in neighbours:
        sim = float(c.get("sim") or 0.0)
        if sim <= DUPLICATE_SIM_FLOOR:
            continue
        match_content = c.get("content") or ""
        overlap = _word_overlap(content, match_content)
        if overlap <= DUPLICATE_OVERLAP_FLOOR:
            continue
        match_tokens = match_content.lower().split()
        match_words = set(match_tokens)
        if not cand_words <= match_words:
            # The candidate says something this row does not. Whatever that
            # word is, it is the reason this statement was derived.
            continue
        if cand_words == match_words and cand_tokens != match_tokens:
            # Same words, different order: word order carries meaning
            # ("tea over coffee"), and a reordering is not a restatement.
            continue
        return int(c["id"]), sim, overlap
    return None


# The owner a record names when its caller has no conversation scope — a
# memory tool called outside any conversation. It is never a thread, so it is
# never deleted: what such a caller decides (a restatement, a second
# retirement) stands through any thread delete, as it would had the thread
# never existed.
UNSCOPED = {"kind": "app", "ref": "memory"}


def _verbatim(entry: dict) -> bool:
    """A held_in entry that restated the row word for word: it keeps no words
    of its own (MemoryConsolidator.note_held)."""
    return not entry.get("words")


def same_scope(a, b) -> bool:
    return (isinstance(a, dict) and isinstance(b, dict) and a.get("kind") == b.get("kind")
            and str(a.get("ref") or "") == str(b.get("ref") or ""))


def _covers(new_content: str, target_id, candidates: list) -> bool:
    """Do the new words carry everything the candidate `target_id` says?"""
    target = next((c for c in candidates if c["id"] == target_id), None)
    if target is None:
        return False
    return set((target.get("content") or "").lower().split()) <= set(new_content.lower().split())


def _close_to(candidate: dict, content: str, embedding_str: Optional[str]) -> bool:
    """The correction bar (_supersession_target): cosine at or above
    SUPERSEDE_SIM_FLOOR with a vector, word overlap at or above
    SUPERSEDE_OVERLAP_FLOOR without one."""
    if embedding_str:
        return float(candidate.get("sim") or 0.0) >= SUPERSEDE_SIM_FLOOR
    return _word_overlap(content, candidate.get("content") or "") >= SUPERSEDE_OVERLAP_FLOOR


def scope_stamp(source_context: Optional[dict],
                from_turns: Optional[list] = None) -> Optional[dict]:
    """What a retirement (metadata.superseded_in) or a goal's close
    (metadata.resolved_in) records: the scope whose conversation did it, plus
    the turn rows that conversation consolidated. The turn ids are what lets
    the stamp move with the messages when a Split or an adopt moves them
    (circles_threads.carry_stamps), the way the rows learned from them move.
    None when there is no scope to name."""
    if not isinstance(source_context, dict) or not source_context.get("kind"):
        return None
    stamp = dict(source_context)
    if from_turns:
        stamp["from_turns"] = [int(t) for t in from_turns]
    return stamp


class MemoryConsolidator:
    """Between-session memory maintenance engine."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def _find_candidates(
        self,
        entity_id: int,
        new_content: str,
        embedding_str: Optional[str],
        memory_type: Optional[str],
        k: int = 5,
        *,
        dropped: bool = False,
    ) -> list[dict]:
        """Find the top-k live memories most similar to the candidate content.

        Uses semantic similarity if we have an embedding; falls back to
        keyword ILIKE so the call still works when Ollama is down.

        `dropped=True` searches instead the rows a Circles thread retired with
        NO replacement (metadata.superseded_in names a thread, no pointer):
        the rows a delete of that thread would bring back. A correction that
        finds no live target links the one it means
        (batch_consolidator._link_dropped).

        EVERY row returned here is a supersession TARGET — the caller may close
        it bi-temporally — so memory_engine.NEVER_SUPERSEDED_TYPES is excluded
        in this one place, which is where BOTH the per-candidate classifier and
        the batch path get their targets.

        Measured: without the exclusion a `turn` row was offered and
        superseded, so t_valid_until closed the raw user message that the
        candidate had been derived FROM and the person's own words left
        recall_recent_turns. See that constant for why each type is on it.
        A rule-marked preference is excluded with them (memory_engine.RULE_KINDS).
        """
        from mastercode.memory_engine import NEVER_SUPERSEDED_TYPES, not_rule_sql
        never = list(NEVER_SUPERSEDED_TYPES)
        not_rule = not_rule_sql()
        live = ("(t_valid_until IS NULL OR t_valid_until > NOW())" if not dropped else
                "t_valid_until <= NOW() AND resolution_pointer IS NULL"
                " AND metadata->'superseded_in'->>'kind' = 'thread'")
        if embedding_str:
            # Multi-dim: route the content vector op to the column matching the
            # entity's active embedding model's dim. Whitelisted name.
            from mastercode.memory_engine import active_embedding_column, embedding_cast_type
            content_col, dim = await active_embedding_column(self.pool, entity_id)
            cast = embedding_cast_type(dim)
            sql = f"""
                SELECT id, content, memory_type, importance,
                       1 - ({content_col} <=> $2::{cast}) AS sim
                FROM entity_memory
                WHERE entity_id = $1
                  AND status = 'active'
                  AND {live}
                  AND {content_col} IS NOT NULL
                  AND NOT (memory_type = ANY($4))
                  AND {not_rule}
                ORDER BY {content_col} <=> $2::{cast}
                LIMIT $3
            """
            rows = await self.pool.fetch(sql, entity_id, embedding_str, k, never)
            return [dict(r) for r in rows]

        # Embedding-less fallback — keyword ILIKE on the longest token.
        tokens = sorted((new_content or "").split(), key=len, reverse=True)
        needle = tokens[0] if tokens else new_content
        sql = f"""
            SELECT id, content, memory_type, importance, 0.0 AS sim
            FROM entity_memory
            WHERE entity_id = $1
              AND status = 'active'
              AND {live}
              AND content ILIKE '%' || $2 || '%'
              AND NOT (memory_type = ANY($4))
              AND {not_rule}
            ORDER BY importance DESC
            LIMIT $3
        """
        rows = await self.pool.fetch(sql, entity_id, needle, k, never)
        return [dict(r) for r in rows]

    async def _carry_about(self, entity_id: int, losers: list[int], winners: list[int],
                           conn=None) -> None:
        """What the Host set a memory to be ABOUT (metadata.about, written by
        memory_scope) outlives a restatement: the row that replaces it takes
        it, with its scope_history, unless it already has its own. Only
        between rows of the same type. Provenance (source_context,
        project_id) is never touched, so thread delete, split and adoption
        still find and move the new row by where it was said. Without this a
        preference made global in one review erodes back to one project the
        next time it is said there. One statement for one pair (_supersede)
        or a batch (_merge_duplicates); a winner with two losers takes the
        most recently scoped one."""
        await (conn or self.pool).execute(
            """
            UPDATE entity_memory n
               SET metadata = COALESCE(n.metadata, '{}'::jsonb) || c.patch
              FROM (SELECT DISTINCT ON (w.winner_id) w.winner_id, o.memory_type,
                           jsonb_strip_nulls(jsonb_build_object(
                               'about', o.metadata->'about',
                               'scoped_by', o.metadata->'scoped_by',
                               'scoped_at', o.metadata->'scoped_at',
                               'scope_history', o.metadata->'scope_history')) AS patch
                      FROM unnest($1::int[], $2::int[]) AS w(loser_id, winner_id)
                      JOIN entity_memory o ON o.id = w.loser_id AND o.entity_id = $3
                     WHERE o.metadata ? 'about'
                     ORDER BY w.winner_id, o.metadata->>'scoped_at' DESC NULLS LAST) c
             WHERE n.id = c.winner_id AND n.entity_id = $3
               AND n.memory_type = c.memory_type
               AND NOT (COALESCE(n.metadata, '{}'::jsonb) ? 'about')
            """,
            list(losers), list(winners), entity_id)

    async def _insert_new_memory(
        self,
        entity_id: int,
        content: str,
        memory_type: str,
        importance: float,
        embedding_str: Optional[str],
        dimension_vec_str: Optional[str] = None,
        parent_id: Optional[int] = None,
        *,
        source_session: Optional[str] = None,
        confidence: float = 0.85,
        visibility: str = "default",
        metadata_extra: Optional[dict] = None,
    ) -> int:
        """Insert a new entity_memory row and return its id.

        parent_id, if set, is written into metadata.supersedes_id so the lineage
        is queryable without joining the AGE graph (useful for read-side audit).

        source_session / confidence / visibility / metadata_extra let
        the extraction wedge keep full row fidelity — a hardcoded
        0.85/'default'/{} would have dropped source_context (which the Threads
        lens groups by), emotion tags, and session provenance from every
        derived memory. Existing callers omit these and get the old defaults.
        """
        meta = dict(metadata_extra or {})
        if parent_id:
            meta["supersedes_id"] = parent_id
        # dimension_vec_str is accepted for the callers' sake and ignored: the
        # ten-dimension personality vector left entity_memory.
        if embedding_str:
            # Multi-dim: write the content embedding into the column matching
            # the entity's active model's dim. Whitelisted name.
            from mastercode.memory_engine import active_embedding_column, embedding_cast_type
            content_col, dim = await active_embedding_column(self.pool, entity_id)
            cast = embedding_cast_type(dim)
            row = await self.pool.fetchrow(
                f"""
                INSERT INTO entity_memory (
                    entity_id, content, memory_type, source_session,
                    importance, confidence, visibility,
                    metadata, {content_col}
                ) VALUES ($1, $2, $3, $4, $5, $6, $7,
                          $8::jsonb, $9::{cast})
                RETURNING id
                """,
                entity_id, content, memory_type, source_session,
                importance, confidence, visibility,
                json.dumps(meta), embedding_str,
            )
        else:
            row = await self.pool.fetchrow(
                """
                INSERT INTO entity_memory (
                    entity_id, content, memory_type, source_session,
                    importance, confidence, visibility,
                    metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                RETURNING id
                """,
                entity_id, content, memory_type, source_session,
                importance, confidence, visibility,
                json.dumps(meta),
            )
        return row["id"]

    async def _supersede(
        self, entity_id: int, target_id: int, replacement_id: Optional[int],
        superseded_in: Optional[dict] = None, *, carry_about: bool = True,
    ) -> bool:
        """Mark target_id superseded — sets t_valid_until=NOW() and points at
        the replacement (NULL for DELETE actions).

        `superseded_in` (scope_stamp) is the scope whose conversation retired
        it, stamped as metadata.superseded_in. When the person deletes that
        thread (circles_threads.delete_thread), a row it retired with no
        replacement comes back by this stamp; a row it replaced comes back by
        the pointer, and only while nothing outside deleted threads has
        replaced it since.

        An ARCHIVED target is left alone: its t_valid_until is still NULL, and
        retiring a row a thread's delete archived would link a live row to it.

        `carry_about` (default True): the replacement takes the target's
        decision (_carry_about). The teaching writer passes False: a rule
        taught with "Remember this:" is a fresh teaching, undecided and read
        everywhere, whatever a plain preference in its words was scoped to.
        """
        res = await self.pool.execute(
            """
            UPDATE entity_memory
            SET t_valid_until = NOW(),
                resolution_pointer = $3,
                metadata = CASE WHEN $4::jsonb IS NULL THEN metadata
                                ELSE COALESCE(metadata, '{}'::jsonb)
                                     || jsonb_build_object('superseded_in', $4::jsonb) END,
                updated_at = NOW()
            WHERE id = $2
              AND entity_id = $1
              AND status = 'active'
              AND t_valid_until IS NULL
            """,
            entity_id, target_id, replacement_id,
            json.dumps(superseded_in) if superseded_in else None,
        )
        done = not str(res).endswith(" 0")
        if done and replacement_id is not None and carry_about:
            await self._carry_about(entity_id, [int(target_id)], [int(replacement_id)])
        return done

    # For each record note_held keeps: the metadata key naming the row's own
    # conversation for that record, and the state the row must still be in.
    _HELD = {
        "held_in": ("source_context",
                    "status = 'active' AND (t_valid_until IS NULL OR t_valid_until > NOW())"),
        "resolved_also_in": ("resolved_in",
                             "status = 'active' AND resolved_at IS NOT NULL"),
        "retired_also_in": ("superseded_in",
                            "status = 'active' AND t_valid_until <= NOW()"),
    }

    async def note_held(self, entity_id: int, row_id: int, stamp: Optional[dict],
                        key: str = "held_in", *, words: Optional[str] = None,
                        memory_type: Optional[str] = None,
                        about: Optional[dict] = None, taught: bool = False) -> bool:
        """Record that another conversation also said what `row_id` holds.

        The duplicate guards drop a candidate that restates a live row
        (batch_consolidator: exact match, _duplicate_of, the model's
        already_known; the teaching writer; extract_and_store; the
        classifier's NOOP), so the restating conversation leaves no row of its
        own. When the row belongs to a Circles thread the person later
        deletes, the delete would take what the live conversation also said.
        `stamp` (scope_stamp) is appended to metadata[key]. A held_in entry
        also keeps when it was first said ("at"), and the conversation's own
        `words` when they are not the row's (with its `memory_type` when that
        differs too and is one a restatement may create: MEMORY_TYPES, never
        NEVER_SUPERSEDED_TYPES).
        circles_threads.delete_thread then gives that conversation the row
        itself if it said the row's words, and otherwise a row of its own
        words, so the thread's extra words leave with the thread. The same
        record, for a goal another conversation also completed, is
        key='resolved_also_in'; for a row another conversation also retired,
        key='retired_also_in'.

        `about` (held_in only): the place the Host decided the words are
        about, when the row is undecided and read there already but not where
        this conversation is (close_turn.held_already). The entry is then
        PROVENANCE ONLY: scope reading skips it (memory_retrieval
        HELD_IN_READS), so the conversation gains no reach, while the delete
        still finds it — _keep_held re-homes the row to it — and a delete of
        the place it names drops the mark (circles_threads._forget), after
        which it reads there like any record. The words said in a
        conversation about a thread outlive that thread there, as a row of
        their own would (a decision naming a deleted thread is stripped).

        `taught` (held_in only): the person said these words as "Remember
        this:" here (the teaching writer). The entry is marked `taught`, and
        on a rule the Host decided about a place only such entries widen it
        (memory_engine.rule_place_clause). A record already held for this
        conversation becomes taught; one with no conversation to name stays
        an ordinary record (no place to keep).

        Returns True when the row still holds it for the caller — recorded,
        or it is the row's own conversation saying it again. Returns False
        when the row is no longer in that state (a thread's delete archived
        it, reopened the goal, brought the row back), or when its thread is
        already deleted and the caller said it in other words; the caller
        then keeps its own copy: stores the candidate, closes the goal,
        retires the row. The row's thread is locked FOR SHARE first, as
        forget_if_deleted does, so a delete in flight finishes before the row
        is read and the record is never written onto a row that delete is
        taking. A failure answers True: the caller then does what it did
        before this record existed. In a takeover it answers False: the
        row's thread is already deleted, so its forget takes the row, and
        the caller's own copy is then the only one that keeps the words."""
        if not stamp or not stamp.get("kind"):
            stamp = dict(UNSCOPED)
        # Taught with no conversation to name: there is no place to keep.
        taught = taught and key == "held_in" and not same_scope(stamp, UNSCOPED)
        owner_key, state = self._HELD[key]
        taking_over = False
        same = lambda a: (isinstance(a, dict)  # noqa: E731
                          and a.get("kind") == stamp.get("kind")
                          and str(a.get("ref") or "") == str(stamp.get("ref") or ""))
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    owner = await conn.fetchval(
                        f"SELECT metadata->'{owner_key}' FROM entity_memory"
                        " WHERE id = $1 AND entity_id = $2", row_id, entity_id)
                    owner = json.loads(owner) if isinstance(owner, str) else owner
                    ref = str((owner or {}).get("ref") or "") if isinstance(owner, dict) else ""
                    owner_deleted = False
                    if isinstance(owner, dict) and owner.get("kind") == "thread":
                        try:
                            _uuid.UUID(ref)
                            owner_deleted = bool(await conn.fetchval(
                                "SELECT deleted_at IS NOT NULL FROM circles_threads"
                                " WHERE id = $1::uuid AND entity_id = $2 FOR SHARE",
                                ref, entity_id))
                        except ValueError:
                            pass
                    row = await conn.fetchrow(
                        f"SELECT ({state}) AS held, metadata, content, memory_type,"
                        " NOW() AS now FROM entity_memory WHERE id = $1 AND entity_id = $2"
                        " FOR UPDATE", row_id, entity_id)
                    if row is None or not row["held"]:
                        return False
                    md = row["metadata"]
                    md = json.loads(md) if isinstance(md, str) else dict(md or {})
                    if same(md.get(owner_key)):
                        # Its own conversation saying it again: nothing to
                        # record — but a teaching here marks the place taught
                        # (a row re-homed here by a delete was not taught here).
                        if key == "held_in" and taught and not any(
                                same(e) and e.get("taught") and not e.get("words")
                                for e in (md.get(key) or []) if isinstance(e, dict)):
                            mark = {"kind": stamp["kind"], "ref": str(stamp.get("ref") or ""),
                                    "at": row["now"].isoformat(), "taught": True}
                            if stamp.get("from_turns"):
                                mark["from_turns"] = [int(t) for t in stamp["from_turns"]]
                            await conn.execute(
                                "UPDATE entity_memory SET metadata = jsonb_set(COALESCE(metadata,"
                                " '{}'::jsonb), '{held_in}', $3::jsonb) WHERE id = $1 AND entity_id = $2",
                                row_id, entity_id, json.dumps(
                                    [e for e in (md.get(key) or []) if isinstance(e, dict)] + [mark]))
                        return True
                    locked_owner = md.get(owner_key)
                    if not (isinstance(locked_owner, dict) and isinstance(owner, dict)
                            and locked_owner.get("kind") == owner.get("kind")
                            and str(locked_owner.get("ref") or "") == ref):
                        # The owner changed between the first read and the
                        # locks (the delete's _keep_held moved the row to a
                        # live conversation): that owner's claim stands, and
                        # this one is recorded beside it.
                        owner_deleted = False
                    # A held_in entry says HOW the conversation said it: word
                    # for word (no words of its own: this row's words are
                    # theirs), or in other words, kept on the entry.
                    fresh = dict(stamp)
                    if key == "held_in":
                        # When it was said: the row a delete gives this
                        # conversation is dated then (circles_threads._own_words).
                        fresh["at"] = row["now"].isoformat()
                        if words is not None and words != row["content"]:
                            fresh["words"] = words
                            from mastercode.memory_engine import (
                                MEMORY_TYPES, NEVER_SUPERSEDED_TYPES,
                            )
                            if (memory_type and memory_type != row["memory_type"]
                                    and memory_type in MEMORY_TYPES
                                    and memory_type not in NEVER_SUPERSEDED_TYPES):
                                fresh["type"] = memory_type
                        if isinstance(about, dict) and about.get("kind"):
                            fresh["about"] = {"kind": str(about["kind"]),
                                              "ref": str(about.get("ref") or "")}
                        if taught:
                            fresh["taught"] = True
                    if owner_deleted and fresh.get("words"):
                        # Its thread is already deleted (a late write that
                        # thread's forget will take), and these words are not
                        # the row's: the caller keeps its own copy.
                        return False
                    if owner_deleted and key != "retired_also_in":
                        taking_over = True
                        # Its thread is already deleted: this is a late write
                        # the thread's forget will take (forget_if_deleted).
                        # The caller's conversation takes it over instead, as
                        # if it had stored its own copy. (A retirement is
                        # different: a live second owner holds it beside a
                        # stamp naming a deleted thread.)
                        if key == "held_in":
                            if same_scope(stamp, UNSCOPED):
                                # No conversation to name: the row is left
                                # unscoped, never labelled with a conversation
                                # that does not exist.
                                md.pop("source_context", None)
                            else:
                                md["source_context"] = {"kind": stamp["kind"],
                                                        "ref": str(stamp.get("ref") or "")}
                            if stamp.get("from_turns"):
                                md["from_turns"] = [int(t) for t in stamp["from_turns"]]
                            else:
                                md.pop("from_turns", None)
                            md["rehomed_from"] = owner
                            from mastercode.circles_threads import _taught_elsewhere
                            if same_scope(md.get("about"), owner) and not await _taught_elsewhere(
                                    conn, entity_id, md, str(owner.get("kind")), str(owner.get("ref") or "")):
                                # a decision naming the deleted thread goes
                                for _k in ("about", "scoped_by", "scoped_at"):
                                    md.pop(_k, None)
                            if (fresh.get("about") and "about" not in md
                                    and not same_scope(fresh["about"], owner)):
                                # Said about a place (a provenance-only
                                # record): the caller's own copy would carry
                                # that decision, so the row it takes does.
                                md["about"] = fresh["about"]
                                md["scoped_by"] = "host"
                            if taught:
                                # Taught here: the place is marked as any
                                # teaching place is (memory_engine.rule_reach).
                                mark = {"kind": stamp["kind"], "ref": str(stamp.get("ref") or ""),
                                        "at": row["now"].isoformat(), "taught": True}
                                if stamp.get("from_turns"):
                                    mark["from_turns"] = [int(t) for t in stamp["from_turns"]]
                                md["held_in"] = [e for e in (md.get("held_in") or [])
                                                 if isinstance(e, dict)] + [mark]
                            project = None
                            if stamp["kind"] == "project":
                                try:
                                    project = int(stamp.get("ref"))
                                except (TypeError, ValueError):
                                    project = None
                            await conn.execute(
                                "UPDATE entity_memory SET metadata = $3::jsonb, project_id = $4"
                                " WHERE id = $1 AND entity_id = $2",
                                row_id, entity_id, json.dumps(md), project)
                            # The row is the caller's now, but what the deleted
                            # thread changed with it is still undone: what it
                            # linked, or had merged into it, comes back beside it.
                            from mastercode.circles_threads import restore_after_takeover
                            await restore_after_takeover(conn, entity_id, row_id)
                        else:
                            md["resolved_in"] = dict(stamp)
                            await conn.execute(
                                "UPDATE entity_memory SET metadata = $3::jsonb"
                                " WHERE id = $1 AND entity_id = $2",
                                row_id, entity_id, json.dumps(md))
                        return True
                    # One entry per conversation, wording and mark: the same
                    # words again add their messages; other words, or a record
                    # that reads where a provenance-only one does not, are
                    # their own entry.
                    entries = [e for e in (md.get(key) or []) if isinstance(e, dict)]
                    for e in entries:
                        if (same(e) and e.get("words") == fresh.get("words")
                                and e.get("about") == fresh.get("about")):
                            turns = {int(t) for t in (e.get("from_turns") or [])}
                            turns |= {int(t) for t in (stamp.get("from_turns") or [])}
                            if turns:
                                e["from_turns"] = sorted(turns)
                            if fresh.get("at") and not e.get("at"):
                                e["at"] = fresh["at"]   # the first time it was said
                            if fresh.get("taught"):
                                e["taught"] = True      # taught here now
                            break
                    else:
                        entries.append(fresh)
                    await conn.execute(
                        "UPDATE entity_memory SET metadata = jsonb_set(COALESCE(metadata,"
                        " '{}'::jsonb), $3::text[], $4::jsonb) WHERE id = $1 AND entity_id = $2",
                        row_id, entity_id, [key], json.dumps(entries))
            return True
        except Exception as e:  # noqa: BLE001 - a missing record never blocks a pass
            log.warning("held-in record failed (entity=%s row=%s): %s", entity_id, row_id, e)
            # A takeover that failed (a deadlock with a concurrent delete, say)
            # rolled back: the late row is still the deleted thread's and its
            # forget takes it, so the caller keeps its own copy.
            return not taking_over

    async def _row_state(self, entity_id: int, row_id) -> str:
        """'live', 'dropped' (a Circles thread retired it with no replacement)
        or 'gone' (archived, replaced, or retired by anything else)."""
        try:
            return str(await self.pool.fetchval(
                "SELECT CASE WHEN status <> 'active' THEN 'gone'"
                "            WHEN t_valid_until IS NULL OR t_valid_until > NOW() THEN 'live'"
                "            WHEN resolution_pointer IS NULL"
                "                 AND metadata->'superseded_in'->>'kind' = 'thread' THEN 'dropped'"
                "            ELSE 'gone' END"
                "  FROM entity_memory WHERE id = $1 AND entity_id = $2",
                row_id, entity_id) or "gone")
        except Exception:  # noqa: BLE001 - unreadable: act as decided
            return "live"

    async def _link_or_retire(self, entity_id: int, dropped_id: int, memory_id: int,
                              stamp: Optional[dict], *, carry_about: bool = True) -> bool:
        """Link a dropped row to the row that replaces it; if the thread that
        dropped it was deleted in the meantime and brought it back, retire it
        here instead — the replacement stands either way. `carry_about`: the
        replacement takes the dropped row's decision (not for a rule: a fresh
        teaching is undecided, memory_engine.rule_reach)."""
        if await self._link(entity_id, dropped_id, memory_id, carry_about=carry_about):
            return True
        if await self._row_state(entity_id, dropped_id) == "live":
            return await self._supersede(entity_id, dropped_id, memory_id, superseded_in=stamp,
                                         carry_about=carry_about)
        return False

    async def _supersede_live(self, entity_id: int, target_id: int, memory_id: int,
                              content: str, memory_type: str,
                              embedding_str: Optional[str], stamp: Optional[dict]
                              ) -> Optional[int]:
        """Retire `target_id` in favour of `memory_id`. If a thread's delete
        archived the target after it was chosen (and brought back the row it
        had replaced), what holds that place now is retired instead, by the
        batch path's own correction rule. Returns the id retired, or None."""
        if await self._supersede(entity_id, target_id, memory_id, superseded_in=stamp):
            return target_id
        fresh = [c for c in await self._find_candidates(
                     entity_id, content, embedding_str, memory_type)
                 if c.get("memory_type") == memory_type and c["id"] != memory_id]
        retarget = _supersession_target(fresh, content, embedding_str)
        log.info("supersede: target %s left the live set before the write (entity %d) — %s",
                 target_id, entity_id, f"now {retarget}" if retarget else "nothing retired")
        if retarget is not None and await self._supersede(
                entity_id, retarget, memory_id, superseded_in=stamp):
            # The new row's lineage names the row actually replaced.
            await self.pool.execute(
                "UPDATE entity_memory SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb),"
                " '{supersedes_id}', to_jsonb($3::int)) WHERE id = $1 AND entity_id = $2",
                memory_id, entity_id, int(retarget))
            return retarget
        await self.pool.execute(
            "UPDATE entity_memory SET metadata = COALESCE(metadata, '{}'::jsonb) - 'supersedes_id'"
            " WHERE id = $1 AND entity_id = $2", memory_id, entity_id)
        return None

    async def _link(self, entity_id: int, dropped_id: int, memory_id: int, *,
                    carry_about: bool = True) -> bool:
        """Point a dropped row at the row that replaces it. Its stamp stays:
        the thread that dropped it still owns that retirement
        (circles_threads._restore_retired). metadata.linked_by says the
        pointer is this replacement, not the merge pass's fold: the replacing
        conversation's own action (circles_threads._chain)."""
        res = await self.pool.execute(
            "UPDATE entity_memory SET resolution_pointer = $3,"
            " metadata = COALESCE(metadata, '{}'::jsonb)"
            "            || jsonb_build_object('linked_by', $3::int),"
            " updated_at = NOW()"
            " WHERE id = $2 AND entity_id = $1 AND resolution_pointer IS NULL"
            " AND t_valid_until IS NOT NULL", entity_id, int(dropped_id), memory_id)
        if str(res).endswith(" 0"):
            return False
        if carry_about:
            await self._carry_about(entity_id, [int(dropped_id)], [int(memory_id)])
        return True

    async def link_dropped(self, entity_id: int, content: str, memory_type: str,
                           embedding_str: Optional[str], memory_id: int, *,
                           corrects: bool, stamp: Optional[dict] = None,
                           rule: bool = False) -> Optional[int]:
        """Point a row a Circles thread DROPPED (retired, no replacement) at the
        row just stored, when that row replaces it: it says the same thing
        word for word, or it was flagged as a correction and found no live
        target (batch_consolidator's own rule, applied to the dropped rows).

        Had the thread never existed, the dropped row would have been live
        and this correction would have retired it. Without the link, deleting
        the thread brings the dropped row back beside the correction
        (circles_threads._restore_retired). The dropped row keeps the stamp of
        the thread that dropped it, so the link never brings it back while that
        thread exists. Never raises; the linked id.

        `rule`: the row is a rule (memory_engine.RULE_KINDS), and
        what it replaces is a dropped rule of either shape; any other row
        never links a rule, so a preference in a skill's words leaves the
        skill where it is."""
        try:
            from mastercode.memory_engine import not_rule_sql, rule_row_sql
            if rule:
                kind_sql, kind_args = f" AND {rule_row_sql()}", ()
            else:
                kind_sql, kind_args = f" AND memory_type = $4 AND {not_rule_sql()}", (memory_type,)
            target = await self.pool.fetchval(
                "SELECT id FROM entity_memory WHERE entity_id = $1 AND content = $2"
                + kind_sql +
                " AND status = 'active' AND t_valid_until <= NOW()"
                " AND resolution_pointer IS NULL"
                " AND metadata->'superseded_in'->>'kind' = 'thread' AND id <> $3"
                " ORDER BY t_valid_until DESC LIMIT 1", entity_id, content, memory_id,
                *kind_args)
            if target is None and corrects:
                dropped = [c for c in await self._find_candidates(
                               entity_id, content, embedding_str, memory_type,
                               k=NEAREST_PER_CANDIDATE, dropped=True)
                           if c.get("memory_type") == memory_type]
                target = _supersession_target(dropped, content, embedding_str)
            # The caller's stamp: if the dropped row came back meanwhile and is
            # retired here instead, the retirement names who did it.
            if target is None or not await self._link_or_retire(
                    entity_id, int(target), memory_id, stamp or UNSCOPED, carry_about=not rule):
                return None
            return int(target)
        except Exception as e:  # noqa: BLE001
            log.warning("dropped-row link failed (entity=%s row=%s): %s", entity_id, memory_id, e)
            return None

