#!/usr/bin/env python3
"""Gate: no reaper removes a container whose maker is alive but hidden from
/proc (2026-10-08).

The CI runner runs three jobs at once, under ProtectProc=invisible, so one
job's /proc hides another job's processes while kill(pid, 0) still finds them
(measured 2026-10-08). Two reapers read a missing /proc entry as "the run that
made this is gone": smoke's (one CI run's smoke job removed another run's live
pod) and the restore gate's (two runs had their database shut down
mid-run beside another gates job). Every reaper now uses kernel_schema.py's
rule (maker_gone; scripts/gate_maker.sh is its shell copy): the maker is gone
only when kill(pid, 0) finds no such process, /proc shows it a zombie, or /proc
shows its pid started at another time than its label says. A pid /proc does
not show is alive, and so is one kill(pid, 0) answers with EPERM.

  A. the rule, on one table of cases, in Python and in shell
  B. each Python reaper through its own entry point, against a fake docker:
     a hidden live maker's container stays (and an EPERM one's), a gone
     maker's is removed, other kinds and unlabelled containers are never
     touched, and each removal is logged with the container, the maker's pid
     and why. kernel_schema.reap_throwaway_leftovers,
     connector_gate_kit.reap_pods, test_connector_telegram_example.reap_pods,
     and test_restore_carry_real_schema.py run as itself up to its database
     start. Smoke's reaper is scripts/test_smoke_reap_leftovers.py.
  C. what the makers label: maker_labels and gate_maker_args agree, and the
     restore gate's container carries the labels under a name the reaper
     before this one cannot parse

No docker, no real process, no signal (scripts/gate_reap_fakes.py): docker is
a fake on PATH, /proc a directory, kill a stand-in that only answers signal 0,
and every pid is above the kernel's pid limit.
"""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import kernel_schema as ks  # noqa: E402
from gate_reap_fakes import PID_BASE, FakeDocker, FakeKill, FakeProc, bash_kill, maker  # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond else f"  [{str(detail)[:700]}]"))
    if not cond:
        FAILS.append(name)


NS, BOOT = str(ks.PIDNS), ks.BOOT_ID
if not ks.PIDNS or not BOOT:
    print("cannot run: no PID namespace or boot id here, so the rule judges nothing (exit 2)")
    sys.exit(2)

P = PID_BASE
# (case, pid, labels (pidns, start, boot), what /proc shows (state, start, name) or None,
#  what kill(pid, 0) says, gone?)
CASES = [
    ("/proc does not show it and kill(pid, 0) finds it: alive", P + 1, (NS, "500", BOOT), None, "alive", False),
    ("/proc does not show it and kill(pid, 0) says EPERM: alive", P + 2, (NS, "500", BOOT), None, "eperm", False),
    ("kill(pid, 0) finds no such process: gone", P + 3, (NS, "500", BOOT), None, "esrch", True),
    ("/proc shows the start its label has: alive", P + 4, (NS, "500", BOOT), ("S", "500", "gate"), "alive", False),
    ("/proc shows a later start, a newer process has the pid: gone",
     P + 5, (NS, "500", BOOT), ("S", "900", "gate"), "alive", True),
    ("/proc shows a zombie: gone", P + 6, (NS, "500", BOOT), ("Z", "500", "gate"), "alive", True),
    ("a process name holding ') ' is read up to its last ')': gone",
     P + 7, (NS, "500", BOOT), ("S", "901", "a) b"), "alive", True),
    ("no start in the label, /proc shows the pid: alive", P + 8, (NS, "", BOOT), ("S", "900", "gate"), "alive", False),
    ("no start in the label, hidden, kill finds it: alive", P + 9, (NS, "", BOOT), None, "alive", False),
    ("no start in the label, no such process: gone", P + 10, (NS, "", BOOT), None, "esrch", True),
    ("labelled in another PID namespace: not judged", P + 11, ("999", "500", BOOT), None, "esrch", False),
    ("labelled on another boot: not judged", P + 12, (NS, "500", "another-boot"), None, "esrch", False),
    ("no boot in the label: not judged", P + 13, (NS, "500", ""), None, "esrch", False),
    ("no PID namespace in the label: not judged", P + 14, ("", "500", BOOT), None, "esrch", False),
    ("PID namespace 0 (unknown) in the label: not judged", P + 15, ("0", "500", BOOT), None, "esrch", False),
    ("a pid that is not a number: not judged", "12x", (NS, "500", BOOT), None, "esrch", False),
]
KILL = {pid: k for _c, pid, _l, _s, k, _g in CASES if isinstance(pid, int)}


@contextlib.contextmanager
def proc_at(path: Path):
    real = getattr(ks, "PROC", None)
    ks.PROC = path
    try:
        yield
    finally:
        ks.PROC = real


with tempfile.TemporaryDirectory(prefix="gate-reapers-") as _td:
    td = Path(_td)
    proc = FakeProc(td / "proc")
    for _c, pid, _l, shown, _k, _g in CASES:
        if shown:
            proc.show(pid, *shown)

    # ── A. the rule, in Python and in shell ──────────────────────────────────
    print("A. the rule (kernel_schema.maker_gone, gate_maker.sh gate_maker_gone), one table")
    if not callable(getattr(ks, "maker_gone", None)):
        check("A kernel_schema.maker_gone exists", False, "missing")
    else:
        kill = FakeKill(KILL)
        with proc_at(proc.root), kill.installed():
            py = {c: ks.maker_gone(str(pid), *labels) for c, pid, labels, _s, _k, _g in CASES}
        for c, _p, _l, _s, _k, gone in CASES:
            check(f"A python: {c}", bool(py[c]) == gone, py[c])
        check("A python: no signal but 0 was ever sent", kill.signals == [], kill.signals)

    lib = SCRIPTS / "gate_maker.sh"
    kill_log = td / "kill-a.log"
    script = ["set -u", f"source {shlex.quote(str(lib))}", bash_kill(KILL, kill_log)]
    for i, (_c, pid, (ns, start, boot), _s, _k, _g) in enumerate(CASES):
        q = [shlex.quote(str(x)) for x in (pid, ns, start, boot)]
        script.append(f'if gate_maker_gone {" ".join(q)}; then echo "{i}|gone|$GATE_MAKER_WHY"; '
                      f'else echo "{i}|kept|$GATE_MAKER_WHY"; fi')
    r = subprocess.run(["bash", "-c", "\n".join(script)], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "GATE_MAKER_PROC": str(proc.root)})
    sh = {}
    for line in r.stdout.splitlines():
        i, verdict, why = line.split("|", 2)
        sh[CASES[int(i)][0]] = (verdict == "gone", why)
    check("A shell: scripts/gate_maker.sh is sourced and answers every case", r.returncode == 0 and len(sh) == len(CASES),
          (r.returncode, r.stderr[-400:]))
    for c, _p, _l, _s, _k, gone in CASES:
        got = sh.get(c)
        check(f"A shell: {c}", got is not None and got[0] == gone and bool(got[1]) == gone, got)
    calls = kill_log.read_text().splitlines() if kill_log.exists() else []
    check("A shell: kill was only ever asked -0", all(c.startswith("kill -0 ") for c in calls), calls)

    # ── B. each Python reaper, through its own entry point ───────────────────
    print("B. each reaper, against a fake docker, a /proc that shows none of its makers, and a stand-in kill")
    HID, EPERM, DEAD, OTHER = P + 101, P + 102, P + 103, P + 104
    B_KILL = {HID: "alive", EPERM: "eperm", DEAD: "esrch", OTHER: "esrch"}
    empty_proc = FakeProc(td / "proc-empty").root

    def fleet(kind: str, name_of) -> tuple[dict, dict]:
        names = {"hidden": name_of(HID), "eperm": name_of(EPERM), "dead": name_of(DEAD),
                 "other kind": f"other-kind-{OTHER}", "unlabelled": f"unlabelled-{DEAD}"}
        containers = {names[k]: {"labels": maker(kind, pid, NS, "500", BOOT)}
                      for k, pid in (("hidden", HID), ("eperm", EPERM), ("dead", DEAD))}
        containers[names["other kind"]] = {"labels": maker("another-kind", OTHER, NS, "500", BOOT)}
        containers[names["unlabelled"]] = {"labels": {}}
        return containers, names

    def judged(label: str, docker: FakeDocker, names: dict, log: str) -> None:
        removed = docker.removed()
        check(f"B {label}: a maker /proc does not show, which kill(pid, 0) finds, keeps its container",
              names["hidden"] not in removed, removed)
        check(f"B {label}: ...and so does one kill(pid, 0) answers with EPERM", names["eperm"] not in removed, removed)
        check(f"B {label}: the container of a maker that is gone is removed", names["dead"] in removed, removed)
        check(f"B {label}: another kind's container and an unlabelled one are never touched",
              not {names["other kind"], names["unlabelled"]} & set(removed), removed)
        line = next((ln for ln in log.splitlines() if names["dead"] in ln), "")
        check(f"B {label}: the removal is logged with the container, its maker's pid and why",
              str(DEAD) in line.replace(names["dead"], "") and "no such process" in line, log[-500:])

    def in_process(label: str, kind: str, name_of, call) -> None:
        docker = FakeDocker(td / f"docker-{kind}")
        containers, names = fleet(kind, name_of)
        docker.load(containers)
        err = io.StringIO()
        kill = FakeKill(B_KILL)
        with docker.in_process(), proc_at(empty_proc), kill.installed(), contextlib.redirect_stderr(err):
            try:
                call()
            except Exception as e:  # noqa: BLE001 — a reaper that raises is a finding, not a crash
                print(f"  ({label} raised {e!r})")
        judged(label, docker, names, err.getvalue())
        check(f"B {label}: no signal but 0 was ever sent", kill.signals == [], kill.signals)

    in_process("kernel_schema.reap_throwaway_leftovers", ks.THROWAWAY_KIND,
               lambda pid: f"kernel-schema-{pid}-15432", ks.reap_throwaway_leftovers)

    import connector_gate_kit as kit  # noqa: E402
    in_process("connector_gate_kit.reap_pods", "gate-test-pod",
               lambda pid: f"gate-test-pod-{pid}-15432", lambda: kit.reap_pods("gate-test-pod"))

    import test_connector_telegram_example as tg  # noqa: E402
    in_process("test_connector_telegram_example.reap_pods", tg.POD_KIND,
               lambda pid: f"telegram-pod-{pid}-15432", tg.reap_pods)

    # The restore gate reaps at the top of its own script, so it runs as
    # itself: os.kill and kernel_schema.PROC are set before the gate starts, and
    # the fake docker refuses the gate's `docker run`, which ends it there. The
    # containers have the old pid-only names, which the reaper before this one
    # parsed, so the same run shows what that reaper did to them.
    label = "test_restore_carry_real_schema.py"
    docker = FakeDocker(td / "docker-restore")
    containers, names = fleet("carry-real", lambda pid: f"carry-real-{pid}")
    docker.load(containers)
    shared = td / "shared"
    for pid in (HID, DEAD):
        (shared / f".cr-{pid}" / "s").mkdir(parents=True)
    (td / "tmp").mkdir()
    # The stand-in /proc shows none of the makers, only the gate itself (its
    # own stat, copied in), which it reads for its own label.
    wrapper = (
        "import json, os, runpy, sys\n"
        "from pathlib import Path\n"
        "scripts = Path(os.environ['APP_ROOT']) / 'scripts'\n"
        "sys.path.insert(0, str(scripts))\n"
        "from gate_reap_fakes import FakeKill\n"
        "os.kill = FakeKill(json.loads(os.environ['GATE_TEST_KILL']))\n"
        "proc = Path(os.environ['GATE_TEST_PROC'])\n"
        "(proc / str(os.getpid())).mkdir()\n"
        "(proc / str(os.getpid()) / 'stat').write_text(Path('/proc/self/stat').read_text())\n"
        "import kernel_schema\n"
        "kernel_schema.PROC = proc\n"
        "gate = str(scripts / 'test_restore_carry_real_schema.py')\n"
        "sys.argv = [gate]\n"
        "runpy.run_path(gate, run_name='__main__')\n")
    env = docker.env({**os.environ, "APP_ROOT": str(ROOT), "CSERIES_GATE_SHARED_DIR": str(shared),
                      "TMPDIR": str(td / "tmp"), "GATE_TEST_KILL": json.dumps(B_KILL),
                      "GATE_TEST_PROC": str(FakeProc(td / "proc-restore").root)})
    if not docker.is_the_docker(env):
        check(f"B {label}: `docker` is the fake (nothing ran)", False, env["PATH"])
    else:
        r = subprocess.run([sys.executable, "-c", wrapper], capture_output=True, text=True, timeout=180,
                           env=env, cwd=td)
        calls = docker.calls()
        check(f"B {label}: the gate ran its reap and went on to start its database",
              any(c[:1] == ["ps"] for c in calls) and any(c[:1] == ["run"] for c in calls),
              (calls, r.stdout[-400:], r.stderr[-600:]))
        check(f"B {label}: its own checks before that, the leftovers one among them, pass",
              not [ln for ln in r.stdout.splitlines() if ln.startswith("  FAIL ")], r.stdout[-600:])
        judged(label, docker, names, r.stdout + r.stderr)
        check(f"B {label}: the gone run's socket folder is removed, the hidden live run's kept",
              not (shared / f".cr-{DEAD}").exists() and (shared / f".cr-{HID}").exists(),
              sorted(p.name for p in shared.iterdir()))

        # ── C. the restore gate's container: labels and name ─────────────────
        run = next((c for c in calls if c[:1] == ["run"]), [])
        labels = dict(run[i + 1].split("=", 1) for i, a in enumerate(run[:-1]) if a == "--label")
        name = run[run.index("--name") + 1] if "--name" in run else ""
        # The gate made its own socket folder before the refused `docker run`,
        # and a refused start leaves it: under CSERIES_GATE_SHARED_DIR, or next
        # to the checkout when that path is too long for a socket.
        gate_pid = labels.get("cseries.gate.pid") or name.removeprefix("carry-real-").split("-")[0]
        if gate_pid.isdigit():
            for base in (shared, ROOT.parent, td / "tmp"):
                if (base / f".cr-{gate_pid}").is_dir():
                    shutil.rmtree(base / f".cr-{gate_pid}", ignore_errors=True)
        check("C the restore gate labels its database with its maker (kind, pid, PID namespace, start, boot)",
              labels.get("cseries.gate.throwaway") == "carry-real" and labels.get("cseries.gate.pidns") == NS
              and labels.get("cseries.gate.boot") == BOOT and labels.get("cseries.gate.pid", "").isdigit()
              and labels.get("cseries.gate.pidstart", "").isdigit(), labels)
        check("C ...under a name the reaper before this one cannot parse (carry-real-<pid> only)",
              name.startswith(f"carry-real-{labels.get('cseries.gate.pid')}-")
              and not name.removeprefix("carry-real-").isdigit(), name)

    # ── C. maker_labels and gate_maker_args agree ────────────────────────────
    print("C. what the makers write")
    if callable(getattr(ks, "maker_labels", None)):
        py_labels = ks.maker_labels("some-kind")
        check("C kernel_schema.maker_labels names this process: kind, pid, PID namespace, start time, boot id",
              py_labels == {"cseries.gate.throwaway": "some-kind", "cseries.gate.pid": str(os.getpid()),
                            "cseries.gate.pidns": NS, "cseries.gate.pidstart": ks._proc_start(os.getpid()),
                            "cseries.gate.boot": BOOT} and py_labels["cseries.gate.pidstart"].isdigit(), py_labels)
    else:
        check("C kernel_schema.maker_labels exists", False, "missing")
    # Under smoke_test.sh's own `set -euo pipefail`, against the real /proc.
    r = subprocess.run(["bash", "-c", f'set -euo pipefail; source {shlex.quote(str(lib))}; gate_maker_args smoke; '
                        'printf "%s\\n" "$$" "$GATE_MAKER_START" "${GATE_MAKER_ARGS[@]}"'],
                       capture_output=True, text=True, timeout=60)
    out = r.stdout.splitlines()
    sh_labels = dict(out[i + 1].split("=", 1) for i in range(2, len(out) - 1) if out[i] == "--label")
    check("C gate_maker_args writes the same labels for the shell that sources it, and returns 0 under set -e",
          r.returncode == 0 and len(out) > 2 and sh_labels == {
              "cseries.gate.throwaway": "smoke", "cseries.gate.pid": out[0], "cseries.gate.pidns": NS,
              "cseries.gate.pidstart": out[1], "cseries.gate.boot": BOOT} and out[1].isdigit(),
          (r.returncode, out, r.stderr[-300:]))
    for path, call in (("connector_gate_kit.py", "maker_labels(kind)"),
                       ("test_connector_telegram_example.py", "maker_labels(POD_KIND)")):
        check(f"C {path}'s Pod labels its containers with {call}", call in (SCRIPTS / path).read_text())

print()
if FAILS:
    print(f"FAIL: {len(FAILS)} check(s)")
    sys.exit(1)
print("PASS: no reaper takes a hidden live maker for a gone one, and every removal says why")
