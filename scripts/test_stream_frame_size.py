#!/usr/bin/env python3
"""Gate: a build's SUCCESS frame is not too big to deliver.

One build ran 38.7 minutes, finished,
and emitted its terminal frame:

    {"ts": 1787430027.772706, "seq": 191, "kind": "status",
     "payload": {"state": "complete", "ok": true, ...

`ok: true`. The kernel never saw it. `async for raw in r.content` is
StreamReader.readline() -> readuntil(max_size=self._high_water), and
_high_water is read_bufsize*2 = 524288 on aiohttp's default (this tree never
sets read_bufsize). harness_core builds that frame with the FULL transcript and
the FULL worktree diff, neither truncated, so its size tracks the size of the
build. Phase 10's crossed 512 KiB, readline() raised LineTooLong, and the
kernel wrote the run down as ok=f / exit_code=NULL / diff_bytes=0 /
transcript='' and failed the phase — after every token was paid for.

The failure is proportional to how much a build produced and lands on the very
last frame, which is the worst possible place. Removing the total run cap (so
long builds are allowed at all) makes large terminal frames MORE common, so a
limit that had been latent for months became a live one.

Case 1 below is the RED: it drives a real aiohttp StreamReader, not a mock, and
reproduces the production exception verbatim.

Run:  python3 scripts/test_stream_frame_size.py
"""
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
# provider_gateway imports `mastercode.*` as a top level, so brain/ has to be
# on the path too — same as the kernel's own sys.path at boot.
sys.path.insert(1, str(_ROOT / "brain"))

import aiohttp                                              # noqa: E402
from aiohttp.helpers import DEFAULT_CHUNK_SIZE              # noqa: E402
from aiohttp.http_exceptions import LineTooLong             # noqa: E402
from aiohttp.streams import StreamReader                    # noqa: E402

from server.routes import review                            # noqa: E402
from brain.mastercode import provider_gateway               # noqa: E402

# Python 3.14 refuses asyncio.get_event_loop() outside a running loop, and a
# real StreamReader needs one at construction — so own the loop explicitly and
# drive everything on it.
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)


def run(coro):
    return LOOP.run_until_complete(coro)


FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


# ── a REAL aiohttp StreamReader, so the limit under test is aiohttp's own ────
class _Proto:
    connected = True

    def pause_reading(self, **kw):
        pass

    def resume_reading(self, **kw):
        pass


def reader(*chunks: bytes) -> StreamReader:
    """A real StreamReader pre-fed with `chunks`, then EOF. Chunk boundaries
    are preserved, so a record can be made to straddle two reads."""
    sr = StreamReader(_Proto(), DEFAULT_CHUNK_SIZE,
                      loop=LOOP)
    for c in chunks:
        sr.feed_data(c)
    sr.feed_eof()
    return sr


async def collect(it) -> list[bytes]:
    return [x async for x in it]


# The frame that killed the build, reconstructed to the shape harness_core
# actually emits: state/ok/exit_code/transcript/diff, transcript + diff over
# the ceiling between them.
BIG_TERMINAL = {
    "ts": 1787430027.772706, "seq": 191, "kind": "status",
    "payload": {
        "state": "complete", "ok": True, "exit_code": 0,
        "transcript": "T" * 300_000,
        "diff": "D" * 300_000,
        "stderr": "", "model": "k3", "elapsed_seconds": 2319.9,
    },
}
BIG_LINE = json.dumps(BIG_TERMINAL).encode() + b"\n"


print("== 1. RED: the ceiling is real, and it is aiohttp's default ==")
lo, hi = reader(b"").get_read_buffer_limits()
check("read_bufsize is aiohttp's default (this tree never sets it)",
      lo == DEFAULT_CHUNK_SIZE == 262144, f"{lo}")
check("...so one line may not exceed 524288 bytes", hi == 524288, f"{hi}")
check("the frame that failed really is over that ceiling",
      len(BIG_LINE) > hi, f"{len(BIG_LINE)} vs {hi}")


async def _old_reader_raises() -> str:
    try:
        async for _ in reader(BIG_LINE):
            pass
    except LineTooLong as e:
        return str(e)
    return ""


_err = run(_old_reader_raises())
check("`async for x in content` RAISES on that frame — the shipped defect",
      isinstance(_err, str) and "Got more than 524288 bytes" in _err, repr(_err[:80]))
check("...and the message matches what a live Host recorded verbatim",
      "Got more than 524288 bytes when reading" in _err, repr(_err[:80]))


print("== 2. _iter_ndjson_lines delivers that same frame whole ==")
got = run(collect(review._iter_ndjson_lines(reader(BIG_LINE))))
check("exactly one record", len(got) == 1, f"{len(got)}")
check("delivered byte-for-byte", got and got[0] == BIG_LINE[:-1],
      f"{len(got[0]) if got else 0} vs {len(BIG_LINE) - 1}")
_parsed = json.loads(got[0]) if got else {}
check("it still parses as the terminal frame, ok=True",
      _parsed.get("payload", {}).get("ok") is True)


print("== 3. framing correctness (what readline used to do for us) ==")
many = run(collect(review._iter_ndjson_lines(
    reader(b'{"a":1}\n{"b":2}\n{"c":3}\n'))))
check("several records inside ONE chunk all come out",
      many == [b'{"a":1}', b'{"b":2}', b'{"c":3}'], f"{many}")

split = run(collect(review._iter_ndjson_lines(
    reader(b'{"a":1}\n{"bcd', b'ef":2}\n'))))
check("a record straddling two reads is reassembled",
      split == [b'{"a":1}', b'{"bcdef":2}'], f"{split}")

# readline() returns a trailing unterminated line at EOF; the caller counts it
# in frames_seen and records it rather than dropping it, so parity matters.
tail = run(collect(review._iter_ndjson_lines(reader(b'{"a":1}\n{"trunc'))))
check("a truncated final record is still yielded, as readline did",
      tail == [b'{"a":1}', b'{"trunc'], f"{tail}")

blank = run(collect(review._iter_ndjson_lines(reader(b""))))
check("an empty body yields nothing and does not hang", blank == [], f"{blank}")


print("== 4. END TO END: the long-build scenario now succeeds ==")


class _Resp:
    status = 200

    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Http:
    def __init__(self, content):
        self._c = content

    def post(self, *a, **kw):
        return _Resp(self._c)


_pre = [
    json.dumps({"ts": 1, "seq": 0, "kind": "text",
                "payload": {"text": "building"}}).encode() + b"\n",
    json.dumps({"ts": 2, "seq": 1, "kind": "tool_call",
                "payload": {"tool": "Bash", "args": {"command": "ls"}}}).encode() + b"\n",
]
with tempfile.TemporaryDirectory() as td:
    sdir = Path(td)
    seen = {"n": 0}
    terminal = run(review._stream_harness_run(
        _Http(reader(*_pre, BIG_LINE)), "http://harness", {"prompt": "x"},
        "kimi", sdir, idle_timeout=30, frames_seen=seen))
    check("the run is reported OK — not a dead stream",
          terminal.get("ok") is True, f"{terminal.get('ok')!r}")
    check("exit_code survives (it was NULL in the failed row)",
          terminal.get("exit_code") == 0, f"{terminal.get('exit_code')!r}")
    check("the full transcript survives (it was '' in the failed row)",
          len(terminal.get("transcript") or "") == 300_000,
          f"{len(terminal.get('transcript') or '')}")
    check("the full diff survives (diff_bytes was 0 in the failed row)",
          len(terminal.get("diff") or "") == 300_000,
          f"{len(terminal.get('diff') or '')}")
    check("all three frames were counted", seen["n"] == 3, f"{seen['n']}")
    on_disk = review._events_path(sdir, "kimi").read_text().splitlines()
    check("all three frames reached events.jsonl", len(on_disk) == 3,
          f"{len(on_disk)}")
    check("the oversize frame on disk is intact and re-readable",
          json.loads(on_disk[-1])["payload"]["ok"] is True)


print("== 5. the sibling SSE reader has the same ceiling removed ==")
_sse = b"data: " + json.dumps({"choices": [{"delta": {"content": "x" * 600_000}}]}).encode() + b"\n"
sse_got = run(collect(provider_gateway._iter_sse_lines(reader(_sse))))
check("an oversize SSE data: line survives", sse_got == [_sse[:-1]],
      f"{len(sse_got)} record(s)")
check("...and it is over the ceiling, so this is a real case",
      len(_sse) > 524288, f"{len(_sse)}")


print("== 6. neither call site can regress to the bare iterator ==")
_rev = (_ROOT / "server" / "routes" / "review.py").read_text()
_pg = (_ROOT / "brain" / "mastercode" / "provider_gateway.py").read_text()
# `async for <name> in <expr>.content:` — the readline() form. Matched on code
# lines only; the docstrings above deliberately quote it to explain the bug.
_bare = re.compile(r"^\s*async for \w+ in [\w.]+\.content:", re.M)
check("review.py no longer iterates a response body directly",
      not _bare.search(_rev), str(_bare.findall(_rev)))
check("provider_gateway.py no longer iterates a response body directly",
      not _bare.search(_pg), str(_bare.findall(_pg)))
check("review.py's stream reader goes through the framing helper",
      "_iter_ndjson_lines(r.content)" in _rev)
check("provider_gateway's local reader goes through its framing helper",
      "_iter_sse_lines(resp.content)" in _pg)


print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all gates passed")
