"""The turn's reply: every piece of visible text a turn sends, recorded once
where it is sent, and what the go-ahead guard reads of it, piece by piece.

EntityRuntime._send_message_core (runtime/session.py) makes one TurnReplies per
turn and keeps its own names for the recorder's pieces and helpers, so the turn
reads them as before.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from mastercode.runtime.turn_input import send_order
from mastercode.runtime.turn_trace import REPLY_SEP, WM_MARKER, join_pieces, note_turn_reply, strip_wm

log = logging.getLogger("cseries.mastercode.entity_runtime")


# Streaming: per-call stream callbacks push visible text
# deltas to the SSE layer as the provider produces them (Anthropic via
# the inference.stream bridge op). brain_service maps a "text_delta"
# event to a 0: frame and suppresses the final buffered 0: once any delta
# has streamed. No progress_cb (background turn) → nothing streams and
# the reply is returned; a provider that ignores stream_cb has its text
# sent when its completion settles (_settle), so it reaches the person
# round by round like a streamed one.
#
# WM-regurgitation guard: the model occasionally parrots an injected
# "## Working Memory" block. Streaming would surface it before the
# strip runs. So each callback holds only the LEADING bytes, and if the
# response opens with the WM marker nothing of it streams; what
# strip_wm leaves is sent when the completion settles. Each model call
# gets its own piece and callback (_next_stream_cb), so the empty-retry
# round can still stream. A response shorter than the marker that never
# gets decided is sent when it settles — never lost.
_WM_MARKER = WM_MARKER


# Blocks held out of the stream as they arrive (_emit), so they are
# never sent, never stored and never read by the guard:
#   <skill_events> — a block no prompt asks for (the
#     XML and its apply step went with the simulated psyche). A model
#     that writes one anyway never streams it. One never closed is
#     text, as the parser has always left it.
#   <think> / <thinking> — reasoning a model writes in-band in its
#     text, only where it OPENS the completion — nothing
#     but blank space before it — as
#     provider_gateway.strip_thinking reads an opening tag: dropped,
#     closed or not. The same tags after the first visible text are
#     the Host writing ABOUT them ("wrap the reasoning in <think>
#     tags"), and are sent as written. (Reasoning with no opening tag,
#     only a closing one, is the adapter's to hold back:
#     provider_gateway._consume_openrouter_stream, a route flagged
#     reasoning_inline.)
_HELD_BLOCKS = (("<skill_events>", "</skill_events>", True),
                ("<think>", "</think>", False),
                ("<thinking>", "</thinking>", False))

_SE_OPEN = re.compile(re.escape(_HELD_BLOCKS[0][0]), re.I)

_THINK_LEAD = re.compile(r"\s*(<think>|<thinking>)", re.I)

_HELD_CLOSE = [re.compile(re.escape(c), re.I) for _o, c, _k in _HELD_BLOCKS]

_HELD_TAIL = max(len(o) for o, _c, _k in _HELD_BLOCKS) - 1


class TurnReplies:
    """One turn's recorder and the guard's reading of it (see the module)."""

    def __init__(self, *, progress_cb, sess, turn_token, result, ct_reply_result,
                 row_project_id, turn_tags, asked_in_box, tools):
        """`sess`, `progress_cb` and `turn_token` are the turn's own. The
        rest are read when the recorder needs them, from the turn as it
        stands then: `result` its latest completion, `ct_reply_result`
        close_turn's owed reply, `row_project_id` and `turn_tags` what its
        row is filed under, `asked_in_box` whether it asked in the question
        box, `tools` the tools it offers."""
        self.progress_cb = progress_cb
        self.sess = sess
        self.turn_token = turn_token
        self.result = result
        self.ct_reply_result = ct_reply_result
        self.row_project_id = row_project_id
        self.turn_tags = turn_tags
        self.asked_in_box = asked_in_box
        self.tools = tools
        # ── The single recorder ──
        # Every piece of visible text the turn sends — each completion's text,
        # the dispatch narration, the platform's own lines (the stall notice)
        # — is recorded exactly once, in _send below, where it is
        # sent. The stored reply is those pieces joined with
        # REPLY_SEP, which the stream also emits before a piece that follows
        # text already sent, so the stored row IS the stream by construction;
        # nothing else adds parts or rebuilds the reply from fragments. Text
        # never sent (a parroted Working Memory block, a block _emit holds
        # back) is never recorded.
        # Each piece is labelled once, when its completion settles (_settle),
        # for the ledger (context_ledger.reply):
        #   narration — prose in a tool round that goes on to another model
        #               round: the person read it;
        #   reply     — closing text: a reply, close_turn's accompanying prose,
        #               a dispatch round's prose and its narration;
        #   repair    — a guard's repair (the go-ahead guard, the claim guard).
        # What the go-ahead guard reads, and what its record (final_*) counts,
        # is decided per piece (_unjudged, below), not by a "closing reply"
        # whose start is a moving guess.
        # A callback holds bytes only while they could still open the WM
        # marker; a completion that opens with it is not sent as it streams,
        # and what the strip leaves of it is sent when it settles.
        self._pieces: list[dict] = []
        self._cur_piece: list = [None]
        # When the reply began to reach the person: the assistant row is stamped
        # with it, not with when the turn ended, so a row written while the
        # turn held (a proof, another task's report) sorts after the reply the
        # person read before it.
        self._reply_at: list = [None]
        self._skill_raw: list[str] = []

        # ── What the go-ahead guard reads, piece by piece ──
        # Where a "closing reply" starts is a guess with holes, so there is
        # no closing reply. ONE boundary,
        # _spoke_at, moves ONLY when the turn reads the PERSON's words — an
        # answer to the box, or text they typed into the turn (the drain,
        # _person_spoke): what they read before speaking, their words answer —
        # only the pieces SENT before their words (words typed while an ask
        # was still to come do not answer it).
        # A tool or agent RESULT never moves it — a result is not the person's
        # answer. Every visible piece is judged at most once (piece["judged"]):
        #   None       not judged yet;
        #   "clean"    the guard read it, and it asks nothing;
        #   "fired"    the guard read it and fired (piece["firing"]);
        #   "repair"   a go-ahead repair's own answer: the firing it answers
        #              (piece["firing"]) decides it — never repaired again;
        #   "platform" the platform's own line (the stall notice): not the
        #              Host's words, never judged.
        # At every exit the guard reads every piece since the person last
        # spoke that it has not judged (_unjudged), and judges them all at
        # once. A guard that cannot read at an exit — its repair spent, input
        # queued — leaves them unjudged: the next exit reads them, and the
        # record (_go_ahead_record) counts any ask in them meanwhile.
        # Narration — prose in a tool round that went on to another model
        # round — is not what the turn ends on and is never judged ("Should I
        # check the logs first? Checking now." beside the call),
        # unless it is the last thing the Host sent: then the turn ends on it.
        self._spoke_at: list = [0]
        # Where the pieces answering the latest input the turn read begin
        # (the person's message, then each drain of their words or a result):
        # an index into _pieces, moved by the drain before its re-call.
        self._input_at: list = [0]

        # Every firing and how it resolved (the detector is a
        # high-recall prefilter and the Host makes the call), kept on the turn
        # row as metadata.go_ahead.fired — {rule, sentence, where, outcome:
        # work_started (task_ids / ops) | escalated (ticket_id) | not_an_ask |
        # rewrote}. Defined here, with the record, because a model error or a
        # Stop on the turn's first call writes the record too.
        self._ga_firings: list[dict] = []


    def _piece(self, label: str | None = None, hint: str | None = None, *,
               sent: bool = False) -> dict:
        """A new piece: a model call's (`sent` False — its callback fills
        it), or text the turn sends whole (`sent` True). `judged` and
        `firing`: the go-ahead guard's verdict on it (_unjudged). `order`:
        when its latest text went out, in the one order of sent text and
        the person's words (send_order — _person_spoke reads it).
        `hold`: none of its text goes out until it settles (a go-ahead
        repair's — _settle checks it against `cards` first); `capture`:
        while set, what _send would send is collected there instead."""
        return {"text": "", "held": "", "got": sent, "decided": sent, "suppress": False,
                "settled": sent, "label": label, "hint": hint, "force": None,
                "se_hold": "", "se_in": None, "se_raw": "", "se_lead": True,
                "judged": None, "firing": None, "order": None,
                "hold": False, "cards": None, "cards_before": None, "capture": None}


    def _mark_sent(self, text: str) -> None:
        """The first visible text: the row is stamped with when the reply
        began, and from here the turn's trace carries what it sends
        (note_turn_reply), so a Stop or a model error before the turn
        writes its row stores what the person saw."""
        if self._reply_at[0] is None and text.strip():
            self._reply_at[0] = datetime.now(timezone.utc)
            note_turn_reply(self.turn_token, self._pieces, self._reply_at[0],
                            project_id=self.row_project_id(), tags=self.turn_tags(),
                            record=self._ga_record_now)


    async def _send(self, piece: dict, text: str) -> None:
        """Send `text` as part of `piece`, and record it: the one place
        visible text leaves the turn."""
        if not text:
            return
        if piece.get("capture") is not None:
            piece["capture"].append(text)
            return
        out = (REPLY_SEP + text) if (not piece["text"] and (piece.pop("after_discard", False) or (
            not piece.get("glue") and any(p["text"] for p in self._pieces if p is not piece)))) else text
        piece["text"] += text
        piece["order"] = send_order()
        self._mark_sent(text)
        if self.progress_cb is not None:
            try:
                await self.progress_cb({"event": "text_delta", "text": out})
            except Exception:
                pass


    async def _emit(self, piece: dict, text: str) -> None:
        """Send a completion's text, holding back the blocks above.
        piece["se_lead"]: nothing visible has gone out of this completion
        yet — where a reasoning block may still open."""
        s_ = piece["se_hold"] + (text or "")
        piece["se_hold"] = ""
        out = ""
        while s_:
            if piece["se_in"] is not None:
                # the closing tag may arrive split across deltas: search
                # the whole block held so far
                blk = piece["se_raw"] + s_
                m = _HELD_CLOSE[piece["se_in"]].search(blk, len(_HELD_BLOCKS[piece["se_in"]][0]))
                if m is None:
                    piece["se_raw"] = blk
                    break
                if _HELD_BLOCKS[piece["se_in"]][2]:
                    self._skill_raw.append(blk[:m.end()])
                piece["se_raw"], piece["se_in"] = "", None
                s_ = blk[m.end():]
                continue
            m = _THINK_LEAD.match(s_) if piece["se_lead"] else None
            if m is not None:
                out += s_[:m.start(1)]              # blank space only
                piece["se_in"] = 1 if m.group(1).lower() == _HELD_BLOCKS[1][0] else 2
                piece["se_raw"] = m.group(1)
                s_ = s_[m.end():]
                continue
            m = _SE_OPEN.search(s_)
            if m is not None:
                out += s_[:m.start()]
                if s_[:m.start()].strip():
                    piece["se_lead"] = False
                piece["se_in"] = 0
                piece["se_raw"] = m.group(0)
                s_ = s_[m.end():]
                continue
            # hold the end of the text while it could still open a block:
            # skill_events anywhere, a reasoning block only at the lead
            _lead = piece["se_lead"]
            k = next((n for n in range(min(_HELD_TAIL, len(s_)), 0, -1)
                      if _HELD_BLOCKS[0][0].startswith(s_[-n:].lower())
                      or (_lead and not s_[:-n].strip()
                          and any(o.startswith(s_[-n:].lower()) for o, _c, _k in _HELD_BLOCKS[1:]))), 0)
            out += s_[:len(s_) - k]
            piece["se_hold"] = s_[len(s_) - k:]
            break
        if out.strip():
            piece["se_lead"] = False
        await self._send(piece, out)


    def _cb_for(self, piece: dict):
        async def _cb(_delta: str):
            if not _delta:
                return
            piece["got"] = True
            if piece["suppress"]:
                return
            if piece["hold"]:
                piece["held"] += _delta     # _settle reads it whole
                return
            if piece["decided"]:
                await self._emit(piece, _delta)
                return
            piece["held"] += _delta
            lead = piece["held"].lstrip()
            # Hold the bytes only while they could still open the WM
            # marker (and the first line has not ended).
            if "\n" not in piece["held"] and (
                    not lead or (len(lead) < len(_WM_MARKER) and _WM_MARKER.startswith(lead))):
                return
            piece["decided"] = True
            if lead.startswith(_WM_MARKER):
                piece["suppress"] = True    # _settle sends what the strip leaves
                return
            buf, piece["held"] = piece["held"], ""
            await self._emit(piece, buf)
        # The provider withdrew what this call streamed (a safety filter
        # that stopped the reply part-way, provider_gateway._call_anthropic).
        _cb.discard_streamed = lambda: self._discard(piece)
        return _cb


    def _discard(self, piece: dict) -> None:
        """Drop what `piece` has received so far: it is not the reply, so it
        is not stored and the go-ahead guard never reads it; what comes next
        on the same callback (the route's fallback answering the same
        request) starts the piece afresh. The chat stream only appends, so
        text the person already saw stays in front of them until the
        conversation is reloaded from the stored reply; the next text sent
        starts a new paragraph."""
        shown = bool(piece["text"])
        piece.update(text="", held="", got=False, decided=False, suppress=False,
                     se_hold="", se_in=None, se_raw="", se_lead=True)
        if shown:
            piece["after_discard"] = True


    def _repair_checked(self, piece: dict, seen: str) -> str:
        """What a go-ahead repair's text may say, now that its round's
        calls have run (a repair that ran
        no tool told the person "I've put it in the question box"). A
        claim of the box stands only with a card open in front of the
        person — the one that round's `escalate` filed, or one already
        open — and then names it; otherwise the text is not sent
        (go_ahead_guard.repair_text). The firing records what was
        dropped, and its answer counts only the text that went out."""
        from mastercode.runtime import go_ahead_guard as _gag_rp
        _cards = piece.get("cards") or []
        _filed = [c for c in _cards if c not in (piece.get("cards_before") or [])]
        # A card the round filed shows right beside the text; one open
        # from before is named, so the pointer is to the card meant.
        _out, _claims = _gag_rp.repair_text(
            seen, (_filed or _cards or [None])[-1], name_it=not _filed)
        _f = piece.get("firing")
        if isinstance(_f, dict):
            _f["_kept_prose"] = bool(_out.strip())
            if _claims:
                _f["claims_dropped"] = [str(c)[:300] for c in _claims[:3]]
            _ans = _f.get("_answer")
            if isinstance(_ans, dict) and "text" in _ans:
                _ans["text"] = _f["_kept_prose"]
        if _claims:
            log.warning("go-ahead guard: the repair claimed the question box with no card "
                        "open — its text was not sent (session %s): %r",
                        self.sess.session_id, _claims[0][:160])
        return _out


    async def _settle(self, res: dict | None = None, *, more: bool = False) -> None:
        """The latest completion (`res`, else the turn's `result`) has
        ended: send what its callback still holds — a short text, the whole
        text from a provider that did not stream, or what the strip leaves
        of a parroted WM block — and label it, once. `more`: another model
        round follows, so prose beside tool calls is narration; when the
        turn ends on that completion, its prose is a reply."""
        piece = self._cur_piece[0]
        if piece is None or piece["settled"]:
            return
        res = res if isinstance(res, dict) else (self.result() if isinstance(self.result(), dict) else {})
        text = res.get("content") or ""
        _hold = piece["hold"]
        if _hold:
            # A go-ahead repair's text, held whole until now — its round's
            # calls have run (or were set aside): read through the same
            # block filter into `capture`, and checked before any of it
            # goes out (_repair_checked).
            piece["capture"] = []
            buf, piece["held"] = (piece["held"] if piece["got"] else text), ""
            await self._emit(piece, strip_wm(buf))
        elif piece["suppress"] or (not piece["got"] and text.lstrip().startswith(_WM_MARKER)):
            await self._emit(piece, strip_wm(text))
        elif piece["held"]:
            buf, piece["held"] = piece["held"], ""
            await self._emit(piece, buf)
        elif not piece["got"]:
            await self._emit(piece, text)
        # What the filter still holds was not a block after all: the start
        # of one that never came, or a skill_events block never closed
        # (text, as the parser has always left it). An unclosed reasoning
        # block — one that opened the completion — is dropped.
        _open_blk = _HELD_BLOCKS[piece["se_in"]] if piece["se_in"] is not None else None
        _rest = piece["se_hold"] + (piece["se_raw"] if _open_blk and _open_blk[2] else "")
        piece["se_hold"], piece["se_raw"], piece["se_in"] = "", "", None
        await self._send(piece, _rest)
        if _hold:
            _seen, piece["capture"] = "".join(piece["capture"] or []), None
            await self._send(piece, self._repair_checked(piece, _seen))
        if piece["force"]:
            piece["label"] = piece["force"]
        elif more and res.get("tool_calls") and res is not self.ct_reply_result():
            piece["label"] = "narration"
        else:
            piece["label"] = piece["hint"] or "reply"
        piece["settled"] = True


    async def _next_stream_cb(self, hint: str | None = None, *, hold: bool = False):
        """The stream callback for the next model call: the completion
        before it settles first, and this one gets its own piece.
        `hold`: its text goes out only when it settles (_settle)."""
        await self._settle(more=True)
        piece = self._piece(hint=hint)
        piece["hold"] = bool(hold)
        self._pieces.append(piece)
        self._cur_piece[0] = piece
        return self._cb_for(piece)


    async def _send_narration(self, text: str) -> None:
        """The dispatch narration: a reply piece of its own, sent as the
        narration frame (brain_service appends it after REPLY_SEP when text
        already went out, else sends it alone)."""
        if not text:
            return
        piece = self._piece("reply", sent=True)
        piece["text"] = text
        piece["order"] = send_order()
        self._pieces.append(piece)
        self._mark_sent(text)
        if self.progress_cb is not None:
            try:
                await self.progress_cb({"event": "narration", "text": text, "content": text})
            except Exception as _cb_err:
                log.debug("progress_cb narration raised (non-fatal): %s", _cb_err)


    async def _send_piece(self, text: str, label: str, notice: str | None = None) -> None:
        """Platform text the turn sends as a piece of its own (the stall
        notice): through the recorder like everything
        else. Not the Host's words, so the go-ahead guard never judges it.
        `notice` ("budget" / "stall") marks a platform NOTICE: the person
        reads it in the row, and the next turn's model never gets it as
        the Host's own words (model_content on the row)."""
        piece = self._piece(label, sent=True)
        piece["judged"] = "platform"
        if notice:
            piece["platform_notice"] = notice
        self._pieces.append(piece)
        await self._send(piece, text)


    def _reply_all(self) -> str:
        """The stored reply: every piece sent, in order."""
        return join_pieces(self._pieces)


    def _reply_written(self) -> bool:
        """The Host's reply to the latest input is already on screen: a
        settled piece of its own words since that input, not narration.
        Asking for "your reply" again then only adds a second closing
        ("Done — … nothing left
        pending." then "Everything's settled on my end … Nothing left
        pending."), so close_turn's result and the closed turn's
        re-asks read this first."""
        return any(p["settled"] and p["text"].strip() and p["judged"] != "platform"
                   and p["label"] != "narration" for p in self._pieces[self._input_at[0]:])


    def _person_spoke(self, words_order: int) -> None:
        """The turn read the person's words, sent at `words_order` (the
        latest of them, send_order): every piece sent before them is
        answered by them — and only those. A piece sent
        after them, or with text still to send (not settled: the caller
        settles first), they had not read when they wrote: the boundary
        stops at it, and the guard reads it at the next exit. A firing is
        answered when every piece it fired on is behind the boundary."""
        i = self._spoke_at[0]
        while i < len(self._pieces):
            p = self._pieces[i]
            if not p["settled"] or (p["text"] and not (
                    p["order"] is not None and p["order"] < words_order)):
                break
            i += 1
        self._spoke_at[0] = i
        _read = {id(p) for p in self._pieces[:i]}
        for _f in self._ga_firings:
            _on = [p for p in self._pieces if p["firing"] is _f and p["judged"] == "fired"]
            if _on and all(id(p) in _read for p in _on):
                _f["_answered"] = True


    def _ending_piece(self) -> dict | None:
        """The last piece of the Host's that carries text."""
        return next((p for p in reversed(self._pieces)
                     if p["text"].strip() and p["judged"] != "platform"), None)


    def _judgeable(self, p: dict, ending: dict | None) -> bool:
        # a piece not yet settled (a Stop mid-stream) is not narration
        return p["judged"] != "platform" and (p["label"] != "narration" or p is ending)


    def _unjudged(self) -> list[dict]:
        """The pieces the guard reads at an exit: every one since the
        person last spoke that it has not judged."""
        ending = self._ending_piece()
        return [p for p in self._pieces[self._spoke_at[0]:] if p["judged"] is None and self._judgeable(p, ending)]


    def _go_ahead_resolved(self, f: dict, outcome: str) -> bool:
        """A firing's repair resolved its ask: work started, the question
        went in the box, a judged reading the Host found was no ask, or a
        card already held the question and the rewrite (the repair's own
        text) stopped asking it."""
        if outcome in ("escalated", "work_started", "not_an_ask"):
            return True
        if not (f.get("open_card") and outcome == "rewrote"):
            return False
        from mastercode.runtime import go_ahead_guard as _gag_res
        _rw = join_pieces([p for p in self._pieces
                           if p["firing"] is f and p["judged"] == "repair"])
        return _gag_res.find_go_ahead_hit(_rw) is None


    def _go_ahead_record(self, stored: str) -> dict:
        """The guard's record for the turn row (metadata.go_ahead), for
        the success measure computed from rows
        (docs/host-orchestrator-turn.md), and every firing so far with how
        it resolved. Three readings, by the detector:
          stored_asks / stored_level — the STORED reply exactly as the
            person sees it (every piece), never cleared: what the
            end-to-end test's gate reads on the same text, and what the whole-reply
            baseline (18 of 97) counted;
          final_asks / final_level — "is an ask since the person last
            spoke still unresolved?" (each ask judged on its own). It counts, from the pieces after the person's last
            words: an ask in any piece the guard has not judged (or a
            repair's own text — the guard never reads that); every firing
            whose repair resolved nothing (_go_ahead_resolved); and the
            ending, when the guard fired on it and it still asks (a
            judged reading the Host let stand as "not an ask"). Narration
            is never counted unless the turn ends on it. The surest of
            them is recorded (core, then explicit, then judged; the latest
            of a level). The person's words answer everything before them;
            a result answers nothing;
          ending_asks / ending_level — the last piece of the Host's that
            carries text.
        Written on the final row, on the held row before a hold, and on
        the row a Stop or a model error leaves (keep_sent_reply), so every
        Host row carries the record of exactly what it stores."""
        from mastercode.runtime import close_turn as _ct_r
        from mastercode.runtime import go_ahead_guard as _gag_rec
        rec: dict = {"stored_asks": None, "stored_level": None,
                     "final_asks": None, "final_level": None,
                     "ending_asks": None, "ending_level": None,
                     "asked_in_box": bool(self.asked_in_box())}
        _ending = self._ending_piece()
        try:
            _stored_hit = _gag_rec.find_go_ahead_hit(stored)
            rec["stored_asks"] = _stored_hit[0] if _stored_hit else None
            rec["stored_level"] = _stored_hit[2] if _stored_hit else None
            _ending_hit = _gag_rec.find_go_ahead_hit(_ending["text"] if _ending else "")
            rec["ending_asks"] = _ending_hit[0] if _ending_hit else None
            rec["ending_level"] = _ending_hit[2] if _ending_hit else None
        except Exception as _gaf_err:  # noqa: BLE001
            log.debug("go-ahead record read failed (non-fatal): %s", _gaf_err)
            _ending_hit = None
        fired = []
        asks: list[tuple[str, str]] = []
        for _f in self._ga_firings:
            _ans = _f.get("_answer") or {}
            if _f.get("ticket_id") is not None:
                outcome = "escalated"
            elif _f.get("task_ids") or _f.get("ops"):
                outcome = "work_started"
            elif _ans.get("error"):
                outcome = "repair_failed"
            elif _ans.get("superseded"):
                outcome = "superseded"
            elif _f.get("claims_dropped") and not _ans.get("text"):
                # The repair's words claimed an ask it never made and were
                # not sent (_repair_checked): it neither did the work, nor
                # asked, nor said there was no ask — at any level, the ask
                # stands unrepaired (a judged one is never "not an ask").
                outcome = "unrepaired"
            elif _ans and not _ans.get("text") and all(
                    n == _ct_r.TOOL_NAME for n in (_ans.get("calls") or [])):
                # Nothing added. For a judged reading that is the third
                # answer the correction offered; a core or explicit form
                # or a recorded wait had no such answer, and stands
                # unrepaired.
                outcome = "not_an_ask" if _f.get("level") == "judged" else "unrepaired"
            else:
                outcome = "rewrote"
            fired.append({**{k: v for k, v in _f.items() if not k.startswith("_")},
                          "outcome": outcome})
            # each firing on its own: one since the person last spoke whose
            # repair resolved nothing still asks (a handoff wait is not an
            # ask in the reply)
            if (not _f.get("_answered") and _f.get("where") == "reply" and _f.get("level")
                    and not self._go_ahead_resolved(_f, outcome)):
                asks.append((str(_f.get("rule")), _f["level"]))
        if fired:
            rec["fired"] = fired
        try:
            _since = self._pieces[self._spoke_at[0]:]
            _open = [p for p in _since
                     if p["judged"] in (None, "repair") and self._judgeable(p, _ending)]
            _open_hit = _gag_rec.find_go_ahead_hit(join_pieces(_open))
            if _open_hit:
                asks.append((_open_hit[0], _open_hit[2]))
            if (_ending is not None and _ending["judged"] == "fired" and _ending_hit
                    and any(p is _ending for p in _since)):
                asks.append((_ending_hit[0], _ending_hit[2]))
        except Exception as _gaf_err:  # noqa: BLE001
            log.debug("go-ahead final read failed (non-fatal): %s", _gaf_err)
        _rank = {"core": 0, "explicit": 1, "judged": 2}
        for _rule, _lv in asks:
            if rec["final_level"] is None or _rank.get(_lv, 1) <= _rank.get(rec["final_level"], 1):
                rec["final_asks"], rec["final_level"] = _rule, _lv
        return rec


    def _go_ahead_recorded(self) -> bool:
        return bool(self._ga_firings or (self.tools() and not getattr(self.sess, "unseen_lane", False)))


    def _ga_record_now(self) -> dict | None:
        """The record for what the turn has sent so far: the row a Stop or
        a model error leaves carries it (note_turn_reply ->
        keep_sent_reply)."""
        return self._go_ahead_record(self._reply_all()) if self._go_ahead_recorded() else None
