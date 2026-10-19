"""The message map: which Host conversation each messaging-app message
belongs to (the table is migrations/v203_connector_messages.sql).

The connector route writes a row for every message the paired account sends
(direction 'in', before the turn) and for every message the connector reports
it sent back (direction 'out', POST /api/connector/messages/sent). A reply to
any of them, or a message in a platform thread bound to one, then lands in
that conversation with no pick (4.2 steps 2 and 3).

The scope is one of the three a connector targets: ('thread', <uuid>),
('project', <id>), ('app', 'circles'). Only ids are stored: no message text,
no picture.

When a Split or a project adopt moves a conversation's turns elsewhere,
retag_moved moves the map rows with them, so a reply to an old message
follows the conversation (4.3).
"""
from __future__ import annotations

import bisect
import logging
from typing import Iterable, Optional

log = logging.getLogger("privatae.connectors.message_map")

SCOPE_KINDS = ("thread", "project", "app")

_KEY = "entity_id = $1 AND app_id = $2 AND platform = $3 AND chat_id = $4"


def valid_scope(kind: str, ref: str) -> bool:
    """One of the three scopes a connector targets, and never app:<anything
    but circles> (the table's CHECK says the same)."""
    if kind == "app":
        return ref == "circles"
    return kind in ("thread", "project") and bool(ref)


async def scope_of_message(pool, eid: int, app_id: str, platform: str, chat_id: str,
                           message_id: str) -> Optional[tuple[str, str]]:
    """The conversation a message in this chat belongs to (either direction),
    or None when the map has never seen it."""
    row = await pool.fetchrow(
        f"SELECT scope_kind, scope_ref FROM connector_messages WHERE {_KEY} "
        "  AND external_message_id = $5 ORDER BY created_at DESC LIMIT 1",
        eid, app_id, platform, chat_id, message_id)
    return (row["scope_kind"], row["scope_ref"]) if row else None


async def scope_of_native_thread(pool, eid: int, app_id: str, platform: str, chat_id: str,
                                 native_thread_id: str) -> Optional[tuple[str, str]]:
    """The conversation a platform thread is bound to: its newest row's."""
    row = await pool.fetchrow(
        f"SELECT scope_kind, scope_ref FROM connector_messages WHERE {_KEY} "
        "  AND native_thread_id = $5 ORDER BY created_at DESC, id DESC LIMIT 1",
        eid, app_id, platform, chat_id, native_thread_id)
    return (row["scope_kind"], row["scope_ref"]) if row else None


async def current_scope(pool, eid: int, app_id: str, platform: str,
                        chat_id: str) -> Optional[tuple[str, str]]:
    """The conversation this chat used last, or None for a chat the map has
    never seen."""
    row = await pool.fetchrow(
        f"SELECT scope_kind, scope_ref FROM connector_messages WHERE {_KEY} "
        "ORDER BY created_at DESC, id DESC LIMIT 1", eid, app_id, platform, chat_id)
    return (row["scope_kind"], row["scope_ref"]) if row else None


async def inbound(pool, eid: int, app_id: str, platform: str, chat_id: str,
                  message_id: str):
    """The inbound row for a message already seen (a re-delivery), or None."""
    return await pool.fetchrow(
        f"SELECT id, scope_kind, scope_ref, turn_id FROM connector_messages WHERE {_KEY} "
        "  AND external_message_id = $5 AND direction = 'in'",
        eid, app_id, platform, chat_id, message_id)


async def write_inbound(pool, eid: int, *, app_id: str, grant_id: int, platform: str,
                        chat_id: str, message_id: str, native_thread_id: Optional[str],
                        kind: str, ref: str, turn_id: Optional[str] = None) -> Optional[int]:
    """Write a message the paired account sent, under the grant it came in
    on. Returns the row id, or None when this message was already written (a
    re-delivery)."""
    if not valid_scope(kind, ref):
        raise ValueError(f"not a connector scope: {kind}:{ref}")
    return await pool.fetchval(
        "INSERT INTO connector_messages (entity_id, app_id, grant_id, platform, chat_id, "
        "  external_message_id, native_thread_id, scope_kind, scope_ref, direction, turn_id) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'in', $10) "
        "ON CONFLICT (entity_id, app_id, platform, chat_id, external_message_id, direction) "
        "DO NOTHING RETURNING id",
        eid, app_id, int(grant_id), platform, chat_id, message_id, native_thread_id, kind, ref,
        turn_id)


async def forget_inbound(pool, row_id: int) -> None:
    """Take back an inbound row whose turn never started (refused, or the send
    path failed), so the same message delivered again is placed, not answered
    as a duplicate of nothing. Only a row no turn was ever recorded on."""
    await pool.execute(
        "DELETE FROM connector_messages WHERE id = $1 AND direction = 'in' AND turn_id IS NULL",
        int(row_id))


async def set_turn(pool, row_id: int, turn_id: Optional[str]) -> None:
    await pool.execute("UPDATE connector_messages SET turn_id = $2 WHERE id = $1",
                       int(row_id), turn_id)


async def record_sent(pool, eid: int, *, app_id: str, grant_id: int, platform: str,
                      chat_id: str, turn_id: str, kind: str, ref: str,
                      sent: Iterable[tuple[int, str]],
                      native_thread_id: Optional[str]) -> int:
    """Write the messages the connector sent for a reply, under the grant it
    posted on. A message already written is not written again (the unique
    index), so a /sent posted twice records once. Returns how many rows are
    new."""
    if not valid_scope(kind, ref):
        raise ValueError(f"not a connector scope: {kind}:{ref}")
    n = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for seq, mid in sent:
                got = await conn.fetchval(
                    "INSERT INTO connector_messages (entity_id, app_id, grant_id, platform, "
                    "  chat_id, external_message_id, native_thread_id, scope_kind, scope_ref, "
                    "  direction, turn_id, seq) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'out', $10, $11) "
                    "ON CONFLICT (entity_id, app_id, platform, chat_id, external_message_id, "
                    "  direction) DO NOTHING RETURNING id",
                    eid, app_id, int(grant_id), platform, chat_id, mid, native_thread_id, kind,
                    ref, turn_id, int(seq))
                n += 1 if got is not None else 0
    return n


async def sent_seqs(pool, eid: int, app_id: str, turn_id: str) -> set[int]:
    """The reply parts the connector already reported as sent."""
    rows = await pool.fetch(
        "SELECT DISTINCT seq FROM connector_messages WHERE entity_id = $1 AND app_id = $2 "
        "  AND turn_id = $3 AND direction = 'out' AND seq IS NOT NULL", eid, app_id, turn_id)
    return {int(r["seq"]) for r in rows}


async def turn_home(pool, eid: int, app_id: str, grant_id: Optional[int], turn_id: str):
    """(scope_kind, scope_ref, chat_id, platform) of the message that started
    this grant's reply `turn_id`, or None: what /sent and a reattach past the
    registry's window read when the reply is no longer in memory. Another
    grant's reply, the same app's before a re-pair included, is None, as
    turns.reply_for answers for a reply still in memory."""
    if grant_id is None:
        return None
    row = await pool.fetchrow(
        "SELECT scope_kind, scope_ref, chat_id, platform FROM connector_messages "
        "WHERE entity_id = $1 AND app_id = $2 AND grant_id = $3 AND turn_id = $4 "
        "  AND direction = 'in' ORDER BY created_at LIMIT 1", eid, app_id, int(grant_id), turn_id)
    return (row["scope_kind"], row["scope_ref"], row["chat_id"], row["platform"]) if row else None


# ── following a move ─────────────────────────────────────────────────────────

async def _has_table(conn) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass('connector_messages') IS NOT NULL"))


def _move_set(rows: list, moved: list, staying: list) -> list[int]:
    """Which map rows go with the moved turns. Each map row belongs with a
    turn row: an inbound message with the NEXT turn row after it (it is
    written just before the person's turn row), a sent message with its
    reply's inbound message when there is one (by turn id), else with the
    LAST turn row before it. Pure, so the rule is testable on its own."""
    if not moved:
        return []
    moved = sorted(moved)
    staying = sorted(staying)
    every = sorted(moved + staying)
    tail_moved = bool(moved) and moved[-1] >= (staying[-1] if staying else moved[-1])
    head_moved = bool(moved) and moved[0] <= (staying[0] if staying else moved[0])

    def _next(seq, t):
        i = bisect.bisect_left(seq, t)
        return seq[i] if i < len(seq) else None

    def _prev(seq, t):
        i = bisect.bisect_right(seq, t)
        return seq[i - 1] if i > 0 else None

    out: list[int] = []
    inbound_moved: dict[tuple, bool] = {}
    for r in rows:
        if r["direction"] != "in":
            continue
        nm, ns = _next(moved, r["created_at"]), _next(staying, r["created_at"])
        go = (tail_moved if _next(every, r["created_at"]) is None
              else nm is not None and (ns is None or nm < ns))
        if r["turn_id"]:
            inbound_moved[(r["app_id"], r["turn_id"])] = go
        if go:
            out.append(int(r["id"]))
    for r in rows:
        if r["direction"] != "out":
            continue
        by_turn = inbound_moved.get((r["app_id"], r["turn_id"])) if r["turn_id"] else None
        if by_turn is None:
            pm, ps = _prev(moved, r["created_at"]), _prev(staying, r["created_at"])
            by_turn = (head_moved if _prev(every, r["created_at"]) is None
                       else pm is not None and (ps is None or pm > ps))
        if by_turn:
            out.append(int(r["id"]))
    return out


async def retag_moved(conn, eid: int, *, src: tuple[str, str], dst: tuple[str, str],
                      moved_rows: list, staying_times) -> int:
    """After a Split or an adopt moved turns from `src` to `dst`: move the map
    rows that belong with them (_move_set). `moved_rows` are the moved turn
    rows (with created_at); `staying_times` is an async callable answering the
    created_at of the turn rows that stayed in `src`, called only when `src`
    has map rows at all. Runs on the mover's connection, inside its
    transaction where it has one. A Host with no map yet, or none in `src`
    (no connector ever wrote there), reads nothing more."""
    src = (str(src[0]), str(src[1]))
    dst = (str(dst[0]), str(dst[1]))
    if not valid_scope(*src) or not valid_scope(*dst) or not moved_rows:
        return 0
    if not await _has_table(conn):
        return 0
    rows = await conn.fetch(
        "SELECT id, app_id, direction, created_at, turn_id FROM connector_messages "
        "WHERE entity_id = $1 AND scope_kind = $2 AND scope_ref = $3 ORDER BY created_at, id",
        eid, src[0], src[1])
    if not rows:
        return 0
    ids = _move_set(list(rows), [r["created_at"] for r in moved_rows], list(await staying_times()))
    if not ids:
        return 0
    await conn.execute(
        "UPDATE connector_messages SET scope_kind = $2, scope_ref = $3 "
        "WHERE entity_id = $1 AND id = ANY($4::bigint[])", eid, dst[0], dst[1], ids)
    log.info("connector map: %d row(s) followed %s:%s -> %s:%s", len(ids), *src, *dst)
    return len(ids)
