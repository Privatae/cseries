#!/usr/bin/env python3
"""Gate: the kernel's writers into project folders never write through a link
(SECURITY).

`set_project_manifest_service` wrote app.yaml with Path.write_text, the land
of a build (review._snapshot_to_project) with shutil.copyfile, and the
artifact ops (_write_new_inode) with write_bytes after an lstat. Each followed
a destination that is a symbolic link, and each wrote into the existing inode,
so a hard link to a file elsewhere (inside .git, say) changed that file too.
They now go through server/replace_file.py: each folder below the caller's
root opened with O_NOFOLLOW, the bytes written to a new file, renamed over the
name; a name or folder that is a link is refused.

Checked here, with temp folders and a stub pool (no DB, no network):
  A. replace_file itself: a hard link elsewhere keeps its bytes; a link as the
     name, or as a folder on the way, is refused and its target untouched; a
     folder as the name is refused; an existing file's mode is kept, a new
     file is 0644; no temporary file is left behind; a source that is a link
     is refused.
  B. the artifact write: a name that is a link to another file in the
     workspace, or a folder on the way that is one, is refused and the other
     file keeps its bytes; a hard link to a file in .git keeps .git's bytes.
  C. the land of a build: a project file hard-linked to another keeps the
     other's bytes.
  D. app.yaml: a manifest that is a link is refused (409), its target untouched.
  E. workspace_file: a name that is a link is refused; a hard link's other name
     (in .git) keeps its bytes.
  F. module snapshots: a hard link keeps its other name's bytes; the source's
     mode and times are kept, as copy2 did.
  G. an invalid app.yaml puts the previous manifest back instead of deleting it.
  H. a read with a folder link on the way is refused; a descriptor's bytes are
     written as a new inode; a named pipe as a copy's source is refused at
     once, not waited on; adopt undo refuses a module whose rollback store is
     a link and leaves the linked store untouched; an artifact restore that
     meets a link planted after the clear writes nothing through it and says
     the workspace is only partly restored.

Run:  python3 scripts/test_kernel_writes_no_follow.py
"""
from __future__ import annotations

import asyncio
import json
import atexit
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO / "brain"))
sys.path.insert(0, str(REPO))

FAILS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


WORK = Path(tempfile.mkdtemp(prefix="nofollow-")).resolve()
atexit.register(shutil.rmtree, WORK, True)


def raises(fn, exc=Exception):
    try:
        fn()
    except exc as e:  # noqa: BLE001
        return e
    return None


# ── A. replace_file ─────────────────────────────────────────────────

def section_a():
    try:
        from server import replace_file as RF
    except ImportError:  # before the fix
        print("A. replace_file")
        check("server/replace_file.py exists", False)
        return
    print("A. replace_file")
    a = WORK / "a"
    a.mkdir()
    (a / "other").write_bytes(b"other's bytes")
    os.link(a / "other", a / "name")
    RF.replace_file(a, "name", b"new bytes")
    check("a hard link elsewhere keeps its bytes", (a / "other").read_bytes() == b"other's bytes")
    check("the name holds the new bytes", (a / "name").read_bytes() == b"new bytes")

    (a / "target").write_bytes(b"target")
    (a / "lnk").symlink_to("target")
    e = raises(lambda: RF.replace_file(a, "lnk", b"x"), RF.LinkRefused)
    check("a name that is a link is refused", e is not None)
    check("the link's target is untouched", (a / "target").read_bytes() == b"target")
    check("the link is still a link", (a / "lnk").is_symlink())

    (a / "realdir").mkdir()
    (a / "realdir" / "f").write_bytes(b"inside")
    (a / "dirlnk").symlink_to("realdir")
    e = raises(lambda: RF.replace_file(a, "dirlnk/f", b"x"), RF.LinkRefused)
    check("a folder on the way that is a link is refused", e is not None)
    check("the linked folder's file is untouched", (a / "realdir" / "f").read_bytes() == b"inside")

    (a / "adir").mkdir()
    check("a folder as the name is refused", raises(lambda: RF.replace_file(a, "adir", b"x"), RF.LinkRefused) is not None)

    (a / "script.sh").write_bytes(b"#!/bin/sh\n")
    os.chmod(a / "script.sh", 0o755)
    RF.replace_file(a, "script.sh", b"#!/bin/sh\necho hi\n")
    check("an existing file keeps its mode", stat.S_IMODE(os.stat(a / "script.sh").st_mode) == 0o755)
    RF.replace_file(a, "new/deeper/file.txt", "text")
    check("a new file in new folders is written", (a / "new" / "deeper" / "file.txt").read_text() == "text")
    check("a new file is 0644", stat.S_IMODE(os.stat(a / "new" / "deeper" / "file.txt").st_mode) == 0o644)
    leftovers = [p.name for p in a.rglob("*.new")]
    check("no temporary file is left behind", leftovers == [], str(leftovers))
    for bad in ("../escape", "/abs", "", ".", "a/../b"):
        check(f"{bad!r} is not a path inside the folder",
              raises(lambda b=bad: RF.replace_file(a, b, b"x"), RF.LinkRefused) is not None)
    # The path is read the way the callers' own checks read it (pathlib):
    # what they checked is what is written. './x' and 'a//b' name x and a/b.
    for given, lands in (("./dot.txt", "dot.txt"), ("sp//lit.txt", "sp/lit.txt"),
                         ("trail.txt/", "trail.txt")):
        e = raises(lambda g=given: RF.replace_file(a, g, b"same place"))
        check(f"{given!r} is written where pathlib names it ({lands})",
              e is None and (a / lands).read_bytes() == b"same place", repr(e))
    # A backslash is a character of a name on Linux, never a separator: the
    # callers check `.git\\config` as ONE name, so it must be written as one.
    (a / ".git").mkdir()
    (a / ".git" / "config").write_bytes(b"[core]\n")
    raises(lambda: RF.replace_file(a, ".git\\config", b"[core]\n\tfsmonitor = evil\n"))
    check("a backslash never turns one name into .git/config",
          (a / ".git" / "config").read_bytes() == b"[core]\n")
    # The permission bits kept are rwx only: a set-user-ID bit on the old file
    # would otherwise be put on a new file the kernel owns, with new bytes.
    # An unprivileged CI runner may refuse to set a set-user-ID bit (EPERM),
    # so the first special bit this machine lets the test set is used; with
    # none settable the check says so instead of passing on an rwx-only file.
    (a / "suid").write_bytes(b"old")
    special = None
    for bits in (0o4755, 0o2755, 0o1755):
        try:
            os.chmod(a / "suid", bits)
        except PermissionError:
            continue
        if stat.S_IMODE(os.stat(a / "suid").st_mode) == bits:
            special = bits
            break
    if special is None:
        print("  SKIP  no set-user-ID, set-group-ID or sticky bit can be set on a file here;"
              " the rwx-only rule is not checked on this machine")
    else:
        RF.replace_file(a, "suid", b"new")
        check(f"a {oct(special)} file's new inode keeps the rwx bits only",
              stat.S_IMODE(os.stat(a / "suid").st_mode) == 0o755,
              oct(stat.S_IMODE(os.stat(a / "suid").st_mode)))

    (a / "srclink").symlink_to("other")
    check("a source that is a link is refused",
          raises(lambda: RF.replace_file_from(a, "copied", a / "srclink"), OSError) is not None)
    RF.replace_file_from(a, "copied", a / "other")
    check("a plain source is copied", (a / "copied").read_bytes() == b"other's bytes")


# ── B. the artifact write ───────────────────────────────────────────

def section_b():
    from server.applications import artifacts as AR
    print("B. artifacts._write_files")
    ws = WORK / "artifact-1"
    (ws / ".git").mkdir(parents=True)
    (ws / ".git" / "config").write_bytes(b"[core]\n")
    (ws / "index.html").write_bytes(b"<p>index</p>")
    (ws / "alias.html").symlink_to("index.html")
    e = raises(lambda: AR._write_files(ws, [{"path": "alias.html", "content": "<p>evil</p>"}]), AR.ArtifactError)
    check("a name that is a link to another file is refused", e is not None and getattr(e, "status", 400) == 400)
    check("the other file keeps its bytes", (ws / "index.html").read_bytes() == b"<p>index</p>")

    (ws / "real").mkdir()
    (ws / "real" / "page.html").write_bytes(b"real page")
    (ws / "via").symlink_to("real")
    e = raises(lambda: AR._write_files(ws, [{"path": "via/page.html", "content": "x"}]), AR.ArtifactError)
    check("a folder on the way that is a link is refused", e is not None)
    check("the folder's file keeps its bytes", (ws / "real" / "page.html").read_bytes() == b"real page")

    os.link(ws / ".git" / "config", ws / "notes.txt")
    AR._write_files(ws, [{"path": "notes.txt", "content": "[filter \"x\"]\n\tclean = touch /tmp/pwned\n"}])
    check("a hard link to .git/config keeps .git's bytes", (ws / ".git" / "config").read_bytes() == b"[core]\n")
    check("the written name holds the new bytes", b"filter" in (ws / "notes.txt").read_bytes())

    # The artifact checks (_reject_unsafe_path, _reject_git_target) read the
    # path with pathlib, where a backslash is part of a name: the write must
    # read it the same way, or `.git\\config` passes them and lands in .git.
    raises(lambda: AR._write_files(ws, [{"path": ".git\\config",
                                         "content": "[core]\n\tfsmonitor = touch /tmp/pwned\n"}]))
    check("an artifact write of '.git\\\\config' leaves .git/config alone",
          (ws / ".git" / "config").read_bytes() == b"[core]\n")
    e = raises(lambda: AR._write_files(ws, [{"path": "./index.html", "content": "<p>dot</p>"}]))
    check("an artifact write of './index.html' writes index.html, as before",
          e is None and (ws / "index.html").read_bytes() == b"<p>dot</p>", repr(e))


# ── C. the land of a build ──────────────────────────────────────────

def section_c():
    from server.routes import review as RV
    print("C. review._snapshot_to_project")
    projects = WORK / "projects"
    proj = projects / "7"
    (proj / "keep").mkdir(parents=True)
    (proj / "keep" / "secret").write_bytes(b"keep me")
    os.link(proj / "keep" / "secret", proj / "style.css")
    run_dir = WORK / "run"
    run_dir.mkdir()
    (run_dir / "style.css").write_bytes(b"body{}")
    RV.PROJECTS_ROOT = projects
    changed = RV._snapshot_to_project(7, run_dir, ["style.css"])
    check("the landed file is reported", changed == ["style.css"], str(changed))
    check("a hard-linked project file's other name keeps its bytes",
          (proj / "keep" / "secret").read_bytes() == b"keep me")
    check("the landed name holds the run's bytes", (proj / "style.css").read_bytes() == b"body{}")

    # A harness owns its worktree and may name a file `.git\\hooks\\pre-commit`
    # (one name on Linux). The land's .git check reads it as one name; the
    # write must too, never as .git/hooks/pre-commit in the project.
    (proj / ".git" / "hooks").mkdir(parents=True)
    (run_dir / ".git\\hooks\\pre-commit").write_bytes(b"#!/bin/sh\ntouch /tmp/pwned\n")
    RV._snapshot_to_project(7, run_dir, [".git\\hooks\\pre-commit"])
    check("a land of '.git\\\\hooks\\\\pre-commit' plants no hook in the project's .git",
          not (proj / ".git" / "hooks" / "pre-commit").exists())


# ── D. app.yaml ─────────────────────────────────────────────────────

def section_d():
    from server.routes import projects as PJ
    print("D. set_project_manifest_service")
    if not hasattr(PJ, "_MANIFEST_PROJECTS_DIR"):
        # Before the fix the folder is /var/lib/cseries/projects itself: not run
        # here, so a check of the old code never writes into this machine.
        check("app.yaml is written under a folder a test can name", False)
        return


    class _Pool:
        async def fetchrow(self, sql="", *a):
            return {"id": a[0] if a else 0}


    mroot = WORK / "manifest-projects"
    (mroot / "9").mkdir(parents=True)
    (mroot / "outside.txt").write_bytes(b"not a manifest")
    (mroot / "9" / "app.yaml").symlink_to("../outside.txt")
    PJ._MANIFEST_PROJECTS_DIR = mroot
    err = None
    try:
        asyncio.run(PJ.set_project_manifest_service(_Pool(), 1, 9, "id: x\nname: y\n"))
    except PJ.ProjectsError as ex:
        err = ex
    check("an app.yaml that is a link is refused", err is not None and err.status == 409,
          repr(err))
    check("the link's target is untouched", (mroot / "outside.txt").read_bytes() == b"not a manifest")

    # The projects folder is made at runtime, not shipped in the image: the
    # first manifest on a new Host must not fail because it is not there yet.
    PJ._MANIFEST_PROJECTS_DIR = WORK / "not-made-yet" / "projects"
    err = None
    try:
        asyncio.run(PJ.set_project_manifest_service(_Pool(), 1, 9, "id: x\nname: y\n"))
    except PJ.ProjectsError:
        pass                       # a manifest verdict, not a missing folder
    except Exception as ex:        # noqa: BLE001
        err = ex
    check("a projects folder not made yet is made, as before", err is None, repr(err))


def section_e():
    print("E. workspace_file write (capabilities.file_access.write_file)")
    from server.capabilities import file_access as FA
    proj = WORK / "fa-project"
    (proj / ".git").mkdir(parents=True)
    (proj / ".git" / "config").write_bytes(b"[core]\n")
    (proj / "index.html").write_bytes(b"<p>index</p>")
    (proj / "alias.html").symlink_to("index.html")
    r = FA.write_file("alias.html", "<p>evil</p>", base=proj)
    check("a name that is a link is refused", r.get("status") != "ok", str(r))
    check("the file it points to keeps its bytes", (proj / "index.html").read_bytes() == b"<p>index</p>")
    os.link(proj / ".git" / "config", proj / "notes.txt")
    r = FA.write_file("notes.txt", "[filter]\n", base=proj)
    check("a hard-linked name is written", r.get("status") == "ok", str(r))
    check("its other name in .git keeps its bytes", (proj / ".git" / "config").read_bytes() == b"[core]\n")


def section_f():
    print("F. module snapshots (project_modules._copy_content)")
    from server.routes import project_modules as PM
    src = WORK / "pm-src"
    dst = WORK / "pm-dst"
    (src / "bin").mkdir(parents=True)
    (src / "bin" / "run.sh").write_bytes(b"#!/bin/sh\n")
    os.chmod(src / "bin" / "run.sh", 0o755)
    os.utime(src / "bin" / "run.sh", (1_700_000_000, 1_700_000_000))
    (dst / "bin").mkdir(parents=True)
    (dst / "elsewhere").write_bytes(b"keep")
    os.link(dst / "elsewhere", dst / "bin" / "run.sh")
    n = PM._copy_content(src, dst)
    check("the file is copied", n == 1 and (dst / "bin" / "run.sh").read_bytes() == b"#!/bin/sh\n")
    check("a hard link in the snapshot keeps its other name's bytes", (dst / "elsewhere").read_bytes() == b"keep")
    st = os.stat(dst / "bin" / "run.sh")
    check("the source's mode is kept, as copy2 did", stat.S_IMODE(st.st_mode) == 0o755, oct(st.st_mode))
    check("the source's modification time is kept, as copy2 did", int(st.st_mtime) == 1_700_000_000)


def section_g():
    print("G. an invalid app.yaml puts the previous one back")
    from server.routes import projects as PJ
    if not hasattr(PJ, "_MANIFEST_PROJECTS_DIR"):
        check("app.yaml is written under a folder a test can name", False)
        return

    class _Pool:
        async def fetchrow(self, sql="", *a):
            return {"id": a[0] if a else 0}

    mroot = WORK / "manifest-rollback"
    (mroot / "4").mkdir(parents=True)
    good = b"id: kept\nname: Kept\n"
    (mroot / "4" / "app.yaml").write_bytes(good)
    PJ._MANIFEST_PROJECTS_DIR = mroot
    err = None
    try:
        asyncio.run(PJ.set_project_manifest_service(_Pool(), 1, 4, "this: [is not a manifest"))
    except PJ.ProjectsError as ex:
        err = ex
    check("an invalid manifest is refused", err is not None and err.status == 400, repr(err))
    check("the manifest before it is put back", (mroot / "4" / "app.yaml").read_bytes() == good)


def section_h():
    print("H. reads with no link on the way, descriptor-fed writes, and adopt undo's module store")
    try:
        from server import replace_file as RF
        _ = RF.open_no_follow, RF.replace_file_from_fd
    except (ImportError, AttributeError):
        check("open_no_follow and replace_file_from_fd exist", False)
        return
    h = WORK / "h"
    (h / "real").mkdir(parents=True)
    (h / "real" / "f").write_bytes(b"real")
    (h / "via").symlink_to("real")
    check("a read through a folder that is a link is refused",
          raises(lambda: RF.open_no_follow(h, "via/f"), RF.LinkRefused) is not None)
    fd = RF.open_no_follow(h, "real/f")
    try:
        RF.replace_file_from_fd(h, "copy/of-f", fd)
    finally:
        os.close(fd)
    check("a descriptor's bytes are written", (h / "copy" / "of-f").read_bytes() == b"real")
    check("a copy whose source path has a folder link is refused",
          raises(lambda: RF.replace_file_from(h, "x", "via/f", src_root=h), RF.LinkRefused) is not None)

    # A named pipe as the source: adopt backs up any project name that is not
    # a folder, and shutil.copy2 refused a pipe before opening it. An open for
    # reading waits for a writer, so the copy hung the kernel's event loop.
    import threading
    os.mkfifo(h / "pipe")
    got: dict = {}

    def _copy_pipe():
        got["e"] = raises(lambda: RF.replace_file_from(h, "pipe-copy", "pipe", like_copy2=True,
                                                       src_root=h), RF.LinkRefused)
    t = threading.Thread(target=_copy_pipe, daemon=True)
    t.start()
    t.join(5)
    hung = t.is_alive()
    if hung:  # let the waiting open finish so the thread does not outlive the gate
        os.close(os.open(h / "pipe", os.O_WRONLY | os.O_NONBLOCK))
        t.join(5)
    check("a named pipe as the source is refused at once, not waited on",
          not hung and got.get("e") is not None, "the open waited for a writer" if hung else repr(got))

    from server.routes import review as RV
    from fastapi import HTTPException
    projects = WORK / "undo-projects"
    (projects / "21" / "mod").mkdir(parents=True)
    (WORK / "elsewhere-store" / "b1").mkdir(parents=True)
    (WORK / "elsewhere-store" / "b1" / "manifest.json").write_text('{"backup_id": "b1"}')
    (projects / "21" / "mod" / RV._ADOPT_BACKUPS).symlink_to(WORK / "elsewhere-store")
    RV.PROJECTS_ROOT = projects
    from server.routes import project_modules as PM
    PM.PROJECTS_ROOT = projects
    err = None
    try:
        asyncio.run(RV.adopt_undo_service(project_id=21, module="mod", backup_id="b1"))
    except HTTPException as e:
        err = e
    except Exception as e:  # noqa: BLE001
        err = e
    check("undo refuses a named module whose rollback store is a link",
          isinstance(err, HTTPException) and err.status_code == 400 and "link" in str(err.detail), repr(err))
    check("the store the link points at is untouched",
          "undone_at" not in (WORK / "elsewhere-store" / "b1" / "manifest.json").read_text())

    # The race: the store is a plain folder when undo checks it, then a link
    # before undo reads the backup. _newest_adopt runs in exactly that window.
    import hashlib
    (projects / "22").mkdir()
    (projects / "22" / RV._ADOPT_BACKUPS).mkdir()
    (projects / "22" / "f.txt").write_bytes(b"adopted")
    out_store = WORK / "race-store"
    (out_store / "b2").mkdir(parents=True)
    (out_store / "b2" / "f.txt").write_bytes(b"from outside")
    (out_store / "b2" / "manifest.json").write_text(json.dumps({
        "backup_id": "b2", "overwrote": ["f.txt"], "created": [], "completed_at": 1,
        "wrote": {"f.txt": hashlib.sha256(b"adopted").hexdigest()}}))
    real_newest = RV._newest_adopt

    def swap_then_newest(roots):
        store = projects / "22" / RV._ADOPT_BACKUPS
        store.rmdir()
        store.symlink_to(out_store)
        return "b2", None
    RV._newest_adopt = swap_then_newest
    err = None
    try:
        asyncio.run(RV.adopt_undo_service(project_id=22))
    except HTTPException as e:
        err = e
    except Exception as e:  # noqa: BLE001
        err = e
    finally:
        RV._newest_adopt = real_newest
    check("undo refuses a store swapped for a link after its check",
          isinstance(err, HTTPException) and err.status_code == 400, repr(err))
    check("nothing from the linked store is restored into the project",
          (projects / "22" / "f.txt").read_bytes() == b"adopted")
    check("the linked store is not marked undone",
          "undone_at" not in (out_store / "b2" / "manifest.json").read_text())

    # server/code_harness.py wrote model output into project folders with
    # write_text, which follows a link, and ran it (python3, psql). Nothing
    # imported it; it was deleted rather than converted, and stays deleted.
    check("the unused model-output writer server/code_harness.py stays deleted",
          not (REPO / "server" / "code_harness.py").exists())

    # A restore that meets a link planted after the workspace was cleared stops
    # there, with the workspace emptied and only partly written back. The answer
    # must say so: "not restored" reads as "nothing changed".
    from server.routes import applications as APPS
    from server.applications import artifacts as AR
    aroot = WORK / "revert-projects"
    ws = aroot / "31"
    (ws / ".history" / "0001" / "a").mkdir(parents=True)
    (ws / ".history" / "0001" / "z").mkdir(parents=True)
    (ws / ".history" / "0001" / "a" / "first.html").write_text("first")
    (ws / ".history" / "0001" / "z" / "last.html").write_text("last")
    (ws / "now.html").write_text("the page as it is now")
    (WORK / "revert-elsewhere").mkdir()
    APPS.PROJECTS_ROOT = str(aroot)

    class _P:
        async def fetchrow(self, *a):
            return {"id": 31, "kind": "artifact"}

        async def execute(self, *a):
            return "UPDATE 1"

        async def fetchval(self, *a):
            return 0

    real_clear = AR._clear_workspace

    def _clear_then_plant(workspace, *, keep):
        real_clear(workspace, keep=keep)
        (workspace / "z").symlink_to(WORK / "revert-elsewhere")
    AR._clear_workspace = _clear_then_plant
    try:
        rerr = raises(lambda: asyncio.run(AR.revert_artifact(_P(), 1, 31, "0001")), AR.ArtifactError)
    finally:
        AR._clear_workspace = real_clear
    check("a restore meeting a planted link writes nothing through it",
          not any((WORK / "revert-elsewhere").iterdir()))
    check("the refusal says the workspace was only partly restored",
          rerr is not None and "partly" in str(getattr(rerr, "detail", rerr)), repr(rerr))


for section in (section_a, section_b, section_c, section_d, section_e, section_f, section_g, section_h):
    try:
        section()
    except Exception as e:  # noqa: BLE001 — a crash in one section is a failure, not a stop
        check(f"{section.__name__} ran", False, f"{type(e).__name__}: {e}")

print()
if FAILS:
    print(f"FAIL — {len(FAILS)} check(s): {FAILS}")
    sys.exit(1)
print("PASS — no kernel writer into a project folder writes through a link")
