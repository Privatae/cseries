// Behaviour of the Orchestrator picker's Claude sign-in, local-model line and
// local-models re-read, with a stand-in DOM and fetch (no browser, no
// network). Driven by scripts/test_picker_claude_local.py.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = process.env.APP_ROOT || path.resolve(__dirname, '..');
const SRC = fs.readFileSync(path.join(ROOT, 'dashboard/base/js/model-picker.js'), 'utf8');

function fakeEl(tag) {
    const el = {
        tagName: tag || 'div', children: [], listeners: {}, attrs: {}, style: {}, dataset: {},
        _html: '', isConnected: true, hidden: false, value: '', textContent: '', disabled: false,
        classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
        set innerHTML(v) { this._html = String(v); }, get innerHTML() { return this._html; },
        setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
        addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
        appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
        remove() { this.isConnected = false; },
        querySelector(sel) { return sub(this, sel); },
        querySelectorAll() { return []; },
        focus() {},
    };
    return el;
}
// One stand-in child per selector, so a test can find what a handler wrote.
function sub(el, sel) {
    el._subs = el._subs || {};
    if (!el._subs[sel]) el._subs[sel] = fakeEl('sub');
    return el._subs[sel];
}

const calls = [];
let routes = {};
function respond(url, init) {
    calls.push({ url, method: (init && init.method) || 'GET', body: init && init.body });
    const h = routes[url.split('?')[0] + ' ' + ((init && init.method) || 'GET')] || routes[url.split('?')[0]];
    const body = typeof h === 'function' ? h(init) : (h === undefined ? {} : h);
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
}

const body = fakeEl('body');
const timers = [];
const sandbox = {
    console, JSON, Promise, Object, Array, Set, Map, String, Number, Math, Date, RegExp, Error,
    encodeURIComponent, decodeURIComponent,
    setTimeout: (f, ms) => {
        if (sandbox.__immediate) { setImmediate(f); return 0; }
        timers.push({ f, ms }); return timers.length;
    },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].f = null; },
    setInterval: () => 0, clearInterval: () => {},
    fetch: respond,
    document: { hidden: false, body, createElement: (t) => fakeEl(t),
                querySelector: (s) => (s === '.mp-claude-scrim'
                    ? body.children.find((c) => c.className === 'mp-claude-scrim' && c.isConnected) || null
                    : null),
                addEventListener() {} },
    localStorage: { getItem() { return null; }, setItem() {} },
};
sandbox.window = sandbox;
sandbox.window.parent = { postMessage(m) { calls.push({ postMessage: m }); } };
vm.createContext(sandbox);
vm.runInContext(SRC, sandbox);

const out = {};
const flush = () => new Promise((r) => setImmediate(r));
async function settle(n) { for (let i = 0; i < (n || 20); i++) await flush(); }

(async () => {
    routes = {
        '/api/inference/routing': { routing: {}, effective: { chat: { provider: 'local', model: 'qwen-invented' } },
                                    providers: [{ id: 'anthropic-subscription', has_key: false, signin: 'claude' },
                                                { id: 'local', has_key: true }] },
        '/api/inference/local-models': { local_models: [] },
    };
    const root = fakeEl('root');
    const p = sandbox.window.ModelPicker.mount(root, { simple: true, showRoutingTab: true,
        recommendations: { providers: { 'anthropic-subscription': { label: 'Your Claude subscription', roles: { chat: { candidates: ['claude-opus-5-5'] } } } } } });
    await settle();
    // (a) the Simple Thinking setup with chat on a local model
    p.mode = 'simple';
    const holder = fakeEl('simple');
    root.querySelector = (sel) => (sel === '[data-mp-view-simple]' ? holder : sub(root, sel));
    try { p._renderSimple(); } catch (e) { out.renderError = String(e && e.stack || e); }
    out.simpleHtml = holder.innerHTML;

    // (b) the local-models re-read: the first poll is scheduled soon while empty
    const firstPoll = timers.filter((t) => t.f).map((t) => t.ms);
    out.pollDelays = firstPoll;
    routes['/api/inference/local-models'] = { local_models: [{ id: 'qwen-invented', bastion: 'ws-invented', endpoint: 'http://x' }] };
    const before = calls.length;
    const t = timers.filter((x) => x.f).pop();
    const f = t.f; t.f = null; await f(); await settle();
    out.reloadedAfterChange = calls.slice(before).filter((c) => c.url === '/api/inference/routing').length;
    out.localAfter = (p.state.localModels || []).length;
    out.nextDelay = timers.filter((x) => x.f).map((x) => x.ms).pop();
    // A reload that throws does not end the watch: the next tick is scheduled.
    routes['/api/inference/local-models'] = { local_models: [] };
    p.reload = () => Promise.reject(new Error('invented render failure'));
    const t2 = timers.filter((x) => x.f).pop();
    const f2 = t2.f; t2.f = null;
    try { await f2(); } catch (e) { out.tickThrew = true; }
    await settle();
    out.rescheduledAfterThrow = timers.filter((x) => x.f).length;
    p.destroy();
    out.timersAfterDestroy = timers.filter((x) => x.f).length;

    // (c) the Claude sign-in pop-up: starts on the Host, posts the code
    routes['/api/inference/claude-signin POST'] = { state: 'waiting', ok: true, url: 'https://claude.ai/oauth/authorize?invented=1', needs_code: true };
    routes['/api/inference/claude-signin/code POST'] = { state: 'signed_in', ok: true };
    const p2 = sandbox.window.ModelPicker.mount(fakeEl('root2'), { simple: true });
    await settle();
    const start = calls.length;
    p2._claudeSignin();
    await settle();
    const scrim = body.children.filter((c) => c.className === 'mp-claude-scrim').pop();
    const b = scrim.querySelector('[data-mp-cs-body]');
    out.popupStarted = calls.slice(start).some((c) => c.url === '/api/inference/claude-signin' && c.method === 'POST');
    out.linkHref = b.querySelector('[data-mp-cs-link]').href;
    b.querySelector('[data-mp-cs-code]').value = 'invented-code#state';
    const fin = b.querySelector('[data-mp-cs-finish]');
    await fin.listeners.click[0]();
    await settle();
    const codeCall = calls.slice(start).find((c) => c.url === '/api/inference/claude-signin/code');
    out.codeSent = codeCall ? JSON.parse(codeCall.body).code : null;
    out.signedInText = b.innerHTML;
    out.askedCircles = calls.some((c) => c.postMessage && c.postMessage.app_id === 'circles');
    p2.destroy();

    // A step slower than the router's window answers 202 + a job: the pop-up
    // reads the job until it is done.
    sandbox.__immediate = true;
    routes['/api/inference/claude-signin POST'] = { pending: true, job: 'j1', poll: '/api/inference/claude-signin/jobs/j1' };
    routes['/api/inference/claude-signin/jobs/j1'] = { status: 'done', result: { state: 'waiting', ok: true, url: 'https://claude.ai/oauth/authorize?job=1', needs_code: true } };
    const p3 = sandbox.window.ModelPicker.mount(fakeEl('root3'), { simple: true });
    await settle();
    p3._claudeSignin();
    await settle(60);
    const scrim3 = body.children.filter((c) => c.className === 'mp-claude-scrim').pop();
    out.jobLinkHref = scrim3.querySelector('[data-mp-cs-body]').querySelector('[data-mp-cs-link]').href;
    out.jobPolled = calls.some((c) => c.url === '/api/inference/claude-signin/jobs/j1');
    sandbox.__immediate = false;
    p3.destroy();

    // (e) an embedding model is never offered as a model a role runs on, and
    // with a key held the Thinking step says which model chat uses and why.
    routes = {
        '/api/inference/routing': {
            routing: {},
            effective: { chat: { provider: 'local', model: 'qwen-invented', source: 'derived',
                                 local_default: { machine: 'ws-invented' },
                                 default_why: 'invented-why: a model on your own machine comes first.' } },
            providers: [{ id: 'anthropic', has_key: true }, { id: 'local', has_key: true }] },
        '/api/inference/local-models': { local_models: [
            { id: 'qwen-invented', bastion: 'ws-invented', endpoint: 'http://x', embedding: false },
            { id: 'nomic-embed-text', bastion: 'ws-invented', endpoint: 'http://x', embedding: true },
            { id: 'mystery-embedder', bastion: 'ws-invented', endpoint: 'http://x', embedding: true }] },
        // The catalog lists the same machines' models, one of them unmarked.
        '/api/inference/catalog': { local_models: [
            { id: 'nomic-embed-text', provider: 'local', available: true },
            { id: 'catalog-embedder', provider: 'local', available: true, embedding: true },
            { id: 'smol-invented', provider: 'local', available: true }] },
    };
    const root5 = fakeEl('root5');
    const p5 = sandbox.window.ModelPicker.mount(root5, { simple: true, showRoutingTab: true,
        recommendations: { providers: { anthropic: { label: 'Anthropic', roles: { chat: { candidates: ['claude-invented'] } } } } } });
    await settle();
    out.laneIds = p5._localLaneModels().map((m) => m.id);
    const holder5 = fakeEl('simple5');
    root5.querySelector = (sel) => (sel === '[data-mp-view-simple]' ? holder5 : sub(root5, sel));
    try { p5._renderSimple(); } catch (e) { out.renderError5 = String(e && e.stack || e); }
    out.simpleKeyedHtml = holder5.innerHTML;
    p5.destroy();
    process.stdout.write(JSON.stringify(out));
})().catch((e) => { process.stdout.write(JSON.stringify({ fatal: String(e && e.stack || e) })); });
