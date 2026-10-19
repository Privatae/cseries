#!/usr/bin/env python3
"""A run's directory inside the harness pod carries its SESSION.

Measured on one Host. Four review sessions each dispatched one
`codex` variant. There is one harness Deployment per (entity, harness), so all
four ran on the same pod — and _run_on_pod named the in-pod directory after
the VARIANT (`worktree.name`), so all four were `/var/lib/cseries/projects/codex`.
/workspace/load rmtree's the directory it is given before extracting
(cli/cseries_sidecar._workspace_load), so sessions B, C and D each emptied the
directory A was building in, four minutes into its run. The host worktrees
were correctly distinct (<session>/work/<vid>); the identity was dropped at
the pod boundary.

What this pins:
  * two sessions on the same variant get two different pod directories;
  * the same session and variant is stable across a retry (the re-dispatch
    must land in the directory it already cloned into);
  * a caller with no session keeps the bare variant name — the pre-fix shape,
    unchanged for anything that never had a session;
  * the name the host derives is one the sidecar's own path guard ACCEPTS
    (_safe_ws_path) and resolves INSIDE the workspace root — a name the pod
    refused would fail every clone-in, so the contract is checked here
    against the real guard, not assumed;
  * _run_on_pod actually uses it.

Container-free.

Run: APP_ROOT=<repo> python3 scripts/test_pod_dir_per_session.py
"""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.routes import review as rv  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


SESS_A = pathlib.Path("/var/lib/cseries/review-sessions/f121f4ded9c74ac7825def192f5e1b52")
SESS_B = pathlib.Path("/var/lib/cseries/review-sessions/bdc74a7add434f25bd31e2ed7f5940b6")
WT_A = SESS_A / "work" / "codex"
WT_B = SESS_B / "work" / "codex"

print("== the incident: two sessions, one variant, one pod ==")
a = rv._pod_dir_name(SESS_A, WT_A)
b = rv._pod_dir_name(SESS_B, WT_B)
check("two sessions on `codex` get two pod directories", a != b, f"{a} == {b}")
check("...each carrying its own session", a.startswith(SESS_A.name) and b.startswith(SESS_B.name), (a, b))
check("...and the variant, so the pod-side path still reads as the run",
      a.endswith("-codex") and b.endswith("-codex"), (a, b))

print("== a retry lands where it cloned ==")
check("same session + variant is stable", rv._pod_dir_name(SESS_A, WT_A) == a)

print("== sampled variants of one session stay apart ==")
s2 = rv._pod_dir_name(SESS_A, SESS_A / "work" / "codex-s2")
check("codex and codex-s2 in one session differ", s2 != a and s2.endswith("-codex-s2"), s2)

print("== no session: the pre-fix shape, unchanged ==")
check("no session -> bare variant name", rv._pod_dir_name(None, WT_A) == "codex")

print("== the sidecar accepts what the host derives ==")
# The real guard, with a real workspace root: a name it refuses would 400
# every clone-in, and a name it resolves OUTSIDE the root would be a traversal.
from cli import cseries_sidecar as sc  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    sc.WORKSPACE_ROOT = tmp
    root = os.path.realpath(tmp)
    for name in (a, b, s2):
        try:
            resolved = sc._safe_ws_path(name)
            inside = resolved.startswith(root + os.sep) and resolved != root
            check(f"_safe_ws_path accepts {name!r} inside the root", inside, resolved)
        except ValueError as exc:
            check(f"_safe_ws_path accepts {name!r}", False, str(exc))
    # Traversal is unexpressible BY CONSTRUCTION: both halves come from
    # Path.name, which never contains a separator, so the derived name is
    # always one path component. (`Path("/x/..").name` is the literal "..",
    # and "..-codex" is an ordinary directory name, not a traversal — the
    # guard rightly accepts it.)
    crafted = rv._pod_dir_name(pathlib.Path("/x/.."), pathlib.Path("/y/../codex"))
    check("a derived name is always ONE path component",
          os.sep not in crafted and crafted not in ("", ".", ".."), crafted)
    # And the guard is still a guard for a name that IS a traversal.
    try:
        sc._safe_ws_path("../codex")
        traversal_refused = False
    except ValueError:
        traversal_refused = True
    check("_safe_ws_path still refuses a real traversal", traversal_refused)

print("== a run's pod directory is emptied once it has been collected (review N6) ==")
# Session-scoped names ended the collision that was also the cleanup; with
# holders deferring the pod's release, N sessions would leave N trees on the
# pod until the last one let go. _drop_pod_dir sends /workspace/load an EMPTY
# tar — the sidecar's own "replace with this" — so the directory is left
# holding nothing. Driven through the REAL route handler.
import io  # noqa: E402
import tarfile  # noqa: E402


class _Req:
    def __init__(self, query, body):
        self.query, self.body = query, body


with tempfile.TemporaryDirectory() as tmp:
    sc.WORKSPACE_ROOT = tmp
    name = rv._pod_dir_name(SESS_A, WT_A)
    # A run's tree, as a real clone-in would leave it.
    seed = io.BytesIO()
    with tarfile.open(fileobj=seed, mode="w") as tf:
        data = b"<!doctype html>"
        ti = tarfile.TarInfo("index.html"); ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
        ti = tarfile.TarInfo("src/app.js"); ti.size = 2
        tf.addfile(ti, io.BytesIO(b"ok"))
    out = sc._workspace_load(_Req(f"dir={name}", seed.getvalue()))
    dest = os.path.join(tmp, name)
    check("the seeded tree landed (2 files)", isinstance(out, dict) and out.get("files") == 2, out)
    # Now what _drop_pod_dir sends.
    empty = rv._empty_workspace_tar()
    check("the drop payload is a valid tar with no members",
          len(empty) > 0 and tarfile.open(fileobj=io.BytesIO(empty)).getmembers() == [])
    out = sc._workspace_load(_Req(f"dir={name}", empty))
    check("the sidecar accepts it (200, 0 files)", isinstance(out, dict) and out.get("ok") is True
          and out.get("files") == 0, out)
    check("...and the run's directory is now empty", os.path.isdir(dest) and os.listdir(dest) == [],
          os.listdir(dest) if os.path.isdir(dest) else "missing")

print("== a pod-connection failure still reports itself (third pass NC1) ==")
# `collected` is stamped on the result after the outer try/except; the
# outer except is where every pod-connection failure lands. An initialiser
# placed inside the try left that path raising UnboundLocalError in place of
# the connection message it exists to carry. Drive the real _run_on_pod with
# the first awaited call in the try raising, everything after it stubbed.
import asyncio  # noqa: E402


async def _raise_routable(http, base, harness_id):
    raise ConnectionRefusedError("[Errno 111] Connect call failed ('fd12:3456:ff::df6b', 8840)")


async def _noop(*a, **k):
    return None


rv._await_routable = _raise_routable
rv._persist_rotated_creds = _noop
rv._maybe_cache_model_catalog = _noop
with tempfile.TemporaryDirectory() as tmp:
    wt = pathlib.Path(tmp) / "work" / "codex"
    wt.mkdir(parents=True)
    try:
        res = asyncio.new_event_loop().run_until_complete(rv._run_on_pod(
            None, "http://[fd12:3456:ff::df6b]:8840", "codex", "codex", "build it",
            wt, 15, 0.0, session_dir=None))
        raised = None
    except Exception as exc:  # noqa: BLE001
        res, raised = None, exc
    check("the run returns an envelope instead of raising", raised is None, repr(raised))
    check("...marked not collected", isinstance(res, dict) and res.get("collected") is False,
          res and res.get("collected"))
    # The message is the connection failure itself — either the transient
    # wording ("pod connection failed after …") or the raw OS error the
    # non-transient branch relays — and never the Python error.
    check("...carrying the pod-connection message, not a Python error",
          isinstance(res, dict) and res.get("ok") is False
          and ("pod connection failed" in (res.get("error") or "")
               or "Connect call failed" in (res.get("error") or ""))
          and "collected" not in (res.get("error") or ""),
          res and res.get("error"))

print("== every consumer of the pod directory agrees on the name ==")
src = (ROOT / "server/routes/review.py").read_text()
check("_run_on_pod derives dir_name through _pod_dir_name",
      "dir_name = _pod_dir_name(session_dir, worktree)" in src)
check("the bare `dir_name = worktree.name` is gone",
      "dir_name = worktree.name" not in src)
# The acceptance runner spelled the pre-fix path as an f-string, which the
# check above passed over — and every phase with declared checks lost its
# verdict to "project_dir missing". Both spellings.
check("no consumer spells the pre-fix path `projects/{worktree.name}`",
      "projects/{worktree.name}" not in src)
check("the acceptance runner uses the session-scoped name",
      'in_container_dir = f"/var/lib/cseries/projects/{_pod_dir_name(session_dir, worktree)}"' in src)
check("the batch path drops the directory after collect-out; the single build keeps it for acceptance and drops it itself",
      "if not keep_dir:\n                await _drop_pod_dir(http, base, dir_name)" in src
      and "keep_dir=True, host_rediffs=host_rediffs)" in src
      and "await _drop_pod_dir(http, wake[\"url\"], _pod_dir_name(session_dir, worktree))" in src)

print()
if FAILURES:
    print(f"FAIL {len(FAILURES)}: {FAILURES}")
    sys.exit(1)
print("all green")
