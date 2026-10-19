"""
Host-side App SDK store — install + serve + integrity self-check (Phase 0, 0b/0c).

The host runs whatever SDK it has *installed*, not whatever file happens to be on
disk in the image. An SDK is installed only after its C-Series signature verifies
(`sdk_artifact.verify`); the installed runtime is self-checked against the recorded
SRI before it is ever served, and serving **fails closed** on any mismatch. The
image ships a C-Series-signed SDK as the offline trust-anchor floor
(`/app/native-sdk/app-sdk.cseries`); the independent distribution channel (0d) installs
newer SDKs on top without an image roll.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from server.applications import sdk_artifact

log = logging.getLogger("privatae.applications.sdk_store")

SDK_ROOT = Path("/var/lib/cseries/sdk")
CURRENT_LINK = SDK_ROOT / "current"
META_NAME = ".sdkmeta.json"
BUNDLE_NAME = "bundle.cseries"   # retained signed bundle, for signature re-verification
# 32-byte raw Ed25519 C-Series apps public key (seeded on boot; same key that
# verifies native app bundles — bundles.py docstring).
CSERIES_PUBKEY_PATH = Path("/etc/cseries/keys/cseries-apps.pub.raw")
# Image-baked floor bundle (built + signed before docker build, like native bundles).
FLOOR_BUNDLE = Path("/app/native-sdk/app-sdk.cseries")

_cached: dict | None = None


class SdkStoreError(RuntimeError):
    """No SDK installed, integrity self-check failed, or trust key missing."""


def _cseries_pubkey() -> bytes:
    try:
        raw = CSERIES_PUBKEY_PATH.read_bytes()
    except OSError as e:
        raise SdkStoreError(f"C-Series SDK pubkey unreadable ({CSERIES_PUBKEY_PATH}): {e}") from None
    if len(raw) != 32:
        raise SdkStoreError(f"C-Series SDK pubkey wrong length: {len(raw)}")
    return raw


def install(bundle_bytes: bytes) -> dict:
    """Verify a signed SDK bundle against the C-Series key and install it.

    Returns {version, hash, sri}. Raises SdkStoreError / SdkArtifactError on a bad
    signature or malformed bundle — an unverified SDK is never written as current.
    The `current` pointer is moved atomically so a concurrent reader never sees a
    half-installed version.
    """
    contents = sdk_artifact.verify(bundle_bytes, public_key_raw=_cseries_pubkey())
    dest = SDK_ROOT / contents.version
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in contents.components.items():
        (dest / name).write_bytes(data)
    meta = {
        "version": contents.version,
        "hash": contents.bundle_hash,
        "sri": contents.runtime_sri,
    }
    (dest / META_NAME).write_text(json.dumps(meta))
    # Retain the signed bundle so its Ed25519 signature can be RE-verified later
    # (sign-time gate, integrity invariant). Extracted components + the recorded
    # hash alone are forgeable by a filesystem attacker; the signature is not.
    (dest / BUNDLE_NAME).write_bytes(bundle_bytes)

    tmp = SDK_ROOT / ".current.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(contents.version)   # relative target
    tmp.replace(CURRENT_LINK)          # atomic swap

    global _cached
    _cached = None
    log.info("SDK installed: version=%s hash=%s", contents.version, contents.bundle_hash[:12])
    return meta


def verify_current() -> dict:
    """Re-verify the INSTALLED SDK's Privatae SIGNATURE (not just the runtime
    self-check) and return {version, hash}. The retained signed bundle is checked
    against the Privatae key, so a filesystem attacker who rewrote the runtime AND
    its recorded hash still can't pass — they can't forge the Ed25519 signature.

    This is the sign-time gate: refuse to sign an app unless the SDK that validated
    it is provably Privatae-signed + intact. Raises SdkStoreError if no SDK is
    installed, the retained bundle is missing, or the signature does not verify.
    """
    if not CURRENT_LINK.exists():
        raise SdkStoreError("no SDK installed")
    version_dir = CURRENT_LINK.resolve()
    bundle_path = version_dir / BUNDLE_NAME
    if not bundle_path.exists():
        raise SdkStoreError("installed SDK has no retained bundle to re-verify")
    contents = sdk_artifact.verify(
        bundle_path.read_bytes(), public_key_raw=_cseries_pubkey()
    )
    # And confirm the extracted runtime still matches (no post-install swap).
    _self_check(version_dir)
    return {"version": contents.version, "hash": contents.bundle_hash}


def _self_check(version_dir: Path) -> dict:
    """Re-hash the installed runtime and compare to the recorded SRI. Fail closed."""
    meta = json.loads((version_dir / META_NAME).read_text())
    runtime = (version_dir / sdk_artifact.RUNTIME_COMPONENT).read_bytes()
    sri = sdk_artifact._runtime_sri(runtime)
    if sri != meta.get("sri"):
        raise SdkStoreError(
            f"installed SDK runtime TAMPERED: sri {sri} != recorded {meta.get('sri')}"
        )
    return meta


def current() -> dict:
    """Return {version, hash, sri, runtime_js} for the installed SDK, self-checked.

    Raises SdkStoreError if no SDK is installed or the integrity self-check fails —
    callers (the injector / the /js/app-sdk.js route) must treat that as fail-closed
    and not serve isolated apps.
    """
    global _cached
    if _cached is not None:
        return _cached
    if not CURRENT_LINK.exists():
        raise SdkStoreError("no SDK installed")
    version_dir = CURRENT_LINK.resolve()
    meta = _self_check(version_dir)
    runtime = (version_dir / sdk_artifact.RUNTIME_COMPONENT).read_bytes()
    _cached = {**meta, "runtime_js": runtime}
    return _cached


def _floor_should_install() -> bool:
    """Whether to (re)install the image floor at boot. True when: no SDK is
    installed; the installed SDK predates bundle-retention (no retained bundle to
    re-verify — pre-0e); or the image floor differs from the installed one AND is
    not older than it.

    VERSION-AWARE SINCE 0d LANDED (2026-08-27). This used to return
    `floor_hash != inst_hash`, i.e. reinstall on ANY difference. That was written
    when the floor was the only source of an SDK, so "different" could only mean
    "newer". Once the registry pull (0d) shipped, hosts routinely run an SDK
    NEWER than the image they boot from, and a hash-only comparison silently
    reinstalled the older floor over it — `install()` has no downgrade guard, so
    a validly-signed older bundle went straight in. Measured on a release whose
    floor was still 1.1.0 while the fleet had pulled 1.3.0: every pod restart
    reverted the SDK to 1.1.0 (re-opening the cross-origin token leak that 1.3.0
    closed) until the hourly pull upgraded it again — and a fleet roll restarts
    every pod at once.

    Equal-version-different-hash still installs: that is an image shipping a
    rebuilt SDK at the same version, which is not a downgrade. Only a strictly
    OLDER floor is refused. `/var/lib/cseries/sdk` is on a persistent PVC, so an
    install survives image rolls; this is what lets a new image's SDK take
    effect."""
    if not CURRENT_LINK.exists():
        return True
    if not FLOOR_BUNDLE.exists():
        return False
    vd = CURRENT_LINK.resolve()
    if not (vd / BUNDLE_NAME).exists():
        return True
    try:
        floor_bytes = FLOOR_BUNDLE.read_bytes()
        floor_hash = sdk_artifact.bundle_hash(floor_bytes)
        meta = json.loads((vd / META_NAME).read_text())
        if floor_hash == meta.get("hash"):
            return False
        floor_version = sdk_artifact.read_version(floor_bytes)
        installed_version = meta.get("version")
        # Named, not inlined: this boolean IS the downgrade guard, and a sign
        # flip here silently re-opens the bug it exists to close. "The floor is
        # older" == "the installed SDK is strictly newer than the floor".
        floor_is_older = sdk_artifact.is_newer(installed_version, floor_version)
        if floor_is_older:
            log.info(
                "image SDK floor v%s is older than installed v%s — keeping the "
                "installed SDK (no downgrade)",
                floor_version, installed_version,
            )
            return False
        # Equal-or-newer floor, differing hash: an image shipping a rebuilt or
        # upgraded SDK. Not a downgrade, so it takes effect.
        return True
    except Exception:
        return True


def ensure_installed() -> dict | None:
    """Boot hook: (re)install the image-baked floor if needed (§_floor_should_install),
    then self-check. Returns {version, hash, sri} on success, or None when there is
    no serviceable SDK (the injector then declines to mark apps isolated).
    Best-effort — never fatal to boot."""
    try:
        SDK_ROOT.mkdir(parents=True, exist_ok=True)
        if _floor_should_install():
            if FLOOR_BUNDLE.exists():
                install(FLOOR_BUNDLE.read_bytes())
            elif not CURRENT_LINK.exists():
                log.warning("no installed SDK and no image floor at %s", FLOOR_BUNDLE)
                return None
        c = current()
        return {"version": c["version"], "hash": c["hash"], "sri": c["sri"]}
    except Exception as e:
        log.error("SDK ensure_installed FAILED — isolated apps not served: %s", e)
        return None
