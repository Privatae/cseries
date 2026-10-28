#!/usr/bin/env python3
"""A claude-code harness run that has just answered its model is not stopped as
"never started".

The harness's watchdog picks the window from each reading of the run's process
tree: an open connection gets the model-wait window, a tree with no connection
the idle window. The CLI closes its provider connection when the model's reply
has been delivered, and that message counts as activity only once the SDK
reader has handled it. The watchdog and the reader share one event loop, but
the watchdog reads the tree and judges it with no await in between, so a
reading taken after the close and before the reader ran is judged on the idle
window counted from the run's last activity (often its start). A run that was
silent for longer than the idle window while it waited on its model was then
stopped as "claude-code produced no output for N minutes and never started".
harness_core has the same rule (test_harness_liveness_close_race.py); this
runner keeps its own copy of it.

The rule now: a reading counts as on the network if it saw a connection or one
within the last idle window did. The activity clock is never reset, so every
run stays bounded by the model-wait window.

Nothing here waits on the machine. The run is driven through the real
`run_stream` handler with a stand-in SDK, and both inputs the race depends on
are scripted:
  * the process-tree readings (open, open, closed, ...) come from a script,
    each one moving a clock the watchdog reads (main's `time`) forward by a
    fixed number of seconds, so "the connection was open for longer than the
    idle window" is a line in the script and no test sleeps;
  * the stand-in SDK hands the reply to the reader only after the watchdog has
    judged the first reading with the connection closed: the gap is certain.

What this pins:
  1. a run whose connection closed as it answered, the reply read after the
     first closed reading, COMPLETES, and its reply reaches the caller;
  2. a run that closes its connection and then goes silent is still stopped as
     stuck, at the first reading more than one idle window after the last one
     that saw a connection: not at the first reading after the close, and not
     later;
  3. a connection that keeps opening and closing without the CLI ever saying
     anything does not extend the wait: the run is stopped at the model-wait
     window, counted from its last activity;
  4. the same while a tool is open: a connection held a moment ago keeps the
     model-wait window for the tool too, the run is stopped at that window and
     says it was holding a network connection, not that it was idle.

Run: APP_ROOT=<repo> python3 scripts/test_claude_code_liveness_close_race.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time as _real_time

FAILS = []

# Idle window 60 s and model-wait window 600 s, in the scripted clock's seconds.
IDLE_MIN, WAIT_MIN = 1, 10
IDLE_S, WAIT_S = IDLE_MIN * 60, WAIT_MIN * 60
# A reading past the end of a script moves the clock this far, so a runner that
# keeps reading after the script ended is stopped by it and the test cannot hang.
OVERRUN_JUMP = 100000.0


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[cc-close-race] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
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


class _Clock:
    """main.py's `time`: the real one, plus an offset that only the scripted
    readings move."""

    def __init__(self):
        self.offset = 0.0

    def time(self):
        return _real_time.time() + self.offset

    def __getattr__(self, name):
        return getattr(_real_time, name)


class _Sample:
    def __init__(self, net):
        self.cpu_ticks, self.io_bytes, self.net = 0, None, net


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, os.path.join(app_root, "cli"))
    sys.path.insert(0, os.path.join(app_root, "system-apps"))
    path = os.path.join(app_root, "system-apps/claude-code-harness/bin/main.py")
    spec = importlib.util.spec_from_file_location("cch_main_close_race", path)
    CCH = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(CCH)

    clock = _Clock()
    CCH.time = clock
    CCH._SDK_AVAILABLE = True
    CCH._get_token = lambda: "tok"
    CCH._ensure_claude_config = lambda: None
    CCH._SdkOptions = lambda **kw: kw
    CCH.worktree_diff = lambda d: ""
    CCH._current_creds = lambda: None
    CCH._stop_run_tree = lambda marker, **k: []
    reply = _blk("AssistantMessage", content=[_blk("TextBlock", text="the answer")])
    done = _blk("ResultMessage", is_error=False, model="claude-opus-5-5",
                usage=None, total_cost_usd=0.1)

    def run(script, sdk, *, release_at=None):
        """One run through the real run_stream. `script` is the readings, as
        (connection open?, seconds that pass before the reading); the first is
        the reading taken before the watchdog loop, with no time before it. The stand-in
        SDK's reply, if it has one, is handed over by the reading numbered
        `release_at` once that reading has been judged."""
        clock.offset = 0.0
        st = {"n": 0, "nets": [], "released": False, "ev": None,
              "delivered_at": None}

        def step(i):
            return script[i] if i < len(script) else (False, OVERRUN_JUMP)

        def poll(idle_s):
            # Asked once per pass, before the wait and the clock read that
            # precede reading number `st["n"]`: the time that passes before
            # that reading. The wait itself is short; how long it really takes
            # decides nothing.
            clock.offset += step(st["n"])[1]
            return 0.01
        CCH._idle_poll_s = poll

        def sample(pids, *a, **k):
            i = st["n"]
            st["n"] += 1
            net = step(i)[0]
            st["nets"].append(net)
            if release_at is not None and i == release_at:
                st["released"] = True
                if st["ev"] is not None:
                    st["ev"].set()
            return _Sample(net)
        CCH.sample_pids = sample
        CCH._sdk_query = sdk(st)
        body = {"prompt": "build it", "project_dir": tempfile.mkdtemp(),
                "idle_minutes": IDLE_MIN, "max_minutes": WAIT_MIN}
        frames = list(CCH.run_stream(FakeRequest(json.dumps(body).encode())))
        final = frames[-1]["payload"] if frames else {}
        texts = [(f.get("payload") or {}).get("text") for f in frames
                 if f.get("kind") == "text"]
        return final, texts, st

    def answering(st):
        # The reply is handed to the reader only after the reading numbered
        # `release_at` was judged: the watchdog's await is the reader's turn.
        async def _q(prompt, options):
            if not st["released"]:
                st["ev"] = asyncio.Event()
                await st["ev"].wait()
            st["delivered_at"] = st["n"]
            yield reply
            yield done
        return _q

    def silent(st):
        async def _q(prompt, options):
            await asyncio.sleep(3600)
            yield done
        return _q

    def tool_then_silent(st):
        # The model asks for a tool, which never returns.
        async def _q(prompt, options):
            yield _blk("AssistantMessage", content=[_blk(
                "ToolUseBlock", id="tool-1", name="Bash",
                input={"command": "make"})])
            await asyncio.sleep(3600)
            yield done
        return _q

    # The connection is open for 80 s of the scripted clock (longer than the
    # 60 s idle window) before the CLI answers and closes it.
    opened = [(False, 0.0), (True, 40.0), (True, 40.0)]

    print("== 1. a run whose connection closed as it answered, its reply read late ==")
    final, texts, st = run(opened + [(False, 10.0)] + [(False, 0.0)] * 300,
                           answering, release_at=3)
    check("the watchdog read the tree with the connection closed before the "
          "reply reached the reader",
          st["nets"][:4] == [False, True, True, False]
          and st["delivered_at"] in (None, *range(4, 10**6)),
          repr(st))
    check("a run that answered and closed its connection COMPLETES",
          final.get("state") == "complete" and final.get("ok") is True,
          f"state={final.get('state')} err={final.get('error')!r} "
          f"readings={st['n']}")
    check("...and its reply reached the caller", "the answer" in texts, repr(texts))

    print("== 2. a run that closes its connection and goes silent is still stopped ==")
    # Reading 3 is 10 s after the last one that saw the connection (inside the
    # idle window); reading 4 is 65 s after it (outside).
    final, texts, st = run(opened + [(False, 10.0), (False, 55.0)], silent)
    err = final.get("error") or ""
    check("a tree silent after its connection closed is stopped as stuck",
          final.get("state") == "timeout" and "stopped as stuck" in err
          and "never started" in err and final.get("limit") == "build.idle_minutes",
          f"state={final.get('state')} err={err!r}")
    check("...not at the first reading after the close (10 s after the last "
          "connection) but at the next, 65 s after it",
          st["n"] == 5, f"stopped after {st['n']} readings; nets={st['nets']}")

    print("== 3. a connection that keeps opening and closing does not extend the wait ==")
    # 45 s per reading, alternating: every closed reading is inside the idle
    # window of the open one before it. The model-wait window (600 s) is
    # passed at the 14th reading (630 s); the 13th is at 585 s.
    flap = [(False, 0.0)] + [((i % 2) == 1, 45.0) for i in range(1, 40)]
    final, texts, st = run(flap, silent)
    err = final.get("error") or ""
    check("a run that never says anything is stopped as hung at the model-wait "
          "window",
          final.get("state") == "timeout"
          and final.get("limit") == "build.model_wait_max_minutes"
          and "waited on the model for 10 minutes" in err,
          f"state={final.get('state')} limit={final.get('limit')!r} err={err!r}")
    check("...at the first reading past it, not before and not after",
          st["n"] == 15, f"stopped after {st['n']} readings")

    print("== 4. the same while a tool is open ==")
    # The tool's message is the run's last activity, handled while the first
    # reading's 45 s pass. Closed readings are the odd ones, each 45 s after an
    # open one. The first reading more than 600 s past the tool's message is the
    # 15th (630 s after it): closed, and 45 s after an open one, so the window
    # it is judged on can only come from the reading before it. The run is
    # stopped there, as a tool that held a connection and went quiet, on the
    # model-wait window: not at the 3rd reading on the 60 s idle window.
    flap_tool = [(False, 0.0)] + [((i % 2) == 0, 45.0) for i in range(1, 40)]
    final, texts, st = run(flap_tool, tool_then_silent)
    err = final.get("error") or ""
    check("a run with a tool open whose connection flaps is stopped at the "
          "model-wait window and says it was holding a connection",
          final.get("state") == "timeout" and "holding a network connection" in err
          and "10 minutes" in err and "produced no output" not in err,
          f"state={final.get('state')} limit={final.get('limit')!r} err={err!r}")
    check("...at the first reading past it, a closed one inside the idle window "
          "of the open one before it",
          st["n"] == 16 and st["nets"][-1] is False and st["nets"][-2] is True,
          f"stopped after {st['n']} readings; nets={st['nets']}")

    print()
    if FAILS:
        print(f"FAIL ({len(FAILS)}): {FAILS}")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
