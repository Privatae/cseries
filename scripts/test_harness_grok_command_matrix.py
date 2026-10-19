#!/usr/bin/env python3
"""grok's commands, judged against the whole case matrix.

Built on how grok 0.2.112 was MEASURED to run commands in its pinned image:
every command,
foreground or background, runs in its own wrapper that is a DIRECT child of
grok and leads a NEW session; grok prints the turn's text about 200 ms before
it starts the command and prints nothing for the command itself. The stub
here does the same: `echo TEXT`, then 0.2 s, then `setsid <command>`.

Each row asserts the outcome AND the stop message:
  1. grok emitting events, with any children: never stopped
  2. grok silent, its own busy command: held to grok's maximum from the
     later of its start and grok's last event, never cut earlier (2b: one
     that never returns)
  3. grok silent, its command idle: the idle stop, naming a command
  4. grok silent: with its own background jobs all idle, the idle stop
     naming the command; with one busy (or crash-looping, or starting a
     worker), held to grok's maximum from its last event (the accepted cost
     of never cutting a working build); with no command at all, a model
     wait; with only a detached leftover (re-parented to pid 1), never a
     command, so the progress stop
  5. commands back to back while silent: each timed from its own start
  6. a burst of events then a busy command (and the slow-reader variant)
  7. a long background build grok waits on in silence at turn end:
     completes past the progress window
  8. an old busy background job with grok silent only briefly: not stopped

Three variants: a 0.3 s poll, a 0.25 s-per-frame slow reader, and the
production 5 s poll (windows scaled to fit). Each row runs in its OWN process,
so its CPU signal is its own. Processes are killed, so the whole file runs in
a private PID namespace (unshare -fp --mount-proc); outside one it re-executes
itself there.

Run: APP_ROOT=<repo> python3 scripts/test_harness_grok_command_matrix.py
"""
import concurrent.futures
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parents[1])
MAXMSG = "held to its own maximum"
IDLEMSG = "while a command was running (the command went idle)"
MODELMSG = "waited on the model"
PROGMSG = "no model or tool event"
VARIANTS = {
    # label: (poll s, unit s, idle min, model-wait min, progress min, max min, slow reader s)
    "poll0.3": (0.3, 1.0, 0.02, 0.02, 0.05, 0.1, 0.0),
    "lag0.25": (0.3, 1.0, 0.02, 0.02, 0.05, 0.1, 0.25),
    "poll5": (5.0, 5.0, 0.2, 0.2, 0.34, 0.5, 0.0),
}


def rows(py, u, X, d="/tmp", prog=3.0):
    # Each command is a small script file, so no quoting nests.
    def busy(sec):     # about a third of a core: over the activity floors
        return f"{py} {d}/busy.py {sec}"

    def cmd(c):        # a grok command: its turn's text, ~200 ms, its own session
        return f"echo TEXT\nsleep 0.2\nsetsid -w {c}\n"

    def job(c):        # a background job grok starts, then keeps talking
        return f"echo TEXT\nsleep 0.2\nsetsid -w {c} &\n"

    def talk(sec):
        return f"i=0\nwhile [ $i -lt {int(sec / 0.5)} ]; do echo TEXT; sleep 0.5; i=$((i+1)); done\n"
    churn = f"{py} {d}/churn.py {u}"
    later = f"{py} {d}/later.py {u} {X + 30 * u}"
    return {
        "1a events, no children": (talk(X + 4 * u) + "exit 0\n", "complete", None),
        "1b events + busy bg job": (job(busy(X + 30 * u)) + talk(X + 4 * u) + "exit 0\n", "complete", None),
        "1c events + churning bg job": (job(churn) + talk(X + 4 * u) + "exit 0\n", "complete", None),
        "2 silent, own busy command": (cmd(busy(X - 2 * u)) + "echo TEXT\nexit 0\n", "complete", None),
        "2b silent, own command never returns": (cmd(busy(X + 30 * u)), "timeout", MAXMSG),
        "3 silent, own command idle": (cmd("sleep 600"), "timeout", IDLEMSG),
        "4a silent, idle bg job": (job("sleep 600") + "echo TEXT\nexec sleep 600\n", "timeout", IDLEMSG),
        "4b silent, busy bg job": (job(busy(X + 30 * u)) + "echo TEXT\nexec sleep 600\n",
                                   "timeout", MAXMSG),
        "4c silent, churning bg job": (job(churn) + "echo TEXT\nexec sleep 600\n", "timeout", MAXMSG),
        "4d silent, bg job starts a worker later": (job(later) + talk(2 * u) + "exec sleep 600\n",
                                                    "timeout", MAXMSG),
        "4e silent, no command at all": ("echo TEXT\nexec sleep 600\n", "timeout", MODELMSG),
        "4f silent, a detached leftover only": (
            f"echo TEXT\nsleep 0.2\nsetsid -w sh -c \"{busy(X + 30 * u)} >/dev/null 2>&1 &\"\n"
            "echo TEXT\nexec sleep 600\n", "timeout", PROGMSG),
        "5 commands back to back": (cmd(busy(0.7 * X)) + f"setsid -w {busy(0.7 * X)}\necho TEXT\nexit 0\n",
                                    "complete", None),
        "6 burst of events then a busy command": ("echo TEXT\n" * 20 + cmd(busy(X - 2 * u))
                                                  + "echo TEXT\nexit 0\n", "complete", None),
        "7 long bg build waited on in silence": (job(busy(prog + 2 * u)) + talk(u) + "wait\n"
                                                 "echo TEXT\nexit 0\n", "complete", None),
        "8 old busy bg job, grok silent briefly": (job(busy(X + 30 * u)) + talk(X + 2 * u)
                                                   + f"sleep {u}\necho TEXT\nexit 0\n",
                                                   "complete", None),
    }


def one_row(variant: str, name: str) -> dict:
    """Run ONE row in this process and return its result."""
    sys.path.insert(0, os.path.join(_ROOT, "cli"))
    sys.path.insert(0, os.path.join(_ROOT, "system-apps"))
    from _harness_runtime import harness_core as hc, specs, events
    import logging
    logging.disable(logging.CRITICAL)
    poll, u, idle_min, wait_min, prog_min, max_min, slow = VARIANTS[variant]
    events.normalize_line = lambda h, line, seq, state=None: [
        {"ts": time.time(), "seq": seq, "kind": "text", "payload": {"text": line.strip()}}]
    KM = specs.ALL_SPECS["kimi"]
    hc._BARE_KEY_BASE_CACHE.clear()
    hc._BARE_KEY_BASE_PROBE = lambda b, k: b == KM.bare_key_base_url
    tmp = Path(tempfile.mkdtemp(prefix="grokrow-"))
    kp = tmp / "kp"
    kp.write_text("sk-moonshot-1")
    hc.IDLE_FLOOR_S, hc.ALIVE_FRAME_S, hc.TOOL_STEP_MINUTES = 0.5, 0.6, 0.05
    hc.POLL_S, hc.PROGRESS_MINUTES, hc.TOOL_STEP_MAX_MINUTES = poll, prog_min, max_min
    hc.TOOL_STEP_MARGIN_MINUTES = u / 60
    X = max_min * 60
    (tmp / "busy.py").write_text(
        "import sys, time\nt = time.time()\nwhile time.time() - t < float(sys.argv[1]):\n"
        "    s = time.time()\n    while time.time() - s < 0.05:\n        pass\n    time.sleep(0.1)\n")
    (tmp / "churn.py").write_text(
        "import subprocess, sys\nwhile True:\n"
        f"    subprocess.run([sys.executable, '{tmp}/busy.py', sys.argv[1]])\n")
    (tmp / "later.py").write_text(
        "import subprocess, sys, time\ntime.sleep(float(sys.argv[1]))\n"
        f"subprocess.run([sys.executable, '{tmp}/busy.py', sys.argv[2]])\n")
    script = rows(sys.executable, u, X, str(tmp), max(prog_min, wait_min) * 60)[name][0]
    stub = tmp / "grok"
    stub.write_text("#!/bin/sh\n" + script)
    stub.chmod(0o755)
    spec = dataclasses.replace(KM, id="grok", binary=stub, creds_path=kp)
    (tmp / "proj").mkdir()

    class Req:
        def __init__(self, b):
            self.body = json.dumps(b).encode()
    t0 = time.time()
    fr = []
    for f in hc.make_run_stream(spec)(Req({"prompt": "x", "project_dir": str(tmp / "proj"),
                                           "model": "k3", "idle_minutes": idle_min,
                                           "max_minutes": wait_min})):
        fr.append(f)
        if slow and f.get("kind") != "status":
            time.sleep(slow)
    st = [f for f in fr if f.get("kind") == "status"]
    p = st[-1]["payload"] if st else {}
    return {"state": p.get("state"), "error": p.get("error") or "", "el": time.time() - t0}


def _in_private_pid_namespace() -> bool:
    try:
        return Path("/proc/1/comm").read_text().strip() == "sh" and os.getppid() == 1 \
            and len([e for e in os.listdir("/proc") if e.isdigit()]) < 60
    except OSError:
        return False


def matrix() -> int:
    fails = []
    py = sys.executable
    for variant, (poll, u, idle_min, wait_min, prog_min, max_min, slow) in VARIANTS.items():
        X = max_min * 60
        names = list(rows(py, u, X, prog=max(prog_min, wait_min) * 60))

        def run(name):
            r = subprocess.run([py, os.path.abspath(__file__), "--row", variant, name],
                               capture_output=True, text=True, timeout=900,
                               env={**os.environ, "APP_ROOT": _ROOT})
            m = re.search(r"^ROW (.*)$", r.stdout, re.M)
            return name, (json.loads(m.group(1)) if m else
                          {"state": "crashed", "error": (r.stdout + r.stderr)[-300:], "el": 0})
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            results = dict(ex.map(run, names))
        print(f"== {variant}: poll {poll} s, idle {idle_min * 60:.1f} s, model wait "
              f"{wait_min * 60:.1f} s, progress {prog_min * 60:.0f} s, grok max {X:.0f} s + {u:.0f} s ==")
        for name, (_s, want, msg) in rows(py, u, X, prog=max(prog_min, wait_min) * 60).items():
            r = results[name]
            ok = r["state"] == want and (msg is None or msg in r["error"])
            if want == "timeout" and msg == MAXMSG:
                ok = ok and r["el"] >= X        # never cut before grok's maximum
            tag = "ok  " if ok else "FAIL"
            print(f"[grok-matrix] {tag} {variant}: {name} -> {want}" + (f" ({msg})" if msg else "")
                  + ("" if ok else f" — state={r['state']} el={r['el']:.1f} err={r['error'][:140]!r}"))
            if not ok:
                fails.append(f"{variant}: {name}")
    print()
    if fails:
        print(f"FAIL ({len(fails)}): {fails}")
        return 1
    print("all green")
    return 0


def main() -> int:
    if len(sys.argv) > 3 and sys.argv[1] == "--row":
        print("ROW " + json.dumps(one_row(sys.argv[2], sys.argv[3])), flush=True)
        return 0
    if _in_private_pid_namespace():
        return matrix()
    cmd = ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
           "sh", "-c", f'"{sys.executable}" "{os.path.abspath(__file__)}"; echo "rc=$?"']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1500,
                           env={**os.environ, "APP_ROOT": _ROOT})
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"FAIL: no private PID namespace here ({e!r}); this gate kills processes and "
              "will not run on the host.", file=sys.stderr)
        return 2
    m = re.search(r"rc=(\d+)\s*\Z", r.stdout)
    print(r.stdout[:m.start()].rstrip() if m else (r.stdout + r.stderr)[-2000:])
    if not m:
        return 2
    return 0 if m.group(1) == "0" else 1


if __name__ == "__main__":
    sys.exit(main())
