#!/usr/bin/env python3
"""Gate: the artifact render cannot be walked out of the artifact.

server/applications/artifacts.render_workspace joined the caller's `entry`
onto the workspace path unchecked, and GET /api/artifacts/{id}/render?entry=
passes the query straight through, so `entry=../../<file>` returned any file
the kernel could read, HTML or not. The revision render
(GET /api/artifacts/{id}/history/{rev}/render?entry=) had the same join.
Measured on origin/main 2026-09-30: render_workspace(ws, "../not-yours.txt")
returned the outside file's text.

It now goes through server/applications/entry_pages:
  - a named entry is checked by entry_param: a name that could leave the
    tree (a NUL, an absolute path, `..`) is 400 and nothing is read;
  - any other name renders the real file it names, as before; one that
    names no real file (missing, a symlink) falls back to the ONE entry rule
    (entry_pages.choose_entry, the rule every preview lane uses), as a
    missing entry always has fallen back, and never to a symlink;
  - several equal pages and no root is 300 naming them, never a silent pick
    (the old fallback broke that tie by filesystem order).
Only symlinks and escapes are refused. Everything else is pinned to what
the render did before: a named file of any suffix, a real page under a
build tool's dot-directory (.output/public/), which folders each fallback
skips, a process app's entrypoint in the project preview, no fallback
inside a revision snapshot.

Choosing a page and reading it are two moments. Every read on this path (the
page and each inlined asset) goes through entry_pages.read_in_tree, which
opens each component with O_NOFOLLOW from the root down, so a symlink
swapped in after the check is still not followed. The swap is made
deterministic here: the test replaces entry_pages._at_read, the hook at
exactly that moment, and swaps the file, a directory on the way, an asset,
or a FIFO in there. No timing.

The second half drives both render routes over HTTP: the real router and the
real requires_permission dependency, with only the DB row and the projects
root stood in for. The last part does the same for the coder-harness preview
(/api/review/sessions/{id}/preview/{harness}), whose tree is model-written:
an index.html linked out of it is not an entry, and the preview and the
deployment-contract audit read nothing outside it. The two routes that
serve a file whole (/preview/{harness}/{path}, /api/projects/{id}/preview/
{path}) serve from the descriptor the walk opened: a link swapped in at the
open is refused, one swapped in after it changes nothing sent, ranges and
lengths are unchanged, and no descriptor is left open. The artifact list's
desktop flag and the project preview's app.yaml read no link either (each
would leak one bit about a file outside).

Run: APP_ROOT=<repo> python3 scripts/test_artifact_render_entry.py
"""
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

from server.applications.artifacts import ArtifactError, render_workspace  # noqa: E402

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


def render(ws: Path, entry=None):
    """(status, text): 200 + the document, or the ArtifactError's status + detail."""
    try:
        return 200, (render_workspace(ws) if entry is None else render_workspace(ws, entry))
    except ArtifactError as e:
        return e.status, str(e.detail)


def mk(root: Path, files: dict) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


# Enough `..` to reach / from any temp directory: the reported shape of the hole.
_ENVIRON = "../" * 24 + "proc/self/environ"


with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    secret = base / "not-yours.txt"
    secret.write_text("SECRET-FROM-OUTSIDE")
    outside_page = base / "outside" / "index.html"
    outside_page.parent.mkdir()
    outside_page.write_text("<html><body>SECRET-OUTSIDE-PAGE</body></html>")

    # ── the rule ────────────────────────────────────────────────────────────
    ws = mk(base / "a1", {"index.html": "<html><body>ROOT</body></html>",
                          "variant.html": "<html><body>VARIANT</body></html>",
                          ".history/r1/index.html": "<html><body>OLD</body></html>"})
    code, out = render(ws)
    check("the root index.html renders", code == 200 and "ROOT" in out, f"{code} {out[:80]}")
    code, out = render(ws, "variant.html")
    check("a named page still renders (a variant)", code == 200 and "VARIANT" in out, f"{code} {out[:80]}")
    code, out = render(ws, "gone.html")
    check("a missing named entry falls back to the rule, as it always has",
          code == 200 and "ROOT" in out, f"{code} {out[:80]}")
    mk(ws, {"notes.txt": "NOTES-IN-THE-TREE", "folder.html/index.html": "<p>IN-FOLDER</p>",
            ".output/public/index.html": "<p>DOT-FOLDER-PAGE</p>"})
    code, out = render(ws, "notes.txt")
    check("a named real file inside renders whatever its suffix, as before",
          code == 200 and "NOTES-IN-THE-TREE" in out, f"{code} {out[:80]}")
    code, out = render(ws, ".output/public/index.html")
    check("a named real page under a dot-directory renders, as before",
          code == 200 and "DOT-FOLDER-PAGE" in out, f"{code} {out[:80]}")
    code, out = render(ws, "folder.html")
    check("a directory named like a page is not a page (the rule's page renders)",
          code == 200 and "ROOT" in out, f"{code} {out[:80]}")

    ws = mk(base / "a2", {"app/index.html": "<html><body>NESTED</body></html>",
                          ".history/r1/index.html": "<html><body>OLD</body></html>"})
    code, out = render(ws)
    check("no root: the one nested page renders (history is never a page)",
          code == 200 and "NESTED" in out, f"{code} {out[:80]}")
    ws = mk(base / "a2h", {"x/y/z/index.html": "<p>DEEP-PAGE</p>",
                           ".history/r1/index.html": "<p>OLD-SNAPSHOT</p>"})
    code, out = render(ws)
    check("history is never a page, even when it is the shallowest index.html",
          code == 200 and "DEEP-PAGE" in out, f"{code} {out[:80]}")
    ws = mk(base / "a2d", {".output/public/index.html": "<p>BUILT-OUTPUT</p>",
                           ".git/index.html": "<p>GIT-STORE</p>"})
    code, out = render(ws)
    check("no root: a real page under a build tool's dot-directory is found, as before (.git never)",
          code == 200 and "BUILT-OUTPUT" in out, f"{code} {out[:80]}")
    ws = base / "a2s"
    (ws / ".output" / "public").mkdir(parents=True)
    (ws / ".output" / "public" / "index.html").symlink_to(outside_page)
    code, out = render(ws)
    check("the same dot-directory page as a link out is never read (404)",
          code == 404 and "SECRET" not in out, f"{code} {out[:80]}")

    ws = mk(base / "a3", {"b/index.html": "<p>PAGE-B</p>", "a/index.html": "<p>PAGE-A</p>",
                          "a/deeper/index.html": "<p>PAGE-DEEP</p>"})
    code, out = render(ws)
    check("no root, two equal shallowest pages: 300 naming both, never a silent pick",
          code == 300 and "a/index.html" in out and "b/index.html" in out
          and "deeper" not in out and "PAGE-" not in out, f"{code} {out[:120]}")
    code, out = render(ws, "a/index.html")
    check("...and naming one of them opens it", code == 200 and "PAGE-A" in out,
          f"{code} {out[:80]}")
    # The route answers that 300 in the preview routes' shape, so the Artifacts
    # window can offer the pages (scripts/e2e_artifacts_page_list.py drives it).
    try:
        render_workspace(ws)
        err = None
    except ArtifactError as e:
        err = e
    import json as _json
    from server.routes.artifacts import _render_refusal
    resp = _render_refusal(err) if err else None
    body = _json.loads(resp.body) if resp is not None else {}
    check("the render route answers it as 300 {detail, entries, basis}",
          resp is not None and resp.status_code == 300
          and body.get("entries") == ["a/index.html", "b/index.html"] and body.get("basis") == "tree"
          and "choose one" in body.get("detail", ""), f"{getattr(resp, 'status_code', None)} {body}")

    # ── the jail: every target below EXISTS and is readable ────────────────
    ws = mk(base / "a4", {"index.html": "<html><body>ROOT</body></html>"})
    code, out = render(ws, "../not-yours.txt")
    check("entry=../not-yours.txt (a real file outside) is refused (400), nothing read",
          code == 400 and "SECRET" not in out, f"{code} {out[:80]}")
    code, out = render(ws, "../outside/index.html")
    check("entry=../outside/index.html (a real page outside) is refused (400)",
          code == 400 and "SECRET" not in out, f"{code} {out[:80]}")
    code, out = render(ws, str(outside_page))
    check("an absolute path to a real page outside is refused (400)",
          code == 400 and "SECRET" not in out, f"{code} {out[:80]}")
    code, out = render(ws, _ENVIRON)
    check("entry=../../…/proc/self/environ is refused (400), nothing read",
          code == 400 and "PATH=" not in out, f"{code} {out[:80]}")
    (ws / "link.html").symlink_to(outside_page)
    code, out = render(ws, "link.html")
    check("a symlinked page pointing outside is never read (the rule's page renders)",
          code == 200 and "SECRET" not in out and "ROOT" in out, f"{code} {out[:80]}")
    code, out = render(ws, "index.html\x00")
    check("a NUL byte in the entry is refused (400), not a crash", code == 400, f"{code} {out[:80]}")

    # A workspace whose ONLY index.html is a link out has no page at all.
    ws = base / "a5"
    ws.mkdir()
    (ws / "index.html").symlink_to(outside_page)
    code, out = render(ws)
    check("a workspace whose index.html links out has nothing to render (404)",
          code == 404 and "SECRET" not in out, f"{code} {out[:80]}")
    mk(ws, {"app/index.html": "<p>REAL-APP-PAGE</p>"})
    code, out = render(ws)
    check("...and with a real page deeper down, the fallback opens that one, never the link",
          code == 200 and "REAL-APP-PAGE" in out and "SECRET" not in out, f"{code} {out[:80]}")

    # ── each guard in page_in_tree, isolated ──────────────────────────────
    # Through render_workspace, entry_param refuses `..`, absolute names and
    # NULs first, and the resolve-escape check backs up the symlink guard, so
    # those guards can cover for each other there. Each case below is refused
    # by exactly ONE guard of page_in_tree, so deleting it fails its check.
    try:
        from server.applications.entry_pages import page_in_tree
    except ImportError as e:
        check("the shared entry rule exists (server/applications/entry_pages)", False, repr(e))
    else:
        g = mk(base / "guards", {"index.html": "root", "a/index.html": "a",
                                 ".dot/index.html": "dot"})
        (g / "in.html").symlink_to(g / "a" / "index.html")   # a link that stays INSIDE
        check("rule: `..` is refused even when it lands back inside the tree",
              page_in_tree(g, "a/../index.html") is None)
        check("rule: a dot-directory is not refused by the rule itself (callers skip their own)",
              page_in_tree(g, ".dot/index.html") == ".dot/index.html")
        check("rule: an absolute path is refused even when it points inside the tree",
              page_in_tree(g, str(g / "a" / "index.html")) is None)
        check("rule: a symlink is refused even when it points inside the tree",
              page_in_tree(g, "in.html") is None)
        try:
            got = page_in_tree(g, "a\x00.html")
            check("rule: a NUL byte is refused, not raised", got is None, repr(got))
        except Exception as e:  # noqa: BLE001
            check("rule: a NUL byte is refused, not raised", False, f"raised {e!r}")
        check("rule: a plain page inside is accepted (the guards do not refuse everything)",
              page_in_tree(g, "a/index.html") == "a/index.html")

    # ── inlined assets: the reader is their only guard ────────────────────
    (base / "secret.css").write_text("SECRET-CSS")
    ws = mk(base / "as1", {"index.html": '<link rel="stylesheet" href="style.css">'
                                         '<script src="app.js"></script><p>PAGE</p>',
                           "style.css": "body{color:#123}", "app.js": "var OK_ASSET=1;"})
    code, out = render(ws)
    check("local assets are inlined (the reader reads real files)",
          code == 200 and "<style>body{color:#123}</style>" in out and "OK_ASSET" in out,
          f"{code} {out[:120]}")
    ws = mk(base / "as2", {"index.html": '<link rel="stylesheet" href="../secret.css">'
                                         f'<link rel="stylesheet" href="{base}/secret.css">'
                                         '<link rel="stylesheet" href="linked.css"><p>PAGE</p>'})
    (ws / "linked.css").symlink_to(base / "secret.css")
    code, out = render(ws)
    check("an asset reached by `..`, by an absolute path or through a link is never inlined",
          code == 200 and "PAGE" in out and "SECRET" not in out, f"{code} {out[:160]}")

    # ── swapped after the check: the read still does not follow ───────────
    class _Hung(Exception):
        """Raised by the alarm when a read blocks. Not an OSError, so the
        reader cannot swallow it."""

    def _alarm(signum, frame):
        raise _Hung()

    try:
        from server.applications import entry_pages as EP
        _hook = hasattr(EP, "_at_read") and hasattr(EP, "read_in_tree")
    except ImportError:
        EP, _hook = None, False
    check("the no-follow reader and its check-to-read hook exist (entry_pages)", _hook)

    def swapped(call, when: tuple, action):
        """Run `call()` with `action()` done once, at the moment between the
        choice and the open of the file whose path parts are `when`.
        (result, whether the swap happened)."""
        noop, fired = EP._at_read, []

        def hook(root, parts):
            if tuple(parts) == when and not fired:
                fired.append(1)
                action()

        EP._at_read = hook
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(10)
        try:
            return call(), bool(fired)
        except _Hung:
            return (-1, "the read blocked"), bool(fired)
        finally:
            signal.alarm(0)
            EP._at_read = noop

    def relink(p: Path, to: Path):
        return lambda: (p.unlink(), p.symlink_to(to))

    if _hook:
        ws = mk(base / "sw1", {"index.html": "<p>REAL-PAGE</p>"})
        (code, out), fired = swapped(lambda: render(ws), ("index.html",),
                                     relink(ws / "index.html", outside_page))
        check("the page swapped for a link after the check is not read (404)",
              fired and code == 404 and "SECRET" not in out, f"fired={fired} {code} {out[:80]}")

        ws = mk(base / "sw2", {"app/index.html": "<p>REAL-APP</p>"})
        (code, out), fired = swapped(
            lambda: render(ws, "app/index.html"), ("app", "index.html"),
            lambda: ((ws / "app").rename(ws / "app-moved"),
                     (ws / "app").symlink_to(outside_page.parent)))
        check("a directory swapped for a link after the check is not walked through (404)",
              fired and code == 404 and "SECRET" not in out, f"fired={fired} {code} {out[:80]}")

        ws = mk(base / "sw3", {"index.html": '<link rel="stylesheet" href="style.css"><p>PAGE-3</p>',
                               "style.css": "body{}"})
        (code, out), fired = swapped(lambda: render(ws), ("style.css",),
                                     relink(ws / "style.css", secret))
        check("an asset swapped for a link after the check is not inlined",
              fired and code == 200 and "PAGE-3" in out and "SECRET" not in out,
              f"fired={fired} {code} {out[:80]}")

        ws = mk(base / "sw4", {"index.html": "<p>REAL-PAGE</p>"})
        (code, out), fired = swapped(
            lambda: render(ws), ("index.html",),
            lambda: ((ws / "index.html").unlink(), os.mkfifo(ws / "index.html")))
        check("a FIFO swapped in after the check is refused, and the read never blocks (404)",
              fired and code == 404, f"fired={fired} {code} {out[:80]}")

    # ── the routes, over HTTP ──────────────────────────────────────────────
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import server.brain_service as BS
    import server.routes.artifacts as R
    from server.applications import artifacts as A
    from server.applications.tokens import AppToken

    AID = 41
    projects = base / "projects"
    live = mk(projects / str(AID), {"index.html": "<html><body>LIVE-PAGE</body></html>",
                                     ".history/0001/index.html": "<html><body>REV-PAGE</body></html>"})
    (live / "link.html").symlink_to(outside_page)
    (live / ".history" / "0001" / "link.html").symlink_to(outside_page)

    class _Pool:
        """The one DB read these routes make: the artifact row, this entity's."""
        async def fetchrow(self, q, *a):
            if "FROM projects" in q and a and int(a[0]) == AID:
                return {"id": AID, "kind": "artifact"}
            return None

        async def fetch(self, q, *a):
            return []

        async def fetchval(self, q, *a):
            return None

    BS.get_pool = lambda: _Pool()
    BS._entity_id = 9003
    A._projects_root = lambda: projects

    def _app(perms, router=R.router):
        app = FastAPI()

        @app.middleware("http")
        async def _token(request, call_next):
            request.state.app_token = AppToken(app_id="gate", window_id="w1", iat=0,
                                               permissions=perms)
            return await call_next(request)

        app.include_router(router)
        return TestClient(app, raise_server_exceptions=False)

    c = _app(["read:projects"])
    r = _app([]).get(f"/api/artifacts/{AID}/render")
    check("the scope gate is live: no read:projects is 403", r.status_code == 403,
          f"{r.status_code} {r.text[:80]}")

    # (route, what the default page says, depth from its root to `base`)
    lanes = ((f"/api/artifacts/{AID}/render", "LIVE-PAGE", 2),
             (f"/api/artifacts/{AID}/history/0001/render", "REV-PAGE", 4))
    for url, page, depth in lanes:
        name = "render" if "history" not in url else "history/{rev}/render"
        r = c.get(url)
        check(f"{name}: the page renders", r.status_code == 200 and page in r.text,
              f"{r.status_code} {r.text[:80]}")
        r = c.get(url, params={"entry": "../" * depth + "not-yours.txt"})
        check(f"{name}: ?entry=../…/not-yours.txt is 400, nothing read",
              r.status_code == 400 and "SECRET" not in r.text, f"{r.status_code} {r.text[:80]}")
        r = c.get(url, params={"entry": _ENVIRON})
        check(f"{name}: ?entry=../…/proc/self/environ is 400, nothing read",
              r.status_code == 400 and "PATH=" not in r.text, f"{r.status_code} {r.text[:80]}")
        r = c.get(url, params={"entry": str(outside_page)})
        check(f"{name}: an absolute ?entry= is 400, nothing read",
              r.status_code == 400 and "SECRET" not in r.text, f"{r.status_code} {r.text[:80]}")
        r = c.get(url, params={"entry": "index.html\x00"})
        check(f"{name}: a NUL in ?entry= is 400", r.status_code == 400,
              f"{r.status_code} {r.text[:80]}")
        r = c.get(url, params={"entry": "link.html"})
        if "history" not in url:
            check(f"{name}: a symlinked page is never read (the rule's page renders)",
                  r.status_code == 200 and "SECRET" not in r.text and page in r.text,
                  f"{r.status_code} {r.text[:80]}")
        else:
            check(f"{name}: a symlinked page is never read (a revision has no fallback: 404, as before)",
                  r.status_code == 404 and "SECRET" not in r.text, f"{r.status_code} {r.text[:80]}")

    # ── the desktop flag in the artifact list reads no link ───────────────
    desk = '<meta name="cseries-desktop" content="desktop"><p>DESK</p>'
    (base / "outside-desk.html").write_text(desk)
    mk(projects / "43", {"index.html": desk})
    (projects / "42").mkdir()
    (projects / "42" / "index.html").symlink_to(base / "outside-desk.html")
    check("desktop flag: a real desktop page inside is a desktop", A._entry_is_desktop(43) is True)
    check("desktop flag: an index.html linked to a desktop page outside answers no (no bit leaks)",
          A._entry_is_desktop(42) is False)
    check("desktop flag: an ordinary page is not a desktop", A._entry_is_desktop(AID) is False)

    # ── the project preview's entry (projects.project_entry_choice) ───────
    from server.routes.projects import project_entry_choice

    def resolve_project_entry(root):
        """The page the project preview opens without asking, or None."""
        return project_entry_choice(root).default

    pj = mk(base / "pj1", {"app.yaml": "id: x\nentrypoint: main.py\n",
                           "main.py": "print('SOURCE')", "kimi/index.html": "<p>UNLANDED-RUN</p>"})
    got = resolve_project_entry(pj)
    check("project preview: a process app's real entrypoint is the entry, as before",
          got == "main.py", repr(got))
    pj = mk(base / "pj1b", {"kimi/index.html": "<p>UNLANDED-RUN</p>",
                            ".pre-639-backup/index.html": "<p>BACKUP</p>",
                            "web/index.html": "<p>REAL-FACE</p>"})
    got = resolve_project_entry(pj)
    check("project preview: review machinery and dot-directories are skipped in the tree, as before",
          got == "web/index.html", repr(got))
    pj = mk(base / "pj2", {"app.yaml": "id: x\nentrypoint: link.html\n", "app/index.html": "<p>FACE</p>"})
    (pj / "link.html").symlink_to(outside_page)
    (pj / "index.html").symlink_to(outside_page)
    got = resolve_project_entry(pj)
    code, out = render(pj, got) if got else (0, "")
    check("project preview: an entrypoint or index.html linked outside is not an entry, nothing read",
          got == "app/index.html" and "SECRET" not in out, f"{got!r} {code} {out[:60]}")
    (base / "outside-app.yaml").write_text("id: x\nentrypoint: picked-from-outside.html\n")
    pj = mk(base / "pj3", {"picked-from-outside.html": "<p>PICKED</p>", "index.html": "<p>FACE</p>"})
    (pj / "app.yaml").symlink_to(base / "outside-app.yaml")
    got = resolve_project_entry(pj)
    check("project preview: an app.yaml linked outside is not read (no bit leaks)",
          got == "index.html", repr(got))

    # ── the coder-harness preview: a model wrote this tree ────────────────
    import server.routes.review as RV

    work = base / "work"
    mk(work / "kimi", {"index.html": '<script src="app.js"></script><p>KIMI-PAGE</p>',
                       "app.js": "var KIMI_ASSET=1;"})
    (work / "codex").mkdir(parents=True)
    (work / "codex" / "index.html").symlink_to(outside_page)          # the page links out
    mk(work / "grok", {"index.html": '<script src="evil.js"></script><p>GROK-PAGE</p>'})
    (work / "grok" / "evil.js").symlink_to(secret)                      # an asset links out
    mk(work / "cursor", {"index.html": "<p>CURSOR-PAGE</p>"})
    mk(work / "antigravity", {".output/public/index.html": "<p>BUILT-BY-TOOL</p>"})   # real, dot-dir
    (work / "kimi-s2" / ".output" / "public").mkdir(parents=True)
    (work / "kimi-s2" / ".output" / "public" / "index.html").symlink_to(outside_page)  # linked, dot-dir
    runs = ["kimi", "codex", "grok", "cursor", "antigravity", "kimi-s2"]
    manifest = {"worktree_root": str(work), "harnesses": ["kimi", "codex", "grok", "cursor", "antigravity"],
                "variants": [{"vid": v} for v in runs]}
    (base / "sessions" / "s1").mkdir(parents=True)
    (base / "sessions" / "s1" / "manifest.json").write_text(json.dumps(manifest))
    RV.SESSIONS_ROOT = base / "sessions"
    hc = _app(["read:review"], RV.router)

    r = hc.get("/api/review/sessions/s1/preview/kimi")
    check("harness preview: a real page renders with its asset inlined",
          r.status_code == 200 and "KIMI-PAGE" in r.text and "KIMI_ASSET" in r.text,
          f"{r.status_code} {r.text[:80]}")
    r = hc.get("/api/review/sessions/s1/preview/codex")
    check("harness preview: an index.html linked outside the tree is refused (404), nothing read",
          r.status_code == 404 and "SECRET" not in r.text, f"{r.status_code} {r.text[:80]}")
    check("harness preview: that run is not offered as openable",
          RV._session_entrypoints(manifest).get("codex") is False,
          str(RV._session_entrypoints(manifest)))
    fatal, warn = RV._contract_violations(work / "codex")
    check("harness preview: the contract audit finds no entry there and reads nothing outside",
          bool(fatal) and "SECRET" not in " ".join(fatal + warn), f"{fatal} {warn}")
    r = hc.get("/api/review/sessions/s1/preview/grok")
    check("harness preview: an asset linked outside the tree is not inlined",
          r.status_code == 200 and "GROK-PAGE" in r.text and "SECRET" not in r.text,
          f"{r.status_code} {r.text[:80]}")
    r = hc.get("/api/review/sessions/s1/preview/antigravity")
    fatal, warn = RV._contract_violations(work / "antigravity")
    eps = RV._session_entrypoints(manifest)
    check("harness preview: a run whose page is real under .output/public/ previews, is openable "
          "and lands with the nested warning, as before",
          r.status_code == 200 and "BUILT-BY-TOOL" in r.text and eps.get("antigravity") is True
          and not fatal and any("nested" in w for w in warn), f"{r.status_code} {eps} {fatal} {warn}")
    r = hc.get("/api/review/sessions/s1/preview/kimi-s2")
    fatal, warn = RV._contract_violations(work / "kimi-s2")
    check("harness preview: the same page as a link out never previews, is not openable, never lands",
          r.status_code == 404 and "SECRET" not in r.text and eps.get("kimi-s2") is False and bool(fatal),
          f"{r.status_code} {eps.get('kimi-s2')} {fatal}")

    # ── files served whole: from the descriptor the walk opened ──────────
    import server.routes.applications as APPS
    import server.routes.projects as PJ
    APPS.PROJECTS_ROOT = projects
    pc = _app(["read:projects"], PJ.router)
    mk(work / "kimi", {"swap1.js": "var REAL1=1;", "swap2.js": "var REAL2=1;"})
    mk(projects / str(AID), {"assets/site.css": "body{REAL-CSS}", "assets/swap1.css": "R1",
                             "assets/swap2.css": "REAL-CSS-2"})
    # (route name, client, url of a real file, its text, url of a link out,
    #  a file to swap at the open, a file to swap after the open)
    file_lanes = (
        ("harness file", hc, "/api/review/sessions/s1/preview/kimi/app.js", "var KIMI_ASSET=1;",
         "/api/review/sessions/s1/preview/grok/evil.js",
         ("/api/review/sessions/s1/preview/kimi/swap1.js", work / "kimi" / "swap1.js", ("swap1.js",)),
         ("/api/review/sessions/s1/preview/kimi/swap2.js", work / "kimi" / "swap2.js", ("swap2.js",),
          "var REAL2=1;")),
        ("project file", pc, f"/api/projects/{AID}/preview/assets/site.css", "body{REAL-CSS}",
         f"/api/projects/{AID}/preview/link.html",
         (f"/api/projects/{AID}/preview/assets/swap1.css", projects / str(AID) / "assets" / "swap1.css",
          ("assets", "swap1.css")),
         (f"/api/projects/{AID}/preview/assets/swap2.css", projects / str(AID) / "assets" / "swap2.css",
          ("assets", "swap2.css"), "REAL-CSS-2")),
    )

    def after_open(call, when: tuple, action):
        """Run `call()` with `action()` done once, right after open_in_tree
        opened the file whose path parts are `when` and before anything is
        sent. (result, whether the swap happened)."""
        real, fired = EP.open_in_tree, []

        def wrapped(root, rel):
            fd = real(root, rel)
            if fd is not None and tuple(Path(str(rel)).parts) == when and not fired:
                fired.append(1)
                action()
            return fd

        EP.open_in_tree = wrapped
        try:
            return call(), bool(fired)
        finally:
            EP.open_in_tree = real

    for name, cl, url, text, link_url, at_open, post_open in file_lanes:
        r = cl.get(url)
        check(f"{name}: a real file is served, with its length",
              r.status_code == 200 and r.text == text
              and r.headers.get("content-length") == str(len(text)), f"{r.status_code} {r.text[:60]}")
        r = cl.get(url, headers={"Range": "bytes=0-3"})
        check(f"{name}: byte ranges still work", r.status_code == 206 and r.text == text[:4],
              f"{r.status_code} {r.text[:60]}")
        r = cl.get(link_url)
        check(f"{name}: a file linked outside is refused (403), nothing sent",
              r.status_code == 403 and "SECRET" not in r.text, f"{r.status_code} {r.text[:60]}")
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(5):
            cl.get(url)
            cl.get(url, headers={"Range": "bytes=0-3"})
        after = len(os.listdir("/proc/self/fd"))
        check(f"{name}: no descriptor is left open after the responses", after <= before,
              f"{before} -> {after}")
        if _hook:
            u, p, parts = at_open
            (r, fired) = swapped(lambda: cl.get(u), parts, relink(p, secret))
            check(f"{name}: a file swapped for a link between the checks and the open is refused (403)",
                  fired and getattr(r, "status_code", None) == 403 and "SECRET" not in getattr(r, "text", ""),
                  f"fired={fired} {getattr(r, 'status_code', r)} {getattr(r, 'text', '')[:60]}")
            u, p, parts, real_text = post_open
            (r, fired) = after_open(lambda: cl.get(u), parts, relink(p, secret))
            check(f"{name}: a link swapped in after the open changes nothing sent",
                  fired and getattr(r, "status_code", None) == 200 and getattr(r, "text", "") == real_text,
                  f"fired={fired} {getattr(r, 'status_code', r)} {getattr(r, 'text', '')[:60]}")
    if _hook:
        (r, fired) = swapped(lambda: hc.get("/api/review/sessions/s1/preview/cursor"),
                             ("index.html",), relink(work / "cursor" / "index.html", outside_page))
        check("harness preview: the page swapped for a link after the check is not read (404)",
              fired and getattr(r, "status_code", None) == 404 and "SECRET" not in getattr(r, "text", ""),
              f"fired={fired} {getattr(r, 'status_code', r)} {getattr(r, 'text', '')[:60]}")
        # The audit names uncompiled sources it finds in the page, so a page
        # read from outside would put SECRET-REF.tsx into its findings.
        audit_bait = base / "outside2" / "index.html"
        mk(audit_bait.parent, {"index.html": '<script src="SECRET-REF.tsx"></script>'})
        mk(work / "opencode", {"index.html": "<p>AUDITED</p>"})
        (fv, fired) = swapped(lambda: RV._contract_violations(work / "opencode"),
                              ("index.html",), relink(work / "opencode" / "index.html", audit_bait))
        fatal, warn = fv
        check("contract audit: the page swapped for a link after the check is not read",
              fired and bool(fatal) and "SECRET" not in " ".join(fatal + warn),
              f"fired={fired} {fatal} {warn}")

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("all gates passed")
