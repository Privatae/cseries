#!/usr/bin/env node
/*
 * Network app — a kill order the box has not carried out yet is visible.
 *
 * A containment rides the heartbeat and is delivered only on a check-in where
 * the bastion can see its agents. These are the pure helpers the
 * app renders that state with, run against the REAL app.js in a minimal DOM
 * sandbox, so the words the user reads are pinned rather than eyeballed.
 *
 * Run: node scripts/test_network_contain_pending.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'system-apps', 'network', 'app.js');

function makeEl() {
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
    addEventListener() {},
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
    Date,
};
sandbox.listeners = {};
sandbox.addEventListener = (t, f) => { (sandbox.listeners[t] = sandbox.listeners[t] || []).push(f); };
sandbox.removeEventListener = () => {};
sandbox.MessageChannel = function () { this.port1 = {}; this.port2 = {}; };
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(
    fs.readFileSync(path.join(__dirname, '..', 'dashboard', 'base', 'js', 'sandbox-csp.js'), 'utf8'),
    sandbox, { filename: 'sandbox-csp.js' });
vm.runInContext(fs.readFileSync(APP, 'utf8'), sandbox, { filename: 'app.js' });

const App = sandbox.window.NetworkApp;
const failures = [];
let checks = 0;
function check(cond, msg) { checks++; if (!cond) failures.push(msg); }

check(!!App && typeof App.containPendingPill === 'function', 'containPendingPill is not exported');
check(!!App && typeof App.containPendingSince === 'function', 'containPendingSince is not exported');
check(!!App && typeof App.schedulerScanNote === 'function', 'schedulerScanNote is not exported');
if (failures.length) { report(); process.exit(1); }

const recent = new Date(Date.now() - 5 * 60 * 1000).toISOString();

// ── the bastion card ─────────────────────────────────────────────────────
check(App.containPendingPill({ online: true, capabilities: ['agent-observe'], contain_pending: [] }) === '',
      'a box with no open order must show no pill');
check(App.containPendingPill({ online: true, capabilities: ['agent-observe'] }) === '',
      'a row without the field (older API) must show no pill, not throw');

const blind = App.containPendingPill({
    online: true, capabilities: ['embed'],
    contain_pending: [{ pid: 4242, signature: 'claude-code', requested_at: recent }],
});
check(/containment waiting/.test(blind), 'one held order reads "containment waiting": ' + blind);
check(/pid 4242/.test(blind), 'the pill names the pid: ' + blind);
check(/not watching its agents right now/.test(blind), 'a box without agent-observe says it is not watching: ' + blind);
check(/lapses/.test(blind) && /management audit/.test(blind), 'the lapse rule is stated where the order is shown: ' + blind);

const offline = App.containPendingPill({
    online: false, capabilities: [],
    contain_pending: [{ pid: 1, requested_at: recent }, { pid: 2, requested_at: recent }],
});
check(/2 containments waiting/.test(offline), 'two orders read plural: ' + offline);
check(/has not checked in/.test(offline), 'an offline box says the order has not reached it: ' + offline);

const watching = App.containPendingPill({
    online: true, capabilities: ['agent-observe'], bastion_version: '0.31.1',
    contain_pending: [{ pid: 4242, requested_at: recent }],
});
check(/delivered on every check-in until the agent is gone/.test(watching), 'a watching 0.31.1 box says the order is being delivered: ' + watching);
check(!/not watching/.test(watching) && !/cannot vouch/.test(watching), 'a watching 0.31.1 box must not be called blind or unvouched: ' + watching);
const older = App.containPendingPill({
    online: true, capabilities: ['agent-observe'], bastion_version: '0.30.5',
    contain_pending: [{ pid: 4242, requested_at: recent }],
});
check(/still lists the agent/.test(older) && /cannot vouch for an empty list/.test(older),
      'a watching pre-0.31.0 box says delivery needs the agent listed and an empty list holds: ' + older);
check(/until it updates/.test(older), 'a pre-0.31.1 box says the hold ends with an update: ' + older);
const atMainVersion = App.containPendingPill({
    online: true, capabilities: ['agent-observe'], bastion_version: '0.31.0',
    contain_pending: [{ pid: 4242, requested_at: recent }],
});
check(/cannot vouch for an empty list/.test(atMainVersion), '0.31.0 was on main WITHOUT the marker and must read as an old build: ' + atMainVersion);
const noVersion = App.containPendingPill({
    online: true, capabilities: ['agent-observe'],
    contain_pending: [{ pid: 4242, requested_at: recent }],
});
check(/cannot vouch for an empty list/.test(noVersion), 'a box reporting no version gets the cautious reading, not the 0.31.1+ one: ' + noVersion);

// ── finding stops (v172) ────────────────────────────────────────────────
check(typeof App.findingContainPendingPill === 'function', 'findingContainPendingPill is not exported');
const FPX = '0123456789abcdef0123456789abcdef';
check(App.findingContainPendingPill({ online: true, contain_findings_pending: [] }) === '',
      'no open finding order → no pill');
check(App.findingContainPendingPill({ online: true }) === '',
      'a row without the field (older API) shows no pill, not throw');
const fNew = App.findingContainPendingPill({
    online: true, bastion_version: '0.32.0',
    contain_findings_pending: [{ fp: FPX, requested_at: recent }],
});
check(/finding stop waiting/.test(fNew), 'one order reads "finding stop waiting": ' + fNew);
check(fNew.indexOf(FPX.slice(0, 12)) !== -1, 'the pill names the fingerprint (12 hex): ' + fNew);
check(/next check-in/.test(fNew) && /still a finding/.test(fNew), 'a 0.32.0 box explains the answer comes next beat: ' + fNew);
check(/management audit/.test(fNew), 'where the answer lands is stated: ' + fNew);
const fOld = App.findingContainPendingPill({
    online: true, bastion_version: '0.31.3',
    contain_findings_pending: [{ fp: FPX, requested_at: recent }, { fp: FPX, requested_at: recent }],
});
check(/2 finding stops waiting/.test(fOld), 'two orders read plural: ' + fOld);
check(/older than 0\.32\.0/.test(fOld) && /lapses after an hour/.test(fOld),
      'a pre-0.32.0 box says it cannot stop a finding and the order lapses: ' + fOld);
const fOff = App.findingContainPendingPill({
    online: false, bastion_version: '0.32.0',
    contain_findings_pending: [{ fp: FPX, requested_at: recent }],
});
check(/has not checked in/.test(fOff), 'an offline box says the order has not reached it: ' + fOff);

// ── the agent card ───────────────────────────────────────────────────────
check(App.containPendingSince({ members: [{ pid: 1 }] }) === null, 'no order → null');
check(App.containPendingSince({ members: [] }) === null, 'no members → null');
check(App.containPendingSince({ members: [{ pid: 1, contain_pending_since: '2026-09-19T22:05:00+00:00' },
                                          { pid: 2, contain_pending_since: '2026-09-19T22:01:00+00:00' }] })
      === '2026-09-19T22:01:00+00:00', 'the oldest open order across the family is the one shown');
check(App.containPendingSince({ members: [{ pid: 1, contain_pending_since: '2026-09-19T22:05:00+00:00' }, { pid: 2 }] })
      === null, 'a family half-ordered still needs its Contain button (null)');

// ── the scheduled-jobs note ──────────────────────────────────────────────
const box = (extra) => ({ hostname: 'box', online: true, capabilities: [], bastion_version: '0.30.5', schedulers: [], ...extra });
check(App.schedulerScanNote(box({ scheduler_scan: { supported: true, entries: 2, kinds: ['cron'] } }))
      === 'box: 2 entries across cron', 'a report reads as the report');
check(App.schedulerScanNote(box({ capabilities: ['scheduler-observe'] })) === null,
      'a watching box with no report yet gets no note');
check(App.schedulerScanNote(box({ online: false })) === null, 'an offline box gets no note');
const old = App.schedulerScanNote(box({ bastion_version: '0.13.2' }));
check(/update the bastion to 0\.13\.12/.test(old), 'a build before 0.13.12 is told to update: ' + old);
for (const v of [null, undefined, '']) {
    const noVer = App.schedulerScanNote(box({ bastion_version: v }));
    check(/update the bastion to 0\.13\.12/.test(noVer), 'a box reporting no version (' + JSON.stringify(v) + ') is told to update, not called merely not-watching: ' + noVer);
}
const notNow = App.schedulerScanNote(box({}));
check(/not read now/.test(notNow) && /not watching/.test(notNow), 'a newer build that is not watching is told so, not told to update: ' + notNow);
check(!/update the bastion/.test(notNow), 'a newer build must not be told to update: ' + notNow);
const kept = App.schedulerScanNote(box({ schedulers: [{ kind: 'cron', name: 'x' }] }));
check(/showing its last inventory/.test(kept), 'a kept inventory is called the last one: ' + kept);

function report() {
    if (failures.length) {
        console.log('FAIL  network contain-pending surface');
        failures.forEach(f => console.log('  - ' + f));
    } else {
        console.log('ok    network contain-pending surface (' + checks + ' checks)');
    }
}
report();
process.exit(failures.length ? 1 : 0);
