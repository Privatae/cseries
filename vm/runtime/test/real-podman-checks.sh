#!/usr/bin/env bash
# The local runtime against the machine's real podman and systemd, on a guest
# where the runtime runs and the Host is up (a Lima VM in vm/lima/test/e2e.sh,
# a cloud box in vm/cloud/test/run-vm-test.sh). As root, in the guest:
#
#   bash -s -- <an image the guest holds> < vm/runtime/test/real-podman-checks.sh
#
#   * the containers' slice: cseries.slice is loaded with CPUWeight 20 and no
#     CPU ceiling, a container and an engine build's RUN step started with
#     --cgroup-parent cseries.slice run in it, and the kernel holds the weight;
#   * an app's kept data out and back: a data volume as the runtime makes one,
#     owned by an id range other than any this machine gave, is exported by
#     the runtime (podman volume export) to the Host's own client inside the
#     Host's container (the peer the runtime admits), then imported back
#     (podman volume import): every file and link as it was, every owner in
#     the range the runtime keeps for the app, a root-owned file given the
#     range's nobody, its set-user bit gone;
#   * a crafted tar sent through the runtime's service as an app's data (a
#     link it made and a file written through it, a name that climbs out, a
#     link above the volume) is refused 400, nothing is written outside the
#     volume, and the app's data is as it was.
#
# Two checks need podman alone, and run first (PODMAN_ONLY=1 runs only these,
# on any guest with podman and this tree's vm/runtime/lib at RT_LIB):
#
#   * the runtime swaps a restored app's data in with one renameat2
#     RENAME_EXCHANGE of two podman volume folders (server.exchange_dirs):
#     on this machine's volume storage each folder then holds the other's
#     tree, and podman's own export reads the swapped one;
#   * podman's own rule for a link in a volume tar, which the runtime's tar
#     gate (server.gate_tar) refuses first: d/l -> ../x stays in the volume
#     and is accepted, l -> ../x leads above it and podman refuses the whole
#     tar (exit 125) writing nothing beside the volume; the gate says the same
#     of both.
#
# Invented names and data only. Leaves nothing behind but the runtime's
# record of the test app's id range.
set -euo pipefail
PODMAN_ONLY=${PODMAN_ONLY:-}
if [ -n "$PODMAN_ONLY" ]; then img=${1:-}; else img=${1:?usage: real-podman-checks.sh <image>}; fi
RT_LIB=${RT_LIB:-/usr/local/lib/cseries-runtime}
fail() { echo "FAIL: $*"; exit 1; }
ok() { echo "ok    $*"; }
work=$(mktemp -d /var/tmp/rt-real-checks.XXXXXX)
app=e2e-appdata
vol=cseries-app-$app-data
xa=cseries-rt-check-xa
xb=cseries-rt-check-xb
lk=cseries-rt-check-links
cleanup() {
    podman volume rm --force "$vol" "$xa" "$xb" "$lk-in" "$lk-out" >/dev/null 2>&1 || true
    podman image rm --force localhost/rt-real-checks:slice >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT
[ -f "$RT_LIB/cseries_runtime/server.py" ] || fail "the runtime's code is not at $RT_LIB (set RT_LIB)"

echo "== two podman volume folders exchanged in one rename (the runtime's exchange_dirs)"
podman volume create "$xa" >/dev/null
podman volume create "$xb" >/dev/null
ma=$(podman volume inspect --format '{{.Mountpoint}}' "$xa")
mb=$(podman volume inspect --format '{{.Mountpoint}}' "$xb")
echo "invented first" > "$ma/which"
mkdir "$ma/sub"
echo "invented deep" > "$ma/sub/f"
echo "invented second" > "$mb/which"
ia=$(stat -c %i "$ma")
python3 - "$RT_LIB" "$ma" "$mb" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from cseries_runtime.server import exchange_dirs
exchange_dirs(sys.argv[2], sys.argv[3])
PY
[ "$(cat "$ma/which")" = "invented second" ] && [ "$(cat "$mb/which")" = "invented first" ] \
    && [ "$(cat "$mb/sub/f")" = "invented deep" ] && [ ! -e "$ma/sub" ] || fail "the folders did not trade trees"
[ "$(stat -c %i "$mb")" = "$ia" ] || fail "the first folder's tree is not at the second name"
[ "$(podman volume inspect --format '{{.Mountpoint}}' "$xa")" = "$ma" ] || fail "podman moved the volume"
got=$(podman volume export "$xa" | python3 -c 'import sys, tarfile
t = tarfile.open(fileobj=sys.stdin.buffer, mode="r|")
print("".join(t.extractfile(m).read().decode().strip() for m in t if m.isfile() and m.name.lstrip("./") == "which"))')
[ "$got" = "invented second" ] || fail "podman's export of $xa reads '$got'"
ok "on $(stat -f -c %T "$ma") each folder holds the other's tree, and podman's export reads the swapped one"

echo "== podman's own rule for a link in a volume tar, and the runtime's gate"
python3 - "$work" <<'PY'
import io, sys, tarfile
def tar_of(path, members):
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as t:
        for kind, name, val in members:
            ti = tarfile.TarInfo(name)
            if kind == "dir":
                ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                t.addfile(ti)
            elif kind == "link":
                ti.type, ti.linkname = tarfile.SYMTYPE, val
                t.addfile(ti)
            else:
                ti.size, ti.mode = len(val), 0o644
                t.addfile(ti, io.BytesIO(val))
tar_of(sys.argv[1] + "/in.tar", [("dir", "./d", None), ("link", "./d/l", "../x"), ("file", "./x", b"invented\n")])
tar_of(sys.argv[1] + "/out.tar", [("link", "./l", "../x"), ("file", "./y", b"invented\n")])
PY
podman volume create "$lk-in" >/dev/null
podman volume create "$lk-out" >/dev/null
podman volume import "$lk-in" "$work/in.tar" || fail "podman refused d/l -> ../x (rc $?)"
mi=$(podman volume inspect --format '{{.Mountpoint}}' "$lk-in")
[ "$(readlink "$mi/d/l")" = ../x ] && [ "$(cat "$mi/d/l")" = invented ] || fail "d/l is not the link to ../x"
ok "podman accepts d/l -> ../x: it stays in the volume"
mo=$(podman volume inspect --format '{{.Mountpoint}}' "$lk-out")
set +e
podman volume import "$lk-out" "$work/out.tar" > "$work/out.said" 2>&1
rc=$?
set -e
[ "$rc" = 125 ] || { cat "$work/out.said"; fail "podman's import of l -> ../x exited $rc, not 125"; }
[ ! -e "$(dirname "$mo")/x" ] && [ ! -L "$mo/l" ] || fail "the refused import wrote something"
ok "podman refuses l -> ../x, exit 125, writing nothing beside the volume ($(tail -1 "$work/out.said" | cut -c1-120))"
python3 - "$RT_LIB" "$work" <<'PY'
import io, sys
sys.path.insert(0, sys.argv[1])
from cseries_runtime.server import TarRefused, gate_tar
def gate(name):
    with open(sys.argv[2] + "/" + name, "rb") as f:
        try:
            gate_tar(f, io.BytesIO())
            return None
        except TarRefused as e:
            return str(e)
assert gate("in.tar") is None, gate("in.tar")
why = gate("out.tar")
assert why and "leads out" in why, why
print("ok    the runtime's gate passes d/l -> ../x and refuses l -> ../x first: " + why[:100])
PY
if [ -n "$PODMAN_ONLY" ]; then echo "PASS (podman alone)"; exit 0; fi

echo "== the containers' slice"
w=$(systemctl show -p CPUWeight --value cseries.slice)
q=$(systemctl show -p CPUQuotaPerSecUSec --value cseries.slice)
[ "$w" = 20 ] || fail "cseries.slice CPUWeight is '$w', not 20"
[ "$q" = infinity ] || fail "cseries.slice has a CPU ceiling ($q)"
ok "cseries.slice: CPUWeight 20, no CPU ceiling"
# --cgroupns host only so the probe sees its cgroup's full path; inside its
# own cgroup namespace it would read "0::/".
cg=$(podman run --rm --network none --cgroupns host --cgroup-parent cseries.slice --entrypoint cat "$img" \
    /proc/self/cgroup)
case "$cg" in *"/cseries.slice/"*) ok "a container started with --cgroup-parent cseries.slice runs in it ($cg)" ;;
    *) fail "the container ran in $cg" ;; esac
[ "$(cat /sys/fs/cgroup/cseries.slice/cpu.weight 2>/dev/null)" = 20 ] || \
    fail "the kernel's cpu.weight for cseries.slice is '$(cat /sys/fs/cgroup/cseries.slice/cpu.weight 2>&1)'"
ok "the kernel holds cpu.weight 20 for it"
printf 'FROM %s\nUSER root\nRUN cat /proc/self/cgroup > /slice-check\n' "$img" > "$work/Containerfile"
podman build -q --network none --cgroupns host --cgroup-parent cseries.slice -t localhost/rt-real-checks:slice \
    -f "$work/Containerfile" "$work" >/dev/null
bcg=$(podman run --rm --network none --entrypoint cat localhost/rt-real-checks:slice /slice-check)
case "$bcg" in *"/cseries.slice/"*) ok "an engine build's RUN step runs in it ($bcg)" ;;
    *) fail "the build's RUN step ran in $bcg" ;; esac

echo "== an app's kept data out of the runtime and back, by podman"
podman volume create --label cseries.runtime.scope=keep --label "cseries.runtime.name=app-$app" "$vol" >/dev/null
m=$(podman volume inspect --format '{{.Mountpoint}}' "$vol")
old=$((200000 + 4000 * 65536))          # a range this machine never gives (slots count from 1)
mkdir -p "$m/sub"
head -c 3000000 /dev/urandom > "$m/sub/blob"
echo "invented note" > "$m/a.txt"
ln -s a.txt "$m/link"
chown -hR $((old + 1000)):$((old + 1000)) "$m"
echo x > "$m/stray"
chown 0:0 "$m/stray"
chmod 4755 "$m/stray"
(cd "$m" && find . -type f ! -name stray -print0 | sort -z | xargs -0 sha256sum) > "$work/before"
podman exec -i cseries-host python3 - "$app" "$old" > "$work/client.json" <<'PY'
import io, json, sys
sys.path[:0] = ["/app/brain", "/app"]
from server.applications import local_runtime as LR
app, base = sys.argv[1], int(sys.argv[2])
buf = io.BytesIO()
head = LR.export_app_data(app, buf.write)
n = buf.tell()
buf.seek(0)
got = LR.import_app_data(app, buf, n, base)
print(json.dumps({"export": head, "import": got}))
PY
cat "$work/client.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["export"]["bytes"] > 3000000 and d["import"]["imported"], d' \
    "$work/client.json" || fail "the export or the import did not land"
ok "the Host's client exported the volume and imported it back through the runtime"
m=$(podman volume inspect --format '{{.Mountpoint}}' "$vol")
(cd "$m" && find . -type f ! -name stray -print0 | sort -z | xargs -0 sha256sum) > "$work/after"
cmp -s "$work/before" "$work/after" || { diff "$work/before" "$work/after" | head; fail "the files changed"; }
[ "$(readlink "$m/link")" = a.txt ] || fail "the link is not a link to a.txt"
ok "every file as it was, the link a link"
slot=$(python3 -c 'import json,sys; print(json.load(open("/var/lib/cseries-runtime/alloc.json"))["names"]["app-'"$app"'"]["slot"])')
new=$((200000 + slot * 65536))
bad=$(find "$m" -printf '%U %G\n' | awk -v lo="$new" -v hi=$((new + 65536)) '$1 < lo || $1 >= hi || $2 < lo || $2 >= hi' | head -3)
[ -z "$bad" ] || fail "owners outside the app's range ($new..): $bad"
[ "$(stat -c %u "$m/a.txt")" = $((new + 1000)) ] || fail "a.txt is owned by $(stat -c %u "$m/a.txt"), not $((new + 1000))"
[ "$(stat -c %u "$m/stray")" = $((new + 65534)) ] || fail "the root-owned file is owned by $(stat -c %u "$m/stray")"
[ $(( 0$(stat -c %a "$m/stray") & 04000 )) = 0 ] || fail "the set-user bit survived"
ok "every owner in the range the runtime keeps for the app (slot $slot); root's file is its nobody, no set-user bit"

echo "== a crafted tar sent through the runtime as the app's data is refused 400, nothing written outside"
outside="$work/outside"
mkdir "$outside"
vols=$(dirname "$(dirname "$m")")
podman exec -i cseries-host python3 - "$app" "$outside" "$old" > "$work/crafted.json" <<'PY'
import io, json, sys, tarfile
sys.path[:0] = ["/app/brain", "/app"]
from server.applications import local_runtime as LR
app, outside, base = sys.argv[1], sys.argv[2], int(sys.argv[3])
def tar_of(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        for kind, name, val in members:
            ti = tarfile.TarInfo(name)
            if kind == "dir":
                ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                t.addfile(ti)
            elif kind == "link":
                ti.type, ti.linkname = tarfile.SYMTYPE, val
                t.addfile(ti)
            else:
                ti.size, ti.mode = len(val), 0o644
                t.addfile(ti, io.BytesIO(val))
    return buf.getvalue()
cases = {
    "a link it made, and a file written through it": [("dir", ".", None), ("link", "./out", outside),
                                                      ("file", "./out/planted", b"invented\n")],
    "a name that climbs out": [("dir", ".", None), ("file", "./../planted", b"invented\n")],
    "a link above the volume": [("dir", ".", None), ("link", "./up", "../../../planted"),
                                ("file", "./x", b"invented\n")],
}
said = {}
for label, members in cases.items():
    blob = tar_of(members)
    try:
        LR.import_app_data(app, io.BytesIO(blob), len(blob), base)
        said[label] = {"code": 200}
    except LR.RuntimeRefusal as e:
        said[label] = {"code": e.code, "text": e.text[:200]}
print(json.dumps(said))
PY
cat "$work/crafted.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert len(d) == 3 and all(v["code"] == 400 for v in d.values()), d' \
    "$work/crafted.json" || fail "a crafted tar was not refused 400"
ok "each crafted tar is refused 400, with the reason"
[ -z "$(ls -A "$outside")" ] || fail "something was written through the link: $(ls -A "$outside")"
[ -z "$(find "$vols" -maxdepth 3 -name planted -print -quit)" ] || fail "a planted file is in podman's volumes"
m=$(podman volume inspect --format '{{.Mountpoint}}' "$vol")
(cd "$m" && find . -type f ! -name stray -print0 | sort -z | xargs -0 sha256sum) > "$work/after-crafted"
cmp -s "$work/after" "$work/after-crafted" || fail "the app's data changed"
ok "nothing was written outside the volume, and the app's data is as it was"
echo "PASS"
