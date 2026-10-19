#!/usr/bin/env python3
"""
workbench_bench.py — judge-free quality measurement for fan_out variants.

The Workbench measurement spine:
score variants by VERIFIABLE ground truth — the project's own test command —
never by an LLM judge. A variant passes or it doesn't; pass@k across variants,
pass rate per (harness, model) config across a task suite. Deterministic:
same session + same command → same numbers, every time.

Runs INSIDE a C-Series container (the review-session dirs are local there):

    docker exec <container> \
        python3 /app/scripts/workbench_bench.py verify --session <id> --cmd "pytest -q"

Modes:
  verify     run the verify command in each harness worktree of ONE fan_out
             session; write <session>/bench.json; print per-variant pass/fail
             + pass@k. The command runs in the user's OWN container against
             their OWN project (same trust as running your own tests),
             timeout-bounded.
  aggregate  fold many bench.json files into per-config pass rates + pass@k
             per task — the experiment report (single vs Self-MoA vs Mixed-MoA
             reads straight off this table once sessions are labeled).

Attribution: the REQUESTED model comes from the session manifest
(harness_model — recorded at dispatch); the RUN model from per-harness state.
Cost joins happen in ComputeView (harness_usage carries the billed cost per
run keyed by session_id + harness) — this script measures correctness only.

Honest about scope: `verify` measures the worktree AS COLLECTED. If a harness
failed its run, the worktree may be the untouched seed — that still counts as
its variant (a harness that produced nothing did not solve the task).
"""
from __future__ import annotations

import argparse
import glob as globlib
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

SESSIONS_ROOT = Path("/var/lib/cseries/review-sessions")


def _load_manifest(session_dir: Path) -> dict:
    try:
        return json.loads((session_dir / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"no readable manifest in {session_dir}: {exc}")


def _run_model(session_dir: Path, harness: str) -> str | None:
    """The model the run actually reported (per-harness state), if any."""
    try:
        state = json.loads((session_dir / "per_harness_state.json").read_text())
        return (state.get(harness) or {}).get("model")
    except (OSError, json.JSONDecodeError):
        return None


def verify_session(session_dir: Path, cmd: str, timeout: int) -> dict:
    """Run `cmd` in every harness worktree of the session. Pure ground truth:
    exit 0 = pass. Returns the bench record (also written to bench.json)."""
    manifest = _load_manifest(session_dir)
    project_dir = Path(manifest.get("project_dir") or "")
    requested = manifest.get("harness_model") or {}
    # Variants, not harnesses: a Self-MoA batch runs N samples per harness and
    # keys each worktree by vid (<harness>, <harness>-s2, ...). The manifest's
    # variant expansion is the request-side record; fall back to the plain
    # harness list for pre-multiplex sessions.
    variants = manifest.get("variants") or [
        {"vid": h, "harness": h} for h in (manifest.get("harnesses") or [])]
    results = []
    for v in variants:
        vid = v.get("vid") or v.get("harness")
        h = v.get("harness") or vid
        worktree = project_dir / vid
        if not worktree.is_dir():
            results.append({"harness": h, "vid": vid, "passed": False,
                            "exit_code": None, "error": "worktree missing",
                            "duration_s": 0.0})
            continue
        t0 = time.time()
        try:
            r = subprocess.run(cmd, shell=True, cwd=str(worktree),
                               capture_output=True, text=True, timeout=timeout)
            passed, exit_code, err = (r.returncode == 0), r.returncode, None
            tail = (r.stdout + "\n" + r.stderr)[-1000:]
        except subprocess.TimeoutExpired:
            passed, exit_code, err, tail = False, None, f"timeout after {timeout}s", ""
        results.append({
            "harness": h,
            "vid": vid,
            "model_requested": v.get("model") or requested.get(h),
            "model_run": _run_model(session_dir, vid),
            "passed": passed,
            "exit_code": exit_code,
            "error": err,
            "duration_s": round(time.time() - t0, 2),
            "output_tail": tail,
        })
    record = {
        "session_id": manifest.get("session_id"),
        "verify_cmd": cmd,
        "seeded": manifest.get("seeded"),
        "prompt": (manifest.get("prompt") or "")[:200],
        "results": results,
        "pass_at_k": any(r["passed"] for r in results),
        "k": len(results),
        "verified_at": time.time(),
    }
    (session_dir / "bench.json").write_text(json.dumps(record, indent=2))
    return record


def _config_label(r: dict) -> str:
    model = r.get("model_requested") or r.get("model_run") or "default"
    return f"{r['harness']}:{model}"


def cmd_verify(args) -> int:
    session_dir = Path(args.sessions_root) / args.session
    record = verify_session(session_dir, args.cmd, args.timeout)
    if args.json:
        print(json.dumps(record, indent=2))
        return 0
    print(f"\n=== bench.verify — session {record['session_id']} "
          f"(seeded={record['seeded']}) ===")
    print(f"  cmd: {record['verify_cmd']}")
    for r in record["results"]:
        mark = "PASS" if r["passed"] else "fail"
        why = f" ({r['error']})" if r.get("error") else ""
        print(f"  {mark}  {_config_label(r):<45} exit={r['exit_code']} "
              f"{r['duration_s']:>6.1f}s{why}")
    print(f"  pass@{record['k']}: {'YES' if record['pass_at_k'] else 'no'}")
    return 0 if record["pass_at_k"] else 1


def cmd_aggregate(args) -> int:
    paths = sorted(globlib.glob(args.glob))
    if not paths:
        raise SystemExit(f"no bench.json files match {args.glob}")
    per_config = defaultdict(lambda: [0, 0])   # label -> [passed, total]
    tasks_pass_k, tasks = 0, 0
    for p in paths:
        try:
            rec = json.loads(Path(p).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        tasks += 1
        tasks_pass_k += 1 if rec.get("pass_at_k") else 0
        for r in rec.get("results", []):
            c = per_config[_config_label(r)]
            c[0] += 1 if r.get("passed") else 0
            c[1] += 1
    print(f"\n=== bench.aggregate — {tasks} task(s) from {len(paths)} file(s) ===")
    for label, (passed, total) in sorted(per_config.items(),
                                         key=lambda kv: -(kv[1][0] / kv[1][1])):
        print(f"  {passed:>3}/{total:<3} ({passed / total:>5.0%})  {label}")
    print(f"  pass@k (any variant per task): {tasks_pass_k}/{tasks} "
          f"({tasks_pass_k / tasks:.0%})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="mode", required=True)
    v = sub.add_parser("verify", help="run the verify command per harness worktree")
    v.add_argument("--session", required=True)
    v.add_argument("--cmd", required=True, help="test command, exit 0 = pass")
    v.add_argument("--timeout", type=int, default=300)
    v.add_argument("--sessions-root", default=str(SESSIONS_ROOT))
    v.add_argument("--json", action="store_true")
    v.set_defaults(fn=cmd_verify)
    a = sub.add_parser("aggregate", help="fold bench.json files into config pass rates")
    a.add_argument("--glob", default=str(SESSIONS_ROOT / "*" / "bench.json"))
    a.set_defaults(fn=cmd_aggregate)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
