#!/usr/bin/env python3
"""Remote access in the Docker Compose install (compose.remote.yaml): the door
reaches the Host's web port and nothing else, and the Host's tunnel still
reaches the door. The door filters itself (sidecars/remote-door/door-filter),
in its own network namespace, before it starts. Real packets, in private
network namespaces, so nothing on this machine's own ruleset is read or
changed.

  1. The wiring: compose.remote.yaml starts the door through door-filter, as
     root with NET_ADMIN, SETGID, SETPCAP and SETUID only, no-new-privileges,
     on the remote network only; the door image carries door-filter and nft
     (pinned) and its own entrypoint is still door-run, which the guests run
     directly (their bridge has remote.nft); caddy and lego lose the file
     capability the base image gives them, which the door's empty bounding
     set would make the kernel refuse to exec; the table door-filter prints for
     compose.remote.yaml's addresses lets out only the Host's web port and in
     only the Host's tunnel to the door's own port; an address that is not an
     IPv4 literal (a name, loopback written as ::1) is refused and prints
     nothing.
  2. door-filter fails closed: with no NET_ADMIN over its network namespace
     (as root, a user namespace without a network namespace of its own), or
     not started as root, it exits 1, says the door does not start, and never
     reaches the door.
  3. Packets. "host" plays the Host at 10.89.79.2, listening as the Host does
     on every address: 8800 (the web port), 8810 and 8811 (the bastion's),
     13129 (the apps' egress lane), 5432 (a port nothing publishes) and a UDP
     port. "door" plays the door at 10.89.79.3:8443, "stranger" another
     address on the network, and the namespace this runs in the machine, at
     the bridge's 10.89.79.1, with a listener of its own.
       - before the filter, the door reaches the Host's 8810, 13129 and 5432,
         the machine, and the Host's 8800 over IPv6 link-local (the gap this
         closes);
       - door-filter itself loads the table in the door's namespace (and then
         stops: no door here to run);
       - with it, the door reaches the Host's 8800, data both ways, and
         nothing else: not the Host's 8810, 8811, 13129, 5432, UDP or 8800
         over IPv6, not the stranger's 8800, not the machine;
       - the Host's tunnel reaches the door's 8443 and a PROXY v2 header and
         the bytes after it go through and come back; nothing reaches the
         door from the stranger, nor on another port;
       - loaded a second time, all of that holds;
       - after door-filter's own drop_privileges() the door is 1000, with an
         empty bounding and effective set and no_new_privs; nft cannot delete
         or change its table, root does not come back, and the table still
         holds.

Needs nft, ip(8), python3 and a way into private namespaces: root's
`unshare -mn` (root in rootlesskit's user namespace counts, as on the forge
runner), or an unprivileged user namespace (`unshare -rmn`). Without them it
FAILS and says why: a skip reads as a pass. Mounts are made with mount(2),
not mount(8) (scripts/private_netns.py).

Container-free: python3 scripts/test_remote_door_compose_filter.py
"""
import os
import re
import shutil
import subprocess
import sys

import yaml

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FILTER = os.path.join(ROOT, "sidecars/remote-door/door-filter")
DOCKERFILE = os.path.join(ROOT, "sidecars/remote-door/Dockerfile")
COMPOSE = os.path.join(ROOT, "compose.remote.yaml")
DOOR_UNIT = os.path.join(ROOT, "vm/wsl/rootfs/etc/containers/systemd/cseries-remote-door.container")

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


def _default(v):
    """${NAME:-default} -> default, as Compose reads it with NAME unset."""
    m = re.fullmatch(r"\$\{\w+:-([^}]*)\}(.*)", str(v))
    return m.group(1) + m.group(2) if m else str(v)


# ── 1. the wiring ───────────────────────────────────────────────────────────

compose = yaml.safe_load(read(COMPOSE) or "{}") or {}
door = (compose.get("services") or {}).get("remote-door") or {}
env = {k: _default(v) for k, v in (door.get("environment") or {}).items()}
check(door.get("entrypoint") == ["/usr/local/sbin/door-filter"],
      "compose.remote.yaml starts the door through door-filter", door.get("entrypoint"))
check(str(door.get("user")) == "0:0", "the door's container starts as root, for door-filter to load its table",
      door.get("user"))
check(door.get("cap_drop") == ["ALL"] and sorted(door.get("cap_add") or []) == ["NET_ADMIN", "SETGID", "SETPCAP", "SETUID"],
      "with NET_ADMIN, SETGID, SETPCAP and SETUID and no other capability", (door.get("cap_drop"), door.get("cap_add")))
check("no-new-privileges:true" in (door.get("security_opt") or []), "and no-new-privileges")
check(list(door.get("networks") or []) == ["remote"], "the door is on the remote network only")
check(env.get("CSERIES_HOST_UPSTREAM") == "10.89.79.2:8800" and env.get("CSERIES_TUNNEL_FROM") == "10.89.79.2"
      and env.get("CSERIES_DOOR_BIND") == "10.89.79.3" and env.get("CSERIES_DOOR_PORT") == "8443",
      "the addresses door-filter reads are the door's (Host 10.89.79.2:8800, tunnel from 10.89.79.2, door 10.89.79.3:8443)",
      env)

dockerfile = read(DOCKERFILE)
copy = [ln for ln in dockerfile.splitlines() if ln.startswith("COPY ") and "door-run" in ln]
check(len(copy) == 1 and "sidecars/remote-door/door-filter" in copy[0].split() and copy[0].endswith(" /usr/local/sbin/"),
      "the door image carries door-filter at /usr/local/sbin/door-filter", copy)
check(re.search(r"apk add --no-cache [^\n]*\bnftables[=~][0-9][^\s]*", dockerfile) is not None
      and "test -x /usr/sbin/nft" in dockerfile, "and nft, pinned, at /usr/sbin/nft")
check('ENTRYPOINT ["/usr/local/sbin/door-run"]' in dockerfile and "/usr/local/sbin/door-run" in read(FILTER),
      "the image's own entrypoint is door-run, which door-filter runs last")
# The caddy base image gives caddy (and Alpine's package gives lego)
# cap_net_bind_service=+ep. The door runs with an empty bounding set, and the
# kernel refuses to exec a binary whose effective file capability it cannot
# grant ("exec: Operation not permitted"): Caddy would never start, nor lego.
strip = [ln for ln in dockerfile.splitlines() if "removexattr" in ln and "security.capability" in ln]
check(len(strip) == 1 and "/usr/bin/caddy" in strip[0] and "/usr/bin/lego" in strip[0]
      and "file capabilities left" in strip[0],
      "the image takes caddy's and lego's file capability away, so both run with the door's empty bounding set",
      strip)
check("door-filter" not in read(DOOR_UNIT) and "Requires=cseries-remote-firewall.service" in read(DOOR_UNIT),
      "the guests' door runs door-run directly, behind their bridge's remote.nft")
check(os.access(FILTER, os.X_OK), "door-filter is executable")


def door_filter(args, extra_env=None, prefix=()):
    e = {"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"), **env, **(extra_env or {})}
    return subprocess.run([*prefix, sys.executable, "-I", FILTER, *args], env=e,
                          capture_output=True, text=True, check=False)


r = door_filter(["rules"])
rules = r.stdout
body = [ln.strip() for ln in rules.splitlines() if ln.strip()]
check(r.returncode == 0 and body == [
    "table inet cseries_door", "delete table inet cseries_door", "table inet cseries_door {",
    "chain output {", "type filter hook output priority filter; policy drop;", 'oifname "lo" accept',
    "ct state established,related accept", "ip daddr 10.89.79.2 tcp dport 8800 ct state new accept", "}",
    "chain input {", "type filter hook input priority filter; policy drop;", 'iifname "lo" accept',
    "ct state established,related accept",
    "ip saddr 10.89.79.2 ip daddr 10.89.79.3 tcp dport 8443 ct state new accept", "}", "}",
], "door-filter's table: its own, replaced whole; out, only new connections to the Host's 8800; in, only new "
   "ones from the Host's tunnel to the door's 8443; replies and loopback; nothing else, IPv6 included",
   f"\n{rules}{r.stderr}")
for bad in ({"CSERIES_HOST_UPSTREAM": "host:8800"}, {"CSERIES_HOST_UPSTREAM": "10.89.79.2"},
            {"CSERIES_TUNNEL_FROM": "::1"}, {"CSERIES_DOOR_BIND": "0.0.0.0 tcp dport 1-65535 accept"},
            {"CSERIES_DOOR_PORT": "8443 accept"}):
    r = door_filter(["rules"], bad)
    check(r.returncode == 1 and not r.stdout and "the door does not start" in r.stderr,
          f"door-filter refuses {bad} and prints no table", (r.returncode, r.stdout, r.stderr))

# ── 2. fail closed ──────────────────────────────────────────────────────────

unshare = shutil.which("unshare")
nft = shutil.which("nft") or ("/usr/sbin/nft" if os.path.exists("/usr/sbin/nft") else None)
ip = shutil.which("ip") or ("/usr/sbin/ip" if os.path.exists("/usr/sbin/ip") else None)
missing = [n for n, p in (("nft", nft), ("ip", ip), ("unshare", unshare)) if not p]
if missing:
    check(False, f"this gate needs {', '.join(missing)} on PATH, so the packets are not checked")
    sys.exit(failed)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from private_netns import MOUNTNETNS, MOUNTS, no_namespace_why  # noqa: E402

# As root: a user namespace of its own and NO network namespace, root there
# with no NET_ADMIN over the network namespace it is in (the machine's), so
# nft is refused and nothing on this machine changes. As anyone else: not
# root at all.
if os.geteuid() == 0:
    r, want, how = door_filter([], prefix=("unshare", "-Ur")), "nft could not load the door's table", \
        "with no NET_ADMIN over its network namespace"
else:
    r, want, how = door_filter([]), "it must start as root", "not started as root"
check(r.returncode == 1 and want in r.stderr and "the door does not start" in r.stderr
      and "running the door" not in r.stderr,
      f"{how}, door-filter exits 1 and the door does not start", (r.returncode, r.stderr[-400:]))

# ── 3. packets ──────────────────────────────────────────────────────────────

r = subprocess.run(MOUNTNETNS + ["true"], capture_output=True, text=True, check=False)
if r.returncode != 0:
    check(False, no_namespace_why(r.stderr, "private mount and network namespace"))
    sys.exit(failed)

# Echo listeners: each TCP connection gets back what it sent, so "open" means
# data went both ways; UDP answers each datagram.
LISTEN = r'''
import selectors, socket, sys, time
sel = selectors.DefaultSelector()
for spec in sys.argv[1:]:
    proto, addr, port = spec.split("|")
    fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
    if proto == "tcp":
        s = socket.socket(fam); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if fam == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        s.bind((addr, int(port))); s.listen(16); s.setblocking(False)
        sel.register(s, selectors.EVENT_READ, "tcp")
    else:
        s = socket.socket(fam, socket.SOCK_DGRAM); s.bind((addr, int(port)))
        sel.register(s, selectors.EVENT_READ, "udp")
end = time.time() + 150
while time.time() < end:
    for key, _ in sel.select(1):
        if key.data == "tcp":
            c, _ = key.fileobj.accept(); c.settimeout(3)
            try:
                c.sendall(c.recv(256))
            except OSError:
                pass
            c.close()
        else:
            d, a = key.fileobj.recvfrom(512); key.fileobj.sendto(d, a)
'''

# tcp|addr|port sends a knock; proxy|addr|port sends a PROXY v2 header (a
# browser at 198.51.100.7:40000 for 10.89.79.3:8443) and a knock after it,
# as the Host's tunnel writes ahead of each browser's TLS.
PROBE = r'''
import socket, struct, sys
PROXY = (b"\r\n\r\n\x00\r\nQUIT\n" + bytes([0x21, 0x11]) + struct.pack("!H", 12)
         + socket.inet_aton("198.51.100.7") + socket.inet_aton("10.89.79.3") + struct.pack("!HH", 40000, 8443))
out = []
for spec in sys.argv[1:]:
    proto, addr, port = spec.split("|")
    fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
    target = (addr, int(port), 0, socket.if_nametoindex("eth0")) if fam == socket.AF_INET6 else (addr, int(port))
    try:
        if proto in ("tcp", "proxy"):
            msg = (PROXY if proto == "proxy" else b"") + b"knock"
            s = socket.socket(fam); s.settimeout(1.5); s.connect(target)
            s.sendall(msg); got = b""
            while len(got) < len(msg):
                part = s.recv(256)
                if not part:
                    break
                got += part
            s.close()
            out.append("open" if got == msg else "no-echo")
        else:
            s = socket.socket(fam, socket.SOCK_DGRAM); s.settimeout(1.5)
            s.sendto(b"q", target); s.recvfrom(16); out.append("open")
    except socket.timeout:
        out.append("dropped")
    except ConnectionRefusedError:
        out.append("refused")
    except OSError as e:
        out.append("error:" + str(e.errno))
print(" ".join(out))
'''

# What a blocked connection looks like: no answer at all (dropped on the way
# in), the kernel refusing to send it (EPERM, dropped on the way out), or no
# neighbour (IPv6's neighbour discovery is dropped with the rest of IPv6).
# "refused" is not blocked: a reset came back, so the SYN got there.
BLOCKED = ("dropped", "error:1", "error:113")
# What the door holds once door-filter's own drop_privileges() has run (as
# root in the door's namespace, where its table is loaded): who it is, its
# bounding and effective sets, no_new_privs, whether nft can change the
# table, and whether root comes back.
AFTERDROP = r'''
import importlib.machinery, importlib.util, os, subprocess, sys
spec = importlib.util.spec_from_loader("door_filter", importlib.machinery.SourceFileLoader("door_filter", sys.argv[1]))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.drop_privileges()
st = {ln.split(":")[0]: ln.split()[1:] for ln in open("/proc/self/status")}
out = ["uid=" + st["Uid"][0], "bnd=%d" % int(st["CapBnd"][0], 16), "eff=%d" % int(st["CapEff"][0], 16),
       "nnp=" + st["NoNewPrivs"][0]]
for args in (["delete", "table", "inet", "cseries_door"], ["add", "rule", "inet", "cseries_door", "output", "accept"]):
    r = subprocess.run([sys.argv[2], *args], capture_output=True, text=True, check=False)
    out.append("nft=refused" if r.returncode != 0 else "nft=changed")
try:
    os.setresuid(0, 0, 0)
    out.append("root=back")
except OSError:
    out.append("root=refused")
print(" ".join(out))
'''
DOOR_OTHER = ["tcp|10.89.79.2|8810", "tcp|10.89.79.2|8811", "tcp|10.89.79.2|13129", "tcp|10.89.79.2|5432",
              "udp|10.89.79.2|5353", "tcp|fe80::2|8800", "tcp|10.89.79.4|8800", "tcp|10.89.79.1|2222"]
ENV = " ".join(f"{k}={v}" for k, v in sorted(env.items()) if k.startswith("CSERIES_") and re.fullmatch(r"[\w.:]*", v))
script = f"""set -eu
python3 -I -c '{MOUNTS}'
ip link set lo up
ip netns add host
ip netns add door
ip netns add stranger
# the remote network, as Docker makes it: a bridge with the gateway's address
ip link add br-cseries type bridge
ip addr add 10.89.79.1/29 dev br-cseries
ip link set br-cseries up
port() {{  # namespace, address, link-local
    ip link add "v$1" type veth peer name eth0 netns "$1"
    ip link set "v$1" master br-cseries
    ip link set "v$1" up
    ip netns exec "$1" sh -c "ip link set lo up; ip addr add $2 dev eth0; ip link set eth0 up"
    ip netns exec "$1" ip -6 addr add "$3/64" dev eth0 nodad
}}
pids=""
port host 10.89.79.2/29 fe80::2
port door 10.89.79.3/29 fe80::3
port stranger 10.89.79.4/29 fe80::4
ip netns exec host timeout 150 python3 -I -c '{LISTEN}' 'tcp|::|8800' 'tcp|0.0.0.0|8810' 'tcp|0.0.0.0|8811' 'tcp|::|13129' 'tcp|0.0.0.0|5432' 'udp|0.0.0.0|5353' &
pids="$pids $!"
ip netns exec door timeout 150 python3 -I -c '{LISTEN}' 'tcp|10.89.79.3|8443' 'tcp|0.0.0.0|9999' &
pids="$pids $!"
ip netns exec stranger timeout 150 python3 -I -c '{LISTEN}' 'tcp|0.0.0.0|8800' &
pids="$pids $!"
timeout 150 python3 -I -c '{LISTEN}' 'tcp|10.89.79.1|2222' &
pids="$pids $!"
sleep 1.5
fromdoor() {{ ip netns exec door python3 -I -c '{PROBE}' "$@"; }}
echo "before: $(fromdoor 'tcp|10.89.79.2|8810' 'tcp|10.89.79.2|13129' 'tcp|10.89.79.2|5432' 'tcp|10.89.79.1|2222' 'tcp|fe80::2|8800')"
set +e
fout=$(ip netns exec door env -i PATH="$PATH" {ENV} {sys.executable} -I {FILTER} 2>&1)
frc=$?
set -e
echo "filter: rc=$frc $(printf '%s' "$fout" | tr '\\n' ' ')"
for round in 1 2; do
    if [ "$round" = 2 ]; then
        ip netns exec door env -i PATH="$PATH" {ENV} {sys.executable} -I {FILTER} rules | ip netns exec door {nft} -f -
    fi
    echo "table$round: $(ip netns exec door {nft} list table inet cseries_door | grep -c 'policy drop')"
    echo "web$round: $(fromdoor 'tcp|10.89.79.2|8800')"
    echo "other$round: $(fromdoor {" ".join(repr(s) for s in DOOR_OTHER)})"
    echo "tunnel$round: $(ip netns exec host python3 -I -c '{PROBE}' 'proxy|10.89.79.3|8443' 'tcp|10.89.79.3|9999')"
    echo "stranger$round: $(ip netns exec stranger python3 -I -c '{PROBE}' 'proxy|10.89.79.3|8443' 'tcp|10.89.79.2|8810')"
done
echo "afterdrop: $(ip netns exec door {sys.executable} -I -c '{AFTERDROP}' {FILTER} {nft} 2>&1 | tr '\\n' ' ')"
echo "table3: $(ip netns exec door {nft} list table inet cseries_door | grep -c 'policy drop')"
echo "web3: $(fromdoor 'tcp|10.89.79.2|8800')"
echo "other3: $(fromdoor 'tcp|10.89.79.2|8810' 'tcp|10.89.79.1|2222')"
# The listeners hold this script's output open until they end.
kill $pids 2>/dev/null || true
wait 2>/dev/null || true
"""
r = subprocess.run(MOUNTNETNS + ["sh", "-c", script], capture_output=True, text=True, check=False)
out = r.stdout
lines = dict(ln.split(": ", 1) for ln in out.splitlines() if ": " in ln)
detail = f"\n--- stdout\n{out}\n--- stderr\n{r.stderr[-1500:]}"

check(lines.get("before") == "open open open open open",
      "without the filter the door reaches the Host's 8810, 13129 and 5432, the machine, and the Host's 8800 "
      "over IPv6 link-local (the gap this closes)", detail)
check(str(lines.get("filter", "")).startswith("rc=1 ") and "running the door" not in lines.get("filter", "")
      or "running the door as 1000:1000" in lines.get("filter", "") and "could not run /usr/local/sbin/door-run"
      in lines.get("filter", ""),
      "door-filter loaded its table in the door's namespace and went no further (no door here to run)", detail)
for rnd in ("1", "2"):
    again = "" if rnd == "1" else ", loaded a second time"
    check(lines.get(f"table{rnd}") == "2", f"the door's namespace holds door-filter's table{again}", detail)
    check(lines.get(f"web{rnd}") == "open", f"the door reaches the Host's web port 8800, data both ways{again}", detail)
    got = str(lines.get(f"other{rnd}", "")).split()
    check(len(got) == len(DOOR_OTHER) and all(g in BLOCKED for g in got),
          f"the door reaches nothing else: the Host's 8810, 8811, 13129, 5432, UDP and 8800 over IPv6, the "
          f"stranger's 8800, the machine{again}", detail)
    check(str(lines.get(f"tunnel{rnd}", "")).split()[:1] == ["open"],
          f"the Host's tunnel reaches the door's 8443, its PROXY v2 header and the bytes after it both ways{again}",
          detail)
    got = str(lines.get(f"tunnel{rnd}", "")).split()[1:] + str(lines.get(f"stranger{rnd}", "")).split()
    check(len(got) == 3 and all(g in BLOCKED for g in got[:2]) and got[2] == "open",
          f"nothing reaches the door but the Host's tunnel on 8443 (not the Host on another port, not the "
          f"stranger), and the filter is the door's alone (the stranger still reaches the Host){again}", detail)
ad = str(lines.get("afterdrop", "")).strip()
if "could not become 1000:1000" in ad:
    # A user namespace that maps no uid 1000 (an unprivileged one maps only
    # this user): door-filter stops there, which is failing closed; what the
    # door holds after the drop is checked where 1000 exists.
    check("the door does not start" in ad,
          "door-filter stops when it cannot become 1000:1000 in this user namespace (uid 1000 is not mapped)", detail)
else:
    check(ad == "uid=1000 bnd=0 eff=0 nnp=1 nft=refused nft=refused root=refused",
          "after door-filter's drop the door is 1000 with an empty bounding and effective set and no_new_privs, "
          "cannot delete or change its table, and cannot become root again", detail)
check(lines.get("table3") == "2" and lines.get("web3") == "open"
      and str(lines.get("other3", "")).split() and all(g in BLOCKED for g in str(lines.get("other3", "")).split()),
      "and the table is still in place: the Host's 8800 open, its 8810 and the machine still dropped", detail)
check(r.returncode == 0, "the packet script ran to its end", detail)

print()
print("FAIL" if failed else "all compose door filter checks passed")
sys.exit(failed)
