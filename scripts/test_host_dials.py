#!/usr/bin/env python3
"""A Host connects to a server that can't reach it back (server/network/host_dials.py,
the bastion's docs/host-connects.md, wire version 1).

Pinned, against a stand-in bastion that speaks the wire contract over real TLS on
loopback (the real S2 bastion binary is the end-to-end run, docs/verification):

  1. The code: normalised as the bastion does (case, dashes, O->0, I/L->1), and
     anything that is not 20 characters of its alphabet refused. The proof is
     HMAC-SHA256 over the label, the role, both certificate hashes and both
     nonces (a fixed vector).
  2. Pairing: this Host is the TLS server on the connection it opens, chooses
     privatae-pair/1, presents its certificate then its CA, proves the code
     first (hello) over the certificate it presented, checks the bastion's
     proof BEFORE it signs anything (a wrong one is 403 and no row, no
     certificate), files the row (transport host_dials, the server's address,
     the certificate's serial), answers transport host_dials, and reads done:
     active and pending give the person's words, refused drops the row.
  3. A bastion that closes after hello (it did not take the Host's proof): the
     person is told the code was not taken, and nothing is kept. A server with
     no window open (no pairing protocol offered): refused, nothing kept.
  4. The address: a name or IP with or without a port (8812 by default), IPv6
     in brackets; loopback and this Host's own names refused.
  5. The dialler: keeps 4 connections idle with ALPN privatae-lane/1 and the
     lane certificate, opens another when one carries its first byte, closes a
     connection whose client certificate is not its pairing's, and serves the
     rest with the lane handler; a server that is down is tried again with a
     growing wait. reconcile starts a dialler per pairing and stops one whose
     pairing was revoked.
  6. Asked in chat: manage_bastion op='add_server' files a card and pairs
     nothing; only the person's Connect pairs (the Host is refused), Don't
     connect pairs nothing, the code is cleared from the card once used, and a
     ticket written through POST /api/tickets cannot pose as the card.

Container-free: a stub pool; real TLS on 127.0.0.1.
APP_ROOT=<repo> python3 scripts/test_host_dials.py
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import socket
import ssl
import sys
import tempfile

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "scripts")]

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

from server.network import host_ca, host_dials, local_bastion  # noqa: E402

FAILS: list = []
CHECKS = 0


def check(cond, msg, extra=None):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:600]) if extra is not None and not cond else ""))
    if not cond:
        FAILS.append(msg)


# The stub pool and the stand-in bastion live in scripts/host_dials_standin.py,
# which scripts/e2e_network_add_server.py shares.
from host_dials_standin import (CODE, FP, NORM, Pool, StandinBastion, client_tls,  # noqa: E402
                                make_csr)


async def start_server(cb):
    srv = await asyncio.start_server(cb, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


def point_connect_at(port):
    orig = host_dials._connect

    async def _c(host, p):
        return await orig("127.0.0.1", port)
    host_dials._connect = _c
    return orig


async def pair_once(pool, bastion, address="203.0.113.9", code=CODE):
    srv, port = await start_server(bastion.serve)
    orig = point_connect_at(port)
    try:
        try:
            res = await host_dials.pair_with_server(pool, 1, address, code)
            err = None
        except host_dials.PairError as e:
            res, err = None, e
        await asyncio.wait_for(bastion.finished.wait(), 10)
        return res, err
    finally:
        host_dials._connect = orig
        srv.close()


async def main():
    print("1 the code and the proof")
    check(host_dials.normalize_code("s12aw-kp3hc fjtg7-has9c") == NORM, "lower case, dashes and spaces")
    check(host_dials.normalize_code("O1LI0" * 4) == "01110" * 4, "O reads as 0, I and L as 1")
    check(host_dials.normalize_code(NORM[:-1]) is None and host_dials.normalize_code(NORM + "Z") is None,
          "19 or 21 characters: not a code")
    check(host_dials.normalize_code("U" * 20) is None and host_dials.normalize_code(None) is None,
          "a letter outside the alphabet, or nothing: not a code")
    v = host_dials.proof(NORM, "host", b"\x01" * 32, b"\x02" * 32, b"\x03" * 32, b"\x04" * 32)
    m = hmac.new(NORM.encode(), b"privatae-pair-v1 host\x00" + b"\x01" * 32 + b"\x02" * 32 + b"\x03" * 32 + b"\x04" * 32,
                 hashlib.sha256).hexdigest()
    check(v == m and v != host_dials.proof(NORM, "bastion", b"\x01" * 32, b"\x02" * 32, b"\x03" * 32, b"\x04" * 32),
          "the proof: HMAC-SHA256(code, label + role + NUL + leaf + ca + nonce_b + nonce_h), per role")

    print("2 pairing")
    pool = Pool()
    host_dials.wake = lambda: None
    b = StandinBastion()
    res, err = await pair_once(pool, b)
    check(err is None and res and res["state"] == "active", "the pairing finished, active", (err, res, b.seen))
    check(b.seen.get("alpn") == host_dials.PAIR_ALPN, "the Host chose privatae-pair/1 of the two offered", b.seen.get("alpn"))
    ca_der = x509.load_pem_x509_certificate((await host_ca.ca_cert_pem(pool)).encode()).public_bytes(serialization.Encoding.DER)
    check(b.seen.get("chain_len") == 2 and b.seen.get("ca_der") == ca_der,
          "the Host presented its certificate, then its CA", b.seen.get("chain_len"))
    check(b.seen.get("leaf_names") == [local_bastion.TCP_SERVER_NAME], "the certificate names host.cseries.internal",
          b.seen.get("leaf_names"))
    check(b.seen.get("host_proved") is True, "the Host proved the code first, over the certificate it presented")
    hello = (b.seen.get("hello") or (0, {}))[1]
    check(hello.get("host_handle") == "home" and hello.get("host_name", "").startswith("home")
          and "proof" in hello and len(bytes.fromhex(hello.get("nonce", ""))) == 32,
          "hello: handle, name, a fresh 32-byte nonce, the proof", hello)
    st, rr = b.seen.get("redeem") or (0, {})
    check(st == 200 and rr.get("transport") == "host_dials" and "lane_address" not in rr and rr.get("bearer")
          and rr.get("ca_cert", "").strip() == (await host_ca.ca_cert_pem(pool)).strip(),
          "redeem: transport host_dials, no lane address, a bearer, the CA it presented", rr)
    cert = x509.load_pem_x509_certificate(rr.get("operator_cert", "").encode())
    cert.verify_directly_issued_by(x509.load_der_x509_certificate(ca_der))
    check(cert.public_key().public_numbers() == b.seen["op_key"].public_key().public_numbers(),
          "the operator certificate is the bastion's CSR, signed by this Host's CA")
    row = pool.rows[-1] if pool.rows else {}
    n = row.get("notes", {})
    check(n.get("transport") == "host_dials" and n.get("door") == "203.0.113.9:8812"
          and n.get("cert_serial") == format(cert.serial_number, "x") and row.get("fp") == FP and not row.get("revoked"),
          "the row: transport host_dials, the server's address, the certificate's serial, the fingerprint", row)
    check(res and "connected to far-server" in res.get("message", "") and res.get("token_id") == row.get("id"),
          "the person's words name the server", res)

    b = StandinBastion()
    res2, err = await pair_once(pool, b)
    check(err is None and len(pool.rows) == 2 and pool.rows[0]["revoked"] and not pool.rows[1]["revoked"],
          "pairing the same server again replaces the earlier pairing (revoked)", (err, pool.rows))

    pool2 = Pool()
    b = StandinBastion(done={"v": 1, "state": "pending", "admitted_by": "studio"})
    res, err = await pair_once(pool2, b)
    check(err is None and res["state"] == "pending" and "studio, the Host in charge of far-server, has to let it in"
          in res["message"] and not pool2.rows[-1]["revoked"],
          "pending: kept, and the words say who lets it in", (err, res))

    pool3 = Pool()
    b = StandinBastion(bad_proof=True)
    res, err = await pair_once(pool3, b)
    check(err is not None and err.status == 403 and (b.seen.get("redeem") or (0,))[0] == 403,
          "a bastion that did not prove the code: 403", (err and err.words, b.seen.get("redeem")))
    check(pool3.rows == [], "...and nothing was signed or filed before the proof was checked", pool3.rows)

    pool4 = Pool()
    b = StandinBastion(done={"v": 1, "state": "refused", "error": "this server already has a Host in charge"})
    res, err = await pair_once(pool4, b)
    check(err is not None and err.status == 409 and "this server already has a Host in charge" in err.words
          and pool4.rows and pool4.rows[-1]["revoked"],
          "done refused: the server's words, and the credentials issued are dropped", (err and err.words, pool4.rows))

    pool4b = Pool()
    b = StandinBastion(refuse_before_redeem="this Host is already paired with this server as home1.cseries.localhost; nothing was paired")
    res, err = await pair_once(pool4b, b)
    check(err is not None and err.status == 409 and "already paired with this server" in err.words
          and b.seen.get("done_status") == 200 and pool4b.rows == [],
          "a refusal before the redeem (already paired there): the server's words, nothing signed or kept",
          (err and err.words, b.seen.get("done_status"), pool4b.rows))

    print("3 a code the server did not take; no window")
    pool5 = Pool()
    b = StandinBastion(code="0" * 20)
    res, err = await pair_once(pool5, b)
    check(b.seen.get("host_proved") is False and err is not None and err.status == 403
          and "didn't take that code" in err.words and pool5.rows == [],
          "a wrong code: the server closes after hello; the person is told, nothing kept", (err and err.words, b.seen))
    pool6 = Pool()
    b = StandinBastion(offer=(host_dials.LANE_ALPN,))
    res, err = await pair_once(pool6, b)
    check(err is not None and err.status == 502 and pool6.rows == [],
          "no window open (only the lane protocol offered): refused, nothing kept", err and err.words)
    pool7 = Pool()
    try:
        await host_dials.pair_with_server(pool7, 1, "203.0.113.9", "not a code")
        check(False, "a malformed code is refused before anything is dialled")
    except host_dials.PairError as e:
        check(e.status == 400 and pool7.rows == [], "a malformed code is refused before anything is dialled")

    # Cancelled after the Host signed and filed (the request went away before
    # done): the credentials it issued are dropped, not left to dial.
    pool8 = Pool()
    b = StandinBastion(hang_before_done=True)
    srv8, port8 = await start_server(b.serve)
    orig = point_connect_at(port8)
    try:
        task = asyncio.ensure_future(host_dials.pair_with_server(pool8, 1, "203.0.113.9", CODE))
        for _ in range(250):
            if b.seen.get("redeem"):
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001
            pass
    finally:
        host_dials._connect = orig
        srv8.close()
    check(pool8.rows and all(r["revoked"] for r in pool8.rows),
          "a pairing cancelled after it signed: the row it filed is dropped", pool8.rows)

    # The person's words carry no machine terms (an exception's class name).
    s9 = socket.socket()
    s9.bind(("127.0.0.1", 0))
    dead9 = s9.getsockname()[1]
    s9.close()
    orig = point_connect_at(dead9)
    try:
        await host_dials.pair_with_server(Pool(), 1, "203.0.113.9", CODE)
        check(False, "an unreachable server is refused")
    except host_dials.PairError as e:
        check("Error" not in e.words and "TCP" not in e.words and "couldn't reach" in e.words,
              "an unreachable server: the person is told in words, no exception names", e.words)
    finally:
        host_dials._connect = orig

    print("4 the address")
    pa = host_dials.parse_address
    check(pa("203.0.113.9") == ("203.0.113.9", 8812) and pa("srv.example.com:9000") == ("srv.example.com", 9000)
          and pa("[2001:db8::5]:8812") == ("2001:db8::5", 8812) and pa("2001:db8::5") == ("2001:db8::5", 8812),
          "names, addresses, ports, IPv6")
    for bad in ("127.0.0.1", "localhost:8812", "[::1]:8812", "0.0.0.0", "", "a b", "srv:port", "x.localhost"):
        try:
            pa(bad)
            check(False, f"{bad!r} refused")
        except host_dials.PairError:
            check(True, f"{bad!r} refused")

    print("5 the dialler")
    await dialler_checks()

    print("6 asked in chat: the card")
    await card_checks()


async def dialler_checks():
    pool = Pool()
    await host_ca.ensure(pool)
    key, csr = make_csr()
    op_cert, serial = await host_ca.sign_operator_csr(pool, csr)
    key2, csr2 = make_csr()
    other_cert, other_serial = await host_ca.sign_operator_csr(pool, csr2)
    ca_pem = await host_ca.ca_cert_pem(pool)
    d = tempfile.mkdtemp(prefix="dials-")

    def client_ctx(cert_pem, k):
        p = os.path.join(d, "c.pem")
        with open(p, "w") as f:
            f.write(cert_pem + k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()).decode())
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cadata=ca_pem)
        ctx.load_cert_chain(p)
        ctx.set_alpn_protocols([host_dials.LANE_ALPN])
        os.unlink(p)
        return ctx

    conns = []
    use_cert = {"pem": op_cert, "key": key}
    alpn_seen = []

    async def door(reader, writer):
        try:
            await client_tls(writer, client_ctx(use_cert["pem"], use_cert["key"]), local_bastion.TCP_SERVER_NAME)
            alpn_seen.append(writer.get_extra_info("ssl_object").selected_alpn_protocol())
            conns.append((reader, writer))
        except Exception as e:  # noqa: BLE001
            alpn_seen.append(repr(e))
    srv, port = await start_server(door)
    served = []

    async def lane_handler(reader, writer):
        line = await reader.readline()
        if not line:
            return               # closed while idle
        served.append(line)
        writer.write(b"ok\n")
        await writer.drain()
        await reader.read()      # until the bastion closes it

    dl = host_dials.Dialler(7, f"127.0.0.1:{port}", serial, pool_getter=lambda: pool, lane_handler=lane_handler)
    t = asyncio.ensure_future(dl.run())
    for _ in range(200):
        if len(conns) >= host_dials.SPARE_IDLE:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.3)
    check(len(conns) == 4 and dl.idle == 4, "4 connections kept idle, and no more", (len(conns), dl.idle))
    check(host_dials.MAX_OPEN == 64 and (host_dials.BACKOFF_MIN_S, host_dials.BACKOFF_MAX_S) == (1.0, 30.0)
          and host_dials.DEFAULT_PORT == 8812,
          "the contract's numbers: at most 64 open, redial 1 s doubling to 30 s, port 8812")
    check(set(alpn_seen) == {host_dials.LANE_ALPN}, "each with privatae-lane/1", alpn_seen)
    r0, w0 = conns[0]
    w0.write(b"GET /bastion-mgmt\n")
    await w0.drain()
    reply = await asyncio.wait_for(r0.readline(), 5)
    for _ in range(200):
        if len(conns) >= host_dials.SPARE_IDLE + 1:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)
    check(reply == b"ok\n" and served == [b"GET /bastion-mgmt\n"], "the lane handler served the bastion's request")
    check(len(conns) == host_dials.SPARE_IDLE + 1 and dl.idle == host_dials.SPARE_IDLE,
          "a connection that carried its first byte is replaced: 4 idle again", (len(conns), dl.idle))
    w0.close()
    await asyncio.sleep(0.3)
    check(dl.open == host_dials.SPARE_IDLE, "its close is counted", dl.open)
    # Another pairing's certificate on this door: closed, never served.
    use_cert.update(pem=other_cert, key=key2)
    n_served = len(served)
    for r, w in conns[1:3]:
        w.close()
    await asyncio.sleep(0.6)
    newest = conns[-1]
    try:
        newest[1].write(b"GET /x\n")
        await newest[1].drain()
        got = await asyncio.wait_for(newest[0].read(10), 3)
    except Exception:  # noqa: BLE001
        got = b""
    check(got == b"" and len(served) == n_served,
          "a client certificate that is not this pairing's: closed, nothing served", (got, served[n_served:]))
    t.cancel()
    srv.close()
    for _r, w in conns:
        w.close()

    # A server that is down: tried again, with a growing wait.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    host_dials.BACKOFF_MIN_S, host_dials.BACKOFF_MAX_S = 0.05, 0.4
    dl2 = host_dials.Dialler(8, f"127.0.0.1:{dead}", serial, pool_getter=lambda: pool, lane_handler=lane_handler)
    t2 = asyncio.ensure_future(dl2.run())
    await asyncio.sleep(1.2)
    t2.cancel()
    check(3 <= dl2.dials <= 8 and dl2.open == 0,
          "a server that is down is redialled with a doubling wait, not in a loop", dl2.dials)
    host_dials.BACKOFF_MIN_S, host_dials.BACKOFF_MAX_S = 1.0, 30.0

    # A server that takes the handshake then drops every connection at once:
    # backed off like a failed dial, never redialled as fast as the network allows.
    async def drop_door(reader, writer):
        try:
            await client_tls(writer, client_ctx(op_cert, key), local_bastion.TCP_SERVER_NAME)
        except Exception:  # noqa: BLE001
            pass
        writer.close()
    srv3, port3 = await start_server(drop_door)
    host_dials.BACKOFF_MIN_S, host_dials.BACKOFF_MAX_S = 0.05, 0.4
    dl3 = host_dials.Dialler(9, f"127.0.0.1:{port3}", serial, pool_getter=lambda: pool, lane_handler=lane_handler)
    t3 = asyncio.ensure_future(dl3.run())
    await asyncio.sleep(1.2)
    t3.cancel()
    srv3.close()
    check(dl3.dials <= 12, "a server that drops each connection after its handshake is backed off, "
                           "not redialled in a loop", dl3.dials)
    host_dials.BACKOFF_MIN_S, host_dials.BACKOFF_MAX_S = 1.0, 30.0

    # reconcile: a dialler per pairing; a revoked pairing's is stopped.
    rp = Pool()
    rp.rows = [{"id": 11, "name": "a", "notes": {"transport": "host_dials", "door": f"127.0.0.1:{dead}",
                                                  "cert_serial": "aa"}, "fp": None, "revoked": False},
               {"id": 12, "name": "b", "notes": {"transport": "tcp", "cert_serial": "bb"}, "fp": None, "revoked": False}]
    host_dials._DIALLERS.clear()
    await host_dials.reconcile(rp, 1, pool_getter=lambda: pool, lane_handler=lane_handler)
    check(set(host_dials.diallers()) == {11}, "reconcile: a dialler for the host_dials pairing only",
          list(host_dials.diallers()))
    rp.rows[0]["revoked"] = True
    task = host_dials._DIALLERS[11][1]
    await host_dials.reconcile(rp, 1, pool_getter=lambda: pool, lane_handler=lane_handler)
    await asyncio.sleep(0.05)
    check(host_dials.diallers() == {} and task.cancelled(), "a revoked pairing's dialler is stopped")


async def card_checks():
    from server.routes import tickets as tk
    pool = Pool()
    filed = []

    async def create(pool_, eid, **kw):
        filed.append(kw)
        tid = 100 + len(filed)
        pool.tickets[tid] = kw["metadata"]
        return {"id": tid}
    tk.create_ticket_service = create
    paired = []

    async def fake_pair(pool_, eid, address, code):
        paired.append((address, code))
        if code == "FAIL":
            raise host_dials.PairError(403, "The server didn't take that code.")
        return {"state": "active", "server": address, "machine": "far-server", "token_id": 3,
                "message": "Your Host is connected to far-server."}
    real_pair = host_dials.pair_with_server
    host_dials.pair_with_server = fake_pair
    try:
        from mastercode.runtime import tools_network
        from server import brain_service
        brain_service.get_pool = lambda: pool
        brain_service._entity_id = 1
        out = await tools_network._exec_manage_bastion({"op": "add_server", "address": "203.0.113.9", "code": CODE})
        md = filed[-1]["metadata"] if filed else {}
        check(out.get("status") == "ok" and out["result"].get("asked") and paired == []
              and md.get("kind") == "server_pair" and md["server_pair"] == {"address": "203.0.113.9:8812", "code": NORM}
              and md.get("user_only") is True,
              "manage_bastion add_server files the person's card and pairs nothing", (out, md))
        bad = await tools_network._exec_manage_bastion({"op": "add_server", "address": "127.0.0.1", "code": CODE})
        check(bad.get("status") != "ok" and len(filed) == 1, "a loopback address is refused, no card", bad)
        tid = 101
        try:
            await host_dials.decide_card(pool, 1, tid, md, option_id="connect", actor="host")
            check(False, "the Host cannot press Connect")
        except host_dials.PairError as e:
            check(e.status == 403 and paired == [], "the Host cannot press Connect")
        r = await host_dials.decide_card(pool, 1, tid, {**md, "server_pair": dict(md["server_pair"])},
                                         option_id="cancel", actor="user")
        check(r.get("connected") is False and paired == [], "Don't connect pairs nothing")
        pool.tickets[102] = {"kind": "server_pair", "server_pair": {"address": "203.0.113.9:8812", "code": NORM}}
        r = await host_dials.decide_card(pool, 1, 102, pool.tickets[102], option_id="connect", actor="user")
        check(paired == [("203.0.113.9:8812", NORM)] and r.get("connected") and "far-server" in r.get("message", ""),
              "Connect pairs with the card's address and code, and carries the server's words", (paired, r))
        check(pool.tickets[102]["server_pair"]["code"] is None, "the code is cleared from the card once used")
        try:
            await host_dials.decide_card(pool, 1, 102, pool.tickets[102], option_id="connect", actor="user")
            check(False, "a used card cannot pair again")
        except host_dials.PairError as e:
            check(e.status == 409 and len(paired) == 1, "a used card cannot pair again")
        pool.tickets[103] = {"kind": "server_pair", "server_pair": {"address": "203.0.113.9:8812", "code": "FAIL"}}
        try:
            await host_dials.decide_card(pool, 1, 103, pool.tickets[103], option_id="connect", actor="user")
            check(False, "a refusal is raised with its words")
        except host_dials.PairError as e:
            check("didn't take that code" in e.words and pool.tickets[103]["server_pair"]["code"] == "FAIL",
                  "a refusal is raised with its words and the card keeps its code to try again")
        gate = asyncio.Event()
        slow_calls = []

        async def slow_pair(pool_, eid, address, code):
            slow_calls.append(code)
            await gate.wait()
            return {"state": "active", "server": address, "machine": "far-server", "token_id": 4,
                    "message": "Your Host is connected to far-server."}
        host_dials.pair_with_server = slow_pair
        pool.tickets[104] = {"kind": "server_pair", "server_pair": {"address": "203.0.113.9:8812", "code": NORM}}
        first = asyncio.ensure_future(host_dials.decide_card(pool, 1, 104, pool.tickets[104],
                                                             option_id="connect", actor="user"))
        await asyncio.sleep(0.05)
        try:
            await asyncio.wait_for(host_dials.decide_card(pool, 1, 104, pool.tickets[104],
                                                          option_id="connect", actor="user"), 2)
            second = None
        except host_dials.PairError as e:
            second = e
        except asyncio.TimeoutError:
            second = None        # it waited on the first: a second pairing was started
        gate.set()
        r1 = await first
        check(second is not None and second.status == 409 and slow_calls == [NORM] and r1.get("connected"),
              "a second Connect while the first is connecting pairs nothing and is told so",
              (second and second.words, slow_calls))
        host_dials.pair_with_server = fake_pair
        stripped = tk._strip_platform_locks({"kind": "server_pair", "server_pair": {"address": "x", "code": "y"}})
        check("kind" not in stripped and "server_pair" not in stripped,
              "POST /api/tickets cannot write a server_pair card", stripped)
    finally:
        host_dials.pair_with_server = real_pair


asyncio.run(main())
print(f"\n{CHECKS - len(FAILS)}/{CHECKS} passed")
sys.exit(1 if FAILS else 0)
