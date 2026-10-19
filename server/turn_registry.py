"""
server/turn_registry.py — detached, visible, reattachable chat turns.

A turn started from a phone died in the UI the moment
the screen turned off, kept running invisibly on the backend, and every retry
started ANOTHER full turn — three claude subprocesses, triple subscription
spend, nothing on screen. Two standing rules were being violated at once:
no hidden processes, and the connection must never own the turn.

The registry makes the chat turn a first-class unit of work:

  - a turn runs as a REGISTERED asyncio task detached from any HTTP response;
    dropping the connection kills only that response's tail, never the turn;
  - every wire frame the turn produces is buffered in order (seq) and fanned
    out to any number of live tails; a reattaching client replays from seq 0
    (or its last seq) and then goes live — same bytes either way;
  - a duplicate send of the SAME text while a turn runs on that thread is a
    pure reattach (the phone-retry case): never a second turn, never double
    subscription spend;
  - a DIFFERENT text while a turn runs becomes a nudge into the live tool
    loop (session.push_nudge — the existing mid-stream-influence design) and
    the sender reattaches to the running stream;
  - turns are listable and cancellable (the UI stop button). Cancel closes
    the inner frame generator, whose finally cancels the runtime task, which
    tears down the engine (and with it the SDK's claude subprocess). Nothing
    keeps running that the user can't see and can't stop.

Process-local by design: the registry lives in the same server process that
serves the SSE responses (one process per entity container), so an in-memory
buffer IS the durable-enough store for reattach. The chat log itself still
lands in entity_memory turn rows exactly as before — this buffer only covers
the live/reattach window (finished records are kept DONE_TTL_S then dropped).

One exception: a turn that is STOPPED never reaches the
runtime's own assistant-row write, so it used to leave nothing in the chat log
at all and the next turn could not see the tool calls that had completed inside
it. _leave_stopped_row writes a short assistant row naming them, tagged
origin='cancelled' so a client renders it as a system note.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Optional

log = logging.getLogger("cseries.turn_registry")

# How long a FINISHED turn stays reattachable (a phone that slept through the
# whole turn still renders the result on wake without a history refetch).
DONE_TTL_S = int(os.environ.get("CSERIES_TURN_DONE_TTL_S", "900"))

def _env_seconds(name: str, default: float) -> float:
    """A seconds value from the environment; unset, empty or unreadable is
    `default`."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        log.warning("%s=%r is not a number of seconds — using %s", name, raw, default)
        return float(default)


# LIVENESS, NOT A WALL CLOCK (a host can handle continuous 24/7 work). A turn
# is torn down only when it has shown NO SIGN OF LIFE for TURN_IDLE_S: no frame
# from the turn (text, a tool call or result, a status frame — the 2:[] wire
# keepalive is brain_service's, never the turn's, and is not counted) and no
# note_alive() from it. That is what an orphan looks like; a turn doing real
# work keeps emitting, however long it runs. This was once a 30-minute TOTAL:
# at the 27-32 s per model call measured on one Host's long turns it ended live
# work at about 60 calls, well short of the 100+-tool workflows the round
# backstop is sized for. The 30-minute figure survives as the idle window, the
# same one the agent reaper uses (brain_service.py, stuck_after_seconds=1800).
TURN_IDLE_S = _env_seconds("CSERIES_CHAT_TURN_IDLE_S", 1800)

# An OPT-IN total, for an operator who wants one on a Host: unset (the
# default), empty or <= 0 means no total at all. Kept under its old name
# because session._turn_timed_out and the offline runner read it; with no total
# it is infinity, so the clock term in those readers never fires on its own and
# the record's status ("timeout", set BEFORE the turn is cancelled) is what
# tells a limit from the person's Stop.
_TOTAL = _env_seconds("CSERIES_CHAT_TURN_TIMEOUT_S", 0)
TURN_TIMEOUT_S = _TOTAL if _TOTAL > 0 else math.inf

# There is no count of turns: the limit is not set by the platform; the
# memory is the Host's to manage. A person's turn always
# starts; a turn the platform starts for the Host's own work starts when the
# Host's memory holds one more (runtime/host_memory, start_or_attach below).

# The reattach buffer is bounded by BYTES, not frames. It once counted frames —
# one per text delta — so ONE long write (a desktop design: 128,000 output
# tokens is ~512 KB of text in ~30,000 deltas) passed the old 6,000-frame cap on
# its own and a reattach replayed a hole. Consecutive text deltas are now
# coalesced into one frame (up to COALESCE_TEXT_CHARS), and the bound is 8 MiB a
# turn: about sixteen of those writes, a bound that protects the pod's memory,
# not one any real turn meets (and part of what a turn costs,
# host_memory.TURN_COST_BYTES). Past it the turn keeps STREAMING live but stops
# buffering, and a reattach says so instead of silently replaying a hole.
MAX_BUFFER_BYTES = int(_env_seconds("CSERIES_TURN_BUFFER_BYTES", 8 * 1024 * 1024))
COALESCE_TEXT_CHARS = 32 * 1024


# In-flight stopped-turn row writes. See _leave_stopped_row for why the
# reference has to be held.
_PENDING_ROW_WRITES: set = set()


# The registry key of the turn running in THIS task (turn types step 6). Set
# by brain_service.send_message just before start_or_attach creates the turn's
# task, so the task — and every prompt it builds — knows which listed turn is
# itself, without waiting on the asynchronous session frame.
import contextvars as _contextvars
CURRENT_THREAD_KEY: "_contextvars.ContextVar[Optional[str]]" = _contextvars.ContextVar(
    "current_turn_thread_key", default=None)
# The record of the turn running in THIS task, set by _run inside the turn's
# own task. The thread's _active slot can already belong to the NEXT turn (a
# message for a turn that stopped reading starts its own), so a turn that asks
# about itself reads this, never _active[key].
CURRENT_TURN_RECORD: "_contextvars.ContextVar[Optional[TurnRecord]]" = _contextvars.ContextVar(
    "current_turn_record", default=None)


class TurnCapError(RuntimeError):
    """Raised when a turn the platform starts for the Host does not fit in the
    Host's memory (host_memory.why_not). A person's turn is never refused."""


# A platform-started turn (system=True: the Host woken for a result) on a
# thread where a turn is already running. It is never turned into a nudge into
# that turn — a person's turn must not receive a platform brief as their own
# words — so the start is refused and the caller waits that turn out
# (session.EntityRuntime._wake_host_turn).
THREAD_BUSY = "a turn is already running in this thread"


class TurnBusyError(RuntimeError):
    """A system turn found the thread busy (THREAD_BUSY)."""


# How long a message for a turn that has stopped reading waits for that turn
# to finish before it starts as a turn of its own (the same bound as
# session.ANSWER_AFTER_WAIT_S, which an answer to a card waits the same way).
# Past it the message starts anyway: it must not be lost to a turn that is
# slow to finish its last writes.
FOLLOW_ON_WAIT_S = 20.0


def _stopped_reading(rec) -> bool:
    """The running turn reads no more input (session.turn_stopped_reading):
    it released its holds at its exit. A nudge pushed into it
    now would sit in a queue nobody drains. Never raises."""
    sid = getattr(rec, "session_id", None)
    if not sid:
        return False
    try:
        from mastercode.runtime.session import turn_stopped_reading
        return turn_stopped_reading(sid)
    except Exception as e:  # noqa: BLE001
        log.debug("stopped-reading check failed (non-fatal): %s", e)
        return False


def _turn_started(session_id) -> None:
    """A turn's session frame arrived: that turn runs under `session_id` and
    reads input from here (session.turn_started). A durable
    session keeps one id across turns; the "stopped reading" mark its turn
    before left at its exit must not make this turn's words a turn of their
    own. Never raises."""
    if not session_id:
        return
    try:
        from mastercode.runtime.session import turn_started
        turn_started(session_id)
    except Exception as e:  # noqa: BLE001
        log.debug("turn-started note failed (non-fatal): %s", e)


def _note_stop(rec, reason: str) -> None:
    """Tell the runtime how this turn was ended from outside, so a result it
    held and had not read is carried on after anything but the person's Stop
    (session._deliver_unread). Never raises."""
    if not getattr(rec, "session_id", None):
        return
    try:
        from mastercode.runtime.session import note_turn_stop
        note_turn_stop(rec.session_id, reason)
    except Exception as e:  # noqa: BLE001
        log.debug("turn stop note failed (non-fatal): %s", e)


def _keys_of(value) -> list:
    """The pending_words keys in `value` (one, None, or lists of them)."""
    try:
        from mastercode.pending_words import keys_of
        return keys_of(value)
    except Exception:  # noqa: BLE001 — no runtime in this process
        return []


def _ids_of(value) -> list:
    """The sender ids in `value` (one id, None, or nested lists of them), each
    once, in order (session.row_client_id's shape)."""
    out: list = []
    for v in (value if isinstance(value, (list, tuple)) else [value]):
        for x in (v if isinstance(v, (list, tuple)) else [v]):
            if isinstance(x, str) and x and not _has_id(out, x):
                out.append(x)
    return out


def _has_id(ids, client_id) -> bool:
    """Whether `ids` holds `client_id` as the same app's (pending_words
    .same_sent; an id with no sender only as the same id with none)."""
    from mastercode.pending_words import same_sent
    s = getattr(client_id, "sender", None)
    return any(same_sent(c, client_id) if s is not None
               else (getattr(c, "sender", None) is None and c == client_id) for c in ids)


def _settle_rows(keys) -> None:
    """The words of these pending_words rows are in a turn now: the rows go
    (detached; this runs in frame handling and teardown). Never raises."""
    ks = _keys_of(keys)
    if not ks:
        return
    try:
        from server import brain_service
        from mastercode.pending_words import settle_later
        settle_later(brain_service.get_pool(), ks)
    except Exception as e:  # noqa: BLE001
        log.error("PENDING_WORDS_NOT_SETTLED n=%d: %s", len(ks), e)


def _text_delta(frame: str) -> Optional[str]:
    """The text of a `0:"…"` text-delta frame, or None for any other frame."""
    if not (isinstance(frame, str) and frame.startswith("0:") and frame.endswith("\n")):
        return None
    try:
        text = json.loads(frame[2:])
    except Exception:  # noqa: BLE001
        return None
    return text if isinstance(text, str) else None


def _text_frame(text: str) -> str:
    """One `0:` text frame carrying `text` (the wire's own encoding)."""
    return "0:" + json.dumps(text) + "\n"


def note_alive(session_id: Optional[str]) -> None:
    """A sign of life from the turn running under `session_id` that is not a
    frame — for session.mark_turn_live to call each round of the tool loop.
    Never raises; a no-op before any registry exists."""
    try:
        if _registry is not None:
            _registry.note_alive(session_id)
    except Exception as e:  # noqa: BLE001
        log.debug("note_alive failed (non-fatal): %s", e)


def turn_row_ids(client_id) -> list:
    """The senders' ids the user row of the turn running in this task keeps
    (session.py, as it writes that row; session.row_client_id shapes them):
    the send's own, and the ids of words that attached to this turn on the
    same words before now (TurnRecord.client_ids, _attach_ids). From here on,
    an id that attaches is marked once the row is written. Only the
    turn's own row takes them, the first written in its task: a send made
    later from inside the turn's work is not that row. Never raises."""
    own = list(client_id) if isinstance(client_id, (list, tuple)) else [client_id]
    try:
        rec = CURRENT_TURN_RECORD.get()
        if rec is None or rec.done.is_set() or rec.row_ids_taken:
            return own
        rec.row_ids_taken = True
        return own + [c for c in rec.client_ids if not _has_id(own, c)]
    except Exception as e:  # noqa: BLE001
        log.debug("turn row ids not read (non-fatal): %s", e)
        return own


def turn_row_task(task) -> None:
    """The task writing the user row of the turn running in this task
    (session.py, after turn_row_ids): an id that attaches after the row took
    its ids is marked once it is done (_mark_ids_after_row). Never raises."""
    try:
        rec = CURRENT_TURN_RECORD.get()
        if rec is not None and rec.row_ids_taken and rec.row_task is None:
            rec.row_task = task
    except Exception as e:  # noqa: BLE001
        log.debug("turn row task not noted (non-fatal): %s", e)


def note_alive_current() -> None:
    """A sign of life for the turn whose task this is (CURRENT_TURN_RECORD),
    from work one of its tool calls waits on and that reports itself alive —
    the harness lane's box, which sends a frame every 8 s while a run goes
    (nac_harness.HarnessRelay.dispatch). A tool call that waits on such a run
    for longer than TURN_IDLE_S produces no frame of the turn's own, so
    without this the idle window stopped the turn while the box was still
    reporting. A no-op outside a turn,
    or for a turn that has ended. Never raises."""
    try:
        rec = CURRENT_TURN_RECORD.get()
        if rec is not None and not rec.done.is_set():
            rec.last_alive = time.time()
    except Exception as e:  # noqa: BLE001
        log.debug("note_alive_current failed (non-fatal): %s", e)


def compose_thread_key(entity_id: Any, app_id: Optional[str],
                       project_id: Any = None,
                       thread_id: Optional[str] = None,
                       history_context: Optional[str] = None) -> str:
    """ONE composition, used by the send path and the lookup/cancel routes —
    if these ever diverge, reattach silently stops finding the running turn,
    so both sides call this and nothing else. Clients send only ids (the
    Circles thread, the project); none builds this string.

    The key names the CONVERSATION, not the app that sent the message: one
    conversation, one turn, whoever is typing. A second message into a conversation whose turn is
    running is then a nudge into that turn from any surface — Circles, the
    Workbench, a connector — instead of a second turn beside it.

      - a Circles thread: ``thread|{entity}|{thread_id}``. A byte compare,
        so the id is the canonical one (circles_threads.canonical_id).
        Two threads are still two lanes.
      - a project: ``project|{entity}|{project_id}``.
      - neither: ``{app}|{entity}|none``, exactly as before. Each app's own
        conversation keeps its own lane.

    `history_context` is the conversation a turn thinks in when it is not
    the sending app's own (session.transient_interaction_context). It is
    read in the order memory_engine.derive_source_context reads it, after
    the thread and the project: ``project:N`` is project N; ``chat`` and
    ``circles`` are the original Circles conversation, keyed as Circles' own
    send keys it (``circles|{entity}|none``); any other value is that app's
    conversation. Replay and A/B variants also pass it, but they call the
    runtime directly and never take this lock."""
    if thread_id:
        return f"thread|{entity_id}|{thread_id}"
    if project_id is not None:
        return f"project|{entity_id}|{project_id}"
    if history_context:
        if history_context.startswith("project:"):
            return f"project|{entity_id}|{history_context.split(':', 1)[1]}"
        conversation = "circles" if history_context in ("chat", "circles") else history_context
        return f"{conversation}|{entity_id}|none"
    return f"{app_id or 'chat'}|{entity_id}|none"


@dataclass
class TurnRecord:
    turn_id: str
    thread_key: str
    entity_id: Any
    app_id: Optional[str]
    user_message: str
    started_at: float
    frames: list[str] = field(default_factory=list)
    status: str = "running"          # running | done | error | cancelled | timeout
    session_id: Optional[str] = None
    # THIS turn's identity, off the session frame (session.py mints it per
    # send_message call). The live turn trace is keyed by it, never by
    # session_id, which spans every turn in the thread.
    turn_token: Optional[str] = None
    finished_at: Optional[float] = None
    truncated: bool = False
    # The reattach buffer's size: bytes buffered so far, and the text
    # deltas not yet sealed into one coalesced `0:` frame (_append).
    buffered_bytes: int = 0
    open_text: list[str] = field(default_factory=list)
    open_chars: int = 0
    open_bytes: int = 0
    # Liveness (T-9): when the turn last showed a sign of life, and why the
    # platform stopped it — "idle" (no sign of life for TURN_IDLE_S) or
    # "total" (the opt-in TURN_TIMEOUT_S). Set BEFORE the turn is cancelled.
    last_alive: float = 0.0
    stop_cause: Optional[str] = None
    expire_handle: Any = None
    task: Optional[asyncio.Task] = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    subscribers: set = field(default_factory=set)   # asyncio.Queue wakeups
    pending_nudges: list[str] = field(default_factory=list)
    # What each pending nudge's user row carries besides its words, in the
    # same order (None for most): a connector's forwarded message keeps its
    # connector_origin (session.push_nudge).
    pending_nudge_tags: list = field(default_factory=list)
    # The sender's id for each pending nudge, in the same order (None for
    # most; session.push_nudge client_id).
    pending_nudge_ids: list = field(default_factory=list)
    # Whether each pending nudge came through a route whose client keeps a
    # copy and sends it again itself (the page's nudge route), in the same
    # order (session.push_nudge keeps_copy). The rest are the server's to
    # deliver (_requeue_undelivered).
    pending_nudge_keeps_copy: list = field(default_factory=list)
    # The pending_words row(s) holding each pending nudge's words, in the same
    # order (None for words no row holds; mastercode/pending_words).
    pending_nudge_keys: list = field(default_factory=list)
    # The pending_words rows holding words that are THIS turn's message (words
    # an earlier turn ended without reading, sent on here; words a restart
    # left). Settled when the turn has started (its session frame, _observe),
    # so a turn that dies before it starts leaves them held.
    message_keys: list = field(default_factory=list)
    # The senders' ids (client_id) of the words this turn STARTED on: one
    # send's, or several unread nudges' sent on together. A send's silent
    # retry under one of them follows this turn, running or finished, and
    # starts no second one (started_with).
    client_ids: list = field(default_factory=list)
    # Whether this turn's user row has taken `client_ids` (turn_row_ids). Ids
    # of words that attach to the turn after that are kept on their markers
    # once the row is written (_attach_ids), never before.
    row_ids_taken: bool = False
    # The task writing this turn's user row (turn_row_task), awaited before an
    # id that attached late is marked.
    row_task: Any = None
    # The senders' ids of the nudges this turn READ (its nudge_read
    # receipts, _observe). Kept here as well as on the buffer, which stops
    # keeping frames at MAX_BUFFER_BYTES: a resend under one of them follows
    # this turn and delivers nothing again (delivered_in).
    read_ids: list = field(default_factory=list)
    # The words this turn sent on as the thread's next turn when it ended
    # without reading them (_requeue_undelivered), whole: a connector whose
    # words are among them follows the turn they went to, whether that turn
    # started for them or was already running (connectors/turns._followed_turn).
    requeued_texts: list = field(default_factory=list)
    # Builds the NEXT turn for a different text on this thread (same kwargs,
    # new message). Used when a turn ends before it ever had a session — the
    # identity hold refused it — with nudges nobody could read: they are sent
    # as the next message instead of being dropped (_requeue_undelivered).
    make_followup: Optional[Callable[[str], AsyncIterator[str]]] = None
    # What a client shows for this turn's opening line when it is NOT what a
    # user typed: a platform-started turn (the host continuing after a
    # dispatch round) registers with the real brief as `user_message` (the
    # dedup/nudge key) and a short label here, so a Workbench tab that
    # re-attaches mid-turn renders the label, never the multi-report brief.
    display_message: Optional[str] = None
    # Turn types step 6: the type this turn resolved to, off its 2:turn_type
    # frame. None until the frame arrives, and for an untyped turn; status
    # "closed" is a typed turn whose type could not be read.
    turn_type_id: Optional[str] = None
    turn_type_status: Optional[str] = None
    # Set by cancel() BEFORE it cancels the task: the person pressed Stop — the
    # one end that means "do not carry on". The runtime also tells
    # a Stop from this turn's own time limit by it (both arrive as a cancel), so
    # a Stop is never read as the limit, whatever the clock says.
    stopped_by_user: bool = False
    # Where the turn runs ({app_id, project_id, thread_id}, from the send
    # path): what a message for it starts a turn in once it has stopped
    # reading (routes/circles.nudge).
    room: Optional[dict] = None

    def describe(self) -> dict:
        now = time.time()
        shown = self.display_message if self.display_message is not None else self.user_message
        return {
            "turn_id": self.turn_id,
            "thread_key": self.thread_key,
            "status": self.status,
            "started_at": self.started_at,
            "elapsed_s": round((self.finished_at or now) - self.started_at, 1),
            "user_message": (shown or "")[:160],
            "system": self.display_message is not None,
            "session_id": self.session_id,
            "seq": len(self.frames),
            "truncated": self.truncated,
            "buffered_bytes": self.buffered_bytes,
            "last_alive_s": (round(now - self.last_alive, 1)
                             if self.last_alive and not self.finished_at else None),
            "turn_type_id": self.turn_type_id,
            "turn_type_status": self.turn_type_status,
            "finished_at": self.finished_at,
        }


class TurnRegistry:
    def __init__(self) -> None:
        self._active: dict[str, TurnRecord] = {}     # thread_key -> running turn
        self._by_id: dict[str, TurnRecord] = {}      # turn_id -> running or recent

    # ── lifecycle ───────────────────────────────────────────────────────

    def start_or_attach(
        self,
        *,
        thread_key: str,
        entity_id: Any,
        app_id: Optional[str],
        message: str,
        make_inner: Callable[[], AsyncIterator[str]],
        display_message: Optional[str] = None,
        make_followup: Optional[Callable[[str], AsyncIterator[str]]] = None,
        system: bool = False,
        room: Optional[dict] = None,
        nudge_tags: Optional[dict] = None,
        nudge_keeps_copy: bool = False,
        nudge_client_id=None,
        pending_key=None,
    ) -> tuple[TurnRecord, str]:
        """Returns (record, mode) with mode ∈ started|reattached|nudged.
        Raises TurnCapError when a platform-started (`system`) turn does not
        fit in the Host's memory. `display_message` is
        the label a client shows instead of `message` (see TurnRecord).
        `system`: a platform-started turn — never a nudge into a running
        one; raises TurnBusyError (THREAD_BUSY) instead. `room`: where the
        turn runs (TurnRecord.room). `nudge_tags`: what the message's user row
        carries besides its words when it becomes a nudge (a connector's
        connector_origin; session.push_nudge). `nudge_keeps_copy`: the
        message came through a route whose client keeps the words and sends
        them again itself (the Circles nudge route, through
        session.answer_after_turn); set by that route on the server side.
        Without it — every other sender — words that become a nudge the turn
        never reads go on as the thread's next turn, once
        (session.end_turn_input, _requeue_undelivered). `nudge_client_id`: the
        sender's id for the message, kept with the nudge as the page's own
        nudge id is (nudge, session.push_nudge): the turn's receipt and the
        nudge's user row carry it. Several messages' words sent on as one
        (_requeue_undelivered) carry the list of their ids to the row.
        `pending_key`: the pending_words row(s) holding the message's words.
        They go with the words: settled when the turn that has them as its
        message starts (started, or reattached to a turn on the same words),
        or carried with the nudge they become (nudged).

        A different message while the thread's turn runs is a nudge into it
        — unless that turn has stopped reading (it released its
        holds at its exit, and nothing drains its queue any more). Then the
        message starts as a turn of its own, the way an answer to a card does
        after the asking turn ended (session.answer_after_turn): registered
        now, so the sender follows it, and run once that turn is done."""
        rec = self._active.get(thread_key)
        after = None
        if rec is not None and not rec.done.is_set():
            if (message or "").strip() == (rec.user_message or "").strip():
                # No nudge: the words are that turn's message. Their senders'
                # ids go on that turn, so its user row carries them (or, once
                # the row has its ids, its senders' markers say so after the
                # row is written: _attach_ids). Their pending_words
                # rows go when it has started (now, if it has).
                self._attach_ids(rec, nudge_client_id)
                self._take_message_keys(rec, pending_key)
                return rec, "reattached"
            if system:
                raise TurnBusyError(THREAD_BUSY)
            if not _stopped_reading(rec):
                if nudge_keeps_copy:
                    self._nudge(rec, message, tags=nudge_tags, keeps_copy=True,
                                client_id=nudge_client_id, pending_key=pending_key)
                else:
                    self._nudge(rec, message, tags=nudge_tags, client_id=nudge_client_id,
                                pending_key=pending_key)
                return rec, "nudged"
            # Its own turn, after that one. It takes the thread's slot from
            # it (the running one finishes without it), so it adds nothing
            # to the ceiling's count.
            after = rec

        if after is None and system:
            # The Host's own work starts when its memory holds one more turn:
            # what is free (its limit less its working set) less a turn's
            # measured cost for each turn running. No count, no platform number.
            from mastercode.runtime import host_memory
            if not host_memory.one_more_fits(len(self._active)):
                raise TurnCapError(host_memory.why_not(len(self._active)))

        rec = TurnRecord(
            turn_id=uuid.uuid4().hex,
            thread_key=thread_key,
            entity_id=entity_id,
            app_id=app_id,
            user_message=message or "",
            started_at=time.time(),
            display_message=display_message,
            make_followup=make_followup,
            room=dict(room) if isinstance(room, dict) else None,
        )
        rec.message_keys = _keys_of(pending_key)
        rec.client_ids = _ids_of(nudge_client_id)
        self._active[thread_key] = rec
        self._by_id[rec.turn_id] = rec
        rec.task = asyncio.create_task(
            self._run(rec, make_inner, after=after), name=f"chat-turn-{rec.turn_id[:8]}")
        log.info("TURN_START turn=%s key=%s active=%d%s",
                 rec.turn_id[:8], thread_key, len(self._active),
                 f" (after turn {after.turn_id[:8]}, which stopped reading)" if after else "")
        return rec, "started"

    async def _run(self, rec: TurnRecord,
                   make_inner: Callable[[], AsyncIterator[str]],
                   after: Optional[TurnRecord] = None) -> None:
        """The detached turn body: drain the inner frame generator into the
        buffer until it ends, shows no sign of life for TURN_IDLE_S (or
        reaches the opt-in TURN_TIMEOUT_S), or is cancelled. The inner
        generator's own finally (brain_service._send_message_inner) cancels
        the runtime task, so closing it here tears the whole turn down —
        engine, tools, SDK subprocess. `after`: a turn on the same thread
        that stopped reading — this one starts when it is done (bounded,
        FOLLOW_ON_WAIT_S).

        Each frame is awaited in THIS task, as before (no task per frame, so
        the generator keeps one context). The liveness check is a timer
        (_expire) that re-arms itself while the turn shows life, and only on a
        real expiry records the cause and status and then cancels this task —
        so the runtime, which sees that cancel, reads "timeout", never a Stop
        (session._turn_timed_out)."""
        agen = None
        CURRENT_TURN_RECORD.set(rec)          # this task only (contextvars)
        try:
            if after is not None and not after.done.is_set():
                try:
                    await asyncio.wait_for(after.done.wait(), timeout=FOLLOW_ON_WAIT_S)
                except asyncio.TimeoutError:
                    log.warning("TURN_FOLLOW_ON turn=%s: turn %s is still finishing after "
                                "%.0fs — starting anyway", rec.turn_id[:8],
                                after.turn_id[:8], FOLLOW_ON_WAIT_S)
            agen = make_inner().__aiter__()
            rec.last_alive = time.time()
            self._arm_expiry(rec)
            while True:
                try:
                    frame = await agen.__anext__()
                except StopAsyncIteration:
                    break
                # Every frame the turn itself produces is a sign of life.
                rec.last_alive = time.time()
                self._observe(rec, frame)
                self._append(rec, frame)
            rec.status = "done"
        except asyncio.CancelledError:
            if rec.stop_cause is not None and not rec.stopped_by_user:
                # The platform's own stop (_expire), not the person's: handled
                # here and not re-raised, as the old wall-clock path was.
                _me = asyncio.current_task()
                if _me is not None:
                    _me.uncancel()
                await self._stop_for_life(rec, agen)
                return
            rec.status = "cancelled"
            _note_stop(rec, "user" if rec.stopped_by_user else "cancelled")
            self._append(rec, "3:" + json.dumps(
                "(stopped — nothing is still running in the background)") + "\n")
            await self._close_gen(agen)
            self._leave_stopped_row(rec, "cancelled")
            raise
        except Exception as e:
            rec.status = "error"
            log.exception("TURN_ERROR turn=%s", rec.turn_id[:8])
            self._append(rec, "3:" + json.dumps(f"turn crashed: {e}") + "\n")
            await self._close_gen(agen)
            await self._record_error(rec, "crash", str(e))
        finally:
            # Whatever trace this turn still holds dies with it. The cancel and
            # timeout handlers above have already POPPED it (that is the whole
            # point of those paths); this covers the two that had not — the
            # crash branch, and any future exit — so a leaked entry cannot
            # accumulate over a pod's lifetime.
            self._disarm_expiry(rec)
            self._evict_trace(rec)
            self._seal_text(rec)
            rec.finished_at = time.time()
            self._requeue_undelivered(rec)
            rec.done.set()
            self._wake(rec)
            if self._active.get(rec.thread_key) is rec:
                del self._active[rec.thread_key]
            log.info("TURN_END turn=%s status=%s frames=%d bytes=%d dur=%.1fs",
                     rec.turn_id[:8], rec.status, len(rec.frames), rec.buffered_bytes,
                     rec.finished_at - rec.started_at)
            loop = asyncio.get_running_loop()
            loop.call_later(DONE_TTL_S, self._by_id.pop, rec.turn_id, None)

    # ── liveness ────────────────────────────────────────────────────────

    @staticmethod
    def _expiry_at(rec: TurnRecord) -> float:
        """When this turn is stopped if nothing changes: the idle window after
        its last sign of life, or the opt-in total, whichever comes first.
        math.inf when neither applies."""
        idle = TURN_IDLE_S if TURN_IDLE_S and TURN_IDLE_S > 0 else math.inf
        total = TURN_TIMEOUT_S if TURN_TIMEOUT_S and TURN_TIMEOUT_S > 0 else math.inf
        return min(rec.last_alive + idle, rec.started_at + total)

    def _arm_expiry(self, rec: TurnRecord) -> None:
        """(Re)arm the liveness timer for the current expiry. Frames only move
        rec.last_alive — the timer re-arms itself when it fires early, so a
        long write's thousands of deltas cost nothing here."""
        self._disarm_expiry(rec)
        at = self._expiry_at(rec)
        if at == math.inf:
            return
        rec.expire_handle = asyncio.get_running_loop().call_later(
            max(0.0, at - time.time()), self._expire, rec)

    @staticmethod
    def _disarm_expiry(rec: TurnRecord) -> None:
        h, rec.expire_handle = rec.expire_handle, None
        if h is not None:
            h.cancel()

    def _expire(self, rec: TurnRecord) -> None:
        """The liveness timer fired. A turn that showed life since it was
        armed is re-armed; one that did not is stopped: the cause and the
        status are recorded FIRST, then its task is cancelled."""
        rec.expire_handle = None
        if rec.done.is_set() or rec.stopped_by_user or rec.task is None:
            return
        now = time.time()
        idle = TURN_IDLE_S if TURN_IDLE_S and TURN_IDLE_S > 0 else math.inf
        total = TURN_TIMEOUT_S if TURN_TIMEOUT_S and TURN_TIMEOUT_S > 0 else math.inf
        idle_at, total_at = rec.last_alive + idle, rec.started_at + total
        if now < min(idle_at, total_at):
            self._arm_expiry(rec)
            return
        rec.stop_cause = "total" if total_at <= idle_at else "idle"
        rec.status = "timeout"
        log.warning("TURN_%s turn=%s — %s; stopping it", rec.stop_cause.upper(),
                    rec.turn_id[:8],
                    (f"no sign of life for {now - rec.last_alive:.0f}s"
                     if rec.stop_cause == "idle"
                     else f"the opt-in total of {total:g}s is reached"))
        rec.task.cancel()

    @staticmethod
    def _span(seconds: float) -> str:
        s = float(seconds)
        if s >= 3600 and s % 3600 == 0:
            n = int(s // 3600)
            return f"{n} hour{'s' if n != 1 else ''}"
        if s >= 60:
            n = int(round(s / 60))
            return f"{n} minute{'s' if n != 1 else ''}"
        return f"{s:g} seconds"

    def stop_line(self, rec: TurnRecord) -> str:
        """What the wire, the error lane and the stopped-turn row say about a
        platform stop — what actually happened, never a ceiling."""
        if rec.stop_cause == "total":
            return (f"this turn reached the {self._span(TURN_TIMEOUT_S)} total set for this "
                    "Host (CSERIES_CHAT_TURN_TIMEOUT_S) and was stopped — nothing is still "
                    "running in the background")
        return (f"this turn showed no sign of life for {self._span(TURN_IDLE_S)} — no "
                "output, no tool call, no progress — and was stopped; nothing is still "
                "running in the background")

    async def _stop_for_life(self, rec: TurnRecord, agen) -> None:
        rec.status = "timeout"
        _note_stop(rec, "timeout")
        line = self.stop_line(rec)
        self._append(rec, "3:" + json.dumps(line) + "\n")
        await self._close_gen(agen)
        self._leave_stopped_row(rec, "timeout")
        await self._record_error(rec, "timeout", line)

    @staticmethod
    def _take_message_keys(rec: TurnRecord, more) -> None:
        """`more` (pending_words keys) are rows holding words that are `rec`'s
        message. Settled once the turn has started (its session frame); kept
        on the record until then."""
        rec.message_keys = [*rec.message_keys, *[k for k in _keys_of(more) if k not in rec.message_keys]]
        if rec.session_id is not None and rec.message_keys:
            _settle_rows(rec.message_keys)
            rec.message_keys = []

    def note_alive(self, session_id: Optional[str]) -> None:
        """A sign of life that is not a frame, from the turn running under
        `session_id` (session.mark_turn_live, each round of the tool loop).
        Never raises."""
        if not session_id:
            return
        now = time.time()
        for rec in list(self._active.values()):
            if rec.session_id == session_id and not rec.done.is_set():
                rec.last_alive = now

    def _requeue_undelivered(self, rec: TurnRecord) -> None:
        """A different message sent while this turn ran is a nudge: it waits
        in `pending_nudges` until the turn's session frame says where to push
        it. A turn that ends WITHOUT ever having a session — the identity hold
        refused it, or it failed first — would drop them silently, and the
        person's message with them. They are sent as the next message instead
        (one turn, the texts joined), and the old stream says so. A stopped
        turn keeps the stop: nothing restarts what the person stopped.

        A turn that HAD its session pushed its nudges there; the words it
        ended without reading that are the server's to deliver were taken
        off that queue at its end and held for this (session.end_turn_input):
        they go on the same way, once — taken here, so nothing else sends
        them. Only the person's Stop keeps them back (the
        platform's own stop, a crash or an error leaves them to go on).

        Words that came through a route whose client keeps a copy (the page's
        nudge route: pending_nudge_keeps_copy) are never sent on, with or
        without a session: no receipt came, so that client sends them itself,
        and sending them here too put them in front of the Host twice.

        The pending_words rows holding the words go with them:
        settled when the next turn has started, so words whose next turn
        dies with the process are still held and a restart delivers them;
        carried with the nudge when that turn is already waiting; settled at
        once when the words are written into the thread as undelivered.

        The words keep their senders' ids (session.push_nudge client_id; the
        Workbench Index sends its request id): the next turn's user row
        carries them, one as itself and several joined as a list
        (session.row_client_id), and so does the nudge they become when that
        turn is already waiting, so a retry of the send finds its words
        delivered (thread_pick.find_index_send). When that waiting turn is
        on the very same words, they attach to it ("reattached": identical
        words are answered before the nudge path): their ids go on that turn,
        and its user row carries them (_attach_ids)."""
        keeps = rec.pending_nudge_keeps_copy
        # (text, tags, the sender's id, the pending_words key(s)) of each the
        # server delivers.
        unread = [(t, rec.pending_nudge_tags[i] if i < len(rec.pending_nudge_tags) else None,
                   rec.pending_nudge_ids[i] if i < len(rec.pending_nudge_ids) else None,
                   rec.pending_nudge_keys[i] if i < len(rec.pending_nudge_keys) else None)
                  for i, t in enumerate(rec.pending_nudges)
                  if t and not (i < len(keeps) and keeps[i])]
        kept = sum(1 for i, t in enumerate(rec.pending_nudges) if t and i < len(keeps) and keeps[i])
        rec.pending_nudges.clear()
        rec.pending_nudge_tags.clear()
        rec.pending_nudge_ids.clear()
        rec.pending_nudge_keeps_copy.clear()
        rec.pending_nudge_keys.clear()
        if kept:
            log.info("TURN_NUDGE_LEFT_TO_CLIENT turn=%s n=%d — the turn ended before it had a "
                     "session; their client keeps a copy and sends them itself",
                     rec.turn_id[:8], kept)
        held_back = rec.status in ("cancelled", "timeout")
        if rec.session_id is not None:
            try:
                from mastercode.runtime.session import take_unread_for_next_turn
                unread = [(tuple(u) + (None,))[:4] for u in take_unread_for_next_turn(rec.session_id)
                          if u[0]]
            except Exception as e:  # noqa: BLE001 — no runtime in this process
                log.debug("unread-nudge take failed (non-fatal): %s", e)
                unread = []
            held_back = rec.stopped_by_user
        queued = [(t, tg) for t, tg, _cid, _k in unread]
        texts = [t for t, _tg in queued]
        if not texts:
            return
        pkeys = _keys_of([k for _t, _tg, _cid, k in unread])
        if rec.make_followup is None or held_back:
            log.warning("TURN_NUDGE_DROPPED turn=%s status=%s n=%d — %s",
                        rec.turn_id[:8], rec.status, len(texts),
                        ("the turn ended without reading them and nothing can send them on"
                         if rec.session_id is not None
                         else "the turn ended before it had a session and nothing can "
                              "send them on"))
            # Only the person's Stop means "do not carry on": it still does
            # not carry the words on, but it says so.
            self._say_undelivered(
                rec, queued,
                "the turn was stopped before the Host read {it}" if rec.stopped_by_user
                else "the turn ended before the Host read {it}, and nothing could send {it} on")
            # The words are in the thread now, whole, to send again: no turn
            # takes them on its own, so nothing holds them any more.
            _settle_rows(pkeys)
            return
        combined = "\n\n".join(texts)
        # A connector's forwarded message keeps its connector_origin on the
        # row the next turn writes for these words (its frame, as when such a
        # message starts a turn): one origin as itself, several as a list in
        # the order of the joined texts.
        origins = [(tg or {}).get("connector_origin") for _t, tg in queued]
        frame = None
        if any(origins):
            frame = {"connector_origin": origins[0] if len(origins) == 1 else origins}
        # A carried message (Ask Ivy) keeps its carry the same way.
        carries = [(tg or {}).get("carry") for _t, tg in queued]
        if any(carries):
            frame = {**(frame or {}), "carry": carries[0] if len(carries) == 1 else carries}
        # Their senders' ids, kept on the next turn's user row: one as itself,
        # several as a list in the order of the joined texts.
        client_id = None
        if any(c for _t, _tg, c, _k in unread):
            try:
                from mastercode.runtime.session import row_client_id
                client_id = row_client_id([c for _t, _tg, c, _k in unread])
            except Exception as e:  # noqa: BLE001 — no runtime in this process
                log.debug("unread-nudge ids not read (non-fatal): %s", e)
        if self._active.get(rec.thread_key) is rec:
            del self._active[rec.thread_key]
        maker = rec.make_followup
        # The thread may already have its next turn waiting (a message sent
        # after this one stopped reading): the words then become a nudge into
        # it, and keep their origin and their ids. They stay the server's to
        # deliver (no client holds them), so that turn sends them on in turn
        # if it never reads them.
        extra: dict = {}
        make_kw: dict = {}
        if frame is not None:
            extra["nudge_tags"] = frame
            make_kw["frame"] = frame
        if client_id is not None:
            extra["nudge_client_id"] = client_id
            make_kw["client_id"] = client_id
        if pkeys:
            extra["pending_key"] = pkeys
        rec.requeued_texts = list(texts)
        try:
            new, mode = self.start_or_attach(
                thread_key=rec.thread_key, entity_id=rec.entity_id, app_id=rec.app_id,
                message=combined,
                make_inner=lambda: maker(combined, **make_kw),
                make_followup=maker, room=rec.room, **extra)
        except Exception as e:  # noqa: BLE001 — TurnCapError included
            log.error("TURN_NUDGE_DROPPED turn=%s n=%d — the next turn could not "
                      "start: %s", rec.turn_id[:8], len(texts), e)
            rec.requeued_texts = []
            self._say_undelivered(
                rec, queued, "the turn ended before the Host read {it}, and the next turn "
                             "could not start (" + str(e) + ")")
            _settle_rows(pkeys)
            return
        # "reattached": the waiting turn is on these very words and delivers
        # them. Their ids went onto that turn (start_or_attach, _attach_ids),
        # so its user row carries them; nothing is marked before that row is
        # written.
        log.info("TURN_NUDGE_REQUEUED turn=%s -> %s n=%d", rec.turn_id[:8],
                 new.turn_id[:8], len(texts))
        # The senders' ids ride the frame: a page that drew a line for its
        # words knows them by it, and follows the turn they went on to.
        _ids = [x for _t, _tg, c, _k in unread
                for x in (c if isinstance(c, (list, tuple)) else [c]) if isinstance(x, str) and x]
        self._append(rec, "2:" + json.dumps([{
            "type": "nudge_requeued", "text": combined[:500],
            "turn_id": new.turn_id, **({"client_ids": _ids} if _ids else {})}]) + "\n")

    def _attach_ids(self, rec: TurnRecord, client_id) -> None:
        """Words sent under `client_id` (one id, or a list) attached to `rec`,
        which is on the very same words ("reattached"): their ids go on the
        turn (client_ids), so a resend under one follows it (started_with)
        and its user row carries them (turn_row_ids), as a row of their own
        would. Nothing is marked before that row exists: a restart before it
        is written loses the turn, and a retry of the send must then run the
        words. Ids that attach after the row took its ids are marked
        on their senders' markers once the row is written
        (thread_pick.mark_index_ids_reattached)."""
        new = [c for c in _ids_of(client_id) if not _has_id(rec.client_ids, c)]
        if not new:
            return
        rec.client_ids.extend(new)
        if not rec.row_ids_taken:
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._mark_ids_after_row(rec, new), name=f"reattached-ids-{rec.turn_id[:8]}")
            _PENDING_ROW_WRITES.add(task)
            task.add_done_callback(_PENDING_ROW_WRITES.discard)
        except Exception as e:  # noqa: BLE001
            log.error("TURN_NUDGE_IDS_NOT_KEPT turn=%s — not dispatched: %s", rec.turn_id[:8], e)

    @staticmethod
    async def _mark_ids_after_row(rec: TurnRecord, client_id) -> None:
        """The markers of `client_id` say their words went in `rec`, once
        `rec`'s user row is written (thread_pick.mark_index_ids_reattached).
        A row write that failed marks nothing. Never raises."""
        n = len(client_id) if isinstance(client_id, list) else 1
        try:
            task = rec.row_task
            if task is not None:
                await asyncio.shield(task)
            from server import brain_service
            from server.connectors import thread_pick
            await thread_pick.mark_index_ids_reattached(brain_service.get_pool(), int(rec.entity_id), client_id)
        except Exception as e:  # noqa: BLE001
            # The count, not the ids: in PII mode rows keep only a digest of an id.
            log.error("TURN_NUDGE_IDS_NOT_KEPT — the markers of %d request id(s) were not marked: %s", n, e)

    def _say_undelivered(self, rec: TurnRecord, queued: list, why: str) -> None:
        """Words the server owns and cannot deliver are never dropped with
        only a log line: the person's Stop, a turn
        with nothing to send them on, a next turn that could not start.

        The ended turn's stream gets a line, so a page or chat still following
        the turn shows it at once. A connector whose reply follows the turn
        sends that stream's error lines to its chat
        (connectors/turns._segment_parts), so its words get a line in plain
        words for that chat. The Host thread gets a row with the words
        themselves, whole, so they can be sent again from any surface.
        `queued`: [(text, tags)]; `why`: what happened, with {it} for the
        words. Never raises: this runs in the turn's teardown."""
        try:
            texts = [t for t, _tg in queued if t]
            if not texts:
                return
            try:
                from server.connectors.turns import sent_words
            except Exception:  # noqa: BLE001 — no connectors in this process
                def sent_words(_t):
                    return None

            def from_chat(t, tg) -> bool:
                return bool((tg or {}).get("connector_origin")) or sent_words(t) is not None
            chat = [t for t, tg in queued if t and from_chat(t, tg)]
            here = [t for t, tg in queued if t and not from_chat(t, tg)]
            # The words as the person wrote them: a connector's without the
            # guidance the platform adds for the Host.
            texts = [sent_words(t) or t for t in texts]
            if chat:
                one = len(chat) == 1
                self._append(rec, "3:" + json.dumps(
                    ("Your message was" if one else f"Your {len(chat)} messages were")
                    + " not delivered to the Host: "
                    + why.replace("{it}", "it" if one else "them")
                    + ". Send " + ("it" if one else "them") + " again.") + "\n")
            for t in here:
                self._append(rec, "3:" + json.dumps(
                    f"Not delivered to the Host: “{t[:500]}” — " + why.replace("{it}", "it")
                    + ". Send it again.") + "\n")
            it = "it" if len(texts) == 1 else "them"
            line = (f"(Not delivered to the Host: {why.replace('{it}', it)}. Send {it} again.)"
                    + "".join(f"\n\n“{t}”" for t in texts))
            room = rec.room if isinstance(rec.room, dict) else {}
            pid = room.get("project_id")
            origin = {
                "project_id": pid, "thread_id": room.get("thread_id"),
                "interaction_context": (room.get("history_context")
                                        or (f"project:{pid}" if pid is not None
                                            else room.get("app_id") or "circles")),
                "session_id": rec.session_id}
            task = asyncio.get_running_loop().create_task(
                self._write_undelivered_row(rec, origin, line, len(texts)),
                name=f"undelivered-row-{rec.turn_id[:8]}")
            _PENDING_ROW_WRITES.add(task)
            task.add_done_callback(_PENDING_ROW_WRITES.discard)
        except Exception as e:  # noqa: BLE001
            log.error("TURN_NUDGE_DROPPED turn=%s — the undelivered line could not be "
                      "written: %s", rec.turn_id[:8], e)

    @staticmethod
    async def _write_undelivered_row(rec: TurnRecord, origin: dict, line: str, n: int) -> None:
        """The detached write of _say_undelivered's row in the Host thread
        (offline_task_runner.post_to_origin_thread, the one writer of a
        platform line into a thread). Never raises."""
        try:
            from server import brain_service
            from mastercode.brain.offline_task_runner import post_to_origin_thread
            ok = await post_to_origin_thread(
                brain_service.get_pool(), rec.entity_id, origin, line,
                extra_tags={"origin": "undelivered", "undelivered": n,
                            "turn_id": rec.turn_id})
            if not ok:
                log.error("TURN_NUDGE_DROPPED turn=%s — the undelivered row was not written",
                          rec.turn_id[:8])
        except Exception as e:  # noqa: BLE001
            log.error("TURN_NUDGE_DROPPED turn=%s — the undelivered row failed: %s",
                      rec.turn_id[:8], e)

    @staticmethod
    async def _close_gen(agen) -> None:
        if agen is None:            # a follow-on turn stopped before it began
            return
        with contextlib.suppress(BaseException):
            await agen.aclose()

    # ── stopped turns leave a trace ─────────────────────────────────────

    @staticmethod
    def describe_stopped_turn(reason: str, tool_calls: list,
                              waiting_on: Optional[str] = None,
                              after_reply: bool = False,
                              cause: Optional[str] = None) -> str:
        """The one-line body of a stopped turn's assistant row. `cause`, for a
        platform stop (reason "timeout"): "idle" — no sign of life for the idle
        window — or "total", the opt-in turn time limit.

        Reads like what it is — the host saying what it got through before
        it was stopped — because the NEXT turn reads it back as history, not
        as a log line:

            (stopped by the user after 2 tool calls: workspace_file write →
             ok; set_desktop apply → refused: not a known wallpaper)
        """
        calls = [c for c in (tool_calls or []) if isinstance(c, dict)]
        opener = ("stopped by the user" if reason == "cancelled"
                  else "stopped after showing no sign of life for "
                       + TurnRegistry._span(TURN_IDLE_S) if cause == "idle"
                  else "stopped at the turn time limit")
        if waiting_on:
            # The turn had written its reply and was holding for what it
            # started (session.note_turn_held): the reply above is kept, and
            # this note says what it was waiting on.
            opener += f" while it waited on {waiting_on}"
            if not calls:
                return f"({opener})"
        if not calls:
            # A turn that wrote its reply before a hold and then went on: the
            # calls before are on that reply row (after_reply).
            return (f"({opener} after the reply above)" if after_reply
                    else f"({opener} before any tool call completed)")
        parts = []
        for c in calls:
            name = str(c.get("tool") or "tool")
            op = str(c.get("op") or "").strip()
            head = f"{name} {op}".strip()
            status = str(c.get("status") or "ok")
            if status == "ok":
                parts.append(f"{head} → ok")
            else:
                why = str(c.get("error") or "").strip()
                parts.append(f"{head} → {status}" + (f": {why[:120]}" if why else ""))
        n = len(calls)
        return (f"({opener} after {n} tool call{'s' if n != 1 else ''}: "
                + "; ".join(parts) + ")")

    def _leave_stopped_row(self, rec: TurnRecord, reason: str) -> None:
        """Write a short assistant turn row for a turn that never reached
        its own bottom.

        WHY (measured on a live Host): a cancelled turn left NO
        trace. This method marks the record cancelled, and session.py's
        assistant-row write sits below the tool loop that was just torn
        down — so the next turn's history (memory_engine.recall_recent_turns)
        has no assistant row for it. Tool calls that COMPLETED inside the
        cancelled turn were invisible to the next turn: an artifact was
        created, a set_desktop was refused, and the host had no idea either
        had happened.

        NO TRACE MEANS NO ROW. The trace is the only evidence
        that this turn did anything worth recording, and its absence is
        meaningful in both directions:

          - no tool call completed before the stop, so the row would read
            "stopped before any tool call completed" and carry project_id
            None — filed to the wrong thread for a project turn, and saying
            nothing the user did not already watch happen; or
          - the turn had ALREADY written its real assistant row and cleared
            (session.py clears at the write), and a stop pressed during the
            post-turn tasks would then contradict a complete reply sitting
            directly above it in the same thread.

        Both were measured. take_turn_trace returning None covers both,
        because the same clear that ends the first case ends the second.

        A turn that SENT text has a trace from its first visible text
        (session.note_turn_reply): what the person read is stored as the
        Host's reply (session.keep_sent_reply), and the note follows it.

        Fire-and-forget on purpose. We are inside a cancellation handler;
        write_turn_row awaits an embed before it inserts, and blocking a
        teardown on a remote embed lane is how a stop button stops
        responding. Same posture as session.py's own turn-row dispatch.
        """
        if not rec.session_id or not rec.turn_token:
            # No session frame was ever observed, so the turn died before the
            # runtime named itself. There is no thread to write into.
            return
        try:
            from mastercode.runtime.session import take_turn_trace
            trace = take_turn_trace(rec.turn_token)
        except Exception as e:  # noqa: BLE001
            log.debug("stopped-turn trace lookup failed (non-fatal): %s", e)
            trace = None
        if not trace:
            log.info("TURN_STOPPED turn=%s reason=%s — no trace (nothing "
                     "completed, or the reply already landed); no row written",
                     rec.turn_id[:8], reason)
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._write_stopped_row(rec, reason, trace),
                name=f"stopped-turn-row-{rec.turn_id[:8]}")
            # Hold a strong reference until it finishes. The event loop keeps
            # only a weak one, so a bare create_task whose result nobody awaits
            # can be garbage-collected mid-await — and this one is dispatched
            # from a task that is being torn down, which is exactly when the
            # last local reference disappears.
            _PENDING_ROW_WRITES.add(task)
            task.add_done_callback(_PENDING_ROW_WRITES.discard)
        except Exception as e:  # noqa: BLE001
            log.debug("stopped-turn row dispatch failed (non-fatal): %s", e)

    @staticmethod
    def _evict_trace(rec: TurnRecord) -> None:
        """Drop any live trace this turn still holds. Runs in _run's finally,
        so the paths that do NOT pop one — the crash branch above all — cannot
        leave an entry behind. Never raises."""
        if not rec.turn_token:
            return
        try:
            from mastercode.runtime.session import take_turn_trace
            take_turn_trace(rec.turn_token)
        except Exception as e:  # noqa: BLE001
            log.debug("turn trace evict failed (non-fatal): %s", e)

    @classmethod
    async def _write_stopped_row(cls, rec: TurnRecord, reason: str,
                                 trace: dict) -> None:
        """The detached write. Never raises — a missing note must never be
        louder than the turn it describes."""
        try:
            from server import brain_service
            from mastercode.memory_engine import write_turn_row
            from mastercode.runtime.session import keep_sent_reply, sent_reply
            # What the person already read is stored first, as what the Host
            # said: the trace carries every piece the turn sent
            # (session.note_turn_reply). A turn that held has its row, which
            # is rewritten in place only if more went out after it.
            _kept = keep_sent_reply(
                brain_service.get_pool(), rec.entity_id, rec.session_id,
                rec.turn_token, trace, ended=reason)
            if _kept is not None:
                await _kept
            _has_reply = bool(sent_reply(trace).strip())
            tool_calls = trace.get("tool_calls") or []
            # A turn that held had already written its reply with the calls
            # before it (session.note_turn_held): only later ones are listed.
            waiting_on = trace.get("held")
            # The reply row written before a hold carries the calls before it:
            # only the later ones are listed (whether it was still waiting).
            tool_calls = tool_calls[int(trace.get("calls_kept") or 0):]
            tags = dict(trace.get("tags") or {})
            if rec.app_id and not tags.get("app_id"):
                tags["app_id"] = rec.app_id
            tags["tool_calls"] = tool_calls
            # The UI renders this as a system note rather than something the
            # host said. `origin` is the key it already switches on, so a
            # timeout is tagged "cancelled" too and carries its real reason
            # alongside — a new origin value would render as a normal reply
            # in every client that has not shipped a case for it.
            tags["origin"] = "cancelled"
            tags["stopped_reason"] = reason
            if rec.stop_cause:
                tags["stopped_cause"] = rec.stop_cause
            tags["turn_id"] = rec.turn_id
            await write_turn_row(
                brain_service.get_pool(), rec.entity_id, rec.session_id,
                "assistant", cls.describe_stopped_turn(
                    reason, tool_calls, waiting_on,
                    after_reply=(int(trace.get("calls_kept") or 0) > 0 or "calls_kept" in trace
                                 or _has_reply),
                    cause=rec.stop_cause),
                project_id=trace.get("project_id"), tags=tags,
                created_at=datetime.now(timezone.utc),
            )
            log.info("TURN_STOPPED_ROW turn=%s reason=%s calls=%d",
                     rec.turn_id[:8], reason, len(tool_calls))
        except Exception as e:  # noqa: BLE001
            log.warning("stopped-turn row write failed (non-fatal): %s", e)

    @staticmethod
    async def _record_error(rec: TurnRecord, kind: str, message: str) -> None:
        """Land a turn-level failure in the error lane (never raises).
        Cancels are user actions, not errors — callers skip them."""
        try:
            from server import brain_service
            from server.error_log import record as _record_err
            await _record_err(
                brain_service.get_pool(), source="turn", kind=kind,
                message=message, turn_id=rec.turn_id,
                thread_key=rec.thread_key, session_id=rec.session_id,
                user_message=(rec.user_message or "")[:200])
        except Exception as e:
            log.debug("turn error-lane record failed (non-fatal): %s", e)

    # ── frame plumbing ──────────────────────────────────────────────────

    def _append(self, rec: TurnRecord, frame: str) -> None:
        """Buffer one frame for reattach and hand it to every live tail.

        Live tails get the frame exactly as produced. The BUFFER coalesces
        consecutive text deltas (`0:"…"`) into one frame of up to
        COALESCE_TEXT_CHARS — the same text, a few frames instead of one per
        delta — and is bounded by MAX_BUFFER_BYTES."""
        text = _text_delta(frame)
        if not rec.truncated:
            if text is not None:
                if rec.buffered_bytes + len(frame) > MAX_BUFFER_BYTES:
                    self._overflow(rec)
                else:
                    rec.open_text.append(text)
                    rec.open_chars += len(text)
                    rec.open_bytes += len(frame)
                    rec.buffered_bytes += len(frame)
                    if rec.open_chars >= COALESCE_TEXT_CHARS:
                        self._seal_text(rec)
            else:
                self._seal_text(rec)
                if rec.buffered_bytes + len(frame) > MAX_BUFFER_BYTES:
                    self._overflow(rec)
                else:
                    rec.frames.append(frame)
                    rec.buffered_bytes += len(frame)
        self._wake(rec, frame)

    @staticmethod
    def _seal_text(rec: TurnRecord) -> None:
        """The text deltas not yet sealed become ONE `0:` frame on the buffer.
        Its size replaces the per-delta sizes counted while they were open."""
        if not rec.open_text:
            return
        sealed = _text_frame("".join(rec.open_text))
        rec.open_text = []
        rec.open_chars = 0
        rec.frames.append(sealed)
        # What the open deltas were counted at (frame overhead included) is
        # replaced by the sealed frame's real size.
        rec.buffered_bytes = max(0, rec.buffered_bytes - rec.open_bytes) + len(sealed)
        rec.open_bytes = 0

    def _overflow(self, rec: TurnRecord) -> None:
        """The byte bound is reached: what is buffered is kept (open text
        sealed), nothing more is buffered, and a reattach says so."""
        self._seal_text(rec)
        rec.truncated = True
        log.warning("TURN_BUFFER_FULL turn=%s bytes=%d (bound %d) — streaming "
                    "continues, replay keeps what was buffered", rec.turn_id[:8],
                    rec.buffered_bytes, MAX_BUFFER_BYTES)

    def _wake(self, rec: TurnRecord, live_frame: Optional[str] = None) -> None:
        """Every live tail gets `live_frame` (a frame to send), or True (look
        at the record: it ended)."""
        for q in list(rec.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(live_frame if live_frame is not None else True)

    def _observe(self, rec: TurnRecord, frame: str) -> None:
        """Capture the transient session_id off the 2:{"type":"session"} data
        frame so nudges (mid-stream influence) can target the live tool loop."""
        if not frame.startswith("2:"):
            return
        # Turn types step 6: the resolved type, whenever it arrives.
        if '"turn_type"' in frame:
            try:
                for item in json.loads(frame[2:]) or []:
                    if isinstance(item, dict) and item.get("type") == "turn_type":
                        rec.turn_type_id = item.get("turn_type_id") or None
                        rec.turn_type_status = item.get("turn_type_status") or None
            except Exception:
                pass
            return
        if '"nudge_read"' in frame:
            # The turn READ words sent under this id (its receipt): kept on the
            # record, which a full buffer does not stop (read_ids).
            try:
                for item in json.loads(frame[2:]) or []:
                    if not isinstance(item, dict) or item.get("type") != "nudge_read":
                        continue
                    cid = item.get("client_id")
                    app = item.get("client_app")
                    if isinstance(cid, str) and cid and isinstance(app, str) and app:
                        # With the app that sent the words: an id is read
                        # only as that app's (pending_words.SentId).
                        from mastercode.pending_words import SentId
                        cid = SentId(cid, app)
                        if not _has_id(rec.read_ids, cid):
                            rec.read_ids.append(cid)
            except Exception:
                pass
            return
        if rec.session_id is not None:
            return
        try:
            items = json.loads(frame[2:])
        except Exception:
            return
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("type") == "session":
                rec.session_id = item.get("session_id")
                rec.turn_token = item.get("turn_token")
                _turn_started(rec.session_id)
                # The turn has started: the words that are its message are
                # its own now, and the rows holding them go.
                self._take_message_keys(rec, None)
                for i, text in enumerate(rec.pending_nudges):
                    tags = rec.pending_nudge_tags[i] if i < len(rec.pending_nudge_tags) else None
                    cid = rec.pending_nudge_ids[i] if i < len(rec.pending_nudge_ids) else None
                    keeps = (rec.pending_nudge_keeps_copy[i]
                             if i < len(rec.pending_nudge_keeps_copy) else False)
                    pkey = rec.pending_nudge_keys[i] if i < len(rec.pending_nudge_keys) else None
                    pkw = {"pending_key": pkey} if pkey else {}
                    if keeps:
                        self._push_nudge(rec.session_id, text, tags, client_id=cid, keeps_copy=True, **pkw)
                    elif cid:
                        self._push_nudge(rec.session_id, text, tags, client_id=cid, **pkw)
                    elif tags:
                        self._push_nudge(rec.session_id, text, tags, **pkw)
                    else:
                        self._push_nudge(rec.session_id, text, **pkw)
                rec.pending_nudges.clear()
                rec.pending_nudge_tags.clear()
                rec.pending_nudge_ids.clear()
                rec.pending_nudge_keeps_copy.clear()
                rec.pending_nudge_keys.clear()
                return

    def nudgeable(self, turn_id: str, entity_id: Any = None) -> Optional[TurnRecord]:
        """The running turn `turn_id` when words sent into it now would be
        read (nudge's checks), else None: unknown, ended, another entity's,
        or stopped reading. Never raises."""
        rec = self._by_id.get(turn_id)
        if rec is None or rec.done.is_set():
            return None
        if entity_id is not None and str(rec.entity_id) != str(entity_id):
            return None
        if _stopped_reading(rec):
            return None
        return rec

    def would_nudge(self, thread_key: str, message: str, *, system: bool = False) -> Optional[TurnRecord]:
        """The running turn a message on `thread_key` would become a nudge
        into right now (start_or_attach's "nudged"), else None: no turn
        running there, the same words as its message (a reattach), a
        platform-started send (refused), or a turn that has stopped reading
        (the message runs as a turn of its own after it)."""
        rec = self._active.get(thread_key)
        if rec is None or rec.done.is_set() or system:
            return None
        if (message or "").strip() == (rec.user_message or "").strip():
            return None
        return None if _stopped_reading(rec) else rec

    def nudge(self, turn_id: str, text: str, entity_id: Any = None,
              client_id: Optional[str] = None, pending_key=None) -> bool:
        """The person's words into THIS running turn (the composer's Send while
        the turn holds, POST /api/brain/turns/{id}/nudge): the same nudge a
        different message on the thread is, so every attached client draws it.
        False when the turn is unknown, ended, or another entity's — or has
        stopped reading (it released its holds at its exit, and
        words pushed into it now would never be read). The client then sends
        them as the next message, which starts its own turn
        (start_or_attach). The words become a user row in the thread when
        the turn reads them (session.py's drain). `client_id`: the sender's
        id for these words, echoed on the nudge frame, the turn's nudge_read
        receipt and the saved row.

        `pending_key`: the pending_words row(s) the route wrote the words to
        before calling this (routes/brain.brain_turn_nudge). Then the words
        are the server's: read by the turn, or sent on as the thread's next
        turn when it ends without reading them, after a restart too, and the
        page keeps no copy. Without one (PII mode writes none) the page holds
        its copy until the turn's receipt and sends it again itself when none
        comes (use-workbench-chat nudgeTurn), so the server never sends those
        words on (keeps_copy)."""
        rec = self.nudgeable(turn_id, entity_id)
        if rec is None:
            return False
        if pending_key:
            self._nudge(rec, text, client_id=client_id, pending_key=pending_key)
        else:
            self._nudge(rec, text, client_id=client_id, keeps_copy=True)
        return True

    def _nudge(self, rec: TurnRecord, text: str, tags: Optional[dict] = None,
               client_id: Optional[str] = None, keeps_copy: bool = False,
               pending_key=None) -> None:
        """A different message while the turn runs = mid-stream influence,
        not a second turn. Surfaced on the buffer so EVERY attached client
        sees the running turn was redirected. `tags`: what its user row
        carries besides the words (session.push_nudge). `keeps_copy`: they
        came through a route whose client keeps a copy (session.push_nudge).
        `pending_key`: the pending_words row(s) holding them, which go with
        the words (session.push_nudge)."""
        text = (text or "").strip()
        if not text:
            return
        pkw = {"pending_key": pending_key} if pending_key else {}
        if rec.session_id and keeps_copy:
            self._push_nudge(rec.session_id, text, tags, client_id=client_id, keeps_copy=True, **pkw)
        elif rec.session_id and client_id:
            self._push_nudge(rec.session_id, text, tags, client_id=client_id, **pkw)
        elif rec.session_id and tags:
            self._push_nudge(rec.session_id, text, tags, **pkw)
        elif rec.session_id:
            self._push_nudge(rec.session_id, text, **pkw)
        else:
            rec.pending_nudges.append(text)
            rec.pending_nudge_tags.append(tags or None)
            rec.pending_nudge_ids.append(client_id or None)
            rec.pending_nudge_keeps_copy.append(bool(keeps_copy))
            rec.pending_nudge_keys.append(pending_key or None)
        frame = {"type": "nudge", "text": text[:500]}
        # One sender's id; the list of several messages joined into this one
        # nudge (_requeue_undelivered) stays with the nudge and its row, and
        # its frame says no id, as before.
        if isinstance(client_id, str) and client_id:
            frame["client_id"] = client_id
        self._append(rec, "2:" + json.dumps([frame]) + "\n")

    @staticmethod
    def _push_nudge(session_id: str, text: str, tags: Optional[dict] = None,
                    client_id: Optional[str] = None, keeps_copy: bool = False,
                    pending_key=None) -> None:
        try:
            from mastercode.runtime.session import push_nudge
            kw: dict = {}
            if tags:
                kw["tags"] = tags
            if client_id:
                kw["client_id"] = client_id
            if keeps_copy:
                kw["keeps_copy"] = True
            if pending_key:
                kw["pending_key"] = pending_key
            push_nudge(session_id, text, **kw)
        except Exception as e:
            log.warning("turn nudge delivery failed (non-fatal): %s", e)

    # ── consumption ─────────────────────────────────────────────────────

    async def tail(self, rec: TurnRecord, *, mode: str = "attached",
                   from_seq: int = 0, mark_replayed: bool = False) -> AsyncIterator[str]:
        """Replay buffered frames from from_seq, then follow live until the
        turn ends. Many tails per turn are fine; killing a tail never touches
        the turn. First frame is turn metadata (not buffered) so every client
        learns the turn_id + mode it attached under.

        The replay is a SNAPSHOT taken in the same step as the subscription
        (nothing awaits between them): the sealed frames, then the text not
        yet sealed as one frame. Everything produced after that arrives on
        this tail's queue as the frame itself, so nothing is sent twice or
        skipped, and the coalescing on the buffer never changes a frame a
        tail has already sent.

        `mark_replayed` (the reattach route's): a `2:[{"type":"replayed"}]`
        frame (not buffered) between the snapshot and the first live frame.
        A replay arrives as fast as the wire carries it, so without the mark
        a client cannot tell words the turn wrote a minute ago from words
        arriving now, and says the Host is writing when it is not."""
        q: asyncio.Queue = asyncio.Queue()
        rec.subscribers.add(q)
        try:
            snapshot = list(rec.frames[min(max(0, from_seq), len(rec.frames)):])
            if rec.open_text:
                snapshot.append(_text_frame("".join(rec.open_text)))
            truncated = rec.truncated
            ended = rec.done.is_set()
            yield "2:" + json.dumps([{
                "type": "turn", "mode": mode, **rec.describe(),
            }]) + "\n"
            for frame in snapshot:
                yield frame
            if truncated:
                # The hole is HERE: what streamed after the buffer filled and
                # before this tail attached was not kept.
                yield "2:" + json.dumps([{
                    "type": "turn_note",
                    "note": (f"this turn's replay buffer is full "
                             f"({MAX_BUFFER_BYTES // (1024 * 1024)} MiB) — what streamed "
                             "after that point and before you attached is not shown; "
                             "live output follows"),
                }]) + "\n"
            if mark_replayed:
                yield "2:" + json.dumps([{"type": "replayed"}]) + "\n"
            if ended:
                return
            while True:
                item = await q.get()
                if isinstance(item, str):
                    yield item
                    continue
                if rec.done.is_set() and q.empty():
                    return
        finally:
            rec.subscribers.discard(q)

    # ── operator surface ────────────────────────────────────────────────

    def get(self, turn_id: str) -> Optional[TurnRecord]:
        return self._by_id.get(turn_id)

    def active_for(self, thread_key: str) -> Optional[TurnRecord]:
        rec = self._active.get(thread_key)
        return rec if rec is not None and not rec.done.is_set() else None

    def by_session(self, session_id: str) -> Optional[TurnRecord]:
        """The latest turn (running, or finished within DONE_TTL_S) that ran
        under `session_id`, or None."""
        if not session_id:
            return None
        hits = [r for r in self._by_id.values() if r.session_id == session_id]
        return max(hits, key=lambda r: r.started_at) if hits else None

    def started_with(self, entity_id: Any, client_id: str) -> Optional[TurnRecord]:
        """The latest turn (running, or finished within DONE_TTL_S) of this
        Host that STARTED on words sent under `client_id`, or None. A send's
        silent retry (the Workbench's, use-workbench-chat attemptTurn) carries
        its first attempt's id: when that attempt started a turn, the retry
        follows it and starts no second one, even once it has finished
        (brain_service.send_message). Only the
        same app's id (pending_words.SentId): app A's id never names a turn
        for app B's send."""
        if getattr(client_id, "sender", None) is None or not client_id:
            return None
        hits = [r for r in self._by_id.values()
                if _has_id(r.client_ids, client_id) and str(r.entity_id) == str(entity_id)]
        return max(hits, key=lambda r: r.started_at) if hits else None

    def delivered_in(self, entity_id: Any, client_id: str) -> Optional[TurnRecord]:
        """The turn (running, or finished within DONE_TTL_S) of this Host that
        has the words sent under `client_id`: the one that started on them
        (started_with), else the latest that READ them as a nudge (read_ids,
        its nudge_read receipt). A send under the id follows it and delivers
        nothing again (brain_service.send_message): a page that sends its
        kept copy twice (a reload before the answer came, a duplicated tab),
        or sends it after the turn read it but the replay no longer shows the
        receipt (a buffer past MAX_BUFFER_BYTES)."""
        first = self.started_with(entity_id, client_id)
        if first is not None or getattr(client_id, "sender", None) is None or not client_id:
            return first
        hits = [r for r in self._by_id.values()
                if _has_id(r.read_ids, client_id) and str(r.entity_id) == str(entity_id)]
        return max(hits, key=lambda r: r.started_at) if hits else None

    def list_turns(self, entity_id: Any = None) -> list[dict]:
        out = []
        for rec in self._by_id.values():
            if entity_id is not None and rec.entity_id != entity_id:
                continue
            out.append(rec.describe())
        out.sort(key=lambda d: d["started_at"], reverse=True)
        return out

    async def cancel(self, turn_id: str) -> dict:
        rec = self._by_id.get(turn_id)
        if rec is None:
            return {"ok": False, "error": "unknown turn (finished >15min ago?)"}
        if rec.done.is_set():
            return {"ok": True, "status": rec.status, "note": "already finished"}
        rec.stopped_by_user = True
        rec.task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(rec.done.wait()), timeout=10)
        return {"ok": True, "status": rec.status}


_registry: Optional[TurnRegistry] = None


def get_turn_registry() -> TurnRegistry:
    global _registry
    if _registry is None:
        _registry = TurnRegistry()
    return _registry
