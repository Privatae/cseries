"""runtime/tools.py and the tools_<category> modules split out of it (2026-10-23)
stay one module to every caller.

  1. Every runtime/tools_*.py is imported by tools.py, and every module tools.py
     imports that way exists: scripts/tools_source.py reads the list off those
     imports, so a module tools.py does not import is a module no gate reads.
  2. No split module imports tools.py at its top level: tools.py imports them
     first, so that import would be circular.
  3. Every name a split module defines at its top level is tools.py's too, as
     the same object, except a name its module rebinds with `global` (a copy
     on tools.py would go stale).
  4. tools_source() reads tools.py and every split module.
  5. Rebinding a name on tools.py (a test standing in for a helper) reaches
     the code that moved, in every split module: tools._SPLIT_MODULES lists
     each one, the module that holds the name reads the new value, and the
     old one again once it is put back. tools.py's own __name__, __file__,
     __spec__ and __loader__ (which importlib.reload sets) are never passed on.

Container-free: imports the module and reads the source.
APP_ROOT=<repo> python3 scripts/test_tools_split.py
"""
import ast
import collections
import glob
import importlib
import os
import sys

from tools_source import split_modules, tools_paths, tools_source

ROOT = os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "brain"))
RT = os.path.join(ROOT, "brain", "mastercode", "runtime")
FAILS = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def top_names(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        ts = node.targets if isinstance(node, ast.Assign) else [node.target]
        return [e.id for t in ts for e in ast.walk(t) if isinstance(e, ast.Name)]
    return []


with open(os.path.join(RT, "tools.py"), encoding="utf-8") as f:
    tools_text = f.read()
imported = split_modules(tools_text)
on_disk = sorted(os.path.basename(p)[:-3] for p in glob.glob(os.path.join(RT, "tools_*.py")))

print("1. tools.py imports every split module, and only modules that exist")
check("there are split modules", bool(imported))
check("every runtime/tools_*.py is imported by tools.py", set(on_disk) <= set(imported),
      sorted(set(on_disk) - set(imported)))
check("every module tools.py imports exists", set(imported) <= set(on_disk),
      sorted(set(imported) - set(on_disk)))

print("2. no split module imports tools.py at its top level")
for m in imported:
    p = os.path.join(RT, m + ".py")
    if not os.path.exists(p):
        continue
    body = ast.parse(open(p, encoding="utf-8").read()).body
    back = [n.lineno for n in body
            if (isinstance(n, ast.ImportFrom) and (n.module == "mastercode.runtime.tools"
                                                   or (n.module == "mastercode.runtime"
                                                       and any(a.name == "tools" for a in n.names))))
            or (isinstance(n, ast.Import) and any(a.name == "mastercode.runtime.tools" for a in n.names))]
    check(f"{m}.py does not import tools.py", not back, f"lines {back}")

print("3. every name a split module defines is tools.py's, as the same object")
tools = importlib.import_module("mastercode.runtime.tools")
defined = {}
for m in imported:
    p = os.path.join(RT, m + ".py")
    if not os.path.exists(p):
        continue
    tree = ast.parse(open(p, encoding="utf-8").read())
    rebound = {g for e in ast.walk(tree) if isinstance(e, ast.Global) for g in e.names}
    names = [nm for n in tree.body for nm in top_names(n) if nm not in rebound and nm != "log"]
    defined[m] = names
    mod = importlib.import_module("mastercode.runtime." + m)
    missing = [nm for nm in names if getattr(tools, nm, None) is not getattr(mod, nm)]
    check(f"{m}: {len(names)} names re-exported by tools.py", not missing, missing[:8])
    stale = [g for g in rebound if g in vars(tools)]
    check(f"{m}: no stale copy on tools.py of a name it rebinds", not stale, stale)

# `is` cannot tell a re-export from a name bound again to an equal cached value
# (a small int, None, an interned string), and the rebinding block passes a name
# on by that same identity: so each name is bound by one file only.
_own = []
for n in ast.parse(tools_text).body:
    if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("mastercode.runtime.tools_"):
        continue
    if isinstance(n, (ast.Import, ast.ImportFrom)):
        _own += [(a.asname or a.name).split(".")[0] for a in n.names]
    else:
        _own += top_names(n)
_split_names = collections.Counter(nm for m in imported for nm in set(defined.get(m, ())))
_shadowed = sorted(set(_own) & set(_split_names))
check("tools.py binds no name a split module defines", not _shadowed, _shadowed[:8])
_twice = sorted(nm for nm, k in _split_names.items() if k > 1)
check("no name is defined by two split modules", not _twice, _twice[:8])

print("4. tools_source() reads tools.py and every split module")
src = tools_source(ROOT)
paths = tools_paths(ROOT)
check("tools_paths() is tools.py and the imported modules, in order",
      [os.path.basename(p) for p in paths] == ["tools.py"] + [m + ".py" for m in imported])
for p in paths:
    with open(p, encoding="utf-8") as f:
        text = f.read()
    check(f"tools_source() holds {os.path.basename(p)}", text in src)

print("5. a name rebound on tools.py is rebound where the code that reads it lives")
# The rebinding reaches only the modules tools.py lists in _SPLIT_MODULES, a
# list kept beside the imports: a module imported but left off it would be
# read by every gate and reached by no stand-in.
_listed = [mm.__name__ for mm in getattr(tools, "_SPLIT_MODULES", ())]
check("tools._SPLIT_MODULES is every split module tools.py imports",
      sorted(_listed) == sorted("mastercode.runtime." + m for m in imported),
      sorted(set("mastercode.runtime." + m for m in imported) ^ set(_listed)))


class _StandIn:
    pass


for m in imported:
    _name = next((nm for nm in defined.get(m, ()) if hasattr(tools, nm)), None)
    if _name is None:
        continue
    _mod = importlib.import_module("mastercode.runtime." + m)
    _real = getattr(tools, _name)
    _stand_in = _StandIn()
    setattr(tools, _name, _stand_in)
    _reached = getattr(_mod, _name) is _stand_in
    setattr(tools, _name, _real)
    check(f"rebinding tools.{_name} rebinds it in {m}", _reached)
    check(f"...and putting it back puts it back in {m}", getattr(_mod, _name) is _real)

# importlib.reload(tools) first sets tools.py's own __name__, __file__,
# __spec__ and __loader__ again; passed on, every split module would be
# renamed to tools.py, and reloading one of them would reload tools.py.
_own = {m: {k: vars(importlib.import_module("mastercode.runtime." + m)).get(k)
            for k in ("__name__", "__file__", "__spec__", "__loader__")} for m in imported}
for k in ("__name__", "__file__", "__spec__", "__loader__"):
    setattr(tools, k, getattr(tools, k))
_renamed = []
for m in imported:
    _mod = importlib.import_module("mastercode.runtime." + m)
    for k, v in _own[m].items():
        if vars(_mod).get(k) is not v:
            _renamed.append(f"{m}.{k}")
            setattr(_mod, k, v)
check("setting tools.py's own __name__/__file__/__spec__/__loader__ leaves each split module's own",
      not _renamed, _renamed[:6])

# A name a split module keeps for itself (rebound there with `global`, so not
# re-exported) is not tools.py's: set on tools.py it would reach nothing, so a
# test setting it there (plainly, or mock.patch.object(..., create=True)) is
# refused, and the module's own is left as it was.
_kept = next(((m, g) for m in imported
              for g in sorted({g for e in ast.walk(ast.parse(open(os.path.join(RT, m + ".py"), encoding="utf-8").read()))
                               if isinstance(e, ast.Global) for g in e.names})), None)
if _kept is not None:
    from unittest import mock
    _mod = importlib.import_module("mastercode.runtime." + _kept[0])
    _before = vars(_mod).get(_kept[1], _StandIn)
    _refusals = 0
    try:
        setattr(tools, _kept[1], _StandIn())
    except AttributeError:
        _refusals += 1
    try:
        with mock.patch.object(tools, _kept[1], _StandIn(), create=True):
            pass
    except AttributeError:
        _refusals += 1
    check(f"setting tools.{_kept[1]} ({_kept[0]}'s own) is refused, plainly and with create=True",
          _refusals == 2, _refusals)
    check(f"...and {_kept[0]}.{_kept[1]} is as it was, with nothing left on tools.py",
          vars(_mod).get(_kept[1], _StandIn) is _before and _kept[1] not in vars(tools))

# A delete on tools.py takes the name from the module that holds it, as it did
# when the name lived here; putting it back puts it back there.
_m0 = next(m for m in imported if defined.get(m))
_name = next(nm for nm in defined[_m0] if hasattr(tools, nm))
_mod = importlib.import_module("mastercode.runtime." + _m0)
_real = getattr(tools, _name)
delattr(tools, _name)
_gone = _name not in vars(_mod)
setattr(tools, _name, _real)
check(f"deleting tools.{_name} deletes it in {_m0}", _gone)
check(f"...and putting it back puts it back in {_m0}", vars(_mod).get(_name) is _real)

# A name tools.py held and a delete took is tools.py's to put back, even while
# the module holds a stand-in of its own (a test that patched the module
# directly, then deleted the name here): the refusal is for names tools.py
# never held, and refusing here would break the undo halfway.
_direct = _StandIn()
vars(_mod)[_name] = _direct
delattr(tools, _name)
try:
    setattr(tools, _name, _real)
    _put_back = vars(tools).get(_name) is _real
except AttributeError:
    _put_back = False
_kept_own = vars(_mod).get(_name) is _direct
vars(_mod)[_name] = _real
vars(tools)[_name] = _real
check(f"tools.{_name}, deleted while {_m0} held a stand-in of its own, can be put back", _put_back)
check(f"...and {_m0}'s own stand-in is left to its own undo", _kept_own)

if FAILS:
    print(f"\n{len(FAILS)} FAIL")
    sys.exit(1)
print("\nall pass")
