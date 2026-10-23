#!/usr/bin/env python3
"""Gate: host-tool parity for the coding bench (the backend-parity rule,
2026-07-18: a backend capability without its host-tool leg is not done).

Static/AST checks, no app imports:
  1. Every compare arm the `build` REGISTRY schema advertises (variants,
     seed, vcpus, context, acceptance, max_minutes) is a real FanOutReq field
     — the schema can never promise an arm the server drops.
  2. Every one of those arms is advertised AND forwarded by the executor
     (build(action='run') → _exec_build_compare → FanOutReq) — the inverse
     direction: the server can never grow an arm the host can't reach.
     (This is the gap that shipped 2026-07-01→18: server honored four arms,
     the tool exposed none.) The door was review.fan_out until 2026-09-22.
  3. Every tool name in the registry dispatch set that this gate owns
     (ladder.*, harness_costs, harness_transcript) has a registry schema and
     a dispatch branch in tools.py — the "Unknown meta-tool" class
     (wired-2026-06-23 bug) can't recur for them.

Run: APP_ROOT=<repo> python3 scripts/test_tool_registry_parity.py
"""
import ast
import os
import re
import sys
from tools_source import tools_source  # tools.py and the modules split out of it

failures = []


def check(label, ok, detail=""):
    print(f"[tool-parity] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    app_root = os.environ.get("APP_ROOT", "/app")
    reg_src = open(os.path.join(
        app_root, "brain/mastercode/public_tool_registry.py")).read()
    tools_src = tools_source(app_root)
    review_src = open(os.path.join(
        app_root, "server/routes/review.py")).read()

    # FanOutReq fields (AST — annotated assignments in the class body).
    fanout_fields: set = set()
    for node in ast.walk(ast.parse(review_src)):
        if isinstance(node, ast.ClassDef) and node.name == "FanOutReq":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fanout_fields.add(stmt.target.id)
    check("FanOutReq parsed", bool(fanout_fields))

    # review.fan_out registry schema properties (AST — walk the dict literal).
    def _schema_props(src: str, tool: str) -> set:
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if tool not in keys:
                continue
            entry = node.values[keys.index(tool)]
            try:
                fn = dict(zip((k.value for k in entry.keys), entry.values))["function"]
                params = dict(zip((k.value for k in fn.keys), fn.values))["parameters"]
                props = dict(zip((k.value for k in params.keys), params.values))["properties"]
                return {k.value for k in props.keys}
            except Exception:
                continue
        return set()

    # The compare's arms reach FanOutReq through ONE door since 2026-09-22:
    # build(action='run', variants=[…]) (review.fan_out was removed).
    props = _schema_props(reg_src, "build")
    check("build schema parsed", bool(props))
    arms = {"variants", "seed", "vcpus", "context", "acceptance", "max_minutes"}
    check("1: every compare arm the build schema advertises is a FanOutReq field",
          arms <= fanout_fields, f"phantom: {sorted(arms - fanout_fields)}")
    check("2a: all compare arms advertised in the build schema",
          arms <= props, f"missing: {sorted(arms - props)}")
    m = re.search(r"async def _exec_build_compare\(.*?\n(?=async def |def |# task_id)",
                  tools_src, re.S)
    body = m.group(0) if m else ""
    missing_fwd = [a for a in sorted(arms - {"max_minutes"})
                   if not re.search(rf"{a}\s*=\s*args\.get\([\"']{a}[\"']\)", body)]
    check("2b: all compare arms forwarded into FanOutReq by _exec_build_compare",
          bool(body) and not missing_fwd, f"not forwarded: {missing_fwd}")
    m2 = re.search(r"async def _exec_build_run\(.*?\n(?=async def |def )", tools_src, re.S)
    body2 = m2.group(0) if m2 else ""
    missing_run = [a for a in sorted(arms) if f'"{a}":' not in body2]
    check("2c: build(action='run') hands every compare arm to the compare door",
          bool(body2) and not missing_run, f"not handed on: {missing_run}")
    check("2d: review.fan_out has no schema left to drift",
          not _schema_props(reg_src, "review.fan_out"))

    owned = ("ladder.run", "ladder.status", "ladder.decision",
             "harness_costs", "harness_transcript", "credential_wiring")
    no_schema = [t for t in owned if f'"{t}":' not in reg_src]
    check("3a: owned tools have registry schemas", not no_schema, str(no_schema))
    no_dispatch_set = [t for t in owned if f'"{t}",' not in reg_src]
    check("3b: owned tools are in the registry dispatch set",
          not no_dispatch_set, str(no_dispatch_set))
    no_branch = [t for t in owned if f'name == "{t}"' not in tools_src]
    check("3c: owned tools have a dispatch branch in tools.py",
          not no_branch, str(no_branch))

    print(f"[tool-parity] {'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing): {failures}'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
