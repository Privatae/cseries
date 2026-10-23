#!/usr/bin/env python3
"""The machine wall (vm/wsl/rootfs/etc/cseries-machine/machine.nft, loaded by
usr/local/sbin/cseries-machine-firewall): from a container in the C-Series
Linux system, nothing on the machine under the VM answers, and what the
product needs still works. Real packets, in private network namespaces, so
nothing on this machine's own ruleset is read or changed.

  1. The text: its own table, replaced whole; no flush ruleset; the forward
     chain accepts established traffic first, then DNS to the resolvers, then
     drops the machine; the Lima image's first addresses are Lima's
     user-mode network.
  2. The loader prints a ruleset `nft -c` takes, with this namespace's default
     gateway, the resolvers and the addresses file; a line that is not an
     address is skipped and said.
  3. Packets. The namespace this runs in plays the guest (eth0 192.168.5.15,
     default via 192.168.5.2, as Lima's user-mode network gives it), with a
     bridge like podman's (10.89.0.1/24, masqueraded out of eth0) and a
     container on it (10.89.0.2). "machine" plays the machine: 192.168.5.2
     with the services measured reachable before (22, 7877, 7878, 7978,
     8800), 192.168.5.3 answering DNS, and its LAN address 192.168.1.20 with
     SSH. "lan" plays the LAN and the internet: a model server at
     192.168.1.50:11434 and a web server at 198.51.100.10:443.
       - before the wall, the container reaches 192.168.5.2:22 (the gap is
         there to close);
       - with it, every machine port is dropped, the machine's LAN address
         too, and 192.168.5.3 answers on 53 and nothing else;
       - the LAN's model server and the internet still answer;
       - the machine still reaches the container on a forwarded port (the
         bastion's lane, 8811), and the replies get back;
       - the plain proxy's user (uid 13) cannot reach the machine's
         addresses; root's own connections from the guest still can (the
         wall is about the containers, not the guest's own processes);
       - loaded a second time, all of that holds.

Needs nft, ip, python3 and root's `unshare -mn`. Without them it says so and
skips, unless CSERIES_REQUIRE_NFT=1, which makes that a failure.

Container-free: python3 scripts/test_vm_machine_firewall.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RULES = os.path.join(ROOT, "vm/wsl/rootfs/etc/cseries-machine/machine.nft")
LOADER = os.path.join(ROOT, "vm/wsl/rootfs/usr/local/sbin/cseries-machine-firewall")
LIMA_ADDRS = os.path.join(ROOT, "vm/lima/rootfs/etc/cseries-machine/addresses")

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


with open(RULES) as f:
    rules_text = f.read()
rules = "\n".join(line for line in rules_text.splitlines() if not line.lstrip().startswith("#"))

# 1. the text
check(not re.search(r"flush\s+ruleset", rules), "machine.nft never flushes the whole ruleset")
check(re.search(r"^table inet cseries_machine\ndelete table inet cseries_machine\n", rules, re.M) is not None,
      "machine.nft replaces its own table and nothing else")
fwd = re.search(r"chain forward \{(.*?)\n\t\}", rules, re.S)
fwd_rules = [ln.strip() for ln in (fwd.group(1) if fwd else "").splitlines() if ln.strip()]
check(fwd_rules[:2] == ["type filter hook forward priority filter; policy accept;",
                        "ct state established,related accept"],
      "the forward chain accepts established traffic before anything else", fwd_rules[:2])
check(any("@dns_v4" in ln and "dport 53 accept" in ln for ln in fwd_rules) and
      fwd_rules.index(next(ln for ln in fwd_rules if "@dns_v4" in ln)) <
      fwd_rules.index(next(ln for ln in fwd_rules if "@machine_v4" in ln)),
      "DNS to the resolvers is admitted before the machine is dropped")
check(all(re.search(r"\b(accept|drop)$", ln) for ln in fwd_rules[2:]),
      "every forward rule ends in accept or drop (no jump out of the table)")
with open(LIMA_ADDRS) as f:
    lima_lines = [ln for ln in f.read().splitlines() if ln and not ln.startswith("#")]
check(lima_lines == ["block 192.168.5.0/24", "dns 192.168.5.3"],
      "the Lima image's first addresses are Lima's user-mode network and its DNS", lima_lines)
with open(os.path.join(ROOT, "vm/lima/customize.sh")) as f:
    lima_c = f.read()
with open(os.path.join(ROOT, "vm/wsl/customize.sh")) as f:
    wsl_c = f.read()
for name, text in (("Lima", lima_c), ("WSL", wsl_c)):
    check("systemctl enable cseries-machine-firewall.service" in text and
          "usr/local/sbin/cseries-machine-firewall" in text,
          f"the {name} image enables the wall's unit and makes its loader runnable")

nft = shutil.which("nft") or ("/usr/sbin/nft" if os.path.exists("/usr/sbin/nft") else None)
ip = shutil.which("ip") or ("/usr/sbin/ip" if os.path.exists("/usr/sbin/ip") else None)
unshare = shutil.which("unshare")
missing = [n for n, p in (("nft", nft), ("ip", ip), ("unshare", unshare)) if not p]
if os.geteuid() != 0:
    missing.append("root")
if missing:
    msg = f"no {', '.join(missing)} here, so the loader and the packets are not checked"
    if os.environ.get("CSERIES_REQUIRE_NFT") == "1":
        check(False, msg)
    else:
        print(f"SKIP  {msg}")
    sys.exit(failed)

work = tempfile.mkdtemp(prefix="cseries-machine-wall-")
groot = os.path.join(work, "guest")
os.makedirs(os.path.join(groot, "etc/cseries-machine"))
shutil.copy(RULES, os.path.join(groot, "etc/cseries-machine/machine.nft"))
with open(os.path.join(groot, "etc/cseries-machine/addresses"), "w") as f:
    # What the cseries command writes for a Lima guest on a machine whose LAN
    # address is 192.168.1.20 (invented), with one line it must skip.
    f.write("# invented for the test\nblock 192.168.5.0/24\ndns 192.168.5.3\nblock 192.168.1.20\n"
            "block 2001:db8::20\nblock not-an-address\n")
with open(os.path.join(groot, "etc/resolv.conf"), "w") as f:
    f.write("nameserver 192.168.5.3\n")

LISTEN = r'''
import selectors, socket, sys, time
sel = selectors.DefaultSelector()
for spec in sys.argv[1:]:
    proto, addr, port = spec.split(":")
    if proto == "tcp":
        s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((addr, int(port))); s.listen(16); s.setblocking(False)
        sel.register(s, selectors.EVENT_READ, "tcp")
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind((addr, int(port)))
        sel.register(s, selectors.EVENT_READ, "udp")
end = time.time() + 120
while time.time() < end:
    for key, _ in sel.select(1):
        if key.data == "tcp":
            c, _ = key.fileobj.accept(); c.close()
        else:
            d, a = key.fileobj.recvfrom(512); key.fileobj.sendto(d, a)
'''

PROBE = r'''
import os, socket, sys
uid = int(os.environ.get("PROBE_UID", "0"))
if uid:
    os.setgid(uid); os.setuid(uid)
out = []
for spec in sys.argv[1:]:
    proto, addr, port = spec.split(":")
    try:
        if proto == "tcp":
            s = socket.create_connection((addr, int(port)), 1.5); s.close(); out.append("open")
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(1.5)
            s.sendto(b"q", (addr, int(port))); s.recvfrom(16); out.append("open")
    except socket.timeout:
        out.append("dropped")
    except ConnectionRefusedError:
        out.append("refused")
    except OSError as e:
        out.append("error:" + str(e.errno))
print(" ".join(out))
'''

MACHINE_PORTS = ["22", "7877", "7878", "7978", "8800"]
script = f"""set -eu
mkdir -p /run/netns
mount -t tmpfs none /run/netns
ip link set lo up
ip netns add machine
ip netns add lan
ip netns add ctr
# guest (this namespace) <-> machine: Lima's user-mode network
ip link add eth0 type veth peer name em netns machine
ip addr add 192.168.5.15/24 dev eth0
ip link set eth0 up
ip netns exec machine sh -c 'ip link set lo up; ip addr add 192.168.5.2/24 dev em; ip addr add 192.168.5.3/24 dev em; ip link set em up; sysctl -qw net.ipv4.ip_forward=1'
ip route add default via 192.168.5.2
sysctl -qw net.ipv4.ip_forward=1
# machine <-> lan: the machine's LAN address, a neighbour, and the internet beyond
ip netns exec machine ip link add el type veth peer name ln netns lan
ip netns exec machine sh -c 'ip addr add 192.168.1.20/24 dev el; ip link set el up; ip route add 198.51.100.0/24 via 192.168.1.50'
ip netns exec lan sh -c 'ip link set lo up; ip addr add 192.168.1.50/24 dev ln; ip link set ln up; ip addr add 198.51.100.10/32 dev lo; ip route add default via 192.168.1.20'
# a bridge like podman's, masqueraded out of eth0, and a container on it
ip link add podman1 type bridge
ip addr add 10.89.0.1/24 dev podman1
ip link set podman1 up
ip link add vc type veth peer name eth0 netns ctr
ip link set vc master podman1
ip link set vc up
ip netns exec ctr sh -c 'ip link set lo up; ip addr add 10.89.0.2/24 dev eth0; ip link set eth0 up; ip route add default via 10.89.0.1'
{nft} -f - <<'NFT'
table ip podman_sim {{
	chain post {{
		type nat hook postrouting priority srcnat; policy accept;
		ip saddr 10.89.0.0/24 oifname "eth0" masquerade
	}}
	chain pre {{
		type nat hook prerouting priority dstnat; policy accept;
		iifname "eth0" tcp dport 8811 dnat to 10.89.0.2:8811
	}}
}}
NFT
ip netns exec machine timeout 120 python3 -I -c '{LISTEN}' {" ".join(f"tcp:192.168.5.2:{p}" for p in MACHINE_PORTS)} tcp:192.168.5.3:53 tcp:192.168.5.3:22 udp:192.168.5.3:53 tcp:192.168.1.20:22 &
ip netns exec lan timeout 120 python3 -I -c '{LISTEN}' tcp:192.168.1.50:11434 tcp:198.51.100.10:443 &
ip netns exec ctr timeout 120 python3 -I -c '{LISTEN}' tcp:10.89.0.2:8811 &
sleep 1
probe() {{ ip netns exec ctr python3 -I -c '{PROBE}' "$@"; }}
echo "before: $(probe tcp:192.168.5.2:22)"
load() {{ CSERIES_GUEST_ROOT={groot} CSERIES_MACHINE_NO_WAIT=1 sh {LOADER} "$@"; }}
load print > {work}/printed.nft 2> {work}/print.err
{nft} -c -f {work}/printed.nft && echo "check: ok"
for round in 1 2; do
    load load
    echo "machine$round: $(probe {" ".join(f"tcp:192.168.5.2:{p}" for p in MACHINE_PORTS)})"
    echo "lanaddr$round: $(probe tcp:192.168.1.20:22)"
    echo "dns$round: $(probe udp:192.168.5.3:53 tcp:192.168.5.3:53 tcp:192.168.5.3:22)"
    echo "kept$round: $(probe tcp:192.168.1.50:11434 tcp:198.51.100.10:443)"
    echo "inbound$round: $(ip netns exec machine python3 -I -c '{PROBE}' tcp:192.168.5.15:8811)"
    echo "proxyuser$round: $(PROBE_UID=13 python3 -I -c '{PROBE}' tcp:192.168.1.20:22 tcp:192.168.5.2:22)"
    echo "guestroot$round: $(python3 -I -c '{PROBE}' tcp:192.168.1.20:22)"
done
{nft} list set inet cseries_machine machine_v4 | tr -d '\\n\\t' | sed 's/^/set: /'; echo
"""
r = subprocess.run(["unshare", "-mn", "sh", "-c", script], capture_output=True, text=True, check=False)
out = r.stdout
lines = dict(ln.split(": ", 1) for ln in out.splitlines() if ": " in ln)
detail = f"\n--- stdout\n{out}\n--- stderr\n{r.stderr}"

def slurp(name):
    try:
        with open(os.path.join(work, name)) as f:
            return f.read()
    except OSError:
        return ""


printed = slurp("printed.nft")
print_err = slurp("print.err")
shutil.rmtree(work, ignore_errors=True)

# 2. the loader
check("add element inet cseries_machine machine_v4 { 192.168.1.20,192.168.5.0/24,192.168.5.2 }" in printed,
      "the loader adds the default gateway and the machine's addresses, each once", printed[-400:])
check("add element inet cseries_machine dns_v4 { 192.168.5.3 }" in printed,
      "the resolvers in resolv.conf and the addresses file are admitted on 53")
check("add element inet cseries_machine machine_v6 { 2001:db8::20 }" in printed,
      "an IPv6 address of the machine is walled off too")
check("skipped block not-an-address" in print_err, "a line that is not an address is skipped, and said", print_err)
check(lines.get("check") == "ok", "nft -c takes what the loader prints", detail)

# 3. packets
check(lines.get("before") == "open",
      "without the wall the container reaches 192.168.5.2:22 (the gap this closes)", detail)
for rnd in ("1", "2"):
    again = "" if rnd == "1" else ", loaded a second time"
    check(lines.get(f"machine{rnd}") == " ".join(["dropped"] * len(MACHINE_PORTS)),
          f"every port measured open on the machine's loopback (192.168.5.2) is dropped from the container{again}",
          detail)
    check(lines.get(f"lanaddr{rnd}") == "dropped", f"the machine's LAN address :22 is dropped{again}", detail)
    check(lines.get(f"dns{rnd}") == "open open dropped",
          f"192.168.5.3 answers DNS on udp and tcp 53, and nothing else{again}", detail)
    check(lines.get(f"kept{rnd}") == "open open",
          f"a model server on the LAN and the internet still answer{again}", detail)
    check(lines.get(f"inbound{rnd}") == "open",
          f"the machine still reaches a forwarded port in the container (the bastion's lane){again}", detail)
    check(lines.get(f"proxyuser{rnd}") == "dropped dropped",
          f"the plain proxy's user (uid 13) cannot reach the machine{again}", detail)
    check(lines.get(f"guestroot{rnd}") == "open",
          f"the guest's own root processes are not walled{again}", detail)

sys.exit(failed)
