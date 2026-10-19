#!/usr/bin/env python3
"""A tool that sheds both its session and the run's marker is still the run's
when the pod serves one run; bystanders and other runs are never touched; the
marker is per attempt.

`setsid env -i <tool>` leaves the CLI's process group, its session and
the per-run environment marker, so neither accounting nor the end-of-run kill
could find it. When this server runs exactly ONE attempt, and both
independent container readings agree, every process that appeared in the
container during the attempt (other than pid 1, the server and its other
direct children) is that attempt's. With two runs in the pod nothing is
claimed that way.

The marker is per attempt, so a stale attempt-1 watchdog or sweep can
never reach attempt 2's processes.

This file sends REAL signals, so it runs only inside a private PID namespace
(unshare --user --pid --fork --mount-proc): the "pod" is that namespace, and
nothing outside it can be signalled. Outside one it re-executes itself there.

Run: APP_ROOT=<repo> python3 scripts/test_harness_run_accounting_pod.py
"""
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

FAILS = []
_ROOT = os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parents[1])


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[pod-accounting] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _in_private_pid_namespace() -> bool:
    # pid 1 here is a shell copied under the name `tini`, so the harness's
    # REAL container checks (_in_harness_container, _container_recheck) read
    # this namespace as the harness container.
    try:
        return Path("/proc/1/comm").read_text().strip() == "tini" and os.getppid() == 1 \
            and len([e for e in os.listdir("/proc") if e.isdigit()]) < 50
    except OSError:
        return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:   # a zombie is gone for our purposes
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


class Req:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()


def inner() -> int:
    sys.path.insert(0, os.path.join(_ROOT, "cli"))
    sys.path.insert(0, os.path.join(_ROOT, "system-apps"))
    from _harness_runtime import harness_core as hc
    from _harness_runtime import specs
    from _harness_runtime import events

    events.normalize_line = lambda h, line, seq, state=None: [{
        "ts": time.time(), "seq": seq,
        "kind": {"TOOL_CALL": "tool_call", "TOOL_RESULT": "tool_result"}.get(
            line.strip(), "text"), "payload": {"text": line.strip()}}]
    KM = specs.ALL_SPECS["kimi"]
    hc._BARE_KEY_BASE_CACHE.clear()
    hc._BARE_KEY_BASE_PROBE = lambda b, k: b == KM.bare_key_base_url
    tmp = Path(tempfile.mkdtemp(prefix="podacct-"))
    kp = tmp / "kp"
    kp.write_text("sk-moonshot-1")
    hc.IDLE_FLOOR_S = 0.5
    # The REAL pair of container checks, not stand-ins.
    check("the real container checks both read this namespace as the harness container",
          hc._in_harness_container() and hc._container_recheck())
    py = sys.executable

    def run(name, script, **extra):
        stub = tmp / name
        stub.write_text("#!/bin/sh\n" + script)
        stub.chmod(0o755)
        spec = dataclasses.replace(KM, binary=stub, creds_path=kp)
        proj = tmp / f"proj-{name}"
        proj.mkdir(exist_ok=True)
        body = {"prompt": "x", "project_dir": str(proj), "model": "k3",
                "idle_minutes": 0.02, "max_minutes": 0.05, **extra}
        fr = list(hc.make_run_stream(spec)(Req(body)))
        st = [f for f in fr if f.get("kind") == "status"]
        return st[-1]["payload"] if st else {}

    def escapee(pidfile, secs=60):
        return (f"( setsid env -i {py} -c 'import os,time\n"
                f"open(\"{pidfile}\",\"w\").write(str(os.getpid()))\n"
                f"t=time.time()\nwhile time.time()-t<{secs}: time.sleep(0.2)' "
                "</dev/null >/dev/null 2>&1 & )\n")

    print("== one run in the pod: an env -i + setsid tool is the run's ==")
    bystander = subprocess.Popen([py, "-c", "import time; time.sleep(120)"],
                                 start_new_session=True)
    time.sleep(0.3)
    # A /status probe or a login: another child of the server, started DURING
    # the run, with a grandchild of its own. Neither is the run's.
    probe_pf = tmp / "probe-grandchild.pid"
    holder = {}

    def _late_probe():
        time.sleep(0.4)
        holder["p"] = subprocess.Popen(
            ["sh", "-c", f"( {py} -c 'import os,time; open(\"{probe_pf}\",\"w\")"
                         f".write(str(os.getpid())); time.sleep(120)' & ) ; sleep 120"])
    threading.Thread(target=_late_probe, daemon=True).start()
    pf = tmp / "esc1.pid"
    final = run("single", escapee(pf) + "echo TEXT\nsleep 1.5\necho TEXT\nexit 0\n")
    time.sleep(0.5)
    esc = int(pf.read_text()) if pf.exists() else None
    check("the run completed", final.get("state") == "complete", repr(final.get("error")))
    check("the escaped tool (no session, no marker) is stopped when the run ends",
          esc is not None and not _alive(esc), f"pid {esc} alive={esc and _alive(esc)}")
    check("a bystander that was there before the run survives it", _alive(bystander.pid))
    gp = int(probe_pf.read_text()) if probe_pf.exists() else None
    check("another child of the server started during the run survives it",
          holder.get("p") is not None and holder["p"].poll() is None)
    check("...and so does that child's own child (re-parented, no marker)",
          gp is not None and _alive(gp), f"pid {gp}")
    for pid in [gp] + ([holder["p"].pid] if holder.get("p") else []):
        try:
            os.kill(pid, 9)
        except (OSError, TypeError):
            pass

    print("== two runs in the pod: nothing is claimed by appearance ==")
    other_pf = tmp / "other.pid"
    other_res = {}

    def _other():
        other_res["final"] = run("other", f"echo $$ > {other_pf}\necho TEXT\nsleep 4\n"
                                          "echo TEXT\nexit 0\n", max_minutes=0.2)
    t = threading.Thread(target=_other)
    t.start()
    time.sleep(0.8)
    pf2 = tmp / "esc2.pid"
    run("second", escapee(pf2) + "echo TEXT\nsleep 1\necho TEXT\nexit 0\n")
    other_cli = int(other_pf.read_text()) if other_pf.exists() else None
    check("the other run's CLI survives this run's end",
          other_cli is not None and _alive(other_cli), f"pid {other_cli}")
    t.join(timeout=20)
    check("...and the other run completes", (other_res.get("final") or {}).get("state") == "complete",
          repr(other_res))
    check("the bystander survives this too", _alive(bystander.pid))
    for f in (pf2,):
        try:
            os.kill(int(f.read_text()), 9)
        except (OSError, ValueError):
            pass
    bystander.kill()
    bystander.wait()

    print("== the marker is per attempt ==")
    seen = tmp / "markers.txt"
    cut = ("error: failed to run prompt: provider.connection_error: Connection error.")
    cnt = tmp / "cnt"
    final = run("retry", f"""
n=$(cat {cnt} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {cnt}
echo "$CSERIES_HARNESS_RUN" >> {seen}
echo TEXT
if [ $n -eq 1 ]; then echo "{cut}" >&2; exit 1; fi
exit 0
""")
    marks = [m for m in seen.read_text().split() if m] if seen.exists() else []
    check("the transient re-run happened", final.get("retries") == 1, repr(final.get("retries")))
    check("each attempt carried its own marker", len(marks) == 2 and marks[0] != marks[1],
          repr(marks))

    print()
    if FAILS:
        print(f"FAIL ({len(FAILS)}): {FAILS}")
        return 1
    print("all green")
    return 0


def main() -> int:
    if _in_private_pid_namespace():
        return inner()
    import shutil
    init_dir = Path(tempfile.mkdtemp(prefix="podinit-"))
    tini = init_dir / "tini"
    shutil.copy2(shutil.which("sh") or "/bin/sh", tini)
    cmd = ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
           str(tini), "-c", f'"{sys.executable}" "{os.path.abspath(__file__)}"; echo "rc=$?"']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           env={**os.environ, "APP_ROOT": _ROOT})
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"FAIL: no private PID namespace here ({e!r}); this gate sends real "
              "signals and will not run on the host.", file=sys.stderr)
        return 2
    m = re.search(r"rc=(\d+)\s*\Z", r.stdout)
    print(r.stdout[:m.start()].rstrip() if m else (r.stdout + r.stderr)[-2000:])
    if not m:
        return 2
    return 0 if m.group(1) == "0" else 1


if __name__ == "__main__":
    sys.exit(main())
