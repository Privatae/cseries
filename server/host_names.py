"""This Host's address zone — the ONE place it is written down.

A Host answers on ``<handle><suffix>``, and each origin-isolated app on
``apps-<key>-<handle><suffix>`` (server/applications/app_origin.py). A
C-Series Host has one zone: the one its CSERIES_URL names (server/home_origin.py).

WHY THIS MODULE EXISTS. A suffix once sat hardcoded in a CSP
``frame-ancestors`` literal that nobody had thought to grep. On a new address
that literal blocked every origin-isolated app — community apps are ALWAYS
isolated — with no error anywhere on the server, just blank frames. One import
beats one more literal.
"""

from __future__ import annotations

from server import home_origin
from server.home_origin import HOME

HOST_SUFFIXES: tuple[str, ...] = (HOME.zone,)
# The desktop's zone and, on a member Host with remote access, the remote
# address's zone (home_origin.REMOTE): where its app origins are framed from.
# HOST_SUFFIXES stays the desktop's alone, since it names what a bastion's
# certificate and records carry. The remote address can be set while the Host
# runs (home_origin.set_remote calls refresh), so read APP_ZONES from this
# module when it is needed, never a copy of it.
APP_ZONES: tuple[str, ...] = tuple(o.zone for o in home_origin.ORIGINS)


def refresh() -> None:
    global APP_ZONES
    APP_ZONES = tuple(o.zone for o in home_origin.ORIGINS)


def host_name(handle: str) -> str:
    """The canonical name for `handle`."""
    return f"{handle}{HOST_SUFFIXES[0]}"


def host_names(handle: str) -> tuple[str, ...]:
    """Every name `handle` legitimately answers to, canonical first.

    Use this, never `host_name`, whenever the value being checked came from
    somewhere OUTSIDE this Host — a bastion's heartbeat, a signed record, a
    stored row.
    """
    return tuple(f"{handle}{s}" for s in HOST_SUFFIXES)


def frame_ancestors() -> str:
    """The `frame-ancestors` source list for an origin-isolated app's CSP.

    The shell frames an isolated app at ``apps-`` + its OWN hostname
    (dashboard/base/js/app-runner.js frameOrigin), on the desktop's scheme and
    port. A source with no port matches only the scheme's default port (CSP3
    §6.7.2.11), so the port is written out whenever there is one.
    """
    return " ".join(o.origin_of(f"*{o.zone}") for o in home_origin.ORIGINS)
