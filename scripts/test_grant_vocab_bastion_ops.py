#!/usr/bin/env python3
"""The ops a Host may grant are the ops the current bastion answers.

A grant is matched op name by op name (nac_mgmt.mgmt_decision), so a name the
Host's list lacks can never be granted, and a name the bastion does not answer
authorizes nothing. The Host's list (nac_dcn.KNOWN_BOX_OPS, from which
grant_vocab.HOST_ALLOW / HOST_DENY are derived) is generated from the bastion's
mgmtOpVocabulary() by scripts/gen_box_ops.py, which needs the bastion's Go
source and a Go toolchain. CI has neither, so this pins the list here:

  V  the generated block is exactly the vocabulary below, taken from
     privatae-bastion a87f11c (main after 0.60.0, 2026-11-02). A regeneration
     against a newer bastion changes the block, fails this, and the person
     doing it updates the pin and the commit named here in the same change.
  O  the four read-only observe ops of 0.58.0 (grant_vocab.OBSERVE_NEW) may be
     granted and denied, and the observe purpose drafts and words them.
  L  the bastion's local CLI reads (observe:baseline, observe:delta,
     observe:baseline:show) are not ops a Host can send, so no grant names them.
  M  op_grant_drop is known, and never a Host's to be granted or denied.
  M  op_grant_drop is known, and never a Host's to be granted or denied.
  P  every op a purpose drafts is one the Host may grant.

Run: python3 scripts/test_grant_vocab_bastion_ops.py
"""
import os
import sys

_ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from server.network import grant_vocab, nac_dcn  # noqa: E402

FAILS = []
CHECKS = 0


def check(cond, msg, extra=""):
    global CHECKS
    CHECKS += 1
    print(("  PASS  " if cond else "  FAIL  ") + msg + (("\n        " + str(extra)[:600]) if extra else ""))
    if not cond:
        FAILS.append(msg)


# mgmtOpVocabulary() (client-go/enforcing.go: handlerOps + sessionOps +
# p8AllowedOps) at privatae-bastion a87f11c, as scripts/gen_box_ops.py
# --src /opt/privatae-bastion --check reported it on 2026-11-02: 65 ops.
BASTION_A87F11C = frozenset({
    "audit_fetch", "backup_fetch", "backup_list", "backup_push", "backup_reseal",
    "coder_grant_drop", "coder_grant_sync", "computer:inventory", "computer:tree",
    "computer:windows", "ctx_del", "ctx_extract", "ctx_get", "ctx_put", "dcn_catalog",
    "dcn_grant_drop", "dcn_grant_sync", "dcn_list", "dcn_plan_apply", "dcn_read_range",
    "dcn_search", "dcn_stat", "device_request", "device_status", "model:drop",
    "model:list", "model:pull", "observe:alerts", "observe:cert", "observe:disk",
    "observe:firewall", "observe:folders", "observe:host", "observe:journal",
    "observe:k8s", "observe:kernel", "observe:logins", "observe:logs", "observe:metrics",
    "observe:net", "observe:sockets", "observe:units", "op_apply", "op_dry_run",
    "op_grant_drop", "op_grant_run", "op_list", "op_plan_line", "op_plan_status",
    "op_plan_undo", "op_run_status", "op_sync", "pairing_anchor_consent",
    "pairing_grant_drop", "pairing_grant_sync", "pairing_list", "pairing_redeem",
    "service_grant_drop", "service_grant_sync", "session:open", "session:root",
    "session:root:exec", "stepup_request", "stepup_status", "uptime",
})

# V
check(len(BASTION_A87F11C) == 65, "V the pin holds the 65 ops the generator reported", len(BASTION_A87F11C))
gen = set(nac_dcn._GATEWAY_BOX_OPS)
check(gen == set(BASTION_A87F11C),
      "V the generated box-op block is the bastion's vocabulary at a87f11c",
      f"missing={sorted(BASTION_A87F11C - gen)} extra={sorted(gen - BASTION_A87F11C)}")
check(set(nac_dcn.KNOWN_BOX_OPS) >= set(BASTION_A87F11C),
      "V every op the bastion answers is known to the Host",
      sorted(set(BASTION_A87F11C) - set(nac_dcn.KNOWN_BOX_OPS)))
pending_released = set(nac_dcn._PENDING_BOX_OPS) & set(BASTION_A87F11C)
check(not pending_released,
      "V no op the released bastion answers is still parked as pending", sorted(pending_released))

# O
for op in grant_vocab.OBSERVE_NEW:
    check(op in BASTION_A87F11C, f"O {op} is answered by the bastion")
    check(op in grant_vocab.HOST_ALLOW, f"O {op} may be granted")
    check(op in grant_vocab.HOST_DENY, f"O {op} may be denied")
check(grant_vocab.unknown_tokens(list(grant_vocab.OBSERVE_NEW)) == [],
      "O a grant naming the four new observe ops is not refused",
      grant_vocab.unknown_tokens(list(grant_vocab.OBSERVE_NEW)))
check(set(grant_vocab.OBSERVE_NEW) <= set(grant_vocab.PURPOSES["observe"]["ops"]),
      "O the observe purpose drafts them")
job = grant_vocab.PURPOSES["observe"]["job"]
check(all(w in job for w in ("folders", "logged", "sign in", "certificate")),
      "O the observe job says the four reads in the person's words", job)

# L
local_only = ["observe:baseline", "observe:delta", "observe:baseline:show"]
check(not (set(local_only) & set(nac_dcn.KNOWN_BOX_OPS)),
      "L the bastion's local CLI reads are not box ops")
check(grant_vocab.unknown_tokens(local_only) == local_only
      and grant_vocab.unknown_tokens(local_only, deny=True) == local_only,
      "L a grant or a deny naming one is refused", grant_vocab.unknown_tokens(local_only))

# M: op_grant_drop takes a standing op approval back; the machine answers
# it, and it is the person's tighten, never a job a Host is granted.
check("op_grant_drop" in nac_dcn.KNOWN_BOX_OPS and "op_grant_drop" not in grant_vocab.HOST_ALLOW
      and "op_grant_drop" not in grant_vocab.HOST_DENY,
      "M op_grant_drop is known, and a Host may neither be granted nor denied it")

# M: op_grant_drop takes a standing op approval back; the machine answers
# it, and it is the person's tighten, never a job a Host is granted.
check("op_grant_drop" in nac_dcn.KNOWN_BOX_OPS and "op_grant_drop" not in grant_vocab.HOST_ALLOW
      and "op_grant_drop" not in grant_vocab.HOST_DENY,
      "M op_grant_drop is known, and a Host may neither be granted nor denied it")

# P
for name, p in grant_vocab.PURPOSES.items():
    bad = [o for o in p["ops"] if o not in grant_vocab.HOST_ALLOW]
    check(not bad, f"P every op the {name} purpose drafts may be granted", bad)

print()
if FAILS:
    print(f"FAILED — {len(FAILS)} of {CHECKS}")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print(f"grant vocabulary matches bastion a87f11c: {CHECKS}/{CHECKS} checks passed")
