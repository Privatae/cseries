#!/usr/bin/env python3
"""Gate: "open app" follows what the RUN produced.

2026-08-22: when a backend agent was done, the Open app button showed an old
front-end run, which made it look like that was what had just been coded.

The button appeared for any completed run with a project id, and for a solo
build it rendered /api/projects/<id>/preview — the PROJECT's last LANDED state,
which has nothing to do with the run just finished. It never asked what the run
produced.

review._session_entrypoints is that answer, computed from the run's OWN worktree
(available for solo builds since run-scoped worktrees, 0fa7afbf), using
entry_pages.choose_entry — the SAME rule preview_harness_index renders with, so
the button offered and the page served cannot disagree.

WHICH PAGE (project 251, 2026-09-30): a run changed one desktop theme
and added four, all at themes/<name>/index.html, and the preview opened the
first one the filesystem listed. The preview now opens the page the RUN
changed (its own diff), lists the pages when the run changed several, and
walks the tree only when the run changed none. Section W drives the route
over a real session on disk: five equal-depth themes, one of them changed.

SCOPE, measured rather than assumed: a build worktree is SEEDED from the
project, so a backend phase in a project that already has a UI inherits that
index.html and IS openable. Checked on three builds on one Host — all
three carry an inherited index.html. So this flag answers "is there anything to
render from this run's tree", NOT "was this a UI run"; the second question is
answered by the diff, which is why "review diff" is offered on every completed
run. What this fixes for certain is the button opening the PROJECT's landed
state — someone else's older run — instead of this run's own output.

Run: APP_ROOT=<repo> python3 scripts/test_run_entrypoints.py
"""
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(_ROOT))

_FAILURES: list[str] = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra and not cond else ''}")
    if not cond:
        _FAILURES.append(label)


import asyncio  # noqa: E402
import json  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from server.applications.entry_pages import choose_entry, page_in_tree  # noqa: E402
from server.routes import review as rv  # noqa: E402
from server.routes.review import (  # noqa: E402
    _run_entry_choice, _session_entrypoints,
)


def _mk(root: Path, harness: str, files: dict) -> None:
    for rel, body in files.items():
        p = root / harness / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


with tempfile.TemporaryDirectory() as td:
    wt = Path(td) / "work"
    wt.mkdir()

    # A frontend run: index.html at the worktree root.
    _mk(wt, "kimi", {"index.html": "<h1>app</h1>"})
    # A backend run: real code, no web entry. THIS is the case that was
    # offering "open app" and showing someone else's frontend.
    _mk(wt, "codex", {"server/main.py": "print('api')",
                      "server/routes.py": "# routes"})
    # A nested run: the harness wrote into a subdir (grok has done this).
    _mk(wt, "grok", {"todo-app/index.html": "<h1>nested</h1>"})
    # A run with an index.html only inside .git — must NOT count.
    _mk(wt, "cursor", {".git/index.html": "<h1>not yours</h1>",
                       "README.md": "notes"})
    # A harness listed in the manifest whose worktree never materialised.
    manifest = {
        "worktree_root": str(wt),
        "project_dir": str(Path(td) / "project"),
        "harnesses": ["kimi", "codex", "grok", "cursor", "ghost"],
    }

    eps = _session_entrypoints(manifest)

    check("a run that built a UI is openable", eps.get("kimi") is True, str(eps))
    check("a BACKEND run is NOT openable — the reported defect",
          eps.get("codex") is False, str(eps))
    check("a nested index.html still counts (harness wrote into a subdir)",
          eps.get("grok") is True, str(eps))
    check("an index.html inside .git does NOT count",
          eps.get("cursor") is False, str(eps))
    check("a harness with no worktree at all is False, not an error",
          eps.get("ghost") is False, str(eps))
    check("every manifest harness gets an answer",
          set(eps) == {"kimi", "codex", "grok", "cursor", "ghost"}, str(eps))

    # The entry the button promises is the entry the preview route serves.
    check("the root entry is the answer when the run names nothing",
          _run_entry_choice(wt / "kimi").entries == ("index.html",))
    check("a NESTED entry is found (the harness wrote into a subdir)",
          _run_entry_choice(wt / "grok").entries == ("todo-app/index.html",))
    check("a backend run has no entry",
          _run_entry_choice(wt / "codex").entries == ())

    # Shallowest, not first-found: a deep entry must lose to a shallow one.
    _mk(wt, "deep", {"a/b/c/index.html": "deep", "x/index.html": "shallow"})
    got = _run_entry_choice(wt / "deep").entries
    check("the shallowest entry wins over a deeper one",
          got == ("x/index.html",), str(got))

    # Equal depth is NOT a tie to break: every one is offered.
    _mk(wt, "ties", {"b/index.html": "b", "a/index.html": "a", "c/d/index.html": "deep"})
    got = _run_entry_choice(wt / "ties")
    check("equal-depth entries are ALL offered, not one picked by listing order",
          got.entries == ("a/index.html", "b/index.html") and got.basis == "tree", str(got))

    # The run's own changes come before the tree.
    _mk(wt, "own", {"index.html": "root", "docs/index.html": "docs",
                    "themes/x/index.html": "x", "themes/x/app.js": "//"})
    got = _run_entry_choice(wt / "own", ["docs/index.html", "README.md"])
    check("a changed entry page beats the root index.html",
          got.entries == ("docs/index.html",) and got.basis == "changed", str(got))
    got = _run_entry_choice(wt / "own", ["themes/x/app.js"])
    check("a run that changed only an asset opens the page above it",
          got.entries == ("themes/x/index.html",) and got.basis == "enclosing", str(got))
    got = _run_entry_choice(wt / "own", ["gone/index.html"])
    check("a DELETED entry page is not offered (falls through to what exists)",
          "gone/index.html" not in got.entries, str(got))

    # A symlinked index.html is not a page: it could point out of the jail.
    outside = Path(td) / "outside.html"
    outside.write_text("<h1>secret</h1>")
    (wt / "link").mkdir()
    (wt / "link" / "index.html").symlink_to(outside)
    check("a symlinked index.html is never an entry",
          _run_entry_choice(wt / "link").entries == (), str(_run_entry_choice(wt / "link")))

    # ── W. the ROUTE, over a real session: five themes, the run changed one ──
    FIVE = ("elm-lantern", "graveyard-shift", "samhain", "seance", "willow-hour")

    def _patch(paths, deleted=()):
        out = []
        for p in paths:
            out.append(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-x\n+y\n")
        for p in deleted:
            out.append(f"diff --git a/{p} b/{p}\ndeleted file mode 100644\n--- a/{p}\n+++ /dev/null\n")
        return "".join(out)

    def _session(sid: str, patch: str | None) -> None:
        sd = rv.SESSIONS_ROOT / sid
        (sd / "diffs").mkdir(parents=True, exist_ok=True)
        (sd / "manifest.json").write_text(json.dumps(
            {"session_id": sid, "harnesses": ["kimi"], "worktree_root": str(sd / "work")}))
        for name in FIVE:
            p = sd / "work" / "kimi" / "themes" / name / "index.html"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"<html><body>THEME:{name}</body></html>")
        (sd / "work" / "kimi" / "themes" / "elm-lantern" / "app.js").write_text("//")
        if patch is not None:
            (sd / "diffs" / "kimi.patch").write_text(patch)

    def _get(sid: str, entry=None):
        try:
            r = asyncio.run(rv.preview_harness_index(sid, "kimi", entry))
        except HTTPException as e:
            return e.status_code, str(e.detail)
        if r.status_code == 300:
            return 300, json.loads(r.body)
        return r.status_code, r.body.decode()

    saved_root = rv.SESSIONS_ROOT
    rv.SESSIONS_ROOT = Path(td) / "sessions"
    try:
        _session("s-one", _patch(["themes/elm-lantern/index.html"],
                                 deleted=["themes/elm-lantern/index.html.patch-tail"]))
        code, body = _get("s-one")
        check("W: five equal-depth themes, the run changed ONE — the preview opens THAT one",
              code == 200 and "THEME:elm-lantern" in body, f"{code} {str(body)[:120]}")
        eps = _session_entrypoints(json.loads(
            (rv.SESSIONS_ROOT / "s-one" / "manifest.json").read_text()))
        check("W: and the run card calls it openable", eps.get("kimi") is True, str(eps))

        _session("s-all", _patch([f"themes/{n}/index.html" for n in FIVE]))
        code, body = _get("s-all")
        check("W: a run that changed all five gets the LIST, not a pick (300)",
              code == 300 and body.get("entries") == [f"themes/{n}/index.html" for n in FIVE]
              and body.get("basis") == "changed", f"{code} {body}")
        code, body = _get("s-all", "themes/seance/index.html")
        check("W: ?entry= opens the page the person chose",
              code == 200 and "THEME:seance" in body, f"{code} {str(body)[:120]}")

        _session("s-asset", _patch(["themes/elm-lantern/app.js"]))
        code, body = _get("s-asset")
        check("W: a run that changed only a theme's script opens that theme",
              code == 200 and "THEME:elm-lantern" in body, f"{code} {str(body)[:120]}")

        _session("s-none", None)
        code, body = _get("s-none")
        check("W: no diff on record — the tree step offers all five, still no silent pick",
              code == 300 and len(body.get("entries") or []) == 5 and body.get("basis") == "tree",
              f"{code} {body}")

        for bad, want in (("../../outside.html", 400), ("/etc/passwd", 400),
                          (".git/index.html", 404), ("themes/nope/index.html", 404),
                          ("themes/elm-lantern/index.html\x00", 400), ("a\x00.html", 400)):
            code, body = _get("s-one", bad)
            check(f"W: ?entry={bad!r} is refused ({want})", code == want, f"{code} {str(body)[:120]}")
        # A named entry is any real file in the tree, whatever its suffix, as
        # on every other named-entry lane (entry_pages.entry_param; the file
        # route serves the same bytes). Only escapes, links and .git refuse.
        code, body = _get("s-one", "themes/elm-lantern/app.js")
        check("W: ?entry= naming a real non-HTML file inside opens it, as named entries always have",
              code == 200 and body == "//", f"{code} {str(body)[:120]}")

        # ── R. refusals with REAL targets (review of 13e9ba03, finding 2) ──
        # Every target below exists and is readable: a refusal here is the
        # guard's doing, never a missing file's. `secret` must never come back.
        sd = rv.SESSIONS_ROOT / "s-one"
        wt_root = sd / "work" / "kimi"
        outside_dir = sd / "work" / "outside"
        outside_dir.mkdir(parents=True, exist_ok=True)
        (outside_dir / "index.html").write_text("<html><body>SECRET-OUTSIDE</body></html>")
        (wt_root / "themes" / "link.html").symlink_to(outside_dir / "index.html")
        for label, name, want in (
                ("../outside/index.html, which really exists outside the tree",
                 "../outside/index.html", 400),
                ("an absolute path to a real .html file outside",
                 str(outside_dir / "index.html"), 400),
                ("a symlinked .html inside the tree pointing out of it",
                 "themes/link.html", 404)):
            code, body = _get("s-one", name)
            check(f"R: ?entry= {label} is refused ({want}) and nothing leaks",
                  code == want and "SECRET-OUTSIDE" not in str(body), f"{code} {str(body)[:120]}")

        # ── D. The default: the root among the changed pages opens directly
        sd = rv.SESSIONS_ROOT / "s-root"
        (sd / "diffs").mkdir(parents=True, exist_ok=True)
        (sd / "manifest.json").write_text(json.dumps(
            {"session_id": "s-root", "harnesses": ["kimi"], "worktree_root": str(sd / "work")}))
        for rel in ("index.html", "about/index.html", "blog/index.html"):
            f = sd / "work" / "kimi" / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"<html><body>PAGE:{rel}</body></html>")
        (sd / "diffs" / "kimi.patch").write_text(
            _patch(["index.html", "about/index.html", "blog/index.html"]))
        r = asyncio.run(rv.preview_harness_index("s-root", "kimi", None))
        listed = json.loads(r.headers.get("x-preview-entries") or "[]")
        check("D: root + about + blog all added — the ROOT opens directly (no list)",
              r.status_code == 200 and "PAGE:index.html" in r.body.decode(), f"{r.status_code}")
        check("D: ...and the switcher is told all three",
              listed == ["about/index.html", "blog/index.html", "index.html"]
              and json.loads(r.headers.get("x-preview-entry") or "null") == "index.html",
              f"{listed} {r.headers.get('x-preview-entry')}")
        _session("s-norootlist", _patch([f"themes/{n}/index.html" for n in FIVE[:3]]))
        code, body = _get("s-norootlist")
        check("D: several changed pages and NONE is the root — still the list (300)",
              code == 300 and len(body.get("entries") or []) == 3, f"{code} {body}")
    finally:
        rv.SESSIONS_ROOT = saved_root

    # ── G. each guard in page_in_tree, isolated: each case below is refused by
    # exactly ONE guard, so deleting that guard fails exactly that check. The
    # resolve-escape check is a backstop behind the symlink and `..` guards
    # and is pinned by the outside-pointing cases with both removed (R above,
    # and the last case here).
    g = Path(td) / "guards"
    (g / "a").mkdir(parents=True)
    (g / "index.html").write_text("root")
    (g / "a" / "index.html").write_text("a")
    (g / "in.html").symlink_to(g / "a" / "index.html")            # a link that stays INSIDE
    (Path(td) / "g-out.html").write_text("SECRET")
    (g / "out.html").symlink_to(Path(td) / "g-out.html")          # a link that leaves
    check("G: `..` is refused even when it lands back inside the tree",
          page_in_tree(g, "a/../index.html") is None)
    check("G: an absolute path is refused even when it points inside the tree",
          page_in_tree(g, str(g / "a" / "index.html")) is None)
    check("G: a symlink is refused even when it points inside the tree",
          page_in_tree(g, "in.html") is None)
    check("G: a symlink that leaves the tree is refused",
          page_in_tree(g, "out.html") is None)
    try:
        got = page_in_tree(g, "a\x00.html")
        check("G: a NUL byte is refused, not raised", got is None, repr(got))
    except Exception as e:  # noqa: BLE001
        check("G: a NUL byte is refused, not raised", False, f"raised {e!r}")
    check("G: a plain page inside is accepted (the guards are not refusing everything)",
          page_in_tree(g, "a/index.html") == "a/index.html")

    # choose_entry never raises on a missing root
    check("choose_entry on a missing root answers none",
          choose_entry(Path(td) / "nope").basis == "none")

    # ── legacy sessions: no worktree_root, falls back to project_dir ────────
    legacy_proj = Path(td) / "legacy"
    (legacy_proj / "kimi").mkdir(parents=True)
    (legacy_proj / "kimi" / "index.html").write_text("<h1>old</h1>")
    legacy = {"project_dir": str(legacy_proj), "harnesses": ["kimi"]}
    check("a pre-0fa7afbf session still resolves via project_dir",
          _session_entrypoints(legacy).get("kimi") is True)

    # ── never raises ────────────────────────────────────────────────────────
    for label, m in (
        ("an empty manifest", {}),
        ("a manifest with no harnesses", {"project_dir": str(wt)}),
        ("a manifest with a junk root", {"project_dir": "/nonexistent/x",
                                         "harnesses": ["kimi"]}),
    ):
        try:
            out = _session_entrypoints(m)
            ok = isinstance(out, dict)
        except Exception as e:      # noqa: BLE001
            ok, out = False, f"raised {e!r}"
        check(f"tolerates {label}", ok, str(out))

    # ── path escape ─────────────────────────────────────────────────────────
    esc = {"worktree_root": str(wt), "harnesses": ["../../etc"]}
    check("a traversal harness name cannot escape the worktree root",
          _session_entrypoints(esc).get("../../etc") is False,
          str(_session_entrypoints(esc)))

print()
if _FAILURES:
    print(f"{len(_FAILURES)} FAILED: {_FAILURES}")
    sys.exit(1)
print("all gates passed")
