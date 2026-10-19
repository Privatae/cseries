#!/usr/bin/env python3
"""Gate: the model picker's markup + wiring invariants.

Origin (caught on a shipped UI): the coding
matrix put `display: flex` on a `<td>`. A flexed table cell drops out of table
layout entirely, so its contents render collapsed/overlapping — the ladder's
reorder, remove and add buttons were visibly present and completely
unclickable. Nothing in the smoke suite could see it: the routes were fine, the
handlers were bound, the JS parsed. Only a human clicking found it.

So this gate checks the two things static analysis CAN prove about that class
of bug:

  1. No class used on a `<td>` may carry `display: flex` in the stylesheet.
     Flex an inner `<div>` instead.
  2. Every control the picker renders with a `mp-*` class that implies an
     action (button/select/input) has a matching addEventListener binding —
     a rendered-but-unwired control is the same silent failure.

Pure text/regex over the two files — no node, no browser (there is neither on
the ship host, which is exactly why this bug reached production).
Run: APP_ROOT=<repo> python3 scripts/test_picker_layout.py
"""
import logging
import os
import re
import sys

# smoke reads this gate with `2>&1 | tail -1`. Module logging writes to
# stderr unbuffered while these prints are block-buffered through a pipe,
# so a stray log line lands after the summary and tail -1 reads it instead
# of the verdict — a green gate reported as FAIL. Silence the modules and
# flush our own output.
logging.disable(logging.CRITICAL)

failures = []


def check(label, ok, detail=""):
    print(f"[picker-layout] {'ok  ' if ok else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    root = os.environ.get("APP_ROOT", "/app")
    js_path = os.path.join(root, "dashboard/base/js/model-picker.js")
    css_path = os.path.join(root, "dashboard/base/css/model-picker.css")
    try:
        js = open(js_path, encoding="utf-8").read()
        css = open(css_path, encoding="utf-8").read()
    except OSError as e:
        check("read picker sources", False, repr(e))
        print("[picker-layout] FAIL (1 failing)")
        return 1

    # ── 1. no flexed <td> ────────────────────────────────────────────
    flexed = {m.group(1) for m in
              re.finditer(r"\.([a-zA-Z0-9_-]+)\s*\{[^}]*display:\s*flex", css)}
    td_classes = set()
    for m in re.finditer(r"<td class=\"([^\"]+)\"", js):
        # Template-literal interpolation inside the attr is fine — we only care
        # about the literal class tokens.
        td_classes.update(t for t in m.group(1).split() if "$" not in t)
    bad = sorted(td_classes & flexed)
    check("no class used on a <td> is display:flex "
          "(a flexed cell leaves table layout and its controls stop being clickable)",
          not bad, f"flexed on <td>: {bad}")

    # ── 2. rendered controls are wired ───────────────────────────────
    # Every mp-* class rendered onto a <button>, <select> or <input> must be
    # bound. A control that renders and does nothing is the shipped bug.
    # A control counts as wired if ANY selector that could reach it is bound —
    # its own mp-* class OR a data-* attribute on the same tag (the picker uses
    # both styles: .mp-rung-remove by class, [data-mp-open-keys] by attribute).
    # Collect every token that appears inside ANY selector call — the picker
    # uses bare classes (.mp-rung-remove), compound selectors
    # (.mp-assign-btn[data-role]), descendant selectors
    # ([data-mp-billing] .mp-bill-opt) and template literals
    # (`.mp-image-base[data-role="${role}"]`). Anything named in a selector
    # counts as reachable.
    bound = set()
    for m in re.finditer(r"(?:querySelectorAll|querySelector|closest)\(\s*([`'\"])(.*?)\1", js, re.S):
        sel = m.group(2)
        bound.update(re.findall(r"\.([a-zA-Z0-9_-]+)", sel))
        bound.update("@" + d for d in re.findall(r"\[(data-[a-zA-Z0-9_-]+)", sel))

    unwired = []
    for tag in ("button", "select", "input"):
        for m in re.finditer(r"<" + tag + r"\b([^>]*)>", js):
            attrs = m.group(1)
            cm = re.search(r"class=\"([^\"]*)\"", attrs)
            classes = [t for t in (cm.group(1).split() if cm else [])
                       if t.startswith("mp-") and "$" not in t]
            if not classes:
                continue
            data_attrs = ["@" + d for d in re.findall(r"\b(data-[a-zA-Z0-9_-]+)", attrs)]
            if any(c in bound for c in classes) or any(d in bound for d in data_attrs):
                continue
            unwired.append(classes[0])
    unwired = sorted(set(unwired))
    check("every rendered mp-* control (button/select/input) has a handler binding "
          "— by class or by data-attribute",
          not unwired, f"rendered but never bound: {unwired}")

    # ── 3. the picker never asks for, or sends, a time budget ─────────
    # The ladder's minutes value is never invented by the
    # UI ("30 m" once appeared unasked). It is not the
    # user's to pick either — the user cannot know how long something
    # takes — the per-rung wall clock is the platform's safety cap
    # (server/routes/ladder.py RUNG_WALL_CAP_MIN). The ladder
    # editor itself is gone from the Orchestrator (a ladder is a coding
    # protocol the host reads — Multi-Agent Coding); the picker renders no
    # minutes field anywhere and never writes max_minutes. A protocol file's
    # optional time_budget_minutes is the user's own JSON, not a picker field.
    check("the picker never writes max_minutes (the platform cap applies)",
          "max_minutes" not in js,
          "the picker must not write max_minutes — the wall clock is the platform's cap")
    check("no rung editor / time-budget field remains in the picker",
          "const saveLadder" not in js and "mp-rung" not in js
          and "Time budget" not in js,
          "the Orchestrator no longer edits a ladder; a per-rung minutes input asks for a number the user cannot know")

    # ── 3b. Coding Engines: ONE default tick, never a builder tick list ──
    # No parallel or ladder builder logic here, just a default
    # agent tick box. The Engines table renders a radio group for
    # the default (one engine at most) and no per-row Build checkbox; the
    # Multi-Agent Coding list renders beside it.
    check("Coding Engines renders the Default tick as a radio group (one engine at most)",
          'name="mp-default-engine"' in js and 'type="radio"' in js,
          "the default tick must be a radio group named mp-default-engine")
    check("no per-engine Build tick list remains (mp-mx-sel)",
          'class="mp-mx-sel"' not in js,
          "the Parallel Builders tick list is gone")
    check("the Multi-Agent Coding list renders (protocols section + editor)",
          "data-mp-protocols" in js and "_renderProtocols()" in js and "mp-proto-json" in js)

    # ── 4. the Keys app: every rendered action has a handler branch ───
    # Same failure class, different app: Keys renders buttons as data-act
    # values and dispatches them from ONE delegated listener, so a rendered
    # action with no branch is a button that silently does nothing.
    # In the REPO the Keys app is loose files; in the IMAGE it ships only as a
    # signed bundle (native-bundles/keys.cseries), which is what entities
    # actually run — so read the bundle when the loose file is absent. Reading
    # the bundle is the stronger check: it verifies what ships, not what sits
    # in the tree beside it.
    keys_js, source = "", ""
    loose = os.path.join(root, "system-apps/keys/app.js")
    bundle = os.path.join(root, "native-bundles/keys.cseries")
    if os.path.exists(loose):
        try:
            keys_js, source = open(loose, encoding="utf-8").read(), "source"
        except OSError as e:
            check("read Keys app.js", False, repr(e))
    elif os.path.exists(bundle):
        try:
            import zipfile
            with zipfile.ZipFile(bundle) as z:
                keys_js = z.read("payload/app.js").decode("utf-8")
            source = "bundle"
        except Exception as e:
            check("read Keys app.js from the shipped bundle", False, repr(e))
    else:
        check("locate the Keys app (loose source or shipped bundle)", False,
              f"neither {loose} nor {bundle}")
    if source:
        print(f"[picker-layout] .... Keys read from the {source}")
    if keys_js:
        rendered = set(re.findall(r"data-act=\"([a-z-]+)\"", keys_js))
        # Actions built by a ternary inside the attribute, e.g.
        # data-act="' + (have ? 'reveal' : 'add-provider') + '"
        for m in re.finditer(r"data-act=\"'\s*\+\s*\(([^)]*)\)", keys_js):
            rendered.update(re.findall(r"'([a-z-]+)'", m.group(1)))
        handled = set(re.findall(r"act === '([a-z-]+)'", keys_js))
        missing = sorted(rendered - handled)
        check("Keys: every rendered data-act has a handler branch",
              not missing, f"rendered but never handled: {missing}")
        check("Keys dispatches from a delegated listener "
              "(a per-render rebind leaves controls dead after a re-render)",
              "document.addEventListener('click'" in keys_js)

    print(f"[picker-layout] {'PASS (0 failing)' if not failures else f'FAIL ({len(failures)} failing): {failures}'}")
    sys.stdout.flush()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
