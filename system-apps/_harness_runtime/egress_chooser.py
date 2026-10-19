"""egress_chooser — route a harness's outbound requests per DESTINATION.

WHY THIS EXISTS. The operator hands every harness pod two ways out: its own
node's squid in HTTP(S)_PROXY, and the user's signed-rule NAC gate in
NAC_EGRESS_LANE. Only the Browser consumed the lane, so a destination the user
had DENIED in a signed rule was still reachable from a coding-agent run while
the Network app said otherwise.

Sending EVERYTHING to the lane fixes that and breaks something worse. The lane
lives in the entity pod, so every byte would be pumped by the kernel's own
asyncio loop, and it would leave from the entity node's ONE shared IPv4 against
a per-pod cap sized for a single workload — `pod_conn_cap maxconn 64` in
manifests/0012, whose comment records the measurement that motivated it: 20 pods
tripped a public endpoint's 429 wall in under 5 seconds. Eight harnesses' worth
of npm and pip collapsed into one pod's bucket is that failure, and squid answers
it with a 403 that no log explains, because both proxies run `access_log none`.

So the routing is INVERTED rather than replaced. This listens on loopback, reads
just the destination out of each request head, and:

  * sends it to the LANE, with the per-subject Basic credential, so the user's
    signed rules decide — that is the whole point of the change; and
  * on `421 Misdirected Request` from the gate, which means "no signed rule names
    this destination and you said you can route it yourself", retries once
    against the node squid and remembers the answer for that host.

The first request to a host costs one extra round trip. After that the verdict is
cached for the life of the sidecar. A DENY still arrives as the gate's own 403
and is NOT retried — falling back on a deny would be the bug this exists to stop.

WHAT THIS IS NOT. It is not an enforcement boundary. A harness runs third-party
vendor code that could ignore its proxy env entirely and dial the node squid
direct; that was true before this file existed and is unchanged by it. What
enforces is the Cilium policy on the pod and the squid ACLs. This makes the
user's rules APPLY to an agent that behaves.

Byte-transparent: it parses the request head to find a hostname and copies
everything else through untouched. It never reads a response body and never
decrypts anything — on CONNECT it reads one status line and then splices.
"""
from __future__ import annotations

import base64
import logging
import os
import socket
import threading
import time
from urllib.parse import urlsplit

log = logging.getLogger("cseries.egress-chooser")

#: Tells the gate we can reach the destination ourselves if no rule names it.
#: Opt-in on purpose: the Browser shares this lane and cannot act on a 421.
FALLBACK_HEADER = b"X-CSeries-Egress-Fallback: 1"

_HEAD_LIMIT = 65536
_CONNECT_TIMEOUT_S = 20

#: host -> (goes_via_lane, decided_at). A verdict EXPIRES, because the user can
#: add a deny mid-session and a harness pod outlives a run: release patches
#: replicas to 0, but a pod stays up across back-to-back runs, and the idle sweep
#: is a 900s crash backstop rather than a lifetime. Caching for the pod's life
#: would mean a new deny does not apply until the pod goes away, which is the one
#: thing this whole change exists to guarantee. The gate re-reads its own rules
#: every 5s (nac_egress._RULES_TTL_SECONDS), so 60s here bounds deny latency to
#: about a minute at a cost of one CONNECT round trip per host per minute.
VERDICT_TTL_S = 60.0
_verdict: dict[str, tuple[bool, float]] = {}
_verdict_lock = threading.Lock()


#: Sweep at this size rather than on a timer: a thread would outlive the runs it
#: serves and there is nothing else in this module that needs one. A build touches
#: tens of hosts, so this is never reached in normal use — it exists so a run that
#: walks a large registry cannot grow the dict without bound.
_VERDICT_SWEEP_AT = 4096


def _remember(host: str | None, via_lane: bool) -> None:
    if not host:
        return
    now = time.monotonic()
    with _verdict_lock:
        if len(_verdict) >= _VERDICT_SWEEP_AT:
            # Expiry is otherwise on read, so a host asked about once and never
            # again is never collected.
            for k in [k for k, (_, at) in _verdict.items()
                      if now - at > VERDICT_TTL_S]:
                del _verdict[k]
        _verdict[host] = (via_lane, now)


def _recall(host: str | None) -> bool | None:
    """The cached verdict, or None if absent or stale."""
    if not host:
        return None
    with _verdict_lock:
        hit = _verdict.get(host)
        if not hit:
            return None
        via_lane, at = hit
        if time.monotonic() - at > VERDICT_TTL_S:
            del _verdict[host]
            return None
        return via_lane


def _host_of(head: bytes) -> str | None:
    """The destination host from a proxy request head, CONNECT or absolute-form."""
    line = head.split(b"\r\n", 1)[0].split(b"\n", 1)[0]
    parts = line.split(b" ")
    if len(parts) < 2:
        return None
    method, target = parts[0].upper(), parts[1]
    try:
        if method == b"CONNECT":                       # host:port
            # latin-1, NOT the idna codec. Two reasons: the idna codec rejects
            # every error handler except 'strict' (so `decode("idna","replace")`
            # raises UnicodeError and this returned None for EVERY host, which
            # silently disabled the cache), and the gate matches the WIRE form —
            # `_host_matches` lowercases and compares ASCII, so decoding punycode
            # here would produce a name its rules could never match.
            return target.rsplit(b":", 1)[0].strip(b"[]").decode("latin-1").lower()
        if b"://" in target:                           # absolute-form
            h = urlsplit(target.decode("latin-1")).hostname
            return h.lower() if h else None
    except (ValueError, UnicodeError):
        return None
    # origin-form through a proxy is unusual; fall back to the Host header.
    for ln in head.split(b"\r\n"):
        if ln[:5].lower().startswith(b"host:"):
            return ln.split(b":", 1)[1].strip().rsplit(b":", 1)[0].decode("latin-1").lower()
    return None


def _inject(head: bytes, cred: str) -> bytes:
    """Add the lane credential and the fallback advertisement to a head."""
    line, sep, rest = head.partition(b"\r\n")
    if not sep:
        return head
    auth = b"Proxy-Authorization: Basic " + base64.b64encode(cred.encode())
    return line + b"\r\n" + auth + b"\r\n" + FALLBACK_HEADER + b"\r\n" + rest


def _dial(hostport: tuple[str, int]) -> socket.socket:
    s = socket.create_connection(hostport, timeout=_CONNECT_TIMEOUT_S)
    s.settimeout(None)
    return s


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


def _splice(client: socket.socket, up: socket.socket, first: bytes = b"") -> None:
    if first:
        try:
            client.sendall(first)
        except OSError:
            up.close()
            client.close()
            return
    threading.Thread(target=_pump, args=(client, up), daemon=True).start()
    threading.Thread(target=_pump, args=(up, client), daemon=True).start()


def _read_head(sock: socket.socket) -> bytes:
    buf = b""
    while b"\r\n\r\n" not in buf and b"\n\n" not in buf:
        if len(buf) > _HEAD_LIMIT:
            return buf
        try:
            d = sock.recv(4096)
        except OSError:
            return buf
        if not d:
            return buf
        buf += d
    return buf


def _status(sock: socket.socket) -> tuple[int, bytes]:
    """First status line of a proxy response, plus everything read with it."""
    buf = b""
    while b"\r\n" not in buf:
        if len(buf) > 8192:
            break
        try:
            d = sock.recv(4096)
        except OSError:
            return 0, buf
        if not d:
            return 0, buf
        buf += d
    try:
        return int(buf.split(b" ")[1]), buf
    except (IndexError, ValueError):
        return 0, buf


def start(lane: tuple[str, int], node_proxy: tuple[str, int], cred: str) -> int:
    """Listen on loopback and route per destination. Returns the bound port.

    LOOPBACK IS CORRECT HERE, and it is the one bind in this tree that should be.
    The only client is the vendor CLI this sidecar launches with subprocess.Popen
    — same container, same network namespace — and the proxy env we hand it names
    this port. Nothing off-box, off-pod or off-container reaches it, and binding
    it wider would publish an authenticated egress path to the whole pod network.
    `cli/cseries_sidecar.py::_bridge_ipv6_proxy` binds 127.0.0.1 for the same
    reason and is the working reference.
    """
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(128)
    lport = lsock.getsockname()[1]

    def handle(client: socket.socket) -> None:
        head = _read_head(client)
        if not head:
            client.close()
            return
        host = _host_of(head)

        def direct(why: str = "") -> None:
            try:
                d = _dial(node_proxy)
                d.sendall(head)          # the ORIGINAL head: no credential, no flag
                _splice(client, d)
            except OSError:
                if why:
                    log.warning("egress: %s and the node proxy is unreachable — "
                                "this request fails", why)
                client.close()

        if _recall(host) is False:              # settled: this host goes direct
            direct()
            return

        try:
            up = _dial(lane)
            up.sendall(_inject(head, cred))
        except OSError:
            # The lane lives in the entity pod; if it is rolling, do not strand
            # the run. Say so: on this path the user's rules do not apply.
            log.warning("egress: the gate is unreachable, falling back to the node "
                        "proxy — signed egress rules do NOT apply to this request")
            direct("the gate is unreachable")
            return

        code, first = _status(up)
        if code == 421:
            # "Nothing pinned this destination, and you said you can route it."
            # Unconditional: a stale `lane` verdict must never forward this
            # sentinel to npm or pip, which would read it as an HTTP error.
            up.close()
            _remember(host, False)
            direct()
            return
        if code == 407:
            # A rotated or wrong token. The no-token path warns and uses the node
            # proxy (harness_core); this is the same situation and gets the same
            # answer, rather than 407ing every request for the pod's life.
            up.close()
            log.warning("egress: the gate rejected our credential (407), falling back "
                        "to the node proxy — signed egress rules do NOT apply")
            _remember(host, False)
            direct()
            return
        _remember(host, True)
        _splice(client, up, first)

    def accept() -> None:
        while True:
            try:
                c, _ = lsock.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    threading.Thread(target=accept, daemon=True, name="egress-chooser").start()
    return lport
