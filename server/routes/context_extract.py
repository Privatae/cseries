"""Context extraction — a stored file becomes TEXT the host can search and use.

A file the user uploaded is stored whole. This turns it into text and files that
text back into the corpus as ordinary rows, which the text embedding lane then
makes searchable. No new columns, no wire probing, no two-tower gate.

WHO READS IT. A file uploaded to a Host is read BY the Host, with its own
deterministic readers (server/readers: PDF, Office, RTF, email, HTML, text)
— a *read* function, never an agent. That replaced an older rule here ("the
host never parses a PDF"), stretched from a remark that a VIDEO DECODER is
not a platform service.
What the host carries no engine for — OCR of a picture or a scan, speech, video
frames — is a job on one of the user's machines (ctx_extract, below).

WHAT COMES BACK IS TEXT, and it lands as normal rows:
  * chunked with the SAME chunker uploaded text uses (_chunk_text), so an
    extracted page and a pasted page behave identically at search time;
  * metadata.kind is NOT 'file', so NOT_FILE_KIND_SQL includes them and the
    existing embed job picks them up with no changes at all;
  * media_parent_id points at the source row (v135), so the text and the
    document it came from are linked in both directions.

RE-EXTRACTION REPLACES, never appends: a user who runs it twice must not get
their document twice in every search result.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends

from server import brain_service
from server.readers import docread as _docread
from server.applications.middleware import requires_permission

logger = logging.getLogger("privatae.context.extract")

router = APIRouter(prefix="/api/context-extract", tags=["brain", "context"])

# The capability a bastion advertises when it has any extraction tool at all
# (client-go/contextprep.go). The host gates on it so a machine that cannot do
# the work is never offered as if it could.
PREP_CAPABILITY = "context-prep"

# The management op the box answers (client-go/contextextract.go). The "ctx_"
# prefix is load-bearing on the BOX, not here: it is what puts the op in the dcn
# caps family, and opFamily returns "" for a name it does not recognise — at
# which point capsAllow ALLOWS it. Renaming this string without renaming it
# there would silently ungate an extractor on every member pairing.
EXTRACT_OP = "ctx_extract"

# The job name for the host's OWN readers (server/readers) — what `tool` names
# to insist on them, and what extracted_by / extracted_on record.
HOST_TOOL = "host-read"

# Host reads in flight at once. A reader is single-threaded CPU work; two
# leave the pod's other cores (and the event loop) free while a drop of many
# files queues rather than piles up.
HOST_READ_CONCURRENCY = 2
_HOST_READ_SEM = None


def _host_read_slots():
    import asyncio
    global _HOST_READ_SEM
    if _HOST_READ_SEM is None:
        _HOST_READ_SEM = asyncio.Semaphore(HOST_READ_CONCURRENCY)
    return _HOST_READ_SEM

# Mimes whose host read can fall back to a machine job. Only a PDF: a scan is
# pictures of pages, which a machine's OCR can read and the host cannot. An
# Office file the host could not open is malformed, and the machine's reader
# would refuse it the same way.
MACHINE_FALLBACK_MIMES = frozenset({"application/pdf"})

# Which job a mime type needs. A mime with no entry is not extractable yet and is
# said so plainly — never attempted with the wrong tool.
#
# The image formats are the ones this box's own tesseract reports linked support
# for in its version banner — libgif, libjpeg, libpng, libtiff, libwebp — plus
# BMP, which leptonica reads without a helper library. HEIC is deliberately
# ABSENT: nothing in the leptonica stack decodes it, so mapping it here would
# route an iPhone photo to a job that refuses it.
MIME_TO_TOOL = {
    "application/pdf": "pdf-text",
    "image/png": "ocr",
    "image/jpeg": "ocr",
    "image/tiff": "ocr",
    "image/webp": "ocr",
    "image/bmp": "ocr",
    "image/gif": "ocr",
    # Office documents. All three are OPC packages — ZIPs of XML
    # — and the box reads them with its OWN code, no program installed
    # (client-go/contextoffice.go).
    #
    # The LEGACY BINARY formats are deliberately ABSENT: there is no entry for
    # application/msword, application/vnd.ms-excel or
    # application/vnd.ms-powerpoint, because a .doc is an OLE2 compound file
    # rather than a ZIP and nothing on the box reads one. A mime with no entry is
    # told so plainly, which is the right answer; mapping it here would route the
    # file to a job that would refuse it — the same reason HEIC is not mapped to
    # the OCR engine above.
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "office-text",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "office-text",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "office-text",
    # Video (client-go/contextframes.go). Every container here was
    # checked against ffmpeg's own demuxer list, not guessed from the extension.
    "video/mp4": "video-frames",
    "video/quicktime": "video-frames",
    "video/webm": "video-frames",
    "video/x-matroska": "video-frames",
    "video/x-msvideo": "video-frames",
    "video/mpeg": "video-frames",
    "video/x-m4v": "video-frames",
    #
    # NOTE these seven are the video mimes' FALLBACK job, not their default. They
    # also have words in them, so MIME_TOOLS below lists both and puts the
    # transcript first; this map is what a clip falls back to when no machine of
    # the user's can transcribe.
    # ── AUDIO (client-go/contextstt.go) ─────────────────────────────────────
    #
    # The four the engine reads BY ITSELF. whisper-cli's own help prints
    # "supported audio formats: flac, mp3, ogg, wav" and it fails on anything
    # else, so these are the mimes that reach a machine holding the engine alone.
    #
    # audio/mp4 and audio/x-m4a are deliberately NOT here — an m4a is an MPEG-4
    # container and the engine cannot open one. They are in MIME_TOOLS below,
    # routed to the job that unpacks it with ffmpeg first, which is the same
    # reason HEIC is absent from the OCR list above: a mime mapped to a job that
    # would refuse it is worse than a mime with no entry.
    "audio/wav": "transcribe",
    "audio/x-wav": "transcribe",
    "audio/wave": "transcribe",
    "audio/vnd.wave": "transcribe",
    "audio/mpeg": "transcribe",
    "audio/mp3": "transcribe",
    "audio/flac": "transcribe",
    "audio/x-flac": "transcribe",
    "audio/ogg": "transcribe",
    "audio/vorbis": "transcribe",
}

# ── WHEN ONE FILE SUPPORTS MORE THAN ONE JOB ────────────────────────────────
#
# MIME_TO_TOOL is one job per mime, and a clip is the case that does not fit: an
# mp4 has words in it AND pictures in it, and they are two different extractions
# producing two different kinds of row.
#
# THE ORDER IS THE VALUE ORDER: "a clip's audio
# is more searchable than its frames, because someone hunting 'the demo where we
# talked about pricing' wants the transcript, not a keyframe." So the default job
# for a clip is the transcript. Frames stay available and are asked for by name.
#
# The two never collide in the corpus: a transcript is filed under EXTRACT_SOURCE
# and frames under FRAME_SOURCE, both hanging off media_parent_id, so running one
# never deletes the other and a clip can carry both.
#
# A NAME IN HERE IS NOT A PROMISE THAT ANY MACHINE HAS IT. Each entry is filtered
# against what the user's machines actually advertise, so a list that names a job
# no bastion offers simply falls through to the next one.
MIME_TOOLS: dict[str, tuple[str, ...]] = {
    # Containers whose audio track needs unpacking before the engine sees it.
    # Every one of these was checked against ffmpeg's own demuxer list.
    "audio/mp4": ("av-transcribe",),
    "audio/x-m4a": ("av-transcribe",),
    "audio/m4a": ("av-transcribe",),
    "audio/aac": ("av-transcribe",),
    "audio/webm": ("av-transcribe",),
    "audio/x-ms-wma": ("av-transcribe",),
    # Video: the words first, the pictures second.
    "video/mp4": ("av-transcribe", "video-frames"),
    "video/quicktime": ("av-transcribe", "video-frames"),
    "video/webm": ("av-transcribe", "video-frames"),
    "video/x-matroska": ("av-transcribe", "video-frames"),
    "video/x-msvideo": ("av-transcribe", "video-frames"),
    "video/mpeg": ("av-transcribe", "video-frames"),
    "video/x-m4v": ("av-transcribe", "video-frames"),
}

# The jobs that hand back a TRANSCRIPT: text, with a timestamp on every piece of
# it, and the name of the model that heard it.
STT_TOOLS = {"transcribe", "av-transcribe"}

# A spoken-language tag a caller may name for a transcript: an ISO 639 code with
# an optional region ("de", "pt-br"), or the literal "auto" for the engine's own
# detection. Shaped rather than enumerated — whisper.cpp knows ~99 languages and
# a list here would be a second copy of the engine's, going stale the moment it
# gains one. The box re-checks it against the model that will actually run, and
# refuses rather than transcribing English under a foreign label.
_LANG_TAG = re.compile(r"^(auto|[a-z]{2,3}(-[a-z0-9]{2,8})?)$")

# Jobs the BOX does with its own code, with nothing for the user to install.
#
# It changes one sentence and the sentence matters: "install the tool and it
# appears on the next check-in" is false advice for a job that needs no tool. A
# machine that cannot do one of these is running a bastion that PREDATES it, and
# the fix is an update, not a package.
#
# The box says so itself, per job, in its advertisement — prepToolReport.builtin
# — and that is the authority wherever an advertisement exists to read. This set
# is only for the case where NO machine advertises the job at all, which is
# precisely the case with no advertisement to read.
BUILTIN_TOOLS = {"office-text"}

# The jobs whose answer is FRAMES rather than text.
#
# THE REPLY PATH FORKS HERE, and the fork is real rather than cosmetic.
# Everything else in this module is built around text: the box returns a `text`
# field, _chunk_text splits it, the rows land with kind='text' and the TEXT
# embedding lane picks them up with no changes at all. A frame is none of those
# things — it is a picture with a timestamp, it belongs in the media column
# family (migrations/v135), and the text lane must never see it. So a frame job
# gets its own reply reader and its own writer, and the two never share a
# denominator.
FRAME_TOOLS = {"video-frames"}

# ── SCANNED PDFs: the job the host asks for when the machine can do it ──────
#
# A scanned PDF is images wrapped in a PDF. pdftotext succeeds and returns
# NOTHING for one, so a user's scanned contract comes back — from a job that
# exited 0 — as a document with no words on it. That is the single worst
# outcome in this lane, and it is the outcome the default routing produces.
#
# THE HOST ASKS FOR THE BETTER JOB BY NAME when the machine advertises it.
# `pdf-ocr`'s contract on the box is "this PDF's text, reading the pages that
# have none" — its own text layer first, the OCR engine only where that layer is
# empty. So a text PDF costs exactly what pdf-text costs and a scanned one comes
# back as words.
#
# The two alternatives were considered and both lose on something concrete:
#
#   THE BOX SILENTLY UPGRADES pdf-text INTO OCR. The host would be told one job
#   ran while another did, `extracted_by` would record something that did not
#   happen, and the user's machine would spend a hundred times the CPU nobody
#   asked it for. A box doing work it was not asked for is the same dishonesty
#   as a box advertising work it cannot do.
#
#   THE HOST RUNS pdf-text, SEES EMPTY, AND RE-DISPATCHES. The pull ticket is
#   SINGLE-USE and machine-bound (server/network/context_tickets.py), so a second
#   attempt needs a second ticket and a second download of the same document —
#   up to PULL_MAX_BYTES pulled twice across the user's own link to read it once.
#
# Preferring the better job is NOT preferring a better machine in general: if no
# online machine has pdf-ocr, the PDF still goes to pdf-text and the empty result
# is explained with what to install, rather than the request being refused.
TOOL_UPGRADE = {"pdf-text": "pdf-ocr"}

# Mirrors ctxExtractMaxBytes on the box. Checked HERE TOO so an oversize document
# is refused before its bytes are pushed down a socket that will refuse them
# anyway — the box's cap is the wall, this is the courtesy.
#
# THIS IS THE INLINE CAP AND IT IS A TRANSPORT LIMIT, not a product one: a
# management job is ONE WebSocket frame with its payload base64 in args, and
# uvicorn's 16 MiB ws_max_size is reached at just under 12 MiB of plaintext
# (measured, client-go/ctxcap_measure_test.go). A machine that can PULL is not
# subject to it — see PULL_MAX_BYTES.
MAX_SOURCE_BYTES = 8 * 1024 * 1024

# The ceiling on the PULL path: the largest source this host will
# authorize a machine to fetch for one extraction.
#
# WHY 64 MiB, and why it is not a number anyone has to pick again:
#   * It cannot bind on anything the platform actually stores today.
#     MAX_FILE_UPLOAD_BYTES is 25 MiB (server/routes/context.py:818), so the
#     upload cap is the real ceiling on a stored document and this sits 2.5x
#     above it. Raising what a user may upload stays a product decision; this
#     number is a platform guard and must never be the thing that silently
#     decides it.
#   * It clears the case the pull path exists for. Scanned PDFs — the OCR lane
#     — run tens of MB at 150-300 dpi, which is 8x past the
#     inline cap and comfortably inside this one.
#   * Production precedent for a document-extraction ceiling brackets it on both
#     sides: AWS Textract accepts 10 MB synchronously and 500 MB asynchronously;
#     Google Document AI accepts 20 MB online and 1 GB in batch; Azure AI
#     Document Intelligence accepts 500 MB on its paid tier. Everyone draws a
#     low line for "one request, answer now" and a high line for "queued job".
#     This lane is the first kind — one dispatch, one answer — so it belongs at
#     the low end of that range, with room for the scanned-PDF case that the
#     10-20 MB lines would refuse.
#   * It bounds what the host pays to serve one. The bytes are stored base64, so
#     serving a 64 MiB source costs ~85 MiB of string plus 64 MiB of bytes at
#     peak, once, in a thread. Larger sources want a streaming rehydrate rather
#     than a bigger number here.
PULL_MAX_BYTES = 64 * 1024 * 1024

# ── how long the BOX is allowed to work, and where that number comes from ───
#
# IT COMES FROM THE BOX. There is no table of tool ceilings on this host.
#
# Reading a PDF's text layer and OCRing thirty scanned pages are not the same
# amount of work, so the wait has to be per-job. The obvious way to do that is a
# table here mirroring the box's, with the unknown-tool default set to the
# largest value THIS host knows so a host behind the box waits too long rather
# than too little. That asymmetry protects an old HOST and leaves a new BOX
# unprotected, and the gap is not hypothetical:
#
#   The bastion publishes separately and users update on their own schedule, so
#   "the box is ahead of the host" is the NORMAL state, not an edge case. The
#   moment a bastion ships a job whose ceiling exceeds this host's largest KNOWN
#   value — transcription of an hour of audio on a CPU is not a 150-second job —
#   the host waits 170 s while the box works for 600 s. The machine is told it
#   did not answer by the side that quit first, and its transcript is discarded
#   on arrival. That is the precise failure the pull path was rebuilt to remove,
#   reintroduced one layer up.
#
# So the number originates in ONE place and travels, the way pull_budget already
# does. The box declares each job's ceiling in its prep advertisement
# (client-go/contextprep.go, prepToolReport.CeilingS, read from the same function
# the job is actually run under). This host waits that plus a round trip, and
# GRANTS the same number back in the job as `extract_budget`, which the box
# clamps DOWN to and never up. A job that does not exist yet is therefore bounded
# correctly by a host that has never heard of it — which no table can do.

# What this host adds on top of the box's own ceiling: one frame out, one frame
# back, and the reply. This IS the margin by which the box gives up first, and it
# is a property of the WIRE rather than of any tool, so it is the only constant
# here that is not read off the box.
REPLY_ROUND_TRIP = 20.0

# The ceiling assumed for a machine that declares none.
#
# NOT a mirror and not a guess: a bastion that reports no ceiling predates the
# field, and the only job such a build has is the text-layer read, which is
# bounded at 20 s on the box (ctxExtractTimeout). Every job added since declares
# its cost — the box's TestEveryAdvertisedToolDeclaresItsCeiling fails the build
# otherwise — so this cannot silently become the value for a long job.
UNDECLARED_CEILING_S = 20.0

# The most this host will park a management worker on one job, whatever a box
# declares. A guard against a buggy or hostile declaration, not a product limit:
# a box asking for an hour gets 30 minutes and its work is cut off, which is a
# visible, reported deadline rather than a worker held indefinitely. A job that
# genuinely needs longer than this wants an async start/status lane, not a
# larger number here.
MAX_DECLARED_CEILING_S = 1800.0


def declared_ceiling(machine: dict, tool: Optional[str]) -> float:
    """The seconds THIS machine says THIS job costs it, from its own prep
    advertisement. Bounded at both ends: a machine that declares nothing gets the
    text-layer number, and one that declares more than this host will park a
    worker for gets the guard."""
    for t in (machine.get("prep_tools") or machine.get("tools_detail") or []):
        if isinstance(t, dict) and t.get("name") == tool:
            try:
                declared = float(t.get("ceiling_s") or 0)
            except (TypeError, ValueError):
                declared = 0.0
            if declared > 0:
                return min(declared, MAX_DECLARED_CEILING_S)
    return UNDECLARED_CEILING_S


def _reply_allowance(ceiling_s: float = UNDECLARED_CEILING_S) -> float:
    """What the host adds on top of the transfer grant: the box's own declared
    ceiling for THIS job plus the frame round trip. This is the margin by which
    the box gives up first, and it holds for a job this host has never heard of
    because the ceiling is the box's number, not one of ours."""
    return ceiling_s + REPLY_ROUND_TRIP


# ── the PULL path's two clocks, and why only one of them is a constant ──────
#
# On the pull path the machine has to DOWNLOAD before it can start, so the host
# has to wait longer than it does inline. Both sides need a deadline, and THE
# ORDERING BETWEEN THEM IS THE POINT: the box must give up first, or the host
# reports "did not answer" about a machine that is downloading happily, then
# discards the text when it finally arrives. The work gets done, the bytes get
# moved, nothing is written, and the user is told their machine is unresponsive
# by the component that quit first.
#
# The first cut of this had two independent constants — a per-size wait here and
# a flat four minutes on the box — with a comment on each side asserting the
# ordering. The arithmetic said the opposite at every size the ceiling permits:
# the host gave up at 124s on a 64 MiB document while the box was good for 240s,
# and it would have taken a 180 MiB source for the claim to come true. A comment
# asserting a relationship between two constants in two repos is exactly the
# thing that rots.
#
# So the host GRANTS the budget on the wire. `pull_budget` travels in the job,
# the box clamps it down and never up, and the host's own wait is that same
# number plus a fixed allowance. The box is therefore under the host by
# PULL_REPLY_ALLOWANCE at every size, by construction rather than by agreement.

# The transfer rate this lane REQUIRES. Not a measurement and not a prediction —
# a floor, with a stated consequence: a transfer slower than this aborts on the
# box and is reported as a slow transfer, never as an unresponsive machine.
# 0.5 MiB/s is ~4 Mbit/s sustained, under every published broadband floor (the
# FCC's own definition was 25/3 Mbit/s from 2015 and 100/20 from 2024) and far
# under what a machine already needs to hold a WireGuard tunnel and serve the
# other bastion lanes. The relevant leg is the user's DOWNSTREAM, which is the
# generous side of any residential link.
PULL_RATE_BYTES_PER_S = 512 * 1024

# The floor of a grant: the dispatch, the TLS handshake, and this host's own
# rehydrate + decode, before a single byte of the document moves.
PULL_BUDGET_BASE = 30.0

# The ceiling on one grant. Mirrored by ctxPullMaxTimeout on the box, which
# clamps to it, so a host asking for more gets the box's number. At the 64 MiB
# source ceiling the formula reaches 158s, so this does not bind today — it is
# the guard for whoever raises PULL_MAX_BYTES later.
PULL_BUDGET_MAX = 240.0

# What the host adds on top of the transfer grant for the job it would ask of a
# machine that declares nothing about its own costs: that machine's text-layer
# read, bounded at 20 s on the box, plus the frame round trip and the reply. This
# IS the margin by which the box gives up first, and it is constant across sizes
# because both numbers come from one formula rather than from two.
#
# DERIVED now, not chosen — it is _reply_allowance() with no argument. The
# allowance is per-job, because OCR is not twenty seconds of work, and the
# per-job number is read off the BOX rather than from a table here; see
# declared_ceiling below for why a table was the wrong answer. Kept as a name
# because it is what makes the ordering readable and because
# scripts/test_bastion_context_pull.py asserts the pull path against it.
PULL_REPLY_ALLOWANCE = UNDECLARED_CEILING_S + REPLY_ROUND_TRIP


def _pull_budget(size_bytes: int) -> float:
    """The seconds the host GRANTS a machine to fetch one source. Travels in the
    job as `pull_budget`; the box clamps it down, never up."""
    return min(PULL_BUDGET_MAX,
               PULL_BUDGET_BASE + max(0, int(size_bytes)) / PULL_RATE_BYTES_PER_S)


def _pull_timeout(size_bytes: int, ceiling_s: float = UNDECLARED_CEILING_S) -> float:
    """How long the host waits for the whole job: the grant plus a fixed
    allowance, so the box's deadline is strictly the smaller of the two.

        size        box budget    host waits   (tool = pdf-text)
        unknown          30 s         70 s
         1 MiB           32 s         72 s
         8 MiB           46 s         86 s
        25 MiB           80 s        120 s   (the upload cap)
        64 MiB          158 s        198 s   (the source ceiling)

    `ceiling_s` — the box's OWN declared cost for the job — moves the second
    column only: a pdf-ocr job on a 25 MiB scan gets the same 80 s to download
    and 170 s on top to read it, because the machine said OCRing thirty pages
    costs it 150 s and OCRing thirty pages is not the twenty seconds a text-layer
    read is bounded at.
    """
    return _pull_budget(size_bytes) + _reply_allowance(ceiling_s)

# Where extracted rows say they came from, and how they are found again to be
# replaced on a re-run.
EXTRACT_SOURCE = "extracted"

# A clip's frames say a DIFFERENT source, so the text a document produced and
# the frames a clip produced are never each other's replace-set: both hang off
# media_parent_id, and a shared source would make re-extracting one delete the
# other.
FRAME_SOURCE = "frames"


def _bare_mime(mime: str) -> str:
    return (mime or "").split(";")[0].strip().lower()


def tools_for_mime(mime: str) -> tuple[str, ...]:
    """Every extraction job this file knows for a mime, best first.

    One entry for almost everything, and more than one for a clip: an mp4 has
    words in it and pictures in it. Empty means this host does not know how to
    turn that file into anything yet, which is said plainly rather than attempted
    with the wrong tool.
    """
    bare = _bare_mime(mime)
    if bare in MIME_TOOLS:
        return MIME_TOOLS[bare]
    one = MIME_TO_TOOL.get(bare)
    return (one,) if one else ()


def tool_for_mime(mime: str) -> Optional[str]:
    """The extraction job a mime type needs, or None when it is not a kind this
    host knows how to turn into text yet."""
    tools = tools_for_mime(mime)
    return tools[0] if tools else None


async def no_reader_message(pool, entity_id: int, fname: str, mime: str, ctx_id) -> str:
    """What the host is told when it holds a file it has no reader for.

    Says what the file is and that the host cannot read it; names a machine
    route ONLY when this type has an extraction job AND one of the user's
    machines is online for that job. The old wording pointed every unreadable
    type at op='extract', including types no machine can turn into text, and
    the host went looking (a gzip; the archive readers
    now cover that case, and this covers the rest). A failure to look is said
    as that, never as "no machine".

    A picture the Host's own model can look at (image_engine
    .LOOKABLE_IMAGE_TYPES, the types action='look' takes from a kept file) is
    said as that first, with the call. It used to be "no reader for that
    type", and once the Host then dispatched an agent to
    read a PNG it could have looked at itself.
    """
    from mastercode.image_engine import LOOKABLE_IMAGE_TYPES
    if _bare_mime(mime) in LOOKABLE_IMAGE_TYPES:
        ref = ctx_id if ctx_id is not None else fname
        head = (f"{fname!r} is a stored {mime} picture, not text — its bytes are kept "
                f"as-is, and there is no reader for that type that turns it into text. "
                f"Look at it yourself: read(tool='image_generate', args={{'action': "
                f"'look', 'images': ['file:{ref}']}}) shows it to your own model "
                f"(file: takes the file's row id or its title).")
    else:
        head = (f"{fname!r} is a stored {mime or 'binary'} file, not text — its bytes are "
                f"kept as-is, and the host has no reader for that type. Tell the user so.")
    tools = tools_for_mime(mime)
    if not tools:
        return head + (" Nothing on this platform turns that type into text, so there is "
                       "no machine to ask; do not try one.")
    # Every job this type has, best first — a clip has words in it and
    # pictures in it, and a machine that offers only the second is still a
    # route (extract_context_file falls back the same way). A route is a
    # machine the user has APPROVED for it (a signed grant naming ctx_extract);
    # one that only has the engine is named as needing the grant.
    try:
        from server.network import nac_mgmt as _nm
        ungranted: list[str] = []
        for job in tools:
            machines = await machines_for(pool, entity_id, job)
            if not machines:
                continue
            gate = await _nm.gate_servers(pool, entity_id, EXTRACT_OP,
                                          [m.get("server") for m in machines])
            ok_names = [str(m.get("server") or "?") for m in machines
                        if gate.get(str(m.get("server")), (False, "", ""))[0]]
            if ok_names:
                return head + (f" {', '.join(ok_names[:3])} can turn it into text: op='extract' "
                               f"(context_id={ctx_id}) runs a {job} job there, then read it.")
            ungranted += [str(m.get("server") or "?") for m in machines]
        if ungranted:
            return head + (f" {', '.join(ungranted[:3])} could run a {tools[0]} job on it but "
                           f"holds no signed grant for {EXTRACT_OP} — do not try it; you may "
                           f"draft a grant naming {EXTRACT_OP} for the user to approve "
                           f"(propose_mgmt_grant) and tell them.")
    except Exception:  # noqa: BLE001 — said, never raised into the read
        return head + (f" A {tools[0]} job on one of the user's machines could, but the host "
                       f"could not check which machines are online just now — say that; "
                       f"do not try one.")
    jobs = " or ".join(tools)
    return head + (f" It would take a {jobs} job on one of the user's machines and no "
                   f"online machine offers one — say that; do not try one.")


def _tool_names(b: dict) -> list:
    """The job names a bastion row advertises.

    None (never reported) and [] (probed, found nothing) are BOTH empty here —
    the difference matters for what the host SAYS, not for what it can ask for,
    and prep_scan is what carries it.
    """
    return [str(t.get("name") or "") for t in (b.get("prep_tools") or []) if isinstance(t, dict)]


def machine_pulls(m: dict) -> bool:
    """Whether this machine fetches the source itself instead of being handed it.

    Reported in the box's prep scan report (client-go/contextprep.go), not as a
    per-tool flag: the transport is a property of the BOX's extraction lane, and
    a build either has the fetch path or it does not. Absent means an older
    bastion that only understands the inline frame — which is the right default,
    because an older box handed a ticket would refuse the job.
    """
    scan = m.get("scan")
    return bool(isinstance(scan, dict) and scan.get("pull"))


async def machines_for(pool, entity_id: int, tool: Optional[str] = None) -> list:
    """The user's ONLINE machines that can run `tool` (or any extraction at all).

    Capability first, then the tool list: a box advertising context-prep with no
    working pdftotext cannot do a PDF, and offering it would be a control that
    fails after the user chose it — the reason mirror.go gates its own
    advertisement.

    NOTE the key: list_bastions returns the token id as "id", not "token_id".
    The mgmt relay is keyed by that value, so reading the wrong key here does not
    raise — it dispatches to None and comes back "no worker connected", which
    looks like an offline machine and is not.
    """
    from server.network import topology
    out = []
    try:
        for b in await topology.list_bastions(pool, entity_id):
            if not b.get("online"):
                continue
            if PREP_CAPABILITY not in (b.get("capabilities") or []):
                continue
            names = _tool_names(b)
            if tool and tool not in names:
                continue
            out.append({
                "server": b.get("hostname"),
                "token_id": b.get("id"),
                "tools": names,
                # The box's OWN entries, carried through whole. declared_ceiling
                # reads the per-job cost out of this — the host keeps no table of
                # its own, so a job this build has never heard of is still
                # bounded by the number the machine that runs it published.
                "prep_tools": b.get("prep_tools") or [],
                # Carried through so a caller can tell "this box has no tools"
                # from "this box could not look" without a second read.
                "scan": b.get("prep_scan"),
            })
            # Said out loud rather than left in the scan blob: how big a document
            # this machine can be asked for, which is a transport fact about that
            # box and the first thing a user wants when one is refused.
            out[-1]["pull"] = machine_pulls(out[-1])
            out[-1]["max_bytes"] = PULL_MAX_BYTES if out[-1]["pull"] else MAX_SOURCE_BYTES
    except Exception as e:
        # RE-RAISE, do not return []. Swallowing here made a DB error read as
        # "no online machine of yours offers context preparation — this runs on
        # your hardware", which sends the user to go set up a machine they
        # already have. The caller turns this into an honest "could not check".
        logger.info("context extract: cannot list machines (%s)", e)
        raise
    return out


def _tool_models(machine: dict, tool: Optional[str]) -> list:
    """The speech models THIS machine says it holds for THIS job, best first.

    Read out of the machine's own advertisement (client-go/contextprep.go,
    prepToolReport.Models) rather than from any table here — the same discipline
    ceiling_s follows, and for the same reason: the box publishes on its own
    schedule and a mirror here would be wrong the first time a user downloads a
    model. An empty list is a bastion that predates the field, and the caller
    treats that as "cannot check" rather than as "has none".
    """
    for t in (machine.get("prep_tools") or []):
        if isinstance(t, dict) and t.get("name") == tool:
            return [str(m) for m in (t.get("models") or []) if str(m).strip()]
    return []


def _mmss(offset_ms: int) -> str:
    """A moment in a recording as a person reads it. Used in the row's TITLE,
    which is what the lexical arm indexes and what a search result shows."""
    s = max(0, int(offset_ms)) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _chunk_segments(segments: list) -> list:
    """Group a transcript's segments into embeddable chunks, keeping the moment.

    Returns [(text, offset_ms), ...] where offset_ms is when the chunk's FIRST
    word was said — which is what goes into media_offset_ms (schema v135) and
    what turns a search hit into "jump to 12:04 of your meeting".

    THREE RULES, each one a thing that goes wrong without it:

      * NEVER SPLIT A SEGMENT. A segment is the engine's own unit and it has one
        timestamp; half of one has no honest offset to carry.
      * TARGET THE SAME SIZE AS EVERY OTHER CHUNK IN THE CORPUS
        (TEXT_CHUNK_CHARS). An extracted transcript and a pasted page have to
        behave identically at search time or one of them quietly wins.
      * A SINGLE OVERSIZE SEGMENT IS ITS OWN CHUNK rather than being dropped or
        cut. It is rare — whisper segments run a line at a time — and losing the
        user's words to a size rule would be the deletion failure this whole job
        is designed around, arriving from our side instead of the engine's.

    There is deliberately NO OVERLAP between chunks, unlike _chunk_text. Overlap
    exists to stop a sentence being cut in half by a blind character window;
    these are cut on the engine's own boundaries, so overlapping would duplicate
    whole sentences in the corpus and give one moment two offsets.
    """
    from server.routes.context import TEXT_CHUNK_CHARS

    out: list = []
    cur: list = []
    cur_len = 0
    cur_off = None
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        try:
            t0 = int(seg.get("t0") or 0)
        except (TypeError, ValueError):
            t0 = 0
        if cur and cur_len + len(text) + 1 > TEXT_CHUNK_CHARS:
            out.append(("\n".join(cur), cur_off))
            cur, cur_len, cur_off = [], 0, None
        if cur_off is None:
            cur_off = t0
        cur.append(text)
        cur_len += len(text) + 1
    if cur:
        out.append(("\n".join(cur), cur_off))
    return out


def _transcript_partial_note(payload: dict) -> str:
    """A leading sentence when only PART of a recording is in the corpus.

    The numbers are SECONDS OF THE USER'S RECORDING, which is the unit they think
    in. Saying "the machine ran out of time" without saying how much of the
    meeting made it leaves them unable to tell whether they have most of it or
    almost none.
    """
    done = int(payload.get("transcribed_ms") or 0) // 1000
    total = int(payload.get("audio_ms") or 0) // 1000
    bits = []
    if payload.get("audio_capped"):
        bits.append(f"only the first {_mmss(done * 1000)} of this recording was "
                    f"transcribed — it is longer than one job covers, and the rest "
                    f"is NOT in your corpus")
    elif payload.get("deadline"):
        if total:
            bits.append(f"the machine ran out of time at {_mmss(done * 1000)} of "
                        f"{_mmss(total * 1000)} — the rest is NOT in your corpus")
        else:
            bits.append(f"the machine ran out of time after {_mmss(done * 1000)} of "
                        f"this recording — the rest is NOT in your corpus")
    elif total and done and total - done > 30:
        # NOT A DEADLINE AND NOT A CAP, and it still stopped short. Usually a long
        # silent tail, sometimes an engine that gave up on a stretch — either way
        # the gap is the user's to know about rather than ours to explain away.
        bits.append(f"the transcript reaches {_mmss(done * 1000)} of a "
                    f"{_mmss(total * 1000)} recording")
    if payload.get("truncated"):
        bits.append("the transcript was longer than one job returns and was cut off")
    if not bits:
        return ""
    return "; ".join(bits) + ". "


def _lang_note(asked: Optional[str], payload: dict) -> str:
    """Said out loud when a language was named and the reply does not confirm it.

    THE CASE THIS EXISTS FOR IS AN OLD BASTION. `lang` on the extract request is
    newer than the published box, and Go silently discards a JSON field it does
    not declare — so a machine that has not updated runs the engine's English
    default and answers exactly as it would have if nothing had been asked. A
    host that assumed otherwise would report a German transcript that is English.

    A box that DOES know the field echoes the language it ran in as the reply's
    `lang` (client-go/contextstt.go:1077, :1210), and one that cannot honour the
    request refuses the job outright rather than transcribing under the wrong
    prior. So: reply names what was asked -> silence; anything else -> this.

    Never an error. The words in the transcript are words the user really said,
    and throwing them away because the label is uncertain would lose the one
    thing the job was for.
    """
    if not asked:
        return ""
    got = str(payload.get("lang") or "").strip().lower()
    if got == asked:
        return ""
    if not got:
        return (f"the language you named ({asked}) did not reach this machine — its "
                f"bastion predates being able to be told one, so the recording was "
                f"read under the engine's English default. Updating the bastion is "
                f"what makes the request land. ")
    return (f"you named {asked} and the machine read this in {got}; the transcript is "
            f"what {got} produced. ")


def _partial_note(payload: dict) -> str:
    """A leading sentence when this is only PART of the document, and nothing at
    all when it is the whole thing.

    Silently returning 30 pages of a 44-page contract is the same failure as
    returning none of it: the user has no way to know the rest exists, and every
    search over their corpus is then quietly missing pages they believe are in
    it. So a partial read says so FIRST, before the reassuring half of the note.
    """
    bits = []
    # A RECORDING IS NOT A DOCUMENT and none of the page sentences below fit one.
    # Falling through would tell a user whose hour-long meeting was cut short that
    # "0 page(s) were read", which is both wrong and unactionable.
    if payload.get("model") or payload.get("transcribed_ms"):
        return _transcript_partial_note(payload)
    if payload.get("pages_more"):
        bits.append(f"only the first {payload.get('pages') or 0} page(s) were read — "
                    f"this document is longer than one extraction job covers, and the "
                    f"rest is NOT in your corpus")
    if payload.get("deadline"):
        bits.append(f"the machine ran out of time part-way through and this is the "
                    f"{payload.get('pages') or 0} page(s) it had finished")
    if payload.get("pages_unread"):
        bits.append(f"{payload['pages_unread']} page(s) took longer than the engine is "
                    f"given per page and were NOT read — those are unread, not blank")
    if payload.get("truncated"):
        bits.append("the text was longer than one job returns and was cut off")
    if not bits:
        return ""
    return "; ".join(bits) + ". "


def blocked_reasons(machines: list, names: set) -> list:
    """Every machine that has a job's PROGRAM installed and still cannot run it,
    with the box's own explanation.

    This is the quiet-vs-blind distinction as the user meets it, and it is worth
    more for transcription than for anything else in this lane: the Ubuntu
    whisper.cpp package ships NO MODEL, so "engine installed, nothing to load" is
    the state a user is in the moment after they install it. Telling that user
    "no machine of yours can transcribe" would send them to install software that
    is already on their disk.
    """
    out = []
    for m in machines:
        scan = m.get("scan")
        if not isinstance(scan, dict):
            continue
        for b in (scan.get("blocked") or []):
            if not isinstance(b, dict) or b.get("name") not in names:
                continue
            out.append({"server": m.get("server"), "reason": b.get("reason") or ""})
    return out


def stt_blocked_reasons(machines: list) -> list:
    """The transcription half of blocked_reasons: engine present, model missing
    (or ffmpeg missing, for the container job)."""
    return blocked_reasons(machines, STT_TOOLS)


def ocr_blocked_reasons(machines: list) -> list:
    """Every machine that has the OCR engine installed and still cannot use it,
    with the box's own explanation.

    This is the quiet-vs-blind distinction as the user meets it. A machine with
    no tesseract says nothing here — there is nothing to tell someone about
    software they never installed. A machine WITH tesseract and no language data
    says so, because "no machine of yours can OCR" would send that user to
    install a package that is already on their disk.
    """
    return blocked_reasons(machines, {"ocr", "pdf-ocr"})


async def _no_text_note(pool, entity_id: int, tool: str, payload: dict,
                        chosen: dict) -> str:
    """Why this document produced no text, in the words the situation deserves."""
    pages = payload.get("pages") or 0
    pages_ocr = payload.get("pages_ocr") or 0

    # ── A RECORDING THAT CAME BACK WITH NO WORDS ───────────────────────────
    #
    # FOUR answers now, and they are fixed by four different things: nothing,
    # nothing, a model download, and an engine install. Getting it wrong here is
    # worse than for OCR, because the commonest of them — engine present, no
    # model — is the state EVERY user is in the moment after they install the
    # package, which ships no model at all.
    if tool in STT_TOOLS:
        if payload.get("blank"):
            # ONE: the engine listened to the whole thing and nobody spoke. Only
            # the machine can tell anyone this, which is why it reports it.
            return (f"{chosen['server']} listened to this recording with "
                    f"{payload.get('model') or 'its speech model'} and there is no "
                    f"speech in it — it is silent, not unreadable")
        if payload.get("non_speech"):
            # TWO: the engine HEARD something and NAMED it rather than
            # transcribing it — music, applause, a ringing phone. Separate from
            # silence because the recording is not silent, and separate from "no
            # text" because the machine knows why there is none.
            #
            # WHAT IT REPLACES IS THE REASON IT EXISTS. Without it a song fell
            # through to the last branch, which tells the user that "a larger
            # model reads quiet or noisy audio far better" and quotes word-error
            # rates — i.e. it sends someone who uploaded music to download 1.6 GB
            # and transcribe it again for the same nothing. A wrong instruction
            # costs more than a missing one.
            return (f"{chosen['server']} listened to this recording with "
                    f"{payload.get('model') or 'its speech model'} and there is sound "
                    f"in it but no speech — the engine recognised it as something "
                    f"other than someone talking, such as music. Nothing was filed, "
                    f"because what the engine calls a sound is its own description "
                    f"and never words anybody said")
        try:
            prep = await machines_for(pool, entity_id)
        except Exception:
            return ("this recording came back with no words in it, and which of your "
                    "machines can transcribe could not be checked just now.")
        stuck = stt_blocked_reasons(prep)
        if stuck:
            # TWO: the engine is installed on their machine and cannot run —
            # nearly always because it has no model. The box's own words say
            # which directories it looked in.
            lines = "; ".join(f"{b['server']}: {b['reason']}" for b in stuck if b["reason"])
            return ("this recording came back with no words in it. The speech engine IS "
                    f"installed on your machine(s) and cannot run — {lines}")
        # THREE: it ran, with a model, and produced nothing the engine called
        # silence either. Worth saying plainly rather than dressing up.
        return (f"{chosen['server']} ran {payload.get('model') or 'its speech model'} "
                f"over this recording and it produced no text. If there is speech in "
                f"it, a larger model reads quiet or noisy audio far better — "
                f"large-v3-turbo scores 3.84% word error where base.en scores 12.55% "
                f"on hard audio")

    # ONE: the engine ran and the pages really are blank. A finished job, not a
    # gap. Only the box can tell anyone this, which is why it reports the count.
    if tool in ("ocr", "pdf-ocr") and (pages_ocr or tool == "ocr"):
        if tool == "ocr":
            return (f"{chosen['server']} read this image with OCR and found no text on "
                    f"it — the picture has no words in it, as far as the engine can see")
        return (f"{chosen['server']} read {pages_ocr} of {pages} page(s) with OCR and "
                f"found no text on them — those pages are blank, not unreadable")

    # AN OFFICE DOCUMENT IS NOT A SCAN, and every sentence below this point
    # assumes it is one. Falling through would tell a user whose empty
    # spreadsheet came back empty that "it is a scan, so the words on it are a
    # picture", and send them to install an OCR engine that would find exactly
    # the same nothing. The box reads these packages structurally: it returns a
    # heading for every sheet and every slide even when there are no words under
    # it, so an EMPTY result from this job means the document really is empty.
    if tool == "office-text":
        return (f"{chosen['server']} opened this document and there is no text in it — "
                f"an empty file, or one whose content is all pictures and shapes. "
                f"Pictures inside a document are not read by this job; OCR reads a "
                f"picture, and this was a document")

    # TWO and THREE: this was a text-layer read of a document with no text
    # layer, i.e. a scan. Say what would read it and whether anything can.
    note = ("this document has no text layer — it is a scan, so the words on it are "
            "a picture and reading them needs OCR")
    try:
        prep = await machines_for(pool, entity_id)
    except Exception:
        return note + ". Which of your machines can OCR could not be checked just now."
    blocked = ocr_blocked_reasons(prep)
    has_ocr = [m["server"] for m in prep if "pdf-ocr" in (m.get("tools") or [])]
    # A retry is offered only where the gate would let it run: a machine with
    # the engine and no signed grant is named as needing the grant, never as a
    # machine to try — that is the probing this lane exists to stop. Since the
    # upgrade already takes any GRANTED OCR machine, a granted one here means
    # the upgrade was skipped by a caller naming the job, and saying so
    # plainly is right.
    if has_ocr:
        from server.network import nac_mgmt as _nm
        try:
            g = await _nm.gate_servers(pool, entity_id, EXTRACT_OP, has_ocr)
        except Exception:
            return note + ". Which of your machines may OCR it could not be checked just now."
        can = [sv for sv in has_ocr if g.get(sv, (False, "", ""))[0]]
        if can:
            return note + f". {', '.join(can)} can do it; try again naming that machine."
        return (note + f". {', '.join(has_ocr)} has the OCR engine but holds no signed grant "
                       f"for {EXTRACT_OP} — do not try it; a grant naming {EXTRACT_OP} is the "
                       f"user's to approve (propose_mgmt_grant drafts one).")
    if blocked:
        lines = "; ".join(f"{b['server']}: {b['reason']}" for b in blocked if b["reason"])
        return (note + f". The OCR engine IS installed on your machine(s) and cannot run — "
                       f"{lines}")
    return (note + ". None of your online machines has an OCR engine yet. Install "
                   "tesseract AND a language pack — they are separate packages on most "
                   "systems (on Debian/Ubuntu, tesseract-ocr and tesseract-ocr-eng), and "
                   "a machine with the engine alone cannot read a word. It appears on "
                   "the next check-in.")


def type_scoped_gate(gate: dict, machines: list, *, named: bool) -> dict:
    """The grant gate's answers with every machine outside the running
    turn's type turned into a refusal (runtime/turn_types.py). '*' in the
    type lets the pick take any machine, never one the caller named."""
    from mastercode.runtime.turn_types import current_server_scope
    scope = current_server_scope()
    if scope is None:
        return gate
    out = dict(gate)
    for m in machines:
        s = m["server"]
        if s not in scope and (named or "*" not in scope):
            out[s] = (False, "outside this turn's type", "denied-pod")
    return out


async def extract_context_file(pool, entity_id: int, ctx_id: int,
                               *, server: Optional[str] = None,
                               tool: Optional[str] = None,
                               model: Optional[str] = None,
                               lang: Optional[str] = None,
                               data: Optional[bytes] = None) -> dict:
    """Turn ONE stored document into text and file it.

    THE HOST READS IT ITSELF when it has a reader for the type (server/readers:
    PDF, Office, RTF, email, HTML, text) — a plain function, no AI, no machine.
    A file uploaded to a Host is where it lives and is
    processed; the reader is a *read* op the host calls, never an agent it
    dispatches. `data` is the file's bytes when the caller already holds them
    (the upload itself), so nothing is re-read from the store.

    A machine of the user's is asked only for what the host carries no engine
    for — OCR of a picture or a scanned PDF, speech, video frames — or when the
    caller names a machine job with `tool`.

    Every refusal names its own reason: "extraction failed" sends a user to look
    at the wrong thing.

    `tool` picks among the jobs this file's mime supports when there is more than
    one — a clip has words in it and pictures in it, and the default is the
    words. It is checked against tools_for_mime, so it can only ever name a job
    that makes sense for THIS file; it is not a way to run an arbitrary job on an
    arbitrary row.

    `model` names which speech model the machine should load, from the list that
    machine advertised. Empty means the best it has. It exists because base.en
    and large-v3-turbo are 3x apart in error rate on hard audio, so "which model"
    is a real choice rather than an implementation detail — and whichever one
    runs is written onto every row that comes back.

    `lang` names the SPOKEN language of a recording, the same way `model` names
    the model. It comes from the caller and from nowhere else, and there is no
    default: the engine's own default is English, that is what every transcript
    has always run as, and the benchmark behind the model ranking covered the
    English-only models plus large-v3-turbo ON ENGLISH AUDIO. So this host does
    not pick a language for anybody — not from a locale it does not store
    (behavioral_params carries a timezone and no language), not from a filename,
    and not by asking the engine to auto-detect on every job. Someone has to say
    "this recording is in German", and then it is said out loud on the wire.

    THE BOX EITHER DOES IT OR REFUSES — and an OLD BOX DOES NEITHER, which is the
    case this host has to handle by itself. `lang` on the request is newer than
    the published bastion, and Go drops a JSON field it has never heard of
    without a word, so a machine that has not updated transcribes under its
    English default and answers as if nothing was asked. The reply's own `lang`
    is what settles it: the box fills it with the language it ACTUALLY read in,
    so a reply that does not name what was asked for means the request did not
    land. That is reported instead of being assumed away — never as a failure,
    because the transcript is real words the user really said.
    """
    row = await pool.fetchrow(
        "SELECT id, title, content, storage_backend, metadata, tags FROM user_context "
        "WHERE id = $1 AND entity_id = $2", ctx_id, entity_id)
    if not row:
        return {"error": "no such context entry", "ctx_id": ctx_id}
    meta = row["metadata"]
    meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
    if meta.get("kind") != "file":
        return {"error": "that entry is already text — extraction is for stored documents",
                "ctx_id": ctx_id}
    mime = (meta.get("mime") or "").strip()
    wanted = (tool or "").strip() or None
    if (wanted in (None, HOST_TOOL)) and _docread.reader_for(mime):
        res = await _read_in_host(pool, entity_id, ctx_id, row, meta, mime, data)
        if (res.get("ok") or wanted == HOST_TOOL or not tools_for_mime(mime)
                or _bare_mime(mime) not in MACHINE_FALLBACK_MIMES):
            return res
        # The host's reader found no text (a scanned PDF is pictures of pages):
        # a machine with OCR can still read it, so that is tried before giving
        # up, and the answer says why a machine was asked.
        mres = await extract_context_file(pool, entity_id, ctx_id, server=server,
                                          tool=tools_for_mime(mime)[0],
                                          model=model, lang=lang)
        key = "note" if mres.get("ok") else "error"
        mres[key] = (f"{res.get('error')} — so one of your machines was asked. "
                     + (mres.get(key) or ""))
        return mres
    supported = tools_for_mime(mime)
    if not supported:
        return {"error": f"nothing here turns {mime or 'that file type'} into text yet",
                "ctx_id": ctx_id, "mime": mime}
    if wanted and wanted not in supported:
        # NAMED, NOT REFUSED BLANKLY. A caller asking for frames of a spreadsheet
        # has made a category error and the list is what tells them so.
        return {"error": f"'{wanted}' is not a job this host runs on {mime or 'that file'} "
                         f"— for this file it is: {', '.join(supported)}",
                "ctx_id": ctx_id, "mime": mime, "tools": list(supported)}
    tool = wanted or supported[0]

    # WHICH LANGUAGE, if the caller named one. Checked HERE, before a machine is
    # chosen, because neither half of it is about a machine: naming a language
    # for a job that reads none is a category error, and a string that is not a
    # language tag is one whatever box would have run it. Bounded rather than
    # passed through — it ends up as an argument to a program on the user's own
    # hardware — and the box checks it again against the model that will actually
    # run, because the box is the authority.
    stt_lang = (lang or "").strip().lower() or None
    if stt_lang:
        if tool not in STT_TOOLS:
            return {"error": f"'{tool}' does not read a spoken language — an OCR job "
                             f"reads whichever language pack the machine has and "
                             f"reports which one it used", "ctx_id": ctx_id}
        if not _LANG_TAG.match(stt_lang):
            return {"error": f"'{lang}' is not a language tag. Name one like 'de' or "
                             f"'pt-br', or 'auto' to let the engine detect it",
                    "ctx_id": ctx_id}

    # THE SIZE COMES FIRST, AND FROM METADATA. size_bytes is written at ingest
    # (context.py `_ingest_binary_file`), so the transport decision costs no
    # rehydrate — which is the whole point of the pull path: on that path the
    # host never materialises the document at all, the machine fetches it.
    # A row that predates the field falls back to the stored length, which is
    # base64 and therefore 4/3 of the truth; over-estimating a size is the safe
    # direction for a cap.
    #
    # DECLARED vs ESTIMATED, and the difference is load-bearing: the box
    # verifies the length it fetched against a DECLARED size and refuses a
    # mismatch. An estimate must therefore never be declared — a row on a
    # non-pg backend holds a marker, not the bytes, so the fallback would say
    # "37 bytes" about a 12 MB PDF and fail every pull as a corrupt transfer.
    # Unknown is sent as 0, which the box reads as "check the ceiling only".
    try:
        declared_size = int(meta.get("size_bytes"))
    except (TypeError, ValueError):
        declared_size = 0
    size = declared_size or (len(row["content"] or "") * 3) // 4

    from server.network import nac_mgmt as _nm

    async def _gated(ms: list) -> dict:
        """The signed-grant gate's answer for each of these machines (see below).
        Under a typed turn a machine outside the type answers as refused, so
        the pick and the fallback below only ever take a listed one; '*' lets
        the pick take any machine it was not handed."""
        g = await _nm.gate_servers(pool, entity_id, EXTRACT_OP, [m["server"] for m in ms])
        return type_scoped_gate(g, ms, named=bool(server))

    def _any_granted(ms: list, g: dict) -> bool:
        return any(g.get(m["server"], (False, "", ""))[0] for m in ms)

    try:
        machines = await machines_for(pool, entity_id, tool)
        gate = await _gated(machines)
        # FALL DOWN THE LIST WHEN NOTHING ONLINE CAN DO THE FIRST JOB, and only
        # when the caller named none. A clip defaults to its transcript because
        # the transcript is what someone hunting "the demo where we talked about
        # pricing" is looking for — but a user whose machine has ffmpeg and no
        # speech engine should get their keyframes rather than an error, and a
        # user who explicitly asked for one job should never silently get the
        # other. A machine with no grant counts as "cannot" here: the first job
        # with an APPROVED machine wins, and when none is approved anywhere the
        # first machines that have the engine stand, so the refusal names them.
        fell_from = None
        if not _any_granted(machines, gate) and not wanted:
            if machines:
                fell_from = (tool, [m["server"] for m in machines])
            for alt in supported[1:]:
                alt_machines = await machines_for(pool, entity_id, alt)
                if not alt_machines:
                    continue
                alt_gate = await _gated(alt_machines)
                if _any_granted(alt_machines, alt_gate) or not machines:
                    tool, machines, gate = alt, alt_machines, alt_gate
                if _any_granted(alt_machines, alt_gate):
                    break
            if tool == (fell_from or (None,))[0]:
                fell_from = None
        # ASK FOR THE BETTER JOB WHEN A MACHINE HAS IT. See TOOL_UPGRADE: for a
        # PDF that is pdf-ocr, which reads the text layer where there is one and
        # the scanned pages where there is not. The upgrade is per-MACHINE and
        # never narrows the answer — if nothing online can do it, the original
        # tool and its machines stand, and the empty result that may follow is
        # explained rather than left bare. Nor does it trade a GRANTED machine
        # for the asked job for an ungranted one with the better job: the
        # upgrade happens only onto an approved machine.
        better = TOOL_UPGRADE.get(tool)
        if better:
            upgraded = await machines_for(pool, entity_id, better)
            if upgraded:
                up_gate = await _gated(upgraded)
                # Onto an approved machine — or, when NOTHING can do the asked
                # job, onto the ungranted ones so the refusal names them and
                # the missing grant rather than "install the tool".
                if _any_granted(upgraded, up_gate) or not machines:
                    tool, machines, gate = better, upgraded, up_gate
    except Exception as e:
        return {"error": f"could not check which of your machines can do this "
                         f"just now ({type(e).__name__}) — a transient error, "
                         f"not a setup problem", "ctx_id": ctx_id}

    def _with_fallback_note(res: dict) -> dict:
        """When the job that ran is not the one the file defaults to, because
        the machines for that one hold no grant, the answer says so — a
        silent swap (keyframes for a transcript) would hide the grant the
        user could approve."""
        if fell_from and isinstance(res, dict) and (res.get("ok") or res.get("needs_setup")):
            asked, names = fell_from
            verb = "ran" if res.get("ok") else "chose"
            res["fallback_note"] = (f"{verb} '{tool}' because no machine approved for "
                                    f"'{asked}' was online: {', '.join(names)} can do "
                                    f"'{asked}' but holds no signed grant for {EXTRACT_OP} "
                                    f"(propose_mgmt_grant drafts one for the user)")
        return res
    if not machines:
        any_prep = await machines_for(pool, entity_id)  # same call, already guarded above
        if any_prep:
            # NAME THE FIX THAT WOULD ACTUALLY WORK. "install the tool" is right
            # for poppler and tesseract and WRONG for a job the bastion does
            # itself: there is no package to install, and a user who went looking
            # for one would not find it. A builtin missing from a machine means
            # that machine's bastion is older than the job.
            how = ("update the bastion on that machine — this job needs nothing "
                   "installed, it is built into the bastion itself"
                   if tool in BUILTIN_TOOLS
                   else "install the tool and it appears on the next check-in")
            return {"error": f"none of your online machines can do '{tool}' yet ({how})",
                    "ctx_id": ctx_id, "machines": any_prep}
        return {"error": "no online machine of yours offers context preparation — "
                         "this runs on your hardware, never on the platform",
                "ctx_id": ctx_id}
    # THE SIGNED-GRANT GATE. This lane was once the one way onto a machine with
    # no grant at all — the box's self-reported `context-prep` capability was
    # the whole check, and the host reached for it after a refused read.
    # Now an extraction job runs only on a machine the user's signed grant
    # names for ctx_extract, the same gate mgmt_run runs, audited the same
    # way. A machine that has the engine and no grant is named as that, so
    # the host drafts a grant for the user instead of trying the machine.
    granted = [m for m in machines if gate.get(m["server"], (False, "", ""))[0]]
    if server:
        chosen = next((m for m in machines if m["server"] == server), None)
        if chosen is None:
            return {"error": f"'{server}' is not one of your machines that can do '{tool}'",
                    "ctx_id": ctx_id, "machines": machines}
        ok, why, decision = gate.get(server, (False, "no signed grant (default-deny)", "denied-pod"))
        if not ok:
            await _nm.audit_mgmt(pool, entity_id, server, EXTRACT_OP, exit_code=126, decision=decision)
            return {"error": f"refused: {EXTRACT_OP} on '{server}' not permitted — {why}. "
                             f"The machine can run '{tool}'; a grant naming {EXTRACT_OP} for it "
                             f"is the user's to approve (propose_mgmt_grant drafts one).",
                    "refused": True, "ctx_id": ctx_id, "server": server}
    else:
        if not granted:
            names = ", ".join(m["server"] for m in machines)
            whys = []
            for m in machines:
                _, why, decision = gate.get(m["server"], (False, "no signed grant (default-deny)", "denied-pod"))
                whys.append(f"{m['server']}: {why}")
                await _nm.audit_mgmt(pool, entity_id, m["server"], EXTRACT_OP, exit_code=126,
                                     decision=decision)
            return {"error": f"refused: {EXTRACT_OP} on {names} not permitted — "
                             f"{'; '.join(whys)}. The machine(s) can run '{tool}'; a grant naming "
                             f"{EXTRACT_OP} is the user's to approve (propose_mgmt_grant drafts one).",
                    "refused": True, "ctx_id": ctx_id, "machines": [m["server"] for m in machines]}
        chosen = granted[0]

    # THE TRANSPORT, and the cap that goes with it. A box that can pull is not
    # subject to the frame's 8 MiB; a box that cannot is, and is told so with
    # the number rather than left to guess.
    pulls = machine_pulls(chosen)
    cap = PULL_MAX_BYTES if pulls else MAX_SOURCE_BYTES
    # HOW LONG THIS MACHINE SAYS THIS JOB COSTS IT. Read from its own
    # advertisement, granted back to it on the wire, and used to size this host's
    # wait — one number, originating on the box, so a bastion that ships a longer
    # job than this host has heard of is still waited for properly.
    ceiling = declared_ceiling(chosen, tool)
    # WHICH MODEL, checked HERE rather than left for the box to refuse.
    #
    # The machine publishes the models it holds in its own advertisement, so a
    # caller naming one it does not have can be told so before the source is
    # pulled across the user's link and a worker is parked. The box refuses the
    # same request too — it is the authority and it re-probes — but making the
    # round trip to learn a name is wrong costs the user a download.
    stt_model = (model or "").strip() or None
    if stt_model:
        if tool not in STT_TOOLS:
            return {"error": f"'{tool}' does not use a speech model", "ctx_id": ctx_id}
        have = _tool_models(chosen, tool)
        if have and stt_model not in have:
            return {"error": f"{chosen['server']} has no speech model called "
                             f"'{stt_model}' — it has: {', '.join(have)}. Which model "
                             f"reads a recording decides how accurate the transcript "
                             f"is, so this is not substituted quietly",
                    "ctx_id": ctx_id, "models": have}
    if size > cap:
        extra = ("" if pulls else
                 f" — {chosen['server']} is on a bastion build that takes the "
                 f"document in one management frame; updating it raises this to "
                 f"{PULL_MAX_BYTES}")
        return {"error": f"this document is about {size} bytes, over the "
                         f"{cap} one extraction job carries — it is not "
                         f"truncated or partly done, it is not attempted" + extra,
                "ctx_id": ctx_id, "size_bytes": size, "max_bytes": cap}

    # A FRAME JOB WITH NOWHERE TO PUT THE FRAMES IS NOT WORTH SPENDING THE
    # USER'S CPU ON. Frames are embedded by the MEDIA role, and a host with no
    # media model has no column for a media vector — so the decode would run on
    # someone's laptop, come back, and be dropped. Checked BEFORE dispatch, and
    # reported as the setup state it is rather than as a failure.
    if tool in FRAME_TOOLS:
        from mastercode.media_engine import media_config_state
        mcfg, mcfg_unreadable = await media_config_state(pool, entity_id)
        if mcfg_unreadable:
            return {"error": "your media model's settings could not be read just now "
                             "— a transient error, not a setup problem; nothing was "
                             "decoded", "ctx_id": ctx_id}
        if not mcfg:
            return _with_fallback_note({"needs_setup": "media_embedding", "ctx_id": ctx_id,
                    "note": "a clip becomes searchable by pointing the MEDIA role at "
                            "a model — the same way the text role is set. Until then "
                            "there is nowhere for a frame's vector to go, so nothing "
                            "was decoded on your machine. Set the media role and run "
                            "this again."})

    from server.network.nac_mgmt import get_mgmt_relay
    relay = get_mgmt_relay()
    if relay.worker_count(chosen["token_id"]) == 0:
        return {"error": f"{chosen['server']} is checked in but has no management "
                         f"worker connected right now", "ctx_id": ctx_id}

    if pulls:
        # THE HOST AUTHORIZES ONE FETCH, it does not grant a read. The machine
        # is handed an opaque single-use ticket bound to its own token id — no
        # ctx_id crosses the wire, so there is nothing for it to change and no
        # id space for it to walk. server/network/context_tickets.py.
        #
        # No sha256 travels with it, deliberately, and this is not an oversight
        # carried over from backup_fetch: computing one would force the host to
        # materialise the whole document, which is the exact cost the pull path
        # exists to avoid. backup_fetch needs a digest because its bytes come
        # from object storage over the open internet; these come from this host,
        # over TLS, inside the user's own tunnel, with a Content-Length. The box
        # verifies the LENGTH against the declared size, which is what actually
        # catches a truncated transfer here.
        from server.network import context_tickets
        ticket = context_tickets.mint(
            entity_id=entity_id, ctx_id=ctx_id,
            bastion_token_id=chosen["token_id"], tool=tool, max_bytes=cap)
        # THE CLOCK RUNS ON A SIZE WE TRUST, and that is not the same one the
        # cap ran on. `size` may be the marker-derived estimate for a row on a
        # non-pg backend, which UNDER-reports — the safe direction for a cap and
        # the dangerous one for a deadline, because a 60 MB document held on a
        # bastion would be granted the 30s floor and abort mid-transfer. Unknown
        # therefore buys the full grant, not the smallest one.
        clock_size = declared_size or cap
        budget = _pull_budget(clock_size)
        job = {"tool": tool, "mime": mime, "ticket": ticket, "doc": f"ctx:{ctx_id}",
               "size": declared_size, "max_bytes": cap,
               "pull_budget": round(budget, 3),
               "extract_budget": round(ceiling, 3)}
        if stt_model:
            job["model"] = stt_model
        if stt_lang:
            job["lang"] = stt_lang
        arg = json.dumps(job)
        # The box gives up first BY CONSTRUCTION: same formula, one number on
        # the wire, this wait is that number plus the allowance for THIS job's
        # own extraction ceiling.
        wait = budget + _reply_allowance(ceiling)
    else:
        # The bytes, inline. A non-pg row lives on a machine or a bucket; an
        # offline holder is an honest "not right now", never a broken extraction.
        content = row["content"]
        if (row["storage_backend"] or "pg") != "pg":
            try:
                from brain.mastercode import context_backends
                content = await context_backends.retrieve_user_context_content(
                    pool, entity_id, ctx_id, row["storage_backend"])
            except Exception as e:
                logger.info("context extract: rehydrate failed for %s (%s)", ctx_id, e)
                content = None
        if not content:
            return {"error": "this document's holding machine isn't checked in right now",
                    "ctx_id": ctx_id}
        job = {"tool": tool, "mime": mime, "b64": content, "doc": f"ctx:{ctx_id}",
               "extract_budget": round(ceiling, 3)}
        if stt_model:
            job["model"] = stt_model
        if stt_lang:
            job["lang"] = stt_lang
        arg = json.dumps(job)
        # Inline: no download, so the wait is the box's own declared ceiling for
        # this job plus the round trip, plus one more round trip for pushing the
        # document down the frame. For a text-layer read that is 60 s, the number
        # this was when it was a flat constant.
        wait = _reply_allowance(ceiling) + REPLY_ROUND_TRIP

    # THE PERSON'S APPROVAL (nac_approval): ctx_extract executes an extractor
    # on the machine, and a box that gates writes (0.33.0) spends one per run.
    # Through the person's CARD first (rung 8): "read <doc> with <tool> on
    # <server>" as an overlay over the chat and a ticket; their click files
    # the device ask; the next call of this waits on the tap.
    from server.network import nac_approval, nac_ops
    try:
        appr = await nac_approval.obtain_via_card(pool, entity_id, chosen["server"], EXTRACT_OP, [arg],
                                                  note=f"read a {mime or 'document'} with {tool}",
                                                  token_id=chosen["token_id"])
    except nac_ops.OpError as e:
        # The card could not be filed: this lane's own error shape, with
        # the words, never a bare 500 out of the tool.
        return {"error": e.detail, "ctx_id": ctx_id}
    if appr["state"] == "asked":
        await _nm.audit_mgmt(pool, entity_id, chosen["server"], EXTRACT_OP, exit_code=None,
                             decision="card-filed")
        return {"error": appr.get("next") or "the card is in front of the person", "ctx_id": ctx_id,
                "pending_approval": True, "asked": True, "ticket_id": appr.get("ticket_id"),
                "ask": appr.get("ask") or {}}
    if appr["state"] == "pending":
        await _nm.audit_mgmt(pool, entity_id, chosen["server"], EXTRACT_OP, exit_code=None,
                             decision="awaiting-approval")
        return {"error": appr.get("next") or "waiting for your approval", "ctx_id": ctx_id,
                "pending_approval": True, "request": appr.get("request") or {}}
    if appr["state"] not in ("approved", "not_needed"):
        await _nm.audit_mgmt(pool, entity_id, chosen["server"], EXTRACT_OP, exit_code=126,
                             decision="no-way" if appr["state"] == "no_way" else "denied-person")
        return {"error": f"refused: {appr.get('detail') or appr['state']}", "ctx_id": ctx_id, "refused": True}
    got = await relay.dispatch(chosen["token_id"], EXTRACT_OP, [arg], wait,
                               approval=appr.get("approval") or "")
    # Command accounting, like every op on the user's machine (nac_mgmt's
    # bright line: management is always audited). The gate above held, so the
    # decision is "ran"; the exit is the box's, or the wait when it never came.
    await _nm.audit_mgmt(pool, entity_id, chosen["server"], EXTRACT_OP,
                         exit_code=(got[2] if got else 124), decision="ran")
    if got is None:
        return {"error": f"{chosen['server']} did not answer within {int(wait)}s",
                "ctx_id": ctx_id}
    out, err, code = got
    if code != 0:
        # The box's own words. It knows whether poppler is missing, the PDF is
        # encrypted, or the document was too big — this host does not.
        return {"error": (err or "extraction failed").strip(), "ctx_id": ctx_id,
                "server": chosen["server"]}
    try:
        payload = json.loads(out or "{}")
    except Exception:
        return {"error": "the machine's reply was not readable", "ctx_id": ctx_id}
    # THE REPLY FORKS HERE. Everything below this line reads a `text` field,
    # chunks it and files TEXT rows. A clip's reply has no text field and never
    # will — it carries pictures and the moments they were taken at — so it goes
    # to its own writer, into the MEDIA column family, counted in the media
    # denominator and in no other.
    if tool in FRAME_TOOLS:
        return _with_fallback_note(await _file_frames(
            pool, entity_id, ctx_id, payload, tool=tool, server=chosen["server"],
            title=meta.get("from_file") or row["title"] or f"context {ctx_id}",
            transport="pull" if pulls else "inline"))

    text = (payload.get("text") or "").strip()
    if not text:
        # THE THREE ANSWERS, and keeping them three different sentences is the
        # point of this whole slice. A document that comes back with no text is
        # one of: the engine read every page and they are genuinely blank; this
        # machine has the engine and cannot use it; or no machine of the user's
        # can OCR at all. They are fixed by doing nothing, by installing a
        # language pack, and by installing tesseract — and a single "found no
        # text" sends the user to the wrong one two times out of three.
        return {"ok": True, "ctx_id": ctx_id, "server": chosen["server"],
                "tool": tool, "chunks": 0, "chars": 0,
                "pages": payload.get("pages"), "pages_ocr": payload.get("pages_ocr"),
                # WHICH MODEL HEARD THE SILENCE. "no words" from base.en and "no
                # words" from large-v3-turbo are not the same claim, and the
                # first is worth re-running.
                "model": payload.get("model"),
                "blank": bool(payload.get("blank")),
                # SOUND WITHOUT SPEECH is its own state, never folded into blank.
                # Calling a song silent is a false statement about the user's own
                # file, and it is the machine — not this host — that knows the
                # difference.
                "non_speech": bool(payload.get("non_speech")),
                "audio_ms": payload.get("audio_ms"),
                "note": _lang_note(stt_lang, payload)
                        + await _no_text_note(pool, entity_id, tool, payload, chosen)}

    title = meta.get("from_file") or row["title"] or f"context {ctx_id}"
    return _with_fallback_note(await _file_text(
        pool, entity_id, ctx_id, title=title, tags=list(row["tags"] or []), text=text,
        payload=payload, tool=tool, server=chosen["server"],
        transport="pull" if pulls else "inline",
        stt_lang=stt_lang, upload_batch=meta.get("upload_batch")))


async def _file_frames(pool, entity_id: int, ctx_id: int, payload: dict, *,
                       tool: str, server: str, title: str,
                       transport: str) -> dict:
    """File a clip's frames as MEDIA rows: a vector and a timestamp, no bytes.

    THE FOUR THINGS THIS FUNCTION IS, each settled before it was written:

    1. IT KEEPS THE VECTOR AND THE TIMESTAMP, NEVER THE FRAME. Measured
       at 416 kB against 4.97 MB per 10-minute clip, 12x, and the bytes
       buy nothing: the clip itself is already stored and any frame reproduces
       by seeking to media_offset_ms in about 150 ms. The columns are v135's,
       added for exactly this.

    2. THE BYTES ARE STAGED, NOT STORED, AND THAT IS WHAT MAKES A RETRY
       POSSIBLE. The rows are written WITH their jpegs and no vector; the
       vector's arrival is what drops the bytes (MEDIA_DROP_STAGED_CONTENT_SQL,
       in the same statement). Steady state is still the vector and the
       timestamp — the 12x holds — but between the user's machine handing the
       frames over and an endpoint answering, there is something to retry FROM.

       The first cut of this embedded inline and wrote only what succeeded. It
       read as safe and was not: a frame row carries no bytes for a fill to work
       from, and the media lane selects positively on kind, so a frame that
       failed to embed was in NO population — excluded from text by
       NOT_FILE_KIND_SQL, never selected by media. One provider 502 on frame 180
       of 240 left those frames unsearchable for ever, repairable only by
       re-pulling the whole clip across the user's link to fix an embedding that
       had nothing to do with the frames.

    3. THE INLINE PASS IS AN OPTIMISATION, NEVER THE GUARANTEE. It makes the
       clip searchable in the same request in the common case. The guarantee is
       the fill job queued below, which is the same lane every uploaded picture
       already uses and which retries on its own schedule. Nothing here
       may be read as "the frames are embedded because this returned ok".

    4. IT NEVER TOUCHES THE TEXT LANE'S NUMBERS. Frame rows are kind='frame',
       which NOT_FILE_KIND_SQL excludes from every text selector and every text
       denominator, and the coverage lens counts frames in their own
       denominator. Two populations, no overlap.
    """
    import time as _time
    from server.routes.context import _TSV_SQL, FRAME_KIND
    from server.routes.embed import _embed_batch_media, enqueue_job
    from mastercode.media_engine import (
        MEDIA_DROP_STAGED_CONTENT_SQL, active_media_target, media_cast_type)

    report = payload.get("report") or {}
    frames = [f for f in (payload.get("frames") or []) if isinstance(f, dict)]
    mime = (payload.get("mime") or "image/jpeg").strip() or "image/jpeg"
    if not frames:
        # An honest empty answer. A file with no video keyframes is a file this
        # job has nothing to say about, and calling it a failure would send the
        # user to look at their decoder.
        return {"ok": True, "ctx_id": ctx_id, "server": server, "tool": tool,
                "transport": transport, "frames": 0, "frames_embedded": 0,
                "report": report,
                "note": "your machine read this clip and found no frames in it — "
                        "nothing was filed"}

    # THE MEDIA COLUMN IS RESOLVED ONCE, and a host that lost its media role
    # between the pre-dispatch check and here is told so rather than having
    # frames written with nowhere for their vectors to go.
    media_col, media_dim, media_model = await active_media_target(pool, entity_id)
    if not media_col:
        return {"needs_setup": "media_embedding", "ctx_id": ctx_id,
                "note": "your media role stopped being set while this clip was "
                        "decoding, so there is nowhere to put a frame's vector. "
                        "Nothing was filed; set it and run this again."}
    cast = media_cast_type(media_dim)

    # ── 1. WRITE EVERY FRAME, WITH ITS BYTES AND NO VECTOR ──────────────────
    #
    # REPLACE, NEVER APPEND, in ONE transaction — the same rule the text path
    # follows and for the same reason: a second run must not double the clip in
    # every search result, and a failure part way through must not leave the
    # previous extraction gone and the new one half-written.
    #
    # This happens BEFORE any embedding is attempted, which is the ordering the
    # durability depends on: a crash, a timeout or a dead endpoint from here on
    # leaves rows the fill can finish, rather than nothing at all.
    tsv = _TSV_SQL.format(title="$5", content="''")
    written = []
    staged_bytes = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            replaced = await conn.execute(
                "DELETE FROM user_context WHERE entity_id = $1 AND media_parent_id = $2 "
                "AND source = $3", entity_id, ctx_id, FRAME_SOURCE)
            for i, f in enumerate(frames):
                b64 = str(f.get("b64") or "")
                if not b64:
                    # A frame with no picture is not a frame. Skipped rather
                    # than written as a row nothing could ever embed.
                    continue
                try:
                    offset_ms = int(f.get("offset_ms") or 0)
                except (TypeError, ValueError):
                    offset_ms = 0
                fmeta = {
                    "kind": FRAME_KIND,
                    "mime": mime,
                    "extracted_from": ctx_id,
                    "extracted_by": tool,
                    "extracted_on": server,
                    "from_file": title,
                    "frame": i,
                    "frames": len(frames),
                    "offset_ms": offset_ms,
                    # keyframe or topup — whether the encoder marked this moment
                    # or the extractor filled a thin stretch. A clip that is
                    # mostly top-up is a clip whose encoder barely marked
                    # anything, which is worth being able to see.
                    "frame_source": str(f.get("source") or ""),
                }
                # The title carries the clip's name and the timestamp, so a
                # frame is findable by the filename through the lexical arm even
                # on a host whose media role is later unset.
                ftitle = f"{title} ({_mmss(offset_ms)})"
                new_id = await conn.fetchval(f"""
                    INSERT INTO user_context
                        (entity_id, content, source, source_id, title, tags, metadata,
                         media_parent_id, media_offset_ms, search_tsv)
                    VALUES ($1, $9, $2, $3, $5, $4, $6::jsonb, $7, $8, {tsv})
                    RETURNING id
                """, entity_id, FRAME_SOURCE, f"{ctx_id}:{offset_ms}", [], ftitle,
                     json.dumps(fmeta), ctx_id, offset_ms, b64)
                if new_id is not None:
                    written.append((int(new_id), b64))
                    staged_bytes += (len(b64) * 3) // 4

    if not written:
        return {"error": "your machine returned frames with no pictures in them — "
                         "nothing was filed", "ctx_id": ctx_id, "server": server,
                "frames": len(frames), "report": report}

    # ── 2. QUEUE THE RETRY LANE, BEFORE trying the fast path ───────────────
    #
    # Queued first ON PURPOSE. If the inline pass below throws, or the process
    # dies in the middle of it, the job is already recorded and the frames get
    # embedded anyway. Queuing after would make the fast path's success a
    # precondition for the slow path existing, which is exactly backwards.
    #
    # One live sweep is enough: the job selects every media row missing a
    # vector, so a second behind it would find nothing — the same check
    # _ingest_binary_file makes for pictures.
    queued, not_queued = None, None
    try:
        live = await pool.fetchval("""
            SELECT id FROM embed_jobs
             WHERE entity_id = $1 AND target = 'user_context_media'
               AND status IN ('pending', 'running', 'paused')
             LIMIT 1
        """, entity_id)
        if live:
            queued = int(live)
        else:
            job = await enqueue_job(entity_id, "fill", "user_context_media")
            queued = (job or {}).get("id")
    except Exception as e:
        # The frames ARE filed and staged; this is the retry lane failing to be
        # booked, which is worth saying out loud rather than swallowing.
        not_queued = str(e)
        logger.info("context extract: frames written but media fill not queued (%s)", e)

    # ── 3. THE INLINE PASS — an optimisation, not the guarantee ────────────
    #
    # Best effort, and every failure path here is non-fatal by construction:
    # whatever this does not embed keeps its staged bytes and is finished by the
    # job queued above. It exists so the common case is searchable in the same
    # request, not so that anything depends on it.
    started = _time.monotonic()
    embedded = 0
    inline_error = None
    try:
        vecs = await _embed_batch_media(
            [(rid, mime, b64) for rid, b64 in written],
            entity_id=entity_id, pool=pool)
        for (rid, _b64), vec in zip(written, vecs):
            if not vec:
                continue
            # THE VECTOR AND THE DROP IN ONE STATEMENT, through the same
            # fragment the fill's writer uses — two spellings of "the staged
            # bytes are gone now" is how one of them ends up keeping 5 MB per
            # clip that nothing reads.
            await pool.execute(
                f"""UPDATE user_context
                       SET {media_col} = $1::{cast},
                           media_embed_model = $3,
                           {MEDIA_DROP_STAGED_CONTENT_SQL},
                           updated_at = now()
                     WHERE id = $2""",
                vec, rid, media_model)
            embedded += 1
    except Exception as e:
        inline_error = str(e)
        logger.info("context extract: inline frame embed failed for %s, leaving it "
                    "to the fill (%s)", ctx_id, e)
    embed_seconds = round(_time.monotonic() - started, 2)

    pending = len(written) - embedded
    return {
        "ok": True, "ctx_id": ctx_id, "server": server, "tool": tool,
        "transport": transport,
        "frames": len(frames), "frames_filed": len(written),
        "frames_embedded": embedded,
        # NOT "failed": a frame this pass did not embed is a frame the fill has
        # not reached yet. The distinction is the whole point of the retry lane.
        "frames_pending": pending,
        "row_ids": [rid for rid, _ in written], "replaced": replaced,
        "media_model": media_model, "media_dim": media_dim,
        "embed_seconds": embed_seconds,
        "embed_job_id": queued,
        "embed_not_queued": not_queued,
        "inline_error": inline_error,
        # How much of the user's disk the staging is using until the fill runs.
        # Zero once every frame is embedded, which is the steady state the 12x
        # measurement describes.
        "staged_bytes_pending": 0 if pending == 0 else staged_bytes,
        # The box's own account of what the clip HAD against what it sent, so a
        # 240-frame answer from a two-hour film is never mistaken for a complete
        # one. See ctxFramesReport in client-go/contextframes.go.
        "report": report,
        "note": ("this clip is searchable by what is in it now; each frame keeps "
                 "its vector and its timestamp, never the picture — the frame "
                 "comes back by seeking into the clip you already have"
                 if pending == 0 else
                 f"{embedded} of {len(written)} frames are searchable now; the "
                 f"other {pending} are filed with their pictures held and will be "
                 f"embedded by the fill job — nothing needs re-decoding"),
    }


def _mmss(offset_ms: int) -> str:
    """A frame's offset as a human reads it. Used in the row's TITLE, which is
    what the lexical arm indexes and what a search result shows."""
    s = max(0, int(offset_ms)) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


@router.get("/machines", dependencies=[Depends(requires_permission("read:context"))])
async def list_extract_machines(tool: Optional[str] = None):
    """Which of the user's machines can turn documents into text, and which jobs."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"error": "Brain not initialized"}
    machines = await machines_for(pool, brain_service._entity_id, tool)
    # Which the user has APPROVED for a job (a signed grant naming ctx_extract):
    # the same answer the host's op='extract_machines' gives, so a machine
    # with the engine and no grant reads as "needs a grant", not "can".
    from server.network import nac_mgmt as _nm
    gate = await _nm.gate_servers(pool, brain_service._entity_id, EXTRACT_OP,
                                  [m.get("server") for m in machines])
    for m in machines:
        ok, why, _ = gate.get(str(m.get("server")), (False, "no signed grant (default-deny)", ""))
        m["granted"] = ok
        if not ok:
            m["not_granted"] = why
    return {"machines": machines,
            # EVERY mime this host can route, not just the one-job ones — a clip
            # supports two jobs and would otherwise be missing from the list a
            # caller uses to decide what is worth uploading.
            "mimes": sorted(set(MIME_TO_TOOL) | set(MIME_TOOLS)),
            "mime_tools": {m: list(tools_for_mime(m))
                           for m in sorted(set(MIME_TO_TOOL) | set(MIME_TOOLS))},
            "capability": PREP_CAPABILITY,
            # SAID OUT LOUD, not buried in each machine's scan blob. A machine
            # with the OCR engine installed and no language data is the case
            # most likely to be misread as "no machine of mine can OCR", and the
            # remedy is a different command.
            "ocr_blocked": ocr_blocked_reasons(machines),
            # SAID OUT LOUD for the same reason ocr_blocked is, and with more
            # cause: "the engine is here and has no model" is where every user
            # lands the moment after installing it, because the package ships
            # none. The box's reason names the directories it searched.
            "stt_blocked": stt_blocked_reasons(machines)}


@router.post("/run", dependencies=[Depends(requires_permission("write:context"))])
async def run_extract(body: dict):
    """Extract ONE stored document on the user's machine."""
    pool = brain_service.get_pool()
    if pool is None:
        return {"error": "Brain not initialized"}
    try:
        ctx_id = int((body or {}).get("ctx_id"))
    except (TypeError, ValueError):
        return {"error": "ctx_id required"}
    server = ((body or {}).get("server") or "").strip() or None
    # `tool` picks among the jobs a clip supports — its words or its pictures —
    # and is validated against the file's own mime, so it can never name an
    # unrelated job. `model` picks which speech model reads a recording.
    tool = ((body or {}).get("tool") or "").strip() or None
    model = ((body or {}).get("model") or "").strip() or None
    # `lang` names the spoken language of a recording. There is no default and
    # this host never infers one — see extract_context_file for why.
    lang = ((body or {}).get("lang") or "").strip() or None
    return await extract_context_file(pool, brain_service._entity_id, ctx_id,
                                      server=server, tool=tool, model=model,
                                      lang=lang)


async def _file_text(pool, entity_id: int, ctx_id: int, *, title: str, tags: list,
                     text: str, payload: dict, tool: str, server: str,
                     transport: str, stt_lang: Optional[str] = None,
                     upload_batch: Optional[str] = None) -> dict:
    """File a document's TEXT as ordinary context rows linked to the source row,
    and queue their embedding. One writer for every reader — the host's own
    (server/readers) and a machine's (ctx_extract) — so their rows are the same
    shape. The rows carry the SOURCE row's tags: a file uploaded into a project
    is tagged project:<id>, and so is every piece of its text. They also carry
    the source's upload_batch, so a read of one upload returns that upload's
    text only, a re-upload supersedes the old text with the old file, and a
    retag by batch reaches the text too."""
    from server.routes.context import _TSV_SQL, _chunk_text
    # ── CHUNKING, and why a transcript does not use the plain chunker ───────
    #
    # _chunk_text slides a 1500-character window over a string. That is right for
    # a document, where a character offset means nothing to anybody, and wrong for
    # a recording: cutting mid-sentence at character 1500 loses the one thing a
    # transcript has that a document does not, which is that every word happened
    # at a time. So when the machine returned segments, the chunks are built ON
    # the segment boundaries and each one carries the millisecond its first word
    # was said at.
    #
    # `chunks` is therefore (text, offset_ms) all the way down, with offset None
    # for everything that is not a recording — one path through the writer rather
    # than two, because two would drift.
    segments = payload.get("segments") if tool in STT_TOOLS else None
    if segments:
        chunks = _chunk_segments(segments)
    else:
        chunks = [(c, None) for c in _chunk_text(text)]

    # REPLACE, never append: a second run must not double the document in search.
    #
    # ONE TRANSACTION. The delete and the inserts were separate statements, so a
    # failure part-way through left the previous extraction gone and the new one
    # half-written — the one state worse than either. Either the document's text
    # is wholly replaced or nothing moves.
    #
    # A promoted chunk is counted and reported rather than silently dropped: the
    # entity_memory it was promoted into survives, but its promoted_to link back
    # here does not, and that is a thing the user should be told rather than
    # discover.
    tsv = _TSV_SQL.format(title="$5", content="$2")
    written = []
    async with pool.acquire() as conn:
      async with conn.transaction():
        promoted = await conn.fetchval(
            "SELECT count(*) FROM user_context WHERE entity_id = $1 "
            "AND media_parent_id = $2 AND source = $3 AND status = 'promoted'",
            entity_id, ctx_id, EXTRACT_SOURCE)
        replaced = await conn.execute(
            "DELETE FROM user_context WHERE entity_id = $1 AND media_parent_id = $2 "
            "AND source = $3", entity_id, ctx_id, EXTRACT_SOURCE)
        for i, (chunk, offset_ms) in enumerate(chunks):
            cmeta = {
                "kind": "text",
                "extracted_from": ctx_id,
                "extracted_by": tool,
                "extracted_on": server,
                "from_file": title,
                "chunk": i,
                "chunks": len(chunks),
                "truncated": bool(payload.get("truncated")),
            }
            if upload_batch:
                cmeta["upload_batch"] = upload_batch
            # WHICH MODEL HEARD THIS, on the row itself and not only in the
            # reply. base.en and large-v3-turbo are 3x apart in error rate on
            # hard audio and their transcripts look identical, so a user who
            # later finds a wrong name in search can see what read it — and a
            # re-run with a better model is a decision they can make on
            # evidence rather than on faith. It is the transcript's equivalent
            # of ocr_lang above, and it matters more.
            if tool in STT_TOOLS:
                cmeta["stt_model"] = str(payload.get("model") or "")
                if offset_ms is not None:
                    cmeta["offset_ms"] = int(offset_ms)
            # HOW the words were obtained, recorded on the row rather than only
            # in the reply. An OCR transcription is a reading of a picture and
            # can be wrong in ways a text layer cannot; a user who later finds a
            # garbled name in search deserves to see that it was OCR'd, in which
            # language, without re-running anything.
            if payload.get("pages_ocr"):
                cmeta["ocr_pages"] = int(payload.get("pages_ocr") or 0)
                cmeta["ocr_lang"] = str(payload.get("lang") or "")
            # THE TITLE CARRIES THE TIMESTAMP for a recording, because the title
            # is what the lexical arm indexes and what a search result shows —
            # so "jump to 12:04" is readable even before anything is embedded.
            ctitle = (f"{title} ({_mmss(offset_ms)})" if offset_ms is not None
                      else f"{title} (text {i + 1}/{len(chunks)})")
            new_id = await conn.fetchval(f"""
                INSERT INTO user_context
                    (entity_id, content, source, source_id, title, tags, metadata,
                     media_parent_id, media_offset_ms, search_tsv)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, {tsv})
                RETURNING id
            """, entity_id, chunk, EXTRACT_SOURCE, f"{ctx_id}:{i}",
                 ctitle, tags, json.dumps(cmeta), ctx_id, offset_ms)
            if new_id is not None:
                written.append(int(new_id))

    # QUEUE THE FILL. The rows qualify for the text lane the moment they are
    # written, but nothing embeds them until a job runs — so the old note,
    # "the embedding job picks it up like any other text", was true only for a
    # user who happened to run one. Extraction that leaves the text unsearchable
    # has not finished the job it was asked to do.
    queued, not_queued = None, None
    try:
        if not written:
            raise RuntimeError("no text rows were written")
        from server.routes import embed as _embed
        job = await _embed.enqueue_job(
            entity_id=entity_id, job_type="embed_context", target="user_context",
            metadata={"ids": written})
        queued = (job or {}).get("id")
    except Exception as e:
        # Not fatal: the text IS filed and findable by its words immediately,
        # because it went into the lexical index on insert. Carry the REASON —
        # a host with no embedding model is in a setup state, not a fault
        # and saying "could not be queued just now" about that would
        # describe a settled choice as a transient error.
        not_queued = str(e)
        logger.info("context extract: text written but embed job not queued (%s)", e)

    return {
        "ok": True, "ctx_id": ctx_id, "server": server, "tool": tool,
        "transport": transport,
        "chunks": len(written), "chars": len(text), "row_ids": written,
        "replaced": replaced, "replaced_promoted": int(promoted or 0),
        "embed_job_id": queued,
        # Said out loud rather than left for the user to notice: the box caps how
        # much text one job returns, and a document past it is flagged, not lied
        # about.
        "truncated": bool(payload.get("truncated")),
        "chars_total": payload.get("chars_total"),
        # THE SCANNED-PDF RECORD, said out loud. pages_more means the document
        # runs past what one job reads (30 pages, the box's ocrPageCap) and the
        # rest was NOT read — silently returning part of a contract is the same
        # failure as returning none of it. deadline means the clock ran out
        # mid-document and this text is what was read before it did.
        "pages": payload.get("pages"),
        "pages_ocr": payload.get("pages_ocr"),
        "pages_more": bool(payload.get("pages_more")),
        "pages_unread": payload.get("pages_unread"),
        "deadline": bool(payload.get("deadline")),
        "lang": payload.get("lang"),
        # ── THE TRANSCRIPT RECORD ──────────────────────────────────────────
        #
        # `model` is the one field this whole job is arranged around: it is what
        # keeps the record from claiming an accuracy the run did not have.
        # audio_ms against transcribed_ms is how much of the recording is in the
        # corpus against how much of it there was, and audio_capped says the
        # difference is a cap rather than a fault.
        "model": payload.get("model"),
        "audio_ms": payload.get("audio_ms"),
        "transcribed_ms": payload.get("transcribed_ms"),
        "audio_capped": bool(payload.get("audio_capped")),
        "demuxed": bool(payload.get("demuxed")),
        "embed_not_queued": not_queued,
        "note": _lang_note(stt_lang, payload) + _partial_note(payload) + (
            # A transcript's rows carry the moment each one was said at, which
            # is the thing a document's rows cannot have.
            "" if tool not in STT_TOOLS else
            f"transcribed by {payload.get('model') or 'this machine'}; every piece "
            f"keeps the moment it was said at, so a search result opens the "
            f"recording at that point. ") + (
            "extracted text is ordinary context now, and an embedding job was "
            "queued for it" if queued else
            "extracted text is filed and already findable by its words; "
            "it is not embedded yet because: " + (not_queued or "no job was queued")),
    }


async def _read_in_host(pool, entity_id: int, ctx_id: int, row, meta: dict,
                        mime: str, data: Optional[bytes]) -> dict:
    """Read ONE stored file with the host's own reader and file its text.
    Deterministic: bytes in, text out, rows written — no model is called."""
    import asyncio
    import base64
    if data is None:
        content = row["content"]
        if (row["storage_backend"] or "pg") != "pg":
            try:
                from brain.mastercode import context_backends
                content = await context_backends.retrieve_user_context_content(
                    pool, entity_id, ctx_id, row["storage_backend"])
            except Exception as e:
                logger.info("host read: rehydrate failed for %s (%s)", ctx_id, e)
                content = None
        if not content:
            return {"error": "this file's bytes are held on a machine that isn't "
                             "checked in right now", "ctx_id": ctx_id}
        try:
            data = base64.b64decode(content)
        except (ValueError, TypeError):
            return {"error": "this file's stored bytes are not readable", "ctx_id": ctx_id}
    try:
        # A reader is CPU work on arbitrary bytes: off the event loop, bounded
        # by the reader's own per-document budget, and at most
        # HOST_READ_CONCURRENCY at once so a burst of uploads cannot starve
        # the host's own turns.
        # The slot is held by the THREAD, not by this coroutine: a cancelled
        # request stops waiting, but the read runs on, so its slot is freed
        # only when the read itself ends — never more than the cap in flight.
        slots = _host_read_slots()
        await slots.acquire()
        try:
            job = asyncio.ensure_future(asyncio.to_thread(_docread.read, data, mime))
        except BaseException:
            slots.release()
            raise
        def _done(f):
            slots.release()
            if not f.cancelled():
                f.exception()          # retrieved: a read abandoned by a cancel is not "never retrieved"
        job.add_done_callback(_done)
        reader, text = await asyncio.shield(job)
    except _docread.Unreadable as e:
        return {"error": str(e), "ctx_id": ctx_id, "tool": HOST_TOOL}
    # An empty result still goes through the writer: it REPLACES, so text from
    # an earlier read of this file does not outlive a read that found none.
    title = meta.get("from_file") or row["title"] or f"context {ctx_id}"
    res = await _file_text(pool, entity_id, ctx_id, title=title,
                           tags=list(row["tags"] or []), text=(text or "").strip(),
                           payload={}, tool=HOST_TOOL, server="host",
                           transport="host", upload_batch=meta.get("upload_batch"))
    res["reader"] = reader
    if not res.get("chunks"):
        res["note"] = "the file holds no text"
    return res
