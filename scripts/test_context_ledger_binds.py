"""Every variable of the turn that the context ledger reads is set on every
path to the ledger, and so is every name a step of the turn returns.

The ledger (the try in _send_message_core that builds _context_ledger) reads
the turn's variables by name. One that a path leaves unset raises inside that
try, and the turn row is written with no ledger and a debug line. Five of them
are set only inside a try (the per-turn, model-style and constitution blocks)
or an `if` (the orchestrator catalog and host rules, only when the Host
orchestrates), so each starts as None before it; nothing else holds that in
place. A step (a method run by _send_message_core) that returns a name one of
its paths leaves unset raises on that path, so _load_turn_history sets
_record_msg = None when it clears the history.

The proof is CPython's own: since 3.12 its compiler works out, for each read
of a local, whether every path to the read has set it (LOAD_FAST) or not
(LOAD_FAST_CHECK), through loops, break, match, try/finally, del and the
unbinding at the end of `except ... as`. It follows only the first 64 local
slots, and the turn has hundreds, so each check compiles the module with the
names under test read first in code that never runs (`if 0:`): that gives
them the low slots and sets nothing. A variable a closure shares (a cell) is
outside that analysis; it must be set by a statement of the function's own
body ahead of the read, and never deleted.

Pure source + compile checks, so this runs outside the image.

  APP_ROOT=<repo> python3 scripts/test_context_ledger_binds.py
"""
import ast
import copy
import dis
import os
import sys
import types
from turn_source import turn_paths

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def finish():
    if failures:
        print(f"\n{len(failures)} FAILED")
        sys.exit(1)
    print("\ncontext ledger binds: all passed")
    sys.exit(0)


check("python 3.12 or later (the compiler that tells LOAD_FAST from LOAD_FAST_CHECK)",
      sys.version_info >= (3, 12), sys.version)
if failures:
    finish()

PATH = turn_paths(ROOT)[0]
with open(PATH, encoding="utf-8") as f:
    SRC = f.read()
TREE = ast.parse(SRC)
_cls = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "EntityRuntime")
_methods = {n.name: n for n in _cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
core = _methods["_send_message_core"]
steps = {n: m for n, m in _methods.items()
         if "Run by _send_message_core" in " ".join((ast.get_docstring(m) or "").split())}
check("the turn's steps are found", len(steps) >= 5 and "_load_turn_history" in steps, sorted(steps))

_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.GeneratorExp)


def loads_in(node):
    """(names read in the function's own scope, names read by a nested scope)
    under node. A list, set or dict comprehension runs in the function's own
    frame (3.12 inlines them); a generator expression's first iterable does
    too. A comprehension's own loop variable, read inside it, is set by the
    comprehension and is not a read of the function's."""
    own, inner = set(), set()

    def visit(n, nested, bound=frozenset()):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound:
            (inner if nested else own).add(n.id)
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp)) and not nested:
            targets = bound | {t.id for g in n.generators for t in ast.walk(g.target) if isinstance(t, ast.Name)}
            visit(n.generators[0].iter, False, bound)
            for c in ast.iter_child_nodes(n):
                if c is not n.generators[0]:
                    visit(c, False, targets)
            for c in ast.iter_child_nodes(n.generators[0]):
                if c is not n.generators[0].iter:
                    visit(c, False, targets)
            return
        if isinstance(n, ast.GeneratorExp) and not nested:
            visit(n.generators[0].iter, False)
            for c in ast.iter_child_nodes(n):
                if c is not n.generators[0]:
                    visit(c, True)
            for c in ast.iter_child_nodes(n.generators[0]):
                if c is not n.generators[0].iter:
                    visit(c, True)
            return
        for c in ast.iter_child_nodes(n):
            visit(c, nested or isinstance(n, _NESTED) and n is not node, bound)
    visit(node, False)
    return own, inner


def _code_of(code, qualname):
    for k in code.co_consts:
        if isinstance(k, types.CodeType):
            if k.co_qualname == qualname:
                return k
            r = _code_of(k, qualname)
            if r is not None:
                return r
    return None


def unbound_reads(fn, names, lo, hi):
    """{name: why} for each name of fn's own frame that a path can reach a
    read of, between lines lo and hi, without setting it."""
    out = {}
    names = sorted(names)
    for i in range(0, len(names), 64):
        chunk = names[i:i + 64]
        # The method alone, in its class: which locals a path sets is the
        # method's own business, so the rest of the module is not compiled.
        # Without its parameters (they read as globals there, which changes
        # nothing for its other locals), so the names under test take the
        # first slots however many parameters it has (the tool loop has 89).
        f2 = copy.deepcopy(fn)
        f2.args = ast.arguments(posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kw_defaults=[],
                                kwarg=None, defaults=[])
        cls2 = copy.copy(_cls)
        cls2.body = [f2]
        tree = ast.Module(body=[cls2], type_ignores=[])
        first = f2.body[0]
        dead = ast.If(test=ast.Constant(0), orelse=[], body=[ast.Expr(
            ast.Tuple([ast.Name(n, ast.Load()) for n in chunk], ast.Load()))])
        for n in ast.walk(dead):
            ast.copy_location(n, first)
        f2.body.insert(0, dead)
        code = _code_of(compile(tree, PATH, "exec"), f"EntityRuntime.{fn.name}")
        seen = {}
        for ins in dis.get_instructions(code):
            ln = ins.positions.lineno if ins.positions else None
            if ln is None or not lo <= ln <= hi or not ins.opname.startswith("LOAD_FAST"):
                continue
            for v in (ins.argval if isinstance(ins.argval, tuple) else (ins.argval,)):
                if v in chunk:
                    seen.setdefault(v, set()).add(ins.opname)
        for n in chunk:
            if n not in code.co_varnames or code.co_varnames.index(n) >= 64:
                out[n] = "outside the compiler's 64 checked slots"
            elif n not in seen:
                out[n] = "no read of it found in the compiled code"
            elif "LOAD_FAST_CHECK" in seen[n]:
                out[n] = "a path reaches the read without setting it"
    return out


_MODULE = compile(TREE, PATH, "exec")


def check_reads(label, fn, region, need_reads=False):
    code = _code_of(_MODULE, f"EntityRuntime.{fn.name}")
    params = set(code.co_varnames[:code.co_argcount + code.co_kwonlyargcount
                                  + bool(code.co_flags & 4) + bool(code.co_flags & 8)])
    own, inner = loads_in(region)
    cells = set(code.co_cellvars) - params
    plain = {n for n in own if n in code.co_varnames and n not in params and n not in cells}
    bad = unbound_reads(fn, plain, region.lineno, region.end_lineno)
    # A cell: set by a statement of the function's own body ahead of region,
    # never deleted.
    deleted = {t.id for d in ast.walk(fn) if isinstance(d, ast.Delete) for t in d.targets
               if isinstance(t, ast.Name)}
    ahead = {t.id for s in fn.body if s.end_lineno < region.lineno
             and isinstance(s, (ast.Assign, ast.AnnAssign)) and s.value is not None
             for t in (s.targets if isinstance(s, ast.Assign) else [s.target]) if isinstance(t, ast.Name)}
    ahead |= {a.asname or a.name.split(".")[0] for s in fn.body if s.end_lineno < region.lineno
              and isinstance(s, (ast.Import, ast.ImportFrom)) for a in s.names}
    for n in sorted((own | inner) & cells):
        if n in deleted or n not in ahead:
            bad[n] = "a closure's variable not set by a statement of the body ahead of the read"
    check(f"{label}: every variable it reads is set on every path ({len(plain)} locals, "
          f"{len((own | inner) & cells)} closure variables)",
          (bool(plain) or not need_reads) and not bad,
          "; ".join(f"{k}: {v}" for k, v in sorted(bad.items())) or "nothing read")
    return plain | ((own | inner) & cells)


_ledger = [s for s in core.body if isinstance(s, ast.Try) and any(
    isinstance(a, ast.Assign) and isinstance(a.value, ast.Dict)
    and any(isinstance(t, ast.Name) and t.id == "_context_ledger" for t in a.targets)
    for a in ast.walk(s))]
check("the context ledger's try is found in the turn's own body", len(_ledger) == 1, f"{len(_ledger)} found")
if len(_ledger) == 1:
    read = check_reads("the context ledger", core, _ledger[0], need_reads=True)
    # The five set only inside a try or an `if`: a sanity floor, so a ledger
    # that stopped reading them by name cannot pass on an empty list.
    want = {"_pt_block", "_style_block", "_const_block", "_orch_block", "_host_rules",
            "history_msgs", "history_chars", "_record_msg", "tools"}
    check("the ledger reads the turn's blocks by name", want <= read, sorted(want - read))

for name, fn in sorted(steps.items()):
    ret = fn.body[-1]
    if isinstance(ret, ast.Return) and ret.value is not None:
        check_reads(f"{name}'s return", fn, ret)

finish()
