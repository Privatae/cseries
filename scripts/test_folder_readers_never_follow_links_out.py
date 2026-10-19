#!/usr/bin/env python3
"""Gate: no reader that sends a folder's bytes somewhere else follows a link
out of the folder.

WHY THIS EXISTS (2026-10-01). An independent review reproduced three readers
that followed a symbolic link out of a project folder: "Publish as site" put
the target's content into a PUBLIC site's zip, "Install as my app" put it
into the app bundle, and the code index read it and sent it to the embedder
and into the Host's code search. A link gets into a project by an ordinary
`git checkout` of a repository that holds one, and a public GitHub repository
reaches the user's Gitea through the migrate API, so `x -> /proc/self/environ`
is one imported repository away from a public site.

Every reader of the class gets the same fixture: a secret file OUTSIDE the
tree, and inside the tree an absolute link to it, a relative `../` escape to
it, a link to a folder outside, an in-tree link to a real file, and an
in-tree link into a folder the reader skips (holding a second secret). The
secret's bytes must never appear in what the reader produces.

Readers covered: the site zip (and the web-root choice), the app bundle
(pack), the code index, the artifact history (snapshot and revert), the
harness worktree seed, the Host backup / export writer, and local_grep.
Measured against origin/main bfc51351's server/ and brain/: every reader's
section fails (23 checks red). The indexer there also hangs for good on a
FIFO named like source (`pipe.js`); that run used the fixture without one.

MUTATIONS, each run against this file (RESULT: red for all):
  tree_walk: the escape check AND Tree.open's `..` refusal removed; link
  targets not passed through `skip`; folders opened without O_NOFOLLOW; a
  linked file listed as a file and opened following. The web root's link
  check removed; the indexer following in-tree links; revert emptying a
  linked folder with rmtree; the seed copying a top-level linked folder;
  the backup opening by name, or without the real-path check; local_grep
  grepping the name instead of the real path.
  O_NOFOLLOW dropped on the file alone (the swapped-file check). Layered,
  so one guard alone survives: the escape check alone, because Tree.open
  refuses `..` again.

Also pinned here, each with its own mutations run red:
  - The harness seed copies through the walk: a folder swapped for a link at
    the moment the walk opens it (tree_walk._at_open) brings nothing in, a
    link keeps its own target text, permission bits are kept, a FIFO is left
    out. (Seed copy back to copytree: red.)
  - A linked artifact .history: read as no history (log, list, render, trial
    mark); replaced by a real folder on the next write with nothing behind
    the link written, changed or removed; a FILE named .history left alone;
    the prune never removes outside .history (a log entry "../victim").
    (.history opened following a link; the prune trusting the log; the
    trial mark removed by path; the log read by path: each red.)
  - A build on a tree with a link out of it is refused before any engine is
    resolved or pod woken, naming each link and what to do: at the chat
    door (the Host's tool result), on the chain (the row's error and the
    bench's activity feed, both rendered by the Workbench's run card, and
    the delivered report), in the build primitive, and for a compare
    (fan_out, no session left behind). (Each of the five checks removed,
    the envelope's continue advice put back, escaping links not
    recognised: each red.)

And, after the review of 5c5a7c4f (symlink probes), each with its mutations run red:
  - A build folder that is a link is followed only to a folder inside the
    project, which is then the web root, reached from the project folder
    (a folder swapped for a link on the way is refused); `dist -> /`, a
    link out of the project, a broken one, one to a file and `dist -> .`
    are 400s naming the link. Never the project root instead: a Vite
    project's root index.html had taken over and the public zip carried
    its source, package.json and .env.
  - A linked folder inside the tree is walked (site zip, pack, scanners),
    shown under the link's name; never one into a left-out folder or one
    whose own name a folder rule leaves out; a loop is refused and the walk
    ends. pack refuses, by name, every link it cannot carry (out of the
    folder, broken, into a hidden folder, looping) instead of dropping it;
    the reviewer's pack probe packs exactly what main packed.
  - The continue and phase-build doors refuse with the message (no claim,
    no row).
  - The contract scanners (build_contract, api_contract) never read a link
    out of the project, so their messages cannot quote one.
  - A log.jsonl inside a real .history that is a link is not appended
    through.

Temp dirs only; no pod, no DB, no network, no model.
"""
import asyncio
import io
import os
import stat
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "brain"))
sys.path.insert(0, str(REPO))

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  ok    " if ok else "  FAIL  ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


WORK = Path(tempfile.mkdtemp(prefix="link-readers-")).resolve()
SECRET = b"OUTSIDE-SECRET-" + os.urandom(6).hex().encode()
SKIPPED = b"SKIPPED-SECRET-" + os.urandom(6).hex().encode()
OUT = WORK / "outside"
(OUT / "sub").mkdir(parents=True)
(OUT / "secret.txt").write_bytes(SECRET + b"\n")
(OUT / "sub" / "inner.js").write_bytes(b"// " + SECRET + b"\n")
(OUT / "sub" / "index.html").write_bytes(b"<p>" + SECRET + b"</p>\n")
REAL = b"const real = 1; // in the tree\n"


def plant(d: Path, skipped_dir: str | None = "node_modules", fifo: bool = True) -> None:
    """The fixture, in folder `d`. `skipped_dir` is a folder the reader under
    test leaves out (None: the reader skips nothing there)."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "real.js").write_bytes(REAL)
    os.symlink(OUT / "secret.txt", d / "abs.js")                          # absolute link
    os.symlink(os.path.relpath(OUT / "secret.txt", d), d / "escape.js")  # ../ escape
    os.symlink(OUT / "sub", d / "linkdir")                                # folder out of the tree
    os.symlink("real.js", d / "same.js")                                  # in-tree link
    if skipped_dir:
        (d / skipped_dir).mkdir(exist_ok=True)
        (d / skipped_dir / "cfg.js").write_bytes(SKIPPED + b"\n")
        os.symlink(f"{skipped_dir}/cfg.js", d / "into-skipped.js")        # in-tree, into a skipped dir
    if fifo:
        os.mkfifo(d / "pipe.js")                                          # must not hang a reader


def leaks(blob: bytes) -> list:
    return [n for n, s in (("outside secret", SECRET), ("skipped secret", SKIPPED)) if s in blob]


def files_under(root: Path) -> bytes:
    """Every regular file's bytes under `root`, links never followed."""
    out = b""
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            if stat.S_ISREG(os.lstat(p).st_mode):
                out += Path(p).read_bytes()
    return out


def zip_members(raw: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


run = asyncio.run

# ── 1. the shared walk (server/applications/tree_walk.py) ─────────────────
print("1. tree_walk")
try:
    from server.applications import tree_walk as tw
except ImportError:
    tw = None
check("server/applications/tree_walk.py exists", tw is not None)
if tw is not None:
    T = WORK / "walk"
    plant(T)
    (T / "deep" / "er").mkdir(parents=True)
    (T / "deep" / "er" / "x.js").write_bytes(REAL)
    with tw.open_tree_no_escape(T) as tree:
        got = {f.rel: (f.source, tree.read(f)) for f in tree.walk(
            skip=lambda parts, is_dir: is_dir and parts[-1] == "node_modules",
            links=tw.LINKS_IN_TREE)}
    check("walk: the outside secret is never read", not leaks(b"".join(v[1] or b"" for v in got.values())),
          str(sorted(got)))
    check("walk: absolute link, ../ escape, linked folder, FIFO left out",
          not ({"abs.js", "escape.js", "pipe.js"} & set(got)) and not any(r.startswith("linkdir") for r in got),
          str(sorted(got)))
    check("walk: an in-tree link reads its target", got.get("same.js") == ("real.js", REAL), str(got.get("same.js")))
    check("walk: a link into a skipped folder is left out", "into-skipped.js" not in got, str(sorted(got)))
    check("walk: sorted(rglob) order", list(got) == sorted(got, key=lambda r: Path(r)), str(list(got)))
    with tw.open_tree_no_escape(T) as tree:
        plain = {f.rel for f in tree.walk()}
    check("walk: links='skip' (the default) yields no link at all",
          plain == {"real.js", "deep/er/x.js", "node_modules/cfg.js"}, str(sorted(plain)))
    # Two linked folders that lead into each other: each is walked once,
    # and the link that would loop is refused, so the walk ends.
    CY = WORK / "cycle"
    (CY / "p").mkdir(parents=True)
    (CY / "q").mkdir()
    (CY / "p" / "f.js").write_bytes(REAL)
    os.symlink("../q", CY / "p" / "x")
    os.symlink("../p", CY / "q" / "y")
    cyc: list = []
    with tw.open_tree_no_escape(CY) as tree:
        cfiles = sorted(f.rel for f in tree.walk(links=tw.LINKS_IN_TREE, refused=cyc))
    check("walk: linked folders that lead into each other end, the looping links refused",
          cfiles == ["p/f.js", "q/y/f.js"]
          and sorted(r for r, _t, why in cyc if "loops back" in why) == ["p/x/y", "q/y/x"],
          str((cfiles, cyc)))
    # A linked folder is left out by its own name exactly as a real folder
    # of that name would be, also by a rule that drops folders only.
    LN = WORK / "linkname"
    (LN / "lib").mkdir(parents=True)
    (LN / "lib" / "a.js").write_bytes(REAL)
    os.symlink("lib", LN / "vendor")
    with tw.open_tree_no_escape(LN) as tree:
        lnames = sorted(f.rel for f in tree.walk(
            skip=lambda parts, is_dir: is_dir and parts[-1] == "vendor", links=tw.LINKS_IN_TREE))
    check("walk: a linked folder is left out by a folder rule on its name",
          lnames == ["lib/a.js"], str(lnames))
    # A name checked by the walk and swapped for a link before the read.
    with tw.open_tree_no_escape(T) as tree:
        listed = [f for f in tree.walk() if f.rel == "deep/er/x.js"]
        os.rename(T / "deep", T / "deep.real")
        os.symlink(OUT / "sub", T / "deep")
        (OUT / "sub" / "er").mkdir()
        (OUT / "sub" / "er" / "x.js").write_bytes(SECRET)
        swapped = tree.read(listed[0]) if listed else b"?"
        os.unlink(T / "deep")
        os.rename(T / "deep.real", T / "deep")
    check("read: a folder swapped for a link after the walk is not followed", swapped is None, repr(swapped))
    # A file checked by the walk and swapped for a link before the read.
    with tw.open_tree_no_escape(T) as tree:
        listed = [f for f in tree.walk() if f.rel == "real.js"]
        os.rename(T / "real.js", T / "real.js.bak")
        os.symlink(OUT / "secret.txt", T / "real.js")
        swapped = tree.read(listed[0]) if listed else b"?"
        os.unlink(T / "real.js")
        os.rename(T / "real.js.bak", T / "real.js")
    check("read: a file swapped for a link after the walk is not followed", swapped is None, repr(swapped))
    # The tree's own root is a link.
    os.symlink(OUT, WORK / "rootlink")
    try:
        tw.open_tree_no_escape(WORK / "rootlink").close()
        refused = False
    except tw.TreeEscape:
        refused = True
    check("open: a root that is itself a link is refused", refused)
    try:
        tw.open_tree_no_escape(WORK, within="rootlink/sub").close()
        refused = False
    except tw.TreeEscape:
        refused = True
    check("open: `within` reached through a link is refused", refused)

# ── 2. Publish as site (_zip_site_dir, _site_webroot) ─────────────────────
print("2. publish as site")
from server.routes import applications as A          # noqa: E402

S = WORK / "site"
plant(S)
(S / "index.html").write_bytes(b"<h1>site</h1>\n")
raw = A._zip_site_dir(S)
m = zip_members(raw)
check("site zip: no secret in any member", not leaks(b"".join(m.values())), str(sorted(m)))
check("site zip: abs, ../ escape and linked folder left out",
      not ({"abs.js", "escape.js"} & set(m)) and not any(n.startswith("linkdir") for n in m), str(sorted(m)))
check("site zip: an in-tree link to a shipped file is carried", m.get("same.js") == REAL, str(sorted(m)))
check("site zip: the real files are there", m.get("real.js") == REAL and "index.html" in m, str(sorted(m)))
# With the desktop look baked in (the artifact lane), the same.
m2 = zip_members(A._zip_site_dir(S, wear_sheet=":root{--x:1}"))
check("site zip (worn): no secret", not leaks(b"".join(m2.values())), str(sorted(m2)))
# A build folder that is a link: followed only to a folder INSIDE the
# project, which is then the web root; anything else is refused by name and
# never passed over for the project root. (Review of 5c5a7c4f: skipping a
# linked dist/ let a Vite project's root index.html take over, and the public
# zip carried its source, package.json and .env.)
def vite_like(name: str) -> Path:
    vp = WORK / name
    (vp / "src").mkdir(parents=True)
    (vp / "build" / "web").mkdir(parents=True)
    (vp / "index.html").write_bytes(b'<div id=app></div><script type="module" src="/src/main.ts"></script>')
    (vp / "src" / "main.js").write_bytes(b"// app source")
    (vp / ".env").write_bytes(b"STRIPE_SECRET_KEY=" + SECRET)
    (vp / "package.json").write_bytes(b"{}")
    (vp / "build" / "web" / "index.html").write_bytes(b"<h1>built</h1>")
    return vp


P = vite_like("site-in-tree")
os.symlink("build/web", P / "dist")
root, kind = A._site_webroot(P)
check("web root: an in-tree linked dist/ is used, as its target",
      root == P / "build" / "web" and kind == "static", str(root))
m3 = zip_members(A._zip_site_dir(root, project_dir=P))
check("web root: the site is exactly the built page", m3 == {"index.html": b"<h1>built</h1>"},
      str(sorted(m3)))
for _nm, _tgt, _why in (("site-abs", "/", "outside the project"),
                        ("site-out", str(OUT / "sub"), "outside the project"),
                        ("site-up", "../outside/sub", "outside the project"),
                        ("site-broken", "build/nowhere", "not a folder in the project"),
                        ("site-file", "package.json", "not a folder in the project"),
                        ("site-dot", ".", "not a folder in the project")):
    _p = vite_like(_nm)
    os.symlink(_tgt, _p / "dist")
    try:
        A._site_webroot(_p)
        _got = None
    except A.ShipError as e:
        _got = (e.status, e.detail)
    check(f"web root: dist -> {_tgt} is refused (400) by name, never the project root",
          _got is not None and _got[0] == 400 and f"`dist` is a link to `{_tgt}`" in _got[1]
          and _why in _got[1], str(_got))
try:
    m4 = zip_members(A._zip_site_dir(WORK / "site-out" / "dist"))
    refused = not leaks(b"".join(m4.values())) and not m4
except A.ShipError as e:
    refused = e.status == 400
check("site zip: a web root that is itself a link is refused", refused)
# The web root is reached from the project folder: a folder on the way
# swapped for a link after the choice is refused, not followed.
P = vite_like("site-swap")
os.symlink("build/web", P / "dist")
root, kind = A._site_webroot(P)
(OUT / "web").mkdir()
(OUT / "web" / "index.html").write_bytes(b"<p>" + SECRET + b"</p>")
os.rename(P / "build", P / "build.real")
os.symlink(OUT, P / "build")
try:
    m5 = zip_members(A._zip_site_dir(root, project_dir=P))
    swapped_ok = not leaks(b"".join(m5.values()))
except A.ShipError as e:
    swapped_ok = e.status == 400
check("site zip: a folder above the web root swapped for a link is not followed", swapped_ok)
# An in-tree linked folder INSIDE the site is carried, as its target.
S2 = WORK / "site-linked-folder"
(S2 / "assets" / "v2").mkdir(parents=True)
(S2 / "index.html").write_bytes(b"<h1>s</h1>")
(S2 / "assets" / "v2" / "app.js").write_bytes(REAL)
os.symlink("assets/v2", S2 / "latest")
(S2 / "node_modules" / "pkg").mkdir(parents=True)
(S2 / "node_modules" / "pkg" / "x.js").write_bytes(SKIPPED)
os.symlink("node_modules", S2 / "nm")                 # a linked folder INTO a skipped one
m6 = zip_members(A._zip_site_dir(S2))
check("site zip: an in-tree linked folder is carried under its name",
      m6.get("latest/app.js") == REAL and m6.get("assets/v2/app.js") == REAL, str(sorted(m6)))
check("site zip: a linked folder into a left-out folder is not carried",
      not leaks(b"".join(m6.values())) and not any(n.startswith("nm/") for n in m6), str(sorted(m6)))

# A linked build folder never points into a left-out or hidden folder.
for _nm, _tgt in (("site-into-backups", ".adopt-backups/v1"), ("site-into-hist", ".history"),
                  ("site-into-nm", "node_modules/pkg/dist"), ("site-into-git", ".git")):
    _p = vite_like(_nm)
    for _d in (".adopt-backups/v1", ".history", "node_modules/pkg/dist", ".git"):
        (_p / _d).mkdir(parents=True, exist_ok=True)
        (_p / _d / "index.html").write_bytes(b"<h1>left out</h1>")
    os.symlink(_tgt, _p / "dist")
    try:
        A._site_webroot(_p)
        _got = None
    except A.ShipError as e:
        _got = (e.status, e.detail)
    check(f"web root: dist -> {_tgt} is refused (400), naming the link",
          _got is not None and _got[0] == 400 and "`dist`" in _got[1] and f"`{_tgt}`" in _got[1],
          str(_got))

# A published site never carries dotfiles or .history; .well-known/ is kept.
H1 = WORK / "site-dots"
(H1 / ".history" / "v1").mkdir(parents=True)
(H1 / ".history" / "log.jsonl").write_bytes(b"past revisions")
(H1 / ".history" / "v1" / "index.html").write_bytes(b"<h1>old</h1>")
(H1 / ".well-known" / "acme-challenge").mkdir(parents=True)
(H1 / ".well-known" / "acme-challenge" / "x").write_bytes(b"token")
(H1 / ".well-known" / ".hidden").write_bytes(b"nested dotfile")
(H1 / "assets").mkdir()
(H1 / "assets" / ".env").write_bytes(b"STRIPE_SECRET_KEY=" + SECRET)
(H1 / "assets" / "a.js").write_bytes(REAL)
(H1 / "index.html").write_bytes(b"<h1>now</h1>")
(H1 / ".env").write_bytes(b"STRIPE_SECRET_KEY=" + SECRET)
(H1 / ".gitignore").write_bytes(b"x")
os.symlink(".history", H1 / "past")                   # a link to a hidden folder
mh = zip_members(A._zip_site_dir(H1))
check("site zip: an artifact's .history/ is not published",
      not any(n.startswith(".history") or n.startswith("past") for n in mh)
      and b"past revisions" not in b"".join(mh.values()), str(sorted(mh)))
check("site zip: a root .env and .gitignore are never in it, at any depth",
      not any(n.split("/")[-1].startswith(".") and not n.startswith(".well-known/") for n in mh)
      and not leaks(b"".join(mh.values())), str(sorted(mh)))
check("site zip: .well-known/acme-challenge/x is kept, a dotfile inside it is not",
      mh.get(".well-known/acme-challenge/x") == b"token" and ".well-known/.hidden" not in mh,
      str(sorted(mh)))
check("site zip: the page and assets are still there",
      mh.get("index.html") == b"<h1>now</h1>" and mh.get("assets/a.js") == REAL, str(sorted(mh)))

# An unbuilt bundler project (its root page is a template) is a 400, even
# with an empty dist/; never the project root.
for _nm, _mk in (("site-empty-dist", "real"), ("site-empty-linked", "link")):
    _p = vite_like(_nm)
    if _mk == "real":
        (_p / "dist").mkdir()
    else:
        (_p / "build" / "empty").mkdir()
        os.symlink("build/empty", _p / "dist")
    try:
        A._site_webroot(_p)
        _got = None
    except A.ShipError as e:
        _got = (e.status, e.detail)
    check(f"web root: an unbuilt bundler project with a {_mk} empty dist/ is a 400, not the project root",
          _got is not None and _got[0] == 400 and "bundler template" in _got[1]
          and "build the site first" in _got[1], str(_got))
_p = WORK / "site-no-build"
_p.mkdir()
(_p / "index.html").write_bytes(b"<h1>static</h1>")
(_p / ".env").write_bytes(b"STRIPE_SECRET_KEY=" + SECRET)
root, kind = A._site_webroot(_p)
mnb = zip_members(A._zip_site_dir(root, project_dir=_p))
check("web root: with no build folder the project root is the site, and its .env stays out",
      root == _p and kind == "static" and mnb == {"index.html": b"<h1>static</h1>"}, str(sorted(mnb)))
_p = vite_like("site-other-built")
(_p / "dist").mkdir()
(_p / "build" / "index.html").write_bytes(b"<h1>built</h1>")
root, kind = A._site_webroot(_p)
check("web root: an empty dist/ does not hide a built build/", root == _p / "build", str(root))

# A dot folder that is not left out by name is a fine build folder (VitePress).
_p = vite_like("site-vitepress")
(_p / "docs" / ".vitepress" / "dist").mkdir(parents=True)
(_p / "docs" / ".vitepress" / "dist" / "index.html").write_bytes(b"<h1>docs</h1>")
os.symlink("docs/.vitepress/dist", _p / "dist")
root, kind = A._site_webroot(_p)
check("web root: dist -> docs/.vitepress/dist is the web root",
      root == _p / "docs" / ".vitepress" / "dist" and kind == "static", str(root))
mv = zip_members(A._zip_site_dir(root, project_dir=_p))
check("web root: ... and the site is exactly that page", mv == {"index.html": b"<h1>docs</h1>"}, str(sorted(mv)))

# A DYNAMIC site (privatae.json): the zone builds and runs the zip, so its
# dotfiles stay; .git, .history and .adopt-backups never go.
D = WORK / "dyn-site"
(D / ".streamlit").mkdir(parents=True)
(D / ".streamlit" / "config.toml").write_bytes(b"[server]\n")
(D / ".git").mkdir()
(D / ".git" / "config").write_bytes(b"git")
(D / ".history").mkdir()
(D / ".history" / "log.jsonl").write_bytes(b"past")
(D / ".adopt-backups").mkdir()
(D / ".adopt-backups" / "old.py").write_bytes(b"old")
(D / "privatae.json").write_bytes(b"{}")
(D / "app.py").write_bytes(b"print(1)")
(D / ".env").write_bytes(b"K=v")
(D / ".npmrc").write_bytes(b"registry=x")
root, kind = A._site_webroot(D)
md = zip_members(A._zip_site_dir(root, project_dir=D, dynamic=kind == "dynamic"))
check("dynamic site: .env, .npmrc and .streamlit/config.toml are kept",
      kind == "dynamic" and md.get(".env") == b"K=v" and md.get(".npmrc") == b"registry=x"
      and md.get(".streamlit/config.toml") == b"[server]\n", str(sorted(md)))
check("dynamic site: .git, .history and .adopt-backups are dropped",
      not any(n.split("/")[0] in (".git", ".history", ".adopt-backups") for n in md), str(sorted(md)))
check("dynamic site: the app is there", md.get("app.py") == b"print(1)", str(sorted(md)))
check("static zip of the same tree drops its dotfiles",
      not any(n.startswith(".") for n in zip_members(A._zip_site_dir(D))), "")

# public/ is source assets in most frameworks.
_p = WORK / "site-plain-public"
(_p / "public").mkdir(parents=True)
(_p / "public" / "logo.png").write_bytes(b"png")
(_p / "index.html").write_bytes(b"<h1>plain</h1>")
(_p / ".env").write_bytes(b"STRIPE_SECRET_KEY=" + SECRET)
root, kind = A._site_webroot(_p)
mp = zip_members(A._zip_site_dir(root, project_dir=_p))
check("web root: a static site with a public/ assets folder publishes the root, minus dotfiles",
      root == _p and set(mp) == {"index.html", "public/logo.png"} and not leaks(b"".join(mp.values())),
      str((root, sorted(mp))))
_p = WORK / "site-unbuilt-vite"
(_p / "public").mkdir(parents=True)
(_p / "public" / "favicon.ico").write_bytes(b"ico")
(_p / "src").mkdir()
(_p / "index.html").write_bytes(
    b'<div id=app></div><script type="module" src="/src/main.ts"></script>')
(_p / "package.json").write_bytes(b'{"scripts": {"build": "vite build"}}')
for _variant in ("no dist", "empty dist"):
    if _variant == "empty dist":
        (_p / "dist").mkdir()
    try:
        A._site_webroot(_p)
        _got = None
    except A.ShipError as e:
        _got = (e.status, e.detail)
    check(f"web root: an unbuilt Vite project ({_variant}, module script to /src/main.ts) gets the build-first 400",
          _got is not None and _got[0] == 400 and "build" in _got[1] and "first" in _got[1], str(_got))
for _nm, _tag in (("src-dir", b'<script type=module src=./src/app.js></script>'),
                  ("vue", b"<script src='/main.vue' type='module'></script>"),
                  ("tsx", b'<script type="module" src="app.tsx?x=1"></script>')):
    _q = WORK / f"site-tpl-{_nm}"
    _q.mkdir()
    (_q / "index.html").write_bytes(b"<div id=r></div>" + _tag)
    (_q / "package.json").write_bytes(b"{}")      # the src/ half needs a package.json
    try:
        A._site_webroot(_q)
        _got = None
    except A.ShipError as e:
        _got = e.status
    check(f"web root: a root page with a module script ({_nm}) is a bundler template", _got == 400, str(_got))
_p = WORK / "site-hugo"
(_p / "public").mkdir(parents=True)
(_p / "public" / "index.html").write_bytes(b"<h1>hugo</h1>")
(_p / "package.json").write_bytes(b'{"scripts": {"build": "hugo"}}')
root, kind = A._site_webroot(_p)
check("web root: a Hugo-shaped public/index.html publishes public/",
      root == _p / "public" and kind == "static", str(root))
# A hand-written site whose build only compiles assets publishes its root.
_p = WORK / "site-sass"
(_p / "scss").mkdir(parents=True)
(_p / "css").mkdir()
(_p / "scss" / "a.scss").write_bytes(b"a{b:c}")
(_p / "css" / "a.css").write_bytes(b"a{b:c}")
(_p / "index.html").write_bytes(b'<link rel=stylesheet href=css/a.css><script src="app.js"></script>')
(_p / "package.json").write_bytes(b'{"scripts": {"build": "sass scss:css"}}')
check("web root: a hand-written site whose build only runs sass publishes the root",
      A._site_webroot(_p) == (_p, "static"), "")
_p = WORK / "site-dist-css"
(_p / "dist").mkdir(parents=True)
(_p / "dist" / "output.css").write_bytes(b"a{b:c}")
(_p / "index.html").write_bytes(b"<link rel=stylesheet href=dist/output.css>")
(_p / "package.json").write_bytes(b'{"scripts": {"build": "tailwindcss -o dist/output.css"}}')
check("web root: root index.html plus dist/output.css (no page in dist/) publishes the root",
      A._site_webroot(_p) == (_p, "static"), "")

# The src/ half of the template rule needs a package.json; a classic script
# (not type=module) is never a template.
_q = WORK / "site-buildless-src"
(_q / "src").mkdir(parents=True)
(_q / "src" / "app.js").write_bytes(REAL)
(_q / "index.html").write_bytes(b'<div id=r></div><script type="module" src="src/app.js"></script>')
def _webroot_or_status(d):
    try:
        return A._site_webroot(d)
    except A.ShipError as e:
        return e.status


check("web root: a buildless site (module script from src/, no package.json) publishes",
      _webroot_or_status(_q) == (_q, "static"), str(_webroot_or_status(_q)))
(_q / "package.json").write_bytes(b"{}")
try:
    A._site_webroot(_q)
    _got = None
except A.ShipError as e:
    _got = e.status
check("web root: the same page with a package.json is refused (build first)", _got == 400, str(_got))
_q = WORK / "site-classic-src"
(_q / "src").mkdir(parents=True)
(_q / "src" / "app.js").write_bytes(REAL)
(_q / "index.html").write_bytes(b'<div id=r></div><script src="src/app.js"></script>')
(_q / "package.json").write_bytes(b'{"scripts": {"build": "x"}}')
check("web root: a classic <script src=src/app.js> page publishes (only module scripts count)",
      _webroot_or_status(_q) == (_q, "static"), str(_webroot_or_status(_q)))

# The dynamic flag reaches the zip through the REAL services, so dropping it
# from either caller fails here (stubs stand in for the database, the git
# host and the publisher; _zip_site_dir and _site_webroot are the real ones).
import base64 as _b64mod                                  # noqa: E402
import shutil as _shutil                                  # noqa: E402


class _Conn:
    async def fetchrow(self, *_a):
        return {"id": 1, "entity_id": 1, "name": "p", "status": "ready", "kind": "project"}


class _Acq:
    async def __aenter__(self):
        return _Conn()

    async def __aexit__(self, *_a):
        return False


class _Pool:
    def acquire(self):
        return _Acq()

    async def fetch(self, *_a):
        return []


_sent: list = []


async def _fake_publish(op, params):
    _sent.append(zip_members(_b64mod.b64decode(params["zip_b64"])))
    return {"hostname": "x.example"}


async def _noop(*_a, **_k):
    return None


def _service_zip(kind: str) -> dict:
    """The member map each real service publishes for a `kind` project."""
    src = WORK / f"svc-{kind}"
    (src / ".git").mkdir(parents=True)
    (src / ".git" / "config").write_bytes(b"git")
    (src / ".env").write_bytes(b"K=v")
    (src / ".streamlit").mkdir()
    (src / ".streamlit" / "config.toml").write_bytes(b"[server]\n")
    (src / "index.html").write_bytes(b"<h1>x</h1>")
    if kind == "dynamic":
        (src / "privatae.json").write_bytes(b"{}")
    saved = (A._module_dir_for, A._license_gate_site, A.publisher_op_service,
             A._entity_git, A.brain_service.get_pool, dict(os.environ))
    out = {}

    async def _mod_dir(*_a, **_k):
        return src

    def _clone(args, **_k):
        _shutil.copytree(src, args[-1], dirs_exist_ok=True, symlinks=True)

        class R:
            returncode, stderr = 0, ""
        return R()

    A._module_dir_for, A._license_gate_site, A.publisher_op_service = _mod_dir, _noop, _fake_publish
    A._entity_git = _clone
    A.brain_service.get_pool = lambda: _Pool()
    from server.bridge import memory_handlers as _mh
    _saved_inbox = _mh.handle_inbox_append
    _mh.handle_inbox_append = _noop          # the reach-out inbox row is not under test
    os.environ.update(GITEA_API_TOKEN="t", GITEA_URL="https://git.example")
    try:
        _sent.clear()
        run(A.publish_site_service(_Pool(), 1, slug="s"))
        out["project"] = _sent[-1]
        run(A.publish_site_repo_service(repo="o/r", slug="s"))
        out["repo"] = _sent[-1]
    finally:
        (A._module_dir_for, A._license_gate_site, A.publisher_op_service,
         A._entity_git, A.brain_service.get_pool) = saved[:5]
        _mh.handle_inbox_append = _saved_inbox
        os.environ.clear()
        os.environ.update(saved[5])
    return out


for _kind in ("dynamic", "static"):
    _svc = _service_zip(_kind)
    for _lane, _m in _svc.items():
        if _kind == "dynamic":
            _ok = (_m.get(".env") == b"K=v" and ".streamlit/config.toml" in _m
                   and not any(n.startswith(".git/") for n in _m))
        else:
            _ok = not any(n.startswith(".") for n in _m) and "index.html" in _m
        check(f"publish service ({_lane} lane): a {_kind} site's zip has the right dotfiles",
              _ok, str(sorted(_m)))

# ── 3. Install as my app / publish (bundles.pack) ──────────────────────────
print("3. app bundle (pack)")
from server.applications import bundles               # noqa: E402

B = WORK / "app"
plant(B, skipped_dir=".git")                           # pack leaves hidden top-level folders out
(B / "app.yaml").write_bytes(b"id: linkapp\nname: Link App\nversion: 0.0.1\n")
plant(B / "payload", skipped_dir=None)
os.symlink(OUT / "sub", B / "bin")                     # a linked bin/ folder
try:
    bundles.pack(B)
    pack_err = None
except bundles.BundleError as e:
    pack_err = str(e)
check("pack: links it cannot carry are refused, not dropped",
      pack_err is not None and not leaks(pack_err.encode()), str(pack_err))
check("pack: the refusal names each of them",
      bool(pack_err) and all(f"`{n}`" in pack_err for n in
                             ("abs.js", "escape.js", "linkdir", "bin", "payload/abs.js",
                              "into-skipped.js")), str(pack_err))
# The reviewer's probe: in-tree links, a linked payload/ and a linked folder
# at the top, pack exactly what main packed.
B2 = WORK / "app-in-tree"
(B2 / "dist" / "js").mkdir(parents=True)
(B2 / "app.yaml").write_bytes(b"id: probe\nname: Probe\nversion: 0.0.1\n")
(B2 / "dist" / "index.html").write_bytes(b"<h1>app</h1>")
(B2 / "dist" / "js" / "app.js").write_bytes(b"console.log(1)")
(B2 / "real.js").write_bytes(REAL)
os.symlink("real.js", B2 / "same.js")
os.symlink("dist", B2 / "payload")                     # payload/ is the build output
os.symlink("dist/js", B2 / "js")                       # a linked folder at the top
c = bundles._read_zip(bundles.pack(B2))
check("pack: in-tree linked folders and files are carried, as on main",
      sorted(c.payload) == ["dist/index.html", "dist/js/app.js", "index.html", "js/app.js",
                            "real.js", "same.js"]
      and c.payload["js/app.js"] == b"console.log(1)" and c.payload["index.html"] == b"<h1>app</h1>"
      and c.payload["same.js"] == REAL and c.binaries == {}, str(sorted(c.payload)))
B3 = WORK / "app-loop"
B3.mkdir()
(B3 / "app.yaml").write_bytes(b"id: loop\nname: Loop\nversion: 0.0.1\n")
os.symlink(".", B3 / "self")
(B3 / "a" / "b").mkdir(parents=True)
os.symlink("..", B3 / "a" / "b" / "up")               # back into a folder that holds it
try:
    bundles.pack(B3)
    loop_err = None
except bundles.BundleError as e:
    loop_err = str(e)
check("pack: a linked folder that loops back is refused by name",
      bool(loop_err) and "`self` → `.` (loops back" in loop_err
      and "`a/b/up` → `..` (loops back" in loop_err, str(loop_err))
(OUT / "app").mkdir()
(OUT / "app" / "app.yaml").write_bytes(b"id: elsewhere\nname: Elsewhere\nversion: 0.0.1\n")
(OUT / "app" / "key.js").write_bytes(SECRET)
os.symlink(OUT / "app", WORK / "applink")
try:
    c2 = bundles._read_zip(bundles.pack(WORK / "applink"))
    refused = not leaks(b"".join(c2.payload.values()))
except bundles.BundleError:
    refused = True
check("pack: a source folder that is itself a link is refused", refused)

# ── 4. The code index (indexer.index_project) ──────────────────────────────
print("4. code index")
from codesearch import bridge_embed, indexer, store   # noqa: E402

I = WORK / "repo"
plant(I)
sent: list[str] = []
_saved = (store.get_file_hashes, store.get_file_state, store.delete_file, store.upsert_chunks,
          store.replace_file, bridge_embed.embed_many, bridge_embed.active_space)


async def _hashes(db, project, space=None):
    return {}


async def _delete(db, project, file_path):
    return 0


async def _upsert(db, chunks, space=None):
    return len(chunks)


async def _replace(db, project, file_path, chunks, space=None, unembedded=False):
    return len(chunks)


async def _space():     # a model is set up, so the index sends text to the embedder
    return store.Space(model="nomic-embed-text", dim=768), ""
store.get_file_hashes, store.get_file_state, store.delete_file, store.upsert_chunks, store.replace_file = (
    _hashes, _hashes, _delete, _upsert, _replace)


async def _embed(texts, *, space=None):
    sent.extend(texts)
    return [[0.0] for _ in texts]
bridge_embed.embed_many, bridge_embed.active_space = _embed, _space
try:
    summary = run(indexer.index_project(None, project="owner/repo", project_root=str(I)))
finally:
    (store.get_file_hashes, store.get_file_state, store.delete_file, store.upsert_chunks,
     store.replace_file, bridge_embed.embed_many, bridge_embed.active_space) = _saved
joined = "\n".join(sent).encode()
check("index: nothing outside the repo reaches the embedder", SECRET not in joined, str(summary))
check("index: the real file is indexed", b"const real = 1" in joined, str(summary))
check("index: a link is indexed once, under its target's own path (not followed)",
      joined.count(b"const real = 1") == 1, str(joined.count(b"const real = 1")))

# ── 5. Artifact history (snapshot + revert) ────────────────────────────────
print("5. artifact history")
from server.applications import artifacts as AR       # noqa: E402

PROJECTS = WORK / "projects"
A.PROJECTS_ROOT = str(PROJECTS)
W = PROJECTS / "77"
plant(W, skipped_dir=".history")                       # history never snapshots itself
rev = AR._snapshot(W, actor="test", op="write")
snap = W / ".history" / str(rev) if rev else W / ".history"
check("snapshot: taken", rev is not None)
check("snapshot: no secret copied into the revision", not leaks(files_under(snap)),
      str(sorted(os.listdir(snap))) if snap.is_dir() else "")
check("snapshot: an in-tree link is not copied as a second file", not (snap / "same.js").exists())
# A revision with links planted in it, and a linked folder in the workspace
# that the revision's files would be written through.
plant(snap / "planted", skipped_dir=None)
(snap / "linkdir2").mkdir()
(snap / "linkdir2" / "planted.js").write_bytes(b"// from the revision\n")
os.symlink(OUT / "sub", W / "linkdir2")


class _Pool:
    async def fetchrow(self, *a):
        return {"id": 77, "kind": "artifact"}

    async def execute(self, *a):
        return "UPDATE 1"


before = sorted(os.listdir(OUT / "sub"))
try:
    run(AR.revert_artifact(_Pool(), 3, 77, str(rev)))
    reverted = True
except Exception as e:  # noqa: BLE001
    reverted = repr(e)
check("revert: ran", reverted is True, str(reverted))
live = b"".join(Path(dp, fn).read_bytes() for dp, _d, fns in os.walk(W)
               if ".history" not in Path(dp).relative_to(W).parts
               for fn in fns if stat.S_ISREG(os.lstat(Path(dp, fn)).st_mode))
check("revert: no secret written into the workspace", not leaks(live))
check("revert: nothing written through a linked folder in the workspace",
      sorted(os.listdir(OUT / "sub")) == before, str(sorted(os.listdir(OUT / "sub"))))

# ── 6. The harness worktree seed (review._prepare_worktree) ───────────────
print("6. harness worktree seed")
import server.routes.review as RV                     # noqa: E402

SEED = WORK / "seed-project"
# No FIFO: copytree has always refused one (shutil.Error), link or not.
plant(SEED, skipped_dir=".git", fifo=False)
plant(SEED / "src", skipped_dir=None, fifo=False)
os.symlink(OUT / "sub", SEED / "cfg")                 # a TOP-LEVEL link to a folder
wt = RV._prepare_worktree(WORK / "runs", "codex", seed=True, seed_from=SEED)
check("seed: no secret in any real file of the worktree", not leaks(files_under(wt)))
check("seed: a top-level linked folder stays a link", (wt / "cfg").is_symlink(),
      "copied as a folder" if (wt / "cfg").is_dir() and not (wt / "cfg").is_symlink() else "")
check("seed: links inside folders stay links", (wt / "src" / "abs.js").is_symlink())
check("seed: a link keeps its own target text", os.readlink(wt / "abs.js") == str(OUT / "secret.txt"))

# A folder swapped for a link while the seed copy runs, at exactly the moment
# the walk opens it (tree_walk._at_open, the walk's hook for this).
SEED2 = WORK / "seed-swap"
(SEED2 / "src" / "deep").mkdir(parents=True)
(SEED2 / "src" / "deep" / "a.js").write_bytes(REAL)
(SEED2 / "run.sh").write_bytes(b"#!/bin/sh\necho hi\n")
os.chmod(SEED2 / "run.sh", 0o755)
os.mkfifo(SEED2 / "pipe")
swapped: list = []
_hook = tw._at_open


def _swap(tree, parts):
    if parts == ("src", "deep") and not swapped:
        os.rename(SEED2 / "src" / "deep", SEED2 / "src" / "deep.real")
        os.symlink(OUT / "sub", SEED2 / "src" / "deep")
        swapped.append(parts)


tw._at_open = _swap
try:
    wt2 = RV._prepare_worktree(WORK / "runs2", "codex", seed=True, seed_from=SEED2)
    seeded_ok = True
except Exception as e:  # noqa: BLE001
    wt2, seeded_ok = WORK / "runs2" / "codex", repr(e)
finally:
    tw._at_open = _hook
check("seed swap: the copy ran (a FIFO in the project is left out, not fatal)",
      seeded_ok is True and not (wt2 / "pipe").exists(), str(seeded_ok))
check("seed swap: the folder was swapped for a link at the moment it was opened", bool(swapped))
check("seed swap: nothing behind the swapped-in link was copied",
      not leaks(files_under(wt2)) and not (wt2 / "src" / "deep" / "inner.js").exists(),
      str(sorted(os.listdir(wt2 / "src" / "deep"))) if (wt2 / "src" / "deep").is_dir() else "")
check("seed: permission bits are kept (as copy2 kept them)",
      (wt2 / "run.sh").exists() and os.stat(wt2 / "run.sh").st_mode & 0o111 == 0o111)

# ── 7. Host backup / export (host_backup._add_member, _add_apps) ──────────
print("7. backup and export writer")
from server import host_backup as hb                  # noqa: E402

H = WORK / "state"
plant(H / "apps" / "a1")


def tar_bytes(fn) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        fn(t)
    out = b""
    with tarfile.open(fileobj=io.BytesIO(buf.getvalue())) as t:
        for mem in t.getmembers():
            if mem.isreg():
                out += t.extractfile(mem).read()
    return out


files: list = []
check("backup: links are stored as links", not leaks(tar_bytes(lambda t: hb._add_apps(t, str(H), files))))
# A name lstat saw as a regular file, a link by the time it is opened.
real_lstat, target_st = os.lstat, os.stat(OUT / "secret.txt")
leaf = str(H / "apps" / "a1" / "abs.js")
os.lstat = lambda p, *a, **k: target_st if str(p) == leaf else real_lstat(p, *a, **k)
try:
    notes: dict = {}
    got = tar_bytes(lambda t: hb._add_member(t, leaf, "apps/a1/abs.js", [], notes))
finally:
    os.lstat = real_lstat
check("backup: a file swapped for a link after lstat is not followed", not leaks(got), str(notes))
# A folder on the way swapped for a link after the walk listed it.
SW = H / "apps" / "a2"
os.symlink(OUT / "sub", SW)
real_walk = os.walk


def _walk(top, *a, **k):
    if os.path.realpath(top) == os.path.realpath(H):
        yield str(H), ["apps"], []
        yield str(H / "apps"), ["a2"], []
        yield str(SW), [], ["inner.js"]             # listed as a real folder before the swap
        return
    yield from real_walk(top, *a, **k)


os.walk = _walk
try:
    got = tar_bytes(lambda t: hb._add_apps(t, str(H), []))
finally:
    os.walk = real_walk
check("backup: a folder swapped for a link mid-walk is not followed", not leaks(got))

# ── 8. local_grep (the Host's grep over its codespace) ─────────────────────
print("8. local_grep")
from mastercode.runtime import tools as TL            # noqa: E402

G = WORK / "codespace"
plant(G / "projects" / "5")
os.symlink(OUT, G / "projects" / "5" / "notes")
_root = TL._ENTITY_CODESPACE_ROOT
TL._ENTITY_CODESPACE_ROOT = str(G)
try:
    r_dir = run(TL._exec_local_grep({"pattern": SECRET.decode(), "path": "projects/5/notes"}))
    r_file = run(TL._exec_local_grep({"pattern": SECRET.decode(), "path": str(G / "projects/5/abs.js")}))
    r_tree = run(TL._exec_local_grep({"pattern": SECRET.decode(), "path": "projects"}))
    r_ok = run(TL._exec_local_grep({"pattern": "const real", "path": "projects/5"}))
finally:
    TL._ENTITY_CODESPACE_ROOT = _root
check("local_grep: a linked folder named as the path is refused",
      r_dir.get("status") == "denied" and SECRET.decode() not in str(r_dir), str(r_dir)[:200])
check("local_grep: a linked file named as the path is refused",
      r_file.get("status") == "denied" and SECRET.decode() not in str(r_file), str(r_file)[:200])
check("local_grep: a recursive grep never follows a link it finds",
      r_tree.get("status") == "ok" and r_tree["result"]["matches"] == [], str(r_tree)[:300])
check("local_grep: real files still match", r_ok.get("status") == "ok" and r_ok["result"]["matches"],
      str(r_ok)[:200])

# ── 9. A linked .history (artifact history writes) ────────────────────────
print("9. a linked .history")
import json                                           # noqa: E402


def state(d: Path) -> list:
    return sorted((str(p.relative_to(d)), p.read_bytes() if p.is_file() and not p.is_symlink() else None)
                  for p in d.rglob("*"))


W2 = PROJECTS / "78"
W2.mkdir(parents=True)
(W2 / "index.html").write_bytes(b"<p>art</p>\n")
HIST = OUT / "hist"
(HIST / "0001").mkdir(parents=True)
(HIST / "0001" / "index.html").write_bytes(b"<p>" + SECRET + b"</p>")
(HIST / "log.jsonl").write_text(json.dumps({"rev": "0001", "ts": "t", "actor": "a", "op": "write",
                                            "note": SECRET.decode(), "files": 1, "digest": "x"}) + "\n")
(HIST / "design-trial").write_text("7 abc")
os.symlink(HIST, W2 / ".history")
hist_before = state(HIST)
check("history: a linked .history reads as no history", AR._history_log(W2) == [])
listed = run(AR.list_history(_Pool(), 3, 78))
check("history: the list shows nothing from behind the link", SECRET.decode() not in json.dumps(listed),
      str(listed)[:200])
try:
    run(AR.render_history(_Pool(), 3, 78, "0001"))
    rendered = "rendered"
except AR.ArtifactError as e:
    rendered = e.args[0] if e.args else None
    rendered = getattr(e, "status", None) or rendered
check("history: a revision behind a linked .history is not rendered (404)", rendered == 404, str(rendered))
check("history: a trial mark is not read through the link", AR.read_history_file(W2, "design-trial") is None)
AR.remove_history_file(W2, "design-trial")
check("history: removing a trial mark removes nothing behind the link", (HIST / "design-trial").exists())
rev2 = AR._snapshot(W2, actor="test", op="write")
check("snapshot: a linked .history is replaced by a real folder",
      (W2 / ".history").is_dir() and not (W2 / ".history").is_symlink())
check("snapshot: history goes on recording, in the real folder",
      rev2 == "0001" and (W2 / ".history" / "0001" / "index.html").read_bytes() == b"<p>art</p>\n", str(rev2))
check("snapshot: nothing behind the link was written, removed or changed", state(HIST) == hist_before)
AR.write_history_file(W2, "design-trial", b"7 def")
check("history: a trial mark is written into the real folder",
      AR.read_history_file(W2, "design-trial") == b"7 def" and (HIST / "design-trial").read_text() == "7 abc")
# A file inside a real .history swapped for a link is never written through.
W5 = PROJECTS / "81"
(W5 / ".history").mkdir(parents=True)
(W5 / "a.html").write_text("x")
(OUT / "log-victim").write_text("untouched\n")
os.symlink(OUT / "log-victim", W5 / ".history" / "log.jsonl")
AR._snapshot(W5, actor="t", op="w")
check("snapshot: a log.jsonl that is a link is not appended through",
      (OUT / "log-victim").read_text() == "untouched\n")
# A FILE named .history is not ours to remove.
W3 = PROJECTS / "79"
W3.mkdir()
(W3 / "a.html").write_text("x")
(W3 / ".history").write_text("not ours")
check("snapshot: a file named .history is left as it is (no revision)",
      AR._snapshot(W3, actor="t", op="w") is None and (W3 / ".history").read_text() == "not ours")
# The prune removes only revision-shaped names, inside .history.
W4 = PROJECTS / "80"
(W4 / "victim").mkdir(parents=True)
(W4 / "victim" / "keep.txt").write_text("keep")
(W4 / ".history").mkdir()
(W4 / ".history" / "log.jsonl").write_text("".join(
    json.dumps({"rev": r, "digest": f"d{i}"}) + "\n"
    for i, r in enumerate(["../victim"] + [f"{n:04d}" for n in range(2, 22)])))
rev4 = AR._snapshot(W4, actor="t", op="w")
check("prune: a log entry naming ../victim removes nothing outside .history",
      rev4 == "0022" and (W4 / "victim" / "keep.txt").exists(), str(rev4))

# ── 10. A build refused for links out of the project ───────────────────────
print("10. build refused for links out of the project")
from fastapi import HTTPException                     # noqa: E402

BP = WORK / "bprojects"
P = BP / "9240"
(P / "src").mkdir(parents=True)
(P / "real.js").write_bytes(REAL)
os.symlink(OUT / "sub", P / "cfg")                                            # absolute
os.symlink(os.path.relpath(OUT / "secret.txt", P / "src"), P / "src" / "up.js")  # ../ escape
os.symlink("../real.js", P / "src" / "same.js")                               # in-tree
(P / ".git").mkdir()
os.symlink("/etc/passwd", P / ".git" / "x")                                   # never seeded
msg = RV.links_out_refusal(P)
check("refusal: names every link out, by path and target",
      bool(msg) and f"`cfg` → `{OUT / 'sub'}`" in msg and "`src/up.js` → `" in msg, msg)
check("refusal: leaves out in-tree links and what is never seeded",
      bool(msg) and "same.js" not in msg and ".git" not in msg, msg)
check("refusal: says what to do", bool(msg) and "Remove each link" in msg and "replace each one" in msg
      and "then build again" in msg, msg)
INSIDE = BP / "242"
(INSIDE / "a").mkdir(parents=True)
(INSIDE / "a" / "x.js").write_bytes(REAL)
os.symlink("a/x.js", INSIDE / "y.js")
os.symlink("../a", INSIDE / "a" / "loop")            # climbs, but stays inside
check("refusal: none for a tree whose links stay inside", RV.links_out_refusal(INSIDE) is None,
      RV.links_out_refusal(INSIDE))
MANY = BP / "241"
MANY.mkdir()
for i in range(12):
    os.symlink(f"/nowhere/{i}", MANY / f"l{i:02d}")
many = RV.links_out_refusal(MANY) or ""
check("refusal: a long list is cut with a count", "and 2 more" in many and "`l09`" in many
      and "`l10`" not in many, many[:300])

_saved_b = (RV.PROJECTS_ROOT, RV.resolve_build_target, TL._run_phase_build, TL._pool_build,
            TL._get_runtime_dispatch)
RV.PROJECTS_ROOT = BP
resolved: list = []


async def _resolve(h, model=None):
    resolved.append(h)
    return (h or "codex"), None, None


class _BuildPool:
    """Records every statement; owns project 9240; has no phases."""

    def __init__(self):
        self.calls: list = []

    async def fetchval(self, sql, *a):
        self.calls.append((sql, a))
        if "FROM projects WHERE id=$1 AND entity_id=$2" in sql:
            return 1
        return None

    async def fetchrow(self, sql, *a):
        self.calls.append((sql, a))
        if "INSERT INTO entity_tasks" in sql:
            return {"task_id": 77}
        return None

    async def execute(self, sql, *a):
        self.calls.append((sql, a))
        return "UPDATE 1"

    async def fetch(self, sql, *a):
        self.calls.append((sql, a))
        return []


RV.resolve_build_target = _resolve
try:
    # The build primitive: refused before any pod, on the bench's feed.
    acts: list = []

    async def _on_act(m):
        acts.append(m)
    out = run(RV.run_single_harness_build(project_id=9240, prompt="x", on_activity=_on_act))
    check("build: refused before any engine is resolved or pod woken",
          out.get("ok") is False and out.get("links_out") and not resolved, out)
    check("build: the refusal is the message", out.get("error") == msg, out.get("error"))
    check("build: the bench's activity feed says no build ran, and why",
          acts == [f"no build ran: {msg}"], acts)
    # The envelope the build registry row and the report are made from.
    env = run(TL._pool_build(entity_id=9003, pool=_BuildPool(), project_id=9240, prompt="x",
                             research_id=None, timeout_s=600, one_shot=True))
    check("envelope: an error that says no build ran and names the links",
          env.get("status") == "error" and env.get("result") == f"no build ran: {msg}"
          and env.get("links_refused") is True, env)
    check("envelope: no continue advice for a build that never ran",
          "continue" not in env.get("result", ""), env.get("result"))
    # The chat door: the Host's tool result, before any row opens.
    started: list = []

    async def _no_phase_build(**kw):
        started.append(kw)
    TL._run_phase_build = _no_phase_build
    door_pool = _BuildPool()
    door = run(TL._exec_build({"action": "run", "prompt": "make it", "harness": "codex"},
                              3, door_pool, "sess-1", "project:9240"))
    check("door (run): the Host's tool result is the refusal, naming the links",
          door.get("status") == "error" and msg in door.get("result", "")
          and "not started" in door.get("result", ""), door)
    check("door (run): no build row opened and nothing started",
          not started and not any("INSERT INTO entity_tasks" in q for q, _a in door_pool.calls))
    # The other two doors: a follow-up (continue) and a phase build. The phase
    # pool is the one the phase-spec gate drives the build door with.
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("phase_spec_gate", REPO / "scripts" / "test_phase_spec_gate.py")
    psg = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(psg)          # main() is __main__-guarded

    class _ClaimPool(psg.BuildPool):
        """psg's pool, also recording a phase claim (the door's UPDATE … RETURNING)."""
        claimed = False

        async def fetchval(self, sql, *args):
            if "UPDATE project_phases SET status=" in sql:
                self.claimed = True
            return await super().fetchval(sql, *args)

    def _phase_pool(status="ready"):
        return _ClaimPool({"id": 7, "phase_number": 2, "title": "Backend", "description": None,
                           "spec": "brief", "status": status, "gate": "none",
                           "approved_at": None, "acceptance": None})
    # A continue follows up a phase that LANDED (build-continue: with nothing
    # landed it refuses before anything else), so its plan has one.
    bp = _phase_pool("complete")
    cont = run(TL._exec_build({"action": "continue", "project_id": 9240, "message": "add a footer"},
                              3, bp, "sess-1", None))
    check("door (continue): the Host's tool result is the refusal, naming the links",
          cont.get("status") == "error" and msg in cont.get("result", "")
          and "not started" in cont.get("result", ""), cont)
    check("door (continue): no build row opened and nothing started", not started and not bp.started)
    bp = _phase_pool()
    phase = run(TL._exec_build({"action": "build", "project_id": 9240}, 3, bp, "sess-1", None))
    check("door (phase build): the Host's tool result is the refusal, naming the phase and the links",
          phase.get("status") == "error" and msg in phase.get("result", "")
          and "phase 2 ('Backend') not started" in phase.get("result", ""), phase)
    check("door (phase build): no phase claimed, no row opened, nothing started",
          not started and not bp.started and not bp.claimed)
    # The chain side: auto-advance, the seeded retry and repairs reach the
    # detached body directly. The Workbench reads the row and its activity;
    # the Host reads the delivered report.
    delivered: list = []
    pooled: list = []

    class _Disp:
        async def _deliver(self, tid):
            delivered.append(tid)

    async def _no_pool_build(**kw):
        pooled.append(kw)
        return {"status": "ok"}
    TL._get_runtime_dispatch = lambda: _Disp()
    TL._pool_build = _no_pool_build
    chain_pool = _BuildPool()
    run(_saved_b[2](entity_id=9003, pool=chain_pool, session_id="s", task_id=91, project_id=9240,
                    phase_id=None, intent="x", research_id=None, timeout_s=600,
                    harness="codex", one_shot=True))
    _act_rows = [a for q, a in chain_pool.calls if "'{activity}'" in q]
    _failed = [a for q, a in chain_pool.calls if "status='failed'" in q]
    _stopped = [a for q, a in chain_pool.calls if "chain_stopped" in q]
    check("chain: no build ran (no pool build)", not pooled, pooled)
    check("chain: the bench's activity feed carries the refusal",
          any(e.get("tool") == f"no build ran: {msg}" for a in _act_rows for e in json.loads(a[0])),
          _act_rows)
    check("chain: the build row fails with the refusal as its error, marked as never run",
          bool(_failed) and _failed[0][0] == f"no build ran: {msg}"
          and json.loads(_failed[0][2]).get("links_refused") is True, _failed)
    check("chain: the reason rides the report, which is delivered to the Host",
          any(a[1] == f"no build ran: {msg}" for a in _stopped) and delivered == [91],
          (_stopped, delivered))
    check("chain: the phase is never touched",
          not any("project_phases" in q and "UPDATE" in q.upper() for q, _a in chain_pool.calls))
    # A compare (fan_out): refused before any session, relayed to the Host.
    from server.routes import _preference_profile as _pp
    from server.routes import harnesses as _hn
    _saved_c = (RV._installed_harnesses, RV._coding_harness_settings, _hn.harness_validity,
                _hn.platform_refresh_enabled, _pp.ban_for, RV.SESSIONS_ROOT, RV._fanout_capacity)

    async def _installed():
        return ["codex"]

    async def _no_settings():
        return {}

    async def _off():
        return False

    async def _no_cap(*a, **k):
        return None
    try:
        RV._installed_harnesses = _installed
        RV._coding_harness_settings = _no_settings
        _hn.harness_validity = lambda pid: {"valid": True, "present": True}
        _hn.platform_refresh_enabled = _off
        _pp.ban_for = lambda c: None
        RV.SESSIONS_ROOT = WORK / "sessions"
        RV.SESSIONS_ROOT.mkdir()
        RV._fanout_capacity = _no_cap
        try:
            run(RV.fan_out(RV.FanOutReq(prompt="x", variants=[{"harness": "codex"}], project_id=9240)))
            fo = None
        except HTTPException as e:
            fo = (e.status_code, e.detail)
        check("compare: fan_out refuses with the message before any session",
              fo == (409, msg) and not any(RV.SESSIONS_ROOT.iterdir()), fo)
        cmp = run(TL._exec_build({"action": "run", "prompt": "x", "variants": [{"harness": "codex"}]},
                                 3, _BuildPool(), "sess-1", "project:9240"))
        check("compare: the Host's tool result relays it", cmp.get("status") == "error"
              and msg in cmp.get("result", ""), cmp)
    finally:
        (RV._installed_harnesses, RV._coding_harness_settings, _hn.harness_validity,
         _hn.platform_refresh_enabled, _pp.ban_for, RV.SESSIONS_ROOT, RV._fanout_capacity) = _saved_c
finally:
    (RV.PROJECTS_ROOT, RV.resolve_build_target, TL._run_phase_build, TL._pool_build,
     TL._get_runtime_dispatch) = _saved_b

# ── 11. The contract scanners (build_contract, api_contract) ──────────────
print("11. contract scanners")
from server.applications import api_contract, build_contract   # noqa: E402

LEAK = "leak-" + os.urandom(4).hex()
(OUT / "scan.js").write_text(f'fetch("https://{LEAK}.example/x"); fetch("/api/{LEAK}/y");\n')
SC = WORK / "scan-project"
SC.mkdir()
(SC / "real.js").write_text('fetch("https://ok.example/x"); fetch("/api/ok/y");\n')
os.symlink(OUT / "scan.js", SC / "abs.js")
os.symlink(os.path.relpath(OUT / "scan.js", SC), SC / "escape.js")
os.symlink("real.js", SC / "same.js")
viol = build_contract._scan_app_sources(SC, [])
calls = api_contract._extract_api_calls(SC)
check("build contract: nothing outside the project reaches its messages",
      LEAK not in " ".join(viol) and any("ok.example" in v for v in viol), str(viol))
check("api contract: nothing outside the project reaches its calls",
      LEAK not in str(calls) and ("real.js", "/api/ok/y", None) in calls, str(calls))
check("contracts: an in-tree link is still scanned",
      any(v.startswith("same.js:") for v in viol) and ("same.js", "/api/ok/y", None) in calls,
      str((viol, calls)))

# The cap on linked folders one walk follows: 300 links give 256 followed and
# the rest refused by name; with the cap lifted all 300 are followed, so a gate
# that passes only while the cap is in force.
CAP = WORK / "cap"
(CAP / "d").mkdir(parents=True)
(CAP / "d" / "f.js").write_bytes(REAL)
for _i in range(300):
    os.symlink("d", CAP / f"l{_i:03d}")
_ref: list = []
with tw.open_tree_no_escape(CAP) as tree:
    _got = [f.rel for f in tree.walk(links=tw.LINKS_IN_TREE, refused=_ref)]
_via = [r for r in _got if r.startswith("l")]
check("walk cap: 300 linked folders give 256 followed", len(_via) == 256 == tw._MAX_LINKED_DIRS,
      f"{len(_via)} followed, cap {tw._MAX_LINKED_DIRS}")
check("walk cap: the other 44 are refused by name, none dropped silently",
      len(_ref) == 44 and all("past the 256 linked folders" in why for _r, _t, why in _ref)
      and sorted(_via + [r for r, _t, _w in _ref]) != [] and len({r.split("/")[0] for r in _via} | {r for r, _t, _w in _ref}) == 300,
      str(len(_ref)))
_cap = tw._MAX_LINKED_DIRS
tw._MAX_LINKED_DIRS = 10 ** 6
try:
    with tw.open_tree_no_escape(CAP) as tree:
        _all = [f.rel for f in tree.walk(links=tw.LINKS_IN_TREE) if f.rel.startswith("l")]
finally:
    tw._MAX_LINKED_DIRS = _cap
check("walk cap: without the cap all 300 are followed (so the case above fails if it is removed)",
      len(_all) == 300, str(len(_all)))

# A scanned file swapped for a link after the walk listed it, at the moment
# the scanner's read opens it (tree_walk._at_open): the link is not followed.
SW = WORK / "scan-swap"
SW.mkdir()
(SW / "real.js").write_text('fetch("https://ok.example/x"); fetch("/api/ok/y");\n')
(SW / "swap.js").write_text('fetch("https://ok.example/x"); fetch("/api/ok/y");\n')
_swapped: list = []
_orig_hook = tw._at_open


def _swap_file(tree, parts):
    if parts == ("swap.js",) and not _swapped:
        os.rename(SW / "swap.js", SW / "swap.js.real")
        os.symlink(OUT / "scan.js", SW / "swap.js")
        _swapped.append(parts)


tw._at_open = _swap_file
try:
    _viol = build_contract._scan_app_sources(SW, [])
    # put the file back for the second scanner; the hook swaps it again
    os.unlink(SW / "swap.js")
    os.rename(SW / "swap.js.real", SW / "swap.js")
    _swapped_first, _swapped[:] = len(_swapped), []
    _calls = api_contract._extract_api_calls(SW)
finally:
    tw._at_open = _orig_hook
check("scanners swap: the file was swapped for a link at the moment each scanner opened it",
      _swapped_first == 1 and len(_swapped) == 1)
check("build contract swap: a file swapped for a link after the walk is not read",
      LEAK not in " ".join(_viol) and not any(v.startswith("swap.js:") for v in _viol)
      and any(v.startswith("real.js:") for v in _viol), str(_viol))
check("api contract swap: a file swapped for a link after the walk is not read",
      LEAK not in str(_calls) and not any(c[0] == "swap.js" for c in _calls)
      and ("real.js", "/api/ok/y", None) in _calls, str(_calls))

# ── 12. The Workbench's landed-desktop scan (desktop_render.landed_desktop_documents)
# A landed build's tree is read for desktop documents (their first 64 KiB),
# each then loaded in the browser and its verdict shown to the Host. Every
# read goes through the contained walk: a linked file or folder is neither
# listed nor read, and one swapped in after the walk listed the name is not
# followed. Every head the scan examines is recorded, so "not read" is seen
# directly, not inferred from the result.
print("12. landed desktop scan")
from server.applications import desktop_render as DRN  # noqa: E402

_DESK = b'<!DOCTYPE html><html><head><meta name="cseries-desktop" content="wallpaper"></head><body>'
LDO = WORK / "landed-outside"
(LDO / "theme").mkdir(parents=True)
(LDO / "folder").mkdir()
for _p in (LDO / "secret.html", LDO / "theme" / "index.html", LDO / "swapped.html",
           LDO / "folder" / "index.html"):
    _p.write_bytes(_DESK + SECRET + b"</body></html>\n")
LD = WORK / "landed"
(LD / "aaa").mkdir(parents=True)
(LD / "aaa" / "readme.html").write_bytes(b"<html><body>notes</body></html>\n")
(LD / "index.html").write_bytes(_DESK + b"the build's own desktop</body></html>\n")
(LD / "later").mkdir()
(LD / "later" / "page.html").write_bytes(b"<html><body>not a desktop</body></html>\n")
(LD / "swap.html").write_bytes(b"<html><body>not a desktop</body></html>\n")
os.symlink(LDO / "secret.html", LD / "linked.html")           # a linked file
os.symlink(LDO / "theme", LD / "themes")                       # a linked folder
_heads: list = []
_real_detect = DRN.detect_mode


def _recording_detect(text):
    _heads.append(text)
    return _real_detect(text)


_ld_swaps: list = []


def _ld_swap(tree, parts):
    # Entering aaa/ (sorted first) is after the root was listed and before
    # swap.html is read: swap.html becomes a link to an outside desktop.
    # Entering later/: the folder itself becomes a link to an outside folder.
    if parts == ("aaa",) and "file" not in _ld_swaps:
        os.rename(LD / "swap.html", LD / "swap.html.real")
        os.symlink(LDO / "swapped.html", LD / "swap.html")
        _ld_swaps.append("file")
    elif parts == ("later",) and "dir" not in _ld_swaps:
        os.rename(LD / "later", LD / "later.real")
        os.symlink(LDO / "folder", LD / "later")
        _ld_swaps.append("dir")


DRN.detect_mode = _recording_detect
tw._at_open = _ld_swap
try:
    _ld_docs, _ld_over = DRN.landed_desktop_documents(LD)
finally:
    tw._at_open = _orig_hook
    DRN.detect_mode = _real_detect
_ld_text = "".join(_heads)
check("landed scan: the build's own desktop is found (the control)",
      _ld_docs == ["index.html"] and _ld_over == [], str(_ld_docs))
check("landed scan: a linked file is not read", "linked.html" not in _ld_docs
      and SECRET.decode() not in _ld_text, str(_ld_docs))
check("landed scan: a linked folder is not walked", not any(d.startswith("themes/") for d in _ld_docs),
      str(_ld_docs))
check("landed scan: a file and a folder were swapped for links mid-walk",
      sorted(_ld_swaps) == ["dir", "file"], str(_ld_swaps))
check("landed scan: a file swapped for a link after the walk listed it is not read",
      "swap.html" not in _ld_docs and SECRET.decode() not in _ld_text, str(_ld_docs))
check("landed scan: a folder swapped for a link as the walk enters it is not walked",
      not any(d.startswith("later/") for d in _ld_docs), str(_ld_docs))
check("landed scan: no head it examined came from outside the project",
      len(_heads) >= 1 and SECRET.decode() not in _ld_text, f"{len(_heads)} heads")
os.symlink(LDO, WORK / "landed-link")
check("landed scan: a project folder that is itself a link scans nothing",
      DRN.landed_desktop_documents(WORK / "landed-link") == ([], []))

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {fails}")
    sys.exit(1)
print("all link readers stay in their tree")
