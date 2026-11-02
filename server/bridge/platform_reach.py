"""What the Privatae network can do for this Host right now: the one answer
every system app gates its platform-only controls on.

A C-Series Host runs standalone unless its owner joins the network
(server/bridge/membership.py). Some of the apps' controls only work through
the network: the member door carries the ops in member_client.NETWORK_OPS and
nothing else, and a standalone Host's NoPlatform carries none. A control
whose op this Host cannot send fails at the last step, so the apps do not
show it at all.

reach() is that answer, in two parts:

  member  The Host is an active member: it holds a passport that has not
          expired, the door has not frozen it, and the door's last word on a
          connection was not a refusal (revoked, conflict, membership,
          unknown, mismatch or refused: member_client.REFUSAL_REASONS). A
          connection that merely dropped keeps it a member; the client dials
          again on its own.
  ops     The platform ops this Host can send now: the member door's table
          when `member`, else none. A feature the network does not offer
          yet is absent here, so its control stays hidden on a member Host
          too, and appears once the door's table carries its op.

A member whose membership lapsed (expired, frozen, refused) reads as not a
member: its platform-only controls are hidden, not shown as needing
membership, because none of them can work. Settings -> Network is where it
reads why (GET /api/network/status).

Read through GET /api/system/status ("network"), which every app and the
shell already read without a token (server/routes/system.py); the apps use
dashboard/base/js/network-reach.js. Anything that cannot be read answers
"not a member, no ops": a failure never shows a control.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("cseries.platform_reach")

NOT_A_MEMBER: dict = {"member": False, "ops": []}

# Whole apps that do nothing without the platform, each with the op it is
# built on. GET /api/applications (server/routes/applications.py list_apps)
# leaves one out of the launcher unless this Host can send its op.
#   sites      every site is published, served and stored by the platform
#              (webapp.* through publisher_op_service, server/routes/applications.py)
#   community  the members' board, over the member door (server/routes/community.py)
#   publisher  publishing to the App Store (appstore.publish_bundle,
#              server/routes/applications.py _publisher_call)
APP_NEEDS_OP: dict[str, str] = {
    "sites": "webapp.publish_bundle",
    "community": "community.threads",
    "publisher": "appstore.publish_bundle",
}


def app_shown(app_id: str, r: dict) -> bool:
    """Whether app `app_id` belongs in the launcher under reach answer `r`."""
    op = APP_NEEDS_OP.get(app_id)
    return op is None or (bool(r.get("member")) and op in (r.get("ops") or ()))


def membership_active(m) -> bool:
    """Whether Membership `m` is an active member right now (see above)."""
    from server.bridge.member_client import REFUSAL_REASONS
    p = getattr(m, "passport", None)
    if p is None or getattr(m, "frozen", False):
        return False
    if getattr(m, "reason", None) in REFUSAL_REASONS:
        return False
    return not m._expired(p)


def reach(m=None, bc=None) -> dict:
    """{"member": bool, "ops": sorted list of op names}. `m` is the
    Membership and `bc` the platform client; both default to the process's
    own."""
    try:
        if m is None:
            from server.bridge import membership
            m = membership.current()
        if not membership_active(m):
            return dict(NOT_A_MEMBER, ops=[])
        if bc is None:
            from server.main import get_bridge_client
            bc = get_bridge_client()
        # Only a client that lists its ops says what it carries; anything
        # else (None before boot) carries nothing here.
        ops = getattr(bc, "offered_ops", None)
        return {"member": True, "ops": sorted(ops) if ops else []}
    except Exception as e:  # noqa: BLE001 — unreadable reads as not a member
        logger.warning("platform reach unreadable: %s", e)
        return dict(NOT_A_MEMBER, ops=[])
