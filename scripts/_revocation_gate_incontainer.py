#!/usr/bin/env python3
"""
App SDK signing-key revocation gate — runs INSIDE an entity/smoke container
against the local Postgres (peer auth over /var/run/postgresql).

The executable backstop for the trust-store revocation path that gates
community-app installs:
  - trust_store.revoke_key() / list_active_keys() (WHERE revoked_at IS NULL)
  - installer.install_bundle() loading ONLY active keys (installer.py:207-217)
  - bundles.verify_any() over the active set

The runtime logic is already correct; this gate keeps "revoke the Community key
-> new installs refused" from silently regressing (invariant-ratchet doctrine).

Self-contained: registers its OWN throwaway community-authorized key + a
throwaway static app, and deletes both in finally(). Prints GATE markers and
exits 0 (PASS) / 1 (FAIL).

Invoked by:
  - scripts/smoke_test.sh         : docker exec "$CNAME" python3 /app/scripts/_revocation_gate_incontainer.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

for p in ("/app", "/app/brain"):
    if p not in sys.path:
        sys.path.insert(0, p)

import asyncpg
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.applications import bundles, installer, trust_store
from server.applications.installer import InstallError

TEST_APP_ID = "revoke-test-app"
TEST_PUBLISHER = "REVOKE-TEST-throwaway"

# entity is the in-container DB role/db; the smoke container + entity pods both
# expose Postgres over the peer socket at /var/run/postgresql.
DSN = os.environ.get(
    "REVOCATION_GATE_DSN",
    "postgresql://entity@/hostmem?host=/var/run/postgresql",
)

MANIFEST = (
    f"id: {TEST_APP_ID}\n"
    "name: Revoke Test\n"
    "entrypoint: index.html\n"
    "provenance: community\n"
    "version: 0.0.1\n"
    "runtime:\n"
    "  kind: static\n"
    "permissions: []\n"
)


async def _run() -> int:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    fp = None
    markers: list[str] = []

    def mark(m: str) -> None:
        markers.append(m)
        print(m, flush=True)

    try:
        # application_signing_keys is declared in
        # migrations/v030_app_runtime_tables.sql:41 and applied by entrypoint.sh
        # before this container serves anything. trust_store.ensure_schema() used
        # to be called here and went with the rest of the runtime DDL.

        # Throwaway community-authorized key.
        priv = Ed25519PrivateKey.generate()
        pub_raw = priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        priv_pem = priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())
        key = await trust_store.register_key(
            pool, public_key=pub_raw, publisher=TEST_PUBLISHER,
            provenance_allowed=["community"])
        fp = key.fingerprint

        # Minimal community static bundle, signed by the throwaway key.
        d = Path(tempfile.mkdtemp())
        (d / "app.yaml").write_text(MANIFEST)
        (d / "index.html").write_text("<!doctype html><title>revoke-test</title>")
        signed = bundles.sign(bundles.pack(d), priv_pem)

        # GATE1 — verifies against the ACTIVE trust store while the key is live.
        active = await trust_store.list_active_keys(pool)
        try:
            bundles.verify_any(signed, trusted_keys=[k.public_key for k in active])
            mark("GATE1 VERIFY_OK")
        except Exception as e:
            mark(f"GATE1 VERIFY_FAIL {type(e).__name__} {e}")

        # GATE2 — install_bundle accepts it (free community static).
        try:
            await installer.install_bundle(
                pool, signed, source="manual", check_entitlement=False)
            mark("GATE2 INSTALL_OK")
        except Exception as e:
            mark(f"GATE2 INSTALL_FAIL {type(e).__name__} {e}")
        # Remove the test app so only the key-revocation effect is measured next.
        try:
            await installer.uninstall_app(pool, TEST_APP_ID, purge=True)
        except Exception:
            pass

        # REVOKE the key.
        await trust_store.revoke_key(pool, fp)

        # GATE3 — the SAME bundle no longer verifies (key excluded from active set).
        active2 = await trust_store.list_active_keys(pool)
        try:
            bundles.verify_any(signed, trusted_keys=[k.public_key for k in active2])
            mark("GATE3 STILL_VERIFIES")   # BAD — revocation had no effect
        except Exception as e:
            mark(f"GATE3 REFUSED {type(e).__name__}")

        # GATE4 — install_bundle now refuses end-to-end.
        try:
            await installer.install_bundle(
                pool, signed, source="manual", check_entitlement=False)
            mark("GATE4 STILL_INSTALLS")    # BAD — revoked key still installs
        except InstallError:
            mark("GATE4 REFUSED InstallError")
        except Exception as e:
            mark(f"GATE4 REFUSED {type(e).__name__}")
    finally:
        # Leave the trust store + registry exactly as found.
        try:
            if fp:
                await pool.execute(
                    "DELETE FROM application_signing_keys WHERE fingerprint=$1", fp)
            await pool.execute(
                "DELETE FROM installed_applications WHERE id=$1", TEST_APP_ID)
        except Exception:
            pass
        await pool.close()

    out = "\n".join(markers)
    failures: list[str] = []
    if "GATE1 VERIFY_OK" not in out:
        failures.append("live community key did not verify the signed bundle")
    if "GATE2 INSTALL_OK" not in out:
        failures.append("install refused a validly-signed community bundle while key live")
    if "GATE3 REFUSED" not in out or "GATE3 STILL_VERIFIES" in out:
        failures.append("revoked key still verifies the bundle (revocation had no effect)")
    if "GATE4 REFUSED InstallError" not in out or "GATE4 STILL_INSTALLS" in out:
        failures.append("install still accepts a bundle signed by a revoked key")

    if failures:
        print("[signing_key_revocation] FAIL:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("[signing_key_revocation] PASS — revoke Community key -> install refused, end to end")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
