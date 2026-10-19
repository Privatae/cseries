#!/usr/bin/env python3
"""cseries-app-egress — the enforcement wall for a user's own process app.

WHY THIS EXISTS. A user's process app runs in its own runtime pod, and a
custom binary needs an enforcement wall outside the untrusted code; the pod
alone has none.

Without the wall, `network[]` in an app.yaml is a declaration that buys the app
nothing at runtime. No pod on the entity pool has external DNS — every egress
goes through the node squid proxy, which does the resolving — so an app only
reaches the internet if the library it happens to use reads HTTP(S)_PROXY. pip
and git do; the app's own HTTP client library did not. Measured in the live
pod: pypi.org, github.com and the app's vendor API all returned "Temporary
failure in name resolution" while the app had declared every one of them, and
the app died at its data-download step with ClientConnectorDNSError. The user cannot fix that from an
app; nothing they build can work.

WHAT THIS DOES. One platform-owned process, in its own container, sharing the
pod's network namespace with the app:

  * DNS (udp :53) — a declared host resolves to loopback, BOTH families
    (A -> 127.0.0.1, AAAA -> ::1). Anything else is forwarded verbatim to
    the real cluster resolver, so `*.svc.cluster.local` (the app's own
    kernel URL) keeps working and an UNDECLARED external host keeps failing
    exactly as it does today. The allowlist is the wall, and it lives out
    here rather than in the app. UDP only: every answer this shim originates
    is tiny, and the only forwarded names are cluster names, so a truncated
    reply (the one thing that makes a resolver retry over TCP) does not
    occur on this path.
  * TCP :443 — reads the TLS ClientHello's SNI, checks it against the
    allowlist, and splices the connection to that host through the squid proxy
    with CONNECT. The bytes are never decrypted: this reads one plaintext
    handshake field and then copies ciphertext both ways.
  * TCP :80 — same check, keyed on the HTTP Host header, but forwarded as an
    ordinary proxy request (`GET http://host/path`) with `Connection: close`,
    because the node squid denies CONNECT to anything but :443
    (manifests/0012_entity_egress_proxy.yaml `http_access deny CONNECT
    !SSL_ports`). One request per connection; a keep-alive client simply
    reconnects, and every request is checked.

The listeners are DUAL-STACK on purpose: asyncio's start_server sets
IPV6_V6ONLY on a `::` socket (measured under python 3.12, re-measured under
3.14 when the toolchain image moved — same behaviour, and a hand-built `::`
socket with V6ONLY off still binds dual-stack),
which would leave the A record above pointing at a port nothing accepts on.
The sockets are built by hand with IPV6_V6ONLY off, so 127.0.0.1:443 and
[::1]:443 are the same listener.

So an app that does its own DNS and opens its own socket now reaches exactly
the hosts it declared, and nothing else, with no code change and no
proxy-awareness. Content-blind and access-log-free, per platform egress
doctrine.

WHAT IT DELIBERATELY IS NOT. Not a TLS terminator, not a cache, not a filter on
payloads. It holds no token and reads no secret. Its whole authority is a list
of hostnames the user's own manifest declared and the operator copied into this
pod's env.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import struct
import sys

log = logging.getLogger("cseries.app-egress")

# The declared hosts, copied by the operator from the app-runtime descriptor's
# `network` (which is the app.yaml's network[]). Empty = the app declared
# nothing, so nothing external resolves through us.
ALLOWED: frozenset[str] = frozenset(
    h.strip().lower().rstrip(".")
    for h in (os.environ.get("CSERIES_EGRESS_HOSTS") or "").split(",")
    if h.strip()
)
# The pod's REAL resolver, before dnsConfig pointed the app at us.
UPSTREAM_DNS = (os.environ.get("CSERIES_EGRESS_UPSTREAM_DNS") or "").strip()
PROXY = (os.environ.get("CSERIES_EGRESS_PROXY")
         or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "")
# The wall's credential for the node squid's app-wall port: a file holding
# `user:password`, mounted by the operator into THIS container only. The app
# shares the pod's address with the wall, so the squid cannot tell them apart
# by source; the credential is what it can tell them apart by, and the app's
# container never holds it. Read on every connection, so a rotated Secret is
# picked up without restarting the pod. Unset (an older operator, or a test)
# means no credential is sent.
PROXY_AUTH_FILE = (os.environ.get("CSERIES_EGRESS_PROXY_AUTH_FILE") or "").strip()
DNS_PORT = int(os.environ.get("CSERIES_EGRESS_DNS_PORT") or 53)
# 80/443 in the pod (the ports an app's URL implies). Overridable ONLY so the
# gate can exercise the real server unprivileged; the operator never sets it.
TCP_PORTS = tuple(int(p) for p in
                  (os.environ.get("CSERIES_EGRESS_TCP_PORTS") or "80,443").split(",") if p)

_HANDSHAKE_MAX = 8192          # a ClientHello / request head; never a payload
_PEEK_TIMEOUT_S = 15


_HOSTNAME_RE = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
                          r"(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)*")


def _host_allowed(name: str) -> bool:
    """Exact match, or a subdomain of a declared host. `api.example.com`
    declared does NOT open `example.com`; `example.com` declared DOES open
    `api.example.com`, which is how a person reads the field.

    The name must be a plain DNS hostname first. It comes from the app (an SNI
    or a Host header) and goes into the request line the wall sends the squid
    with ITS credential, so `evil.com/.pypi.org` (squid reads the authority up
    to the `/`) or an SNI carrying CRLF and a second request line would
    otherwise pass the suffix test and send the squid somewhere undeclared
    (both measured against the pinned squid)."""
    n = name.lower().rstrip(".")
    if len(n) > 253 or not _HOSTNAME_RE.fullmatch(n):
        return False
    if n in ALLOWED:
        return True
    return any(n.endswith("." + a) for a in ALLOWED)


def _proxy_auth_header() -> bytes:
    """The Proxy-Authorization line the wall sends the squid, or b"" when it
    has no credential. A configured file that cannot be read sends nothing:
    the squid then refuses the request (407), which is the safe failure."""
    if not PROXY_AUTH_FILE:
        return b""
    try:
        with open(PROXY_AUTH_FILE, "rb") as fh:
            cred = fh.read().strip()
    except OSError as e:
        log.warning("egress proxy credential unreadable (%s): sending none", e)
        return b""
    if not cred or b":" not in cred:
        log.warning("egress proxy credential file holds no user:password: sending none")
        return b""
    import base64
    return b"Proxy-Authorization: Basic " + base64.b64encode(cred) + b"\r\n"


def _proxy_target() -> tuple[str, int] | None:
    """(host, port) of the squid proxy, parsed from the proxy URL."""
    p = PROXY
    if not p:
        return None
    if "//" in p:
        p = p.split("//", 1)[1]
    p = p.rstrip("/")
    if p.startswith("["):                       # [2001:db8::238]:3128
        addr, _, rest = p[1:].partition("]")
        port = int(rest.lstrip(":") or 3128)
        return addr, port
    addr, _, port_s = p.rpartition(":")
    if not addr:
        return p, 3128
    return addr, int(port_s or 3128)


# ─────────────────────────── DNS ───────────────────────────────────────────

def _dns_question(pkt: bytes) -> tuple[str, int] | None:
    """(qname, qtype) from a DNS query, or None if it isn't parseable."""
    try:
        if len(pkt) < 12:
            return None
        qd = struct.unpack("!H", pkt[4:6])[0]
        if qd < 1:
            return None
        i, labels = 12, []
        while True:
            ln = pkt[i]
            if ln == 0:
                i += 1
                break
            if ln & 0xC0:                        # a pointer in a question: skip
                return None
            labels.append(pkt[i + 1:i + 1 + ln].decode("ascii", "ignore"))
            i += 1 + ln
        qtype = struct.unpack("!H", pkt[i:i + 2])[0]
        return ".".join(labels), qtype
    except Exception:
        return None


def _dns_local_answer(pkt: bytes, qtype: int) -> bytes:
    """Answer the query with loopback: A -> 127.0.0.1, AAAA -> ::1. Any other
    type gets NOERROR/no-answer, which is what a resolver expects for a name
    that exists in one family only."""
    tid = pkt[:2]
    flags = b"\x81\x80"                          # response, recursion available
    qend = 12
    while pkt[qend] != 0:
        qend += 1 + pkt[qend]
    qend += 5                                    # null label + qtype + qclass
    question = pkt[12:qend]
    if qtype == 1:
        rdata, rtype = socket.inet_pton(socket.AF_INET, "127.0.0.1"), 1
    elif qtype == 28:
        rdata, rtype = socket.inet_pton(socket.AF_INET6, "::1"), 28
    else:
        return tid + flags + struct.pack("!HHHH", 1, 0, 0, 0) + question
    answer = (b"\xc0\x0c" + struct.pack("!HHIH", rtype, 1, 30, len(rdata)) + rdata)
    return tid + flags + struct.pack("!HHHH", 1, 1, 0, 0) + question + answer


def _dns_servfail(pkt: bytes) -> bytes:
    """SERVFAIL, echoing the question — a real answer, so the caller fails
    immediately instead of waiting out its resolver timeout."""
    if len(pkt) < 12:
        return pkt
    return pkt[:2] + b"\x81\x82" + pkt[4:6] + b"\x00\x00\x00\x00\x00\x00" + pkt[12:]


async def _dns_forward(pkt: bytes) -> bytes | None:
    """Hand the query to the pod's real resolver, unchanged."""
    if not UPSTREAM_DNS:
        return None
    fam = socket.AF_INET6 if ":" in UPSTREAM_DNS else socket.AF_INET
    loop = asyncio.get_running_loop()
    s = socket.socket(fam, socket.SOCK_DGRAM)
    s.setblocking(False)
    try:
        await loop.sock_connect(s, (UPSTREAM_DNS, 53))
        await loop.sock_sendall(s, pkt)
        return await asyncio.wait_for(loop.sock_recv(s, 4096), timeout=5)
    except Exception as e:
        log.debug("dns upstream failed: %s", e)
        return None
    finally:
        s.close()


class _DnsProtocol(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        asyncio.create_task(self._handle(data, addr))

    async def _handle(self, data: bytes, addr):
        q = _dns_question(data)
        if q and _host_allowed(q[0]):
            self.transport.sendto(_dns_local_answer(data, q[1]), addr)
            return
        reply = await _dns_forward(data)
        # ALWAYS answer. Dropping the query makes the caller wait out its own
        # resolver timeout (5s x attempts) before it sees a failure — the app
        # looks hung rather than refused. SERVFAIL is the honest answer when
        # the pod's real resolver could not be reached, and it is what a
        # resolver expects.
        self.transport.sendto(reply if reply else _dns_servfail(data), addr)


# ─────────────────────── TCP: SNI / Host forwarding ────────────────────────

def _sni_from_client_hello(buf: bytes) -> str | None:
    """The server_name from a TLS ClientHello. Returns None if this isn't one
    (we then refuse rather than guess a destination)."""
    try:
        if len(buf) < 5 or buf[0] != 0x16:            # handshake record
            return None
        i = 5 + 4 + 2 + 32                            # hdr, hs hdr, version, random
        i += 1 + buf[i]                               # session id
        i += 2 + struct.unpack("!H", buf[i:i + 2])[0]  # cipher suites
        i += 1 + buf[i]                               # compression
        i += 2                                        # extensions length
        end = len(buf)
        while i + 4 <= end:
            etype, elen = struct.unpack("!HH", buf[i:i + 4])
            i += 4
            if etype == 0x0000:                       # server_name
                j = i + 2 + 1                         # list len + name type
                nlen = struct.unpack("!H", buf[j:j + 2])[0]
                return buf[j + 2:j + 2 + nlen].decode("ascii", "ignore")
            i += elen
    except Exception:
        return None
    return None


def _host_from_http(buf: bytes) -> str | None:
    try:
        head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1", "ignore")
        for line in head.split("\r\n")[1:]:
            k, _, v = line.partition(":")
            if k.strip().lower() == "host":
                return v.strip().split(":")[0]
    except Exception:
        return None
    return None


async def _open_via_proxy(host: str, port: int):
    """A tunnel to host:port through the node squid, using CONNECT."""
    tgt = _proxy_target()
    if tgt is None:
        raise OSError("no egress proxy configured for this pod")
    r, w = await asyncio.open_connection(tgt[0], tgt[1])
    w.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            .encode("latin-1") + _proxy_auth_header() + b"\r\n")
    await w.drain()
    status = await asyncio.wait_for(r.readline(), timeout=_PEEK_TIMEOUT_S)
    while True:                                   # drain the CONNECT headers
        line = await asyncio.wait_for(r.readline(), timeout=_PEEK_TIMEOUT_S)
        if line in (b"\r\n", b"\n", b""):
            break
    parts = status.split()
    if len(parts) < 2 or parts[1] != b"200":
        w.close()
        raise OSError(f"proxy refused CONNECT {host}:{port}: "
                      f"{status.decode('latin-1', 'ignore').strip()}")
    return r, w


def _absolute_form(head: bytes, host: str, port: int) -> bytes:
    """Rewrite a plain-HTTP request head into the form a forward proxy takes:
    `GET /path HTTP/1.1` -> `GET http://host[:port]/path HTTP/1.1`, and
    `Connection: close` (replacing any Connection / Proxy-Connection the
    client sent) so the proxy closes after ONE response — the next request
    arrives on a new connection and is checked on its own. Any
    Proxy-Authorization the app sent is dropped and the wall's own is added:
    the proxy credential is the wall's, never the app's. Returns None if
    the head is not a request line we can rewrite.

    The destination is ALWAYS the checked `host`: an absolute-form target
    keeps only its path, never its authority, because the squid goes where
    the URL says and not where the Host header says (measured: `GET
    http://undeclared/` behind `Host: <declared>` reached the undeclared host
    with the wall's credential). The head must be complete and split exactly
    as the squid splits it: a bare CR or LF, which the squid's relaxed parser
    takes as a line end, would let the app end its own head early and turn
    the wall's credential line into part of a second request it wrote."""
    try:
        idx = head.find(b"\r\n\r\n")
        if idx < 0:
            return None
        top, rest = head[:idx], head[idx + 4:]
        unpaired = top.replace(b"\r\n", b"")
        if b"\r" in unpaired or b"\n" in unpaired or b"\x00" in unpaired:
            return None
        lines = top.split(b"\r\n")
        if any(line[:1] in (b" ", b"\t") for line in lines[1:]):
            return None                            # obs-fold
        method, target, version = lines[0].split(b" ", 2)
        if not version.startswith(b"HTTP/") or b" " in version:
            return None
        lowered = target[:8].lower()
        if lowered.startswith(b"http://") or lowered.startswith(b"https://"):
            after = target.split(b"//", 1)[1]
            cuts = [i for i in (after.find(b"/"), after.find(b"?")) if i >= 0]
            path = after[min(cuts):] if cuts else b"/"
            if not path.startswith(b"/"):
                path = b"/" + path
        elif target.startswith(b"/"):
            path = target
        else:
            return None
        authority = host if port == 80 else f"{host}:{port}"
        target = b"http://" + authority.encode("idna") + path
        kept = [line for line in lines[1:]
                if line.split(b":", 1)[0].strip().lower()
                not in (b"connection", b"proxy-connection", b"proxy-authorization")]
        auth = _proxy_auth_header().rstrip(b"\r\n")
        new_top = b"\r\n".join([b" ".join((method, target, version))]
                               + kept + ([auth] if auth else []) + [b"Connection: close"])
        return new_top + b"\r\n\r\n" + rest
    except (ValueError, UnicodeError):
        return None


async def _open_plain_via_proxy():
    """A plain connection to the squid for a proxy-form HTTP request."""
    tgt = _proxy_target()
    if tgt is None:
        raise OSError("no egress proxy configured for this pod")
    return await asyncio.open_connection(tgt[0], tgt[1])


async def _pipe(reader, writer):
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _make_handler(port: int):
    """One handler per listening port. Which protocol a connection speaks is
    decided from ITS FIRST BYTE (0x16 = a TLS handshake record), not from the
    port number: an app is free to serve TLS on a non-443 port, and keying on
    the port silently sends such a connection down the HTTP path where it
    finds no Host header and gets refused."""

    async def handle(cr, cw):
        peer_ok, host = False, None
        try:
            head = await asyncio.wait_for(cr.read(_HANDSHAKE_MAX),
                                          timeout=_PEEK_TIMEOUT_S)
            if not head:
                cw.close()
                return
            is_tls = head[:1] == b"\x16"
            # A plain request head can arrive in more than one read. Collect
            # it whole (bounded), so the Host check and the rewrite see the
            # same complete head the squid will; an incomplete one is refused.
            while (not is_tls and b"\r\n\r\n" not in head
                   and len(head) < _HANDSHAKE_MAX):
                more = await asyncio.wait_for(cr.read(_HANDSHAKE_MAX - len(head)),
                                              timeout=_PEEK_TIMEOUT_S)
                if not more:
                    break
                head += more
            host = (_sni_from_client_hello(head) if is_tls
                    else _host_from_http(head))
            if not host:
                log.warning("egress refused :%d — no %s in the first bytes",
                            port, "SNI" if is_tls else "Host header")
                cw.close()
                return
            if not _host_allowed(host):
                # The wall. The app asked for a host its manifest never
                # declared; it does not get a route, and the reason is logged
                # where the user can read it.
                log.warning("egress REFUSED %s:%d — not in the app's declared "
                            "network[] (%s)", host, port,
                            ", ".join(sorted(ALLOWED)) or "none declared")
                cw.close()
                return
            if is_tls:
                sr, sw = await _open_via_proxy(host, port)
            else:
                head = _absolute_form(head, host, port)
                if head is None:
                    log.warning("egress refused %s:%d — not an HTTP request "
                                "line", host, port)
                    cw.close()
                    return
                sr, sw = await _open_plain_via_proxy()
            peer_ok = True
            sw.write(head)
            await sw.drain()
            await asyncio.gather(_pipe(cr, sw), _pipe(sr, cw))
        except Exception as e:
            if not peer_ok:
                log.warning("egress %s:%d failed: %s", host or "?", port, e)
            try:
                cw.close()
            except Exception:
                pass
    return handle


def _dual_stack_listener(port: int) -> socket.socket:
    """One listening socket that accepts BOTH [::1]:port and 127.0.0.1:port.
    asyncio.start_server(host="::") would set IPV6_V6ONLY and silently
    refuse the IPv4 half of the loopback answer the DNS side hands out."""
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    s.bind(("::", port))
    s.listen(128)
    s.setblocking(False)
    return s


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s app-egress %(levelname)s %(message)s")
    if not ALLOWED:
        log.warning("no CSERIES_EGRESS_HOSTS declared — this app reaches nothing "
                    "external, which is what an empty network[] means")
    else:
        log.info("declared network[]: %s", ", ".join(sorted(ALLOWED)))
    tgt = _proxy_target()
    log.info("egress proxy: %s | upstream dns: %s",
             f"{tgt[0]}:{tgt[1]}" if tgt else "NONE", UPSTREAM_DNS or "NONE")

    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        _DnsProtocol, local_addr=("::", DNS_PORT),
        family=socket.AF_INET6, reuse_port=False)
    servers = [await asyncio.start_server(_make_handler(p), sock=_dual_stack_listener(p))
               for p in TCP_PORTS]
    log.info("ready — dns :%d, tcp %s", DNS_PORT,
             " and ".join(f":{p}" for p in TCP_PORTS))
    await asyncio.gather(*(s.serve_forever() for s in servers))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
