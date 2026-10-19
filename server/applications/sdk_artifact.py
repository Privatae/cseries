"""
App SDK — the signed, versioned artifact (Phase 0 of the App SDK integrity model).

The App SDK is the trusted computing base for app validation: the runtime
(`app-sdk.js`) PLUS the manifest schema, scope catalog, build-skill, and validator
ruleset, packaged as ONE versioned unit. If it is poisoned it can certify compromised
apps, so it must be hashed + signed and its integrity verified at injection and at
app-signing time.

This module is the artifact layer. It reuses the existing `.cseries` bundle crypto
(`bundles._canonical_hash` + Ed25519 `sign`/`verify` — no new primitives): an SDK
bundle is a signed ZIP with the SAME structure as an app bundle, only its
`manifest.yaml` is an SDK *descriptor* (kind=app-sdk) and its `payload/` holds the SDK
components. It is signed by the **Privatae key only** — an entity key must never be
able to publish an SDK (that would let a host mint its own validator).
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass

import yaml

from server.applications import bundles

log = logging.getLogger("privatae.applications.sdk_artifact")

SDK_KIND = "app-sdk"
RUNTIME_COMPONENT = "app-sdk.js"


class SdkArtifactError(ValueError):
    """The SDK bundle is malformed, mis-signed, or not an SDK."""
    # Not a refusal of the caller's input (the platform's own SDK artifact): the platform's
    # (tool_call_ledger.refused_if_caller).
    caller_refusal = False


@dataclass(frozen=True)
class SdkContents:
    version: str                      # semver from the descriptor
    bundle_hash: str                  # hex sha256 — the signed artifact identity
    components: dict[str, bytes]      # component name -> bytes (incl app-sdk.js)
    runtime_js: bytes | None          # the app-sdk.js bytes the host serves
    runtime_sri: str | None           # "sha384-<b64>" for the <script integrity=...>


def _runtime_sri(runtime_js: bytes) -> str:
    """The Subresource Integrity value the browser checks against the served
    app-sdk.js. sha384 per the SRI spec; base64 of the raw digest."""
    return "sha384-" + base64.b64encode(hashlib.sha384(runtime_js).digest()).decode()


def build_unsigned(version: str, components: dict[str, bytes]) -> bytes:
    """Build an UNSIGNED SDK bundle from in-memory components.

    `components` maps a component filename (e.g. "app-sdk.js",
    "scope-catalog.json", "manifest-schema.json", "build-skill.md",
    "validator-ruleset.json") to its bytes. The descriptor records the version
    and the sorted component list so the signed hash covers WHAT the SDK is.
    """
    if not version or not isinstance(version, str):
        raise SdkArtifactError("SDK version must be a non-empty string")
    if RUNTIME_COMPONENT not in components:
        raise SdkArtifactError(f"SDK must include the {RUNTIME_COMPONENT!r} runtime")
    descriptor = {
        "kind": SDK_KIND,
        "version": version,
        "components": sorted(components),
    }
    descriptor_yaml = yaml.safe_dump(descriptor, sort_keys=True).encode("utf-8")
    # Reuse the bundle zip layout: manifest.yaml=descriptor, payload/=components,
    # no binaries. _build_zip is the same path the app bundler signs over.
    return bundles._build_zip(descriptor_yaml, dict(components), {}, signature=None)


def sign(unsigned_bundle: bytes, privatae_private_key_pem: bytes) -> bytes:
    """Sign an unsigned SDK bundle with the Privatae Ed25519 key. Central /
    publish-time only — the private key never lives on an entity host."""
    return bundles.sign(unsigned_bundle, privatae_private_key_pem)


def verify(bundle_bytes: bytes, *, public_key_raw: bytes) -> SdkContents:
    """Verify a signed SDK bundle against the C-Series public key and parse it.

    Raises SdkArtifactError on a bad signature, a malformed bundle, a non-SDK
    descriptor, or a missing runtime. This is the gate every consumer goes
    through before trusting an SDK (host install, injection, sign-time re-check).
    """
    try:
        contents = bundles.verify(bundle_bytes, public_key_raw=public_key_raw)
    except bundles.BundleError as e:
        raise SdkArtifactError(f"SDK signature/format invalid: {e}") from None

    try:
        descriptor = yaml.safe_load(contents.manifest_yaml) or {}
    except yaml.YAMLError as e:
        raise SdkArtifactError(f"SDK descriptor not valid YAML: {e}") from None
    if descriptor.get("kind") != SDK_KIND:
        raise SdkArtifactError(
            f"not an SDK bundle (kind={descriptor.get('kind')!r}, expected {SDK_KIND!r})"
        )
    version = descriptor.get("version")
    if not version or not isinstance(version, str):
        raise SdkArtifactError("SDK descriptor missing a string version")

    runtime = contents.payload.get(RUNTIME_COMPONENT)
    if not runtime:
        raise SdkArtifactError(f"SDK bundle missing {RUNTIME_COMPONENT!r}")

    bundle_hash = bundles._canonical_hash(
        contents.manifest_yaml, contents.payload, contents.binaries
    ).hex()

    return SdkContents(
        version=version,
        bundle_hash=bundle_hash,
        components=dict(contents.payload),
        runtime_js=runtime,
        runtime_sri=_runtime_sri(runtime),
    )


def bundle_hash(bundle_bytes: bytes) -> str:
    """The canonical hex hash of an SDK bundle WITHOUT verifying the signature.
    For logging / comparison only — never a substitute for verify()."""
    contents = bundles._read_zip(bundle_bytes)
    return bundles._canonical_hash(
        contents.manifest_yaml, contents.payload, contents.binaries
    ).hex()


def read_version(bundle_bytes: bytes) -> str | None:
    """The descriptor version of an SDK bundle WITHOUT verifying the signature.
    Same trust caveat as bundle_hash(): for comparison/ordering only, never a
    substitute for verify(). Returns None when the descriptor has no usable
    version, so callers treat "unknown" as the lowest-ordering case rather than
    trusting an unparsed string."""
    try:
        contents = bundles._read_zip(bundle_bytes)
        descriptor = yaml.safe_load(contents.manifest_yaml) or {}
        version = descriptor.get("version")
        return version if isinstance(version, str) and version else None
    except Exception:
        return None


def version_key(v: str | None) -> tuple:
    """Sort key for an SDK semver. Conservative by design: anything that does not
    parse as dotted integers sorts as (0,), the lowest possible value, so a
    malformed or attacker-chosen version string can never win a comparison."""
    try:
        return tuple(int(x) for x in (v or "0").split("."))
    except Exception:
        return (0,)


def is_newer(candidate: str | None, installed: str | None) -> bool:
    """True if `candidate` is strictly newer than `installed`.

    The version-ordering rule for the image-floor boot check
    (sdk_store._floor_should_install). sdk_pull._is_newer still carries its own
    identical implementation for the registry-pull path; the two agree today and
    both sort unparseable versions lowest. Collapsing them onto this one is worth
    doing the next time sdk_pull is touched — two copies of an ordering rule is
    how the two paths eventually disagree about which SDK is older."""
    return version_key(candidate) > version_key(installed)
