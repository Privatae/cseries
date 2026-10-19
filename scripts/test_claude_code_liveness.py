#!/usr/bin/env python3
"""The claude-code harness judges a run by whether it is alive, and its
buffered /run has no total time cap.

The claude-code harness has its own runner (the Agent SDK, not a CLI pipe), so
it carried its own copy of the rules harness_core had: a 10-minute stop on
silence while a tool runs, a model-wait stop that called itself "the run's
whole budget", and a buffered /run with a TOTAL deadline of max_minutes
("claude-code exceeded 8min cap" and "…20min cap" while
the runs were working).

Driven through the real handlers with a stand-in SDK (no CLI, no pod). The
run's process tree is read through `sample_pids`; the gate swaps that for a
reading that rises, which is what a quiet but busy tool looks like.

Run: APP_ROOT=<repo> python3 scripts/test_claude_code_liveness.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[cc-liveness] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class FakeRequest:
    def __init__(self, body: bytes):
        self.body = body


def _blk(clsname, **kw):
    o = type(clsname, (), {})()
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "cli"))
    sys.path.insert(0, os.path.join(app_root, "system-apps"))
    path = os.path.join(app_root, "system-apps/claude-code-harness/bin/main.py")
    spec = importlib.util.spec_from_file_location("cch_main_liveness", path)
    CCH = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(CCH)

    CCH._SDK_AVAILABLE = True
    CCH._get_token = lambda: "tok"
    CCH._ensure_claude_config = lambda: None
    CCH._SdkOptions = lambda **kw: kw
    CCH.worktree_diff = lambda d: ""
    CCH._current_creds = lambda: None
    CCH.IDLE_FLOOR_S = 0.1
    CCH._ALIVE_FRAME_S = 0.3
    CCH._stop_run_tree = lambda marker, **k: []

    def _script(steps):
        async def _fake_query(prompt, options):
            for step in steps:
                if isinstance(step, (int, float)):
                    await asyncio.sleep(step)
                else:
                    yield step
        return _fake_query

    done = _blk("ResultMessage", is_error=False, model="claude-opus-5-5",
                usage=None, total_cost_usd=0.1)
    tool = _blk("AssistantMessage", content=[
        _blk("ToolUseBlock", id="t1", name="Bash", input={"command": "pytest -q"})])
    result = _blk("UserMessage", content=[_blk("ToolResultBlock", tool_use_id="t1")])
    body = {"prompt": "build it", "max_minutes": 0.01, "idle_minutes": 0.005}

    # A busy tree: CPU ticks rise on every reading.
    ticks = [0]

    class _Sample:
        def __init__(self, cpu, net=False):
            self.cpu_ticks, self.io_bytes, self.net = cpu, None, net

    def _busy(pids, *a, **k):
        ticks[0] += 1000
        return _Sample(ticks[0])
    CCH.sample_pids = _busy

    print("== 1. a quiet tool that is using CPU is not stopped ==")
    CCH._sdk_query = _script([tool, 1.5, result, done])
    t0 = time.time()
    frames = list(CCH.run_stream(FakeRequest(json.dumps(
        {**body, "project_dir": tempfile.mkdtemp()}).encode())))
    p = frames[-1]["payload"]
    check("a tool silent for 1.5 s (idle window 0.3 s) whose processes use CPU "
          "completes", p.get("state") == "complete" and p.get("ok") is True,
          f"{p.get('state')} {p.get('error')} {time.time() - t0:.1f}s")
    working = [f for f in frames if f.get("kind") == "status"
               and (f.get("payload") or {}).get("state") == "working"]
    check("...and `working` frames kept the kernel's backstop fed",
          len(working) >= 1, f"{len(working)} working frames")

    print("== 2. the model-wait stop tells the truth ==")
    CCH.sample_pids = lambda pids, *a, **k: None       # nothing to read
    CCH._sdk_query = _script([_blk("AssistantMessage", content=[
        _blk("TextBlock", text="writing index.html")]), 30])
    frames = list(CCH.run_stream(FakeRequest(json.dumps(
        {**body, "project_dir": tempfile.mkdtemp()}).encode())))
    err = frames[-1]["payload"].get("error") or ""
    check("a model wait is not called the run's whole budget", "whole budget" not in err, err)
    check("...and says the run itself has no time limit", "no time limit" in err, err)

    print("== 3. buffered /run has no total cap ==")
    hb = _blk("SystemMessage", subtype="thinking_tokens")
    steps = [_blk("AssistantMessage", content=[_blk("TextBlock", text="starting")])]
    steps += [x for _ in range(15) for x in (0.1, hb)] + [done]
    CCH._sdk_query = _script(steps)
    if hasattr(CCH, "_run_sdk"):
        # The pre-fix /run drove the SDK through _run_sdk; the same stand-in
        # SDK serves it, so main is measured on the same script.
        CCH.query = CCH._sdk_query
    t0 = time.time()
    out = CCH.run(FakeRequest(json.dumps(
        {**body, "max_minutes": 1, "project_dir": tempfile.mkdtemp()}).encode()))
    res = out[0] if isinstance(out, tuple) else out
    check("a buffered run still talking past a tiny max_minutes completes",
          isinstance(res, dict) and res.get("ok") is True and not isinstance(out, tuple),
          repr(out)[:300])

    CCH._sdk_query = _script([tool, 30])
    out = CCH.run(FakeRequest(json.dumps(
        {**body, "project_dir": tempfile.mkdtemp()}).encode()))
    res, status = (out if isinstance(out, tuple) else (out, 200))
    check("a buffered run with no sign of life is stopped as stuck (504) with "
          "its reason", status == 504 and "stopped as stuck" in (res.get("error") or ""),
          repr(out)[:300])

    print()
    if FAILS:
        print(f"FAIL ({len(FAILS)}): {FAILS}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
