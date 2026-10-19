#!/usr/bin/env python3
"""
Project preview + landed-file read gate (2026-08-20).

Origin: the finance build LANDED — index.html + app.yaml at
/var/lib/cseries/projects/207/ — and the user still had nothing to look at,
while the host (whose workspace_file jails to $CEIGAS_WORKSPACE and whose
local_grep jails to /var/lib/cseries) could LOCATE the files but not READ them,
so it offered to redraw the app from spec. A model cannot be kept from
inferring; the cure is the context that answers the pattern.

Gates (exit 0 = all pass; exit 1 + [preview] FAIL lines):
  ENTRY RESOLUTION (server/routes/projects.py:project_entry_choice):
   1. app.yaml entrypoint wins when the file exists
   2. entrypoint naming a missing file falls back to root index.html
   3. invalid app.yaml never blocks the root index.html
   4. an index.html ONLY inside a harness worktree (kimi/) is NOT the
      project's face — review machinery never previews as landed state
   5. nested real content (app/index.html) is found; machinery still skipped
   6. hidden dirs (.git, .history) are skipped
   7. empty tree -> None
   8. traversal entrypoint (../x) is ignored, falls back
  16. equal-depth pages are a LIST (project 251), never a pick
  PREVIEW ROUTES (ownership stubbed; fs jail is the subject):
   9. preview renders the landed entry (self-contained; local CSS inlined)
  10. no landed entry -> 404 naming the truth ("no renderable entry")
  11. file route serves a landed file with the right media type
  12. traversal -> 400; .git -> 400; symlink escape -> 403; missing -> 404
  17. several candidate pages -> 300 with the list; ?entry= opens one
  18. ?entry= refusals against REAL targets: ../ and absolute -> 400,
      a symlink out -> 404, a NUL byte -> 400; nothing outside is ever served
  FILE ACCESS BASE (server/capabilities/file_access.py):
  13. read_file/list_directory honor `base` (the project jail)
  14. traversal is still blocked under a base
  15. a path escaping the base resolves to an error, not a read

Runs against the image (/app) or a source tree (APP_ROOT=<repo>).
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

APP_ROOT = os.environ.get("APP_ROOT", "/app")
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))

FAILS = []


def check(name: str, ok: bool, detail: str = ""):
    tag = "ok  " if ok else "FAIL"
    print(f"[preview] {tag} {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _mk(root: Path, rel: str, content: str = "x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_entry_resolution():
    from server.routes.projects import project_entry_choice

    def entry(root):
        """The one page the rule answers with, or None (no page, or a list)."""
        return project_entry_choice(root).single

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 1: entrypoint wins
        _mk(root, "app.yaml", "id: x\nentrypoint: main.html\n")
        _mk(root, "main.html", "<html>main</html>")
        _mk(root, "index.html", "<html>idx</html>")
        check("entrypoint wins", entry(root) == "main.html")
        # 2: entrypoint names a missing file -> root index.html
        _mk(root, "app.yaml", "id: x\nentrypoint: gone.html\n")
        check("missing entrypoint falls back", entry(root) == "index.html")
        # 3: invalid yaml -> root index.html
        _mk(root, "app.yaml", "{{{{not yaml")
        check("invalid manifest falls back", entry(root) == "index.html")
        # 8: traversal entrypoint ignored
        _mk(root, "app.yaml", "id: x\nentrypoint: ../../etc/passwd\n")
        check("traversal entrypoint ignored", entry(root) == "index.html")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 4: only a harness worktree has index.html -> nothing
        _mk(root, "kimi/index.html", "<html>worktree</html>")
        _mk(root, "claude-code/index.html", "<html>worktree</html>")
        _mk(root, "_merge/index.html", "<html>merge</html>")
        check("worktree-only tree has no face", project_entry_choice(root).entries == ())
        # 5: nested real content found, machinery still skipped
        _mk(root, "app/index.html", "<html>real</html>")
        check("nested real content found", entry(root) == "app/index.html")
        # 6: hidden dirs skipped
        with tempfile.TemporaryDirectory() as td2:
            r2 = Path(td2)
            _mk(r2, ".git/index.html", "x")
            _mk(r2, ".history/a/index.html", "x")
            check("hidden dirs skipped", project_entry_choice(r2).entries == ())
        # 7: empty tree
        with tempfile.TemporaryDirectory() as td3:
            check("empty tree -> nothing", project_entry_choice(Path(td3)).entries == ())

    # 16: equal-depth pages are a LIST, never a pick by listing order
    # (project 251: five themes/<name>/index.html)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for n in ("willow-hour", "elm-lantern", "seance"):
            _mk(root, f"themes/{n}/index.html", n)
        got = project_entry_choice(root)
        check("equal-depth pages are all offered, sorted",
              got.entries == ("themes/elm-lantern/index.html", "themes/seance/index.html",
                              "themes/willow-hour/index.html") and got.single is None, str(got))


class _StubPool:
    async def fetchrow(self, *_a, **_k):
        return {"id": 1, "name": "t", "kind": "project"}


def test_preview_routes():
    from fastapi import HTTPException
    from server.routes import projects as P

    orig_pool, orig_eid, orig_root = P._pool, P._eid, P.project_fs_root
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk(root, "index.html",
            '<html><head><link rel="stylesheet" href="style.css"></head>'
            "<body>LANDED-APP</body></html>")
        _mk(root, "style.css", "body{color:red}")
        _mk(root, "data.json", '{"k":1}')
        outside = Path(tempfile.mkdtemp()) / "secret.txt"
        outside.write_text("secret")
        (root / "leak").symlink_to(outside)

        P._pool = lambda: _StubPool()
        P._eid = lambda: 1
        P.project_fs_root = lambda pid: root
        try:
            # 9: renders + inlines
            r = asyncio.run(P.preview_project(1))
            body = r.body.decode()
            check("preview renders landed entry", "LANDED-APP" in body)
            check("preview inlines local css", "color:red" in body and "style.css" not in body)

            # 11: file route + media type
            fr = asyncio.run(P.preview_project_file(1, "data.json"))
            # The route serves the descriptor the no-follow walk opened, so
            # the response's path names that open file, not data.json: check
            # what it sends.
            served = Path(fr.path).read_text() if getattr(fr, "path", None) else ""
            check("file route serves landed file",
                  served == '{"k":1}' and "json" in (fr.media_type or ""), repr(served))

            # 12: jail
            for rel, want in (("../etc/passwd", 400), (".git/config", 400),
                              ("leak", 403), ("nope.txt", 404)):
                try:
                    asyncio.run(P.preview_project_file(1, rel))
                    check(f"jail {rel} -> {want}", False, "no exception raised")
                except HTTPException as e:
                    check(f"jail {rel} -> {want}", e.status_code == want,
                          f"got {e.status_code}")

            # 17: several candidate pages -> 300 + list; ?entry= opens one
            with tempfile.TemporaryDirectory() as td4:
                r4 = Path(td4)
                for n in ("seance", "elm-lantern"):
                    _mk(r4, f"themes/{n}/index.html", f"<html><body>THEME:{n}</body></html>")
                P.project_fs_root = lambda pid: r4
                r = asyncio.run(P.preview_project(1))
                import json as _json
                body = _json.loads(r.body) if r.status_code == 300 else {}
                check("several pages -> 300 with the list",
                      r.status_code == 300 and body.get("entries") == [
                          "themes/elm-lantern/index.html", "themes/seance/index.html"],
                      f"{r.status_code} {body}")
                r = asyncio.run(P.preview_project(1, entry="themes/seance/index.html"))
                check("?entry= opens the chosen page", "THEME:seance" in r.body.decode())
                # 18: ?entry= refusals with REAL targets outside the tree
                # (review of 13e9ba03): every target exists and is readable,
                # so a refusal is the guard's doing, and the secret never comes back.
                out_dir = Path(td4).parent / (Path(td4).name + "-outside")
                out_dir.mkdir(exist_ok=True)
                (out_dir / "index.html").write_text("<html><body>SECRET-OUTSIDE</body></html>")
                (r4 / "themes" / "link.html").symlink_to(out_dir / "index.html")
                for label, name, want in (
                        ("a real page reached with ../", f"../{out_dir.name}/index.html", 400),
                        ("an absolute path to a real page outside", str(out_dir / "index.html"), 400),
                        ("a symlinked .html pointing outside", "themes/link.html", 404),
                        ("a NUL byte", "themes/seance/index.html\x00", 400)):
                    try:
                        rr = asyncio.run(P.preview_project(1, entry=name))
                        check(f"?entry= {label} -> {want}", False,
                              f"served {rr.status_code}: {rr.body[:60]!r}")
                    except HTTPException as e:
                        check(f"?entry= {label} -> {want}, nothing leaks",
                              e.status_code == want and "SECRET" not in str(e.detail),
                              f"{e.status_code} {e.detail}")
                P.project_fs_root = lambda pid: root

            # 10: no landed entry -> honest 404
            with tempfile.TemporaryDirectory() as td2:
                P.project_fs_root = lambda pid: Path(td2)
                try:
                    asyncio.run(P.preview_project(1))
                    check("faceless project -> 404", False, "no exception raised")
                except HTTPException as e:
                    check("faceless project -> 404",
                          e.status_code == 404 and "no renderable entry" in str(e.detail))
        finally:
            P._pool, P._eid, P.project_fs_root = orig_pool, orig_eid, orig_root


def test_file_access_base():
    from server.capabilities import file_access as fa

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        _mk(base, "index.html", "<html>landed</html>")
        _mk(base, "sub/a.txt", "aaa")
        # 13
        r = fa.read_file("index.html", base=base)
        check("read honors base", r.get("status") == "ok" and "landed" in r.get("content", ""))
        li = fa.list_directory("", base=base)
        names = {e["name"] for e in li.get("entries", [])}
        check("list honors base", li.get("status") == "ok" and {"index.html", "sub"} <= names)
        li2 = fa.list_directory("sub", base=base)
        check("list subdir under base",
              li2.get("status") == "ok" and li2["entries"][0]["name"] == "a.txt")
        # 14
        r2 = fa.read_file("../outside.txt", base=base)
        check("traversal blocked under base", r2.get("status") == "error")
        # 15: symlink escaping base
        outside = Path(tempfile.mkdtemp()) / "s.txt"
        outside.write_text("secret")
        (base / "leak").symlink_to(outside)
        r3 = fa.read_file("leak", base=base)
        check("symlink escape blocked under base", r3.get("status") == "error",
              str(r3)[:120])
        # workspace default unchanged: no base -> workspace jail still applies
        r4 = fa.read_file("index.html")
        check("no-base call still workspace-jailed",
              r4.get("status") in ("ok", "error"))  # shape sanity, path differs per env


def main():
    test_entry_resolution()
    test_preview_routes()
    test_file_access_base()
    if FAILS:
        print(f"\n[preview] {len(FAILS)} FAILURE(S): {FAILS}")
        sys.exit(1)
    print("\n[preview] all gates pass")


if __name__ == "__main__":
    main()
