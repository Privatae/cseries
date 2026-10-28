#!/usr/bin/env python3
# gate-scope: whole-tree — every sidecars/harness-*/Dockerfile
"""Gate: an acceptance check that needs a tool the harness image does not ship
is REFUSED at PLAN time (add_phase / set_phase_spec / plan) — never authored
into a phase that can only say `command not found`. The BUILD door and the
auto-advance chain no longer refuse: checks are information only and never
change a build's outcome, so a grandfathered node/rg check just runs on the pod
and its envelope rides the row.

Measured on one factory build (kimi): the
host wrote `node -e …` as the phase's acceptance; no harness image ships Node
(sidecars/harness-*/Dockerfile: python:3.14-slim-bookworm + git ca-certificates
curl tini, codex + ripgrep), so the pod answered `bash: node: command not
found`, the phase failed on a missing tool with the work itself fine, and with
auto-deliver on a repair was dispatched at a healthy result. The fix is the
refusal for node, not Node in every image.

What this gate holds, container-free (no docker, no DB, no pod):

  1. The toolset table (system-apps/_harness_runtime/image_tools.py) is tied
     to the Dockerfiles it describes: same base digest on every image, the apt
     line of each Dockerfile equals the table's, every harness dir has a row
     and vice versa, every extra package maps to the executables it adds, and
     the bash builtin/keyword sets equal this box's `compgen -b` / `compgen -k`.
     (The executable lists themselves are measured with docker —
     scripts/measure_harness_image_tools.py — and cannot be re-derived here.)
  2. The parser: refuses node/npm/npx/make/jq in every shape the host writes
     them (pipelines, &&, subshells, $(…), backticks, bash -c "…", env/timeout/
     xargs wrappers, absolute PATH paths); accepts builtins, keywords, shipped
     tools, worktree scripts, $VAR commands, `command -v` probes and heredoc
     bodies; and is PER IMAGE — `rg` passes on codex and is refused on kimi
     with the reason saying codex has it.
  3. The doors, EXECUTED against stub pools: add_phase, set_phase_spec and
     build(action='plan') refuse a check no image can run; build(action='build')
     and the chain-side _run_phase_build do NOT refuse (they open the row, flip
     the phase to running and wake the pod with the check handed through).
  4. The tool schema tells the host the constraint and that a violation is
     refused at plan time.

Run from the repo root:  APP_ROOT=<repo> python3 scripts/test_acceptance_tool_gate.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
sys.path[:0] = [str(_ROOT), str(_ROOT / "brain"), str(_ROOT / "system-apps")]

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(("[accept-tools] ok   " if cond else "[accept-tools] FAIL ") + label
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


def _drain_loop() -> None:
    """The codex-ALLOWED build door case starts a detached _run_phase_build
    task; cancel it on our loop so it does not die noisily at interpreter
    exit ("Task was destroyed but it is pending!")."""
    pending = [t for t in asyncio.all_tasks(_LOOP) if not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        _LOOP.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


def main() -> int:
    from _harness_runtime import image_tools as T

    # ── 1. the table is tied to the Dockerfiles ──────────────────────────────
    dockerfiles = sorted((_ROOT / "sidecars").glob("harness-*/Dockerfile"))
    ids_on_disk = {p.parent.name[len("harness-"):] for p in dockerfiles}
    check("every sidecars/harness-*/ has a row in the table and vice versa",
          ids_on_disk == set(T.known_images()),
          f"disk={sorted(ids_on_disk)} table={sorted(T.known_images())}")
    apt_re = re.compile(r"apt-get install -y --no-install-recommends\s*\\?\s*\n?\s*([^\n]*?)\s*\\?\s*\n?\s*&& rm -rf", re.S)
    for df in dockerfiles:
        hid = df.parent.name[len("harness-"):]
        src = df.read_text()
        froms = re.findall(r"^FROM\s+(\S+)", src, re.M)
        check(f"{hid}: FROM is the recorded base digest",
              froms == [f"{T.BASE_IMAGE}@{T.BASE_IMAGE_DIGEST}"], f"got {froms}")
        m = apt_re.search(src)
        pkgs = set(m.group(1).split()) if m else set()
        check(f"{hid}: apt line == table ({sorted(pkgs)})",
              pkgs == set(T.IMAGE_APT_PACKAGES.get(hid, ())),
              f"table={sorted(T.IMAGE_APT_PACKAGES.get(hid, ()))}")
        for extra_pkg in pkgs - T.SHARED_APT_PACKAGES:
            execs = T.APT_PACKAGE_EXECUTABLES.get(extra_pkg)
            check(f"{hid}: extra package {extra_pkg} maps to executables",
                  bool(execs), "add it to APT_PACKAGE_EXECUTABLES (measure first)")
            check(f"{hid}: those executables are in the image's extras",
                  bool(execs) and execs <= T.IMAGE_EXTRA_EXECUTABLES.get(hid, frozenset()),
                  f"{sorted(execs or [])} vs {sorted(T.IMAGE_EXTRA_EXECUTABLES.get(hid, []))}")
    for h in T.HIGHLIGHT_TOOLS:
        check(f"highlighted tool `{h}` is in the common set", h in T.COMMON_EXECUTABLES)
    check("the common set is the measured 450 names (not an empty stub)",
          len(T.COMMON_EXECUTABLES) >= 400, str(len(T.COMMON_EXECUTABLES)))
    for absent in ("node", "npm", "npx", "make", "jq", "docker", "yarn", "pnpm", "deno", "bun", "go", "cargo"):
        check(f"no image ships `{absent}`", not T.images_shipping(absent), str(T.images_shipping(absent)))
    check("`rg` is on codex only", T.images_shipping("rg") == ("codex",), str(T.images_shipping("rg")))
    if shutil.which("bash"):
        b = set(subprocess.run(["bash", "-c", "compgen -b"], capture_output=True, text=True).stdout.split())
        k = set(subprocess.run(["bash", "-c", "compgen -k"], capture_output=True, text=True).stdout.split())
        check("BASH_BUILTINS == this bash's compgen -b", b == T.BASH_BUILTINS,
              f"+{sorted(b - T.BASH_BUILTINS)} -{sorted(T.BASH_BUILTINS - b)}")
        check("BASH_KEYWORDS == this bash's compgen -k", k == T.BASH_KEYWORDS,
              f"+{sorted(k - T.BASH_KEYWORDS)} -{sorted(T.BASH_KEYWORDS - k)}")

    # ── 2. the parser ────────────────────────────────────────────────────────
    REFUSE = [  # (cmd, image, tool named)
        ('node -e "require(\'./dist/index.js\')"', "kimi", "node"),
        ("npm test", "kimi", "npm"),
        ("npx jest --ci", "kimi", "npx"),
        ("cd app && npm run build", "kimi", "npm"),
        ("python3 -m pytest -q || node scripts/check.js", "kimi", "node"),
        ("echo \"$(node -v)\" | grep v2", "kimi", "node"),
        ("`node -v`", "kimi", "node"),
        ('bash -c "npx jest"', "kimi", "npx"),
        ('sh -ec "set -e; deno run x"', "kimi", "deno"),
        ("FOO=1 env BAR=2 node x", "kimi", "node"),
        ("timeout 30 npm test", "kimi", "npm"),
        ("timeout -k 5 30 -- make test", "kimi", "make"),
        ("ls | xargs node", "kimi", "node"),
        ("xargs -0 -I{} node {}", "kimi", "node"),
        ("(npm test)", "kimi", "npm"),
        ("/usr/bin/node x.js", "kimi", "/usr/bin/node"),
        ("diff <(node a) <(cat b)", "kimi", "node"),
        ("case $x in a) jq . f;; esac", "kimi", "jq"),
        ("sudo apt install x", "kimi", "sudo"),
        ("rg --files | head", "kimi", "rg"),            # per-image: codex has it
        ("npm test", None, "npm"),                       # authoring: no image has it
        # a function that itself calls a missing tool is refused naming the TOOL
        ("function g() { node x; }; g", "kimi", "node"),
        ("h() { npm test; } ; h", "kimi", "npm"),
        # Delta review 2026-09-06: a trailing `#` comment on line 1 must
        # not hide a `node` line below it (shlex's commenter ran to end of text).
        ("echo hi # x\nnode -v", "kimi", "node"),
        ("# checks\necho a # note\n\nnpm test", "kimi", "npm"),
    ]
    for cmd, img, tool in REFUSE:
        r = T.refusal(cmd, img)
        check(f"REFUSES {cmd!r} on {img or 'any image'}", bool(r), "accepted")
        check(f"...naming `{tool}`", bool(r) and f"`{tool}`" in r, r or "")
        check("...and the image",
              bool(r) and ((f"{img} image" in r) if img else ("none of the coding-engine images" in r)),
              r or "")
    r = T.refusal("rg --files | head", "kimi")
    check("the per-image reason says which image DOES ship it", bool(r) and "(codex does)" in r, r or "")
    ACCEPT = [
        ('bash -c "python3 -m pytest -q"', "kimi"),
        ("python3 -m pytest -q 2>&1 | tail -1", "kimi"),
        ("git diff --quiet", "kimi"),
        ("curl -sf http://127.0.0.1:8080/ -o /dev/null", "kimi"),
        ("grep -q 'ok' out.txt | wc -l", "kimi"),
        ("test -f dist/index.html && [ -s dist/index.html ]", "kimi"),
        ("for f in *.py; do python3 -m py_compile \"$f\"; done", "kimi"),
        ("./run_tests.sh", "kimi"),
        ("scripts/verify.sh --strict", "kimi"),
        ("command -v node >/dev/null || true", "kimi"),          # a probe, not a run
        ("python3 - <<'EOF'\nimport os\nassert os.path.exists('x')\nEOF", "kimi"),
        ("if [[ -f x ]]; then echo ok; else exit 1; fi", "kimi"),
        ("case $x in a) echo 1;; esac", "kimi"),
        ("FOO=bar python3 x.py", "kimi"),
        ("$PYTHON -m pytest", "kimi"),
        ("echo hi > out.txt; wc -l < out.txt", "kimi"),
        ("find . -name '*.py' | xargs python3 -m py_compile", "kimi"),
        ("timeout 60 python3 -m unittest discover", "kimi"),
        ("awk '{print $1}' f | sort | uniq -c", "kimi"),
        ("tar -tzf dist.tgz | grep index.html", "kimi"),
        ("rg --files | head", "codex"),                           # codex ships rg
        ("rg --files | head", None),                              # authoring: SOME image has it
        ("uvicorn --version", "claude-code"),                     # pip console script, measured
        ("true", "kimi"), (":", "kimi"), ("exit 0", "kimi"),
        # Review 2026-09-06: inline functions, leading fd redirects,
        # array literals — all were false refusals.
        ("check() { python3 -m pytest -q; }; check", "kimi"),
        ("check () { python3 -m pytest -q; } && check", "kimi"),
        ("function f { python3 x.py; }; f", "kimi"),
        ("function f() { git diff --quiet; }; f", "kimi"),
        ("2>/dev/null python3 x.py", "kimi"),
        ("exec 3>&1; ls", "kimi"),
        ("1>out.txt 2>&1 python3 x.py", "kimi"),
        ("arr=(a b); echo ${arr[0]}", "kimi"),
        ("local x=(node y); echo ok", "kimi"),          # array literal, not a run
        ("arr+=(c d)", "kimi"),
        # comments: a quoted `#` is a word, a full-line comment is nothing
        ('echo "a # b"', "kimi"),
        ("grep '#include' x.c", "kimi"),
        ("# full-line comment\npython3 -m pytest", "kimi"),
        ('printf "%s\\n" "one\ntwo" | wc -l', "kimi"),     # quoted newline stays one word
    ]
    check("`echo \"a # b\"` is ONE command with the quoted `#` kept",
          T.invoked_programs('echo "a # b"') == ["echo"], str(T.invoked_programs('echo "a # b"')))
    check("`echo hi # x\\nnode -v` sees node", T.invoked_programs("echo hi # x\nnode -v") == ["echo", "node"],
          str(T.invoked_programs("echo hi # x\nnode -v")))
    for cmd, img in ACCEPT:
        r = T.refusal(cmd, img)
        check(f"ACCEPTS {cmd!r} on {img or 'any image'}", r is None, r or "")
    for cmd, fn in (("function g() { node x; }; g", "g"), ("h() { npm test; } ; h", "h")):
        r = T.refusal(cmd, "kimi") or ""
        check(f"...and {cmd!r} names the tool, not the function `{fn}`", f"`{fn}`" not in r, r)
    # KNOWN, deliberate (review 2026-09-06): a venv console script is
    # not on the image PATH — the pod user is the non-root `harness`
    # (sidecars/harness-kimi/Dockerfile) and nothing installs pytest into the
    # image — so a bare `pytest` after `. .venv/bin/activate` IS refused,
    # while `python -m pytest` is accepted (python is on PATH; whether the
    # module exists is the python-modules gap this gate does not cover).
    # Pinned so nobody reads that refusal as a parser bug.
    check("KNOWN: `. .venv/bin/activate && pytest -q` is refused naming `pytest`",
          "`pytest`" in (T.refusal(". .venv/bin/activate && pytest -q", "kimi") or ""))
    check("KNOWN: `. .venv/bin/activate && python -m pytest` is accepted",
          T.refusal(". .venv/bin/activate && python -m pytest", "kimi") is None)
    check("an unparseable command is left to the pod, not refused",
          T.refusal("echo 'unterminated", "kimi") is None)

    # ── 3. the doors, executed ───────────────────────────────────────────────
    spec = importlib.util.spec_from_file_location(
        "phase_spec_gate", _ROOT / "scripts/test_phase_spec_gate.py")
    psg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(psg)  # defines FakePool/BuildPool; main() is __main__-guarded
    from mastercode.runtime import tools as _t
    import server.routes.review as _rv

    pool = psg.FakePool()
    go = lambda a: run(_t._exec_project_phase(a, 1, pool))  # noqa: E731
    bad = go({"op": "add_phase", "project_id": 9, "title": "x", "spec": "y",
              "acceptance": ["node -e 1"]})
    check("add_phase REFUSES a node check", bad["status"] == "error"
          and "`node`" in bad["result"] and "none of the coding-engine images" in bad["result"],
          str(bad))
    check("...and nothing landed", not pool.phases)
    ok = go({"op": "add_phase", "project_id": 9, "title": "x", "spec": "y",
             "acceptance": ["python3 -m pytest -q", "git diff --quiet"]})
    check("add_phase ACCEPTS shipped-tool checks", ok["status"] == "ok", str(ok))
    some = go({"op": "add_phase", "project_id": 9, "title": "x", "spec": "y",
               "acceptance": ["rg --files"]})
    check("add_phase ACCEPTS a check SOME image can run (engine not chosen yet)",
          some["status"] == "ok", str(some))
    pid = ok["result"]["id"]
    rep = go({"op": "set_phase_spec", "project_id": 9, "phase_id": pid, "spec": "y2",
              "acceptance": ["npm test"]})
    check("set_phase_spec REFUSES a npm check", rep["status"] == "error" and "`npm`" in rep["result"], str(rep))
    check("...and left the stored acceptance alone",
          json.loads(pool.phases[pid]["acceptance"])[0]["cmd"] == "python3 -m pytest -q")
    rep2 = go({"op": "set_phase_spec", "project_id": 9, "phase_id": pid, "spec": "y2"})
    check("set_phase_spec with no acceptance is untouched by the gate", rep2["status"] == "ok", str(rep2))

    plan = run(_t._exec_build({"action": "plan", "project_id": 9, "intent": "an app",
                               "phases": [{"title": "A", "spec": "brief",
                                           "acceptance": ["node -e 1"]}]}, 1, pool, None))
    check("build(plan) REFUSES the whole plan on a node check, naming the phase",
          plan["status"] == "error" and "plan refused" in plan["result"]
          and "phase 'A'" in plan["result"] and "`node`" in plan["result"], str(plan))

    async def _resolve_kimi(req=None, model=None):
        return ("kimi", None, "")

    async def _resolve_codex(req=None, model=None):
        return ("codex", None, "")

    _orig = _rv.resolve_build_target
    try:
        _rv.resolve_build_target = _resolve_kimi
        bp = psg.BuildPool({"id": 7, "phase_number": 2, "title": "Backend", "description": None,
                            "spec": "brief", "status": "ready", "gate": "none", "approved_at": None,
                            "acceptance": json.dumps([{"cmd": "node -e 1", "expect_exit": 0, "timeout_s": 300}])})
        out = run(_t._exec_build({"action": "build", "project_id": 9}, 1, bp, None, None))
        check("build door does NOT refuse a grandfathered node check on kimi (plan-time still does)",
              out["status"] == "ok" and out["result"].get("state") == "running"
              and bool(bp.started), str(out))
        _drain_loop()
        bp = psg.BuildPool({"id": 7, "phase_number": 2, "title": "Backend", "description": None,
                            "spec": "brief", "status": "ready", "gate": "none", "approved_at": None,
                            "acceptance": json.dumps([{"cmd": "rg --files", "expect_exit": 0, "timeout_s": 300}])})
        out = run(_t._exec_build({"action": "build", "project_id": 9}, 1, bp, None, None))
        check("build door does NOT refuse `rg` on kimi either (checks are notes)",
              out["status"] == "ok" and out["result"].get("state") == "running"
              and bool(bp.started), str(out))
        _drain_loop()
        _rv.resolve_build_target = _resolve_codex
        bp = psg.BuildPool({"id": 7, "phase_number": 2, "title": "Backend", "description": None,
                            "spec": "brief", "status": "ready", "gate": "none", "approved_at": None,
                            "acceptance": json.dumps([{"cmd": "rg --files", "expect_exit": 0, "timeout_s": 300}])})
        out = run(_t._exec_build({"action": "build", "project_id": 9}, 1, bp, None, None))
        check("build door ALLOWS the same `rg` check on codex",
              "doesn't have" not in str(out.get("result")), str(out))
    finally:
        _rv.resolve_build_target = _orig

    # The chain side: _run_phase_build with the engine already chosen. The
    # codex-ALLOWED door case above left its detached build on this loop;
    # it is cancelled first, so the only builds counted below are the
    # chain's own (task 42). Measured 2026-10-01: once the build path awaited
    # a thread (the link check, tools._links_out_refusal), that detached
    # build reached the stubbed _pool_build below and was counted as a wake
    # (billing_key factory-build-1, codex).
    _drain_loop()

    class ChainPool:
        def __init__(self, acceptance):
            self.acceptance = acceptance
            self.executed: list[str] = []

        async def fetchval(self, sql, *args):
            if "SELECT acceptance FROM project_phases" in sql:
                return self.acceptance
            return None

        async def fetchrow(self, sql, *args):
            return None

        async def fetch(self, sql, *args):
            return []

        async def execute(self, sql, *args):
            self.executed.append(sql + " :: " + json.dumps([str(a) for a in args]))
            return "UPDATE 1"

    woke = []

    async def _no_pool_build(**kw):
        woke.append(kw)
        return {"status": "ok", "result": {}}

    _orig_pb = _t._pool_build
    _t._pool_build = _no_pool_build
    try:
        cp = ChainPool(json.dumps([{"cmd": "node -e 1", "expect_exit": 0, "timeout_s": 300}]))
        run(_t._run_phase_build(entity_id=1, pool=cp, session_id=None, task_id=42, project_id=9,
                                phase_id=7, intent="x", research_id=None, timeout_s=60,
                                harness="kimi"))
        check("chain does NOT refuse a node check on kimi: the pod is woken with the check handed through",
              len(woke) == 1 and woke[0].get("acceptance")
              and woke[0]["acceptance"][0]["cmd"] == "node -e 1", str(woke))
        check("...flips the phase to running",
              any("UPDATE project_phases" in s and '"in_progress"' in s for s in cp.executed),
              "\n".join(cp.executed))
        check("...stamps no acceptance_refused and stops no chain on a check",
              not any("acceptance_refused" in s or "chain_stopped" in s
                      for s in cp.executed), "\n".join(cp.executed))
        cp = ChainPool(json.dumps([{"cmd": "python3 -m pytest -q", "expect_exit": 0, "timeout_s": 300}]))
        run(_t._run_phase_build(entity_id=1, pool=cp, session_id=None, task_id=42, project_id=9,
                                phase_id=7, intent="x", research_id=None, timeout_s=60,
                                harness="kimi"))
        check("a shipped-tool check reaches the build too", len(woke) == 2, str(len(woke)))
    finally:
        _t._pool_build = _orig_pb

    # ── 3b. retry budget: a refusal is not an attempt ────────────────────────
    # Review 2026-09-06: _phase_retry_pick counts status='failed' rows
    # for the phase; the chain-side refusal closes its row failed. Without the
    # exclusion, refused -> host rewrites -> real failure reads "failed twice"
    # and the one seeded retry never fires.
    class RetryPool:
        def __init__(self, rows):
            self.rows = rows

        async def fetchval(self, sql, *args):
            if "build_chain_halt" in sql:
                return False
            if "COUNT(*) FROM entity_tasks" in sql:
                check("the attempt count excludes acceptance_refused rows (SQL)",
                      "acceptance_refused" in sql, sql)
                return sum(1 for r in self.rows
                           if r["status"] == "failed"
                           and not ("acceptance_refused" in sql
                                    and r["result"].get("acceptance_refused")))
            return None

        async def fetchrow(self, sql, *args):
            if "FROM project_phases" in sql:
                return {"id": 7, "phase_number": 2, "title": "Backend", "spec": "brief",
                        "description": None, "status": "failed", "gate": "none",
                        "approved_at": None, "acceptance": None}
            return None

        async def fetch(self, sql, *args):
            # The failed rows as the attempt count reads them since the cap
            # audit of 2026-10-02 (tools._phase_failed_builds, classified by
            # tools._failed_build_kind): the flag rides the row.
            if "FROM entity_tasks" in sql and "status='failed'" in sql:
                check("the attempt count reads acceptance_refused off each row (SQL)",
                      "acceptance_refused" in sql, sql)
                return [{"error": "boom",
                         "acceptance_refused": bool(r["result"].get("acceptance_refused")),
                         "links_refused": False, "integration_conflict": False}
                        for r in self.rows if r["status"] == "failed"]
            return []

        async def execute(self, sql, *args):
            return "UPDATE 1"

    refused = {"status": "failed", "result": {"project_id": 9, "phase_id": 7, "acceptance_refused": True}}
    real = {"status": "failed", "result": {"project_id": 9, "phase_id": 7}}
    pick = run(_t._phase_retry_pick(pool=RetryPool([refused, real]), entity_id=1, project_id=9,
                                    phase_id=7, failure="boom", chain_depth=1, finished_task_id=2))
    check("refusal then ONE real failure -> the seeded retry still fires",
          isinstance(pick, tuple) and pick[0]["id"] == 7 and "boom" in pick[1], str(pick))
    pick2 = run(_t._phase_retry_pick(pool=RetryPool([refused, real, real]), entity_id=1, project_id=9,
                                     phase_id=7, failure="boom", chain_depth=1, finished_task_id=2))
    check("...and two real failures still stop (budget unchanged)", pick2 is None, str(pick2))

    # ── 4. the host is told ──────────────────────────────────────────────────
    from mastercode.public_tool_registry import PUBLIC_TOOL_SCHEMAS as P
    d1 = P["build"]["function"]["parameters"]["properties"]["phases"]["items"]["properties"]["acceptance"]["description"]
    d2 = P["project_phase"]["function"]["parameters"]["properties"]["acceptance"]["description"]
    for name, d in (("build.phases[].acceptance", d1), ("project_phase.acceptance", d2)):
        check(f"{name} names what the image ships and that a miss is refused (plan time)",
              "bash -c" in d and "python3" in d and "node" in d and "refused" in d.lower(), d[:200])

    _drain_loop()
    print(f"\n[accept-tools] {'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
