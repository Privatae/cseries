#!/usr/bin/env python3
"""Remote access on a C-Series Cloud box, as the box's files set it up.

A cloud box keeps its own front door (Caddy on 443, cseries-host.container.d/
cloud.conf) and can also turn on remote access the way a member's own machine
does: the remote-access door and its certificate job (vm/wsl/rootfs's
quadlets, reused unchanged), which the Host's tunnel hands browsers to from
the network's relay. The two doors must not collide in the Host's unit. This
test builds the overlay, installs it into a temporary root as bootstrap does,
runs cseries-guest configure there with a door image (as bootstrap and the
updater do once the release pins one), and reads the Host's unit and its
drop-ins in name order, as Quadlet does (the last value for a variable wins):

  1. The overlay ships remote access's units, unchanged from the guest's, and
     everything they require exists on a box: the remote firewall, and the
     name the guest's door units require for the machine wall, which on a box
     stands for door.nft's firewall. Every cseries-remote-* unit waits for the
     vault (one prefix drop-in), and cseries-unlocked.target starts the door
     and its job after an unlock.
  2. With the door configured the Host has both doors' settings: the front
     door's key (CSERIES_DOOR_SECRET_FILE) and the remote door's
     (CSERIES_REMOTE_DOOR_SECRET_FILE), mounted at different places, no two
     mounts on one path in the container, /run/cseries-door still the box's
     (the machine directory and the pairing helper), and CSERIES_TRUSTED_PROXIES
     naming Caddy's address (the same value cloud.conf trusts, from
     /etc/cseries/front-proxies) and the remote door's /32.
  3. A front-proxies file holding anything but single addresses (a network,
     however written, or text) stops configure before it writes a file.
  4. The release's manifest pins the door's image; bootstrap and the updater
     hand it to configure as door=, and a rollback hands back the previous one.

Container-free: python3 and sh; no root, no network.

    python3 scripts/test_cloud_box_remote_access.py      (from the repo root)
"""
import ast
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "vm/cloud/overlay/usr/local/lib/cseries-cloud"))
import cseries_cloud as cc  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


TMP = tempfile.mkdtemp(prefix="cloud-remote-")
UNITS_REL = "etc/containers/systemd"


def quadlet(units, name, extra=()):
    """[(section, key, value)] for a unit and its .d/*.conf drop-ins in name
    order, as Quadlet reads them."""
    d = os.path.join(units, name) + ".d"
    drops = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".conf")] if os.path.isdir(d) else []
    out, section = [], None
    for path in [os.path.join(units, name)] + sorted(drops, key=os.path.basename):
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1]
                    continue
                k, _, v = line.partition("=")
                out.append((section, k.strip(), v.strip()))
    return out


def values(unit, section, key):
    return [v for s, k, v in unit if s == section and k == key]


def env_of(unit):
    env = {}
    for v in values(unit, "Container", "Environment"):
        k, _, val = v.partition("=")
        env[k] = val
    return env


try:
    # ── the overlay, installed as bootstrap installs it ──
    out = os.path.join(TMP, "out")
    r = subprocess.run(["bash", os.path.join(ROOT, "vm/cloud/build-overlay.sh"), "--version", "1.2.3",
                        "--out", out, "--epoch", "1760000000"], capture_output=True, text=True, check=False)
    check(r.returncode == 0, "the overlay builds", r.stderr[-300:])
    unpacked = os.path.join(TMP, "unpacked")
    with tarfile.open(os.path.join(out, "cseries-cloud-overlay-1.2.3.tar.gz")) as t:
        t.extractall(unpacked, filter="data")
    box = os.path.join(TMP, "box")
    paths = cc.Paths(box)
    cfg = {"name": "orchard", "address_id": "k7x2m9", "zone": "cloud.privatae.ai", "reboot_window": "04:00"}
    cc.apply_overlay(os.path.join(unpacked, "root"), paths,
                     cc.template_values(cfg, {"privatae-host": 990, "cseries": 991}, vault=True))
    units = paths.p(UNITS_REL)
    sysd = paths.p("etc/systemd/system")

    # ── 1. the units, and what they require ──
    remote_units = {
        "cseries-remote-door.container": "cseries-remote-door.service",
        "cseries-remote-cert.container": "cseries-remote-cert.service",
        "cseries-remote-acme.volume": "cseries-remote-acme-volume.service",
        "cseries-remote-cert-status.volume": "cseries-remote-cert-status-volume.service",
        "cseries-remote-door-key.volume": "cseries-remote-door-key-volume.service",
        "cseries-remote-tls.volume": "cseries-remote-tls-volume.service",
        "cseries-remote.network": "cseries-remote-network.service",
    }
    for src in remote_units:
        with open(os.path.join(ROOT, "vm/wsl/rootfs", UNITS_REL, src), "rb") as a, \
                open(os.path.join(units, src), "rb") as b:
            check(a.read() == b.read(), f"{src} is on the box, the guest's unchanged")
    door_u = quadlet(units, "cseries-remote-door.container")
    cert_u = quadlet(units, "cseries-remote-cert.container")
    on_box = set(os.listdir(sysd))
    for name, u in (("door", door_u), ("certificate job", cert_u)):
        req = set(" ".join(values(u, "Unit", "Requires")).split())
        missing = sorted(x for x in req if x not in on_box)
        check(req and not missing, f"every unit the {name} requires is on the box", (sorted(req), missing))
    with open(os.path.join(sysd, "cseries-machine-firewall.service")) as f:
        shim = f.read()
    check(re.search(r"^Requires=cseries-firewall\.service$", shim, re.M) and
          re.search(r"^After=cseries-firewall\.service$", shim, re.M) and "[Install]" not in shim,
          "on a box the guest's machine-wall name stands for door.nft's firewall, which it requires; "
          "nothing enables it")
    vault = os.path.join(sysd, "cseries-remote-.service.d", "vault.conf")
    with open(vault) as f:
        vtext = f.read()
    check("Requires=cseries-vault.service" in vtext and "After=cseries-vault.service" in vtext,
          "one prefix drop-in holds every cseries-remote-* unit to the vault")
    # systemd gives a unit the drop-ins of each prefix of its name cut after a
    # dash (systemd.unit(5), "foo-.service.d"): every generated unit's name
    # has this one's prefix, and nothing else of the box's does.
    gen = sorted(remote_units.values()) + ["cseries-remote-firewall.service"]
    check(all(n.startswith("cseries-remote-") and n.endswith(".service") for n in gen) and
          not [n for n in on_box if n.startswith("cseries-remote-") and n.endswith(".service")
               and n != "cseries-remote-firewall.service"],
          "the prefix drop-in reaches remote access's units and no other unit of the box", gen)
    with open(os.path.join(sysd, "cseries-unlocked.target")) as f:
        target = f.read()
    wants = " ".join(re.findall(r"^Wants=(.*)$", target, re.M)).split()
    check({"cseries-host.service", "cseries-remote-door.service", "cseries-remote-cert.service"} <= set(wants),
          "the unlock starts the door and its certificate job with the Host", wants)
    with open(paths.p("etc/cseries/front-proxies")) as f:
        front = f.read().strip()
    cloud_env = env_of(quadlet(units, "cseries-host.container"))
    check(front == cc.TRUSTED_PROXY == cloud_env.get("CSERIES_TRUSTED_PROXIES"),
          "front-proxies is the address cloud.conf trusts for Caddy, from the same value", (front, cloud_env))

    # ── 2. configure with the door's image: both doors in the Host's unit ──
    fake_bin = os.path.join(TMP, "bin")
    os.makedirs(fake_bin)
    for n, body in (("systemctl", '#!/bin/sh\necho "systemctl $*" >> "$FAKE_LOG"\n'
                                  'case "$1" in show) echo loaded ;; esac\nexit 0\n'),
                    ("podman", '#!/bin/sh\necho "podman $*" >> "$FAKE_LOG"\nexit 0\n'),
                    # The local runtime's group, which bootstrap makes on a box.
                    ("getent", '#!/bin/sh\n[ "$*" = "group cseries-runtime" ] && echo "cseries-runtime:x:992:"\n')):
        with open(os.path.join(fake_bin, n), "w") as f:
            f.write(body)
        os.chmod(os.path.join(fake_bin, n), 0o755)

    def guest(root, stdin):
        env = dict(os.environ, CSERIES_GUEST_ROOT=root, FAKE_LOG=os.path.join(root, "calls.log"),
                   PATH=fake_bin + os.pathsep + os.environ.get("PATH", ""))
        return subprocess.run(["sh", os.path.join(root, "usr/local/bin/cseries-guest"), "configure"],
                              input=stdin, env=env, capture_output=True, text=True, check=False)

    D = "@sha256:" + "a" * 64
    door = "ghcr.io/privatae/cseries-remote-door@sha256:" + "9" * 64
    keys = f"host=ghcr.io/privatae/cseries{D}\npostgres=ghcr.io/privatae/cseries-postgres{D}\nrelease=1.2.3\n"
    r = guest(box, keys + f"door={door}\n")
    check(r.returncode == 0 and "changed=1" in r.stdout, "configure takes the door's image on the box",
          (r.returncode, r.stdout, r.stderr))
    host = quadlet(units, "cseries-host.container")
    env = env_of(host)
    check(env.get("CSERIES_TRUSTED_PROXIES") == f"{cc.TRUSTED_PROXY},10.89.79.3/32",
          "the Host trusts Caddy's address and the remote door's /32, both", env.get("CSERIES_TRUSTED_PROXIES"))
    check(env.get("CSERIES_DOOR_SECRET_FILE") == "/run/cseries-cloud/door-secret" and
          env.get("CSERIES_REMOTE_DOOR_SECRET_FILE") == "/run/cseries-remote-door/door-secret",
          "the front door's key and the remote door's are two settings, each its own file",
          {k: v for k, v in env.items() if "SECRET" in k})
    check(env.get("CSERIES_MACHINE_DIR") == "/run/cseries-door" and
          env.get("CSERIES_DOOR_HELPER") == "/run/cseries-door/helper.sock" and
          env.get("CSERIES_URL") == "https://orchard.k7x2m9.cloud.privatae.ai" and
          env.get("CSERIES_REMOTE_DOOR") == "10.89.79.3:8443",
          "the box's own settings stand beside remote access's", env)
    dests = [v.split(":")[1] for v in values(host, "Container", "Volume")]
    dup = sorted({d for d in dests if dests.count(d) > 1})
    check(not dup, "no two mounts in the Host's container share a path", dup)
    vols = {v.split(":")[1]: v.split(":")[0] for v in values(host, "Container", "Volume")}
    check(vols.get("/run/cseries-door") == "/run/cseries-door" and
          vols.get("/run/cseries-remote-door") == "cseries-remote-door-key.volume" and
          vols.get("/run/cseries-cloud/door-secret") == "/etc/cseries-cloud/door-secret",
          "/run/cseries-door stays the box's directory; the remote door's key volume is mounted beside it", vols)
    nets = values(host, "Container", "Network")
    check("cseries.network:ip=10.89.77.2" in nets and "cseries-remote.network:ip=10.89.79.2" in nets,
          "the Host keeps its fixed address and joins the remote network at its own", nets)
    for which in ("remote-door", "remote-cert"):
        with open(os.path.join(units, f"cseries-{which}.container.d", "image.conf")) as f:
            check(f.read().strip() == f"[Container]\nImage={door}", f"the {which} unit runs the door's image")

    # ── 3. a front-proxies file that is wrong ──
    bad = os.path.join(TMP, "bad")
    shutil.copytree(box, bad, symlinks=True)
    os.unlink(os.path.join(bad, UNITS_REL, "cseries-host.container.d", "remote.conf"))
    with open(os.path.join(bad, "etc/cseries/front-proxies"), "w") as f:
        f.write("10.89.77.1\nEnvironment=CSERIES_URL=https://elsewhere.example\n")
    with open(os.path.join(bad, UNITS_REL, "cseries-host.container.d", "image.conf")) as f:
        image_before = f.read()
    r = guest(bad, keys.replace("a" * 64, "b" * 64) + f"door={door}\n")
    with open(os.path.join(bad, UNITS_REL, "cseries-host.container.d", "image.conf")) as f:
        image_after = f.read()
    check(r.returncode != 0 and "front-proxies" in r.stderr and image_after == image_before and
          not os.path.exists(os.path.join(bad, UNITS_REL, "cseries-host.container.d", "remote.conf")),
          "a front-proxies file holding more than addresses stops configure before it writes any file",
          (r.returncode, r.stderr[-200:]))
    # Characters an address may hold, naming a network: every peer, or the
    # apps' and the remote network's, would be believed as a proxy.
    for wide in ("0.0.0.0/0", "::/0", "10.89.77.1/3", "10.89.77.1,10.0.0.0/8", "10.89.77.1/24",
                 "fd00::1/64", "10.89.77", "10.89.77.256", "1.2.3.4.5", "10.89.77.1/32/32"):
        with open(os.path.join(bad, "etc/cseries/front-proxies"), "w") as f:
            f.write(wide + "\n")
        r = guest(bad, keys.replace("a" * 64, "b" * 64) + f"door={door}\n")
        with open(os.path.join(bad, UNITS_REL, "cseries-host.container.d", "image.conf")) as f:
            image_after = f.read()
        check(r.returncode != 0 and "front-proxies" in r.stderr and image_after == image_before and
              not os.path.exists(os.path.join(bad, UNITS_REL, "cseries-host.container.d", "remote.conf")),
              f"a front-proxies file naming {wide!r}, not one address, stops configure before it writes any file",
              (r.returncode, r.stderr[-200:]))
    for one, want in (("10.89.77.1/32", "10.89.77.1/32,10.89.79.3/32"),
                      ("10.89.77.1, fd00::1/128", "10.89.77.1,fd00::1/128,10.89.79.3/32")):
        with open(os.path.join(bad, "etc/cseries/front-proxies"), "w") as f:
            f.write(one + "\n")
        r = guest(bad, keys + f"door={door}\n")
        env_bad = env_of(quadlet(os.path.join(bad, UNITS_REL), "cseries-host.container"))
        check(r.returncode == 0 and env_bad.get("CSERIES_TRUSTED_PROXIES") == want,
              f"front-proxies {one!r}, single addresses, is taken", (r.returncode, r.stderr[-200:], env_bad))
        os.unlink(os.path.join(bad, UNITS_REL, "cseries-host.container.d", "remote.conf"))

    # ── 4. the door's image from the release to configure ──
    m = {"version": "1.2.3",
         "images": {"host": {"ref": "ghcr.io/privatae/cseries:1.2.3", "digest": "sha256:" + "a" * 64},
                    "postgres": {"ref": "ghcr.io/privatae/cseries-postgres:1.2.3", "digest": "sha256:" + "a" * 64},
                    "door": {"ref": "ghcr.io/privatae/cseries-remote-door:1.2.3", "digest": "sha256:" + "9" * 64}},
         "overlay": {"url": "https://example.test/o.tar.gz", "sha256": "b" * 64},
         "bastion": {"min_version": "0.53.2"}}
    cc.validate_manifest(m)
    check(cc.door_lines(m["images"]) == "door=ghcr.io/privatae/cseries-remote-door:1.2.3@sha256:" + "9" * 64 + "\n",
          "the manifest's door image is configure's door= line")
    check(cc.door_lines_installed(paths) == f"door={door}\n",
          "the door image on the box is the line a rollback hands back")
    for prog, needle in (("cseries-cloud-bootstrap", 'cc.door_lines(manifest["images"])'),
                         ("cseries-cloud-update", 'cc.door_lines(manifest["images"])'),
                         ("cseries-cloud-update", "cc.door_lines_installed(self.paths)")):
        with open(os.path.join(ROOT, "vm/cloud/overlay/usr/local/sbin", prog)) as f:
            src = f.read()
        ast.parse(src)
        check(needle in src, f"{prog} hands configure {needle}")
    cfg_b = cc.validate_bootstrap({"name": "orchard", "address_id": "k7x2m9",
                                   "machine_id": "6f1d3c2a-9b8e-4a7d-8c5f-0e1d2c3b4a59",
                                   "image_archive": {"path": "/var/tmp/i.tar.gz", "host": "cseries:t",
                                                     "postgres": "cseries-postgres:t",
                                                     "door": "cseries-remote-door:t"}})
    check(cfg_b["image_archive"]["door"] == "cseries-remote-door:t",
          "bootstrap.json's image archive may carry the door's image (a test's VM)")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("PASSED" if not failed else "FAILED")
sys.exit(failed)
