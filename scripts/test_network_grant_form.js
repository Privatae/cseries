#!/usr/bin/env node
/*
 * Network app, NAC, Management: "Give your Host a job on a machine". A Host with
 * no model has no agent to draft a management grant, so the person drafts one
 * here from a job (GET /api/network/nac/mgmt/purposes) and approves it in the
 * list below with their code.
 *
 * Holds, against the REAL app.js in a minimal DOM sandbox:
 *   - the form offers each machine that checked in, "every machine", and each
 *     job in its own words, every one escaped (a job's words are the Host's,
 *     but a machine's name is the machine's);
 *   - no machine, no form;
 *   - Draft with no job ticked sends nothing and says why;
 *   - Draft posts {server, purposes} to the propose route, opens the Management
 *     lane and reloads it, so the draft shows at once;
 *   - a refusal from the route is shown beside the button, word for word.
 *
 * Run: node scripts/test_network_grant_form.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'system-apps', 'network', 'app.js');

function makeEl() {
    return {
        innerHTML: '', textContent: '', value: '', hidden: false, dataset: {}, style: {},
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        listeners: {},
        addEventListener(t, f) { this.listeners[t] = f; }, appendChild() {}, removeAttribute() {},
        setAttribute() {}, getAttribute() { return null; }, querySelector() { return makeEl(); },
        querySelectorAll() { return []; }, closest() { return null; },
    };
}
const els = {};
let checked = [];
const doc = {
    getElementById: (id) => (els[id] = els[id] || makeEl()),
    createElement: () => makeEl(),
    querySelectorAll: (sel) => (/input\[name=purpose\]:checked/.test(sel) ? checked.map(v => ({ value: v })) : []),
    querySelector: () => makeEl(),
    addEventListener() {},
    body: makeEl(),
};
const calls = [];
let proposeAnswer = { status: 200, body: { id: 1, state: 'draft' } };
function respond(status, body) {
    return Promise.resolve({ ok: status < 400, status, json: async () => body, text: async () => JSON.stringify(body) });
}
const sandbox = {
    document: doc, console,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    fetch: (url, opts) => {
        calls.push({ url, opts });
        if (url.endsWith('/nac/mgmt/purposes')) {
            return respond(200, { purposes: [
                { name: 'contain', job: 'stop an AI agent or unrecognised program the machine detected', asks: 'every stop', ops: ['contain'] },
                { name: 'observe', job: 'watch the <b>health</b>', asks: '', ops: ['uptime'] },
            ] });
        }
        if (url.endsWith('/nac/mgmt/grant/propose')) return respond(proposeAnswer.status, proposeAnswer.body);
        return Promise.reject(new Error('no network in this test'));
    },
    navigator: { userAgent: 'node' },
    location: { href: 'http://localhost/', origin: 'http://localhost' },
    Date,
};
sandbox.addEventListener = () => {};
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

(async () => {
    check(!!App && typeof App.renderGrantForm === 'function' && typeof App.draftGrant === 'function',
          'renderGrantForm and draftGrant are exported');
    if (failures.length) return;
    const box = () => els['nac-grant-new'];

    App.state.nac.endpoints = [];
    await App.renderGrantForm();
    check(box().innerHTML === '', 'no machine checked in: no form');

    App.state.nac.endpoints = [{ hostname: 'box-b' }, { hostname: 'box-a' }, { hostname: 'box-a' },
                               { hostname: '"><img src=x>' }];
    await App.renderGrantForm();
    const html = box().innerHTML;
    check(/value="box-a"/.test(html) && /value="box-b"/.test(html), 'each machine is offered');
    check((html.match(/value="box-a"/g) || []).length === 1, 'a machine is offered once');
    check(/value="\*">every machine</.test(html), '"every machine" is offered');
    check(!html.includes('"><img') && html.includes('>&quot;&gt;&lt;img src=x&gt;</option>'),
          'a machine name shaped like markup cannot leave its attribute and shows as text');
    check(html.includes('stop an AI agent or unrecognised program the machine detected'), 'a job is offered in its own words');
    check(html.includes('Still asks you: every stop'), 'what still asks is said');
    check(!html.includes('<b>health</b>'), "a job's words are escaped too");
    check(calls.filter(c => c.url.endsWith('/purposes')).length === 1, 'the jobs are read once');

    doc.getElementById('nac-grant-server').value = 'box-a';
    checked = [];
    await App.draftGrant();
    check(/Pick at least one job/.test(doc.getElementById('nac-grant-status').textContent), 'no job ticked: says why');
    check(!calls.some(c => c.url.endsWith('/propose')), 'no job ticked: nothing is sent');

    checked = ['contain'];
    App.state.nac.laneOpen = {};
    await App.draftGrant();
    const sent = calls.find(c => c.url.endsWith('/propose'));
    check(sent && sent.opts.method === 'POST', 'Draft posts to the propose route');
    check(sent && JSON.stringify(JSON.parse(sent.opts.body)) === JSON.stringify({ server: 'box-a', purposes: ['contain'] }),
          'it sends the machine and the jobs, nothing else: ' + (sent && sent.opts.body));
    check(App.state.nac.laneOpen.mgmt === true, 'the Management lane is opened so the draft shows');
    check(!/Cannot|undefined/.test(doc.getElementById('nac-grant-status').textContent), 'no error after a draft: ' + doc.getElementById('nac-grant-status').textContent);
    check(calls.some(c => c.url.endsWith('/nac/mgmt/grants')), 'the lane is reloaded after a draft');

    proposeAnswer = { status: 400, body: { detail: "an allow must name a machine that has checked in" } };
    await App.draftGrant();
    check(doc.getElementById('nac-grant-status').textContent === 'an allow must name a machine that has checked in',
          'a refusal is shown word for word: ' + doc.getElementById('nac-grant-status').textContent);
})().then(() => {
    if (failures.length) {
        console.error(`FAIL ${failures.length}/${checks}`);
        failures.forEach(f => console.error('  - ' + f));
        process.exit(1);
    }
    console.log(`ok ${checks}/${checks} — the person drafts a grant from a job, and sees it at once`);
});
