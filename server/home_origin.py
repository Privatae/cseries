"""The address the owner opens C-Series on, and the zone its apps live in.

CSERIES_URL is the desktop's address as the browser types it, for example
http://home.cseries.localhost:8800 (the default). Its first label is the
Host's handle and the rest is the zone: each origin-isolated app is framed
from apps-<key>-<handle><zone> on the same scheme and port
(server/applications/app_origin.py), so one app's page can never read
another's or the desktop's.

Chrome, Firefox and Safari on macOS 26 or later send every *.localhost name
to this machine, so the default needs no DNS. To reach C-Series from other
machines, put it behind a TLS proxy under a name of your own with a wildcard
DNS record for the zone (home.cseries.lan and *.cseries.lan, say) and set
CSERIES_URL to https://home.cseries.lan.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

log = logging.getLogger("cseries.home_origin")

DEFAULT_URL = "http://home.cseries.localhost:8800"
_DEFAULT_PORTS = {"http": 80, "https": 443}
# The handle must leave room for "apps-" + a 20-character key + "-" in one
# 63-character DNS label (app_origin.MAX_APP_HANDLE_LEN).
_HANDLE_RE = re.compile(r"[a-z0-9]{1,37}")
_LABEL_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


@dataclass(frozen=True)
class HomeOrigin:
    scheme: str
    handle: str
    zone: str          # with its leading dot: ".cseries.localhost"
    port: int | None   # None when it is the scheme's default

    @property
    def host(self) -> str:
        return f"{self.handle}{self.zone}"

    def origin_of(self, host: str) -> str:
        """`scheme://host[:port]` for a host in this zone."""
        return f"{self.scheme}://{host}" + (f":{self.port}" if self.port else "")

    @property
    def origin(self) -> str:
        return self.origin_of(self.host)


def parse(url: str) -> HomeOrigin:
    """A HomeOrigin from CSERIES_URL's value. Raises ValueError, naming what
    is wrong, for anything that cannot be a home address."""
    parts = urlsplit((url or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError("it must start with http:// or https://")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("it must be a scheme, a host name and an optional port, nothing more")
    host = (parts.hostname or "").lower()
    labels = host.split(".")
    if len(labels) < 3 or not all(_LABEL_RE.fullmatch(lb) for lb in labels):
        raise ValueError("the host must be a name of three labels or more, like "
                         "home.cseries.localhost: the first label names the desktop and "
                         "the rest is the zone the apps live in")
    if not _HANDLE_RE.fullmatch(labels[0]):
        raise ValueError("the first label must be 1 to 37 lowercase letters or digits")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"the port is not a number: {exc}") from None
    if port == _DEFAULT_PORTS[scheme]:
        port = None
    return HomeOrigin(scheme=scheme, handle=labels[0], zone="." + ".".join(labels[1:]), port=port)


def _load() -> HomeOrigin:
    raw = os.environ.get("CSERIES_URL", "").strip() or DEFAULT_URL
    try:
        return parse(raw)
    except ValueError as exc:
        log.error("CSERIES_URL=%r is not usable (%s); using %s", raw, exc, DEFAULT_URL)
        return parse(DEFAULT_URL)


HOME = _load()


# ── the remote address (remote access for members) ──────────────────────────
#
# CSERIES_REMOTE_URL is the Host's second address, the one a member opens from
# a phone or another computer anywhere: https://<handle>.<address_id>.<zone>,
# for example https://home.k7x2m9.remote.privatae.ai. Browsers reach it through
# the network's relay and this machine's own door (a TLS front end beside the
# Host that holds the address's certificate and asks the Host about every
# request; compose.remote.yaml). Its apps live in its own zone the same way
# (apps-<key>-<handle>.<address_id>.<zone>).
#
# It is open to the whole internet, so it is held to stricter rules than the
# desktop's own address: sign-in there is by passkey or recovery code only
# (never the password), the Host is never set up from it, and a request for
# one of its names is never let in for coming from loopback
# (localhost_only.loopback_host_ok). Unset, or not usable, there is none and
# the Host behaves as it always has.

REMOTE_URL_ENV = "CSERIES_REMOTE_URL"


def parse_remote(url: str, home: HomeOrigin) -> HomeOrigin:
    """A HomeOrigin for CSERIES_REMOTE_URL's value. Raises ValueError, naming
    what is wrong."""
    o = parse(url)
    if o.scheme != "https" or o.port is not None:
        raise ValueError("it must be an https address on the default port")
    if o.zone.endswith(".localhost") or o.zone == ".localhost":
        raise ValueError("it must be a name the internet can reach, not a .localhost one")
    labels = o.zone.split(".")
    from server.network.remote_protocol import valid_address_id
    if len(labels) < 4 or not valid_address_id(labels[1]):
        # ".k7x2m9.remote.example" splits into "", "k7x2m9", "remote", "example".
        raise ValueError("it must be <handle>.<address_id>.<zone>, like home.k7x2m9.remote.privatae.ai")
    if o.zone == home.zone or o.zone.endswith(home.zone) or home.zone.endswith(o.zone):
        raise ValueError("its zone must not overlap the desktop's own zone")
    if o.handle != home.handle:
        # One handle, so an app origin's handle names the same Host on both
        # addresses (app_origin.frame_ancestors_for).
        raise ValueError(f"its first label must be the desktop's own, {home.handle}")
    return o


def _load_remote() -> HomeOrigin | None:
    raw = os.environ.get(REMOTE_URL_ENV, "").strip()
    if not raw:
        return None
    try:
        return parse_remote(raw, HOME)
    except ValueError as exc:
        log.error("%s=%r is not usable (%s); there is no remote address", REMOTE_URL_ENV, raw, exc)
        return None


REMOTE: HomeOrigin | None = _load_remote()
# Every address this Host answers on, the desktop's own first.
ORIGINS: tuple[HomeOrigin, ...] = (HOME,) if REMOTE is None else (HOME, REMOTE)


def origin_of_zone(zone: str) -> HomeOrigin | None:
    """The origin whose zone is `zone` (with its leading dot), or None."""
    for o in ORIGINS:
        if o.zone == zone:
            return o
    return None


def desktop_origin(host: str) -> HomeOrigin | None:
    """The origin whose desktop name is `host` exactly, or None."""
    host = (host or "").lower()
    for o in ORIGINS:
        if o.host == host:
            return o
    return None


def is_remote_host(host: str) -> bool:
    """Whether `host` is the remote address or any name in its zone (one of
    its app origins, or anything else a browser could name there)."""
    if REMOTE is None:
        return False
    host = (host or "").lower().rstrip(".")
    return host == REMOTE.host or host.endswith(REMOTE.zone)
