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

The other direction holds too: a variable of the turn that a step sets, and
that the turn reads after the step (in its own code, in a closure, or by name
through the context ledger's locals()), is one the step returns. A step's
variables are its own, so one it sets and does not return leaves the turn
reading the value from before the step, with nothing raised.

Pure source + AST checks, so this runs outside the image.

  APP_ROOT=<repo> python3 scripts/test_turn_steps_bind.py
"""
import ast
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
        elif (isinstance(e, ast.Call) and ast.unparse(e.func) == "locals().get" and len(e.args) == 1
              and isinstance(e.args[0], ast.Constant)):
            out.append(e.args[0].value)
        else:
            return None
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
# The context ledger reads the turn's variables by name: _loc = locals(), then
# _loc.get("history_msgs") and the like.
_loc_vars = {t.id for s in ast.walk(core) if isinstance(s, ast.Assign) and isinstance(s.value, ast.Call)
             and ast.unparse(s.value) == "locals()" for t in s.targets if isinstance(t, ast.Name)}
core_reads += [(x.args[0].value, x) for x in ast.walk(core)
               if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) and x.func.attr == "get"
               and isinstance(x.func.value, ast.Name) and x.func.value.id in _loc_vars
               and x.args and isinstance(x.args[0], ast.Constant) and isinstance(x.args[0].value, str)]
check("the context ledger's locals() read is found", bool(_loc_vars))
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
        check(f"{name}: returns its values once, at its end, by name", False)
        continue
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
