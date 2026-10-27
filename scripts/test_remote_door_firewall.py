#!/usr/bin/env python3
"""Remote access's firewall (vm/wsl/rootfs/etc/cseries-remote/remote.nft,
loaded by cseries-remote-firewall.service, which the door's unit requires):
on the remote network the door reaches the Host's web port and nothing else,
and the Host's tunnel still reaches the door. Real packets, in private network
namespaces, so nothing on this machine's own ruleset is read or changed.

  1. The text: its own two tables, each replaced whole; no flush ruleset; the
     bridge chain's rules in order; its addresses and port are the network's,
     the door's and the Host's (cseries-remote.network, cseries-remote-door
     .container, cseries-guest); the bridge it names is the one the network
     quadlet names; the door's unit requires and follows the firewall's unit,
     which loads this file; both guest images copy the overlay that carries
     them.
  2. Packets. The namespace this runs in plays the guest, with a bridge like
     podman's for the remote network (cseries-remote0, 10.89.79.1/29) and a
     listener of the guest's own on every address (as sshd on the Lima
     guest). "host" plays the Host at 10.89.79.2, listening as the Host does
     on every address: 8800 (the web port), 8810 and 8811 (the bastion's),
     13129 (the apps' egress lane), 5432 (a port nothing publishes) and a UDP
     port. "door" plays the door at 10.89.79.3:8443. Two more namespaces sit
     on another bridge, which the rules must leave alone.
       - before the rules, the door reaches the Host's 8810 and 5432, the
         guest's own listener, and the Host's 8800 over IPv6 link-local (the
         gap this closes);
       - with them, the door reaches the Host's 8800, data both ways, and
         nothing else on the Host (8810, 8811, 13129, 5432, UDP, and 8800
         over IPv6 link-local), nor the guest;
       - the Host's tunnel still reaches the door's 8443, data both ways;
       - another bridge's containers still reach each other on any port;
       - loaded a second time, all of that holds, this time with the
         bridge's IPv4 traffic also passed through the IP hooks
         (br_netfilter's bridge-nf-call-iptables), as on a machine where
         another tool turned that on.

Needs nft, ip(8), python3 and a way into private namespaces: root's
`unshare -mn` (root in rootlesskit's user namespace counts, as on the forge
runner), or an unprivileged user namespace (`unshare -rmn`). Without them it
FAILS and says why: a skip reads as a pass. Mounts are made with mount(2),
not mount(8) (scripts/private_netns.py).

Container-free: python3 scripts/test_remote_door_firewall.py
"""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OVERLAY = os.path.join(ROOT, "vm/wsl/rootfs")
RULES = os.path.join(OVERLAY, "etc/cseries-remote/remote.nft")
FW_UNIT = os.path.join(OVERLAY, "etc/systemd/system/cseries-remote-firewall.service")
UNITS = os.path.join(OVERLAY, "etc/containers/systemd")
GUEST = os.path.join(OVERLAY, "usr/local/bin/cseries-guest")

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


def unit(path):
    """{section: [(key, value), ...]}, as systemd and Quadlet read a unit."""
    out, section = {}, None
    for raw in read(path).splitlines():
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


# ── 1. the text ─────────────────────────────────────────────────────────────

rules_text = read(RULES)
check(bool(rules_text), "remote.nft is in the guest's overlay at /etc/cseries-remote/remote.nft")
rules = "\n".join(ln for ln in rules_text.splitlines() if not ln.lstrip().startswith("#"))
check(not re.search(r"flush\s+ruleset", rules), "remote.nft never flushes the whole ruleset")
for fam in ("bridge", "inet"):
    check(re.search(rf"^table {fam} cseries_remote\ndelete table {fam} cseries_remote\n", rules, re.M) is not None,
          f"remote.nft replaces its own {fam} table and nothing else")
tables = re.findall(r"^table (\w+) (\w+) \{", rules, re.M)
check(sorted(tables) == [("bridge", "cseries_remote"), ("inet", "cseries_remote")],
      "remote.nft holds exactly its two tables", tables)
defs = dict(re.findall(r"^define (\w+) = (\S+)$", rules, re.M))
br = re.search(r"table bridge cseries_remote \{\s*chain forward \{(.*?)\n\t\}", rules, re.S)
br_rules = [ln.strip() for ln in (br.group(1) if br else "").splitlines() if ln.strip()]
check(br_rules == [
    "type filter hook forward priority filter; policy accept;",
    'meta ibrname != "cseries-remote0" accept',
    "ether type arp accept",
    "ip saddr $REMOTE_HOST accept",
    "ip saddr $REMOTE_DOOR ip daddr $REMOTE_HOST tcp dport $REMOTE_HOST_PORT accept",
    "ip saddr $REMOTE_DOOR ip daddr $REMOTE_HOST tcp flags & (syn | ack) != syn accept",
    "drop",
], "the bridge chain: other bridges untouched, then ARP, the Host, the door to the Host's web port, "
   "the door's side of the Host's connections, and nothing else", br_rules)
check(set(re.findall(r'"([^"]+)"', rules)) == {"cseries-remote0"},
      "every interface remote.nft names is the remote network's bridge", re.findall(r'"([^"]+)"', rules))
inp = re.search(r"chain input \{(.*?)\n\t\}", rules, re.S)
inp_rules = [ln.strip() for ln in (inp.group(1) if inp else "").splitlines() if ln.strip()]
check(inp_rules == ["type filter hook input priority filter; policy accept;", 'iifname "cseries-remote0" drop'],
      "nothing from the remote network reaches the guest itself", inp_rules)

q_net = unit(os.path.join(UNITS, "cseries-remote.network"))
q_door = unit(os.path.join(UNITS, "cseries-remote-door.container"))
door_env = {}
for v in values(q_door, "Container", "Environment"):
    k, _, val = v.partition("=")
    door_env[k] = val
door_ip = next((n.split(":ip=", 1)[1] for n in values(q_door, "Container", "Network")
                if n.startswith("cseries-remote.network:ip=")), "")
guest = read(GUEST)
g_host = (re.findall(r"^REMOTE_HOST_IP=(\S+)$", guest, re.M) or [""])[0]
g_door = (re.findall(r"^REMOTE_DOOR_IP=(\S+)$", guest, re.M) or [""])[0]
check(values(q_net, "Network", "Subnet") == [defs.get("REMOTE_NET")],
      "remote.nft's network is cseries-remote.network's subnet", (defs.get("REMOTE_NET"), values(q_net, "Network", "Subnet")))
check(defs.get("REMOTE_DOOR") == door_ip == door_env.get("CSERIES_DOOR_BIND") == g_door != "",
      "remote.nft's door is the door unit's address and bind, and cseries-guest's", (defs.get("REMOTE_DOOR"), door_ip, g_door))
check(defs.get("REMOTE_HOST") == door_env.get("CSERIES_TUNNEL_FROM") == g_host != "" and
      door_env.get("CSERIES_HOST_UPSTREAM") == f"{defs.get('REMOTE_HOST')}:{defs.get('REMOTE_HOST_PORT')}",
      "remote.nft's Host and port are what the door passes to (CSERIES_HOST_UPSTREAM) and believes "
      "(CSERIES_TUNNEL_FROM), and cseries-guest's Host address",
      (defs.get("REMOTE_HOST"), defs.get("REMOTE_HOST_PORT"), door_env.get("CSERIES_HOST_UPSTREAM"), g_host))
check(defs.get("REMOTE_HOST_PORT") == "8800", "the one port the door reaches is the Host's web port, 8800")
check("--interface-name=cseries-remote0" in values(q_net, "Network", "PodmanArgs"),
      "cseries-remote.network names its bridge cseries-remote0, the bridge remote.nft matches",
      values(q_net, "Network", "PodmanArgs"))
check("cseries-remote-firewall.service" in values(q_door, "Unit", "Requires") and
      "cseries-remote-firewall.service" in values(q_door, "Unit", "After"),
      "the door's unit requires and follows remote access's firewall, so it never runs without it")
q_fw = unit(FW_UNIT)
check(values(q_fw, "Service", "ExecStart") == ["/usr/sbin/nft -f /etc/cseries-remote/remote.nft"] and
      values(q_fw, "Service", "Type") == ["oneshot"] and values(q_fw, "Service", "RemainAfterExit") == ["yes"] and
      not values(q_fw, "Service", "ExecStop"),
      "cseries-remote-firewall.service loads remote.nft once and leaves it in place when stopped")
for name, rel, src in (("WSL", "vm/wsl/customize.sh", "/src/rootfs/."), ("Lima", "vm/lima/customize.sh", "/src-wsl/rootfs/.")):
    check(f'cp -R {src} "$root/"' in read(os.path.join(ROOT, rel)),
          f"the {name} image copies the overlay that carries remote.nft and its unit")

# ── 2. packets ──────────────────────────────────────────────────────────────

nft = shutil.which("nft") or ("/usr/sbin/nft" if os.path.exists("/usr/sbin/nft") else None)
ip = shutil.which("ip") or ("/usr/sbin/ip" if os.path.exists("/usr/sbin/ip") else None)
unshare = shutil.which("unshare")
missing = [n for n, p in (("nft", nft), ("ip", ip), ("unshare", unshare)) if not p]
if missing:
    check(False, f"this gate needs {', '.join(missing)} on PATH, so the packets are not checked")
    sys.exit(failed)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from private_netns import MOUNTNETNS, MOUNTS, no_namespace_why  # noqa: E402

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
                c.sendall(c.recv(64))
            except OSError:
                pass
            c.close()
        else:
            d, a = key.fileobj.recvfrom(512); key.fileobj.sendto(d, a)
'''

PROBE = r'''
import socket, sys
out = []
for spec in sys.argv[1:]:
    proto, addr, port = spec.split("|")
    fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
    target = (addr, int(port), 0, socket.if_nametoindex("eth0")) if fam == socket.AF_INET6 else (addr, int(port))
    try:
        if proto == "tcp":
            s = socket.socket(fam); s.settimeout(1.5); s.connect(target)
            s.sendall(b"knock"); got = s.recv(16); s.close()
            out.append("open" if got == b"knock" else "no-echo")
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

# What a blocked connection looks like from the door: no answer at all, or no
# neighbour (IPv6's neighbour discovery is dropped with the rest of IPv6).
# "refused" is not blocked: a reset came back, so the SYN got there.
BLOCKED = ("dropped", "error:113")
HOST_OTHER = ["tcp|10.89.79.2|8810", "tcp|10.89.79.2|8811", "tcp|10.89.79.2|13129",
              "tcp|10.89.79.2|5432", "udp|10.89.79.2|5353", "tcp|fe80::2|8800"]
script = f"""set -eu
python3 -I -c '{MOUNTS}'
ip link set lo up
ip netns add host
ip netns add door
ip netns add a
ip netns add b
# the remote network, as podman makes it: a bridge with the gateway's address
ip link add cseries-remote0 type bridge
ip addr add 10.89.79.1/29 dev cseries-remote0
ip link set cseries-remote0 up
port() {{  # bridge, namespace, address, link-local
    ip link add "v$2" type veth peer name eth0 netns "$2"
    ip link set "v$2" master "$1"
    ip link set "v$2" up
    ip netns exec "$2" sh -c "ip link set lo up; ip addr add $3 dev eth0; ip link set eth0 up"
    [ -z "$4" ] || ip netns exec "$2" ip -6 addr add "$4/64" dev eth0 nodad
}}
pids=""
port cseries-remote0 host 10.89.79.2/29 fe80::2
port cseries-remote0 door 10.89.79.3/29 fe80::3
# another bridge (the Host's own network, say), which the rules leave alone
ip link add podman3 type bridge
ip link set podman3 up
port podman3 a 10.89.5.2/24 ""
port podman3 b 10.89.5.3/24 ""
ip netns exec host timeout 150 python3 -I -c '{LISTEN}' 'tcp|::|8800' 'tcp|0.0.0.0|8810' 'tcp|0.0.0.0|8811' 'tcp|::|13129' 'tcp|0.0.0.0|5432' 'udp|0.0.0.0|5353' &
pids="$pids $!"
ip netns exec door timeout 150 python3 -I -c '{LISTEN}' 'tcp|10.89.79.3|8443' &
pids="$pids $!"
ip netns exec b timeout 150 python3 -I -c '{LISTEN}' 'tcp|0.0.0.0|8810' &
pids="$pids $!"
timeout 150 python3 -I -c '{LISTEN}' 'tcp|0.0.0.0|2222' &
pids="$pids $!"
sleep 1.5
fromdoor() {{ ip netns exec door python3 -I -c '{PROBE}' "$@"; }}
echo "before: $(fromdoor 'tcp|10.89.79.2|8810' 'tcp|10.89.79.2|5432' 'tcp|10.89.79.1|2222' 'tcp|fe80::2|8800')"
for round in 1 2; do
    if [ "$round" = 2 ]; then
        if [ -w /proc/sys/net/bridge/bridge-nf-call-iptables ]; then
            echo 1 > /proc/sys/net/bridge/bridge-nf-call-iptables
            echo "brnf2: on"
        else
            echo "brnf2: unavailable"
        fi
    fi
    {nft} -f {RULES}
    echo "web$round: $(fromdoor 'tcp|10.89.79.2|8800')"
    echo "other$round: $(fromdoor {" ".join(repr(s) for s in HOST_OTHER)})"
    echo "guest$round: $(fromdoor 'tcp|10.89.79.1|2222')"
    echo "tunnel$round: $(ip netns exec host python3 -I -c '{PROBE}' 'tcp|10.89.79.3|8443')"
    echo "elsewhere$round: $(ip netns exec a python3 -I -c '{PROBE}' 'tcp|10.89.5.3|8810')"
done
# The listeners hold this script's output open until they end.
kill $pids 2>/dev/null || true
wait 2>/dev/null || true
"""
r = subprocess.run(MOUNTNETNS + ["sh", "-c", script], capture_output=True, text=True, check=False)
out = r.stdout
lines = dict(ln.split(": ", 1) for ln in out.splitlines() if ": " in ln)
detail = f"\n--- stdout\n{out}\n--- stderr\n{r.stderr[-1500:]}"

check(lines.get("before") == "open open open open",
      "without the rules the door reaches the Host's 8810 and 5432, the guest itself, and the Host's 8800 over "
      "IPv6 link-local (the gap this closes)", detail)
for rnd in ("1", "2"):
    again = "" if rnd == "1" else ", loaded a second time"
    if rnd == "2" and lines.get("brnf2") == "on":
        again += " with br_netfilter passing the bridge's traffic through the IP hooks"
    check(lines.get(f"web{rnd}") == "open", f"the door reaches the Host's web port 8800, data both ways{again}", detail)
    got = str(lines.get(f"other{rnd}", "")).split()
    check(len(got) == len(HOST_OTHER) and all(g in BLOCKED for g in got),
          f"the door reaches nothing else on the Host: 8810, 8811, 13129, 5432, UDP, and 8800 over IPv6{again}",
          detail)
    check(lines.get(f"guest{rnd}") in BLOCKED, f"the door does not reach the guest itself{again}", detail)
    check(lines.get(f"tunnel{rnd}") == "open",
          f"the Host's tunnel reaches the door's 8443, data both ways{again}", detail)
    check(lines.get(f"elsewhere{rnd}") == "open",
          f"two containers on another bridge still reach each other on any port{again}", detail)
check(r.returncode == 0, "the packet script ran to its end", detail)

print()
print("FAIL" if failed else "all remote door firewall checks passed")
sys.exit(failed)
