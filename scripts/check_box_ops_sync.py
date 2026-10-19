#!/usr/bin/env python3
"""The fleet must not go degraded because the host is behind on op NAMES.

WHAT HAPPENED (measured 2026-09-15, per-pod entity DBs). Every bastion that
heartbeats was sitting at posture='degraded', across two accounts —
one: 26 degraded of 39 endpoints; the other: 1 of 1 — all with the same
last_decision:

    drift: the box answers management ops this host does not know:
    computer:inventory, computer:tree, computer:windows, ctx_extract,
    model:drop, model:list

Not cosmetic: posture is a PDP input (nac_controller._match / min_posture :250,
permits_inbound :254), so a min_posture rule was cutting real access to machines
that were behaving perfectly.

TWO CAUSES, AND THIS GATE COVERS BOTH.

1. KNOWN_BOX_OPS had 16 of the gateway's 37 ops. It is now generated from the Go
   by scripts/gen_box_ops.py, and the first half of this gate re-derives and
   diffs when a gateway checkout is reachable.

2. The gate that should have caught it could not. check_dcn_cn1.py compared
   KNOWN_BOX_OPS to a SECOND hand-typed list carrying the same omissions — a
   stale copy pinned against a stale copy. That is why the second half of this
   gate does not depend on a gateway checkout at all: it asserts the BEHAVIOUR,
   that an op the host cannot name is reported and never degrades. That holds
   whether or not the list is current, which is the property the old design
   lacked. A list will go stale again; being behind must stay survivable.

Run:  python3 scripts/check_box_ops_sync.py
      CSERIES_GATEWAY_SRC=/path/to/cseries-gateway python3 scripts/check_box_ops_sync.py
"""
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from server.network.nac_dcn import (  # noqa: E402
    KNOWN_BOX_OPS, grant_drift, unknown_box_ops,
)

FAILS = []
CHECKS = 0

# The exact vocabulary the live fleet reported on 2026-09-15, from
# network_bastion_heartbeat.enforcing->'mgmt_ops' in the entity pods. Not a
# guess at what a box sends — what one did send, while being called degraded.
LIVE_FLEET_OPS = [
    "backup_fetch", "computer:inventory", "computer:tree", "computer:windows",
    "ctx_del", "ctx_extract", "ctx_get", "ctx_put",
    "dcn_catalog", "dcn_grant_drop", "dcn_grant_sync", "dcn_list", "dcn_plan_apply",
    "dcn_read_range", "dcn_search", "dcn_stat",
    "model:drop", "model:list", "model:pull",
    "observe:disk", "observe:firewall", "observe:host", "observe:kernel",
    "observe:net", "observe:sockets", "observe:units",
    "pairing_anchor_consent", "pairing_grant_drop", "pairing_grant_sync", "pairing_list",
    "session:open", "session:root", "stepup_request", "stepup_status", "uptime",
]


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(msg)


def main():
    print("[live] the fleet's own report must not read as drift")
    # One granted store, held exactly — the ONLY thing that should be able to
    # degrade this box. Everything else here is the op vocabulary.
    expected = [{"store_id": 1, "signature": "s1"}]
    live = {"grants": [{"store_id": 1, "signature": "s1"}], "mgmt_ops": LIVE_FLEET_OPS}
    d = grant_drift(expected, live)
    check(d == [],
          "a real bastion's advertised vocabulary produces NO drift (it degraded the fleet)",
          f"got {d}")
    missing = sorted(set(LIVE_FLEET_OPS) - set(KNOWN_BOX_OPS))
    check(not missing, "every op the live fleet reports is known to this host",
          f"missing={missing}")
    check(unknown_box_ops(live) == [],
          "and none of them reads as unknown", f"got {unknown_box_ops(live)}")

    print("\n[behaviour] being behind must be survivable, not an outage")
    # The property that does not depend on the list being right. This is the one
    # the old design lacked: it assumed the list could be kept current by hand.
    ahead = {"grants": [{"store_id": 1, "signature": "s1"}],
             "mgmt_ops": LIVE_FLEET_OPS + ["op_from_a_future_bastion", "another:new"]}
    check(grant_drift(expected, ahead) == [],
          "a NEWER box advertising ops this host has never heard of is NOT drift",
          f"got {grant_drift(expected, ahead)}")
    check(unknown_box_ops(ahead) == ["another:new", "op_from_a_future_bastion"],
          "they are reported instead, sorted and deduped",
          f"got {unknown_box_ops(ahead)}")
    check(unknown_box_ops({"mgmt_ops": ["uptime"]}) == [],
          "a known op is never reported as unknown")
    check(unknown_box_ops(None) == [] and unknown_box_ops({}) == []
          and unknown_box_ops({"mgmt_ops": "junk"}) == [],
          "no report / no ops / a malformed list -> nothing to say, no raise")

    print("\n[grants] the real faults still degrade — unchanged")
    drifted = {"grants": [{"store_id": 1, "signature": "OLD"}, {"store_id": 7, "signature": "x"}],
               "mgmt_ops": LIVE_FLEET_OPS}
    d = grant_drift([{"store_id": 1, "signature": "s1"}, {"store_id": 2, "signature": "s2"}], drifted)
    check(len(d) == 3, "stale + missing + never-issued are all still drift", f"got {d}")
    check(any("stale" in x for x in d) and any("not held" in x for x in d)
          and any("never issued" in x for x in d),
          "and each is still named in plain words", f"got {d}")

    print("\n[sync] KNOWN_BOX_OPS vs the gateway source")
    sys.path[:0] = [os.path.join(_ROOT, "scripts")]
    import gen_box_ops
    src = gen_box_ops.find_src()
    if not src:
        # Not a pass and not a silent skip: say which half did not run, and why.
        print("  ....  SKIPPED — no gateway checkout reachable.\n"
              "        Set CSERIES_GATEWAY_SRC to re-derive. The behaviour checks above\n"
              "        do NOT depend on this and have run.")
    else:
        ops, problems = gen_box_ops.derive(src)
        check(not problems, "the gateway vocabulary builds and reports",
              f"problems={problems}")
        missing = sorted(set(ops) - set(KNOWN_BOX_OPS))
        extra = sorted(set(KNOWN_BOX_OPS) - set(ops))
        # Only MISSING can produce a wrong verdict on a real machine — see the
        # asymmetry note in nac_dcn.py. An extra op is reported, never a failure.
        check(not missing,
              f"every op the gateway answers at {src} is known to this host",
              f"missing={missing}\n        "
              f"regenerate: python3 scripts/gen_box_ops.py --src {src} --write")
        if extra:
            print(f"  ....  NOTE — known here, absent from this gateway tree: {extra}\n"
                  f"        Inert: no box can report a name nothing sends. Normally an op\n"
                  f"        from a gateway PR still in flight (_PENDING_BOX_OPS).")


# Guarded: test_box_ops_not_degraded.py imports LIVE_FLEET_OPS from here so the
# measured list has ONE copy. Without the guard that import runs this gate and
# exits, and the importing test silently never runs — which it did, once.
if __name__ == "__main__":
    main()
    print()
    if FAILS:
        print(f"BOX-OPS SYNC FAILED — {len(FAILS)} of {CHECKS}")
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)
    if CHECKS == 0:
        print("FAILED — no checks ran; a gate that asserts nothing is not a pass")
        sys.exit(1)
    print(f"BOX-OPS OK — {CHECKS}/{CHECKS}: the live fleet's vocabulary is clean, an unknown op "
          f"is reported and never degrades, and grant drift still degrades")
    sys.exit(0)
