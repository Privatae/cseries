#!/usr/bin/env python3
"""Gate for the 2026-08-20 streaming-lane fixes (server/routes/review.py +
runtime/tools.py):

  1. _persistable_frame strips dispatcher-only payload keys (rotated_creds)
     from what reaches <session>/<harness>.events.jsonl — the user's OAuth
     tokens were landing in the Bench transcript verbatim (session
     7030c805) — while leaving the in-memory frame untouched so the
     terminal-frame extraction still hands the dispatcher the full payload.
  2. _stream_harness_run end-to-end with a fake aiohttp response: the file on
     disk never contains rotated_creds; the returned terminal payload does.
  3. resolve_session_id maps an unambiguous >=8-char prefix onto the full
     session dir name, leaves full ids / short / ambiguous / unknown input
     alone, and get_session_service accepts the prefix.
  4. _summarize_fan_out carries the FULL session id in its summary text (the
     host quotes what it reads; the 8-char prefix alone is what sent it to
     review_session('7a43f712') → 404).

Run from the repo root:  python3 scripts/test_stream_frame_scrub.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(_ROOT))
# runtime.tools reaches `mastercode.*` as a top level, so brain/ belongs on the
# path too — same as the kernel's own sys.path at boot. Without it this gate
# died at import (never reaching a single check) when run outside the image.
sys.path.insert(1, str(_ROOT / "brain"))

from server.routes import review  # noqa: E402
from brain.mastercode.runtime import tools  # noqa: E402

FAILS = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILS
    print(("  ok   " if cond else "  FAIL ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS += 1


print("== 1. _persistable_frame ==")
creds = {".credentials.json": '{"claudeAiOauth":{"accessToken":"sk-ant-oat01-SECRET","refreshToken":"sk-ant-ort01-SECRET"}}'}
term = {"ts": 1.0, "seq": 9, "kind": "status",
        "payload": {"state": "complete", "ok": True, "exit_code": 0,
                    "transcript": "done", "rotated_creds": creds}}
out = review._persistable_frame(term)
check("rotated_creds stripped from the persisted copy", "rotated_creds" not in out["payload"])
check("every other payload key survives",
      {k: v for k, v in term["payload"].items() if k != "rotated_creds"} == out["payload"])
check("the caller's frame is NOT mutated", term["payload"].get("rotated_creds") is creds)
plain = {"ts": 1.0, "seq": 1, "kind": "text", "payload": {"text": "hi"}}
check("a frame without dispatcher-only keys is returned as-is (same object)",
      review._persistable_frame(plain) is plain)
nonedict = {"kind": "text", "payload": "raw-string"}
check("a non-dict payload is left alone", review._persistable_frame(nonedict) is nonedict)


print("== 2. _stream_harness_run writes a creds-free file, returns the full terminal ==")


class _Content:
    # iter_any(), not __aiter__: the reader frames its own lines now, because
    # `async for x in content` is readline() and readline cannot carry a
    # terminal frame over 512 KiB (see test_stream_frame_size.py).
    def __init__(self, lines):
        self._lines = lines

    def iter_any(self):
        async def gen():
            for ln in self._lines:
                yield ln
        return gen()


class _Resp:
    status = 200

    def __init__(self, lines):
        self.content = _Content(lines)

    def raise_for_status(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Http:
    def __init__(self, lines):
        self._lines = lines

    def post(self, *a, **kw):
        return _Resp(self._lines)


frames = [
    json.dumps({"ts": 1, "seq": 0, "kind": "text", "payload": {"text": "I'll build it."}}).encode() + b"\n",
    json.dumps({"ts": 2, "seq": 1, "kind": "tool_call", "payload": {"tool": "Bash", "args": {"command": "ls"}}}).encode() + b"\n",
    json.dumps(term).encode() + b"\n",
]
with tempfile.TemporaryDirectory() as td:
    sdir = Path(td)
    seen = {"n": 0}
    terminal = asyncio.run(review._stream_harness_run(
        _Http(frames), "http://harness", {"prompt": "x"}, "claude-code", sdir,
        idle_timeout=30, frames_seen=seen))
    n_frames = seen["n"]
    text = review._events_path(sdir, "claude-code").read_text()
    check("events.jsonl has all 3 frames", text.count("\n") == 3, text)
    # The frame COUNT tells the caller whether re-running on buffered /run
    # would duplicate work already done. It rides a caller-owned HOLDER, not a
    # return value, because this function's real failures are raises — see
    # test_stream_fallback_billing.py for that path.
    check("the frames_seen holder is filled as frames land",
          n_frames == 3, f"got {n_frames}")
    check("events.jsonl contains NO rotated_creds / token material",
          "rotated_creds" not in text and "SECRET" not in text)
    check("terminal payload returned to the dispatcher still carries rotated_creds",
          terminal.get("rotated_creds") == creds)
    check("terminal payload keeps ok/exit_code/transcript",
          terminal.get("ok") is True and terminal.get("exit_code") == 0 and terminal.get("transcript") == "done")


print("== 3. resolve_session_id / get_session_service prefix ==")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    _orig_root = review.SESSIONS_ROOT
    review.SESSIONS_ROOT = root
    try:
        full_a = "7a43f71241ad478a91175e78e7fd385e"
        full_b = "7a43f799aaaaaaaaaaaaaaaaaaaaaaaa"
        full_c = "7030c80567d64eef89b052f0777dda0b"
        for s in (full_a, full_b, full_c):
            (root / s).mkdir()
            (root / s / "manifest.json").write_text(json.dumps({"session_id": s, "harnesses": ["claude-code"]}))
        check("full id resolves to itself", review.resolve_session_id(full_a) == full_a)
        check("unambiguous 8-char prefix resolves", review.resolve_session_id("7030c805") == full_c)
        check("unambiguous 8-char prefix resolves even with a near-sibling", review.resolve_session_id("7a43f712") == full_a)
        check("prefix shorter than 8 chars is never resolved (even if unique)",
              review.resolve_session_id("7030c80") == "7030c80")
        check("ambiguous 8+-char prefix is returned unchanged",
              review.resolve_session_id("7a43f7" + "99"[:0] + "12"[:0] or "7a43f7") == "7a43f7")
        (root / "7a43f712deadbeefdeadbeefdeadbeef").mkdir()
        check("a prefix that now matches TWO sessions is returned unchanged",
              review.resolve_session_id("7a43f712") == "7a43f712")
        check("unknown prefix is returned unchanged", review.resolve_session_id("deadbeef") == "deadbeef")
        check("empty/None input is safe", review.resolve_session_id("") == "" and review.resolve_session_id(None) == "")
        res = asyncio.run(review.get_session_service(session_id="7030c805"))
        check("get_session_service accepts the prefix and reports the FULL id",
              res.get("session_id") == full_c and res.get("manifest", {}).get("session_id") == full_c)
        try:
            asyncio.run(review.get_session_service(session_id="deadbeef"))
            check("unknown session still 404s", False)
        except Exception as e:  # HTTPException
            check("unknown session still 404s", getattr(e, "status_code", None) == 404, repr(e))
    finally:
        review.SESSIONS_ROOT = _orig_root


print("== 4. _summarize_fan_out carries the full session id ==")
manifest = {"session_id": "7030c80567d64eef89b052f0777dda0b", "harnesses": ["claude-code"],
            "results": {"claude-code": {"ok": True, "model": "claude-opus-5", "elapsed_seconds": 162.8}}}
state = {"claude-code": {"state": "done", "model": "claude-opus-5", "elapsed_seconds": 162.84, "diff_bytes": 27597, "exit_code": 0}}
ok, summary, per, done, failed = tools._summarize_fan_out(manifest, state)
check("ok + done", ok and done == ["claude-code"] and not failed)
check("summary carries the full session id", "7030c80567d64eef89b052f0777dda0b" in summary, summary)
check("summary still leads with the short tag", summary.startswith("Compare 7030c805"), summary)

print()
print("FAIL" if FAILS else "PASS", f"({FAILS} failing checks)")
sys.exit(1 if FAILS else 0)
