"""Circles' seen marks, unread counts and page data — the server half of the
book of pages.

The Workbench keeps a page's marks on its projects row (projects.seen_at,
seen_turns; server/routes/projects.py mark_project_seen_service). A Circles
conversation has no row to hold them: the original conversation is not a
circles_threads row, and a peer Host, a person and a room are not rows of this
database at all. So every conversation's mark is a row of circles_page_marks
(migrations/v219), keyed by the kind and id GET /api/circles/thread/{kind}/{id}
already takes:

  self    your Host's page: 'original' or a thread's canonical uuid
  entity  a peer Host: its entity id; one topic of that conversation:
          "<entity id>/<topic>", topic 'original' or a uuid
  dm      a person: their email, lowercased, for the operator reading it
  circle  a room of several Hosts: the circle's id

A 'dm' mark belongs to the operator who reads it, because the People list and
a person's thread are that operator's own (routes/circles.py
_list_people_threads, _read_people_thread). Every other kind is the Host's,
shared by its operators, as a Workbench project's mark is.

Where each kind's messages are, for its unread count:
  entity, dm  this Host's own inbox_messages (routes/circles.py
              _list_entity_threads, _list_people_threads); a message's time is
              COALESCE(sent_at, delivered_at), the time those lists sort on,
              which every writer sets from this database's clock.
  circle      the platform's circle_thread_messages, read through the bridge's
              circles.messages op (routes/circles.py get_thread); nothing of a
              room is stored here.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from server.circles_reading import person_checked_sql
from server.peer import topics as _topics

log = logging.getLogger("privatae.circles_marks")

KINDS = ("self", "entity", "dm", "circle")
# A turn token (session.py: uuid4().hex), as projects.mark_project_seen_service
# takes one.
_TURN_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# How many watched replies a page keeps (v219's CHECK holds the same number,
# as v217's does for a project).
SEEN_TURNS_KEEP = 32
# A person's address as the People list keys them; v219's CHECK bounds a ref
# at the same length.
_REF_MAX = 320
# A room's unread count reads its newest page of messages from the bridge, at
# most this many. A room with more unread than that says unread_capped.
ROOM_UNREAD_PAGE = 100
# Rooms read at once. Each is one bridge call.
ROOM_READS_AT_ONCE = 8
# How long the conversations list waits for its rooms' reads, all of them
# together. A room not read by then says unread_unknown and the list answers:
# the list is what the person is waiting for, a room's count is a mark on it.
# Without it each read waited the bridge's own 30 s (bridge_client
# REQUEST_TIMEOUT), one wave of ROOM_READS_AT_ONCE rooms after another, so one
# slow room held the whole list that long.
ROOM_UNREAD_DEADLINE = 2.0


class MarksError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def page_ref(pool, entity_id: int, kind: str, ref, reader: str,
                   *, topic=None) -> tuple[str, str]:
    """(ref, reader) as circles_page_marks keys the conversation `kind`/`ref`
    names. Raises MarksError: 400 for a kind or an id that is not one, 404 for
    a thread that is not a live thread of this entity, 403 for a person's
    conversation with no verified operator to own its mark.

    `topic` names one topic of a conversation with another
    Host: "original" or a topic's id (server/peer/topics.py). Its mark is
    kind 'entity', ref "<peer>/<topic>", beside the peer's own "<peer>". Only
    an 'entity' conversation has topics (400 otherwise); a topic other than
    the original with no message in the conversation is a 404."""
    from mastercode import circles_threads as ct
    if kind not in KINDS:
        raise MarksError(400, f"kind must be one of {', '.join(KINDS)}")
    if topic is not None and kind != "entity":
        raise MarksError(400, "only a conversation with another Host has topics")
    raw = "" if ref is None else str(ref).strip()
    if kind == "self":
        if ct.is_legacy(raw):
            return ct.LEGACY_THREAD_ID, ""
        tid = ct.canonical_id(raw)
        # Closed is fine (a closed page can still be read); deleted is gone.
        if not ct._is_uuid(tid) or await ct.thread_exists_open(pool, entity_id, tid) is None:
            raise MarksError(404, "thread not found")
        return str(tid), ""
    if kind in ("entity", "circle"):
        try:
            n = int(raw)
        except ValueError:
            raise MarksError(400, f"a {kind} conversation's id is a number") from None
        if n <= 0:
            raise MarksError(400, f"a {kind} conversation's id is a number")
        if topic is not None:
            tid = _topics.parse(topic)
            if tid is None:
                raise MarksError(400, "topic is \"original\" or a topic's id")
            if tid != _topics.ORIGINAL:
                exists, _ = await _topics.title_of(pool, entity_id, n, tid)
                if not exists:
                    raise MarksError(404, "There is no such topic with this Host.")
            return f"{n}/{tid}", ""
        return str(n), ""
    # dm
    addr = raw.lower()
    if "@" not in addr or len(addr) > _REF_MAX or any(c.isspace() for c in addr):
        raise MarksError(400, "a person's conversation is named by their email")
    if not reader:
        raise MarksError(403, "No verified identity on this request — reload and sign in again.")
    return addr, reader


async def mark_seen(pool, entity_id: int, kind: str, ref, *, reader: str = "",
                    turn_token: str | None = None, topic=None) -> dict:
    """Record that the person has this conversation open now. The mark only
    moves forward (GREATEST keeps a later one already stored, from another
    device or a clock stepped back), so a repeated or late call is harmless.

    `turn_token`: a turn on one of the Host's own pages that the person
    watched end. Its reply's row is written after the stream, so it lands
    after this mark; the token goes on seen_turns (newest last, the last
    SEEN_TURNS_KEEP kept) and the page's unseen_landed_at leaves that reply out
    whenever it lands (circles_threads.list_threads). A mark without one keeps
    the tokens already there. Only a 'self' page runs a turn here, so a token
    on any other kind is refused.

    `topic`: one topic of a conversation with another Host (page_ref). The
    returned id is then "<peer>/<topic>".

    Returns {kind, id, seen_at}. Raises MarksError (page_ref's, and 400 for a
    token that is not one)."""
    token = turn_token or None
    if token is not None:
        if kind != "self":
            raise MarksError(400, "turn_token is only for your own Host's pages; "
                                  "no turn of this Host runs in this conversation")
        if not _TURN_TOKEN_RE.fullmatch(token):
            raise MarksError(400, "turn_token is not a turn token")
    key, who = await page_ref(pool, entity_id, kind, ref, reader if kind == "dm" else "",
                              topic=topic)
    seen = await pool.fetchval(
        """
        INSERT INTO circles_page_marks AS p (entity_id, kind, ref, reader, seen_at, seen_turns)
        VALUES ($1, $2, $3, $4, now(), CASE WHEN $5::text IS NULL THEN NULL ELSE ARRAY[$5::text] END)
        ON CONFLICT (entity_id, kind, ref, reader) DO UPDATE
           SET seen_at = GREATEST(p.seen_at, now()),
               seen_turns = CASE WHEN $5::text IS NULL THEN p.seen_turns
                 ELSE (SELECT array_agg(t ORDER BY i) FROM (
                         SELECT t, i FROM unnest(array_remove(COALESCE(p.seen_turns, '{}'::text[]), $5::text)
                                                 || $5::text) WITH ORDINALITY AS u(t, i)
                          ORDER BY i DESC LIMIT $6) kept) END
        RETURNING seen_at
        """,
        entity_id, kind, key, who, token, SEEN_TURNS_KEEP)
    return {"kind": kind, "id": key, "seen_at": seen}


# ---- Unread: the other side's messages after the mark ----------------------
#
# A conversation never marked counts every message the other side sent: until
# the person has had it open, none of them has been seen here. A book that does
# not want a contact's whole history lit on its first read holds a contact with
# no seen_at against what it first read, as the Workbench book holds a page
# with no mark (workbench-web lib/book.ts withBaselines); each contact carries
# seen_at and last_inbound_at for that.

# A peer Host's messages to this Host: the rows _list_entity_threads lists for
# that peer, less this Host's own. $1 is this entity, $2 the marks' kind for a
# peer Host.
#
# A message is seen when it is older than the peer's whole-conversation mark
# (ref "<peer>") OR the mark of the topic it is in (ref "<peer>/<topic>",
# server/peer/topics.py); GREATEST skips a NULL. With no topic
# mark this is the count it was. `seen_at` is the later of the two, so a
# conversation read only topic by topic still reads as opened.
_ENTITY_UNREAD = f"""
    SELECT i.from_entity_id::text AS ref,
           count(*) FILTER (WHERE GREATEST(m.seen_at, t.seen_at) IS NULL
                               OR COALESCE(i.sent_at, i.delivered_at)
                                  > GREATEST(m.seen_at, t.seen_at)) AS unread,
           max(COALESCE(i.sent_at, i.delivered_at)) AS last_inbound_at,
           max(GREATEST(m.seen_at, t.seen_at)) AS seen_at
      FROM inbox_messages i
      LEFT JOIN circles_page_marks m
             ON m.entity_id = $1 AND m.kind = $2
            AND m.ref = i.from_entity_id::text AND m.reader = ''
      LEFT JOIN circles_page_marks t
             ON t.entity_id = $1 AND t.kind = $2
            AND t.ref = i.from_entity_id::text || '/' || {_topics.topic_sql("i")}
            AND t.reader = ''
     WHERE i.to_entity_id = $1
       AND i.from_entity_id IS NOT NULL AND i.from_entity_id <> $1
       AND i.message_kind = 'message' AND i.archived_at IS NULL
       AND NOT (i.from_user_email IS NOT NULL AND i.to_user_email IS NOT NULL)
     GROUP BY i.from_entity_id"""

# A person's messages to the operator reading ($2): the rows
# _list_people_threads lists for that person, less the operator's own. $1 is
# this entity. Only rows whose person this Host can stand behind
# (circles_reading.person_checked_sql), as the list itself reads them.
_PEOPLE_UNREAD = f"""
    SELECT lower(i.from_user_email) AS ref,
           count(*) FILTER (WHERE m.seen_at IS NULL
                               OR COALESCE(i.sent_at, i.delivered_at) > m.seen_at) AS unread,
           max(COALESCE(i.sent_at, i.delivered_at)) AS last_inbound_at,
           max(m.seen_at) AS seen_at
      FROM inbox_messages i
      LEFT JOIN circles_page_marks m
             ON m.entity_id = $1 AND m.kind = 'dm'
            AND m.ref = lower(i.from_user_email) AND m.reader = $2
     WHERE i.to_entity_id = $1 AND i.to_user_email = $2
       AND i.from_user_email IS NOT NULL AND i.from_user_email <> $2
       AND i.message_kind = 'message' AND i.archived_at IS NULL
       AND {person_checked_sql("i", "$1")}
     GROUP BY lower(i.from_user_email)"""


def _unread_fields(n, seen_at=None, last_inbound_at=None, capped: bool = False) -> dict:
    """A contact's unread fields: `unread`, the field circles-web reads
    (lib/api.ts ConversationItemRaw), and `unread_count`, the field this list
    has always sent, carrying the same number."""
    out = {"unread": int(n or 0), "unread_count": int(n or 0),
           "seen_at": seen_at, "last_inbound_at": last_inbound_at}
    if capped:
        out["unread_capped"] = True
    return out


async def entity_unread(pool, entity_id: int) -> dict[str, dict]:
    """Each peer Host's unread fields, by its id, in one query."""
    try:
        rows = await pool.fetch(_ENTITY_UNREAD, int(entity_id), "entity")
    except Exception as e:  # noqa: BLE001 — a count never fails the list
        log.warning("peer unread counts failed (non-fatal): %s", e)
        return {}
    return {r["ref"]: _unread_fields(r["unread"], r["seen_at"], r["last_inbound_at"]) for r in rows}


async def people_unread(pool, entity_id: int, reader: str) -> dict[str, dict]:
    """Each person's unread fields for the operator `reader`, by their
    (lowercased) email, in one query. {} with no verified operator: such a
    request lists no People either."""
    if not reader:
        return {}
    try:
        rows = await pool.fetch(_PEOPLE_UNREAD, int(entity_id), reader)
    except Exception as e:  # noqa: BLE001 — a count never fails the list
        log.warning("people unread counts failed (non-fatal): %s", e)
        return {}
    return {r["ref"]: _unread_fields(r["unread"], r["seen_at"], r["last_inbound_at"]) for r in rows}


def _ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def circle_unread(pool, entity_id: int, circle_ids: list, bridge_call) -> dict[str, dict]:
    """Each room's unread fields, by its id: the messages other Hosts sent
    after the room's mark, among its newest ROOM_UNREAD_PAGE (one bridge
    circles.messages call per room, ROOM_READS_AT_ONCE at a time, all within
    ROOM_UNREAD_DEADLINE). A room the bridge did not answer for, did not
    answer for in time, or whose page could not be counted is left out (its
    fields are absent, never a made-up 0); the other rooms still count. The
    room's times are the platform's clock and the mark is this database's;
    the two are kept by NTP, and a message that lands within their difference
    of the person's look may count once more, until the next mark.
    `bridge_call(op, params)` returns the op's data or None, never raises
    (routes/circles._bridge_call)."""
    ids = []
    for c in circle_ids:
        try:
            ids.append(int(c))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {}
    try:
        marks = {r["ref"]: r["seen_at"] for r in await pool.fetch(
            "SELECT ref, seen_at FROM circles_page_marks "
            "WHERE entity_id = $1 AND kind = 'circle' AND reader = '' AND ref = ANY($2::text[])",
            int(entity_id), [str(i) for i in ids])}
    except Exception as e:  # noqa: BLE001 — a count never fails the list
        log.warning("room marks unreadable (non-fatal): %s", e)
        return {}
    gate = asyncio.Semaphore(ROOM_READS_AT_ONCE)

    async def one(cid: int):
        async with gate:
            data = await bridge_call("circles.messages", {"circle_id": cid, "limit": ROOM_UNREAD_PAGE})
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            return None
        seen = marks.get(str(cid))
        theirs = [m for m in data["messages"]
                  if isinstance(m, dict) and m.get("sender_entity_id") is not None
                  and str(m.get("sender_entity_id")) != str(entity_id)]
        times = [t for t in (_ts(m.get("created_at")) for m in theirs) if t is not None]
        unread = [t for t in times if seen is None or t > seen]
        # Every message on the page is unread and older ones exist: the count
        # is a floor.
        capped = (len(unread) == len(data["messages"]) and bool(data.get("next_before")))
        return str(cid), _unread_fields(len(unread), seen, max(times) if times else None, capped)

    # One deadline for every room, not one per read: N slow rooms cost the
    # list ROOM_UNREAD_DEADLINE, never N / ROOM_READS_AT_ONCE waits. A read
    # still out at the deadline is cancelled (request_full drops its waiter, so
    # a late answer is discarded), and so is every read when the list's own
    # request goes away first.
    tasks = {asyncio.ensure_future(one(c)): c for c in ids}
    try:
        done, pending = await asyncio.wait(list(tasks), timeout=ROOM_UNREAD_DEADLINE)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        log.warning("room unread: %d of %d room(s) not read within %.1f s (unread_unknown): %s",
                    len(pending), len(ids), ROOM_UNREAD_DEADLINE, sorted(tasks[t] for t in pending))
    out = {}
    for t in done:
        if t.cancelled():
            continue
        exc = t.exception()
        if exc is not None:
            # One room's page that cannot be counted (a time with no offset
            # against the mark's, say) is that room's unread_unknown, never
            # the whole list's 500.
            log.warning("room %s unread uncountable (non-fatal): %s: %s", tasks[t], type(exc).__name__, exc)
            continue
        res = t.result()
        if res is not None:
            out[res[0]] = res[1]
    return out


# ---- The page: one of the Host's pages, in one call -----------------------

async def page_data(pool, entity_id: int, ref) -> dict:
    """One of the Host's pages, in the shape of the Workbench's page route
    (projects.project_page_service), read-only and with no network call:

      where_this_stands  close_turn.thread_state_parts on the page's scope
                         (circles_threads.descriptor_for): the text a turn
                         starts from and the same rows as fields.
      totals             context_budget.running_spend on that scope.
      totals_vs_transcript
                         whether the totals' rows are the transcript's rows
                         (totals_scope_rows), with the counts either way.
      seen_at, last_activity_at, last_landed_at, unseen_landed_at,
      waiting_on_you, ask_cards_open, turn_count
                         the thread list's entry for the page, read by the
                         same SQL (circles_threads.page_marks).

    Each read is best-effort, as the Workbench page's are: one that fails is
    logged and reads null, and the page still opens. Raises MarksError(404)
    for a thread that is not a live thread of this entity."""
    from mastercode import circles_threads as ct
    entry = await ct.page_marks(pool, entity_id, ref)
    if entry is None:
        raise MarksError(404, "thread not found")
    tid = entry["id"]
    scope = ct.descriptor_for(None if tid == ct.LEGACY_THREAD_ID else tid)
    out: dict = {"kind": "self", "id": tid, "title": entry.get("title"), "scope": scope}

    async def _section(name: str, read):
        try:
            out[name] = await read()
        except Exception as e:  # noqa: BLE001 — one section never closes the page
            log.warning("circles page %s: %s unavailable: %s", tid, name, e)
            out[name] = None

    from mastercode.context_budget import running_spend
    from mastercode.runtime.close_turn import thread_state_parts
    await _section("where_this_stands", lambda: thread_state_parts(pool, entity_id, scope))
    await _section("totals", lambda: running_spend(pool, entity_id, scope))
    await _section("totals_vs_transcript", lambda: totals_scope_rows(pool, entity_id, tid))
    for k in ("seen_at", "last_activity_at", "last_landed_at", "unseen_landed_at",
              "waiting_on_you", "ask_cards_open", "turn_count"):
        out[k] = entry.get(k)
    return out


async def totals_scope_rows(pool, entity_id: int, ref) -> dict:
    """Whether the running totals read the same rows the page shows.

    The totals (running_spend) read the page's scope through
    memory_engine._turn_row_where with its descriptor, as recall does; the
    transcript reads circles_threads.shown_where. For a thread the two differ
    by superseded rows (a transcript keeps them, recall does not) and by the
    rows a fired wake-up, a replay or an A/B variant wrote on the hidden lane
    there (the totals count them, the transcript leaves them out).

    {same, shown, counted, shown_not_counted, counted_not_shown}: turn rows
    of each set; `same` is True when neither difference holds a row.

    Both sets lie inside one narrower set, read first: a thread's active,
    project-less turn rows (the hidden lane included, which the totals
    count), and the legacy scope's active, project-less rows of no thread."""
    from mastercode import circles_threads as ct
    from mastercode.memory_engine import _turn_row_where
    tid = None if ct.is_legacy(ref) else ct.canonical_id(ref)
    args: list = [entity_id]
    shown = " AND ".join(ct.shown_where(args, tid))
    # The totals' scope as running_spend reads it: a fired wake-up's hidden
    # rows count (they carry its spend), and the transcript leaves them out.
    counted = " AND ".join(_turn_row_where(args, session_id="", source_context=ct.descriptor_for(tid),
                                           with_wakeup_rows=True))
    if tid is None:
        within = ("em.entity_id = $1 AND em.memory_type = 'turn' AND em.status = 'active'"
                  " AND em.project_id IS NULL"
                  " AND (em.metadata->'source_context'->>'kind') IS DISTINCT FROM 'thread'")
    else:
        args.append(str(tid))
        # Every turn row of the thread, the hidden lane included: the totals
        # count a fired wake-up's rows, so they must be inside the set read.
        within = ("em.entity_id = $1 AND em.memory_type = 'turn' AND em.status = 'active'"
                  " AND em.project_id IS NULL"
                  " AND (em.metadata->'source_context'->>'kind') = 'thread'"
                  f" AND (em.metadata->'source_context'->>'ref') = ${len(args)}")
    row = await pool.fetchrow(
        f"""
        SELECT count(*) FILTER (WHERE s) AS shown,
               count(*) FILTER (WHERE c) AS counted,
               count(*) FILTER (WHERE s AND NOT c) AS shown_not_counted,
               count(*) FILTER (WHERE c AND NOT s) AS counted_not_shown
          FROM (SELECT COALESCE(({shown}), false) AS s, COALESCE(({counted}), false) AS c
                  FROM entity_memory em
                 WHERE {within}) x
         WHERE s OR c
        """,
        *args,
    )
    out = {k: int(row[k] or 0) for k in ("shown", "counted", "shown_not_counted", "counted_not_shown")}
    out["same"] = out["shown_not_counted"] == 0 and out["counted_not_shown"] == 0
    return out
