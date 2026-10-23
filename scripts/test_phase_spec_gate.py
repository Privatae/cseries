#!/usr/bin/env python3
"""Gates for the phase-brief (spec) lane.

The defect: add_phase could not write a `spec` at all, so a plan the host
restructured in chat landed spec-NULL / status 'planning'. Two consequences,
both silent:
  - the chat build door selected purely on `status <> 'complete'`, so it picked
    such a phase and built it with a TITLE-only brief (_compose_phase_intent
    falls back through spec -> description -> title);
  - the auto-advance chain correctly refused to build it, but said nothing, so
    the user saw a phase land and then silence.

Measured on project 9198: phases 6-11 came out of a
restructure with spec NULL and description ''.

No DB and no pod, but NOT merely lexical: the phase ops and the build door's
readiness gate are EXECUTED against stub pools (FakePool / BuildPool), so a
bypass inserted ahead of the gate shows up as a build that was allowed to
start. The remaining source assertions cover wiring that has no reachable
call path from here (delivery plumbing, the tool schema, sibling writers).

Run: APP_ROOT=<repo> python3 scripts/test_phase_spec_gate.py
"""
import ast
import os
import sys
from pathlib import Path
from turn_source import turn_source  # session.py and the modules split out of it
from tools_source import tools_source  # tools.py and the modules split out of it

# Repo root — APP_ROOT when smoke drives it, else the tree this file lives in.
# Same resolution the sibling gates use (test_autoadvance_selection.py). The
# bare cwd-relative paths this carried before only resolved when the gate was
# run FROM the repo root, which is why it could not be wired into smoke.
_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

FAILED = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[phase-spec] ok   {label}")
    else:
        FAILED.append(label)
        print(f"[phase-spec] FAIL {label}" + (f" — {detail}" if detail else ""))


class FakePool:
    """Minimal asyncpg stand-in over an in-memory phase table.

    Enough to RUN _exec_project_phase rather than grep it — the SQL is matched
    on its distinguishing fragment, which also pins the statements' shape: a
    rewrite that changes what they do will stop matching and fail loudly here.
    """

    def __init__(self):
        self.phases: dict[int, dict] = {}
        self.steps: list[dict] = []
        self._next = 1

    async def fetchval(self, sql, *args):
        if "SELECT 1 FROM projects" in sql:
            return 1                                   # project is owned
        if "COALESCE(MAX(phase_number),0)+1" in sql:
            return len(self.phases) + 1
        if "COALESCE(MAX(step_number),0)+1" in sql:
            return sum(1 for s in self.steps if s["phase_id"] == args[0]) + 1
        if "SELECT status FROM project_phases" in sql:
            row = self.phases.get(args[0])             # archive_phase's diagnosis
            return row["status"] if row else None
        return None

    async def fetchrow(self, sql, *args):
        if "FROM project_phases p LEFT JOIN project_modules" in sql:
            # set_phase_spec reads what the phase will carry (v168).
            row = self.phases.get(args[0])
            if row is None:
                return None
            return {"acceptance": row.get("acceptance"), "validation": row.get("validation"),
                    "module_validation": None}
        if "INSERT INTO project_phases" in sql:
            # v124: proof + gate ride the insert (acceptance jsonb text, gate).
            # v168: the module and how the phase is proven ride it too.
            # v175: what it waits for and its own engine. The
            # plan writer inserts the engine and writes depends_on after every
            # phase has an id; add_phase inserts all three.
            if len(args) == 12:
                (pid, num, title, descr, spec, status, acc, gate, module_id, validation,
                 harness, model) = args
                depends_on = None
            else:
                (pid, num, title, descr, spec, status, acc, gate, module_id, validation,
                 depends_on, harness, model) = args
            row = {"id": self._next, "phase_number": num, "title": title,
                   "description": descr, "spec": spec, "status": status,
                   "acceptance": acc, "gate": gate, "approved_at": None,
                   "module_id": module_id, "validation": validation,
                   "depends_on": depends_on, "harness": harness, "model": model,
                   "integration_error": None}
            self.phases[self._next] = row
            self._next += 1
            return {"id": row["id"], "phase_number": num, "status": status,
                    "gate": gate}
        if "INSERT INTO phase_steps" in sql:
            phase_id, num, title, descr, domain, model = args
            srow = {"id": len(self.steps) + 1, "phase_id": phase_id,
                    "step_number": num, "title": title, "status": "pending",
                    "domain_tag": domain, "execute_model": model}
            self.steps.append(srow)
            return srow
        if "UPDATE project_phases SET spec=" in sql:
            # v124: acceptance/gate ride the repair path as CASE WHEN $given.
            # v175: so do depends_on, harness and model; a rewrite clears the
            # recorded collision.
            (spec, ready, phase_id, project_id,
             acc_given, acc, gate_given, gate, validation,
             deps_given, deps, h_given, harness, model, m_given) = args
            row = self.phases.get(phase_id)
            if row is None:
                return None
            row["spec"] = spec
            if row["status"] == "planning":
                row["status"] = ready
            if acc_given:
                row["acceptance"] = acc
            if gate_given:
                row["gate"] = gate
            row["validation"] = validation
            if deps_given:
                row["depends_on"] = deps
            if h_given:
                row["harness"] = harness
            if m_given:
                row["model"] = model
            row["integration_error"] = None
            return {"id": row["id"], "phase_number": row["phase_number"],
                    "title": row["title"], "status": row["status"],
                    "gate": row["gate"], "validation": validation,
                    "depends_on": row.get("depends_on"), "harness": row.get("harness"),
                    "model": row.get("model")}
        if "UPDATE project_phases SET status=" in sql:
            # archive_phase — the guarded retire. Mirrors the real statement's
            # WHERE: right project, and NOT already complete.
            archived, phase_id, project_id, done = args
            row = self.phases.get(phase_id)
            if row is None or row["status"] == done:
                return None
            row["status"] = archived
            return {"id": row["id"], "phase_number": row["phase_number"],
                    "title": row["title"], "status": row["status"]}
        return None

    async def fetch(self, sql, *args):
        # depends_on validation (v175) reads the project's phases.
        if "FROM project_phases WHERE project_id" in sql:
            return sorted(self.phases.values(), key=lambda r: r["phase_number"])
        return []

    async def execute(self, sql, *args):
        return "UPDATE 1"


class BuildPool:
    """Answers exactly the queries _exec_build runs BEFORE its readiness gate,
    so the gate itself executes against a phase row we control."""

    def __init__(self, phase_row: dict):
        self.phase = phase_row
        self.started = False          # set if a build row is ever opened

    async def fetchval(self, sql, *args):
        if "SELECT 1 FROM projects" in sql:
            return 1                                   # owned
        if "FROM entity_tasks" in sql and "in_progress" in sql:
            return None                                # nothing in flight
        if "SELECT COUNT(*) FROM project_phases" in sql:
            return 1                                   # a plan exists
        if "UPDATE project_phases SET status=" in sql and "RETURNING id" in sql:
            return self.phase["id"]                    # the claim (v175)
        return None

    async def fetchrow(self, sql, *args):
        # Pinned to the OPEN-phase predicate (_PHASE_OPEN_SQL, v110). This
        # matcher read "status <> 'complete'" until 2026-08-24 and stopped
        # matching the moment the six copies of that literal were collapsed
        # into the shared constant — which is the stub earning its keep: a
        # rewrite that changes what the statement DOES fails loudly here
        # rather than silently selecting nothing.
        if "FROM project_phases" in sql and "status NOT IN" in sql:
            return self.phase
        if "INSERT INTO entity_tasks" in sql:
            self.started = True
            return {"task_id": 1}
        return None

    async def fetch(self, sql, *args):
        # The door reads the whole plan to schedule it (v175).
        if "FROM project_phases WHERE project_id" in sql:
            return [self.phase]
        return []

    async def execute(self, sql, *args):
        return "UPDATE 1"


def main() -> int:
    import asyncio
    from mastercode.runtime.tools import _exec_project_phase

    tsrc = tools_source(_ROOT)
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as _P
    P_BUILD = _P["build"]

    # ── FUNCTIONAL: run the ops, don't grep them ────────────────────────────
    pool = FakePool()
    run = lambda a: asyncio.run(_exec_project_phase(a, 1, pool))  # noqa: E731

    bare = run({"op": "add_phase", "project_id": 9, "title": "Backend service"})
    check("add_phase WITHOUT a spec stays 'planning'",
          bare["status"] == "ok" and bare["result"]["status"] == "planning",
          f"got {bare}")
    check("...and says plainly that nothing will build it",
          "WITHOUT a spec" in (bare["result"].get("next_step") or ""),
          f"next_step={bare['result'].get('next_step')!r}")

    # A spec alone makes a phase buildable: proof is optional (checks are notes).
    unproven = run({"op": "add_phase", "project_id": 9, "title": "Unproven",
                    "spec": "Render nationwide radar overlays on a tile map."})
    check("add_phase WITH a spec and no checks is accepted, created ready",
          unproven["status"] == "ok" and unproven["result"]["status"] == "ready",
          f"got {unproven}")
    spec_d = run({"op": "add_phase", "project_id": 9, "title": "Overlays map",
                  "spec": "Render nationwide radar overlays on a tile map.",
                  "validation": "review"})
    check("add_phase WITH a spec is created ready",
          spec_d["result"]["status"] == "ready", f"got {spec_d}")
    check("...and carries no warning",
          "next_step" not in spec_d["result"])

    bare_id = bare["result"]["id"]
    unproven2 = run({"op": "set_phase_spec", "project_id": 9, "phase_id": bare_id,
                     "spec": "Aggregate NWS + NOAA feeds behind a cached API."})
    check("set_phase_spec making a phase buildable with no checks is accepted",
          unproven2["status"] == "ok" and unproven2["result"]["status"] == "ready",
          f"got {unproven2}")
    fixed = run({"op": "set_phase_spec", "project_id": 9, "phase_id": bare_id,
                 "spec": "Aggregate NWS + NOAA feeds behind a cached API.",
                 "validation": "review"})
    check("set_phase_spec promotes a planning phase to ready",
          fixed["result"]["status"] == "ready", f"got {fixed}")
    check("...and the brief is actually stored",
          pool.phases[bare_id]["spec"].startswith("Aggregate NWS"))

    already = run({"op": "set_phase_spec", "project_id": 9,
                   "phase_id": spec_d["result"]["id"], "spec": "revised brief"})
    check("set_phase_spec does NOT demote or re-promote a non-planning phase",
          already["result"]["status"] == "ready")

    empty = run({"op": "set_phase_spec", "project_id": 9, "phase_id": bare_id,
                 "spec": "   "})
    check("set_phase_spec refuses an empty brief",
          empty["status"] == "error" and "spec required" in empty["result"],
          f"got {empty}")

    missing = run({"op": "set_phase_spec", "project_id": 9, "phase_id": 4242,
                   "spec": "x"})
    check("set_phase_spec refuses a phase outside the project",
          missing["status"] == "error" and "not in project" in missing["result"])

    # ── v124: proof + gate are COLUMNS the ops write, validated first ──────
    import json as _json
    from mastercode.runtime.tools import (
        _normalize_acceptance, _phase_awaits_approval, _PHASE_REVIEW)
    import mastercode.runtime.tools as _tools_mod
    gated = run({"op": "add_phase", "project_id": 9, "title": "Web UI",
                 "spec": "Frame the bot UI and add controls.", "gate": "user",
                 "acceptance": [{"cmd": "bash verify.sh"},
                                {"cmd": "test -f index.html", "timeout_s": 5}]})
    check("add_phase stores the gate and the acceptance checks",
          gated["status"] == "ok" and gated["result"]["gate"] == "user"
          and gated["result"]["acceptance_checks"] == 2
          and _json.loads(pool.phases[gated["result"]["id"]]["acceptance"])[0]
              == {"cmd": "bash verify.sh", "expect_exit": 0, "timeout_s": 300},
          f"got {gated}")
    bad = run({"op": "add_phase", "project_id": 9, "title": "x", "spec": "y",
               "acceptance": [{"expect_exit": 0}]})
    check("add_phase REFUSES an acceptance check with no cmd (never lands unrunnable proof)",
          bad["status"] == "error" and "no cmd" in bad["result"], f"got {bad}")
    badgate = run({"op": "add_phase", "project_id": 9, "title": "x", "spec": "y",
                   "gate": "host"})
    check("add_phase REFUSES an unknown gate value",
          badgate["status"] == "error" and "gate must be" in badgate["result"])
    repaired = run({"op": "set_phase_spec", "project_id": 9,
                    "phase_id": bare_id, "spec": "brief v2",
                    "acceptance": ["python3 -m pytest -q"]})
    check("set_phase_spec can add acceptance to an existing phase",
          repaired["status"] == "ok"
          and _json.loads(pool.phases[bare_id]["acceptance"])[0]["cmd"]
              == "python3 -m pytest -q")
    kept = run({"op": "set_phase_spec", "project_id": 9,
                "phase_id": bare_id, "spec": "brief v3"})
    check("...and leaves it alone when not mentioned",
          kept["status"] == "ok"
          and _json.loads(pool.phases[bare_id]["acceptance"])[0]["cmd"]
              == "python3 -m pytest -q")
    _pp_src = ast.get_source_segment(
        tsrc, [n for n in ast.walk(ast.parse(tsrc))
               if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "_exec_project_phase"][0]) or ""
    check("no project_phase op can write approved_at (only the Workbench route does)",
          "approved_at =" not in _pp_src and "approved_at=" not in _pp_src
          and "UPDATE project_phases SET approved_at" not in _pp_src,
          "the host must never be able to approve its own gate")

    # A build's outcome is landed/failed only; checks are information.
    check("_phase_outcome is gone (checks never decide the outcome)",
          not hasattr(_tools_mod, "_phase_outcome")
          and not hasattr(_tools_mod, "_acceptance_failure_text"))
    check("_normalize_acceptance: strings become checks with defaults, [] is none",
          _normalize_acceptance(["make test"])
          == [{"cmd": "make test", "expect_exit": 0, "timeout_s": 300}]
          and _normalize_acceptance([]) is None
          # Honoured as declared (it was once cut to 1800 s without a word);
          # scripts/test_build_lane_limits.py pins the refusal past what the
          # pod can time.
          and _normalize_acceptance([{"cmd": "x", "timeout_s": 99999}])[0]["timeout_s"] == 99999)
    check("_phase_awaits_approval: gated + unapproved only",
          _phase_awaits_approval({"gate": "user", "approved_at": None})
          and not _phase_awaits_approval({"gate": "user", "approved_at": "2026-09-03"})
          and not _phase_awaits_approval({"gate": "none", "approved_at": None})
          and not _phase_awaits_approval({"id": 1}))

    # (the build door's refusal of a gated phase is exercised below, inside the
    # patched-resolver block that already drives the readiness gate)

    # ── no tool may advertise a capability the platform cannot perform ─────
    # The App Factory orchestrator was removed 2026-05-30 (main.py: "dead/
    # unwired + contradicted the 'App Factory organizes; host drives harnesses
    # via fan_out' design") and its SURFACE was left standing. approve_step was
    # still advertised to MCP clients, promising to move a step "sitting in
    # 'checking' state" and to "unblock the orchestrator to run the next step".
    # Nothing has written 'checking' since — its guard could only ever miss, so
    # every call returned "step not in 'checking' state", and the orchestrator
    # it named no longer exists.
    msrc2 = (_ROOT / "server/routes/mcp.py").read_text()
    check("approve_step is gone from every site, not just the tool list",
          "approve_step" not in msrc2,
          "an advertised tool that cannot fire is worse than an absent one")
    check("...and 'checking' is no longer written or waited on by any step op",
          "status = 'checking'" not in msrc2)

    # The states the removed lane owned are still in the CHECK constraints and
    # deliberately left there: dropping a value from an enum is a migration
    # that buys nothing, and a stray legacy row carrying one must not fail a
    # boot. What matters is that no tool CLAIMS to produce them.
    check("no step op claims to move a step INTO a state nothing writes",
          "sitting in 'checking'" not in msrc2)

    # ── ONE execution path drives a plan; a run says it is not one ─────────
    # The platform had two execution models over the same rows.
    # build(action='build') moves phase and step status and completes the
    # project; a run (build action='run', and review.fan_out before it) writes
    # NOTHING — server/routes/review.py does not
    # reference project_phases or phase_steps at all. The SOP taught the second
    # as the way to execute a plan ("Each coding task = one review.fan_out
    # call"), and executed that way a project's phases stay 'ready' however
    # much code lands, so the plan never reflects the work and the project
    # never reaches 'complete'. (It does NOT follow that it cannot ship: there
    # is no status=='complete' ship gate, because shippability is a property
    # of the tree, not the plan state.)
    # Project 9198 only completed because the host used the build door despite
    # writing its plan with the other model's tool.
    rvsrc = (_ROOT / "server/routes/review.py").read_text()
    check("review.py still touches NEITHER plan table",
          "project_phases" not in rvsrc and "phase_steps" not in rvsrc,
          "if fan_out starts writing the plan, this disclosure becomes a lie "
          "and the two models need reconciling on purpose, not by drift")
    check("a run DISCLOSES that it does not advance the plan",
          'out["advances_plan"] = False' in tsrc
          and "does NOT advance the plan" in tsrc)
    check("...only when the project has a plan (no noise otherwise)",
          'if _has_plan else ""' in tsrc)
    b_desc = _P["build"]["function"]["description"]
    check("...and the tool says a planned phase is action='build'",
          "action='build' starts every ready approved phase" in b_desc, b_desc)

    sop = (_ROOT / "default-skills/build-workflow.md").read_text()
    check("the SOP no longer teaches a run as the way to execute a plan",
          "Each coding task = one `review.fan_out` call" not in sop
          and "review.fan_out" not in sop,
          "this line is what produced the unshippable-project shape")
    check("...and states the build door is what drives a phase",
          "One `build(action='build')` runs the approved plan" in sop)
    check("...while keeping compare for deliberate side-by-side runs",
          "variants" in sop and "compare" in sop.lower())

    # ── projects.status: one enum, three sources, no drift (v111) ──────────
    # The CHECK declared in init-db.sql only ever ran on a FRESH entity DB, and
    # not one of the six ALTER TABLE projects migrations touched status. So a
    # long-lived entity kept DEFAULT 'inactive' and NO constraint while a new
    # one had DEFAULT 'planning' and the constraint — measured 2026-08-24:
    # One Host drifted, entity-9122 correct. Same code, two behaviours, and the
    # broken one only shows up for NEW users.
    from server.routes.projects import (
        PROJECT_STATUSES, ProjectsError, update_project_service)

    v111 = _ROOT / "migrations" / "v111_projects_status_converge.sql"
    check("the convergence migration ships", v111.is_file())
    msql = v111.read_text() if v111.is_file() else ""
    check("...it backfills BEFORE constraining (order is load-bearing)",
          msql.find("UPDATE projects") < msql.find("ADD CONSTRAINT"),
          "adding the CHECK first would abort entity startup on legacy rows")
    check("...and cannot fail on an unmeasured value",
          "NOT IN ('planning','active','review','complete','archived')" in msql,
          "a value outside the enum would wedge boot (entrypoint.sh exits 1)")
    check("...and resets the stale default",
          "ALTER COLUMN status SET DEFAULT 'planning'" in msql)

    dbsql = (_ROOT / "init-db.sql").read_text()
    declared = "CHECK (status IN ('planning','active','review','complete','archived'))"
    check("the declared schema, the migration and the code agree on the enum",
          declared in dbsql
          and "CHECK (status IN ('planning','active','review','complete','archived'))" in msql
          and PROJECT_STATUSES == {"planning", "active", "review", "complete", "archived"},
          "three sources define this enum; they must not drift again")

    try:
        asyncio.run(update_project_service(None, 1, 9, status="inactive"))
        _refused = False
    except ProjectsError as e:
        _refused = e.status == 400 and "inactive" in e.detail
    except Exception:
        _refused = False
    check("update_project_service REFUSES an unknown status by name",
          _refused,
          "it wrote status verbatim; the constraint was the only guard, and it "
          "had drifted off long-lived entities entirely")

    for _ok in ("planning", "archived"):
        try:
            asyncio.run(update_project_service(None, 1, 9, status=_ok))
            _accepted = True          # got past validation into the pool call
        except ProjectsError:
            _accepted = False
        except Exception:
            _accepted = True          # died on the None pool, i.e. validation passed
        check(f"...and still accepts the declared status {_ok!r}", _accepted)

    check("list_projects no longer advertises 'inactive' as a status",
          "'inactive' /" not in
          _P["list_projects"]["function"]["parameters"]["properties"]["status"]["description"])

    # ── the DRAFT shape is unbuildable by construction ──────────────────────
    # _compose_default_plan used to fabricate a brief per phase ("Build the
    # core functionality: <intent>") — a title-only brief wearing a spec. It
    # satisfied the readiness gate, so _persist_plan marked it 'ready' and it
    # was indistinguishable from a plan the host had composed. Measured on
    # one Host: 17 phases still carried it, 12 had been BUILT from it.
    from mastercode.runtime.tools import _compose_default_plan
    draft = _compose_default_plan("a dog-grooming business site")
    check("the draft carries NO brief on any phase",
          all(not (ph.get("spec") or "").strip() for ph in draft),
          f"a phase still ships a fabricated brief: {[ph.get('spec') for ph in draft]}")
    check("...and no description standing in for one",
          all(not (ph.get("description") or "").strip() for ph in draft),
          "description feeds the same _brief fallback in _persist_plan")
    check("...but keeps titles and steps as a shape to react to",
          all(ph.get("title") and ph.get("steps") for ph in draft))

    # The whole point: run the draft through the REAL writer and confirm every
    # phase lands unbuildable. This is the assertion that would have caught the
    # original defect — it executes _persist_plan rather than reading it.
    from mastercode.runtime.tools import _persist_plan
    dpool = FakePool()
    dplan = asyncio.run(_persist_plan(dpool, 9, draft))
    check("a drafted plan lands entirely in 'planning' — nothing buildable",
          all(ph["status"] == "planning" for ph in dplan["phases"]),
          f"got {[ph['status'] for ph in dplan['phases']]}")
    composed = asyncio.run(_persist_plan(FakePool(), 9, [
        {"title": "Website foundation", "spec": "Rebrand the adopted homepage."}]))
    check("...while a HOST-composed phase with a brief still lands ready",
          composed["phases"][0]["status"] == "ready",
          f"got {composed['phases'][0]}")

    # The host must be able to pass a plan at all — the runtime read
    # a.get("phases") all along, but the tool schema never
    # declared the parameter, so no host could ever supply one and the
    # fabricated draft was the only reachable outcome.
    bparams = P_BUILD["function"]["parameters"]["properties"]
    check("the build tool EXPOSES `phases` so a composed plan is reachable",
          "phases" in bparams, "runtime reads it; schema never offered it")
    check("...as an array of phase objects carrying a spec",
          bparams.get("phases", {}).get("type") == "array"
          and "spec" in bparams["phases"]["items"]["properties"])
    check("the tool description warns that omitting it yields placeholders",
          "placeholder TITLES" in P_BUILD["function"]["description"])

    # ── retirement: a pivoted plan must be able to drop phases (v110) ───────
    # Until v110 NOTHING could remove a phase — no op here, no REST route, no
    # control in the Workbench — so a project that pivoted kept its dead phases
    # forever, and the only lever that cleared them (the supersede inside
    # action='plan') deleted the replacement plan too.
    retire_me = run({"op": "add_phase", "project_id": 9, "title": "Old direction",
                     "spec": "Superseded when the project pivoted.",
                     "validation": "review"})
    rid = retire_me["result"]["id"]
    arch = run({"op": "archive_phase", "project_id": 9, "phase_id": rid})
    check("archive_phase retires an unbuilt phase",
          arch["status"] == "ok" and arch["result"]["status"] == "archived",
          f"got {arch}")
    check("...and says the project can now complete without it",
          "no longer counts as open work" in (arch["result"].get("next_step") or ""))
    check("...and keeps the row rather than deleting it",
          rid in pool.phases and pool.phases[rid]["title"] == "Old direction")

    pool.phases[rid]["status"] = "complete"
    done_arch = run({"op": "archive_phase", "project_id": 9, "phase_id": rid})
    check("archive_phase REFUSES a completed phase",
          done_arch["status"] == "error" and "is complete" in done_arch["result"],
          f"got {done_arch}")
    check("...and says why rather than shrugging",
          "record of what was built" in done_arch["result"])

    gone = run({"op": "archive_phase", "project_id": 9, "phase_id": 4242})
    check("archive_phase refuses a phase outside the project",
          gone["status"] == "error" and "not in project" in gone["result"])

    # ONE predicate for "still open", not six copies. The six queries that mean
    # it (both auto-advance picks, the behind-count, the build door's pick, the
    # project-complete flip, the halt report) drifted as literals before; a new
    # terminal state honoured by some and ignored by the rest is exactly how an
    # archived phase would keep blocking a project from completing.
    check("no raw <> 'complete' predicate survives in a project_phases query",
          "AND status <> 'complete'" not in tsrc
          and "project_id=$1 AND status <> 'complete'" not in tsrc,
          "a selection still hardcodes the predicate instead of _PHASE_OPEN_SQL")
    check("the shared predicate excludes complete, archived AND review (v124)",
          "_PHASE_OPEN_SQL = (f\"status NOT IN ('{_PHASE_DONE}','{_PHASE_ARCHIVED}',\"" in tsrc
          and "f\"'{_PHASE_REVIEW}')\")" in tsrc)
    check("re-planning does NOT delete retired phases",
          'DELETE FROM project_phases WHERE project_id=$1 AND " + _PHASE_OPEN_SQL' in tsrc,
          "the supersede would take archived history with it")

    mig = _ROOT / "migrations" / "v110_project_phase_archived_status.sql"
    check("the migration widening the CHECK ships with it",
          mig.is_file() and "'archived'" in mig.read_text(),
          "archive_phase would violate project_phases_status_check without it")

    # ── the build door refuses what it cannot brief — EXECUTED, not grepped ──
    # The gate itself runs here. Everything ahead of it in _exec_build is
    # satisfied by BuildPool + a patched resolve_build_target, so a bypass
    # inserted BEFORE the gate line (which the lexical checks below would miss)
    # shows up as a build that was allowed to start.
    from mastercode.runtime import tools as _t
    import server.routes.review as _rv

    async def _fake_resolve(req=None, model=None):
        return ("kimi", None, "")

    _orig_resolve = getattr(_rv, "resolve_build_target", None)
    _rv.resolve_build_target = _fake_resolve
    try:
        for label, phase, expect_refusal, expect_phrase in (
            ("planning + no brief", dict(status="planning", spec=None),
             True, "has no brief"),
            ("planning + HAS a brief", dict(status="planning", spec="a real brief"),
             True, "already has a brief"),
            ("ready", dict(status="ready", spec="a real brief"), False, ""),
            ("failed (a retry)", dict(status="failed", spec="a real brief"),
             False, ""),
        ):
            bp = BuildPool({"id": 7, "phase_number": 2, "title": "Backend",
                            "description": None, **phase})
            out = asyncio.run(_t._exec_build(
                {"action": "build", "project_id": 9}, 1, bp, None, None))
            refused = (out.get("status") == "error"
                       and "not ready to build" in str(out.get("result", "")))
            if expect_refusal:
                check(f"build door REFUSES {label}", refused, f"got {out}")
                check(f"...and diagnoses it correctly ({label})",
                      expect_phrase in str(out.get("result", "")),
                      f"expected {expect_phrase!r} in {out.get('result')!r}")
            else:
                check(f"build door ALLOWS {label}", not refused,
                      f"unexpectedly refused: {out}")

        # v124: the APPROVAL GATE is a column the door refuses on — executed.
        # Entity 9128 project 1's phase 3 carried its gate as a sentence in the
        # brief and was built by the chain; this is the row that stops it.
        gp = BuildPool({"id": 7, "phase_number": 3, "title": "Web UI",
                        "description": None, "spec": "a real brief",
                        "status": "ready", "gate": "user", "approved_at": None,
                        "acceptance": None})
        gout = asyncio.run(_t._exec_build(
            {"action": "build", "project_id": 9}, 1, gp, None, None))
        check("build door REFUSES a gated phase the user has not approved",
              gout.get("status") == "error"
              and "not been approved" in str(gout.get("result", ""))
              and not gp.started, f"got {gout}")
        ap = BuildPool({"id": 7, "phase_number": 3, "title": "Web UI",
                        "description": None, "spec": "a real brief",
                        "status": "ready", "gate": "user",
                        "approved_at": "2026-09-03T15:00:00+00:00",
                        "acceptance": None})
        aout = asyncio.run(_t._exec_build(
            {"action": "build", "project_id": 9}, 1, ap, None, None))
        check("build door ALLOWS the same phase once the user approved it",
              "not been approved" not in str(aout.get("result", "")),
              f"got {aout}")

        # A turn the PLATFORM woke (a result came back; nobody is in it) meets
        # the same gates — they are code, whatever its brief says — and it
        # alone spends a phase's AUTOMATIC repairs: past
        # tools.PHASE_AUTO_REPAIRS landings without completing, the door
        # refuses it and names the question box.
        from mastercode.memory_engine import CURRENT_TURN_WOKEN as _woken

        async def _as_woken(coro_fn):
            tok = _woken.set({"session_id": "s", "woken": True})
            try:
                return await coro_fn()
            finally:
                _woken.reset(tok)

        wp = BuildPool({"id": 7, "phase_number": 3, "title": "Web UI",
                        "description": None, "spec": "a real brief",
                        "status": "ready", "gate": "user", "approved_at": None,
                        "acceptance": None})
        wout = asyncio.run(_as_woken(lambda: _t._exec_build(
            {"action": "build", "project_id": 9}, 1, wp, None, None)))
        check("a WOKEN turn meets the same approval gate at the door",
              wout.get("status") == "error" and "not been approved" in str(wout.get("result", ""))
              and not wp.started, f"got {wout}")

        class RepairPool(BuildPool):
            def __init__(self, phase_row, reviews):
                super().__init__(phase_row)
                self.reviews = reviews

            async def fetchrow(self, sql, *args):
                if "COUNT(*) FILTER" in sql:
                    return {"reviews": self.reviews, "fails": 0}
                return await super().fetchrow(sql, *args)

        _rev = {"id": 7, "phase_number": 3, "title": "Web UI", "description": None,
                "spec": "a real brief", "status": "ready", "gate": "none",
                "approved_at": None, "acceptance": None}
        rp = RepairPool(dict(_rev), 1 + _t.PHASE_AUTO_REPAIRS)
        rout = asyncio.run(_as_woken(lambda: _t._exec_build(
            {"action": "build", "project_id": 9, "phase_id": 7}, 1, rp, None, None)))
        check("a woken turn's rebuild of a phase past its automatic repairs is REFUSED at the door",
              rout.get("status") == "error" and "automatic repairs are spent" in str(rout.get("result"))
              and "question box" in str(rout.get("result")) and not rp.started, f"got {rout}")
        pp = RepairPool(dict(_rev), 1 + _t.PHASE_AUTO_REPAIRS)
        pout = asyncio.run(_t._exec_build(
            {"action": "build", "project_id": 9, "phase_id": 7}, 1, pp, None, None))
        check("...while a person asking for the same build is not refused for it",
              "automatic repairs" not in str(pout.get("result", "")), f"got {pout}")
        lp = RepairPool(dict(_rev), _t.PHASE_AUTO_REPAIRS)
        lout = asyncio.run(_as_woken(lambda: _t._exec_build(
            {"action": "build", "project_id": 9, "phase_id": 7}, 1, lp, None, None)))
        check("...and a woken turn with a repair left may rebuild",
              "automatic repairs" not in str(lout.get("result", "")), f"got {lout}")
    finally:
        if _orig_resolve is not None:
            _rv.resolve_build_target = _orig_resolve

    # ── a chain that stops must SAY so ──────────────────────────────────────
    check("_chain_stop records the reason on the finishing task",
          "async def _chain_stop(" in tsrc
          and "'{chain_stopped}'" in tsrc)
    # Both expectations below were STALE when this gate was first wired into
    # smoke. The gate had never run, so the code moved underneath
    # it: `finished_phase_id` was added to the pick_next call (so the task_id
    # kwarg is no longer last and the old literal's closing paren no longer
    # matched), and the forward-only guard added a FOURTH stop path. Both are
    # legitimate growth — the code was correct, the assertions were not.
    check("the call site passes the finishing task id",
          "chain_depth=chain_depth, finished_task_id=task_id," in tsrc)
    # Grew to SIX with retry-once-then-stop (_phase_retry_pick):
    # a failed phase used to end the chain with no reason at all — the `if ok:`
    # guard meant nothing ran and nothing was said. Its two stop paths are the
    # halt-flag check and the exhausted retry budget. Same kind of legitimate
    # growth as the fourth path above; the point of the count is that a NEW stop
    # path has to be registered here deliberately, never added silently.
    # Then FIVE: the scheduler folded three
    # single-phase stops — earlier phases still incomplete, next phase not
    # ready, next phase awaits approval — into ONE stop that lists every open
    # phase with why it is held (_held_text: waiting / no brief / approval /
    # collision) and names the phases that could build but do not follow. The
    # reasons are all still written; they now ride one note instead of three.
    # SIX again: a phase the PLATFORM failed twice
    # (a wake, a dropped pod connection, a lost stream) stops on its own path,
    # saying it was the platform, instead of spending the work's retry.
    check("all six stop paths write a reason",
          tsrc.count("await _chain_stop(pool, finished_task_id,") == 6,
          f"found {tsrc.count('await _chain_stop(pool, finished_task_id,')}, expected 6 "
          "(depth backstop, halt requested, nothing further starts [lists every "
          "held phase], halted-while-failed, retry budget spent, platform failing)")
    _held = tsrc[tsrc.index("def _held_text("):tsrc.index("async def _autoadvance_pick_next(")]
    check("...and the folded stop still names each reason it replaced",
          "has no brief" in _held and "waiting for the user's approval" in _held
          and "waits for" in _held and "whether to build or drop them is the user's" in tsrc)

    # ── every writer of project_phases honours the same rule ────────────────
    # These two wrote the spec but once left status at its 'planning'
    # default, so a phase created through them was unbuildable despite having a
    # brief — and the new gate would have refused it saying "no brief".
    # _persist_plan — the build(action='plan') door, and the one writer this
    # gate never covered. It hardcodes 'ready' whether or not a brief was
    # supplied, so the fabricated default plan (_compose_default_plan) lands
    # BUILDABLE carrying a one-line restatement of the intent as its spec —
    # a title-only brief wearing a spec, which walks straight through the
    # readiness gate above. Measured on one Host: 17 phases
    # still carried that placeholder brief and 12 of them had been built.
    check("_persist_plan sets ready ONLY when a spec is supplied",
          "VALUES ($1,$2,$3,$4,$5,'ready')" not in tsrc,
          "_persist_plan hardcodes 'ready' — the plan door is the one writer "
          "exempt from the rule the other three follow")

    psrc = (_ROOT / "server/routes/projects.py").read_text()
    check("REST create_phase sets ready when a spec is supplied",
          '_status = "ready" if (body.spec or "").strip() else "planning"' in psrc
          and "(project_id, phase_number, title, description, spec, status, acceptance, gate," in psrc
          # ...and, ready, it must say how it is proven (Workbench Phase 2).
          and '_phase_validation_or_400(_acc, body.validation, None) if _status == "ready"' in psrc)
    # v124: the ONLY writer of approved_at is the user's route, and it is
    # entity-scoped and gated on write:projects like every phase route.
    check("approve_phase is the sole writer of approved_at, behind write:projects",
          psrc.count("SET approved_at = now()") == 1
          and 'approved_at = COALESCE(approved_at, now())' in psrc
          and '"/{project_id}/phases/{phase_id}/approve"' in psrc
          and 'requires_permission("write:projects")' in psrc[psrc.index("/approve"):psrc.index("/approve") + 200])
    check("approve on a 'review' phase completes it on the user's word",
          "if row[\"status\"] == \"review\":" in psrc
          and "SET status = 'complete'" in psrc)
    check("no host tool writes approved_at (tools.py, mcp.py)",
          "approved_at = now()" not in tsrc and "approved_at=now()" not in tsrc
          and "approved_at" not in (_ROOT / "server/routes/mcp.py").read_text())
    msrc = (_ROOT / "server/routes/mcp.py").read_text()
    # The MCP plan writer (order_software's auto-planner) went with the /mcp
    # `factory` scope: no mint path issued the scope.
    check("the MCP plan writer is gone with the factory scope",
          "_auto_plan_project" not in msrc and "order_software" not in msrc.split("TOOL_HANDLERS", 1)[0])
    check("the refusal DIAGNOSES rather than asserting 'no brief'",
          '_has_spec = bool((prow["spec"] or "").strip())' in tsrc
          and "It already has a brief, so it only needs promoting" in tsrc)

    adsrc = (_ROOT / "brain/mastercode/runtime/agent_dispatch.py").read_text()
    check("delivery carries chain_stopped out of the row",
          '"chain_stopped": res.get("chain_stopped"),' in adsrc)

    ssrc = turn_source(_ROOT)
    check("the delivery states it UNCONDITIONALLY, not behind the notes elif",
          'if (record.get("chain_stopped") or "").strip():' in ssrc,
          "a successful build sets preview_url, which would swallow an elif")

    # ── the schema actually exposes it to the host ───────────────────────────
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P
    params = P["project_phase"]["function"]["parameters"]["properties"]
    check("project_phase exposes set_phase_spec", "set_phase_spec" in params["op"]["enum"])
    check("project_phase exposes a spec parameter", "spec" in params)
    check("the tool description warns a spec-less phase is unbuildable",
          "NOTHING will build it" in P["project_phase"]["function"]["description"])

    # ── the dead surface the removed orchestrator left behind ─────────────
    asrc = (_ROOT / "server/routes/applications.py").read_text()
    psrc2 = (_ROOT / "server/routes/projects.py").read_text()
    msrc3 = (_ROOT / "server/routes/mcp.py").read_text()

    # Shippability is about the TREE, not the plan state. Requiring
    # status=='complete' meant re-planning (which flips a completed project
    # back to 'active') made a live app un-republishable until every new phase
    # finished — so opening maintenance could strand a shipped app.
    check("the ship gate no longer refuses on plan state",
          "requires status='complete'" not in asrc,
          "maintenance would block republishing again")
    check("...and refuses on a build IN FLIGHT instead (the real hazard)",
          "a build is still running on this project" in asrc
          and "task_type = 'factory_build'" in asrc)

    # 'running' is not a member of the phase_steps CHECK and nothing
    # writes 'checking', so this predicate matched nothing, always.
    # project_status went with the /mcp `factory` scope.
    check("project_status no longer looks for an in-flight state that never exists",
          "s.status IN ('running', 'checking')" not in msrc3)

    # Columns that were 0 for every row.
    check("the permanently-zero steps_checking column is gone",
          "AS steps_checking" not in psrc2)
    check("the permanently-zero provenance_count column is gone",
          "AS provenance_count" not in psrc2)

    # The same method+path registered twice; FastAPI served the first and the
    # second was unreachable, returning a different shape.
    check("DELETE /{project_id} is declared exactly once",
          psrc2.count("async def archive_project") == 1,
          f"found {psrc2.count('async def archive_project')} definitions")

    # ── the research gate's contract, pinned where it can actually run ─────
    # test_research_paths asserted a retired build gate and
    # nobody noticed for six weeks because nothing ran it. Its assertions are
    # corrected — but that file drives a LIVE entity pod over kubectl exec by
    # design (its own docstring), so it cannot run inside the smoke container:
    # wiring it there red-failed this very ship with FileNotFoundError:
    # 'kubectl'. Wiring it repo-side would fire live research on a live Host
    # during every ship, which is worse.
    #
    # So the live test stays live-only, and the thing that actually rotted —
    # the CONTRACT — is pinned here instead, in a gate that is already wired
    # and needs no infra. If _is_build_gated is renarrowed or rewidened again,
    # this goes red in smoke on the next ship rather than in six weeks.
    from mastercode.runtime.tools import _is_build_gated
    check("research fires for build(action='plan') and nothing else",
          _is_build_gated("build", {"action": "plan"})
          and not _is_build_gated("build", {"action": "build"})
          and not _is_build_gated("build", {})
          and not _is_build_gated("write_file")
          and not _is_build_gated("local_grep"),
          "the contract test_research_paths asserts, checked without a pod")

    print()
    if FAILED:
        print(f"[phase-spec] {len(FAILED)} gate(s) FAILED: {', '.join(FAILED)}")
        return 1
    print("[phase-spec] all gates passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
