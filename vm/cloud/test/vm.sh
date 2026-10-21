#!/usr/bin/env bash
# A Debian 13 virtual machine for testing the cloud box, on a Linux machine
# with KVM. Run as root (the second network card is a tap device).
#
#   vm/cloud/test/vm.sh image            download the Debian 13 cloud image and
#                                        check it against Debian's SHA512SUMS
#   vm/cloud/test/vm.sh up               a fresh VM from that image, first boot
#                                        through cloud-init as the default user
#   vm/cloud/test/vm.sh ssh [command]    a shell or a command as that user
#   vm/cloud/test/vm.sh scp <file>... <dir in the VM>
#   vm/cloud/test/vm.sh reboot           an ACPI reboot from outside, then wait
#   vm/cloud/test/vm.sh down             power off and remove the tap device
#
# Everything lives under $VM_WORK (default /var/tmp/cloud-vm/vm), the Debian
# image under $VM_IMAGE_DIR (default /var/tmp/cloud-vm/debian-image).
#
# Two network cards:
#   NIC1  QEMU user-mode networking: the VM's way to the internet, and SSH
#         from this machine through a forwarded port on 127.0.0.1 (the way a
#         provisioner reaches a fresh VPS before the box is sealed).
#   NIC2  a tap device, 192.0.2.1/30 and 2001:db8:c5::1/64 on this side and
#         192.0.2.2 / 2001:db8:c5::2 in the VM. This is the "outside" the
#         tests scan from and pair from. Nothing else on this machine is
#         changed: no firewall rule, no route beyond the tap's own.
set -euo pipefail

WORK=${VM_WORK:-/var/tmp/cloud-vm/vm}
IMAGE_DIR=${VM_IMAGE_DIR:-/var/tmp/cloud-vm/debian-image}
IMAGE_URL=https://cloud.debian.org/images/cloud/trixie/latest
IMAGE_NAME=debian-13-genericcloud-amd64.qcow2
TAP=${VM_TAP:-cbtap0}
SSH_PORT=${VM_SSH_PORT:-2222}
MEM=${VM_MEM:-6144}
CPUS=${VM_CPUS:-4}
DISK_SIZE=${VM_DISK:-24G}
MAC1=52:54:00:c5:00:01
MAC2=52:54:00:c5:00:02
OUT_V4=192.0.2.1
VM_V4=192.0.2.2
OUT_V6=2001:db8:c5::1
VM_V6=2001:db8:c5::2

die() { printf 'vm.sh: %s\n' "$*" >&2; exit 1; }
say() { printf '== %s\n' "$*" >&2; }

ssh_opts=(-i "$WORK/id_ed25519" -o UserKnownHostsFile="$WORK/known_hosts"
          -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o BatchMode=yes
          -o ServerAliveInterval=10 -o LogLevel=ERROR)

vm_ssh() { ssh "${ssh_opts[@]}" -p "$SSH_PORT" debian@127.0.0.1 "$@"; }

qmp() {
    # One QMP command on the monitor socket.
    python3 -I - "$WORK/qmp.sock" "$1" <<'EOF'
import json, socket, sys
s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
f = s.makefile("rw")
f.readline()
for cmd in ({"execute": "qmp_capabilities"}, {"execute": sys.argv[2]}):
    f.write(json.dumps(cmd) + "\n"); f.flush()
    while True:
        r = json.loads(f.readline())
        if "return" in r or "error" in r:
            break
print(json.dumps(r))
EOF
}

running() { [ -f "$WORK/qemu.pid" ] && kill -0 "$(cat "$WORK/qemu.pid")" 2>/dev/null; }

wait_ssh() {
    for _ in $(seq 1 "${1:-120}"); do
        if vm_ssh true 2>/dev/null; then return 0; fi
        running || die "the VM stopped (serial log: $WORK/serial.log)"
        sleep 2
    done
    die "no SSH after $(( ${1:-120} * 2 ))s (serial log: $WORK/serial.log)"
}

cmd_image() {
    # A new directory of its own: the download is untrusted until its sum
    # matches the one Debian publishes.
    mkdir -p "$IMAGE_DIR"
    if [ -f "$IMAGE_DIR/$IMAGE_NAME" ] && [ -f "$IMAGE_DIR/SHA512SUMS" ] &&
       (cd "$IMAGE_DIR" && grep " $IMAGE_NAME\$" SHA512SUMS | sha512sum -c --status); then
        say "image present and matches its SHA512SUMS"
    else
        say "downloading $IMAGE_URL/$IMAGE_NAME"
        curl -fsSL -o "$IMAGE_DIR/SHA512SUMS" "$IMAGE_URL/SHA512SUMS"
        curl -fSL -o "$IMAGE_DIR/$IMAGE_NAME.part" "$IMAGE_URL/$IMAGE_NAME"
        mv "$IMAGE_DIR/$IMAGE_NAME.part" "$IMAGE_DIR/$IMAGE_NAME"
        (cd "$IMAGE_DIR" && grep " $IMAGE_NAME\$" SHA512SUMS | sha512sum -c) ||
            { rm -f "$IMAGE_DIR/$IMAGE_NAME"; die "the image does not match Debian's SHA512SUMS"; }
    fi
    # The sums file itself, against the copy Debian serves now: a stale or
    # altered local copy shows up as a difference.
    curl -fsSL "$IMAGE_URL/SHA512SUMS" | grep " $IMAGE_NAME\$" |
        diff - <(grep " $IMAGE_NAME\$" "$IMAGE_DIR/SHA512SUMS") >/dev/null ||
        die "the local SHA512SUMS differs from the one Debian serves now; run with a fresh $IMAGE_DIR"
}

seed() {
    local d="$WORK/seed"
    rm -rf "$d"; mkdir -p "$d"
    cat > "$d/meta-data" <<EOF
instance-id: cseries-cloud-test-$(date +%s)
local-hostname: cloud-test
EOF
    cat > "$d/user-data" <<EOF
#cloud-config
ssh_pwauth: false
ssh_authorized_keys:
  - $(cat "$WORK/id_ed25519.pub")
EOF
    cat > "$d/network-config" <<EOF
version: 2
ethernets:
  uplink:
    match:
      macaddress: "$MAC1"
    dhcp4: true
    dhcp6: false
  outside:
    match:
      macaddress: "$MAC2"
    dhcp4: false
    dhcp6: false
    accept-ra: false
    addresses:
      - $VM_V4/30
      - $VM_V6/64
EOF
    cloud-localds -N "$d/network-config" "$WORK/seed.iso" "$d/user-data" "$d/meta-data"
}

tap_up() {
    if ! ip link show "$TAP" >/dev/null 2>&1; then
        ip tuntap add dev "$TAP" mode tap
        ip addr add "$OUT_V4/30" dev "$TAP"
        ip -6 addr add "$OUT_V6/64" dev "$TAP" nodad
    fi
    ip link set "$TAP" up
}

tap_down() {
    if ip link show "$TAP" >/dev/null 2>&1; then ip link del "$TAP"; fi
}

start_qemu() {
    qemu-system-x86_64 -name cseries-cloud-test -machine q35,accel=kvm -cpu host \
        -m "$MEM" -smp "$CPUS" \
        -drive file="$WORK/disk.qcow2",if=virtio,format=qcow2,discard=unmap \
        -drive file="$WORK/seed.iso",if=virtio,format=raw,readonly=on \
        -netdev user,id=n1,hostfwd=tcp:127.0.0.1:"$SSH_PORT"-:22 \
        -device virtio-net-pci,netdev=n1,mac="$MAC1" \
        -netdev tap,id=n2,ifname="$TAP",script=no,downscript=no \
        -device virtio-net-pci,netdev=n2,mac="$MAC2" \
        -qmp unix:"$WORK/qmp.sock",server=on,wait=off \
        -display none -serial file:"$WORK/serial.log" \
        -daemonize -pidfile "$WORK/qemu.pid"
}

cmd_up() {
    [ -e /dev/kvm ] || die "no /dev/kvm"
    running && die "a VM is already running (vm.sh down first)"
    mkdir -p "$WORK"
    cmd_image
    [ -f "$WORK/id_ed25519" ] || ssh-keygen -q -t ed25519 -N '' -C cseries-cloud-test -f "$WORK/id_ed25519"
    rm -f "$WORK/disk.qcow2" "$WORK/known_hosts" "$WORK/serial.log"
    qemu-img create -q -f qcow2 -F qcow2 -b "$IMAGE_DIR/$IMAGE_NAME" "$WORK/disk.qcow2" "$DISK_SIZE"
    seed
    tap_up
    start_qemu
    say "booting (serial log $WORK/serial.log)"
    wait_ssh 150
    # shellcheck disable=SC2016 # expands in the VM
    vm_ssh 'cloud-init status --wait >/dev/null 2>&1 || true; . /etc/os-release; echo "VM up: $PRETTY_NAME, $(uname -r)"'
}

cmd_reboot() {
    running || die "no VM is running"
    qmp system_powerdown >/dev/null
    # ACPI power-down ends the QEMU process; start it again on the same disk,
    # the way a provider's reboot keeps the machine's disk.
    for _ in $(seq 1 90); do running || break; sleep 2; done
    running && die "the VM did not power off within 180s"
    start_qemu
    say "rebooted"
}

cmd_down() {
    if running; then
        qmp system_powerdown >/dev/null 2>&1 || true
        for _ in $(seq 1 60); do running || break; sleep 2; done
        if running; then kill "$(cat "$WORK/qemu.pid")"; sleep 2; fi
    fi
    rm -f "$WORK/qemu.pid" "$WORK/qmp.sock"
    tap_down
    say "VM is off and $TAP removed"
}

c=${1:-}
[ $# -gt 0 ] && shift
case "$c" in
    image) cmd_image ;;
    up) cmd_up ;;
    ssh) vm_ssh "$@" ;;
    scp)
        [ $# -ge 2 ] || die "usage: vm.sh scp <file>... <dir in the VM>"
        dest=${*: -1}
        set -- "${@:1:$(($#-1))}"
        scp -q "${ssh_opts[@]}" -P "$SSH_PORT" "$@" "debian@127.0.0.1:$dest"
        ;;
    wait-ssh) wait_ssh "${1:-120}" ;;
    reboot) cmd_reboot ;;
    down) cmd_down ;;
    running) running ;;
    *) die "usage: vm.sh image|up|ssh|scp|wait-ssh|reboot|down" ;;
esac
