"""
Bundle format + Ed25519 signing for cseries apps.

A `.cseries` bundle is a ZIP file with this layout:

    bundle.cseries
    ├── manifest.yaml         # the app's manifest (loaded by manifest.py)
    ├── payload/              # everything the app needs to render or run
    │   ├── index.html
    │   ├── app.js
    │   ├── style.css
    │   └── icon*.png
    └── signature.bin         # Ed25519(64 bytes) over canonical_hash

The canonical hash is SHA-256 over a deterministic encoding of every
file in the bundle EXCEPT signature.bin. The encoding is:

    for each (path, content) sorted by path:
        write len(path_utf8) as 4 bytes BE
        write path_utf8
        write len(content) as 8 bytes BE
        write content

This makes the hash stable across re-zips and OS differences. The
signature attests to (manifest + payload). Replacing signature.bin
does not invalidate the hash; verification depends on a separate
trusted public key.

Public keys live in the `application_signing_keys` PG table. The
C-Series key is seeded from /etc/cseries/keys/cseries-apps.pub.raw on
container boot.
"""

from __future__ import annotations

import hashlib
import io
import logging
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization

log = logging.getLogger("privatae.applications.bundles")

SIGNATURE_FILENAME = "signature.bin"
MANIFEST_FILENAME = "manifest.yaml"
PAYLOAD_DIR = "payload"
BIN_DIR = "bin"


class BundleError(ValueError):
    """Bundle is malformed, mis-signed, or has an unrecognized signer."""


@dataclass(frozen=True)
class BundleContents:
    manifest_yaml: bytes
    payload: dict[str, bytes]    # relative path under payload/ -> bytes
    binaries: dict[str, bytes]   # relative path under bin/ -> bytes
    signature: bytes | None      # 64 bytes Ed25519, or None when unsigned


def _canonical_hash(
    manifest_yaml: bytes,
    payload: dict[str, bytes],
    binaries: dict[str, bytes],
) -> bytes:
    """Deterministic SHA-256 over the manifest + payload + bin trees."""
    h = hashlib.sha256()

    def feed(path: str, content: bytes) -> None:
        p = path.encode("utf-8")
        h.update(struct.pack(">I", len(p)))
        h.update(p)
        h.update(struct.pack(">Q", len(content)))
        h.update(content)

    feed(MANIFEST_FILENAME, manifest_yaml)
    for rel in sorted(payload):
        feed(f"{PAYLOAD_DIR}/{rel}", payload[rel])
    for rel in sorted(binaries):
        feed(f"{BIN_DIR}/{rel}", binaries[rel])

    return h.digest()


def _read_zip(zbytes: bytes) -> BundleContents:
    manifest_yaml: bytes | None = None
    payload: dict[str, bytes] = {}
    binaries: dict[str, bytes] = {}
    signature: bytes | None = None

    def _safe_rel(rel: str) -> str:
        # Zip-slip guard: a bundle is signature-covered, but a malicious (yet
        # validly-signed, e.g. community) bundle could carry payload/bin entries
        # that traverse out of the install dir. Reject absolute paths, any '..'
        # segment, and empty/dot segments so extract_payload can only write
        # inside the per-app dir.
        if rel.startswith("/") or rel == "":
            raise BundleError(f"unsafe bundle path (absolute/empty): {rel!r}")
        parts = rel.split("/")
        if any(seg in ("", ".", "..") for seg in parts):
            raise BundleError(f"unsafe bundle path (traversal/empty segment): {rel!r}")
        return rel

    try:
        with zipfile.ZipFile(io.BytesIO(zbytes), "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                data = zf.read(info)
                if name == MANIFEST_FILENAME:
                    manifest_yaml = data
                elif name == SIGNATURE_FILENAME:
                    signature = data
                elif name.startswith(f"{PAYLOAD_DIR}/"):
                    payload[_safe_rel(name[len(f"{PAYLOAD_DIR}/"):])] = data
                elif name.startswith(f"{BIN_DIR}/"):
                    binaries[_safe_rel(name[len(f"{BIN_DIR}/"):])] = data
                else:
                    raise BundleError(f"unexpected file in bundle: {name!r}")
    except zipfile.BadZipFile as e:
        raise BundleError(f"corrupt bundle: {e}") from None

    if manifest_yaml is None:
        raise BundleError(f"missing {MANIFEST_FILENAME}")

    return BundleContents(
        manifest_yaml=manifest_yaml,
        payload=payload,
        binaries=binaries,
        signature=signature,
    )



# Compiled bytecode is environment-specific and gitignored — packing it would
# make a signed bundle non-reproducible (two packs of the same source producing
# different bytes) and put a stray .pyc inside a distributable artifact. It also
# red-failed a ship: test_bundle_freshness compares packed content against the
# source tree, and a __pycache__ left by any local import reads as "the bundle
# is stale".
def _is_build_detritus(p) -> bool:
    return "__pycache__" in p.parts or p.suffix == ".pyc"

def pack(source_dir: Path, exclude_top=frozenset()) -> bytes:
    """Pack a source directory into an unsigned bundle.

    source_dir layout (the existing app.yaml-style folder):
        <source_dir>/
            app.yaml         (renamed to manifest.yaml in bundle)
            index.html
            app.js
            style.css
            icon*.png

    Everything except app.yaml gets dropped under payload/. `exclude_top`
    names top-level folders that are not part of this app — a project's
    module directories when its ROOT app is packed (each module ships as its
    own bundle, from its own directory).

    Nothing outside `source_dir` is read (server/applications/tree_walk.py):
    a bundle is installed elsewhere and can be published, so a link out of
    the folder would carry its target's bytes with it. A link to a file or a
    folder inside the folder is followed (through the same contained walk),
    and the bundle carries what it points at under the link's name, as it
    always has. A link the bundle cannot carry (out of the folder, absolute,
    broken, into a hidden or other module's folder, or looping back) is
    refused by name: BundleError, never a bundle silently missing it.
    """
    from server.applications.tree_walk import (LINKS_IN_TREE, TreeEscape,
                                               describe_refused, open_tree_no_escape)
    _own = ("app.yaml", "bin", "payload")

    def _skip(parts: tuple, is_dir: bool) -> bool:
        # Hidden top-level entries never ship: .history (artifact change
        # history), .git (project repos) — workspace bookkeeping, not app
        # payload. Nor do the top-level folders of other modules.
        head = parts[0]
        return head.startswith(".") or (head in exclude_top and head not in _own)

    try:
        tree = open_tree_no_escape(source_dir)
    except TreeEscape:
        raise BundleError(f"{source_dir} is a link or not a folder") from None
    manifest_yaml = None
    payload: dict[str, bytes] = {}
    binaries: dict[str, bytes] = {}
    top: dict[str, bytes] = {}
    refused: list = []
    with tree:
        for f in tree.walk(skip=_skip, links=LINKS_IN_TREE, refused=refused):
            parts = f.rel.split("/")
            head, sub = parts[0], "/".join(parts[1:])
            if f.rel == "app.yaml":
                manifest_yaml = tree.read(f)
                continue
            if head in _own and not sub:
                continue          # a FILE named bin or payload is neither
            if head == "bin":
                # bin/ goes to bin/ verbatim.
                if not _is_build_detritus(PurePosixPath(f.rel)):
                    data = tree.read(f)
                    if data is not None:
                        binaries[sub] = data
            elif head == "payload":
                # payload/ goes to payload/ verbatim if present. Skip the
                # .keep placeholder used to commit empty dirs.
                if sub != ".keep" and not _is_build_detritus(PurePosixPath(f.rel)):
                    data = tree.read(f)
                    if data is not None:
                        payload[sub] = data
            elif head != "app.yaml":
                # Root-level files + dirs (other than app.yaml/bin/payload)
                # go to payload/. Covers icons, HTML, CSS, JS for the static
                # apps that use the legacy flat layout AND for sidecar apps
                # that keep their icons at the bundle root.
                data = tree.read(f)
                if data is not None:
                    top[f.rel] = data
    if refused:
        raise BundleError(
            f"{len(refused)} link{'' if len(refused) == 1 else 's'} in {source_dir.name} "
            f"cannot go into the bundle: {describe_refused(refused)}. Remove each one, or "
            "replace it with what it points to, inside the folder, then pack again.")
    if manifest_yaml is None:
        raise BundleError(f"no app.yaml in {source_dir}")
    # A root-level file wins over payload/'s file of the same name, as it
    # always has.
    payload.update(top)
    return _build_zip(manifest_yaml, payload, binaries, signature=None)


def sign(unsigned_bundle: bytes, private_key_pem: bytes) -> bytes:
    """Sign an unsigned bundle. Returns a new bundle bytes with
    signature.bin embedded."""
    contents = _read_zip(unsigned_bundle)
    if contents.signature is not None:
        # Strip any existing sig; we'll re-sign.
        pass

    priv = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(priv, Ed25519PrivateKey):
        raise BundleError("private key is not Ed25519")

    digest = _canonical_hash(
        contents.manifest_yaml, contents.payload, contents.binaries
    )
    signature = priv.sign(digest)

    return _build_zip(
        contents.manifest_yaml, contents.payload, contents.binaries,
        signature=signature,
    )


def verify(
    bundle_bytes: bytes,
    *,
    public_key_raw: bytes,
) -> BundleContents:
    """Verify a signed bundle against a single Ed25519 public key.

    Returns the parsed contents on success. Raises BundleError on
    any failure: malformed zip, missing files, missing sig, bad sig.
    """
    contents = _read_zip(bundle_bytes)
    if contents.signature is None:
        raise BundleError("bundle is unsigned")
    if len(contents.signature) != 64:
        raise BundleError(f"signature wrong length: {len(contents.signature)}")
    if len(public_key_raw) != 32:
        raise BundleError(f"public key wrong length: {len(public_key_raw)}")

    pub = Ed25519PublicKey.from_public_bytes(public_key_raw)
    digest = _canonical_hash(
        contents.manifest_yaml, contents.payload, contents.binaries
    )
    try:
        pub.verify(contents.signature, digest)
    except InvalidSignature:
        raise BundleError("signature does not verify against given key") from None
    return contents


def verify_any(
    bundle_bytes: bytes,
    *,
    trusted_keys: list[bytes],
) -> tuple[BundleContents, bytes]:
    """Try every trusted public key. Returns (contents, key_that_matched).
    Raises BundleError if none match.
    """
    contents = _read_zip(bundle_bytes)
    if contents.signature is None:
        raise BundleError("bundle is unsigned")

    digest = _canonical_hash(
        contents.manifest_yaml, contents.payload, contents.binaries
    )
    for raw in trusted_keys:
        if len(raw) != 32:
            continue
        pub = Ed25519PublicKey.from_public_bytes(raw)
        try:
            pub.verify(contents.signature, digest)
            return contents, raw
        except InvalidSignature:
            continue
    raise BundleError("signature does not verify against any trusted key")


def extract_payload(contents: BundleContents, dest_dir: Path) -> None:
    """Write the manifest + payload + bin trees to dest_dir.

    Layout in dest_dir:
        app.yaml              (manifest)
        <payload files>       (extracted to dest_dir/<rel>)
        bin/<bin files>       (sidecar executables; chmod +x)

    Payload files land at the root of dest_dir to keep the legacy
    static-app URL space stable (/system-apps/<id>/index.html).
    Binaries always go under bin/ since they're never web-served.
    """
    import stat

    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "app.yaml").write_bytes(contents.manifest_yaml)

    dest_root = dest_dir.resolve()
    for rel, data in contents.payload.items():
        target = dest_dir / rel
        # Defense-in-depth against zip-slip (primary guard is _safe_rel in
        # _read_zip): never write outside the per-app install dir.
        resolved = target.resolve()
        if resolved != dest_root and dest_root not in resolved.parents:
            raise BundleError(f"payload path escapes install dir: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    if contents.binaries:
        bin_root = dest_dir / "bin"
        bin_root.mkdir(parents=True, exist_ok=True)
        for rel, data in contents.binaries.items():
            target = bin_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            # Set execute bit on the owner so the supervisor can spawn
            # bin/* binaries. Group/other don't get exec — file system
            # privileges still gate; we just need a runnable mode.
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IWUSR)


def fingerprint(public_key_raw: bytes) -> str:
    """Stable identifier for a public key: hex SHA-256 of the raw bytes."""
    return hashlib.sha256(public_key_raw).hexdigest()


def _build_zip(
    manifest_yaml: bytes,
    payload: dict[str, bytes],
    binaries: dict[str, bytes] | None = None,
    *,
    signature: bytes | None,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_FILENAME, manifest_yaml)
        for rel in sorted(payload):
            zf.writestr(f"{PAYLOAD_DIR}/{rel}", payload[rel])
        for rel in sorted(binaries or {}):
            zf.writestr(f"{BIN_DIR}/{rel}", binaries[rel])
        if signature is not None:
            zf.writestr(SIGNATURE_FILENAME, signature)
    return buf.getvalue()
