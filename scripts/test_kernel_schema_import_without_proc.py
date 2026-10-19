#!/usr/bin/env python3
"""scripts/kernel_schema.py imports where /proc is missing.

Every gate that only builds the kernel schema imports this module, and it read
the PID namespace (/proc/self/ns/pid) at import with nothing catching the
error, so the import itself failed on a box without /proc. Without /proc the
boot id is empty too, and the throwaway reap then removes nothing.

A partial /proc can refuse only the namespace path and still give the boot id.
PIDNS is then 0 in every PID namespace, so 0 must mean "unknown" to the reap,
never a namespace to match: otherwise a gate would look up another
namespace's pid and remove a live gate's database.

Container-free: os.stat and Path.read_text refuse /proc paths while the module
is imported fresh, and are restored before any check runs.
"""

import importlib
import os
import pathlib
import sys

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(APP_ROOT, "scripts"))

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {extra}")
        FAILS.append(name)


real_stat, real_read = os.stat, pathlib.Path.read_text


def fresh_import(refused):
    """kernel_schema imported anew while every path that `refused` accepts
    raises ENOENT; returns (module or None, error or None)."""

    def stat(p, *a, **k):
        if refused(str(p)):
            raise FileNotFoundError(2, "No such file or directory", str(p))
        return real_stat(p, *a, **k)

    def read(self, *a, **k):
        if refused(str(self)):
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_read(self, *a, **k)

    sys.modules.pop("kernel_schema", None)
    os.stat, pathlib.Path.read_text = stat, read
    try:
        return importlib.import_module("kernel_schema"), None
    except Exception as e:  # noqa: BLE001 — the failure is what is under test
        return None, e
    finally:
        os.stat, pathlib.Path.read_text = real_stat, real_read


# No /proc at all.
ks, err = fresh_import(lambda p: p.startswith("/proc"))
check("kernel_schema imports with no /proc", ks is not None, repr(err))
if ks is not None:
    check("no PID namespace is claimed", ks.PIDNS == 0, repr(ks.PIDNS))
    check("no boot id, so the reap judges nothing", ks.BOOT_ID == "", repr(ks.BOOT_ID))

# /proc present, the namespace path refused.
ks, err = fresh_import(lambda p: p == "/proc/self/ns/pid")
check("kernel_schema imports with the PID namespace unreadable", ks is not None, repr(err))
if ks is not None:
    check("the boot id is still read", bool(ks.BOOT_ID), repr(ks.BOOT_ID))
    # Labelled by a gate that could not read its namespace either, on this
    # boot, its pid gone here: that pid may be a live gate in another namespace.
    got = ks.throwaway_leftovers([f"ns-unknown\t101\t0\t5000\t{ks.BOOT_ID}"], start_of=lambda pid: None)
    check("with the PID namespace unknown the reap judges nothing", got == [], got)

check("os.stat and Path.read_text are restored",
      os.stat is real_stat and pathlib.Path.read_text is real_read)

if FAILS:
    print(f"\n{len(FAILS)} kernel_schema no-/proc check(s) FAILED")
    sys.exit(1)
print("\nall kernel_schema no-/proc checks passed")
