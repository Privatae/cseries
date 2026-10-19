"""Unit test for the App SDK signed artifact (Phase 0, integrity foundation).

Proves the core security property: a poisoned SDK (any tampered component, a
wrong/absent signature) fails verification, so it can never be trusted to certify
apps. Runs standalone with system python (cryptography + pyyaml); no infra.

    python3 tests/test_sdk_artifact.py
"""
import io
import sys
import zipfile

import os
import pathlib

# The tree this file lives in (APP_ROOT to point elsewhere). It used to be
# a fixed operator path, which does not exist on the build box, so this never ran.
sys.path.insert(0, os.environ.get("APP_ROOT") or str(pathlib.Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.applications import sdk_artifact as sa


def _keypair():
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()
    )
    raw = priv.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    return pem, raw


def main() -> int:
    priv_pem, pub_raw = _keypair()
    comps = {
        "app-sdk.js": b"(function(){window.CSeriesApp={};})();",
        "scope-catalog.json": b'{"scopes":[]}',
        "build-skill.md": b"# build skill",
    }
    signed = sa.sign(sa.build_unsigned("1.0.0", comps), priv_pem)

    # 1. roundtrip: a properly-signed SDK verifies + exposes version/hash/SRI.
    c = sa.verify(signed, public_key_raw=pub_raw)
    assert c.version == "1.0.0"
    assert c.runtime_js == comps["app-sdk.js"]
    assert c.runtime_sri.startswith("sha384-")
    assert len(c.bundle_hash) == 64

    # 2. a different (untrusted) key must NOT verify.
    _, other_pub = _keypair()
    try:
        sa.verify(signed, public_key_raw=other_pub)
        raise AssertionError("wrong key accepted")
    except sa.SdkArtifactError:
        pass

    # 3. POISONED RUNTIME — swap app-sdk.js after signing; must be rejected.
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(signed)) as zin, zipfile.ZipFile(buf, "w") as zout:
        for it in zin.infolist():
            data = zin.read(it)
            if it.filename == "payload/app-sdk.js":
                data = b"(function(){window.CSeriesApp={evil:1};})();"
            zout.writestr(it, data)
    try:
        sa.verify(buf.getvalue(), public_key_raw=pub_raw)
        raise AssertionError("poisoned runtime accepted")
    except sa.SdkArtifactError:
        pass

    # 4. an unsigned bundle must be rejected.
    try:
        sa.verify(sa.build_unsigned("1.0.0", comps), public_key_raw=pub_raw)
        raise AssertionError("unsigned accepted")
    except sa.SdkArtifactError:
        pass

    # 5. build refuses an SDK without the runtime component.
    try:
        sa.build_unsigned("1.0.0", {"scope-catalog.json": b"{}"})
        raise AssertionError("built without app-sdk.js")
    except sa.SdkArtifactError:
        pass

    print("PASS test_sdk_artifact — signed-SDK roundtrip + poison/wrong-key/unsigned rejection")
    return 0


def test_sdk_artifact_roundtrip_and_rejections():
    """The pytest entry: this file was a standalone script only, so the tests/
    lane collected nothing from it and it ran nowhere."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
