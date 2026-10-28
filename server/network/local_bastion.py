"""A standalone Host and the bastion on its own machine.

With no platform there is no WireGuard tunnel and no platform CA. The Host and
the bastion meet in one directory on the machine, bind-mounted into the Host
container at the same path (compose.yaml; created on the machine by
etc/tmpfiles.d/privatae-host.conf):

  enroll.sock      served here (plain HTTP): the bastion's `pair` redeems its
                   pairing token (bastion_enroll.redeem_local). The Host's own
                   CA (host_ca) signs the operator CSR.
  lane.sock        served here (HTTPS, mutual TLS): the same front door as
                   :8443 (server/mtls_frontdoor.handle_client). Server cert
                   from the Host CA; client certs verified against the Host CA
                   only. It forwards to the dashboard on loopback with the
                   verified CN, so the /bastion-harness workers, the bearer
                   checks and the heartbeat run unchanged behind it.
  runs/<id>.sock   served by the bastion: one coder run's control plane. The
                   Host connects with open_run_connection / run_connector.

When the Host runs in a VM and the bastion on the machine outside it, the two
share no kernel and no directory, so the Host also serves the same two doors
on TCP, published on the machine's loopback only (compose.yaml):

  :8810 (enroll)   HTTPS, one route: POST /v1/local/redeem, the same redeem
                   with transport "tcp". Server certificate from the Host CA
                   with the DNS name host.cseries.internal (TCP_SERVER_NAME);
                   the bastion trusts that CA alone, handed to it with the
                   token by `python3 -m cli.cseries bastion pair-token`.
  :8811 (lane)     mutual TLS under the Host CA, server certificate as above,
                   then the same front door as lane.sock. A client
                   certificate must be one this Host issued at a redeem and
                   has not revoked: its serial finds the bastion's row in
                   mcp_client_tokens, and no row means no lane. The
                   certificate is the bastion's identity here, where on the
                   sockets it was the directory's owner and group.

A Host on a server of its own is reached on the same two ports from other
machines, at the name CSERIES_LANE_HOST gives (lane_host): the redeem URL and
the lane address name it instead of the loopback. The server's firewall, not
this process, decides who reaches them; the enroll port is opened to one
address at a time, for the pairing window the owner asked for
(server/network/door_helper.py).

Nothing here runs for a Host with a platform: run_supervised checks
standalone() first, and the platform's redeem (bastion_enroll.redeem) is not
touched. Nothing a bastion runs reaches the Host's :8800 listener through
these doors: the enroll doors answer one route in this process, and the
lane doors forward only requests on a client cert this Host's CA signed,
which the kernel then holds to the bastion's paths (localhost_only).
"""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import logging
import os
import re
import socket
import stat
import sys
import time
from typing import Optional

from cryptography import x509
from fastapi import HTTPException

log = logging.getLogger("privatae.network.local_bastion")

DEFAULT_DIR = "/run/privatae-host"
# For /api/system/health/deep: "init", "platform" (not used), "no-shared-dir",
# "listening", "crashed: ...". STATE is the two sockets, TCP_STATE the two
# ports; each is supervised on its own, so one failing leaves the other up.
STATE = "init"
TCP_STATE = "init"

# The two ports in the container, on every address of its own network
# namespace; compose.yaml publishes them on the machine's 127.0.0.1 only.
TCP_BIND = "0.0.0.0"
ENROLL_PORT = 8810
LANE_PORT = 8811
# The ports on the machine's loopback, unless CSERIES_ENROLL_PORT and
# CSERIES_LANE_PORT say compose.yaml published them elsewhere.
PUBLISHED_ENROLL_PORT = 8810
PUBLISHED_LANE_PORT = 8811
TCP_REDEEM_PATH = "/v1/local/redeem"
# The one name on both ports' server certificates, and the name the bastion
# checks on them (client-go pair_tcp.go). Not this Host's handle: a bastion
# reaches the ports on an address of the machine, never by the Host's name.
TCP_SERVER_NAME = "host.cseries.internal"

# client-go/coders.go coderRunIDRe: the only run ids the bastion makes.
_RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{6,61}[a-z0-9]")
# The bastion runs as root and is the only writer of runs/ (0750 root). A
# socket there owned by anyone else is not the bastion's.
RUN_SOCKET_OWNER_UID = 0

MAX_BODY = 64 * 1024
READ_TIMEOUT = 10
# Every path a bastion may POST its redeem to: the design's, today's Host
# route, and the path client-go/pair.go builds on its redeem URL today.
_REDEEM_PATHS = frozenset({"/bastion/enroll/redeem", "/api/network/bastions/redeem"})
_V1_REDEEM_RE = re.compile(r"/v1/bastions/[a-z0-9]{1,37}/redeem")


# ── where things are ─────────────────────────────────────────────────────────

def shared_dir() -> str:
    return os.environ.get("PRIVATAE_HOST_DIR", "").strip() or DEFAULT_DIR


def enroll_socket() -> str:
    return os.path.join(shared_dir(), "enroll.sock")


def lane_socket() -> str:
    return os.path.join(shared_dir(), "lane.sock")


def runs_dir() -> str:
    """Where the bastion puts one control socket per coder run."""
    return os.path.join(shared_dir(), "runs")


def _published_port(var: str, default: int) -> int:
    raw = os.environ.get(var, "").strip()
    try:
        port = int(raw) if raw else default
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        log.warning("%s=%r is not a port; using %d", var, raw, default)
        return default
    return port


_LANE_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_lane_host_warned: set = set()


def _lane_host_setting() -> Optional[str]:
    """CSERIES_LANE_HOST in the form a URL and a host:port carry it (an IPv6
    address in brackets), or None when it is unset or is neither a host name
    nor an IP address."""
    raw = os.environ.get("CSERIES_LANE_HOST", "").strip().lower()
    if not raw:
        return None
    try:
        ip = ipaddress.ip_address(raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw)
        return f"[{ip}]" if ip.version == 6 else str(ip)
    except ValueError:
        pass
    if len(raw) <= 253 and all(_LANE_LABEL_RE.fullmatch(lb) for lb in raw.split(".")):
        return raw
    if raw not in _lane_host_warned:
        _lane_host_warned.add(raw)
        log.warning("CSERIES_LANE_HOST=%r is not a host name or an IP address; using 127.0.0.1", raw)
    return None


def remote_lane() -> bool:
    """Whether bastions on other machines reach this Host's pairing ports
    (CSERIES_LANE_HOST is set): a Host on a server of its own, not in a VM on
    the bastion's machine."""
    return _lane_host_setting() is not None


def lane_host() -> str:
    """Where a bastion reaches the enroll and lane ports: CSERIES_LANE_HOST,
    the Host's public name on a server of its own, or else the machine's
    loopback, where compose.yaml publishes them. The redeem URL and the lane
    address always name the same host: the bastion refuses a lane on any host
    but the one its redeem URL named."""
    return _lane_host_setting() or "127.0.0.1"


def enroll_url() -> str:
    """The redeem URL on the enroll port (CSERIES_ENROLL_PORT, default 8810)
    at lane_host()."""
    return f"https://{lane_host()}:{_published_port('CSERIES_ENROLL_PORT', PUBLISHED_ENROLL_PORT)}{TCP_REDEEM_PATH}"


def lane_address() -> str:
    """The lane's address, as host:port (CSERIES_LANE_PORT, default 8811) at
    lane_host(). The redeem on the enroll port answers it."""
    return f"{lane_host()}:{_published_port('CSERIES_LANE_PORT', PUBLISHED_LANE_PORT)}"


def pairing_host_name(handle: str) -> str:
    """THE name of this Host to a bastion on its machine, from one place: the
    redeem answer's host_name (the bastion files the pairing under it), the
    first DNS name on the lane certificate (the bastion's TLS ServerName), and
    the `subject` a coder grant names."""
    from server.network import bastion_enroll
    return bastion_enroll._host_name(handle)


async def host_name(entity_id: int = 1) -> str:
    """pairing_host_name for this Host's own handle."""
    from server.network import bastion_enroll
    handle = await bastion_enroll.resolve_self_handle(entity_id)
    if not handle:
        raise RuntimeError("this Host has no handle")
    return pairing_host_name(handle)


def run_socket_path(run: str) -> str:
    """`<runs_dir>/<run_id>.sock` for a run id the bastion could have made, or
    the same path as a run-up answer's `control_socket` gives it. Raises
    ValueError for anything else, so neither can name a path outside runs/."""
    if not isinstance(run, str):
        raise ValueError("a run id or a control_socket path is required")
    run_id = run
    if "/" in run:
        if os.path.dirname(run) != runs_dir() or not run.endswith(".sock"):
            raise ValueError(f"control_socket {run!r} is not a socket directly in {runs_dir()}")
        run_id = os.path.basename(run)[:-len(".sock")]
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run_id must be 8-63 of a-z, 0-9 and '-', starting and ending "
                         "with a letter or digit")
    return os.path.join(runs_dir(), run_id + ".sock")


def _checked_run_socket(run: str) -> str:
    path = run_socket_path(run)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise FileNotFoundError(f"no control socket at {path} (is the run up?)") from None
    if not stat.S_ISSOCK(st.st_mode):
        raise PermissionError(f"{path} is not a socket")
    if st.st_uid != RUN_SOCKET_OWNER_UID:
        raise PermissionError(f"{path} is not the bastion's (owner uid {st.st_uid})")
    return path


async def open_run_connection(run: str, *, timeout: float = 10.0):
    """(reader, writer) on one coder run's control socket. `run` is the run id
    or the run-up answer's `control_socket`. The caller speaks the run's
    control plane over it (/health, /check, /workspace/*) with the run's
    control cap, as it does over the tunnel port on the platform; a unix
    pairing's `tunnel_port` means nothing."""
    path = _checked_run_socket(run)
    return await asyncio.wait_for(asyncio.open_unix_connection(path), timeout)


def run_connector(run: str):
    """An aiohttp connector on one coder run's control socket (`run` as for
    open_run_connection): `aiohttp.ClientSession(connector=run_connector(r))`,
    then request `http://run/<path>`; the URL's host is not used for routing."""
    import aiohttp
    return aiohttp.UnixConnector(path=_checked_run_socket(run))


# ── platform or not ──────────────────────────────────────────────────────────

def _bridge_client():
    # Read from the running app, never imported here: outside a running Host
    # (a tool, a test) there is no platform client and so no standalone Host,
    # and importing server.main for the answer would load the whole app.
    m = sys.modules.get("server.main")
    get = getattr(m, "get_bridge_client", None)
    return get() if callable(get) else None


def standalone() -> bool:
    """True on a Host with no platform to pair its bastion through: its
    platform client is the NoPlatform stand-in, or the network's member door
    (server/bridge/member_client.py), whose ops include no bastion enrolment
    and no handle registry. A Host with a platform, connected or not, is never
    standalone, so its pairing keeps the bridge path."""
    try:
        from server.bridge.member_client import MemberClient
        from server.bridge.no_platform import NoPlatform
        return isinstance(_bridge_client(), (NoPlatform, MemberClient))
    except Exception:
        return False


# ── unix sockets ─────────────────────────────────────────────────────────────

def _take_socket_path(path: str) -> None:
    """Make `path` free to bind: remove a stale socket left by an earlier
    start. Refuse a path that is not a socket, or one a live server answers
    on (another Host on this machine), rather than take it over."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise RuntimeError(f"{path} exists and is not a socket; not removing it")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1)
    try:
        probe.connect(path)
    except OSError:
        os.unlink(path)  # nobody answers: ours, from an earlier start
        return
    finally:
        probe.close()
    raise RuntimeError(f"{path} is served by another process; not taking it over")


def _share_socket(path: str) -> None:
    """0660 and the shared directory's group: the bastion (root, or a member
    of privatae-host) can connect, nobody else can."""
    os.chmod(path, 0o660)
    try:
        gid = os.stat(os.path.dirname(path)).st_gid
        if os.stat(path).st_gid != gid:
            os.chown(path, -1, gid)
    except OSError as e:
        log.info("could not give %s its directory's group: %s", path, e)


def _dir_usable(d: str) -> bool:
    return os.path.isdir(d) and os.access(d, os.W_OK | os.X_OK)


# ── enroll.sock ──────────────────────────────────────────────────────────────

_REASONS = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
            405: "Method Not Allowed", 409: "Conflict", 411: "Length Required",
            413: "Payload Too Large", 500: "Internal Server Error",
            503: "Service Unavailable"}


def _response(status: int, body: dict) -> bytes:
    raw = json.dumps(body).encode()
    return (f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n"
            "Connection: close\r\n\r\n").encode() + raw


def _error(status: int, detail: str) -> bytes:
    # client-go/pair.go reads `error` from a refusal; FastAPI clients read `detail`.
    return _response(status, {"error": detail, "detail": detail})


def _unix_redeem_path(target: str) -> bool:
    return target in _REDEEM_PATHS or bool(_V1_REDEEM_RE.fullmatch(target))


def _tcp_redeem_path(target: str) -> bool:
    return target == TCP_REDEEM_PATH


async def _read_request(reader: asyncio.StreamReader,
                        path_ok=_unix_redeem_path) -> tuple[Optional[bytes], Optional[dict]]:
    """(an error response, None) or (None, the JSON body) for one request.
    An empty error response means the client went away: send nothing."""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT)
    except asyncio.LimitOverrunError:
        return _error(413, "request head too large"), None
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        return b"", None
    lines = head[:-4].split(b"\r\n")
    parts = lines[0].split(b" ")
    if len(parts) != 3 or not parts[2].startswith(b"HTTP/1."):
        return _error(400, "bad request line"), None
    method, target = parts[0], parts[1].decode("latin-1").split("?", 1)[0]
    headers: dict[bytes, list[bytes]] = {}
    for ln in lines[1:]:
        name, sep, val = ln.partition(b":")
        if not sep:
            return _error(400, "bad header line"), None
        headers.setdefault(name.strip().lower(), []).append(val.strip())
    if not path_ok(target):
        return _error(404, "this listener serves the bastion redeem only"), None
    if method != b"POST":
        return _error(405, "redeem is POST"), None
    if b"transfer-encoding" in headers:
        return _error(411, "send Content-Length, not Transfer-Encoding"), None
    cl = headers.get(b"content-length") or []
    if len(cl) != 1 or not cl[0].isdigit():
        return _error(411, "one Content-Length is required"), None
    n = int(cl[0])
    if n > MAX_BODY:
        return _error(413, "request body too large"), None
    try:
        raw = await asyncio.wait_for(reader.readexactly(n), READ_TIMEOUT)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        return b"", None
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return _error(400, "body is not JSON"), None
    if not isinstance(body, dict):
        return _error(400, "body must be a JSON object"), None
    return None, body


def _control_pool():
    from server import brain_service
    return brain_service.get_control_pool()


def make_enroll_handler(pool_getter=None, entity_id: int = 1, transport: str = "unix"):
    """The redeem, for enroll.sock (transport "unix") or the enroll port
    ("tcp"): the port serves TCP_REDEEM_PATH alone and answers the lane's
    address instead of the socket paths."""
    path_ok = _tcp_redeem_path if transport == "tcp" else _unix_redeem_path

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        from server.network import bastion_enroll
        try:
            err, body = await _read_request(reader, path_ok)
            if err is not None:
                if err:
                    writer.write(err)
                return
            pool = (pool_getter or _control_pool)()
            if not pool:
                writer.write(_error(503, "brain pool not ready"))
                return
            try:
                out = await bastion_enroll.redeem_local(
                    pool, entity_id=entity_id,
                    handle=str(body.get("handle") or ""),
                    secret=str(body.get("secret") or ""),
                    operator_csr=str(body.get("operator_csr") or ""),
                    hostname=str(body.get("hostname") or ""),
                    platform=str(body.get("platform") or ""),
                    transport=transport,
                )
                writer.write(_response(200, out))
            except HTTPException as e:
                writer.write(_error(e.status_code, str(e.detail)))
            except Exception:
                log.exception("local redeem failed")
                writer.write(_error(500, "redeem failed on the Host; see its log"))
        finally:
            try:
                await writer.drain()
            except Exception:
                pass
            writer.close()
    return handle


async def start_enroll_server(path: str, pool_getter=None, entity_id: int = 1):
    _take_socket_path(path)
    server = await asyncio.start_unix_server(make_enroll_handler(pool_getter, entity_id),
                                             path=path, limit=MAX_BODY)
    _share_socket(path)
    return server


# ── lane.sock ────────────────────────────────────────────────────────────────

async def start_lane_server(path: str, ctx):
    """The :8443 front door's handler on a unix socket, under `ctx`."""
    from server import mtls_frontdoor
    _take_socket_path(path)
    server = await asyncio.start_unix_server(mtls_frontdoor.handle_client, path=path,
                                             ssl=ctx, limit=mtls_frontdoor.STREAM_LIMIT)
    _share_socket(path)
    return server


async def lane_names(entity_id: int) -> list[str]:
    """The DNS names on the lane certificate: pairing_host_name first (what
    the bastion checks), then every other name this Host answers to and the
    bare handle."""
    from server.network import bastion_enroll
    handle = await bastion_enroll.resolve_self_handle(entity_id)
    if not handle:
        raise RuntimeError("this Host has no handle")
    return list(dict.fromkeys([pairing_host_name(handle)]
                              + list(bastion_enroll._host_names(handle)) + [handle]))


# ── the enroll and lane ports ────────────────────────────────────────────────

# The bastion a lane-port client certificate belongs to: the row its redeem
# wrote (bastion_enroll.redeem_local notes.cert_serial), still not revoked and
# not expired. Any transport's row: one Host, one CA, one bastion per row.
_LANE_ROW_SQL = (
    "SELECT id, name FROM mcp_client_tokens "
    "WHERE entity_id = $1 AND operator_kind = 'bastion' "
    "AND notes->>'cert_serial' = $2 AND revoked_at IS NULL "
    "AND (expires_at IS NULL OR expires_at > now())")

# Serials already logged as connected by this process, so a bastion's first
# lane connection is logged and its next hundred are not.
_lane_logged: set[str] = set()

# The lane-port connections open now, by the bastion row each was admitted
# for, so a revoke closes them (close_lanes) the way the platform's revoke
# drops the bastion's WireGuard peer. The row lookup admits a connection when
# it opens; without this a revoked bastion would keep every lane it already
# held. _closed_at is when close_lanes last ran for a row, so a connection
# whose lookup began before that (and so may have read the row before the
# revoke) is closed as soon as it registers; one looked up later went by the
# database, which is the authority.
_lane_conns: dict[int, set] = {}
_closed_at: dict[int, float] = {}


def close_lanes(token_id: int) -> int:
    """Close every lane-port connection bastion row `token_id` holds, and
    any whose lookup was already under way. Returns how many."""
    _closed_at[token_id] = time.monotonic()
    conns = _lane_conns.pop(token_id, set())
    for w in conns:
        try:
            w.close()
        except Exception:
            pass
    if conns:
        log.info("lane port: closed %d connection(s) of revoked bastion token_id=%s",
                 len(conns), token_id)
    return len(conns)


def peer_cert_serial(writer: asyncio.StreamWriter) -> Optional[str]:
    """The serial of the client certificate the TLS handshake verified, in
    the hex host_ca.sign_operator_csr stores; None when there is none."""
    ssl_obj = writer.get_extra_info("ssl_object")
    der = ssl_obj.getpeercert(binary_form=True) if ssl_obj is not None else None
    if not der:
        return None
    try:
        return format(x509.load_der_x509_certificate(der).serial_number, "x")
    except Exception:
        return None


def make_tcp_lane_handler(pool_getter=None, entity_id: int = 1):
    """The lane port's door: the handshake has already held the client
    certificate to the Host CA; this finds the bastion it was issued to, and
    only then hands the connection to the :8443 front door's handler."""
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        from server import mtls_frontdoor
        serial = peer_cert_serial(writer)
        row = None
        looked_at = time.monotonic()
        if serial:
            pool = (pool_getter or _control_pool)()
            if pool:
                try:
                    row = await pool.fetchrow(_LANE_ROW_SQL, entity_id, serial)
                except Exception as e:
                    log.warning("lane port: could not look the client certificate up (%s: %s)",
                                type(e).__name__, e)
        if row is None:
            log.info("lane port: refused a client certificate this Host did not issue at a "
                     "redeem, or has revoked (serial %s)", serial)
            try:
                writer.close()
            except Exception:
                pass
            return
        if serial not in _lane_logged:
            _lane_logged.add(serial)
            log.info("bastion connected on the lane port: token_id=%s name=%s serial=%s",
                     row["id"], row["name"], serial)
        rid = row["id"]
        _lane_conns.setdefault(rid, set()).add(writer)
        try:
            if _closed_at.get(rid, float("-inf")) >= looked_at:
                writer.close()  # revoked while its lookup was under way
                return
            await mtls_frontdoor.handle_client(reader, writer)
        finally:
            held = _lane_conns.get(rid)
            if held is not None:
                held.discard(writer)
                if not held:
                    _lane_conns.pop(rid, None)
    return handle


async def start_tcp_enroll_server(ctx, pool_getter=None, entity_id: int = 1,
                                  host: Optional[str] = None, port: Optional[int] = None):
    """The redeem on the enroll port, HTTPS under `ctx`."""
    return await asyncio.start_server(
        make_enroll_handler(pool_getter, entity_id, transport="tcp"),
        TCP_BIND if host is None else host, ENROLL_PORT if port is None else port,
        ssl=ctx, limit=MAX_BODY)


async def start_tcp_lane_server(ctx, pool_getter=None, entity_id: int = 1,
                                host: Optional[str] = None, port: Optional[int] = None):
    """The lane on the lane port, mutual TLS under `ctx`."""
    from server import mtls_frontdoor
    return await asyncio.start_server(
        make_tcp_lane_handler(pool_getter, entity_id),
        TCP_BIND if host is None else host, LANE_PORT if port is None else port,
        ssl=ctx, limit=mtls_frontdoor.STREAM_LIMIT)


# ── the supervisor ───────────────────────────────────────────────────────────

def _renew_wait(not_after: datetime.datetime) -> float:
    """Seconds to sleep before looking at a server certificate again."""
    from server.network import host_ca
    left = not_after - datetime.datetime.now(datetime.timezone.utc)
    wait = (left - host_ca.LANE_RENEW_BEFORE).total_seconds()
    return max(60.0, min(wait, 12 * 3600.0))


async def _renew_due(pool, certs: list, names: list[str], what: str) -> None:
    """Renew each [ctx, not_after, chain_ca] in `certs` that is within the
    renewal window. chain_ca is true for the enroll port, whose chain carries
    the Host CA after the leaf (host_ca.enroll_server_ssl_context)."""
    from server.network import host_ca
    for c in certs:
        if c[1] - datetime.datetime.now(datetime.timezone.utc) <= host_ca.LANE_RENEW_BEFORE:
            c[1] = await host_ca.renew_lane_cert(pool, c[0], names, chain_ca=c[2])
            log.info("%s certificate renewed until %s", what, c[1].isoformat())


async def _serve_once(pool, entity_id: int) -> None:
    global STATE
    from server.network import host_ca
    names = await lane_names(entity_id)
    ctx, not_after = await host_ca.lane_server_ssl_context(pool, names)
    enroll = await start_enroll_server(enroll_socket(), entity_id=entity_id)
    try:
        lane = await start_lane_server(lane_socket(), ctx)
    except Exception:
        enroll.close()
        raise
    STATE = "listening"
    log.info("local bastion sockets up: %s (redeem), %s (lane, Host CA)",
             enroll_socket(), lane_socket())
    certs = [[ctx, not_after, False]]
    try:
        while True:
            await asyncio.sleep(_renew_wait(certs[0][1]))
            await _renew_due(pool, certs, names, "lane socket")
    finally:
        for s in (enroll, lane):
            s.close()
        for p in (enroll_socket(), lane_socket()):
            try:
                os.unlink(p)
            except OSError:
                pass


async def _serve_tcp_once(pool, entity_id: int) -> None:
    global TCP_STATE
    from server.network import host_ca
    names = [TCP_SERVER_NAME]
    enroll_ctx, enroll_until = await host_ca.enroll_server_ssl_context(pool, names)
    lane_ctx, lane_until = await host_ca.lane_server_ssl_context(pool, names)
    enroll = await start_tcp_enroll_server(enroll_ctx, entity_id=entity_id)
    try:
        lane = await start_tcp_lane_server(lane_ctx, entity_id=entity_id)
    except Exception:
        enroll.close()
        raise
    TCP_STATE = "listening"
    log.info("local bastion ports up: %s:%d (redeem on %s), %s:%d (lane, Host CA), "
             "certificate name %s", TCP_BIND, ENROLL_PORT, TCP_REDEEM_PATH, TCP_BIND, LANE_PORT,
             TCP_SERVER_NAME)
    certs = [[enroll_ctx, enroll_until, True], [lane_ctx, lane_until, False]]
    try:
        while True:
            await asyncio.sleep(_renew_wait(min(c[1] for c in certs)))
            await _renew_due(pool, certs, names, "enroll and lane port")
    finally:
        enroll.close()
        lane.close()


async def _supervise_sockets(entity_id: int) -> None:
    global STATE
    from server.network import host_ca
    said_dir = False
    while True:
        try:
            pool = _control_pool()
            if not pool:
                await asyncio.sleep(2)
                continue
            await host_ca.ensure(pool)
            d = shared_dir()
            if not _dir_usable(d):
                STATE = "no-shared-dir"
                if not said_dir:
                    log.info("local bastion: %s is missing or not writable by this Host; "
                             "no bastion can pair on its sockets until it is (see "
                             "etc/tmpfiles.d/privatae-host.conf)", d)
                    said_dir = True
                await asyncio.sleep(30)
                continue
            said_dir = False
            await _serve_once(pool, entity_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            STATE = f"crashed: {type(e).__name__}: {e}"
            log.warning("local bastion sockets failed (%s: %s); retrying in 5s",
                        type(e).__name__, e)
            await asyncio.sleep(5)


async def _supervise_ports(entity_id: int) -> None:
    global TCP_STATE
    from server.network import host_ca
    while True:
        try:
            pool = _control_pool()
            if not pool:
                await asyncio.sleep(2)
                continue
            await host_ca.ensure(pool)
            await _serve_tcp_once(pool, entity_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            TCP_STATE = f"crashed: {type(e).__name__}: {e}"
            log.warning("local bastion ports failed (%s: %s); retrying in 5s",
                        type(e).__name__, e)
            await asyncio.sleep(5)


async def run_supervised(entity_id: int = 1) -> None:
    """For the life of the process, on a standalone Host only: make the Host
    CA (first boot), then serve enroll.sock and lane.sock while the shared
    directory is there and writable, and the enroll and lane ports always.
    Each pair restarts after a crash on its own, like the :8443 door, and the
    sockets wait for the directory instead of giving up."""
    global STATE, TCP_STATE
    if not standalone():
        STATE = TCP_STATE = "platform"
        return
    await asyncio.gather(_supervise_sockets(entity_id), _supervise_ports(entity_id))
