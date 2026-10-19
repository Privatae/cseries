#!/usr/bin/env node
/*
 * Oversight app — the Machines view never says "no differences" about a class
 * nobody compared.
 *
 * A pass can leave a class out (the bastion's `skipped`: a reader that failed,
 * a platform that cannot read it, an accounting that predates it). The counts
 * are then real zeros for what WAS compared and say nothing about the rest, so
 * the line names the classes left out. Run against the REAL app.js in a
 * minimal DOM sandbox, like test_network_contain_pending.js.
 *
 * Run: node scripts/test_oversight_detect_line.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'system-apps', 'oversight', 'app.js');

function makeEl() {
    return {
        textContent: '', innerHTML: '', hidden: false, dataset: {}, style: {},
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener() {}, appendChild() {}, removeAttribute() {},
        setAttribute() {}, querySelectorAll() { return []; }, closest() { return null; },
    };
}
const doc = {
    getElementById: () => makeEl(), createElement: () => makeEl(),
    querySelectorAll: () => [], querySelector: () => makeEl(),
    addEventListener() {}, body: makeEl(), hidden: false,
};
const sandbox = {
    document: doc, console,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    fetch: () => Promise.reject(new Error('no network in this test')),
    navigator: { userAgent: 'node' },
    location: { href: 'http://localhost/', origin: 'http://localhost', hash: '' },
    Date,
};
sandbox.addEventListener = () => {};
sandbox.removeEventListener = () => {};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(APP, 'utf8'), sandbox, { filename: 'app.js' });

const failures = [];
let checks = 0;
function check(cond, msg) { checks++; if (!cond) failures.push(msg); }

const App = sandbox.window.Oversight;
check(!!App && typeof App.detectLine === 'function', 'detectLine is not exported');
if (failures.length) { console.log('FAIL ' + failures.join('\n')); process.exit(1); }

const zero = { critical: 0, high: 0, warn: 0, notice: 0 };
check(App.detectLine({ detect: zero }) === 'compared, no differences',
      'nothing skipped reads exactly as before: ' + App.detectLine({ detect: zero }));
const partial = App.detectLine({ detect: Object.assign({ skipped: ['listener', 'identity'] }, zero) });
check(/listening ports and accounts not compared/.test(partial),
      'a pass that left classes out names them: ' + partial);
const withFindings = App.detectLine({ detect: Object.assign({}, zero, { high: 1, skipped: ['listener'] }) });
check(/1 high/.test(withFindings) && /listening ports not compared/.test(withFindings),
      'counts with a class left out say the counts leave it out: ' + withFindings);
const unknown = App.detectLine({ detect: Object.assign({ skipped: ['rm -rf'] }, zero) });
check(unknown === 'compared, no differences',
      'an unknown class name is dropped, never printed as undefined or an empty name: ' + unknown);
check(App.detectLine({ detect: { critical: null, high: null, warn: null, notice: null } }) === 'nothing was counted',
      'null counts still read as nothing counted');

if (failures.length) {
    console.log('FAIL — ' + failures.length + ' of ' + checks + ' checks\n  ' + failures.join('\n  '));
    process.exit(1);
}
console.log('PASSED — ' + checks + ' checks');
