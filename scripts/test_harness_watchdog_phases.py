#!/usr/bin/env python3
"""The idle watchdog knows what the CLI is doing when it goes quiet.

Measured on one Host 2026-09-21: gpt-6-astra on
codex, two builds, both killed at the 10-minute idle mark with exit -9. The
last event each time was a COMPLETED `pip install` — and no tool_call after
it. codex emits item.started before it runs anything, so it was not running
anything: it had handed the tool output to the model and was waiting on the
model's next turn. The process was alive and doing exactly what it should.
The watchdog read silence as "wedged" and killed it, 660 s and 675 s in.

The idle window had been sized on "a reasoning-heavy turn runs minutes with
no output" (harness_core.py, IDLE_MINUTES). Astra runs longer than ten. So:

  * while a TOOL is in flight (tool_call with no tool_result yet), or before
    the CLI has emitted anything at all, silence past idle_s is a wedged run
    and is killed — the rule the watchdog was built for;
  * after a tool_result or any other event, with no tool in flight, the CLI
    is in a MODEL TURN: silence is bounded by the run's own budget
    (max_minutes, the number the caller already sends), never by idle_s.

What this pins, each by RUNNING the real stream path with a stub CLI:
  1. the astra shape — tool_result, then silence longer than idle_s but
     inside the budget — SURVIVES and completes;
  2. a model turn silent past the whole budget is stopped, and the error
     says it waited on the model, not that a command hung;
  3. a command silent past idle_s is still killed, and the error says a
     command was running;
  4. a CLI that never spoke is still killed at idle_s;
  5. the budget can never be tighter than idle_s.

The vendor parser is swapped for a literal one (TEXT / TOOL_CALL /
TOOL_RESULT lines): the watchdog is under test, not a dialect — the dialects
have their own goldens in test_harness_runtime.py.

Run: APP_ROOT=<repo> python3 scripts/test_harness_watchdog_phases.py
"""
import dataclasses
import json
import os
import sys
import tempfile
import time
from pathlib import Path

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[watchdog] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "cli"))
    sys.path.insert(0, os.path.join(app_root, "system-apps"))
    from _harness_runtime import harness_core as hc
    from _harness_runtime import specs
    from _harness_runtime import events

    tmp = Path(tempfile.mkdtemp(prefix="watchdog-"))

    # The literal parser: one line -> one event of the named kind.
    def _literal(harness_id, line, seq, state=None):
        raw = (line or "").strip()
        kinds = {"TOOL_CALL": "tool_call", "TOOL_RESULT": "tool_result",
                 "FILE_EDIT": "file_edit", "STATUS": "status", "TEXT": "text"}
        if raw in kinds:
            return [{"ts": time.time(), "seq": seq, "kind": kinds[raw], "payload": {"text": raw}}]
        # Anything else is what events.normalize_line does with a line no
        # parser claimed: a text frame marked raw=True (the never-drop rule).
        return [{"ts": time.time(), "seq": seq, "kind": "text",
                 "payload": {"text": raw, "raw": True}}]

    events.normalize_line = _literal

    # A live-looking kimi spec on a stub binary, exactly as the runtime test
    # builds one: the auth probe is pinned so nothing touches the network.
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

    # Seconds, not minutes: idle_s = 1.2 s (floor lowered for the gate),
    # turn budget = 0.06 min = 3.6 s. The shape of the rule is under test.
    hc.IDLE_FLOOR_S = 0.5
    IDLE_MIN, BUDGET_MIN = 0.02, 0.06

    def run(script_name: str, script: str, **body_extra) -> tuple[dict, float]:
        spec = dataclasses.replace(KM, binary=stub(script_name, script), creds_path=kp)
        body = {"prompt": "build it", "project_dir": str(tmp / f"proj-{script_name}"),
                "model": "k3", "idle_minutes": IDLE_MIN, "max_minutes": BUDGET_MIN}
        body.update(body_extra)
        t0 = time.time()
        frames = list(hc.make_run_stream(spec)(Req(body)))
        el = time.time() - t0
        statuses = [f for f in frames if f.get("kind") == "status"]
        return (statuses[-1]["payload"] if statuses else {}), el

    print("== 1. the astra shape: silent after a tool result, inside the budget ==")
    final, el = run("astra-shape", """
echo TEXT
echo TOOL_CALL
echo TOOL_RESULT
sleep 2
echo TEXT
exit 0
""")
    check("a model turn silent for 2 s (> idle 1.2 s, < budget 3.6 s) SURVIVES",
          final.get("state") == "complete" and final.get("ok") is True,
          f"state={final.get('state')} ok={final.get('ok')} err={final.get('error')!r} el={el:.1f}")

    print("== 2. a model turn silent past the whole budget ==")
    final, el = run("model-wait", """
echo TEXT
echo TOOL_CALL
echo TOOL_RESULT
sleep 30
""")
    check("stopped, but only once the BUDGET is spent (not at idle_s)",
          final.get("state") == "timeout" and 3.4 <= el < 12, f"state={final.get('state')} el={el:.1f}")
    check("...and the error says it waited on the model",
          "waited on the model" in (final.get("error") or ""), final.get("error"))
    check("...and not that a command hung",
          "command was running" not in (final.get("error") or ""), final.get("error"))

    print("== 3. a command silent past idle_s ==")
    final, el = run("tool-hang", """
echo TEXT
echo TOOL_CALL
sleep 30
""")
    check("killed at idle_s, not the budget",
          final.get("state") == "timeout" and 1.0 <= el < 3.2, f"state={final.get('state')} el={el:.1f}")
    check("...and the error says a command was running",
          "while a command was running" in (final.get("error") or ""), final.get("error"))

    print("== 4. a CLI that never spoke ==")
    final, el = run("never-spoke", "sleep 30\n")
    check("killed at idle_s", final.get("state") == "timeout" and 1.0 <= el < 3.2,
          f"state={final.get('state')} el={el:.1f}")
    check("...and the error says it never started",
          "never started" in (final.get("error") or ""), final.get("error"))

    print("== 5. the budget can never be tighter than idle_s ==")
    final, el = run("budget-floor", """
echo TEXT
echo TOOL_CALL
echo TOOL_RESULT
sleep 30
""", max_minutes=0.001)
    check("a 0.06 s budget is lifted to idle_s (killed at ~1.2 s, not instantly)",
          final.get("state") == "timeout" and 1.0 <= el < 3.2, f"el={el:.1f}")

    print("== 6. codex's file edits end a tool without a tool_result (review N1) ==")
    # events._codex_item_events: file_change started -> tool_call(apply_patch),
    # completed -> file_edit frames, never tool_result. A model turn after an
    # edit must still get the budget, or the fix stops applying after the
    # first apply_patch — on the very harness it was written for.
    final, el = run("after-edit", """
echo TEXT
echo TOOL_CALL
echo FILE_EDIT
sleep 2
echo TEXT
exit 0
""")
    check("a model turn after a file_edit (no tool_result) SURVIVES 2 s of silence",
          final.get("state") == "complete" and final.get("ok") is True,
          f"state={final.get('state')} err={final.get('error')!r} el={el:.1f}")

    print("== 7. a banner is not a model turn (review N3) ==")
    # codex's thread.started / agy's init are status frames. A CLI that printed
    # its banner and then wedged on stdin or auth is the never-started case.
    final, el = run("banner-then-wedge", """
echo STATUS
sleep 30
""")
    check("status-only then silence is killed at idle_s, not the budget",
          final.get("state") == "timeout" and 1.0 <= el < 3.2, f"state={final.get('state')} el={el:.1f}")
    check("...and the error says it never started",
          "never started" in (final.get("error") or ""), final.get("error"))

    print("== 8. an unparseable banner is not a model turn either (re-review NB2) ==")
    # The real normalizer's never-drop fallback: a line no parser claimed
    # becomes a text frame marked raw=True. Check the marker on the REAL
    # function, then drive the watchdog with a raw banner and silence.
    # events.normalize_line was swapped for the literal parser above, so load
    # a pristine copy of the module to check the real marker.
    import importlib.util
    spec_ = importlib.util.spec_from_file_location(
        "_pristine_events", os.path.join(app_root, "system-apps", "_harness_runtime", "events.py"))
    pristine = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(pristine)
    frames = pristine.normalize_line("codex", "Codex CLI v0.154.0 (rust)", 0)
    check("the real normalizer marks an unparseable line text+raw",
          len(frames) == 1 and frames[0]["kind"] == "text" and frames[0]["payload"].get("raw") is True,
          frames)
    frames = pristine.normalize_line("codex", '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"hi"}}', 0)
    check("...and a parsed vendor text frame is NOT marked raw",
          len(frames) == 1 and frames[0]["kind"] == "text" and not frames[0]["payload"].get("raw"),
          frames)
    final, el = run("raw-banner-then-wedge", """
echo "Codex CLI v0.154.0 (rust)"
sleep 30
""")
    check("a raw banner then silence is killed at idle_s, not the budget",
          final.get("state") == "timeout" and 1.0 <= el < 3.2, f"state={final.get('state')} el={el:.1f}")

    print()
    if FAILS:
        print(f"FAIL {len(FAILS)}: {FAILS}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
