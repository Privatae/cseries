"""Remote access on this machine: turning it on, keeping the tunnel up, where
the tunnel hands browsers over, and what Settings shows about it.

A member opens Settings -> Remote access on the machine and turns it on. Then,
in this process:

  1. The Host makes its own ACME account (server/network/remote_acme.py); the
     key is made here and never leaves the machine.
  2. It asks the member door to turn remote access on, with that account's URL
     (remote.enable {acme_account_uri}). The door draws the Host's address ID
     once and asks the platform for its DNS: a CAA record pinned to that
     account, the acme-dns challenge name, and the wildcard name pointed at
     the network's edge. Until the records exist the door answers dns
     "wanted", and the Host asks again every ENABLE_POLL_S.
  3. The first answer after the records exist carries the acme-dns
     credential, ONCE. It is written to disk before anything else is done with
     the answer (store_credential), 0600, into the directory the certificate
     job reads (CSERIES_REMOTE_ACME_DIR, the job's /etc/cseries-cloud), with
     cloud.json and acme-dns-api.json beside it, as on a cloud box. A Host
     that lost it asks again with reissue_acme_dns. So does a Host whose
     credential acme-dns refuses (401, which the certificate job reports:
     acme_dns_refused for the acme-dns.json it read): an account pinned to
     the addresses of a Cloud machine the Host has since left never works
     again. At once, then at most once every ACME_DNS_REISSUE_S, whatever
     the job's status says in between. It is never logged and never shown.
  4. The address (https://<handle>.<address_id>.<zone>) is kept in the Host's
     state (CSERIES_REMOTE_STATE) and set at once (home_origin.set_remote).
  5. The tunnel (server/network/remote_tunnel.py) starts: one link per relay
     address the door's ticket names (remote.ticket), so whichever edge the
     network's DNS sends browsers to, the Host is already there. Tickets come
     from the member door through the member client; a link refreshes its
     ticket every 20 minutes over its open tunnel.

The tunnel stops, cleanly, when remote access is turned off, when the Host
leaves the network or loses its passport, and when the door refuses a ticket
(403: the membership lapsed or no longer carries remote access; 503: the door
offers no remote access). After a refusal it asks again after REFUSED_RETRY_S,
the member client's own wait. A renewed passport rebuilds the tunnel with the
new certificate and a ticket for its serial.

THE HAZARD THIS GUARDS. The Host lets a request from loopback in without a
session (server/middleware/owner_session.py, OwnerSessionGate): a loopback
peer is the machine's own. A browser on the internet must never arrive that
way. So the door is never on loopback: door_address() refuses a loopback,
unspecified or link-local address, and a name, for CSERIES_REMOTE_DOOR; the
tunnel then dials the door's own container address and the Host sees the
door's address as the peer. The Host refuses the loopback lane for any name
on the remote address as well (localhost_only.loopback_host_ok), and
scripts/test_remote_door_loopback.py drives a browser through a relay, the
tunnel and the real door to the Host and checks the peer address the Host saw.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("cseries.remote_access")

DOOR_ENV = "CSERIES_REMOTE_DOOR"
DEFAULT_DOOR_PORT = 8443
# The directory the certificate job reads as its /etc/cseries-cloud
# (compose.remote.yaml; the guest's cseries-host.container.d/remote.conf).
ACME_DIR_ENV = "CSERIES_REMOTE_ACME_DIR"
# The certificate job's last result (sidecars/remote-door/door-cert).
CERT_STATUS_ENV = "CSERIES_REMOTE_CERT_STATUS"

# The platform's DNS worker makes a pass every 30 s (privatae-controller
# features/remote_access/provision.py); asking more often gains nothing.
ENABLE_POLL_S = 30.0
# After a refusal the door says the same again until something changes on the
# account page: the member client's REFUSED_RETRY_S.
REFUSED_RETRY_S = 900.0
# Both links refresh on the same timer: one ticket fetched this recently
# serves the other.
TICKET_REUSE_S = 60.0
# A ticket this close to its expiry is not handed to a new link.
TICKET_MIN_LEFT_S = 300.0
RELAY_PORT = 443
# The supervisor's pace once everything is up.
CHECK_S = 60.0
# How long a step waits when the member door is not connected yet.
NOT_CONNECTED_S = 10.0
# acme-dns refused the credential the certificate job used (401): one new
# account at once, then never two asks closer than this, whether or not the
# job got through in between. A day is the job's own pace once it works
# (door-cert DAY_S): an acme-dns that refuses every account, or a status file
# that says refused and then ok over and over (only the certificate job's
# container writes it), costs the platform one registration a day per Host,
# never a loop, and lego's 30-day renewal window (cseries-cert
# `renew --days 30`) still holds about 30 tries before the certificate
# expires.
ACME_DNS_REISSUE_S = 86400.0


class DoorAddressError(ValueError):
    pass


def check_door(host: str, port: int) -> tuple[str, int]:
    """(host, port) for the door, or DoorAddressError: an IP literal (no
    name, so nothing can resolve it to loopback later), not loopback, not
    unspecified, not link-local, not multicast, an IPv4-mapped address read
    as its IPv4 form, and a port in range."""
    try:
        addr = ipaddress.ip_address((host or "").strip().strip("[]"))
    except ValueError:
        raise DoorAddressError(f"{host!r} is not an IP address: the door is named by the "
                               "address of its container, never a name") from None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.is_loopback or addr.is_unspecified or addr.is_link_local or addr.is_multicast:
        raise DoorAddressError(
            f"{addr} cannot be the door: a request from loopback (or from this machine "
            "itself) is let into the Host without a session, so a browser from the "
            "internet must reach the Host from the door's own container address")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise DoorAddressError(f"{port!r} is not a port")
    return str(addr), port


def door_address(env: Optional[dict] = None) -> Optional[tuple[str, int]]:
    """The door from CSERIES_REMOTE_DOOR (`<ip>:<port>` or `[<ipv6>]:<port>`),
    checked; None when it is unset. Raises DoorAddressError for a door that
    must not be used."""
    raw = ((env if env is not None else os.environ).get(DOOR_ENV) or "").strip()
    if not raw:
        return None
    host, port = raw, DEFAULT_DOOR_PORT
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0:
            raise DoorAddressError(f"{raw!r}: an IPv6 address is written [address]:port")
        host, rest = raw[1:end], raw[end + 1:]
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                raise DoorAddressError(f"{raw!r}: the port is not a number")
            port = int(rest[1:])
    elif raw.count(":") == 1:
        host, _, p = raw.partition(":")
        if not p.isdigit():
            raise DoorAddressError(f"{raw!r}: the port is not a number")
        port = int(p)
    return check_door(host, port)


# The kernel's IPv4 routes for this network namespace (the Host's container).
ROUTES_PATH = Path("/proc/net/route")
_RTF_UP = 0x1


def door_network_problem(door: tuple[str, int], routes: Optional[Path] = None) -> Optional[str]:
    """Why this Host is not on its door's network, or None.

    The guest joins the Host to the door's network with the drop-in that also
    names the door (cseries-host.container.d/remote.conf). A Host whose unit
    could not be made from that drop-in keeps running on its old networks
    after a restart, and the door's address then leads nowhere: remote access
    is broken with nothing to say so. So the Host looks for a route of its own
    straight onto the door's address (on its link, no gateway). Only IPv4 is
    read (the guest's door is IPv4); no answer when the routes can't be read."""
    try:
        addr = ipaddress.ip_address(door[0])
    except (ValueError, TypeError, IndexError):
        return None
    if addr.version != 4:
        return None
    try:
        lines = (routes or ROUTES_PATH).read_text().splitlines()[1:]
    except OSError:
        return None
    want = int(addr)
    for line in lines:
        f = line.split()
        if len(f) < 8:
            continue
        try:
            # An address field is its four network-order bytes printed as one
            # native-order word.
            dest, gw, mask = (int.from_bytes(int(x, 16).to_bytes(4, sys.byteorder), "big")
                              for x in (f[1], f[2], f[7]))
            flags = int(f[3], 16)
        except (ValueError, OverflowError):
            continue
        if flags & _RTF_UP and gw == 0 and mask != 0 and want & mask == dest & mask:
            return None
    return (f"this Host is not on its door's network (no route of its own to {addr}): "
            "its unit did not take the remote-access settings, so a browser from the "
            "internet cannot reach it. Run the cseries command again; if this stays, "
            "its log says why (cseries-guest logs host)")


# ── what the door's answers carry ───────────────────────────────────────────

class RemoteError(Exception):
    """Remote access could not be turned on or kept; `message` is for the
    owner, `status` the HTTP answer the route gives."""

    def __init__(self, message: str, status: int = 502, *, final: bool = False):
        super().__init__(message)
        self.message = message
        self.status = status
        # The door's own refusal (403, or 503 with remote access not offered):
        # asked again after REFUSED_RETRY_S, not at the next poll.
        self.final = final


class TicketRefused(Exception):
    """The member door will not give this Host a ticket (403 or 503)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Ticket:
    token: str
    exp: float
    serial: str
    fetched: float
    relays: tuple[str, ...]


def ticket_payload(token: str) -> dict:
    """The payload of a ticket this Host's own member door handed it, read
    without checking the signature (the relay checks it); {} when it does not
    decode."""
    try:
        body = token.strip().split(".")[1]
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (AttributeError, IndexError, ValueError):
        return {}


class BadRelays(ValueError):
    """The ticket's relay list is not one the Host dials."""


def relay_hosts(relays) -> tuple[str, ...]:
    """Every address the ticket's relays carry, each once, in order. The door
    sends one relay (tunnel.<zone>) with one address per edge (privatae
    bridge/remote_ticket.py RemoteConfig.relays), each a bare IPv4 or IPv6
    literal: no port, no brackets, no name, no zone index. The Host holds a
    link to each address, dialled on 443 with the name tunnel.<zone>, so the
    edge a failover moves browsers to already has this Host's tunnel.

    Anything else in the list refuses the whole ticket (BadRelays): a ticket
    that names a relay some other way is not the door's, and dialling a name
    would let DNS choose where the passport is presented."""
    if not isinstance(relays, list) or not relays:
        raise BadRelays("the ticket names no relay")
    out: list[str] = []
    for relay in relays:
        addrs = relay.get("addrs") if isinstance(relay, dict) else None
        if not isinstance(addrs, list) or not addrs:
            raise BadRelays("a relay in the ticket has no addresses")
        for a in addrs:
            if not isinstance(a, str) or not a or "%" in a or a != a.strip():
                raise BadRelays(f"the ticket names a relay address that is not an IP literal: {a!r:.80}")
            try:
                ip = ipaddress.ip_address(a)
            except ValueError:
                raise BadRelays(f"the ticket names a relay address that is not an IP literal: {a!r:.80}") from None
            if ip.is_unspecified or ip.is_multicast:
                raise BadRelays(f"the ticket names a relay address no relay can have: {a}")
            if str(ip) not in out:
                out.append(str(ip))
    return tuple(out)


def _err(env: dict) -> str:
    return str(env.get("error") or "the network gave no answer")[:500]


# ── files ───────────────────────────────────────────────────────────────────

def _write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".new-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _write_json(path: Path, obj, mode: int = 0o600) -> None:
    _write(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode(), mode)


def _read_json(path: Optional[Path]):
    if path is None:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def acme_dir() -> Optional[Path]:
    raw = os.environ.get(ACME_DIR_ENV, "").strip()
    return Path(raw) if raw else None


def cert_status_path() -> Optional[Path]:
    raw = os.environ.get(CERT_STATUS_ENV, "").strip()
    return Path(raw) if raw else None


def store_credential(directory: Path, *, address_id: str, zone: str, cred: dict) -> None:
    """The acme-dns credential the door hands over once, where the
    certificate job's hook reads it (cseries-acme-dns-hook: acme-dns.json
    keyed by <address_id>.<zone>, acme-dns-api.json beside it). 0600: the job
    runs as the Host's own user. Raises OSError or ValueError; never logs a
    value."""
    need = ("username", "password", "subdomain", "fulldomain", "api_base")
    if not isinstance(cred, dict) or not all(isinstance(cred.get(k), str) and cred.get(k) for k in need):
        raise ValueError("the acme-dns credential the network sent is incomplete")
    if not cred["api_base"].startswith("https://"):
        raise ValueError("the acme-dns API the network named is not https")
    entry = {k: cred[k] for k in ("username", "password", "subdomain", "fulldomain")}
    _write_json(directory / "acme-dns.json", {f"{address_id}.{zone}": entry}, 0o600)
    _write_json(directory / "acme-dns-api.json", {"api_base": cred["api_base"]}, 0o600)


def have_credential(directory: Optional[Path], *, address_id: str, zone: str) -> bool:
    doc = _read_json(directory / "acme-dns.json") if directory else None
    entry = doc.get(f"{address_id}.{zone}") if isinstance(doc, dict) else None
    return isinstance(entry, dict) and all(entry.get(k) for k in ("username", "password", "subdomain"))


# ── the manager ─────────────────────────────────────────────────────────────

@dataclass
class _State:
    on: bool = False
    address_id: Optional[str] = None
    zone: Optional[str] = None
    remote_url: Optional[str] = None
    acme_account_uri: Optional[str] = None
    dns: Optional[str] = None
    dns_error: Optional[str] = None
    credential_stored: bool = False
    enabled_at: Optional[float] = None
    # When this Host last asked for a new acme-dns account because acme-dns
    # refused the one it had. Kept on disk, so a restart does not ask again,
    # and kept when the certificate job gets through, so the bound holds
    # whatever its status says.
    acme_dns_reissued_at: Optional[float] = None

    @classmethod
    def load(cls, path: Path) -> "_State":
        doc = _read_json(path)
        if not isinstance(doc, dict):
            return cls()
        known = {k: doc.get(k) for k in cls.__dataclass_fields__ if k in doc}
        return cls(**known)


@dataclass
class _Refusal:
    code: int
    message: str
    until: float


class RemoteAccess:
    """This Host's remote access. `membership` is the Membership it rides on
    (server/bridge/membership.py): its member client asks the door, its
    passport is the tunnel's certificate."""

    def __init__(self, membership, *, state_path: Optional[Path] = None,
                 acme_directory: Optional[str] = None, acme_session=None, acme_allow_http: bool = False,
                 dial: Optional[Callable[[str], tuple[str, int]]] = None,
                 door: Optional[Callable[[], Optional[tuple[str, int]]]] = None,
                 door_network: Optional[Callable[[tuple[str, int]], Optional[str]]] = None,
                 tunnel_kw: Optional[dict] = None, enable_poll: float = ENABLE_POLL_S,
                 refused_retry: float = REFUSED_RETRY_S, check_every: float = CHECK_S,
                 ticket_reuse: float = TICKET_REUSE_S,
                 acme_dns_reissue_every: float = ACME_DNS_REISSUE_S,
                 clock: Callable[[], float] = time.time):
        from server import home_origin
        from server.network import remote_acme
        self.m = membership
        self.state_path = Path(state_path or home_origin.remote_state_path())
        self.acme_directory = acme_directory or remote_acme.ACME_DIRECTORY
        self._acme_session = acme_session
        self._acme_allow_http = acme_allow_http
        self._dial = dial or (lambda addr: (addr, RELAY_PORT))
        # Where the tunnel hands browsers over: CSERIES_REMOTE_DOOR, checked
        # (door_address). A test rig gives its own.
        self._door = door or door_address
        # Whether this Host is on that door's network (door_network_problem);
        # a rig that gives its own door gives its own answer, or none.
        self._door_network = door_network or (door_network_problem if door is None else (lambda _d: None))
        self._tunnel_kw = dict(tunnel_kw or {})
        self._enable_poll = enable_poll
        self._refused_retry = refused_retry
        self._check_every = check_every
        self._ticket_reuse = ticket_reuse
        self._acme_dns_reissue_every = acme_dns_reissue_every
        self._clock = clock
        self.state = _State.load(self.state_path)
        self.problem: Optional[str] = None
        self.refusal: Optional[_Refusal] = None
        self.tunnel = None
        self._tunnel_task: Optional[asyncio.Task] = None
        self._tunnel_serial: Optional[str] = None
        self._tunnel_relays: tuple[str, ...] = ()
        self._ticket: Optional[Ticket] = None
        self._ticket_lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._lock = asyncio.Lock()
        self._side: set[asyncio.Task] = set()

    # ── state ──

    def _save(self) -> None:
        _write_json(self.state_path, dict(vars(self.state)), 0o600)

    def _event(self) -> asyncio.Event:
        if self._wake is None:
            self._wake = asyncio.Event()
        return self._wake

    def wake(self) -> None:
        self._event().set()

    def _passport(self):
        return getattr(self.m, "passport", None)

    def _client(self):
        return getattr(self.m, "client", None)

    # ── turning it on and off ──

    def available(self) -> Optional[str]:
        """None when this install can carry remote access, else why not."""
        if acme_dir() is None:
            return ("this install has no door for remote access; update C-Series to a "
                    "release that carries it")
        try:
            if self._door() is None:
                return ("this install has no door for remote access; update C-Series to a "
                        "release that carries it")
        except DoorAddressError as exc:
            return str(exc)
        return None

    async def turn_on(self) -> dict:
        """Settings' "Turn on remote access". Asks the door once now, so a
        refusal is shown at once, then keeps at it in the background."""
        why = self.available()
        if why:
            raise RemoteError(why, 409)
        frozen = getattr(self.m, "frozen_note", None)
        if frozen:
            raise RemoteError(frozen, 409)
        if self._passport() is None:
            raise RemoteError("join the network first: remote access is part of the membership", 409)
        client = self._client()
        if client is None or not client.connected:
            raise RemoteError("this Host is not connected to the network right now; try again shortly", 503)
        async with self._lock:
            self.refusal = None
            await self._enable()
            self.state.on = True
            self._save()
        self.start()
        self.wake()
        return self.status()

    async def turn_off(self) -> dict:
        """Settings' "Turn off". The tunnel stops; the address and the
        certificate stay, so turning it on again is quick. The door keeps the
        address for this Host (it never hands one out twice)."""
        async with self._lock:
            self.state.on = False
            self._save()
        await self._stop_supervisor()
        await self._stop_tunnel()
        self._set_origin(None)
        self.problem = None
        self.refusal = None
        return self.status()

    def _set_origin(self, url: Optional[str]) -> None:
        from server import home_origin
        try:
            home_origin.set_remote(url)
        except ValueError as exc:
            self.problem = f"the remote address {url!r} cannot be used here: {exc}"
            log.error("remote access: %s", self.problem)

    async def _enable(self, *, reissue_acme_dns: bool = False) -> None:
        """One remote.enable round trip and what it says. Raises RemoteError
        for a refusal; stores the credential first when it comes.
        `reissue_acme_dns`: acme-dns refused the credential this Host holds,
        so the door is asked for a new account (_acme_dns_reissue_due)."""
        from server import home_origin
        from server.network import remote_acme
        directory = acme_dir()
        if directory is None:
            raise RemoteError(self.available() or "no certificate directory", 409)
        try:
            uri = await remote_acme.ensure_account(directory / "acme-account",
                                                   directory_url=self.acme_directory,
                                                   session=self._acme_session,
                                                   allow_http=self._acme_allow_http)
        except remote_acme.AcmeError as exc:
            raise RemoteError(f"this Host's certificate account could not be made: {exc}", 502) from None
        client = self._client()
        if client is None or not client.connected:
            raise RemoteError("this Host is not connected to the network right now", 503)
        s = self.state
        reissue = bool(s.address_id and s.dns == "ready" and not have_credential(
            directory, address_id=s.address_id, zone=s.zone or ""))
        params = {"acme_account_uri": uri}
        if reissue or reissue_acme_dns:
            params["reissue_acme_dns"] = True
        if reissue_acme_dns:
            # Counted when it is sent, whatever the door answers: the bound
            # holds however the round trip ends. dns "wanted": the door
            # registers the new account after it answers, so until an answer
            # comes the Host polls remote.enable without reissue_acme_dns,
            # which picks the new credential up even when this answer is lost.
            s.acme_dns_reissued_at = self._clock()
            s.dns = "wanted"
            self._save()
            log.warning("remote access: acme-dns refused this Host's certificate credential; "
                        "asking the network for a new acme-dns account")
        env = await client.request_full("remote.enable", params)
        if not env.get("ok"):
            code = env.get("code")
            if code == 503 and env.get("error_kind") != "infra":
                raise RemoteError("the network does not offer remote access yet", 503, final=True)
            if code in (400, 403, 409):
                raise RemoteError(_err(env), 403 if code == 403 else 409, final=code == 403)
            raise RemoteError(f"the network did not answer: {_err(env)}", 502)
        data = env.get("data") if isinstance(env.get("data"), dict) else {}
        aid = data.get("address_id")
        names = data.get("names") if isinstance(data.get("names"), dict) else {}
        zone = str(names.get("zone") or "").lower()
        from server.network import remote_protocol as rp
        if not rp.valid_address_id(aid) or not rp.valid_dns_name(zone):
            raise RemoteError("the network's answer names no usable address", 502)
        stored = s.credential_stored and s.address_id == aid and have_credential(
            directory, address_id=aid, zone=zone)
        if isinstance(data.get("acme_dns"), dict):
            # Shown once: on disk before anything else can fail.
            try:
                store_credential(directory, address_id=aid, zone=zone, cred=data["acme_dns"])
                stored = True
            except (OSError, ValueError) as exc:
                # The new one is lost: the door hands it over once. `stored`
                # stays what it was: a credential this Host already held for
                # this address is still on disk (store_credential writes
                # atomically), and if acme-dns refuses it the refusal bound
                # asks again (_acme_dns_reissue_due). credential_stored False
                # would only poll remote.enable without reissue_acme_dns,
                # which never hands a new one over.
                self.problem = f"the certificate credential could not be kept: {exc.__class__.__name__}"
                log.error("remote access: %s; it is asked for again", self.problem)
        url = f"https://{home_origin.HOME.handle}.{aid}.{zone}"
        try:
            home_origin.parse_remote(url, home_origin.HOME)
        except ValueError as exc:
            raise RemoteError(f"the remote address {url} cannot be used here: {exc}", 502) from None
        _write_json(directory / "cloud.json",
                    {"name": home_origin.HOME.handle, "address_id": aid, "zone": zone,
                     "acme_email": remote_acme.ACCOUNT_LABEL, "acme_server": self.acme_directory}, 0o644)
        first = s.address_id != aid
        s.address_id, s.zone, s.remote_url, s.acme_account_uri = aid, zone, url, uri
        s.dns = str(data.get("dns") or "") or None
        s.dns_error = str(data["dns_error"])[:500] if data.get("dns_error") else None
        s.credential_stored = stored
        s.enabled_at = s.enabled_at or self._clock()
        self._save()
        self._set_origin(url)
        if first:
            log.info("remote access: this Host's remote address is %s", url)

    # ── running ──

    def start(self) -> None:
        """Start the supervisor when remote access is on (from the
        membership's start, and from turn_on)."""
        if not self.state.on:
            return
        if self._task is None or self._task.done():
            if self.state.remote_url:
                self._set_origin(self.state.remote_url)
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while self.state.on:
            try:
                wait = await self._step()
            except asyncio.CancelledError:
                raise
            except RemoteError as exc:
                self.problem = exc.message
                if exc.final:
                    self.refusal = _Refusal(exc.status, exc.message, self._clock() + self._refused_retry)
                    await self._stop_tunnel()
                wait = self._refused_retry if exc.final else self._enable_poll
            except Exception as exc:  # noqa: BLE001 — the next pass tries again
                log.warning("remote access: %s: %s", exc.__class__.__name__, exc)
                self.problem = f"{exc.__class__.__name__}: {exc}"
                wait = self._enable_poll
            ev = self._event()
            try:
                await asyncio.wait_for(ev.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            ev.clear()

    async def _step(self) -> float:
        p, client = self._passport(), self._client()
        if p is None or client is None:
            await self._stop_tunnel()
            self.problem = "this Host is not a network member"
            return self._check_every
        r = self.refusal
        if r is not None:
            left = r.until - self._clock()
            if left > 0:
                return left
            self.refusal = None
        s = self.state
        if not client.connected:
            return NOT_CONNECTED_S
        if s.address_id is None or s.dns != "ready" or not s.credential_stored:
            async with self._lock:
                await self._enable()
            if s.dns == "failed":
                self.problem = f"the network could not make this Host's DNS: {s.dns_error or 'no reason given'}"
        elif self._acme_dns_reissue_due():
            async with self._lock:
                await self._enable(reissue_acme_dns=True)
        if self.tunnel is not None and self._tunnel_serial != p.serial:
            # A renewed passport (the tunnel's certificate and its tickets are
            # the old one's), or the ticket's relays changed.
            log.info("remote access: the passport or the relays changed; the tunnel starts again")
            await self._stop_tunnel()
        if self.tunnel is None and s.address_id:
            await self._start_tunnel()
        if self.tunnel is not None and s.dns != "failed":
            self.problem = None
        if s.dns != "ready" or not s.credential_stored:
            return self._enable_poll
        return self._check_every

    def _credential_refused(self) -> Optional[bool]:
        """The certificate job's last word on the acme-dns credential this
        Host holds now: True when acme-dns refused it (401), False when the
        job got through, None when that word is about another credential
        (its run read an acme-dns.json this is not, or says not which) or is
        neither. An outage, a 5xx or a refused name is never True."""
        directory = acme_dir()
        doc = _read_json(cert_status_path())
        if directory is None or not isinstance(doc, dict):
            return None
        try:
            held = (directory / "acme-dns.json").stat().st_mtime_ns
        except OSError:
            return None
        ran_with = doc.get("credential_mtime_ns")
        if not isinstance(ran_with, int) or isinstance(ran_with, bool) or ran_with != held:
            return None
        if doc.get("acme_dns_refused") is True:
            return True
        return False if doc.get("ok") is True else None

    def _acme_dns_reissue_due(self) -> bool:
        """Whether to ask the door for a new acme-dns account now: acme-dns
        refused the credential this Host holds, and this Host never asked, or
        asked ACME_DNS_REISSUE_S ago or more. A job that got through in
        between changes nothing: the job's status is another container's
        word, and the bound must hold whatever it says."""
        if self._credential_refused() is not True:
            return False
        s = self.state
        last, now = s.acme_dns_reissued_at, self._clock()
        if not isinstance(last, (int, float)) or isinstance(last, bool):
            last = None
        elif last > now:
            # The clock went back past the last ask: counted from now, so the
            # bound neither lapses nor waits for a time that may never come.
            s.acme_dns_reissued_at = last = now
            self._save()
        return last is None or now - last >= self._acme_dns_reissue_every

    async def _start_tunnel(self) -> None:
        from server.network import remote_tunnel as rt
        p = self._passport()
        door = self._door()
        if door is None:
            raise RemoteError(self.available() or "no door", 409)
        try:
            ticket = await self._fetch_ticket()
        except TicketRefused as exc:
            self._refused(exc)
            return
        hosts = ticket.relays
        if not hosts:
            raise RemoteError("the network's ticket names no relay", 502)
        relays = []
        for h in hosts:
            dh, dp = self._dial(h)
            relays.append(rt.RelayAddress(host=dh, port=dp, label=h))
        ctx = rt.client_context(passport_cert=str(p.chain_path), passport_key=str(p.key_path),
                                network_root=str(p.root_path))
        self.tunnel = rt.TunnelClient(address_id=self.state.address_id, zone=self.state.zone,
                                      relays=relays, ssl_context=ctx, tickets=self._tickets,
                                      door=door, **self._tunnel_kw)
        self._tunnel_serial = p.serial
        self._tunnel_relays = hosts
        self._tunnel_task = self.tunnel.start()
        log.info("remote access: tunnel started to %d relay address(es)", len(relays))

    async def _stop_tunnel(self) -> None:
        tunnel, task = self.tunnel, self._tunnel_task
        self.tunnel, self._tunnel_task, self._tunnel_serial, self._tunnel_relays = None, None, None, ()
        self._ticket = None
        if tunnel is not None:
            try:
                await tunnel.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("remote access: tunnel stop: %s", exc)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _stop_supervisor(self) -> None:
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def shutdown(self) -> None:
        """The Host is stopping: everything stops, nothing on disk changes."""
        await self._stop_supervisor()
        await self._stop_tunnel()
        for t in list(self._side):
            t.cancel()

    async def left_network(self) -> None:
        """The passport is gone (the Host left, or the door revoked or froze
        it). The address belonged to that passport's Host, so it is forgotten."""
        await self._stop_supervisor()
        await self._stop_tunnel()
        had = self.state.on or self.state.address_id is not None
        self.state = _State()
        if had:
            try:
                self._save()
            except OSError as exc:
                log.warning("remote access: state not written: %s", exc)
            self._set_origin(None)
        self.problem = self.refusal = None

    async def network_stopped(self) -> None:
        """The door froze this Host (membership.py: a copy of its passport was
        used beside it) and its passport is gone. The tunnel and the supervisor
        stop; the owner's choice, the address and the certificate's credential
        stay, because the same Host comes back with a rejoin code and the
        address is still its own. Membership starts the supervisor again when
        the Host rejoins."""
        await self._stop_supervisor()
        await self._stop_tunnel()
        self.problem = self.refusal = None

    def passport_renewed(self) -> None:
        self._ticket = None
        self.wake()

    def membership_refused(self, reason: str, error: str) -> None:
        """The member door refused the Host's connection (membership.py's
        _on_refusal). A lapsed membership stops the tunnel now rather than at
        the ticket's expiry."""
        if reason != "membership" or self.tunnel is None:
            return
        self._refused(TicketRefused(403, error or "membership not active"))

    # ── tickets ──

    def _ask_enable_again(self) -> None:
        """The door's ticket says it no longer knows this Host's address as
        the Host does: the supervisor asks remote.enable again. A link that
        found it wakes the supervisor. The supervisor that found it does not:
        its own wait after the failed step paces the next try, where a wake
        would end that wait at once and a door that kept answering this way
        would be asked twice a round trip, using up the Host's requests on the
        member connection."""
        self.state.dns = None
        if asyncio.current_task() is not self._task:
            self.wake()

    def _refused(self, exc: TicketRefused) -> None:
        self.refusal = _Refusal(exc.code, exc.message, self._clock() + self._refused_retry)
        self.problem = (f"the network refused remote access: {exc.message}" if exc.code == 403 else
                        "the network does not offer remote access right now")
        log.warning("remote access: the member door refused a ticket (%s); the tunnel stops and "
                    "this Host asks again in %d s", exc.code, int(self._refused_retry))
        # Not from inside a link: stopping the tunnel cancels its links.
        t = asyncio.get_running_loop().create_task(self._stop_tunnel())
        self._side.add(t)
        t.add_done_callback(self._side.discard)
        self.wake()

    async def _fetch_ticket(self) -> Ticket:
        client, p = self._client(), self._passport()
        if client is None or p is None:
            raise ConnectionError("this Host is not a network member")
        if not client.connected:
            raise ConnectionError("the member door is not connected")
        env = await client.request_full("remote.ticket", {})
        if not env.get("ok"):
            code = env.get("code")
            from server.bridge.member_client import REFUSAL_REASONS
            if env.get("reason") in REFUSAL_REASONS:
                raise TicketRefused(403, _err(env))
            if code in (403, 503) and env.get("error_kind") != "infra":
                raise TicketRefused(int(code), _err(env))
            if code == 409:
                # The door has no address for this Host: ask for it again.
                self._ask_enable_again()
            raise ConnectionError(f"no ticket: {_err(env)}")
        data = env.get("data") if isinstance(env.get("data"), dict) else {}
        token = data.get("ticket")
        if not isinstance(token, str) or not token:
            raise ConnectionError("the network's answer carries no ticket")
        payload = ticket_payload(token)
        if data.get("address_id") != self.state.address_id or payload.get("address_id") != self.state.address_id:
            self._ask_enable_again()
            raise ConnectionError("the ticket is for another address than this Host's")
        if payload.get("serial") != p.serial:
            # Asked on the member connection made before a renewal: the next
            # ask, on the new connection, is for this passport.
            raise ConnectionError("the ticket is for an earlier passport")
        try:
            hosts = relay_hosts(data.get("relays"))
        except BadRelays as exc:
            self.problem = str(exc)
            raise ConnectionError(str(exc)) from None
        exp = payload.get("exp")
        t = Ticket(token=token, exp=float(exp) if isinstance(exp, (int, float)) else 0.0,
                   serial=p.serial, fetched=self._clock(), relays=hosts)
        self._ticket = t
        if self.tunnel is not None and t.relays and t.relays != self._tunnel_relays:
            log.info("remote access: the relays changed; the tunnel starts again")
            self._tunnel_serial = None      # the supervisor rebuilds it
            self.wake()
        return t

    async def _tickets(self, fresh: bool) -> str:
        """The tunnel's TicketSource."""
        async with self._ticket_lock:
            p, t, now = self._passport(), self._ticket, self._clock()
            if (t is not None and p is not None and t.serial == p.serial
                    and t.exp - now > TICKET_MIN_LEFT_S
                    and (not fresh or now - t.fetched < self._ticket_reuse)):
                return t.token
            try:
                return (await self._fetch_ticket()).token
            except TicketRefused as exc:
                self._refused(exc)
                raise ConnectionError(f"the network refused remote access: {exc.message}") from None

    # ── what Settings shows ──

    def status(self) -> dict:
        from server import home_origin
        r = home_origin.REMOTE
        s = self.state
        out = {"on": s.on, "address": r.origin if r else None, "address_id": s.address_id,
               "dns": s.dns, "dns_error": s.dns_error, "door": None, "links": [],
               "running": self.tunnel is not None, "problem": self.problem,
               "refused": self.refusal.message if self.refusal else None,
               "available": self.available() is None, "certificate": certificate_status(),
               "member": self._passport() is not None,
               # The door froze this Host (membership.py): the tunnel is off
               # and the owner is told what to do.
               "frozen": getattr(self.m, "frozen_note", None)}
        try:
            door = self._door()
            out["door"] = f"{door[0]}:{door[1]}" if door else None
            off_net = self._door_network(door) if door else None
            if off_net:
                out["problem"] = off_net
        except DoorAddressError as exc:
            out["problem"] = str(exc)
        if self.tunnel is not None:
            out["links"] = [{k: v for k, v in link.items()
                             if k in ("relay", "state", "since", "connected_at", "last_error",
                                      "last_refusal", "streams")}
                            for link in self.tunnel.status()]
        return out


def certificate_status() -> Optional[dict]:
    """The certificate job's last word (sidecars/remote-door/door-cert):
    when it looked, the certificate's expiry and names, and its error."""
    doc = _read_json(cert_status_path())
    if not isinstance(doc, dict):
        return None
    out = {}
    for k in ("checked_at", "not_after", "error", "ok", "names", "next_check_at"):
        if k in doc:
            v = doc[k]
            out[k] = v[:500] if isinstance(v, str) else v
    return out


_current: Optional[RemoteAccess] = None


def current() -> Optional[RemoteAccess]:
    return _current


def install(ra: Optional[RemoteAccess]) -> None:
    global _current
    _current = ra


def status() -> dict:
    """What Settings shows about remote access."""
    ra = _current
    if ra is not None:
        return ra.status()
    from server.home_origin import REMOTE
    out = {"on": False, "address": REMOTE.origin if REMOTE else None, "door": None, "links": [],
           "running": False, "problem": None, "certificate": certificate_status()}
    try:
        door = door_address()
        out["door"] = f"{door[0]}:{door[1]}" if door else None
        if door:
            out["problem"] = door_network_problem(door)
    except DoorAddressError as exc:
        out["problem"] = str(exc)
    return out
