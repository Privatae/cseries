#!/usr/bin/env python3
"""A coding engine's Connect opens Keys on the form for that engine's own key.

Found on the Ubuntu test VM: Orchestrator > Coding engines > Connect Cursor
opened Keys on its Providers list, where Cursor is not, and the person had to
know to go to Vault > Add a key and type `harness-cursor`.

  1. server/routes/inference.py _build_coding_matrix: an engine that runs on
     its own key file (harness_keys.KEYS_HARNESS_PROVIDERS, not the generic
     band, no provider key) carries action.key = its Keys name; a vendor login
     (Claude Code), the generic band (opencode) and a provider-bound engine
     (Kimi) do not
  2. dashboard/base/js/model-picker.js: a Keys action with a key opens Keys at
     the view 'key-<name>' (the desktop's view lane), anything else as before
  3. dashboard/base/js/app-runner.js lets the Orchestrator send Keys a view
     (test_isolated_app_asks holds the relay rule itself)
  4. system-apps/keys/app.js: the view 'key-<name>' opens the Vault and the
     Add a key form with the name filled in, on a fresh window (#view=, after
     the first load) or an open one (cseries:app-view from the desktop only);
     any other view is ignored

Container-free; the Keys view parser runs in node.
APP_ROOT=<repo> python3 scripts/test_engine_connect_opens_its_key.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys

ROOT = os.environ.get("APP_ROOT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path[:0] = [ROOT, os.path.join(ROOT, "brain")]
logging.disable(logging.CRITICAL)

FAILS: list[str] = []


def check(name, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + name + ("" if ok else f"  -- {str(detail)[:400]}"))
    if not ok:
        FAILS.append(name)


def src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_the_matrix_names_the_key():
    from server.routes import inference as inf
    m = asyncio.new_event_loop().run_until_complete(inf._build_coding_matrix())
    rows = {r.get("id"): r for r in m.get("harnesses", [])}
    act = lambda h: (rows.get(h) or {}).get("action") or {}  # noqa: E731
    check("1. Connect Cursor (its own key file) names the key: harness-cursor",
          act("cursor").get("app") == "keys" and act("cursor").get("key") == "harness-cursor",
          act("cursor"))
    check("1. a vendor login (Claude Code), the generic band (opencode) and a provider-bound "
          "engine (Kimi) name none",
          "key" not in act("claude-code") and "key" not in act("opencode")
          and "key" not in act("kimi"), (act("claude-code"), act("opencode"), act("kimi")))


def test_the_picker_and_the_relay():
    p = src("dashboard/base/js/model-picker.js")
    m = re.search(r"\} else if \(\(btn\.dataset\.app \|\| 'keys'\) === 'keys' && btn\.dataset\.key\) \{(.*?)\} else \{",
                  p, re.S)
    check("2. a Keys action with a key opens Keys at the view 'key-<name>'",
          bool(m) and "this._openAppView('keys', 'key-' + btn.dataset.key)" in m.group(1),
          m and m.group(1)[:200])
    check("2. the button carries the key from the action",
          'data-key="${esc(row.action.key)}"' in p)
    r = src("dashboard/base/js/app-runner.js")
    check("3. the desktop lets the Orchestrator send Keys a view",
          "'orchestrator': { open: { keys: { view: true }," in r)


KEYS_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const m = /function keyFromView\(view\) \{[\s\S]*?\n    \}/.exec(src);
const keyFromView = new Function(m[0] + '; return keyFromView;')();
const views = ['key-harness-cursor', 'key-harness-antigravity', 'key-', 'key-../x', 'bastion:1',
               'key-Harness', 'key-' + 'a'.repeat(61), null, 'key-harness-cursor#x'];
process.stdout.write(JSON.stringify(views.map((v) => keyFromView(v))));
"""


def test_keys_opens_the_form():
    k = src("system-apps/keys/app.js")
    node = shutil.which("node")
    if not node:
        check("4. node is here to run the Keys view parser", False, "no node on PATH (NOT a pass)")
        return
    got = subprocess.run([node, "-e", KEYS_DRIVER, os.path.join(ROOT, "system-apps/keys/app.js")],
                         capture_output=True, text=True, timeout=60)
    try:
        names = json.loads(got.stdout)
    except ValueError:
        names = None
    check("4. 'key-<name>' names the key; anything else is ignored",
          names == ["harness-cursor", "harness-antigravity", None, None, None, None, None, None, None],
          (names, got.stderr[-300:]))
    body = k[k.index("function applyView(view)"):k.index("function viewFromHash()")]
    check("4. the view opens the Vault and Add a key with the name filled in",
          "state.section = 'vault'" in body and "openAdd({ mode: 'custom', suggest: name })" in body)
    check("4. a fresh window applies its #view after the first load; an open one takes it from "
          "the desktop only",
          "Promise.resolve(load()).then(function () { if (initialView) applyView(initialView); });" in k
          and "if (ev.source !== window.parent) return;" in k[k.index("function viewFromHash()"):])


GUARD_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const grab = (re) => { const m = re.exec(src); if (!m) throw new Error('missing ' + re); return m[0]; };
const kfv = grab(/function keyFromView\(view\) \{[\s\S]*?\n    \}/);
const av = grab(/function applyView\(view\) \{[\s\S]*?\n    \}/);
const cd = grab(/function customDynHTML\(hasProviders, suggest\) \{[\s\S]*?\n    \}/);
const esc = (x) => String(x).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
const out = {};
function run(scrimHidden, keys) {
    const opened = [];
    const scrim = { hidden: scrimHidden };
    const state = { section: 'providers', keys: keys };
    const f = new Function('$', 'state', 'render', 'openAdd', 'esc', 'TYPES',
        kfv + av + cd + '; return { applyView: applyView, customDynHTML: customDynHTML };');
    const api = f((id) => (id === 'keys-scrim' ? scrim : null), state, () => {},
                  (o) => opened.push(o), esc, [{ v: 'opaque', label: 'Opaque' }]);
    return { api, opened, state };
}
let r = run(true, []);
r.api.applyView('key-harness-cursor');
out.closed_opens = r.opened.length === 1 && r.state.section === 'vault';
r = run(false, []);
r.api.applyView('key-harness-cursor');
out.open_modal_kept = r.opened.length === 0 && r.state.section === 'providers';
r = run(true, [{ name: 'harness-cursor' }]);
out.held_warns = /already sealed[\s\S]*replaces/.test(r.api.customDynHTML(false, 'harness-cursor'));
r = run(true, [{ name: 'other' }]);
out.new_quiet = !/already sealed/.test(r.api.customDynHTML(false, 'harness-cursor'));
process.stdout.write(JSON.stringify(out));
"""


def test_a_view_never_clobbers_and_names_a_replace():
    node = shutil.which("node")
    if not node:
        check("5. node is here to run the Keys view guard", False, "no node on PATH (NOT a pass)")
        return
    got = subprocess.run([node, "-e", GUARD_DRIVER, os.path.join(ROOT, "system-apps/keys/app.js")],
                         capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(got.stdout)
    except ValueError:
        out = {}
    check("5. a view on a Keys window with nothing open opens the form", out.get("closed_opens") is True,
          (out, got.stderr[-300:]))
    check("5. a view never replaces a window the person has open (a paste, a reveal, a sign-in)",
          out.get("open_modal_kept") is True, (out, got.stderr[-300:]))
    check("5. a filled-in name that already holds a key says sealing replaces it",
          out.get("held_warns") is True, (out, got.stderr[-300:]))
    check("5. a filled-in name that holds nothing says nothing about replacing",
          out.get("new_quiet") is True, (out, got.stderr[-300:]))


def main() -> int:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(name, False, repr(e))
    print(f"\nFAILURES: {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
