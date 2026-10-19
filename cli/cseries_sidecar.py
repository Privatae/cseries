"""
cseries-sidecar — tiny helper for writing process-runtime apps.

Usage (in your sidecar's main):

    from cseries_sidecar import sidecar, run_until_terminated, KernelClient

    @sidecar.handler("GET", "/health")
    def health():
        return {"ok": True}

    @sidecar.handler("GET", "/")
    def index():
        return _html("<h1>hello from a sidecar</h1>")

    @sidecar.handler("GET", "/memory")
    def memory(request):
        # Use the incoming request's token to call back into the kernel.
        token = request.headers.get("X-Application-Token")
        kernel = KernelClient(token=token)
        return kernel.get("/api/brain/memory?q=hello")

    if __name__ == "__main__":
        sidecar.serve()

The helper:
  * Reads PORT, PRIVATAE_APP_ID, CSERIES_KERNEL_URL from env
  * Builds a tiny stdlib-only HTTP server (no FastAPI dependency
    required in the sidecar's image)
  * Provides a route decorator and a KernelClient class
  * Handles graceful shutdown on SIGTERM

For sidecars that prefer their own HTTP stack (FastAPI, Flask, etc.)
this module is optional — they just need to listen on $PORT and
expose a /health endpoint within the manifest's ready_timeout.
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional
from urllib.parse import urlparse

log = logging.getLogger("cseries_sidecar")


PORT = int(os.environ.get("PORT", "0"))
APP_ID = os.environ.get("PRIVATAE_APP_ID", "unknown")
# [::1] — the kernel binds ::, v6-only in k8s pods; IPv4 loopback does not
# connect there (server/loopback.py; this helper stays stdlib/self-contained).
KERNEL_URL = os.environ.get("CSERIES_KERNEL_URL", "http://[::1]:8800")

# Sidecar bind address. Default loopback = the same-pod model (the entity reaches
# the sidecar via 127.0.0.1). The own-container/per-entity-subnet model sets
# PRIVATAE_SIDECAR_BIND=0.0.0.0 so the entity can reach it pod-to-pod; egress is then
# bounded by the per-pod CiliumNetworkPolicy, not by the loopback bind.
BIND_HOST = os.environ.get("PRIVATAE_SIDECAR_BIND", "127.0.0.1")

# Root for the per-task workspace the dispatcher clones in / collects out.
# Own-pod harnesses mount their ephemeral PVC here; the host delivers the worktree
# over /workspace/load and collects the worktree-after over /workspace/export
# (the host never shares a filesystem with the harness pod).
WORKSPACE_ROOT = os.environ.get("PRIVATAE_WORKSPACE", "/var/lib/cseries/projects")

# ── Control-plane capability ────────────────────────────────────────────────
# Without a capability, any process in the entity pod could POST /run on a live
# harness Service and drive a build — no dispatcher, no `build` tool, no
# code-exec call — because nothing else on this control plane authenticates a
# caller. Every route here requires the capability.
#
# WHY THIS IS NOT A NETWORK POLICY. The dispatcher (server/routes/review.py)
# runs INSIDE the entity pod. The dispatcher and any rogue caller are therefore
# the same pod, the same Cilium identity, often the same process — no ingress
# rule can tell them apart. The separation has to be a secret the dispatcher
# holds and a captured agent does not: dispatched agents never execute code, so a
# capability that is never written to entity disk or env is out of their reach
# unless a tool leaks it.
#
# The platform already ships this control in the OTHER direction — the editor
# harness presents a per-wake `git-cap` to the entity's git-credential broker,
# which 403s without it (server/applications/harness_backend.py:check_git_cap).
# This is the missing mirror: the harness authenticated to the entity, but the
# entity never authenticated to the harness.
#
def _env(new: str, old: str, default: str = "", *, strip: bool = False) -> str:
    """The value under `new`, else under `old`, else `default`.

    A bastion coder run passes only the PRIVATAE_* names; the platform operator
    still sets the older names on pool pods. An empty value counts as unset, so
    an empty new name never hides a value under the old one. With `strip`, both
    values are stripped first, so a whitespace-only new value counts as unset
    too: for the capability that matters, because a blank capability fails OPEN.
    Socket paths are not stripped, so a blank path still refuses to start.
    """
    if strip:
        return ((os.environ.get(new) or "").strip()
                or (os.environ.get(old) or "").strip() or default)
    return os.environ.get(new) or os.environ.get(old) or default


# The capability is minted BY THE OPERATOR per pod generation, injected here
# from the per-harness creds Secret, and returned to the dispatcher in the
# harness.wake response — so holding it is proof of a bridge-governed wake
# (the bridge verifies the entity cert CN + EXECUTE_TOOL before forwarding).
CONTROL_CAP = _env("PRIVATAE_HARNESS_CAP", "CSERIES_HARNESS_CAP", strip=True)
CAP_HEADER = "X-CSeries-Harness-Cap"
# /health is the kubelet's EXEC probe (curl localhost, no header) — it carries no
# capability and never will. It is a liveness bit, not a control op.
CAP_OPEN_PATHS = frozenset({"/health"})
# Rollout gate. Harness images and entity images update on independent
# schedules, so a fail-closed first ship would break every entity whose cseries
# tag predates the dispatcher sending the header. Ship OBSERVING (deny=0, log
# every unauthenticated request), read the logs, then flip. Set on the harness
# Deployment template by the operator, so one env var rolls or reverts it.
CAP_ENFORCE = _env("PRIVATAE_HARNESS_CAP_ENFORCE", "CSERIES_HARNESS_CAP_ENFORCE", "0",
                   strip=True).lower() not in ("", "0", "false", "no", "off")

# ── Harness-only routes ─────────────────────────────────────────────────────
# /workspace/load, /workspace/export, /session/export and /run/progress act on
# this process's filesystem for ONE caller: the kernel's dispatcher, talking to
# an own-pod harness over its Service (server/routes/review.py _clone_in,
# _collect_out, _collect_session). Were they in the shared `sidecar`, every
# process that imported it would get them, including the Claude Code app's sidecar, which
# the supervisor runs INSIDE the Host container with the kernel's env. There
# CSERIES_HARNESS_CAP is unset, so _cap_denial lets every request through, and
# WORKSPACE_ROOT is the kernel's own /var/lib/cseries/projects. The kernel proxy
# forwards any path to an in-container process app with no token check, so a
# community app's page could POST /system-apps/claude-code/workspace/load?dir=
# and delete every project (an empty dir resolved to the root), or GET
# /workspace/export and read all of them.
#
# So the routes exist only in a process that says it IS a harness:
# CSERIES_SIDECAR_ROLE=harness, set by ENV in every sidecars/harness-*/Dockerfile.
# It is a property of the image, so the flag and this code always ship
# together; the Host image never sets it, and the supervisor strips it from
# the env it hands in-container apps (supervisor._child_env). Anything else
# (unset, a typo, a port number from a manifest's port_env) is not a harness
# and the routes are absent: 404, fail closed.
#
# Not keyed on CSERIES_HARNESS_CAP: the operator injects that optional=True, so a
# harness pod may legitimately run without one and must keep working.
SIDECAR_ROLE = os.environ.get("CSERIES_SIDECAR_ROLE", "").strip().lower()
HARNESS_MODE = SIDECAR_ROLE == "harness"
# The kernel proxy refuses these same paths for in-pod apps
# (server/applications/proxy.py _HARNESS_ONLY_SIDECAR_PATHS); the gate
# scripts/test_sidecar_workspace_routes_harness_only.py holds the two in step.
HARNESS_ONLY_ROUTES = frozenset({
    ("POST", "/workspace/load"),
    ("GET", "/workspace/export"),
    ("GET", "/session/export"),
    ("GET", "/run/progress"),
})


# ── Unix-socket mode (coder runs on a bastion) ──────────────────────────────
# A coding run on a user's machine runs with `--network none`: no interface but
# loopback. Its only ways in and out are two unix sockets the bastion
# bind-mounts in (the bastion's coder mode):
#   PRIVATAE_SIDECAR_EGRESS_SOCK   the bastion's HTTP CONNECT proxy. A loopback
#                                  TCP forwarder in front of it becomes
#                                  HTTP(S)_PROXY, because the vendor CLIs only
#                                  speak TCP proxies.
#   PRIVATAE_SIDECAR_CONTROL_SOCK  this server's control plane, served on the
#                                  socket instead of TCP. The bastion forwards a
#                                  tunnel port to it.
# The older CSERIES_SIDECAR_* spellings are still read when the new ones are
# unset. Both unset (every pool pod) is the TCP behaviour above, unchanged. A
# value that is set must be an absolute path, or serve() refuses to start:
# falling back to TCP would leave a run with no network and no control plane,
# failing in a way nobody could see.
EGRESS_SOCK = _env("PRIVATAE_SIDECAR_EGRESS_SOCK", "CSERIES_SIDECAR_EGRESS_SOCK")
CONTROL_SOCK = _env("PRIVATAE_SIDECAR_CONTROL_SOCK", "CSERIES_SIDECAR_CONTROL_SOCK")


def _require_abs_socket_paths() -> None:
    for new, old, val in (("PRIVATAE_SIDECAR_EGRESS_SOCK", "CSERIES_SIDECAR_EGRESS_SOCK", EGRESS_SOCK),
                          ("PRIVATAE_SIDECAR_CONTROL_SOCK", "CSERIES_SIDECAR_CONTROL_SOCK", CONTROL_SOCK)):
        var = new if os.environ.get(new) else old  # name the variable that was set
        if val and not os.path.isabs(val):
            raise RuntimeError(
                f"{var} must be an absolute path, got {val!r}; refusing to "
                "start rather than fall back to TCP")


def _cap_denial(path: str, presented: Optional[str]) -> Optional[str]:
    """Reason this control-plane request is unauthorized, or None if it is fine.

    Fails OPEN when no capability was issued (`CSERIES_HARNESS_CAP` empty): a pod
    the operator started without one cannot verify anything, and a harness that
    refuses every build because a Secret key was missing is a worse outage than
    the exposure it guards. That state is logged loudly at startup, so an
    un-capped harness is visible rather than quietly unprotected.
    """
    if path in CAP_OPEN_PATHS:
        return None
    if not CONTROL_CAP:
        return None
    if not presented:
        return "control-plane requests require the harness capability header"
    import hmac
    if not hmac.compare_digest(CONTROL_CAP, presented):
        return "harness capability does not match this pod"
    return None


# How long the bridge waits for the node-local squid to ACCEPT a connection.
# Bounded on purpose (a dead proxy must not hang every child forever) — and
# CONNECT-ONLY on purpose: see _bridge_connect.
BRIDGE_CONNECT_TIMEOUT_S = 10.0


def _bridge_connect(host: str, port: int) -> socket.socket:
    """One upstream connection for the proxy bridge: connect within
    BRIDGE_CONNECT_TIMEOUT_S, then run with NO timeout.

    `socket.create_connection(..., timeout=N)` leaves N on the socket AFTER
    the connect — every later recv()/send() raises TimeoutError once the peer
    is silent for N seconds, and the pump treated that like a closed peer.
    MEASURED on a live Host's 7-harness fan-out:
      * grok (883 s, exit 1): four think→prose cycles, each cut as soon as
        the model went quiet to produce a large tool call — the final error
        frame landed 10.0 s after the last streamed token, and the CLI died
        with "reqwest error stream: Transport error: error decoding response
        body" (a stream cut mid-body), never having written a file.
      * antigravity (81 s, exit 1): three "error_message" steps 15 s apart
        after an agent_response that streamed 0 output tokens for 13 s, then
        "There was a network issue connecting to the server, please try
        again." — a model call whose time-to-first-byte exceeded 10 s.
    Both went through this bridge (the operator sets HTTPS_PROXY to an IPv6
    literal on every harness pod). The cut was ours, not the vendor's and
    not squid's. A vendor stream may stay silent until the run's own
    max_minutes (how long one model call may go with nothing arriving) runs
    out; that is the only clock here."""
    up = socket.create_connection((host, port), timeout=BRIDGE_CONNECT_TIMEOUT_S)
    up.settimeout(None)
    return up


def _bridge_unix_connect(path: str) -> socket.socket:
    """_bridge_connect for a unix-socket upstream: connect within
    BRIDGE_CONNECT_TIMEOUT_S, then run with no timeout, for the same reason."""
    up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        up.settimeout(BRIDGE_CONNECT_TIMEOUT_S)
        up.connect(path)
    except OSError:
        up.close()
        raise
    up.settimeout(None)
    return up


def _pump_bytes(a: socket.socket, b: socket.socket) -> None:
    """Copy a -> b until either side ends, then close both."""
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        for sk in (a, b):
            try:
                sk.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sk.close()
            except OSError:
                pass


def _bridge_unix_egress(path: Optional[str] = None) -> int:
    """Loopback TCP forwarder in front of the egress unix socket, and point
    every proxy variable at it. Returns the loopback port.

    Each accepted connection gets its own unix-socket connection, pumped both
    ways and closed when either side ends. An upstream that cannot be reached
    closes the client connection; the client sees a proxy that hung up, the
    same as a dead squid behind _bridge_ipv6_proxy."""
    path = path or EGRESS_SOCK
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(64)
    lport = lsock.getsockname()[1]

    def _accept() -> None:
        while True:
            try:
                c, _ = lsock.accept()
            except OSError:
                return
            try:
                up = _bridge_unix_connect(path)
            except OSError:
                c.close()
                continue
            threading.Thread(target=_pump_bytes, args=(c, up), daemon=True).start()
            threading.Thread(target=_pump_bytes, args=(up, c), daemon=True).start()

    threading.Thread(target=_accept, daemon=True, name="unix-egress-bridge").start()
    # Unconditional: on a bastion this socket is the only way out, so every
    # proxy variable must name it whatever the image or the env said before.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[var] = f"http://127.0.0.1:{lport}"
    for var in ("NO_PROXY", "no_proxy"):
        os.environ[var] = "localhost,127.0.0.1,::1"
    log.info("unix egress bridge: 127.0.0.1:%d -> %s", lport, path)
    return lport


def _bridge_ipv6_proxy() -> None:
    """Node-based harness CLIs (cursor-agent, opencode/Bun) pass the HTTP_PROXY
    host to getaddrinfo VERBATIM, so the bracketed IPv6 literal the operator
    injects ("http://[2001:db8::...]:3128", node-local squid via downward API) fails
    DNS with ENOTFOUND — glibc/python/curl accept it, Node does not. When the
    proxy host is an IPv6 literal, splice a loopback TCP forwarder in front of
    it and repoint the proxy env at 127.0.0.1 so every child process sees a
    resolvable proxy. No-op for hostname/IPv4 proxies or no proxy at all.

    The forwarder is byte-transparent and TIME-transparent:
    it never closes a connection because the peer went quiet (_bridge_connect)."""
    from urllib.parse import urlsplit
    url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    try:
        u = urlsplit(url)
        host, port = u.hostname, u.port or 3128
    except ValueError:
        return
    if not host or ":" not in host:
        return
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(64)
    lport = lsock.getsockname()[1]

    def _pump(a: socket.socket, b: socket.socket) -> None:
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                b.sendall(d)
        except OSError:
            pass
        finally:
            for sk in (a, b):
                try:
                    sk.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sk.close()
                except OSError:
                    pass

    def _accept() -> None:
        while True:
            try:
                c, _ = lsock.accept()
            except OSError:
                return
            try:
                up = _bridge_connect(host, port)
            except OSError:
                c.close()
                continue
            threading.Thread(target=_pump, args=(c, up), daemon=True).start()
            threading.Thread(target=_pump, args=(up, c), daemon=True).start()

    threading.Thread(target=_accept, daemon=True, name="proxy-bridge").start()
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if os.environ.get(var):
            os.environ[var] = f"http://127.0.0.1:{lport}"
    log.info("ipv6 proxy bridge: 127.0.0.1:%d -> [%s]:%d", lport, host, port)



def worktree_diff(project_dir: str) -> str:
    """Compute a harness worktree's staged diff IN-CONTAINER (G3c).

    The host must NEVER shell git into a harness-controlled worktree: the harness
    owns .git/config there, so an unhardened git honors attacker-set [diff] textconv
    / core.fsmonitor / core.hooksPath = code execution with the host's mounts. The
    sidecar computes the diff here instead — any poisoned config executes only inside
    this confined throwaway container. Hardened flags neutralize the config-driven
    exec vectors as defense-in-depth. Returns "" on failure (the run's exit_code
    carries the real outcome)."""
    import subprocess
    git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
           "-C", project_dir]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    try:
        # Init on demand so the diff works whether the host pre-init'd the worktree
        # (current same-pod model) or the sidecar got a fresh PVC (own-container).
        if subprocess.run([*git, "rev-parse", "--git-dir"],
                          capture_output=True, env=env).returncode != 0:
            subprocess.run([*git, "init", "-q"], capture_output=True, timeout=20, env=env)
        subprocess.run([*git, "add", "-A"], capture_output=True, timeout=60, env=env)
        r = subprocess.run([*git, "diff", "--cached", "--no-color"],
                           capture_output=True, text=True, timeout=60, env=env)
        return r.stdout
    except Exception as exc:
        log.warning("worktree_diff failed for %s: %s", project_dir, exc)
        return ""


def _safe_ws_path(name: str) -> str:
    """Resolve a workspace subdir, hard-bounded under WORKSPACE_ROOT. Rejects
    absolute paths, .. traversal, and anything that realpath()s outside the
    workspace — the `dir` param is dispatcher-supplied, so treat it as untrusted.

    The root itself is refused too. An empty `dir`, ".", or
    "x/.." used to resolve to WORKSPACE_ROOT, so one /workspace/load replaced
    every run's directory at once (and on a harness pod the vendor homes that
    live there, .kimi-home and .agy-home), and /workspace/export returned all
    of them. Every caller names one run's directory (review._pod_dir_name is
    never empty), so no legitimate request is the root."""
    base = os.path.realpath(WORKSPACE_ROOT)
    target = os.path.realpath(os.path.join(base, name or ""))
    if target == base:
        raise ValueError("a workspace dir is required (the workspace root itself "
                         "is never loaded or exported)")
    if not target.startswith(base + os.sep):
        raise ValueError(f"workspace path escapes {base}: {name!r}")
    return target


def _via_symlink(target: str, name: str) -> bool:
    """True when `name` reaches `target` through a symlink anywhere on the way.
    _safe_ws_path bounds the REALPATH under the workspace, so a link from one
    run's directory to another still passes it; comparing the realpath with
    the nominal join catches it."""
    nominal = os.path.normpath(os.path.join(os.path.realpath(WORKSPACE_ROOT), name or ""))
    return target != nominal


def _touches_git(name: str, linkname: str = "") -> bool:
    """True when a tar member's path, or a link member's target, has a `.git`
    component in any case. ONE rule for both ends of a clone-in: the uploader
    (server/routes/review.py _tar_member_touches_git, which _clone_in's
    _drop_git uses) drops exactly what this refuses, so a worktree holding a
    `.GIT` entry or a `hooks -> .git/hooks` link uploads cleanly instead of
    400ing. The kernel imports nothing from cli/, so that side is a hand
    mirror; scripts/test_sidecar_workspace_routes_harness_only.py runs both
    over the same corpus and fails when they disagree."""
    for field in (name, linkname):
        if any(p.lower() == ".git" for p in (field or "").replace("\\", "/").split("/")):
            return True
    return False


def _git_metadata_member(members) -> Optional[str]:
    """The first tar member that is, sits under, or links into a `.git`
    (any case), else None.

    A worktree upload never carries git metadata: the dispatcher drops every
    such member before it tars (review._clone_in, _drop_git, same rule), and
    the harness inits its own repo as its own uid (worktree_diff). So .git in
    an upload is never a real clone-in. It is how an uploader would plant
    .git/config (core.fsmonitor, core.hooksPath, a diff textconv) or
    .git/hooks, which run as whoever next uses git in that tree. Refused
    outright rather than stripped: the only legitimate sender never sends it,
    so seeing it at all is the signal."""
    for m in members:
        if _touches_git(m.name, m.linkname if (m.issym() or m.islnk()) else ""):
            return m.name
    return None


HandlerFn = Callable[..., object]


class _Request:
    def __init__(self, method: str, path: str, query: str, headers: dict,
                 body: bytes):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body

    def json(self) -> object:
        return json.loads(self.body) if self.body else None


class Sidecar:
    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], HandlerFn] = {}

    def handler(self, method: str, path: str):
        def deco(fn: HandlerFn) -> HandlerFn:
            self._routes[(method.upper(), path)] = fn
            return fn
        return deco

    def serve(self, *, port: int | None = None) -> None:
        _require_abs_socket_paths()
        if EGRESS_SOCK:
            _bridge_unix_egress()
        else:
            _bridge_ipv6_proxy()
        bind_port = port if port is not None else PORT
        # The control socket replaces the TCP port, so PORT is not needed.
        if bind_port == 0 and not CONTROL_SOCK:
            raise RuntimeError(
                "PORT env not set — refusing to bind ephemeral; "
                "kernel always passes a fixed port"
            )

        sidecar = self

        class _H(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:  # noqa
                log.info("[%s] %s", APP_ID, fmt % args)

            def _fail(self, code: int, message: str) -> None:
                """An error response that cannot destroy itself.

                BaseHTTPRequestHandler.send_error puts `message` in the HTTP
                REASON PHRASE, and http.server encodes the status line latin-1
                STRICT (send_response_only). So any non-ASCII character in a
                handler's error text raises UnicodeEncodeError WHILE WRITING
                THE STATUS LINE: nothing is sent, the connection closes with
                zero bytes, and the caller sees a bare connection drop instead
                of the error the pod was trying to hand it.

                Measured on a live Host's factory build. The
                harness raised an entirely correct message, "kimi has no model
                configured [em-dash] pick one in the Orchestrator Coding
                section", and that em-dash at position 42 of the status line
                blew up send_error. The kernel recorded
                ServerDisconnectedError('Server disconnected'), and a night
                went into chasing a network fault that never existed. It masked
                EVERY non-ASCII handler error, not just that one, and house
                style puts em-dashes in error text throughout.

                The reason phrase is left to the stdlib default (ASCII); the
                real message rides the JSON body, which is what every caller
                already reads.
                """
                self._write(({"ok": False, "error": str(message)}, code))

            def _dispatch(self) -> None:
                parsed = urlparse(self.path)

                # The control-plane wall. Checked BEFORE routing, so an
                # unauthorized caller learns nothing about which routes exist —
                # and before the body is read, so it cannot spend the pod's
                # memory on a tar upload it was never allowed to make.
                denial = _cap_denial(parsed.path,
                                     self.headers.get(CAP_HEADER))
                if denial:
                    log.warning(
                        "HARNESS_CAP_%s app=%s %s %s from=%s: %s",
                        "DENIED" if CAP_ENFORCE else "WOULD_DENY",
                        APP_ID, self.command, parsed.path,
                        self.client_address[0] if self.client_address else "?",
                        denial)
                    if CAP_ENFORCE:
                        self._fail(403, denial)
                        return

                key = (self.command, parsed.path)
                fn = sidecar._routes.get(key)
                if fn is None:
                    self._fail(404, f"no route for {self.command} {parsed.path}")
                    return

                # Same failure class as the encoder above, and it was sitting
                # OUTSIDE the try: a malformed Content-Length raises ValueError
                # here, before any byte is written, so the caller gets the
                # identical unattributable connection drop. Answer 400 instead.
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    self._fail(400, "bad Content-Length header")
                    return
                # A NEGATIVE length is worse than a malformed one: int("-5")
                # parses fine and rfile.read(-5) reads until EOF, so a client
                # that holds the connection open HANGS this worker thread
                # instead of dropping it. Bounded here, where the value enters.
                if length < 0:
                    self._fail(400, "negative Content-Length header")
                    return
                body = self.rfile.read(length) if length else b""
                req = _Request(
                    method=self.command,
                    path=parsed.path,
                    query=parsed.query,
                    headers={k: v for k, v in self.headers.items()},
                    body=body,
                )

                try:
                    sig = fn.__code__.co_argcount
                    result = fn(req) if sig else fn()
                except Exception as e:
                    log.exception("handler error: %s %s", self.command, parsed.path)
                    self._fail(500, str(e))
                    return

                self._write(result)

            def _write(self, result: object) -> None:
                if isinstance(result, tuple) and len(result) == 2:
                    payload, code = result
                else:
                    payload, code = result, 200

                # NDJSON streaming: a handler may return a
                # GENERATOR of dicts instead of one body. Each yielded dict is
                # written as a line and flushed immediately, so the caller sees
                # harness output while the run is still going. Chunked so no
                # Content-Length is needed; the run's final frame is just the
                # last line. A generator that raises mid-stream still closes the
                # response cleanly — the consumer sees a truncated stream, never
                # a hang.
                if hasattr(payload, "__next__") or hasattr(payload, "__iter__") and not isinstance(
                        payload, (dict, list, str, bytes)):
                    self.send_response(code)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    try:
                        for item in payload:
                            line = (json.dumps(item) + "\n").encode("utf-8")
                            self.wfile.write(b"%x\r\n" % len(line) + line + b"\r\n")
                            self.wfile.flush()
                    except Exception as e:      # noqa: BLE001 - never hang the client
                        log.exception("stream handler failed mid-run")
                        try:
                            err = (json.dumps({"kind": "error", "payload": {
                                "message": "stream failed: %s" % e}}) + "\n").encode()
                            self.wfile.write(b"%x\r\n" % len(err) + err + b"\r\n")
                        except OSError:
                            pass
                    try:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    except OSError:
                        pass
                    return

                if isinstance(payload, (dict, list)):
                    # Third site of the same class: a handler returning
                    # anything json can't serialize raised TypeError HERE,
                    # before a byte was written, and the caller saw the same
                    # unattributable drop. Degrade to a described 500 rather
                    # than vanish. (default=str is not used: silently
                    # stringifying part of a payload would hide the bug in the
                    # handler instead of reporting it.)
                    try:
                        body = json.dumps(payload).encode("utf-8")
                    except (TypeError, ValueError) as exc:
                        log.exception("handler returned a non-serializable payload")
                        body = json.dumps({
                            "ok": False,
                            "error": f"handler returned a non-serializable "
                                     f"payload: {exc}",
                        }).encode("utf-8")
                        code = 500
                    ctype = "application/json"
                elif isinstance(payload, str):
                    body = payload.encode("utf-8")
                    ctype = "text/html; charset=utf-8"
                elif isinstance(payload, bytes):
                    body = payload
                    ctype = "application/octet-stream"
                else:
                    body = repr(payload).encode("utf-8")
                    ctype = "text/plain"

                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self): self._dispatch()
            def do_POST(self): self._dispatch()
            def do_PUT(self): self._dispatch()
            def do_DELETE(self): self._dispatch()
            def do_PATCH(self): self._dispatch()
            def do_OPTIONS(self): self._dispatch()

        class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            # Entity pods are IPv6-only: when BIND_HOST is an IPv6 wildcard ("::"),
            # bind AF_INET6 with V6ONLY=0 so BOTH the kubelet health probe (pod's
            # IPv6 addr) and the dispatcher (IPv6 Service) can reach it. An IPv4
            # 0.0.0.0 bind is unreachable on an IPv6 pod (the /health probe then
            # fails and k8s crashloops the harness).
            address_family = socket.AF_INET6 if ":" in BIND_HOST else socket.AF_INET

            def server_bind(self):
                if self.address_family == socket.AF_INET6:
                    try:
                        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                    except OSError:
                        pass
                super().server_bind()

        class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        class _UnixH(_H):
            # client_address is '' for AF_UNIX; the stdlib's address_string
            # would index into it.
            def address_string(self) -> str:
                return "unix"

        if CONTROL_SOCK:
            # A socket file left by an earlier process would make bind fail
            # with EADDRINUSE. Anything else at the path (a directory) is not
            # ours to remove, and unlink raises on it: fail closed.
            try:
                os.unlink(CONTROL_SOCK)
            except FileNotFoundError:
                pass
            # Born 0600 rather than chmod'ed after: no window in which another
            # uid that can see the directory could connect.
            old_umask = os.umask(0o177)
            try:
                srv = _UnixServer(CONTROL_SOCK, _UnixH)
            finally:
                os.umask(old_umask)
            os.chmod(CONTROL_SOCK, 0o600)
            where = f"unix:{CONTROL_SOCK}"
            log.info("sidecar %s listening on %s", APP_ID, where)
        else:
            srv = _Server((BIND_HOST, bind_port), _H)
            where = f":{bind_port}"
            log.info("sidecar %s listening on %s:%d", APP_ID, BIND_HOST, bind_port)
        # Say the control-plane posture out loud at start. A silently un-capped
        # harness and a capped one look identical from the operator's chair
        # otherwise, and "is this actually protecting anything" must be readable
        # from the pod's first log line.
        if not CONTROL_CAP:
            log.warning("sidecar %s: NO control capability issued "
                        "(PRIVATAE_HARNESS_CAP and CSERIES_HARNESS_CAP unset) — "
                        "control plane is OPEN to "
                        "anything that can reach %s", APP_ID, where)
        else:
            log.info("sidecar %s: control plane capability-gated, enforce=%s",
                     APP_ID, CAP_ENFORCE)
        log.info("sidecar %s: harness workspace routes %s (CSERIES_SIDECAR_ROLE=%r)",
                 APP_ID, "served" if HARNESS_MODE else "absent", SIDECAR_ROLE)

        def _shutdown(*_) -> None:
            log.info("sidecar %s SIGTERM, shutting down", APP_ID)
            threading.Thread(target=srv.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)
        srv.serve_forever()


class AppData:
    """Per-app survivable config storage.

    Each app's data is namespaced by its own app_id (read off the
    incoming request's token). Reads + writes go through the kernel's
    /api/applications/<app_id>/config endpoints.

    Usage inside a handler:

        @sidecar.handler("GET", "/api/prefs")
        def get_prefs(request):
            data = AppData.from_request(request)
            return data.get("theme") or "dark"
    """

    def __init__(self, app_id: str, token: str | None) -> None:
        self.app_id = app_id
        self._client = KernelClient(token=token)

    @classmethod
    def from_request(cls, request: "_Request") -> "AppData":
        token = (
            request.headers.get("X-Application-Token")
            or request.headers.get("x-application-token")
        )
        return cls(app_id=APP_ID, token=token)

    def get(self, key: str, default: object = None) -> object:
        r = self._client.get(f"/api/applications/{self.app_id}/config/{key}")
        if isinstance(r, dict) and r.get("_http_error") == 404:
            return default
        if isinstance(r, dict) and "value" in r:
            return r["value"]
        return default

    def put(self, key: str, value: object) -> None:
        self._client.put(
            f"/api/applications/{self.app_id}/config/{key}",
            body=value,
        )

    def delete(self, key: str) -> None:
        self._client.delete(f"/api/applications/{self.app_id}/config/{key}")

    def list_all(self) -> dict:
        r = self._client.get(f"/api/applications/{self.app_id}/config")
        return r.get("config", {}) if isinstance(r, dict) else {}


class KernelClient:
    """Thin HTTP client for outbound calls from the sidecar back to the
    kernel's /api/*. Forwards an X-Application-Token if you pass one
    (you typically pass the token that arrived on the incoming request,
    so kernel-side permission enforcement still bites)."""

    def __init__(self, token: str | None = None, base_url: str | None = None) -> None:
        self.base_url = (base_url or KERNEL_URL).rstrip("/")
        self.token = token

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-Application-Token"] = self.token
        return h

    def get(self, path: str) -> object:
        return self._request("GET", path, body=None)

    def post(self, path: str, body: object) -> object:
        return self._request("POST", path, body=body)

    def put(self, path: str, body: object) -> object:
        return self._request("PUT", path, body=body)

    def delete(self, path: str) -> object:
        return self._request("DELETE", path, body=None)

    def _request(self, method: str, path: str, body: object) -> object:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, method=method, data=data, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                if r.headers.get("Content-Type", "").startswith("application/json"):
                    return json.loads(raw) if raw else None
                return raw
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return {"_http_error": e.code, "detail": json.loads(raw)}
            except Exception:
                return {"_http_error": e.code, "detail": raw.decode(errors="replace")}


# Module-level default instance so simple apps can just import + use.
sidecar = Sidecar()


def _harness_route(method: str, path: str):
    """Register one of HARNESS_ONLY_ROUTES on the shared `sidecar`, in a
    harness process only (HARNESS_MODE). Anywhere else the function is still
    defined, so it can be tested, but no request can reach it."""
    if (method.upper(), path) not in HARNESS_ONLY_ROUTES:
        raise ValueError(f"{method} {path} is not in HARNESS_ONLY_ROUTES; add it "
                         "there so the kernel proxy refuses it for in-pod apps too")

    def deco(fn: HandlerFn) -> HandlerFn:
        if HARNESS_MODE:
            sidecar.handler(method, path)(fn)
        return fn
    return deco


# ── G3c: clone-in / collect-out over the control API ────────────────────
# A harness sidecar (HARNESS_MODE) gets these routes from the shared
# `sidecar`. The own-pod harness has no shared filesystem with the host, so the
# dispatcher delivers the worktree (load) and collects the worktree-after
# (export) over HTTP. ?dir=<name> selects the subdir under WORKSPACE_ROOT.

def _remove_tree(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except OSError:
            pass


@_harness_route("POST", "/workspace/load")
def _workspace_load(request):
    """Clone-in: replace WORKSPACE_ROOT/<dir> with the tar in the request body.
    tarfile filter='data' refuses traversal, links out of the tree and device
    members, and strips a leading '/' so an absolute member lands inside.

    Extract, THEN swap. This used to rmtree the target before it
    had even opened the tar, so a body that was not a tar, or one that failed
    part-way through extraction, still deleted the directory; with an empty
    `dir` that was every project at once. Now the tar is checked and extracted
    into a staging directory beside the target, and only a complete extract is
    renamed into place. Anything that fails leaves the target exactly as it
    was. An empty tar still empties the directory (review._drop_pod_dir)."""
    import io
    import tarfile
    import uuid
    from urllib.parse import parse_qs
    name = (parse_qs(request.query).get("dir", [""])[0]) or ""
    try:
        dest = _safe_ws_path(name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}, 400
    if _via_symlink(dest, name):
        return {"ok": False, "error": "workspace dir is reached through a symlink"}, 400
    body = request.body or b""
    if not body:
        return {"ok": False, "error": "empty body (expected a tar)"}, 400

    parent, leaf = os.path.dirname(dest), os.path.basename(dest)
    staging = os.path.join(parent, f".{leaf}.load-{uuid.uuid4().hex}")
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as tf:
            members = tf.getmembers()
            git_member = _git_metadata_member(members)
            if git_member is not None:
                return {"ok": False, "error": (
                    f"tar carries git metadata ({git_member!r}); a worktree upload "
                    "never includes .git")}, 400
            os.makedirs(parent, exist_ok=True)
            os.mkdir(staging)
            tf.extractall(staging, members=members, filter="data")
    except Exception as exc:
        _remove_tree(staging)
        log.warning("workspace/load extract failed for %s: %s", dest, exc)
        return {"ok": False, "error": f"extract failed: {exc}"}, 400

    old = None
    try:
        if os.path.lexists(dest):
            old = os.path.join(parent, f".{leaf}.old-{uuid.uuid4().hex}")
            os.rename(dest, old)
        os.rename(staging, dest)
    except OSError as exc:
        if old is not None and not os.path.lexists(dest):
            try:
                os.rename(old, dest)
                old = None
            except OSError:
                log.error("workspace/load: could not put %s back from %s", dest, old)
        _remove_tree(staging)
        log.warning("workspace/load swap failed for %s: %s", dest, exc)
        return {"ok": False, "error": f"could not replace the workspace dir: {exc}"}, 500
    if old is not None:
        _remove_tree(old)
    n = sum(len(files) for _, _, files in os.walk(dest))
    log.info("workspace/load: extracted %d files into %s", n, dest)
    return {"ok": True, "path": dest, "files": n}


@_harness_route("GET", "/workspace/export")
def _workspace_export(request):
    """Collect-out: return a tar of WORKSPACE_ROOT/<dir> (the worktree-after).
    EXCLUDES .git so the harness's repo config (attacker-controllable [diff]
    textconv / hooks) never lands on the host — the host applies picks via the
    already-hardened propose_commit file-copy, never by trusting this .git."""
    import io
    import tarfile
    from urllib.parse import parse_qs
    name = (parse_qs(request.query).get("dir", [""])[0]) or ""
    try:
        src = _safe_ws_path(name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}, 400
    # The requested dir must be a REAL directory on the path asked for, not
    # reached through a symlink: _safe_ws_path bounds the realpath under the
    # workspace, so a `scratch` swapped for a symlink to its parent would
    # still pass and tar the vendor's whole CLI dir — token file included.
    # Compare the realpath with the nominal join; any link on the way differs.
    nominal = os.path.normpath(os.path.join(os.path.realpath(WORKSPACE_ROOT), name or ""))
    if src != nominal:
        return {"ok": False, "error": "workspace dir is reached through a symlink"}, 400
    if not os.path.isdir(src):
        return {"ok": False, "error": "no such workspace dir"}, 404

    def _drop_git(ti):
        return None if ".git" in ti.name.split("/") else ti

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        tf.add(src, arcname=".", filter=_drop_git)
    return buf.getvalue()


# ── Workbench P3: native session-state collection ────────────────────────
# The harness's OWN session files (~/.claude sessions, codex session json, …)
# live on the pod's emptyDir and die at scale-to-0 — resume/audit need them
# collected before release. Dirs come from CSERIES_SESSION_DIRS (colon-sep, set
# per harness image), NEVER from the request: this endpoint exports exactly
# the configured state and nothing else. Credential files are dropped by NAME
# so an OAuth token / API key can never ride along to the host store.
SESSION_EXPORT_DIRS = [p for p in os.environ.get(
    "CSERIES_SESSION_DIRS",
    "/home/privatae/.claude:/home/privatae/.claude.json").split(":") if p]
# This set MUST cover every filename in server/routes/review.py
# _HARNESS_CRED_FILES and _OPAQUE_CRED_FILES and server/network/harness_keys.py
# _PRIMARY_CREDS — the sidecar runs in the pod and cannot import any of them,
# so the mirror is by hand and tests/test_session_export_denies_every_cred_file.py
# holds it in step. A name missing here, such as `provider.json` (kimi) or
# `antigravity-oauth-token`, is unreachable only while no image exports the
# directory that file lives in, which is one CSERIES_SESSION_DIRS edit away
# from a credential on the host's session PVC.
_SESSION_EXPORT_DENY = {".credentials.json", "auth.json", "credentials.json",
                        "api_key", "provider.json", "antigravity-oauth-token",
                        "id_rsa", "id_ed25519",
                        # Bare token shapes no vendor writes today; named so a
                        # future one is caught by containment, not by luck.
                        "credentials", "access_token", "refresh_token"}
# Directory components that hold per-provider grants. kimi keeps
# `credentials/<id>.json` today; the rest are the names a vendor would pick
# if it split one auth file into many (opencode's exported dir IS its own
# data dir, and `auth.json` → `auth/<provider>.json` is the obvious move).
_SESSION_EXPORT_DENY_DIRS = {"credentials", "creds", "auth", "keys", "secrets", "tokens"}


def _is_cred_path(arc_name: str) -> bool:
    """True when a tar member must NOT leave the pod. Deliberately WIDER than
    an exact basename match: the runtimes write credentials as
    write-tmp-then-replace, which produces DERIVED names — `.credentials.json.tmp`,
    `auth.json.tmp`, `antigravity-oauth-token.tmp`, or the
    `.credentials.credentials.json.tmp` a `with_suffix` slip in claude_creds.py once produced
    — and a rename that fails or a pod killed mid-write leaves that file in
    the one directory claude-code exports. So: drop when the basename
    CONTAINS a denied name (case-insensitively), ends in a key suffix, or sits
    under a credentials directory (exact component match — Claude Code's
    project slugs are `-root-x` shaped and must not trip it). Over-dropping
    costs a resume one non-credential file; under-dropping ships a token to
    the host store. Pure and import-free on purpose — the tests exec it
    straight out of this source, and one asserts _session_export calls it."""
    parts = [p for p in arc_name.replace("\\", "/").split("/") if p and p != "."]
    if not parts:
        return False
    base = parts[-1].lower()
    if base.endswith((".pem", ".key")):
        return True
    if any(name in base for name in _SESSION_EXPORT_DENY):
        return True
    return any(p.lower() in _SESSION_EXPORT_DENY_DIRS for p in parts[:-1])


@_harness_route("GET", "/session/export")
def _session_export(request):
    """Tar the harness's native session dirs (CSERIES_SESSION_DIRS). 404 when
    none exist yet — the dispatcher reads that as 'nothing to collect'."""
    import io
    import tarfile

    def _drop_creds(ti):
        return None if _is_cred_path(ti.name) else ti

    found = [d for d in SESSION_EXPORT_DIRS if os.path.exists(d)]
    if not found:
        return {"ok": False, "error": "no native session state present"}, 404
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for d in found:
            tf.add(d, arcname=os.path.basename(d.rstrip("/")), filter=_drop_creds)
    return buf.getvalue()


# ── The Ladder Tier-1 probe: live run-behavior counters ──────────────────
# The host polls this ~30s mid-run to
# catch stall/loop/flail before the wall cap instead of billing a dead pod's
# full window. Stat-only by design: sizes + mtimes of the SAME session dirs
# /session/export tars, plus a workspace change signature — no file content
# is read, so nothing (credentials included) can ride along. The host reads
# a 404 as "old image → Tier-0 only" (the /session/export tolerance pattern).

def _dir_signature(root: str) -> dict:
    files, size, newest = 0, 0, 0.0
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git"]
        for n in names:
            try:
                st = os.stat(os.path.join(base, n))
            except OSError:
                continue
            files += 1
            size += st.st_size
            newest = max(newest, st.st_mtime)
    return {"files": files, "bytes": size, "newest_mtime": newest}


@_harness_route("GET", "/run/progress")
def _run_progress(request):
    """Deterministic liveness counters for the run watchdog: the harness CLI
    appends session events as it works, so session-file growth + age is the
    'still thinking vs. stalled' signal, and the workspace signature is the
    'churning without progress' (flail) signal. ?dir=<name> scopes the
    workspace part to one run's subdir (dispatcher-supplied → untrusted →
    hard-bounded by _safe_ws_path, the /workspace/export rule)."""
    from urllib.parse import parse_qs
    now = time.time()
    sess = {"files": 0, "bytes": 0, "newest_mtime": 0.0}
    for d in SESSION_EXPORT_DIRS:
        if os.path.isfile(d):
            try:
                st = os.stat(d)
                sess["files"] += 1
                sess["bytes"] += st.st_size
                sess["newest_mtime"] = max(sess["newest_mtime"], st.st_mtime)
            except OSError:
                pass
        elif os.path.isdir(d):
            sig = _dir_signature(d)
            sess["files"] += sig["files"]
            sess["bytes"] += sig["bytes"]
            sess["newest_mtime"] = max(sess["newest_mtime"], sig["newest_mtime"])
    out = {
        "ok": True,
        "session_files": sess["files"],
        "session_bytes": sess["bytes"],
        "last_event_age_s": round(now - sess["newest_mtime"], 1) if sess["newest_mtime"] else None,
    }
    name = (parse_qs(request.query).get("dir", [""])[0]) or ""
    if name:
        try:
            ws = _safe_ws_path(name)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, 400
        if os.path.isdir(ws):
            sig = _dir_signature(ws)
            # One comparable token: the flail detector only needs "changed or
            # not" between two probes, never the contents.
            out["workspace_diffstat"] = f"{sig['files']}:{sig['bytes']}:{sig['newest_mtime']:.0f}"
    return out
