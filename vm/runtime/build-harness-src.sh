#!/usr/bin/env bash
# Builds the coding engines' source tarball: everything the engines'
# Dockerfiles copy from this tree, so a machine can build each engine itself
# (the engines' images are never published; their vendors' binaries are
# downloaded and checksum-checked by each Dockerfile at build time).
#
#   vm/runtime/build-harness-src.sh --version X.Y.Z --out <dir> [--epoch SECONDS]
#
# Writes <dir>/cseries-harness-src-X.Y.Z.tar.gz and its .sha256. Inside, at
# the paths the Dockerfiles name (their build context is the tree's root):
#
#   sidecars/harness-*/            every engine's Dockerfile
#   cli/cseries_sidecar.py         the sidecar runtime
#   system-apps/_harness_runtime/  the engines' shared runtime
#   system-apps/claude-code-harness/
#   LICENSE, VERSION
#
# The same tree gives the same bytes: entries in name order, owner root,
# fixed modes, every time set to the epoch (by default the commit time of
# HEAD), and a gzip header with no name or time. A machine pins the sha256
# from the signed release manifest and refuses anything else.
set -euo pipefail

usage() {
    echo "usage: $0 --version X.Y.Z --out <dir> [--epoch SECONDS]" >&2
    exit 2
}

version="" out="" epoch=""
while [ $# -gt 0 ]; do
    case "$1" in
        --version) version=${2:-}; shift 2 ;;
        --out) out=${2:-}; shift 2 ;;
        --epoch) epoch=${2:-}; shift 2 ;;
        *) usage ;;
    esac
done
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "--version must be X.Y.Z (got '$version')" >&2; exit 2; }
[ -n "$out" ] || usage

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
if [ -z "$epoch" ]; then
    epoch=$(git -C "$repo" log -1 --format=%ct 2>/dev/null) || {
        echo "not a git checkout: pass --epoch" >&2; exit 2; }
fi
[[ "$epoch" =~ ^[0-9]+$ ]] || { echo "--epoch must be seconds since 1970" >&2; exit 2; }

mkdir -p "$out"
out=$(cd "$out" && pwd)
name="cseries-harness-src-$version.tar.gz"

python3 -I - "$repo" "$version" "$epoch" "$out/$name" <<'EOF'
import gzip, io, os, sys, tarfile

repo, version, epoch, dest = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
TREES = ["system-apps/_harness_runtime", "system-apps/claude-code-harness"]
FILES = ["cli/cseries_sidecar.py", "LICENSE"]
sidecars = os.path.join(repo, "sidecars")
TREES += sorted(f"sidecars/{d}" for d in os.listdir(sidecars) if d.startswith("harness-"))

files = {}
def add(arc, src):
    with open(src, "rb") as f:
        files[arc] = (f.read(), os.stat(src).st_mode & 0o111)

for rel in FILES:
    add(rel, os.path.join(repo, rel))
for tree in TREES:
    top = os.path.join(repo, tree)
    if not os.path.isdir(top):
        sys.exit(f"build-harness-src: {tree} is missing")
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in sorted(filenames):
            if fn.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                sys.exit(f"build-harness-src: {os.path.relpath(full, repo)} is a link")
            add(os.path.relpath(full, repo), full)
files["VERSION"] = (f"{version}\n".encode(), 0)

dirs = set()
for arc in files:
    parts = arc.split("/")[:-1]
    for i in range(1, len(parts) + 1):
        dirs.add("/".join(parts[:i]))

def info(arcname, isdir, size=0, mode=0o644):
    ti = tarfile.TarInfo(arcname)
    ti.type = tarfile.DIRTYPE if isdir else tarfile.REGTYPE
    ti.mode = 0o755 if isdir else mode
    ti.size = size
    ti.mtime = epoch
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    return ti

buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
    for arc in sorted(dirs | set(files)):
        if arc in dirs:
            t.addfile(info(arc, True))
        else:
            data, x = files[arc]
            t.addfile(info(arc, False, len(data), 0o755 if x else 0o644), io.BytesIO(data))
with open(dest, "wb") as raw:
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as gz:
        gz.write(buf.getvalue())
EOF

(cd "$out" && sha256sum "$name" > "$name.sha256")
echo "$out/$name"
cat "$out/$name.sha256"
