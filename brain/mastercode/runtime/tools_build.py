"""The build tools: their handlers, the helpers only they use, and the build-phase helpers
the projects tools share.

Moved out of runtime/tools.py, which re-exports every name defined here. A tool's
category in the tool registry (public_tool_registry `_meta.category`) names the module
its handler lives in."""

import asyncio
import collections
import json
import logging
import re
from pathlib import Path

from mastercode.runtime.tool_call_ledger import refused as _refused, refused_if_caller as _refused_if_caller
from mastercode.runtime.tools_shared import _active_review_hold, _ci_merge_gate, _review_hold_reason

log = logging.getLogger("cseries.mastercode.entity_runtime")


def _current_turn_type_id():
    """The turn type the starting turn runs under (v186); None untyped."""
    try:
        from mastercode.runtime.turn_types import current_turn_type_id
        return current_turn_type_id()
    except Exception:  # noqa: BLE001 — a record never fails the work
        return None


# ── review.* tool implementations ──────────────────────────────────

async def _exec_build_compare(args: dict, entity_id: int | None = None, pool=None,
                              session_id: str | None = None,
                              interaction_context: str | None = None,
                              thread_id: str | None = None) -> dict:
    """A COMPARE: the same task on each named variant ({harness, model?,
    effort?}), each in its own worktree; variants of one harness share its
    pod. Reached only through build(action='run', variants=[…]) — the one
    build door. Returns the task_id at
    once; the detached watcher reports into the thread when every variant
    has landed, and the user picks (review.compare shows, review.adopt lands).

    Delivery lane: a compare started from chat used to end in
    silence — the harnesses ran, completion stamped a manifest on disk, and
    nothing could reach the thread again except the host polling. Once,
    claude-code ran clean for 203 s, the host said "I'll report when
    it lands", and never could. The door opens an entity_tasks row (origin
    back-ref, the shape a build row carries) and awaits the session in a
    detached watcher; when it lands the row is finalized and
    AgentDispatch._deliver wakes the host in the originating thread."""
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return _refused("prompt required")
    max_minutes = int(args.get("max_minutes") or BUILD_MODEL_WAIT_S // 60)
    try:
        from fastapi import HTTPException as _HTTPExc
        from server.routes.review import fan_out, FanOutReq
        req = FanOutReq(
            prompt=prompt,
            variants=args.get("variants"),
            max_minutes=max_minutes,
            project_id=args.get("project_id"),
            # Governed context routing — the host controls what context
            # reaches the builders; absent it, they get only the prompt.
            context=args.get("context"),
            vcpus=args.get("vcpus"),
            seed=args.get("seed"),
            acceptance=args.get("acceptance"),
            module=args.get("module"),
            target=args.get("target"),
        )
        result = await fan_out(req)
    except _HTTPExc as e:
        # A refusal names what was wrong (an unknown harness, a banned arm,
        # nothing connected) — pass the reason through, not the class name.
        return _refused_if_caller(e, f"compare refused: {e.detail}")
    except Exception as e:
        return _refused_if_caller(e, f"compare failed: {e}")

    out = dict(result) if isinstance(result, dict) else {"raw": result}
    run_sid = str(out.get("session_id") or "")
    dispatched = list(out.get("harnesses_dispatched") or [])
    variants = [v for v in (out.get("variants") or []) if isinstance(v, dict)]
    labels = [f"{v.get('harness')}" + (f" ({v['model']})" if v.get("model") else "")
              for v in variants] or dispatched
    tid: int | None = None
    if pool is not None and entity_id and run_sid:
        origin = {
            "entity_id": entity_id,
            "session_id": session_id,
            "app_id": "workbench" if (interaction_context or "").startswith("project:") else "circles",
            "project_id": args.get("project_id"),
            "interaction_context": interaction_context,
            # The Circles thread the compare was asked in; deliver_agent_result
            # writes the result back there (a turn's home is where its work
            # was asked for). None for the original conversation.
            "thread_id": thread_id,
            "origin_request": f"compare on {', '.join(labels) or 'the named engines'}: {prompt[:200]}",
        }
        # The row's budget follows the session's (review._session_is_over):
        # one slot is the run plus its declared checks, and variants of one
        # harness run in sequence, so the longest queue sets the length.
        _acc_s = (sum(int(c.get("timeout_s") or 300) for c in args["acceptance"]) + 60
                  if args.get("acceptance") else 0)
        _slot_min = max_minutes + (_acc_s + 59) // 60
        _per_h: dict = {}
        for v in variants:
            _per_h[v.get("harness")] = _per_h.get(v.get("harness"), 0) + 1
        tid = await _fan_out_task_start(
            pool, entity_id, prompt=prompt, fan_session_id=run_sid,
            harnesses=dispatched,
            harness_model={str(v.get("vid")): str(v.get("model"))
                           for v in variants if v.get("vid") and v.get("model")},
            project_id=args.get("project_id"),
            max_minutes=_slot_min * max([1, *_per_h.values()]),
            origin=origin)
        if tid is not None:
            task = asyncio.create_task(_watch_fan_out(pool, tid, run_sid))
            _FAN_OUT_TASKS[tid] = task
            task.add_done_callback(lambda _t, k=tid: _FAN_OUT_TASKS.pop(k, None))
    out["task_id"] = tid
    # A compare writes NOTHING to the plan: `project_id` scopes the session
    # for the live view and seeds the worktrees; project_phases and
    # phase_steps are untouched. Only build(action='build') advances a phase;
    # a compare-driven project with a good tree can still ship —
    # shippability is the tree's property, not the plan's.
    out["advances_plan"] = False
    out["note"] = (
        f"Compare started on {', '.join(labels) or 'no engine'} and is running "
        "in the background — the bench shows each variant's progress. Tell the "
        "user which engines and models are building. Do NOT block the chat or "
        "poll review_session for it; "
        + ("I'll report in this thread when every variant has landed; then show "
           "the candidates with review.compare and let the user pick"
           + (f" — review.adopt(into='{args['module']}') makes the pick the "
              "module's next version." if args.get("module") else ".")
           if tid is not None else
           "no delivery row could be opened, so check review_session on the "
           "user's next message.")
        + (args.get("plan_note") or "")
    )
    return {"status": "ok", "result": out}


# task_id -> the detached fan-out watcher (held so it isn't GC'd; the monitor's
# stuck scan cancels it the way it cancels a wedged build task).
_FAN_OUT_TASKS: dict[int, "asyncio.Task"] = {}


async def _fan_out_task_start(pool, entity_id: int, *, prompt: str,
                              fan_session_id: str, harnesses: list[str],
                              harness_model: dict | None, project_id: int | None,
                              max_minutes: int, origin: dict) -> int | None:
    """Open the registry row for a fan-out (task_type='fan_out'). Mirrors
    _factory_build_task_start: written BEFORE anything can finish, best-effort,
    None disables the delivery watcher. `timeout_s` is the session's own
    budget plus review._session_is_over's 900 s overhead, so the monitor's
    stuck horizon (2x) never reaps a fan-out that is merely slow."""
    result: dict = {
        "project_id": project_id,
        "intent": (prompt or "")[:500],
        "session_id": fan_session_id,
        "harnesses": list(harnesses or []),
        # The card's "harness" slot: every builder in the fan-out, so the
        # bench never has to infer from the title.
        "harness": ", ".join(harnesses or []),
        "timeout_s": int(max_minutes) * 60 + 900,
        "origin": origin,
        "run_kind": "fan_out",
    }
    if isinstance(harness_model, dict) and harness_model:
        result["harness_model"] = {str(k): str(v) for k, v in harness_model.items()}
    try:
        row = await pool.fetchrow(
            "INSERT INTO entity_tasks (entity_id, title, description, task_type, "
            "status, started_at, turn_type_id, result) "
            "VALUES ($1, $2, $3, 'fan_out', 'in_progress', NOW(), $4::uuid, $5::jsonb) "
            "RETURNING task_id",
            entity_id, f"compare: {prompt[:56]}"[:200], (prompt or "")[:4000],
            _current_turn_type_id(), json.dumps(result),
        )
        return row["task_id"] if row else None
    except Exception as e:
        log.warning("compare registry start failed (non-fatal): %s", e)
        return None


def _summarize_fan_out(manifest: dict, state: dict) -> tuple[bool, str, dict, list, list]:
    """Fold the session's terminal artifacts into (ok, summary, per_harness,
    done, failed). Per-harness truth comes from per_harness_state.json (each
    variant's own terminal write); the manifest adds the run-level error (a
    crashed/cancelled background task) and per-variant results when present.
    A variant still 'running'/'queued' once the session is over died without
    a terminal write — reported as failed with the session-level error."""
    results = manifest.get("results") if isinstance(manifest.get("results"), dict) else {}
    session_err = str(manifest.get("error") or "").strip()
    vids = list(dict.fromkeys(list((state or {}).keys()) + list(results.keys())))
    if not vids:
        vids = list(manifest.get("harnesses") or [])
    per: dict = {}
    done: list = []
    failed: list = []
    for vid in vids:
        st = (state or {}).get(vid) or {}
        r = results.get(vid) or {}
        s = str(st.get("state") or ("done" if r.get("ok") else ("failed" if r else "")))
        err = st.get("error") or r.get("error")
        if s not in ("done", "failed"):
            s = "failed"
            err = err or session_err or "no terminal state written (run died mid-flight)"
        entry = {
            "state": s,
            "model": st.get("model") or r.get("model"),
            "elapsed_seconds": st.get("elapsed_seconds") or r.get("elapsed_seconds"),
            "diff_bytes": st.get("diff_bytes"),
            "exit_code": st.get("exit_code", r.get("exit_code")),
            "error": (str(err)[:500] if err else None),
        }
        acc = st.get("acceptance") if isinstance(st.get("acceptance"), dict) else None
        if acc:
            entry["checks"] = {"ran": acc.get("ran"), "ok": acc.get("ok"),
                               "reason": acc.get("reason")}
        if st.get("killed_by"):
            # Killed (review.kill_session_runs): the delivery reads a compare
            # whose every variant was killed as a stop (session.kill_of).
            entry["killed_by"] = str(st["killed_by"])
        cf = r.get("changed_files")
        if isinstance(cf, list) and cf:
            # Whole, as on a build's row: a cut list read as the whole one.
            entry["changed_files"] = [str(f) for f in cf]
        per[vid] = entry
        (done if s == "done" else failed).append(vid)
    parts = []
    for vid in done:
        e = per[vid]
        bits = [b for b in (
            e.get("model"),
            f"{float(e['elapsed_seconds']):.0f} s" if e.get("elapsed_seconds") else None,
            f"{int(e['diff_bytes'])} B diff" if e.get("diff_bytes") else
            # A recorded zero is an empty run, not a landing.
            ("changed no files" if e.get("diff_bytes") is not None else None),
            ({True: "checks passed", False: "checks FAILED"}.get((e.get("checks") or {}).get("ok"),
                                                                "checks did not run")
             if e.get("checks") else None),
        ) if b]
        parts.append(f"{vid} landed" + (f" ({', '.join(bits)})" if bits else ""))
    for vid in failed:
        parts.append(f"{vid} failed: {per[vid].get('error') or 'unknown error'}")
    sid = str(manifest.get("session_id") or "")
    # The FULL id rides the summary: the host quotes what it reads, and an
    # 8-char prefix alone sent it to review_session('7a43f712') → "session not
    # found" → "the earlier run never registered".
    # review_session now also resolves unambiguous prefixes, belt and braces.
    summary = (
        f"Compare {sid[:8]} (session_id {sid}) finished — {len(done)}/{len(vids)} "
        f"variant(s) produced a result. " + "; ".join(parts)
        # Shown on the bench as well as read by the host, so it uses the
        # words on screen; the host's review skill names the tools.
        + (". Each version has an open version button; the user picks which "
           "one to use." if len(done) > 1 else "")
    ).strip()
    return bool(done), summary, per, done, failed


async def _watch_fan_out(pool, task_id: int, fan_session_id: str) -> None:
    """DETACHED watcher: await the fan-out session, finalize its registry row
    (guarded — a monitor reap that already delivered wins), then wake the
    host through the same _deliver lane a build uses."""
    manifest: dict = {}
    state: dict = {}
    try:
        from server.routes.review import await_session, _read_session_state, SESSIONS_ROOT
        manifest = await await_session(fan_session_id)
        state = _read_session_state(SESSIONS_ROOT / fan_session_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        manifest = {"session_id": fan_session_id, "error": f"compare watch failed: {e}"}
    if not manifest.get("session_id"):
        manifest["session_id"] = fan_session_id
    ok, summary, per, done, failed = _summarize_fan_out(manifest, state)
    # Past its first part the summary says so; every variant's whole state is
    # on this row (per_harness) and in the compare session.
    _where = f"per_harness on this record and review_session('{fan_session_id}')"
    merge = {
        "result": _mark_cut(summary, _REPORT_KEEP_CHARS, _where),
        "summary": _mark_cut(summary, _REPORT_KEEP_CHARS, _where),
        "per_harness": per,
        "harnesses_done": done,
        "harnesses_failed": failed,
        "session_id": fan_session_id,
    }
    models = sorted({str(e.get("model")) for e in per.values() if e.get("model")})
    if models:
        merge["model"] = ", ".join(models)
    try:
        if ok:
            wrote = await pool.execute(
                "UPDATE entity_tasks SET status='completed', completed_at=NOW(), "
                "result = COALESCE(result,'{}'::jsonb) || $1::jsonb "
                "WHERE task_id=$2 AND status='in_progress'",
                json.dumps(merge), task_id)
        else:
            err = "; ".join(
                f"{v}: {per[v].get('error') or 'unknown error'}" for v in failed
            ) or str(manifest.get("error") or "no harness produced a result")
            wrote = await pool.execute(
                "UPDATE entity_tasks SET status='failed', completed_at=NOW(), error=$1, "
                "result = COALESCE(result,'{}'::jsonb) || $3::jsonb "
                "WHERE task_id=$2 AND status='in_progress'",
                err[:2000], task_id, json.dumps(merge))
        if str(wrote).endswith(" 0"):
            log.warning("compare task=%s finished after being marked terminal elsewhere "
                        "— outcome write skipped", task_id)
    except Exception as e:
        log.warning("compare registry finish failed (non-fatal): %s", e)
    disp = _get_runtime_dispatch()
    if disp is not None:
        try:
            await disp._deliver(task_id)
        except Exception as e:
            log.warning("compare deliver failed (monitor will retry): %s", e)


# ── build implementation — harness POOL ───────
#
# The `build` meta-tool builds into the entity's Workbench project dir
# (/var/lib/cseries/projects/<id>/) by running ONE coding harness on the
# harness POOL — the SAME wake→run-on-pod→release path fan_out uses
# (server/routes/review.py::run_single_harness_build). Code executes
# OUTSIDE the entity container (the pod IS the sandbox), per the A/B
# safety model (docs/host-orchestrator-turn.md). This REPLACED the
# in-pod /factory-run sidecar (Claude was always a harness in the pool) —
# that build path, `_ensure_sidecar_running`,
# and the _SIDECAR_* constants retired with it. The claude-code system
# app stays as the chat/skill reasoning substrate, not a build engine.
#
# A research-grounded prompt is composed (deep_research auto-fires at
# plan time via _is_build_gated). A build's outcome is NOT written to
# memory: the task record and the project hold it, and a
# memory copy is a project-state snapshot that goes stale — one Host held
# 36 of them, harness transcripts included, recalled as knowledge.

_PROJECTS_ROOT = "/var/lib/cseries/projects"


async def _load_research_context(pool, research_id: str) -> str:
    """Pull the summary + by-leg rows for a research_id and assemble
    a research-context block to inject into the build prompt. Empty
    string if nothing matches (deep_research persistence may have
    failed, or the id is stale)."""
    try:
        rows = await pool.fetch(
            """SELECT content, metadata FROM entity_memory
               WHERE memory_type='research'
                 AND metadata->>'research_id' = $1
                 AND status = 'active' AND (t_valid_until IS NULL OR t_valid_until > NOW())
               ORDER BY id ASC""",
            research_id,
        )
    except Exception as e:
        log.warning("build: research context load failed for %s: %s", research_id, e)
        return ""

    if not rows:
        return ""

    summary = ""
    by_leg: dict[str, list[str]] = {"local": [], "github": [], "web": []}
    for r in rows:
        meta = r["metadata"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (TypeError, ValueError):
                meta = {}
        meta = meta or {}
        sk = meta.get("source_kind")
        content = r["content"] or ""
        if sk == "research_summary":
            summary = content
        elif sk in ("local", "github", "web"):
            by_leg[sk].append(content[:280])

    parts = []
    if summary:
        parts.append(f"Research summary:\n{summary}")
    for leg in ("local", "github", "web"):
        if by_leg[leg]:
            parts.append(f"{leg.upper()} findings:\n" + "\n".join(f"- {x}" for x in by_leg[leg]))
    return "\n\n".join(parts)


def _compose_build_prompt(intent: str, research_context: str) -> str:
    """Assemble the TASK prompt for a pool harness run.

    POOL CONTRACT: the harness runs on a harness pool pod with
    cwd = the seeded project worktree (harness_core.py sets cwd on /run) and
    ONLY that directory is collected back. So the workspace is conveyed by
    the working directory — NEVER by an entity-pod absolute path. The old
    sidecar-era wording ("Working dir: /var/lib/cseries/projects/<id>/") made
    the harness write to the POOL POD's own filesystem: zero files came back
    while the run scored success (seen live).
    The target's contract (target_contracts.contract_prefix) is prefixed at
    the pool layer (run_single_harness_build), identically to fan_out — not
    here. So this text names no deliverable: an app's index.html and app.yaml
    are its contract's to name, and a change to an existing repository has
    neither."""
    research_block = (
        f"Pre-build research (you must reference what already exists rather than re-implementing):\n\n{research_context}\n\n"
        if research_context else
        ""
    )
    return (
        f"Your current working directory IS the project — the worktree you are "
        f"in right now. Work here, with RELATIVE paths only. NEVER write to "
        f"an absolute path: only files inside your working directory are "
        f"collected into the project.\n\n"
        f"{research_block}"
        f"Task: {intent}\n\n"
        f"Be terse in your final response — name every file you changed, with a "
        f"one-line summary each."
    )


def _validate_built_project(project_dir: str, exclude_top=frozenset()) -> list[str]:
    """Run the App SDK build contract over a freshly-built project dir — the manifest
    + static portion of the bar the ship step (POST /api/applications/build) enforces.
    NOTE: ship enforces strictly MORE — it also runs the live-route /api conformance
    gate (validate_build(..., app=<serving app>) → api_contract.check_conformance),
    which needs the running FastAPI route table the harness doesn't have here. So a
    nonexistent-route / undeclared-scope violation surfaces at ship, not in this
    in-loop pre-check. Returns violation strings ([] = compliant). Best-effort: a load
    failure is surfaced as a violation the harness can act on, never an exception."""
    try:
        from pathlib import Path as _P
        from server.applications.manifest import load as load_manifest, ManifestError
        from server.applications.build_contract import validate_build
        pdir = _P(project_dir)
        app_yaml = pdir / "app.yaml"
        if not app_yaml.is_file():
            return ["app.yaml is missing at the project root — write the manifest (app.yaml) first"]
        try:
            manifest = load_manifest(app_yaml)
        except ManifestError as e:
            return [f"app.yaml is invalid: {e}"]
        return validate_build(pdir, manifest, exclude_top=exclude_top)
    except Exception as e:
        log.warning("build-contract validation skipped (non-fatal): %s", e)
        return []


# ── Factory-build registry rows ────────────────────────
#
# A pool build had NO surface: the bench's only sources are
# /api/review/sessions (fan-out coding runs) and /api/agents (dispatched agents), so
# the user watched the `build` tool card in chat while the bench showed
# nothing. Every sdk_build / sdk_continue run now records an entity_tasks
# row (task_type='factory_build') — written BEFORE the sidecar POST so a
# RUNNING build is visible while the sidecar works, finalized after — and
# rides the same /api/agents feed the bench and tray already render.

async def _factory_build_task_start(pool, entity_id: int, *, title: str,
                                    intent: str, project_id: int | None,
                                    origin: dict | None = None,
                                    phase_id: int | None = None,
                                    phase_number: int | None = None,
                                    phase_title: str | None = None,
                                    timeout_s: int | None = None,
                                    harness: str | None = None,
                                    model: str | None = None) -> int | None:
    """Open the registry row for a factory build. Best-effort — a registry
    write must never block a build; None disables the finalize.

    `model`: the model the build was asked to run on (the user's
    explicit pick or the ladder rung's), stamped at OPEN like `harness` so the
    card says "claude-code · claude-opus-5" from the first second; the finish
    confirms it from what actually ran.

    `harness`: the coding harness this build was resolved to,
    stamped at OPEN so the bench card and the host both know what is running
    from the first second (one build's row said nothing about claude-code — the
    only trace was inside the error text 25 s later).

    `origin`: when the build is started by the `build` meta-tool
    it seeds the SAME origin back-ref dispatched agents carry, so its terminal
    outcome flows through AgentDispatch._deliver → the result goes back to
    the host (the turn that started it, or one woken in the originating
    thread), which carries on from it. Left None for the
    plan action (no async delivery there — plans return in-line)."""
    result: dict = {"project_id": project_id, "intent": (intent or "")[:500]}
    if phase_id is not None:
        result["phase_id"] = phase_id
    # The USER-facing phase identity (number + title) rides the record so the
    # delivery composer never surfaces the internal phase_id (the host said
    # "Phase 6" for the project's phase 1).
    if phase_number is not None:
        result["phase_number"] = phase_number
    if phase_title:
        result["phase_title"] = phase_title[:200]
    if timeout_s:
        # The run's own budget — the monitor's stuck horizon scales off this
        # (a 4h build isn't "stuck" at the 4h floor; 2x its budget is).
        result["timeout_s"] = int(timeout_s)
    if harness:
        result["harness"] = str(harness)
    if model:
        result["model"] = str(model)
    if origin:
        result["origin"] = origin
    try:
        row = await pool.fetchrow(
            "INSERT INTO entity_tasks (entity_id, title, description, task_type, "
            "status, started_at, turn_type_id, result) "
            "VALUES ($1, $2, $3, 'factory_build', 'in_progress', NOW(), $4::uuid, $5::jsonb) "
            "RETURNING task_id",
            entity_id, (title or "build")[:200], (intent or "")[:4000],
            _current_turn_type_id(), json.dumps(result),
        )
        return row["task_id"] if row else None
    except Exception as e:
        log.warning("factory-build registry start failed (non-fatal): %s", e)
        return None


async def _build_usage_for_task(pool, task_id: int) -> dict:
    """The build's ledger row, folded onto its registry record.

    A factory build is billed and metered on harness_usage under
    session_id 'factory-build-<task_id>' (review.py: bkey = session_id), but
    the registry row the bench and `agent_status` read never carried any of
    it — tokens/est_cost_usd/rounds defaulted to 0 there, so the Host read
    "tokens=0, cost=0, rounds=0" for a build that was billed
    $0.0034. This is the join. `rounds`
    is an agent concept; a build reports none rather than zero."""
    try:
        row = await pool.fetchrow(
            """SELECT tokens_total, token_source, cost_microdollars, cost_method,
                      compute_s, wall_clock_s, billing_ref
                 FROM harness_usage WHERE session_id = $1
                ORDER BY id DESC LIMIT 1""", f"factory-build-{int(task_id)}")
    except Exception as e:  # noqa: BLE001 - the ledger join is best-effort
        log.debug("build usage join failed for task %s: %s", task_id, e)
        return {}
    if not row:
        return {}
    src = str(row["token_source"] or "unknown")
    tokens = int(row["tokens_total"] or 0)
    return {
        # None, not 0, when the harness reported no usage: kimi's role JSONL
        # carries no usage line at all (0.41.0), so a zero there is
        # "not reported", never "free".
        "tokens": tokens if src != "unknown" else None,
        "token_source": src,
        "cost_usd": (round(int(row["cost_microdollars"]) / 1_000_000.0, 6)
                     if row["cost_microdollars"] is not None else None),
        "cost_method": row["cost_method"],
        "compute_s": row["compute_s"],
        "wall_clock_s": row["wall_clock_s"],
        "billing_ref": row["billing_ref"],
        "rounds": None,
    }


# The harness's closing report as the Host reads it: its first part, and when
# that is not all of it, a line saying so and where the whole of it is. The
# whole run is kept on its harness_run row, so the Host's copy can stay short
# without anything being lost or cut without a word.
_REPORT_KEEP_CHARS = 4000
_CUT_MARK = "\n[cut: "
_CUT_MARK_ROOM = 400


def _mark_cut(text, keep: int, where: str) -> str:
    """`text` up to `keep` characters, with a closing line naming how much was
    left out and `where` the whole of it is. A text already cut this way
    upstream is passed through as it is."""
    text = str(text or "")
    if len(text) <= keep:
        return text
    if len(text) <= keep + _CUT_MARK_ROOM and _CUT_MARK in text[-_CUT_MARK_ROOM:]:
        return text
    return (f"{text[:keep]}{_CUT_MARK}the first {keep} of {len(text)} characters "
            f"are shown; the whole of it is in {where}]")


def _run_report_where(src: dict) -> str:
    """Where the whole of a build run's report can be read."""
    src = src if isinstance(src, dict) else {}
    if src.get("run_id"):
        return f"harness_transcript(run_id={src['run_id']})"
    if src.get("session_id") and src.get("harness"):
        return (f"harness_transcript(session_id='{src['session_id']}', "
                f"harness='{src['harness']}')")
    return "the run's transcript (harness_transcript)"


async def _factory_build_task_finish(pool, task_id: int | None, out: dict) -> bool:
    """Finalize the registry row from the meta-tool's return envelope.
    status='ok' → completed (summary/session/violations onto result);
    anything else → failed with the error text. Best-effort. Returns True iff
    THIS call moved the row (it was still in_progress) — the phase-state
    write in _run_phase_build keys on it so a late finisher never contradicts
    an outcome the monitor already delivered."""
    if task_id is None:
        return False
    try:
        ok = isinstance(out, dict) and out.get("status") == "ok"
        res = out.get("result") if isinstance(out, dict) else None
        res = res if isinstance(res, dict) else {}
        # WHICH harness/model this build actually ran on rides the row on BOTH
        # outcomes: a failed row used to carry only the error
        # text, so "what ran?" had to be inferred from the wording.
        _ran = {}
        if isinstance(out, dict):
            for k in ("harness", "model"):
                v = res.get(k) if res.get(k) else out.get(k)
                if v:
                    _ran[k] = str(v)
            # The run's bench session (= harness_run.session_id): the delivery
            # reads the run's transcript tail by it. It used to ride neither
            # outcome, so a failure report never saw what the run was doing.
            _sid = res.get("session_id") or out.get("session_id")
            if _sid:
                _ran["session_id"] = str(_sid)
        # The ledger row (billed cost, tokens with provenance) rides BOTH
        # outcomes too — a failed run was still metered and charged.
        _ran.update(await _build_usage_for_task(pool, task_id))
        if ok:
            # A build's summary is cut ONCE, inside the F-014 screen in
            # _pool_build, whose line states the report's own length; that
            # result carries `report_chars` and is stored as it is. Cutting it
            # again here cut the SCREENED text, which runs longer than the
            # report wherever the screen swaps a short marker for
            # "[neutralized]": the build's line was dropped and the new one
            # stated the screened length as the report's, or cut a report that
            # was whole. A summary from anywhere else is cut here.
            if isinstance(res.get("report_chars"), int):
                _summary = str(res.get("summary") or "")
            else:
                _summary = _mark_cut(res.get("summary"), _REPORT_KEEP_CHARS,
                                     _run_report_where(res))
            merge = {
                # `result` mirrors the dispatched-agent record shape so _row_to_agent /
                # the bench card render the outcome with zero special-casing.
                "result": _summary,
                "summary": _summary,
                "sidecar_session_id": res.get("session_id"),
                "violations": res.get("build_contract_violations") or [],
                "fix_rounds": res.get("fix_rounds"),
                # What the landed result IS rides the row (a merge
                # that dropped the face had the host invent an artifact
                # detour; on one Host the face was a static render labelled as
                # a live app). The row is what delivery reads, so it
                # carries the static render's URL under its honest name and the
                # OBSERVED app state (server.applications.app_state).
                "files_preview_url": res.get("files_preview_url"),
                "app": res.get("app"),
                # Every landed file, stored whole: this list was cut to 50
                # without a word, and the Host told the user what it read.
                "changed_files": list(res.get("changed_files") or []),
                # The pod's acceptance verdict (v124) rides the row on both
                # outcomes — it is what the delivery says about proof.
                "acceptance": res.get("acceptance"),
                # The desktop render check on a landed desktop design
                # (desktop_render.landed_tree_verdict) — the row is what the
                # delivery reads, so a verdict left off it never reaches the Host.
                "desktop_render": (res.get("desktop_render")
                                   if isinstance(res.get("desktop_render"), dict) else None),
                **_ran,
            }
            # status='in_progress' guard: a late finisher (a build task the
            # monitor already timed out + delivered as failed) must NOT flip
            # the row back to completed — the user was already told it failed.
            wrote = await pool.execute(
                "UPDATE entity_tasks SET status='completed', completed_at=NOW(), "
                "result = COALESCE(result,'{}'::jsonb) || $1::jsonb "
                "WHERE task_id=$2 AND status='in_progress'",
                json.dumps(merge), task_id,
            )
        else:
            err = ""
            if isinstance(out, dict):
                err = str(out.get("result") or out.get("error") or "build failed")
                # A build refused for links out of the project
                # (_links_out_refusal): no pod woke, nothing was built, so it is
                # stamped and _phase_retry_pick does not count it as an attempt.
                if out.get("links_refused"):
                    _ran["links_refused"] = True
                # Nor is a run whose land was refused as an integration
                # conflict: the coder did the work;
                # the plan's split put it on top of another phase's.
                if isinstance(out.get("integration_conflict"), dict):
                    _ran["integration_conflict"] = out["integration_conflict"]
                # Nor is a run someone KILLED (review.kill_session_runs): the
                # person or the Host stopped it. Stamped so the retry budget
                # (_failed_build_kind) and the delivery (session.kill_of) read
                # a stop, never the work failing.
                if out.get("killed_by"):
                    _ran["killed_by"] = str(out["killed_by"])
            wrote = await pool.execute(
                "UPDATE entity_tasks SET status='failed', completed_at=NOW(), error=$1, "
                "result = COALESCE(result,'{}'::jsonb) || $3::jsonb "
                "WHERE task_id=$2 AND status='in_progress'",
                err[:2000], task_id, json.dumps(_ran),
            )
        landed = not str(wrote).endswith(" 0")
        if not landed:
            log.warning("factory-build task=%s finished after being marked terminal "
                        "elsewhere — outcome write skipped (row keeps what the user was told)",
                        task_id)
        return landed
    except Exception as e:
        log.warning("factory-build registry finish failed (non-fatal): %s", e)
        return False


# Where a seeded retry's failure text starts in a phase intent (the brief is
# everything before it).
_RETRY_SEED_MARK = "\n\n--- what the previous attempt hit ---\n"


# A build's `timeout` / `max_minutes`. It is NOT a
# budget for the build: a build has no total time limit,
# and a run that is working — printing, using CPU, moving data — is never
# stopped by it. It is the longest ONE model call may go with nothing arriving
# before the pod stops the run as hung (harness_core.turn_budget_s), and with
# a margin the kernel's stream backstop. The default was 900 s, which on the
# pod bounded a silent model turn (a long streamed write on an older image) and
# on the buffered fallback the whole run; the Host's tool text said the
# opposite. 3600 matches the pod's own default (harness_core.MODEL_WAIT_MINUTES).
# The 4 h maximum is the most a Host may ask one hung call to be waited on,
# billed pod time with nothing happening; it is stated in the tool text.
BUILD_MODEL_WAIT_S = 3600
BUILD_MODEL_WAIT_MAX_S = 14400


def _build_wait_cap_s() -> float | None:
    """The person's limit `build.model_wait_max_minutes` (runtime/user_limits)
    in seconds, BUILD_MODEL_WAIT_MAX_S by default; None for no limit."""
    from mastercode.runtime import user_limits
    return user_limits.person_seconds_or("build.model_wait_max_minutes",
                                         BUILD_MODEL_WAIT_MAX_S, off=None)


def _build_wait_s(requested_s) -> tuple[int, str | None]:
    """(seconds to wait on one silent model call, None) or (0, refusal). A
    build that names no wait gets BUILD_MODEL_WAIT_S, within the person's
    limit. One that asks for more than the limit is refused with the limit
    named rather than quietly cut, so the Host can ask the person."""
    cap = _build_wait_cap_s()
    if requested_s is None:
        return int(min(BUILD_MODEL_WAIT_S, cap) if cap else BUILD_MODEL_WAIT_S), None
    if cap is not None and requested_s > cap:
        from mastercode.runtime import user_limits
        return 0, (f"a wait of {int(requested_s) // 60} minutes is above the person's limit. "
                   + user_limits.hit_line("build.model_wait_max_minutes"))
    return int(requested_s), None


async def _pool_build(*, entity_id: int, pool, project_id: int, prompt: str,
                      research_id: str | None, timeout_s: int,
                      harness: str | None = None, model: str | None = None,
                      effort: str | None = None,
                      on_activity=None, billing_key: str | None = None,
                      phase_id: int | None = None,
                      acceptance: list[dict] | None = None,
                      one_shot: bool = False,
                      module: str | None = None,
                      target: str | None = None,
                      pod_slots: int | None = None) -> dict:
    """Run ONE coding harness on the harness POOL against the project and land
    the result in its dir — the SAME wake→run-on-pod→release path fan_out uses.
    This replaces the retired in-pod /factory-run sidecar (Claude was always
    a harness in the pool). Code executes OUTSIDE the
    entity container (the pod IS the sandbox), per the A/B safety model. Returns
    the meta-tool envelope; validates the landed project against the App SDK
    build contract and surfaces (does not auto-fix — a single-harness pool run
    has no in-session resume; a follow-up is a seeded rerun).

    THE PLAN GATE LIVES HERE — the primitive layer. Every route to the pool
    (build/continue, any surface) passes through this function, so a project
    with no phases is refused HERE, not just at the chat door. A gate only at
    the door is one bypassed argument away from an unplanned build.

    `one_shot` is the exception, and it
    is a keyword the phase lanes never pass. Two callers set it:
    build(action='run') — a single build the user asked for, with no plan
    behind it — and the kernel's post-ship repair (app_verify._start_repair),
    a fixed-brief fix to a tree that already shipped, never a phase. A phase
    build, the chain, the seeded retry and a `continue` all reach here with it
    False, so the gate still holds for every one of them
    (scripts/test_build_run_door.py §8 pins the callers)."""
    phase_count = 1 if one_shot else await pool.fetchval(
        "SELECT COUNT(*) FROM project_phases WHERE project_id=$1", project_id)
    if not phase_count:
        return _refused("This project has no plan — refusing to run a build. Call "
            "build(action='plan', project_id, intent) first, SHOW the phased "
            "plan to the user, and build only after they approve it.")

    # A MODULE build works in the module's directory
    # and is handed its target's contract; without one, the project root and
    # the private-app contract, exactly as before modules.
    project_dir = f"{_PROJECTS_ROOT}/{project_id}" + (f"/{module}" if module else "")
    from server.applications.target_contracts import checks_for
    _checks = checks_for(target)
    _cap_s = _build_wait_cap_s()
    max_minutes = max(1, (timeout_s + 59) // 60 if _cap_s is None
                      else min((timeout_s + 59) // 60, int(_cap_s) // 60))
    try:
        from server.routes.review import run_single_harness_build
    except Exception as e:
        return _refused_if_caller(e, f"pool build unavailable: {e}")
    try:
        out = await run_single_harness_build(
            project_id=project_id, prompt=prompt, harness=harness, model=model,
            effort=effort, max_minutes=max_minutes, on_activity=on_activity,
            billing_key=billing_key, phase_id=phase_id, acceptance=acceptance,
            module=module, target=target, slots=pod_slots)
    except Exception as e:
        return _refused_if_caller(e, f"pool build failed: {e}")

    # The checks the phase or run listed ran on the pod after the harness
    # finished (review._run_acceptance). Their result is information that
    # rides the envelope; it never decides the outcome. A build either landed
    # its files or the harness failed (four builds that landed working code were once reported as
    # "never produced the files" because a check named a path no brief asked for).
    _acc = out.get("acceptance") if isinstance(out.get("acceptance"), dict) else None

    if not out.get("ok") and out.get("links_out"):
        # Refused before any pod woke (review.links_out_refusal): nothing ran,
        # so none of the continue advice below applies.
        return _refused(f"no build ran: {out.get('error')}", links_refused=True, harness=out.get("harness") or harness)

    if not out.get("ok") and out.get("integration_conflict"):
        # The run built, but its changes could not be merged with work that
        # landed in the project while it ran. Nothing
        # landed and nothing is to be continued from — the continue advice
        # below would be wrong here — so the envelope carries the conflict as
        # data and the caller (_run_phase_build) says what it means.
        return {"status": "error", "result": out.get("error") or "integration conflict",
                "integration_conflict": out["integration_conflict"],
                "harness": out.get("harness") or harness,
                "model": out.get("model"),
                "session_id": out.get("session_id")}

    if not out.get("ok"):
        # Failure must not VANISH the work — and it must not INVENT work
        # either. The old message said "partial work KEPT at <worktree>": an
        # entity-pod absolute path the next harness pod cannot see, in a dir
        # the next run's _prepare_worktree deletes. The host repeated it
        # verbatim into the continue prompt ("resume from kept partial work at
        # /var/lib/cseries/projects/<id>/kimi"
        # — the CLI correctly reported the path does not exist). Everything
        # stated here must be TRUE and durable: the archived copy in the
        # bench session (review.run_single_harness_build), with a file count,
        # and the real semantics of a continue.
        err = out.get("error") or "harness run failed"
        kept = out.get("failed_worktree") or {}
        sid = out.get("session_id")
        if kept.get("files"):
            err += (f" — the failed attempt's partial work ({kept['files']} "
                    f"file(s)) is archived at {kept['path']}")
        else:
            err += " — the failed attempt left no recoverable files"
        if sid:
            err += (f"; full live transcript + collected harness logs are in "
                    f"bench session {sid} (harness_transcript reads the run row)")
        err += (". build(action='continue') runs FRESH, seeded from the "
                "project's landed state — archived partial work is not "
                "auto-seeded, and the build prompt must never name host-pod "
                "file paths (pool harnesses see only their own working "
                "directory).")
        # The harness/model that failed ride the envelope so the registry row
        # and the delivered report name them (not just the error prose).
        return {"status": "error", "result": err,
                "harness": out.get("harness") or harness,
                "model": out.get("model"),
                "session_id": sid,
                # A kill (review.kill_session_runs): the row is stamped with it,
                # so the retry budget and the delivery read a stop, not a failure.
                **({"killed_by": out["killed_by"]} if out.get("killed_by") else {})}

    # F-014: same untrusted source as harness_transcript. `content` lands in
    # the tool result the host reasons over, so it is screened here.
    # The summary PREFERS the harness's own closing words (final_report, from
    # the normalized frame log) over a raw-transcript prefix — one run's
    # prefix was a version header + numbered file dump, and the host reported
    # "the log cuts off mid-file" about a clean run.
    # The Host's copy is the report's first part; past it, a closing line says
    # how much was left out and where the whole run is.
    # The screen's output IS the summary: the closing line is built first and
    # screened together with the report, in the one expression below, and
    # nothing is added to `content` after it. Appending the line after the
    # screen made the guarantee rest on every field of
    # that line staying platform-authored, which nothing enforced, and the
    # smoke screen gate (scripts/test_untrusted_content_screen.py) failed the
    # ship on it. That gate reads this expression's shape, `[:4000]` included;
    # tests/test_build_summary_screen.py holds 4000 equal to _REPORT_KEEP_CHARS
    # and drives this path with a hostile report.
    from mastercode.runtime.untrusted_content import screen_for_model
    _report_len = len(str(out.get("final_report") or out.get("transcript") or ""))
    _cut_line = ""
    if _report_len > _REPORT_KEEP_CHARS:
        _cut_line = (f"{_CUT_MARK}the first {_REPORT_KEEP_CHARS} of {_report_len} "
                     f"characters are shown; the whole of it is in "
                     f"{_run_report_where({**out, 'harness': out.get('harness') or harness})}]")
    content, _inj_flags = screen_for_model(
        (str(out.get("final_report") or out.get("transcript") or ""))[:4000] + _cut_line,
        source="build_summary")
    # The App SDK build contract applies only to what installs as an app — a
    # public site or a component has no manifest to validate.
    # A ROOT build in a project that also has modules: the module folders
    # are not the root app — they are not scanned against its contract.
    _excl = frozenset()
    if not module and _checks["manifest"]:
        try:
            _excl = frozenset(r["name"] for r in await pool.fetch(
                "SELECT name FROM project_modules WHERE project_id=$1", int(project_id)))
        except Exception as e:  # noqa: BLE001 — scan everything rather than nothing
            log.warning("module list for the build check failed: %s", e)
    # Contract violations (a missing app.yaml among them) ride the landed
    # result for the report and the build PR; the Code Approval refuses to
    # merge a tree that has them. They do not turn a landed build into a
    # failed one.
    violations = _validate_built_project(project_dir, _excl) if _checks["manifest"] else []

    # What the landed result IS, as typed data. This
    # used to return `preview_url` — the STATIC render of index.html — under
    # a `notes` string calling it the app's live render; the host then told
    # the user three times to open a live app that had never been
    # installed. Now: `files_preview_url` is named for what it is, and `app`
    # is the OBSERVED state of the app the project declares (installed /
    # running measured, never inferred). No prose rides the envelope.
    files_preview_url = None
    try:
        from pathlib import Path as _Path
        from server.routes.projects import project_entry_choice
        if project_entry_choice(_Path(project_dir)).entries:
            files_preview_url = (f"/api/projects/{project_id}/preview"
                                 + (f"?module={module}" if module else ""))
    except Exception:
        pass
    try:
        from pathlib import Path as _Path
        from server.applications.app_state import project_app_state
        app_state = await project_app_state(pool, project_id, _Path(project_dir))
    except Exception as e:  # noqa: BLE001 — the empty state is the honest fallback
        log.warning("app state unavailable for project %d (non-fatal): %s", project_id, e)
        from server.applications.app_state import empty_state
        app_state = empty_state()

    return {
        "status": "ok",
        "result": {
            "summary": content,
            # The harness report's own length, before any cut: `summary` was
            # cut and screened once above, and _factory_build_task_finish
            # stores it as it is when this is present.
            "report_chars": _report_len,
            "project_dir": f"{project_dir}/",
            "research_id": research_id,
            "harness": out.get("harness"),
            "model": out.get("model"),
            "compute_s": out.get("compute_s"),
            "changed_files": out.get("changed_files") or [],
            # The numbered run this build recorded (harness_run.id) — carried
            # to the save-point commit as its Host-Run trailer.
            "run_id": out.get("run_id"),
            "build_contract_violations": violations,
            "injection_flags": _inj_flags,
            # The bench session that carries this run's live transcript +
            # collected harness session/logs (review-sessions/<id>).
            "session_id": out.get("session_id"),
            # The pod's verdict on the listed checks (v124): {ran, ok, reason?,
            # results} or None when nothing was listed. Reported as notes (the
            # delivery record carries it); it never decides the phase's status.
            "acceptance": _acc,
            # Static render of the landed files, or None when there is no
            # entry document. It is NOT the app.
            "files_preview_url": files_preview_url,
            # {app_id, kind, installed, running, opens_via, install_action}
            "app": app_state,
        },
    }


# ── build: plan → approve → build, structurally enforced ──
#
# The host must not go straight into building: with no planning, no phased
# builds and no way to see the plan, it just handed the ask to the build
# agent. The `build` meta-tool is the ONLY chat
# door to building, and it CANNOT jump straight to code:
#
#   build(action='plan',  project_id, intent)  → composes + persists a
#       phased plan (project_phases + phase_steps), returns it so the host
#       SHOWS it in chat. Research-grounded (the build gate fires
#       deep_research for action='plan').
#   build(action='build', project_id, phase_id?) → REFUSES if the project
#       has no phases (plan first). Otherwise starts a
#       DETACHED per-phase build (survives the chat turn), returns the
#       task_id immediately, writes phase/step transitions as it goes so the
#       bench PLAN section lights up live, and on completion/failure hands
#       the result to the host (AgentDispatch._deliver ->
#       deliver_agent_result): into the turn that started it while that turn
#       holds for it, else a turn woken in the originating thread, which
#       carries on from it with its tools.
#
# The structural gate is the backstop; default-skills/build-workflow.md +
# the workbench prompt teach the manner (align → plan → build, shown as it
# starts). The user's request is the yes: a plan the request
# asked for builds in the same turn, and only a choice the user alone can make
# goes to the question box (escalate). The one approval the code enforces is a
# gate='user' phase (_phase_awaits_approval).

# task_id -> the detached per-phase build task (held so it isn't GC'd and a
# monitor veto / kernel introspection can find it). Survives chat turns.
_FACTORY_BUILD_TASKS: dict[int, "asyncio.Task"] = {}

# project_phases.status ∈ (planning,speccing,ready,in_progress,review,complete)
# phase_steps.status   ∈ (pending,speccing,implementing,checking,done,failed)
# — the transitions below use the REAL enum values (an earlier set_step_status
# accepted 'in_progress', which VIOLATES the phase_steps CHECK).
# project_phases.status ∈ (planning,speccing,ready,in_progress,review,complete,
# failed) — 'failed' added by migrations/v108. A failed build used
# to set its phase back to READY, indistinguishable in the workflow pane from
# a phase never attempted; it now says failed. Still re-buildable: the next
# build(action='build') picks the first phase that is still OPEN (_PHASE_OPEN_SQL).
_PHASE_RUNNING, _PHASE_DONE, _PHASE_FAILED = "in_progress", "complete", "failed"
# 'ready' = has a brief and may be built. _persist_plan writes it; add_phase
# now writes it too when a spec is supplied. Named rather than repeated as a
# literal, because three places have to agree on it: the planner, the chat
# build door's readiness gate, and the auto-advance chain's.
_PHASE_READY = "ready"
# Retirement, not deletion (v110). With nothing able to REMOVE a phase,
# a pivoted project kept dead phases forever and the only lever that cleared
# them — the supersede inside action='plan' — took the replacement plan with it.
_PHASE_ARCHIVED = "archived"
# A phase that landed without its checks passing once waited here (v124).
# Nothing writes it any more — a landed build is 'complete' — but older rows
# still carry it, so every reader
# treats it as landed: not open, and met as a dependency.
_PHASE_REVIEW = "review"
# A phase the user must approve before anything builds it (v124). Data, not a
# sentence in the brief: phase 3 of that same project carried "approved by the
# user BEFORE this phase builds" in prose and auto-advance built it anyway.
_PHASE_GATE_USER = "user"
# THE predicate for "work this project still has open". Six queries mean exactly
# this — the build door's pick, both auto-advance picks, the behind-count, the
# project-complete flip and the halt report — and they were six separate copies
# of `status <> 'complete'`. One constant so a new terminal state can never be
# honoured by some of them and ignored by the rest, which is the drift this
# whole lane keeps producing. SQL-safe: a module constant, never user input.
#
# 'review' is NOT open: those phases landed (see _PHASE_REVIEW).
_PHASE_OPEN_SQL = (f"status NOT IN ('{_PHASE_DONE}','{_PHASE_ARCHIVED}',"
                   f"'{_PHASE_REVIEW}')")
_STEP_RUNNING, _STEP_DONE, _STEP_FAILED = "implementing", "done", "failed"


def _get_runtime_dispatch():
    """The runtime's AgentDispatch singleton (for _deliver). None until the
    runtime is wired — delivery then falls to the monitor safety net."""
    try:
        import server.brain_service as brain_service
        rt = brain_service.get_runtime()
        return getattr(rt, "agent_dispatch", None) if rt is not None else None
    except Exception:
        return None


def _compose_default_plan(intent: str) -> list[dict]:
    """The DRAFT shape used when the host passes no `phases` — titles and steps
    only, deliberately WITHOUT briefs.

    It used to fabricate a brief per phase ("Build the core functionality:
    <intent>"), which is a title-only brief wearing a spec: enough to satisfy
    the readiness gate, not enough to build from. _persist_plan then marked it
    'ready', so the draft was indistinguishable from a plan the host had
    actually composed — same table, same status, same shape. Measured:
    17 phases still carried that fabricated brief and 12
    of them had been BUILT from it, across four projects.

    Emitting no brief makes the difference structural rather than a matter of
    the host reading a note: _persist_plan lands these 'planning', the build
    door refuses them by name and says exactly what unblocks it, and
    auto-advance will not walk into them. The titles remain because a shape to
    react to is genuinely useful — it is a starting point for the host to
    replace, not an artifact to approve.

    `research_context` is gone from the signature: it only ever appeared inside
    the fabricated brief text, and a draft with no brief has nowhere to put it.
    Research still reaches the real plan — the host composes `phases` from it
    and passes them in (see the `plan` action)."""
    intent_short = (intent or "the app").strip()
    return [
        {
            "title": "Scaffold & manifest",
            "steps": [
                {"title": "Write app.yaml manifest (provenance, network, scopes)"},
                {"title": "Scaffold index.html + assets skeleton"},
            ],
        },
        {
            "title": f"Implement: {intent_short[:80]}",
            "steps": [
                {"title": "Implement the core feature described by the intent"},
                {"title": "Wire state via CSeriesApp.storage (never localStorage)"},
            ],
        },
        {
            "title": "Verify & preview",
            "steps": [
                {"title": "Self-check the build contract (isolation, storage, declared network)"},
                {"title": "Confirm index.html renders in the project preview"},
            ],
        },
    ]


_ACCEPTANCE_MAX_CMDS = 32
_ACCEPTANCE_DEFAULT_TIMEOUT_S = 300
# A check's declared timeout is honoured as written (cut to 1800 s without a
# word, a 3600 s integration suite was killed half way). The one bound left is the pod's: it runs a check with
# Popen.communicate(timeout=...), whose wait is a poll() in milliseconds held
# in a C int, and Python raises OverflowError past 2**31-1 ms (measured: 2.0e6 s
# runs, 2.2e6 s raises). A check over that, about 24 days, is refused by name
# when the plan is written, never cut.
_ACCEPTANCE_MAX_TIMEOUT_S = (2**31 - 1) // 1000


def _normalize_acceptance(raw) -> list[dict] | None:
    """The phase's acceptance checks as the pod will run them, or None when
    nothing is declared. Raises ValueError on a shape that cannot run — a
    plan carrying an unrunnable check must be refused at write time, never
    discovered when the phase lands.

    Shape (v124): [{cmd: str, expect_exit?: int (0), timeout_s?: int (300)}].
    Commands run with `bash -c` in the run's own worktree inside the harness
    pod (harness_core POST /check) after the harness finishes; their results
    are reported as notes and never decide the phase's outcome."""
    if raw is None or raw == []:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("acceptance must be a list of {cmd, expect_exit?, timeout_s?}")
    out: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            item = {"cmd": item}
        if not isinstance(item, dict):
            raise ValueError("each acceptance check must be {cmd, expect_exit?, timeout_s?}")
        cmd = str(item.get("cmd") or "").strip()
        if not cmd:
            raise ValueError("an acceptance check has no cmd")
        try:
            expect = int(item.get("expect_exit", 0) or 0)
        except (TypeError, ValueError):
            raise ValueError(f"expect_exit must be an integer: {item.get('expect_exit')!r}")
        try:
            timeout = int(item.get("timeout_s") or _ACCEPTANCE_DEFAULT_TIMEOUT_S)
        except (TypeError, ValueError):
            raise ValueError(f"timeout_s must be an integer: {item.get('timeout_s')!r}")
        if timeout < 1:
            raise ValueError(f"timeout_s must be a positive number of seconds: {timeout}")
        if timeout > _ACCEPTANCE_MAX_TIMEOUT_S:
            raise ValueError(
                f"timeout_s {timeout} for the check {cmd[:80]!r} is more than the "
                f"{_ACCEPTANCE_MAX_TIMEOUT_S} s (about 24 days) a check can be timed for "
                "on the pod; give it a shorter timeout or split it into several checks")
        out.append({"cmd": cmd, "expect_exit": expect, "timeout_s": timeout})
    if len(out) > _ACCEPTANCE_MAX_CMDS:
        raise ValueError(f"at most {_ACCEPTANCE_MAX_CMDS} acceptance checks per phase")
    return out


def _image_tools():
    """system-apps/_harness_runtime/image_tools — the measured per-image
    toolset + the command parser. Lives with the harness runtime (it ships
    inside every harness image AND the entity image: Dockerfile.linux copies
    the package to /app/cli/_harness_runtime), so the entity validates against
    the same table the pod runs with. Source tree: system-apps/."""
    try:
        from _harness_runtime import image_tools
        return image_tools
    except ImportError:
        import sys
        _root = Path(__file__).resolve().parents[3]
        for _sub in ("cli", "system-apps"):
            _p = str(_root / _sub)
            if _p not in sys.path:
                sys.path.append(_p)
        from _harness_runtime import image_tools
        return image_tools


async def _links_out_refusal(pool, project_id: int, *, module: str | None = None,
                             module_id: int | None = None) -> str | None:
    """None, or why no build can start on this project (or this module): the
    links in the tree a build is seeded from that point outside it, each by
    name, and what to do (review.links_out_refusal). Refused at the chat door
    and on the chain before a phase is claimed, a row opened or a pod woken,
    the shape of the acceptance tool gate. A failure to look is not a
    refusal: run_single_harness_build checks again before its seed copy."""
    try:
        from server.routes import review as _rv
        from server.routes.project_modules import get_module_by_id, module_dir
        name = module
        if name is None and module_id is not None:
            row = await get_module_by_id(pool, module_id)
            name = row["name"] if row else None
        root = module_dir(project_id, name) if name else _rv.PROJECTS_ROOT / str(int(project_id))
        return await asyncio.to_thread(_rv.links_out_refusal, root)
    except Exception as e:  # noqa: BLE001
        log.warning("link check for project %s failed (the build checks again): %s",
                    project_id, e)
        return None


def _acceptance_tool_refusal(cmds: list[dict] | None, harness: str | None) -> str | None:
    """Refuse an acceptance check that needs a tool the harness image does not
    ship — a refusal, chosen after a build
    (kimi) failed its phase on `bash: node:
    command not found` with the work itself fine, and auto-deliver dispatched
    a repair at a healthy result.

    `harness` None = the engine is not picked yet (add_phase / set_phase_spec /
    plan): only a tool that NO image ships is refused there, so nothing that
    some engine could run is turned away early. A run (build action='run', a
    compare) calls again with the resolved engine, and that is where a
    per-image gap (`rg` is on codex only) is refused — before any pod wakes. A
    phase build is not refused at build time: its checks are reported as notes
    and never decide its outcome.

    Returns the plain-English reason (tool + image + what to use instead),
    or None. Never raises: an unreadable table is logged and the check is
    left to the pod, which reports exit 127 the way it always did."""
    if not cmds:
        return None
    try:
        it = _image_tools()
    except Exception as e:  # pragma: no cover — the package is in both images
        log.error("acceptance tool gate unavailable (image_tools import): %s", e)
        return None
    reasons = []
    for c in cmds:
        try:
            r = it.refusal(str((c or {}).get("cmd") or ""), harness)
        except Exception as e:
            log.warning("acceptance tool gate could not parse %r (left to the pod): %s",
                        (c or {}).get("cmd"), e)
            r = None
        if r:
            reasons.append(r)
    return "\n\n".join(reasons) or None


def _normalize_gate(raw) -> str:
    """'none' | 'user' — the project_phases.gate CHECK, validated before the
    write so a typo can never land as an unenforced gate."""
    g = (str(raw or "none")).strip().lower() or "none"
    if g not in ("none", _PHASE_GATE_USER):
        raise ValueError(f"gate must be 'none' or '{_PHASE_GATE_USER}', got {raw!r}")
    return g


def _phase_validation_for(acc, own, module_validation) -> tuple[str | None, str | None]:
    """(validation, refusal) for ONE phase becoming buildable (add_phase,
    set_phase_spec). The value is recorded and decides nothing: a build either
    landed or failed. Refused only when a value is given
    that is not a known one."""
    own = (str(own or "").strip().lower() or None)
    if own is not None and own not in ("checks", "proof", "review"):
        return None, "validation must be one of checks, proof, review"
    return ("checks" if acc else (own or module_validation or "review")), None


def _validate_plan(phases, modules) -> tuple[list[dict], list[dict]]:
    """The whole plan, checked BEFORE anything is written or superseded.
    Returns (modules, phases): modules normalized
    ({name, target, validation, description}), phases flattened in order, each
    carrying `_module` (a module name or None for the project's root app) and
    its normalized `_acc` / `_gate`. Raises ValueError naming what is wrong.

    A phase's checks (`acceptance`) and `validation` are optional and are
    recorded only: a build either landed or failed."""
    from server.routes.project_modules import validate_module_fields, ModuleError
    mods_out: list[dict] = []
    flat: list[dict] = []
    seen: set = set()
    for i, m in enumerate(modules or []):
        if not isinstance(m, dict):
            raise ValueError(f"modules[{i}] must be {{name, target, validation, phases}}")
        name = str(m.get("name") or "").strip().lower()
        target = str(m.get("target") or "").strip().lower()
        validation = str(m.get("validation") or "").strip().lower() or "review"
        try:
            validate_module_fields(name, target, validation)
        except ModuleError as e:
            raise ValueError(str(e))
        if name in seen:
            raise ValueError(f"module {name!r} is named twice")
        seen.add(name)
        mods_out.append({"name": name, "target": target, "validation": validation,
                         "description": str(m.get("description") or "").strip(),
                         # Make an existing root-app folder of that name the
                         # module (refused otherwise — upsert_module).
                         "adopt_dir": bool(m.get("adopt_dir"))})
        for ph in (m.get("phases") or []):
            flat.append({**(ph if isinstance(ph, dict) else {}), "_module": name,
                         "_validation": validation})
    for ph in (phases or []):
        flat.append({**(ph if isinstance(ph, dict) else {}), "_module": None,
                     "_validation": None})
    for ph in flat:
        title = (ph.get("title") or "").strip()
        if not title:
            continue
        acc = _normalize_acceptance(ph.get("acceptance"))
        tool_ref = _acceptance_tool_refusal(acc, None)
        if tool_ref:
            raise ValueError(f"phase '{title}': {tool_ref}")
        ph["_acc"] = acc
        ph["_gate"] = _normalize_gate(ph.get("gate"))
        where = f"phase '{title}'" + (f" (module {ph['_module']})" if ph["_module"] else "")
        _val, _why = _phase_validation_for(acc, ph.get("validation"), ph["_validation"])
        if _why:
            raise ValueError(f"{where}: {_why}")
        ph["_validation"] = _val
    _resolve_plan_dependencies(flat)
    return mods_out, flat


def _find_cycle(edges: dict) -> list | None:
    """A dependency cycle in {node: [nodes it waits for]}, as the nodes on it,
    or None. A cycle is a plan that can never start."""
    state: dict = {}
    stack: list = []

    def visit(n):
        state[n] = 1
        stack.append(n)
        for d in edges.get(n, ()):
            if state.get(d) == 1:
                return stack[stack.index(d):] + [d]
            if d not in state:
                found = visit(d)
                if found:
                    return found
        stack.pop()
        state[n] = 2
        return None

    for n in list(edges):
        if n not in state:
            found = visit(n)
            if found:
                return found
    return None


def _resolve_plan_dependencies(flat: list) -> None:
    """Each titled phase of a plan gets `_deps` — the
    indexes (into the titled phases, in plan order) of the phases it waits
    for — and its engine (`_harness`, `_model`). Raises ValueError naming
    what is wrong; nothing is written until the whole plan passes.

    `depends_on` names phases of the SAME plan by their `key` or `title`
    ([] = waits for nothing). Omitted: the phase before it in its module (or
    among the root app's phases), none for the first — the order every plan
    had before. Resolved here so a new plan never waits on a phase left over
    from the plan it replaces."""
    titled = [ph for ph in flat if (ph.get("title") or "").strip()]
    by_key: dict = {}
    by_title: dict = {}
    for i, ph in enumerate(titled):
        key = str(ph.get("key") or "").strip().lower()
        if key:
            if key in by_key:
                raise ValueError(f"two phases have the key {key!r}")
            by_key[key] = i
        by_title.setdefault(ph["title"].strip().lower(), []).append(i)
    try:
        from server.routes.review import HARNESS_APP_IDS as _known
    except Exception:  # noqa: BLE001 — the build door still refuses by name
        _known = ()
    edges: dict = {}
    last_in: dict = {}
    for i, ph in enumerate(titled):
        title = ph["title"].strip()
        raw = ph.get("depends_on")
        if raw is None:
            deps = [last_in[ph["_module"]]] if ph["_module"] in last_in else []
        else:
            if not isinstance(raw, list):
                raise ValueError(f"phase '{title}': depends_on must be a list of phase keys or titles")
            deps = []
            for ref in raw:
                r = str(ref).strip().lower() if isinstance(ref, str) else None
                if not r:
                    raise ValueError(
                        f"phase '{title}': depends_on names phases of this plan by key "
                        f"or title — {ref!r} is neither")
                if r in by_key:
                    j = by_key[r]
                elif len(by_title.get(r, [])) == 1:
                    j = by_title[r][0]
                elif r in by_title:
                    raise ValueError(
                        f"phase '{title}': depends_on {ref!r} matches several phases — "
                        "give them keys and depend on the key")
                else:
                    raise ValueError(
                        f"phase '{title}': depends_on {ref!r} is not a phase of this plan")
                if j == i:
                    raise ValueError(f"phase '{title}' cannot depend on itself")
                if j not in deps:
                    deps.append(j)
        ph["_deps"] = deps
        edges[i] = deps
        last_in[ph["_module"]] = i
        h = str(ph.get("harness") or "").strip() or None
        if h and _known and h not in _known:
            raise ValueError(
                f"phase '{title}': harness {h!r} is not a coding engine — one of "
                + ", ".join(_known))
        ph["_harness"] = h
        ph["_model"] = str(ph.get("model") or "").strip() or None
    cyc = _find_cycle(edges)
    if cyc:
        raise ValueError("the phases wait for each other in a circle, so none could "
                         "ever start: " + " → ".join(f"'{titled[i]['title'].strip()}'"
                                                     for i in cyc))


async def _persist_plan(pool, project_id: int, phases: list[dict],
                        module_ids: dict | None = None) -> dict:
    """Write phases + steps via the existing paths. Each step's domain is
    auto-classified and its execute model auto-filled from project routing +
    the codebook — the ported step intelligence. Returns the persisted plan.

    Raises ValueError on an acceptance/gate shape that cannot be honoured —
    the caller refuses the whole plan rather than landing a phase whose proof
    is malformed."""
    out_phases: list[dict] = []
    ids: list[int] = []
    for ph in phases:
        title = (ph.get("title") or "").strip()
        if not title:
            continue
        nxt = await pool.fetchval(
            "SELECT COALESCE(MAX(phase_number),0)+1 FROM project_phases WHERE project_id=$1",
            project_id)
        _brief = (ph.get("spec") or ph.get("description") or "").strip() or None
        # Normalized by _validate_plan when the plan door wrote this phase;
        # computed here for any other caller.
        _acc = ph["_acc"] if "_acc" in ph else _normalize_acceptance(ph.get("acceptance"))
        # A check that needs a tool no harness image ships is refused with
        # the plan, by name. A phase build does not refuse one
        # later: its checks are notes.
        _tool_ref = _acceptance_tool_refusal(_acc, None)
        if _tool_ref:
            raise ValueError(f"phase '{title}': {_tool_ref}")
        _gate = ph["_gate"] if "_gate" in ph else _normalize_gate(ph.get("gate"))
        _mid = (module_ids or {}).get(ph.get("_module")) if ph.get("_module") else None
        row = await pool.fetchrow(
            """INSERT INTO project_phases
                   (project_id, phase_number, title, description, spec, status,
                    acceptance, gate, module_id, validation, harness, model)
               VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12)
               RETURNING id, phase_number, title, status, gate""",
            project_id, nxt, title,
            (ph.get("description") or "").strip() or None,
            _brief,
            # Same rule the other three writers of this table already follow
            # (add_phase, REST create_phase, the MCP plan writer): WITH a brief
            # a phase is ready to build; without one it stays 'planning'
            # honestly rather than looking buildable. This door was the lone
            # exemption, marking every row ready unconditionally — so a phase
            # carrying no brief reached the build door, which then briefs the
            # harness by falling back through description to the TITLE.
            #
            # CORRECTED: when this landed, _compose_default_plan still
            # fabricated a brief per phase, so the draft kept landing 'ready'
            # and this was "no behaviour change". A later commit in the same
            # branch stripped those fabricated briefs, so the draft now lands
            # 'planning' too — which is the whole point of it, and this rule is
            # what makes that happen.
            _PHASE_READY if _brief else "planning",
            json.dumps(_acc) if _acc is not None else None,
            _gate, _mid, ph.get("_validation"),
            ph.get("_harness"), ph.get("_model"))
        phase_id = row["id"]
        ids.append(phase_id)
        out_steps: list[dict] = []
        for st in (ph.get("steps") or []):
            st_title = (st.get("title") or "").strip()
            if not st_title:
                continue
            st_desc = (st.get("description") or "").strip()
            domain_tag = (st.get("domain_tag") or "").strip() or None
            if not domain_tag:
                try:
                    from mastercode.domain_classifier import classify_domain
                    domain_tag = (classify_domain(f"{st_title} {st_desc}") or {}).get("domain")
                except Exception:
                    domain_tag = None
            execute_model = None
            if domain_tag:
                try:
                    from server.routes.projects import _resolve_model_for_domain
                    execute_model = await _resolve_model_for_domain(pool, project_id, domain_tag)
                except Exception:
                    execute_model = None
            snxt = await pool.fetchval(
                "SELECT COALESCE(MAX(step_number),0)+1 FROM phase_steps WHERE phase_id=$1",
                phase_id)
            srow = await pool.fetchrow(
                """INSERT INTO phase_steps
                       (phase_id, step_number, title, description, domain_tag, execute_model)
                   VALUES ($1,$2,$3,$4,$5,$6)
                   RETURNING id, step_number, title, status, domain_tag, execute_model""",
                phase_id, snxt, st_title, st_desc or None, domain_tag, execute_model)
            out_steps.append(dict(srow))
        out_phases.append({**dict(row), "steps": out_steps,
                           "acceptance_checks": len(_acc or []),
                           "module": ph.get("_module"),
                           "validation": ph.get("_validation"),
                           "harness": ph.get("_harness"),
                           "model": ph.get("_model")})
    # Dependencies, once every phase has its id. A
    # plan door phase carries `_deps` (_resolve_plan_dependencies); a phase
    # from any other caller keeps NULL — the phase before it in its module.
    titled = [ph for ph in phases if (ph.get("title") or "").strip()]
    for i, ph in enumerate(titled):
        if "_deps" not in ph:
            continue
        dep_ids = [ids[j] for j in ph["_deps"]]
        await pool.execute(
            "UPDATE project_phases SET depends_on=$1::int[] WHERE id=$2",
            dep_ids, ids[i])
        out_phases[i]["depends_on"] = dep_ids
    return {"phases": out_phases}


async def _set_phase_status(pool, phase_id: int, status: str) -> None:
    # NEVER overwrite a deliberate retirement. This wrote unconditionally, so a
    # phase archived while its build was in flight was silently un-archived the
    # moment that build landed and stamped complete/failed — the user's explicit
    # act reverted with no signal. The build's own
    # outcome is still recorded on its entity_tasks row and still delivered, so
    # nothing about the run is lost; what is preserved is the retirement.
    try:
        await pool.execute(
            "UPDATE project_phases SET status=$1 WHERE id=$2 AND status <> $3",
            status, phase_id, _PHASE_ARCHIVED)
    except Exception as e:
        log.warning("phase status update failed (non-fatal): %s", e)


async def _set_phase_steps_status(pool, phase_id: int, status: str) -> None:
    try:
        await pool.execute("UPDATE phase_steps SET status=$1 WHERE phase_id=$2", status, phase_id)
    except Exception as e:
        log.warning("step status update failed (non-fatal): %s", e)


async def _append_build_activity(pool, task_id: int, msg: str) -> None:
    """Append a lifecycle entry to the factory_build row's activity feed — the
    live surface the bench renders while the build runs (the pool's harness
    /run is one blocking call, so this coarse lifecycle feed + the post-run
    transcript are the harness-stream visibility; a token-level tail would need
    pod-side streaming). Best-effort."""
    from datetime import datetime as _dt, timezone as _tz
    try:
        entry = json.dumps([{"at": _dt.now(_tz.utc).isoformat(timespec="seconds"),
                             "event": "start", "tool": msg}])
        await pool.execute(
            "UPDATE entity_tasks SET result = jsonb_set(COALESCE(result,'{}'::jsonb),"
            "'{activity}', COALESCE(result->'activity','[]'::jsonb) || $1::jsonb) "
            "WHERE task_id=$2",
            entry, task_id)
    except Exception:
        pass


async def _compose_phase_intent(pool, prow) -> str:
    """The phase-scoped build intent: its spec plus its steps. Shared by the
    chat `build` action and the auto-advance chain so a hands-off phase is
    briefed EXACTLY like a hand-started one."""
    steps = await pool.fetch(
        "SELECT title, description FROM phase_steps WHERE phase_id=$1 ORDER BY step_number",
        prow["id"])
    step_lines = "\n".join(
        f"  - {s['title']}" + (f": {s['description']}" if s['description'] else "")
        for s in steps)
    phase_spec = (prow["spec"] or prow["description"] or prow["title"] or "").strip()
    return (
        f"Phase: {prow['title']}\n{phase_spec}\n\n"
        f"Steps to complete in this phase:\n{step_lines}"
        if step_lines else f"Phase: {prow['title']}\n{phase_spec}")


# Auto-advance backstop. The chain is already self-limiting — each link must
# flip a phase to 'complete' before the next is selected, and a completed phase
# can never be re-picked — so this only catches a pathological case where a
# build lands ok without completing its phase. Sized well past any real plan
# (the largest live plan is 11 phases) because a cap must never throttle
# legitimate work; it exists to stop a runaway, not to bound how big a plan
# the user is allowed to approve.
_AUTOADVANCE_MAX_CHAIN = 32


async def _chain_stop(pool, task_id: int | None, why: str) -> None:
    """Record WHY the chain stopped, on the task that just finished.

    A stop used to be a log line and nothing else: the user got "phase 5 done"
    and then silence, which is indistinguishable from the workflow crashing —
    the exact complaint auto-advance was built to answer ("it keeps stopping and
    leaving me to wonder whats happening"). The chain runs BEFORE _deliver, so a
    reason written here rides that delivery out to the user. Best-effort: a
    telemetry failure must never take down a build that succeeded."""
    if task_id is None:
        return
    try:
        await pool.execute(
            "UPDATE entity_tasks SET result = jsonb_set("
            "  COALESCE(result,'{}'::jsonb), '{chain_stopped}', to_jsonb($2::text), true"
            ") WHERE task_id = $1", task_id, why)
    except Exception as exc:
        log.warning("chain-stop note failed for task %s (non-fatal): %s", task_id, exc)


# The columns every phase selection reads: the build door, the chain and the
# seeded retry decide on the same row shape.
_PHASE_COLS = ("id, phase_number, title, spec, description, status, gate, approved_at, "
               "acceptance, module_id, depends_on, harness, model, integration_error")


def _row_get(row, key):
    """A column of an asyncpg Record or a dict stub; None when it is absent
    (a stub written before the column existed)."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _phase_deps(ph, rows) -> list[int]:
    """The phases `ph` waits for.

    Its own `depends_on` when the plan said — an empty list waits for nothing.
    Otherwise (NULL: every plan written before v175, and any phase the plan
    left unmarked) the phase before it IN ITS MODULE, retired ones skipped. So
    a plan with no dependencies builds in exactly the order it always did,
    phase by phase, and two modules never wait on each other: modules are
    independent by default, phases inside one are not."""
    own = _row_get(ph, "depends_on")
    if own is not None:
        return [int(d) for d in own]
    prev = None
    for r in rows:
        if (_row_get(r, "module_id") == _row_get(ph, "module_id")
                and r["phase_number"] < ph["phase_number"]
                and r["status"] != _PHASE_ARCHIVED
                and (prev is None or r["phase_number"] > prev["phase_number"])):
            prev = r
    return [prev["id"]] if prev is not None else []


def _schedule_phases(rows, *, unlocked_by: int | None = None):
    """Which phases can start NOW — the scheduler auto-advance became.

    Returns (startable, held). A phase is considered when it is open and not
    already building; with `unlocked_by`, only when it depends on that phase
    (what a finishing phase may start — never a phase that does not follow
    from it; see _autoadvance_pick_next). It STARTS when every dependency is
    complete (or retired, or gone with a superseded plan), it has a brief
    (ready, or failed = a retry of an approved phase), it is not waiting for
    the user's approval, and its last run did not collide with another
    phase's (integration_error). Otherwise it is HELD, with (row, why, detail):
    'waiting' (detail = the rows it waits for), 'no_brief', 'approval',
    'conflict'. Pure — no I/O — so the graph is testable on its own.

    A dependency in 'review' (an older phase that landed when a landed phase
    could still wait there) is met: it landed."""
    met = (_PHASE_DONE, _PHASE_ARCHIVED, _PHASE_REVIEW)
    by_id = {r["id"]: r for r in rows}
    start: list = []
    held: list = []
    for ph in sorted(rows, key=lambda r: r["phase_number"]):
        if ph["status"] in (_PHASE_DONE, _PHASE_ARCHIVED, _PHASE_REVIEW, _PHASE_RUNNING):
            continue
        deps = _phase_deps(ph, rows)
        if unlocked_by is not None and int(unlocked_by) not in deps:
            continue
        waiting = [by_id[d] for d in deps if d in by_id and by_id[d]["status"] not in met]
        if waiting:
            held.append((ph, "waiting", waiting))
        elif ph["status"] not in (_PHASE_READY, _PHASE_FAILED):
            held.append((ph, "no_brief", None))
        elif _phase_awaits_approval(ph):
            held.append((ph, "approval", None))
        elif _row_get(ph, "integration_error"):
            held.append((ph, "conflict", None))
        else:
            start.append(ph)
    return start, held


def _held_text(ph, why: str, detail) -> str:
    """One line saying why a phase is not starting, in the host's terms."""
    name = f"phase {ph['phase_number']} ('{ph['title']}')"
    if why == "waiting":
        on = ", ".join(f"phase {w['phase_number']} ('{w['title']}', {w['status']})"
                       for w in detail)
        return f"{name} waits for {on}"
    if why == "no_brief":
        if (_row_get(ph, "spec") or "").strip():
            # A written brief nobody has readied yet (a phase brought in by a
            # ticket import lands 'planning' with its spec on purpose).
            return (f"{name} is '{ph['status']}': its brief is written but it has not been "
                    "readied, so nothing builds it yet — readying it (going through the "
                    "brief) comes first: do it when the user's request covers it, otherwise "
                    "say so; do NOT say it is running")
        return (f"{name} is '{ph['status']}' and has no brief, so nothing can build it "
                "yet — writing its spec comes first: do it when the user's request covers "
                "it, otherwise say so; do NOT say it is running")
    if why == "approval":
        return (f"{name} is waiting for the user's approval in the Workbench (the "
                "phase shows an Approve button); nothing builds it until they approve")
    if why == "conflict":
        return (f"{name} did not land: its last run collided with another phase's "
                "work — re-split it (depends_on) or build it by name")
    return name


async def _autoadvance_pick_next(*, pool, project_id: int, chain_depth: int,
                                 finished_task_id: int | None = None,
                                 finished_phase_id: int | None = None) -> list:
    """DECIDE what an ALREADY-APPROVED plan starts next: every phase the one
    that just finished unlocks, together ("start
    everything whose dependencies are met"). Returns the phase rows to start,
    [] when none.

    Split from the dispatch half (_autoadvance_start_phase) for one reason: the
    stop REASON must ride the finishing phase's delivery, while the next build
    must not START until that phase is committed and reported. So this half runs
    before _deliver and writes _chain_stop; the other half runs after it.

    The workflow should be autonomous, not stop and leave the user wondering
    what is happening. The approval gate stays exactly where it is (tools.py header: align → plan → show → yes → build) — the
    user still approves the PLAN before anything builds. On a successful phase
    what follows from it starts by itself, and the user is still told after
    each (every build wakes the host through AgentDispatch._deliver).

    FORWARD, never back. The chain starts only phases that depend on the one
    that finished. The old selection was the lowest incomplete phase in the
    project; measured, the user built phase
    6 and the chain then ran phases 3 and 4 — backwards into leftovers of a
    restructure. A phase that could build but does not follow from this one is
    named in the stop reason and left for the user to decide.

    Stops (writes the reason) only when NOTHING else of the project is still
    building: while a parallel phase runs, the chain has not stopped — that
    phase's own finish decides again, and the last one to land explains
    whatever is left. Also stops on the depth backstop and the halt flag.
    Fail-soft throughout — a problem here must never corrupt the phase that
    just succeeded."""
    from mastercode.runtime import user_limits as _ul
    if chain_depth >= _ul.person_or("build.chain_phases", _AUTOADVANCE_MAX_CHAIN):
        log.warning("auto-advance: chain depth %d reached on project %d — stopping",
                    chain_depth, project_id)
        from server import brain_service as _bs
        await _ul.record_hit(pool, _bs._entity_id, "build.chain_phases",
                             f"project {project_id}'s phases advancing by themselves")
        await _chain_stop(pool, finished_task_id, (
            f"Auto-advance stopped after {chain_depth} consecutive phases. Remaining "
            "phases have not been abandoned; whether to go on is the user's call — put "
            "it in the question box (escalate). " + _ul.hit_line("build.chain_phases")))
        return []
    try:
        # The brake, checked at the phase boundary — the ONE point where this
        # chain is interruptible, since the running build itself cannot be
        # truthfully cancelled. Read before any work so a halt requested during
        # the previous phase is honoured the instant that phase lands.
        if await pool.fetchval(
            "SELECT COALESCE((config->>'build_chain_halt')::boolean, false) "
            "FROM projects WHERE id=$1", project_id,
        ):
            log.info("auto-advance: halt flag set on project %d — chain stops "
                     "here (the finished phase is delivered as normal)",
                     project_id)
            await _chain_stop(pool, finished_task_id, (
                "Auto-advance is halted for this project because a stop was "
                "requested. Nothing further starts until the plan is resumed "
                "(after a stop, a build of the project resumes it)."))
            return []

        rows = await pool.fetch(
            f"SELECT {_PHASE_COLS} FROM project_phases WHERE project_id=$1 "
            "ORDER BY phase_number", project_id)
        start, _held = _schedule_phases(rows, unlocked_by=finished_phase_id)
        if start:
            return start

        # Nothing follows from this phase. If another phase of the project is
        # still building, the chain is not over: its finish decides again.
        in_flight = await pool.fetchval(
            "SELECT COUNT(*) FROM entity_tasks WHERE task_type='factory_build' "
            "AND status='in_progress' AND (result->>'project_id')::int = $1 "
            "AND task_id <> $2", project_id, int(finished_task_id or 0))
        if in_flight:
            log.info("auto-advance: nothing follows phase %s on project %d; %d "
                     "other build(s) still running — they decide next",
                     finished_phase_id, project_id, in_flight)
            return []

        # Nothing running and nothing starting: say why for every open phase,
        # so "phase done" is never followed by silence.
        free, held = _schedule_phases(rows)
        await _raise_phase_gate_tickets(
            pool, None, project_id, [ph for ph, why, _d in held if why == "approval"])
        lines = [_held_text(ph, why, d) for ph, why, d in held]
        if free:
            names = ", ".join(f"phase {p['phase_number']} ('{p['title']}')" for p in free)
            lines.insert(0, (
                f"{names} could build but do{'es' if len(free) == 1 else ''} not follow "
                "from the phase that just finished, so the chain did not start "
                f"{'it' if len(free) == 1 else 'them'} — going there on its own could "
                "rebuild ground the user skipped on purpose, and such phases are often "
                "leftovers from a plan that was restructured. Tell the user which "
                "phases are outstanding; whether to build or drop them is the user's "
                "call — put it in the question box (escalate: build them, or drop them)"))
        if lines:
            await _chain_stop(pool, finished_task_id, (
                "Auto-advance stopped — nothing further starts on its own:\n- "
                + "\n- ".join(lines)))
        return []
    except Exception as e:
        log.warning("auto-advance failed for project %d (non-fatal): %s",
                    project_id, e, exc_info=True)
        return []


async def _claim_phase(pool, prow) -> bool:
    """Take a phase for ONE build: flip it to in_progress only if it is still
    in the status it was read in. Two parallel phases can finish at the same
    moment and both unlock the same next one; exactly one claim wins, so it
    builds once. The caller releases the claim if the build never starts."""
    got = await pool.fetchval(
        "UPDATE project_phases SET status=$1 WHERE id=$2 AND status=$3 "
        "AND status IN ($4,$5) RETURNING id",
        _PHASE_RUNNING, prow["id"], prow["status"], _PHASE_READY, _PHASE_FAILED)
    return got is not None


async def _release_phase_claim(pool, phase_id: int, prior: str) -> None:
    """Undo _claim_phase for a build that never started (fail-soft)."""
    try:
        await pool.execute(
            "UPDATE project_phases SET status=$1 WHERE id=$2 AND status=$3",
            prior, phase_id, _PHASE_RUNNING)
    except Exception as e:  # noqa: BLE001
        log.warning("phase %s claim release failed (non-fatal): %s", phase_id, e)


async def _running_phase_text(pool, prow) -> str:
    """What the door says about a phase marked in_progress: building, when a
    live build of it exists; otherwise the truth — its build ended without
    closing it (the monitor fails such a phase when it reaps the build)."""
    name = f"phase {prow['phase_number']} ('{prow['title']}')"
    try:
        live = await pool.fetchval(
            "SELECT COUNT(*) FROM entity_tasks WHERE task_type='factory_build' "
            "AND status='in_progress' AND (result->>'phase_id')::int = $1", prow["id"])
    except Exception:  # noqa: BLE001 — say the conservative thing
        live = 1
    if live:
        return f"{name} is already building — I'll report it here when it lands"
    return (f"{name} is still marked in progress but no build of it is running — "
            "a build ended without closing it. Nothing starts it on its own: a "
            "kernel restart marks it failed (the boot reap), or a new plan replaces "
            "it. Tell the user")


def _phase_engine(prow, harness: str | None, model: str | None) -> tuple:
    """(harness, model) a phase builds on: its own when the plan named one
    (each task its own engine), otherwise the build's.
    A phase that names only a model runs it on the build's engine."""
    own_h = (str(_row_get(prow, "harness") or "").strip() or None)
    own_m = (str(_row_get(prow, "model") or "").strip() or None)
    if own_h:
        return own_h, own_m
    return harness, (own_m or model)


def _phase_awaits_approval(prow) -> bool:
    """gate='user' with no approved_at. Tolerates rows/stubs without the v124
    columns (a pre-migration read) by treating them as ungated — a missing
    column can never invent a gate, and never hide one that exists."""
    try:
        gate = prow.get("gate") if hasattr(prow, "get") else prow["gate"]
        approved = prow.get("approved_at") if hasattr(prow, "get") else prow["approved_at"]
    except (KeyError, TypeError):
        return False
    return gate == _PHASE_GATE_USER and not approved


async def start_approved_phase(pool, entity_id: int, project_id: int, phase_id: int) -> dict:
    """The user approved a gated phase — on its ticket or with the Workbench's
    Approve button. That click IS the go-ahead: nothing is left to type
    (a user should never read 'say go'). The
    card used to say "Say build to start it".

    When the phase's turn has come — it has a brief, it is not building,
    every phase it waits for has landed, and nobody stopped the plan — it
    starts NOW through the build door, the same engine choice, claim and
    delivery as the Host's own build(action='build', phase_id=…); its result
    comes back through deliver_agent_result like any build (a woken turn
    carries on). Otherwise the plan starts it when its turn comes, as
    auto-advance already does for an approved phase. Never builds ahead of
    what the phase waits for. Never raises: the approval stands either way.
    Returns {started, why | result}."""
    try:
        if await pool.fetchval(
                "SELECT COALESCE((config->>'build_chain_halt')::boolean, false) "
                "FROM projects WHERE id=$1", int(project_id)):
            return {"started": False, "why": "the plan is stopped; it starts when the plan is resumed"}
        rows = await pool.fetch(
            f"SELECT {_PHASE_COLS} FROM project_phases WHERE project_id=$1 "
            "ORDER BY phase_number", int(project_id))
        by = {r["id"]: r for r in rows}
        prow = by.get(int(phase_id))
        if prow is None:
            return {"started": False, "why": "no such phase"}
        if prow["status"] not in (_PHASE_READY, _PHASE_FAILED):
            return {"started": False, "why": f"the phase is {prow['status']}"}
        if _phase_awaits_approval(prow):
            return {"started": False, "why": "the phase is not approved"}
        waits = [by[d] for d in _phase_deps(prow, rows)
                 if d in by and by[d]["status"] not in (_PHASE_DONE, _PHASE_ARCHIVED, _PHASE_REVIEW)]
        if waits:
            return {"started": False, "why": (
                "it starts when " + ", ".join(f"phase {w['phase_number']} ('{w['title']}')"
                                              for w in waits) + " lands")}
        res = await _exec_build({"action": "build", "project_id": int(project_id),
                                 "phase_id": int(phase_id)},
                                entity_id, pool, None, f"project:{int(project_id)}")
        ok = isinstance(res, dict) and res.get("status") == "ok"
        return {"started": ok, "result": (res or {}).get("result")}
    except Exception as e:  # noqa: BLE001 — the approval already stands
        log.warning("start_approved_phase(project %s, phase %s) failed: %s",
                    project_id, phase_id, e)
        return {"started": False, "why": f"could not start it: {str(e)[:200]}"}


async def continue_plan(pool, entity_id: int, project_id: int) -> dict:
    """The user completed a phase that landed in 'review' (the Workbench's
    Approve on it — the user IS the acceptance when no command is). That
    click goes on with the plan: every phase that can start now starts
    through the build door, as when a phase completes on its own — the chain
    stop texts used to end "then say build to continue". Nothing starts over
    a stop the user set; a gated phase next in line raises its card. Never
    raises. Returns {started, why | result}."""
    try:
        if await pool.fetchval(
                "SELECT COALESCE((config->>'build_chain_halt')::boolean, false) "
                "FROM projects WHERE id=$1", int(project_id)):
            return {"started": False, "why": "the plan is stopped; it goes on when the plan is resumed"}
        res = await _exec_build({"action": "build", "project_id": int(project_id)},
                                entity_id, pool, None, f"project:{int(project_id)}")
        ok = isinstance(res, dict) and res.get("status") == "ok"
        return {"started": ok, "result": (res or {}).get("result")}
    except Exception as e:  # noqa: BLE001 — the completion already stands
        log.warning("continue_plan(project %s) failed: %s", project_id, e)
        return {"started": False, "why": f"could not go on: {str(e)[:200]}"}


# ── The join to Circles tickets ─────────────────────────────────
# One ticket system, two shapes: a phase is the engineering ticket, a Circles
# ticket is the operations one, joined through the ticket's metadata
# (server/routes/tickets.py — on_phase_outcome, raise_phase_gate_ticket). Both
# helpers are fail-soft: the build path never fails because a card did not.

async def _ticket_phase_outcome(pool, entity_id, phase_id, status, out) -> None:
    """A phase landed: tell every open ticket joined to it (metadata.phase_id).
    'complete' decides the ticket with 'built'; anything else is recorded on
    the card and the ticket stays open."""
    try:
        from server.routes.tickets import on_phase_outcome
        res = out.get("result") if isinstance(out, dict) else None
        if isinstance(res, dict):
            note = str(res.get("summary") or res.get("message") or "")
        else:
            note = str(res or "")
        await on_phase_outcome(pool, entity_id, phase_id=int(phase_id),
                               status=str(status), note=note[:2000])
    except Exception as e:  # noqa: BLE001
        log.warning("ticket join: phase %s outcome not delivered (non-fatal): %s", phase_id, e)


async def _raise_phase_gate_tickets(pool, entity_id, project_id, phases) -> list:
    """The plan is waiting on the user's approval of these phases: make sure
    each has its ONE open ticket in Circles (idempotent in the service), so the
    approval is reachable from the doorbell and the Tickets pane, not only from
    the Workbench plan pane. Returns the service's answer per phase (a phase
    the user SENT BACK and has not changed since answers with `sent_back` =
    their note and raises nothing); [] on any failure."""
    if not phases:
        return []
    out: list = []
    try:
        if entity_id is None:
            from server import brain_service
            entity_id = brain_service._entity_id
        from server.routes.tickets import raise_phase_gate_ticket
        for ph in phases:
            out.append(await raise_phase_gate_ticket(
                pool, entity_id, project_id=int(project_id), phase_id=int(ph["id"])))
    except Exception as e:  # noqa: BLE001
        log.warning("ticket join: gate ticket not raised on project %s (non-fatal): %s",
                    project_id, e)
    return out


# ── What a FAILED build row counts as ──────────────
# A phase's retry budget (retry once, then stop) and its
# automatic repairs are spent by the WORK failing: a harness ran and did not
# land the phase. A failed row is not always that. Nothing structured says who
# failed, so the platform's own failures are recognised by the words each
# writer stores in entity_tasks.error, matched at the START of the text, where
# every one of these writers puts them. A harness's own stderr that merely
# mentions one of them further in is still the work's failure.
#   wake        review._dispatch_to_pod_harness "harness wake failed: ..." (the
#               operator's "not Ready within 60s" and a plan refusal ride
#               inside it); review._await_routable "harness <h> never became
#               routable in 30s: ..."
#   connection  review._run_on_pod "harness <h> pod connection failed after
#               ..."; rows from before that wording: aiohttp's bare "Server
#               disconnected"
#   stream      review._run_on_pod "the live stream died mid-run after ..."
#   collect     review._run_on_pod "the run finished, but its files could not
#               be collected from the harness: ..."
#   kernel      agent_dispatch's boot reap "kernel restarted mid-run — ...";
#               the monitor's "build stuck (kernel restarted or pod run
#               wedged)"; _run_phase_build's "build crashed: ..." (the
#               kernel's own code raised)
#   not run     _run_phase_build's "no build ran: ..."
# A row stamped acceptance_refused, links_refused or integration_conflict is
# neither: it was refused before a pod woke, or the plan's split put its work
# on top of another phase's. Those count toward nothing, as before. Nor does
# a row stamped killed_by (review.kill_session_runs): the person or the Host
# stopped it, and a stop spends neither the work's retry nor its repairs.
_PLATFORM_FAILURE_RE = re.compile(
    r"\s*(?:"
    r"harness wake failed\b"
    r"|harness \S+ never became routable\b"
    r"|harness \S+ pod connection failed after\b"
    r"|Server disconnected(?:\s+—|\s*$)"
    r"|the live stream died mid-run\b"
    r"|the run finished, but its files could not be collected from the harness\b"
    r"|kernel restarted mid-run\b"
    r"|build stuck \(kernel restarted or pod run wedged\)"
    r"|build crashed:"
    r"|no build ran:"
    r")")


def _is_platform_failure(error) -> bool:
    """The failure text is the platform's (or nothing ran), not the work's."""
    return bool(_PLATFORM_FAILURE_RE.match(str(error or "")))


def _failed_build_kind(row) -> str:
    """What one FAILED factory_build row counts as, the one rule the retry
    budget (_phase_retry_pick) and the automatic repairs (phase_repair_refusal)
    both count by: 'refused' (counts toward nothing), 'stopped' (killed —
    counts toward nothing), 'platform' (the platform failed, or nothing ran)
    or 'work' (a harness ran and failed on the work). `row` carries error,
    acceptance_refused, links_refused, integration_conflict and killed
    (_phase_failed_builds)."""
    def _get(key):
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return None
    if _get("acceptance_refused") or _get("links_refused") or _get("integration_conflict"):
        return "refused"
    if _get("killed"):
        return "stopped"
    if _is_platform_failure(_get("error")):
        return "platform"
    return "work"


async def _phase_failed_builds(pool, entity_id: int, project_id: int, phase_id: int) -> list:
    """The phase's FAILED build rows, shaped for _failed_build_kind."""
    return list(await pool.fetch(
        "SELECT error, "
        "COALESCE((result->>'acceptance_refused')::boolean, false) AS acceptance_refused, "
        "COALESCE((result->>'links_refused')::boolean, false) AS links_refused, "
        "(result ? 'integration_conflict') AS integration_conflict, "
        "(result ? 'killed_by') AS killed "
        "FROM entity_tasks WHERE entity_id=$1 AND task_type='factory_build' "
        "AND status='failed' "
        "AND (result->>'project_id')::int = $2 AND (result->>'phase_id')::int = $3",
        entity_id, project_id, phase_id) or [])


def _first_line(text, limit: int = 300) -> str:
    line = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[:limit] + " …"


# A phase's AUTOMATIC repairs. A turn
# the platform wakes for a finished build may carry the plan on — build the
# next phase whose dependencies landed — and it may rebuild the SAME phase after
# it landed without completing (a blocking or unproven review, a failure): that
# is a repair. Nothing bounded those: a reviewer that blocks every time drove
# wake -> rebuild -> block -> wake for ever, each lap a harness build, a review
# and a Host turn, with nobody watching. Two automatic repairs per phase (the
# same budget as a shipped app's proof repairs, app_verify.MAX_AUTO_REPAIRS);
# past them the build door refuses a rebuild from a turn nobody is in, and the
# Host asks in the question box, with the findings. Counted from the phase's
# build rows — the record of what landed — so a success elsewhere never resets
# it. A person who asks for another build (their message, or their answer read
# into the turn) is not refused: the budget is for automatic work only.
PHASE_AUTO_REPAIRS = 2


def _turn_is_woken() -> bool:
    """The turn calling the door was started by the platform and no person's
    words have been read into it (memory_engine.CURRENT_TURN_WOKEN)."""
    try:
        from mastercode.memory_engine import CURRENT_TURN_WOKEN
        cur = CURRENT_TURN_WOKEN.get()
        return isinstance(cur, dict) and cur.get("woken") is True
    except Exception:  # noqa: BLE001
        return False


async def phase_repair_refusal(pool, entity_id: int, project_id: int, prow) -> dict | None:
    """The build door's refusal of an AUTOMATIC rebuild of a phase whose
    repairs are spent, or None (not a woken turn, or repairs left)."""
    if not _turn_is_woken():
        return None
    try:
        row = await pool.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE status = 'completed' "
            "AND result->>'phase_status' = 'review') AS reviews "
            "FROM entity_tasks WHERE entity_id = $1 AND task_type = 'factory_build' "
            "AND (result->>'project_id')::int = $2 AND (result->>'phase_id')::int = $3",
            entity_id, project_id, prow["id"])
        # Failed rows count by the same rule as the retry budget: only the
        # work failing spends a repair (_failed_build_kind).
        kinds = [_failed_build_kind(r) for r in
                 await _phase_failed_builds(pool, entity_id, project_id, prow["id"])]
    except Exception as e:  # noqa: BLE001 — the budget fails toward building
        log.warning("phase repairs: count failed for phase %s (non-fatal): %s", prow["id"], e)
        return None
    def _n(key: str) -> int:
        try:
            return int((row[key] if row is not None else 0) or 0)
        except (KeyError, TypeError, IndexError, ValueError):
            return 0
    reviews, fails = _n("reviews"), kinds.count("work")
    platform_fails = kinds.count("platform")
    landed = reviews + fails
    from mastercode.runtime import user_limits as _ul
    _repairs = _ul.person_or("build.auto_repairs", PHASE_AUTO_REPAIRS)
    if landed < 1 + _repairs:
        if platform_fails >= 1 + _repairs:
            # The platform kept failing this phase. That is not the work's
            # failure and spends none of its repairs, but a woken turn must
            # not go on rebuilding into it unattended either.
            log.info("phase repairs: phase %s could not be run %d times on the platform's "
                     "side — refused in a woken turn", prow["id"], platform_fails)
            return {"status": "error", "reason": "platform_failing", "result": (
                f"phase {prow['phase_number']} ('{prow['title']}') could not be run "
                f"{platform_fails} times, each time for a reason on the platform's side, "
                "not in the work, and this turn was started by the platform with nobody in "
                "it. None of those counted as the build failing and none used its repairs. "
                "Do NOT build it again from here. Tell the user plainly that the platform "
                "could not run this phase and that nothing in their project caused it, in "
                "the question box (escalate), with what stopped it, and these options: try "
                "it again; or leave it. When the answer comes back into the turn, act on it.")}
        return None
    log.info("phase repairs: phase %s landed without completing %d times — the automatic "
             "repairs are spent; refused in a woken turn", prow["id"], landed)
    await _ul.record_hit(pool, entity_id, "build.auto_repairs",
                         f"a repair of phase {prow['phase_number']} of project {project_id}")
    return {"status": "error", "reason": "repairs_spent", "result": (
        f"phase {prow['phase_number']} ('{prow['title']}') has landed without completing "
        f"{landed} times ({reviews} in review, {fails} failed), and this turn was started "
        f"by the platform with nobody in it: the phase's {_repairs} automatic "
        "repairs are spent. Do NOT build it again from here. Put it in the question box "
        "(escalate), with what blocked it each time, and these options: build it once "
        "more; approve it as it is (Approve on the phase in the Workbench); or leave it. "
        "When the answer comes back into the turn, act on it. "
        + _ul.hit_line("build.auto_repairs"))}


async def _phase_retry_pick(*, pool, entity_id: int, project_id: int,
                            phase_id: int, failure: str, chain_depth: int,
                            finished_task_id: int | None, killed: bool = False):
    """DECIDE whether a FAILED phase gets its one seeded retry, or the chain
    stops here. Returns (phase_row, seed_text) to retry, or None to stop.

    The choice is "retry the phase once, then stop": feed the failure
    back to the same harness for a single seeded rerun; if it still fails, halt
    and report. Before this a failed phase just ended the chain silently — the
    `if ok:` guard on auto-advance meant nothing further happened and nothing
    said why.

    CHAINED BUILDS ONLY. The retry exists because an autonomous chain has
    nobody watching it: a phase fails at 3am and without this the plan just
    stops. A build the USER started by hand has the opposite property — they are
    sitting there, and spending a second harness run on their behalf without
    asking is a cost decision that is theirs, not ours. They can say "try
    again". `chain_depth` is the discriminator and it is exact: it defaults to 0
    (_run_phase_build) and both user-initiated call sites take that default;
    only _autoadvance_start_phase passes chain_depth + 1. So >= 1 means, by
    construction, "the chain started this build, not a person".
    Demonstrated concretely by the smoke factory-build gate, whose zero-files scenario is a one-off manual
    build — it spent an unrequested second harness run and raced the gate's
    own assertion.

    Split decide/start for the SAME reason the auto-advance pair is split: the
    stop reason must be written before _deliver so it rides that delivery out to
    the user, while the retry build must not start until the failure has been
    reported. Fail-soft — a problem here must never mask the failure itself.

    The attempt count comes from the build rows themselves (entity_tasks, whose
    result carries phase_id — _factory_build_task_start), not from a marker we
    maintain: the rows ARE the record of how many times this phase was built, so
    the count cannot drift out of sync with reality or survive a re-plan.
    """
    try:
        # Hand-started build: report the failure and stop. No _chain_stop note —
        # there is no chain to explain the end of, and the user is right there.
        if chain_depth < 1:
            log.info("phase retry: project %d phase %d failed on a hand-started "
                     "build (chain_depth=0) — not retrying; the user decides",
                     project_id, phase_id)
            return None
        # KILLED (review.kill_session_runs): the person or the Host stopped
        # it. Nothing restarts what a kill stopped — not even a run of
        # another project that shared the killed run's pod, whose own project
        # is not halted. No chain-stop note: the delivery states the kill.
        if killed:
            log.info("phase retry: project %d phase %d was killed — not retrying",
                     project_id, phase_id)
            return None
        if await pool.fetchval(
            "SELECT COALESCE((config->>'build_chain_halt')::boolean, false) "
            "FROM projects WHERE id=$1", project_id,
        ):
            await _chain_stop(pool, finished_task_id, (
                "The phase failed and auto-advance is halted for this project, "
                "so no retry was started. Report the failure; what to do next is "
                "the user's call — put it in the question box (escalate)."))
            return None

        # FAILED builds, not all builds. Counting every row made the stop
        # reason lie: a phase built cleanly once and then failed on a later
        # rebuild reported "Phase failed twice (2 builds)" when it had failed
        # once. This build's own row is already terminal here — the finish
        # write (_factory_build_task_finish) runs before this decision — so the
        # current failure is included, and the budget reads: 1 = retry it,
        # 2 = it has now failed twice, stop.
        # A row closed by the acceptance TOOL GATE (result.acceptance_refused)
        # is not an attempt: no pod woke, nothing was built. Counting it would
        # spend one of the two on a refusal, so the phase's first real failure
        # after the host rewrote the check would read "failed twice". Nor is a
        # failure on the platform's side (a wake that failed, a dropped pod
        # connection, a lost stream, the kernel's own crash): the work did not
        # fail, so it does not spend the work's retry (_failed_build_kind).
        kinds = [_failed_build_kind(r) for r in
                 await _phase_failed_builds(pool, entity_id, project_id, phase_id)]
        attempts = kinds.count("work")
        platform_fails = kinds.count("platform")
        this_platform = _is_platform_failure(failure)
        if attempts >= 2:
            await _chain_stop(pool, finished_task_id, (
                f"This phase has now FAILED {attempts} times and was NOT "
                "retried again — the retry budget for a phase is one. Do not "
                "start another build. Tell the user plainly that this phase is "
                "stuck and show what failed; how to go on is the user's call — put "
                "it in the question box (escalate), with what failed."))
            return None
        if this_platform and platform_fails >= 2:
            # The same one retry, counted on its own: the platform failing
            # this phase twice stops the chain and says it was the platform.
            await _chain_stop(pool, finished_task_id, (
                f"This phase could not be run {platform_fails} times, each time for a "
                "reason on the platform's side, not in the work (the last: "
                f"{_first_line(failure)}). None of those counted as the build failing "
                "and none used its retry. Do not start another build. Tell the user "
                "plainly that the platform could not run this phase and that nothing "
                "in their project caused it; how to go on is the user's call — put it "
                "in the question box (escalate), with what stopped it."))
            return None

        prow = await pool.fetchrow(
            f"SELECT {_PHASE_COLS} FROM project_phases WHERE id=$1", phase_id)
        if prow is None:
            return None

        if this_platform:
            seed = (
                "The previous attempt at this phase was stopped by the platform, "
                "not by anything in the work, so build it as briefed.\n\n"
                "--- what stopped it ---\n"
                f"{(failure or '').strip()[:4000]}\n"
                "--- end ---")
        else:
            seed = (
                "This phase was already attempted once and FAILED. Below is what "
                "went wrong. Read it first, fix the cause, and do not repeat the "
                "same approach.\n\n--- previous attempt's failure ---\n"
                f"{(failure or '(no output captured)').strip()[:4000]}\n"
                "--- end ---")
        log.info("phase retry: project %d phase %d failed (%d work failure(s), %d on the "
                 "platform's side, this one %s) — retrying once, seeded with the failure",
                 project_id, phase_id, attempts, platform_fails,
                 "the platform's" if this_platform else "the work's")
        return prow, seed
    except Exception as e:
        log.warning("phase-retry decision failed for project %d (non-fatal): %s",
                    project_id, e, exc_info=True)
        return None


async def _engine_counts(harnesses) -> tuple:
    """(counts, keys): how many of these builds run on each engine, and each
    build's key, by the engine each will ACTUALLY run on: a phase that
    names none is
    counted under the one resolve_build_target — the same resolution the
    build itself makes — picks. Parallel phases on one engine share its pod,
    so each one's wake asks the plan to hold this many runs at once. A
    resolution that fails counts under the raw name, which can only
    undercount (fewer slots asked, never more)."""
    counts: collections.Counter = collections.Counter()
    keys = []
    for h in harnesses:
        key = h
        if not key:
            try:
                from server.routes.review import resolve_build_target
                key = (await resolve_build_target(None))[0]
            except Exception as e:  # noqa: BLE001 — a count, never the build
                log.debug("engine resolution for the slot count failed: %s", e)
        keys.append(key)
        counts[key] += 1
    return counts, keys


def _launch_phase_build(tid: int, kwargs: dict) -> None:
    """Detach one phase build (_run_phase_build) and track it."""
    task = asyncio.create_task(_run_phase_build(**kwargs))
    _FACTORY_BUILD_TASKS[tid] = task
    task.add_done_callback(lambda _t, k=tid: _FACTORY_BUILD_TASKS.pop(k, None))


async def _autoadvance_start_phase(prow, *, entity_id: int, pool,
                                   session_id: str | None, project_id: int,
                                   interaction_context: str | None,
                                   research_id: str | None, timeout_s: int,
                                   harness: str | None, model: str | None,
                                   effort: str | None, chain_depth: int,
                                   seed: str | None = None,
                                   engine_default: tuple | None = None,
                                   pod_slots: int | None = None,
                                   launch: bool = True):
    """START a phase _autoadvance_pick_next chose (called once per phase it
    returned — parallel phases start together), or the seeded retry.

    `harness`/`model` are the engine of the build that just finished. A retry
    runs on exactly that engine; a chained phase runs on its own when the plan
    named one, else on `engine_default` — the engine the plan's build was
    started with, carried down the chain so one phase's own engine is never
    inherited by the next.

    The phase is CLAIMED before its row opens (_claim_phase): two parallel
    phases finishing together can both unlock it, and it must build once.

    Runs AFTER the finishing phase's sync_project_repo and _deliver. It used to
    run before both: measured, the next build opened
    147 ms after the previous run's row was written, while that phase's work was
    still being committed. _run_phase_build begins with prepare_workspace(),
    which fast-forwards the project directory, so the new phase's prep raced the
    finishing phase's commit on the SAME directory.

    Fail-soft: a problem starting the chain must never retroactively spoil the
    phase that just succeeded.

    `launch=False` claims the phase and opens its row but does not start the
    build: it returns (task_id, the _run_phase_build kwargs), or None when
    nothing was claimed, so the caller can count the phases that really start
    per engine before any of them wakes a pod.
    """
    try:
        scoped_intent = await _compose_phase_intent(pool, prow)
        # `seed` is set only by the retry path: the previous attempt's failure,
        # appended so the harness starts from what went wrong instead of the
        # identical brief that already failed once.
        if seed:
            scoped_intent = f"{scoped_intent}{_RETRY_SEED_MARK}{seed}"
        _what = "retry" if seed else "auto-advance"
        origin = {
            "entity_id": entity_id,
            "session_id": session_id,
            "app_id": "workbench",
            "project_id": project_id,
            "interaction_context": interaction_context,
            "origin_request": (
                f"{_what}: build phase {prow['phase_number']} "
                f"('{prow['title']}') of project {project_id}"),
        }
        # Re-read the halt at the LAST instant before the row exists. Selecting
        # a phase and composing its brief costs several DB roundtrips, and a
        # stop landing inside that window would otherwise start one more phase
        # after the user asked for none. This does not remove the race — the
        # flag can still land between this read and the insert — but it shrinks
        # it from "the whole selection path" to a single statement.
        if await pool.fetchval(
            "SELECT COALESCE((config->>'build_chain_halt')::boolean, false) "
            "FROM projects WHERE id=$1", project_id,
        ):
            log.info("auto-advance: halt arrived while composing phase %d on "
                     "project %d — not starting it", prow["id"], project_id)
            return

        if seed:
            _h, _m = harness, model
        else:
            _h, _m = _phase_engine(prow, *(engine_default or (harness, model)))
        if not await _claim_phase(pool, prow):
            log.info("auto-advance: phase %d on project %d was taken by another "
                     "build — not starting it twice", prow["id"], project_id)
            return
        tid = await _factory_build_task_start(
            pool, entity_id,
            title=(f"{'retry' if seed else 'build'} phase "
                   f"{prow['phase_number']}: {prow['title']}")[:200],
            intent=scoped_intent,
            project_id=project_id, origin=origin, phase_id=prow["id"],
            phase_number=prow["phase_number"], phase_title=prow["title"],
            timeout_s=timeout_s, harness=_h, model=_m)
        if tid is None:
            log.warning("auto-advance: could not open the registry row for "
                        "project %d phase %d", project_id, prow["id"])
            await _release_phase_claim(pool, prow["id"], prow["status"])
            return
        kwargs = dict(
            entity_id=entity_id, pool=pool, session_id=session_id,
            task_id=tid, project_id=project_id, phase_id=prow["id"],
            intent=scoped_intent, research_id=research_id, timeout_s=timeout_s,
            harness=_h, model=_m, effort=effort,
            interaction_context=interaction_context,
            chain_depth=chain_depth + 1,
            claimed_from=prow["status"],
            engine_default=engine_default or (harness, model),
            pod_slots=pod_slots)
        if not launch:
            return tid, kwargs
        _launch_phase_build(tid, kwargs)
        log.info("auto-advance: project %d → phase %d ('%s') started as task %d "
                 "(chain depth %d)", project_id, prow["phase_number"],
                 prow["title"], tid, chain_depth + 1)
    except Exception as e:
        log.warning("auto-advance start failed for project %d (non-fatal): %s",
                    project_id, e, exc_info=True)


_OUTCOME_LOCKS: dict[int, asyncio.Lock] = {}


def _outcome_lock(project_id: int) -> asyncio.Lock:
    """Serializes the finish bookkeeping of one project's builds (phase
    status, plan-complete flip, what starts next) — see _run_phase_build."""
    lock = _OUTCOME_LOCKS.get(int(project_id))
    if lock is None:
        lock = _OUTCOME_LOCKS[int(project_id)] = asyncio.Lock()
    return lock


async def _explain_idle(pool, project_id: int, task_id: int, phase_id: int) -> None:
    """Append, to the finishing build's chain_stopped, why each OTHER open
    phase is not starting — only when no other build of the project is still
    running (then that build's finish explains). Fail-soft."""
    try:
        in_flight = await pool.fetchval(
            "SELECT COUNT(*) FROM entity_tasks WHERE task_type='factory_build' "
            "AND status='in_progress' AND (result->>'project_id')::int = $1 "
            "AND task_id <> $2", project_id, int(task_id))
        if in_flight:
            return
        rows = await pool.fetch(
            f"SELECT {_PHASE_COLS} FROM project_phases WHERE project_id=$1 "
            "ORDER BY phase_number", project_id)
        free, held = _schedule_phases(rows)
        await _raise_phase_gate_tickets(
            pool, None, project_id,
            [p for p, w, _d in held if w == "approval" and p["id"] != phase_id])
        lines = [_held_text(p, w, d) for p, w, d in held if p["id"] != phase_id]
        free = [p for p in free if p["id"] != phase_id]
        if free:
            lines.insert(0, ", ".join(f"phase {p['phase_number']} ('{p['title']}')" for p in free)
                         + " could build but nothing started "
                         + ("it" if len(free) == 1 else "them")
                         + " — whether to build "
                         + ("it" if len(free) == 1 else "them")
                         + " is the user's call: put it in the question box (escalate)")
        if not lines:
            return
        prev = await pool.fetchval(
            "SELECT result->>'chain_stopped' FROM entity_tasks WHERE task_id=$1", task_id)
        await _chain_stop(pool, task_id, ((prev + "\n") if prev else "")
                          + "Nothing else of the plan is building. The other open phases:\n- "
                          + "\n- ".join(lines))
    except Exception as e:  # noqa: BLE001
        log.warning("idle explanation failed for project %s (non-fatal): %s", project_id, e)


async def _split_conflict_text(pool, phase_id: int, conflict: dict) -> str:
    """What the user and the host are told when a phase's run could not be
    merged: which files, against which phase, and the
    re-split on offer. A conflict fails the SPLIT — two pieces of work the plan
    built apart touch the same lines — so it is never worded as the coder's
    failure."""
    files = [f for f in (conflict.get("files") or []) if isinstance(f, dict)]
    ids = {int((f.get("by") or {}).get("phase_id"))
           for f in files if (f.get("by") or {}).get("phase_id") is not None}
    ids.add(int(phase_id))
    names: dict = {}
    try:
        for r in await pool.fetch(
                "SELECT id, phase_number, title FROM project_phases WHERE id = ANY($1::int[])",
                list(ids)):
            names[r["id"]] = r
    except Exception as e:  # noqa: BLE001 — the files still say what collided
        log.warning("split-conflict phase names unreadable: %s", e)
    me = names.get(int(phase_id))
    me_txt = (f"phase {me['phase_number']} ('{me['title']}')" if me else f"phase id {phase_id}")
    kinds = {"both_changed": "both changed the same lines",
             "merge_failed": "both changed it, and git could not merge it",
             "added_twice": "both created it, differently",
             "deleted_in_project": "it was deleted in the project",
             "not_a_file": "the project has a directory there"}
    others: dict = {}
    lines = []
    for f in files[:12]:
        by = f.get("by") or {}
        other = names.get(int(by["phase_id"])) if by.get("phase_id") is not None else None
        if other is not None and other["id"] != int(phase_id):
            others[other["id"]] = other
            who = f"phase {other['phase_number']} ('{other['title']}')"
        else:
            who = "a change made in the project while it was building"
        lines.append(f"{f.get('path')} — {kinds.get(f.get('kind'), f.get('kind'))} ({who})")
    more = int(conflict.get("count") or len(files)) - len(lines)
    if more > 0:
        lines.append(f"… and {more} more")
    if others:
        o = next(iter(others.values()))
        resplit = (
            f"The way on is a re-split: make {me_txt} wait for phase "
            f"{o['phase_number']} (project_phase op='set_phase_spec', "
            f"phase_id={phase_id}, spec=<the same brief>, depends_on=[{o['id']}]) and "
            f"build it again — it then starts from phase {o['phase_number']}'s landed "
            "work — or merge the two phases into one. Do it when the user's request "
            "covers it; when which of the two is the user's call, put it in the "
            "question box (escalate).")
    else:
        resplit = ("The way on is to build it again by name (build(action='build', "
                   f"phase_id={phase_id})); it starts from the project as it is now. Do it "
                   "when the user's request covers it; otherwise whether to is the user's "
                   "call — put it in the question box (escalate).")
    return (
        f"SPLIT CONFLICT — {me_txt} built, but its changes could not be merged with "
        "work that landed while it was building, so NOTHING from it landed. This is "
        "the plan's split, not a coding failure: the phase was not retried, it is "
        "back to ready, and nothing starts it on its own. Files:\n- "
        + "\n- ".join(lines) + "\n" + resplit)


class _SeedPending(Exception):
    """A mounted project's code is not in its directory yet (project_git
    seed_project_workspace): the build does not run."""


class _KilledBeforeStart(Exception):
    """Killed while the build prepared its workspace, before its run started
    (review.kill_session_runs): the build does not run, and the row says who
    stopped it."""

    def __init__(self, by: str, line: str):
        super().__init__(line)
        self.by, self.line = by, line


async def _run_phase_build(*, entity_id: int, pool, session_id: str | None,
                           task_id: int, project_id: int,
                           phase_id: int | None, intent: str,
                           research_id: str | None, timeout_s: int,
                           harness: str | None = None,
                           model: str | None = None,
                           effort: str | None = None,
                           interaction_context: str | None = None,
                           chain_depth: int = 0,
                           one_shot: bool = False,
                           run_acceptance: list[dict] | None = None,
                           module: str | None = None,
                           claimed_from: str | None = None,
                           engine_default: tuple | None = None,
                           pod_slots: int | None = None) -> None:
    """DETACHED build body (survives the chat turn). Drives the phase/step
    transitions LIVE (when phase_id is set — a continue has no phase), runs the
    build on the harness pool, finalizes the registry row, and wakes the host to
    report + offer the disposition. `harness` is the id _exec_build already
    resolved (and stamped on the row) — passed through so the pool runs THAT
    one, never re-resolved to something else in between.

    `claimed_from` — the status the phase was claimed out of (_claim_phase);
    a build that stops before it runs puts the phase back there.
    `engine_default` — (harness, model) the phases this one unlocks build on
    when they name no engine of their own; None = this build's."""
    if engine_default is None:
        engine_default = (harness, model)

    async def _unclaim() -> None:
        if phase_id is not None and claimed_from:
            await _release_phase_claim(pool, phase_id, claimed_from)

    # The checks the phase lists (v124). Loaded once, up front, and handed to
    # the pool so they run in the run's own worktree on the pod after the
    # harness finishes. Their result is reported; it does not decide anything.
    acceptance_cmds: list[dict] | None = None
    if phase_id is not None:
        try:
            _raw_acc = await pool.fetchval(
                "SELECT acceptance FROM project_phases WHERE id=$1", phase_id)
            if isinstance(_raw_acc, str):
                _raw_acc = json.loads(_raw_acc)
            acceptance_cmds = _normalize_acceptance(_raw_acc)
        except Exception as e:
            log.warning("phase %s: acceptance unreadable (treated as none): %s",
                        phase_id, e)
            acceptance_cmds = None
    elif run_acceptance:
        # build(action='run') carries its checks on the call (no phase row to
        # read them from); the door already normalized and tool-gated them.
        acceptance_cmds = run_acceptance
    acceptance_declared = bool(acceptance_cmds)

    # The MODULE this build is for: named by a run,
    # or the phase's own. Resolved here so the chain and the seeded retry,
    # which reach this function directly, build the same module a hand-started
    # phase would. None = the project root, as every build before modules.
    _module_row = None
    try:
        from server.routes.project_modules import get_module, get_module_by_id
        if module:
            _module_row = await get_module(pool, project_id, module)
        elif phase_id is not None:
            _mid = await pool.fetchval("SELECT module_id FROM project_phases WHERE id=$1", phase_id)
            if _mid is not None:
                _module_row = await get_module_by_id(pool, _mid)
    except Exception as e:  # noqa: BLE001 — a lookup failure must be loud, not a root build
        out = _refused_if_caller(e, f"build not started — its module could not be read: {e}", harness=harness)
        await _factory_build_task_finish(pool, task_id, out)
        await _unclaim()
        return
    if module and _module_row is None:
        out = _refused(f"build not started — project {project_id} has no module {module!r}", harness=harness)
        await _factory_build_task_finish(pool, task_id, out)
        await _unclaim()
        return
    _mod_name = _module_row["name"] if _module_row else None
    _mod_target = (_module_row["target"] if _module_row
                   else await _root_build_target(pool, project_id))

    # The project's PIPELINE (runtime/pipeline.py): which checks
    # are listed for this phase. Their results are reported (the build PR
    # body, the delivery) and never decide the phase's outcome; the diff
    # reviewer's required flag still decides the review hold on auto-merge.
    from mastercode.runtime import pipeline as _pipeline
    try:
        _cfg = await pool.fetchval("SELECT config FROM projects WHERE id=$1", project_id)
        if isinstance(_cfg, str):
            _cfg = json.loads(_cfg)
        # A check that runs outside the platform counts only under the
        # user's signed step-up (pipeline_approval); resolve lists the rest.
        _pipe = _pipeline.resolve(_cfg, module=_mod_name, approved=await _pipeline.load_approved(
            pool, entity_id, project_id, _cfg))
    except Exception as e:  # noqa: BLE001 — unproven, never a default
        log.warning("pipeline setting read failed for project %s: %s", project_id, e)
        _pipe = {**_pipeline.default_pipeline(),
                 "error": f"the project's pipeline setting could not be read ({e})"}

    # LINK GATE, chain side: the same refusal as the chat door's, for the
    # chain, the seeded retry and the repair, which reach this function
    # directly. Before the phase flips to running: it never ran, so it is
    # neither failed nor retried. The reason goes on the bench's activity
    # feed, on the row, and into the report the user reads.
    _links = await _links_out_refusal(pool, project_id, module=_mod_name)
    if _links:
        out = _refused(f"no build ran: {_links}", harness=harness, links_refused=True)
        await _append_build_activity(pool, task_id, out["result"])
        await _factory_build_task_finish(pool, task_id, out)
        await _unclaim()
        await _chain_stop(pool, task_id, out["result"])
        disp = _get_runtime_dispatch()
        if disp is not None:
            try:
                await disp._deliver(task_id)
            except Exception as e:
                log.warning("link-refusal deliver failed (monitor will retry): %s", e)
        return

    if phase_id is not None:
        await _set_phase_status(pool, phase_id, _PHASE_RUNNING)
        await _set_phase_steps_status(pool, phase_id, _STEP_RUNNING)
        # A new run supersedes the collision that held this phase back.
        try:
            await pool.execute(
                "UPDATE project_phases SET integration_error=NULL "
                "WHERE id=$1 AND integration_error IS NOT NULL", phase_id)
        except Exception as e:  # noqa: BLE001
            log.warning("integration_error clear failed (non-fatal): %s", e)

    # Backfill the project's gitea repo up-front — v081's fail-soft creation
    # may have left projects.repo NULL (created before v081, or gitea wasn't
    # connected then); the repo must exist before the first save point lands.
    try:
        from server.routes.projects import ensure_project_repo
        _nm = await pool.fetchval("SELECT name FROM projects WHERE id=$1", project_id)
        if _nm is not None:
            await ensure_project_repo(pool, project_id, _nm)
    except Exception as e:
        log.warning("ensure_project_repo at build start failed (non-fatal): %s", e)

    async def _activity(msg: str) -> None:
        await _append_build_activity(pool, task_id, msg)

    # THE KILL before the run starts (review.kill_session_runs): from here to
    # the run's registration at its wake, the build is preparing its workspace
    # and nothing of it is on a pod, while its row reads as running. A kill in
    # this window is kept, and the build does not start.
    _kill_sid = f"factory-build-{task_id}"
    try:
        from server.routes.review import (build_prep_open as _prep_open,
                                          build_prep_close as _prep_close,
                                          killed_before_start as _killed_before)
    except Exception as e:  # noqa: BLE001 — without the registry there is no kill to honour
        log.warning("kill registry unavailable for build %s: %s", task_id, e)
        _prep_open = _prep_close = (lambda *a, **k: None)
        _killed_before = (lambda *a, **k: None)
    _prep_open(_kill_sid, harness)

    out: dict
    try:
        # Inside the try: a raise here used to kill the detached
        # task before _factory_build_task_finish ever ran — the row sat
        # in_progress until the 4h horizon and the phase stayed in_progress.
        research_context = await _load_research_context(pool, research_id) if research_id else ""
        prompt = _compose_build_prompt(intent, research_context)
        # What the user pushed to host/build is what this build starts from
        # — fast-forward the project
        # dir to origin/host/build when it is clean. Fail-soft; the result rides
        # the activity log so a skipped pull is visible, never silent.
        _seed_block = None
        try:
            from server.routes.project_git import prepare_workspace
            _prep = await prepare_workspace(pool, project_id)
            if _prep.get("seed_pending"):
                # A project mounted on an existing Code Repository whose code
                # could not be read in: a build now would start from nothing
                # and its save point would propose deleting the repository.
                _seed_block = _prep.get("error") or "the project's code has not been read in yet"
            elif _prep.get("updated"):
                await _activity(f"workspace fast-forwarded to origin/host/build "
                                f"({_prep.get('from')} → {_prep.get('commit')})")
            elif _prep.get("error") or _prep.get("note"):
                await _activity(f"workspace not pulled: {_prep.get('error') or _prep.get('note')}")
        except Exception as e:
            log.warning("prepare_workspace before build failed (non-fatal): %s", e)
        if _seed_block:
            await _activity(f"no build ran: {_seed_block}")
            raise _SeedPending(_seed_block)
        if _module_row is None:
            # The root's target again, now the code is in: the first build of
            # a project just mounted on a repository was resolved above over
            # an empty directory, where an app repo's manifest is not yet.
            _mod_target = await _root_build_target(pool, project_id)
        _killer = _killed_before(_kill_sid)
        if _killer:
            from server.routes.review import _killed_sentence
            _stopped_line = _killed_sentence(_killer, before_start=True)
            await _activity(_stopped_line)
            raise _KilledBeforeStart(_killer, _stopped_line)
        out = await _pool_build(
            entity_id=entity_id, pool=pool, project_id=project_id, prompt=prompt,
            research_id=research_id, timeout_s=timeout_s, harness=harness,
            model=model, effort=effort, on_activity=_activity,
            billing_key=f"factory-build-{task_id}",
            # What makes cost-by-phase answerable: this run's harness_usage
            # row is stamped with the phase it was building (v112). None on
            # a `continue` pass, which has no phase.
            phase_id=phase_id,
            acceptance=acceptance_cmds,
            one_shot=one_shot,
            module=_mod_name, target=_mod_target,
            # Builds starting together on this engine: its pod is asked to
            # hold them at once.
            pod_slots=pod_slots)
    except asyncio.CancelledError:
        # Failed — unless a NEWER build of this phase is live: a reaped
        # build finishing its land after the monitor
        # released the phase, while the user started it again, must not mark
        # the new build's phase failed and let a third claim it.
        if phase_id is not None:
            try:
                _newer = await pool.fetchval(
                    "SELECT COUNT(*) FROM entity_tasks WHERE task_type='factory_build' "
                    "AND status='in_progress' AND task_id <> $1 "
                    "AND (result->>'phase_id')::int = $2", task_id, phase_id)
            except Exception:  # noqa: BLE001 — fail it, as before
                _newer = 0
            if not _newer:
                await _set_phase_status(pool, phase_id, _PHASE_FAILED)
                await _set_phase_steps_status(pool, phase_id, _STEP_FAILED)
        raise
    except _SeedPending as e:
        out = _refused(f"no build ran: {e}", harness=harness)
    except _KilledBeforeStart as e:
        out = {"status": "error", "result": e.line, "harness": harness, "killed_by": e.by}
    except Exception as e:
        out = _refused_if_caller(e, f"build crashed: {e}", harness=harness)
    finally:
        _prep_close(_kill_sid)

    ok = isinstance(out, dict) and out.get("status") == "ok"
    # The phase's outcome is the build's: landed (complete) or the harness
    # failed (failed). The listed checks are reported
    # as information — the build PR body and the delivery state them — and
    # never change the outcome. A `continue` has no phase.
    _acc_env = None
    if isinstance(out, dict):
        _r = out.get("result")
        _acc_env = (_r.get("acceptance") if isinstance(_r, dict) else None) \
            or out.get("acceptance")
    # A build that landed a DESKTOP design is loaded in the browser before it
    # counts (desktop_render.landed_tree_verdict) — behind the user's
    # browser-proof switch; None when that is off or nothing desktop-shaped
    # landed. Its verdict joins the phase's checks as `desktop-render`, and
    # rides a one-shot run's result, so the Host reads the error and its line.
    _render_verdict = None
    if ok:
        try:
            from server.applications import desktop_render as _dr
            _render_verdict = await _dr.landed_tree_verdict(
                pool, entity_id, project_id, _mod_name)
        except Exception as e:  # noqa: BLE001 — a proof that cannot run is not a failure
            log.warning("desktop render check on landed build %s skipped: %s", task_id, e)
    if _render_verdict is not None and isinstance(out, dict) \
            and isinstance(out.get("result"), dict):
        out["result"]["desktop_render"] = _render_verdict
    phase_final = None
    _checks = None
    if phase_id is not None:
        phase_final = _PHASE_DONE if ok else _PHASE_FAILED
        _verdicts = [_pipeline.harness_check_verdict(_acc_env, acceptance_declared)]
        if _render_verdict is not None:
            _verdicts.append(_render_verdict)
            if not any(r.get("provider") == _render_verdict["provider"]
                       for r in _pipe.get("check") or []):
                _pipe = {**_pipe, "check": [*(_pipe.get("check") or []),
                                            {"provider": _render_verdict["provider"],
                                             "required": True}]}
        # The forge's CI is not asked per phase (it answers about the PR's
        # head before an automatic merge), so its row is left out of this
        # build's summary rather than reported as "not run".
        _checks = [c for c in _pipeline.decide(ok, _pipe, _verdicts)["checks"]
                   if c.get("provider") != "gitea-actions"]

    # The part of the project this build's contract checked: the root app
    # (""), or this module — None when its target has no contract (a
    # component, a public site, an op), so its clean result clears nothing.
    from server.applications.target_contracts import checks_for as _checks_for
    _contract_scope = ((_mod_name or "") if _checks_for(_mod_target)["manifest"] else None)

    async def _save_point(checks):
        """Commit + push the landed tree as a save point and keep the build
        PR current (sync_project_repo). Fail-soft, like the sync itself."""
        _res = out.get("result") if isinstance(out.get("result"), dict) else {}
        _lbl = f"build: {intent[:80]}"
        if phase_id is not None:
            _ph = await pool.fetchrow(
                "SELECT phase_number, title FROM project_phases WHERE id=$1", phase_id)
            if _ph:
                _lbl = f"phase {_ph['phase_number']}: {_ph['title']}"
        if _mod_name:
            _lbl = f"{_mod_name}: {_lbl}"
        from server.routes.project_git import sync_project_repo
        return _lbl, await sync_project_repo(
            pool, project_id, phase_label=_lbl,
            gate_violations=_res.get("build_contract_violations") or [],
            changed_files=[(f"{_mod_name}/{f}" if _mod_name else f)
                           for f in (_res.get("changed_files") or [])],
            run_ids=[int(_res["run_id"])] if _res.get("run_id") else None,
            # A module build commits its own directory only.
            paths=[_mod_name] if _mod_name else None,
            # Each check's verdict, provider named, for the PR body.
            checks=checks,
            # What this build's contract covered (the root app, or this
            # module), so the Code Approval clears a refusal for that part
            # only (project_git._ensure_pr).
            contract_scope=_contract_scope)

    # The forge's CI is asked before an automatic merge or install
    # (_ci_merge_gate, on the build PR's head); a phase does not wait for it.
    proven = phase_final == _PHASE_DONE
    # The run built but its land was refused: its changes overlap work another
    # phase landed while it ran. Not a phase failure.
    _conflict = (out.get("integration_conflict")
                 if isinstance(out, dict) and isinstance(out.get("integration_conflict"), dict)
                 else None)
    # The phases the chain starts next — every one this phase unlocks — chosen
    # BEFORE _deliver (so a _chain_stop reason rides that delivery out to the
    # user) but started AFTER it (so this phase's work is committed and
    # reported first). [] = nothing starts from here.
    _next_phases: list = []
    # Set when THIS phase was the last open one in the approved plan. That is
    # the moment auto-merge acts on (one rolling PR, each
    # phase a commit inside it, merged at plan end) — decided here with the
    # phase-complete write, applied below after the commit.
    _plan_complete = False
    # The ONE seeded retry of a phase that just failed, decided before _deliver
    # and started after it (same split as _next_phase). None = no retry.
    _retry_phase = None
    _retry_seed = None
    # The registry row is the truth the user was told; the phase follows IT.
    # If the row is no longer in_progress (the monitor already reaped and
    # delivered this build as failed, or a veto landed), a late finisher must
    # not flip the phase to complete/failed and contradict what was said —
    # the guarded write below tells us whether this outcome landed.
    # The outcome is written and the next step decided under ONE lock per
    # project. Parallel phases finish independently;
    # without it two landing together could both count "nothing left" and
    # both run the plan-end merge and delivery, or both decide on the same
    # next phase before either claimed it.
    # A delivery that may wait on the forge's CI after this row is finished
    # (auto-deliver, _ci_merge_gate wait=True) holds the report until it is
    # done, so the merge, the install or the reason neither happened rides
    # the report the user reads. Stamped BEFORE the row turns terminal, so
    # the monitor never sees it unheld; cleared when the outcome is written
    # below; it expires on its own if this process dies mid-wait.
    _report_held = False
    if ok:
        _ci_budget = await _ci_delivery_budget(pool, project_id)
        if _ci_budget:
            _report_held = await _hold_report(pool, task_id, _ci_budget + 300)
    async with _outcome_lock(project_id):
        _landed = await _factory_build_task_finish(pool, task_id, out)
        if phase_id is not None and _landed and _conflict:
            # THE SPLIT FAILED, not the coder: nothing landed, so the phase goes
            # back to ready with the collision on it (the scheduler holds it until
            # it is re-split or built by name), it is not retried, and the reason
            # — which files, against which phase — rides this delivery.
            _why = await _split_conflict_text(pool, phase_id, _conflict)
            try:
                await pool.execute(
                    "UPDATE project_phases SET status=$1, integration_error=$2 "
                    "WHERE id=$3 AND status <> $4",
                    _PHASE_READY, _why[:4000], phase_id, _PHASE_ARCHIVED)
            except Exception as e:  # noqa: BLE001
                log.warning("split-conflict phase write failed (non-fatal): %s", e)
            await _set_phase_steps_status(pool, phase_id, "pending")
            try:
                await pool.execute(
                    "UPDATE entity_tasks SET result = COALESCE(result,'{}'::jsonb) "
                    "|| $1::jsonb WHERE task_id=$2",
                    json.dumps({"phase_status": "split_conflict",
                                "acceptance_declared": acceptance_declared}),
                    task_id)
            except Exception as e:  # noqa: BLE001
                log.warning("split verdict write onto the build row failed (non-fatal): %s", e)
            await _chain_stop(pool, task_id, _why)
        elif phase_id is not None and _landed:
            await _set_phase_status(pool, phase_id, phase_final)
            # The operations ticket joined to this phase (if any) hears the
            # outcome here, next to the phase write, so card and phase agree.
            await _ticket_phase_outcome(pool, entity_id, phase_id, phase_final, out)
            # Steps follow the phase: failed on a failed phase, done otherwise.
            await _set_phase_steps_status(
                pool, phase_id,
                _STEP_FAILED if phase_final == _PHASE_FAILED else _STEP_DONE)
            # The verdict rides the registry row — it is what the delivery record
            # and the bench read (agent_dispatch._deliver: phase_status,
            # acceptance). Written here, next to the phase write, so the two can
            # never disagree.
            try:
                await pool.execute(
                    "UPDATE entity_tasks SET result = COALESCE(result,'{}'::jsonb) "
                    "|| $1::jsonb WHERE task_id=$2",
                    json.dumps({"phase_status": phase_final,
                                "acceptance_declared": acceptance_declared,
                                # One summary per listed check, each naming
                                # its provider (runtime/pipeline.py) — what
                                # the delivery reports as notes.
                                "checks": _checks}),
                    task_id)
            except Exception as e:
                log.warning("phase verdict write onto the build row failed (non-fatal): %s", e)
            # P2: a successful build of the FINAL planned phase completes the
            # PROJECT. Historically load-bearing for "Install as my app" (the ship
            # lane used to require projects.status='complete' and nothing else ever
            # set it, so the disposition 4xx'd on every fresh build); that ship
            # requirement was since REMOVED (applications.py — any state ships
            # except a build in flight), so this is now bookkeeping for the UI's
            # project state, not a ship gate. The user re-opening work later just
            # re-plans (a new plan flips it back to active).
            if proven:
                try:
                    remaining = await pool.fetchval(
                        "SELECT COUNT(*) FROM project_phases "
                        "WHERE project_id=$1 AND " + _PHASE_OPEN_SQL, project_id)
                    if not remaining:
                        await pool.execute(
                            "UPDATE projects SET status='complete', updated_at=now() "
                            "WHERE id=$1", project_id)
                        _plan_complete = True
                    else:
                        # Approved-plan auto-advance is DECIDED here — after the
                        # phase-complete write, so the selection can never re-pick
                        # the phase that just finished — and DISPATCHED at the very
                        # end of this function, after the commit and the report.
                        # The decision has to happen on this side of _deliver
                        # because a _chain_stop reason written here is what rides
                        # that delivery out to the user.
                        _next_phases = await _autoadvance_pick_next(
                            pool=pool, project_id=project_id,
                            chain_depth=chain_depth, finished_task_id=task_id,
                            finished_phase_id=phase_id)
                except Exception as e:
                    log.warning("project-complete flip failed (non-fatal): %s", e)
            elif phase_final == _PHASE_FAILED:
                # FAILED. Decide the one seeded retry here — on this side of
                # _deliver — for the same reason the auto-advance decision is here:
                # when we are NOT retrying, _chain_stop's reason has to ride the
                # delivery that tells the user the phase failed. The retry itself
                # starts at the very end, after that report.
                _retry = await _phase_retry_pick(
                    pool=pool, entity_id=entity_id, project_id=project_id,
                    phase_id=phase_id,
                    failure=str((out or {}).get("result") or "")[:4000],
                    chain_depth=chain_depth, finished_task_id=task_id,
                    killed=bool(isinstance(out, dict) and out.get("killed_by")))
                if _retry is not None:
                    _retry_phase, _retry_seed = _retry
        # A phase that ended WITHOUT completing (failed and not retried, a
        # split conflict) starts nothing. If it was the last build
        # of the project still running, it is the one that has to say what
        # every other open phase is waiting on — a phase that completed
        # earlier stayed quiet because this one was still building.
        if (phase_id is not None and _landed and phase_final != _PHASE_DONE
                and _retry_phase is None):
            await _explain_idle(pool, project_id, task_id, phase_id)

    # Save point: a SUCCESSFUL
    # phase autocommits the workspace to the host's work branch and keeps the
    # single build PR toward the default branch current. Mechanical + fail-soft
    # — the sync result rides in the build outcome; main only moves when the
    # user accepts the PR in the Workbench.
    if ok:
        _label = f"build: {intent[:80]}"
        _sync = None
        try:
            _label, _sync = await _save_point(_checks)
            if isinstance(out.get("result"), dict):
                out["result"]["repo_sync"] = _sync
        except Exception as e:
            log.warning("project repo sync hook failed (non-fatal): %s", e)
        # A landed module build IS the module's next version: the directory
        # as it now stands, the save point if one was
        # made, and the pointer moves to it. The previous version stays.
        if _mod_name:
            try:
                from server.routes.project_modules import record_version
                _res = out.get("result") if isinstance(out.get("result"), dict) else {}
                _ver = await record_version(
                    pool, project_id, _mod_name, source="build",
                    run_id=int(_res["run_id"]) if _res.get("run_id") else None,
                    commit_sha=(_sync or {}).get("commit_sha"),
                    note=_label)
                if isinstance(out.get("result"), dict):
                    out["result"]["module"] = _mod_name
                    out["result"]["version"] = _ver
            except Exception as e:  # noqa: BLE001 — the files landed; the record is said
                log.warning("module version record failed for %s/%s: %s",
                            project_id, _mod_name, e)
                if isinstance(out.get("result"), dict):
                    out["result"]["version_error"] = str(e)[:200]
        # ── Auto-merge at PLAN END (opt-in, per project) ───────────────────
        # Ordering matters: AFTER sync_project_repo, so the final phase's commit
        # is inside the PR being merged, and BEFORE _deliver, so the merge (or
        # the reason it did not happen) rides the same report the user reads.
        # Off unless projects.config.auto_merge is true — the default is still
        # that the user clicks merge themselves.
        # ── AUTO-DELIVER: the same switch, the whole loop ─────
        # The switch used to end at the merge, and delivery ended at
        # "installed" — the user pressed "Install as my app", opened the
        # window, and found out. With the switch ON
        # the kernel now carries a landed tree all the way: merge the rolling
        # PR, ship it, start it, PROVE it runs (mastercode.runtime.app_verify),
        # and open a seeded repair when it does not. Fires on the phase that
        # completes the plan AND on every landed follow-up (a `continue` —
        # phase_id None), because a repair IS a continue and the user's own
        # follow-ups on a shipped app deserve the same proof.
        _auto_deliver = False
        _repair_origin = None
        if _plan_complete or phase_id is None:
            try:
                _auto_deliver = bool(await pool.fetchval(
                    "SELECT COALESCE((config->>'auto_merge')::boolean, false) "
                    "FROM projects WHERE id=$1", project_id))
            except Exception as e:
                log.warning("auto-deliver flag read failed (non-fatal): %s", e)
            try:
                _orig_raw = await pool.fetchval(
                    "SELECT result->'origin' FROM entity_tasks WHERE task_id=$1", task_id)
                _orig = json.loads(_orig_raw) if isinstance(_orig_raw, str) else _orig_raw
                if isinstance(_orig, dict) and isinstance(_orig.get("repair"), dict):
                    _repair_origin = _orig["repair"]
            except Exception as e:
                log.warning("build origin read failed (non-fatal): %s", e)
        # A review hold recorded when a diff reviewer could still block a
        # landing stands until the person merges the PR that carries it
        # (closing one moves the hold to the next PR): the blocked change is
        # already on the rolling PR, and delivering now would ship it without
        # them. No new hold is ever made (the reviewer is gone,
        # pipeline.RETIRED_CHECKS).
        if _auto_deliver:
            _hold = await _active_review_hold(pool, project_id)
            if _hold is not None:
                _auto_deliver = False
                _held = _review_hold_reason(_hold)
                if isinstance(out.get("result"), dict):
                    out["result"]["auto_merge"] = {"merged": False, "held": _held}
                    out["result"]["auto_ship"] = {"shipped": False, "reason": _held}
                log.info("auto-deliver held for project %d: review hold %s (task %s)",
                         project_id, _hold, task_id)
        if _auto_deliver:
            _merged = None
            # Set only once the forge's CI (gitea-actions) passed the build
            # PR's head — or the project has no such check. Anything that
            # stops before that (a raise included) installs nothing.
            _ci_ok = False
            _ci_asked = False
            try:
                from server.routes.project_git import merge_project_pr
                if _plan_complete:
                    # Parallel phases each commit their own save point after
                    # their outcome; one that landed just before this one may
                    # still be waiting for the commit lock. Everything landed
                    # goes into the PR before it merges — a no-op when every
                    # save point is already in.
                    from server.routes.project_git import committable_paths, sync_project_repo
                    from server.routes.review import phase_landed_paths
                    _left = await asyncio.to_thread(
                        committable_paths, project_id, phase_landed_paths(project_id))
                    if _left:
                        # Only files PHASES landed: a follow-up run landing at
                        # this moment is not swept into this commit.
                        await sync_project_repo(
                            pool, project_id, paths=_left,
                            phase_label="plan end: remaining landed work")
                # The forge's CI on the PR's head AS IT NOW STANDS — after this
                # build's save point and the plan end's last commit, so it
                # judges exactly the tree about to be merged and installed.
                # A head the CI has not answered on yet is waited for, inside
                # the row's budget, as a phase's own commit is.
                if _report_held:
                    # From here the gate may wait its whole budget: the hold
                    # restarts now, so the steps before it never eat into it.
                    await _hold_report(pool, task_id, _ci_budget + 300)
                _ci_held, _ci_head = await _ci_merge_gate(
                    pool, project_id, wait=True, on_wait=_activity)
                _ci_asked = True
                if _ci_held:
                    if isinstance(out.get("result"), dict):
                        out["result"]["auto_merge"] = {"merged": False, "held": _ci_held}
                        out["result"]["auto_ship"] = {"shipped": False, "reason": _ci_held}
                    log.info("auto-deliver held for project %d: %s (task %s)",
                             project_id, _ci_held, task_id)
                else:
                    _ci_ok = True
                    if _report_held:
                        # The CI wait is over; the merge and the install are
                        # still ahead. The hold runs from NOW, so it cannot
                        # lapse on a slow merge or install and let the report
                        # go out without them.
                        await _hold_report(pool, task_id, _DELIVERY_HOLD_S)
                    # Pinned to the head the CI passed: a save point landing
                    # after the check is refused by the forge, not merged.
                    _merged = await merge_project_pr(pool, project_id, expect_head=_ci_head)
                    if isinstance(out.get("result"), dict):
                        out["result"]["auto_merge"] = _merged
                    if (_merged or {}).get("merged") and _merged.get("pr") is not None:
                        # Auto-Approve's merge gets the same fixed line in the
                        # project's chat as a person's approval, written from
                        # the forge's merge record (code_approvals.announce_merge).
                        try:
                            from server.routes.code_approvals import announce_merge
                            await announce_merge(pool, entity_id, project_id, int(_merged["pr"]))
                        except Exception as e:  # noqa: BLE001 — the merge stands
                            log.warning("Auto-Approve chat line not written: %s", e)
                    if (_merged or {}).get("head_moved"):
                        # Unchecked work arrived; the tree is not installed either.
                        _ci_ok = False
                        if isinstance(out.get("result"), dict):
                            out["result"]["auto_ship"] = {"shipped": False,
                                                          "reason": _merged.get("reason")}
                    log.info("auto-merge (%s) for project %d: %s",
                             "plan end" if _plan_complete else "follow-up", project_id, _merged)
            except Exception as e:
                log.warning("auto-merge hook failed (non-fatal): %s", e)
                if not _ci_asked:
                    # The step raised before the CI was asked: the tree still
                    # ships as it always did (below) — only if the CI does not
                    # hold it, asked now without waiting.
                    try:
                        _ci_held, _ = await _ci_merge_gate(pool, project_id)
                        _ci_ok = not _ci_held
                    except Exception as e2:  # noqa: BLE001 — unasked installs nothing
                        log.warning("CI gate after a failed merge step raised: %s", e2)
            # Ship + prove. A merge that was held off (contract violations, a
            # conflict) still ships the TREE — shippability is the tree's
            # property (applications._pack_and_sign_project) — and the
            # violations ride the PR body the user reads. The proof is what
            # decides delivery either way.
            # Not installed when the CI held it, when the merge was refused
            # because the head moved after the CI's check (the tree holds work
            # the CI has not passed), or when the step raised before the CI
            # was asked.
            if _ci_ok:
                if _report_held:
                    await _hold_report(pool, task_id, _DELIVERY_HOLD_S)
                try:
                    from mastercode.runtime.app_verify import ship_and_verify
                    _round = int((_repair_origin or {}).get("round") or 0)
                    _ship_mods = await _delivery_targets(
                        pool, project_id, _mod_name, _mod_target, _plan_complete)
                    _ships: list = []
                    for _sm, _st in _ship_mods:
                        if _st in ("public_site", "component"):
                            # Nothing to install: a public site is published
                            # (Sites), a component ships inside another module.
                            _ships.append({"module": _sm, "shipped": False, "reason": (
                                f"module {_sm} is a {_st.replace('_', ' ')} — "
                                "auto-deliver installs apps only")})
                            continue
                        _one = await ship_and_verify(
                            pool=pool, entity_id=entity_id, project_id=project_id,
                            origin={"entity_id": entity_id, "session_id": session_id,
                                    "app_id": "workbench", "project_id": project_id,
                                    "interaction_context": interaction_context,
                                    # The proof's report follows this build's
                                    # (agent_dispatch._wait_for_prior_report).
                                    "after_task_id": task_id},
                            repair_round=_round,
                            reason=("repair landed" if _repair_origin else
                                    "plan end" if _plan_complete else "follow-up landed"),
                            harness=harness, model=model, module=_sm)
                        _ships.append({"module": _sm, **(_one or {})})
                    if not _ships:
                        _ships = [{"shipped": False, "reason": (
                            "no installable module has a version yet — nothing to install")}]
                    _shipped = _ships[0]
                    if isinstance(out.get("result"), dict):
                        out["result"]["auto_ship"] = {
                            k: _shipped.get(k) for k in
                            ("shipped", "reason", "app_id", "verify_task_id", "open_phases",
                             "module")}
                        if len(_ships) > 1:
                            out["result"]["auto_ship_modules"] = [
                                {k: sh.get(k) for k in ("module", "shipped", "reason", "app_id",
                                                        "verify_task_id")}
                                for sh in _ships]
                    log.info("auto-deliver for project %d: %s", project_id,
                             [{k: sh.get(k) for k in ("module", "shipped", "reason", "app_id",
                                                      "verify_task_id")} for sh in _ships])
                except Exception as e:
                    log.warning("auto-ship hook failed (non-fatal): %s", e)

        # The sync result rides the outcome the user sees: merge it onto the
        # row already finalized above (guarded the same way).
        try:
            _res2 = out.get("result") if isinstance(out.get("result"), dict) else {}
            _carry = {}
            if _res2.get("repo_sync") is not None:
                _carry["repo_sync"] = _res2.get("repo_sync")
            if _res2.get("auto_merge") is not None:
                _carry["auto_merge"] = _res2.get("auto_merge")
            if _res2.get("auto_ship") is not None:
                _carry["auto_ship"] = _res2.get("auto_ship")
            if _res2.get("auto_ship_modules") is not None:
                _carry["auto_ship_modules"] = _res2.get("auto_ship_modules")
            for _k in ("module", "version", "version_error"):
                if _res2.get(_k) is not None:
                    _carry[_k] = _res2.get(_k)
            if _carry or _report_held:
                # …and the report is released here (deliver_after dropped).
                await pool.execute(
                    "UPDATE entity_tasks SET result = (COALESCE(result,'{}'::jsonb) "
                    "- 'deliver_after') || $1::jsonb WHERE task_id=$2",
                    json.dumps(_carry), task_id)
                _report_held = False
        except Exception as e:
            log.warning("repo_sync merge onto the build row failed (non-fatal): %s", e)
        if _report_held:
            # The write above failed: release the report on its own, so the
            # delivery below goes out now rather than when the hold lapses.
            try:
                await pool.execute(
                    "UPDATE entity_tasks SET result = result - 'deliver_after' WHERE task_id=$1",
                    task_id)
            except Exception as e:  # noqa: BLE001 — it lapses on its own
                log.warning("report release failed for task %s: %s", task_id, e)

    # Wake the host: report in the originating thread + (on success) offer the
    # disposition. _deliver is at-most-once (the monitor is the safety net).
    disp = _get_runtime_dispatch()
    if disp is not None:
        try:
            await disp._deliver(task_id)
        except Exception as e:
            log.warning("factory-build deliver failed (monitor will retry): %s", e)

    # ── Auto-advance: LAST, after the commit and after the report ───────────
    # This used to fire from inside the phase-complete block above, which put it
    # two steps before sync_project_repo and three before _deliver. Measured:
    # the next build opened 147 ms after the previous
    # run's row was written — while that phase's work was still being committed
    # and before the user had been told anything about it.
    #
    # That is not merely confusing ordering. _run_phase_build starts by calling
    # prepare_workspace(), which fast-forwards the project directory — so the
    # next phase's workspace prep raced the finishing phase's sync_project_repo
    # on the SAME directory, and phase N+1 could begin from a tree phase N had
    # not committed yet.
    #
    # Ordering now: finalize row -> flip phase -> commit + PR -> tell the user
    # -> start the next phase. Fail-soft, exactly as before: a problem starting
    # the chain must never retroactively spoil the phase that just succeeded.
    #
    # Every phase this one unlocked starts here, together — each on its own
    # engine when the plan named one.
    if _next_phases:
        # Claimed first, launched after: phases on one engine share its pod,
        # so each wake asks the plan to hold every phase that REALLY starts on
        # that engine now — a phase another build
        # took, or a halt that stopped one, is not counted.
        _ready = []
        for _nxt in _next_phases:
            _r = await _autoadvance_start_phase(
                _nxt,
                entity_id=entity_id, pool=pool, session_id=session_id,
                project_id=project_id, interaction_context=interaction_context,
                research_id=research_id, timeout_s=timeout_s,
                harness=harness, model=model, effort=effort,
                chain_depth=chain_depth, engine_default=engine_default,
                launch=False)
            if _r:
                _ready.append(_r)
        _per_engine, _keys = await _engine_counts(kw["harness"] for _t, kw in _ready)
        for (_tid, _kw), _key in zip(_ready, _keys):
            _kw["pod_slots"] = _per_engine[_key]
            _launch_phase_build(_tid, _kw)
            log.info("auto-advance: project %d → phase %d started as task %d "
                     "(chain depth %d, %d on %s)", project_id, _kw["phase_id"],
                     _tid, chain_depth + 1, _per_engine[_key], _key)
    # ── The one seeded retry of a FAILED phase ─────────────────────────────
    # Mutually exclusive with the branch above by construction: _next_phase is
    # only ever set on success, _retry_phase only on failure. Last, and after
    # _deliver, for the same reason — the user hears that the phase failed
    # before its retry starts, never the other way round. Same harness and
    # model as the attempt that failed; the failure text is the seed.
    elif _retry_phase is not None:
        await _autoadvance_start_phase(
            _retry_phase,
            entity_id=entity_id, pool=pool, session_id=session_id,
            project_id=project_id, interaction_context=interaction_context,
            research_id=research_id, timeout_s=timeout_s,
            harness=harness, model=model, effort=effort,
            chain_depth=chain_depth, seed=_retry_seed,
            engine_default=engine_default)


async def _exec_build_kill(a: dict, entity_id: int, pool) -> dict:
    """action='kill': stop the project's running builds and
    compares NOW, on their pods (`harness` narrows to one engine). 'stop'
    halts auto-advance at the phase boundary and lets the running phase
    finish; this ends the runs themselves — for a run that keeps working and
    gets nowhere, which the idle watchdog never stops because it is never
    idle. A factory build's project also stops auto-advancing, so the plan
    does not start its next phase the moment the kill lands.

    The pod's /stop ends every run it holds, so other runs sharing that pod
    stop too; the answer names each one (review.kill_session_runs)."""
    try:
        project_id = int(a.get("project_id"))
    except (TypeError, ValueError):
        return _refused("project_id (int) required — the project whose running build to kill")
    rows = await pool.fetch(
        "SELECT task_id, task_type, result->>'session_id' AS sid FROM entity_tasks "
        "WHERE entity_id=$1 AND status='in_progress' "
        "AND task_type IN ('factory_build', 'fan_out') "
        "AND (result->>'project_id')::int = $2 ORDER BY task_id", entity_id, project_id)
    if not rows:
        return {"status": "ok", "result": {
            "project_id": project_id, "stopped": [],
            "next_step": (f"Nothing is building in project {project_id} right now; "
                          "a run that ended a moment ago reports to this thread.")}}
    from server.routes.review import kill_session_runs
    stopped: list = []
    errors: list = []
    halted = None
    for r in rows:
        # A fan-out records its session; a factory build's session is its
        # task's (review.py bkey = 'factory-build-<task_id>').
        session = r["sid"] or f"factory-build-{r['task_id']}"
        out = await kill_session_runs(session, harness=a.get("harness"), by="host")
        stopped += [dict(s, task_id=r["task_id"]) for s in out.get("stopped") or []]
        errors += out.get("errors") or []
        halted = halted or out.get("halted_project_id")
    # The halt lands before any stop (review.kill_session_runs), so it holds
    # whatever the stop did; every answer says so when it happened.
    halt_note = (" Auto-advance is halted for the project; build resumes it."
                 if halted else "")
    if errors:
        errs = "; ".join(f"{e.get('harness')}: {e.get('error')}" for e in errors)
        return {"status": "error", "stopped": stopped, "halted_project_id": halted,
                "result": (f"The kill did not stop everything in project {project_id}: "
                           f"{errs}. Whatever is listed as stopped did stop." + halt_note)}
    if not stopped:
        return {"status": "ok", "result": {
            "project_id": project_id, "stopped": [], "halted_project_id": halted,
            "next_step": ("Its builds are recorded as running but none is on a "
                          "coding engine right now; each reports to this thread when it ends."
                          + halt_note)}}
    others = [s for s in stopped if s.get("other_session")]
    _ran = sorted({s["task_id"] for s in stopped if not s.get("not_started")})
    _unstarted = sorted({s["task_id"] for s in stopped if s.get("not_started")} - set(_ran))
    return {"status": "ok", "result": {
        "project_id": project_id, "stopped": stopped, "halted_project_id": halted,
        "next_step": (
            (f"Killed on the coding engine: task(s) "
             + ", ".join(str(t) for t in _ran)
             + "; their partial work is kept and each reports to this thread."
             if _ran else "")
            + (f"{' ' if _ran else ''}Stopped before it started, so nothing ran: task(s) "
               + ", ".join(str(t) for t in _unstarted) + "."
               if _unstarted else "")
            + (f" {len(others)} run(s) of other work on the same engine pod stopped "
               "too — tell the user which." if others else "")
            + halt_note)}}


async def _exec_build(args: dict, entity_id: int, pool, session_id: str | None,
                      interaction_context: str | None = None,
                      thread_id: str | None = None) -> dict:
    """The ONLY chat door to building. action='plan' composes + persists a
    phased plan (shown in chat); action='build' refuses without phases, then
    runs the phase build detached and returns the task_id immediately.
    action='run' needs no plan: one
    engine is a build, `variants=[…]` is a compare — the same task on each."""
    a = dict(args or {})
    action = (a.get("action") or "").strip().lower()
    # Inside a project conversation the project is the conversation's own —
    # the host rarely passes it (the same derivation review.adopt does).
    if a.get("project_id") in (None, "") and (interaction_context or "").startswith("project:"):
        try:
            a["project_id"] = int((interaction_context or "").split(":", 1)[1])
        except (ValueError, IndexError):
            pass
    if action == "kill":
        # Before the ownership read below: the kill only reaches this
        # entity's own running tasks, so it needs nothing else to start.
        return await _exec_build_kill(a, entity_id, pool)
    # A run with variants is a compare: it has its own worktrees per variant
    # (under its session), so it neither takes nor waits for the project's
    # one-build-at-a-time slot, and each variant resolves its own engine.
    _is_compare = action == "run" and a.get("variants") is not None
    try:
        project_id = int(a.get("project_id"))
    except (TypeError, ValueError):
        return _refused("project_id (int) required")
    owned = await pool.fetchval(
        "SELECT 1 FROM projects WHERE id=$1 AND entity_id=$2", project_id, entity_id)
    if not owned:
        return _refused(f"project {project_id} not found")

    # In-flight guard: re-PLANNING mid-build would supersede-delete the running
    # phase's row, finishing the run phase-orphaned, and a continue is a
    # follow-up on the whole project. Wait for the outcome.
    # `build` is not guarded any more: every run has
    # its own worktree under its session, the land merges instead of copying
    # (review._integrate_with_project), and a phase is claimed before it
    # starts — so saying build while phases run starts what else is ready.
    # A single `run` is not guarded either, for the same reason: it goes
    # through the same lane (run_single_harness_build — its own worktree, the
    # land under project_lock, an overlap refused by name, never overwritten).
    # Guarding it refused parallel builds: a Host fired four
    # runs on one project in one round; the first took the slot and the other
    # three were refused "a build is already running".
    if action in ("plan", "continue"):
        running = await pool.fetchval(
            "SELECT task_id FROM entity_tasks "
            "WHERE entity_id=$1 AND task_type='factory_build' "
            "AND status='in_progress' AND (result->>'project_id')::int = $2 "
            "ORDER BY task_id DESC LIMIT 1",
            entity_id, project_id)
        if running is not None:
            return _refused(f"a build (task {running}) is already running for project "
                f"{project_id} — wait for its outcome (I'll report it in this "
                "thread) before re-planning or starting another build.")

    # Which coding engine fires — resolved HERE, synchronously, before any
    # registry row is opened or pod woken. The
    # host names one (`harness`, from the user's ask or a protocol step) or the
    # resolver takes the DEFAULT engine ticked in Orchestrator → Coding Engines;
    # a default that is not connected, or no default among
    # several connected engines, is a REFUSAL that names the connected set,
    # never a silent swap to a builder the user did not pick. The result
    # of build/continue carries the resolved id, so the host can tell the user
    # what is actually running instead of guessing ("running on the Kimi
    # harness" — while claude-code was failing its 36th wake).
    harness_req = (str(a.get("harness") or "").strip() or None)
    # The user's named model/effort ("use opus 5") ride the build lane.
    # With no way to say a model in `build`, the host reached for a compare
    # door (harness_model=…) to honour the ask — the one path with no
    # delivery lane — and the user got silence after a successful 203 s Opus
    # run. An explicit model beats the ladder rung and
    # the saved override (run_single_harness_build applies those only when
    # model is None); the harness's own default is the last resort.
    model_req = (str(a.get("model") or "").strip() or None)
    effort_req = (str(a.get("effort") or "").strip().lower() or None)
    if effort_req and effort_req not in _known_efforts():
        return _refused("effort must be one of " + ", ".join(_known_efforts()))
    harness: str | None = None
    harness_model: str | None = None   # a ladder rung's model rides with its harness
    if action == "continue" or (action == "run" and not _is_compare):
        try:
            from server.routes.review import resolve_build_target
            harness, harness_model, _why = await resolve_build_target(
                harness_req, model=model_req)
        except Exception as e:
            harness, harness_model, _why = None, None, f"could not resolve a coding harness: {e}"
        if harness is None:
            return {"status": "error", "result": _why or "no coding harness available"}
        if model_req:
            harness_model = model_req
        if effort_req:
            # The engine's OWN accepted set (per model where the catalog says
            # so — codex) — the same validator the Orchestrator and a coding
            # protocol's save use, so an effort a protocol step carries is
            # either honoured here or refused by name, never silently dropped.
            try:
                from server.routes.inference import validate_harness_effort
                _eff_err = validate_harness_effort(harness, effort_req, model=harness_model)
            except Exception:
                _eff_err = None
            if _eff_err:
                return _refused(_eff_err)

    if action == "stop":
        # The brake on approved-plan auto-advance. DELIBERATELY outside the
        # in-flight guard above: the moment you need this is precisely while a
        # build is running, and the guard refuses plan/build/continue then —
        # without this there is no reachable way to halt a chain at all. It
        # halts at the PHASE BOUNDARY and says so: the
        # running phase finishes, because cancelling a factory_build row would
        # not stop the harness pod, and marking it cancelled while it kept
        # running is the exact lie agent_dispatch.cancel() is scoped to avoid
        # (agent_dispatch.py:475-478). Honest brake > fake kill.
        await pool.execute(
            "UPDATE projects SET config = jsonb_set("
            "  COALESCE(config, '{}'::jsonb), '{build_chain_halt}', 'true'::jsonb, true"
            "), updated_at = now() WHERE id=$1", project_id)
        # Every build still running, not the newest one: phases and runs can
        # be in flight together, and each of them finishes.
        _running_rows = await pool.fetch(
            "SELECT task_id FROM entity_tasks "
            "WHERE entity_id=$1 AND task_type='factory_build' "
            "AND status='in_progress' AND (result->>'project_id')::int = $2 "
            "ORDER BY task_id", entity_id, project_id)
        running_ids = [r["task_id"] for r in _running_rows or []]
        running = running_ids[-1] if running_ids else None
        remaining = await pool.fetchval(
            "SELECT COUNT(*) FROM project_phases "
            "WHERE project_id=$1 AND " + _PHASE_OPEN_SQL, project_id)
        return {"status": "ok", "result": {
            "project_id": project_id, "halted": True,
            "running_task_id": running,
            "running_task_ids": running_ids,
            "phases_not_started": int(remaining or 0),
            "next_step": (
                "Auto-advance is halted for this project — no further phase "
                "will start on its own. (If a phase was landing at this exact "
                "moment, one more may already have begun; it will be reported "
                "here and the chain stops after it.)" + (
                    (f" Task {running} is still running and will finish; I'll "
                     "report it here." if len(running_ids) == 1 else
                     " Tasks " + ", ".join(str(t) for t in running_ids)
                     + " are still running and will finish; I'll report each here.")
                    if running_ids else
                    " Nothing is running right now.")
                + " A build of the project resumes it and clears the halt."),
        }}

    if action == "plan":
        intent = (a.get("intent") or "").strip()
        if not intent:
            return _refused("intent required to plan")
        # NOTE: the research is deliberately NOT loaded here any more. It only
        # ever fed the fabricated brief text, and that text is gone — so this
        # loaded a value and discarded it, which is how it already behaved
        # whenever `phases` WAS supplied. Research reaches the plan the way it
        # should: the host reads it (the auto-fire in _execute_meta_tool puts a
        # research_id in args before this runs, or the host passes its own) and
        # composes `phases` from it. `research_id` still rides through to the
        # BUILD, which does consume it (_compose_build_prompt).
        #
        # The host may pass an explicit composed plan; otherwise a DRAFT SHAPE
        # lands — titles only, no briefs, unbuildable by construction — so that
        # a plan row always exists without a placeholder ever being buildable.
        phases = a.get("phases")
        modules = a.get("modules")
        _drafted = not ((isinstance(phases, list) and phases)
                        or (isinstance(modules, list) and modules))
        mods: list[dict] = []
        if _drafted:
            # Placeholder titles: 'planning', unbuildable by construction until
            # a brief makes a phase buildable (project_phase set_phase_spec).
            flat = [{**p, "_module": None, "_validation": None}
                    for p in _compose_default_plan(intent)]
        else:
            # The WHOLE plan is checked before anything is superseded: a refused
            # plan leaves the previous one exactly as it was (it used to delete
            # the unfinished phases first and then refuse, leaving none).
            try:
                mods, flat = _validate_plan(phases, modules)
            except ValueError as e:
                return _refused(f"plan refused — {e}. Nothing was changed: the previous plan "
                    "stands. `acceptance` (optional checks, reported as notes) is a "
                    "list of {cmd, expect_exit?, timeout_s?} and `gate` one of none|user.")
        # A plan with no titled phase writes nothing at all — checked before
        # any write, or the supersede below would clear the previous plan and
        # leave none (a module list whose phases were all untitled did this).
        if not any((p.get("title") or "").strip() for p in flat):
            return _refused("plan refused — it has no phase with a title, so there is nothing "
                "to write. Nothing was changed: the previous plan stands.")
        # Modules, the supersede and the new phases land in ONE transaction: a
        # write that fails part-way leaves the previous plan exactly as it was.
        from server.routes.project_modules import upsert_module
        module_ids: dict = {}
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for m in mods:
                        row = await upsert_module(conn, project_id, **m)
                        module_ids[m["name"]] = row["id"]
                    # Re-planning SUPERSEDES the previous incomplete plan: drop
                    # phases that never completed (steps cascade) so repeated
                    # plans don't stack duplicates; completed phases stay as
                    # history. The project flips back to active.
                    await conn.execute(
                        "DELETE FROM project_phases WHERE project_id=$1 AND " + _PHASE_OPEN_SQL,
                        project_id)
                    await conn.execute(
                        "UPDATE projects SET status='active', updated_at=now() "
                        "WHERE id=$1 AND status='complete'", project_id)
                    plan = await _persist_plan(conn, project_id, flat, module_ids)
        except Exception as e:  # noqa: BLE001 — rolled back, whatever failed
            return _refused_if_caller(e, f"plan refused while writing — {type(e).__name__}: {e}. Nothing was "
                "changed: the previous plan stands.")
        if mods:
            plan["modules"] = mods
        if not plan.get("phases"):
            return _refused("plan composed no phases — check the intent")
        # The note must say WHICH of the two artifacts this is. Saying "Plan
        # written" either way made a fabricated placeholder and a composed
        # plan indistinguishable to the host —
        # and the user's yes was collected against whichever one landed. Once
        # that meant 17 phases carrying the placeholder brief, 12 of
        # them built from it.
        plan["drafted"] = _drafted
        plan["note"] = (
            ("PLACEHOLDER TITLES ONLY — you did not pass `phases`, so this is a "
             "starting shape, not a plan. Each phase has NO brief and CANNOT "
             "build (they are 'planning'). Do NOT present this as the plan and "
             "do NOT ask the user to approve it. Compose the real phases from "
             "the intent + research and either call plan again WITH `phases` "
             "(replaces this draft), or write each brief with "
             "project_phase(op='set_phase_spec'). Phases that no longer apply: "
             "project_phase(op='archive_phase')."
             if _drafted else
             "Plan written from the phases you composed. When the user's "
             "request says what they want, that request is the yes: call "
             "build(action='build') now, in this turn, and show these phases "
             "as they start. A choice only the user can make goes in the "
             "question box (escalate). A build starts every phase whose "
             "depends_on are complete, together (parallel builds); each phase "
             "that lands starts the ones that wait for it.")
        )
        return {"status": "ok", "result": plan}

    if action == "build":
        # A deliberate build IS the resume: clear any standing halt, or a
        # single earlier stop would silently suppress auto-advance forever and
        # look like the chain feature had simply stopped working.
        await pool.execute(
            "UPDATE projects SET config = config - 'build_chain_halt' "
            "WHERE id=$1 AND config ? 'build_chain_halt'", project_id)
        # ── Structural gate: never jump straight to building. ──
        phase_count = await pool.fetchval(
            "SELECT COUNT(*) FROM project_phases WHERE project_id=$1", project_id)
        if not phase_count:
            return _refused("This project has no plan yet — refusing to build. Call "
                "build(action='plan', project_id, intent, phases=[...]) first, "
                "then build — in the same turn when the user's request says "
                "what they want, showing the phases as they start.")

        # WHAT to build. A named phase_id builds that
        # phase. Otherwise every phase that can start now starts, together —
        # each whose dependencies are complete (_schedule_phases); for a plan
        # with no dependencies marked that is the next phase, exactly as
        # before. Phases already building are left alone, so saying build
        # while part of the plan runs starts what else is ready.
        phase_id = a.get("phase_id")
        rows = await pool.fetch(
            f"SELECT {_PHASE_COLS} FROM project_phases WHERE project_id=$1 "
            "ORDER BY phase_number", project_id)
        waits_note = None
        if phase_id is not None:
            try:
                phase_id = int(phase_id)
            except (TypeError, ValueError):
                return _refused("phase_id must be an integer")
            prow = next((r for r in rows if r["id"] == phase_id), None)
            if prow is not None and prow["status"] in (_PHASE_DONE, _PHASE_ARCHIVED):
                prow = None
            if prow is not None and prow["status"] == _PHASE_RUNNING:
                return _refused(await _running_phase_text(pool, prow))
            if prow is not None:
                # Built by name: the user's call, even ahead of what it waits
                # for — but the result says so.
                _by = {r["id"]: r for r in rows}
                _open = [_by[d] for d in _phase_deps(prow, rows)
                         if d in _by and _by[d]["status"] not in
                         (_PHASE_DONE, _PHASE_ARCHIVED, _PHASE_REVIEW)]
                if _open:
                    waits_note = ("Built ahead of what it waits for: " + ", ".join(
                        f"phase {w['phase_number']} ('{w['title']}', {w['status']})"
                        for w in _open) + " — tell the user.")
            targets = [prow] if prow is not None else []
        else:
            targets, held = _schedule_phases(rows)
            if not targets:
                building = [r for r in rows if r["status"] == _PHASE_RUNNING]
                if building:
                    return _refused("nothing else can start: "
                        + "; ".join([await _running_phase_text(pool, r) for r in building]
                                    + [_held_text(p, w, d) for p, w, d in held]))
                # Nothing startable and nothing running: refuse on the FIRST
                # open phase, in the words the door has always used for it.
                targets = [held[0][0]] if held else []
                if held and held[0][1] in ("waiting", "conflict"):
                    return _refused("nothing can start: " + _held_text(*held[0])
                        + ". Build a phase by name (phase_id) to override, or fix "
                        "what holds it.")
        if not targets:
            return _refused("no buildable phase (all phases complete, or phase_id not in this "
                "project). Plan a new phase or pick an existing phase_id.")

        # Each phase's engine: its own when the plan named one, else the one
        # this call names or the default. The default is resolved only when a
        # phase needs it, and the phases the chain starts later inherit it.
        default_engine = (harness_req, model_req)
        _default_resolved = None
        starts: list = []
        for prow in targets:
            # READINESS GATE. This door selected purely on `status <> 'complete'`,
            # so a phase created without a brief (add_phase could not write one) was
            # picked and built anyway — _compose_phase_intent falls back to the
            # TITLE, so the harness got "Phase: Backend aggregation service" and a
            # step list as its entire specification. That is the slop the plan gate
            # exists to prevent, arriving through the gate itself. Refuse, and say
            # exactly what unblocks it. 'failed' is buildable: that is a retry of a
            # phase that already had a brief.
            _pstatus = prow["status"]
            if _pstatus not in (_PHASE_READY, _PHASE_FAILED):
                # Say what is ACTUALLY wrong rather than assuming. The usual cause
                # is a missing brief, but a phase can be non-ready WITH a spec (a
                # writer that forgot the status), and telling the host "it has no
                # brief" then sends it to write one that already exists — a refusal
                # that misdiagnoses is worse than one that just refuses.
                _has_spec = bool((prow["spec"] or "").strip())
                # The phase the model named is in the wrong state: its outcome.
                return _refused((
                    f"phase {prow['phase_number']} ('{prow['title']}') is "
                    f"'{_pstatus}', not ready to build. "
                    + ("It already has a brief, so it only needs promoting: call "
                       f"project_phase(op='set_phase_spec', phase_id={prow['id']}, "
                       "spec='…') with that same brief to mark it ready."
                       if _has_spec else
                       "It has no brief, so a builder would only see its title. "
                       f"Write one with project_phase(op='set_phase_spec', "
                       f"phase_id={prow['id']}, spec='…'), then build.")
                    + " Do NOT tell the user it is building until it is ready."))
            # APPROVAL GATE (v124). A column, checked here and in the chain, so a
            # phase the user must sign off on cannot be built by the host naming
            # its phase_id, and no host tool can clear it (approved_at is written
            # only by the user's own act: the Workbench approve route, or their
            # 'approve' on the phase-gate ticket in Circles).
            if _phase_awaits_approval(prow):
                # The refusal also makes the approval reachable: one open
                # Circles ticket per gated phase (idempotent). If the user sent
                # the phase BACK from that card and nothing has changed on the
                # phase since, no new card is raised and their note is the
                # answer the host must act on.
                _gate = await _raise_phase_gate_tickets(pool, entity_id, project_id, [prow])
                _sent_back = next((g.get("sent_back") for g in _gate if g.get("sent_back")), None)
                if _sent_back is not None:
                    return _refused(f"phase {prow['phase_number']} ('{prow['title']}') was SENT BACK "
                        f"by the user from its approval card: {_sent_back!r}. Change the "
                        "phase to answer that (project_phase op='set_phase_spec' — a "
                        "rewritten brief re-asks for approval) instead of asking again. "
                        "Do NOT start it.")
                # The card the refusal raised: the turn reads it
                # as a question already in front of the person, so its go-ahead
                # guard names this card instead of asking for another.
                _gate_tid = next((g.get("ticket_id") for g in _gate
                                  if isinstance(g, dict) and g.get("ticket_id")), None)
                return _refused(f"phase {prow['phase_number']} ('{prow['title']}') is gated on "
                    "the user's approval and has not been approved. It cannot be "
                    "built until they approve it in the Workbench (the phase shows "
                    "an Approve button) or in Circles (Tickets — a card asks them). "
                    "Tell the user that, and do NOT start it.", card={"kind": "phase_gate", "ticket_id": _gate_tid,
                             "project_id": int(project_id),
                             "phase_number": prow["phase_number"],
                             "title": str(prow["title"] or "")})
            # A phase's automatic repairs: a woken turn may not rebuild a
            # phase that has landed without completing past its budget.
            _rep = await phase_repair_refusal(pool, entity_id, project_id, prow)
            if _rep is not None:
                return _rep

            _h, _m = _phase_engine(prow, harness_req, model_req)
            if _h is None or _h == harness_req:
                if _default_resolved is None:
                    try:
                        from server.routes.review import resolve_build_target
                        _default_resolved = await resolve_build_target(
                            harness_req, model=model_req)
                    except Exception as e:
                        _default_resolved = (None, None, f"could not resolve a coding harness: {e}")
                _dh, _dm, _why = _default_resolved
                if _dh is None:
                    return {"status": "error", "result": _why or "no coding harness available"}
                default_engine = (_dh, model_req or _dm)
                _h, _m = _dh, (_m or _dm)
            else:
                try:
                    from server.routes.review import resolve_build_target
                    _ph, _pm, _why = await resolve_build_target(_h, model=_m)
                except Exception as e:
                    _ph, _pm, _why = None, None, f"could not resolve a coding harness: {e}"
                if _ph is None:
                    return {"status": "error", "result": (
                        f"phase {prow['phase_number']} ('{prow['title']}') names the "
                        f"'{_h}' engine — {_why or 'it is not available'}")}
                _h, _m = _ph, (_m or _pm)
            if effort_req:
                # The engine's OWN accepted set (per model where the catalog says
                # so — codex) — the same validator the Orchestrator and a coding
                # protocol's save use, so an effort a protocol step carries is
                # either honoured here or refused by name, never silently dropped.
                try:
                    from server.routes.inference import validate_harness_effort
                    _eff_err = validate_harness_effort(_h, effort_req, model=_m)
                except Exception:
                    _eff_err = None
                if _eff_err:
                    return _refused(_eff_err)

            # LINK GATE: the tree this phase is seeded from holds links out of
            # it, which no build can carry — said by name, before any claim.
            _links = await _links_out_refusal(pool, project_id,
                                              module_id=_row_get(prow, "module_id"))
            if _links:
                return _refused(f"phase {prow['phase_number']} ('{prow['title']}') not started — "
                    f"{_links} Tell the user which links, and what to do.")
            starts.append((prow, _h, _m))

        research_id = a.get("research_id")
        # `timeout` bounds ONE model call that never answers (nothing
        # arriving: no output, no CPU, no data), never the build — a build
        # has no total time limit (BUILD_MODEL_WAIT_S).
        try:
            timeout_s, _wait_refusal = _build_wait_s(
                int(a["timeout"]) if a.get("timeout") else None)
        except (TypeError, ValueError):
            return _refused("timeout must be a whole number of seconds")
        if _wait_refusal:
            return _refused(_wait_refusal)
        timeout_s = max(60, timeout_s)

        started: list = []
        taken: list = []
        # Claimed first, launched after: phases on one
        # engine share its pod, so each wake asks the plan to hold every phase
        # that REALLY starts on that engine now — a phase a chain took at this
        # moment is not counted.
        _ready: list = []
        try:
            for prow, _h, _m in starts:
                # Compose the phase-scoped build intent from its spec + steps. Shared
                # with the auto-advance chain — a hands-off phase must be briefed with
                # the SAME text a hand-started one gets, and two copies of this would
                # drift the moment either is touched. Composed BEFORE the claim, so
                # a failing read cannot leave the phase claimed with no build.
                scoped_intent = await _compose_phase_intent(pool, prow)
                # Taken before its row opens, so a chain landing at this moment
                # cannot start the same phase too.
                if not await _claim_phase(pool, prow):
                    taken.append(prow)
                    continue
                origin = {
                    "entity_id": entity_id,
                    "session_id": session_id,
                    "app_id": "workbench",
                    "project_id": project_id,
                    "interaction_context": interaction_context,
                    "origin_request": (
                        f"build phase {prow['phase_number']} ('{prow['title']}') "
                        f"of project {project_id}"),
                }
                tid = await _factory_build_task_start(
                    pool, entity_id,
                    title=f"build phase {prow['phase_number']}: {prow['title']}"[:200],
                    intent=scoped_intent,
                    project_id=project_id, origin=origin, phase_id=prow["id"],
                    phase_number=prow["phase_number"], phase_title=prow["title"],
                    timeout_s=timeout_s, harness=_h, model=_m)
                if tid is None:
                    await _release_phase_claim(pool, prow["id"], prow["status"])
                    continue

                _ready.append((tid, dict(
                    entity_id=entity_id, pool=pool, session_id=session_id,
                    task_id=tid, project_id=project_id, phase_id=prow["id"],
                    intent=scoped_intent, research_id=research_id, timeout_s=timeout_s,
                    harness=_h, model=_m, effort=effort_req,
                    interaction_context=interaction_context,
                    claimed_from=prow["status"], engine_default=default_engine)))
                started.append({"task_id": tid, "phase_id": prow["id"],
                                "phase_number": prow["phase_number"],
                                "phase_title": prow["title"], "harness": _h, "model": _m})
        except BaseException:
            # A later phase's compose / claim / row insert raised, or the chat
            # turn was cancelled mid-loop: every phase already claimed still
            # builds — before claim-then-count it was launched on the spot,
            # and nothing else would ever start it. Counted by raw engine name
            # here (no await on the way out), which can only undercount.
            _raw = collections.Counter(kw["harness"] for _t, kw in _ready)
            for _tid, _kw in _ready:
                _kw["pod_slots"] = _raw[_kw["harness"]]
                _launch_phase_build(_tid, _kw)
            raise
        # Detach the runs: the chat turn returns NOW; the builds survive it.
        _per_engine, _keys = await _engine_counts(kw["harness"] for _t, kw in _ready)
        for (_tid, _kw), _key in zip(_ready, _keys):
            _kw["pod_slots"] = _per_engine[_key]
            _launch_phase_build(_tid, _kw)
        if not started:
            if taken:
                return _refused(", ".join(f"phase {p['phase_number']} ('{p['title']}')" for p in taken)
                    + " started building a moment ago (the plan's chain took "
                    + ("it" if len(taken) == 1 else "them")
                    + ") — I'll report here when it lands; nothing else to start.")
            return {"status": "error", "result": "could not open the build registry row"}

        first = started[0]
        engines = sorted({s["harness"] for s in started if s["harness"]})
        note = (
            (f"Build started for this phase on the '{first['harness']}' harness and is "
             "running in the background"
             if len(started) == 1 else
             f"{len(started)} phases started together (parallel builds), on "
             + ", ".join(f"phase {s['phase_number']} → '{s['harness']}'" for s in started)
             + " — each builds in its own worktree and is merged into the project "
             "when it lands")
            + " — the bench shows its progress "
            "(lifecycle events while it runs, the full harness transcript on "
            "completion). Tell the user WHICH harness is building (this one — "
            "never a different name). Do NOT block the chat waiting: the "
            "result comes back to you in this thread when it lands, and you "
            "carry on from it.")
        if waits_note:
            note += " " + waits_note
        return {"status": "ok", "result": {
            "task_id": first["task_id"],
            "phase_id": first["phase_id"],
            "phase_number": first["phase_number"],
            "phase_title": first["phase_title"],
            "project_id": project_id,
            "harness": first["harness"],
            "model": first["model"],
            "effort": effort_req,
            "state": "running",
            # Every phase this call started (one, or several in parallel).
            "started": started,
            "engines": engines,
            "note": note,
        }}

    if action == "continue":
        # A follow-up on an already-built project: a SEEDED RERUN on the pool
        # (the harness pool is stateless between runs — the project's current
        # landed state IS the context). Requires a follow-up message; runs
        # detached + owned exactly like a phase build, no phase transitions.
        # Plan-gated like build: a "continue" on a never-planned project is a
        # first build wearing a different argument. Fast-fail here (the
        # _pool_build gate is the backstop for every other route).
        phase_count = await pool.fetchval(
            "SELECT COUNT(*) FROM project_phases WHERE project_id=$1", project_id)
        if not phase_count:
            return _refused("This project has no plan — refusing to continue-build. Call "
                "build(action='plan', project_id, intent) first, SHOW the phased "
                "plan to the user, and build only after they approve it.")
        message = (a.get("message") or a.get("intent") or "").strip()
        if not message:
            return _refused("continue requires a follow-up `message`.")
        research_id = a.get("research_id")
        # `timeout` bounds ONE model call that never answers (nothing
        # arriving: no output, no CPU, no data), never the build — a build
        # has no total time limit (BUILD_MODEL_WAIT_S).
        try:
            timeout_s, _wait_refusal = _build_wait_s(
                int(a["timeout"]) if a.get("timeout") else None)
        except (TypeError, ValueError):
            return _refused("timeout must be a whole number of seconds")
        if _wait_refusal:
            return _refused(_wait_refusal)
        timeout_s = max(60, timeout_s)
        _links = await _links_out_refusal(
            pool, project_id, module=(str(a.get("module") or "").strip().lower() or None))
        if _links:
            return _refused(f"build not started — {_links} Tell the user which links, and "
                "what to do.")
        origin = {
            "entity_id": entity_id,
            "session_id": session_id,
            "app_id": "workbench",
            "project_id": project_id,
            "interaction_context": interaction_context,
            "origin_request": f"continue build of project {project_id}",
        }
        tid = await _factory_build_task_start(
            pool, entity_id, title=f"continue: {message[:56]}"[:200],
            intent=message, project_id=project_id, origin=origin,
            timeout_s=timeout_s, harness=harness, model=harness_model)
        if tid is None:
            return {"status": "error", "result": "could not open the build registry row"}
        task = asyncio.create_task(_run_phase_build(
            entity_id=entity_id, pool=pool, session_id=session_id,
            task_id=tid, project_id=project_id, phase_id=None,
            intent=message, research_id=research_id, timeout_s=timeout_s,
            harness=harness, model=harness_model, effort=effort_req,
            # A follow-up on one MODULE; the build
            # refuses an unknown one by name before any pod wakes.
            module=(str(a.get("module") or "").strip().lower() or None)))
        _FACTORY_BUILD_TASKS[tid] = task
        task.add_done_callback(lambda _t, k=tid: _FACTORY_BUILD_TASKS.pop(k, None))
        return {"status": "ok", "result": {
            "task_id": tid, "project_id": project_id, "harness": harness,
            "model": harness_model, "effort": effort_req,
            "state": "running",
            "note": (f"Follow-up build (seeded rerun) started on the '{harness}' "
                     "harness in the background — tell the user which harness is "
                     "building; I'll report the outcome in this thread when it "
                     "finishes."),
        }}

    if action == "run":
        return await _exec_build_run(
            a, entity_id=entity_id, pool=pool, session_id=session_id,
            interaction_context=interaction_context, project_id=project_id,
            harness=harness, model=harness_model, effort=effort_req,
            is_compare=_is_compare, thread_id=thread_id)

    return _refused("action must be 'run', 'plan', 'build', 'continue' or 'stop'")


async def _root_build_target(pool, project_id: int) -> str | None:
    """The target of a build at the project's root (no module):
    target_contracts.root_target over the project's config and directory, so
    a project mounted on or forked from a repository builds as a repository
    change, and one whose repository is an app (an app manifest at its root)
    keeps the app contract. None (the app default, as before modules) when
    the config cannot be read."""
    from server.applications.target_contracts import root_target
    try:
        return root_target(await pool.fetchval(
            "SELECT config FROM projects WHERE id=$1", project_id),
            f"{_PROJECTS_ROOT}/{int(project_id)}")
    except Exception as e:  # noqa: BLE001 — the default contract, never a failed build
        log.warning("project %s: config unreadable for its build target: %s", project_id, e)
        return None


async def _exec_build_run(a: dict, *, entity_id: int, pool, session_id: str | None,
                          interaction_context: str | None, project_id: int,
                          harness: str | None, model: str | None,
                          effort: str | None, is_compare: bool,
                          thread_id: str | None = None) -> dict:
    """build(action='run') — one call, no plan. Without `variants` it is a
    BUILD: one engine (already resolved by the door), detached on the pool
    exactly like a `continue`, reported into the thread when it lands. With
    `variants=[{harness, model?, effort?}]` it is a COMPARE: the same task on
    each variant, each in its own worktree, one pod per harness.

    Never touches project_phases: a run is not a phase, so it neither needs a
    plan nor advances one. The plan gate still holds for every phase lane —
    the pool is told this one is a one-shot (_pool_build `one_shot`)."""
    prompt = (a.get("prompt") or a.get("intent") or a.get("message") or "").strip()
    if not prompt:
        return _refused("run requires a `prompt` — the task to build.")
    try:
        _req_s = int(a["max_minutes"]) * 60 if a.get("max_minutes") else None
    except (TypeError, ValueError):
        return _refused("max_minutes must be an integer")
    # Same bound as a phase build's `timeout` (the person's limit
    # build.model_wait_max_minutes): it is the longest one model call may go
    # with nothing arriving, not a limit on how long the run may take.
    _wait_s, _wait_refusal = _build_wait_s(_req_s)
    if _wait_refusal:
        return _refused(_wait_refusal)
    max_minutes = max(1, _wait_s // 60)
    try:
        acceptance = _normalize_acceptance(a.get("acceptance"))
    except ValueError as e:
        return _refused(f"run refused — {e}")
    # A run writes NOTHING to the plan: no phase or step changes state
    # however much code lands. Said on the result when the project HAS a plan
    # — the one case where the host could mistake a run for executing it —
    # and not otherwise (no noise on an unplanned project).
    try:
        _has_plan = bool(await pool.fetchval(
            "SELECT COUNT(*) FROM project_phases WHERE project_id=$1", project_id))
    except Exception:
        _has_plan = False
    plan_note = (" This does NOT advance the plan — no phase or step changes "
                 "state; a planned phase is built with build(action='build')."
                 if _has_plan else "")
    # Into a MODULE: its directory, its target's
    # contract; a landed build becomes the module's next version.
    module = str(a.get("module") or "").strip().lower() or None
    mod_row = None
    if module:
        from server.routes.project_modules import get_module
        mod_row = await get_module(pool, project_id, module)
        if mod_row is None:
            return _refused(f"project {project_id} has no module {module!r} — modules are "
                "declared by build(action='plan', modules=[{name, target, "
                "validation, phases}])")

    if is_compare:
        # Top-level engine args mean "one engine" — in a compare each run names
        # its own in `variants`. Refused by name rather than dropped, so the
        # host never believes a model it set ran when it did not.
        _stray = [k for k in ("harness", "model", "effort")
                  if str(a.get(k) or "").strip()]
        if _stray:
            return _refused(f"{', '.join(_stray)} cannot be combined with variants — in a "
                "compare each run names its own {harness, model?, effort?} "
                "inside variants")
        variants = a.get("variants")
        if not isinstance(variants, list) or not variants:
            return _refused("variants must be a non-empty list of {harness, model?, effort?} — "
                "leave it out for a single build")
        for i, v in enumerate(variants):
            if not isinstance(v, dict) or not str(v.get("harness") or "").strip():
                return _refused(f"variants[{i}] must name a harness: {{harness, model?, effort?}}")
            eff = str(v.get("effort") or "").strip().lower()
            if eff:
                if eff not in _known_efforts():
                    return _refused(f"variants[{i}].effort must be one of " + ", ".join(_known_efforts()))
                try:
                    from server.routes.inference import validate_harness_effort
                    _eff_err = validate_harness_effort(
                        str(v["harness"]).strip(), eff,
                        model=(str(v.get("model") or "").strip() or None))
                except Exception:
                    _eff_err = None
                if _eff_err:
                    return _refused(f"variants[{i}]: {_eff_err}")
        if acceptance:
            # The same tool gate a phase build passes, per engine in the
            # compare: a check its image cannot run is refused before any pod
            # wakes, never discovered as a false failure after.
            for h in dict.fromkeys(str(v["harness"]).strip() for v in variants):
                _ref = _acceptance_tool_refusal(acceptance, h)
                if _ref:
                    return _refused(f"compare not started — its checks cannot run on the {h} "
                        f"engine. {_ref}")
        return await _exec_build_compare(
            {"prompt": prompt, "variants": variants, "max_minutes": max_minutes,
             "project_id": project_id, "context": a.get("context"),
             "seed": a.get("seed"), "vcpus": a.get("vcpus"),
             "acceptance": acceptance, "plan_note": plan_note,
             "module": module, "target": mod_row["target"] if mod_row else None},
            entity_id, pool, session_id, interaction_context,
            thread_id=thread_id)

    if acceptance:
        _ref = _acceptance_tool_refusal(acceptance, harness)
        if _ref:
            return _refused(f"build not started — its checks cannot run on the {harness} "
                f"engine. {_ref}")
    # A single build seeds from the project's landed tree (the pool worktree
    # is prepared from it); `seed=False` asks for a clean start — refused by
    # name rather than ignored, since the single-build lane has no greenfield
    # mode and a silently seeded run would not be what was asked.
    if a.get("seed") is False:
        return _refused("seed=false is a compare option (a fresh tree per variant); a "
            "single build always starts from the project's current files")
    intent = prompt
    if (a.get("context") or "").strip():
        intent = f"{prompt}\n\nContext from the host:\n{str(a['context']).strip()}"
    timeout_s = max_minutes * 60
    _links = await _links_out_refusal(pool, project_id, module=module)
    if _links:
        return _refused(f"build not started — {_links} Tell the user which links, and what "
            "to do.")
    origin = {
        "entity_id": entity_id,
        "session_id": session_id,
        "app_id": "workbench",
        "project_id": project_id,
        "interaction_context": interaction_context,
        "origin_request": f"build on project {project_id}: {prompt[:200]}",
    }
    tid = await _factory_build_task_start(
        pool, entity_id, title=f"build: {prompt[:56]}"[:200],
        intent=intent, project_id=project_id, origin=origin,
        timeout_s=timeout_s, harness=harness, model=model)
    if tid is None:
        return {"status": "error", "result": "could not open the build registry row"}
    # No pod_slots here: runs the host starts in one round share their
    # engine's pod, and the wake sizes it from every build in flight on that
    # engine at the moment it wakes (review._builds_in_flight). Over the pod's
    # size a run waits for a slot; it is never refused.
    task = asyncio.create_task(_run_phase_build(
        entity_id=entity_id, pool=pool, session_id=session_id,
        task_id=tid, project_id=project_id, phase_id=None,
        intent=intent, research_id=a.get("research_id"), timeout_s=timeout_s,
        harness=harness, model=model, effort=effort,
        interaction_context=interaction_context,
        one_shot=True, run_acceptance=acceptance, module=module))
    _FACTORY_BUILD_TASKS[tid] = task
    task.add_done_callback(lambda _t, k=tid: _FACTORY_BUILD_TASKS.pop(k, None))
    return {"status": "ok", "result": {
        "task_id": tid, "project_id": project_id, "harness": harness,
        "model": model, "effort": effort, "state": "running",
        "checks": len(acceptance or []),
        "advances_plan": False,
        "note": (f"Build started on the '{harness}' engine"
                 + (f" ({model})" if model else "")
                 + " and is running in the background — tell the user which "
                 "engine is building (this one, never a different name). Do NOT "
                 "block the chat; I'll report the outcome in this thread when it "
                 "lands." + plan_note),
    }}


def _known_efforts() -> tuple:
    """Every reasoning-effort value SOME engine accepts (the union of
    inference.HARNESS_EFFORTS — low/medium/high/xhigh/max/ultra today); the
    per-engine narrowing happens after the engine is resolved. Falls back to
    the four classic values if the server module is unavailable."""
    try:
        from server.routes.inference import HARNESS_EFFORTS
        seen: list = []
        for _engine in HARNESS_EFFORTS:
            for v in HARNESS_EFFORTS[_engine]:
                if v not in seen:
                    seen.append(v)
        if seen:
            return tuple(seen)
    except Exception:
        pass
    return ("low", "medium", "high", "max")


# ── list_projects implementation ─────────────────────


async def _ci_delivery_budget(pool, project_id: int) -> int:
    """How long an auto-deliver of this project may wait on the forge's CI
    (the longest gitea-actions row's wait_budget_s), or 0 when it never
    waits: auto_merge off, or no such row. Unreadable → 0: the gate itself
    then holds without waiting."""
    from mastercode.runtime import pipeline as _pl
    try:
        cfg = await pool.fetchval("SELECT config FROM projects WHERE id=$1", project_id)
        cfg = json.loads(cfg) if isinstance(cfg, str) else cfg
    except Exception:  # noqa: BLE001
        return 0
    if not isinstance(cfg, dict) or cfg.get("auto_merge") is not True \
            or "gitea-actions" not in json.dumps(cfg.get("pipeline") or {}):
        return 0
    norm, _ = _pl.validate(cfg.get("pipeline"))
    rows = [r for r in _pl._check_rows_everywhere(norm or {}) if r["provider"] == "gitea-actions"]
    return max((int(r.get("wait_budget_s") or 900) for r in rows), default=0)


# How long a report stays held while the merge and the install that follow a
# CI wait run. Renewed at each of those steps, so it only has to outlast one.
_DELIVERY_HOLD_S = 1800


async def _hold_report(pool, task_id, hold_s: int) -> bool:
    """Stamp deliver_after on the build's row: no delivery claims it before
    then (agent_dispatch._deliver, the monitor's scan). True when written."""
    try:
        await pool.execute(
            "UPDATE entity_tasks SET result = jsonb_set(COALESCE(result,'{}'::jsonb), "
            "'{deliver_after}', to_jsonb((now() + make_interval(secs => $2))::text), true) "
            "WHERE task_id=$1", task_id, float(hold_s))
        return True
    except Exception as e:  # noqa: BLE001 — the report may then go out before the merge
        log.warning("report hold for task %s failed: %s", task_id, e)
        return False

async def _delivery_targets(pool, project_id: int, mod_name, mod_target,
                            plan_complete: bool) -> list:
    """WHAT auto-deliver ships, as [(module or None for the root app, target)].

    - A module build (or its repair) ships its module.
    - A project with no modules ships its root, as it always did.
    - In a project of modules the root may still be an app of its own
      (build-workflow: "a phase of the project's root app (no module)") —
      it is one when an app.yaml sits at the project root. A root follow-up
      ships that root app; with no root app it ships every installable module
      that has a version (a root edit can change what they share).
    - The plan end ships the whole project: every installable module with a
      current version, AND the root app when there is one.
    """
    if mod_name and not plan_complete:
        return [(mod_name, mod_target)]
    try:
        from server.routes.project_modules import INSTALLABLE_TARGETS, list_modules
        _all = await list_modules(pool, project_id)
    except Exception as e:  # noqa: BLE001 — ship what this build made
        log.warning("module list for auto-deliver failed: %s", e)
        return [(mod_name, mod_target)]
    if not _all:
        return [(None, None)]
    mods = [(m["name"], m["target"]) for m in _all
            if m["target"] in INSTALLABLE_TARGETS and m.get("current_version")]
    root_app = (Path(str(_PROJECTS_ROOT)) / str(int(project_id)) / "app.yaml").is_file()
    if plan_complete:
        return ([(None, None)] if root_app else []) + mods
    return [(None, None)] if root_app else mods
