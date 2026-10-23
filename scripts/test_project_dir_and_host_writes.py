#!/usr/bin/env python3
"""Gate: a project has a directory from birth, and the host may write into it.

A host could not write to its own project.

Two separate faults, one visible symptom. Measured on one Host's project:
created, its gitea repo created the same second, three phases planned, four
fan-out runs that produced real files — and `review.adopt failed: 404: no
project <id>`.

  1. NOTHING created the project's directory. Only the single-harness build
     lane did (review.run_single_harness_build), so a project planned but never
     built had none. fan_out then fell back to a session-named dir, adopt
     refused with "no project <id>", and project_git returned "project
     workspace is empty" — three lanes reading one absence as their own
     failure. Cases 1-4.

  2. The host could not write into a project at all: workspace_file refused
     write/delete whenever project_id was set. That rule holds for build OUTPUT
     and for nothing else, so the decisions, research and briefs a project is
     made of had nowhere to live. Cases 5-9 — including the containment that
     has to survive giving write a base, which is the part worth a gate.

Run:  python3 scripts/test_project_dir_and_host_writes.py
"""
import ast
import os
import sys
import tempfile
from pathlib import Path
from tools_source import tools_function, tools_source  # tools.py and the modules split out of it

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))
sys.path.insert(1, str(_ROOT / "brain"))

from server.capabilities import file_access  # noqa: E402
from server.routes import projects as projects_mod  # noqa: E402

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def src(rel: str) -> str:
    return (_ROOT / rel).read_text()


def fn_src(rel: str, name: str) -> str:
    """The source of one top-level function/coroutine, by AST — so a match
    can't come from a neighbouring function that happens to share a token."""
    tree = ast.parse(src(rel))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src(rel), node) or ""
    return ""


print("\n1. ensure_project_dir creates the directory and is idempotent")
with tempfile.TemporaryDirectory() as td:
    projects_mod.PROJECT_FILES_ROOT = Path(td)
    p1 = projects_mod.ensure_project_dir(9213)
    check("creates /<root>/9213", p1.is_dir(), str(p1))
    (p1 / "keep.txt").write_text("x")
    p2 = projects_mod.ensure_project_dir(9213)
    check("second call returns the same dir", p1 == p2, f"{p1} != {p2}")
    check("second call does not clear it", (p1 / "keep.txt").is_file())
    # A project id arrives from a model on the tool path. It must never be
    # able to name a directory outside the root.
    escaped = False
    try:
        projects_mod.ensure_project_dir("../../etc/evil")     # type: ignore[arg-type]
    except (ValueError, TypeError):
        escaped = True
    check("a non-integer project id is refused, not interpolated", escaped)

print("\n2. both project-creation doors create the directory")
create_project = fn_src("server/routes/projects.py", "create_project")
check("route: create_project calls ensure_project_dir",
      "ensure_project_dir(" in create_project)
tools_src = tools_source()
# By AST, as fn_src does: the text up to the list_projects section header was
# this function only while both sat in tools.py.
exec_create = tools_function("_exec_create_project")
check("host tool: _exec_create_project calls ensure_project_dir",
      "ensure_project_dir(" in exec_create)
check("the directory is not gated on gitea being connected",
      exec_create.index("ensure_project_dir(") < exec_create.index("ensure_project_repo("),
      "repo creation can fail soft; the directory must still exist")

print("\n3. adopt tests the project ROW, not the directory")
adopt = fn_src("server/routes/review.py", "adopt_run_service")
check("adopt still refuses an id with no project row",
      'raise HTTPException(404, f"no project {project_id}")' in adopt)
check("adopt creates the dir for a project that IS real",
      "ensure_project_dir(project_id)" in adopt)
_guard = adopt.index("SELECT 1 FROM projects WHERE id = $1")
check("the row check comes BEFORE the directory is created",
      _guard < adopt.index("ensure_project_dir(project_id)"),
      "otherwise a wrong id conjures a directory, the exact bug the old guard prevented")

print("\n4. an adopted run reaches the repo, not just the disk")
check("adopt calls sync_project_repo", "sync_project_repo(" in adopt)
# Staged by the adopted paths — prefixed with the module directory when the
# adopt is INTO a module (test_modules_versions.py runs it and checks paths == ["site/index.html"]).
check("adopt stages only the files it adopted",
      "paths=_proj_rels" in adopt and "_proj_rels = [_mod_prefix + r for r in rels]" in adopt)
check("adopt reports the sync instead of swallowing it",
      '"repo_sync": repo_sync' in adopt)
check("a gitea failure does not fail the adopt",
      '{"synced": False, "error"' in adopt,
      "the files are in the project either way")

print("\n5. the sync can stage named paths instead of the whole tree")
sync_blocking = fn_src("server/routes/project_git.py", "_sync_blocking")
check("_sync_blocking takes paths", "paths: list | None = None" in sync_blocking)
check("paths given -> staged by name",
      '"add", "-A", "--", *paths' in sync_blocking)
check("paths omitted -> add -A, as before",
      'else _run(base, env, "add", "-A")' in sync_blocking)
sync_repo = fn_src("server/routes/project_git.py", "sync_project_repo")
check("sync_project_repo passes paths through",
      "gate_violations or [], changed_files or [], paths" in sync_repo)

print("\n6. workspace_file no longer refuses a project write")
# By AST: the text up to the next def was this one function only while both sat in tools.py.
wsf = tools_function("_exec_workspace_file")
check("the read-only refusal is gone",
      "project files are read-only here" not in wsf)
check("write passes the project base", "base=base)" in wsf)
check("delete passes the project base", "file_access.delete_file(path, base=base)" in wsf)
check("ownership is still checked",
      "SELECT 1 FROM projects WHERE id=$1 AND entity_id=$2" in wsf)
check("ownership is checked BEFORE the dir is created",
      wsf.index("AND entity_id=$2") < wsf.index("ensure_project_dir(project_id)"))

print("\n7. a project write commits")
check("write/delete sync to the repo", "sync_project_repo(" in wsf)
check("only the written path is staged", "paths=[path]" in wsf)
check("workspace writes do NOT sync (no project, no repo)",
      "if base is not None and op in (\"write\", \"delete\")" in wsf)
check("a sync failure does not fail the write",
      '"repo_sync"] = {"synced": False' in wsf,
      "the bytes are on disk either way")

print("\n8. file_access honours a base on every op")
with tempfile.TemporaryDirectory() as td:
    base = Path(td) / "9213"
    base.mkdir()
    r = file_access.write_file("notes/decisions.md", "we picked kimi's run", base=base)
    check("write lands under the base", r.get("status") == "ok", str(r))
    check("the file is really there", (base / "notes" / "decisions.md").is_file())
    rr = file_access.read_file("notes/decisions.md", base=base)
    check("read sees what write wrote",
          rr.get("content") == "we picked kimi's run", str(rr)[:120])
    dr = file_access.delete_file("notes/decisions.md", base=base)
    check("delete removes it under the base", dr.get("status") == "ok", str(dr))
    check("and it is gone", not (base / "notes" / "decisions.md").exists())

print("\n9. containment survives giving write a base")
# The whole reason write was workspace-only. `path` is model-supplied on this
# lane, so every escape shape has to still be refused now that a base exists.
with tempfile.TemporaryDirectory() as td:
    base = Path(td) / "9213"
    base.mkdir()
    outside = Path(td) / "secret.txt"
    outside.write_text("do not overwrite me")
    for bad in ("../secret.txt", "../../etc/passwd", "/etc/passwd",
                "notes/../../secret.txt", "~/secret.txt"):
        r = file_access.write_file(bad, "pwned", base=base)
        check(f"write refused: {bad!r}", r.get("status") == "error", str(r))
        d = file_access.delete_file(bad, base=base)
        check(f"delete refused: {bad!r}", d.get("status") == "error", str(d))
    check("the file outside the base is untouched",
          outside.read_text() == "do not overwrite me")
    r = file_access.write_file("has\x00null", "x", base=base)
    check("null byte in path refused", r.get("status") == "error", str(r))

print("\n10. containment survives a SYMLINK, which is the regression that matters")
# Section 9's string cases stay green under a lexical normpath, which collapses
# "../" without following links — exactly the change that would reopen this
# hole. These cases pass only because _validate_path RESOLVES before comparing,
# so they are the ones actually pinning the guard.
with tempfile.TemporaryDirectory() as td:
    base = Path(td) / "9213"
    (base / "notes").mkdir(parents=True)
    outside_dir = Path(td) / "outside"
    outside_dir.mkdir()
    secret = outside_dir / "secret.txt"
    secret.write_text("do not overwrite me")

    (base / "escape").symlink_to(outside_dir)          # dir symlink pointing out
    (base / "notes" / "leak").symlink_to(secret)       # file symlink pointing out

    r = file_access.write_file("escape/pwned.txt", "x", base=base)
    check("write THROUGH a dir symlink out of the base is refused",
          r.get("status") == "error", str(r))
    check("...and nothing was created outside",
          not (outside_dir / "pwned.txt").exists())

    r = file_access.write_file("notes/leak", "pwned", base=base)
    check("write ONTO a file symlink pointing out is refused",
          r.get("status") == "error", str(r))
    check("...and the target still holds its own bytes",
          secret.read_text() == "do not overwrite me")

    r = file_access.read_file("notes/leak", base=base)
    check("read through a symlink out is refused", r.get("status") == "error", str(r))
    d = file_access.delete_file("escape/secret.txt", base=base)
    check("delete through a dir symlink out is refused", d.get("status") == "error", str(d))
    check("...and the file it aimed at survives", secret.is_file())

    # A base that is ITSELF a symlink is not an escape — it resolves to the real
    # directory and writes land inside it. Pinned so a later guard cannot "fix"
    # containment by refusing a legitimate setup.
    linked_base = Path(td) / "9213-link"
    linked_base.symlink_to(base)
    r = file_access.write_file("ok.txt", "fine", base=linked_base)
    check("a base that is a symlink still works", r.get("status") == "ok", str(r))
    check("...writing into the real directory", (base / "ok.txt").read_text() == "fine")

print("\n11. the whole tool boundary, driven end to end")
# Sections 2-7 read the source; this RUNS it. Ownership gate -> ensure_project_dir
# -> file_access with the project base -> commit, through _exec_workspace_file
# itself, so a break anywhere in that chain fails here even when every substring
# those sections look for is still present.
import asyncio  # noqa: E402
from server import brain_service  # noqa: E402
from server.routes import project_git as pg_mod  # noqa: E402
from mastercode.runtime import tools as tools_mod  # noqa: E402


class _FakePool:
    """get_pool() returns brain_service._pool verbatim (brain_service.py:25,149),
    so setting that global is the whole stub."""

    def __init__(self, owned=True, kind="app"):
        self._owned = owned
        self._kind = kind

    async def fetchval(self, sql="", *a, **k):
        if "SELECT kind" in sql:
            return self._kind if self._owned else None
        return 1 if self._owned else None


SYNCED: list = []


async def _fake_sync(pool, project_id, *, phase_label, paths=None, **kw):
    SYNCED.append({"project_id": project_id, "label": phase_label, "paths": paths})
    return {"synced": True, "commit": "deadbee", "paths": paths}


with tempfile.TemporaryDirectory() as td:
    projects_mod.PROJECT_FILES_ROOT = Path(td)
    pg_mod.sync_project_repo = _fake_sync
    brain_service._pool = _FakePool(owned=True)

    async def _drive():
        w = await tools_mod._exec_workspace_file(
            {"op": "write", "project_id": 9213, "path": "notes/decisions.md",
             "content": "we adopted kimi's run"})
        check("write into a project the host owns succeeds",
              w.get("status") == "ok", str(w)[:200])
        landed = Path(td) / "9213" / "notes" / "decisions.md"
        check("the bytes are in the PROJECT dir, not the workspace",
              landed.is_file() and landed.read_text() == "we adopted kimi's run",
              str(landed))
        check("the directory was created by the call itself", (Path(td) / "9213").is_dir())
        check("the write was committed", len(SYNCED) == 1, str(SYNCED))
        check("staged to just that path",
              bool(SYNCED) and SYNCED[0]["paths"] == ["notes/decisions.md"], str(SYNCED))
        check("the sync result rides out to the host",
              ((w.get("result") or {}).get("repo_sync") or {}).get("synced") is True,
              str(w)[:200])

        # The escape shapes again, but through the REAL entry point this time.
        for bad in ("../../etc/passwd", "/etc/passwd", "../outside.txt"):
            r = await tools_mod._exec_workspace_file(
                {"op": "write", "project_id": 9213, "path": bad, "content": "pwned"})
            check(f"tool refuses escape {bad!r}", r.get("status") == "error", str(r)[:120])
        check("no escape produced a commit", len(SYNCED) == 1, str(SYNCED))

        # Review machinery: refused with a reason, not a git error out of a
        # fail-soft sync the host has to decode.
        r = await tools_mod._exec_workspace_file(
            {"op": "write", "project_id": 9213, "path": "_merge/notes.md", "content": "x"})
        check("a write into review machinery is refused up front",
              r.get("status") == "error" and "review machinery" in str(r.get("result")),
              str(r)[:200])
        check("...and did not land on disk", not (Path(td) / "9213" / "_merge").exists())

        # An ARTIFACT's .history/ is its revision store and trial mark: the
        # artifact ops refuse it as a path, and so does this lane — a write
        # there could rewrite what "revert" restores or mark any artifact a
        # discardable trial (review of the desktop render check, 2026-10-01).
        brain_service._pool = _FakePool(owned=True, kind="artifact")
        for op, extra in (("write", {"content": "forged"}), ("delete", {})):
            r = await tools_mod._exec_workspace_file(
                {"op": op, "project_id": 214, "path": ".history/design-trial", **extra})
            check(f"an artifact's .history/ is refused to {op}",
                  r.get("status") == "error" and "revision store" in str(r.get("result")),
                  str(r)[:200])
        check("...and nothing landed there", not (Path(td) / "214" / ".history").exists())
        brain_service._pool = _FakePool(owned=True, kind="app")
        r = await tools_mod._exec_workspace_file(
            {"op": "write", "project_id": 215, "path": ".history/notes.md", "content": "x"})
        check("...while in an ordinary project .history/ is just a folder",
              r.get("status") == "ok", str(r)[:200])

        # A project the host does NOT own: refused, and no directory conjured.
        brain_service._pool = _FakePool(owned=False)
        r = await tools_mod._exec_workspace_file(
            {"op": "write", "project_id": 999, "path": "x.md", "content": "x"})
        check("a project the host does not own is refused",
              r.get("status") == "error" and "not found" in str(r.get("result")), str(r)[:120])
        check("...and NO directory was conjured for it", not (Path(td) / "999").exists(),
              "ownership must be checked before the directory is created")

    asyncio.run(_drive())

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): " + "; ".join(FAILS))
    sys.exit(1)
print("all checks passed")
