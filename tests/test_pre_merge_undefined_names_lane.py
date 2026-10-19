"""scripts/pre_merge_gates.sh's undefined-names lane, driven end to end.

Each case runs the REAL script against a throwaway repo that has one trivial
gate, so only this lane decides the result. A ref named origin/main stands in
for the remote; "no origin/main" is the CI shape (depth-1 fetch into a fresh
`git init`), where the lane reads every tracked file.

The cases pin the ways the lane could pass without checking anything: a
changed file whose path git C-quotes (non-ASCII), a file pyflakes cannot parse,
and pyflakes missing.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "pre_merge_gates.sh"

pytest.importorskip("pyflakes")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "commit.gpgsign=false", *args],
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    (r / "scripts" / "smoke_test.sh").write_text('python3 "$SMOKE_REPO/scripts/ok_gate.py"\n')
    (r / "scripts" / "ok_gate.py").write_text("print('ok')\n")
    (r / "scripts" / "gates-container-free.txt").write_text("")
    (r / "scripts" / "gates-container-free.d").mkdir()
    shutil.copy(_ROOT / "scripts" / "hang_watch.py", r / "scripts" / "hang_watch.py")
    (r / "clean.py").write_text("import os\nprint(os.sep)\n")
    (r / "gone.py").write_text("print(1)\n")
    _git(r, "init", "-q", "-b", "work")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "update-ref", "refs/remotes/origin/main", "HEAD")
    return r


def _run(repo, **env):
    # Its own results directory: the script records a pass, the last failures
    # and every gate's time there, and this repo's fake gate must not land in
    # the box's real history (the gate-results directory).
    e = {**os.environ, "PRE_MERGE_JOBS": "1", "GATE_RESULTS_DIR": str(repo.parent / "results"), **env}
    p = subprocess.run(["bash", str(_SCRIPT), str(repo)], capture_output=True,
                       text=True, env=e, timeout=300)
    return p.returncode, p.stdout + p.stderr


def _lane(out):
    return [l for l in out.splitlines() if "undefined names" in l]


def test_clean_change_passes_and_deleted_file_is_skipped(repo):
    (repo / "clean.py").write_text("import os\nprint(os.sep, 2)\n")      # unstaged
    (repo / "gone.py").unlink()                                       # deleted
    rc, out = _run(repo)
    assert rc == 0, out
    assert any("OK    undefined names (1 .py files" in l for l in _lane(out)), out


@pytest.mark.parametrize("where", ["committed", "staged", "untracked"])
def test_undefined_name_fails(repo, where):
    (repo / "bad.py").write_text("def f():\n    return DCN_DISPATCH_TIMEOUT\n")
    if where in ("committed", "staged"):
        _git(repo, "add", "bad.py")
    if where == "committed":
        _git(repo, "commit", "-q", "-m", "bad")
    rc, out = _run(repo)
    assert rc != 0, out
    assert "bad.py:2:12: undefined name 'DCN_DISPATCH_TIMEOUT'" in out, out


def test_path_git_would_quote_is_still_checked(repo):
    # Without -z git prints "caf\303\251 x.py" (quoted), the -f test misses
    # it and the file drops out of the lane unchecked.
    (repo / "café x.py").write_text("print(missing_name)\n")
    rc, out = _run(repo)
    assert rc != 0, out
    assert "undefined name 'missing_name'" in out, out


def test_file_pyflakes_cannot_parse_fails(repo):
    # pyflakes reports a syntax error on stderr and checks no names in that
    # file; filtering its output for "undefined name" read that as a pass.
    (repo / "clean.py").write_text("def f(:\n    return undefined_here\n")
    rc, out = _run(repo)
    assert rc != 0, out
    assert "pyflakes could not read these" in out, out
    assert "clean.py:1:" in out, out


def test_no_merge_base_checks_every_tracked_file(repo):
    # The CI shape: no origin/main. An undefined name in a file this change
    # never touched is still found, because every tracked file is read.
    (repo / "old.py").write_text("print(stale_name)\n")
    _git(repo, "add", "old.py")
    _git(repo, "commit", "-q", "-m", "old")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    rc, out = _run(repo)
    assert rc == 0, out          # with a merge base, only changed files: not read
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    rc, out = _run(repo)
    assert rc != 0, out
    assert "every tracked file (no merge base with origin/main)" in out, out
    assert "old.py:1:7: undefined name 'stale_name'" in out, out


def test_missing_pyflakes_fails_loudly(repo):
    rc, out = _run(repo, PRE_MERGE_PYFLAKES="python3 -m no_such_pyflakes_module")
    assert rc != 0, out
    assert "FAIL  undefined names — pyflakes is not available" in out, out
