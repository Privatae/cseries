"""Chat attachments in the shape each provider wire accepts.

A file dropped into a chat reaches the model as an Anthropic-native content
block (runtime/session.py builds them from the composer's attachments):

    {"type": "image",    "source": {"type": "base64", "media_type", "data"},
     "_title": <file name, when known>}
    {"type": "document", "source": {"type": "base64", "media_type", "data"},
     "title": <file name, when known>}

plus one private key, `_kept`, saying whether the file is also being stored on
this Host. Private keys never reach a provider.

A picture's name is PRIVATE (`_title`). The Messages API takes `title` on a
document and refuses it on an image ("messages.4.content.6.image.title: Extra
inputs are not permitted"): from 2026-09-22 to 2026-10-12 every picture pasted
into a chat on an Anthropic route was refused, re-dialled with a note in its
place, and the model was told it "does not read pictures" (44 refusals
in one Host's inference_cost_log). The name is still used, in the notes.
A picture a tool hands the Host's turn (runtime/turn_pictures) has the same
shape and takes the same path.

The OpenAI wire (kimi, openai, openrouter, xai, …, and a local
OpenAI-compatible server) has no `image` or `document` part: it takes
`image_url` and `text`. Sent unconverted, those parts were refused and the
whole turn died on every non-Anthropic route (seen on kimi).

So, before a call is dialled:

  documents  OpenAI wire: READ by the host's own readers (server/readers:
             PDF, Office, text) into a text part. Messages wire: a PDF passes
             (the only base64 document Anthropic takes); anything else is
             read to text the same way.
  pictures   only JPEG / PNG / GIF / WEBP travel as pictures — the four both
             wires take. SVG is text (XML) and goes as text. BMP, TIFF, HEIC
             and the rest become a note: the standard library has no codec to
             convert them, and a picture a provider refuses kills the turn.
             On the OpenAI wire a picture becomes an image_url data URI.
  no vision  a model whose catalog row says it reads no images gets a note in
             place of each picture (images_as_notes). A model whose row says
             nothing is sent the picture; if the provider then refuses the
             call, provider_gateway re-dials once with notes that quote the
             provider's refusal (notes_why), so a picture never costs the turn
             and the model is told what actually happened. There is no second
             model to send a picture to: the Host's own model is the one that
             sees.

A document is read ONCE per content (sha256 of its bytes): tool rounds and a
failover re-dial reuse the reading. Reads run off the event loop in the host's
read slots (context_extract._host_read_slots). The caller's messages are never
mutated: a failover may dial the other wire from the originals.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
from collections import OrderedDict

log = logging.getLogger("cseries.mastercode.attachment_parts")

# HOW MUCH OF ONE DOCUMENT GOES INTO THE TURN. 100,000 characters is about
# 25,000 tokens at the usual ~4 characters per token of English — a
# 60-80 page text document. The smallest context windows on the routes in
# use are 128k tokens; the host prompt, memory and history take roughly half,
# so one document of this size still leaves room to answer, and two do not
# crowd everything else out. PER_TURN_CHARS bounds several attachments
# together at the same budget twice over.
PER_DOCUMENT_CHARS = 100_000
PER_TURN_CHARS = 200_000

ANTHROPIC_DOCUMENT_TYPES = frozenset({"application/pdf"})
# The picture types both wires accept (Anthropic's documented four; OpenAI's
# vision input takes the same set).
WIRE_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
TEXT_IMAGE_TYPES = frozenset({"image/svg+xml"})

# Readings by content: (sha256(bytes), media type) -> ("ok", text_prefix,
# total_chars, reader) | ("err", sentence). Bounded by entry count and by the
# characters held; a reading is deterministic, so a hit is exact.
_READ_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_READ_CACHE_MAX = 64
_READ_CACHE_MAX_CHARS = 16 * PER_DOCUMENT_CHARS
READS = {"n": 0}          # readings actually run (the gate counts them)


def _cache_get(key):
    hit = _READ_CACHE.get(key)
    if hit is not None:
        _READ_CACHE.move_to_end(key)
    return hit


def _cache_put(key, value):
    _READ_CACHE[key] = value
    _READ_CACHE.move_to_end(key)
    held = sum(len(v[1]) for v in _READ_CACHE.values() if v[0] == "ok")
    while _READ_CACHE and (len(_READ_CACHE) > _READ_CACHE_MAX or held > _READ_CACHE_MAX_CHARS):
        _, old = _READ_CACHE.popitem(last=False)
        if old[0] == "ok":
            held -= len(old[1])


def _public(part: dict) -> dict:
    return {k: v for k, v in part.items() if not str(k).startswith("_")}


def _parts_of(messages):
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict):
                    yield p


def _has_attachment_parts(messages) -> bool:
    return any(p.get("type") in ("image", "document") for p in _parts_of(messages))


def has_pictures(messages) -> bool:
    return any(p.get("type") in ("image", "image_url") for p in _parts_of(messages))


async def _read_document(data: bytes, media_type: str):
    """(reader, text) or raises docread.Unreadable — off the event loop, in
    the host's read slots."""
    from server.readers import docread
    READS["n"] += 1
    try:
        from server.routes.context_extract import _host_read_slots
        slots = _host_read_slots()
    except Exception:  # noqa: BLE001 — a missing slot helper never blocks a read
        slots = None
    if slots is None:
        return await asyncio.to_thread(docread.read, data, media_type)
    await slots.acquire()
    try:
        job = asyncio.ensure_future(asyncio.to_thread(docread.read, data, media_type))
    except BaseException:
        slots.release()
        raise

    def _done(f):
        slots.release()
        if not f.cancelled():
            f.exception()
    job.add_done_callback(_done)
    return await asyncio.shield(job)


async def _reading(data: bytes, media_type: str) -> tuple:
    key = (hashlib.sha256(data).hexdigest(), media_type)
    hit = _cache_get(key)
    if hit is not None:
        return hit
    from server.readers import docread
    try:
        reader, text = await _read_document(data, media_type)
        text = (text or "").strip()
        value = ("ok", text[:PER_DOCUMENT_CHARS], len(text), reader)
    except docread.Unreadable as e:
        value = ("err", f"could not be read — {e}")
    except Exception as e:  # noqa: BLE001 — never a failed turn over one file
        # NOT cached: an unexpected failure may be one-off, and caching it
        # would call the file unreadable for every later round and turn.
        log.warning("attachment read failed (%s): %s", media_type, e)
        return ("err", f"could not be read ({type(e).__name__})")
    _cache_put(key, value)
    return value


def _kept_phrase(part: dict, name: str) -> str:
    if part.get("_kept"):
        return (f"It is being kept on this Host; once stored, context_ingest "
                f"op='read' file='{name}' opens all of it.")
    return "It was not kept on this Host."


async def _document_as_text(part: dict, budget: dict) -> dict:
    src = part.get("source") or {}
    name = str(part.get("title") or "").strip() or "a document"
    media_type = str(src.get("media_type") or "application/octet-stream").lower()
    label = f"Attached document {name} ({media_type})"
    if src.get("type") != "base64":
        return {"type": "text", "text": f"[{label}: not included — only an uploaded "
                                        f"file's bytes can be read here.]"}
    try:
        data = base64.b64decode(src.get("data") or "", validate=False)
    except (ValueError, TypeError):
        return {"type": "text", "text": f"[{label}: its bytes were not readable.]"}
    got = await _reading(data, media_type)
    if got[0] == "err":
        return {"type": "text", "text": f"[{label}: {got[1]}.]"}
    _, text, total, _reader = got
    if not text:
        return {"type": "text", "text": f"[{label}: holds no text.]"}
    room = max(0, min(PER_DOCUMENT_CHARS, PER_TURN_CHARS - budget["used"]))
    note = ""
    if total > room:
        text = text[:room]
        note = (f"\n[Cut at {room:,} of {total:,} characters; the rest is not in this "
                f"message. {_kept_phrase(part, name)}]")
    budget["used"] += len(text)
    return {"type": "text", "text": f"{label}:\n{text}{note}"}


# Why a picture is a note, when the caller does not say: the catalog row of the
# model answering says it reads no pictures. There is no other model to ask.
NO_VISION_WHY = ("the model answering this turn does not read pictures; a Host "
                 "model that does (its catalog row lists image input) would see it")


def _picture_name(part: dict) -> str:
    # `title` too: a picture built before 2026-10-12 may still carry the name
    # there, and the note is the only place it is used.
    return str(part.get("_title") or part.get("title") or "").strip()


def _picture_note(part: dict, why: str) -> dict:
    src = part.get("source") or {}
    name = _picture_name(part) or "a picture"
    media = src.get("media_type") or "unknown type"
    kept = " It is being kept on this Host." if part.get("_kept") else ""
    return {"type": "text", "text": f"[A picture, {name} ({media}), is not shown here: {why}.{kept}]"}


def _url_note(why: str) -> dict:
    return {"type": "text", "text": f"[A picture is not shown here: {why}.]"}


# ── fitting a picture to what the provider takes ─────────────────────────────
#
# THE NUMBERS ARE THE PROVIDER'S, not ours. Anthropic, "Vision" → "Image limits
# and costs" → "Request limits" (platform.claude.com/docs/en/build-with-claude/
# vision, read 2026-10-12):
#   - "The maximum size per image is 10 MB (base64-encoded) when using the
#     Claude API directly" (5 MB on Bedrock and Google Cloud, which this
#     platform does not route to);
#   - "The maximum dimensions per image are 8000x8000 px", and "If a single
#     API request contains more than 20 images ... resize each image so that
#     neither dimension exceeds 2000 px";
#   - "100 per request on the API, for models with a 200k-token context
#     window. 600 per request on the API, for all other models";
#   - request size limits: "32 MB for standard endpoints".
# And "Resolution and token cost": Claude 4.7 and later see at most a 2576 px
# long edge ("Images larger than either limit are downscaled before
# processing"). So a picture is brought down to that long edge BEFORE it is
# sent — the model would have seen no more — which keeps a pasted 4K screenshot
# from costing the request size, and keeps every picture under the byte limit.
#
# Each request a turn makes re-sends every picture still in it (a pasted one
# rides every round), so these hold for the whole request, not one picture.
# Until 2026-10-12 none of this existed and a picture the provider refused cost
# a refused call AND a re-dial on every round of the turn (36 in one turn,
# measured).
NATIVE_LONG_EDGE = 2576            # the largest long edge current models see
MANY_PICTURES = 20                 # over this many, every picture must fit...
MANY_LONG_EDGE = 2000              # ...within this
MAX_PICTURE_B64 = 10_000_000       # "10 MB (base64-encoded)"; MB read as 10^6, the smaller reading
MAX_PICTURES = 100                 # the documented floor across models (600 on 1M-context ones)
# Of the documented 32 MB request, what the pictures together may take; the
# other 4 MB is for everything else the request carries (the Host's prompt,
# history and tool schemas measure well under 1 MB).
PICTURES_B64_BUDGET = 28_000_000

# Fitted pictures by (sha256 of the original, long edge): a picture rides every
# round, and fitting it is decoding it. Bounded by entries and by bytes held.
# A picture that already fits is cached as its verdict alone (its true type,
# no bytes): the caller's own string is what is sent, so nothing is held twice.
# As many entries as one request may carry pictures: fewer, and a request of
# more is decoded again every round (the oldest is always the next one needed).
_FIT_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()
_FIT_CACHE_MAX = MAX_PICTURES
_FIT_CACHE_MAX_B64 = PICTURES_B64_BUDGET
# The type a picture really is, from its decoded format (never the label it came
# with: a .png that is a JPEG goes as image/jpeg).
_FORMAT_MEDIA = {"PNG": "image/png", "JPEG": "image/jpeg", "MPO": "image/jpeg",
                 "GIF": "image/gif", "WEBP": "image/webp"}
_PILLOW_MISSING_SAID = {"done": False}


def picture_sha(part: dict) -> str | None:
    """The identity of a picture block's bytes (sha256 of its base64), or None.
    A legacy `image_url` picture's is the sha256 of its url (a data: URL
    included), so a refusal of one can be remembered like any other."""
    if part.get("_sha"):
        # A legacy data: URL turned into an image block (_as_image_block)
        # keeps its url's identity, so every lookup agrees on it.
        return str(part["_sha"])
    if part.get("type") == "image_url":
        url = str((part.get("image_url") or {}).get("url") or "")
        return hashlib.sha256(url.encode()).hexdigest() if url else None
    src = part.get("source") or {}
    if part.get("type") != "image" or src.get("type") != "base64" or not src.get("data"):
        return None
    return hashlib.sha256(str(src["data"]).encode()).hexdigest()


def picture_shas(messages) -> set:
    return {s for s in (picture_sha(p) for p in _parts_of(messages)) if s}


def _fit_bytes(data: bytes, media: str, long_edge: int, max_b64: int):
    """The Pillow half (a seam the gates replace): `data` brought to at most
    `long_edge` on its long side and under `max_b64` once encoded, as
    (media_type, bytes), or ("", why) when it cannot be. A picture already
    within both is returned as it came. Runs off the event loop."""
    import io
    from PIL import Image
    try:
        opened = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError:
        # Past TWICE the line below Pillow will not open it at all: the same
        # verdict, not "sent as it came" for the provider to refuse.
        return "", (f"it is too large to prepare on this Host "
                    f"(over {Image.MAX_IMAGE_PIXELS:,} pixels)")
    with opened as im:
        w, h = im.size
        # Pillow's own decompression-bomb line: a picture past it is not
        # decoded on the Host at all (decoding is what costs the memory).
        if w * h > (Image.MAX_IMAGE_PIXELS or w * h + 1):
            return "", (f"{w}x{h} px is too large to prepare on this Host "
                        f"(over {Image.MAX_IMAGE_PIXELS:,} pixels)")
        true_media = _FORMAT_MEDIA.get(im.format or "")
        if true_media and max(w, h) <= long_edge and len(data) * 4 // 3 + 4 <= max_b64:
            # Fits as it is: its bytes go unchanged, under the type they
            # really are (a mislabelled one is refused by the provider).
            return true_media, data
        im.draft("RGB", (long_edge, long_edge))   # JPEG: decode at a reduced scale
        im.seek(0)                                 # an animation: its first frame, as the API uses
        alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
        if im.mode.startswith("I"):
            # 16-bit grey (PNG): Pillow's convert clips at 255 instead of
            # scaling, so a mid-grey picture went as pure white. Scaled to 8
            # bits first; its one transparent grey (tRNS), if any, is dropped.
            im = im.convert("I").point(lambda v: v * (1 / 256)).convert("L")
            alpha = False
        if im.mode in ("RGB", "RGBA", "L") and "transparency" not in im.info:
            # Shrunk where it was decoded, then converted: converting first
            # held a second full-size copy (measured 2026-10-12: a peak of
            # 3.2-3.7x the decoded size; a 1.5 MB 9400x9400 PNG took 1.16 GB).
            im.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
            frame = im.convert("RGBA" if alpha else "RGB")
        else:
            # A palette (resized only by nearest neighbour), CMYK, or a
            # colour-keyed transparency: converted first, then shrunk.
            frame = im.convert("RGBA" if alpha else "RGB")
            frame.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
    tries = ([("image/png", {"format": "PNG", "optimize": True}),
              ("image/webp", {"format": "WEBP", "quality": 90})] if frame.mode == "RGBA" else
             [("image/png", {"format": "PNG", "optimize": True})] if media == "image/png" else [])
    tries += [("image/jpeg", {"format": "JPEG", "quality": 90})] if frame.mode != "RGBA" else []
    for out_media, kw in tries:
        buf = io.BytesIO()
        frame.save(buf, **kw)
        if buf.tell() * 4 // 3 + 4 <= max_b64:
            return out_media, buf.getvalue()
    return "", f"it does not fit the provider's {max_b64 // 1_000_000} MB a picture may be, even at {long_edge} px"


async def _in_read_slot(fn, *args):
    """`fn(*args)` off the event loop in the Host's read slots — the same slots
    a document's reading takes (context_extract._host_read_slots), so pictures
    being fitted for several turns at once never decode past what the Host
    allows for reads. No slot helper: the default thread pool.

    The slot is held by the THREAD, not by this coroutine (as in
    _read_document): a turn cancelled mid-fit stops waiting, but the decode
    runs on, so its slot is freed only when the decode itself ends."""
    try:
        from server.routes.context_extract import _host_read_slots
        slots = _host_read_slots()
    except Exception:  # noqa: BLE001 — a missing slot helper never blocks a fit
        slots = None
    if slots is None:
        return await asyncio.to_thread(fn, *args)
    await slots.acquire()
    try:
        job = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    except BaseException:
        slots.release()
        raise

    def _done(f):
        slots.release()
        if not f.cancelled():
            f.exception()
    job.add_done_callback(_done)
    return await asyncio.shield(job)


async def _fit(part: dict, long_edge: int, max_b64: int) -> dict:
    """`part` with its picture fitted (a new block; the original untouched), or
    a note saying why it could not be. Without Pillow the picture goes as it
    came and the provider's refusal, if any, is quoted (said once in the log)."""
    src = part.get("source") or {}
    if src.get("type") != "base64" or not src.get("data"):
        return part
    key = (picture_sha(part), long_edge)
    hit = _FIT_CACHE.get(key)
    if hit is None:
        raw = "".join(str(src["data"]).split())
        try:
            data = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError):
            return _picture_note(part, "its bytes are not valid base64")
        try:
            out_media, out = await _in_read_slot(
                _fit_bytes, data, str(src.get("media_type") or "").lower(), long_edge, max_b64)
        except ImportError:
            if not _PILLOW_MISSING_SAID["done"]:
                _PILLOW_MISSING_SAID["done"] = True
                log.warning("attachment_parts: Pillow is not installed; pictures go unfitted")
            return part
        except Exception as e:  # noqa: BLE001 — a picture never costs the turn
            # Not readable HERE is not the provider's verdict: it goes as it
            # came, and a refusal, if one comes, is quoted in its note.
            log.info("attachment_parts: picture not fitted (%s: %s); sent as it came",
                     type(e).__name__, e)
            return part
        # (media, None): fits as it came, sent from the caller's own string.
        hit = (out_media, None if out_media and out is data else
               (base64.b64encode(out).decode() if out_media else out))
        _FIT_CACHE[key] = hit
        _FIT_CACHE.move_to_end(key)
        held = sum(len(v[1] or "") for v in _FIT_CACHE.values())
        while len(_FIT_CACHE) > _FIT_CACHE_MAX or held > _FIT_CACHE_MAX_B64:
            _, old = _FIT_CACHE.popitem(last=False)
            held -= len(old[1] or "")
    else:
        _FIT_CACHE.move_to_end(key)
    out_media, out_b64 = hit
    if out_media and out_b64 is None:
        out_b64 = "".join(str(src["data"]).split())
    if not out_media:
        return _picture_note(part, out_b64)
    return {**part, "source": {**src, "media_type": out_media, "data": out_b64}}


async def _image_part(part: dict, wire: str, budget: dict, images_as_notes: bool,
                      notes_why: str | None = None, *, long_edge: int = NATIVE_LONG_EDGE,
                      refused: dict | None = None) -> dict:
    src = part.get("source") or {}
    media = str(src.get("media_type") or "").lower()
    if media in TEXT_IMAGE_TYPES and src.get("type") == "base64":
        # An SVG is XML: its text is what a model can use.
        return await _document_as_text({**part, "source": {**src, "media_type": media}}, budget)
    if src.get("type") == "base64" and media not in WIRE_IMAGE_TYPES:
        return _picture_note(part, f"{media or 'its type'} is not a picture format the "
                                   f"model's provider accepts")
    if images_as_notes:
        return _picture_note(part, notes_why or NO_VISION_WHY)
    sha = picture_sha(part)
    if refused and sha in refused:
        # Refused earlier in this turn: said once more, never sent again
        # (runtime/turn_pictures.note_refused).
        return _picture_note(part, refused[sha])
    part = await _fit(part, long_edge, MAX_PICTURE_B64)
    if part.get("type") == "text":
        return part
    src = part.get("source") or {}
    media = str(src.get("media_type") or media).lower()
    if wire != "openai":
        # The Messages API's image block is type + source and nothing else: a
        # `title` (a picture built before 2026-10-12) is refused with a 400.
        return {k: v for k, v in _public(part).items() if k != "title"}
    if src.get("type") == "base64" and src.get("data"):
        return {"type": "image_url",
                "image_url": {"url": f"data:{media or 'image/jpeg'};base64,{src['data']}"}}
    if src.get("type") == "url" and src.get("url"):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return {"type": "text", "text": "[An attached picture had no data to send.]"}


def _image_url_part(part: dict, images_as_notes: bool, notes_why: str | None = None,
                    refused: dict | None = None) -> dict:
    """A picture already in OpenAI shape (the legacy `images` lane)."""
    url = str((part.get("image_url") or {}).get("url") or "")
    if images_as_notes:
        return _url_note(notes_why or NO_VISION_WHY)
    sha = picture_sha(part)
    if refused and sha in refused:
        # Refused earlier in this turn, as _image_part: said, never sent again.
        return _url_note(refused[sha])
    m = re.match(r"data:([^;,]+)", url)
    if m and m.group(1).lower() not in WIRE_IMAGE_TYPES:
        return {"type": "text", "text": f"[The person attached a {m.group(1)} picture, "
                                        f"a format the model's provider does not accept.]"}
    return part


def _needs_work(p: dict, images_as_notes: bool, refused: dict | None = None) -> bool:
    t = p.get("type")
    if t in ("image", "document"):
        return True
    if t == "image_url":
        url = str((p.get("image_url") or {}).get("url") or "")
        m = re.match(r"data:([^;,]+)", url)
        return (images_as_notes or bool(m and m.group(1).lower() not in WIRE_IMAGE_TYPES)
                or bool(refused and picture_sha(p) in refused))
    return False


# Case-blind: a data: URL's scheme and its `base64` may be written in any case
# (RFC 2397; the WHATWG data: URL parser), and its media type may be left out.
_DATA_URL = re.compile(r"data:([^;,]*)((?:;[^;,]*)*);base64,(.*)", re.DOTALL | re.IGNORECASE)


def _as_image_block(p):
    """A legacy picture given as a data: URL (`image_url`), as the image block
    every other picture is — so it is fitted, counted and held to the request's
    size like them (review: the legacy lane skipped all three). It keeps its
    url's identity (`_sha`, private) for the refusal memory. Anything else is
    returned as it is (an http(s) url is fetched by the provider itself)."""
    if not isinstance(p, dict) or p.get("type") != "image_url":
        return p
    m = _DATA_URL.fullmatch(str((p.get("image_url") or {}).get("url") or ""))
    if not m:
        return p
    # No media type: image/jpeg, as the Messages payload builder has always
    # read one (provider_gateway._blocks_of); fitting relabels it by its bytes.
    media = m.group(1).strip().lower() or "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                         "data": m.group(3)}, "_sha": picture_sha(p)}


def pasted_picture(url: str) -> dict:
    """A picture pasted into the Host's turn as a data: URL (its `images`), as
    the part the turn carries: an image block when its bytes are base64 (its
    identity is its bytes, as any picture the turn builds), else the url as it
    came, for for_wire to send or say what became of it — never dropped."""
    return _public(_as_image_block({"type": "image_url", "image_url": {"url": url}}))


def _legacy_as_blocks(messages: list) -> list:
    """`messages` with each legacy data: URL picture as an image block; the
    list itself when there is none (new dicts otherwise, originals untouched)."""
    if not any(isinstance(p, dict) and p.get("type") == "image_url" and _DATA_URL.fullmatch(
            str((p.get("image_url") or {}).get("url") or "")) for p in _parts_of(messages)):
        return messages
    return [{**m, "content": [_as_image_block(p) for p in m["content"]]}
            if isinstance(m, dict) and isinstance(m.get("content"), list) else m
            for m in messages]


def _sendable(p) -> bool:
    if not isinstance(p, dict) or p.get("type") != "image":
        return False
    src = p.get("source") or {}
    return str(src.get("media_type") or "").lower() in WIRE_IMAGE_TYPES


async def for_wire(messages: list, wire: str, *, images_as_notes: bool = False,
                   notes_why: str | None = None, refused: dict | None = None) -> list:
    """Messages with attachment parts in the shape `wire` ('openai' or
    'anthropic') accepts. images_as_notes replaces every picture with a note;
    notes_why is what the note says happened (the provider's refusal on a
    re-dial), defaulting to "the model reads no pictures". `refused` maps a
    picture's sha256 to why it was refused earlier this turn: such a picture
    is a note from then on. Every picture is fitted to the provider's limits
    (see "fitting a picture" above); on the Messages wire the request's
    pictures together are held to the per-request count and size, the OLDEST
    becoming notes first. Returns the input list itself when there is nothing
    to convert; otherwise new message dicts (originals untouched)."""
    messages = _legacy_as_blocks(messages)
    if not any(_needs_work(p, images_as_notes, refused) for p in _parts_of(messages)):
        return messages
    budget = {"used": 0}
    pics = [] if images_as_notes else [
        p for p in _parts_of(messages) if _sendable(p) and not (refused and picture_sha(p) in refused)]
    long_edge = MANY_LONG_EDGE if len(pics) > MANY_PICTURES else NATIVE_LONG_EDGE
    over = max(0, len(pics) - MAX_PICTURES) if wire == "anthropic" else 0
    too_many = {id(p) for p in pics[:over]}
    out = []
    for m in messages:
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, list) or not any(
                isinstance(p, dict) and _needs_work(p, images_as_notes, refused) for p in c):
            out.append(m)
            continue
        parts = []
        for p in c:
            t = p.get("type") if isinstance(p, dict) else None
            if t == "image" and id(p) in too_many:
                parts.append(_picture_note(p, (
                    f"the provider takes at most {MAX_PICTURES} pictures in one request, "
                    f"and this is one of the oldest of {len(pics)}")))
            elif t == "image":
                parts.append(await _image_part(p, wire, budget, images_as_notes, notes_why,
                                               long_edge=long_edge, refused=refused))
            elif t == "image_url":
                parts.append(_image_url_part(p, images_as_notes, notes_why, refused))
            elif t == "document":
                media = ((p.get("source") or {}).get("media_type") or "").lower()
                if (wire == "anthropic" and media in ANTHROPIC_DOCUMENT_TYPES
                        and (p.get("source") or {}).get("type") == "base64"):
                    parts.append(_public(p))
                else:
                    parts.append(await _document_as_text(p, budget))
            else:
                parts.append(p)
        out.append({**m, "content": parts})
    if wire == "anthropic":
        _hold_request_size(out)
    return out


def _hold_request_size(out: list) -> None:
    """The pictures of one Messages request together within
    PICTURES_B64_BUDGET: the oldest become notes until they fit (in place, on
    the new message dicts for_wire built — never the caller's)."""
    sent = [(mi, pi, len(str((p.get("source") or {}).get("data") or "")))
            for mi, m in enumerate(out) if isinstance(m.get("content"), list)
            for pi, p in enumerate(m["content"])
            if isinstance(p, dict) and p.get("type") == "image"]
    total = sum(n for _, _, n in sent)
    for mi, pi, n in sent:
        if total <= PICTURES_B64_BUDGET:
            break
        old = out[mi]["content"][pi]
        out[mi]["content"][pi] = _picture_note(old, (
            f"the pictures in one request may total {PICTURES_B64_BUDGET // 1_000_000} MB "
            f"of the provider's 32 MB, and this is one of the oldest"))
        total -= n


def model_reads_images(provider: str, model: str):
    """True / False from the catalog row's input modalities; None unknown."""
    try:
        from server.routes.inference import cached_input_modalities
        mods = cached_input_modalities(provider, model)
    except Exception:  # noqa: BLE001 — unknown, never a failure
        return None
    if mods is None:
        return None
    return "image" in mods


def refused_input(result) -> bool:
    """A provider refusal of the REQUEST (a 4xx other than rate limiting) —
    the shape a picture the model cannot take comes back as."""
    if not isinstance(result, dict) or result.get("content") or not result.get("error"):
        return False
    if result.get("lane_unavailable"):
        return False
    # ANCHORED to the status the HTTP-error paths put first ("kimi 400: …",
    # "anthropic 400: …", "Local model error: 400"). A catch-all failure
    # ("… inference failure: … column 412", ":443") is not a refusal, and it
    # may come after text already streamed — re-sending it would show that
    # text twice and bill both generations.
    m = _REFUSAL.match(str(result.get("error")))
    return bool(m) and m.group(1) != "429"


_REFUSAL = re.compile(r"(?:Local model error:|[\w.-]+)\s(4\d\d)\b")


_NAMED_BLOCK = re.compile(r"\bmessages\.(\d+)\.content\.(\d+)\b")


_SHA_TAG = "_refusal_sha"


def refused_picture_shas(result, originals: list, sent: list) -> set | None:
    """The one picture a Messages API refusal names ("messages.4.content.6.image
    .title: …"), as {sha256 of the picture the caller gave}, so only it is
    remembered as refused. An empty set when the block it names is a picture
    with no identity of ours (an image block whose source is a url): none of
    the caller's pictures was the cause. None when the refusal names no
    picture block — then every picture in the request is taken as refused.

    `sent` is what for_wire made of `originals` for the wire (one message per
    message, one part per part); the index in the error is into the payload
    provider_gateway builds from `sent`, so it is rebuilt the same way here,
    from `sent` with each picture TAGGED with its original's sha. The payload
    passes a picture block through as it is (copying it whole at most), so
    the named block carries its own tag: no counting, which a picture the
    payload drops (in an assistant, system or tool message) would shift. A
    legacy `image_url` picture is turned into its block by the payload
    builder itself, here, and tagged with its url's sha: rebuilding the url
    from the block would miss any data: URL whose header carries more than
    the media type."""
    from mastercode.provider_gateway import _anthropic_messages_payload
    m = _NAMED_BLOCK.search(str((result or {}).get("error") or "") if isinstance(result, dict) else "")
    if not m:
        return None
    try:
        tagged: list = []
        for n, sm in enumerate(sent or []):
            om = originals[n] if n < len(originals or []) else None
            sc = sm.get("content") if isinstance(sm, dict) else None
            oc = om.get("content") if isinstance(om, dict) else None
            if not isinstance(sc, list):
                tagged.append(sm)
                continue
            same = isinstance(oc, list) and len(oc) == len(sc)
            parts = []
            for x, sp in enumerate(sc):
                t = sp.get("type") if isinstance(sp, dict) else None
                if t in ("image", "image_url"):
                    op = oc[x] if same else None
                    sha = picture_sha(op) if isinstance(op, dict) else None
                    if t == "image":
                        sp = {**sp, _SHA_TAG: sha}
                    else:
                        # Its block exactly as the payload makes it; a url the
                        # builder cannot send stays as it is, and becomes the
                        # same text note below (never an image it could name).
                        one = _anthropic_messages_payload(
                            [{"role": "user", "content": [sp]}], "model", 0.0, 1, None
                        )["messages"][0]["content"]
                        if len(one) == 1 and one[0].get("type") == "image":
                            sp = {**one[0], _SHA_TAG: sha}
                parts.append(sp)
            tagged.append({**sm, "content": parts})
        payload = _anthropic_messages_payload(tagged, "model", 0.0, 1, None)["messages"]
        block = payload[int(m.group(1))]["content"][int(m.group(2))]
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    if not isinstance(block, dict) or block.get("type") != "image":
        return None
    sha = block.get(_SHA_TAG)
    return {sha} if sha else set()


def refusal_why(result) -> str:
    """What a picture note says when the provider refused the request that
    carried the picture: the provider's own words, so the model reports what
    happened instead of guessing (a Host guessed "over 5 MB" for a
    321 KB picture because the note said it could not read pictures). The
    refusal is also in the Host's failure record, which `diagnose
    area='failures'` reads."""
    err = str((result or {}).get("error") or "").strip() if isinstance(result, dict) else ""
    said = err
    head, _, body = err.partition(":")
    try:
        import json as _json
        doc = _json.loads(body.strip())
        said = f"{head.strip()}: {((doc.get('error') or {}).get('message')) or body.strip()}"
    except (ValueError, TypeError, AttributeError):
        pass
    said = " ".join(said.split())[:300] or "the provider refused it"
    return (f"the provider refused the request that carried it ({said}), so it was "
            f"sent again without the picture; diagnose area='failures' lists each refusal")
