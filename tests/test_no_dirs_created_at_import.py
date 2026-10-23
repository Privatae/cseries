"""Importing a module creates no directories.

Four modules once created their data dirs under /var/lib at import. A test run
by a user who cannot write /var/lib (a CI runner, a developer's account) then
failed while collecting every test that imports them, before any test ran.
Each dir is created where something is first written into it (parents=True),
or at server start (server/main.py's lifespan), and every reader checks that it
exists first.
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MAKERS = {"mkdir", "makedirs"}


def _is_main_block(node):
    """`if __name__ == "__main__":`, whose body runs only as a script."""
    t = node.test
    return (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
            and t.left.id == "__name__" and len(t.comparators) == 1
            and isinstance(t.comparators[0], ast.Constant)
            and t.comparators[0].value == "__main__")


def _evaluated(node):
    """The nodes under `node` that are evaluated when `node` is: a def's or a
    lambda's decorators and defaults, never its body; a class's body; not the
    body of an `if __name__ == "__main__":` block."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack += n.args.defaults + [d for d in n.args.kw_defaults if d is not None]
            stack += getattr(n, "decorator_list", [])
        elif isinstance(n, ast.If) and _is_main_block(n):
            stack += n.orelse
        else:
            stack += ast.iter_child_nodes(n)


def _calls(nodes):
    for n in nodes:
        if isinstance(n, ast.Call):
            f = n.func
            yield n, (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))


def _calls_at_import(tree):
    """The calls that create a directory when the module is imported: a
    mkdir/makedirs, or a call of a function defined at the top of the same
    module that makes one, itself or through another such function."""
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    runs = {name: [c for stmt in f.body for c in _calls(_evaluated(stmt))]
            for name, f in funcs.items()}
    makers = {name for name, cs in runs.items() if any(n in _MAKERS for _, n in cs)}
    grew = True
    while grew:
        grew = False
        for name, cs in runs.items():
            if name not in makers and any(isinstance(c.func, ast.Name) and n in makers
                                          for c, n in cs):
                makers.add(name)
                grew = True
    for c, n in _calls(_evaluated(tree)):
        if n in _MAKERS or (isinstance(c.func, ast.Name) and n in makers):
            yield c


def test_no_module_creates_a_directory_when_imported():
    found = []
    for top in ("server", "brain"):
        for path in sorted((_ROOT / top).rglob("*.py")):
            rel = path.relative_to(_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            found += [f"{rel}:{c.lineno}: {ast.unparse(c)}" for c in _calls_at_import(tree)]
    assert not found, "creates a directory at import:\n" + "\n".join(sorted(found))


def test_the_scan_sees_a_module_level_mkdir():
    src = (
        "import os\nfrom pathlib import Path\n"                          # 1-2
        "A = Path('/x')\nA.mkdir(parents=True, exist_ok=True)\n"         # 3-4
        "if True:\n    os.makedirs('/y', exist_ok=True)\n"               # 5-6
        "def later():\n    Path('/z').mkdir()\n"                         # 7-8
        "lazy = lambda: Path('/z').mkdir()\n"                            # 9
        "def _ensure():\n    later()\n"                                  # 10-11
        "_ensure()\n"                                                    # 12
        "class C:\n    def m(self):\n        later()\n"                  # 13-15
        "    B = os.makedirs('/c')\n"                                    # 16
        "def f(d=os.makedirs('/d')):\n    pass\n"                        # 17-18
        "if __name__ == '__main__':\n    later()\n"                      # 19-20
    )
    lines = sorted(c.lineno for c in _calls_at_import(ast.parse(src)))
    assert lines == [4, 6, 12, 16, 17]


# The modules that created their dirs at import; each must import with every
# directory creation refused.
_MODULES = ("server.routes.review", "server.routes.ladder",
            "server.routes.training_memory", "server.main")

_REFUSE_AND_IMPORT = """
import importlib, os, pathlib, sys
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/brain"]
def refuse(*a, **k):
    raise PermissionError(13, "directory creation refused at import", str(a[0]) if a else "")
pathlib.Path.mkdir = refuse
os.makedirs = refuse
os.mkdir = refuse
# Nothing under the state root exists, as on a machine no Host ever ran on (a
# CI runner). Where it does exist, a check of a dir there passes whether or not
# the import made it.
STATE = "/var/lib/cseries/"
def hidden(p):
    try:
        return (os.path.abspath(os.fspath(p)) + "/").startswith(STATE)
    except TypeError:
        return False
def absent(real):
    return lambda p, *a, **k: False if hidden(p) else real(p, *a, **k)
for mod, name in ((os.path, "exists"), (os.path, "isdir"), (os.path, "lexists"),
                  (pathlib.Path, "exists"), (pathlib.Path, "is_dir")):
    setattr(mod, name, absent(getattr(mod, name)))
# The image's own dirs under /app are not on this machine; the same switch
# tests/test_undefined_logger_except_branches.py uses, for those dirs only.
import starlette.staticfiles as sf
_init = sf.StaticFiles.__init__
def init(self, *a, **k):
    if str(k.get("directory", "")).startswith("/app/"):
        k["check_dir"] = False
    _init(self, *a, **k)
sf.StaticFiles.__init__ = init
for name in sys.argv[2:]:
    importlib.import_module(name)
main = sys.modules["server.main"]
print([str(r.app.directory) == str(main._USR_APPS)
       for r in main.app.routes if getattr(r, "name", "") == "user-apps"])
"""


def test_the_modules_import_with_directory_creation_refused():
    r = subprocess.run(
        [sys.executable, "-c", _REFUSE_AND_IMPORT, str(_ROOT), *_MODULES],
        capture_output=True, text=True, timeout=120, cwd=_ROOT)
    assert r.returncode == 0, r.stderr[-3000:]
    # /user-apps is mounted although its dir does not exist yet: the server
    # makes it at start, and an import that skipped the mount would leave the
    # Workbench's apps unserved until a restart.
    out = r.stdout.strip().splitlines()
    assert out and out[-1] == "[True]", r.stdout[-500:]
