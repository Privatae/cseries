#!/usr/bin/env node
/*
 * The one-line setup card, driven headless (run by test_bastion_setup_card.py,
 * which passes a payload made by the real bastion_enroll.pairing_code and
 * one_line, and what its code must decode to).
 *
 * Holds: the first line shown after the warning is the Windows one line, as
 * text; its code decodes to the redeem URL, the CA SHA-256 and the token the
 * server minted; the Copy buttons copy the two lines exactly; an address in the
 * pairing window is shown as text, never as markup; the countdown starts at the
 * window's length; "Open the window to this address" sets up again once,
 * with pair_from and the card's own token for the server to end, and a refusal
 * (a mistyped address) leaves the first card, its line, Copy buttons and
 * countdown in place with the reason beside the field; before setup, another
 * network's address is offered only where setup opens a pairing window, and is
 * one setup request with pair_from alone; and an answer with no one line shows
 * the card as before.
 *
 * Run: node scripts/test_network_one_line_card.js <payload.json>
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// NETWORK_APP_JS checks another copy (test_bastion_setup_card.py passes its own).
const APP = process.env.NETWORK_APP_JS || path.join(__dirname, '..', 'system-apps', 'network', 'app.js');

function makeEl() {
    const el = {
        _text: '', _on: {}, children: [], value: '',
        get textContent() { return this._text; },
        set textContent(v) { this._text = String(v == null ? '' : v); },
        innerHTML: '', hidden: false, dataset: {}, style: {}, className: '',
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener(ev, fn) { (this._on[ev] = this._on[ev] || []).push(fn); },
        click() { (this._on.click || []).forEach(fn => fn({ target: this })); },
        appendChild(c) { this.children.push(c); }, removeAttribute() {},
        setAttribute() {}, querySelectorAll() { return []; }, closest() { return null; },
        select() {},
    };
    return el;
}
let byId = {};
const doc = {
    getElementById: (id) => (byId[id] = byId[id] || makeEl()),
    createElement: () => makeEl(),
    querySelectorAll: () => [],
    querySelector: () => makeEl(),
    addEventListener() {},
    execCommand: () => true,
    body: makeEl(),
};
const copied = [];
const posts = [];
let answer = null;
// What POST /bastions/setup answers instead of `answer`, when set: a refusal.
let setupRefusal = null;
let listAnswer = { bastions: [] };
const reply = (ok, status, body) => Promise.resolve({
    ok, status, json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)) });
const sandbox = {
    document: doc,
    console,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
    fetch: (url, opts) => {
        const u = String(url);
        if (u.includes('/bastions/setup')) {
            posts.push(opts && opts.body);
            return setupRefusal ? reply(false, setupRefusal.status, { detail: setupRefusal.detail })
                                : reply(true, 200, answer);
        }
        if (u.endsWith('/api/network/bastions')) return reply(true, 200, listAnswer);
        return reply(true, 200, answer);
    },
    navigator: { userAgent: 'node', clipboard: { writeText: (t) => { copied.push(t); return Promise.resolve(); } } },
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
function check(cond, msg) { checks++; console.log((cond ? '  PASS  ' : '  FAIL  ') + msg); if (!cond) failures.push(msg); }
const esc = (s) => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const unesc = (s) => String(s).replace(/&(amp|lt|gt|quot|#39);/g, (_, e) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'" }[e]));
let finished = false;
process.on('exit', (code) => {
    if (!finished && code === 0) {
        console.log('  FAIL  the gate ended without reaching the end — the card flow never settled');
        process.exitCode = 1;
    }
});

const given = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const settle = () => new Promise((resolve) => setTimeout(resolve, 30));
const actionsOf = () => doc.getElementById('inspector-actions').children;
const button = (label) => actionsOf().find(b => b.textContent === label);

async function run() {
    if (!App || typeof App.setupBastion !== 'function') {
        check(false, 'NetworkApp.setupBastion is exposed');
        return;
    }
    const one = given.payload.run.one_line;

    // 1. the one-line card
    answer = given.payload;
    await App.setupBastion();
    await settle();
    const card = doc.getElementById('inspector-body').innerHTML;
    const vals = [...card.matchAll(/<span class="net-field-val[^"]*">([\s\S]*?)<\/span><\/div>/g)].map(m => m[1]);
    // A command's words are wrapped in spans of their own (codeHTML); the text is the line.
    const textOf = (h) => unesc(String(h || '').replace(/<[^>]*>/g, ''));
    const markupOf = (h) => String(h || '').replace(/<\/?span[^>]*>/g, '');
    check(vals.length > 3 && vals[1].includes('id="pair-countdown"')
          && textOf(vals[2]) === one.windows && textOf(vals[3]) === one.unix
          && markupOf(vals[2]) === esc(one.windows) && markupOf(vals[3]) === esc(one.unix),
          'after the warning comes the pairing window, then the Windows one line, then the macOS / Linux one, as text');
    check([...card.matchAll(/<span class="net-field-val mono net-code">/g)].length >= 2
          && /<span class="net-code-long">&#39;skp_/.test(vals[2]) && /<span class="net-code-word">-Pair<\/span>/.test(vals[2]),
          'the two lines are shown as commands: a long word breaks at the box edge, -Pair is never split');
    const m = textOf(vals[2]).match(/'(skp_[A-Za-z0-9_-]+)'$/);
    let d = null;
    try { d = m && JSON.parse(Buffer.from(m[1].slice(4), 'base64url').toString('utf8')); } catch (e) { d = null; }
    check(d && JSON.stringify(Object.keys(d)) === '["v","u","ca","t"]' && d.v === 1
          && d.u === given.expect.u && d.ca === given.expect.ca && d.t === given.expect.t,
          'the shown line\'s code decodes to the redeem URL, the CA SHA-256 and the token the server minted');
    check(!card.includes(given.payload.pairing_window.address) && card.includes(esc(given.payload.pairing_window.address)),
          'the pairing window\'s address is shown as text, never as markup');
    check(doc.getElementById('pair-countdown').textContent === 'for 15:00 more',
          'the countdown starts at the window\'s 15 minutes');
    const win = button('Copy Windows line'), nix = button('Copy macOS / Linux line');
    check(!!win && !!nix && !button('Copy token'), 'the one-line card copies the two lines, not the token');
    if (win) win.click();
    if (nix) nix.click();
    check(copied.length === 2 && copied[0] === one.windows && copied[1] === one.unix,
          'Copy copies each line exactly');

    check(/The line above stops working, and this uses one more of the hour's pairing openings\./.test(unesc(card)),
          'the card says plainly that asking from it ends its line and uses one more of the hour\'s openings');

    // 2. asked from the card, refused (a mistyped address): the card stays,
    //    its line, Copy buttons and countdown with it; the reason is beside the field
    posts.length = 0;
    copied.length = 0;
    const countdownBefore = doc.getElementById('pair-countdown').textContent;
    const refusal = '192.168.1.5 is not a public address. Give the public address of the machine you are pairing.';
    setupRefusal = { status: 400, detail: refusal };
    doc.getElementById('pair-from-addr').value = '192.168.1.5';
    doc.getElementById('pair-from-go').click();
    await settle();
    setupRefusal = null;
    check(posts.length === 1 && JSON.parse(posts[0]).replaces === given.payload.token,
          'asked from the card, the request names the card\'s own token to end', posts);
    check(doc.getElementById('inspector-body').innerHTML === card && doc.getElementById('inspector').hidden === false,
          'a refused request leaves the first card on screen as it was');
    check(doc.getElementById('pair-countdown').textContent === countdownBefore,
          'a refused request leaves the countdown running where it was');
    // The same two buttons (a click relabels one "Copied ✓" until a timer this
    // sandbox never fires, so they are not found by label again).
    const still = actionsOf();
    if (win) win.click();
    if (nix) nix.click();
    check(still.length === 2 && still[0] === win && still[1] === nix
          && copied.length === 2 && copied[0] === one.windows && copied[1] === one.unix,
          'a refused request leaves the same Copy buttons, copying the first line');
    check(doc.getElementById('pair-from-error').textContent === refusal,
          'the refusal is shown beside the address field', doc.getElementById('pair-from-error').textContent);
    check(doc.getElementById('pair-from-go').disabled === false, 'the button works again after a refusal');

    // 3. asked from the card, answered: one request, pair_from and the card's token, a new card
    posts.length = 0;
    doc.getElementById('pair-from-addr').value = '  ' + given.elsewhere + ' ';
    doc.getElementById('pair-from-go').click();
    await settle();
    check(posts.length === 1 && posts[0] === JSON.stringify({ pair_from: given.elsewhere, replaces: given.payload.token }),
          'Open the window to this address sets up again once, with pair_from and the card\'s token', posts);
    posts.length = 0;
    doc.getElementById('pair-from-addr').value = '   ';
    doc.getElementById('pair-from-go').click();
    await settle();
    check(posts.length === 0, 'an empty address sets up nothing');

    // 4. before setup: offered only where setup opens a pairing window, and one request
    listAnswer = { bastions: [], pairing_window: true };
    await App.loadBastions({ keepInspector: true });
    check(doc.getElementById('gw-add-elsewhere').hidden === false,
          'beside Set up a bastion, another network is offered where setup opens a pairing window');
    listAnswer = { bastions: [], pairing_window: false };
    await App.loadBastions({ keepInspector: true });
    check(doc.getElementById('gw-add-elsewhere').hidden === true, '...and not where it does not');
    posts.length = 0;
    doc.getElementById('gw-add-elsewhere-addr').value = ' ' + given.elsewhere + '  ';
    App.setupFromElsewhere();
    await settle();
    check(posts.length === 1 && posts[0] === JSON.stringify({ pair_from: given.elsewhere }),
          'before setup, a machine on another network is one setup request with pair_from alone', posts);
    check(doc.getElementById('gw-add-elsewhere').open === false
          && doc.getElementById('gw-add-elsewhere-addr').value === ''
          && doc.getElementById('gw-add-elsewhere-form').hidden === true,
          'after that setup, the choice is closed, its address cleared and its form hidden');

    // 5. an answer with no one line: the card as before
    byId = {};
    const old = JSON.parse(JSON.stringify(given.payload));
    delete old.run.one_line;
    delete old.pairing_code;
    answer = old;
    await App.setupBastion();
    await settle();
    const oldCard = doc.getElementById('inspector-body').innerHTML;
    check(!!button('Copy token') && !!button('Copy pair command') && !button('Copy Windows line')
          && !oldCard.includes('Do it step by step') && oldCard.includes(esc(old.token)),
          'an answer with no one line shows the card as before');
}

run().catch((e) => check(false, 'setupBastion threw: ' + e.message)).then(() => {
    finished = true;
    if (failures.length) {
        console.log(`\n${failures.length} of ${checks} failed`);
        process.exit(1);
    }
    console.log(`one-line setup card: ${checks}/${checks} checks passed`);
});
