#!/usr/bin/env python3
"""Gate: a Host reply as the parts a messaging connector sends
(server/connectors/reply_parts.py).

  T. text keeps its order; the fences taken out split it where they were; a
     plain ```html block stays text, fence lines and all;
  V. a ```visual the renderer draws becomes a PNG part in place; one it cannot
     draw becomes a line and ONE link to the thread, however many fail;
  A. an artifact (an ```artifact fence, or a tool call that made or changed
     one, live record or persisted chip) becomes ONE link to it on the Host's
     own origin, with its picture under the link only when the renderer draws
     its page; a failed call, a read-only op and an artifact that is not this
     Host's give nothing;
  I. a generated image goes as the file it already is, in place when the
     reply names it in markdown; one stored only at a provider's link, or
     missing, is a line and the thread link, never the provider's URL;
  L. every link is on the Host's origin, carries only an app id and an id,
     and with no origin there is no link at all;
  R. a renderer that raises costs the picture, never the reply;
  N. nothing is written anywhere, and nothing here can reach the browser.

Pure: the Host's reads and the renderer are fakes, so this runs from the tree.
"""
import asyncio
import base64
import os
import pathlib
import re
import sys
import types

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

from server.connectors import reply_parts as rp  # noqa: E402

FAILED = 0


def check(label, cond, detail=""):
    global FAILED
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILED += 1


ORIGIN = "https://alex.host.example.test"
THREAD = "6f1c2a8e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"
PNG = b"\x89PNG\r\n\x1a\nfake-picture"
GIMG = "gimg_" + "A" * 22
GIMG2 = "gimg_" + "B" * 22


class Reads(rp.HostReads):
    """The Host's rows, faked: artifacts 41 and 42, image GIMG."""

    def __init__(self):
        self.calls = []

    async def artifact_name(self, artifact_id):
        self.calls.append(("name", artifact_id))
        return {41: "Q3 plan", 42: "Orbit"}.get(artifact_id)

    async def artifact_page(self, artifact_id):
        self.calls.append(("page", artifact_id))
        return {41: "<h1>Q3 plan</h1><div>drawable</div>",
                42: "<svg><rect/></svg><script>x()</script>"}.get(artifact_id)

    async def image(self, image_id):
        self.calls.append(("image", image_id))
        return ("image/jpeg", b"jpeg-bytes") if image_id == GIMG else None


def drawer(seen):
    """The renderer, faked: draws a page that says 'drawable', refuses the rest."""
    async def draw(html):
        seen.append(html)
        ok = "drawable" in html
        return types.SimpleNamespace(png=PNG if ok else None, reason="" if ok else "svg")
    return draw


def build(text, calls=(), *, origin=ORIGIN, kind="thread", ref=THREAD, draw=None, reads=None):
    seen = []
    parts = asyncio.run(rp.build_reply_parts(
        text, calls, scope_kind=kind, scope_ref=ref, host_origin=origin,
        draw=draw or drawer(seen), reads=reads or Reads()))
    return parts, seen


def kinds(parts):
    return [p["type"] for p in parts]


def seqs_ok(parts):
    return [p["seq"] for p in parts] == list(range(len(parts)))


# ── T: text and code ─────────────────────────────────────────────────────────
parts, seen = build("Hello there.\n\nSecond paragraph.")
check("T plain text is one text part, seq 0",
      kinds(parts) == ["text"] and parts[0]["text"] == "Hello there.\n\nSecond paragraph." and seqs_ok(parts), parts)
check("T nothing was drawn for plain text", seen == [], seen)

html_block = "Here is the source:\n```html\n<div>drawable</div>\n```\nThat's it."
parts, seen = build(html_block)
check("T a ```html block stays text, fence lines included",
      kinds(parts) == ["text"] and "```html\n<div>drawable</div>\n```" in parts[0]["text"]
      and parts[0]["text"].startswith("Here is the source:") and parts[0]["text"].endswith("That's it."), parts)
check("T ... and is never drawn", seen == [], seen)

parts, _ = build("~~~~python\nprint('```visual')\n~~~~\nafter")
check("T a ``` inside a longer ~~~~ fence does not open a visual",
      kinds(parts) == ["text"] and "print('```visual')" in parts[0]["text"], parts)

# ── F: the fences the chat draws, and no others ──────────────────────────────
# Each verdict below is what micromark, the parser the chat renders with
# (circles-web node_modules/mdast-util-from-markdown), makes of the same text,
# and streamdown draws a fence only when its language is exactly "visual".
def drawn(parts):
    return [p for p in parts if p["type"] == "image"]

def text_of(parts):
    return "\n".join(p["text"] for p in parts if p["type"] == "text")

parts, seen = build("```Visual\n<div>drawable</div>\n```")
check("F ```Visual is a code listing in the chat, so it stays text here",
      seen == [] and kinds(parts) == ["text"] and "```Visual" in parts[0]["text"], parts)

parts, seen = build("```visual x\n<div>drawable</div>\n```")
check("F an info word joined by a no-break space is not `visual`", seen == [] and kinds(parts) == ["text"], parts)

parts, seen = build("Look: ```visual\n<div>drawable</div>\n```")
check("F U+2028 is not a line ending: no fence opens after it", seen == [] and kinds(parts) == ["text"], parts)

parts, seen = build("~~~visual `x`\n<div>drawable</div>\n~~~\ntail")
check("F a tilde fence's info string may hold a backtick",
      kinds(parts) == ["image", "text"] and parts[1]["text"] == "tail", parts)

listed = ("- ```visual\n  <div>drawable A</div>\n  ```\n\nSome text.\n\n"
          "```visual\n<div>drawable B</div>\n```")
parts, seen = build(listed)
check("F a fence on a list item's marker line is drawn, and its closing line opens nothing",
      kinds(parts) == ["image", "text", "image"] and parts[1]["text"] == "Some text."
      and [s.strip() for s in seen] == ["<div>drawable A</div>", "<div>drawable B</div>"], (parts, seen))

nested = "1. ````markdown\n   ```visual\n   <div>drawable</div>\n   ```\n   ````\n\nend"
parts, seen = build(nested)
check("F a ```visual line inside a list item's code block is source: never drawn",
      seen == [] and kinds(parts) == ["text"] and "```visual" in parts[0]["text"]
      and parts[0]["text"].endswith("end"), (parts, seen))

unclosed = "- item\n  ```visual\n  <div>drawable</div>\nNext paragraph that must stay text."
parts, seen = build(unclosed)
check("F an unclosed fence ends with its list item: the paragraph after it stays text",
      "Next paragraph that must stay text." in text_of(parts)
      and [s.strip() for s in seen] == ["<div>drawable</div>"], (parts, seen))

parts, seen = build("> ```visual\n> <div>drawable</div>\n> ```\n\ntail")
check("F a fence in a block quote is drawn, the quote markers off its body",
      kinds(parts) == ["image", "text"] and seen == ["<div>drawable</div>\n"], (parts, seen))

parts, seen = build("1. Step\n   ```visual\n   <div>drawable</div>\n   ```\n2. Next")
check("F a fence under a list item is drawn between the items",
      kinds(parts) == ["text", "image", "text"] and parts[2]["text"] == "2. Next", parts)

parts, seen = build("<div>\n```visual\n<p>x</p>\n```\n</div>\n\n```visual\n<div>drawable</div>\n```")
check("F a fence line inside a raw HTML block is the block's text; the fence after it is drawn",
      seen == ["<div>drawable</div>\n"] and kinds(parts) == ["text", "image"]
      and "```visual\n<p>x</p>" in parts[0]["text"], (parts, seen))
parts, seen = build("<!-- note\n```visual\n<p>x</p>\n```\n-->\nafter")
check("F ... and inside an HTML comment", seen == [] and kinds(parts) == ["text"], (parts, seen))

import time  # noqa: E402
deep = "- " * 20000 + "x\n" + "\n" * 20000 + "```visual\n<div>drawable</div>\n```\n"
t0 = time.monotonic()
segs = rp.split_fences(deep)
took = time.monotonic() - t0
check("F a reply nesting thousands of list items deep is scanned in linear time",
      took < 1.0 and [k for k, _ in segs][-1] == "visual", f"{took:.2f}s")

# ── V: visuals ───────────────────────────────────────────────────────────────
reply = "Before.\n```visual\n<h2>Weekly spend</h2><div>drawable</div>\n```\nAfter."
parts, seen = build(reply)
check("V a drawable visual is a PNG part between the text around it",
      kinds(parts) == ["text", "image", "text"] and parts[0]["text"] == "Before."
      and parts[2]["text"] == "After." and seqs_ok(parts), parts)
check("V the picture is the renderer's PNG, named from its heading",
      parts[1]["mime"] == "image/png" and base64.b64decode(parts[1]["data_b64"]) == PNG
      and parts[1]["name"] == "weekly-spend.png", parts[1])
check("V the renderer saw the fence body only", seen == ["<h2>Weekly spend</h2><div>drawable</div>\n"], seen)

two_bad = "A\n```visual\n<svg/>\n```\nB\n```visual\n<canvas></canvas>\n```\nC"
parts, _ = build(two_bad)
links = [p for p in parts if p["type"] == "link"]
check("V an undrawable visual is the line, then ONE thread link for the whole reply",
      [p["text"] for p in parts if p["type"] == "text"].count(rp.UNDRAWN_PICTURE) == 0  # merged into neighbours
      and sum(p["text"].count(rp.UNDRAWN_PICTURE) for p in parts if p["type"] == "text") == 2
      and len(links) == 1 and links[0]["label"] == rp.THREAD_LINK_LABEL, parts)
check("V the thread link opens that Circles thread on the Host",
      links and links[0]["url"] == f"{ORIGIN}/?open=circles&view=thread:{THREAD}", links)

parts, _ = build("```visual\n<div>drawable</div>")
check("V an unclosed visual fence runs to the end and is still drawn", kinds(parts) == ["image"], parts)

# ── A: artifacts ─────────────────────────────────────────────────────────────
parts, seen = build('Made it.\n```artifact\n{"id": 41}\n```\nDone.')
check("A an ```artifact fence is a link, then its picture, in place",
      kinds(parts) == ["text", "link", "image", "text"] and seqs_ok(parts), parts)
check("A the link opens the Artifacts window on it, on the Host's origin",
      parts[1]["url"] == f"{ORIGIN}/?open=artifacts&view=artifact:41"
      and parts[1]["label"] == "Open “Q3 plan” in your Host", parts[1])
check("A the picture under it is the artifact's page drawn", parts[2]["name"] == "q3-plan.png", parts[2])

parts, _ = build('```artifact\n{"id": 42}\n```')
check("A an artifact the renderer cannot draw is the link alone", kinds(parts) == ["link"], parts)

parts, _ = build('```artifact\n{"id": 999}\n```\n```artifact\nnot json\n```')
check("A an artifact that is not this Host's, or a broken fence, gives nothing", parts == [], parts)

try:
    parts, _ = build('Here is the plan, all of it.\n```artifact\n{"id": "²"}\n```')
    survived = parts
except Exception as e:  # noqa: BLE001
    survived = e
check("A an artifact id of non-ASCII digits costs the link, never the reply",
      isinstance(survived, list) and kinds(survived) == ["text"]
      and survived[0]["text"] == "Here is the plan, all of it.", survived)
check("A ids read as artifact-ref.ts idOf reads them: ASCII digits, whole numbers",
      (rp._positive_id("١٢"), rp._positive_id(41.0), rp._positive_id("41"), rp._positive_id(True),
       rp._id_from_preview_url("/api/artifacts/١٢/render")) == (None, 41, 41, None, None))

live_write = {"tool": "operate", "result_status": "ok",
              "args": {"tool": "artifact", "intent": "tweak", "args": {"op": "write", "artifact_id": 42}}}
chip_create = {"tool": "artifact", "op": "create", "status": "ok",
               "digest": {"preview_url": "/api/artifacts/41/render"}}
live_create_env = {"tool": "artifact", "result_status": "ok", "args": {"op": "create", "name": "Orbit"},
                   "result": {"status": "ok", "result": {"artifact_id": 42}}}
failed = {"tool": "artifact", "result_status": "error", "args": {"op": "write", "artifact_id": 41}}
readonly = {"tool": "artifact", "result_status": "ok", "args": {"op": "render", "artifact_id": 41}}
check("A artifact_id_of reads a live operate write, a persisted chip and a result envelope",
      (rp.artifact_id_of(live_write), rp.artifact_id_of(chip_create), rp.artifact_id_of(live_create_env))
      == (42, 41, 42))
check("A a failed call and a read-only op leave nothing",
      rp.artifact_id_of(failed) is None and rp.artifact_id_of(readonly) is None)
check("A artifacts_of_turn: each once, in last-touched order",
      rp.artifacts_of_turn([live_create_env, chip_create, live_write]) == [41, 42])

parts, _ = build('Done.\n```artifact\n{"id": 41}\n```', [chip_create, live_write, failed])
link_urls = [p["url"] for p in parts if p["type"] == "link"]
check("A a tool-call artifact already named by a fence is linked once, others after the text",
      link_urls == [f"{ORIGIN}/?open=artifacts&view=artifact:41", f"{ORIGIN}/?open=artifacts&view=artifact:42"],
      link_urls)

# ── I: generated images ─────────────────────────────────────────────────────
gen = {"tool": "image_generate", "result_status": "ok", "args": {"action": "generate", "prompt": "a cat"},
       "result": {"status": "ok", "result": {"images": [{"id": GIMG, "url": f"/api/images/{GIMG}"}]}}}
parts, _ = build("Here you go.", [gen])
check("I a generated image goes as the file it is, after the text",
      kinds(parts) == ["text", "image"] and parts[1]["mime"] == "image/jpeg"
      and base64.b64decode(parts[1]["data_b64"]) == b"jpeg-bytes" and parts[1]["name"] == f"{GIMG}.jpg", parts)

parts, _ = build(f"Look:\n![a cat](/api/images/{GIMG})\nNice?", [gen])
check("I a generated image the reply names in markdown goes in place, and once",
      kinds(parts) == ["text", "image", "text"] and parts[0]["text"] == "Look:" and parts[2]["text"] == "Nice?", parts)

elsewhere = {"tool": "image_generate", "result_status": "ok", "args": {"action": "generate"},
             "result": {"status": "ok", "result": {"images": [{"id": None, "url": "https://provider.example/x.png"}]}}}
parts, _ = build("Here.", [elsewhere])
check("I an image kept only at a provider's link is a line and the thread link, never that URL",
      "provider.example" not in str(parts) and rp.UNATTACHED_IMAGE in parts[0]["text"]
      and kinds(parts) == ["text", "link"], parts)

parts, _ = build(f"![gone](/api/images/{GIMG2})")
check("I a missing stored image is the same line and link", kinds(parts) == ["text", "link"], parts)

parts, _ = build("![x](https://elsewhere.example/a.png)")
check("I another site's image stays text", kinds(parts) == ["text"], parts)

peer = f"![theirs](https://alice.host.example.test/api/images/{GIMG})"
reads = Reads()
parts, _ = build(peer, reads=reads)
check("I another Host's /api/images link stays text, and nothing is read for it",
      kinds(parts) == ["text"] and parts[0]["text"] == peer and reads.calls == [], (parts, reads.calls))
parts, _ = build(f"![mine](https://ALEX.host.example.test/api/images/{GIMG})")
check("I this Host's own absolute /api/images link is the image", kinds(parts) == ["image"], parts)

looked = {"tool": "image_generate", "result_status": "ok", "args": {"action": "look"},
          "result": {"status": "ok", "result": {"images": [{"id": GIMG}]}}}
check("I only a generate call yields images", rp.generated_images_of_turn([looked]) == ([], 0))

# ── L: links ─────────────────────────────────────────────────────────────────
check("L a project thread opens the Workbench on it",
      rp.thread_link_path("project", "12") == "/?open=workbench&view=project:12")
check("L the original conversation opens Circles on it",
      rp.thread_link_path("app", "circles") == "/?open=circles&view=thread:original")
check("L anything else has no thread link",
      rp.thread_link_path("thread", "not-a-uuid") is None and rp.thread_link_path("app", "vscode") is None
      and rp.thread_link_path("project", "0") is None)

everything = ('A\n```visual\n<svg/>\n```\n```artifact\n{"id": 41}\n```')
parts, _ = build(everything, [gen, live_write])
urls = [p["url"] for p in parts if p["type"] == "link"]
check("L every link is on the Host's own origin and is only open + view",
      urls and all(re.fullmatch(re.escape(ORIGIN) + r"/\?open=[a-z-]+&view=[a-z]+:[0-9a-f-]+", u) for u in urls), urls)

parts, _ = build(everything, [gen, live_write], origin="")
check("L with no origin there is no link at all, and the artifact is named in text",
      "link" not in kinds(parts) and any("“Q3 plan” is in your Host's Artifacts window." in p.get("text", "")
                                         for p in parts), parts)

# ── R: a failing renderer ────────────────────────────────────────────────────
async def boom(html):
    raise RuntimeError("renderer crashed")
parts, _ = build("Hi\n```visual\n<div>drawable</div>\n```", draw=boom)
check("R a renderer that raises costs the picture, never the reply",
      kinds(parts) == ["text", "link"] and rp.UNDRAWN_PICTURE in parts[0]["text"], parts)

# ── N: nothing written, no browser ───────────────────────────────────────────
class Pool:
    def __init__(self):
        self.sql = []
    async def fetchval(self, sql, *a):
        self.sql.append(sql)
        return "Q3 plan"
    async def fetchrow(self, sql, *a):
        self.sql.append(sql)
        return {"content_type": "image/png", "bytes": b"png"}
    async def execute(self, *a):
        raise AssertionError("a write")
    async def fetch(self, sql, *a):
        self.sql.append(sql)
        return []

pool = Pool()
fake_bs = types.ModuleType("server.brain_service")
fake_bs.get_pool = lambda: pool
fake_bs._entity_id = 7
real_bs = sys.modules.get("server.brain_service")
sys.modules["server.brain_service"] = fake_bs
import server  # noqa: E402
real_attr = getattr(server, "brain_service", None)
server.brain_service = fake_bs
try:
    reads = rp.HostReads()
    name = asyncio.run(reads.artifact_name(41))
    img = asyncio.run(reads.image(GIMG))
    bad = asyncio.run(reads.image("../../etc/passwd"))
    bad_nl = asyncio.run(reads.image(GIMG + "\n"))
    huge = asyncio.run(reads.artifact_name(2**31))
finally:
    if real_bs is not None:
        sys.modules["server.brain_service"] = real_bs
    else:
        sys.modules.pop("server.brain_service", None)
    if real_attr is not None:
        server.brain_service = real_attr
check("N the Host's reads are SELECTs, entity-scoped, and read what they name",
      name == "Q3 plan" and img == ("image/png", b"png") and pool.sql
      and all(s.lstrip().upper().startswith("SELECT") and "entity_id" in s for s in pool.sql), pool.sql)
check("N a malformed image id is refused before any query", bad is None and len(pool.sql) == 2, pool.sql)
check("N an image id with a trailing newline is malformed too", bad_nl is None and len(pool.sql) == 2, pool.sql)
check("N an artifact id past projects.id's SERIAL range names nothing, and is never bound",
      huge is None and len(pool.sql) == 2, pool.sql)

src = (ROOT / "server/connectors/reply_parts.py").read_text()
check("N the builder writes no row: no INSERT, UPDATE, DELETE or execute",
      not re.search(r"\b(INSERT|UPDATE|DELETE)\b|\.execute\(", src))
check("N the builder cannot reach the browser sidecar",
      not re.search(r"browser_sidecar|app_verify|/v1/verify|sidecars\.browser", src))

# ── C: an image the reply shows as CODE stays code ───────────────────────────
# The chat draws `![c](/api/images/…)` inside a code span, or in an indented
# code block, as the literal text: the Host is showing the syntax, not the
# image. The messaging app gets the same text, never the picture.
for label, text in [
    ("inside a code span", f"Embed it with `![c](/api/images/{GIMG})` in your reply."),
    ("inside a double-backtick span", f"Write ``![c](/api/images/{GIMG}) and `this` `` exactly."),
    ("in an indented code block", f"Like so:\n\n    ![c](/api/images/{GIMG})\n\nThat's it."),
]:
    parts, _ = build(text)
    check(f"C an image {label} stays text, verbatim",
          kinds(parts) == ["text"] and f"/api/images/{GIMG}" in parts[0]["text"]
          and parts[0]["text"].count("`") == text.count("`"), parts)
parts, _ = build(f"Code `x` then ![c](/api/images/{GIMG}) after.")
check("C an image after a closed code span on the same line is still the image",
      kinds(parts) == ["text", "image", "text"], parts)
parts, _ = build(f"An unclosed ` backtick, then ![c](/api/images/{GIMG}).")
check("C a lone backtick opens no span", kinds(parts) == ["text", "image", "text"], parts)
parts, _ = build(f"One `span\n\nacross a paragraph` break ![c](/api/images/{GIMG}).")
check("C a span cannot cross a blank line", "image" in kinds(parts), parts)
parts, _ = build(f"- item\n    ![c](/api/images/{GIMG})")
check("C four spaces continuing a paragraph are not a code block", "image" in kinds(parts), parts)

# ── B: an image larger than one platform upload goes as a line and a link ────
class BigReads(Reads):
    async def image(self, image_id):
        return ("image/png", b"x" * (rp.IMAGE_PART_MAX + 1))

parts, _ = build(f"Here.\n![big](/api/images/{GIMG})", reads=BigReads())
check("B an image over the upload limit is never streamed: a line and the thread link",
      "image" not in kinds(parts) and any(rp.UNATTACHED_IMAGE in p.get("text", "") for p in parts)
      and "link" in kinds(parts), [(p["type"], len(str(p))) for p in parts])
from server.network import nac_connector as _nc  # noqa: E402
check("B the limit is the platform call's own per-file limit",
      rp.IMAGE_PART_MAX == _nc.PLATFORM_FILE_MAX, (rp.IMAGE_PART_MAX, _nc.PLATFORM_FILE_MAX))

print("RESULT: ALL PASS" if FAILED == 0 else f"RESULT: {FAILED} FAILED")
sys.exit(1 if FAILED else 0)
