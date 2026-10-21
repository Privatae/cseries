#!/usr/bin/env python3
"""A WebSocket to the Host's /ws through the box's front door, for
run-vm-test.sh: TLS to <address>:443 with the Host's name, an HTTP/1.1
handshake (with the session cookie when one is given), then, when the
handshake is answered 101, one text frame that is not JSON, which the Host's
/ws answers with an error frame of its own.

  ws_probe.py <host name> <address> [<cookie name>=<value>]

Prints `status <code>` and, after a 101, `frame <text>`. Standard library
only; the box's certificate is not checked (it is the test's own).
"""
import base64
import os
import socket
import ssl
import sys


def read_frame(sock, buf):
    """(payload, rest) of one unmasked server frame at the start of buf."""
    while len(buf) < 2:
        buf += sock.recv(4096)
    n, at = buf[1] & 0x7F, 2
    if n == 126:
        while len(buf) < 4:
            buf += sock.recv(4096)
        n, at = int.from_bytes(buf[2:4], "big"), 4
    elif n == 127:
        while len(buf) < 10:
            buf += sock.recv(4096)
        n, at = int.from_bytes(buf[2:10], "big"), 10
    while len(buf) < at + n:
        buf += sock.recv(4096)
    return buf[at:at + n], buf[at + n:]


def main(argv):
    host, addr = argv[0], argv[1]
    cookie = argv[2] if len(argv) > 2 else ""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["http/1.1"])
    sock = ctx.wrap_socket(socket.create_connection((addr, 443), timeout=15), server_hostname=host)
    lines = ["GET /ws HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}",
             "Sec-WebSocket-Version: 13", f"Origin: https://{host}"]
    if cookie:
        lines.append(f"Cookie: {cookie}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    parts = head.split(b"\r\n", 1)[0].split()
    status = parts[1].decode() if len(parts) > 1 else "none"
    print(f"status {status}")
    if status != "101":
        return 0
    payload, mask = b"not json", os.urandom(4)
    sock.sendall(bytes([0x81, 0x80 | len(payload)]) + mask +
                 bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
    frame, _rest = read_frame(sock, rest)
    print(f"frame {frame.decode('utf-8', 'replace')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
