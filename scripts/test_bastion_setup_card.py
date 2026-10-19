#!/usr/bin/env python3
"""The "Set up a bastion" token card must survive the list reload that follows it.

Measured 2026-09-22 on one Host: the card opened and vanished, on every host
tried. The kernel side was fine — the log shows one pairing token minted per
click, forty in three minutes — but loadBastions() closes whatever inspector is
open (it renders a row snapshot; see its own comment), and setupBastion reloaded
the list right after opening the card, so the close landed on the reload's
continuation, after GET /api/network/bastions resolved. A single-use token
nobody can read is a pairing nobody can do.

The shape this pins: setupBastion opens the card FIRST (it must not wait on the
reload — fetchJSON has no timeout, and a hung bastion-latest would hide the token
by a different route), then reloads with `loadBastions({ keepInspector: true })`,
and loadBastions' close is guarded by that flag. Nothing after the card may close
the inspector or reload the list without the flag.

Source-reading, like scripts/test_network_map_routes.py. The behavioural side is
scripts/test_network_anchor_step.js (run by test_anchor_step_is_visible.py): it
drives the real setupBastion in a headless DOM and, since 2026-09-22, settles on
a real timer and asserts the inspector is not hidden afterwards — measured to
FAIL on the pre-fix app.js and pass on this one. Before that assertion it read
only innerHTML, which a close leaves intact, and passed on the broken code too.
Set NETWORK_APP_JS to check a different copy of the file (the mutation run that
proves this gate can fail).
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_JS = os.environ.get("NETWORK_APP_JS") or os.path.join(_ROOT, "system-apps/network/app.js")

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + what)
    if not cond:
        failures.append(what)


def _fn_body(src: str, name: str) -> str:
    """The exact text of `async function NAME(...) { ... }`, brace-matched from
    its opening brace. Strings and comments are not parsed, so a brace inside a
    string literal would skew it — none of the two functions this reads has one,
    and a skew shows up as a wrong extraction, not a silent pass."""
    m = re.search(r"async function " + re.escape(name) + r"\([^)]*\)\s*\{", src)
    if not m:
        return ""
    depth, i = 0, m.end() - 1
    while i < len(src):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start(): i + 1]
        i += 1
    return ""


def main() -> int:
    src = open(_APP_JS, encoding="utf-8").read()

    load = _fn_body(src, "loadBastions")
    check(bool(load), "loadBastions() exists in the network app")
    sig = re.match(r"async function loadBastions\(([^)]*)\)", load)
    check(bool(sig) and "keepInspector" in sig.group(1),
          "loadBastions takes a keepInspector option")
    check("if (!keepInspector) closeInspector();" in load,
          "loadBastions' close of the open inspector is guarded by keepInspector "
          "(an unconditional close is the 2026-09-12 shape that took the token off the screen)")
    check(load.count("closeInspector(") == 1,
          "loadBastions closes the inspector in exactly one place, the guarded one")

    setup = _fn_body(src, "setupBastion")
    check(bool(setup), "setupBastion() exists in the network app")
    card_at = setup.find("openInspector('Bastion ready")
    check(card_at != -1, "setupBastion opens the 'Bastion ready' token card "
                         "(a copy edit to that title must update this gate — loud on purpose)")
    tail = setup[card_at:] if card_at != -1 else ""
    check("loadBastions({ keepInspector: true })" in tail,
          "after the card, the list reload is told to keep the inspector")
    check(re.search(r"loadBastions\(\s*\)", tail) is None,
          "no bare loadBastions() after the card (that reload would close it)")
    check("closeInspector(" not in tail,
          "nothing after the card closes the inspector")
    check("await loadBastions" not in setup,
          "the card does not wait on the reload (a hung request would hide the token again)")

    if failures:
        print(f"\nFAIL — {len(failures)} problem(s):")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nok — setup card survives the reload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
