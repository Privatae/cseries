#!/usr/bin/env python3
"""
Contract check: every keyword passed to `_call_entity_model(...)` anywhere in
the runtime must exist in the wrapper's signature (brain/mastercode/runtime/
tools.py). Pure AST — no imports of the runtime, runs on any box.

Why this exists: the 3-lane router added `base_url=` to the chat path's call
(session.py MODEL_INTENT block) without updating the wrapper, and every
open-lane chat turn died with "unexpected keyword argument 'base_url'"
(the first live Workbench message). The gateway alias
(provider_gateway.call_model) takes **kwargs, so only the wrapper can drift —
this pins wrapper and call sites together.

`**dict` spreads are resolved, not skipped: one release crashed every chat
turn with "unexpected keyword argument 'on_alive'" while this check
read green, because the Host turn passes its route fields as
`**_chat_call_kw`, often through `_call_with_overflow_retry(msgs, **kw)`,
and both were invisible here. Now:
  - a function that forwards its own **kwarg into _call_entity_model (the
    overflow-retry wrapper) counts as a call site, and its callers are checked;
  - a `**name` spread is resolved to the keys the enclosing function writes
    into `name` (dict literal, dict(...), name["k"] = ..., name.update(...));
  - a spread this check cannot resolve FAILS, so the blind spot cannot reopen.
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "brain/mastercode/runtime/tools.py"
# Files whose _call_entity_model call sites resolve to the runtime wrapper
# (direct or via runtime/__init__ re-export). memory.py / memory_handlers.py
# forward opaque **kwargs — nothing to check statically there.
CALLERS = [
    ROOT / "brain/mastercode/runtime/session.py",
    ROOT / "brain/mastercode/runtime/tools.py",
    ROOT / "brain/mastercode/thread_consolidator.py",
]
TARGET = "_call_entity_model"
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def wrapper_params(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == TARGET:
            a = node.args
            names = {p.arg for p in a.args + a.posonlyargs + a.kwonlyargs}
            if a.kwarg:  # **kwargs — accepts anything
                return set()
            return names
    raise SystemExit(f"FAIL: no {TARGET} def in {path}")


def _called_name(call: ast.Call) -> str | None:
    f = call.func
    return f.id if isinstance(f, ast.Name) else (
        f.attr if isinstance(f, ast.Attribute) else None)


def _parents(tree: ast.AST) -> dict:
    par = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            par[child] = node
    return par


def _enclosing_funcs(node, par) -> list:
    out = []
    while node in par:
        node = par[node]
        if isinstance(node, _FUNCS):
            out.append(node)
    return out


def forwarders(tree: ast.AST, par: dict) -> dict[str, str]:
    """{function name: its **kwarg name} for every function that passes its
    own **kwarg straight into _call_entity_model."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _called_name(node) == TARGET):
            continue
        for kw in node.keywords:
            if kw.arg is None and isinstance(kw.value, ast.Name):
                fns = _enclosing_funcs(node, par)
                if fns and fns[0].args.kwarg and fns[0].args.kwarg.arg == kw.value.id \
                        and fns[0].name != TARGET:
                    out[fns[0].name] = kw.value.id
    return out


def _dict_keys(value: ast.AST) -> tuple[set[str], str | None]:
    """Keys of a dict literal / dict(...) call; (keys, reason) where reason is
    set when some key cannot be read statically."""
    if isinstance(value, ast.Dict):
        keys, why = set(), None
        for k in value.keys:
            if k is None:
                why = "nested ** inside the dict"
            elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
            else:
                why = f"non-literal key {ast.unparse(k)}"
        return keys, why
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
            and value.func.id == "dict" and not value.args:
        keys = {k.arg for k in value.keywords if k.arg}
        why = "nested ** inside dict(...)" if any(k.arg is None for k in value.keywords) else None
        return keys, why
    return set(), f"not a dict literal: {ast.unparse(value)[:60]}"


def resolve_spread(name: str, call: ast.Call, par: dict) -> tuple[set[str], str | None]:
    """Every key the nearest enclosing function that builds `name` writes
    into it."""
    for fn in _enclosing_funcs(call, par):
        keys: set[str] = set()
        found, why = False, None
        for node in ast.walk(fn):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and t.id == name and node.value is not None:
                        k, w = _dict_keys(node.value)
                        keys |= k
                        found, why = True, why or w
                    elif isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) \
                            and t.value.id == name:
                        found = True
                        if isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str):
                            keys.add(t.slice.value)
                        else:
                            why = why or f"non-literal key {ast.unparse(t.slice)}"
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("update", "setdefault") \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == name:
                found = True
                if node.func.attr == "setdefault":
                    a0 = node.args[0] if node.args else None
                    if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                        keys.add(a0.value)
                    else:
                        why = why or "non-literal setdefault key"
                else:
                    keys |= {k.arg for k in node.keywords if k.arg}
                    for a in node.args:
                        k, w = _dict_keys(a)
                        keys |= k
                        why = why or w
        if found:
            return keys, why
    return set(), "no assignment found in the enclosing functions"


def call_keywords(path: Path) -> tuple[list[tuple[int, str]], list[str]]:
    """(lineno, keyword) for every keyword reaching the wrapper, and the
    spreads that could not be resolved."""
    out, opaque = [], []
    tree = ast.parse(path.read_text())
    par = _parents(tree)
    fwd = forwarders(tree, par)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name != TARGET and name not in fwd:
            continue
        own = _enclosing_funcs(node, par)
        own_kwarg = own[0].args.kwarg.arg if own and own[0].args.kwarg else None
        for kw in node.keywords:
            if kw.arg is not None:
                out.append((node.lineno, kw.arg))
                continue
            v = kw.value
            if isinstance(v, ast.Name) and v.id == own_kwarg and own[0].name in fwd:
                continue  # the forwarder's own **kwarg — its callers are checked
            if isinstance(v, ast.Name):
                keys, why = resolve_spread(v.id, node, par)
            else:
                keys, why = _dict_keys(v)
            out.extend((node.lineno, k) for k in sorted(keys))
            if why:
                opaque.append(f"{path.relative_to(ROOT)}:{node.lineno} "
                              f"**{ast.unparse(v)[:60]}: {why}")
    return out, opaque


def main() -> int:
    params = wrapper_params(WRAPPER)
    if not params:  # wrapper grew **kwargs — contract satisfied by shape
        print("OK — wrapper accepts **kwargs")
        return 0
    bad, opaque_all = [], []
    checked = 0
    for path in CALLERS:
        kws, opaque = call_keywords(path)
        opaque_all.extend(opaque)
        for lineno, kw in kws:
            checked += 1
            if kw not in params:
                bad.append(f"{path.relative_to(ROOT)}:{lineno} passes {kw!r} "
                           f"— not in the wrapper signature")
    if bad or opaque_all:
        print(f"FAIL: {TARGET} call-site/signature drift:")
        for b in sorted(set(bad)):
            print("  " + b)
        for o in opaque_all:
            print("  unresolvable spread (name its keys so this check can see them): " + o)
        return 1
    print(f"OK — {checked} keywords across {len(CALLERS)} files all bind "
          f"({len(params)} wrapper params)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
