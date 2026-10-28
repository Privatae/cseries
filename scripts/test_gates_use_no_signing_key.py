#!/usr/bin/env python3
# gate-scope: whole-tree — every file under scripts/ and tests/
"""Gate: no gate or test reads the app signing key.

SIGN_KEY names the Ed25519 private key every Host trusts: it signs the
built-in app bundles and the App SDK. A gate that signs with it makes
artifacts every Host accepts: tests/test_sdk_pull.py signed an SDK one patch
NEWER than the shipped floor and removed it only when every assertion passed,
and scripts/test_sdk_floor_guard.py signed versions up to 1.10.0 (review of
preview-animated at fa0dccbe). Both now make a throwaway key per run; a test
that needs a signature makes its own.

The publish tools (build_sdk_bundle.py, publish_sdk.py,
rebuild_factory_bundles.sh) are where the key belongs and are not gates. This
reads every gate — scripts/test_*, scripts/e2e_*, everything under tests/ — and
fails on SIGN_KEY in code: a string in Python (docstrings and
comments are prose and do not count), any non-comment line in shell.

Run: APP_ROOT=<repo> python3 scripts/test_gates_use_no_signing_key.py
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
KEY_NAME = "SIGN_KEY"
SELF = "scripts/test_gates_use_no_signing_key.py"


def _docstrings(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def uses(rel: str, src: str) -> list[int]:
    """Lines where `src` (a gate at `rel`) names the signing key in code."""
    if rel.endswith(".py"):
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return [0]
        skip = _docstrings(tree)
        return sorted({n.lineno for n in ast.walk(tree)
                       if isinstance(n, ast.Constant) and isinstance(n.value, str)
                       and KEY_NAME in n.value and id(n) not in skip})
    return [i for i, ln in enumerate(src.splitlines(), 1)
            if KEY_NAME in ln and not ln.lstrip().startswith("#")]


def gates(root: Path):
    for f in sorted((root / "scripts").glob("*")):
        if f.is_file() and (f.name.startswith("test_") or f.name.startswith("e2e_")) \
                and f.suffix in (".py", ".sh"):
            yield f
    for f in sorted((root / "tests").rglob("*")):
        if f.is_file() and f.suffix in (".py", ".sh"):
            yield f


def main() -> int:
    fails = 0

    def check(label, ok, detail=""):
        nonlocal fails
        print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n      {detail}" if detail and not ok else ""))
        fails += 0 if ok else 1

    found, n = [], 0
    for f in gates(ROOT):
        rel = f.relative_to(ROOT).as_posix()
        if rel == SELF:
            continue
        n += 1
        found += [f"{rel}:{line}" for line in uses(rel, f.read_text(errors="replace"))]
    check(f"no gate reads {KEY_NAME} ({n} gates read) — a test that needs a signature makes a throwaway key",
          not found, "\n      ".join(found))

    # The reader itself: code counts, prose does not.
    probes = {
        ('KEY = Path(os.environ["SIGN_KEY"])\n', "x.py"): True,
        ('k = open(f"{env[\'SIGN_KEY\']}")\n', "x.py"): True,
        ('"""Uses no SIGN_KEY."""\n', "x.py"): False,
        ("# signed with SIGN_KEY once\nx = 1\n", "x.py"): False,
        ('openssl pkeyutl -sign -inkey "$SIGN_KEY"\n', "x.sh"): True,
        ("# the old run read $SIGN_KEY\n", "x.sh"): False,
    }
    wrong = [src for (src, rel), want in probes.items() if bool(uses(rel, src)) != want]
    check("the reader counts code and not prose", not wrong, wrong)
    print()
    if fails:
        print(f"{fails} FAILED")
        return 1
    print("all gates passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
