"""
mastercode/pending_words.py — the person's words, held by the server from the
moment it receives them until a turn has them (migrations/v222_pending_words.sql
says why: a reply typed into Circles mid-turn vanished on 2026-10-11, because
the only copy of words the turn had not read yet was in process memory and in
the page's).

A row per message, written before the route that received the words answers
(hold). Whatever takes the words removes the row, so nothing is delivered twice:

  - the running turn reads them (session.py's drain)          -> settle
  - they start a turn as its message, which has started
    (turn_registry: the turn's session frame)                  -> settle
  - the Host restarts with rows left                           -> deliver_left
    starts a turn in each room with its words, once, and that
    turn's start settles them as above.

In PII mode nothing is written (pii_queue: raw text at rest is the leak that
mode forbids); hold answers None and the words stay in process memory only,
as they were before this module.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("cseries.mastercode.pending_words")

# Settles in flight: held, because the loop keeps only a weak reference to a
# task nobody awaits.
_settling: set = set()
# Rows deliver_left has handed to a turn in this process, or a resend has
# claimed (claim_resend). The turn settles them when it starts; until then a
# later pass must not hand them again. Checked and added in one step (nothing
# awaits between), so the boot pass and a resend never both take a row.
_handed: set[str] = set()
# Sender ids (client_id), with the app that sent each (SentId: (app, id)), of
# words this process has held (hold) or handed on
# (deliver_left, claim_resend), whether or not a turn has settled them since:
# a resend under one of them must not deliver the words again. Insertion-
# ordered and capped, oldest out (a resend comes within moments).
_seen_cids: dict[tuple, None] = {}
_SEEN_CIDS_MAX = 4096


def sender_of(app_id) -> str:
    """The app that sent words, as a sender's id (client_id) is told apart by:
    the app the route resolved (the app token's app_id on /api/brain; the
    Circles page's own routes, routes/circles.py, which send as "circles"),
    or "chat" for a send with no app, the name compose_thread_key gives that
    conversation. The one place this rule lives.

    "chat" and "circles" are NOT one sender. The Circles page never posts
    through the chat route (/api/brain/messages): its sends and nudges go
    through /api/circles/messages and /api/circles/nudge, which name the app
    themselves. A send with no app token is any page that did not carry one,
    so it shares the "chat" ids with no other app. That memory_engine reads
    rows of "", "chat" and "circles" as one conversation is about where the
    words are read, not who sent them."""
    return str(app_id) if isinstance(app_id, str) and app_id else "chat"


class SentId(str):
    """A sender's id for words (client_id), with the app that sent them
    (`sender`, sender_of). It reads and serializes as the id itself, so every
    frame, row and page sees the id it sent; the sender rides with it in
    process memory (the registry's records, the session's nudge queue), and
    is stored beside it where the id is stored (pending_words.client_app,
    the user row's client_keys). An id counts as delivered only for the app
    that sent it: app A's "1" says nothing about app B's "1" (same_sent)."""
    sender: str

    def __new__(cls, value: str, sender):
        obj = super().__new__(cls, value)
        obj.sender = sender_of(sender)
        return obj


def sent_ids(value, app_id):
    """`value` (an id, None, or (nested) lists of ids) with each plain id
    marked as sent by `app_id`. An id that already knows its sender keeps it."""
    if isinstance(value, SentId):
        return value
    if isinstance(value, str):
        return SentId(value, app_id) if value else value
    if isinstance(value, (list, tuple)):
        return [sent_ids(v, app_id) for v in value]
    return value


def same_sent(a, b) -> bool:
    """Whether two ids are the same app's same id. An id whose sender is not
    known matches nothing: the words are delivered, never taken for another
    app's."""
    sa, sb = getattr(a, "sender", None), getattr(b, "sender", None)
    return (isinstance(a, str) and isinstance(b, str) and sa is not None
            and sa == sb and str(a) == str(b))


def _sent_key(c) -> Optional[tuple]:
    s = getattr(c, "sender", None)
    return (s, str(c)) if isinstance(c, str) and c and s else None


def sender_keys(value) -> list[str]:
    """What a user row keeps under metadata.client_keys for its ids (`value`,
    session.row_client_id's shape): "<app>/<id>" for each id that knows its
    sender, the id as client_id_key gives it (its digest in PII mode, never
    the id as written). What delivered_row matches by, beside the id."""
    from mastercode import pii_queue
    out: list[str] = []
    for v in (value if isinstance(value, (list, tuple)) else [value]):
        for c in (v if isinstance(v, (list, tuple)) else [v]):
            k = _sent_key(c)
            if k is not None:
                key = f"{k[0]}/{pii_queue.client_id_key(k[1])}"
                if key not in out:
                    out.append(key)
    return out


def _note_cids(value) -> None:
    for c in (value if isinstance(value, (list, tuple)) else [value]):
        k = _sent_key(c)
        if k is not None:
            _seen_cids.pop(k, None)
            _seen_cids[k] = None
    while len(_seen_cids) > _SEEN_CIDS_MAX:
        _seen_cids.pop(next(iter(_seen_cids)))


def durable() -> bool:
    """Whether words are held in the database (False in PII mode)."""
    try:
        from mastercode import pii_queue
        return not pii_queue.enabled()
    except Exception:  # noqa: BLE001
        return True


def keys_of(value: Any) -> list[str]:
    """The row keys in `value`: one key, None, or (nested) lists of them."""
    out: list[str] = []
    for v in (value if isinstance(value, (list, tuple)) else [value]):
        for k in (v if isinstance(v, (list, tuple)) else [v]):
            if isinstance(k, str) and k and k not in out:
                out.append(k)
    return out


async def hold(pool, entity_id, session_id: str, text: str, *, client_id=None,
               tags: Optional[dict] = None, room: Optional[dict] = None,
               thread_key: Optional[str] = None) -> Optional[str]:
    """Write the words; returns the row's key. None when nothing is written by
    design (no pool, PII mode, no words). Raises when the write fails: the
    caller must not say it has the words then."""
    if pool is None or not durable() or not (text or "").strip() or not session_id:
        return None
    key = str(uuid.uuid4())
    cid = str(client_id) if isinstance(client_id, str) else (
        json.dumps(client_id) if client_id else None)
    # The app that sent each id, beside it (SentId): one, or a list in the
    # order of a list of ids.
    app = (getattr(client_id, "sender", None) if isinstance(client_id, str) else (
        json.dumps([getattr(c, "sender", None) for c in client_id])
        if isinstance(client_id, (list, tuple)) and client_id else None))
    await pool.execute(
        "INSERT INTO pending_words (id, entity_id, session_id, thread_key, room, text, tags,"
        " client_id, client_app)"
        " VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6, $7::jsonb, $8, $9)",
        key, int(entity_id), str(session_id), thread_key,
        json.dumps(room) if isinstance(room, dict) else None, text,
        json.dumps(tags) if tags else None, cid, app)
    _note_cids(client_id)
    return key


async def settle(pool, keys) -> int:
    """The words of these rows are in a turn now: the rows go. Returns how many
    went (0 for keys already settled)."""
    ks = keys_of(keys)
    if pool is None or not ks:
        return 0
    res = await pool.execute("DELETE FROM pending_words WHERE id = ANY($1::uuid[])", ks)
    try:
        return int(str(res).split()[-1])
    except (ValueError, IndexError):
        return 0


def settle_later(pool, keys) -> None:
    """settle, from code that cannot await (a turn's teardown, a frame
    observer). Never raises; a failure is logged with the count."""
    ks = keys_of(keys)
    if pool is None or not ks:
        return

    async def _run():
        try:
            await settle(pool, ks)
        except Exception as e:  # noqa: BLE001
            log.error("PENDING_WORDS_NOT_SETTLED n=%d — a restart would deliver them again: %s",
                      len(ks), e)
    try:
        t = asyncio.get_running_loop().create_task(_run(), name="pending-words-settle")
    except RuntimeError:
        return
    _settling.add(t)
    t.add_done_callback(_settling.discard)


async def claim_resend(pool, entity_id, client_id) -> tuple[str, list[str]]:
    """A send under `client_id` whose words the server may hold already: the
    Workbench send's one silent retry after its stream died before anything
    showed (use-workbench-chat attemptTurn), whose first attempt landed in a
    running turn as a nudge (brain_service.send_message), perhaps in a process
    that has ended since.

      ("none", [])       no row holds words sent under it, and this process
                         never held or handed any: the send goes on.
      ("held", [])       this process held them (a turn has them, or reads
                         them), or handed them on to a turn (the boot pass):
                         the send must not deliver them again.
      ("claimed", keys)  rows a gone process left that nothing here has
                         handed on: they are this send's now, taken from the
                         boot pass in the same step as the check, and go with
                         its words (send_message pending_keys). A send that
                         cannot start gives them back (unclaim)."""
    sk = _sent_key(client_id)
    if pool is None or sk is None or not durable():
        return "none", []
    if sk in _seen_cids:
        return "held", []
    # Only the same app's words under the id (SentId): another app's words
    # under the same id are not this send's to follow or to take.
    rows = await pool.fetch(
        "SELECT id::text AS id FROM pending_words WHERE entity_id = $1 AND client_id = $2"
        " AND client_app = $3 ORDER BY received_at, id", int(entity_id), sk[1], sk[0])
    # No await from here to the claim: the check and the claim are one step.
    if sk in _seen_cids:
        return "held", []
    keys = [r["id"] for r in rows if r["id"] not in _handed]
    if not keys:
        return ("held", []) if rows else ("none", [])
    _handed.update(keys)
    _note_cids(client_id)
    return "claimed", keys


async def delivered_row(pool, entity_id, client_id) -> bool:
    """Whether a user row of this Host's threads carries `client_id`: the
    words sent under it are in a thread already, as a turn's message or a
    nudge a turn read (session.py keeps the id on the row it writes for
    them: metadata.client_id, or in PII mode its digest under
    client_id_digest; one id, or a list for messages joined into one turn),
    sent by the same app (metadata.client_keys, sender_keys).
    What a send under the id is answered from once the turn registry has let
    that turn go (brain_service.send_message). Matched by the expression
    idx_entity_memory_client_id (v218) is on. Raises on a failed read."""
    keys = sender_keys(client_id)
    if pool is None or not keys:
        return False
    from mastercode import pii_queue
    # The id narrows (the index), the row's client_keys say which app sent it:
    # a row of app A's words under the id is not app B's words delivered.
    return bool(await pool.fetchval(
        "SELECT 1 FROM entity_memory WHERE entity_id = $1"
        " AND COALESCE(metadata->'client_id', metadata->'client_id_digest') @> to_jsonb($2::text)"
        " AND metadata->'client_keys' @> to_jsonb($3::text)"
        " AND metadata->>'role' = 'user' LIMIT 1",
        int(entity_id), pii_queue.client_id_key(str(client_id)), keys[0]))


def unclaim(keys, client_id=None) -> None:
    """Rows a resend claimed and could not deliver go back to the boot pass."""
    _handed.difference_update(keys_of(keys))
    sk = _sent_key(client_id)
    if sk is not None:
        _seen_cids.pop(sk, None)


async def held_for(pool, entity_id, thread_key: str) -> list[dict]:
    """The words held for one conversation, oldest first: what a page shows as
    sent and waiting for the Host to read."""
    if pool is None or not thread_key:
        return []
    rows = await pool.fetch(
        "SELECT id::text AS id, text, client_id, received_at FROM pending_words"
        " WHERE entity_id = $1 AND thread_key = $2 ORDER BY received_at, id",
        int(entity_id), thread_key)
    return [{"id": r["id"], "text": r["text"], "client_id": r["client_id"],
             "received_at": r["received_at"].isoformat() if r["received_at"] else None}
            for r in rows]


async def left_before(pool, entity_id, before: datetime) -> list[dict]:
    """Rows written before `before` (by a process that is gone), oldest first."""
    rows = await pool.fetch(
        "SELECT id::text AS id, session_id, thread_key, room, text, tags, client_id, client_app,"
        " received_at"
        " FROM pending_words WHERE entity_id = $1 AND received_at < $2"
        " ORDER BY received_at, id", int(entity_id), before)
    out = []
    for r in rows:
        room = r["room"]
        tags = r["tags"]
        out.append({"id": r["id"], "session_id": r["session_id"], "thread_key": r["thread_key"],
                    "room": json.loads(room) if isinstance(room, str) else (room or None),
                    "text": r["text"],
                    "tags": json.loads(tags) if isinstance(tags, str) else (tags or None),
                    "client_id": _sent_back(_cid_back(r["client_id"]), _cid_back(r["client_app"]))})
    return out


def _sent_back(cid, app):
    """A row's id(s) with the app that sent each (client_app), as held."""
    if isinstance(cid, str):
        return SentId(cid, app) if isinstance(app, str) and app else cid
    if isinstance(cid, list):
        apps = app if isinstance(app, list) else []
        return [SentId(c, apps[i]) if isinstance(c, str) and i < len(apps)
                and isinstance(apps[i], str) and apps[i] else c for i, c in enumerate(cid)]
    return cid


def _cid_back(raw):
    if not raw:
        return None
    if raw.startswith("["):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


async def _start_turn(entity_id, room: dict, text: str, *, client_id=None,
                      frame: Optional[dict] = None, keys: list[str]) -> bool:
    """A turn of its own in `room` for words a gone process held, the way an
    answer after its asking turn ended starts one (session.answer_after_turn):
    the registry settles the rows when the turn has started."""
    from server import brain_service
    kw: dict = {}
    if client_id:
        kw["client_id"] = client_id
    if frame:
        kw["frame"] = frame
    agen = brain_service.send_message(
        entity_id=entity_id, message=text, session_id=None,
        app_id=room.get("app_id"), project_id=room.get("project_id"),
        thread_id=room.get("thread_id"), history_context=room.get("history_context"),
        # Held words passed the root thread's check when they were held: a
        # nudge route refuses a caller that is not the person before it
        # holds anything, and send_message holds only after its own check
        # (runtime/root_turn.words_refusal). A forwarded or carried message
        # still rides `frame` and is still refused there.
        from_person=True,
        pending_keys=keys, **kw)
    try:
        first = await agen.__anext__()
    except StopAsyncIteration:
        return False
    if (first or "").startswith("3:"):
        log.warning("pending words: the turn for them was refused: %s", first[:200])
        with _suppress():
            await agen.aclose()
        return False

    async def _follow():
        try:
            async for _f in agen:
                pass
        except Exception as e:  # noqa: BLE001
            log.debug("pending words: following their turn ended early: %s", e)
    t = asyncio.ensure_future(_follow())
    _settling.add(t)
    t.add_done_callback(_settling.discard)
    return True


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


async def deliver_left(pool, entity_id, before: datetime, *,
                       start: Optional[Callable[..., Awaitable[bool]]] = None) -> int:
    """Words a process that is gone held and no turn took: each conversation's
    words, joined in the order they were sent, start one turn there, as the
    words a turn ends without reading go on (turn_registry
    ._requeue_undelivered). Returns how many turns started. A conversation
    whose turn cannot start keeps its rows, for the next pass."""
    if pool is None or not durable():
        return 0
    rows = [r for r in await left_before(pool, entity_id, before) if r["id"] not in _handed]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["thread_key"] or f"session|{r['session_id']}", []).append(r)
    started = 0
    from mastercode.runtime.session import row_client_id
    for _key, rs in groups.items():
        # Claimed here, in the same step as the check (a resend may have
        # claimed some while an earlier group's turn was starting).
        rs = [r for r in rs if r["id"] not in _handed]
        if not rs:
            continue
        ids = [r["id"] for r in rs]
        _handed.update(ids)
        _note_cids([r["client_id"] for r in rs])
        text = "\n\n".join(r["text"] for r in rs)
        cid = row_client_id([r["client_id"] for r in rs])
        origins = [(r["tags"] or {}).get("connector_origin") for r in rs]
        carries = [(r["tags"] or {}).get("carry") for r in rs]
        frame: dict = {}
        if any(origins):
            frame["connector_origin"] = origins[0] if len(origins) == 1 else origins
        if any(carries):
            frame["carry"] = carries[0] if len(carries) == 1 else carries
        room = next((r["room"] for r in rs if isinstance(r["room"], dict)), None) or {}
        try:
            ok = await (start or _start_turn)(entity_id, room, text, client_id=cid,
                                              frame=frame or None, keys=[r["id"] for r in rs])
        except Exception as e:  # noqa: BLE001
            log.error("pending words: %d message(s) left by the last process could not start "
                      "a turn: %s", len(rs), e)
            ok = False
        if not ok:
            _handed.difference_update(ids)
            for r in rs:
                for c in (r["client_id"] if isinstance(r["client_id"], list) else [r["client_id"]]):
                    sk = _sent_key(c)
                    if sk is not None:
                        _seen_cids.pop(sk, None)
        else:
            started += 1
            log.info("PENDING_WORDS_DELIVERED n=%d — left by the last process, sent on as a turn",
                     len(rs))
    return started


async def deliver_left_loop(pool, entity_id, before: datetime, *,
                            first_wait_s: float = 5.0, max_wait_s: float = 600.0) -> None:
    """The lifespan's pass: deliver what the last process left, retrying with a
    doubling wait while any is left (a turn refused, the runtime not up yet),
    for as long as the process runs."""
    wait = first_wait_s
    while True:
        await asyncio.sleep(wait)
        try:
            await deliver_left(pool, entity_id, before)
            left = [r for r in await left_before(pool, entity_id, before) if r["id"] not in _handed]
        except Exception as e:  # noqa: BLE001
            log.warning("pending words: the boot pass failed, retrying: %s", e)
            left = 1
        if not left:
            return
        wait = min(max(wait * 2, 30.0), max_wait_s)
