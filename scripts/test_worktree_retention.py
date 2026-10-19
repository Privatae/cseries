#!/usr/bin/env python3
"""Gates for run-scoped worktrees + their retention sweep (2026-08-22).

The property under test is the one the numbered runs log promises the user:
a run's built output is NOT destroyed by the next run, so "open app" on run
#43 shows run #43. Before this, the worktree path was project_dir/<harness>
with no run component and _prepare_worktree rmtree'd it on every run — the
same defect that ate a "partial work KEPT at <worktree>" promise in
a resumed run.

Runs without a DB or a pod: pure path/seed/resolver logic plus the sweep's
containment guard.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path[:0] = [".", "brain"]

FAILED = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[worktree] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[worktree] FAIL {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    if not subprocess.run(["git", "--version"], capture_output=True).returncode == 0:
        print("[worktree] SKIP — git unavailable")
        return 0

    from server.routes import review

    # ── 1. The two roots agree (compute mirrors review's constant) ──────────
    from server.routes import compute
    check("compute.SESSIONS_ROOT mirrors review.SESSIONS_ROOT",
          compute.SESSIONS_ROOT == review.SESSIONS_ROOT,
          f"{compute.SESSIONS_ROOT} != {review.SESSIONS_ROOT}")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "projects" / "198"
        project.mkdir(parents=True)
        (project / "index.html").write_text("<h1>landed</h1>")

        # ── 2. Output lands under the parent it was GIVEN, seeded from
        #      elsewhere — the split that makes run-scoping possible ────────
        run_a = tmp / "sessions" / "run-a" / "work"
        run_a.mkdir(parents=True)
        wt_a = review._prepare_worktree(run_a, "kimi", seed=True, seed_from=project)
        check("worktree is created under the parent passed in",
              wt_a == run_a / "kimi" and wt_a.is_dir(), str(wt_a))
        check("seed_from content is copied into the worktree",
              (wt_a / "index.html").read_text() == "<h1>landed</h1>")
        check("seeding does NOT write into the seed source",
              not (project / "kimi").exists())

        # That run produces its result.
        (wt_a / "index.html").write_text("<h1>run A design</h1>")

        # ── 3. THE REGRESSION: a second run must not touch the first ───────
        run_b = tmp / "sessions" / "run-b" / "work"
        run_b.mkdir(parents=True)
        wt_b = review._prepare_worktree(run_b, "kimi", seed=True, seed_from=project)
        (wt_b / "index.html").write_text("<h1>run B design</h1>")
        check("a later run on the SAME harness leaves the earlier run intact",
              (wt_a / "index.html").read_text() == "<h1>run A design</h1>",
              "run A's output was overwritten — the bug this change exists to fix")
        check("the later run has its own distinct output",
              (wt_b / "index.html").read_text() == "<h1>run B design</h1>")
        check("the two runs are different directories", wt_a != wt_b)

        # ── 4. Same-parent reuse still nukes (a retry within one run) ───────
        wt_a2 = review._prepare_worktree(run_a, "kimi", seed=True, seed_from=project)
        check("re-preparing the SAME run's worktree resets it to the seed",
              (wt_a2 / "index.html").read_text() == "<h1>landed</h1>")

        # ── 5. Backward compat: legacy default keeps old behaviour ─────────
        legacy = tmp / "legacy"
        legacy.mkdir()
        (legacy / "a.txt").write_text("x")
        wt_l = review._prepare_worktree(legacy, "codex", seed=True)
        check("seed_from defaults to the worktree parent (legacy callers)",
              (wt_l / "a.txt").read_text() == "x")

    # ── 6. The resolver: new key wins, old sessions still resolve ──────────
    check("_session_worktree_root prefers worktree_root",
          review._session_worktree_root(
              {"project_dir": "/p", "worktree_root": "/s/work"}) == Path("/s/work"))
    check("_session_worktree_root falls back to project_dir for old sessions",
          review._session_worktree_root({"project_dir": "/p"}) == Path("/p"))

    # ── 7. The CALL SITES actually pass a run-scoped parent ────────────────
    # Gate 3 proves _prepare_worktree honours its parent; this proves the
    # callers hand it a per-run one. Reverting either call site to project_dir
    # silently restores the overwrite, and no behavioural test above would
    # notice — the whole fix lives in what these two lines pass.
    rsrc = Path("server/routes/review.py").read_text()
    # Both call sites now go through asyncio.to_thread — seeding copytree's the
    # whole project and was stalling the event loop (2026-08-22) — so the
    # function is a REFERENCE followed by its args rather than a direct call.
    # The property being pinned is unchanged: the root passed is session-scoped.
    check("single-build passes a session-scoped worktree root",
          "worktree_root = session_dir / \"work\"" in rsrc
          and "_prepare_worktree, worktree_root, harness, seed=seed," in rsrc
          and "seed_from=project_dir)" in rsrc)
    check("fan-out passes a session-scoped worktree root",
          "_wt_root = session_dir / \"work\"" in rsrc
          and "_prepare_worktree(_wt_root, v[\"vid\"], seed=seeded," in rsrc)
    check("no worktree path is still derived from project_dir",
          "_prepare_worktree(project_dir" not in rsrc)
    check("both manifests record worktree_root for the resolver",
          rsrc.count('"worktree_root": str(') == 2)

    # ── 7b. ONE session identity: the dir name and the recorded id agree ───
    # A divergence orphans the worktree forever — the sweep looks for a run's
    # files at SESSIONS_ROOT/<harness_run.session_id>/work/<harness>.
    check("session dir name and harness_run.session_id come from one value",
          "bkey = session_id" in rsrc
          and 'session_id = billing_key or f"build-{project_id}-{int(time.time() * 1000)}"' in rsrc,
          "the two identities are computed independently again")

    # ── 7c. `harness` is contained before it becomes the security root ─────
    # It IS the containment root for the pick guard, so an unchecked harness
    # moves the boundary and every path check below measures the wrong thing.
    check("propose_commit contains `harness` before using it as the root",
          "rejected_unsafe_harness" in rsrc
          and "if _wt_root.resolve() not in harness_root.parents:" in rsrc)
    check("a landed solo build is marked committed (so retention keeps it)",
          "auto-accept mark_committed failed" in rsrc)

    # ── 8. The sweep only ever deletes inside the sessions tree ────────────
    src = Path("server/routes/compute.py").read_text()
    check("worktree reclaim is containment-guarded before rmtree",
          "refusing to reclaim out-of-tree path" in src
          and "SESSIONS_ROOT.resolve() not in wt_real.parents" in src)
    check("reclaim skips runs that were picked",
          'if r["kept"] or not r["session_id"]' in src)

    print()
    if FAILED:
        print(f"[worktree] {len(FAILED)} gate(s) FAILED: {', '.join(FAILED)}")
        return 1
    print("[worktree] all gates pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
