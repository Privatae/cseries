"""
The Ladder — routing-economics engine for the Workbench (the protocol half
of protocol=code, policy=data).

route → attempt cheap → verify (judge-free) → escalate WITH EVIDENCE →
record outcome + cost. Each rung is one fan_out dispatch (review.py owns
all pod mechanics); verification is the project's own test command
(scripts/workbench_bench.py — deterministic, no judge); escalation forwards
FAILURE EVIDENCE (failing test output, watchdog verdicts, the human's
rejection note), never the failed attempt — attempts anchor, evidence
compresses. The host auto-climbs ONLY on verified failure, within the rungs
the user configured; a passing rung always awaits human review (the human
stays the acceptance gate — decision-substrate).

Policy = data: the rungs arrive ON THE RUN (`rungs`) — normally the chained
steps of a coding protocol the host follows (server/routes/coding_protocols
.py, one rung per step); omitted, the DEFAULT ladder applies: the Default
engine ticked in Orchestrator → Coding Engines first, then the bench. The
older stored policy (behavioral_params.coding_ladder, the old
Orchestrator rung editor) is no longer read here — the one-time migration
(coding_protocols.ensure_migrated) turned it into a protocol file.

This module never touches server/applications/escalation.py (the
governance-ticket system) — same word, different machine.
"""

import asyncio
import importlib.util
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import brain_service
from server.applications.middleware import requires_permission
from server.routes import review
from server.routes import _run_watchdog as watchdog

log = logging.getLogger("privatae.pc.ladder")

router = APIRouter(prefix="/api/ladder", tags=["ladder"])

# Created where a run's state is first written, never at import.
LADDER_ROOT = Path("/var/lib/cseries/ladder-runs")

# Evidence is carried WHOLE (an old fixed 4,000-char cut dropped the failure
# detail the next rung needed). The one real bound is the prompt's transport:
# a rung's prompt is ONE argv string to every CLI harness (review.py,
# _REF_DIR note), and Linux refuses a single argument past MAX_ARG_STRLEN —
# 128 KiB — so the run would never start. _with_evidence fits the evidence
# into what is left of that after the prompt, minus _PROMPT_ARG_HEADROOM for
# what the fan-out prepends (the reference-files line), and says so in place
# when it has to cut.
_PROMPT_ARG_MAX = 128 * 1024
_PROMPT_ARG_HEADROOM = 8 * 1024
_VERIFY_TIMEOUT_S = 600
# The ladder's per-rung WAIT LIMIT, in minutes. It is not a limit on how long
# a run may work: a coding run has no total time limit, and a run that is
# printing, computing or moving data is never stopped. What this number does:
#   1. It bounds a rung's optional `max_minutes` override to 1..60 (below).
#   2. Nothing else. The climb waits for a rung's session to END, however long
#      it works: it used to stop waiting at `max_minutes` * 60 + 180 s and
#      classify a run still working as the rung's failure, then climb past it.
#      A session that nothing is running any more and that
#      has gone silent reports itself over (review._session_is_over), so a
#      dead rung still ends the wait.
# `max_minutes` itself, as the harness runtime now reads it, is only how long
# ONE model call may go with nothing arriving (no output, no CPU, no data)
# before the run is stopped as hung (harness_core.turn_budget_s).
# Why 60 (derived from one Host's harness_run, 523 successful
# runs): p50 16s, p90 26s, p99 85s, longest success 1134s
# (~19 min); the old 10-minute default had killed real builds ("exceeded
# 20min cap" x4, "exceeded 8min cap" x5). 60 covers every success ever
# recorded with 3x headroom. The rung editor does not ask the user for a
# number (the user cannot know how long something takes); `max_minutes` is an
# override for API/host callers.
RUNG_WALL_CAP_MIN = 60


def rung_wait_cap() -> int | None:
    """The most a rung may ask for: the person's limit
    build.model_wait_max_minutes (runtime/user_limits, 2026-10-20), the same
    one a build's own wait is held to; None for no limit. RUNG_WALL_CAP_MIN
    stays the default a rung gets when it names none."""
    from mastercode.runtime import user_limits
    v = user_limits.value("build.model_wait_max_minutes")
    return None if v is None else int(v)


_ACTIVE: set[str] = set()   # ladder ids with a live climb task (no double-climb)
# Strong refs to in-flight climb tasks. asyncio.create_task only holds a WEAK
# ref, so a detached climb could be garbage-collected mid-flight and freeze
# its run at "running" forever — the exact dead-task bug review.py's _BG_TASKS
# already closes for fan-outs. Keep the task here until done.
_CLIMB_TASKS: set = set()


def _spawn_climb(lid: str) -> bool:
    """Register + spawn the climb for `lid`. Registration into _ACTIVE happens
    SYNCHRONOUSLY (before the task first runs) so a status read between spawn
    and task start can never mistake a fresh run for an orphan. Returns False
    when a climb is already live for this id (no double-climb)."""
    if lid in _ACTIVE:
        return False
    _ACTIVE.add(lid)
    task = asyncio.get_running_loop().create_task(_climb(lid))
    _CLIMB_TASKS.add(task)
    task.add_done_callback(_CLIMB_TASKS.discard)
    return True


def _reap_if_orphaned(state: dict) -> dict:
    """Restart safety: a run whose disk state says "running" but that has no
    live climb task IN THIS PROCESS is an orphan (kernel restarted mid-climb —
    nothing rehydrates climbs, and pretending it is still running left an
    eternal spinner with no resume or fail affordance). Mark it terminal at
    read time, preserving attempts/evidence so a fresh run can reuse them."""
    if state.get("status") == "running" and state.get("ladder_id") not in _ACTIVE:
        state["status"] = "error"
        state["error"] = ("kernel restarted mid-climb — the climb task no "
                          "longer exists. Attempts and evidence are preserved; "
                          "start a new run to continue from them.")
        try:
            _write_state(state)
        except Exception:  # noqa: BLE001 — read paths must not fail on the reap write
            log.warning("ladder %s: orphan reap write failed", state.get("ladder_id"))
    return state


# ── Policy (data) ────────────────────────────────────────────────────

def _valid_rung(r: dict, model_opts: Optional[dict] = None) -> Optional[str]:
    """Shape check; returns an error string or None. '*' = the full bench
    (existing fan-out-everything + human pick) as a rung — the last resort.
    `model_opts` = the coding-matrix model lists (inference.harness_model_
    options), resolved ONCE by the caller for the whole rung list."""
    if not isinstance(r, dict) or not isinstance(r.get("harness"), str) or not r["harness"]:
        return "each rung needs a harness id (or '*' for the full bench)"
    # max_minutes is OPTIONAL: absent = RUNG_WALL_CAP_MIN. Present, it is an
    # override bounded by the person's limit (rung_wait_cap).
    mm = r.get("max_minutes")
    _cap = rung_wait_cap()
    if mm is not None and (not isinstance(mm, int) or isinstance(mm, bool) or mm < 1
                           or (_cap is not None and mm > _cap)):
        return (f"max_minutes, when given, must be a whole number of minutes from 1"
                + (f" to {_cap}, the person's limit build.model_wait_max_minutes"
                   if _cap is not None else ""))
    n = r.get("samples", 1)
    if not isinstance(n, int) or not 1 <= n <= 16:
        return "samples must be an int in 1..16 (Self-MoA bound)"
    if r.get("model") is not None and not isinstance(r["model"], str):
        return "model must be a string (lane-native slug)"
    if r.get("model") and r["harness"] != "*":
        # No typed model names: a rung's model must be one its
        # harness's provider actually offers — same validator as the
        # coding-harness overrides, checked against the SAME list the picker
        # rendered (the coding matrix), fails open on a cold catalog.
        from server.routes.inference import validate_harness_model
        err = validate_harness_model(r["harness"], r["model"].strip(), model_opts)
        if err:
            return err
    if r.get("effort") is not None and not isinstance(r["effort"], str):
        return "effort must be a string"
    return None


async def _validate_rungs(rungs: list) -> None:
    """400 on the first bad rung. Resolves the coding-matrix model lists once,
    and only when some rung actually pins a model."""
    model_opts: Optional[dict] = None
    if any(isinstance(r, dict) and r.get("model") and r.get("harness") != "*"
           for r in rungs):
        from server.routes.inference import harness_model_options
        model_opts = await harness_model_options()
    for i, r in enumerate(rungs):
        err = _valid_rung(r, model_opts)
        if err:
            raise HTTPException(400, f"rung {i}: {err}")


async def _default_ladder() -> list[dict]:
    """Cold start: the USER'S own bench — the default engine first (the tick in
    Orchestrator → Coding Engines), then the other connected engines, then the
    full bench. No invented vendors: a rung only ever
    names something actually connected — subscription harnesses are flat-rate,
    so for a subscription user the subscription IS the cheap rung; a per-token
    lane is a rung only if the user connected one. No per-rung minutes: the
    platform cap applies. (The Orchestrator no longer edits this policy —
    a ladder is a coding protocol the host reads; `rungs` on
    ladder.run is how the host passes one.)"""
    installed = await review._installed_harnesses()
    default = await review._default_coding_harness()
    order = ([default] if default in installed else []) + [
        h for h in installed if h != default]
    rungs: list[dict] = [{"harness": h} for h in order[:2]]
    rungs.append({"harness": "*"})
    return rungs


@router.get("/policy", dependencies=[Depends(requires_permission("read:inference_catalog"))])
async def get_policy() -> dict:
    """The rungs a run takes when it names none: the DEFAULT ladder (the Default
    engine first, then the bench). There is no stored policy any more —
    the Orchestrator's rung editor is gone and its old
    `coding_ladder` key was migrated into a protocol file; a host that wants a
    specific ladder passes `rungs` on ladder.run."""
    return {"rungs": await _default_ladder(), "source": "default"}


async def _rung_targets(h: str) -> list[str]:
    """The engines a rung dispatches to: the named engine, or — for the `*`
    (full-bench) rung — every CONNECTED engine at climb time. fan_out refuses
    a call that names nothing (there is no stored selection to
    fall back on), so the bench rung must name its engines itself; an empty
    bench is reported as not-dispatched by the caller, never sent."""
    if h == "*":
        return list(await review._installed_harnesses())
    return [h]


# ── Run state (filesystem, the review-session pattern) ───────────────

def _state_path(lid: str) -> Path:
    return LADDER_ROOT / lid / "state.json"


def _read_state(lid: str) -> dict:
    p = _state_path(lid)
    if not p.exists():
        raise HTTPException(404, "ladder run not found")
    return json.loads(p.read_text())


def _write_state(state: dict) -> None:
    state["updated_at"] = time.time()
    d = LADDER_ROOT / state["ladder_id"]
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, default=str))
    os.replace(tmp, d / "state.json")


# ── Verify (judge-free) ──────────────────────────────────────────────

_WB = None


def _bench_mod():
    """scripts/workbench_bench.py is a script, not a package — load it once
    by path (repo root/scripts, /app/scripts in-container)."""
    global _WB
    if _WB is None:
        p = Path(__file__).resolve().parents[2] / "scripts" / "workbench_bench.py"
        spec = importlib.util.spec_from_file_location("workbench_bench", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _WB = mod
    return _WB


def _verify(session_id: str, cmd: str) -> dict:
    return _bench_mod().verify_session(
        review.SESSIONS_ROOT / session_id, cmd, _VERIFY_TIMEOUT_S)


# ── Evidence (what climbs — never the failed diff) ───────────────────

def _evidence_from_bench(bench: dict, stats: dict) -> str:
    lines = [f"Deterministic check `{bench.get('verify_cmd')}` FAILED on every variant:"]
    for r in bench.get("results", []):
        if r.get("passed"):
            continue
        who = f"{r.get('harness')}" + (f" ({r.get('model_requested')})" if r.get("model_requested") else "")
        lines.append(f"- {who}: exit={r.get('exit_code')} {r.get('error') or ''}")
        tail = (r.get("output_tail") or "").strip()
        if tail:
            lines.append("  failing output:\n  " + tail.replace("\n", "\n  "))
        v = (stats.get(r.get("vid") or "") or {})
        if v.get("verdict"):
            lines.append(f"  behavior: {v['verdict']} (repeated `{v.get('top_repeat')}` ×{v.get('max_consec_identical')})")
    return "\n".join(lines)


def _evidence_from_failure(per: dict, reasons: dict, stats: dict) -> str:
    lines = ["The previous attempt FAILED to complete:"]
    for vid, row in per.items():
        v = stats.get(vid) or {}
        extra = f"; repeated `{v.get('top_repeat')}` ×{v.get('max_consec_identical')}" if v.get("verdict") else ""
        lines.append(f"- {vid}: {reasons.get(vid)} — {row.get('error') or ''}{extra}")
    return "\n".join(lines)


_EVIDENCE_HEAD = ("\n\n=== PRIOR-ATTEMPT EVIDENCE (a cheaper attempt failed verification; "
                  "this is the discriminating clue — do NOT repeat the failed approach) ===\n")


def _with_evidence(prompt: str, evidence: str) -> str:
    if not evidence:
        return prompt
    room = (_PROMPT_ARG_MAX - _PROMPT_ARG_HEADROOM
            - len(prompt.encode("utf-8")) - len(_EVIDENCE_HEAD.encode("utf-8")))
    raw = evidence.encode("utf-8")
    if len(raw) > room:
        def _mark(n: int) -> str:
            return (f"\n…[{n:,} more bytes of evidence not carried — a rung's prompt "
                    f"travels as one argument of at most {_PROMPT_ARG_MAX // 1024} KiB]")
        keep = max(0, room - len(_mark(len(raw)).encode("utf-8")))
        cut = raw[:keep].decode("utf-8", "ignore")
        evidence = cut + _mark(len(raw) - len(cut.encode("utf-8")))
    return prompt + _EVIDENCE_HEAD + evidence


# ── The climb (protocol = code) ──────────────────────────────────────

async def _climb(lid: str) -> None:
    # _ACTIVE registration happens in _spawn_climb, synchronously at spawn —
    # the double-climb guard lives there too. This task only ever releases.
    try:
        state = _read_state(lid)
        rungs = state["rungs"]
        while state["next_rung"] < len(rungs) and state["status"] == "running":
            i = state["next_rung"]
            rung = rungs[i]
            h = rung["harness"]
            # Absent or 0 → RUNG_WALL_CAP_MIN; a stored override is bounded by
            # the person's limit (rung_wait_cap) — clamped again here so a
            # hand-written state file, or a limit lowered since, cannot exceed it.
            mm = int(rung.get("max_minutes") or RUNG_WALL_CAP_MIN)
            _cap = rung_wait_cap()
            if _cap is not None:
                mm = min(mm, _cap)
            targets = await _rung_targets(h)
            if h == "*":
                # The bench rung is the REST of the bench: an engine that
                # already climbed (and failed) on an earlier rung of this run
                # does not get re-run inside the last resort.
                climbed = {str(r.get("harness")) for r in rungs[:i]
                           if isinstance(r, dict) and r.get("harness") != "*"}
                _before = list(targets)
                targets = [t for t in targets if t not in climbed]
                if _before and not targets:
                    # Every connected engine already climbed: say THAT, not
                    # "no engine is connected" (the branch below), because
                    # the evidence rides into the next rung's prompt.
                    attempt = {"rung": i, "config": rung, "started_at": time.time(),
                               "session_id": None, "skipped": [],
                               "run_status": "not-dispatched"}
                    state["attempts"].append(attempt)
                    state["evidence"] = (
                        f"Rung {i} (the rest of the bench) had nothing left to run: every "
                        f"connected engine ({', '.join(sorted(climbed & set(_before)))}) "
                        "already climbed on an earlier rung.")
                    attempt["evidence_out"] = state["evidence"]
                    state["next_rung"] = i + 1
                    _write_state(state)
                    continue
            if not targets:
                # The bench rung with nothing connected: nothing to dispatch.
                # Same shape as a skipped credential — climb past it with
                # that as the evidence, never hand fan_out an empty list.
                attempt = {"rung": i, "config": rung, "started_at": time.time(),
                           "session_id": None, "skipped": [],
                           "run_status": "not-dispatched"}
                state["attempts"].append(attempt)
                state["evidence"] = (
                    f"Rung {i} (the whole bench) could not run: no coding engine "
                    "is connected.")
                attempt["evidence_out"] = state["evidence"]
                state["next_rung"] = i + 1
                _write_state(state)
                continue
            req = review.FanOutReq(
                prompt=_with_evidence(state["prompt"], state.get("evidence") or ""),
                project_dir=state.get("project_dir"),
                project_id=state.get("project_id"),
                harnesses=targets,
                harness_model=({h: rung["model"]} if h != "*" and rung.get("model") else None),
                samples=({h: int(rung.get("samples", 1))} if h != "*" and int(rung.get("samples", 1)) > 1 else None),
                max_minutes=mm,
                vcpus=state.get("vcpus"),
            )
            res = await review.fan_out(req)
            attempt = {"rung": i, "config": rung, "started_at": time.time(),
                       "session_id": res.get("session_id"),
                       "skipped": res.get("skipped") or []}
            if not res.get("harnesses_dispatched"):
                # Nothing ran (e.g. credential missing → skipped, alert already
                # sent by fan_out). That's a dispatch failure, not a model
                # failure — climb with that as the evidence.
                attempt.update(run_status="not-dispatched")
                state["attempts"].append(attempt)
                state["evidence"] = (
                    f"Rung {i} ({h}) could not run: {res.get('skipped')} "
                    "(credential missing/expired — a reconnect alert was sent).")
                attempt["evidence_out"] = state["evidence"]
                state["next_rung"] = i + 1
                _write_state(state)
                continue

            sid = res["session_id"]
            # Wait for the rung to end, judged by the session's liveness
            # (review._session_is_over), never by a clock.
            sess = None
            while True:
                await asyncio.sleep(10)
                sess = await review.get_session_service(session_id=sid)
                if sess["status"] not in ("queued", "running"):
                    break
            per = (sess or {}).get("per_harness") or {}

            # Watchdog Tier 0 — post-run transcript analysis per variant.
            stats = {}
            for vid in per:
                tr = await review.get_session_harness_transcript(sid, vid)
                stats[vid] = watchdog.analyze_transcript(
                    tr.get("transcript") or "") if tr.get("available") else \
                    {"parsed": False, "verdict": None}
            attempt["watchdog"] = stats
            attempt["per_harness"] = {
                vid: {k: row.get(k) for k in
                      ("state", "error", "error_type", "exit_code", "model",
                       "elapsed_seconds", "diff_bytes")}
                for vid, row in per.items()}
            attempt["run_status"] = (sess or {}).get("status") or "unknown"

            any_done = any((row.get("state") == "done") for row in per.values())
            if any_done and state.get("test_cmd"):
                bench = await asyncio.to_thread(_verify, sid, state["test_cmd"])
                passed = [r for r in bench.get("results", []) if r.get("passed")]
                attempt["verify"] = {
                    "cmd": state["test_cmd"], "pass_at_k": bench.get("pass_at_k"),
                    "passed_variants": [r.get("vid") for r in passed]}
                state["attempts"].append(attempt)
                if passed:
                    state["status"] = "awaiting_review"   # human = acceptance gate
                else:
                    state["evidence"] = _evidence_from_bench(bench, stats)
                    attempt["evidence_out"] = state["evidence"]
                    state["next_rung"] = i + 1
                _write_state(state)
                continue

            state["attempts"].append(attempt)
            if any_done:
                state["status"] = "awaiting_review"       # no deterministic check
                _write_state(state)
                continue

            # Whole rung failed → classify (evidence-grade reasons) and climb.
            reasons = {vid: watchdog.classify_failure(row, stats.get(vid))
                       for vid, row in per.items()}
            attempt["reasons"] = reasons
            state["evidence"] = _evidence_from_failure(per, reasons, stats)
            attempt["evidence_out"] = state["evidence"]
            state["next_rung"] = i + 1
            _write_state(state)

        if state["status"] == "running":
            state["status"] = "exhausted"   # every rung spent; the user decides by hand
            _write_state(state)
    except Exception as exc:                # noqa: BLE001 — a climb must never die silent
        log.exception("ladder %s climb failed: %s", lid, exc)
        try:
            state = _read_state(lid)
            state["status"] = "error"
            state["error"] = str(exc)[:500]
            _write_state(state)
        except Exception:
            pass
    finally:
        _ACTIVE.discard(lid)


# ── Routes ───────────────────────────────────────────────────────────

class LadderRunReq(BaseModel):
    prompt: str
    project_dir: Optional[str] = None
    project_id: Optional[int] = None
    # Deterministic verify command (the project's own tests). None = no
    # deterministic check → every completed rung waits for the human.
    test_cmd: Optional[str] = None
    # The rungs to climb — normally a coding protocol's chained steps, one
    # rung per step. Omitted = the default ladder (default engine → bench).
    rungs: Optional[list[dict]] = None
    vcpus: Optional[int] = None


@router.post("/run", dependencies=[Depends(requires_permission("write:review"))])
async def start_run(body: LadderRunReq) -> dict:
    rungs = body.rungs or (await get_policy())["rungs"]
    if not rungs:
        raise HTTPException(400, "no rungs: pass rungs (a protocol's steps) or connect an engine")
    await _validate_rungs(rungs)
    lid = uuid.uuid4().hex[:12]
    project_dir = body.project_dir
    if not project_dir and body.project_id is not None:
        cand = Path("/var/lib/cseries/projects") / str(body.project_id)
        if cand.is_dir():
            project_dir = str(cand)
    state = {
        "ladder_id": lid, "status": "running", "next_rung": 0,
        "prompt": body.prompt, "project_dir": project_dir,
        "project_id": body.project_id, "test_cmd": body.test_cmd,
        "rungs": rungs, "vcpus": body.vcpus,
        "attempts": [], "evidence": "", "created_at": time.time(),
    }
    _write_state(state)
    _spawn_climb(lid)
    return {"ok": True, "ladder_id": lid, "rungs": len(rungs),
            "poll_url": f"/api/ladder/runs/{lid}"}


@router.get("/runs", dependencies=[Depends(requires_permission("read:review"))])
async def list_runs(limit: int = 50) -> dict:
    rows = []
    for d in LADDER_ROOT.iterdir() if LADDER_ROOT.exists() else []:
        p = d / "state.json"
        if not p.exists():
            continue
        try:
            s = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        s = _reap_if_orphaned(s)
        rows.append({
            "ladder_id": s.get("ladder_id"), "status": s.get("status"),
            "rung": s.get("next_rung"), "rungs": len(s.get("rungs") or []),
            "attempts": len(s.get("attempts") or []),
            "session_ids": [a.get("session_id") for a in (s.get("attempts") or [])
                            if a.get("session_id")],
            "checked": bool(s.get("test_cmd")),
            "prompt": (s.get("prompt") or "")[:120],
            "created_at": s.get("created_at"), "updated_at": s.get("updated_at"),
        })
    rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return {"runs": rows[:limit]}


@router.get("/runs/{ladder_id}", dependencies=[Depends(requires_permission("read:review"))])
async def get_run(ladder_id: str) -> dict:
    return _reap_if_orphaned(_read_state(ladder_id))


class DecisionReq(BaseModel):
    action: str          # accept | reject
    note: str = ""       # reject: WHY — it becomes the evidence that climbs


@router.post("/runs/{ladder_id}/decision", dependencies=[Depends(requires_permission("write:review"))])
async def decide(ladder_id: str, body: DecisionReq) -> dict:
    """The human gate. accept = done (pick/propose_commit proceeds on the
    attempt's review session as usual). reject = a climb signal everywhere:
    the note is the evidence forwarded to the next rung."""
    state = _read_state(ladder_id)
    if state["status"] != "awaiting_review":
        raise HTTPException(409, f"run is {state['status']}, not awaiting_review")
    if body.action == "accept":
        state["status"] = "accepted"
        state["decision_note"] = body.note
        _write_state(state)
        return {"ok": True, "status": "accepted",
                "session_id": (state["attempts"][-1] or {}).get("session_id")}
    if body.action != "reject":
        raise HTTPException(400, "action must be accept or reject")
    i = state["next_rung"]
    state["evidence"] = (
        f"A human reviewed rung {i} ({state['rungs'][i].get('harness')}) and "
        f"REJECTED the result: {body.note or '(no note)'}")
    if state["attempts"]:
        state["attempts"][-1]["evidence_out"] = state["evidence"]
    state["next_rung"] = i + 1
    if state["next_rung"] >= len(state["rungs"]):
        state["status"] = "exhausted"
        _write_state(state)
        return {"ok": True, "status": "exhausted"}
    state["status"] = "running"
    _write_state(state)
    _spawn_climb(ladder_id)
    return {"ok": True, "status": "running", "next_rung": state["next_rung"]}
