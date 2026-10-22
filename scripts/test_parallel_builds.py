#!/usr/bin/env python3
"""Gate: parallel builds in the Workbench.

The gates, container-free:
  1. two independent phases start in one tick; a dependent one waits
     (the scheduler, _schedule_phases, on real row shapes);
  2. a manufactured conflict produces the INTEGRATION failure, not a phase
     failure (the land, _land_worktree_in_project, on real git worktrees made
     by the real _prepare_worktree);
and around them: the default order a plan without dependencies keeps, the
plan-time refusals (unknown reference, self, circle, unknown engine), and
source pins on the door and the chain that the in-image gate
(scripts/test_parallel_builds_db.py) then drives end to end against a DB.

Run: APP_ROOT=<repo> python3 scripts/test_parallel_builds.py
"""
import ast
import os
import re
import sys
import tempfile
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

FAILS: list = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILS.append(label)


from mastercode.runtime import tools as T  # noqa: E402
from server.routes import review as rv  # noqa: E402


def ph(id, n, status="ready", module_id=None, depends_on=None, gate="none",
       approved_at=None, integration_error=None, title=None):
    return {"id": id, "phase_number": n, "title": title or f"p{n}", "status": status,
            "module_id": module_id, "depends_on": depends_on, "gate": gate,
            "approved_at": approved_at, "integration_error": integration_error,
            "spec": "brief", "description": None, "acceptance": None,
            "harness": None, "model": None}


def ids(rows):
    return [r["id"] for r in rows]


print("== 1. the scheduler: independent phases start together, a dependent one waits ==")
# Two modules, two phases in the first: modules are independent by default,
# phases inside one module are in order.
rows = [ph(1, 1, module_id=10), ph(2, 2, module_id=10), ph(3, 3, module_id=20)]
start, held = T._schedule_phases(rows)
check("two modules' first phases start in ONE scheduling pass", ids(start) == [1, 3], start)
check("...and the second phase of a module waits for the first",
      [(h[0]["id"], h[1]) for h in held] == [(2, "waiting")], held)

# Explicit split: A and B independent, C waits for both.
rows = [ph(1, 1, depends_on=[]), ph(2, 2, depends_on=[]), ph(3, 3, depends_on=[1, 2])]
start, held = T._schedule_phases(rows)
check("depends_on=[] phases start together", ids(start) == [1, 2], start)
check("C waits for both", held and held[0][0]["id"] == 3 and held[0][1] == "waiting"
      and ids(held[0][2]) == [1, 2], held)
rows[0]["status"] = "complete"
rows[1]["status"] = "in_progress"
start, held = T._schedule_phases(rows, unlocked_by=1)
check("A landing does NOT start C while B is still building",
      start == [] and held and held[0][0]["id"] == 3
      and ids(held[0][2]) == [2], (start, held))
rows[1]["status"] = "complete"
start, _ = T._schedule_phases(rows, unlocked_by=2)
check("B landing starts C", ids(start) == [3], start)
start, _ = T._schedule_phases(rows, unlocked_by=1)
check("...and it is unlocked by either of its dependencies once both are done",
      ids(start) == [3], start)

print("== 2. a plan with no dependencies keeps the order it always had ==")
rows = [ph(1, 1), ph(2, 2), ph(3, 3)]
start, _ = T._schedule_phases(rows)
check("a legacy (NULL) root plan starts only its first phase", ids(start) == [1], start)
rows[0]["status"] = "complete"
start, _ = T._schedule_phases(rows, unlocked_by=1)
check("...and phase 1 landing starts phase 2 only", ids(start) == [2], start)
# One Host's project 9198: stale open phases 3-4 behind a hand-built phase 6.
rows = [ph(1, 1, "complete"), ph(2, 2, "complete"), ph(3, 3), ph(4, 4),
        ph(5, 5, "complete"), ph(6, 6, "complete"), ph(7, 7)]
start, _ = T._schedule_phases(rows, unlocked_by=6)
check("the chain from phase 6 goes FORWARD to 7, never back to stale 3",
      ids(start) == [7], start)
rows = [ph(1, 1), ph(2, 2, "archived"), ph(3, 3)]
check("a retired phase is skipped: 3 waits for 1, not for archived 2",
      T._phase_deps(rows[2], rows) == [1], T._phase_deps(rows[2], rows))

print("== 3. what holds a phase, by name ==")
rows = [ph(1, 1, depends_on=[], status="planning"),
        ph(2, 2, depends_on=[], gate="user"),
        ph(3, 3, depends_on=[], integration_error="SPLIT CONFLICT …"),
        ph(4, 4, depends_on=[], status="failed"),
        ph(5, 5, depends_on=[], status="in_progress")]
start, held = T._schedule_phases(rows)
why = {h[0]["id"]: h[1] for h in held}
check("no brief / approval / collision each hold their phase",
      why == {1: "no_brief", 2: "approval", 3: "conflict"}, why)
check("a failed phase is a retry of an approved one and may start; a building one is left alone",
      ids(start) == [4], start)
rows = [ph(1, 1, "review"), ph(2, 2)]
check("a legacy 'review' dependency counts as met: the chain goes on from it",
      ids(T._schedule_phases(rows)[0]) == [2] and ids(T._schedule_phases(rows, unlocked_by=1)[0]) == [2])
check("...and the 'review' phase itself is never started",
      T._schedule_phases([ph(1, 1, "review")]) == ([], []))
for why_code, row in (("waiting", ph(9, 9)), ("no_brief", rows[0]),
                      ("approval", ph(8, 8)), ("conflict", ph(7, 7))):
    txt = T._held_text(row, why_code, [ph(1, 1)] if why_code == "waiting" else None)
    check(f"held reason '{why_code}' is said in words", "phase" in txt and len(txt) > 20, txt)

print("== 4. plan-time: references, circles, engines ==")


def flat(*phs, module=None):
    return [{**p, "_module": module} for p in phs]


f = flat({"title": "Scaffold", "key": "s"}, {"title": "API", "depends_on": ["s"]},
         {"title": "UI", "depends_on": ["scaffold"]}, {"title": "Docs", "depends_on": []},
         {"title": "Glue"})
T._resolve_plan_dependencies(f)
check("depends_on resolves keys and titles to plan positions",
      [p["_deps"] for p in f] == [[], [0], [0], [], [3]], [p["_deps"] for p in f])
f = flat({"title": "a"}, {"title": "b"}, module="api") + flat({"title": "c"}, module="ui")
T._resolve_plan_dependencies(f)
check("omitted depends_on = the phase before it IN ITS MODULE; modules independent",
      [p["_deps"] for p in f] == [[], [0], []], [p["_deps"] for p in f])
for label, bad, needle in (
        ("an unknown reference", flat({"title": "a", "depends_on": ["nope"]}), "not a phase"),
        ("a phase depending on itself", flat({"title": "a", "depends_on": ["a"]}), "itself"),
        ("a circle", flat({"title": "a", "depends_on": ["b"]}, {"title": "b", "depends_on": ["a"]}), "circle"),
        ("an ambiguous title", flat({"title": "x"}, {"title": "x"}, {"title": "y", "depends_on": ["x"]}), "several"),
        ("a duplicate key", flat({"title": "a", "key": "k"}, {"title": "b", "key": "k"}), "key"),
        ("an unknown engine", flat({"title": "a", "harness": "gpt-builder"}), "not a coding engine"),
        ("a non-list", flat({"title": "a", "depends_on": "b"}), "list")):
    try:
        T._resolve_plan_dependencies(bad)
        check(f"the plan refuses {label}", False, "accepted")
    except ValueError as e:
        check(f"the plan refuses {label}", needle in str(e), e)
f = flat({"title": "a", "harness": "codex", "model": "gpt-5.5"})
T._resolve_plan_dependencies(f)
check("a phase keeps its own engine", (f[0]["_harness"], f[0]["_model"]) == ("codex", "gpt-5.5"), f)
check("_find_cycle finds a longer circle",
      T._find_cycle({1: [2], 2: [3], 3: [1]}) is not None
      and T._find_cycle({1: [2], 2: [3], 3: []}) is None)
check("the phase engine: its own wins, else the build's; a model alone rides the build's engine",
      T._phase_engine({"harness": "kimi", "model": None}, "codex", "m1") == ("kimi", None)
      and T._phase_engine({"harness": None, "model": None}, "codex", "m1") == ("codex", "m1")
      and T._phase_engine({"harness": None, "model": "m2"}, "codex", "m1") == ("codex", "m2"))

print("== 5. the land is an integration: merge, or refuse and land nothing ==")
with tempfile.TemporaryDirectory() as td:
    saved = rv.PROJECTS_ROOT
    try:
        rv.PROJECTS_ROOT = Path(td) / "projects"
        proj = rv.PROJECTS_ROOT / "5"
        proj.mkdir(parents=True)
        (proj / "notes.txt").write_text("one\ntwo\nthree\nfour\nfive\n")
        (proj / "keep.txt").write_text("seed\n")

        def worktree(name):
            return rv._prepare_worktree(Path(td) / "sessions" / name, "codex",
                                        seed=True, seed_from=proj)

        a, b, c = worktree("a"), worktree("b"), worktree("c")
        check("the seed is a commit the land can read",
              rv._worktree_base_blobs(a) and "notes.txt" in rv._worktree_base_blobs(a))
        (a / "notes.txt").write_text("ONE\ntwo\nthree\nfour\nfive\n")
        (a / "a.txt").write_text("from a\n")
        (b / "notes.txt").write_text("one\ntwo\nthree\nfour\nFIVE\n")
        (c / "notes.txt").write_text("uno\ntwo\nthree\nfour\nfive\n")
        (c / "c.txt").write_text("from c\n")
        # The host edits keep.txt after every run was seeded.
        (proj / "keep.txt").write_text("host edit\n")

        ch, conf = [], []
        rv._land_worktree_in_project(5, a, ch, None, None, conf, {"phase_id": 101})
        check("A lands its change and its new file", conf == [] and sorted(ch) == ["a.txt", "notes.txt"],
              (ch, conf))
        check("a file the run did not change is NOT written back over a later edit",
              (proj / "keep.txt").read_text() == "host edit\n")
        ch, conf = [], []
        rv._land_worktree_in_project(5, b, ch, None, None, conf, {"phase_id": 102})
        check("B's edit to other lines of the same file MERGES",
              conf == [] and (proj / "notes.txt").read_text() == "ONE\ntwo\nthree\nfour\nFIVE\n",
              ((proj / "notes.txt").read_text(), conf))
        ch, conf = [], []
        landed = rv._land_worktree_in_project(5, c, ch, None, None, conf, {"phase_id": 103})
        check("C's edit to the SAME line conflicts", len(conf) == 1 and conf[0]["path"] == "notes.txt"
              and conf[0]["kind"] == "both_changed", conf)
        check("...names the build that last landed that file (B's merge)",
              (conf[0].get("by") or {}).get("phase_id") == 102, conf)
        check("...and lands NOTHING — not even its non-conflicting new file",
              landed == [] and ch == [] and not (proj / "c.txt").exists(), (landed, ch))

        # Greenfield twins: the same new file, different bytes.
        proj2 = rv.PROJECTS_ROOT / "6"
        proj2.mkdir()
        g1 = rv._prepare_worktree(Path(td) / "s2" / "g1", "kimi", seed=False)
        g2 = rv._prepare_worktree(Path(td) / "s2" / "g2", "kimi", seed=False)
        g3 = rv._prepare_worktree(Path(td) / "s2" / "g3", "kimi", seed=False)
        for g, text in ((g1, "from-a"), (g2, "from-b"), (g3, "from-a")):
            (g / "shared.txt").write_text(text)
            (g / "index.html").write_text("<p>same</p>")
        c1 = []
        rv._land_worktree_in_project(6, g1, None, None, None, c1, {"phase_id": 201})
        c3 = []
        rv._land_worktree_in_project(6, g3, None, None, None, c3, {"phase_id": 203})
        check("the same new file with the same bytes is not a conflict", c1 == [] and c3 == [], (c1, c3))
        c2 = []
        rv._land_worktree_in_project(6, g2, None, None, None, c2, {"phase_id": 202})
        check("the same new file with different bytes IS one (added twice)",
              [x["kind"] for x in c2] == ["added_twice"]
              and (proj2 / "shared.txt").read_text() == "from-a", c2)

        # The project's own .gitignore / .gitattributes
        # must not shape the seed. An ignored file a later run rewrites is an
        # ordinary change, never a second add.
        proj3 = rv.PROJECTS_ROOT / "7"
        (proj3 / "dist").mkdir(parents=True)
        (proj3 / ".gitignore").write_text("dist/\n__pycache__/\n")
        (proj3 / "index.html").write_text("v1\n")
        (proj3 / "dist" / "app.js").write_text("build 1\n")
        (proj3 / "crlf.txt").write_bytes(b"a\r\nb\r\nc\r\nd\r\ne\r\n")
        (proj3 / ".gitattributes").write_text("* text=auto eol=lf\n")
        w = rv._prepare_worktree(Path(td) / "s3" / "w", "codex", seed=True, seed_from=proj3)
        base3 = rv._worktree_base_blobs(w)
        check("an ignored file is IN the seed", base3 and "dist/app.js" in base3, base3)
        check("the seed stores the bytes on disk (no eol conversion)",
              base3 and base3.get("crlf.txt") == rv._blob_sha((proj3 / "crlf.txt").read_bytes()),
              base3)
        (w / "index.html").write_text("v2\n")
        (w / "dist" / "app.js").write_text("build 2\n")
        _d = rv._worktree_diff_host(w)
        check("the run's diff (what the reviewer reads) still leaves ignored files out",
              "index.html" in _d and "dist/app.js" not in _d, _d[:300])
        c7 = []
        rv._land_worktree_in_project(7, w, None, None, None, c7, {"phase_id": 301})
        check("a single build that edits a page and rewrites an ignored build output LANDS",
              c7 == [] and (proj3 / "index.html").read_text() == "v2\n"
              and (proj3 / "dist" / "app.js").read_text() == "build 2\n", c7)
        w1 = rv._prepare_worktree(Path(td) / "s3" / "x1", "codex", seed=True, seed_from=proj3)
        w2 = rv._prepare_worktree(Path(td) / "s3" / "x2", "codex", seed=True, seed_from=proj3)
        (w1 / "crlf.txt").write_bytes(b"A\r\nb\r\nc\r\nd\r\ne\r\n")
        (w2 / "crlf.txt").write_bytes(b"a\r\nb\r\nc\r\nd\r\nE\r\n")
        c8, c9 = [], []
        rv._land_worktree_in_project(7, w1, None, None, None, c8, {"phase_id": 302})
        rv._land_worktree_in_project(7, w2, None, None, None, c9, {"phase_id": 303})
        check("edits to different lines of a CRLF file under an eol attribute merge",
              c8 == [] and c9 == []
              and (proj3 / "crlf.txt").read_bytes() == b"A\r\nb\r\nc\r\nd\r\nE\r\n",
              (c8, c9, (proj3 / "crlf.txt").read_bytes()))
        # A directory where the run has a file: that path is skipped, the rest lands.
        wd = rv._prepare_worktree(Path(td) / "s3" / "d", "codex", seed=True, seed_from=proj3)
        (wd / "later").write_text("a file")
        (wd / "ok.txt").write_text("fine")
        (proj3 / "later").mkdir()
        cd, sk = [], []
        landed_d = rv._land_worktree_in_project(7, wd, None, sk, None, cd, {"phase_id": 304})
        check("a directory in the way skips that one path, the rest lands",
              cd == [] and "later" in sk and (proj3 / "ok.txt").read_text() == "fine"
              and "later" not in landed_d, (cd, sk, landed_d))

        # The plan-end catch-up stages only what git will take: an ignored
        # file named explicitly, or a path that is gone, sinks `git add`.
        import subprocess as _sp
        from server.routes import project_git as pg
        _sp.run(["git", "-C", str(proj3), "init", "-q"], capture_output=True)
        (proj3 / "dist" / "tracked.js").write_text("old\n")
        _sp.run(["git", "-C", str(proj3), "add", "-f", "dist/tracked.js"], capture_output=True)
        _saved_pg = pg._PROJECTS_ROOT
        pg._PROJECTS_ROOT = rv.PROJECTS_ROOT
        try:
            got = pg.committable_paths(7, ["index.html", "dist/app.js", "gone.txt",
                                           "dist/tracked.js"])
        finally:
            pg._PROJECTS_ROOT = _saved_pg
        check("the plan-end catch-up drops ignored and missing paths", got == ["index.html"], got)

        # No seed commit: the land copies, exactly as before.
        plain = Path(td) / "plain"
        plain.mkdir()
        (plain / "x.txt").write_text("x")
        check("a worktree with no seed commit lands as a plain copy",
              rv._land_worktree_in_project(5, plain) == ["x.txt"]
              and (proj / "x.txt").read_text() == "x")
        # _merge3 merges files on disk into an output file: None when
        # merged, else the conflict.
        def merge3(ours, base, theirs):
            m = Path(td) / "m3"
            m.mkdir(exist_ok=True)
            for name, data in (("o", ours), ("b", base), ("t", theirs)):
                (m / name).write_bytes(data)
            why = rv._merge3(m / "o", m / "b", m / "t", m / "out")
            return (m / "out").read_bytes() if why is None else why
        check("a binary file changed on both sides does not merge",
              merge3(b"a\0b", b"a\0", b"a\0c") == {"kind": "both_changed"}
              and merge3(b"1\n2\n3\n", b"1\n2\n", b"0\n1\n2\n") == b"0\n1\n2\n3\n")
    finally:
        rv.PROJECTS_ROOT = saved

print("== 6. source pins: the door, the chain, the delivery ==")
tsrc = (ROOT / "brain/mastercode/runtime/tools.py").read_text()
tree = ast.parse(tsrc)
F = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
seg = lambda n: ast.get_source_segment(tsrc, F[n]) or ""  # noqa: E731
door, start, run = seg("_exec_build"), seg("_autoadvance_start_phase"), seg("_run_phase_build")
check("`build` and a single `run` are no longer refused while other builds run (plan/continue still are)",
      'if action in ("plan", "continue"):' in door
      and 'if action in ("plan", "continue") or (action == "run"' not in door)
check("the door starts every schedulable phase, and claims each before its row opens",
      "_schedule_phases(rows)" in door and "review_met" not in door
      and door.index("_claim_phase(pool, prow)") < door.index("_factory_build_task_start("))
check("the chain claims a phase before opening its row, and releases on failure",
      start.index("_claim_phase(pool, prow)") < start.index("_factory_build_task_start(")
      and "_release_phase_claim" in start)
check("the chain starts EVERY phase the finished one unlocked",
      "for _nxt in _next_phases:" in run)
check("the outcome is decided under the per-project lock (one plan-end, one start)",
      "async with _outcome_lock(project_id):\n        _landed = await _factory_build_task_finish(" in run
      and run.index("async with _outcome_lock(project_id):")
      < run.index("_autoadvance_pick_next(")
      < run.index("# Save point: a SUCCESSFUL"))
check("a split conflict puts the phase back to ready, marks it, and is never retried",
      "integration_error=$2" in run and "_PHASE_READY, _why[:4000]" in run
      and "if phase_id is not None and _landed and _conflict:" in run
      # The retry budget counts the phase's failed rows through one classifier
      # (shared with the automatic repairs);
      # a split-conflict row counts toward nothing there. Driven in
      # scripts/test_build_lane_limits.py.
      and "_failed_build_kind(" in seg("_phase_retry_pick")
      and "(result ? 'integration_conflict')" in seg("_phase_failed_builds")
      and "integration_conflict" in seg("_failed_build_kind"))
check("the delivery says what a split conflict means",
      "split_conflict" in turn_source(ROOT))
check("the door composes the brief BEFORE it claims (a failed read strands nothing)",
      door.index("scoped_intent = await _compose_phase_intent(pool, prow)")
      < door.index("if not await _claim_phase(pool, prow):"))
check("the door says the truth about an in-progress phase with no live build, and a lost claim",
      "_running_phase_text(pool" in door and "the plan's chain took" in door)
check("a build that ends without completing explains the other open phases when it is the last",
      "await _explain_idle(pool, project_id, task_id, phase_id)" in run)
check("the plan end commits every landed save point before it merges — phase files only",
      run.index('phase_label="plan end: remaining landed work"')
      < run.index("_merged = await merge_project_pr(pool, project_id, expect_head=_ci_head)")
      and "paths=_left," in run)
check("a cancelled build fails its phase only when no newer build of it is live",
      "_newer = await pool.fetchval(" in run and "if not _newer:" in run)
check("phase_landed_paths lists only what phases landed",
      (rv._note_landed(99, ["a.txt"], {"phase_id": 1}), rv._note_landed(99, ["b.txt"], {"phase_id": None}),
       rv.phase_landed_paths(99))[2] == ["a.txt"])
adsrc = (ROOT / "brain/mastercode/runtime/agent_dispatch.py").read_text()
check("a reaped build fails its phase, unless another live build of it exists",
      "UPDATE project_phases SET status='failed'" in adsrc
      and "AND NOT EXISTS (SELECT 1 FROM entity_tasks t" in adsrc)
rsrc = (ROOT / "server/routes/review.py").read_text()
check("the seed copy runs under the project lock, and a cancelled land finishes first",
      "async with project_lock(project_id):\n        worktree = await asyncio.to_thread(" in rsrc
      and "await asyncio.wait({_fut})" in rsrc)
check("the land runs under the project write lock, off the event loop",
      "async with project_lock(project_id):" in rsrc
      and "_land_worktree_in_project, project_id, worktree" in rsrc)
gsrc = (ROOT / "server/routes/project_git.py").read_text()
# Every thread that writes the project dir — the save point and the pre-build
# fast-forward, on the platform forge and on the user's machine — runs under
# the project lock: the line before each dispatch takes it.
_g_lines = gsrc.splitlines()
_writers = [i for i, l in enumerate(_g_lines)
            if re.search(r"asyncio\.to_thread\(\s*$|asyncio\.to_thread\(_(sync|prepare)_blocking", l)
            and any(w in " ".join(_g_lines[i:i + 2]) for w in ("_sync_blocking", "_prepare_blocking"))]
check("the save-point commit and the pre-build fast-forward take the same lock",
      len(_writers) == 4 and all(
          "async with project_lock(project_id):" in _g_lines[i - 1]
          or "async with project_lock(project_id):" in _g_lines[i - 2] for i in _writers),
      f"{[(i + 1, _g_lines[i].strip()) for i in _writers]}")
check("review.py still touches neither plan table", "project_phases" not in rsrc
      and "phase_steps" not in rsrc)
mig = (ROOT / "migrations/v175_phase_dependencies.sql").read_text()
init = (ROOT / "init-db.sql").read_text()
for col in ("depends_on", "harness", "model", "integration_error"):
    check(f"v175 adds project_phases.{col} idempotently, and init-db declares it",
          f"ADD COLUMN IF NOT EXISTS {col} " in mig
          and col in init[init.index("CREATE TABLE project_phases"):init.index("CREATE INDEX idx_project_phases_project")])

print()
if FAILS:
    print(f"FAIL: {len(FAILS)} parallel-builds check(s): {FAILS}")
    sys.exit(1)
print("OK: parallel builds — scheduler, integration and chain pins hold")
