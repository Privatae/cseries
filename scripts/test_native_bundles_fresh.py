#!/usr/bin/env python3
"""Ratchet: every image-baked .cseries bundle must match its system-apps/ source.

The trap this closes (fired at least four times):
  editing system-apps/<app>/ does NOT update native-bundles/<app>.cseries.
  The image bakes the BUNDLE, and install_image_bundles() refreshes an
  entity's installed copy from it. So a PR that edits app source without
  re-running scripts/rebuild_factory_bundles.sh ships the new code into the
  image's source path while every existing entity keeps installing the OLD
  signed bundle. The feature looks shipped, gates pass, nothing errors, and
  the UI silently never arrives. Cost so far: a Threads UI, six days of UI
  in one incident, the "sites" strand, and the Settings desktop-layout tab.

Why the pre-existing gates missed it: smoke_test.sh checked that each bundle
EXISTS in the image (`test -f`) and that the baked SET matches NATIVE_APPS.
Both pass happily on a stale bundle. Presence is not freshness.

How this checks it: re-pack each app from source with the real packer and
compare CONTENTS against the committed bundle — manifest.yaml, payload/*,
bin/*. The signature is ignored on purpose: signing is non-deterministic, so
byte-comparing .cseries files reports every bundle as different and teaches
everyone to ignore the gate. Contents are deterministic.

Reuses server.applications.bundles.pack() rather than reimplementing its
file-selection rules (hidden files skipped, .keep skipped, app.yaml ->
manifest.yaml, bin/ verbatim), so this can never drift from the packer.

Runs in-tree, not in-container: the image ships native-bundles/ but NOT
system-apps/, so the comparison is only possible against the source tree.

Exit 0 = every native bundle is fresh. Exit 1 = at least one is stale.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from server.applications.bundles import _read_zip, pack  # noqa: E402

MANIFEST = REPO / "scripts" / "image_baked_apps.txt"


def native_ids() -> list[str]:
    """Read the image-baked set from scripts/image_baked_apps.txt — the one
    source the packer, smoke gate and store-lane ratchet all read."""
    try:
        lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        print(f"FAIL  could not read image-baked manifest {MANIFEST}: {e}",
              file=sys.stderr)
        raise SystemExit(2)
    ids = [ln.strip() for ln in lines
           if ln.strip() and not ln.lstrip().startswith("#")]
    if not ids:
        print(f"FAIL  image-baked manifest {MANIFEST} is empty", file=sys.stderr)
        raise SystemExit(2)
    return ids


def diff_app(app_id: str) -> list[str]:
    """Return a list of human-readable differences; empty means fresh."""
    bundle_p = REPO / "native-bundles" / f"{app_id}.cseries"
    src_p = REPO / "system-apps" / app_id

    if not bundle_p.exists():
        return [f"native-bundles/{app_id}.cseries is MISSING"]
    if not (src_p / "app.yaml").exists():
        return [f"system-apps/{app_id}/app.yaml is MISSING (id in NATIVE_APPS with no source)"]

    have = _read_zip(bundle_p.read_bytes())
    want = _read_zip(pack(src_p))

    diffs: list[str] = []
    if have.manifest_yaml != want.manifest_yaml:
        diffs.append("manifest.yaml differs from system-apps/%s/app.yaml" % app_id)
    for label, h, w in (("payload", have.payload, want.payload),
                        ("bin", have.binaries, want.binaries)):
        for key in sorted(set(h) | set(w)):
            if key not in h:
                diffs.append(f"{label}/{key} is in source but NOT in the bundle")
            elif key not in w:
                diffs.append(f"{label}/{key} is in the bundle but NOT in source")
            elif h[key] != w[key]:
                diffs.append(
                    f"{label}/{key} differs (bundle {len(h[key])}B vs source {len(w[key])}B)"
                )
    return diffs


def main() -> int:
    ids = native_ids()
    stale: dict[str, list[str]] = {}
    for app_id in ids:
        d = diff_app(app_id)
        if d:
            stale[app_id] = d

    if not stale:
        print(f"PASSED: all {len(ids)} image-baked bundles match their system-apps/ source")
        return 0

    print(f"FAILED: {len(stale)} of {len(ids)} image-baked bundles are STALE\n", file=sys.stderr)
    for app_id, diffs in stale.items():
        print(f"  {app_id}:", file=sys.stderr)
        for d in diffs:
            print(f"    - {d}", file=sys.stderr)
    print(
        "\n  A stale bundle means this app's UI change will NOT reach any existing"
        "\n  entity — the image bakes the bundle, and install_image_bundles()"
        "\n  refreshes each entity's installed copy from it. Nothing will error;"
        "\n  the UI just silently never arrives."
        "\n"
        "\n  Fix:"
        "\n    scripts/rebuild_factory_bundles.sh"
        "\n    git checkout -- factory-bundles/          # store-delivered, separate concern"
        "\n    git checkout -- native-bundles/<unchanged>.cseries   # keep ONLY the apps you edited"
        "\n    git add native-bundles/<edited>.cseries     # commit with the source edit",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
