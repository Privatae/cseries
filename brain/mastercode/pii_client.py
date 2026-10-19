"""
mastercode/pii_client.py — client for the home PII sterilization service.

Design: reports/2026-08-03-pii-sterilization-platform-design.md (v0.1).
The service is BASTION-ADVERTISED, exactly like models (v102/v104
precedent): the heartbeat carries a `pii_port` advertisement and the
client resolves the live endpoint from the entity's ONLINE bastion
heartbeat rows at call time — never a stored address (a stored mesh
address goes stale and hangs the caller). Wherever the service executes (home
server today, bastion daemon tomorrow), the advertisement moves and this
client follows.

Transport: the entity pod has NO route to the bastion mesh (the overlay
named by MESH_OVERLAY_CIDR) — direct dials time out (measured). Heartbeat-resolved endpoints
(mesh addresses) go through the hub sidecar's CONNECT proxy
(hub/peerfwd, HUB_PEER_FORWARD_ADDR, default [::1]:3129 — its reachable
set is the host's registered peers, and the bastion's tunnel IP is
registered by construction). A PII_SERVICE_URL override pointing at a
non-mesh address (dev/test fixture) dials direct. Minimal HTTP/1.1 over
asyncio streams in the _post_json_via_peer_forward shape
(memory_engine.py:318) — aiohttp can't speak plain-http CONNECT. One
connection per call (Connection: close); the service is one call per
write, so session pooling bought nothing and doesn't survive CONNECT.

Contract (v1): bearer auth; GET /health; POST /sterilize {text, scope,
verify} → {text, findings, stats}; POST /rehydrate {text, scope} → {text,
unknown_tokens, restored}; 502 = no output exists. Fail closed EVERYWHERE:
every error path returns a typed PiiFailure — callers never get partial
output. Logging carries stats counts, scope id, latency — NEVER bodies,
NEVER the key.
"""

import asyncio
import dataclasses
import ipaddress
import json
import logging
import os
import time

log = logging.getLogger("cseries.mastercode.pii_client")

# Connect is quick or broken; reads tolerate the 35B (contract: /sterilize
# ≥300s — ~13s TTFT before a token starts).
_CONNECT_TIMEOUT = 10.0
_READ_TIMEOUT = 60.0
_READ_TIMEOUT_STERILIZE = 600.0

# The pod can't forward-dial the bastion overlay — mesh endpoints ride the
# hub peerfwd CONNECT proxy instead. The overlay is deployment config
# (MESH_OVERLAY_CIDR) with no default: unset, no endpoint counts as mesh and
# every dial is direct.


def _mesh_net():
    raw = (os.environ.get("MESH_OVERLAY_CIDR") or "").strip()
    if not raw:
        return None
    try:
        return ipaddress.ip_network(raw, strict=False)
    except ValueError:
        log.warning("MESH_OVERLAY_CIDR is not a network: %r", raw)
        return None


_DEFAULT_PEER_FORWARD = "[::1]:3129"


def _api_key() -> str | None:
    """Resolve the service bearer key.

    TODO(Q3, open in the design doc): per-entity key store location
    is TBD (Entity CR env / entity DB secret row / platform secrets.json
    keyed by entity — different blast radii). Interim source: process env
    PII_API_KEY (one pod = one entity, so pod env IS per-entity). This is
    the ONLY place the key is read — swap the store here, callers don't
    change.
    """
    return os.environ.get("PII_API_KEY") or None


async def resolve_endpoint(pool, entity_id: int) -> str | None:
    """Live PII-service endpoint for this entity, or None when not advertised.

    Mirrors model_backends.list_local_models' heartbeat query shape. The
    `pii_port` column does not exist server-side yet (the advertisement is
    the platform side's piece): an undefined-column error is treated as NOT ADVERTISED
    — the honest disabled state, never an error. PII_SERVICE_URL env
    overrides resolution entirely (dev).
    """
    override = (os.environ.get("PII_SERVICE_URL") or "").strip()
    if override:
        return override.rstrip("/")
    if pool is None:
        return None
    try:
        from server.network.topology import ONLINE_WINDOW_SECONDS
        row = await pool.fetchrow(
            f"""
            SELECT h.reported_ip, h.pii_port
              FROM mcp_client_tokens t
              JOIN network_bastion_heartbeat h ON h.token_id = t.id
             WHERE t.entity_id = $1
               AND t.operator_kind = 'bastion'
               AND t.revoked_at IS NULL
               AND h.pii_port IS NOT NULL
               AND h.last_seen_at > now() - INTERVAL '{int(ONLINE_WINDOW_SECONDS)} seconds'
             ORDER BY h.last_seen_at DESC
             LIMIT 1
            """,
            entity_id,
        )
    except Exception as e:
        # Pre-advertisement schema (or DB hiccup) — same "discovery pending"
        # honesty as list_local_models: None, logged at debug, never raised.
        log.debug("pii endpoint resolution unavailable (%s)", type(e).__name__)
        return None
    if not row:
        return None
    from mastercode.model_backends import resolve_model_endpoint
    return resolve_model_endpoint(
        {"reported_ip": row["reported_ip"]}, row["pii_port"])


@dataclasses.dataclass
class PiiResult:
    """Typed outcome of one service call. `ok=False` is a FAILURE — the
    caller must defer/drop its write, never fall back to raw text."""
    ok: bool
    text: str | None = None
    unknown_tokens: list = dataclasses.field(default_factory=list)
    findings: int = 0       # /sterilize: how many values were tokenized
    restored: int = 0       # /rehydrate: how many tokens were restored
    error: str | None = None
    latency_ms: int = 0


# Session pooling is gone (one CONNECT per call — see the module docstring).

def _is_mesh_endpoint(url: str) -> bool:
    """True when the endpoint host sits in the configured mesh overlay."""
    net = _mesh_net()
    if net is None:
        return False
    try:
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname
        return bool(host) and ipaddress.ip_address(host) in net
    except ValueError:
        return False


def _proxy_for(url: str) -> str | None:
    """The hub peerfwd address for mesh endpoints, None for direct dials."""
    if not _is_mesh_endpoint(url):
        return None
    return (os.environ.get("HUB_PEER_FORWARD_ADDR") or "").strip() or _DEFAULT_PEER_FORWARD


class _ConnectRefused(ConnectionError):
    """The proxy answered CONNECT with a non-200 — the peer is not
    reachable through it right now. Typed so callers can tell 'the proxy
    says no' apart from a dead dial."""


async def _http_request(url: str, method: str, payload: dict | None,
                        headers: dict, *, proxy_addr: str | None = None,
                        read_timeout: float = _READ_TIMEOUT) -> tuple[int, dict]:
    """Minimal HTTP/1.1 over asyncio streams — the _post_json_via_peer_forward
    shape (memory_engine.py:318), plus GET support and caller headers.
    Mesh target: CONNECT through the proxy, then the request rides the
    tunnel. Direct target: plain dial, same request code. Content-Length,
    chunked, and read-to-EOF responses; Connection: close always.
    Returns (status_code, parsed_json_or_{})."""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    target = u.netloc                      # e.g. [2001:db8::a:d]:18081
    if proxy_addr:
        fhost, _, fport = proxy_addr.rpartition(":")
        dial = (fhost.strip("[]"), int(fport))
    else:
        dial = (u.hostname, u.port or 80)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(*dial), _CONNECT_TIMEOUT)
    try:
        if proxy_addr:
            writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            # drain bounded too: an accept-then-never-read peer must not
            # wedge the drain worker (review).
            await asyncio.wait_for(writer.drain(), _CONNECT_TIMEOUT)
            status = await asyncio.wait_for(reader.readline(), _CONNECT_TIMEOUT)
            if b" 200" not in status:
                raise _ConnectRefused(
                    f"peer-forward refused CONNECT {target}: "
                    f"{status.decode(errors='replace').strip()}")
            while True:  # drain the CONNECT response headers
                line = await asyncio.wait_for(reader.readline(), _CONNECT_TIMEOUT)
                if line in (b"\r\n", b"\n", b""):
                    break
        body = json.dumps(payload).encode() if payload is not None else b""
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        req_lines = [f"{method} {path} HTTP/1.1", f"Host: {target}"]
        for hk, hv in headers.items():
            req_lines.append(f"{hk}: {hv}")
        if payload is not None:
            req_lines.append("Content-Type: application/json")
            req_lines.append(f"Content-Length: {len(body)}")
        req_lines.append("Connection: close")
        writer.write(("\r\n".join(req_lines) + "\r\n\r\n").encode() + body)
        await asyncio.wait_for(writer.drain(), _CONNECT_TIMEOUT)
        status_line = await asyncio.wait_for(reader.readline(), read_timeout)
        parts = status_line.decode(errors="replace").split()
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        content_len = None
        chunked = False
        while True:
            line = await asyncio.wait_for(reader.readline(), read_timeout)
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode(errors="replace").partition(":")
            k = k.strip().lower()
            if k == "content-length":
                try:
                    content_len = int(v.strip())
                except ValueError:
                    pass
            elif k == "transfer-encoding" and "chunked" in v.lower():
                chunked = True
        if chunked:
            raw = b""
            while True:
                size_line = await asyncio.wait_for(reader.readline(), read_timeout)
                try:
                    size = int(size_line.strip().split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    await asyncio.wait_for(reader.readline(), read_timeout)  # trailing CRLF
                    break
                raw += await asyncio.wait_for(reader.readexactly(size), read_timeout)
                await asyncio.wait_for(reader.readexactly(2), read_timeout)  # chunk CRLF
        elif content_len is not None:
            raw = await asyncio.wait_for(reader.readexactly(content_len), read_timeout)
        else:  # Connection: close → read to EOF
            raw = await asyncio.wait_for(reader.read(), read_timeout)
        try:
            return code, json.loads(raw) if raw else {}
        except Exception:
            return code, {}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _call(pool, entity_id, method: str, path: str,
                payload: dict | None = None, scope: str | None = None) -> PiiResult:
    """One service call, fail-closed. Every early return is a typed failure;
    this function never raises and never returns a partial body."""
    t0 = time.monotonic()

    def _fail(error: str) -> PiiResult:
        latency = int((time.monotonic() - t0) * 1000)
        # Stats-only: endpoint kind, scope id, latency, error class — never
        # the request/response body, never the key.
        log.warning("pii %s %s failed: error=%s scope=%s %dms",
                    method, path, error, scope, latency)
        return PiiResult(ok=False, error=error, latency_ms=latency)

    endpoint = await resolve_endpoint(pool, entity_id)
    if not endpoint:
        return _fail("no_endpoint")

    headers = {}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    # No key → the request goes WITHOUT the header and the server's 401 is
    # surfaced as the failure (contract: no key = unauthorized), not a
    # client-side guess.

    # Mesh endpoints (heartbeat-resolved) ride the hub peerfwd proxy; a
    # non-mesh PII_SERVICE_URL override dials direct.
    proxy = _proxy_for(endpoint)
    read_timeout = _READ_TIMEOUT_STERILIZE if path == "/sterilize" else _READ_TIMEOUT
    try:
        status, data = await _http_request(
            endpoint + path, method, payload, headers,
            proxy_addr=proxy, read_timeout=read_timeout,
        )
    except _ConnectRefused:
        return _fail("connect_refused")
    except Exception as e:
        # Proxy/target unreachable, timeouts, resets — typed by class name.
        return _fail(type(e).__name__)

    if status in (401, 403):
        return _fail("unauthorized")
    if status == 502:
        # Contract: 502 = no output exists. Propagate as failure.
        return _fail("no_output")
    if status != 200:
        return _fail(f"status_{status}")
    if not isinstance(data, dict):
        return _fail("bad_payload")
    latency = int((time.monotonic() - t0) * 1000)
    return PiiResult(
        ok=True,
        text=data.get("text"),
        unknown_tokens=list(data.get("unknown_tokens") or []),
        findings=len(data.get("findings") or []),
        restored=int(data.get("restored") or 0),
        latency_ms=latency,
    )


async def health(pool=None, entity_id: int | None = None) -> PiiResult:
    """GET /health — the availability gate. ok=True iff the service answers."""
    return await _call(pool, entity_id, "GET", "/health")


async def sterilize(text: str, scope: str, verify: bool = False,
                    pool=None, entity_id: int | None = None) -> PiiResult:
    """POST /sterilize. On ok, `text` holds the TOKENIZED body. Any failure
    means NO safe output exists — the caller defers, never writes raw."""
    if not isinstance(text, str) or not text:
        return PiiResult(ok=False, error="empty_input")
    res = await _call(pool, entity_id, "POST", "/sterilize",
                      payload={"text": text, "scope": scope, "verify": bool(verify)},
                      scope=scope)
    if res.ok and not isinstance(res.text, str):
        # A 200 without a text body is not a partial success — it is a failure.
        return PiiResult(ok=False, error="bad_payload", latency_ms=res.latency_ms)
    return res


async def rehydrate(text: str, scope: str,
                    pool=None, entity_id: int | None = None) -> PiiResult:
    """POST /rehydrate. Display-time only (design §6). Tokens the scope map
    doesn't know come back in `unknown_tokens` and stay AS-IS in the text —
    surfaced, never repaired. `restored` counts the tokens that DID resolve."""
    if not isinstance(text, str) or not text:
        return PiiResult(ok=False, error="empty_input")
    res = await _call(pool, entity_id, "POST", "/rehydrate",
                      payload={"text": text, "scope": scope}, scope=scope)
    if res.ok and not isinstance(res.text, str):
        # Same guard as sterilize: a 200 without a text body is a failure,
        # never a partial success.
        return PiiResult(ok=False, error="bad_payload", latency_ms=res.latency_ms)
    return res
