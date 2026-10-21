#!/usr/bin/env python3
"""Checks and file writes for .github/workflows/release.yml.

    release.py oci-check LAYOUT_DIR IMAGE ARCH
        The OCI layout the build wrote holds one image manifest for ARCH, and
        IMAGE (the same archive, loaded into Docker for the smoke test) is that
        image: the same layers in the same order and the same runtime config.
        Prints the manifest digest, which is what gets pushed.

    release.py index-check INDEX_JSON AMD64_DIGEST ARM64_DIGEST
        INDEX_JSON (a registry's image index, or - for stdin) lists exactly
        those two manifests, as linux/amd64 and linux/arm64, and nothing else.

    release.py release-json FILE --version X.Y.Z --host-image REPO
            --host-digest D --postgres-image REPO --postgres-digest D
            --guest-dir DIR --download-base URL
        Fills the committed release.json (which holds "dev" placeholders) with
        the release's images and guest images (WSL's and Lima's, from
        --guest-dir, each beside its .sha256). Every other key is kept as
        committed. Refuses to write a file that still says "dev" anywhere.

    release.py cloud-json FILE --version X.Y.Z --host-image REPO
            --host-digest D --postgres-image REPO --postgres-digest D
            --overlay TARBALL --download-base URL --out OUT
        Writes OUT, the C-Series Cloud release manifest, from the committed
        FILE (vm/cloud/cloud-release.json, "dev" placeholders): the release's
        images and the overlay tarball's URL and sha256, checked against the
        .sha256 beside it. bastion.min_version is kept as committed. Refuses
        to write a manifest that still says "dev" anywhere. The maintainer
        signs OUT's exact bytes afterwards (docs/RELEASING.md).

    release.py stamp-npm X.Y.Z PACKAGE_JSON...
        Sets each package's version to X.Y.Z, and every dependency on one of
        the release's own packages (@privatae/cseries-*) to exactly X.Y.Z.

    release-json and cloud-json also take the runtime images and the harness
    source, all of them or none:
            --runtime FAMILY=REPO@DIGEST   (once per family: python, browser,
                                            helper, creative)
            --harness-src-url URL --harness-src-sha256 HEX
        With them the manifest gets a "runtime" section, which a machine's
        local runtime reads its image table from: each family's image pinned
        by digest, and the tarball the coding engines are built from. Without
        any of them the section is left out (a "runtime" the committed file
        holds with "dev" placeholders is dropped, never published).

    release.py notices --image REPO --version X.Y.Z --arch ARCH [--root DIR]
        Prints the third-party notices for one published runtime image: the
        Debian packages installed under DIR (default /) with the licences
        their copyright files name, the Python distributions with the licence
        their metadata names, and what the image adds beside them. The
        workflow runs it inside each image it publishes, so the list is the
        image's own.

Standard library only: the jobs that run it use the runner's own python3.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
GUEST_ARCHES = ("x64", "arm64")
# The guest image of each VM driver, as release.json keys it (release.js's
# GUEST_KEYS is these keys for each of GUEST_ARCHES) and as its build script
# names the file: WSL's root filesystem (vm/wsl/build-rootfs.sh) and Lima's
# qcow2 disk for macOS and Linux (vm/lima/build-image.sh).
GUEST_DRIVERS = (
    ("wsl", "WSL", "cseries-wsl-rootfs-{version}-{arch}.tar.gz"),
    ("lima", "Lima", "cseries-guest-{version}-{arch}.qcow2"),
)
OWN_NPM_SCOPE = "@privatae/cseries-"
# The image families a machine's local runtime starts containers from, each
# published as its own image. Every one is pinned whenever any is: a machine
# with only some of them would refuse the apps that need the others with no
# way to tell why.
RUNTIME_FAMILIES = ("python", "browser", "helper", "creative")
# A registry image name without a tag or a digest (release.js's REF, minus the
# tag): the digest is its own field, and a tag would say nothing the digest
# does not.
IMAGE_NAME_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$")


def fail(msg):
    print(f"release.py: {msg}", file=sys.stderr)
    sys.exit(1)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def blob_path(layout, digest):
    algo, _, hexpart = digest.partition(":")
    return os.path.join(layout, "blobs", algo, hexpart)


def read_blob(layout, digest):
    with open(blob_path(layout, digest), "rb") as f:
        raw = f.read()
    got = "sha256:" + hashlib.sha256(raw).hexdigest()
    if got != digest:
        fail(f"blob {digest} in {layout} hashes to {got}")
    return raw


def read_layout(layout, arch):
    """(manifest digest, config) of the layout's one image, every blob checked."""
    with open(os.path.join(layout, "index.json"), encoding="utf-8") as f:
        index = json.load(f)
    manifests = index.get("manifests") or []
    if len(manifests) != 1 or manifests[0].get("mediaType") != OCI_MANIFEST:
        fail(f"{layout}/index.json must list exactly one image manifest, found "
             f"{[m.get('mediaType') for m in manifests]}")
    digest = manifests[0]["digest"]
    manifest = json.loads(read_blob(layout, digest))
    config = json.loads(read_blob(layout, manifest["config"]["digest"]))
    for layer in manifest.get("layers") or []:
        path = blob_path(layout, layer["digest"])
        if not os.path.isfile(path) or "sha256:" + sha256_file(path) != layer["digest"]:
            fail(f"layer {layer['digest']} is missing from {layout} or does not match")
    if config.get("os") != "linux" or config.get("architecture") != arch:
        fail(f"{layout} is {config.get('os')}/{config.get('architecture')}, "
             f"this job builds linux/{arch}")
    return digest, config


def oci_check(layout, image, arch):
    digest, config = read_layout(layout, arch)
    diff_ids = config["rootfs"]["diff_ids"]

    out = subprocess.run(["docker", "image", "inspect", image],
                         capture_output=True, text=True, check=False)
    if out.returncode != 0:
        fail(f"docker image inspect {image}: {out.stderr.strip()}")
    loaded = json.loads(out.stdout)[0]
    # Compared by content, not by image id: the classic image store names an
    # image by its config digest and the containerd store by its manifest's,
    # so the id means different things on different runners. diff_ids are the
    # uncompressed layer digests, so equal lists are equal filesystems.
    if (loaded.get("RootFS") or {}).get("Layers") != diff_ids:
        fail(f"{image} does not have the layout's layers")
    if (loaded.get("Os"), loaded.get("Architecture")) != ("linux", arch):
        fail(f"{image} is {loaded.get('Os')}/{loaded.get('Architecture')}")
    want_cfg, got_cfg = config.get("config") or {}, loaded.get("Config") or {}
    for key in ("Entrypoint", "Cmd", "Env", "User", "WorkingDir", "Labels"):
        if (want_cfg.get(key) or None) != (got_cfg.get(key) or None):
            fail(f"{image} {key} is {got_cfg.get(key)!r}, the layout's is {want_cfg.get(key)!r}")
    print(digest)


def index_check(source, amd64, arm64):
    for d in (amd64, arm64):
        if not DIGEST_RE.match(d):
            fail(f"not a sha256 digest: {d!r}")
    raw = sys.stdin.read() if source == "-" else open(source, encoding="utf-8").read()
    index = json.loads(raw)
    got = sorted((m.get("digest"), (m.get("platform") or {}).get("os"),
                  (m.get("platform") or {}).get("architecture"))
                 for m in index.get("manifests") or [])
    want = sorted([(amd64, "linux", "amd64"), (arm64, "linux", "arm64")])
    if got != want:
        fail(f"the index lists {got}, expected {want}")


def runtime_section(a):
    """The manifest's "runtime" section from the --runtime and --harness-src-*
    flags, or None when none of them was given. Refuses a partial set."""
    specs = list(a.runtime or [])
    url, sha = a.harness_src_url, a.harness_src_sha256
    if not specs and url is None and sha is None:
        return None
    out = {}
    for spec in specs:
        family, eq, pinned = spec.partition("=")
        ref, at, digest = pinned.partition("@")
        if not eq or not at:
            fail(f"--runtime takes FAMILY=REPO@DIGEST, got {spec!r}")
        if family not in RUNTIME_FAMILIES:
            fail(f"--runtime family must be one of {', '.join(RUNTIME_FAMILIES)}, got {family!r}")
        if family in out:
            fail(f"--runtime names {family} twice")
        if not IMAGE_NAME_RE.match(ref):
            fail(f"--runtime {family}: {ref!r} is not an image name without a tag")
        if not DIGEST_RE.match(digest):
            fail(f"--runtime {family}: not a sha256 digest: {digest!r}")
        out[family] = {"ref": ref, "digest": digest}
    missing = [f for f in RUNTIME_FAMILIES if f not in out]
    if missing:
        fail(f"--runtime is missing {', '.join(missing)}; the runtime images are pinned all "
             "together or not at all")
    want = f"cseries-harness-src-{a.version}.tar.gz"
    if not isinstance(url, str) or not re.match(r"^https://[^\s]+$", url) or \
            url.rsplit("/", 1)[-1] != want:
        fail(f"--harness-src-url must be an https URL ending in /{want}, got {url!r}")
    if not isinstance(sha, str) or not SHA256_RE.match(sha):
        fail(f"--harness-src-sha256 must be 64 hex characters, got {sha!r}")
    out["harness_src"] = {"url": url, "sha256": sha}
    return out


def _set_runtime(data, a):
    rt = runtime_section(a)
    if rt is None:
        data.pop("runtime", None)
    else:
        data["runtime"] = rt


def release_json(a):
    if not VERSION_RE.match(a.version):
        fail(f"--version must be X.Y.Z, got {a.version!r}")
    for d in (a.host_digest, a.postgres_digest):
        if not DIGEST_RE.match(d):
            fail(f"not a sha256 digest: {d!r}")
    with open(a.file, encoding="utf-8") as f:
        data = json.load(f)

    guest = {}
    for driver, label, pattern in GUEST_DRIVERS:
        for arch in GUEST_ARCHES:
            name = pattern.format(version=a.version, arch=arch)
            path = os.path.join(a.guest_dir, name)
            if not os.path.isfile(path) or not os.path.isfile(path + ".sha256"):
                fail(f"the {label} guest image {name} (and its .sha256) is not in {a.guest_dir}; "
                     "a CLI is never published without every guest image")
            with open(path + ".sha256", encoding="utf-8") as f:
                stated = (f.read().split() or [""])[0]
            got = sha256_file(path)
            if stated != got:
                fail(f"{name}.sha256 says {stated!r}, the file hashes to {got}")
            guest[f"{driver}-{arch}"] = {"url": f"{a.download_base.rstrip('/')}/{name}", "sha256": got}

    data["version"] = a.version
    images = data.setdefault("images", {})
    images["host"] = {"ref": f"{a.host_image}:{a.version}", "digest": a.host_digest}
    images["postgres"] = {"ref": f"{a.postgres_image}:{a.version}", "digest": a.postgres_digest}
    data.setdefault("guest", {}).update(guest)
    _set_runtime(data, a)

    left = placeholders(data)
    if left:
        fail(f"placeholders left after filling: {', '.join(left)}")
    text = json.dumps(data, indent=2) + "\n"
    with open(a.file, "w", encoding="utf-8") as f:
        f.write(text)
    sys.stdout.write(text)


def placeholders(data):
    left = []

    def walk(node, where):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{where}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{where}[{i}]")
        elif isinstance(node, str) and (node == "dev" or node.endswith(":dev")):
            left.append(where)

    walk(data, "release")
    return left


def cloud_json(a):
    if not VERSION_RE.match(a.version):
        fail(f"--version must be X.Y.Z, got {a.version!r}")
    for d in (a.host_digest, a.postgres_digest):
        if not DIGEST_RE.match(d):
            fail(f"not a sha256 digest: {d!r}")
    with open(a.file, encoding="utf-8") as f:
        data = json.load(f)
    name = os.path.basename(a.overlay)
    if name != f"cseries-cloud-overlay-{a.version}.tar.gz":
        fail(f"the overlay for {a.version} is cseries-cloud-overlay-{a.version}.tar.gz, not {name}")
    if not os.path.isfile(a.overlay) or not os.path.isfile(a.overlay + ".sha256"):
        fail(f"{a.overlay} (and its .sha256) is missing")
    with open(a.overlay + ".sha256", encoding="utf-8") as f:
        stated = (f.read().split() or [""])[0]
    got = sha256_file(a.overlay)
    if stated != got:
        fail(f"{name}.sha256 says {stated!r}, the file hashes to {got}")
    data["version"] = a.version
    data["images"] = {
        "host": {"ref": f"{a.host_image}:{a.version}", "digest": a.host_digest},
        "postgres": {"ref": f"{a.postgres_image}:{a.version}", "digest": a.postgres_digest},
    }
    data["overlay"] = {"url": f"{a.download_base.rstrip('/')}/{name}", "sha256": got}
    if not VERSION_RE.match(str((data.get("bastion") or {}).get("min_version", ""))):
        fail("the committed cloud manifest names no bastion.min_version X.Y.Z")
    _set_runtime(data, a)
    left = placeholders(data)
    if left:
        fail(f"placeholders left after filling: {', '.join(left)}")
    text = json.dumps(data, indent=2) + "\n"
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(text)
    sys.stdout.write(text)


# What each published runtime image adds beside its Debian and Python
# packages, keyed by the image's name under ghcr.io/privatae. Each line states
# where the component and its licence files are, so a reader can check it in
# the image itself.
IMAGE_ADDS = {
    "cseries-app-runtime-python": [
        "C-Series runtime files (/opt/app-runtime: fetch.py, run.py, egress.py): "
        "AGPL-3.0-only, source at https://github.com/Privatae/cseries",
    ],
    "cseries-app-runtime-creative": [
        "C-Series runtime files (/opt/app-runtime): AGPL-3.0-only, source at "
        "https://github.com/Privatae/cseries",
        "Blender, from its release archive, unpacked whole into /opt/blender with the "
        "licence files the archive carries: GNU GPL. The source for exactly this version is "
        "attached to the same GitHub release as blender-<version>.tar.xz.",
        "The ArtCraft command-line programs listed in /opt/craft: each one's licence and "
        "NOTICE files are kept in /opt/craft/<name>/doc.",
    ],
    "cseries-browser": [
        "C-Series browser control service (/app): AGPL-3.0-only, source at "
        "https://github.com/Privatae/cseries",
        "Chromium is the Debian chromium package listed below.",
    ],
    "cseries-app-helper": [
        "C-Series app helper (/app-helper, a static Go binary): AGPL-3.0-only, source at "
        "https://github.com/Privatae/cseries (sidecars/app-helper)",
        "The Go standard library compiled into it: BSD-3-Clause (https://go.dev/LICENSE)",
        "/etc/ssl/certs/ca-certificates.crt, copied from the Alpine build image's "
        "ca-certificates package: MPL-2.0",
    ],
}


def _dpkg_installed(root):
    """[(package, version, source, source version)] of every package the dpkg
    status file under root says is installed, sorted; [] without one."""
    path = os.path.join(root, "var/lib/dpkg/status")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        return []
    out = []
    for para in text.split("\n\n"):
        fields = {}
        for line in para.splitlines():
            if line[:1] in (" ", "\t") or ":" not in line:
                continue
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
        if not fields.get("Package") or not fields.get("Status", "").endswith(" installed"):
            continue
        version = fields.get("Version", "")
        src, src_version = fields["Package"], version
        m = re.match(r"^(\S+)(?:\s+\((\S+)\))?$", fields.get("Source", ""))
        if m:
            src = m.group(1)
            src_version = m.group(2) or version
        out.append((fields["Package"], version, src, src_version))
    return sorted(out)


def _copyright_licences(root, package):
    """The licence names a package's machine-readable copyright file declares
    (its License: lines), or [] when it declares none that way."""
    path = os.path.join(root, "usr/share/doc", package, "copyright")
    names = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("License:"):
                    name = line[len("License:"):].strip()
                    if name:
                        names.add(name)
    except (FileNotFoundError, NotADirectoryError):
        pass
    return sorted(names)


def _python_dists(root):
    """[(name, version, licence)] of the Python distributions installed under
    root, read from their metadata, sorted by name."""
    import glob
    found = {}
    pats = ("usr/local/lib/python3*/site-packages/*.dist-info/METADATA",
            "usr/lib/python3/dist-packages/*.dist-info/METADATA")
    for pat in pats:
        for path in glob.glob(os.path.join(root, pat)):
            name = version = None
            expr = lic = None
            classifiers = []
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line in ("\n", "\r\n"):
                        break       # the headers end at the first blank line
                    k, _, v = line.partition(":")
                    v = v.strip()
                    if k == "Name":
                        name = v
                    elif k == "Version":
                        version = v
                    elif k == "License-Expression":
                        expr = v
                    elif k == "License" and v and lic is None:
                        lic = v
                    elif k == "Classifier" and v.startswith("License ::"):
                        classifiers.append(v.split("::")[-1].strip())
            if not name:
                continue
            licence = expr or (lic if lic and len(lic) <= 80 else None) or \
                "; ".join(classifiers) or "see the distribution's own licence file"
            found[(name.lower(), version)] = (name, version or "?", licence)
    return [found[k] for k in sorted(found)]


def notices(image, version, arch, root="/"):
    if not VERSION_RE.match(version):
        fail(f"--version must be X.Y.Z, got {version!r}")
    name = image.rsplit("/", 1)[-1]
    lines = [f"Third-party notices for {image}:{version} (linux/{arch})", ""]
    adds = IMAGE_ADDS.get(name)
    if adds is None:
        fail(f"no notices are written for {image}; add it to IMAGE_ADDS")
    lines.append("What this image adds:")
    lines += [f"- {a}" for a in adds]
    pkgs = _dpkg_installed(root)
    if pkgs:
        lines += ["", f"Debian packages ({len(pkgs)}). Each one's licence text is in the image at "
                  "/usr/share/doc/<package>/copyright; the source package and version are "
                  "listed, and https://snapshot.debian.org/package/<source>/<version>/ holds "
                  "that source."]
        for pkg, ver, src, src_ver in pkgs:
            lic = "; ".join(_copyright_licences(root, pkg)) or \
                "see /usr/share/doc/" + pkg + "/copyright"
            lines.append(f"- {pkg} {ver} (source {src} {src_ver}): {lic}")
    dists = _python_dists(root)
    if dists:
        lines += ["", f"Python distributions ({len(dists)}), with the licence their metadata names:"]
        lines += [f"- {n} {v}: {lic}" for n, v, lic in dists]
    return "\n".join(lines) + "\n"


def stamp_npm(version, paths):
    if not VERSION_RE.match(version):
        fail(f"version must be X.Y.Z, got {version!r}")
    for path in paths:
        with open(path, encoding="utf-8") as f:
            pkg = json.load(f)
        pkg["version"] = version
        for field in ("dependencies", "optionalDependencies", "peerDependencies"):
            for name in list((pkg.get(field) or {})):
                if name.startswith(OWN_NPM_SCOPE):
                    pkg[field][name] = version
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(pkg, indent=2) + "\n")
        print(f"{path}: {pkg.get('name')}@{version}")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("oci-check")
    o.add_argument("layout")
    o.add_argument("image")
    o.add_argument("arch", choices=("amd64", "arm64"))
    i = sub.add_parser("index-check")
    i.add_argument("index")
    i.add_argument("amd64")
    i.add_argument("arm64")
    r = sub.add_parser("release-json")
    r.add_argument("file")
    for flag in ("--version", "--host-image", "--host-digest", "--postgres-image",
                 "--postgres-digest", "--guest-dir", "--download-base"):
        r.add_argument(flag, required=True)
    c = sub.add_parser("cloud-json")
    c.add_argument("file")
    for flag in ("--version", "--host-image", "--host-digest", "--postgres-image",
                 "--postgres-digest", "--overlay", "--download-base", "--out"):
        c.add_argument(flag, required=True)
    for sp in (r, c):
        sp.add_argument("--runtime", action="append", metavar="FAMILY=REPO@DIGEST")
        sp.add_argument("--harness-src-url")
        sp.add_argument("--harness-src-sha256")
    n = sub.add_parser("notices")
    n.add_argument("--image", required=True)
    n.add_argument("--version", required=True)
    n.add_argument("--arch", required=True, choices=("amd64", "arm64"))
    n.add_argument("--root", default="/")
    s = sub.add_parser("stamp-npm")
    s.add_argument("version")
    s.add_argument("paths", nargs="+")
    a = p.parse_args()
    if a.cmd == "oci-check":
        oci_check(a.layout, a.image, a.arch)
    elif a.cmd == "index-check":
        index_check(a.index, a.amd64, a.arm64)
    elif a.cmd == "release-json":
        release_json(a)
    elif a.cmd == "cloud-json":
        cloud_json(a)
    elif a.cmd == "notices":
        sys.stdout.write(notices(a.image, a.version, a.arch, a.root))
    else:
        stamp_npm(a.version, a.paths)


if __name__ == "__main__":
    main()
