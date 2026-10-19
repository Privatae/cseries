"""
Direct host↔host peer transport (Workstream B, protocol §6.5/§10.3).

The data plane for host↔host: instead of relaying every message through the
governance bridge, a host connects DIRECTLY to a peer's `:8444` mTLS listener and
presents a bridge-signed permission slip. The peer verifies BOTH locally — the
client cert (mTLS = WHO) and the slip (Ed25519 over canonical terms = WHAT) — with
NO bridge in the data path. The bridge stays control-plane: it signs slips
(`contract.issue`), publishes its signing public keys (`contract.signing_keys`),
and feeds the CRL (`contract.revoked_since`); the host pins those and verifies
offline (verifiers recompute and verify, never look up).

Pieces:
  slip_verify.py — pure offline slip verification (port of the bridge's _verify_slip).
  peer_trust.py  — pins the bridge signing pubkeys + syncs the CRL, both via the
                   host's existing trusted mTLS bridge channel; periodic refresh.
  peer_server.py — the in-pod :8444 mTLS terminator + slip-gated op dispatch.
  peer_client.py — dial a peer's :8444 (deterministic svc DNS) with our relay cert.

Transport-agnostic: circles.send_message tries this direct lane first and falls
back to the bridge relay if it's unavailable.
"""
