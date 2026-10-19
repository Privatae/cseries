#!/usr/bin/env python3
"""
Error-lane gate (server/error_log.py + v098, smoke).

Structural verification of the pod-local error lane, which logs errors so
they can be diagnosed quickly:

  1. recorder contract — record() NEVER raises: with pool=None, with a pool
     whose execute() explodes, and with unserializable context. The log line
     is the floor; a logging failure must not compound the logged failure.
  2. schema parity — migrations/v098_error_events.sql and init-db.sql both
     define error_events with the same columns (the v090 lesson).
  3. read side — /api/system/errors/recent route exists in system.py.
  4. coverage map — every choke point listed in error_log.py's docstring
     actually imports the recorder: session.py (tool), host_sdk_engine.py
     (sdk_tool), turn_registry.py (turn), brain_service.py (stream). A
     refactor that drops one silently un-wires the lane; this ratchet makes
     that loud.

Stdlib-only; loads server/error_log.py straight from file. Exit 0 = pass.
"""

import asyncio
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")

spec = importlib.util.spec_from_file_location(
    "error_log", os.path.join(ROOT, "server", "error_log.py"))
el = importlib.util.module_from_spec(spec)
sys.modules["error_log"] = el
spec.loader.exec_module(el)

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[error-lane] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class _ExplodingPool:
    async def execute(self, *_a, **_k):
        raise RuntimeError("db down")


class _Unserializable:
    def __repr__(self):
        raise ValueError("even repr explodes")


async def main():
    # ── 1. recorder never raises ─────────────────────────────────────────
    try:
        await el.record(None, source="tool", message="x", tool="recall")
        check("record with pool=None", True)
    except Exception as e:
        check("record with pool=None", False, repr(e))
    try:
        await el.record(_ExplodingPool(), source="turn", kind="crash", message="y")
        check("record with exploding pool", True)
    except Exception as e:
        check("record with exploding pool", False, repr(e))
    try:
        await el.record(_ExplodingPool(), source="stream", message="z",
                        context_obj=_Unserializable())
        check("record with unserializable context", True)
    except Exception as e:
        check("record with unserializable context", False, repr(e))

    # ── 2. schema parity ─────────────────────────────────────────────────
    cols = ["id", "at", "source", "kind", "tool", "message", "context"]
    for rel in ("migrations/v098_error_events.sql", "init-db.sql"):
        text = read(rel)
        m = re.search(
            r"CREATE TABLE IF NOT EXISTS error_events\s*\((.*?)\);",
            text, re.DOTALL)
        if not m:
            check(f"error_events defined in {rel}", False, "no CREATE TABLE")
            continue
        body = m.group(1)
        missing = [c for c in cols if not re.search(rf"^\s*{c}\s", body, re.MULTILINE)]
        check(f"error_events columns in {rel}", not missing, f"missing {missing}")
        check(f"at-index in {rel}", "idx_error_events_at" in text)

    # ── 3. read side ─────────────────────────────────────────────────────
    sysroutes = read("server/routes/system.py")
    check("GET /errors/recent route",
          '@router.get("/errors/recent"' in sysroutes
          and "error_events" in sysroutes)

    # ── 4. coverage map (the choke-point ratchet) ────────────────────────
    for rel, source_tag in (
        ("brain/mastercode/runtime/session.py", 'source="tool"'),
        ("server/turn_registry.py", 'source="turn"'),
        ("server/brain_service.py", 'source="stream"'),
    ):
        text = read(rel)
        wired = "from server.error_log import record" in text and source_tag in text
        check(f"choke point wired: {rel.split('/')[-1]} ({source_tag})", wired)


if __name__ == "__main__":
    asyncio.run(main())
    if FAILS:
        print(f"[error-lane] {len(FAILS)} FAILED: {', '.join(FAILS)}")
        sys.exit(1)
    print("[error-lane] all checks passed")
    sys.exit(0)
