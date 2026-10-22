"""Words the person sends into a running turn (nudges), in the order they were sent, and the turn's
pending input events.

Moved out of runtime/session.py, which re-exports every name defined here."""

import itertools
import logging
import re
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("cseries.mastercode.entity_runtime")


# ── Nudge queue ──────────────────────────────────────────────────────
# User can mid-stream "nudge" a running session — the
# nudge text gets injected as a priority message at the START of the
# next tool-loop round, and the tool-round counter resets. This is
# real influence on the thought chain, in contrast with a side-chat
# pattern. Module-level dict keyed by
# session_id; HTTP nudge route writes, the tool loop drains.
_pending_nudges: dict[str, list[str]] = {}
# When each queued nudge was sent (same order as _pending_nudges): the user
# row the drain writes for it is stamped with the moment the person sent it.
_nudge_times: dict[str, list] = {}
# The ORDER of what passes between the person and a running turn: every word pushed into a session and every piece of
# text a turn sends draws the next number of one counter (send_order), so
# "was this piece sent before those words?" compares two numbers — exact,
# with no clock in it. Same order as _pending_nudges.
_nudge_orders: dict[str, list[int]] = {}
# What a queued nudge's user row carries besides its words (same order as
# _pending_nudges; None for most). Only NUDGE_ROW_TAGS pass: a connector's
# forwarded message keeps its connector_origin when it lands in a running
# turn, as it does when it starts one (the frame, read at the turn's row).
_nudge_tags: dict[str, list] = {}
NUDGE_ROW_TAGS = ("connector_origin", "carry")
# The id the sender gave each queued nudge (same order as _pending_nudges; None
# for most): the turn's nudge_read receipt and the nudge's user row carry it,
# so the sender knows its own words whatever the platform made of their text
# (secret shapes cut, the frame's 500-character cut, trimming). Matched by
# text, a nudge with a pasted key would never match its receipt, and the page sent the words again as the next message.
_nudge_ids: dict[str, list] = {}
_NUDGE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Whether each queued nudge came through a route whose client KEEPS a copy and
# sends it again itself when no nudge_read receipt comes (same order as
# _pending_nudges). Decided by the route on the server side, never by anything
# the client sends: the page's POST /api/brain/turns/{id}/nudge
# (turn_registry.nudge) and the Circles nudge route (routes/circles.nudge).
# Every other way words become a nudge (a ticket's answer from the decide
# route, answer_into_turn; a connector's message, a Circles composer send or
# another tab's send that became a nudge through send_message; words sent on
# from a turn that ended) is the server's to deliver: when the turn ends
# without reading them, the server sends them on as the thread's next turn,
# once (end_turn_input).
_nudge_keeps_copy: dict[str, list[bool]] = {}
# The pending_words row(s) holding each queued nudge's words (same order as
# _pending_nudges; None for words no row holds: PII mode, a route that writes
# none). The row goes when a turn has the words (mastercode/pending_words):
# the drain settles it when it reads them; words a turn ends without reading
# carry it on to the thread's next turn (end_turn_input), which settles it
# once it has started.
_nudge_keys: dict[str, list] = {}


def nudge_id_ok(client_id) -> bool:
    """A sender's nudge id: 1-64 of [A-Za-z0-9_-]."""
    return isinstance(client_id, str) and bool(_NUDGE_ID.fullmatch(client_id))


def row_client_id(value):
    """What a user row keeps under metadata.client_id for `value`: one sender's
    id as itself, and the ids of several messages that go on as ONE turn or
    nudge (words a turn ended without reading, joined:
    turn_registry._requeue_undelivered) as a list, in the order of their
    words, each once. `value` is an id, None, or a list of ids or of such
    lists (words joined, unread again, joined again). What is not an id
    (nudge_id_ok) is left out; None when no id is left. A lookup by one id
    matches the row either way (thread_pick.find_index_send)."""
    ids: list = []
    for v in (value if isinstance(value, (list, tuple)) else [value]):
        for x in (v if isinstance(v, (list, tuple)) else [v]):
            if nudge_id_ok(x) and x not in ids:
                ids.append(x)
    if not ids:
        return None
    return ids[0] if len(ids) == 1 else ids


def _id_echo(client_id) -> dict:
    """What a nudge_read receipt says of the words' id: the id, and the app
    that sent it (pending_words.SentId), so the registry keeps it as that
    app's (turn_registry._observe, read_ids)."""
    if not client_id:
        return {}
    s = getattr(client_id, "sender", None)
    return {"client_id": client_id, **({"client_app": s} if s else {})}


def _sender_tags(client_id) -> dict:
    """What a user row keeps of which app sent each of its ids: client_keys
    ("<app>/<id>", the id's digest in PII mode; pending_words.sender_keys),
    which delivered_row matches by. Empty when no id knows its sender."""
    try:
        from mastercode.pending_words import sender_keys
        keys = sender_keys(client_id)
    except Exception as e:  # noqa: BLE001
        log.debug("sender keys not read (non-fatal): %s", e)
        return {}
    return {"client_keys": keys} if keys else {}


def _turn_row_ids(client_id):
    """The ids the running turn's user row keeps: `client_id` and those of
    words that attached to the turn (server/turn_registry.turn_row_ids).
    `client_id` alone where there is no registry."""
    try:
        from server.turn_registry import turn_row_ids
    except Exception:  # noqa: BLE001 — no server in this process
        return client_id
    return turn_row_ids(client_id)


def _turn_row_task(task) -> None:
    """The running turn's user row is written by `task`
    (server/turn_registry.turn_row_task). Never raises."""
    try:
        from server.turn_registry import turn_row_task
    except Exception:  # noqa: BLE001 — no server in this process
        return
    turn_row_task(task)
_send_order = itertools.count(1)


def send_order() -> int:
    """The next number in the one order of words and sent text."""
    return next(_send_order)


def last_nudge_order(session_id: str) -> int | None:
    """The order of the LATEST words queued for this session, read without
    draining them (None: nothing queued)."""
    orders = _nudge_orders.get(session_id) if session_id else None
    return max(orders) if orders else None


def nudge_words(text: str) -> str:
    """The words of a nudge as the turn reads them and the server holds them:
    stripped, with secret shapes cut (push_nudge's door). A route that holds
    the words (pending_words.hold) writes these, never the raw text."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        from mastercode import secret_shapes as _secret_shapes
        text, _findings = _secret_shapes.redact(text)
    except Exception as _ss_err:
        log.warning("Secret-shape screen on nudge failed (non-fatal): %s", _ss_err)
    return text.strip()


def push_nudge(session_id: str, text: str, tags: Optional[dict] = None,
               client_id: Optional[str] = None, keeps_copy: bool = False,
               pending_key=None) -> None:
    """Append a nudge to the per-session pending queue. Called by
    /api/chat/nudge — completes synchronously so the HTTP caller
    knows the nudge landed. The tool loop picks it up on the next
    round boundary. `tags`: what its user row carries besides the words
    (only the NUDGE_ROW_TAGS keys are kept). `client_id`: the sender's id for
    these words (nudge_id_ok), carried by the receipt and the row; or the ids
    of several messages' words sent on as this one nudge (row_client_id),
    which the row keeps as a list.
    `keeps_copy`: the words came through a route whose client keeps a copy
    and sends it again itself (_nudge_keeps_copy); words without it that the
    turn never reads go on as the thread's next turn (end_turn_input).
    `pending_key`: the pending_words row(s) holding the words (_nudge_keys)."""
    text = (text or "").strip()
    if not text or not session_id:
        return
    # Secret shapes at the door: a nudge is
    # injected verbatim into the model's message array at the drain site,
    # and the composer's Nudge button reads the textarea directly — so this
    # is the one seat every nudge passes through. Same rules file, same hard
    # shapes, same marker as send_message's gate.
    try:
        from mastercode import secret_shapes as _secret_shapes
        text, _findings = _secret_shapes.redact(text)
        if _secret_shapes.hard(_findings):
            log.warning("Secret shapes redacted from nudge (session %s): %s",
                        session_id, [f.id for f in _secret_shapes.hard(_findings)])
        if not text.strip():
            return
    except Exception as _ss_err:
        log.warning("Secret-shape screen on nudge failed (non-fatal): %s", _ss_err)
    kept = ({k: tags[k] for k in NUDGE_ROW_TAGS if tags.get(k)}
            if isinstance(tags, dict) else {})
    _pending_nudges.setdefault(session_id, []).append(text)
    _nudge_times.setdefault(session_id, []).append(datetime.now(timezone.utc))
    _nudge_orders.setdefault(session_id, []).append(send_order())
    _nudge_tags.setdefault(session_id, []).append(kept or None)
    _nudge_ids.setdefault(session_id, []).append(row_client_id(client_id))
    _nudge_keeps_copy.setdefault(session_id, []).append(bool(keeps_copy))
    _nudge_keys.setdefault(session_id, []).append(pending_key or None)


def consume_nudges(session_id: str) -> list[str]:
    """Atomically drain pending nudges for a session. Tool loop calls
    this between rounds — returns the queued text(s) in arrival order
    and clears the queue."""
    if not session_id:
        return []
    _nudge_times.pop(session_id, None)
    _nudge_orders.pop(session_id, None)
    _nudge_tags.pop(session_id, None)
    _nudge_ids.pop(session_id, None)
    _nudge_keeps_copy.pop(session_id, None)
    _nudge_keys.pop(session_id, None)
    pending = _pending_nudges.pop(session_id, None)
    return pending or []


def nudge_row_tags(session_id: str) -> dict[str, dict]:
    """What the queued nudges' user rows carry besides their words, by text
    (only those that carry something), read WITHOUT draining: the drain reads
    this, then consume_nudges_timed, with no await between."""
    if not session_id:
        return {}
    texts = _pending_nudges.get(session_id) or []
    tags = _nudge_tags.get(session_id) or []
    return {t: tags[i] for i, t in enumerate(texts) if i < len(tags) and tags[i]}


def nudge_ids(session_id: str) -> list:
    """The senders' ids of the queued nudges, in queue order (None where a
    sender gave none), read WITHOUT draining: the drain reads this, then
    consume_nudges_timed, with no await between."""
    if not session_id:
        return []
    return list(_nudge_ids.get(session_id) or [])


def nudge_pending_keys(session_id: str) -> list:
    """The pending_words keys of the queued nudges, in queue order (None where
    no row holds the words), read WITHOUT draining, like nudge_ids."""
    if not session_id:
        return []
    return list(_nudge_keys.get(session_id) or [])


def consume_nudges_timed(session_id: str) -> list[tuple[str, "datetime"]]:
    """consume_nudges, with the moment each was sent."""
    if not session_id:
        return []
    _nudge_orders.pop(session_id, None)
    _nudge_tags.pop(session_id, None)
    _nudge_ids.pop(session_id, None)
    _nudge_keeps_copy.pop(session_id, None)
    _nudge_keys.pop(session_id, None)
    texts = _pending_nudges.pop(session_id, None) or []
    times = _nudge_times.pop(session_id, None) or []
    now = datetime.now(timezone.utc)
    return [(t, times[i] if i < len(times) else now) for i, t in enumerate(texts)]


def has_pending_nudge(session_id: str) -> bool:
    """Whether the person has words queued for this session — read without
    draining, so the loop can leave them to the one drain that handles them."""
    return bool(session_id and _pending_nudges.get(session_id))


# ── A turn holds open for what it started ──
# A Host should wake when a coder finishes: the turn holds open until the
# work is done, and the prompt cache is preserved. Three kinds of work outlive the round that starts them: a
# question in the box (`escalate` — the ask card; its answer comes back as a
# nudge), a `build` on the harness pool, and a dispatched agent. Until then
# nothing waited for any of them. The Host wrote its reply and the turn ended;
# a click became a new message, and a finished build woke a TOOL-LESS phrasing
# call (the old deliver_agent_result) that could only describe it and propose
# a next step — so the phases a Host had promised never started.
#
# Now the turn holds at its exit for what it started, at most HOLD_S in a turn
# (summed): inside the 5-minute prompt-cache window, far under
# turn_registry.TURN_TIMEOUT_S. What arrives meanwhile (the person's words, a
# result) enters the running turn through the same drain as a nudge, and the
# Host goes on with its tools. A result that lands after the turn ended wakes
# a REAL turn in the thread (EntityRuntime.continue_after_result). The wire
# stays alive on its own: brain_service._never_silent puts a 2:[] keepalive on
# it after every 8 s of quiet, whatever the runtime is doing.
#
# What a holding turn holds: its turn_registry entry and the memory of its
# turn — no lock, connection or event-loop time (an asyncio sleep between
# polls). A turn holds, and a woken turn starts, when the Host's memory holds
# one more turn (runtime/host_memory: the memory is the
# Host's to manage, so there is no count of background turns).
HOLD_S = 240.0
# The queues are in-process dicts, so polling them is free.
ASK_POLL_S = 0.5
# An open ask is answered INTO the turn, whatever surface the person answers
# on: deciding the ticket pushes the ask card's words into the session that
# asked (tickets.decide_ticket_service -> answer_into_turn), so the hold hears
# the Workbench card and the Tickets pane exactly as it hears Circles.

_TICKET_REF = re.compile(r"(?i)\bticket\s+#?(\d+)")


def asks_still_open(open_ids: list[int], nudges: list[str]) -> list[int]:
    """The asks a batch of the person's words leaves unanswered. Words naming a
    ticket — the ask card's "Ticket #12 — I chose …" / "Not now on ticket
    #12 …" (app-web-kit/ask.ts answerText / notNowText) — answer that ticket;
    words naming none are the person talking to the Host, which answers every
    ask (the Host reads them and decides what they settle)."""
    left = list(open_ids or [])
    for n in nudges or []:
        refs = {int(x) for x in _TICKET_REF.findall(n or "")}
        if not refs:
            return []
        left = [t for t in left if t not in refs]
    return left


# Results for a running turn: the brief a finished build or agent hands in,
# drained at the top of the tool loop beside the person's nudges (not wrapped
# as a USER NUDGE — the brief says what it is).
_turn_events: dict[str, list[str]] = {}


def push_turn_event(session_id: str, text: str) -> None:
    if session_id and (text or "").strip():
        _turn_events.setdefault(session_id, []).append(text)


def consume_turn_events(session_id: str) -> list[str]:
    if not session_id:
        return []
    return _turn_events.pop(session_id, None) or []


def has_pending_input(session_id: str) -> bool:
    """Words from the person or a result, queued for this session."""
    return bool(session_id and (_pending_nudges.get(session_id)
                                or _turn_events.get(session_id)))
