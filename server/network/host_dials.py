"""Your Host connects to a server (bastion docs/host-connects.md, wire version 1).

A Host on a person's own computer sits behind their home router: a server on
the internet cannot open a connection to it, and nothing on the home network
should be opened for it. The server can be reached, though. So for a server
the Host can reach but that cannot reach it back, the Host opens the
connections, to one port on the server (8812 unless the person says another),
and the bastion there runs every lane over them exactly as it runs them over a
connection it opened itself.

Only the direction of the TCP connection changes. On every connection this
Host is the TLS SERVER and the bastion the TLS client, the Host's certificate
names host.cseries.internal (local_bastion.TCP_SERVER_NAME), the Host's CA is
the only root the bastion trusts, and on a lane the Host requires and checks
the bastion's operator certificate against that CA, as its lane port does.

Two things live here:

  pair_with_server  "Add a server": the person gives the server's address and
                    the code privatae-bastion pair --open showed there. One
                    connection, ALPN privatae-pair/1: this Host presents its
                    certificate then its CA, and answers the bastion's three
                    requests. It proves the code first (hello), checks the
                    bastion's proof BEFORE signing anything (redeem), and
                    reads the bastion's verdict (done), which the person is
                    shown in plain words. The code never crosses the wire.
  supervise         a dialler per such pairing: 4 connections kept idle, at
                    most 64 open, ALPN privatae-lane/1, each served by the lane
                    port's own handler (local_bastion.make_tcp_lane_handler) as
                    if it had been accepted there. A failed dial or handshake
                    is tried again after 1 s, doubling to 30 s, with jitter.

The pairing is a bastion row like any other (mcp_client_tokens), with
notes.transport "host_dials", the server's address in notes.door and the
operator certificate's serial in notes.cert_serial, which is how the lane
handler finds the row a connection belongs to, and how a revoke closes it.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import random
import secrets
import socket
import ssl
import time
from typing import Any, Callable, Optional

log = logging.getLogger("cseries.host_dials")

PAIR_ALPN = "privatae-pair/1"
LANE_ALPN = "privatae-lane/1"
TRANSPORT = "host_dials"
DEFAULT_PORT = 8812
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LEN = 20
PROOF_LABEL = b"privatae-pair-v1 "

# The pool a dialler keeps (host-connects.md "How many").
SPARE_IDLE = 4
MAX_OPEN = 64
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0
# A connection the server closes this soon after its handshake, before it
# spoke on it, is a refusal, not a success: the next dial waits as after a
# failed one (a server that takes the handshake then drops every connection
# would otherwise be redialled as fast as the network turns round).
QUICK_CLOSE_S = 0.5
KEEPALIVE_S = 25
CONNECT_TIMEOUT_S = 10.0
HANDSHAKE_TIMEOUT_S = 10.0
# The whole pairing exchange, as the bastion bounds it (windowExchangeTimeout).
EXCHANGE_TIMEOUT_S = 60.0
# How often the supervisor reads which pairings there are, when nothing wakes it.
RECONCILE_S = 5.0
MAX_BODY = 64 * 1024

_PAIR_PATHS = ("/v1/pair/hello", "/v1/pair/redeem", "/v1/pair/done")


class PairError(Exception):
    """A pairing that did not happen, with words for the person."""

    def __init__(self, status: int, words: str):
        super().__init__(words)
        self.status = status
        self.words = words


# ── the code and the proofs ──────────────────────────────────────────────────

def normalize_code(s: Any) -> Optional[str]:
    """The code as both sides key the proofs with it: upper case, dashes and
    spaces dropped, O read as 0 and I or L as 1; None when what is left is not
    CODE_LEN characters of CODE_ALPHABET (pair_window.go normalizeWindowCode)."""
    if not isinstance(s, str):
        return None
    out = []
    for ch in s.upper():
        if ch in "- \t":
            continue
        ch = {"O": "0", "I": "1", "L": "1"}.get(ch, ch)
        if ch not in CODE_ALPHABET:
            return None
        out.append(ch)
    return "".join(out) if len(out) == CODE_LEN else None


def proof(code: str, role: str, leaf_sha: bytes, ca_sha: bytes, nonce_b: bytes, nonce_h: bytes) -> str:
    """role's proof of the code on one connection (pair_window.go windowProof):
    HMAC-SHA256 keyed by the normalised code over the label, the role, the
    SHA-256 of this Host's certificate as presented, that of its CA, and both
    nonces, as lowercase hex."""
    m = hmac.new(code.encode(), PROOF_LABEL + role.encode() + b"\x00", hashlib.sha256)
    for p in (leaf_sha, ca_sha, nonce_b, nonce_h):
        m.update(p)
    return m.hexdigest()


def parse_address(s: Any) -> tuple[str, int]:
    """(host, port) of a server the person named: a name or an address, with
    or without :port (8812 when none), an IPv6 address in brackets when it
    carries a port. Loopback and unspecified addresses are refused: from this
    Host they name its own virtual machine, never the server."""
    raw = (s or "").strip() if isinstance(s, str) else ""
    if raw.lower().startswith(("https://", "http://")):
        raw = raw.split("://", 1)[1].rstrip("/")
    if not raw or len(raw) > 260 or any(c.isspace() for c in raw):
        raise PairError(400, "Give the server's address: its name or IP address, as "
                             "privatae-bastion pair --open showed it, with :port if it showed one.")
    host, port = raw, DEFAULT_PORT
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0:
            raise PairError(400, f"{raw} is not an address this Host can read.")
        host, rest = raw[1:end], raw[end + 1:]
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                raise PairError(400, f"{raw} is not an address this Host can read.")
            port = int(rest[1:])
    elif raw.count(":") == 1:
        host, p = raw.split(":")
        if not p.isdigit():
            raise PairError(400, f"{raw} has no port number after the colon.")
        port = int(p)
    if not 0 < port < 65536:
        raise PairError(400, f"{port} is not a port number.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
        labels = host.rstrip(".").split(".")
        if not host or not all(lb and len(lb) <= 63 and all(c.isalnum() or c == "-" for c in lb)
                               for lb in labels):
            raise PairError(400, f"{host} is not a server name or an IP address.")
        if host.rstrip(".").lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(".localhost"):
            ip = ipaddress.ip_address("127.0.0.1")
    if ip is not None and (ip.is_loopback or ip.is_unspecified):
        raise PairError(400, "That address is this Host's own. Give the server's address on your "
                             "network or on the internet.")
    return host, port


def _unreached(e: BaseException) -> str:
    """Why a connection to the server did not open, in words for the person
    (the exception itself goes to the log)."""
    if isinstance(e, socket.gaierror):
        return "no server by that name was found"
    if isinstance(e, ConnectionRefusedError):
        return "nothing on it took the connection on that port"
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "it didn't answer in time"
    return "the connection didn't open"


def door_label(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


# ── a TLS server on a connection this Host opens ────────────────────────────

def _keepalive(sock: socket.socket) -> None:
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for opt, val in (("TCP_KEEPIDLE", KEEPALIVE_S), ("TCP_KEEPINTVL", KEEPALIVE_S), ("TCP_KEEPCNT", 4)):
            if hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)
    except OSError:
        pass


async def _connect(host: str, port: int) -> socket.socket:
    """A connected, non-blocking TCP socket to host:port."""
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
                                   CONNECT_TIMEOUT_S)
    last: Optional[BaseException] = None
    for family, stype, proto, _cn, addr in infos:
        sock = socket.socket(family, stype, proto)
        sock.setblocking(False)
        try:
            await asyncio.wait_for(loop.sock_connect(sock, addr), CONNECT_TIMEOUT_S)
            _keepalive(sock)
            return sock
        except BaseException as e:  # noqa: BLE001 — the next address, then the error
            sock.close()
            last = e
            if isinstance(e, asyncio.CancelledError):
                raise
    raise last or OSError(f"no address for {host}")


class _FirstData(asyncio.StreamReader):
    """A StreamReader that says when the first byte arrives (a lane connection
    is no longer idle then)."""

    def __init__(self, on_first: Optional[Callable[[], None]] = None, **kw):
        super().__init__(**kw)
        self._on_first = on_first

    def feed_data(self, data):
        if data and self._on_first is not None:
            cb, self._on_first = self._on_first, None
            try:
                cb()
            except Exception:  # noqa: BLE001
                pass
        super().feed_data(data)


async def tls_server_on(sock: socket.socket, ctx: ssl.SSLContext, *, limit: int):
    """The TLS server side on a socket this Host connected: the handshake,
    then (reader, writer)."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=limit, loop=loop)
    transport, protocol = await loop.connect_accepted_socket(
        lambda: asyncio.StreamReaderProtocol(reader, loop=loop), sock=sock, ssl=ctx,
        ssl_handshake_timeout=HANDSHAKE_TIMEOUT_S)
    return reader, asyncio.StreamWriter(transport, protocol, reader, loop)


def _server_ctx(alpn: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.set_alpn_protocols([alpn])
    # Nothing goes from this Host on a connection before the bastion speaks
    # (host-connects.md): no TLS 1.3 session tickets after the handshake.
    try:
        ctx.num_tickets = 0
    except (AttributeError, ValueError):
        pass
    return ctx


# ── the pairing exchange ─────────────────────────────────────────────────────

_REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
            409: "Conflict", 411: "Length Required", 413: "Payload Too Large", 500: "Internal Server Error"}


def _reply(status: int, body: dict) -> bytes:
    """An answer on the kept-alive pairing connection (no Connection: close:
    the bastion sends all three requests on it)."""
    raw = json.dumps(body).encode()
    return (f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(raw)}\r\n\r\n").encode() + raw


async def _read(reader: asyncio.StreamReader, timeout: float) -> tuple[str, dict]:
    """(path, JSON body) of the bastion's next request; raises on anything else."""
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    lines = head[:-4].split(b"\r\n")
    parts = lines[0].split(b" ")
    if len(parts) != 3 or not parts[2].startswith(b"HTTP/1."):
        raise ValueError("bad request line")
    method, path = parts[0], parts[1].decode("latin-1").split("?", 1)[0]
    n = None
    for ln in lines[1:]:
        k, _, v = ln.partition(b":")
        k = k.strip().lower()
        if k == b"transfer-encoding":
            raise ValueError("chunked request")
        if k == b"content-length":
            if n is not None or not v.strip().isdigit():
                raise ValueError("bad Content-Length")
            n = int(v.strip())
    if method != b"POST" or path not in _PAIR_PATHS:
        raise ValueError(f"{method.decode('latin-1')} {path} is not a pairing request")
    if n is None or n > MAX_BODY:
        raise ValueError("no or too large Content-Length")
    raw = await asyncio.wait_for(reader.readexactly(n), timeout)
    body = json.loads(raw or b"{}")
    if not isinstance(body, dict):
        raise ValueError("body is not a JSON object")
    return path, body


def _plain(s: Any, n: int = 200) -> str:
    t = " ".join(str(s or "").split())
    return "".join(ch for ch in t if ch.isprintable())[:n]


def done_words(done: dict, server: str) -> str:
    """What the bastion's done says, for the person."""
    state = done.get("state")
    machine = _plain(done.get("machine") or server, 80)
    if state == "active":
        return (f"Your Host is connected to {machine}. It connects to it by itself from now on, "
                f"whenever it runs.")
    if state == "pending":
        who = _plain(done.get("admitted_by"), 80)
        by = f"{who}, the Host in charge of {machine}," if who else f"the Host in charge of {machine}"
        return (f"{machine} is paired with your Host, but {by} has to let it in first. Your Host "
                f"keeps connecting and can use {machine} as soon as it is let in.")
    why = _plain(done.get("error"), 300) or "it gave no reason"
    return f"{machine} didn't pair with your Host: {why}. Nothing was kept."


async def _insert_row(pool, entity_id: int, *, door: str, operator_csr: str, hostname: str,
                      platform: str) -> dict:
    """Sign the bastion's CSR with this Host's CA and file the pairing:
    (answer for the bastion, the row id). The same row redeem_local writes,
    with this transport and the server's address."""
    import uuid
    from server.network import bastion_enroll, host_ca, local_bastion
    own = await bastion_enroll.resolve_self_handle(entity_id)
    if not own:
        raise PairError(409, "This Host has no name yet, so it can't pair a server. Give it a name in Settings first.")
    host_ca.checked_operator_csr(operator_csr)
    operator_cert, serial = await host_ca.sign_operator_csr(pool, operator_csr)
    ca_cert = await host_ca.ca_cert_pem(pool)
    pair_name = local_bastion.pairing_host_name(own, host_ca.ca_sha256(ca_cert)[:local_bastion.HOST_ID_LEN])
    fp = bastion_enroll._fp_from_csr(operator_csr)
    bearer, token_hash = bastion_enroll._mint_bearer()
    notes = {
        "transport": TRANSPORT,
        "door": door,
        "operator_uuid": str(uuid.uuid4()),
        "cert_serial": serial,
        "host_name": pair_name,
        "machine": hostname or None,
        "platform": platform or None,
    }
    row = await pool.fetchrow(
        """
        INSERT INTO mcp_client_tokens
            (entity_id, name, token_hash, url_handle, scopes, operator_kind, notes,
             hardware_fingerprint, expires_at)
        VALUES ($1, $2, $3, $4, $5, 'bastion', $6::jsonb, $7, now() + interval '7 days')
        RETURNING id
        """,
        entity_id, (hostname or door)[:120], token_hash, secrets.token_hex(8),
        bastion_enroll.BASTION_SCOPES, json.dumps(notes), fp)
    log.info("server paired through its window: token_id=%s door=%s machine=%s fp=%s",
             row["id"], door, hostname or "?", "csr-bound" if fp else "none")
    return {"id": int(row["id"]), "answer": {
        "operator_cert": operator_cert, "ca_cert": ca_cert,
        "operator_crt": operator_cert, "ca_crt": ca_cert,
        "bearer": bearer, "transport": TRANSPORT, "host_handle": own, "host_name": pair_name}}


async def _drop_row(pool, token_id: int, why: str) -> None:
    try:
        await pool.execute("UPDATE mcp_client_tokens SET revoked_at = now() "
                           "WHERE id=$1 AND revoked_at IS NULL", token_id)
        log.info("server pairing token_id=%s dropped: %s", token_id, why)
    except Exception as e:  # noqa: BLE001
        log.warning("server pairing token_id=%s not dropped (%s): %s", token_id, type(e).__name__, why)


async def _replace_older(pool, entity_id: int, token_id: int, door: str) -> None:
    """A pairing just made with the server at `door` replaces any earlier one
    this Host holds with it: the server's bastion keeps one pairing per Host
    and gave the new one fresh credentials, so the old row's certificate is no
    longer what it presents, and its dialler would only be turned away."""
    from server.network import local_bastion
    try:
        rows = await pool.fetch(
            "UPDATE mcp_client_tokens SET revoked_at = now() WHERE entity_id = $1 "
            "AND operator_kind = 'bastion' AND notes->>'transport' = $2 AND notes->>'door' = $3 "
            "AND id <> $4 AND revoked_at IS NULL RETURNING id", entity_id, TRANSPORT, door, token_id)
    except Exception as e:  # noqa: BLE001 — the new pairing stands either way
        log.warning("server %s: the earlier pairing was not revoked (%s)", door, type(e).__name__)
        return
    for r in rows:
        log.info("server %s: pairing token_id=%s replaced by token_id=%s", door, r["id"], token_id)
        local_bastion.close_lanes(int(r["id"]), "replaced")


async def pair_with_server(pool, entity_id: int, address: Any, code: Any) -> dict:
    """Pair this Host with the server at `address` through the window its
    bastion opened, with the code it showed. Returns {state, server, machine,
    host_name, admitted_by, token_id, message}; raises PairError with words
    for the person when it did not pair."""
    norm = normalize_code(code)
    if norm is None:
        raise PairError(400, "That isn't the code: it is 20 letters and numbers, as "
                             "privatae-bastion pair --open showed it on the server.")
    host, port = parse_address(address)
    door = door_label(host, port)
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from server.network import bastion_enroll, host_ca, local_bastion
    own = await bastion_enroll.resolve_self_handle(entity_id)
    if not own:
        raise PairError(409, "This Host has no name yet, so it can't pair a server. Give it a name in Settings first.")
    await host_ca.ensure(pool)
    ca_pem = await host_ca.ca_cert_pem(pool)
    pair_name = local_bastion.pairing_host_name(own, host_ca.ca_sha256(ca_pem)[:local_bastion.HOST_ID_LEN])
    leaf_pem, key_pem, _ = await host_ca.issue_lane_server_cert(pool, [local_bastion.TCP_SERVER_NAME])
    ctx = _server_ctx(PAIR_ALPN)
    host_ca.load_server_chain(ctx, leaf_pem + ca_pem.encode(), key_pem)
    der = lambda pem: x509.load_pem_x509_certificate(pem).public_bytes(serialization.Encoding.DER)  # noqa: E731
    leaf_sha = hashlib.sha256(der(leaf_pem)).digest()
    ca_sha = hashlib.sha256(der(ca_pem.encode())).digest()

    try:
        sock = await _connect(host, port)
    except Exception as e:  # noqa: BLE001
        log.info("pairing with %s: no connection: %r", door, e)
        raise PairError(502, f"Your Host couldn't reach {door}: {_unreached(e)}. Check the address, "
                             f"that privatae-bastion pair --open is still waiting there, and that port "
                             f"{port} on the server is open to your Host.") from None
    try:
        reader, writer = await tls_server_on(sock, ctx, limit=MAX_BODY * 2)
    except Exception as e:  # noqa: BLE001
        sock.close()
        log.info("pairing with %s: handshake: %r", door, e)
        raise PairError(502, f"{door} didn't take the pairing connection. After a "
                             f"wrong code the server waits a few seconds before it takes the next "
                             f"try, so try again in a moment. If it still fails, check that "
                             f"privatae-bastion pair --open is still waiting there: it keeps a "
                             f"window open for 15 minutes.") from None
    token_id: Optional[int] = None
    machine = ""
    deadline = time.monotonic() + EXCHANGE_TIMEOUT_S

    def left() -> float:
        return max(0.1, deadline - time.monotonic())
    try:
        ssl_obj = writer.get_extra_info("ssl_object")
        if ssl_obj is None or ssl_obj.selected_alpn_protocol() != PAIR_ALPN:
            raise PairError(502, f"{door} answered, but not as a server with a pairing window open. "
                                 f"Run privatae-bastion pair --open there and try again.")
        nonce_b = nonce_h = b""
        stage = "hello"
        while True:
            try:
                path, body = await _read(reader, left())
            except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError, ssl.SSLError):
                if stage == "redeem":
                    # The bastion closes an attempt whose hello proof is wrong.
                    raise PairError(403, "The server didn't take that code. Check it against the "
                                         "one privatae-bastion pair --open shows there (each "
                                         "wrong try makes it wait longer), or open a new window.")
                raise PairError(502, f"{door} closed the pairing before it finished.")
            if path == "/v1/pair/hello" and stage == "hello":
                try:
                    nonce_b = bytes.fromhex(str(body.get("nonce") or ""))
                except ValueError:
                    nonce_b = b""
                if len(nonce_b) != 32:
                    writer.write(_reply(400, {"error": "hello needs a 32-byte nonce"}))
                    raise PairError(502, f"{door} sent a pairing request this Host can't read.")
                nonce_h = os.urandom(32)
                writer.write(_reply(200, {"v": 1, "host_handle": own, "host_name": pair_name,
                                          "nonce": nonce_h.hex(),
                                          "proof": proof(norm, "host", leaf_sha, ca_sha, nonce_b, nonce_h)}))
                await writer.drain()
                stage = "redeem"
            elif path == "/v1/pair/redeem" and stage == "redeem":
                want = proof(norm, "bastion", leaf_sha, ca_sha, nonce_b, nonce_h)
                got = str(body.get("proof") or "").lower()
                if not hmac.compare_digest(want.encode(), got.encode()):
                    writer.write(_reply(403, {"error": "this server did not prove the code"}))
                    await writer.drain()
                    raise PairError(403, f"{door} didn't prove it holds that code, so nothing was "
                                         f"signed. Check the address and the code.")
                csr = str(body.get("operator_csr") or "")
                if "CERTIFICATE REQUEST" not in csr:
                    writer.write(_reply(400, {"error": "operator_csr required"}))
                    raise PairError(502, f"{door} sent a pairing request this Host can't read.")
                machine = _plain(body.get("hostname"), 80)
                try:
                    made = await _insert_row(pool, entity_id, door=door, operator_csr=csr,
                                             hostname=machine, platform=_plain(body.get("platform"), 16))
                except host_ca.CSRRefused as e:
                    writer.write(_reply(400, {"error": str(e)}))
                    log.info("pairing with %s: certificate request refused: %s", door, e)
                    raise PairError(502, f"{door} asked your Host to vouch for it in a way it never "
                                         f"does, so nothing was signed and nothing was kept.")
                token_id = made["id"]
                writer.write(_reply(200, made["answer"]))
                await writer.drain()
                stage = "done"
            elif path == "/v1/pair/done" and stage == "redeem":
                # The server refused before it asked for anything to be
                # signed (this Host already paired there, a pairing rule of
                # the server's): its done says why, and nothing was issued.
                writer.write(_reply(200, {}))
                await writer.drain()
                raise PairError(409, done_words({"state": "refused", "machine": machine,
                                                 "error": body.get("error")}, door))
            elif path == "/v1/pair/done" and stage == "done":
                writer.write(_reply(200, {}))
                await writer.drain()
                state = str(body.get("state") or "")
                done = {"state": state, "machine": machine, "admitted_by": body.get("admitted_by"),
                        "error": body.get("error")}
                if state not in ("active", "pending"):
                    await _drop_row(pool, token_id, f"the server refused: {_plain(body.get('error'))}")
                    token_id = None
                    raise PairError(409, done_words({**done, "state": "refused"}, door))
                await _replace_older(pool, entity_id, token_id, door)
                wake()
                return {"state": state, "server": door, "machine": machine or door,
                        "host_name": _plain(body.get("host_name")) or pair_name,
                        "admitted_by": _plain(body.get("admitted_by"), 80) or None,
                        "token_id": token_id, "message": done_words(done, door)}
            else:
                writer.write(_reply(400, {"error": f"{path} is not the next step"}))
                raise PairError(502, f"{door} sent the pairing out of order; nothing was kept.")
    except PairError:
        if token_id is not None:
            await _drop_row(pool, token_id, "the pairing did not finish")
        raise
    except Exception as e:  # noqa: BLE001
        if token_id is not None:
            await _drop_row(pool, token_id, f"the pairing did not finish ({type(e).__name__})")
        log.warning("pairing with %s failed: %r", door, e)
        raise PairError(502, f"The pairing with {door} didn't finish. Nothing was kept.") from None
    except BaseException:
        # Cancelled mid-exchange (the request went away, the Host stopping):
        # the credentials it issued are dropped as on any other unfinished
        # pairing, or the row would stay and its dialler start.
        if token_id is not None:
            await asyncio.shield(_drop_row(pool, token_id, "the pairing was cancelled"))
        raise
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ── the dialler ──────────────────────────────────────────────────────────────

class Dialler:
    """The connections of one pairing whose Host connects to the server: kept
    SPARE_IDLE idle, MAX_OPEN at most, each served as if accepted on the lane
    port, redialled with backoff."""

    def __init__(self, token_id: int, door: str, serial: str, *, entity_id: int = 1,
                 pool_getter=None, lane_handler=None):
        self.token_id = token_id
        self.door = door
        self.serial = serial
        self.entity_id = entity_id
        self._pool_getter = pool_getter
        self._lane_handler = lane_handler
        self.idle = 0
        self.open = 0
        self.dials = 0
        self.failures = 0
        self._kick = asyncio.Event()
        self._writers: set = set()
        self._ctx: Optional[ssl.SSLContext] = None
        self._ctx_until: Optional[datetime.datetime] = None
        self.connected_once = False
        # The redial wait, and whether the last connection was dropped
        # straight after its handshake (QUICK_CLOSE_S): run() then waits
        # before the next dial, and the wait goes back to BACKOFF_MIN_S only
        # once a connection is used or lasts.
        self._wait = BACKOFF_MIN_S
        self._refused = False

    def _pool(self):
        if self._pool_getter is not None:
            return self._pool_getter()
        from server.network import local_bastion
        return local_bastion._control_pool()

    async def _context(self) -> ssl.SSLContext:
        """The lane's server context: a certificate for host.cseries.internal
        from this Host's CA, client certificates checked against that CA only,
        ALPN privatae-lane/1. Remade before its certificate ends."""
        from server.network import host_ca, local_bastion
        now = datetime.datetime.now(datetime.timezone.utc)
        if self._ctx is None or self._ctx_until is None or self._ctx_until - now < datetime.timedelta(days=1):
            pool = self._pool()
            leaf, key, until = await host_ca.issue_lane_server_cert(pool, [local_bastion.TCP_SERVER_NAME])
            ctx = _server_ctx(LANE_ALPN)
            host_ca.load_server_chain(ctx, leaf, key)
            ctx.load_verify_locations(cadata=await host_ca.ca_cert_pem(pool))
            ctx.verify_mode = ssl.CERT_REQUIRED
            self._ctx, self._ctx_until = ctx, until
        return self._ctx

    def _nudge(self):
        self._kick.set()

    async def _serve(self, reader, writer, conn: dict):
        """One lane connection, after the handshake: the bastion's certificate
        must be THIS pairing's, then the lane port's handler serves it."""
        from server.network import local_bastion
        self._writers.add(writer)
        try:
            if local_bastion.peer_cert_serial(writer) != self.serial:
                log.info("server %s presented another certificate than its pairing's; closed", self.door)
                return
            handler = self._lane_handler or local_bastion.make_tcp_lane_handler(self._pool_getter, self.entity_id)
            await handler(reader, writer)
        finally:
            self._writers.discard(writer)
            try:
                writer.close()
            except Exception:
                pass
            self.open -= 1
            if not conn["used"]:
                conn["used"] = True
                self.idle -= 1
            if conn["spoke"] or time.monotonic() - conn["at"] >= QUICK_CLOSE_S:
                self._wait = BACKOFF_MIN_S
            else:
                self._refused = True
            self._nudge()

    async def _dial_one(self) -> bool:
        host, _, port = self.door.rpartition(":")
        host = host.strip("[]")
        self.dials += 1
        try:
            ctx = await self._context()
            sock = await _connect(host, int(port))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("server %s: dial failed: %r", self.door, e)
            return False
        loop = asyncio.get_running_loop()
        # used: the bastion has spoken on it (it is no longer idle), counted
        # once whichever comes first, its first byte or its close.
        conn = {"counted": False, "used": False, "spoke": False, "at": time.monotonic()}

        def first():
            if conn["counted"] and not conn["used"]:
                conn["used"] = True
                conn["spoke"] = True
                self.idle -= 1
                self._nudge()

        def connected(reader, writer):
            ssl_obj = writer.get_extra_info("ssl_object")
            if ssl_obj is None or ssl_obj.selected_alpn_protocol() != LANE_ALPN:
                writer.close()
                return None
            conn["counted"] = True
            conn["at"] = time.monotonic()
            self.open += 1
            self.idle += 1
            return self._serve(reader, writer, conn)
        reader = _FirstData(first, limit=_stream_limit(), loop=loop)
        try:
            transport, _ = await loop.connect_accepted_socket(
                lambda: asyncio.StreamReaderProtocol(reader, connected, loop=loop),
                sock=sock, ssl=ctx, ssl_handshake_timeout=HANDSHAKE_TIMEOUT_S)
        except asyncio.CancelledError:
            sock.close()
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("server %s: handshake failed: %r", self.door, e)
            sock.close()
            return False
        if not conn["counted"]:
            transport.close()
            return False
        if not self.connected_once:
            self.connected_once = True
            log.info("connected to server %s (token_id=%s)", self.door, self.token_id)
        return True

    async def _back_off(self):
        await asyncio.sleep(self._wait * random.uniform(0.8, 1.2))
        self._wait = min(2 * self._wait, BACKOFF_MAX_S)

    async def run(self):
        self._wait = BACKOFF_MIN_S
        try:
            while True:
                if self._refused:
                    self._refused = False
                    self.failures += 1
                    await self._back_off()
                    continue
                if self.idle >= SPARE_IDLE or self.open >= MAX_OPEN:
                    self._kick.clear()
                    try:
                        await asyncio.wait_for(self._kick.wait(), 1.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                if await self._dial_one():
                    # The wait goes back to BACKOFF_MIN_S when this connection
                    # is used or lasts (_serve), not at the handshake: a server
                    # that drops it at once is backed off like a failed dial.
                    self.failures = 0
                    continue
                self.failures += 1
                await self._back_off()
        finally:
            for w in list(self._writers):
                try:
                    w.close()
                except Exception:
                    pass


def _stream_limit() -> int:
    from server import mtls_frontdoor
    return mtls_frontdoor.STREAM_LIMIT


_DIALLERS: dict[int, tuple[Dialler, asyncio.Task]] = {}
_WAKE: Optional[asyncio.Event] = None

_ROWS_SQL = (
    "SELECT id, notes->>'door' AS door, notes->>'cert_serial' AS serial FROM mcp_client_tokens "
    "WHERE entity_id = $1 AND operator_kind = 'bastion' AND notes->>'transport' = $2 "
    "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())")


def wake() -> None:
    """Read the pairings again now (a pairing was just made)."""
    if _WAKE is not None:
        _WAKE.set()


def diallers() -> dict[int, Dialler]:
    return {k: v[0] for k, v in _DIALLERS.items()}


async def reconcile(pool, entity_id: int, *, pool_getter=None, lane_handler=None) -> None:
    """Start a dialler for every pairing of this kind, stop the rest."""
    rows = await pool.fetch(_ROWS_SQL, entity_id, TRANSPORT)
    want = {int(r["id"]): (r["door"], r["serial"]) for r in rows if r["door"] and r["serial"]}
    for tid in list(_DIALLERS):
        d, t = _DIALLERS[tid]
        if want.get(tid) != (d.door, d.serial) or t.done():
            t.cancel()
            del _DIALLERS[tid]
    for tid, (door, serial) in want.items():
        if tid not in _DIALLERS:
            d = Dialler(tid, door, serial, entity_id=entity_id, pool_getter=pool_getter,
                        lane_handler=lane_handler)
            _DIALLERS[tid] = (d, asyncio.ensure_future(d.run()))


async def supervise(entity_id: int = 1) -> None:
    """For the life of the process, on a standalone Host: a dialler for each
    pairing whose Host connects to the server."""
    global _WAKE
    from server.network import local_bastion
    _WAKE = asyncio.Event()
    try:
        while True:
            pool = local_bastion._control_pool()
            if pool:
                try:
                    await reconcile(pool, entity_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning("server diallers: %s: %s", type(e).__name__, e)
            _WAKE.clear()
            try:
                await asyncio.wait_for(_WAKE.wait(), RECONCILE_S)
            except asyncio.TimeoutError:
                pass
    finally:
        for d, t in _DIALLERS.values():
            t.cancel()
        _DIALLERS.clear()


# ── asked in chat: the card ──────────────────────────────────────────────────

# The person tells their Host the server's address and the code. The Host
# never pairs on its own word: it files this card over the chat, and only the
# person's Connect runs pair_with_server (tickets.decide_ticket_service calls
# decide_card BEFORE the card is decided, so a refusal leaves it up with the
# words). Words on a web page or in a document the Host read can ask for the
# card; only the person can press it.
SERVER_CARD_KIND = "server_pair"
# The cards whose Connect is running now, so a second press waits for nothing
# and pairs nothing (one process: server/launch.py runs one worker).
_CARDS_CONNECTING: set = set()
CARD_CONNECT = "connect"
CARD_CANCEL = "cancel"


def card_words(door: str) -> tuple[str, list[dict]]:
    sitrep = (f"Connect your Host to the server at {door}?\n"
              f"Your Host will connect to that server with the code you gave it, and from then on "
              f"keep connecting to it whenever it runs. The server can then do work for your Host, "
              f"and only what you allow it. Connect only if you just ran "
              f"privatae-bastion pair --open on that server yourself.")
    options = [
        {"id": CARD_CONNECT, "label": "Connect", "consequence": f"Your Host pairs with {door} now.",
         "recommend": True},
        {"id": CARD_CANCEL, "label": "Don't connect", "consequence": "Nothing is paired.",
         "recommend": False},
    ]
    return sitrep, options


async def ask_to_pair(pool, entity_id: int, address: Any, code: Any) -> dict:
    """The Host's ask (manage_bastion op='add_server'): check the address and
    the code, then file the card for the person. Nothing connects here."""
    if normalize_code(code) is None:
        raise PairError(400, "That isn't the code: it is 20 letters and numbers, as "
                             "privatae-bastion pair --open showed it on the server.")
    host, port = parse_address(address)
    door = door_label(host, port)
    sitrep, options = card_words(door)
    from server.routes import tickets as _tk
    t = await _tk.create_ticket_service(
        pool, entity_id, source="runtime", sitrep=sitrep, options=options, priority=70,
        metadata={"kind": SERVER_CARD_KIND,
                  SERVER_CARD_KIND: {"address": door, "code": normalize_code(code)},
                  "user_only": True, "no_pattern": True})
    tid = t.get("id") if isinstance(t, dict) else None
    return {"asked": True, "ticket_id": tid, "server": door,
            "message": f"Your person was asked on a card over the chat whether to connect to {door}. "
                       f"Their answer reaches you as the answer to that card, with what the server "
                       f"said. There is nothing for you to approve."}


async def decide_card(pool, eid: int, ticket_id: int, md: dict, *, option_id: str, actor: str) -> dict:
    """The person's answer on the card: Connect pairs now, with the address and
    code the card holds; Don't connect does nothing. Raises PairError with the
    words for the card when it did not pair. The code is cleared from the card
    once it paired, or when the person chose not to connect; after a refusal
    it stays, so Connect can be pressed again (the server's own window bounds
    the tries). A second press while the first is still connecting is told
    so, and does nothing."""
    if actor != "user":
        raise PairError(403, "this card is the person's to answer")
    if ticket_id in _CARDS_CONNECTING:
        raise PairError(409, "Your Host is already connecting to that server; its answer comes on this card.")
    sp = md.get(SERVER_CARD_KIND) if isinstance(md.get(SERVER_CARD_KIND), dict) else {}
    if option_id == CARD_CANCEL:
        await _clear_code(pool, ticket_id)
        return {"connected": False, "message": "Nothing was paired."}
    if option_id != CARD_CONNECT:
        raise PairError(400, f"option_id must be {CARD_CONNECT} or {CARD_CANCEL}")
    if not sp.get("code"):
        raise PairError(409, "This card's code was already used. Run privatae-bastion pair --open "
                             "on the server again and give your Host the new code.")
    _CARDS_CONNECTING.add(ticket_id)
    try:
        res = await pair_with_server(pool, eid, sp.get("address"), sp.get("code"))
    finally:
        _CARDS_CONNECTING.discard(ticket_id)
    await _clear_code(pool, ticket_id)
    return {"connected": True, **{k: res.get(k) for k in ("state", "server", "machine", "admitted_by",
                                                           "token_id", "message")}}


async def _clear_code(pool, ticket_id: int) -> None:
    try:
        await pool.execute(
            "UPDATE entity_tickets SET metadata = jsonb_set(metadata, $2::text[], 'null'::jsonb) "
            "WHERE id = $1 AND metadata ? $3", int(ticket_id), "{%s,code}" % SERVER_CARD_KIND,
            SERVER_CARD_KIND)
    except Exception as e:  # noqa: BLE001
        log.info("server card %s: code not cleared: %r", ticket_id, e)
