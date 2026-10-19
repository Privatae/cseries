#!/usr/bin/env python3
"""Invariant: fp-bound-from-csr (P1, Fork 4).

The bastion device fingerprint is bound from the device-SIGNED operator CSR at redeem,
never TOFU'd from a self-reported heartbeat field.

Pure (always):
  * a CSR carrying urn:privatae:hwfp:<fp> whose self-signature verifies -> fp extracted
  * a CSR with no fingerprint SAN -> None (old client => bearer-only, full access)
  * a corrupted CSR (broken self-signature) -> None (fail closed; never trust it)

DB (skippable via SKIP_DB=1):
  * upsert_heartbeat with a hardware_fingerprint in the payload does NOT bind it —
    the heartbeat is verify-not-bind; the redeem CSR is the only binder.

Run in-pod: docker exec <c> python3 /app/scripts/check_fp_from_csr.py
"""
import asyncio
import base64
import os
import secrets
import sys

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, os.environ.get("CSERIES_ROOT", "/app"))
from server.network.bastion_enroll import _fp_from_csr, _FP_SAN_PREFIX

TESTFP = "INV-FP-abc123XYZ"


def _csr(fp):
    """Build a self-signed operator CSR; embed the fp URI SAN when fp is not None."""
    key = ec.generate_private_key(ec.SECP256R1())
    b = x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "operator")]))
    if fp is not None:
        b = b.add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(_FP_SAN_PREFIX + fp)]),
            critical=False)
    csr = b.sign(key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.PEM).decode(), csr


def pure_tests():
    fails = []
    pem, csr = _csr(TESTFP)
    got = _fp_from_csr(pem)
    if got != TESTFP:
        fails.append(f"valid signed CSR: expected fp {TESTFP!r}, got {got!r}")

    pem_no, _ = _csr(None)
    if _fp_from_csr(pem_no) is not None:
        fails.append("CSR with no fingerprint SAN must yield None (old client -> bearer-only)")

    # Corrupt the DER so the self-signature no longer verifies -> must be rejected.
    der = bytearray(csr.public_bytes(serialization.Encoding.DER))
    der[len(der) // 4] ^= 0xFF
    bad_pem = ("-----BEGIN CERTIFICATE REQUEST-----\n"
               + base64.encodebytes(bytes(der)).decode()
               + "-----END CERTIFICATE REQUEST-----\n")
    if _fp_from_csr(bad_pem) is not None:
        fails.append("corrupted/tampered CSR must yield None (fail closed)")

    if _fp_from_csr("") is not None:
        fails.append("empty CSR must yield None")
    return fails


async def db_tests():
    fails = []
    if os.environ.get("SKIP_DB") == "1":
        print("  [db] SKIPPED (SKIP_DB=1)")
        return fails
    try:
        import asyncpg
        conn = await asyncpg.connect(host="/var/run/postgresql", user="entity", database="hostmem")
    except Exception as e:
        print(f"  [db] SKIPPED (database not reachable: {e})")
        return fails
    from server.network import topology
    eid = await conn.fetchval("SELECT id FROM entity_registry ORDER BY id LIMIT 1")
    if eid is None:
        print("  [db] SKIPPED (no entity in registry)")
        await conn.close()
        return fails
    tok = None
    try:
        tok = await conn.fetchval(
            "INSERT INTO mcp_client_tokens "
            "(entity_id, name, token_hash, url_handle, scopes, operator_kind, notes) "
            "VALUES ($1,'inv-fp-bastion',$2,$3,$4,'bastion','{}'::jsonb) RETURNING id",
            eid, secrets.token_bytes(32), secrets.token_hex(8), ["shape", "recall", "signal"])
        # A heartbeat that reports a fingerprint must NOT bind it (verify-not-bind).
        await topology.upsert_heartbeat(conn, eid, {
            "token_id": tok, "hardware_fingerprint": "EVIL-SELF-REPORTED-FP", "hostname": "inv"})
        bound = await conn.fetchval(
            "SELECT hardware_fingerprint FROM mcp_client_tokens WHERE id=$1", tok)
        if bound is not None:
            fails.append(f"heartbeat TOFU-bound a self-reported fp ({bound!r}) — Fork 4 requires verify-not-bind")
        else:
            print("  [db] OK — heartbeat did NOT bind a self-reported fp (verify-not-bind)")
    except Exception as e:
        fails.append(f"db: upsert_heartbeat test raised: {e}")
    finally:
        if tok is not None:
            await conn.execute("DELETE FROM network_bastion_heartbeat WHERE token_id=$1", tok)
            await conn.execute("DELETE FROM mcp_client_tokens WHERE id=$1", tok)
        await conn.close()
    return fails


def main():
    fails = pure_tests()
    fails += asyncio.run(db_tests())
    if fails:
        print("FP-FROM-CSR CHECK FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("FP-FROM-CSR INVARIANTS OK — fp extracted from a signed CSR, tampered CSR "
          "rejected (fail closed), no-SAN -> bearer-only, heartbeat does not bind a self-report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
