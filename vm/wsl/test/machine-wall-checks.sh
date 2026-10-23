#!/usr/bin/env bash
# The machine wall, checked inside a running guest, as root (vm/lima/test/e2e.sh
# and vm/wsl/test/boot-test.sh feed it on standard input):
#
#   machine-wall-checks.sh <host image> <the machine's address> <port>...
#
# <the machine's address> is the machine as this guest reaches it: Lima's
# 192.168.5.2, which is the machine's own loopback, or the guest's default
# gateway. The ports are ones something on the machine listens on.
#
#   - the wall's unit is active and its table loaded;
#   - the guest itself (root) reaches the machine on the first port, so the
#     path is there, and it is the wall that stops the containers; set
#     MACHINE_PATH_OPTIONAL=1 where the machine may not answer the guest at all;
#   - from the Host's container, and from a fresh container on the Host's
#     network, every port is dropped;
#   - from the Host's container a public name resolves and its HTTPS port
#     answers: DNS and the way out stay.
set -euo pipefail
img=${1:?host image}
machine=${2:?the machine address as the guest reaches it}
shift 2
[ $# -gt 0 ] || { echo "usage: $0 <host image> <machine address> <port>..."; exit 2; }
fail() { echo "FAIL: $*"; exit 1; }

systemctl is-active --quiet cseries-machine-firewall.service || fail "cseries-machine-firewall.service is not active"
nft list table inet cseries_machine >/dev/null || fail "the cseries_machine table is not loaded"
echo "the machine wall is loaded"

probe='
import socket, sys
out = []
for port in sys.argv[2:]:
    try:
        socket.create_connection((sys.argv[1], int(port)), 3).close(); out.append("open")
    except socket.timeout:
        out.append("dropped")
    except ConnectionRefusedError:
        out.append("refused")
    except OSError as e:
        out.append("error:%s" % e.errno)
print(" ".join(out))
'
want=$(printf 'dropped %.0s' "$@"); want=${want% }

# Every command here reads /dev/null: this script arrives on bash's standard
# input, and a command that read it would eat the rest of the script.
first=$(python3 -c "$probe" "$machine" "$1" </dev/null)
if [ "$first" = open ]; then
    echo "the guest itself reaches $machine:$1 (the path the wall closes for containers)"
elif [ "${MACHINE_PATH_OPTIONAL:-}" = 1 ]; then
    echo "the guest itself does not reach $machine:$1 here ($first); only the containers' side is checked"
else
    fail "the guest itself does not reach $machine:$1 ($first), so this run proves nothing about the wall"
fi

got=$(podman exec cseries-host python3 -c "$probe" "$machine" "$@" </dev/null)
[ "$got" = "$want" ] || fail "from the Host's container, $machine:$* gave: $got"
echo "from the Host's container, $machine:$* are dropped"

got=$(podman run --rm --network cseries_default --entrypoint python3 "$img" -c "$probe" "$machine" "$@" </dev/null)
[ "$got" = "$want" ] || fail "from a new container on the Host's network, $machine:$* gave: $got"
echo "from a new container on the Host's network, $machine:$* are dropped"

podman exec cseries-host python3 -c '
import socket
socket.getaddrinfo("deb.debian.org", 443)
socket.create_connection(("deb.debian.org", 443), 10).close()
' </dev/null || fail "the Host's container cannot resolve deb.debian.org or reach its port 443"
echo "the Host's container resolves a public name and reaches it"
echo "machine wall checks: PASS"
