#!/usr/bin/env node
/*
 * Network map views — headless check of the view-model + renderer registry.
 *
 * The Network lens draws its map by building ONE graph (buildGraph) and handing
 * it to a registered renderer (VIEWS). This pins that contract: every renderer
 * draws every machine in the graph, from the graph alone, and none of them
 * invents a node. It runs without a browser — a minimal DOM stub is enough,
 * because a renderer is a pure graph → markup function by design.
 *
 * Run: node scripts/test_network_views.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'system-apps', 'network', 'app.js');

// ── the smallest DOM the app's module body touches ──────────────────────
function makeEl() {
    // textContent -> innerHTML must actually round-trip. The app's esc() is
    // `d.textContent = s; return d.innerHTML`, so a stub that leaves innerHTML
    // empty makes EVERY escaped string in the tested output disappear — and the
    // assertions about that text then pass or fail on nothing.
    const el = {
        _text: '',
        get textContent() { return this._text; },
        set textContent(v) {
            this._text = String(v == null ? '' : v);
            this.innerHTML = this._text
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        },
        innerHTML: '', hidden: false, dataset: {}, style: {},
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener() {}, appendChild() {}, removeAttribute() {},
        setAttribute() {}, querySelectorAll() { return []; }, closest() { return null; },
    };
    return el;
}
const doc = {
    getElementById: () => makeEl(),
    createElement: () => makeEl(),
    querySelectorAll: () => [],
    querySelector: () => makeEl(),
    addEventListener() {},   // DOMContentLoaded never fires: init does not run
    body: makeEl(),
};
const sandbox = {
    document: doc,
    console,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    fetch: () => Promise.reject(new Error('no network in this test')),
    navigator: { userAgent: 'node' },
    location: { href: 'http://localhost/', origin: 'http://localhost' },
};
// The app binds a window-level listener for the host-map bridge's hello.
// Recorded rather than ignored, so a test can assert the door exists.
sandbox.listeners = {};
sandbox.addEventListener = (t, f) => { (sandbox.listeners[t] = sandbox.listeners[t] || []).push(f); };
sandbox.removeEventListener = () => {};
sandbox.MessageChannel = function () { this.port1 = {}; this.port2 = {}; };
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
// The page loads /js/sandbox-csp.js before app.js; load the REAL module here
// for the same reason — the map now refuses to mount without it (fail closed),
// so a stub that omitted it would test the refusal path and call it a pass.
vm.runInContext(
    fs.readFileSync(path.join(__dirname, '..', 'dashboard', 'base', 'js', 'sandbox-csp.js'), 'utf8'),
    sandbox, { filename: 'sandbox-csp.js' });
vm.runInContext(fs.readFileSync(APP, 'utf8'), sandbox, { filename: 'app.js' });

const App = sandbox.window.NetworkApp;

// ── failures are collected, not thrown, so one run reports everything ────
const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }

check(!!App, 'app.js did not export window.NetworkApp');
check(!!App && typeof App.buildGraph === 'function', 'NetworkApp.buildGraph is missing');
check(!!App && App.VIEWS && Object.keys(App.VIEWS).length >= 2,
      'expected at least two registered views (logical, physical)');
if (failures.length) { report(); process.exit(1); }

// ── a graph with every node kind, and a down machine ─────────────────────
App.state.overview = { identity: { handle: 'acme', entity_id: 42 } };
App.state.bastions = [
    {
        id: 7, name: 'desk', hostname: 'desktop-01', platform: 'linux',
        online: true, last_seen_at: '2026-09-07T17:00:00Z', reported_ip: '2001:db8::a:7',
        capabilities: ['embed'], models: ['mxbai-embed-large'], tunnel_status: 'up',
        // This box also serves two other Hosts — the thing the map exists to show.
        peers: [
            { host_name: 'beta.zone.example.test', handle: 'beta' },
            { host_name: 'gamma.zone.example.test', handle: 'gamma' },
        ],
        enrollment: {
            wg_bastion_pubkey: 'PUB', wg_endpoint: 'edge-a.example.test:51820',
            wg_host_ip: '2001:db8::e:42', wg_bastion_ip: '2001:db8::a:7',
        },
    },
    {   // never reported: must render as down, and must NOT be dropped
        id: 8, name: 'nas', hostname: null, platform: null,
        online: false, last_seen_at: null, reported_ip: null,
        capabilities: [], models: [], enrollment: null,
    },
];
App.state.peers = {
    secured_channels: [{ peer_entity_id: 99 }],
    contacts: [{ id: 5, kind: 'host', status: 'active', display_name: 'Beta Host' }],
};

const g = App.buildGraph();

// ── the graph itself ────────────────────────────────────────────────────
check(g.host.label === 'acme', `host label should come from the handle, got ${JSON.stringify(g.host.label)}`);
check(g.nodes.length === 6,
      `expected 6 nodes (2 machines + 2 machine-peers + peer + contact), got ${g.nodes.length}`);

// A Host that shares one of your machines hangs off THAT MACHINE, not off your
// Host — it has no link to your Host, and an edge to the hub would claim one.
const mp = g.nodes.filter(n => n.kind === 'machine-peer');
check(mp.length === 2, `both shared Hosts are on the graph (got ${mp.length})`);
check(mp.every(n => String(n.parent) === '7'),
      'a shared Host names the machine it shares');
check(g.edges.filter(e => e.kind === 'shares-machine').every(e => e.to === 7),
      'the shares-machine edge runs to the MACHINE, never to the host');
check(mp.every(n => n.via === 'desktop-01'),
      'a shared Host carries the machine name for its label');

const m1 = g.nodes.find(n => String(n.id) === '7');
const m2 = g.nodes.find(n => String(n.id) === '8');
check(!!m1 && m1.status === 'live', 'an online bastion must be live');
check(!!m2 && m2.status === 'down', 'a bastion that never reported must be down, not dropped');
check(!!m1 && m1.machine.endpoint === 'edge-a.example.test:51820',
      'the relay endpoint must come from the enrollment row');
check(!!m1 && m1.machine.tunnel_ip === '2001:db8::a:7', 'the tunnel address must be carried onto the node');
check(!!m2 && m2.machine.endpoint === null, 'a machine with no enrollment must report no endpoint, not a guess');

// every edge must land on the host, and name a node that exists
const ids = new Set(g.nodes.map(n => String(n.id)));
check(g.edges.length === g.nodes.length,
      `one edge per node, got ${g.edges.length} for ${g.nodes.length} nodes`);
// Not "everything points at the host" any more: a Host that shares one of your
// machines points at THAT MACHINE. Every edge must still land somewhere real.
const targets = new Set([...ids, 'host']);
check(g.edges.every(e => targets.has(String(e.to))),
      'every edge terminates at the host or at a node in the graph');
check(g.edges.filter(e => e.kind !== 'shares-machine').every(e => e.to === 'host'),
      'everything except a shared Host still hangs off your Host');
check(g.edges.every(e => ids.has(String(e.from))), 'an edge names a node that is not in the graph');

// the 16-node cap must drop the edges of dropped nodes too, or renderers
// would draw a line to nothing.
App.state.bastions = Array.from({ length: 30 }, (_, i) => ({
    id: 100 + i, name: 'b' + i, hostname: 'box-' + i, online: true,
    last_seen_at: '2026-09-07T17:00:00Z', capabilities: [], models: [], enrollment: null,
}));
const big = App.buildGraph();
const bigIds = new Set(big.nodes.map(n => String(n.id)));
check(big.nodes.length === 16, `the node cap should hold at 16, got ${big.nodes.length}`);
check(big.edges.every(e => bigIds.has(String(e.from))),
      'the node cap left an edge pointing at a dropped node');

// ── the BUILT-IN renderers, over the same graph ─────────────────────────
// 'hosted' is excluded here and asserted separately below: it does not draw
// the graph itself, it hands the graph to the host's own document. Holding it
// to "must emit SVG containing every machine" would be asserting the wrong
// contract, not a stricter one.
for (const [name, view] of Object.entries(App.VIEWS)) {
    if (name === 'hosted') continue;
    check(typeof view.label === 'string' && view.label.length > 0, `view "${name}" has no label`);
    check(typeof view.render === 'function', `view "${name}" has no render()`);
    let out;
    try {
        out = view.render(g);
    } catch (e) {
        failures.push(`view "${name}" threw: ${e.message}`);
        continue;
    }
    check(typeof out === 'string' && out.includes('<svg'), `view "${name}" did not return an SVG`);
    // Drawn from the graph: the live machine must appear by name in every view.
    check(out.includes('desktop-01'), `view "${name}" does not draw the machine desktop-01`);
    // EVERY machine, not just the reachable ones. A view that quietly omits an
    // offline machine tells the user their network is smaller than it is —
    // which is the exact failure this whole surface exists to prevent.
    const drawn = new Set([...out.matchAll(/data-node="([^"]+)"/g)].map(m => m[1]));
    for (const n of g.nodes.filter(x => x.kind === 'bastion')) {
        check(drawn.has(String(n.id)),
              `view "${name}" does not draw machine ${n.label} (status ${n.status}) — every machine must appear`);
    }
    // A view must not invent a node the graph does not have.
    check(!/box-2\b/.test(out), `view "${name}" drew a node that is not in this graph`);
    // Every clickable node must carry a data-node the click handler can resolve.
    const clickable = [...out.matchAll(/data-node="([^"]+)"/g)].map(m => m[1]);
    check(clickable.every(idv => ids.has(String(idv))),
          `view "${name}" has a data-node that resolves to no node`);
}

// ── the HOST-AUTHORED view ──────────────────────────────────────────────
// It is offered only when the host has designed a map, and when it renders it
// must put the document in a frame with no authority.
const hosted = App.VIEWS.hosted;
check(!!hosted, 'the host-authored view is registered');
if (hosted) {
    App.state.hostedDoc = null;
    const none = hosted.render(g);
    check(!/<iframe/i.test(none),
          'with no host-authored map, the view renders an explanation, not an empty frame');

    App.state.hostedDoc = '<!DOCTYPE html><html><head>'
        + '<meta name="cseries-network" content="map"></head>'
        + '<body><div data-cseries-nodes></div><script>CSeriesNetwork.on(function(){});</script></body></html>';
    const out = hosted.render(g);
    check(/<iframe/i.test(out), 'the host-authored map renders in a frame');
    check(/sandbox="allow-scripts"/.test(out),
          'the frame is sandboxed to allow-scripts ONLY — opaque origin, no session, no API reach');
    check(!/allow-same-origin/.test(out),
          'allow-same-origin is never granted: it would undo the sandbox entirely');
    check(out.includes('CSeriesNetwork'), 'the bridge is injected into the document');
    check(out.includes('Content-Security-Policy'),
          'the sandbox policy is injected with the bridge');

    // FAIL CLOSED: with the shared policy absent, the map must refuse to mount
    // rather than render host HTML unsealed. The live pod served /js/qrcode.js
    // 200 while /js/sandbox-csp.js was still 404, so this window is real.
    const savedSandbox = sandbox.CSeriesSandbox;
    sandbox.CSeriesSandbox = undefined;
    const unsealed = hosted.render(g);
    check(!/<iframe/i.test(unsealed),
          'with no sandbox policy loaded, the map refuses to mount a frame at all');
    check(/did not load/i.test(unsealed),
          'and it says why, rather than showing an empty panel');
    sandbox.CSeriesSandbox = savedSandbox;

    // The bridge must run BEFORE any script the document brought, or a document
    // script could take the channel first.
    const decoded = out.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
    const bridgeAt = decoded.indexOf('MessageChannel');
    const docScriptAt = decoded.indexOf('CSeriesNetwork.on');
    check(bridgeAt > -1 && docScriptAt > -1 && bridgeAt < docScriptAt,
          `the bridge is injected ahead of the document's own scripts (bridge ${bridgeAt}, doc ${docScriptAt})`);
    // …and after the DOCTYPE, not before it, or the document is quirks-mode.
    check(/^\s*<!DOCTYPE/i.test(decoded.slice(decoded.indexOf('srcdoc="') + 8)),
          'the DOCTYPE still leads the document');
    App.state.hostedDoc = null;
}

// An empty graph must not throw in any view.
App.state.bastions = []; App.state.peers = { secured_channels: [], contacts: [] };
const emptyGraph = App.buildGraph();
check(emptyGraph.nodes.length === 0, 'an empty network should build an empty graph');
for (const [name, view] of Object.entries(App.VIEWS)) {
    try { view.render(emptyGraph); }
    catch (e) { failures.push(`view "${name}" threw on an empty graph: ${e.message}`); }
}

report();
process.exit(failures.length ? 1 : 0);

function report() {
    if (!failures.length) {
        const names = Object.keys(App && App.VIEWS ? App.VIEWS : {}).join(', ');
        console.log(`ok — view-model + renderers (${names})`);
        return;
    }
    console.error(`FAIL — ${failures.length} problem(s):`);
    for (const f of failures) console.error('  · ' + f);
}
