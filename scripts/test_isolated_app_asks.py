#!/usr/bin/env python3
"""Gate: an isolated app's asks reach the desktop, from its own frame only.

Every "Connect ..." button and "Open Keys" in the Orchestrator did nothing on
every Host since 0.1.15. The Orchestrator runs on its own origin
(server/applications/kernel.py SDK_MANAGED_APPS), so model-picker.js's
`window.parent.AppRunner` throws there and it falls back to posting
{type: 'cseries:open-app', ...} to the desktop, whose only message relay
dropped anything not from the desktop's own origin. The App Store's Open and
Settings' Desktop tab, wallpaper repaint and boot screen died the same way.

app-runner.js now gives each isolated window its own listener, which takes a
message only when event.source is that window's frame and event.origin is
that app's own origin, and then only an ask the app is listed for
(isolatedAsk). This runs the REAL app-runner.js in node with the browser
surface stubbed and checks:
  P. the policy (isolatedAsk): what each listed app may ask, and that a
     community app, an unlisted app, an unlisted target, a view or prompt the
     rule does not allow, a prompt over 2000 characters and malformed asks are
     refused;
  W. the wiring: the Orchestrator's open-app (Keys, Circles with the Connect
     seed, Network with a view) opens the window; the same message from
     another window on the right origin, or from the frame on another origin,
     opens nothing; a refused ask opens nothing; Settings' layout ask is
     answered into its own frame on its own origin; closing the window
     removes its listener; the desktop-wide relay still refuses another
     origin.

Needs node (a missing node FAILS here).
Run: APP_ROOT=<repo> python3 scripts/test_isolated_app_asks.py
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
const KEYS = { orchestrator: 'abcdefghijklmnopqrst', settings: 'bcdefghijklmnopqrstu',
               'app-store': 'cdefghijklmnopqrstuv', oracle: 'defghijklmnopqrstuvw',
               evil: 'efghijklmnopqrstuvwx' };
const PROV = { evil: 'community' };
const ISO = new Set(Object.keys(KEYS));
const out = { created: [], posted: [], warned: [], wallpaper: 0, boot: [], layout: [] };
const listeners = [];
const frames = {};
const wins = new Map();
const WindowManager = {
  create(opts) {
    if (wins.has(opts.id)) return opts.id;
    wins.set(opts.id, { id: opts.id, minimized: false, onClose: opts.onClose });
    out.created.push(opts.id);
    const host = { appendChild(f) { frames[opts.appId] = f; }, clientWidth: 900, clientHeight: 600 };
    if (opts.onInit) opts.onInit({ querySelector: () => host });
    return opts.id;
  },
  getAll() { return Array.from(wins.values()); },
  focus() {}, restore() {}, minimize() {}, notify() {},
  close(id) { const w = wins.get(id); wins.delete(id); if (w && w.onClose) w.onClose(); },
};
function manifestFor(id) {
  return { id, name: id, window: {}, url: '/system-apps/' + id + '/index.html',
           isolated: ISO.has(id), sdk_managed: ISO.has(id),
           provenance: PROV[id] || 'native', origin_key: KEYS[id] };
}
async function fetchStub(url, init) {
  const m = /^\/api\/applications\/([^/]+)(\/.*)?$/.exec(url);
  if (m && m[2] === '/launch') return { ok: true, json: async () => ({ token: 't', window_id: 'w-' + m[1], window_beat_s: 0 }) };
  if (m && !m[2]) return { ok: true, json: async () => manifestFor(decodeURIComponent(m[1])) };
  return { ok: true, json: async () => ({}) };
}
function makeFrame() {
  const cw = { posted: [], postMessage(msg, origin) { out.posted.push({ msg, origin }); } };
  return { style: {}, dataset: {}, contentWindow: cw, src: '', title: '',
           setAttribute() {}, addEventListener() {} };
}
const ctx = {
  console: { log() {}, debug() {}, error() {}, warn(m) { out.warned.push(String(m)); } },
  URL, Promise, Map, Set, JSON, Date, Math, String, Object, Array, Error,
  fetch: fetchStub, setTimeout, clearTimeout, setInterval: () => 1, clearInterval() {},
  document: { querySelector: () => null, createElement: () => makeFrame() },
  CustomEvent: function () {},
};
ctx.window = ctx;
ctx.window.location = { origin: 'https://h.example', href: 'https://h.example/', protocol: 'https:', host: 'h.example' };
ctx.window.addEventListener = (t, fn) => { if (t === 'message') listeners.push(fn); };
ctx.window.removeEventListener = (t, fn) => { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); };
ctx.window.WindowManager = WindowManager;
ctx.window.applyWallpaper = () => { out.wallpaper++; };
ctx.window.WSClient = { showBootScreen() { out.boot.push('show'); }, hideBootScreen() { out.boot.push('hide'); } };
const lay = { dock: ['circles'], desk: [] };
ctx.window.DesktopLayout = {
  ready: Promise.resolve(),
  pin(id) { out.layout.push('pin:' + id); if (!lay.dock.includes(id)) lay.dock.push(id); },
  unpin(id) { out.layout.push('unpin:' + id); lay.dock = lay.dock.filter((x) => x !== id); },
  addToDesktop(id) { out.layout.push('add:' + id); lay.desk.push(id); },
  removeFromDesktop(id) { out.layout.push('remove:' + id); lay.desk = lay.desk.filter((x) => x !== id); },
  resetLayout() { out.layout.push('reset'); },
  dockPinned() { return lay.dock.slice(); },
  desktopIds() { return lay.desk.slice(); },
};
vm.createContext(ctx);
vm.runInContext(src, ctx);
const AR = ctx.window.AppRunner;
const tick = () => new Promise((r) => setTimeout(r, 30));
const originOf = (id) => 'https://apps-' + KEYS[id] + '-h.example';
function send(fromId, data, opts) {
  opts = opts || {};
  const f = frames[fromId];
  const ev = { source: opts.source || (f && f.contentWindow), origin: opts.origin || originOf(fromId), data };
  listeners.slice().forEach((fn) => fn(ev));
}
(async () => {
  const res = {};
  // ── P: the policy ──
  const M = manifestFor;
  const ask = (m, d) => JSON.parse(JSON.stringify(AR.isolatedAsk(m, d)));
  const seed = "Connect Claude Code for me. Run its own login (harness_connections op='login', provider='claude-code') and walk me through finishing it.";
  res.p = {
    keys: ask(M('orchestrator'), { type: 'cseries:open-app', app_id: 'keys' }),
    keys_undef_prompt: ask(M('orchestrator'), { type: 'cseries:open-app', app_id: 'keys', prompt: undefined }),
    circles_seed: ask(M('orchestrator'), { type: 'cseries:open-app', app_id: 'circles', prompt: seed }),
    network_view: ask(M('orchestrator'), { type: 'cseries:open-app', app_id: 'network', view: 'logical' }),
    keys_view: ask(M('orchestrator'), { type: 'cseries:open-app', app_id: 'keys', view: 'key-harness-cursor' }),
    orch_settings: ask(M('orchestrator'), { type: 'cseries:open-app', app_id: 'settings' }),
    orch_keys_prompt: ask(M('orchestrator'), { type: 'cseries:open-app', app_id: 'keys', prompt: 'hi' }),
    orch_circles_view: ask(M('orchestrator'), { type: 'cseries:open-app', app_id: 'circles', view: 'x' }),
    orch_network_badview: ask(M('orchestrator'), { type: 'cseries:open-app', app_id: 'network', view: '../x' }),
    orch_long_prompt: ask(M('orchestrator'), { type: 'cseries:open-app', app_id: 'circles', prompt: 'x'.repeat(2001) }),
    orch_blank_prompt: ask(M('orchestrator'), { type: 'cseries:open-app', app_id: 'circles', prompt: '   ' }),
    orch_wallpaper: ask(M('orchestrator'), { type: 'cseries:wallpaper-changed' }),
    orch_layout: ask(M('orchestrator'), { type: 'cseries:layout', op: 'get' }),
    orch_proto: ask(M('orchestrator'), { type: 'cseries:open-app', app_id: '__proto__' }),
    store_any: ask(M('app-store'), { type: 'cseries:open-app', app_id: 'sites' }),
    store_view: ask(M('app-store'), { type: 'cseries:open-app', app_id: 'sites', view: 'catalog' }),
    store_prompt: ask(M('app-store'), { type: 'cseries:open-app', app_id: 'circles', prompt: 'hi' }),
    store_badid: ask(M('app-store'), { type: 'cseries:open-app', app_id: 'Sites/../x' }),
    store_self: ask(M('app-store'), { type: 'cseries:open-app', app_id: 'app-store' }),
    settings_wp: ask(M('settings'), { type: 'cseries:wallpaper-changed' }),
    settings_boot: ask(M('settings'), { type: 'cseries:boot-screen', show: true }),
    settings_boot_bad: ask(M('settings'), { type: 'cseries:boot-screen', show: 'yes' }),
    settings_pin: ask(M('settings'), { type: 'cseries:layout', op: 'pin', app_id: 'oracle', defaults: ['settings'] }),
    settings_pin_noid: ask(M('settings'), { type: 'cseries:layout', op: 'pin' }),
    settings_badop: ask(M('settings'), { type: 'cseries:layout', op: 'setDockPinned', app_id: 'x' }),
    settings_baddefs: ask(M('settings'), { type: 'cseries:layout', op: 'get', defaults: [1] }),
    settings_open: ask(M('settings'), { type: 'cseries:open-app', app_id: 'keys' }),
    oracle_open: ask(M('oracle'), { type: 'cseries:open-app', app_id: 'keys' }),
    community_orch: ask(Object.assign(M('orchestrator'), { provenance: 'community' }), { type: 'cseries:open-app', app_id: 'keys' }),
    custom_orch: ask(Object.assign(M('orchestrator'), { provenance: 'custom' }), { type: 'cseries:open-app', app_id: 'keys' }),
    not_isolated: ask(Object.assign(M('orchestrator'), { isolated: false }), { type: 'cseries:open-app', app_id: 'keys' }),
    no_data: ask(M('orchestrator'), null),
    string_data: ask(M('orchestrator'), 'cseries:open-app'),
  };
  // ── W: the wiring ──
  await AR.launch('orchestrator'); await tick();
  const base = out.created.length;
  const stranger = { postMessage() {} };
  send('orchestrator', { type: 'cseries:open-app', app_id: 'keys' }, { source: stranger });            // another window, right origin
  send('orchestrator', { type: 'cseries:open-app', app_id: 'keys' }, { origin: 'https://evil.example' }); // the frame, wrong origin
  send('orchestrator', { type: 'cseries:open-app', app_id: 'settings' });                               // not on its list
  await tick();
  res.w_refused = out.created.slice(base);
  res.w_warned = out.warned.filter((w) => w.includes('may not ask')).length;
  send('orchestrator', { type: 'cseries:open-app', app_id: 'keys' }); await tick();
  send('orchestrator', { type: 'cseries:open-app', app_id: 'circles', prompt: seed }); await tick();
  send('orchestrator', { type: 'cseries:open-app', app_id: 'network', view: 'logical' }); await tick();
  res.w_opened = out.created.slice(base);
  res.w_network_src = frames.network ? frames.network.src : null;
  res.w_orch_src = frames.orchestrator ? frames.orchestrator.src : null;
  // Settings: its layout ask is answered into its own frame, on its origin.
  await AR.launch('settings'); await tick();
  out.posted.length = 0;
  send('settings', { type: 'cseries:layout', op: 'pin', app_id: 'oracle', defaults: ['settings'] }); await tick();
  send('settings', { type: 'cseries:wallpaper-changed' });
  send('settings', { type: 'cseries:boot-screen', show: true });
  send('settings', { type: 'cseries:layout', op: 'pin', app_id: 'evil' }, { source: stranger }); await tick();
  res.w_layout = out.layout.slice();
  res.w_layout_reply = out.posted.map((p) => ({ type: p.msg.type, op: p.msg.op, dock: p.msg.dock, origin: p.origin }));
  res.w_wallpaper = out.wallpaper; res.w_boot = out.boot.slice();
  // Closing the Orchestrator removes its listener: nothing it posts opens a window.
  const before = listeners.length;
  WindowManager.close('app-keys'); WindowManager.close('app-orchestrator');
  res.w_listeners_dropped = before - listeners.length;
  const n = out.created.length;
  send('orchestrator', { type: 'cseries:open-app', app_id: 'keys' }); await tick();
  res.w_after_close = out.created.slice(n);
  // A community app and an unlisted first-party app ask for Keys: refused.
  await AR.launch('evil'); await AR.launch('oracle'); await tick();
  const m2 = out.created.length;
  send('evil', { type: 'cseries:open-app', app_id: 'keys' });
  send('oracle', { type: 'cseries:open-app', app_id: 'keys' }); await tick();
  res.w_unlisted = out.created.slice(m2);
  // The desktop-wide relay: a foreign origin's open-app opens nothing; the desktop's own does.
  const m3 = out.created.length;
  listeners.slice().forEach((fn) => fn({ source: stranger, origin: originOf('orchestrator'), data: { type: 'cseries:open-app', app_id: 'keys' } }));
  await tick();
  res.w_relay_foreign = out.created.slice(m3);
  listeners.slice().forEach((fn) => fn({ source: stranger, origin: 'https://h.example', data: { type: 'cseries:open-app', app_id: 'keys' } }));
  await tick();
  res.w_relay_own = out.created.slice(m3);
  console.log(JSON.stringify(res));
})().catch((e) => { console.error(e && e.stack || e); process.exit(2); });
"""

FAILS = []


def check(label, cond, detail=""):
    print(f"[isolated-asks] {'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


if not shutil.which("node"):
    print("[isolated-asks] FAIL node is not available")
    sys.exit(1)

d = Path(tempfile.mkdtemp(prefix="isolated-asks-", dir=os.environ.get("TMPDIR")))
try:
    h = d / "h.js"
    h.write_text(HARNESS)
    r = subprocess.run(["node", str(h), str(JS)], capture_output=True, text=True, timeout=60)
finally:
    shutil.rmtree(d, ignore_errors=True)
if r.returncode != 0:
    print(f"[isolated-asks] FAIL node: {r.stderr[-1200:]}")
    sys.exit(1)
res = json.loads(r.stdout.strip().splitlines()[-1])
p = res["p"]
SEED = ("Connect Claude Code for me. Run its own login (harness_connections op='login', "
        "provider='claude-code') and walk me through finishing it.")

# ── P ──
check("P orchestrator may open Keys", p["keys"] == {"kind": "open", "app_id": "keys"}, repr(p["keys"]))
check("P ...with prompt: undefined (model-picker's _openApp('keys'))",
      p["keys_undef_prompt"] == {"kind": "open", "app_id": "keys"}, repr(p["keys_undef_prompt"]))
check("P orchestrator may open Circles with the Connect seed",
      p["circles_seed"] == {"kind": "open", "app_id": "circles", "prompt": SEED}, repr(p["circles_seed"]))
check("P orchestrator may open Network on a view",
      p["network_view"] == {"kind": "open", "app_id": "network", "view": "logical"}, repr(p["network_view"]))
check("the Orchestrator may open Keys at the form for an engine's own key (its Connect)",
      p["keys_view"] == {"kind": "open", "app_id": "keys", "view": "key-harness-cursor"}, repr(p["keys_view"]))
for k, why in [("orch_settings", "an app not on its list"), ("orch_keys_prompt", "a prompt to Keys"),
               ("orch_circles_view", "a view on Circles"), ("orch_network_badview", "a view that is not a name"),
               ("orch_long_prompt", "a prompt over 2000 characters"), ("orch_blank_prompt", "a blank prompt"),
               ("orch_wallpaper", "the wallpaper"), ("orch_layout", "the layout"),
               ("orch_proto", "__proto__ as the app id")]:
    check(f"P orchestrator may NOT ask {why}", p[k] is None, repr(p[k]))
check("P App Store may open an installed app", p["store_any"] == {"kind": "open", "app_id": "sites"}, repr(p["store_any"]))
for k, why in [("store_view", "a view"), ("store_prompt", "a prompt"), ("store_badid", "a malformed id"),
               ("store_self", "itself")]:
    check(f"P App Store may NOT open with {why}", p[k] is None, repr(p[k]))
check("P Settings may repaint the wallpaper", p["settings_wp"] == {"kind": "wallpaper"}, repr(p["settings_wp"]))
check("P Settings may show the boot screen", p["settings_boot"] == {"kind": "boot-screen", "show": True}, repr(p["settings_boot"]))
check("P Settings may pin an app",
      p["settings_pin"] == {"kind": "layout", "op": "pin", "app_id": "oracle", "defaults": ["settings"]}, repr(p["settings_pin"]))
for k, why in [("settings_boot_bad", "a boot screen with a non-boolean show"), ("settings_pin_noid", "a pin with no app"),
               ("settings_badop", "a layout op not listed"), ("settings_baddefs", "defaults that are not ids"),
               ("settings_open", "to open an app")]:
    check(f"P Settings may NOT ask {why}", p[k] is None, repr(p[k]))
for k, why in [("oracle_open", "an isolated first-party app with no listing"),
               ("community_orch", "a community app, even under a listed id"),
               ("custom_orch", "a custom (Workbench-built) app under a listed id"),
               ("not_isolated", "a same-origin app (it never needs this lane)"),
               ("no_data", "a null message"), ("string_data", "a string message")]:
    check(f"P refused: {why}", p[k] is None, repr(p[k]))

# ── W ──
check("W the open-app from another window on the right origin, from the frame on a wrong origin, "
      "or for an unlisted app opens nothing", res["w_refused"] == [], repr(res["w_refused"]))
check("W ...and the unlisted ask is warned about", res["w_warned"] >= 1, repr(res["w_warned"]))
check("W the Orchestrator's frame opens Keys, Circles and Network",
      res["w_opened"] == ["app-keys", "app-circles", "app-network"], repr(res["w_opened"]))
check("W Network opens on the view asked for", (res["w_network_src"] or "").endswith("#view=logical"), repr(res["w_network_src"]))
check("W the Orchestrator's frame is on its own origin",
      (res["w_orch_src"] or "").startswith("https://apps-abcdefghijklmnopqrst-h.example/system-apps/orchestrator/"),
      repr(res["w_orch_src"]))
check("W Settings' pin reaches the desktop layout; the stranger's does not", res["w_layout"] == ["pin:oracle"], repr(res["w_layout"]))
check("W the layout answer goes into Settings' frame on Settings' origin only",
      res["w_layout_reply"] == [{"type": "cseries:layout-state", "op": "pin", "dock": ["circles", "oracle"],
                                 "origin": "https://apps-bcdefghijklmnopqrstu-h.example"}], repr(res["w_layout_reply"]))
check("W Settings repaints the wallpaper and shows the boot screen",
      res["w_wallpaper"] == 1 and res["w_boot"] == ["show"], repr((res["w_wallpaper"], res["w_boot"])))
check("W closing the Orchestrator removes its listener", res["w_listeners_dropped"] >= 1 and res["w_after_close"] == [],
      repr((res["w_listeners_dropped"], res["w_after_close"])))
check("W a community app and an unlisted first-party app cannot open Keys", res["w_unlisted"] == [], repr(res["w_unlisted"]))
check("W the desktop-wide relay refuses an isolated origin", res["w_relay_foreign"] == [], repr(res["w_relay_foreign"]))
check("W ...and still takes the desktop's own origin", res["w_relay_own"] == ["app-keys"], repr(res["w_relay_own"]))

if FAILS:
    print(f"[isolated-asks] {len(FAILS)} FAILED")
    sys.exit(1)
print("[isolated-asks] all checks passed")
