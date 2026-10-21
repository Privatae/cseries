#!/usr/bin/env bash
# Builds the C-Series Cloud overlay: every file a cloud box installs on top of
# Debian 13, plus bootstrap.sh, in one tarball.
#
#   vm/cloud/build-overlay.sh --version X.Y.Z --out <dir> [--epoch SECONDS]
#
# Writes <dir>/cseries-cloud-overlay-X.Y.Z.tar.gz and its .sha256. Inside:
#
#   VERSION         X.Y.Z
#   bootstrap.sh    vm/cloud/bootstrap.sh
#   root/           the files, under / on the box: vm/cloud/overlay/, and from
#                   the WSL guest the parts a cloud box runs unchanged (both
#                   quadlets, cseries.network, containers.conf.d/cseries.conf,
#                   cseries-guest), the local runtime (vm/runtime: its code,
#                   units, network and plain proxy's config), both seccomp
#                   profiles and etc/tmpfiles.d/privatae-host.conf
#
# The same tree gives the same bytes: entries in name order, owner root,
# fixed modes (0755 for directories and programs, 0644 for the rest), every
# time set to the epoch (by default the commit time of HEAD), and a gzip
# header with no name or time. Python's standard library builds it, so
# nothing else is needed.
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
name="cseries-cloud-overlay-$version.tar.gz"

python3 -I - "$repo" "$version" "$epoch" "$out/$name" <<'EOF'
import gzip, io, os, sys, tarfile

repo, version, epoch, dest = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
wsl = os.path.join(repo, "vm/wsl/rootfs")
REUSED = [
    "etc/containers/containers.conf.d/cseries.conf",
    "etc/containers/systemd/cseries-host.container",
    "etc/containers/systemd/cseries-postgres.container",
    "etc/containers/systemd/cseries.network",
    "usr/local/bin/cseries-guest",
]
EXEC_DIRS = ("root/usr/local/bin/", "root/usr/local/sbin/")

files = {}  # archive name -> bytes
def add(arc, src):
    if arc in files:
        sys.exit(f"build-overlay: {arc} comes from two places")
    with open(src, "rb") as f:
        files[arc] = f.read()

overlay = os.path.join(repo, "vm/cloud/overlay")
for dirpath, dirnames, filenames in os.walk(overlay):
    for fn in filenames:
        if fn.endswith((".pyc", ".pyo")) or "__pycache__" in dirpath:
            continue
        full = os.path.join(dirpath, fn)
        add("root/" + os.path.relpath(full, overlay), full)
for rel in REUSED:
    add("root/" + rel, os.path.join(wsl, rel))
add("root/usr/share/cseries/seccomp-personal.json", os.path.join(repo, "seccomp-personal.json"))
add("root/usr/share/cseries/seccomp-browser.json", os.path.join(repo, "seccomp-browser.json"))
# The local runtime, the same files the WSL guest installs (vm/wsl/customize.sh).
rt = os.path.join(repo, "vm/runtime")
lib = os.path.join(rt, "lib/cseries_runtime")
for fn in sorted(os.listdir(lib)):
    if fn.endswith(".py"):
        add("root/usr/local/lib/cseries-runtime/cseries_runtime/" + fn, os.path.join(lib, fn))
add("root/usr/local/sbin/cseries-runtime", os.path.join(rt, "sbin/cseries-runtime"))
add("root/usr/local/sbin/cseries-build-user", os.path.join(rt, "sbin/cseries-build-user"))
for fn in sorted(os.listdir(os.path.join(rt, "systemd"))):
    add("root/etc/systemd/system/" + fn, os.path.join(rt, "systemd", fn))
add("root/etc/containers/systemd/cseries-apps.network", os.path.join(rt, "containers/cseries-apps.network"))
add("root/etc/cseries-runtime/squid.conf", os.path.join(rt, "squid.conf"))
add("root/etc/tmpfiles.d/privatae-host.conf", os.path.join(repo, "etc/tmpfiles.d/privatae-host.conf"))
files["root/etc/cseries/guest-version"] = f"cloud-{version}\n".encode()
add("bootstrap.sh", os.path.join(repo, "vm/cloud/bootstrap.sh"))
files["VERSION"] = f"{version}\n".encode()

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
            data = files[arc]
            executable = arc.startswith(EXEC_DIRS) or arc == "bootstrap.sh"
            t.addfile(info(arc, False, len(data), 0o755 if executable else 0o644), io.BytesIO(data))
with open(dest, "wb") as raw:
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as gz:
        gz.write(buf.getvalue())
EOF

(cd "$out" && sha256sum "$name" > "$name.sha256")
echo "$out/$name"
cat "$out/$name.sha256"
