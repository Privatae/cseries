#!/usr/bin/env python3
"""
A build records what it really did, with nothing cut silently.

The run terminal shows the build session manifest's `prompt` to the
user as "what the host sent the harness". It was stored as prompt[:8000] with
no marker, so a long composed brief read as shorter than what the harness got.
The fan-out manifest already stored the whole prompt.
  1. a build's manifest holds the whole prompt, byte for byte

_note_landed kept at most 5,000 landed paths per project and evicted
the oldest without a word. The plan-end save point commits from
phase_landed_paths, so an evicted path was left out of that commit. One entry
per project path: the project's own file count already bounds it.
  2. every phase-landed path is still listed past 5,000
  3. a path landed again is listed once (the bound that remains is per path)

Stub engine, temp dirs; no pod, no DB. Runs against the image (/app) or a
source tree (APP_ROOT=<repo>).
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[build-record] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)
    sys.path.insert(0, os.path.join(app_root, "brain"))
    os.environ["FACTORY_ENGINE"] = "stub"
    os.environ["CSERIES_SMOKE"] = "1"

    import server.routes.review as rv

    async def _conn(): return ["kimi"]

    async def _cfg(): return {"kimi": {"model": "k3"}}
    rv._installed_harnesses = _conn
    rv._coding_harness_settings = _cfg

    # 1. The whole prompt is stored.
    prompt = "build it. " + "".join(f"step {i}: keep going. " for i in range(2000))
    with tempfile.TemporaryDirectory() as td:
        rv.PROJECTS_ROOT = Path(td) / "projects"
        rv.SESSIONS_ROOT = Path(td) / "sessions"
        rv.PROJECTS_ROOT.mkdir(parents=True)
        rv.SESSIONS_ROOT.mkdir(parents=True)
        out = asyncio.run(rv.run_single_harness_build(
            project_id=42, prompt=prompt, harness="kimi",
            billing_key="factory-build-7001"))
        m = json.loads((rv.SESSIONS_ROOT / "factory-build-7001" / "manifest.json").read_text())
        stored = m.get("prompt") or ""
        check("1. the build manifest holds the whole prompt (not its first 8000 chars)",
              len(prompt) > 8000 and stored == prompt,
              f"prompt={len(prompt)} stored={len(stored)} ok={out.get('ok')}")

    # 2-3. Landed paths are never evicted.
    rv._LANDED_BY.pop(9901, None)
    paths = [f"src/f{i}.txt" for i in range(6000)]
    rv._note_landed(9901, paths, {"phase_id": 7})
    got = rv.phase_landed_paths(9901)
    check("2. all 6000 phase-landed paths are listed (none evicted past 5000)",
          len(got) == 6000 and set(got) == set(paths), f"listed={len(got)}")
    rv._note_landed(9901, paths[:10], {"phase_id": 8})
    got = rv.phase_landed_paths(9901)
    check("3. a path landed again is listed once",
          len(got) == 6000 and len(set(got)) == 6000, f"listed={len(got)}")
    rv._LANDED_BY.pop(9901, None)

    if FAILS:
        print(f"[build-record] {len(FAILS)} FAILED")
        return 1
    print("[build-record] all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
