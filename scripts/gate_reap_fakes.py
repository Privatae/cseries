"""Stand-ins for the reaper gates (scripts/test_gate_reapers_hidden_proc.py,
scripts/test_smoke_reap_leftovers.py): a docker, a /proc and a kill, so a
reaper runs its real code with no container and no real process involved.

  FakeDocker  a `docker` on PATH that lists the containers and volumes a JSON
              state file holds, removes them when asked, refuses everything
              else, and logs every call. DOCKER_HOST points nowhere, so a real
              docker, if one were ever reached instead, reaches no daemon.
  FakeProc    a directory of <pid>/stat files, for kernel_schema.PROC and
              gate_maker.sh's GATE_MAKER_PROC.
  FakeKill    an os.kill that answers signal 0 from a table and never sends a
              signal; bash_kill is the same as a bash function.

Every pid the gates use is above PID_MAX_LIMIT (4194304, the most pid_max can
be), so no real process has one: a stand-in that were bypassed would still
reach nothing.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import shlex
import shutil
from pathlib import Path

PID_MAX_LIMIT = 4194304
PID_BASE = 5_000_000          # every pid in the reaper gates is above PID_MAX_LIMIT

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""docker for the reaper gates: serves ps, volume ls, rm, stop and volume rm
from $FAKE_DOCKER_STATE, refuses anything else, logs each call."""
import json, os, re, sys

state_file, log_file = os.environ["FAKE_DOCKER_STATE"], os.environ["FAKE_DOCKER_LOG"]
args = sys.argv[1:]
with open(log_file, "a") as f:
    f.write(json.dumps(args) + "\n")
with open(state_file) as f:
    state = json.load(f)


def parse(argv):
    filters, fmt, quiet, rest, i = [], None, False, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--filter":
            filters.append(argv[i + 1])
            i += 2
        elif a == "--format":
            fmt = argv[i + 1]
            i += 2
        else:
            if a in ("-q", "--quiet"):
                quiet = True
            elif a not in ("-a", "--all", "-f", "--force"):
                rest.append(a)
            i += 1
    return filters, fmt, quiet, rest


def chosen(name, obj, filters):
    labels = obj.get("labels", {})
    for f in filters:
        key, _, val = f.partition("=")
        if key == "name" and not re.search(val, name):
            return False
        if key == "label":
            k, eq, v = val.partition("=")
            if k not in labels or (eq and labels[k] != v):
                return False
        if key == "volume" and val not in obj.get("volumes", []):
            return False
    return True


def show(fmt, name, obj):
    labels = obj.get("labels", {})
    out = re.sub(r'\{\{\s*\.Label\s+"([^"]*)"\s*\}\}', lambda m: labels.get(m.group(1), ""), fmt)
    return re.sub(r"\{\{\s*\.Names?\s*\}\}", lambda m: name, out)


def listing(objs, argv):
    filters, fmt, quiet, _ = parse(argv)
    for name, obj in objs.items():
        if chosen(name, obj, filters):
            print(name if quiet or not fmt else show(fmt, name, obj))


def remove(objs, argv):
    for name in parse(argv)[3]:
        objs.pop(name, None)
    with open(state_file, "w") as f:
        json.dump(state, f)


if args[:1] == ["version"] or args[:2] == ["image", "inspect"]:
    sys.exit(0)
if args[:1] == ["ps"]:
    listing(state["containers"], args[1:])
elif args[:2] == ["volume", "ls"]:
    listing(state["volumes"], args[2:])
elif args[:1] in (["rm"], ["stop"]):
    remove(state["containers"], args[1:])
elif args[:2] == ["volume", "rm"]:
    remove(state["volumes"], args[2:])
else:
    print("fake docker: not served: " + " ".join(args), file=sys.stderr)
    sys.exit(1)
'''


def maker(kind: str, pid, pidns: str, start: str, boot: str) -> dict[str, str]:
    """The labels kernel_schema.maker_labels writes, for a maker of our choosing."""
    labels = {"cseries.gate.throwaway": kind, "cseries.gate.pid": str(pid), "cseries.gate.pidns": pidns,
              "cseries.gate.pidstart": start}
    if boot:
        labels["cseries.gate.boot"] = boot
    return labels


def stat_line(pid, state: str, start: str, comm: str = "gate") -> str:
    """/proc/<pid>/stat: pid, (comm), then fields 3 to 24 with the state third
    and the start time (field 22) twentieth after the name."""
    return f"{pid} ({comm}) {state} 1 {pid} {pid} 0 -1 4194560 0 0 0 0 0 0 0 0 20 0 1 0 {start} 1000 100\n"


class FakeProc:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def show(self, pid, state: str = "S", start: str = "500", comm: str = "gate") -> None:
        (self.root / str(pid)).mkdir(exist_ok=True)
        (self.root / str(pid) / "stat").write_text(stat_line(pid, state, start, comm))


class FakeDocker:
    def __init__(self, root: Path):
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.state, self.log = root / "docker-state.json", root / "docker-calls.jsonl"
        self.load({})

    def load(self, containers: dict, volumes: dict | None = None) -> None:
        """containers: {name: {"labels": {...}, "volumes": [...]}}; volumes: {name: {"labels": {...}}}."""
        self.state.write_text(json.dumps({"containers": containers, "volumes": volumes or {}}))
        self.log.write_text("")

    def env(self, base: dict | None = None) -> dict:
        e = dict(os.environ if base is None else base)
        e["PATH"] = f"{self.bin}{os.pathsep}{e.get('PATH', '')}"
        e["FAKE_DOCKER_STATE"], e["FAKE_DOCKER_LOG"] = str(self.state), str(self.log)
        e["DOCKER_HOST"] = "unix:///nonexistent/reaper-gate-fake-docker-only.sock"
        return e

    def is_the_docker(self, env: dict) -> bool:
        return shutil.which("docker", path=env["PATH"]) == str(self.bin / "docker")

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def removed(self) -> list[str]:
        """Every container and volume a call asked to remove or stop, in order."""
        out = []
        for argv in self.calls():
            if argv[:1] in (["rm"], ["stop"]) or argv[:2] == ["volume", "rm"]:
                out += [a for a in argv[2 if argv[0] == "volume" else 1:] if not a.startswith("-")]
        return out

    def left(self) -> tuple[set[str], set[str]]:
        s = json.loads(self.state.read_text())
        return set(s["containers"]), set(s["volumes"])

    @contextlib.contextmanager
    def in_process(self):
        """os.environ pointed at this docker, for reapers called in this
        process; refuses to yield if `docker` would be anything else."""
        saved = dict(os.environ)
        os.environ.update(self.env())
        try:
            if not self.is_the_docker(os.environ):
                raise SystemExit(f"FAIL: `docker` on PATH is not {self.bin / 'docker'}: no reaper is run")
            yield self
        finally:
            os.environ.clear()
            os.environ.update(saved)


class FakeKill:
    """os.kill for the reaper gates: answers signal 0 from `table` (pid ->
    "alive", "eperm" or "esrch"; any other pid has no process) and sends no
    signal ever. A reaper that tried another signal gets AssertionError, and
    the attempt is kept in `signals`."""

    def __init__(self, table: dict):
        self.table = {int(k): v for k, v in table.items()}
        self.signals: list[tuple[int, int]] = []

    def __call__(self, pid, sig):
        if sig != 0:
            self.signals.append((pid, sig))
            raise AssertionError(f"a reaper gate refuses to send signal {sig} to pid {pid}")
        what = self.table.get(int(pid), "esrch")
        if what == "alive":
            return None
        if what == "eperm":
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))
        raise ProcessLookupError(errno.ESRCH, os.strerror(errno.ESRCH))

    @contextlib.contextmanager
    def installed(self):
        real = os.kill
        os.kill = self
        try:
            yield self
        finally:
            os.kill = real


def bash_kill(table: dict, log: Path) -> str:
    """A bash function `kill` (bash runs a function before its builtin) that
    answers `kill -0 <pid>` from `table` as the builtin words it, logs each
    call to `log`, and refuses anything else."""
    alive = "|".join(str(p) for p, w in table.items() if w == "alive") or "no-pid"
    eperm = "|".join(str(p) for p, w in table.items() if w == "eperm") or "no-pid"
    return f'''kill() {{
    echo "kill $*" >> {shlex.quote(str(log))}
    if [ "$#" -ne 2 ] || [ "$1" != -0 ]; then echo "a reaper gate refuses: kill $*" >&2; return 99; fi
    case "$2" in
        {alive}) return 0 ;;
        {eperm}) echo "bash: kill: ($2) - Operation not permitted" >&2; return 1 ;;
        *) echo "bash: kill: ($2) - No such process" >&2; return 1 ;;
    esac
}}
'''
