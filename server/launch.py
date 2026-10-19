"""Bind the dashboard listener dual-stack, then hand the socket to uvicorn.

Why this exists instead of `uvicorn --host ::`:

`entrypoint.sh` used to exec uvicorn with `--host ::` and a comment claiming
"Kernel bindv6only=0 keeps IPv4 docker reachability working". The sysctl does
read 0 in the container, but it never got a vote: when asyncio's
`create_server()` resolves a *hostname* to an AF_INET6 address it sets
`IPV6_V6ONLY` on the socket itself, which overrides the sysctl. The listener
was therefore IPv6-ONLY on every image ever built, and two things that are
supposed to work over IPv4 silently did not:

  * the image's own HEALTHCHECK (Dockerfile.linux) dials
    `http://127.0.0.1:8800/api/system/health`, so every container ran
    permanently `(unhealthy)`;
  * `docker run -p 127.0.0.1:$PORT:8800` published a port that always reset
    the connection — docker's proxy forwards to the container's IPv4 address
    and nothing was listening there. Smoke never noticed, because it reaches
    the kernel through `docker exec ... [::1]:8800`.

Binding the socket here lets us clear `IPV6_V6ONLY` explicitly before bind,
which is the same idiom the harness sidecar already uses
(cli/cseries_sidecar.py::_Server.server_bind).

IPv6 behaviour is unchanged — entity pods are IPv6-only and the kubelet's
liveness/readiness probes dial the pod's IPv6 address, which still lands on
this same socket. This only ADDS the IPv4 path.
"""

import os
import secrets
import socket
import sys

import uvicorn

BIND_HOST = os.environ.get("DASHBOARD_BIND_HOST", "::")
BIND_PORT = int(os.environ.get("DASHBOARD_PORT", "8800"))


def _listener(host: str, port: int) -> socket.socket:
    """A bound, listening socket — dual-stack when host is an IPv6 wildcard."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        # The whole point of this module. Best-effort: a kernel that refuses
        # the option leaves us exactly where `--host ::` already was, so
        # failing here would be strictly worse than carrying on.
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
    sock.bind((host, port))
    sock.listen(socket.SOMAXCONN)
    sock.set_inheritable(True)
    return sock


def ensure_internal_api_secret() -> None:
    """Make this Host's INTERNAL_API_SECRET, unless one was supplied.

    The secret gates only calls this process makes to itself over loopback
    (server/applications/middleware.py _INTERNAL_LOOPBACK_PATHS, routes/mcp.py
    internal auth and its self-call, routes/synaptive.py). Every reader is in
    this process or a child of it, so the value lives in this process's
    environment and nowhere else. Until now the operator generated it into a
    Kubernetes Secret (cseries-internal-api) that anyone with cluster admin could
    read; a Host whose operator no longer supplies it makes its own here, before
    uvicorn imports the app.

    A value already in the environment is kept: an operator below the version
    gate still supplies one, and the smoke test supplies a known one so it can
    drive the loopback from `docker exec`. A process started later by an `exec` into
    the container does not inherit this value, which is intended."""
    if not os.environ.get("INTERNAL_API_SECRET"):
        os.environ["INTERNAL_API_SECRET"] = secrets.token_hex(32)


def main() -> int:
    ensure_internal_api_secret()
    sock = _listener(BIND_HOST, BIND_PORT)
    config = uvicorn.Config(
        "server.main:app",
        log_level=os.environ.get("DASHBOARD_LOG_LEVEL", "info"),
        access_log=False,
        # The request's client must be the socket's peer. The localhost gate
        # trusts a loopback peer with no session, and uvicorn's proxy-header
        # support (on by default) replaces the peer with X-Forwarded-For from
        # any address in FORWARDED_ALLOW_IPS; set that to "*", as guides for
        # running behind a proxy suggest, and any visitor who sends
        # "X-Forwarded-For: 127.0.0.1" is loopback. Measured on this tree:
        # the full-scope token mint answered 200 to exactly that, with no
        # session (scripts/test_owner_login.sh). Off, whatever the env says.
        proxy_headers=False,
    )
    uvicorn.Server(config).run(sockets=[sock])
    return 0


if __name__ == "__main__":
    sys.exit(main())
