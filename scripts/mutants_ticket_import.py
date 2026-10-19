#!/usr/bin/env python3
"""Mutants for scripts/test_ticket_import.py — each one removes ONE rule the
gate claims to pin; every mutant must turn the gate red.

Not a gate itself (it rewrites source files, one at a time, and restores
them): run it by hand when the import or its gate changes. It drives the gate
the same way a standalone run does, so it needs a DB carrying init-db.sql +
migrations and the image to run in:

    GATE_DB_CONTAINER=<pg container> CSERIES_IMAGE=cseries:vX \\
        python3 scripts/mutants_ticket_import.py

Prints one line per mutant (RED = caught, GREEN = the gate missed it) and
exits non-zero if any mutant survives.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TI = "brain/mastercode/runtime/ticket_import.py"
TOOLS = "brain/mastercode/runtime/tools.py"

MUTANTS = [
    ("user_only dropped", TI, '"user_only": True}', "}"),
    ("the module's agent refusal dropped", TI, "    if agent_grant is not None:\n", "    if False:\n"),
    ("the grant not passed down by the dispatch", TOOLS,
     "return await _exec_tickets(args, agent_grant=grant_bits)", "return await _exec_tickets(args)"),
    ("the Host's bits not checked", TI, "    if bits & IMPORT_BITS != IMPORT_BITS:", "    if False:"),
    ("phases land ready", TI, "'planning', 'none', 'review', '{}')", "'ready', 'none', 'review', '{}')"),
    ("phases wait for the one before", TI, "'planning', 'none', 'review', '{}')",
     "'planning', 'none', 'review', NULL)"),
    ("sha256 not checked", TI, 'if b.get("sha256") != digest(b):', "if False:"),
    ("NUL not refused", TI, '        if "\\x00" in v:', "        if False:"),
    ("tickets re-imported", TI, '        if ref in have["refs"]:', "        if False:"),
    ("phase keys not recorded", TI, "WHERE id = $1 AND config->'import'->>'bundle_id' = $4",
     "WHERE id = $1 AND config->'import'->>'bundle_id' = $4 AND false"),
    ("archive sections re-imported", TI,
     "            if sorted((n, of, sha) for _i, _st, n, of, sha in stored) == want:",
     "            if False:"),
    ("a section judged by position alone, not content", TI,
     "            if sorted((n, of, sha) for _i, _st, n, of, sha in stored) == want:",
     "            if sorted(n for _i, _st, n, of, sha in stored) == [w[0] for w in want]:"),
    ("stored raw pieces not cleared first", TI,
     '                if any(st == "raw" for _i, st, _n, _of, _sha_ in stored):', "                if False:"),
    ("the lock ignored", TI, "        if not got:", "        if False:"),
    ("source not 'manual'", TI, 'source="manual"', 'source="runtime"'),
    ("a truncated gzip uncaught", TI, "(OSError, EOFError, zlib.error)", "(OSError,)"),
    ("no project directory", TI, "        ensure_project_dir(int(project_id))\n", ""),
    ("a blank piece counted", TI, 'parts = [x for x in pieces(s["text"]) if x.strip()]',
     'parts = pieces(s["text"])'),
    ("a repair dismisses promoted pieces", TI, "              AND metadata->>'import_key' = $3 AND status = 'raw'\n",
     "              AND metadata->>'import_key' = $3 AND status <> 'dismissed'\n"),
    ("promoted pieces rewritten", TI, "            } for n, text in enumerate(parts) if n not in keep]",
     "            } for n, text in enumerate(parts)]"),
    ("a promoted piece of another export kept in place", TI,
     '                    if st == "promoted" and (n, of, sha) in want}',
     '                    if st == "promoted"}'),
    ("a stale promoted piece left in the document", TI, "                if detach:", "                if False:"),
    ("a detached piece kept under the archive's name", TI,
     "'upload_batch', $3::text, 'from_file', $4::text,", "'upload_batch', $3::text,"),
    ("one bad ticket stops the rest", TI,
     "        except Exception as e:  # noqa: BLE001 — one item never stops the rest",
     "        except ZeroDivisionError as e:  # mutant"),
]


def gate() -> str:
    cmd = ["docker", "run", "--rm", "--network", f"container:{os.environ['GATE_DB_CONTAINER']}",
           "-v", f"{ROOT}:/gate:ro", "-e", "APP_ROOT=/gate", "-e", "SMOKE_PG_HOST=127.0.0.1",
           "--entrypoint", "python3", os.environ["CSERIES_IMAGE"], "-B", "/gate/scripts/test_ticket_import.py"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip().splitlines()
    return out[-1] if out else "(no output)"


def main() -> int:
    survived = 0
    for name, rel, a, b in MUTANTS:
        path = ROOT / rel
        orig = path.read_text()
        if orig.count(a) < 1:
            print(f"STALE {name}: the text it mutates is gone — update this list")
            survived += 1
            continue
        try:
            path.write_text(orig.replace(a, b, 1))
            last = gate()
        finally:
            path.write_text(orig)
        red = not last.startswith("OK")
        survived += 0 if red else 1
        print(f"{'RED  ' if red else 'GREEN'} {name} | {last[:120]}")
    print(f"{len(MUTANTS) - survived} of {len(MUTANTS)} mutants caught")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
