"""Unit test for the App SDK host pull loop (Phase 0, 0d.3).

Proves the rotation policy: install a strictly-newer signed SDK, no-op on same,
never downgrade, refuse a newer bundle signed by any other key, and degrade (no
raise) when the bridge is down/absent. Runs with system python (cryptography +
pyyaml) on any box.

It also checks the floor this tree SHIPS the way a Host does on boot: it must
verify under etc/cseries-apps.pub.raw, the public key the image installs, or
no Host would install it. The rotation test re-signs the floor with a throwaway
key, so without this the lane passed whatever key had signed the shipped floor
(review of preview-animated at e296d06c). Public key only.

NO PRIVATE KEY. This used to sign with the operator's app-signing key
— the key every Host trusts for its SDK — and the bundle it signed was a version
NEWER than the floor (1.3.1 against 1.3.0), written to a temp dir it removed only
when every assertion passed. A failed run left a validly signed, fleet-acceptable
SDK on disk (review of preview-animated at fa0dccbe). Now each run makes a
throwaway Ed25519 key and points the store at its public half; the image floor
this tree ships is re-signed with it, so the versions still come from the real
floor, and nothing this test signs verifies against the Privatae key.

    python3 tests/test_sdk_pull.py
"""
import asyncio
import base64
import pathlib
import shutil
import sys

# The tree this file lives in (APP_ROOT to point elsewhere). It used to be
# a fixed operator path, which does not exist on the build box, so this never ran.
ROOT = pathlib.Path(__import__("os").environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.applications import bundles, sdk_artifact, sdk_pull, sdk_store

FLOOR = ROOT / "native-sdk" / "app-sdk.cseries"
# The key every Host's SDK store trusts: the image copies this file to
# /etc/cseries/keys/cseries-apps.pub.raw (Dockerfile.linux), sdk_store's
# CSERIES_PUBKEY_PATH. A public key: nothing here can sign with it.
PUBKEY = ROOT / "etc" / "cseries-apps.pub.raw"


def throwaway_key() -> tuple[bytes, bytes]:
    """(private PEM, raw public) of a key made for this run and never stored."""
    k = Ed25519PrivateKey.generate()
    return (k.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption()),
            k.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw))


class _BC:
    def __init__(self, data):
        self.data = data

    async def request(self, op, params):
        assert op == "sdk.current"
        return self.data


class _Boom:
    async def request(self, op, params):
        raise RuntimeError("bridge down")


def shipped_floor_is_trusted() -> str:
    """The shipped floor, checked as a Host checks it: verified under the
    public key the image installs, then installed and re-verified through the
    real sdk_store in a temp root. A key that is not the Host's refuses it,
    so the check can fail. Returns the floor's version."""
    import tempfile
    from server.applications.sdk_artifact import SdkArtifactError
    pub = PUBKEY.read_bytes()
    assert len(pub) == 32, f"{PUBKEY} is not a raw Ed25519 public key ({len(pub)} bytes)"
    shipped = FLOOR.read_bytes()
    contents = sdk_artifact.verify(shipped, public_key_raw=pub)
    assert contents.version == sdk_artifact.read_version(shipped)
    _, other_pub = throwaway_key()
    try:
        sdk_artifact.verify(shipped, public_key_raw=other_pub)
        raise AssertionError("the shipped floor verified under a key that is not the Host's")
    except SdkArtifactError:
        pass
    saved = (sdk_store.SDK_ROOT, sdk_store.CURRENT_LINK, sdk_store.CSERIES_PUBKEY_PATH,
             sdk_store.FLOOR_BUNDLE, sdk_store._cached)
    work = pathlib.Path(tempfile.mkdtemp(prefix="sdkfloor-trust-"))
    try:
        sdk_store.SDK_ROOT = work / "sdk"
        sdk_store.CURRENT_LINK = work / "sdk" / "current"
        sdk_store.CSERIES_PUBKEY_PATH = PUBKEY
        sdk_store.FLOOR_BUNDLE = FLOOR
        sdk_store._cached = None
        sdk_store.ensure_installed()
        assert sdk_store.current()["version"] == contents.version, sdk_store.current()
        sdk_store.verify_current()
    finally:
        (sdk_store.SDK_ROOT, sdk_store.CURRENT_LINK, sdk_store.CSERIES_PUBKEY_PATH,
         sdk_store.FLOOR_BUNDLE, sdk_store._cached) = saved
        shutil.rmtree(work, ignore_errors=True)
    return contents.version


def main() -> int:
    assert FLOOR.is_file(), f"the image SDK floor {FLOOR} is missing from this tree"
    trusted = shipped_floor_is_trusted()
    print(f"PASS the shipped SDK floor {trusted} verifies under etc/cseries-apps.pub.raw and installs")
    priv_pem, pub = throwaway_key()
    other_pem, _ = throwaway_key()
    import tempfile
    work = pathlib.Path(tempfile.mkdtemp(prefix="sdkpull-"))
    try:
        return _run(work, priv_pem, pub, other_pem)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _run(work: pathlib.Path, priv_pem: bytes, pub: bytes, other_pem: bytes) -> int:
    # The floor this tree ships, re-signed with the throwaway key: same version,
    # same components, a signature only this run's store accepts.
    shipped = FLOOR.read_bytes()
    floor_v = sdk_artifact.read_version(shipped)
    assert floor_v, "the shipped floor carries no version"
    floor = work / "floor.cseries"
    floor.write_bytes(sdk_artifact.sign(
        sdk_artifact.build_unsigned(floor_v, bundles._read_zip(shipped).payload), priv_pem))

    pubp = work / "pub.raw"; pubp.write_bytes(pub)
    root = work / "sdk"
    sdk_store.SDK_ROOT = root
    sdk_store.CURRENT_LINK = root / "current"
    sdk_store.CSERIES_PUBKEY_PATH = pubp
    sdk_store.FLOOR_BUNDLE = floor
    sdk_store._cached = None
    sdk_store.ensure_installed()

    assert sdk_pull._is_newer("1.0.1", "1.0.0")
    assert not sdk_pull._is_newer("1.0.0", "1.0.0")
    assert not sdk_pull._is_newer("0.9.0", "1.0.0")

    def signed(v, body, key=priv_pem):
        return sdk_artifact.sign(sdk_artifact.build_unsigned(v, {"app-sdk.js": body}), key)

    # Versions are read from the FLOOR this tree ships, never typed in: the
    # floor moves (1.0.0 when this was written, 1.3.0 by 2026-09-30), and a
    # "newer" that is older than the floor tests nothing.
    cur = sdk_store.current()
    assert cur["version"] == floor_v
    major, minor, patch = (int(x) for x in floor_v.split("."))
    newer_v = f"{major}.{minor}.{patch + 1}"
    older_v = "0.0.1"
    assert sdk_pull._is_newer(newer_v, floor_v) and not sdk_pull._is_newer(older_v, floor_v)

    # same hash → no rotate
    same_bundle = (root / "current").resolve() / "bundle.cseries"
    asyncio.run(sdk_pull._pull_once(_BC({
        "version": cur["version"], "sdk_hash": cur["hash"],
        "bundle_b64": base64.b64encode(same_bundle.read_bytes()).decode(),
    })))
    assert sdk_store.current()["version"] == floor_v

    # newer, but signed by a key the store does not trust → refused
    forged = signed(newer_v, b"/*forged*/", other_pem)
    asyncio.run(sdk_pull._pull_once(_BC({
        "version": newer_v, "sdk_hash": sdk_artifact.bundle_hash(forged),
        "bundle_b64": base64.b64encode(forged).decode(),
    })))
    assert sdk_store.current()["version"] == floor_v, "a bundle signed by another key was installed"

    # newer → rotate
    nb = signed(newer_v, b"/*newer*/")
    asyncio.run(sdk_pull._pull_once(_BC({
        "version": newer_v, "sdk_hash": sdk_artifact.bundle_hash(nb),
        "bundle_b64": base64.b64encode(nb).decode(),
    })))
    assert sdk_store.current()["version"] == newer_v

    # older → no downgrade
    ob = signed(older_v, b"old")
    asyncio.run(sdk_pull._pull_once(_BC({
        "version": older_v, "sdk_hash": sdk_artifact.bundle_hash(ob),
        "bundle_b64": base64.b64encode(ob).decode(),
    })))
    assert sdk_store.current()["version"] == newer_v

    # bridge absent / down → degrade, keep current
    asyncio.run(sdk_pull._pull_once(None))
    asyncio.run(sdk_pull._pull_once(_Boom()))
    assert sdk_store.current()["version"] == newer_v

    print("PASS test_sdk_pull — rotate-on-newer, no-op same, no-downgrade, foreign key refused, "
          "degrade-on-failure (throwaway key)")
    return 0


def test_the_shipped_floor_verifies_under_the_key_every_host_trusts():
    """The public-key check on its own, so the tests/ lane names it."""
    assert shipped_floor_is_trusted()


def test_sdk_pull_rotation_policy():
    """The pytest entry: this file was a standalone script only, so the tests/
    lane collected nothing from it and it ran nowhere."""
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
