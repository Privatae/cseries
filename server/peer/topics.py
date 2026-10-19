"""Topics between two Hosts.

Two Hosts share ONE conversation. Every message between them files under the
thread the RECEIVER derives from who is on it (messages.pair_thread_id), on
both ways in: the direct lane (peer/handlers.inbox_deliver) and the relay
(bridge/memory_handlers.handle_inbox_append). A topic is a named strand inside
that conversation and nothing more: two keys of the message's nested payload,

  topic_id     a uuid, minted by the Host whose person started the topic
  topic_title  what they called it, the sender's words like a subject, and
               bounded the same way (TITLE_MAX, control characters removed)

A TOPIC NEVER CHOOSES A THREAD. It is not a thread id, and no reader looks a
topic up by itself: every read below takes the pair first (PAIR_ROWS, this
Host and one peer) and the topic second. So a peer that sends a topic id
another pair uses has named nothing of that pair's; its message is in its own
conversation with us, under a topic of its own.

THE FIRST TOPIC is the conversation as it was before topics: id "original",
"The original conversation", the sentinel your own Host's first thread uses
(mastercode.circles_threads.LEGACY_THREAD_ID). A message with no topic files
there, which is every message an older Host sends. A message sent into the
original conversation carries no topic keys at all, so an older Host reads it
exactly as it always has; a topic a newer Host sends is dropped by an older
one's direct lane (its payload allow-list is empty) and kept but unread by its
relay path, so an older Host shows one conversation.

A TOPIC EXISTS ONCE A MESSAGE CARRIES IT. There is no table of topics: both
Hosts list the same topics from the same messages, so nothing has to be kept in
step between them. Starting one ("+") is the send of its first message, which
names a title and no id (routes/circles.py _topic_for_send). Its title is the
one on the topic's earliest message that has one, so a later message cannot
rename it.

The marks: a topic's seen mark is a circles_page_marks row of kind 'entity'
with ref "<peer>/<topic>" (server/circles_marks.py), beside the peer's own
"<peer>" mark. A message is seen when it is older than either.
"""
from __future__ import annotations

import re
import uuid

#: The first topic, and the one every message with no topic belongs to.
ORIGINAL = "original"
ORIGINAL_TITLE = "The original conversation"
#: A topic's title, bounded like another Host's subject (peer/handlers._MAX_SUBJECT).
TITLE_MAX = 120
#: The nested-payload keys a topic travels in. The direct lane allow-lists
#: exactly these (peer/handlers._ALLOWED_DELIVER_PAYLOAD_KEYS).
KEYS = ("topic_id", "topic_title")
#: The circles_page_marks kind a peer Host's conversation, and each of its
#: topics, is marked under (server/circles_marks.py KINDS).
MARK_KIND = "entity"

# Line and paragraph separators too: a title is one line wherever it is shown.
_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]+")
_SPACES = re.compile(r"\s+")
# A topic id as it is stored: a uuid in canonical lowercase form. The SQL form
# below reads anything else as the original conversation, which covers a row a
# relay stored before this change, when nothing on that path checked the key.
_UUID_SQL = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def clean_id(value) -> str | None:
    """A topic id as stored: the canonical lowercase form of a uuid, or None
    for anything else (the original conversation included: it is never
    stored, it is the absence of a topic)."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError):
        return None


def parse(value) -> str | None:
    """A topic a CALLER names, as the routes take one: ORIGINAL for the
    original conversation (absent, empty or "original"), a stored id for a
    uuid, and None for anything that is neither."""
    if value is None:
        return ORIGINAL
    raw = str(value).strip()
    if raw == "" or raw.lower() == ORIGINAL:
        return ORIGINAL
    return clean_id(raw)


def clean_title(value) -> str | None:
    """A title as stored and shown: one line, no control characters, runs of
    space collapsed, at most TITLE_MAX characters. None when nothing is left."""
    if not isinstance(value, str):
        return None
    out = _SPACES.sub(" ", _CONTROL.sub(" ", value)).strip()[:TITLE_MAX].strip()
    return out or None


def stored(body_payload: dict, *, carries_topic: bool) -> dict:
    """`body_payload` with its topic keys as this Host stores them.

    Run on every row handle_inbox_append writes, because the relay hands the
    sending Host's nested payload over whole (bridge adapter.py
    op_circles_send_entity_dm copies params["payload"]; _enrich_and_verify_
    cross_host removes only the keys the bridge stamps). So this is where a
    topic is checked on both ways in.

    `carries_topic` is False for every row that is not a Host-to-Host
    conversation message: another kind, or a person-to-person message (a
    person is one continuous chat, decision 4 of the plan). Those keep no
    topic keys at all. Otherwise a topic is kept only with an id that is a
    uuid; its title is cleaned, and dropped when nothing is left of it."""
    has_keys = any(k in body_payload for k in KEYS)
    if not has_keys:
        return body_payload
    out = {k: v for k, v in body_payload.items() if k not in KEYS}
    if not carries_topic:
        return out
    tid = clean_id(body_payload.get("topic_id"))
    if tid is None:
        return out
    out["topic_id"] = tid
    title = clean_title(body_payload.get("topic_title"))
    if title is not None:
        out["topic_title"] = title
    return out


def wire(topic_id: str | None, title: str | None) -> dict:
    """The nested-payload keys a send carries for a topic: nothing for the
    original conversation, so an older Host stores it as it always has."""
    if topic_id is None or topic_id == ORIGINAL:
        return {}
    out = {"topic_id": topic_id}
    if title:
        out["topic_title"] = title
    return out


def topic_sql(alias: str = "") -> str:
    """The topic a stored row belongs to, as SQL: its topic_id when that is a
    canonical uuid, else ORIGINAL."""
    p = f"{alias}." if alias else ""
    return (f"(CASE WHEN {p}payload->>'topic_id' ~ '{_UUID_SQL}' "
            f"THEN {p}payload->>'topic_id' ELSE '{ORIGINAL}' END)")


#: The rows of one conversation between this Host ($1) and a peer Host ($2):
#: the rows routes/circles.py _read_entity_thread reads and
#: _list_entity_threads lists, written once here so a topic is read over
#: exactly the conversation it lives in.
PAIR_ROWS = """
     WHERE message_kind = 'message' AND archived_at IS NULL
       AND ((from_entity_id = $1 AND to_entity_id = $2)
         OR (from_entity_id = $2 AND to_entity_id = $1))
       -- person ↔ person rows are the People thread, not this one
       AND NOT (from_user_email IS NOT NULL AND to_user_email IS NOT NULL)"""


async def title_of(pool, me: int, peer: int, topic_id: str) -> tuple[bool, str | None]:
    """(whether the conversation with `peer` has a message in `topic_id`,
    its title). The title is the one on the topic's earliest message that
    carries one; None when none did (only a sender other than this code can
    send a topic without one)."""
    row = await pool.fetchrow(
        "SELECT payload->>'topic_title' AS title FROM inbox_messages" + PAIR_ROWS
        + f" AND {topic_sql()} = $3"
        "  ORDER BY (payload->>'topic_title' IS NULL), COALESCE(sent_at, delivered_at), message_id"
        "  LIMIT 1",
        int(me), int(peer), topic_id)
    if row is None:
        return False, None
    return True, clean_title(row["title"])


async def list_topics(pool, me: int, peer: int) -> list[dict]:
    """The topics of this Host's conversation with `peer`: the original
    conversation first (always, even with no message), then the rest by last
    activity, newest first. Each is

      id, title          ORIGINAL / ORIGINAL_TITLE, or the topic's
      started_by         "self" or "peer": whose message opened it (None for
                         the original conversation)
      messages           how many messages it holds
      last_activity_at   its newest message, either side
      last_inbound_at    its newest message from the peer
      seen_at            its own mark (circles_page_marks "<peer>/<topic>")
      unread             the peer's messages newer than both its own mark and
                         the peer's whole-conversation mark ("<peer>")

    One query, over the pair's rows only. The marks' kind is bound ($3), as
    circles_marks binds it, rather than written into the SQL."""
    rows = await pool.fetch(
        f"""
        WITH pair AS (
            SELECT message_id, from_entity_id,
                   COALESCE(sent_at, delivered_at) AS ts,
                   {topic_sql()} AS topic,
                   NULLIF(payload->>'topic_title', '') AS title
              FROM inbox_messages {PAIR_ROWS}
        )
        SELECT p.topic,
               count(*) AS messages,
               max(p.ts) AS last_activity_at,
               max(p.ts) FILTER (WHERE p.from_entity_id = $2) AS last_inbound_at,
               (array_agg(p.title ORDER BY p.ts, p.message_id)
                    FILTER (WHERE p.title IS NOT NULL))[1] AS title,
               (array_agg(p.from_entity_id ORDER BY p.ts, p.message_id))[1] AS started_by,
               max(tm.seen_at) AS seen_at,
               count(*) FILTER (
                   WHERE p.from_entity_id = $2
                     AND (GREATEST(pm.seen_at, tm.seen_at) IS NULL
                          OR p.ts > GREATEST(pm.seen_at, tm.seen_at))) AS unread
          FROM pair p
          LEFT JOIN circles_page_marks pm
                 ON pm.entity_id = $1 AND pm.kind = $3
                AND pm.ref = $2::int::text AND pm.reader = ''
          LEFT JOIN circles_page_marks tm
                 ON tm.entity_id = $1 AND tm.kind = $3
                AND tm.ref = $2::int::text || '/' || p.topic AND tm.reader = ''
         GROUP BY p.topic
        """,
        int(me), int(peer), MARK_KIND)
    by_id = {r["topic"]: r for r in rows}
    original = by_id.pop(ORIGINAL, None)
    if original is None:
        # The original conversation is always the first tab. With no message
        # in it, its own mark is read on its own.
        seen = await pool.fetchval(
            "SELECT seen_at FROM circles_page_marks WHERE entity_id = $1 AND kind = $3 "
            "AND ref = $2 AND reader = ''", int(me), f"{int(peer)}/{ORIGINAL}", MARK_KIND)
        first = {"id": ORIGINAL, "title": ORIGINAL_TITLE, "started_by": None, "messages": 0,
                 "last_activity_at": None, "last_inbound_at": None, "seen_at": seen, "unread": 0}
    else:
        first = _entry(original, me, title=ORIGINAL_TITLE, started=False)
    rest = sorted((_entry(r, me) for r in by_id.values()),
                  key=lambda t: t["last_activity_at"], reverse=True)
    return [first, *rest]


def _entry(r, me: int, *, title: str | None = None, started: bool = True) -> dict:
    by = r["started_by"]
    return {
        "id": r["topic"],
        "title": title if title is not None else clean_title(r["title"]),
        "started_by": (("self" if int(by) == int(me) else "peer") if started and by is not None else None),
        "messages": int(r["messages"] or 0),
        "last_activity_at": r["last_activity_at"],
        "last_inbound_at": r["last_inbound_at"],
        "seen_at": r["seen_at"],
        "unread": int(r["unread"] or 0),
    }
