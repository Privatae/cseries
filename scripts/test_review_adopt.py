#!/usr/bin/env python3
"""Gates on `review.adopt` — using a run's files as they are.

WHY THIS EXISTS
---------------
A user builds a dashboard with their Host, compares several runs, and likes
what one run produced: "I like that run's dashboard better, use that."

There was no operation under that sentence. `pick` records a preference and
copies nothing. The only path that wrote a run's output into a project was
`merge_session_service`, which had a HARNESS REBUILD the result from a spec — a
model in the loop — so what landed was a rewrite of the thing they chose, every
time. The manual workaround (hand-porting files) destroyed a working app.
That path is gone; adopt is the only one, so these gates are
the whole contract for getting a run's files into a project.

adopt is the missing verb. These gates pin the three properties that make it
safe to say yes to, each one earned by a specific way the manual version failed:

  gate1  it copies BYTES. Not a rebuild, not a diff, not a model — the adopted
         file is byte-identical to the run's file. This is the whole feature;
         everything else is safety around it.
  gate2  it is ADDITIVE. A file the run does not contain is untouched. The
         working FastAPI backend lived in server/ and the run had no server/.
  gate3  it is REVERSIBLE, in BOTH directions: undo restores what was
         overwritten AND removes what was created. Restoring contents alone
         leaves the new files behind, in a state that never existed.
  gate4  `paths` narrows it — "port over the DESIGN, that's it" is a real and
         common request and must not be all-or-nothing.
  gate5  dry_run writes NOTHING.
  gate6  it refuses to write outside the project. The run directory is model
         output; `..` in a path must not become a write into another project.
  gate7  provenance is recorded and ACCUMULATES across adopts.
  gate8  the host tool is wired: named in caps, reachable in dispatch, and the
         review SOP points "use run N" at it rather than at compare/propose.
"""
import asyncio
import json
import os
import pathlib
import shutil
import sys
import tempfile
from tools_source import tools_source  # tools.py and the modules split out of it

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "brain"))

fails = []


def ck(name, cond, extra=""):
    if cond:
        print(f"OK    {name}")
    else:
        fails.append(name)
        print(f"FAIL  {name} {extra}")


WORK = pathlib.Path(tempfile.mkdtemp())
SESSIONS = WORK / "review-sessions"
PROJECTS = WORK / "projects"
SESSION = "5e55f00d1a2b4c3d9e8f7a6b5c4d3e2f"
RUN_SRC = SESSIONS / SESSION / "work" / "codex"
PROJ = PROJECTS / "9198"

# The shape that actually bit: a run that produced a front end and no backend,
# adopted into a project whose backend is the part that works.
(RUN_SRC / "assets").mkdir(parents=True)
(RUN_SRC / "index.html").write_text("<title>the design he picked</title>")
(RUN_SRC / "assets" / "styles.css").write_text("/* 47KB of design */")
(RUN_SRC / "assets" / "app.js").write_text("// the run's render layer")
# Sessions written from 2026-08-22 carry worktree_root; older ones do not and
# resolve to project_dir/<harness>. Both shapes must adopt — review found the
# hardcoded work/<harness> path made 628 recorded runs 404 as if the build were
# broken.
(SESSIONS / SESSION / "manifest.json").write_text(json.dumps(
    {"project_id": 9198, "worktree_root": str(SESSIONS / SESSION / "work")}))

# ...and a LEGACY session: no worktree_root, worktrees beside the project.
LEGACY = "legacy0000000000000000000000000000"
(SESSIONS / LEGACY).mkdir(parents=True)
(PROJECTS / "9198" / "codex").mkdir(parents=True, exist_ok=True)
(PROJECTS / "9198" / "codex" / "legacy.txt").write_text("from an older run")
(SESSIONS / LEGACY / "manifest.json").write_text(json.dumps(
    {"project_id": 9198, "project_dir": str(PROJECTS / "9198")}))

(PROJ / "assets").mkdir(parents=True)
(PROJ / "server").mkdir(parents=True)
(PROJ / "index.html").write_text("<title>his working app</title>")
(PROJ / "assets" / "styles.css").write_text("/* his css */")
(PROJ / "assets" / "data.js").write_text("// the working fetch layer")
(PROJ / "server" / "main.py").write_text("# the FastAPI backend that works")

from fastapi import HTTPException                                   # noqa: E402
import server.routes.review as R                                    # noqa: E402

R.SESSIONS_ROOT = SESSIONS
R.PROJECTS_ROOT = PROJECTS
run = asyncio.run


def run_expect_error(coro):
    """Run a coroutine, returning the exception instead of raising.

    The hostile-input gates below assert on WHAT HAPPENED ON DISK as much as on
    the exception, so they need the call to complete either way.
    """
    try:
        return run(coro)
    except Exception as e:                                              # noqa: BLE001
        return e


def adopt(_expect_error=False, **kw):
    coro = R.adopt_run_service(session_id=SESSION, harness="codex", **kw)
    return run_expect_error(coro) if _expect_error else run(coro)


# ── gate5 first: dry_run must not have written anything ─────────────
before = {p: p.read_bytes() for p in PROJ.rglob("*") if p.is_file()}
d = adopt(dry_run=True)
after = {p: p.read_bytes() for p in PROJ.rglob("*") if p.is_file()}
ck("gate5 dry_run changes nothing on disk", before == after,
   f"{set(after) ^ set(before)}")
ck("gate5 dry_run still reports the real plan",
   set(d["would_overwrite"]) == {"index.html", "assets/styles.css"}
   and d["would_create"] == ["assets/app.js"], d)

# ── gate1/gate2: the copy itself ────────────────────────────────────
res = adopt()
ck("gate1 the adopted file is BYTE-IDENTICAL to the run's",
   (PROJ / "index.html").read_bytes() == (RUN_SRC / "index.html").read_bytes(),
   (PROJ / "index.html").read_text())
ck("gate1 every file the run produced arrived",
   set(res["adopted"]) == {"index.html", "assets/styles.css", "assets/app.js"},
   res["adopted"])
ck("gate2 ADDITIVE: the backend the run never had is untouched",
   (PROJ / "server" / "main.py").read_text() == "# the FastAPI backend that works",
   "server/main.py was modified or removed")
ck("gate2 ADDITIVE: a sibling asset the run never had is untouched",
   (PROJ / "assets" / "data.js").read_text() == "// the working fetch layer",
   "assets/data.js was modified or removed")

# ── gate3: undo, in both directions ─────────────────────────────────
u = run(R.adopt_undo_service(project_id=9198, backup_id=res["backup_id"]))
ck("gate3 undo restores an OVERWRITTEN file exactly",
   (PROJ / "index.html").read_text() == "<title>his working app</title>",
   (PROJ / "index.html").read_text())
ck("gate3 undo REMOVES a file the adopt created",
   not (PROJ / "assets" / "app.js").exists(),
   "assets/app.js survived the undo — the project is in a state it was "
   "never in, which is the failure mode a contents-only restore has")
ck("gate3 undo leaves untouched files alone",
   (PROJ / "server" / "main.py").exists() and (PROJ / "assets" / "data.js").exists())
ck("gate3 undo reports what it did", set(u["restored"]) == {"index.html", "assets/styles.css"}
   and u["removed"] == ["assets/app.js"], u)

# ── gate4: "port over the DESIGN. the DESIGN. that's it." ───────────
res2 = adopt(paths=["assets/styles.css"])
ck("gate4 paths= adopts ONLY what was named",
   res2["adopted"] == ["assets/styles.css"], res2["adopted"])
ck("gate4 ...and the markup it did not name is left alone",
   (PROJ / "index.html").read_text() == "<title>his working app</title>",
   (PROJ / "index.html").read_text())
ck("gate4 ...and the design file did land",
   (PROJ / "assets" / "styles.css").read_text() == "/* 47KB of design */")
run(R.adopt_undo_service(project_id=9198, backup_id=res2["backup_id"]))
# a named DIRECTORY carries what is under it
res3 = adopt(paths=["assets"])
ck("gate4 a named directory carries its contents",
   set(res3["adopted"]) == {"assets/styles.css", "assets/app.js"}, res3["adopted"])
run(R.adopt_undo_service(project_id=9198, backup_id=res3["backup_id"]))

# ── gate6: the run directory is MODEL OUTPUT ────────────────────────
# The previous cut of this gate called R._adopt_dest("../199/pwned.txt")
# directly — an input adopt can never produce, since rel comes from
# p.relative_to(src). It unit-tested the helper and asserted "nothing was
# written next door" after attempting no write. An implementation that used
# `proj_root / rel` and never called the guard passed it. These go through
# adopt() and assert on the filesystem.
OUTSIDE = WORK / "outside"
OUTSIDE.mkdir(exist_ok=True)
(OUTSIDE / "secret").write_text("SSH PRIVATE KEY MATERIAL")

# (a) a SYMLINK in the run dir must not be followed into the project
(RUN_SRC / "notes.txt").symlink_to(OUTSIDE / "secret")
res6 = adopt(paths=["notes.txt"], _expect_error=True)
ck("gate6 a symlink in the run dir is not adopted",
   isinstance(res6, Exception), f"adopted {getattr(res6, 'get', lambda k: None)('adopted')}")
ck("gate6 ...and its target's contents did NOT land in the project",
   not (PROJ / "notes.txt").exists(),
   (PROJ / "notes.txt").read_text() if (PROJ / "notes.txt").exists() else "")
(RUN_SRC / "notes.txt").unlink()

# (b) a traversing `harness` must not read an arbitrary directory.
# The ../ depth is COMPUTED, not guessed: an escape aimed at a path that does
# not exist is refused by the is_dir() check instead of by the guard, so the
# gate would pass with the guard deleted. It must land on a real directory
# holding a real secret.
_wt_root = R._session_worktree_root(
    json.loads((SESSIONS / SESSION / "manifest.json").read_text())).resolve()
_escape = os.path.relpath(OUTSIDE, _wt_root)          # e.g. "../../../outside"
assert (_wt_root / _escape).resolve() == OUTSIDE, (_wt_root, _escape)
assert (OUTSIDE / "secret").is_file()
res6b = run_expect_error(R.adopt_run_service(
    session_id=SESSION, harness=_escape, project_id=9198))
ck("gate6 a traversing harness id is refused (target dir really exists)",
   isinstance(res6b, Exception), repr(res6b))
ck("gate6 ...and the file it aimed at did NOT land in the project",
   not (PROJ / "secret").exists(),
   (PROJ / "secret").read_text() if (PROJ / "secret").exists() else "")

# (c) a traversing session_id likewise — also aimed at a real tree
(OUTSIDE / "work" / "codex").mkdir(parents=True, exist_ok=True)
(OUTSIDE / "work" / "codex" / "stolen.txt").write_text("SSH PRIVATE KEY MATERIAL")
_sid_escape = os.path.relpath(OUTSIDE, SESSIONS.resolve())
res6c = run_expect_error(R.adopt_run_service(
    session_id=_sid_escape, harness="codex", project_id=9198))
ck("gate6 a traversing session_id is refused (target dir really exists)",
   isinstance(res6c, Exception), repr(res6c))
ck("gate6 ...and nothing from it landed",
   not (PROJ / "stolen.txt").exists())

# (d) an unknown harness is refused BY NAME, before any path work. Asserting
# only "an exception" cannot see the allowlist go: a bogus id also fails the
# is_dir() check two lines later, with a message that reads as a broken build
# rather than a bad argument.
res6d = run_expect_error(R.adopt_run_service(
    session_id=SESSION, harness="definitely-not-a-harness", project_id=9198))
ck("gate6 an unknown harness id is refused as UNKNOWN, not as 'no output'",
   isinstance(res6d, Exception)
   and "unknown harness" in str(getattr(res6d, "detail", res6d)),
   repr(getattr(res6d, "detail", res6d)))

# (d2) a LEGAL harness id whose worktree is a symlink out of the session. The
# allowlist passes it by construction, so this is the one shape only the
# containment check catches — without it the allowlist looks sufficient and is
# not.
_sneak = SESSIONS / SESSION / "work" / "cursor"
_sneak.symlink_to(OUTSIDE)
res6d2 = run_expect_error(R.adopt_run_service(
    session_id=SESSION, harness="cursor", project_id=9198))
ck("gate6 a legal harness id symlinked out of the session is refused",
   isinstance(res6d2, Exception), repr(res6d2))
ck("gate6 ...and its contents did not land",
   not (PROJ / "secret").exists())
_sneak.unlink()

# (e) the destination guard still holds, via a symlinked dir INSIDE the project
(PROJ / "vendor").symlink_to(OUTSIDE)
(RUN_SRC / "vendor").mkdir(exist_ok=True)
(RUN_SRC / "vendor" / "victim.txt").write_text("should not land outside")
res6e = run_expect_error(R.adopt_run_service(
    session_id=SESSION, harness="codex", project_id=9198, paths=["vendor"]))
ck("gate6 a symlinked dir inside the project cannot be written through",
   isinstance(res6e, Exception), repr(res6e))
ck("gate6 ...and the outside dir is untouched",
   not (OUTSIDE / "victim.txt").exists())
(PROJ / "vendor").unlink()
shutil.rmtree(RUN_SRC / "vendor")

# ── gate7: provenance accumulates ───────────────────────────────────
a1 = adopt(paths=["index.html"])
a2 = adopt(paths=["assets/app.js"])
prov = json.loads((PROJ / ".adopt-provenance.json").read_text())
ck("gate7 provenance records BOTH adopts, not just the last",
   len(prov) >= 2 and prov[-1]["backup_id"] == a2["backup_id"], prov)
ck("gate7 provenance names the run it came from",
   all(r.get("session_id") == SESSION and r.get("harness") == "codex" for r in prov),
   prov)
run(R.adopt_undo_service(project_id=9198, backup_id=a2["backup_id"]))
run(R.adopt_undo_service(project_id=9198, backup_id=a1["backup_id"]))

# undo with no backup_id takes the most recent
a3 = adopt(paths=["index.html"])
u3 = run(R.adopt_undo_service(project_id=9198))
ck("gate7 undo defaults to the most recent adopt",
   u3["backup_id"] == a3["backup_id"], f'{u3["backup_id"]} vs {a3["backup_id"]}')
# ...and does not select the same one twice. Without the undone marker, a bare
# second undo re-picks the backup it just consumed and reports another success
# for work it did not do.
u3b = run_expect_error(R.adopt_undo_service(project_id=9198))
ck("gate7 a second bare undo does not re-select the backup just undone",
   isinstance(u3b, Exception) or u3b["backup_id"] != u3["backup_id"],
   f'{u3b} re-picked {u3["backup_id"]}')

# the project is back where it started, after everything above
_now = {str(p.relative_to(PROJ)): p.read_bytes() for p in PROJ.rglob("*")
        if p.is_file() and ".adopt-" not in str(p)}
_then = {str(p.relative_to(PROJ)): v for p, v in before.items()}
_diff = {k: (_then.get(k), _now.get(k)) for k in set(_then) | set(_now)
         if _then.get(k) != _now.get(k)}
ck("gate3 after all adopts and undos the project is byte-for-byte original",
   not _diff, _diff)

# ── gate9: two adopts in the SAME SECOND must not share a backup ────
# Found by gate3 rather than reasoned about. The id was
# f"{int(time.time())}-run{n}", so back-to-back adopts collided: the second
# manifest replaced the first, and undoing the first restored NOTHING while
# reporting ok. An undo that lies is worse than no undo.
b1 = adopt(paths=["index.html"])
b2 = adopt(paths=["assets/app.js"])
ck("gate9 back-to-back adopts get distinct backups",
   b1["backup_id"] != b2["backup_id"], f'{b1["backup_id"]} == {b2["backup_id"]}')
run(R.adopt_undo_service(project_id=9198, backup_id=b2["backup_id"]))
run(R.adopt_undo_service(project_id=9198, backup_id=b1["backup_id"]))
ck("gate9 ...so undoing the FIRST of them actually restores it",
   (PROJ / "index.html").read_text() == "<title>his working app</title>",
   (PROJ / "index.html").read_text())

# ── gate10: undo must never destroy work it did not create ─────────
# Review executed this: adopt creates new.js -> the user rewrites it with their
# own work -> undo unlinks it and reports {'ok': True, 'removed': ['new.js']}.
# Their content existed nowhere on disk. The SOP this feature ships pitches undo
# as "try it, it undoes cleanly", which makes an unguarded delete worse.
a10 = adopt(paths=["assets/app.js"])              # a CREATE
(PROJ / "assets" / "app.js").write_text("// THE USER'S OWN WORK, written after")
u10 = run(R.adopt_undo_service(project_id=9198, backup_id=a10["backup_id"]))
ck("gate10 undo does NOT delete a created file the user edited afterwards",
   (PROJ / "assets" / "app.js").exists()
   and (PROJ / "assets" / "app.js").read_text().startswith("// THE USER'S OWN WORK"),
   "the user's file was destroyed")
ck("gate10 ...and it says so rather than reporting a clean undo",
   "assets/app.js" in (u10.get("kept") or []) and u10.get("note"), u10)
(PROJ / "assets" / "app.js").unlink()

# same for an OVERWRITE edited after the fact
a10b = adopt(paths=["index.html"])
(PROJ / "index.html").write_text("<title>edited after the adopt</title>")
u10b = run(R.adopt_undo_service(project_id=9198, backup_id=a10b["backup_id"]))
ck("gate10 undo does NOT clobber an overwritten file edited afterwards",
   (PROJ / "index.html").read_text() == "<title>edited after the adopt</title>",
   (PROJ / "index.html").read_text())
ck("gate10 ...and reports it kept", "index.html" in (u10b.get("kept") or []), u10b)
(PROJ / "index.html").write_text("<title>his working app</title>")

# ── gate11: overlapping adopts undone FORWARD ──────────────────────
# Measured: adopt A, adopt B, undo A, undo B left run A's content in the project
# while BOTH undos returned ok with a non-empty restored list. The old
# byte-for-byte gate could not see it — every undo in this file was in reverse
# order on disjoint paths, so it passed against the live bug.
(RUN_SRC / "page.html").write_text("RUN-A content")
(PROJ / "page.html").write_text("USER ORIGINAL")
A = adopt(paths=["page.html"])
(RUN_SRC / "page.html").write_text("RUN-B content")
B = adopt(paths=["page.html"])
uA = run(R.adopt_undo_service(project_id=9198, backup_id=A["backup_id"]))
ck("gate11 undoing the OLDER of two overlapping adopts does not silently "
   "restore stale content over the newer one",
   not uA.get("restored"), uA)
ck("gate11 ...it reports the file as kept, not restored",
   "page.html" in (uA.get("kept") or []), uA)
uB = run(R.adopt_undo_service(project_id=9198, backup_id=B["backup_id"]))
uA2 = run(R.adopt_undo_service(project_id=9198, backup_id=A["backup_id"]))
ck("gate11 ...and undoing them in the documented order gets the original back",
   (PROJ / "page.html").read_text() == "USER ORIGINAL",
   (PROJ / "page.html").read_text())
(PROJ / "page.html").unlink(); (RUN_SRC / "page.html").unlink()

# ── gate12: adopt's own artifacts must not be PUBLISHED ─────────────
# _site_webroot falls back to the project root, which is the static-dashboard
# case adopt was built for. Review replayed the publisher's filter and got every
# replaced draft plus a root-level JSON of session ids and run ids into the zip.
#
# THIS CALLS THE REAL _zip_site_dir AND READS THE ZIP. The first cut of this
# gate exec'd the two constants out of applications.py and re-implemented the
# loop by hand — so deleting the real `if rel.name in _SITE_ZIP_SKIP_FILES`
# left it green. Same defect as gate6's original: asserting on a path the
# product does not take.
import io as _io                                                        # noqa: E402
import zipfile as _zipfile                                              # noqa: E402
import server.routes.applications as _A                                 # noqa: E402

a12 = adopt(paths=["index.html"])
_names = _zipfile.ZipFile(_io.BytesIO(_A._zip_site_dir(PROJ))).namelist()
ck("gate12 the rollback store is not in the published zip",
   not any(n.startswith(".adopt-backups") for n in _names),
   [n for n in _names if n.startswith(".adopt-")])
ck("gate12 the provenance file is not in the published zip",
   ".adopt-provenance.json" not in _names,
   [n for n in _names if n.startswith(".adopt-")])
ck("gate12 ...while the real site files still are",
   "index.html" in _names and "server/main.py" in _names, _names[:8])
run(R.adopt_undo_service(project_id=9198, backup_id=a12["backup_id"]))

# ── gate13: the rollback store is not seeded into bake-off worktrees ─
ck("gate13 .adopt-backups is treated as machinery, not project content",
   R._is_review_dir(".adopt-backups"),
   "a seeded fan_out would copytree every past version into N harness worktrees")

# ── gate14: paths=[] means NOTHING, not everything ──────────────────
ck("gate14 an empty selection selects no files",
   R._adopt_files(RUN_SRC, []) == [], R._adopt_files(RUN_SRC, []))
ck("gate14 ...and paths=None still means all of them",
   len(R._adopt_files(RUN_SRC, None)) > 0)

# ── gate15: the run_id path, and whose project wins ─────────────────
# Not covered at all before: run_id, _adopt_resolve_run, and the precedence
# line. Review traced that the tool shim injects project_id from the ambient
# interaction_context on every project conversation, and that an explicit
# project_id used to WIN over the run's own — so a host sitting in project 9200
# saying "use run 9639" wrote run 9639's files over project 9200, while the
# docstring, the tool description and the SOP all said "into its project".
# The DB is stubbed; the precedence is the thing under test.
_saved_resolve = R._adopt_resolve_run


async def _fake_resolve(run_id):
    assert run_id == 9639
    return {"session_id": SESSION, "harness": "codex", "project_id": 9198}


R._adopt_resolve_run = _fake_resolve

d15 = run(R.adopt_run_service(run_id=9639, dry_run=True))
ck("gate15 run_id alone resolves the run", d15["session_id"] == SESSION
   and d15["harness"] == "codex", d15)
ck("gate15 ...and lands in the RUN's project", d15["project_id"] == 9198, d15)

# The ambient-project case, verbatim. Project 9200 is CREATED first: without it
# the call fails with "no project 9200" and a gate accepting any exception would
# pass for the wrong reason — the write would still have gone to 9200 had the
# directory existed.
(PROJECTS / "9200").mkdir(parents=True, exist_ok=True)
(PROJECTS / "9200" / "index.html").write_text("<title>a DIFFERENT project</title>")
d15b = run_expect_error(R.adopt_run_service(run_id=9639, project_id=9200, dry_run=True))
ck("gate15 a DIFFERENT project_id does not silently win over the run's",
   isinstance(d15b, Exception) and "belongs to project 9198" in str(getattr(d15b, "detail", d15b)),
   repr(d15b))
d15c = run_expect_error(R.adopt_run_service(run_id=9639, project_id=9200))
ck("gate15 ...and a real (non-dry) adopt does not write into it either",
   (PROJECTS / "9200" / "index.html").read_text() == "<title>a DIFFERENT project</title>",
   (PROJECTS / "9200" / "index.html").read_text())
# The SESSION path needs the same guard as the run_id path. Found by driving
# the HTTP route, not by calling the service: the fix was on the run_id branch
# only, and session_id + harness + a foreign project_id still wrote through.
sess_foreign = run_expect_error(R.adopt_run_service(
    session_id=SESSION, harness="codex", project_id=9200, paths=["index.html"]))
ck("gate15 a foreign project_id is refused on the SESSION path too",
   isinstance(sess_foreign, Exception)
   and "belongs to project 9198" in str(getattr(sess_foreign, "detail", sess_foreign)),
   repr(getattr(sess_foreign, "detail", sess_foreign)))
ck("gate15 ...and project 9200 still holds its own file",
   (PROJECTS / "9200" / "index.html").read_text() == "<title>a DIFFERENT project</title>",
   (PROJECTS / "9200" / "index.html").read_text()[:80])

ck("gate15 ...and adopted nothing into it",
   not (PROJECTS / "9200" / "assets").exists()
   and sorted(q.name for q in (PROJECTS / "9200").iterdir()) == ["index.html"],
   sorted(q.name for q in (PROJECTS / "9200").iterdir()))

# and the shim must not inject the ambient project when a run names its own
_tools_src = tools_source(pathlib.Path(__file__).resolve().parents[1])
_branch = _tools_src[_tools_src.index('name == "review.adopt"'):]
_branch = _branch[:_branch.index("_exec_review_adopt(a)")]
ck("gate15 the dispatch skips ambient project_id when run_id is given",
   'not a.get("run_id")' in _branch, _branch[-320:])

R._adopt_resolve_run = _saved_resolve

# A project that does not exist is refused, not created. This needs a session
# whose manifest does NOT name a project — otherwise the cross-project guard
# answers first and the existence check is never reached, so the gate passes
# with it deleted.
NOPROJ = "noproj00000000000000000000000000"
(SESSIONS / NOPROJ / "work" / "codex").mkdir(parents=True)
(SESSIONS / NOPROJ / "work" / "codex" / "x.txt").write_text("x")
(SESSIONS / NOPROJ / "manifest.json").write_text(json.dumps(
    {"worktree_root": str(SESSIONS / NOPROJ / "work")}))
noproj = run_expect_error(R.adopt_run_service(
    session_id=NOPROJ, harness="codex", project_id=99999))
ck("gate15 adopt does not conjure a project directory",
   isinstance(noproj, HTTPException) and noproj.status_code == 404
   and "no project 99999" in str(noproj.detail),
   f"{type(noproj).__name__}: {getattr(noproj, 'detail', noproj)}")
ck("gate15 ...and did not create it", not (PROJECTS / "99999").exists())

# ── gate16: runs recorded before 2026-08-22 are still adoptable ─────
# _session_worktree_root is documented as "the ONE resolver ... so they cannot
# disagree about where a run's output is". Adopt hardcoded work/<harness>
# instead, so every older run 404'd with "nothing to adopt" — which reads as a
# broken build, not as a resolver mismatch.
leg = run(R.adopt_run_service(session_id=LEGACY, harness="codex",
                              project_id=9198, dry_run=True))
ck("gate16 a legacy session (project_dir/<harness>) resolves",
   "legacy.txt" in leg["would_overwrite"] + leg["would_create"], leg)

# ── gate17: type collisions fail cleanly, before anything is written ─
# Review: project has a DIRECTORY where the run has a FILE -> IsADirectoryError
# out of the backup copy2, uncaught, 500, plus an orphan backup dir with no
# manifest. Reverse shape -> FileExistsError from mkdir MID-COPY-LOOP, leaving a
# partial adopt. Both must be refused up front, with the project untouched.
(RUN_SRC / "collide.txt").write_text("run has a file here")
(PROJ / "collide.txt").mkdir()
(PROJ / "collide.txt" / "inner").write_text("user content")
r17 = adopt(_expect_error=True)
ck("gate17 dir-in-project vs file-in-run is refused CLEANLY (409, not a 500 "
   "out of the middle of the copy)",
   isinstance(r17, HTTPException) and r17.status_code == 409,
   f"{type(r17).__name__}: {getattr(r17, 'detail', r17)}")
ck("gate17 ...and the user's directory is intact",
   (PROJ / "collide.txt" / "inner").read_text() == "user content")
ck("gate17 ...and no half-written adopt landed",
   not (PROJ / "assets" / "app.js").exists(),
   "a file from the same adopt was written before the failure")
ck("gate17 ...and it left no orphan backup dir for undo to trip over",
   not [d for d in (PROJ / ".adopt-backups").iterdir()
        if d.is_dir() and not (d / "manifest.json").exists()]
   if (PROJ / ".adopt-backups").is_dir() else True,
   [d.name for d in (PROJ / ".adopt-backups").iterdir()
    if d.is_dir() and not (d / "manifest.json").exists()]
   if (PROJ / ".adopt-backups").is_dir() else [])
shutil.rmtree(PROJ / "collide.txt"); (RUN_SRC / "collide.txt").unlink()

(RUN_SRC / "coll2").mkdir()
(RUN_SRC / "coll2" / "x.txt").write_text("run has a dir here")
(PROJ / "coll2").write_text("user has a FILE here")
r17b = adopt(_expect_error=True)
ck("gate17 file-in-project vs dir-in-run is refused CLEANLY (409, not a "
   "partial adopt)",
   isinstance(r17b, HTTPException) and r17b.status_code == 409,
   f"{type(r17b).__name__}: {getattr(r17b, 'detail', r17b)}")
ck("gate17 ...and the user's file is intact",
   (PROJ / "coll2").is_file() and (PROJ / "coll2").read_text() == "user has a FILE here")
(PROJ / "coll2").unlink(); shutil.rmtree(RUN_SRC / "coll2")

# ── gate18: adopt's artifacts never ride a seeded worktree ──────────
# .adopt-backups was made machinery and .adopt-provenance.json was not, so a
# seeded fan_out copied the provenance file into a harness worktree. Adopting
# that run later restored a STALE snapshot over the live file and then appended
# to it — silently destroying the record of every adopt in between — and shipped
# session ids and run ids into the vendor CLI's pod.
ck("gate18 the provenance file is machinery, like the backup store",
   R._is_review_dir(".adopt-provenance.json"),
   "a seeded fan_out would copy it into every harness worktree")
(RUN_SRC / ".adopt-provenance.json").write_text('[{"backup_id": "STALE"}]')
(RUN_SRC / ".adopt-backups").mkdir(exist_ok=True)
(RUN_SRC / ".adopt-backups" / "x.txt").write_text("stale backup")
ck("gate18 adopt never carries them OUT of a run directory",
   not any(".adopt-" in f for f in R._adopt_files(RUN_SRC, None)),
   [f for f in R._adopt_files(RUN_SRC, None) if ".adopt-" in f])
_prov_before = (PROJ / ".adopt-provenance.json").read_text()
a18 = adopt()
ck("gate18 ...so adopting a polluted run does not clobber live provenance",
   json.loads((PROJ / ".adopt-provenance.json").read_text())[0]
   != {"backup_id": "STALE"},
   (PROJ / ".adopt-provenance.json").read_text()[:120])
run(R.adopt_undo_service(project_id=9198, backup_id=a18["backup_id"]))
shutil.rmtree(RUN_SRC / ".adopt-backups"); (RUN_SRC / ".adopt-provenance.json").unlink()

# ── gate19: a PARTIAL undo is not marked done ───────────────────────
# undo A keeps f and restores g -> undo B -> the user follows the note's own
# advice ("undo newer adopts first") and the bare undo says there is nothing
# left, while run A's content sits in the project.
(RUN_SRC / "f.html").write_text("RUN-A f"); (RUN_SRC / "g.html").write_text("RUN-A g")
(PROJ / "f.html").write_text("USER ORIGINAL f"); (PROJ / "g.html").write_text("USER ORIGINAL g")
A19 = adopt(paths=["f.html", "g.html"])
(RUN_SRC / "f.html").write_text("RUN-B f")
B19 = adopt(paths=["f.html"])
uA19 = run(R.adopt_undo_service(project_id=9198, backup_id=A19["backup_id"]))
ck("gate19 the partial undo declines the contended file",
   uA19["kept"] == ["f.html"] and uA19["restored"] == ["g.html"], uA19)
run(R.adopt_undo_service(project_id=9198, backup_id=B19["backup_id"]))
uA19b = run_expect_error(R.adopt_undo_service(project_id=9198))
ck("gate19 ...and a bare undo afterwards still finds the partial one",
   not isinstance(uA19b, Exception) and uA19b["backup_id"] == A19["backup_id"],
   repr(uA19b))
ck("gate19 ...and the user's original comes back",
   (PROJ / "f.html").read_text() == "USER ORIGINAL f",
   (PROJ / "f.html").read_text())
for _f in ("f.html", "g.html"):
    (PROJ / _f).unlink(missing_ok=True); (RUN_SRC / _f).unlink(missing_ok=True)

# ── gate20: undo's failsafes ────────────────────────────────────────
# (a) a manifest with no fingerprints must be treated as UNKNOWN, not as safe.
a20 = adopt(paths=["index.html"])
_bm = PROJ / ".adopt-backups" / a20["backup_id"] / "manifest.json"
_m = json.loads(_bm.read_text()); _m.pop("wrote", None); _bm.write_text(json.dumps(_m))
u20 = run(R.adopt_undo_service(project_id=9198, backup_id=a20["backup_id"]))
ck("gate20 a fingerprint-less manifest makes undo decline, not destroy",
   u20["kept"] == ["index.html"] and not u20["restored"], u20)
_bm.write_text(json.dumps({**_m, "wrote": {"index.html": R._adopt_sha(PROJ / "index.html")}}))
run(R.adopt_undo_service(project_id=9198, backup_id=a20["backup_id"]))

# (b) a backup whose blob is gone cannot restore — and must SAY so
a20b = adopt(paths=["index.html"])
(PROJ / ".adopt-backups" / a20b["backup_id"] / "index.html").unlink()
u20b = run(R.adopt_undo_service(project_id=9198, backup_id=a20b["backup_id"]))
ck("gate20 a missing backup blob is reported, not silently skipped",
   u20b["kept"] == ["index.html"] and not u20b["restored"], u20b)
(PROJ / "index.html").write_text("<title>his working app</title>")

# (c) a backup dir with NO manifest, as the ONLY candidate. It has to be the
#     only one: with real backups alongside, a bare undo picks one of those and
#     the gate passes whether or not the orphan is skipped.
P201 = PROJECTS / "201"
(P201 / ".adopt-backups" / "9999999999-orphan").mkdir(parents=True)
(P201 / ".adopt-backups" / "9999999999-orphan" / "index.html").write_text("orphaned")
u20c = run_expect_error(R.adopt_undo_service(project_id=201))
ck("gate20 an orphan backup dir is not selectable — it carries no undo "
   "instructions, and used to 500 as 'no usable manifest'",
   isinstance(u20c, HTTPException) and u20c.status_code == 404
   and "no adopt to undo" in str(u20c.detail),
   f"{type(u20c).__name__}: {getattr(u20c, 'detail', u20c)}")

# (d) a crash BETWEEN the backup copies and the end of the copy loop must stay
#     undoable. The manifest is written before the loop for exactly this: every
#     backup blob is already on disk, so it is correct for the files copied so
#     far. Written only at the end, this left an unrecoverable half-adopt —
#     bare undo 404, explicit undo 500, pre-adopt bytes in a dir nothing reads.
_real_copy2 = shutil.copy2
_seen = {"n": 0}


def _copy2_that_dies(a, b, *args, **kw):
    # let the backup pass through, then die inside the ADOPT copy loop
    if str(b).startswith(str(PROJ)) and ".adopt-backups" not in str(b):
        _seen["n"] += 1
        if _seen["n"] > 1:
            raise OSError("simulated crash mid-copy")
    return _real_copy2(a, b, *args, **kw)


(PROJ / "crash-a.txt").write_text("USER A")
(PROJ / "crash-b.txt").write_text("USER B")
(RUN_SRC / "crash-a.txt").write_text("RUN A")
(RUN_SRC / "crash-b.txt").write_text("RUN B")
shutil.copy2 = _copy2_that_dies
R.shutil = shutil
crashed = adopt(paths=["crash-a.txt", "crash-b.txt"], _expect_error=True)
shutil.copy2 = _real_copy2
ck("gate20 a crash mid-copy raises", isinstance(crashed, Exception), repr(crashed))
_ids = sorted((PROJ / ".adopt-backups").iterdir(), key=lambda d: d.stat().st_mtime)
_last = _ids[-1]
ck("gate20 ...and still leaves a usable manifest behind",
   (_last / "manifest.json").is_file(),
   f"{_last.name} has no manifest — the half-adopt is unrecoverable")
u20d = run_expect_error(R.adopt_undo_service(project_id=9198, backup_id=_last.name))
ck("gate20 ...so the half-adopt can be undone",
   not isinstance(u20d, Exception) and (PROJ / "crash-a.txt").read_text() == "USER A",
   f"{u20d} / crash-a.txt={(PROJ / 'crash-a.txt').read_text()}")
# The file that never got copied is the one `pre` exists for: it is already in
# the state undo wants, and must be recognised as such rather than reported as
# declined. Asserting only on the copied file cannot see the pre-fingerprint go.
ck("gate20 ...and the file that never got copied reads as already-original, "
   "not as a file someone changed",
   not isinstance(u20d, Exception)
   and "crash-b.txt" in (u20d.get("already_original") or [])
   and "crash-b.txt" not in (u20d.get("kept") or []),
   u20d)
for _f in ("crash-a.txt", "crash-b.txt"):
    (PROJ / _f).unlink(missing_ok=True); (RUN_SRC / _f).unlink(missing_ok=True)

# ── gate21: undo leaves no empty directories behind ─────────────────
(RUN_SRC / "newdir").mkdir(exist_ok=True)
(RUN_SRC / "newdir" / "x.js").write_text("new")
a21 = adopt(paths=["newdir"])
run(R.adopt_undo_service(project_id=9198, backup_id=a21["backup_id"]))
ck("gate21 undo removes a directory that existed only for what it created",
   not (PROJ / "newdir").exists(),
   "empty newdir/ left behind — a state the project was never in")
shutil.rmtree(RUN_SRC / "newdir")

# THE NEGATIVE HALF. Pruning by "is it empty now" is a proxy for "did adopt
# create it", and the two differ exactly where it hurts: a user's own empty
# directory. Measured — an empty uploads/ was deleted with kept=[] and
# undone_at set, so the caller was told the undo was exact.
(PROJ / "uploads").mkdir()
(PROJ / "uploads").chmod(0o750)
(RUN_SRC / "uploads").mkdir(exist_ok=True)
(RUN_SRC / "uploads" / "logo.png").write_text("run asset")
a21b = adopt(paths=["uploads"])
u21b = run(R.adopt_undo_service(project_id=9198, backup_id=a21b["backup_id"]))
ck("gate21 a PRE-EXISTING empty directory survives the undo",
   (PROJ / "uploads").is_dir(),
   "the user's own empty uploads/ was deleted and the undo reported clean")
ck("gate21 ...with the mode the user chose",
   (PROJ / "uploads").is_dir() and ((PROJ / "uploads").stat().st_mode & 0o777) == 0o750,
   oct((PROJ / "uploads").stat().st_mode & 0o777) if (PROJ / "uploads").is_dir() else "gone")
# ...and the same for a multi-level scaffold the user made
(PROJ / "src" / "components" / "widgets").mkdir(parents=True)
(RUN_SRC / "src" / "components" / "widgets").mkdir(parents=True, exist_ok=True)
(RUN_SRC / "src" / "components" / "widgets" / "w.js").write_text("x")
a21c = adopt(paths=["src"])
run(R.adopt_undo_service(project_id=9198, backup_id=a21c["backup_id"]))
ck("gate21 ...and a multi-level pre-existing scaffold is not collapsed",
   (PROJ / "src" / "components" / "widgets").is_dir(),
   "all three levels of the user's scaffold were removed")
shutil.rmtree(PROJ / "uploads"); shutil.rmtree(PROJ / "src")
shutil.rmtree(RUN_SRC / "uploads"); shutil.rmtree(RUN_SRC / "src")

# A directory adopt created, that the user has since put their OWN file in,
# must survive. Only the emptiness check stands between the recorded list and
# deleting that file — swapping it for rmtree passed every other gate.
(RUN_SRC / "shared").mkdir(exist_ok=True)
(RUN_SRC / "shared" / "run.js").write_text("run file")
a21d = adopt(paths=["shared"])
(PROJ / "shared" / "mine.txt").write_text("MY OWN FILE, added after")
run(R.adopt_undo_service(project_id=9198, backup_id=a21d["backup_id"]))
ck("gate21 a created directory the user has since used is NOT removed",
   (PROJ / "shared").is_dir()
   and (PROJ / "shared" / "mine.txt").read_text() == "MY OWN FILE, added after",
   "the user's file went with the directory")
shutil.rmtree(PROJ / "shared"); shutil.rmtree(RUN_SRC / "shared")

# The prune bound is now structural rather than a comparison: created_dirs is
# built by walking UP from each destination and stopping at proj_root, so the
# root can never enter the list. Assert that, not the old `d != proj_root`.
(RUN_SRC / "deep" / "er").mkdir(parents=True, exist_ok=True)
(RUN_SRC / "deep" / "er" / "f.txt").write_text("x")
a21e = adopt(paths=["deep"], dry_run=False)
_rec = json.loads((PROJ / ".adopt-backups" / a21e["backup_id"] / "manifest.json").read_text())
ck("gate21 the project root is never a recorded created-dir",
   all(d not in ("", ".", "/") for d in _rec["created_dirs"]),
   _rec["created_dirs"])
ck("gate21 ...and the recorded dirs are deepest-first",
   _rec["created_dirs"] == sorted(_rec["created_dirs"],
                                  key=lambda x: x.count("/"), reverse=True),
   _rec["created_dirs"])
run(R.adopt_undo_service(project_id=9198, backup_id=a21e["backup_id"]))
ck("gate21 the prune never reaches the project root",
   PROJ.is_dir() and PROJECTS.is_dir(), "undo walked past proj_root")
shutil.rmtree(RUN_SRC / "deep")

# ── gate22: the tool shim validates what a model hands it ───────────
# project_id reaches int() in the service, so a non-numeric value from a model
# surfaced as a 500 rather than a bad argument. Exercised through the shim,
# because that is the only caller that can produce it.
from mastercode.runtime.tools import _exec_review_adopt                 # noqa: E402

bad = run(_exec_review_adopt({"run_id": 9639, "project_id": "not-a-number"}))
ck("gate22 a non-numeric project_id is a bad argument, not a 500",
   bad["status"] == "error" and "must be a number" in bad["result"], bad)
missing = run(_exec_review_adopt({}))
ck("gate22 ...and neither run_id nor session_id+harness is refused clearly",
   missing["status"] == "error" and "run_id" in missing["result"], missing)

# ── gate23: every sink that copies a project excludes the artifacts ─
# Review 2's blocker was one missed sink; review 3 found a second (the gitea
# sync). Enumerate them here so the next one is caught by a gate rather than by
# a reviewer. _land_worktree_in_project's file filter had NO gate at all —
# deleting it left all 89 green.
import inspect                                                          # noqa: E402

_land_src = inspect.getsource(R._land_worktree_in_project)
ck("gate23 pick/land filters the artifacts out of the worktree",
   "_ADOPT_ARTIFACTS" in _land_src, _land_src[-300:])
ck("gate23 fan_out seeding treats them as machinery",
   R._is_review_dir(".adopt-backups") and R._is_review_dir(".adopt-provenance.json"))

# the gitea sync path — its own docstring says it mirrors _is_review_dir
import tempfile as _tf                                                  # noqa: E402
from server.routes.project_git import _write_exclude                    # noqa: E402

_gd = pathlib.Path(_tf.mkdtemp())
import subprocess as _sp                                                 # noqa: E402
_sp.run(["git", "-C", str(_gd), "init", "-q"], check=True)
_write_exclude(_gd)
_ex = (_gd / ".git" / "info" / "exclude").read_text()


def _ignored(rel: str) -> bool:
    """What git ACTUALLY does with the exclude file, not what its text says."""
    return _sp.run(["git", "-C", str(_gd), "check-ignore", "-q", "--no-index", rel]).returncode == 0

ck("gate23 the gitea sync excludes the rollback store",
   _ignored(".adopt-backups/x/a.js"), _ex)
ck("gate23 the gitea sync excludes the provenance file",
   _ignored(".adopt-provenance.json"), _ex)
# An adopt INTO a module keeps its bookkeeping in the
# module's directory — excluded there too, and the module's content is not.
ck("gate23 a module's adopt bookkeeping is excluded too",
   _ignored("reader/.adopt-backups/x/a.js") and _ignored("reader/.adopt-provenance.json"), _ex)
ck("gate23 ...while the module's own files are not",
   not _ignored("reader/index.html"), _ex)
shutil.rmtree(_gd, ignore_errors=True)

# ── gate24: a run that changes UNDER the adopt ──────────────────────
# `wrote` is the run file's hash before the copy. If the run dir is rewritten
# mid-adopt (a still-active run: _collect_out wipes and re-extracts), the
# planned hash is not what landed, and undo declined a file it could restore.
_real_copy2b = shutil.copy2
(PROJ / "live.txt").write_text("USER ORIGINAL")
(RUN_SRC / "live.txt").write_text("RUN v1")


def _copy2_mutating_source(a, b, *args, **kw):
    # Only the copy OUT OF THE RUN DIR. Keying on the filename alone also fired
    # on the backup copy — whose source is the project's own file — so the
    # probe rewrote the thing it was trying to preserve and "failed" against
    # correct code.
    if str(a).startswith(str(RUN_SRC)) and str(a).endswith("live.txt"):
        pathlib.Path(a).write_text("RUN v2")        # the run moved under us
    return _real_copy2b(a, b, *args, **kw)


shutil.copy2 = _copy2_mutating_source
a24 = adopt(paths=["live.txt"])
shutil.copy2 = _real_copy2b
u24 = run(R.adopt_undo_service(project_id=9198, backup_id=a24["backup_id"]))
ck("gate24 undo restores a file whose run content changed mid-adopt",
   (PROJ / "live.txt").read_text() == "USER ORIGINAL",
   f'{u24} / live.txt={(PROJ / "live.txt").read_text()}')
(PROJ / "live.txt").unlink(); (RUN_SRC / "live.txt").unlink()

# ── gate25: out-of-order undo where the bytes happen to match ───────
# `already_original` bypassed the `if not kept` guard: undoing the older of two
# overlapping adopts, where the newer content equals the pre-adopt bytes, gave
# a clean ok with no note and undone_at set. completed_at is what separates
# "we never wrote it" (crash) from "someone put it back" (out of order).
(PROJ / "same.html").write_text("SHARED BYTES")
(RUN_SRC / "same.html").write_text("RUN-A same")
A25 = adopt(paths=["same.html"])
(RUN_SRC / "same.html").write_text("SHARED BYTES")     # B writes back the original
B25 = adopt(paths=["same.html"])
u25 = run(R.adopt_undo_service(project_id=9198, backup_id=A25["backup_id"]))
ck("gate25 an out-of-order undo is not reported as a clean one",
   "same.html" in (u25.get("kept") or []) and u25.get("note"), u25)
ck("gate25 ...and the older adopt is NOT marked done",
   not json.loads((PROJ / ".adopt-backups" / A25["backup_id"] / "manifest.json")
                  .read_text()).get("undone_at"),
   "A was marked undone after declining its work")
run(R.adopt_undo_service(project_id=9198, backup_id=B25["backup_id"]))
run(R.adopt_undo_service(project_id=9198, backup_id=A25["backup_id"]))
(PROJ / "same.html").unlink(missing_ok=True); (RUN_SRC / "same.html").unlink()

# ── gate26: two run paths cannot collide on one destination ─────────
(PROJ / "a").mkdir(exist_ok=True)
(PROJ / "b").symlink_to(PROJ / "a")
(RUN_SRC / "a").mkdir(exist_ok=True); (RUN_SRC / "b").mkdir(exist_ok=True)
(RUN_SRC / "a" / "x.js").write_text("from a"); (RUN_SRC / "b" / "x.js").write_text("from b")
r26 = adopt(paths=["a", "b"], _expect_error=True)
ck("gate26 two run paths resolving to one destination are refused",
   isinstance(r26, HTTPException) and r26.status_code == 409,
   f"{type(r26).__name__}: {getattr(r26, 'detail', r26)}")
ck("gate26 ...and the user's directory is untouched",
   (PROJ / "a").is_dir() and not any((PROJ / "a").iterdir()),
   sorted(q.name for q in (PROJ / "a").iterdir()))
(PROJ / "b").unlink(); shutil.rmtree(PROJ / "a")
shutil.rmtree(RUN_SRC / "a"); shutil.rmtree(RUN_SRC / "b")

# ── gate8: the host can actually reach it ───────────────────────────
from mastercode.runtime.meta_tool_caps import META_TOOL_CAPS         # noqa: E402

ck("gate8 review.adopt is in the tool catalog the host reads",
   "review.adopt" in META_TOOL_CAPS, sorted(k for k in META_TOOL_CAPS if "adopt" in k))
ck("gate8 review.adopt_undo too", "review.adopt_undo" in META_TOOL_CAPS)
# Assert the VOCABULARY, not agreement with a neighbour: "same bits as
# propose_commit" passes even if both are wrong together.
from mastercode.runtime.agent_grant import describe as _describe          # noqa: E402
ck("gate8 adopt's bit IS write_context (not merely equal to a sibling's)",
   _describe(META_TOOL_CAPS["review.adopt"]["required_bits"]) == "write_context",
   _describe(META_TOOL_CAPS["review.adopt"]["required_bits"]))
ck("gate8 ...and adopt_undo's too",
   _describe(META_TOOL_CAPS["review.adopt_undo"]["required_bits"]) == "write_context",
   _describe(META_TOOL_CAPS["review.adopt_undo"]["required_bits"]))

_tools = pathlib.Path(__file__).resolve().parents[1] / "brain/mastercode/runtime/tools.py"
_src = tools_source(pathlib.Path(__file__).resolve().parents[1])
for n in ("review.adopt", "review_adopt", "review.adopt_undo", "review_adopt_undo"):
    ck(f"gate8 dispatch handles {n!r}", f'name == "{n}"' in _src)
ck("gate8 the implementations exist",
   "async def _exec_review_adopt(" in _src and "async def _exec_review_adopt_undo(" in _src)

# gate27 — the Host can actually REACH it. The grep above proves a handler
# exists in _execute_meta_tool; it says nothing about whether a call ever gets
# there. Three things decide that, and the first version of this feature had
# only one of them: (a) a schema in PUBLIC_TOOL_SCHEMAS — without it the tool
# is absent from tools= and the Host cannot name it; (b) the name in
# PUBLIC_META_TOOL_NAMES — the session router (session.py, `if func_name in
# _META_TOOL_NAMES`) answers "Unknown tool" for anything else BEFORE
# _execute_meta_tool; (c) lane "host" — so it is held natively by the Host
# with the session's interaction_context (the ambient project_id the shim
# injects only exists on that path; `operate` passes None) and is stripped
# from every agent scope. Measured with the real objects, not the source.
from mastercode.public_tool_registry import (                              # noqa: E402
    PUBLIC_TOOL_SCHEMAS as _PTS, PUBLIC_META_TOOL_NAMES as _PMN)
from mastercode.runtime.tool_lanes import lane_for as _lane_for             # noqa: E402
from mastercode.runtime.tools import host_tool_allowed_on as _htao          # noqa: E402
from mastercode.runtime.triage import _META_TOOL_NAMES as _ROUTER_NAMES     # noqa: E402
for n in ("review.adopt", "review.adopt_undo"):
    ck(f"gate27 {n} has a schema (the Host can see it)", n in _PTS)
    ck(f"gate27 {n} schema name matches its key",
       ((_PTS.get(n) or {}).get("function") or {}).get("name") == n)
    ck(f"gate27 {n} is in the dispatch set (the router will execute it)", n in _PMN)
    ck(f"gate27 {n} is in the router's live name set", n in _ROUTER_NAMES)
    ck(f"gate27 {n} is lane 'host' (the Host's own, never an agent's)",
       _lane_for(n, _PTS.get(n)) == "host", _lane_for(n, _PTS.get(n)))
    ck(f"gate27 {n} is advertised on a project conversation",
       _htao(n, "project:1") is True)
    ck(f"gate27 {n} is advertised in chat", _htao(n, "chat") is True)
# The schema's parameters must name what the service accepts, or the Host
# fills in a shape the shim drops on the floor.
_adopt_props = set(_PTS["review.adopt"]["function"]["parameters"]["properties"])
ck("gate27 adopt schema exposes run_id/session_id/harness/project_id/paths/dry_run/into",
   _adopt_props == {"run_id", "session_id", "harness", "project_id", "paths", "dry_run", "into"},
   sorted(_adopt_props))
_undo_props = set(_PTS["review.adopt_undo"]["function"]["parameters"]["properties"])
ck("gate27 adopt_undo schema exposes project_id/backup_id/module",
   _undo_props == {"project_id", "backup_id", "module"}, sorted(_undo_props))

_sop = (pathlib.Path(__file__).resolve().parents[1] / "default-skills/review.md").read_text()
ck("gate8 the SOP lets the review skill call it",
   "review.adopt" in _sop.split("---")[1], _sop.split("---")[1])
# The SOP must point "use run N" at adopt. Without this the host keeps reaching
# for compare/propose_commit — which is exactly what happened for four hours.
ck("gate8 the SOP tells the host adopt is the answer to \"use run N\"",
   "review.adopt(run_id=" in _sop and "Do NOT reach for compare/propose_commit" in _sop)

shutil.rmtree(WORK, ignore_errors=True)
if fails:
    print(f"\nreview.adopt gates: FAIL ({len(fails)}): {fails}")
    sys.exit(1)
print("\nreview.adopt gates: all passed")
