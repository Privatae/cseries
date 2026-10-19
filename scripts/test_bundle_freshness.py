#!/usr/bin/env python3
"""
Bundle freshness ratchet. The .cseries bundles baked into the image are what
entities actually INSTALL — system-apps/ source that never got re-packed
ships nothing. This has silently eaten UI work twice (the second time, a day of
workbench briefing/UI ships stayed at 0.4.2 on the fleet while the source said
0.6.1 — the host kept reading the old briefing and granting a build tool to
agent dispatches).

Gate: for every native-bundles/<id>.cseries, the bundle's manifest version
must EQUAL system-apps/<id>/app.yaml's version. Run it before docker build;
red means run scripts/rebuild_factory_bundles.sh on the build host (needs
the signing key) and commit the regenerated bundles.
"""

import io
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_VER = re.compile(r'^version:\s*"?([^"\s]+)"?\s*$', re.M)


def manifest_version(text: str) -> str | None:
    m = _VER.search(text)
    return m.group(1) if m else None


def bundle_manifest(path: Path) -> str | None:
    """A .cseries is a zip carrying manifest.yaml + payload/ (see
    server/applications/bundles.py). Read just the manifest text;
    unreadable bundle -> None (reported)."""
    try:
        with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as z:
            # bundles.py MANIFEST_FILENAME — the manifest is "manifest.yaml"
            # at the bundle root (payload/ carries the app files).
            return z.read("manifest.yaml").decode("utf-8", "replace")
    except Exception:
        return None


def check_bundle(bundle: Path, bad: list) -> bool:
    """Version + payload freshness for ONE bundle. Appends to `bad`; returns
    True when the bundle was checkable (counted), False when it was skipped."""
    app_id = bundle.stem
    src = ROOT / "system-apps" / app_id / "app.yaml"
    if not src.exists():
        bad.append(f"{bundle.name}: no system-apps/{app_id}/app.yaml source")
        return False
    src_ver = manifest_version(src.read_text())
    mtext = bundle_manifest(bundle)
    if mtext is None:
        bad.append(f"{bundle.name}: bundle manifest unreadable")
        return False
    b_ver = manifest_version(mtext)
    if src_ver != b_ver:
        bad.append(f"{bundle.name}: bundle has {b_ver}, source says {src_ver} "
                   f"— re-pack with scripts/rebuild_factory_bundles.sh")
        return False
    # Version equality is too coarse (installer.py says the same — a
    # source change without a version bump once silently shipped
    # nothing): compare payload bytes to the system-apps source.
    try:
        with zipfile.ZipFile(io.BytesIO(bundle.read_bytes())) as z:
            # A bundle's app files usually sit under payload/, but a HARNESS
            # bundle also packs its sidecar at the bundle ROOT (claude-code
            # carries bin/main.py there). Counting
            # only payload/ reported those as never packed — a false stale that
            # stayed invisible while this gate ran over native-bundles alone,
            # because claude-code is factory-only.
            def _rel_of(name: str) -> str | None:
                if name.endswith("/") or name in ("manifest.yaml", "signature.bin"):
                    return None
                return name[len("payload/"):] if name.startswith("payload/") else name

            stale_files = []
            for name in z.namelist():
                rel = _rel_of(name)
                if rel is None:
                    continue
                srcf = ROOT / "system-apps" / app_id / rel
                if not srcf.exists() or srcf.read_bytes() != z.read(name):
                    stale_files.append(rel)
            # Build detritus is not source: __pycache__/*.pyc is gitignored, is
            # not packed (bundles._is_build_detritus), and appears in a tree
            # only because something imported the file locally. Counting it made
            # this gate red-fail a ship on a stray .pyc dated a week earlier
            # — a gate that fails on detritus trains
            # people to ignore it.
            src_files = {
                str(f.relative_to(ROOT / "system-apps" / app_id))
                for f in (ROOT / "system-apps" / app_id).rglob("*")
                if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc"
            } - {"app.yaml", ".web-src-rev"}  # stamp = build metadata, pack() skips dotfiles
            packed = {r for r in (_rel_of(n) for n in z.namelist()) if r}
            missing = sorted(src_files - packed)[:5]
            if stale_files or missing:
                what = ", ".join((stale_files[:5] + [f"(unpacked: {m})" for m in missing])[:6])
                bad.append(f"{bundle.name}: payload differs from source ({what}) "
                           f"— re-pack with scripts/rebuild_factory_bundles.sh")
    except Exception as e:
        bad.append(f"{bundle.name}: payload check failed: {e}")

    return True


import hashlib

# Repo files OUTSIDE src/ and app-web-kit/ that an app's build reads, so an edit
# to one changes its output with no src/ byte moving. Keyed "../" + the
# repo-relative path in the hash (cannot collide with src/ or ../app-web-kit/).
# app-web-kit/place.mjs keeps the identical table (EXTRA_INPUTS).
WEB_EXTRA_INPUTS = {
    # circles-web and workbench-web src/lib/secret-shapes.ts import the pod-side
    # rules file at build time.
    "circles": ["brain/mastercode/secret_shapes.json"],
    "workbench": ["brain/mastercode/secret_shapes.json"],
}


def web_src_rev(root: Path, web_dir: str, app_id: str) -> str:
    """The web-source freshness stamp for <web_dir>: sha256 over sorted
    relpath + NUL + bytes + NUL for src/**, the shared kit (../app-web-kit/),
    the app's extra build inputs (../<repo path>) and package-lock.json.
    Must equal what app-web-kit/place.mjs stamps (`--print-rev` prints it).

    The `dist`/`node_modules` skip is tested on the path RELATIVE to the tree
    being walked: testing the absolute path would silently drop every file from
    the hash for a checkout living under a directory named `dist`."""
    def _keep(p, base):
        return p.is_file() and not {"node_modules", "dist"} & set(p.relative_to(base).parts)

    web = root / web_dir
    kit = root / "app-web-kit"
    entries = [(str(p.relative_to(web)).replace("\\", "/"), p)
               for p in (web / "src").rglob("*") if _keep(p, web)]
    entries.append(("package-lock.json", web / "package-lock.json"))
    entries += [("../app-web-kit/" + str(p.relative_to(kit)).replace("\\", "/"), p)
                for p in kit.rglob("*") if _keep(p, kit)] if kit.exists() else []
    entries += [("../" + rel, root / rel) for rel in WEB_EXTRA_INPUTS.get(app_id, [])]
    entries.sort(key=lambda t: t[0])
    h = hashlib.sha256()
    for rel, p in entries:
        h.update(rel.encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def main() -> int:
    bad: list = []
    checked = 0
    # BOTH bundle trees. native-bundles/ is image-baked; factory-bundles/ is
    # what the store publishes from — and while only the first was
    # gated, four factory bundles sat version-stale in the repo with nothing
    # to catch it: circles 0.8.7 vs 0.8.8, compute 0.1.0 vs 0.2.0,
    # getting-started 0.1.0 vs 0.1.1, vscode 0.1.1 vs 0.2.0. Same failure this
    # gate was written for, one shelf across.
    for tree in ("native-bundles", "factory-bundles"):
        for bundle in sorted((ROOT / tree).glob("*.cseries")):
            if check_bundle(bundle, bad):
                checked += 1

    # Web-source freshness: the React apps build OUTSIDE their
    # system-apps dir (circles-web/, workbench-web/) — the payload check above
    # can't see src edits that never ran `npm run ship`. place.mjs stamps
    # system-apps/<id>/.web-src-rev = sha256 over sorted-relpath+NUL+bytes+NUL
    # of src/** + package-lock.json; recompute and compare. This is the gate
    # an earlier release needed: backend rolled, chat UI stayed a week old, and the user
    # saw "literally no change".
    # THE SHARED KIT IS IN THE HASH. app-web-kit/ is compiled into
    # all four apps via the @kit alias, but it lives at the repo ROOT, inside no
    # app's src/. Hashing src/ alone meant editing app-web-kit/ui/button.tsx
    # changed every app's shipped output while no stamp moved and this gate
    # stayed green — the exact failure above, reintroduced for the
    # most-shared code in the tree, and introduced by the extraction that
    # created it. Its key is prefixed `../app-web-kit/` so it cannot collide
    # with a src/ path, and place.mjs's srcRev() walks the identical set with
    # the identical prefix. The two implementations are load-bearing on each
    # other: if they ever disagree the stamp never matches and this gate goes
    # red, which is the safe direction.
    #
    # ALL FOUR APPS ARE CHECKED. It was circles + workbench only, because those
    # were the two whose place.mjs carried the stamp. The shared script stamps
    # every app that uses it, so community and sites are now covered too —
    # before this they were built by the same script, stamped, and never
    # verified.
    for web_dir, app_id in (("circles-web", "circles"), ("workbench-web", "workbench"),
                            ("community-web", "community"), ("sites-web", "sites")):
        web = ROOT / web_dir
        stamp_f = ROOT / "system-apps" / app_id / ".web-src-rev"
        if not web.exists():
            continue
        if not stamp_f.exists():
            bad.append(f"{app_id}: no .web-src-rev stamp — run `npm run ship` in {web_dir}/")
            continue
        if web_src_rev(ROOT, web_dir, app_id) != stamp_f.read_text().strip():
            # Name BOTH inputs. The hash now covers app-web-kit/ as well, so a
            # single kit edit reds every app at once — and a message naming only
            # <app>-web/src sends the reader to a tree that did not change.
            bad.append(f"{app_id}: {web_dir}/src or app-web-kit/ changed since the last "
                       f"`npm run ship` in {web_dir}/ — rebuild + re-place + re-pack, "
                       f"or the image ships the OLD UI. (A kit edit reds all four apps: "
                       f"re-ship each one.)")
    if bad:
        print(f"FAIL: {len(bad)} stale/broken bundle(s):")
        for b in bad:
            print("  " + b)
        return 1
    print(f"OK — {checked} bundles (native + factory) match their system-apps source")
    return 0


if __name__ == "__main__":
    sys.exit(main())
