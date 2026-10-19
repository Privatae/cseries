#!/usr/bin/env python3
"""A harness pod can be told to stop what it is running, and the pod stays up.

_harness_runtime/harness_core.py stop_everything (POST /stop on every harness)
freezes and kills every process the pod runs for the host, and the kernel's
POST /api/review/harness_stop calls it on a pod this kernel holds — never
waking one. What this file pins:

  1. The target set: every descendant of the server (a child that left its
     process group with setsid included), plus orphans under pid 1 ONLY when
     pid 1 is tini and the server is its direct child — the harness image.
     Never pid 1, never the server.
  2. Frozen before killed; a frozen process that cannot be killed is resumed;
     the rounds are bounded.
  3. For real, with signals: children THIS TEST starts — a plain one, a
     grandchild, and one that called setsid — are all stopped, and on this
     machine (pid 1 is not tini) nothing that is not the test's own
     descendant can be a target.
  4. The kernel route never wakes, sends the pod's capability, and reads an
     image without /stop as such.

Container-free. Run: APP_ROOT=<repo> python3 scripts/test_harness_stop.py
"""
import asyncio
import os
import signal
import subprocess
import json
import sys
import time
from pathlib import Path

_ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(_ROOT, "cli"), os.path.join(_ROOT, "system-apps"),
                _ROOT, os.path.join(_ROOT, "brain")]

from _harness_runtime import harness_core as hc  # noqa: E402

FAILS: list[str] = []
PASSES = 0


def check(name: str, ok: bool, detail="") -> None:
    global PASSES
    if ok:
        PASSES += 1
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}" + (f"\n        {detail}" if detail != "" else ""))


def test_targets():
    self_pid = 500
    ppids = {1: 0, 500: 1,            # tini -> server
             501: 500, 502: 501,      # a run and its child
             503: 500,                # a child that called setsid: parent link intact
             600: 1, 601: 600,        # an orphaned daemon and its child
             700: 42}                 # something unrelated
    # MUTATION: include orphans unconditionally.
    # RESULT: red — 600/601 appear outside the container.
    check("outside the container: descendants only",
          hc._stop_targets(ppids, self_pid, orphans=False) == {501, 502, 503},
          hc._stop_targets(ppids, self_pid, orphans=False))
    check("inside the container: descendants AND orphans under pid 1, never pid 1 or the server",
          hc._stop_targets(ppids, self_pid, orphans=True) == {501, 502, 503, 600, 601},
          hc._stop_targets(ppids, self_pid, orphans=True))
    # A corrupt map naming pid 1 or the server as a child never makes them targets.
    bad = {**ppids, 1: 500}
    check("pid 1 and the server are never targets, whatever the map says",
          not ({1, 500} & hc._stop_targets(bad, self_pid, orphans=True)))


def test_freeze_then_kill():
    log: list = []
    walks = [{1: 0, os.getpid(): 1, 901: os.getpid()},
             {1: 0, os.getpid(): 1, 901: os.getpid(), 902: 901}]
    calls = {"n": 0}

    def ppids():
        i = min(calls["n"], len(walks) - 1)
        calls["n"] += 1
        return walks[i]

    def sig(pid, s):
        log.append((pid, s))
        if s == signal.SIGKILL and pid == 902:
            raise PermissionError
    res = hc.stop_everything(signal_fn=sig, ppids_fn=ppids, in_container_fn=lambda: False)
    kills = [i for i, (_, s) in enumerate(log) if s == signal.SIGKILL]
    stops = [i for i, (_, s) in enumerate(log) if s == signal.SIGSTOP]
    # MUTATION: SIGKILL inside the walk.
    # RESULT: red — a kill precedes the last stop.
    check("every process is frozen before any is killed", stops and kills and max(stops) < min(kills), log)
    # MUTATION: walk once.
    # RESULT: red — 902 (spawned between walks) is never frozen.
    check("a child spawned during the stop is caught on the next walk",
          (902, signal.SIGSTOP) in log, log)
    # MUTATION: drop the SIGCONT on kill failure.
    # RESULT: red.
    check("a frozen process that cannot be killed is resumed, and reported",
          (902, signal.SIGCONT) in log and res["resumed"] == [902] and res["stopped"] == [901], (log, res))

    grow = {"n": 0}

    def endless():
        grow["n"] += 1
        m = {1: 0, os.getpid(): 1}
        for i in range(grow["n"]):
            m[3000 + i] = os.getpid()
        return m
    hc.stop_everything(signal_fn=lambda p, s: None, ppids_fn=endless, in_container_fn=lambda: False)
    check("the rounds are bounded against a tree that never stops growing",
          grow["n"] <= hc._STOP_MAX_ROUNDS, grow["n"])


def _in_private_pid_namespace() -> bool:
    """True only when this process sees no more than its own namespace: the
    re-exec below makes a shell pid 1 with this test as its child."""
    try:
        return Path("/proc/1/comm").read_text().strip() == "sh" and os.getppid() == 1 \
            and len([e for e in os.listdir("/proc") if e.isdigit()]) < 50
    except OSError:
        return False


def test_second_guard():
    """The guard that holds when the first one is gone. The first guard is
    defeated TWO ways — the target set forced to include pid 1's other children
    (the mutant that took a development machine down), and the container check itself answering
    wrongly true — and still only the server's own descendants are signalled,
    because the second guard reads the container from its OWN sources
    (_container_recheck), here honestly false. Recorded, never real, signals."""
    me = os.getpid()
    ppids = {1: 0, me: 77, 77: 1, 901: me, 50: 1, 51: 50}
    real_targets = hc._stop_targets
    hc._stop_targets = lambda pp, sp, orphans: real_targets(pp, sp, True)
    log: list = []
    try:
        hc.stop_everything(signal_fn=lambda p, s: log.append((p, s)), ppids_fn=lambda: ppids,
                           in_container_fn=lambda: True, recheck_fn=lambda: False)
    finally:
        hc._stop_targets = real_targets
    hit = {p for p, _ in log}
    # MUTATION: remove the second guard.  RESULT: red — 50, 51, 77 signalled.
    # MUTATION: the second guard calls in_container_fn instead of recheck_fn.
    # RESULT: red — the wrong reading opens both guards.
    check("with the first guard defeated, nothing outside the server's own tree is signalled",
          hit == {901}, sorted(hit))
    check("the recheck is its own reading: false here (pid 1 is not tini)",
          hc._container_recheck() is False)


def test_honest_counts():
    """A process already gone at the kill is STOPPED, never 'could not stop';
    one that could not be frozen at all is reported, never left out."""
    me = os.getpid()
    ppids = {1: 0, me: 1, 901: me, 902: me, 903: me}

    def sig(pid, s):
        if pid == 901 and s == signal.SIGKILL:
            raise ProcessLookupError   # the run's own watchdog got there first
        if pid == 902 and s == signal.SIGSTOP:
            raise PermissionError      # not ours to signal
    res = hc.stop_everything(signal_fn=sig, ppids_fn=lambda: ppids, in_container_fn=lambda: False)
    # MUTATION: treat ProcessLookupError on SIGKILL as a failure.  RESULT: red.
    check("a process gone before the kill counts as stopped", 901 in res["stopped"]
          and 901 not in res["resumed"], res)
    # MUTATION: skip a SIGSTOP failure silently.  RESULT: red.
    check("a process that could not be frozen is reported as not stopped",
          res.get("not_stopped") == [902], res)
    check("the rest is stopped", 903 in res["stopped"], res)


def _children_from_proc(parent: int) -> list[int]:
    """Direct children of `parent`, read from /proc by THIS TEST — the fence is
    built from it, and a fence built with the code under test falls with it."""
    out = []
    for e in os.listdir("/proc"):
        if not e.isdigit():
            continue
        try:
            raw = Path(f"/proc/{e}/stat").read_text()
            if int(raw[raw.rindex(")") + 2:].split()[1]) == parent:
                out.append(int(e))
        except (OSError, ValueError, IndexError):
            continue
    return out


def test_real_children():
    """REAL SIGNALS — and this whole file only ever runs inside a private PID
    namespace (see main), where only the test's own processes exist. The signal
    function it hands in also refuses, and records, any pid outside the set it
    read from /proc itself before calling stop_everything."""
    check("the container check is false here (pid 1 is sh, not tini)",
          hc._in_harness_container() is False)
    plain = subprocess.Popen(["sleep", "300"])
    parent = subprocess.Popen(["sh", "-c", "sleep 300 & wait"])
    escaped = subprocess.Popen(["setsid", "sleep", "300"])
    kids = [plain, parent, escaped]
    try:
        deadline = time.time() + 5
        grand: list[int] = []
        while time.time() < deadline:
            grand = _children_from_proc(parent.pid)
            if grand:
                break
            time.sleep(0.02)
        allowed = {plain.pid, parent.pid, escaped.pid, *grand}
        refused: list[int] = []

        def fenced(pid, sig):
            if pid not in allowed:
                refused.append(pid)
                raise PermissionError(f"test fence: {pid} is not the test's own")
            os.kill(pid, sig)

        res = hc.stop_everything(signal_fn=fenced)
        stopped = set(res["stopped"])
        check("the test's own children are stopped, a grandchild and a setsid'd child included",
              allowed <= stopped, (res, allowed))
        check("stop_everything asked to signal nothing outside the test's own tree",
              not refused, refused)
        for k in kids:
            k.wait(timeout=5)
        check("they are gone", all(k.poll() is not None for k in kids))
    finally:
        for k in kids:
            if k.poll() is None:
                k.kill()
                k.wait()


def test_kernel_route():
    try:
        from server.routes import review
    except Exception as e:  # pragma: no cover - import environment
        check("the kernel route imports", False, repr(e))
        return
    harness = sorted(review.HARNESS_APP_IDS)[0]
    saved_base, saved_cap = dict(review._cap_base), dict(review._control_cap)
    review._cap_base.clear()
    review._control_cap.clear()
    try:
        wakes: list = []
        real_bridge = review._harness_bridge

        async def fake_bridge(op, h, **kw):
            wakes.append((op, h))
            return {"ok": False, "error": "test bridge"}
        review._harness_bridge = fake_bridge
        try:
            out = asyncio.run(review.harness_stop(review.HarnessStopReq(harness=harness)))
        finally:
            review._harness_bridge = real_bridge
        # MUTATION: wake the pod when none is held.
        # RESULT: red — the fake bridge records a wake.
        check("no held pod: answered without waking, and says the address is what is missing",
              out["ok"] is False and out["held"] is False and wakes == []
              and "does not hold the address" in out["error"], (out, wakes))

        seen = {}

        class _Content:
            """response.content, as harness_input's capped reader uses it."""

            def __init__(self, raw):
                self._b = raw

            async def iter_chunked(self, n):
                for i in range(0, len(self._b), n):
                    yield self._b[i:i + n]

            async def read(self, n=-1):
                return self._b if n is None or n < 0 else self._b[:n]

        class Resp:
            def __init__(self, status, body):
                self.status, self._body = status, body
                raw = json.dumps(body).encode()
                self.content, self.content_length = _Content(raw), len(raw)

            async def json(self, content_type=None):
                return self._body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        def session(status, body):
            class S:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                def post(self, url, json=None, headers=None, timeout=None):
                    seen.update(url=url, headers=headers or {})
                    return Resp(status, body)
            return S

        base = "http://harness-x.host-ns.svc.cluster.local:8843"
        review._cap_base[harness] = base
        review._control_cap[base] = "cap-123"
        real = review.aiohttp.ClientSession
        try:
            review.aiohttp.ClientSession = session(200, {"ok": True, "stopped": [11, 12], "resumed": []})
            out = asyncio.run(review.harness_stop(review.HarnessStopReq(harness=harness)))
            check("a held pod is stopped at its own address with its capability",
                  seen.get("url") == base + "/stop"
                  and seen["headers"].get("X-CSeries-Harness-Cap") == "cap-123", seen)
            check("the answer counts what the pod reported",
                  out == {"harness": harness, "ok": True, "held": True, "stopped": 2, "could_not_stop": 0}, out)
            review.aiohttp.ClientSession = session(404, {"error": "not found"})
            out = asyncio.run(review.harness_stop(review.HarnessStopReq(harness=harness)))
            check("an image without /stop is said to be one, with release named as the stop",
                  out["ok"] is False and "no /stop" in out["error"] and "releas" in out["error"], out)
        finally:
            review.aiohttp.ClientSession = real
    finally:
        review._cap_base.clear()
        review._cap_base.update(saved_base)
        review._control_cap.clear()
        review._control_cap.update(saved_cap)


def main() -> int:
    # THE WHOLE FILE RUNS INSIDE A PRIVATE PID NAMESPACE. Some
    # checks hand fake pids to the code under test; there, a mutant that
    # bypassed the signal seam would hit whatever HOST process holds that
    # number. Inside the namespace no host process exists to hit. Where a
    # namespace cannot be made the gate FAILS loudly (exit 2) — it never runs
    # on the host, and it never passes without having run.
    if not _in_private_pid_namespace():
        cmd = ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
               "sh", "-c", f'"{sys.executable}" "{os.path.abspath(__file__)}"; echo "rc=$?"']
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                               env={**os.environ, "APP_ROOT": _ROOT})
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"SKIP-LOUD: no private PID namespace here ({e!r}) — this gate proves nothing "
                  "without one and will not run on the host.", file=sys.stderr)
            return 2
        # The exit code is read from STDOUT alone: the inner run also logs to
        # stderr, and a combined stream does not end with the marker.
        import re as _re
        m = _re.search(r"rc=(\d+)\s*\Z", r.stdout)
        if not m:
            print("SKIP-LOUD: no private PID namespace here — this gate proves nothing without "
                  "one and will not run on the host.\n" + (r.stdout + r.stderr)[-400:], file=sys.stderr)
            return 2
        print(r.stdout[:m.start()].rstrip())
        if r.stderr.strip():
            print(r.stderr.rstrip(), file=sys.stderr)
        return 0 if m.group(1) == "0" else 1
    print("harness stop — the pod stops what it runs for us, and nothing else "
          "(inside a private PID namespace)")
    for t in (test_targets, test_freeze_then_kill, test_second_guard, test_honest_counts,
              test_real_children, test_kernel_route):
        t()
    print(f"\n{PASSES}/{PASSES + len(FAILS)} passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
