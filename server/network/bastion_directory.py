"""The Circles bastions folder: the machines this Host is paired to, and who
else is on each one.

Drawn from THIS Host outward and from nothing else. A machine tells each Host
it serves about the others on its own heartbeat (the bastion's peers.go), and
the platform deliberately cannot correlate one machine across two entities —
every pairing mints its own keys and fingerprint so the pairings are
unlinkable from outside. So a circle-wide directory computed by the platform
would need exactly the join that design forbids. What this Host CAN say is:
the machines it is paired to, whether each has been heard from, who anchors
each, and the Hosts each machine announced. Every line is the machine's own report,
and the pane says so.

Narrowed on purpose. The Network app's bastion list carries keys, grants,
addresses, folder stores and posture; none of that belongs in a directory of
who shares a machine, so none of it leaves here. What travels per Host is
what the machine announced (name, handle, state, since) plus the entity id the
peering resolved to, which is what lets the pane show a contact card and send
a connection request through the existing, peer-gated route.

Pure: build_directory takes the two lists and returns the folder, so the
tests exercise the real shaping without a database.
"""
from __future__ import annotations

from typing import Any, Optional

PAIRING_STATES = ("active", "pending", "revoked")
MODES = ("local", "networked")

# Anchor first, then the Hosts that are actually on the machine, then the ones
# waiting to be admitted, then the ones removed.
_STATE_ORDER = {"active": 0, "pending": 1, "revoked": 2}


def _state(v: Any) -> Optional[str]:
    s = str(v or "").strip().lower()
    return s if s in PAIRING_STATES else None


def _handle_of(host_name: str) -> str:
    return host_name.split(".")[0] if host_name else ""


def _anchor(b: dict) -> Optional[dict]:
    a = b.get("anchor")
    if not isinstance(a, dict) or not a.get("anchor_host"):
        return None
    enforcing = b.get("enforcing") if isinstance(b.get("enforcing"), dict) else {}
    host_name = str(a.get("anchor_host") or "")
    return {
        "host_name": host_name,
        "handle": str(a.get("anchor_handle") or "") or _handle_of(host_name),
        # Typed at the machine by whoever ran claim-anchor. Shown as the
        # machine states it, never as a verified identity.
        "owner": str(a.get("owner") or "") or None,
        # The signature check the host ran on the stored claim. An unverified
        # claim is still shown, and worded as unverified.
        "verified": a.get("verified") is True,
        # The box's own answer for THIS pairing (anchor.go pairingEnforcing).
        # None when the report was too large to keep whole: topology.py stores
        # only {truncated, grants} then, so the answer is not known.
        "is_me": None if enforcing.get("truncated") else enforcing.get("is_anchor") is True,
    }


def _hosts(b: dict, anchor: Optional[dict],
           peerings: dict[str, tuple[Optional[int], bool]]) -> Optional[list]:
    peers = b.get("peers")
    # None = the machine never said who else it serves (a bastion too old to
    # announce). That is "unknown", never "nobody else": the pane words the two
    # differently and must be able to.
    if not isinstance(peers, list):
        return None
    anchor_host = anchor["host_name"] if anchor else ""
    out = []
    for p in peers:
        if not isinstance(p, dict):
            continue
        host_name = str(p.get("host_name") or "").strip()
        if not host_name:
            continue
        host_id, current = peerings.get(host_name, (None, False))
        out.append({
            "host_name": host_name,
            "handle": str(p.get("handle") or "") or _handle_of(host_name),
            # A bastion before 0.16 sent no state; everything it announced was
            # an established pairing, since pending did not exist yet.
            "state": _state(p.get("state")) or "active",
            "since": str(p.get("since") or "") or None,
            # None until peer discovery resolves the name to a Host. Without it
            # there is no card to show and nobody to send a request to.
            "host_id": host_id,
            # Whether the machine is STILL announcing this Host (list_peerings'
            # `current`). The connection request is refused for a peering that
            # is not current, so the pane only offers it when this is true.
            "current": current,
            "is_anchor": bool(anchor_host) and host_name == anchor_host,
        })
    out.sort(key=lambda h: (not h["is_anchor"], _STATE_ORDER.get(h["state"], 9),
                            h["handle"].lower(), h["host_name"]))
    return out


def build_directory(bastions: list[dict], peerings: list[dict]) -> list[dict]:
    """One entry per machine this Host is paired to.

    `bastions` is topology.list_bastions; `peerings` is
    peer_discovery.list_peerings. Both are already scoped to this entity."""
    ids: dict[int, dict[str, tuple[Optional[int], bool]]] = {}
    for p in peerings or []:
        tid, name, eid = p.get("token_id"), p.get("peer_host_name"), p.get("peer_entity_id")
        if isinstance(tid, int) and name:
            ids.setdefault(tid, {})[name] = (eid if isinstance(eid, int) else None,
                                             bool(p.get("current")))

    out = []
    for b in bastions or []:
        tid = b.get("id")
        if not isinstance(tid, int):
            continue
        # A machine that has never sent a heartbeat: the pairing exists (the
        # token is minted when it is redeemed) but the box has said nothing.
        # This is also what a pairing waiting for the anchor looks like — the
        # bastion does not run a pending pairing, so it sends no beat until it
        # is approved. It must not read as "too old to report".
        heard = b.get("last_seen_at") is not None
        mode = b.get("mode") if b.get("mode") in MODES else None
        anchor = _anchor(b)
        out.append({
            "token_id": tid,
            "machine": b.get("hostname") or b.get("name") or f"machine {tid}",
            "online": bool(b.get("online")),
            "heard": heard,
            "last_seen_at": b.get("last_seen_at"),
            # None = a bastion too old to report its mode; not "local".
            "mode": mode,
            # A box that reports its mode reports its anchor on the same beat,
            # so only then does a missing anchor mean "not anchored".
            "anchored": (anchor is not None) if mode else None,
            "anchor": anchor,
            "hosts": _hosts(b, anchor, ids.get(tid, {})),
        })
    out.sort(key=lambda m: (not m["online"], str(m["machine"]).lower()))
    return out
