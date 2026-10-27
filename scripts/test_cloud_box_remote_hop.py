#!/usr/bin/env python3
"""On a cloud box, the hop between the Host and its remote-access door, with
the box's firewall (vm/cloud/overlay/etc/cseries-cloud/nftables/door.nft) and
remote access's (vm/wsl/rootfs/etc/cseries-remote/remote.nft) both loaded.

door.nft's forward chain drops every new connection a container opens to a
private range (internal_v4, 10.0.0.0/8 among them), and the Host's tunnel
hands browsers to the door at 10.89.79.3:8443, a private address. Whether
that rule meets the hop depends on how the packets travel: the Host and the
door are two ports of one bridge (cseries-remote0), so their frames are
bridged, and the inet forward hook sees bridged frames only when br_netfilter
passes them to the IP hooks (net.bridge.bridge-nf-call-iptables=1). The
local runtime's network (cseries-apps0) is in the same position: the Host
and an app container talk across one bridge, and door.nft names no exception
for that either.

So this gate sends real packets, in private namespaces playing the box, the
Host, the door and an app container on two bridges built as podman builds
them, with both rule files loaded:

  1. With the bridge's frames kept out of the IP hooks (no br_netfilter, or
     its sysctl off): the tunnel reaches the door's 8443 and the door the
     Host's 8800, data both ways; the door still reaches no other port of the
     Host (remote.nft); and an app reaches the Host's 8800, as on a box today.
  2. With bridge-nf-call-iptables on, when this kernel has br_netfilter,
     door.nft's private-range drop meets the bridged frames and cuts the
     remote hop and the runtime's Host-to-app hop alike (measured, not
     required). So the box keeps the hooks off: its modprobe.d file refuses
     the module, and `cseries-firewall load` turns the hooks off where the
     kernel has them anyway. After the box's own `cseries-firewall
     bridge-off` (the step load runs) every hop is open again.
  3. The box's files say so: modprobe.d refuses br_netfilter, and load runs
     bridge_off right after door.nft.

Needs nft, ip and unshare, and root or a user namespace of its own.

    python3 scripts/test_cloud_box_remote_hop.py      (from the repo root)
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DOOR_NFT = os.path.join(ROOT, "vm/cloud/overlay/etc/cseries-cloud/nftables/door.nft")
REMOTE_NFT = os.path.join(ROOT, "vm/wsl/rootfs/etc/cseries-remote/remote.nft")
FIREWALL = os.path.join(ROOT, "vm/cloud/overlay/usr/local/sbin/cseries-firewall")
MODPROBE = os.path.join(ROOT, "vm/cloud/overlay/etc/modprobe.d/cseries-cloud.conf")
failed = 0


def check(cond, what, detail=""):
    global failed
    if cond:
        print(f"OK    {what}")
    else:
        failed = 1
        print(f"FAIL  {what} {detail}")


nft = shutil.which("nft") or ("/usr/sbin/nft" if os.path.exists("/usr/sbin/nft") else None)
ip = shutil.which("ip") or ("/usr/sbin/ip" if os.path.exists("/usr/sbin/ip") else None)
missing = [n for n, p in (("nft", nft), ("ip", ip), ("unshare", shutil.which("unshare"))) if not p]
if missing:
    check(False, f"this gate needs {', '.join(missing)} on PATH, so the packets are not checked")
    sys.exit(failed)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from private_netns import MOUNTNETNS, MOUNTS, no_namespace_why  # noqa: E402

r = subprocess.run(MOUNTNETNS + ["true"], capture_output=True, text=True, check=False)
if r.returncode != 0:
    check(False, no_namespace_why(r.stderr, "private mount and network namespace"))
    sys.exit(failed)

LISTEN = r'''
import selectors, socket, sys, time
sel = selectors.DefaultSelector()
for spec in sys.argv[1:]:
    addr, port = spec.split("|")
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((addr, int(port))); s.listen(16); s.setblocking(False)
    sel.register(s, selectors.EVENT_READ)
end = time.time() + 90
while time.time() < end:
    for key, _ in sel.select(1):
        c, _ = key.fileobj.accept(); c.settimeout(3)
        try:
            c.sendall(c.recv(64))
        except OSError:
            pass
        c.close()
'''

PROBE = r'''
import socket, sys
out = []
for spec in sys.argv[1:]:
    addr, port = spec.split("|")
    try:
        s = socket.socket(); s.settimeout(1.5); s.connect((addr, int(port)))
        s.sendall(b"knock"); got = s.recv(16); s.close()
        out.append("open" if got == b"knock" else "no-echo")
    except socket.timeout:
        out.append("dropped")
    except ConnectionRefusedError:
        out.append("refused")
    except OSError as e:
        out.append("error:" + str(e.errno))
print(" ".join(out))
'''

script = f"""set -eu
python3 -I -c '{MOUNTS}'
ip link set lo up
for n in host door rthost app; do ip netns add "$n"; done
bridge() {{  # name, gateway address
    ip link add "$1" type bridge
    ip addr add "$2" dev "$1"
    ip link set "$1" up
}}
port() {{  # bridge, namespace, address
    ip link add "v$2" type veth peer name eth0 netns "$2"
    ip link set "v$2" master "$1"
    ip link set "v$2" up
    ip netns exec "$2" sh -c "ip link set lo up; ip addr add $3 dev eth0; ip link set eth0 up"
}}
# The remote network (cseries-remote.network) and the runtime's
# (cseries-apps.network), with the Host's and the door's fixed addresses.
bridge cseries-remote0 10.89.79.1/29
port cseries-remote0 host 10.89.79.2/29
port cseries-remote0 door 10.89.79.3/29
bridge cseries-apps0 10.89.78.1/24
port cseries-apps0 rthost 10.89.78.2/24
port cseries-apps0 app 10.89.78.10/24
echo 1 > /proc/sys/net/ipv4/ip_forward
pids=""
ip netns exec host timeout 90 python3 -I -c '{LISTEN}' '0.0.0.0|8800' '0.0.0.0|8810' &
pids="$pids $!"
ip netns exec door timeout 90 python3 -I -c '{LISTEN}' '10.89.79.3|8443' &
pids="$pids $!"
ip netns exec rthost timeout 90 python3 -I -c '{LISTEN}' '0.0.0.0|8800' &
pids="$pids $!"
sleep 1.5
{nft} -f {DOOR_NFT}
{nft} -f {REMOTE_NFT}
brnf=/proc/sys/net/bridge/bridge-nf-call-iptables
for round in 1 2 3; do
    if [ "$round" = 1 ]; then
        if [ -w "$brnf" ]; then echo 0 > "$brnf"; fi
    elif [ "$round" = 2 ]; then
        if [ -w "$brnf" ]; then echo 1 > "$brnf"; echo "brnf2: on"; else echo "brnf2: unavailable"; break; fi
    else
        # The box's own step (cseries-firewall load runs it after door.nft).
        echo "bridgeoff: $(sh {FIREWALL} bridge-off >/dev/null 2>&1; echo $?) $(cat "$brnf")"
    fi
    echo "tunnel$round: $(ip netns exec host python3 -I -c '{PROBE}' '10.89.79.3|8443')"
    echo "web$round: $(ip netns exec door python3 -I -c '{PROBE}' '10.89.79.2|8800')"
    echo "other$round: $(ip netns exec door python3 -I -c '{PROBE}' '10.89.79.2|8810')"
    echo "apps$round: $(ip netns exec app python3 -I -c '{PROBE}' '10.89.78.2|8800')"
done
kill $pids 2>/dev/null || true
wait 2>/dev/null || true
"""
r = subprocess.run(MOUNTNETNS + ["sh", "-c", script], capture_output=True, text=True, check=False)
out = r.stdout
lines = dict(ln.split(": ", 1) for ln in out.splitlines() if ": " in ln)
detail = f"\n--- stdout\n{out}\n--- stderr\n{r.stderr[-1500:]}"
check(r.returncode == 0, "the packet script ran to its end", detail)

check(lines.get("tunnel1") == "open",
      "door.nft and remote.nft loaded, the bridge's frames out of the IP hooks: the Host's tunnel reaches "
      "the door's 8443, data both ways", detail)
check(lines.get("web1") == "open", "and the door reaches the Host's web port 8800, data both ways", detail)
check(lines.get("other1") in ("dropped", "error:113"), "and nothing else on the Host (8810)", detail)
check(lines.get("apps1") == "open", "and an app reaches the Host's 8800 on the runtime's network, as today", detail)
if lines.get("brnf2") == "on":
    print(f"MEASURE  with bridge-nf-call-iptables on: tunnel {lines.get('tunnel2')}, web {lines.get('web2')}, "
          f"runtime's Host-to-app hop {lines.get('apps2')}")
    check(lines.get("bridgeoff") == "0 0",
          "the box's cseries-firewall bridge-off succeeds and turns bridge-nf-call-iptables off", detail)
    check(all(lines.get(k + "3") == "open" for k in ("tunnel", "web", "apps")) and
          lines.get("other3") in ("dropped", "error:113"),
          "after it, the tunnel reaches the door, the door the Host's 8800 and an app the Host's 8800 again, "
          "and the door still nothing else on the Host", detail)
else:
    print("SKIP  bridge-nf-call-iptables is not on this kernel (no br_netfilter): rounds 2 and 3 not run")

# 3. The box keeps the module out, and load turns the hooks off.
with open(MODPROBE) as f:
    modprobe = [ln.split() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
check(["install", "br_netfilter", "/bin/false"] in modprobe,
      "the box's modprobe.d refuses to load br_netfilter", modprobe)
with open(FIREWALL) as f:
    fw = f.read()
load = fw.split("    load)", 1)[1].split(";;", 1)[0]
check([ln.strip() for ln in load.strip().splitlines()[:2]] == ['nft -f "$D/door.nft"', "bridge_off"],
      "cseries-firewall load turns the bridge's hooks off right after it loads door.nft", load[:200])

print("PASSED" if not failed else "FAILED")
sys.exit(failed)
