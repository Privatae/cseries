#!/usr/bin/env python3
"""Project → modules → versions; a plan that knows its target, on a REAL
database.

Gates:
  1. two modules of different targets each build with their OWN contract
     (a public site is not handed the App SDK; a component gets no app
     contract and is not failed for having no index.html);
  2. promoting a run creates a version and leaves the previous one intact;
  3. undo is pointer-only: no version is deleted, the directory follows the
     pointer, and the pointer can move forward again;
  4. a plan that says nothing about proof is accepted (validation is optional
     and decides nothing); a plan naming an unknown validation is refused and
     changes nothing (the previous plan stands);
plus: list_projects carries modules + current versions; a module name that
collides with review machinery is refused; ship refuses a module whose target
it does not serve; adopt(into=module) lands in the module and versions it.

The SQL is the risky half of this (upserts, per-module version numbering
under a row lock, the batched ANY() read), so it runs against Postgres: in
smoke via `docker exec` (the entity pod's own DB), standalone with SMOKE_PG_*
pointing at any DB carrying init-db.sql + migrations. Files go to temp dirs;
the gitea sync is stubbed (a real one would create a repo for the test
project). Everything the test writes to the DB is under one project it
creates and deletes.

Run: SMOKE_PG_HOST=<socket dir> APP_ROOT=<repo> python3 scripts/test_modules_versions.py
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
from pathlib import Path

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "brain"))

FAILS: list = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"  [{str(detail)[:400]}]"))
    if not cond:
        FAILS.append(label)


async def main() -> int:
    import asyncpg
    from server.routes import project_modules as pm
    from server.routes import review as rv
    from server.routes import project_git as pg
    from server import brain_service
    from mastercode.runtime import tools as t

    pool = await asyncpg.create_pool(
        host=os.environ.get("SMOKE_PG_HOST", "/var/run/postgresql"),
        port=int(os.environ.get("SMOKE_PG_PORT", "5432")),
        user=os.environ.get("SMOKE_PG_USER", "entity"),
        password=os.environ.get("SMOKE_PG_PASSWORD") or None,
        database=os.environ.get("SMOKE_PG_DB", "hostmem"),
        min_size=1, max_size=3)
    eid = await pool.fetchval("SELECT id FROM entity_registry ORDER BY id LIMIT 1")
    pid = await pool.fetchval(
        "INSERT INTO projects (entity_id, name, status) VALUES ($1, 'modules gate', 'active') "
        "RETURNING id", eid)
    td = tempfile.mkdtemp(prefix="modules-gate-")
    saved = (pm.PROJECTS_ROOT, pm.VERSIONS_ROOT, rv.PROJECTS_ROOT, rv.SESSIONS_ROOT,
             pg.sync_project_repo, brain_service._pool,
             os.environ.get("FACTORY_ENGINE"), os.environ.get("CSERIES_SMOKE"),
             rv.resolve_build_target, rv._coding_harness_settings)
    synced: list = []

    async def _fake_sync(pool_, project_id, **kw):
        synced.append(kw)
        return {"synced": False, "committed": True, "commit_sha": "c0ffee" + str(len(synced)).zfill(34)}

    async def _target(h, model=None):
        return "claude-code", None, None

    async def _no_settings():
        return {}

    try:
        pm.PROJECTS_ROOT = rv.PROJECTS_ROOT = Path(td) / "projects"
        pm.VERSIONS_ROOT = Path(td) / "module-versions"
        rv.SESSIONS_ROOT = Path(td) / "sessions"
        pg.sync_project_repo = _fake_sync
        brain_service._pool = pool
        rv.resolve_build_target = _target
        rv._coding_harness_settings = _no_settings

        print("== 4. proof is optional; an unknown validation is refused, and changes nothing ==")
        base = await t._exec_build(
            {"action": "plan", "project_id": pid, "intent": "reader + site",
             "phases": [{"title": "Root first", "spec": "an old root phase",
                         "validation": "review"}]}, eid, pool, None)
        check("a root plan with validation is written", base.get("status") == "ok", base)
        before = await pool.fetchval(
            "SELECT COUNT(*) FROM project_phases WHERE project_id=$1", pid)
        bad = await t._exec_build(
            {"action": "plan", "project_id": pid, "intent": "x",
             "modules": [{"name": "reader", "target": "private_app", "validation": "bogus",
                          "phases": [{"title": "Odd", "spec": "build it"}]}]},
            eid, pool, None)
        check("a module with an unknown validation is refused, and nothing was changed",
              bad.get("status") == "error" and "validation must be one of" in bad.get("result", "")
              and "Nothing was changed" in bad.get("result", ""), bad)
        after = await pool.fetchval(
            "SELECT COUNT(*) FROM project_phases WHERE project_id=$1", pid)
        check("...and the previous plan still stands (validated before superseding)",
              before == after == 1, (before, after))
        bad2 = await t._exec_build(
            {"action": "plan", "project_id": pid, "intent": "x",
             "phases": [{"title": "Odd root", "spec": "build it", "validation": "bogus"}]},
            eid, pool, None)
        check("a root phase naming an unknown validation is refused",
              bad2.get("status") == "error" and "validation must be one of" in bad2.get("result", ""),
              bad2)
        check("...and still only one phase stands",
              await pool.fetchval("SELECT COUNT(*) FROM project_phases WHERE project_id=$1", pid) == 1)
        # validation is optional: proof on a non-installable target and an
        # empty validation are both accepted (nothing reads it to decide).
        pm.validate_module_fields("site", "public_site", "proof")
        pm.validate_module_fields("site", "public_site", "")
        check("validate_module_fields accepts 'proof' on a non-installable target and an empty value", True)
        try:
            pm.validate_module_fields("site", "public_site", "bogus")
            check("validate_module_fields still refuses an unknown value", False, "accepted")
        except pm.ModuleError:
            check("validate_module_fields still refuses an unknown value", True)
        vm, vp = t._validate_plan(None, [{"name": "quiet", "target": "component",
                                          "phases": [{"title": "p", "spec": "s"}]}])
        check("a module and phase that say nothing about proof validate, recorded as 'review'",
              vm[0]["validation"] == "review" and vp[0]["_validation"] == "review", (vm, vp))
        quiet = await t._exec_build(
            {"action": "plan", "project_id": pid, "intent": "x",
             "phases": [{"title": "Unproven root", "spec": "build it"}]}, eid, pool, None)
        check("a root plan whose phase says nothing about proof is accepted",
              quiet.get("status") == "ok", quiet)
        qrow = await pool.fetchrow(
            "SELECT status, validation FROM project_phases WHERE project_id=$1 "
            "AND title='Unproven root'", pid)
        check("...and the phase is ready to build, validation 'review'",
              qrow is not None and (qrow["status"], qrow["validation"]) == ("ready", "review"),
              dict(qrow) if qrow else None)
        bad4 = await t._exec_build(
            {"action": "plan", "project_id": pid, "intent": "x",
             "modules": [{"name": "codex", "target": "component", "validation": "review",
                          "phases": [{"title": "p", "spec": "s"}]}]}, eid, pool, None)
        check("a module named like review machinery (a harness id) is refused",
              bad4.get("status") == "error" and "machinery" in bad4.get("result", ""), bad4)

        good = await t._exec_build(
            {"action": "plan", "project_id": pid, "intent": "reader + site",
             "modules": [
                 {"name": "reader", "target": "private_app", "validation": "checks",
                  "phases": [{"title": "Reader UI", "spec": "the reader",
                              "acceptance": [{"cmd": "test -f index.html"}]}]},
                 {"name": "site", "target": "public_site", "validation": "review",
                  "phases": [{"title": "Landing", "spec": "the landing page"}]},
                 {"name": "api", "target": "component", "validation": "review",
                  "phases": [{"title": "API", "spec": "a small python api"}]}]},
            eid, pool, None)
        check("a module plan is written", good.get("status") == "ok", good)
        rows = await pool.fetch(
            "SELECT p.title, p.validation, m.name FROM project_phases p "
            "JOIN project_modules m ON m.id = p.module_id WHERE p.project_id=$1 "
            "ORDER BY p.phase_number", pid)
        check("each phase belongs to its module and records how it is proven",
              [(r["name"], r["validation"]) for r in rows]
              == [("reader", "checks"), ("site", "review"), ("api", "review")],
              [dict(r) for r in rows])
        check("module directories exist in the one tree",
              all((pm.PROJECTS_ROOT / str(pid) / n).is_dir() for n in ("reader", "site", "api")))

        print("== 1. each module builds with its own contract ==")
        os.environ["FACTORY_ENGINE"], os.environ["CSERIES_SMOKE"] = "stub", "1"
        prompts: dict = {}
        _orig_stub = rv._stub_harness_build

        def _stub(worktree, prompt):
            key = ("site" if "PUBLIC SITE" in prompt else
                   "api" if "COMPONENT" in prompt else "reader")
            prompts[key] = prompt
            if key == "api":
                (worktree / "server.py").write_text("print('api')\n")
                return {"ok": True, "harness": "stub", "exit_code": 0}
            return _orig_stub(worktree, prompt)
        rv._stub_harness_build = _stub
        outs = {}
        for name, target in (("reader", "private_app"), ("site", "public_site"), ("api", "component")):
            outs[name] = await rv.run_single_harness_build(
                project_id=pid, prompt=f"build the {name}", harness="claude-code",
                module=name, target=target)
        check("the private app is handed the App SDK contract",
              "PLATFORM DEPLOYMENT CONTRACT" in prompts.get("reader", "")
              and "app.yaml" in prompts.get("reader", ""))
        check("the public site gets the site contract, not the App SDK",
              "PUBLIC SITE" in prompts.get("site", "")
              and "PLATFORM DEPLOYMENT CONTRACT" not in prompts.get("site", "")
              and "window.CSeriesApp" in prompts.get("site", ""))   # named as forbidden
        check("the component gets no app contract",
              "COMPONENT" in prompts.get("api", "")
              and "PLATFORM DEPLOYMENT CONTRACT" not in prompts.get("api", ""))
        check("the component is not failed for having no index.html",
              outs["api"].get("ok") is True, outs["api"].get("error"))
        check("each build landed in its own module directory",
              (pm.PROJECTS_ROOT / str(pid) / "api" / "server.py").is_file()
              and (pm.PROJECTS_ROOT / str(pid) / "reader" / "index.html").is_file()
              and not (pm.PROJECTS_ROOT / str(pid) / "server.py").exists())

        print("== a build through the door becomes the module's next version ==")
        r = await t._exec_build({"action": "run", "project_id": pid,
                                 "prompt": "tweak the api", "module": "api"},
                                eid, pool, "sess-mod", f"project:{pid}")
        tid = (r.get("result") or {}).get("task_id")
        task = t._FACTORY_BUILD_TASKS.get(tid)
        if task is not None:
            await asyncio.wait_for(task, 120)
        row = await pool.fetchrow("SELECT status, result FROM entity_tasks WHERE task_id=$1", tid)
        res = json.loads(row["result"]) if row and isinstance(row["result"], str) else (row["result"] if row else {})
        check("build(action='run', module='api') ran and landed",
              r.get("status") == "ok" and row and row["status"] == "completed", (r, row and row["status"]))
        check("...recorded as the api module's version 1, with the save point",
              (res.get("version") or {}).get("number") == 1
              and (res.get("version") or {}).get("commit", "").startswith("c0ffee"), res.get("version"))
        check("...committing the module's directory only",
              any(k.get("paths") == ["api"] for k in synced), synced)
        r_bad = await t._exec_build({"action": "run", "project_id": pid,
                                     "prompt": "x", "module": "nope"}, eid, pool, "s", None)
        check("a run into an unknown module is refused by name",
              r_bad.get("status") == "error" and "no module 'nope'" in r_bad.get("result", ""), r_bad)
        await pool.execute("DELETE FROM entity_tasks WHERE task_id=$1", tid)

        print("== 2-3. versions: promote keeps the previous; undo is pointer-only ==")
        v1 = await pm.record_version(pool, pid, "reader", source="build", run_id=None,
                                     commit_sha="a" * 40, note="first")
        rd = pm.PROJECTS_ROOT / str(pid) / "reader"
        (rd / "index.html").write_text("<p>version two</p>")
        (rd / "extra.js").write_text("// v2 only")
        v2 = await pm.record_version(pool, pid, "reader", source="build", commit_sha="b" * 40)
        check("promoting creates the next version", (v1["number"], v2["number"]) == (1, 2), (v1, v2))
        check("...and leaves the previous intact (its snapshot unchanged)",
              "version two" not in (pm.version_dir(pid, "reader", 1) / "index.html").read_text())
        mods = await pm.list_modules(pool, pid)
        cur = {m["name"]: (m["current_version"] or {}).get("number") for m in mods}
        check("the pointer is at the newest version", cur.get("reader") == 2, mods)
        und = await pm.undo_version(pool, pid, "reader")
        vers = await pm.list_versions(pool, pid, "reader")
        check("undo moves the pointer back one version",
              und["number"] == 1 and und["from"] == 2, und)
        check("...deletes no version", sorted(v["number"] for v in vers) == [1, 2], vers)
        check("...and the directory follows the pointer (v2's extra file is gone)",
              not (rd / "extra.js").exists()
              and "version two" not in (rd / "index.html").read_text())
        fwd = await pm.set_current_version(pool, pid, "reader", 2)
        check("the pointer can move forward again", fwd["number"] == 2
              and (rd / "extra.js").exists(), fwd)
        try:
            await pm.undo_version(pool, pid, "site")
            check("undo on a module with no version is refused", False, "accepted")
        except pm.ModuleError as e:
            check("undo on a module with no version is refused", "no version" in str(e), e)

        print("== list_projects carries modules + current versions ==")
        lp = await t._exec_list_projects({"search": "modules gate"}, eid, pool)
        row = next((p for p in (lp.get("result") or {}).get("projects") or []
                    if p["project_id"] == pid), {})
        mods = {m["name"]: m for m in row.get("modules") or []}
        check("every module is listed with its target",
              {k: v["target"] for k, v in mods.items()}
              == {"api": "component", "reader": "private_app", "site": "public_site"}, mods)
        check("...and the reader's current version is 2 of 2",
              (mods.get("reader", {}).get("current_version") or {}).get("number") == 2
              and mods["reader"]["version_count"] == 2, mods.get("reader"))

        print("== adopt(into=module) lands in the module and versions it ==")
        sd = rv.SESSIONS_ROOT / "cmp1"
        wt = sd / "work" / "codex-s2"
        wt.mkdir(parents=True)
        (wt / "index.html").write_text("<p>adopted</p>")
        (sd / "manifest.json").write_text(json.dumps({
            "session_id": "cmp1", "project_id": pid, "worktree_root": str(sd / "work"),
            "variants": [{"vid": "codex", "harness": "codex"},
                         {"vid": "codex-s2", "harness": "codex"}]}))
        ad = await rv.adopt_run_service(session_id="cmp1", harness="codex-s2",
                                        project_id=pid, into="site")
        check("the pick lands in the module's directory, not the root",
              (pm.PROJECTS_ROOT / str(pid) / "site" / "index.html").read_text() == "<p>adopted</p>"
              and not (pm.PROJECTS_ROOT / str(pid) / "index.html").exists(), ad)
        check("...and becomes the module's version 1", (ad.get("version") or {}).get("number") == 1, ad)
        check("...committed under the module's path", synced and synced[-1].get("paths") == ["site/index.html"],
              synced[-1] if synced else None)
        try:
            await rv.adopt_run_service(session_id="cmp1", harness="codex-s2",
                                       project_id=pid, into="nope")
            check("adopt into an unknown module is refused", False, "accepted")
        except Exception as e:
            check("adopt into an unknown module is refused", "no module" in str(getattr(e, "detail", e)), e)

        print("== ship serves only the targets it can ==")
        from server.routes.applications import ship_project_service, ShipError
        try:
            await ship_project_service(pool, pid, verify=False, module="site")
            check("installing a public site is refused by target", False, "accepted")
        except ShipError as e:
            check("installing a public site is refused by target",
                  "cannot be installed" in str(e.detail), e.detail)

        # ── Edge cases, each pinned by a check that fails without its fix. ──
        print("== a plan that writes nothing, or fails part-way, changes nothing ==")
        n0 = await pool.fetchval("SELECT COUNT(*) FROM project_phases WHERE project_id=$1", pid)
        emp = await t._exec_build(
            {"action": "plan", "project_id": pid, "intent": "x",
             "modules": [{"name": "ghost", "target": "component", "validation": "review",
                          "phases": [{"spec": "untitled"}]}]}, eid, pool, None)
        n1 = await pool.fetchval("SELECT COUNT(*) FROM project_phases WHERE project_id=$1", pid)
        check("a module plan with no titled phase is refused",
              emp.get("status") == "error" and "no phase with a title" in emp.get("result", ""), emp)
        check("...the previous plan stands and no module was created",
              n0 == n1 and n0 > 0 and await pm.get_module(pool, pid, "ghost") is None, (n0, n1))
        mid = await t._exec_build(
            {"action": "plan", "project_id": pid, "intent": "x",
             "modules": [{"name": "ghost2", "target": "component", "validation": "review",
                          "phases": [{"title": "fine", "spec": "a"},
                                     {"title": "bad\x00title", "spec": "b"}]}]},
            eid, pool, None)
        n2 = await pool.fetchval("SELECT COUNT(*) FROM project_phases WHERE project_id=$1", pid)
        check("a write that fails part-way is refused as 'nothing was changed'",
              mid.get("status") == "error" and "Nothing was" in mid.get("result", ""), mid)
        check("...and it really changed nothing (one transaction)",
              n2 == n0 and await pm.get_module(pool, pid, "ghost2") is None, (n0, n2))

        print("== acceptance commands are recorded as checks everywhere ==")
        v9 = t._validate_plan(None, [{"name": "rv9", "target": "private_app", "validation": "review",
                                      "phases": [{"title": "t", "spec": "s",
                                                  "acceptance": [{"cmd": "true"}]}]}])[1]
        check("a review-module phase WITH acceptance is recorded as checks (as add_phase does)",
              v9[0]["_validation"] == "checks" == t._phase_validation_for([{"cmd": "true"}], None, "review")[0],
              v9)

        print("== a module directory that is a link is refused ==")
        await pm.upsert_module(pool, pid, name="linked", target="component", validation="review")
        outside = Path(td) / "outside"
        outside.mkdir()
        (outside / "precious.txt").write_text("keep")
        ld = pm.PROJECTS_ROOT / str(pid) / "linked"
        ld.rmdir()
        ld.symlink_to(outside, target_is_directory=True)
        try:
            pm.module_dir(pid, "linked")
            check("module_dir refuses a linked module directory", False, "accepted")
        except pm.ModuleError as e:
            check("module_dir refuses a linked module directory", "is a link" in str(e), e)
        try:
            await pm.record_version(pool, pid, "linked", source="build")
            check("versioning through a linked module dir is refused", False, "accepted")
        except pm.ModuleError as e:
            check("versioning through a linked module dir is refused", "is a link" in str(e), e)
        check("...and the link's target is untouched", (outside / "precious.txt").read_text() == "keep")
        ld.unlink()
        await pool.execute("DELETE FROM project_modules WHERE project_id=$1 AND name='linked'", pid)

        print("== a restore keeps adopt's bookkeeping and backs up what it displaces ==")
        (rd / ".adopt-backups" / "b1").mkdir(parents=True, exist_ok=True)
        (rd / ".adopt-backups" / "b1" / "keep.txt").write_text("rollback store")
        (rd / ".adopt-provenance.json").write_text("{}")
        (rd / "scratch.txt").write_text("unversioned work")
        und2 = await pm.undo_version(pool, pid, "reader")
        check("adopt's rollback store and provenance survive the restore",
              (rd / ".adopt-backups" / "b1" / "keep.txt").is_file()
              and (rd / ".adopt-provenance.json").is_file(), und2)
        _bk = pm.PROJECTS_ROOT / str(pid) / (und2.get("backup") or "missing")
        check("unversioned work is moved to a named backup, not deleted",
              not (rd / "scratch.txt").exists() and (_bk / "scratch.txt").read_text() == "unversioned work",
              und2)
        # The pointer move takes the module row lock record_version takes.
        mid_ = (await pm.get_module(pool, pid, "reader"))["id"]
        async with pool.acquire() as holder:
            tr = holder.transaction()
            await tr.start()
            await holder.execute("SELECT 1 FROM project_modules WHERE id=$1 FOR UPDATE", mid_)
            mover = asyncio.create_task(pm.set_current_version(pool, pid, "reader", 2))
            await asyncio.sleep(0.8)
            check("a restore waits for the module row lock before touching the directory",
                  not mover.done() and not (rd / "extra.js").exists())
            await tr.rollback()
        await asyncio.wait_for(mover, 10)
        check("...and completes once it is released", mover.result()["number"] == 2)

        print("== propose_commit on a module compare lands in the module ==")
        sd2 = rv.SESSIONS_ROOT / "cmp2"
        (sd2 / "work" / "codex").mkdir(parents=True)
        (sd2 / "work" / "codex" / "pc.html").write_text("<p>picked</p>")
        (sd2 / "manifest.json").write_text(json.dumps({
            "session_id": "cmp2", "project_id": pid, "module": "site",
            "project_dir": str(pm.module_dir(pid, "site")),
            "worktree_root": str(sd2 / "work"),
            "variants": [{"vid": "codex", "harness": "codex"}]}))
        try:
            await rv.propose_commit(rv.ProposeCommitReq(session_id="cmp2", picks={"pc.html": "codex"}))
        except Exception as e:  # the staging git step is not what this checks
            print(f"    (propose_commit after the copy: {type(e).__name__})")
        check("the picked file is in the module directory",
              (pm.PROJECTS_ROOT / str(pid) / "site" / "pc.html").is_file())
        _sv = {v["number"]: v for v in await pm.list_versions(pool, pid, "site")}
        check("a pick into a module becomes its next version",
              2 in _sv and _sv[2]["current"] and (pm.version_dir(pid, "site", 2) / "pc.html").is_file(),
              _sv)
        check("...and NOT copied to the project root",
              not (pm.PROJECTS_ROOT / str(pid) / "pc.html").exists())
        check("staging a module pick creates NO repo inside the module",
              not (pm.PROJECTS_ROOT / str(pid) / "site" / ".git").exists())
        check("...it stages in the project's one repo, under the module path",
              (pm.PROJECTS_ROOT / str(pid) / ".git").is_dir())
        # A nested repo an earlier run left behind (with history in it) is
        # moved aside into the backup store and named — never deleted.
        import subprocess as _sp
        _site = pm.PROJECTS_ROOT / str(pid) / "site"
        _sp.run(["git", "init", "-q"], cwd=_site, check=True)
        _sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                 "--allow-empty", "-m", "old history"], cwd=_site, check=True)
        (sd2 / "work" / "codex" / "pc2.html").write_text("<p>again</p>")
        try:
            r4b = await rv.propose_commit(rv.ProposeCommitReq(session_id="cmp2",
                                                              picks={"pc2.html": "codex"}))
        except Exception as e:  # noqa: BLE001
            r4b = {"error": repr(e)}
        _moved = (r4b or {}).get("nested_git_moved")
        _hist = (_sp.run(["git", "--git-dir", str(pm.PROJECTS_ROOT / str(pid) / _moved), "log",
                          "--format=%s"], capture_output=True, text=True).stdout
                 if _moved else "")
        check("a nested .git from an earlier run is moved aside with its history, and named",
              _moved and not (_site / ".git").exists() and "old history" in _hist, (r4b, _hist))
        _staged = _sp.run(["git", "diff", "--cached", "--name-only"], capture_output=True, text=True,
                          cwd=pm.PROJECTS_ROOT / str(pid)).stdout.split()
        check("...and the pick is staged in the project repo as site/pc2.html",
              "site/pc2.html" in _staged, _staged)

        print("== adopt with no `into` on a module run lands in its module ==")
        sd3 = rv.SESSIONS_ROOT / "cmp3"
        (sd3 / "work" / "codex").mkdir(parents=True)
        (sd3 / "work" / "codex" / "six.html").write_text("<p>six</p>")
        (sd3 / "manifest.json").write_text(json.dumps({
            "session_id": "cmp3", "project_id": pid, "module": "site",
            "worktree_root": str(sd3 / "work"),
            "variants": [{"vid": "codex", "harness": "codex"}]}))
        ad6 = await rv.adopt_run_service(session_id="cmp3", harness="codex", project_id=pid)
        check("the run lands in the module it was built for",
              (pm.PROJECTS_ROOT / str(pid) / "site" / "six.html").is_file()
              and not (pm.PROJECTS_ROOT / str(pid) / "six.html").exists(), ad6)
        # site: v1 the adopt above, v2 + v3 the two compare picks (above),
        # v4 this.
        check("...and is versioned there", ad6.get("module") == "site"
              and (ad6.get("version") or {}).get("number") == 4, ad6)

        print("== no writer of a buildable phase requires proof; unknown values are refused ==")
        from server.routes import projects as prj
        from fastapi import HTTPException as _HE
        _saved_eid = brain_service._entity_id
        brain_service._entity_id = eid
        try:
            nop = await prj.create_phase(pid, prj.CreatePhase(title="rest", spec="build it"))
            check("REST create of a ready phase with no proof is accepted, recorded 'review'",
                  nop.get("status") == "ready" and nop.get("validation") == "review", nop)
            try:
                await prj.create_phase(pid, prj.CreatePhase(title="rest bad", spec="b",
                                                            validation="bogus"))
                check("REST create with an unknown validation is refused", False, "accepted")
            except _HE as e:
                check("REST create with an unknown validation is refused",
                      e.status_code == 400, e.detail)
            okp = await prj.create_phase(pid, prj.CreatePhase(title="rest ok", spec="b",
                                                              validation="review"))
            check("...and accepted with validation='review', recorded", okp.get("validation") == "review", okp)
            chk = await prj.create_phase(pid, prj.CreatePhase(title="rest chk", spec="b",
                                                              acceptance=[{"cmd": "true"}]))
            check("...acceptance records checks", chk.get("validation") == "checks", chk)
            await prj.update_phase(pid, chk["id"], prj.UpdatePhase(acceptance=[]))
            check("REST clearing the only check of a ready phase is accepted", True)
            pl = await prj.create_phase(pid, prj.CreatePhase(title="rest plan"))
            await prj.update_phase(pid, pl["id"], prj.UpdatePhase(spec="x", status="ready"))
            plv = await pool.fetchrow("SELECT status, validation FROM project_phases WHERE id=$1", pl["id"])
            check("REST making a phase ready with no proof is accepted, recorded 'review'",
                  (plv["status"], plv["validation"]) == ("ready", "review"), dict(plv))
            await prj.update_phase(pid, pl["id"], prj.UpdatePhase(title="renamed"))
            check("...a plain title edit is untouched", True)
            fl = await prj.create_phase(pid, prj.CreatePhase(title="rest failed", spec="b",
                                                             acceptance=[{"cmd": "true"}]))
            await pool.execute("UPDATE project_phases SET status='failed' WHERE id=$1", fl["id"])
            await prj.update_phase(pid, fl["id"], prj.UpdatePhase(acceptance=[]))
            check("REST clearing the only check of a FAILED phase is accepted", True)
            try:
                await prj.update_phase(pid, fl["id"], prj.UpdatePhase(validation="bogus"))
                check("REST setting an unknown validation on a FAILED phase is refused",
                      False, "accepted")
            except _HE as e:
                check("REST setting an unknown validation on a FAILED phase is refused",
                      e.status_code == 400, e.detail)
        finally:
            brain_service._entity_id = _saved_eid

        # The MCP auto-planner checked here went with the /mcp `factory`
        # scope (no mint path issued it).

        print("== plan-end delivery ships every installable module ==")
        import mastercode.runtime.app_verify as _av
        from server.routes import project_git as _pgit
        shipped_mods: list = []
        _s_sv, _s_merge = _av.ship_and_verify, getattr(_pgit, "merge_project_pr", None)

        async def _sv(**kw):
            shipped_mods.append(kw.get("module"))
            return {"shipped": True, "app_id": f"app-{kw.get('module')}"}

        async def _merge(pool_, project_id):
            return {"merged": False, "reason": "stub"}
        _av.ship_and_verify, _pgit.merge_project_pr = _sv, _merge
        await pool.execute(
            "UPDATE projects SET config = COALESCE(config,'{}'::jsonb) || '{\"auto_merge\": true}' "
            "WHERE id=$1", pid)
        # The root carries a manifest of its own, so the root build lands
        # clean — what is under test is what the delivery then ships.
        _stub_before = rv._stub_harness_build

        def _root_stub(worktree, prompt):
            out_ = _orig_stub(worktree, prompt)
            if not (worktree / "app.yaml").exists():
                (worktree / "app.yaml").write_text(
                    "id: gate-root\nname: Gate root\nversion: 0.1.0\nentrypoint: index.html\n")
            return out_
        rv._stub_harness_build = _root_stub
        _s_troot = t._PROJECTS_ROOT
        t._PROJECTS_ROOT = type(_s_troot)(pm.PROJECTS_ROOT)
        try:
            r8 = await t._exec_build({"action": "run", "project_id": pid,
                                      "prompt": "root tweak"}, eid, pool, "sess-8", f"project:{pid}")
            tid8 = (r8.get("result") or {}).get("task_id")
            task8 = t._FACTORY_BUILD_TASKS.get(tid8)
            if task8 is not None:
                await asyncio.wait_for(task8, 120)
            _row8 = await pool.fetchrow("SELECT status, error FROM entity_tasks WHERE task_id=$1", tid8)
            # The root build wrote an app.yaml at the project root: the root
            # is an app of its own beside the modules, and a root build ships it.
            check("a root build in a project of modules ships the root app it built",
                  shipped_mods == [None], (shipped_mods, _row8 and dict(_row8)))
            _dt = t._delivery_targets
            check("...at plan end the root app ships AND every installable module with a version",
                  await _dt(pool, pid, None, None, True) == [(None, None), ("reader", "private_app")],
                  await _dt(pool, pid, None, None, True))
            check("...a module build ships its module",
                  await _dt(pool, pid, "reader", "private_app", False) == [("reader", "private_app")])
            _ay = pm.PROJECTS_ROOT / str(pid) / "app.yaml"
            _ay.rename(_ay.with_suffix(".off"))
            try:
                check("...with no root app, a root build ships the installable modules",
                      await _dt(pool, pid, None, None, False) == [("reader", "private_app")],
                      await _dt(pool, pid, None, None, False))
            finally:
                _ay.with_suffix(".off").rename(_ay)
            if tid8:
                await pool.execute("DELETE FROM entity_tasks WHERE task_id=$1", tid8)
        finally:
            rv._stub_harness_build = _stub_before
            t._PROJECTS_ROOT = _s_troot
            _av.ship_and_verify = _s_sv
            if _s_merge is not None:
                _pgit.merge_project_pr = _s_merge
            await pool.execute("UPDATE projects SET config = config - 'auto_merge' WHERE id=$1", pid)

        print("== a verified ship carries its module into the proof ==")
        from server.routes import applications as appmod
        _s_pack, _s_inst, _s_ser, _s_start = (appmod._pack_and_sign_project,
                                              appmod.installer.install_bundle,
                                              appmod._serialize, _av.start_verify)
        started: list = []

        async def _pack(pool_, project_id, module):
            return {"signed": b"x", "entity_id": eid, "open_phases": 0}

        class _Inst:
            id = "reader-app"

        async def _install(pool_, signed, **kw):
            return _Inst()

        async def _start(**kw):
            started.append(kw.get("origin"))
            return 1
        appmod._pack_and_sign_project, appmod.installer.install_bundle = _pack, _install
        appmod._serialize, _av.start_verify = (lambda i: {"id": i.id}), _start
        try:
            await appmod.ship_project_service(pool, pid, verify=True, module="reader")
            check("the proof's origin names the module (a repair rebuilds it)",
                  started and (started[0] or {}).get("module") == "reader", started)
        finally:
            (appmod._pack_and_sign_project, appmod.installer.install_bundle,
             appmod._serialize, _av.start_verify) = _s_pack, _s_inst, _s_ser, _s_start

        print("== the first adopt into a module can be undone ==")
        await pm.upsert_module(pool, pid, name="undo1", target="component", validation="review")
        u1 = pm.PROJECTS_ROOT / str(pid) / "undo1"
        (u1 / "orig.txt").write_text("original")           # files a push put there
        sdu = rv.SESSIONS_ROOT / "cmpu"
        (sdu / "work" / "codex").mkdir(parents=True)
        (sdu / "work" / "codex" / "orig.txt").write_text("adopted")
        (sdu / "work" / "codex" / "new.txt").write_text("new")
        (sdu / "manifest.json").write_text(json.dumps({
            "session_id": "cmpu", "project_id": pid, "worktree_root": str(sdu / "work"),
            "variants": [{"vid": "codex", "harness": "codex"}]}))
        # An OLDER root adopt, so a bare undo has a wrong answer to pick.
        sdr = rv.SESSIONS_ROOT / "cmpr"
        (sdr / "work" / "codex").mkdir(parents=True)
        (sdr / "work" / "codex" / "rootfile.txt").write_text("root adopt")
        (sdr / "manifest.json").write_text(json.dumps({
            "session_id": "cmpr", "project_id": pid, "worktree_root": str(sdr / "work"),
            "variants": [{"vid": "codex", "harness": "codex"}]}))
        adr = await rv.adopt_run_service(session_id="cmpr", harness="codex", project_id=pid)
        await asyncio.sleep(1.1)                            # a strictly newer `at`
        adu = await rv.adopt_run_service(session_id="cmpu", harness="codex",
                                         project_id=pid, into="undo1")
        check("the module adopt is its v1", (adu.get("version") or {}).get("number") == 1, adu)
        und = await t._exec_review_adopt_undo({"project_id": pid})
        check("a BARE undo undoes the newest adopt — the module's, not the older root one",
              und.get("status") == "ok" and (und.get("result") or {}).get("module") == "undo1"
              and (pm.PROJECTS_ROOT / str(pid) / "rootfile.txt").is_file(), und)
        check("...its overwritten file is back and its new file is gone",
              (u1 / "orig.txt").read_text() == "original" and not (u1 / "new.txt").exists())
        m_u1 = await pm.get_module(pool, pid, "undo1")
        check("...the module is back to no version, and v1's row stays",
              m_u1["current_version_id"] is None
              and [v["number"] for v in await pm.list_versions(pool, pid, "undo1")] == [1], m_u1)
        und2 = await t._exec_review_adopt_undo({"project_id": pid})
        check("...and the NEXT bare undo reaches the older root adopt",
              und2.get("status") == "ok" and not (pm.PROJECTS_ROOT / str(pid) / "rootfile.txt").exists(),
              und2)
        check("the adopt's undo hint names the module form that works",
              "module='undo1'" in (adu.get("undo") or ""), adu.get("undo"))

        print("== undoing a later module adopt marks it undone ==")
        adu2 = await rv.adopt_run_service(session_id="cmpu", harness="codex",
                                          project_id=pid, into="undo1")
        adu3 = await rv.adopt_run_service(session_id="cmp1", harness="codex-s2",
                                          project_id=pid, into="undo1")
        await t._exec_review_adopt_undo({"project_id": pid, "module": "undo1"})
        _bk3 = json.loads((u1 / ".adopt-backups" / adu3["backup_id"] / "manifest.json").read_text())
        check("the pointer moved back AND the adopt it moved off is recorded undone",
              bool(_bk3.get("undone_at")), _bk3)

        print("== versions skip derived trees; copies run outside the lock; old snapshots pruned ==")
        await pm.upsert_module(pool, pid, name="heavy", target="component", validation="review")
        hv = pm.PROJECTS_ROOT / str(pid) / "heavy"
        (hv / "node_modules" / "dep").mkdir(parents=True)
        (hv / "node_modules" / "dep" / "big.js").write_text("x" * 1000)
        (hv / ".venv").mkdir()
        (hv / ".venv" / "lib.py").write_text("x")
        (hv / "src.py").write_text("v1")
        hv1 = await pm.record_version(pool, pid, "heavy", source="build")
        snap = pm.version_dir(pid, "heavy", hv1["number"])
        check("a version holds the source, not node_modules/.venv",
              (snap / "src.py").is_file() and not (snap / "node_modules").exists()
              and not (snap / ".venv").exists(), sorted(p.name for p in snap.iterdir()))
        (hv / "src.py").write_text("v2")
        await pm.record_version(pool, pid, "heavy", source="build")
        await pm.undo_version(pool, pid, "heavy")
        check("a restore leaves the derived trees where they are",
              (hv / "node_modules" / "dep" / "big.js").is_file() and (hv / ".venv" / "lib.py").is_file()
              and (hv / "src.py").read_text() == "v1")
        hid = (await pm.get_module(pool, pid, "heavy"))["id"]
        async with pool.acquire() as holder:
            tr = holder.transaction()
            await tr.start()
            await holder.execute("SELECT 1 FROM project_modules WHERE id=$1 FOR UPDATE", hid)
            rec = asyncio.create_task(pm.record_version(pool, pid, "heavy", source="build"))
            await asyncio.sleep(0.8)
            staged = [d for d in (pm.VERSIONS_ROOT / str(pid) / "heavy").iterdir()
                      if d.name.startswith(".staging-")]
            check("the snapshot is copied BEFORE the row lock is taken",
                  not rec.done() and staged and (staged[0] / "src.py").is_file(), staged)
            await tr.rollback()
        await asyncio.wait_for(rec, 10)
        check("...and lands as the next version once the lock is free",
              rec.result()["number"] == 3 and not any(
                  d.name.startswith(".staging-") for d in (pm.VERSIONS_ROOT / str(pid) / "heavy").iterdir()))
        for i in range(pm.KEEP_SNAPSHOTS + 1):
            (hv / "src.py").write_text(f"n{i}")
            last = await pm.record_version(pool, pid, "heavy", source="build")
        hvs = {v["number"]: v for v in await pm.list_versions(pool, pid, "heavy")}
        newest = max(hvs)
        check("snapshots older than the newest KEEP_SNAPSHOTS are pruned, rows kept",
              len(hvs) == newest and not hvs[1]["restorable"]
              and all(hvs[n]["restorable"] for n in range(newest - pm.KEEP_SNAPSHOTS + 1, newest + 1)),
              {n: v["restorable"] for n, v in hvs.items()})
        # The pointer on the OLDEST kept version, then one more version row
        # that does not move it: that version now falls outside the newest
        # KEEP_SNAPSHOTS and is kept only because it is current.
        cur_n = newest - pm.KEEP_SNAPSHOTS + 1
        await pm.set_current_version(pool, pid, "heavy", cur_n)
        await pool.execute(
            "INSERT INTO module_versions (module_id, number, source, note) VALUES ($1, $2, 'build', 'x')",
            hid, newest + 1)
        pm.version_dir(pid, "heavy", newest + 1).mkdir(parents=True)
        pruned = await pm.prune_snapshots(pool, pid, "heavy")
        check("...never the current version's, even outside the newest KEEP_SNAPSHOTS",
              pm.version_dir(pid, "heavy", cur_n).is_dir() and cur_n not in pruned, pruned)
        try:
            await pm.set_current_version(pool, pid, "heavy", 1)
            check("restoring a pruned version is refused by name", False, "accepted")
        except pm.ModuleError as e:
            check("restoring a pruned version is refused by name", "pruned" in str(e), e)

        print("== the link check is repeated under the lock ==")
        await pm.upsert_module(pool, pid, name="race", target="component", validation="review")
        rc = pm.PROJECTS_ROOT / str(pid) / "race"
        (rc / "a.txt").write_text("1")
        await pm.record_version(pool, pid, "race", source="build")
        (rc / "a.txt").write_text("2")
        await pm.record_version(pool, pid, "race", source="build")
        rid = (await pm.get_module(pool, pid, "race"))["id"]
        victim = Path(td) / "victim"
        victim.mkdir()
        (victim / "precious.txt").write_text("keep")
        async with pool.acquire() as holder:
            tr = holder.transaction()
            await tr.start()
            await holder.execute("SELECT 1 FROM project_modules WHERE id=$1 FOR UPDATE", rid)
            mv = asyncio.create_task(pm.set_current_version(pool, pid, "race", 1))
            await asyncio.sleep(0.5)
            import shutil as _sh
            _sh.rmtree(rc)
            rc.symlink_to(victim, target_is_directory=True)     # a fast-forward lands a link
            await tr.rollback()
        try:
            await asyncio.wait_for(mv, 10)
            check("a module dir that became a link while waiting is refused", False, "accepted")
        except pm.ModuleError as e:
            check("a module dir that became a link while waiting is refused", "link" in str(e), e)
        check("...and the link's target is untouched", (victim / "precious.txt").read_text() == "keep"
              and len(list(victim.iterdir())) == 1)
        rc.unlink()
        rc.mkdir()
        ab = pm.PROJECTS_ROOT / str(pid) / ".adopt-backups"
        ab_real = Path(td) / "ab-real"
        _sh.move(str(ab), str(ab_real))
        other = Path(td) / "elsewhere"
        other.mkdir()
        ab.symlink_to(other, target_is_directory=True)            # a TRACKED symlink
        try:
            await pm.set_current_version(pool, pid, "race", 2)
            check("a restore refuses a project .adopt-backups that is a link", False, "accepted")
        except pm.ModuleError as e:
            check("a restore refuses a project .adopt-backups that is a link", "link" in str(e), e)
        check("...and moved nothing through it", not any(other.iterdir()))
        ab.unlink()
        _sh.move(str(ab_real), str(ab))

        print("== collisions, stored target, failed restore named ==")
        (pm.PROJECTS_ROOT / str(pid) / "docs").mkdir(exist_ok=True)
        (pm.PROJECTS_ROOT / str(pid) / "docs" / "readme.md").write_text("root app docs")
        try:
            await pm.upsert_module(pool, pid, name="docs", target="component", validation="review")
            check("a new module named like a root-app folder with files is refused", False, "accepted")
        except pm.ModuleError as e:
            check("a new module named like a root-app folder with files is refused",
                  "adopt_dir" in str(e) and await pm.get_module(pool, pid, "docs") is None, e)
        await pm.upsert_module(pool, pid, name="docs", target="component", validation="review",
                               adopt_dir=True)
        check("...and taken on explicitly with adopt_dir", await pm.get_module(pool, pid, "docs") is not None)
        await pm.upsert_module(pool, pid, name="docs", target="component", validation="checks")
        check("...an existing module is updated without it",
              (await pm.get_module(pool, pid, "docs"))["validation"] == "checks")

        # fan_out is stopped right after it resolves the module (the harness
        # settings read comes next), so no run is launched.
        class _Stop(Exception):
            pass
        _s_inst, _s_cap, _s_set = rv._installed_harnesses, rv._fanout_capacity, rv._coding_harness_settings

        async def _inst():
            return ["codex"]

        async def _cap(*a, **k):
            return None

        async def _stop():
            raise _Stop()
        rv._installed_harnesses, rv._fanout_capacity, rv._coding_harness_settings = _inst, _cap, _stop
        from server.routes import harnesses as _hmod
        _s_val = _hmod.harness_validity
        _hmod.harness_validity = lambda pid_: {"valid": True, "present": True}
        try:
            req = rv.FanOutReq(prompt="x", project_id=pid, module="site", target="private_app",
                               variants=[{"harness": "codex"}])
            try:
                await rv.fan_out(req)
            except _Stop:
                pass
            check("a module compare takes the MODULE's stored target, not the caller's",
                  req.target == "public_site", req.target)
        finally:
            rv._installed_harnesses, rv._fanout_capacity, rv._coding_harness_settings = _s_inst, _s_cap, _s_set
            _hmod.harness_validity = _s_val

        (rc / "b.txt").write_text("displaced")
        _s_copy = pm._copy_content

        def _boom(src_, dst_):
            raise OSError("disk full")
        pm._copy_content = _boom
        try:
            try:
                await pm.set_current_version(pool, pid, "race", 1)
                check("a restore that fails part-way names where the files went", False, "accepted")
            except pm.ModuleError as e:
                named = __import__("re").search(r"\.adopt-backups/(\S+?);", str(e))
                check("a restore that fails part-way names where the files went",
                      named and (pm.PROJECTS_ROOT / str(pid) / ".adopt-backups" / named.group(1)).is_dir()
                      and "pointer did not move" in str(e), e)
        finally:
            pm._copy_content = _s_copy

        print("== a module committed as an embedded repo stages its files ==")
        import subprocess as _sp3
        await pm.upsert_module(pool, pid, name="gl", target="component", validation="review")
        _gl = pm.PROJECTS_ROOT / str(pid) / "gl"
        _root = pm.PROJECTS_ROOT / str(pid)
        _g = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
        _sp3.run(["git", "init", "-q"], cwd=_gl, check=True)
        (_gl / "old.txt").write_text("old")
        _sp3.run(_g + ["add", "-A"], cwd=_gl, check=True)
        _sp3.run(_g + ["commit", "-q", "-m", "nested"], cwd=_gl, check=True)
        _sp3.run(_g + ["add", "gl"], cwd=_root, check=True, capture_output=True)
        _sp3.run(_g + ["commit", "-q", "-m", "embedded"], cwd=_root, check=True)
        _gll = _sp3.run(["git", "ls-files", "-s", "--", "gl"], cwd=_root,
                        capture_output=True, text=True).stdout
        check("(setup) the root repo holds gl as a gitlink", _gll.startswith("160000"), _gll)
        sdg = rv.SESSIONS_ROOT / "cmpg"
        (sdg / "work" / "codex").mkdir(parents=True)
        (sdg / "work" / "codex" / "new.html").write_text("<p>g</p>")
        (sdg / "manifest.json").write_text(json.dumps({
            "session_id": "cmpg", "project_id": pid, "module": "gl",
            "project_dir": str(_gl), "worktree_root": str(sdg / "work"),
            "variants": [{"vid": "codex", "harness": "codex"}]}))
        rg = await rv.propose_commit(rv.ProposeCommitReq(session_id="cmpg", picks={"new.html": "codex"}))
        _st = _sp3.run(["git", "ls-files", "-s", "--", "gl"], cwd=_root,
                       capture_output=True, text=True).stdout
        check("the gitlink is replaced by the module's real files in the index",
              "gl/new.html" in _st and "gl/old.txt" in _st and not _st.startswith("160000")
              and rg.get("nested_git_moved"), (_st, rg.get("failed")))

        print("== a prune waits for the module row lock ==")
        hid2 = (await pm.get_module(pool, pid, "heavy"))["id"]
        await pool.execute(
            "INSERT INTO module_versions (module_id, number, source, note) VALUES ($1, $2, 'build', 'x')",
            hid2, 900)
        pm.version_dir(pid, "heavy", 900).mkdir(parents=True)
        _victim = pm.version_dir(pid, "heavy", cur_n)       # held only by being current
        await pool.execute("UPDATE project_modules SET current_version_id=NULL WHERE id=$1", hid2)
        async with pool.acquire() as holder:
            tr = holder.transaction()
            await tr.start()
            # A restore holds the lock while it copies this snapshot.
            await holder.execute("SELECT 1 FROM project_modules WHERE id=$1 FOR UPDATE", hid2)
            pr = asyncio.create_task(pm.prune_snapshots(pool, pid, "heavy"))
            await asyncio.sleep(0.8)
            check("a prune does not run while a restore holds the module lock",
                  not pr.done() and _victim.is_dir())
            await tr.rollback()
        await asyncio.wait_for(pr, 10)
        check("...and runs once it is released", cur_n in pr.result(), pr.result())

        print("== the REST undo moves a module's pointer too ==")
        await pm.upsert_module(pool, pid, name="rest1", target="component", validation="review")
        a1 = await rv.adopt_run_service(session_id="cmpu", harness="codex", project_id=pid, into="rest1")
        await asyncio.sleep(1.1)
        a2 = await rv.adopt_run_service(session_id="cmp1", harness="codex-s2", project_id=pid, into="rest1")
        rr = await rv.adopt_undo(rv.AdoptUndoReq(project_id=pid))
        m_r1 = await pm.get_module(pool, pid, "rest1")
        _cn = await pool.fetchval("SELECT number FROM module_versions WHERE id=$1",
                                  m_r1["current_version_id"])
        check("a bare REST undo of a module adopt moves the pointer back (v2 → v1)",
              rr.get("module") == "rest1" and _cn == 1
              and not (pm.PROJECTS_ROOT / str(pid) / "rest1" / "index.html").exists(), (rr, _cn))
        try:
            await rv.adopt_undo(rv.AdoptUndoReq(project_id=pid, backup_id=a2["backup_id"]))
            check("a REST undo naming a module adopt that is no longer current is refused", False, "ok")
        except Exception as e:
            check("a REST undo naming a module adopt that is no longer current is refused",
                  getattr(e, "status_code", None) == 400, e)

        print("== the root app's bundle leaves the module folders out ==")
        from server.applications import bundles as _bund, build_contract as _bc
        from server.applications import sdk_store as _sdk
        (_root / "app.yaml").write_text(
            "id: gate-root\nname: Gate root\nversion: 0.1.0\nentrypoint: index.html\n"
            "provenance: custom\n")
        (_root / "index.html").write_text("<p>root</p>")
        _seen: dict = {}
        _s_pack, _s_vb, _s_ver = _bund.pack, _bc.validate_build, _sdk.verify_current

        class _Stop3(Exception):
            pass

        def _pack(d, exclude_top=frozenset()):
            _seen["payload"] = sorted(_bund._read_zip(_s_pack(d, exclude_top=exclude_top)).payload)
            raise _Stop3()

        def _vb(d, m, app=None, exclude_top=frozenset()):
            _seen["vb_exclude"] = set(exclude_top)
            return []
        _bund.pack, _bc.validate_build = _pack, _vb
        _sdk.verify_current = lambda: {"version": "0", "hash": "0"}
        _s_apr = appmod.PROJECTS_ROOT
        appmod.PROJECTS_ROOT = pm.PROJECTS_ROOT
        try:
            try:
                await appmod._pack_and_sign_project(pool, pid, None)
            except (_Stop3, appmod.ShipError):   # stopped right after the pack
                pass
        finally:
            _bund.pack, _bc.validate_build, _sdk.verify_current = _s_pack, _s_vb, _s_ver
            appmod.PROJECTS_ROOT = _s_apr
        _pl = _seen.get("payload") or []
        check("the root app's bundle holds the root's files and no module folder",
              "index.html" in _pl and not any(p.split("/")[0] in ("reader", "site", "api", "gl")
                                               for p in _pl), _pl)
        check("...and its contract scan skips the module folders too",
              {"reader", "site", "api"} <= _seen.get("vb_exclude", set()), _seen.get("vb_exclude"))

        print("== an undone adopt is never reinstated by a later undo ==")
        await pm.upsert_module(pool, pid, name="u4", target="component", validation="review")
        u4 = pm.PROJECTS_ROOT / str(pid) / "u4"
        (u4 / "orig.txt").write_text("pre")
        await rv.adopt_run_service(session_id="cmpu", harness="codex", project_id=pid, into="u4")
        await rv.undo_adopt(pool, project_id=pid, module="u4")
        (u4 / "build.txt").write_text("built")
        b2 = await pm.record_version(pool, pid, "u4", source="build")
        check("(setup) adopt, undo, then a build is v2", b2["number"] == 2, b2)
        r41 = await rv.undo_adopt(pool, project_id=pid, module="u4")
        m4 = await pm.get_module(pool, pid, "u4")
        check("undoing the build lands on the pre-adopt state, not the undone adopt",
              (u4 / "orig.txt").read_text() == "pre" and not (u4 / "new.txt").exists()
              and not (u4 / "build.txt").exists() and m4["current_version_id"] is None, r41)
        v4 = {v["number"]: v["undone"] for v in await pm.list_versions(pool, pid, "u4")}
        check("...and both versions are marked undone", v4 == {1: True, 2: True}, v4)
        await pm.set_current_version(pool, pid, "u4", 2)
        v4b = {v["number"]: v["undone"] for v in await pm.list_versions(pool, pid, "u4")}
        check("moving the pointer back onto a version by hand clears its undone mark",
              v4b == {1: True, 2: False}, v4b)

        print("== a bare undo after adopt + build undoes the build ==")
        await pm.upsert_module(pool, pid, name="u5", target="component", validation="review")
        u5 = pm.PROJECTS_ROOT / str(pid) / "u5"
        await asyncio.sleep(1.1)
        await rv.adopt_run_service(session_id="cmpu", harness="codex", project_id=pid, into="u5")
        (u5 / "build.txt").write_text("built")
        await pm.record_version(pool, pid, "u5", source="build")
        r5 = await rv.undo_adopt(pool, project_id=pid)
        m5 = await pm.get_module(pool, pid, "u5")
        n5 = await pool.fetchval("SELECT number FROM module_versions WHERE id=$1", m5["current_version_id"])
        check("the module's CURRENT change (the build) is undone; the adopt stands",
              r5.get("module") == "u5" and n5 == 1 and not (u5 / "build.txt").exists()
              and (u5 / "new.txt").is_file(), (r5, n5))

        print("== a root site publish leaves the module folders out ==")
        _seen2: dict = {}
        _s_lic, _s_pub, _s_apr2 = appmod._license_gate_site, appmod.publisher_op_service, appmod.PROJECTS_ROOT

        async def _lic(*a, **k):
            return None

        class _Stop4(Exception):
            pass

        async def _pub(op, args):
            import base64 as _b64m, io as _iom, zipfile as _zm
            _seen2["names"] = _zm.ZipFile(_iom.BytesIO(_b64m.b64decode(args["zip_b64"]))).namelist()
            _seen2["args"] = dict(args)
            raise _Stop4()
        appmod._license_gate_site, appmod.publisher_op_service = _lic, _pub
        appmod.PROJECTS_ROOT = pm.PROJECTS_ROOT
        try:
            try:
                await appmod.publish_site_service(pool, pid, slug="gate-root")
            except _Stop4:
                pass
        finally:
            appmod._license_gate_site, appmod.publisher_op_service, appmod.PROJECTS_ROOT = \
                _s_lic, _s_pub, _s_apr2
        _nm = _seen2.get("names") or []
        check("the root site's zip holds the root's files and no module folder",
              "index.html" in _nm and not any(n.split("/")[0] in ("reader", "site", "api", "u4")
                                              for n in _nm), _nm)
        # A published site is public. The login tier this used
        # to ask for was stored open to every logged-in account.
        check("a publish asks for no default exposure, so a new site is public",
              "default_exposure" not in (_seen2.get("args") or {}), sorted(_seen2.get("args") or {}))

        # The repo lane is the other caller of webapp.publish_bundle and asked
        # for the same login tier. The clone is faked; nothing dials gitea.
        import types as _ty4
        _seen2.clear()
        _s_git, _s_gp = appmod._entity_git, appmod.brain_service.get_pool
        _s_env = {k: os.environ.get(k) for k in ("GITEA_API_TOKEN", "GITEA_URL")}

        def _git(args, **k):
            (Path(args[-1]) / "index.html").write_text("<h1>repo</h1>")
            return _ty4.SimpleNamespace(returncode=0, stderr="")
        appmod._entity_git, appmod.brain_service.get_pool = _git, (lambda: pool)
        appmod._license_gate_site, appmod.publisher_op_service = _lic, _pub
        os.environ.update(GITEA_API_TOKEN="t", GITEA_URL="http://gitea.invalid")
        try:
            try:
                await appmod.publish_site_repo_service(repo="owner/site", slug="gate-repo")
            except _Stop4:
                pass
        finally:
            appmod._entity_git, appmod.brain_service.get_pool = _s_git, _s_gp
            appmod._license_gate_site, appmod.publisher_op_service = _s_lic, _s_pub
            for _k, _v in _s_env.items():
                if _v is None:
                    os.environ.pop(_k, None)
                else:
                    os.environ[_k] = _v
        check("a repo publish asks for no default exposure either",
              _seen2.get("names") == ["index.html"]
              and "default_exposure" not in (_seen2.get("args") or {}),
              (_seen2.get("names"), sorted(_seen2.get("args") or {})))

        print("== Phase 3: the Workbench's modules view — candidates, app state, phases by module ==")
        import json as _json
        from server.routes import projects as _P
        _sess = rv.SESSIONS_ROOT
        # p3cmp    undecided compare: codex ok, codex-s2 failed → codex is a candidate
        # p3adopt  "use as version" took claude-code by run id → the session is decided,
        #          kimi (its other variant) leaves the list too
        # p3pick   propose_commit took grok: a version with NO run id + the harness
        #          marked committed in harness_usage (the live signal) → decided
        # p3legacy an older session's picks.json → decided
        # p3root   built for the project root, not a module → never a module candidate
        for _sid, _mod, _legacy in (("p3cmp", "site", False), ("p3adopt", "site", False),
                                    ("p3pick", "site", False), ("p3legacy", "site", True),
                                    ("p3root", None, False)):
            (_sess / _sid).mkdir(parents=True, exist_ok=True)
            (_sess / _sid / "manifest.json").write_text(_json.dumps(
                {"session_id": _sid, "kind": "compare", "project_id": pid, "module": _mod}))
            if _legacy:
                (_sess / _sid / "picks.json").write_text("{}")
        _ok = await pool.fetchval(
            "INSERT INTO harness_run (session_id, project_id, harness, model, ok) "
            "VALUES ('p3cmp', $1, 'codex', 'gpt', true) RETURNING id", pid)
        await pool.execute(
            "INSERT INTO harness_run (session_id, project_id, harness, ok) VALUES "
            "('p3cmp', $1, 'codex-s2', false), ('p3adopt', $1, 'kimi', true), "
            "('p3pick', $1, 'grok', true), ('p3pick', $1, 'grok-s2', true), "
            "('p3legacy', $1, 'opencode', true), ('p3root', $1, 'cursor', true)", pid)
        _taken = await pool.fetchval(
            "INSERT INTO harness_run (session_id, project_id, harness, ok) "
            "VALUES ('p3adopt', $1, 'claude-code', true) RETURNING id", pid)
        _site_id = await pool.fetchval(
            "SELECT id FROM project_modules WHERE project_id=$1 AND name='site'", pid)
        _vn = await pool.fetchval(
            "SELECT COALESCE(MAX(number),0)+1 FROM module_versions WHERE module_id=$1", _site_id)
        await pool.execute(
            "INSERT INTO module_versions (module_id, number, run_id, source) VALUES "
            "($1, $2, $3, 'adopt'), ($1, $2 + 1, NULL, 'adopt')", _site_id, _vn, _taken)
        await pool.execute(
            "INSERT INTO harness_usage (session_id, project_id, harness, committed) "
            "VALUES ('p3pick', $1, 'grok', true), ('p3pick', $1, 'grok-s2', false)", pid)
        _view = await pm.modules_view(pool, pid)
        _mods = {m["name"]: m for m in _view["modules"]}
        check("a finished run built for a module and not yet a version is its candidate",
              [c["run_id"] for c in _mods["site"]["candidates"]] == [_ok]
              and _mods["site"]["candidates"][0]["harness"] == "codex"
              and _mods["site"]["candidates"][0]["session_id"] == "p3cmp", _mods["site"]["candidates"])
        _all = [c["harness"] for m in _mods.values() for c in m["candidates"]]
        check("...a failed run is not",  "codex-s2" not in _all, _all)
        check("...a compare the user took a run from by run id is decided — its OTHER variant leaves too",
              "claude-code" not in _all and "kimi" not in _all, _all)
        check("...a compare picked through propose_commit (version without run id, harness committed) is decided",
              "grok" not in _all and "grok-s2" not in _all, _all)
        check("...an older session's picks.json still counts, and a root run is never a module candidate",
              "opencode" not in _all and "cursor" not in _all, _all)
        check("an installable module carries its OBSERVED app state; a site or component carries none",
              isinstance(_mods["reader"]["app"], dict)
              and set(_mods["reader"]["app"]) >= {"app_id", "installed", "running"}
              and _mods["site"]["app"] is None and _mods["api"]["app"] is None, _mods["reader"]["app"])
        check("the view carries the root app's state (the app of a project with no modules)",
              isinstance(_view.get("root_app"), dict) and "installed" in _view["root_app"], _view.get("root_app"))
        _sp, _se = _P._pool, _P._eid
        _P._pool, _P._eid = (lambda: pool), (lambda: eid)
        try:
            _ph = (await _P.list_phases(pid))["phases"]
        finally:
            _P._pool, _P._eid = _sp, _se
        _by = {p["title"]: p for p in _ph}
        check("phases carry their module and how they are proven (the plan pane groups by it)",
              _by.get("Reader UI", {}).get("module") == "reader"
              and _by.get("Reader UI", {}).get("validation") == "checks"
              and _by.get("Landing", {}).get("module") == "site"
              and _by.get("Landing", {}).get("validation") == "review", _ph)
    finally:
        (pm.PROJECTS_ROOT, pm.VERSIONS_ROOT, rv.PROJECTS_ROOT, rv.SESSIONS_ROOT,
         pg.sync_project_repo, brain_service._pool, fe, sm,
         rv.resolve_build_target, rv._coding_harness_settings) = saved
        for k, v in (("FACTORY_ENGINE", fe), ("CSERIES_SMOKE", sm)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        await pool.execute("DELETE FROM harness_run WHERE project_id=$1", pid)
        await pool.execute("DELETE FROM harness_usage WHERE project_id=$1", pid)
        await pool.execute("DELETE FROM projects WHERE id=$1", pid)
        await pool.close()
        import shutil
        shutil.rmtree(td, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("OK: modules + versions gates pass")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
