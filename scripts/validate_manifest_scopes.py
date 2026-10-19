#!/usr/bin/env python3
"""Ratchet: every permission an app manifest declares must exist in the
server/applications/scopes.py PERMISSIONS catalog.

The trap this closes (it once fired on three releases in a row): a route can gate on a
scope and an app.yaml can declare it while the catalog never learned it —
nothing connects the three at commit time. The install-time validator
(scopes.validate, "Unknown permission(s)") then rejects the bundle at BOOT,
install_native_bundles() logs it as a warning, the previously-installed copy
keeps serving, and the feature silently never lands. circles declared
write:brain_personality (one change added the route AND the app.yaml entry but
not the catalog entry); smoke passed 123/0 on two consecutive ships while
circles failed to install on every boot. Cost: two full wasted ship cycles
before live-verify caught it.

How this checks it: validate the `permissions:` list of every
system-apps/*/app.yaml AND every native-bundles/*.cseries manifest.yaml
against the real catalog (imported, not copied — this can never drift from
what install-time enforcement uses). Bundles are checked too so a stale
bundle carrying a since-removed scope is caught even when its source is
already clean.

Runs in-tree in under a second. Called from scripts/smoke_test.sh (ship
gate) and before an image build (validate-tree — fail here before a
build is spent).

Exit 0 = every declared permission is catalogued. Exit 1 = at least one
manifest declares an unknown permission.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from server.applications.scopes import PERMISSIONS  # noqa: E402


def unknown_perms(manifest: dict) -> list[str]:
    perms = manifest.get("permissions") or []
    return [p for p in perms if p not in PERMISSIONS]


def main() -> int:
    problems: dict[str, list[str]] = {}
    checked = 0

    for app_yaml in sorted(REPO.glob("system-apps/*/app.yaml")):
        checked += 1
        bad = unknown_perms(yaml.safe_load(app_yaml.read_text()))
        if bad:
            problems[str(app_yaml.relative_to(REPO))] = bad

    for bundle in sorted(REPO.glob("native-bundles/*.cseries")):
        checked += 1
        manifest = yaml.safe_load(zipfile.ZipFile(bundle).read("manifest.yaml"))
        bad = unknown_perms(manifest)
        if bad:
            problems[str(bundle.relative_to(REPO))] = bad

    if not checked:
        print("FAIL  found no manifests to check — wrong repo root?", file=sys.stderr)
        return 2

    if not problems:
        print(f"PASSED: all {checked} manifests declare only catalogued permissions")
        return 0

    print(
        f"FAILED: {len(problems)} manifest(s) declare permissions missing from "
        "server/applications/scopes.py PERMISSIONS\n",
        file=sys.stderr,
    )
    for path, bad in problems.items():
        for p in bad:
            print(f"  {path}: unknown permission {p!r}", file=sys.stderr)
    print(
        "\n  An unknown permission does NOT fail the build or the boot — "
        "\n  install_native_bundles() logs a warning and the entity keeps serving"
        "\n  the previously-installed copy, so the app change silently never lands"
        "\n  (as circles/write:brain_personality once did)."
        "\n"
        "\n  Fix: either register the permission in scopes.py PERMISSIONS (a"
        "\n  deliberate decision — routes gate on it) or remove it from the"
        "\n  manifest; then rerun scripts/rebuild_factory_bundles.sh for the"
        "\n  affected app so the bundle matches.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
