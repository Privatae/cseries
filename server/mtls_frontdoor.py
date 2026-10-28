"""
In-pod mTLS front terminator for the dashboard :8443 listener (Layer 2 of the
cross-tenant fix).

WHY THIS EXISTS
  The dashboard's localhost-bypass (server/middleware/localhost_only.py) used to
  trust ANY request that landed on the mTLS port. But uvicorn's
  `--ssl-cert-reqs=2` accepts ANY cert chained to the CEIGAS CA — i.e. *any*
  entity's relay cert, not just the dashboard router — and uvicorn (0.34) does
  not surface the verified peer cert to the ASGI app, so the app could not tell
  *which* identity called. One tenant could therefore read another's dashboard
  API over :8443. Layer 1 closes this at the network (per-entity Cilium
  ingressDeny, invariant `cross-tenant-pod-deny`); THIS is Layer 2, so a future
  mis-set network policy cannot reopen it.

HOW
  This process OWNS :8443. It terminates mTLS (CEIGAS CA, CERT_REQUIRED),
  extracts the client cert CN, and reverse-proxies to the loopback dashboard
  (uvicorn on [::1]:8800) injecting a TRUSTED `X-Client-Cert-CN: <cn>`
  header — stripping any client-supplied value first. LocalhostOnlyMiddleware
  grants the dashboard bypass ONLY when that header (seen from loopback) equals
  the dashboard router CN (DASHBOARD_ROUTER_CN); any other verified identity
  (a peer's `domain-<id>` cert) gets NO dashboard trust -> 403.

  It also adds X-CSeries-Frontdoor-Proof, a value made once per process that
  only this process holds. Every other caller on the pod's loopback
  can write X-Client-Cert-CN and X-Authentik-Email too, so the kernel keeps
  those headers only on a request carrying the proof
  (server/middleware/router_identity.py).

  Full HTTP/1.1 is proxied: keep-alive, request/response bodies (Content-Length
  and chunked), SSE streaming (chunked, flushed per-chunk), and WebSocket
  upgrades (101 -> raw bidirectional tunnel). One upstream connection per
  REQUEST (a 101 keeps it as the tunnel); see handle_client for why.
"""
from __future__ import annotations

import asyncio
import logging
import os
import ssl

from server.middleware import router_identity

log = logging.getLogger("mtls_frontdoor")

# Observable terminator state, reported by /api/system/health/deep — the
# original 2026-06 incident was exactly a silently dark :8443, so the door's
# aliveness is now a first-class health field rather than a log line.
# Values: "init" (task never dispatched — wiring lost), "waiting-cert",
# "listening", "crashed: <detail>" (supervisor restarts it in 2s).
STATE = "init"

MTLS_PORT = int(os.environ.get("MTLS_PORT", "8443"))
BIND_HOST = os.environ.get("MTLS_BIND_HOST", "::")
# Entity pods are IPv6-only — the dashboard binds [::] and is reachable on [::1]
# but NOT 127.0.0.1 (IPv4 loopback isn't bound; the smoke test probes [::1]:8800).
# Default to IPv6 loopback and fall back to IPv4 for any dual/legacy env.
UP_HOST = os.environ.get("FRONTDOOR_UPSTREAM_HOST", "::1")
UP_PORT = int(os.environ.get("FRONTDOOR_UPSTREAM_PORT",
                             os.environ.get("DASHBOARD_PORT", "8800")))
SERVER_CERT = os.environ.get("MTLS_SERVER_CERT", "/etc/privatae/certs/server.crt")
SERVER_KEY = os.environ.get("MTLS_SERVER_KEY", "/etc/privatae/certs/server.key")
CA_BUNDLE = os.environ.get("MTLS_CA_BUNDLE", "/etc/privatae/certs/ceigas_ca.crt")

HEADER = b"X-Client-Cert-CN"
_PROOF_HEADER = router_identity.PROOF_HEADER
CONNECT_TIMEOUT = 10
IDLE_TIMEOUT = 300          # keep-alive idle before we drop the client connection
STREAM_LIMIT = 4 * 1024 * 1024  # max header block / read buffer


def _build_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(SERVER_CERT, SERVER_KEY)
    ctx.load_verify_locations(CA_BUNDLE)
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _extract_cn(peercert) -> str | None:
    if not peercert:
        return None
    for rdn in peercert.get("subject", ()):
        for k, v in rdn:
            if k == "commonName":
                return v
    return None


def _parse_head(head: bytes):
    lines = head.split(b"\r\n")
    start = lines[0]
    headers = []
    for ln in lines[1:]:
        if not ln or b":" not in ln:
            continue
        name, _, val = ln.partition(b":")
        headers.append((name.strip(), val.strip()))
    return start, headers


def _request_head_problem(head: bytes, headers) -> str | None:
    """Why a client's request head must not be forwarded, or None (RFC 9112
    §2.2, §6.3). The head is rebuilt line by line before it goes upstream, and
    the proof is the last line of it (_rebuild_request_head). A bare LF or CR
    left inside a value, or a NUL, is a place where the kernel's parser could
    end a line, or the whole head, where this one did not, and so read the
    proof line as body: a request whose response echoes its body would hand
    the proof to the client. Two framings (duplicate Content-Length, or
    Transfer-Encoding beside it) are the same split on the body instead.
    Refused here, before anything is forwarded, whatever the kernel's parser
    would have done."""
    if b"\x00" in head:
        return "NUL in the request head"
    if b"\r" in head.replace(b"\r\n", b"") or b"\n" in head.replace(b"\r\n", b""):
        return "a bare CR or LF in the request head"
    cls = [v for k, v in headers if k.lower() == b"content-length"]
    tes = [v for k, v in headers if k.lower() == b"transfer-encoding"]
    if len(cls) > 1:
        return "more than one Content-Length"
    if cls and not cls[0].isdigit():
        return "a Content-Length that is not a number"
    if len(tes) > 1:
        return "more than one Transfer-Encoding"
    if cls and tes:
        return "Transfer-Encoding with Content-Length"
    return None


def _hget(headers, name: bytes):
    nl = name.lower()
    for k, v in headers:
        if k.lower() == nl:
            return v
    return None


def _rebuild_request_head(start: bytes, headers, cn: str | None) -> bytes:
    out = [start]
    for k, v in headers:
        if k.lower() in (HEADER.lower(), _PROOF_HEADER):
            continue  # strip any client-supplied CN or proof — never trust the wire value
        out.append(k + b": " + v)
    out.append(HEADER + b": " + (cn or "").encode("ascii", "replace"))
    # What tells the kernel this request came through here and not from some
    # other caller on the pod's loopback, who can write X-Client-Cert-CN and
    # X-Authentik-Email as easily as we can. A per-process value that
    # only this process holds; server/middleware/router_identity.py checks it
    # and takes it off before any route sees the request.
    out.append(_PROOF_HEADER + b": " + router_identity.proof())
    return b"\r\n".join(out) + b"\r\n\r\n"


async def _copy_chunked(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    while True:
        size_line = await reader.readuntil(b"\r\n")
        writer.write(size_line)
        size = int(size_line.split(b";")[0].strip() or b"0", 16)
        if size == 0:
            # trailers (if any) up to the terminating blank line
            while True:
                tl = await reader.readuntil(b"\r\n")
                writer.write(tl)
                if tl == b"\r\n":
                    break
            await writer.drain()
            return
        # Copied as it arrives, never read whole: the size is whatever the sender
        # declares, so readexactly(size) held up to that much of a chunk before
        # any cap behind this door could count a byte of it.
        await _copy_n(reader, writer, size)
        crlf = await reader.readexactly(2)
        writer.write(crlf)
        await writer.drain()


async def _copy_n(reader, writer, n: int):
    remaining = n
    while remaining > 0:
        chunk = await reader.read(min(65536, remaining))
        if not chunk:
            raise ConnectionError("eof mid-body")
        writer.write(chunk)
        remaining -= len(chunk)
        await writer.drain()


async def _copy_until_eof(reader, writer):
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            break
        writer.write(chunk)
        await writer.drain()


async def _copy_body(reader, writer, headers, *, is_response, method=None, status=None):
    """Copy a message body per HTTP/1.1 framing. Returns True if the body was
    close-delimited (connection must end after it)."""
    if is_response and (method == b"HEAD" or status in (204, 304)
                        or (status is not None and 100 <= status < 200)):
        return False
    te = (_hget(headers, b"transfer-encoding") or b"").lower()
    cl = _hget(headers, b"content-length")
    if b"chunked" in te:
        await _copy_chunked(reader, writer)
        return False
    if cl is not None:
        await _copy_n(reader, writer, int(cl))
        return False
    if is_response:
        # no CL, not chunked -> body runs until the upstream closes
        await _copy_until_eof(reader, writer)
        return True
    return False  # request with no framed body


async def _pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _close(writer):
    try:
        writer.close()
    except Exception:
        pass


# Try the configured loopback first, then the other family — covers IPv6-only
# pods ([::1]) and any IPv4/dual env (127.0.0.1) without a config change.
_UPSTREAM_HOSTS = [UP_HOST] + [h for h in ("::1", "127.0.0.1") if h != UP_HOST]


async def _open_upstream():
    last = None
    for h in _UPSTREAM_HOSTS:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(h, UP_PORT, limit=STREAM_LIMIT),
                CONNECT_TIMEOUT)
        except Exception as e:
            last = e
    raise last if last else ConnectionError("no upstream host")


async def handle_client(c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter):
    ssl_obj = c_writer.get_extra_info("ssl_object")
    cn = _extract_cn(ssl_obj.getpeercert() if ssl_obj else None)
    # ONE UPSTREAM CONNECTION PER REQUEST, not per client. The client side is
    # kept alive for IDLE_TIMEOUT (300s); uvicorn closes an idle upstream after
    # its own keep-alive (5s, the default server/launch.py runs with). A single
    # upstream pinned for the client's life was dead after any gap over 5s, so
    # the next request got no response at all — the client saw EOF, and a POST
    # is not retried. Measured on a rig (this handler, uvicorn
    # 0.34.0, one keep-alive client, 6s gaps): ok, FAIL, ok, FAIL, ok, FAIL —
    # the alternation the end-to-end test's agent store check retried around,
    # and every bastion POST spaced like the 60s heartbeat. A loopback connect
    # per request costs microseconds; it is also nginx's default upstream mode.
    u_writer = None
    try:
        while True:
            try:
                head = await asyncio.wait_for(
                    c_reader.readuntil(b"\r\n\r\n"), IDLE_TIMEOUT)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                    asyncio.LimitOverrunError, ConnectionError):
                break
            start, headers = _parse_head(head)
            if _request_head_problem(head, headers) is not None:
                # Answered and closed before any upstream exists, so nothing
                # of this request reaches the kernel.
                try:
                    c_writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n"
                                   b"Connection: close\r\n\r\n")
                    await c_writer.drain()
                except Exception:
                    pass
                break
            method = start.split(b" ", 1)[0].upper()
            req_conn = (_hget(headers, b"connection") or b"").lower()

            try:
                u_reader, u_writer = await _open_upstream()
            except Exception as e:
                log.warning("upstream connect failed (%s:%s): %s", UP_HOST, UP_PORT, e)
                # The request is already read, so the client is owed an
                # answer: a 502 it can act on, never a bare close it reads as
                # EOF (a POST is not retried).
                try:
                    c_writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                                   b"Connection: close\r\n\r\n")
                    await c_writer.drain()
                except Exception:
                    pass
                break
            u_writer.write(_rebuild_request_head(start, headers, cn))
            await u_writer.drain()
            try:
                await _copy_body(c_reader, u_writer, headers,
                                 is_response=False, method=method)
            except Exception:
                break

            # INTERIM 1xx RESPONSES (100 Continue, 103 Early Hints) are passed
            # through and the FINAL response is read after them — treating a
            # 100 as final left the real response unread in the upstream.
            # 101 is final here: it ends HTTP and opens the tunnel.
            interim_ok = True
            while True:
                try:
                    resp_head = await u_reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                        ConnectionError):
                    interim_ok = False
                    break
                r_start, r_headers = _parse_head(resp_head)
                try:
                    status = int(r_start.split(b" ")[1])
                except Exception:
                    status = 0
                if not (100 <= status < 200) or status == 101:
                    break
                try:
                    c_writer.write(resp_head)
                    await c_writer.drain()
                except Exception:
                    break
            if not interim_ok:
                # No final response came (the upstream died, possibly after a
                # 1xx). A final 502 is still valid HTTP here and tells the
                # client what a bare close would not.
                try:
                    c_writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                                   b"Connection: close\r\n\r\n")
                    await c_writer.drain()
                except Exception:
                    pass
                break

            c_writer.write(resp_head)
            await c_writer.drain()

            if status == 101:  # WebSocket / protocol upgrade -> raw tunnel
                await asyncio.gather(_pipe(c_reader, u_writer),
                                     _pipe(u_reader, c_writer))
                return

            try:
                close_delimited = await _copy_body(
                    u_reader, c_writer, r_headers,
                    is_response=True, method=method, status=status)
            except Exception:
                break

            resp_conn = (_hget(r_headers, b"connection") or b"").lower()
            if close_delimited or b"close" in resp_conn or b"close" in req_conn:
                break
            _close(u_writer)
            u_writer = None
    finally:
        if u_writer is not None:
            _close(u_writer)
        _close(c_writer)


async def main():
    global STATE
    ctx = _build_ssl_context()
    server = await asyncio.start_server(
        handle_client, BIND_HOST, MTLS_PORT, ssl=ctx, limit=STREAM_LIMIT)
    socknames = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("mTLS front terminator listening on %s -> %s:%s (router CN gated in-app)",
             socknames, UP_HOST, UP_PORT)
    STATE = "listening"
    async with server:
        await server.serve_forever()


def _certs_readable() -> bool:
    return all(os.access(p, os.R_OK) for p in (SERVER_CERT, SERVER_KEY, CA_BUNDLE))


async def run_supervised() -> None:
    """Own :8443 for the life of the pod, from inside the uvicorn event loop.

    Replaces the entrypoint.sh shell supervisor + separate python3 process
    (the second interpreter cost ~33-64MB/pod).
    Same guarantees the shell loop had, for the same historical reason (a
    silently dark :8443 looks like a routing bug and every bastion loses
    the pod): restart on crash with a short backoff, wait for a
    (re)provisioned cert instead of giving up at boot, log loudly.

    Genesis-mode pods (no server cert yet) sit in the wait branch until
    provisioning lands the cert — the dashboard :8800 listener is up the
    whole time, so enrollment is unaffected.
    """
    global STATE
    waiting_logged = False
    while True:
        if _certs_readable():
            waiting_logged = False
            try:
                await main()
            except asyncio.CancelledError:
                raise  # lifespan shutdown — don't supervise past it
            except Exception as e:
                STATE = f"crashed: {type(e).__name__}: {e}"
                log.warning("mTLS front terminator crashed (%s: %s) — restarting in 2s",
                            type(e).__name__, e)
        else:
            STATE = "waiting-cert"
            if not waiting_logged:
                # Log the wait once, not every 2s — genesis pods sit here
                # for their whole pre-enrollment life.
                log.info("mTLS front terminator: waiting for server cert at %s",
                         SERVER_CERT)
                waiting_logged = True
        await asyncio.sleep(2)
