"""scripts/test_route_param_markers_direct_calls.py, on a tree built for it.

The gate is a parser. A parser that stops resolving imports goes quietly blind
and keeps printing OK, so this holds it to known answers: a left-out marker
parameter is caught through each import form, and the safe forms (passing the
argument, the Annotated default, a route that normalises the parameter itself)
and the calls it cannot judge (*args / **kwargs) pass.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap

_ROOT = pathlib.Path(__file__).resolve().parents[1]
GATE = _ROOT / "scripts" / "test_route_param_markers_direct_calls.py"

ROUTES = '''
from typing import Annotated
from fastapi import APIRouter, Query, Depends
router = APIRouter()

@router.get("/a")
async def marker(status: str | None = None, limit: int = Query(5), offset: int = Query(0)):
    return limit, offset

@router.get("/b")
async def annotated(offset: Annotated[int, Query(ge=0)] = 0):
    return offset

@router.get("/c")
async def guarded(offset: int = Query(0)):
    offset = offset if isinstance(offset, int) else 0
    return offset

def not_a_route(offset=Query(0)):
    return offset

async def same_module_caller():
    return await marker(limit=1)
'''

CALLER = '''
from server.routes import things as T
from server.routes.things import marker as m2
import server.routes.things


async def go(kw, args):
    await T.marker(limit=1)                         # BAD: offset left out
    await m2(None, 1, 2)                            # ok: all positional
    await server.routes.things.marker(limit=1)      # BAD: dotted import
    await T.marker(limit=1, offset=0)               # ok
    await T.annotated()                             # ok: Annotated default
    await T.guarded()                               # ok: normalised in the body
    await T.marker(**kw)                            # not judged
    await T.marker(*args)                           # not judged
    T.not_a_route()                                 # not a route
'''


def _tree(tmp_path: pathlib.Path, caller: str) -> pathlib.Path:
    (tmp_path / "server" / "routes").mkdir(parents=True)
    (tmp_path / "server" / "__init__.py").write_text("")
    (tmp_path / "server" / "routes" / "__init__.py").write_text("")
    (tmp_path / "server" / "routes" / "things.py").write_text(textwrap.dedent(ROUTES))
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "caller.py").write_text(textwrap.dedent(caller))
    return tmp_path


def _run(root: pathlib.Path):
    return subprocess.run([sys.executable, str(GATE)], capture_output=True, text=True,
                          env={**os.environ, "APP_ROOT": str(root)}, timeout=120)


def test_left_out_marker_parameters_are_caught_through_each_import_form(tmp_path):
    p = _run(_tree(tmp_path, CALLER))
    assert p.returncode == 1, p.stdout + p.stderr
    flagged = [ln.split()[0] for ln in p.stdout.splitlines() if " calls " in ln]
    lines = CALLER.splitlines()
    bad = [f"scripts/caller.py:{i}" for i, ln in enumerate(lines, 1) if "# BAD" in ln]
    same_module = [f"server/routes/things.py:{i}" for i, ln in
                   enumerate(textwrap.dedent(ROUTES).splitlines(), 1)
                   if "await marker(limit=1)" in ln]
    assert sorted(flagged) == sorted(bad + same_module), p.stdout
    assert "offset=Query()" in p.stdout


def test_a_tree_whose_calls_pass_every_marker_is_green(tmp_path):
    good = "\n".join(ln for ln in CALLER.splitlines() if "# BAD" not in ln)
    root = _tree(tmp_path, good)
    routes = root / "server" / "routes" / "things.py"
    routes.write_text(routes.read_text().replace("await marker(limit=1)",
                                                 "await marker(limit=1, offset=0)"))
    p = _run(root)
    assert p.returncode == 0, p.stdout + p.stderr
    assert p.stdout.startswith("OK:")


def test_a_tree_with_no_marker_routes_is_a_failure_not_a_pass(tmp_path):
    (tmp_path / "x.py").write_text("def f():\n    return 1\n")
    p = _run(tmp_path)
    assert p.returncode == 1 and "parser no longer reads this tree" in p.stdout
