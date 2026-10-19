#!/usr/bin/env python3
"""
Gate: code search's tree-sitter parsers ship IN the image (Dockerfile.linux,
builder stage), so a Host chunks code by function with no network.

tree-sitter-language-pack ships no grammars. Until 2026-10-03 the first
get_parser() in each pod's life downloaded a 22 MB bundle from github.com into
~/.cache — an emptyDir, so every restart did it again, and a pod without
egress chunked every file by 60-line window instead of by function (measured
on one Host: the cache did not exist until a chunk call made the pod fetch it).

  P1  TREE_SITTER_LANGUAGE_PACK_CACHE_DIR names the baked directory, outside
      the home directory, owned by this process's user (the pack refuses a
      cache it does not own)
  P2  every language the chunker parses (chunker.CHUNKABLE_NODE_TYPES) is
      already on disk there — nothing left to download
  P3  with every download made impossible (the manifest URL is a closed local
      port, HOME does not exist), a .py file chunks by function with its
      symbols and a .ts file by declaration — no line windows
  P4  each of those languages yields a parser under the same conditions
  P5  an exported or const declaration in TS/JS is indexed under its own name
      and kind (export function firstThing -> function 'firstThing'), so a
      name search finds it; until 2026-10-03 every one came back unnamed
  P6  module-level code between definitions (a constants table) is indexed
      as a 'module' chunk; until 2026-10-13 only definitions and the tail
      after the last one were

Run in the image: python3 /app/scripts/test_codesearch_parsers_baked.py
Prints OK <n> checks on success (smoke reads the last line).
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT / "brain"))

FAILS: list = []
PASSED = 0


def check(label, cond, detail=""):
    global PASSED
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:400]}]"))
    if cond:
        PASSED += 1
    else:
        FAILS.append(label)


PY = '''\
import os

# A module-level table between the imports and the first definition: since
# 2026-10-13 (chunker version 2) it is indexed as a 'module' chunk.
LANE_TABLE = {
    "first_function": "read",
    "Second": "operate",
}

def first_function(a, b):
    """Adds two numbers and says so, at enough length to be its own chunk."""
    total = a + b
    return total


class Second:
    """A class with a method, long enough to clear the minimum chunk size."""
    def method(self):
        return first_function(1, 2)
'''

TS = '''\
export function firstThing(a: number, b: number): number {
  // long enough to be a chunk on its own, comfortably over the minimum
  return a + b;
}

export interface SecondThing {
  name: string;
  value: number;
  description: string;
}

export const DRAG_TYPE =
  // long enough to be a chunk on its own, comfortably over the minimum size
  "application/x-workbench-project";
'''

JS = '''\
export default class Thing {
  // long enough to be a chunk on its own, comfortably over the minimum size
}

let plainValue =
  // long enough to be a chunk on its own, comfortably over the minimum size
  2;
'''

# Runs in a child so the environment that forbids a download is in force from
# the pack's first import.
CHILD = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from codesearch import chunker
py = chunker.chunk_file("probe.py", sys.argv[2])
ts = chunker.chunk_file("probe.ts", sys.argv[3])
js = chunker.chunk_file("probe.js", sys.argv[4])
parsers = {l: chunker._try_tree_sitter(l) is not None for l in sorted(chunker.CHUNKABLE_NODE_TYPES)}
print(json.dumps({"py": [[c.chunk_kind, c.symbol] for c in py],
                  "ts": [[c.chunk_kind, c.symbol] for c in ts],
                  "js": [[c.chunk_kind, c.symbol] for c in js], "parsers": parsers}))
'''


def main() -> int:
    from codesearch import chunker
    want = sorted(chunker.CHUNKABLE_NODE_TYPES)

    cache = os.environ.get("TREE_SITTER_LANGUAGE_PACK_CACHE_DIR", "")
    home = os.path.expanduser("~")
    p = pathlib.Path(cache) if cache else None
    check("P1: TREE_SITTER_LANGUAGE_PACK_CACHE_DIR names a baked directory outside the home dir",
          bool(cache) and p.is_dir() and not cache.startswith(home), f"{cache!r} home={home!r}")
    check("P1: that directory is owned by this process's user (the pack refuses one it does not own)",
          bool(p) and p.is_dir() and p.stat().st_uid == os.getuid(),
          f"uid={p.stat().st_uid if p and p.exists() else None} me={os.getuid()}")

    try:
        import tree_sitter_language_pack as tslp
        on_disk = set(tslp.downloaded_languages())
    except Exception as e:  # noqa: BLE001
        on_disk = set()
        print(f"  (language pack unreadable: {e})")
    check("P2: every language the chunker parses is already on disk",
          set(want) <= on_disk, f"missing {sorted(set(want) - on_disk)}")

    env = dict(os.environ,
               TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL="http://127.0.0.1:9/parsers.json",
               HOME="/nonexistent-home-for-this-gate")
    r = subprocess.run([sys.executable, "-c", CHILD, str(ROOT / "brain"), PY, TS, JS],
                       env=env, capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        out = {"py": [], "ts": [], "js": [], "parsers": {}}
        print(f"  (child failed rc={r.returncode}: {r.stderr[-400:]})")
    py_kinds = {k for k, _ in out["py"]}
    py_syms = {s for _, s in out["py"]}
    check("P3: with no download possible, a .py file chunks by function, not by line window",
          "line_window" not in py_kinds and {"first_function", "Second"} <= py_syms, out["py"])
    check("P3: ... and a .ts file by declaration",
          len(out["ts"]) == 3 and "line_window" not in {k for k, _ in out["ts"]}, out["ts"])
    check("P4: every language the chunker parses yields a parser with no download possible",
          bool(out["parsers"]) and all(out["parsers"].values()), out["parsers"])

    check("P6: module-level code between definitions is indexed (a 'module' chunk holds LANE_TABLE)",
          "module" in py_kinds, out["py"])
    check("P5: an exported TS declaration is indexed under its own name and kind",
          out["ts"] == [["function", "firstThing"], ["interface", "SecondThing"], ["variable", "DRAG_TYPE"]],
          out["ts"])
    check("P5: ... and in JS, a default-exported class and a plain let too",
          out["js"] == [["class", "Thing"], ["variable", "plainValue"]], out["js"])

    if FAILS:
        print(f"FAIL {len(FAILS)} of {len(FAILS) + PASSED}: " + "; ".join(FAILS))
        return 1
    print(f"OK {PASSED} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
