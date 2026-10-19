"""
CEIGAS Bridge Client — persistent mTLS connection to the governance bridge.

This is the container's phone line to the Privatae platform. It:
  - Connects to BRIDGE_HOST:BRIDGE_PORT with the entity's CEIGAS cert
  - Sends heartbeat every 30s to stay alive
  - Provides request/response for any bridge operation (outbound)
  - Receives device.dispatch from the bridge and routes to handlers (inbound)
  - Reconnects automatically on disconnect

Protocol: JSON-line over mTLS.
  Outbound:   {"op": "heartbeat.ping", "params": {}}\\n
  Response:   {"ok": true, "data": {...}}\\n
  Inbound:    {"op": "device.dispatch", "params": {action, correlation_id, payload}}\\n
  Reply:      {"op": "device.respond", "params": {correlation_id, result}}\\n
"""

import asyncio
import contextlib
import inspect
import json
import logging
import os
import random
import socket
import ssl
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("privatae.pc.bridge_client")

CERT_DIR = Path(os.environ.get("CEIGAS_CERT_DIR", "/etc/privatae/certs"))
# No default: with BRIDGE_HOST unset the client never dials anything.
BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "").strip()
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8643"))
# The name the cloud pod sends as SNI; its hostname is not checked.
CLOUD_SERVER_HOSTNAME = "bridge.services.ceigas.internal"

HEARTBEAT_INTERVAL = 30
# Consecutive failed heartbeats before the connection is declared dead and
# the reconnect loop takes over (a bridge-roll outage: a black-holed
# socket — no RST/FIN, e.g. a hostNetwork bridge pod replaced on the same
# node — made every ping time out FOREVER while _connected stayed True; the
# reconnect loop only runs when _heartbeat_loop exits, so the pod sat
# connected-but-dead for ~5h until a manual restart). 2 misses ≈ 2 minutes
# worst case (timeout + interval per miss).
HEARTBEAT_MISS_LIMIT = 2
RECONNECT_BASE = 3
RECONNECT_MAX = 60

# Per-ADDRESS connect budget — see _candidate_addrs() for why this exists.
CONNECT_TIMEOUT = 5
REQUEST_TIMEOUT = 30

# Long-running ops use a wider timeout. These ops block the request lock
# for their duration, so add new entries deliberately — every entry here
# serializes other bridge calls behind itself for up to LONG_REQUEST_TIMEOUT.
LONG_REQUEST_TIMEOUT = 660
LONG_OPS = {
    "docker.build", "docker.push",
    # Inference legitimately takes >30s on larger models (Sonnet ~30-60s,
    # reasoning models more). Without bumping it into LONG_OPS the pod side
    # times out before the bridge gets a chance to return — and the surface
    # that called it ends up reporting "no data" while the bridge's LLM call
    # quietly continues to completion off-screen.
    "inference.execute",
    # harness.wake blocks on the operator scaling 0->1 AND the pod pulling its
    # image (pullPolicy Always on floating :dev tags) + creds-init + Ready
    # probe: 35-60s cold is NORMAL; the operator itself Ready-polls for 60s.
    # At the default 30s every cold wake returned {"error":"timeout"} while
    # the pod came up fine seconds later (seen on every fresh-image wake).
    "harness.wake",
    # pc.update_apply does a real rolling update of the pod the caller
    # is INSIDE — the operator patches the CR, the pod kills + recreates,
    # health check has to pass. First-time updates routinely take 90-180s.
    # The 30s timeout was returning {"detail":"timeout"} long before the
    # operator could finish, surfacing as a 502 even when the update itself
    # was succeeding.
    "pc.update_apply",
    # A bundle publish: a staged one waits up to 600 s at the bridge while the
    # controller assembles, checks and stores it, and a 16 MiB part can
    # outlast 30 s on a slow link.
    "webapp.publish_bundle", "appstore.publish_bundle", "bundle.upload_part",
}

# ── Bundles past one bridge frame ────────────────────────────────────────
# The bridge reads each request line with a 64 MiB limit (bridge_server.py
# STREAM_LIMIT), so a zip sent in-band as base64 tops out near 47 MiB, and the
# platform refused anything over 50 MB anyway. A bundle over BUNDLE_IN_BAND_MAX
# is now sent in parts (bundle.upload_part, BUNDLE_PART_BYTES each) into the
# platform's object storage, and the publish names the upload instead of
# carrying the bytes. Every caller of a publish op gets this through
# request_full; a bundle that fits is sent exactly as before. The only size
# bound left is the platform's zip-bomb guard on what the bundle UNPACKS to.
BUNDLE_OPS = frozenset({"webapp.publish_bundle", "appstore.publish_bundle"})
BUNDLE_IN_BAND_MAX = 32 * 1024 * 1024
BUNDLE_PART_BYTES = 16 * 1024 * 1024


async def stage_bundle_params(send, params: dict) -> dict:
    """`params` with a big `zip_b64` swapped for a staged upload: each part
    sent with `send("bundle.upload_part", ...)`, then upload_id + parts +
    sha256 in place of the bytes. A bundle that fits in-band is returned
    unchanged. Returns {"ok": False, "error": ...} when a part is refused."""
    b64 = params.get("zip_b64") if isinstance(params, dict) else None
    if not isinstance(b64, str) or len(b64) * 3 // 4 <= BUNDLE_IN_BAND_MAX:
        return params
    import base64
    import hashlib
    import secrets
    try:
        raw = base64.b64decode(b64)
    except Exception as e:  # noqa: BLE001 — the bridge would refuse it the same way
        return {"ok": False, "error": f"zip_b64 not base64: {e}"}
    upload_id = secrets.token_hex(16)
    parts = 0
    for seq, off in enumerate(range(0, len(raw), BUNDLE_PART_BYTES)):
        env = await send("bundle.upload_part", {
            "upload_id": upload_id, "seq": seq,
            "data_b64": base64.b64encode(raw[off:off + BUNDLE_PART_BYTES]).decode("ascii")})
        if not env.get("ok"):
            return {"ok": False, "error_kind": env.get("error_kind"),
                    "error": (f"part {seq + 1} of the {len(raw)}-byte bundle was not staged: "
                              f"{env.get('error')}")}
        parts = seq + 1
    out = {k: v for k, v in params.items() if k != "zip_b64"}
    out.update(upload_id=upload_id, parts=parts, sha256=hashlib.sha256(raw).hexdigest())
    return out

# ── Circuit breaker for wedged data-plane ops ────────────────────────
# A failing backend behind a bridge op (e.g. a dead embedder) must NOT be able
# to hold the single request lock for its full timeout, attempt after attempt,
# and starve control ops (heartbeat, circles, contracts) on the shared
# connection. After CIRCUIT_FAIL_THRESHOLD consecutive failures on a breakable
# op, its circuit OPENS: callers fast-fail WITHOUT acquiring the lock or
# sending, for CIRCUIT_COOLDOWN seconds, after which exactly ONE half-open
# probe is allowed. A success closes it; a failed probe re-arms the cooldown.
# Scope is deliberately narrow — only ops whose backend can be down for a long
# time independent of the bridge itself. embeddings.embed is the one that bit
# us: a CPU-Ollama outage stormed the bridge and timed out even heartbeats.
BREAKABLE_OPS: frozenset[str] = frozenset({"embeddings.embed"})
CIRCUIT_FAIL_THRESHOLD = 3
CIRCUIT_COOLDOWN = 60.0

# ── harness.release across an operator roll ──────────────────────────────
# MEASURED (4-harness fan-out, from the cluster's Kubernetes events, not
# inferred): the operator Deployment is `strategy: Recreate`, `replicas: 1`
# (a singleton — kill old before new, never 2 reconcilers). Its roll scaled
# the old ReplicaSet 1->0 at 01:09:11, the new one 0->1 at 01:09:41, the
# container Started at 01:09:43, and the release for antigravity landed at
# 01:09:44 into a Service with NO endpoints: "operator unreachable: Cannot
# connect to host <operator address> [No route to host]". The run had
# SUCCEEDED; only the release failed. The pod then sat on metered compute
# until the operator's harness_idle_sweep reclaimed it 15+ minutes later, and
# only 112.4 s of that was billed.
#
# So EVERY operator roll has a guaranteed window with zero endpoints: 30-32 s
# from scale-down to container start, plus the readiness probe before the
# endpoint is published (the operator's readiness probe: initialDelaySeconds 5,
# periodSeconds 5), i.e. ~40-45 s end to end. Any release from any lane that
# lands in it fails the same way; antigravity was just the one that did.
#
# The fix is in the CALLER, not the operator: Recreate is deliberate for a
# kopf singleton (two live reconcilers is a correctness problem), so the
# window cannot be removed — a release has to survive it. Retrying a release
# is safe because the operator's release is idempotent by design and by
# code (the operator's harness release handler): it re-reads
# the metering annotations — which release never clears — and re-patches
# `replicas: 0`, tolerating 404. A retry after a response that was PROCESSED
# but lost therefore returns the same ready_duration_s, so billing lands
# once and lands right.
#
# Policy: exponential backoff with equal jitter (delay drawn from [d/2, d],
# d doubling from 1 s to a 15 s cap), under a 90 s wall-clock budget from
# the first attempt = twice the measured window. The d/2 floor is what makes
# the attempt count bounded when the operator fast-fails ("No route to host"
# returns in milliseconds): at most ~16 attempts fit in 90 s. A SYN blackhole
# instead costs one REQUEST_TIMEOUT (30 s) per attempt and burns the budget
# in 2-3 attempts. The bound is the deadline PLUS one in-flight attempt: the
# deadline is checked before each sleep, never mid-request, so the last
# attempt may start with seconds of budget left and run its full
# REQUEST_TIMEOUT — worst case 90 s + 30 s. That is deliberate: cutting the
# final attempt at `remaining - REQUEST_TIMEOUT` would give up 30 s early in
# the common fast-fail case and lose a third of the margin over the measured
# window, while the extra 30 s costs nothing to lanes that are already
# minutes long (inline) or off the request path (detached).
# Retries fire ONLY on transport-class failures (see release_error_is_transport);
# an answer the operator actually gave — a 400 for an unknown harness, a
# 404 for an unprovisioned one — still fails fast.
#
# A retry is ABANDONED when the pod is woken again while it waits: lane A's release fails in the
# roll window; the operator returns; lane B wakes the same harness — WARM, because replicas is
# still 1, so the operator reuses the URL and the control cap (the operator's harness wake) — and
# A's next attempt would patch replicas:0 under B and then drop B's cap as a "landed" release.
# Before the retry existed the same inputs left B running; a retry made the kill systematic on
# every operator roll. So every wake sender bumps a per-key generation on success, the retry
# snapshots it when it starts, and checks it before EACH further attempt. Keyed by _release_key
# so a wake on a different harness (or the app_rt lane with the same id) cannot abandon it.
# The window this cannot close: a wake that is serialized at the operator
# BEHIND an attempt already in flight — that is the same ordering race a
# single un-retried release always had, and only the operator could fence it.
RELEASE_RETRY_BUDGET_S = 90.0
RELEASE_RETRY_BASE_S = 1.0
RELEASE_RETRY_CAP_S = 15.0
RELEASE_RETRY_MAX_ATTEMPTS = 16

# _release_key -> number of successful harness.wake answers seen for that pod.
# Written by note_harness_wake from every wake sender (review._harness_bridge,
# harness_backend._wake / _wake_app); read by request_release_retrying.
_wake_generation: dict[str, int] = {}

# Detached retries (an HTTP handler must not sit out the window) are keyed by
# the pod they release, so a second failure for the same pod — a polled login
# route, a re-install after a stop — joins the retry already running rather
# than stacking a new one. Strong refs: an unreferenced Task can be collected
# mid-sleep (the same GC hazard test_status_honesty pins for ladder climbs).
_detached_release_tasks: dict[str, "asyncio.Task"] = {}


def release_error_is_transport(env: dict) -> bool:
    """Is a failed harness.release envelope a TRANSPORT failure — the request
    never reached the operator, or its answer never came back — as opposed to
    an answer the operator gave? Only the former is worth retrying.

    The shapes, each cited to where it is produced:
      * error_kind == "infra": this client's own pod<->bridge transport —
        "not connected" (request_full, above), the REQUEST_TIMEOUT "timeout",
        and a send that raised.
      * "operator unreachable: ...": the bridge could not complete its HTTP
        call to the operator (privatae bridge/adapter.py:8124-8125 — every
        aiohttp exception, connect refused / no route / its 120 s total).
      * "Timeout": the bridge server's own read timeout (bridge_server.py:590,
        code 408).
    An operator answer comes back as "operator /harness/release NNN: ..."
    (adapter.py:8121-8122) with the operator's status — never matched here."""
    if env.get("ok"):
        return False
    if env.get("error_kind") == "infra":
        return True
    err = str(env.get("error") or "")
    if err.startswith("operator unreachable"):
        return True
    return err in ("timeout", "Timeout", "not connected")


def _release_key(params: dict) -> str:
    p = params or {}
    return f"harness:{p.get('harness')}" if p.get("harness") else f"app_rt:{p.get('app_rt')}"


def note_harness_wake(params: dict) -> int:
    """A harness.wake for `params` ({harness: id} or {app_rt: id} — the same
    body the wake was sent with) was ANSWERED ok. Bumps that pod's wake
    generation so any release retry waiting on the same key abandons itself
    before its next attempt (see the note above). Call it from every
    sender of a successful wake, warm or cold — a keepalive re-stamp is a
    lane saying it still holds the pod, which is exactly what a pending
    release must yield to. Returns the new generation."""
    key = _release_key(params)
    gen = _wake_generation.get(key, 0) + 1
    _wake_generation[key] = gen
    return gen


async def request_release_retrying(bc, params: dict, *,
                                   budget_s: float = RELEASE_RETRY_BUDGET_S,
                                   detach: bool = False,
                                   on_result: Optional[Callable[[dict], Awaitable[None]]] = None,
                                   ) -> dict:
    """Send `harness.release` through `bc` (a BridgeClient or any object with
    its `request_full`), retrying transport failures under the policy above.
    Returns the final envelope in the same shape as request_full.

    detach=False (default — build, fan-out, catalog, idle-timer lanes): the
    whole retry runs inline and the returned envelope is the last word, with
    ready_duration_s ready for billing. Those lanes are already minutes long
    and their release sits in a shielded finally, so waiting the budget there
    is the right trade.

    detach=True (HTTP-handler lanes — login routes, a user's stop/re-install):
    ONE attempt runs inline. If it fails on transport, the remaining budget
    runs in a background Task, this returns the failed envelope with
    `retrying: True`, and `on_result(final_env)` is awaited when the Task
    finishes — that is where the caller bills or forgets a capability. A
    caller must therefore act on a returned envelope only when it is ok or
    when `retrying` is absent; on_result covers the detached case exactly
    once. A detached retry already running for the same pod is joined, not
    duplicated (the key is the pod, see _release_key). A JOINED caller's
    on_result is never called — only the on_result of the caller that started
    the task fires, so a joiner passing a different on_result must not rely
    on it; the joiner's envelope says `retrying: True` and nothing more.

    ABANDONED: if a harness.wake for the same key lands (note_harness_wake)
    while the retry is between attempts, no further attempt is sent and the
    result is {ok: False, abandoned: True, ...} — not a landed release, so
    nothing is billed and no cap is dropped. In detached mode on_result is
    NOT called for an abandoned retry: the pod now belongs to the lane that
    woke it, and that lane's own release will bill the whole metered window
    (the operator's warm wake does not restart the meter)."""
    key = _release_key(params)
    gen0 = _wake_generation.get(key, 0)
    started = time.monotonic()
    deadline = started + max(0.0, float(budget_s))

    async def _loop(first_env: Optional[dict], attempt: int) -> dict:
        env = first_env
        while True:
            if env is None:
                gen = _wake_generation.get(key, 0)
                if gen != gen0:
                    logger.info(
                        "harness.release %s: abandoned before attempt %d — the pod was "
                        "woken again (wake generation %d -> %d); it belongs to that lane now",
                        key, attempt + 1, gen0, gen)
                    return {"ok": False, "abandoned": True, "release_attempts": attempt,
                            "error": "release abandoned: the harness was woken again "
                                     "by another lane while the retry waited"}
                env = await bc.request_full("harness.release", params)
                attempt += 1
            if not release_error_is_transport(env):
                return env
            remaining = deadline - time.monotonic()
            if remaining <= 0 or attempt >= RELEASE_RETRY_MAX_ATTEMPTS:
                logger.warning(
                    "harness.release %s: giving up after %d attempt(s) over %.0fs — %s",
                    key, attempt, time.monotonic() - started, env.get("error"))
                return {**env, "release_attempts": attempt}
            d = min(RELEASE_RETRY_CAP_S, RELEASE_RETRY_BASE_S * (2 ** (attempt - 1)))
            delay = min(random.uniform(d / 2, d), remaining)
            logger.info(
                "harness.release %s: transport failure on attempt %d (%s) — "
                "retrying in %.1fs, %.0fs of budget left",
                key, attempt, env.get("error"), delay, remaining)
            await asyncio.sleep(delay)
            env = None

    if not detach:
        return await _loop(None, 0)

    first = await bc.request_full("harness.release", params)
    if not release_error_is_transport(first):
        return first
    live = _detached_release_tasks.get(key)
    if live is not None and not live.done():
        logger.info("harness.release %s: a detached retry is already running — joined", key)
        return {**first, "retrying": True}

    async def _detached() -> None:
        try:
            final = await _loop(first, 1)
        except Exception as exc:  # noqa: BLE001 — a retry must never raise into nowhere
            logger.warning("harness.release %s: detached retry raised: %r", key, exc)
            final = {"ok": False, "error": f"detached retry raised: {exc!r}"}
        if final.get("abandoned"):
            return  # nothing landed: no bill, no cap to drop — see the docstring
        if on_result is not None:
            try:
                await on_result(final)
            except Exception as exc:  # noqa: BLE001
                logger.warning("harness.release %s: on_result raised: %r", key, exc)

    task = asyncio.get_running_loop().create_task(_detached())
    _detached_release_tasks[key] = task
    task.add_done_callback(lambda t: (_detached_release_tasks.get(key) is t
                                      and _detached_release_tasks.pop(key, None)))
    return {**first, "retrying": True}


def _heartbeat_params() -> dict:
    """What every heartbeat.ping carries besides liveness: which WireGuard key
    this Host's Hub runs (hub_identity.report_state), so the platform can tell
    which Hosts still serve the operator-made key without exec-ing into them.
    Never raises; a process with no Hub support sends {} and the bridge treats
    the ping exactly as before.

    And, only when a harness run since the last beat was refused by its vendor,
    `harness_refusals`: the content-free records server/harness_refusals.py
    keeps ({harness, cli_version, min_version_named, hour}, each pair at most
    once an hour). take_due does I/O only when it has a record to send."""
    params: dict = {}
    try:
        from server.network import hub_identity
        params["hub_key"] = hub_identity.report_state()
    except Exception:
        pass
    try:
        from server import harness_refusals
        due = harness_refusals.take_due()
        if due:
            params["harness_refusals"] = due
    except Exception:
        pass
    return params


def _build_ssl_context(cert=None, key=None, ca=None,
                       verify_hostname: bool = False) -> Optional[ssl.SSLContext]:
    """The client context. With no arguments: the cloud pod's relay cert and
    the CEIGAS CA from CERT_DIR, hostname not checked. A network member passes
    its passport files and verify_hostname=True (server/bridge/member_client.py)."""
    cert = Path(cert) if cert is not None else CERT_DIR / "relay.crt"
    key = Path(key) if key is not None else CERT_DIR / "relay.key"
    ca = Path(ca) if ca is not None else CERT_DIR / "ceigas_ca.crt"

    if not cert.exists() or not key.exists():
        logger.info("No CEIGAS certs — bridge client disabled")
        return None

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_cert_chain(str(cert), str(key))
    if ca.exists():
        ctx.load_verify_locations(str(ca))
    if not verify_hostname:
        ctx.check_hostname = False
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


# Handler signature: async fn(payload: dict) -> dict | None
DeviceHandler = Callable[[dict], Awaitable[Any]]


# Governance-affecting inbound actions. These write rows that the live chat
# path TRUSTS as governance state:
#   behavioral.append   -> behavioral_corrections (surfaces into the SYSTEM
#                          PROMPT as a durable correction)
# (The trust-score upsert went with the trust score: the platform
# does not quarantine a Host from outside; the kill switch is the stop.)
# An inbound dispatch reaches the pod ONLY over the mTLS-authenticated bridge
# reader loop, but `_handle_dispatch` previously ran ANY registered handler
# purely on action-name match, with no per-op distinction between a benign
# device action (mouse_move) and a governance write. This set makes the
# governance-affecting actions EXPLICIT so:
#   (a) a governance write is refused unless it arrives over the live
#       authenticated bridge connection (fail closed, not fail open), and
#   (b) every governance dispatch is audit-attested before the handler runs.
# Adding a new governance-affecting handler MUST add its action here (default
# is "not governance" — benign device actions are unaffected and unblocked).
GOVERNANCE_ACTIONS: frozenset[str] = frozenset({
    "behavioral.append",
})


class BridgeUnavailable(RuntimeError):
    """Typed pod↔bridge TRANSPORT failure (envelope error_kind == "infra":
    not-connected / timeout / circuit-open / send raise) — as opposed to an
    op-level error or a genuinely empty result. Raised by request() only
    when the caller opts in via raise_on_infra=True, so bridge-down reads
    can fail honestly instead of laundering into success-shaped emptiness
    (the "hallucinated tool calls" class)."""

    def __init__(self, op: str, error: str):
        super().__init__(f"bridge unavailable on {op}: {error}")
        self.op = op
        self.error = error


class BridgeClient:
    """Persistent mTLS connection to the CEIGAS governance bridge.

    Bidirectional: outbound requests via `request()`, inbound dispatches
    via registered handlers. A single dedicated reader task demultiplexes
    response shapes from inbound `op` shapes.
    """

    # Where and how to connect. The defaults are the cloud pod's: the bridge
    # name and port from the environment, the relay cert from CERT_DIR, and
    # the bridge's service name sent as SNI without a hostname check. Class
    # attributes as well as instance ones, so a subclass that skips __init__
    # (the heartbeat tests do) still reads the cloud values.
    _host: str = BRIDGE_HOST
    _port: int = BRIDGE_PORT
    _cert: Optional[Path] = None
    _key: Optional[Path] = None
    _ca: Optional[Path] = None
    _server_hostname: str = CLOUD_SERVER_HOSTNAME
    _verify_hostname: bool = False
    # Dial this node's own address first (HOST_IP, see _candidate_addrs). Only
    # meaningful inside the cluster.
    _prefer_host_ip: bool = True

    def __init__(self, *, host: Optional[str] = None, port: Optional[int] = None,
                 cert=None, key=None, ca=None,
                 server_hostname: str = CLOUD_SERVER_HOSTNAME,
                 verify_hostname: bool = False):
        self._host = BRIDGE_HOST if host is None else host
        self._port = BRIDGE_PORT if port is None else int(port)
        self._cert = cert
        self._key = key
        self._ca = ca
        self._server_hostname = server_hostname
        self._verify_hostname = verify_hostname
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._running = False
        self._connected = False
        self._reconnect_delay = RECONNECT_BASE
        self._writer_lock = asyncio.Lock()
        # Multiplexing: each request carries a monotonic "id"; the
        # server echoes it, so the reader matches responses to waiters by id
        # instead of FIFO order. This lets concurrent requests ride one
        # connection without the end-to-end serialization the lock imposed.
        #   _pending: id -> Future awaiting that response
        #   _next_id: monotonic id source
        self._pending: dict = {}
        self._next_id = 0
        # Streaming: a streaming request registers a per-id queue
        # here in ADDITION to its _pending future. Chunk frames (which carry the
        # id + a "chunk", no "ok") are routed into this queue by the reader and
        # drained in-order by request_stream; the terminal "ok" frame resolves
        # the future (same as request_full) so billing + the tool-loop read the
        # full accumulated result, not the chunks.
        #   _streams: id -> asyncio.Queue of text deltas (None = end sentinel)
        self._streams: dict = {}
        # Retained for the LEGACY FIFO fallback only (talking to an OLD server
        # that doesn't echo ids — responses arrive with no "id"). New-server
        # responses never touch the queue or this lock.
        self._request_lock = asyncio.Lock()
        self._response_queue: asyncio.Queue = asyncio.Queue()
        self._reader_task: Optional[asyncio.Task] = None
        self._handlers: dict[str, DeviceHandler] = {}
        self._last_heartbeat = 0.0
        self._connect_time = 0.0
        # Cross-talk guard (B): count of in-flight requests we ABANDONED on
        # timeout. The bridge still answers them, in order, so the reader must
        # DISCARD that many late responses instead of handing one to the next
        # caller (the "circles.list got the embed's error response" bug).
        self._pending_orphans = 0
        # Circuit-breaker state (A), keyed by op.
        self._op_failures: dict[str, int] = {}
        self._op_circuit_open_until: dict[str, float] = {}
        self._op_probing: set[str] = set()

    def _alloc_id(self) -> int:
        """Monotonic request id for response multiplexing."""
        self._next_id += 1
        return self._next_id

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def status(self) -> dict:
        return {
            "connected": self._connected,
            "reconnecting": self._running and not self._connected,
            "host": self._host,
            "port": self._port,
            "last_heartbeat": self._last_heartbeat,
            "uptime": int(time.time() - self._connect_time) if self._connected else 0,
            "handlers": sorted(self._handlers.keys()),
        }

    def register_handler(self, action: str, fn: DeviceHandler) -> None:
        """Register a handler for inbound device.dispatch with this action.

        Handler receives the dispatch payload dict and returns a dict that
        the bridge forwards back to the originating edge proxy.
        """
        self._handlers[action] = fn

    def _build_context(self) -> Optional[ssl.SSLContext]:
        return _build_ssl_context(self._cert, self._key, self._ca, self._verify_hostname)

    async def start(self):
        """Run the persistent connection loop. Returns only on stop()."""
        if not self._host:
            logger.warning("BRIDGE_HOST is not set — bridge client not started")
            return
        ssl_ctx = self._build_context()
        if not ssl_ctx:
            return

        self._running = True
        while self._running:
            try:
                await self._connect(ssl_ctx)
                # Spawn reader; heartbeat loop runs alongside it.
                self._reader_task = asyncio.create_task(self._reader_loop())
                await self._heartbeat_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Bridge connection lost: %s", e)
            finally:
                self._connected = False
                if self._reader_task and not self._reader_task.done():
                    self._reader_task.cancel()
                    try:
                        await self._reader_task
                    except asyncio.CancelledError:
                        pass
                self._reader_task = None

            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_MAX)

    async def stop(self):
        self._running = False
        self._connected = False
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

    async def _candidate_addrs(self) -> list[str]:
        """Bridge addresses to try, best first.

        BRIDGE_HOST is normally the headless service
        `bridge.privatae-system.svc.cluster.local`, which resolves to EVERY
        node's bridge — the DaemonSet runs one per node and each advertises its
        own node IP. Only the pod's OWN node ever answers: a cross-node dial to
        a hostNetwork bridge is silently dropped rather than refused, so it
        costs the full OS TCP connect timeout (~130s) before the next address is
        tried. Measured with the load-test harness: same-node
        112 ok / 0 fail, cross-node 0 ok / 732 fail. The extra addresses buy no
        failover — only delay.

        Left to itself, `asyncio.open_connection(host, ...)` walks that list
        internally with no per-address bound, so an unlucky DNS order stalls a
        reconnect for minutes. Measured live the same day: after a bridge
        DaemonSet roll, entity pods took >15 minutes to reconnect while their
        node-local bridge answered a direct dial in 63ms.

        So: resolve here, put this pod's own node first (HOST_IP, the downward
        API `status.hostIP` the operator already injects for the node-local
        squid), and let the caller bound each attempt separately. A single
        wait_for around open_connection would be WORSE — it would abort the walk
        before reaching the address that actually works.

        Falls back to [BRIDGE_HOST] if resolution fails, which preserves the old
        behaviour rather than turning a DNS blip into a hard outage.
        """
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(
                self._host, self._port, type=socket.SOCK_STREAM)
        except Exception as e:
            logger.warning("bridge DNS resolve failed for %s (%s) — dialing the name",
                           self._host, e)
            return [self._host]

        addrs: list[str] = []
        for info in infos:
            ip = info[4][0]
            if ip not in addrs:
                addrs.append(ip)
        if not addrs:
            return [self._host]

        host_ip = (os.environ.get("HOST_IP") or "").strip() if self._prefer_host_ip else ""
        # ALWAYS first when set — whether or not DNS returned it. The bridge
        # Service is headless with no publishNotReadyAddresses, so while this
        # node's bridge pod is being replaced (not Ready) its address is
        # ABSENT from the answer; the old `if host_ip in addrs` then never
        # promoted it, and the client walked every cross-node address at
        # CONNECT_TIMEOUT each — dials the docstring above measured as
        # silently dropped (0 ok / 732 fail). Measured on one entity pod: the
        # bridge DaemonSet replaced the node's pod at 01:19:56; the kernel
        # logged "no bridge address answered (7 tried, 5s each)"
        # at 01:20:36 and only reconnected — to the node-local address — at
        # 01:20:42. A ~5-10 s pod swap became >40 s with no bridge. Dialing
        # HOST_IP first costs one CONNECT_TIMEOUT while the pod is still
        # coming up, then succeeds the moment it listens; the extra addresses
        # buy nothing either way.
        if host_ip:
            if host_ip in addrs:
                addrs.remove(host_ip)
            addrs.insert(0, host_ip)
        return addrs

    async def _connect(self, ssl_ctx: ssl.SSLContext):
        addrs = await self._candidate_addrs()
        logger.info("Connecting to bridge %s:%d — %d candidate address(es), trying %s first",
                    self._host, self._port, len(addrs), addrs[0])
        # limit=10 MiB: default StreamReader cap is 64 KiB, which is smaller
        # than a typical signed-bundle envelope (base64 of a 100 KB .cseries is
        # ~140 KB). Without this, fetch_bundle / icon responses raise
        # LimitOverrunError in the reader loop, the connection is marked
        # dead, and every callsite times out at REQUEST_TIMEOUT.
        last_err: Optional[BaseException] = None
        for i, addr in enumerate(addrs):
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        addr, self._port, ssl=ssl_ctx,
                        server_hostname=self._server_hostname,
                        limit=10 * 1024 * 1024,
                    ),
                    timeout=CONNECT_TIMEOUT,
                )
                if i:
                    logger.warning("bridge: connected to %s after %d unreachable "
                                   "address(es) — the node-local bridge did not answer",
                                   addr, i)
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_err = e
                logger.debug("bridge address %s unreachable in %ss: %s",
                             addr, CONNECT_TIMEOUT, e)
        else:
            # Every candidate failed. Raise so start()'s loop applies its backoff
            # rather than leaving the client wedged claiming a live connection.
            raise ConnectionError(
                "no bridge address answered (%d tried, %ss each): %s"
                % (len(addrs), CONNECT_TIMEOUT, last_err))
        self._connected = True
        self._connect_time = time.time()
        self._reconnect_delay = RECONNECT_BASE
        # Drain any leftover queue items from prior connection.
        while not self._response_queue.empty():
            try:
                self._response_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # A fresh connection means any abandoned-request responses from the OLD
        # socket will never arrive — reset the orphan counter so we don't
        # wrongly discard a real response on the new connection.
        self._pending_orphans = 0
        # Likewise, any id-keyed futures awaiting a response on the OLD socket
        # will never resolve — fail them so their awaiters raise instead of
        # hanging until timeout, then clear the map for the new connection.
        for _f in self._pending.values():
            if not _f.done():
                _f.set_exception(ConnectionError("bridge reconnected"))
        self._pending.clear()
        # End any in-flight streams on the old socket: push the sentinel so their
        # drain tasks finish; the failed future above already unblocks the
        # awaiting request_stream, whose finally pops the entry.
        for _q in self._streams.values():
            try:
                _q.put_nowait(None)
            except Exception:
                pass
        self._streams.clear()
        logger.info("Connected to bridge")

        # Ship D — register as a memory.config subscriber so the bridge knows
        # to fan central config-deltas down to us via device.dispatch. Fire
        # and forget; failure is non-fatal (config sync just stays poll-based
        # until the next reconnect).
        try:
            asyncio.create_task(self._subscribe_memory_config())
        except Exception as exc:
            logger.warning("Could not schedule memory.config.subscribe: %s", exc)

    async def _subscribe_memory_config(self) -> None:
        """One-shot bridge call to register as a config-push subscriber.

        Runs after `_connect` succeeds. Reads the local config_versions table
        to pick a sane starting cursor (max sequence we've already applied)
        so the bridge doesn't replay deltas we already have.
        """
        try:
            from server import brain_service
            pool = brain_service.get_pool()
            cursor = 0
            if pool is not None:
                try:
                    cursor = int(
                        await pool.fetchval(
                            "SELECT COALESCE(MAX(sequence), 0) FROM config_versions"
                        ) or 0
                    )
                except Exception:
                    cursor = 0
            resp = await self.request(
                "memory.config.subscribe",
                {"sequence_cursor": cursor},
            )
            logger.info("memory.config.subscribe ack: %s", resp)
        except Exception as exc:
            logger.warning("memory.config.subscribe failed (non-fatal): %s", exc)

    async def _reader_loop(self):
        """Single reader: demultiplex responses vs inbound dispatches."""
        try:
            while self._connected and self._reader:
                line = await self._reader.readline()
                if not line:
                    self._connected = False
                    break
                try:
                    msg = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    logger.warning("Bridge sent malformed line, ignoring")
                    continue

                # Streaming text delta: a chunk frame carries the id + "chunk"
                # and NO "ok" (it is not terminal). Route it into the per-id
                # stream queue, in order; the request stays pending until its
                # terminal "ok" frame arrives. Checked before the "ok" block
                # since chunk frames have no "ok" key.
                if "chunk" in msg and "ok" not in msg:
                    rid = msg.get("id")
                    q = self._streams.get(rid)
                    if q is not None:
                        try:
                            q.put_nowait(msg.get("chunk") or "")
                        except Exception:
                            pass
                    # else: late/unknown stream id → discard silently.
                    continue

                # Response to one of our outbound requests
                if "ok" in msg:
                    rid = msg.get("id")
                    # New server: response carries the echoed id → hand it to
                    # the matching waiter's future directly (id multiplexing).
                    if rid is not None and rid in self._pending:
                        # Terminal for a stream too: drop the stream registry
                        # entry (its drain task ends via the finally sentinel).
                        self._streams.pop(rid, None)
                        fut = self._pending.pop(rid)
                        # Its waiter may have been cancelled in this same loop
                        # turn (a timeout firing as the reply is read): the
                        # reply is dropped, and the reader lives on — setting a
                        # result on a cancelled future raises, which would end
                        # this loop and drop the connection for every request.
                        if not fut.done():
                            fut.set_result(msg)
                        continue
                    # Old server: no id echoed → fall back to legacy FIFO,
                    # including the orphan-discard for timed-out requests.
                    if rid is None:
                        # Discard late responses from requests we already gave up
                        # on (timed out). The bridge answers in FIFO order, so
                        # the next `_pending_orphans` responses belong to
                        # abandoned callers, NOT to the current waiter — handing
                        # one over is the cross-talk bug (op X receiving op Y's
                        # response).
                        if self._pending_orphans > 0:
                            self._pending_orphans -= 1
                            logger.warning(
                                "bridge: discarded a late/orphan response from a "
                                "timed-out request (%d orphan(s) remain)",
                                self._pending_orphans,
                            )
                            continue
                        await self._response_queue.put(msg)
                        continue
                    # id present but no waiter → a late/timed-out response.
                    # Discard it (its awaiter is already gone).
                    logger.debug(
                        "bridge: discarded response for unknown/late id=%s", rid,
                    )
                    continue

                # Inbound dispatch from bridge
                if msg.get("op") == "device.dispatch":
                    asyncio.create_task(self._handle_dispatch(msg.get("params") or {}))
                    continue

                logger.warning("Bridge sent unrecognized message shape: %s", list(msg.keys()))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Bridge reader loop ended: %s", e)
            self._connected = False

    async def _handle_dispatch(self, params: dict):
        action = params.get("action")
        correlation_id = params.get("correlation_id")
        payload = params.get("payload") or {}

        # Governance-affecting actions get a per-op gate BEFORE the handler
        # runs — the previous code trusted the action name unconditionally.
        # The only legitimate source of a governance dispatch is the live
        # mTLS-authenticated bridge connection (the reader loop that called
        # us). If we are not connected to the bridge, this dispatch did not
        # arrive through the authenticated transport, so a governance write
        # is REFUSED (fail closed) rather than applied.
        if action in GOVERNANCE_ACTIONS:
            attested, why = self._attest_governance(action, payload)
            if not attested:
                logger.warning(
                    "governance dispatch %s refused: %s (correlation_id=%s)",
                    action, why, correlation_id,
                )
                try:
                    # device.respond is fire-and-forget (we never await its
                    # ack), but carry an id so a NEW server echoes it and the
                    # reader DISCARDS the waiter-less ack instead of leaking it
                    # onto the legacy FIFO queue (which nothing drains now).
                    await self._send({
                        "op": "device.respond",
                        "id": self._alloc_id(),
                        "params": {
                            "correlation_id": correlation_id,
                            "result": {"ok": False, "error": f"governance dispatch refused: {why}"},
                        },
                    })
                except Exception as e:
                    logger.warning("failed to send governance-refusal respond: %s", e)
                return
            logger.info("governance dispatch %s attested (correlation_id=%s)",
                        action, correlation_id)

        handler = self._handlers.get(action)
        if not handler:
            result = {"ok": False, "error": f"unknown action: {action}"}
        else:
            try:
                ret = handler(payload)
                if inspect.isawaitable(ret):
                    ret = await ret
                if isinstance(ret, dict) and "ok" in ret:
                    result = ret
                else:
                    result = {"ok": True, "data": ret}
            except Exception as e:
                logger.exception("device handler %s failed", action)
                result = {"ok": False, "error": str(e)}

        try:
            # Carry an id (fire-and-forget): a NEW server echoes it and the
            # reader discards the waiter-less ack rather than leaking it onto
            # the legacy FIFO queue.
            await self._send({
                "op": "device.respond",
                "id": self._alloc_id(),
                "params": {"correlation_id": correlation_id, "result": result},
            })
        except Exception as e:
            logger.warning("Failed to send device.respond for %s: %s", correlation_id, e)

    def _attest_governance(self, action: str, payload: dict) -> tuple[bool, str]:
        """Attest a governance-affecting inbound dispatch before it runs.

        Returns (True, "") to allow, (False, reason) to refuse.

        The trust anchor for an inbound dispatch is the mTLS-authenticated
        bridge connection: `device.dispatch` is delivered ONLY by the reader
        loop on the live bridge socket (cert-pinned in `_build_ssl_context`).
        So the attestation is "this dispatch arrived over the live
        authenticated bridge" — `self._connected` is True exactly when that
        socket is up. If we are not connected, the dispatch did not come
        through the authenticated transport and the governance write is
        refused (fail closed). Benign device actions never reach here.

        This deliberately does NOT invent a new required wire field: the
        PME->EME producers (the platform's memory service) dispatch these actions with a
        plain payload, so requiring an unsent token would dark-wire the
        live governance path. The connection-authenticity check is the
        real, already-present wall; making it explicit per-op is the fix.
        """
        if not self._connected:
            return False, "no live authenticated bridge connection"
        if not self._writer:
            return False, "bridge transport not established"
        return True, ""

    async def _send(self, msg: dict) -> None:
        if not self._connected or not self._writer:
            raise RuntimeError("bridge not connected")
        async with self._writer_lock:
            self._writer.write(json.dumps(msg).encode() + b"\n")
            await self._writer.drain()

    async def _heartbeat_loop(self):
        """Liveness authority for the connection. start()'s reconnect path
        runs ONLY when this loop exits — so every failure mode must exit,
        never spin. Two covered wedges (the bridge-roll outage class):
          (a) black-holed socket: pings time out, request() returns None
              forever → the miss counter breaks out after
              HEARTBEAT_MISS_LIMIT consecutive misses;
          (b) blocked send: _send() can hang on writer.drain() against a
              dead peer (request_full's wait_for only covers the response
              future, not the send) → the outer wait_for breaks out.
        Data ops are deliberately NOT the liveness signal — a slow LONG_OP
        must not trip a reconnect; only the control ping decides."""
        misses = 0
        while self._running and self._connected:
            try:
                result = await asyncio.wait_for(
                    self.request("heartbeat.ping", _heartbeat_params()),
                    timeout=REQUEST_TIMEOUT + 5,
                )
                if result is not None:
                    self._last_heartbeat = time.time()
                    misses = 0
                else:
                    misses += 1
                    logger.warning(
                        "Heartbeat miss %d/%d (no pong)", misses, HEARTBEAT_MISS_LIMIT)
                    if misses >= HEARTBEAT_MISS_LIMIT:
                        logger.warning(
                            "Bridge declared DEAD after %d consecutive heartbeat "
                            "misses — reconnecting", misses)
                        self._connected = False
                        break
            except Exception as e:
                logger.warning("Heartbeat failed (%s) — reconnecting", e)
                self._connected = False
                break
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def request_full(self, op: str, params: dict = None) -> dict:
        """Send a request, return full envelope.

        Multiplexing: each request carries a monotonic "id" and
        registers a future in `self._pending[id]`. The reader resolves that
        future when the matching response (same echoed id) arrives, so many
        requests can be in flight concurrently on one connection — no
        end-to-end request lock, no FIFO ordering dependency, no pre-send
        queue drain. The id match is what prevents cross-talk now.

        (Against an OLD server that doesn't echo ids the future never resolves
        and this call times out; correctness relies on rolling the server
        first. The reader still feeds the legacy `_response_queue` for the
        id-less case, but this method no longer reads from it.)
        """
        if not self._connected:
            return {"ok": False, "error": "not connected", "error_kind": "infra"}  # pod↔bridge transport = infra

        if op in BUNDLE_OPS and isinstance(params, dict):
            staged = await stage_bundle_params(self.request_full, params)
            if staged.get("ok") is False:
                return staged
            params = staged

        # Circuit breaker (A): a breakable op whose backend is down must not
        # be sent repeatedly and starve control ops. While its circuit is
        # open, fast-fail WITHOUT sending; allow ONE half-open probe per
        # cooldown. Healthy operation is unchanged — the circuit only opens
        # after CIRCUIT_FAIL_THRESHOLD consecutive failures.
        breakable = op in BREAKABLE_OPS
        if breakable:
            open_until = self._op_circuit_open_until.get(op, 0.0)
            if open_until:
                if time.time() < open_until:
                    return {"ok": False, "error": "circuit_open", "error_kind": "infra"}
                # Cooldown elapsed → half-open. Let exactly ONE caller probe;
                # everyone else keeps fast-failing. Re-arm the window so late
                # checkers fast-fail until this probe resolves.
                if op in self._op_probing:
                    return {"ok": False, "error": "circuit_open", "error_kind": "infra"}
                self._op_probing.add(op)
                self._op_circuit_open_until[op] = time.time() + CIRCUIT_COOLDOWN

        timeout = LONG_REQUEST_TIMEOUT if op in LONG_OPS else REQUEST_TIMEOUT

        mid = self._alloc_id()
        fut = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        try:
            try:
                await self._send({"op": op, "params": params or {}, "id": mid})
                msg = await asyncio.wait_for(fut, timeout=timeout)
                if breakable:
                    self._record_op_result(op, ok=bool(msg.get("ok")))
                return msg
            except asyncio.TimeoutError:
                # Drop the waiter; a late response for this id is discarded by
                # the reader (no waiter present) instead of poisoning another.
                self._pending.pop(mid, None)
                if breakable:
                    self._record_op_result(op, ok=False)
                return {"ok": False, "error": "timeout", "error_kind": "infra"}
            except Exception as e:
                self._connected = False
                if breakable:
                    self._record_op_result(op, ok=False)
                return {"ok": False, "error": str(e), "error_kind": "infra"}
        finally:
            self._pending.pop(mid, None)

    async def request_stream(self, op: str, params: dict, on_delta) -> dict:
        """Send a streaming request; await `on_delta(text)` per chunk frame; return
        the terminal full envelope (same shape as request_full) for billing + the
        tool-loop.

        Concurrency-safe via the same id multiplexing as request_full: the request
        registers BOTH a _pending future (for the terminal "ok" frame) and a
        _streams queue (for chunk frames). A dedicated drain task delivers chunks
        to on_delta IN ORDER off the queue, so the single reader loop never blocks
        on a slow consumer and concurrent streams don't interleave. All queued
        chunks are flushed before this returns (the terminal frame is enqueued
        after the last chunk by the in-order reader, and the finally sentinel
        follows it).

        Streaming only works against a NEW server that echoes ids and emits chunk
        frames; correctness relies on rolling the bridge server first (done in
        Stage 1). On an old server the terminal future never resolves → timeout.
        """
        if not self._connected:
            return {"ok": False, "error": "not connected", "error_kind": "infra"}  # pod↔bridge transport = infra

        timeout = LONG_REQUEST_TIMEOUT  # a streamed generation is a long op

        mid = self._alloc_id()
        fut = asyncio.get_event_loop().create_future()
        q: asyncio.Queue = asyncio.Queue()
        self._pending[mid] = fut
        self._streams[mid] = q

        async def _drain():
            while True:
                chunk = await q.get()
                if chunk is None:  # end sentinel
                    return
                try:
                    await on_delta(chunk)
                except Exception:
                    # A consumer error must not wedge the stream or the reader.
                    pass

        drain_task = asyncio.create_task(_drain())
        try:
            await self._send({"op": op, "params": params or {}, "id": mid})
            msg = await asyncio.wait_for(fut, timeout=timeout)
            return msg
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            return {"ok": False, "error": "timeout", "error_kind": "infra"}
        except Exception as e:
            self._connected = False
            return {"ok": False, "error": str(e), "error_kind": "infra"}
        finally:
            self._pending.pop(mid, None)
            self._streams.pop(mid, None)
            # Stop the drain task: the sentinel goes AFTER any chunks already
            # queued, so they are all delivered to on_delta before we return.
            try:
                q.put_nowait(None)
            except Exception:
                pass
            with contextlib.suppress(Exception):
                await drain_task

    def _record_op_result(self, op: str, *, ok: bool) -> None:
        """Update circuit-breaker state for a breakable op. A success closes
        the circuit and clears the failure count; the Nth consecutive failure
        (N >= CIRCUIT_FAIL_THRESHOLD) opens it for CIRCUIT_COOLDOWN seconds."""
        self._op_probing.discard(op)
        if ok:
            if self._op_failures.get(op):
                logger.info("bridge: circuit for op=%s closed (probe succeeded)", op)
            self._op_failures[op] = 0
            self._op_circuit_open_until[op] = 0.0
            return
        n = self._op_failures.get(op, 0) + 1
        self._op_failures[op] = n
        if n >= CIRCUIT_FAIL_THRESHOLD:
            self._op_circuit_open_until[op] = time.time() + CIRCUIT_COOLDOWN
            logger.warning(
                "bridge: circuit for op=%s OPEN after %d consecutive failures "
                "— fast-failing it for %.0fs so it can't starve control ops",
                op, n, CIRCUIT_COOLDOWN,
            )

    async def request(self, op: str, params: dict = None, *,
                      raise_on_infra: bool = False) -> Optional[dict]:
        """Send a request, return data dict or None on error.

        error_kind consumption: a not-ok envelope tagged
        error_kind == "infra" is a TRANSPORT failure (bridge down /
        timeout / circuit-open), not "no data". A caller that must
        distinguish passes raise_on_infra=True and catches
        BridgeUnavailable; the None return stays for best-effort callers.
        Never flip the default — _heartbeat_loop's miss counter counts
        None pongs, and an exception there would collapse the 2-miss
        tolerance to a single miss.
        """
        msg = await self.request_full(op, params)
        if msg.get("ok"):
            return msg.get("data", {})
        if msg.get("error_kind") == "infra":
            logger.warning("Bridge INFRA failure on %s: %s", op, msg.get("error"))
            if raise_on_infra:
                raise BridgeUnavailable(op, str(msg.get("error") or "bridge unavailable"))
        else:
            logger.warning("Bridge error on %s: %s", op, msg.get("error"))
        return None

    @contextlib.asynccontextmanager
    async def busy(self):
        """Mark this entity as internally busy for the duration of a block.

        Used by the brain around substantive work (memory consolidation,
        scheduled bots, codebook reconciliation, feed processing) so the
        operator's freeze sweep doesn't put a metered entity to sleep
        while it's actually working.

        Sends `entity.busy {busy: true}` on entry and `{busy: false}` on exit.
        Best-effort: failures don't raise (the brain still runs locally even
        if the bridge is unreachable).

        Usage:
            async with bridge_client.busy():
                await consolidator.consolidate(...)
        """
        try:
            await self.request("entity.busy", {"busy": True})
        except Exception as e:
            logger.debug("entity.busy(true) failed: %s", e)
        try:
            yield
        finally:
            try:
                await self.request("entity.busy", {"busy": False})
            except Exception as e:
                logger.debug("entity.busy(false) failed: %s", e)
