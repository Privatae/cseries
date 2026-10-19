#!/usr/bin/env python3
"""The Circles bastions folder (server/network/bastion_directory.py).

Pure: build_directory takes topology.list_bastions rows and
peer_discovery.list_peerings rows, so this feeds it rows of those exact shapes
and checks what the pane will be told.

What it pins, each by an input only that rule can move:
  - "unknown" stays distinct from "none": a bastion too old to report its mode
    reads anchored=None, not "not anchored"; one that never announced peers
    reads hosts=None, not "nobody else".
  - the anchor is the machine's claim: unverified is carried as unverified, and
    "you anchor this" comes from the box's own is_anchor, never a name match.
  - a peer's Host id comes from the peering on THAT machine only.
  - nothing from the Network payload travels: keys, addresses, grants, folder
    stores, posture.
  - the route asks for read:topology as well as read:circles.
"""
from __future__ import annotations

import json
import os
import sys

REPO = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from server.network.bastion_directory import build_directory  # noqa: E402

FAILED = 0


def check(label, cond, extra=""):
    global FAILED
    if cond:
        print(f"ok    {label}")
    else:
        FAILED += 1
        print(f"FAIL  {label} {extra}")


def bastion(tid, hostname, **kw):
    row = {
        "id": tid, "name": f"tok-{tid}", "hostname": hostname, "online": True,
        "last_seen_at": "2026-09-23T09:00:00+00:00",
        # Everything below is in the Network payload and must NOT reach Circles.
        "wg_pubkey": "WGKEY=", "reported_ip": "203.0.113.9",
        "hardware_fingerprint": "fp", "freeagent_stores": {"s1": {"root": "/home/a"}},
        "pairing_grants": [{"id": 1, "caps": ["dcn"]}], "nac_posture": "strict",
        "pairings": [{"host_name": "x", "state": "pending"}],
        "unapproved_pairing_attempts": [], "other_bastions": [],
        "mode": None, "anchor": None, "enforcing": None, "peers": None,
    }
    row.update(kw)
    return row


ANCHOR = {"owner": "Alex", "anchor_host": "a1b2.host.example.test",
          "anchor_handle": "alex", "claimed_at": "2026-09-14T00:00:00Z",
          "public_key": "PUB", "signature": "SIG", "verified": True}

rows = [
    # Old bastion: says nothing about mode or peers.
    bastion(1, "old-box", online=False),
    # Paired, but the box has never sent a beat — what a pairing waiting for
    # the anchor looks like, since the bastion does not run a pending pairing.
    bastion(6, "waiting-box", online=False, last_seen_at=None),
    # Local, unanchored, serves only us.
    bastion(2, "laptop", mode="local", peers=[],
            enforcing={"pairing_state": "active"}),
    # Networked + anchored by ANOTHER host; we are a member. Three peers.
    bastion(3, "build-server", mode="networked", anchor=dict(ANCHOR),
            enforcing={"pairing_state": "active", "anchored": True, "is_anchor": False},
            peers=[
                {"host_name": "zed.host.example.test", "handle": "zed", "state": "pending", "since": "2026-09-20"},
                {"host_name": "bob.host.example.test", "handle": "bob", "since": "2026-09-01"},
                {"host_name": "a1b2.host.example.test", "handle": "alex", "state": "active"},
                {"host_name": "old.host.example.test", "handle": "old", "state": "revoked"},
            ]),
    # Anchored by US, but the stored claim did not verify.
    bastion(4, "desk", mode="networked",
            anchor=dict(ANCHOR, anchor_host="me.host.example.test", verified=False),
            enforcing={"pairing_state": "active", "is_anchor": True},
            peers=[{"host_name": "bob.host.example.test", "handle": "bob", "state": "active"}]),
]
peerings = [
    {"token_id": 3, "peer_host_name": "bob.host.example.test", "peer_entity_id": 42, "current": True},
    {"token_id": 3, "peer_host_name": "a1b2.host.example.test", "peer_entity_id": 3, "current": True},
    # bob resolved on machine 3 only; machine 4's bob must not borrow it.
    {"token_id": 4, "peer_host_name": "bob.host.example.test", "peer_entity_id": None, "current": True},
    # zed is known on machine 3 but no longer announced.
    {"token_id": 3, "peer_host_name": "zed.host.example.test", "peer_entity_id": 77, "current": False},
]

out = build_directory(rows, peerings)
by = {m["machine"]: m for m in out}

check("one entry per machine", len(out) == 5, str(len(out)))
check("online machines first, offline last",
      [m["online"] for m in out] == [True, True, True, False, False], str([m["machine"] for m in out]))

wb = by["waiting-box"]
check("never heard from -> heard False", wb["heard"] is False)
check("a machine that has beaten -> heard True", by["old-box"]["heard"] is True)

old = by["old-box"]
check("too old to report mode -> mode None", old["mode"] is None)
check("too old to report mode -> anchored None, not False", old["anchored"] is None)
check("never announced peers -> hosts None, not []", old["hosts"] is None)

lap = by["laptop"]
check("reports mode, no anchor -> anchored False", lap["anchored"] is False)
check("announced [] -> hosts []", lap["hosts"] == [])

bs = by["build-server"]
check("anchored by another host", bs["anchored"] is True and bs["anchor"]["is_me"] is False)
check("anchor handle and owner carried", bs["anchor"]["handle"] == "alex" and bs["anchor"]["owner"] == "Alex")
check("verified claim carried as verified", bs["anchor"]["verified"] is True)
order = [h["handle"] for h in bs["hosts"]]
check("anchor first, then active, pending, revoked", order == ["alex", "bob", "zed", "old"], str(order))
hmap = {h["handle"]: h for h in bs["hosts"]}
check("anchor host flagged is_anchor", hmap["alex"]["is_anchor"] and not hmap["bob"]["is_anchor"])
check("absent peer state reads active (pre-0.16 bastion)", hmap["bob"]["state"] == "active")
check("pending stays pending", hmap["zed"]["state"] == "pending")
check("resolved peering gives the host id", hmap["bob"]["host_id"] == 42 and hmap["alex"]["host_id"] == 3)
check("host id carried even when not current", hmap["zed"]["host_id"] == 77)
check("a peering no longer announced is not current", hmap["zed"]["current"] is False)
check("a current peering is current", hmap["bob"]["current"] is True)
check("no peering at all -> not current, no id",
      hmap["old"]["current"] is False and hmap["old"]["host_id"] is None)
check("since carried", hmap["zed"]["since"] == "2026-09-20" and hmap["alex"]["since"] is None)

desk = by["desk"]
check("is_me from the box's is_anchor", desk["anchor"]["is_me"] is True)
check("unverified claim stays unverified", desk["anchor"]["verified"] is False)
check("a host id never crosses machines", desk["hosts"][0]["host_id"] is None,
      str(desk["hosts"][0]))

# The anchor's is_me must come from the box, not from a name match: a claim
# naming a host that is also a peer does not make US the anchor.
spoof = build_directory([bastion(9, "m", mode="networked", anchor=dict(ANCHOR),
                                 enforcing={"pairing_state": "active"}, peers=[])], [])
check("no is_anchor from the box -> is_me False", spoof[0]["anchor"]["is_me"] is False)
big = build_directory([bastion(10, "big", mode="networked", anchor=dict(ANCHOR),
                               enforcing={"truncated": True, "grants": []}, peers=[])], [])
check("a truncated report -> is_me unknown (None)", big[0]["anchor"]["is_me"] is None)

blob = json.dumps(out)
for leaked in ("WGKEY=", "203.0.113.9", "/home/a", "caps", "strict", "SIG", "PUB",
               "hardware_fingerprint", "pairing_grants", "unapproved", "other_bastions"):
    check(f"does not carry {leaked!r}", leaked not in blob)
allowed = {"token_id", "machine", "online", "heard", "last_seen_at", "mode", "anchored",
           "anchor", "hosts"}
check("machine keys are exactly the narrowed set",
      all(set(m) == allowed for m in out), str([sorted(set(m) - allowed) for m in out]))

check("garbage rows are skipped, not raised on",
      build_directory([{"id": "x"}, bastion(5, "ok", peers=["junk", {"host_name": ""}])], [])[0]["hosts"] == [])

# The route: both permissions, and the real builder behind it.
src = open(os.path.join(REPO, "server/routes/circles.py"), encoding="utf-8").read()
i = src.find('@router.get("/bastions"')
head = src[i:i + 400] if i >= 0 else ""
check("route exists at /api/circles/bastions", i >= 0)
check("route requires read:circles", "_READ" in head)
check("route requires read:topology", 'requires_permission("read:topology")' in head)
check("route returns build_directory", "bastion_directory.build_directory(" in src[i:i + 1400])

# The Network app's copy of the same list (Circles cards plan, slice 9): under
# read:topology alone, so Network can offer Request connection and Message
# without holding read:circles. The same builder, so the two never differ.
nsrc = open(os.path.join(REPO, "server/routes/network.py"), encoding="utf-8").read()
j = nsrc.find('@router.get("/bastions/directory"')
nhead = nsrc[j:nsrc.find("\n", j)] if j >= 0 else ""
check("route exists at /api/network/bastions/directory", j >= 0)
check("it requires read:topology", 'requires_permission("read:topology")' in nhead)
check("it requires nothing else (no read:circles)", nhead.count("requires_permission(") == 1, nhead)
nbody = nsrc[j:j + 1400]
check("it returns build_directory over list_bastions and list_peerings",
      "bastion_directory.build_directory(" in nbody and "topology.list_bastions(" in nbody
      and "peer_discovery.list_peerings(" in nbody)

# The Network half: the link Open in Network sends ('bastion:<token id>') must
# open that machine's inspector. The app is plain JS, so its own harness runs it.
import shutil  # noqa: E402
import subprocess  # noqa: E402

node = shutil.which("node")
check("node is available for the Network half", node is not None)
if node:
    proc = subprocess.run([node, os.path.join(REPO, "scripts", "test_network_bastion_view.js")],
                          capture_output=True, text=True, timeout=60)
    sys.stdout.write(proc.stdout)
    check("the Network app opens the bastion a link names", proc.returncode == 0, proc.stderr[-400:])

print("PASS" if not FAILED else f"{FAILED} FAILED")
sys.exit(1 if FAILED else 0)
