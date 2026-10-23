"""Two things the CI runner's sandbox does not allow a gate, and how a gate
checks for them instead of failing on them.

A unix socket deep in TMPDIR. An AF_UNIX address holds 107 bytes of path.
pre_merge_gates.sh gives every gate a TMPDIR inside its own scratch folder,
inside the job's folder on a CI runner, and on the forge's runner that is
already 72 bytes deep. A socket two levels under a gate's folder then fails
to bind ("AF_UNIX path too long"), while the same gate passes on a machine
whose TMPDIR is short.

A chown to an app's ids. The runner runs the gates under rootlesskit: the
gate is uid 0, but only the runner's own 65536 ids are mapped, and the
runtime's app ids start at 200000 (cseries_runtime/common.py UID_BASE), so a
chown to one fails with EINVAL. Uid 0 is not enough to tell.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

SUN_PATH_MAX = 107
_MKDTEMP_SUFFIX = 8   # the random letters tempfile.mkdtemp adds to the prefix


def fits(base: str, prefix: str, deepest: str) -> bool:
    """Whether `deepest`, under a mkdtemp(prefix) folder in base, fits an
    AF_UNIX address."""
    path = os.path.join(os.path.abspath(base), prefix + "x" * _MKDTEMP_SUFFIX, deepest)
    return len(path.encode()) <= SUN_PATH_MAX


def socket_tmp(prefix: str, deepest: str) -> Path:
    """A new folder (tempfile.mkdtemp) in TMPDIR when `deepest`, the longest
    path the gate binds a socket at relative to the folder, fits there, else
    in /tmp. The caller removes it, as it would a folder in TMPDIR."""
    for base in (tempfile.gettempdir(), "/tmp"):
        if os.path.isdir(base) and fits(base, prefix, deepest):
            return Path(tempfile.mkdtemp(prefix=prefix, dir=base))
    raise SystemExit(f"FAIL: no temp folder short enough for the unix socket {deepest!r} "
                     f"(TMPDIR={tempfile.gettempdir()!r})")


def owns_app_ids(base: int, span: int, uid_map: str = "/proc/self/uid_map") -> bool:
    """Whether this process can chown to every id in [base, base + span): uid
    0, with one uid_map row covering all of them."""
    if os.geteuid() != 0:
        return False
    try:
        with open(uid_map) as f:
            rows = [tuple(int(x) for x in line.split()) for line in f if line.strip()]
    except (OSError, ValueError):
        return False
    return any(len(r) == 3 and r[0] <= base and base + span <= r[0] + r[2] for r in rows)
