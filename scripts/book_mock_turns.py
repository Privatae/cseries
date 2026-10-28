"""The turn registry in miniature, for the browser harnesses that drive a
book over a mock kernel: the Workbench's (scripts/test_workbench_book.py) and
Circles' cards (scripts/test_circles_cards.py). Moved here from the first so
both import one copy.

What it keeps, as server/turn_registry.py does:
  - every turn's frames in a buffer that the sender tails and a reattach
    replays from any seq;
  - a nudge goes into the running turn and the turn reads it at its next
    step: a `nudge_read` receipt echoing the sender's id, and the words saved
    as a user row with metadata.nudge (session.push_nudge, the drain); a
    secret shape is cut from the words first (the mock's one shape, ZZSECRET
    and eight digits, stands for one the server's screen cuts);
  - a turn that has stopped reading answers a nudge "no" (the routes' 409);
  - the reply's row lands REPLY_WRITE_S after the stream's last frame
    (runtime/session.py writes it in a task of its own once the turn has
    returned), with its turn's token, its spend record and its price.

A harness supplies the parts that are its own through a TurnHost: its
settings (how long a turn runs, how late its row lands, whether nudges are
read, a slow turn frame), the conversation key a turn reports
(server/turn_registry.py compose_thread_key), the session id its session
frame carries, how a row is stored, what landing does (a page's totals), and
whether a turn is still the live one (a scenario may reset the mock under
it).
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone

# When the reply's row lands after its stream ends (see the docstring).
REPLY_WRITE_S = 1.5
# What one of the mock's turns spends (its spend record, summed over its
# calls) and its list price, as the context frame and the reply's row carry
# them (session.py: context_ledger.spend, est_cost_usd).
REPLY_TOKENS = {"uncached": 240, "cache_read": 900, "cache_write": 60, "output": 40}
REPLY_USD = 0.0062
REPLY_TAIL = "I will look at it now."


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime | None) -> str | None:
    # The server's own spelling: datetime.isoformat(), six fraction digits.
    return t.isoformat() if t else None


class TurnHost:
    """What a harness supplies to its turns."""

    #: The Host model the context frame and the reply's row name.
    model = "claude-opus-5-5"

    def settings(self) -> dict:
        """turn_secs, reply_write_s, read_nudges, turn_frame_delay."""
        raise NotImplementedError

    def thread_key(self, turn: "MockTurn") -> str:
        raise NotImplementedError

    def session_id(self, turn: "MockTurn") -> str:
        raise NotImplementedError

    def write_row(self, turn: "MockTurn", role: str, content: str, created_at: str, metadata: dict) -> None:
        raise NotImplementedError

    def landed(self, turn: "MockTurn", at: datetime) -> None:
        """The reply's row has landed (after write_row)."""

    def live(self, turn: "MockTurn") -> bool:
        """Still the turn the mock keeps (a reset drops it)."""
        return True

    def move(self, turn: "MockTurn", to: str) -> None:
        """The Host's pick filed the exchange in thread `to` (a `move`
        step): the host moves the turn's rows so far there."""
        raise NotImplementedError

    def requeue(self, turn: "MockTurn", words: list) -> "MockTurn | None":
        """The turn ends without reading `words` [(text, at, cid)]: a host
        whose server owns them starts the thread's next turn with them and
        returns it (turn_registry._requeue_undelivered); None leaves them to
        the client, as the Workbench page's route still does."""
        return None


class MockTurn:
    def __init__(self, host: TurnHost, page, message: str):
        self.host = host
        self.id = "turn-" + uuid.uuid4().hex[:8]
        self.token = uuid.uuid4().hex
        self.page, self.message = page, message
        self.frames: list[str] = []
        self.done = False
        self.reading = True
        self.pending: list[tuple[str, datetime, str | None]] = []
        self.started = now()
        self.wake = asyncio.Event()
        self.user_row_meta: dict = {}

    # ── the buffer ──
    def emit(self, frame: str) -> None:
        self.frames.append(frame)
        self.wake.set()

    def describe(self) -> dict:
        return {"turn_id": self.id, "status": "done" if self.done else "running",
                "started_at": self.started.timestamp(), "elapsed_s": (now() - self.started).total_seconds(),
                "user_message": self.message[:160], "seq": len(self.frames),
                "session_id": self.host.session_id(self), "thread_key": self.host.thread_key(self)}

    async def tail(self, from_seq: int = 0, mark_replayed: bool = False):
        """The buffer from `from_seq`, then live. `mark_replayed` (the
        reattach route's, server/turn_registry.py tail): a `replayed` frame
        after what was buffered when the tail began."""
        i = from_seq
        if mark_replayed:
            end = len(self.frames)
            while i < end:
                yield self.frames[i]
                i += 1
            yield "2:" + json.dumps([{"type": "replayed"}]) + "\n"
        while True:
            while i < len(self.frames):
                yield self.frames[i]
                i += 1
            if self.done:
                return
            self.wake.clear()
            if i < len(self.frames):
                continue
            try:
                await asyncio.wait_for(self.wake.wait(), 1.0)
            except asyncio.TimeoutError:
                pass

    # ── nudges ──
    def nudge(self, text: str, cid: str | None, *, refused: bool = False) -> bool:
        """Words into the running turn. False when it has ended or stopped
        reading (the route's 409), or `refused` (a scenario's 409)."""
        if self.done or not self.reading or refused:
            return False
        self.pending.append((text, now(), cid))
        return True

    def _read_nudges(self) -> None:
        for text, at, cid in self.pending:
            read = re.sub(r"ZZSECRET[0-9]{8}", "[REDACTED:test-shape]", text)
            self.emit("2:" + json.dumps([{"type": "nudge_read", "text": read,
                                          **({"client_id": cid} if cid else {})}]) + "\n")
            self.host.write_row(self, "user", read, iso(at),
                                {"turn_token": self.token, "nudge": True, **({"client_id": cid} if cid else {})})
        self.pending = []

    # ── the turn ──
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        cfg = self.host.settings()
        self.host.write_row(self, "user", self.message, iso(self.started),
                            {"turn_token": self.token, **self.user_row_meta})
        self.emit("2:" + json.dumps([{"type": "session", "session_id": self.host.session_id(self),
                                      "turn_token": self.token}]) + "\n")
        if cfg.get("turn_frame_delay"):
            await asyncio.sleep(cfg["turn_frame_delay"])
        self.emit("2:" + json.dumps([{"type": "turn", "turn_id": self.id}]) + "\n")
        if cfg.get("crisis_frame"):
            # The platform's crisis check flagged the words (session.py
            # _crisis_flagged): one content-free frame before the reply; the
            # reply is the Host's own and streams as any other.
            self.emit("2:" + json.dumps([{"type": "governance", "kind": "crisis"}]) + "\n")
        self.emit("0:" + json.dumps("Noted. ") + "\n")
        # A scripted turn (a harness's `steps` setting): a tool call, its
        # result, a silent gap, more words, in that order, as a real turn's
        # stream carries them. Without one the turn just runs turn_secs.
        for step in cfg.get("steps") or ():
            if self.done or not self.host.live(self):
                return
            kind = step[0]
            if kind == "tool":
                self.emit("9:" + json.dumps({"toolCallId": step[1], "toolName": step[2], "args": step[3]}) + "\n")
            elif kind == "result":
                self.emit("a:" + json.dumps({"toolCallId": step[1], "result": step[2]}) + "\n")
            elif kind == "text":
                self.emit("0:" + json.dumps(step[1]) + "\n")
            elif kind == "move":
                # The Host's pick_thread filed the exchange in thread step[1]
                # (titled step[2]): its rows go there, and the stream says so
                # as brain_service.py's thread_moved frame does.
                frm = self.page
                self.host.move(self, step[1])
                self.page = step[1]
                self.emit("2:" + json.dumps([{
                    "type": "thread_moved", "from_project_id": None, "to_project_id": None,
                    "to_project_name": None, "from": {"kind": "thread", "ref": frm},
                    "to": {"kind": "thread", "ref": step[1]}, "to_title": step[2]}]) + "\n")
            elif kind == "wait":
                until = loop.time() + step[1]
                while loop.time() < until and not self.done:
                    await asyncio.sleep(min(0.25, max(0.0, until - loop.time())))
        end = loop.time() + (0 if cfg.get("steps") else self.host.settings()["turn_secs"])
        while loop.time() < end and not self.done:
            await asyncio.sleep(0.25)
            if not self.host.live(self):  # a scenario reset the mock under it
                return
            if self.pending and self.host.settings()["read_nudges"]:
                self._read_nudges()
        if self.done:  # stopped
            return
        if self.pending and self.host.settings()["read_nudges"]:
            self._read_nudges()
        self.reading = False
        if self.pending:
            # Words the turn ended without reading: the server's go on as the
            # thread's next turn, and the stream says so with their ids.
            words, self.pending = self.pending, []
            nxt = self.host.requeue(self, words)
            if nxt is not None:
                ids = [cid for _t, _at, cid in words if cid]
                self.emit("2:" + json.dumps([{"type": "nudge_requeued", "text": nxt.message[:500],
                                              "turn_id": nxt.id, **({"client_ids": ids} if ids else {})}]) + "\n")
        self.emit("0:" + json.dumps(REPLY_TAIL) + "\n")
        self.emit("2:" + json.dumps([{
            "t": "context", "tokens_in": 1200, "window": 1_000_000, "budget": None, "model": self.host.model,
            "provider": "anthropic", "est_cost_usd": REPLY_USD, "price_basis": "list",
            "ledger": {"calls": 2, "spend": {"calls": 2, "tokens": REPLY_TOKENS}}}]) + "\n")
        usage = {"promptTokens": 1200, "completionTokens": 40, "cachedTokens": 900, "cacheWriteTokens": 60}
        self.emit("d:" + json.dumps({"finishReason": "stop", "usage": usage}) + "\n")
        self.emit("e:" + json.dumps({"finishReason": "stop", "usage": usage, "isContinued": False}) + "\n")
        self.done = True
        loop.call_later(self.host.settings()["reply_write_s"], self.land)

    def land(self) -> None:
        """The reply's row is written: it lands, with its turn's token, its
        spend record and its price."""
        if not self.host.live(self):  # from a scenario before the last reset
            return
        t = now()
        self.host.write_row(self, "assistant", "Noted. " + REPLY_TAIL, iso(self.started), {
            "model": self.host.model, "provider": "anthropic", "gen_ms": 1400, "turn_token": self.token,
            "tokens_in": 1200, "tokens_out": 40, "tokens_total": 1240, "tokens_cached": 900,
            "est_cost_usd": REPLY_USD, "price_basis": "list",
            "context_ledger": {"calls": 2, "spend": {"calls": 2, "tokens": REPLY_TOKENS}}})
        self.host.landed(self, t)

    def cancel(self) -> None:
        """The Stop: the turn ends now, and every tail is told."""
        if not self.done:
            self.done = True
            self.emit("3:" + json.dumps("(stopped — nothing is still running in the background)") + "\n")
