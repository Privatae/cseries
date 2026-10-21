#!/usr/bin/env python3
"""Build the free public App Store's catalog from signed bundles.

  python3 scripts/build_store_catalog.py --version X.Y.Z --out <dir>
      [--bundles factory-bundles] [--download-base <URL>] [--images <json>]
      [--source-base <URL>] [--epoch SECONDS] [--issued-at SECONDS]

Writes <dir>/catalog.json and copies every listed bundle to
<dir>/<app_id>-<version>.cseries. A Host reads the catalog through
server/applications/public_store.py, which checks its signature, each
bundle's sha256 against it, and then (the installer) each bundle's own
signature.

WHICH APPS. The store lane: every bundle in --bundles whose app is not baked
into the Host image (scripts/image_baked_apps.txt: the image lays those down
itself), not retired (kernel.RETIRED_APPS: the installer refuses them) and not
a platform tool (platform_tools.PLATFORM_TOOLS: no C-Series Host may install
one). Each bundle must verify against the C-Series apps key (and declare
native or standard provenance) or the community key (and declare community),
the same keys and the same provenance rule the installer applies; anything
else stops the build, so a catalog never lists a bundle a Host would refuse.

THE BYTES. The same inputs give the same catalog, byte for byte: entries in
app id order, keys sorted, generated_at from --epoch, else SOURCE_DATE_EPOCH,
else the commit time of HEAD. Nothing is signed here; the maintainer signs the
catalog with the release key (docs/RELEASING.md), and the command for that is
printed at the end.

ISSUED_AT. The time the catalog was issued, which the signature then covers: a
Host shows a notice when the catalog it reads is older than 45 days
(public_store.CATALOG_MAX_AGE_S), since anyone serving the catalog URL could keep
serving an old one. It comes from --issued-at (Unix seconds), else the same
time as generated_at, so a re-run of the release workflow builds the same bytes
(the workflow never replaces an asset already on the release, and checks so).

--download-base makes each bundle's URL absolute (<base>/<file>); without it
the URLs are relative to the catalog, for a catalog served beside its
bundles. --images is a JSON object mapping a runtime family (python,
creative, helper, browser) to its image pinned by digest; each entry lists the
families its manifest uses, for the reader to see what an app will pull.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from server.applications import bundles  # noqa: E402

KIND = "cseries-store"
REPO_LICENSE = "AGPL-3.0-only"
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
FAMILIES = ("python", "creative", "helper", "browser")
SUMMARY_MAX = 1000


class BuildError(Exception):
    pass


def baked_ids(path: Path) -> set[str]:
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def excluded_ids(baked_file: Path) -> dict[str, str]:
    """{app id: why it is not in the store lane}."""
    from server.applications.kernel import RETIRED_APPS
    from server.applications.platform_tools import PLATFORM_TOOLS
    why = {i: "baked into the Host image" for i in baked_ids(baked_file)}
    why.update({i: "retired" for i in RETIRED_APPS})
    why.update({i: "a platform tool" for i in PLATFORM_TOOLS})
    return why


def families_of(m: dict) -> list[str]:
    """The runtime image families an app's manifest uses."""
    rt = m.get("runtime") if isinstance(m.get("runtime"), dict) else {}
    out = set()
    if rt.get("image") in ("python", "creative"):
        out.add(rt["image"])
    if rt.get("kind") == "helper":
        out.add("helper")
    if rt.get("harness") == "browser":
        out.add("browser")
    return sorted(out)


def licence_of(m: dict) -> str:
    """The app's licence: an SPDX id the manifest states, else its upstream's,
    else this repository's (a first-party app is part of it)."""
    lic = m.get("license")
    if isinstance(lic, str) and lic.strip():
        return lic.strip()
    if isinstance(lic, dict):
        for k in ("spdx", "id"):
            if isinstance(lic.get(k), str) and lic[k].strip():
                return lic[k].strip()
    src = m.get("source") if isinstance(m.get("source"), dict) else {}
    up = src.get("upstream") if isinstance(src.get("upstream"), dict) else {}
    if isinstance(up.get("license"), str) and up["license"].strip():
        return up["license"].strip()
    return REPO_LICENSE


def verify_bundle(data: bytes, cseries_key: bytes, community_key: bytes) -> tuple[dict, str]:
    """(manifest, signer) for a bundle whose signature and declared provenance
    the installer would accept; BuildError otherwise."""
    try:
        contents, key = bundles.verify_any(data, trusted_keys=[cseries_key, community_key])
    except bundles.BundleError as e:
        raise BuildError(f"its signature does not verify against the C-Series apps key or "
                         f"the community key ({e})") from None
    m = yaml.safe_load(contents.manifest_yaml) or {}
    if not isinstance(m, dict) or not isinstance(m.get("id"), str):
        raise BuildError("its manifest names no id")
    prov = str(m.get("provenance") or m.get("tier") or "standard")
    if key == cseries_key:
        if prov not in ("native", "standard"):
            raise BuildError(f"it is signed with the C-Series apps key but declares "
                             f"provenance {prov!r}")
        return m, "C-Series"
    if prov != "community":
        raise BuildError(f"it is signed with the community key but declares provenance {prov!r}")
    return m, "community"


def build(version: str, bundles_dir: Path, out: Path, *, download_base: str | None = None,
          images: dict | None = None, source_base: str | None = None, epoch: int = 0,
          issued_at: int | None = None,
          cseries_key: bytes, community_key: bytes, baked_file: Path,
          log=print) -> tuple[bytes, list[dict]]:
    """Write the catalog and the bundles into out. Returns (catalog bytes,
    entries)."""
    if not VERSION_RE.match(version):
        raise BuildError(f"--version must be X.Y.Z, not {version!r}")
    images = images or {}
    for fam, ref in images.items():
        if fam not in FAMILIES or not isinstance(ref, str) or not IMAGE_RE.match(ref):
            raise BuildError(f"--images: {fam} must be one of {', '.join(FAMILIES)}, "
                             "pinned by digest")
    skip = excluded_ids(baked_file)
    entries: dict[str, dict] = {}
    files: dict[str, bytes] = {}
    for path in sorted(bundles_dir.glob("*.cseries")):
        data = path.read_bytes()
        try:
            m, signer = verify_bundle(data, cseries_key, community_key)
        except BuildError as e:
            raise BuildError(f"{path.name}: {e}") from None
        app_id = m["id"]
        if app_id in skip:
            log(f"  skip {app_id}: {skip[app_id]}")
            continue
        if app_id in entries:
            raise BuildError(f"{path.name}: {app_id} is in two bundles")
        v = str(m.get("version") or "")
        if not v:
            raise BuildError(f"{path.name}: its manifest names no version")
        name = f"{app_id}-{v}.cseries"
        url = f"{download_base.rstrip('/')}/{name}" if download_base else name
        fams = families_of(m)
        missing = [f for f in fams if f not in images]
        if missing and images:
            log(f"  note {app_id}: no image given for {', '.join(missing)}")
        source = ""
        if signer == "C-Series" and source_base and (ROOT / "system-apps" / app_id).is_dir():
            source = f"{source_base.rstrip('/')}/system-apps/{app_id}"
        elif signer == "community":
            src = m.get("source") if isinstance(m.get("source"), dict) else {}
            up = src.get("upstream") if isinstance(src.get("upstream"), dict) else {}
            if isinstance(up.get("url"), str) and up["url"].startswith("https://"):
                source = up["url"]
        summary = str(m.get("description") or "").strip()
        if len(summary) > SUMMARY_MAX:
            summary = summary[:SUMMARY_MAX - 1].rstrip() + "…"
        entries[app_id] = {
            "app_id": app_id,
            "name": str(m.get("name") or app_id),
            "summary": summary,
            "publisher": signer if signer == "C-Series" else str(m.get("author") or "Community"),
            "version": v,
            "provenance": str(m.get("provenance") or "standard"),
            "is_default": False,
            "price_micros": 0,
            "bundle": {"url": url, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)},
            "images": {f: images[f] for f in fams if f in images},
            "license": licence_of(m),
            "source": source,
        }
        files[name] = data
        log(f"  {app_id} {v} ({len(data)} bytes)")
    issued = epoch if issued_at is None else issued_at
    doc = {
        "version": version,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
        "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(issued)),
        "apps": [entries[k] for k in sorted(entries)],
    }
    body = (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out / name).write_bytes(data)
    (out / "catalog.json").write_bytes(body)
    return body, doc["apps"]


def default_epoch() -> int:
    env = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if env.isdigit():
        return int(env)
    try:
        return int(subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%ct"],
                                  capture_output=True, text=True, check=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="build_store_catalog.py")
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--bundles", default=str(ROOT / "factory-bundles"))
    ap.add_argument("--download-base")
    ap.add_argument("--images", help="JSON file: {family: ref@sha256:...}")
    ap.add_argument("--source-base")
    ap.add_argument("--epoch", type=int)
    ap.add_argument("--issued-at", type=int, help="Unix seconds; default: the --epoch time")
    ap.add_argument("--cseries-key", default=str(ROOT / "etc" / "cseries-apps.pub.raw"))
    ap.add_argument("--community-key", default=str(ROOT / "etc" / "community-apps.pub.raw"))
    ap.add_argument("--baked", default=str(ROOT / "scripts" / "image_baked_apps.txt"))
    ap.add_argument("--tag", help="the release tag, for the printed upload command")
    a = ap.parse_args(argv)
    images = {}
    if a.images:
        with open(a.images, encoding="utf-8") as f:
            images = json.load(f)
        if not isinstance(images, dict):
            print("--images must hold one JSON object", file=sys.stderr)
            return 2
    try:
        body, apps = build(a.version, Path(a.bundles), Path(a.out),
                           download_base=a.download_base, images=images,
                           source_base=a.source_base,
                           epoch=a.epoch if a.epoch is not None else default_epoch(),
                           issued_at=a.issued_at,
                           cseries_key=Path(a.cseries_key).read_bytes(),
                           community_key=Path(a.community_key).read_bytes(),
                           baked_file=Path(a.baked))
    except (BuildError, OSError, ValueError) as e:
        print(f"build_store_catalog: {e}", file=sys.stderr)
        return 1
    sha = hashlib.sha256(body).hexdigest()
    tag = a.tag or f"v{a.version}"
    print(f"catalog.json: {len(apps)} apps, sha256 {sha}")
    print("Sign it on the computer that holds the release key, then upload the signature:")
    print(f"  release-sign sign {KIND} {a.version} {sha} > catalog.json.{sha[:16]}.sig")
    print(f"  gh release upload {tag} catalog.json.{sha[:16]}.sig --repo Privatae/cseries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
