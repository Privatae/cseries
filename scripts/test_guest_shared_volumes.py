#!/usr/bin/env python3
# gate-scope: whole-tree — every Quadlet unit and drop-in of the guest and the cloud overlay
"""A volume that more than one of the guest's containers mounts is made once,
before any of them starts.

podman makes a named volume a container mounts as that container starts, if
it is not there yet. Two containers that mount the same one and start
together race to make it, and the second fails with podman's exit 125
("creating named volume ...: volume with name ... already exists"); systemd
then starts it again. The door's first start on the v0.1.12 WSL guest did
exactly that over cseries_remote-tls, which the door, its certificate job and
(for the other three remote-access volumes) the Host's remote.conf drop-in
share.

So a volume more than one unit mounts is a Quadlet .volume unit, which makes
it with `podman volume create --ignore` before each unit that names it
(Quadlet gives every such unit Requires= and After= on the volume's service,
from a drop-in as from the unit itself), or every unit but the one that makes
it starts after that one. Over the guest's units (vm/wsl/rootfs, which the
Lima image copies), their drop-ins, and the remote.conf drop-in
`cseries-guest configure` writes:

  - every shared volume is a .volume unit for every unit that mounts it, and
    no unit names it by its own name; or its units are ordered, each after
    the one that makes it (the database's socket: the Host follows the
    database);
  - each .volume unit's VolumeName is the name the volume has always had (the
    compose project's, cseries_<name>), the four remote access shares among
    them, so an install that made them before these units keeps its data; it
    has no [Install], so only a unit that names it makes it;
  - every Volume= line in cseries-guest is one of the drop-in's read here, or
    a directory of the guest's own, so a new drop-in's volume is not missed;
  - the cloud overlay (vm/cloud/build-overlay.sh) ships every .volume unit
    that the units it reuses, or its own drop-ins, name.

Container-free: python3 scripts/test_guest_shared_volumes.py
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OVERLAY = os.path.join(ROOT, "vm/wsl/rootfs")
UNITS = os.path.join(OVERLAY, "etc/containers/systemd")
GUEST = os.path.join(OVERLAY, "usr/local/bin/cseries-guest")
CLOUD_BUILD = os.path.join(ROOT, "vm/cloud/build-overlay.sh")
CLOUD_OVERLAY = os.path.join(ROOT, "vm/cloud/overlay")
# The four volumes remote access shares, by the names every install that has
# run the door has them under.
REMOTE_SHARED = {"cseries_remote-tls", "cseries_remote-acme", "cseries_remote-door-key",
                 "cseries_remote-cert-status"}

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def parse(text, out=None):
    """{section: [(key, value), ...]}, added to `out`, as systemd reads a unit."""
    out = {} if out is None else out
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = out.setdefault(line[1:-1], [])
            continue
        k, _, v = line.partition("=")
        if section is not None:
            section.append((k.strip(), v.strip()))
    return out


def values(u, section, key):
    return [v for k, v in u.get(section, []) if k == key]


def words(u, section, key):
    return {w for v in values(u, section, key) for w in v.split()}


# The drop-in cseries-guest configure writes for the Host once it has the
# door's image (remote.conf), from cseries-guest itself, with a systemctl and
# a podman that only answer.
tmp = tempfile.mkdtemp(prefix="guest-shared-volumes-")
fake_bin = os.path.join(tmp, "bin")
os.makedirs(fake_bin)
for name, body in (("systemctl", '#!/bin/sh\ncase "$1" in show) echo loaded ;; esac\nexit 0\n'),
                   ("podman", "#!/bin/sh\nexit 0\n")):
    with open(os.path.join(fake_bin, name), "w") as f:
        f.write(body)
    os.chmod(os.path.join(fake_bin, name), 0o755)
digest = "@sha256:" + "a" * 64
g_root = os.path.join(tmp, "g")
os.makedirs(g_root)
r = subprocess.run(["sh", GUEST, "configure"],
                   input=(f"host=ghcr.io/privatae/cseries{digest}\npostgres=ghcr.io/privatae/cseries-postgres{digest}\n"
                          f"release=1.2.3\ndoor=ghcr.io/privatae/cseries-remote-door{digest}\n"),
                   env=dict(os.environ, CSERIES_GUEST_ROOT=g_root, PATH=fake_bin + os.pathsep + os.environ.get("PATH", "")),
                   capture_output=True, text=True, check=False)
remote_conf = os.path.join(g_root, "etc/containers/systemd/cseries-host.container.d/remote.conf")
check(r.returncode == 0 and os.path.isfile(remote_conf),
      "cseries-guest configure, given the door's image, writes the Host's remote.conf drop-in", r.stderr[-800:])
written = {"cseries-host.container": [remote_conf] if os.path.isfile(remote_conf) else []}

# Every container unit in the guest, read as Quadlet reads it: the unit, then
# its drop-ins (the shipped ones and configure's) in name order.
containers = {}
for fn in sorted(os.listdir(UNITS)):
    if not fn.endswith(".container"):
        continue
    d = os.path.join(UNITS, fn + ".d")
    drops = [os.path.join(d, x) for x in os.listdir(d) if x.endswith(".conf")] if os.path.isdir(d) else []
    u = parse(read(os.path.join(UNITS, fn)))
    for p in sorted(drops + written.get(fn, []), key=os.path.basename):
        parse(read(p), u)
    containers[fn] = u
check({"cseries-host.container", "cseries-postgres.container", "cseries-remote-door.container",
       "cseries-remote-cert.container"} <= set(containers),
      "the Host's, the database's, the door's and the certificate job's units are read", sorted(containers))

volume_units = {fn: parse(read(os.path.join(UNITS, fn))) for fn in sorted(os.listdir(UNITS)) if fn.endswith(".volume")}


def volume_name(fn):
    return (values(volume_units.get(fn, {}), "Volume", "VolumeName") or [""])[-1]


# Who mounts what: {volume's name in podman: [(unit, as written)]}.
users = {}
for fn, u in containers.items():
    for v in values(u, "Container", "Volume"):
        src = v.split(":", 1)[0]
        if not src or src.startswith(("/", ".", "%")):
            continue  # a directory of the guest's, or an anonymous volume
        name = volume_name(src) if src.endswith(".volume") else src
        if src.endswith(".volume"):
            check(bool(name), f"{fn}'s {src} is a volume unit the guest ships, with a VolumeName", v)
        users.setdefault(name or src, []).append((fn, src))

# ── each .volume unit ───────────────────────────────────────────────────────
for fn, vu in volume_units.items():
    want = "cseries_" + fn[len("cseries-"):-len(".volume")] if fn.startswith("cseries-") else None
    check(want is not None and values(vu, "Volume", "VolumeName") == [want],
          f"{fn} names the volume {want}, the name it has always had, so an install that made it keeps it",
          values(vu, "Volume", "VolumeName"))
    check("Install" not in vu, f"{fn} has no [Install]: only a unit that names it makes it")
made_by_unit = {volume_name(fn) for fn in volume_units}
check(REMOTE_SHARED <= made_by_unit,
      "the four volumes remote access shares (tls, acme, door key, certificate status) are each made by a .volume unit",
      sorted(REMOTE_SHARED - made_by_unit))

# ── each volume more than one unit mounts ───────────────────────────────────
shared = {name: refs for name, refs in users.items() if len({fn for fn, _ in refs}) > 1}
check(REMOTE_SHARED <= set(shared), "the door, its certificate job and the Host share the four remote volumes",
      sorted(REMOTE_SHARED - set(shared)))
for name, refs in sorted(shared.items()):
    units = sorted({fn for fn, _ in refs})
    raw = sorted({fn for fn, src in refs if not src.endswith(".volume")})
    if not raw:
        check(True, f"{name} ({', '.join(units)}): made by its .volume unit before any of them starts")
        continue
    if len(raw) < len(units):
        check(False, f"{name} is made by its .volume unit, and every unit that mounts it names that unit",
              f"named by its own name in {', '.join(raw)}")
        continue
    # All by name: fine only when every unit but one starts after that one,
    # which makes it first.
    def service(fn):
        return fn[:-len(".container")] + ".service"
    makers = [m for m in units if all(
        service(m) in words(containers[o], "Unit", "After") and
        service(m) in (words(containers[o], "Unit", "Wants") | words(containers[o], "Unit", "Requires"))
        for o in units if o != m)]
    check(bool(makers), f"{name} ({', '.join(units)}): made before they race for it, by a .volume unit or by "
          f"every other unit starting after the one that makes it",
          "no .volume unit, and the units start independently")
    if makers:
        others = [o for o in units if o != makers[0]]
        print(f"      ({name}: {', '.join(others)} start{'s' if len(others) == 1 else ''} after {makers[0]})")

# No unit, drop-in or cseries-guest names a volume a .volume unit makes (or
# one of the four remote access shares) by its own name.
texts = {fn: read(os.path.join(UNITS, fn)) for fn in containers}
for fn in containers:
    d = os.path.join(UNITS, fn + ".d")
    for x in (os.listdir(d) if os.path.isdir(d) else []):
        texts[f"{fn}.d/{x}"] = read(os.path.join(d, x))
guest_text = read(GUEST)
texts["cseries-guest"] = guest_text
for where, text in texts.items():
    raw_refs = [ln for ln in re.findall(r"^\s*Volume=([^:\s]+):", text, re.M) if ln in made_by_unit | REMOTE_SHARED]
    check(not raw_refs, f"{where} names no shared volume by its own name, only by its .volume unit", raw_refs)

# Every Volume= cseries-guest writes is one this check read, or a directory.
read_lines = {f"Volume={v}" for v in values(parse(read(remote_conf)), "Container", "Volume")}
for ln in re.findall(r"^\s*(Volume=\S+)", guest_text, re.M):
    check(ln in read_lines or ln.startswith("Volume=/"),
          f"cseries-guest's {ln} is in a drop-in this check reads, or a directory of the guest's")

# ── the cloud overlay ───────────────────────────────────────────────────────
build = read(CLOUD_BUILD)
reused = re.findall(r'^\s*"(etc/containers/systemd/[^"]+)",', build, re.M)
check(bool(reused), "the cloud overlay's reused guest units are read from build-overlay.sh")
shipped = {os.path.basename(p) for p in reused}
cloud_units = [os.path.join(OVERLAY, p) for p in reused]
for dirpath, _dirs, files in os.walk(os.path.join(CLOUD_OVERLAY, "etc/containers/systemd")):
    for x in files:
        cloud_units.append(os.path.join(dirpath, x))
        shipped.add(x)
for p in cloud_units:
    for src in re.findall(r"^\s*Volume=([^:\s]+\.volume):", read(p), re.M):
        check(src in shipped, f"the cloud overlay ships {src}, which its {os.path.basename(p)} names")
print(f"      (the cloud overlay reuses {', '.join(sorted(os.path.basename(p) for p in reused))})")

print()
print("FAIL" if failed else "all guest shared volume checks passed")
sys.exit(failed)
