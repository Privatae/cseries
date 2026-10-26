#!/usr/bin/env python3
"""The cloud box's firewall files (vm/cloud/overlay/etc/cseries-cloud/nftables/)
load in nft, checked against the kernel with `nft -c` inside a network
namespace of their own, so nothing on this machine's ruleset is read or
changed.

  1. door.nft loads, twice in a row (its declare, delete, define reload).
  2. bastion.nft loads on top of it; its cgroup path is swapped for one that
     exists here, since nft resolves a cgroup when the rule is loaded.
  3. Loaded for real in that namespace: the pairing sets take an element with
     a timeout (the helper's add, delete, add), the SSH rule cseries-firewall
     adds before the seal and flushes at it.
  4. The text holds what the posture needs: input policy drop, no SSH rule of
     its own, 8810 only from the pairing sets, the rate meter and its shared
     fallback, and no `flush ruleset`.
  5. Real packets in network namespaces: the pairing window and a loopback
     DNAT; and the meter when its sets are full, where new sources share one
     budget instead of going unmetered.

Needs nft, ip(8) and a way into private namespaces: root's `unshare -mn`
(root in rootlesskit's user namespace counts, as on the forge runner), or an
unprivileged user namespace (`unshare -rmn`). Without them it FAILS and says
why: a skip reads as a pass. It never runs mount(8): the forge runner's unit
makes /usr/bin/mount inaccessible (InaccessiblePaths), so the packet tests
make their two mounts with mount(2) (MOUNTS below).

Container-free: python3 scripts/test_cloud_box_nft.py
"""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
NFT_DIR = os.path.join(ROOT, "vm/cloud/overlay/etc/cseries-cloud/nftables")
DOOR = os.path.join(NFT_DIR, "door.nft")
BASTION = os.path.join(NFT_DIR, "bastion.nft")

failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


with open(DOOR) as f:
    door = f.read()
with open(BASTION) as f:
    bastion = f.read()

# 4. the text
rules = "\n".join(line for line in door.splitlines() if not line.lstrip().startswith("#"))
check(not re.search(r"flush\s+ruleset", rules), "door.nft never flushes the whole ruleset")
check(re.search(r"^table inet cseries_door\ndelete table inet cseries_door\n", rules, re.M) is not None,
      "door.nft replaces its own table and nothing else")
check(re.search(r"hook input priority filter; policy drop;", rules) is not None, "input's policy is drop")
check(not re.search(r"dport\s+22\b", rules), "door.nft has no SSH rule of its own")
check(re.search(r"tcp dport 8810 tcp dport @lanes_open ip6 saddr @pairing_v6 accept", rules) is not None and
      re.search(r"tcp dport 8810 tcp dport @lanes_open ip saddr @pairing_v4 dnat", rules) is not None and
      re.search(r"ct status dnat tcp dport 8810 tcp dport @lanes_open ip saddr @pairing_v4 accept", rules)
      is not None,
      "8810 is admitted only from the pairing sets, and only while the Host's ports are open")
# A machine whose vault is locked runs no Host: every rule that admits or
# redirects 8810 or 8811 names lanes_open, which is empty until the unlock.
opens_lanes = [ln.strip() for ln in rules.splitlines()
               if re.search(r"\b88(10|11)\b", ln) and re.search(r"\b(accept|dnat)\b", ln)]
check(len(opens_lanes) == 6 and all("tcp dport @lanes_open" in ln for ln in opens_lanes),
      "every rule that admits or redirects 8810 or 8811 needs the Host's ports open (lanes_open)",
      opens_lanes)
check(re.search(r"set lanes_open \{\s*type inet_service\s*\}", rules) is not None,
      "lanes_open is a set of ports, empty when door.nft loads")
# Every rule naming 8810 that admits or redirects it names a pairing set in
# the same rule: the forward chain checks the window again, so a DNAT to the
# Host's 8810 that door_dnat did not make (netavark's) admits nothing.
opens_8810 = [ln.strip() for ln in rules.splitlines()
              if re.search(r"\b8810\b", ln) and re.search(r"\b(accept|dnat)\b", ln)]
check(opens_8810 and all("@pairing_v" in ln for ln in opens_8810),
      "no rule admits or redirects 8810 without naming a pairing set", opens_8810)
check(re.search(r"hook prerouting priority -150;[^}]*\biif lo accept\s+ip daddr 127\.0\.0\.0/8 drop\s+"
                r"ip6 daddr ::1 drop", rules) is not None,
      "before any NAT, traffic from outside addressed to loopback is dropped")
check("limit rate over 20/second burst 100 packets" in rules, "new connections are metered per source")
check(re.search(r"ip saddr != @conn_v4 jump door_overflow", rules) is not None and
      re.search(r"ip6 saddr & ffff:ffff:ffff:ffff:: != @conn_v6 jump door_overflow", rules) is not None and
      re.search(r"chain door_overflow \{\s*limit rate over 100/second burst 200 packets drop\s*\}", rules)
      is not None,
      "a source a full meter set cannot hold shares one limit (100/s, burst 200)")
check(re.search(r'socket cgroupv2 level 2 "system.slice/privatae-bastion.service" jump bastion_vet', bastion)
      is not None, "bastion.nft sends the bastion's cgroup through bastion_vet")

nft = shutil.which("nft") or ("/usr/sbin/nft" if os.path.exists("/usr/sbin/nft") else None)
unshare = shutil.which("unshare")


# Root (or root in a user namespace, as under rootlesskit) makes the
# namespaces itself; anyone else asks for a user namespace of their own, where
# it is root.
NS_ROOT = os.geteuid() == 0
NETNS = ["unshare", "-n"] if NS_ROOT else ["unshare", "-rn"]
MOUNTNETNS = ["unshare", "-mn"] if NS_ROOT else ["unshare", "-rmn"]


def no_namespace_why(stderr):
    """Why no private namespace could be made, for the failure message."""
    why = (stderr or "").strip().splitlines()[-1:] or ["unshare failed"]
    hint = ""
    try:
        with open("/proc/sys/kernel/apparmor_restrict_unprivileged_userns") as f:
            if f.read().strip() == "1" and not NS_ROOT:
                hint = (" (kernel.apparmor_restrict_unprivileged_userns=1 refuses a user namespace "
                        "to an unprivileged process; run it as root, or under rootlesskit as the "
                        "forge runner's gate lane does)")
    except OSError:
        pass
    return f"no private namespace can be made here: {why[0]}{hint}"


def in_netns(script):
    """Run a shell script in a new network namespace; (returncode, output),
    or (None, why) when no namespace can be made."""
    r = subprocess.run(NETNS + ["sh", "-c", script], capture_output=True, text=True, check=False)
    if r.returncode != 0 and "unshare" in r.stderr and (
            "Operation not permitted" in r.stderr or "uid_map" in r.stderr):
        return None, no_namespace_why(r.stderr)
    return r.returncode, r.stdout + r.stderr


missing = [n for n, p in (("nft", nft), ("unshare", unshare), ("ip", shutil.which("ip"))) if not p]
if missing:
    check(False, f"this gate needs {', '.join(missing)} on PATH")
    sys.exit(failed)
usable = in_netns(f"{nft} list ruleset")
if usable[0] is None:
    check(False, f"the nft load checks: {usable[1]}")
    sys.exit(failed)

# A cgroup that exists on this machine, at level 1, in place of the service's.
probe = "user.slice" if os.path.isdir("/sys/fs/cgroup/user.slice") else "init.scope"
bastion_here = bastion.replace('level 2 "system.slice/privatae-bastion.service"', f'level 1 "{probe}"')
check(bastion_here != bastion, "the bastion rule's cgroup can be swapped for one that exists here")

# 1-2. check mode
rc, out = in_netns(f"{nft} -c -f {DOOR}")
check(rc == 0, "nft -c -f door.nft", out)
rc, out = in_netns(f"{nft} -f {DOOR} && {nft} -f {DOOR} && {nft} list table inet cseries_door >/dev/null")
check(rc == 0, "door.nft loads twice in a row (it replaces its own table)", out)
script = f"{nft} -f {DOOR} && cat <<'NFT' | {nft} -c -f -\n{bastion_here}\nNFT\n"
rc, out = in_netns(script)
check(rc == 0, "nft -c -f bastion.nft, on top of door.nft", out)

# 3. what the helper and cseries-firewall do to the loaded table
script = f"""set -e
{nft} -f {DOOR}
{nft} -f - <<'NFT'
add element inet cseries_door pairing_v4 {{ 203.0.113.9 }}
delete element inet cseries_door pairing_v4 {{ 203.0.113.9 }}
add element inet cseries_door pairing_v4 {{ 203.0.113.9 timeout 900s }}
add element inet cseries_door pairing_v6 {{ 2001:db8::9 timeout 900s }}
NFT
{nft} list set inet cseries_door pairing_v4 | grep -q '203.0.113.9 expires 1[45]m'
{nft} add rule inet cseries_door pre_seal tcp dport 22 accept
{nft} list chain inet cseries_door pre_seal | grep -q 'tcp dport 22 accept'
{nft} flush chain inet cseries_door pre_seal
! {nft} list chain inet cseries_door pre_seal | grep -q 'dport 22'
"""
rc, out = in_netns(script)
check(rc == 0, "a pairing window takes, and the pre-seal SSH rule comes and goes", out)

# 5. Real packets, in three private network namespaces: this one is the box
# (door.nft loaded), "out" is a neighbour on its network, "host" is the Host
# container at 10.89.77.2. Beside door.nft sits a DNAT of 127.0.0.1:8810 to
# the Host, the shape of netavark's rule for the quadlet's loopback publish.
# A SYN the neighbour addresses to 127.0.0.1:8810 must not reach the Host;
# one to the box's address reaches it only from an address in pairing_v4.
INJECT = r'''
import socket, struct, sys
ifname, dmac, sip, dip, dport = sys.argv[1:6]
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
s.bind((ifname, 0))
def csum(b):
    b += b"\0" * (len(b) % 2)
    t = sum(struct.unpack("!%dH" % (len(b) // 2), b))
    while t >> 16:
        t = (t & 0xffff) + (t >> 16)
    return ~t & 0xffff
src, dst = socket.inet_aton(sip), socket.inet_aton(dip)
tcp = struct.pack("!HHIIBBHHH", 40000, int(dport), 1, 0, 5 << 4, 0x02, 64240, 0, 0)
tcp = tcp[:16] + struct.pack("!H", csum(src + dst + struct.pack("!BBH", 0, 6, len(tcp)) + tcp)) + tcp[18:]
ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 1, 0, 64, 6, 0, src, dst)
ip = ip[:10] + struct.pack("!H", csum(ip)) + ip[12:]
s.send(bytes.fromhex(dmac.replace(":", "")) + s.getsockname()[4] + b"\x08\x00" + ip + tcp)
'''


LISTEN = ("import socket\n"
          "s = socket.socket()\n"
          "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
          "s.bind(('0.0.0.0', 8810))\n"
          "s.listen(1)\n"
          "c, _ = s.accept()\n"
          "c.sendall(b'ok')\n"
          "c.close()\n").replace("'", '"')
DIAL = ("import socket\n"
        "s = socket.create_connection(('127.0.0.1', 8810), 3)\n"
        "print(s.recv(2).decode())\n").replace("'", '"')

# The two mounts each packet test makes in its own mount namespace, with
# mount(2): every mount stays private to it, and /run/netns is a tmpfs of its
# own, so `ip netns add` neither needs the machine's /run nor leaves anything
# there. Not mount(8): the forge runner's unit makes /usr/bin/mount
# inaccessible, and there the shell's `mount` failed with "Permission denied"
# (2026-10-26). Where /run/netns is missing and /run is read-only (a
# ProtectSystem=strict unit), /run gets the tmpfs and /run/netns is made in it.
MOUNTS = r'''
import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
MS_REC, MS_PRIVATE = 0x4000, 0x40000
def mount(src, target, fstype, flags, data):
    if libc.mount(src, target.encode(), fstype, flags, data) != 0:
        sys.exit("mount(2) on " + target + ": " + os.strerror(ctypes.get_errno()))
mount(None, "/", None, MS_REC | MS_PRIVATE, None)
if not os.path.isdir("/run/netns"):
    mount(b"none", "/run", b"tmpfs", 0, b"mode=0755")
    os.mkdir("/run/netns")
mount(b"none", "/run/netns", b"tmpfs", 0, b"mode=0755")
'''


def packet_test(door_path):
    """SYNs that reached the Host's 8810, after each of: a SYN to
    127.0.0.1:8810, a SYN to the box's address with no window open, and one
    with a window open for the sender. (False, output) when it did not run
    through."""
    script = f"""set -eu
python3 -I -c '{MOUNTS}'
ip link set lo up
ip netns add out
ip netns add host
ip link add vo type veth peer name eo netns out
ip link add vh type veth peer name eh netns host
ip addr add 198.51.100.1/24 dev vo
ip addr add 10.89.77.1/24 dev vh
ip link set vo up
ip link set vh up
ip -n out link set lo up
ip -n out addr add 198.51.100.2/24 dev eo
ip -n out link set eo up
ip -n host link set lo up
ip -n host addr add 10.89.77.2/24 dev eh
ip -n host link set eh up
ip -n host route add default via 10.89.77.1
echo 1 > /proc/sys/net/ipv4/ip_forward
{nft} -f {door_path}
{nft} -f - <<'NFT'
table ip loopback_publish {{
	chain pre {{
		type nat hook prerouting priority dstnat; policy accept;
		fib daddr type local ip daddr 127.0.0.1 tcp dport 8810 dnat to 10.89.77.2:8810
	}}
	chain out {{
		type nat hook output priority -100; policy accept;
		ip daddr 127.0.0.1 tcp dport 8810 dnat to 10.89.77.2:8810
	}}
	chain post {{
		type nat hook postrouting priority srcnat; policy accept;
		ip saddr 127.0.0.0/8 ip daddr 10.89.77.2 masquerade
	}}
}}
NFT
echo 1 > /proc/sys/net/ipv4/conf/vh/route_localnet
ip netns exec host {nft} -f - <<'NFT'
table inet seen {{
	counter syn {{ }}
	chain input {{
		type filter hook input priority 0; policy accept;
		tcp dport 8810 tcp flags & (syn | ack) == syn counter name syn
	}}
}}
NFT
mac=$(ip -o link show vo | sed -n 's/.*link\\/ether \\([0-9a-f:]*\\).*/\\1/p')
seen() {{ sleep 0.3; ip netns exec host {nft} list counter inet seen syn | sed -n 's/.*packets \\([0-9]*\\).*/\\1/p'; }}
inject() {{ ip netns exec out python3 -I -c '{INJECT}' eo "$mac" 198.51.100.2 "$1" 8810; }}
inject 127.0.0.1
echo "loopback=$(seen)"
inject 198.51.100.1
echo "nowindow=$(seen)"
{nft} add element inet cseries_door pairing_v4 {{ 198.51.100.2 timeout 60s }}
inject 198.51.100.1
echo "lockedwindow=$(seen)"
{nft} add element inet cseries_door lanes_open {{ 8810, 8811 }}
inject 198.51.100.1
echo "window=$(seen)"
# The box's own use of the loopback publish (cseries-guest health curls
# 127.0.0.1:8800 the same way) still answers.
ip netns exec host timeout 10 python3 -I -c '{LISTEN}' &
lst=$!
sleep 0.5
echo "local=$(timeout 5 python3 -I -c '{DIAL}' 2>/dev/null || echo refused)"
kill "$lst" 2>/dev/null || true
wait 2>/dev/null || true
"""
    r = subprocess.run(MOUNTNETNS + ["sh", "-c", script], capture_output=True, text=True, check=False)
    got = dict(re.findall(r"^(loopback|nowindow|lockedwindow|window)=(\d+)$", r.stdout, re.M))
    local = re.search(r"^local=(\S+)$", r.stdout, re.M)
    if r.returncode != 0 or len(got) != 4 or not local:
        return False, (r.stdout + r.stderr)[-800:]
    # The counter only grows: each case is what it added.
    total = {k: int(v) for k, v in got.items()}
    return {"loopback": total["loopback"], "nowindow": total["nowindow"] - total["loopback"],
            "lockedwindow": total["lockedwindow"] - total["nowindow"],
            "window": total["window"] - total["lockedwindow"], "local": local.group(1)}, r.stdout


# The meter with full sets, real packets: this namespace is the box (door.nft
# loaded), "out" a network neighbour that sends SYNs to 443 from addresses of
# its choosing. A counter after door_meter (prerouting, priority -100) counts
# the SYNs it let through. Each burst is sent in a few milliseconds, so a
# budget of B after a burst of N lets about min(N, B) through.
FLOOD = r'''
import socket, struct, sys
ifname, dmac, sip, dip, count = sys.argv[1:6]
v6 = ":" in sip
fam = socket.AF_INET6 if v6 else socket.AF_INET
src, dst = socket.inet_pton(fam, sip), socket.inet_pton(fam, dip)
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
s.bind((ifname, 0))
eth = bytes.fromhex(dmac.replace(":", "")) + s.getsockname()[4]
def csum(b):
    b += b"\0" * (len(b) % 2)
    t = sum(struct.unpack("!%dH" % (len(b) // 2), b))
    while t >> 16:
        t = (t & 0xffff) + (t >> 16)
    return ~t & 0xffff
for i in range(int(count)):
    tcp = struct.pack("!HHIIBBHHH", 20000 + i, 443, 1, 0, 5 << 4, 0x02, 64240, 0, 0)
    if v6:
        pseudo = src + dst + struct.pack("!IxxxB", len(tcp), 6)
    else:
        pseudo = src + dst + struct.pack("!BBH", 0, 6, len(tcp))
    tcp = tcp[:16] + struct.pack("!H", csum(pseudo + tcp)) + tcp[18:]
    if v6:
        ip = struct.pack("!IHBB16s16s", 6 << 28, len(tcp), 6, 64, src, dst)
        s.send(eth + b"\x86\xdd" + ip + tcp)
    else:
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 1, 0, 64, 6, 0, src, dst)
        ip = ip[:10] + struct.pack("!H", csum(ip)) + ip[12:]
        s.send(eth + b"\x08\x00" + ip + tcp)
'''

# 65535 elements less what is in the set already, for a set the listing says
# holds "count N".
FILL = r'''
import ipaddress, re, sys
kind, listing, out = sys.argv[1:4]
m = re.search(r"count (\d+)", open(listing).read())
have = int(m.group(1)) if m else 0
base = ipaddress.ip_address("10.200.0.0" if kind == "v4" else "2001:db8:a::")
step = 1 if kind == "v4" else 1 << 64
with open(out, "w") as f:
    f.write("add element inet cseries_door conn_%s { " % kind)
    f.write(", ".join(str(base + i * step) for i in range(65535 - have)))
    f.write(" }\n")
'''


def overflow_test(door_path):
    """SYNs to 443 let through by door_meter in each phase, or (None|False,
    why). Phases: three sources of 100 each and one of 300 while the IPv4
    set has room; then, with it full, a new source of 300, and 100 from a
    source already in the set; then the same for IPv6 /64s. (False, output)
    when it did not run through."""
    script = f"""set -eu
python3 -I -c '{MOUNTS}'
work=$(mktemp -d)
ip link set lo up
ip netns add out
ip link add vo type veth peer name eo netns out
ip addr add 198.51.100.1/24 dev vo
ip -6 addr add 2001:db8:5::1/64 dev vo nodad
ip link set vo up
ip -n out link set lo up
ip -n out link set eo up
{nft} -f {door_path}
{nft} -f - <<'NFT'
table inet seen {{
	counter v4 {{ }}
	counter v6 {{ }}
	chain pre {{
		type filter hook prerouting priority -100; policy accept;
		meta nfproto ipv4 tcp dport 443 tcp flags & (syn | ack) == syn counter name v4
		meta nfproto ipv6 tcp dport 443 tcp flags & (syn | ack) == syn counter name v6
	}}
}}
NFT
mac=$(ip -o link show vo | sed -n 's/.*link\\/ether \\([0-9a-f:]*\\).*/\\1/p')
seen() {{ sleep 0.3; {nft} list counter inet seen "$1" | sed -n 's/.*packets \\([0-9]*\\).*/\\1/p'; }}
flood() {{ ip netns exec out python3 -I -c '{FLOOD}' eo "$mac" "$1" "$2" "$3"; }}
fill() {{
	{nft} list set inet cseries_door "conn_$1" > "$work/list"
	python3 -I -c '{FILL}' "$1" "$work/list" "$work/fill"
	{nft} -f "$work/fill"
}}
echo "v4_start=$(seen v4)"
flood 198.51.100.2 198.51.100.1 1
echo "v4_member=$(seen v4)"
flood 198.51.100.4 198.51.100.1 100
flood 198.51.100.5 198.51.100.1 100
flood 198.51.100.6 198.51.100.1 100
echo "v4_three=$(seen v4)"
flood 198.51.100.7 198.51.100.1 300
echo "v4_one=$(seen v4)"
fill v4
echo "v4_full=$({nft} list set inet cseries_door conn_v4 | sed -n 's/.*count \\([0-9]*\\).*/\\1/p')"
flood 198.51.100.3 198.51.100.1 300
echo "v4_new=$(seen v4)"
flood 198.51.100.2 198.51.100.1 100
echo "v4_old=$(seen v4)"
sleep 2.5
echo "v6_start=$(seen v6)"
flood 2001:db8:5:1::2 2001:db8:5::1 300
echo "v6_one=$(seen v6)"
fill v6
echo "v6_full=$({nft} list set inet cseries_door conn_v6 | sed -n 's/.*count \\([0-9]*\\).*/\\1/p')"
flood 2001:db8:5::2 2001:db8:5::1 300
echo "v6_new=$(seen v6)"
rm -rf "$work"
"""
    r = subprocess.run(MOUNTNETNS + ["sh", "-c", script], capture_output=True, text=True, check=False)
    got = {k: int(v) for k, v in re.findall(r"^(v[46]_[a-z]+)=(\d+)$", r.stdout, re.M)}
    want = ["v4_start", "v4_member", "v4_three", "v4_one", "v4_full", "v4_new", "v4_old",
            "v6_start", "v6_one", "v6_full", "v6_new"]
    if r.returncode != 0 or any(k not in got for k in want):
        return False, (r.stdout + r.stderr)[-1200:]
    return {"three": got["v4_three"] - got["v4_member"], "one": got["v4_one"] - got["v4_three"],
            "full4": got["v4_full"], "new4": got["v4_new"] - got["v4_one"],
            "old4": got["v4_old"] - got["v4_new"], "one6": got["v6_one"] - got["v6_start"],
            "full6": got["v6_full"], "new6": got["v6_new"] - got["v6_one"]}, r.stdout


counts, out = packet_test(DOOR)
check(counts is not False, "the packet test runs (three namespaces, door.nft, a loopback DNAT)", out)
if counts:
    check(counts["window"] == 1 and counts["nowindow"] == 0,
          "a SYN to the box's 8810 reaches the Host only from an address in the pairing window",
          counts)
    check(counts["lockedwindow"] == 0,
          "while the vault is locked (lanes_open empty) not even the pairing window reaches 8810",
          counts)
    check(counts["loopback"] == 0,
          "a SYN a neighbour sends to 127.0.0.1:8810 does not reach the Host through a loopback DNAT",
          counts)
    check(counts["local"] == "ok",
          "the box itself still reaches the Host through its 127.0.0.1 publish", counts)

meter, out = overflow_test(DOOR)
check(meter is not False, "the meter packet test runs (two namespaces, door.nft)", out)
if meter:
    check(meter["three"] >= 295, "with room in the set, three sources of 100 each all pass: "
          "no shared limit applies then", meter)
    check(90 <= meter["one"] <= 130, "one IPv4 source of 300 gets about its burst of 100", meter)
    check(meter["full4"] == 65535 and 150 <= meter["new4"] <= 240,
          "with conn_v4 full, a new source of 300 gets about the shared burst of 200, not all 300", meter)
    check(meter["old4"] >= 95, "a source already in the full set keeps its own budget of 100", meter)
    check(80 <= meter["one6"] <= 130, "one IPv6 /64 of 300 gets about its burst of 100", meter)
    check(meter["full6"] == 65535 and 150 <= meter["new6"] <= 240,
          "with conn_v6 full, a new /64 of 300 gets about the shared burst of 200, not all 300", meter)

sys.exit(failed)
