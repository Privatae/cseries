#!/usr/bin/env python3
"""The restore block of entrypoint.sh, run for real under /bin/sh.

What protects a restored Host that cannot run on the restored data is the plan
in server/host_restore.py (boots are counted until the kernel says it started;
scripts/test_restore_crash_safety.py). The shell trap is only a shortcut, and
this pins exactly what it does and does not do:

  * a boot this shell ends with an error (a failed migration, `set -e`) undoes
    the restore at once;
  * a SIGTERM (the kubelet stopping the pod mid-boot) is not a failure of the
    restored data: exit 143, nothing undone;
  * the trap is disarmed right before `exec`, where it could not run anyway —
    after that only the plan decides;
  * no plan, no trap: an ordinary boot's failures never call undo.

The block is taken from entrypoint.sh itself, with python3 replaced by a stub
that records what it was asked to do.

    python3 scripts/test_restore_boot_trap.py
"""
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILED.append(label)


src = Path(os.environ.get("ENTRYPOINT_FILE") or ROOT / "entrypoint.sh").read_text()
start = src.index("# ══════ A restore waiting to be swapped in ══════")
end = src.index("# ══════ Schema: base load + migrations ══════")
block = src[start:end]
disarm = re.search(r"^if \[ \"\$_restore_armed\" = 1 \]; then trap - EXIT TERM; fi$", src, re.M)
check("entrypoint.sh disarms the restore trap right before it execs the server",
      disarm is not None and src.index(disarm.group(0)) < src.index("exec python3 -m server.launch")
      and src.index("exec python3 -m server.launch") - src.index(disarm.group(0)) < 200)

tmp = Path(tempfile.mkdtemp(prefix="boot-trap-"))
plan = tmp / "plan.json"
log = tmp / "calls"
stub = tmp / "bin" / "python3"
stub.parent.mkdir()
stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 0\n')
stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

AFTER = {
    "explicit": 'exit 1',                         # e.g. a migration failed
    "setefail": 'false',                          # set -e
    "sigterm": 'kill -TERM $$; sleep 5; exit 0',  # the kubelet stops the pod
    "server_fails": '',                           # reaches exec; the server then fails
    "ok": '',
}


def boot(mode, with_plan=True):
    if log.exists():
        log.unlink()
    if with_plan:
        plan.write_text("{}")
    elif plan.exists():
        plan.unlink()
    script = ("set -eu\nENTITY_DB=hostmem\n"
              + block.replace("/var/lib/cseries/.host-restore/plan.json", str(plan))
              + AFTER[mode] + "\n"
              + (disarm.group(0) if disarm else "") + "\n"
              + ("exec sh -c 'exit 7'\n" if mode == "server_fails" else "exec sh -c 'exit 0'\n"))
    f = tmp / f"boot-{mode}.sh"
    f.write_text(script)
    env = dict(os.environ, PATH=f"{stub.parent}:{os.environ['PATH']}")
    t0 = time.time()
    p = subprocess.run(["/bin/sh", str(f)], env=env, capture_output=True, text=True, timeout=30)
    calls = log.read_text().splitlines() if log.exists() else []
    return p.returncode, calls, time.time() - t0


print("== a boot with a restore swapped in")
rc, calls, _ = boot("explicit")
check("a boot the shell ends with an error undoes the restore at once",
      rc == 1 and any(c.startswith("-m server.host_restore undo hostmem") for c in calls), (rc, calls))
rc, calls, _ = boot("setefail")
check("so does one stopped by set -e", rc != 0 and any("undo" in c for c in calls), (rc, calls))
rc, calls, took = boot("sigterm")
check("a SIGTERM is not a failure of the restored data: exit 143, nothing undone",
      rc == 143 and not any("undo" in c for c in calls) and took < 5, (rc, calls, took))
rc, calls, _ = boot("server_fails")
check("past exec the shell undoes nothing: the plan's boot count decides",
      rc == 7 and calls == ["-m server.host_restore swap hostmem"], (rc, calls))
rc, calls, _ = boot("ok")
check("a clean boot only swaps", rc == 0 and calls == ["-m server.host_restore swap hostmem"], (rc, calls))

print("== an ordinary boot")
rc, calls, _ = boot("explicit", with_plan=False)
check("no plan: a failing boot never calls the restore at all", rc == 1 and calls == [], (rc, calls))

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: the restore trap does what entrypoint.sh says, and only that")
