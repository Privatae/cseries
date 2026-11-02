#!/usr/bin/env node
/*
 * Network app, Agents view: what the bastion keeps about each agent and each
 * machine (v256) reaches the person, in plain words, and as text.
 *
 * Per agent the account behind it (what kind of account, administrator or not,
 * whether anyone is signed in to it), the file it runs and its unit; per
 * machine the tool servers its agent clients declare and how much the bastion
 * read. Every one of those strings was written by a program on the person's
 * machine, so each check that draws HTML also feeds a value shaped like markup
 * and asserts it arrives escaped.
 *
 * Run against the REAL app.js in a minimal DOM sandbox, like
 * scripts/test_network_contain_pending.js.
 *
 * Run: node scripts/test_network_agent_facts.js
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
// One element per id, so what a render wrote can be read back.
const els = {};
const doc = {
    getElementById: (id) => (els[id] = els[id] || makeEl()),
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
function report() {
    if (failures.length) {
        console.error(`FAIL ${failures.length}/${checks}`);
        failures.forEach(f => console.error('  - ' + f));
    } else {
        console.log(`ok ${checks}/${checks} — the Agents view shows the account, the file and the tool servers, as text`);
    }
}

for (const fn of ['accountWords', 'adminPill', 'toolServerProblems', 'toolServerLine', 'toolServerScanNote',
                  'agentFamilies', 'renderToolServers', 'openFamilyInspector']) {
    check(!!App && typeof App[fn] === 'function', fn + ' is not exported');
}
if (failures.length) { report(); process.exit(1); }

const MARKUP = '"><img src=x onerror=alert(1)>';

// ── the account behind an agent ──────────────────────────────────────────
check(App.accountWords({}) === null, 'a row from a bastion too old to say has no account words');
check(App.accountWords(null) === null, 'no member, no account words');
const admin = App.accountWords({ owner_class: 'local_user', owner_admin: 'yes', owner_interactive: 'no', agent_user: 'invented' });
check(/a person's account on this machine \(invented\)/.test(admin), 'a local user is named as a person\'s account, with its name: ' + admin);
check(/administrator/.test(admin) && /nobody is signed in/.test(admin), 'administrator and nobody signed in are both said: ' + admin);
const svc = App.accountWords({ owner_class: 'service', owner_admin: 'no', owner_interactive: 'unknown' });
check(/exists for a program, not a person/.test(svc), 'a service account is not called a person: ' + svc);
check(/is not an administrator/.test(svc), 'admin "no" is said as no: ' + svc);
check(/whether anyone is signed in to it could not be read/.test(svc), 'interactive "unknown" is never folded into no: ' + svc);
const unk = App.accountWords({ owner_class: 'unknown', owner_admin: 'unknown', owner_interactive: 'yes' });
check(/could not identify/.test(unk) && /whether it is an administrator could not be read/.test(unk),
      'unknown class and admin read as unknown: ' + unk);
check(/someone is signed in to it now/.test(unk), 'interactive "yes" is said: ' + unk);
check(/directory/.test(App.accountWords({ owner_class: 'domain_user' })), 'a directory account is named as one');
check(/root or SYSTEM/.test(App.accountWords({ owner_class: 'system' })), 'the system account is named as one');

check(App.adminPill({ owner_admin: 'no' }) === '', 'no pill for an agent that is not an administrator');
check(App.adminPill({ owner_admin: 'unknown' }) === '', 'no pill when it could not be read');
check(App.adminPill(null) === '', 'no pill without facts');
const pill = App.adminPill({ owner_class: 'local_user', owner_admin: 'yes', owner_interactive: 'yes', agent_user: MARKUP });
check(/>administrator</.test(pill), 'an administrator agent carries the pill: ' + pill);
check(!pill.includes('<img') && !pill.includes('"><'), 'an account name shaped like markup is escaped in the pill title: ' + pill);

// ── the family card and the inspector ────────────────────────────────────
App.state.agents = [
    { token_id: 7, hostname: 'box-a', pid: 100, ppid: 1, signature: 'claude-code', kind: 'agent', family: 'f1',
      agent_user: 'invented', active: true, requested: false, acknowledged: false,
      exe: '/opt/invented/bin/' + MARKUP, unit: 'invented-agent.service',
      owner_class: 'local_user', owner_admin: 'yes', owner_interactive: 'no', last_seen: new Date().toISOString() },
    { token_id: 7, hostname: 'box-a', pid: 101, ppid: 100, signature: 'claude-code', kind: 'agent', family: 'f1',
      agent_user: 'invented', active: true, requested: false, acknowledged: false, last_seen: new Date().toISOString() },
    { token_id: 7, hostname: 'box-a', pid: 300, ppid: 1, signature: 'old-agent', kind: 'agent', family: 'f2',
      active: true, requested: false, acknowledged: false, last_seen: new Date().toISOString() },
];
const fams = App.agentFamilies();
const f1 = fams.find(f => f.key === 'f1');
const f2 = fams.find(f => f.key === 'f2');
check(f1 && f1.owner && f1.owner.pid === 100, 'the family takes the root\'s account facts');
check(f1 && f1.exe === '/opt/invented/bin/' + MARKUP && f1.unit === 'invented-agent.service', 'the family takes the root\'s file and unit');
check(f2 && f2.owner === null && f2.exe === null && f2.unit === null, 'a family with nothing reported carries nulls');

App.openFamilyInspector(f1);
const body = els['inspector-body'].innerHTML;
check(/account/.test(body) && /administrator/.test(body), 'the inspector has the account line: ' + body.slice(0, 300));
check(/program file/.test(body) && /\/opt\/invented\/bin\//.test(body), 'the inspector names the program file');
check(/service unit/.test(body) && /invented-agent\.service/.test(body), 'the inspector names the unit');
check(!body.includes('<img'), 'an agent-written file path is drawn as text, never markup');
App.openFamilyInspector(f2);
const body2 = els['inspector-body'].innerHTML;
check(/not reported \(the bastion is too old to say\)/.test(body2), 'an agent with no account facts says not reported');
check(!/service unit/.test(body2), 'no unit line when none was reported');

// ── tool servers ─────────────────────────────────────────────────────────
const secret = { client: 'claude-code', path: '/home/invented/.claude.json', name: 'files', locality: 'local_process',
                 package_name: '@invented/files-server', package_pinned: 'no', secret_in_plaintext: true,
                 running_state: 'declared_running' };
const p = App.toolServerProblems(secret);
check(p.length === 2 && /plain text in \/home\/invented\/\.claude\.json/.test(p[0]), 'a plain-text key is a problem, naming the file: ' + p);
check(/no fixed version/.test(p[1]), 'an unpinned package is a problem: ' + p);
check(App.toolServerProblems({ ...secret, disabled: true }).length === 0, 'a turned-off server has no problems');
check(App.toolServerProblems({ running_state: 'running_not_declared' }).length === 1, 'a server no file names is a problem');
check(App.toolServerProblems({ package_pinned: 'yes', package_name: 'x', running_state: 'declared_not_running' }).length === 0,
      'a pinned, declared server has none');
check(/started on this machine/.test(App.toolServerLine(secret)) && /running now/.test(App.toolServerLine(secret)),
      'the line says where it runs and whether it runs: ' + App.toolServerLine(secret));
check(/over the internet at tools\.invented\.example/.test(App.toolServerLine({ locality: 'remote', url_host: 'tools.invented.example', running_state: 'not_local' })),
      'a remote server names its host');

const scanned = App.toolServerScanNote({ hostname: 'box-a', online: true, tool_servers: [],
    tool_server_scan: { supported: true, files_read: 3, files_unreadable: 1, files_unparsed: 0, files_foreign_owner: 2,
                        timed_out: true, process_scan: 'failed' } });
check(/box-a: read 3 settings files/.test(scanned) && /1 could not be opened/.test(scanned), 'the scan says how much was read: ' + scanned);
check(/another account owns them/.test(scanned) && /too long/.test(scanned) && /could not be checked/.test(scanned),
      'skipped files, a timeout and a failed process scan are each said: ' + scanned);
check(/cannot read/.test(App.toolServerScanNote({ hostname: 'b', online: true, tool_server_scan: { supported: false } })),
      'a platform the bastion cannot read is said');
check(/not reported/.test(App.toolServerScanNote({ hostname: 'b', online: true, tool_servers: null, tool_server_scan: null })),
      'an online box that never sent the list says not reported');
check(/showing its last list/.test(App.toolServerScanNote({ hostname: 'b', online: true, tool_servers: [secret], tool_server_scan: null })),
      'a kept list from an earlier beat is dated as the last list');
check(App.toolServerScanNote({ hostname: 'b', online: false, tool_servers: null, tool_server_scan: null }) === null,
      'an offline box that never reported adds no note');

App.state.bastions = [
    { hostname: 'box-a', online: true, tool_server_scan: { supported: true, files_read: 1, process_scan: 'ok' },
      tool_servers: [
          { client: 'cursor', name: 'calm', package_pinned: 'yes', package_name: 'calm', running_state: 'declared_running' },
          { ...secret, name: '<script>alert(1)</script>', path: '/home/invented/' + MARKUP },
      ] },
];
App.renderToolServers();
const tl = els['toolservers-list'].innerHTML;
check(tl.includes('&lt;script&gt;') && !tl.includes('<script>'), 'a server name shaped like markup is escaped');
check(!tl.includes('<img'), 'a settings path shaped like markup is escaped');
check(tl.indexOf('&lt;script&gt;') < tl.indexOf('calm'), 'the server with problems comes first on its machine');
check(/2 problems/.test(tl), 'the problem count is on the card');
check(/box-a: read 1 settings file/.test(tl), 'the scan note is above the list');
App.state.bastions = [{ hostname: 'box-a', online: true, tool_servers: [], tool_server_scan: { supported: true, files_read: 4, process_scan: 'ok' } }];
App.renderToolServers();
check(/No tool servers found/.test(els['toolservers-list'].innerHTML) && /read 4 settings files/.test(els['toolservers-list'].innerHTML),
      'an empty list after a real read says so, with what was read');

report();
process.exit(failures.length ? 1 : 0);
