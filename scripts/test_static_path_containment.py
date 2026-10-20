#!/usr/bin/env python3
"""Gate: the dashboard overlay never serves a file from outside custom/.

Container-free. Reproduces the escape through the real ASGI path (starlette
TestClient, percent-encoded '..') against a StaticFiles subclass built the
way server/main.py's OverlayStaticFiles joins its overlay directory, first
the old way (must leak, proving the test can see the hole) and then through
server.static_paths.inside (must not). Then the two other places that serve a
file from a tree something else can write: an installed app's directory index
(applications/proxy.py) and the custom tokens.css (routes/appearance.py
base_tokens_css), each through the real function."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain")]

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import FileResponse  # noqa: E402
from starlette.routing import Mount  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from server import static_paths  # noqa: E402

FAILED = 0


def check(name, ok):
    global FAILED
    print(("ok    " if ok else "FAIL  ") + name)
    FAILED += 0 if ok else 1


def app_for(root: Path, contained: bool):
    class Overlay(StaticFiles):
        async def get_response(self, path, scope):
            if contained:
                cp = static_paths.inside(root / "custom", path)
                hit = cp is not None and cp.is_file()
            else:
                cp = root / "custom" / path
                hit = cp.is_file()
            if hit:
                return FileResponse(str(cp))
            return await super().get_response(path, scope)
    return Starlette(routes=[Mount("/", app=Overlay(directory=str(root / "base")))])


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    (root / "base").mkdir()
    (root / "custom").mkdir()
    (root / "outside.txt").write_text("OUTSIDE")
    (root / "base" / "a.txt").write_text("base")
    (root / "custom" / "a.txt").write_text("custom")
    os.symlink(root / "outside.txt", root / "custom" / "link.txt")
    escapes = ["/%2e%2e/outside.txt", "/..%2foutside.txt", "/x/%2e%2e/%2e%2e/outside.txt", "/link.txt"]

    old = TestClient(app_for(root, contained=False))
    check("the old join leaks through a percent-encoded '..' (the test sees the hole)",
          old.get("/%2e%2e/outside.txt").text == "OUTSIDE")

    new = TestClient(app_for(root, contained=True))
    check("custom/ still overrides base/", new.get("/a.txt").text == "custom")
    for p in escapes:
        check(f"no escape for {p}", "OUTSIDE" not in new.get(p).text)

    check("inside() refuses '..'", static_paths.inside(root / "custom", "../outside.txt") is None)
    check("inside() refuses a symlink out", static_paths.inside(root / "custom", "link.txt") is None)
    check("inside() keeps a normal file", static_paths.inside(root / "custom", "a.txt") == (root / "custom" / "a.txt").resolve())
    check("inside() refuses an absolute path", static_paths.inside(root / "custom", str(root / "outside.txt")) is None)
    check("inside() refuses a NUL", static_paths.inside(root / "custom", "a\x00.txt") is None)
    (root / "custom2").mkdir()
    (root / "custom2" / "x.txt").write_text("SIBLING")
    check("inside() refuses a sibling that shares the prefix",
          static_paths.inside(root / "custom", "../custom2/x.txt") is None)

    # Legitimate overlay files still serve: nested directories, and a symlink
    # that stays inside custom/.
    (root / "custom" / "js" / "apps").mkdir(parents=True)
    (root / "custom" / "js" / "apps" / "n.js").write_text("nested")
    os.symlink(root / "custom" / "js" / "apps" / "n.js", root / "custom" / "alias.js")
    check("a nested custom/ file serves", new.get("/js/apps/n.js").text == "nested")
    check("a symlink inside custom/ serves", new.get("/alias.js").text == "nested")

    # The same escape one level down, through the real app file server: the
    # install dir is checked, then a directory's index.html is appended and
    # served. That index.html can itself link out of the install dir.
    from server.applications import proxy as P
    from fastapi import HTTPException
    app_dir = root / "apps" / "demo"
    (app_dir / "out").mkdir(parents=True)
    (app_dir / "in").mkdir()
    (app_dir / "app.yaml").write_text("id: demo\nprovenance: native\n")
    (app_dir / "in" / "index.html").write_text("<p>in</p>")
    os.symlink(root / "outside.txt", app_dir / "out" / "index.html")
    check("_has_static refuses a directory whose index.html links out", P._has_static(app_dir, "out") is False)
    try:
        r = P._serve_static(app_dir, "out", "demo")
        leaked = b"OUTSIDE" in getattr(r, "body", b"")
        check("_serve_static refuses a directory whose index.html links out", not leaked)
    except HTTPException as e:
        check("_serve_static refuses a directory whose index.html links out", e.status_code in (403, 404))
    check("_has_static keeps a directory's own index.html", P._has_static(app_dir, "in") is True)
    check("_serve_static serves a directory's own index.html",
          b"<p>in</p>" in P._serve_static(app_dir, "in", "demo").body)
    check("_has_static refuses '..'", P._has_static(app_dir, "../../outside.txt") is False)

    # /css/tokens.css is answered before the overlay's join (main.py), from
    # appearance.base_tokens_css, which reads custom/ itself.
    import types
    (root / "base" / "css").mkdir()
    (root / "base" / "css" / "tokens.css").write_text("BASE-TOKENS")
    (root / "custom" / "css").mkdir()
    os.symlink(root / "outside.txt", root / "custom" / "css" / "tokens.css")
    fake_main = types.ModuleType("server.main")
    fake_main.DASHBOARD_BASE = root / "base"
    fake_main.DASHBOARD_CUSTOM = root / "custom"
    sys.modules["server.main"] = fake_main
    from server.routes import appearance as A
    check("tokens.css: a custom copy that links out is not served", A.base_tokens_css() == "BASE-TOKENS")
    os.unlink(root / "custom" / "css" / "tokens.css")
    (root / "custom" / "css" / "tokens.css").write_text("CUSTOM-TOKENS")
    check("tokens.css: a real custom copy still wins", A.base_tokens_css() == "CUSTOM-TOKENS")
    del sys.modules["server.main"]

    # The same tokens.css through the REAL OverlayStaticFiles behind the REAL
    # AppOriginGuard. The guard admits /css/tokens.css on an app's own origin
    # (app_origin._SHARED_FILE_RE) and marks the scope; the overlay then must
    # not serve custom/'s copy there. The desktop keeps the custom copy,
    # and the design override (an inert, whitelisted grammar meant for every
    # app) is appended on both.
    import starlette.staticfiles as _sf
    from server.applications import app_origin as AO
    from server.middleware.app_origin_guard import AppOriginGuard
    _orig_init = _sf.StaticFiles.__init__

    def _no_dir_check(self, *a, **k):
        k["check_dir"] = False
        return _orig_init(self, *a, **k)
    _sf.StaticFiles.__init__ = _no_dir_check
    try:
        import server.main as M
    finally:
        _sf.StaticFiles.__init__ = _orig_init
    M.DASHBOARD_BASE, M.DASHBOARD_CUSTOM = root / "base", root / "custom"
    _real_design = A.design_tokens_css

    async def _design():
        return "/*DESIGN*/"
    A.design_tokens_css = _design
    try:
        shell = TestClient(AppOriginGuard(Starlette(routes=[Mount("/", app=M.OverlayStaticFiles())])))
        app_host = f"apps-{AO.origin_key('settings')}-0123456789abcdef.cseries.localhost"
        desk_host = "0123456789abcdef.cseries.localhost"
        on_app = shell.get("/css/tokens.css", headers={"host": app_host})
        on_desk = shell.get("/css/tokens.css", headers={"host": desk_host})
        check("tokens.css on an app origin is admitted by the guard", on_app.status_code == 200)
        check("tokens.css on an app origin is the image's copy, never custom/'s",
              "BASE-TOKENS" in on_app.text and "CUSTOM-TOKENS" not in on_app.text)
        check("tokens.css on an app origin keeps the desktop's design override",
              on_app.text.endswith("/*DESIGN*/"))
        check("tokens.css on the desktop still serves custom/'s copy",
              on_desk.status_code == 200 and "CUSTOM-TOKENS" in on_desk.text
              and on_desk.text.endswith("/*DESIGN*/"))
        (root / "custom" / "css" / "other.css").write_text("CUSTOM-OTHER")
        (root / "base" / "css" / "other.css").write_text("BASE-OTHER")
        check("another shared stylesheet on an app origin is the image's copy",
              shell.get("/css/other.css", headers={"host": app_host}).text == "BASE-OTHER")
    finally:
        A.design_tokens_css = _real_design

    # The desktop render check inlines a fixed list of shell scripts into the
    # page its browser loads (desktop_render._mount_script), custom/ first.
    # A custom copy that links out of custom/ must not be read; the base copy
    # is used instead, as the HTTP overlay would serve it.
    from server.applications import desktop_render as DR
    (root / "base" / "js").mkdir()
    (root / "base" / "js" / "sandbox-csp.js").write_text("BASE-SCRIPT")
    (root / "custom" / "js" / "desktop-preview.js").write_text("CUSTOM-SCRIPT")
    (root / "base" / "js" / "desktop-preview.js").write_text("BASE-PREVIEW")
    os.symlink(root / "outside.txt", root / "custom" / "js" / "sandbox-csp.js")
    _render_main = types.ModuleType("server.main")
    _render_main.DASHBOARD_BASE = root / "base"
    _render_main.DASHBOARD_CUSTOM = root / "custom"
    _saved_main = sys.modules.get("server.main")
    sys.modules["server.main"] = _render_main
    try:
        check("_mount_script does not read a custom/ script that links out",
              DR._mount_script("js/sandbox-csp.js") == "BASE-SCRIPT")
        check("_mount_script still prefers a real custom/ script",
              DR._mount_script("js/desktop-preview.js") == "CUSTOM-SCRIPT")
    finally:
        if _saved_main is not None:
            sys.modules["server.main"] = _saved_main
        else:
            del sys.modules["server.main"]

main_src = (Path(__file__).resolve().parents[1] / "server" / "main.py").read_text()
ov = main_src[main_src.index("class OverlayStaticFiles"):]
check("OverlayStaticFiles joins custom/ through static_paths.inside",
      "static_paths.inside(self.custom_dir, path)" in ov and "self.custom_dir / path" not in ov)

sys.exit(1 if FAILED else 0)
