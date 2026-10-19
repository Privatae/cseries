#!/usr/bin/env python3
"""Gates for speech to text — the host half.

A clip's audio is more searchable than its frames: someone hunting "the demo
where we talked about pricing" wants the transcript, not a keyframe. What the
HOST decides for this lane is which mime goes to which job, how a transcript's
timestamps become rows a search result can jump to, and what a user is told when
the answer comes back empty — so these checks are about the routing, the
chunking, and the SENTENCES, which is where this lane can lie.

THE TRANSCRIPTION ITSELF IS PROVEN WHERE IT RUNS: the gateway's
client-go/contextstt_test.go drives real audio through real whisper-cli, over a
real ctx_extract management frame, on the PULL transport, and asserts the words
come back with their timestamps and the model that produced them.

THREE THINGS THIS FILE PINS THAT NOTHING ELSE DOES:

  1. A RECORDING'S EMPTY ANSWER IS NOT A SCAN'S. "this document has no text
     layer — it is a scan" is the right sentence for a PDF and nonsense for a
     voice memo, and the OCR branch would have caught it by falling through.

  2. WHICH MODEL RAN IS ON THE ROW. base.en and large-v3-turbo are 3x apart in
     word error rate on hard audio and their transcripts look identical, so a
     transcript with no model on it claims an accuracy nobody can check.

  3. A TRANSCRIPT CHUNKS ON ITS OWN SEGMENTS, not on a character window. The
     window is right for a document, where a character offset means nothing to
     anyone, and wrong for a recording, where every word happened at a time.

Run: APP_ROOT=<repo> python3 scripts/test_stt_lane.py
"""
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

failures = []
checks = 0


def check(label, cond, extra=""):
    global checks
    checks += 1
    print(("  ok    " if cond else "  FAIL  ") + label +
          (("\n        " + str(extra)) if extra and not cond else ""))
    if not cond:
        failures.append(label)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


extract_py = read("server/routes/context_extract.py")

from server.routes import context_extract as ce  # noqa: E402

print("\n── 1. audio routes to the job that can actually open it ──")

# The engine reads these four BY ITSELF — whisper-cli's own help prints
# "supported audio formats: flac, mp3, ogg, wav" (measured against the binary).
for mime in ("audio/wav", "audio/mpeg", "audio/flac", "audio/ogg"):
    check(f"{mime} → transcribe", ce.tool_for_mime(mime) == "transcribe",
          ce.tool_for_mime(mime))

# AND THE CONTAINERS DO NOT. An m4a is an MPEG-4 container; whisper-cli fails on
# it. Mapping it to the plain job would route a user's voice memo to something
# that refuses it — the same mistake as mapping HEIC to the OCR engine.
for mime in ("audio/mp4", "audio/x-m4a", "audio/aac"):
    check(f"{mime} → av-transcribe (it needs an ffmpeg demux first)",
          ce.tool_for_mime(mime) == "av-transcribe", ce.tool_for_mime(mime))

check("a file type nothing here reads is still told so plainly",
      ce.tools_for_mime("application/x-nonsense") == ())

print("\n── 2. a clip has words AND pictures, and the words come first ──")

# A clip's audio is more searchable than its frames, because someone hunting
# 'the demo where we talked about pricing' wants the transcript, not a keyframe.
clip = ce.tools_for_mime("video/mp4")
check("an mp4 supports more than one job", len(clip) >= 1, clip)
check("and its DEFAULT is the transcript", clip[0] == "av-transcribe", clip)
check("a clip's transcript and its frames are different SOURCES, so running one "
      "never deletes the other",
      ce.EXTRACT_SOURCE != getattr(ce, "FRAME_SOURCE", "frames"))

print("\n── 3. an empty transcript is not an empty scan ──")

# The OCR branch's sentences are about text layers and language packs. A user
# whose silent recording came back empty must not be sent to install tesseract.
m = re.search(r"if tool in STT_TOOLS:(.{0,2600}?)\n\n", extract_py, re.S)
stt_branch = m.group(1) if m else ""
check("_no_text_note forks for a recording BEFORE the scan sentences",
      "no text layer" not in stt_branch and "tesseract" not in stt_branch,
      stt_branch[:300])
check("silence is a FINISHED job and says the recording is silent",
      "silent, not unreadable" in extract_py)
check("engine-present-and-blocked is its own sentence with the box's own reason",
      "stt_blocked_reasons" in extract_py and
      "IS \n" not in extract_py and "installed on your machine(s) and cannot run" in extract_py)
check("and a transcript that really is empty names the bigger model, with the "
      "measurement rather than an adjective",
      "3.84%" in extract_py and "12.55%" in extract_py)

print("\n── 4. which model ran is recorded, never inferred ──")

check("the model is written onto every row, not only into the reply",
      '"stt_model"' in extract_py)
check("and into the reply",
      '"model": payload.get("model")' in extract_py)
check("a model the machine does not have is REFUSED with the list, not substituted",
      "is not substituted quietly" in extract_py or
      "not substituted" in extract_py)
check("and the list of models comes from the MACHINE's own advertisement, "
      "never from a table here",
      "_tool_models" in extract_py and "prepToolReport.Models" in extract_py)

# There must be no model table on the host. The box publishes on its own
# schedule; a mirror here is wrong the first time a user downloads a model.
check("no hardcoded model list on the host",
      "large-v3-turbo\"" not in extract_py.replace(
          "large-v3-turbo scores", ""))

print("\n── 5. chunking keeps the moment ──")

segs = [{"t0": 0, "t1": 2000, "text": "a" * 900},
        {"t0": 2000, "t1": 4000, "text": "b" * 900},
        {"t0": 4000, "t1": 6000, "text": "c" * 50}]
chunks = ce._chunk_segments(segs)
check("segments group into chunks rather than one row each", len(chunks) == 2, chunks and len(chunks))
check("every chunk carries the moment its FIRST word was said at",
      all(c[1] is not None for c in chunks), chunks)
check("the first chunk starts at the recording's start", chunks[0][1] == 0, chunks[0][1])
check("the second starts where its first segment does", chunks[1][1] == 2000, chunks[1][1])

# A SEGMENT IS NEVER SPLIT. Half a segment has no honest offset to carry, and
# losing the user's words to a size rule would be the deletion failure this whole
# job is designed around, arriving from our side.
joined = "".join(c[0] for c in chunks).replace("\n", "")
check("no word is lost to the size rule",
      joined == "a" * 900 + "b" * 900 + "c" * 50, len(joined))

big = ce._chunk_segments([{"t0": 7000, "t1": 9000, "text": "x" * 5000}])
check("a single oversize segment is its own chunk rather than being dropped",
      len(big) == 1 and len(big[0][0]) == 5000, big and len(big[0][0]))
check("and it keeps its moment", big[0][1] == 7000, big[0][1])

check("an empty transcript chunks to nothing", ce._chunk_segments([]) == [])
check("a malformed segment does not take the whole transcript down",
      ce._chunk_segments([None, {"text": ""}, {"t0": "x", "text": "hi"}]) ==
      [("hi", 0)])

check("the row carries media_offset_ms, which is what a jump reads",
      "media_offset_ms" in extract_py and "$9" in extract_py)
check("and the TITLE carries the timestamp, so a hit is readable before "
      "anything is embedded",
      "_mmss(offset_ms)" in extract_py)
check("m:ss under an hour, h:mm:ss over it",
      ce._mmss(64000) == "1:04" and ce._mmss(3723000) == "1:02:03",
      (ce._mmss(64000), ce._mmss(3723000)))

print("\n── 6. a partial recording says how much of it is missing ──")

note = ce._transcript_partial_note(
    {"model": "base.en", "audio_capped": True, "transcribed_ms": 3600000})
check("a capped recording says the rest is NOT in the corpus",
      "NOT in your corpus" in note, note)
note = ce._transcript_partial_note(
    {"model": "base.en", "deadline": True, "transcribed_ms": 600000,
     "audio_ms": 3000000})
check("a deadline names how far it got AND how long the recording was",
      "10:00" in note and "50:00" in note, note)
check("and it never talks about PAGES", "page" not in note.lower(), note)
check("a complete recording gets no partial note at all",
      ce._transcript_partial_note(
          {"model": "base.en", "transcribed_ms": 60000, "audio_ms": 60000}) == "")
# _partial_note must route a recording to the transcript wording rather than to
# the page wording, or an hour-long meeting is reported as "0 page(s)".
check("_partial_note forks on the transcript before the page branches",
      "page" not in ce._partial_note(
          {"model": "base.en", "deadline": True, "transcribed_ms": 1000}).lower())

print("\n── 7. nothing here transcribes anything platform-side ──")

# Speech is an engine the Host does not carry: a recording is transcribed on
# one of the user's machines (documents, by contrast, the Host reads itself —
# server/readers). This file names audio mime types and must never
# name something that decodes one.
# PROSE ABOUT ffmpeg IS FINE AND IS THE POINT — this file has to explain why an
# mp4 needs it ON THE USER'S MACHINE. What must not exist is an import of one, or
# a subprocess call, because either would mean the platform is decoding the
# user's recording. So the check reads the IMPORTS and the exec surface, not the
# comments; the substring version of this check fired on its own documentation,
# which is how a gate gets bypassed.
BANNED = ("whisper", "ffmpeg", "ffprobe", "pydub", "librosa", "soundfile",
          "torchaudio", "vosk", "speech_recognition", "av", "wave", "audioop")
import_lines = [ln.strip() for ln in extract_py.splitlines()
                if re.match(r"\s*(import|from)\s", ln)]
found = [(ln, b) for ln in import_lines for b in BANNED
         if re.search(rf"\b{re.escape(b)}\b", ln)]
check("the host route IMPORTS no audio library — it routes, the box listens",
      not found, found)
check("and it starts no process of its own",
      not re.search(r"\b(subprocess|os\.system|Popen|os\.exec)", extract_py),
      [ln for ln in extract_py.splitlines() if "subprocess" in ln])
check("and the standing gate that enforces that across every image still passes",
      os.system(f"cd {ROOT} && python3 scripts/check_no_platform_extractors.py "
                f">/dev/null 2>&1") == 0)

print("\n── 8. the ceiling still comes from the box, not from here ──")

# Transcription is the job the ceiling mechanism was built for: an hour of audio
# is 75 seconds of work at base.en on eight cores and fifteen minutes at
# large-v3-turbo, so no number on this host could bound it honestly.
check("a machine's declared ceiling is read from its own advertisement",
      ce.declared_ceiling({"prep_tools": [{"name": "transcribe", "ceiling_s": 900}]},
                          "transcribe") == 900)
check("a machine declaring more than this host will park a worker for is clamped",
      ce.declared_ceiling({"prep_tools": [{"name": "transcribe", "ceiling_s": 99999}]},
                          "transcribe") == ce.MAX_DECLARED_CEILING_S)
check("and a bastion that predates the field still gets a bounded job",
      ce.declared_ceiling({}, "transcribe") == ce.UNDECLARED_CEILING_S)

print("\n── 9. the whole host half, driven on a REAL box reply ──")

# THE REPLY BELOW IS NOT A MOCK. It is the exact JSON the gateway's
# ctx_extract returned, over the PULL transport, from real whisper-cli reading
# real speech on this machine (contextstt_test.go's fixture, base.en, the JFK
# clip). Captured verbatim so the piece this file owns — reply to rows — is
# exercised against what the box actually sends rather than against what this
# file imagines it sends. A hand-written payload here would agree with a
# hand-written parser for ever.
REAL_BOX_REPLY = {
    "tool": "transcribe",
    "source": "pull",
    "chars": 107,
    "text": "And so my fellow Americans, ask not what your country can do for you, "
            "ask what you can do for your country.",
    "model": "base.en",
    "segments": [{"t0": 0, "t1": 11000,
                  "text": "And so my fellow Americans, ask not what your country "
                          "can do for you, ask what you can do for your country."}],
    "audio_ms": 11000,
    "transcribed_ms": 11000,
}

import asyncio            # noqa: E402
import json as _json      # noqa: E402
import types              # noqa: E402

ENTITY = 7
CTX_ID = 500
MACHINE = {
    "server": "laptop.example", "token_id": 11,
    "tools": ["transcribe", "av-transcribe"],
    "prep_tools": [{"name": "transcribe", "bin": "whisper-cli", "ceiling_s": 900,
                    "models": ["large-v3-turbo", "base.en"]},
                   {"name": "av-transcribe", "bin": "whisper-cli", "ceiling_s": 900,
                    "models": ["large-v3-turbo", "base.en"]}],
    "scan": {"supported": True, "probed": 6, "found": 2, "pull": True},
    "pull": True, "max_bytes": ce.PULL_MAX_BYTES,
}


class _Conn:
    """Captures every insert so the rows can be asserted rather than assumed."""

    def __init__(self, sink):
        self.sink = sink

    def transaction(self):
        conn = self

        class _T:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _T()

    async def execute(self, sql, *args):
        self.sink["deleted"] = args
        return "DELETE 0"

    async def fetchval(self, sql, *args):
        if "count(*)" in sql:
            return 0
        self.sink["rows"].append(args)
        return 900 + len(self.sink["rows"])


class _Pool:
    def __init__(self, sink, row):
        self.sink, self.row = sink, row

    async def fetchrow(self, sql, *args):
        return self.row if "FROM user_context" in sql else None

    def acquire(self):
        conn = _Conn(self.sink)

        class _A:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _A()


async def _drive(reply, *, mime="audio/wav", tool=None, model=None, lang=None):
    """Run extract_context_file against a machine that answers with `reply`."""
    sink = {"rows": [], "dispatched": None}
    row = {"id": CTX_ID, "title": "standup.wav", "content": "",
           "storage_backend": "pg", "tags": ["project:3"],
           "metadata": _json.dumps({"kind": "file", "mime": mime,
                                    "size_bytes": 352_078})}
    pool = _Pool(sink, row)

    async def _machines(p, eid, t=None):
        if t is None or t in MACHINE["tools"]:
            return [dict(MACHINE)]
        return []

    class _Relay:
        def worker_count(self, tid):
            return 1

        async def dispatch(self, tid, op, args, wait, approval=""):  # (+ approval since 0.33.0)
            sink["dispatched"] = _json.loads(args[0])
            return (_json.dumps(reply), "", 0)

    sys.modules.setdefault("server.main", types.ModuleType("server.main"))
    sys.modules["server.network.nac_mgmt"] = types.ModuleType("server.network.nac_mgmt")
    sys.modules["server.network.nac_mgmt"].get_mgmt_relay = lambda: _Relay()
    # The signed-grant gate grants every machine here;
    # test_gate_the_ungated.py is where a missing grant refuses.
    async def _gate_servers(p, eid, op, servers):
        return {str(sv): (True, "", "ran") for sv in servers if sv}
    sys.modules["server.network.nac_mgmt"].gate_servers = _gate_servers
    async def _audit(*a, **k):
        return None
    sys.modules["server.network.nac_mgmt"].audit_mgmt = _audit
    sys.modules["server.network.context_tickets"] = types.ModuleType(
        "server.network.context_tickets")
    sys.modules["server.network.context_tickets"].mint = lambda **k: "f" * 64
    _embed = types.ModuleType("server.routes.embed")

    async def _enqueue(**k):
        sink["embed_ids"] = k.get("metadata", {}).get("ids")
        return {"id": 42}
    _embed.enqueue_job = _enqueue
    sys.modules["server.routes.embed"] = _embed

    real = ce.machines_for
    ce.machines_for = _machines
    try:
        res = await ce.extract_context_file(pool, ENTITY, CTX_ID,
                                            tool=tool, model=model, lang=lang)
    finally:
        ce.machines_for = real
    return res, sink


res, sink = asyncio.run(_drive(REAL_BOX_REPLY))

check("the box's real reply files rows without error", not res.get("error"), res)
check("and the job actually went out over the PULL transport",
      res.get("transport") == "pull", res.get("transport"))
check("one row per chunk of the transcript", res.get("chunks") == 1, res)
check("transcript rows carry the source file's tags (a project upload stays in its project)",
      bool(sink["rows"]) and all(r[5] == ["project:3"] for r in sink["rows"]),
      [r[5] for r in sink["rows"]])
check("the reply says which model heard it", res.get("model") == "base.en", res)
check("and how much of the recording is in the corpus",
      res.get("audio_ms") == 11000 and res.get("transcribed_ms") == 11000, res)
check("a complete recording is not described as partial",
      "NOT in your corpus" not in (res.get("note") or ""), res.get("note"))
check("the note names the model and the jump",
      "base.en" in (res.get("note") or "") and
      "moment" in (res.get("note") or ""), res.get("note"))
check("the rows were queued for embedding, so the transcript is searchable",
      sink.get("embed_ids") == res.get("row_ids"), (sink.get("embed_ids"), res.get("row_ids")))

# THE ROW ITSELF. Positional params, in the order the INSERT declares them:
# entity, content, source, source_id, title, tags, metadata, parent, offset.
row = sink["rows"][0]
check("the row's content is the user's words", "fellow Americans" in row[1], row[1][:60])
check("it hangs off the recording it came from", row[7] == CTX_ID, row[7])
check("it carries media_offset_ms, which is what a jump reads",
      row[8] == 0, row[8])
meta = _json.loads(row[6])
check("the row says WHICH MODEL heard it — not only the reply does",
      meta.get("stt_model") == "base.en", meta)
check("and it is kind='text', so the lane that already embeds text picks it up "
      "with no changes",
      meta.get("kind") == "text", meta)
check("the title carries the timestamp for the lexical arm",
      "0:00" in row[4], row[4])
check("the replace-set is the transcript's own source, so a clip's frames "
      "survive a re-transcription",
      sink["deleted"][2] == ce.EXTRACT_SOURCE, sink.get("deleted"))

# ── the ceiling and the model actually reach the wire ──
job = sink["dispatched"]
check("the job carries the ceiling THIS MACHINE declared, not a number from here",
      job.get("extract_budget") == 900.0, job)
check("and no model when none was asked for, so the box uses its best",
      "model" not in job, job)

res2, sink2 = asyncio.run(_drive(REAL_BOX_REPLY, model="large-v3-turbo"))
check("a model the machine advertises reaches the box by name",
      sink2["dispatched"].get("model") == "large-v3-turbo", sink2["dispatched"])

res3, _ = asyncio.run(_drive(REAL_BOX_REPLY, model="whisper-enormous"))
check("a model the machine does NOT advertise is refused before the source moves",
      "no speech model called" in (res3.get("error") or ""), res3)
check("and the refusal lists what that machine actually has",
      "base.en" in (res3.get("error") or ""), res3)

# ── silence, end to end ──
res4, sink4 = asyncio.run(_drive({
    "tool": "transcribe", "source": "pull", "chars": 0, "text": "",
    "model": "tiny.en", "blank": True, "audio_ms": 5000}))
check("a silent recording is a FINISHED job, not an error", res4.get("ok"), res4)
check("nothing is filed for it", res4.get("chunks") == 0, res4)
check("and the user is told it is silent rather than unreadable",
      "silent, not unreadable" in (res4.get("note") or ""), res4.get("note"))
check("named with the model that listened, so a re-run with a better one is a "
      "decision they can make",
      "tiny.en" in (res4.get("note") or ""), res4.get("note"))

# ── sound that is not speech, end to end ──
#
# The box now tells the difference between a silent recording and one with
# sound in it that nobody spoke over. The host has to keep them apart, because
# calling a song silent is a false statement about the user's own file — and
# because the branch it falls through to otherwise tells them a larger model
# reads quiet audio better, i.e. sends someone who uploaded music to download
# 1.6 GB and transcribe it again for the same nothing.
res4b, _ = asyncio.run(_drive({
    "tool": "transcribe", "source": "pull", "chars": 0, "text": "",
    "model": "base.en", "non_speech": True, "audio_ms": 10000}))
check("a recording with sound but no speech is a FINISHED job",
      res4b.get("ok"), res4b)
check("nothing is filed for it — the engine's name for a sound is not a word "
      "anybody said", res4b.get("chunks") == 0, res4b)
check("the state survives this seam instead of being dropped like B2's fields",
      res4b.get("non_speech") is True, res4b)
check("it is NOT reported as silent, because the recording is not silent",
      "silent, not unreadable" not in (res4b.get("note") or ""), res4b.get("note"))
check("the user is told there is sound in it and no speech",
      "sound" in (res4b.get("note") or "") and "no speech" in (res4b.get("note") or ""),
      res4b.get("note"))
check("and is NOT sent to download a bigger model to re-read their music",
      "large-v3-turbo" not in (res4b.get("note") or ""), res4b.get("note"))
check("silence and a named sound stay two different sentences",
      (res4.get("note") or "") != (res4b.get("note") or ""), res4b.get("note"))

# ── a meeting that ran out of clock ──
res5, _ = asyncio.run(_drive({
    "tool": "transcribe", "source": "pull", "chars": 20,
    "text": "the first ten minutes",
    "segments": [{"t0": 0, "t1": 600000, "text": "the first ten minutes"}],
    "model": "base.en", "deadline": True,
    "audio_ms": 3000000, "transcribed_ms": 600000}))
check("a partial transcript is still FILED — the words already heard are the "
      "user's", res5.get("chunks") == 1, res5)
check("and it says how far it got and how long the recording was",
      "10:00" in (res5.get("note") or "") and "50:00" in (res5.get("note") or ""),
      res5.get("note"))
check("in the words 'NOT in your corpus', so the gap is not discovered later",
      "NOT in your corpus" in (res5.get("note") or ""), res5.get("note"))

# ── a clip: the words by default, the pictures by name ──
res6, sink6 = asyncio.run(_drive(REAL_BOX_REPLY, mime="video/mp4"))
check("an mp4 goes to its TRANSCRIPT by default",
      sink6["dispatched"].get("tool") == "av-transcribe", sink6["dispatched"])
res7, _ = asyncio.run(_drive(REAL_BOX_REPLY, mime="video/mp4", tool="ocr"))
check("and a job that is not one of that file's own is refused, with the list",
      "is not a job this host runs" in (res7.get("error") or ""), res7)

# ── the language: named by someone, never guessed, never assumed to have landed ─
#
# The engine's default is English and every transcript this lane has ever filed
# ran under it. A host that picked a language for the user would be choosing new
# behaviour from a proxy nobody measured — the benchmark behind the model ranking
# covered the English-only models plus large-v3-turbo ON ENGLISH AUDIO, and this
# host stores no language for a user anyway (behavioral_params carries a
# timezone). So the language comes from the caller, and the reply is what says
# whether the machine could act on it.
print("\n── 8. the language is named, not guessed ──")

_no_lang = asyncio.run(_drive(REAL_BOX_REPLY))[1]["dispatched"]
check("nothing invents a language: a job with none named carries no `lang` at all",
      "lang" not in _no_lang, _no_lang)
check("and the host does not reach for 'auto' on its own either",
      _no_lang.get("lang") != "auto", _no_lang)

_de = asyncio.run(_drive({**REAL_BOX_REPLY, "lang": "de"}, lang="de"))
check("a language the caller names travels on the wire",
      _de[1]["dispatched"].get("lang") == "de", _de[1]["dispatched"])
check("and a machine that honoured it says nothing extra about it",
      "did not reach this machine" not in (_de[0].get("note") or ""),
      _de[0].get("note"))

# THE OLD-BOX CASE, which is the one the host has to catch by itself: `lang` on
# the request is newer than the published bastion and Go drops an unknown JSON
# field without a word, so the box transcribes English and answers as if nothing
# was asked. The reply's own `lang` is the only evidence either way.
_old = asyncio.run(_drive(REAL_BOX_REPLY, lang="de"))
check("a bastion too old to be told a language is SAID to be, not assumed to "
      "have obeyed",
      "did not reach this machine" in (_old[0].get("note") or ""),
      _old[0].get("note"))
check("and the transcript is still filed — the words are the user's either way",
      _old[0].get("chunks") == 1, _old[0])

_other = asyncio.run(_drive({**REAL_BOX_REPLY, "lang": "en"}, lang="de"))
check("a machine that read it in something else than what was asked says which",
      "read this in en" in (_other[0].get("note") or ""), _other[0].get("note"))

_bad = asyncio.run(_drive(REAL_BOX_REPLY, lang="Deutsch, bitte"))[0]
check("a string that is not a language tag is refused before anything is "
      "dispatched", "is not a language tag" in (_bad.get("error") or ""), _bad)
_wrongjob = asyncio.run(_drive(REAL_BOX_REPLY, mime="application/pdf",
                               tool="pdf-text", lang="de"))[0]
check("and a job that reads no spoken language refuses to be given one",
      "does not read a spoken language" in (_wrongjob.get("error") or ""),
      _wrongjob)

print()
if failures:
    print(f"FAIL: {len(failures)} of {checks} check(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS: {checks} checks — audio reaches the job that can open it, a clip's "
      f"words come before its pictures, every chunk keeps the moment it was said "
      f"at, and the model that heard it is on the record")
