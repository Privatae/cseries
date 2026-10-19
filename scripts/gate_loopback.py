"""Loopback test servers that let the client hang up first.

WHY. The CI runner's firewall (an nftables input chain) lets a runner's loopback SYN through only to a socket inside
cirunner.slice. A TIME_WAIT socket is not a full socket, so it never matches,
and the SYN is refused. TCP leaves the TIME_WAIT on whichever side closes
first. A test server that hangs up first therefore leaves one on its own port
for every connection, and a later connection whose source port lands on one of
them gets "Connection refused" from a server that is still running. The
backup-ship gate's fake failed CI runs this way; the throwaway databases did
the same until they moved to a unix socket.

When the client closes first, the TIME_WAIT sits on the client's side, where
the rule never looks, so a server that waits for its client cannot be hit.

ClientClosesFirst does that for an http.server handler. When the handler is
done with a connection, it waits for the client to hang up before the server
closes. urllib sends "Connection: close" on every request and closes its own
end as soon as it has read a Content-Length body, so for urllib the wait ends
at once. The handler waits only after a response whose end the client can see
(a Content-Length, chunked framing, or no body). After a response that ends
only when the connection closes, the server closes at once, as before;
otherwise each side would wait for the other.

client_closes_first_aiohttp does that for an aiohttp server. aiohttp takes the
keep-alive decision from the request before any hook (web_response.py
_prepare_headers), so a client that sends "Connection: close" (urllib does, on
every request) gets its connection closed by the server right after the reply,
and the TIME_WAIT lands on the server. The helper turns that decision over, for
a reply the client can see the end of, so the client closes first.

Not a gate (no test_ prefix): scripts/test_gate_coverage.py counts test_*.py
and check_*.py only.
"""
from __future__ import annotations

import asyncio
import http.client
import socket
import time

# Long enough for a client that reads a large body while doing other work
# (the backup-ship engine streams its source into a slow multipart upload)
# to finish and close; the wait runs in the handler's own thread.
CLIENT_CLOSE_WAIT_S = 30.0

# How long a server may hold a connection idle before it closes it, for a
# server whose own default (uvicorn's is 5 s) hangs up first on a client that
# keeps its connection pooled between requests. As long as
# pre_merge_gates.sh lets a gate run (its backstop is 1800 s), so a pooled
# connection ends when its client closes it, or when the gate ends.
IDLE_KEEP_ALIVE_S = 1800


def await_client_close(sock: socket.socket, wait_s: float = CLIENT_CLOSE_WAIT_S) -> bool:
    """Read and drop anything the client still sends until it hangs up, for at
    most wait_s seconds. True when the client closed first.

    Reads the TCP socket underneath any TLS layer. Reading through the TLS
    layer instead, the server answered the client's FIN with a 24-byte TLS
    record, which the closed client answered with a reset (measured
    with Python's ssl client on both sides). The handler is done
    with the connection, so there is nothing left to decrypt."""
    deadline = time.monotonic() + wait_s
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            sock.settimeout(left)
            if not socket.socket.recv(sock, 65536):
                return True
    except OSError:
        # A reset means the client is gone too. A timeout means it never closed,
        # so the server closes first, as it did before this helper.
        return False


class ClientClosesFirst:
    """Mix into an http.server handler, ahead of the base class:

        class Handler(ClientClosesFirst, http.server.BaseHTTPRequestHandler): ...

    Serve it from http.server.ThreadingHTTPServer. The wait runs in the
    handler's own thread; on a single-threaded server it would hold every other
    connection until this client hangs up."""

    client_close_wait_s = CLIENT_CLOSE_WAIT_S
    _client_sees_end = False

    def handle_one_request(self):
        # Each request starts undecided. On a kept-alive connection a handler
        # that fails before it answers must not inherit the last response's
        # framing, or finish() would wait for a client that is itself still
        # waiting for an answer.
        self._client_sees_end = False
        super().handle_one_request()

    def send_response_only(self, code, message=None):
        # Each response decides afresh. A 100 Continue is followed by the real
        # response, which decides again.
        self._client_sees_end = (code < 200 or code in (204, 304)
                                 or self.command == "HEAD")
        super().send_response_only(code, message)

    def send_header(self, keyword, value):
        k = keyword.lower()
        if k == "content-length" or (k == "transfer-encoding"
                                     and "chunked" in str(value).lower()):
            self._client_sees_end = True
        super().send_header(keyword, value)

    def finish(self):
        super().finish()
        if self._client_sees_end:
            await_client_close(self.connection, self.client_close_wait_s)


def client_closes_first_aiohttp(app, wait_s: float = CLIENT_CLOSE_WAIT_S) -> None:
    """Make an aiohttp app's server let the client close first. Call it on the
    app before its runner is set up (the app is frozen after that).

    When a client asked for "Connection: close" (HTTP/1.1), aiohttp closes the
    connection as soon as the reply is written. This keeps it open instead,
    until the client hangs up. If the client has not, the server hangs up
    wait_s seconds after the handler returned (as it did before), and not
    while it is still sending the reply. Every aiohttp reply is framed in
    HTTP/1.1 (a Content-Length, or chunked), so a client reads to the end of
    the reply without waiting for the connection to close.

    Left alone: a WebSocket, which ends by its own close handshake; an
    HTTP/1.0 request; a keep-alive request; and a handler that closes
    request.transport itself, which is how a gate cuts a stream on purpose.
    (A handler that calls force_close() on a reply to a "Connection: close"
    request is not told apart from aiohttp's own close; no gate does.)

    Reaches into StreamResponse._keep_alive, which aiohttp reads in
    RequestHandler.start after the handler returns (web_protocol.py) and
    exposes read-only as keep_alive. scripts/test_gate_loopback.py checks the
    effect on a real connection, so a version that stops honouring it fails
    that gate rather than quietly leaving TIME_WAITs."""
    from aiohttp import web

    def client_will_close(request, response) -> bool:
        return (not request.keep_alive and request.version >= (1, 1)
                and not isinstance(response, web.WebSocketResponse))

    async def leave_open(request, response):
        # Runs after aiohttp took its keep-alive decision from the request
        # (web_response.py _start: _prepare_headers, then this hook).
        if client_will_close(request, response):
            response._keep_alive = True

    def hang_up(protocol):
        transport = protocol.transport
        if transport is None or transport.is_closing():
            return
        if transport.get_write_buffer_size():
            # Still sending the reply to a slow reader: that is not idle.
            asyncio.get_running_loop().call_later(wait_s, hang_up, protocol)
            return
        protocol.force_close()

    @web.middleware
    async def cap(request, handler):
        response = None
        try:
            response = await handler(request)
            return response
        finally:
            # Armed when the handler is done, so a long stream is not cut.
            if client_will_close(request, response) and request.protocol is not None:
                asyncio.get_running_loop().call_later(wait_s, hang_up, request.protocol)

    app.on_response_prepare.append(leave_open)
    app.middlewares.append(cap)


async def await_peer_close(reader, wait_s: float = CLIENT_CLOSE_WAIT_S) -> bool:
    """await_client_close for an asyncio stream: read and drop what the peer
    still sends until it hangs up, for at most wait_s seconds. True when the
    peer closed first."""
    async def drain():
        while await reader.read(65536):
            pass
    try:
        await asyncio.wait_for(drain(), wait_s)
        return True
    except (asyncio.TimeoutError, OSError):
        return False


def get_client_first(port: int, path: str, timeout: float = 2.0,
                     host: str = "127.0.0.1") -> tuple[int, bytes]:
    """(status, body) of GET http://host:port/path, with the client's FIN sent
    before the server's.

    For a server that hangs up as soon as it has answered, whatever the client
    does (http.server with HTTP/1.0 does: the connector apps' own /health
    server is one). The client half-closes right after its request, so its FIN
    is the first on the connection, and the server's later FIN leaves the
    TIME_WAIT on the client. A server that reads its request and then answers
    (http.server does) is not troubled by it.

    For a server in another process. A server thread in this process can win
    the GIL race and answer and close before this thread has sent its FIN
    (measured: 2 to 5 of 42 connections in test_connector_permanent_failures);
    there, mix ClientClosesFirst into the handler instead."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path)
        conn.sock.shutdown(socket.SHUT_WR)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()
