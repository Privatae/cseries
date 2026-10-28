#!/usr/bin/env python3
# gate-scope: whole-tree — every .py file in the tree, for direct calls to route handlers
"""
A route's FastAPI parameter marker never reaches a direct Python caller.

A route written `offset: int = Query(0, ge=0)` has, as its PYTHON default, the
Query() marker object itself. Over HTTP, FastAPI resolves it to 0. Called
directly from Python (a host tool, a gate, another route) with that argument
left out, the function receives the Query(0) object, and the first SQL argument
it reaches raises. Measured on a rollout: a batch of fixes gave
projects.list_projects `offset: int = Query(0, ge=0)`,
scripts/test_pipeline_db.py calls list_projects(include_archived=True,
limit=1000) directly, and smoke failed with "invalid input for query argument
$3: Query(0) ('Query' object cannot be interpreted as an integer)". The gate
that caught it needs a live DB, so it only ran after the image was built.

This gate needs only the source tree. It parses every .py file, finds each
route function (decorated by a router verb) with a marker default (Query,
Path, Body, Header, Cookie, Form, File, Depends, Security), resolves every
direct call to it through the caller's imports, and fails on a call that
leaves such a parameter to its default. Two forms are safe and pass:
  - the Annotated form, `offset: Annotated[int, Query(ge=0)] = 0`: HTTP
    validation is unchanged and a direct caller gets a real 0 (the default is
    no longer a marker, so nothing here flags it);
  - a function that normalises the parameter itself, i.e. its body contains
    `isinstance(<param>, ...)` (server/routes/workspace.py's workspace_tree).

A call with *args or **kwargs is not judged (what it passes cannot be read
from source); nor is a call through getattr/importlib.

Exit 0 = no offending call. Exit 1 = each offending call printed as
file:line, the route it reaches, and the parameters it leaves as markers.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parent.parent)

MARKERS = frozenset({"Query", "Path", "Body", "Header", "Cookie", "Form", "File",
                     "Depends", "Security"})
VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options",
                   "api_route", "websocket"})
SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv",
                       "dist", "build", ".mypy_cache", ".pytest_cache"})
# Package roots on sys.path in the image besides the repo root (/app):
# /app/brain (mastercode.*, server code imports it that way).
EXTRA_ROOTS = ("brain",)


def _files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith(".py"):
                yield Path(dirpath) / f


def _module_names(path: Path) -> list[str]:
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    names = [".".join(parts)] if parts else []
    for extra in EXTRA_ROOTS:
        if len(parts) > 1 and parts[0] == extra:
            names.append(".".join(parts[1:]))
    return names


def _marker_name(node) -> str | None:
    if isinstance(node, ast.Call):
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if name in MARKERS:
            return name
    return None


def _is_route(fn) -> bool:
    for d in fn.decorator_list:
        call = d.func if isinstance(d, ast.Call) else d
        if isinstance(call, ast.Attribute) and call.attr in VERBS:
            return True
    return False


def _guarded(fn, param: str) -> bool:
    """The body normalises `param` itself: isinstance(param, ...) appears."""
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "isinstance" and n.args
                and isinstance(n.args[0], ast.Name) and n.args[0].id == param):
            return True
    return False


class Route:
    def __init__(self, module: str, fn, path: Path):
        self.module, self.name, self.path, self.line = module, fn.name, path, fn.lineno
        a = fn.args
        self.positional = [p.arg for p in a.posonlyargs + a.args]
        self.markers: dict[str, str] = {}
        pos = a.posonlyargs + a.args
        for p, dflt in zip(pos[len(pos) - len(a.defaults):], a.defaults):
            m = _marker_name(dflt)
            if m and not _guarded(fn, p.arg):
                self.markers[p.arg] = m
        for p, dflt in zip(a.kwonlyargs, a.kw_defaults):
            m = _marker_name(dflt) if dflt is not None else None
            if m and not _guarded(fn, p.arg):
                self.markers[p.arg] = m


def _dotted(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _resolve_from(module_of_file: str, is_pkg: bool, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    base = module_of_file.split(".")
    if not is_pkg:
        base = base[:-1]
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def main() -> int:
    trees: dict[Path, ast.AST] = {}
    for p in _files():
        try:
            trees[p] = ast.parse(p.read_text(encoding="utf-8", errors="replace"), str(p))
        except SyntaxError:
            continue

    routes: dict[tuple[str, str], Route] = {}
    modules: set[str] = set()
    for p, tree in trees.items():
        names = _module_names(p)
        modules.update(names)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_route(node):
                r = Route(names[0] if names else str(p), node, p)
                if r.markers:
                    for mn in names:
                        routes[(mn, node.name)] = r

    if not routes:
        print("FAIL: found no route function with a marker default — the parser no "
              "longer reads this tree; fix it rather than trust a green run")
        return 1

    bad = []
    calls_checked = 0
    for p, tree in trees.items():
        names = _module_names(p)
        here = names[0] if names else ""
        is_pkg = p.name == "__init__.py"
        mod_alias: dict[str, str] = {}
        fn_alias: dict[str, Route] = {}
        for mn in names:
            for (rm, rn), r in routes.items():
                if rm == mn:
                    fn_alias.setdefault(rn, r)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        mod_alias[a.asname] = a.name
                    else:
                        top = a.name.split(".")[0]
                        mod_alias.setdefault(top, top)
            elif isinstance(node, ast.ImportFrom):
                src = _resolve_from(here, is_pkg, node)
                for a in node.names:
                    local = a.asname or a.name
                    full = f"{src}.{a.name}" if src else a.name
                    if full in modules:
                        mod_alias[local] = full
                    elif (src, a.name) in routes:
                        fn_alias[local] = routes[(src, a.name)]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = None
            if isinstance(node.func, ast.Name):
                target = fn_alias.get(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                d = _dotted(node.func.value)
                if d:
                    head, _, rest = d.partition(".")
                    mod = mod_alias.get(head)
                    if mod:
                        full = f"{mod}.{rest}" if rest else mod
                        target = routes.get((full, node.func.attr))
            if target is None:
                continue
            if any(isinstance(a, ast.Starred) for a in node.args) or \
                    any(k.arg is None for k in node.keywords):
                continue
            calls_checked += 1
            passed = {k.arg for k in node.keywords}
            passed.update(target.positional[: len(node.args)])
            left = sorted(set(target.markers) - passed)
            if left:
                bad.append((p.relative_to(ROOT), node.lineno, target, left))

    if bad:
        for path, line, r, left in sorted(bad, key=lambda b: (str(b[0]), b[1])):
            what = ", ".join(f"{n}={r.markers[n]}()" for n in left)
            print(f"  {path}:{line} calls {r.module}.{r.name} "
                  f"({r.path.relative_to(ROOT)}:{r.line}) leaving {what}")
        print(f"FAIL: {len(bad)} direct call(s) leave a route parameter as its FastAPI "
              "marker object — write the default as Annotated[T, Query(...)] = value, "
              "or pass the argument")
        return 1
    print(f"OK: {len(routes)} route/module pairs with marker defaults; {calls_checked} "
          "direct calls pass every such parameter (or the route normalises it)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
