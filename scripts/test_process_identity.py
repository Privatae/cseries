#!/usr/bin/env python3
"""A process in the Host's container cannot make itself protected by its name.

server/process_awareness.py decides which processes the host may stop
(POST /api/system/processes/terminate) and which count as "expected". It used
to decide by substring over the command line: a process whose argv contained
`entrypoint`, `tini`, `server.launch` or `postgres: ` was expected and refused
— and argv is whatever the process says it is. This file pins the replacement:
protection comes only from what a process cannot choose (pid 1, the kernel's
own pid, the kernel's ancestry). Neither a command line nor an executable's
filename protects a same-uid process, and another uid is `other-user`
whatever it is called.

It starts a handful of short-lived children of its own (a forged-argv sleep, a
sleep copied to a file named `postgres`, plain sleeps) and stops them through
the real terminate(). EVERY signal the process sends while this runs goes
through a fence on os.kill that refuses — and fails the run for — any pid the
test did not start, so a regression that drops the pid-1 or kernel refusals
fails here instead of signalling the host's init.

Container-free. Run: APP_ROOT=<repo> python3 scripts/test_process_identity.py
"""
import asyncio
import os
import subprocess
import sys
import time

_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain")]

from server import process_awareness as pa  # noqa: E402

FAILED: list[str] = []
PASSED = 0

FORGED = "/app/entrypoint.sh tini python3 -m server.launch postgres: entity postgres -D"


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))


def _swap(**repl):
    saved = {k: getattr(pa, k) for k in repl}
    for k, v in repl.items():
        setattr(pa, k, v)
    return lambda: [setattr(pa, k, v) for k, v in saved.items()]


_OWN: set[int] = set()
_FENCED: list = []
_real_kill = os.kill


def _fenced_kill(pid, sig):
    """os.kill for the whole run: only the test's own children, ever."""
    if pid not in _OWN:
        _FENCED.append((pid, sig))
        raise PermissionError(f"test fence: {pid} is not one of this test's children")
    return _real_kill(pid, sig)


def _own(proc):
    _OWN.add(proc.pid)
    return proc


def main() -> int:
    os.kill = _fenced_kill
    try:
        return _main()
    finally:
        os.kill = _real_kill


def _main() -> int:
    print("process identity — a name never protects a process this container can signal")

    # A real child with a forged argv: every word the old substring rule trusted.
    child = _own(subprocess.Popen([FORGED, "300"], executable="/bin/sleep"))
    try:
        deadline = time.time() + 5
        cmdline = ""
        while time.time() < deadline and "server.launch" not in cmdline:
            cmdline = pa._read_cmdline(child.pid)
            time.sleep(0.02)
        check("the forged argv is what /proc reports (the test is testing the attack)",
              "server.launch" in cmdline and "entrypoint" in cmdline, cmdline)

        role, _w, _o, killable = pa._identify(child.pid, cmdline, "sleep")
        # MUTATION: identify by pa._label_by_name(cmdline) again.
        # RESULT: red — the child reads as `entrypoint`/`kernel`, unkillable.
        check("a same-uid process with every protected word in its argv is UNKNOWN and killable",
              role == "unknown" and killable is True, f"role={role} killable={killable}")

        snap = pa.snapshot(include_db=False)
        row = next((p for p in snap["processes"] if p["pid"] == child.pid), None)
        check("the snapshot lists it as unexpected, not as part of the machine",
              row is not None and row["expected"] is False and row["role"] == "unknown", row)

        # ── the protected roles, by what cannot be forged ──
        check("pid 1 is init, whatever it is called",
              pa._identify(1, "anything at all", "x")[0] == "init")
        check("this process is the kernel, whatever it is called",
              pa._identify(os.getpid(), "definitely-not-the-kernel", "x")[:1] == ("kernel",)
              and pa._identify(os.getpid(), "", "")[3] is False)
        ppid = os.getppid()
        if ppid > 1:
            # MUTATION: drop the _kernel_ancestors() branch.
            # RESULT: red — the parent reads as unknown and becomes killable.
            check("an ancestor of the kernel is the entrypoint, and unkillable",
                  pa._identify(ppid, "some-shell", "sh")[0] == "entrypoint"
                  and pa._identify(ppid, "", "")[3] is False)

        # ── a chosen EXECUTABLE NAME is no better than a chosen argv ──
        # Copying a binary to a file named `postgres` defeats a check by
        # executable name. This does exactly that, for real.
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        fake = os.path.join(tmp, "postgres")
        # A copy of the Python interpreter, not of /bin/sleep: where sleep is a
        # multicall coreutils binary (uutils: /usr/lib/cargo/bin/coreutils) it
        # dispatches on its own name and, copied as `postgres`, can exit at once
        # with "coreutils: unknown program 'postgres'" — the fake died, the
        # check below failed, and "terminate() stops it" passed on a corpse
        # (measured: 6 of 24 concurrent spawns). Python runs under
        # any filename.
        shutil.copy(os.path.realpath(sys.executable), fake)
        pg = _own(subprocess.Popen(
            ["postgres: entity entity [local] idle", "-c", "import time; time.sleep(300)"],
            executable=fake))
        try:
            # Wait for the thing checked, not for the link to exist: /proc/<pid>/exe
            # exists from the fork (pointing at this python) before the child
            # execs, so an existence poll can end before the exec.
            def _exe():
                try:
                    return os.readlink(f"/proc/{pg.pid}/exe")
                except OSError:
                    return ""
            deadline = time.time() + 5
            while time.time() < deadline and not _exe().endswith("/postgres"):
                time.sleep(0.02)
            check("the fake really runs as .../postgres (the test is testing the attack)",
                  _exe().endswith("/postgres"), _exe())
            # MUTATION: restore a basename(exe) in {postgres, postmaster} branch.
            # RESULT: red — the fake reads as postgres-backend, unkillable.
            r = pa._identify(pg.pid, pa._read_cmdline(pg.pid), "postgres")
            check("a same-uid binary NAMED postgres with a postgres title is unknown and killable",
                  r[0] == "unknown" and r[3] is True, r)
            res = asyncio.run(pa.terminate(pg.pid))
            check("…and terminate() stops it", res.get("ok") is True, res)
        finally:
            if pg.poll() is None:
                pg.kill()
            pg.wait()
            shutil.rmtree(tmp, ignore_errors=True)

        # ── another uid: never labelled by its name, never signalled, never expected ──
        restore = _swap(_read_uid=lambda pid: os.getuid() + 1)
        try:
            r = pa._identify(424242, "postgres: entity entity idle", "postgres")
            # MUTATION: label another uid from its name again.
            # RESULT: red — it reads as postgres-backend, expected.
            check("another uid's process titled like postgres is still `other-user`, unkillable",
                  r[0] == "other-user" and r[3] is False, r)
            r = pa._identify(424242, "something-else", "x")
            # MUTATION: return _UNKNOWN (killable) for another uid.
            # RESULT: red.
            check("another uid's unrecognised process is `other-user`, unkillable, with its own reason",
                  r[0] == "other-user" and r[3] is False
                  and "another user" in pa._kill_block_reason("other-user"), r)
        finally:
            restore()

        # The snapshot's `expected` for another uid: unkillable from here is not
        # the same fact as "belongs here".
        # MUTATION: expected = role != "unknown" (other-user counted as expected).
        # RESULT: red.
        plain = _own(subprocess.Popen(["/bin/sleep", "300"]))
        try:
            real_uid = pa._read_uid
            foreign = {child.pid, plain.pid}
            restore = _swap(_read_uid=lambda pid: (os.getuid() + 1) if pid in foreign else real_uid(pid))
            try:
                deadline = time.time() + 5
                rows = {}
                while time.time() < deadline and len(rows) < 2:
                    rows = {p["pid"]: p for p in pa.snapshot(include_db=False)["processes"] if p["pid"] in foreign}
                    time.sleep(0.02)
                r = rows.get(plain.pid)
                check("another uid's unrecognised process is listed as NOT expected",
                      r is not None and r["role"] == "other-user" and r["expected"] is False
                      and r["killable"] is False, r)
                r = rows.get(child.pid)
                # MUTATION: keep every name label for another uid.
                # RESULT: red — the forged `tini` in its argv makes it `init`.
                check("another uid's process cannot take init/kernel/entrypoint by name",
                      r is not None and r["role"] not in ("init", "kernel", "entrypoint")
                      and r["killable"] is False, r)
            finally:
                restore()
        finally:
            plain.kill()
            plain.wait()

        # ── the real kill path, on our own forged child ──
        res = asyncio.run(pa.terminate(child.pid))
        # MUTATION: identify in terminate() by name.
        # RESULT: red — refused as a protected role.
        check("terminate() stops the forged process instead of refusing it as protected",
              res.get("ok") is True, res)
        # MUTATION: drop the zombie check from _still_the_same.
        # RESULT: red — the dead-but-unreaped child reads as having ignored SIGTERM.
        check("…and it is gone (a zombie awaiting reaping counts as stopped)",
              child.wait(timeout=5) is not None)
        # A process already dead and not yet reaped is reported as exited, not
        # as "the number has been reused".
        # MUTATION: remove the _exited() early return in terminate().
        # RESULT: red — the reused-pid refusal comes back.
        z = _own(subprocess.Popen(["/bin/sleep", "300"]))
        os.kill(z.pid, 9)
        deadline = time.time() + 5
        while time.time() < deadline and pa._read_state(z.pid) != "Z":
            time.sleep(0.02)
        zr = asyncio.run(pa.terminate(z.pid))
        check("terminate() on an unreaped zombie says it already exited",
              zr.get("ok") is True and "already exited" in zr.get("result", ""), zr)
        z.wait()
        check("terminate() still refuses the kernel", asyncio.run(pa.terminate(os.getpid())).get("ok") is False)
        check("terminate() still refuses pid 1", asyncio.run(pa.terminate(1)).get("ok") is False)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()

    # MUTATION: drop terminate()'s pid-1 refusal AND _identify's pid-1 branch.
    # RESULT: red here, and the fence absorbed the signal instead of init.
    check("nothing outside the test's own children was ever signalled", not _FENCED, _FENCED)
    print(f"\n{PASSED}/{PASSED + len(FAILED)} passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
