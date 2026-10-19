#!/usr/bin/env python3
"""An app runtime the plan cannot hold is a REFUSAL the user hears about.

Found by review on 2026-09-04, on the seam between the operator's new
install-time plan gate and the kernel: the operator answered 402 with
needs_mi / headroom_mi / upgrade_to, and `sync_app_runtime` dropped it on the
floor — `bc.request()` collapses every not-ok envelope to None, the return
value was discarded, and `install_bundle` reported success. The result would
have been a registry row for an app the operator holds no reserve for: a
successful-looking install of a permanently dead app, and a machine total
counting memory nothing was actually paying for.

What this pins:
  * a 402 envelope raises AppRuntimeOverPlan, carrying the operator's numbers;
  * every OTHER not-ok envelope stays best-effort (returns False) — "the
    bridge was down" is a retry, "this will never fit" is an answer;
  * the operator's payload survives the bridge's string flattening;
  * the install path rolls back and re-raises rather than keeping the row;
  * every caller of install_bundle has an arm for it, so a refusal never
    surfaces to a user as "crashed" or as a traceback.

Container-free: server.applications.installer imports clean, and the bridge
is a stub object with the one method under test.

Run: python3 scripts/test_app_rt_over_plan.py
"""
import ast
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.applications import installer  # noqa: E402
from server.applications.installer import (  # noqa: E402
    AppRuntimeOverPlan, _over_plan_detail, sync_app_runtime,
)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# The operator's real 402 body, as FastAPI serializes an HTTPException whose
# detail is a dict, wrapped the way the bridge flattens it (adapter
# op_workspace_set_app_runtime: f"operator /set-app-runtime {status}: {text}").
_DETAIL = ('{"detail":{"error":"capability_over_plan","harness":'
           '"trading-bot","needs_mi":160,"headroom_mi":32,'
           '"upgrade_to":"solo"}}')
# The bridge flattens the operator's body and CUTS IT AT 200 CHARS
# (adapter.op_workspace_set_app_runtime). That cap is why the operator sends
# numbers and no sentence: with a message the body measured 238 bytes and the
# numbers were sliced off mid-JSON, so this exception carried None for all of
# them and the UI rendered "upgrade to null".
_BRIDGE_ERR = f"operator /set-app-runtime 402: {_DETAIL}"[:200]


class _BC:
    """A connected bridge that answers one canned envelope."""

    _connected = True

    def __init__(self, envelope):
        self.envelope = envelope
        self.calls = []

    async def request_full(self, op, params=None):
        self.calls.append((op, params))
        return self.envelope

    async def request(self, op, params=None, **kw):     # must NOT be used
        raise AssertionError("sync_app_runtime must use request_full — "
                             "request() collapses a 402 to None")


print("the operator's payload survives the bridge")
d = _over_plan_detail(_BRIDGE_ERR)
check("parsed to a dict", isinstance(d, dict), type(d).__name__)
check("carries the numbers",
      isinstance(d, dict) and d.get("needs_mi") == 160
      and d.get("headroom_mi") == 32 and d.get("upgrade_to") == "solo")
check("the operator's body survives the bridge's 200-char cap",
      len(f"operator /set-app-runtime 402: {_DETAIL}") <= 200,
      f"{len(_DETAIL)}B of detail")
check("an unparseable error is passed through, never dropped",
      _over_plan_detail("operator unreachable: boom") == "operator unreachable: boom")
check("a body with no json is passed through",
      _over_plan_detail("plain failure") == "plain failure")


print("a 402 raises; anything else stays best-effort")
bc = _BC({"ok": False, "code": 402, "error": _BRIDGE_ERR})
try:
    run(sync_app_runtime("trading-bot", False, bc=bc))
    check("402 raises AppRuntimeOverPlan", False, "no exception")
except AppRuntimeOverPlan as e:
    check("402 raises AppRuntimeOverPlan", True)
    check("the exception carries needs_mi", e.needs_mi == 160)
    check("...headroom_mi", e.headroom_mi == 32)
    check("...and the tier that fits", e.upgrade_to == "solo")
    check("the sentence is composed HERE, from those numbers",
          "160MiB" in str(e) and "32MiB" in str(e) and "solo has room" in str(e))

bc = _BC({"ok": False, "code": 500, "error": "operator unreachable: timeout"})
check("a transport failure is still best-effort (False, no raise)",
      run(sync_app_runtime("trading-bot", False, bc=bc)) is False)

bc = _BC({"ok": False, "code": 400, "error": "descriptor budget 128Mi != signed 64Mi"})
check("a 400 refusal is still best-effort",
      run(sync_app_runtime("trading-bot", False, bc=bc)) is False)

bc = _BC({"ok": True, "data": {}})
check("the happy path still returns True",
      run(sync_app_runtime("trading-bot", False, bc=bc)) is True)

bc = _BC({"ok": False, "code": 402, "error": _BRIDGE_ERR})
try:
    run(sync_app_runtime("trading-bot", False, bc=bc))
except AppRuntimeOverPlan:
    pass
check("it went through request_full, not request",
      bc.calls and bc.calls[0][0] == "workspace.set_app_runtime")


print("the install path rolls back instead of keeping the row")
_src = (ROOT / "server" / "applications" / "installer.py").read_text()
_tree = ast.parse(_src)
_fn = next(n for n in ast.walk(_tree)
           if isinstance(n, ast.AsyncFunctionDef) and n.name == "install_bundle")
_handlers = [h for h in ast.walk(_fn) if isinstance(h, ast.ExceptHandler)
             and isinstance(h.type, ast.Name) and h.type.id == "AppRuntimeOverPlan"]
check("install_bundle handles AppRuntimeOverPlan", len(_handlers) == 1,
      f"{len(_handlers)} handlers")
if _handlers:
    _body = ast.dump(_handlers[0])
    check("...by uninstalling what it just wrote", "uninstall_app" in _body)
    check("...and re-raising, so the caller can surface the upsell",
          any(isinstance(n, ast.Raise) and n.exc is None
              for n in ast.walk(_handlers[0])))

print("every install_bundle caller has an arm for it")
# A refusal that reaches a user as "install crashed" is the same bug in a
# different costume, so the callers are pinned too.
for rel, must in (
        ("server/routes/applications.py", "HTTP_402_PAYMENT_REQUIRED"),
        ("server/applications/default_install.py", "over plan"),
        ("brain/mastercode/runtime/tools.py", "over_plan"),
):
    text = (ROOT / rel).read_text()
    check(f"{rel} names the refusal",
          "AppRuntimeOverPlan" in text and must in text)

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} — {', '.join(FAILURES)}")
    sys.exit(1)
print("all over-plan checks passed")
