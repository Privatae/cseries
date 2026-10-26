#!/usr/bin/env python3
"""The release publishes what a machine's local runtime needs, and pins it.

  1. release.py writes a "runtime" section into cloud-release.json and
     release.json: each of the four image families pinned by digest, and the
     coding engines' source tarball by URL and sha256. All of them or none;
     a partial set, a tag in place of a bare image name, a malformed digest,
     an http or misnamed tarball URL and a short sha256 are refused, and a
     manifest written without them carries no runtime placeholder.
  2. release.py notices lists an image's installed Debian packages with the
     licences their copyright files name, its Python distributions with the
     licence their metadata names, and what the image adds beside them.
  3. The workflow builds, smoke-tests, scans, pushes by digest and attests
     each runtime image per architecture (creative on amd64 only), tags
     exactly the tested digests, attaches the harness source tarball, the
     Blender source held to a pinned sha256 for the version the creative
     image unpacks, and the images' notices, writes the store catalog after
     the tags and the release exist with contents: write only and leaves its
     signature to the maintainer, and pins every runtime image in the cloud
     and npm manifests. It builds and attaches the Lima guest images beside
     the WSL ones, and release.py pins them in release.json.
  4. The app helper's build image is pinned by digest.
  5. Each runtime image's SPDX SBOM, written from the tested archive, is
     attested to its pushed digest in the registry.

Container-free: python3 scripts/test_release_runtime_images.py (needs PyYAML,
from scripts/requirements-gates.txt). No network, no container.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RELEASE_PY = os.path.join(ROOT, ".github/scripts/release.py")

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


def run(*args):
    return subprocess.run(["python3", RELEASE_PY, *args], capture_output=True, text=True, check=False)


D = {f: "sha256:" + c * 64 for f, c in zip(("host", "postgres", "python", "browser", "helper",
                                             "creative"), "abcdef")}
REPOS = {"python": "ghcr.io/privatae/cseries-app-runtime-python",
         "browser": "ghcr.io/privatae/cseries-browser",
         "helper": "ghcr.io/privatae/cseries-app-helper",
         "creative": "ghcr.io/privatae/cseries-app-runtime-creative"}
BASE = "https://github.com/Privatae/cseries/releases/download/v4.5.6"
SRC_URL = f"{BASE}/cseries-harness-src-4.5.6.tar.gz"
SRC_SHA = "1" * 64


def runtime_args(drop=None, **over):
    out = []
    for fam, repo in REPOS.items():
        if fam == drop:
            continue
        out += ["--runtime", over.get(fam) or f"{fam}={repo}@{D[fam]}"]
    out += ["--harness-src-url", over.get("url", SRC_URL),
            "--harness-src-sha256", over.get("sha", SRC_SHA)]
    return out


# ── 1. the runtime section ───────────────────────────────────────────────────

with tempfile.TemporaryDirectory() as tmp:
    overlay = os.path.join(tmp, "cseries-cloud-overlay-4.5.6.tar.gz")
    with open(overlay, "wb") as f:
        f.write(b"an overlay\n")
    with open(overlay + ".sha256", "w") as f:
        f.write(hashlib.sha256(b"an overlay\n").hexdigest() + "  cseries-cloud-overlay-4.5.6.tar.gz\n")
    out = os.path.join(tmp, "cloud-release.json")
    cloud = ["cloud-json", os.path.join(ROOT, "vm/cloud/cloud-release.json"), "--version", "4.5.6",
             "--host-image", "ghcr.io/privatae/cseries", "--host-digest", D["host"],
             "--postgres-image", "ghcr.io/privatae/cseries-postgres", "--postgres-digest", D["postgres"],
             "--overlay", overlay, "--download-base", BASE, "--out", out]

    r = run(*cloud, *runtime_args())
    check(r.returncode == 0, "cloud-json writes the manifest with the runtime images", r.stderr)
    m = json.load(open(out)) if r.returncode == 0 else {}
    want = {fam: {"ref": repo, "digest": D[fam]} for fam, repo in REPOS.items()}
    want["harness_src"] = {"url": SRC_URL, "sha256": SRC_SHA}
    check(m.get("runtime") == want,
          "its runtime section pins each family by digest and the harness source by URL and sha256",
          m.get("runtime"))
    check(m.get("images", {}).get("host") == {"ref": "ghcr.io/privatae/cseries:4.5.6", "digest": D["host"]},
          "and the Host's images are written as before")

    r = run(*cloud)
    m = json.load(open(out)) if r.returncode == 0 else {}
    check(r.returncode == 0 and "runtime" not in m and '"dev"' not in json.dumps(m),
          "without the runtime flags the template's placeholder section is left out, not published",
          r.stderr + json.dumps(m))

    refusals = [
        ("a missing family", runtime_args(drop="creative"), "missing creative"),
        ("a family twice", runtime_args() + ["--runtime", f"python={REPOS['python']}@{D['python']}"], "twice"),
        ("an unknown family", runtime_args() + ["--runtime", f"java={REPOS['python']}@{D['python']}"],
         "family must be"),
        ("an image name with a tag", runtime_args(browser=f"browser={REPOS['browser']}:4.5.6@{D['browser']}"),
         "without a tag"),
        ("a malformed digest", runtime_args(helper=f"helper={REPOS['helper']}@sha256:abc"), "digest"),
        ("a value without a digest", runtime_args(helper=f"helper={REPOS['helper']}"), "FAMILY=REPO@DIGEST"),
        ("a tarball over http", runtime_args(url=SRC_URL.replace("https:", "http:")), "harness-src-url"),
        ("a tarball of another version", runtime_args(url=f"{BASE}/cseries-harness-src-4.5.5.tar.gz"),
         "harness-src-url"),
        ("a short tarball sha256", runtime_args(sha="1" * 10), "harness-src-sha256"),
        ("a tarball with no sha256", runtime_args()[:-2], "harness-src-sha256"),
    ]
    for what, args, says in refusals:
        r = run(*cloud, *args)
        check(r.returncode != 0 and r.stderr.startswith("release.py: ") and says in r.stderr,
              f"cloud-json refuses {what}", r.stderr[-200:])
    r = run(*cloud, "--harness-src-url", SRC_URL)
    check(r.returncode != 0 and r.stderr.startswith("release.py: ") and "missing python" in r.stderr,
          "cloud-json refuses a harness source with no runtime images", r.stderr[-200:])

    # release.json (npm): the same section, beside the guest images.
    guest = os.path.join(tmp, "guest")
    os.makedirs(guest)
    # One file per guest image release.json pins (release.js's GUEST_KEYS):
    # WSL's root filesystem and Lima's qcow2 disk, for each CPU.
    def guest_file(name, body):
        with open(os.path.join(guest, name), "wb") as f:
            f.write(body)
        with open(os.path.join(guest, name + ".sha256"), "w") as f:
            f.write(hashlib.sha256(body).hexdigest() + f"  {name}\n")

    for arch in ("x64", "arm64"):
        guest_file(f"cseries-wsl-rootfs-4.5.6-{arch}.tar.gz", f"wsl-{arch}".encode())
        guest_file(f"cseries-guest-4.5.6-{arch}.qcow2", f"lima-{arch}".encode())
    DOOR_DIGEST = "sha256:" + "9" * 64
    npm_args = ("--version", "4.5.6",
                "--host-image", "ghcr.io/privatae/cseries", "--host-digest", D["host"],
                "--postgres-image", "ghcr.io/privatae/cseries-postgres", "--postgres-digest", D["postgres"],
                "--door-image", "ghcr.io/privatae/cseries-remote-door", "--door-digest", DOOR_DIGEST,
                "--guest-dir", guest, "--download-base", BASE, *runtime_args())
    npm = os.path.join(tmp, "release.json")
    shutil.copy(os.path.join(ROOT, "packages/cseries/release.json"), npm)
    r = run("release-json", npm, *npm_args)
    m = json.load(open(npm)) if r.returncode == 0 else {}
    check(r.returncode == 0 and m.get("runtime") == want,
          "release-json writes the same runtime section into the npm release.json", r.stderr)
    check(m.get("guest", {}).get("wsl-x64", {}).get("url") == f"{BASE}/cseries-wsl-rootfs-4.5.6-x64.tar.gz",
          "and the guest images as before")
    check(m.get("images", {}).get("door") == {"ref": "ghcr.io/privatae/cseries-remote-door:4.5.6",
                                              "digest": DOOR_DIGEST},
          "and the remote-access door's image, pinned by digest", m.get("images"))
    shutil.copy(os.path.join(ROOT, "packages/cseries/release.json"), npm)
    no_door = [x for x in npm_args]
    i = no_door.index("--door-image")
    del no_door[i:i + 4]
    r = run("release-json", npm, *no_door)
    check(r.returncode != 0 and "--door-image" in r.stderr,
          "release-json refuses to write a CLI release without the door's image", r.stderr[-200:])
    check(all(m.get("guest", {}).get(f"lima-{arch}") ==
              {"url": f"{BASE}/cseries-guest-4.5.6-{arch}.qcow2",
               "sha256": hashlib.sha256(f"lima-{arch}".encode()).hexdigest()} for arch in ("x64", "arm64")),
          "and each Lima guest image by its URL on the release and its sha256", m.get("guest"))
    # A release built without a Lima guest is refused, not published with a
    # manifest the CLI would reject on macOS and Linux.
    os.rename(os.path.join(guest, "cseries-guest-4.5.6-arm64.qcow2"), os.path.join(tmp, "moved.qcow2"))
    shutil.copy(os.path.join(ROOT, "packages/cseries/release.json"), npm)
    r = run("release-json", npm, *npm_args)
    check(r.returncode != 0 and "cseries-guest-4.5.6-arm64.qcow2" in r.stderr,
          "release-json refuses a guest directory with no Lima guest for one CPU", r.stderr[-200:])

with open(os.path.join(ROOT, "vm/cloud/cloud-release.json")) as f:
    template = json.load(f)
check(set((template.get("runtime") or {})) == set(REPOS) | {"harness_src"},
      "the committed cloud manifest shows the runtime section's shape, with placeholders",
      template.get("runtime"))

# ── 2. notices ───────────────────────────────────────────────────────────────

STATUS = """Package: libexample1
Status: install ok installed
Version: 2.0-1
Source: example (2.0-1)
Description: an invented library

Package: exampletool
Status: install ok installed
Version: 1.4-2+b1
Source: exampletool (1.4-2)

Package: gone
Status: deinstall ok config-files
Version: 0.1

Package: plain
Status: install ok installed
Version: 3.3
"""
with tempfile.TemporaryDirectory() as root:
    os.makedirs(os.path.join(root, "var/lib/dpkg"))
    with open(os.path.join(root, "var/lib/dpkg/status"), "w") as f:
        f.write(STATUS)
    for pkg, text in (("libexample1", "Files: *\nLicense: LGPL-2.1+\n\nFiles: x\nLicense: GPL-2+\n"),
                      ("exampletool", "This file is not machine-readable.\n")):
        os.makedirs(os.path.join(root, "usr/share/doc", pkg))
        with open(os.path.join(root, "usr/share/doc", pkg, "copyright"), "w") as f:
            f.write(text)
    site = os.path.join(root, "usr/local/lib/python3.13/site-packages")
    for name, meta in (("widgetlib-1.0.dist-info", "Name: widgetlib\nVersion: 1.0\nLicense-Expression: MIT\n"),
                       ("oldlib-2.1.dist-info", "Name: oldlib\nVersion: 2.1\nLicense: UNKNOWN LONG TEXT " + "x" * 100 +
                        "\nClassifier: License :: OSI Approved :: BSD License\n\nLicense: not a header\n")):
        os.makedirs(os.path.join(site, name))
        with open(os.path.join(site, name, "METADATA"), "w") as f:
            f.write(meta)
    r = run("notices", "--image", REPOS["python"], "--version", "4.5.6", "--arch", "arm64", "--root", root)
    text = r.stdout
    check(r.returncode == 0 and text.startswith(f"Third-party notices for {REPOS['python']}:4.5.6 (linux/arm64)"),
          "notices names the image, its version and architecture", r.stderr)
    check("- libexample1 2.0-1 (source example 2.0-1): GPL-2+; LGPL-2.1+" in text,
          "a package is listed with its source and the licences its copyright file declares", text)
    check("- exampletool 1.4-2+b1 (source exampletool 1.4-2): see /usr/share/doc/exampletool/copyright" in text,
          "a copyright file that declares none points at itself, and a binNMU names its source version")
    check("- plain 3.3 (source plain 3.3)" in text and "gone" not in text,
          "a package with no Source field is its own source, and a removed package is not listed")
    check("Debian packages (3)" in text and "snapshot.debian.org" in text,
          "the count and the source pointer are stated")
    check("- widgetlib 1.0: MIT" in text and "- oldlib 2.1: BSD License" in text and "not a header" not in text,
          "Python distributions carry the licence their metadata names, read from the headers only", text)
    check("AGPL-3.0-only" in text, "what the image adds is stated")

with tempfile.TemporaryDirectory() as empty:
    r = run("notices", "--image", REPOS["helper"], "--version", "4.5.6", "--arch", "amd64", "--root", empty)
    check(r.returncode == 0 and "Go standard library" in r.stdout and "Debian packages" not in r.stdout,
          "a scratch image's notices are what it adds", r.stdout + r.stderr)
    r = run("notices", "--image", "ghcr.io/privatae/cseries-unknown", "--version", "4.5.6", "--arch", "amd64",
            "--root", empty)
    check(r.returncode != 0 and "no notices are written" in r.stderr,
          "an image with no notices written for it is refused")
    r = run("notices", "--image", REPOS["helper"], "--version", "4.5", "--arch", "amd64", "--root", empty)
    check(r.returncode != 0 and "X.Y.Z" in r.stderr, "a version that is not X.Y.Z is refused")

# ── 3. the workflow ──────────────────────────────────────────────────────────

with open(os.path.join(ROOT, ".github/workflows/release.yml")) as f:
    wf = yaml.safe_load(f)
jobs = wf["jobs"]
env = wf.get("env") or {}
FAMILY_ENV = {"python": "PYTHON_IMAGE", "browser": "BROWSER_IMAGE", "helper": "HELPER_IMAGE",
              "creative": "CREATIVE_IMAGE"}
check(all(env.get(FAMILY_ENV[f]) == REPOS[f] for f in REPOS), "the workflow names the four runtime images",
      {k: env.get(v) for k, v in FAMILY_ENV.items()})


def script(job):
    return "\n".join(st.get("run", "") for st in job.get("steps", []))


def pinned(job):
    return all(re.match(r"^[^@]+@[0-9a-f]{40}$", st["uses"]) for st in job.get("steps", []) if "uses" in st)


img = jobs.get("runtime-image") or {}
inc = (((img.get("strategy") or {}).get("matrix") or {}).get("include")) or []
pairs = sorted((e.get("image"), e.get("arch")) for e in inc)
check(pairs == sorted([("browser", "amd64"), ("browser", "arm64"), ("creative", "amd64"),
                       ("helper", "amd64"), ("helper", "arm64"), ("python", "amd64"), ("python", "arm64")]),
      "an image job per runtime image and architecture, creative on amd64 only", pairs)
check(all(e.get("repo") == REPOS.get(e.get("image")) for e in inc),
      "each matrix entry pushes to its family's repository")
check(all((e["arch"], e["runner"], e["machine"]) in (("amd64", "ubuntu-24.04", "x86_64"),
                                                    ("arm64", "ubuntu-24.04-arm", "aarch64")) for e in inc),
      "each architecture builds natively on a runner of that architecture")
DOCKERFILES = {"python": "sidecars/app-runtime-python/Dockerfile", "browser": "sidecars/browser/Dockerfile",
               "helper": "sidecars/app-helper/Dockerfile", "creative": "sidecars/app-runtime-creative/Dockerfile"}
check(all(e.get("dockerfile") == DOCKERFILES[e["image"]] and os.path.isfile(os.path.join(ROOT, e["dockerfile"]))
          and os.path.isdir(os.path.join(ROOT, e["context"])) for e in inc),
      "each builds from its own Dockerfile and a context that exists")
s = script(img)
check("--scanners secret --exit-code 1" in s and '--severity "$VULN_FAIL_SEVERITY" --ignore-unfixed --exit-code 1' in s,
      "the image job runs trivy's secret scan and the vulnerability gate over the archive")
check("release.py oci-check" in s and 'crane push "$RUNNER_TEMP/oci/layout" "$REPO@$DIGEST"' in s and
      'crane manifest "$REPO@$DIGEST" | sha256sum' in s,
      "it pushes the tested layout by digest and checks the registry's manifest hashes to it")
check(all(f"{name})" in s or f"{name}|" in s or f"|{name})" in s for name in REPOS) and "Smoke test" in
      [st.get("name") for st in img.get("steps", [])],
      "it smoke-tests every runtime image")
check("seccomp-browser.json" in s and "/health" in s and "-healthcheck" in s and "blender" in s,
      "the browser under its own seccomp profile, the helper's own probe and Blender are exercised")
check("release.py notices" in s, "it writes each image's notices from inside the image")
check(any("attest-build-provenance" in st.get("uses", "") for st in img.get("steps", [])) and pinned(img),
      "it attests each image, with actions pinned by commit")
steps = img.get("steps", [])
sbom_steps = [st for st in steps if "--format spdx-json" in st.get("run", "")]
attest_sbom = [st for st in steps if (st.get("with") or {}).get("sbom-path")]
names = [st.get("name") for st in steps]
check(len(sbom_steps) == 1 and '--input "$archive"' in sbom_steps[0]["run"] and
      "--skip-db-update" in sbom_steps[0]["run"],
      "it writes an SPDX SBOM of each image from the tested archive")
check(len(attest_sbom) == 1 and attest_sbom[0].get("uses", "").startswith("actions/attest@") and
      (attest_sbom[0].get("with") or {}).get("push-to-registry") is True and
      (attest_sbom[0].get("with") or {}).get("subject-digest") == "${{ steps.oci.outputs.digest }}" and
      (attest_sbom[0].get("with") or {}).get("subject-name") == "${{ matrix.repo }}" and
      names.index(attest_sbom[0].get("name")) > names.index("Push by digest"),
      "it attests that SBOM to the pushed digest in the registry (an asset would differ on every re-run)",
      [st.get("name") for st in attest_sbom])
check(img.get("permissions") == {"contents": "read", "packages": "write", "id-token": "write",
                                  "attestations": "write"}, "the image job's permissions are the host image job's")

mrg = jobs.get("runtime-merge") or {}
ms = script(mrg)
check("runtime-image" in mrg.get("needs", []) and "index-check" in ms and 'crane tag "$CREATIVE_IMAGE@$creative"' in ms
      and "is never moved" in ms, "the tags are made from exactly the tested digests and never moved")
check(set((mrg.get("outputs") or {})) == set(REPOS), "the tag job hands on each family's digest")

rel = jobs.get("runtime-release") or {}
rs = script(rel)
check(set(rel.get("needs", [])) >= {"version", "runtime-merge", "release"} and
      rel.get("permissions") == {"contents": "write"}, "the runtime assets go up after the release exists")
check("vm/runtime/build-harness-src.sh" in rs and "cseries-harness-src-$VERSION.tar.gz" in rs,
      "the harness source tarball is built and its sha256 checked")
check('sha256sum -c' in rs and "BLENDER_SOURCE_SHA256" in rs and "md5sum -c" in rs and
      "download.blender.org/source" in rs, "the Blender source is checked against its sha256 before it is attached")
check(re.fullmatch(r"[0-9a-f]{64}", str(env.get("BLENDER_SOURCE_SHA256", ""))) is not None,
      "the Blender source's sha256 is pinned")
with open(os.path.join(ROOT, "sidecars/app-runtime-creative/Dockerfile")) as f:
    m = re.search(r"^ARG BLENDER_URL=\S*/blender-([0-9.]+)-linux-x64\.tar\.xz$", f.read(), re.M)
check(m is not None and str(env.get("BLENDER_SOURCE_VERSION")) == m.group(1),
      "the pinned Blender source is the version the creative image unpacks",
      (env.get("BLENDER_SOURCE_VERSION"), m and m.group(1)))
check("never replaced" in rs and pinned(rel), "a released asset is never replaced")

st = jobs.get("store") or {}
ss = script(st)
check(set(st.get("needs", [])) >= {"version", "runtime-merge", "release"} and
      st.get("permissions") == {"contents": "write"},
      "the store job runs after the tags and the release exist, with contents: write only", st.get("needs"))
check("scripts/build_store_catalog.py" in ss and all(flag in ss for flag in (
      "--version", "--bundles factory-bundles", "--download-base", "--images", "--source-base", "--out")),
      "it writes the catalog with build_store_catalog.py")
check('"$out/catalog.json" "$out"/*.cseries' in ss and "never replaced" in ss,
      "it uploads catalog.json and the bundles, never replacing a released asset")
check("release-sign sign cseries-store" in ss and "secrets." not in json.dumps(st) and pinned(st),
      "it leaves the cseries-store signature to the maintainer and uses no secret")

for name in ("cloud", "npm"):
    j = jobs.get(name) or {}
    js = script(j)
    check(set(j.get("needs", [])) >= {"runtime-merge", "runtime-release"} and
          all(f'--runtime "{fam}=${FAMILY_ENV[fam]}@${fam.upper()}_DIGEST"' in js for fam in REPOS) and
          "--harness-src-url" in js and "--harness-src-sha256" in js,
          f"the {name} job pins every runtime image and the harness source in its manifest")
npm_s = script(jobs.get("npm") or {})
check('"$CREATIVE_IMAGE@$CREATIVE_DIGEST"' in npm_s and 'm["runtime"]["harness_src"]' in npm_s,
      "the npm job checks the runtime images and the tarball are public before publishing")
check("secrets." not in json.dumps(wf), "the workflow uses no secret")

# The remote-access door's image goes through the Host's image jobs the way
# the database image does: built from its Dockerfile, checked against its
# layout, scanned, pushed by digest, attested, tagged from the tested
# digests, and pinned in the npm release.json, whose public check covers it.
hi = script(jobs.get("image") or {})
check(env.get("DOOR_IMAGE") == "ghcr.io/privatae/cseries-remote-door" and
      "-f sidecars/remote-door/Dockerfile" in hi and
      'oci-check "$RUNNER_TEMP/oci/door" cseries-remote-door:release "$ARCH"' in hi and
      "for name in host postgres door; do" in hi and
      'crane push "$RUNNER_TEMP/oci/door" "$DOOR_IMAGE@$DOOR_DIGEST"' in hi and
      '"$RUNNER_TEMP/digests/door"' in hi and
      any((st.get("with") or {}).get("subject-name") == "${{ env.DOOR_IMAGE }}"
          for st in (jobs.get("image") or {}).get("steps", [])),
      "the door's image is built, checked, scanned, pushed by digest and attested like the database's")
mj = jobs.get("merge") or {}
check("for image in host postgres door; do" in script(mj) and
      (mj.get("outputs") or {}).get("door") == "${{ steps.tag.outputs.door }}",
      "its multi-arch tag is made from exactly the tested digests and handed on")
check('--door-image "$DOOR_IMAGE" --door-digest "$DOOR_DIGEST"' in npm_s and '"$DOOR_IMAGE@$DOOR_DIGEST"' in npm_s and
      ((jobs.get("npm") or {}).get("env") or {}).get("DOOR_DIGEST") == "${{ needs.merge.outputs.door }}",
      "the npm job pins the door's image in release.json and checks it is public")

# The guest job builds every guest image release.json pins, and the release
# job attaches each of them.
gs = script(jobs.get("guest") or {})
check("vm/wsl/build-rootfs.sh" in gs and "vm/lima/build-image.sh" in gs and
      '"cseries-guest-$VERSION-$ARCH.qcow2"' in gs,
      "the guest job builds the Lima guest image beside the WSL one, and checks its sha256")
check('"cseries-guest-$VERSION-$arch.qcow2"' in script(jobs.get("release") or {}),
      "the release job attaches the Lima guest images")

# ── 4. the helper's build image ──────────────────────────────────────────────

with open(os.path.join(ROOT, "sidecars/app-helper/Dockerfile")) as f:
    froms = re.findall(r"^FROM\s+(\S+)", f.read(), re.M)
check(froms and all(f == "scratch" or re.search(r"@sha256:[0-9a-f]{64}$", f) for f in froms),
      "the app helper's build image is pinned by digest", froms)

sys.exit(failed)
