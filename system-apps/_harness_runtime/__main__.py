#!/usr/bin/env python3
"""Generic Lane-A harness entrypoint: `python3 -m _harness_runtime <harness_id>`.

Replaces the per-vendor bin/main.py shims (the dock apps were removed in audit
phase 3b). The Lane-A image sets PORT + passes the harness id; this resolves the
HarnessSpec and serves the shared /health /status /run /sessions surface. The
claude-code SDK harness keeps its own bin (different engine)."""
import os
import sys

from _harness_runtime import harness_core
from _harness_runtime.specs import ALL_SPECS

hid = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HARNESS_ID", "")
spec = ALL_SPECS.get(hid)
if spec is None:
    raise SystemExit(f"_harness_runtime: unknown harness id {hid!r} "
                     f"(known: {sorted(ALL_SPECS)})")

# Bound core dumps before anything can crash. A harness sidecar is not
# in the entity pod, so entrypoint.sh's ulimit never reached it — measured:
# these ran unlimited/unlimited with a writable cwd. Bounded, not
# banned: the dump is the user's to keep.
_prev_core = harness_core.bound_core_dumps()

log = harness_core.register(spec)
if _prev_core is not None:
    log.info("core dumps bounded to 2 GiB (was soft=%s hard=%s)", *_prev_core)

# Route this harness through the user's own signed-rule gate rather than the
# node squid. Must run before the first /run: every spec's build_env returns
# {**os.environ, ...}, so the rewrite lands once here, not per vendor.
harness_core.adopt_nac_egress_lane(log)

if __name__ == "__main__":
    log.info("%s sidecar starting on port %s", hid, os.environ.get("PORT"))
    harness_core.sidecar.serve()
