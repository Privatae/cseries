#!/usr/bin/env python3
"""test_image_inline_b64.py — an image the provider returns INLINE is kept, and
served back only as a raster image.

THE BUG (measured). With the image role routed to OpenRouter
(the Keys-provider lane), one real call to google/gemini-2.5-flash-image returned
200 with data[0] = {b64_json: <1.3 MB>, media_type: "image/png"} and no `url`,
at a cost of $0.0387. image_engine._generate kept only entries with a url, so it
returned {"status": "ok", "count": 0}: the user paid and got nothing, and the
host was told it had succeeded.

What this pins, by driving the real engine against a stand-in provider:
  1. the OpenRouter shape is stored and served from /api/images/<id>;
  2. the url shape is untouched;
  3. what gets stored is decided by the BYTES — HTML, SVG, bad base64 and an
     oversized payload are refused however the provider labels them;
  4. a 200 that yields no image is reported as an error, never "ok, count 0";
  5. the route serves only well-formed ids, only raster types, with a CSP.

Container-free: APP_ROOT=<repo> python3 scripts/<this>.py. No pod, no DB,
no network.
"""
from __future__ import annotations

import asyncio
import json
import base64
import os
import re
import sys
import types
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


from mastercode import image_engine  # noqa: E402

# A real 1x1 PNG (the one the end-to-end test's vision row sends).
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PNG_B64 = base64.b64encode(PNG).decode()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16
HTML_B64 = base64.b64encode(b"<html><script>alert(1)</script></html>").decode()
SVG_B64 = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>').decode()


class _Resp:
    def __init__(self, payload):
        self.status = 200
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return ""


_NEXT: list[dict] = []
_POSTED: list = []


class _Session:
    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, **kw):
        _POSTED.append((url, kw.get("json")))
        return _Resp(_NEXT.pop(0))


class _Pool:
    def __init__(self):
        self.images: list[tuple] = []      # INSERT INTO generated_images args
        self.context: list[tuple] = []     # INSERT INTO entity_context args

    async def execute(self, sql, *args):
        if "INSERT INTO generated_images" in sql:
            self.images.append(args)
        elif "INSERT INTO entity_context" in sql:
            self.context.append(args)


async def _route(context, role):
    return {"base": "https://openrouter.ai/api/v1",
            "model": "google/gemini-2.5-flash-image", "key": "k"}


image_engine.aiohttp = types.SimpleNamespace(
    ClientSession=_Session, ClientTimeout=lambda **kw: None)
image_engine._resolve_image_route = _route


def generate(payload: dict, n: int = 1):
    _NEXT[:] = [payload]
    pool = _Pool()
    out = asyncio.run(image_engine.run(image_engine.EngineContext(
        9003, pool, args={"action": "generate", "prompt": "a plain red square", "n": n})))
    return out, pool


# ── 1. the OpenRouter shape is kept ──────────────────────────────────────
print("== 1. an inline (b64_json) image is kept, not dropped ==")
out, pool = generate({"data": [{"b64_json": PNG_B64, "media_type": "image/png"}]})
check("status ok with ONE image (was: ok, count 0)",
      out.get("status") == "ok" and out.get("count") == 1, str(out)[:200])
url = (out.get("images") or [{}])[0].get("url", "")
check("it is served from this Host", url.startswith("/api/images/gimg_"), url)
check("exactly one row written to generated_images", len(pool.images) == 1, str(len(pool.images)))
if pool.images:
    img_id, eid, ctype, data, prompt, model = pool.images[0]
    check("the id is the one in the url", url == f"/api/images/{img_id}", f"{url} vs {img_id}")
    check("the id carries 128 random bits (22 url-safe chars)",
          re.fullmatch(r"gimg_[A-Za-z0-9_-]{22}", img_id) is not None, img_id)
    check("the stored bytes are the decoded image, byte for byte", data == PNG)
    check("the content type is SNIFFED from the bytes", ctype == "image/png", ctype)
    check("owned by this entity", eid == 9003, str(eid))
    check("prompt and model recorded", prompt == "a plain red square"
          and model == "google/gemini-2.5-flash-image", f"{prompt!r} {model!r}")
check("the gallery record points at the Host url",
      len(pool.context) == 1 and url in pool.context[0][2], str(pool.context)[:200])

out, pool = generate({"data": [{"b64_json": PNG_B64}]})
check("no media_type at all: still kept (the bytes decide, not the label)",
      out.get("count") == 1 and pool.images and pool.images[0][2] == "image/png", str(out)[:160])

out, pool = generate({"data": [{"b64_json": "data:image/png;base64," + PNG_B64}]})
check("a data: URI is the same payload with a header",
      out.get("count") == 1 and pool.images and pool.images[0][3] == PNG, str(out)[:160])

for label, raw, want in (("JPEG", JPEG, "image/jpeg"), ("WebP", WEBP, "image/webp")):
    out, pool = generate({"data": [{"b64_json": base64.b64encode(raw).decode()}]})
    check(f"{label} is kept as {want}",
          out.get("count") == 1 and pool.images and pool.images[0][2] == want, str(out)[:160])

# ── 2. the url shape is untouched ────────────────────────────────────────
print("== 2. a provider link is kept as a link ==")
out, pool = generate({"data": [{"url": "https://cdn.example/x.png", "revised_prompt": "r"}]})
check("url responses are unchanged",
      out.get("status") == "ok" and out["images"][0]["url"] == "https://cdn.example/x.png"
      and not pool.images, str(out)[:200])

# ── 3. the bytes decide what is kept ─────────────────────────────────────
print("== 3. anything that is not a raster image is refused, however it is labelled ==")
for label, payload, reason in (
        ("HTML labelled image/png", {"b64_json": HTML_B64, "media_type": "image/png"}, "not a PNG"),
        ("SVG (a document that can carry script)", {"b64_json": SVG_B64, "media_type": "image/svg+xml"}, "not a PNG"),
        ("invalid base64", {"b64_json": "!!!not base64!!!"}, "not valid base64"),
        ("a non-string b64_json", {"b64_json": 12345}, "not a string")):
    out, pool = generate({"data": [payload]})
    check(f"{label}: nothing stored", not pool.images, str(pool.images)[:120])
    check(f"{label}: reported as an error naming why",
          out.get("status") == "error" and reason in (out.get("result") or ""), str(out)[:200])

_cap = image_engine._MAX_INLINE_IMAGE_BYTES
image_engine._MAX_INLINE_IMAGE_BYTES = 16
out, pool = generate({"data": [{"b64_json": base64.b64encode(PNG + b"\x00" * 64).decode()}]})
image_engine._MAX_INLINE_IMAGE_BYTES = _cap
check("an oversized payload is refused before it is stored",
      not pool.images and out.get("status") == "error" and "larger than" in out.get("result", ""),
      str(out)[:200])

# ── 4. a 200 with nothing kept is not a success ──────────────────────────
print("== 4. \"ok, count 0\" is never the answer ==")
out, _ = generate({"data": []})
check("an empty list is an error that says it may have charged",
      out.get("status") == "error" and "charged" in (out.get("result") or ""), str(out)[:200])
out, pool = generate({"data": [{"b64_json": PNG_B64}, {"b64_json": HTML_B64}]}, n=2)
check("one good + one bad: ok with the good one, and the bad one listed",
      out.get("status") == "ok" and out.get("count") == 1
      and len(out.get("dropped") or []) == 1, str(out)[:240])
out, _ = generate({"data": [{"revised_prompt": "neither"}]})
check("an entry with neither url nor b64_json is an error, not ok",
      out.get("status") == "error", str(out)[:200])

# ── 4b. never more than was asked for ─────────────────────────────────────
print("== 4b. at most n images are kept, whatever the provider returns ==")
out, pool = generate({"data": [{"b64_json": PNG_B64}] * 3}, n=1)
check("n=1 with three returned: exactly one stored",
      out.get("count") == 1 and len(pool.images) == 1, str(out)[:200])
check("...and the two extra are reported as dropped",
      sum(1 for d in out.get("dropped") or [] if "beyond" in d["reason"]) == 2,
      str(out.get("dropped")))

# ── 4c. the bytes decide — tightened after the review's mutants survived ──
print("== 4c. near-misses are refused: loose base64, a RIFF that is not WebP, a bare 0xFF ==")
junk = PNG_B64[:8] + "!!" + PNG_B64[8:]
for label, payload in (
        ("base64 with junk characters (validate=True must refuse it)", {"b64_json": junk}),
        ("a RIFF container that is WAVE, not WEBP",
         {"b64_json": base64.b64encode(b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 16).decode()}),
        ("a lone 0xFF that is not a JPEG SOI",
         {"b64_json": base64.b64encode(b"\xff\x00\x00" + b"\x00" * 16).decode()})):
    out, pool = generate({"data": [payload]})
    check(f"{label}: nothing stored", not pool.images and out.get("status") == "error",
          str(out)[:200])

# ── 4e. the token-less route is exempt from the app-token gate ─────────────
print("== 4e. /api/images is on the app-token allowlist (an <img> cannot send one) ==")
_mw = (ROOT / "server" / "applications" / "middleware.py").read_text()
_allow = _mw[_mw.index("_ALLOWLIST_PREFIXES = ("):]
_allow = _allow[:_allow.index("\n)\n")]
check("'/api/images/' is in _ALLOWLIST_PREFIXES (else strict mode 401s every image)",
      '"/api/images/"' in _allow, _allow[-200:])


# ── 4d. look shows this Host's own images to the Host's own model ──────────
# Image understanding (a second model on a 'vision' route) is gone:
# the Host's own model sees its pictures. look hands them to the running turn
# (runtime/turn_pictures); no provider is called for a picture of its own.
print("== 4d. look on /api/images/<id> hands the picture to the Host's turn ==")
OWN = "gimg_" + "B" * 22
from mastercode.runtime import turn_pictures  # noqa: E402


class _OwnPool(_Pool):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    async def fetchrow(self, sql, *args):
        return self.rows.get(args)


def look(refs, rows, *, in_turn=True):
    _POSTED.clear()
    tok = turn_pictures.begin_turn() if in_turn else turn_pictures.begin_agent()
    try:
        out = asyncio.run(image_engine.run(image_engine.EngineContext(
            9003, _OwnPool(rows), args={"action": "look", "images": refs})))
        pics = turn_pictures.drain()
    finally:
        turn_pictures.end(tok)
    return out, pics


ROW = {(OWN, 9003): {"content_type": "image/png", "bytes": PNG, "prompt": "blender/run_script",
                  "model": "app:blender-engine"}}
out, pics = look([f"/api/images/{OWN}"], ROW)
check("in a Host turn it answers ok and shows one picture", out.get("status") == "ok"
      and out.get("count") == 1 and len(pics) == 1, (str(out)[:200], len(pics)))
check("the picture is the stored bytes with the stored type",
      pics and pics[0]["data"] == PNG_B64 and pics[0]["media_type"] == "image/png", str(pics)[:200])
check("no provider is called for a picture of the Host's own", not _POSTED, _POSTED[:1])
check("an app's render is marked as MCP output for the turn", out.get("origins") == ["app"], out)
out, pics = look([f"/api/images/{OWN}"], ROW, in_turn=False)
check("in an agent's run it shows nothing and says only the Host sees pictures",
      out.get("status") == "error" and not pics and "only to the Host" in out.get("result", ""), out)
out, pics = look([f"/api/images/{OWN}"], {})
check("an id this Host does not hold is an error, not a picture",
      out.get("status") == "error" and not pics and not _POSTED, str(out)[:200])
out, pics = look(["/api/images/../../etc/passwd"], {})
check("a malformed own-image path is refused before any lookup",
      out.get("status") == "error" and not pics and not _POSTED, str(out)[:200])
out = asyncio.run(image_engine.run(image_engine.EngineContext(
    9003, _OwnPool({}), args={"action": "understand", "image_url": f"/api/images/{OWN}"})))
check("action='understand' says it was removed and names look", out.get("status") == "error"
      and "look" in out.get("result", "") and "removed" in out.get("result", ""), out)


# ── 5. the route ─────────────────────────────────────────────────────────
print("== 5. /api/images/{id} serves only well-formed ids and raster types ==")
try:
    from server.routes import images as images_route
    from server import brain_service
    from fastapi import HTTPException
except Exception as exc:  # pragma: no cover
    check("the route module imports", False, f"{type(exc).__name__}: {exc}")
else:
    GOOD = "gimg_" + "A" * 22
    rows: dict = {}
    queries: list = []

    class _RoutePool:
        async def fetchrow(self, sql, *args):
            queries.append((sql, args))
            return rows.get(args)

    brain_service.get_pool = lambda: _RoutePool()
    brain_service._entity_id = 9003

    def serve(i):
        try:
            return asyncio.run(images_route.serve_generated_image(i))
        except HTTPException as exc:
            return exc

    rows[(GOOD, 9003)] = {"content_type": "image/png", "bytes": PNG}
    r = serve(GOOD)
    check("a stored image is served with its bytes",
          getattr(r, "body", None) == PNG and r.media_type == "image/png", str(r)[:120])
    check("with a sandboxing CSP",
          "sandbox" in (getattr(r, "headers", {}).get("content-security-policy") or ""),
          str(getattr(r, "headers", {}))[:200])
    check("the lookup is scoped to THIS entity",
          queries and queries[-1][1] == (GOOD, 9003) and "entity_id" in queries[-1][0], str(queries[-1:]))

    queries.clear()
    for bad in ("gimg_short", "../../etc/passwd", "gimg_" + "A" * 21 + "/", "img_1_0", ""):
        r = serve(bad)
        check(f"malformed id {bad!r} is a 404 before any query",
              isinstance(r, HTTPException) and r.status_code == 404 and not queries, str(r))

    rows[(GOOD, 9003)] = {"content_type": "text/html", "bytes": b"<html></html>"}
    r = serve(GOOD)
    check("a row that is not a raster type is never served (defense in depth)",
          isinstance(r, HTTPException) and r.status_code == 404, str(r))

    rows.clear()
    r = serve(GOOD)
    check("an unknown id is a 404", isinstance(r, HTTPException) and r.status_code == 404, str(r))

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    sys.exit(1)
print("ok  inline images are kept, served, and only as raster images")
