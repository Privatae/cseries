"""Private namespaces for the gates that send real packets: how to get into
them, why they could not be made, and the one mount the packet scripts make
in them. Imported by scripts/test_vm_machine_firewall.py; the same is in
scripts/test_cloud_box_nft.py, which can import it in place of its own copy.
Not a gate itself (no test_ prefix): scripts/test_gate_coverage.py counts
test_*.py and check_*.py only. A gate that imports it names this file
(scripts/private_netns.py) in its source, so a change here runs that gate
first (scripts/pre_merge_gates.sh reads a gate's source for changed paths).

Root (or root in a user namespace, as under rootlesskit on the forge runner)
makes the namespaces itself; anyone else asks for a user namespace of their
own, where it is root and no other uid is mapped.
"""
import os

NS_ROOT = os.geteuid() == 0
NETNS = ["unshare", "-n"] if NS_ROOT else ["unshare", "-rn"]
MOUNTNETNS = ["unshare", "-mn"] if NS_ROOT else ["unshare", "-rmn"]


def no_namespace_why(stderr, what="private namespace"):
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
    return f"no {what} can be made here: {why[0]}{hint}"


# Python for `python3 -I -c '...'` as the first line of a packet script run in
# MOUNTNETNS: every mount stays private to that namespace, and /run/netns is a
# tmpfs of its own, so `ip netns add` neither needs the machine's /run nor
# leaves anything there. With mount(2), not mount(8): the forge runner's unit
# makes /usr/bin/mount inaccessible (InaccessiblePaths), and there the shell's
# `mount` failed with "Permission denied" (2026-10-26). Where /run/netns is
# missing (and /run may be read-only, as in a ProtectSystem=strict unit), /run
# gets a tmpfs of its own and /run/netns is made in it. It holds no single
# quote, so it sits in the shell's single quotes as it is.
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
assert "'" not in MOUNTS
