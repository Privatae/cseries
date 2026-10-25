#!/usr/bin/env python3
"""Ratchet: every module that can spend money on inference is reachable.

WHAT THIS EXISTS FOR (measured on one Host, 2026-09-19). `inference_cost_log`
had 4.5 months of rows and only six `source_module` values in them. Seven
call_model callers had never logged under their own name. Tracing each one found
a whole package — `mastercode/brain/`'s UnifiedBrain (brain, lens, synthesis,
constitutional, watchdog_lens, task_executor, model_client and their helpers) —
plus `failure_classifier.py` and `runtime/code_harness.py`, with no caller at
all. They were deleted; this keeps them from growing back.

Two things made the rot invisible for months, and both are what the checks below
are shaped around:

  1. `mastercode/brain/__init__.py` re-exported the dead modules, so importing
     the LIVE `offline_task_runner` from the same package executed every dead
     one at boot. They were always loaded, which reads exactly like being used.
     Hence check 2 walks the import graph the way Python actually does it,
     parent `__init__.py` included.

  2. `provider_gateway.call_model`'s `_source` argument DEFAULTS to "unknown"
     (provider_gateway.py:1250/1431/1754). A caller that omits it lands in a
     bucket shared with dispatched agents, so "no rows under its name" is not
     evidence a module is dead — and, read the other way, was very nearly taken
     as evidence that live callers like the `order_software` planner were dead.
     The log cannot answer this question. The import graph can, so it is what
     this gate uses.

LIMITS, stated because a gate that oversells itself is worse than none:

  * Import reachability catches an orphaned MODULE, not a dead function inside a
    live one. `server/routes/security.py` is reachable here because its router is
    mounted, which is the right answer — `_call_opus_analysis` fires from
    POST /api/security/scan on demand — but the same check would not notice if
    that route were deleted and the function left behind.

  * The reachability walk covers the KERNEL roots only (brain/, server/): they
    share one process and one entry point, and all ten of today's callers live
    there. The rest of the Python here runs as SEPARATE processes with their own
    container entry points (cli/, sidecars/, system-apps/), so
    "reachable from server/main.py" is the wrong question for them and would fail
    every one. Rather than leave them unscanned, which is what happened until
    2026-09-19, NON_KERNEL_ROOTS is swept for call_model callers and the gate
    fails if one appears — naming the root, so whoever adds it wires that lane's
    real entry point in rather than letting it go unchecked.

  * scripts/ and tests/ are deliberately outside that sweep: they are invoked
    directly by the gate runner, never imported, and they legitimately name
    call_model in assertions about other modules.

  * A module's own `if __name__ == "__main__"` block is NOT an entry point here.
    runtime/code_harness.py had one and was still dead; counting it would have
    excused the exact module this gate exists to catch.

Pure: parses files, imports nothing, runs nothing. Container-free.
  APP_ROOT=<repo> python3 scripts/test_no_dead_model_lanes.py
"""
import ast
import os
import sys

FAILED = 0

# Deleted 2026-09-19. Each had zero importers outside its own package.
DELETED = [
    # The UnifiedBrain package: a chat veto and an autonomous loop, neither wired.
    "brain/mastercode/brain/brain.py",
    "brain/mastercode/brain/integration.py",
    "brain/mastercode/brain/lens.py",
    "brain/mastercode/brain/lens_prompts.py",
    "brain/mastercode/brain/model_client.py",
    "brain/mastercode/brain/synthesis.py",
    "brain/mastercode/brain/constitutional.py",
    "brain/mastercode/brain/watchdog_lens.py",
    "brain/mastercode/brain/task_executor.py",
    "brain/mastercode/brain/signal_collector.py",
    "brain/mastercode/brain/skill_tree.py",
    "brain/mastercode/brain/capability_codebook.py",
    "brain/mastercode/brain/monitor.py",
    "brain/mastercode/brain/complexity_gate.py",
    # Teaching-rule extraction whose only caller, server/benchmark_engine.py,
    # had itself already been deleted.
    "brain/mastercode/failure_classifier.py",
    # A "direct coding execution harness, no Claude Code dependency" that
    # predates the harness pool; nothing imported it but its own __main__.
    "brain/mastercode/runtime/code_harness.py",
]

# What is left in mastercode/brain/ — the scheduled/offline half, all live.
# Named so that deleting one by mistake fails here instead of at boot.
BRAIN_PACKAGE_SURVIVORS = {
    "__init__.py",
    "entity_clock.py",
    "offline_task_runner.py",
    "replica_reconciler.py",
    "task_manager.py",
}

# The one process entry point: entrypoint.sh ends in `exec python3 -m server.launch`.
ENTRY_POINTS = ["server/launch.py", "server/main.py"]

# The roots that share that process, and are therefore what reachability means.
KERNEL_ROOTS = ("brain", "server")

# Separate processes with their own container entry points. No call_model caller
# is expected here today; if one lands, the gate says so instead of not looking.
# vm/ is the machines the Host runs in: the cloud box's own programs, which run
# on the box beside the containers (vm/cloud/overlay), and the VM test's.
NON_KERNEL_ROOTS = ("cli", "sidecars", "system-apps", "vm")

# Run directly by the gate runner, never imported, and they name call_model in
# assertions about other modules — so neither sweep applies.
NON_IMPORTED_ROOTS = ("scripts", "tests")

SKIP_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build"}


def check(label: str, cond: bool, detail: str = "") -> None:
    global FAILED
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILED += 1
        for line in (detail or "").strip().splitlines():
            print(f"        {line}")


def walk_py(base):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


class Graph:
    """Import graph over the two roots that are on sys.path in the container.

    entrypoint.sh runs `python3 -m server.launch` from /app, and the brain
    modules are imported as `mastercode.*` (sys.path gets /app/brain). So the
    same file is reachable under two names, and both are indexed.
    """

    def __init__(self, root):
        self.root = root
        self.roots = [root, os.path.join(root, "brain")]
        self.mods = {}
        for base in self.roots:
            if not os.path.isdir(base):
                continue
            for full in walk_py(base):
                parts = os.path.relpath(full, base)[:-3].split(os.sep)
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if parts:
                    self.mods.setdefault(".".join(parts), full)

    def _pkg_of(self, path):
        """The package a file lives IN — the base for `from . import x`.

        For pkg/__init__.py that is ["pkg"], because __init__ IS the package.
        The longest matching root wins so mastercode.* resolves as mastercode.*.
        """
        best = max((b for b in self.roots if path.startswith(b + os.sep)),
                   key=len, default=None)
        if best is None:
            return []
        return os.path.relpath(path, best)[:-3].split(os.sep)[:-1]

    def imports_of(self, path):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read())
        except (SyntaxError, OSError):
            return set()
        here, names = self._pkg_of(path), set()
        for node in ast.walk(tree):
            # Deferred (function-body) imports count: this codebase uses them
            # heavily, and an import inside a function still binds the module.
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = here[:len(here) - node.level + 1] if node.level > 1 else here
                    mod = ".".join(base + ([node.module] if node.module else []))
                else:
                    mod = node.module or ""
                if mod:
                    names.add(mod)
                    names.update(f"{mod}.{a.name}" for a in node.names)
        return names

    def resolve(self, names):
        out = set()
        for n in names:
            hit = n if n in self.mods else n.rsplit(".", 1)[0]
            if hit not in self.mods:
                continue
            out.add(self.mods[hit])
            # Importing pkg.sub EXECUTES every ancestor package's __init__.py.
            # This is the edge that hid the UnifiedBrain package for months.
            parts = hit.split(".")
            for i in range(1, len(parts)):
                anc = ".".join(parts[:i])
                if anc in self.mods:
                    out.add(self.mods[anc])
        return out

    def reachable(self, entries):
        seen, stack = set(), [os.path.join(self.root, e) for e in entries]
        stack = [p for p in stack if os.path.exists(p)]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(n for n in self.resolve(self.imports_of(cur)) if n not in seen)
        return seen


def main() -> int:
    root = os.environ.get("APP_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))

    print("dead model lanes stay dead")
    for rel in DELETED:
        check(f"{rel} is gone", not os.path.exists(os.path.join(root, rel)))

    brain_pkg = os.path.join(root, "brain/mastercode/brain")
    if os.path.isdir(brain_pkg):
        present = {f for f in os.listdir(brain_pkg) if f.endswith(".py")}
        extra = sorted(present - BRAIN_PACKAGE_SURVIVORS)
        missing = sorted(BRAIN_PACKAGE_SURVIVORS - present)
        check("mastercode/brain/ holds only the scheduled/offline lanes",
              not extra,
              f"unexpected module(s): {extra}\n"
              "If this is a new background lane, add it to BRAIN_PACKAGE_SURVIVORS.\n"
              "If it is a reasoning lane, it belongs somewhere a reader expects one.")
        check("every scheduled/offline lane is still present", not missing,
              f"missing: {missing}")

    init = os.path.join(brain_pkg, "__init__.py")
    if os.path.exists(init):
        with open(init, encoding="utf-8") as fh:
            src = fh.read()
        has_import = any(
            isinstance(n, (ast.Import, ast.ImportFrom))
            for n in ast.walk(ast.parse(src)))
        check("mastercode/brain/__init__.py re-exports nothing",
              not has_import,
              "A re-export here drags every sibling into any import of one of\n"
              "them, which is exactly how the dead package stayed warm at boot.")

    print("\nevery call_model caller is reachable from a live entry point")
    graph = Graph(root)
    reach = graph.reachable(ENTRY_POINTS)
    check(f"the entry points resolve ({len(reach)} modules reachable)",
          len(reach) > 100,
          f"only {len(reach)} modules reached from {ENTRY_POINTS} — the graph "
          "walk is broken, not the tree")

    def callers_under(*roots):
        out = []
        for r in roots:
            base = os.path.join(root, r)
            if not os.path.isdir(base):
                continue
            for full in walk_py(base):
                try:
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        src = fh.read()
                except OSError:
                    continue
                # The definition itself lives in provider_gateway; skip it.
                if "call_model(" not in src or "def call_model" in src:
                    continue
                out.append(full)
        return out

    callers = sorted(set(callers_under(*KERNEL_ROOTS)))
    unreachable = sorted(os.path.relpath(f, root) for f in callers if f not in reach)

    # A floor that proves the scan matches, not a count to keep: 7 since
    # 2026-10-20, when runtime/diff_review.py (a caller on every landed
    # build) was deleted with the review role.
    check(f"found the kernel call_model callers to check ({len(callers)})",
          len(callers) >= 7,
          "far fewer callers than expected — the scan is probably not matching")
    check("no call_model caller is orphaned", not unreachable,
          "unreachable from server/launch.py + server/main.py:\n"
          + "\n".join(f"  {u}" for u in unreachable)
          + "\nEither wire it to an entry point or delete it. A module that can "
            "spend money\nand cannot be reached is the defect this gate exists for.")

    # The lanes this gate does not walk: catch a caller arriving there, rather
    # than quietly not looking (which is how the kernel's own went unseen).
    strays = sorted(os.path.relpath(f, root)
                    for f in callers_under(*NON_KERNEL_ROOTS))
    check("no call_model caller has appeared in a non-kernel lane", not strays,
          "found in a root this gate cannot walk from server/main.py:\n"
          + "\n".join(f"  {s}" for s in strays)
          + "\nThese run as separate processes. Add that lane's real container\n"
            "entry point to ENTRY_POINTS and include its root in KERNEL_ROOTS,\n"
            "so the caller is checked instead of exempt.")

    # Keep the root lists honest: a new top-level directory holding Python must
    # be classified, or the sweeps above silently do not cover it.
    known = set(KERNEL_ROOTS) | set(NON_KERNEL_ROOTS) | set(NON_IMPORTED_ROOTS)
    unclassified = []
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if name.startswith("."):
            continue
        # A repo-root .py file is swept by neither arm above (they walk
        # directories); it must not hold a model caller either.
        if os.path.isfile(full) and name.endswith(".py"):
            if "call_model" in open(full, encoding="utf-8", errors="replace").read():
                unclassified.append(name)
            continue
        if not os.path.isdir(full) or name in known:
            continue
        if any(True for _ in walk_py(full)):
            unclassified.append(name)
    check("every top-level directory holding Python is classified, and no root-level file calls a model",
          not unclassified,
          f"unclassified root(s) / root files naming call_model: {unclassified}\n"
          "Add each directory to KERNEL_ROOTS, NON_KERNEL_ROOTS or NON_IMPORTED_ROOTS; "
          "move a root-level caller into a kernel root.")

    print(f"\n{'FAILED: %d check(s)' % FAILED if FAILED else 'OK'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
