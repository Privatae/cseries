"""Bastion git lane (build 3 of Gitea-through-the-Host, 2026-08-28).

The platform forge answers only platform addresses (edge source gate), so a user's
LOCAL git cannot reach it directly. Their bastion now offers a loopback HTTP proxy
(`cseries-bastion run` → 127.0.0.1:<git_proxy_addr>); each local connection is spliced
over the tunnel into ONE WebSocket on this Host (`/bastion-git`, bearer + mTLS binding
exactly like the other lanes) and this worker relays it out the PLATFORM path —
`EgressProxy._direct_dispatch` (node squid → platform egress) — so the request arrives
at the edge from a platform address.

This lane is NOT general egress. It is the inverse of /bastion-egress (there the Host
dispatches the user's browsing OUT through the bastion); here the bastion sends the
user's git IN and the Host relays it to exactly one destination. Hard properties:
  - CONNECT to the platform's own git host on :443 only (nac_egress._platform_hosts);
    any other method, host, port, or bytes after the CONNECT head → 403, lane closes.
  - it never calls EgressProxy._handle (its bastion-first branch would bounce the request
    back out the bastion) and not _direct_dispatch either: the Host performs the CONNECT
    itself and splices only after the upstream's 200, so a denied CONNECT can never turn
    into a keep-alive channel for a second request.
  - it carries NO credential. The user's git presents its own (the bastion's
    `git-credential` helper fetches the user's platform-minted token from this Host,
    /api/network/bastions/git-credential); the Host's ~/.git-credentials is never used.
  - on for every paired bastion unless that bastion is quarantined.

Framing is the egress lane's, roles swapped: bastion → Host 'D'+bytes / 'E' (end);
Host → bastion the same. One connection per WebSocket.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("privatae.bastion.git")

_HEAD_LIMIT = 65536
_IO_TIMEOUT = 20.0


class _WSReader:
    """asyncio.StreamReader-shaped view over the bastion's 'D'/'E' frames."""

    def __init__(self, ws):
        self.ws = ws
        self._buf = b""
        self._eof = False

    async def read(self, n: int = 65536) -> bytes:
        while not self._buf and not self._eof:
            try:
                frame = await self.ws.receive_bytes()
            except Exception:
                self._eof = True
                break
            if not frame:
                continue
            if frame[:1] == b"D":
                self._buf = frame[1:]
            elif frame[:1] == b"E":
                self._eof = True
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


class _WSWriter:
    """asyncio.StreamWriter-shaped view: write() buffers, drain() sends 'D' frames,
    close() schedules 'E' best-effort (every real path drains before closing, so bodies
    reach git; a close racing the endpoint's own WS close may drop the 'E', which git
    sees as EOF anyway). Matches what EgressProxy._direct_dispatch/_pipe call."""

    def __init__(self, ws):
        self.ws = ws
        self._pending: list[bytes] = []
        self._closed = False

    def write(self, data: bytes) -> None:
        if data:
            self._pending.append(bytes(data))

    async def drain(self) -> None:
        pend, self._pending = self._pending, []
        for chunk in pend:
            await self.ws.send_bytes(b"D" + chunk)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        async def _fin():
            try:
                await self.drain()
            except Exception:
                pass
            try:
                await self.ws.send_bytes(b"E")
            except Exception:
                pass
        try:
            asyncio.get_running_loop().create_task(_fin())
        except RuntimeError:
            pass


async def _refuse(writer: _WSWriter, status: str, body: str) -> None:
    payload = body.encode()
    writer.write((f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                  f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode() + payload)
    try:
        await writer.drain()
    except Exception:
        pass
    writer.close()


class GitWorker:
    def __init__(self, ws, *, bastion_token_id: int, proxy):
        self.ws = ws
        self.token_id = bastion_token_id
        self.proxy = proxy   # the running EgressProxy (server.main._nac_egress_ref)

    async def serve(self) -> None:
        reader, writer = _WSReader(self.ws), _WSWriter(self.ws)
        head = b""
        try:
            while b"\r\n\r\n" not in head and b"\n\n" not in head:
                chunk = await asyncio.wait_for(reader.read(2048), timeout=_IO_TIMEOUT)
                if not chunk:
                    break
                head += chunk
                if len(head) > _HEAD_LIMIT:
                    break
        except asyncio.TimeoutError:
            head = b""
        if not head:
            writer.close()
            return
        first, _, _ = head.partition(b"\r\n")
        parts = first.split()
        method = parts[0].decode("latin-1", "ignore").upper() if parts else ""
        target = parts[1].decode("latin-1", "ignore") if len(parts) > 1 else ""
        # CONNECT ONLY. git over https always CONNECTs
        # through an http.proxy; with absolute-form allowed, the bytes AFTER the first
        # blank line — a second request on the same connection — would be spliced to
        # squid unchecked, i.e. general egress from a DMZ address for any local process.
        # A CONNECT head must also end exactly at its blank line: TLS bytes only follow
        # the 200, so anything trailing is a smuggled request.
        sep = b"\r\n\r\n" if b"\r\n\r\n" in head else b"\n\n"
        trailing = head[head.index(sep) + len(sep):]
        from server.network.nac_egress import _is_platform_dest
        host, port = self.proxy._host_port(target, 443) if method == "CONNECT" else (None, 0)
        if method != "CONNECT" or trailing or not _is_platform_dest(host) or port != 443:
            log.info("git lane: refused (method=%s host=%s port=%s trailing=%d) token %s",
                     method, host, port, len(trailing), self.token_id)
            await _refuse(writer, "403 Forbidden",
                          "This lane carries git (CONNECT to your platform forge:443) only.")
            return
        # The Host performs the CONNECT itself and splices ONLY after the upstream says
        # 200: squid answers a denied CONNECT with 403 +
        # keep-alive and would parse whatever the client sent next as a new request —
        # general egress. Nothing the client sends after its head reaches the upstream
        # before that 200, and after it the stream is TLS to the forge.
        await _connect_via_platform(host, head, reader, writer, self.token_id)


async def _open_upstream(host: str, port: int):
    """(reader, writer, speaks_proxy) to the platform path: the node squid when the pod
    env names one (the normal case — IPv6-only pods cannot dial an A-only forge), else a
    raw dial. Split out so the invariant check can fake it."""
    from server.network.nac_egress import _upstream_proxy
    up = _upstream_proxy()
    if up is not None:
        r, w = await asyncio.wait_for(asyncio.open_connection(up[0], up[1]), timeout=_IO_TIMEOUT)
        return r, w, True
    r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=_IO_TIMEOUT)
    return r, w, False


async def _connect_via_platform(host: str, head: bytes, reader: _WSReader, writer: _WSWriter,
                                token_id: int) -> None:
    try:
        up_r, up_w, speaks_proxy = await _open_upstream(host, 443)
    except Exception as exc:
        log.info("git lane: platform path unreachable (%s) token %s", type(exc).__name__, token_id)
        await _refuse(writer, "502 Bad Gateway",
                      "The platform egress proxy is unreachable; retry shortly.")
        return
    try:
        if speaks_proxy:
            up_w.write(head)
            await up_w.drain()
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = await asyncio.wait_for(up_r.read(2048), timeout=_IO_TIMEOUT)
                if not chunk:
                    break
                resp += chunk
                if len(resp) > _HEAD_LIMIT:
                    break
            status = resp.split(b"\r\n", 1)[0].split()
            if len(status) < 2 or status[1] != b"200":
                # Relay the upstream's answer (a 403/5xx body) and CLOSE both sides —
                # never leave the client on a keep-alive connection to squid.
                log.info("git lane: upstream refused CONNECT (%s) token %s",
                         status[1].decode("latin-1", "ignore") if len(status) > 1 else "?", token_id)
                writer.write(resp)
                try:
                    await writer.drain()
                except Exception:
                    pass
                writer.close()
                return
            # squid's own 200 line goes to the client (it may carry headers git ignores)
            writer.write(resp)
            await writer.drain()
        else:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        from server.network.nac_egress import EgressProxy
        await EgressProxy._pipe(reader, writer, up_r, up_w)
    except Exception:
        for w in (writer, up_w):
            try:
                w.close()
            except Exception:
                pass
