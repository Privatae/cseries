#!/usr/bin/env node
/*
 * Settings → Updates says what the check answered: on a standalone Host the
 * installer's version and the command to run, and a failed check's error,
 * never "You're up to date". Boots the REAL system-apps/settings/app.js in a
 * vm with a stub DOM and calls the exported renderUpdates, so the words
 * checked are the words shown.
 * Run: node scripts/test_update_check_standalone.js  (test_update_check_standalone.py runs it)
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const els = {};
function makeEl() {
    return { innerHTML: '', textContent: '', className: '', hidden: false, disabled: false, value: '',
        dataset: {}, style: {},
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener() {}, appendChild() {}, setAttribute() {}, removeAttribute() {},
        querySelectorAll() { return []; }, querySelector() { return null; }, closest() { return null; } };
}
const doc = { getElementById: (id) => (els[id] = els[id] || makeEl()), createElement: () => makeEl(),
    querySelectorAll: () => [], querySelector: () => makeEl(), addEventListener() {}, body: makeEl() };
const sandbox = { document: doc, console, localStorage: { getItem: () => null, setItem() {} },
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    fetch: () => Promise.reject(new Error('no network')), navigator: { userAgent: 'node' },
    location: { href: 'http://localhost/', origin: 'http://localhost', hash: '' } };
sandbox.addEventListener = () => {};
sandbox.window = sandbox; sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'system-apps', 'settings', 'app.js'), 'utf8'), sandbox);

const app = sandbox.window.SettingsApp;
const failures = []; let checks = 0;
function check(cond, msg) { checks++; if (!cond) failures.push(msg); }
function shown(answer) {
    app.state.version = { tag: '0.1.4' };
    app.state.available = answer;
    // A real element has one content: writing either property replaces it.
    const el0 = doc.getElementById('update-result');
    el0.textContent = ''; el0.innerHTML = '';
    app.renderUpdates();
    const el = els['update-result'];
    return { text: (el.textContent || '') + (el.innerHTML || ''), cls: el.className };
}

check(app && typeof app.renderUpdates === 'function', 'renderUpdates is exported');

let r = shown({ available: true, latest: '0.1.5', current: '0.1.4', channel: 'installer',
    message: 'Run npx cseries update on this machine' });
check(/Version 0\.1\.5 is available\. Run npx cseries update on the machine C-Series is installed on\./.test(r.text),
    'a newer installer version names it and the command: ' + r.text);
check(!/Apply update/.test(r.text), 'a standalone Host is not offered the platform Apply button');

r = shown({ available: false, latest: '0.1.4', current: '0.1.4', channel: 'installer' });
check(/You're up to date \(0\.1\.4\)/.test(r.text), 'up to date names the version: ' + r.text);

r = shown({ available: false, latest: null, current: '0.1.4', channel: 'installer',
    error: 'could not ask the npm registry for the newest release: timed out' });
check(/timed out/.test(r.text) && !/up to date/i.test(r.text), 'a failed installer check shows its error: ' + r.text);

r = shown({ error: '/api/update/check → 500 bridge not connected' });
check(/bridge not connected/.test(r.text) && !/up to date/i.test(r.text),
    'a check that failed outright shows the error, never up to date: ' + r.text);

r = shown({ available: false, tag: '0.1.3', stale: true, error: 'bridge unavailable' });
check(/bridge unavailable/.test(r.text) && !/up to date/i.test(r.text),
    'a stale cached answer with an error shows the error: ' + r.text);

r = shown({ available: false, tag: '0.1.4' });
check(/up to date/i.test(r.text), 'the platform answer without an error still says up to date');

if (failures.length) {
    failures.forEach((f) => console.log('  FAIL  ' + f));
    console.log(`\nFAIL: ${failures.length} of ${checks}`);
    process.exit(1);
}
console.log(`  PASS  ${checks} Settings update-result checks`);
