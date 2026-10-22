#!/usr/bin/env node
/*
 * "Use managed instead" on a bastion that serves the Host's embeddings, in the
 * Network app (and "Use managed" on the Context app's embedding card), is
 * offered only when the managed embedder's catalog carries the current model. The switch it calls (POST /api/embedding/use-managed) refuses
 * any model outside that catalog, and a Host with no platform has no catalog,
 * so there the button could only end in an error.
 *
 * Boots the REAL app.js in a vm with a stub DOM (elements cached by id, as
 * test_network_bastion_view.js does), opens the serving bastion's inspector
 * through '#view=bastion:<id>', and reads the action buttons it rendered:
 *   - catalog empty (no platform): no "Use managed instead"
 *   - catalog carries the model: the button is there
 *   - catalog carries other models only: no button
 *   - the catalog cannot be read: no button
 * The Context app's storage view, embedding lens, is booted the same way and
 * its embedding card read for the button, in the same four cases.
 *
 * Run: node scripts/test_embed_use_managed_button.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const MODEL = 'invented-embed-1';
const BASTION = { id: 4, hostname: 'gpu-box-4', online: true, capabilities: ['embed'],
                  embed_endpoint: 'http://10.77.0.4:11434', models: [MODEL] };

function boot(catalogAnswer, appDir = 'network', current = null) {
    function makeEl() {
        return {
            _text: '', children: [],
            get textContent() { return this._text; },
            set textContent(v) { this._text = String(v == null ? '' : v); this.innerHTML = this._text; },
            innerHTML: '', hidden: false, dataset: {}, style: {}, value: '',
            classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
            addEventListener() {}, appendChild(c) { this.children.push(c); }, removeAttribute() {},
            setAttribute() {}, querySelectorAll() { return []; }, querySelector() { return null; },
            closest() { return null; }, select() {}, focus() {},
        };
    }
    const byId = {};
    let ready = null;
    const doc = {
        getElementById: (id) => (byId[id] = byId[id] || makeEl()),
        createElement: () => makeEl(),
        querySelectorAll: () => [],
        querySelector: () => makeEl(),
        addEventListener(type, fn) { if (type === 'DOMContentLoaded') ready = fn; },
        execCommand: () => true,
        body: makeEl(),
        hidden: false,
    };
    const answer = (body) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body),
                                               text: () => Promise.resolve(JSON.stringify(body)) });
    const sandbox = {
        document: doc,
        console,
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
        setInterval: () => 0, clearInterval() {}, setTimeout: () => 0, clearTimeout() {},
        fetch: (url) => {
            const u = String(url);
            if (u.startsWith('/api/network/bastions') && !u.includes('/bastions/')) return answer({ bastions: [BASTION] });
            if (u === '/api/embedding/bastions') {
                return answer({ bastions: [{ hostname: BASTION.hostname, embed_endpoint: BASTION.embed_endpoint }] });
            }
            if (u === '/api/embedding/current') {
                if (current) return answer(current);
                return answer({ provider: 'local', model: MODEL, dim: 1024, bastion: BASTION.hostname,
                                embedding_base_url: BASTION.embed_endpoint, configured: true });
            }
            if (u === '/api/embedding/catalog') {
                return catalogAnswer === null ? Promise.reject(new Error('down')) : answer(catalogAnswer);
            }
            return Promise.reject(new Error('no network in this test'));
        },
        navigator: { userAgent: 'node' },
        location: { href: 'http://localhost/', origin: 'http://localhost', hash: '#view=bastion%3A4' },
        parent: {},
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
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'system-apps', appDir, 'app.js'), 'utf8'),
        sandbox, { filename: 'app.js' });
    byId.inspector = makeEl();
    byId.inspector.hidden = true;
    return { byId, sandbox, start: () => ready && ready() };
}

const settle = () => new Promise((r) => setImmediate(r));
async function flush() { for (let i = 0; i < 20; i++) await settle(); }

const failures = [];
let checks = 0;
function check(cond, msg) { checks++; if (!cond) failures.push(msg); }

async function labels(catalogAnswer) {
    const app = boot(catalogAnswer);
    app.start();
    await flush();
    const opened = !app.byId.inspector.hidden;
    return { opened, labels: ((app.byId['inspector-actions'] || {}).children || []).map((b) => b.textContent) };
}

async function contextCard(catalogAnswer, current = null) {
    const app = boot(catalogAnswer, 'context-manager', current);
    const ctx = app.sandbox.window.ContextApp;
    ctx.state.lens = 'embedding';
    ctx.showView('storage');
    await flush();
    return (app.byId['embed-model-card'] || {}).innerHTML || '';
}

(async () => {
    const none = await labels({ embedding_models: [] });
    check(none.opened, 'the serving bastion\'s inspector opens');
    check(none.labels.includes('Revoke bastion'), `the inspector renders its actions (got ${JSON.stringify(none.labels)})`);
    check(!none.labels.includes('Use managed instead'),
        `no platform, no managed catalog: no "Use managed instead" (got ${JSON.stringify(none.labels)})`);

    const carries = await labels({ embedding_models: [{ id: MODEL, output_dim: 1024 }] });
    check(carries.labels.includes('Use managed instead'),
        `the managed catalog carries the model: the button is offered (got ${JSON.stringify(carries.labels)})`);

    const other = await labels({ embedding_models: [{ id: 'some-other-embed', output_dim: 1024 }] });
    check(!other.labels.includes('Use managed instead'),
        `the managed catalog lacks the model: no button (got ${JSON.stringify(other.labels)})`);

    const down = await labels(null);
    check(!down.labels.includes('Use managed instead'),
        `the catalog cannot be read: no button (got ${JSON.stringify(down.labels)})`);

    const cNone = await contextCard({ embedding_models: [] });
    check(cNone.includes(MODEL), `the Context app's embedding card renders the model (got ${cNone.slice(0, 120)})`);
    check(!cNone.includes('embed-use-managed'), 'Context app, no managed catalog: no "Use managed"');
    check((await contextCard({ embedding_models: [{ id: MODEL, output_dim: 1024 }] })).includes('embed-use-managed'),
        'Context app, the managed catalog carries the model: "Use managed" is offered');
    check(!(await contextCard({ embedding_models: [{ id: 'some-other-embed' }] })).includes('embed-use-managed'),
        'Context app, the managed catalog lacks the model: no "Use managed"');
    check(!(await contextCard(null)).includes('embed-use-managed'),
        'Context app, the catalog cannot be read: no "Use managed"');

    // The card's lane pill names the lane /api/embedding/current reports.
    const prov = await contextCard({ embedding_models: [] },
        { provider: 'openai', model: 'invented-provider-embed', dim: 1024, bastion: null,
          embedding_base_url: null, configured: true });
    check(prov.includes('>openai</span>') && !prov.includes('>managed</span>'),
        `Context app, a provider-key lane is named by its provider, not "managed" (got ${prov.slice(0, 200)})`);
    const unset = await contextCard({ embedding_models: [] },
        { provider: null, model: null, dim: null, bastion: null, embedding_base_url: null, configured: false });
    check(unset.includes('>not set</span>') && !unset.includes('>managed</span>'),
        `Context app, no model set reads "not set" (got ${unset.slice(0, 200)})`);
    const managed = await contextCard({ embedding_models: [{ id: MODEL }] },
        { provider: 'privatae', model: MODEL, dim: 1024, bastion: null, embedding_base_url: null, configured: true });
    check(managed.includes('>managed</span>'), 'Context app, the managed lane still reads "managed"');

    if (failures.length) {
        failures.forEach((f) => console.log('  FAIL  ' + f));
        console.log(`\n${failures.length} of ${checks} failed`);
        process.exit(1);
    }
    console.log(`embed use-managed button: ${checks}/${checks} checks passed`);
})().catch((e) => { console.log('  FAIL  ' + (e && e.stack || e)); process.exit(1); });
