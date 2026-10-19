"""
C-Series network package — the host's view of its sovereign network.

Components:
  topology.py       — read layer for the Network app: bastions + peers + health
  bastion_enroll.py — the host's "set up a bastion" + redeem control-plane actions

NOTE: this package does NOT control the host's outbound traffic. On a sovereign
network the user owns their outbound; the platform meters throughput only and
isolates tenants for the user's protection. The platform-imposed egress
allowlist/posture machinery (policy.py / proxy.py / state.py / audit.py +
system/network_postures.yaml) was an internet-blocking wall built off a
misread of "egress"; it was removed 2026-06-19.
"""
