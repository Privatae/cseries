"""Bounded reads of everything a harness pod (or an app runtime pod) sends the
Host.

A harness pod runs model-driven code, so every byte it returns is untrusted:
an HTTP body, a stream, a tar, a JSON document, a request it makes to the
kernel. Each reader here bounds two things, and says what the bound is:

  * Host MEMORY. A body is read in chunks against a cap on DECODED bytes
    (aiohttp inflates gzip/deflate/br/zstd a bounded step at a time,
    DeflateBuffer.max_decompress_size, so a small compressed body cannot
    balloon before the cap sees it), into one buffer, never copied again. JSON
    is refused before parsing when its structure would inflate past a node
    budget (json objects cost ~20x their text: `[[],[],...]`). A run's
    RESULT, which grows with the project, is never refused for size: past
    RECORD_INLINE_MAX it is parsed as it arrives (parse_large_record). A tar
    is pre-scanned from its raw 512-byte headers before tarfile sees it,
    reading them exactly as tarfile will (prescan_tar; the gate holds the
    two to the same layout on real exports and thousands of fuzzed ones):
    tarfile materialises every pax record as Python objects before any
    caller-side check can run (measured on Python 3.14.7: one
    oversized pax header in a 32 MiB export grew RSS by 501 MiB).
  * Event-loop TIME. Anything proportional to the body (JSON over
    INLINE_JSON_MAX, the tar scan, the structure count) runs in a worker
    thread, so the loop only ever moves chunks.

Callers pick the cap for their path; the module refuses with
HarnessInputRefused, a RuntimeError, so existing `except RuntimeError` and
`except Exception` handlers keep working.
"""

from __future__ import annotations

import asyncio
import collections as _collections
import json
import re

INLINE_JSON_MAX = 1 << 20          # parse on the loop at or below this many bytes
ERROR_TAIL_MAX = 4096              # an error body is read to this, never whole
# Structural tokens (values) a JSON body over INLINE_JSON_MAX may hold. It is
# a TIME bound as much as a memory one: json.loads holds the GIL for the whole
# parse, so a worker thread does not keep the loop running (measured:
# 450,000 short strings in 8-16 MiB stalled the loop 86-104 ms).
# 200,000 keeps that near 50 ms. Real bodies hold far fewer: a browser
# snapshot is at most 200 elements (sidecars/browser/control/sessions.py
# MAX_ELEMENTS), a /check result 32 commands, a run's result a few dozen.
JSON_MAX_NODES = 200_000
READ_CHUNK = 1 << 16
# The readers hand the event loop a turn every this many bytes: data aiohttp
# has already inflated and buffered is returned without suspending, so a tight
# read of a capped body would otherwise hold the loop for the whole cap
# (measured: 56 ms for a 32 MiB cap).
READ_YIELD = 1 << 20

# Tar pre-scan limits (see prescan_tar).
TAR_EXT_RECORD_MAX = 64 * 1024     # one pax 'x' or GNU 'L'/'K' record
TAR_EXT_TOTAL_MAX = 64 * 1024 * 1024    # tarfile.add writes a pax path + mtime per long-named
                                        # member: ~200 bytes each, 40 MB at 200,000 members
TAR_NAME_TOTAL_MAX = 32 * 1024 * 1024
TAR_PATH_MAX = 4096


class HarnessInputRefused(RuntimeError):
    """Something a harness pod sent was over a bound and was not read on."""


def _refuse(what: str, why: str) -> HarnessInputRefused:
    return HarnessInputRefused(f"{what} refused: {why}")


# ── HTTP responses from a pod (aiohttp) ─────────────────────────────────────

async def read_body(resp, cap: int, what: str) -> bytearray:
    """The decoded body of `resp`, refused past `cap` bytes: on a declared
    Content-Length before a byte is read (a compressed body's declared length
    is its compressed size, so this is only an early no), and on the decoded
    bytes actually received. Returned as the bytearray it was read into; no
    second copy is made (json.loads and tarfile accept it)."""
    declared = getattr(resp, "content_length", None)
    if declared is not None and declared > cap:
        raise _refuse(what, f"body is {declared} bytes, over the {cap} cap")
    buf = bytearray()
    mark = READ_YIELD
    async for chunk in resp.content.iter_chunked(READ_CHUNK):
        if len(buf) + len(chunk) > cap:
            raise _refuse(what, f"body passed the {cap}-byte cap")
        buf += chunk
        if len(buf) >= mark:
            mark += READ_YIELD
            await asyncio.sleep(0)
    return buf


async def read_json(resp, cap: int, what: str, *, max_nodes: int = JSON_MAX_NODES):
    """read_body, then parse_json."""
    return await parse_json(await read_body(resp, cap, what), what, max_nodes=max_nodes)


async def error_tail(resp, n: int = ERROR_TAIL_MAX) -> str:
    """At most `n` decoded bytes of an error body, as text. Never raises."""
    try:
        return bytes(await resp.content.read(n)).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — an error message must never raise
        return ""


def free_bytes(path) -> int:
    """Bytes free to an unprivileged writer on the filesystem holding `path`."""
    import os
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def disk_floor(path, *, share: float = 0.10, reserve: int = 256 << 20,
               ceiling: int = 1 << 30) -> int:
    """The free space a writer must leave on `path`'s filesystem: `share` of
    it, never less than `reserve` and never more than `ceiling`. A Host's
    PVCs are small (the sessions volume is 2 GiB: 10% alone would be 205
    MiB, so 256 MiB); on a large volume a share would keep back gigabytes
    that are really free ("less than 10021 MiB free" once refused
    a download with 5.3 GiB free), so 1 GiB is the most it keeps back."""
    import os
    st = os.statvfs(path)
    return min(max(int(st.f_blocks * st.f_frsize * share), reserve), ceiling)


async def spill_to_file(resp, dest, cap: int | None, what: str, *,
                        keep_free: int | None = None) -> int:
    """read_body, written to `dest` as it arrives: the body never sits in
    memory (one chunk at a time). Returns the byte count. `dest` is removed on
    refusal or failure. `cap` None means no size cap: for a body that is the
    user's own data (a file their browser agent downloaded), where the Host
    has only its memory to protect, and this protects it. `keep_free` refuses
    the body if writing it would leave fewer bytes free on the filesystem:
    checked against a declared length before a byte is written, and again
    every READ_YIELD bytes."""
    import os
    declared = getattr(resp, "content_length", None)
    if cap is not None and declared is not None and declared > cap:
        raise _refuse(what, f"body is {declared} bytes, over the {cap} cap")
    folder = os.path.dirname(os.path.abspath(dest))

    def _room(more: int) -> None:
        if keep_free is not None and free_bytes(folder) - more < keep_free:
            raise _refuse(what, f"saving it would leave less than {keep_free >> 20} MiB "
                                "free on the disk")
    if declared is not None:
        _room(declared)
    n = 0
    try:
        with open(dest, "wb") as fh:
            mark = READ_YIELD
            async for chunk in resp.content.iter_chunked(READ_CHUNK):
                n += len(chunk)
                if cap is not None and n > cap:
                    raise _refuse(what, f"body passed the {cap}-byte cap")
                fh.write(chunk)
                if n >= mark:
                    mark += READ_YIELD
                    _room(0)
                    await asyncio.sleep(0)
    except BaseException:
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
    return n


# ── requests a pod makes to the kernel (Starlette) ──────────────────────────

async def read_request(request, cap: int, what: str) -> bytearray:
    """The body of an inbound request, refused past `cap` bytes, read from
    request.stream() so a chunked body is bounded as it arrives (Starlette's
    request.body() reads it whole, then a length check is too late)."""
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > cap:
                raise _refuse(what, f"request body is {declared} bytes, over the {cap} cap")
        except ValueError:
            raise _refuse(what, "bad Content-Length") from None
    buf = bytearray()
    mark = READ_YIELD
    async for chunk in request.stream():
        if len(buf) + len(chunk) > cap:
            raise _refuse(what, f"request body passed the {cap}-byte cap")
        buf += chunk
        if len(buf) >= mark:
            mark += READ_YIELD
            await asyncio.sleep(0)
    return buf


async def read_request_json(request, cap: int, what: str, *, max_nodes: int = JSON_MAX_NODES):
    return await parse_json(await read_request(request, cap, what), what, max_nodes=max_nodes)


# ── JSON ─────────────────────────────────────────────────────────────────────

_JSON_STRING = re.compile(rb'"[^"\\]*+(?:\\.[^"\\]*+)*+"')   # possessive: see _STR_RUN


def json_nodes(body, limit: int | None = None) -> int:
    """An upper bound on the values a JSON document holds: the structural
    tokens (`[`, `{`, `,`) outside strings, plus one. All of them are counted
    in C first; when that is already within `limit` nothing more is done (a
    long transcript full of commas is one string, the common case). Else the
    ones inside each string are subtracted, string by string, stopping as
    soon as the count is within `limit`. A Python-level loop on purpose: one
    C call over the whole body (the re.sub this replaced) holds the GIL for
    all of it (measured: a 65 ms loop stall and a 77 MiB collapsed copy for
    450,000 short strings), where a loop lets the event loop in every few
    milliseconds and holds one match at a time."""
    if isinstance(body, memoryview):
        body = bytes(body)
    total = body.count(b"[") + body.count(b"{") + body.count(b",") + 1
    if limit is not None and total <= limit:
        return total
    for m in _JSON_STRING.finditer(body):
        a, z = m.span()
        inside = body.count(b"[", a, z) + body.count(b"{", a, z) + body.count(b",", a, z)
        if inside:
            total -= inside
            if limit is not None and total <= limit:
                return total
    return total


def _parse_bounded(body, what: str, max_nodes: int):
    if len(body) > INLINE_JSON_MAX and json_nodes(body, max_nodes) > max_nodes:
        raise _refuse(what, f"JSON holds more than {max_nodes} values")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise _refuse(what, f"invalid JSON ({exc})") from None


async def parse_json(body, what: str, *, max_nodes: int = JSON_MAX_NODES):
    """json.loads of a body already read under a cap. At or below
    INLINE_JSON_MAX it is parsed on the loop (bounded: ~20 MiB of objects at
    the worst inflation); above, the structure is counted and the parse runs
    in a worker thread."""
    if not body:
        return None
    if len(body) <= INLINE_JSON_MAX:
        try:
            return json.loads(body)
        except ValueError as exc:
            raise _refuse(what, f"invalid JSON ({exc})") from None
    return await asyncio.to_thread(_parse_bounded, body, what, max_nodes)


# ── one large JSON record, parsed as it streams ─────────────────────────────
#
# A harness run ends with ONE record that carries the whole transcript and the
# whole pod-side worktree diff (harness_core's terminal frame and the buffered
# /run result; neither is truncated pod-side). Measured over the
# 130 pod-authored terminal frames on disk (149 events.jsonl files from
# two Hosts): whole line p50 144 KB, p90 1.25 MB,
# p99 5.7 MB, max 19.3 MB (a grok run whose diff was 18.4 MB of node_modules);
# transcript max 2.2 MB; diff max 18.4 MB. The record grows with the project,
# so no cap on it can be right: a cap fails a finished build after every token
# is paid for. So a record up to RECORD_INLINE_MAX is read whole and
# json.loads'd, and a larger one is PARSED AS IT ARRIVES (parse_large_record):
# a worker thread walks the JSON from a small queue of chunks, keeps the fields
# the Host uses up to a stated size in UTF-8 BYTES (the caller's `rules`: the
# transcript's tail, the diff's head, each with a marker saying what was cut),
# keeps every other value up to SMALL_VALUE_MAX bytes, and never holds the
# record. Input is read exactly as json.loads reads bytes: UTF-8 with
# surrogatepass (a raw surrogate sequence is kept, one code point each), and
# a record json.loads would refuse (invalid UTF-8 included) is refused.

RECORD_INLINE_MAX = 8 << 20      # 5.7 MB is the p99 terminal record; above this, streamed
SMALL_VALUE_MAX = 1 << 20        # any other value in a large record (stderr max seen: 5 KB)
SMALL_TOTAL_MAX = 8 << 20        # all the other values of one large record together
LARGE_RECORD_NODES = 100_000     # JSON values kept from one large record (real ones: < 100)
LARGE_RECORD_SCAN = 1_000_000    # JSON values walked at all: past this it is not a result
                                 # (each costs Python time; ~1 s for a million)
_QUEUE_CHUNKS = 8

_WS = re.compile(rb"[ \t\r\n]*")
# One run of a JSON string's body up to its closing quote: plain bytes and
# COMPLETE escapes only, so a run always ends on an escape boundary (a `\u`
# cut by a chunk edge is left for the next chunk). Control bytes end a run
# (JSON forbids them raw), and so does any invalid escape.
# POSSESSIVE quantifiers: with plain ones, sre keeps backtracking state per
# repetition, measured at ~40 bytes per escape (509 MiB for 12 MB of a
# JSON-escaped diff); possessive, the same match takes 0.2 MiB and a quarter
# of the time. The grammar is unambiguous, so no match changes.
_STR_RUN = re.compile(rb'[^"\\\x00-\x1f]*+(?:(?:\\["\\/bfnrt]|\\u[0-9a-fA-F]{4})[^"\\\x00-\x1f]*+)*+')
_SCALAR = re.compile(rb"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null"
                     rb"|NaN|-?Infinity")          # json.loads takes these three too
_TOKEN = re.compile(rb"[^ \t\r\n,\]}]*")
_HIGH_SURROGATE_ESCAPE = re.compile(rb"\\u[dD][89abAB][0-9a-fA-F]{2}\Z")


class LargeRecordTimeout(HarnessInputRefused):
    """A large record still arriving after LARGE_RECORD_TOTAL_S of transfer:
    the rest of it was NOT read, so a caller framing a stream must skip to the
    end of that record itself. `partial` is (object, info) for what did
    arrive (info["incomplete"] says where it stopped), or None when what
    arrived is not a JSON object the Host can read. A caller holding a
    finished run's result keeps the partial rather than failing the run."""

    def __init__(self, message: str, partial: "tuple | None" = None):
        super().__init__(message)
        self.partial = partial


class LargeRecordError(ValueError):
    """A large record that is not the JSON object it must be."""
    # Not a refusal of the caller's input (a harness's own output did not parse): the platform's
    # (tool_call_ledger.refused_if_caller).
    caller_refusal = False


class _CutShort(Exception):
    """The record stopped being read before its end (LargeRecordTimeout):
    raised inside the parser so it can hand back what it has."""


class _Drop:
    pass


_DROPPED = _Drop()


class _Text:
    """One string's kept part, sized in UTF-8 BYTES: the form every store of
    it holds (the diff's patch file, the harness_run row, the events file,
    all UTF-8). 'head' keeps the first `limit` bytes, 'tail' the last `limit`,
    each cut on a character boundary; `total` counts every byte seen. The
    kept part is held as UTF-8 bytes, never as more than `limit` of them: the
    tail drops whole pieces off its front as it grows, then trims the first
    piece (a copy of at most _PIECE bytes per add), and it is decoded to text
    once, when the string ends. (The first version counted CHARACTERS, and
    let the tail reach 2x its limit before compacting: a 24 Mi-character
    emoji transcript kept 96 MiB where the limit said 16.)"""

    def __init__(self, mode: str, limit: int):
        self.mode, self.limit = mode, limit
        self.parts: "_collections.deque[bytes]" = _collections.deque()
        self.kept = self.total = 0
        self.full = False

    def add(self, s: str) -> None:
        b = s.encode("utf-8", "surrogatepass")
        self.total += len(b)
        if self.mode == "head":
            if self.full:
                return
            room = self.limit - self.kept
            if len(b) > room:
                # the first cut ends the head: a later, shorter add must not
                # append after a character that was dropped here
                self.full = True
                b = b[:max(room, 0)]
                hold = _utf8_hold(b)            # never end on half a character
                if hold:
                    b = b[:-hold]
            if b:
                self.parts.append(b)
                self.kept += len(b)
            return
        # Pieces are cut on CHARACTER boundaries, so every piece starts on a
        # character and dropping whole pieces off the front can never leave
        # half of one at the start (pieces cut at raw
        # byte offsets left continuation bytes at the front after a pop, and
        # the decode then failed on valid input).
        i, n = 0, len(b)
        while i < n:
            j = min(i + _PIECE, n)
            while j < n and 0x80 <= b[j] < 0xC0:
                j -= 1
            if j <= i:                          # no boundary in reach (cannot happen in UTF-8)
                j = min(i + _PIECE, n)
            self.parts.append(b[i:j])
            self.kept += j - i
            i = j
        while self.kept > self.limit:
            first = self.parts[0]
            over = self.kept - self.limit
            if len(first) <= over:
                self.parts.popleft()
                self.kept -= len(first)
                continue
            k = over
            while k < len(first) and 0x80 <= first[k] < 0xC0:
                k += 1                          # start on a character, not inside one
            self.parts[0] = first[k:]
            self.kept -= k
            if not self.parts[0]:
                self.parts.popleft()

    def value(self) -> str:
        # The marker is joined as BYTES and the whole decoded once: adding it
        # to the decoded str would copy the str (measured: +64 MiB at peak
        # for a 16 MiB kept part whose text is 4 bytes a character).
        cut = self.total - self.kept
        if cut and self.mode == "tail":
            self.parts.appendleft(f"[... {cut} earlier bytes not kept by the Host]\n".encode())
        elif cut:
            self.parts.append(f"\n[... {cut} more bytes not kept by the Host]".encode())
        data = b"".join(self.parts)
        self.parts.clear()
        text = data.decode("utf-8", "surrogatepass")
        del data
        return text


_PIECE = 1 << 16


def _utf8_hold(seg: bytes) -> int:
    """How many trailing bytes of `seg` are an incomplete UTF-8 character
    (a pod may send raw UTF-8 in a string; a chunk edge can cut one)."""
    for back in range(1, min(4, len(seg)) + 1):
        b = seg[-back]
        if b < 0x80:
            return 0
        if b >= 0xC0:
            need = 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            return back if back < need else 0
    return 0


class _Skim:
    """A pull parser for ONE JSON object, reading chunks from `read()`
    (b"" at the record's end). Values at a path in `rules` ({path tuple:
    (mode, limit)}) that are strings go to a _Text; every other value is kept
    up to SMALL_VALUE_MAX (strings: their head, with a marker; containers:
    whole or not at all) within SMALL_TOTAL_MAX for all of them. Objects on
    the way to a rule path are walked, never sized. Holds one chunk and what
    it keeps.

    `read()` returning None means the record stopped being read before its
    end; partial() then gives what had arrived. For that, the objects on the
    way to a rule path are noted as they open (they join their parent only
    when they close), and so is the kept string being read."""

    def __init__(self, read, rules: dict):
        self.read, self.rules = read, rules
        self.route = {pth[:i] for pth in rules for i in range(len(pth))} | {()}
        self.buf, self.pos, self.base, self.eof = b"", 0, 0, False
        self.small_left, self.nodes, self.scanned = SMALL_TOTAL_MAX, 0, 0
        self.cut: dict = {}
        self.dropped: list = []
        self.open: dict = {}          # path -> an on-route object, from when it opens
        self.cur = None               # (path, _Text) of the kept string being read

    def partial(self) -> tuple:
        """(object, info) for a record cut short: the object holds every
        value that arrived whole, plus the kept part of the string that was
        arriving, marked; info["incomplete"] says where it stopped. The
        object is None when the record's own braces never opened."""
        root = self.open.get(())
        at = kept = got = None
        if root is not None:
            for path in sorted(self.open, key=len):
                parent = self.open.get(path[:-1]) if path else None
                if isinstance(parent, dict):
                    parent.setdefault(path[-1], self.open[path])
            if self.cur is not None:
                path, sink = self.cur
                at, kept, got = "/".join(map(str, path)), sink.kept, sink.total
                parent = self.open.get(path[:-1])
                if isinstance(parent, dict) and path:
                    parent[path[-1]] = sink.value() + "\n[... the rest of this did not arrive]"
        info = {"cut": {"/".join(k): v for k, v in self.cut.items()}, "dropped": self.dropped,
                "incomplete": {"received_bytes": self._off(), "at": at,
                               "kept_bytes": kept, "field_received_bytes": got}}
        return root, info

    # -- input ---------------------------------------------------------------
    def _more(self) -> bool:
        if self.eof:
            return False
        chunk = self.read()
        if chunk is None:
            raise _CutShort()
        if not chunk:
            self.eof = True
            return False
        self.base += self.pos
        self.buf = self.buf[self.pos:] + bytes(chunk)
        self.pos = 0
        return True

    def _off(self) -> int:
        return self.base + self.pos

    def _peek(self) -> int:
        while True:
            self.pos = _WS.match(self.buf, self.pos).end()
            if self.pos < len(self.buf):
                return self.buf[self.pos]
            if not self._more():
                return -1

    def _expect(self, ch: int) -> None:
        if self._peek() != ch:
            raise LargeRecordError(f"expected {chr(ch)!r}")
        self.pos += 1

    # -- values --------------------------------------------------------------
    def parse(self):
        if self._peek() != 0x7B:
            raise LargeRecordError("not a JSON object")
        out = self._value((), 0, keep=True, limit=None)
        if self._peek() != -1:
            raise LargeRecordError("data after the object")
        return out

    def _value(self, path, depth, *, keep, limit):
        if depth > 64:
            raise LargeRecordError("nested deeper than 64")
        self.scanned += 1
        if self.scanned > LARGE_RECORD_SCAN:
            raise LargeRecordError(f"more than {LARGE_RECORD_SCAN} JSON values")
        c = self._peek()
        if c == 0x22:
            return self._string(path, keep=keep, limit=limit)
        if c in (0x7B, 0x5B):
            return self._container(path, depth, c == 0x7B, keep=keep, limit=limit)
        return self._scalar(keep)

    def _count(self, keep: bool) -> bool:
        if not keep:
            return False
        self.nodes += 1
        return self.nodes <= LARGE_RECORD_NODES

    def _scalar(self, keep):
        # the token runs to a delimiter; one cut by a chunk edge waits for more
        while True:
            end = _TOKEN.match(self.buf, self.pos).end()
            if end < len(self.buf) or self.eof or end - self.pos > 400:
                break
            self._more()
        m = _SCALAR.fullmatch(self.buf, self.pos, end)
        if not m or end - self.pos > 400:
            raise LargeRecordError("invalid JSON value")
        self.pos = end
        if not self._count(keep):
            return _DROPPED
        return json.loads(m.group())

    def _string(self, path, *, keep, limit, key=False):
        rule = self.rules.get(path) if keep and not key else None
        if rule is not None:
            sink = _Text(*rule)
        elif keep:
            sink = _Text("head", min(limit if limit is not None else SMALL_VALUE_MAX,
                                     SMALL_VALUE_MAX))
        else:
            sink = None
        self.cur = (path, sink) if sink is not None and not key else None
        self.pos += 1                                      # the opening quote
        carry = b""
        while True:
            end = _STR_RUN.match(self.buf, self.pos).end()
            closed = end < len(self.buf) and self.buf[end] == 0x22
            waiting = (not closed and not self.eof
                       and (end == len(self.buf) or len(self.buf) - end < 6))
            if not closed and not waiting:
                raise LargeRecordError("invalid or unterminated JSON string")
            seg = carry + self.buf[self.pos:end]
            carry = b""
            self.pos = end
            if sink is not None and seg:
                if not closed:
                    hold = _utf8_hold(seg)
                    if hold:
                        seg, carry = seg[:-hold], seg[-hold:]
                try:
                    text = json.loads(b'"' + seg + b'"')
                except ValueError:
                    raise LargeRecordError("invalid JSON string") from None
                if (not closed and text and "\ud800" <= text[-1] <= "\udbff"
                        and _HIGH_SURROGATE_ESCAPE.search(seg)):
                    # A `\uD8xx` escape at a chunk edge may be the first half
                    # of a pair json.loads joins with a `\uDCxx` that follows:
                    # carry the 6-byte escape, so the pair decodes whole. A
                    # surrogate sent as raw bytes (ED A0 80) is never joined
                    # by json.loads (it decodes bytes with surrogatepass, one
                    # code point per sequence), so it is not carried: carrying
                    # it by the escape's length duplicated up to 3 bytes.
                    text, carry = text[:-1], seg[-6:] + carry
                sink.add(text)
            if closed:
                self.pos += 1
                break
            if not self._more():
                raise LargeRecordError("unterminated JSON string")
        self.cur = None
        if key:
            return sink.value() if sink is not None else ""
        if sink is None or not self._count(keep):
            return _DROPPED
        if sink.total > sink.kept:
            self.cut[path] = {"kept_bytes": sink.kept, "total_bytes": sink.total}
        return sink.value()

    def _container(self, path, depth, is_obj, *, keep, limit):
        start = self._off()
        on_route = keep and path in self.route
        self.pos += 1
        out = ({} if is_obj else []) if keep else None
        keep = self._count(keep) and keep
        if keep and on_route and is_obj:
            self.open[path] = out
        close = 0x7D if is_obj else 0x5D
        if self._peek() == close:
            self.pos += 1
            return out if keep else _DROPPED
        while True:
            if is_obj:
                if self._peek() != 0x22:
                    raise LargeRecordError("expected an object key")
                kstart = self._off()
                key = self._string(path, keep=True, limit=4096, key=True)
                if self._off() - kstart > 4096 + 2:
                    raise LargeRecordError("an object key over 4096 bytes")
                self._expect(0x3A)
                sub = path + (key,)
            else:
                sub = path + ("[]",)
            c = self._peek()
            if on_route and ((sub in self.rules and c == 0x22)
                             or (sub in self.route and c == 0x7B)):
                # a kept field (a string, sized by its rule) or an object on
                # the way to one (walked, not sized)
                v = self._value(sub, depth + 1, keep=True, limit=None)
            elif on_route:
                # Any other value: its own budget, within the record's total
                # for such values. Dropped whole past it (a string keeps its
                # head, with a marker).
                room = min(SMALL_VALUE_MAX, self.small_left)
                vstart = self._off()
                v = self._value(sub, depth + 1, keep=room > 0, limit=room)
                if v is _DROPPED:
                    self.dropped.append("/".join(map(str, sub))[:200])
                else:
                    self.small_left -= min(self._off() - vstart, room)
            else:
                v = self._value(sub, depth + 1, keep=keep, limit=limit)
            if keep and v is not _DROPPED:
                if is_obj:
                    out[key] = v
                else:
                    out.append(v)
            if keep and not on_route and limit is not None and self._off() - start > limit:
                keep, out = False, None                    # over its budget: dropped whole
            c = self._peek()
            if c == 0x2C:
                self.pos += 1
                continue
            if c == close:
                self.pos += 1
                break
            raise LargeRecordError("expected ',' or the end of a container")
        return out if keep else _DROPPED


def _skim(read, rules: dict) -> tuple:
    """(object, info): info is None when nothing was cut or dropped, else
    {"cut": {path: {kept_bytes, total_bytes}}, "dropped": [paths]}, plus
    "incomplete" when `read()` said the record stopped being read before its
    end (_Skim.partial)."""
    sk = _Skim(read, rules)
    try:
        obj = sk.parse()
    except _CutShort:
        return sk.partial()
    if not sk.cut and not sk.dropped:
        return obj, None
    return obj, {"cut": {"/".join(k): v for k, v in sk.cut.items()}, "dropped": sk.dropped}


# Large-record parses run on their OWN threads, at most this many at once. A
# parse holds its thread for the whole transfer (it waits on the network, a
# chunk at a time), so on asyncio's default pool a few slow senders could take
# every worker, and aiohttp's DNS resolver runs on that pool too. Measured
# need: one parse per harness run finishing at the same moment; a Host runs a
# handful of variants at once.
LARGE_PARSES_MAX = 4
# A slot is held while the record arrives, and a stream has only an idle
# timeout, so a sender trickling a large record could keep a slot, and with
# four of them every other run's result, waiting. So the TRANSFER of one
# record, from the moment it holds a slot, gets this long (a real terminal
# record, at most tens of MB on the cluster network, arrives in seconds);
# past it the Host stops reading and keeps what arrived (LargeRecordTimeout's
# `partial`). The wait for a slot is not timed: every holder is bounded by
# this, so the queue always moves, and timing the wait charged one record
# for four others' transfers.
LARGE_RECORD_TOTAL_S = 300.0
_LARGE_POOL = None
_LARGE_SEM: "asyncio.Semaphore | None" = None
_LARGE_SEM_LOOP = None


def _large_parse_slots():
    global _LARGE_POOL, _LARGE_SEM, _LARGE_SEM_LOOP
    import concurrent.futures
    if _LARGE_POOL is None:
        _LARGE_POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=LARGE_PARSES_MAX, thread_name_prefix="large-record")
    loop = asyncio.get_running_loop()
    if _LARGE_SEM is None or _LARGE_SEM_LOOP is not loop:
        _LARGE_SEM, _LARGE_SEM_LOOP = asyncio.Semaphore(LARGE_PARSES_MAX), loop
    return _LARGE_POOL, _LARGE_SEM


def skim_bytes(data, rules: dict) -> tuple:
    """_skim over bytes already in memory (a record whose structure is over
    the JSON value budget): the same kept fields, a bounded number of values.
    Synchronous; callers run it in a worker thread."""
    chunks = [bytes(data)]
    return _skim(lambda: chunks.pop() if chunks else b"", rules)


async def parse_large_record(first, rest, rules: dict, what: str) -> tuple:
    """Parse one JSON object too large to read whole: `first` is what has
    been read of it already, `rest` an async iterator of its remaining bytes
    (it ends where the record ends). Returns (object, info), info as _skim
    gives it (None when nothing was cut); raises HarnessInputRefused when the
    record is not a JSON object.

    The parse runs in a worker thread fed through a queue of _QUEUE_CHUNKS
    chunks, so the Host holds a few chunks and what `rules` keep, never the
    record, and the event loop only moves chunks. At most LARGE_PARSES_MAX
    run at once, on their own pool (_large_parse_slots); a caller past that
    waits for a slot before it reads on, untimed. `rest` is read to its end,
    even after a parse error, so a caller framing a stream stays on a record
    boundary, unless the record is still arriving LARGE_RECORD_TOTAL_S after
    it got its slot: then LargeRecordTimeout, carrying what arrived."""
    import queue as _queue
    import threading
    pool, sem = _large_parse_slots()
    async with sem:
        return await _parse_large_record(pool, first, rest, rules, what, _queue, threading)


async def _parse_large_record(pool, first, rest, rules, what, _queue, threading) -> tuple:
    q: _queue.Queue = _queue.Queue(maxsize=_QUEUE_CHUNKS)
    stop = threading.Event()
    short = threading.Event()        # stopped before the record's end

    def read():
        while True:
            try:
                return q.get(timeout=0.1)
            except _queue.Empty:
                if stop.is_set():
                    return None if short.is_set() else b""

    fut = asyncio.get_running_loop().run_in_executor(pool, _skim, read, rules)
    # read here below; also retrieved if this coroutine is cancelled first
    fut.add_done_callback(lambda f: f.cancelled() or f.exception())

    async def put(chunk) -> None:
        while not fut.done():
            try:
                q.put_nowait(chunk)
                return
            except _queue.Full:
                await asyncio.sleep(0.002)

    # Timed from here, holding the slot: the transfer, never the queue.
    budget = asyncio.timeout(LARGE_RECORD_TOTAL_S)
    try:
        async with budget:
            await put(bytes(first))
            async for chunk in rest:
                if chunk:
                    await put(chunk)
            await put(b"")
    except TimeoutError:
        if not budget.expired():
            raise        # another timeout (aiohttp's idle or total): its own error, not ours
        short.set()
    finally:
        stop.set()
    if short.is_set():
        # The parser reads what is queued, then hands back what it has.
        try:
            obj, info = await fut
        except LargeRecordError:
            obj = info = None
        if isinstance(obj, dict) and isinstance(info, dict):
            info["incomplete"]["after_s"] = LARGE_RECORD_TOTAL_S
            partial = (obj, info)
        else:
            partial = None
        raise LargeRecordTimeout(
            f"{what} refused: a record over {RECORD_INLINE_MAX} bytes was still arriving "
            f"after {LARGE_RECORD_TOTAL_S:g} s (the Host reads one large record for at most "
            f"that long, so a slow sender cannot hold one of its {LARGE_PARSES_MAX} parse slots)",
            partial)
    try:
        return await fut
    except LargeRecordError as exc:
        raise _refuse(what, f"it is over {RECORD_INLINE_MAX} bytes and not a JSON "
                            f"object the Host can read: {exc}") from None


async def read_record(resp, rules: dict, what: str) -> tuple:
    """A JSON object body from `resp`, of ANY size: read whole up to
    RECORD_INLINE_MAX and parsed as parse_json does, else parsed as it
    arrives (parse_large_record). Returns (object, info), info None unless
    something was cut or dropped.

    The record read here is a finished run's result (the buffered /run
    answer, sent once the run has ended). When it is still arriving past
    LARGE_RECORD_TOTAL_S and what arrived already says how the run ended
    ("ok", which leads the record), that part is returned with
    info["incomplete"] saying where it stopped: refusing it would record a
    finished build as failed over a Host-side read limit."""
    it = resp.content.iter_chunked(READ_CHUNK).__aiter__()
    buf = bytearray()
    mark = READ_YIELD
    async for chunk in it:
        buf += chunk
        if len(buf) > RECORD_INLINE_MAX:
            async def rest():
                async for c in it:
                    yield c
            try:
                return await parse_large_record(buf, rest(), rules, what)
            except LargeRecordTimeout as exc:
                if exc.partial is not None and "ok" in exc.partial[0]:
                    return exc.partial
                raise
        if len(buf) >= mark:
            mark += READ_YIELD
            await asyncio.sleep(0)
    if len(buf) > INLINE_JSON_MAX and await asyncio.to_thread(json_nodes, buf, JSON_MAX_NODES) > JSON_MAX_NODES:
        # more values than the budget: walk it, keeping a bounded number of
        # them (the run's own fields among them), rather than refuse the run
        try:
            obj, info = await asyncio.to_thread(skim_bytes, buf, rules)
        except LargeRecordError as exc:
            raise _refuse(what, f"not a JSON object ({exc})") from None
        return obj, info
    return await parse_json(buf, what), None


# ── tar ──────────────────────────────────────────────────────────────────────

import contextlib as _contextlib
import threading as _threading

_GC_PAUSE_LOCK = _threading.Lock()
_GC_PAUSES = 0
_GC_ASIDE_OPEN = 0


@_contextlib.contextmanager
def gc_paused():
    """Pause the cyclic garbage collector while a worker thread parses a
    large tar. Parsing 200,000 members allocates over a million objects, and
    a full collection over them holds the GIL in one piece: measured on the
    Host's Python 3.14.7, the event loop stalled 84-107 ms with the collector
    on and 25 ms with it paused. gc is process-wide, so pauses are counted:
    the collector comes back when the LAST concurrent parse ends, and always
    comes back (finally). Reference counting still frees everything acyclic
    in the meantime; cycles wait the few seconds a parse takes. If any
    gc_set_aside() is open when the collector comes back, what is alive is
    set aside from it first (gc.freeze)."""
    import gc
    global _GC_PAUSES
    with _GC_PAUSE_LOCK:
        _GC_PAUSES += 1
        if _GC_PAUSES == 1:
            was_enabled = gc.isenabled()
            gc.disable()
            gc_paused._was_enabled = was_enabled
    try:
        yield
    finally:
        with _GC_PAUSE_LOCK:
            _GC_PAUSES -= 1
            if _GC_PAUSES == 0 and getattr(gc_paused, "_was_enabled", True):
                if _GC_ASIDE_OPEN:
                    gc.freeze()
                gc.enable()


@_contextlib.contextmanager
def gc_set_aside():
    """For the whole of a tar phase that parses under gc_paused() and then
    keeps its members in use (extraction): when the collector comes back
    from the pause, what is alive is first set aside from it (gc.freeze), so
    its first collection does not walk the members still in use. That walk
    held the GIL 33-38 ms at 200,000 members on a quiet box, and up to
    92 ms under load. Let the members go (release_members) before this
    closes.

    Counted, like the pause: the freeze happens at the end of any pause
    while at least one set-aside is open, and what is set aside is handed
    back (gc.unfreeze: a list splice, no collection) when the LAST open one
    closes, so nothing stays frozen once every tar phase has ended, and one
    phase ending never hands back another's members. Nothing set aside is
    lost meanwhile: reference counting frees it as usual, and the collector
    gets the rest back in its oldest generation."""
    import gc
    global _GC_ASIDE_OPEN
    with _GC_PAUSE_LOCK:
        _GC_ASIDE_OPEN += 1
    try:
        yield
    finally:
        with _GC_PAUSE_LOCK:
            _GC_ASIDE_OPEN -= 1
            if _GC_ASIDE_OPEN == 0:
                gc.unfreeze()


# How many tar members release_members lets go of in one statement.
RELEASE_STEP = 2048


def release_members(*lists) -> None:
    """Empty each list of a tar's members (TarInfo objects) a slice at a
    time. Freeing 200,000 members in one piece, when the TarFile or the
    last list holding them goes, is one C-level cascade that holds the GIL
    throughout: 20-28 ms on a quiet box and 64-90 ms
    under load. A slice of RELEASE_STEP frees in about a millisecond, and
    the GIL can pass between slices. Only the LAST list to hold a member
    frees it, so every list holding the members is passed, the TarFile's
    own `members` last."""
    for lst in lists:
        if isinstance(lst, list):
            while lst:
                del lst[-RELEASE_STEP:]


_COMPRESSED_MAGIC = (b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd")
# POSIX member types tarfile understands and a directory walk can produce:
# regular (0, NUL, and 7 contiguous), hard link 1, symlink 2, char 3, block 4,
# dir 5, FIFO 6. Extension records handled here: pax 'x', GNU 'L' / 'K'.
# Everything else is refused: pax global 'g' and Solaris 'X' (the sidecar
# never writes one), GNU sparse 'S', volume 'V', multivolume 'M', old long
# name 'N', dump 'D', and any vendor type (tarfile reads an unknown type as a
# regular file; nothing here needs one).
_MEMBER_TYPES = frozenset(b"01234567\0")
_REGULAR_TYPES = frozenset(b"07\0")          # tarfile REGULAR_TYPES minus 'S'
_EXT_TYPES = frozenset(b"xLK")
# The only pax keys accepted, each with the longest value a real export
# carries. tarfile.add in PAX format (what every sidecar export is) writes
# path / linkpath for a name that is long or not ASCII, uname / gname likewise,
# uid / gid past the ustar field, mtime for EVERY member (a float), and
# hdrcharset for a name that is not valid UTF-8. Every other key is refused,
# which covers the two that change how tarfile LAYS OUT the archive: `size`
# (overrides the header's, so the next header moves) and GNU.sparse.* (turns
# on sparse handling). Refusing unknown keys also bounds what tarfile KEEPS:
# it stores every key of a block in each member's pax_headers dict, so a
# block of thousands of distinct tiny keys (`6 a1=\n`...) inflated ~20x.
# (Harness tars are opened with lean_tarinfo(), which drops that dict.) It
# never writes atime or ctime, so neither is accepted.
_PAX_KEYS = {
    b"path": TAR_PATH_MAX, b"linkpath": TAR_PATH_MAX,
    b"uname": 64, b"gname": 64,         # tarfile.add: only past ustar's 32 or not ASCII
    b"uid": 32, b"gid": 32,
    b"mtime": 64,
    b"hdrcharset": 64,
}
# tarfile's own pax record prefix (tarfile._header_length_prefix_re): 1-20
# ASCII digits and a space, matched AT the record start (no sign, no
# whitespace first, which int() alone would accept).
_PAX_LENGTH = re.compile(rb"([0-9]{1,20}) ")
# The only number form tarfile.add writes in a header field: octal digits,
# padded with spaces, ended by NUL. tarfile's reader (nti) is looser: int()
# also takes a sign, underscores and a 0o prefix, and a first byte of 0x80 /
# 0xff switches to base-256. Anything but the written form is refused, so
# every number read here is the number tarfile reads; a negative size, which
# nti returns, would otherwise seek this scan BACKWARDS.
_NUMBER = re.compile(rb" *[0-7]* *")
# The header fields tarfile parses with nti (tarfile.TarInfo._frombuf): mode,
# uid, gid, size, mtime, chksum, devmajor, devminor. One it cannot parse makes
# tarfile end the archive there (next() returns None mid-tar), so each is
# checked: a tar it would cut short is refused rather than scanned past.
_NUMBER_FIELDS = ((100, 108), (108, 116), (116, 124), (124, 136), (136, 148),
                  (148, 156), (329, 337), (337, 345))


def _number(field: bytes, what: str) -> int:
    s = field.split(b"\0", 1)[0]
    if not _NUMBER.fullmatch(s):
        raise _refuse(what, "a tar header number not in octal digits (a sign, "
                            "base-256 or other text)")
    s = s.strip(b" ")
    return int(s, 8) if s else 0


_octal = _number                     # the name earlier callers and gates use


def _block(n: int) -> int:
    """tarfile.TarInfo._block: `n` rounded up to whole 512-byte blocks."""
    return (n + 511) // 512 * 512


_HIGH_BYTES = bytes(range(128, 256))


def _checksum_ok(hdr: bytes, want: int) -> bool:
    """tarfile's rule (calc_chksums): the header summed with its checksum
    field as eight spaces, unsigned or signed. C-level sums, no Python loop
    per byte: a 400,000-header tar is scanned in well under a second."""
    field = hdr[148:156]
    unsigned = sum(hdr) - sum(field) + 256
    if want == unsigned:
        return True
    high = len(hdr) - len(hdr.translate(None, _HIGH_BYTES))
    high -= len(field) - len(field.translate(None, _HIGH_BYTES))
    return want == unsigned - 256 * high


def _name_cost(raw: bytes, prefix: bytes = b"") -> int:
    """What a member name costs against the name budget: its bytes in the
    tar, or the bytes Python holds for it once tarfile has decoded it
    (UTF-8, surrogateescape), whichever is larger. A str is stored at one
    width for all its characters, 1, 2 or 4 bytes, set by its widest one,
    so a single emoji in a name of ASCII makes every character cost 4 bytes
    (measured: names of that shape filling the byte budget held
    246 MiB for 200,000 members, against 112 MiB for ASCII ones). A ustar
    name is tarfile's `prefix + "/" + name`, each part decoded on its own,
    as tarfile does. ASCII, what every real export's names almost all are,
    costs its length and is not decoded."""
    n = len(raw) + (len(prefix) + 1 if prefix else 0)
    if raw.isascii() and prefix.isascii():
        return n
    chars, top = (1 if prefix else 0), 0
    for part in ((prefix, raw) if prefix else (raw,)):
        text = part.decode("utf-8", "surrogateescape")
        chars += len(text)
        if text:
            top = max(top, ord(max(text)))
    width = 1 if top < 0x100 else 2 if top < 0x10000 else 4
    return max(n, chars * width)


def _pax_records(buf: bytes, what: str) -> tuple:
    """Parse one pax block EXACTLY as tarfile does (TarInfo._proc_pax): the
    whole padded block it reads, record after record, until the end of the
    block or a NUL where the next record would start. A record in the padding
    therefore counts: tarfile applies it. Returns ({b"path"|b"linkpath":
    value length}, bytes of records parsed). Refused: a record tarfile
    rejects (so a tar is never scanned past a point where tarfile stops), a
    key not in _PAX_KEYS, or a value over its limit."""
    pos, n, names = 0, len(buf), {}
    while n > pos and buf[pos] != 0:
        m = _PAX_LENGTH.match(buf, pos)
        if not m:
            raise _refuse(what, "malformed pax record")
        length = int(m.group(1))
        if length < 5 or pos + length > n:
            raise _refuse(what, "malformed pax record length")
        end = m.start(1) + length - 1
        key, eq, value = buf[m.end(1) + 1:end].partition(b"=")
        if not key or eq != b"=" or buf[end] != 0x0A:
            raise _refuse(what, "malformed pax record")
        limit = _PAX_KEYS.get(key)
        if limit is None:
            raise _refuse(what, f"pax key {key[:64].decode('ascii', 'replace')!r} is not accepted")
        if len(value) > limit:
            raise _refuse(what, f"a path longer than {limit} bytes" if limit == TAR_PATH_MAX
                          else f"pax {key.decode()} of {len(value)} bytes (the limit is {limit})")
        if key in (b"path", b"linkpath"):
            names[key] = _name_cost(value)
        pos += length
    return names, pos


def _lean_tarinfo_class():
    import tarfile

    class LeanTarInfo(tarfile.TarInfo):
        """A TarInfo that does not KEEP its pax records. tarfile applies a
        member's pax records to its attributes (path, linkpath, uname, gname,
        uid, gid, mtime) and then stores a copy of the whole record dict on
        every member (`pax_headers`), with a fresh key string per record:
        about 350 bytes a member for tarfile.add's one mtime record, and 2 KB
        for a hostile member carrying every allowed key (measured: 288 MiB for
        150,000 of them). Nothing reading a harness tar uses pax_headers once
        the attributes are set (extraction and the data filter read the
        attributes), so it is dropped. __slots__ = (): a subclass without it
        would give every member a __dict__."""
        __slots__ = ()

        def _apply_pax_info(self, pax_headers, encoding, errors):
            super()._apply_pax_info(pax_headers, encoding, errors)
            # Called for EVERY member (tarfile applies its global headers to
            # each), so this is where a member is made small. Kept: what
            # extraction reads through filter="data" (names, type, size,
            # mode, mtime, offsets). Dropped: the pax dict (one shared empty
            # one), the header checksum, and uid / gid / uname / gname, which
            # the data filter clears on every member before anything uses
            # them (a hostile tar gave each member two unique 64-byte names).
            self.pax_headers = _NO_PAX
            self.chksum = 0
            self.uname = self.gname = ""
            self.uid = self.gid = 0          # cleared by the data filter too (a pax
                                             # uid of 32 digits was a 40-byte int each)
            # Header numbers past 256 are an int object each (28-32 bytes):
            # 24 MiB for 200,000 members with a large mode, size, devmajor
            # and devminor (measured). The mode is kept as one shared int
            # per permission value: the data filter keeps only `mode & 0o755`
            # of it, and tarfile.add writes only the 0o7777 bits. The device
            # numbers are read only to create a device, so a member that is
            # not one keeps zeros (a device is refused by the data filter,
            # and dropped by the collect-out check, before that).
            self.mode = _SMALL_INTS[self.mode & 0o7777]
            if not (self.ischr() or self.isblk()):
                self.devmajor = self.devminor = 0

    return LeanTarInfo


_LEAN_TARINFO = None
_NO_PAX: dict = {}                           # shared; tarfile never writes to a member's
_SMALL_INTS = tuple(range(0o10000))          # one int object per mode, not one per member


def lean_tarinfo():
    """The TarInfo class to open every harness tar with
    (`tarfile.open(..., tarinfo=lean_tarinfo())`)."""
    global _LEAN_TARINFO
    if _LEAN_TARINFO is None:
        _LEAN_TARINFO = _lean_tarinfo_class()
    return _LEAN_TARINFO


def prescan_tar(fh, what: str, *, max_members: int, max_data: int,
                max_ext_record: int = TAR_EXT_RECORD_MAX,
                max_ext_total: int = TAR_EXT_TOTAL_MAX,
                max_names: int = TAR_NAME_TOTAL_MAX,
                layout: list | None = None) -> dict:
    """Walk a tar's raw 512-byte headers BEFORE tarfile does, reading them
    EXACTLY as tarfile's reader will (Python 3.14 tarfile.TarInfo._frombuf,
    _proc_member and next()), and refuse it (HarnessInputRefused) on anything
    that would cost unbounded memory, that no sidecar export writes, or that
    tarfile itself would read differently or stop on. Reads the file
    sequentially and seeks over member data; holds one 512-byte block plus,
    for an extension record, its padded block (at most `max_ext_record` +
    511 bytes), so the scan itself is bounded whatever the file holds.

    Parity with tarfile, rule by rule:
      * numbers: only the octal form tarfile.add writes (_number); tarfile
        accepts more, so a field it would parse differently is refused;
      * checksum: tarfile's unsigned-or-signed rule; a bad one is refused
        (tarfile ends the archive there, or raises on the first header);
      * a type-NUL header whose name ends in '/' is refused: tarfile reads it
        as a DIRECTORY with no data (the v7 rule, tarfile's dircheck), so its
        "data" would be parsed as headers; tarfile.add never writes one;
      * pax 'x': parsed over the whole padded block as tarfile does
        (_pax_records), keys allowlisted (_PAX_KEYS);
      * GNU 'L'/'K': the name is the padded block up to its first NUL, as
        tarfile's nts() takes it;
      * an extension record applies to the next header, which must be a
        member: at most one pax record, or at most one 'L' and one 'K', per
        member (tarfile.add's PAX and GNU layouts). A chain tarfile would
        recurse through, or an extension with no member after it, is refused;
      * data follows a regular member (0, 7, NUL) and nothing else, and a
        member whose data runs past the end of the file is refused (tarfile
        raises on the next read);
      * the end: an all-NUL block or the end of the file, as in tarfile.

    Other refusals: a compressed body (by magic); a truncated header; a member
    type outside _MEMBER_TYPES; an extension record over `max_ext_record`, or
    all of them over `max_ext_total`; more than `max_members` members; names
    (ustar name + prefix + link name, or the pax / GNU names that replace
    them) over `max_names` in total, each at the larger of its bytes and the
    bytes Python will hold for it (_name_cost), or one over TAR_PATH_MAX;
    regular-file data over `max_data`.

    `layout`, when given, receives (offset, offset_data, type, size) per member
    in tarfile's terms (TarInfo.offset is the member's FIRST header, the
    extension record's if it has one), so a test can hold the scan to
    tarfile's reading. Returns the counts it saw (members, data, ext, names)."""
    head = fh.read(8)
    if any(head.startswith(m) for m in _COMPRESSED_MAGIC):
        raise _refuse(what, "a compressed tar (sidecar exports are uncompressed; "
                            "nothing is inflated)")
    fh.seek(0, 2)
    end = fh.tell()
    fh.seek(0)
    if end == 0:
        # tarfile.open raises ReadError("empty file"); an empty sidecar
        # export is still a tar (two zero blocks at least).
        raise _refuse(what, "an empty body (no tar header)")
    members = data = ext = names = 0
    pos = 0
    # Extension records seen since the last member, and what they set. A pax
    # path/linkpath or a GNU 'L'/'K' long name REPLACES the member's ustar
    # name / link name (tarfile applies it to that member), so it is counted
    # instead of the ustar field, never as well.
    chain: list = []
    chain_start = 0
    pending: dict = {}
    while True:
        hdr = fh.read(512)
        if not hdr or hdr.count(0) == 512:
            if chain:
                raise _refuse(what, "an extension record with no member after it")
            break
        if len(hdr) < 512:
            raise _refuse(what, "truncated tar header")
        nums = [_number(hdr[a:b], what) for a, b in _NUMBER_FIELDS]
        if not _checksum_ok(hdr, nums[5]):
            raise _refuse(what, "bad tar header checksum")
        size = nums[3]
        kind = hdr[156]
        name = hdr[0:100].split(b"\0", 1)[0]
        if kind in _EXT_TYPES:
            if kind in chain or (chain and (kind == 0x78 or 0x78 in chain)):
                raise _refuse(what, "chained tar extension records (tarfile.add "
                                    "writes one per member)")
            if size > max_ext_record:
                raise _refuse(what, f"a {chr(kind)!r} extension record of {size} bytes "
                                    f"(the limit is {max_ext_record})")
            padded = _block(size)
            if pos + 512 + padded > end:
                raise _refuse(what, "truncated extension record")
            body = fh.read(padded)
            if kind == 0x78:                                   # 'x'
                got, parsed = _pax_records(body, what)
                pending.update(got)
            else:                                              # 'L' / 'K'
                nul = body.find(b"\0")
                parsed = nul if nul >= 0 else len(body)
                if parsed > TAR_PATH_MAX:
                    raise _refuse(what, f"a path longer than {TAR_PATH_MAX} bytes")
                pending[b"path" if kind == 0x4C else b"linkpath"] = _name_cost(body[:parsed])
            ext += max(size, parsed)
            if ext > max_ext_total:
                raise _refuse(what, f"extension records over {max_ext_total} bytes in total")
            if not chain:
                chain_start = pos
            chain.append(kind)
            pos += 512 + padded
        elif kind in _MEMBER_TYPES:
            if kind == 0 and name.endswith(b"/"):
                raise _refuse(what, "a type-NUL member named like a directory "
                                    "(tarfile reads it as one, with no data)")
            members += 1
            if members > max_members:
                raise _refuse(what, f"more than {max_members} members")
            if b"path" in pending:
                names += pending[b"path"]
            else:
                names += _name_cost(name, hdr[345:500].split(b"\0", 1)[0])
            if b"linkpath" in pending:
                names += pending[b"linkpath"]
            else:
                names += _name_cost(hdr[157:257].split(b"\0", 1)[0])
            offset_data = pos + 512
            nxt = offset_data
            if kind in _REGULAR_TYPES:
                data += size
                if data > max_data:
                    raise _refuse(what, f"member data over {max_data} bytes")
                nxt += _block(size)
                if nxt > end:
                    raise _refuse(what, "member data runs past the end of the tar")
                fh.seek(nxt)
            if layout is not None:
                layout.append((chain_start if chain else pos, offset_data,
                               bytes([kind]), size))
            chain, pending = [], {}
            pos = nxt
        else:
            raise _refuse(what, f"tar member type {chr(kind)!r} is not accepted")
        if names > max_names:
            raise _refuse(what, f"member names over {max_names} bytes in total")
    return {"members": members, "data": data, "ext": ext, "names": names}
