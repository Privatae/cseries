#!/usr/bin/env python3
"""Every Host has the house designs built into its image, with no bridge.

designs/builtin/ carries complete desktop documents and an index naming them;
server/routes/designs.py lists them on the house shelf and serves and tries
them from disk. Pinned here:

  1. the index names First Light (41) and Garnet Silk (42), and each file is
     there;
  2. each document passes the desktop floor the shelf applies
     (desktop_contract.check_document), uses only the release's names
     (window.CSeriesDesktop, cseries-* metas, data-cseries-* attributes,
     --cseries-* tokens) and fetches nothing from outside the document;
  3. with no platform (NoPlatform) GET /api/designs lists both on the house
     shelf and reports no library error, and their documents come from disk
     without the bridge being asked;
  4. with a bridge that lists designs, the built-ins are merged in without
     repeating an id the bridge already listed;
  5. trying a design reads the same local copy (_install goes through
     _design_doc), and the image copies designs/builtin/.

Container-free: APP_ROOT=<repo> python3 scripts/test_builtin_designs.py
"""
import asyncio
import json
import os
import re
import sys
import types
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path[:0] = [str(ROOT), str(ROOT / "brain")]

FAILS: list[str] = []


def check(cond, msg, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + msg + (f"\n        {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(msg)


BUILTIN = ROOT / "designs" / "builtin"
OLD = "sk" + "ema"   # written in pieces, as scripts/test_public_source_clean.py does

# 1
index = json.loads((BUILTIN / "index.json").read_text())["designs"]
check({(d["id"], d["name"]) for d in index} == {(41, "First Light"), (42, "Garnet Silk")},
      "the index names First Light (41) and Garnet Silk (42)", str(index))
for d in index:
    check((BUILTIN / d["file"]).is_file(), f"{d['file']} is in designs/builtin/")

# 2
from server.applications.desktop_contract import _EXTERNAL_RE, check_document  # noqa: E402
from server.applications.desktop_tokens import extract_tokens  # noqa: E402

FETCHES = re.compile(r"fetch\s*\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts|"
                     r"https?://|<link\b|<img\b|\bsrc\s*=|@import|url\(\s*['\"]?(?!#)", re.I)
for d in index:
    html = (BUILTIN / d["file"]).read_text(encoding="utf-8")
    report = check_document(html, installed_ids=frozenset())
    check(report["ok"], f"{d['name']} passes the desktop floor", str(report.get("errors")))
    check(OLD not in html.lower(), f"{d['name']} carries no old name")
    check("window.CSeriesDesktop" in html and 'name="cseries-desktop"' in html,
          f"{d['name']} uses the release's desktop bridge and metas")
    check(len(extract_tokens(html) or {}) >= 13, f"{d['name']} declares its --cseries-* palette")
    hit = FETCHES.search(html) or _EXTERNAL_RE.search(html)
    check(hit is None, f"{d['name']} fetches nothing from outside the document",
          hit.group(0) if hit else "")
    check(not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", html), f"{d['name']} holds no address")


# 3, 4
class Pool:
    async def fetch(self, *a):
        return []

    async def fetchval(self, *a):
        return None

    async def fetchrow(self, *a):
        return None


async def main():
    from server.bridge.no_platform import NoPlatform
    # server.main serves the dashboard from /app and will not import outside
    # the image; the route asks it for one thing, the bridge client.
    stub = types.ModuleType("server.main")
    stub.get_bridge_client = lambda: NoPlatform()
    sys.modules["server.main"] = stub
    from server.routes import designs

    async def wearing(pool):
        return {}

    designs._pool = lambda: Pool()
    designs._wearing = wearing

    out = await designs.list_designs()
    house = {d["id"]: d for d in out["designs"] if d.get("shelf") == "house"}
    check(set(house) == {41, 42}, "standalone: both built-ins are on the house shelf", str(out["designs"]))
    check(out["library_error"] is None, "standalone: no 'shelf can't be reached' error",
          str(out["library_error"]))
    check(all(d.get("tokens") for d in house.values()), "each card carries its palette")

    asked = []

    class Bridge:
        connected = True

        async def request_full(self, op, params=None):
            asked.append(op)
            if op == "design.list":
                return {"ok": True, "data": {"designs": [
                    {"id": 41, "name": "First Light", "shelf": "house", "author_name": "a Host"},
                    {"id": 99, "name": "Elsewhere", "shelf": "house", "author_name": "a Host"}]}}
            return {"ok": False, "error": "not asked for in this gate"}

    stub.get_bridge_client = lambda: Bridge()
    out = await designs.list_designs()
    ids = [d["id"] for d in out["designs"]]
    check(sorted(ids) == [41, 42, 99], "a bridge's shelf and the built-ins merge without a repeat",
          str(ids))

    asked.clear()
    for did in (41, 42):
        doc = await designs.design_document(did)
        on_disk = (BUILTIN / next(d["file"] for d in index if d["id"] == did)).read_text(encoding="utf-8")
        check(doc["html"] == on_disk, f"design {did}'s document is the file on disk")
    check("design.get" not in asked, "previewing a built-in never asks the bridge", str(asked))

    stub.get_bridge_client = lambda: NoPlatform()
    data = await designs._design_doc(42)
    check(data.get("name") == "Garnet Silk" and data.get("html"), "trying reads the local copy")


asyncio.run(main())

# 5
import inspect  # noqa: E402
from server.routes import designs as _d  # noqa: E402
src = inspect.getsource(_d._install)
check("_design_doc(" in src and 'design.get' not in src, "_install reads the design through _design_doc")
docker = (ROOT / "Dockerfile.linux").read_text()
check(re.search(r"^COPY .*designs/builtin/ /app/designs/builtin/", docker, re.M) is not None,
      "the image copies designs/builtin/")
check(_d.BUILTIN_DIR == ROOT / "designs" / "builtin", "the route reads the folder the image copies",
      str(_d.BUILTIN_DIR))

if FAILS:
    print(f"\nFAIL: {len(FAILS)} check(s)")
    sys.exit(1)
print("\nall checks passed")
