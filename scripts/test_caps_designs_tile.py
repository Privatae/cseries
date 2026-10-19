#!/usr/bin/env python3
"""Gate: a rich desktop design can be published, and a desktop over the Host
Manager's tile limit still gets a tile that says what it left out.

WHY THIS EXISTS. The design library refused any document over 256 KB
(designs.py MAX_DESIGN_BYTES, "that document is over 256KB") — a mirror of
the Host Manager TILE reader's limit, a thumbnail's number. A real desktop
inlines its CSS, SVG and data-URI images and passes 256 KB; that is the
incident this fixes. And the tile itself (shell_snapshot.compose_clean_shell)
gave up on anything over 256 KB with only a log line: the host got no tile
and nobody was told.

What this pins (container-free: a temp shell, stubs, no browser, no bridge):
  1. a 400 KB design gets past the size door (the next door, the floor,
     answers instead of 413); a design past the render check's 4 MB page is
     refused before any browser is woken, naming the limit;
  2. a desktop with 300 KB of script gets a full tile (the tile frame is
     sandbox="", the scripts never ran) and no note;
  3. a desktop with large inline images gets a tile under the limit that
     keeps the page and leaves out the largest images, and LAST_TILE says so
     in plain words; the set door puts that note in front of the person;
  4. a desktop that cannot fit even without its images is reported as
     "no tile", never silently.

Measured red against the tree before the fix (APP_ROOT=<pristine export>).
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from base64 import b64encode
from pathlib import Path

REPO = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO / "brain"))
sys.path.insert(0, str(REPO))

FAILS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def run(coro):
    return asyncio.run(coro)


WORK = Path(tempfile.mkdtemp(prefix="caps-g2-designs-")).resolve()
# The suite's own temp dir, removed when the interpreter leaves — pass, fail or
# raise (every manual run used to leave it behind).
import atexit as _atexit  # noqa: E402
import shutil as _shutil  # noqa: E402
_atexit.register(_shutil.rmtree, WORK, True)

# ── 1. the design library's size door ─────────────────────────────────────
print("1. a rich design reaches the floor; only the render check's limit refuses")
from fastapi import HTTPException                       # noqa: E402
from server.routes import designs as D                   # noqa: E402


async def _no_ids(_pool):
    return frozenset()

D._installed_ids = _no_ids
_floor_calls = []


def _floor(html, installed_ids=None):
    _floor_calls.append(len(html))
    return {"ok": False, "errors": ["stub floor"], "warnings": [], "tokens": {}, "mode": None}

D.check_document = _floor
D.format_report = lambda r: "stub floor refused"
doc400 = "<!doctype html><html><head></head><body>" + ("<p>x</p>" * 52_000) + "</body></html>"
try:
    run(D.publish_document(None, "Dark Cinematic", doc400))
    got = "published?"
except HTTPException as e:
    got = f"{e.status_code} {e.detail}"
check("1: a 400 KB design is not refused for its size (the floor answers)",
      got.startswith("422") and _floor_calls, got)
big = "<!doctype html><html><body>" + ("y" * (5 * 1024 * 1024)) + "</body></html>"
_floor_calls.clear()
try:
    run(D.publish_document(None, "Dark Cinematic", big))
    got = "published?"
except HTTPException as e:
    got = f"{e.status_code} {e.detail}"
check("1: a 5 MB design is refused before the floor or a browser, naming the check's limit",
      got.startswith("413") and "4 MB" in got and not _floor_calls, got)

# ── 2-4. the Host Manager tile ────────────────────────────────────────────
print("2. a desktop over the tile limit still gets a tile")
from server.applications import shell_snapshot as S      # noqa: E402

base = WORK / "dashboard-base"
(base / "css").mkdir(parents=True)
(base / "css" / "shell.css").write_text("#taskbar{position:fixed;bottom:0}")
(base / "index.html").write_text(
    '<!doctype html><html><head><link rel="stylesheet" href="/css/shell.css"></head>'
    '<body><div id="desktop"><div id="desktop-icons"></div></div>'
    '<div id="taskbar"><span id="entity-name">Starting up...</span></div>'
    '<script>boot()</script></body></html>')
S.DASHBOARD_BASE = base


def compose(wallpaper):
    return run(S.compose_clean_shell(None, 3, host_name="Nova", wallpaper_html=wallpaper))


scripty = ('<html><head><meta name="cseries-desktop" content="wallpaper"></head><body>'
           '<h1>Clock</h1><script>' + ("var a=1;" * 40_000) + '</script></body></html>')
d2 = compose(scripty)
tile = getattr(S, "LAST_TILE", {})
check("2: a desktop with 300 KB of script gets a tile", d2 is not None)
check("2: under the tile limit", d2 is not None and len(d2.encode()) <= S._MAX_BYTES)
check("2: the picture kept its page", d2 is not None and "Clock" in d2)
check("2: nothing visible was left out, so nothing is said", not tile.get("note"), str(tile))

print("3. large inline images go first, largest first, and the tile says so")
img_big = "data:image/png;base64," + b64encode(os.urandom(200_000)).decode()
img_mid = "data:image/png;base64," + b64encode(os.urandom(60_000)).decode()
img_small = "data:image/png;base64," + b64encode(b"tiny-icon").decode()
pics = ('<html><head><meta name="cseries-desktop" content="wallpaper"></head><body>'
        f'<h1>Gallery</h1><img src="{img_big}"><div style="background:url({img_mid})"></div>'
        f'<img src="{img_small}"></body></html>')
d3 = compose(pics)
tile = getattr(S, "LAST_TILE", {})
check("3: a desktop with 340 KB of images gets a tile", d3 is not None)
check("3: under the tile limit", d3 is not None and len(d3.encode()) <= S._MAX_BYTES)
check("3: the page and the small image stay", d3 is not None and "Gallery" in d3
      and b64encode(b"tiny-icon").decode() in d3)
check("3: the largest image is the one left out", d3 is not None and img_big[40:80] not in d3)
check("3: the tile says what it left out, in plain words",
      bool(tile.get("note")) and "left out" in tile["note"] and "image" in tile["note"], str(tile))

print("4. a desktop that cannot fit even without images: no tile, said plainly")
words = ('<html><head><meta name="cseries-desktop" content="wallpaper"></head><body>'
         + ("<p>" + "lorem ipsum dolor " * 20 + "</p>") * 900 + '</body></html>')
d4 = compose(words)
tile = getattr(S, "LAST_TILE", {})
check("4: the tile falls back to the shell alone, under the limit",
      d4 is not None and len(d4.encode()) <= S._MAX_BYTES and "Nova" in d4)
check("4: and says the desktop itself was left out",
      bool(tile.get("note")) and "desktop document" in tile["note"], str(tile))

print("6. the reduction is bounded and runs off the event loop")
import asyncio as _aio  # noqa: E402
_calls = {"n": 0, "on_loop": 0}
_real_assemble = S._assemble


def _counting_assemble(*a, **kw):
    _calls["n"] += 1
    if _aio._get_running_loop() is not None:
        _calls["on_loop"] += 1
    return _real_assemble(*a, **kw)


many = "".join(f'<img src="data:image/png;base64,{b64encode(os.urandom(6_000)).decode()}">'
               for _ in range(60))
pics60 = ('<html><head><meta name="cseries-desktop" content="wallpaper"></head><body>'
          f'<h1>Sixty</h1>{many}</body></html>')
S._assemble = _counting_assemble
try:
    d6 = compose(pics60)
finally:
    S._assemble = _real_assemble
check("6: a desktop with 60 inline images (480 KB) gets a tile under the limit",
      d6 is not None and len(d6.encode()) <= S._MAX_BYTES and "Sixty" in d6)
check("6: it is assembled a bounded number of times, not once per image",
      _calls["n"] <= 6, str(_calls))
check("6: no assembly runs on the event loop", _calls["on_loop"] == 0, str(_calls))

print("5. the set door shows the tile's note to the person")
src = (REPO / "server" / "routes" / "appearance.py").read_text()
seg = src.split("async def set_desktop_artifact", 1)[-1].split("\nasync def ", 1)[0]
check("5: set_desktop_artifact adds LAST_TILE's note to its warnings",
      "LAST_TILE" in seg and "warnings" in seg.split("LAST_TILE", 1)[-1][:200])

print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s): {FAILS}")
    sys.exit(1)
print("PASS: rich designs publish; an over-limit desktop gets a reduced, labelled tile")
