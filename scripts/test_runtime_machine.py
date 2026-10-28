#!/usr/bin/env python3
# gate-scope: whole-tree — every file under vm/lima/rootfs/
"""The machine side of the local runtime: what a WSL guest and a cloud box
install so the runtime and the Host find each other.

  1. cseries-guest configure (in a temporary root): the runtime's image table
     from the release's keys (pinned by digest only), the browser's control
     token made once (0600), and the Host's drop-in joining it to the apps
     network, the socket's directory and the token, with the runtime group's
     gid; a second identical run changes nothing; configure without runtime
     keys keeps the table (a rollback hands it back); a machine without the
     runtime gets none of it; pull fetches the python image with the Host's.
  2. The units: the socket's directory is the runtime group's, 0750, made
     before the socket; the service needs the apps network; the plain proxy
     runs on its own config with Debian's masked.
  3. One set of numbers: the network file, both firewall files, the squid
     config and the runtime's constants name the same subnet, gateway, Host
     address and bridge name; the network has no default route.
  4. The WSL runtime.nft loads (nft -c in a private network namespace) and
     squid parses its config, where those tools are present
     (CSERIES_REQUIRE_NFT=1 / CSERIES_REQUIRE_SQUID=1 make them required).

Container-free: python3 scripts/test_runtime_machine.py
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "vm/runtime/lib"))
from cseries_runtime import common as C  # noqa: E402
from cseries_runtime import validate as V  # noqa: E402

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


GUEST = os.path.join(ROOT, "vm/wsl/rootfs/usr/local/bin/cseries-guest")
D = "sha256:" + "a" * 64
HOST = "ghcr.io/privatae/cseries@" + D
PG = "ghcr.io/privatae/cseries-postgres@" + D
PY = "ghcr.io/privatae/cseries-app-runtime-python@sha256:" + "b" * 64
BR = "ghcr.io/privatae/cseries-browser@sha256:" + "c" * 64
SRC = "https://example.test/cseries-harness-src-1.2.3.tar.gz"

FAKES = {
    "systemctl": "#!/bin/sh\necho \"systemctl $*\" >> \"$FAKE_LOG\"\n[ \"$1\" = show ] && echo loaded\nexit 0\n",
    "getent": "#!/bin/sh\n[ \"$1 $2\" = \"group cseries-runtime\" ] && echo 'cseries-runtime:x:987:' && exit 0\nexit 2\n",
    "podman": "#!/bin/sh\necho \"podman $*\" >> \"$FAKE_LOG\"\n[ \"$1 $2\" = \"image exists\" ] && exit 1\nexit 0\n",
}


def guest(root, bindir, args, stdin=""):
    env = dict(os.environ, CSERIES_GUEST_ROOT=root, FAKE_LOG=os.path.join(root, "calls.log"),
               PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
    return subprocess.run(["sh", GUEST, *args], input=stdin, env=env, capture_output=True, text=True,
                          check=False)


with tempfile.TemporaryDirectory() as tmp:
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir)
    for name, body in FAKES.items():
        with open(os.path.join(bindir, name), "w") as f:
            f.write(body)
        os.chmod(os.path.join(bindir, name), 0o755)

    # A machine with the runtime installed.
    root = os.path.join(tmp, "m1")
    os.makedirs(os.path.join(root, "usr/local/sbin"))
    with open(os.path.join(root, "usr/local/sbin/cseries-runtime"), "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(os.path.join(root, "usr/local/sbin/cseries-runtime"), 0o755)
    keys = (f"host={HOST}\npostgres={PG}\nrelease=1.2.3\npython={PY}\nbrowser={BR}\n"
            f"harness_src_url={SRC}\nharness_src_sha256={'d' * 64}\n")
    r = guest(root, bindir, ["configure"], keys)
    check(r.returncode == 0 and "changed=1" in r.stdout, "configure with the runtime's keys", r.stderr)
    with open(os.path.join(root, "etc/cseries-runtime/images.json")) as f:
        table = json.load(f)
    images, src = V.image_table(table)
    check(images == {"python": PY, "browser": BR} and src == {"url": SRC, "sha256": "d" * 64},
          "it writes the image table the runtime reads", table)
    tok_p = os.path.join(root, "etc/cseries-runtime/browser-control-token")
    tok = open(tok_p).read().strip()
    env_line = open(tok_p + ".env").read()
    check(re.fullmatch(r"[0-9a-f]{64}", tok) and env_line == f"BROWSER_CONTROL_TOKEN={tok}\n" and
          stat.S_IMODE(os.stat(tok_p).st_mode) == 0o600 and stat.S_IMODE(os.stat(tok_p + ".env").st_mode) == 0o600,
          "the browser's control token: 32 random bytes, in both files, 0600")
    dropin = open(os.path.join(root, "etc/containers/systemd/cseries-host.container.d/runtime.conf")).read()
    for line in ("Network=cseries-apps.network:ip=10.89.78.2,mac=02:63:0a:59:4e:02", "Volume=/run/cseries-runtime:/run/cseries-runtime",
                 "GroupAdd=987", "Environment=CSERIES_RUNTIME_SOCKET=/run/cseries-runtime/runtime.sock",
                 "Environment=CSERIES_RUNTIME_KERNEL_ADDR=10.89.78.2:8800",
                 "Environment=CSERIES_RUNTIME_NET=10.89.78.0/24",
                 "EnvironmentFile=/etc/cseries-runtime/browser-control-token.env",
                 "Requires=cseries-runtime.socket", "After=cseries-runtime.socket"):
        check(line in dropin.splitlines(), f"the Host's runtime drop-in has {line}")
    check(not any(x.startswith("IP=") for x in dropin.splitlines()), "and no single IP=")
    r = guest(root, bindir, ["configure"], keys)
    check(r.returncode == 0 and "changed=0" in r.stdout and open(tok_p).read().strip() == tok,
          "the same keys again change nothing and keep the token", r.stdout)
    # The Host's limits the cseries command sends beside the runtime's keys:
    # each drop-in is written, and a new ceiling alone is a change (each
    # drop-in has a change flag of its own).
    dropins = os.path.join(root, "etc/containers/systemd/cseries-host.container.d")
    r = guest(root, bindir, ["configure"], keys + "cpu_quota=300%\nmemory_max=2460M\n")
    check(r.returncode == 0 and "changed=1" in r.stdout
          and "CPUQuota=300%" in open(os.path.join(dropins, "10-cpu.conf")).read().splitlines()
          and "MemoryMax=2460M" in open(os.path.join(dropins, "10-memory.conf")).read().splitlines()
          and os.path.exists(os.path.join(dropins, "runtime.conf")),
          "configure with the runtime's keys and the Host's limits writes all three drop-ins", r.stderr)
    r = guest(root, bindir, ["configure"], keys + "cpu_quota=300%\nmemory_max=2000M\n")
    check(r.returncode == 0 and "changed=1" in r.stdout
          and "MemoryMax=2000M" in open(os.path.join(dropins, "10-memory.conf")).read().splitlines(),
          "a new memory ceiling alone is a change", r.stdout)
    r = guest(root, bindir, ["configure"], keys + "cpu_quota=200%\nmemory_max=2000M\n")
    check(r.returncode == 0 and "changed=1" in r.stdout, "a new processor ceiling alone is a change", r.stdout)
    r = guest(root, bindir, ["configure"], keys + "cpu_quota=200%\nmemory_max=2000M\n")
    check(r.returncode == 0 and "changed=0" in r.stdout, "the same limits again change nothing", r.stdout)
    r = guest(root, bindir, ["configure"], f"host={HOST}\npostgres={PG}\nrelease=1.2.3\n")
    images2, src2 = V.image_table(json.load(open(os.path.join(root, "etc/cseries-runtime/images.json"))))
    check(r.returncode == 0 and images2 == images and src2 == src,
          "configure without runtime keys keeps the image table")
    for bad, why in ((f"python=ghcr.io/privatae/cseries-app-runtime-python:latest\n", "an unpinned image"),
                     (f"harness_src_url={SRC}\n", "sources without their sha256"),
                     (f"harness_src_url=file:///etc/shadow\nharness_src_sha256={'d' * 64}\n", "a file URL"),
                     (f"harness_src_url=https://x.test/a\"b\nharness_src_sha256={'d' * 64}\n", "a quote in a URL"),
                     ("ubuntu=x@" + D + "\n", "an unknown family")):
        r = guest(root, bindir, ["configure"], f"host={HOST}\npostgres={PG}\nrelease=1.2.3\n" + bad)
        check(r.returncode != 0, f"configure refuses {why}", r.stdout)
    open(os.path.join(root, "calls.log"), "w").close()
    r = guest(root, bindir, ["pull"])
    calls = open(os.path.join(root, "calls.log")).read()
    check(r.returncode == 0 and f"podman pull {PY}" in calls and f"podman pull {BR}" not in calls,
          "pull fetches the python image with the Host's (the browser's comes on first use)", calls)

    # A machine without the runtime.
    root2 = os.path.join(tmp, "m2")
    os.makedirs(root2)
    r = guest(root2, bindir, ["configure"], keys)
    check(r.returncode == 0 and not os.path.exists(os.path.join(
        root2, "etc/containers/systemd/cseries-host.container.d/runtime.conf")) and
          not os.path.exists(os.path.join(root2, "etc/cseries-runtime")),
          "a machine without the runtime gets no drop-in, no table and no token", r.stderr)

# ── 2. units ────────────────────────────────────────────────────────────────

sock = read("vm/runtime/systemd/cseries-runtime.socket")
for line in ("ListenStream=/run/cseries-runtime/runtime.sock", "SocketGroup=cseries-runtime", "SocketMode=0660",
             "DirectoryMode=0750",
             "ExecStartPre=/usr/bin/install -d -m 0750 -o root -g cseries-runtime /run/cseries-runtime"):
    check(line in sock.splitlines(), f"the socket unit has {line}")
svc = read("vm/runtime/systemd/cseries-runtime.service")
check("Requires=cseries-runtime.socket cseries-apps-network.service" in svc and
      "ExecStart=/usr/local/sbin/cseries-runtime" in svc and "SyslogIdentifier=cseries-runtime" in svc and
      "[Install]" not in svc, "the service needs its socket and the apps network, and only its socket starts it")
squid_svc = read("vm/runtime/systemd/cseries-squid.service")
check("ExecStart=/usr/sbin/squid -N -f /etc/cseries-runtime/squid.conf" in squid_svc,
      "the plain proxy runs on its own config")
cust = read("vm/wsl/customize.sh")
check("systemctl mask squid.service" in cust and "groupadd --system cseries-runtime" in cust and
      "systemctl enable cseries-runtime.socket cseries-squid.service cseries-runtime-firewall.service" in cust,
      "the WSL guest makes the group, enables the runtime and masks Debian's squid")
# The Lima guest (macOS and Linux) takes the WSL guest's packages, so squid
# with them; it installs the runtime the same way, from the same sources.
lima_cust = read("vm/lima/customize.sh")
lima_build = read("vm/lima/build-image.sh")
wsl_runtime = [x for x in cust.splitlines() if "/src-runtime/" in x or "seccomp-browser.json" in x
               or ("cseries-runtime" in x and x.startswith("chroot")) or "mask squid.service" in x]
check(len(wsl_runtime) >= 9 and all(x in lima_cust.splitlines() for x in wsl_runtime),
      "the Lima guest installs the runtime with the WSL guest's own lines",
      [x for x in wsl_runtime if x not in lima_cust.splitlines()])
check('-v "$repo/vm/runtime:/src-runtime:ro"' in lima_build
      and '-v "$repo/seccomp-browser.json:/src-seccomp/seccomp-browser.json:ro"' in lima_build,
      "the Lima build hands its builder the runtime's sources and the browser's seccomp profile")
# The Lima guest's quadlets, drop-ins and cseries-guest are the WSL guest's own
# files, so the remote-access door's units (and the drop-in cseries-guest
# writes for the Host) cannot drift between the two guests, and the WSL boot
# test's door part speaks for both: Lima's builder copies WSL's whole overlay
# (vm/wsl/rootfs), takes away only the two files WSL alone reads, installs
# under etc/containers only lines WSL's customize.sh has too, removes or
# rewrites nothing there or cseries-guest, and its own overlay, copied after,
# brings nothing that would replace them. Its packages add no podman of
# another series (WSL's list brings it).
lima_lines = [x.strip() for x in lima_cust.splitlines() if x.strip() and not x.strip().startswith("#")]
wsl_lines = [x.strip() for x in cust.splitlines()]
copy_at = lima_lines.index('cp -R /src-wsl/rootfs/. "$root/"') if 'cp -R /src-wsl/rootfs/. "$root/"' in lima_lines else -1
check(copy_at >= 0 and 'wsl="$repo/vm/wsl"' in lima_build and '-v "$wsl:/src-wsl:ro"' in lima_build,
      "the Lima guest copies the WSL guest's whole overlay (vm/wsl/rootfs)")
lima_rm = [x for x in lima_lines if x.split()[0] in ("rm", "mv", "ln", "sed", "truncate")]
check(lima_rm == ['rm -f "$root/etc/wsl.conf" "$root/etc/wsl-distribution.conf"', 'rm -f "$root"/etc/ssh/ssh_host_*',
                  'rm -f "$root/etc/apt/sources.list"'],
      "the Lima guest removes only WSL's two own files, the SSH host keys and the apt list", lima_rm)
touch = [x for x in lima_lines[copy_at + 1:] if "containers" in x or "cseries-guest" in x or "systemd/system/" in x]
check(all(x in wsl_lines or x.startswith("chmod 0755 ") for x in touch) and
      not [x for x in lima_lines if ">" in x and ("containers" in x or "cseries-guest" in x)],
      "after the copy the Lima guest writes under etc/containers and cseries-guest only as the WSL guest does",
      [x for x in touch if x not in wsl_lines])
lima_overlay = [os.path.relpath(os.path.join(d, f), os.path.join(ROOT, "vm/lima/rootfs"))
                for d, _s, fs in os.walk(os.path.join(ROOT, "vm/lima/rootfs")) for f in fs]
check(lima_overlay and not [p for p in lima_overlay if p.startswith(("etc/containers/", "usr/local/", "etc/systemd/system/"))],
      "the Lima guest's own overlay replaces none of the WSL guest's units, drop-ins or cseries-guest", lima_overlay)
check("containers" not in read("vm/lima/mkimage.sh") and
      not [x for x in read("vm/lima/packages").split() if x.startswith(("podman", "golang-github-containers"))],
      "the Lima image's own build and packages add no units and no other podman")
door_units = ("cseries-remote.network", "cseries-remote-door.container", "cseries-remote-cert.container")
check(all(os.path.isfile(os.path.join(ROOT, "vm/wsl/rootfs/etc/containers/systemd", u)) for u in door_units),
      "the door's network and both of its units are in the overlay both guests take")
pk = [x.strip() for x in read("vm/wsl/packages").splitlines() if x.strip() and not x.startswith("#")]
check("python3" in pk and "squid" in pk, "the WSL guest's packages include python3 and squid")
boot = read("vm/cloud/overlay/usr/local/sbin/cseries-cloud-bootstrap")
check('"mask", "squid.service"' in boot and '"cseries-runtime.socket", "cseries-squid.service"' in boot and
      boot.index('"cseries-runtime.socket", "cseries-squid.service"') < boot.index("start_host(host_ref"),
      "a cloud box masks Debian's squid and starts the runtime's socket before the Host")
check('"coders", "off"' in boot and '"coders", "on"' not in boot,
      "the bastion's coder mode stays off where the runtime runs (one memory budget)")
check(" squid" in read("vm/cloud/bootstrap.sh").split("PACKAGES=")[1].split('"')[1].replace("\n", " "),
      "a cloud box installs squid")

# ── 3. one set of numbers ───────────────────────────────────────────────────

net = read("vm/runtime/containers/cseries-apps.network")
check(f"Subnet={C.SUBNET}" in net and f"Gateway={C.GATEWAY}" in net and f"NetworkName={C.NETWORK}" in net and
      "PodmanArgs=--interface-name=cseries-apps0" in net.splitlines()
      and not any(x.startswith("InterfaceName=") for x in net.splitlines())
      and "Options=no_default_route=1" in net,
      "the apps network: the runtime's subnet, gateway and name, its bridge name, and no default route")
# The Host treats the network's first address as the machine (its gateway),
# never as a container (server/middleware/localhost_only.runtime_peer).
import ipaddress  # noqa: E402
check(ipaddress.ip_network(C.SUBNET).network_address + 1 == ipaddress.ip_address(C.GATEWAY),
      "the apps network's gateway is its first address, which the Host's gate never counts as a container")
# Podman hands out the network's own range to a container started without a
# fixed address; the runtime places every one of its own at a fixed address
# from IP_FIRST, so the two never meet.
rng = [x for x in net.splitlines() if x.startswith("IPRange=")]
lo, _, hi = (rng[0].split("=", 1)[1] if rng else "-").partition("-")
check(rng and lo.startswith(C.IP_PREFIX) and hi.startswith(C.IP_PREFIX) and
      C.HOST_IP.rsplit(".", 1)[1] < lo.rsplit(".", 1)[1] and
      int(C.HOST_IP.rsplit(".", 1)[1]) < int(lo.rsplit(".", 1)[1]) <= int(hi.rsplit(".", 1)[1]) < C.IP_FIRST,
      "the network's own range lies between the Host and the runtime's fixed addresses", rng)
check(lo == f"{C.IP_PREFIX}{C.POOL_IP_FIRST}" and hi == f"{C.IP_PREFIX}{C.POOL_IP_LAST}",
      "the network's own range is common.py's POOL_IP_FIRST..POOL_IP_LAST", rng)

# The plain proxy is the coding engines' only (a build reaches it as its own
# user, from the machine): an app's own code reaching it would leave past its
# declared hosts and the owner's signed rules. Every address range on the
# network is either inside the proxy's clients or outside them entirely, and
# only engines get the inside; an address podman hands out itself is outside.
proxy_ips = {i for a, b in C.PROXY_CLIENTS for i in range(a, b + 1)}
app_ips = set(range(C.IP_FIRST, C.IP_LAST + 1))
engine_ips = set(range(C.ENGINE_IP_FIRST, C.ENGINE_IP_LAST + 1))
pool_ips = set(range(C.POOL_IP_FIRST, C.POOL_IP_LAST + 1))
check(not app_ips & proxy_ips and engine_ips <= proxy_ips and not app_ips & engine_ips
      and not pool_ips & proxy_ips and proxy_ips == engine_ips
      and C.HOST_IP.rsplit(".", 1)[1] not in {str(i) for i in proxy_ips}
      and len(engine_ips) >= len(C.CODING_ENGINES) and C.IP_LAST < 255 and C.ENGINE_IP_LAST < 255,
      "apps, the browser, the Host and podman's own range are outside the proxy's clients; "
      "every coding engine fits inside")
check(all(C.ip_range("harness", h) == (C.ENGINE_IP_FIRST, C.ENGINE_IP_LAST) for h in C.CODING_ENGINES)
      and C.ip_range("harness", C.BROWSER) == (C.IP_FIRST, C.IP_LAST)
      and C.ip_range("app", "telegram") == (C.IP_FIRST, C.IP_LAST),
      "a coding engine takes an engine address; the browser and an app take an app's")
nft_clients = "{ " + ", ".join(f"{C.IP_PREFIX}{a}-{C.IP_PREFIX}{b}" for a, b in C.PROXY_CLIENTS) + " }"
for rel in ("vm/wsl/rootfs/etc/cseries-runtime/runtime.nft", "vm/cloud/overlay/etc/cseries-cloud/nftables/door.nft"):
    t = read(rel)
    check(f"define APPS_NET = {C.SUBNET}" in t and f"define APPS_GW = {C.GATEWAY}" in t and
          f"define APPS_HOST = {C.HOST_IP}" in t and 'meta ibrname != "cseries-apps0" accept' in t and
          "ip daddr $APPS_HOST tcp dport { 8800, 13129 } accept" in t and
          "ip saddr $APPS_NET ip daddr != $APPS_NET drop" in t and
          f"define APPS_PROXY_CLIENTS = {nft_clients}" in t and
          "ip saddr $APPS_PROXY_CLIENTS ip daddr $APPS_GW tcp dport 3128 accept" in t and
          "ip saddr $APPS_NET ip daddr $APPS_GW tcp dport 3128 accept" not in t,
          f"{rel}: the same network, the bridge filter, no routing out, the proxy on the gateway "
          "for the engines only")
    # The rules name IPv4; IPv6 from the bridge (a link-local address a
    # container may hold) is dropped ahead of them, in input and forward.
    inp = t.split("chain input", 1)[1].split("chain ", 1)[0]
    fwd = t.split("chain forward", 1)[1].split("chain ", 1)[0]
    first = [ln.strip() for ln in inp.splitlines()
             if ln.strip() not in ("", "{") and not ln.strip().startswith(("#", "type "))]
    check(first and first[0] == 'iifname "cseries-apps0" meta nfproto ipv6 drop' and
          'iifname "cseries-apps0" meta nfproto ipv6 drop' in fwd and
          'oifname "cseries-apps0" meta nfproto ipv6 drop' in fwd,
          f"{rel}: IPv6 from the apps bridge is dropped before any accept", first[:2])
sq = read("vm/runtime/squid.conf")
acl = [ln.split()[3:] for ln in sq.splitlines() if ln.startswith("acl apps src ")]
squid_ips = set()
for part in (acl[0] if acl else []):
    squid_ips |= {int(str(a).rsplit(".", 1)[1]) for a in ipaddress.ip_network(part, strict=False)}
check(len(acl) == 1 and squid_ips == proxy_ips and "http_port 3128" in sq,
      "the plain proxy admits exactly the coding engines' addresses, on the runtime's port",
      sorted(squid_ips ^ proxy_ips))
check(C.HOST_PORT == 8800 and C.LANE_PORT == 13129 and C.SQUID_PORT == 3128,
      "the ports the firewall opens are the runtime's")
# NAT64: squid refuses exactly what the Host's gate refuses inside 64:ff9b::/32
# (nac_egress._outside) -- each refused IPv4 range as the well-known prefix
# carries it, and the rest of the /32 whole -- and nothing else there.
internal = [ipaddress.ip_network(x) for ln in sq.splitlines() if ln.startswith("acl internal dst ")
            for x in ln.split()[3:]]
v4 = [n for n in internal if n.version == 4]
wkp, all64 = ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b::/32")
want = [ipaddress.ip_network((int(wkp.network_address) + int(n.network_address), 96 + n.prefixlen))
        for n in v4] + list(all64.address_exclude(wkp))
have = [n for n in internal if n.version == 6 and n.subnet_of(all64)]
check(sorted(ipaddress.collapse_addresses(have)) == sorted(ipaddress.collapse_addresses(want)),
      "squid refuses each refused IPv4 range inside NAT64's 64:ff9b::/96, and the rest of 64:ff9b::/32",
      sorted(set(map(str, ipaddress.collapse_addresses(want))) ^
             set(map(str, ipaddress.collapse_addresses(have))))[:6])


def refused(addr):
    a = ipaddress.ip_address(addr)
    return any(a.version == n.version and a in n for n in internal)


check(all(refused(a) for a in ("64:ff9b::a9fe:a9fe", "64:ff9b::a00:1", "64:ff9b::7f00:1",
                               "64:ff9b::c0a8:101", "64:ff9b:1::808:808", "64:ff9b:1:ffff::a00:1")) and
      not any(refused(a) for a in ("64:ff9b::808:808", "64:ff9b::5db8:d70e")),
      "so 169.254.169.254, 10/8, 127/8 and 192.168/16 through NAT64 and the local-use /48 are refused, "
      "and a public IPv4 site through NAT64 is not")

# ── 4. the tools ────────────────────────────────────────────────────────────

nft = shutil.which("nft")
unshare = shutil.which("unshare")
if nft and unshare and os.geteuid() == 0:
    r = subprocess.run([unshare, "-n", nft, "-c", "-f",
                        os.path.join(ROOT, "vm/wsl/rootfs/etc/cseries-runtime/runtime.nft")],
                       capture_output=True, text=True, check=False)
    check(r.returncode == 0, "the WSL runtime.nft loads (nft -c, private network namespace)", r.stderr)
elif os.environ.get("CSERIES_REQUIRE_NFT") == "1":
    check(False, "nft and a private network namespace are needed")
else:
    print("SKIP  nft is not usable here")
squid = shutil.which("squid") or (os.path.exists("/usr/sbin/squid") and "/usr/sbin/squid")
if squid:
    r = subprocess.run([squid, "-k", "parse", "-f", os.path.join(ROOT, "vm/runtime/squid.conf")],
                       capture_output=True, text=True, check=False)
    check(r.returncode == 0, "squid parses the plain proxy's config", r.stderr[-400:])
elif os.environ.get("CSERIES_REQUIRE_SQUID") == "1":
    check(False, "squid is needed")
else:
    print("SKIP  squid is not installed here")

print()
print("FAIL" if failed else "all runtime machine checks passed")
sys.exit(failed)
