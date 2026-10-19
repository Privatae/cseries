#!/usr/bin/env python3
"""Gate: a keep-alive client of the mTLS front door gets an answer to EVERY
request, however long it waited between them.

Measured on a rig (server/mtls_frontdoor.handle_client with TLS off,
uvicorn 0.34.0 on its defaults, one keep-alive client, POSTs 6s apart): ok,
FAIL, ok, FAIL, ok, FAIL. handle_client opened ONE upstream connection per
client and kept it for the client's life (IDLE_TIMEOUT, 300s), while uvicorn
drops an idle upstream after its keep-alive (5s). The request after any longer
gap was written into a closed upstream and the client got no response at all —
an EOF, which a Go client does not retry for a POST. Bastion add-folder hit it
on every second attempt (the end-to-end test's agent-store check has retried
around it), and so does anything posting through the door on a cadence.

Container-free and uvicorn-free: the upstream here is a small asyncio HTTP/1.1
server that closes a connection after IDLE_UPSTREAM_S of silence — the same
behaviour as uvicorn's keep-alive, on a clock short enough for a gate.

Prints one line; OK... on success. Run: python3 scripts/test_frontdoor_upstream_idle.py
"""
import asyncio
import http.client
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDLE_UPSTREAM_S = 0.3     # the stand-in for uvicorn's 5s keep-alive
GAP_S = 0.6               # longer than that, as a 60s heartbeat is longer than 5s
N = 5


def _free_port() -> int:
    # Loopback on purpose: the upstream, the door and the client all live in
    # this one test process and nothing outside it may reach them.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


UP_PORT, DOOR_PORT = _free_port(), _free_port()
os.environ["FRONTDOOR_UPSTREAM_HOST"] = "127.0.0.1"
os.environ["FRONTDOOR_UPSTREAM_PORT"] = str(UP_PORT)
sys.path.insert(0, ROOT)
from server import mtls_frontdoor as fd  # noqa: E402


async def _upstream(reader, writer):
    """Keep-alive HTTP/1.1 that hangs up after IDLE_UPSTREAM_S idle."""
    try:
        while True:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"),
                                              IDLE_UPSTREAM_S)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                return
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            body = await reader.readexactly(length) if length else b""
            if b"x-test-interim" in head.lower():
                # An interim response first, as uvicorn sends for Expect.
                writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            out = b"ok:" + body
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                         b"Content-Length: " + str(len(out)).encode() + b"\r\n\r\n" + out)
            await writer.drain()
    finally:
        writer.close()


def _serve():
    async def main():
        up = await asyncio.start_server(_upstream, "127.0.0.1", UP_PORT)
        door = await asyncio.start_server(fd.handle_client, "127.0.0.1", DOOR_PORT)
        async with up, door:
            await asyncio.gather(up.serve_forever(), door.serve_forever())
    asyncio.run(main())


def main() -> int:
    threading.Thread(target=_serve, daemon=True).start()
    time.sleep(0.3)
    conn = http.client.HTTPConnection("127.0.0.1", DOOR_PORT, timeout=5)
    got = []
    for i in range(N):
        try:
            conn.request("POST", "/api/x", body=f"r{i}".encode(),
                         headers={"Content-Type": "text/plain"})
            r = conn.getresponse()
            got.append(f"{r.status}:{r.read().decode()}")
        except Exception as e:  # noqa: BLE001 — the failure IS the measurement
            got.append(f"FAIL:{type(e).__name__}")
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", DOOR_PORT, timeout=5)
        time.sleep(GAP_S)
    want = [f"200:ok:r{i}" for i in range(N)]
    if got != want:
        print(f"FAIL front door lost requests after an idle gap longer than the "
              f"upstream keep-alive: {' '.join(got)}")
        return 1
    # An interim 100 is passed through and the FINAL response still arrives
    # (it used to be taken as final, leaving the real one unread).
    conn = http.client.HTTPConnection("127.0.0.1", DOOR_PORT, timeout=3)
    try:
        conn.request("POST", "/api/x", body=b"i", headers={"X-Test-Interim": "1"})
        r = conn.getresponse()
        interim = f"{r.status}:{r.read().decode()}"
    except Exception as e:  # noqa: BLE001
        interim = f"FAIL:{type(e).__name__}"
    if interim != "200:ok:i":
        print(f"FAIL an interim 100 was taken as the final response: {interim}")
        return 1
    print(f"OK {N}/{N} keep-alive POSTs answered across {GAP_S}s gaps "
          f"(upstream idles out at {IDLE_UPSTREAM_S}s); 100 Continue passed through")
    return 0


if __name__ == "__main__":
    sys.exit(main())
