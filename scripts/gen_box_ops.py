#!/usr/bin/env python3
"""Derive the bastion's management-op vocabulary by ASKING THE GO COMPILER.

WHY THIS EXISTS. KNOWN_BOX_OPS used to be hand-typed, and check_dcn_cn1.py
"pinned" it against a second hand-typed list whose comment claimed it was
"VERBATIM from the gateway". Both copies had the same 19 omissions, so the
gate compared a stale copy to a stale copy and passed while every heartbeating
bastion in production sat at posture=degraded.

WHY IT COMPILES INSTEAD OF PATTERN-MATCHING. The first cut of this file read the
Go with regexes and claimed it reported anything it could not resolve. That was
false in three measured shapes, all found in review:

  * an unbalanced `}` inside a `//` comment inside handlerOps closed the brace
    scan early — 12 of 37 ops recovered, and `problems` came back EMPTY;
  * a map entry whose value opened a multi-line literal was dropped by the
    line filter, silently;
  * a later `x = "y"` reassignment shadowed a const, silently.

A generator that silently under-reports is the exact failure it exists to
prevent, and `--write` would have shrunk the committed list to 12. Regexes
cannot see Go's syntax; the compiler can. So this copies the package to a
scratch directory, drops in a test that prints mgmtOpVocabulary() — the one
function the heartbeat actually reports from (enforcing.go:145) — and runs
`go test`. Comments, multi-line literals, const shadowing and whatever the
gateway does next are all handled, because the compiler handles them.

The gateway checkout is NEVER written to: the package is copied first and the
scratch copy is removed afterwards.

Requires the go toolchain and a gateway checkout. If either is missing it says
so and derives nothing — it never falls back to guessing. The behaviour half of
check_box_ops_sync.py deliberately needs neither: an unknown op has to be
survivable whether or not this list is current.

Usage:
    python3 scripts/gen_box_ops.py --src ../cseries-gateway
    python3 scripts/gen_box_ops.py --src ... --check     # diff, exit 1 on drift
    python3 scripts/gen_box_ops.py --src ... --write     # rewrite nac_dcn.py
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

# Where a gateway checkout usually is: beside this one. The committed list went
# stale the first time because a feature worktree six commits behind main was
# preferred over the main checkout; pass --src or CSERIES_GATEWAY_SRC to name
# the one you mean.
DEFAULT_SRC_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "cseries-gateway"),
]

_MARK_BEGIN = "# BEGIN GENERATED box ops"
_MARK_END = "# END GENERATED box ops"

# The probe. mgmtOpVocabulary() is unexported, so it has to be called from
# inside the package — which is why the package is copied rather than imported.
_PROBE = '''package main

import (
\t"fmt"
\t"testing"
)

func TestZZDumpBoxOpVocabulary(t *testing.T) {
\tfor _, op := range mgmtOpVocabulary() {
\t\tfmt.Println("VOCAB\\t" + op)
\t}
}
'''
_PROBE_NAME = "zz_box_ops_probe_test.go"


def find_src(explicit=None):
    """A gateway checkout containing client-go/mgmt.go, or None."""
    cands = [explicit] if explicit else []
    if not explicit and os.environ.get("CSERIES_GATEWAY_SRC"):
        cands.append(os.environ["CSERIES_GATEWAY_SRC"])
    cands += DEFAULT_SRC_CANDIDATES
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "client-go", "mgmt.go")):
            return c
    return None


def derive(src):
    """(ops, problems) — exactly what mgmtOpVocabulary() returns, per the compiler.

    problems is non-empty when the toolchain is missing, the package does not
    build, or the probe printed nothing. An EMPTY vocabulary is itself reported
    as a problem rather than returned: "no ops" is not something a real bastion
    reports, and returning it quietly would blank the committed list.
    """
    if shutil.which("go") is None:
        return [], ["the go toolchain is not on PATH; cannot derive"]
    scratch = tempfile.mkdtemp(prefix="box-ops-probe-")
    pkg = os.path.join(scratch, "client-go")
    try:
        shutil.copytree(os.path.join(src, "client-go"), pkg)
        with open(os.path.join(pkg, _PROBE_NAME), "w", encoding="utf-8") as fh:
            fh.write(_PROBE)
        try:
            p = subprocess.run(
                ["go", "test", "-count=1", "-v", "-run", "TestZZDumpBoxOpVocabulary", "."],
                cwd=pkg, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            return [], ["`go test` timed out deriving the vocabulary"]
        ops = sorted({ln[len("VOCAB\t"):].strip()
                      for ln in p.stdout.splitlines() if ln.startswith("VOCAB\t")})
        tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-15:])
        if not ops:
            return [], [f"the probe printed no ops (rc={p.returncode}):\n{tail}"]
        if p.returncode != 0:
            return ops, [f"`go test` exited {p.returncode}:\n{tail}"]
        return ops, []
    except (OSError, shutil.Error) as e:
        return [], [f"could not stage a scratch copy of {src}: {e}"]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _rev(src):
    """Best-effort provenance: the commit the vocabulary was read from."""
    try:
        p = subprocess.run(["git", "-C", src, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=30)
        if p.returncode == 0:
            return p.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def render(ops, src):
    """The generated KNOWN_BOX_OPS body, wrapped in its markers."""
    rev = _rev(src)
    lines = [f"    {_MARK_BEGIN} — do not hand-edit; scripts/gen_box_ops.py writes this.",
             f"    # mgmtOpVocabulary() as the Go compiler reports it, from {src}"
             + (f" ({rev})" if rev else "") + "."]
    row = "    "
    for op in ops:
        piece = f'"{op}", '
        if len(row) + len(piece) > 92:
            lines.append(row.rstrip())
            row = "    "
        row += piece
    if row.strip():
        lines.append(row.rstrip().rstrip(","))
    lines.append(f"    {_MARK_END}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=None)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    src = find_src(a.src)
    if not src:
        print("no gateway checkout found; set CSERIES_GATEWAY_SRC", file=sys.stderr)
        return 2
    ops, problems = derive(src)
    for p in problems:
        print("PROBLEM: " + p, file=sys.stderr)
    if problems:
        return 2

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    if a.check or a.write:
        sys.path[:0] = [root]
        from server.network import nac_dcn
        # Against the GENERATED set, not KNOWN_BOX_OPS: the latter also carries
        # _PENDING_BOX_OPS, ops from gateway PRs still in flight, which would
        # otherwise read as permanent drift.
        have = set(nac_dcn._GATEWAY_BOX_OPS)
        missing = sorted(set(ops) - have)
        extra = sorted(have - set(ops))
        if not missing and not extra:
            print(f"the generated box ops match {src} ({_rev(src)}) — {len(ops)} ops")
            return 0
        print(f"DRIFT vs {src} ({_rev(src)}):\n  missing={missing}\n  extra={extra}")
        if not a.write:
            # Only MISSING is a failure — see the asymmetry note in nac_dcn.py.
            # An op the host knows and the gateway does not cannot be reported by
            # any box, so it cannot produce a wrong verdict.
            return 1 if missing else 0
        target = os.path.join(root, "server", "network", "nac_dcn.py")
        text = open(target, encoding="utf-8").read()
        new = re.sub(r"[ \t]*" + re.escape(_MARK_BEGIN) + r".*?" + re.escape(_MARK_END),
                     render(ops, src), text, flags=re.S)
        if new == text:
            print(f"markers not found in {target}", file=sys.stderr)
            return 2
        open(target, "w", encoding="utf-8").write(new)
        print(f"rewrote {target}")
        return 0
    print("\n".join(ops))
    print(f"\n{len(ops)} ops from {src} ({_rev(src)})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
