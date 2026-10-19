#!/usr/bin/env python3
"""Gate: kernel-local loops never give up on legitimate work, and no dead
limit table can be switched on by accident.

  - default_install.run_loop and harness_sync.run_loop retried 12 x 300 s and
    then stopped — default_install with no log at all — so a bridge down for
    over an hour left the default apps uninstalled and the editor harness
    un-asserted until the next restart. Now both retry with a doubling wait
    (300 s .. 1 h) for as long as it takes, logging each retry, and end only
    when there is nothing to do.
  - hardening.py carried an rlimit table (256 files, 64 processes, 1 GiB data,
    256 MiB file size) no caller applied and that would break real apps if one
    did. It is gone; a sidecar spawned through build_preexec inherits the
    kernel's own limits.
  - brain/mastercode/brain_memory.py (MAX_LESSONS = 50, a 7-day TTL) was
    imported by nothing. It is deleted.

The loops run for real with asyncio.sleep and run_once scripted; no bridge,
no DB. Run: APP_ROOT=<repo> python3 scripts/test_boot_reconcilers_never_give_up.py
"""
import asyncio
import os
import resource
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "brain"))
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("server.main", types.ModuleType("server.main"))

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[never-give-up] {'ok  ' if cond else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


from server.applications import default_install as DI  # noqa: E402
from server.applications import harness_sync as HS  # noqa: E402
from server.applications import hardening as HD  # noqa: E402


def drive(mod, statuses):
    """Run mod.run_loop with run_once answering `statuses` in order (then
    the last forever) and asyncio.sleep recorded, not slept."""
    sleeps: list[float] = []
    calls = {"n": 0}

    async def run_once(_bc):
        i = min(calls["n"], len(statuses) - 1)
        calls["n"] += 1
        st = statuses[i]
        if st == "crash":
            raise RuntimeError("boom")
        return {"status": st}

    class _Proxy:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        @staticmethod
        async def sleep(d):
            sleeps.append(float(d))
            if len(sleeps) > 60:
                raise asyncio.CancelledError()

    real_once, real_asyncio = mod.run_once, mod.asyncio
    mod.run_once, mod.asyncio = run_once, _Proxy()
    try:
        asyncio.new_event_loop().run_until_complete(mod.run_loop(None))
    except asyncio.CancelledError:
        pass
    finally:
        mod.run_once, mod.asyncio = real_once, real_asyncio
    return sleeps, calls["n"]


for mod, ok, name in ((DI, "installed", "default_install"),
                      (HS, "synced", "harness_sync")):
    statuses = ["bridge_not_ready"] * 15 + ["crash"] + [ok]
    sleeps, n = drive(mod, statuses)
    check(f"{name}: still retrying after the old 12-attempt give-up, until the pass runs",
          n == 17, f"run_once called {n} times")
    waits = sleeps[1:]          # the first sleep is the startup delay
    check(f"{name}: each retry waits twice as long, from 300 s up to 1 h",
          waits[:6] == [300.0, 600.0, 1200.0, 2400.0, 3600.0, 3600.0], repr(waits[:6]))
    check(f"{name}: and it stops once the pass has run", len(waits) == 16, repr(len(waits)))
    check(f"{name}: attempt 5,000 still waits the 1 h cap",
          mod.retry_wait(5000) == 3600.0)

# No dead rlimit table to switch on
check("hardening carries no rlimit table and no switch to apply one",
      not hasattr(HD, "Limits") and not hasattr(HD, "DEFAULT_LIMITS")
      and not hasattr(HD, "_apply_rlimits"))
soft_nofile = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
out = subprocess.run([sys.executable, "-c",
                      "import resource;print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])"],
                     capture_output=True, text=True, preexec_fn=HD.build_preexec())
check("a sidecar spawned through build_preexec inherits the kernel's open-file limit",
      out.stdout.strip() == str(soft_nofile), f"{out.stdout.strip()} vs {soft_nofile}")

# The dead lesson store is gone
check("brain/mastercode/brain_memory.py (dead: nothing imports it) is deleted",
      not (ROOT / "brain" / "mastercode" / "brain_memory.py").exists())

if FAILS:
    print(f"\n[never-give-up] {len(FAILS)} FAILED")
    sys.exit(1)
print("\n[never-give-up] all checks passed")
