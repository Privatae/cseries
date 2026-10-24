"""The turn's tool loop (EntityRuntime._run_tool_loop) shares values with the
rest of the turn (_send_message_core) only through `turn`
(runtime/turn_state.TurnState): what the loop and the helpers the core hands
it (_go_ahead_round, _on_resume, _keep_reply, _continue_card, the reply
recorder's getters) both read or set, and what the loop sets that the core
reads after it. So:

  - `turn = TurnState()` is made once, at the core's own level, and never
    rebound there or in the loop (handed in, a rebinding would leave the two
    reading different objects);
  - the loop is handed `turn`;
  - every `turn.X` in the turn's code is one of TurnState's slots (a slot is
    checked only when the line runs, so a misspelt one would fail in a turn,
    not here), and every slot is used.

That the loop sets nothing else the core reads after it, or a helper made
before it reads, is scripts/test_turn_steps_bind.py's check for every step of
the turn.

Pure source + AST checks, so this runs outside the image.

  APP_ROOT=<repo> python3 scripts/test_turn_state_shared.py
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


_paths = turn_paths(ROOT)
with open(_paths[0], encoding="utf-8") as f:
    tree = ast.parse(f.read())
with open(os.path.join(os.path.dirname(_paths[0]), "turn_state.py"), encoding="utf-8") as f:
    ts_tree = ast.parse(f.read())
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "EntityRuntime")
core = next(n for n in cls.body if isinstance(n, _FUNCS) and n.name == "_send_message_core")
loop_fn = next((n for n in cls.body if isinstance(n, _FUNCS) and n.name == "_run_tool_loop"), None)
check("the tool loop is a method of its own (_run_tool_loop)", loop_fn is not None)
fns = [core] + ([loop_fn] if loop_fn is not None else [])


def binds_turn(fn):
    """Lines where fn binds `turn`: an assignment or del, and the binding
    forms that are not a Name (except-as, import-as, a def or class, a match
    capture)."""
    out = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "turn"
           and isinstance(n.ctx, (ast.Store, ast.Del))]
    out += [n.lineno for n in ast.walk(fn)
            if (isinstance(n, (ast.ExceptHandler, *_FUNCS, ast.ClassDef, ast.MatchAs, ast.MatchStar))
                and n is not fn and getattr(n, "name", None) == "turn")
            or (isinstance(n, (ast.Import, ast.ImportFrom))
                and any((a.asname or a.name.split(".")[0]) == "turn" for a in n.names))]
    return out


# ── turn is made once and never rebound ─────────────────────────────────────
_made = [s for s in core.body if isinstance(s, ast.Assign) and ast.unparse(s) == "turn = TurnState()"]
_binds = binds_turn(core)
check("turn = TurnState() is made once, at the core's own level, and never rebound",
      len(_made) == 1 and len(_binds) == 1, f"made {len(_made)}, bound at {_binds}")
if loop_fn is not None:
    check("the loop is handed turn and never rebinds it",
          "turn" in [a.arg for a in loop_fn.args.kwonlyargs] and not binds_turn(loop_fn), binds_turn(loop_fn))
    _calls = [n for n in ast.walk(core) if isinstance(n, ast.Call) and ast.unparse(n.func) == "self._run_tool_loop"]
    check("...the core's own turn",
          len(_calls) == 1 and any(k.arg == "turn" and ast.unparse(k.value) == "turn" for k in _calls[0].keywords))

# ── every turn.X is a slot ──────────────────────────────────────────────────
_ts = next(n for n in ts_tree.body if isinstance(n, ast.ClassDef) and n.name == "TurnState")
_slots = next((ast.literal_eval(s.value) for s in _ts.body if isinstance(s, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "__slots__" for t in s.targets)), ())
_used = {n.attr for fn in fns for n in ast.walk(fn) if isinstance(n, ast.Attribute)
         and isinstance(n.value, ast.Name) and n.value.id == "turn"}
check("TurnState declares its slots", bool(_slots))
check("every turn.X the turn uses is one of TurnState's slots", _used <= set(_slots), sorted(_used - set(_slots)))
check("every slot is used (none left behind by a later change)", set(_slots) <= _used, sorted(set(_slots) - _used))

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nturn state shared: all passed")
