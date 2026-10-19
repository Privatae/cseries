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
        the release's images and WSL guest images. Every other key is kept as
        committed. Refuses to write a file that still says "dev" anywhere.

    release.py stamp-npm X.Y.Z PACKAGE_JSON...
        Sets each package's version to X.Y.Z, and every dependency on one of
        the release's own packages (@privatae/cseries-*) to exactly X.Y.Z.

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
OWN_NPM_SCOPE = "@privatae/cseries-"


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


def release_json(a):
    if not VERSION_RE.match(a.version):
        fail(f"--version must be X.Y.Z, got {a.version!r}")
    for d in (a.host_digest, a.postgres_digest):
        if not DIGEST_RE.match(d):
            fail(f"not a sha256 digest: {d!r}")
    with open(a.file, encoding="utf-8") as f:
        data = json.load(f)

    guest = {}
    for arch in GUEST_ARCHES:
        name = f"cseries-wsl-rootfs-{a.version}-{arch}.tar.gz"
        path = os.path.join(a.guest_dir, name)
        if not os.path.isfile(path) or not os.path.isfile(path + ".sha256"):
            fail(f"the WSL guest image {name} (and its .sha256) is not in {a.guest_dir}; "
                 "a CLI is never published without both guest images")
        with open(path + ".sha256", encoding="utf-8") as f:
            stated = (f.read().split() or [""])[0]
        got = sha256_file(path)
        if stated != got:
            fail(f"{name}.sha256 says {stated!r}, the file hashes to {got}")
        guest[f"wsl-{arch}"] = {"url": f"{a.download_base.rstrip('/')}/{name}", "sha256": got}

    data["version"] = a.version
    images = data.setdefault("images", {})
    images["host"] = {"ref": f"{a.host_image}:{a.version}", "digest": a.host_digest}
    images["postgres"] = {"ref": f"{a.postgres_image}:{a.version}", "digest": a.postgres_digest}
    data.setdefault("guest", {}).update(guest)

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
    if left:
        fail(f"placeholders left after filling: {', '.join(left)}")
    text = json.dumps(data, indent=2) + "\n"
    with open(a.file, "w", encoding="utf-8") as f:
        f.write(text)
    sys.stdout.write(text)


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
    else:
        stamp_npm(a.version, a.paths)


if __name__ == "__main__":
    main()
