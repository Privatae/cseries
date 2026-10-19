#!/usr/bin/env python3
"""A finished run's large result record (over harness_input.RECORD_INLINE_MAX)
is never failed by the Host's large-record read limit.

Two halves:

1. The limit times the TRANSFER of one record, not the wait for one of the
   LARGE_PARSES_MAX parse slots. Before, the wait and the transfer shared
   LARGE_RECORD_TOTAL_S, so a record that arrived in a second was refused
   because four others were ahead of it.
2. A run's terminal record that is still arriving at the limit keeps what
   arrived (state and ok lead the record) as its result, with a result_cut
   note saying where it stopped, instead of the run being recorded ok:False.
   The trickle-sender bound itself stays: it protects the Host's parse
   threads.

No pod, no DB: fake response bodies on the event loop, the real
harness_input and review functions.

Run: APP_ROOT=<repo> python3 scripts/test_large_result_record_not_failed_by_slot_wait.py
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []
MiB = 1 << 20


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class _Parts:
    """An aiohttp-like body iterator: each __anext__ is a fresh read, so a
    cancelled read loses nothing (as aiohttp's AsyncStreamIterator)."""

    def __init__(self, parts):
        self.parts = list(parts)          # [(delay_s, bytes)]

    def iter_any(self):
        return self

    def iter_chunked(self, _n):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.parts:
            raise StopAsyncIteration
        delay, chunk = self.parts[0]
        if delay:
            await asyncio.sleep(delay)
            self.parts[0] = (0, chunk)
        return self.parts.pop(0)[1]


class _Resp:
    status = 200

    def __init__(self, parts):
        self.content = _Parts(parts)

    def raise_for_status(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Http:
    def __init__(self, parts):
        self.parts = parts

    def post(self, *a, **kw):
        return _Resp(self.parts)


def main() -> int:
    from server import harness_input as hi
    from server.routes import review as rv

    old_total = hi.LARGE_RECORD_TOTAL_S
    hi.LARGE_RECORD_TOTAL_S = 0.5
    try:
        print("== 1. the slot wait is not charged to the record ==")

        async def slot_wait_case():
            pool, sem = hi._large_parse_slots()
            for _ in range(hi.LARGE_PARSES_MAX):
                await sem.acquire()           # every slot busy

            async def free_later():
                await asyncio.sleep(1.5)      # 3x the transfer limit
                for _ in range(hi.LARGE_PARSES_MAX):
                    sem.release()
            freer = asyncio.create_task(free_later())
            body = json.dumps({"ok": True, "exit_code": 0, "transcript": "t" * (9 * MiB)}).encode()

            async def rest():
                yield body[8 * MiB:]
            try:
                obj, _info = await hi.parse_large_record(body[:8 * MiB], rest(), rv._RESULT_RULES, "t")
                got = ("read", obj.get("ok"))
            except hi.HarnessInputRefused as exc:
                got = ("refused", str(exc)[:160])
            await freer
            return got
        got = asyncio.run(slot_wait_case())
        check("1a. a large record that waits for a parse slot longer than LARGE_RECORD_TOTAL_S, "
              "then arrives at once, is read (only its transfer is timed)",
              got == ("read", True), got)

        print("== 2. the trickle-sender bound stays, and keeps what arrived ==")

        async def trickle_case():
            read = [0]

            async def slow():
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    read[0] += 1
                    yield b"T" * 1024
                yield b'"}'
            first = b'{"ok": true, "exit_code": 0, "transcript": "' + b"T" * (9 * MiB)
            try:
                await hi.parse_large_record(first, slow(), rv._RESULT_RULES, "t")
                return None, read[0]
            except hi.LargeRecordTimeout as exc:
                return exc, read[0]
        exc, n_read = asyncio.run(trickle_case())
        check("2a. a record still trickling after LARGE_RECORD_TOTAL_S of transfer is still "
              "stopped before the trickle ends (it protects the Host's parse threads)",
              exc is not None and n_read <= 60, (exc, n_read))
        msg = str(exc) if exc is not None else ""
        check("2b. the stop says which limit and why, plainly",
              "still arriving after" in msg and "parse slots" in msg, msg)
        partial = getattr(exc, "partial", None)
        obj, info = partial if isinstance(partial, tuple) else (None, None)
        inc = (info or {}).get("incomplete") or {}
        check("2c. what arrived comes back with the stop: the fields that arrived whole, and the "
              "kept part of the one arriving, marked as cut short",
              isinstance(obj, dict) and obj.get("ok") is True and obj.get("exit_code") == 0
              and str(obj.get("transcript", "")).endswith("[... the rest of this did not arrive]"),
              {k: (v if k != "transcript" else str(v)[-60:]) for k, v in (obj or {}).items()})
        check("2d. ...and info['incomplete'] says where it stopped and after how long",
              inc.get("at") == "transcript" and inc.get("after_s") == 0.5
              and (inc.get("received_bytes") or 0) >= 9 * MiB, inc)

        print("== 3. the buffered /run result (read_record) ==")

        async def buffered_case(lead: bytes):
            first = lead + b"T" * (9 * MiB)
            resp = _Resp([(0, first), (1.5, b'", "diff": "x"}')])
            try:
                return await hi.read_record(resp, rv._RESULT_RULES, "/run result")
            except hi.HarnessInputRefused as exc:
                return ("refused", str(exc)[:120])
        got = asyncio.run(buffered_case(b'{"ok": true, "exit_code": 0, "transcript": "'))
        ok3 = (isinstance(got, tuple) and isinstance(got[0], dict) and got[0].get("ok") is True
               and isinstance(got[1], dict) and (got[1].get("incomplete") or {}).get("at") == "transcript")
        check("3a. a finished run's buffered result still arriving at the limit is returned as what "
              "arrived (ok as the pod sent it) with info['incomplete'], not refused",
              ok3, got if not ok3 else "")
        got = asyncio.run(buffered_case(b'{"transcript": "'))
        check("3b. ...but a record that has not yet said how the run ended is still refused",
              isinstance(got, tuple) and got[0] == "refused", got)

        print("== 4. the stream path: a terminal frame cut short is the run's result ==")
        term_head = (b'{"ts": 1, "seq": 5, "kind": "status", "payload": {"state": "complete", '
                     b'"ok": true, "exit_code": 0, "transcript": "' + b"T" * (9 * MiB))
        parts = [(0, b'{"kind": "text", "payload": {"text": "before"}}\n'), (0, term_head),
                 (1.5, b"T" * MiB + b'", "diff": "d"}}\n'),
                 (0, b'{"kind": "text", "payload": {"text": "after"}}\n')]

        async def frames():
            out = []
            async for raw in rv._iter_ndjson_lines(_Parts(parts)):
                if isinstance(raw, rv._LargeFrame):
                    ev = raw.as_frame()
                    out.append(("large", raw.error, (ev.get("payload") or {}).get("state"),
                                (ev.get("payload") or {}).get("ok"),
                                ((raw.info or {}).get("incomplete") or {}).get("at")))
                else:
                    ev = rv._parse_stream_record(raw)
                    out.append(("small", (ev or {}).get("payload", {}).get("text")))
            return out
        got4 = asyncio.run(frames())
        check("4a. a terminal frame still arriving at the limit comes out as the frame (state, ok "
              "as sent) with what was cut short, and the next record is framed after it",
              got4 == [("small", "before"), ("large", None, "complete", True, "payload/transcript"),
                       ("small", "after")], got4)
        nonterm = [(0, b'{"kind": "text", "payload": {"text": "' + b"x" * (9 * MiB)),
                   (1.5, b'"}}\n'), (0, b'{"kind": "text", "payload": {"text": "after"}}\n')]

        async def frames_nonterm():
            out = []
            async for raw in rv._iter_ndjson_lines(_Parts(nonterm)):
                out.append(("large", raw.error is not None) if isinstance(raw, rv._LargeFrame)
                           else ("small", (rv._parse_stream_record(raw) or {}).get("payload", {}).get("text")))
            return out
        got4b = asyncio.run(frames_nonterm())
        check("4b. a NON-terminal frame cut short is still shown as refused (nothing to keep it as)",
              got4b == [("large", True), ("small", "after")], got4b)

        async def run_stream(td):
            return await rv._stream_harness_run(_Http(parts), "http://h", {}, "kimi",
                                                pathlib.Path(td), idle_timeout=30, frames_seen={})
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as td:
            term = asyncio.run(run_stream(td))
            lines = [json.loads(x) for x in rv._events_path(pathlib.Path(td), "kimi").read_bytes().splitlines()]
        states = [(ln.get("payload") or {}).get("state") for ln in lines if ln.get("kind") == "status"]
        rc = term.get("result_cut") if isinstance(term, dict) else None
        check("4c. the run's result is the pod's: ok true, exit 0, not a failed run",
              isinstance(term, dict) and term.get("ok") is True and term.get("exit_code") == 0,
              {k: v for k, v in (term or {}).items() if k not in ("transcript",)})
        check("4d. ...carrying result_cut with what did not arrive",
              isinstance(rc, dict) and (rc.get("incomplete") or {}).get("at") == "payload/transcript", rc)
        check("4e. the run terminal says so (a result_cut status frame) before the frame that "
              "closes it, and that frame says ok",
              "result_cut" in states and "complete" in states
              and states.index("result_cut") < states.index("complete")
              and [ln for ln in lines if (ln.get("payload") or {}).get("state") == "complete"][0]
              ["payload"].get("ok") is True, states)
        note = next(((ln.get("payload") or {}).get("detail") or "" for ln in lines
                     if (ln.get("payload") or {}).get("state") == "result_cut"), "")
        check("4f. the note names the limit, how much came in, and what was not received",
              "still arriving after 0 s" not in note and "still arriving after" in note
              and "partway through the transcript" in note and "not received" in note, note)

        print("== 5. a result that stopped arriving is never dropped as 'only a diff note' ==")
        inc_only = {"cut": {"payload/diff": {"kept_bytes": 1, "total_bytes": 2}}, "dropped": [],
                    "incomplete": {"received_bytes": 9 * MiB, "at": "payload/diff", "after_s": 300.0}}
        kept = rv._without_diff_cut(inc_only)
        check("5a. _without_diff_cut (a run the Host re-diffs) still reports a result cut short",
              isinstance(kept, dict) and kept.get("incomplete") and not kept.get("cut"), kept)
        r = {"ok": True, "result_cut": dict(inc_only)}
        rv._forget_pod_diff_cut(r)
        check("5b. _forget_pod_diff_cut keeps it too, with its sentence",
              isinstance(r.get("result_cut"), dict) and r["result_cut"].get("incomplete")
              and "not received" in (r.get("result_note") or ""), r)
    finally:
        hi.LARGE_RECORD_TOTAL_S = old_total

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - an unfixed tree reads as FAIL, not a traceback
        print(f"  FAIL ran to completion -- {exc!r}"[:300])
        sys.exit(1)
