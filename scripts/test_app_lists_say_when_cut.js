#!/usr/bin/env node
/*
 * The Memory app's Slots lens and the Compute app's list of model calls say
 * when their cap cut the list, and render every server string escaped.
 *
 * Compute titles its list "Every model call, newest first" and asked for 50:
 * a day with more calls showed the newest 50 as if they were all of them.
 * The Slots lens read at most 200 slots and 200 entries with no topic the
 * same way. Each now asks for one more than it lists, lists the cap, and
 * says the rest exist. Memory text, topics, scopes, model names and errors
 * are the Host's or a provider's words, so each must reach the page as text.
 *
 * Run against the REAL app.js files in a minimal DOM sandbox, like
 * test_oversight_detect_line.js. Invented values only.
 *
 * Run: node scripts/test_app_lists_say_when_cut.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// Start-up work the apps do on load (their first fetches) is not under test.
process.on('unhandledRejection', () => {});

const failures = [];
let checks = 0;
function check(cond, msg) { checks++; if (!cond) failures.push(msg); }

const XSS = '<img src=x onerror="alert(1)">';

function sandboxFor(appFile, respond) {
    const els = {};
    function makeEl() {
        return {
            textContent: '', innerHTML: '', hidden: false, dataset: {}, style: {}, value: '',
            classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
            addEventListener() {}, removeEventListener() {}, appendChild() {}, remove() {},
            removeAttribute() {}, setAttribute() {}, getAttribute() { return null; },
            querySelector() { return null; }, querySelectorAll() { return []; },
            closest() { return null; }, focus() {}, scrollIntoView() {},
        };
    }
    const el = (key) => els[key] || (els[key] = makeEl());
    const doc = {
        getElementById: (id) => el('#' + id), createElement: () => makeEl(),
        querySelector: (s) => el(s), querySelectorAll: () => [],
        addEventListener() {}, body: makeEl(), documentElement: makeEl(),
        hidden: false, visibilityState: 'visible',
    };
    const urls = [];
    const sandbox = {
        document: doc, console: { log() {}, warn() {}, error() {}, info() {}, debug() {} },
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        setInterval: () => 0, clearInterval() {}, setTimeout: (f) => 0, clearTimeout() {},
        requestAnimationFrame: () => 0,
        matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
        fetch: (url) => {
            urls.push(String(url));
            const body = respond(String(url));
            if (body === undefined) return new Promise(() => {});
            return Promise.resolve({ ok: true, status: 200, json: async () => body });
        },
        navigator: { userAgent: 'node' },
        location: { href: 'http://localhost/', origin: 'http://localhost', hash: '', search: '' },
        history: { replaceState() {}, pushState() {} },
        URL, URLSearchParams, Date,
    };
    sandbox.addEventListener = () => {};
    sandbox.removeEventListener = () => {};
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(appFile, 'utf8'), sandbox, { filename: path.basename(appFile) });
    return { sandbox, el, urls };
}

const settle = () => new Promise((r) => setImmediate(r));
const count = (html, re) => (html.match(re) || []).length;

async function compute() {
    const call = (i) => ({
        at: '2026-10-19T08:00:00Z', group: 'Turn', detail: i === 0 ? XSS : "Your Host's own turn",
        model: i === 0 ? XSS : 'invented-model', tokens_in: 1200, tokens_out: 300,
        cost_usd: i % 2 ? 0.0123 : null, cost_basis: i % 2 ? 'list' : XSS,
        ok: i !== 0, error: i === 0 ? XSS : null,
    });
    let calls = 51;
    const { sandbox, el, urls } = sandboxFor(
        path.join(__dirname, '..', 'system-apps', 'compute', 'app.js'),
        (url) => url.includes('/api/inference/activity')
            ? { activity: Array.from({ length: calls }, (_, i) => call(i)) }
            : url.includes('/api/compute/inference') ? {} : undefined);
    await vm.runInContext('renderInference()', sandbox);
    await settle();
    const html = el('#view-root').innerHTML;
    const asked = urls.filter((u) => u.includes('/api/inference/activity'));
    check(asked.length === 1 && /[?&]limit=51(&|$)/.test(asked[0]),
          'Compute asks for one more call than it lists: ' + asked.join(' '));
    const listed = count(html, /<tr(?: class="ca-row-fail")?><td>/g);
    check(listed === 50, 'Compute lists 50 calls when 51 came back: ' + listed);
    check(/More than 50 model calls in the last day: the newest 50 are listed\./.test(html),
          'Compute says the list was cut');
    check(!html.includes('<img'), 'Compute renders no server string as markup');
    check(html.includes('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;'),
          'Compute shows the server strings as text');
    calls = 50;
    const full = vm.runInContext(
        'recentCallsHTML({activity: Array.from({length: 50}, () => ({at: null, group: "Turn", detail: "x", ok: true}))})',
        sandbox);
    check(!/More than/.test(full) && count(full, /<tr><td>/g) === 50,
          'Compute with exactly 50 calls lists them all and says nothing was cut');
}

async function oracle() {
    const slot = (i) => ({
        type: 'preference', scope: i === 0 ? XSS : 'global', topic: i === 0 ? XSS : `topic_${i}`,
        rule: i === 1,
        live: { id: 1000 + i, content: i === 0 ? XSS : `invented entry ${i}`, turn_type: i === 0 ? XSS : null,
                created_at: '2026-10-19T08:00:00Z' },
        chain: i === 0 ? [{ id: 5, content: XSS, replaced_at: '2026-10-18T08:00:00Z', why: XSS }] : [],
    });
    let nSlots = 201, nLoose = 3;
    const { sandbox, el, urls } = sandboxFor(
        path.join(__dirname, '..', 'system-apps', 'oracle', 'app.js'),
        (url) => url.includes('/api/brain/memory-slots')
            ? { slots: Array.from({ length: nSlots }, (_, i) => slot(i)),
                no_topic: Array.from({ length: nLoose }, (_, i) => ({
                    id: 3000 + i, content: i === 0 ? XSS : `loose ${i}`, type: i === 0 ? XSS : 'preference',
                    rule: false })) }
            : undefined);
    await vm.runInContext('loadSlots()', sandbox);
    await settle();
    let html = el('#lens-body').innerHTML;
    const asked = urls.filter((u) => u.includes('/api/brain/memory-slots'));
    check(asked.length === 1 && /[?&]limit=201(&|$)/.test(asked[0]),
          'Slots asks for one more than it lists: ' + asked.join(' '));
    check(count(html, /<div class="slot">/g) === 200, 'Slots lists 200 slots when 201 came back: '
          + count(html, /<div class="slot">/g));
    check(/More than 200 slots, by type, place and topic: the first 200 are listed\./.test(html),
          'Slots says the slot list was cut');
    check(!/More than 200 entries with no topic/.test(html),
          'Slots says nothing about the no-topic list when it was not cut');
    check(!html.includes('<img'), 'Slots renders no memory text, topic, scope or reason as markup');
    check(count(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/g) >= 6,
          'Slots shows the memory text, topic, scope, turn type, chain and no-topic entry as text: '
          + count(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/g));
    nSlots = 2; nLoose = 201;
    await vm.runInContext('loadSlots()', sandbox);
    await settle();
    html = el('#lens-body').innerHTML;
    check(/More than 200 entries with no topic yet, newest first: the first 200 are listed\./.test(html)
          && !/More than 200 slots/.test(html),
          'Slots says the no-topic list was cut, and only that one');
}

(async () => {
    try { await compute(); } catch (e) { failures.push('compute run failed: ' + (e && e.stack || e)); }
    try { await oracle(); } catch (e) { failures.push('oracle run failed: ' + (e && e.stack || e)); }
    if (failures.length) {
        console.log('FAIL — ' + failures.length + ' of ' + checks + ' checks\n  ' + failures.join('\n  '));
        process.exit(1);
    }
    console.log('PASSED — ' + checks + ' checks');
    process.exit(0);
})();
