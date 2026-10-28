#!/usr/bin/env python3
# gate-scope: whole-tree — every system app with a store bundle, against its version in history
"""Store-lane version ratchet — changed payload must carry a version bump.

THE DEFECT (measured).

A store-lane app reaches entities as factory-bundles/<id>.cseries through the
central app store. The only thing that makes an installed copy move is
server/applications/default_install.py:210:

    if not target_version or _version_tuple(target_version) <= _version_tuple(local_app.version):
        continue

STRICTLY newer, and deliberately so — the comment above it records the
incident where a bare `!=` let a stale store row DOWNGRADE a freshly
image-installed app on every boot (sites 0.5.3 -> 0.5.2, skills 0.1.2 -> 0.1.1).

The consequence nothing was checking: **edit a store-lane app without bumping
its version and the change can never reach any existing install.** Not on a
boot, not on an image roll, not on a store sync. Forever.

And it is invisible to every gate that existed:
  - test_bundle_freshness.py compares SOURCE to BUNDLE. Repack and it is green.
  - the release's parity table compares SOURCE VERSION to STORE
    VERSION. Both read the same stale number, so it reports "in sync".
The byte-for-byte fingerprint in installer.py:625-641 that saves the native lane
does NOT apply here: it runs only for IMAGE-baked bundles, and
default_install.py:198 skips NATIVE_APPS outright. Store-lane apps touch
neither path.

MEASURED when this gate was written — every store-lane app with a live store
row was drifted:
    app-store        1.1.2   app.js +133/-68, index.html, style.css
    publisher        0.3.2   app.js, index.html, style.css
    training-memory  0.2.2   app.yaml, index.html, style.css
    backups          3.0.2   index.html, style.css
    browser          0.1.2   4 commits incl. "the viewport never connected"
That last one is what exposed it: its store row had served the bundle it was
first published with ever since.

The pattern is old and the previous fix was always to EVACUATE the app to the
image-baked lane rather than fix the lane — see the comments in
scripts/rebuild_factory_bundles.sh for sites, artifacts, designs
and compute, each moved after this same failure.

THE RULE. For every store-lane app, find the commit that last changed its
`version:` line, then diff its directory from there to HEAD. Any changed file
means payload moved without a version bump, so the change is unshippable.

Fix = bump the version in system-apps/<id>/app.yaml, repack with
scripts/rebuild_factory_bundles.sh, and publish with
scripts/app_store/publish.py. Merging alone publishes nothing.

Pure git — no network, no DB, no container.
"""
import os
import re
import subprocess
import sys

FAILED = 0


def git(root: str, *args: str) -> str:
    return subprocess.run(("git", *args), cwd=root, capture_output=True,
                          text=True).stdout.strip()


def native_apps(root: str) -> set[str]:
    """The image-baked set, read from scripts/image_baked_apps.txt — the one
    source the packer and the smoke gate also read.

    LOUD on a missing/empty manifest instead of returning set(): the old
    bash-array parse returned empty on any drift in the packer's syntax,
    which made this gate treat all 19 image-baked apps as store-lane and
    false-FAIL them with misleading bump advice. An unreadable manifest is a
    broken tree, not a quiet default.
    """
    path = os.path.join(root, "scripts", "image_baked_apps.txt")
    try:
        with open(path, encoding="utf-8") as f:
            ids = {ln.strip() for ln in f
                   if ln.strip() and not ln.lstrip().startswith("#")}
    except OSError as e:
        raise SystemExit(f"FATAL: cannot read image-baked manifest {path}: {e}")
    if not ids:
        raise SystemExit(f"FATAL: image-baked manifest {path} is empty — "
                         "refusing to run the store-lane gate against a tree "
                         "that claims zero image-baked apps")
    return ids


def version_of(path: str) -> str | None:
    try:
        src = open(path, encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r'^version:\s*"?([^"\s]+)"?\s*$', src, re.M)
    return m.group(1) if m else None


def version_tuple(v) -> tuple:
    """Dotted-numeric version key, mirroring server/applications/default_install.py
    :173-182 — the code that actually decides whether an install moves. Non-numeric
    parts compare as 0 there, so a malformed version can never look newer than a
    real one, and this gate must agree with it or it would pass versions the
    installer will silently refuse."""
    parts = []
    for p in str(v or "").strip().split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def version_history(root: str, rel_yaml: str) -> list:
    """Every distinct version this app.yaml has carried in committed history,
    oldest first. Used to catch a BACKWARD move, which the drift check alone
    cannot see: once a lower version is committed, the pickaxe finds that very
    commit, the directory diff from there to HEAD is empty, and the gate goes
    green while the app is permanently frozen on every existing install."""
    out = []
    for commit in reversed(git(root, "log", "--format=%H", "--", rel_yaml).splitlines()):
        commit = commit.strip()
        if not commit:
            continue
        m = re.search(r'^version:\s*"?([^"\s]+)"?\s*$',
                      git(root, "show", f"{commit}:{rel_yaml}"), re.M)
        if m and (not out or out[-1][1] != m.group(1)):
            out.append((commit, m.group(1)))
    return out


def main() -> int:
    global FAILED
    root = os.environ.get("APP_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))

    if not git(root, "rev-parse", "--git-dir"):
        print("FAIL: not a git repository — this gate reads history and cannot")
        print("      run against an exported tree. Run it in the source repo.")
        return 1

    native = native_apps(root)
    sysapps = os.path.join(root, "system-apps")
    drifted: list[tuple[str, str, str, list[str]]] = []
    checked = 0

    for app_id in sorted(os.listdir(sysapps)):
        # `_template` / `_harness_runtime` are scaffolding, not shippable apps.
        if app_id.startswith("_") or app_id in native:
            continue
        yaml_path = os.path.join(sysapps, app_id, "app.yaml")
        if not os.path.exists(yaml_path):
            continue
        # Only apps actually packaged for the store lane. One without a bundle
        # is not delivered this way, so a version bump would mean nothing.
        if not os.path.exists(os.path.join(root, "factory-bundles", f"{app_id}.cseries")):
            continue

        version = version_of(yaml_path)
        if not version:
            print(f"  FAIL  {app_id}: no version: field in app.yaml")
            FAILED += 1
            continue

        checked += 1
        rel_yaml = f"system-apps/{app_id}/app.yaml"
        rel_dir = f"system-apps/{app_id}/"

        # DIRECTION FIRST. A version that moves BACKWARD is worse than one that
        # does not move: default_install.py:279 is strictly-newer, so a lower
        # number is skipped on every sync forever and reaches no existing
        # install — and the drift check below cannot see it, because once the
        # lower value is committed the pickaxe finds that commit and the
        # directory diff from there to HEAD is empty. Silent green, dead app.
        prior = [v for _, v in version_history(root, rel_yaml)
                 if version_tuple(v) > version_tuple(version)]
        if prior:
            highest = max(prior, key=version_tuple)
            print(f"  FAIL  {app_id}: version {version} is LOWER than {highest}, "
                  f"which this app.yaml already carried. default_install.py "
                  f"upgrades on strictly-newer only, so {version} would be "
                  f"skipped forever and reach no existing install.")
            FAILED += 1
            continue
        # The commit that last introduced this exact version string. `-m` is
        # load-bearing: without it the pickaxe never diffs merge commits, so a
        # bump made while resolving a merge conflict (browser 0.2.4, a
        # twin-payload union) is invisible and this gate false-fails on
        # correct, committed work.
        bump = git(root, "log", "-1", "--format=%H", "-m", "-S", f'version: "{version}"',
                   "--", rel_yaml)
        if not bump:
            # The version is not in committed history. Two very different
            # causes, and conflating them makes this gate false-fail on
            # correct work — which it did on its own first real use, when the
            # six bumps it had just demanded were still in the working tree.
            #   - bumped in the working tree, not yet committed: the fix is in
            #     progress and correct. Pass it, and say which way it moved.
            #   - HEAD carries the same value and it is still unfindable:
            #     genuinely odd (squashed or rewritten history). Say so.
            head_yaml = git(root, "show", f"HEAD:{rel_yaml}")
            m = re.search(r'^version:\s*"?([^"\s]+)"?\s*$', head_yaml, re.M)
            committed = m.group(1) if m else None
            if committed and version_tuple(version) > version_tuple(committed):
                print(f"  ok    {app_id}: {committed} -> {version} bumped in the "
                      f"working tree (not yet committed)")
                continue
            if committed and committed != version:
                # Was `committed != version` — an INEQUALITY, not a direction, so
                # a downgrade in the working tree printed "ok ... bumped". The
                # backward case is caught above once committed; this catches it
                # before the commit, which is where it is still cheap to fix.
                print(f"  FAIL  {app_id}: {committed} -> {version} in the working "
                      f"tree moves BACKWARD. default_install.py upgrades on "
                      f"strictly-newer only, so this would never reach an "
                      f"existing install.")
                FAILED += 1
                continue
            print(f"  FAIL  {app_id}: version {version} appears nowhere in the "
                  f"history of {rel_yaml} and HEAD carries the same value — "
                  f"check by hand rather than trusting this gate.")
            FAILED += 1
            continue

        changed = [f for f in git(root, "diff", "--name-only", f"{bump}..HEAD",
                                  "--", rel_dir).splitlines() if f.strip()]
        if changed:
            when = git(root, "log", "-1", "--format=%ad", "--date=short", bump)
            drifted.append((app_id, version, when, changed))

    print(f"store-lane version ratchet — {checked} store-lane apps checked")

    for app_id, version, when, changed in drifted:
        FAILED += 1
        print(f"  FAIL  {app_id} v{version} (bumped {when}) — "
              f"{len(changed)} file(s) changed since, with no version bump:")
        for f in changed:
            print(f"          {f}")

    if drifted:
        print()
        print("  These changes CANNOT reach any existing install. The store sync")
        print("  upgrades on a strictly-newer version only (default_install.py:210),")
        print("  so an unchanged version is skipped forever — repacking the bundle")
        print("  does not help, and the step 7f parity table reads 'in sync'")
        print("  because both sides show the same stale number.")
        print()
        print("  Fix, per app: bump version: in system-apps/<id>/app.yaml, then")
        print("    bash scripts/rebuild_factory_bundles.sh")
        print("    publish the app to the store         (merging")
        print("                                    alone publishes nothing)")

    if not FAILED:
        print("  ok    every store-lane app's payload is covered by its version")

    print(f"\n{'PASS' if not FAILED else str(FAILED) + ' FAILED'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
