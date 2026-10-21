#!/usr/bin/env python3
"""The egress gate's app lane never dials a private or reserved address
(server/network/nac_egress.py, EgressProxy._direct_dispatch).

The app lane (:13129) is how the local runtime's containers reach the
internet: the browser, the process apps' egress wall, the helpers, the coding
engines' named destinations. Each presents its own subject's credential, and
the owner's signed rules decide. The lane then dialled whatever it was given.
On a cloud box the box's firewall drops private ranges for every container,
but in the WSL guest nothing does, so a page in the browser could have had the
Host reach into the owner's own network. Now a destination that resolves to
any non-global address is refused 403 before anything is dialled, and the
dial goes to the address that was checked (a name cannot resolve again to
something else between the check and the connection).

  * CONNECT and absolute-form requests to 10.0.0.1, 192.168.1.1,
    169.254.169.254, [::1], [fc00::1], [::ffff:10.0.0.1], 0.0.0.0, a name
    resolving to 127.0.0.1, and a name with one public and one private
    address: 403, and nothing dialled;
  * a destination outside every private range is dialled, at the address
    that was checked (CSERIES_EGRESS_TEST_NETS lets a test count a local
    range as outside);
  * the loopback gate (the Host's own traffic) is unchanged, and the app lane
    still asks for its subject's credential first;
  * NAT64: an address in 64:ff9b::/96 is judged by the IPv4 address in its last
    four bytes (64:ff9b::a9fe:a9fe is 169.254.169.254, refused; 64:ff9b::808:808
    is outside), and the rest of 64:ff9b::/32, the local-use 64:ff9b:1::/48
    among it, is refused outright, as a name resolving there is;
  * a request a signed rule pins to a bastion meets the same floor: a private
    destination is refused 403 and never handed to the bastion, and a public one
    is handed over as the address that was checked (a CONNECT to it; a plain
    HTTP request down a CONNECT tunnel to it, in origin form), so the bastion
    cannot resolve the name to something else. The loopback gate's pins are
    unchanged.

Container-free: python3 scripts/test_egress_lane_private_refused.py
"""
import asyncio
import base64
import hashlib
import hmac
import os
import socket
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "CSERIES_EGRESS_TEST_NETS"):
    os.environ.pop(k, None)

from server.network import nac_egress as NE  # noqa: E402

FAILURES = []


def check(cond, name, detail=""):
    if cond:
        print(f"OK    {name}")
    else:
        print(f"FAIL  {name} {detail}")
        FAILURES.append(name)


SECRET = b"s" * 32
TOKEN = hmac.new(SECRET, b"nac-egress-subject:app:demo", hashlib.sha256).hexdigest()
AUTH = b"Proxy-Authorization: Basic " + base64.b64encode(f"app:demo:{TOKEN}".encode()) + b"\r\n"

NAMES = {"internal.example.test": ["127.0.0.1"],
         "split.example.test": ["93.184.215.14", "10.1.2.3"],
         "public.example.test": ["127.0.0.1"],
         "nat64.example.test": ["64:ff9b::a9fe:a9fe"],
         "site.example.test": ["93.184.215.14"]}
resolved = []


async def fake_resolve(host, port):
    resolved.append(host)
    return list(NAMES.get(host, [host]))


# The gate's own resolver (nac_egress._resolve) is replaced so no real DNS
# is asked; an address resolves to itself, as getaddrinfo answers.
NE._resolve = fake_resolve


async def no_rules():
    return []


async def main():
    hits = []

    async def target(reader, writer):
        hits.append(writer.get_extra_info("peername"))
        writer.write(b"pong")
        await writer.drain()
        writer.close()
    tsrv = await asyncio.start_server(target, "127.0.0.1", 0)
    tport = tsrv.sockets[0].getsockname()[1]

    lane = NE.EgressProxy(None, 9001, None, app_lane=True)
    lane._app_secret = SECRET
    lane._rules_now = no_rules
    lsrv = await asyncio.start_server(lane._handle, "127.0.0.1", 0)
    lport = lsrv.sockets[0].getsockname()[1]
    gate = NE.EgressProxy(None, 9001, None, app_lane=False)
    gate._rules_now = no_rules
    gsrv = await asyncio.start_server(gate._handle, "127.0.0.1", 0)
    gport = gsrv.sockets[0].getsockname()[1]

    async def ask(port, head):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(head)
        await w.drain()
        try:
            line = await asyncio.wait_for(r.readline(), timeout=10)
        except asyncio.TimeoutError:
            line = b"no answer within 10 s (the gate is dialling it)"
        w.close()
        return line.decode("latin-1").strip()

    try:
        for dest in ("10.0.0.1:443", "192.168.1.1:443", "169.254.169.254:443", "[::1]:443",
                     "[fc00::1]:443", "[::ffff:10.0.0.1]:443", "internal.example.test:443",
                     "split.example.test:443", "0.0.0.0:443"):
            line = await ask(lport, f"CONNECT {dest} HTTP/1.1\r\nHost: {dest}\r\n".encode() + AUTH + b"\r\n")
            check(line.startswith("HTTP/1.1 403"), f"the app lane refuses CONNECT {dest}", line)
        line = await ask(lport, b"GET http://169.254.169.254/latest/meta-data HTTP/1.1\r\n"
                                b"Host: 169.254.169.254\r\n" + AUTH + b"\r\n")
        check(line.startswith("HTTP/1.1 403"), "the app lane refuses an absolute-form request to metadata", line)
        line = await ask(lport, b"GET http://internal.example.test/ HTTP/1.1\r\nHost: internal.example.test\r\n"
                         + AUTH + b"\r\n")
        check(line.startswith("HTTP/1.1 403"), "and to a name that resolves to loopback", line)
        check(not hits, "nothing private was dialled", hits)

        os.environ["CSERIES_EGRESS_TEST_NETS"] = "127.0.0.0/8"
        resolved.clear()
        line = await ask(lport, f"CONNECT public.example.test:{tport} HTTP/1.1\r\n"
                                f"Host: public.example.test:{tport}\r\n".encode() + AUTH + b"\r\n")
        check(line.startswith("HTTP/1.1 200") and hits and hits[-1][0] == "127.0.0.1" and
              resolved == ["public.example.test"],
              "a destination outside every private range is dialled, at the address that was checked",
              (line, hits, resolved))
        os.environ.pop("CSERIES_EGRESS_TEST_NETS")

        n = len(hits)
        line = await ask(gport, f"CONNECT 127.0.0.1:{tport} HTTP/1.1\r\nHost: 127.0.0.1:{tport}\r\n\r\n".encode())
        check(line.startswith("HTTP/1.1 200") and len(hits) == n + 1,
              "the loopback gate (the Host's own traffic) is unchanged", line)
        line = await ask(lport, f"CONNECT 127.0.0.1:{tport} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        check(line.startswith("HTTP/1.1 407"), "the app lane still wants its subject's credential first", line)

        # NAT64. Only refusals are asked through the lane: a public NAT64
        # address would be dialled for real, so that side is asked of _outside.
        n = len(hits)
        for dest in ("[64:ff9b::a9fe:a9fe]:443", "[64:ff9b::a00:1]:443", "[64:ff9b::7f00:1]:443",
                     "[64:ff9b:1::808:808]:443", "[64:ff9b:2::808:808]:443", "nat64.example.test:443"):
            line = await ask(lport, f"CONNECT {dest} HTTP/1.1\r\nHost: {dest}\r\n".encode() + AUTH + b"\r\n")
            check(line.startswith("HTTP/1.1 403"), f"the app lane refuses NAT64 CONNECT {dest}", line)
        check(len(hits) == n, "no NAT64 address was dialled", hits[n:])
        check(NE._outside("64:ff9b::808:808") and NE._outside("64:ff9b::5db8:d70e"),
              "a NAT64 address holding a public IPv4 address is outside (DNS64 networks reach IPv4 sites so)")
        check(not NE._outside("64:ff9b::c0a8:101") and not NE._outside("64:ff9b::6440:1") and
              not NE._outside("64:ff9b:1:ffff::808:808"),
              "192.168.1.1 and 100.64.0.1 inside 64:ff9b::/96, and anything in the local-use /48, are not")
    finally:
        for s in (tsrv, lsrv, gsrv):
            s.close()

    await pinned_to_a_bastion()


class FakeRelay:
    """The bastion end of the relay: it records what the gate hands it and
    answers as client-go/egress.go does (200 to a CONNECT, then whatever the
    tunnel carries goes to the destination, here a canned answer)."""

    def __init__(self):
        self.heads = []
        self.requests = []

    def has_bastion(self, token_id):
        return token_id == 7

    def worker_count(self, token_id=None):
        return 1

    async def dispatch(self, head, reader, writer, timeout, bastion_token_id=None):
        self.heads.append((head, bastion_token_id))
        if head.startswith(b"CONNECT "):
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        try:
            req = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        except Exception:  # noqa: BLE001
            req = b""
        self.requests.append(req)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
        return True


async def pinned_to_a_bastion():
    # The gate looks the relay up at each request (egress_relay.get_egress_relay,
    # imported inside EgressProxy._handle), so the stand-in is put there.
    from server.network import egress_relay as ER
    relay = FakeRelay()
    ER.get_egress_relay = lambda: relay

    async def pinned_rules():
        return [{"id": 1, "pattern": "*", "action": "allow", "priority": 1, "subject": "*",
                 "exit": "bastion:pc"}]

    async def pin(name):
        return (7, None) if name == "pc" else (None, b"no such machine")

    lane = NE.EgressProxy(None, 9001, None, app_lane=True)
    lane._app_secret = SECRET
    lane._rules_now = pinned_rules
    lane._resolve_pin = pin
    gate = NE.EgressProxy(None, 9001, None, app_lane=False)
    gate._rules_now = pinned_rules
    gate._resolve_pin = pin
    lsrv = await asyncio.start_server(lane._handle, "127.0.0.1", 0)
    gsrv = await asyncio.start_server(gate._handle, "127.0.0.1", 0)
    lport, gport = lsrv.sockets[0].getsockname()[1], gsrv.sockets[0].getsockname()[1]

    async def talk(port, head, then=b""):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(head)
        await w.drain()
        try:
            first = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=10)
            if then:
                w.write(then)
                await w.drain()
            first += await asyncio.wait_for(r.read(4096), timeout=10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
            first = getattr(e, "partial", b"") or b"no answer"
        w.close()
        return first.decode("latin-1")

    try:
        for dest in ("10.0.0.1:443", "127.0.0.1:443", "169.254.169.254:443", "[::1]:443",
                     "[64:ff9b::a9fe:a9fe]:443", "internal.example.test:443", "split.example.test:443"):
            got = await talk(lport, f"CONNECT {dest} HTTP/1.1\r\nHost: {dest}\r\n".encode() + AUTH + b"\r\n")
            check(got.startswith("HTTP/1.1 403"), f"pinned to a bastion, the app lane refuses CONNECT {dest}",
                  got[:80])
        got = await talk(lport, b"GET http://127.0.0.1:11434/api/pull HTTP/1.1\r\nHost: 127.0.0.1:11434\r\n"
                         + AUTH + b"\r\n")
        check(got.startswith("HTTP/1.1 403"), "and a plain-HTTP request to this machine's own service", got[:80])
        got = await talk(lport, b"GET http://internal.example.test/ HTTP/1.1\r\nHost: internal.example.test\r\n"
                         + AUTH + b"\r\n")
        check(got.startswith("HTTP/1.1 403"), "and one to a name that resolves to loopback", got[:80])
        check(not relay.heads, "nothing private was handed to the bastion", relay.heads)

        got = await talk(lport, b"CONNECT site.example.test:443 HTTP/1.1\r\nHost: site.example.test:443\r\n"
                         + AUTH + b"\r\n", then=b"HELLO\r\n\r\n")
        head = relay.heads[-1][0] if relay.heads else b""
        check(got.startswith("HTTP/1.1 200") and head.startswith(b"CONNECT 93.184.215.14:443 HTTP/1.1\r\n")
              and relay.heads[-1][1] == 7 and b"Proxy-Authorization" not in head,
              "a public CONNECT goes to the pinned bastion as the address that was checked, without the "
              "lane's credential", (got[:60], head[:80]))
        got = await talk(lport, b"GET http://site.example.test/a/b?c=1 HTTP/1.1\r\nHost: site.example.test\r\n"
                         + AUTH + b"\r\n")
        head = relay.heads[-1][0] if relay.heads else b""
        req = relay.requests[-1] if relay.requests else b""
        check(head == b"CONNECT 93.184.215.14:80 HTTP/1.1\r\n\r\n" and
              req.startswith(b"GET /a/b?c=1 HTTP/1.1\r\n") and b"Host: site.example.test\r\n" in req and
              b"Proxy-Authorization" not in req and got.startswith("HTTP/1.1 200 OK") and got.endswith("ok"),
              "a plain-HTTP request goes down a CONNECT tunnel to the checked address, in origin form, "
              "and the client gets the site's answer", (head, req[:80], got[-60:]))

        n = len(relay.heads)
        got = await talk(gport, b"CONNECT 10.0.0.1:443 HTTP/1.1\r\nHost: 10.0.0.1:443\r\n\r\n", then=b"X\r\n\r\n")
        check(len(relay.heads) == n + 1 and relay.heads[-1][0].startswith(b"CONNECT 10.0.0.1:443 "),
              "the loopback gate's pinned traffic (the Host's own) goes to its bastion as before",
              relay.heads[n:])
    finally:
        lsrv.close()
        gsrv.close()

    # The lane as the Host starts it: platform pods dial it over IPv6, the
    # local runtime's containers over IPv4 (its network is 10.89.78.0/24).
    probe = socket.socket(socket.AF_INET6)
    probe.bind(("::", 0))
    NE.EGRESS_LANE_PORT = probe.getsockname()[1]
    probe.close()
    real = NE.EgressProxy(None, 9001, None, app_lane=True)
    real._rules_now = no_rules
    srv = await real.start()
    try:
        for fam, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(addr, NE.EGRESS_LANE_PORT), 5)
                w.write(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n")
                await w.drain()
                line = (await asyncio.wait_for(r.readline(), 10)).decode("latin-1").strip()
                w.close()
            except OSError as e:
                line = repr(e)
            check(line.startswith("HTTP/1.1 407"),
                  f"the started app lane answers over {'IPv4' if fam == socket.AF_INET else 'IPv6'}", line)
    finally:
        srv.close()


asyncio.run(main())
print()
if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    sys.exit(1)
print("all egress lane private-range checks passed")
