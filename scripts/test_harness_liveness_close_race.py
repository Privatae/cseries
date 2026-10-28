#!/usr/bin/env python3
"""A CLI that answers and closes its connection is not stopped before its
answer is read.

The watchdog picks the window from each reading: an open connection gets the
model-wait window, a tree with no connection and no sign of life the idle
window. A CLI closes its provider connection the moment it prints the reply,
and that line counts as activity only once the stdout thread has read it.
Before this check, a reading that landed in that gap (connection gone, line
not yet read, CLI not yet exited) judged the run on the idle window counted
from its last activity, often its start, and stopped a run that had just
answered: "kimi produced no output for 0 minutes and never started".
test_harness_liveness_not_output.py case 2 met it now and then under parallel
load.

The gap is made certain here rather than left to timing: every step waits on
the watchdog's own readings, not on how fast the machine is.
  * the stand-in provider answers only once the watchdog has seen the
    connection open for longer than the idle window, so under the old rule
    the first reading after the close is past the idle window;
  * the CLI's shell stays up until the watchdog has taken two readings with
    the connection closed;
  * the stdout thread is handed the reply only after the CLI has exited (a
    starved reader).

What this pins, by RUNNING the real runner with a stub CLI (no pod):
  1. a CLI that answers and closes its connection COMPLETES, even when its
     reply is read only after the CLI exits (fails under the old rule);
  2. a CLI that closes its connection and then goes silent is still stopped
     as stuck, one idle window after the close, not before it and not long
     after it.

Run: APP_ROOT=<repo> python3 scripts/test_harness_liveness_close_race.py
"""
import dataclasses
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

FAILS = []
# Every wait below is bounded by this, so a broken runner fails the gate
# instead of hanging it.
BOUND_S = 20.0


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[close-race] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _exited(pid: int) -> bool:
    """The process has exited: a zombie, or already reaped."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read()
        return raw[raw.rindex(b")") + 2:].split()[0] == b"Z"
    except (OSError, ValueError, IndexError):
        return True


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "cli"))
    sys.path.insert(0, os.path.join(app_root, "system-apps"))
    from _harness_runtime import harness_core as hc
    from _harness_runtime import specs
    from _harness_runtime import events

    tmp = Path(tempfile.mkdtemp(prefix="close-race-"))

    def _literal(harness_id, line, seq, state=None):
        raw = (line or "").strip()
        return [{"ts": time.time(), "seq": seq, "kind": "text",
                 "payload": {"text": raw}}]
    events.normalize_line = _literal

    KM = specs.ALL_SPECS["kimi"]
    hc._BARE_KEY_BASE_CACHE.clear()
    hc._BARE_KEY_BASE_PROBE = lambda base, key: base == KM.bare_key_base_url
    kp = tmp / "kimi-bare"
    kp.write_text("sk-moonshot-1")

    # Seconds, not minutes: idle window 1.2 s, model-wait window 4.8 s, a
    # reading every 0.1 s.
    hc.IDLE_FLOOR_S = 0.5
    hc.ALIVE_FRAME_S = 0.6
    hc.POLL_S = 0.1
    IDLE_S, WAIT_MIN = 1.2, 0.08
    py = sys.executable

    # A stand-in provider on loopback, in this process: it holds every
    # connection and answers one byte when told to.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    conns: list = []
    conns_lock = threading.Lock()

    def _accept():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with conns_lock:
                conns.append(c)
    threading.Thread(target=_accept, daemon=True).start()

    real_sample = hc.sample_run_tree
    real_popen = hc.subprocess.Popen

    def run(name: str, *, prints: bool, after: str):
        """One run of a stub CLI that connects, waits for the provider's byte,
        prints the reply (or nothing) and closes the connection as it exits;
        then its shell does `after`. Returns (final status, timings)."""
        fifo = tmp / f"{name}.fifo"
        os.mkfifo(fifo)
        reply = "print(\"TEXT\", flush=True)\n" if prints else ""
        stub = tmp / name
        stub.write_text(
            "#!/bin/sh\n"
            f"{py} -c 'import socket\n"
            f"s=socket.create_connection((\"127.0.0.1\",{port}))\n"
            "s.recv(1)\n"
            f"{reply}'\n"
            f"{after.replace('FIFO', str(fifo))}\n")
        stub.chmod(0o755)
        t = {"first_net": None, "answered": None, "gap_readings": 0,
             "released": False, "pid": None}

        def _release():
            # Blocks until the shell opens the FIFO to read; a daemon thread,
            # so a run stopped first leaves nothing waiting on it.
            with open(fifo, "w") as fh:
                fh.write("go\n")

        def sample(*a, **k):
            cur = real_sample(*a, **k)
            now = time.time()
            net = bool(cur is not None and cur.net)
            if t["answered"] is None:
                if net and t["first_net"] is None:
                    t["first_net"] = now
                # Answer once the connection has been seen for longer than
                # the idle window: under the old rule the next reading after the
                # close is then judged on an idle window that has already run out.
                if t["first_net"] is not None and now - t["first_net"] > IDLE_S + 0.4:
                    t["answered"] = now
                    with conns_lock:
                        for c in conns:
                            try:
                                c.sendall(b"g")
                            except OSError:
                                pass
            elif not net:
                # A reading in the gap: connection closed, CLI still alive,
                # reply not yet handed to the stdout thread. The shell is let
                # go at the second one, after the first was judged.
                t["gap_readings"] += 1
                if t["gap_readings"] == 2 and not t["released"]:
                    t["released"] = True
                    threading.Thread(target=_release, daemon=True).start()
            return cur

        class HeldStdout:
            """The CLI's stdout, handed to the runner's reader only once the
            CLI has exited: the starved reader of the race."""

            def __init__(self, f, pid):
                self.f, self.pid = f, pid

            def __iter__(self):
                for ln in self.f:
                    end = time.time() + BOUND_S
                    while not _exited(self.pid) and time.time() < end:
                        time.sleep(0.02)
                    yield ln

            def __getattr__(self, n):
                return getattr(self.f, n)

        class HeldPopen(real_popen):
            def __init__(self, args, *a, **k):
                super().__init__(args, *a, **k)
                argv = args if isinstance(args, (list, tuple)) else [args]
                if self.stdout is not None and str(stub) in map(str, argv):
                    t["pid"] = self.pid
                    self.stdout = HeldStdout(self.stdout, self.pid)

        spec = dataclasses.replace(KM, binary=stub, creds_path=kp)
        body = {"prompt": "build it", "project_dir": str(tmp / f"proj-{name}"),
                "model": "k3", "idle_minutes": IDLE_S / 60, "max_minutes": WAIT_MIN}

        class Req:
            pass
        Req.body = json.dumps(body).encode()
        hc.sample_run_tree, hc.subprocess.Popen = sample, HeldPopen
        try:
            t0 = time.time()
            frames = list(hc.make_run_stream(spec)(Req()))
            t["end"] = time.time()
            t["el"] = t["end"] - t0
        finally:
            hc.sample_run_tree, hc.subprocess.Popen = real_sample, real_popen
            with conns_lock:
                for c in conns:
                    c.close()
                conns.clear()
        statuses = [f for f in frames if f.get("kind") == "status"]
        t["texts"] = [(f.get("payload") or {}).get("text") for f in frames
                      if f.get("kind") == "text"]
        return (statuses[-1]["payload"] if statuses else {}), t

    print("== 1. a CLI that answers and closes its connection, its reply read late ==")
    final, t = run("answered", prints=True, after="read _ < FIFO\nexit 0")
    check("the watchdog saw the connection, then took readings in the gap "
          "(connection closed, reply unread, CLI alive)",
          t["first_net"] is not None and t["answered"] is not None
          and t["gap_readings"] >= 1, repr(t))
    check("a CLI that answered and closed its connection COMPLETES",
          final.get("state") == "complete" and final.get("ok") is True,
          f"state={final.get('state')} err={final.get('error')!r} t={t!r} "
          f"stderr={(final.get('stderr') or '')[-300:]!r}")
    check("...and the reply it printed reached the caller", "TEXT" in t["texts"],
          repr(t["texts"]))

    print("== 2. a CLI that closes its connection and goes silent is still stopped ==")
    final, t = run("silent", prints=False, after="sleep 30")
    after_close = (t["end"] - t["answered"]) if t["answered"] else -1.0
    err = final.get("error") or ""
    check("a tree silent after its connection closed is stopped as stuck",
          final.get("state") == "timeout" and "stopped as stuck" in err,
          f"state={final.get('state')} err={err!r}")
    check("...one idle window (1.2 s) after the close: not before it, not long after",
          IDLE_S - 0.2 <= after_close < IDLE_S + 3.0,
          f"stopped {after_close:.2f}s after the close; t={t!r}")

    srv.close()
    print()
    if FAILS:
        print(f"FAIL ({len(FAILS)}): {FAILS}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
