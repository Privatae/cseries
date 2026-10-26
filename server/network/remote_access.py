"""Remote access on this machine: where the tunnel hands browsers over, and
what Settings shows about it.

The tunnel (server/network/remote_tunnel.py) runs in the Host's own process
and hands each browser's still-encrypted connection to the machine's door: a
TLS front end in a container of its own beside the Host (compose.remote.yaml,
sidecars/remote-door). The door ends TLS with the remote address's certificate,
whose key was made in that container and never leaves it, asks the Host about
every request (GET /__host/door, as a cloud machine's Caddy does) and passes
what the Host lets in to the Host's own address on the containers' network.

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

Not started here yet: the ticket source and the relay list come from the
member door (remote.ticket), which is not built yet. status() says so.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Optional

DOOR_ENV = "CSERIES_REMOTE_DOOR"
DEFAULT_DOOR_PORT = 8443


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


# The running tunnel, once something starts it (with the member door's tickets).
client = None


def status() -> dict:
    """What Settings shows about remote access."""
    from server.home_origin import REMOTE
    out = {"address": REMOTE.origin if REMOTE else None, "door": None, "links": [],
           "running": False, "problem": None}
    try:
        door = door_address()
        out["door"] = f"{door[0]}:{door[1]}" if door else None
    except DoorAddressError as exc:
        out["problem"] = str(exc)
    if client is not None:
        out["running"] = True
        out["links"] = client.status()
    return out
