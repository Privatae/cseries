#!/usr/bin/env python3
"""Harness runs report what actually happened (the seven-harness test).

Every check drives the REAL code path with vendor-shaped input; none supplies
the value it asserts. The frame shapes are the ones measured on that day's
runs; the text in them is placeholder.

  1. claude-code: _sdk_options gives the Agent SDK a line bound that holds an
     image Read (a run died at the SDK's 1 MiB default).
  2. cursor: its stream-json reads through its own parser — no raw rows,
     prose fragments joined, the repeated whole message skipped, tool calls
     with results and edits, the result frame's usage kept.
  3. grok: a mid-step tool_call_update is a lifecycle status, not a raw row;
     the completed update still closes the call.
  4. opencode: (a) the per-run output ceiling is the model's own catalog
     limit, and unset when the catalog has none; (b) a run whose last step
     ended "length" with no file changes is not ok and says why, through the
     REAL watched runner; a zero-change landed run says it changed nothing.
  5. usage: antigravity's / cursor's / opencode's cache reads are separate
     from input; antigravity's thinking_tokens is reasoning, not unmapped.
  6. vendor cost: stream-reported cost reaches harness_usage, grok's end-frame
     cost reaches its usage frame, and the windows tool says "not reported"
     instead of $0.00.

The watched runner kills process trees, so this gate runs only inside a
private PID namespace and re-executes itself there (the
test_harness_vendor_refusal_run.py pattern).

Run: APP_ROOT=<repo> python3 scripts/test_harness_run_truth.py
"""
import ast
import asyncio
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FAILS = []
_ROOT = os.environ.get("APP_ROOT") or str(Path(__file__).resolve().parents[1])


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[run-truth] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _in_private_pid_namespace() -> bool:
    try:
        return Path("/proc/1/comm").read_text().strip() == "tini" and os.getppid() == 1 \
            and len([e for e in os.listdir("/proc") if e.isdigit()]) < 50
    except OSError:
        return False


class Req:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()


def _frames(EV, harness, lines):
    state, out, seq = {}, [], 0
    for ln in lines:
        evs = EV.normalize_line(harness, ln, seq, state)
        seq += len(evs)
        out += evs
    return out


# ── 1. claude-code ──────────────────────────────────────────────────────────
def t_claude_code():
    import importlib.util as ilu
    path = os.path.join(_ROOT, "system-apps/claude-code-harness/bin/main.py")
    spec = ilu.spec_from_file_location("cch_main_truth", path)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._SdkOptions = lambda **kw: kw          # the SDK's options class, passed through
    opts = mod._sdk_options("/w/x", None, None)
    got = opts.get("max_buffer_size")
    # The line an image Read emits: the image block AND tool_use_result.file.base64,
    # each up to the bundled CLI's 5 MiB per-image cap, in one JSON message.
    b64 = "A" * (5 * 1024 * 1024)
    line = json.dumps({"type": "user", "message": {"content": [{"type": "tool_result",
                       "content": [{"type": "image", "source": {"data": b64}}]}]},
                       "tool_use_result": {"type": "image", "file": {"base64": b64}}})
    check("claude-code: _sdk_options sets the SDK's max_buffer_size",
          isinstance(got, int), repr(got))
    check("claude-code: an image Read's line (image twice, 5 MiB base64 each) fits it",
          isinstance(got, int) and len(line.encode()) < got and got > 1024 * 1024,
          f"line={len(line)} bound={got}")


# ── 2. cursor ───────────────────────────────────────────────────────────────
def t_cursor(EV, CM):
    sid = "s-1"
    lines = [
        {"type": "system", "subtype": "init", "model": "composer-x", "cwd": "/w", "session_id": sid},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "build it"}]}},
        {"type": "thinking", "subtype": "delta", "text": "I am ", "timestamp_ms": 1},
        {"type": "thinking", "subtype": "delta", "text": "planning", "timestamp_ms": 2},
        {"type": "thinking", "subtype": "completed", "timestamp_ms": 3},
        {"type": "tool_call", "subtype": "started", "call_id": "c1",
         "tool_call": {"editToolCall": {"args": {"path": "/w/a.html", "streamContent": "<p>"}}}},
        {"type": "tool_call", "subtype": "completed", "call_id": "c1",
         "tool_call": {"editToolCall": {"args": {"path": "/w/a.html", "streamContent": "<p>"},
                                        "result": {"success": {"path": "/w/a.html", "linesAdded": 1,
                                                               "message": "Wrote contents to /w/a.html",
                                                               "afterFullFileContent": "<p>"}}}}},
        {"type": "tool_call", "subtype": "started", "call_id": "c2",
         "tool_call": {"shellToolCall": {"args": {"command": "node -e 1"}, "description": "check"}}},
        {"type": "tool_call", "subtype": "completed", "call_id": "c2",
         "tool_call": {"shellToolCall": {"args": {"command": "node -e 1"},
                                         "result": {"failure": {"exitCode": "127", "stdout": "",
                                                                "stderr": "node: not found"}}}}},
        {"type": "tool_call", "subtype": "started", "call_id": "c3",
         "tool_call": {"lsToolCall": {"args": {"targetDirectory": "/w"}}}},
        {"type": "tool_call", "subtype": "completed", "call_id": "c3",
         "tool_call": {"lsToolCall": {"args": {"targetDirectory": "/w"},
                                      "result": {"success": {"files": ["a.html"]}}}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Built "}]}, "timestamp_ms": 4},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "it."}]}, "timestamp_ms": 5},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Built it."}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "Built it.",
         "usage": {"inputTokens": 1000, "outputTokens": 200, "cacheReadTokens": 4000,
                   "cacheWriteTokens": 0}},
    ]
    fr = _frames(EV, "cursor", [json.dumps(o) for o in lines])
    raw = [f for f in fr if f["payload"].get("raw")]
    check("cursor: no line falls to a raw row", not raw, json.dumps(raw)[:300])
    kinds = [f["kind"] for f in fr]
    txt = [f for f in fr if f["kind"] == "text"]
    check("cursor: prose fragments are partial chunks, the repeated whole message is not prose",
          [f["payload"].get("delta") for f in txt] == [True, True]
          and any(f["kind"] == "status" and f["payload"].get("state") == "message_end" for f in fr),
          json.dumps(txt))
    th = [f for f in fr if f["kind"] == "thinking"]
    check("cursor: thinking deltas join (delta) and carry the reasoning",
          all(f["payload"].get("delta") is True for f in th)
          and "".join(f["payload"]["text"] for f in th) == "I am planning", json.dumps(th))
    calls = [f["payload"] for f in fr if f["kind"] == "tool_call"]
    res = [f["payload"] for f in fr if f["kind"] == "tool_result"]
    edits = [f["payload"] for f in fr if f["kind"] == "file_edit"]
    check("cursor: any <x>ToolCall is a call named <x> with the renderer's keys",
          [c["tool"] for c in calls] == ["edit", "shell", "ls"]
          and calls[0]["args"].get("path") == "/w/a.html"
          and calls[1]["args"].get("command") == "node -e 1"
          and calls[2]["args"].get("path") == "/w", json.dumps(calls))
    check("cursor: results close their calls by id; a failed shell is an error with its exit code",
          [(r["id"], r["status"]) for r in res] == [("c1", "ok"), ("c2", "error"), ("c3", "ok")]
          and res[1].get("exit_code") == 127 and "not found" in res[1]["result"]
          and "a.html" in res[2]["result"], json.dumps(res))
    check("cursor: a successful edit is a file_edit folded onto its call (no tool)",
          len(edits) == 1 and edits[0]["path"] == "/w/a.html" and "tool" not in edits[0]
          and edits[0]["id"] == "c1", json.dumps(edits))
    check("cursor: the result frame is done + usage",
          "usage" in kinds and any(f["kind"] == "status" and f["payload"].get("state") == "done"
                                   for f in fr), str(kinds))
    agg = CM.aggregate_usage_events(fr, "cursor")
    check("cursor: usage reaches the aggregate with cache SEPARATE from input",
          agg.get("input_tokens") == 5000 and agg.get("cached_input_tokens") == 4000
          and agg.get("total_tokens") == 5200 and not (agg["frames"][0].get("unmapped")),
          json.dumps({k: agg.get(k) for k in ("input_tokens", "cached_input_tokens", "total_tokens")}))


# ── 3. grok ─────────────────────────────────────────────────────────────────
def t_grok(EV, CM):
    lines = [
        {"type": "tool_call", "toolCallId": "k1", "title": "Read", "kind": "read",
         "status": "pending", "toolName": "read_file", "rawInput": {"path": "/w/a"}},
        {"type": "tool_call_update", "toolCallId": "k1", "status": None, "content": [],
         "rawOutput": None, "locations": [{"path": "/w"}]},
        {"type": "tool_call_update", "toolCallId": "k1", "status": "in_progress", "content": [],
         "rawOutput": None, "locations": []},
        {"type": "tool_call_update", "toolCallId": "k1", "status": "completed",
         "content": [{"type": "content", "content": {"type": "text", "text": "body"}}]},
        {"type": "end", "stopReason": "EndTurn",
         "usage": {"input_tokens": 300, "cache_read_input_tokens": 7000,
                   "cache_creation_input_tokens": 0, "output_tokens": 200,
                   "reasoning_tokens": 150, "total_tokens": 7500},
         "num_turns": 2, "total_cost_usd": 1.97625204, "modelUsage": {"m": {"costUSD": 1.97625204}}},
    ]
    fr = _frames(EV, "grok", [json.dumps(o) for o in lines])
    raw = [f for f in fr if f["payload"].get("raw")]
    check("grok: mid-step tool_call_updates are no raw rows", not raw, json.dumps(raw)[:300])
    upd = [f["payload"] for f in fr if f["kind"] == "status" and f["payload"].get("state") == "tool_update"]
    check("grok: each is a tool_update lifecycle status", len(upd) == 2, json.dumps(upd))
    res = [f["payload"] for f in fr if f["kind"] == "tool_result"]
    check("grok: the final update still closes the call",
          len(res) == 1 and res[0]["id"] == "k1" and res[0]["tool"] == "read_file"
          and res[0]["result"] == "body", json.dumps(res))
    agg = CM.aggregate_usage_events(fr, "grok")
    check("grok: the end frame's total_cost_usd reaches the run's usage",
          abs(float(agg.get("cost_usd") or 0) - 1.97625204) < 1e-9, repr(agg.get("cost_usd")))


# ── 4a. opencode output ceiling ─────────────────────────────────────────────
def t_opencode_env(specs, tmp: Path):
    stub = tmp / "opencode-models"
    known = {"id": "z-ai/glm-x", "limit": {"context": 1000000, "output": 131072}}
    unknown = {"id": "z-ai/nolimit", "limit": {"context": 131072, "output": 0}}
    listing = ("openrouter/z-ai/glm-x\n" + json.dumps(known, indent=2) + "\n"
               "openrouter/z-ai/nolimit\n" + json.dumps(unknown, indent=2) + "\n")
    (tmp / "listing.txt").write_text(listing)
    stub.write_text("#!/bin/sh\n"
                    f'if [ "$1" = "models" ] && [ "$2" = "openrouter" ]; then cat "{tmp}/listing.txt"; exit 0; fi\n'
                    "exit 3\n")
    stub.chmod(0o755)
    spec = dataclasses.replace(specs.ALL_SPECS["opencode"], binary=stub)
    run_env = getattr(spec, "run_env", None)
    check("opencode: the spec has a per-run env", callable(run_env), repr(run_env))
    if not callable(run_env):
        return
    specs._OC_LIMITS.clear()
    env = {"PATH": os.environ.get("PATH", "")}
    got = run_env(spec, "openrouter/z-ai/glm-x", env)
    check("opencode: a model with a catalog limit runs at THAT limit, not 32,000",
          got == {"OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX": "131072"}, repr(got))
    got = run_env(spec, "openrouter/z-ai/nolimit", env)
    check("opencode: a model with NO known limit leaves the override unset (opencode's default)",
          got == {}, repr(got))
    got = run_env(spec, "openrouter/z-ai/missing", env)
    check("opencode: a model absent from the listing leaves it unset", got == {}, repr(got))
    bad = dataclasses.replace(spec, binary=tmp / "does-not-exist")
    specs._OC_LIMITS.clear()
    got = run_env(bad, "openrouter/z-ai/glm-x", env)
    check("opencode: an unreadable listing leaves it unset", got == {}, repr(got))
    got = run_env(spec, "openrouter/z-ai/glm-x",
                  {**env, "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX": "5000"})
    check("opencode: a deployment's own setting is kept", got == {}, repr(got))


# ── 4b. opencode stopped at the output limit, through the real runner ───────
def t_opencode_length(hc, specs, tmp: Path):
    key = tmp / "key"
    key.write_text("placeholder-not-a-key")
    hc.IDLE_FLOOR_S = 0.5

    def run(name, body_sh):
        stub = tmp / name
        stub.write_text("#!/bin/sh\n"
                        'if [ "$1" = "--version" ]; then echo "1.18.35"; exit 0; fi\n'
                        'if [ "$1" = "models" ]; then exit 0; fi\n' + body_sh)
        stub.chmod(0o755)
        spec = dataclasses.replace(specs.ALL_SPECS["opencode"], binary=stub, creds_path=key,
                                   baked=True)
        proj = tmp / f"proj-{name}"
        proj.mkdir(exist_ok=True)
        frames = list(hc.make_run_stream(spec)(Req(
            {"prompt": "x", "project_dir": str(proj), "idle_minutes": 0.05,
             "model": "openrouter/z-ai/glm-x"})))
        st = [f for f in frames if f.get("kind") == "status"
              and (f.get("payload") or {}).get("state") in ("complete", "timeout")]
        return st[-1]["payload"] if st else {}

    start = json.dumps({"type": "step_start", "part": {"type": "step-start"}})

    def finish(reason):
        return json.dumps({"type": "step_finish", "part": {
            "type": "step-finish", "reason": reason, "cost": 0.3,
            "tokens": {"total": 49744, "input": 16720, "output": 2, "reasoning": 31998,
                       "cache": {"write": 0, "read": 1024}}}})

    final = run("length-empty", f"echo '{start}'\necho '{finish('length')}'\nexit 0\n")
    check("opencode: rc 0 + last step 'length' + no file changes is NOT ok",
          final.get("ok") is False and final.get("exit_code") == 0, repr(final.get("ok")))
    check("opencode: it says it stopped at the model's output limit and changed nothing",
          "output limit" in str(final.get("error")) and "changed no files" in str(final.get("error"))
          and "32,000" in str(final.get("error")), repr(final.get("error")))
    final = run("length-wrote", f"echo '{start}'\necho hi > out.txt\necho '{finish('length')}'\nexit 0\n")
    check("opencode: the same stop with files written keeps its work and its ok",
          final.get("ok") is True, repr((final.get("ok"), final.get("error"))))
    final = run("stop-empty", f"echo '{start}'\necho '{finish('stop')}'\nexit 0\n")
    check("opencode: a step that ended 'stop' is not an output-limit failure",
          final.get("ok") is True, repr((final.get("ok"), final.get("error"))))


def t_zero_change_words():
    src = Path(_ROOT, "brain/mastercode/runtime/session.py").read_text()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_compare_delivery")
    ns: dict = {"json": json}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "session.py", "exec"), ns)
    content, _t, _l = ns["_compare_delivery"]({"per_harness": {
        "opencode": {"state": "done", "model": "glm", "elapsed_seconds": 508, "diff_bytes": 0},
        "codex": {"state": "done", "model": "gpt", "elapsed_seconds": 600, "diff_bytes": 50979}}})
    line = next((ln for ln in content.splitlines() if "(opencode)" in ln), "")
    check("delivery: a landed run with a recorded zero diff says it changed no files",
          "changed no files" in line, line)
    other = next((ln for ln in content.splitlines() if "(codex)" in ln), "")
    check("delivery: a run with changes is unchanged", "changed no files" not in other
          and "KB of changes" in other, other)
    from mastercode.runtime import tools as T
    ok, summary, per, done, failed = T._summarize_fan_out(
        {"session_id": "abc"}, {"opencode": {"state": "done", "diff_bytes": 0, "model": "glm"}})
    check("fan-out summary: a zero-diff landed run says it changed no files",
          "changed no files" in summary, summary)


# ── 5. usage semantics ──────────────────────────────────────────────────────
def t_usage(CM):
    def ev(**p):
        return {"kind": "usage", "payload": p}
    # antigravity: total == input + output; cache separate; thinking <= output.
    frames = [ev(input_tokens=23557, cache_read_tokens=0, output_tokens=1861,
                 thinking_tokens=1761, total_tokens=25418),
              ev(input_tokens=2257, cache_read_tokens=138911, output_tokens=1570,
                 thinking_tokens=611, total_tokens=3827),
              ev(input_tokens=397784, cache_read_tokens=1952625, output_tokens=109210,
                 thinking_tokens=44759, total_tokens=506994)]
    agg = CM.aggregate_usage_events(frames, "antigravity")
    check("antigravity: input is input + cache reads (the true context), not ~6x low",
          agg.get("input_tokens") == 397784 + 1952625
          and agg.get("cached_input_tokens") == 1952625, json.dumps(agg.get("frames")))
    check("antigravity: the total never sits below input + output",
          agg.get("total_tokens") == 397784 + 1952625 + 109210, repr(agg.get("total_tokens")))
    check("antigravity: thinking_tokens is reasoning, not unmapped",
          agg.get("reasoning_tokens") == 44759
          and "thinking_tokens" not in (agg["frames"][0].get("unmapped") or {}),
          json.dumps(agg["frames"][0]))
    agg = CM.aggregate_usage_events([ev(input_tokens=1, cache_read_tokens=0, output_tokens=1,
                                        thinking_tokens=0, total_tokens=2)], "antigravity-s2")
    check("antigravity: a sampled variant shares the rule", agg.get("input_tokens") == 1)
    # opencode, as parse_opencode normalizes step_finish.
    agg = CM.aggregate_usage_events([ev(input_tokens=16720, output_tokens=2, reasoning_tokens=31998,
                                        total_tokens=49744, cache_read_tokens=1024,
                                        cache_write_tokens=0, cost_usd=0.32898944)], "opencode")
    check("opencode: cache reads add to input; the vendor's larger total is kept",
          agg.get("input_tokens") == 17744 and agg.get("total_tokens") == 49744, json.dumps(agg["frames"]))
    # codex keeps the inclusive convention (cached is a subset of input).
    agg = CM.aggregate_usage_events([ev(input_tokens=12000, cached_input_tokens=9000,
                                        output_tokens=800)], "codex")
    check("codex: unchanged — cached stays a subset of input", agg.get("input_tokens") == 12000)


# ── 6. vendor cost ──────────────────────────────────────────────────────────
def t_cost(CM):
    class Conn:
        def __init__(self, sink):
            self.sink = sink

        async def execute(self, sql, *args):
            self.sink.append((sql, args))

        async def fetchrow(self, *a):
            return None

    class Acq:
        def __init__(self, c):
            self.c = c

        async def __aenter__(self):
            return self.c

        async def __aexit__(self, *a):
            return False

    class Pool:
        def __init__(self):
            self.sink = []

        def acquire(self):
            return Acq(Conn(self.sink))

        async def fetchrow(self, *a):
            return None

    def vendor_of(run_out):
        p = Pool()
        asyncio.run(CM.record_harness_usage(p, session_id="s", project_id=1, harness="opencode",
                                            run_out=run_out))
        ins = [a for s, a in p.sink if "INSERT INTO harness_usage" in s]
        return ins[0][14] if ins else "no insert"

    agg = CM.aggregate_usage_events([{"kind": "usage", "payload": {
        "input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "cost_usd": 0.32898944}}],
        "opencode")
    got = vendor_of({"ok": True, "usage_stream": agg})
    check("cost: a stream-reported cost reaches harness_usage.vendor_cost_microdollars",
          got == 328989, repr(got))
    got = vendor_of({"ok": True, "usage_stream": {"total_tokens": 12, "cost_usd": 0.0}})
    check("cost: no reported cost stays NULL, never 0", got is None, repr(got))
    got = vendor_of({"ok": True, "total_cost_usd": 0.5, "usage_stream": agg})
    check("cost: the SDK's own run total still wins when present", got == 500000, repr(got))

    from mastercode.runtime import tools as T

    class WPool:
        async def fetch(self, sql, *a):
            row = {"harness": "codex", "frames": 3, "tokens": 100, "input_tokens": 90,
                   "cached_tokens": 50, "vendor_cost_usd": 0}
            row2 = {"harness": "opencode", "frames": 2, "tokens": 50, "input_tokens": 40,
                    "cached_tokens": 0, "vendor_cost_usd": 0.33}
            # The query counts frames that carried a cost; answer it as the
            # table would for these rows.
            if "cost_frames" in sql:
                row["cost_frames"], row2["cost_frames"] = 0, 1
            return [row, row2]
    res = asyncio.run(T._exec_harness_usage_windows({}, WPool()))
    items = {i["harness"]: i for w in (res.get("result") or {}).get("windows", [])
             for i in w["harnesses"]}
    cx, oc = items.get("codex", {}), items.get("opencode", {})
    check("windows: a harness with no reported cost says not reported, not $0.00",
          cx.get("vendor_cost_usd") is None and "not reported" in str(cx.get("vendor_cost")),
          json.dumps(cx))
    check("windows: a reported cost is still a number",
          abs(float(oc.get("vendor_cost_usd") or 0) - 0.33) < 1e-9, json.dumps(oc))


# ── four gaps of the same class ─────────────────────────────────────────────
def t_cursor_report(EV):
    """Run 741's result frame repeats prose the card already shows: the first
    paragraph of each earlier span plus the WHOLE final message (1,566 chars
    there). Both views turn a done detail over 80 chars into a "report" step
    (run-steps.ts foldSteps, bench app.js), so the final message showed
    twice. Placeholder text; the shape is a real run's."""
    early = "Exploring the workspace and planning the build."
    final = "Here is what was delivered: " + "one file, one page, " * 6 + "done."
    usage = {"inputTokens": 1, "outputTokens": 1, "cacheReadTokens": 0, "cacheWriteTokens": 0}
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": early}]}, "timestamp_ms": 1},
        {"type": "tool_call", "subtype": "started", "call_id": "c1",
         "tool_call": {"lsToolCall": {"args": {"targetDirectory": "/w"}}}},
        {"type": "tool_call", "subtype": "completed", "call_id": "c1",
         "tool_call": {"lsToolCall": {"args": {"targetDirectory": "/w"},
                                      "result": {"success": {"files": ["a"]}}}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": final[:40]}]}, "timestamp_ms": 2},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": final[40:]}]}, "timestamp_ms": 3},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": final}]}},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": early + "\n\n\n" + final, "usage": usage},
    ]
    fr = _frames(EV, "cursor", [json.dumps(o) for o in lines])
    said = "".join(f["payload"].get("text", "") for f in fr
                   if f["kind"] == "text" and not f["payload"].get("raw"))
    reports = [f["payload"].get("detail") for f in fr if f["kind"] == "status"
               and f["payload"].get("state") == "done"
               and len(str(f["payload"].get("detail") or "").strip()) > 80]
    check("cursor: the result frame does not show the streamed prose a second time",
          not reports and final in said and said.count(final) == 1, json.dumps(reports)[:200])
    fr = _frames(EV, "cursor", [json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                            "result": early + " " + final, "usage": usage})])
    check("cursor: a run that streamed no prose keeps its result as the report",
          any(f["kind"] == "status" and f["payload"].get("detail") == early + " " + final
              for f in fr), json.dumps(fr)[:200])


def t_opencode_headroom(specs, tmp: Path):
    """opencode v1.18.35 compacts once a step's tokens reach
    context - maxOutputTokens(model, override) (function vn in the binary,
    when the model has no limit.input). An override equal to a catalog
    output limit that equals the context leaves no input headroom at all:
    qwen/qwen3.6-27b (262,144 / 262,140), openrouter/auto, bodybuilder in
    that binary's openrouter catalog."""
    stub = tmp / "opencode-models-headroom"
    rows = {"z-ai/full": {"limit": {"context": 262144, "output": 262140}},
            "z-ai/inp": {"limit": {"context": 400000, "input": 272000, "output": 128000}},
            "z-ai/small": {"limit": {"context": 50000, "output": 50000}}}
    listing = "".join(f"openrouter/{k}\n" + json.dumps({"id": k, **v}, indent=2) + "\n"
                      for k, v in rows.items())
    (tmp / "listing-headroom.txt").write_text(listing)
    stub.write_text("#!/bin/sh\n"
                    f'if [ "$1" = "models" ]; then cat "{tmp}/listing-headroom.txt"; exit 0; fi\n'
                    "exit 3\n")
    stub.chmod(0o755)
    spec = dataclasses.replace(specs.ALL_SPECS["opencode"], binary=stub)
    specs._OC_LIMITS.clear()
    env = {"PATH": os.environ.get("PATH", "")}

    def val(m):
        got = spec.run_env(spec, f"openrouter/{m}", env)
        return int(got.get("OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX", 0) or 0)
    full = val("z-ai/full")
    check("opencode: an output limit equal to the context never takes the input headroom",
          full and 262144 - full >= 262144 // 2, repr(full))
    check("opencode: a model with its own input limit runs at its full output limit",
          val("z-ai/inp") == 128000, repr(val("z-ai/inp")))
    small = val("z-ai/small")
    check("opencode: output == context on a small model runs at opencode's own 32,000, "
          "never at the context and never lower", small in (0, 32000), repr(small))


def t_windows_route(CM):
    """/api/compute/windows: harness_usage_event.cost_usd is NOT NULL DEFAULT 0
    and a frame stores 0 when no cost came, so a sum of 0 over frames that
    carried none is "not reported", never $0.00 (the Host tool's rule)."""
    class WPool:
        async def fetch(self, sql, *a):
            base = {"sessions": 1, "output_tokens": 1, "reasoning_tokens": 0,
                    "first_at": None, "last_at": None}
            row = {**base, "harness": "codex", "frames": 3, "tokens": 100, "input_tokens": 90,
                   "cached_tokens": 50, "vendor_cost_usd": 0}
            row2 = {**base, "harness": "opencode", "frames": 2, "tokens": 50, "input_tokens": 40,
                    "cached_tokens": 0, "vendor_cost_usd": 0.33}
            if "cost_frames" in sql:
                row["cost_frames"], row2["cost_frames"] = 0, 1
            return [row, row2]
    saved = CM.brain_service.get_pool
    CM.brain_service.get_pool = lambda: WPool()
    try:
        res = asyncio.run(CM.usage_windows(session_id=None, harness=None))
    finally:
        CM.brain_service.get_pool = saved
    items = {i["harness"]: i for w in res.get("windows", []) for i in w["harnesses"]}
    cx, oc = items.get("codex", {}), items.get("opencode", {})
    check("windows route: no reported cost is null + 'not reported', not 0",
          cx.get("vendor_cost_usd") is None and "not reported" in str(cx.get("vendor_cost")),
          json.dumps(cx, default=str))
    check("windows route: a reported cost is still a number",
          abs(float(oc.get("vendor_cost_usd") or 0) - 0.33) < 1e-9, json.dumps(oc, default=str))


def t_summary_route(CM):
    """/api/compute/summary: vendor_cost_microdollars is NULL when no vendor
    cost came. The stub answers as Postgres would for the SQL it is sent,
    from each run's own vendor_cost_microdollars: a coalesce(sum(...),0) over
    NULLs is 0, a bare sum is NULL, count(col) counts the non-NULL ones."""
    def _agg(sql, per_run):
        got = [v for v in per_run if v is not None]
        out = {"runs": len(per_run)}
        out["vendor_ud"] = (sum(got) if got else
                            (0 if "coalesce(sum(vendor_cost_microdollars),0)" in sql else None))
        if "count(vendor_cost_microdollars)" in sql:
            out["vendor_runs"] = len(got)
        return out

    def _run(by_harness):
        class SPool:
            async def fetch(self, sql, *a):
                base = {"accepted": 1, "tokens": 10, "compute_s": 1.0, "cost_ud": 1000,
                        "committed_files": 1}
                return [{**base, "harness": h, **_agg(sql, v)} for h, v in by_harness.items()]

            async def fetchrow(self, sql, *a):
                every = [x for v in by_harness.values() for x in v]
                return {"accepted": 2, "tokens": 20, "cost_ud": 2000, **_agg(sql, every)}
        saved = CM.brain_service.get_pool
        CM.brain_service.get_pool = lambda: SPool()
        try:
            res = asyncio.run(CM.summary(window="30d"))
        finally:
            CM.brain_service.get_pool = saved
        return {h["harness"]: h for h in res.get("harnesses", [])}, res.get("totals", {})

    items, tot = _run({"codex": [None, None], "opencode": [329000]})
    cx, oc = items.get("codex", {}), items.get("opencode", {})
    check("summary route: a harness with no reported vendor cost is null + 'not reported', not 0",
          cx.get("vendor_cost_usd") is None and "not reported" in str(cx.get("vendor_cost")),
          json.dumps(cx, default=str))
    check("summary route: a reported vendor cost is still a number",
          abs(float(oc.get("vendor_cost_usd") or 0) - 0.329) < 1e-9, json.dumps(oc, default=str))
    # A total over runs only some of which reported is the reported runs'
    # dollars, not the window's vendor charge: it says how many runs it covers.
    check("summary route: totals over some-null runs say they cover 1 of 3 runs, not the whole",
          abs(float(tot.get("vendor_cost_usd") or 0) - 0.329) < 1e-9
          and tot.get("vendor_cost_runs") == 1 and "1 of 3 runs" in str(tot.get("vendor_cost"))
          and "covers only" in str(tot.get("vendor_cost")),
          json.dumps(tot, default=str))
    items, tot = _run({"codex": [None, None], "opencode": [329000, None]})
    oc = items.get("opencode", {})
    check("summary route: a harness where only some runs reported says so",
          oc.get("vendor_cost_runs") == 1 and "1 of 2 runs" in str(oc.get("vendor_cost"))
          and "covers only" in str(oc.get("vendor_cost")), json.dumps(oc, default=str))
    items, tot = _run({"opencode": [329000, 1000]})
    oc = items.get("opencode", {})
    check("summary route: every run reported -> whole, no 'covers only' (the control)",
          oc.get("vendor_cost_runs") == 2 and "2 of 2 runs" in str(oc.get("vendor_cost"))
          and "covers only" not in str(oc.get("vendor_cost"))
          and "covers only" not in str(tot.get("vendor_cost")), json.dumps([oc, tot], default=str))
    items, tot = _run({"codex": [None, None], "opencode": [None]})
    check("summary route: totals with no reported vendor cost are null + 'not reported', not 0",
          tot.get("vendor_cost_usd") is None and "not reported" in str(tot.get("vendor_cost")),
          json.dumps(tot, default=str))


def t_projects_vendor_coverage(CM):
    """/api/compute/projects (and every _money_row lane): vendor_est_usd sums
    only the runs that reported a vendor cost, so the row says how many runs
    it covers. The stub answers each aggregate the SQL asks for from each
    run's own vendor_cost_microdollars, as Postgres would; every other alias
    of _HU_MONEY_COLS is 0."""
    import re as _re

    def _hu_row(sql, project_id, per_run):
        got = [v for v in per_run if v is not None]
        aliases = _re.findall(r"\bAS (\w+)", sql)
        row = {k: 0 for k in aliases}
        row.update({"project_id": project_id, "project_name": f"p{project_id}",
                    "runs": len(per_run), "tokens": None,
                    "vendor_ud": sum(got) if got else None})
        if "vendor_runs" in aliases:
            row["vendor_runs"] = len(got)
        return row

    class PPool:
        async def fetch(self, sql, *a):
            if "FROM harness_usage hu" in sql:
                return [_hu_row(sql, 1, [None, None]), _hu_row(sql, 2, [329000, None, None]),
                        _hu_row(sql, 3, [1000, 2000])]
            return []
    saved = CM.brain_service.get_pool
    CM.brain_service.get_pool = lambda: PPool()
    try:
        res = asyncio.run(CM.by_project(window="30d"))
    finally:
        CM.brain_service.get_pool = saved
    ps = {p["project_id"]: p for p in res.get("projects", [])}
    none, part, whole = ps.get(1, {}), ps.get(2, {}), ps.get(3, {})
    check("projects: a partial vendor estimate says it covers 1 of 3 runs",
          abs(float(part.get("vendor_est_usd") or 0) - 0.329) < 1e-9
          and part.get("vendor_est_runs") == 1
          and "1 of 3 runs" in str(part.get("vendor_est"))
          and "covers only" in str(part.get("vendor_est")),
          json.dumps(part, default=str)[:600])
    check("projects: every run reported -> 2 of 2, no 'covers only' (the control)",
          whole.get("vendor_est_runs") == 2 and "2 of 2 runs" in str(whole.get("vendor_est"))
          and "covers only" not in str(whole.get("vendor_est")),
          json.dumps(whole, default=str)[:600])
    check("projects: no run reported -> null, 0 runs, 'not reported'",
          none.get("vendor_est_usd") is None and none.get("vendor_est_runs") == 0
          and "not reported" in str(none.get("vendor_est")),
          json.dumps(none, default=str)[:600])


def t_timeseries_vendor_coverage(CM):
    """/api/compute/timeseries: a day's vendor_est_usd sums only that day's
    runs that reported a vendor cost, so each day says how many it covers.
    The stub aggregates each run's own value as Postgres would."""
    import datetime as _dt

    def _day(sql, day, per_run):
        got = [v for v in per_run if v is not None]
        row = {"day": _dt.date(2026, 10, day), "runs": len(per_run), "tokens": None,
               "compute_s": 0.0, "billed_ud": 0, "est_compute_ud": 0,
               "vendor_ud": sum(got) if got else None}
        if "count(vendor_cost_microdollars)" in sql:
            row["vendor_runs"] = len(got)
        return row

    class TPool:
        async def fetch(self, sql, *a):
            if "FROM harness_usage" in sql:
                return [_day(sql, 1, [None, None]), _day(sql, 2, [329000, None, None]),
                        _day(sql, 3, [1000, 2000])]
            return []
    saved = CM.brain_service.get_pool
    CM.brain_service.get_pool = lambda: TPool()
    try:
        res = asyncio.run(CM.timeseries(window="30d"))
    finally:
        CM.brain_service.get_pool = saved
    days = {r["day"]: r for r in res.get("harness_daily", [])}
    none, part, whole = days.get("2026-10-01", {}), days.get("2026-10-02", {}), days.get("2026-10-03", {})
    check("timeseries: a day where 1 of 3 runs reported says it covers 1 of 3",
          abs(float(part.get("vendor_est_usd") or 0) - 0.329) < 1e-9
          and part.get("vendor_est_runs") == 1 and "1 of 3 runs" in str(part.get("vendor_est"))
          and "covers only" in str(part.get("vendor_est")), json.dumps(part, default=str))
    check("timeseries: a day where every run reported is whole (the control)",
          whole.get("vendor_est_runs") == 2 and "covers only" not in str(whole.get("vendor_est")),
          json.dumps(whole, default=str))
    check("timeseries: a day with no reported vendor cost is null, 0 runs, 'not reported'",
          none.get("vendor_est_usd") is None and none.get("vendor_est_runs") == 0
          and "not reported" in str(none.get("vendor_est")), json.dumps(none, default=str))


def t_cursor_thinking_between_fragments(EV):
    """The whole assistant message repeats the TEXT fragments. Thinking
    streamed between them (as frames, or as a block inside a fragment) must
    not break the match, or the whole message is emitted a second time."""
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Built "}]},
         "timestamp_ms": 1},
        {"type": "thinking", "subtype": "delta", "text": "checking the file", "timestamp_ms": 2},
        {"type": "thinking", "subtype": "completed", "timestamp_ms": 3},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "still sure", "text": "still sure"},
            {"type": "text", "text": "the page"}]}, "timestamp_ms": 4},
        {"type": "thinking", "subtype": "delta", "text": "one more look", "timestamp_ms": 5},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "."}]},
         "timestamp_ms": 6},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Built the page."}]}},
    ]
    fr = _frames(EV, "cursor", [json.dumps(o) for o in lines])
    txt = [f["payload"] for f in fr if f["kind"] == "text"]
    check("cursor: interleaved thinking — the whole message is a message_end, not prose again",
          [t.get("delta") for t in txt] == [True, True, True]
          and "".join(t["text"] for t in txt) == "Built the page."
          and any(f["kind"] == "status" and f["payload"].get("state") == "message_end" for f in fr),
          json.dumps([(f["kind"], f["payload"]) for f in fr])[:600])
    th = [f["payload"] for f in fr if f["kind"] == "thinking"]
    check("cursor: interleaved thinking still reads as thinking",
          "".join(t["text"] for t in th) == "checking the fileone more look", json.dumps(th))


def t_seeded_output_limit(hc):
    """A seeded worktree reaches the pod without its seed commit, so the pod's
    diff is every seeded file as new and its output-limit check never fires.
    The host re-diffs against the seed (review._worktree_diff_host); the same
    verdict has to follow that diff."""
    from server.routes import review as rv
    fn = getattr(rv, "_output_limit_unfinished", None)
    check("seeded: the host has an output-limit verdict for its own diff", callable(fn), repr(fn))
    if not callable(fn):
        return
    check("seeded: host and pod read the same finish reasons",
          set(getattr(rv, "_OUTPUT_LIMIT_REASONS", ())) == set(hc._OUTPUT_LIMIT_REASONS))
    r = {"ok": True, "stop_reason": "length", "diff": ""}
    fn(r, "opencode")
    check("seeded: stopped at 'length' and no change against the seed is not ok",
          r["ok"] is False and "output limit" in str(r.get("error"))
          and "changed no files" in str(r.get("error")), repr(r))
    for r in ({"ok": True, "stop_reason": "length", "diff": "diff --git a/x b/x\n+y\n"},
              {"ok": True, "stop_reason": "stop", "diff": ""},
              {"ok": True, "diff": ""}):
        before = dict(r)
        fn(r, "opencode")
        check(f"seeded: left alone — {before}", r == before, repr(r))
    r = {"ok": False, "stop_reason": "length", "diff": "", "error": "its own reason"}
    fn(r, "opencode")
    check("seeded: a run that already failed keeps its own reason", r["error"] == "its own reason")
    # Both host re-diff sites apply it right after replacing the pod's diff.
    src = Path(_ROOT, "server/routes/review.py").read_text()
    sites = [m.start() for m in re.finditer(
        r'\[\s*"diff"\s*\]\s*=\s*await asyncio\.to_thread\(_worktree_diff_host, (worktree|worktrees\[vid\])\)', src)]
    calls = [s for s in sites if "_output_limit_unfinished(" in src[s:s + 400]]
    check("seeded: the single build and the fan-out both judge the host's diff",
          len(sites) >= 2 and len(calls) == len(sites), f"{len(calls)} of {len(sites)}")


def inner() -> int:
    sys.path[:0] = [_ROOT, os.path.join(_ROOT, "brain"), os.path.join(_ROOT, "cli"),
                    os.path.join(_ROOT, "system-apps")]
    from _harness_runtime import events as EV
    from _harness_runtime import harness_core as hc
    from _harness_runtime import specs
    from server.routes import compute as CM
    tmp = Path(tempfile.mkdtemp(prefix="run-truth-"))
    t_claude_code()
    t_cursor(EV, CM)
    t_grok(EV, CM)
    t_opencode_env(specs, tmp)
    t_opencode_length(hc, specs, tmp)
    t_zero_change_words()
    t_usage(CM)
    t_cost(CM)
    t_cursor_report(EV)
    t_opencode_headroom(specs, tmp)
    t_windows_route(CM)
    t_summary_route(CM)
    t_projects_vendor_coverage(CM)
    t_timeseries_vendor_coverage(CM)
    t_cursor_thinking_between_fragments(EV)
    t_seeded_output_limit(hc)
    print()
    if FAILS:
        print(f"FAIL ({len(FAILS)}): {FAILS}")
        return 1
    print("all green")
    return 0


def main() -> int:
    if _in_private_pid_namespace():
        return inner()
    import shutil
    init_dir = Path(tempfile.mkdtemp(prefix="podinit-"))
    tini = init_dir / "tini"
    shutil.copy2(shutil.which("sh") or "/bin/sh", tini)
    cmd = ["unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc",
           str(tini), "-c", f'"{sys.executable}" "{os.path.abspath(__file__)}"; echo "rc=$?"']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           env={**os.environ, "APP_ROOT": _ROOT})
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"FAIL: no private PID namespace here ({e!r}); this gate sends real "
              "signals and will not run on the host.", file=sys.stderr)
        return 2
    m = re.search(r"rc=(\d+)\s*\Z", r.stdout)
    print(r.stdout[:m.start()].rstrip() if m else (r.stdout + r.stderr)[-3000:])
    if not m:
        return 2
    if m.group(1) != "0":
        print(r.stderr[-3000:], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
