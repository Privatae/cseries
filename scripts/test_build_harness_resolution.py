#!/usr/bin/env python3
"""
Build harness resolution gate (smoke).

The chat `build` tool used to pick its coding harness as: the user's
Orchestrator → Coding selection ∩ connected → else `connected[0]` → else a
literal "claude-code". Two silent swaps in a row. On a Host the selection was
codex/copilot/grok (none connected), so every build ran on claude-code — a
harness the user never picked, whose credential file was a HOLLOW OAuth blob
(empty accessToken/refreshToken) that presence-only checks called "connected".
36 builds, 36 failures in a row, and the host told the user the build
was "running on the Kimi harness".

Gates (exit 0 = all pass; exit 1 + [build-harness] FAIL lines):
  RESOLVER (server/routes/review.py:resolve_build_harness — driven with a
  stubbed default-engine tick + connected set, no pool, no pod;
  Orchestrator → Coding Engines, the Default tick replaces the old selection
  and ladder reads):
   1. explicit + connected → that harness
   2. explicit + NOT connected → refusal naming the connected set
   3. default ticked + connected → the default
   4. default ticked, NOT connected, others connected → refusal naming the
      default and the connected set (the task-282 shape) — never a silent swap
   5. no default, exactly one connected → that one
   6. no default, several connected → refusal listing them
   7. nothing connected → refusal
   8. run_single_harness_build turns a refusal into {ok: False, error} and
      never reaches the pool (no wake)
  CREDENTIAL TRUTH (server/routes/harnesses.py:_creds_present):
   9. a hollow claudeAiOauth blob is NOT present (not connected)
  10. a claudeAiOauth blob with a refreshToken only IS present (pod refreshes)
  11. a claudeAiOauth blob with an accessToken IS present
  12. a bare api key stays present (presence-only, unchanged)
  13. an empty file stays not present
  RECORD SHAPE:
  14. the `build` schema exposes `harness`
  15. _factory_build_task_start stamps harness on the row's result

Runs against the image (/app) or a source tree (APP_ROOT=<repo>).
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"[build-harness] {'ok  ' if ok else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    sys.path.insert(0, app_root)
    sys.path.insert(0, os.path.join(app_root, "brain"))
    # The stub engine must be OFF for these gates — it short-circuits the
    # resolver by design (smoke's hermetic build).
    os.environ.pop("CSERIES_SMOKE", None)
    os.environ.pop("FACTORY_ENGINE", None)

    import server.routes.review as rv

    def _drive(default, connected, explicit=None, settings=None, model=None):
        async def _def(): return default
        async def _conn(): return list(connected)
        # Saved per-engine overrides (Orchestrator → Coding Engines). The
        # model gate reads them; the pre-existing gates below
        # give kimi a saved model so they keep exercising the engine choice
        # alone — the gate's own cases pass `settings` explicitly.
        _settings = {"kimi": {"model": "k3"}} if settings is None else settings
        async def _cfg(): return dict(_settings)
        rv._default_coding_harness = _def
        rv._installed_harnesses = _conn
        rv._coding_harness_settings = _cfg
        # A tree without the resolver must FAIL, not crash.
        _resolve = getattr(rv, "resolve_build_target", None)
        if _resolve is None:
            return "<no resolve_build_target in this tree>", None
        h, _m, why = asyncio.run(_resolve(explicit, model=model))
        return h, why

    h, why = _drive("codex", ["kimi", "cursor"], explicit="kimi")
    check("explicit + connected → that harness", h == "kimi" and why is None, f"{h!r} {why!r}")

    h, why = _drive("codex", ["kimi", "cursor"], explicit="grok")
    check("explicit + not connected → refusal naming the connected set",
          h is None and bool(why) and "grok" in why and "kimi" in why and "cursor" in why,
          f"{h!r} {why!r}")

    h, why = _drive("cursor", ["kimi", "cursor"])
    check("default ticked + connected → the default engine",
          h == "cursor" and why is None, f"{h!r} {why!r}")

    # The task-282 shape: the ticked default has no key; claude-code + kimi do.
    h, why = _drive("codex", ["claude-code", "cursor", "kimi"])
    check("default ticked but NOT connected → REFUSAL (never a silent swap)",
          h is None and why is not None, f"{h!r} {why!r}")
    check("…the refusal names the default and the connected set",
          bool(why) and "codex" in why and "kimi" in why and "claude-code" in why
          and "harness=" in why, f"{why!r}")

    h, why = _drive(None, ["kimi"])
    check("no default + exactly one connected → that one",
          h == "kimi" and why is None, f"{h!r} {why!r}")

    h, why = _drive(None, ["kimi", "cursor"])
    check("no default + several connected → refusal listing them",
          h is None and bool(why) and "kimi" in why and "cursor" in why
          and "Coding Engines" in why, f"{h!r} {why!r}")

    h, why = _drive("codex", [])
    check("nothing connected → refusal",
          h is None and bool(why) and "No coding engine is connected" in why, f"{h!r} {why!r}")

    # ── The model gate (project 1) ──────────────────────────────
    # kimi ran nothing without a model: four pod wakes (three "Server
    # disconnected", then "kimi has no model configured" from inside the pod)
    # were each delivered as a failed build. The resolver now refuses such an
    # engine BEFORE a wake unless it has a saved model, the build names one,
    # or its CLI/SDK picks its own.
    h, why = _drive(None, ["kimi"], settings={"kimi": {"model": "k3"}})
    check("model gate: engine with a saved model → ok", h == "kimi" and why is None, f"{h!r} {why!r}")
    h, why = _drive(None, ["codex"], settings={})
    check("model gate: engine with a builtin default (codex) and no saved model → ok",
          h == "codex" and why is None, f"{h!r} {why!r}")
    h, why = _drive(None, ["kimi"], settings={})
    check("model gate: engine with neither → refusal naming it and where to fix",
          h is None and bool(why) and "'kimi'" in why and "no model configured" in why
          and "Coding Engines" in why, f"{h!r} {why!r}")
    h, why = _drive("kimi", ["kimi", "codex"], settings={})
    check("model gate: applies to the DEFAULT tick too (kimi default, no model → refusal)",
          h is None and bool(why) and "'kimi'" in why, f"{h!r} {why!r}")
    h, why = _drive(None, ["kimi"], settings={}, model="k3")
    check("model gate: a model named on the build passes the engine through",
          h == "kimi" and why is None, f"{h!r} {why!r}")
    h, why = _drive(None, ["opencode"], settings={})
    check("model gate: opencode is under the same contract (no default model)",
          h is None and bool(why) and "'opencode'" in why, f"{h!r} {why!r}")
    # The set is a MIRROR of the specs contract: an engine is in it iff its
    # spec declares `default_model=<ENV> or None`. Read off the source so the
    # mirror cannot drift silently.
    import re as _re
    from server.routes.inference import HARNESS_MODEL_REQUIRED
    # SOURCE-ONLY CHECK. smoke runs this whole gate IN-IMAGE (docker exec
    # /app/scripts/...), and `/app/system-apps` is EMPTY there by design: system
    # apps ship as signed .cseries bundles under /app/native-bundles, and
    # _harness_runtime rides the harness sidecar images rather than the kernel.
    # Reading the file unconditionally is what red-failed a smoke run with
    # FileNotFoundError, on a gate that had nothing to say about the image. So:
    # run the mirror where the source exists, and say plainly where it does not.
    # The gate is ALSO in gates-container-free.txt, so this branch really does
    # execute on every pre-merge run and every smoke — skipping it in-image
    # costs no coverage, it moves it to the lane that can do the work.
    _specs_path = Path(app_root, "system-apps/_harness_runtime/specs.py")
    if not _specs_path.is_file():
        print(f"  skip HARNESS_MODEL_REQUIRED mirror — no {_specs_path} "
              "(in-image run; the container-free lane covers it)")
    else:
        _specs_src = _specs_path.read_text()
        _no_default: set = set()
        for m in _re.finditer(r'id="([a-z\-]+)".*?(?=\n\w|\Z)', _specs_src, _re.S):
            block = m.group(0)
            if _re.search(r"default_model=\w+ or None", block):
                _no_default.add(m.group(1))
        check("HARNESS_MODEL_REQUIRED mirrors the specs that declare no default model",
              set(HARNESS_MODEL_REQUIRED) == _no_default,
              f"table={sorted(HARNESS_MODEL_REQUIRED)} specs={sorted(_no_default)}")

    # 8. The single-harness build path refuses BEFORE the pool.
    woke = []

    async def _no_wake(*a, **k):
        woke.append(a)
        return {"ok": True}
    rv._harness_bridge = _no_wake

    async def _def2(): return "codex"

    async def _conn2(): return ["kimi", "cursor"]
    rv._default_coding_harness = _def2
    rv._installed_harnesses = _conn2
    with tempfile.TemporaryDirectory() as td:
        rv.PROJECTS_ROOT = Path(td)
        out = asyncio.run(rv.run_single_harness_build(project_id=1, prompt="x"))
    check("run_single_harness_build refuses with {ok: False, error}",
          isinstance(out, dict) and out.get("ok") is False and "codex" in str(out.get("error")),
          f"{out}")
    check("…and never wakes a pod", woke == [], f"woke={woke}")

    # 8b. Build runs are bench sessions; failed worktrees are ARCHIVED
    # (the failure envelope promised
    # "partial work KEPT at <worktree>" and the next run's _prepare_worktree
    # deleted that exact dir before the CLI could look; the host pasted the
    # dead path into the continue prompt). Stub engine — no pod, no DB.
    os.environ["FACTORY_ENGINE"] = "stub"
    os.environ["CSERIES_SMOKE"] = "1"

    async def _conn_kimi(): return ["kimi"]
    rv._installed_harnesses = _conn_kimi
    try:
        with tempfile.TemporaryDirectory() as td:
            rv.PROJECTS_ROOT = Path(td) / "projects"
            rv.SESSIONS_ROOT = Path(td) / "sessions"
            rv.PROJECTS_ROOT.mkdir(parents=True)
            rv.SESSIONS_ROOT.mkdir(parents=True)

            out = asyncio.run(rv.run_single_harness_build(
                project_id=42, prompt="build it", harness="kimi",
                billing_key="factory-build-9999"))
            sdir = rv.SESSIONS_ROOT / "factory-build-9999"
            m = json.loads((sdir / "manifest.json").read_text())
            st = json.loads((sdir / "per_harness_state.json").read_text())
            check("build creates a bench session (manifest: kind/project/completed)",
                  out.get("ok") is True
                  and out.get("session_id") == "factory-build-9999"
                  and m.get("kind") == "build" and m.get("project_id") == 42
                  and m.get("harnesses") == ["kimi"]
                  and bool(m.get("completed_at")), f"out={out} m={m}")
            check("build session state lands terminal 'done'",
                  st.get("kimi", {}).get("state") == "done", f"st={st}")
            check("successful build archives nothing",
                  out.get("failed_worktree") is None
                  and not (sdir / "failed-worktree").exists())

            # FAILED run: seed the project (so the worktree holds real files),
            # drive the stub's refusal (absolute-path prompt), and require the
            # partial work to survive in the session dir with a true count.
            (rv.PROJECTS_ROOT / "43").mkdir(parents=True)
            (rv.PROJECTS_ROOT / "43" / "seed.html").write_text("<p>seed</p>")
            out = asyncio.run(rv.run_single_harness_build(
                project_id=43, prompt="resume /var/lib/cseries/projects/43/kimi",
                harness="kimi", billing_key="factory-build-9998"))
            sdir = rv.SESSIONS_ROOT / "factory-build-9998"
            st = json.loads((sdir / "per_harness_state.json").read_text())
            kept = out.get("failed_worktree") or {}
            # Assert the PROMISE, not the mechanism: the failure envelope names
            # a path, and that path must really hold the partial work. Where it
            # lives is an implementation detail — it was a copy under
            # failed-worktree/ while worktrees hung off project_dir and were
            # nuked per run; now the run-scoped worktree is itself
            # durable and gets named directly. Both satisfy the user-visible
            # claim, so the gate checks the claim.
            kept_path = Path(kept.get("path", "")) if kept.get("path") else None
            check("failed build reports partial work at a path that really holds it",
                  out.get("ok") is False and kept.get("files", 0) >= 1
                  and kept_path is not None
                  and (kept_path / "seed.html").exists()
                  and st.get("kimi", {}).get("state") == "failed",
                  f"out={out} st={st}")
            # It survives the NEXT run — the exact 603→604 failure shape.
            asyncio.run(rv.run_single_harness_build(
                project_id=43, prompt="build it again", harness="kimi",
                billing_key="factory-build-9997"))
            check("partial work survives the next run on the same harness",
                  kept_path is not None and (kept_path / "seed.html").exists(),
                  f"gone from {kept_path}")

            # The envelope the HOST sees states only durable truths: archived
            # path + count, fresh-seed semantics, no bare worktree promise.
            import mastercode.runtime.tools as t2

            class _P:
                async def fetchval(self, *a, **k): return 1
            env = asyncio.run(t2._pool_build(
                entity_id=1, pool=_P(), project_id=43,
                prompt="resume /var/lib/cseries/projects/43/kimi",
                research_id=None, timeout_s=60,
                billing_key="factory-build-9996"))
            msg = str(env.get("result"))
            check("failure envelope: archived truth + fresh-seed semantics, "
                  "no 'KEPT at <worktree>' promise",
                  env.get("status") == "error"
                  and "archived at" in msg and "not" in msg
                  and "auto-seeded" in msg and "KEPT at" not in msg
                  and env.get("session_id") == "factory-build-9996", f"{env}")
    finally:
        os.environ.pop("FACTORY_ENGINE", None)
        os.environ.pop("CSERIES_SMOKE", None)

    # 9-13. Credential truth.
    from server.routes.harnesses import _creds_present
    with tempfile.TemporaryDirectory() as td:
        def _f(name, content):
            p = Path(td) / name
            p.write_text(content)
            return {"creds_path": str(p)}
        hollow = _f("hollow.json", json.dumps({"claudeAiOauth": {
            "accessToken": "", "refreshToken": "", "expiresAt": 0,
            "scopes": ["user:inference"], "subscriptionType": "max"}}))
        check("hollow claudeAiOauth blob is NOT present", _creds_present(hollow) is False)
        refresh_only = _f("refresh.json", json.dumps({"claudeAiOauth": {
            "accessToken": "", "refreshToken": "rt-1", "expiresAt": 0}}))
        check("claudeAiOauth with refreshToken only IS present (pod refreshes)",
              _creds_present(refresh_only) is True)
        access = _f("access.json", json.dumps({"claudeAiOauth": {
            "accessToken": "at-1", "refreshToken": "", "expiresAt": 9999999999999}}))
        check("claudeAiOauth with accessToken IS present", _creds_present(access) is True)
        bare = _f("api_key", "sk-kimi-abcdef")
        check("bare api key stays present", _creds_present(bare) is True)
        empty = _f("empty", "")
        check("empty file stays not present", _creds_present(empty) is False)

    # 14. Schema exposes harness.
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS
    props = ((PUBLIC_TOOL_SCHEMAS["build"]["function"].get("parameters") or {})
             .get("properties") or {})
    check("`build` schema exposes `harness`", "harness" in props, f"{sorted(props)}")

    # 15. The registry row carries harness from OPEN.
    import mastercode.runtime.tools as t
    inserted = {}

    class _Pool:
        async def fetchrow(self, sql, *args):
            inserted["sql"] = sql
            inserted["args"] = args
            return {"task_id": 42}
    tid = asyncio.run(t._factory_build_task_start(
        _Pool(), 3, title="t", intent="i", project_id=211, harness="kimi"))
    res = {}
    try:
        res = json.loads(inserted["args"][-1])
    except Exception:
        pass
    check("_factory_build_task_start stamps harness on the row",
          tid == 42 and res.get("harness") == "kimi", f"{inserted.get('args')}")

    # ── The reason a failed run reports ──
    # The pod returns {ok:false, exit_code, stderr} with NO `error` when the
    # CLI dies mid-run; the reason must come off stderr, never "harness run
    # failed". A tree without the helper FAILS, not crashes.
    _reason = getattr(rv, "_run_failure_reason", None)
    check("review._run_failure_reason exists", _reason is not None)
    if _reason is not None:
        r1 = _reason({"ok": False, "exit_code": 1, "error": None,
                      "stderr_tail": ("Let me build a single\n"
                                      "error: failed to run prompt: provider.connection_error: terminated\n"
                                      "See log: /var/lib/cseries/projects/.kimi-home/.kimi-code/logs/kimi-code.log\n")})
        check("stderr reason surfaces the CLI's own error line",
              "provider.connection_error: terminated" in r1 and r1.startswith("harness exited 1"), r1)
        r2 = _reason({"ok": False, "error": "codex not connected", "stderr_tail": "junk"})
        check("an explicit harness error wins over stderr", r2 == "codex not connected", r2)
        r3 = _reason({"ok": False, "exit_code": 137})
        check("no stderr → the exit code is still named", "137" in r3, r3)
        r4 = _reason({"ok": False, "exit_code": 2, "stderr_tail": "line one\nline two\nline three"})
        check("no marked line → the last stderr lines", "line three" in r4, r4)
        # One fan-out run: cursor-agent refused a model
        # with ONE long line — the reason at the front, then every valid id.
        # The [-400:] slice kept the list's tail and lost the words.
        r5 = _reason({"ok": False, "exit_code": 1,
                      "stderr_tail": "Error: unknown model 'gpt-5.6-luna-pro'. Available models: "
                                     + ", ".join(f"model-{i}" for i in range(400)) + "\n"})
        check("a long error line keeps its HEAD (the reason), clipped, not the list's tail",
              r5.startswith("harness exited 1: Error: unknown model 'gpt-5.6-luna-pro'")
              and " … " in r5 and len(r5) < 460, r5[:120])
        # Same fan-out: agy 1.1.17 exited 1 with an EMPTY stderr and its reason
        # on STDOUT as the result event — this read "harness exited 1 with no
        # output". The vendor's own words from the transcript tail now win.
        _agy_tr = ('{"event":"init","conversation_id":"f4bd"}\n'
                   '{"event":"step_update","step_update":{"step_type":"error_message","state":"DONE"}}\n'
                   '{"event":"result","result":{"conversation_id":"f4bd","status":"ERROR","response":"",'
                   '"error":"There was a network issue connecting to the server, please try again.",'
                   '"duration_seconds":76.9,"num_turns":1}}\n')
        r6 = _reason({"ok": False, "exit_code": 1, "stderr": "", "transcript": _agy_tr})
        check("empty stderr → the vendor's stdout result.error is the reason",
              r6 == "harness exited 1: There was a network issue connecting to the server, please try again.", r6)
        _grok_tr = ('{"type":"thought","data":"The"}\n'
                    '{"type":"error","message":"Internal error: {\\n  \\"message\\": \\"reqwest error stream: '
                    'Transport error: error decoding response body\\",\\n  \\"promptUsage\\": {}\\n}"}\n')
        r7 = _reason({"ok": False, "exit_code": 1, "stderr": "", "transcript": _grok_tr})
        check("empty stderr → a {type:error} stdout frame is the reason (whitespace folded)",
              r7.startswith("harness exited 1: Internal error:") and "error decoding response body" in r7
              and "\n" not in r7, r7)
        r8 = _reason({"ok": False, "exit_code": 1, "stderr": "",
                      "transcript": '{"type":"text","data":"all fine"}\n{"type":"end","stopReason":"EndTurn"}\n'})
        check("empty stderr + a clean stdout → still 'with no output' (no invented reason)",
              r8 == "harness exited 1 with no output", r8)
        r9 = _reason({"ok": False, "exit_code": 1, "stderr": "[… stderr elided …]\nerror: real line\n"})
        check("the stderr elision marker is never picked as the reason",
              r9 == "harness exited 1: error: real line", r9)
        # kimi streams reasoning prose to stderr and
        # that prose says "failed"/"timed out" freely. A long marked prose
        # line BEFORE the real failure line must never crowd the last line
        # out — the budget is spent from the END.
        _prose = ("Let me think about how the fetch should behave when the request failed "
                  "or timed out; the error path should " * 6).strip()
        r10 = _reason({"ok": False, "exit_code": 1,
                       "stderr": _prose + "\n" + _prose + "\n"
                                 "error: failed to run prompt: provider.connection_error: terminated\n"})
        check("a long marked prose line before the real failure line: the LAST line survives",
              r10.endswith("provider.connection_error: terminated") and len(r10) <= 420, r10[-120:])
        r11 = _reason({"ok": False, "exit_code": -9, "stderr": "",
                       "transcript": '{"type":"error","message":"stream disconnected, retrying"}\n'
                                     '{"type":"text","data":"…continuing"}\n'})
        check("a signal kill (rc<0) never names a stale transcript error as the cause",
              r11 == "harness exited -9 with no output", r11)

    # ── final_report: the harness's own closing words become the summary ─────
    # (one build's summary was transcript[:4000] = a version header +
    # numbered file dump; the host said "the log cuts off mid-file" about a
    # clean run. The summary lane now prefers the trailing text frames after
    # the last tool/file activity — the CLI's final assistant message.)
    import json as _json
    import tempfile as _tf
    from pathlib import Path as _P
    from server.routes.review import _final_text_from_frames

    def _frames_file(dirp, label, frames):
        p = _P(dirp) / f"{label}.events.jsonl"
        p.write_text("\n".join(_json.dumps(f) for f in frames) + "\n")

    with _tf.TemporaryDirectory() as _td:
        _frames_file(_td, "kimi", [
            {"seq": 0, "kind": "status", "payload": {"state": "system.version", "detail": "0.36.1"}},
            {"seq": 1, "kind": "text", "payload": {"text": "planning prose"}},
            {"seq": 2, "kind": "tool_call", "payload": {"tool": "Read", "args": {}}},
            {"seq": 3, "kind": "tool_result", "payload": {"tool": "", "result": "1\t<html>..."}},
            {"seq": 4, "kind": "file_edit", "payload": {"path": "index.html"}},
            {"seq": 5, "kind": "text", "payload": {"text": "Done. Two fixes:"}},
            {"seq": 6, "kind": "thinking", "payload": {"text": "hm"}},
            {"seq": 7, "kind": "text", "payload": {"text": "subscriptions total corrected."}},
            {"seq": 8, "kind": "status", "payload": {"state": "session.resume_hint", "detail": "kimi -r x"}},
        ])
        rep = _final_text_from_frames(_P(_td), "kimi")
        check("final report = trailing text AFTER the last tool/file frame",
              rep.startswith("Done. Two fixes:") and "subscriptions total" in rep
              and "planning prose" not in rep, rep)
        check("status/thinking frames never clear the tail",
              "corrected" in rep)
        _frames_file(_td, "codex", [
            {"seq": 0, "kind": "tool_call", "payload": {"tool": "x"}},
        ])
        check("no trailing text -> empty (caller falls back to transcript)",
              _final_text_from_frames(_P(_td), "codex") == "")
        check("missing frame log -> empty, never raises",
              _final_text_from_frames(_P(_td), "nope") == ""
              and _final_text_from_frames(None, "kimi") == "")

    # ── build takes the model; fan-out gets the delivery lane ──
    # 07:40Z, "use opus 5 to create the same demo app kimi did": the
    # `build` tool had no `model` argument, so the host reached for
    # review.fan_out(harness_model=…) — the one path with no delivery lane.
    # claude-code ran clean for 203 s and the thread never
    # heard of it. Gates: build carries model/effort end to end; a chat
    # fan_out opens a registry row with an origin back-ref, a detached
    # watcher finalizes it when the session lands, and _deliver is woken.
    check("`build` schema exposes `model` and `effort`",
          "model" in props and "effort" in props
          and set((props.get("effort") or {}).get("enum") or [])
          == {"low", "medium", "high", "xhigh", "max", "ultra"},
          f"{sorted(props)}")
    b_desc = PUBLIC_TOOL_SCHEMAS["build"]["function"].get("description") or ""
    check("build description promises the in-thread report, and a compare is `variants` on run",
          "I report here when it lands" in b_desc and "variants" in props
          and "review.fan_out" not in PUBLIC_TOOL_SCHEMAS, b_desc[:120])

    inserted.clear()
    tid = asyncio.run(t._factory_build_task_start(
        _Pool(), 3, title="t", intent="i", project_id=211, harness="claude-code",
        model="claude-opus-5"))
    res = {}
    try:
        res = json.loads(inserted["args"][-1])
    except Exception:
        pass
    check("_factory_build_task_start stamps the requested model on the row",
          tid == 42 and res.get("model") == "claude-opus-5" and res.get("harness") == "claude-code",
          f"{inserted.get('args')}")

    # _exec_build(action='continue', harness, model, effort) → the detached
    # build receives the EXPLICIT model (beating the ladder rung) and effort.
    captured = {}

    async def _fake_run_phase_build(**kw):
        captured.update(kw)

    async def _resolve_rung(explicit, model=None):
        return (explicit or "kimi"), "k3-rung-model", None

    class _BuildPool:
        async def fetchval(self, sql, *a):
            if "FROM projects" in sql:
                return 1
            if "task_type='factory_build'" in sql:
                return None           # no build in flight
            if "FROM project_phases" in sql:
                return 2              # planned
            return None

        async def fetchrow(self, sql, *a):
            return {"task_id": 77}

    _orig_rpb = t._run_phase_build
    _orig_resolve = rv.resolve_build_target
    t._run_phase_build = _fake_run_phase_build
    rv.resolve_build_target = _resolve_rung
    try:
        out = asyncio.run(t._exec_build(
            {"action": "continue", "project_id": 9207, "message": "same app",
             "harness": "claude-code", "model": "claude-opus-5", "effort": "high"},
            3, _BuildPool(), "sess-1", "project:9207"))
        # Let the (fake) detached task run.
        asyncio.run(asyncio.sleep(0))
    finally:
        t._run_phase_build = _orig_rpb
        rv.resolve_build_target = _orig_resolve
    check("build(model=, effort=) — explicit model beats the ladder rung, effort rides along",
          out.get("status") == "ok"
          and captured.get("model") == "claude-opus-5" and captured.get("effort") == "high"
          and captured.get("harness") == "claude-code"
          and (out.get("result") or {}).get("model") == "claude-opus-5",
          f"out={out} captured={ {k: captured.get(k) for k in ('model', 'effort', 'harness')} }")
    out = asyncio.run(t._exec_build(
        {"action": "continue", "project_id": 9207, "message": "x", "effort": "extreme"},
        3, _BuildPool(), "sess-1", "project:9207"))
    check("build rejects an unknown effort before touching anything",
          out.get("status") == "error" and "effort" in str(out.get("result")), f"{out}")

    # Effort is checked against the ENGINE'S own set: the union of every
    # engine's values passes the door, then
    # the resolved engine narrows it — codex takes xhigh, kimi does not take
    # medium. A coding protocol step's effort (validated against the same
    # lists) is therefore honoured or refused by name, never dropped.
    captured.clear()
    t._run_phase_build = _fake_run_phase_build
    rv.resolve_build_target = _resolve_rung
    try:
        out = asyncio.run(t._exec_build(
            {"action": "continue", "project_id": 9207, "message": "x",
             "harness": "codex", "effort": "xhigh"},
            3, _BuildPool(), "sess-1", "project:9207"))
        asyncio.run(asyncio.sleep(0))
        check("build accepts an effort the engine takes beyond the classic four (codex xhigh)",
              out.get("status") == "ok" and captured.get("effort") == "xhigh", f"{out}")
        out = asyncio.run(t._exec_build(
            {"action": "continue", "project_id": 9207, "message": "x",
             "harness": "kimi", "effort": "medium"},
            3, _BuildPool(), "sess-1", "project:9207"))
        check("build refuses an effort THIS engine does not take, by name (kimi medium)",
              out.get("status") == "error" and "kimi" in str(out.get("result")), f"{out}")
    finally:
        t._run_phase_build = _orig_rpb
        rv.resolve_build_target = _orig_resolve

    # _pool_build threads effort through to run_single_harness_build (the
    # saved-override precedence itself is three lines at its call site:
    # explicit wins, saved fills a None — same rule as model).
    import inspect as _inspect
    check("run_single_harness_build accepts `effort`",
          "effort" in _inspect.signature(rv.run_single_harness_build).parameters)
    seen_kw = {}

    async def _capture_rshb(**kw):
        seen_kw.update(kw)
        return {"ok": True, "transcript": "", "changed_files": [], "model": kw.get("model")}
    _orig_rshb = rv.run_single_harness_build
    rv.run_single_harness_build = _capture_rshb
    try:
        class _P2:
            async def fetchval(self, *a, **k): return 1
        env = asyncio.run(t._pool_build(
            entity_id=1, pool=_P2(), project_id=43, prompt="p", research_id=None,
            timeout_s=60, harness="claude-code", model="claude-opus-5", effort="max",
            billing_key="factory-build-1"))
    finally:
        rv.run_single_harness_build = _orig_rshb
    check("_pool_build passes harness/model/effort to run_single_harness_build",
          seen_kw.get("harness") == "claude-code" and seen_kw.get("model") == "claude-opus-5"
          and seen_kw.get("effort") == "max" and env.get("status") == "ok",
          f"{ {k: seen_kw.get(k) for k in ('harness', 'model', 'effort')} } {env.get('status')}")

    # Fan-out delivery lane: row + watcher + deliver.
    rows = {}
    delivered = []

    class _FanPool:
        async def fetchrow(self, sql, *args):
            rows["insert_sql"] = sql
            rows["insert"] = json.loads(args[-1])
            rows["title"] = args[1]
            return {"task_id": 501}

        async def execute(self, sql, *args):
            rows["finish_sql"] = sql
            rows["finish_args"] = args
            return "UPDATE 1"

    class _Disp:
        async def _deliver(self, task_id):
            delivered.append(task_id)

    async def _fake_fan_out(req):
        vs = list(req.variants or [])
        return {"ok": True, "session_id": "abc123def456", "status": "queued",
                "harnesses_dispatched": list(dict.fromkeys(v["harness"] for v in vs)),
                "variants": [{"vid": v["harness"], "harness": v["harness"],
                              "model": v.get("model"), "effort": None} for v in vs],
                "skipped": []}

    async def _fake_await(sid, **k):
        return {"session_id": sid, "harnesses": ["claude-code", "kimi"],
                "completed_at": 1.0,
                "results": {"claude-code": {"ok": True, "changed_files": ["index.html"]},
                            "kimi": {"ok": False, "error": "provider.connection_error"}}}

    _orig_fan_out, _orig_await = rv.fan_out, getattr(rv, "await_session", None)
    _orig_state, _orig_disp = rv._read_session_state, t._get_runtime_dispatch
    check("review.await_session exists", _orig_await is not None)
    rv.fan_out = _fake_fan_out
    rv.await_session = _fake_await
    rv._read_session_state = lambda sd: {
        "claude-code": {"state": "done", "model": "claude-opus-5", "elapsed_seconds": 203.2, "diff_bytes": 27638},
        "kimi": {"state": "failed", "error": "provider.connection_error", "model": "k3"}}
    t._get_runtime_dispatch = lambda: _Disp()
    try:
        async def _go():
            out = await t._exec_build_compare(
                {"prompt": "build the finance view",
                 "variants": [{"harness": "claude-code", "model": "claude-opus-5"},
                              {"harness": "kimi"}],
                 "project_id": 9207},
                3, _FanPool(), "sess-9", "project:9207")
            # let the detached watcher run to completion
            for _ in range(20):
                await asyncio.sleep(0)
                if delivered:
                    break
            return out
        out = asyncio.run(_go())
    finally:
        rv.fan_out, rv._read_session_state = _orig_fan_out, _orig_state
        if _orig_await is not None:
            rv.await_session = _orig_await
        t._get_runtime_dispatch = _orig_disp
    ins = rows.get("insert") or {}
    check("a chat compare opens a task_type='fan_out' row with an origin back-ref",
          "'fan_out'" in rows.get("insert_sql", "")
          and ins.get("session_id") == "abc123def456"
          and ins.get("harnesses") == ["claude-code", "kimi"]
          and (ins.get("origin") or {}).get("session_id") == "sess-9"
          and (ins.get("origin") or {}).get("project_id") == 9207
          and (ins.get("origin") or {}).get("app_id") == "workbench"
          and ins.get("harness_model") == {"claude-code": "claude-opus-5"}
          # the compare's default model-wait window is 60 min
          # (tools.BUILD_MODEL_WAIT_S), was 15
          and ins.get("timeout_s") == 60 * 60 + 900,
          f"{rows.get('insert_sql', '')[:80]} {ins}")
    check("compare result carries task_id + a no-poll note",
          (out.get("result") or {}).get("task_id") == 501
          and "report in this thread" in str((out.get("result") or {}).get("note")),
          f"{out}")
    fin = {}
    try:
        fin = json.loads(rows["finish_args"][0])
    except Exception:
        pass
    check("watcher finalizes the row COMPLETED (one harness landed) with per-harness truth",
          "status='completed'" in rows.get("finish_sql", "")
          and "status='in_progress'" in rows.get("finish_sql", "")
          and fin.get("harnesses_done") == ["claude-code"]
          and fin.get("harnesses_failed") == ["kimi"]
          and (fin.get("per_harness") or {}).get("claude-code", {}).get("changed_files") == ["index.html"]
          and "provider.connection_error" in str((fin.get("per_harness") or {}).get("kimi"))
          and "claude-opus-5" in str(fin.get("model")),
          f"{rows.get('finish_sql', '')[:90]} {fin}")
    check("watcher wakes _deliver for the compare row", delivered == [501], f"{delivered}")

    # All-failed → the row lands FAILED with each harness's error.
    ok_, summ, per, done, failed = t._summarize_fan_out(
        {"session_id": "s1", "harnesses": ["codex", "grok"],
         "error": "background task cancelled (kernel shutdown mid-run)"},
        {"codex": {"state": "failed", "error": "codex not connected"},
         "grok": {"state": "running"}})
    check("_summarize_fan_out: all failed → ok=False; a variant left 'running' is failed with the session error",
          ok_ is False and done == [] and failed == ["codex", "grok"]
          and per["grok"]["state"] == "failed" and "cancelled" in per["grok"]["error"]
          and "codex not connected" in summ, f"{summ} {per}")

    # await_session: fast path awaits the owned task; slow path reads the
    # manifest until _session_is_over; an unknown session returns {} at once.
    if _orig_await is not None:
        with tempfile.TemporaryDirectory() as td:
            _orig_root = rv.SESSIONS_ROOT
            rv.SESSIONS_ROOT = Path(td)
            try:
                sd = Path(td) / "fast1"
                sd.mkdir()

                async def _bg():
                    await asyncio.sleep(0.01)
                    (sd / "manifest.json").write_text(json.dumps(
                        {"session_id": "fast1", "completed_at": 1.0, "results": {}}))

                async def _fast():
                    task = asyncio.create_task(_bg())
                    rv._SESSION_TASKS["fast1"] = task
                    m = await rv.await_session("fast1", poll_s=0.01)
                    rv._SESSION_TASKS.pop("fast1", None)
                    return m
                m = asyncio.run(_fast())
                check("await_session fast path returns the stamped manifest",
                      m.get("completed_at") == 1.0, f"{m}")
                sd2 = Path(td) / "slow1"
                sd2.mkdir()
                (sd2 / "manifest.json").write_text(json.dumps(
                    {"session_id": "slow1", "started_at": 1.0, "max_minutes": 1}))
                # Over by SILENCE: nothing
                # has written to it for longer than any live run can be quiet.
                _old = time.time() - 3 * 3600
                os.utime(sd2 / "manifest.json", (_old, _old))
                os.utime(sd2, (_old, _old))
                m = asyncio.run(rv.await_session("slow1", poll_s=0.01))
                check("await_session slow path: a session silent past every live window is over",
                      m.get("session_id") == "slow1", f"{m}")
                m = asyncio.run(rv.await_session("nope", poll_s=0.01))
                check("await_session on an unknown session returns {} immediately", m == {})
            finally:
                rv.SESSIONS_ROOT = _orig_root

    if FAILS:
        print(f"\n[build-harness] {len(FAILS)} FAILED: {FAILS}")
        return 1
    print("\n[build-harness] all gates pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
