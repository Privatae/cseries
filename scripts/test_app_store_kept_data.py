#!/usr/bin/env python3
"""The App Store's uninstall choice, its list of data kept from removed apps,
and the catalog's notices (system-apps/app-store/app.js), run for real in Node
against a small stand-in DOM and a fetch that answers as the Host's routes do.

  * Uninstall's confirm shows "Also delete this app's data", UNTICKED, with
    the size this machine keeps for the app (GET /api/applications/runtime/
    app-data); confirming without ticking sends DELETE /api/applications/<id>
    with no purge, and with it ticked sends ?purge=true;
  * a confirm the list re-rendered away (the catalog reloads every minute) is
    armed again by the next click, never acted on with a box no longer shown;
  * "Data kept from removed apps" lists only removed apps' data, with sizes and
    a Delete that asks twice before DELETE /api/applications/runtime/app-data/<id>;
  * the catalog's notices show above the list, escaped.

Container-free: python3 scripts/test_app_store_kept_data.py (needs node).
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("APP_ROOT") or pathlib.Path(__file__).resolve().parent.parent)
APP_JS = ROOT / "system-apps" / "app-store" / "app.js"

HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.env.APP_JS, 'utf8');

function el(id) {
  const e = {
    id, hidden: true, textContent: '', disabled: false, value: '', _html: '', isConnected: true,
    children: [], attrs: {}, classes: new Set(),
    classList: { add: c => e.classes.add(c), remove: c => e.classes.delete(c),
                 toggle: (c, on) => (on ? e.classes.add(c) : e.classes.delete(c)) },
    get innerHTML() { return e._html; },
    set innerHTML(v) { e._html = String(v); for (const k of e.children) k.isConnected = false; e.children = []; },
    setAttribute(k, v) { e.attrs[k] = String(v); },
    getAttribute(k) { return k in e.attrs ? e.attrs[k] : null; },
    addEventListener() {}, focus() {},
    querySelector(sel) { return sel === 'input[type="checkbox"]' ? (e.input || null) : null; },
    querySelectorAll() { return []; },
    insertAdjacentElement(where, other) { (e.parent || e).children.push(other); other.isConnected = true; },
    remove() { e.isConnected = false; },
    closest() { return null; },
  };
  return e;
}
const els = {};
const document = {
  getElementById(id) { return els[id] || (els[id] = el(id)); },
  querySelectorAll() { return []; },
  addEventListener(t, f) { (document._on[t] = document._on[t] || []).push(f); },
  createElement(tag) {
    const e = el(tag);
    e.input = { type: 'checkbox', checked: false };
    return e;
  },
  activeElement: null,
  _on: {},
};
const calls = [];
const answers = {
  'GET /api/applications/store/catalog': { apps: [
      { app_id: 'notes', name: 'Notes', install_status: 'up_to_date', latest_version: '1.0.0',
        installed_version: '1.0.0', runtime_kind: 'process' },
      { app_id: 'photos', name: 'Photos', install_status: 'not_installed', latest_version: '2.0.0' }],
    notices: [{ kind: 'catalog_old', text: 'The App Store\'s catalog was issued on 2026-08-01 <b>' }] },
  'GET /api/applications/runtime/app-data': { available: true, apps: [
      { app_id: 'notes', bytes: 5 * 1048576, complete: true, installed: true },
      { app_id: 'photos', bytes: 3 * 1073741824, complete: false, installed: false }] },
};
async function fetch(url, opts) {
  const m = ((opts && opts.method) || 'GET') + ' ' + url;
  calls.push(m);
  const body = answers[m.split('?')[0]] || answers[m] || {};
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
}
const ctx = { document, window: { parent: null }, fetch, console, location: { origin: 'http://h' },
              setTimeout: (() => { let n = 0; return () => ++n; })(), clearTimeout() {},
              setInterval() { return 0; }, Date, Promise,
              JSON, Math, String, Number, Array, Object, encodeURIComponent, isNaN, parseInt };
vm.createContext(ctx);
vm.runInContext(src, ctx);
const tick = () => new Promise(r => setImmediate(r));
function target(attrName, id, extra) {
  const b = el('btn-' + attrName + '-' + id);
  b.attrs[attrName] = id;
  b.textContent = extra || 'Uninstall';
  b.closest = (sel) => (sel === '[' + attrName + ']' ? b : null);
  return b;
}
function click(t) {
  for (const f of document._on.click || []) f({ target: t, stopPropagation() {} });
}
(async () => {
  for (const f of document._on.DOMContentLoaded || []) f();
  for (let i = 0; i < 10; i++) await tick();
  const out = {};
  const $ = (id) => document.getElementById(id);
  out.notices = { hidden: $('store-notices').hidden, html: $('store-notices').innerHTML };
  out.kept = { hidden: $('kept-data-section').hidden, html: $('kept-data-list').innerHTML };

  let b = target('data-app-uninstall', 'notes');
  click(b);
  const box = b.children[0];
  out.armed = { text: b.textContent, box: box ? box._html : null, ticked: box ? box.input.checked : null,
                isLabel: box ? box.attrs['data-uninstall-data-box'] : null };
  click(b);
  for (let i = 0; i < 10; i++) await tick();
  out.after_untick = calls.filter(c => c.startsWith('DELETE'));

  b = target('data-app-uninstall', 'notes');
  click(b);
  if (b.children[0]) b.children[0].input.checked = true;
  click(b);
  for (let i = 0; i < 10; i++) await tick();
  out.after_tick = calls.filter(c => c.startsWith('DELETE'));

  b = target('data-app-uninstall', 'notes');
  click(b);
  if (b.children[0]) {
    b.children[0].isConnected = false;        // the list re-rendered under it
    b.children[0].input.checked = true;
  }
  const nb = target('data-app-uninstall', 'notes');
  click(nb);
  for (let i = 0; i < 10; i++) await tick();
  out.after_rerender = calls.filter(c => c.startsWith('DELETE'));
  out.rearmed = nb.textContent;

  const k = target('data-kept-delete', 'photos', 'Delete');
  click(k);
  out.kept_first = { text: k.textContent, deletes: calls.filter(c => c.startsWith('DELETE')).length };
  click(k);
  for (let i = 0; i < 10; i++) await tick();
  out.after_kept = calls.filter(c => c.startsWith('DELETE'));
  process.stdout.write(JSON.stringify(out));
})().catch(e => { process.stdout.write(JSON.stringify({ error: String(e && e.stack || e) })); });
"""

FAILURES = []


def check(cond, name, detail=""):
    if cond:
        print(f"OK    {name}")
    else:
        print(f"FAIL  {name} {detail}")
        FAILURES.append(name)


node = shutil.which("node")
if not node:
    print("SKIP  node is not installed here")
    sys.exit(0 if os.environ.get("CSERIES_REQUIRE_NODE") != "1" else 1)
r = subprocess.run([node, "-"], input=HARNESS, capture_output=True, text=True, timeout=60,
                   env={**os.environ, "APP_JS": str(APP_JS)})
try:
    out = json.loads(r.stdout or "{}")
except ValueError:
    out = {"error": r.stdout[-500:] + r.stderr[-500:]}
check("error" not in out, "the App Store runs in the harness", out.get("error", "")[:600])

n = out.get("notices") or {}
check(n.get("hidden") is False and "issued on 2026-08-01" in n.get("html", "") and
      "&lt;b&gt;" in n.get("html", "") and "<b>" not in n.get("html", ""),
      "the catalog's notice shows above the list, escaped", n)
k = out.get("kept") or {}
check(k.get("hidden") is False and "Photos" in k.get("html", "") and "at least 3.0 GB" in k.get("html", "")
      and 'data-kept-delete="photos"' in k.get("html", "") and "notes" not in k.get("html", "").lower(),
      "data kept from removed apps is listed with its size and a Delete; an installed app's is not", k)
a = out.get("armed") or {}
check(a.get("text") == "Confirm remove" and a.get("ticked") is False and a.get("isLabel") == "notes" and
      "Also delete this app" in (a.get("box") or "") and "5.0 MB on this machine" in (a.get("box") or "") and
      "checked" not in (a.get("box") or ""),
      "Uninstall's confirm offers \"Also delete this app's data\", unticked, with its size", a)
check(out.get("after_untick") == ["DELETE /api/applications/notes"],
      "confirmed without the tick, the uninstall keeps the data (no purge)", out.get("after_untick"))
check(out.get("after_tick") == ["DELETE /api/applications/notes", "DELETE /api/applications/notes?purge=true"],
      "confirmed with it ticked, the uninstall deletes the data (?purge=true)", out.get("after_tick"))
check(out.get("after_rerender") == out.get("after_tick") and out.get("rearmed") == "Confirm remove",
      "a confirm the list re-rendered away is armed again, not acted on", (out.get("after_rerender"),
                                                                           out.get("rearmed")))
kf = out.get("kept_first") or {}
check(kf.get("text") == "Delete for good" and kf.get("deletes") == 2,
      "deleting kept data asks once more first", kf)
check((out.get("after_kept") or [])[-1:] == ["DELETE /api/applications/runtime/app-data/photos"],
      "and then deletes that removed app's data", out.get("after_kept"))

print()
if FAILURES:
    print(f"FAIL ({len(FAILURES)})")
    sys.exit(1)
print("all App Store kept-data checks passed")
