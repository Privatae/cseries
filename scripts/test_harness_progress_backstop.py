#!/usr/bin/env python3
"""A run that keeps its processes busy but makes no progress is stopped; a run
that makes progress never is.

Activity (CPU, bytes moved) keeps a run from being judged wedged. On its own
it would let a spinning test, a hot-looping server or a retry spin bill until
someone pressed Stop. The progress backstop is separate: output from the CLI
(every model and tool event rides it) or a change in the worktree. CPU never
counts as progress.

Pinned by RUNNING the real runner with stub CLIs and real child processes:
  1. inside a tool step, a spinning step is stopped when the tool-step window
     ends, and the message says what was measured;
  2. a legitimately long tool step (busy, printing or writing files) is not
     stopped before that window, though it outlasts the progress window;
  3. outside a tool step only the CLI's own events are progress: retry
     lines, a stderr spinner or worktree writes do not keep it alive;
  4. a worktree write never resets the activity clock, a
     requested model wait is never cut by the progress stop, and 0
     turns a window off;
  5. a tool that setsid()s and is re-parented to pid 1 still counts as
     the run's (its CPU keeps the run alive) and is stopped with the run;
  6. a reader that goes away while the CLI floods stdout leaks no
     stdout-pump thread;
  7. the claude-code harness has the same backstop, keyed on SDK messages;
  8. the activity floors sit above what each vendor CLI does while merely
     WAITING on a model (measured from the pinned harness images), so a
     wedged wait is never read as alive, and a busy suite still is.

Run: APP_ROOT=<repo> python3 scripts/test_harness_progress_backstop.py
"""
import asyncio
import dataclasses
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[progress] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class Req:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "cli"))
    sys.path.insert(0, os.path.join(app_root, "system-apps"))
    from _harness_runtime import harness_core as hc
    from _harness_runtime import specs
    from _harness_runtime import events

    tmp = Path(tempfile.mkdtemp(prefix="progress-"))
    py = sys.executable

    def _literal(harness_id, line, seq, state=None):
        raw = (line or "").strip()
        kinds = {"TOOL_CALL": "tool_call", "TOOL_RESULT": "tool_result", "TEXT": "text"}
        if raw in kinds:
            return [{"ts": time.time(), "seq": seq, "kind": kinds[raw], "payload": {"text": raw}}]
        return [{"ts": time.time(), "seq": seq, "kind": "text",
                 "payload": {"text": raw, "raw": True}}]
    events.normalize_line = _literal

    KM = specs.ALL_SPECS["kimi"]
    hc._BARE_KEY_BASE_CACHE.clear()
    hc._BARE_KEY_BASE_PROBE = lambda base, key: base == KM.bare_key_base_url
    kp = tmp / "kimi-bare"
    kp.write_text("sk-moonshot-1")

    hc.IDLE_FLOOR_S = 0.5
    hc.ALIVE_FRAME_S = 0.6
    hc.PROGRESS_SCAN_S = 0.5
    hc.PROGRESS_MINUTES = 0.05          # 3 s — a branch without it ignores this

    def run(name: str, script: str, **extra):
        stub = tmp / name
        stub.write_text("#!/bin/sh\n" + script)
        stub.chmod(0o755)
        spec = dataclasses.replace(KM, binary=stub, creds_path=kp)
        proj = tmp / f"proj-{name}"
        proj.mkdir(exist_ok=True)
        body = {"prompt": "build it", "project_dir": str(proj), "model": "k3",
                "idle_minutes": 0.02, "max_minutes": 0.2}
        body.update(extra)
        t0 = time.time()
        frames = list(hc.make_run_stream(spec)(Req(body)))
        st = [f for f in frames if f.get("kind") == "status"]
        return (st[-1]["payload"] if st else {}), time.time() - t0

    spin = f"{py} -c 'import time\nt=time.time()\nwhile time.time()-t<{{s}}: pass'"

    hc.TOOL_STEP_MINUTES = 0.05         # 3 s stand-in for HARNESS_TOOL_STEP_MINUTES

    def spin_act(secs: float, act: str) -> str:
        # Busy for `secs`, doing `act` once a second.
        return (f"{py} -c 'import time,sys,os\nt=time.time()\n"
                f"while time.time()-t<{secs}:\n  s=time.time()\n"
                f"  while time.time()-s<1.0: pass\n  {act}'")

    print("== 1. inside a tool step: a spinning step is stopped when its window ends ==")
    final, el = run("spin", "echo TEXT\necho TOOL_CALL\n" + spin.format(s=30) + "\n")
    err = final.get("error") or ""
    check("a CPU spin under a tool call is stopped at the tool-step window (~3 s)",
          final.get("state") == "timeout" and 2.5 <= el < 12, f"el={el:.1f} {err!r}")
    check("...and the message says the tool step had not returned and that it was busy",
          "tool step" in err and "had not returned" in err and "using CPU" in err, err)

    print("== 2. a legitimately long tool step is not stopped before its window ==")
    hc.TOOL_STEP_MINUTES = 0.15         # 9 s; the progress window stays 3 s
    final, el = run("long-step", "echo TEXT\necho TOOL_CALL\n" + spin.format(s=6)
                    + "\necho TOOL_RESULT\necho TEXT\nexit 0\n")
    check("a CPU-busy tool step of 6 s (twice the 3 s progress window) completes",
          final.get("state") == "complete" and final.get("ok") is True,
          f"{final.get('state')} {final.get('error')!r} el={el:.1f}")
    final, el = run("busy-files", "echo TEXT\necho TOOL_CALL\n"
                    + spin_act(6, 'open("out%d.txt" % int(time.time()), "w").write("x")')
                    + "\necho TOOL_RESULT\nexit 0\n")
    check("...and so does one writing files as it works",
          final.get("state") == "complete" and final.get("ok") is True,
          f"{final.get('state')} {final.get('error')!r} el={el:.1f}")
    hc.TOOL_STEP_MINUTES = 0.05

    print("== 3. outside a tool step: only the model's own events are progress ==")
    final, el = run("retry-lines", "echo TEXT\n" + spin_act(30, 'print("Reconnecting...", flush=True)') + "\n",
                      max_minutes=0.02)
    err = final.get("error") or ""
    check("a CLI printing retry lines (not events) while spinning is stopped by progress",
          final.get("state") == "timeout" and el < 12 and "no model or tool event" in err,
          f"el={el:.1f} {err!r}")
    final, el = run("stderr-spinner", "echo TEXT\n"
                    + spin_act(30, 'sys.stderr.write(chr(13)+"|"); sys.stderr.flush()') + "\n",
                    max_minutes=0.02)
    err = final.get("error") or ""
    check("a spinner on stderr is not progress either",
          final.get("state") == "timeout" and el < 12 and "no model or tool event" in err,
          f"el={el:.1f} {err!r}")
    final, el = run("wt-writer", "echo TEXT\n"
                    + spin_act(30, 'open("test.log","a").write("loop"+chr(10))') + "\n",
                    max_minutes=0.02)
    err = final.get("error") or ""
    check("a CPU spinner appending to a worktree file is stopped too",
          final.get("state") == "timeout" and el < 12 and "no model or tool event" in err,
          f"el={el:.1f} {err!r}")
    check("...and the worktree writes are named as evidence, not counted",
          "not counted as progress" in err, err)

    print("== 4. a worktree write never resets the activity clock ==")
    final, el = run("quiet-writer", f"""echo TEXT
echo TOOL_CALL
{py} -c 'import time
while True:
  f=open("app.log","a"); f.write("l"+chr(10)); f.close(); time.sleep(1)' </dev/null >/dev/null 2>&1 &
sleep 30
echo TOOL_RESULT
exit 0
""")
    err = final.get("error") or ""
    check("a wedged CLI whose background job appends to a log is stopped at the idle window",
          final.get("state") == "timeout" and el < 6 and "stopped as stuck" in err,
          f"el={el:.1f} {err!r}")
    log_f = tmp / "proj-quiet-writer" / "app.log"
    n1 = sum(1 for _ in open(log_f)) if log_f.exists() else 0
    time.sleep(2.5)
    n2 = sum(1 for _ in open(log_f)) if log_f.exists() else 0
    check("...and its background writer is stopped with it", n2 == n1, f"{n1} -> {n2}")

    print("== 4b. a requested model wait is never cut by the progress stop ==")
    final, el = run("model-wait-ok", "echo TEXT\nsleep 5\necho TEXT\nexit 0\n",
                    max_minutes=0.15)            # 9 s, above the 3 s progress window
    check("a 5 s silent model wait with max_minutes 9 s completes",
          final.get("state") == "complete" and final.get("ok") is True,
          f"{final.get('state')} {final.get('error')!r} el={el:.1f}")
    final, el = run("model-wait-hung", "echo TEXT\nsleep 30\n", max_minutes=0.15)
    err = final.get("error") or ""
    check("one that never answers is stopped at the requested 9 s, labelled a model wait",
          final.get("state") == "timeout" and 8 <= el < 16 and "waited on the model" in err,
          f"el={el:.1f} {err!r}")

    print("== 4c. 0 turns each window off ==")
    saved = hc.TOOL_STEP_MINUTES
    hc.TOOL_STEP_MINUTES = 0
    try:
        final, el = run("spin-off", "echo TEXT\necho TOOL_CALL\n" + spin.format(s=5)
                        + "\necho TOOL_RESULT\nexit 0\n")
    finally:
        hc.TOOL_STEP_MINUTES = saved
    check("with the tool-step window off, a 5 s spinning step completes",
          final.get("state") == "complete" and final.get("ok") is True,
          f"{final.get('state')} {final.get('error')!r} el={el:.1f}")

    print("== 5. a setsid'd, re-parented tool is still the run's ==")
    pidf = tmp / "orphan.pid"
    final, el = run("orphan", f"""
( setsid {py} -c 'import os,time
open("{pidf}","w").write(str(os.getpid()))
t=time.time()
while time.time()-t<60: pass' </dev/null >/dev/null 2>&1 & )
echo TEXT
echo TOOL_CALL
sleep 2.5
echo TOOL_RESULT
exit 0
""")
    check("its CPU keeps the run alive past the idle window (the CLI itself sleeps)",
          final.get("state") == "complete" and final.get("ok") is True,
          f"{final.get('state')} {final.get('error')!r} el={el:.1f}")
    alive = True
    try:
        opid = int(pidf.read_text())
        for _ in range(20):
            try:
                os.kill(opid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.1)
    except (OSError, ValueError):
        opid = None
    check("...and it is stopped when the run ends, not left spinning", not alive,
          f"pid {opid} still running")
    if alive and opid:
        try:
            os.kill(opid, 9)
        except OSError:
            pass

    print("== 6. a reader that goes away leaks no stdout pump ==")

    def _pumps():
        return sum(1 for t in threading.enumerate() if t.name == "harness-stdout")
    before = _pumps()
    stub = tmp / "flood"
    stub.write_text("#!/bin/sh\necho TEXT\nyes TEXT | head -n 200000\nsleep 30\n")
    stub.chmod(0o755)
    spec = dataclasses.replace(KM, binary=stub, creds_path=kp)
    (tmp / "proj-flood").mkdir(exist_ok=True)
    g = hc.make_run_stream(spec)(Req({"prompt": "x", "project_dir": str(tmp / "proj-flood"),
                                     "model": "k3", "idle_minutes": 0.02}))
    next(g)
    time.sleep(1.0)          # let the queue fill while nobody reads
    g.close()                # the client went away
    t_end = time.time() + 5
    while _pumps() > before and time.time() < t_end:
        time.sleep(0.1)
    check("closing the stream mid-flood leaves no stdout-pump thread behind",
          _pumps() == before, f"{_pumps() - before} pump thread(s) left")

    print("== 7. the claude-code harness has the same backstop ==")
    path = os.path.join(app_root, "system-apps/claude-code-harness/bin/main.py")
    sp = importlib.util.spec_from_file_location("cch_progress", path)
    CCH = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(CCH)
    CCH._SDK_AVAILABLE = True
    CCH._get_token = lambda: "tok"
    CCH._ensure_claude_config = lambda: None
    CCH._SdkOptions = lambda **kw: kw
    CCH.worktree_diff = lambda d: ""
    CCH._current_creds = lambda: None
    CCH.IDLE_FLOOR_S = 0.1
    CCH._ALIVE_FRAME_S = 0.3
    CCH._stop_run_tree = lambda marker, **k: []
    CCH.PROGRESS_MINUTES = 0.03         # 1.8 s
    CCH.PROGRESS_SCAN_S = 0.5
    ticks = [0]

    class _S:
        def __init__(self, cpu):
            self.cpu_ticks, self.io_bytes, self.net = cpu, None, False

    def _busy(pids, *a, **k):
        ticks[0] += 1000
        return _S(ticks[0])
    CCH.sample_pids = _busy

    def _blk(cls, **kw):
        o = type(cls, (), {})()
        for k, v in kw.items():
            setattr(o, k, v)
        return o

    def _script(steps):
        async def _q(prompt, options):
            for st in steps:
                if isinstance(st, (int, float)):
                    await asyncio.sleep(st)
                else:
                    yield st
        return _q
    tool = _blk("AssistantMessage", content=[_blk("ToolUseBlock", id="t1", name="Bash",
                                                  input={"command": "spin"})])
    done = _blk("ResultMessage", is_error=False, model="m", usage=None, total_cost_usd=0)
    body = {"prompt": "x", "idle_minutes": 0.005, "max_minutes": 0.01}
    CCH.TOOL_STEP_MINUTES = 0.03        # 1.8 s stand-in
    CCH._sdk_query = _script([tool, 30])
    t0 = time.time()
    fr = list(CCH.run_stream(Req({**body, "project_dir": tempfile.mkdtemp()})))
    p = fr[-1]["payload"]
    check("claude-code: a busy tool step that does not return is stopped at the "
          "tool-step window", p.get("state") == "timeout"
          and "tool step" in (p.get("error") or "") and time.time() - t0 < 10,
          f"{p.get('state')} {p.get('error')!r} {time.time() - t0:.1f}s")
    talk = [_blk("AssistantMessage", content=[_blk("TextBlock", text="step")]), 0.4] * 10
    CCH._sdk_query = _script([*talk, done])
    fr = list(CCH.run_stream(Req({**body, "project_dir": tempfile.mkdtemp()})))
    p = fr[-1]["payload"]
    check("claude-code: a run whose model keeps producing messages is never stopped by it",
          p.get("state") == "complete" and p.get("ok") is True,
          f"{p.get('state')} {p.get('error')!r}")
    # For claude-code: the SDK wedged with a tool open, the tree idle, and
    # a writer appending to the worktree — the idle stop, not kept alive.

    class _Idle:
        cpu_ticks, io_bytes, net = 0, 0, False
    CCH.sample_pids = lambda pids, *a, **k: _Idle()
    CCH.IDLE_FLOOR_S = 2.0
    CCH.TOOL_STEP_MINUTES = 0.5
    proj = tempfile.mkdtemp()
    stop_w = threading.Event()

    def _writer():
        while not stop_w.wait(0.5):
            with open(os.path.join(proj, "app.log"), "a") as f:
                f.write("l\n")
    threading.Thread(target=_writer, daemon=True).start()
    CCH._sdk_query = _script([tool, 15, done])
    t0 = time.time()
    fr = list(CCH.run_stream(Req({"prompt": "x", "project_dir": proj,
                                  "idle_minutes": 0.03, "max_minutes": 0.05})))
    stop_w.set()
    p = fr[-1]["payload"]
    check("claude-code: a wedged SDK with a worktree writer is stopped at the idle window",
          p.get("state") == "timeout" and time.time() - t0 < 8
          and "stopped as stuck" in (p.get("error") or ""),
          f"{p.get('state')} {p.get('error')!r} {time.time() - t0:.1f}s")

    print("== 8. the activity floors sit above what a WAITING CLI does ==")
    # Measured: each vendor CLI from its pinned
    # harness image, its model unreachable (the egress proxy accepting CONNECT
    # and never answering), sampled every 5 s after a 30 s settle. Each pair
    # is (CPU ticks, rchar+wchar bytes) used in one 5 s window.
    waiting = {
    "opencode": [[2, 261], [5, 351], [3, 351], [5, 359], [2, 261], [5, 498], [4, 351], [5, 408], [4, 261], [6, 547], [5, 351], [6, 506], [4, 359], [6, 449], [3, 253], [5, 457], [4, 261], [6, 310], [4, 253], [4, 359], [5, 359], [4, 351], [4, 351], [5, 367], [3, 261], [6, 441], [4, 253], [5, 359], [3, 261], [5, 351], [3, 253], [4, 359], [3, 261], [5, 351], [3, 253], [5, 408], [4, 261], [4, 351], [6, 351]],
    "kimi": [[3, 1104], [5, 2021], [3, 1024], [3, 1104], [2, 992], [3, 1056], [3, 960], [3, 960], [4, 1168], [2, 1104], [4, 1008], [2, 1136], [3, 976], [19, 88912], [2, 1064], [4, 1088], [3, 1024], [4, 984], [3, 1024], [3, 1112], [4, 960], [2, 1136], [4, 1152], [4, 1040], [3, 1088], [3, 984], [4, 1008], [3, 1032], [3, 928], [3, 992], [3, 1064], [4, 1024], [3, 1064], [3, 1080], [24, 12888], [4, 1080], [3, 1024], [3, 928], [5, 944]],
    "kimi-acp-idle": [[0, 0], [0, 16], [0, 0], [0, 16], [0, 16], [0, 0], [0, 16], [0, 0], [0, 16], [0, 16], [0, 0], [0, 16], [0, 16], [0, 0], [0, 16], [0, 0], [22, 14272], [0, 16], [0, 0], [0, 16], [0, 0], [0, 16], [0, 16], [0, 0], [0, 16], [0, 0], [0, 16], [0, 16], [0, 0], [0, 16], [0, 16], [0, 0], [0, 16], [0, 0], [0, 16], [0, 16], [0, 0], [19, 59008], [0, 0]],
    "cursor": [[1, 16], [0, 16], [0, 0], [0, 16], [0, 16], [0, 0], [0, 16], [0, 0], [1, 16], [0, 16], [0, 0], [0, 16], [0, 0], [0, 16], [0, 16], [0, 0], [10, 7312], [0, 0], [0, 16], [1, 16], [0, 0], [1, 16], [1, 16], [0, 0], [0, 16], [0, 0], [0, 16], [0, 16], [1, 0], [0, 16], [0, 16], [0, 0], [0, 16], [0, 0], [0, 16], [1, 16], [0, 0], [7, 50832], [0, 0]],
    "antigravity": [[0, 0], [0, 11], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [21, 17584008], [0, 498], [0, 311], [2, 812], [0, 359], [0, 295], [1, 65895], [0, 295], [0, 407], [1, 343], [1, 359], [0, 343], [2, 59846], [0, 456], [0, 450], [0, 343], [1, 295], [0, 327], [0, 41287], [1, 279], [1, 391], [0, 295], [0, 263], [0, 311], [4, 59954], [0, 215], [0, 440], [1, 418], [0, 311], [0, 311], [1, 41191], [1, 247]],
    "claude-code": [[1, 420], [4, 945], [2, 1010], [2, 420], [2, 420], [3, 420], [2, 420], [2, 420], [3, 420], [1, 344], [2, 230], [1, 230], [1, 230], [2, 230], [1, 230], [2, 268], [1, 230], [3, 230], [1, 230], [1, 230], [1, 230], [3, 268], [1, 230], [1, 230], [2, 230], [2, 230], [2, 230], [1, 268], [2, 230], [2, 230], [1, 230], [2, 230], [3, 2388], [3, 344], [3, 534], [3, 420], [2, 420], [3, 420], [3, 1012]],
    "codex": [[0, 0], [1, 897587], [0, 0], [0, 16564], [1, 902478], [0, 4128], [1, 0], [1, 902534], [1, 0], [0, 336], [2, 903094], [0, 320], [1, 312], [0, 749], [0, 902713], [0, 8], [0, 344], [6, 228901], [0, 0], [1, 328], [0, 0], [1, 24], [0, 344], [0, 16], [0, 0], [0, 384], [1, 0], [0, 16], [0, 352], [1, 16], [0, 0], [1, 392], [0, 0], [1, 16], [0, 360], [0, 16], [0, 0], [1, 352], [0, 0]],
    "grok-xaikey": [[0, 0], [0, 288], [0, 8], [0, 24], [0, 0], [0, 0], [0, 8], [0, 243], [0, 0], [0, 0], [0, 8], [0, 897804], [0, 219], [0, 0], [10, 5748775], [0, 312], [1, 901646], [1, 320], [0, 2754], [0, 296], [0, 296], [0, 2526], [0, 312], [1, 312], [0, 532], [1, 2542], [0, 328], [0, 352], [0, 320], [0, 2534], [0, 540], [0, 312], [1, 288], [1, 384], [0, 328], [0, 304], [0, 532], [0, 2542], [0, 296]],
    "codex-stdin-idle": [[0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [1, 8], [0, 0], [0, 8], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0]],
}
    for cli, wins in waiting.items():
        gate = hc.ActivityGate() if hasattr(hc, "ActivityGate") else None
        cpu = io = 0
        active = 0
        prev = hc.TreeSample(cpu_ticks=0, io_bytes=0, net=True)
        for c, b in wins:
            cpu, io = cpu + c, io + b
            cur = hc.TreeSample(cpu_ticks=cpu, io_bytes=io, net=True)
            hit = (gate.feed(prev, cur, 5.0) if gate is not None
                   else bool(hc.tree_was_active(prev, cur, 5.0)))
            active += bool(hit)
            prev = cur
        check(f"{cli} waiting on its model is never read as active "
              f"({len(wins)} measured windows)", active == 0, f"{active} active window(s)")
    gate = hc.ActivityGate() if hasattr(hc, "ActivityGate") else None
    prev, hits = hc.TreeSample(cpu_ticks=0, io_bytes=0, net=False), []
    for k in range(1, 4):          # a test suite: most of a core, every window
        cur = hc.TreeSample(cpu_ticks=400 * k, io_bytes=0, net=False)
        hits.append(gate.feed(prev, cur, 5.0) if gate else bool(hc.tree_was_active(prev, cur, 5.0)))
        prev = cur
    check("a busy test suite is active from its second reading", hits[1:] == [True, True], hits)

    print()
    if FAILS:
        print(f"FAIL ({len(FAILS)}): {FAILS}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
