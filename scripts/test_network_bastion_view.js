#!/usr/bin/env node
/*
 * The Network app opens the bastion a link names — view 'bastion:<token id>',
 * which Circles › Bastions › Open in Network sends through the shell.
 *
 * Boots the REAL app.js in a vm with a stub DOM (elements cached by id, as
 * test_network_anchor_step.js explains), runs its DOMContentLoaded handler so
 * the real message listener is installed, and drives both delivery paths the
 * shell uses: '#view=' on a fresh frame, and a 'cseries:app-view' message into an
 * open one. A message from anything but the framing desktop must be ignored.
 *
 * Run: node scripts/test_network_bastion_view.js   (test_bastion_directory.py runs it)
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

function makeEl() {
    return {
        _text: '',
        get textContent() { return this._text; },
        set textContent(v) { this._text = String(v == null ? '' : v); this.innerHTML = this._text; },
        innerHTML: '', hidden: false, dataset: {}, style: {}, value: '',
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener() {}, appendChild() {}, removeAttribute() {},
        setAttribute() {}, querySelectorAll() { return []; }, querySelector() { return null; },
        closest() { return null; }, select() {}, focus() {},
    };
}
const byId = {};
let ready = null;
const doc = {
    getElementById: (id) => (byId[id] = byId[id] || makeEl()),
    createElement: () => makeEl(),
    querySelectorAll: () => [],
    querySelector: () => makeEl(),
    addEventListener(type, fn) { if (type === 'DOMContentLoaded') ready = fn; },
    execCommand: () => true,
    body: makeEl(),
    hidden: false,
};
const MACHINES = [
    { id: 7, hostname: 'build-server-7', online: true, capabilities: [], models: [] },
    { id: 9, hostname: 'laptop-9', online: true, capabilities: [], models: [],
      peers: [{ host_name: 'wren.host.example', handle: 'wren', state: 'active' },
              { host_name: 'quill.host.example', handle: 'quill' }] },
];
const listeners = {};
let held = null;
const parentWin = {};
const sandbox = {
    document: doc,
    console,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    fetch: (url) => {
        if (!(String(url).startsWith('/api/network/bastions') && !String(url).includes('/bastions/'))) {
            return Promise.reject(new Error('no network in this test'));
        }
        const reply = { ok: true, status: 200, json: () => Promise.resolve({ bastions: MACHINES }) };
        // Held mode: each list fetch waits until the test releases it, so two
        // loads can be made to finish in a chosen order.
        if (held) return new Promise((resolve) => held.push(() => resolve(reply)));
        return Promise.resolve(reply);
    },
    navigator: { userAgent: 'node' },
    location: { href: 'http://localhost/', origin: 'http://localhost', hash: '#view=bastion%3A9' },
    parent: parentWin,
};
sandbox.addEventListener = (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); };
sandbox.removeEventListener = () => {};
sandbox.MessageChannel = function () { this.port1 = {}; this.port2 = {}; };
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(
    fs.readFileSync(path.join(__dirname, '..', 'dashboard', 'base', 'js', 'sandbox-csp.js'), 'utf8'),
    sandbox, { filename: 'sandbox-csp.js' });
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'system-apps', 'network', 'app.js'), 'utf8'),
    sandbox, { filename: 'app.js' });

const App = sandbox.window.NetworkApp;
const failures = [];
let checks = 0;
function check(cond, msg) { checks++; if (!cond) failures.push(msg); }
const settle = () => new Promise((r) => setImmediate(r));
async function flush() { for (let i = 0; i < 20; i++) await settle(); }
function inspectorShows(name) {
    return !byId.inspector.hidden && (byId['inspector-body'] || makeEl()).innerHTML.includes(name);
}

(async () => {
    check(typeof App.bastionFromView === 'function', 'bastionFromView is exported');
    check(App.bastionFromView('bastion:7') === 7, "'bastion:7' names token 7");
    for (const bad of ['bastion:', 'bastion:x', 'bastion:7x', ' bastion:7', 'inbox', '', null, undefined, 'bastion:1234567890123']) {
        check(App.bastionFromView(bad) === null, `'${bad}' names no bastion`);
    }

    // A fresh frame: '#view=bastion:9' opens the Bastions lens on laptop-9.
    check(typeof ready === 'function', 'the app installs a DOMContentLoaded handler');
    byId.inspector = makeEl();
    byId.inspector.hidden = true;
    ready();
    await flush();
    check(App.state.lens === 'bastions', `the hash view opens the Bastions lens (lens=${App.state.lens})`);
    check(inspectorShows('laptop-9'), 'the hash view opens the named machine\'s inspector');
    check(App.state.pendingBastion === null, 'the request is cleared once opened');
    // 'also serves': a peer the machine named without a state is not shown bare,
    // which would read the same as an established pairing.
    const body = (byId['inspector-body'] || makeEl()).innerHTML;
    check(body.includes('quill <span class="label-sys">(state not reported)</span>'),
          'a peer with no reported state says so');
    check(body.includes('wren') && !body.includes('wren <span'), 'an active peer carries no label');

    // An open window: the shell's message opens another machine.
    const onMessage = (listeners.message || []).slice(-1)[0];
    check(typeof onMessage === 'function', 'a message listener is installed');
    byId.inspector.hidden = true;
    onMessage({ source: {}, data: { type: 'cseries:app-view', view: 'bastion:7' } });
    await flush();
    check(byId.inspector.hidden === true, 'a view from anything but the framing desktop is ignored');
    onMessage({ source: parentWin, data: { type: 'cseries:app-view', view: 'bastion:7' } });
    await flush();
    check(inspectorShows('build-server-7'), 'a view from the desktop opens that machine');

    // A machine this Host does not have opens nothing and leaves no request behind.
    byId.inspector.hidden = true;
    App.applyView('bastion:404');
    await flush();
    check(byId.inspector.hidden === true, 'an unknown bastion opens no inspector');
    check(App.state.pendingBastion === null, 'an unknown bastion leaves no pending request');

    // Two loads in flight: one already running when the link arrives, then the
    // link's own. The OLDER one finishing first must not take the request, or
    // the newer one's close would take the inspector straight off the screen.
    held = [];
    byId.inspector.hidden = true;
    App.showLens('bastions');           // load A
    App.applyView('bastion:9');         // load B
    await flush();
    check(held.length === 2, `two list loads are in flight (got ${held.length})`);
    held[0]();                           // A finishes first
    await flush();
    held[1]();                           // then B
    await flush();
    held = null;
    check(inspectorShows('laptop-9'), 'a late older load does not leave the linked machine closed');
    check(App.state.pendingBastion === null, 'the request is cleared by the newest load');

    // The other order: the link's load finishes first and opens the machine,
    // then the older load lands. Its close must not take the inspector away.
    held = [];
    byId.inspector.hidden = true;
    App.showLens('bastions');           // load A
    App.applyView('bastion:7');         // load B
    await flush();
    held[1]();                           // B finishes first
    await flush();
    held[0]();                           // then A
    await flush();
    held = null;
    check(inspectorShows('build-server-7'), 'an older load landing last does not close the linked machine');

    // The newest load fails: the link is dropped, not left for a later load.
    const realFetch = sandbox.fetch;
    sandbox.fetch = () => Promise.reject(new Error('down'));
    App.applyView('bastion:9');
    await flush();
    check(App.state.pendingBastion === null, 'a link whose load failed is dropped');
    sandbox.fetch = realFetch;

    if (failures.length) {
        failures.forEach((f) => console.log('  FAIL  ' + f));
        console.log(`\n${failures.length} of ${checks} failed`);
        process.exit(1);
    }
    console.log(`network bastion view: ${checks}/${checks} checks passed`);
})().catch((e) => { console.log('  FAIL  ' + (e && e.stack || e)); process.exit(1); });
