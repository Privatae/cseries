"""
Per-sidecar process hardening.

Builds the preexec callback the supervisor runs in the child between
fork and exec. The callback:

  * raises PR_SET_NO_NEW_PRIVS so the child can never gain new
    capabilities via setuid binaries, file capabilities, etc.
  * applies NO per-process rlimits. The table that used to sit here
    (256 open files, 64 processes, 1 GiB data, 256 MiB file size) was
    never switched on by any caller, and switching it on would have
    broken real apps — a web server with a few hundred connections, a
    build with a worker pool, a database file over 256 MiB. It was
    removed so it cannot be switched on
    by accident. What bounds a sidecar is the pod's own memory and CPU
    limits, which are the plan's (pricing), not a number in this file.
  * puts the child in its own process group so SIGTERM at termination
    time hits the whole group, not just the leader.

What this DOES NOT do (deferred to a future container model with
either root, user namespaces, or per-app POSIX users):

  * separate UID per sidecar
  * chroot / pivot_root into the install dir
  * libseccomp syscall filter (would let us deny mount, ptrace,
    kexec, etc.; adds a libseccomp dependency that's not in the
    image today)

The combination above is what's reliably achievable inside a Docker
container running as non-root. It hardens against accidental damage
and the most common privilege-escalation footguns; it does NOT
prevent a malicious sidecar in the same UID from reading the kernel's
files. The trust boundary for that threat model is still the
permission token (a sidecar can only reach kernel APIs it declared).
"""

from __future__ import annotations

import ctypes
import logging
import os
from typing import Callable

log = logging.getLogger("privatae.applications.hardening")


# Linux prctl numbers (from <linux/prctl.h>); stable across kernel versions
# we care about.
_PR_SET_NO_NEW_PRIVS = 38


def _try_prctl_no_new_privs() -> None:
    """Best-effort PR_SET_NO_NEW_PRIVS. Silent no-op on platforms that
    don't expose prctl (non-Linux). Only called inside the child."""
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        ret = libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        if ret != 0:
            err = ctypes.get_errno()
            # Don't fail the spawn — log and continue. The child
            # is mid-fork; raising would propagate up oddly.
            os.write(2, f"prctl(NO_NEW_PRIVS) failed errno={err}\n".encode())
    except Exception as e:
        os.write(2, f"prctl(NO_NEW_PRIVS) exception: {e}\n".encode())


def build_preexec(*, apply_no_new_privs: bool = True) -> Callable[[], None]:
    """Return a preexec callable for subprocess.

    Runs inside the child after fork() and before exec(). Anything
    raised in here will turn the spawn into an OSError on the parent
    side, so we keep this defensive — best-effort per step, log to
    stderr (which the supervisor captures), then exec.
    """
    def _preexec() -> None:
        # Own process group — clean SIGTERM on close + tree-kill on hang.
        try:
            os.setpgrp()
        except OSError:
            pass

        if apply_no_new_privs:
            _try_prctl_no_new_privs()

    return _preexec
