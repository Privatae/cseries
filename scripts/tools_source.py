"""runtime/tools.py's source as one text: tools.py and the tools_<category>
modules split out of it, in the order tools.py imports them.

A gate that checks the tool code by reading it reads it here, so code moved
between these files stays where the gate looks. A gate that needs one file's
own text (its line numbers, or what only that file may hold) reads that file.

The module list is read off tools.py's own `from mastercode.runtime.tools_<x>
import` lines, so a module split out later is read without editing this file.
"""
import ast
import os

RUNTIME = ("brain", "mastercode", "runtime")
SPLIT_PREFIX = "mastercode.runtime.tools_"


def repo_root() -> str:
    return os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def split_modules(tools_text: str) -> list:
    """The tools_<category> module names tools.py imports, in its order."""
    out = []
    for n in ast.parse(tools_text).body:
        if isinstance(n, ast.ImportFrom) and (n.module or "").startswith(SPLIT_PREFIX):
            name = n.module.rsplit(".", 1)[1]
            if name not in out:
                out.append(name)
    return out


def tools_paths(root=None) -> list:
    """tools.py, then every module split out of it. tools.py must exist
    (FileNotFoundError, as a plain read of it raised)."""
    base = os.path.join(str(root or repo_root()), *RUNTIME)
    first = os.path.join(base, "tools.py")
    with open(first, encoding="utf-8") as f:
        text = f.read()
    return [first] + [os.path.join(base, m + ".py") for m in split_modules(text)]


def tools_source(root=None) -> str:
    """Every tools file's text, joined by a blank line. A split module tools.py
    names but that is missing raises, as a missing tools.py does: an empty text
    would let every absence check pass. A tree from before the split reads as
    tools.py alone."""
    parts = []
    for p in tools_paths(root):
        with open(p, encoding="utf-8") as f:
            parts.append(f.read())
    return "\n\n".join(parts)


def tools_function(name: str, root=None) -> str:
    """The source of one top-level function or class of tools.py or a module
    split out of it, by AST, wherever it lives: a slice from its `def` to the
    next one's held only while both sat in tools.py. "" when none defines it."""
    for p in tools_paths(root):
        with open(p, encoding="utf-8") as f:
            text = f.read()
        for n in ast.parse(text).body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
                return ast.get_source_segment(text, n) or ""
    return ""
