#!/usr/bin/env python3
"""System-separation invariant.

Doctrine: personality (how the entity expresses) and
constitution (what it must/must not do) are TWO systems. They meet only
in the prompt. Neither writes the other's store. This gate fails the build
on any weld — the mechanism that let the two systems fuse into "mushy
code" over 8 months.

The personality side is the person's ten sliders and
nothing else: the mastercode/personality package (expression, drift, the
dual codebook) is gone, the sliders never move on their own, and the only
writers of the weight columns are the two registered sources in
runtime/personality_validate.REGISTERED_WRITER_SOURCES. So the checks are:

  1. WRITE WALL — constitution/** contains no SQL writing personality
     weight columns (current_weights / seed_weights on entity_registry).
  2. WRITER WALL — across brain/ and server/, the only files whose SQL
     writes a weight column are the two registered writer sites, each of
     which passes its own registered source to guard_personality_write;
     and the registry names exactly those two sources. A third writer, or
     a registration with no site, fails the gate.
  3. The personality package stays gone, so nothing can import it or
     write the rules stores from it.

Usage: python3 scripts/check_system_separation.py [repo_root]
(defaults to /app when present, else the script's parent's parent).
Exit 0 = boundaries hold, 1 = weld detected.
"""
import ast
import os
import re
import sys

root = sys.argv[1] if len(sys.argv) > 1 else (
    "/app" if os.path.isdir("/app/brain") else
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
PERSONALITY_DIR = os.path.join(root, "brain", "mastercode", "personality")
CONSTITUTION_DIR = os.path.join(root, "brain", "mastercode", "constitution")
VALIDATE_PY = os.path.join(root, "brain", "mastercode", "runtime", "personality_validate.py")
SCAN_DIRS = (os.path.join(root, "brain"), os.path.join(root, "server"))

# The registered writer sources and the one file each lives in. Adding a
# writer is a deliberate act: the call site, REGISTERED_WRITER_SOURCES and
# this map change together.
WRITER_SITES = {
    "routes.genesis._seed_codebook": os.path.join("server", "routes", "genesis.py"),
    "routes.brain.user_weights_set": os.path.join("server", "routes", "brain.py"),
}

fails: list[str] = []


def _py_files(d):
    for dirpath, dirs, names in os.walk(d):
        dirs[:] = [x for x in dirs if x not in ("node_modules", "__pycache__")]
        for n in names:
            if n.endswith(".py"):
                yield os.path.join(dirpath, n)


# Write-wall pattern: the statement must both name the table and touch a
# weight column in the same statement window.
_WEIGHT_WRITE = re.compile(
    r"(UPDATE\s+entity_registry|INSERT\s+INTO\s+entity_registry)[\s\S]{0,400}?"
    r"(current_weights|seed_weights)",
    re.I,
)


def check_write_wall(pkg_dir, pattern, label):
    for f in _py_files(pkg_dir):
        src = open(f).read()
        m = pattern.search(src)
        if m:
            line = src[:m.start()].count("\n") + 1
            fails.append(f"WRITE WALL: {f}:{line} — {label}: {m.group(0)[:80]!r}")


def _registered_sources():
    """REGISTERED_WRITER_SOURCES' keys, read from the source (no import)."""
    try:
        tree = ast.parse(open(VALIDATE_PY).read())
    except (OSError, SyntaxError) as e:
        fails.append(f"WRITER WALL: cannot read {VALIDATE_PY} ({e})")
        return None
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target == "REGISTERED_WRITER_SOURCES" and isinstance(value, ast.Dict):
            return {k.value for k in value.keys if isinstance(k, ast.Constant)}
    fails.append(f"WRITER WALL: REGISTERED_WRITER_SOURCES not found in {VALIDATE_PY}")
    return None


def check_writer_wall():
    registered = _registered_sources()
    if registered is not None and registered != set(WRITER_SITES):
        fails.append(f"WRITER WALL: REGISTERED_WRITER_SOURCES is {sorted(registered)}; "
                     f"this gate knows the sites of {sorted(WRITER_SITES)} — change both together")
    allowed = {os.path.normpath(p) for p in WRITER_SITES.values()}
    for d in SCAN_DIRS:
        for f in _py_files(d):
            rel = os.path.normpath(os.path.relpath(f, root))
            src = open(f, errors="ignore").read()
            m = _WEIGHT_WRITE.search(src)
            if m and rel not in allowed:
                line = src[:m.start()].count("\n") + 1
                fails.append(f"WRITER WALL: {rel}:{line} writes a weight column and is not a "
                             f"registered writer site: {m.group(0)[:80]!r}")
    for source, rel in WRITER_SITES.items():
        path = os.path.join(root, rel)
        src = open(path).read() if os.path.isfile(path) else ""
        if not _WEIGHT_WRITE.search(src):
            fails.append(f"WRITER WALL: {rel} no longer writes a weight column; "
                         f"retire {source} from REGISTERED_WRITER_SOURCES and this gate")
        elif "guard_personality_write(" not in src or f'source="{source}"' not in src:
            fails.append(f"WRITER WALL: {rel} writes a weight column without "
                         f"guard_personality_write(source=\"{source}\")")


def main() -> int:
    print("[system-separation gate]")
    if not os.path.isdir(CONSTITUTION_DIR):
        fails.append(f"package missing: {CONSTITUTION_DIR}")
    if os.path.exists(PERSONALITY_DIR):
        fails.append(f"the personality package is back: {PERSONALITY_DIR} — it left with the "
                     "simulated psyche; the sliders are the person's")
    if not fails:
        check_write_wall(CONSTITUTION_DIR, _WEIGHT_WRITE,
                         "constitution writing personality weights")
        check_writer_wall()
    if fails:
        for f in fails:
            print(f"  FAIL  {f}")
        print(f"system-separation gate: FAIL ({len(fails)})")
        return 1
    print("  OK    constitution writes no personality weight")
    print(f"  OK    the weight columns have exactly the {len(WRITER_SITES)} registered writers, each guarded")
    print("  OK    the personality package stays gone")
    print("system-separation gate: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
