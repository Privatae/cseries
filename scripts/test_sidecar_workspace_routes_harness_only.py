#!/usr/bin/env python3
"""
SECURITY: the shared sidecar's workspace routes are harness-only.

WHAT WAS OPEN. cli/cseries_sidecar.py registered POST /workspace/load, GET
/workspace/export, GET /session/export and GET /run/progress on EVERY process
that imported its shared `sidecar`. The Claude Code app's sidecar is one of
them, and the supervisor runs it INSIDE the Host container with the kernel's
own env (server/applications/supervisor.py _spawn). There CSERIES_HARNESS_CAP is
unset, so _cap_denial let every request through, and WORKSPACE_ROOT is the
kernel's own /var/lib/cseries/projects. The kernel proxy forwards any path to an
in-container process app with no token check, so a community app's JS could
POST /system-apps/claude-code/workspace/load?dir= : the empty dir resolved to
the projects ROOT, and rmtree ran before the tar was even opened. GET
/workspace/export with no dir returned every project's source.

WHAT THIS PINS
  1. an in-container sidecar (no CSERIES_SIDECAR_ROLE, no cap), served for real
     in a subprocess, answers 404 on all four routes, and a POST with dir=""
     leaves every project in place
  2. a harness sidecar (CSERIES_SIDECAR_ROLE=harness, capped, enforcing) still
     loads and exports over HTTP with its capability, and the dispatcher's
     own _clone_in / _collect_out / _export_fallback_dir / _drop_pod_dir work
     against it unchanged; a host worktree's .git, .GIT, nested .Git and a
     `hooks -> .git/hooks` link are all dropped before upload, and (2c) the
     uploader's rule and the receiver's agree on every member of a corpus
  3. the role is the only switch: a cap alone, a port number, or a near-miss
     value registers nothing
  4. load and export refuse an empty dir and every alias of the root
  5. a rejected tar deletes nothing and leaves no staging directory behind
  6. a tar carrying .git (config, hooks, any case, or a link into it) is refused
  7. a good load replaces the directory; an empty tar empties it (_drop_pod_dir)
  8. load refuses a directory reached through a symlink
  9. the kernel proxy 404s the four paths for in-pod apps before it dials
     anything, including dotted, doubled, ;param and #fragment spellings and
     %2e / unreserved escapes that yarl decodes on the way out; a
     harness-backed app's paths are untouched. (9b) the same through the REAL
     request path: raw bytes -> uvicorn -> the proxy -> aiohttp -> a sidecar
     that serves the routes, with a no-wall negative control showing the
     probe's x/%252e%252e/workspace/load really arrives as /workspace/load
 10. the supervisor never hands CSERIES_SIDECAR_ROLE to an in-container app
 11. every harness image that bakes cseries_sidecar sets the role, and the entity
     image does not
 12. collect-out treats the harness's export as untrusted: forged tars that
     write into the host worktree's .git through their own symlinks (h ->
     .git/hooks then h/pre-commit, and variants) are refused whole, with .git
     unchanged and the worktree not cleared; .git-touching members are
     dropped; a real export (dir and file links, pnpm's link-through-a-link)
     still lands. (12b) the fallback collect drops a file that resolves into
     .git through a link already on disk
 13. host-side git ignores a planted diff.external and textconv
     (_worktree_diff_host, project_git's base), and every hardened `diff`
     call carries --no-ext-diff --no-textconv
 12c. the link check is order-independent (reversed-order tars), and
     what a real workspace can hold (a venv's absolute bin/python, a link
     out of the tree, `b -> a/..`, a FIFO) is DROPPED, not the whole collect;
     links match case-sensitively (a `Docs` link beside a real `docs/`)
 14. a 469-byte gzip naming one 160,000-component path is
     refused in well under a second with the event loop free; so is the
     uncompressed form; the member walk is linear; the byte cap (declared and
     streamed), member cap, name cap and "uncompressed only" each refuse with
     the worktree untouched. (14c) the rest of the class: /run/stream framing
     is linear, error bodies are read to 4 KiB, harness JSON results are
     capped, the fallback and session tars refuse compression, no harness
     body is read whole, and every host diff runs off the loop
 15. the WebSocket lane refuses a harness path before it dials, with a
     negative control that an ordinary path IS dialled
 16. propose_commit stages with the hardened git (a planted diff.external
     and textconv in the project repo do not run, the diff is real) and
     refuses a `.GIT/...` pick; (16b) every git argv in review.py,
     project_git.py, attestation.py, gitea_propose.py and embed.py starts
     `"git", *HARDENED_GIT_CONFIG`, none runs through a shell, and every diff
     carries HARDENED_DIFF_FLAGS

Exit 0 = all pass; exit 1 + FAIL lines otherwise. Tolerant of the UNFIXED tree
(reports FAIL lines, never a traceback), so running it on main is the negative
control.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib
import io
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
CLI = ROOT / "cli"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CLI))

FAILS: list[str] = []

# Every subprocess this gate starts leads its own process group, and is listed
# here while it runs. A SIGTERM or SIGINT to the gate (the test supervisor's
# stop, a timeout) kills each group whole, so no sidecar and no file server
# outlives it, then the gate exits through its own `finally` blocks.
_LIVE_GROUPS: set[int] = set()


def _spawn(argv, **kw) -> subprocess.Popen:
    proc = subprocess.Popen(argv, start_new_session=True, **kw)
    _LIVE_GROUPS.add(proc.pid)
    return proc


def _kill_group(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)
    _LIVE_GROUPS.discard(proc.pid)


def _on_stop(signum, frame):
    for pgid in list(_LIVE_GROUPS):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
    raise SystemExit(128 + signum)


CAP = "cap-for-the-workspace-route-gate"
ROUTES = (("POST", "/workspace/load"), ("GET", "/workspace/export"),
          ("GET", "/session/export"), ("GET", "/run/progress"))
# Env that would change the sidecar's posture if inherited from the shell
# running this gate. Each server below states its own posture explicitly.
_POSTURE_ENV = ("CSERIES_SIDECAR_ROLE", "CSERIES_HARNESS_CAP", "CSERIES_HARNESS_CAP_ENFORCE",
                "PRIVATAE_HARNESS_CAP", "PRIVATAE_HARNESS_CAP_ENFORCE",
                "PRIVATAE_WORKSPACE", "CSERIES_SESSION_DIRS", "HTTP_PROXY", "HTTPS_PROXY",
                "http_proxy", "https_proxy")


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _tar(files: dict | None = None, links: dict | None = None, *, dot: bool = True) -> bytes:
    """A tar shaped like the dispatcher's _clone_in (arcname '.', './'-prefixed)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        if dot:
            ti = tarfile.TarInfo("."); ti.type = tarfile.DIRTYPE; ti.mode = 0o755
            tf.addfile(ti)
        for name, data in (files or {}).items():
            ti = tarfile.TarInfo(name); ti.size = len(data); ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(data))
        for name, target in (links or {}).items():
            ti = tarfile.TarInfo(name); ti.type = tarfile.SYMTYPE; ti.linkname = target
            tf.addfile(ti)
    return buf.getvalue()


def _tree(root: str) -> dict:
    """{relative path: bytes} of every regular file under root."""
    out = {}
    for base, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(base, n)
            if os.path.isfile(p) and not os.path.islink(p):
                out[os.path.relpath(p, root)] = pathlib.Path(p).read_bytes()
    return out


def _seed_projects(ws: str) -> dict:
    """Two projects, as the kernel's /var/lib/cseries/projects holds them."""
    for rel, data in (("proj-a/index.html", b"<h1>a</h1>"), ("proj-a/.git/HEAD", b"ref: x\n"),
                      ("proj-b/src/main.py", b"print('b')\n")):
        p = pathlib.Path(ws) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return _tree(ws)


# ── a real sidecar in a subprocess ───────────────────────────────────────

def _free_port() -> int:
    # Loopback on purpose: the server below is dialled only by this gate, in
    # the same network namespace, and must not be reachable from anywhere else.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _http(port: int, method: str, path: str, body: bytes | None = None,
          cap: str | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=body)
    if body is not None:
        req.add_header("Content-Type", "application/octet-stream")
    if cap:
        req.add_header("X-CSeries-Harness-Cap", cap)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class _Server:
    """`cseries_sidecar.sidecar.serve()` exactly as an app's main calls it, in its
    own interpreter, so the posture is read at import from a real env."""

    def __init__(self, ws: str, sessions: str, env: dict):
        self.port = _free_port()
        e = {k: v for k, v in os.environ.items() if k not in _POSTURE_ENV}
        e.update({"PORT": str(self.port), "PRIVATAE_SIDECAR_BIND": "127.0.0.1",
                  "PRIVATAE_APP_ID": "claude-code", "PRIVATAE_WORKSPACE": ws,
                  "CSERIES_SESSION_DIRS": sessions, "PYTHONDONTWRITEBYTECODE": "1"})
        e.update(env)
        code = ("import sys; sys.path.insert(0, %r); import cseries_sidecar; "
                "cseries_sidecar.sidecar.serve()" % str(CLI))
        self.proc = _spawn([sys.executable, "-c", code], env=e,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while self.proc.poll() is None:      # until it answers, or exits: never on a clock
            try:
                _http(self.port, "GET", "/health")
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"sidecar did not come up (rc={self.proc.poll()})")

    def close(self):
        _kill_group(self.proc)


def http_checks() -> None:
    print("== 1. an in-container sidecar has no workspace routes (real server) ==")
    with tempfile.TemporaryDirectory() as td:
        ws, sess = os.path.join(td, "projects"), os.path.join(td, "claude-home")
        os.makedirs(ws); os.makedirs(sess)
        pathlib.Path(sess, "history.jsonl").write_text("{}\n")
        before = _seed_projects(ws)
        srv = _Server(ws, sess, {})           # the Host container: no role, no cap
        try:
            # Reads first: the destructive POST last, so on an unfixed tree
            # the reads are not answered 404 merely because it already
            # emptied the workspace.
            code, body = _http(srv.port, "GET", "/workspace/export")
            check("GET /workspace/export (no dir) -> 404, no project source",
                  code == 404 and b"proj-b" not in body, f"HTTP {code}, {len(body)} bytes")
            code, _ = _http(srv.port, "GET", "/workspace/export?dir=proj-a")
            check("GET /workspace/export?dir=proj-a -> 404", code == 404, f"HTTP {code}")
            code, _ = _http(srv.port, "GET", "/session/export")
            check("GET /session/export -> 404", code == 404, f"HTTP {code}")
            code, _ = _http(srv.port, "GET", "/run/progress?dir=proj-a")
            check("GET /run/progress -> 404", code == 404, f"HTTP {code}")
            code, _ = _http(srv.port, "POST", "/workspace/load?dir=", _tar({"x": b"y"}))
            check("POST /workspace/load?dir= -> 404", code == 404, f"HTTP {code}")
            check("...and every project is still there, byte for byte",
                  _tree(ws) == before, f"now {sorted(_tree(ws))}")
        finally:
            srv.close()

    print("== 2. a harness sidecar still loads and exports ==")
    with tempfile.TemporaryDirectory() as td:
        ws, sess = os.path.join(td, "projects"), os.path.join(td, "claude-home")
        os.makedirs(ws); os.makedirs(sess)
        pathlib.Path(sess, "history.jsonl").write_text("{}\n")
        srv = _Server(ws, sess, {"CSERIES_SIDECAR_ROLE": "harness", "CSERIES_HARNESS_CAP": CAP,
                                 "CSERIES_HARNESS_CAP_ENFORCE": "1"})
        try:
            tar = _tar({"./index.html": b"<!doctype html>", "./src/app.js": b"ok"})
            code, body = _http(srv.port, "POST", "/workspace/load?dir=s1-codex", tar, cap=CAP)
            check("capped POST /workspace/load?dir=s1-codex -> 200", code == 200, f"HTTP {code} {body[:120]!r}")
            check("...the files landed", _tree(os.path.join(ws, "s1-codex")) ==
                  {"index.html": b"<!doctype html>", "src/app.js": b"ok"},
                  str(_tree(ws))[:200])
            code, body = _http(srv.port, "GET", "/workspace/export?dir=s1-codex", cap=CAP)
            names = []
            if code == 200:
                with tarfile.open(fileobj=io.BytesIO(body)) as tf:
                    names = sorted(n.lstrip("./") for n in tf.getnames())
            check("capped GET /workspace/export?dir=s1-codex -> 200 with the files",
                  code == 200 and "index.html" in names and "src/app.js" in names, f"HTTP {code} {names}")
            code, _ = _http(srv.port, "GET", "/session/export", cap=CAP)
            check("capped GET /session/export -> 200", code == 200, f"HTTP {code}")
            code, _ = _http(srv.port, "GET", "/run/progress?dir=s1-codex", cap=CAP)
            check("capped GET /run/progress -> 200", code == 200, f"HTTP {code}")
            code, _ = _http(srv.port, "POST", "/workspace/load?dir=s1-codex", tar)
            check("uncapped POST to a capped, enforcing harness -> 403 (F-013 unchanged)",
                  code == 403, f"HTTP {code}")
            code, body = _http(srv.port, "GET", "/workspace/export", cap=CAP)
            check("capped GET /workspace/export with NO dir -> 400 (not the whole root)",
                  code == 400, f"HTTP {code}, {len(body)} bytes")
        finally:
            srv.close()


def dispatcher_checks() -> None:
    print("== 2b. the dispatcher's own call sites still work against a harness ==")
    import aiohttp
    from server.routes import review as rv
    with tempfile.TemporaryDirectory() as td:
        ws, sess = os.path.join(td, "projects"), os.path.join(td, "claude-home")
        os.makedirs(ws); os.makedirs(sess)
        srv = _Server(ws, sess, {"CSERIES_SIDECAR_ROLE": "harness", "CSERIES_HARNESS_CAP": CAP,
                                 "CSERIES_HARNESS_CAP_ENFORCE": "1"})
        base = f"http://127.0.0.1:{srv.port}"
        rv._remember_control_cap("codex", base, CAP)
        # A host worktree as _prepare_worktree leaves it: files AND a .git,
        # which _clone_in must drop before the load's .git refusal sees it.
        wt = pathlib.Path(td) / "sess-1" / "work" / "codex"
        (wt / "src").mkdir(parents=True)
        (wt / "index.html").write_text("<h1>seed</h1>")
        (wt / "src" / "app.js").write_text("ok")
        (wt / ".git" / "hooks").mkdir(parents=True)
        (wt / ".git" / "config").write_text("[core]\n")
        # Entries the RECEIVER refuses but an older uploader
        # sent as-is (it dropped only an exact ".git" name component), which
        # would have failed every clone-in of this worktree with HTTP 400.
        (wt / ".GIT").mkdir()
        (wt / ".GIT" / "config").write_text("[core]\n")
        (wt / "vendor" / ".Git").mkdir(parents=True)
        (wt / "vendor" / ".Git" / "HEAD").write_text("ref: x\n")
        os.symlink(".git/hooks", wt / "hooks")
        dir_name = rv._pod_dir_name(wt.parents[1], wt)

        async def go():
            async with aiohttp.ClientSession() as http:
                try:
                    await rv._clone_in(http, base, wt, dir_name)
                    err = None
                except Exception as exc:  # noqa: BLE001 - the old uploader 400s here
                    err = repr(exc)[:160]
                check("_clone_in of a worktree with .git, .GIT, vendor/.Git and hooks -> "
                      ".git/hooks succeeds", err is None, err or "")
                landed = _tree(os.path.join(ws, dir_name))
                check("...the files land, and none of the git entries or the link",
                      landed == {"index.html": b"<h1>seed</h1>", "src/app.js": b"ok"}
                      and not os.path.lexists(os.path.join(ws, dir_name, "hooks")),
                      sorted(landed))
                # the harness writes its output
                pathlib.Path(ws, dir_name, "out.txt").write_text("built")
                out_wt = pathlib.Path(td) / "collected"
                (out_wt / ".git").mkdir(parents=True)
                await rv._collect_out(http, base, dir_name, out_wt)
                got = {k: v for k, v in _tree(str(out_wt)).items() if not k.startswith(".git")}
                check("_collect_out -> the worktree-after comes back",
                      got.get("out.txt") == b"built" and got.get("index.html") == b"<h1>seed</h1>",
                      sorted(got))
                none = await rv._export_fallback_dir(http, base, "no-such-scratch")
                check("_export_fallback_dir on a missing dir -> None (404 tolerated)", none is None)
                dropped = await rv._drop_pod_dir(http, base, dir_name)
                check("_drop_pod_dir -> True, and the pod dir is empty",
                      dropped is True and os.listdir(os.path.join(ws, dir_name)) == [],
                      os.listdir(os.path.join(ws, dir_name)))

        try:
            asyncio.run(go())
        finally:
            rv._forget_control_cap("codex")
            srv.close()


def git_rule_parity_checks() -> None:
    """The uploader (review._tar_member_touches_git, via _clone_in's _drop_git)
    and the receiver (cseries_sidecar._touches_git / _git_metadata_member) must
    agree on every member: whatever one side would send, the other accepts."""
    print("== 2c. uploader and receiver agree on .git ==")
    from server.routes import review as rv
    sc = _load_module({"CSERIES_SIDECAR_ROLE": "harness"})
    up = getattr(rv, "_tar_member_touches_git", None)
    check("review has _tar_member_touches_git (the uploader's rule)", up is not None)

    def ti(name, kind="file", link=""):
        t = tarfile.TarInfo(name)
        t.type = {"file": tarfile.REGTYPE, "dir": tarfile.DIRTYPE,
                  "sym": tarfile.SYMTYPE, "hard": tarfile.LNKTYPE}[kind]
        t.linkname = link
        return t

    corpus = [
        (ti("./.git", "dir"), True), (ti("./.git/config"), True), (ti("./.GIT/config"), True),
        (ti("./vendor/.Git/HEAD"), True), (ti("./sub\\.git\\config"), True),
        (ti("./hooks", "sym", ".git/hooks"), True), (ti("./l", "sym", "../.GIT"), True),
        (ti("./h", "hard", "./x/.git/objects/ab"), True), (ti("./.git", "file"), True),
        (ti("./x.git"), False), (ti("./.gitignore"), False), (ti("./.github/workflows/ci.yml"), False),
        (ti("./a/b.git/c"), False), (ti("./docs", "sym", "documentation"), False),
        (ti("./index.html"), False), (ti("."), False),
        # a regular file's linkname is not a link and is ignored by both
        (ti("./plain", "file", ".git/config"), False),
    ]
    disagree, wrong = [], []
    for t, want in corpus:
        recv = sc._git_metadata_member([t]) is not None
        send = bool(up(t)) if up is not None else None
        if send != recv:
            disagree.append((t.name, t.linkname, send, recv))
        if recv != want:
            wrong.append((t.name, t.linkname, recv))
    check("the two rules agree on every member of the corpus", not disagree, disagree)
    check("...and both give the expected answer", not wrong, wrong)


# ── the handlers, in-process ─────────────────────────────────────────────

class _Req:
    def __init__(self, query: str, body: bytes = b""):
        self.method, self.path, self.headers = "POST", "/", {}
        self.query, self.body = query, body


def _load_module(env: dict):
    for k in _POSTURE_ENV:
        os.environ.pop(k, None)
    os.environ.update(env)
    os.environ.setdefault("PORT", "8845")
    import cseries_sidecar
    return importlib.reload(cseries_sidecar)


def _status(out) -> int:
    return out[1] if isinstance(out, tuple) else 200


def role_checks() -> None:
    print("== 3. the role is the only switch ==")
    for label, env, want in (
        ("no role, no cap (the Host container)", {}, False),
        ("a cap but no role", {"CSERIES_HARNESS_CAP": CAP}, False),
        ("a port number (a manifest port_env named the role)", {"CSERIES_SIDECAR_ROLE": "8123"}, False),
        ("a near miss ('harnesses')", {"CSERIES_SIDECAR_ROLE": "harnesses"}, False),
        ("'1'", {"CSERIES_SIDECAR_ROLE": "1"}, False),
        ("role=harness", {"CSERIES_SIDECAR_ROLE": "harness"}, True),
        ("role=' Harness ' (case and space)", {"CSERIES_SIDECAR_ROLE": " Harness "}, True),
    ):
        sc = _load_module(env)
        have = [r for r in ROUTES if r in sc.sidecar._routes]
        check(f"{label}: routes {'registered' if want else 'absent'}",
              (len(have) == len(ROUTES)) if want else not have, f"registered: {have}")
    sc = _load_module({"CSERIES_SIDECAR_ROLE": "harness"})
    declared = set(getattr(sc, "HARNESS_ONLY_ROUTES", ()) or ())
    check("HARNESS_ONLY_ROUTES names exactly the four routes", declared == set(ROUTES), sorted(declared))
    for k in _POSTURE_ENV:
        os.environ.pop(k, None)


def handler_checks() -> None:
    sc = _load_module({"CSERIES_SIDECAR_ROLE": "harness"})
    load, export = sc._workspace_load, sc._workspace_export

    print("== 4. empty dir and every alias of the root are refused ==")
    with tempfile.TemporaryDirectory() as td:
        sc.WORKSPACE_ROOT = td
        before = _seed_projects(td)
        for d in ("", ".", "./", "proj-a/..", "proj-a/../."):
            out = load(_Req(f"dir={d}", _tar({"x": b"y"})))
            check(f"load dir={d!r} -> 400", _status(out) == 400, str(out)[:100])
            out = export(_Req(f"dir={d}"))
            check(f"export dir={d!r} -> 400", _status(out) == 400, str(out)[:60])
        check("...and every project is intact", _tree(td) == before, sorted(_tree(td)))
        try:
            sc._safe_ws_path("")
            root_ok = True
        except ValueError:
            root_ok = False
        check("_safe_ws_path('') raises", not root_ok)

    print("== 5. a rejected tar deletes nothing ==")
    with tempfile.TemporaryDirectory() as td:
        sc.WORKSPACE_ROOT = td
        before = _seed_projects(td)
        # a good first member, then a traversal: filter='data' raises MID-extract
        midway = io.BytesIO()
        with tarfile.open(fileobj=midway, mode="w") as tf:
            ti = tarfile.TarInfo("./ok.txt"); ti.size = 2; tf.addfile(ti, io.BytesIO(b"ok"))
            ti = tarfile.TarInfo("../escape.txt"); ti.size = 2; tf.addfile(ti, io.BytesIO(b"no"))
        for label, body in (("garbage bytes", b"this is not a tar at all" * 40),
                            # cut inside the member's DATA: the header parses,
                            # extraction then fails part-way through
                            ("a tar cut off mid-file", _tar({"a": b"1" * 4096})[:1500]),
                            ("a traversal member after a good one", midway.getvalue()),
                            ("an escaping symlink", _tar(links={"./l": "/etc/passwd"}))):
            out = load(_Req("dir=proj-a", body))
            check(f"{label}: 400", _status(out) == 400, str(out)[:100])
            check(f"{label}: proj-a untouched", _tree(td) == before,
                  sorted(set(before) ^ set(_tree(td))))
        leftovers = [n for n in os.listdir(td) if n not in ("proj-a", "proj-b")]
        check("no staging directory is left behind", not leftovers, leftovers)
        check("nothing escaped the workspace", not os.path.exists(os.path.join(os.path.dirname(td), "escape.txt")))

    print("== 6. a tar carrying .git is refused ==")
    with tempfile.TemporaryDirectory() as td:
        sc.WORKSPACE_ROOT = td
        before = _seed_projects(td)
        for label, body in (
            (".git/config", _tar({"./index.html": b"x", "./.git/config": b"[core]\n\tfsmonitor = touch /tmp/pwn\n"})),
            (".git/hooks/pre-commit", _tar({"./.git/hooks/pre-commit": b"#!/bin/sh\nid\n"})),
            ("a nested repo's .git", _tar({"./vendor/lib/.git/config": b"[core]\n"})),
            ("upper-case .GIT", _tar({"./.GIT/config": b"[core]\n"})),
            ("a .git FILE (gitdir pointer)", _tar({"./.git": b"gitdir: /var/lib/cseries/projects/proj-b/.git\n"})),
            ("a symlink into .git", _tar({"./h/pre-commit": b"#!/bin/sh\n"}, links={"./h": ".git/hooks"})),
        ):
            out = load(_Req("dir=proj-a", body))
            check(f"{label}: 400", _status(out) == 400 and ".git" in str(out), str(out)[:100])
        check("...and proj-a (with its own .git) is untouched", _tree(td) == before,
              sorted(set(before) ^ set(_tree(td))))
        check("a plain .gitignore is fine", _status(load(_Req("dir=gi", _tar({"./.gitignore": b"node_modules\n"})))) == 200)

    print("== 7. a good load replaces; an empty tar empties ==")
    with tempfile.TemporaryDirectory() as td:
        sc.WORKSPACE_ROOT = td
        _seed_projects(td)
        out = load(_Req("dir=proj-a", _tar({"./new.txt": b"new", "./d/e.txt": b"e"})))
        check("load -> 200, files=2", _status(out) == 200 and out.get("files") == 2, str(out)[:120])
        check("the directory holds exactly the tar (old files gone)",
              _tree(os.path.join(td, "proj-a")) == {"new.txt": b"new", "d/e.txt": b"e"},
              sorted(_tree(os.path.join(td, "proj-a"))))
        check("the sibling project is untouched", _tree(os.path.join(td, "proj-b")) == {"src/main.py": b"print('b')\n"})
        out = load(_Req("dir=proj-a", _tar(dot=False)))
        check("empty tar (_drop_pod_dir) -> 200, files=0", _status(out) == 200 and out.get("files") == 0, str(out)[:120])
        check("...and leaves the directory, empty", os.path.isdir(os.path.join(td, "proj-a"))
              and os.listdir(os.path.join(td, "proj-a")) == [])
        out = load(_Req("dir=fresh-codex", _tar({"./a": b"1"})))
        check("a load into a new directory creates it", _status(out) == 200
              and _tree(os.path.join(td, "fresh-codex")) == {"a": b"1"}, str(out)[:120])
        # filter='data' strips a leading '/', so an absolute member is not a
        # rejection: it lands INSIDE the directory, never at its absolute path.
        outside = os.path.join(os.path.dirname(td), f"abs-escape-{os.getpid()}.txt")
        out = load(_Req("dir=abs-codex", _tar({outside: b"x"})))
        check("an absolute member lands inside the directory, not at its path",
              _status(out) == 200 and not os.path.exists(outside)
              and _tree(os.path.join(td, "abs-codex")) == {outside.lstrip("/"): b"x"},
              str(out)[:120])
        leftovers = sorted(n for n in os.listdir(td) if n.startswith("."))
        check("no staging or old-copy directory remains", not leftovers, leftovers)

    print("== 8. load refuses a directory reached through a symlink ==")
    with tempfile.TemporaryDirectory() as td:
        sc.WORKSPACE_ROOT = td
        before = _seed_projects(td)
        os.symlink(os.path.join(td, "proj-b"), os.path.join(td, "s2-codex"))
        out = load(_Req("dir=s2-codex", _tar({"./x": b"y"})))
        check("load via symlink -> 400", _status(out) == 400 and "symlink" in str(out), str(out)[:100])
        check("...and the link's target is untouched", _tree(os.path.join(td, "proj-b")) ==
              {"src/main.py": b"print('b')\n"})
        del before
    for k in _POSTURE_ENV:
        os.environ.pop(k, None)


# ── the kernel proxy ─────────────────────────────────────────────────────

# The path as Starlette hands it to serve_app: ALREADY decoded once by uvicorn.
REFUSED = ("workspace/load", "/workspace/load", "workspace//load", "x/../workspace/load",
           "./workspace/export", "workspace/load;x=1", "workspace/export#frag",
           "workspace/load?dir=", "WORKSPACE/LOAD", "workspace/load/", "session/export",
           "run/progress",
           # Double-encoded dot segments. yarl decodes %2e and
           # drops the segment, so each of these left aiohttp as /workspace/load.
           "x/%2e%2e/workspace/load", "%2e/workspace/load", "workspace/%2E/load",
           "x/.%2e/workspace/load", "x/%2e./workspace/load", "x/%2E%2E/run/progress",
           # the same class through other unreserved escapes and a ';' escape
           "workspace/lo%61d", "%77orkspace/export", "workspace/load%3Bx",
           "session%2Fexport",
           # nothing current decodes three times; refused anyway (loose reading)
           "x/%252e%252e/workspace/load")
FORWARDED = ("sessions", "sessions/continue", "status", "health", "run", "run/stream",
             "workspace", "workspacex/load", "index.html", "api/x/workspace/load",
             "x/%2e%2e/sessions", "%73essions")


class _Reached(Exception):
    """Raised by _StubReq.body(): the forwarder got past its refusal."""


class _StubReq:
    """Just enough of a starlette Request for the forwarders: the query is
    read at the refusal, and body() is the first thing after it."""
    class url:  # noqa: N801
        query = ""
    method = "POST"
    headers: dict = {}

    async def body(self):
        raise _Reached()


def proxy_checks() -> None:
    print("== 9. the kernel proxy refuses the four paths for in-pod apps ==")
    from fastapi import HTTPException
    from server.applications import proxy
    refuse = getattr(proxy, "_refuse_harness_only", None)
    check("proxy has a harness-only refusal", refuse is not None)

    def _code(p, q=""):
        try:
            refuse(p, q)
            return None
        except HTTPException as exc:
            return exc.status_code
        except Exception as exc:  # noqa: BLE001 - an older signature reads as FAIL
            return repr(exc)[:60]

    if refuse is not None:
        for p in REFUSED:
            code = _code(p)
            check(f"refused: {p!r} -> 404", code == 404, f"got {code}")
        for p in FORWARDED:
            code = _code(p)
            check(f"forwarded: {p!r}", code is None, f"got {code}")
        code = _code("workspace/load?", "dir=")
        check("a decoded '?' in the path, before the real query -> 404", code == 404, f"got {code}")
        code = _code("workspace/load%3F", "dir=")
        check("a still-encoded '?' (no stack routes it) is refused by the loose reading",
              code == 404, f"got {code}")
    sc = _load_module({"CSERIES_SIDECAR_ROLE": "harness"})
    lib = {p.lstrip("/") for _, p in getattr(sc, "HARNESS_ONLY_ROUTES", ())}
    check("the proxy's list IS the sidecar's HARNESS_ONLY_ROUTES",
          lib and set(getattr(proxy, "_HARNESS_ONLY_SIDECAR_PATHS", ())) == lib,
          f"proxy={sorted(getattr(proxy, '_HARNESS_ONLY_SIDECAR_PATHS', ()))} sidecar={sorted(lib)}")

    consulted = []

    class _Sup:
        def get(self, app_id):
            consulted.append(app_id)
            return None

    orig = proxy.get_supervisor
    proxy.get_supervisor = lambda: (consulted.append("get_supervisor") or _Sup())

    def _run(coro):
        try:
            asyncio.run(coro)
            return None
        except HTTPException as exc:
            return exc.status_code
        except _Reached:
            return "reached"
        except Exception as exc:  # noqa: BLE001 - a crash reads as "not refused"
            return repr(exc)[:80]

    try:
        code = _run(proxy._serve_process("claude-code", "workspace/load", _StubReq()))
        check("in-container process app: /workspace/load -> 404 before the supervisor is asked",
              code == 404 and not consulted, f"got {code}, consulted={consulted}")
        consulted.clear()
        code = _run(proxy._serve_process("claude-code", "sessions", _StubReq()))
        check("...its own routes still go to the supervisor", bool(consulted), f"got {code}")
        code = _run(proxy._serve_external("some-app", 8899, "workspace/export", _StubReq(), True, None))
        check("in-pod (loopback) external app: /workspace/export -> 404 before any dial",
              code == 404, f"got {code}")
        code = _run(proxy._serve_external("some-app", 8899, "%2e%2e/%2e%2e/workspace/load",
                                          _StubReq(), False, None))
        check("in-pod external, strip_prefix off: dot segments climbing out of "
              "system-apps/<id>/ -> 404", code == 404, f"got {code}")
        code = _run(proxy._serve_external("browser", 8830, "workspace/export", _StubReq(), False, "h.svc"))
        check("a harness-backed app's paths are not touched by the in-pod rule",
              code == "reached", f"got {code}")
    finally:
        proxy.get_supervisor = orig


def proxy_wire_checks() -> None:
    """The real request path, not the helper: raw bytes on the wire -> uvicorn
    (decodes once) -> a Starlette route shaped like serve_app's -> the proxy's
    real forwarders -> aiohttp (yarl normalises) -> a sidecar that SERVES the
    routes (an in-pod sidecar on an older library copy, which is what this wall
    exists for). A bypass is visible as the sidecar answering and the
    workspace changing."""
    print("== 9b. through uvicorn + aiohttp to a sidecar that serves the routes ==")
    import http.client
    import threading
    import uvicorn
    from gate_loopback import IDLE_KEEP_ALIVE_S
    from fastapi import HTTPException
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from server.applications import proxy

    with tempfile.TemporaryDirectory() as td:
        ws, sess = os.path.join(td, "projects"), os.path.join(td, "claude-home")
        os.makedirs(ws); os.makedirs(sess)
        before = _seed_projects(ws)
        side = _Server(ws, sess, {"CSERIES_SIDECAR_ROLE": "harness"})   # uncapped
        side_port = side.port

        class _Running:
            base_url = f"http://127.0.0.1:{side_port}"

            def is_alive(self):
                return True

        class _Sup:
            def get(self, app_id):
                return _Running()

        methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]

        # Starlette routing, which is what serve_app's FastAPI route resolves
        # through: the same {path:path} convertor over uvicorn's decoded path.
        # A refusal is tagged "by": "proxy" so it cannot be mistaken for the
        # sidecar's own 404.
        async def _proc(request):
            pp = request.path_params
            try:
                return await proxy._serve_process(pp["app_id"], pp["path"], request)
            except HTTPException as exc:
                return JSONResponse({"by": "proxy", "detail": exc.detail}, exc.status_code)

        async def _ext(request):
            pp = request.path_params
            try:
                return await proxy._serve_external(pp["app_id"], side_port, pp["path"],
                                                   request, False, None)
            except HTTPException as exc:
                return JSONResponse({"by": "proxy", "detail": exc.detail}, exc.status_code)

        app = Starlette(routes=[
            Route("/system-apps/{app_id}/{path:path}", _proc, methods=methods),
            Route("/ext-noprefix/{app_id}/{path:path}", _ext, methods=methods),
        ])

        kport = _free_port()
        # Loopback on purpose, like _free_port: only this gate dials it.
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=kport,
                                               log_level="error", lifespan="off", timeout_keep_alive=IDLE_KEEP_ALIVE_S))
        thread = threading.Thread(target=server.run, daemon=True)
        orig_sup, orig_refuse = proxy.get_supervisor, proxy._refuse_harness_only
        proxy.get_supervisor = lambda: _Sup()
        thread.start()
        while not server.started and thread.is_alive():     # until it serves, or its thread ends
            time.sleep(0.05)

        def send(method, raw, body=None):
            c = http.client.HTTPConnection("127.0.0.1", kport, timeout=15)
            c.request(method, raw, body=body,
                      headers={"Content-Type": "application/octet-stream"} if body else {})
            r = c.getresponse()
            out = (r.status, r.read())
            c.close()
            return out

        pwn = _tar({"./PWNED": b"x"})
        try:
            check("the in-process kernel came up", server.started)
            # NEGATIVE CONTROL: the wall removed, the probe's request goes
            # all the way through and replaces proj-a. Proves this section can
            # see a bypass, and that the double decode is real on this stack.
            proxy._refuse_harness_only = lambda *a, **k: None
            code, body = send("POST", "/system-apps/claude-code/x/%252e%252e/workspace/load?dir=proj-a", pwn)
            check("negative control: with no wall, x/%252e%252e/workspace/load reaches the "
                  "sidecar and replaces proj-a", code == 200 and
                  _tree(os.path.join(ws, "proj-a")) == {"PWNED": b"x"}, f"HTTP {code} {body[:80]!r}")
            proxy._refuse_harness_only = orig_refuse
            for p in pathlib.Path(ws).iterdir():
                if p.is_dir():
                    shutil.rmtree(p)
            before = _seed_projects(ws)

            for method, raw in (
                ("POST", "/system-apps/claude-code/x/%252e%252e/workspace/load?dir=proj-a"),
                ("POST", "/system-apps/claude-code/%252e/workspace/load?dir=proj-a"),
                ("POST", "/system-apps/claude-code/workspace/%252E/load?dir=proj-a"),
                ("POST", "/system-apps/claude-code/x/.%252e/workspace/load?dir=proj-a"),
                ("POST", "/system-apps/claude-code/x/%252e%2e/workspace/load?dir=proj-a"),
                ("POST", "/system-apps/claude-code/workspace/lo%2561d?dir=proj-a"),
                ("POST", "/system-apps/claude-code/workspace/load%253Bx?dir=proj-a"),
                ("POST", "/system-apps/claude-code/workspace/load%3Bx?dir=proj-a"),
                ("POST", "/system-apps/claude-code/workspace/load?dir=proj-a"),
                ("GET", "/system-apps/claude-code/x/%252e%252e/workspace/export?dir=proj-a"),
                ("GET", "/system-apps/claude-code/%2577orkspace/export?dir=proj-b"),
                ("GET", "/system-apps/claude-code/x/%252e%252e/session/export"),
                ("GET", "/system-apps/claude-code/x/%252e%252e/run/progress?dir=proj-a"),
                ("POST", "/ext-noprefix/foo/%252e%252e/%252e%252e/%252e%252e/workspace/load?dir=proj-a"),
            ):
                code, body = send(method, raw, pwn if method == "POST" else None)
                refused_here = code == 404 and b'"by":"proxy"' in body   # not the sidecar's 404
                check(f"{method} {raw} -> the proxy's 404", refused_here, f"HTTP {code} {body[:80]!r}")
            check("...and after all of them, every project is untouched", _tree(ws) == before,
                  sorted(set(before) ^ set(_tree(ws))))
            code, body = send("GET", "/system-apps/claude-code/x/%252e%252e/nothing-here")
            check("an ordinary path still reaches the sidecar (its own 404 body)",
                  code == 404 and b"no route for GET /nothing-here" in body, f"HTTP {code} {body[:80]!r}")
        finally:
            proxy.get_supervisor, proxy._refuse_harness_only = orig_sup, orig_refuse
            server.should_exit = True
            thread.join(timeout=10)
            side.close()


# ── the supervisor and the images ────────────────────────────────────────

def env_and_image_checks() -> None:
    print("== 10. the supervisor never hands the role to an in-container app ==")
    from server.applications import supervisor as sup
    child_env = getattr(sup, "_child_env", None)
    check("supervisor builds a child env through _child_env", child_env is not None)
    if child_env is not None:
        os.environ["CSERIES_SIDECAR_ROLE"] = "harness"
        try:
            env = child_env({"PORT": "9001", "PRIVATAE_APP_ID": "claude-code"})
        finally:
            os.environ.pop("CSERIES_SIDECAR_ROLE", None)
        check("a role on the kernel's env does not reach the child", "CSERIES_SIDECAR_ROLE" not in env)
        check("...the rest of the kernel env and the overrides do",
              env.get("PORT") == "9001" and env.get("PATH") == os.environ.get("PATH"))
    src = (ROOT / "server/applications/supervisor.py").read_text()
    spawn = src[src.index("async def _spawn("):]
    spawn = spawn[:spawn.index("\n    async def ", 10) if "\n    async def " in spawn[10:] else len(spawn)]
    check("_spawn uses _child_env, not a bare **os.environ",
          "_child_env(" in spawn and "**os.environ" not in spawn)

    print("== 11. every harness image sets the role; the entity image does not ==")
    baked = []
    for df in sorted((ROOT / "sidecars").glob("*/Dockerfile")):
        text = df.read_text()
        if "cli/cseries_sidecar.py" not in text:
            continue
        baked.append(df.parent.name)
        check(f"{df.parent.name}: ENV CSERIES_SIDECAR_ROLE=harness",
              re.search(r"^\s*CSERIES_SIDECAR_ROLE=harness\s*\\?\s*$", text, re.M) is not None)
    check("found the harness images (not an empty glob)", len(baked) >= 7, baked)
    check("Dockerfile.linux (the Host image) never sets the role",
          "CSERIES_SIDECAR_ROLE" not in (ROOT / "Dockerfile.linux").read_text())


# ── collect-out: an untrusted export cannot reach the host worktree's .git ─

def _raw_tar(entries: list) -> bytes:
    """A tar built member by member, in order, as a forged export would be:
    ("file", name, data) | ("dir", name) | ("sym", name, target) |
    ("hard", name, target)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for e in entries:
            ti = tarfile.TarInfo(e[1])
            if e[0] == "file":
                ti.size = len(e[2]); ti.mode = 0o644
                tf.addfile(ti, io.BytesIO(e[2]))
                continue
            ti.type = {"dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE,
                       "hard": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE}[e[0]]
            ti.mode = 0o755
            if len(e) > 2:
                ti.linkname = e[2]
            tf.addfile(ti)
    return buf.getvalue()


async def _serve_export(body: bytes):
    """A pod's /workspace/export that answers `body` for any dir. Loopback on
    purpose: only this gate dials it."""
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/workspace/export", lambda _r: web.Response(body=body))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def _host_worktree(td: str, name: str):
    """A SEEDED host worktree made by the real _prepare_worktree: its own .git
    with the seed base commit, as _collect_out finds it."""
    from server.routes import review as rv
    seed = pathlib.Path(td) / f"seed-{name}"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "a.txt").write_text("seed\n")
    return rv._prepare_worktree(pathlib.Path(td) / f"work-{name}", "codex",
                                seed=True, seed_from=seed)


def _git_state(wt) -> dict:
    return {str(p.relative_to(wt)): (p.read_bytes() if p.is_file() else None)
            for p in (wt / ".git").rglob("*") if "objects" not in p.parts and not p.is_symlink()}


def collect_out_checks() -> None:
    print("== 12. collect-out: an untrusted export cannot reach the worktree's .git ==")
    import aiohttp
    from server.routes import review as rv
    hook = ("file", "h/pre-commit", b"#!/bin/sh\nexit 0\n")
    forged = [
        ("h -> .git/hooks, then h/pre-commit", [("sym", "./h", ".git/hooks"), hook]),
        ("c -> .git, then c/config", [("sym", "./c", ".git"),
                                      ("file", "c/config", b"[diff]\n\texternal = /bin/true\n")]),
        ("x -> ., then x/.git/hooks/pre-commit", [("sym", "./x", "."),
                                                  ("file", "x/.git/hooks/pre-commit", b"x")]),
        ("h -> .GIT/hooks (case), then h/pre-commit", [("sym", "./h", ".GIT/hooks"), hook]),
        ("s -> src/a.txt, then a FILE named s (write through)", [("sym", "./s", "src/a.txt"),
                                                                ("file", "s", b"through\n")]),
        ("l -> sub, then a hard link through l", [("dir", "./sub"), ("sym", "./l", "sub"),
                                                  ("hard", "./k", "l/x")]),
        ("x/../h after h -> .git/hooks (dot-dot walk)", [("sym", "./h", ".git/hooks"),
                                                        ("file", "x/../h/pre-commit", b"x")]),
        # 12c. order-independent. The member comes BEFORE the link it goes
        # through; a one-pass check that records links as it meets them
        # passes these.
        ("REVERSED: h/pre-commit, then h -> .git/hooks", [hook, ("sym", "./h", ".git/hooks")]),
        ("REVERSED: hard link k -> l/x, then l -> sub", [("dir", "./sub"), ("file", "sub/x", b"x"),
                                                          ("hard", "./k", "l/x"), ("sym", "./l", "sub")]),
        ("the same name twice (dir, then a link over it)", [("dir", "./d"), ("sym", "./d", "src")]),
    ]

    async def collect(wt, body):
        runner, base = await _serve_export(body)
        try:
            async with aiohttp.ClientSession() as http:
                await rv._collect_out(http, base, "d", wt)
            return None
        except Exception as exc:  # noqa: BLE001 - a refusal raises; reported below
            return repr(exc)[:140]
        finally:
            await runner.cleanup()

    with tempfile.TemporaryDirectory() as td:
        for i, (label, entries) in enumerate(forged):
            wt = _host_worktree(td, f"f{i}")
            before_git = _git_state(wt)
            err = asyncio.run(collect(wt, _raw_tar(entries)))
            check(f"{label}: refused", err is not None and "refused" in err, err or "accepted")
            check(f"{label}: .git unchanged (no hook, no config)",
                  _git_state(wt) == before_git and not (wt / ".git" / "hooks" / "pre-commit").exists(),
                  sorted(set(_git_state(wt)) ^ set(before_git)))
            seed_file = wt / "src" / "a.txt"
            check(f"{label}: the worktree was not cleared",
                  seed_file.is_file() and seed_file.read_text() == "seed\n")

        # .git-touching members WITHOUT a write-through are dropped, the rest lands
        wt = _host_worktree(td, "drop")
        before_git = _git_state(wt)
        err = asyncio.run(collect(wt, _raw_tar([
            ("dir", "."), ("file", "out.txt", b"built\n"), ("file", ".GIT/config", b"[core]\n"),
            ("sym", "./hooks", ".git/hooks"), ("hard", "./hc", ".git/config"),
            ("file", "vendor/.Git/HEAD", b"ref: x\n")])))
        check("an export carrying .GIT/, vendor/.Git, a link and a hard link into .git "
              "is collected with those dropped", err is None and (wt / "out.txt").read_text() == "built\n"
              and not os.path.lexists(wt / ".GIT") and not os.path.lexists(wt / "hooks")
              and not os.path.lexists(wt / "hc") and not os.path.lexists(wt / "vendor" / ".Git"),
              err or sorted(p.name for p in wt.iterdir()))
        check("...and .git is unchanged", _git_state(wt) == before_git)

        # a legitimate export: links to directories and files, nothing under them
        wt = _host_worktree(td, "legit")
        err = asyncio.run(collect(wt, _raw_tar([
            ("dir", "."), ("dir", "./documentation"), ("file", "documentation/i.md", b"# i\n"),
            ("sym", "./docs", "documentation"), ("dir", "./node_modules/pkg/bin"),
            ("file", "node_modules/pkg/bin/tool", b"#!/bin/sh\n"), ("dir", "./node_modules/.bin"),
            ("sym", "./node_modules/.bin/tool", "../pkg/bin/tool"), ("file", "src/a.txt", b"edited\n"),
            # pnpm's shape: a link whose TARGET goes through another link
            ("dir", "./node_modules/.pnpm/dep@1/node_modules/dep/bin"),
            ("file", "node_modules/.pnpm/dep@1/node_modules/dep/bin/d", b"#!/bin/sh\n"),
            ("sym", "./node_modules/.bin/d", "../dep/bin/d"),
            ("sym", "./node_modules/dep", ".pnpm/dep@1/node_modules/dep"),
            ("sym", "./node_modules/.bin/d2", "../dep/bin/d")])))    # after dep: goes through it
        check("a real export with links to a dir and a file is collected, links intact",
              err is None and os.readlink(wt / "docs") == "documentation"
              and os.readlink(wt / "node_modules" / ".bin" / "tool") == "../pkg/bin/tool"
              and (wt / "src" / "a.txt").read_text() == "edited\n", err or "")
        check("...including pnpm's link-through-a-link (.bin/d -> ../dep/bin/d, dep a link)",
              err is None and (wt / "node_modules" / ".bin" / "d").read_bytes() == b"#!/bin/sh\n")
        check("...and no staging directory is left in the worktree",
              not [p for p in wt.iterdir() if p.name.startswith(".cseries-collect-")])

    print("== 12c. what a real workspace can hold is DROPPED, not the whole collect (N1, N4) ==")
    with tempfile.TemporaryDirectory() as td:
        for i, (label, entries, gone, kept) in enumerate([
            # 12c. both orders. The target normalises to "." lexically but
            # climbs out through `a`; only that link is dropped.
            ("a -> ., b -> a/..", [("sym", "./a", "."), ("sym", "./b", "a/..")], ["b"], ["a"]),
            ("REVERSED: b -> a/.., a -> .", [("sym", "./b", "a/.."), ("sym", "./a", ".")], ["b"], ["a"]),
            # 12c. a venv's absolute interpreter link is regenerable, and would
            # point at the HOST's file; the collect keeps everything else.
            ("venv bin/python -> /usr/bin/python3", [("dir", "./venv/bin"),
                                                     ("sym", "./venv/bin/python", "/usr/bin/python3"),
                                                     ("file", "venv/pyvenv.cfg", b"home = /usr/bin\n")],
             ["venv/bin/python"], ["venv/pyvenv.cfg"]),
            ("a link climbing out of the tree", [("sym", "./up", "../../outside")], ["up"], []),
            ("a FIFO", [("fifo", "./pipe")], ["pipe"], []),
            # 12c. links match case-SENSITIVELY, like the filesystem
            ("Docs -> documentation beside a real docs/", [
                ("dir", "./documentation"), ("file", "documentation/a.md", b"a\n"),
                ("sym", "./Docs", "documentation"), ("dir", "./docs"), ("file", "docs/b.md", b"b\n")],
             [], ["Docs", "docs/b.md"]),
        ]):
            wt = _host_worktree(td, f"d{i}")
            before_git = _git_state(wt)
            err = asyncio.run(collect(wt, _raw_tar([("dir", "."), *entries,
                                                    ("file", "out.txt", b"built\n")])))
            check(f"{label}: collected (not refused)", err is None
                  and (wt / "out.txt").read_text() == "built\n", err or "")
            check(f"{label}: {gone or 'nothing'} dropped, {kept or 'the rest'} kept",
                  err is None and not any(os.path.lexists(wt / g) for g in gone)
                  and all(os.path.lexists(wt / k) for k in kept),
                  sorted(str(p.relative_to(wt)) for p in wt.rglob("*") if ".git" not in p.parts)[:12])
            check(f"{label}: .git unchanged", _git_state(wt) == before_git)

    print("== 12b. the fallback collect cannot write into .git through a link on disk ==")
    with tempfile.TemporaryDirectory() as td:
        wt = _host_worktree(td, "fb")
        before_git = _git_state(wt)
        os.symlink(".git/hooks", wt / "h")          # a link already on disk in the worktree
        body = _raw_tar([("dir", "."), ("file", "h/pre-commit", b"#!/bin/sh\n"),
                         ("file", "index.html", b"<h1>x</h1>")])

        async def fb():
            runner, base = await _serve_export(body)
            try:
                async with aiohttp.ClientSession() as http:
                    return await rv._collect_out_fallback(http, base, "scratch", wt, before={})
            finally:
                await runner.cleanup()
        try:
            landed = asyncio.run(fb())
        except Exception as exc:  # noqa: BLE001
            landed = repr(exc)[:120]
        check("fallback: h/pre-commit (resolving into .git/hooks) is dropped, index.html lands",
              landed == 1 and (wt / "index.html").exists()
              and not (wt / ".git" / "hooks" / "pre-commit").exists(), f"landed={landed}")
        check("...and .git is unchanged", _git_state(wt) == before_git,
              sorted(set(_git_state(wt)) ^ set(before_git)))


def host_git_checks() -> None:
    print("== 13. host-side git ignores a planted diff.external and textconv ==")
    from server.routes import review as rv
    from server.routes import project_git as pg
    with tempfile.TemporaryDirectory() as td:
        wt = _host_worktree(td, "diff")
        marks = pathlib.Path(td) / "marks"
        marks.mkdir()
        for name in ("ext", "tc"):
            s = pathlib.Path(td) / f"{name}.sh"
            s.write_text(f"#!/bin/sh\necho ran >> {marks}/{name}\n" + ('cat "$1"\n' if name == "tc" else ""))
            s.chmod(0o755)
        # what a write into .git would plant, plus a worktree .gitattributes
        # (worktree content IS the harness's) naming the planted driver
        with open(wt / ".git" / "config", "a") as fh:
            fh.write(f"[diff]\n\texternal = {td}/ext.sh\n[diff \"x\"]\n\ttextconv = {td}/tc.sh\n")
        (wt / ".gitattributes").write_text("* diff=x\n")
        (wt / "src" / "a.txt").write_text("changed by the run\n")
        out = rv._worktree_diff_host(wt)
        check("_worktree_diff_host: the planted diff.external did not run",
              not (marks / "ext").exists())
        check("_worktree_diff_host: the planted textconv did not run", not (marks / "tc").exists())
        # A .git/config holding a key the kernel never writes is
        # refused before git runs (project_git.check_repo); the -c set below
        # stays the second wall for git run past that check.
        check("_worktree_diff_host: a planted config is refused, no diff is read", out == "", out[:160])
        clean = _host_worktree(td, "clean")
        (clean / "src" / "a.txt").write_text("changed by the run\n")
        real = rv._worktree_diff_host(clean)
        check("_worktree_diff_host: on the kernel's own worktree the diff is real (the edit is in it)",
              "+changed by the run" in real, real[:160])
        try:
            pg._hardened_cmd(wt, "host", "example.invalid")
            why = ""
        except OSError as exc:
            why = str(exc)
        check("project_git._hardened_cmd refuses the planted tree, naming the key",
              "diff.external" in why, why)
        base = ["git", *pg.HARDENED_GIT_CONFIG, "-C", str(wt)]
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        subprocess.run([*base, "add", "-A", "--", "src"], env=env, capture_output=True)
        flags = tuple(getattr(pg, "HARDENED_DIFF_FLAGS", ()))
        r = subprocess.run([*base, "diff", "--cached", "--quiet", *flags], env=env, capture_output=True)
        check("project_git's base + HARDENED_DIFF_FLAGS: diff --quiet runs nothing planted and "
              "still reports the change (rc 1)", r.returncode == 1 and not (marks / "ext").exists()
              and not (marks / "tc").exists(), f"rc={r.returncode} {r.stderr[:120]!r}")
        r = subprocess.run([*base, "diff", "--cached", *flags], env=env, capture_output=True)
        check("project_git's base + HARDENED_DIFF_FLAGS: a full diff runs nothing planted",
              r.returncode == 0 and not (marks / "ext").exists() and not (marks / "tc").exists(),
              f"rc={r.returncode}")

    # Every hardened `diff` call carries the flags. With diff.external empty,
    # the Host's git 2.39.5 exits 128 on a diff without --no-ext-diff (measured),
    # so a call that forgets them fails closed; this makes it fail here first.
    missing = []
    for rel, base_names in (("server/routes/review.py", ("_g",)),
                            ("server/routes/project_git.py", ("base",))):
        src = (ROOT / rel).read_text()
        for m in re.finditer(r'(?:\[\*(\w+),|_run\((\w+), \w+,)\s*"diff"([^\n]*)', src):
            if (m.group(1) or m.group(2)) in base_names and "HARDENED_DIFF_FLAGS" not in m.group(3):
                missing.append(f"{rel}:{src[:m.start()].count(chr(10)) + 1}")
    check("every hardened `git diff` call passes HARDENED_DIFF_FLAGS", not missing, missing)
    from server.routes import project_git as pg2
    cfg = tuple(getattr(pg2, "HARDENED_GIT_CONFIG", ()))
    check("the shared config carries hooksPath, fsmonitor and diff.external",
          {"core.hooksPath=/dev/null", "core.fsmonitor=false", "diff.external="} <= set(cfg), cfg)
    g, _ = rv._hardened_git(pathlib.Path("/nonexistent"))
    c, _ = pg2._hardened_cmd(pathlib.Path("/nonexistent"), "u", "h")
    check("review._hardened_git and project_git._hardened_cmd both use it",
          bool(cfg) and tuple(g[1:1 + len(cfg)]) == cfg and tuple(c[1:1 + len(cfg)]) == cfg)


# ── the clocks the timed checks read (sections 14 and 17) ───────────────────
#
# Each timed check states a property of this code: a parse costs under so
# much, the event loop is never held longer than so much. A gate run shares
# the machine with other gates, so no check may count the time this process
# waits for a CPU. A cost is the CPU the child process spent
# (time.process_time, all its threads).
#
# A loop stall is read per gap between two runs of a ticker on the loop, and
# is the SMALLER of two clocks:
#   cpu    the CPU the process spent in the gap, all threads;
#   wall   the gap by the wall clock, past the ticker's sleep, less `queued`:
#          the time the loop thread itself waited in the run queue for a CPU
#          (run_delay, field 2 of /proc/thread-self/schedstat).
# Code that holds the loop counts on both. Work on the loop is CPU, and the
# loop thread is running, not queued. A thread holding the GIL spends CPU
# while the loop thread sleeps waiting for the GIL; only its wake every 5 ms
# to ask for it can queue. Measured on collect_limits_deep: 200 ms of Python
# on the loop reads 200 ms alone and 410-414 ms beside 24 busy loops; a
# second thread holding the GIL in one sort of 1.6 million floats (225 ms of
# its CPU alone, 258-267 ms loaded) reads 271 ms alone and 317-330 ms loaded.
# Each clock alone also counts something no code did, which the other does not:
#   - cpu: another thread spending CPU while the loop thread waits for a CPU
#     (work that releases the GIL, or the GIL taken back before the loop
#     thread is given a CPU to take it). Measured on the reference machine beside 24 busy
#     loops on 8 cores and the three other collect_limits cases: one gap of
#     collect_limits was 106 ms of CPU, and of its 302 ms the loop thread
#     spent 287 ms waiting for a CPU (15 ms by `wall`).
#   - wall: a thread holding the GIL while the machine gives its CPU to
#     someone else, which holds the loop and spends nothing. Measured
#     beside 24 busy loops: collect_limits_deep's loop went 153 ms
#     without a tick by `wall`, and the process spent 29 ms of CPU in that time.
# `queued` is read just inside each tick's other clock reads, so a wait that
# straddles a tick counts as stall, never against one. Where the kernel keeps
# no schedstat it reads 0, and `wall` is the plain wall clock. Neither clock
# counts a wait outside the process (a disk, a child process) with the loop
# blocked: every stall these checks have caught was work.
#
# The clocks are read WITHOUT letting go of the GIL. Python's file I/O lets
# go of it around its system calls, and while a worker thread runs Python
# the loop thread then waits up to a switch interval (5 ms) to get it back.
# Reading schedstat with open() and read() makes six system calls, twice a
# tick, so the ticker's own reads were most of the stall it measured. On a
# quiet reference machine (8 cores), worst gap of the collect_limits_deep tar phase in
# a worker: 46-47 ms with those reads, of which the reads took up to 41 ms;
# 10.4 ms with the GIL kept (a plain Python loop in the worker: 46 ms and
# 10.3 ms). So run_delay is read by a libc pread through ctypes.PyDLL,
# which keeps the GIL for the call, from a descriptor each thread opens once
# (/proc/thread-self is the opening thread's own file).
_CHILD_CLOCKS = r'''
import asyncio as _asyncio, time as _time

def _schedstat_reader():
    import ctypes, os, threading
    try:
        pread = ctypes.PyDLL(None).pread
    except (OSError, AttributeError):
        return lambda: None
    pread.restype = ctypes.c_ssize_t
    pread.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_long)
    buf = ctypes.create_string_buffer(256)
    fds = {}

    def read():
        tid = threading.get_native_id()
        fd = fds.get(tid)
        if fd is None:
            try:
                fd = os.open("/proc/thread-self/schedstat", os.O_RDONLY | os.O_CLOEXEC)
            except OSError:
                fd = -1                    # no schedstat here: never opened again
            fds[tid] = fd
        if fd < 0:
            return None
        n = pread(fd, buf, len(buf), 0)
        return buf.raw[:n] if n > 0 else None
    return read

_schedstat = _schedstat_reader()

def _queued():
    """The seconds this thread has waited in the run queue for a CPU so
    far (schedstat run_delay); 0.0 where the kernel does not keep it. Read
    with the GIL held (see the note above _CHILD_CLOCKS)."""
    try:
        return int(_schedstat().split()[1]) / 1e9
    except (AttributeError, ValueError, IndexError):
        return 0.0

class _Ticker:
    """A ticker on the running loop. gaps: the loop stall between two of its
    runs, min(cpu, wall - queued); and per gap, for the record, cpu: the CPU
    the process spent; wall: the wall clock past the sleep; queued: the loop
    thread's wait for a CPU."""
    def __init__(self, sleep):
        self.sleep, self.stop = sleep, False
        self.gaps, self.cpu, self.wall, self.queued = [0.0], [0.0], [0.0], [0.0]

    @staticmethod
    def _read():
        q_in = _queued()
        now, cpu = _time.perf_counter(), _time.process_time()
        return q_in, now, cpu, _queued()

    async def run(self):
        _, last, cpu, q_out = self._read()
        while not self.stop:
            await _asyncio.sleep(self.sleep)
            q_in, now, cpu2, q_out2 = self._read()
            c, w, q = cpu2 - cpu, now - last - self.sleep, max(q_in - q_out, 0.0)
            self.gaps.append(min(c, w - q)); self.cpu.append(c)
            self.wall.append(w); self.queued.append(q)
            last, cpu, q_out = now, cpu2, q_out2

    def record(self):
        """The largest stall in ms, the three clocks of that gap, and the
        largest gap by cpu and by wall alone."""
        i = max(range(len(self.gaps)), key=self.gaps.__getitem__)
        return {"stall_ms": round(self.gaps[i] * 1000, 1),
                "stall_gap_ms": {"cpu": round(self.cpu[i] * 1000, 1),
                                 "wall": round(self.wall[i] * 1000, 1),
                                 "queued": round(self.queued[i] * 1000, 1)},
                "stall_cpu_ms": round(max(self.cpu) * 1000, 1),
                "stall_wall_ms": round(max(self.wall) * 1000, 1)}
'''


# ── 14. bounded, linear, and off the event loop ─────────────────────────────
#
# Run in a CHILD process with a timeout: on the unfixed tree the 160,000-
# component walk held the event loop for ~45 s, and a gate
# must report that as a FAIL, not hang on it.
_B1_CHILD = _CHILD_CLOCKS + r'''
import asyncio, gzip, io, json, os, sys, tarfile, tempfile, time, pathlib
sys.path.insert(0, sys.argv[1])
from server.routes import review as rv
import aiohttp
from aiohttp import web

def one_member(name, gz):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        ti = tarfile.TarInfo(name); ti.size = 1; tf.addfile(ti, io.BytesIO(b"x"))
    data = buf.getvalue()
    return gzip.compress(data) if gz else data

async def measured(coro):
    tk = _Ticker(0.01)
    t = asyncio.create_task(tk.run())
    await asyncio.sleep(0.02)
    c0, t0 = time.process_time(), time.perf_counter()
    try:
        await coro; err = None
    except Exception as e:
        err = repr(e)[:160]
    el, wall = time.process_time() - c0, time.perf_counter() - t0
    tk.stop = True; await t
    return {"elapsed": el, "max_gap": max(tk.gaps), "err": err,
            "wall_s": round(wall, 3), "max_gap_wall": round(max(tk.wall), 3),
            "max_gap_cpu": round(max(tk.cpu), 3), "max_gap_clocks_ms": tk.record()["stall_gap_ms"]}

async def collect(body):
    app = web.Application()
    app.router.add_get("/workspace/export", lambda _r: web.Response(body=body))
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()   # loopback: this child only
    base = "http://127.0.0.1:%d" % site._server.sockets[0].getsockname()[1]
    with tempfile.TemporaryDirectory() as td:
        seed = pathlib.Path(td) / "seed"; (seed / "src").mkdir(parents=True)
        (seed / "src" / "a.txt").write_text("seed\n")
        wt = rv._prepare_worktree(pathlib.Path(td) / "w", "codex", seed=True, seed_from=seed)
        try:
            async with aiohttp.ClientSession() as http:
                res = await measured(rv._collect_out(http, base, "d", wt))
            res["seed_kept"] = (wt / "src" / "a.txt").is_file()
            return res
        finally:
            await runner.cleanup()

deep = "a/" * 160000 + "f"
case = sys.argv[2]
if case == "gzip":
    out = asyncio.run(collect(one_member(deep, True)))
    out["body_bytes"] = len(one_member(deep, True))
elif case == "plain":
    out = asyncio.run(collect(one_member(deep, False)))
elif case == "direct":
    t0, w0 = time.process_time(), time.perf_counter()
    try:
        rv._collect_out_members([tarfile.TarInfo(deep)]); err = None
    except RuntimeError as e:
        err = repr(e)[:120]
    out = {"elapsed": time.process_time() - t0, "wall_s": time.perf_counter() - w0, "err": err}
elif case == "linear":
    # The SAME number of path components (256,000), shallow-and-wide vs
    # deep-and-narrow. A linear walk costs about the same for both; one that
    # rebuilds a key per component costs ~8x more for the deep one.
    def members(n, depth):
        mem = []
        for i in range(n):
            ti = tarfile.TarInfo("/".join("d%d" % k for k in range(depth - 1)) + "/f%d" % i)
            mem.append(ti)
        for i in range(max(n // 10, 1)):
            ti = tarfile.TarInfo("/".join("d%d" % k for k in range(depth - 2)) + "/l%d" % i)
            ti.type = tarfile.SYMTYPE; ti.linkname = "x"; mem.append(ti)
        return mem
    def best(mem):
        ts = []
        for _ in range(3):
            t0 = time.process_time(); kept = len(rv._collect_out_members(mem))
            ts.append(time.process_time() - t0)
        return min(ts), kept
    try:
        shallow, k1 = best(members(8000, 32))
        deep, k2 = best(members(1000, 256))
        out = {"shallow": shallow, "deep": deep, "ratio": deep / max(shallow, 1e-6),
               "kept": k1 + k2, "err": None}
    except RuntimeError as e:
        out = {"err": repr(e)[:120]}
elif case == "ndjson":
    class C:
        def __init__(self, chunks): self.chunks = chunks
        async def iter_any(self):
            for c in self.chunks: yield c
    async def drain(chunks):
        n = 0
        async for _ in rv._iter_ndjson_lines(C(chunks)): n += 1
        return n
    big = [b"x" * 65536] * 384 + [b"\n"]            # one 24 MiB record, 64 KiB at a time
    t0 = time.process_time(); n1 = asyncio.run(drain(big)); t1 = time.process_time() - t0
    many = [b"\n" * 262144 + b"tail"]                # 262,144 blank lines in ONE 256 KiB chunk
    t0 = time.process_time(); n2 = asyncio.run(drain(many)); t2 = time.process_time() - t0
    recs = [b"{}\n" * 87381]                          # 87,381 records in ONE 256 KiB chunk
    t0 = time.process_time(); n3 = asyncio.run(drain(recs)); t3 = time.process_time() - t0
    out = {"big_s": t1, "big_n": n1, "many_s": t2, "many_n": n2, "recs_s": t3, "recs_n": n3}
print(json.dumps(out))
'''


# ── TIMED CHECKS ON A LOADED MACHINE ────────────────────────────────────────
#
# Sections 14 and 17 once timed their children by the wall clock and measured
# each up to five times, keeping the first attempt within bounds: waiting for a
# CPU was counted as if this code had blocked (measured: 172.8 ms
# against 100 for collect_padsize inside pre_merge_gates.sh, 9 ms alone). The
# children now read their own clocks (_CHILD_CLOCKS), which a busy machine does
# not move, so each is measured once. Nor is a child stopped for being slow:
# only for spending more than its CPU budget (a runaway), or for spending no
# CPU at all for CHILD_IDLE_S (a hang). A live process on a busy machine still
# gets a CPU many times a second.
CHILD_IDLE_S = 60


def _cpu_s(pid: int) -> float:
    """utime + stime of a process, all its threads (/proc/<pid>/stat)."""
    with open(f"/proc/{pid}/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def _run_child(code: str, case: str, cpu_budget_s: float) -> dict:
    """Run a timed child to its end and return the JSON on its last line;
    {"timeout": why} if it was stopped, {"crash": tail} if it printed none."""
    # The child's scratch is a folder this side makes and removes, so a child
    # that is stopped leaves nothing behind (its cases write up to ~300 MiB).
    scratch = tempfile.mkdtemp(prefix="child-")
    try:
        # Its own process group: the out-of-process file server it may start
        # goes with it, whether it ends, crashes or is stopped.
        proc = _spawn([sys.executable, "-c", code, str(ROOT), case],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                      env={**os.environ, "TMPDIR": scratch})
        spent, idle = 0.0, 0
        while True:
            try:
                out, err = proc.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                pass
            try:
                now = _cpu_s(proc.pid)
            except (OSError, ValueError, IndexError):
                continue                     # exiting: communicate() collects it
            idle = idle + 1 if now <= spent else 0
            spent = now
            if spent > cpu_budget_s or idle >= CHILD_IDLE_S:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                return {"timeout": (f"spent over {cpu_budget_s:.0f} s of CPU" if spent > cpu_budget_s
                                    else f"spent no CPU for {CHILD_IDLE_S} s")}
    finally:
        with contextlib.suppress(NameError):
            _kill_group(proc)                              # whatever of its group is left
        shutil.rmtree(scratch, ignore_errors=True)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"crash": (err or out)[-400:]}


def _b1_child(case: str, cpu_budget_s: float = 25.0) -> dict:
    return _run_child(_B1_CHILD, case, cpu_budget_s)


# Section 14's timing bounds, in seconds: a cost in CPU, a tick gap a loop
# stall (see _CHILD_CLOCKS).
_B1_S = 1.0             # a hostile export refused
_B1_GAP_S = 0.25        # the largest event-loop tick gap meanwhile
_B1_DIRECT_S = 0.05     # _collect_out_members refusing a 160,000-component name
_B1_RATIO = 2.5         # deep walk / shallow walk
_B1_DEEP_S = 1.5        # the deep walk itself
_NDJSON_S = 0.5         # each /run/stream framing case


def export_limits_checks() -> None:
    print("== 14. B1: a hostile export is refused fast, bounded, and off the event loop ==")
    r = _b1_child("gzip")
    check("the probe payload (one 160,000-component member, gzip) is refused",
          "refused" in str(r.get("err")), r)
    check("...in well under a second of CPU", r.get("elapsed", 99) < _B1_S, r)
    check("...without holding the event loop (largest tick gap < 250 ms, see _CHILD_CLOCKS)",
          r.get("max_gap", 99) < _B1_GAP_S, r)
    check("...and the worktree is untouched", r.get("seed_kept") is True, r)
    r = _b1_child("plain")
    check("the same member uncompressed (a 320 KB pax name) is refused fast, loop free",
          "refused" in str(r.get("err")) and r.get("elapsed", 99) < _B1_S
          and r.get("max_gap", 99) < _B1_GAP_S and r.get("seed_kept") is True, r)
    r = _b1_child("direct")
    check("_collect_out_members refuses a 160,000-component name before walking it (< 50 ms of CPU)",
          "refused" in str(r.get("err")) and r.get("elapsed", 99) < _B1_DIRECT_S, r)
    r = _b1_child("linear")
    check("the member walk is linear: 256,000 components cost the same 32 deep or 256 deep "
          "(ratio < 2.5; a per-step key rebuild is ~8)",
          r.get("err") is None and r.get("ratio", 99) < _B1_RATIO and r.get("deep", 99) < _B1_DEEP_S, r)
    r = _b1_child("ndjson")
    check("/run/stream framing is linear: one 24 MiB record in 64 KiB chunks (< 0.5 s of CPU)",
          r.get("big_n") == 1 and r.get("big_s", 99) < _NDJSON_S, r)
    check("...262,144 blank lines in ONE 256 KiB chunk are skipped, not yielded (< 0.5 s)",
          r.get("many_n") == 1 and r.get("many_s", 99) < _NDJSON_S, r)
    check("...and 87,381 records in ONE 256 KiB chunk (< 0.5 s)",
          r.get("recs_n") == 87381 and r.get("recs_s", 99) < _NDJSON_S, r)

    print("== 14b. the limits themselves (each check stands alone) ==")
    import gzip
    import aiohttp
    from aiohttp import web
    from server.routes import review as rv

    async def collect_with(wt, body, *, chunked=False):
        app = web.Application()

        async def h(_r):
            if not chunked:
                return web.Response(body=body)
            resp = web.StreamResponse()
            resp.enable_chunked_encoding()
            await resp.prepare(_r)
            for i in range(0, len(body), 4096):
                await resp.write(body[i:i + 4096])
            await resp.write_eof()
            return resp
        app.router.add_get("/workspace/export", h)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)       # loopback: only this gate dials it
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            async with aiohttp.ClientSession() as http:
                await rv._collect_out(http, base, "d", wt)
            return None
        except Exception as exc:  # noqa: BLE001
            return repr(exc)[:160]
        finally:
            await runner.cleanup()

    def isolated(label, fn):
        """One check, one verdict: an older tree that lacks a name this check
        uses FAILS here with the reason, and the next check still runs."""
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(label, False, repr(exc)[:160])

    ok_tar = _raw_tar([("dir", "."), ("file", "out.txt", b"x" * 6000)])

    def with_limits(**over):
        """Run collect_with under temporarily lowered limits; setattr, so a
        tree without the name gets one it never reads (and the check FAILS)."""
        saved = {k: getattr(rv, k, None) for k in over}
        for k, v in over.items():
            setattr(rv, k, v)
        return saved

    def restore(saved):
        for k, v in saved.items():
            if v is None:
                try:
                    delattr(rv, k)
                except AttributeError:
                    pass
            else:
                setattr(rv, k, v)

    def _cap_declared():
        with tempfile.TemporaryDirectory() as td:
            wt = _host_worktree(td, "lim")
            saved = with_limits(_EXPORT_MAX_BYTES=4096)
            try:
                err = asyncio.run(collect_with(wt, ok_tar))
            finally:
                restore(saved)
            check("a body over the byte cap (declared Content-Length) is refused",
                  err is not None and "cap" in err, err or "accepted")
    isolated("a body over the byte cap (declared Content-Length) is refused", _cap_declared)

    def _cap_streamed():
        with tempfile.TemporaryDirectory() as td:
            wt = _host_worktree(td, "lim")
            saved = with_limits(_EXPORT_MAX_BYTES=4096)
            try:
                err = asyncio.run(collect_with(wt, ok_tar, chunked=True))
            finally:
                restore(saved)
            check("...and over the cap with NO Content-Length (streamed) too",
                  err is not None and "cap" in err, err or "accepted")
    isolated("...and over the cap with NO Content-Length (streamed) too", _cap_streamed)

    def _members():
        with tempfile.TemporaryDirectory() as td:
            wt = _host_worktree(td, "lim")
            saved = with_limits(_EXPORT_MAX_MEMBERS=5)
            try:
                err = asyncio.run(collect_with(wt, _raw_tar([("dir", "."), *[
                    ("file", f"f{i}", b"x") for i in range(6)]])))
            finally:
                restore(saved)
            check("a tar over the member cap is refused", err is not None and "members" in err,
                  err or "accepted")
    isolated("a tar over the member cap is refused", _members)

    def _compressed():
        with tempfile.TemporaryDirectory() as td:
            wt = _host_worktree(td, "lim")
            err = asyncio.run(collect_with(wt, gzip.compress(ok_tar)))
            check("a COMPRESSED export is refused, by name (every sidecar export is uncompressed)",
                  err is not None and "compressed" in err, err or "accepted")
            check("...and the worktree is untouched after it",
                  (wt / "src" / "a.txt").read_text() == "seed\n"
                  and not [p for p in wt.iterdir() if p.name.startswith(".cseries-collect-")])
    isolated("a COMPRESSED export is refused, by name", _compressed)

    def _long_name():
        with tempfile.TemporaryDirectory() as td:
            wt = _host_worktree(td, "lim")
            err = asyncio.run(collect_with(wt, _raw_tar([("dir", "."), ("file", "n" * 5000, b"x")])))
            check("a member name over 4096 bytes is refused", err is not None and "longer" in err,
                  err or "accepted")
    isolated("a member name over 4096 bytes is refused", _long_name)

    def _normal():
        with tempfile.TemporaryDirectory() as td:
            wt = _host_worktree(td, "lim")
            err = asyncio.run(collect_with(wt, ok_tar))
            check("a normal export still collects", err is None and (wt / "out.txt").is_file(), err or "")
    isolated("a normal export still collects", _normal)

    print("== 14c. the rest of the class (each check stands alone) ==")

    class _FakeContent:
        def __init__(self, body): self._b = body
        async def iter_chunked(self, n):
            for i in range(0, len(self._b), n):
                yield self._b[i:i + n]
        async def read(self, n=-1):
            return self._b if n is None or n < 0 else self._b[:n]

    class _FakeResp:
        def __init__(self, body, declared=True):
            self.content = _FakeContent(body)
            self.content_length = len(body) if declared else None

    async def _try(coro):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            return exc

    def _c1():
        got = asyncio.run(_try(rv._read_capped(_FakeResp(b"x" * 100, declared=False), 50, "t")))
        check("_read_capped refuses past the cap with no Content-Length", isinstance(got, RuntimeError), got)
    isolated("_read_capped refuses past the cap with no Content-Length", _c1)

    def _c2():
        got = asyncio.run(_try(rv._read_capped(_FakeResp(b"x" * 100), 50, "t")))
        check("_read_capped refuses on the declared length", isinstance(got, RuntimeError), got)
    isolated("_read_capped refuses on the declared length", _c2)

    def _c3():
        tail = asyncio.run(rv._error_tail(_FakeResp(b"e" * (10 << 20))))
        check("a harness error body is read to 4 KiB, not whole", len(tail) == 4096, len(tail))
    isolated("a harness error body is read to 4 KiB, not whole", _c3)

    def _c4():
        got = asyncio.run(_try(rv._json_capped(_FakeResp(b'{"a": "' + b"x" * 200 + b'"}'), 64, "t")))
        check("a harness JSON result over its cap is refused", isinstance(got, RuntimeError), got)
    isolated("a harness JSON result over its cap is refused", _c4)

    def _c5():
        got = asyncio.run(_try(rv._json_capped(_FakeResp(json.dumps({"ok": True}).encode()), 64, "t")))
        check("...and one under it parses", got == {"ok": True}, got)
    isolated("...and one under it parses", _c5)

    fb = _raw_tar([("dir", "."), ("file", "index.html", b"<h1>x</h1>")])

    def _c6():
        try:
            rv._fallback_file_members(gzip.compress(fb))
            fb_err = None
        except Exception as exc:  # noqa: BLE001
            fb_err = exc
        check("the fallback export refuses a compressed tar", fb_err is not None, fb_err)
    isolated("the fallback export refuses a compressed tar", _c6)

    def _c7():
        check("...and reads an uncompressed one",
              [t.name for t in rv._fallback_file_members(fb)] == ["index.html"])
    isolated("...and reads an uncompressed one", _c7)

    from server.routes import compute as cm
    wire = _raw_tar([("dir", "."), ("file", "wire.jsonl",
                                    b'{"type":"usage.record","usage":{"input":5,"output":2}}\n')])

    def _c8():
        check("session usage parses an uncompressed session tar",
              len(cm.usage_frames_from_session_tar(wire, "kimi")) == 1)
    isolated("session usage parses an uncompressed session tar", _c8)

    def _c9():
        check("...and refuses a compressed one (no decompression bomb)",
              cm.usage_frames_from_session_tar(gzip.compress(wire), "kimi") == [])
    isolated("...and refuses a compressed one (no decompression bomb)", _c9)

    def _c10():
        check("compute's session member cap is review's export member cap",
              getattr(cm, "_SESSION_TAR_MAX_MEMBERS", None) == rv._EXPORT_MAX_MEMBERS)
    isolated("compute's session member cap is review's export member cap", _c10)

    def _c11():
        rv_src = (ROOT / "server/routes/review.py").read_text()
        loop_diffs = [i + 1 for i, ln in enumerate(rv_src.splitlines())
                      if "_worktree_diff_host(" in ln and "def _worktree_diff_host" not in ln
                      and "to_thread(_worktree_diff_host" not in ln]
        check("every _worktree_diff_host call runs off the event loop", not loop_diffs, loop_diffs)
    isolated("every _worktree_diff_host call runs off the event loop", _c11)


# ── the whole-body read detector ────────────────────────────────────────────
#
# A "whole-body read" is a call that returns a harness response's ENTIRE body:
# `.json(...)` and `.text(...)` with any arguments, and `.read(...)` with no
# size or a negative/None one (`r.read()`, `r.content.read(-1)`). It counts
# when its result is actually taken: awaited directly, passed to an asyncio
# wrapper (`asyncio.wait_for(r.json(), 5)`, ensure_future, create_task,
# shield, gather), or kept in a name that is awaited later in the same
# function (a deferred future). A bounded `r.content.read(4096)` is not one.
_KNOWN_WHOLE_READS = ("_stop_leftovers", "_read_catalog_from_pod", "harness_login",
                      "harness_login_finish", "harness_stop", "harness_login_code")
_ASYNC_WRAPPERS = {"wait_for", "ensure_future", "create_task", "shield", "gather", "wait"}


def _is_whole_read(call) -> bool:
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
        return False
    attr = call.func.attr
    if attr in ("json", "text"):
        return True
    if attr != "read":
        return False
    size = call.args[0] if call.args else next(
        (k.value for k in call.keywords if k.arg == "n"), None)
    if size is None:
        return True
    try:
        v = ast.literal_eval(size)
    except ValueError:
        return False              # a name or an expression: a bounded read
    return v is None or (isinstance(v, int) and v < 0)


def _whole_body_reads(src: str) -> dict:
    """{function name: [line of each whole-body read]} over `src`."""
    found: dict = {}

    def scan(fn):
        awaited_names = set()
        nodes = []

        def walk(node):
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    if not isinstance(ch, ast.Lambda):
                        scan(ch)
                    continue
                nodes.append((node, ch))
                walk(ch)
        walk(fn)
        for parent, ch in nodes:
            if isinstance(ch, ast.Await) and isinstance(ch.value, ast.Name):
                awaited_names.add(ch.value.id)
        for parent, ch in nodes:
            if not _is_whole_read(ch):
                continue
            taken = isinstance(parent, ast.Await)
            if (isinstance(parent, ast.Call) and ch in parent.args
                    and (getattr(parent.func, "attr", None) in _ASYNC_WRAPPERS
                         or getattr(parent.func, "id", None) in _ASYNC_WRAPPERS)):
                taken = True
            if (isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is ch):
                targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
                if any(isinstance(t, ast.Name) and t.id in awaited_names for t in targets):
                    taken = True
            if taken:
                found.setdefault(fn.name, []).append(ch.lineno)

    def outermost(node):
        # every function not inside another one, wherever it sits (module
        # level, a class, an if/try block); scan() handles the nested ones
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scan(ch)
            else:
                outermost(ch)
    outermost(ast.parse(src))
    return found


_WHOLE_READ_PROBES = (
    ("counts a known reply's one read",
     "async def harness_stop(resp):\n    return await resp.json(content_type=None)\n",
     {"harness_stop": 1}),
    ("counts a SECOND read inside a known function",
     "async def harness_stop(resp):\n    a = await resp.json(content_type=None)\n"
     "    return a, await resp.text()\n", {"harness_stop": 2}),
    ("catches r.text(arg)",
     "async def f(r):\n    return await r.text('utf-8')\n", {"f": 1}),
    ("catches r.content.read(-1) and read(n=-1)",
     "async def f(r):\n    a = await r.content.read(-1)\n    return a, await r.content.read(n=-1)\n",
     {"f": 2}),
    ("catches r.read() and read(None)",
     "async def f(r):\n    return await r.read(), await r.content.read(None)\n", {"f": 2}),
    ("catches asyncio.wait_for(r.json(), ...)",
     "import asyncio\nasync def f(r):\n    return await asyncio.wait_for(r.json(), 5)\n", {"f": 1}),
    ("catches a deferred future",
     "async def f(r):\n    fut = r.json()\n    x = 1\n    return await fut\n", {"f": 1}),
    ("catches asyncio.ensure_future(r.read())",
     "import asyncio\nasync def f(r):\n    t = asyncio.ensure_future(r.read())\n    return await t\n",
     {"f": 1}),
    ("catches a read in a nested function, attributed to it",
     "async def outer(r):\n    async def inner():\n        return await r.json()\n"
     "    return await inner()\n", {"inner": 1}),
    ("finds a function defined under an if or in a class",
     "if True:\n    async def f(r):\n        return await r.json()\n"
     "class C:\n    async def g(self, r):\n        return await r.text()\n", {"f": 1, "g": 1}),
    ("does NOT count a bounded read (a size) or a sync file read",
     "async def f(r, n, fh):\n    a = await r.content.read(4096)\n    b = await r.content.read(n)\n"
     "    return a, b, fh.read()\n", {}),
)


# ── 15. the WebSocket lane ───────────────────────────────────────────────────

def ws_checks() -> None:
    print("== 15. the WebSocket lane refuses a harness path before it dials (N2) ==")
    import threading
    import types
    from fastapi import FastAPI
    from server.applications import proxy

    lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lst.bind(("127.0.0.1", 0))      # loopback on purpose: stands in for an in-pod sidecar
    lst.listen(16)
    up_port = lst.getsockname()[1]
    accepted: list = []

    def _accept():
        while True:
            try:
                c, _ = lst.accept()
            except OSError:
                return
            accepted.append(1)
            c.close()
    threading.Thread(target=_accept, daemon=True).start()

    with tempfile.TemporaryDirectory() as td:
        appdir = pathlib.Path(td) / "wsapp"
        appdir.mkdir()
        (appdir / "app.yaml").write_text(
            f"id: wsapp\nruntime:\n  kind: external\n  port: {up_port}\n")
        # The loopback WS lane also answers only the app's own window
        # (proxy._require_own_window), so every attempt below carries wsapp's
        # own window token. A tokenless attempt is closed 1008 by that gate
        # before this section's wall is reached, and the three refusals would
        # pass with the wall removed.
        from server.applications import tokens
        secret = b"s" * 32
        own_token, _ = tokens.mint(secret, app_id="wsapp", permissions=[])

        async def _secret(_pool):
            return secret
        saved = (proxy.INSTALL_ROOT, proxy.brain_service.get_pool, proxy.registry.get,
                 tokens.get_secret)

        async def _get(_pool, _app_id):
            return types.SimpleNamespace(status="active")
        proxy.INSTALL_ROOT = pathlib.Path(td)
        proxy.brain_service.get_pool = lambda: object()
        proxy.registry.get = _get
        tokens.get_secret = _secret
        app = FastAPI()
        app.include_router(proxy.router)
        # Starlette's TestClient drives the REAL serve_app_ws in-process. (A
        # uvicorn here would be no test at all on a box without a WebSocket
        # backend: measured on a machine with neither websockets nor
        # wsproto: uvicorn served the upgrade as plain HTTP through serve_app
        # and the WS route never ran. The Host image pins websockets==16.0.)
        # TestClient decodes the path once, as uvicorn does.
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
        client = TestClient(app)

        def attempt(path):
            try:
                with client.websocket_connect(
                        path, headers={"X-Application-Token": own_token}) as ws:
                    try:
                        ws.receive_text()
                    except WebSocketDisconnect as e:
                        return ("accepted-then-closed", e.code)
                    return ("accepted", None)
            except WebSocketDisconnect as e:
                return ("closed-before-accept", e.code)
            except Exception as e:  # noqa: BLE001
                return ("error", repr(e)[:80])
        try:
            for path in ("/system-apps/wsapp/x/%252e%252e/workspace/export?dir=",
                         "/system-apps/wsapp/workspace/load", "/system-apps/wsapp/run/progress"):
                before = len(accepted)
                r = attempt(path)
                time.sleep(0.3)
                check(f"WS {path}: closed before accept (1008), and nothing was dialled",
                      len(accepted) == before and r == ("closed-before-accept", 1008),
                      f"{r}, dials={len(accepted) - before}")
            before = len(accepted)
            r = attempt("/system-apps/wsapp/websockify")
            time.sleep(0.3)
            check("NEGATIVE CONTROL: an ordinary WS path IS accepted and dialled",
                  len(accepted) > before and r[0].startswith("accepted"),
                  f"{r}, dials={len(accepted) - before}")
        finally:
            (proxy.INSTALL_ROOT, proxy.brain_service.get_pool, proxy.registry.get,
             tokens.get_secret) = saved
            client.close()
            lst.close()


# ── 16. propose_commit and every other host-side git ─────────────────────────

def propose_commit_checks() -> None:
    print("== 16. propose_commit stages with the hardened git; .git picks in any case refused (N3) ==")
    from server.routes import review as rv
    with tempfile.TemporaryDirectory() as tdn:
        td = pathlib.Path(tdn)
        sessions = td / "sessions"
        sd = sessions / "s1"
        wt = sd / "work" / "codex"
        wt.mkdir(parents=True)
        (wt / "page.html").write_text("<p>picked</p>\n")
        (wt / ".GIT").mkdir()
        (wt / ".GIT" / "config").write_text("[core]\n")
        proj = td / "proj"
        proj.mkdir()
        g = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run([*g, "init", "-q"], cwd=proj, check=True)
        (proj / "page.html").write_text("<p>old</p>\n")
        (proj / ".gitattributes").write_text("* diff=x\n")
        subprocess.run([*g, "add", "-A"], cwd=proj, check=True)
        subprocess.run([*g, "commit", "-qm", "base"], cwd=proj, check=True)
        marks = td / "marks"
        marks.mkdir()
        for name in ("ext", "tc"):
            s = td / f"{name}.sh"
            s.write_text(f"#!/bin/sh\necho ran >> {marks}/{name}\n" + ('cat "$1"\n' if name == "tc" else ""))
            s.chmod(0o755)
        with open(proj / ".git" / "config", "a") as fh:
            fh.write(f"[diff]\n\texternal = {td}/ext.sh\n[diff \"x\"]\n\ttextconv = {td}/tc.sh\n")
        (sd / "manifest.json").write_text(json.dumps({
            "session_id": "s1", "project_dir": str(proj), "worktree_root": str(sd / "work"),
            "variants": [{"vid": "codex", "harness": "codex"}]}))
        saved = rv.SESSIONS_ROOT
        rv.SESSIONS_ROOT = sessions

        def _pc(picks):
            try:
                return asyncio.run(rv.propose_commit(rv.ProposeCommitReq(session_id="s1", picks=picks)))
            except Exception as exc:  # noqa: BLE001
                return {"error": repr(exc)[:200]}
        try:
            # Two calls on purpose: a refused pick must not be what keeps the
            # staging from reaching `git diff` (it did on the unfixed tree).
            out = _pc({"page.html": "codex"})
            out2 = _pc({".GIT/config": "codex"})
            # The same project once its config holds only what the kernel
            # writes: the staging runs and the diff is real (a planted
            # config is refused, not run past).
            for key in ("diff.external", "diff.x.textconv"):
                subprocess.run(["git", "config", "--unset", key], cwd=proj, check=True)
            out3 = _pc({"page.html": "codex"})
        finally:
            rv.SESSIONS_ROOT = saved
        check("propose_commit: a planted diff.external in the project repo did not run",
              not (marks / "ext").exists())
        check("propose_commit: a planted textconv did not run", not (marks / "tc").exists())
        check("propose_commit: a planted project repo is refused before any git runs, by key, "
              "and the pick still lands",
              not out.get("staged_diff") and "page.html" in (out.get("applied") or [])
              and any("diff.external" in str(f.get("error")) for f in out.get("failed", [])),
              str(out)[:200])
        check("propose_commit: on the kernel's own repo the staged diff is real",
              "+<p>picked</p>" in str(out3.get("staged_diff")) and out3.get("ok") is True,
              str(out3)[:200])
        check("propose_commit: a pick of .GIT/config is refused (the any-case .git rule)",
              any(f.get("path") == ".GIT/config" and f.get("error") == "rejected_unsafe_path"
                  for f in out2.get("failed", [])) and not (proj / ".GIT").exists(),
              str(out2)[:200])

    print("== 16b. every host-side git call in a harness-influenced tree is hardened ==")
    scope = ("server/routes/review.py", "server/routes/project_git.py",
             "server/applications/attestation.py", "server/routes/gitea_propose.py",
             "server/routes/embed.py")
    bare, shells, diffs = [], [], []
    for rel in scope:
        src = (ROOT / rel).read_text()
        lines = src.splitlines()
        for i, ln in enumerate(lines, 1):
            code = ln.split("#", 1)[0]
            for m in re.finditer(r'"git"\s*(,|\])', code):
                rest = code[m.end():] + " " + (lines[i] if i < len(lines) else "")
                if m.group(1) == "]" or not rest.lstrip().startswith("*HARDENED_GIT_CONFIG"):
                    bare.append(f"{rel}:{i}")
            if "create_subprocess_shell(" in code and "git" in "\n".join(lines[i - 1:i + 8]):
                shells.append(f"{rel}:{i}")
            if re.search(r'"diff",\s*"--', code):
                window = code + " " + " ".join(lines[i:i + 2])
                if "HARDENED_DIFF_FLAGS" not in window:
                    diffs.append(f"{rel}:{i}")
    check("every git argv starts `\"git\", *HARDENED_GIT_CONFIG`", not bare, bare)
    check("no host-side git runs through a shell", not shells, shells)
    check("every git diff carries HARDENED_DIFF_FLAGS", not diffs, diffs)


# ── 17. MEASURED: every harness-to-Host input path, adversarial payloads ────
#
# Each case runs in its own child process. The adversarial server streams its
# payload from a generator (so the server side holds a chunk at a time), and
# the case reports the PEAK RSS the process reached while handling it
# (/proc/self/clear_refs=5 resets VmHWM to the current RSS first) and the
# largest stall a 5 ms ticker on the same event loop saw (see
# _CHILD_CLOCKS). A case stopped by _run_child is a FAIL, as is one over its
# stated ceiling.
_MEM_CHILD = _CHILD_CLOCKS + r'''
import asyncio, gc, io, json, os, sys, tarfile, tempfile, time, zlib, pathlib, types
ROOT, CASE = sys.argv[1], sys.argv[2]
sys.path.insert(0, ROOT)
import aiohttp
from aiohttp import web
from server.routes import review as rv
from server import harness_input as _hi_floor
# The disk floor is tested on its own (section 22, with free space faked);
# here it is off, so a measurement never depends on how full this box is.
_hi_floor.disk_floor = lambda *a, **k: 0

MiB = 1 << 20

def st(k):
    for l in open("/proc/self/status"):
        if l.startswith(k):
            return int(l.split()[1]) / 1024.0

async def serve(handler):
    app = web.Application(client_max_size=1 << 40)
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)     # loopback: only this child dials it
    await site.start()
    return runner, "http://127.0.0.1:%d" % site._server.sockets[0].getsockname()[1]

# The adversary OUT of this process, for the cases where the Host's side does
# real CPU work in a worker thread: an in-process server writing in a tight
# loop pays CPython's GIL convoy on every sendmsg (measured: a 164 ms "stall"
# that was the test's own server; 16 ms with the server in its own process).
_FILE_SERVER = """
import asyncio, sys
from aiohttp import web
path = sys.argv[1]
async def h(r):
    resp = web.StreamResponse()
    resp.enable_chunked_encoding()
    await resp.prepare(r)
    with open(path, "rb") as fh:
        while True:
            c = fh.read(65536)
            if not c:
                break
            await resp.write(c)
    await resp.write_eof()
    return resp
async def main():
    app = web.Application(client_max_size=1 << 40)
    app.router.add_route("*", "/{tail:.*}", h)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)       # loopback: only this child dials it
    await site.start()
    print(site._server.sockets[0].getsockname()[1], flush=True)   # listening now: the parent may dial
    await asyncio.Event().wait()
asyncio.run(main())
"""

def _file_chunks(path_):
    with open(path_, "rb") as fh:
        while True:
            c = fh.read(1 << 20)
            if not c:
                return
            yield c

# case: (form, a /run/stream frame?, transcript piece, reps, diff piece, reps,
#        \\uXXXX on the wire?, open the stream view after?)
_RESULT_CASES = {
    "run_big":            ("ascii", False, "thinking... done\n" * 4096, 360, "+x = \"caf\u00e9\"\n\t" * 4096, 1092, True, False),
    "stream_big":         ("ascii", True, "thinking... done\n" * 4096, 360, "+x = \"caf\u00e9\"\n\t" * 4096, 1092, True, False),
    "stream_real_max":    ("ascii", True, "thinking... done\n" * 4096, 12, "+ const x = 1;\n" * 4096, 300, True, False),
    "wide_run":           ("utf8", False, "\U0001F600" * 65536, 384, "\u4e00" * 65536, 384, False, False),
    "wide_stream":        ("utf8", True, "\U0001F600" * 65536, 384, "\u4e00" * 65536, 384, False, True),
    "wide_escaped_stream": ("escaped", True, "\U0001F600" * 65536, 192, "\u4e00" * 65536, 192, True, True),
    "wide_mixed_stream":  ("ucs4", True, "a" * 65535 + "\U0001F600", 512, "b" * 65535 + "\U0001F600", 512, False, True),
}

async def serve_file(td, payload):
    import socket, subprocess
    path = pathlib.Path(td) / "payload.bin"
    with open(path, "wb") as fh:
        for piece in (payload if not isinstance(payload, (bytes, bytearray)) else [payload]):
            fh.write(piece)
    # The server binds its own port and prints it once it listens: no port
    # handed over between two binds, and no retrying against the clock.
    proc = subprocess.Popen([sys.executable, "-c", _FILE_SERVER, str(path)], stdout=subprocess.PIPE, text=True)
    line = await asyncio.to_thread(proc.stdout.readline)
    if not line.strip().isdigit():
        raise RuntimeError("the file server did not start (rc=%s)" % proc.poll())
    base = "http://127.0.0.1:%d" % int(line)
    class _Stop:
        async def cleanup(self):
            proc.terminate(); proc.wait()
    return _Stop(), base

async def stream(request, chunks, headers=None):
    resp = web.StreamResponse(headers=headers or {})
    resp.enable_chunked_encoding()
    await resp.prepare(request)
    try:
        for c in chunks:
            await resp.write(c)
        await resp.write_eof()
    except Exception:
        pass
    return resp

def gzip_zeros(total):
    c = zlib.compressobj(9, zlib.DEFLATED, 31)
    blk = b"\0" * MiB
    out = [c.compress(blk) for _ in range(total // MiB)]
    out.append(c.flush())
    return b"".join(out)

def repeat(piece, total, head=b"", tail=b""):
    yield head
    sent = len(head)
    while sent + len(piece) <= total:
        yield piece
        sent += len(piece)
    yield tail

def json_bomb(total):                       # [[],[],...]: ~20x as Python objects
    return repeat(b"[]," * 21845, total, b"[", b"[]]")

def pax_bomb(pax_bytes):
    # VALID pax records ("12 xx=abcde\n", 12 bytes each, ~2.8 million of
    # them in 32 MiB): tarfile parses every one into Python objects before any
    # caller check can run, which is the measured amplification.
    rec = b"12 xx=abcde\n"
    pax_bytes -= pax_bytes % len(rec)
    ti = tarfile.TarInfo("././@PaxHeader"); ti.type = tarfile.XHDTYPE; ti.size = pax_bytes
    yield ti.tobuf(format=tarfile.USTAR_FORMAT)
    left, blk = pax_bytes, rec * (65532 // len(rec))
    while left > 0:
        n = min(left, len(blk)); yield blk[:n]; left -= n
    yield b"\0" * ((512 - pax_bytes % 512) % 512)
    yield tarfile.TarInfo("x").tobuf(format=tarfile.USTAR_FORMAT)
    yield b"\0" * 1024

def _hh(name, typ, size, size_field=None):
    ti = tarfile.TarInfo(name); ti.type = typ; ti.size = size
    b = bytearray(ti.tobuf(format=tarfile.USTAR_FORMAT))
    if size_field is not None:
        b[124:136] = (size_field + b"\0" * 12)[:12]
        b[148:156] = b" " * 8; b[148:156] = b"%06o\0 " % sum(b)
    return bytes(b)

def _pad(n):
    return b"\0" * ((512 - n % 512) % 512)

def _pax_plain(n):
    rec = b"12 xx=abcde\n"; n -= n % len(rec)
    return _hh("././@PaxHeader", tarfile.XHDTYPE, n) + rec * (n // len(rec)) + _pad(n)

def v7dir_bomb(n):
    # the first bypass: tarfile reads a type-NUL `d/` as a directory
    # (no data) and parses the pax bomb the old scan skipped as its data
    hidden = _pax_plain(n) + _hh("x", tarfile.REGTYPE, 0)
    yield _hh("d/", tarfile.AREGTYPE, len(hidden)); yield hidden; yield b"\0" * 1024

def padsize_bomb(n):
    # the second bypass: a pax `size` record in a pax block's padding
    rec1, sz = b"13 path=file\n", b"12 size=512\n"
    hidden = _pax_plain(n) + _hh("x", tarfile.REGTYPE, 0)
    yield _hh("././@PaxHeader", tarfile.XHDTYPE, len(rec1))
    yield rec1 + sz + b"\0" * (512 - len(rec1) - len(sz))
    yield _hh("f", tarfile.REGTYPE, 0) + _hh("g", tarfile.REGTYPE, len(hidden))
    yield hidden; yield b"\0" * 1024

def negpax_bomb(n):
    # review item 4: a pax header of size -1 made the old scan read the rest
    yield _hh("././@PaxHeader", tarfile.XHDTYPE, 0, b"-0000000001")
    rec = b"12 xx=abcde\n" * 5461
    for _ in range(n // len(rec)):
        yield rec

def newline_member(name, size):
    ti = tarfile.TarInfo(name); ti.size = size
    yield tarfile.TarInfo(".").tobuf(format=tarfile.USTAR_FORMAT)
    yield ti.tobuf(format=tarfile.USTAR_FORMAT)
    left, blk = size, b"\n" * 65536
    while left > 0:
        n = min(left, len(blk)); yield blk[:n]; left -= n
    yield b"\0" * ((512 - size % 512) % 512) + b"\0" * 1024

def worktree(td, name="w"):
    seed = pathlib.Path(td) / ("seed-" + name); (seed / "src").mkdir(parents=True)
    (seed / "src" / "a.txt").write_text("seed\n")
    return rv._prepare_worktree(pathlib.Path(td) / name, "codex", seed=True, seed_from=seed)

async def measured(fn):
    tk = _Ticker(0.005)
    gc.collect()
    rss0 = st("VmRSS")
    open("/proc/self/clear_refs", "w").write("5")
    t = asyncio.create_task(tk.run()); await asyncio.sleep(0.02)
    c0, t0 = time.process_time(), time.perf_counter()
    try:
        val, err = await fn(), None
    except BaseException as e:            # noqa: BLE001 - the verdict is data
        val, err = None, repr(e)[:200]
    el, wall = time.process_time() - c0, time.perf_counter() - t0
    tk.stop = True; await t
    return {"elapsed_s": round(el, 2), **tk.record(), "wall_s": round(wall, 2),
            "rss_mib": round(st("VmHWM") - rss0, 1), "err": err,
            "val": val if isinstance(val, (int, float, str, bool, type(None))) else str(val)[:160]}

async def _case(td):
    if CASE in ("collect_pax", "fallback_pax", "snapshot_pax", "session_pax"):
        async def h(r): return await stream(r, pax_bomb(32 * MiB))
        runner, base = await serve(h)
        wt = worktree(td)
        sdir = pathlib.Path(td) / "sess"; sdir.mkdir()
        async with aiohttp.ClientSession() as http:
            fn = {"collect_pax": lambda: rv._collect_out(http, base, "d", wt),
                  "fallback_pax": lambda: rv._collect_out_fallback(http, base, "s", wt, before={}),
                  "snapshot_pax": lambda: rv._snapshot_fallback_dir(http, base, "s"),
                  "session_pax": lambda: rv._collect_session(http, base, sdir, "kimi")}[CASE]
            out = await measured(fn)
        out["worktree_kept"] = (wt / "src" / "a.txt").is_file()
        await runner.cleanup()
    elif CASE in ("collect_v7dir", "collect_padsize", "collect_negpax"):
        # built BEFORE the measurement: the adversary's server shares this
        # process, and its payload is not the Host's memory
        payload = b"".join({"collect_v7dir": lambda: v7dir_bomb(16 * MiB),
                            "collect_padsize": lambda: padsize_bomb(16 * MiB),
                            "collect_negpax": lambda: negpax_bomb(64 * MiB)}[CASE]())
        view = memoryview(payload)
        def gen():
            for i in range(0, len(view), 65536):
                yield view[i:i + 65536]
        async def h(r): return await stream(r, gen())
        runner, base = await serve(h)
        wt = worktree(td)
        async with aiohttp.ClientSession() as http:
            out = await measured(lambda: rv._collect_out(http, base, "d", wt))
        out["worktree_kept"] = (wt / "src" / "a.txt").is_file()
        await runner.cleanup()
    elif CASE == "session_lines":
        async def h(r): return await stream(r, newline_member("agents/main/wire.jsonl", 60 * MiB))
        runner, base = await serve(h)
        sdir = pathlib.Path(td) / "sess"; sdir.mkdir()
        async with aiohttp.ClientSession() as http:
            out = await measured(lambda: rv._collect_session(http, base, sdir, "kimi"))
        await runner.cleanup()
    elif CASE == "status_gzip":
        bomb = gzip_zeros(256 * MiB)
        async def h(r):
            return web.Response(body=bomb, headers={"Content-Encoding": "gzip",
                                                    "Content-Type": "application/json"})
        runner, base = await serve(h)
        async with aiohttp.ClientSession() as http:
            out = await measured(lambda: rv._read_catalog_from_pod(http, base, "cursor"))
        out["bomb_bytes"] = len(bomb)
        await runner.cleanup()
    elif CASE == "stop_json":
        async def h(r): return await stream(r, repeat(b"a" * 65536, 256 * MiB, b'{"stopped": "', b'"}'))
        runner, base = await serve(h)
        out = await measured(lambda: rv._stop_leftovers(base, "kimi"))
        await runner.cleanup()
    elif CASE == "run_nodes":
        runner, base = await serve_file(td, json_bomb(30 * MiB))
        async def go():
            async with aiohttp.ClientSession() as http:
                async with http.post(base + "/run", json={}) as resp:
                    reader = getattr(rv._hin, "read_record", None)
                    if reader is None:                     # the head before it
                        return await rv._json_capped(resp, rv._RUN_RESULT_MAX, "/run result")
                    return (await reader(resp, rv._RESULT_RULES, "/run result"))[0]
        out = await measured(go)
        await runner.cleanup()
    elif CASE in _RESULT_CASES:
        # A REAL result shape, too large to read whole, written to a file as the pod would send it and served
        # from another process. Sizes and forms per case (_RESULT_CASES):
        # ASCII; the largest terminal frame measured on disk (19.3 MB: an
        # 18.4 MB node_modules diff, 0.8 MB of transcript); and WIDE text
        # (limits that counted characters kept
        # 4x their size in bytes), raw UTF-8 as json.dumps(ensure_ascii=False)
        # writes it, \\uXXXX-escaped as the sidecar's json.dumps writes it, and
        # ASCII with one emoji per 64 KiB, which makes every kept Python str
        # 4 bytes a character.
        form, frame, tr_piece, tr_n, diff_piece, diff_n, ascii_wire, view = _RESULT_CASES[CASE]
        path_ = pathlib.Path(td) / "payload.json"

        def body(fh, piece, n):
            enc = json.dumps(piece, ensure_ascii=ascii_wire)[1:-1].encode("utf-8", "surrogatepass")
            for _ in range(n):
                fh.write(enc)
        with open(path_, "wb") as fh:
            head = {"ok": True, "exit_code": 0, "model": "m", "stderr": "", "usage": {"input": 5, "output": 7}}
            if frame:
                fh.write(b'{"ts": 1, "seq": 0, "kind": "text", "payload": {"text": "hi"}}\n')
                fh.write(b'{"ts": 2, "seq": 1, "kind": "status", "payload": {"state": "complete", ')
            else:
                fh.write(b"{")
            fh.write(json.dumps(head)[1:-1].encode() + b', "diff": "')
            body(fh, diff_piece, diff_n)
            fh.write(b'", "transcript": "')
            body(fh, tr_piece, tr_n)
            fh.write(b'"}}\n' if frame else b'"}')
        record_mib = round(path_.stat().st_size / MiB, 1)
        runner, base = await serve_file(td, open(path_, "rb").read() if False else _file_chunks(path_))
        sdir = pathlib.Path(td) / "sess"; sdir.mkdir()
        async def go():
            async with aiohttp.ClientSession() as http:
                if not frame:
                    async with http.post(base + "/run", json={}) as resp:
                        reader = getattr(rv._hin, "read_record", None)
                        if reader is None:
                            return await rv._json_capped(resp, rv._RUN_RESULT_MAX, "/run result")
                        obj, info = await reader(resp, rv._RESULT_RULES, "/run result")
                        if info:                     # as _run_on_pod records it
                            obj["result_cut"] = info
                        return obj
                return await rv._stream_harness_run(http, base, {"prompt": "x"}, "kimi", sdir,
                                                    idle_timeout=60, frames_seen={})
        box = {}
        async def keep():
            box["r"] = await go()
            return "done"
        out = await measured(keep)
        r = box.get("r") or {}
        d, t = r.get("diff") or "", r.get("transcript") or ""
        out.update({"ok": r.get("ok"), "usage": r.get("usage"), "record_mib": record_mib,
                    "diff_bytes": len(d.encode("utf-8", "surrogatepass")),
                    "diff_marker": "not kept by the Host]" in d[-200:],
                    "tr_bytes": len(t.encode("utf-8", "surrogatepass")),
                    "tr_marker": "not kept by the Host]" in t[:200],
                    "result_cut": bool(r.get("result_cut")),
                    "tr_src": len(tr_piece.encode("utf-8", "surrogatepass")) * tr_n,
                    "diff_src": len(diff_piece.encode("utf-8", "surrogatepass")) * diff_n,
                    "tr_keep": rv._RESULT_RULES[("transcript",)][1],
                    "diff_keep": rv._RESULT_RULES[("diff",)][1]})
        del d, t, r, box
        ev = list(sdir.glob("*.events.jsonl"))
        out["events_mib"] = round(sum(p.stat().st_size for p in ev) / MiB, 2)
        out["events_max_line_kib"] = round(max((len(ln) for p in ev for ln in open(p, "rb")), default=0) / 1024, 1)
        out["cut_note_frame"] = any(b'"result_cut"' in ln for p in ev for ln in open(p, "rb"))
        if view and ev:
            # a viewer opening the run afterwards (measured: +665 MiB and
            # a 933 ms stall on every open, from one 192 MiB events line)
            rv.SESSIONS_ROOT = pathlib.Path(td)
            gc.collect()
            rss1 = st("VmRSS"); open("/proc/self/clear_refs", "w").write("5")
            tk2 = _Ticker(0.005)
            t2 = asyncio.create_task(tk2.run()); await asyncio.sleep(0.02)
            resp = await rv.stream_session_harness_events("sess", "kimi", from_seq=0)
            nb = 0
            async for chunk in resp.body_iterator:
                nb += len(chunk)
            tk2.stop = True; await t2
            out["view"] = {**tk2.record(), "rss_mib": round(st("VmHWM") - rss1, 1),
                           "bytes_kib": round(nb / 1024, 1)}
        await runner.cleanup()
    elif CASE == "status_flood":
        # Status frames were never elided (300,000
        # of them wrote a 285 MiB file), and a caught-up live viewer's every
        # 0.5 s poll re-read the whole file (a 494-509 ms stall each)
        line = json.dumps({"kind": "status", "payload": {"state": "working", "pad": "s" * 900}}).encode() + b"\n"
        path_ = pathlib.Path(td) / "payload.json"
        with open(path_, "wb") as fh:
            for i in range(300_000):
                fh.write(line.replace(b'"working"', b'"working %d"' % i) if i == 299_999 else line)
            fh.write(json.dumps({"kind": "status", "payload": {"state": "complete", "ok": True}}).encode() + b"\n")
        runner, base = await serve_file(td, _file_chunks(path_))
        sdir = pathlib.Path(td) / "sess"; sdir.mkdir()
        async with aiohttp.ClientSession() as http:
            out = await measured(lambda: rv._stream_harness_run(
                http, base, {"prompt": "x"}, "kimi", sdir, idle_timeout=60, frames_seen={}))
        await runner.cleanup()
        evp = rv._events_path(sdir, "kimi")
        lines = evp.read_bytes().splitlines()
        out["events_mib"] = round(evp.stat().st_size / MiB, 1)
        out["frames"] = len(lines)
        out["last_two"] = [json.loads(x)["payload"].get("state") for x in lines[-2:]]
        # a live viewer, caught up at the end, polling for 3 s
        rv.SESSIONS_ROOT = pathlib.Path(td)
        n_seq = len(lines) - 1          # before the terminal frame: the tail stays open
        with open(evp, "rb") as fh:
            data = fh.read()
        with open(evp, "wb") as fh:     # the same file without its terminal line
            fh.write(b"\n".join(lines[:-1]) + b"\n")
        tk3 = _Ticker(0.005)
        resp = await rv.stream_session_harness_events("sess", "kimi", from_seq=0)
        async def drain():
            async for _ in resp.body_iterator:
                pass
        t3 = asyncio.create_task(tk3.run())
        await asyncio.sleep(0.02)
        t0 = time.perf_counter()
        try:
            await asyncio.wait_for(drain(), timeout=6.0)    # catch up, then ~6 polls caught up
        except asyncio.TimeoutError:
            pass
        tk3.stop = True; await t3
        rec3 = tk3.record()
        out["poll"] = {"worst_stall_ms": rec3.pop("stall_ms"),
                       "blocked_share": round(sum(g for g in tk3.gaps if g > 0.05) / 6.0, 3), **rec3}
    elif CASE in ("json_values_16mib", "json_values_over"):
        # json.loads holds the GIL for its whole parse, in a worker thread or
        # not (measured: 64 MiB of browser profile, 143-159 ms). The
        # value budget bounds it: 16 MiB of values just under the budget, and
        # one over it (refused by the count, before any parse).
        from server import harness_input as hi
        nvals = hi.JSON_MAX_NODES - 1000 if CASE == "json_values_16mib" else 450_000
        val = b'"' + b"a" * max(1, (16 * MiB) // nvals - 4) + b'"'
        body = b"[" + b",".join([val] * nvals) + b"]"
        out = await measured(lambda: hi.parse_json(body, CASE))
        out["values"] = nvals
        out["body_mib"] = round(len(body) / MiB, 1)
        out["val"] = None
    elif CASE == "json_escapes":
        # A body over INLINE_JSON_MAX has its values counted before it is
        # parsed (json_nodes). The count's string regex, with plain
        # quantifiers, kept sre backtracking state per escape: 330 MiB for an
        # 11.7 MiB body of a JSON-escaped diff (measured, ae41848c).
        body = json.dumps({"diff": ("+x = \"caf\u00e9\"\n\t") * (8 * MiB // 15)}).encode()
        from server import harness_input as hi
        out = await measured(lambda: hi.parse_json(body, "json_escapes"))
        out["body_mib"] = round(len(body) / MiB, 1)
    elif CASE == "stream_blank":
        # 128 MiB of bare newlines cost 27.7 s of
        # loop work with 170 ms stalls (the yield counted parsed frames only)
        runner, base = await serve_file(td, (b"\n" * MiB for _ in range(128)))
        sdir = pathlib.Path(td) / "sess"; sdir.mkdir()
        async with aiohttp.ClientSession() as http:
            out = await measured(lambda: rv._stream_harness_run(
                http, base, {"prompt": "x"}, "kimi", sdir, idle_timeout=60, frames_seen={}))
        await runner.cleanup()
    elif CASE in ("stream_flood", "stream_record", "stream_nodes"):
        body = {"stream_flood": repeat(b"{}\n" * 21845, 8 * MiB),
                "stream_record": repeat(b"x" * 65536, 64 * MiB),
                "stream_nodes": repeat(b"[]," * 21845, 20 * MiB, b'{"a": [', b'[]]}\n')}[CASE]
        runner, base = await serve_file(td, body)
        sdir = pathlib.Path(td) / "sess"; sdir.mkdir()
        async with aiohttp.ClientSession() as http:
            out = await measured(lambda: rv._stream_harness_run(
                http, base, {"prompt": "x"}, "kimi", sdir, idle_timeout=60, frames_seen={}))
        ev = list(sdir.glob("*.events.jsonl"))
        out["events_mib"] = round(sum(p.stat().st_size for p in ev) / MiB, 1)
        out["elided"] = any(b'"frames_elided"' in p.read_bytes()[-4096:] or
                            b'"frames_elided"' in p.read_bytes()[:1 << 20] for p in ev)
        await runner.cleanup()
    elif CASE == "browser_gzip":
        bomb = gzip_zeros(256 * MiB)
        async def h(r):
            return web.Response(body=bomb, headers={"Content-Encoding": "gzip"})
        runner, base = await serve(h)
        from server.vault import authz
        from server import browser_agents as ba
        authz.browser_base_url = lambda: base
        authz.browser_headers = lambda: {}
        out = await measured(lambda: ba._call("GET", "/v1/screenshot"))
        await runner.cleanup()
    elif CASE == "browser_download":
        # blocker 3 of the ae41848c review: the file a user's browser agent
        # downloaded is THEIR file. It is not capped; it streams to disk.
        runner, base = await serve_file(td, (b"%" * MiB for _ in range(64)))
        from server.vault import authz
        from server import browser_agents as ba
        authz.browser_base_url = lambda: base
        authz.browser_headers = lambda: {}
        dest = pathlib.Path(td) / "out" / "statement.csv"
        dest.parent.mkdir()
        dl = getattr(ba, "_download", None)
        async def go():
            if dl is None:                         # the head before it: a capped raw read
                st, data = await ba._call("GET", "/v1/sessions/s/downloads/statement.csv",
                                          timeout=120, raw=True)
                dest.write_bytes(data)
                return len(data)
            return await dl("/v1/sessions/s/downloads/statement.csv", dest)
        out = await measured(go)
        out["saved"] = dest.stat().st_size if dest.exists() else None
        out["part_left"] = any(p.name.endswith(".part") for p in dest.parent.iterdir())
        await runner.cleanup()
    elif CASE == "inbound_mcp":
        import socket, uvicorn
        from fastapi import FastAPI
        from scripts.gate_loopback import IDLE_KEEP_ALIVE_S
        from server.routes import mcp
        cap = "cap-for-the-inbound-case"
        rv._remember_control_cap("kimi", "http://harness-kimi.test:1", cap)
        app = FastAPI(); app.include_router(mcp.router)
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,   # loopback: this child only
                                               log_level="error", lifespan="off",
                                               timeout_keep_alive=IDLE_KEEP_ALIVE_S))
        srv = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.02)
        client = (
            "import http.client, sys\n"
            "c = http.client.HTTPConnection('127.0.0.1', %d, timeout=60)\n"
            "c.putrequest('POST', '/mcp')\n"
            "c.putheader('Transfer-Encoding', 'chunked')\n"
            "c.putheader('Content-Type', 'application/json')\n"
            "c.putheader('x-cseries-harness-cap', %r)\n"
            "c.endheaders()\n"
            "blk = b'a' * 65536; sent = 0\n"
            "try:\n"
            "    while sent < 256 * 1048576:\n"
            "        c.send(b'%%x\\r\\n' %% len(blk) + blk + b'\\r\\n'); sent += len(blk)\n"
            "    c.send(b'0\\r\\n\\r\\n')\n"
            "except OSError:\n"
            "    pass\n"
            "try:\n"
            "    print(c.getresponse().status)\n"
            "except Exception as e:\n"
            "    print('no-response', type(e).__name__)\n" % (port, cap))
        async def go():
            p = await asyncio.create_subprocess_exec(sys.executable, "-c", client,
                                                     stdout=asyncio.subprocess.PIPE)
            o, _ = await p.communicate()
            return o.decode().strip()
        out = await measured(go)
        server.should_exit = True
        await srv
    elif CASE == "derived_land":
        wt = worktree(td)
        big = wt / "src" / "big.bin"
        with open(big, "wb") as fh:
            fh.truncate(256 * MiB)               # 256 MiB the run "wrote" (sparse on disk)
        proj = pathlib.Path(td) / "projects"
        (proj / "7" / "src").mkdir(parents=True)
        (proj / "7" / "src" / "big.bin").write_bytes(b"project side\n")
        rv.PROJECTS_ROOT = proj
        base = {"src/big.bin": "0" * 40}         # both sides changed -> conflict, not a merge
        def go_sync():
            w, m, c = rv._integrate_with_project(7, wt, ["src/big.bin"], base)
            (proj / "8").mkdir()
            ch = rv._snapshot_to_project(8, wt, ["src/big.bin"])
            return "conflicts=%d copied=%d kinds=%s" % (len(c), len(ch),
                                                        ",".join(x["kind"] for x in c))
        out = await measured(lambda: asyncio.to_thread(go_sync))
    elif CASE == "derived_diff":
        wt = worktree(td)
        with open(wt / "src" / "added.txt", "wb") as fh:
            for _ in range(64):
                fh.write(b"line\n" * (MiB // 5))
        out = await measured(lambda: asyncio.to_thread(lambda: len(rv._worktree_diff_host(wt))))
    elif CASE in ("collect_limits", "collect_limits_hostile", "collect_limits_wide",
                  "collect_limits_deep", "collect_limits_refused"):
        from server import harness_input as hi
        path = pathlib.Path(td) / "limits.tar"
        n = rv._EXPORT_MAX_MEMBERS
        if CASE in ("collect_limits", "collect_limits_refused"):
            with tarfile.open(path, "w") as tf:      # tarfile.add's shape: pax path + mtime per member
                for i in range(n):
                    ti = tarfile.TarInfo(("./node_modules/.pnpm/pkg%06d/" % i).ljust(150, "d") + "/f.js")
                    if CASE == "collect_limits_refused" and i == n - 1:
                        ti = tarfile.TarInfo("a/../../f.js")    # refused by the check's LAST step
                    ti.mtime = 1727000000.25
                    tf.addfile(ti)
        elif CASE == "collect_limits_deep":
            # the member check's slowest shape measured: every member a
            # symlink named 97 components deep (all the name budget allows),
            # a link at every depth above it, so every prefix of every name
            # is looked up, and a `..` target
            with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as tf:
                for i in range(n):
                    ti = tarfile.TarInfo("a/" * (i if i < 96 else 96) + "l%06d" % i)
                    # `../x`: a target with `..` also walks the link's own
                    # directory (the first link's climbs out: dropped)
                    ti.type = tarfile.SYMTYPE; ti.linkname = "../x"
                    tf.addfile(ti)
        else:
            # EVERY limit filled at once: the member cap, all
            # 40 MiB of names (through the ustar name, prefix and link fields,
            # which cost no extension-record budget), a symlink per member (so
            # the member check builds its link table too), and every pax key
            # the scan allows, at its longest, up to the whole extension budget.
            # _wide: every name and link held 4 bytes a character (one emoji
            # each), as long as the name budget, which counts that, allows.
            def fixck(b):
                h = bytearray(b); h[148:156] = b" " * 8; b[148:156] = b"%06o\0 " % sum(h); return b
            def rec(key, val):
                body = b" " + key + b"=" + val + b"\n"; m = len(body) + 1
                while len(str(m)) + len(body) != m:
                    m += 1
                return str(m).encode() + body
            keys = getattr(hi, "_PAX_KEYS", {})
            vmax = lambda k: min(keys.get(k, 32), 256)
            ext_per = (getattr(hi, "TAR_EXT_TOTAL_MAX", 32 << 20) - 4096) // n
            names_per = (getattr(rv, "_EXPORT_MAX_NAME_BYTES", 24 << 20) - 4096) // n
            size_per = rv._EXPORT_MAX_BYTES // n
            # the longest number values the scan allows, so the retained
            # fields cost what they can at most
            fixed = b"".join(rec(k, v) for k, v in ((b"uid", b"9" * vmax(b"uid")),
                                                    (b"gid", b"9" * vmax(b"gid")),
                                                    (b"mtime", b"1" * 20 + b"." + b"3" * (vmax(b"mtime") - 21)))
                             if k in keys)
            if b"hdrcharset" in keys:
                fixed += rec(b"hdrcharset", b"BINARY")
            room = max(ext_per - len(fixed) - 40, 2)
            while True:
                body = fixed + rec(b"uname", b"u" * min(room // 2, vmax(b"uname"))) \
                    + rec(b"gname", b"g" * min(room - room // 2, vmax(b"gname")))
                if len(body) <= ext_per - 8 or room <= 2:
                    break
                room -= 1
            pad = b"\0" * ((512 - len(body) % 512) % 512)
            ti = tarfile.TarInfo("././@PaxHeader"); ti.type = tarfile.XHDTYPE; ti.size = len(body)
            xh = bytes(fixck(bytearray(ti.tobuf(format=tarfile.USTAR_FORMAT))))
            with open(path, "wb") as fh:
                for i in range(n):
                    nm = b"n/%06d" % i
                    t2 = tarfile.TarInfo("x"); t2.type = tarfile.SYMTYPE
                    mh = bytearray(t2.tobuf(format=tarfile.USTAR_FORMAT))
                    nmb = (nm + b"/" + b"n" * 99)[:min(99, names_per // 3)]
                    pre = (b"q" * 154)[:min(154, names_per // 3)]
                    # an ABSOLUTE target: every member is both a link (the
                    # link table) and dropped (the dropped table)
                    lnk = (b"/" + b"l" * 98)[:max(2, min(99, names_per - len(nmb) - len(pre) - 1))]
                    if CASE == "collect_limits_wide":
                        # 4 bytes a character: (prefix + "/" + name) and the
                        # link, 13 characters of fixed parts, the rest split
                        e = "\U0001F600".encode()
                        k = (names_per // 4 - 13) // 3
                        pre, nmb, lnk = e + b"q" * k, nm + b"/" + b"n" * k, b"/" + e + b"l" * k
                    mh[0:100] = nmb.ljust(100, b"\0"); mh[345:500] = pre.ljust(155, b"\0")
                    mh[157:257] = lnk.ljust(100, b"\0")
                    # and every header number tarfile keeps as its own int
                    # object: the largest mode and device numbers, and a size
                    # (a symlink's is not skipped over) that spends the
                    # members' 512 MiB data total
                    mh[100:108] = b"7777777\0"; mh[329:337] = b"7777777\0"; mh[337:345] = b"7777777\0"
                    mh[124:136] = b"%011o\0" % size_per
                    fh.write(xh + body + pad + bytes(fixck(mh)))
                fh.write(b"\0" * 1024)
        def parse_check():
            # exactly the worker's tar phase (_collect_out_apply), extraction
            # aside: its own _collect_out_tar where the tree has one
            import contextlib
            if hasattr(rv, "_collect_out_tar"):
                with rv._collect_out_tar(path) as (tf, members):
                    return "%d read, %d kept" % (len(tf.members), len(members))
            rv._prescan_export(path, "collect-out", rv._EXPORT_MAX_BYTES)
            lean = getattr(hi, "lean_tarinfo", lambda: tarfile.TarInfo)()
            with tarfile.open(str(path), mode="r:", tarinfo=lean) as tf:
                with getattr(hi, "gc_paused", contextlib.nullcontext)():
                    return len(rv._collect_out_members(rv._read_bounded_members(tf, "collect-out")))
        out = await measured(lambda: asyncio.to_thread(parse_check))
        out["members"] = n
    print(json.dumps(out))

async def main():
    td = tempfile.mkdtemp(prefix="cseries-mem-")
    try:
        await _case(td)
    finally:
        # The child owns its scratch and leaves none behind: a case writes up
        # to ~300 MiB here, and TMPDIR can be a small tmpfs.
        import shutil
        shutil.rmtree(td, ignore_errors=True)

asyncio.run(main())
'''

# (case, stated RSS ceiling MiB, stated stall ceiling ms, must-refuse)
_MEM_CASES = (
    ("collect_pax", 16, 100, True),
    ("fallback_pax", 16, 100, True),
    ("snapshot_pax", 16, 100, True),
    ("session_pax", 16, 100, True),
    ("collect_v7dir", 16, 100, True),     # the two pax bypasses
    ("collect_padsize", 16, 100, True),
    ("collect_negpax", 16, 100, True),
    ("session_lines", 32, 100, False),
    ("status_gzip", 16, 100, True),
    ("stop_json", 16, 100, False),       # _stop_leftovers logs and carries on by design
    ("run_nodes", 96, 100, True),
    ("stream_flood", 32, 100, False),    # elided from the events file, never refused
    ("stream_record", 32, 100, False),   # 64 MiB, not JSON: kept as its first 64 KiB of text
    ("stream_nodes", 96, 100, False),    # the value flood is dropped, the frame kept
    ("stream_blank", 16, 100, False),
    ("json_escapes", 48, 100, False),    # the value count itself stays small
    ("json_values_16mib", 64, 100, False),   # just under the value budget: the GIL-bound parse
    ("json_values_over", 32, 100, True),     # over it: refused by the count
    ("run_big", 128, 100, False),        # blocker 2: a result over any cap is NOT refused
    ("stream_big", 128, 100, False),
    ("stream_real_max", 96, 100, False),
    ("wide_run", 128, 100, False),       # wide text
    ("wide_stream", 128, 100, False),
    ("wide_escaped_stream", 128, 100, False),
    ("wide_mixed_stream", 128, 100, False),   # every kept str 4 bytes a character
    ("status_flood", 32, 100, False),    # status frames bound too
    ("browser_gzip", 48, 100, True),
    ("browser_download", 16, 100, False),   # blocker 3: 64 MiB saved, not refused
    ("inbound_mcp", 16, 100, False),     # the verdict is the 413 the client reads
    ("derived_land", 16, 100, False),
    ("derived_diff", 64, 100, False),
    ("collect_limits", 192, 100, False),
    ("collect_limits_hostile", 192, 100, False),   # every tar limit filled at once
    ("collect_limits_wide", 192, 100, False),      # ...with names stored 4 bytes a character
    ("collect_limits_deep", 192, 100, False),      # the member check's slowest shape
    ("collect_limits_refused", 192, 100, True),    # 200,000 read, the last one refused
)


_RESULT_CASE_NAMES = ("run_big", "stream_big", "stream_real_max", "wide_run", "wide_stream",
                      "wide_escaped_stream", "wide_mixed_stream")


# The timing bounds section 17 states, in one place: a cost in CPU, a stall
# a loop stall (see _CHILD_CLOCKS).
_VIEW_STALL_MS = 100          # opening a finished run's stream view
_POLL_STALL_MS = 20           # one poll of a live viewer caught up on a file
_COLLECT_LIMITS_S = 30        # reading and checking 200,000 members
_STREAM_BLANK_S = 2           # 128 MiB of blank lines
_SESSION_LINES_S = 10         # the session_lines parse

# Every timing limit above was set on the reference machine. CI runs on its
# own machine, whose cores do the same
# Python work about 1.9x slower; so do the workers the Hosts run on (measured with the
# probe below: the reference machine 0.041-0.053 s under CI load, the CI machine 0.085 s,
# a Host worker 0.086-0.091 s). There, run_big/stream_big/stream_real_max/wide_escaped_stream stalled 104-168
# ms against 100. So each timing limit is scaled by how much slower this machine runs the
# probe than the reference machine did: never below 1.0 (a faster machine gets no slack,
# and on the reference machine every limit and check name is unchanged) and never above 2.5 (a machine slower than that
# is not one these limits can speak for). The probe is the work these paths do: a JSON
# round trip, a compression and an interpreter loop, in this thread's CPU seconds.
# It is taken as the fastest of many probes spread over the whole run (every
# _SPEED_PROBE_EVERY_S while the cases run), not five at the start: on a loaded reference
# machine (load 45-48) a probe reads two ways, 0.034-0.038 s or 0.049-0.064 s when the
# core is shared, and a contended spell outlasts 1 s, so a best-of-five at the start read
# x1.00-x1.42 there and stretched every limit by a passing moment. The fastest probe over
# the run is the core itself: the reference machine x1.00, the CI machine idle 0.0845-0.0857 s (x1.88).
_SPEED_PROBE_REF_S = 0.045
_SPEED_PROBE_EVERY_S = 2.0
_SPEED_PROBE_DOC = {"k%d" % i: ["v" * 40, i, {"n": i * 1.5}] for i in range(20000)}


def _speed_probe() -> float:
    import zlib
    t = time.thread_time()          # this thread's own: the pool's threads run beside it
    blob = json.dumps(_SPEED_PROBE_DOC)
    json.loads(blob)
    zlib.compress(blob.encode(), 6)
    s = 0
    for i in range(1_000_000):
        s += i
    return time.thread_time() - t


def core_speed_factor(probes: list) -> float:
    return min(2.5, max(1.0, min(probes) / _SPEED_PROBE_REF_S))


def _mem_child(case: str, cpu_budget_s: float = 120.0) -> dict:
    return _run_child(_MEM_CHILD, case, cpu_budget_s)


# The cases run side by side: each reads only its own process's clocks and
# memory, so they do not disturb each other, and on a busy machine the gate's
# length follows its CPU, not the sum of its cases' waits for one.
MEM_WORKERS = max(1, min(4, (os.cpu_count() or 2) // 2))


MEASURED: dict = {}


def measured_checks() -> None:
    print("== 17. MEASURED: each harness-to-Host path under an adversarial payload ==")
    from concurrent.futures import ThreadPoolExecutor, wait
    probes = [_speed_probe() for _ in range(5)]
    with ThreadPoolExecutor(MEM_WORKERS) as pool:
        futs = [pool.submit(_mem_child, c[0]) for c in _MEM_CASES]
        while wait(futs, timeout=_SPEED_PROBE_EVERY_S).not_done:
            probes.append(_speed_probe())
        results = [fu.result() for fu in futs]
    probes += [_speed_probe() for _ in range(5)]
    f = core_speed_factor(probes)
    print(f"   timing limits x{f:.2f}: this machine's core against the reference machine's "
          f"(core_speed_factor, fastest of {len(probes)} probes over the run)")
    view_ms, poll_ms = round(_VIEW_STALL_MS * f), round(_POLL_STALL_MS * f)
    for (case, rss_cap, stall_cap, must_refuse), r in zip(_MEM_CASES, results):
        MEASURED[case] = r
        stall_cap = round(stall_cap * f)
        ok_bounds = (not r.get("timeout") and "crash" not in r
                     and r.get("rss_mib", 1e9) <= rss_cap and r.get("stall_ms", 1e9) <= stall_cap)
        check(f"{case}: peak RSS growth <= {rss_cap} MiB and loop stall <= {stall_cap} ms",
              ok_bounds, r)
        if must_refuse:
            check(f"{case}: refused", "refused" in str(r.get("err")), r)
        if case == "stream_flood":
            check("stream_flood: the events file stays small (< 16 MiB)",
                  r.get("events_mib", 1e9) < 16, r)
            check("stream_flood: not refused (frames past the budget are elided)",
                  r.get("err") is None and r.get("elided") is True, r)
        if case in ("stream_record", "stream_nodes", "stream_blank"):
            check(f"{case}: not refused", r.get("err") is None, r)
        if case.startswith("collect_limits"):
            # the whole tar phase (pre-scan, parse, member check) runs in a
            # worker thread, so it never blocks the loop; its CPU time is
            # still bounded (measured on the reference machine: 6-9 s; the slowest shape is
            # collect_limits_deep)
            want = {"collect_limits": "200000 read, 200000 kept",
                    "collect_limits_hostile": "200000 read, 0 kept",      # every one dropped
                    "collect_limits_wide": "200000 read, 0 kept",
                    "collect_limits_deep": "200000 read, 199999 kept"}.get(case)
            check(f"{case}: all 200,000 members read and checked in under {_COLLECT_LIMITS_S * f:g} s of CPU"
                  + (" (and refused)" if must_refuse else f" ({want})"),
                  (r.get("err") is None and r.get("val") == want if not must_refuse
                   else "refused" in str(r.get("err")))
                  and r.get("elapsed_s", 1e9) < _COLLECT_LIMITS_S * f, r)
        if case == "browser_download":
            check("browser_download: the user's 64 MiB download is saved whole, not refused",
                  r.get("err") is None and r.get("saved") == 64 * 1024 * 1024
                  and r.get("part_left") is False, r)
        if case == "stream_blank":
            check(f"stream_blank: 128 MiB of blank lines cost under {_STREAM_BLANK_S * f:g} s of CPU",
                  r.get("elapsed_s", 99) < _STREAM_BLANK_S * f, r)
        if case in _RESULT_CASE_NAMES:
            check(f"{case}: the finished run is read, ok, and not refused",
                  r.get("err") is None and r.get("ok") is True
                  and r.get("usage") == {"input": 5, "output": 7}, r)
            kept_ok = True
            for fld in ("tr", "diff"):
                src, keep, got = r.get(f"{fld}_src", 0), r.get(f"{fld}_keep", 0), r.get(f"{fld}_bytes", -1)
                if src > keep:     # cut: the kept part is the limit in BYTES (a character's worth under), plus a <= 80-byte marker
                    kept_ok &= r.get(f"{fld}_marker") is True and keep - 4 < got <= keep + 80
                else:              # whole: nothing cut, no marker
                    kept_ok &= r.get(f"{fld}_marker") is False and got == src
            check(f"{case}: transcript and diff kept to their limits in UTF-8 BYTES "
                  "(cut ones marked, whole ones untouched)", kept_ok, r)
            any_cut = r.get("tr_src", 0) > r.get("tr_keep", 0) or r.get("diff_src", 0) > r.get("diff_keep", 0)
            check(f"{case}: result_cut is set exactly when something was cut",
                  r.get("result_cut") is any_cut, r)
            if case != "run_big" and case != "wide_run":
                check(f"{case}: the events file holds no copy of the transcript or diff "
                      "(its largest line under 64 KiB), and the run terminal says what was cut",
                      r.get("events_max_line_kib", 1e9) < 64 and r.get("cut_note_frame") is any_cut, r)
            if "view" in r:
                check(f"{case}: opening the run's stream view afterwards: loop stall <= {view_ms:.0f} ms, "
                      "peak RSS growth <= 32 MiB",
                      r["view"].get("stall_ms", 1e9) <= view_ms
                      and r["view"].get("rss_mib", 1e9) <= 32, r["view"])
        if case == "status_flood":
            check("status_flood: 300,000 status frames: the run's result is read, not refused",
                  r.get("err") is None, r)
            check("status_flood: the events file stays under its bound (<= 130 MiB) and ends on the "
                  "LATEST status, then the terminal frame",
                  r.get("events_mib", 1e9) <= 130 and r.get("last_two") == ["working 299999", "complete"], r)
            check(f"status_flood: a live viewer caught up on that file: per-poll loop stall <= {poll_ms:.0f} ms",
                  (r.get("poll") or {}).get("worst_stall_ms", 1e9) <= poll_ms, r.get("poll"))
        if case == "inbound_mcp":
            check("inbound_mcp: the client is answered 413", str(r.get("val")) == "413", r)
        if case == "session_lines":
            check(f"session_lines: collected, and the parse finished (< {_SESSION_LINES_S * f:g} s of CPU)",
                  r.get("err") is None and r.get("elapsed_s", 1e9) < _SESSION_LINES_S * f, r)
        if case == "derived_land":
            check("derived_land: the both-changed 256 MiB file is a conflict and the copy lands",
                  str(r.get("val")).startswith("conflicts=1 copied=1"), r)
            # Size alone is no longer a
            # conflict; this file is one because both sides are binary, found
            # by a chunked scan (the RSS bound above holds it to that).
            check("derived_land: ...a conflict because it is binary on both sides "
                  "(both_changed), not because it is large",
                  str(r.get("val")).endswith("kinds=both_changed"), r)
        if case == "derived_diff":
            check("derived_diff: the diff kept is cut at 16 MiB (plus its marker)",
                  isinstance(r.get("val"), int) and 16 << 20 <= r["val"] <= (16 << 20) + 200, r)
        if case in ("collect_pax", "fallback_pax", "snapshot_pax", "collect_v7dir",
                    "collect_padsize", "collect_negpax"):
            check(f"{case}: the worktree is untouched", r.get("worktree_kept") is True, r)
    print("   measured (stall_ms a loop stall, elapsed_s CPU; stall_cpu_ms, stall_wall_ms and wall_s "
          "by one clock alone, not checked):",
          json.dumps({k: {x: v.get(x) for x in ("rss_mib", "stall_ms", "elapsed_s", "stall_cpu_ms",
                                                "stall_wall_ms", "wall_s")}
                      for k, v in MEASURED.items()}))


# ── 18. sparse members, cancel safety, and the derived reads ────────────────

def _gnu_sparse_tar(name: str, data: bytes) -> bytes:
    """A tar whose one member is an old-GNU sparse file ('S'): tarfile.add
    never writes one. Built by hand (tarfile cannot write sparse), with one
    map entry covering the data and a real size twice the data's."""
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    hdr = bytearray(ti.tobuf(format=tarfile.GNU_FORMAT))
    hdr[156:157] = b"S"
    hdr[386:398] = b"%011o\0" % 0                       # sparse[0].offset
    hdr[398:410] = b"%011o\0" % len(data)               # sparse[0].numbytes
    hdr[482:483] = b"\0"                                # isextended
    hdr[483:495] = b"%011o\0" % (2 * len(data))         # realsize
    hdr[148:156] = b" " * 8
    hdr[148:156] = b"%06o\0 " % sum(hdr)
    pad = b"\0" * ((512 - len(data) % 512) % 512)
    return bytes(hdr) + data + pad + b"\0" * 1024


def sparse_cancel_derived_checks() -> None:
    print("== 18. sparse members are refused at every layer ==")
    from server import harness_input as hi
    from server.routes import review as rv
    from server.routes import compute as cm
    sp = _gnu_sparse_tar("agents/main/wire.jsonl",
                         b'{"type":"usage.record","usage":{"input":5,"output":2}}\n')
    try:
        hi.prescan_tar(io.BytesIO(sp), "t", max_members=10, max_data=1 << 20)
        got = "accepted"
    except Exception as exc:  # noqa: BLE001
        got = repr(exc)
    check("the raw pre-scan refuses a GNU sparse ('S') member", "'S'" in got and "refused" in got, got)
    ti = tarfile.TarInfo("s")
    ti.pax_headers = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0"}
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w", format=tarfile.PAX_FORMAT) as tf:
        tf.addfile(ti)
    try:
        hi.prescan_tar(io.BytesIO(b.getvalue()), "t", max_members=10, max_data=1 << 20)
        got = "accepted"
    except Exception as exc:  # noqa: BLE001
        got = repr(exc)
    check("...and a pax GNU.sparse member", "GNU.sparse" in got, got)
    try:
        with tarfile.open(fileobj=io.BytesIO(sp), mode="r:") as tf:
            rv._read_bounded_members(tf, "t")
        got = "accepted"
    except Exception as exc:  # noqa: BLE001
        got = repr(exc)
    check("_read_bounded_members refuses a sparse member (the tarfile-level guard)",
          "sparse" in got, got)
    check("the session usage reader skips a sparse member rather than reading it",
          cm.usage_frames_from_session_tar(sp, "kimi") == [])

    print("== 18b. collect-out is cancel-safe, and one collect runs per worktree ==")
    import threading
    import aiohttp
    from aiohttp import web
    lock_fn = getattr(rv, "_worktree_lock", None)
    check("review has a per-worktree collect lock", lock_fn is not None)
    with tempfile.TemporaryDirectory() as td:
        wt = _host_worktree(td, "cancel")
        body = _raw_tar([("dir", "."), *[("file", f"f{i}.txt", b"x" * 100) for i in range(300)]])
        started = threading.Event()
        orig = rv._collect_out_apply

        def slow_apply(tar_path, worktree):
            started.set()
            time.sleep(0.4)                 # the worker is mid-collect when the caller is cancelled
            return orig(tar_path, worktree)

        async def go():
            app = web.Application()
            app.router.add_get("/workspace/export", lambda _r: web.Response(body=body))
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)      # loopback: only this gate dials it
            await site.start()
            base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
            try:
                async with aiohttp.ClientSession() as http:
                    task = asyncio.create_task(rv._collect_out(http, base, "d", wt))
                    while not started.is_set():
                        await asyncio.sleep(0.01)
                    task.cancel()
                    try:
                        await task
                        return "not cancelled"
                    except asyncio.CancelledError:
                        return "cancelled"
            finally:
                await runner.cleanup()
        rv._collect_out_apply = slow_apply
        try:
            outcome = asyncio.run(go())
        finally:
            rv._collect_out_apply = orig
        check("the caller is cancelled while the worker is mid-collect", outcome == "cancelled", outcome)
        finished = False
        if lock_fn is not None:
            lk = lock_fn(wt)
            finished = lk.acquire(timeout=10)
            if finished:
                lk.release()
        else:
            time.sleep(1.0)
        check("...the worker still completes (it holds the worktree lock until done)", finished)
        check("...and cleans up after itself: no tar, no staging dir left in the worktree",
              not [p.name for p in wt.iterdir() if p.name.startswith(".cseries-collect-")],
              [p.name for p in wt.iterdir()])
        check("...leaving the worktree whole: the collected files, the seed replaced",
              (wt / "f299.txt").is_file() and not (wt / "src").exists())
        if lock_fn is not None:
            lk = lock_fn(wt)
            lk.acquire()
            done = threading.Event()
            tp = wt / ".cseries-collect-held.tar"
            tp.write_bytes(_raw_tar([("dir", "."), ("file", "g.txt", b"g")]))
            th = threading.Thread(target=lambda: (rv._collect_out_apply(tp, wt), done.set()))
            th.start()
            waited = not done.wait(0.3)
            lk.release()
            th.join(10)
            check("a second collect into the same worktree waits for the first (no interleave)",
                  waited and done.is_set() and (wt / "g.txt").is_file())

    # The SPILL was outside the lock, so an
    # earlier collect's worker, clearing the worktree, could delete a later
    # collect's spilled tar under it ("collect-out refused: [Errno 2]").
    with tempfile.TemporaryDirectory() as td:
        wt = _host_worktree(td, "spill")
        body1 = _raw_tar([("dir", "."), ("file", "first.txt", b"1")])
        body2 = _raw_tar([("dir", "."), ("file", "second.txt", b"2")])
        orig = rv._collect_out_apply
        first_in_worker = threading.Event()
        order: list = []

        def slow_first(tar_path, worktree):
            if not first_in_worker.is_set():
                first_in_worker.set()
                time.sleep(0.5)             # the first worker is mid-collect...
                order.append("first worker done")
            return orig(tar_path, worktree)

        async def go2():
            hits = {"n": 0}

            async def export(_r):
                hits["n"] += 1
                if hits["n"] == 2:
                    order.append("second spill started")
                return web.Response(body=body1 if hits["n"] == 1 else body2)
            app = web.Application()
            app.router.add_get("/workspace/export", export)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)      # loopback: only this gate dials it
            await site.start()
            base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
            try:
                async with aiohttp.ClientSession() as http:
                    t1 = asyncio.create_task(rv._collect_out(http, base, "d", wt))
                    while not first_in_worker.is_set():
                        await asyncio.sleep(0.01)
                    t2 = asyncio.create_task(rv._collect_out(http, base, "d", wt))
                    res = await asyncio.gather(t1, t2, return_exceptions=True)
                    return [None if r is None else repr(r)[:160] for r in res]
            finally:
                await runner.cleanup()
        rv._collect_out_apply = slow_first
        try:
            res = asyncio.run(go2())
        finally:
            rv._collect_out_apply = orig
        check("two collects into one worktree: the second's spill waits for the first's worker "
              "(item 11)", order == ["first worker done", "second spill started"], order)
        check("...and both succeed, the second's files in place", res == [None, None]
              and (wt / "second.txt").is_file() and not (wt / "first.txt").exists(), res)

    print("== 18c. the Host's later reads of harness-written files are chunked ==")
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "f.bin"
        p.write_bytes(b"hello\x00world" * 1000)
        check("_blob_sha_file == _blob_sha (SHA-1 and SHA-256 repos)",
              rv._blob_sha_file(p, 40) == rv._blob_sha(p.read_bytes(), 40)
              and rv._blob_sha_file(p, 64) == rv._blob_sha(p.read_bytes(), 64))
        wt = _host_worktree(td, "merge")
        proj = pathlib.Path(td) / "projects"
        saved = rv.PROJECTS_ROOT
        rv.PROJECTS_ROOT = proj
        try:
            (proj / "5" / "src").mkdir(parents=True)
            base_txt = "".join(f"line {i}\n" for i in range(200))
            base_sha = rv._blob_sha(base_txt.encode(), 40)
            (wt / "src" / "m.txt").write_text(base_txt.replace("line 10\n", "line 10 run\n"))
            (proj / "5" / "src" / "m.txt").write_text(base_txt.replace("line 150\n", "line 150 proj\n"))
            big = "x" * 100 + "\n"
            (wt / "src" / "big.txt").write_text(big * (90_000) + "run\n")
            (proj / "5" / "src" / "big.txt").write_text("proj\n" + big * (90_000))
            orig_bf = rv._blob_to_file

            def _fake_blob(_w, sha, dest):
                dest.write_text(base_txt if sha == base_sha else big * 90_000)
                return True
            rv._blob_to_file = _fake_blob
            stage = pathlib.Path(td) / "stage"
            stage.mkdir()
            try:
                w, m, c = rv._integrate_with_project(5, wt, ["src/m.txt", "src/big.txt"],
                                                     {"src/m.txt": base_sha, "src/big.txt": "1" * 40},
                                                     None, stage)
            finally:
                rv._blob_to_file = orig_bf
            check("a small both-changed text file is still merged", "src/m.txt" in m, (w, list(m), c))
            # A both-changed file over 8 MiB is
            # merged on disk, no longer a too_large_to_merge conflict.
            got = pathlib.Path(m["src/big.txt"]) if "src/big.txt" in m else None
            check("a both-changed file over 8 MiB is merged on disk (a file, not bytes held in memory)",
                  not c and got is not None and got.parent == stage
                  and got.stat().st_size == len(big) * 90_000 + 9, (c, got))
        finally:
            rv.PROJECTS_ROOT = saved


# ── 19. the enumeration, pinned: every Host read of harness input is bounded ─
#
# Harness-facing code is found by function, not by a regex on `()`: an
# awaited .json(...)/.text(...)/.read(...) on an HTTP object, WITH ANY
# ARGUMENTS, is refused anywhere in review.py, and in the other harness-facing
# modules outside a short allowlist whose reasons are written here.
_HARNESS_FACING = (
    "server/routes/review.py", "server/browser_agents.py", "server/vault/authz.py",
    "server/applications/browser_profile.py", "brain/mastercode/runtime/app_verify.py",
    "server/routes/mcp.py", "server/applications/proxy.py",
    "server/routes/compute.py", "server/applications/harness_backend.py",
    "server/peer/app_lane.py", "server/routes/browser_forms.py",
)
_NOT_HARNESS = {
    ("server/vault/authz.py", "_do_http_call"): "the vault broker's pinned call to an external API",
    ("server/vault/authz.py", "_path_secret_request"): "the same broker call in path-secret mode (a "
        "messaging platform's API host, from the connector binding); content.read(_MAX_BODY + 1), bounded",
    ("server/routes/mcp.py", "_tool_read_url"): "url_reader.read, bounded by _MAX_HTML_BYTES",
    ("server/peer/app_lane.py", "_hand_to_live_process"): "content.read(MAX_RESPONSE_BYTES + 1), bounded",
}
# Kernel handlers a pod can reach (server/middleware/localhost_only.py
# allowlists), which must read a body only through server/harness_input.py.
_POD_REACHABLE_HANDLERS = (
    ("server/routes/mcp.py", "mcp_endpoint"),
    ("server/routes/applications.py", "app_runtime_log"),
    ("server/routes/applications.py", "app_source"),
)


def _awaited_reads(tree):
    import ast
    for node in ast.walk(tree):
        if (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in ("json", "text", "read", "body", "form")):
            yield node


def _enclosing(tree, lineno):
    import ast
    best = None
    for f in ast.walk(tree):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.lineno <= lineno <= f.end_lineno:
            if best is None or f.lineno > best.lineno:
                best = f
    return best.name if best else "<module>"


def enumeration_checks() -> None:
    import ast
    print("== 19. every Host read of harness input goes through the bounded readers ==")
    bad = []
    for rel in _HARNESS_FACING:
        tree = ast.parse((ROOT / rel).read_text())
        for node in _awaited_reads(tree):
            fn = _enclosing(tree, node.lineno)
            target = ast.unparse(node.value.func.value)
            if target in ("request",):
                continue              # inbound requests: the next check, per handler
            if (rel, fn) in _NOT_HARNESS:
                continue
            bad.append(f"{rel}:{node.lineno} {fn}: await {ast.unparse(node.value)[:60]}")
    check("no harness response is read whole (json/text/read with ANY arguments)", not bad, bad)
    # The same, in every FORM a read can take: passed to
    # an asyncio wrapper (asyncio.wait_for(r.json(), 5), ensure_future, ...)
    # or kept as a deferred future and awaited later, with r.text(arg) and
    # r.content.read(-1) counted as whole. Six such reads once existed; here
    # there are none outside the allowlist.
    bad = []
    for rel in _HARNESS_FACING:
        for fn, lines in _whole_body_reads((ROOT / rel).read_text()).items():
            if (rel, fn) not in _NOT_HARNESS:
                bad.append(f"{rel} {fn}: lines {lines}")
    check("...nor through an asyncio wrapper or a deferred future",
          not bad, bad)
    for label, snippet, want_counts in _WHOLE_READ_PROBES:
        check(f"(the detector) {label}", {f: len(v) for f, v in _whole_body_reads(snippet).items()}
              == want_counts, _whole_body_reads(snippet))
    bad = []
    for rel, name in _POD_REACHABLE_HANDLERS:
        tree = ast.parse((ROOT / rel).read_text())
        fns = [f for f in ast.walk(tree)
               if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == name]
        if not fns:
            bad.append(f"{rel}: {name} not found")
            continue
        for node in _awaited_reads(fns[0]):
            if ast.unparse(node.value.func.value) == "request":
                bad.append(f"{rel}:{node.lineno} {name}: await {ast.unparse(node.value)[:50]}")
    check("pod-reachable kernel handlers read bodies only through harness_input", not bad, bad)
    src = (ROOT / "server/applications/proxy.py").read_text()
    check("the proxy caps a verify-cap request's body (harness_input.read_request)",
          "async def _request_body" in src and "_hin.read_request(request, _VERIFY_BODY_MAX" in src
          and src.count("await request.body()") == 1)
    cm_tree = ast.parse((ROOT / "server/routes/compute.py").read_text())
    fn = [f for f in ast.walk(cm_tree) if isinstance(f, ast.FunctionDef)
          and f.name == "usage_frames_from_session_tar"]
    reads = [ast.unparse(n) for n in ast.walk(fn[0]) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr in ("read", "splitlines")] if fn else ["missing"]
    check("the session usage reader never reads a member whole (no .read(), no splitlines)",
          not reads, reads)
    rv_src = (ROOT / "server/routes/review.py").read_text()
    # The land's merge reads its
    # sides from disk too, so no exception remains: neither the run's file
    # nor the project's nor a merge result is read whole by these.
    t = ast.parse(rv_src)
    for name in ("_snapshot_to_project", "_integrate_with_project", "propose_commit",
                 "_merge3", "_git_merge_file"):
        f = [x for x in ast.walk(t) if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name == name]
        merge_fn = name in ("_integrate_with_project", "_merge3", "_git_merge_file")
        whole = [ast.unparse(n) for n in ast.walk(f[0]) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "read_bytes"
                 and (merge_fn or "src" in ast.unparse(n.func.value))] if f else ["missing"]
        check(f"{name}: a harness file is not read whole", not whole, whole)


# ── 20. the raw pre-scan reads a tar EXACTLY as tarfile does ───────────────
#
# The pre-scan exists so tarfile never parses what would cost unbounded memory.
# It only protects anything if it walks the SAME headers tarfile will: a header
# the scan skips as "data" but tarfile parses (a type-NUL member named like a
# directory; a pax record in a block's padding that moves the next header) is a
# header nothing checked. So the check is structural: for every tar the scan
# ACCEPTS, real exports and thousands of fuzzed variants, the member layout it
# reports equals tarfile's, and tarfile parses the whole tar without an error.

def _parity_tree(d: pathlib.Path, gnu: bool = False) -> None:
    """A worktree with every shape a real export carries: sizes at block
    edges, names past 100 and 255 bytes, non-ASCII and non-UTF-8 names, long
    link targets, a hard link, a dangling link, a FIFO, empty and deep dirs,
    pre-1970 and far-future mtimes."""
    (d / "src/pkg").mkdir(parents=True)
    for i, n in enumerate((0, 1, 511, 512, 513, 1024, 5000)):
        (d / f"src/f{i}.txt").write_bytes(b"x" * n)
    (d / "src" / ("a" * 120)).mkdir()
    (d / "src" / ("a" * 120) / ("b" * 150 + ".txt")).write_text("deep\n")
    (d / "src/pkg/néü.txt").write_text("unicode\n")
    try:
        os.mkdir(os.fsencode(str(d / "src")) + b"/raw\xff\xfe")
    except OSError:
        pass
    (d / "empty/nested").mkdir(parents=True)
    os.symlink("f1.txt", d / "src/link")
    os.symlink("../" * 3 + "c" * 110, d / "src/longlink")
    os.symlink("missing", d / "src/dangling")
    os.link(d / "src/f2.txt", d / "src/hard")
    os.mkfifo(d / "src/fifo")
    if not gnu:        # GNU format writes a pre-1970 mtime in base-256, which the scan refuses
        q = d / "src/old.txt"; q.write_text("1969\n"); os.utime(q, (-864000, -864000))
    q = d / "src/future.txt"; q.write_text("2200\n"); os.utime(q, (7258118400.5, 7258118400.5))
    x = d / "d0"
    for k in range(40):
        x = x / f"level{k}"
    x.mkdir(parents=True); (x / "leaf").write_text("leaf\n")


def _tarfile_layout(body: bytes) -> tuple:
    """(offset, offset_data, type, size) per member, and the name bytes,
    exactly as tarfile reads them. Raises what tarfile raises."""
    out, names = [], 0
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tf:
        while True:
            ti = tf.next()
            if ti is None:
                return out, names
            out.append((ti.offset, ti.offset_data, ti.type, ti.size))
            names += _held(ti.name) + _held(ti.linkname)


def _held(name: str) -> int:
    """A name at the larger of its bytes and what Python holds for its
    characters (1, 2 or 4 bytes each, set by the widest): the pre-scan's
    name budget must never count less than either."""
    n = len(os.fsencode(name))
    if name.isascii():
        return n
    top = ord(max(name))
    return max(n, len(name) * (1 if top < 0x100 else 2 if top < 0x10000 else 4))


class _ReadRecorder(io.BytesIO):
    """For a pre-scan with no `layout` argument (the head before this
    check): the offsets at which it read a 512-byte header."""
    def __init__(self, b):
        super().__init__(b)
        self.headers = []

    def read(self, n=-1):
        if n == 512:
            self.headers.append(self.tell())
        return super().read(n)


def _tarfile_headers(body: bytes) -> list:
    offs = []
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tf:
        while True:
            ti = tf.next()
            if ti is None:
                return offs
            o = ti.offset
            while o < ti.offset_data - 512:
                offs.append(o)
                sz = int(body[o + 124:o + 136].split(b"\0")[0].strip() or b"0", 8)
                o += 512 + (sz + 511) // 512 * 512
            offs.append(ti.offset_data - 512)


def _parity_verdict(hi, body: bytes):
    """None when the scan refused; else a string naming the disagreement
    ('' when it agrees with tarfile)."""
    kw = dict(max_members=10_000, max_data=64 << 20, max_names=64 << 20)
    has_layout = "layout" in hi.prescan_tar.__code__.co_varnames
    lay, rec = [], _ReadRecorder(body)
    try:
        got = (hi.prescan_tar(io.BytesIO(body), "parity", layout=lay, **kw) if has_layout
               else hi.prescan_tar(rec, "parity", **kw))
    except hi.HarnessInputRefused:
        return None
    except Exception as exc:  # noqa: BLE001 - the scan must refuse, never crash
        return f"the scan raised {exc!r}"[:160]
    try:
        want, names = _tarfile_layout(body)
        if not has_layout:
            heads = _tarfile_headers(body)
    except Exception as exc:  # noqa: BLE001
        return f"tarfile raised {exc!r} on a tar the scan accepted"[:160]
    if has_layout:
        if lay != want:
            return "member layout differs"
        if got["names"] < names:
            return f"names undercounted ({got['names']} < {names})"
        return ""
    scanned = [o for o in rec.headers if o + 512 <= len(body) and body[o:o + 512].count(0) != 512]
    return "" if scanned == heads else "header offsets differ"


_PARITY_NUMBERS = [b"0", b"1", b"777", b"1000", b"1001", b"-1", b"-777", b"+7", b"1_0", b"0o7",
                   b" 7", b"7 ", b"\x807", b"\xff\xff", b"99", b"77777777777", b"", b"\t7"]
_PARITY_TYPES = list(b"0\x001234567xLKgXSVMND") + [0x41, 0x7f, 0xff]


def _fix_checksum(b: bytearray, off: int) -> None:
    h = bytearray(b[off:off + 512]); h[148:156] = b" " * 8
    b[off + 148:off + 156] = b"%06o\0 " % sum(h)


def _parity_mutate(rng, base: bytes, hdr_offs: list, xblocks: list) -> bytes:
    b = bytearray(base)
    for _ in range(rng.randint(1, 3)):
        op = rng.randrange(9)
        live = [o for o in hdr_offs if o + 512 <= len(b)]
        if not live:
            break
        off = rng.choice(live)
        if op == 0:                                   # random header bytes
            for _ in range(rng.randint(1, 4)):
                b[off + rng.randrange(512)] = rng.randrange(256)
            if rng.random() < 0.7:
                _fix_checksum(b, off)
        elif op == 1:                                 # the type byte
            b[off + 156] = rng.choice(_PARITY_TYPES); _fix_checksum(b, off)
        elif op == 2:                                 # the size field, every number form
            b[off + 124:off + 136] = (rng.choice(_PARITY_NUMBERS) + b"\0" * 12)[:12]
            _fix_checksum(b, off)
        elif op == 3 and xblocks:                     # a record in a pax block's padding
            xo, xsize = rng.choice(xblocks)
            body = rng.choice([b"size=512\n", b"size=0\n", b"path=" + b"p" * rng.randint(1, 300) + b"\n",
                               b"a=b\n", b"GNU.sparse.size=9\n", b"mtime=1\n", b"linkpath=zz\n"])
            n = len(body) + 2
            while len(b"%d " % n) + len(body) != n:
                n += 1
            recb = b"%d " % n + body
            start, room = xo + 512 + xsize, (512 - xsize % 512) % 512
            if room >= len(recb) and start + len(recb) <= len(b):
                b[start:start + len(recb)] = recb
        elif op == 4:                                 # v7 directory: type NUL, name ending '/'
            nm = bytes(b[off:off + 100]).split(b"\0", 1)[0][:98] + b"/"
            b[off:off + 100] = nm + b"\0" * (100 - len(nm)); b[off + 156] = 0
            _fix_checksum(b, off)
        elif op == 5:                                 # truncate, or trailing garbage
            if rng.random() < 0.5:
                del b[rng.randrange(1, len(b)):]
            else:
                b += bytes(rng.randrange(256) for _ in range(rng.randint(1, 1500)))
        elif op == 6:                                 # the other number fields
            a, e = rng.choice(((100, 108), (108, 116), (116, 124), (136, 148), (329, 337), (337, 345)))
            b[off + a:off + e] = (rng.choice(_PARITY_NUMBERS) + b"\0" * 12)[:e - a]
            _fix_checksum(b, off)
        elif op == 7:                                 # a duplicated header (chains, strays)
            blk = bytes(b[off:off + 512]); at = rng.choice(live)
            b[at:at] = blk
        elif op == 8:                                 # a zeroed block (early end, dangling ext)
            b[off:off + 512] = b"\0" * 512
    return bytes(b)


def _parity_shape(body: bytes) -> tuple:
    heads, xblocks = _tarfile_headers(body), []
    for o in heads:
        if body[o + 156:o + 157] == b"x":
            xblocks.append((o, int(body[o + 124:o + 136].split(b"\0")[0].strip() or b"0", 8)))
    return heads, xblocks


def _hand_header(name: str, typ: bytes, size: int, size_field: bytes | None = None) -> bytes:
    ti = tarfile.TarInfo(name); ti.type = typ; ti.size = size
    b = bytearray(ti.tobuf(format=tarfile.USTAR_FORMAT))
    if size_field is not None:
        b[124:136] = (size_field + b"\0" * 12)[:12]
        _fix_checksum(b, 0)
    return bytes(b)


def _pad512(n: int) -> bytes:
    return b"\0" * ((512 - n % 512) % 512)


def _pax_block(n: int) -> bytes:
    rec = b"12 xx=abcde\n"
    n -= n % len(rec)
    return _hand_header("././@PaxHeader", tarfile.XHDTYPE, n) + rec * (n // len(rec)) + _pad512(n)


def _v7dir_bomb(n: int) -> bytes:
    """The first bypass: a type-NUL member named `d/` whose "data"
    is a pax bomb. tarfile reads `d/` as a DIRECTORY (no data) and parses the
    bomb as headers; the old scan skipped it as file data."""
    hidden = _pax_block(n) + _hand_header("x", tarfile.REGTYPE, 0)
    return _hand_header("d/", tarfile.AREGTYPE, len(hidden)) + hidden + b"\0" * 1024


def _padsize_bomb(n: int) -> bytes:
    """The second bypass: a pax `size` record in the PADDING of a
    pax block. tarfile parses the padding and skips 512 bytes the old scan
    read as the next header, landing on a hidden pax bomb."""
    rec1 = b"13 path=file\n"
    sz = b"12 size=512\n"
    block = rec1 + sz + b"\0" * (512 - len(rec1) - len(sz))
    hidden = _pax_block(n) + _hand_header("x", tarfile.REGTYPE, 0)
    return (_hand_header("././@PaxHeader", tarfile.XHDTYPE, len(rec1)) + block
            + _hand_header("f", tarfile.REGTYPE, 0)
            + _hand_header("g", tarfile.REGTYPE, len(hidden)) + hidden + b"\0" * 1024)


_NEG_LOOP_CHILD = r"""
import io, sys, tarfile, time
sys.path.insert(0, sys.argv[1])
from server import harness_input as hi
ti = tarfile.TarInfo("f"); ti.size = 0
h = bytearray(ti.tobuf(format=tarfile.USTAR_FORMAT))
h[124:136] = b"-1000\0" + b"\0" * 6            # octal -512: one block BACK
h[148:156] = b" " * 8; h[148:156] = b"%06o\0 " % sum(h)
try:
    hi.prescan_tar(io.BytesIO(bytes(h) + b"\0" * 1024), "neg", max_members=10**9, max_data=1 << 30)
    print("accepted")
except hi.HarnessInputRefused as e:
    print("refused")
"""


def prescan_parity_checks() -> None:
    import random
    from server import harness_input as hi
    print("== 20. the raw pre-scan reads every tar exactly as tarfile does ==")
    sc = _load_module({"CSERIES_SIDECAR_ROLE": "harness"})
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        _parity_tree(tdp / "ws" / "proj")
        _parity_tree(tdp / "gnu", gnu=True)
        sc.WORKSPACE_ROOT = str(tdp / "ws")
        real = sc._workspace_export(_Req("dir=proj"))
        check("the sidecar's own /workspace/export built a tar", isinstance(real, bytes), str(real)[:80])
        sc.SESSION_EXPORT_DIRS = [str(tdp / "ws" / "proj" / "src"), str(tdp / "ws" / "proj" / "d0")]
        sess = sc._session_export(_Req(""))
        check("the sidecar's own /session/export built a tar", isinstance(sess, bytes), str(sess)[:80])
        gbuf = io.BytesIO()
        with tarfile.open(fileobj=gbuf, mode="w", format=tarfile.GNU_FORMAT) as tf:
            tf.add(str(tdp / "gnu"), arcname=".")            # GNU 'L'/'K' long-name records
        corpus = {"workspace export": real, "session export": sess, "GNU-format export": gbuf.getvalue()}
        for label, body in corpus.items():
            if isinstance(body, bytes):
                v = _parity_verdict(hi, body)
                check(f"{label}: accepted, and read exactly as tarfile reads it", v == "", v)
        bases = [b for b in corpus.values() if isinstance(b, bytes)]
        shapes = [_parity_shape(b) for b in bases]
        rng = random.Random(761)
        n_fuzz, accepted, refused, bad = 2500, 0, 0, []
        t0 = time.perf_counter()
        for i in range(n_fuzz):
            k = i % len(bases)
            v = _parity_verdict(hi, _parity_mutate(rng, bases[k], *shapes[k]))
            if v is None:
                refused += 1
            else:
                accepted += 1
                if v:
                    bad.append((i, v))
        el = time.perf_counter() - t0
        check(f"fuzz: every one of {n_fuzz} variants the scan accepts is read exactly as "
              "tarfile reads it", not bad, {"accepted": accepted, "mismatches": len(bad),
                                             "first": bad[:5]})
        check("fuzz: not vacuous (hundreds accepted, hundreds refused)",
              accepted >= 300 and refused >= 300, {"accepted": accepted, "refused": refused})
        print(f"   fuzz: {n_fuzz} variants, {accepted} accepted, {refused} refused, {el:.1f} s")

    directed = {
        "a type-NUL member named like a directory, hiding a pax bomb (review B1)": _v7dir_bomb(1 << 20),
        "a pax `size` record in a pax block's padding (review B1)": _padsize_bomb(1 << 20),
        "a pax header of size -1 (review item 4)": _hand_header("././@PaxHeader", tarfile.XHDTYPE, 0, b"-0000000001")
                                                  + b"12 xx=abcde\n" * 1000,
        "a size written '+7'": _hand_header("f", tarfile.REGTYPE, 0, b"+7") + b"\0" * 1536,
        "a size written '1_0'": _hand_header("f", tarfile.REGTYPE, 0, b"1_0") + b"\0" * 1536,
        "a size written '0o7'": _hand_header("f", tarfile.REGTYPE, 0, b"0o7") + b"\0" * 1536,
        "a base-256 size": _hand_header("f", tarfile.REGTYPE, 0, b"\x80" + b"\0" * 10 + b"\x07") + b"\0" * 1536,
        "two pax headers chained before one member": _pax_block(24) + _pax_block(24)
                                                    + _hand_header("f", tarfile.REGTYPE, 0) + b"\0" * 1024,
        "a pax header with no member after it": _pax_block(24) + b"\0" * 1024,
        "an unknown pax key": _hand_header("././@PaxHeader", tarfile.XHDTYPE, 8) + b"8 zz=y\n\0"
                             + _pad512(8) + _hand_header("f", tarfile.REGTYPE, 0) + b"\0" * 1024,
        "member data that runs past the end of the file": _hand_header("f", tarfile.REGTYPE, 4096) + b"x" * 600,
        "an empty body": b"",
    }
    for label, body in directed.items():
        v = _parity_verdict(hi, body)
        check(f"refused: {label}", v is None, v)
    try:
        r = subprocess.run([sys.executable, "-c", _NEG_LOOP_CHILD, str(ROOT)],
                           capture_output=True, text=True, timeout=20)
        out = r.stdout.strip()
    except subprocess.TimeoutExpired:
        out = "still scanning after 20 s (a negative size seeks the scan backwards, forever)"
    check("refused at once: a regular member of size -512 (the old scan looped on it forever)",
          out == "refused", out)
    # tarfile itself, on the two bypasses, to show what the scan now stands in front of
    for label, body in (("v7 directory", _v7dir_bomb(1 << 20)), ("padding size", _padsize_bomb(1 << 20))):
        try:
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tf:
                tf.getmembers()
            parsed = True
        except Exception:  # noqa: BLE001
            parsed = False
        check(f"(control) tarfile reads the {label} bypass without an error, so only the scan stops it",
              parsed)


# ── 21. collect, merge and land: the smaller items, each pinned ─────────────

def review_items_checks() -> None:
    import ast as _ast
    import filecmp
    from server.routes import review as rv
    print("== 21. collect, merge and land: the smaller items ==")
    src = (ROOT / "server/routes/review.py").read_text()
    tree = _ast.parse(src)

    def fn(name):
        for n in _ast.walk(tree):
            if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and n.name == name:
                return n
        return None

    # A failed collect is said, in the result and on the terminal
    rp = fn("_run_on_pod")
    rp_src = _ast.get_source_segment(src, rp) if rp else ""
    check("item 7: a failed collect-out sets collect_error and a collect_failed frame",
          "collect_error = str(e)" in rp_src and '"state": "collect_failed"' in rp_src)
    check("item 7: ...and a run that finished but collected nothing is not reported ok",
          'result["collect_error"] = collect_error' in rp_src
          and 'result["ok"] = False' in rp_src.split('result["collect_error"] = collect_error')[-1])

    # items 5/6: past the events budget, frames are elided, never refused, and
    # the terminal status frame still lands (and is the run's result)
    if hasattr(rv, "_EVENTS_MAX_FRAMES"):
        class _C:
            def __init__(self, data):
                self.data = data

            async def iter_any(self):
                for i in range(0, len(self.data), 65536):
                    yield self.data[i:i + 65536]

        class _R:
            status = 200

            def __init__(self, data):
                self.content = _C(data)

            def raise_for_status(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _H:
            def __init__(self, data):
                self.data = data

            def post(self, *a, **kw):
                return _R(self.data)
        frames = [json.dumps({"kind": "text", "payload": {"text": f"t{i}"}}) for i in range(80)]
        frames.append(json.dumps({"kind": "tool_result", "payload": {"status": "ok", "out": "x"}}))
        frames.append(json.dumps({"kind": "status", "payload": {"state": "complete", "ok": True,
                                                                 "exit_code": 0, "diff": "d"}}))
        data = ("\n\n".join(frames) + "\n").encode()
        old = rv._EVENTS_MAX_FRAMES
        rv._EVENTS_MAX_FRAMES = 50
        try:
            with tempfile.TemporaryDirectory() as td, _FloorOff():
                seen = {"n": 0}
                term = asyncio.run(rv._stream_harness_run(_H(data), "http://h", {}, "kimi",
                                                          pathlib.Path(td), idle_timeout=5,
                                                          frames_seen=seen))
                lines = rv._events_path(pathlib.Path(td), "kimi").read_text().splitlines()
        finally:
            rv._EVENTS_MAX_FRAMES = old
        kinds = [json.loads(x)["kind"] + "/" + str(json.loads(x)["payload"].get("state", "")) for x in lines]
        check("items 5/6: past the events budget the run is not refused; its result is read",
              term.get("ok") is True and term.get("exit_code") == 0, term)
        check("items 5/6: ...50 frames kept, one frames_elided marker, the rest (a tool "
              "result carrying \"status\" bytes included) elided, the terminal frame written",
              len(lines) == 52 and kinds.count("status/frames_elided") == 1
              and kinds[-1] == "status/complete" and seen.get("elided") == 31,
              {"lines": len(lines), "tail": kinds[-3:], "elided": seen.get("elided")})
    else:
        check("items 5/6: past the events budget the run is not refused; its result is read",
              False, "no _EVENTS_MAX_FRAMES: the stream is refused past a frame cap")

    # A merge conflict says what it is. There is no over-size kind: a file of any size is
    # merged on disk, and a merge git cannot run carries git's own reason
    # (scripts/test_land_merges_large_files_on_disk.py drives it).
    check("item 8: no merge is refused for size, and a merge git cannot run says why",
          '"too_large_to_merge"' not in src and '"merge_failed"' in src
          and '"reason": f"git merge-file exited {rc}"' in src)

    # A watchdog-stopped host diff says it is incomplete
    with tempfile.TemporaryDirectory() as td:
        seed = pathlib.Path(td) / "seed"
        (seed / "src").mkdir(parents=True)
        (seed / "src" / "a.txt").write_text("seed\n")
        wt = rv._prepare_worktree(pathlib.Path(td) / "w", "codex", seed=True, seed_from=seed)
        (wt / "src" / "a.txt").write_text("changed\n")
        fake = pathlib.Path(td) / "bin"
        fake.mkdir()
        # Its diff prints a part, then sleeps 30 s and only then leaves a mark:
        # a git the watchdog stopped is gone and left none. Read from the
        # process, not a clock, so a busy machine cannot fail it.
        pidf, donef = pathlib.Path(td) / "git.pid", pathlib.Path(td) / "git.done"
        (fake / "git").write_text(
            "#!/bin/sh\n"
            "for a in \"$@\"; do [ \"$a\" = diff ] && { printf 'diff --git a/x b/x\\n+part\\n'; "
            f"echo $$ > '{pidf}'; sleep 30 >/dev/null 2>&1 && touch '{donef}'; exit 0; }}; done\nexit 0\n")
        (fake / "git").chmod(0o755)
        old_path, old_to = os.environ.get("PATH", ""), getattr(rv, "_HOST_DIFF_TIMEOUT_S", None)
        if old_to is None:
            check("item 9: a host diff whose git the watchdog stopped is marked incomplete",
                  False, "no _HOST_DIFF_TIMEOUT_S: the 60 s watchdog cannot be tested quickly")
        else:
            os.environ["PATH"] = f"{fake}:{old_path}"
            rv._HOST_DIFF_TIMEOUT_S = 0.5
            try:
                out = rv._worktree_diff_host(wt)
            finally:
                os.environ["PATH"], rv._HOST_DIFF_TIMEOUT_S = old_path, old_to

            def running(pid):
                try:
                    return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0] != "Z"
                except OSError:
                    return False
            pid = int(pidf.read_text()) if pidf.exists() else None
            check("item 9: a host diff whose git the watchdog stopped is marked incomplete, "
                  "and that git was stopped, not waited for",
                  "+part" in out and "[diff incomplete: git ran past" in out
                  and pid is not None and not running(pid) and not donef.exists(),
                  {"tail": out[-120:], "pid": pid, "running": pid is not None and running(pid),
                   "finished": donef.exists()})
    cut_fn = getattr(rv, "_diff_was_cut", None)
    check("item 9: ...and the compare view knows every cut marker",
          cut_fn is not None and all(cut_fn("diff\n" + m) for m in (
              "\n[diff cut at 16777216 bytes: the run changed more than a review diff holds]\n",
              "\n[diff incomplete: git ran past 60 s and was stopped]\n",
              "\n[... 5 more bytes not kept by the Host]"))
          and not cut_fn("diff --git a/x b/x\n+ok\n"))
    check("item 9: ...and says so on the candidate (changed_files_note)",
          '"changed_files_note"' in src)

    # The GC pause covers the parse, never the extraction (which
    # _collect_out_apply does inside _collect_out_tar's yield)
    ap, ct = fn("_collect_out_apply"), fn("_collect_out_tar")
    in_pause = []
    paused = 0
    for f in (ap, ct):
        for w in _ast.walk(f):
            if isinstance(w, _ast.With) and any("gc_paused" in _ast.unparse(i.context_expr)
                                                for i in w.items):
                paused += 1
                in_pause += [c for c in _ast.walk(w) if (isinstance(c, _ast.Call)
                             and _ast.unparse(c.func).endswith("extractall"))
                             or isinstance(c, _ast.Yield)]
    uses_ct = ap is not None and "_collect_out_tar(" in _ast.unparse(ap)
    check("item 10: _collect_out_apply extracts OUTSIDE the GC pause (the pause in "
          "_collect_out_tar ends before it yields the members)",
          ap is not None and ct is not None and uses_ct and paused == 1 and not in_pause,
          ([_ast.unparse(c)[:80] for c in in_pause], paused, uses_ct))

    # Land compares bytes, not filecmp's (size, mtime) cache
    with tempfile.TemporaryDirectory() as td:
        a, b = pathlib.Path(td) / "a", pathlib.Path(td) / "b"
        a.write_bytes(b"0123456789")
        b.write_bytes(b"0123456789")
        st = b.stat()
        filecmp.clear_cache()
        first = filecmp.cmp(a, b, shallow=False)
        b.write_bytes(b"0123456780")                     # same size...
        os.utime(b, ns=(st.st_atime_ns, st.st_mtime_ns))  # ...and the same mtime
        stale = filecmp.cmp(a, b, shallow=False)
        check("item 13: (control) filecmp.cmp answers a same-size, same-mtime edit from its cache",
              first is True and stale is True)
        same = getattr(rv, "_same_bytes", None)
        check("item 13: _same_bytes sees the edit", same is not None and same(a, b) is False)
    check("item 13: ...and review.py no longer calls filecmp.cmp", "filecmp.cmp(" not in src)


# ── 22. stream framing and the caps: the rest, each pinned ──────────────────

class _FloorOff:
    """The disk floor off for a check that is not about it: the gate must
    not depend on how full this box's disk is."""
    def __enter__(self):
        from server import harness_input as hi
        self.hi, self.orig = hi, hi.disk_floor
        hi.disk_floor = lambda *a, **k: 0

    def __exit__(self, *exc):
        self.hi.disk_floor = self.orig


def _skim_parity_case(hi, data: bytes, rng) -> str:
    """'' when the streaming parser reads `data` exactly as json.loads does
    (the same object, or both refuse), fed in random chunks; else why not."""
    try:
        want = json.loads(data)
        if not isinstance(want, dict):
            want = "refuse"
    except (ValueError, RecursionError):
        want = "refuse"
    pieces, i = [], 0
    while i < len(data):
        n = rng.choice([1, 2, 3, 5, 7, 11, 64, 4096])
        pieces.append(data[i:i + n])
        i += n
    pieces.reverse()
    try:
        got, _info = hi._skim(lambda: pieces.pop() if pieces else b"", {("x",): ("head", 1 << 30)})
    except hi.LargeRecordError:
        got = "refuse"
    except Exception as exc:  # noqa: BLE001 - the parser must refuse, never crash
        return f"raised {exc!r}"[:120]
    if got == want or (isinstance(got, dict) and isinstance(want, dict)
                       and json.dumps(got, sort_keys=True) == json.dumps(want, sort_keys=True)):
        return ""
    return f"differs: {str(got)[:60]} vs {str(want)[:60]}"


def review_763_checks() -> None:
    import random
    import threading
    from server import harness_input as hi
    from server.routes import review as rv
    print("== 22. stream framing and the caps ==")

    # -- invalid and surrogate UTF-8, read exactly as json.loads reads it
    rng = random.Random(763)
    atoms = [b"a", b"\\n", b"\\\\", b'\\"', b"\\u00e9", b"\\ud83d\\ude00", b"\\ud83d", b"\\ude00",
             "é".encode(), "一".encode(), "\U0001F600".encode(),
             b"\xed\xa0\xbd", b"\xed\xb8\x80", b"\xed\xa0\x80"]         # raw surrogates (json.loads: surrogatepass)
    invalid = [b"\xff", b"\xc3", b"\xe4\xb8", b"\x80", b"\xf0\x9f\x98"]  # invalid UTF-8 (json.loads: refuse)
    bad = []
    n_ok = n_refuse = 0
    for i in range(3000):
        k = rng.randint(1, 4)
        fields = []
        for j in range(k):
            val = [rng.choice(atoms) for _ in range(rng.randint(0, 40))]
            if rng.random() < 0.15:
                val.insert(rng.randint(0, len(val)), rng.choice(invalid))
            val = b"".join(val)
            fields.append(b'"f%d": "%s"' % (j, val))
        data = b"{" + b", ".join(fields) + b"}"
        v = _skim_parity_case(hi, data, rng)
        if v:
            bad.append((i, v, data[:80]))
        try:
            json.loads(data)
            n_ok += 1
        except ValueError:
            n_refuse += 1
    check("the large-record parser reads raw surrogate bytes, split escapes and invalid UTF-8 "
          "exactly as json.loads does (3,000 records, random chunk edges)", not bad,
          {"mismatches": len(bad), "first": bad[:3]})
    check("...and the fuzz is not vacuous (both accepted and refused records)",
          n_ok >= 300 and n_refuse >= 300, {"accepted": n_ok, "refused": n_refuse})
    # a raw high surrogate right at a chunk edge
    data = b'{"x": "ab\xed\xa0\xbdcd"}'
    for cut in range(6, len(data)):
        parts = [data[cut:], data[:cut]]
        try:
            got = hi._skim(lambda: parts.pop() if parts else b"", {})[0]
        except hi.LargeRecordError as exc:
            got = f"refused {exc}"
        if got != json.loads(data):
            bad.append((cut, got))
            break
    check("a raw surrogate at every chunk edge is neither refused nor duplicated",
          not bad[-1:] or bad[-1][0] >= 3000, bad[-1:])

    # -- framing does not depend on how the network cut the stream
    MiB_ = 1 << 20
    def frame(i):
        return json.dumps({"seq": i, "kind": "text", "payload": {"text": f"t{i}"}}).encode()
    big = json.dumps({"seq": 9, "kind": "status", "payload": {
        "state": "complete", "ok": True, "transcript": "T" * (9 * MiB_), "diff": "D" * (9 * MiB_)}}).encode()
    data = (frame(1) + b"\n\n  \n" + frame(2) + b"\n" + big + b"\n" + frame(3) + b"\n"
            + b"x" * (9 * MiB_) + b"\n" + frame(4) + b"\n" + big)          # last one unterminated

    class _Content:
        def __init__(self, sizes, seed):
            self.sizes, self.r = sizes, random.Random(seed)

        def iter_any(self):
            async def g():
                pos = 0
                while pos < len(data):
                    n = self.r.choice(self.sizes)
                    yield data[pos:pos + n]
                    pos += n
            return g()

    async def frames(sizes, seed):
        out = []
        async for raw in rv._iter_ndjson_lines(_Content(sizes, seed)):
            if isinstance(raw, rv._LargeFrame):
                f = raw.as_frame(); pl = f.get("payload", {})
                out.append(("large", f.get("kind"), len(pl.get("transcript", "")),
                            len(pl.get("diff", "")), len(pl.get("text", ""))))
            else:
                ev = rv._parse_stream_record(raw)
                out.append(("small", ev and ev.get("seq")))
        return out
    runs = [asyncio.run(frames(sz, sd)) for sz, sd in (([65536], 1), ([1, 7, 100, 65536], 2),
                                                       ([3 * MiB_], 3), ([len(data)], 4))]
    check("what the stream keeps does not depend on how the network cut it (a large record "
          "arriving whole in one read takes the large-record path too)",
          all(r == runs[0] for r in runs) and [x[0] for x in runs[0]].count("large") == 3,
          [r[:6] for r in runs])

    # -- keep limits in BYTES, never over them while growing
    t = hi._Text("tail", 1000)
    peak = 0
    for _ in range(500):
        t.add("\U0001F600" * 97 + "a")
        peak = max(peak, t.kept)
    v = t.value()
    check("the tail keeper never holds more than its limit in bytes, and keeps whole characters",
          peak <= 1000 and len(v.split("]\n", 1)[1].encode("utf-8")) <= 1000
          and v.startswith("[... ") and "�" not in v, peak)
    h = hi._Text("head", 1001)
    h.add("一" * 1000)
    hv = h.value()
    check("the head keeper cuts on a character boundary, in bytes",
          hv.startswith("一" * 333) and "一" * 334 not in hv and hv.endswith("not kept by the Host]"),
          hv[:10])

    # -- pieces on character boundaries, a head that
    #    ends at its first cut; fuzzed with small pieces, so one add makes many
    import re as _re
    orig_piece = hi._PIECE
    bad3 = []
    alph = ["a", "b", "é", "一", "\U0001F600", "\n", '"', "\\"]
    for piece in (4, 16, 1 << 16):
        hi._PIECE = piece
        r3 = random.Random(piece)
        for i in range(4000):
            adds = ["".join(r3.choice(alph) for _ in range(r3.randint(0, 120)))
                    for _ in range(r3.randint(1, 4))]
            lim, mode = r3.randint(1, 300), r3.choice(["tail", "head"])
            t3 = hi._Text(mode, lim)
            try:
                for x in adds:
                    t3.add(x)
                v3 = t3.value()
            except Exception as exc:  # noqa: BLE001
                bad3.append((piece, i, repr(exc)[:60]))
                continue
            whole = "".join(adds).encode()
            m3 = (_re.match(r"\[\.\.\. (\d+) earlier bytes not kept by the Host\]\n", v3) if mode == "tail"
                  else _re.search(r"\n\[\.\.\. (\d+) more bytes not kept by the Host\]\Z", v3))
            kept3 = ((v3[m3.end():] if mode == "tail" else v3[:m3.start()]) if m3 else v3).encode()
            ok3 = (whole.endswith(kept3) if mode == "tail" else whole.startswith(kept3))
            ok3 &= len(kept3) <= lim and (not m3 or int(m3.group(1)) == len(whole) - len(kept3))
            ok3 &= (len(whole) <= lim) == (m3 is None) and len(kept3) >= min(len(whole), lim) - 3
            if not ok3:
                bad3.append((piece, i, mode, lim))
    hi._PIECE = orig_piece
    check("the keepers decode, keep a true tail/head within their byte limit, across piece "
          "edges (12,000 cases, pieces of 4, 16 and 64 KiB bytes)", not bad3, bad3[:4])
    h3 = hi._Text("head", 5)
    h3.add("abcd一")
    h3.add("XYZ")
    check("a head never keeps text after a character it dropped",
          h3.value().startswith("abcd\n[... "), h3.value())
    # an end-to-end repro: a 1 MiB + 64 KiB value with a 2-byte
    # character straddling byte 65536, in a large record
    val = "x" * 65535 + "é" + "y" * ((1 << 20) + (64 << 10) - 65537)
    rec3 = json.dumps({"ok": True, "stderr": val, "n": list(range(210_000))}).encode()
    try:
        o3, i3 = hi.skim_bytes(rec3, rv._RESULT_RULES)
        ok_e2e = o3["ok"] is True and o3["stderr"].endswith("y" * 100) and not o3["stderr"].startswith("x")
    except Exception as exc:  # noqa: BLE001
        ok_e2e = repr(exc)[:120]
    check("a valid record whose kept tail starts at a piece edge inside a character parses",
          ok_e2e is True, ok_e2e)

    # -- a large record that trickles is refused after LARGE_RECORD_TOTAL_S,
    #    and gives its parse slot back
    old_total = hi.LARGE_RECORD_TOTAL_S
    hi.LARGE_RECORD_TOTAL_S = 1.0

    async def trickle_case():
        # 100 pieces 50 ms apart: 5 s of trickle against a 1 s limit. What
        # shows the limit held is how much was read before the refusal, not a
        # clock: a busy machine only makes the pieces come slower.
        read = [0]

        async def slow():
            for _ in range(100):
                await asyncio.sleep(0.05)
                read[0] += 1
                yield b" "
        try:
            await hi.parse_large_record(b'{"a": 1', slow(), {}, "t")
            refused = False
        except hi.HarnessInputRefused:
            refused = True
        el = read[0]

        async def none():
            if False:
                yield b""
        again = await hi.parse_large_record(b'{"a": 2}', none(), {}, "t")
        return refused, el, again
    try:
        refused, el, again = asyncio.run(trickle_case())
    finally:
        hi.LARGE_RECORD_TOTAL_S = old_total
    check("a large record still trickling past LARGE_RECORD_TOTAL_S is refused before the trickle "
          "ends (at most 60 of its 100 pieces read), and the slot is free again",
          refused and el <= 60 and again == ({"a": 2}, None), (refused, el, again))

    # -- the disk floor is checked again while frames are held back; a large
    #    file_edit frame keeps a head for the run card; a fresh file is indexed
    class _C2:
        def __init__(self, data):
            self.data = data

        async def iter_any(self):
            for i in range(0, len(self.data), 4096):
                yield self.data[i:i + 4096]
                await asyncio.sleep(0.002)

    class _R2:
        status = 200

        def __init__(self, data):
            self.content = _C2(data)

        def raise_for_status(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _H2:
        def __init__(self, data):
            self.data = data

        def post(self, *a, **kw):
            return _R2(self.data)
    lines2 = [json.dumps({"kind": "text", "payload": {"text": f"t{i}"}}) for i in range(3000)]
    lines2.append(json.dumps({"kind": "file_edit", "payload": {"path": "big.js", "diff": "+" * (9 << 20)}}))
    lines2 += [json.dumps({"kind": "text", "payload": {"text": f"after{i}"}}) for i in range(200)]
    lines2.append(json.dumps({"kind": "status", "payload": {"state": "complete", "ok": True}}))
    data2 = ("\n".join(lines2) + "\n").encode()
    state = {"low": True, "t0": time.monotonic()}
    orig_free, orig_floor = hi.free_bytes, hi.disk_floor
    hi.disk_floor = lambda *a, **k: 100
    hi.free_bytes = lambda _p: 0 if time.monotonic() - state["t0"] < 1.5 else 1 << 40
    try:
        with tempfile.TemporaryDirectory() as td:
            term2 = asyncio.run(rv._stream_harness_run(_H2(data2), "http://h", {}, "kimi",
                                                       pathlib.Path(td), idle_timeout=5,
                                                       frames_seen={}))
            path2 = rv._events_path(pathlib.Path(td), "kimi")
            got2 = [json.loads(x) for x in path2.read_bytes().splitlines()]
            indexed = str(path2) in rv._EVENTS_INDEX
    finally:
        hi.free_bytes, hi.disk_floor = orig_free, orig_floor
    kinds2 = [g.get("payload", {}).get("state") or g.get("kind") for g in got2]
    fe = [g for g in got2 if g.get("kind") == "file_edit"]
    check("a brief low-disk spell holds frames back, then the stream is written again",
          "frames_elided" in kinds2 and kinds2[-1] == "complete" and term2.get("ok") is True
          and any(g.get("kind") == "text" for g in got2[kinds2.index("frames_elided") + 1:]),
          kinds2[:3] + kinds2[-3:])
    check("a large file_edit frame keeps the head of its diff in the events file (the run card shows it)",
          len(fe) == 1 and fe[0]["payload"]["diff"].startswith("+" * 1000)
          and len(fe[0]["payload"]["diff"]) < 70_000, len(fe[0]["payload"]["diff"]) if fe else None)
    check("a fresh events file gets its index entry as the stream starts (no rescan later)", indexed)

    # -- a timed-out large record is skipped to its
    #    end (no junk frame from its tail); only OUR timeout is relabelled;
    #    a held status goes out in order when the disk recovers
    class _C4:
        # like aiohttp's AsyncStreamIterator: each __anext__ is a fresh read,
        # so a cancelled one loses nothing (a Python async generator would
        # be closed by the cancellation)
        def __init__(self, parts):
            self.parts = list(parts)

        def iter_any(self):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.parts:
                raise StopAsyncIteration
            delay, chunk = self.parts[0]
            if delay:
                await asyncio.sleep(delay)
                self.parts[0] = (0, chunk)
            return self.parts.pop(0)[1]
    # A NON-terminal frame: a run's terminal frame cut short is kept as its
    # result instead (test_large_result_record_not_failed_by_slot_wait.py).
    big4 = b'{"kind": "status", "payload": {"state": "working", "transcript": "' + b"T" * (9 << 20)
    parts4 = [(0, b'{"kind": "text", "payload": {"text": "before"}}\n'), (0, big4),
              (1.5, b"T" * (3 << 20) + b'"}}\n'), (0, b'{"kind": "text", "payload": {"text": "after"}}\n')]
    old_total = hi.LARGE_RECORD_TOTAL_S
    hi.LARGE_RECORD_TOTAL_S = 0.5

    async def frames4():
        out = []
        async for raw in rv._iter_ndjson_lines(_C4(parts4)):
            if isinstance(raw, rv._LargeFrame):
                out.append(("large", raw.error is not None))
            else:
                ev = rv._parse_stream_record(raw)
                out.append(("small", (ev or {}).get("payload", {}).get("text")))
        return out
    try:
        got4 = asyncio.run(frames4())
    finally:
        hi.LARGE_RECORD_TOTAL_S = old_total
    check("a large record refused for taking too long is skipped to its end: the next frame is the "
          "next record, not the refused one's tail", got4 == [("small", "before"), ("large", True),
                                                              ("small", "after")], got4)

    async def other_timeout():
        async def rest():
            yield b" "
            raise TimeoutError("aiohttp's own")
        try:
            await hi.parse_large_record(b'{"a": ', rest(), {}, "t")
        except hi.HarnessInputRefused as exc:
            return f"relabelled: {exc}"
        except TimeoutError as exc:
            return f"passed through: {exc}"
    r4 = asyncio.run(other_timeout())
    check("another timeout inside a large record passes through as itself, never relabelled "
          "as the large-record limit", r4 == "passed through: aiohttp's own", r4)

    lines5 = [json.dumps({"kind": "text", "payload": {"text": "first"}}),
              json.dumps({"kind": "status", "payload": {"state": "A"}})]
    lines5 += [json.dumps({"kind": "text", "payload": {"text": f"low{i}"}}) for i in range(50)]
    data5a = ("\n".join(lines5) + "\n").encode()
    data5b = (json.dumps({"kind": "status", "payload": {"state": "B"}}) + "\n"
              + json.dumps({"kind": "text", "payload": {"text": "after"}}) + "\n"
              + json.dumps({"kind": "status", "payload": {"state": "complete", "ok": True}}) + "\n").encode()

    class _C5:
        async def iter_any(self):
            yield data5a
            await asyncio.sleep(1.3)
            yield data5b

    class _R5:
        status = 200
        content = _C5()

        def raise_for_status(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _H5:
        def post(self, *a, **kw):
            return _R5()
    t05 = time.monotonic()
    orig_free, orig_floor = hi.free_bytes, hi.disk_floor
    hi.disk_floor = lambda *a, **k: 100
    hi.free_bytes = lambda _p: 1 << 40 if time.monotonic() - t05 > 1.0 else 0
    try:
        with tempfile.TemporaryDirectory() as td:
            asyncio.run(rv._stream_harness_run(_H5(), "http://h", {}, "kimi", pathlib.Path(td),
                                               idle_timeout=5, frames_seen={}))
            got5 = [json.loads(x) for x in rv._events_path(pathlib.Path(td), "kimi").read_bytes().splitlines()]
    finally:
        hi.free_bytes, hi.disk_floor = orig_free, orig_floor
    seq5 = [(g["payload"].get("state") or g["payload"].get("text")) for g in got5]
    # the first frame is written before the first disk check; then held back
    check("when the disk recovers, the held status goes out at once and in order, the file says "
          "frames are kept again, and later frames follow",
          seq5[-5:] == ["A", "frames_kept", "B", "after", "complete"] and "frames_elided" in seq5, seq5)

    # the marker names the limit that will not lift when both are hit
    old_max = rv._EVENTS_MAX_FRAMES
    rv._EVENTS_MAX_FRAMES = 2
    orig_free, orig_floor = hi.free_bytes, hi.disk_floor
    hi.disk_floor, hi.free_bytes = (lambda *a, **k: 100), (lambda _p: 0)
    try:
        with tempfile.TemporaryDirectory() as td:
            asyncio.run(rv._stream_harness_run(_H2(data2), "http://h", {}, "kimi", pathlib.Path(td),
                                               idle_timeout=5, frames_seen={}))
            mk = [json.loads(x)["payload"].get("detail", "")
                  for x in rv._events_path(pathlib.Path(td), "kimi").read_bytes().splitlines()
                  if b'"frames_elided"' in x]
    finally:
        rv._EVENTS_MAX_FRAMES = old_max
        hi.free_bytes, hi.disk_floor = orig_free, orig_floor
    check("a disk hold that turns into the frame budget (which never lifts) says so in a second "
          "marker, so the file does not end on 'until it has room'",
          len(mk) == 2 and "until it has room" in mk[0] and "not kept here" in mk[1], mk)

    # -- caps protect the platform and never fail real work: a worktree after
    #    `npm install` is exported with its node_modules
    from server.routes import compute as _cm
    check("the export member cap stays at 200,000 (main's), the session cap with it, and the "
          "name budget holds 200,000 pnpm-length paths",
          rv._EXPORT_MAX_MEMBERS >= 200_000 and _cm._SESSION_TAR_MAX_MEMBERS == rv._EXPORT_MAX_MEMBERS
          and rv._EXPORT_MAX_NAME_BYTES >= 200_000 * 200
          and hi.TAR_EXT_TOTAL_MAX >= 200_000 * 200,
          (rv._EXPORT_MAX_MEMBERS, _cm._SESSION_TAR_MAX_MEMBERS, rv._EXPORT_MAX_NAME_BYTES,
           hi.TAR_EXT_TOTAL_MAX))

    # -- what keeps 200,000 members small: the member check's flat hash tables
    rng2 = random.Random(200_000)
    vals = [rng2.randrange(-(1 << 63) + 1, 1 << 63) for _ in range(3000)]
    vals += [k << 40 for k in range(1, 400)] + [-(k << 40) for k in range(1, 400)]   # shared low bits
    vals += [1, 0, -2, -1 << 62, (1 << 63) - 1, -(1 << 63) + 1]
    vals = [v for v in vals if v != -1]             # never a hash: the empty-slot mark
    inside, outside = vals[::2], vals[1::2]
    tbl = rv._PathHashes(len(inside))
    for v in inside:
        tbl.add(v)
    for v in inside[:50]:
        tbl.add(v)                                  # again: no second entry
    ref = set(inside)
    wrong = [v for v in vals + [-1] if (v in tbl) != (v in ref)]
    try:
        tbl.add(-1); minus_one = "added"
    except ValueError:
        minus_one = "refused"
    check("_PathHashes answers membership exactly as a set does (random 64-bit values, values "
          "sharing their low 40 bits, 0, the extremes), counts each value once, is at most half "
          "full, and refuses -1 (its empty mark, which hash() never returns)",
          not wrong and tbl.count == len(ref) and 2 * tbl.count <= len(tbl._slots)
          and minus_one == "refused" and hash(-1) != -1 and hash((-1, "x")) != -1,
          (wrong[:5], tbl.count, len(ref), minus_one))
    small = rv._PathHashes(4)
    try:
        for v in range(1, 100):
            small.add(v)
        over = None
    except RuntimeError as exc:
        over = str(exc)
    check("_PathHashes refuses an entry past half its slots instead of filling up",
          over is not None and small.count * 2 <= len(small._slots), (over, small.count))

    # -- the collect-out tar lets its members go a slice at a time, accepted or refused,
    #    and sets them aside from the collector only while they are in use
    import gc as _gc
    lean_cls = hi.lean_tarinfo()
    real_freeze, real_unfreeze = _gc.freeze, _gc.unfreeze
    at_freeze: list = []
    at_unfreeze: list = []

    def _counting_freeze():
        at_freeze.append(sum(1 for o in _gc.get_objects() if type(o) is lean_cls))
        real_freeze()

    def _counting_unfreeze():
        # frozen objects are not in get_objects(): count right after the
        # hand-back, before anything else can run in this thread
        real_unfreeze()
        at_unfreeze.append(sum(1 for o in _gc.get_objects() if type(o) is lean_cls))

    def _members_after(names: list):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for nm in names:
                tf.addfile(tarfile.TarInfo(nm))
        tp = pathlib.Path(tempfile.mkdtemp(prefix="cseries-rel-")) / "t.tar"
        tp.write_bytes(buf.getvalue())
        seen = {}
        at_freeze.clear()
        at_unfreeze.clear()
        _gc.collect()
        base = sum(1 for o in _gc.get_objects() if type(o) is lean_cls)
        _gc.freeze, _gc.unfreeze = _counting_freeze, _counting_unfreeze
        try:
            with rv._collect_out_tar(tp) as (tf, members):
                seen["kept"] = len(members)
                seen["frozen_in_use"] = _gc.get_freeze_count()
                seen["tf"] = tf
            verdict = "accepted"
        except hi.HarnessInputRefused as exc:
            verdict = str(exc)[:60]
        finally:
            _gc.freeze, _gc.unfreeze = real_freeze, real_unfreeze
            shutil.rmtree(tp.parent, ignore_errors=True)
        return {"verdict": verdict, "kept": seen.get("kept"),
                "left": seen["tf"].members if "tf" in seen else None,
                "frozen_in_use": seen.get("frozen_in_use"),
                "members_at_freeze": [c - base for c in at_freeze],
                "members_at_unfreeze": [c - base for c in at_unfreeze],
                "frozen_after": _gc.get_freeze_count(), "gc_on": _gc.isenabled()}
    ok = _members_after(["f%d" % i for i in range(10)])
    bad = _members_after(["f%d" % i for i in range(10)] + ["a/../../x"])
    check("_collect_out_tar yields the members to land, sets them aside from the collector "
          "while in use (all 10 alive at the freeze), lets every one go BEFORE handing the "
          "set-aside back (none alive at the unfreeze), then leaves the TarFile holding none, "
          "nothing frozen and the collector on",
          ok["verdict"] == "accepted" and ok["kept"] == 10 and ok["left"] == []
          and ok["frozen_in_use"] > 0 and ok["members_at_freeze"] == [10]
          and ok["members_at_unfreeze"] == [0]
          and ok["frozen_after"] == 0 and ok["gc_on"], ok)
    check("...a tar refused by the member check lets every member go BEFORE the collector "
          "comes back (none alive at the freeze), and leaves nothing frozen",
          "refused" in bad["verdict"] and bad["members_at_freeze"] in ([], [0])
          and bad["frozen_after"] == 0 and bad["gc_on"], bad)

    class _Rec(list):
        def __delitem__(self, key):
            cut.append(len(self[key]))
            super().__delitem__(key)
    cut: list = []
    step_was = hi.RELEASE_STEP
    hi.RELEASE_STEP = 3
    try:
        rec = _Rec(range(10))
        hi.release_members(rec, None, (1, 2))
    finally:
        hi.RELEASE_STEP = step_was
    check("release_members empties each list a slice of RELEASE_STEP at a time (10 -> "
          "1+3+3+3 from the end) and skips anything that is not a list",
          rec == [] and cut == [3, 3, 3, 1], cut)

    # -- the set-aside is counted across threads: a plain pause that outlives a
    #    collect's never freezes after it; one collect ending never hands back
    #    another's members; nothing stays frozen when all have ended
    import threading as _th

    def _run_seq(steps: list) -> list:
        """steps: (thread name, action); each thread does its own actions in
        order, one at a time across threads, in the listed order."""
        names = sorted({t for t, _ in steps})
        turn = {n: _th.Event() for n in range(len(steps) + 1)}
        cms: dict = {}
        seen: list = []

        def worker(me):
            for i, (t, act) in enumerate(steps):
                if t != me:
                    continue
                turn[i].wait(5)
                kind, which = act.split(":")
                if kind == "enter":
                    cm = hi.gc_paused() if which == "pause" else hi.gc_set_aside()
                    cm.__enter__(); cms[(me, which)] = cm
                else:
                    cms.pop((me, which)).__exit__(None, None, None)
                seen.append((i, _gc.get_freeze_count() > 0))
                turn[i + 1].set()
        ts = [_th.Thread(target=worker, args=(n,)) for n in names]
        for t in ts:
            t.start()
        turn[0].set()
        for t in ts:
            t.join(10)
        return [f for _, f in sorted(seen)]
    b1 = _run_seq([("F", "enter:pause"), ("C", "enter:aside"), ("C", "enter:pause"),
                   ("C", "exit:pause"), ("C", "exit:aside"), ("F", "exit:pause")])
    check("gc_set_aside: a plain pause (fallback) that ends after a collect's set-aside has "
          "closed does not freeze, so nothing is left frozen with no one to hand it back",
          b1 == [False] * 6 and _gc.get_freeze_count() == 0 and _gc.isenabled(), b1)
    n2 = _run_seq([("A", "enter:aside"), ("B", "enter:aside"), ("A", "enter:pause"),
                   ("A", "exit:pause"), ("A", "exit:aside"), ("B", "exit:aside")])
    check("gc_set_aside: with two collects, the first to end does not hand back the other's "
          "set-aside members; the last one does",
          n2 == [False, False, False, True, True, False] and _gc.get_freeze_count() == 0
          and _gc.isenabled(), n2)

    # -- the name budget counts what Python holds for a name, not only its bytes
    emoji = "\U0001F600".encode()
    costs = {"ascii": hi._name_cost(b"a" * 50), "prefixed": hi._name_cost(b"a" * 5, b"p" * 4),
             "latin1": hi._name_cost("é".encode() * 10), "cjk": hi._name_cost("一".encode() * 10),
             "one_emoji": hi._name_cost(emoji + b"a" * 50),
             "emoji_prefix": hi._name_cost(b"a" * 10, emoji),
             "invalid": hi._name_cost(b"\xff" * 10)}
    check("_name_cost: an ASCII name costs its bytes; one emoji makes every character cost 4 "
          "bytes (the prefix too); a name never costs less than its bytes",
          costs == {"ascii": 50, "prefixed": 10, "latin1": 20, "cjk": 30, "one_emoji": 51 * 4,
                    "emoji_prefix": (1 + 1 + 10) * 4, "invalid": 20}, costs)

    def _tar_named(names: list) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT, encoding="utf-8") as tf:
            for nm in names:
                tf.addfile(tarfile.TarInfo(nm))
        return buf.getvalue()
    wide = _tar_named(["\U0001F600" + "a" * 50 + "%03d" % i for i in range(10)])
    narrow = _tar_named(["b" * 54 + "%03d" % i for i in range(10)])
    try:
        hi.prescan_tar(io.BytesIO(wide), "t", max_members=100, max_data=1 << 20, max_names=1000)
        wide_v = "accepted"
    except hi.HarnessInputRefused as exc:
        wide_v = str(exc)
    narrow_v = hi.prescan_tar(io.BytesIO(narrow), "t", max_members=100, max_data=1 << 20,
                              max_names=1000)
    check("the pre-scan refuses names over the budget by what Python holds (10 names of 57 "
          "bytes, 54 characters of 4 bytes each), and passes the same bytes in ASCII",
          "member names over 1000" in wide_v and narrow_v["names"] == 570, (wide_v, narrow_v))

    # -- lean_tarinfo keeps one int per mode and zero device numbers off devices,
    #    and the data filter makes the same member of it as of tarfile's own TarInfo
    raw = bytearray()
    for nm, typ in (("l", tarfile.SYMTYPE), ("f", tarfile.REGTYPE), ("c", tarfile.CHRTYPE)):
        t = tarfile.TarInfo(nm); t.type = typ; t.linkname = "f" if typ == tarfile.SYMTYPE else ""
        h = bytearray(t.tobuf(format=tarfile.USTAR_FORMAT))
        h[100:108] = b"7777777\0"; h[329:337] = b"7777777\0"; h[337:345] = b"0000012\0"
        h[148:156] = b" " * 8; h[148:156] = b"%06o\0 " % sum(h)
        raw += h
    raw += b"\0" * 1024
    with tarfile.open(fileobj=io.BytesIO(bytes(raw)), mode="r:", tarinfo=hi.lean_tarinfo()) as tf:
        lean_m = tf.getmembers()
    with tarfile.open(fileobj=io.BytesIO(bytes(raw)), mode="r:") as tf:
        plain_m = tf.getmembers()
    got = [(m.name, m.mode, m.devmajor, m.devminor) for m in lean_m]
    same_filter = True
    for a, b in zip(lean_m, plain_m):
        if a.ischr():
            continue                                 # refused by the data filter either way
        fa, fb = tarfile.data_filter(a, "/tmp/x"), tarfile.data_filter(b, "/tmp/x")
        same_filter &= (fa.mode, fa.uid, fa.gid, fa.uname, fa.gname) == \
            (fb.mode, fb.uid, fb.gid, fb.uname, fb.gname)
    check("lean_tarinfo: mode kept as its 0o7777 bits (one shared int), device numbers zero "
          "except on a device, and filter='data' yields the same mode and owners as tarfile's "
          "TarInfo",
          got == [("l", 0o7777, 0, 0), ("f", 0o7777, 0, 0), ("c", 0o7777, 0o7777777, 0o12)]
          and lean_m[0].mode is lean_m[1].mode and same_filter, (got, same_filter))

    # -- result_cut: set only when something was cut, surfaced as a sentence
    async def _plr(body: bytes):
        async def rest():
            for i in range(8 << 20, len(body), 1 << 16):
                yield body[i:i + (1 << 16)]
        return await hi.parse_large_record(body[:8 << 20], rest(), rv._RESULT_RULES, "t")
    whole = json.dumps({"ok": True, "diff": "d" * (7 << 20), "transcript": "t" * (1536 << 10)}).encode()
    obj, info = asyncio.run(_plr(whole))
    check("a large record with nothing over its keep limits reports no cut (info None)",
          info is None and len(obj["diff"]) == 7 << 20, info)
    cutb = json.dumps({"ok": True, "diff": "d" * (9 << 20), "transcript": "t"}).encode()
    obj, info = asyncio.run(_plr(cutb))
    sent = rv._result_cut_sentence(info) if info else ""
    check("...and one over them reports it, as a sentence a person reads",
          bool(info) and "the diff was 9.0 mib, and the host kept its first 8.0 mib" in sent.lower(), sent)
    norm = rv._normalize_run("kimi", {"ok": True, "result_cut": info}, "", time.time())
    norm2 = rv._normalize_run("kimi", {"ok": True}, "", time.time())
    check("...carried on the run's result (result_note), and absent when nothing was cut",
          norm.get("result_note") == sent and "result_note" not in norm2 and "result_cut" not in norm2)

    # -- large-record parses: at most LARGE_PARSES_MAX at once, on their own pool
    live, peak_live, names = [0], [0], set()
    lock = threading.Lock()
    orig = hi._skim

    def counting(read, rules):
        with lock:
            live[0] += 1
            peak_live[0] = max(peak_live[0], live[0])
            names.add(threading.current_thread().name.split("_")[0])
        try:
            time.sleep(0.2)
            return orig(read, rules)
        finally:
            with lock:
                live[0] -= 1
    hi._skim = counting

    async def many():
        body = json.dumps({"ok": True, "x": "y"}).encode()
        async def one():
            async def rest():
                if False:
                    yield b""
            return await hi.parse_large_record(body, rest(), {}, "t")
        return await asyncio.gather(*(one() for _ in range(3 * hi.LARGE_PARSES_MAX)))
    try:
        res = asyncio.run(many())
    finally:
        hi._skim = orig
    check(f"concurrent large-record parses: at most LARGE_PARSES_MAX ({hi.LARGE_PARSES_MAX}) at once, "
          "on their own threads (not the default pool)",
          peak_live[0] <= hi.LARGE_PARSES_MAX and names == {"large-record"}
          and all(r[0] == {"ok": True, "x": "y"} for r in res), {"peak": peak_live[0], "threads": names})

    # -- the events file: numbered from an index, never recounted per frame
    with tempfile.TemporaryDirectory() as td:
        sd = pathlib.Path(td)
        pth = rv._events_path(sd, "kimi")
        with open(pth, "w") as fh:
            for i in range(100_000):
                fh.write(json.dumps({"seq": i, "kind": "text", "payload": {"text": "x" * 200}}) + "\n")
        t0 = time.process_time()           # CPU: the cost of a recount, not the machine's load
        first = rv._events_state(pth)
        t_first = time.process_time() - t0
        t0 = time.process_time()
        for i in range(300):
            rv._append_run_frame(sd, "kimi", "text", {"text": f"k{i}"})
        t_app = time.process_time() - t0
        rv._append_run_frame(sd, "kimi", "status", {"state": "complete", "ok": True})
        lines = pth.read_bytes().splitlines()
        seqs = [json.loads(x)["seq"] for x in lines[100_000:]]
        check("300 appended frames are numbered from the index, not by recounting a 100,000-line "
              "file each time (< 0.5 s of CPU for all 300)",
              first == (100_000, False) and t_app < 0.5 and seqs == list(range(100_000, 100_301)),
              {"first_scan_s": round(t_first, 3), "appends_s": round(t_app, 3)})
        check("...the index knows the terminal frame, so _close_run_terminal writes nothing more",
              rv._events_state(pth) == (100_301, True))
        rv._close_run_terminal(sd, "kimi", ok=False, error="late")
        check("...(and it did not)", len(pth.read_bytes().splitlines()) == 100_301)
        with open(pth, "w") as fh:             # a replaced file is counted again
            fh.write(json.dumps({"seq": 0, "kind": "text", "payload": {}}) + "\n")
        check("...and a replaced file is recounted, not trusted from the index",
              rv._events_state(pth) == (1, False))
    check("frames are written UTF-8 (ensure_ascii=False) with seq first; a lone surrogate "
          "becomes its own JSON escape",
          rv._frame_line_bytes({"kind": "text", "payload": {"text": "é\ud800"}, "seq": 7})
          == '{"seq": 7, "kind": "text", "payload": {"text": "é\\ud800"}}\n'.encode("utf-8"),
          rv._frame_line_bytes({"kind": "text", "payload": {"text": "é\ud800"}, "seq": 7}))

    # -- downloads: a disk floor, a total time limit, OSError as a sentence
    from aiohttp import web
    import aiohttp as _aiohttp
    from server import browser_agents as ba
    from server.vault import authz

    async def dl_cases():
        async def fast(_r):
            return web.Response(body=b"x" * (1 << 20))

        async def trickle(r):
            resp = web.StreamResponse()
            resp.enable_chunked_encoding()
            await resp.prepare(r)
            try:
                for _ in range(100):
                    await resp.write(b"y" * 1024)
                    await asyncio.sleep(0.05)
            except Exception:  # noqa: BLE001
                pass
            return resp
        app = web.Application()
        app.router.add_get("/fast", fast)
        app.router.add_get("/slow", trickle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)      # loopback: only this gate dials it
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        authz.browser_base_url, authz.browser_headers = (lambda: base), (lambda: {})
        out = {}
        with tempfile.TemporaryDirectory() as td:
            dest = pathlib.Path(td) / "a.csv"
            orig_free = hi.free_bytes
            hi.free_bytes = lambda _p: 1 << 20              # 1 MiB free: under any floor
            try:
                await ba._download("/fast", dest)
                out["floor"] = "saved"
            except ba.AgentError as e:
                out["floor"] = str(e)
            finally:
                hi.free_bytes = orig_free
            out["floor_left"] = sorted(x.name for x in pathlib.Path(td).iterdir())
            hi_floor, floor_orig = hi.disk_floor, hi.disk_floor
            hi.disk_floor = lambda *a, **k: 0            # the rest are not about the floor
            try:
                await ba._download("/slow", dest, idle_timeout=5, total_timeout=1.0)
                out["total"] = "saved"
            except ba.AgentError as e:
                out["total"] = str(e)
            ro = pathlib.Path(td) / "ro"
            ro.mkdir()
            ro.chmod(0o500)
            try:
                await ba._download("/fast", ro / "b.csv")
                out["oserror"] = "saved" if os.geteuid() != 0 else "root"
            except ba.AgentError as e:
                out["oserror"] = str(e)
            except OSError as e:
                out["oserror"] = f"raw {e!r}"
            ro.chmod(0o700)
            out["ok"] = await ba._download("/fast", dest)
            hi.disk_floor = floor_orig
        await runner.cleanup()
        return out
    r = asyncio.run(dl_cases())
    check("a download that would take the disk under its floor is refused in a sentence, "
          "leaving nothing behind",
          "free on the disk" in str(r.get("floor")) and r.get("floor_left") == [], r)
    check("a download still trickling past its TOTAL limit ends, saying so",
          "still arriving after" in str(r.get("total")), r.get("total"))
    check("a write that fails (no permission) is an AgentError that says so, never a raw OSError",
          r.get("oserror") == "root" or "could not save the download" in str(r.get("oserror")),
          r.get("oserror"))
    check("...and an ordinary download still saves", r.get("ok") == 1 << 20, r.get("ok"))

    # -- json.loads holds the GIL: the caps that bound it
    from server.applications import browser_profile as bp
    from server.routes import browser_forms as bf
    check("the value budget keeps a parse near 50 ms (JSON_MAX_NODES <= 200,000), and the "
          "browser profile reads at most 16 MiB",
          hi.JSON_MAX_NODES <= 200_000 and bp._PROFILE_JSON_MAX <= 16 << 20
          and bf._FORMS_JSON_MAX <= 16 << 20)


def main() -> int:
    for step in (http_checks, dispatcher_checks, git_rule_parity_checks, role_checks,
                 handler_checks, proxy_checks, proxy_wire_checks, collect_out_checks,
                 host_git_checks, export_limits_checks, ws_checks, propose_commit_checks,
                 sparse_cancel_derived_checks, enumeration_checks, prescan_parity_checks,
                 review_items_checks, review_763_checks, measured_checks,
                 env_and_image_checks):
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - an unfixed tree reads as FAIL, not a traceback
            check(f"{step.__name__} ran to completion", False, repr(exc)[:200])
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _on_stop)
    signal.signal(signal.SIGINT, _on_stop)
    sys.exit(main())
