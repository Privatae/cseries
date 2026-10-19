#!/usr/bin/env python3
"""Phase 3b: a coding harness is AVAILABLE when its creds are connected,
independent of any dock app. Exercises the real connections.connected_harness_ids
against the real PROVIDERS map, toggling creds files on disk. Run in-image
(has fastapi): docker run --entrypoint python3 :latest scripts/test_harness_availability.py"""
import os, sys, pathlib
sys.path.insert(0, "/app"); sys.path.insert(0, "/app/brain")
FAILS = []
def check(n, ok, d=""):
    print(f"[harness-avail] {'ok  ' if ok else 'FAIL'} {n}" + (f" — {d}" if d and not ok else ""))
    (FAILS.append(n) if not ok else None)

from server.routes.review import HARNESS_APP_IDS
from server.routes.harnesses import connected_harness_ids, PROVIDERS, _harness_app_to_provider

m = _harness_app_to_provider()
check("every harness app id maps to a provider",
      all(h in m for h in HARNESS_APP_IDS), f"missing: {[h for h in HARNESS_APP_IDS if h not in m]}")

def wipe():
    for h in HARNESS_APP_IDS:
        p = pathlib.Path(m[h]["creds_path"])
        if p.exists():
            p.unlink()
def make(h):
    p = pathlib.Path(m[h]["creds_path"]); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"tok":"x"}')

wipe()
check("no creds -> nothing available", connected_harness_ids(HARNESS_APP_IDS) == [])
make("codex"); make("grok")
avail = connected_harness_ids(HARNESS_APP_IDS)
check("codex+grok connected -> exactly those (order per HARNESS_APP_IDS)",
      avail == [h for h in HARNESS_APP_IDS if h in ("codex", "grok")], repr(avail))
make("cursor")
avail = connected_harness_ids(HARNESS_APP_IDS)
check("adding cursor creds makes it available (no dock app involved)",
      set(avail) == {"codex", "grok", "cursor"}, repr(avail))
wipe()
print(f"[harness-avail] {'PASS' if not FAILS else 'FAIL'} ({len(FAILS)} failing)")
sys.exit(1 if FAILS else 0)
