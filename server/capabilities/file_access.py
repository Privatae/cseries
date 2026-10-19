"""
File Access — CEIGAS-governed file I/O for entity capabilities.

Sandboxed to workspace directory only. Used by the relay/bridge
when the entity requests file operations on the user's machine.

Bounds:
  - RANGE reads (offset/length) reach any size and say where the next range
    starts; a whole-file read answers in one
    relay message and stops at that frame (10MB), naming offset/length
  - open_file streams a whole file of any size to a caller that needs it
    whole — the publish lane's bundle
  - Listings page (offset/limit) and report the TRUE total
  - Max write size 50MB
  - Binary support via base64
  - Null byte path rejection
  - Symlink escape protection
"""

import base64
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("privatae.pc.file_access")

# A WHOLE-file read (no offset/length) answers in ONE message, bounded at the
# 10 MiB frame the old relay connection carried (that client has since been
# removed; the bound stays so one answer stays one bounded message) — so past
# that it is refused, naming offset/length, never a silent half. A RANGE read has no size refusal at all: it seeks, reads
# `length` bytes, and says where the next range starts. A caller that needs a
# whole file of any size streams it through open_file (the publish lane's
# bundle) instead of holding a read result.
MAX_READ_BYTES = 10 * 1024 * 1024    # whole-file reads only: the relay frame
MAX_WRITE_BYTES = 50 * 1024 * 1024   # 50MB
# One listing PAGE, not a ceiling: the total is always the real count and
# next_offset says where the next page starts.
MAX_LIST_ENTRIES = 500
WORKSPACE = Path(os.environ.get("CEIGAS_WORKSPACE", "/app/workspace"))


def _validate_path(requested: str, base: Optional[Path] = None) -> tuple[bool, Path, str]:
    """
    Validate and resolve a path within the sandbox root.
    Returns (ok, resolved_path, error_msg).

    `base` overrides the sandbox root (project-file access jails to
    /var/lib/cseries/projects/<id>); None = the CEIGAS workspace.

    Every op takes a base, writes included. It used to be
    read-side only, which is what made the host unable to put a decision, a
    research note or a brief into the project it was working in — the
    containment here was never the reason for that, the caller's refusal was.
    Containment is unchanged: a base still has to be an absolute root the
    resolved path stays inside, and traversal is blocked before resolution.
    """
    root = base if base is not None else WORKSPACE
    if not requested:
        return False, Path(), "path is required"

    if "\x00" in requested:
        return False, Path(), "null byte in path"

    # Block traversal patterns
    parts = Path(requested).parts
    for part in parts:
        if part in ("..", "~") or part.startswith("/"):
            return False, Path(), f"path traversal blocked: {requested}"

    candidate = (root / requested).resolve()
    root_resolved = root.resolve()

    try:
        candidate.relative_to(root_resolved)
        return True, candidate, ""
    except ValueError:
        return False, Path(), f"path resolves outside workspace: {requested}"


def _utf8_window(raw: bytes, at_start: bool, at_eof: bool) -> tuple[int, int]:
    """Bytes to skip at the head and drop at the tail of a byte window so it
    starts and ends on a utf-8 character boundary. A window cut mid-character
    would otherwise decode as an error and fall back to base64."""
    head = 0
    if not at_start:
        while head < min(3, len(raw)) and (raw[head] & 0xC0) == 0x80:
            head += 1
    tail = 0
    if not at_eof and raw:
        # Walk back over at most 3 continuation bytes to the lead byte; drop
        # the partial character when its declared length runs past the end.
        i = len(raw) - 1
        back = 0
        while i > head and back < 3 and (raw[i] & 0xC0) == 0x80:
            i -= 1
            back += 1
        lead = raw[i]
        need = 1 if lead < 0x80 else 2 if lead >> 5 == 0b110 else \
            3 if lead >> 4 == 0b1110 else 4 if lead >> 3 == 0b11110 else 1
        if (len(raw) - i) < need:
            tail = len(raw) - i
    return head, tail


class Refusal(str):
    """An error sentence that refuses the caller's own input (a path outside
    the sandbox, not there, not a file) rather than reporting a failure of
    this module. A str, so every caller reading the error text is unchanged;
    `input_refused` is what a tool reads to file it as the model's outcome
    (tool_call_ledger.refused)."""
    input_refused = True


def open_file(path: str, base: Optional[Path] = None):
    """Open a sandboxed file for STREAMING: (binary file object, size, "")
    or (None, 0, error). The same path rules as every other op (no
    traversal, no absolute path, resolved inside the sandbox). No size
    refusal: the caller reads it in pieces and closes it. This is how a whole
    file of any size leaves the sandbox — the publish lane's bundle went
    through read_file and was refused past 10 MB, though nothing downstream needed that limit.

    A refusal of the path itself comes back as a `Refusal`; an OSError
    opening a file that is there is a plain str (the platform's)."""
    ok, resolved, err = _validate_path(path, base)
    if not ok:
        return None, 0, Refusal(err)
    if not resolved.exists():
        return None, 0, Refusal("file not found")
    if not resolved.is_file():
        return None, 0, Refusal("not a file")
    try:
        fh = open(resolved, "rb")
    except OSError as e:
        return None, 0, f"cannot open: {e.strerror or e}"
    try:
        size = os.fstat(fh.fileno()).st_size
    except OSError as e:
        fh.close()
        return None, 0, f"cannot stat: {e.strerror or e}"
    return fh, size, ""


class ByteOrderUnknown(ValueError):
    """A utf-16 / utf-32 file with no byte-order mark whose order its bytes do
    not show. Refused in words, never decoded the wrong way round."""


def _sniff_order(fh, unit: int):
    """'le' / 'be' for a BOM-less utf-16 (unit 2) or utf-32 (unit 4) file, from
    where its NUL bytes fall in the first 4 KiB, or None when that does not
    tell. Text in the Latin range has a NUL high byte in every unit: after
    the low byte for little-endian, before it for big."""
    pos = fh.tell()
    fh.seek(0)
    head = fh.read(4096)
    fh.seek(pos)
    if len(head) < unit:
        return "le"                       # nothing to decode either way
    return _order_of(head, unit)


def _order_of(head: bytes, unit: int):
    """'le' / 'be' when the NUL bytes in `head` show the order, else None —
    including for a head too short to show anything (the writer keeps an
    order only when it can actually be told)."""
    head = head[:4096]
    head = head[:len(head) - len(head) % unit]
    if not head:
        return None
    units = len(head) // unit
    hi_first = sum(1 for i in range(0, len(head), unit) if head[i] == 0)
    hi_last = sum(1 for i in range(unit - 1, len(head), unit) if head[i] == 0)
    need = max(2, units // 8)
    if hi_last >= need and hi_last >= 2 * hi_first:
        return "le"
    if hi_first >= need and hi_first >= 2 * hi_last:
        return "be"
    return None


def resolve_codec(encoding: str, head: bytes) -> tuple[str, int]:
    """THE codec resolver for every reader — workspace, relay and DCN: (the
    codec that decodes this file's bytes exactly, BOM bytes at its start),
    from the file's first bytes (`head`, up to 4 KiB). utf-16 / utf-32 name no
    byte order; it is read from the file's own BOM, or — with no BOM — from
    where its NUL bytes fall (_order_of); when neither tells,
    ByteOrderUnknown: the caller refuses in words rather than guess (a
    big-endian file read as little-endian was once garbled silently).
    utf-8-sig is utf-8 after its 3-byte BOM. Anything else is its own codec,
    no BOM. LookupError for a name that is no codec."""
    import codecs as _codecs
    name = _codecs.lookup(encoding).name
    if name not in ("utf-16", "utf-32", "utf-8-sig"):
        return name, 0
    lead = head[:4]
    if name == "utf-8-sig":
        return "utf-8", 3 if lead.startswith(_codecs.BOM_UTF8) else 0
    if name == "utf-32":
        if lead.startswith(_codecs.BOM_UTF32_BE):
            return "utf-32-be", 4
        if lead.startswith(_codecs.BOM_UTF32_LE):
            return "utf-32-le", 4
        unit = 4
    else:
        if lead.startswith(_codecs.BOM_UTF16_BE):
            return "utf-16-be", 2
        if lead.startswith(_codecs.BOM_UTF16_LE):
            return "utf-16-le", 2
        unit = 2
    order = "le" if len(head) < unit else _order_of(head, unit)   # nothing to decode either way
    if order is None:
        raise ByteOrderUnknown(
            f"this file is {name} with no byte-order mark, and its bytes do not show "
            f"whether it is little- or big-endian — read it with encoding "
            f"'{name}-le' or '{name}-be'")
    return f"{name}-{order}", 0


def needs_head(encoding) -> bool:
    """Whether the codec for `encoding` depends on the file's first bytes."""
    import codecs as _codecs
    try:
        return _codecs.lookup(str(encoding)).name in ("utf-16", "utf-32", "utf-8-sig")
    except LookupError:
        return False


def _concrete_codec(fh, encoding: str) -> tuple[str, int]:
    """resolve_codec over an open file's first 4 KiB."""
    pos = fh.tell()
    fh.seek(0)
    head = fh.read(4096)
    fh.seek(pos)
    return resolve_codec(encoding, head)


# ── Codecs a page-at-a-time reader cannot use ─────────────────────────────
# Every page is decoded on its own. A codec whose decoder carries state from
# one byte to the next — escape sequences that switch character sets
# (iso2022_*), shift modes (hz, utf-7) — decodes page 2 from its start state
# and gets it wrong with no error. Which codecs those are is MEASURED, never
# listed by hand: a codec is refused when a sample text in it, decoded as two
# pages, differs from the whole, or when its incremental decoder holds state
# at a character boundary. utf-16/utf-32 hold only their byte order, which
# resolve_codec settles from the file's head, so they are read as -le/-be.
_STATE_SAMPLE = ("Plain ASCII line 123, ok.\n" "Ça été déjà — “quotes” € ß\n" "Привет мир\n"
                 "Καλημέρα κόσμε\n" "日本語のテキスト。カタカナ ｶﾀｶﾅ\n" "中文简体繁體字\n"
                 "한국어 텍스트\n" "עברית عربي\n" "ไทย\n" "🙂 x\n") * 2


def _encodes(ch: str, name: str) -> bool:
    try:
        ch.encode(name)
        return True
    except (UnicodeError, ValueError, LookupError, TypeError):
        return False


_PAGE_CODEC_VERDICT: dict = {}


def _page_safety(name: str) -> str:
    """'ok', 'not-text', 'stateful' or 'untestable' for a canonical codec name."""
    if name in _PAGE_CODEC_VERDICT:
        return _PAGE_CODEC_VERDICT[name]
    import codecs as _codecs
    info = _codecs.lookup(name)
    verdict = "ok"
    if getattr(info, "_is_text_encoding", True) is False:
        verdict = "not-text"
    elif name not in ("utf-16", "utf-32", "utf-8-sig"):
        text = "".join(ch for ch in _STATE_SAMPLE if _encodes(ch, name))
        try:
            data = text.encode(name)
            whole = data.decode(name)
        except (UnicodeError, ValueError, TypeError):
            verdict = "untestable"
        else:
            for k in range(1, len(data)):
                try:
                    a, b = data[:k].decode(name), data[k:].decode(name)
                except (UnicodeError, ValueError):
                    continue
                if a + b != whole:
                    verdict = "stateful"
                    break
            if verdict == "ok":
                try:
                    dec = _codecs.getincrementaldecoder(name)()
                    start_flag = dec.getstate()[1]
                    for k in range(len(data)):
                        dec.decode(data[k:k + 1])
                        buf, flag = dec.getstate()
                        if not buf and flag != start_flag:
                            verdict = "stateful"
                            break
                except (UnicodeError, ValueError, TypeError, AttributeError):
                    verdict = "untestable"
    _PAGE_CODEC_VERDICT[name] = verdict
    return verdict


def codec_refusal(encoding) -> Optional[str]:
    """Why `encoding` cannot be read a page at a time, in words — or None.
    'base64' and 'binary' are bytes, not a codec, and always fine."""
    import codecs as _codecs
    raw = str(encoding or "utf-8").strip()
    if raw.lower() in ("base64", "binary"):
        return None
    try:
        name = _codecs.lookup(raw).name
    except LookupError:
        return (f"unknown encoding {raw!r}: use utf-8 (default), latin-1, cp1252, another "
                "Python codec name, or base64")
    verdict = _page_safety(name)
    if verdict == "ok":
        return None
    if verdict == "not-text":
        return (f"{raw!r} is not a text encoding (it turns bytes into bytes); read the file "
                "as base64 and decode it in one piece")
    return (f"{raw!r} carries state from one part of a file to the next ({name}: escape "
            "sequences or shift modes), so a page read on its own would decode wrong without "
            "any error. Read it with encoding='base64' (exact bytes) and decode the whole file "
            "in one piece, or convert the file to utf-8")


def _unit(codec: str) -> int:
    return 2 if codec.startswith("utf-16") else 4 if codec.startswith("utf-32") else 1


def _partial_tail(codec: str, data: bytes, at_eof: bool) -> int:
    """Bytes at the END of `data` that are only part of a character (left for
    the next page, which starts exactly on them). Never anything at the head:
    a page starts where the previous one stopped, byte for byte."""
    if codec == "utf-8":
        return _utf8_window(data, True, at_eof)[1]
    unit = _unit(codec)
    if unit == 1:
        return 0
    tail = len(data) % unit
    if unit == 2 and not at_eof and len(data) - tail >= 2:
        # A high surrogate as the last unit is half a character.
        end = len(data) - tail
        v = int.from_bytes(data[end - 2:end], "little" if codec.endswith("le") else "big")
        if 0xD800 <= v <= 0xDBFF:
            tail += 2
    return tail


def _decode_page(fh, codec: str, bom: int, raw: bytes, start: int, size: int):
    """ONE page for the whole fetched range. Returns
    (kind, data, skipped, consumed): kind 'text' with the decoded string, or
    'b64' with raw bytes; `skipped` is the BOM at the start of the file and
    nothing else, `consumed` the bytes after it.
      * text when every byte of the range — less a character split by the
        range's end, left for the next page — decodes in `codec`;
      * otherwise the WHOLE range as base64 (ending on a character / unit
        boundary unless the file ends), never a stretch of it, so what a page
        fetches is what it delivers whatever the file's shape (one bad byte per
        4 KB, bursts of bad lines, binary blobs). latin-1 and cp1252 read any
        byte as text, and the reader says so.
    It always moves forward and never skips a byte, so the pages of a file
    joined are the file."""
    head = bom if (start == 0 and bom) else 0
    unit = _unit(codec)
    for _ in range(6):
        at_eof = start + len(raw) >= size
        if unit > 1 and start % unit:
            # Mid-unit (a caller's own offset): the rest of that unit, as bytes.
            need = unit - start % unit
            while len(raw) < need:
                more = fh.read(need - len(raw))
                if not more:
                    break
                raw += more
            chunk = raw[:need]
            return "b64", chunk, 0, len(chunk)
        data = raw[head:]
        tail = _partial_tail(codec, data, at_eof)
        body = data[:len(data) - tail]
        if not body:
            if at_eof:
                if not data:                      # nothing but a BOM, or at the end
                    return "text", "", head, 0
                return "b64", data, head, len(data)
            more = fh.read(4)
            if not more:
                size = start + len(raw)           # the file shrank under us
                continue
            raw += more
            continue
        try:
            return "text", body.decode(codec), head, len(body)
        except UnicodeDecodeError as e:
            if not at_eof and unit == 1 and e.end >= len(body):
                if e.start == 0:
                    # One character cut by the range's end is all there is (a
                    # 1-byte range of shift_jis): read on to finish it.
                    more = fh.read(4)
                    if more:
                        raw += more
                        continue
                elif codec != "utf-8" and e.start >= len(body) - 3:
                    # A multi-byte legacy codec (shift_jis, gb18030...) split
                    # by the range's end: the characters before it are text.
                    try:
                        return "text", body[:e.start].decode(codec), head, e.start
                    except UnicodeDecodeError:
                        pass
            chunk = data if at_eof else body
            if not at_eof and unit > 1:
                chunk = chunk[: len(chunk) - (start + head + len(chunk)) % unit] or data
            return "b64", chunk, head, len(chunk)
    data = raw[head:]
    return "b64", data, head, len(data)


# What a base64 page of a text read tells the reader.
_CP1252_UNDEFINED = frozenset(b"\x81\x8d\x8f\x90\x9d")


def _looks_utf32(head: bytes) -> bool:
    """UTF-32 text below U+10000 has a NUL PAIR in every 4-byte unit (the
    high half); UTF-16's NULs come singly, so _order_of(head, 4) alone would
    take UTF-16 for UTF-32."""
    n = len(head) // 4
    if n < 2:
        return False
    pairs = sum(1 for i in range(0, n * 4, 4)
                if head[i:i + 2] == b"\0\0" or head[i + 2:i + 4] == b"\0\0")
    return pairs * 2 >= n


def base64_note(raw: bytes, codec, at_file_start: bool = False) -> str:
    """What a base64 page of a text read tells the reader: only encodings
    that WILL decode these bytes, never the one that just failed — latin-1
    decodes any byte; cp1252 only a page without its five undefined bytes —
    and, when the bytes look like UTF-16/32 (a BOM, or the NUL pattern),
    that."""
    import codecs as _codecs
    try:
        used = _codecs.lookup(str(codec)).name if codec else None
    except LookupError:
        used = None
    tips = []
    head = raw[:4096]
    wide = None
    if at_file_start and head.startswith((_codecs.BOM_UTF32_LE, _codecs.BOM_UTF32_BE)):
        wide = "utf-32"
    elif at_file_start and head.startswith((_codecs.BOM_UTF16_LE, _codecs.BOM_UTF16_BE)):
        wide = "utf-16"
    elif _looks_utf32(head):
        wide = "utf-32"
    elif _order_of(head, 2):
        wide = "utf-16"
    if wide and not (used or "").startswith(wide):
        tips.append(f"these bytes look like {wide.upper()} — read them with encoding='{wide}'")
    if used != "latin-1":
        tips.append("encoding='latin-1' reads any byte as text")
    if used != "cp1252" and not (_CP1252_UNDEFINED & set(raw)):
        tips.append("'cp1252' a Windows file")
    lead = f"these bytes are not all valid {codec}, so the page is base64"
    return lead + ("; " + "; ".join(tips) if tips else "")


def decode_bytes_page(raw: bytes, codec: str, start: int, at_eof: bool):
    """_decode_page over bytes already in memory (a DCN range): the same
    invariants — never a skipped byte, always forward — with no read-ahead
    (a partial character at the end of a range that is not the file's end is
    left for the next range). Returns (kind, data, skipped, consumed)."""
    import io as _io
    size = start + len(raw) + (0 if at_eof else 1)
    return _decode_page(_io.BytesIO(b""), codec, 0, raw, start, size)


def _refused(msg: str) -> dict:
    """An error that REFUSES the call's own input — a path that is missing,
    not there or outside the sandbox, a bad range, an encoding or content the
    op cannot take — not a failure of this module. `input_refused` is what
    tool_call_ledger.fault_class reads (INPUT_REFUSED): the error lane files
    it as the caller's outcome, never as a platform fault. An I/O failure
    while doing a valid op is not marked."""
    return {"status": "error", "error": msg, "input_refused": True}


def read_file(path: str, encoding: str = "utf-8",
              base: Optional[Path] = None, offset: Optional[int] = None,
              length: Optional[int] = None) -> dict:
    """Read a file from the sandbox (workspace, or `base` when supplied).

    offset/length (bytes) make it a RANGE read: no size refusal, the file is
    seeked rather than loaded, and the result carries `offset`, `next_offset`
    (None at end of file) and `size` (the whole file's bytes). A text range
    is moved to utf-8 character boundaries, so next_offset is exact."""
    ok, resolved, err = _validate_path(path, base)
    if not ok:
        return _refused(err)

    if not resolved.exists():
        return _refused("file not found")
    if not resolved.is_file():
        return _refused("not a file")

    size = resolved.stat().st_size
    ranged = offset is not None or length is not None
    if not ranged and size > MAX_READ_BYTES:
        return _refused((
            f"file too large to answer whole: {size} bytes, past the "
            f"{MAX_READ_BYTES}-byte message a whole-file read answers in — pass "
            "offset/length to read it in ranges"))

    start = 0
    clamped_from = None
    if ranged:
        try:
            start = max(0, int(offset or 0))
            want = int(length) if length is not None else size - start
        except (TypeError, ValueError):
            return _refused("offset and length must be integers")
        if want <= 0:
            return _refused("length must be positive")
        start = min(start, size)
        # One range holds at most what one answer can carry: a 100 MB
        # `length` read the whole range into memory (351 MB peak) for a
        # result that was then cut to 61 KB. Past MAX_READ_BYTES the range
        # is CLAMPED — never refused — and next_offset continues.
        if want > MAX_READ_BYTES:
            clamped_from, want = want, MAX_READ_BYTES
        why = codec_refusal(encoding) if encoding != "binary" else None
        if why and not why.startswith("unknown encoding"):
            # A RANGE is decoded on its own; a whole-file read below
            # decodes in one piece and may use any codec.
            return _refused(why)
        with open(resolved, "rb") as fh:
            codec, bom = None, 0
            if encoding != "binary":
                try:
                    codec, bom = _concrete_codec(fh, encoding)
                except LookupError:
                    codec = None
                except ByteOrderUnknown as e:
                    return _refused(str(e))
            fh.seek(start)
            raw = fh.read(want)
            page = _decode_page(fh, codec, bom, raw, start, size) if codec else None
    else:
        codec, bom, page = None, 0, None
        with open(resolved, "rb") as fh:
            if encoding != "binary":
                try:
                    codec, bom = _concrete_codec(fh, encoding)
                except LookupError:
                    codec = None
                except ByteOrderUnknown as e:
                    return _refused(str(e))
            raw = fh.read()

    def _range_fields(consumed: int, skipped: int = 0, unit: str = "bytes") -> dict:
        if not ranged:
            return {}
        end = start + skipped + consumed
        out = {"offset": start + skipped, "next_offset": end if end < size else None,
               "size": size, "eof": end >= size, "offset_unit": unit}
        if clamped_from is not None:
            out["clamped"] = (f"asked for {clamped_from} bytes; one range returns at "
                              f"most {MAX_READ_BYTES} — read on from next_offset")
        return out

    if encoding == "binary":
        return {
            "status": "ok",
            "path": path,
            "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii"),
            "size": size if ranged else len(raw),
            **_range_fields(len(raw), unit="b64"),
        }

    # What the file IS, on every page and on a whole read: `codec` is the
    # exact codec its bytes decode with (utf-16-be, not just "utf-16"), and
    # `bom` the bytes of byte-order mark at its start. A base64 first page
    # and a whole read report them too, so the byte order never needs a
    # second read, and a write can keep it (write_file).
    file_kind = {"codec": codec, "bom": bom} if codec else {}
    if ranged and page is not None:
        kind, data, skipped, consumed = page
        if kind == "text":
            # `codec` is also what these offsets count bytes in — the transcript
            # wall uses it to find where a cut page ends.
            return {"status": "ok", "path": path, "encoding": encoding, "content": data,
                    "size": size, **file_kind, **_range_fields(consumed, skipped)}
        return {"status": "ok", "path": path, "encoding": "base64",
                "content": base64.b64encode(data).decode("ascii"),
                "size": size, **file_kind, **_range_fields(consumed, skipped, unit="b64"),
                "note": base64_note(data, codec or encoding, start == 0)}
    try:
        text = raw[bom:].decode(codec) if codec else raw.decode(encoding)
        out = {
            "status": "ok",
            "path": path,
            "encoding": encoding,
            "content": text,
            "size": size if ranged else len(raw),
            **file_kind,
            **_range_fields(len(raw)),
        }
        return out
    except (UnicodeDecodeError, LookupError):
        return {
            "status": "ok",
            "path": path,
            "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii"),
            "size": size if ranged else len(raw),
            **_range_fields(len(raw), unit="b64"),
        }


# The relay's file_read answer crosses the relay as ONE JSON frame, and the
# smallest frame on that path is 5 MiB: the relay end of the tunnel reads with
# websockets max_size=5 MiB (the platform relay's own setting),
# below this pod's own 10 MiB (MAX_READ_BYTES above).
# Base64 makes binary 4/3 and JSON escapes non-ASCII text up to 6x, so a range
# is sized by its ENCODED answer, not its raw bytes (a 10 MiB binary range was ~13.98 MB of JSON).
RELAY_FRAME_BYTES = 5 * 1024 * 1024
_RELAY_ENVELOPE = 64 * 1024     # the relay's own message fields around the answer


def _encoded_len(result: dict) -> int:
    import json as _json
    return len(_json.dumps(result, default=str))


def relay_file_read(p: dict) -> dict:
    """The relay's file_read: one answer that fits the relay frame once
    encoded. A whole-file read that fits answers as before; anything larger
    — a whole file or a range — comes back as the largest range whose encoded
    answer fits, with the true size, next_offset and `clamped` saying so."""
    path = p.get("path", "")
    encoding = p.get("encoding", "utf-8")
    offset, length = p.get("offset"), p.get("length")
    budget = RELAY_FRAME_BYTES - _RELAY_ENVELOPE
    if offset is None and length is None:
        fh, size, err = open_file(path)
        if fh is None:
            return {"status": "error", "error": err}
        fh.close()
        if size * 6 <= budget:          # fits whatever the encoding does to it
            return read_file(path, encoding=encoding)
        offset = 0
    try:
        asked = int(length) if length is not None else None
        want = asked if asked is not None else budget
    except (TypeError, ValueError):
        return {"status": "error", "error": "offset and length must be integers"}
    want = max(1, min(want, budget * 3 // 4))
    while True:
        r = read_file(path, encoding=encoding, offset=offset or 0, length=want)
        if r.get("status") != "ok":
            return r
        n = _encoded_len(r)
        if n <= budget or want <= 1:
            break
        want = max(1, int(want * budget / n * 0.95))
    if (asked is None or asked > want) and r.get("next_offset") is not None:
        r["clamped"] = (f"one relay answer carries at most {RELAY_FRAME_BYTES} bytes "
                        f"encoded; this range is {want} bytes of the file — read on "
                        "from next_offset")
    return r


def _encode_keeping_order(resolved: Path, content: str, encoding: str) -> bytes:
    """`content` in `encoding` — and for utf-16 / utf-32, which name no byte
    order, in the order of the file being overwritten when that order can
    actually be told. Python's own utf-16/32
    encoders write the MACHINE's order with a BOM, so a big-endian file read
    and written back with the encoding it was read with came back
    little-endian.
      * the existing file has a BOM: its BOM and order are kept;
      * it has none but its bytes confidently show the order: that order is
        kept, BOM-less as it was — unless the NEW text would not show it (no
        Latin-range characters), in which case a BOM in that order is written,
        so the file can always be read back with the same encoding;
      * otherwise (a new, empty, 1-byte or undecidable file): what the caller
        asked for — the generic encodings with Python's BOM."""
    import codecs as _codecs
    try:
        name = _codecs.lookup(encoding).name
    except LookupError:
        return content.encode(encoding)            # raises the same LookupError
    if name not in ("utf-16", "utf-32") or not resolved.is_file():
        return content.encode(encoding)
    unit = 2 if name == "utf-16" else 4
    boms = ({"be": _codecs.BOM_UTF16_BE, "le": _codecs.BOM_UTF16_LE} if unit == 2
            else {"be": _codecs.BOM_UTF32_BE, "le": _codecs.BOM_UTF32_LE})
    with open(resolved, "rb") as fh:
        head = fh.read(4096)
    for order in ("be", "le"):
        if head.startswith(boms[order]):
            return boms[order] + content.encode(f"{name}-{order}")
    order = _order_of(head, unit)
    if order is None:
        return content.encode(encoding)
    data = content.encode(f"{name}-{order}")
    if _order_of(data, unit) == order:
        return data                                # BOM-less stays BOM-less
    return boms[order] + data                      # say the order, so it reads back


def write_file(path: str, content: str, encoding: str = "utf-8",
               base: Optional[Path] = None) -> dict:
    """Write a file to the sandbox (workspace, or `base` when supplied)."""
    ok, resolved, err = _validate_path(path, base)
    if not ok:
        return _refused(err)

    if encoding == "base64":
        try:
            data = base64.b64decode(content)
        except Exception:
            return _refused("invalid base64 content")
    else:
        data = _encode_keeping_order(resolved, content, encoding)

    if len(data) > MAX_WRITE_BYTES:
        return _refused(f"content too large: {len(data)} bytes (max {MAX_WRITE_BYTES})")

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(data)

    return {"status": "ok", "path": path, "bytes_written": len(data)}


def list_directory(path: str = "", base: Optional[Path] = None,
                   offset: int = 0, limit: Optional[int] = None) -> dict:
    """List directory contents within the sandbox (workspace, or `base`).

    One page of `limit` entries (default MAX_LIST_ENTRIES) from `offset`, in
    sorted order. `total` is the directory's REAL entry count and
    `next_offset` is where the next page starts (None on the last page) —
    the listing used to stop at 500 and report 500 as the total."""
    if path:
        ok, resolved, err = _validate_path(path, base)
        if not ok:
            return _refused(err)
    else:
        resolved = (base if base is not None else WORKSPACE).resolve()

    if not resolved.exists():
        return _refused("directory not found")
    if not resolved.is_dir():
        return _refused("not a directory")

    try:
        offset = max(0, int(offset or 0))
        limit = MAX_LIST_ENTRIES if limit is None else max(1, int(limit))
    except (TypeError, ValueError):
        return _refused("offset and limit must be integers")

    # Names only for the whole directory (cheap); stat only the page.
    names = sorted(resolved.iterdir())
    total = len(names)
    entries = []
    for item in names[offset:offset + limit]:
        try:
            stat = item.stat()
            entries.append({
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "size": stat.st_size if item.is_file() else 0,
                "modified": stat.st_mtime,
            })
        except (PermissionError, OSError):
            continue

    end = offset + limit
    return {"status": "ok", "path": path or "/", "entries": entries,
            "total": total, "offset": offset,
            "next_offset": end if end < total else None}


def delete_file(path: str, base: Optional[Path] = None) -> dict:
    """Delete a file from the sandbox (workspace, or `base`). Directories not
    supported."""
    ok, resolved, err = _validate_path(path, base)
    if not ok:
        return _refused(err)

    if not resolved.exists():
        return _refused("file not found")
    if resolved.is_dir():
        return _refused("cannot delete directories")

    resolved.unlink()
    return {"status": "ok", "path": path, "deleted": True}
