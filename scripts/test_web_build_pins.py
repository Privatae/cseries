#!/usr/bin/env python3
"""A signed app bundle is built from pinned third-party code, and its freshness
stamp covers everything its build reads.

What happened: the circles bundle committed with the calendar change was built
against a node_modules that did not match package-lock.json — a lockfile-exact
`npm ci` + `vite build` of the same source produced different content-hashed
names (index-CdsflcP3.css, chunks HWGCJPDP / KPVGNVTW). Every Host reinstalls
that bundle, so it cannot carry unpinned code.

  1. app-web-kit/verify-node-modules.mjs compares npm's own record of what is
     installed (node_modules/.package-lock.json) with package-lock.json:
     versions, integrity, resolved URLs, extra and missing packages. A drifted
     node_modules fails with "run npm ci"; a clean install passes.
  2. All four web apps' `ship` scripts run it BEFORE `vite build`, so a drifted
     node_modules cannot reach a build (a real `npm run ship` is run below with
     the exact ship string and the build replaced by a marker).
  3. The freshness stamp includes brain/mastercode/secret_shapes.json for the
     two apps whose build imports it, and the Python gate and place.mjs compute
     the same stamp (`place.mjs <app> --print-rev`).

Container-free: no pod, no DB. Needs node and npm (skipped honestly without).
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[web-pins] FAIL {name} {extra}")


node, npm = shutil.which("node"), shutil.which("npm")
if not node or not npm:
    print("web build pins: SKIPPED — node/npm not on PATH")
    sys.exit(0)

TMP = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None))
VERIFY = ROOT / "app-web-kit" / "verify-node-modules.mjs"


def lock_entry(version, integrity=None, resolved=None, **extra):
    return {"version": version,
            "resolved": resolved or f"https://registry.npmjs.org/x/-/x-{version}.tgz",
            "integrity": integrity or f"sha512-{version}", **extra}


def make_project(name, lock_pkgs, installed_pkgs, hidden=True):
    d = TMP / name
    (d / "node_modules").mkdir(parents=True)
    (d / "package-lock.json").write_text(json.dumps(
        {"lockfileVersion": 3, "packages": {"": {"name": name}, **lock_pkgs}}))
    if hidden:
        (d / "node_modules" / ".package-lock.json").write_text(json.dumps(
            {"lockfileVersion": 3, "packages": installed_pkgs}))
    return d


def verify(d):
    r = subprocess.run([node, str(VERIFY)], cwd=d, capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr


A = lambda v="1.0.0", **k: lock_entry(v, **k)
base = {"node_modules/a": A(), "node_modules/b": A("2.0.0")}

code, out = verify(make_project("clean", base, dict(base)))
check("verify: an install that matches the lockfile passes", code == 0 and "matches" in out, out)

code, out = verify(make_project("version", base, {**base, "node_modules/a": A("1.0.1", integrity=base["node_modules/a"]["integrity"], resolved=base["node_modules/a"]["resolved"])}))
check("verify: a different installed version fails with 'Run npm ci'",
      code == 1 and "Run npm ci" in out and "node_modules/a" in out, out)

code, out = verify(make_project("integrity", base, {**base, "node_modules/a": A(integrity="sha512-other")}))
check("verify: same version, different integrity fails", code == 1 and "integrity" in out, out)

code, out = verify(make_project("resolved", base, {**base, "node_modules/a": A(resolved="https://evil.example/a.tgz")}))
check("verify: same version and integrity, different source fails", code == 1 and "resolved" in out, out)

code, out = verify(make_project("extra", base, {**base, "node_modules/c": A("3.0.0")}))
check("verify: a package installed but not in the lockfile fails",
      code == 1 and "not in package-lock.json" in out, out)

code, out = verify(make_project("missing", base, {"node_modules/a": A()}))
check("verify: a lockfile package that is not installed fails",
      code == 1 and "not installed" in out and "node_modules/b" in out, out)

opt = {**base, "node_modules/fsevents": A("2.3.3", optional=True)}
code, out = verify(make_project("optional", opt, dict(base)))
check("verify: an optional package the platform skipped is fine", code == 0, out)

code, out = verify(make_project("nohidden", base, {}, hidden=False))
check("verify: a node_modules npm never wrote (no .package-lock.json) fails with 'Run npm ci'",
      code == 1 and "Run npm ci" in out, out)

(TMP / "nonm").mkdir()
(TMP / "nonm" / "package-lock.json").write_text("{}")
code, out = verify(TMP / "nonm")
check("verify: no node_modules at all fails", code == 1 and "npm ci" in out, out)

# ── every app's ship runs it before the build ────────────────────────────────
for app in ("circles", "workbench", "community", "sites"):
    pkg = json.loads((ROOT / f"{app}-web" / "package.json").read_text())
    ship = pkg["scripts"]["ship"]
    check(f"ship[{app}]: verifies node_modules before vite build",
          ship.startswith("node ../app-web-kit/verify-node-modules.mjs && vite build"), ship)

# Run the REAL ship string, with only the build swapped for a marker, in a tree
# laid out like the repo (app-web-kit beside the web dir).
(TMP / "repo" / "app-web-kit").mkdir(parents=True)
shutil.copy(VERIFY, TMP / "repo" / "app-web-kit" / "verify-node-modules.mjs")


def run_ship(name, lock_pkgs, installed_pkgs):
    d = make_project(f"repo/{name}-web", lock_pkgs, installed_pkgs)
    ship = json.loads((ROOT / "circles-web" / "package.json").read_text())["scripts"]["ship"]
    ship = ship.replace("vite build && npm run place", "node -e \"console.log('BUILD'+'-RAN')\"")
    (d / "package.json").write_text(json.dumps({"name": name, "scripts": {"ship": ship}}))
    r = subprocess.run([npm, "run", "ship"], cwd=d, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


code, out = run_ship("drift", base, {**base, "node_modules/a": A("9.9.9")})
check("ship: a drifted node_modules fails `npm run ship` and the build never runs",
      code != 0 and "BUILD-RAN" not in out and "Run npm ci" in out, out[-400:])
code, out = run_ship("clean", base, dict(base))
check("ship: a matching node_modules passes and the build runs", code == 0 and "BUILD-RAN" in out, out[-400:])

# A real `npm ci` of circles-web's own lockfile passes the check (needs npm's
# cache or network; reported, not failed, when neither is there).
real = TMP / "real-circles"
real.mkdir()
shutil.copy(ROOT / "circles-web" / "package.json", real / "package.json")
shutil.copy(ROOT / "circles-web" / "package-lock.json", real / "package-lock.json")
ci = subprocess.run([npm, "ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"],
                    cwd=real, capture_output=True, text=True, timeout=300)
if ci.returncode == 0:
    code, out = verify(real)
    check("real: a clean `npm ci` of circles-web's lockfile passes the check", code == 0, out)
    pk = real / "node_modules" / "react" / "package.json"
    if pk.exists():
        data = json.loads(pk.read_text())
        data["version"] = "0.0.1"
        pk.write_text(json.dumps(data))
        hidden = real / "node_modules" / ".package-lock.json"
        h = json.loads(hidden.read_text())
        h["packages"]["node_modules/react"]["version"] = "0.0.1"
        hidden.write_text(json.dumps(h))
        code, out = verify(real)
        check("real: a node_modules whose react version drifted from the lockfile fails",
              code == 1 and "node_modules/react" in out, out)
else:
    print("  (real npm ci not run: no npm cache/network here: " + ci.stderr.strip()[-120:] + ")")

# ── the freshness stamp: Python gate and place.mjs agree, and cover the rules file ──
spec = importlib.util.spec_from_file_location("bundle_freshness", ROOT / "scripts" / "test_bundle_freshness.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)


def layout(root: Path):
    for app in ("circles", "sites"):
        (root / f"{app}-web" / "src").mkdir(parents=True)
        (root / f"{app}-web" / "src" / "main.ts").write_text(f"// {app}\n")
        (root / f"{app}-web" / "package-lock.json").write_text("{}\n")
    (root / "app-web-kit" / "ui").mkdir(parents=True)
    (root / "app-web-kit" / "ui" / "button.tsx").write_text("// kit\n")
    shutil.copy(ROOT / "app-web-kit" / "place.mjs", root / "app-web-kit" / "place.mjs")
    (root / "brain" / "mastercode").mkdir(parents=True)
    (root / "brain" / "mastercode" / "secret_shapes.json").write_text('{"rules": []}\n')


def js_rev(root: Path, web: str, app: str) -> str:
    r = subprocess.run([node, "../app-web-kit/place.mjs", app, "--print-rev"],
                       cwd=root / web, capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


st = TMP / "stamp"
layout(st)
py0 = {a: bf.web_src_rev(st, f"{a}-web", a) for a in ("circles", "sites")}
js0 = {a: js_rev(st, f"{a}-web", a) for a in ("circles", "sites")}
check("stamp: place.mjs and the Python gate compute the same stamp for every app",
      py0 == js0 and all(len(v) == 64 for v in js0.values()), repr((py0, js0)))
(st / "brain" / "mastercode" / "secret_shapes.json").write_text('{"rules": [1]}\n')
py1 = {a: bf.web_src_rev(st, f"{a}-web", a) for a in ("circles", "sites")}
js1 = {a: js_rev(st, f"{a}-web", a) for a in ("circles", "sites")}
check("stamp: editing secret_shapes.json changes circles' stamp (Python gate and place.mjs)",
      py1["circles"] != py0["circles"] and js1["circles"] != js0["circles"] and py1 == js1,
      repr((py0, py1)))
check("stamp: ...and leaves an app whose build does not read it (sites) alone",
      py1["sites"] == py0["sites"] and js1["sites"] == js0["sites"], repr((py0, py1)))
(st / "app-web-kit" / "README.md").write_text("# The kit, reworded\n")
(st / "app-web-kit" / "ui" / "notes.md").write_text("notes\n")
py2 = {a: bf.web_src_rev(st, f"{a}-web", a) for a in ("circles", "sites")}
js2 = {a: js_rev(st, f"{a}-web", a) for a in ("circles", "sites")}
check("stamp: the kit's Markdown (its documentation, which no build reads) moves no stamp",
      py2 == py1 and js2 == js1, repr((py1, py2, js2)))
(st / "app-web-kit" / "ui" / "button.tsx").write_text("// kit, changed\n")
py3 = {a: bf.web_src_rev(st, f"{a}-web", a) for a in ("circles", "sites")}
js3 = {a: js_rev(st, f"{a}-web", a) for a in ("circles", "sites")}
check("stamp: ...while an edit to the kit's code moves every app's (Python gate and place.mjs)",
      all(py3[a] != py2[a] for a in py3) and py3 == js3, repr((py2, py3, js3)))
check("stamp: the rules file is in the table for both apps whose src imports it",
      bf.WEB_EXTRA_INPUTS == {"circles": ["brain/mastercode/secret_shapes.json"],
                              "workbench": ["brain/mastercode/secret_shapes.json"]}
      and "secret_shapes.json" in (ROOT / "circles-web/src/lib/secret-shapes.ts").read_text()
      and "secret_shapes.json" in (ROOT / "workbench-web/src/lib/secret-shapes.ts").read_text(), "")

shutil.rmtree(TMP, ignore_errors=True)
if FAILS:
    print(f"\n[web-pins] {len(FAILS)} FAILED")
    sys.exit(1)
print("\nweb build pins: all passed")
