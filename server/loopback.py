"""
server/loopback.py — the kernel's OWN loopback base URL, once.

CLIENT dial target, NOT a bind address: the kernel's listener keeps binding
`::` (entrypoint.sh — reachable off-pod for kubelet probes and the harness
lane). This constant exists only for callers INSIDE the same network
namespace (the kernel process itself, brain-side meta-tools, in-pod system
apps and scripts) dialing their own server; nothing off the pod ever uses it.

The kernel binds `::` (entrypoint.sh — entity pods are IPv6-only; the kubelet
probes the pod's IPv6 address). In k8s that socket is v6-only: IPv4 loopback
(127.0.0.1) does NOT connect in-pod — a live failure on a Host:
`sidecar launch unreachable: Cannot connect to host 127.0.0.1:8800` while
/proc/net showed :8800 bound on :: and ::1 only. `[::1]` connects everywhere
we run (k8s pods AND legacy docker, whose loopback carries ::1 by default),
which is why the smoke test and the mTLS front terminator already speak
[::1]/::1 — the callers just never followed.

EVERY in-process/in-pod dial of the kernel's own HTTP surface must build its
URL from this constant — never a literal at the call site. The smoke test
exercises the derived sidecar URLs against the real bind, so a regression to
a hardcoded 127.0.0.1 fails the ship.

Deliberately stdlib-only with no package side effects (brain-side modules and
scripts import it without dragging server.main in).
"""

import os

# Bracketed IPv6 loopback host, ready for URL composition.
KERNEL_LOOPBACK_HOST = "[::1]"

KERNEL_LOOPBACK_PORT = int(os.environ.get("DASHBOARD_PORT", "8800"))

# The base every in-pod caller composes on, e.g. f"{KERNEL_LOOPBACK_BASE}/mcp".
KERNEL_LOOPBACK_BASE = f"http://{KERNEL_LOOPBACK_HOST}:{KERNEL_LOOPBACK_PORT}"
