#!/usr/bin/env python3
"""Gate: a user's app reaches the hosts it declared, and only those.

The defect. A user's process app runs in its own runtime pod. That pod
shipped with its egress wall explicitly deferred, so `network[]` in an app.yaml
bought the app nothing at runtime. No pod on the entity pool has external DNS —
egress goes through the node squid, which resolves — so an app worked only if
the library it happened to use read HTTP(S)_PROXY. pip and git do; ccxt does
not. Measured in the live pod: every declared host (pypi.org, github.com,
api.exchange.example) failed with "Temporary failure in name resolution", and a
trading bot died at download-data. Nothing the user built could have worked,
and nothing they could write would have fixed it.

sidecars/app-egress/egress.py is the wall. This drives the REAL server over a
real socket — a real ClientHello, a real HTTP request, a real DNS query, and a
stub CONNECT proxy standing in for squid — and asserts:

  1. a declared host resolves to loopback (A and AAAA), so any library that
     does its own DNS finds it;
  2. an UNDECLARED name is not hijacked, so *.svc.cluster.local (the app's own
     kernel URL) keeps resolving through the pod's real resolver;
  3. a TLS connection for a declared host is spliced to that host through the
     proxy, with the ClientHello delivered intact;
  4. a TLS connection for an UNDECLARED host is refused and never reaches the
     proxy — the wall, enforced outside the app;
  5. the same for plain HTTP, keyed on the Host header;
  6. a subdomain of a declared host is allowed; a parent of one is NOT
     (declaring api.exchange.example must not open exchange.example);
  7. a connection whose first bytes name no host at all is refused rather than
     guessed at.

BINDS: every socket this gate opens is an in-process test double (a stub squid,
a stub resolver, and the client side of each probe). They are loopback ON
PURPOSE — nothing off this box may reach them, and the gate must not open a
port to the network to run. The shim under test binds `::`, which is what the
pod needs; that bind is asserted in check 0 below rather than assumed.

No cluster, no pod, no root: the gate binds high ports via
CSERIES_EGRESS_TCP_PORTS. Prints a single OK…/FAIL… last line.
"""
import asyncio
import os
import socket
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gate_loopback import await_peer_close  # noqa: E402

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
EGRESS = _ROOT / "sidecars/app-egress/egress.py"
LO = "::1"          # test doubles only — see the BINDS note in the docstring

FAILED: list[str] = []
# Every shim the gate started; the exit path stops any still running, even when a
# check raises before main() reaches its own stop.
SHIMS: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[app-egress] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[app-egress] FAIL {label}" + (f" — {detail}" if detail else ""))


def free_ports(n: int) -> list:
    """n ports free for the shim's own binds: dual-stack "::" for both TCP and UDP,
    as the shim binds them. Only a probe: in the CI pool another gate can take a
    port before the shim binds it, which start_shim answers by starting again."""
    out = []
    while len(out) < n:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        try:
            s.bind(("::", 0))
            p = s.getsockname()[1]
        finally:
            s.close()
        u = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        u.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        try:
            u.bind(("::", p))
        except OSError:
            continue
        finally:
            u.close()
        if p not in out:
            out.append(p)
    return out


async def start_shim(proxy_port: int, first_ports=None, tries: int = 3, extra_env=None):
    """Start the egress shim; if it exits because a port was taken between the
    probe and its own bind (seen in CI), start it again on fresh
    ports. Returns (proc, (dns, http, tls), attempts, output of the last failure)."""
    out, attempt = "", 0
    for attempt in range(1, tries + 1):
        ports = first_ports if (attempt == 1 and first_ports) else free_ports(3)
        dns_port, http_port, tls_port = ports
        env = {
            **os.environ,
            "CSERIES_EGRESS_HOSTS": "api.exchange.example, pypi.org",
            "CSERIES_EGRESS_PROXY": f"http://[{LO}]:{proxy_port}",
            # No upstream resolver in the gate: an undeclared name must then get
            # NO local answer, which is exactly what check 2 asserts.
            "CSERIES_EGRESS_UPSTREAM_DNS": "",
            "CSERIES_EGRESS_DNS_PORT": str(dns_port),
            "CSERIES_EGRESS_TCP_PORTS": f"{http_port},{tls_port}",
            **(extra_env or {}),
        }
        proc = subprocess.Popen([sys.executable, str(EGRESS)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        SHIMS.append(proc)
        for _ in range(80):
            if proc.poll() is not None:
                break
            try:
                # Short: a port held by something that never answers must not cost 5 s a probe.
                await udp_ask(dns_port, dns_query("api.exchange.example"), timeout=0.3)
                return proc, (dns_port, http_port, tls_port), attempt, ""
            except Exception:
                await asyncio.sleep(0.1)
        if proc.poll() is None:
            proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        proc.wait()
        if "Address already in use" not in out:
            break
    return None, None, attempt, out


def client_hello(server_name: str) -> bytes:
    """A real, minimal TLS 1.2 ClientHello carrying one SNI extension."""
    sni = server_name.encode()
    ext_sni = (b"\x00\x00" + struct.pack("!H", len(sni) + 5)
               + struct.pack("!H", len(sni) + 3) + b"\x00"
               + struct.pack("!H", len(sni)) + sni)
    body = (b"\x03\x03" + b"\xAA" * 32 + b"\x00"          # version, random, sid
            + struct.pack("!H", 2) + b"\x00\x2f"          # cipher suites
            + b"\x01\x00"                                 # compression
            + struct.pack("!H", len(ext_sni)) + ext_sni)
    hs = b"\x01" + struct.pack("!I", len(body))[1:] + body
    return b"\x16\x03\x01" + struct.pack("!H", len(hs)) + hs


class StubProxy:
    """Stands in for node squid. CONNECT (the :443 path): records the target,
    answers 200, echoes the spliced head. Absolute-form request (the :80
    path — squid denies CONNECT to anything but :443, so plain HTTP must
    arrive as `GET http://host/path`): records the URL's authority, the
    method, and the request headers, answers 200 with a short body.

    After its answer it waits for the shim to hang up before it closes, so the
    shim's connection is closed from the shim's side first and the TIME_WAIT
    is not left on this server's port (scripts/gate_loopback.py)."""

    def __init__(self):
        self.targets: list[str] = []
        self.methods: list[str] = []
        self.headers: list[list[str]] = []

    async def start(self):
        self.server = await asyncio.start_server(self._handle, LO, 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, r, w):
        try:
            line = await asyncio.wait_for(r.readline(), timeout=5)
            method, target = line.split()[0].decode(), line.split()[1].decode()
            self.methods.append(method)
            hdrs = []
            while True:
                h = await asyncio.wait_for(r.readline(), timeout=5)
                if h in (b"\r\n", b"\n", b""):
                    break
                hdrs.append(h.decode("latin-1").strip())
            self.headers.append(hdrs)
            if method == "CONNECT":
                self.targets.append(target)
                w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await w.drain()
                payload = await asyncio.wait_for(r.read(4096), timeout=5)
                w.write(b"ECHO:" + payload[:16])       # proves the head was spliced
                await w.drain()
            else:
                self.targets.append(target.split("//", 1)[1].split("/", 1)[0])
                w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n"
                        b"Connection: close\r\n\r\nPONG")
                await w.drain()
        except Exception:
            pass
        finally:
            try:
                await await_peer_close(r)
                w.close()
            except Exception:
                pass


def dns_query(name: str, qtype: int = 1) -> bytes:
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    return (b"\x12\x34" + b"\x01\x00" + struct.pack("!HHHH", 1, 0, 0, 0)
            + q + struct.pack("!HH", qtype, 1))


async def udp_ask(port: int, payload: bytes, timeout: float = 5) -> bytes:
    loop = asyncio.get_running_loop()
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    s.setblocking(False)
    try:
        await loop.sock_connect(s, (LO, port))
        await loop.sock_sendall(s, payload)
        return await asyncio.wait_for(loop.sock_recv(s, 4096), timeout=timeout)
    finally:
        s.close()


def answer_ips(reply: bytes) -> list[str]:
    """Every A/AAAA rdata in a response, as text."""
    an = struct.unpack("!H", reply[6:8])[0]
    i = 12
    while reply[i] != 0:
        i += 1 + reply[i]
    i += 5
    out = []
    for _ in range(an):
        if reply[i] & 0xC0:
            i += 2
        else:
            while reply[i] != 0:
                i += 1 + reply[i]
            i += 1
        rtype, _, _, rdlen = struct.unpack("!HHIH", reply[i:i + 10])
        i += 10
        rd = reply[i:i + rdlen]
        i += rdlen
        if rtype == 1:
            out.append(socket.inet_ntop(socket.AF_INET, rd))
        elif rtype == 28:
            out.append(socket.inet_ntop(socket.AF_INET6, rd))
    return out


async def main() -> int:
    src = EGRESS.read_text(encoding="utf-8")
    check("the shim binds :: (reachable in the pod), not loopback",
          'local_addr=("::", DNS_PORT)' in src and 's.bind(("::", port))' in src)
    check("the TCP listeners are dual-stack (asyncio's own :: bind sets "
          "IPV6_V6ONLY and refuses the A-record half of the answer)",
          "IPV6_V6ONLY, 0" in src)
    check("in the pod it listens on 80 and 443 by default",
          '"CSERIES_EGRESS_TCP_PORTS") or "80,443"' in src)
    check("protocol is decided by the first byte, not the port number",
          'is_tls = head[:1] == b"\\x16"' in src)

    proxy = StubProxy()
    await proxy.start()

    # The gate's own start survives a port taken between probe and bind: hold the
    # DNS port the first attempt is given, and the shim must come up on a later one.
    # At least 2, not exactly 2: the second attempt can lose the same race in the
    # CI pool, and the retry starting the shim on its third must not fail the gate.
    held = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    held.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    held.bind(("::", 0))
    taken = [held.getsockname()[1]] + free_ports(2)
    p2, _, tries2, out2 = await start_shim(proxy.port, first_ports=taken)
    held.close()
    check("a port taken before the shim binds it: the gate starts the shim again on fresh ports",
          p2 is not None and tries2 >= 2, (tries2, out2[-300:]))
    if p2 is not None:
        p2.kill()
        p2.wait()

    proc, ports, _tries, out = await start_shim(proxy.port)
    if proc is None:
        # The end of the output carries the exception; its start is only the banner.
        check("the egress shim starts", False, out[-600:])
        print("FAIL: app egress wall — the shim did not start")
        return 1
    dns_port, http_port, tls_port = ports
    check("the egress shim starts and serves DNS", True)

    # ── 1. a declared host resolves to loopback ────────────────────────────
    a = answer_ips(await udp_ask(dns_port, dns_query("api.exchange.example", 1)))
    check("declared host: A -> 127.0.0.1", a == ["127.0.0.1"], repr(a))
    aaaa = answer_ips(await udp_ask(dns_port, dns_query("api.exchange.example", 28)))
    check("declared host: AAAA -> ::1", aaaa == ["::1"], repr(aaaa))

    # ── 2. an undeclared name is NOT hijacked, and IS answered ─────────────
    # With no upstream reachable the honest answer is SERVFAIL. Dropping it
    # would make the app hang on its own resolver timeout instead of failing.
    r = await udp_ask(dns_port, dns_query("kubernetes.default.svc.cluster.local", 1))
    check("undeclared name is not hijacked to loopback",
          answer_ips(r) == [], repr(answer_ips(r)))
    check("...and it is ANSWERED (SERVFAIL), never dropped — a dropped query "
          "makes the app hang rather than fail", (r[3] & 0x0F) == 2, hex(r[3]))

    # ── 6. subdomain yes, parent no ────────────────────────────────────────
    sub = answer_ips(await udp_ask(dns_port, dns_query("files.pypi.org", 1)))
    check("a subdomain of a declared host resolves", sub == ["127.0.0.1"], repr(sub))
    parent = answer_ips(await udp_ask(dns_port, dns_query("exchange.example", 1)))
    check("a PARENT of a declared host does NOT resolve (declaring "
          "api.exchange.example must not open exchange.example)", parent == [], repr(parent))

    # ── 3. TLS for a declared host is spliced through the proxy ────────────
    before = len(proxy.targets)
    r_, w_ = await asyncio.open_connection(LO, tls_port)
    w_.write(client_hello("api.exchange.example"))
    await w_.drain()
    echoed = await asyncio.wait_for(r_.read(64), timeout=8)
    w_.close()
    # The tunnel target is host:<the port the app dialled>, which in the pod is
    # 443; the gate listens on a random high port, so assert against that.
    check("TLS to a declared host reaches the proxy",
          len(proxy.targets) > before
          and proxy.targets[-1] == f"api.exchange.example:{tls_port}",
          repr(proxy.targets[-3:]))
    check("...and the ClientHello is delivered intact (not swallowed)",
          echoed.startswith(b"ECHO:\x16\x03\x01"), repr(echoed[:20]))

    # ── 3b. the SAME over IPv4 — the A record says 127.0.0.1, so an IPv4-only
    # client (or one that tries the A answer first) must reach the wall too.
    # Measured: asyncio's `::` listener refused this outright.
    before = len(proxy.targets)
    try:
        r_, w_ = await asyncio.open_connection("127.0.0.1", tls_port)
        w_.write(client_hello("api.exchange.example"))
        await w_.drain()
        echoed4 = await asyncio.wait_for(r_.read(64), timeout=8)
        w_.close()
    except OSError as e:
        echoed4 = repr(e).encode()
    check("TLS over IPv4 loopback (127.0.0.1, the A answer) reaches the proxy",
          len(proxy.targets) > before and echoed4.startswith(b"ECHO:\x16"),
          repr(echoed4[:60]))

    # ── 4. TLS for an UNDECLARED host is refused, proxy never sees it ──────
    before = len(proxy.targets)
    r_, w_ = await asyncio.open_connection(LO, tls_port)
    w_.write(client_hello("evil.example.com"))
    w_.write_eof()      # the refusal closes the connection at once; our FIN goes first
    await w_.drain()
    got = await asyncio.wait_for(r_.read(64), timeout=8)
    w_.close()
    check("TLS to an UNDECLARED host is refused", got == b"", repr(got[:20]))
    check("...and the proxy never saw it (the wall is outside the app)",
          len(proxy.targets) == before, repr(proxy.targets[-3:]))

    # ── 5. plain HTTP, keyed on Host ───────────────────────────────────────
    before = len(proxy.targets)
    r_, w_ = await asyncio.open_connection(LO, http_port)
    w_.write(b"GET /simple/ HTTP/1.1\r\nHost: pypi.org\r\nConnection: keep-alive\r\n\r\n")
    await w_.drain()
    body = await asyncio.wait_for(r_.read(256), timeout=8)
    w_.close()
    check("HTTP to a declared host reaches the proxy on :80",
          len(proxy.targets) > before
          and proxy.targets[-1] == f"pypi.org:{http_port}", repr(proxy.targets[-3:]))
    # The squid denies CONNECT to !443, so :80 must arrive as an ordinary
    # proxy request, one per connection.
    check("...as an absolute-form proxy request, NOT a CONNECT",
          proxy.methods[-1] == "GET", repr(proxy.methods[-1:]))
    check("...with Connection: close (one checked request per connection)",
          any(h.lower() == "connection: close" for h in proxy.headers[-1])
          and not any(h.lower().startswith("connection: keep-alive")
                      for h in proxy.headers[-1]), repr(proxy.headers[-1]))
    check("...and the response comes back to the app", body.endswith(b"PONG"), repr(body[-40:]))

    before = len(proxy.targets)
    r_, w_ = await asyncio.open_connection(LO, http_port)
    w_.write(b"GET / HTTP/1.1\r\nHost: evil.example.com\r\n\r\n")
    w_.write_eof()      # as above
    await w_.drain()
    got = await asyncio.wait_for(r_.read(64), timeout=8)
    w_.close()
    check("HTTP to an UNDECLARED host is refused",
          got == b"" and len(proxy.targets) == before, repr(got[:20]))

    # ── 5b. the app cannot steer the wall's request elsewhere ──────────────
    # The wall's request carries the squid credential and the
    # app's own :3128 is gone, so each of these, measured against the pinned
    # squid, reached an UNDECLARED host before the fix.
    async def http_raw(chunks, port=None):
        r_, w_ = await asyncio.open_connection(LO, port or http_port)
        try:
            for c in chunks:
                w_.write(c)
                await w_.drain()
                await asyncio.sleep(0.05)
            w_.write_eof()
        except OSError:
            pass                    # the wall already hung up: a refusal
        got_ = await asyncio.wait_for(r_.read(256), timeout=8)
        w_.close()
        return got_

    before = len(proxy.targets)
    await http_raw([b"GET http://evil.example.com/x HTTP/1.1\r\nHost: pypi.org\r\n\r\n"])
    check("an absolute-form URL naming an undeclared host goes to the CHECKED host",
          len(proxy.targets) > before and proxy.targets[-1] == f"pypi.org:{http_port}",
          repr(proxy.targets[-3:]))
    before = len(proxy.targets)
    got = await http_raw([b"GET / HTTP/1.1\r\nHost: pypi.org\r\nX: a\n\n"
                          b"GET http://evil.example.com/ HTTP/1.1\nHost: evil.example.com\r\n\r\n"])
    check("a head with a bare LF (a second request hidden from the wall) is refused",
          got == b"" and len(proxy.targets) == before, repr(proxy.targets[-3:]))
    before = len(proxy.targets)
    got = await http_raw([b"GET / HTTP/1.1\r\nHost: evil.example.com/.pypi.org\r\n\r\n"])
    check("a Host value that is not a hostname is refused",
          got == b"" and len(proxy.targets) == before, repr(proxy.targets[-3:]))
    before = len(proxy.targets)
    got = await http_raw([client_hello("evil.example.com:443 HTTP/1.1\r\nX: .pypi.org")],
                         port=tls_port)
    check("an SNI carrying CRLF and a declared suffix is refused",
          got == b"" and len(proxy.targets) == before, repr(proxy.targets[-3:]))
    before = len(proxy.targets)
    await http_raw([b"GET /split HTTP/1.1\r\nHo", b"st: pypi.org\r\n\r\n"])
    check("a request head split across reads is still forwarded whole",
          len(proxy.targets) > before and proxy.targets[-1] == f"pypi.org:{http_port}",
          repr(proxy.targets[-3:]))

    # ── 7. no host in the first bytes = refuse, never guess ────────────────
    before = len(proxy.targets)
    r_, w_ = await asyncio.open_connection(LO, tls_port)
    w_.write(b"\x00\x01\x02\x03garbage")
    w_.write_eof()      # as above
    await w_.drain()
    got = await asyncio.wait_for(r_.read(64), timeout=8)
    w_.close()
    check("a connection naming no host is refused, not guessed",
          got == b"" and len(proxy.targets) == before, repr(got[:20]))

    # ── 8. the app's own Proxy-Authorization never reaches the proxy ───────
    r_, w_ = await asyncio.open_connection(LO, http_port)
    w_.write(b"GET / HTTP/1.1\r\nHost: pypi.org\r\nProxy-Authorization: Basic YXBwOmd1ZXNz\r\n\r\n")
    await w_.drain()
    await asyncio.wait_for(r_.read(256), timeout=8)
    w_.close()
    check("a Proxy-Authorization the app sends is dropped, and with no credential "
          "configured none is sent",
          not any(h.lower().startswith("proxy-authorization") for h in proxy.headers[-1]),
          repr(proxy.headers[-1]))

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    # ── 9. the wall presents ITS credential ────────────────────────────────
    # The squid's app-wall port serves only a client with the wall's
    # credential; the operator mounts it into the wall container alone.
    import base64
    import tempfile
    cred_dir = tempfile.mkdtemp(prefix="wall-cred-")
    cred = Path(cred_dir) / "auth"
    cred.write_text("app-wall:s3cret\n")
    want = "Proxy-Authorization: Basic " + base64.b64encode(b"app-wall:s3cret").decode()
    proc, ports, _tries, out = await start_shim(
        proxy.port, extra_env={"CSERIES_EGRESS_PROXY_AUTH_FILE": str(cred)})
    if proc is None:
        check("the shim starts with a credential file", False, out[-600:])
    else:
        _dns, http_port, tls_port = ports
        r_, w_ = await asyncio.open_connection(LO, tls_port)
        w_.write(client_hello("pypi.org"))
        await w_.drain()
        await asyncio.wait_for(r_.read(64), timeout=8)
        w_.close()
        check("CONNECT carries the wall's Proxy-Authorization",
              proxy.methods[-1] == "CONNECT" and want in proxy.headers[-1], repr(proxy.headers[-1]))
        r_, w_ = await asyncio.open_connection(LO, http_port)
        w_.write(b"GET / HTTP/1.1\r\nHost: pypi.org\r\nProxy-Authorization: Basic YXBwOmd1ZXNz\r\n\r\n")
        await w_.drain()
        await asyncio.wait_for(r_.read(256), timeout=8)
        w_.close()
        auth_lines = [h for h in proxy.headers[-1] if h.lower().startswith("proxy-authorization")]
        check("a plain request carries the wall's credential and not the app's",
              auth_lines == [want], repr(proxy.headers[-1]))
        cred.write_text("app-wall:rotated\n")
        r_, w_ = await asyncio.open_connection(LO, tls_port)
        w_.write(client_hello("pypi.org"))
        await w_.drain()
        await asyncio.wait_for(r_.read(64), timeout=8)
        w_.close()
        check("a rotated credential is used on the next connection, no restart",
              ("Proxy-Authorization: Basic " + base64.b64encode(b"app-wall:rotated").decode())
              in proxy.headers[-1], repr(proxy.headers[-1]))
        cred.unlink()
        r_, w_ = await asyncio.open_connection(LO, tls_port)
        w_.write(client_hello("pypi.org"))
        await w_.drain()
        await asyncio.wait_for(r_.read(64), timeout=8)
        w_.close()
        check("a credential file that is gone sends none (the proxy then refuses: fail closed)",
              not any(h.lower().startswith("proxy-authorization") for h in proxy.headers[-1]),
              repr(proxy.headers[-1]))
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    if FAILED:
        print(f"FAIL: app egress wall — {len(FAILED)} check(s): " + "; ".join(FAILED))
        return 1
    print("OK: app egress wall — a declared host resolves and connects; an "
          "undeclared one never reaches the proxy")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    finally:
        for p in SHIMS:
            if p.poll() is None:
                p.kill()
                p.wait()
