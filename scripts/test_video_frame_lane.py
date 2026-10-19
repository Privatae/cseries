#!/usr/bin/env python3
"""Gate: the video frame lane keeps the two things it was built on.

A clip becomes searchable by having FRAMES pulled on the user's own machine and
embedded here. Two decisions hold that together, and both are the kind that can
silently stop being true:

 1. THE HOST'S WAIT COMES FROM THE BOX, never from a copy of the box's number.
    One extraction ceiling cannot bound both a PDF text-layer read and a long
    video decode, so the ceiling is per-tool — and a per-tool table here beside
    a per-tool table in client-go is the defect the pull path spent a session
    removing. The moment a bastion ships a tool with a longer ceiling than this
    repo's copy knows about, the HOST gives up first: it reports a machine that
    is working perfectly as unresponsive and discards the output when it
    arrives. So each tool declares its own ceiling in the prep advertisement,
    the host reads it off the machine it is dispatching to, and waits that plus
    one allowance.

 2. THE DECODER IS THE USER'S. ffmpeg may never enter a platform image, which
    scripts/check_no_platform_extractors.py enforces directly; what THIS gate
    checks is the other half — that nothing on the host ever tries to decode,
    and that a clip with nowhere to put its vectors is refused BEFORE someone's
    laptop spends CPU on it.

Repo-side: no pod, no DB, no network, no ffmpeg.

Run: APP_ROOT=<repo> python3 scripts/test_video_frame_lane.py
"""
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT", "/app")
sys.path.insert(0, os.path.join(ROOT, "brain"))
sys.path.insert(0, ROOT)

failures = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  ok    {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label}" + (f"  — {detail}" if detail else ""))


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def flat(s: str) -> str:
    return re.sub(r"\s+", " ", s)


cx = read("server/routes/context_extract.py")
embed_py = read("server/routes/embed.py")
me = read("brain/mastercode/media_engine.py")

# ── 1. the ceiling is READ, not mirrored ────────────────────────────────────
#
# The MECHANISM is not this lane's — it landed with the OCR slice and lives in
# declared_ceiling / UNDECLARED_CEILING_S / MAX_DECLARED_CEILING_S. What this
# gate pins is the video-shaped property of it: a long clip's ceiling is the
# BOX's number, so a bastion that declares minutes for a decode is waited for
# even on a host that has never heard of the job. That is the exact case a
# mirrored table cannot serve, and video is the first job big enough to prove it.
from server.routes.context_extract import (  # noqa: E402
    FRAME_TOOLS, MAX_DECLARED_CEILING_S, REPLY_ROUND_TRIP, UNDECLARED_CEILING_S,
    _reply_allowance, declared_ceiling, tool_for_mime,
)

_box = {"prep_tools": [{"name": "video-frames", "ceiling_s": 120},
                       {"name": "pdf-text", "ceiling_s": 20}]}
check("the host waits on the number the BOX published for the clip job",
      declared_ceiling(_box, "video-frames") == 120.0)
check("a job this host has never heard of is still bounded by the box's number",
      declared_ceiling({"prep_tools": [{"name": "transcribe", "ceiling_s": 600}]},
                       "transcribe") == 600.0)
check("and the host's wait is strictly longer than that, so the BOX is the one "
      "that gives up first",
      _reply_allowance(600.0) > 600.0
      and _reply_allowance(600.0) == 600.0 + REPLY_ROUND_TRIP)
check("a machine that declares nothing gets the text-layer number, never a "
      "long-job default",
      declared_ceiling({}, "video-frames") == UNDECLARED_CEILING_S)
check("a hostile or buggy declaration is clamped rather than parking a worker",
      declared_ceiling({"prep_tools": [{"name": "video-frames",
                                        "ceiling_s": 999999}]},
                       "video-frames") == MAX_DECLARED_CEILING_S)
check("a junk ceiling is not trusted and is not a crash",
      declared_ceiling({"prep_tools": [{"name": "video-frames",
                                        "ceiling_s": "soon"}]},
                       "video-frames") == UNDECLARED_CEILING_S)

# THE ONE THAT MATTERS. A map from tool name to a number of seconds, living in
# this repo, IS the mirrored table — however it is spelled.
_ceiling_table = re.search(r"^[A-Z_]*CEILING[A-Z_]*\s*=\s*\{", cx, re.M)
check("there is NO per-tool ceiling TABLE on the host — the number travels in "
      "the advertisement instead",
      _ceiling_table is None,
      _ceiling_table.group(0) if _ceiling_table else "")
check("...and no tool name is mapped to a literal number of seconds anywhere here",
      not re.search(r'["\'](?:pdf-text|video-frames|ocr|pdf-ocr)["\']\s*:\s*\d+', cx))
check("the clip's dispatch wait is built from the declared ceiling, not a "
      "constant chosen here",
      "declared_ceiling(chosen, tool)" in cx and "_reply_allowance(ceiling)" in cx)

# ── 2. video routes to the frame job ────────────────────────────────────────
# A clip has a SECOND job — its transcript — and that is the default: a clip's
# audio is more searchable than its frames, because someone hunting 'the demo
# where we talked about pricing' wants the transcript, not a keyframe. So what this pins is that the
# frame job is still REACHABLE for every one of these containers, which is what
# it was always protecting; tool_for_mime returning it FIRST was how that was
# expressed while frames were the only job a clip had.
from server.routes.context_extract import tools_for_mime  # noqa: E402

check("the common containers route to the frame job",
      all("video-frames" in tools_for_mime(m) for m in
          ("video/mp4", "video/quicktime", "video/webm", "video/x-matroska")))
check("a mime with a codecs parameter still routes",
      "video-frames" in tools_for_mime("video/mp4;codecs=avc1.640028"))
check("and a clip's DEFAULT is its transcript, with the frames behind it",
      tools_for_mime("video/mp4") == ("av-transcribe", "video-frames"),
      tools_for_mime("video/mp4"))
check("the frame job is named as one, so the reply reader forks on a set rather "
      "than on a guess about the payload",
      FRAME_TOOLS == {"video-frames"})
check("the reply forks BEFORE the text reader touches it",
      cx.index("if tool in FRAME_TOOLS:\n        return _with_fallback_note(await _file_frames(")
      < cx.index('text = (payload.get("text")'))

# ── 3. nothing on the host decodes ──────────────────────────────────────────
#
# Comments are stripped first, on purpose: this file NAMES ffmpeg when it
# explains where the decode happens, and a check that fired on the word would
# be teaching whoever hits it to stop writing the explanation rather than to
# stop writing the decode.
# STRIP DOCSTRINGS TOO, not just # lines. The reason above is about PROSE, and a
# docstring is prose — the line filter simply could not see it, so a function
# explaining that ffmpeg runs on the USER'S machine failed a check whose whole
# point is that it runs there. Parsing the module and removing every docstring
# makes this read executable code, which is what it always meant to read.
import ast as _ast  # noqa: E402


def _strip_docstrings(src: str) -> str:
    tree = _ast.parse(src)
    drop = set()
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.Module, _ast.FunctionDef,
                                 _ast.AsyncFunctionDef, _ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], _ast.Expr)
                and isinstance(body[0].value, _ast.Constant)
                and isinstance(body[0].value.value, str)):
            drop.update(range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1))
    return "\n".join(ln for n, ln in enumerate(src.splitlines(), 1)
                      if n not in drop and not ln.lstrip().startswith("#"))


_code = _strip_docstrings(cx)
for bad in ("ffmpeg", "ffprobe", "cv2", "imageio", "av.open", "moviepy",
            "subprocess"):
    check(f"the host never invokes {bad} — the decoder is the user's machine",
          bad not in _code)

# ── 4. the user's CPU is not spent on work with nowhere to go ───────────────
check("a clip is refused as a SETUP state when no media model is set, "
      "and that refusal reads as setup rather than as an outage",
      '"needs_setup": "media_embedding"' in cx
      and "outage" not in flat(cx).lower().split("needs_setup")[1][:400])
check("the check runs before the job is dispatched, not after the decode",
      cx.index("needs_setup") < cx.index("relay.dispatch"))
check("a transient config read failure is told apart from an unset role",
      "unreadable" in cx and "not a setup problem" in cx)

# ── 5. what comes back is reported honestly ────────────────────────────────
check("the box's own account of the clip travels back to the caller",
      '"report": report' in cx)
check("a frame the inline pass did not reach is PENDING, not failed — the "
      "distinction is the whole point of the retry lane",
      '"frames_pending"' in cx and '"frames_not_embedded"' not in cx)

# ── 6. THE RETRY LANE, which is the part with no second chance ──────────────
#
# A frame row is kind='frame'. NOT_FILE_KIND_SQL excludes it from every text
# selector — correct — and the media lane selects positively, so before this it
# selected kind='file' and a frame was in NEITHER population. The only thing
# that ever embedded one was the inline pass at extraction time, so a provider
# 502 on frame 180 of 240 left those frames unsearchable FOR EVER, repairable
# only by re-pulling the whole clip across the user's link.
#
# scripts/test_media_fill_finds_frames.py proves the repair against a real
# planner (it needs a container, so it is not in the container-free list). These
# are the source-level invariants it rests on, pinned HERE so a revert fails the
# container-free suite too.
check("the media fill owns frames as well as pictures, through ONE predicate",
      "media_embeddable_sql" in embed_py
      and "metadata->>'kind' IN ('file', 'frame')" in me)
check("...and the fill's fetch and its denominator both read that one predicate",
      flat(embed_py).count("media_embeddable_sql(media_col") >= 2)
check("a row the fill could do no work on is excluded rather than failed on "
      "every pass for ever",
      "length(content) > 0" in me)
check("the retry job is queued BEFORE the inline pass runs, so a throw in the "
      "fast path cannot take the slow path with it",
      cx.index("enqueue_job(entity_id, \"fill\", \"user_context_media\")")
      < cx.index("_embed_batch_media("))
check("the inline pass is wrapped, so its failure leaves staged rows for the "
      "fill instead of raising out of the route",
      "inline_error" in cx and "inline frame embed failed" in cx
      and "except Exception as e:" in cx.split("_embed_batch_media(")[1][:1400])
check("the code says plainly that the inline pass is an optimisation and not "
      "the guarantee",
      "AN OPTIMISATION, NEVER THE GUARANTEE" in cx)
check("re-running replaces the clip's frames rather than doubling them",
      "DELETE FROM user_context" in cx and "source = $3" in cx
      and "FRAME_SOURCE" in cx)

print()
if failures:
    print(f"FAIL: {len(failures)} of {checks} check(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS: {checks} checks — the video frame lane's contracts hold")
