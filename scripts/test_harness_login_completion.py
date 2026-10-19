#!/usr/bin/env python3
"""A vendor login only counts when the vendor wrote a NEW grant.

`claude auth status` is presence-based, not validity-based — measured against
the bundled binary (2.1.220) in the pinned harness image:

    CLAUDE_CODE_OAUTH_TOKEN=sk-fake ... claude auth status
      -> {"loggedIn": true,  "authMethod": "oauth_token"}
    env -u CLAUDE_CODE_OAUTH_TOKEN  claude auth status
      -> {"loggedIn": false, "authMethod": "none"}

It also answers loggedIn=true for a creds file whose token expired long ago. So
a status-only completion check reports success for two logins that never
happened, and the reconnect path is the common one: an EXPIRED harness wakes
seeded with the entity's stale creds, and the flow would re-persist that dead
grant while telling the user they are connected.

This exercises the real _login_completed / _vendor_env against a stubbed vendor
so both cases stay closed.

Run in-image: docker run --entrypoint python3 :latest scripts/test_harness_login_completion.py
"""
import sys, types, hashlib, json, importlib.util
from pathlib import Path
import tempfile, os

# Stub the sidecar deps so main.py imports outside the image.
for name, attrs in {
    "cseries_sidecar": {"sidecar": types.SimpleNamespace(handler=lambda *a, **k: (lambda f: f)),
                      "worktree_diff": lambda *a: "", "WORKSPACE_ROOT": Path("/tmp")},
}.items():
    m = types.ModuleType(name); [setattr(m, k, v) for k, v in attrs.items()]; sys.modules[name] = m
hr = types.ModuleType("_harness_runtime"); hr.__path__ = []
cc = types.ModuleType("_harness_runtime.claude_creds")
cc.read_local_creds = lambda p: {}
cc.get_local_token = lambda **k: None
hr.claude_creds = cc
sys.modules["_harness_runtime"] = hr; sys.modules["_harness_runtime.claude_creds"] = cc
# harness_core is stubbed for the same reason claude_creds is. main.py imports
# `bound_core_dumps` at module scope and CALLS it on import, and the
# real one is not a pure function: it setrlimit()s RLIMIT_CORE soft AND hard on
# whatever process imports main.py — which here is the smoke runner on the build
# box, not a harness sidecar. `hr.__path__ = []` above means an unstubbed
# submodule cannot resolve at all, so without this the gate dies at import with
# ModuleNotFoundError and reports red for a login path it never reached (that is
# exactly how it once failed on a ship). The rlimit call has nothing to do
# with what this gate pins, so it is stubbed rather than executed.
hc = types.ModuleType("_harness_runtime.harness_core")
hc.bound_core_dumps = lambda *a, **k: None
# adopt_nac_egress_lane joined the sidecar's import list (the harness
# egress chooser). Stubbed for the same reason as bound_core_dumps: main.py calls
# it at module scope, it would try to reach a NAC gate that does not exist on the
# build host, and what it does has nothing to do with the login path this gate
# pins. scripts/test_harness_nac_lane.py is what covers its behaviour.
hc.adopt_nac_egress_lane = lambda *a, **k: None
# make_stop joined the import list (POST /stop). Stubbed with a
# handler that signals NOTHING: this gate is about login, and the real
# stop_everything kills processes — its own gate (test_harness_stop.py) runs its
# real-signal checks only inside a private PID namespace.
hc.make_stop = lambda *a, **k: (lambda request: {"ok": True, "stopped": [], "resumed": []})
# _idle_poll_s joined the import list (/run/stream's quiet-run
# watchdog, no total cap). Pure, and only read while a streamed run is live —
# never on the login path — so a stand-in with the same shape is enough.
hc._idle_poll_s = lambda idle_s: max(0.25, min(5.0, idle_s / 4))
# _proc_ppids / _descends_from joined it for the same watchdog (an idle stop
# kills that run's process tree). Stubbed to see and match NOTHING: this gate
# must never be able to signal a process.
hc._proc_ppids = lambda: {}
hc._descends_from = lambda ppids, pid, ancestor: False
hc._in_harness_container = lambda: False
hc._container_recheck = lambda: False
# sample_pids / tree_was_active joined it (a run's
# CPU and bytes count as life). Only read while a streamed run is live, never
# on the login path; stubbed to read nothing.
hc.sample_pids = lambda *a, **k: None
hc.tree_was_active = lambda *a, **k: None
hc.ActivityGate = type("ActivityGate", (), {"feed": lambda self, *a, **k: False})
# marker_pids / worktree_signature joined it with the progress backstop:
# read only while a run is live; stubbed to read nothing.
hc.marker_pids = lambda *a, **k: set()
hc.worktree_signature = lambda *a, **k: None
# The single-run fallback: read and signalled only while
# a run is live; stubbed to see and signal NOTHING.
import threading as _thr  # noqa: E402
hc._LIVE_ATTEMPTS = set()
hc._LIVE_LOCK = _thr.Lock()
hc._pod_strays = lambda *a, **k: set()
hc._pids_now = lambda *a, **k: set()
hc._kill_pids = lambda *a, **k: None
hc.requested_step_seconds = lambda *a, **k: 0.0
# Module-level CONSTANTS are lifted from the real harness_core source instead of
# being mirrored here. A change once added
# `RUN_RETRY_LIMIT, RUN_RETRY_MIN_BUDGET_S, _RETRYABLE_STDERR` to the sidecar's
# import list; this stub still carried only bound_core_dumps, so the gate died
# at import with "cannot import name 'RUN_RETRY_LIMIT'" and red-failed a login
# path it never reached — the same failure shape, one layer up. Hardcoding
# the three values here would just re-arm the same trap on the next constant.
# So: parse the real module, exec ONLY its simple module-level assignments (an
# int, a float, a compiled regex — no imports, no function bodies, and never
# bound_core_dumps, whose setrlimit side effect is the reason this stub exists),
# and bind whatever they define. A constant added upstream resolves for free; a
# name the sidecar imports that is NOT a constant still fails loudly, which is
# correct — that is a real contract change worth a red gate.
def _lift_harness_core_constants(target) -> None:
    import ast as _ast
    src_path = (Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
                / "system-apps/_harness_runtime/harness_core.py")
    try:
        tree = _ast.parse(src_path.read_text())
    except (OSError, SyntaxError) as exc:      # missing/broken source is the gate's problem
        print(f"WARN: could not read {src_path}: {exc}")
        return
    ns: dict = {"os": os, "re": __import__("re"), "Path": Path}
    for node in tree.body:
        if not isinstance(node, _ast.Assign) or len(node.targets) != 1:
            continue
        tgt = node.targets[0]
        if not isinstance(tgt, _ast.Name):
            continue
        try:
            exec(compile(_ast.Module(body=[node], type_ignores=[]), str(src_path), "exec"), ns)
        except Exception:
            continue                            # depends on something we don't stub — skip it
        if tgt.id in ns:
            setattr(target, tgt.id, ns[tgt.id])


_lift_harness_core_constants(hc)
hr.harness_core = hc
sys.modules["_harness_runtime.harness_core"] = hc
# host_mcp is loaded FOR REAL, not stubbed. main.py gained
# `from _harness_runtime import host_mcp` and calls host_mcp.mcp_servers()
# inside _sdk_options; `hr.__path__ = []` above means an unregistered submodule
# cannot resolve, so the gate died at import with "cannot import name
# 'host_mcp'" — the same shape as the two failures documented above. It is
# stubbed nowhere because it does not need to be: the module imports only
# stdlib (json, logging, os, sys, pathlib) and does nothing at import time, so
# the real contract is what this gate should see. With neither
# CSERIES_ENTITY_MCP_URL nor CSERIES_HARNESS_CAP set here, enabled() is False and
# mcp_servers() returns {} — the "no channel" path, which is what a build box
# genuinely is.
def _load_real_host_mcp(package):
    src = (Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
           / "system-apps/_harness_runtime/host_mcp.py")
    spec = importlib.util.spec_from_file_location("_harness_runtime.host_mcp", str(src))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_harness_runtime.host_mcp"] = mod
    spec.loader.exec_module(mod)
    setattr(package, "host_mcp", mod)          # `from _harness_runtime import host_mcp`
    return mod


_load_real_host_mcp(hr)


# measure is loaded FOR REAL too, for the same reason as host_mcp:
# main.py gained `from _harness_runtime.measure import RunMeasure`, the stubbed
# package cannot resolve it, and the module is stdlib-only with nothing done
# at import time.
def _load_real_measure(package):
    src = (Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
           / "system-apps/_harness_runtime/measure.py")
    spec = importlib.util.spec_from_file_location("_harness_runtime.measure", str(src))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_harness_runtime.measure"] = mod
    spec.loader.exec_module(mod)
    setattr(package, "measure", mod)
    return mod


_load_real_measure(hr)
sys.modules["claude_agent_sdk"] = types.ModuleType("claude_agent_sdk")

# Resolve from APP_ROOT (how smoke invokes it) or from this file's own
# location, never the cwd — the smoke gate runs it from an arbitrary directory.
ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
MAIN = ROOT / "system-apps/claude-code-harness/bin/main.py"
if not MAIN.exists():
    print(f"FAIL cannot find {MAIN}"); raise SystemExit(1)
spec = importlib.util.spec_from_file_location("hmain", str(MAIN))
m = importlib.util.module_from_spec(spec); sys.modules["hmain"] = m
try:
    spec.loader.exec_module(m)
except Exception as e:
    print("FAIL import:", type(e).__name__, e); raise SystemExit(1)

FAILS = []
def check(n, ok, d=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {n}" + (f" — {d}" if d else ""))
    (FAILS.append(n) if not ok else None)

tmp = Path(tempfile.mkdtemp()) / "creds.json"
m.LOCAL_CREDS_PATH = tmp

# --- 1. the env-token leak, as measured
os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "fake-token-from-a-previous-run"
env = m._vendor_env()
check("CLAUDE_CODE_OAUTH_TOKEN is popped from the vendor env",
      "CLAUDE_CODE_OAUTH_TOKEN" not in env)
check("HOME is set for the vendor", env.get("HOME") == "/home/privatae")

# --- 2. expired-reconnect: pod woken WITH creds, vendor says loggedIn, file unchanged
tmp.write_text(json.dumps({"claudeAiOauth": {"accessToken": "old", "expiresAt": 1}}))
before = m._creds_fingerprint()
m._vendor_auth_state = lambda: {"loggedIn": True, "authMethod": "oauth_token"}
done, _ = m._login_completed(before)
check("expired reconnect: vendor says loggedIn but file UNCHANGED -> not done",
      done is False, "this is the case that would re-persist a dead grant")

# --- 3. a real login: the vendor writes a NEW grant
tmp.write_text(json.dumps({"claudeAiOauth": {"accessToken": "brand-new", "expiresAt": 9}}))
done, _ = m._login_completed(before)
check("real login: file CHANGED and vendor says loggedIn -> done", done is True)

# --- 4. file changed but vendor says NOT logged in
m._vendor_auth_state = lambda: {"loggedIn": False}
done, _ = m._login_completed(before)
check("file changed but vendor not signed in -> not done", done is False)

# --- 5. no creds at all
tmp.unlink()
m._vendor_auth_state = lambda: {"loggedIn": True}
done, _ = m._login_completed(None)
check("no creds file -> not done", done is False)

print("PASS 6/6 login-completion checks" if not FAILS
      else f"FAIL {len(FAILS)}: {FAILS}")
sys.exit(1 if FAILS else 0)
