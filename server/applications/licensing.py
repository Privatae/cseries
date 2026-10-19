"""Licence enforcement — the machine half of `license.redistribute`.

Every app carries an author-set
licence (manifest.LicenseSpec, default deny), copied into the installed row at
install (registry.app_license). `redistribute` was once declared,
stored and never read. This module reads it, in the two places the design puts
the machine:

  publish_gate()  the store publish lanes (publish_project_service and the
                  Publisher upload route). A submission that DESCENDS from an
                  installed app someone else authored, whose licence withholds
                  redistribution, is refused unless it is submitted as a
                  declared FLAVOUR of that app (the parent's fork_policy is the
                  operator's check). Rights come from the installed ROW, never
                  from the submission's own manifest.
  write_gate()    the host's own reuse path (workspace_file op=write): a file
                  written into a project whose bytes are a file of such an app
                  is refused, with the same message. Reading is never gated —
                  the user's machine, the user's right; copying INTO a project
                  is where it becomes redistribution.

DESCENT is decided from bytes, not names: the installed app's files are on
this machine (installer.INSTALL_ROOT/<id>), so the submission's file hashes
are compared with each foreign app's. An app descends from another when it
carries at least DESCENT_MIN_SHARED byte-identical files of it AND those are
at least DESCENT_FRACTION of that app's files. Both numbers are tunable policy
(the fraction is the usual "half the work" line used by
code-clone detectors — a couple of shared icons or a vendored library never
clears it, a copied app does). Small files (< SMALL_FILE_BYTES) and the
platform's own bookkeeping (app.yaml, attestation.json, preserved runtime
caches) are not counted on either side.

What this gate does NOT decide, stated plainly like _license_gate_site: a
copy that rewrites every file, or that came from a machine where the app was
never installed, is not stopped HERE — that is what store review and the
lineage corpus (the approved sources in the review repo) are for. This makes
the honest path correct on the installer's own machine; it is not an
anti-copy mechanism.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("privatae.applications.licensing")

DESCENT_MIN_SHARED = 2
DESCENT_FRACTION = 0.5
SMALL_FILE_BYTES = 64
_MAX_FILES_PER_APP = 2000
_MAX_BYTES_PER_APP = 50 * 1024 * 1024
_SKIP_NAMES = frozenset({"app.yaml", "attestation.json", ".cseries-bundle-sha256"})


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _walk_sorted(d: Path):
    """Every path below `d` in exactly sorted(d.rglob("*")) order (paths sort
    by their parts, which is a pre-order walk with each folder's entries
    sorted by name), produced lazily so the caller can stop early. A linked
    folder is not entered, as rglob does not."""
    import os
    try:
        with os.scandir(d) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return
    for e in entries:
        p = Path(e.path)
        yield p
        try:
            is_dir = e.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        if is_dir:
            yield from _walk_sorted(p)


def hash_tree(root: Path, skip_dirs: tuple = ("python-libs",), *,
              unhashed: dict | None = None) -> dict[str, str]:
    """{relative path: sha256} of an installed app's countable files.
    `skip_dirs` defaults to installer._RUNTIME_CACHE_DIRS' value; the caller
    passes the live constant.

    The hashing work per app stays bounded (_MAX_FILES_PER_APP /
    _MAX_BYTES_PER_APP: this runs in the kernel on every store publish and
    project write), and so does the walk: it stops at the first countable file
    past the bound instead of listing the rest. That the
    bound was reached is never dropped silently: `unhashed`, when given,
    receives {"bound_reached": True, "from": <first file not compared>}."""
    out: dict[str, str] = {}
    total = 0
    if not root.is_dir():
        return out
    for p in _walk_sorted(root):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        parts = rel.split("/")
        if parts[0] in skip_dirs or p.name in _SKIP_NAMES or p.name.startswith("."):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size < SMALL_FILE_BYTES:
            continue
        if len(out) >= _MAX_FILES_PER_APP or total + size > _MAX_BYTES_PER_APP:
            if unhashed is not None:
                unhashed.update({"bound_reached": True, "from": rel})
            break
        try:
            out[rel] = _sha(p.read_bytes())
        except OSError:
            continue
        total += size
    return out


def countable_hashes(payload: dict[str, bytes], binaries: dict[str, bytes] | None = None) -> set[str]:
    """The submission side of the comparison, under the same rules."""
    hs: set[str] = set()
    for rel, data in payload.items():
        name = rel.rsplit("/", 1)[-1]
        if name in _SKIP_NAMES or name.startswith(".") or len(data) < SMALL_FILE_BYTES:
            continue
        hs.add(_sha(data))
    for _rel, data in (binaries or {}).items():
        if len(data) >= SMALL_FILE_BYTES:
            hs.add(_sha(data))
    return hs


def descent(sub_hashes: set[str], index: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Which indexed apps the submission descends from. `index` rows are
    {app_id, name, license, authored_here, files: {rel: sha}}. Pure."""
    out = []
    for row in index:
        files = row.get("files") or {}
        if not files:
            continue
        shared = sum(1 for h in files.values() if h in sub_hashes)
        total = len(files)
        if shared >= DESCENT_MIN_SHARED and shared / total >= DESCENT_FRACTION:
            out.append({"app_id": row["app_id"], "name": row.get("name") or row["app_id"],
                        "license": dict(row.get("license") or {}),
                        "first_party": bool(row.get("first_party")),
                        "shared": shared, "total": total})
    return out


FIRST_PARTY_PROVENANCES = frozenset({"native", "standard"})


def drop_shared_assets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A PLATFORM asset — bytes that a first-party app ships AND that appear
    in more than one installed app (the stock icon eleven system apps carry,
    a design-system file) — is nobody's authorship; it is dropped from every
    app's files so it can never count toward descent. Deliberately NOT "any
    bytes two apps share": a flavour is mostly its parent's bytes, and
    dropping those would blank the parent and let a wholesale copy of it
    publish once its flavour is installed too.
    A library two unrelated third-party apps both vendor
    still counts — at one or two files of many it stays under
    DESCENT_FRACTION, which is the design's own argument. Pure."""
    seen: dict[str, int] = {}
    first_party_hashes: set[str] = set()
    for r in rows:
        hs = set((r.get("files") or {}).values())
        for h in hs:
            seen[h] = seen.get(h, 0) + 1
        if r.get("first_party"):
            first_party_hashes |= hs
    platform_assets = {h for h in first_party_hashes if seen.get(h, 0) > 1}
    out = []
    for r in rows:
        files = {rel: h for rel, h in (r.get("files") or {}).items()
                 if h not in platform_assets}
        out.append({**r, "files": files})
    return out


async def foreign_app_index(pool) -> list[dict[str, Any]]:
    """Every installed app this machine did NOT author, with its licence and
    file hashes. Reads the registry rows (rights) and INSTALL_ROOT (bytes).
    Rows carry `first_party` (provenance native/standard — Privatae's own
    apps, installed from the image or the store): they are still not the
    user's to republish, but they are not "someone else's" either, and the
    refusal says so; the write gate does not apply to them (it protects
    other users' authorship; a platform asset in a project is fine, and a
    wholesale copy is caught at publish)."""
    from server.applications import registry
    from server.applications.installer import INSTALL_ROOT, _RUNTIME_CACHE_DIRS
    rows = []
    for app in await registry.list_active(pool):
        if app.authored_here:
            continue
        left: dict = {}
        files = hash_tree(INSTALL_ROOT / app.id, skip_dirs=tuple(_RUNTIME_CACHE_DIRS),
                          unhashed=left)
        if left.get("bound_reached"):
            log.warning("licence index: %s reached the %d-file / %d-byte hashing bound; "
                        "its files from %s on (in path order) are not compared for descent",
                        app.id, _MAX_FILES_PER_APP, _MAX_BYTES_PER_APP, left.get("from"))
        rows.append({"app_id": app.id, "name": app.name, "license": dict(app.app_license or {}),
                     "authored_here": bool(app.authored_here),
                     "first_party": app.provenance in FIRST_PARTY_PROVENANCES,
                     "files": files, "unhashed": left})
    return drop_shared_assets(rows)


def refusal(parent: dict[str, Any]) -> str:
    name = parent.get("name") or parent.get("app_id")
    lic = parent.get("license") or {}
    flavours_open = str(lic.get("flavors") or "open") == "open"
    if parent.get("first_party"):
        return (f"{name!r} is one of Privatae's own apps; "
                f"{parent.get('shared')} of its {parent.get('total')} files are in this "
                f"submission byte-for-byte, and it is not yours to republish as a new "
                f"app. Build your own, or fork it in the Workbench and publish that as a "
                f"flavour if its licence allows flavours.")
    msg = (f"{name!r} is published by someone else, and its licence does not allow "
           f"republishing it (license.redistribute is not granted). "
           f"{parent.get('shared')} of its {parent.get('total')} files are in this "
           f"submission byte-for-byte.")
    if flavours_open:
        msg += (" Its author allows FLAVOURS: fork it in the Workbench and publish "
                "that project — it joins the store as a child of theirs, with "
                "their authorship recorded.")
    else:
        msg += (" Its author allows no published flavours either; ask them to set "
                "license.redistribute or license.flavors in the app.")
    return msg


def gate_from_index(sub_hashes: set[str], index: list[dict[str, Any]],
                    forked_from_app_id: str | None = None) -> list[str]:
    """The publish decision, pure: violations for the descents that are not
    the declared parent and whose licence withholds redistribution."""
    errs = []
    for h in descent(sub_hashes, index):
        if forked_from_app_id and h["app_id"] == forked_from_app_id:
            # The declared parent: admitted here as a flavour. Whether that
            # parent's fork_policy allows flavours is decided by the OPERATOR
            # (at submit and again at approve), and every publish is
            # human-reviewed — so a hand-typed parent buys nothing
            # the parent's author did not grant.
            continue
        if (h.get("license") or {}).get("redistribute") is True:
            continue
        errs.append(refusal(h))
    return errs


async def publish_gate(pool, *, payload: dict[str, bytes],
                       binaries: dict[str, bytes] | None = None,
                       forked_from_app_id: str | None = None) -> list[str]:
    """Violations for a store submission ([] = ok). A declared flavour of the
    very app it descends from is allowed here — whether that parent permits
    flavours is the operator's fork admission."""
    try:
        index = await foreign_app_index(pool)
    except Exception as e:  # a registry read that fails must not open the gate
        log.warning("licence gate: registry/install-dir read failed: %s", e)
        return [f"cannot check licences right now ({type(e).__name__}) — publish refused "
                f"rather than guessed"]
    return gate_from_index(countable_hashes(payload, binaries), index, forked_from_app_id)


def write_refusal(content_hash: str, index: list[dict[str, Any]]) -> str | None:
    for row in index:
        if (row.get("license") or {}).get("redistribute") is True:
            continue
        if row.get("first_party"):
            # A platform asset in a user's project is fine (the gate protects
            # other users' authorship); a wholesale copy is caught at publish.
            continue
        for rel, fh in (row.get("files") or {}).items():
            if fh == content_hash:
                return (f"this file is {rel!r} from {row.get('name') or row['app_id']!r}, "
                        f"published by someone else, and its licence does not allow "
                        f"copying it into a project for republishing "
                        f"(license.redistribute is not granted). Write your own, or "
                        f"fork the app as a flavour if its author allows that.")
    return None


async def write_gate(pool, content: bytes) -> str | None:
    """The reuse gate for one file being written into a project: refuse when
    its bytes are a file of a foreign app whose licence withholds
    redistribution. None = allowed. Tiny files are never a copy."""
    if len(content) < SMALL_FILE_BYTES:
        return None
    try:
        index = await foreign_app_index(pool)
    except Exception as e:
        log.warning("licence write gate: index read failed (allowing): %s", e)
        return None
    return write_refusal(_sha(content), index)
