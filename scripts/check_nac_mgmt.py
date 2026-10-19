#!/usr/bin/env python3
"""P8.1/H4 management+harness grant invariant: the pod gate is DEFAULT-DENY and enforces signed grants.

A verified grant authorizes (server, op); anything it does not cover is denied. The harness gate
(H4) additionally enforces an IMAGE ALLOWLIST: a harness dispatch needs harness:run on the server
AND the requested image enumerated in the SAME signed grant. The signature check + signed-expiry
live in load_mgmt_grants (DB path); this asserts the pure decision logic. Mirrors
scripts/check_nac_egress.py. Exit 0 = invariant holds.
"""
import sys

sys.path.insert(0, "/app")
from server.network.nac_mgmt import (  # noqa: E402
    mgmt_decision, mgmt_denied_by, deny_reason, harness_image_allowed, harness_denied_by,
    _image_token, HARNESS_RUN_OP, HARNESS_DEFAULT_IMAGE_TOKEN, CONTAIN_OP, DENY_ALL,
    LAB_MODE_TOKEN,
)

fail = 0


def check(name, cond):
    global fail
    print(("OK   " if cond else "FAIL ") + name)
    if not cond:
        fail = 1


G = [{"server": "box-a", "ops": ["uptime", "df"]}]

# ── mgmt (/mgmt/run): DEFAULT-DENY (the inverse of egress's default-open) ──
check("no grants => deny", mgmt_decision([], 3, "box-a", "uptime") is False)
check("ungranted op on a granted server => deny", mgmt_decision(G, 3, "box-a", "whoami") is False)
check("granted op on a DIFFERENT server => deny", mgmt_decision(G, 3, "box-b", "uptime") is False)
# ALLOW only when a grant covers (server, op).
check("granted op on the granted server => allow", mgmt_decision(G, 3, "box-a", "uptime") is True)
check("second granted op => allow", mgmt_decision(G, 3, "box-a", "df") is True)
# A '*' server grant covers any host in the mesh.
check("wildcard '*' server grant", mgmt_decision([{"server": "*", "ops": ["uptime"]}], 3, "anyhost", "uptime") is True)
# NAC agent: terminating a detected agent is a management op like any
# other — no grant listing 'contain' for the server, no kill.
check("contain: no grant => deny", mgmt_decision(G, 3, "box-a", CONTAIN_OP) is False)
check("contain: granted => allow",
      mgmt_decision([{"server": "box-a", "ops": [CONTAIN_OP]}], 3, "box-a", CONTAIN_OP) is True)
check("contain: suspended server denies even with the grant",
      mgmt_decision([{"server": "box-a", "ops": [CONTAIN_OP]}], 3, "box-a", CONTAIN_OP,
                    suspended={"box-a"}) is False)

# ── plain-text grants: DENY BEATS ALLOW, across grants ──
D_same = [{"server": "boxA", "ops": ["uptime", "df"], "deny": ["df"]}]
D_split = [{"server": "boxA", "ops": ["uptime", "df"]},
           {"server": "boxA", "ops": [], "deny": ["df"], "intent": {"do_not_want": "never touch disks"}}]
D_star = [{"server": "boxA", "ops": ["uptime"]}, {"server": "*", "deny": ["uptime"]}]
D_all = [{"server": "boxA", "ops": ["uptime"]}, {"server": "boxA", "deny": [DENY_ALL]}]
check("deny: allowed op still allowed when not denied", mgmt_decision(D_same, 3, "boxA", "uptime") is True)
check("deny: same-grant deny beats its own allow", mgmt_decision(D_same, 3, "boxA", "df") is False)
check("deny: a deny in ANOTHER grant beats this grant's allow", mgmt_decision(D_split, 3, "boxA", "df") is False)
check("deny: '*' server deny covers a concrete server", mgmt_decision(D_star, 3, "boxA", "uptime") is False)
check("deny: DENY_ALL forbids everything on the server", mgmt_decision(D_all, 3, "boxA", "uptime") is False)
check("deny: a deny on boxA does not reach boxB",
      mgmt_decision([{"server": "boxB", "ops": ["df"]}, {"server": "boxA", "deny": ["df"]}], 3, "boxB", "df") is True)
check("deny: mgmt_denied_by returns the denying grant with its intent",
      (mgmt_denied_by(D_split, "boxA", "df") or {}).get("intent", {}).get("do_not_want") == "never touch disks")
check("deny: mgmt_denied_by is None when nothing denies", mgmt_denied_by(D_split, "boxA", "uptime") is None)
check("deny: reason quotes the user's words", "never touch disks" in deny_reason(mgmt_denied_by(D_split, "boxA", "df")))
check("deny: a grant signed before v125 (no deny key) still allows",
      mgmt_decision([{"server": "boxA", "ops": ["uptime"]}], 3, "boxA", "uptime") is True)

# ── harness (/harness/run, H4): signed grant for harness:run + IMAGE ALLOWLIST ──
IMG = "registry.example/cseries-harness:1"
Hrun_only = [{"server": "boxA", "ops": [HARNESS_RUN_OP]}]
Hdefault  = [{"server": "boxA", "ops": [HARNESS_RUN_OP, HARNESS_DEFAULT_IMAGE_TOKEN]}]
Himg      = [{"server": "boxA", "ops": [HARNESS_RUN_OP, "image:" + IMG]}]
Hstar     = [{"server": "boxA", "ops": [HARNESS_RUN_OP, "image:*"]}]
Hsplit    = [{"server": "boxA", "ops": [HARNESS_RUN_OP]},
             {"server": "boxA", "ops": ["image:" + IMG]}]

check("harness: no grant => deny", harness_image_allowed([], 3, "boxA", IMG) is False)
check("harness: run granted but image not listed => deny (named)",
      harness_image_allowed(Hrun_only, 3, "boxA", IMG) is False)
check("harness: run granted but image not listed => deny (omitted/default)",
      harness_image_allowed(Hrun_only, 3, "boxA", None) is False)
check("harness: default sentinel allows the omitted/default image",
      harness_image_allowed(Hdefault, 3, "boxA", None) is True)
check("harness: default sentinel does NOT allow a named image",
      harness_image_allowed(Hdefault, 3, "boxA", IMG) is False)
check("harness: exact image grant allows that image",
      harness_image_allowed(Himg, 3, "boxA", IMG) is True)
check("harness: exact image grant denies a different image",
      harness_image_allowed(Himg, 3, "boxA", IMG + "-other") is False)
check("harness: image:* allows any named image", harness_image_allowed(Hstar, 3, "boxA", "x:y") is True)
check("harness: image:* allows the omitted/default image too", harness_image_allowed(Hstar, 3, "boxA", None) is True)
check("harness: wrong server => deny even with image:*", harness_image_allowed(Hstar, 3, "boxB", "x:y") is False)
check("harness: NO cross-grant compose (run + image must be ONE grant)",
      harness_image_allowed(Hsplit, 3, "boxA", IMG) is False)
check("harness: '*' server grant covers any host",
      harness_image_allowed([{"server": "*", "ops": [HARNESS_RUN_OP, "image:*"]}], 3, "anyhost", "z") is True)
check("token: omitted/blank image => default sentinel",
      _image_token(None) == HARNESS_DEFAULT_IMAGE_TOKEN and _image_token("  ") == HARNESS_DEFAULT_IMAGE_TOKEN)
# deny beats allow on the harness gate too — the run op, the image, or the mode
check("harness deny: harness:run denied in another grant => deny",
      harness_image_allowed(Hstar + [{"server": "boxA", "deny": [HARNESS_RUN_OP]}], 3, "boxA", IMG) is False)
check("harness deny: this image denied => deny even under image:*",
      harness_image_allowed(Hstar + [{"server": "*", "deny": ["image:" + IMG]}], 3, "boxA", IMG) is False)
check("harness deny: a different image is still allowed",
      harness_image_allowed(Hstar + [{"server": "*", "deny": ["image:" + IMG]}], 3, "boxA", "other:1") is True)
check("harness deny: mode:lab denied => lab run refused, ordinary run stands",
      harness_image_allowed(Hstar + [{"server": "boxA", "ops": [LAB_MODE_TOKEN]}, {"server": "boxA", "deny": [LAB_MODE_TOKEN]}],
                            3, "boxA", IMG, mode="lab") is False
      and harness_image_allowed(Hstar + [{"server": "boxA", "deny": [LAB_MODE_TOKEN]}], 3, "boxA", IMG) is True)
check("harness deny: harness_denied_by names the grant",
      harness_denied_by(Hstar + [{"server": "boxA", "deny": [HARNESS_RUN_OP]}], "boxA", IMG) is not None
      and harness_denied_by(Hstar, "boxA", IMG) is None)

print("nac_mgmt P8.1/H4 grant gate: " + ("PASS" if not fail else "FAIL"))
sys.exit(fail)
