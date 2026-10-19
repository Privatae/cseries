#!/usr/bin/env node
/*
 * The Network app's "Automatic updates" sentence — one per ring and stage.
 * Boots the REAL app.js in a vm with a stub DOM and calls the exported
 * autoUpdateLine, so the words checked are the words shown.
 * Run: node scripts/test_network_autoupdate_line.js  (test_bastion_autoupdate.py runs it)
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function makeEl() {
    return { innerHTML: '', textContent: '', hidden: false, dataset: {}, style: {},
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener() {}, appendChild() {}, setAttribute() {}, removeAttribute() {},
        querySelectorAll() { return []; }, querySelector() { return null; }, closest() { return null; } };
}
const doc = { getElementById: () => makeEl(), createElement: () => makeEl(), querySelectorAll: () => [],
    querySelector: () => makeEl(), addEventListener() {}, body: makeEl() };
const sandbox = { document: doc, console, localStorage: { getItem: () => null, setItem() {} },
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    fetch: () => Promise.reject(new Error('no network')), navigator: { userAgent: 'node' },
    location: { href: 'http://localhost/', origin: 'http://localhost', hash: '' } };
sandbox.addEventListener = () => {};
sandbox.MessageChannel = function () { this.port1 = {}; this.port2 = {}; };
sandbox.window = sandbox; sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'dashboard', 'base', 'js', 'sandbox-csp.js'), 'utf8'), sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'system-apps', 'network', 'app.js'), 'utf8'), sandbox);

const line = sandbox.window.NetworkApp.autoUpdateLine;
const failures = []; let checks = 0;
function check(cond, msg) { checks++; if (!cond) failures.push(msg); }
const on = (ring, stage, extra) => ({ enabled: true, soak_hours: 24, silent_minutes: 15,
    latest: Object.assign({ version: '0.33.0', ring, stage }, extra || {}) });

check(typeof line === 'function', 'autoUpdateLine is exported');
check(/Cannot tell/.test(line(null)), 'a failed read says it cannot tell');
check(/^Off\./.test(line({ enabled: false })), 'off says off');
check(/^On\./.test(line({ enabled: true, latest: null })), 'on with nothing seen yet says on');
check(/canary for 0\.33\.0/.test(line(on(0, 'canary'))) && /24 hours/.test(line(on(0, 'canary'))),
    'ring 0 soaking names the version and the soak');
check(/passed the canary/.test(line(on(0, 'general', { reason: '21 on it' }))) && /21 on it/.test(line(on(0, 'general', { reason: '21 on it' }))),
    'ring 0 passed carries the reason');
check(/held back from customers: box-9 went silent/.test(line(on(0, 'held', { reason: 'box-9 went silent' }))),
    'ring 0 held carries the reason');
check(/running on the platform's own machines first/.test(line(on(1, 'canary'))), 'ring 1 during the canary waits');
check(/take it first/.test(line(on(1, 'awaiting_canary'))) && !/is running/.test(line(on(1, 'awaiting_canary'))),
    'ring 1 before the canary opens is not told it is running there');
check(/0\.33\.0 is released/.test(line(on(1, 'general'))) && !/moving/.test(line(on(1, 'general'))),
    'ring 1 on general says released, true once every machine has it too');
check(/held back after it failed/.test(line(on(1, 'held'))), 'ring 1 held says it stays put');
check(!/canary/i.test(line(on(1, 'general'))), 'ring 1 is never told it is the canary');

if (failures.length) { failures.forEach(f => console.log('  FAIL  ' + f)); console.log(`\n${failures.length} of ${checks} failed`); process.exit(1); }
console.log(`network auto-update line: ${checks}/${checks} checks passed`);
