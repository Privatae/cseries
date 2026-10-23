#!/usr/bin/env python3
"""Gate: the Lab Harness is wired the way its walls require.

Three things this pins, all pure Python (no server, no DB):

  1. WALL #2 — a mode='lab' dispatch needs the SAME signed grant to carry 'mode:lab'.
     A grant that covers harness:run + image:* for a home box must NOT cover a lab run,
     and a lab grant must still need the op + image tokens like any harness grant.
  2. The `lab_run` tool is registered where it must be: the operator-only platform
     schemas (mesh_harness_run left them for the public set), the
     inline meta-dispatch set, the host-door (code-exec) set, and
     the code-exec fallback mirror — so it can never leak into the agent lane or be
     treated as a read.
  3. Its schema DISCLOSES billing: the description names the lab rate (or says it is not
     configured), and the executor refuses when the rate is unset — never the ordinary rate.

Run: python3 scripts/test_lab_run.py
"""
import ast
import os
import sys
from tools_source import tools_source  # tools.py and the modules split out of it

ROOT = os.environ.get("APP_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


# ── 1. wall #2: the mode token (server/network/nac_mgmt.py) ───────────────────
from server.network import nac_mgmt  # noqa: E402

home_grant = {"server": "home-box", "ops": [nac_mgmt.HARNESS_RUN_OP, "image:*"]}
lab_grant = {"server": "lab-box", "ops": [nac_mgmt.HARNESS_RUN_OP, "image:*", nac_mgmt.LAB_MODE_TOKEN]}
lab_no_image = {"server": "lab-box", "ops": [nac_mgmt.HARNESS_RUN_OP, nac_mgmt.LAB_MODE_TOKEN]}
lab_no_op = {"server": "lab-box", "ops": ["image:*", nac_mgmt.LAB_MODE_TOKEN]}

A = nac_mgmt.harness_image_allowed
check("home grant still covers an ordinary run", A([home_grant], 1, "home-box", "img:1"))
check("home grant (image:*) does NOT cover mode=lab", not A([home_grant], 1, "home-box", "img:1", mode="lab"))
check("lab grant covers mode=lab", A([lab_grant], 1, "lab-box", "img:1", mode="lab"))
check("lab grant also covers an ordinary run on that box", A([lab_grant], 1, "lab-box", "img:1"))
check("lab grant without an image token denies", not A([lab_no_image], 1, "lab-box", "img:1", mode="lab"))
check("lab grant without harness:run denies", not A([lab_no_op], 1, "lab-box", "img:1", mode="lab"))
check("mode token cannot be composed across two grants",
      not A([home_grant, {"server": "home-box", "ops": [nac_mgmt.LAB_MODE_TOKEN]}], 1, "home-box", "img:1", mode="lab"))
check("mode is case/space tolerant", nac_mgmt._mode_token(" Lab ") == nac_mgmt.LAB_MODE_TOKEN)
check("ordinary modes need no token", nac_mgmt._mode_token("git") is None and nac_mgmt._mode_token(None) is None)

# ── 2. registrations (brain/mastercode/public_tool_registry.py + code_exec_guard.py) ──
from mastercode import public_tool_registry as reg  # noqa: E402
from mastercode import code_exec_guard  # noqa: E402

schema = reg.OPERATOR_PLATFORM_TOOL_SCHEMAS.get("lab_run")
check("lab_run is an operator-only platform schema", schema is not None)
check("lab_run is not public (mesh_harness_run is, since 2026-10-02)",
      "lab_run" not in reg.PUBLIC_TOOL_SCHEMAS and "mesh_harness_run" in reg.PUBLIC_TOOL_SCHEMAS)
check("lab_run is in PUBLIC_META_TOOL_NAMES", "lab_run" in reg.PUBLIC_META_TOOL_NAMES)
check("lab_run is a host-door (code-exec) tool", "lab_run" in code_exec_guard.HOST_DOOR_TOOLS)
ta_src = open(os.path.join(ROOT, "brain", "mastercode", "runtime", "tool_access.py")).read()
check("tool_access code-exec fallback names lab_run", '"lab_run"' in ta_src)

# ── 3. disclosure ─────────────────────────────────────────────────────────────
fn_schema = (schema or {}).get("function", {})
desc = fn_schema.get("description", "")
check("description discloses billing", "BILLING" in desc and "LAB RATE" in desc)
check("description names the rate or says it is not configured",
      (os.environ.get("LAB_HARNESS_RATE_TEXT") or "NOT CONFIGURED") in desc)
check("tests is required", "tests" in fn_schema.get("parameters", {}).get("required", []))

tools_src = tools_source(ROOT)
tree = ast.parse(tools_src)
fn = next((n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_exec_lab_run"), None)
check("_exec_lab_run exists", fn is not None)
if fn is not None:
    src_fn = ast.get_source_segment(tools_src, fn)
    check("refuses when the rate is unset", "lab_rate_text()" in src_fn and "refused" in src_fn)
    check("forces mode='lab' on dispatch", 'mode="lab"' in src_fn)
    check("passes the rate back in the result", 'res["rate"]' in src_fn)
    check("dispatches through _exec_lab_run in _execute_meta_tool", 'name == "lab_run"' in tools_src)

# ── 4. two invariants ────────────────────────────────────────────────────────
# The rate refusal must live in run_harness_op — the SINGLE source of truth — because
# POST /harness/run and mesh_harness_run can both name mode='lab' directly. A check that
# lived only in _exec_lab_run was bypassable.
nh_src = open(os.path.join(ROOT, "server", "network", "nac_harness.py")).read()
nh_tree = ast.parse(nh_src)
rho = next((n for n in ast.walk(nh_tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_harness_op"), None)
check("run_harness_op exists", rho is not None)
if rho is not None:
    rho_src = ast.get_source_segment(nh_src, rho)
    check("the rate check is in run_harness_op, not only in the tool",
          "LAB_HARNESS_RATE_TEXT" in rho_src)
    check("the rate check runs BEFORE the grant gate",
          rho_src.index("LAB_HARNESS_RATE_TEXT") < rho_src.index("harness_image_allowed"))
    check("gate='auto' is enforced there too", "gate" in rho_src and "auto" in rho_src)
    # A lab run is many turns plus the time to build the lab; waiting one turn timed the
    # caller out mid-run while the bastion carried on and its result was discarded.
    check("the lab dispatch wait is built from hosts and steps, not one turn",
          "LAB_HOST_READY_BUDGET_S" in rho_src and "tests" in rho_src)
    check("the ordinary modes keep the one-turn wait",
          'float(spec["max_minutes"]) * 60 + 30' in rho_src)

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all lab_run gates pass")
