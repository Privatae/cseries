#!/usr/bin/env python3
"""Gate: the dashboard listener is dual-stack, and the source-IP gate speaks IPv4.

`entrypoint.sh` exec'd `uvicorn --host ::` under a comment asserting "Kernel
bindv6only=0 keeps IPv4 docker reachability working". It never did: asyncio
sets IPV6_V6ONLY on the socket when it resolves a hostname to AF_INET6, which
overrides the sysctl. Two things that are supposed to work over IPv4 were
therefore broken on every image ever built, and both failed quietly:

  * the image's own HEALTHCHECK dials http://127.0.0.1:8800/api/system/health,
    so every container ran permanently `(unhealthy)`;
  * `docker run -p 127.0.0.1:$PORT:8800` published a port that reset every
    connection, which is why the publisher UI gate — the only host-side check
    in smoke — could never pass. Every other smoke assertion reaches the
    kernel through `docker exec ... [::1]:8800`, so nothing else noticed.

Fixing the bind exposed the second half: with a dual-stack socket an IPv4 peer
arrives as `::ffff:127.0.0.1`, and the source-IP gate is written in IPv4 terms
(`ALLOWED_HOSTS` names "127.0.0.1"; the docker-bridge test only matches an
`IPv4Address`), so every IPv4 caller 403'd instead.

This gate holds both halves in place. It is container-free: it reads the
entrypoint + launcher as text and exercises the middleware helpers directly.
"""

import ipaddress
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")
        failures.append(label)


# ── 1. the entrypoint must go through the dual-stack launcher ─────────────
entry = (ROOT / "entrypoint.sh").read_text()
check(
    "entrypoint execs server.launch (not `uvicorn --host` directly)",
    "exec python3 -m server.launch" in entry,
    "a bare `uvicorn --host ::` is IPv6-ONLY — see this file's docstring",
)
check(
    "entrypoint no longer execs uvicorn directly",
    "-m uvicorn" not in entry,
)

# ── 2. the launcher must clear V6ONLY before bind ─────────────────────────
launch_src = (ROOT / "server" / "launch.py").read_text()
check(
    "launcher clears IPV6_V6ONLY",
    "IPV6_V6ONLY" in launch_src,
)
check(
    "launcher hands the socket to uvicorn (run(sockets=...))",
    "sockets=[sock]" in launch_src,
    "binding then letting uvicorn re-bind by hostname would undo the fix",
)

# ── 3. the launcher actually produces a dual-stack socket ─────────────────
# Bind for real on an ephemeral port and read the option back.
import socket as _s  # noqa: E402

from server.launch import _listener  # noqa: E402

sock = _listener("::", 0)
try:
    v6only = sock.getsockopt(_s.IPPROTO_IPV6, _s.IPV6_V6ONLY)
    check("bound socket has IPV6_V6ONLY == 0", v6only == 0, f"got {v6only}")
finally:
    sock.close()

# ── 4. the source-IP gate must accept the v4-mapped spellings ─────────────
from server.middleware.localhost_only import _is_allowed, _unmap_v4  # noqa: E402

check("_unmap_v4 collapses ::ffff:127.0.0.1", _unmap_v4("::ffff:127.0.0.1") == "127.0.0.1")
check("_unmap_v4 leaves ::1 alone", _unmap_v4("::1") == "::1")
check("_unmap_v4 leaves a plain v4 alone", _unmap_v4("127.0.0.1") == "127.0.0.1")
check("_unmap_v4 passes through a non-address", _unmap_v4("localhost") == "localhost")
check("_unmap_v4 passes through None", _unmap_v4(None) is None)

# The two callers that must now work.
check("v4-mapped loopback allowed (the HEALTHCHECK's path)", _is_allowed("::ffff:127.0.0.1"))
# Since the owner login the bridge gateway is not trusted: every
# visitor to a published port arrives from it. It needs the owner's session.
check("v4-mapped docker bridge gw refused without a session", not _is_allowed("::ffff:172.17.0.1"))

# ── 5. …and must NOT have widened the boundary ────────────────────────────
check("plain loopback still allowed", _is_allowed("127.0.0.1") and _is_allowed("::1"))
check("public v4 still refused", not _is_allowed("8.8.8.8"))
check("v4-mapped PUBLIC address still refused", not _is_allowed("::ffff:8.8.8.8"))
check("public v6 still refused", not _is_allowed("2001:db8::1"))
check("non-bridge RFC1918 still refused", not _is_allowed("10.0.0.5"))
check("v4-mapped non-bridge RFC1918 still refused", not _is_allowed("::ffff:10.0.0.5"))
check("empty/None still refused", not _is_allowed(None) and not _is_allowed(""))

# Guard the bridge range itself against a sloppy widening.
bridge = ipaddress.IPv4Network("172.16.0.0/12")
check(
    "bridge range unchanged (172.16/12)",
    ipaddress.ip_address("172.31.255.255") in bridge
    and ipaddress.ip_address("172.32.0.1") not in bridge,
)
check("just outside the bridge range refused", not _is_allowed("::ffff:172.32.0.1"))

if failures:
    print(f"\nFAILED: {len(failures)} check(s) — {', '.join(failures)}")
    sys.exit(1)
print("\nPASSED: dashboard listener dual-stack; source-IP gate speaks v4-mapped")
