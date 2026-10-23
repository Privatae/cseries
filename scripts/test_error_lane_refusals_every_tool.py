#!/usr/bin/env python3
"""Every tool's refusal of the model's own input is the MODEL's outcome on the
error lane, not a platform fault — the class, not one tool (a live end-to-end
test's errors_clean, on both Hosts).

Container-free: APP_ROOT=<repo> python3 scripts/test_error_lane_refusals_every_tool.py
No pod, no DB, no network, no model: a temp workspace and the real executors.

One live run failed errors_clean
with 12 platform rows on entity 9163 and 5 on 9164. An earlier fix had marked
workspace_file alone; every one of the 17 was another tool refusing what the
model sent (rows read from the two test Hosts' error lane):

  operate -> manage_site   {op: publish, project_id: 2}              slug is required for op='publish'
  operate -> manage_site   {op: publish, slug: canary-app}           bundle_path is required for op='publish'
  operate -> manage_site   {..., bundle_path: canary-app-bundle}     publish failed reading bundle: file not found
  operate -> manage_site   {..., bundle_path: /var/lib/cseries/...}    publish failed reading bundle: path traversal blocked
  operate -> publish_app   {project_id: 2}                           op must be one of [...] (got '').
  operate -> project_phase {op: set_phase_spec, phase_id: 1, ...}    project_id (int) required
  build                    {action: build, project_id: 2, phase_id: 1}  phase 1 ... is 'planning', not ready to build

  1 those calls, through the real executors, are refused with the run's own
    words AND filed 'model' (the build refusal is covered by 3: it needs a DB)
  2 a failure of the tool itself stays 'platform': a publish whose bundle IS
    there but the licence check has no database, and the bridge not connected
  3 THE CLASS: every `{"status": "error", ...}` a model-callable module returns
    outside a broad (Exception / bare) handler has been sorted — refused() for the model's
    input or a precondition it can see and fix, a plain dict for the
    platform's. The unsorted count per module is pinned; a new site raises it
    and fails here until whoever adds it decides whose it is.

Exit 0 = all pass; exit 1 + FAIL lines otherwise.
"""

import ast
import asyncio
import logging
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from turn_source import TURN_FILES  # session.py and the modules split out of it
from tools_source import tools_paths  # tools.py and the modules split out of it

APP_ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_ROOT)
sys.path.insert(0, os.path.join(APP_ROOT, "brain"))
logging.basicConfig(level=logging.CRITICAL)

_WS = tempfile.mkdtemp(prefix="ws-refusals-every-tool-")
os.environ["CEIGAS_WORKSPACE"] = _WS          # read by file_access at import

FAILS: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"[refusals-every-tool] FAIL {name} {extra}")


from mastercode.runtime import tool_call_ledger as L  # noqa: E402
from mastercode.runtime import tools as T  # noqa: E402
from server.capabilities import file_access as FA  # noqa: E402

FA.WORKSPACE = Path(_WS)


def lane_fault(res) -> str:
    """The fault the error lane files for this result (session._error_lane)."""
    return L.fault_class(L.classify(res), res)


# ── 1. the run's calls, through the real executors ─────────────────────────
print("1 the run's refusals, through the real executors")
RUN = [
    (T._exec_manage_site, {"op": "publish", "project_id": 2},
     "slug is required for op='publish'"),
    (T._exec_manage_site, {"op": "publish", "slug": "canary-app"},
     "bundle_path is required for op='publish'"),
    (T._exec_manage_site, {"op": "publish", "project_id": 2, "slug": "canary-app",
                           "bundle_path": "canary-app-bundle"},
     "publish failed reading bundle: file not found"),
    (T._exec_manage_site, {"op": "publish", "project_id": 2, "slug": "canary-app",
                           "bundle_path": "/var/lib/cseries/projects/factory-build-7-opencode/canary-app-bundle.zip"},
     "publish failed reading bundle: path traversal blocked"),
    (T._exec_publish_app, {"project_id": 2},
     "op must be one of"),
]
for fn, args, said in RUN:
    res = asyncio.run(fn(dict(args)))
    label = f"{fn.__name__} {args}"
    check(f"1: {label} is refused with the run's words ({said!r})",
          isinstance(res, dict) and res.get("status") == "error" and said in str(res.get("result")), res)
    check(f"1: {label} is filed as the model's outcome", lane_fault(res) == "model",
          f"fault={lane_fault(res)} res={res}")

# project_phase and build refuse a missing project_id before they touch the
# pool, so None stands in for it: a refusal that reached the pool would raise.
res = asyncio.run(T._exec_project_phase(
    {"op": "set_phase_spec", "phase_id": 1, "spec": "This phase will prepare the canary app."}, 163, None))
check("1: project_phase with no project_id is refused, filed 'model'",
      res.get("result") == "project_id (int) required" and lane_fault(res) == "model", res)
res = asyncio.run(T._exec_build({"action": "build", "phase_id": 1}, 163, None, None))
check("1: build with no project_id is refused, filed 'model'",
      res.get("result") == "project_id (int) required" and lane_fault(res) == "model", res)

# ── 2. the tool's own failures stay the platform's ─────────────────────────
print("2 a failure of the tool itself stays 'platform'")
with zipfile.ZipFile(os.path.join(_WS, "real-bundle.zip"), "w") as z:
    z.writestr("index.html", "<h1>here</h1>")
import server.brain_service as BS  # noqa: E402
_real_get_pool = getattr(BS, "get_pool", None)
BS.get_pool = lambda: None
try:
    res = asyncio.run(T._exec_manage_site(
        {"op": "publish", "slug": "canary-app", "bundle_path": "real-bundle.zip"}))
finally:
    if _real_get_pool is not None:
        BS.get_pool = _real_get_pool
check("2: a publish whose bundle is there but whose licence check has no database is refused",
      res.get("status") == "error" and "no database" in str(res.get("result")), res)
check("2: ...and filed 'platform' (the model sent nothing wrong)", lane_fault(res) == "platform", res)
check("2: an unmarked error is 'platform'",
      lane_fault({"status": "error", "result": "bridge not connected"}) == "platform")
check("2: refused() carries the mark fault_class reads, and nothing else changes",
      L.refused("x", harness="h") == {"status": "error", "result": "x", "harness": "h",
                                       L.INPUT_REFUSED: True})

# Typed errors: the real classes. Ours with a 4xx, our own ValueError
# subclasses and classes marked caller_refusal are the caller's; a 5xx, a 429,
# no status, a builtin ValueError, an opted-out class, or a DEPENDENCY's own
# error with a 4xx are the platform's.
import urllib.error  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from server.routes.applications import ShipError, _StoreError  # noqa: E402
from server.applications.supervisor import SupervisorError  # noqa: E402
from server.applications.manifest import ManifestError as _ManifestError  # noqa: E402
from server.applications.installer import (  # noqa: E402
    InstallError as _InstallError, EntitlementUnverifiable as _EntitlementUnverifiable)
from server.harness_input import LargeRecordError as _LargeRecordError  # noqa: E402

for err, want in ((ShipError(400, "this bundle declares no app id"), "model"),
                  (ShipError(403, "publish refused (licence)"), "model"),
                  (HTTPException(400, "files must be a non-empty list of {path, content, op}"), "model"),
                  (SupervisorError("not installed: canary-app", status=404), "model"),
                  (_StoreError("bundle fetch 404", status=404), "model"),
                  (ShipError(429, "held off"), "platform"),
                  (ShipError(503, "bridge not connected"), "platform"),
                  (SupervisorError("spawn failed: boom"), "platform"),
                  (_StoreError("store unreachable: timeout"), "platform"),
                  (urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, None), "platform"),
                  (RuntimeError("no status"), "platform"),
                  (ValueError("int() of an internal value"), "platform"),
                  (__import__("json").JSONDecodeError("bad", "x", 0), "platform"),
                  (_ManifestError("app.yaml: missing required field(s): ['id']"), "model"),
                  (_InstallError("bundle rejected: bad signature"), "model"),
                  (_EntitlementUnverifiable("bridge unreachable"), "platform"),
                  (_LargeRecordError("invalid JSON value"), "platform")):
    res = L.refused_if_caller(err, f"failed: {err}", harness="h")
    check(f"2: {type(err).__name__} {getattr(err, 'status', getattr(err, 'status_code', None))} "
          f"is filed {want!r}, its words unchanged",
          lane_fault(res) == want and res["result"] == f"failed: {err}" and res["harness"] == "h", res)

# A later live test run (both Hosts): three more refusals
# that reached the tool as an exception through a broad handler.
print("2b the later run's refusals, through the real executors")
import server.applications.supervisor as SUP  # noqa: E402


class _Conn:
    async def fetchrow(self, q, *a):
        return None                       # no installed_applications row


class _Acq:
    async def __aenter__(self):
        return _Conn()

    async def __aexit__(self, *a):
        return False


class _Pool:
    def acquire(self):
        return _Acq()


_real_sup, _real_pool = SUP.get_supervisor(), getattr(BS, "get_pool", None)
SUP.set_supervisor(SUP.Supervisor())
BS.get_pool = lambda: _Pool()
try:
    res = asyncio.run(T._exec_system_start_app({"app_id": "canary-app"}))
finally:
    SUP._SUPERVISOR = _real_sup
    if _real_pool is not None:
        BS.get_pool = _real_pool
check("2b: system.start_app on an app that is not installed is refused, filed 'model'",
      "not installed: canary-app" in str(res.get("result")) and lane_fault(res) == "model", res)

import server.routes.applications as APPS  # noqa: E402
_real_fetch = APPS._fetch_bundle_from_store


async def _store_404(app_id, version):
    raise _StoreError("bundle fetch 404", status=404)

APPS._fetch_bundle_from_store = _store_404
BS.get_pool = lambda: _Pool()
try:
    res = asyncio.run(T._exec_install_app({"app_id": "canary-app"}))
finally:
    APPS._fetch_bundle_from_store = _real_fetch
    if _real_pool is not None:
        BS.get_pool = _real_pool
check("2b: install_app of an app the store does not have (404) is refused, filed 'model'",
      "bundle fetch 404" in str(res.get("result")) and lane_fault(res) == "model", res)

# ── 3. the class: every error return in a model-callable module is sorted ──
# The gitea pull-request reads: what the forge refuses about the
# call is the model's, the forge's own failure the platform's.
for st, want in ((403, "model"), (404, "model"), (422, "model"), (429, "platform"),
                 (502, "platform"), (0, "platform")):
    _r = T._gitea_http_failure("owner/repo/pulls", st, "forge words")
    check(f"2: a gitea pull-request read answered HTTP {st} is filed {want!r}, the forge's words kept",
          lane_fault(_r) == want and "forge words" in str(_r.get("result")), _r)

print("3 every unsorted error return is pinned")
# Unsorted = a `{"status": "error", ...}` dict literal returned (or assigned)
# outside a broad (Exception / bare) handler, without the INPUT_REFUSED mark and without a
# stamped `reason`. What is left after the sort is the platform's
# own failures: no pool, the bridge not connected, a dependency's error passed
# through, a write that did not land. A new one raises the count: decide whose
# it is — refused() for the model's input or a precondition the model can see
# and fix, a plain dict for a failure of the platform — then set the number.
# tools.py 148 -> 149 (the kill): the kill's "the pod did not
# stop" answer is a platform failure; "stopped before it started" is the build
# lane's own record of a deliberate stop, written to the task row and never
# the tool call's answer. Neither is the model's input to refuse.
# tools.py 149 -> 151 (mcp_connections): "the Host's database is
# not ready" and an MCP transport failure (the egress gate refused, the network
# failed, the server timed out or answered 5xx) are the lane's own failures; the
# server's refusals of the call go through refused().
# tools.py 151 -> 152 (index every readable repo): embed_jobs
# target='gitea_repo' answers "gitea did not say which repos this host can
# read" when the forge's listing fails — the forge's failure, not the model's
# input; a repo the listing does not hold is refused().
# tools.py 152 -> 154 (machine_exec): "the pool is unavailable" and
# "no worker / no answer from the machine" are the lane's own failures; a
# missing machine or command and a command the box would not take go through
# refused(), and the machine's own refusal is status 'denied'.
# tools.py 154 -> 153 (Host parity): the gitea pull-request
# reads (pulls, pull, pull_files, pull_reviews, commit_status, branches) answer
# through _gitea_http_failure: a 4xx the forge returns for what the call
# named is refused(), the forge's or the network's failure is the platform's
# (that helper's one dict); main stood at 152 under the old pin.
# tools.py 153 -> 154 (memory_curate op='alias'): "memory pool not
# ready" is the database lane's own failure. A missing memory_id or content, an
# alias over 300 characters and a memory that is not live go through refused().
# tools.py 154 -> 155 (memory_curate op='key', memory slots): "memory pool not
# ready" is the database lane's own failure, as for op='alias'. A missing
# topic, a memory_id that is not an integer, words no live row holds and a row
# that is not a live preference or relationship note go through refused().
# tools.py 155 -> 156 (keys_manage op='revoke'): "the disconnect could not be
# recorded" is the Host's own database failing to write the marker, so nothing
# is revoked and the person is told; a missing name goes through refused().
# tools.py and every module split out of it (2026-10-23) share tools.py's pin:
# the count is their sum, so code moved between them neither frees the pin
# nor leaves a site outside it.
TOOLS_PIN = 156
PINNED = {
    # session.py and every module split out of it hold session.py's pin.
    **{"brain/mastercode/runtime/" + f: 0 for f in TURN_FILES},
    "brain/mastercode/runtime/turn_type_approval.py": 0,
    "brain/mastercode/runtime/github_lane.py": 2,
    "brain/mastercode/runtime/context_ops.py": 1,
    "brain/mastercode/runtime/link_ops.py": 0,
    "brain/mastercode/runtime/close_turn.py": 1,
    "brain/mastercode/runtime/bug_review.py": 0,
    "brain/mastercode/runtime/support_collect.py": 0,
    "brain/mastercode/runtime/ticket_import.py": 1,
    "brain/mastercode/runtime/url_reader.py": 2,
    "brain/mastercode/runtime/scoped_agent.py": 0,
    "brain/mastercode/runtime/kernel_ops.py": 1,
    "brain/mastercode/runtime/direct_ops.py": 0,
}
_BROAD = {"Exception", "BaseException"}


def _unsorted(path: str) -> list[int]:
    tree = ast.parse(Path(APP_ROOT, path).read_text())
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}

    def in_broad_handler(n) -> bool:
        # Only `except Exception` / `BaseException` / bare is the tool's own
        # failure by construction. A TYPED handler (ShipError, SpecError,
        # re.error, ValueError …) catches a named refusal and is sorted like
        # any other return: this gate once had a typed-handler blind
        # spot that hid publish_app's ShipError(400) and turn_type's SpecError.
        while n in parents:
            n = parents[n]
            if isinstance(n, ast.ExceptHandler):
                if n.type is None:
                    return True
                names = {ast.unparse(e) for e in (n.type.elts if isinstance(n.type, ast.Tuple)
                                                  else [n.type])}
                return bool(names & _BROAD)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return False
        return False

    out = []
    for n in ast.walk(tree):
        v = n.value if isinstance(n, (ast.Return, ast.Assign)) else None
        if not isinstance(v, ast.Dict):
            continue
        keys = {k.value if isinstance(k, ast.Constant) else ast.unparse(k) for k in v.keys if k is not None}
        status = dict(zip([k.value if isinstance(k, ast.Constant) else None for k in v.keys], v.values)).get("status")
        if not (isinstance(status, ast.Constant) and status.value == "error"):
            continue
        if "_INPUT_REFUSED" in keys or "input_refused" in keys or "reason" in keys:
            continue
        if in_broad_handler(n):
            continue
        out.append(n.lineno)
    return sorted(out)


for path, pinned in PINNED.items():
    got = _unsorted(path)
    check(f"3: {path}: {len(got)} unsorted error return(s), pinned {pinned}",
          len(got) <= pinned,
          f"— new unsorted site(s) among lines {got}: refused() if the model's input "
          f"or a precondition it can fix, else a plain dict and raise the pin")
    if len(got) < pinned:
        print(f"  note  {path}: {len(got)} < pinned {pinned} — lower the pin")

got = [f"{os.path.relpath(p, APP_ROOT)}:{ln}" for p in tools_paths(APP_ROOT) for ln in _unsorted(p)]
check(f"3: tools.py and its split modules: {len(got)} unsorted error return(s), pinned {TOOLS_PIN}",
      len(got) <= TOOLS_PIN,
      f"— new unsorted site(s) among {got}: refused() if the model's input "
      f"or a precondition it can fix, else a plain dict and raise the pin")
if len(got) < TOOLS_PIN:
    print(f"  note  tools.py and its split modules: {len(got)} < pinned {TOOLS_PIN} — lower the pin")

if FAILS:
    print(f"\n{len(FAILS)} FAIL")
    sys.exit(1)
print("\nall pass")
