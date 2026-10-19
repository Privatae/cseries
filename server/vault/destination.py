"""Where the credential broker may send a request: never to this Host itself.

The broker (server/vault/authz.py, http_call) sends the Host's own sealed
credentials to third-party APIs, and its callers name the URL: the Host's
model through keys_manage, an app through POST /api/vault/secrets/{name}/use.
Pointed at the Host's own kernel it was a way in from inside: a request from
the pod's loopback carrying headers the caller chose. The
pod's NO_PROXY holds localhost, 127.0.0.1, ::1 and .svc, so those go straight
to the kernel rather than out through the egress proxy.

No caller has a reason to send a credential to its own Host, so the broker
refuses a destination that is:
  - a loopback or unspecified address, in any spelling (IPv4-mapped too), or a
    localhost name;
  - one of this pod's own addresses (every container in the pod shares them);
  - this kernel's own names: its Service and its pod's DNS name
    (harness_backend.kernel_routable_base), with the shorter forms the pod's
    DNS search list completes, and whatever addresses those resolve to, which
    covers the Service's ClusterIP and any other name for it.

It is checked on every hop, redirects included (a client middleware runs once
per request aiohttp sends), and a name is checked again on the addresses the
connection actually uses (a resolver that refuses them), so a name that changes
its answer between the check and the connect is refused too. An IP literal
never reaches a resolver, which is why the per-hop check exists.

A name that does not resolve here is let through to the call, which then fails
or leaves through the egress proxy. The proxy runs on the node, where loopback
is the node's and the pod is reached from outside it, so it cannot hand the
kernel a loopback request.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from typing import Optional

log = logging.getLogger("privatae.vault.destination")

_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
_KERNEL_ADDR_TTL = 300.0
_kernel_addr_cache: tuple[float, frozenset] = (0.0, frozenset())


class SelfDestinationRefused(Exception):
    """The destination is this Host. Not an OSError: aiohttp re-wraps those."""


def _ip(text: str):
    try:
        ip = ipaddress.ip_address(str(text).split("%", 1)[0])
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def own_addresses() -> set:
    """Every address on this pod's interfaces. Read each call: cheap, and a
    stale list would be a hole."""
    out: set = set()
    try:
        with open("/proc/net/if_inet6") as f:
            for line in f:
                hexaddr = line.split()[0] if line.strip() else ""
                if len(hexaddr) == 32:
                    out.add(ipaddress.IPv6Address(int(hexaddr, 16)))
    except (OSError, ValueError):
        pass
    try:
        last = None
        with open("/proc/net/fib_trie") as f:
            for line in f:
                s = line.strip()
                if s.startswith("|--"):
                    last = s[3:].strip()
                elif "/32 host LOCAL" in s and last:
                    ip = _ip(last)
                    if ip is not None:
                        out.add(ip)
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = _ip(info[4][0])
            if ip is not None:
                out.add(ip)
    except (OSError, UnicodeError):
        pass
    return out


def _kernel_fqdns() -> list[str]:
    """This kernel's pod DNS name and its Service's, from the one place that
    spells them (harness_backend.kernel_routable_base: <pod>.<svc>.<ns>.svc.
    cluster.local). Empty outside a provisioned pod (no ENTITY_ID)."""
    try:
        from urllib.parse import urlsplit

        from server.applications.harness_backend import kernel_routable_base
        pod = (urlsplit(kernel_routable_base()).hostname or "").lower()
    except Exception:  # noqa: BLE001 — no harness module, no names to add
        return []
    if not pod or "." not in pod:
        return []
    return [pod, pod.split(".", 1)[1]]


def kernel_names() -> set:
    """This kernel's own DNS names, the shorter forms the pod's search list
    completes (entity-<id>, entity-<id>.entity-<id>, ...), and the pod's
    hostname."""
    names: set = set()
    try:
        names.add(socket.gethostname().lower())
    except OSError:
        pass
    for fqdn in _kernel_fqdns():
        parts = fqdn.split(".")
        for i in range(1, len(parts) + 1):
            names.add(".".join(parts[:i]))
    names.discard("")
    return names


async def _resolve_addrs(host: str, port: Optional[int]) -> list[str]:
    """Every address `host` resolves to from this pod (tests replace this)."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port or 0, type=socket.SOCK_STREAM)
    return sorted({str(i[4][0]) for i in infos})


async def kernel_addresses() -> frozenset:
    """What this kernel's own names resolve to (its pod IP and its Service's
    ClusterIP). Cached for a few minutes when the lookup worked; a failed
    lookup is not cached, so a DNS hiccup is not remembered as 'no names'."""
    global _kernel_addr_cache
    at, cached = _kernel_addr_cache
    if cached and time.monotonic() - at < _KERNEL_ADDR_TTL:
        return cached
    out: set = set()
    for name in _kernel_fqdns():
        try:
            for a in await _resolve_addrs(name, None):
                ip = _ip(a)
                if ip is not None:
                    out.add(ip)
        except (OSError, UnicodeError):
            continue
    frozen = frozenset(out)
    if frozen:
        _kernel_addr_cache = (time.monotonic(), frozen)
    return frozen


_V4_COMPAT = ipaddress.ip_network("::/96")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")
_V4_THIS_NET = ipaddress.ip_network("0.0.0.0/8")


def _embedded_v4(ip):
    """The IPv4 address an IPv6 form carries inside it, or None: IPv4-compatible
    (::a.b.c.d), NAT64 (64:ff9b::a.b.c.d) and 6to4 (2002:aabb:ccdd::). A host
    stack or a translator on the path can turn each back into the IPv4 it
    names, so each is judged as that IPv4."""
    if ip.version != 6:
        return None
    if ip in _V4_COMPAT and int(ip) > 1:          # :: and ::1 are judged as they are
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _SIX_TO_FOUR:
        return ip.sixtofour
    return None


def _is_self(ip, own: set) -> bool:
    if ip.is_loopback or ip.is_unspecified or ip in own:
        return True
    v4 = _embedded_v4(ip)
    return v4 is not None and (v4.is_loopback or v4 in _V4_THIS_NET or v4 in own)


async def refusal(host: str, port: Optional[int] = None) -> Optional[str]:
    """Why the broker may not send to `host`, or None."""
    h = (host or "").strip().strip("[]").lower().rstrip(".")
    if not h:
        return "the url names no host"
    if h in _LOCAL_NAMES or h.endswith(".localhost"):
        return f"{h} is this Host itself; the broker sends credentials to other services only"
    if h in kernel_names():
        return f"{h} is this Host's own kernel; the broker sends credentials to other services only"
    ip = _ip(h)
    if ip is not None:
        addrs = [ip]
    else:
        try:
            addrs = [a for a in (_ip(x) for x in await _resolve_addrs(h, port)) if a is not None]
        except (OSError, UnicodeError):
            return None
    own = own_addresses() | set(await kernel_addresses())
    hits = [str(a) for a in addrs if _is_self(a, own)]
    if hits:
        return (f"{h} resolves to this Host itself ({', '.join(hits[:3])}); "
                "the broker sends credentials to other services only")
    return None


async def _hop_guard(req, handler):
    """aiohttp client middleware: runs for every request the session sends,
    each redirect hop included, before it connects."""
    why = await refusal(req.url.raw_host or "", req.url.port)
    if why:
        raise SelfDestinationRefused(why)
    return await handler(req)


def session_kwargs() -> dict:
    """What a broker ClientSession is opened with: the per-hop check, and a
    connector whose resolver refuses this Host's addresses on the connect
    itself. Built per call, inside a running loop (the connector needs one)."""
    import aiohttp
    import aiohttp.connector
    from aiohttp.abc import AbstractResolver

    class _Resolver(AbstractResolver):
        def __init__(self):
            # The resolver a plain TCPConnector would use, so a guarded
            # session resolves exactly as an unguarded one, plus the check.
            self._inner = aiohttp.connector.DefaultResolver()

        async def resolve(self, host, port=0, family=socket.AF_INET):
            res = await self._inner.resolve(host, port, family=family)
            own = own_addresses() | set(await kernel_addresses())
            for r in res:
                ip = _ip(r.get("host") or "")
                if ip is not None and _is_self(ip, own):
                    raise SelfDestinationRefused(
                        f"{host} resolves to this Host itself ({ip}); "
                        "the broker sends credentials to other services only")
            return res

        async def close(self):
            await self._inner.close()

    return {"connector": aiohttp.TCPConnector(resolver=_Resolver(), use_dns_cache=False),
            "middlewares": (_hop_guard,)}
