#!/usr/bin/env node
/*
 * The anchor step is visible — headless check of the Network app's instructions.
 *
 * WHY THIS EXISTS. 2026-09-12: the app never said where to get a pair token,
 * which is not coherent for a user who already paired the bastion and then
 * changes the mode to networked. Right twice over: the product never named `set-mode networked` or `claim-anchor` anywhere
 * (the word "anchor" appeared in this app only as a STATE to display), and the
 * command then demanded a second pairing token, redeemed against a machine that
 * was already paired — the first one is consumed at redeem.
 *
 * So the instructions ARE the feature here, and a check that only ran the code
 * would pass on an app that says nothing. These assertions are about text the
 * user can act on, and about which machines it appears for.
 *
 * Runs without a browser: openBastionInspector builds a field list and hands it
 * to openInspector, which writes markup. The DOM stub below caches elements by
 * id — the sibling harness (test_network_views.js) returns a FRESH element per
 * getElementById, which here would make every assertion read an empty string and
 * pass on nothing.
 *
 * Run: node scripts/test_network_anchor_step.js
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
        select() {},
    };
    return el;
}
// Cached by id — see the header.
const byId = {};
const doc = {
    getElementById: (id) => (byId[id] = byId[id] || makeEl()),
    createElement: () => makeEl(),
    querySelectorAll: () => [],
    querySelector: () => makeEl(),
    addEventListener() {},
    execCommand: () => true,
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
let finished = false;
function done(label) {
    finished = true;
    if (failures.length) {
        failures.forEach(f => console.log('  FAIL  ' + f));
        console.log(`\n${failures.length} of ${checks} failed`);
        process.exit(1);
    }
    console.log(`${label}: ${checks}/${checks} checks passed`);
}
// The card checks run inside a promise chain. If setupBastion never settles the
// chain never runs, node drains and exits 0 with nothing printed, and the Python
// wrapper reads that as green. Measured 2026-09-22 (review): a never-resolving
// await before the card opens produced no output and rc=0. So a run that ends
// without reaching done() is a failure, not a pass.
process.on('exit', (code) => {
    if (!finished && code === 0) {
        console.log('  FAIL  the gate ended without reaching done() — the card flow never settled');
        process.exitCode = 1;
    }
});

if (!App || typeof App.openBastionInspector !== 'function') {
    console.log('  FAIL  NetworkApp.openBastionInspector is missing — the gate cannot call the real builder');
    process.exit(1);
}

App.state.overview = { identity: { handle: 'acme', entity_id: 42 } };

// Render one machine and hand back the inspector markup.
function inspect(machine) {
    const body = doc.getElementById('inspector-body');  // lazily created, then cached
    body.innerHTML = '';
    App.openBastionInspector(machine);
    return body.innerHTML;
}
const base = {
    id: 7, name: 'desk', hostname: 'DESKTOP-EXAMPLE1', online: true,
    last_seen_at: '2026-09-12T20:00:00Z', capabilities: [], models: [],
    // The rows are gated on the version where claim-anchor's token became
    // optional; without this every assertion below would be testing a hidden row.
    bastion_version: '0.23.0',
};

// ── 1. a LOCAL machine is told how to become a shared one ────────────────
const local = inspect({ ...base, platform: 'linux', mode: 'local' });
check(local.includes('privatae-bastion set-mode networked'),
      'a local machine must be told the set-mode command');
check(local.includes('privatae-bastion claim-anchor'),
      'a local machine must be told the claim-anchor command');
check(/optional/i.test(local),
      'the shared-machine step must read as optional — most machines never need it');
// The defect being fixed: the user was sent looking for a token. `claim-anchor`
// is stripped first because the command name is not an instruction to find one.
check(!/token/i.test(local.replace(/claim-anchor/g, '')),
      'the instructions must not send the user looking for a token — claim-anchor takes none');

// ── 2. a NETWORKED but UNANCHORED machine is told to anchor it ───────────
const unanchored = inspect({ ...base, platform: 'linux', mode: 'networked' });
check(unanchored.includes('privatae-bastion claim-anchor'),
      'a networked machine with no anchor must be told to anchor it');
check(/not anchored/i.test(unanchored),
      'an unanchored networked machine must SAY it is unanchored — that is the risk being surfaced');
check(!unanchored.includes('set-mode networked'),
      'a machine already in networked mode must not be told to set the mode again');

// ── 3. an ANCHORED machine is told nothing — the step is done ────────────
const anchored = inspect({
    ...base, platform: 'linux', mode: 'networked',
    anchor: { owner: 'Alex', anchor_host: 'host-a.zone.example.test', anchor_handle: 'host-a', verified: true },
});
check(!anchored.includes('claim-anchor'),
      'an anchored machine must NOT be told to claim the anchor — it already is anchored');
check(anchored.includes('host-a'),
      'an anchored machine still shows who anchored it');

// ── 4. Windows is never told to sudo ─────────────────────────────────────
// A Windows user handed `sudo -H` reads a correct step as broken. Same defect
// class as os.Geteuid() returning -1 on Windows: an instruction written for one
// platform and silently wrong on the other.
const win = inspect({ ...base, platform: 'windows-amd64', mode: 'local' });
check(/Administrator/i.test(win),
      'a Windows machine must be told to use an Administrator terminal');
check(!/sudo/i.test(win),
      'a Windows machine must NEVER be told to sudo — there is no sudo on Windows');
const nix = inspect({ ...base, platform: 'linux', mode: 'local' });
check(/sudo -H/.test(nix), 'a Linux machine must be told sudo -H');
check(!/Administrator/i.test(nix), 'a Linux machine must not be told about Administrator PowerShell');
// DARWIN. `'darwin'.indexOf('win') === 3`, so the first version of adminHowTo
// told every Mac to open an Administrator PowerShell — the Windows bug above,
// inverted, and invisible because the gate only exercised linux and windows.
// Both spellings the fleet actually reports.
for (const p of ['darwin', 'darwin-arm64']) {
    const mac = inspect({ ...base, platform: p, mode: 'local' });
    check(/sudo -H/.test(mac), `a Mac (${p}) must be told sudo -H`);
    check(!/Administrator/i.test(mac) && !/PowerShell/i.test(mac),
          `a Mac (${p}) must NEVER be told to open PowerShell — 'darwin' contains 'win'`);
}

// ── 4b. the rows are gated on the bastion that can run them ──────────────
// On 0.22.0 a bare `claim-anchor` prints usage and exits 2. Offering it to the
// live fleet would hand most machines a command their own bastion rejects.
for (const v of ['0.22.0', '0.13.13', undefined]) {
    const old = inspect({ ...base, bastion_version: v, platform: 'linux', mode: 'local' });
    check(!old.includes('claim-anchor'),
          `a bastion on ${v === undefined ? 'an unreported version' : v} must not be offered claim-anchor`);
}

// ── 4c. a box serving several hosts is not promised "no token" ───────────
// claim-anchor refuses a multi-pairing box as ambiguous; the token is what
// names which host takes the anchor there.
const many = inspect({
    ...base, platform: 'linux', mode: 'networked',
    pairings: [{ host_name: 'a', state: 'active' }, { host_name: 'b', state: 'active' }],
});
check(!/No token/.test(many),
      'a box with several pairings must not be promised that no token is needed');
check(/token names which one/.test(many),
      'a multi-pairing box must be told the token picks which host takes the anchor', many.slice(0, 200));

// ── 5. a bastion too old to report a mode is not guessed at ──────────────
// network.md: "A bastion too old to say reports neither; say 'not reported',
// never assume local." Printing set-mode instructions at it would be that guess.
const unknown = inspect({ ...base, platform: 'linux' });
check(!unknown.includes('set-mode') && !unknown.includes('claim-anchor'),
      'a bastion that reports no mode must get no anchor instructions — its mode is unknown, not local');

// ── 6. the commands survive escaping ─────────────────────────────────────
// These rows are rendered raw (openInspector's 4th field flag). A future switch
// to the escaped path would turn <code> into visible &lt;code&gt; and the user
// would be reading markup, so pin that the command is plain text in the output.
check(!local.includes('&lt;code&gt;'),
      'the command rows must render as markup, not as escaped &lt;code&gt; text');

// ── 7. the SETUP CARD, driven by the real server payload ─────────────────
// Given a payload file (test_anchor_step_is_visible.py passes the live output of
// mint_pairing_token), drive the real setupBastion and assert the card shows the
// third step. That makes the two halves one gate: wording the server stops
// emitting is wording the card stops showing, and this catches it.
const payloadPath = process.argv[2];
if (payloadPath) {
    const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
    sandbox.fetch = () => Promise.resolve({
        ok: true, status: 200, json: () => Promise.resolve(payload),
        text: () => Promise.resolve(''),
    });
    const body = doc.getElementById('inspector-body');
    body.innerHTML = '';
    App.setupBastion().then(() => {
        const card = body.innerHTML;
        check(card.includes('skb_'), 'the setup card must still show the token');
        check(card.includes('privatae-bastion pair'), 'the setup card must still show step 2');
        check(card.includes('set-mode networked'),
              'the setup card must carry the share step from the server payload');
        check(card.includes('claim-anchor'),
              'the setup card must name claim-anchor');
        check(/Administrator/.test(card) && /sudo -H/.test(card),
              'the setup card must cover BOTH platforms — it cannot know which machine this is');
        // setupBastion reloads the bastion list WITHOUT awaiting it, and that reload
        // closes any open inspector unless told to keep it. The close lands on the
        // reload's continuation, after the (stubbed) fetch resolves — i.e. after this
        // .then. Measured 2026-09-22: the card opened, then vanished, on every host.
        // So settle on the REAL timer (the sandbox's setTimeout is a stub that never
        // fires; this one is the test process's own) and only then read `hidden`,
        // which is the one thing closeInspector changes — innerHTML survives a close,
        // so the checks above cannot see it.
        return new Promise((resolve) => setTimeout(resolve, 50)).then(() => {
            check(doc.getElementById('inspector').hidden === false,
                  'the setup card is still on screen after the list reload settles — ' +
                  'the single-use token must be readable');
            done('anchor step is visible (with the setup card)');
        });
    }).catch((e) => {
        check(false, 'setupBastion threw against the real payload: ' + e.message);
        done('anchor step is visible');
    });
} else {
    done('anchor step is visible');
}
