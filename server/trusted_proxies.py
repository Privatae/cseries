"""The reverse proxies in front of this Host, and what is believed from them.

CSERIES_TRUSTED_PROXIES is a comma-separated list of the addresses or
networks of the proxies in front of this Host. Empty means none. Only a
request whose immediate peer is one of them may carry X-Forwarded-For or
X-Forwarded-Host that is believed; anyone else's is a header the browser's
page could have set.
"""

from __future__ import annotations

import ipaddress
import logging
import os

log = logging.getLogger("cseries.trusted_proxies")


def networks() -> list:
    out = []
    for part in os.environ.get("CSERIES_TRUSTED_PROXIES", "").split(","):
        part = part.strip()
        if part:
            try:
                out.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                log.warning("CSERIES_TRUSTED_PROXIES: ignoring %r", part)
    return out


def contains(addr: str | None, nets: list) -> bool:
    try:
        a = ipaddress.ip_address(addr or "")
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return any(a in n for n in nets)


def peer_is_trusted(addr: str | None) -> bool:
    return contains(addr, networks())


def _unmapped(addr: str) -> str:
    try:
        a = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    return str(a.ipv4_mapped) if a.version == 6 and a.ipv4_mapped else addr


def visitor(peer: str | None, forwarded_for: str | None) -> str:
    """The address of whoever sent a request: the socket peer, or, when the
    peer is a trusted proxy, the nearest X-Forwarded-For hop that is not
    itself a trusted proxy (hops nearer the visitor are theirs to write).
    "?" when there is no peer."""
    peer = _unmapped(peer or "") or "?"
    nets = networks()
    if not nets or not contains(peer, nets):
        return peer
    hops = [h.strip() for h in (forwarded_for or "").split(",") if h.strip()]
    for hop in reversed(hops):
        hop = _unmapped(hop)
        if not contains(hop, nets):
            return hop
    return peer
