"""The turn's source as one text: runtime/session.py and the modules split out
of it, in that order.

A gate that checks the turn's code by reading it reads it here, so code moved
between these files stays where the gate looks. A gate that needs one file's
own text (its line numbers, or what only that file may hold) reads that file.
"""
import os

RUNTIME = ("brain", "mastercode", "runtime")

# session.py first; then the modules it re-exports, in the order it imports them.
TURN_FILES = (
    "session.py",
    "turn_input.py",
    "turn_holds.py",
    "turn_trace.py",
    "turn_replies.py",
    "turn_notices.py",
    "orchestration_tools.py",
    "turn_lanes.py",
    "delivery_checks.py",
)


def repo_root() -> str:
    return os.environ.get("APP_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def turn_paths(root=None) -> list:
    base = os.path.join(str(root or repo_root()), *RUNTIME)
    return [os.path.join(base, f) for f in TURN_FILES]


def turn_source(root=None) -> str:
    """Every turn file's text, joined by a blank line. session.py must exist
    (FileNotFoundError, as a plain read of it raised: an empty text would let
    every absence check pass). A missing split module is skipped, so a tree
    from before the split reads as session.py alone."""
    paths = turn_paths(root)
    with open(paths[0], encoding="utf-8") as f:
        parts = [f.read()]
    for p in paths[1:]:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                parts.append(f.read())
    return "\n\n".join(parts)


def sync_turn_attr(session_module, *names) -> None:
    """After a test rebinds names on runtime/session.py, rebind them in every
    module split out of it that holds them, so code there reads the same value,
    as it did when that code lived in session.py."""
    import sys
    for f in TURN_FILES[1:]:
        mod = sys.modules.get("mastercode.runtime." + f[:-3])
        if mod is None:
            continue
        for name in names:
            if name in mod.__dict__:
                setattr(mod, name, getattr(session_module, name))
