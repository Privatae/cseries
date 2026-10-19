#!/usr/bin/env python3
"""Typecheck the four React apps. Nothing else in this repo does.

WHY THIS EXISTS. The app-web-kit extraction moved
the shared client core out of the four apps and into app-web-kit/. Main had
added `uploadContextFile` to workbench AFTER the branch point, and it called
workbench's local `extractDetail`. The branch deleted that definition; the kit's
copy was module-private. Git auto-merged the two changes with NO CONFLICT, so
nothing surfaced it.

The result shipped:

    workbench-web/src/lib/api.ts:89   const detail = await extractDetail(res);
    -> ReferenceError at runtime, caught by the enclosing try, surfaced to the
       user as the upload error text "extractDetail is not defined", status 0
       instead of the real HTTP status, on EVERY failed context upload.

It reached `system-apps/workbench/assets/*.js` and both signed workbench
bundles. It passed 167 pre-merge gates and the bundle-freshness gate, because
`"build": "vite build"` in all four package.json files has no tsc step and no
gate typechecked the apps. It was found by running tsc by
hand. That is the hole this closes.

A dangling identifier is not a style problem — it is a guaranteed runtime
ReferenceError on the path that reaches it, and minification preserves the free
name rather than erroring, so the build stays green all the way into a signed
bundle.

SKIPS, LOUDLY, WITHOUT node_modules. tsc lives in each app's node_modules, which
a fresh clone does not have. A skip is reported as a skip with the reason —
never as a pass — so `npm ci`-less environments are honest rather than green.

Exit 0 on skip is deliberate, and the reason is the freshness ratchet, NOT the
image build. An earlier draft of this comment said "the ship host builds the apps
anyway"; it does not — Dockerfile.linux:185 is
`COPY native-bundles/ /app/native-bundles/`, the committed pre-built bundles, and
the file's only two npm mentions are comments about the Claude Code CLI package.

The operative reason: test_bundle_freshness.py forces `npm run ship` on whichever
machine changed `<app>-web/src` or `app-web-kit/`, and that machine necessarily
has node_modules — so this gate runs wherever a web change is actually made. A
machine without dev deps cannot have been the one that changed the source without
going red on freshness first. The residual gap was a machine that has neither —
every reviewer's fresh clone — and there this gate had nothing to say. Now
an app this branch changes is installed with `npm ci` and checked
there too, and a failed install fails the gate (see changed_apps below).
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPS = ["circles-web", "workbench-web", "community-web", "sites-web"]


def changed_apps():
    """Apps whose src/ (or the shared app-web-kit/) differs from where this
    branch left origin/main, uncommitted edits included.

    A fresh clone or a reviewer's checkout has no node_modules, so every app
    used to skip there and a branch that broke an app's types
    passed review. An app the branch changes is now installed with `npm ci`
    and checked; one it does not change still skips loudly. Returns None when
    git cannot say (no origin/main ref, or git itself failing), and the run
    says so rather than skipping silently."""
    try:
        base = subprocess.run(["git", "merge-base", "HEAD", "origin/main"], cwd=ROOT,
                              capture_output=True, text=True, timeout=30)
        if base.returncode != 0 or not base.stdout.strip():
            return None
        diff = subprocess.run(["git", "diff", "--name-only", base.stdout.strip(), "--"]
                              + [f"{a}/src" for a in APPS] + ["app-web-kit"],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)
        if diff.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    changed = set()
    for line in diff.stdout.splitlines():
        if line.startswith("app-web-kit/"):
            changed.update(APPS)
        for a in APPS:
            if line.startswith(a + "/src/"):
                changed.add(a)
    return changed


fails, skipped, checked = [], [], 0
changed = changed_apps()
if changed is None:
    print("  ....  could not tell which apps this branch changes (no origin/main "
          "ref, or git failed): apps without node_modules are skipped, not installed")
    changed = set()

for app in APPS:
    d = os.path.join(ROOT, app)
    if not os.path.isdir(d):
        continue
    tsc = os.path.join(d, "node_modules", ".bin", "tsc")
    if not os.path.isfile(tsc) and app in changed:
        # This branch changed the app, so a skip would let a type error through.
        try:
            # 4-6 s per app measured on the build machine; 120 s fails a hung install
            # with its own reason, inside the gate's 600 s (gate-timeouts.txt).
            r = subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=d,
                               capture_output=True, text=True, timeout=120)
            out = (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.SubprocessError) as e:
            r, out = None, str(e)
        if r is None or r.returncode != 0 or not os.path.isfile(tsc):
            lines = [ln for ln in out.splitlines() if ln.strip()][-8:]
            fails.append((app, ["this branch changes the app, and `npm ci` could not "
                                "install it to typecheck:"] + lines))
            continue
    if not os.path.isfile(tsc):
        skipped.append(f"{app}: no node_modules/.bin/tsc — run `npm ci` in {app}/ to check it")
        continue
    # Use the app's own tsconfig; --noEmit so this never writes build output.
    r = subprocess.run([tsc, "--noEmit"], cwd=d, capture_output=True, text=True)
    checked += 1
    if r.returncode != 0:
        out = (r.stdout or "") + (r.stderr or "")
        lines = [ln for ln in out.splitlines() if ln.strip()][:12]
        fails.append((app, lines))
    else:
        print(f"  ok    {app}: tsc --noEmit clean")

for s in skipped:
    print(f"  ....  SKIPPED {s}")

if fails:
    print(f"\nFAIL: {len(fails)} app(s) do not typecheck:")
    for app, lines in fails:
        print(f"  {app}:")
        for ln in lines:
            print(f"      {ln}")
    print("\nA dangling identifier here becomes a runtime ReferenceError in the")
    print("shipped bundle — minification keeps the free name and the build stays")
    print("green. Fix before merging.")
    sys.exit(1)

if checked == 0:
    print("web typecheck: SKIPPED — no app had node_modules; nothing was verified")
    sys.exit(0)

print(f"web typecheck — {checked} app(s) clean"
      + (f", {len(skipped)} skipped" if skipped else ""))
sys.exit(0)
