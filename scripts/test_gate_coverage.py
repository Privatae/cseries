#!/usr/bin/env python3
# gate-scope: whole-tree — every gate script, gate list and tests/ file
"""Gate-coverage ratchet — every gate script must run SOMEWHERE, or say why not.

THE DEFECT THIS EXISTS FOR (measured on one ship).
scripts/ held 129 test_*.py files. Smoke invoked 88 of them. The other 41 ran
nowhere at all — not in smoke, not in a runbook, not in CI, because the repo
had no CI then (no .gitea/workflows, no .github/workflows, no active git hooks;
all three verified absent that day).

Of those 41, eighteen were container-free and passed on the spot. They were
simply dead. test_phase_spec_gate.py had been in that same set and was wired in
by hand during that session; test_migrations_guarded.py — the ratchet
against migrations that crashloop an entity at boot — was still dead when that
same ship required checking migration idempotency by hand.

Nothing announced any of this. A gate that is never invoked looks exactly like a
gate that passes: the file is present, it is named test_*, and the suite is
green. That is the failure this ratchet ends.

THE CONTRACT. Every scripts/test_*.py and scripts/check_*.py must be exactly
one of:
  1. invoked by name inside scripts/smoke_test.sh, or
  2. listed in scripts/gates-container-free.d/ (runs in smoke AND pre-merge via
     the shared list), or
  3. listed in scripts/gates-infra-only.d/ with the live dependency that stops
     it running in either lane, or
  4. listed in scripts/gates-store-apps.txt with the store app it tests: it runs
     on the store apps' own lane (scripts/store_app_gates.sh) and never in the
     kernel's (store app gates run separately from the kernel's). A gate listed
     there AND in any of 1-3 is refused:
     the kernel lane would run it, or skip it, depending on which reader looked.

Each list is a DIRECTORY with one file per gate: scripts/
gates-container-free.d/test_foo.txt holds the gate's comment and the line
test_foo.py. Every branch that added a gate used to append to one shared .txt,
so any two such branches conflicted and a PR went unmergeable the moment the
one before it merged (two branches were rebased twice for that file
alone). The old .txt files stay as headers and must list no gate.

It is bidirectional on purpose. A listed file that no longer exists fails, and a
file listed as infra-only that smoke also invokes fails — otherwise the lists rot
into decoration, which is the same disease one layer up.

Pure: reads files, runs nothing. Cheap enough for the pre-merge lane.
"""
import os
import re
import sys

FAILED = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILED
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED += 1
        if detail:
            for line in detail.strip().splitlines():
                print(f"        {line}")


def read_gate_dir(scripts: str, name: str) -> tuple[list[str], list[str], list[str]]:
    """(gates, entries left in <name>.txt, problems) for one list.

    The list is scripts/<name>.d/, one file per gate: the file names exactly
    one gate, the one its file name says (test_foo.txt -> test_foo.py), so a
    file can never become a second shared list. scripts/<name>.txt is the
    header and must list no gate; its entries are returned so they are both
    still read (nothing silently drops) and refused."""
    left = read_list(os.path.join(scripts, f"{name}.txt"))
    gates: list[str] = list(left)
    problems: list[str] = []
    d = os.path.join(scripts, f"{name}.d")
    if not os.path.isdir(d):
        return gates, left, [f"scripts/{name}.d/ is missing"]
    for f in sorted(os.listdir(d)):
        path = os.path.join(d, f)
        # The shell readers take `<name>.d/*.txt` kept by `[[ -f ]]`: the glob
        # skips a dotfile, and -f skips a directory or a dangling link while
        # following a live one, as isfile does. Anything else here is read by
        # no lane, so it is refused rather than counted or quietly skipped.
        if f.startswith(".") or not f.endswith(".txt") or not os.path.isfile(path):
            problems.append(f"{name}.d/{f}: not a .txt file the readers read, so it lists nothing")
            continue
        listed = read_list(path)
        stem = f[: -len(".txt")]
        want = stem if os.path.splitext(stem)[1] else stem + ".py"
        if listed != [want]:
            problems.append(f"{name}.d/{f}: must name exactly one gate, {want}; it names {listed}")
        gates.extend(listed)
    return gates, left, problems


def read_store_registry(root: str) -> tuple[list[str], list[str], list[str]]:
    """(scripts gates, tests/ paths, problems) from scripts/gates-store-apps.txt.

    A line is `<gate> <app-id>[,<app-id>...] [<path>...]`. Each app id must be
    a system-apps/<id> that is not image-baked (scripts/image_baked_apps.txt):
    a baked app reaches every Host with the image, so its gates are the
    kernel's."""
    scripts = os.path.join(root, "scripts")
    path = os.path.join(scripts, "gates-store-apps.txt")
    gates: list[str] = []
    tests: list[str] = []
    problems: list[str] = []
    if not os.path.exists(path):
        return gates, tests, problems
    baked = set(read_list(os.path.join(scripts, "image_baked_apps.txt")))
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            f = raw.split("#", 1)[0].split()
            if not f:
                continue
            if len(f) < 2:
                problems.append(f"gates-store-apps.txt:{n}: {f[0]} names no store app")
                continue
            for app in f[1].split(","):
                if not os.path.isdir(os.path.join(root, "system-apps", app)):
                    problems.append(f"gates-store-apps.txt:{n}: no system-apps/{app}")
                elif app in baked:
                    problems.append(f"gates-store-apps.txt:{n}: {app} is image-baked, not a store app")
            for extra in f[2:]:
                if not os.path.exists(os.path.join(root, extra)):
                    problems.append(f"gates-store-apps.txt:{n}: {extra} does not exist")
            if f[0].startswith("tests/"):
                if not os.path.isfile(os.path.join(root, f[0])):
                    problems.append(f"gates-store-apps.txt:{n}: {f[0]} does not exist")
                tests.append(f[0])
            else:
                if not os.path.isfile(os.path.join(scripts, f[0])):
                    problems.append(f"gates-store-apps.txt:{n}: scripts/{f[0]} does not exist")
                gates.append(f[0])
    return gates, tests, problems


def read_list(path: str) -> list[str]:
    """One bare filename per line; # comments and blanks ignored.

    An inline trailing comment is allowed (gates-infra-only.txt carries the
    dependency there), so split on '#' and keep the first field.
    """
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def main() -> int:
    root = os.environ.get("APP_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    scripts = os.path.join(root, "scripts")
    smoke_path = os.path.join(scripts, "smoke_test.sh")

    if not os.path.isdir(scripts):
        print(f"FAIL: no scripts/ under {root}")
        return 1
    if not os.path.exists(smoke_path):
        print(f"FAIL: no scripts/smoke_test.sh under {root}")
        return 1

    smoke_raw = open(smoke_path, encoding="utf-8").read()
    # Match against smoke with #-comments stripped, so a gate MENTIONED in a
    # comment does not count as invoked. Two ways that matters, both real:
    # a commented-out invocation would otherwise read as live coverage, and a
    # cautionary comment naming an infra gate (e.g. "don't wire
    # test_research_paths.py here") would otherwise false-fail the
    # infra-only-not-in-smoke check.
    # Stripping from the first '#' is safe for this purpose: every real
    # invocation puts the script path BEFORE any trailing comment, so it
    # survives; smoke's own `# || true:` notes sit after theirs.
    smoke = "\n".join(line.split("#", 1)[0] for line in smoke_raw.splitlines())
    # test_* AND check_* — both naming conventions are in use for gates here, so
    # the ratchet has to cover both or the convention decides which files are
    # allowed to go dead. Not hypothetical: scripts/check_store_delisted.py was
    # written as "the invariant ratchet" for the store-delist rule,
    # was invoked by nothing for six weeks, and the invariant it guards was
    # broken that whole time (12 retired app ids still serving bundles, found
    # later). It was the ONLY unaccounted-for file of the 18 check_*.py in
    # this directory — the other 17 are invoked by smoke — so widening the scope
    # costs one list entry and closes the naming loophole.
    gates = sorted(f for f in os.listdir(scripts)
                   if (f.startswith("test_") or f.startswith("check_"))
                   and f.endswith(".py"))
    # This file gates the others; it is wired into smoke like any gate, but
    # exclude it so a rename can never make it silently self-exempt.
    gates = [g for g in gates if g != os.path.basename(__file__)]

    free, free_left, free_bad = read_gate_dir(scripts, "gates-container-free")
    infra, infra_left, infra_bad = read_gate_dir(scripts, "gates-infra-only")
    store, store_tests, store_bad = read_store_registry(root)

    print(f"gate coverage — {len(gates)} gate scripts, "
          f"{len(free)} container-free, {len(infra)} infra-only, {len(store)} store-app")

    # ── 1. every gate is accounted for ────────────────────────────────────
    # Match the basename as a whole token so test_backup_ship.py is not counted
    # as covering a hypothetical test_backup_ship_extra.py.
    def invoked_by_smoke(name: str) -> bool:
        return re.search(rf"(?<![\w.]){re.escape(name)}(?![\w])", smoke) is not None

    unaccounted = [g for g in gates
                   if not invoked_by_smoke(g) and g not in free and g not in infra
                   and g not in store]
    check(
        "every scripts/test_*.py and check_*.py is invoked by smoke, "
        "container-free-listed, infra-listed, or store-app-listed",
        not unaccounted,
        "These gates run NOWHERE. Each is dead code pretending to be coverage.\n"
        "Wire it into smoke, add scripts/gates-container-free.d/<gate>.txt, or\n"
        "declare its live dependency in scripts/gates-infra-only.d/<gate>.txt,\n"
        "or, if its subject is a store app, list it in scripts/gates-store-apps.txt:\n  "
        + "\n  ".join(unaccounted),
    )

    # ── 2. the lists cannot rot ───────────────────────────────────────────
    # One file per gate, and the old shared files list none: a line appended
    # to one shared file is what made every pair of gate-adding branches
    # conflict.
    check("gates-container-free.txt lists no gate (each gate is its own file in "
          "gates-container-free.d/)", not free_left,
          "Move each of these to scripts/gates-container-free.d/<gate>.txt:\n  "
          + "\n  ".join(free_left))
    check("gates-infra-only.txt lists no gate (each gate is its own file in "
          "gates-infra-only.d/)", not infra_left,
          "Move each of these to scripts/gates-infra-only.d/<gate>.txt:\n  "
          + "\n  ".join(infra_left))
    check("every file in the two gate directories names exactly the gate its name says",
          not (free_bad + infra_bad), "\n".join(free_bad + infra_bad))
    missing_free = [g for g in free if not os.path.exists(os.path.join(scripts, g))]
    check("every gates-container-free.d entry exists", not missing_free,
          "Listed but not on disk (renamed or deleted):\n  " + "\n  ".join(missing_free))

    missing_infra = [g for g in infra if not os.path.exists(os.path.join(scripts, g))]
    check("every gates-infra-only.d entry exists", not missing_infra,
          "Listed but not on disk (renamed or deleted):\n  " + "\n  ".join(missing_infra))

    both = sorted(set(free) & set(infra))
    check("no gate is in both lists", not both,
          "A gate is either runnable in the cheap lanes or it is not:\n  "
          + "\n  ".join(both))

    # ── 2b. a store app's gate is registered once, on the store lane ──────
    check("every gates-store-apps.txt line names an existing gate and store app",
          not store_bad, "\n".join(store_bad))
    store_twice = sorted(g for g in set(store)
                         if g in free or g in infra or invoked_by_smoke(g))
    check("no gate is in gates-store-apps.txt and in a kernel lane too", not store_twice,
          "A store app's gate runs on the store lane only (scripts/store_app_gates.sh).\n"
          "Remove it from gates-container-free.d/ / gates-infra-only.d/ / smoke_test.sh,\n"
          "or from gates-store-apps.txt if its subject is kernel code:\n  "
          + "\n  ".join(store_twice))

    # An infra-only gate that smoke also invokes means one of the two is wrong,
    # and the expensive way to find out is a red ship. test_research_paths.py
    # failed a ship exactly here.
    infra_in_smoke = [g for g in infra if invoked_by_smoke(g)]
    check("no infra-only gate is invoked by smoke", not infra_in_smoke,
          "smoke_test.sh invokes these, but they are declared to need live infra.\n"
          "Either they do not (move to gates-container-free.d/) or the smoke\n"
          "invocation is a false red waiting to happen:\n  "
          + "\n  ".join(infra_in_smoke))

    # ── 3. smoke actually consumes the container-free list ────────────────
    # The list only means something if smoke reads it. Without this, the 18
    # gates go back to being dead the moment someone drops the loop.
    check("smoke_test.sh reads gates-container-free.d",
          "gates-container-free.d" in smoke,
          "scripts/smoke_test.sh no longer reads the shared list, so every gate\n"
          "in it is dead again — which is the exact condition this file exists\n"
          "to make loud.")

    pmg_path = os.path.join(scripts, "pre_merge_gates.sh")
    # Comment-stripped for the same reason as smoke above: both files DISCUSS
    # the list in their headers, so an un-stripped match would pass on the prose
    # alone even after the code that reads it was deleted.
    pmg_raw = open(pmg_path, encoding="utf-8").read() if os.path.exists(pmg_path) else ""
    pmg = "\n".join(line.split("#", 1)[0] for line in pmg_raw.splitlines())
    check("pre_merge_gates.sh reads gates-container-free.d",
          "gates-container-free.d" in pmg,
          "scripts/pre_merge_gates.sh must consume the same list, or the cheap\n"
          "lane silently covers less than the ship lane.")
    # The registry only keeps store gates out of the kernel lane if both of the
    # kernel's runners read it, and only runs them if the store lane does.
    for reader in ("pre_merge_gates.sh", "smoke_test.sh", "store_app_gates.sh"):
        rp = os.path.join(scripts, reader)
        rtext = (open(rp, encoding="utf-8").read() if os.path.exists(rp) else "")
        rtext = "\n".join(line.split("#", 1)[0] for line in rtext.splitlines())
        check(f"{reader} reads gates-store-apps.txt", "gates-store-apps.txt" in rtext,
              f"scripts/{reader} must read scripts/gates-store-apps.txt: the kernel\n"
              "lane's runners to leave those gates out, store_app_gates.sh to run them.")
    # The public CI's pytest is a kernel run too, so it leaves the store apps'
    # tests/ entries out the way pre_merge_gates.sh's does. Its own step script
    # runs here with pytest replaced by a print of the arguments it would get.
    wf = os.path.join(root, ".github", "workflows", "test.yml")
    if os.path.exists(wf):
        import subprocess
        import yaml
        doc = yaml.safe_load(open(wf, encoding="utf-8")) or {}
        runs = [str(s.get("run")) for job in (doc.get("jobs") or {}).values()
                for s in (job.get("steps") or []) if "-m pytest" in str(s.get("run") or "")]
        args: list[str] = []
        for run in runs:
            r = subprocess.run(["bash", "-eo", "pipefail", "-c",
                                run.replace("python3 -m pytest", "printf '%s\\n'")],
                               cwd=root, capture_output=True, text=True, timeout=60)
            args += r.stdout.split() if r.returncode == 0 else [f"(the step failed: {r.stderr.strip()})"]
        _, store_tests, _ = read_store_registry(root)
        missing = [t for t in store_tests if f"--ignore={t}" not in args]
        check(".github/workflows/test.yml's pytest leaves out every store app's tests/ entry",
              bool(runs) and not missing,
              f"not left out: {missing}; pytest's arguments there: {args}" if runs
              else "no step of .github/workflows/test.yml runs `python3 -m pytest`")

    # ── 4. tests/ is in a lane too ────────────────────────────────────────
    # THE BLIND SPOT THIS RATCHET ITSELF HAD. Everything above
    # polices scripts/. The repo also has tests/ — 26 files, 519 tests — and it
    # was invoked by NOTHING: not smoke, not pre_merge_gates, not either list,
    # and this repo had no CI then. Ten of those tests were failing and
    # nothing could say so. (CI runs tests/ now too: on the forge the gates job
    # of .gitea/workflows/check.yml, through pre_merge_gates.sh, on every pull
    # request; on GitHub .github/workflows/test.yml, on every push to main and
    # every pull request. This lane still has to.)
    #
    # This file is the thing meant to notice a suite running nowhere, and it
    # could not see this one because its contract named a DIRECTORY rather than
    # the principle. The same disease it was written to cure, one directory over.
    # WALKED, not listdir'd. A tests/unit/ reorganisation leaves the top level
    # empty, `if test_files:` then SKIPS this check rather than failing it, and
    # pre_merge_gates' own top-level glob goes quiet at the same moment — so
    # both halves would disappear together and the run stays green. Measured.
    tests_dir = os.path.join(root, "tests")
    test_files = []
    for dirpath, _dirnames, filenames in os.walk(tests_dir):
        test_files += [os.path.join(dirpath, f) for f in filenames
                       if f.startswith("test_") and f.endswith(".py")]
    if test_files:
        # A LIVE line, not merely a present string. Comments are already
        # stripped above, but `echo "skipping: python3 -m pytest tests/"` is not
        # a comment and satisfied a bare substring match. Requiring the
        # invocation to start its statement rejects it being an argument to
        # echo/printf. This is a heuristic, and the honest limit of a static
        # check: a lane that runs the command and discards the result still
        # passes, exactly as with the three checks above.
        invoke = re.compile(r"(?m)(?:^|[;&|]|\bthen\b|\bdo\b|\$\()\s*"
                            r"(?:[A-Za-z_][\w]*=\S*\s+|timeout\s+\S+\s+)*"
                            r"(?:python3?\s+-m\s+)?pytest\s+tests/")
        runs_tests = bool(invoke.search(pmg) or invoke.search(smoke))
        check(
            f"tests/ ({len(test_files)} files) is run by a lane",
            runs_tests,
            "tests/ exists and no lane invokes it. That is 500+ tests whose\n"
            "result nobody sees, which is indistinguishable from a suite that\n"
            "passes — the exact condition this file exists to make loud, and it\n"
            "was true of tests/ until 2026-09-15 because this ratchet's contract\n"
            "named scripts/ rather than the principle.\n"
            "Add a `pytest tests/` step to scripts/pre_merge_gates.sh (or\n"
            "smoke_test.sh) rather than deleting this check.",
        )

        # ── 5. the tests/ lane's limit is a measured line, read ───────────
        # tests/ ran 164-166 s against the 180 s every gate gets (measured
        # unloaded). Its limit is a line in gate-timeouts.txt with the
        # measurement written above it, and that line only counts if the lane
        # reads it: pytest_supervisor.py's --backstop-s takes it, so a lane
        # left on PER_GATE_TIMEOUT (or on no backstop) fails here.
        gt_path = os.path.join(scripts, "gate-timeouts.txt")
        gt_lines = (open(gt_path, encoding="utf-8").read().splitlines()
                    if os.path.exists(gt_path) else [])
        at = next((i for i, ln in enumerate(gt_lines) if ln.split()[:1] == ["tests/"]), None)
        check(
            "gate-timeouts.txt has a tests/ line, a measurement written above it",
            at is not None and at > 0 and gt_lines[at - 1].startswith("#")
            and len(gt_lines[at].split()) >= 2 and gt_lines[at].split()[1].isdigit()
            and any(re.search(r"\d s\b", ln) for ln in gt_lines[:at] if ln.startswith("#")),
            "scripts/gate-timeouts.txt needs `tests/ <seconds>` with the run it was\n"
            "measured from written above it (the file's own rule).",
        )
        limit_var = re.search(r'(\w+)="\$\(timeout_for tests/\)"', pmg)
        check(
            "pre_merge_gates.sh's tests/ lane takes its limit from that line",
            limit_var is not None
            and re.search(r'pytest_supervisor\.py[\s\\]*--backstop-s "\$' + limit_var.group(1)
                          + r'"[\s\\]*--[\s\\]*python3 -m pytest tests/', pmg) is not None,
            "The tests/ lane must run under pytest_supervisor.py with\n"
            "`--backstop-s \"$(timeout_for tests/)\"` (through a variable): otherwise its\n"
            "gate-timeouts.txt line is read by nothing.",
        )

    print(f"\n{'PASS' if not FAILED else str(FAILED) + ' FAILED'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
