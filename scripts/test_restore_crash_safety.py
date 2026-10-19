#!/usr/bin/env python3
"""A restore that the kernel started on is never undone (server/host_restore.py).

Two ways the old restore lost the user's data, reproduced on a real folder tree
(the databases are stubs):

  1. finish() dropped the previous database and the set-aside files, then
     failed before it cleared the plan (a platform blip while recording the
     run). The plan still read "swapped", so the next boots counted as boots
     that never started; the third ran undo(), which moved the RESTORED files
     back into the staging folder (there was no previous database to return
     to any more), and finish() on the now-failed plan deleted that folder.
     The apps tree ended up empty, including whatever the user did on the
     restored Host in between.
  2. The only thing that said "the kernel started on the restored data" was
     finish(), and finish() ran from the backup loop, which does not start at
     all on a Host that cannot back up. Such a Host, restored and running
     fine, was put back to its old data on its third restart.

Both scenarios use only functions the old module also had, so the same script
can be pointed at an older host_restore.py/host_backup.py pair to show it fails
there (HR_FILE=... HB_FILE=...).

    python3 scripts/test_restore_crash_safety.py
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILED = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  [{detail}]"))
    if not cond:
        FAILED.append(label)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


if os.environ.get("HB_FILE"):
    hb = load("server.host_backup", os.environ["HB_FILE"])
    import server
    server.host_backup = hb
else:
    from server import host_backup as hb  # noqa: E402
if os.environ.get("HR_FILE"):
    hr = load("server.host_restore", os.environ["HR_FILE"])
else:
    from server import host_restore as hr  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="hr-crash-"))
live = tmp / "live"
hr.LIVE = live
hr.RESTORE_DIR = live / ".host-restore"
hr.STAGED = hr.RESTORE_DIR / "apps"
hr.PREVIOUS = hr.RESTORE_DIR / "previous"
hr.PLAN = hr.RESTORE_DIR / "plan.json"
hr.MANIFEST = hr.RESTORE_DIR / "manifest"
dbs = set()


def rename(a, b):
    for old, new in (a, b):
        dbs.discard(old)
        dbs.add(new)


hr._databases = lambda: set(dbs)
hr._rename_pair = rename


class Pool:
    """The database, as finish() sees it. `broken` makes the run record fail,
    the way a connection blip does."""

    def __init__(self):
        self.broken = False
        self.recorded = []

    async def execute(self, sql, *a):
        if sql.startswith("DROP DATABASE"):
            dbs.discard(sql.split('"')[1])
            return
        if self.broken and "INSERT INTO host_backup_runs" in sql:
            raise ConnectionRefusedError(111, "Connection refused")
        if "INSERT INTO host_backup_runs" in sql:
            self.recorded.append(a[1])
        return "OK"

    def acquire(self):
        pool = self

        class Conn:
            async def execute(self, *a):
                return "OK"

            def transaction(self):
                class T:
                    async def __aenter__(self):
                        return None

                    async def __aexit__(self, *a):
                        return False
                return T()

        class Ctx:
            async def __aenter__(self):
                return Conn()

            async def __aexit__(self, *a):
                return False
        return Ctx()

    async def fetch(self, sql, *a):
        return [{"datname": d} for d in dbs]

    async def fetchval(self, sql, *a):
        return None


async def _nothing(*a, **k):
    return None
hb._set_state = _nothing
hb._drop_state = _nothing
hb._state = _nothing


def put(base, rel, text):
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def swapped_restore():
    """A Host whose restore has just been swapped in at boot."""
    import shutil
    shutil.rmtree(live, ignore_errors=True)
    dbs.clear()
    dbs.update({"hostmem", "hostmem_restore"})
    put(live, "app-state/user-files/notes.txt", "before the restore\n")
    put(hr.STAGED, "app-state/user-files/notes.txt", "the restored notes\n")
    put(hr.STAGED, "proj/work.txt", "the restored project\n")
    hr._write_manifest()
    hr._write_plan({"state": hr.STAGED_STATE, "taken_at": "2026-09-27T02:00:00+00:00",
                    "stamp": "20260927T020000Z", "boots": 0})
    assert hr.swap("hostmem") == 0


def tree():
    return {str(p.relative_to(live)): p.read_text()
            for p in live.rglob("*") if p.is_file() and ".host-restore" not in p.parts}


print("== finish fails after dropping what was set aside")
swapped_restore()
pool = Pool()
pool.broken = True
try:
    asyncio.run(hr.finish(pool, 3))
except Exception:
    pass                                  # the backup loop logs this and carries on
put(live, "proj/work.txt", "the restored project + A DAY OF THE USER'S WORK\n")
pool.broken = False
for boot in (2, 3, 4):
    hr.swap("hostmem")                    # the Host restarts (a wake, a roll)
with_error = None
try:
    asyncio.run(hr.finish(pool, 3))
except Exception as e:
    with_error = e
now = tree()
check("the user's work on the restored Host survives three more restarts",
      now.get("proj/work.txt") == "the restored project + A DAY OF THE USER'S WORK\n", now)
check("and so do the restored files", now.get("app-state/user-files/notes.txt") == "the restored notes\n",
      now)
check("the restore ends recorded, not undone", hr.read_plan() is None and with_error is None,
      (hr.read_plan(), with_error))

print("== a restored Host that cannot back up still keeps its restore")
swapped_restore()
# The kernel starts fine. It cannot back up (say age is missing), so the
# backup loop never runs finish(); only the kernel's own start says so.
if hasattr(hr, "mark_started"):
    hr.mark_started()
put(live, "proj/work.txt", "the restored project + work\n")
rcs = [hr.swap("hostmem") for boot in (2, 3, 4)]
if hasattr(hr, "mark_started"):
    check("a boot after the kernel started finds nothing to swap (so no undo is armed)",
          rcs == [3, 3, 3], rcs)
now = tree()
check("three restarts later it is still on the restored data",
      now.get("app-state/user-files/notes.txt") == "the restored notes\n"
      and now.get("proj/work.txt") == "the restored project + work\n", now)
check("and nothing was put back", "hostmem_before_restore" in dbs or (hr.read_plan() or {}).get("state") != hr.FAILED,
      (dbs, hr.read_plan()))

print("== a kernel that never starts on the restored data is still put back")
swapped_restore()
for boot in (2, 3):
    rc = hr.swap("hostmem")
check("its third boot without a start undoes it",
      rc == 1 and (hr.read_plan() or {}).get("state") == hr.FAILED
      and tree().get("app-state/user-files/notes.txt") == "before the restore\n", (rc, hr.read_plan()))
asyncio.run(hr.finish(Pool(), 3))
check("and finish clears the unwanted copy without touching the live tree",
      tree().get("app-state/user-files/notes.txt") == "before the restore\n" and not hr.STAGED.exists())

print("== an undo cut short is carried on, never recorded as a restore")
if hasattr(hr, "UNDOING"):
    swapped_restore()
    _real_rename = hr._rename_pair
    calls = {"n": 0}

    def flaky_rename(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise hr.RestoreError("the database is busy")
        _real_rename(a, b)
    hr._rename_pair = flaky_rename
    try:
        hr.undo("hostmem", "the Host did not start")
    except hr.RestoreError:
        pass
    check("an undo that stops part way is marked as undoing",
          (hr.read_plan() or {}).get("state") == hr.UNDOING, hr.read_plan())
    p = Pool()
    asyncio.run(hr.finish(p, 3))
    check("finish records nothing and deletes nothing while an undo is unfinished",
          p.recorded == [] and hr.read_plan() is not None and hr.STAGED.exists(), p.recorded)
    check("mark_started does not turn it into a restore", hr.mark_started() is False)
    rc = hr.swap("hostmem")
    hr._rename_pair = _real_rename
    check("the next boot carries the undo on: the previous data is back",
          rc == 1 and (hr.read_plan() or {}).get("state") == hr.FAILED
          and tree().get("app-state/user-files/notes.txt") == "before the restore\n"
          and "hostmem_before_restore" not in dbs, (rc, hr.read_plan(), tree()))
    p = Pool()
    asyncio.run(hr.finish(p, 3))
    check("and finish records a failed restore, not a successful one", p.recorded == ["failed"], p.recorded)

print("== undo never runs once the previous database is gone")
swapped_restore()
dbs.discard("hostmem_before_restore")
hr.undo("hostmem", "test")
now = tree()
check("the restored data stays where it is",
      now.get("app-state/user-files/notes.txt") == "the restored notes\n"
      and (hr.read_plan() or {}).get("state") != hr.FAILED, (now, hr.read_plan()))

import shutil  # noqa: E402
shutil.rmtree(tmp, ignore_errors=True)
print()
if FAILED:
    print(f"FAILED: {len(FAILED)}")
    sys.exit(1)
print("OK: a restore the kernel started on is never undone")
