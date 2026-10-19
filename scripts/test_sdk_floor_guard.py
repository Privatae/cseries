#!/usr/bin/env python3
"""Gate: the image-baked SDK floor must never DOWNGRADE a newer installed SDK.

Pins a regression. The installer has no version check — it verifies
the Ed25519 signature and moves `current` — and the image floor is a validly
signed bundle. `_floor_should_install()` used to return
`floor_hash != inst_hash`, which was correct only while the floor was the ONLY
source of an SDK. Once the registry pull (0d) shipped, hosts routinely run an
SDK newer than the image they boot from, and the hash-only compare put the
older floor back over it. On one release the floor was still 1.1.0 while the
fleet had pulled 1.3.0, so a restart would have reverted every entity to
1.1.0 — re-opening the cross-origin token leak 1.3.0 closed — until the hourly
pull upgraded it again. A fleet roll restarts every pod at once.

Container-free: builds its own signed bundles in a temp dir, removed however
the run ends. NO REAL KEY: it used to sign with the Privatae key every Host
trusts — versions up to 1.10.0, newer than the fleet's — and to SKIP (exit 0)
on a box without that key, so it passed there having checked nothing. Each run
now makes a throwaway Ed25519 key; _floor_should_install() reads versions and
hashes, never the signature, so the key changes nothing it decides.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

def main() -> int:
    root = pathlib.Path(tempfile.mkdtemp(prefix="_sdkfloor"))
    try:
        return _run(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run(root: pathlib.Path) -> int:
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from server.applications import sdk_artifact, sdk_store

    # A key made for this run and never stored (see the docstring).
    _k = Ed25519PrivateKey.generate()
    key = _k.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption())
    pub = _k.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)

    def mk(version: str, body: bytes) -> bytes:
        return sdk_artifact.sign(
            sdk_artifact.build_unsigned(version, {"app-sdk.js": body}), key)

    sdk_store.SDK_ROOT = root
    sdk_store.CURRENT_LINK = root / "current"

    def wipe() -> None:
        for p in list(root.iterdir()):
            p.unlink() if (p.is_symlink() or p.is_file()) else shutil.rmtree(p)

    def setup(installed: bytes | None, floor: bytes) -> None:
        wipe()
        fp = root / "floor.cseries"
        fp.write_bytes(floor)
        sdk_store.FLOOR_BUNDLE = fp
        if installed is None:
            return
        c = sdk_artifact.verify(installed, public_key_raw=pub)
        d = root / c.version
        d.mkdir(parents=True, exist_ok=True)
        (d / sdk_store.BUNDLE_NAME).write_bytes(installed)
        (d / sdk_store.META_NAME).write_text(json.dumps(
            {"version": c.version, "hash": c.bundle_hash, "sri": c.runtime_sri}))
        sdk_store.CURRENT_LINK.symlink_to(c.version)

    # (label, floor, installed, expected _floor_should_install())
    cases = [
        # THE REGRESSION. Must stay False forever.
        ("older floor over newer installed -> REFUSED",
         mk("1.1.0", b"old"), mk("1.3.0", b"new"), False),
        ("newer floor over older installed -> installs",
         mk("1.4.0", b"newer"), mk("1.3.0", b"new"), True),
        ("same version, different content -> installs (not a downgrade)",
         mk("1.3.0", b"rebuilt"), mk("1.3.0", b"new"), True),
        ("identical floor and installed -> no work",
         mk("1.3.0", b"same"), mk("1.3.0", b"same"), False),
        ("unparseable installed version -> floor wins (recovery)",
         mk("1.3.0", b"good"), mk("99.99.99-evil", b"junk"), True),
        ("multi-digit ordering: floor 1.9.0 under installed 1.10.0 -> REFUSED",
         mk("1.9.0", b"old"), mk("1.10.0", b"new"), False),
    ]

    failures = 0
    for label, floor, installed, expect in cases:
        setup(installed, floor)
        got = sdk_store._floor_should_install()
        if got != expect:
            print(f"  FAIL {label}: expected {expect}, got {got}")
            failures += 1
        else:
            print(f"  ok   {label}")

    # No SDK present at all -> the floor is the only source, always installs.
    setup(None, mk("1.1.0", b"old"))
    if not sdk_store._floor_should_install():
        print("  FAIL nothing installed -> floor must install")
        failures += 1
    else:
        print("  ok   nothing installed -> floor installs")

    # Corrupt meta must FAIL OPEN to the signed floor (service recovery), never
    # brick: the self-check needs that same meta, so the installed SDK is
    # unservable anyway and the floor is the designed trust anchor.
    setup(mk("1.3.0", b"new"), mk("1.1.0", b"old"))
    (sdk_store.CURRENT_LINK.resolve() / sdk_store.META_NAME).write_text("{not json")
    if not sdk_store._floor_should_install():
        print("  FAIL corrupt meta -> floor must install (fail-open recovery)")
        failures += 1
    else:
        print("  ok   corrupt meta -> floor installs (fail-open recovery)")

    if failures:
        print(f"FAIL test_sdk_floor_guard — {failures} case(s)")
        return 1
    print("PASS test_sdk_floor_guard — no downgrade, upgrades apply, "
          "recovery paths fail open")
    return 0


if __name__ == "__main__":
    sys.exit(main())
