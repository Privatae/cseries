#!/usr/bin/env python3
"""Gate: two launches of one app at once leave no orphan window hold.

dashboard/base/js/app-runner.js mints a kernel window id (and, for an app
that runs in its own pod, a beat timer that holds the pod) on every launch.
Two launches in the gap between its "already open?" check and recording the
window both minted ids, but WindowManager.create only FOCUSES an id that
already exists (desktop.js createWindow), so the second launch's close
handler was never registered and its id kept beating — the pod was held
forever. Triggers: a double-click on a floating bubble (floating-apps.js), the
Host opening an app over the websocket (ws-client.js) while the person
clicks it, a restore racing a deep link.

This runs the REAL app-runner.js in node with the browser surface stubbed
(fetch answering after a delay, timers recorded, a WindowManager whose create
behaves as desktop.js's does) and checks:
  1. a double-click mints ONE window id, creates one window, runs one beat
     timer, and closing the window leaves no timer beating;
  2. the Host's open (with a view) racing a click: the same, and the second
     launch takes the already-open path;
  3. a window that came up by another route while a launch was minting: the
     id this launch minted is closed and its timer stopped at once.

Needs node (it is on the gate host; a missing node FAILS here).
Run: APP_ROOT=<repo> python3 scripts/test_app_runner_launch_race.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT") or Path(__file__).resolve().parents[1])
JS = ROOT / "dashboard/base/js/app-runner.js"

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
const log = { launches: 0, closes: [], beats: [], timers: new Map(), created: [], focused: 0, nextId: 1 };
const wins = new Map();
const WindowManager = {
  create(opts) {
    if (wins.has(opts.id)) { log.focused++; return opts.id; }   // desktop.js: focus, return
    wins.set(opts.id, { id: opts.id, minimized: false, onClose: opts.onClose });
    log.created.push(opts.id);
    return opts.id;
  },
  getAll() { return Array.from(wins.values()); },
  focus() { log.focused++; }, restore() { log.focused++; }, minimize() {},
  notify() {},
  close(id) { const w = wins.get(id); wins.delete(id); if (w && w.onClose) w.onClose(); },
};
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
async function fetchStub(url, init) {
  await delay(5);
  if (url.endsWith('/launch')) {
    log.launches++;
    const wid = 'w' + (log.nextId++);
    return { ok: true, json: async () => ({ token: 't', window_id: wid, window_beat_s: 60 }) };
  }
  if (url.endsWith('/close')) { log.closes.push(JSON.parse(init.body).window_id); return { ok: true }; }
  if (url.endsWith('/window-beat')) { log.beats.push(JSON.parse(init.body).window_id); return { ok: true }; }
  return { ok: true, json: async () => ({ id: 'dash', name: 'Dash', window: {}, isolated: false }) };
}
let tid = 0;
const ctx = {
  console, URL, Promise, Map, Set, JSON, Date, Math, String, Object, Array, Error,
  fetch: fetchStub,
  setTimeout, clearTimeout,
  setInterval: (fn, ms) => { const id = ++tid; log.timers.set(id, ms); return id; },
  clearInterval: (id) => { log.timers.delete(id); },
  document: { querySelector: () => null, createElement: () => ({ style: {}, dataset: {}, addEventListener() {}, setAttribute() {} }) },
};
ctx.window = ctx;
ctx.window.location = { origin: 'https://h.example', href: 'https://h.example/', protocol: 'https:', host: 'h.example' };
ctx.window.addEventListener = () => {};
ctx.window.WindowManager = WindowManager;
vm.createContext(ctx);
vm.runInContext(src, ctx);
const AR = ctx.window.AppRunner;
(async () => {
  if (scenario === 'double') {
    await Promise.all([AR.launch('dash'), AR.launch('dash')]);
  } else if (scenario === 'host-and-click') {
    await Promise.all([AR.launch('dash', { view: 'chart' }), AR.launch('dash')]);
  } else if (scenario === 'appeared') {
    // the window comes up by another route while this launch is minting
    const p = AR.launch('dash');
    wins.set('app-dash', { id: 'app-dash', minimized: false, onClose: null });
    await p;
  }
  const open = log.timers.size;
  if (scenario !== 'appeared') WindowManager.close('app-dash');
  await delay(20);
  console.log(JSON.stringify({ launches: log.launches, created: log.created, focused: log.focused,
    timers_while_open: open, timers_after_close: log.timers.size, closes: log.closes }));
})();
"""

FAILS = []


def check(label, cond, detail=""):
    print(f"[launch-race] {'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def run(scenario: str) -> dict:
    d = Path(tempfile.mkdtemp(prefix="launch-race-", dir=os.environ.get("TMPDIR")))
    h = d / "h.js"
    h.write_text(HARNESS)
    r = subprocess.run(["node", str(h), str(JS), scenario], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise SystemExit(f"node failed: {r.stderr[-800:]}")
    return json.loads(r.stdout.strip().splitlines()[-1])


if not shutil.which("node"):
    print("[launch-race] FAIL node is not available")
    sys.exit(1)

r = run("double")
check("a double-click mints ONE window id and creates ONE window",
      r["launches"] == 1 and r["created"] == ["app-dash"], repr(r))
check("...holds the pod with ONE beat timer, and closing the window stops it",
      r["timers_while_open"] == 1 and r["timers_after_close"] == 0 and r["closes"] == ["w1"], repr(r))
check("...the second click takes the already-open path (focus)", r["focused"] >= 1, repr(r))

r = run("host-and-click")
check("the Host's open racing a click: one id, one window, nothing left beating after close",
      r["launches"] == 1 and r["created"] == ["app-dash"] and r["timers_after_close"] == 0, repr(r))

r = run("appeared")
check("a window that came up meanwhile: the id this launch minted is closed and stops beating",
      r["launches"] == 1 and r["created"] == [] and r["closes"] == ["w1"]
      and r["timers_after_close"] == 0, repr(r))

if FAILS:
    print(f"[launch-race] {len(FAILS)} FAILED")
    sys.exit(1)
print("[launch-race] all checks passed")
