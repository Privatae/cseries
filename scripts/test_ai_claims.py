#!/usr/bin/env python3
"""docs/ai/claims.md cites a real test for every claim, or a real file.

docs/ai/claims.md lists C-Series's own security claims, each with the test
that fails if the claim stops being true. A renamed or deleted test would
leave the table pointing at nothing while still reading as checked, so this
gate parses every table row and fails when:

  - a row cites a test as `path::name` and the file does not exist, or the
    name is not in it: a Python def or class, or text inside a string
    literal that is not a docstring (a check(...) label, or the fixed part
    of an f-string label), or text inside the name a JS test(...) / it(...)
    / describe(...) is given;
  - a row says "no test; enforced by the code at `path`" and the path does
    not exist;
  - a row does neither.

It also runs itself against a made-up table with a missing test, so a parser
that stopped matching anything cannot pass.

Pure: reads files, runs nothing. Exit 0 = pass, 1 = fail.
"""
import ast
import os
import re
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
CLAIMS = os.path.join("docs", "ai", "claims.md")

TEST_REF = re.compile(r"`([^`\s]+?)::([^`]+)`")
NO_TEST = re.compile(r"no test; enforced by the code at ((?:`[^`]+`(?:,\s*|\s+and\s+)?)+)")
CODE_PATH = re.compile(r"`([^`]+)`")
JS_NAMES = re.compile(r"""\b(?:test|it|describe)\s*\(\s*(['"`])((?:\\.|(?!\1).)*)\1""")
JS_FUNCS = re.compile(r"\bfunction\s+([A-Za-z_$][\w$]*)")


def _inside(root, rel):
    path = os.path.realpath(os.path.join(root, rel))
    return path if path.startswith(os.path.realpath(root) + os.sep) else None


# A cited label shorter than this must be a def: a short fragment would be
# found inside almost any file's strings.
MIN_LABEL = 12


def python_names(src):
    """(defs, string literals). A check label is a string literal: some gates
    pass it first, others second (check(cond, "label")), adjacent literals
    arrive here already joined, and the fixed text of an f-string label is a
    literal of its own. A docstring, or any string standing alone as a
    statement, is not a label: prose there must not stand in for a check."""
    tree = ast.parse(src)
    prose = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}
    defs, literals = set(), []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in prose:
            literals.append(node.value)
    return defs, literals


def js_names(src):
    # test('the manifest\'s ...'): the name as written, escapes undone.
    return set(JS_FUNCS.findall(src)), [re.sub(r"\\(.)", r"\1", m.group(2))
                                        for m in JS_NAMES.finditer(src)]


def defined(path, name):
    with open(path, encoding="utf-8", errors="replace") as f:
        src = f.read()
    if path.endswith(".py"):
        defs, labels = python_names(src)
    elif path.endswith((".js", ".cjs", ".mjs", ".ts")):
        defs, labels = js_names(src)
    else:
        # Shell and anything else: a function of that name, or quoted text.
        defs = set(re.findall(r"^\s*([\w-]+)\s*\(\)", src, re.M))
        labels = re.findall(r"\"([^\"\n]*)\"|'([^'\n]*)'", src)
        labels = [a or b for a, b in labels]
    if name in defs:
        return True
    return len(name) >= MIN_LABEL and any(name in label for label in labels)


def rows(text):
    """The body rows of every Markdown table: (line number, cells)."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s.startswith("|") or re.fullmatch(r"\|[\s:|-]+\|", s):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and cells[0].lower() == "claim":
            continue
        out.append((n, cells))
    return out


def problems(text, root):
    found = []
    table = rows(text)
    if not table:
        return ["no claim rows found in the tables"], 0
    for n, cells in table:
        line = " | ".join(cells)
        refs = TEST_REF.findall(line)
        no_test = NO_TEST.search(line)
        if not refs and not no_test:
            found.append(f"line {n}: cites no `path::name` test and no "
                         f"'no test; enforced by the code at `path`'")
        for rel, name in refs:
            path = _inside(root, rel)
            if not path or not os.path.isfile(path):
                found.append(f"line {n}: {rel} does not exist")
            elif not defined(path, name):
                found.append(f"line {n}: {rel} defines no test named {name!r}")
        if no_test:
            for rel in CODE_PATH.findall(no_test.group(1)):
                path = _inside(root, rel)
                if not path or not os.path.exists(path):
                    found.append(f"line {n}: {rel} does not exist")
    return found, len(table)


def main():
    # Negative control: a made-up table naming a test that is not there. The
    # name is put together here so that no literal in this file holds it.
    missing = "_".join(("no", "such", "test", "here"))
    fake = ("| Claim | Test | Asserts |\n|---|---|---|\n"
            "| x | `scripts/test_ai_claims.py::" + missing + "` | y |\n"
            "| x | `scripts/test_ai_claims.py::python_names` | y |\n")
    bad, _ = problems(fake, ROOT)
    control_ok = len(bad) == 1 and missing in bad[0]
    print(f"  [{'ok' if control_ok else 'FAIL'}] a made-up missing test is caught, a real one is not")

    with open(os.path.join(ROOT, CLAIMS), encoding="utf-8") as f:
        found, count = problems(f.read(), ROOT)
    for p in found:
        print(f"  [FAIL] {CLAIMS} {p}")
    print(f"  [{'ok' if not found else 'FAIL'}] {count} claim rows in {CLAIMS}, each citing a test or file that exists")
    return 0 if control_ok and not found else 1


if __name__ == "__main__":
    sys.exit(main())
