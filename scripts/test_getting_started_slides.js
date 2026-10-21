#!/usr/bin/env node
/*
 * Getting Started's slides and the engine's one addition: a slide that opens
 * its app at a view (the Name slide opens Settings at About, where the name
 * is saved; the Privatae network slide opens Settings at Network). The
 * walkthrough never saves the name itself: renaming takes Settings' own
 * token (server/routes/host_name.py). Loads the REAL
 * steps.js and app.js in a vm with a stub DOM and a stub fetch.
 * Run: node scripts/test_getting_started_slides.js  (test_getting_started_slides.py runs it)
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'system-apps', 'getting-started');
const failures = []; let checks = 0;
function check(cond, msg) { checks++; if (!cond) failures.push(msg); }

// ── the slides, as data ───────────────────────────────────────────────
const sb0 = { window: {} };
vm.createContext(sb0);
vm.runInContext(fs.readFileSync(path.join(APP, 'steps.js'), 'utf8'), sb0);
const SLIDES = sb0.window.GS_SLIDES;
const ids = SLIDES.map((s) => s.id);
check(ids.join(',') === 'welcome,name,keys,orchestrator,privatae-network,circles,workbench,done',
    'slide order: ' + ids.join(','));
const name = SLIDES.find((s) => s.id === 'name');
check(!!name && ids.indexOf('name') === ids.indexOf('welcome') + 1, 'Name your Host comes right after Welcome');
check(name && name.title === 'Name your Host' && name.button === 'Open Settings', 'the Name slide is titled and opens Settings');
check(name && name.appId === 'settings' && name.appView === 'overview' && !name.input,
    'it opens Settings at About and asks for nothing itself');
check(!SLIDES.some((s) => s.input || /\/api\/system\/name/.test(JSON.stringify(s))),
    'no slide sends the name');
check(name && name.check.endpoint === '/api/system/status', 'its check reads the current name');
check(name && name.check.evaluate({ entity_display_name: 'C-Series' }) === false, 'the default name has not passed');
check(name && name.check.evaluate({ entity_display_name: 'Host #1' }) === false, 'a placeholder has not passed');
check(name && name.check.evaluate({ entity_display_name: 'Juniper' }) === true, 'a chosen name passes');
check(!SLIDES.some((s) => s.id === 'network' || s.appId === 'network'), 'the bastion slide is gone');
check(!SLIDES.some((s) => /bastion/i.test((s.lines || []).join(' '))), 'no slide talks about a bastion');
const net = SLIDES.find((s) => s.id === 'privatae-network');
check(!!net && /optional/i.test(net.title) && /Privatae network/.test(net.title), 'the network slide is the optional Privatae network');
const words = net ? net.lines.join(' ') : '';
check(/App Store without any account/.test(words), 'it says apps install without an account');
check(/privatae\.ai\/account\/network/.test(words) && /Settings/.test(words), 'it names the code page and Settings');
check(net && net.appId === 'settings' && net.appView === 'network', 'its button opens Settings at Network');
check(net && net.check.endpoint === '/api/network/status'
    && net.check.evaluate({ member: true }) === true && net.check.evaluate({ member: false }) === false,
    'its check passes once the Host is a member');

// ── the engine ────────────────────────────────────────────────────────
const els = {};
function makeEl() {
    return { innerHTML: '', textContent: '', className: '', hidden: false, disabled: false, value: '',
        maxLength: 0, dataset: {}, style: {}, onclick: null,
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener() {}, setAttribute() {} };
}
const docListeners = {};
const doc = { getElementById: (id) => (els[id] = els[id] || makeEl()),
    addEventListener: (t, fn) => { docListeners[t] = fn; } };
const calls = []; const posted = [];
function respond(status, body) {
    return Promise.resolve({ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) });
}
const sandbox = { document: doc, console, setTimeout: (fn) => { fn(); return 0; }, clearTimeout() {},
    location: { origin: 'http://localhost', hash: '' },
    fetch: (url, opts) => {
        calls.push({ url, method: (opts && opts.method) || 'GET', body: opts && opts.body });
        if (url === '/api/system/status') return respond(200, { entity_display_name: 'C-Series' });
        if (url === '/api/network/status') return respond(200, { member: false });
        return respond(200, {});
    } };
sandbox.addEventListener = () => {};
sandbox.window = sandbox;
sandbox.parent = { postMessage: (m) => posted.push(m), document: { addEventListener() {} } };
sandbox.GS_SLIDES = SLIDES;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(APP, 'app.js'), 'utf8'), sandbox);

const tick = () => new Promise((r) => setImmediate(r));
async function settle() { for (let i = 0; i < 20; i++) await tick(); }

(async () => {
    await docListeners.DOMContentLoaded();
    await settle();
    const App = sandbox.window.App;
    await App.goTo(ids.indexOf('name'));
    await settle();
    check(els['btn-skip'].hidden === false, 'the Name slide can be skipped');
    check(els['btn-primary'].textContent === 'Open Settings', 'its button is Open Settings');
    await els['btn-primary'].onclick();
    const openName = posted.filter((m) => m.type === 'cseries:open-app').pop();
    check(openName && openName.app_id === 'settings' && openName.view === 'overview',
        'the Name slide opens Settings at About: ' + JSON.stringify(openName));
    check(!calls.some((c) => c.url === '/api/system/name'), 'the walkthrough never sends the name');

    await App.goTo(ids.indexOf('privatae-network'));
    await settle();
    await els['btn-primary'].onclick();
    const open = posted.filter((m) => m.type === 'cseries:open-app').pop();
    check(open && open.app_id === 'settings' && open.view === 'network', 'the network slide opens Settings at Network: ' + JSON.stringify(open));

    if (failures.length) {
        failures.forEach((f) => console.log('  FAIL  ' + f));
        console.log(`\nFAIL: ${failures.length} of ${checks}`);
        process.exit(1);
    }
    console.log(`  PASS  ${checks} Getting Started checks`);
})().catch((e) => { console.log('  FAIL  ' + (e && e.stack || e)); process.exit(1); });
