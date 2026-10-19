"""
Offline permission-slip verification — the in-pod mirror of the bridge's
`_verify_slip` (`bridge/adapter.py`), so a host can verify a peer's slip WITHOUT a
bridge round-trip (the locked design: "Ed25519, offline-verifiable").

Two trust inputs, both supplied by the caller (kept pure/testable):
  trusted_keys — {key_id: ed25519 raw public-key bytes}. The verifier checks the
                 signature against the PINNED trusted key for the slip's key_id,
                 NEVER the `signing_public_key` embedded in the slip (an attacker
                 could put their own there). PeerTrust pins these from the bridge.
  revoked_ids  — set of revoked contract_ids, synced from the bridge CRL.

The canonical-terms encoding MUST byte-match the bridge's `_canonical_terms` or
every signature fails — keep them identical.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def canonical_terms(terms: dict) -> bytes:
    """Deterministic bytes the signature covers — EVERY term except `signature`,
    sorted keys, compact, ASCII. Byte-identical to bridge.adapter._canonical_terms
    (contract_id is included in the signed bytes)."""
    core = {k: v for k, v in terms.items() if k != "signature"}
    return json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


# Clock-skew allowance, symmetric, in seconds. Two hosts'
# clocks (and the bridge's, which stamps issued_at/expires_at) agree to well
# under a second when NTP runs; this covers a VM whose clock has drifted. It is
# bounded by the shortest slip the bridge issues — _SLIP_TTL_MIN, 60 s — so
# even the shortest slip keeps most of its life, and the default 1 h slip,
# renewed at its half-life (30 min), is never stretched by more than ~1%.
# Revocation does not depend on it: a revoked slip is refused by contract_id.
CLOCK_SKEW_S = 30


def _when(value):
    """An aware UTC datetime from an ISO string, or None when it is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)


def slip_key_id(slip) -> str | None:
    """The signing key a slip names, or None when the slip is not shaped like
    one (not a dict, `terms` not a dict, key_id not a non-empty string). ONE
    shape check for both lanes — the relay's inbox gate and the direct lane's
    peer_server — so a malformed slip is refused in words on either, never
    raised on."""
    if not isinstance(slip, dict):
        return None
    terms = slip.get("terms")
    if not isinstance(terms, dict):
        return None
    key_id = terms.get("key_id") or slip.get("key_id")
    return key_id if isinstance(key_id, str) and key_id else None


def verify_slip(slip: dict, presenter_entity_id, action, *,
                trusted_keys: dict, revoked_ids: set, expected_issuer=None):
    """Verify a presented slip OFFLINE. Returns (ok, reason, info). Never
    raises: anything not shaped like a slip is refused as "malformed slip".

    Checks, in order: well-formed → envelope contract_id matches signed terms →
    signature valid under the PINNED trusted key for key_id → presenter is the
    grantee → (optional) issuer is us → issued (not in the future) and not
    expired, each within CLOCK_SKEW_S → not revoked → action allowed.
    """
    from datetime import timedelta
    malformed = (False, "malformed slip", {})
    if not isinstance(slip, dict):
        return malformed
    terms = slip.get("terms")
    if not isinstance(terms, dict) or not terms:
        return malformed
    contract_id = slip.get("contract_id")
    key_id = terms.get("key_id") or slip.get("key_id")
    sig_s = slip.get("signature")
    if not (isinstance(contract_id, str) and contract_id and isinstance(key_id, str)
            and key_id and isinstance(sig_s, str) and sig_s):
        return malformed
    try:
        sig = base64.b64decode(sig_s)
    except Exception:
        return malformed
    if contract_id != terms.get("contract_id"):
        return False, "contract_id mismatch (envelope vs signed terms)", {}

    pub = trusted_keys.get(key_id)
    if pub is None:
        return False, "unknown signing key (not pinned/trusted)", {}
    try:
        Ed25519PublicKey.from_public_bytes(bytes(pub)).verify(sig, canonical_terms(terms))
    except Exception:
        return False, "signature invalid (terms altered or not bridge-signed)", {}

    try:
        grantee = int(terms.get("grantee", -1))
        issuer = int(terms.get("issuer", -1))
    except (TypeError, ValueError):
        return malformed
    try:
        if presenter_entity_id is not None and grantee != int(presenter_entity_id):
            return False, "presenter is not the grantee", {}
        if expected_issuer is not None and issuer != int(expected_issuer):
            return False, "slip not issued by this host", {}
    except (TypeError, ValueError):
        return False, "presenter or issuer is not a host id", {}

    exp = _when(terms.get("expires_at"))
    if exp is None:
        return False, "bad expires_at", {}
    now = datetime.now(timezone.utc)
    skew = timedelta(seconds=CLOCK_SKEW_S)
    if now - skew >= exp:
        return False, "expired", {}
    issued = terms.get("issued_at")
    if issued is not None:
        t = _when(issued)
        if t is None:
            return False, "bad issued_at", {}
        if t > now + skew:
            return False, "not yet valid (issued in the future)", {}

    if contract_id in revoked_ids:
        return False, "revoked", {}

    ops = terms.get("allowed_ops") or []
    if not isinstance(ops, list):
        return malformed
    if action is not None and action not in ops:
        return False, f"action '{action}' not in allowed_ops", {}

    return True, "valid", {
        "issuer": terms.get("issuer"),
        "grantee": terms.get("grantee"),
        "resource": terms.get("resource"),
        "allowed_ops": ops,
        "reasoning": terms.get("reasoning"),
        "contract_id": contract_id,
    }
