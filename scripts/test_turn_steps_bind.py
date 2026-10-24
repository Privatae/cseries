"""The turn's steps hand values across by name: each step EntityRuntime runs
for _send_message_core (a method whose docstring says "Run by
_send_message_core") is called once from the core, every argument is the
core's same-named variable, and the call binds exactly what the step returns,
in the order it returns it.

Many gates read a turn variable where it is set and where it is used by its
name (a rule's write task started in one place and handed to the move in
another, the row's project passed to each write site). With a step between
the two places, the name means the same value only while this holds: a call
that passes project_id as _row_project_id, or binds the step's returns in
another order, leaves every one of those gates green.

One step can end the turn itself: the tool loop (_run_tool_loop), whose
model error ends the turn where it happens. Every return in it hands back the
turn's reply, a value, and the core returns that reply when it is not None, in
the statement right after the call. None is what the loop's end returns, so a
bare return (or `return None`) there would let the turn go on past the loop as
if it had run to its end.

The other direction holds too: a variable of the turn that a step sets, and
that the turn reads after the step (in its own code or in a closure), is one
the step returns. A step's variables are its own, so one it sets and does not
return leaves the turn reading the value from before the step, with nothing
raised. And a variable of the turn that a step reads is one it is handed,
or one the step binds before every read of it: a step that sets the name
somewhere and reads it on a path before that reads its own unbound variable
(UnboundLocalError), where the turn read its value. CPython's own flow
analysis decides "before every read" (below). Every such read is one this gate sees: neither the turn nor a step
reads a variable through locals(), vars(), eval or exec, which no name check
can follow.

Pure source + AST checks, so this runs outside the image.

  APP_ROOT=<repo> python3 scripts/test_turn_steps_bind.py
"""
import ast
import copy
import dis
import os
import sys
from turn_source import turn_paths

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


with open(turn_paths(ROOT)[0], encoding="utf-8") as f:
    tree = ast.parse(f.read())
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EntityRuntime")
core = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send_message_core")
steps = {n.name: n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)
         and "Run by _send_message_core" in " ".join((ast.get_docstring(n) or "").split())}
# Without the steps this gate checks nothing; the three below are named so a
# docstring reworded away from the phrase cannot drop one silently.
check("the turn's steps are found",
      len(steps) >= 5 and {"_write_opening_row", "_read_turn_window", "_add_current_message"} <= set(steps),
      sorted(steps))


def returned(fn):
    """The names the step returns ([] for none), or None for a shape this
    gate does not read (an early return, a value that is not a name)."""
    rets = [n for n in ast.walk(fn) if isinstance(n, ast.Return)
            and not any(n in ast.walk(d) for d in ast.walk(fn)
                        if d is not fn and isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))]
    if not rets:
        return []
    if len(rets) != 1 or rets[0] is not fn.body[-1] or rets[0].value is None:
        return None
    v = rets[0].value
    out = []
    for e in (v.elts if isinstance(v, ast.Tuple) else [v]):
        if isinstance(e, ast.Name):
            out.append(e.id)
        else:
            return None
    return out


def ends_turn(fn, s, core):
    """fn can end the turn: every return in its own code carries a value that
    is not the None constant, it does not end on one, and the core binds the
    call (statement s) to one name and returns it when it is not None, in the
    next statement."""
    own = [n for n in ast.walk(fn) if isinstance(n, ast.Return)
           and not any(n in ast.walk(d) for d in ast.walk(fn)
                       if d is not fn and isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))]
    if not own or isinstance(fn.body[-1], ast.Return) or any(
            r.value is None or (isinstance(r.value, ast.Constant) and r.value.value is None) for r in own):
        return False
    if not (isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name)):
        return False
    v = s.targets[0].id
    for node in ast.walk(core):
        for field in ("body", "orelse", "finalbody"):
            lst = getattr(node, field, None)
            if isinstance(lst, list) and s in lst:
                i = lst.index(s)
                return i + 1 < len(lst) and ast.unparse(lst[i + 1]) == f"if {v} is not None:\n    return {v}"
    return False


class _GenAsList(ast.NodeTransformer):
    """For the probe only: a generator expression becomes a list
    comprehension. A list comprehension runs in the function's own frame
    (inlined since 3.12), so a variable of fn that it reads is a local the
    flow analysis follows; a generator expression is a scope of its own,
    which makes that variable a cell the analysis does not follow. It is read
    where it is made: a generator that reads a name before the step binds it
    reads the step's unbound variable when it runs (NameError), where the
    turn read its value."""

    def visit_GeneratorExp(self, n):
        self.generic_visit(n)
        return ast.copy_location(ast.ListComp(elt=n.elt, generators=n.generators), n)


def maybe_unbound(fn, names):
    """Of `names` (variables of fn's own scope), those fn may read before
    binding them on some path, by CPython's own flow analysis. The compiler
    loads a local it cannot prove bound with LOAD_FAST_CHECK, but it tracks
    only the first 64 locals precisely (past those, parameters included, every
    load after a block boundary is checked). So a probe copy of fn: no
    parameters (they read as globals there, which changes nothing for the
    others), and the names named first, in a batch, in dead code (`if 0:`
    binds nothing but takes the first slots). Generator expressions are read
    as list comprehensions (_GenAsList). A name still a cell after that (a def
    or lambda in fn reads it) is loaded with LOAD_DEREF, which the analysis
    does not follow: it is reported, never passed unread."""
    out = {}
    names = sorted(names)
    body = [_GenAsList().visit(copy.deepcopy(s)) for s in fn.body]
    for i in range(0, len(names), 60):
        batch = names[i:i + 60]
        probe = ast.AsyncFunctionDef(
            name="_probe", args=ast.arguments(posonlyargs=[], args=[], vararg=None, kwonlyargs=[],
                                              kw_defaults=[], kwarg=None, defaults=[]),
            body=[ast.If(test=ast.Constant(0), body=[ast.Assign(
                targets=[ast.Name(x, ast.Store()) for x in batch], value=ast.Constant(None))], orelse=[])]
            + body, decorator_list=[], returns=None, type_params=[])
        co = next(c for c in compile(ast.fix_missing_locations(ast.Module(body=[probe], type_ignores=[])),
                                     "probe", "exec").co_consts if getattr(c, "co_name", None) == "_probe")
        cells = [x for x in batch if x in co.co_cellvars]
        if cells:
            out.update({x: ["read by a def or lambda in the step: the probe cannot follow it"] for x in cells})
        probed = [x for x in batch if x not in co.co_cellvars]
        untracked = [x for x in probed if x not in co.co_varnames[:64]]
        if untracked:
            out.update({x: ["not in the slots the compiler tracks"] for x in untracked})
        for ins in dis.get_instructions(co):
            if ins.opname == "LOAD_FAST_CHECK" and ins.argval in probed:
                out.setdefault(ins.argval, []).append(ins.positions.lineno)
    return out


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef,
           ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def own_names(fn):
    """What fn's own scope sets and reads, as (name, node) lists. A nested
    function, lambda or comprehension is a read of every name it uses and does
    not set itself (a closure reads the turn's variable whenever it runs). An
    `except ... as e` name is not a set: Python unbinds it when the handler
    ends, so it carries nothing out of a step."""
    sets, reads = [], []

    def visit(node):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, _SCOPES):
                if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    sets.append((c.name, c))
                inner = {x.id for x in ast.walk(c) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
                inner |= {a.arg for x in ast.walk(c) if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                          for a in x.args.posonlyargs + x.args.args + x.args.kwonlyargs
                          + [v for v in (x.args.vararg, x.args.kwarg) if v is not None]}
                reads.extend((x.id, x) for x in ast.walk(c)
                             if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id not in inner)
                continue
            if isinstance(c, ast.Name):
                (sets if isinstance(c.ctx, (ast.Store, ast.Del)) else reads).append((c.id, c))
            elif isinstance(c, (ast.Import, ast.ImportFrom)):
                sets.extend(((a.asname or a.name.split(".")[0]), c) for a in c.names)
            visit(c)

    visit(fn)
    return sets, reads


core_sets, core_reads = own_names(core)
# A read by name through locals() (the context ledger's, until 2026-10-23)
# is no Name node, so the checks below would pass over it.
# builtins.locals() and a frame's f_locals count too.
_BY_NAME = ("locals", "vars", "eval", "exec", "_getframe", "currentframe", "f_locals")
_by_name = sorted(f"{fn.name}:{x.lineno} {ast.unparse(x)[:40]}" for fn in [core, *steps.values()] for x in ast.walk(fn)
                  if (isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in _BY_NAME)
                  or (isinstance(x, ast.Attribute) and x.attr in _BY_NAME))
check("the turn and its steps read no variable by name (locals, vars, eval, exec, a frame's locals)",
      not _by_name, _by_name)
_closures = [n for n in ast.walk(core) if n is not core and isinstance(n, _SCOPES[:3])]

stmts = {}
for s in ast.walk(core):
    if isinstance(s, (ast.Assign, ast.Expr)) and isinstance(s.value, ast.Await) \
            and isinstance(s.value.value, ast.Call) and isinstance(s.value.value.func, ast.Attribute) \
            and isinstance(s.value.value.func.value, ast.Name) and s.value.value.func.value.id == "self" \
            and s.value.value.func.attr in steps:
        stmts.setdefault(s.value.value.func.attr, []).append(s)
for name, fn in sorted(steps.items()):
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == name]
    at = stmts.get(name, [])
    check(f"{name}: called once, from the core, as a statement of its own",
          len(calls) == 1 and len(at) == 1, f"{len(calls)} calls, {len(at)} in the core")
    if len(at) != 1:
        continue
    call = at[0].value.value
    params = [a.arg for a in fn.args.args]
    kwonly = [a.arg for a in fn.args.kwonlyargs]
    pos = [ast.unparse(a) for a in call.args]
    bad_kw = [f"{k.arg}={ast.unparse(k.value)}" for k in call.keywords
              if not (isinstance(k.value, ast.Name) and k.value.id == k.arg)]
    check(f"{name}: every value it is handed is the core's variable of the same name",
          not bad_kw and pos == params[1:] and {k.arg for k in call.keywords} == set(kwonly)
          and not fn.args.vararg and not fn.args.kwarg and not fn.args.defaults
          and all(d is None for d in fn.args.kw_defaults),
          f"keywords {bad_kw}, positional {pos} for {params[1:]}")
    want = returned(fn)
    s = at[0]
    if want is None:
        # Or it can end the turn: then it hands back only the turn's reply, and
        # what it sets that the turn reads after it goes through `turn` (the
        # check below holds it to that).
        _ends = ends_turn(fn, s, core)
        check(f"{name}: returns its values once, at its end, by name; or every return hands back the "
              f"turn's reply and the core returns it when it is not None, right after the call", _ends)
        if not _ends:
            continue
        want = []
    else:
        if not want:
            got = None if isinstance(s, ast.Expr) else "an assignment"
        elif isinstance(s, ast.Assign) and len(s.targets) == 1:
            t = s.targets[0]
            got = [e.id if isinstance(e, ast.Name) else ast.unparse(e)
                   for e in (t.elts if isinstance(t, ast.Tuple) else [t])]
        else:
            got = "not one assignment"
        check(f"{name}: the core binds what it returns, by the same names, in order",
              (got is None) if not want else got == want, f"returns {want}, bound to {got}")
    # A turn variable the step reads: handed in, or bound by the step first on
    # every path.
    _params = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
    _step_sets = {n for n, _x in own_names(fn)[0]}
    _turn_names = {n for n, _x in core_sets} | {a.arg for a in core.args.posonlyargs + core.args.args
                                                + core.args.kwonlyargs}
    _unbound = maybe_unbound(fn, (_step_sets & _turn_names) - _params)
    check(f"{name}: every variable of the turn it reads is handed in, or bound by it first on every path",
          not _unbound, {k: sorted(set(v))[:4] for k, v in sorted(_unbound.items())})
    # A turn variable the step sets: one the turn sets before the call. Read
    # after the call, or by a closure made before it, it must come back.
    in_call = {id(x) for x in ast.walk(s)}
    step_sets = {n for n, _x in own_names(fn)[0]}
    turn_vars = {n for n, x in core_sets if x.lineno < s.lineno and id(x) not in in_call}
    early = {id(x) for c in _closures if c.lineno < s.lineno for x in ast.walk(c)}
    read_later = {n for n, x in core_reads if id(x) not in in_call
                  and (x.lineno > s.end_lineno or id(x) in early)}
    lost = sorted(n for n in (step_sets & turn_vars) - set(want) if n in read_later)
    check(f"{name}: every turn variable it sets that the turn reads after it is one it returns",
          not lost, f"set in the step, read by the turn after it, not returned: {lost}")

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nturn steps bind by name: all passed")
