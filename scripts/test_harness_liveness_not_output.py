#!/usr/bin/env python3
"""A harness run is judged by whether it is ALIVE, not by whether it printed.

Cap audit (a Host handles continuous 24/7 work on large data, efficiently).
The pod's watchdog killed a run after 10 minutes with
no OUTPUT, so a quiet test suite, a heavy install or a first model turn that
streams nothing looked exactly like a wedged process. One Host lost five
working runs that way (one had a 121 KB
diff). The buffered /run kept a total wall-clock cap (twenty runs stopped on
2026-07-02 at 3-20 minutes while working). The model-wait stop called itself
"the run's whole budget", which was false.

What this pins, each by RUNNING the real runner with a stub CLI (no pod):
  1. HR-1: a command that prints nothing but uses CPU past the idle window
     SURVIVES and completes;
  2. HR-1: a CLI that has not spoken yet but holds an open network connection
     (waiting on its provider) gets the model-wait window, not the idle one;
  3. HR-1: a tree with no sign of life (no output, no CPU, no connection) is
     still stopped at the idle window (the protection stays);
  4. HR-1: a quiet-but-alive run sends `working` frames, so the kernel's
     frame-based silence backstop never gives up on it;
  5. HR-2: the model-wait stop no longer calls itself the run's whole budget
     and says the run has no time limit;
  6. HR-3: buffered /run has no total cap — a run still producing output past
     max_minutes completes, and a stopped buffered run returns its transcript;
  7. HR-9: an acceptance check runs with the timeout it declared (no silent
     1800 s clamp) and says how much output came before the kept tail;
  8. HR-28: a running CLI's stderr (kimi's reasoning) is kept up to the
     kernel's own 1 MiB bound, not a 4 KB tail.

Run: APP_ROOT=<repo> python3 scripts/test_harness_liveness_not_output.py
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


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[liveness] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "cli"))
    sys.path.insert(0, os.path.join(app_root, "system-apps"))
    from _harness_runtime import harness_core as hc
    from _harness_runtime import specs
    from _harness_runtime import events

    tmp = Path(tempfile.mkdtemp(prefix="liveness-"))

    def _literal(harness_id, line, seq, state=None):
        raw = (line or "").strip()
        kinds = {"TOOL_CALL": "tool_call", "TOOL_RESULT": "tool_result",
                 "STATUS": "status", "TEXT": "text"}
        if raw in kinds:
            return [{"ts": time.time(), "seq": seq, "kind": kinds[raw],
                     "payload": {"text": raw}}]
        return [{"ts": time.time(), "seq": seq, "kind": "text",
                 "payload": {"text": raw, "raw": True}}]

    events.normalize_line = _literal

    KM = specs.ALL_SPECS["kimi"]
    hc._BARE_KEY_BASE_CACHE.clear()
    hc._BARE_KEY_BASE_PROBE = lambda base, key: base == KM.bare_key_base_url
    kp = tmp / "kimi-bare"
    kp.write_text("sk-moonshot-1")

    def stub(name: str, script: str) -> Path:
        p = tmp / name
        p.write_text("#!/bin/sh\n" + script)
        p.chmod(0o755)
        return p

    class Req:
        def __init__(self, body: dict):
            self.body = json.dumps(body).encode()

    # Seconds, not minutes: idle window 1.2 s, model-wait window 4.8 s.
    hc.IDLE_FLOOR_S = 0.5
    hc.ALIVE_FRAME_S = 0.6   # a branch without it ignores this attribute
    IDLE_MIN, WAIT_MIN = 0.02, 0.08
    py = sys.executable

    def run(name: str, script: str, *, handler="stream", **extra):
        spec = dataclasses.replace(KM, binary=stub(name, script), creds_path=kp)
        body = {"prompt": "build it", "project_dir": str(tmp / f"proj-{name}"),
                "model": "k3", "idle_minutes": IDLE_MIN, "max_minutes": WAIT_MIN}
        body.update(extra)
        t0 = time.time()
        if handler == "stream":
            frames = list(hc.make_run_stream(spec)(Req(body)))
            el = time.time() - t0
            statuses = [f for f in frames if f.get("kind") == "status"]
            return (statuses[-1]["payload"] if statuses else {}), el, frames
        out = hc.make_run(spec)(Req(body))
        el = time.time() - t0
        return out, el, []

    print("== 1. a quiet command using CPU past the idle window ==")
    burn = f"{py} -c 'import time\nt=time.time()\nwhile time.time()-t<3.5: pass'"
    final, el, frames = run("quiet-cpu", f"""
echo TEXT
echo TOOL_CALL
{burn}
echo TOOL_RESULT
echo TEXT
exit 0
""")
    check("a command silent for 3.5 s (idle window 1.2 s) but using CPU SURVIVES",
          final.get("state") == "complete" and final.get("ok") is True,
          f"state={final.get('state')} err={final.get('error')!r} el={el:.1f}")
    working = [f for f in frames if f.get("kind") == "status"
               and (f.get("payload") or {}).get("state") == "working"]
    check("...and it sent `working` frames while quiet (the kernel backstop "
          "resets on frames)", len(working) >= 1, f"{len(working)} working frames")
    check("...a working frame says a command is still running",
          any("command is still running" in ((f.get("payload") or {}).get("detail") or "")
              for f in working), repr([f.get("payload") for f in working])[:200])

    print("== 2. never spoke, but waiting on the network ==")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    held = []

    def _accept():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            held.append(c)       # accept and never answer: a slow provider
    threading.Thread(target=_accept, daemon=True).start()
    wait_sock = (f"{py} -c 'import socket,time\ns=socket.create_connection((\"127.0.0.1\",{port}))\n"
                 f"time.sleep(3.0)\nprint(\"TEXT\")'")
    final, el, _ = run("net-wait", f"""
{wait_sock}
exit 0
""")
    check("a CLI silent for 3 s with an open connection (idle 1.2 s, model "
          "wait 4.8 s) SURVIVES", final.get("state") == "complete" and final.get("ok") is True,
          f"state={final.get('state')} err={final.get('error')!r} el={el:.1f}")

    print("== 3. no sign of life at all is still stopped at the idle window ==")
    final, el, _ = run("wedged", """
echo TEXT
echo TOOL_CALL
sleep 30
""")
    check("a parked command (no output, no CPU, no connection) is stopped at idle",
          final.get("state") == "timeout" and 1.0 <= el < 4.0,
          f"state={final.get('state')} el={el:.1f}")
    check("...and the error says it was stopped as stuck, not for running long",
          "stopped as stuck" in (final.get("error") or "")
          and "not stopped for running long" in (final.get("error") or ""),
          final.get("error"))

    print("== 5. the model-wait stop tells the truth ==")
    final, el, _ = run("model-hang", """
echo TEXT
echo TOOL_CALL
echo TOOL_RESULT
sleep 30
""")
    err = final.get("error") or ""
    check("a model turn with nothing arriving is stopped at the model-wait window",
          final.get("state") == "timeout" and 4.4 <= el < 14, f"el={el:.1f}")
    check("...the error no longer calls it the run's whole budget",
          "whole budget" not in err, err)
    check("...and says the run itself has no time limit",
          "no time limit" in err, err)

    print("== 6. buffered /run has no total cap ==")
    # Output every 0.5 s for 6 s against max_minutes = 0.08 (4.8 s): a total
    # cap stops it; a liveness rule lets it finish.
    final, el, _ = run("buffered-long", """
i=0
while [ $i -lt 12 ]; do echo "working $i"; sleep 0.5; i=$((i+1)); done
echo "done"
exit 0
""", handler="run")
    out = final[0] if isinstance(final, tuple) else final
    check("a buffered run producing output past max_minutes COMPLETES",
          isinstance(out, dict) and out.get("ok") is True and "done" in (out.get("transcript") or ""),
          repr(out)[:300])
    final, el, _ = run("buffered-wedged", "echo started\nsleep 30\n", handler="run")
    out, status = (final if isinstance(final, tuple) else (final, 200))
    check("a buffered run with no sign of life is stopped and still returns its transcript",
          status == 504 and isinstance(out, dict) and "started" in (out.get("transcript") or "")
          and "stopped as stuck" in (out.get("error") or ""), repr(final)[:300])

    print("== 7. HR-9: acceptance checks run with the timeout they declared ==")
    chk = hc.make_check(KM)
    pd = tmp / "proj-check"
    pd.mkdir()
    out = chk(Req({"project_dir": str(pd), "commands": [
        {"cmd": "true", "timeout_s": 3600},
        {"cmd": f"{py} -c 'print(\"x\" * 5000)'"}]}))
    res = out.get("results") if isinstance(out, dict) else None
    check("a check declaring 3600 s runs with 3600 s (no silent 1800 s clamp)",
          bool(res) and res[0].get("timeout_s") == 3600, repr(out)[:300])
    check("...and a cut output tail says how much came before it",
          bool(res) and res[1].get("output_cut_chars", 0) > 0, repr(out)[:300])

    print("== 8. HR-28: a running CLI's stderr is kept to the kernel's own bound ==")
    final, el, _ = run("stderr-reasoning", f"""
{py} -c 'import sys
for i in range(4000): sys.stderr.write("reasoning line %05d ................................\\n" % i)'
echo TEXT
exit 0
""")
    st = final.get("stderr") or ""
    check("200 KB of reasoning on stderr comes back (not a 4 KB tail)",
          len(st) > 150_000 and "reasoning line 03999" in st, f"len={len(st)}")

    srv.close()
    for c in held:
        c.close()
    print()
    if FAILS:
        print(f"FAIL ({len(FAILS)}): {FAILS}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
