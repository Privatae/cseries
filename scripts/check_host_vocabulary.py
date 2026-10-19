#!/usr/bin/env python3
"""Gate: the retired build words never reach the host again.

The host's words are build (one task, one engine), compare (the same
task on N engines/models, the user picks) and parallel builds (N independent
tasks). "fan-out" and "bake-off" were the architecture leaking into speech — a
single codex run reported as "the fan-out I started" — and review.fan_out, the
door that taught them, is removed. This gate fails if either word, or the
removed tool's name, reappears in anything the host reads:

  1. every tool schema the host can be offered (public, operator-only, bot and
     bridge registries) — names, descriptions, purposes, parameter text;
  2. every skill in default-skills/;
  3. every string literal in the kernel and routes (brain/mastercode,
     server) — tool results, refusals, prompts and delivery templates are all
     string literals. Docstrings, comments and log-call arguments are not read
     by the host and are skipped;
  4. every system app manifest (system-apps/*/app.yaml) — descriptions and
     user_path are read by the host; changelog comments are not.

The internal identifier `fan_out` (a task_type value, a route, a function
name) is not a word the host speaks and is allowed; `review.fan_out` /
`review_fan_out` are not.

Run: APP_ROOT=<repo> python3 scripts/check_host_vocabulary.py
"""
import ast
import json
import os
import re
import sys
from pathlib import Path

APP_ROOT = Path(os.environ.get("APP_ROOT", "/app"))
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "brain"))

# The one place the removed name must still be spelled: the retired-tool set
# that refuses a stray call to it everywhere (code_exec_guard).
_ALLOWED = {("brain/mastercode/code_exec_guard.py", "review.fan_out"),
            ("brain/mastercode/code_exec_guard.py", "review_fan_out")}
BANNED = re.compile(r"fan[- ]out|fanout|bake[- ]off|bakeoff|review[._]fan_out", re.I)
_LOG_ATTRS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}

fails: list[str] = []


def _hit(where: str, text: str) -> None:
    m = BANNED.search(text or "")
    if m:
        a = max(0, m.start() - 40)
        fails.append(f"{where}: …{text[a:m.end() + 40]!s}…".replace("\n", " "))


# 1. tool schemas
from mastercode.public_tool_registry import (  # noqa: E402
    PUBLIC_TOOL_SCHEMAS, OPERATOR_PLATFORM_TOOL_SCHEMAS, PUBLIC_META_TOOL_NAMES)
from mastercode.runtime.triage import _BOT_TOOL_SCHEMAS, _BRIDGE_TOOL_SCHEMAS  # noqa: E402
for label, src in (("public", PUBLIC_TOOL_SCHEMAS), ("operator", OPERATOR_PLATFORM_TOOL_SCHEMAS),
                   ("bot", _BOT_TOOL_SCHEMAS), ("bridge", _BRIDGE_TOOL_SCHEMAS)):
    for key, sc in src.items():
        _hit(f"schema[{label}] {key}", json.dumps(sc, ensure_ascii=False))
for name in PUBLIC_META_TOOL_NAMES:
    _hit("PUBLIC_META_TOOL_NAMES", name)

# 2. skills
for md in sorted((APP_ROOT / "default-skills").glob("*.md")):
    for i, line in enumerate(md.read_text().splitlines(), 1):
        _hit(f"default-skills/{md.name}:{i}", line)


# 3. string literals in the kernel + routes
def _skipped_ids(tree: ast.AST) -> set[int]:
    skip: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b = n.body[0] if n.body else None
            if isinstance(b, ast.Expr) and isinstance(getattr(b, "value", None), ast.Constant):
                skip.add(id(b.value))
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in _LOG_ATTRS
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id in ("log", "logger", "_log", "LOG", "logging")):
            for arg in n.args:
                for sub in ast.walk(arg):
                    skip.add(id(sub))
    return skip


for base in ("brain/mastercode", "server"):
    for py in sorted((APP_ROOT / base).rglob("*.py")):
        try:
            tree = ast.parse(py.read_text())
        except (SyntaxError, UnicodeDecodeError) as e:
            fails.append(f"{py.relative_to(APP_ROOT)}: unparseable ({e})")
            continue
        skip = _skipped_ids(tree)
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
                rel = str(py.relative_to(APP_ROOT))
                if (rel, n.value) in _ALLOWED:
                    continue
                _hit(f"{rel}:{n.lineno}", n.value)

# 4. system app manifests (non-comment lines)
for yml in sorted((APP_ROOT / "system-apps").glob("*/app.yaml")):
    for i, line in enumerate(yml.read_text().splitlines(), 1):
        if not line.lstrip().startswith("#"):
            _hit(f"{yml.relative_to(APP_ROOT)}:{i}", line)

if fails:
    for f in fails:
        print(f"FAIL: {f}")
    print(f"\n{len(fails)} place(s) the host would read a retired word — the words are "
          "build / compare / parallel builds.")
    sys.exit(1)
print("OK: no 'fan-out' / 'bake-off' / review.fan_out in tool schemas, skills, "
      "kernel + route string literals, or system app manifests")
