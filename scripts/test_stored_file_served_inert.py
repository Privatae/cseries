"""A stored file is never served as a page of the Host's own origin.

Two routes hand a browser bytes from a row with the row's own type: a context
file (GET /api/brain/context/{id}/file, user_context.metadata.mime, shown
inline) and the wallpaper (GET /api/appearance/image, no token at all,
entity_appearance.image_content_type). A restored backup's rows are anyone's:
before this, a row saying text/html (or image/svg+xml) made either route serve
a page that runs as the owner on the Host's origin.

Pinned here on the real route functions with invented rows (the pool is a
stand-in; no database):

  1. A context file whose mime is a page (text/html, with or without a
     charset, image/svg+xml, application/xhtml+xml) is sent as a download of
     bytes, never inline.
  2. A picture, a PDF and plain text are still shown inline, as their type.
  3. The wallpaper is only ever an allowed image type, and sandboxed.

Run: APP_ROOT=<repo> python3 scripts/test_stored_file_served_inert.py
"""
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

from starlette.requests import Request          # noqa: E402

from server import brain_service                 # noqa: E402
from server.routes import appearance as AP       # noqa: E402
from server.routes import context as CX          # noqa: E402

FAILED: list = []


def check(label, cond, detail=""):
    print(("  ok    " if cond else "  FAIL  ") + label + (f"\n        {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(label)


PAGE = b"<!doctype html><script>fetch('/api/invented-target', {method: 'POST'})</script>"


class _Pool:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, *_a, **_k):
        return self.row

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *_e):
                return False
        return _C()


def context_file(mime, body=PAGE):
    row = {"id": 41, "title": "invented-notes", "content": base64.b64encode(body).decode(),
           "storage_backend": "pg",
           "metadata": json.dumps({"kind": "file", "mime": mime, "from_file": "invented-notes.bin"})}
    brain_service._pool = _Pool(row)
    r = asyncio.run(CX.get_context_file(41))
    return r.media_type, r.headers.get("content-disposition", "")


for mime in ("text/html", "text/html; charset=utf-8", "TEXT/HTML", "image/svg+xml",
             "application/xhtml+xml", "application/javascript", None):
    t, disp = context_file(mime)
    check(f"a context file said to be {mime!r} is sent as a download of bytes",
          t == "application/octet-stream" and disp.startswith("attachment;"), (t, disp))

for mime in ("image/png", "application/pdf", "text/plain"):
    t, disp = context_file(mime, b"invented bytes")
    check(f"a context file said to be {mime} is still shown inline as {mime}",
          t == mime and disp.startswith("inline;"), (t, disp))


def wallpaper(ctype):
    brain_service._pool = _Pool({"image_bytes": PAGE, "image_content_type": ctype})
    req = Request({"type": "http", "method": "GET", "path": "/api/appearance/image",
                   "query_string": b"", "headers": []})
    r = asyncio.run(AP.serve_custom_image(req))
    return r.media_type, r.headers.get("content-security-policy", "")


t, csp = wallpaper("text/html")
check("a wallpaper row said to be text/html is served as an image, sandboxed",
      t == "image/jpeg" and "sandbox" in csp, (t, csp))
t, csp = wallpaper("image/webp")
check("a webp wallpaper is still image/webp", t == "image/webp", t)

print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: a stored file is never a page of the Host's origin")
