#!/usr/bin/env python3
"""Main-container wrapper of a process app's runtime pod: run the manifest
command and ship what it prints — and what it costs — back to the app's kernel.

Origin. An app's bootstrap failed at one of its setup steps
and the ONLY record was a file on the pod's own PVC. The
kernel's `system.app_status` reports `log_tail` for an in-container sidecar
(the supervisor redirects its output to a file) but had nothing for a
pod-backed app — "a pod-backed app's output belongs to its pod" — so the host
could not see why the app was down, and the user pasted the diagnosis into
the chat by hand, three times. The machine panel had the same blind spot:
no pid, no memory for the app.

This wrapper closes both without giving the kernel any reach into the pod:
the pod already talks to its kernel once (fetch.py pulls the source with the
per-app token), so the same token, the same route family and the same
direction carry the output back. Stdlib only, like fetch.py.

What it does, and only that:
  * exec the manifest `command` (everything after `--`) as a child with
    stdout+stderr merged into one pipe, cwd unchanged ($APP_SRC);
  * tee every line to its own stdout (so `kubectl logs` still shows it) AND
    batch it to POST $CSERIES_KERNEL_URL/api/applications/$PRIVATAE_APP_ID/runtime-log
    with X-App-Runtime-Source-Token — at most every FLUSH_S seconds or
    FLUSH_BYTES bytes, whichever first, and a final flush carrying the
    child's exit code; every POST is tagged with this pod's name (HOSTNAME)
    so the kernel can tell one pod generation from the next;
  * with each flush, `stats`: the child's pid and this container's memory
    (cgroup v2 memory.current — the whole app including anything it
    spawned; falls back to the child's RSS from /proc);
  * as PID 1: reap orphaned grandchildren, forward SIGTERM/SIGINT to the
    child, and exit with the child's code (128+signal when it died of one).

It never reads the app's files, never holds any credential but the source
token (which reaches exactly two routes on the kernel: /source and
/runtime-log), and a kernel that is unreachable costs the app nothing: the
POST is best-effort, the child runs regardless.

Env (rendered by the operator, kopf_handlers _reconcile_app_runtime_process):
  CSERIES_KERNEL_URL, PRIVATAE_APP_ID, APP_RUNTIME_SOURCE_TOKEN (optional —
  without it lines are only printed), HOSTNAME (the kubelet's).
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

FLUSH_S = 2.0
FLUSH_BYTES = 32 * 1024
MAX_LINE = 4000
# A runaway printer must not flood the kernel: past this many lines per flush
# window the batch is truncated and says so.
MAX_LINES_PER_FLUSH = 400
# Memory is sampled at most this often (a flush every 2s with a full
# /sys read each time is fine; this is a cap, not a cadence).
STATS_EVERY_S = 5.0
# A quiet app still reports its numbers on this cadence.
STATS_HEARTBEAT_S = 30.0
# After the child exits, how long the reader waits for a grandchild that
# inherited the pipe before the exit is reported anyway.
DRAIN_GRACE_S = 5.0
_CGROUP_MEM = "/sys/fs/cgroup/memory.current"


def _mem_bytes(pid):
    """This container's memory.current (cgroup v2), else the child's RSS."""
    try:
        with open(_CGROUP_MEM) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        pass
    if pid:
        try:
            with open(f"/proc/{pid}/statm") as fh:
                pages = int(fh.read().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            pass
    return None


class Shipper:
    def __init__(self):
        self.kernel = os.environ.get("CSERIES_KERNEL_URL", "").rstrip("/")
        self.app_id = os.environ.get("PRIVATAE_APP_ID", "")
        self.token = os.environ.get("APP_RUNTIME_SOURCE_TOKEN", "")
        self.pod = os.environ.get("HOSTNAME", "")
        self.enabled = bool(self.kernel and self.app_id and self.token)
        self.pid = None
        self._buf = []
        self._bytes = 0
        self._dropped = 0
        self._lock = threading.Lock()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._failed_posts = 0
        self._last_stats_at = 0.0
        self._last_stats = None
        if not self.enabled:
            print("[app-runtime] runtime-log shipping off (CSERIES_KERNEL_URL, "
                  "PRIVATAE_APP_ID or APP_RUNTIME_SOURCE_TOKEN missing) — output "
                  "is printed only", file=sys.stderr, flush=True)

    def add(self, line):
        """Queue one line; True when the batch should flush now."""
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + "…[truncated]"
        with self._lock:
            if len(self._buf) >= MAX_LINES_PER_FLUSH:
                self._dropped += 1
                return True
            self._buf.append(line)
            self._bytes += len(line) + 1
            return self._bytes >= FLUSH_BYTES

    def _stats(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_stats_at < STATS_EVERY_S:
            return self._last_stats
        self._last_stats_at = now
        s = {"pid": self.pid}
        m = _mem_bytes(self.pid)
        if m is not None:
            s["mem_bytes"] = m
        self._last_stats = s
        return s

    def flush(self, exit_code=None, heartbeat=False):
        with self._lock:
            lines, self._buf = self._buf, []
            dropped, self._dropped = self._dropped, 0
            self._bytes = 0
        if dropped:
            lines.append(f"[app-runtime] {dropped} line(s) dropped from this batch "
                         f"(more than {MAX_LINES_PER_FLUSH} in {FLUSH_S:.0f}s)")
        if not self.enabled:
            return
        stats = self._stats(force=exit_code is not None or heartbeat)
        if not lines and exit_code is None and not heartbeat:
            return
        payload = {"lines": lines, "exit_code": exit_code, "pod": self.pod}
        if stats:
            payload["stats"] = stats
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.kernel}/api/applications/{self.app_id}/runtime-log",
            data=body, method="POST",
            headers={"X-App-Runtime-Source-Token": self.token,
                     "Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=10) as resp:
                resp.read()
            self._failed_posts = 0
        except Exception as e:  # noqa: BLE001 — best-effort by design
            self._failed_posts += 1
            # Say it once per streak, not once per batch: a kernel that is
            # down for a minute would otherwise print thirty warnings.
            if self._failed_posts == 1:
                print(f"[app-runtime] runtime-log POST failed: {e}",
                      file=sys.stderr, flush=True)


def _reap_orphans(child_pid):
    """PID 1 duty: collect grandchildren the app orphaned, without stealing
    the child's own exit (waitpid on -1 would). Called from the ticker."""
    while True:
        try:
            pid, _st = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
        if pid == child_pid:
            # Not ours to consume here; Popen.wait() will find it gone —
            # remember the status so main() can still report it.
            _reap_orphans.child_status = _st
            return
_reap_orphans.child_status = None


def _lockfile_from_record(src_dir):
    """The declared lockfile, read from the platform's build record
    (attestation.json — JSON, so stdlib can read it; the manifest is YAML).
    None when the app declares no lockfile: then the app's own command owns
    its dependencies, exactly as before."""
    path = os.path.join(src_dir, "attestation.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as fh:
            att = json.loads(fh.read().decode("utf-8"))
        bld = att.get("build") or {}
        rel = bld.get("lockfile")
        return str(rel) if rel else None
    except (OSError, ValueError):
        return None


def prepare_deps(src_dir, app_id, shipper, *, venv_dir=None, pip_argv_prefix=None):
    """Install the declared lockfile — and nothing else — before the app runs.
    Returns the env additions
    for the child ({} when no lockfile is declared), or an int exit code on
    refusal.

    The venv lives on the app's appdata PVC so it survives sleeps; the
    lockfile's sha256 is written beside it after a successful install, and an
    unchanged lockfile skips pip on the next boot. `pip install
    --require-hashes --no-deps` is pip's own hash-checking mode: every entry
    must carry a hash and every download must match, or pip refuses — so a
    lockfile that pins names, not bytes, cannot install here."""
    lock_rel = _lockfile_from_record(src_dir)
    if not lock_rel:
        return {}
    lock_path = os.path.join(src_dir, lock_rel)
    if not os.path.isfile(lock_path):
        msg = f"[app-runtime] build.lockfile {lock_rel!r} is declared but absent"
        print(msg, file=sys.stderr, flush=True); shipper.add(msg)
        return 78
    import hashlib
    with open(lock_path, "rb") as fh:
        lock_sha = hashlib.sha256(fh.read()).hexdigest()
    venv_dir = venv_dir or os.environ.get("APP_VENV") or \
        f"/var/lib/cseries/appdata/{app_id}/venv"
    marker = os.path.join(venv_dir, ".cseries-lock-sha256")
    pip = os.path.join(venv_dir, "bin", "pip")
    py = os.path.join(venv_dir, "bin", "python")
    # PIP_REQUIRE_HASHES=1: pip's own env switch for hash-checking mode, so a
    # bootstrap that runs `pip install foo` on top of the lockfile is refused
    # by pip itself — the contract's "nothing else is installed" is enforced
    # by the child's environment, not by prose.
    env_add = {"VIRTUAL_ENV": venv_dir,
               "PIP_REQUIRE_HASHES": "1",
               "PATH": os.path.join(venv_dir, "bin") + os.pathsep + os.environ.get("PATH", "")}
    try:
        with open(marker) as fh:
            if fh.read().strip() == lock_sha and os.path.isfile(py):
                shipper.add(f"[app-runtime] deps: {lock_rel} unchanged "
                            f"({lock_sha[:12]}…), venv reused")
                return env_add
    except OSError:
        pass
    steps = []
    if not os.path.isfile(py):
        steps.append([sys.executable, "-m", "venv", venv_dir])
    steps.append(list(pip_argv_prefix or [pip]) + [
        "install", "--require-hashes", "--no-deps", "--disable-pip-version-check",
        "-r", lock_path])
    for argv in steps:
        shipper.add("[app-runtime] deps: " + " ".join(argv))
        try:
            proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, errors="replace")
        except OSError as e:
            msg = f"[app-runtime] deps: cannot run {argv[0]!r}: {e}"
            print(msg, file=sys.stderr, flush=True); shipper.add(msg)
            return 78
        for line in proc.stdout.splitlines()[-60:]:
            sys.stdout.write(line + "\n")
            shipper.add(line)
        sys.stdout.flush()
        if proc.returncode != 0:
            msg = (f"[app-runtime] deps: {os.path.basename(argv[0])} exited "
                   f"{proc.returncode} — the lockfile could not be installed "
                   f"exactly as pinned; refusing to start the app")
            print(msg, file=sys.stderr, flush=True); shipper.add(msg)
            return 78
    try:
        os.makedirs(venv_dir, exist_ok=True)
        with open(marker, "w") as fh:
            fh.write(lock_sha + "\n")
    except OSError as e:
        shipper.add(f"[app-runtime] deps: could not write {marker}: {e}")
    shipper.add(f"[app-runtime] deps: installed {lock_rel} ({lock_sha[:12]}…) into {venv_dir}")
    return env_add


def main(argv):
    if "--" in argv:
        cmd = argv[argv.index("--") + 1:]
    else:
        cmd = argv[1:]
    if not cmd:
        print("run: usage: run.py -- <command...>", file=sys.stderr)
        return 2

    shipper = Shipper()
    # The declared lockfile is installed first — the recipe's pinned half.
    deps = prepare_deps(os.getcwd(), shipper.app_id, shipper)
    if isinstance(deps, int):
        shipper.flush(exit_code=deps)
        return deps
    child_env = {**os.environ, **deps}
    try:
        child = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
            text=True, errors="replace", env=child_env)
    except OSError as e:
        msg = f"[app-runtime] cannot start {cmd[0]!r}: {e}"
        print(msg, file=sys.stderr, flush=True)
        shipper.add(msg)
        shipper.flush(exit_code=127)
        return 127
    shipper.pid = child.pid

    def _forward(signum, _frame):
        try:
            child.send_signal(signum)
        except Exception:
            pass
    signal.signal(signal.SIGTERM, _forward)
    signal.signal(signal.SIGINT, _forward)

    stop = threading.Event()
    child_done = threading.Event()
    reader_done = threading.Event()

    def _ticker():
        last_hb = time.monotonic()
        while not stop.wait(FLUSH_S):
            hb = time.monotonic() - last_hb >= STATS_HEARTBEAT_S
            shipper.flush(heartbeat=hb)
            if hb:
                last_hb = time.monotonic()
            if os.getpid() == 1:
                _reap_orphans(child.pid)
    threading.Thread(target=_ticker, daemon=True).start()

    def _reader():
        # Reads until EOF — which a grandchild holding the pipe can delay
        # past the child's exit; main() stops waiting after DRAIN_GRACE_S.
        try:
            for raw in child.stdout:
                line = raw.rstrip("\n")
                sys.stdout.write(raw)
                sys.stdout.flush()
                if shipper.add(line):
                    shipper.flush()
        finally:
            reader_done.set()
    threading.Thread(target=_reader, daemon=True).start()

    try:
        rc = child.wait()
    except ChildProcessError:
        # The reaper consumed it (PID 1 race); use the status it kept.
        st = _reap_orphans.child_status
        rc = os.waitstatus_to_exitcode(st) if st is not None else 0
    child_done.set()
    reader_done.wait(DRAIN_GRACE_S)
    stop.set()
    if rc < 0:
        shipper.add(f"[app-runtime] command killed by signal {-rc}")
    else:
        shipper.add(f"[app-runtime] command exited with code {rc}")
    shipper.flush(exit_code=rc)
    # A container exit code is 0..255: a signal death is 128+signum, the
    # shell convention, never a negative that the kubelet would show as 241.
    return (128 - rc) if rc < 0 else rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
