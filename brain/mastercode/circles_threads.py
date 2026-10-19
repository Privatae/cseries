"""Circles self-threads — the rows, the descriptor, and the two user acts
(topic markers and Split).

A Circles thread is what a project already is in the Workbench: a scope with an id that
every turn written inside it is stamped with. Nothing is cached on it and
nothing is shared through it — it is not a session.

THE LEGACY SCOPE IS NOT A ROW. Every Circles chat ever written lives in
``app:circles`` (memory_engine.derive_source_context:1052-1053). That scope is
not migrated: it appears in the API under the sentinel id "original" with the
title "the original conversation" and keeps every behaviour it has today.
``thread_id`` absent or null means the same thing. The client's historic value
for the self thread — the literal string "self", from the conversations
payload (server/routes/circles.py:1050) — is accepted as the same sentinel so
an un-updated client keeps working.

ONE SCOPE TRUTH. Every turn-row count and filter here goes through
memory_engine._turn_row_where (or count_scope_turns, which is built on it).
A hand-written scope clause is how the consolidator's stats and the recall
ladder drift apart.
"""

from __future__ import annotations

import json
import logging
import uuid as _uuid

from mastercode import memory_engine

log = logging.getLogger("privatae.circles_threads")

# The sentinel id for the un-migrated app:circles conversation, in the API and
# in every body that carries a thread id.
LEGACY_THREAD_ID = "original"

LEGACY_TITLE = "The original conversation"

# What circles-web sends today as the self thread's id (the `id` field of the
# self pane, server/routes/circles.py:1050). Treated as the legacy sentinel so
# a client that has not learned about threads yet keeps landing in the
# original conversation rather than in a 400.
_LEGACY_ALIASES = {LEGACY_THREAD_ID, "self", ""}

# Reasons circles_thread_links accepts (the table's CHECK constraint).
LINK_REASONS = ("recall", "split")


def is_legacy(thread_id: str | None) -> bool:
    """True when this id means the un-migrated app:circles conversation:
    absent, null, "original", or the client's historic "self"."""
    if thread_id is None:
        return True
    return str(thread_id).strip().lower() in _LEGACY_ALIASES


def canonical_id(thread_id: str | None) -> str | None:
    """The id every scope key must be composed from: None for the legacy
    sentinel, the CANONICAL uuid string when the value is uuid-shaped, and
    the value untouched otherwise.

    uuid.UUID accepts an uppercase or hyphenless form, and Postgres
    canonicalises on ``::uuid``, so a non-canonical id still finds the row —
    but every scope key built by string formatting (the descriptor ref in
    memory_engine._turn_row_where's thread branch, turn_registry's
    compose_thread_key) is a byte compare. A reader that composes its key
    from the raw query parameter while the writer composes it from the row's
    id looks at a different lane and finds nothing. An unparseable value is
    returned as given rather than folded to None: it belongs to no thread,
    and mapping it to None would silently answer for the ORIGINAL
    conversation instead.
    """
    if is_legacy(thread_id):
        return None
    try:
        return str(_uuid.UUID(str(thread_id)))
    except (ValueError, AttributeError, TypeError):
        return str(thread_id)


def descriptor_for(thread_id: str | None) -> dict:
    """The turn-row provenance descriptor for a Circles thread id.

    Built through memory_engine.derive_source_context so the writer and the
    reader collapse identically — deriving it by hand here is how a scope
    that can be written to and never read back gets created (the drift
    derive_prompt_log_context:1068-1074 records).
    """
    if is_legacy(thread_id):
        # interaction_context "chat" is what the circles surface carries;
        # derive_source_context maps "" | "chat" | "circles" to app:circles.
        return memory_engine.derive_source_context("chat")
    # Canonicalised HERE, so every scope built from a thread id — the read,
    # Clear, markers, Split, the tool boundary, the routes' raw path params —
    # compares the same bytes the writer stamped, whatever form the caller
    # handed in.
    return memory_engine.derive_source_context(
        "chat", thread_id=canonical_id(thread_id))


def legacy_conversation_where(args: list) -> list[str]:
    """The WHERE clause for what the original conversation SHOWS — the UI
    read, its Clear, the replay window scan, and a Split off it.

    This is the app_id path of memory_engine._turn_row_where (app_id
    'circles' or none, project-less, not a thread's row), which is what the
    Circles UI has filtered on since before the descriptor existed. It differs
    from the descriptor path recall uses in two measured ways, both kept on
    purpose (live, read-only psql on every running entity):

      * NARROWER on app_id: a hidden-lane timer reply is written with app_id
        'scheduled_task' and an app:circles descriptor (offline_task_runner),
        so recall sees it but the conversation never renders it. Widening to the descriptor clause showed those
        replies twice (the hidden turn plus the runner's own report) and let
        Clear archive them.
      * WIDER on supersession: the conversation has always shown turn rows
        whose t_valid_until has passed, while recall never did. Consolidation
        once superseded turn rows (through consolidate_new and
        _merge_duplicates), and older Hosts still hold such rows. Hiding them would make those
        messages vanish from the conversation. Hence live_only=False.

    `args` carries $1=entity_id on entry, as for _turn_row_where.
    """
    return memory_engine._turn_row_where(
        args, session_id="", app_id="circles", live_only=False)


# Replay and A/B runs write turn rows tagged app_id 'circles-replay' /
# 'circles-ab' into whichever conversation they reconstruct. A fired scheduled
# wake-up (app_id 'scheduled_task', offline_task_runner) now THINKS in the
# thread it was set in: its rows carry that thread's
# descriptor so the thread is its chain of thought for recall, and the
# runner's own report is the one message the thread shows for it — exactly
# what the original conversation's app_id clause already does for it. The read, the
# replay window scan, the list's turn count and Split's move set filter them
# out with this one clause, appended ON TOP of conversation_where — it is not
# a scope of its own. On the original conversation it is redundant (that
# clause's app_id term already excludes them); on a thread it is what keeps a
# regenerate's rows out of the transcript. Clear does not use it: a thread's
# Clear takes those rows too. COALESCE so a row with no app_id at all is kept
# (NULL NOT IN (...) would drop it).
NOT_REPLAY_ROW = memory_engine.NOT_HIDDEN_LANE_ROW


def conversation_where(args: list, thread_id: str | None) -> list[str]:
    """The WHERE clause for what a Circles conversation SHOWS — the original
    conversation or a thread. The UI read, its Clear, the replay window scan,
    Split's move set and the marker anchor all use this and nothing else, so
    what the user sees, moves, clears and is anchored on is one row set.

    A conversation is the record of what was said, so it never applies
    recall's supersession filter (live_only=False on both arms): a superseded
    turn row is hidden from recall, not from the transcript. Without that on
    the thread arm, a Split off the original conversation moved superseded
    rows into a thread that then filtered them out — visible nowhere
    (measured against a smoke database).
    """
    if is_legacy(thread_id):
        return legacy_conversation_where(args)
    return memory_engine._turn_row_where(
        args, session_id="", source_context=descriptor_for(thread_id),
        live_only=False,
        # The conversation's row set includes a wake-up's hidden reply rows,
        # so Clear archives them; shown_where's NOT_REPLAY_ROW keeps them off
        # the transcript, the counts and Split's move set.
        with_wakeup_rows=True)


def shown_where(args: list, thread_id: str | None) -> list[str]:
    """conversation_where plus NOT_REPLAY_ROW: exactly the rows the transcript
    RENDERS. The read, the replay window scan, the list's turn count, Split's
    move set and the marker anchor all use this one helper, so a row set
    that differs from what the user sees cannot creep into any of them (the
    marker anchor was the one site left on conversation_where alone, and a
    regenerate's hidden row could then anchor a marker no message carried).
    Clear is the one consumer that wants conversation_where without the
    exclusion — a thread's Clear takes a regenerate's rows too."""
    where = conversation_where(args, thread_id)
    where.append(NOT_REPLAY_ROW)
    return where


def _is_uuid(value) -> bool:
    try:
        _uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _as_dict(md):
    """entity_memory.metadata as a dict — asyncpg hands back jsonb as a str
    on some connections and as a dict on others."""
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    return md if isinstance(md, dict) else {}


async def _turn_count(pool, entity_id: int, thread_id: str | None) -> int:
    """How many messages this conversation renders — shown_where on either
    arm, so the number the list reports is the number the user would see
    (superseded rows in, a regenerate's rows and the hidden lane out). This
    is deliberately not recall's count."""
    try:
        args: list = [entity_id]
        where = shown_where(args, thread_id)
        n = await pool.fetchval(
            f"SELECT count(*) FROM entity_memory WHERE {' AND '.join(where)}",
            *args)
        return int(n or 0)
    except Exception as exc:  # noqa: BLE001 - a count never fails a list
        log.warning("thread turn count failed (entity %s, thread %s): %s",
                    entity_id, thread_id, exc)
        return 0


def _row_dict(row, turn_count: int) -> dict:
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "topic": row["topic"],
        "created_at": _iso(row["created_at"]),
        "closed_at": _iso(row["closed_at"]),
        "origin": row["origin"],
        "parent_thread_id": (
            str(row["parent_thread_id"]) if row["parent_thread_id"] else None),
        "turn_count": turn_count,
        # Turn types: the type this thread is bound to, so the
        # Circles list can name it. Absent from rows the create/patch
        # RETURNING shapes produce (they carry no join); None there.
        "type_id": (str(row["type_id"]) if _rget(row, "type_id") else None),
        "type_name": _rget(row, "type_name"),
    }


def _rget(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


# ---- Read ----

async def _turn_counts_by_thread(pool, entity_id: int) -> dict[str, int]:
    """Every THREAD scope's rendered turn count in ONE grouped query, keyed by
    the descriptor ref. The legacy scope is not in here — it is not a thread
    ref and its clause is a different one (legacy_conversation_where).

    This is shown_where's thread arm with the per-thread ref equality lifted
    into a GROUP BY: entity + turn + active + project_id IS NULL +
    source_context.kind='thread' (memory_engine._turn_row_where's thread
    branch), no t_valid_until filter (live_only=False — a transcript shows
    superseded rows), plus NOT_REPLAY_ROW. It is the one hand-written scope
    clause in this module and it exists because N threads used to cost N full
    counts on every list; scripts/test_circles_threads.py asserts it agrees
    with _turn_count per thread, so a drift from _turn_row_where fails the
    gate rather than quietly under-counting a list.
    """
    try:
        rows = await pool.fetch(
            f"""
            SELECT metadata->'source_context'->>'ref' AS ref, count(*) AS n
              FROM entity_memory
             WHERE entity_id = $1
               AND memory_type = 'turn'
               AND status = 'active'
               AND project_id IS NULL
               AND metadata->'source_context'->>'kind' = 'thread'
               AND {NOT_REPLAY_ROW}
             GROUP BY 1
            """,
            entity_id,
        )
    except Exception as exc:  # noqa: BLE001 - a count never fails a list
        log.warning("thread turn counts failed (entity %s): %s", entity_id, exc)
        return {}
    return {str(r["ref"]): int(r["n"] or 0) for r in rows if r["ref"]}


# ---- Page marks ----
#
# What the book's tabs read for each of the Host's pages, the original
# conversation and every thread, in the shape the Workbench's projects list
# gives them (server/routes/projects.py _PAGE_MARKS_COLS):
#   seen_at           the page's mark (circles_page_marks, kind 'self'), set by
#                     PUT /api/circles/thread/self/{id}/seen; NULL while unset.
#   last_activity_at  the newest of the transcript's rows, by when each was
#                     last written; a thread with none reads its own creation.
#   last_landed_at    the newest thing the Host wrote on the page: its
#                     replies, a scheduled task's report
#                     (offline_task_runner.post_to_origin_thread) and an
#                     agent's delivered result (session.deliver_agent_result),
#                     all role 'assistant' rows of the transcript. Never the
#                     person's own words, and never a marker row (_NOT_MARKER).
#   unseen_landed_at  what lights the tab: the newest of those that landed
#                     after seen_at, less the replies whose turn token is in
#                     seen_turns (the turns the person watched end there; v217
#                     says why a time cannot tell those apart). NULL while
#                     nothing has, and while seen_at is NULL.
#   waiting_on_you    the page's open tickets, the cards the chat raises over
#                     it (GET /api/tickets?status=open&asked_in=thread:<id>,
#                     tickets._room_filter); ask_cards_open is the same count,
#                     as the Workbench names it. Circles has no Code Approvals.
#
# When a row landed: COALESCE(landed_at, created_at), as the Workbench reads
# it (projects._LANDED_AT_SQL, v216): a reply that ran on, or a held turn
# written over later, lands after its created_at.
_LANDED = "COALESCE(em.landed_at, em.created_at)"
# The rows a split, a connector's pick, the Index's pick and the Circles
# topic pick leave in a conversation (split_thread's marker; thread_pick
# .PICK_MARKER_KIND, INDEX_PICK_MARKER_KIND and TOPIC_MARKER_KIND). Each is
# written for the person's own act, not by the Host, so it never lights a
# tab; the picker's newest exchange leaves the same four out
# (thread_pick._NOT_MARKER).
MARKER_KINDS = ("thread_marker", "connector_pick", "index_pick", "topic_pick")
_NOT_MARKER = ("COALESCE(em.metadata->>'source_kind', '') NOT IN ("
               + ", ".join(f"'{k}'" for k in MARKER_KINDS) + ")")
_HOST_WROTE = f"em.metadata->>'role' = 'assistant' AND {_NOT_MARKER}"
# A thread's transcript rows (shown_where's thread arm, the same lift
# _turn_counts_by_thread makes), with the thread's ref left to the caller.
# Written out, not built from _turn_row_where, because
# migrations/v219_circles_page_marks.sql indexes exactly these rows
# (idx_entity_memory_thread_rows) and the planner uses that index only while
# the text is the same; scripts/test_circles_list_speed_pg.py checks the
# plan, and scripts/test_circles_page_data_pg.py that the rows are
# shown_where's for every thread.
_THREAD_ROWS = (
    "em.entity_id = $1 AND em.memory_type = 'turn' AND em.status = 'active'"
    " AND em.project_id IS NULL"
    " AND (em.metadata->'source_context'->>'kind') = 'thread'"
    " AND COALESCE(em.metadata->>'app_id', '') NOT IN ("
    + ", ".join(f"'{a}'" for a in memory_engine.HIDDEN_LANE_APP_IDS) + ")")
# The Host's own rows among them (idx_entity_memory_thread_replies).
_THREAD_REPLIES = (
    _THREAD_ROWS
    + " AND (em.metadata->>'role') = 'assistant'"
    + " AND " + _NOT_MARKER)
_UNWATCHED = "COALESCE(em.metadata->>'turn_token', '') <> ALL(COALESCE(m.seen_turns, '{}'::text[]))"
# The open tickets asked in each of the Host's pages, keyed as the list keys a
# page: a thread's canonical id, or 'original'. The rooms tickets.
# _room_filter matches: a thread by its id alone; the original conversation
# as Circles' room with neither a thread nor a project.
_OPEN_ASKS = """
    SELECT CASE WHEN COALESCE(tk.metadata->'asked_in'->>'thread_id', '') <> ''
                THEN lower(tk.metadata->'asked_in'->>'thread_id')
                WHEN tk.metadata->'asked_in'->>'app_id' = 'circles'
                     AND COALESCE(tk.metadata->'asked_in'->>'project_id', '') = ''
                THEN 'original' END AS ref,
           count(*) AS n
      FROM entity_tickets tk
     WHERE tk.entity_id = $1 AND tk.status = 'open'
     GROUP BY 1"""


def _marks(row) -> dict:
    """A page's marks as the list and the page route carry them."""
    n = int(_rget(row, "waiting_on_you") or 0)
    return {
        "seen_at": _iso(_rget(row, "seen_at")),
        "last_activity_at": _iso(_rget(row, "last_activity_at")),
        "last_landed_at": _iso(_rget(row, "last_landed_at")),
        "unseen_landed_at": _iso(_rget(row, "unseen_landed_at")),
        "waiting_on_you": n,
        "ask_cards_open": n,
    }


async def list_threads(pool, entity_id: int) -> list[dict]:
    """Every Circles thread for this entity, newest first. Each carries its
    turn count and its page marks (see "Page marks" above). There is no
    original conversation entry: every turn lands in a
    thread, and the legacy app:circles scope is listed nowhere.

    Two reads, however many threads: the thread rows with each thread's
    marks, and one grouped pass for the threads' counts
    (_turn_counts_by_thread). A thread's
    newest row and newest reply are each one entry of a v219 index, and its
    unseen read starts at its mark in that index, so the marks cost follows
    the number of threads, not their rows. A thread with no rows is absent
    from the group and reads 0, which is what a fresh thread is."""
    out: list[dict] = []
    try:
        rows = await pool.fetch(
            f"""
            WITH oa AS ({_OPEN_ASKS})
            SELECT c.id, c.title, c.topic, c.created_at, c.closed_at, c.origin,
                   c.parent_thread_id, c.type_id, t.name AS type_name,
                   m.seen_at,
                   GREATEST(c.created_at, la.at) AS last_activity_at,
                   lt.at AS last_landed_at,
                   us.at AS unseen_landed_at,
                   COALESCE(oa.n, 0) AS waiting_on_you
              FROM circles_threads c
              LEFT JOIN turn_types t ON t.id = c.type_id AND t.entity_id = c.entity_id
              LEFT JOIN circles_page_marks m
                     ON m.entity_id = c.entity_id AND m.kind = 'self'
                    AND m.ref = c.id::text AND m.reader = ''
              LEFT JOIN LATERAL (
                  SELECT max({_LANDED}) AS at FROM entity_memory em
                   WHERE {_THREAD_ROWS}
                     AND (em.metadata->'source_context'->>'ref') = c.id::text) la ON true
              LEFT JOIN LATERAL (
                  SELECT max({_LANDED}) AS at FROM entity_memory em
                   WHERE {_THREAD_REPLIES}
                     AND (em.metadata->'source_context'->>'ref') = c.id::text) lt ON true
              LEFT JOIN LATERAL (
                  SELECT max({_LANDED}) AS at FROM entity_memory em
                   WHERE {_THREAD_REPLIES}
                     AND (em.metadata->'source_context'->>'ref') = c.id::text
                     AND {_LANDED} > m.seen_at
                     AND {_UNWATCHED}) us ON true
              LEFT JOIN oa ON oa.ref = c.id::text
             WHERE c.entity_id = $1
               AND c.deleted_at IS NULL
             ORDER BY c.created_at DESC, c.id DESC
            """,
            entity_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("thread list failed (entity %s): %s", entity_id, exc)
        return out
    counts = await _turn_counts_by_thread(pool, entity_id)
    for r in rows:
        entry = _row_dict(r, counts.get(str(r["id"]), 0))
        entry.update(_marks(r))
        out.append(entry)
    return out


async def page_marks(pool, entity_id: int, thread_id) -> dict | None:
    """One of the Host's pages in the list's shape, marks included, or None
    when the id is not a live thread of this entity (the legacy sentinel
    included: it is no page). Read by the same SQL as list_threads, so the
    page route and the tabs never disagree."""
    if is_legacy(thread_id):
        return None
    tid = canonical_id(thread_id)
    if not _is_uuid(tid):
        return None
    for entry in await list_threads(pool, entity_id):
        if entry["id"] == tid:
            return entry
    return None


async def get_thread(pool, entity_id: int, thread_id) -> dict | None:
    """One thread in the list shape, or None when the id is not this
    entity's. The legacy sentinel is None too: there is no original
    conversation to resolve to."""
    if is_legacy(thread_id) or not _is_uuid(thread_id):
        return None
    try:
        row = await pool.fetchrow(
            """
            SELECT id, title, topic, created_at, closed_at, origin,
                   parent_thread_id
              FROM circles_threads
             WHERE id = $1::uuid AND entity_id = $2 AND deleted_at IS NULL
            """,
            str(thread_id), entity_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("thread read failed (entity %s, thread %s): %s",
                    entity_id, thread_id, exc)
        return None
    if not row:
        return None
    return _row_dict(row, await _turn_count(pool, entity_id, str(row["id"])))


async def thread_exists_open(pool, entity_id: int, thread_id) -> dict | None:
    """{"id": <canonical uuid str>, "closed_at": …} for a thread this entity
    owns, or None. The legacy sentinel resolves to {"id": None}: the old
    app:circles scope is no page any more (there is no original
    conversation), but a Split may still move its rows into a thread, and
    split_thread reads it here. Every send path refuses the sentinel before
    it gets this far (routes/circles._thread_id_from_body).

    The cheap half of get_thread: existence + closed state and nothing else.
    Every send takes this path (the body's thread_id has to be validated
    before a turn is written into it), and get_thread's turn_count is a full
    count over the thread's rows — a number no send has ever used.
    """
    if is_legacy(thread_id):
        return {"id": None, "closed_at": None}
    if not _is_uuid(thread_id):
        return None
    try:
        row = await pool.fetchrow(
            "SELECT id, closed_at FROM circles_threads "
            " WHERE id = $1::uuid AND entity_id = $2 AND deleted_at IS NULL",
            str(thread_id), entity_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("thread existence check failed (entity %s, thread %s): %s",
                    entity_id, thread_id, exc)
        return None
    if not row:
        return None
    return {"id": str(row["id"]), "closed_at": row["closed_at"]}


# ---- Write ----

async def create_thread(pool, entity_id: int, *, title: str | None = None,
                        origin: str = "user",
                        parent_thread_id: str | None = None) -> dict:
    """Insert a thread row and return it in the list shape (turn_count 0 —
    a new thread's scope has no rows, which is the point: its first turn
    is the low-context call)."""
    if origin not in ("user", "split"):
        raise ValueError("origin must be 'user' or 'split'")
    parent = str(parent_thread_id) if parent_thread_id and _is_uuid(parent_thread_id) else None
    row = await pool.fetchrow(
        """
        INSERT INTO circles_threads (entity_id, title, origin, parent_thread_id)
        VALUES ($1, $2, $3, $4::uuid)
        RETURNING id, title, topic, created_at, closed_at, origin, parent_thread_id
        """,
        entity_id, (title or None), origin, parent,
    )
    return _row_dict(row, 0)


async def patch_thread(pool, entity_id: int, thread_id, *,
                       title: str | None = None,
                       closed: bool | None = None) -> dict:
    """Rename and/or open/close a thread. Both fields are optional and are
    applied only when given, so renaming never reopens a closed thread and
    closing never clears a title.

    Raises ValueError('thread not found') for the legacy sentinel and for an
    id that is not this entity's: the original conversation has no row to
    rename or close, and saying so is better than a silent no-op.
    """
    if is_legacy(thread_id) or not _is_uuid(thread_id):
        raise ValueError("thread not found")
    sets: list[str] = []
    args: list = [str(thread_id), entity_id]
    if title is not None:
        args.append(title.strip() or None)
        sets.append(f"title = ${len(args)}")
    if closed is not None:
        sets.append("closed_at = " + ("NOW()" if closed else "NULL"))
    if not sets:
        row = await get_thread(pool, entity_id, thread_id)
        if row is None:
            raise ValueError("thread not found")
        return row
    row = await pool.fetchrow(
        f"""
        UPDATE circles_threads SET {', '.join(sets)}
         WHERE id = $1::uuid AND entity_id = $2 AND deleted_at IS NULL
        RETURNING id, title, topic, created_at, closed_at, origin, parent_thread_id
        """,
        *args,
    )
    if not row:
        raise ValueError("thread not found")
    return _row_dict(row, await _turn_count(pool, entity_id, str(row["id"])))


# The one status_reason a thread's deletion archives with, shared with the
# late-row guard in the turn writer (memory_engine._archive_if_thread_deleted).
DELETED_REASON = memory_engine.THREAD_DELETED_REASON

# What brain_service.send_message answers a turn addressed to a deleted
# thread; the wake-up runner reads it as the task's cancellation.
DELETED_THREAD_REFUSAL = "This thread was deleted."

# pg_advisory_xact_lock(_FORGET_LOCK, entity_id): one forgetting at a time per
# entity (delete_thread, forget_if_deleted). Each reads which OTHER threads are
# deleted — whose rows it may restore, which conversation may keep a row — and
# two overlapping deletes could not see each other's uncommitted deleted_at.
_FORGET_LOCK = 0x43544844  # "CTHD"


async def live_thread_id(pool, entity_id: int, thread_id):
    """`thread_id`, or None when it names a thread the person deleted. An
    agent's result still addressed to that thread lands in the original
    conversation instead of in a thread nobody can see; a turn addressed to it
    is refused (brain_service.send_message), since the delete cancelled the
    wake-ups set in it. The legacy sentinels and an unreadable answer come
    back unchanged."""
    if is_legacy(thread_id) or not _is_uuid(thread_id):
        return thread_id
    try:
        gone = await pool.fetchval(
            "SELECT deleted_at IS NOT NULL FROM circles_threads "
            " WHERE id = $1::uuid AND entity_id = $2", str(thread_id), entity_id)
    except Exception as exc:  # noqa: BLE001 - a read never blocks a delivery
        log.warning("deleted-thread check failed (entity %s, thread %s): %s",
                    entity_id, thread_id, exc)
        return thread_id
    return None if gone else thread_id


def home_title(text, fallback: str = "New topic") -> str:
    """A topic title from the first line of `text`, at most 60 characters:
    the title thread_pick gives a new topic it starts from a message."""
    first = str(text or "").strip().splitlines()[0].strip() if str(text or "").strip() else ""
    if len(first) > 60:
        first = first[:59].rstrip() + "…"
    return first or fallback


async def home_thread(pool, entity_id: int, thread_id, *, title: str) -> str | None:
    """The Circles thread a write belongs in: `thread_id` when it names a live
    thread of this entity, else a new topic titled `title`.

    There is no original conversation: a scheduled turn goes to whatever
    thread called for it. So a Circles write whose caller named no thread, or
    a thread that is gone (deleted, or not this entity's), gets a topic of its
    own instead of landing in the legacy app:circles scope. Only for writes
    that would land in Circles: a Workbench project's writes keep their
    project. None only when the database cannot be read or written; the
    caller then writes nothing rather than into the legacy scope."""
    if _is_uuid(thread_id) and not is_legacy(thread_id):
        try:
            live = await pool.fetchval(
                "SELECT deleted_at IS NULL FROM circles_threads"
                " WHERE id = $1::uuid AND entity_id = $2", str(thread_id), entity_id)
        except Exception as exc:  # noqa: BLE001 - the caller decides what to do without one
            log.warning("home thread check failed (entity %s, thread %s): %s",
                        entity_id, thread_id, exc)
            return None
        if live:
            return canonical_id(thread_id)
    try:
        row = await create_thread(pool, entity_id, title=home_title(title))
    except Exception as exc:  # noqa: BLE001
        log.warning("home thread create failed (entity %s): %s", entity_id, exc)
        return None
    return str(row["id"])


async def delete_thread(pool, entity_id: int, thread_id) -> dict:
    """Delete a thread: it leaves every list, and what was said and learned in
    it leaves the Host's memory. Clear (the DELETE /thread route) archives the
    turn rows only, so the summaries, goals and facts consolidation derived in
    the thread kept steering the Host — and open goals are read by every
    thread (batch_consolidator._open_goals). In one transaction this:

      * archives the turn rows, the set Clear takes (conversation_where), and
        marks them extracted so the consolidator never mines them;
      * archives every other row learned in it: whose descriptor is this
        thread's, whatever its type (summary, goal, fact, a taught rule, …),
        or whose metadata.from_turns are all this thread's messages;
      * undoes what it retired elsewhere (_restore_retired) and reopens a goal
        its conversation closed (metadata.resolved_in). The delete undoes everything the thread changed and keeps what other
        conversations said, without guessing what the merge pass would have
        done without it. So a fact the thread corrected comes back even when
        another conversation repeated the correction in its own words; both
        stay live until one is corrected;
      * cancels the wake-ups set in it that have not fired (wakeups_set_in);
      * frees a turn type bound to it (turn_types.thread_id), rewrites the
        split marker that points at it, and stamps deleted_at (closed_at too).

    Archived, never erased: status='archived' with status_reason
    DELETED_REASON, the filter every recall and prompt loader applies
    (memory_retrieval._canonical_where: status = 'active'). A turn row that
    lands in the thread afterwards is archived on arrival
    (memory_engine._archive_if_thread_deleted) and is never mined
    (turn_consolidator.TURN_QUEUE_WHERE); what a consolidation or a skill
    event already under way writes after it is taken by forget_if_deleted.
    Returns {"id", "messages", "learned", "restored", "reopened", "kept",
    "cancelled"} — the counts — and "cancelled_ids", the wake-ups it
    cancelled.

    What it leaves, on purpose: a stated preference (the person's own like,
    dislike or ban, which they list and lift themselves), a drift correction
    (a tone nudge built from drift numbers, carrying nothing said in the
    thread), and an agent's result, which still reaches the original
    conversation (live_thread_id) because the person asked for it. A row
    learned from messages later split into this thread before
    metadata.from_turns existed carries no record of them and stays with the
    thread it was learned in; so does a record that names no messages — a
    skill the Host marked outdated, or a memory it curated with memory_curate
    — when the conversation is split or adopted with a time window, because
    such a record names the thread and not the messages. A conversation
    repeating its OWN row is not recorded (MemoryConsolidator.note_held):
    stamping it with that pass's messages would credit a later pass that only
    re-read earlier lines, and so keep what a deleted thread said. So when a
    Split or a windowed adopt moves the repeating messages away and the
    conversation that kept the row is deleted, the repetition goes with it.
    A pass that marks as already known something it only read in its prompt
    (another thread's goal, say) is recorded as having said it.

    Raises ValueError('thread not found') for the legacy sentinel, an id that
    is not this entity's, or a thread already deleted. The original
    conversation is cleared, never deleted: what was learned in it is most of
    what the Host knows about its person."""
    if is_legacy(thread_id) or not _is_uuid(thread_id):
        raise ValueError("thread not found")
    tid = canonical_id(thread_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            # First, before any read: an earlier delete of this entity is
            # committed and visible from here on (_FORGET_LOCK).
            await conn.execute("SELECT pg_advisory_xact_lock($1, $2)",
                               _FORGET_LOCK, int(entity_id))
            row = await conn.fetchrow(
                "UPDATE circles_threads"
                "   SET deleted_at = NOW(), closed_at = COALESCE(closed_at, NOW())"
                " WHERE id = $1::uuid AND entity_id = $2 AND deleted_at IS NULL"
                " RETURNING id",
                tid, entity_id)
            if row is None:
                raise ValueError("thread not found")
            out = await _forget(conn, entity_id, tid)
            # A wake-up set here would run a full Host turn in the original
            # conversation and leave what it learned there, where no delete
            # reaches. One that is firing now finishes that run and is not
            # re-armed (offline_task_runner._finalize).
            cancelled = await conn.fetch(
                "UPDATE calendar_events SET status = 'cancelled', updated_at = NOW(),"
                "       cancelled_reason = 'the thread it was set in was deleted'"
                f" WHERE {_WAKEUP_IN_THREAD} RETURNING id",
                entity_id, tid)
            # A request held for the person's next message in this thread has
            # no next message now: it is inert (cancelled) and stops being
            # looked for.
            await conn.execute(
                "UPDATE calendar_events SET updated_at = NOW(),"
                "       cancelled_reason = 'the thread it was set in was deleted'"
                " WHERE entity_id = $1 AND event_type = 'scheduled_task'"
                "   AND status = 'cancelled' AND cancelled_reason = $3"
                "   AND lower(regexp_replace(COALESCE(payload->'origin'->>'thread_id', ''),"
                "       '[^0-9a-fA-F]', '', 'g')) = replace($2, '-', '')",
                entity_id, tid, HELD_FOR_PERSON_REASON)
            # A turn type whose thread this was gets a new one on its next use
            # (turn_types.ensure_type_thread), never this one back.
            await conn.execute(
                "UPDATE turn_types SET thread_id = NULL, updated_at = now()"
                " WHERE entity_id = $1 AND thread_id = $2::uuid", entity_id, tid)
            # The marker a split left in the thread it came from said "nothing
            # was deleted; the conversation continues in that thread".
            await conn.execute(
                "UPDATE entity_memory"
                "   SET content = '→ Moved ' || COALESCE(metadata->'marker'->>'moved_turns', 'some')"
                "                 || ' messages into a thread that was later deleted.',"
                "       metadata = jsonb_set(metadata, '{marker,deleted}', 'true'::jsonb)"
                " WHERE entity_id = $1 AND metadata->>'source_kind' = 'thread_marker'"
                "   AND metadata->'marker'->>'thread_id' = $2",
                entity_id, tid)
    out["cancelled"] = len(cancelled)
    # For the route: a wake-up firing now runs in its own turn lane, which
    # stopping the thread's reply does not reach.
    out["cancelled_ids"] = [int(r["id"]) for r in cancelled]
    log.info("thread deleted (entity %s, thread %s): %d messages and %d learned rows "
             "archived, %d kept by a live conversation, %d retired rows restored, "
             "%d goals reopened, %d wake-ups cancelled", entity_id, tid,
             out["messages"], out["learned"], out["kept"], out["restored"],
             out["reopened"], out["cancelled"])
    return {"id": tid, **out}


# A wake-up set in a thread (payload.origin.thread_id, written by
# runtime/tools.py schedule_task) that has not finished. The stored id is in
# whatever form the turn carried; comparing the hex digits alone matches it
# against the canonical id ($2) without a cast that a malformed value would
# fail.
# The cancelled_reason of a request the identity hold is keeping for the
# person's next ordinary message (runtime/identity_hold.py). The row stays
# 'cancelled', so nothing that lists or runs scheduled work sees it; deleting
# the thread clears the reason so it is not looked for any more.
HELD_FOR_PERSON_REASON = "held_for_person"

_WAKEUP_IN_THREAD = (
    "entity_id = $1 AND event_type = 'scheduled_task'"
    " AND status IN ('scheduled', 'running')"
    " AND lower(regexp_replace(COALESCE(payload->'origin'->>'thread_id', ''),"
    " '[^0-9a-fA-F]', '', 'g')) = replace($2, '-', '')")


async def wakeups_set_in(pool, entity_id: int, thread_id) -> list[dict]:
    """The wake-ups deleting this thread cancels, soonest first, so the confirm
    dialog can name them: [{"id", "title", "fire_at", "recurrence"}]. [] for
    the original conversation or a value that is not a thread id."""
    if is_legacy(thread_id) or not _is_uuid(thread_id):
        return []
    rows = await pool.fetch(
        "SELECT id, title, start_time, recurrence FROM calendar_events"
        f" WHERE {_WAKEUP_IN_THREAD} ORDER BY start_time ASC NULLS LAST",
        entity_id, canonical_id(thread_id))
    return [{"id": int(r["id"]), "title": r["title"] or "",
             "fire_at": r["start_time"].isoformat() if r["start_time"] else None,
             "recurrence": r["recurrence"]} for r in rows]


async def _forget(conn, entity_id: int, tid: str) -> dict:
    """The memory half of a delete, on a connection inside a transaction that
    holds the thread row: archive its turn rows and what was learned from
    them, restore what they retired, reopen the goals they closed. Idempotent
    (a second run finds nothing live left to archive), which is what lets
    forget_if_deleted repeat it for a write that landed after the delete.

    The measure is the memory as it would be had the thread never existed. A
    row the thread learned that a live conversation also said (metadata.
    held_in: that conversation's copy was dropped as a duplicate of it) moves
    to that conversation instead of being archived when it said the row's own
    words (_keep_held); said in other words, that conversation gets a row of
    its own words (_own_words), so the thread's extra words leave with it. A
    goal a live conversation also completed (metadata.resolved_also_in) stays
    closed under that conversation (_keep_held)."""
    scope = descriptor_for(tid)
    kind, ref = scope["kind"], str(scope["ref"])
    args: list = [entity_id]
    where = conversation_where(args, tid)
    args.append(DELETED_REASON)
    turns = await conn.fetch(
        "UPDATE entity_memory"
        f"   SET status = 'archived', status_reason = ${len(args)},"
        "       status_changed_at = NOW()"
        f" WHERE {' AND '.join(where)} RETURNING id",
        *args)
    # Every message the thread holds, whatever its status now (this delete's,
    # an earlier Clear's, an earlier run of this), is what "learned from this
    # thread's messages" is measured against.
    said = [int(r["id"]) for r in await conn.fetch(
        "SELECT id FROM entity_memory"
        " WHERE entity_id = $1 AND memory_type = 'turn' AND project_id IS NULL"
        "   AND metadata->'source_context'->>'kind' = $2"
        "   AND metadata->'source_context'->>'ref' = $3",
        entity_id, kind, ref)]
    kept, in_own_words, void_into = await _keep_held(conn, entity_id, kind, ref, said)
    learned = await conn.fetch(
        "UPDATE entity_memory"
        "   SET status = 'archived', status_reason = $4,"
        "       status_changed_at = NOW()"
        " WHERE entity_id = $1 AND status = 'active'"
        "   AND memory_type <> 'turn'"
        "   AND ((metadata->'source_context'->>'kind' = $2"
        "         AND metadata->'source_context'->>'ref' = $3)"
        "        OR (jsonb_typeof(metadata->'from_turns') = 'array'"
        "            AND jsonb_array_length(metadata->'from_turns') > 0"
        "            AND (metadata->'from_turns') <@ $5::jsonb))"
        " RETURNING id",
        entity_id, kind, ref, DELETED_REASON, json.dumps(said))
    # A correction memory_curate made elsewhere carries the corrected row's
    # place as its decision (scoped_by 'memory_curate'); with that place gone
    # it goes too, as the row it corrected did — read nowhere else, it would
    # start riding the conversation it was made in, where the rule never did.
    learned = list(learned) + list(await conn.fetch(
        "UPDATE entity_memory"
        "   SET status = 'archived', status_reason = $4, status_changed_at = NOW()"
        " WHERE entity_id = $1 AND status = 'active'"
        "   AND metadata->'about'->>'kind' = $2 AND metadata->'about'->>'ref' = $3"
        "   AND metadata->>'scoped_by' = 'memory_curate'"
        f"  AND NOT {_TAUGHT_ELSEWHERE_SQL}"
        " RETURNING id",
        entity_id, kind, ref, DELETED_REASON))
    # A decision (memory_scope) naming this thread is no place to be read any
    # more. A row learned here left above; one said somewhere still live keeps
    # its life and is read there again, undecided — as _keep_held does. A rule
    # the person taught somewhere still live keeps the decision: it then rides
    # only where it was taught (memory_engine.rule_reach), not everywhere.
    await conn.execute(
        "UPDATE entity_memory"
        "   SET metadata = metadata - 'about' - 'scoped_by' - 'scoped_at'"
        " WHERE entity_id = $1 AND metadata->'about'->>'kind' = $2"
        "   AND metadata->'about'->>'ref' = $3"
        f"  AND NOT {_TAUGHT_ELSEWHERE_SQL}",
        entity_id, kind, ref)
    # The same for a provenance-only record (a held_in entry about this
    # thread: note_held's `about`): the conversation it names now reads the
    # row, as its own row said there about this thread would be read there.
    await conn.execute(
        "UPDATE entity_memory"
        "   SET metadata = jsonb_set(metadata, '{held_in}', ("
        "       SELECT jsonb_agg(CASE WHEN e->'about'->>'kind' = $2 AND e->'about'->>'ref' = $3"
        "                             THEN e - 'about' ELSE e END)"
        "         FROM jsonb_array_elements(metadata->'held_in') e))"
        " WHERE entity_id = $1 AND jsonb_typeof(metadata->'held_in') = 'array'"
        "   AND EXISTS (SELECT 1 FROM jsonb_array_elements(metadata->'held_in') e"
        "                WHERE e->'about'->>'kind' = $2 AND e->'about'->>'ref' = $3)",
        entity_id, kind, ref)
    gone = await _deleted_threads(conn, entity_id)
    cache: dict = {}
    given, held_back = await _own_words(conn, entity_id, in_own_words, gone, void_into, cache)
    kept += given
    restored = await _restore_retired(conn, entity_id, kind, ref,
                                      [int(r["id"]) for r in learned], void_into, gone, cache)
    restored_n = len(restored) + held_back
    # Tool results its turns stashed (thread_consolidator.stash_tool_artifacts)
    # are documents recall searches; dismissed like the rest. A file the person
    # uploaded is their own document and stays.
    await conn.execute(
        "UPDATE user_context SET status = 'dismissed', updated_at = now(),"
        "       metadata = COALESCE(metadata, '{}'::jsonb)"
        "                  || jsonb_build_object('dismissed_reason', $4::text)"
        " WHERE entity_id = $1 AND status <> 'dismissed'"
        "   AND metadata->>'artifact' = 'true'"
        "   AND metadata->'source_context'->>'kind' = $2"
        "   AND metadata->'source_context'->>'ref' = $3",
        entity_id, kind, ref, DELETED_REASON)
    reopened = await conn.fetch(
        "UPDATE entity_memory"
        "   SET resolved_at = NULL,"
        "       metadata = COALESCE(metadata, '{}'::jsonb) - 'resolved_in',"
        "       updated_at = NOW()"
        " WHERE entity_id = $1 AND status = 'active' AND resolved_at IS NOT NULL"
        "   AND memory_type = 'goal'"
        "   AND metadata->'resolved_in'->>'kind' = $2"
        "   AND metadata->'resolved_in'->>'ref' = $3"
        " RETURNING id",
        entity_id, kind, ref)
    return {"messages": len(turns), "learned": len(learned),
            "restored": restored_n, "reopened": len(reopened), "kept": kept}


def taught_elsewhere_sql(m: str = "metadata", mt: str = "memory_type", eid: str = "$1",
                         kind: str = "$2", ref: str = "$3") -> str:
    """SQL true for a rule the person taught in some place still live other
    than the thread `kind`/`ref` names: it keeps a decision naming that
    thread when the thread is deleted, and rides where it was taught
    (memory_engine.rule_reach) rather than everywhere. `m`/`mt` are the row's
    metadata and memory_type expressions, `eid` its entity."""
    return (
        f"(({mt} = 'skill_memory' OR COALESCE({m} ? 'rule_kind', false)) AND EXISTS ("
        f" SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof({m}->'held_in') = 'array'"
        f"   THEN {m}->'held_in' ELSE '[]'::jsonb END) e"
        " WHERE e ? 'taught' AND NOT (e ? 'about')"
        "   AND jsonb_typeof(e->'kind') = 'string' AND jsonb_typeof(e->'ref') = 'string'"
        f"   AND NOT (e->>'kind' = {kind} AND e->>'ref' = {ref})"
        "   AND (e->>'kind' <> 'thread' OR EXISTS (SELECT 1 FROM circles_threads tt"
        f"        WHERE tt.entity_id = {eid} AND tt.id::text = e->>'ref' AND tt.deleted_at IS NULL))))")


_TAUGHT_ELSEWHERE_SQL = taught_elsewhere_sql()


async def _taught_elsewhere(conn, entity_id: int, md: dict, kind: str, ref: str) -> bool:
    """The Python side of _TAUGHT_ELSEWHERE_SQL for one row's metadata."""
    from mastercode.memory_engine import rule_taught_places
    if "rule_kind" not in md:
        return False
    for p in rule_taught_places(md):
        if p == {"kind": kind, "ref": str(ref)}:
            continue
        if p["kind"] != "thread":
            return True
        if _is_uuid(p["ref"]) and await conn.fetchval(
                "SELECT deleted_at IS NULL FROM circles_threads WHERE id = $1::uuid AND entity_id = $2",
                p["ref"], entity_id):
            return True
    return False


async def _keep_held(conn, entity_id: int, kind: str, ref: str,
                     said: list[int]) -> tuple[int, list]:
    """Move what a live conversation also said out of the thread before it is
    archived. For each row the thread learned (tagged with it, or learned
    from its messages) whose metadata.held_in names a conversation that still
    exists and said the row's own words, the row becomes that conversation's:
    its source_context and from_turns are the record's, project_id follows a
    project, and the record leaves held_in. A goal the thread closed whose
    metadata.resolved_also_in names a live conversation stays closed, its
    resolved_in now that conversation's. Returns how many rows it kept, and
    the rows whose live conversations said them only in other words, with
    those records, for _own_words once the rows are archived, and the rows it
    re-homed (whose own changes the delete still undoes)."""

    async def live(e) -> bool:
        if not isinstance(e, dict) or not e.get("kind"):
            return False
        if e["kind"] != "thread":
            return True
        if not _is_uuid(e.get("ref")):
            return False
        return bool(await conn.fetchval(
            "SELECT deleted_at IS NULL FROM circles_threads"
            " WHERE id = $1::uuid AND entity_id = $2", str(e["ref"]), entity_id))

    kept = 0
    void_into: set = set()
    in_own_words: list = []
    for key in ("held_in", "resolved_also_in"):
        if key == "held_in":
            # the rows the delete would archive (the learned-row clause below)
            rows = await conn.fetch(
                "SELECT id, metadata FROM entity_memory WHERE entity_id = $1"
                " AND status = 'active' AND memory_type <> 'turn'"
                " AND jsonb_typeof(metadata->'held_in') = 'array'"
                " AND ((metadata->'source_context'->>'kind' = $2"
                "       AND metadata->'source_context'->>'ref' = $3)"
                "      OR (jsonb_typeof(metadata->'from_turns') = 'array'"
                "          AND jsonb_array_length(metadata->'from_turns') > 0"
                "          AND (metadata->'from_turns') <@ $4::jsonb))"
                " FOR UPDATE",
                entity_id, kind, ref, json.dumps(said))
        else:
            # the goals the delete would reopen (the reopen clause below)
            rows = await conn.fetch(
                "SELECT id, metadata FROM entity_memory WHERE entity_id = $1"
                " AND status = 'active' AND resolved_at IS NOT NULL"
                " AND memory_type = 'goal'"
                " AND jsonb_typeof(metadata->'resolved_also_in') = 'array'"
                " AND metadata->'resolved_in'->>'kind' = $2"
                " AND metadata->'resolved_in'->>'ref' = $3 FOR UPDATE",
                entity_id, kind, ref)
        for r in rows:
            md = r["metadata"]
            md = json.loads(md) if isinstance(md, str) else dict(md or {})
            entries = [e for e in (md.get(key) or []) if isinstance(e, dict)]
            # The FIRST live conversation that said it, by when it said it
            # (a record copied here later can carry an earlier time): had the
            # thread never existed, that one would have stored it. Only a
            # conversation that said THIS row's words (word for word) is a
            # home for them; one that said it in other words keeps its own
            # words (_own_words).
            # A provenance-only record (its own `about`: note_held) is a home
            # only once the place it names is this deleted thread: until then
            # the conversation it names does not read the row.
            homes = [(i, e) for i, e in enumerate(entries)
                     if (key != "held_in" or (_verbatim(e) and _mark_void(e, kind, ref)))
                     and await live(e)]
            home = min(homes, key=_first_said)[1] if homes else None
            if home is None:
                if key == "held_in":
                    own = [e for e in entries if not _verbatim(e) and await live(e)]
                    if own:
                        in_own_words.append((int(r["id"]), own))
                continue
            # A taught record stays when its conversation takes the row: that
            # is still where the person taught it (memory_retrieval.
            # HELD_IN_TAUGHT), and a narrowed rule rides there by it.
            rest = [e for e in entries if e is not home or (key == "held_in" and e.get("taught"))]
            where_to = {"kind": home["kind"], "ref": str(home.get("ref") or "")}
            if key == "held_in":
                from mastercode.memory_consolidation import UNSCOPED, same_scope
                if same_scope(where_to, UNSCOPED):
                    # no conversation to name: left unscoped, never labelled
                    # with a conversation that does not exist
                    md.pop("source_context", None)
                else:
                    md["source_context"] = where_to
                if home.get("from_turns"):
                    md["from_turns"] = [int(t) for t in home["from_turns"]]
                else:
                    md.pop("from_turns", None)
                md["rehomed_from"] = {"kind": kind, "ref": ref}
                if (same_scope(md.get("about"), {"kind": kind, "ref": ref})
                        and not await _taught_elsewhere(conn, entity_id, md, kind, ref)):
                    # The Host's decision named the thread now deleted; the
                    # conversation that also said it holds it, undecided.
                    for _k in ("about", "scoped_by", "scoped_at"):
                        md.pop(_k, None)
                project = None
                if where_to["kind"] == "project":
                    try:
                        project = int(where_to["ref"])
                    except (TypeError, ValueError):
                        project = None
            else:
                md["resolved_in"] = dict(home)
            md[key] = rest
            if key == "held_in":
                # Dated when that conversation said it, as its own row would be.
                await conn.execute(
                    "UPDATE entity_memory SET metadata = $3::jsonb, project_id = $4,"
                    " t_valid_from = COALESCE($5::timestamptz, t_valid_from),"
                    " updated_at = NOW() WHERE id = $1 AND entity_id = $2",
                    r["id"], entity_id, json.dumps(md), project, _said_at(home))
                # What the thread changed with it is still undone: the rows it replaced are judged with it as the
                # thread's (_restore_retired, void_into).
                void_into.add(int(r["id"]))
            else:
                await conn.execute(
                    "UPDATE entity_memory SET metadata = $3::jsonb, updated_at = NOW()"
                    " WHERE id = $1 AND entity_id = $2", r["id"], entity_id, json.dumps(md))
            kept += 1
    return kept, in_own_words, void_into


def _verbatim(entry: dict) -> bool:
    from mastercode.memory_consolidation import _verbatim as v
    return v(entry)


def _mark_void(entry: dict, kind: str, ref: str) -> bool:
    """A held_in entry that reads: no provenance-only mark (`about`), or one
    naming the thread being deleted (kind, ref)."""
    about = entry.get("about")
    if not isinstance(about, dict):
        return True
    return about.get("kind") == kind and str(about.get("ref") or "") == str(ref)


def _first_said(item: tuple) -> tuple:
    """Sort key for (position, entry): by when it was said, then position."""
    i, e = item
    at = _said_at(e)
    return (at is None, at.timestamp() if at else 0.0, i)


def _said_at(entry: dict):
    """When a held_in entry was first said (note_held's "at"), or None."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(entry.get("at"))) if entry.get("at") else None
    except ValueError:
        return None


async def _deleted_threads(conn, entity_id: int) -> set:
    return {str(r["ref"]) for r in await conn.fetch(
        "SELECT id::text AS ref FROM circles_threads"
        " WHERE entity_id = $1 AND deleted_at IS NOT NULL", entity_id)}


async def _mem_row(conn, cache: dict, entity_id: int, rid: int, *, lock: bool = False):
    """One entity_memory row for the chain walk (metadata decoded), cached for
    the delete's transaction; `lock` re-reads it FOR UPDATE."""
    rid = int(rid)
    if not lock and rid in cache:
        return cache[rid]
    r = await conn.fetchrow(
        "SELECT id, content, memory_type, status, status_reason, t_valid_until,"
        " resolution_pointer, metadata FROM entity_memory WHERE id = $1 AND entity_id = $2"
        + (" FOR UPDATE" if lock else ""), rid, entity_id)
    d = None
    if r is not None:
        d = dict(r)
        md = d["metadata"]
        d["metadata"] = json.loads(md) if isinstance(md, str) else dict(md or {})
    cache[rid] = d
    return d


def _retired_by(md: dict) -> list:
    """Who retired a row: its stamp (metadata.superseded_in) and each
    conversation that retired it again (metadata.retired_also_in)."""
    owners = [md["superseded_in"]] if isinstance(md.get("superseded_in"), dict) else []
    return owners + [o for o in (md.get("retired_also_in") or []) if isinstance(o, dict)]


def _of_gone(owner: dict, gone: set) -> bool:
    return owner.get("kind") == "thread" and str(owner.get("ref") or "") in gone


def _late_row(row: dict, gone: set) -> bool:
    """A live row still tagged with a deleted thread. The delete archives
    every row tagged with it, so this one was written after the delete by
    work already running, and that work's forget (forget_if_deleted) has not
    taken it yet."""
    owner = row["metadata"].get("source_context")
    return isinstance(owner, dict) and _of_gone(owner, gone)


async def _chain(conn, cache: dict, entity_id: int, row: dict, gone: set,
                 void_into: set) -> str:
    """Why `row` is retired, read along its chain (resolution_pointer), one
    step at a time. The one rule every restore applies (the delete undoes
    what a thread changed and keeps what others did):

      * a live conversation's own action ends it — the row STAYS: a stamp
        naming a conversation that still exists (a correction, a drop,
        metadata.retired_also_in), a live row that replaced a dropped one
        (metadata.linked_by: MemoryConsolidator._link), a correction before
        stamps existed (the next row's metadata.supersedes_id), or an archive
        for any reason but a thread's delete (the person in the Memory app,
        the integrity pass);
      * a deleted thread's action is void: a stamp naming it, one of its
        rows (archived by its delete, or written after it and not yet
        taken by its forget), or a row it wrote that a live conversation
        keeps (`void_into`: it said the row word for word);
      * the merge pass's folds and a restore's twin pointers are seen
        through: the chain continues from the row they point at.

    It comes BACK when it reaches a live row, or the end of the chain,
    having passed a void step and no live action; a row only the merge pass
    retired into a live row stays, MERGED."""
    x, void, seen = row, False, {int(row["id"])}
    while True:
        owners = _retired_by(x["metadata"])
        if any(not _of_gone(o, gone) for o in owners):
            return "stays"
        void = void or bool(owners)
        nxt = x["resolution_pointer"]
        if nxt is None:
            return "back" if void else "stays"
        n = await _mem_row(conn, cache, entity_id, int(nxt))
        if n is None or int(n["id"]) in seen:
            return "stays"
        seen.add(int(n["id"]))
        if n["status"] != "active":
            if n["status_reason"] != DELETED_REASON:
                return "stays"
            void = True
            if n["t_valid_until"] is None:
                return "back"
        elif int(n["id"]) in void_into or _late_row(n, gone):
            void = True
        elif (str(x["metadata"].get("linked_by") or "") == str(n["id"])
              or (not owners
                  and str(n["metadata"].get("supersedes_id") or "") == str(x["id"]))):
            # A replacement of it (a link, or a correction from before stamps
            # existed), made when that row was written. A row a delete or a
            # takeover handed on (metadata.rehomed_from) was written by a
            # thread now deleted: its replacement is void. Otherwise it is a
            # live conversation's.
            if not n["metadata"].get("rehomed_from"):
                return "stays"
            void = True
        if n["status"] == "active" and n["t_valid_until"] is None:
            return "back" if void else "merged"
        x = n


def _twin_rule_sql(n: int) -> str:
    """A twin carries the rule mark the row does (_marks_rule), bound as $n."""
    return f"COALESCE(metadata ? 'rule_kind', false) = ${n}::boolean"


def _marks_rule(md) -> bool:
    """A row marked a rule (memory_engine.RULE_KINDS). A twin is one only
    when the row is: a rule and a plain preference in the same words are two
    rows, and holding one on the other would make a rule retirable, or a
    plain preference a rule."""
    return isinstance(md, dict) and "rule_kind" in md


async def _live_twin(conn, entity_id: int, row: dict):
    """A live row of the same type, the same words and the same rule mark:
    one live copy."""
    return await conn.fetchval(
        "SELECT id FROM entity_memory WHERE entity_id = $1 AND id <> $2"
        " AND content = $3 AND memory_type = $4 AND status = 'active'"
        " AND (t_valid_until IS NULL OR t_valid_until > NOW())"
        " AND " + _twin_rule_sql(5) +
        " ORDER BY id LIMIT 1", entity_id, int(row["id"]), row["content"], row["memory_type"],
        _marks_rule(row["metadata"]))


async def _bring_back(conn, entity_id: int, row, said: list) -> None:
    """Restore a retired row as _undo does, adding `said` (held_in records of
    the conversations whose words it holds)."""
    md = row["metadata"]
    md = json.loads(md) if isinstance(md, str) else dict(md or {})
    md.pop("superseded_in", None)
    md.pop("retired_also_in", None)
    md.pop("linked_by", None)
    if said:
        md["held_in"] = [x for x in (md.get("held_in") or []) if isinstance(x, dict)] + said
    await conn.execute(
        "UPDATE entity_memory SET t_valid_until = NULL, resolution_pointer = NULL,"
        " metadata = $3::jsonb, updated_at = NOW() WHERE id = $1 AND entity_id = $2",
        int(row["id"]), entity_id, json.dumps(md))


async def _undo(conn, entity_id: int, seeds: list, gone: set, void_into: set,
                cache: dict) -> list:
    """Every retired row whose chain runs into `seeds` (the rows a delete
    archived or re-homed, the rows stamped with the deleted thread) is judged
    by _chain, in id order, each under a row lock. One that comes BACK is
    restored — unless its words are already live in a row of its type and
    rule mark (_live_twin): then
    it points at that copy and stays retired, so it comes back if that copy
    later goes. Returns the restored rows."""
    if not seeds:
        return []
    ids = [int(r["id"]) for r in await conn.fetch(
        """
        WITH RECURSIVE up AS (
            SELECT s AS id FROM unnest($2::int[]) AS s
            UNION
            SELECT m.id
              FROM entity_memory m JOIN up u ON m.resolution_pointer = u.id
             WHERE m.entity_id = $1
               AND ((m.status = 'active' AND m.t_valid_until IS NOT NULL)
                    OR (m.status = 'archived' AND m.status_reason = $3))
        )
        SELECT DISTINCT m.id FROM up JOIN entity_memory m ON m.id = up.id
         WHERE m.entity_id = $1 AND m.status = 'active' AND m.t_valid_until IS NOT NULL
         ORDER BY m.id
        """, entity_id, [int(s) for s in seeds], DELETED_REASON)]
    restored = []
    for rid in ids:
        row = await _mem_row(conn, cache, entity_id, rid, lock=True)
        if row is None or row["status"] != "active" or row["t_valid_until"] is None:
            continue
        if await _chain(conn, cache, entity_id, row, gone, void_into) != "back":
            continue
        twin = await _live_twin(conn, entity_id, row)
        if twin is not None:
            # A twin pointer, seen through like a merge: the link that
            # retired it was void, so it no longer names one.
            await conn.execute(
                "UPDATE entity_memory SET resolution_pointer = $3,"
                "       metadata = COALESCE(metadata, '{}'::jsonb) - 'linked_by',"
                "       updated_at = NOW()"
                " WHERE id = $1 AND entity_id = $2", rid, entity_id, twin)
        else:
            restored += await conn.fetch(
                "UPDATE entity_memory"
                "   SET t_valid_until = NULL, resolution_pointer = NULL,"
                "       metadata = COALESCE(metadata, '{}'::jsonb) - 'superseded_in'"
                "                  - 'retired_also_in' - 'linked_by',"
                "       updated_at = NOW()"
                " WHERE id = $1 AND entity_id = $2 RETURNING id", rid, entity_id)
        cache.pop(rid, None)
    return restored


async def restore_after_takeover(conn, entity_id: int, row_id: int) -> int:
    """A deleted thread's late row that a live conversation took over (it
    said it word for word: MemoryConsolidator.note_held): the conversation
    keeps it, and what the thread changed with it is still undone, as at the
    delete (_undo with the row as the thread's). The thread's forget cannot
    do this later: the row is no longer tagged with the thread, so it is
    neither archived nor re-homed there, and the rows it linked or had
    merged into it carry no stamp naming the thread. Returns how many rows
    came back."""
    gone = await _deleted_threads(conn, entity_id)
    return len(await _undo(conn, entity_id, [int(row_id)], gone, {int(row_id)}, {}))


async def _hold_on_twin(conn, entity_id: int, twin, where_to: dict, turns: list,
                        e: dict) -> None:
    """A conversation's words are already live in `twin`: that row holds
    them for it (held_in, word for word). The row itself, its owner and its
    date are left as they are."""
    from mastercode.memory_consolidation import same_scope
    tmd = twin["metadata"]
    tmd = json.loads(tmd) if isinstance(tmd, str) else dict(tmd or {})
    if same_scope(tmd.get("source_context"), where_to):
        return
    held = [x for x in (tmd.get("held_in") or []) if isinstance(x, dict)]
    for x in held:
        if same_scope(x, where_to) and _verbatim(x):
            x["from_turns"] = sorted({int(t) for t in (x.get("from_turns") or [])} | set(turns))
            break
    else:
        held.append(dict(where_to, **({"from_turns": turns} if turns else {}),
                         **({"at": e["at"]} if e.get("at") else {})))
    await conn.execute(
        "UPDATE entity_memory SET metadata = jsonb_set(COALESCE(metadata,"
        " '{}'::jsonb), '{held_in}', $3::jsonb), updated_at = NOW()"
        " WHERE id = $1 AND entity_id = $2", int(twin["id"]), entity_id, json.dumps(held))


async def _own_words(conn, entity_id: int, in_own_words: list, gone: set, void_into: set,
                     cache: dict) -> tuple:
    """The rows this delete archived that live conversations said only in
    other words (held_in entries with "words": MemoryConsolidator.note_held).
    The delete undoes what the thread changed and keeps what other
    conversations said, without guessing what the merge
    pass would have done:

      * a row the archived row replaced that comes back (_chain), is of the
        restatement's kind (type and rule mark) and holds
        all of a conversation's words keeps that conversation's record, as
        its restatement would have been recorded there;
      * every other conversation gets a row of its own words, with its
        source_context and from_turns, the record's type when a restatement
        may create it (else the archived row's), dated when it was said (the
        record's "at", else its newest message), with the archived row's
        importance. It is retired the way the archived row was only when a
        live conversation's own action retired that row (_chain: a
        correction, a drop): a deleted thread's action or the merge pass's
        fold is not one. Words already live in a row of that type, a rule
        if the restatement is one and a plain row if not (_marks_rule), are
        recorded there instead (held_in, word for word): one live copy.

    The new rows have no vector yet; the memory backfill embeds them
    (server.main._backfill_unembedded_memories). Returns how many records it
    kept and how many replaced rows it brought back holding them."""
    from mastercode.memory_consolidation import UNSCOPED, same_scope
    from mastercode.memory_engine import MEMORY_TYPES, NEVER_SUPERSEDED_TYPES
    given, held_back = 0, 0

    def holds(row, words: str, mtype: str, as_rule: bool) -> bool:
        # The same kind of row the restatement would be (type and rule mark,
        # as _live_twin), holding all of its words.
        return (row["memory_type"] == mtype and _marks_rule(row["metadata"]) == as_rule
                and set(str(words).lower().split())
                <= set((row["content"] or "").lower().split()))

    def record(e: dict, row) -> dict:
        r = dict(e)
        if r.get("words") == row["content"]:
            # its words are that row's own: word for word
            r.pop("words", None)
            r.pop("type", None)
        return r

    for row_id, entries in in_own_words:
        base = await _mem_row(conn, cache, entity_id, row_id)
        if base is None:
            continue
        bmd = base["metadata"]
        retired_live = (base["t_valid_until"] is not None
                        and await _chain(conn, cache, entity_id, base, gone, void_into)
                        == "stays")
        replaced = []
        for p in await conn.fetch(
                "SELECT id FROM entity_memory WHERE entity_id = $1 AND resolution_pointer = $2"
                " AND status = 'active' AND t_valid_until IS NOT NULL ORDER BY id",
                entity_id, row_id):
            prow = await _mem_row(conn, cache, entity_id, int(p["id"]), lock=True)
            if (prow is not None
                    and await _chain(conn, cache, entity_id, prow, gone, void_into) == "back"
                    and await _live_twin(conn, entity_id, prow) is None):
                replaced.append(prow)
        back: dict = {}
        for e in entries:
            where_to = {"kind": e["kind"], "ref": str(e.get("ref") or "")}
            mtype = e.get("type")
            if mtype not in MEMORY_TYPES or mtype in NEVER_SUPERSEDED_TYPES:
                mtype = base["memory_type"]
            turns = [int(t) for t in (e.get("from_turns") or [])]
            # A rule said again elsewhere stays a rule there (below).
            as_rule = mtype == base["memory_type"] and _marks_rule(bmd)
            holder = next((p for p in replaced if holds(p, e["words"], mtype, as_rule)), None)
            if holder is not None:
                back.setdefault(int(holder["id"]), (holder, []))[1].append(record(e, holder))
                given += 1
                continue
            if not retired_live:
                twin = await conn.fetchrow(
                    "SELECT id, metadata FROM entity_memory WHERE entity_id = $1"
                    " AND content = $2 AND memory_type = $3 AND status = 'active'"
                    " AND (t_valid_until IS NULL OR t_valid_until > NOW())"
                    " AND " + _twin_rule_sql(4) +
                    " ORDER BY id LIMIT 1 FOR UPDATE", entity_id, e["words"], mtype, as_rule)
                if twin is not None:
                    await _hold_on_twin(conn, entity_id, twin, where_to, turns, e)
                    cache.pop(int(twin["id"]), None)
                    given += 1
                    continue
            # Retired as the archived row was: its stamps, its pointer, and
            # the live link that replaced it, so a later delete of its
            # dropper leaves it retired while that link stands.
            keys = (("superseded_in", "retired_also_in", "linked_by") if retired_live else ()) \
                + ("resolved_in", "resolved_also_in") \
                + ("about", "scoped_by", "scoped_at", "scope_history")  # the Host's decision
            md = {k: bmd[k] for k in keys if k in bmd}
            if as_rule:
                # A rule said again elsewhere stays a rule there
                # (memory_engine.RULE_KINDS): unmarked, it would be an
                # ordinary preference the automatic paths may retire.
                md["rule_kind"] = bmd["rule_kind"]
            if isinstance(md.get("about"), dict) and _of_gone(md["about"], gone):
                # a decision naming a deleted thread is no place to be read
                for _k in ("about", "scoped_by", "scoped_at"):
                    md.pop(_k, None)
            if not same_scope(where_to, UNSCOPED):
                md["source_context"] = where_to
            if turns:
                md["from_turns"] = turns
            md["restated_from"] = int(row_id)
            project = None
            if where_to["kind"] == "project":
                try:
                    project = int(where_to["ref"])
                except (TypeError, ValueError):
                    project = None
            when = _said_at(e)
            if when is None and turns:
                when = await conn.fetchval(
                    "SELECT max(created_at) FROM entity_memory"
                    " WHERE entity_id = $1 AND id = ANY($2::int[])", entity_id, turns)
            await conn.fetchval(
                "INSERT INTO entity_memory (entity_id, content, memory_type,"
                " importance, confidence, visibility, metadata, project_id,"
                " t_valid_until, resolution_pointer, resolved_at, created_at, t_valid_from)"
                " SELECT entity_id, $3, $4, importance, confidence,"
                "        visibility, $5::jsonb, $6,"
                "        CASE WHEN $8 THEN t_valid_until END,"
                "        CASE WHEN $8 THEN resolution_pointer END,"
                "        resolved_at, COALESCE($7::timestamptz, created_at),"
                "        COALESCE($7::timestamptz, created_at)"
                "   FROM entity_memory WHERE id = $1 AND entity_id = $2 RETURNING id",
                row_id, entity_id, e["words"], mtype, json.dumps(md), project, when,
                bool(retired_live))
            given += 1
        for p, said in back.values():
            await _bring_back(conn, entity_id, p, said)
            cache.pop(int(p["id"]), None)
            held_back += 1
    return given, held_back


async def _restore_retired(conn, entity_id: int, kind: str, ref: str, archived: list,
                           void_into: set, gone: set, cache: dict) -> list:
    """Bring back what the deleted thread retired, and nothing a live
    conversation still holds retired: every retired row whose chain runs
    into this delete — the rows it archived (`archived`) or re-homed
    (`void_into`), the rows stamped with it — is judged by _chain.

    A row the thread corrected comes back when nothing after it on its chain
    is a live conversation's own action. The test thread says "lives in
    Utah", which retires Ohio; the person later says "lives in Idaho" in the
    original conversation, which retires Utah: deleting the thread leaves
    Ohio retired, because the chain runs on to Idaho, a live correction. The
    merge pass's folds are seen through, so the same holds when the thread's
    row had been folded into another row before the person corrected it. A
    row a live conversation repeated word for word (it keeps that row) still
    has what the thread changed with it undone.

    A row retired with no replacement (a DELETE action, the Host's
    <outdated> skill event) comes back when every conversation that retired
    it (its stamp, each retired_also_in) is a deleted thread. A Split or an
    adopt moves the stamps with the messages that made them (carry_stamps),
    so deleting the thread they left restores nothing that moved.

    An archive for any other reason — by the person in the Memory app, by
    the integrity pass — ends the chain, and the row before it stays retired:
    that archive is someone's decision about the belief. A row whose words
    are already live elsewhere points at that copy instead of coming back
    (_undo)."""
    seeds = list(archived) + list(void_into) + [int(r["id"]) for r in await conn.fetch(
        "SELECT id FROM entity_memory WHERE entity_id = $1"
        " AND ((status = 'active' AND t_valid_until IS NOT NULL)"
        "      OR (status = 'archived' AND status_reason = $4))"
        " AND ((metadata->'superseded_in'->>'kind' = $2"
        "       AND metadata->'superseded_in'->>'ref' = $3)"
        "      OR EXISTS (SELECT 1 FROM jsonb_array_elements("
        "          CASE WHEN jsonb_typeof(metadata->'retired_also_in') = 'array'"
        "               THEN metadata->'retired_also_in' ELSE '[]'::jsonb END) o"
        "          WHERE o->>'kind' = $2 AND o->>'ref' = $3))",
        entity_id, kind, ref, DELETED_REASON)]
    return await _undo(conn, entity_id, seeds, gone, void_into, cache)


async def forget_if_deleted(pool, entity_id: int, scope) -> bool:
    """Repeat a thread's forgetting for what landed after its delete. A
    consolidation or a skill event already under way when the person deleted
    the thread writes its rows, retirements and closed goals afterwards; each
    such writer calls this once its writes are committed. FOR SHARE waits out
    a delete in flight, so every write is committed before the delete (which
    takes it) or before this check (which does). True when the thread is
    deleted and the forgetting ran. Never raises."""
    if not isinstance(scope, dict) or scope.get("kind") != "thread":
        return False
    ref = scope.get("ref")
    if is_legacy(ref) or not _is_uuid(ref):
        return False
    tid = canonical_id(ref)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1, $2)",
                                   _FORGET_LOCK, int(entity_id))
                gone = await conn.fetchval(
                    "SELECT deleted_at IS NOT NULL FROM circles_threads"
                    " WHERE id = $1::uuid AND entity_id = $2 FOR SHARE",
                    tid, entity_id)
                if not gone:
                    return False
                out = await _forget(conn, entity_id, tid)
    except Exception as exc:  # noqa: BLE001 - the writer's own work is done
        log.warning("late forget failed (entity %s, thread %s): %s",
                    entity_id, tid, exc)
        return False
    if any(out.values()):
        log.info("deleted thread %s: forgot what landed after the delete "
                 "(entity %s): %s", tid, entity_id, out)
    return True


async def carry_stamps(conn, entity_id: int, moved_ids: list[int], to_scope: dict, *,
                       from_scope: dict | None = None) -> int:
    """Move the records of what the moved messages did with them, as the rows
    learned from them move: a retirement or goal close (metadata.superseded_in,
    metadata.resolved_in: memory_consolidation.scope_stamp) and a restatement,
    second completion or second retirement (the held_in / resolved_also_in /
    retired_also_in lists: MemoryConsolidator.note_held). A record whose from_turns are all `moved_ids` is
    re-pointed at `to_scope`. With `from_scope`, a record that names it and
    carries no from_turns moves too, which is for a move that takes the whole
    conversation (an adopt with no time window). Returns the rows changed."""
    moved_ids = [int(i) for i in moved_ids or []]
    # As a jsonb array once, not converted again for every row compared.
    moved = json.dumps(moved_ids)
    target = json.dumps({"kind": to_scope.get("kind"),
                         "ref": str(to_scope.get("ref") or "")})
    fkind = str((from_scope or {}).get("kind") or "")
    fref = str((from_scope or {}).get("ref") or "")

    def moves(e: str) -> str:
        return (f"((jsonb_typeof({e}->'from_turns') = 'array'"
                f"  AND jsonb_array_length({e}->'from_turns') > 0"
                f"  AND ({e}->'from_turns') <@ $2::jsonb)"
                f" OR ($4 <> '' AND {e}->'from_turns' IS NULL"
                f"     AND {e}->>'kind' = $4 AND {e}->>'ref' = $5))")

    n = 0
    for key in ("superseded_in", "resolved_in"):
        stamp = f"metadata->'{key}'"
        res = await conn.execute(
            "UPDATE entity_memory"
            f"   SET metadata = jsonb_set(metadata, '{{{key}}}', {stamp} || $3::jsonb)"
            f" WHERE entity_id = $1 AND jsonb_typeof({stamp}) = 'object'"
            f"   AND {moves(stamp)}",
            entity_id, moved, target, fkind, fref)
        n += _count(res)
    for key in ("held_in", "resolved_also_in", "retired_also_in"):
        res = await conn.execute(
            "UPDATE entity_memory"
            f"   SET metadata = jsonb_set(metadata, '{{{key}}}', ("
            f"       SELECT jsonb_agg(CASE WHEN {moves('e')} THEN e || $3::jsonb ELSE e END)"
            f"         FROM jsonb_array_elements(metadata->'{key}') e))"
            f" WHERE entity_id = $1 AND jsonb_typeof(metadata->'{key}') = 'array'"
            f"   AND EXISTS (SELECT 1 FROM jsonb_array_elements(metadata->'{key}') e"
            f"                WHERE {moves('e')})",
            entity_id, moved, target, fkind, fref)
        n += _count(res)
    return n


def _count(res) -> int:
    try:
        return int(str(res).split()[-1])
    except (ValueError, IndexError):
        return 0


async def set_thread_topic(pool, entity_id: int, thread_id, topic: str | None,
                           *, title: str | None = None) -> bool:
    """Record a thread's topic on the thread row — the consolidator's segment
    topic, or the Host's own (close_turn).

    A no-op for the legacy scope, which has no row. The title follows the
    first topic when the user never set one (the title comes from the
    user or, when absent, the first segment_topic the consolidator writes)
    — COALESCE, so a user's title is never overwritten. `title` (close_turn)
    is the title to use when the row has none, in place of the topic. An
    empty topic leaves the stored one as it is. True when the UPDATE ran.
    """
    if is_legacy(thread_id) or not _is_uuid(thread_id):
        return False
    topic = (topic or "").strip() or None
    title = (title or "").strip() or None
    if topic is None and title is None:
        return False
    try:
        await pool.execute(
            """
            UPDATE circles_threads
               SET topic = COALESCE($3, topic),
                   title = COALESCE(title, $4)
             WHERE id = $1::uuid AND entity_id = $2
            """,
            str(thread_id), entity_id, topic, title or topic,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - never fail a consolidation
        log.warning("set thread topic failed (entity %s, thread %s): %s",
                    entity_id, thread_id, exc)
        return False


async def write_link(pool, entity_id: int, from_thread, to_thread,
                     reason: str) -> None:
    """Record that one thread reached into another. A link is an EVENT, so it
    is countable — never a judgement the model makes.

    No-op when either side is the legacy scope: both columns are uuid and the
    sentinel is not one. The original conversation therefore has no links,
    which is the honest answer rather than a fabricated id.

    ENTITY-SCOPED ON BOTH ENDS. Every read in this module filters on
    entity_id; this write did not, so a from/to pair naming another entity's
    thread inserted a row whose reach is exactly what a link is supposed to
    record. INSERT … SELECT … WHERE EXISTS on both ends does the check inside
    the same statement, so it cannot race a delete between a check and an
    insert. The self-link guard is here AND as a table CHECK
    (migrations/v167): a thread reaching into itself is what a non-canonical
    ref produced on the read path, and it rendered the thread as its own
    RELATED item.
    """
    if is_legacy(from_thread) or is_legacy(to_thread):
        return
    if not (_is_uuid(from_thread) and _is_uuid(to_thread)):
        return
    if reason not in LINK_REASONS:
        raise ValueError(f"link reason must be one of {LINK_REASONS}")
    try:
        await pool.execute(
            """
            INSERT INTO circles_thread_links
              (entity_id, from_thread, to_thread, reason)
            SELECT $1, $2::uuid, $3::uuid, $4
             WHERE $2::uuid <> $3::uuid
               AND EXISTS (SELECT 1 FROM circles_threads
                            WHERE id = $2::uuid AND entity_id = $1)
               AND EXISTS (SELECT 1 FROM circles_threads
                            WHERE id = $3::uuid AND entity_id = $1)
            """,
            entity_id, str(from_thread), str(to_thread), reason,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("thread link write failed (entity %s, %s->%s): %s",
                    entity_id, from_thread, to_thread, exc)


# ---- Topic markers ----
#
# WHICH METADATA THE FLIP IS READ FROM, and why it is not `topic_changed`.
# _store_summary (thread_consolidator.py) writes five keys plus two
# optional: source_kind, source_context, summary_version, consolidated_at,
# turns_covered, `segment_topic` when a topic was named, and `flip_turn_id`
# when the topic CHANGED. `topic_changed` and `prev_topic` are PARAMETERS of that
# function, not stored fields — a flip leaves two traces on the row instead:
# the changed segment_topic, and a "Previously: <prev topic>" line prepended
# to the content (:269-270). So a flip is derived by walking the summary
# chain in summary_version order and comparing consecutive segment_topics,
# with the "Previously:" line as the fallback source of prev_topic when the
# predecessor row carried no topic of its own.
#
# THE ANCHOR. `turns_covered` is a count and `consolidated_at` is when the
# summarizer ran, so neither identifies a turn row.
# _store_summary records `flip_turn_id` on a flip row — the newest turn the
# summary covered, resolved through shown_where so it is a row the user can
# see — and that is preferred here. It is the LAST turn of the new segment
# rather than its first (the consolidator is handed role/content pairs;
# recall_recent_turns returns role/content only), which is still
# strictly earlier than the fallback: the summary row's own created_at, whose
# first turn at-or-after lands on the turn AFTER the whole covered window.
# Rows written before that key existed keep the time-based anchor.

_PREVIOUSLY = "Previously:"


async def _first_turn_at_or_after(pool, entity_id: int, thread_id, at) -> int | None:
    """The id of the first turn row this conversation SHOWS created at or
    after `at` — the row set the read renders (shown_where), so a marker
    never anchors on a row the user cannot see. It may land on an earlier
    split's marker; a Split there moves everything said after it and leaves
    that marker, which is what the user sees happen."""
    args: list = [entity_id]
    where = shown_where(args, thread_id)
    args.append(at)
    where.append(f"created_at >= ${len(args)}")
    try:
        return await pool.fetchval(
            f"SELECT id FROM entity_memory WHERE {' AND '.join(where)}"
            f" ORDER BY created_at ASC, id ASC LIMIT 1",
            *args,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("marker anchor lookup failed (entity %s, thread %s): %s",
                    entity_id, thread_id, exc)
        return None


def _recorded_flip_turn(md: dict) -> int | None:
    """`flip_turn_id` off a summary row's metadata, or None when the row
    predates the key or carries something that is not a turn id. jsonb hands
    the value back as an int or a str depending on how it was written."""
    raw = md.get("flip_turn_id")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def markers(pool, entity_id: int, thread_id) -> list[dict]:
    """Topic flips in this thread, oldest first, from the superseded-summary
    chain. Each: {summary_id, topic, prev_topic, at, from_turn_id}.

    The chain is every summary row the consolidator wrote for the scope —
    superseded rows included, since each supersede is what a flip looks like
    in storage (t_valid_until + resolution_pointer, :298-313).
    """
    src = descriptor_for(thread_id)
    try:
        rows = await pool.fetch(
            """
            SELECT id, content, metadata, created_at
              FROM entity_memory
             WHERE entity_id = $1
               AND memory_type = 'summary'
               AND status = 'active'
               AND metadata->>'source_kind' = 'thread_consolidator'
               AND metadata->'source_context'->>'kind' = $2
               AND metadata->'source_context'->>'ref' = $3
             ORDER BY COALESCE((metadata->>'summary_version')::int, 0) ASC,
                      id ASC
            """,
            entity_id, str(src.get("kind")), str(src.get("ref") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("thread markers read failed (entity %s, thread %s): %s",
                    entity_id, thread_id, exc)
        return []

    out: list[dict] = []
    prev_topic: str | None = None
    for r in rows:
        md = _as_dict(r["metadata"])
        topic = md.get("segment_topic")
        if topic and prev_topic is not None and topic != prev_topic:
            # prev_topic from the chain, except where the predecessor row had
            # no topic of its own — then the flip's own "Previously:" line is
            # the only record of what came before.
            said_prev = prev_topic
            first_line = (r["content"] or "").split("\n", 1)[0].strip()
            if first_line.startswith(_PREVIOUSLY):
                said_prev = first_line[len(_PREVIOUSLY):].strip() or prev_topic
            from_turn_id = _recorded_flip_turn(md)
            if from_turn_id is None:
                from_turn_id = await _first_turn_at_or_after(
                    pool, entity_id, thread_id, r["created_at"])
            out.append({
                "summary_id": r["id"],
                "topic": topic,
                "prev_topic": said_prev,
                "at": _iso(r["created_at"]),
                "from_turn_id": from_turn_id,
            })
        if topic:
            prev_topic = topic
    return out


# ---- Split ----

async def split_thread(pool, entity_id: int, source_thread_id, *,
                       from_turn_id: int, title: str | None = None) -> dict:
    """Move this thread's turns from `from_turn_id` onward into a new thread.

    A retag, never a delete — the same shape as projects.adopt_thread_core
    (server/routes/projects.py:315-373): rows get a new source_context, the
    provenance of the move rides beside it, and a marker turn stays behind in
    the source so the move never reads as a disappearance.

    A CLOSED SOURCE SPLITS. `closed_at` gates SENDS and nothing else, which
    is what the rest of the surface already does: patch_thread applies a
    rename to a closed thread (patch_thread, above), replay passes
    require_open=False (server/routes/circles.py:2101), and markers and Clear never look at
    the column. Reorganising a finished conversation is the same kind of act
    as renaming it, and the thread the split creates is open — which is the
    point, since you split in order to keep talking. Refusing here would be
    the one inconsistent gate on the surface.

    THE SOURCE'S ROLLING SUMMARY IS RETIRED, not rewritten. Moving rows
    out leaves a live summary describing
    content that now lives elsewhere, and the consolidator would not notice
    for a long time: its trigger counts turns since `prev_summary.created_at`
    and needs TURNS_SINCE_TRIGGER=60 of them (thread_consolidator.
    maybe_consolidate, :879-889). Superseding the
    row instead means the next turn is assembled with no summary rather than
    a wrong one, and the next consolidation sees no `prev` and counts the
    whole scope. The destination needs nothing — it was created in this call,
    so no summary row can carry its ref.

    ONE TRANSACTION. Create → move → marker → link → supersede all run on one
    connection inside one transaction, so a crash or a raise anywhere in the
    sequence leaves the database exactly as it was. That is also why the
    concurrent-split case needs no cleanup: the raise rolls the new thread
    row back with everything else.

    Raises ValueError('thread not found') when the source is not this
    entity's; ValueError('turn not found in this conversation') when
    `from_turn_id` is not a row that conversation renders; and
    ValueError('nothing to move') when the move set is empty (the anchor is
    a trailing marker, or a concurrent split took the rows first).
    """
    # Existence only — the full count get_thread does is not read here.
    if await thread_exists_open(pool, entity_id, source_thread_id) is None:
        raise ValueError("thread not found")
    legacy_source = is_legacy(source_thread_id)
    src_ctx = descriptor_for(source_thread_id)

    async with pool.acquire() as conn:
        async with conn.transaction():
            created = await _split_in_transaction(
                conn, entity_id, source_thread_id,
                from_turn_id=from_turn_id, title=title,
                legacy_source=legacy_source, src_ctx=src_ctx)
    # Re-read after the commit, on the pool: the row is durable by now, and
    # a read that swallows its own error never sits inside the transaction.
    out = await get_thread(pool, entity_id, created["id"])
    return out or created


async def _split_in_transaction(conn, entity_id: int, source_thread_id, *,
                                from_turn_id: int, title: str | None,
                                legacy_source: bool, src_ctx: dict) -> dict:
    """split_thread's body, on ONE connection inside its transaction. Every
    helper it calls (create_thread, write_link, get_thread) takes the pool
    positionally and only ever calls fetch/fetchrow/fetchval/execute on it,
    which an asyncpg Connection provides with the same signatures — the same
    substitution thread_consolidator._store_summary already makes."""
    # "From that point" is a moment, not an id: the two rows of one exchange
    # race on insert while created_at is the utterance time
    # (memory_engine.write_turn_row), so a later-said row can hold a smaller
    # id and an `id >=` cut could leave one row of an exchange behind. The
    # anchor must be a row this conversation RENDERS (shown_where: a marker
    # counts — a split at a marker moves everything after it; a regenerate's
    # hidden row does not).
    anchor_args: list = [entity_id]
    anchor_where = shown_where(anchor_args, source_thread_id)
    anchor_args.append(int(from_turn_id))
    anchor_where.append(f"id = ${len(anchor_args)}")
    cut_at = await conn.fetchval(
        f"SELECT created_at FROM entity_memory WHERE {' AND '.join(anchor_where)}",
        *anchor_args)
    if cut_at is None:
        raise ValueError("turn not found in this conversation")

    args: list = [entity_id]
    # What the user sees is what moves — shown_where on either arm
    # (hidden-lane rows stay in the original conversation, a regenerate's
    # rows stay in the thread that hid them; superseded rows move with the
    # transcript they belong to). Everything said at or after the anchor
    # row's moment moves (cut_at, resolved above).
    where = shown_where(args, source_thread_id)
    args.append(cut_at)
    where.append(f"created_at >= ${len(args)}")
    # A marker describes a move that happened HERE; carrying it into the new
    # thread would read as one having happened there. Same exclusion
    # adopt_thread_core applies when its source is another thread's rows.
    where.append("COALESCE(metadata->>'source_kind', '') <> 'thread_marker'")

    # Nothing to move → nothing is created. A marker suggestion can trail the
    # conversation (an earlier split left it as the newest shown row), and a
    # split there would otherwise leave an empty thread in the list.
    n_to_move = await conn.fetchval(
        f"SELECT count(*) FROM entity_memory WHERE {' AND '.join(where)}",
        *args)
    if not n_to_move:
        raise ValueError("nothing to move")

    new_thread = await create_thread(
        conn, entity_id, title=title, origin="split",
        # A split off the original conversation has no parent row to point
        # at: the legacy scope is not a row (the FK would have nothing to
        # reference), so parent_thread_id stays NULL and `origin='split'` is
        # what records where it came from.
        parent_thread_id=None if legacy_source else str(source_thread_id),
    )
    new_id = new_thread["id"]

    args.append(json.dumps({"kind": "thread", "ref": new_id}))
    sctx_idx = len(args)
    args.append(json.dumps({"kind": src_ctx.get("kind"),
                            "ref": str(src_ctx.get("ref") or "")}))
    from_idx = len(args)

    # `metadata - 'app_id'` is applied when the source is the legacy scope,
    # and only then. Both app arms of _turn_row_where exclude a thread's rows
    # regardless of the tag, so the strip is not what keeps a moved row out of
    # the original conversation here; it is what keeps it out of the one
    # reader that matches on the bare tag with no thread exclusion
    # (server/routes/brain.py's app_id filter). The old value is kept under
    # split_prior_app_id so the move stays reversible. On a thread source the
    # tag is left alone: a thread's rows are matched by descriptor only, the
    # tag is what the read uses to hide a regenerate's rows, and a row that
    # was written inside a thread carries app_id 'circles' anyway.
    if legacy_source:
        base_expr = (
            "(metadata - 'app_id') || CASE WHEN metadata ? 'app_id' "
            "THEN jsonb_build_object('split_prior_app_id', metadata->'app_id') "
            "ELSE '{}'::jsonb END")
    else:
        base_expr = "metadata"
    moved = await conn.fetch(
        f"""
        UPDATE entity_memory
           SET metadata = {base_expr}
                   || jsonb_build_object(
                          'source_context', ${sctx_idx}::jsonb,
                          'split_from',     ${from_idx}::jsonb,
                          'split_at',       to_jsonb(now()::timestamptz))
         WHERE {' AND '.join(where)}
        RETURNING id, created_at
        """,
        *args,
    )

    if not moved:
        # The count above said there was something to move and the UPDATE
        # found nothing: a concurrent split took the same rows first. The
        # raise rolls this transaction back, which takes the thread row with
        # it — no cleanup statement of our own, and nothing left behind on
        # any other failure path either.
        raise ValueError("nothing to move")

    # What was learned from the moved messages goes with them: a row whose
    # metadata.from_turns (batch_consolidator) are all moved rows is retagged to
    # the new thread, so deleting that thread takes it too. A row learned from
    # messages on both sides of the split stays where it is, and a row learned
    # before from_turns existed carries none and stays as well. What those
    # messages retired or closed follows the same rule (carry_stamps).
    moved_ids = [int(r["id"]) for r in moved]
    await conn.execute(
        "UPDATE entity_memory"
        "   SET metadata = metadata || jsonb_build_object("
        "           'source_context', $3::jsonb, 'split_from', $4::jsonb)"
        " WHERE entity_id = $1 AND memory_type <> 'turn' AND status = 'active'"
        "   AND jsonb_typeof(metadata->'from_turns') = 'array'"
        "   AND jsonb_array_length(metadata->'from_turns') > 0"
        "   AND (metadata->'from_turns') <@ $2::jsonb",
        entity_id, json.dumps(moved_ids),
        json.dumps(descriptor_for(new_id)), json.dumps(src_ctx))
    await carry_stamps(conn, entity_id, moved_ids, descriptor_for(new_id))

    # A messaging connector's map rows follow the turns they belong to, so a
    # reply in the messaging app to a moved message lands in the new thread
    # (messaging connectors design 4.3). Read before the marker below exists,
    # so the rows that stayed are the conversation's own.
    async def _staying() -> list:
        stay_args: list = [entity_id]
        stay_where = shown_where(stay_args, source_thread_id)
        stay_where.append("COALESCE(metadata->>'source_kind', '') <> 'thread_marker'")
        return [r["created_at"] for r in await conn.fetch(
            f"SELECT created_at FROM entity_memory WHERE {' AND '.join(stay_where)}", *stay_args)]
    from server.connectors.message_map import retag_moved
    await retag_moved(conn, entity_id,
                      src=(src_ctx.get("kind"), str(src_ctx.get("ref") or "")),
                      dst=("thread", str(new_id)), moved_rows=moved, staying_times=_staying)

    if moved:
        marker_md = {
            "role": "assistant",
            "source_kind": "thread_marker",
            "source_context": {"kind": src_ctx.get("kind"),
                               "ref": str(src_ctx.get("ref") or "")},
            "marker": {"thread_id": new_id,
                       "thread_title": new_thread["title"],
                       "moved_turns": len(moved)},
        }
        if src_ctx.get("kind") == "app":
            # The legacy scope is read through the app_id tag as well as the
            # descriptor; without it the marker renders nowhere.
            marker_md["app_id"] = str(src_ctx.get("ref") or "")
        label = new_thread["title"] or "a new thread"
        # It carries no embedding: furniture, not knowledge.
        await conn.execute(
            """
            INSERT INTO entity_memory
              (entity_id, content, memory_type, importance, status,
               source_session, project_id, metadata)
            VALUES ($1, $2, 'turn', 0.1, 'active', $3, NULL, $4::jsonb)
            """,
            entity_id,
            f"→ Moved {len(moved)} messages into “{label}” — nothing was "
            f"deleted; the conversation continues in that thread.",
            f"split:{new_id}",
            json.dumps(marker_md),
        )

    if not legacy_source:
        # write_link swallows its own DB errors, which is right when it is
        # called on its own (a link is bookkeeping, never worth failing a
        # read for). It cannot swallow one HERE: a failed statement aborts
        # the surrounding transaction, so the next statement raises and the
        # whole split rolls back rather than landing without its link.
        await write_link(conn, entity_id, str(source_thread_id), new_id, "split")

    # The source's live summary now describes content that moved out. Retire
    # it in this same transaction (see the docstring): superseded, with no
    # successor to point at, so get_live_summary stops returning it and the
    # consolidator's next pass sees no `prev`. Any failure propagates and
    # rolls the split back — a split that leaves the stale summary in place
    # is the bug this is here to fix, so landing one is not a better outcome.
    from mastercode.thread_consolidator import supersede_live_summary
    retired = await supersede_live_summary(conn, entity_id, src_ctx)

    log.info("circles split entity=%d src=%s:%s -> thread %s turns=%d "
             "retired_summary=%s",
             entity_id, src_ctx.get("kind"), src_ctx.get("ref"), new_id,
             len(moved), retired)
    # No read that swallows its own error runs inside the transaction: a
    # failed statement here would abort the transaction while the swallowed
    # exception let it "commit" (Postgres turns that COMMIT into a ROLLBACK
    # and the caller would be told a thread exists that does not). The
    # caller re-reads the row after the commit.
    return {**new_thread, "turn_count": len(moved)}
