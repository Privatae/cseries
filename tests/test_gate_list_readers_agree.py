"""The three readers of the container-free gate list build the SAME gate set.

scripts/gates-container-free.d/ is read three times: by smoke_test.sh (runs the
gates at ship time), by pre_merge_gates.sh (runs them at merge time) and by
scripts/test_gate_coverage.py (counts a gate as wired). When the coverage gate
counts a file the two runners skip, that gate is "accounted for" and runs
nowhere, with every lane green: the condition the coverage gate exists to stop.
Measured once: a gate file committed as a
symlink was counted by the coverage gate (os.path.isfile follows the link) and
skipped by both runners (`find -type f` does not), so smoke ran 0 gates and
the coverage gate passed.

Each case runs the REAL reader code, cut out of the two scripts, against a
throwaway tree, and requires: the coverage gate refuses the tree, or all three
readers name the same gates. A stand-in python3 on PATH records which gate
smoke would run instead of running it.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
_LIST = "gates-container-free"


def _block(script: str, start: str) -> str:
    """From the line that assigns the list path to the first column-0 `fi`."""
    lines = (_SCRIPTS / script).read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if re.match(start, line):
            for j in range(i, len(lines)):
                if lines[j] == "fi":
                    return "\n".join(lines[i:j + 1])
    pytest.fail(f"scripts/{script} no longer has its {_LIST} reader where this test "
                f"looks for it (a line matching {start!r}). Point this test at the "
                "new reader rather than deleting it.")


def _coverage():
    spec = importlib.util.spec_from_file_location("tgc", _SCRIPTS / "test_gate_coverage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _smoke_runs(repo: pathlib.Path, tmp: pathlib.Path) -> list[str]:
    """The gates smoke_test.sh's container-free loop would run, in its order."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "python3"
    fake.write_text('#!/bin/sh\nbasename "$1" >> "$GATE_LOG"\n')
    fake.chmod(0o755)
    log = tmp / "smoke-ran.log"
    log.write_text("")
    body = _block("smoke_test.sh", r'GATE_LIST="\$SMOKE_REPO/scripts/' + _LIST + r'\.txt"')
    # The loop writes each gate's output under $SMOKE_SCRATCH, which smoke_test.sh
    # makes before it; the throwaway tree has no pass record, so the "already
    # passed" check falls through to reading the list, as on a first run.
    scratch = tmp / "scratch"
    scratch.mkdir(exist_ok=True)
    script = ("set -euo pipefail\nred(){ :; }; green(){ :; }; yellow(){ :; }\n"
              f'failed=0\nSMOKE_REPO="$1"\nSMOKE_SCRATCH="$2"\n{body}\n')
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GATE_LOG": str(log)}
    p = subprocess.run(["bash", "-c", script, "smoke", str(repo), str(scratch)], env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    return log.read_text().split()


def _pre_merge_reads(repo: pathlib.Path) -> list[str]:
    """The SHARED array pre_merge_gates.sh builds from the list."""
    body = _block("pre_merge_gates.sh", r'SHARED_LIST="\$REPO/scripts/' + _LIST + r'\.txt"')
    script = (f'set -uo pipefail\nred(){{ :; }}\nREPO="$1"\n{body}\n'
              'printf "%s\\n" "${SHARED[@]}"\n')
    p = subprocess.run(["bash", "-c", script, "pmg", str(repo)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout.split()


def _readers(repo: pathlib.Path, tmp: pathlib.Path):
    gates, left, problems = _coverage().read_gate_dir(str(repo / "scripts"), _LIST)
    return _smoke_runs(repo, tmp), _pre_merge_reads(repo), gates, bool(left or problems)


def _tree(tmp: pathlib.Path, files: dict[str, bytes]) -> pathlib.Path:
    repo = tmp / "repo"
    d = repo / "scripts" / f"{_LIST}.d"
    d.mkdir(parents=True)
    (repo / "scripts" / f"{_LIST}.txt").write_text("# header only\n")
    for gate in ("test_a.py", "test_b.py"):
        (repo / "scripts" / gate).write_text("print('ok')\n")
    for name, data in files.items():
        (d / name).write_bytes(data)
    return repo


# Shapes a hand-written or tool-written gate file takes. Each must reach all
# three readers as exactly the gates listed here.
@pytest.mark.parametrize("files, want", [
    ({"test_a.txt": b"# why\ntest_a.py\n", "test_b.txt": b"test_b.py\n"}, ["test_a.py", "test_b.py"]),
    ({"test_a.txt": b"# why\r\ntest_a.py\r\n"}, ["test_a.py"]),                       # CRLF
    ({"test_a.txt": b"# why\ntest_a.py", "test_b.txt": b"test_b.py"},                # no final newline
     ["test_a.py", "test_b.py"]),
    ({"test_a.txt": b"test_a.py   # needs nothing live\n"}, ["test_a.py"]),         # inline comment
], ids=["plain", "crlf", "no-final-newline", "inline-comment"])
def test_every_reader_reads_the_same_gates(tmp_path, files, want):
    smoke, pmg, cov, refused = _readers(_tree(tmp_path, files), tmp_path)
    assert not refused
    assert sorted(smoke) == sorted(pmg) == sorted(cov) == want, (smoke, pmg, cov)


def _symlinked(repo):
    real = repo / "scripts" / "test_a.list"
    real.write_text("test_a.py\n")
    (repo / "scripts" / f"{_LIST}.d" / "test_a.txt").symlink_to(real)


def _hidden(repo):
    (repo / "scripts" / f"{_LIST}.d" / ".test_b.txt").write_text("test_b.py\n")


def _directory(repo):
    sub = repo / "scripts" / f"{_LIST}.d" / "test_b.txt"
    sub.mkdir()
    (sub / "test_b.txt").write_text("test_b.py\n")


def _dangling(repo):
    (repo / "scripts" / f"{_LIST}.d" / "test_b.txt").symlink_to(repo / "nowhere")


def _left_in_header(repo):
    (repo / "scripts" / f"{_LIST}.txt").write_text("test_b.py\n")


def _stray(repo):
    (repo / "scripts" / f"{_LIST}.d" / "test_b.txt.orig").write_text("test_b.py\n")


# Entries no reader was written for. Refusing one is fine; reading it in one
# reader and not another is the dead-gate hole.
@pytest.mark.parametrize("shape", [_symlinked, _hidden, _directory, _dangling,
                                   _left_in_header, _stray],
                         ids=lambda f: f.__name__.strip("_"))
def test_an_odd_entry_is_refused_or_read_alike(tmp_path, shape):
    repo = _tree(tmp_path, {"test_a.txt": b"test_a.py\n"} if shape is not _symlinked else {})
    shape(repo)
    smoke, pmg, cov, refused = _readers(repo, tmp_path)
    assert refused or sorted(smoke) == sorted(pmg) == sorted(cov), (
        f"the coverage gate counts {sorted(cov)} as wired, smoke runs {sorted(smoke)} "
        f"and pre-merge runs {sorted(pmg)}: a gate counted by one and run by "
        "neither is the dead gate the coverage ratchet exists to catch")


def test_the_real_list_reaches_pre_merge_and_coverage_alike():
    # Smoke's loop is left out here: it forks several processes per gate, which
    # took 38 s over the real list (measured at load 5 on 8 cores). Its
    # reader is the same code as pre-merge's, held to it by the cases above.
    gates, left, problems = _coverage().read_gate_dir(str(_SCRIPTS), _LIST)
    pmg = _pre_merge_reads(_ROOT)
    assert not left and not problems, (left, problems)
    assert len(gates) > 0
    assert len(pmg) == len(set(pmg)), "a gate is listed twice"
    assert sorted(pmg) == sorted(gates)
