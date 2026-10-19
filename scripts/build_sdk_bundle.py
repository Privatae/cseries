#!/usr/bin/env python3
"""Build + sign the image-baked App SDK floor (native-sdk/app-sdk.cseries).

Run BEFORE docker build, like scripts/rebuild_factory_bundles.sh — the signed
floor is COPY'd into the image and installed on first boot as the offline
trust-anchor SDK. The independent distribution channel (0d) installs newer,
C-Series-signed SDKs on top without an image roll.

Signed with the key that signs the built-in app bundles (SIGN_KEY, the PEM whose
public half is etc/cseries-apps.pub.raw). Bump SDK_VERSION when any SDK component changes.

    SIGN_KEY=/path/to/key.pem python3 scripts/build_sdk_bundle.py
"""
import os
import pathlib
import sys

# The floor bundle is committed and then baked into the image, so it is built
# from the checkout this script lives in. CSERIES_REPO overrides.
_REPO = os.environ.get("CSERIES_REPO", str(pathlib.Path(__file__).resolve().parent.parent))

sys.path.insert(0, _REPO)

from cryptography.hazmat.primitives import serialization as ser

from server.applications import sdk_artifact

REPO = pathlib.Path(_REPO)
SIGN_KEY = pathlib.Path(os.environ.get("SIGN_KEY", ""))
OUT = REPO / "native-sdk" / "app-sdk.cseries"
SDK_VERSION = "1.3.1"   # 1.3.1: runtime comments rewritten as plain rationale; 1.3.0: fetch shim is SAME-ORIGIN only (token leak + CORS preflight break)


def main() -> int:
    if not os.environ.get("SIGN_KEY") or not SIGN_KEY.is_file():
        print("fatal: set SIGN_KEY to the Ed25519 private key (PEM) that signs the "
              f"built-in apps (got {os.environ.get('SIGN_KEY', '')!r})", file=sys.stderr)
        return 1
    runtime = (REPO / "dashboard" / "base" / "js" / "app-sdk.js").read_bytes()
    # The SDK is one versioned unit; for the 0b/0c slice it carries the runtime.
    # 0d/0e add scope-catalog.json, manifest-schema.json, build-skill.md,
    # validator-ruleset.json as further components (each bumps SDK_VERSION).
    components = {"app-sdk.js": runtime}

    unsigned = sdk_artifact.build_unsigned(SDK_VERSION, components)
    priv_pem = SIGN_KEY.read_bytes()
    signed = sdk_artifact.sign(unsigned, priv_pem)

    # Self-verify before writing (derive the public key from the private key).
    priv = ser.load_pem_private_key(priv_pem, password=None)
    pub_raw = priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    c = sdk_artifact.verify(signed, public_key_raw=pub_raw)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(signed)
    print(
        f"built SDK floor v{c.version} -> {OUT} ({len(signed)} bytes) "
        f"hash={c.bundle_hash[:12]} sri={c.runtime_sri[:24]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
