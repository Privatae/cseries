"""Gates for chat attachments on EVERY provider route, and for keeping them.

2026-09-22 (kimi route): a file dropped into a chat reached the
model as an Anthropic-native `image` / `document` block. The OpenAI wire has
neither part, nothing converted them, and the provider refused the whole
turn. And the file was never kept: a later turn could not read it.

WHAT THIS FILE PINS, on real bytes (no provider is called — the payloads a
provider WOULD receive are asserted):

  1. OpenAI wire: a picture becomes an image_url data URI; a PDF, a .docx and
     a text file become text parts holding their words, read by the host's
     own readers; an unreadable file becomes one sentence saying why.
  2. Messages wire: a PDF passes through untouched (Anthropic takes base64
     PDFs); a .docx / text file is read to text (Anthropic does not take
     those as base64 documents); a picture passes through.
  3. The caller's messages are never mutated (a failover re-dials the other
     wire from them), and a message with no attachment is passed as-is.
  4. Injected text is bounded, and a cut says where the rest is.
  5. call_model's dial converts per wire (source check on _dial).
  6. The Circles route keeps every attachment on the Host through the upload
     lane, in the background, and marks it for the frame; a failed store
     never fails the send.

Run: APP_ROOT=<repo> python3 scripts/test_chat_attachments.py
"""
import asyncio
import base64
import copy
import io
import os
import sys
import types
import zipfile
import zlib
from turn_source import turn_source  # session.py and the modules split out of it

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

failures = []
checks = 0


def check(label, cond, extra=""):
    global checks
    checks += 1
    print(("  ok    " if cond else "  FAIL  ") + label +
          (("\n        " + str(extra)[:400]) if extra and not cond else ""))
    if not cond:
        failures.append(label)


def _pdf(content: bytes) -> bytes:
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Count 1 /Kids [4 0 R] >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"]
    raw = zlib.compress(content)
    out = b"%PDF-1.4\n"
    for i, o in enumerate(objs, 1):
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    return out + (b"5 0 obj\n<< /Filter /FlateDecode /Length %d >>\nstream\n" % len(raw)
                  + raw + b"\nendstream\nendobj\ntrailer << /Root 1 0 R >>\n%%EOF\n")


def _docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PNG = _b64(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)


def doc(data: bytes, media: str, title=None):
    d = {"type": "document", "source": {"type": "base64", "media_type": media, "data": _b64(data)}}
    if title:
        d["title"] = title
    return d


MESSAGES = [
    {"role": "system", "content": "you are a host"},
    {"role": "user", "content": "earlier turn, no attachment"},
    {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}},
        {**doc(_pdf(b"BT /F1 12 Tf 72 700 Td (Quiz Results) Tj ET"), "application/pdf", "quiz.pdf"),
         "_kept": True},
        doc(_docx("Quarterly plan"), DOCX, "plan.docx"),
        doc(b"plain notes here", "text/markdown", "notes.md"),
        doc(b"\x00\x01garbage", "application/octet-stream", "blob.bin"),
        {"type": "text", "text": "what do these say?"},
    ]},
]

from mastercode import attachment_parts as ap  # noqa: E402

ORIGINAL = copy.deepcopy(MESSAGES)

print("OpenAI wire (kimi, openai, openrouter, xai, local)")
oa = asyncio.run(ap.for_wire(MESSAGES, "openai"))
parts = oa[2]["content"]
types_ = [p.get("type") for p in parts]
check("no Anthropic-only part reaches the OpenAI wire",
      "image" not in types_ and "document" not in types_, types_)
check("the picture is an image_url data URI",
      parts[0] == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}}, parts[0])
check("the PDF is its words, named", parts[1]["type"] == "text"
      and "Attached document quiz.pdf" in parts[1]["text"] and "Quiz Results" in parts[1]["text"], parts[1])
check("the .docx is its words", "Quarterly plan" in parts[2]["text"], parts[2])
check("a markdown file is its words", "plain notes here" in parts[3]["text"], parts[3])
check("an unreadable file is one sentence saying why, and the turn goes on",
      parts[4]["type"] == "text" and "could not be read" in parts[4]["text"], parts[4])
check("the person's own words are kept, last", parts[5] == {"type": "text", "text": "what do these say?"})
check("messages without attachments are the same objects", oa[0] is MESSAGES[0] and oa[1] is MESSAGES[1])
check("the caller's messages are not mutated (failover re-dials from them)", MESSAGES == ORIGINAL)

print("\nMessages wire (anthropic)")
an = asyncio.run(ap.for_wire(MESSAGES, "anthropic"))
ap_parts = an[2]["content"]
check("the picture passes through", ap_parts[0] == MESSAGES[2]["content"][0])
check("the PDF passes through as a document (Anthropic takes base64 PDFs)",
      ap_parts[1] == {k: v for k, v in MESSAGES[2]["content"][1].items() if k != "_kept"})
check("no private key reaches either wire",
      not any(k.startswith("_") for m in (oa, an) for p in m[2]["content"] for k in p),
      [list(p) for p in an[2]["content"]])
check("a .docx is read to text (Anthropic takes no base64 docx)",
      ap_parts[2]["type"] == "text" and "Quarterly plan" in ap_parts[2]["text"], ap_parts[2])
check("a text file is read to text", ap_parts[3]["type"] == "text", ap_parts[3])

from mastercode import provider_gateway as pg  # noqa: E402
payload = pg._anthropic_messages_payload(an, "claude-x", 0.3, 100, None)
user_blocks = [m for m in payload.get("messages", []) if m.get("role") == "user"][-1]["content"]
check("the Messages payload carries the PDF document block with its title",
      any(b.get("type") == "document" and b.get("title") == "quiz.pdf" for b in user_blocks),
      [b.get("type") for b in user_blocks])

print("\nbounds")
big = [{"role": "user", "content": [{**doc(("word " * 60000).encode(), "text/plain", "big.txt"),
                                     "_kept": True}]}]
cut = asyncio.run(ap.for_wire(big, "openai"))[0]["content"][0]["text"]
check("one document is capped, and a KEPT file's cut says where the rest is",
      len(cut) < ap.PER_DOCUMENT_CHARS + 500 and "context_ingest op='read' file='big.txt'" in cut,
      len(cut))
big[0]["content"][0]["_kept"] = False
cut = asyncio.run(ap.for_wire(big, "openai"))[0]["content"][0]["text"]
check("a file that was not kept never points at a read that would find nothing",
      "op='read'" not in cut and "not kept on this Host" in cut, cut[-300:])
many = [{"role": "user", "content": [doc(("w " * 60000).encode(), "text/plain", f"f{i}.txt")
                                     for i in range(4)]}]
tot = sum(len(p["text"]) for p in asyncio.run(ap.for_wire(many, "openai"))[0]["content"])
check("several documents together stay within the per-turn cap",
      tot < ap.PER_TURN_CHARS + 4 * 600, tot)

print("\neach document is read once per content")
ap._READ_CACHE.clear()
ap.READS["n"] = 0
two = [{"role": "user", "content": [doc(_docx("alpha"), DOCX, "a.docx"),
                                    doc(_pdf(b"BT /F1 12 Tf 72 700 Td (beta) Tj ET"),
                                        "application/pdf", "b.pdf")]}]
for _round in range(5):
    for wire in ("openai", "anthropic"):
        asyncio.run(ap.for_wire(two, wire))
check("2 documents x 2 wires x 5 rounds = 2 readings, not 14", ap.READS["n"] == 2, ap.READS)

print("\npictures: formats and models that cannot see")
def img(media, data=PNG, **kw):
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}, **kw}
pics = [{"role": "user", "content": [
    img("image/svg+xml", _b64(b"<svg><text>Chart title</text></svg>"), title="c.svg"),
    img("image/bmp", title="scan.bmp", _kept=True),
    img("image/heic", title="phone.heic"),
    img("image/jpeg", title="ok.jpg"),
    {"type": "image_url", "image_url": {"url": "data:image/tiff;base64,AAAA"}},
]}]
for wire in ("openai", "anthropic"):
    got = asyncio.run(ap.for_wire(pics, wire))[0]["content"]
    check(f"{wire}: an SVG goes as its text", got[0]["type"] == "text" and "Chart title" in got[0]["text"], got[0])
    check(f"{wire}: BMP / HEIC / TIFF become notes, never a picture the provider refuses",
          all(g["type"] == "text" and "not a picture format" in g["text"] or "does not accept" in g.get("text", "")
              for g in (got[1], got[2], got[4])), [got[1], got[2], got[4]])
    check(f"{wire}: a JPEG still travels as a picture",
          got[3]["type"] == ("image_url" if wire == "openai" else "image"), got[3])
check("a note says when the picture is being kept", "being kept on this Host" in
      asyncio.run(ap.for_wire(pics, "openai"))[0]["content"][1]["text"])
blind = asyncio.run(ap.for_wire(pics, "openai", images_as_notes=True))[0]["content"]
check("a model that reads no pictures gets a note saying so, naming the picture, for every picture",
      all(p["type"] == "text" for p in blind[1:])
      and ap.NO_VISION_WHY in blind[3]["text"] and "ok.jpg" in blind[3]["text"], blind)
check("no picture note sends the Host to a second model (understand is gone, 2026-10-12)",
      not any("understand" in p.get("text", "") for p in blind), blind)

print("\npictures with a name: the Messages API takes none on an image")
# THE CAUSE, measured on one Host 2026-10-11 (44 rows in its
# inference_cost_log): session.py put the file name on the image block as
# `title`, and Anthropic refused every call with
#   400 messages.4.content.6.image.title: Extra inputs are not permitted
# so every pasted picture became a note. The pre-fix fixtures above carry
# names the way this one does; this asserts what reaches each wire.
named = [{"role": "user", "content": [
    img("image/png", _title="image.png", _kept=True),   # how session.py builds it now
    img("image/png", title="older.png"),                 # a block built before the fix
    {"type": "text", "text": "what is this?"}]}]
anth = asyncio.run(ap.for_wire(named, "anthropic"))[0]["content"]
check("anthropic: a named picture is type + source and nothing else",
      [sorted(p) for p in anth[:2]] == [["source", "type"], ["source", "type"]], anth[:2])
oai = asyncio.run(ap.for_wire(named, "openai"))[0]["content"]
check("openai: a named picture is an image_url part", [p["type"] for p in oai[:2]]
      == ["image_url", "image_url"], oai[:2])
noted = asyncio.run(ap.for_wire(named, "anthropic", images_as_notes=True))[0]["content"]
check("the private name still names the picture in a note", "image.png" in noted[0]["text"]
      and "older.png" in noted[1]["text"], noted[:2])
src_sess = turn_source(ROOT)
check("session.py builds a pasted picture with its name private (_title), never `title`",
      '_img["_title"] = _iname' in src_sess and '_img["title"]' not in src_sess)
real = {"error": ('anthropic 400: {"type":"error","error":{"type":"invalid_request_error",'
                  '"message":"messages.4.content.6.image.title: Extra inputs are not permitted"},'
                  '"request_id":"req_011CfwDpkzTA3sy3jRGVGcMa"}')}
why = ap.refusal_why(real)
check("a refusal note quotes the provider's words, not a guessed cause",
      "messages.4.content.6.image.title: Extra inputs are not permitted" in why
      and "anthropic 400" in why and "diagnose area='failures'" in why
      and "does not read pictures" not in why, why)
check("a refusal note is a note about the picture, with that reason",
      "Extra inputs are not permitted" in asyncio.run(ap.for_wire(
          named, "anthropic", images_as_notes=True, notes_why=why))[0]["content"][0]["text"])

import server.routes.inference as inf  # noqa: E402
inf._DIRECT_MODELS_CACHE["kimi"] = {"data": [
    {"id": "k3", "input_modalities": ["text"]},
    {"id": "k3-vision", "input_modalities": ["text", "image"]},
    {"id": "k-unknown"}]}
check("capability comes from the catalog row: text-only, vision, unknown",
      (ap.model_reads_images("kimi", "k3"), ap.model_reads_images("kimi", "k3-vision"),
       ap.model_reads_images("kimi", "k-unknown"), ap.model_reads_images("kimi", "nope"))
      == (False, True, None, None))

print("\nthe dial never loses a turn to a picture")
from mastercode import provider_gateway as pg2  # noqa: E402
calls = []

_sent_texts = []


async def fake_remote(**kw):
    kinds = [p.get("type") for p in kw["messages"][-1]["content"]]
    calls.append((kw["model"], kinds))
    _sent_texts.append(" ".join(p.get("text", "") for p in kw["messages"][-1]["content"]))
    if "image_url" in kinds:
        return {"error": "kimi 400: image input is not supported", "content": ""}
    return {"content": "answered", "tokens_used": 1}

real_remote = pg2._call_remote
pg2._call_remote = fake_remote
_failure_log = []
_real_fail = pg2._log_call_failure


async def _capture_failure(result, **kw):
    _failure_log.append((dict(result), kw))
pg2._log_call_failure = _capture_failure
turn = [{"role": "user", "content": [img("image/png", title="p.png"), {"type": "text", "text": "hi"}]}]
try:
    calls.clear()
    r1 = asyncio.run(pg2.call_model(messages=turn, provider="kimi", provider_model="k3"))
    known_blind = list(calls)
    calls.clear()
    r2 = asyncio.run(pg2.call_model(messages=turn, provider="kimi", provider_model="k-unknown"))
    unknown = list(calls)
finally:
    pg2._call_remote = real_remote
    pg2._log_call_failure = _real_fail
check("a known text-only model is sent a note, one call", r1.get("content") == "answered"
      and len(known_blind) == 1 and "image_url" not in known_blind[0][1], (r1, known_blind))
check("an unknown model is sent the picture; a refusal re-dials once with a note",
      r2.get("content") == "answered"
      and len(unknown) == 2 and "image_url" in unknown[0][1] and "image_url" not in unknown[1][1],
      (r2, unknown))
check("the re-dialled note quotes the refusal (kimi 400: image input is not supported)",
      "kimi 400: image input is not supported" in _sent_texts[-1], _sent_texts[-1:])

print("\nthe dial converts per wire")
src = open(os.path.join(ROOT, "brain/mastercode/provider_gateway.py")).read()
dial = src[src.index("    async def _dial("):src.index("    result = await _dial(")]
check("_dial converts from the caller's messages for the wire it dials",
      'wire = "anthropic" if p == "anthropic" else "openai"' in dial
      and "_ap.for_wire(messages, wire" in dial
      and dial.count("messages=wire_messages") == 3, dial[:200])

print("\nthe Circles route keeps attachments on the Host")
sys.modules.setdefault("server.main", types.ModuleType("server.main"))
try:
    from server.routes import circles as cr
except Exception as e:  # noqa: BLE001
    cr = None
    check("server.routes.circles imports", False, repr(e))
if cr is not None:
    stored = []

    async def fake_ingest(pool, eid, raw, name, ftype, tags, batch, supersede=True):
        if name == "boom.pdf":
            raise RuntimeError("store failed")
        stored.append((eid, raw, name, ftype, tags, supersede))
        return {"id": 1}

    import server.routes.context as ctxmod
    real_ingest, real_pool = ctxmod._ingest_binary_file, cr.brain_service.get_pool
    ctxmod._ingest_binary_file = fake_ingest
    cr.brain_service.get_pool = lambda: object()
    atts = [
        {"name": "quiz.pdf", "content_block": {"type": "document", "source": {
            "type": "base64", "media_type": "application/pdf", "data": _b64(b"%PDF-1.4 x")}}},
        {"content_block": {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": PNG}}},
        {"name": "boom.pdf", "content_block": {"type": "document", "source": {
            "type": "base64", "media_type": "application/pdf", "data": _b64(b"x")}}},
        {"name": "image.png", "content_block": {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": PNG}}},
        {"name": "image.png", "content_block": {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": PNG}}},
        {"name": "huge.pdf", "content_block": {"type": "document", "source": {
            "type": "base64", "media_type": "application/pdf",
            "data": "A" * ((ctxmod.MAX_FILE_UPLOAD_BYTES + 1024) * 4 // 3)}}},
    ]

    async def drive():
        cr._keep_attachments_on_host(3, atts)
        for t in list(cr._KEEP_TASKS):
            await t
    try:
        asyncio.run(drive())
        err = None
    except Exception as e:  # noqa: BLE001
        err = e
    finally:
        ctxmod._ingest_binary_file, cr.brain_service.get_pool = real_ingest, real_pool
    check("a failed store never reaches the chat", err is None, repr(err))
    check("each attachment is stored through the upload lane with its bytes and type",
          [(s[2], s[3]) for s in stored] == [("quiz.pdf", "application/pdf"),
                                              ("attachment-2.png", "image/png"),
                                              ("image.png", "image/png"),
                                              ("image (2).png", "image/png")]
          and stored[0][1] == b"%PDF-1.4 x", [s[2] for s in stored])
    check("a chat store never supersedes (not another upload, not a sibling)",
          stored and all(s[5] is False for s in stored), [s[5] for s in stored])
    check("same-named files in one message get distinct stored names, and the frame gets those",
          [a["name"] for a in atts[3:5]] == ["image.png", "image (2).png"], [a["name"] for a in atts])
    check("a file over the upload cap is not kept, and says why",
          atts[5].get("kept_on_host") is False and "MB" in (atts[5].get("keep_note") or "")
          and "huge.pdf" not in [s[2] for s in stored], atts[5].get("keep_note"))
    check("and the rest are marked kept", all(a.get("kept_on_host") for a in atts[:5]))

    print("\nsupersede=False on the upload lane itself")
    sup_calls = []

    async def fake_sup(*a, **k):
        sup_calls.append(a)
        return 1

    class _P:
        async def fetchval(self, *a, **k):
            return 77

        async def execute(self, *a, **k):
            return "UPDATE 0"

        async def fetch(self, *a, **k):
            return []
    real_sup = ctxmod._supersede_prior_copies
    ctxmod._supersede_prior_copies = fake_sup
    try:
        r_chat = asyncio.run(ctxmod._ingest_binary_file(_P(), 3, b"xx", "image.png", "application/x-thing",
                                                        [], "b1", supersede=False))
        n_chat = len(sup_calls)
        r_up = asyncio.run(ctxmod._ingest_binary_file(_P(), 3, b"xx", "image.png", "application/x-thing",
                                                      [], "b2"))
    finally:
        ctxmod._supersede_prior_copies = real_sup
    check("a chat store runs no supersede; an upload still does",
          n_chat == 0 and len(sup_calls) == 1 and r_chat.get("superseded") == 0
          and r_up.get("superseded") == 1, (n_chat, len(sup_calls)))

print("\nthe frame names what was kept")
fsrc = open(os.path.join(ROOT, "brain/mastercode/runtime/frame.py")).read()
check("the frame says 'being stored', never claims a background store is done",
      "Being stored on this Host" in fsrc and "Kept on this Host" not in fsrc
      and '"attachments_not_kept": fr.get("attachments_not_kept")' in fsrc
      and "Not kept on this Host: " in fsrc)

# The re-send fires ONLY on an HTTP refusal, anchored to the status the error
# paths put first. A catch-all failure (possibly after text streamed) never
# re-sends, or the person would see partial text twice and pay twice.
from mastercode import attachment_parts as _apx  # noqa: E402
for err, want in [("kimi 400: image input not supported", True),
                  ("openrouter 404: No endpoints found that support image input", True),
                  ("anthropic 400: invalid image", True),
                  ("Local model error: 400", True),
                  ("kimi 429: rate limited", False),
                  ("openrouter 503: upstream", False),
                  ("openrouter inference failure: Expecting value: line 1 column 412", False),
                  ("anthropic inference failure: Cannot connect to host api.anthropic.com:443", False)]:
    check(f"refused_input({err[:40]!r}) is {want}",
          _apx.refused_input({"error": err, "content": ""}) is want)

# A one-off reader failure is NOT cached: the next round reads again.
import asyncio as _aio  # noqa: E402
_calls = {"n": 0}
_real_rd = _apx._read_document


async def _flaky(data, media_type):
    _calls["n"] += 1
    raise RuntimeError("transient")
_apx._read_document = _flaky
try:
    blob = b"flaky-bytes-%d" % id(_calls)
    _aio.run(_apx._reading(blob, "application/pdf"))
    _aio.run(_apx._reading(blob, "application/pdf"))
finally:
    _apx._read_document = _real_rd
check("an unexpected read failure is retried next time, never cached", _calls["n"] == 2, _calls)

# The refusal is a FAILED call on the ledger: the ordinary failure purpose
# (every reader counts 'inference_error' as failed), the kind in metadata.
check("the picture refusal is logged once, as an ordinary failure, marked in its metadata",
      len(_failure_log) == 1 and "purpose" not in _failure_log[0][1]
      and _failure_log[0][0].get("picture_refused") is True, _failure_log)

print()
if failures:
    print(f"FAIL: {len(failures)} of {checks} check(s)")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print(f"PASS: {checks} checks — a dropped file reaches every provider in its own shape, and stays on the Host")
