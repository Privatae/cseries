#!/usr/bin/env python3
"""Gate: a run's preview outlives its worktree.

The run card shows the page a run built in place, as soon as the run lands
(2026-10-06: scroll to see which version it is, then open the one you want). The retention sweep (compute.curate_harness_runs)
frees an unpicked run's work/<run> after its grace window and keeps the rest
of the session dir — measured on one Host compare 81ad98fe: diffs/*.patch for
all four engines, work/ empty — so GET /sessions/{id}/preview/{run} answered
410 and every older card lost its picture.

review._write_preview_snapshot keeps, at collect and beside diffs/<run>.patch,
the files the preview reads (every page the run offers + the local css/js
those pages inline) under previews/<run>/, with the entry choice in
previews/<run>.json. The route renders that snapshot through the SAME
_inline_page it renders the worktree with, so after reclaim it must answer
exactly what it answered before: same status, same headers, same body.

Run: APP_ROOT=<repo> python3 scripts/test_run_preview_snapshot.py
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
import shutil  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from server.routes import review as rv  # noqa: E402

snapshot = getattr(rv, "_write_preview_snapshot", None)
check("the collect step has a preview snapshot writer", callable(snapshot))


def _patch(paths):
    return "".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-x\n+y\n"
                   for p in paths)


def _session(sid: str, files: dict, changed=(), links: dict = None) -> Path:
    sd = rv.SESSIONS_ROOT / sid
    (sd / "diffs").mkdir(parents=True, exist_ok=True)
    (sd / "manifest.json").write_text(json.dumps(
        {"session_id": sid, "harnesses": ["kimi"], "worktree_root": str(sd / "work")}))
    wt = sd / "work" / "kimi"
    for rel, body in files.items():
        p = wt / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    for rel, target in (links or {}).items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, wt / rel)
    (sd / "diffs" / "kimi.patch").write_text(_patch(changed))
    return sd


def _collect(sd: Path):
    return snapshot(sd, "kimi", sd / "work" / "kimi") if callable(snapshot) else None


def _reclaim(sd: Path) -> None:
    # exactly what compute.curate_harness_runs removes
    shutil.rmtree(sd / "work" / "kimi")


def _get(sid: str, entry=None):
    try:
        r = asyncio.run(rv.preview_harness_index(sid, "kimi", entry))
    except HTTPException as e:
        return e.status_code, str(e.detail), {}
    hdr = {k: v for k, v in r.headers.items() if k.startswith("x-preview")}
    if r.status_code == 300:
        return 300, json.loads(r.body), hdr
    return r.status_code, r.body.decode(), hdr


def _files(d: Path) -> list:
    return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()) if d.is_dir() else []


PAGE = ('<!doctype html><html><head><link rel="stylesheet" href="styles.css">'
        '<link rel="stylesheet" href="evil.css"><link rel="stylesheet" href="_ref_project/ref.css">'
        '</head><body>APP<script src="app.js"></script>'
        '<script type="text/babel" data-presets="react" src="jsx.js"></script></body></html>')

with tempfile.TemporaryDirectory() as td:
    secret = Path(td) / "outside.txt"
    secret.write_text("OUTSIDE-THE-TREE")
    saved_root = rv.SESSIONS_ROOT
    rv.SESSIONS_ROOT = Path(td) / "sessions"
    try:
        # ── A. collect keeps the page and exactly the assets it inlines ─────
        sd = _session("s-a", {"index.html": PAGE, "styles.css": "body{color:red}",
                              "app.js": "var A=1;", "jsx.js": "const J=<b/>;",
                              "_ref_project/ref.css": "REF", "server/main.py": "x=1",
                              "big/data.bin": "B" * 50000},
                      changed=["index.html"], links={"evil.css": str(secret)})
        live = _get("s-a")
        rec = _collect(sd)
        tree = sd / "previews" / "kimi"
        check("A: collect writes a record of the kept preview",
              isinstance(rec, dict) and (sd / "previews" / "kimi.json").is_file(), str(rec))
        kept = _files(tree)
        check("A: it keeps the page and the css/js the page inlines, nothing else",
              kept == ["app.js", "index.html", "jsx.js", "styles.css"], str(kept))
        check("A: a symlinked asset is not copied (it points out of the tree)",
              tree.is_dir() and not (tree / "evil.css").exists()
              and all("OUTSIDE-THE-TREE" not in (tree / f).read_text() for f in kept))
        check("A: no temporary names are left behind",
              (sd / "previews").is_dir()
              and not [p.name for p in (sd / "previews").iterdir() if p.name.startswith(".")])

        # ── B. after reclaim the route answers what it answered before ──────
        _reclaim(sd)
        after = _get("s-a")
        check("B: live, the route renders the page with its assets inlined",
              live[0] == 200 and "body{color:red}" in live[1] and "var A=1;" in live[1], str(live)[:200])
        check("B: after the worktree is reclaimed, the SAME page (status, headers, body)",
              after == live, f"live={str(live)[:120]} after={str(after)[:120]}")
        check("B: the babel script keeps its type in the kept render",
              '<script type="text/babel" data-presets="react">const J=<b/>;</script>' in str(after[1]))
        check("B: the symlinked asset is still a link, never its target",
              'href="evil.css"' in str(after[1]) and "OUTSIDE-THE-TREE" not in str(after[1]))
        check("B: a reference copy is still not inlined", 'href="_ref_project/ref.css"' in str(after[1]))
        eps = rv._session_entrypoints(json.loads((sd / "manifest.json").read_text()))
        check("B: and the run card still calls the reclaimed run openable", eps.get("kimi") is True, str(eps))

        # ── C. several pages: the 300 list, ?entry= and the page switcher ──
        THEMES = {f"themes/{n}/index.html": f'<link href="t.css" rel="stylesheet">THEME:{n}'
                  for n in ("dusk", "moss", "tide")}
        THEMES.update({f"themes/{n}/t.css": f".{n}{{}}" for n in ("dusk", "moss", "tide")})
        sd = _session("s-c", THEMES, changed=[k for k in THEMES if k.endswith(".html")])
        asks = (None, "themes/moss/index.html", "themes/none/index.html", "../manifest.json")
        live = [_get("s-c", a) for a in asks]
        _collect(sd)
        _reclaim(sd)
        after = [_get("s-c", a) for a in asks]
        check("C: three changed pages — the same 300 list after reclaim",
              live[0][0] == 300 and after[0] == live[0], f"{live[0]} | {after[0]}")
        check("C: ?entry= opens the named page, inlined, after reclaim",
              live[1][0] == 200 and ".moss{}" in live[1][1] and after[1] == live[1], f"{after[1]}")
        check("C: a page that does not exist is still 404",
              after[2][0] == 404 and live[2][0] == 404, f"{after[2]}")
        check("C: an unsafe name is still 400", after[3][0] == 400 and live[3][0] == 400, f"{after[3]}")

        sd = _session("s-h", {"index.html": "ROOT", "admin/index.html": "ADMIN"},
                      changed=["index.html", "admin/index.html"])
        live = _get("s-h")
        _collect(sd)
        _reclaim(sd)
        check("C: the root among several opens with X-Preview-Entries, after reclaim too",
              live[0] == 200 and "x-preview-entries" in live[2] and _get("s-h") == live, f"{live}")

        # ── D. a run with no page answers the same 404 ──────────────────────
        sd = _session("s-d", {"server/main.py": "x=1"}, changed=["server/main.py"])
        live = _get("s-d")
        _collect(sd)
        _reclaim(sd)
        check("D: no page — the same 404 and reason after reclaim",
              live[0] == 404 and _get("s-d") == live, f"{live} | {_get('s-d')}")
        eps = rv._session_entrypoints(json.loads((sd / "manifest.json").read_text()))
        check("D: and the card does not call it openable", eps.get("kimi") is False, str(eps))

        # ── E. reclaimed with no snapshot, or a partial one: 410 as before ──
        sd = _session("s-e", {"index.html": "OLD"}, changed=["index.html"])
        _reclaim(sd)
        code = _get("s-e")[0]
        check("E: a run reclaimed before snapshots existed still answers 410", code == 410, str(code))
        sd = _session("s-p", {"index.html": "P"}, changed=["index.html"])
        _collect(sd)
        (sd / "previews" / "kimi.json").unlink(missing_ok=True)
        _reclaim(sd)
        check("E: a snapshot without its record (cut mid-write) is not served",
              _get("s-p")[0] == 410, str(_get("s-p")))
        eps = rv._session_entrypoints(json.loads((sd / "manifest.json").read_text()))
        check("E: and the card does not call that run openable", eps.get("kimi") is False, str(eps))

        # ── F. a second collect replaces the first, whole ──────────────────
        sd = _session("s-f", {"index.html": "FIRST"}, changed=["index.html"])
        _collect(sd)
        (sd / "work" / "kimi" / "index.html").write_text("SECOND")
        _collect(sd)
        _reclaim(sd)
        check("F: the newer collect is what is served", _get("s-f")[1] == "SECOND", str(_get("s-f")))
        check("F: and the swap leaves no temporary names",
              (sd / "previews").is_dir()
              and not [p.name for p in (sd / "previews").iterdir() if p.name.startswith(".")])

        # ── G. a snapshot that cannot be written never fails the run ────────
        sd = _session("s-g", {"index.html": "G"}, changed=["index.html"])
        (sd / "previews").write_text("a file where the directory goes")
        try:
            res, raised = _collect(sd), False
        except Exception:  # noqa: BLE001
            res, raised = "raised", True
        check("G: an unwritable snapshot returns None and does not raise",
              callable(snapshot) and not raised and res is None, str(res))

        # ── I. an asset named with backslashes, or one reaching out ─────────
        # read_in_tree reads `css\site.css` as css/site.css, so the live render
        # inlines it; the snapshot must keep it under the name it was READ by,
        # or the kept render leaves it a link and differs from the live one.
        # A `..` or absolute reference is never read, so never kept.
        page_i = ('<link rel="stylesheet" href="css\\site.css">'
                  '<link rel="stylesheet" href="_ref_project\\ref.css">'
                  '<link rel="stylesheet" href="../../outside.css">'
                  f'<link rel="stylesheet" href="{secret.with_suffix(".css")}">APP-I')
        secret.with_suffix(".css").write_text("OUTSIDE-CSS")
        sd = _session("s-i", {"index.html": page_i, "css/site.css": ".site{}",
                              "_ref_project/ref.css": "REF-COPY"}, changed=["index.html"])
        (sd / "work" / "outside.css").write_text("OUTSIDE-CSS")
        live = _get("s-i")
        _collect(sd)
        kept = _files(sd / "previews" / "kimi")
        _reclaim(sd)
        after = _get("s-i")
        check("I: live, a backslash-named asset is inlined", live[0] == 200 and ".site{}" in live[1], str(live)[:160])
        check("I: it is kept under the name it was read by", kept == ["css/site.css", "index.html"], str(kept))
        check("I: after reclaim the SAME page", after == live, f"live={str(live)[:120]} after={str(after)[:120]}")
        check("I: a reference copy named with a backslash is still not inlined",
              "REF-COPY" not in str(live[1]) and "REF-COPY" not in str(after[1]), str(live)[:160])
        check("I: a `..` or absolute asset is never kept or inlined",
              "OUTSIDE-CSS" not in str(after[1]) and "OUTSIDE-CSS" not in str(live[1]))

        # ── J. a preview over the keep bound is never read whole, nor kept ──
        # A run that writes a huge app.js must not be read into the server or
        # copied into the session dir: every read the snapshot makes carries a
        # limit, a run over the bound keeps no preview, and once its worktree
        # is reclaimed the route says why instead of the generic 410.
        from server.applications import entry_pages as _ep
        saved_max, saved_read = getattr(rv, "_PREVIEW_KEEP_MAX", None), _ep.read_in_tree
        limits = []

        def _spy(root, rel, limit=None):
            limits.append(limit)
            return saved_read(root, rel, limit)

        try:
            rv._PREVIEW_KEEP_MAX = 4000
            _ep.read_in_tree = _spy
            sd = _session("s-j", {"index.html": '<script src="big.js"></script>APP-J',
                                  "big.js": "J" * 9000}, changed=["index.html"])
            rec = _collect(sd)
            read_limits = list(limits)
            sd_ok = _session("s-k", {"index.html": '<script src="ok.js"></script>APP-K',
                                     "ok.js": "K" * 1000}, changed=["index.html"])
            _collect(sd_ok)
        finally:
            _ep.read_in_tree = saved_read
            if saved_max is None:
                del rv._PREVIEW_KEEP_MAX
            else:
                rv._PREVIEW_KEEP_MAX = saved_max
        check("J: every read the snapshot makes is bounded by what is left to keep",
              bool(read_limits) and all(lim is not None and lim <= 4001 for lim in read_limits),
              str(read_limits))
        check("J: a run over the bound keeps no preview, and its record says so",
              isinstance(rec, dict) and rec.get("too_large") is True
              and not (sd / "previews" / "kimi").exists(), str(rec))
        _reclaim(sd)
        code, detail, _ = _get("s-j")
        check("J: after reclaim the route says the page was too large to keep",
              code == 410 and "larger than" in detail, f"{code} {detail[:160]}")
        eps = rv._session_entrypoints(json.loads((sd / "manifest.json").read_text()))
        check("J: and the card does not call it openable", eps.get("kimi") is False, str(eps))
        _reclaim(sd_ok)
        check("J: a preview under the bound is kept as before",
              _get("s-k")[0] == 200 and "K" * 1000 in _get("s-k")[1], str(_get("s-k"))[:120])

        # ── J2. the bound holds for what a page RENDERS to, not only its files ─
        # An asset read once is inlined at every tag naming it, so a small file
        # named by many tags renders to many times the bound: the snapshot
        # writer builds that render at collect and the route builds it again
        # after reclaim. A page whose render passes the bound is not kept (and
        # an older snapshot of the run goes with it); pages that share an
        # asset are bounded each on its own render, never on their sum.
        try:
            rv._PREVIEW_KEEP_MAX = 4000
            sd = _session("s-j2", {"index.html": '<script src="r.js"></script>APP-J2',
                                   "r.js": "R" * 1000}, changed=["index.html"])
            first = _collect(sd)
            had_old = (sd / "previews" / "kimi").is_dir()
            (sd / "work" / "kimi" / "index.html").write_text('<script src="r.js"></script>' * 10)
            rec2 = _collect(sd)
            sd_sh = _session("s-j3", {"index.html": '<script src="a/s.js"></script>ROOT',
                                      "a/index.html": '<script src="s.js"></script>SUB',
                                      "a/s.js": "S" * 2500},
                             changed=["index.html", "a/index.html"])
            rec3 = _collect(sd_sh)
        finally:
            if saved_max is None:
                del rv._PREVIEW_KEEP_MAX
            else:
                rv._PREVIEW_KEEP_MAX = saved_max
        check("J2: a page under the bound is kept the first time",
              isinstance(first, dict) and not first.get("too_large") and had_old, str(first))
        check("J2: a page whose render passes the bound is not kept, though its files fit",
              isinstance(rec2, dict) and rec2.get("too_large") is True, str(rec2))
        check("J2: and the older snapshot of the run is removed with it",
              not (sd / "previews" / "kimi").exists()
              and not [p.name for p in (sd / "previews").iterdir() if p.name.startswith(".")],
              str(sorted(p.name for p in (sd / "previews").iterdir())))
        check("J2: two pages sharing an asset are bounded each on its own render",
              isinstance(rec3, dict) and not rec3.get("too_large")
              and rec3.get("entries") == ["a/index.html", "index.html"], str(rec3))

        # ── L. the LIVE route is bounded the same way ───────────────────────
        # Since workbench 0.24.0 the run card loads this route by itself the
        # moment a run lands, so a page that renders past the bound is
        # answered with the reason, never built: one small asset named by
        # many tags renders to many times its size (the reviewer's 300 KB page
        # × 8 MB asset ≈ 2 TB). Below the bound it renders exactly as before.
        saved_max = getattr(rv, "_PREVIEW_KEEP_MAX", None)
        try:
            rv._PREVIEW_KEEP_MAX = 4000
            many = "".join('<script src="a.js"></script>' for _ in range(50))
            _session("s-l", {"index.html": many + "APP-L", "a.js": "A" * 200}, changed=["index.html"])
            code_l, detail_l, _ = _get("s-l")
            _session("s-l2", {"index.html": '<script src="b.js"></script>APP-L2', "b.js": "B" * 9000},
                     changed=["index.html"])
            code_l2, detail_l2, _ = _get("s-l2")
            _session("s-l3", {"index.html": '<script src="c.js"></script>APP-L3', "c.js": "C" * 1000},
                     changed=["index.html"])
            ok_l3 = _get("s-l3")
        finally:
            if saved_max is None:
                del rv._PREVIEW_KEEP_MAX
            else:
                rv._PREVIEW_KEEP_MAX = saved_max
        check("L: live, a page whose render passes the bound is refused with the reason",
              code_l == 422 and "larger than" in detail_l, f"{code_l} {str(detail_l)[:120]}")
        check("L: live, a page whose files pass the bound is refused the same way",
              code_l2 == 422 and "larger than" in detail_l2, f"{code_l2} {str(detail_l2)[:120]}")
        check("L: the reason names the bound in words a person reads (never '0 MB')",
              "0 MB" not in detail_l and "4000 bytes" in detail_l, str(detail_l)[:160])
        check("L: live, a page under the bound renders as before",
              ok_l3[0] == 200 and "C" * 1000 in ok_l3[1] and "APP-L3" in ok_l3[1], str(ok_l3)[:120])

        # ── H. both collect sites keep the snapshot, right after the patch ──
        src = (_ROOT / "server" / "routes" / "review.py").read_text()
        for site, patch_line, call in (
                ("single build", '(session_dir / "diffs" / f"{harness}.patch").write_text(_diff or "")',
                 "_write_preview_snapshot, session_dir, harness, worktree)"),
                ("compare variant", '(session_dir / "diffs" / f"{vid}.patch").write_text(r.get("diff") or "")',
                 "_write_preview_snapshot, session_dir, vid, worktrees[vid])")):
            at, call_at = src.find(patch_line), src.find(call)
            check(f"H: the {site} collect keeps the preview after writing its patch",
                  at != -1 and call_at != -1 and 0 < call_at - at < 600, f"patch@{at} call@{call_at}")
    finally:
        rv.SESSIONS_ROOT = saved_root

print()
if _FAILURES:
    print(f"FAIL: {len(_FAILURES)} check(s) failed")
    sys.exit(1)
print("PASS: a run's preview outlives its worktree")
