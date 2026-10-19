/**
 * ModelPicker — the Orchestrator app's catalog/routing/balances widget (its only
 * consumer since the Orchestrator absorbed the per-app pickers). Mount with:
 *
 *   const picker = ModelPicker.mount(rootEl, {
 *       showBalances: false,       // include the Balances tab (provider account balances)
 *       showRoutingTab: true,      // include the Routing tab
 *       showCoding: true,          // Coding Engines (per-engine defaults + the Default tick)
 *                                  // + Multi-Agent Coding (the protocol files)
 *       defaultTab: 'models',      // which tab opens first ('routing' in the Orchestrator)
 *       simple: false,             // Simple/Advanced switch: Simple = the overlay
 *                                  // ("what answers when") + one-click recommended routing;
 *                                  // Advanced = the full surface below, unchanged
 *       recommendations: null,     // recommended-routes.json, fetched by the app and passed
 *                                  // in — DATA the user applies, never pins (see the file)
 *       onChange: null,            // (kind, payload) after saves — 'routing' | 'favorites' |
 *                                  // 'coding-harnesses' | 'coding-default' | 'coding-protocols' | 'embedding'
 *   });
 *
 * Returns { reload(), destroy() }. The widget owns its own nav, tabs, and
 * fetches /api/inference/*. Apps can wire `onChange` to react to saves.
 *
 * PROVIDER → MODEL PER ROLE: every
 * text role independently picks a PROVIDER, then a MODEL from the user's
 * favorites for that provider, plus an optional FALLBACK PROVIDER (answers when
 * the primary provider errors or doesn't host the model). Providers come from
 * the backend registry via the routing payload (never hardcoded here); API keys
 * live in the Keys app — this widget never touches credentials. 'local' is
 * always available: models the user's machines advertise over their mesh.
 *
 * BALANCES: the inert billing-mode switch (privatae, self, local —
 * nothing read it at inference time) is gone. The tab now answers the only
 * real money question: what does each of the user's own provider accounts
 * hold, dialed live via GET /api/inference/balances.
 */
(() => {
    // The text roles — each picks provider → model (+ optional fallback).
    // `code` is NOT here: coding is the Coding Engines table (each engine
    // brings its own model) + the Multi-Agent Coding protocol files, rendered
    // as their own sections below. Every role here is read through the backend
    // resolver, so an assignment is real, never decorative. `embedding` is
    // intentionally omitted — it routes through its own /api/embedding/switch
    // flow (re-embed job).
    //
    // Two, not eight: the orchestrator was too complicated, and model calls
    // belong only where reasoning happens. plan, research, agents, tutor and
    // journal are gone — over 14 days, four of them never fired at all and
    // the other two had one obvious home — and consolidation ("Memory") went
    // when every memory op became a tool call in the host's own
    // turn. Those consumers now name chat at their own call sites, so the
    // feature works without the user filling a seat first. This list must match
    // server/routes/inference.py ROLES; scripts/test_three_text_roles.py
    // checks that it does.
    const ROLES = ['chat', 'review'];

    // Display labels — the backend role id 'chat' is the host's conversational /
    // orchestration model, surfaced to the user as "Host model".
    const ROLE_LABELS = {
        chat: 'Host model', review: 'Review',
    };

    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function formatContext(n) {
        if (!n) return '—';
        if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, '') + 'M';
        if (n >= 1_000)     return (n / 1_000).toFixed(0) + 'K';
        return String(n);
    }

    // Catalog SHOWS vendor list price so users can compare what a model costs
    // (an earlier "no pricing" rule had no basis). This is
    // the provider's per-token list price rendered as $/M in·out — carried by
    // OpenRouter's catalog and DeepInfra's public list, absent
    // where the provider doesn't publish it. Never Privatae's billing basis
    // (there is no platform inference billing — balances live on the Balances
    // tab). Local = free.
    function perM(v) {
        if (v == null || v === '') return null;
        const n = typeof v === 'number' ? v : parseFloat(v);
        return Number.isFinite(n) ? n * 1_000_000 : null;
    }
    function fmtUsd(n) {
        if (n === 0) return '$0';
        if (n < 1) return '$' + n.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
        return '$' + (Math.round(n * 100) / 100);
    }
    // VALIDATED: the model passed the platform's admission
    // bench under the current suite — a product-truth label like the price,
    // from the platform-shipped list (validated_models.json), never a
    // per-host opinion. Absent = not validated: once the gate enforces, such
    // a model on chat keeps read tools and loses operate/dispatch.
    function validatedPill(m) {
        if (!m || !m.validated) return '';
        return ' <span class="cseries-pill ok" title="Passed the platform\'s admission bench for tool use under the current suite.">validated</span>';
    }

    function formatPrice(m) {
        if (m.provider === 'local') return 'free';
        if (m.subscription) return 'subscription';   // flat plan — no per-token price exists
        const i = perM(m.input_per_token), o = perM(m.output_per_token);
        if (i == null && o == null) return '—';
        if (i === 0 && o === 0) return 'free';
        return fmtUsd(i == null ? 0 : i) + ' / ' + fmtUsd(o == null ? 0 : o);
    }
    // Per-cell provenance: where the number came from when it isn't the
    // provider's own API (first-party list prices ride the OpenRouter catalog).
    function priceTitle(m) {
        if (m.subscription) return 'Included in your subscription plan';
        if (m.price_source) return '$ per 1M tokens — ' + m.price_source;
        return '$ per 1M tokens — input / output';
    }

    function formatCreated(ts) {
        if (!ts) return '—';
        const d = new Date(ts * 1000);
        return d.toISOString().slice(0, 10);
    }

    function vendorOf(m) {
        const id = m.id || '';
        return id.includes('/') ? id.split('/', 1)[0] : (m.provider || 'local');
    }

    function modalityOf(m) {
        return (m.architecture && m.architecture.modality) || (m.provider === 'local' ? 'text->text' : '—');
    }

    function flatten(c) {
        const out = [];
        for (const m of (c.local_models || [])) {
            out.push({
                id: m.id,
                name: m.name || m.id,
                provider: 'local',
                vendor: 'local',
                context_length: 0,
                created: 0,
                modality: 'text->text',
                description: 'Local model — served on your own hardware',
                available: m.available !== false,
            });
        }
        for (const m of (c.openrouter_models || [])) {
            out.push({
                id: m.id,
                name: m.name || m.id,
                provider: 'openrouter',
                vendor: vendorOf(m),
                context_length: m.context_length || 0,
                input_per_token: m.input_per_token,
                output_per_token: m.output_per_token,
                created: m.created || 0,
                modality: modalityOf(m),
                description: m.description || '',
                available: true,
                validated: !!m.validated,
            });
        }
        return out;
    }

    async function fetchJSON(url, init) {
        try {
            const r = await fetch(url, init);
            if (!r.ok) throw new Error(`${r.status}`);
            return await r.json();
        } catch (e) {
            return null;
        }
    }

    // Like fetchJSON but keeps the failure detail — a refused save needs the
    // 400 reason verbatim, not a silent null.
    async function fetchDetail(url, init) {
        try {
            const r = await fetch(url, init);
            const body = await r.json().catch(() => ({}));
            if (!r.ok) {
                return { ok: false, status: r.status,
                         detail: (body && body.detail) ? body.detail : `HTTP ${r.status}` };
            }
            return { ok: true, body };
        } catch (e) {
            return { ok: false, status: 0, detail: e.message };
        }
    }

    class Picker {
        constructor(root, opts) {
            this.root = root;
            this.opts = Object.assign({
                showBalances: false,
                showRoutingTab: true,
                showCoding: true,   // Coding Engines + Multi-Agent Coding are SET here (the host reads them)
                defaultTab: 'models',
                simple: false,
                recommendations: null,
                onChange: null,
            }, opts || {});
            // Simple vs Advanced. Simple is the default for a fresh
            // host; the choice sticks per browser. localStorage can be blocked
            // in a sandboxed iframe — degrade to per-mount memory.
            let mode = 'simple';
            try { mode = window.localStorage.getItem('mp-orch-mode') || 'simple'; } catch (e) {}
            this.mode = this.opts.simple ? (mode === 'advanced' ? 'advanced' : 'simple') : 'advanced';
            this.state = {
                catalog: null,
                routing: {},
                effective: {},
                // providers: [{id, has_key}] from the routing payload — the
                // backend registry + {id:'local', has_key:true}. Keyless
                // providers render disabled ("— add key in Keys").
                providers: [],
                // Per-provider favorites: {provider: [model_id, …]}. The model
                // dropdowns are built from THIS, never a hardcoded list.
                favoritesByProvider: {},
                // Provider account balances for the Balances tab
                // (/api/inference/balances — per-provider rows, some
                // unsupported, none invented).
                balances: [],
                // Models tab: which provider's catalog is showing. 'openrouter'
                // keeps the searchable/facetable catalog machinery; any other
                // registry provider lists its own /direct-models; 'local' is
                // the mesh table.
                modelsProvider: 'openrouter',
                directModels: {},     // {provider: [..] | null(loading) | {error}}
                // Transient per-role provider browse (a provider pick swaps the
                // model control; nothing saves until a model is chosen).
                roleSel: {},
                // The OpenRouter catalog DEFAULTS to the user's favorites. The
                // "Browse all models" toggle (and any active search) flips to
                // the full list.
                favoritesView: true,
                filters: { search: '', mode: '', vendor: '', modality: '' },
                sort: { col: 'name', dir: 'asc' },
                expanded: null,
                tab: this.opts.defaultTab || 'models',
            };
            this._render();
            this.reload();
        }

        async reload() {
            // RESILIENT load: one failing endpoint must NOT blank the whole
            // picker. allSettled + per-section: render whatever loaded, record which
            // failed so the tab can flag it instead of going dark.
            const sources = [
                ['catalog',      '/api/inference/catalog'],
                ['favorites',    '/api/inference/favorites'],
                ['routing',      '/api/inference/routing'],
                ['balances',     this.opts.showBalances ? '/api/inference/balances' : null],
                ['embedCurrent', '/api/embedding/current'],
                ['embedBastions', '/api/embedding/bastions'],
                ['localModels',  '/api/inference/local-models'],
                ['localEndpoints', '/api/inference/local-endpoints'],
                // Voice is a PROVIDER choice, not provider+model — the model is
                // the browser app's at socket-open time (server/routes/realtime.py).
                ['voice',        '/api/inference/voice'],
                ['matrix',       this.opts.showCoding ? '/api/inference/coding-matrix' : null],
                ['protocols',    this.opts.showCoding ? '/api/coding-protocols' : null],
            ];
            const settled = await Promise.allSettled(
                sources.map(([, url]) => (url ? fetchJSON(url) : Promise.resolve(null)))
            );
            const got = {};
            const failed = [];
            settled.forEach((s, i) => {
                const [key, url] = sources[i];
                if (s.status === 'fulfilled') {
                    got[key] = s.value;
                } else {
                    failed.push(key);
                    console.warn('ModelPicker: ' + url + ' failed:', s.reason && s.reason.message);
                }
            });
            this.state.loadErrors = failed;

            if (got.catalog) this.state.catalog = got.catalog;
            if (got.embedCurrent) this.state.embedCurrent = got.embedCurrent;
            if (got.embedBastions) this.state.embedBastions = got.embedBastions.bastions || [];
            if (got.routing) {
                this.state.routing = got.routing.routing || {};
                this.state.effective = got.routing.effective || {};
                this.state.providers = got.routing.providers || [];
                this.state.roleDescriptions = got.routing.role_descriptions || {};
                this.state.harnessOptions = got.routing.harness_options || [];
                this.state.codingHarnesses = got.routing.coding_harnesses || [];
                this.state.defaultCodingHarness = got.routing.default_coding_harness || null;
                this.state.favoritesByProvider = got.routing.favorites_by_provider || {};
                // {provider: [efforts]} — which providers get a reasoning
                // control at all. Server-owned (TEXT_ROLE_EFFORTS), never
                // guessed here: a provider the gateway cannot send an effort
                // for must render no control.
                this.state.roleEfforts = got.routing.role_efforts || {};
                // {role: {provider, model, efforts, default, known}} for the
                // route each row shows (server effort_options). Anthropic's
                // levels are per MODEL (the Models API) and its blank pick
                // names the model's own default, so those rows read this.
                this.state.roleEffortOptions = got.routing.role_effort_options || {};
            }
            if (got.favorites) {
                // GET /favorites returns the legacy flat list (OpenRouter slugs)
                // AND by_provider. Merge both into the per-provider map; the
                // flat list folds into 'openrouter' so favorites starred from
                // other surfaces stay visible here.
                const by = got.favorites.by_provider || {};
                const merged = Object.assign({}, this.state.favoritesByProvider);
                Object.keys(by).forEach((p) => {
                    merged[p] = Array.from(new Set([].concat(merged[p] || [], by[p])));
                });
                const flat = got.favorites.favorites || [];
                if (flat.length) {
                    merged.openrouter = Array.from(new Set([].concat(merged.openrouter || [], flat)));
                }
                this.state.favoritesByProvider = merged;
            }
            if (got.balances) this.state.balances = got.balances.balances || [];
            if (got.localModels) this.state.localModels = got.localModels.local_models || [];
            if (got.localEndpoints) this.state.localEndpoints = got.localEndpoints;
            if (got.voice) this.state.voice = got.voice;
            if (got.matrix) this.state.matrix = got.matrix;
            // null (endpoint failed) renders the honest "unavailable" line;
            // an empty list renders the empty state.
            if (got.protocols && Array.isArray(got.protocols.protocols)) {
                this.state.protocols = got.protocols.protocols;
            } else if (this.opts.showCoding && this.state.protocols === undefined) {
                this.state.protocols = null;
            }
            this._renderTab();
        }

        destroy() {
            this.root.innerHTML = '';
        }

        // ---- Templates ----

        _render() {
            const tabs = [];
            if (this.opts.showRoutingTab) tabs.push({ id: 'routing', label: 'Routing' });
            tabs.push({ id: 'models', label: 'Models' });
            if (this.opts.showBalances)   tabs.push({ id: 'balances', label: 'Balances' });
            const startTab = tabs.some((t) => t.id === this.state.tab) ? this.state.tab : tabs[0].id;
            this.state.tab = startTab;

            this.root.innerHTML = `
                <div class="mp-root">
                    ${this.opts.simple ? `
                    <div class="mp-modebar" role="tablist" aria-label="Simple or Advanced">
                        <button class="mp-modebtn" data-mp-mode="simple" role="tab">Simple</button>
                        <button class="mp-modebtn" data-mp-mode="advanced" role="tab">Advanced</button>
                    </div>
                    <section class="mp-view mp-simple" data-mp-view-simple hidden></section>
                    ` : ''}
                    <nav class="cseries-nav mp-nav">
                        ${tabs.map((t) => `
                            <button class="cseries-nav-btn ${t.id === startTab ? 'active' : ''}" data-mp-tab="${t.id}">${esc(t.label)}</button>
                        `).join('')}
                    </nav>

                    ${this.opts.showRoutingTab ? `
                    <section class="mp-view" data-mp-view="routing" ${startTab === 'routing' ? '' : 'hidden'}>
                        <div class="mp-section-head card-label label-sys">Text roles</div>
                        <div class="label-sys mp-help">Pick a provider, then a model, per role. The fallback answers when the primary fails.
                            <button class="cseries-btn ghost mp-keys-link" data-mp-open-keys>Open Keys</button></div>
                        <div class="mp-routing" data-mp-routing></div>
                        <div class="mp-local-lane" data-mp-local></div>
                        ${this.opts.showCoding ? `
                        <div class="mp-section-head card-label label-sys">Coding Engines</div>
                        <div class="label-sys mp-help">One row per engine: whether it's connected, its default model and effort. Tick one as Default — builds that don't name an engine run there.</div>
                        <div class="mp-matrix" data-mp-matrix></div>
                        <div class="mp-section-head card-label label-sys">Multi-Agent Coding</div>
                        <div class="label-sys mp-help">Protocols: JSON instruction files your host follows — which engines, one after another or at once, what counts as pass or fail. Ask the host to write one, edit the file, share it, import one.</div>
                        <div class="mp-protocols" data-mp-protocols></div>
                        ` : ''}
                        <div class="mp-section-head card-label label-sys">Embedding</div>
                        <div class="label-sys mp-help">Where your memory is indexed and searched — a machine you run, or a provider you key.</div>
                        <div class="mp-embed" data-mp-embed></div>
                        <div class="mp-section-head card-label label-sys">Image generation</div>
                        <div class="label-sys mp-help">Making images runs on the model you pick — pick from the models your provider says can do it. Looking at images is the Host model's own job.</div>
                        <div class="mp-image-roles" data-mp-image></div>
                    </section>
                    ` : ''}

                    <section class="mp-view" data-mp-view="models" ${startTab === 'models' ? '' : 'hidden'}>
                        <div class="mp-models-head">
                            <label class="label-sys mp-models-prov-label">Provider
                                <select class="cseries-select" data-mp-models-provider aria-label="Provider"></select>
                            </label>
                            <span class="label-sys mp-models-prov-note" data-mp-models-prov-note></span>
                        </div>
                        <div class="mp-controls" data-mp-catalog-controls>
                            <input type="text" class="cseries-input" data-mp-search placeholder="Search models, vendors, descriptions…">
                            <button class="cseries-btn mp-browse-toggle" data-mp-browse-toggle>Browse all models</button>
                            <select class="cseries-select" data-mp-filter-vendor>
                                <option value="">All vendors</option>
                            </select>
                            <select class="cseries-select" data-mp-filter-modality>
                                <option value="">Any modality</option>
                            </select>
                        </div>
                        <div class="mp-table-wrap" data-mp-catalog-wrap>
                            <table class="mp-table">
                                <thead>
                                    <tr>
                                        <th class="mp-star-col"></th>
                                        <th class="sortable" data-sort="name">Name</th>
                                        <th class="sortable" data-sort="vendor">Vendor</th>
                                        <th class="sortable num" data-sort="context_length">Context</th>
                                        <th class="num" title="Vendor list price, $ per 1M tokens (input / output)">Price /M</th>
                                        <th class="sortable num" data-sort="created">Released</th>
                                        <th class="sortable" data-sort="modality">Modality</th>
                                    </tr>
                                </thead>
                                <tbody data-mp-tbody>
                                    <tr><td colspan="7" class="mp-empty">Loading catalog…</td></tr>
                                </tbody>
                            </table>
                        </div>
                        <div data-mp-models-body></div>
                        <div class="mp-foot">
                            <span class="label-sys" data-mp-count>—</span>
                        </div>
                    </section>

                    ${this.opts.showBalances ? `
                    <section class="mp-view" data-mp-view="balances" hidden>
                        <div class="cseries-card">
                            <div class="card-label label-sys">Provider balances</div>
                            <div class="label-sys mp-help">Live from each provider's own API, under your keys. You pay providers directly — Privatae never bills inference.
                                <button class="cseries-btn ghost mp-keys-link" data-mp-open-keys>Open Keys</button></div>
                            <div class="mp-balances-list" data-mp-balances></div>
                            <div style="margin-top:10px"><button class="cseries-btn ghost" data-mp-balances-refresh>Refresh</button></div>
                        </div>
                    </section>
                    ` : ''}
                </div>
            `;
            this._bindEvents();
            // Simple is the default surface — apply mode visibility now, not
            // only after the first reload lands.
            if (this.opts.simple) this._renderTab();
        }

        _renderTab() {
            const simple = this.opts.simple && this.mode === 'simple';
            this.root.querySelectorAll('[data-mp-mode]').forEach((btn) => {
                btn.classList.toggle('active', btn.dataset.mpMode === this.mode);
                btn.setAttribute('aria-selected', btn.dataset.mpMode === this.mode ? 'true' : 'false');
            });
            const nav = this.root.querySelector('.mp-nav');
            if (nav) nav.hidden = simple;
            const sv = this.root.querySelector('[data-mp-view-simple]');
            if (sv) sv.hidden = !simple;
            if (simple) {
                this.root.querySelectorAll('[data-mp-view]').forEach((v) => { v.hidden = true; });
                this._renderSimple();
                return;
            }
            const t = this.state.tab;
            this.root.querySelectorAll('[data-mp-tab]').forEach((btn) => {
                btn.classList.toggle('active', btn.dataset.mpTab === t);
            });
            this.root.querySelectorAll('[data-mp-view]').forEach((v) => {
                v.hidden = v.dataset.mpView !== t;
            });
            if (t === 'models') this._renderModels();
            if (t === 'routing') this._renderRouting();
            if (t === 'balances') this._renderBalances();
        }

        _setMode(m) {
            this.mode = m === 'advanced' ? 'advanced' : 'simple';
            try { window.localStorage.setItem('mp-orch-mode', this.mode); } catch (e) {}
            this._renderTab();
        }

        // ── Simple mode ──────────────────────────────────────────────────
        // Two pieces: the OVERLAY ("when I do X, which provider/model answers,
        // and what happens if it fails" — plain sentences, no grid) and the
        // one-click SETUP (recommended routes per keyed provider, applied as
        // ordinary, visible model_routing entries via the same PUT the
        // Advanced grid uses). Recommendations are DATA passed in at mount
        // (recommended-routes.json) — the resolver never reads them; an
        // unapplied recommendation routes nothing.

        // Providers with recommendation data, keyed or not, in registry order.
        _recProviders() {
            const recs = ((this.opts.recommendations || {}).providers) || {};
            return this._providers()
                .filter((p) => p.id !== 'local' && recs[p.id])
                .map((p) => ({ ...p, rec: recs[p.id] }));
        }

        _recModelFor(prov, role) {
            const recs = ((this.opts.recommendations || {}).providers) || {};
            const entry = ((recs[prov] || {}).roles || {})[role];
            const cands = (entry && entry.candidates) || [];
            const avail = this._availableIdsFor(prov);
            if (Array.isArray(avail) && avail.length) {
                const hit = cands.find((c) => avail.indexOf(c) >= 0);
                if (hit) return hit;
            }
            return cands[0] || null;
        }

        // What `prov`'s catalog serves under the user's key: an array of ids,
        // null while loading, or {error}. openrouter reads the already-loaded
        // catalog; direct providers lazily fetch /direct-models (which
        // re-renders this view on arrival via _ensureDirectModels).
        _availableIdsFor(prov) {
            if (prov === 'openrouter') {
                // No catalog + a recorded fetch failure = unreachable, not
                // loading — the plan must degrade to the unverified apply, not
                // wedge the button at "Checking the catalog…" forever.
                if (!this.state.catalog) {
                    return ((this.state.loadErrors || []).indexOf('catalog') >= 0)
                        ? { error: 'openrouter catalog unreachable' } : null;
                }
                const ids = flatten(this.state.catalog)
                    .filter((m) => m.provider === 'openrouter').map((m) => m.id);
                const favs = ((this.state.favoritesByProvider || {}).openrouter) || [];
                return ids.concat(favs.filter((f) => ids.indexOf(f) < 0));
            }
            this._ensureDirectModels(prov);
            const dm = (this.state.directModels || {})[prov];
            if (dm == null) return null;
            if (dm.error) return { error: String(dm.error) };
            return dm.map((m) => (typeof m === 'string' ? m : (m.id || m.name)))
                .filter(Boolean);
        }

        // The plan preview for one provider: per text role, the first
        // recommended candidate the live catalog serves — or the first
        // candidate marked unverified when the catalog can't confirm it
        // (same honesty as the manual model-id entry).
        _planRows(prov) {
            const recs = ((this.opts.recommendations || {}).providers) || {};
            const roles = (recs[prov] || {}).roles || {};
            const avail = this._availableIdsFor(prov);
            const efforts = (this.state.roleEfforts || {})[prov] || [];
            return ROLES.map((role) => {
                const entry = roles[role] || {};
                const cands = entry.candidates || [];
                let model = cands[0] || null;
                let status = 'unknown';
                if (Array.isArray(avail)) {
                    const hit = cands.find((c) => avail.indexOf(c) >= 0);
                    if (hit) { model = hit; status = 'served'; }
                    else status = 'unlisted';
                } else if (avail && avail.error) {
                    status = 'catalog-error';
                } else {
                    status = 'loading';
                }
                // Only an effort the server offers for this provider is
                // applied — the PUT refuses anything else, and a refused
                // effort must not sink the whole role.
                const effort = (entry.effort && efforts.indexOf(entry.effort) >= 0)
                    ? entry.effort : null;
                return { role, model, status, effort, why: entry.why || '' };
            }).filter((r) => r.model);
        }

        async _applyPlan(prov) {
            const rows = this._planRows(prov);
            const fb = (this.state.simpleFb && this.state.simpleFb !== prov)
                ? this.state.simpleFb : null;
            this.state.simpleApplying = true;
            this.state.simpleResults = {};
            this._renderSimple();
            // SEQUENTIAL on purpose: the routing PUT read-modifies-writes the
            // whole behavioral_params row — parallel PUTs would race and lose
            // routes.
            for (const row of rows) {
                const body = { role: row.role, provider: prov, model: row.model };
                if (row.effort) body.effort = row.effort;
                if (fb) {
                    body.fallback_provider = fb;
                    // The fallback provider's OWN recommended model for this
                    // role — model ids don't carry across providers.
                    const fm = this._recModelFor(fb, row.role);
                    if (fm) body.fallback_model = fm;
                }
                const res = await fetchDetail('/api/inference/routing', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                this.state.simpleResults[row.role] = res.ok
                    ? { ok: true,
                        local: !!(res.body && res.body.defaulted_local),
                        effDropped: !!(res.body && res.body.effort_dropped) }
                    : { ok: false, detail: res.detail || 'could not set this route' };
                this._renderSimple();
            }
            this.state.simpleApplying = false;
            await this.reload();
            if (typeof this.opts.onChange === 'function') this.opts.onChange('routing', this.state.routing);
        }

        // One overlay row: activity → who answers → what happens on failure.
        _overlayRowHtml(activity, answer, failure, state) {
            const pill = state === 'ok' ? ''
                : state === 'soft' ? '<span class="cseries-pill info">uses the Host model</span>'
                : '<span class="cseries-pill warn">nothing yet</span>';
            return `
                <div class="mp-ov-row">
                    <div class="mp-ov-act">${activity}</div>
                    <div class="mp-ov-ans">${answer} ${pill}</div>
                    ${failure ? `<div class="mp-ov-fail label-sys">${failure}</div>` : ''}
                </div>`;
        }

        _overlayHtml() {
            const eff = this.state.effective || {};
            const errs = this.state.loadErrors || [];
            // A failed load must never read as absence: "no model picked" on a
            // configured host is exactly the false claim this surface exists
            // to prevent. Say "couldn't read", per source.
            if (errs.indexOf('routing') >= 0) {
                return '<div class="mp-ov-row"><div class="mp-ov-ans label-sys">Couldn\'t read your routing — reload to retry.</div></div>';
            }
            // One line per role, in the user's terms: what happens, not which
            // module runs. Chat covers every lane with no model of its own —
            // the editor, the phase planner, web research and dispatched
            // agents all resolve the chat route (three-role cut).
            const ACT = {
                chat: 'You talk to your host — and it plans, researches and runs agents',
                review: 'Your host judges a build it just landed',
            };
            const rows = ROLES.map((role) => {
                const e = eff[role] || {};
                const model = e.model || e.model_id || '';
                if (e.provider && model && !e.unconfigured) {
                    // Which of the user's credentials answers (xAI: the
                    // sign-in, or the per-token API key when there is none).
                    const answered = this._credentialAnswered(e.provider);
                    // An unset Anthropic route runs at the model's own
                    // default, which the server names when the docs do.
                    const thinks = e.effort
                        || (e.provider === 'anthropic' && this._roleEffortDefault(role)
                            ? 'default · ' + this._roleEffortDefault(role) : '');
                    const answer = `<strong>${esc(e.provider)}</strong> · ${esc(model)}`
                        + (thinks ? ` <span class="label-sys">(thinks: ${esc(thinks)})</span>` : '')
                        + (answered ? ` <span class="label-sys">on your ${esc(answered)}</span>` : '');
                    // Only the row-SPECIFIC failure story is printed here. The
                    // no-fallback case used to print "if it fails you see the
                    // error — nothing substitutes silently" on every such row,
                    // which on a normal host is up to eight identical lines
                    // (repetitive and noisy). That promise
                    // is still made — once, at the section head.
                    const failure = e.fallback_provider
                        ? `if ${esc(e.provider)} fails → one retry on ${esc(e.fallback_provider)} · ${esc(e.fallback_model || 'same model')}, and the answer says so`
                        : '';
                    return this._overlayRowHtml(esc(ACT[role] || role), answer, failure, 'ok');
                }
                // The 'no model of its own' row is gone with the tutor role:
                // every remaining role either has a route or has none, and
                // neither borrows the other's.
                return this._overlayRowHtml(esc(ACT[role] || role),
                    '<span class="label-sys">no model picked</span>', '', 'warn');
            });

            // Beyond the text roles: memory search, images, builds — their own
            // stores, shown here so the answer to "what runs what" is complete.
            const cur = this.state.embedCurrent || {};
            if (errs.indexOf('embedCurrent') >= 0) {
                rows.push(this._overlayRowHtml('Your memories are indexed and searched',
                    '<span class="label-sys">couldn\'t read the embedding store — reload to retry</span>', '', 'ok'));
            } else {
                rows.push(this._overlayRowHtml('Your memories are indexed and searched',
                    cur.model
                        ? `<strong>${esc(cur.provider === 'local' ? 'your machine' : (cur.provider || 'platform'))}</strong> · ${esc(cur.model)}`
                        : '<span class="label-sys">no embedding model picked</span>',
                    cur.model ? '' : 'set it in Advanced → Embedding',
                    cur.model ? 'ok' : 'warn'));
            }
            // No 'vision' row: a picture you share goes to the Host
            // model itself, the row above, so there is no second model to show.
            [['image_gen', 'Your host generates an image']].forEach(([role, act]) => {
                // Read the stored route too, exactly as the Advanced image
                // renderer does, or a configured image role would show as
                // absent here.
                const e = eff[role] || {};
                const r = this.state.routing[role] || {};
                const prov = e.provider || r.provider || '';
                const model = e.model || e.model_id || r.model || '';
                rows.push(this._overlayRowHtml(esc(act),
                    (prov && model) ? `<strong>${esc(prov)}</strong> · ${esc(model)}`
                        : '<span class="label-sys">no image model picked</span>',
                    (prov && model) ? '' : 'set it in Advanced → Image generation',
                    (prov && model) ? 'ok' : 'warn'));
            });
            if (errs.indexOf('matrix') >= 0) {
                rows.push(this._overlayRowHtml('You start a build without naming an engine',
                    '<span class="label-sys">couldn\'t read the coding settings — reload to retry</span>', '', 'ok'));
                return rows.join('');
            }
            const m = this.state.matrix || {};
            const engines = Array.isArray(m.harnesses) ? m.harnesses : [];
            const connected = engines.filter((r) => r.key && r.key.state === 'connected');
            const defId = m.default_engine || this.state.defaultCodingHarness || null;
            const defRow = engines.find((r) => r.id === defId) || null;
            let buildAns, buildFail = '', buildState = 'ok';
            if (defRow) {
                buildAns = `<strong>${esc(defRow.label)}</strong>`;
                if (!(defRow.key && defRow.key.state === 'connected')) {
                    buildFail = 'it is not connected right now — such a build stops and says so';
                }
            } else if (connected.length === 1) {
                buildAns = `<strong>${esc(connected[0].label)}</strong> <span class="label-sys">(your only connected engine)</span>`;
            } else if (connected.length > 1) {
                buildAns = '<span class="label-sys">no default engine ticked</span>';
                buildFail = 'the build stops and asks instead of guessing — tick one in Advanced → Coding Engines';
                buildState = 'warn';
            } else {
                buildAns = '<span class="label-sys">no coding engine connected</span>';
                buildFail = 'connect one in Advanced → Coding Engines';
                buildState = 'warn';
            }
            rows.push(this._overlayRowHtml('You start a build without naming an engine', buildAns, buildFail, buildState));
            return rows.join('');
        }

        // ── Simple: Memory (embeddings) ──────────────────────────────────
        // Simple mode has to ADDRESS embeddings, in plain
        // English, with two ways in. Before this, embeddings existed only in
        // Advanced, so a first-time user finished "setup" with no memory
        // recall and nothing told them.
        _simpleMemoryHtml() {
            const cur = this.state.embedCurrent || {};
            const errs = this.state.loadErrors || [];
            // 'local' is EXCLUDED here on purpose: running embeddings on your
            // own machine is the OTHER button, and it needs a bastion walked
            // through, not a model dropdown. Leaving local in this list also
            // hung the pane — _ensureEmbedProviderModels returns early for
            // local, so it never resolved past "reading local's models…".
            const provs = this._providers().filter(
                (p) => p.id !== 'local' && p.embeddings && p.has_key);
            const open = this.state.simpleEmbedOpen;

            let state;
            if (errs.indexOf('embedCurrent') >= 0) {
                state = '<span class="label-sys">couldn\'t read your embedding setting — reload to retry</span>';
            } else if (cur.model) {
                const where = cur.provider === 'local'
                    ? 'your machine' + (cur.bastion ? ' · ' + esc(cur.bastion) : '')
                    : esc(cur.provider || 'platform');
                state = `<span class="cseries-pill allow">on</span> <strong>${where}</strong> · ${esc(cur.model)}`;
            } else {
                state = '<span class="cseries-pill warn">not set up</span>';
            }

            let chooser = '';
            if (open) {
                const sel = this.state.simpleEmbedProv || (provs[0] && provs[0].id) || '';
                const dm = sel ? ((this.state.embedProviderModels || {})[sel]) : undefined;
                if (sel) this._ensureEmbedProviderModels(sel);
                let models;
                if (!provs.length) {
                    models = '<span class="label-sys">none of your keys serve embeddings — add one in Keys</span>';
                } else if (dm == null) {
                    models = `<span class="label-sys">reading ${esc(sel)}'s embedding models…</span>`;
                } else if (dm.error) {
                    models = `<span class="label-sys">${esc(dm.error)}</span>`;
                } else if (!dm.length) {
                    models = `<span class="label-sys">${esc(sel)} lists no embedding models</span>`;
                } else {
                    models = `<select class="cseries-select" data-mp-simple-embed-model>
                        <option value="">— pick a model —</option>
                        ${dm.map((m) => `<option value="${esc(m.id)}">${esc(m.id)}</option>`).join('')}
                    </select>
                    <button class="cseries-btn" data-mp-simple-embed-apply>Use this</button>`;
                }
                chooser = `
                    <div class="mp-simple-inline">
                        <label class="mp-role-field">provider
                            <select class="cseries-select" data-mp-simple-embed-prov>
                                ${provs.map((p) => `<option value="${esc(p.id)}"${p.id === sel ? ' selected' : ''}>${esc(p.id)}</option>`).join('')}
                            </select>
                        </label>
                        ${models}
                        <span class="label-sys" data-mp-simple-embed-status></span>
                    </div>`;
            }

            return `
                <div class="mp-setup-step" data-mp-step="memory">
                    <div class="mp-setup-head">
                        <span class="mp-setup-name">Memory</span>
                        <span class="mp-setup-state">${state}</span>
                    </div>
                    <p class="mp-setup-why">Search finds words. Embeddings know what they
                        <em>mean</em> — so your host can recall something you phrased differently
                        months ago. Adding an embeddings model turns on memory recall and the
                        features built on it.</p>
                    <div class="mp-setup-actions">
                        <button class="cseries-btn" data-mp-simple-embed-local>Run it free on your machine</button>
                        <button class="cseries-btn ghost" data-mp-simple-embed-provider>Use a provider I have a key for</button>
                    </div>
                    ${chooser}
                </div>`;
        }

        // ── Simple: Seeing (image generation) ────────────────────────────
        // Capability comes from the server's modality read
        // (/api/inference/capable-models), never from a model NAME, and there
        // is no endpoint box: it is just selecting a model that fits from an
        // authenticated provider.
        // Reading an image is not a row here: a picture you
        // share goes to the Host model itself (images are not a separate
        // role), so only MAKING one needs a model of its own.
        _simpleSeeingHtml() {
            const SEE = [
                { role: 'image_gen', label: 'Make an image' },
            ];
            const eff = this.state.effective || {};
            const keyed = this._providers().filter((p) => p.id !== 'local' && p.has_key);

            const rows = SEE.map(({ role, label }) => {
                const e = eff[role] || {};
                const r = this.state.routing[role] || {};
                const prov = e.provider || r.provider || '';
                const model = e.model || e.model_id || r.model || '';
                const cur = (prov && model)
                    ? `<strong>${esc(prov)}</strong> · ${esc(model)}`
                    : '<span class="cseries-pill warn">not set up</span>';

                let ctl = '';
                if (this.state.simpleSeeOpen === role) {
                    const sel = (this.state.simpleSeeProv || {})[role]
                        || prov || (keyed[0] && keyed[0].id) || '';
                    if (sel) this._ensureCapableModels(role, sel);
                    const list = sel ? ((this.state.capable || {})[role + '::' + sel]) : undefined;
                    let models;
                    if (!keyed.length) {
                        models = '<span class="label-sys">no provider keys yet — add one in Keys</span>';
                    } else if (list === undefined || list === null) {
                        models = '<span class="label-sys">checking which of their models can do this…</span>';
                    } else if (list.error) {
                        models = `<span class="label-sys">${esc(list.error)}</span>`;
                    } else if (!list.length) {
                        models = `<span class="label-sys">none of ${esc(sel)}'s models can do this — try another provider</span>`;
                    } else if (list.unpublished) {
                        // Never print "these can do it" over a list we did not
                        // filter — say the provider publishes nothing and let
                        // the user pick.
                        models = `<span class="label-sys">${esc(sel)} doesn't publish which of its models can do this — all ${list.length} shown, pick one that fits</span>
                        <select class="cseries-select" data-mp-simple-see-model="${esc(role)}">
                            <option value="">— pick a model —</option>
                            ${list.map((m) => `<option value="${esc(m.id)}"${m.id === model ? ' selected' : ''}>${esc(m.name || m.id)}</option>`).join('')}
                        </select>
                        <button class="cseries-btn" data-mp-simple-see-apply="${esc(role)}">Use this</button>`;
                    } else {
                        models = `<select class="cseries-select" data-mp-simple-see-model="${esc(role)}">
                            <option value="">— pick a model —</option>
                            ${list.map((m) => `<option value="${esc(m.id)}"${m.id === model ? ' selected' : ''}>${esc(m.name || m.id)}${m.verified ? '' : ' (capability inherited)'}</option>`).join('')}
                        </select>
                        <button class="cseries-btn" data-mp-simple-see-apply="${esc(role)}">Use this</button>`;
                    }
                    ctl = `
                        <div class="mp-simple-inline">
                            <label class="mp-role-field">provider
                                <select class="cseries-select" data-mp-simple-see-prov="${esc(role)}">
                                    ${keyed.map((p) => `<option value="${esc(p.id)}"${p.id === sel ? ' selected' : ''}>${esc(p.id)}</option>`).join('')}
                                </select>
                            </label>
                            ${models}
                            <span class="label-sys" data-mp-simple-see-status="${esc(role)}"></span>
                        </div>`;
                }
                return `
                    <div class="mp-setup-row">
                        <span class="mp-setup-rowname">${esc(label)}</span>
                        <span class="mp-setup-rowval">${cur}</span>
                        <button class="cseries-btn ghost" data-mp-simple-see="${esc(role)}">${prov && model ? 'Change' : 'Pick a model'}</button>
                    </div>
                    ${ctl}`;
            }).join('');

            return `
                <div class="mp-setup-step" data-mp-step="seeing">
                    <div class="mp-setup-head"><span class="mp-setup-name">Seeing</span></div>
                    <p class="mp-setup-why">Your host looks at the images you share with its own
                        model, so pick a Host model that reads images. Making new images needs an
                        image model — this list is only the models your providers say can make one.</p>
                    ${rows}
                </div>`;
        }

        // ── Simple: Hearing (realtime voice) ─────────────────────────────
        // A PROVIDER choice only — the model belongs to the browser voice app
        // at socket-open time (server/routes/realtime.py), so offering a model
        // here would be inventing one.
        _simpleHearingHtml() {
            const v = this.state.voice;
            const errs = this.state.loadErrors || [];
            if (errs.indexOf('voice') >= 0 || !v) {
                return `
                    <div class="mp-setup-step" data-mp-step="hearing">
                        <div class="mp-setup-head"><span class="mp-setup-name">Hearing</span></div>
                        <p class="mp-setup-why label-sys">Couldn't read your voice setting — reload to retry.</p>
                    </div>`;
            }
            const options = v.options || [];
            const keyed = options.filter((o) => o.has_key);
            const state = v.provider
                ? `<span class="cseries-pill allow">on</span> <strong>${esc(v.provider)}</strong>`
                : '<span class="cseries-pill warn">not set up</span>';
            const ctl = keyed.length
                ? `<select class="cseries-select" data-mp-simple-voice>
                       <option value="">— off —</option>
                       ${keyed.map((o) => `<option value="${esc(o.provider)}"${o.provider === v.provider ? ' selected' : ''}>${esc(o.provider)}</option>`).join('')}
                   </select>
                   <span class="label-sys" data-mp-simple-voice-status></span>`
                : `<span class="label-sys">no key yet for a provider that does live voice${options.length ? ' (' + options.map((o) => esc(o.provider)).join(', ') + ')' : ''}.
                   <button class="cseries-btn ghost" data-mp-open-keys>Open Keys</button></span>`;
            return `
                <div class="mp-setup-step" data-mp-step="hearing">
                    <div class="mp-setup-head">
                        <span class="mp-setup-name">Hearing</span>
                        <span class="mp-setup-state">${state}</span>
                    </div>
                    <p class="mp-setup-why">Talk to your host out loud instead of typing.</p>
                    <div class="mp-simple-inline">${ctl}</div>
                </div>`;
        }

        _renderSimple() {
            const el = this.root.querySelector('[data-mp-view-simple]');
            if (!el) return;
            // Before the first reload lands there is nothing true to show —
            // an empty-state flash here would claim "no keys" it hasn't read.
            if (this.state.loadErrors === undefined) {
                el.innerHTML = '<div class="mp-empty">Loading…</div>';
                return;
            }
            const recProviders = this._recProviders();
            const keyed = recProviders.filter((p) => p.has_key);
            const keyless = recProviders.filter((p) => !p.has_key);
            const noRec = ((this.opts.recommendations || {}).no_recommendations) || {};
            const keyedNoRec = this._providers().filter((p) =>
                p.id !== 'local' && p.has_key && !recProviders.some((r) => r.id === p.id));
            const localCount = this._localLaneModels().length;

            // Selected provider: sticky pick, else the first keyed one.
            let sel = this.state.simpleSel;
            if (!sel || !keyed.some((p) => p.id === sel)) sel = keyed.length ? keyed[0].id : null;
            this.state.simpleSel = sel;

            // Fallback default: the one OTHER keyed provider with
            // recommendations, when there is exactly one — visible and
            // changeable, never a hidden guess.
            const fbOptions = keyed.filter((p) => p.id !== sel);
            let fb = this.state.simpleFb;
            if (fb === undefined) fb = fbOptions.length === 1 ? fbOptions[0].id : '';
            if (fb && (fb === sel || !keyed.some((p) => p.id === fb))) fb = '';
            this.state.simpleFb = fb;

            const results = this.state.simpleResults || {};
            const applying = !!this.state.simpleApplying;

            let setup;
            if (!((this.opts.recommendations || {}).providers)) {
                setup = `
                    <div class="mp-empty mp-prov-empty">
                        The recommendations file didn't load — route per role in Advanced instead.
                    </div>`;
            } else if (!keyed.length && keyedNoRec.length) {
                setup = `
                    <div class="mp-empty mp-prov-empty">
                        Your keyed provider${keyedNoRec.length === 1 ? '' : 's'}
                        (${keyedNoRec.map((p) => esc(p.id)).join(', ')}) ha${keyedNoRec.length === 1 ? 's' : 've'}
                        no recommendations yet — route per role in Advanced, or add a key for one of:
                        ${recProviders.map((p) => esc(p.rec.label || p.id)).join(', ')}.
                        <button class="cseries-btn" data-mp-open-keys>Open Keys</button>
                    </div>`;
            } else if (!keyed.length) {
                setup = `
                    <div class="mp-empty mp-prov-empty">
                        No provider keys yet. Add a key in the Keys app — or run models on your own
                        machines and route them in Advanced.
                        <button class="cseries-btn" data-mp-open-keys>Open Keys</button>
                    </div>`;
            } else {
                const plan = this._planRows(sel);
                const loading = plan.some((r) => r.status === 'loading');
                const applied = Object.keys(results).length > 0;
                const okCount = Object.values(results).filter((r) => r.ok).length;
                const planRow = (r) => {
                    const res = results[r.role];
                    const mark = res
                        ? (res.ok
                            ? (res.local
                                ? '✓ runs on your machine' + (res.effDropped ? ' (reasoning effort doesn\'t apply locally)' : '')
                                : '✓ set')
                            : '✗ ' + esc(res.detail))
                        : r.status === 'served' ? 'served on your key'
                        : r.status === 'loading' ? 'checking the catalog…'
                        : r.status === 'unlisted' ? 'not seen in the catalog — will apply as typed (unverified)'
                        : 'catalog unreachable — will apply unverified';
                    return `
                        <tr class="${res ? (res.ok ? 'mp-plan-ok' : 'mp-plan-err') : ''}">
                            <td>${esc(ROLE_LABELS[r.role] || r.role)}</td>
                            <td><span class="mp-id">${esc(r.model)}</span>${r.effort ? ` <span class="label-sys">thinks: ${esc(r.effort)}</span>` : ''}</td>
                            <td class="label-sys">${res ? mark : esc(r.why || '') + (r.status === 'served' ? '' : ' — ' + mark)}</td>
                        </tr>`;
                };
                const recMeta = this.opts.recommendations || {};
                setup = `
                    <div class="mp-simple-provs">
                        ${keyed.map((p) => `
                            <button class="mp-prov-card ${p.id === sel ? 'active' : ''}" data-mp-simple-prov="${esc(p.id)}"${applying ? ' disabled' : ''}>
                                <span class="mp-prov-name">${esc(p.rec.label || p.id)}</span>
                                <span class="label-sys">key in place</span>
                            </button>`).join('')}
                        ${keyless.map((p) => `
                            <button class="mp-prov-card mp-prov-dim" data-mp-open-keys title="No ${esc(p.id)} key stored yet">
                                <span class="mp-prov-name">${esc(p.rec.label || p.id)}</span>
                                <span class="label-sys">add key in Keys</span>
                            </button>`).join('')}
                        ${keyedNoRec.map((p) => `
                            <span class="mp-prov-card mp-prov-dim" title="${esc(noRec[p.id] || 'No recommendations for this provider yet — route it in Advanced.')}">
                                <span class="mp-prov-name">${esc(p.id)}</span>
                                <span class="label-sys">key in place — route in Advanced</span>
                            </span>`).join('')}
                        ${localCount ? `
                            <span class="mp-prov-card mp-prov-dim" title="${esc(noRec.local || '')}">
                                <span class="mp-prov-name">your machines</span>
                                <span class="label-sys">${localCount} model${localCount === 1 ? '' : 's'} — route in Advanced</span>
                            </span>` : ''}
                    </div>
                    <div class="label-sys mp-simple-plan-head">${esc((((recMeta.providers || {})[sel]) || {}).plan || '')}</div>
                    <table class="mp-table mp-plan-table">
                        <thead><tr><th>When</th><th>Model</th><th></th></tr></thead>
                        <tbody>${plan.map(planRow).join('')}</tbody>
                    </table>
                    <div class="mp-simple-actions">
                        ${fbOptions.length ? `
                        <label class="mp-role-field" title="If ${esc(sel)} errors mid-call, each role retries once on this provider's recommended model for that role — and the answer says so.">backup provider
                            <select class="cseries-select" data-mp-simple-fb${applying ? ' disabled' : ''}>
                                <option value="">none</option>
                                ${fbOptions.map((p) => `<option value="${esc(p.id)}"${p.id === fb ? ' selected' : ''}>${esc(p.rec.label || p.id)}</option>`).join('')}
                            </select>
                        </label>` : ''}
                        <button class="cseries-btn mp-simple-apply" data-mp-simple-apply ${applying || loading ? 'disabled' : ''}>
                            ${applying ? 'Setting routes…' : loading ? 'Checking the catalog…' : 'Use this routing'}
                        </button>
                        ${applied && !applying ? `<span class="label-sys">${okCount}/${Object.keys(results).length} roles routed — the overlay above is live</span>` : ''}
                    </div>
                    <div class="label-sys mp-simple-note">
                        Every route this writes is visible and editable in Advanced — nothing is hidden in code.
                        Embedding, images and coding engines keep their own sections there.
                    </div>`;
            }

            // ORDER: setup FIRST, "what answers when" LAST.
            // The old order opened on a status table a new user could not act
            // on, and buried the one button that does the work below it. This
            // reads as a setup checklist: thinking, memory, seeing, hearing —
            // every model type the host needs, each with its own state and one
            // action — and the status overlay is the receipt at the bottom.
            el.innerHTML = `
                <div class="mp-section-head card-label label-sys">Set up your host</div>
                <div class="label-sys mp-help">Four things your host needs. Each one is
                    independent — set up what you want now, come back for the rest.</div>

                <div class="mp-setup-step" data-mp-step="thinking">
                    <div class="mp-setup-head"><span class="mp-setup-name">Thinking</span></div>
                    <p class="mp-setup-why">Your host needs a model to think with. Pick a provider
                        you hold a key for and one click routes both jobs — talking and
                        reviewing builds — to a researched recommendation on it.</p>
                    ${setup}
                </div>

                ${this._simpleMemoryHtml()}
                ${this._simpleSeeingHtml()}
                ${this._simpleHearingHtml()}

                <div class="mp-section-head card-label label-sys">What answers when</div>
                <div class="label-sys mp-help">If a model fails you see the error — your host
                    never quietly swaps in a different one.</div>
                <div class="mp-overlay">${this._overlayHtml()}</div>`;

            this._bindKeysLinks(el);
            this._bindSimpleCapabilities(el);
            el.querySelectorAll('[data-mp-simple-prov]').forEach((btn) => {
                btn.addEventListener('click', () => {
                    this.state.simpleSel = btn.dataset.mpSimpleProv;
                    this.state.simpleFb = undefined;   // re-derive for the new primary
                    this.state.simpleResults = {};
                    this._renderSimple();
                });
            });
            const fbSel = el.querySelector('[data-mp-simple-fb]');
            if (fbSel) fbSel.addEventListener('change', () => {
                this.state.simpleFb = fbSel.value || '';
                this._renderSimple();
            });
            const applyBtn = el.querySelector('[data-mp-simple-apply]');
            if (applyBtn) applyBtn.addEventListener('click', () => {
                if (!this.state.simpleApplying && this.state.simpleSel) this._applyPlan(this.state.simpleSel);
            });
        }

        // Handlers for the Memory / Seeing / Hearing steps. Each write goes
        // through the SAME endpoint the Advanced surface uses, so a Simple
        // pick is an ordinary visible route, never a second hidden lane.
        _bindSimpleCapabilities(el) {
            const q = (s) => el.querySelector(s);

            // ── Memory ──
            const localBtn = q('[data-mp-simple-embed-local]');
            if (localBtn) localBtn.addEventListener('click', () => {
                // The walkthrough's Network step explains what a bastion is and
                // walks the user through standing one up — that is where "free
                // on your machine" has to land, not a bare settings pane.
                this._openAppView('getting-started', 'network');
            });
            const provBtn = q('[data-mp-simple-embed-provider]');
            if (provBtn) provBtn.addEventListener('click', () => {
                this.state.simpleEmbedOpen = !this.state.simpleEmbedOpen;
                this._renderSimple();
            });
            const eProv = q('[data-mp-simple-embed-prov]');
            if (eProv) eProv.addEventListener('change', () => {
                this.state.simpleEmbedProv = eProv.value;
                this._renderSimple();
            });
            const eApply = q('[data-mp-simple-embed-apply]');
            if (eApply) eApply.addEventListener('click', async () => {
                const sel = q('[data-mp-simple-embed-model]');
                const st = q('[data-mp-simple-embed-status]');
                const model = sel && sel.value;
                if (!model) { if (st) st.textContent = 'pick a model'; return; }
                const provider = (q('[data-mp-simple-embed-prov]') || {}).value;
                eApply.disabled = true;
                if (st) st.textContent = 'switching…';
                const res = await fetchJSON('/api/embedding/use-provider', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ provider, model }),
                });
                eApply.disabled = false;
                if (res && !res.error) {
                    this.state.simpleEmbedOpen = false;
                    if (typeof this.opts.onChange === 'function') this.opts.onChange('embedding', res);
                    await this.reload();
                } else if (st) {
                    st.textContent = (res && res.error) || 'switch failed';
                }
            });

            // ── Seeing ──
            el.querySelectorAll('[data-mp-simple-see]').forEach((btn) => {
                btn.addEventListener('click', () => {
                    const role = btn.dataset.mpSimpleSee;
                    this.state.simpleSeeOpen = this.state.simpleSeeOpen === role ? null : role;
                    this._renderSimple();
                });
            });
            el.querySelectorAll('[data-mp-simple-see-prov]').forEach((sel) => {
                sel.addEventListener('change', () => {
                    const role = sel.dataset.mpSimpleSeeProv;
                    this.state.simpleSeeProv = this.state.simpleSeeProv || {};
                    this.state.simpleSeeProv[role] = sel.value;
                    this._renderSimple();
                });
            });
            el.querySelectorAll('[data-mp-simple-see-apply]').forEach((btn) => {
                btn.addEventListener('click', async () => {
                    const role = btn.dataset.mpSimpleSeeApply;
                    const sel = el.querySelector(`[data-mp-simple-see-model="${role}"]`);
                    const st = el.querySelector(`[data-mp-simple-see-status="${role}"]`);
                    const provider = (el.querySelector(`[data-mp-simple-see-prov="${role}"]`) || {}).value;
                    const model = sel && sel.value;
                    if (!model) { if (st) st.textContent = 'pick a model'; return; }
                    btn.disabled = true;
                    if (st) st.textContent = 'setting…';
                    // No base_url: the endpoint box is gone. An authenticated
                    // provider plus a model it says can do this is the whole
                    // decision now.
                    const res = await fetchDetail('/api/inference/routing', {
                        method: 'PUT', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ role, provider, model }),
                    });
                    btn.disabled = false;
                    if (!res.ok) { if (st) st.textContent = res.detail || 'could not set'; return; }
                    this.state.routing[role] = { provider, model };
                    this.state.simpleSeeOpen = null;
                    await this.reload();
                });
            });

            // ── Hearing ──
            const voiceSel = q('[data-mp-simple-voice]');
            if (voiceSel) voiceSel.addEventListener('change', async () => {
                const st = q('[data-mp-simple-voice-status]');
                voiceSel.disabled = true;
                if (st) st.textContent = 'saving…';
                const res = await fetchDetail('/api/inference/voice', {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ provider: voiceSel.value || null }),
                });
                voiceSel.disabled = false;
                if (!res.ok) { if (st) st.textContent = res.detail || 'could not save'; return; }
                await this.reload();
            });
        }

        // ── Providers + favorites helpers ────────────────────────────────

        // The routing payload's provider list is the ONLY source (backend
        // registry + local) — a new registry row appears here with zero UI
        // changes. Defensive: 'local' is appended if the payload omitted it.
        _providers() {
            const list = (this.state.providers || []).slice();
            if (!list.some((p) => p.id === 'local')) list.push({ id: 'local', has_key: true, embeddings: true });
            return list;
        }

        _providerById(id) {
            return this._providers().find((p) => p.id === id) || null;
        }

        // For a provider whose model calls can run on a host-run sign-in
        // (xAI), from the routing payload (server/routes/inference.
        // _provider_status): the credential that served the last model call,
        // or — before any — the one the next call dials (via_label): "xAI
        // sign-in" or "xAI API key". Plain text; the caller escapes. Empty for
        // every other provider.
        _credentialAnswered(providerId) {
            const p = this._providerById(providerId) || {};
            const ls = p.last_served || null;
            return (ls && ls.label) || p.via_label || '';
        }

        // Provider <option>s. Keyless providers render disabled with the
        // in-option hint — EXCEPT the currently stored one, which stays
        // selectable rather than silently rewriting the user's pick (same rule
        // as the ladder's harness select); the Models tab passes allowKeyless
        // so a keyless provider stays pickable there (its pane is the "add the
        // key in Keys" empty state). A stored provider the registry no longer
        // lists is kept visible, marked (unknown).
        _providerOptionsHtml(selId, opts) {
            const noneLabel = opts && opts.none;
            const includeBlank = !noneLabel && (!opts || opts.blank !== false);
            const allowKeyless = !!(opts && opts.allowKeyless);
            const provs = this._providers();
            let html = includeBlank ? '<option value="">—</option>' : '';
            if (noneLabel) html += `<option value="">${esc(noneLabel)}</option>`;
            html += provs.map((p) => {
                const keyless = !p.has_key;
                const dis = keyless && !allowKeyless && p.id !== selId ? ' disabled' : '';
                const suffix = keyless ? ' — add key in Keys' : '';
                return `<option value="${esc(p.id)}"${p.id === selId ? ' selected' : ''}${dis}>${esc(p.id)}${suffix}</option>`;
            }).join('');
            if (selId && !provs.some((p) => p.id === selId)) {
                html += `<option value="${esc(selId)}" selected>${esc(selId)} (unknown)</option>`;
            }
            return html;
        }

        // Pickable models for one provider: the user's FAVORITES for it (never
        // the full catalog, never typed ids). 'local' is the exception — its
        // options are what the user's machines actually serve (GET
        // /api/inference/local-models, merged with catalog.local_models).
        _modelsForProvider(prov) {
            if (!prov) return [];
            if (prov === 'local') return this._localLaneModels();
            const ids = (this.state.favoritesByProvider || {})[prov] || [];
            const byId = {};
            if (prov === 'openrouter' && this.state.catalog) {
                flatten(this.state.catalog).forEach((m) => { byId[m.id] = m; });
            }
            return ids.map((id) => {
                const m = byId[id];
                return { id, label: m ? (m.name || id) : id };
            });
        }

        _isFavorite(provider, id) {
            return ((this.state.favoritesByProvider || {})[provider] || []).indexOf(id) >= 0;
        }

        // Star/unstar, provider-scoped: POST {provider, model_id}; DELETE
        // /favorites/<id> where <id> is the bare model id for openrouter
        // (legacy flat shape) or "provider::model_id" (URL-encoded) otherwise.
        async _toggleFavorite(provider, modelId) {
            if (!provider || !modelId) return;
            const wasFav = this._isFavorite(provider, modelId);
            const res = wasFav
                ? await fetchJSON('/api/inference/favorites/' + (provider === 'openrouter'
                    ? encodeURI(modelId)
                    : encodeURIComponent(provider + '::' + modelId)), { method: 'DELETE' })
                : await fetchJSON('/api/inference/favorites', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ provider, model_id: modelId }),
                });
            if (!res) return;
            // Trust the response when it carries the maps; patch locally
            // otherwise so the star still flips.
            if (res.by_provider) {
                const merged = Object.assign({}, res.by_provider);
                const flat = res.favorites || [];
                if (flat.length) merged.openrouter = Array.from(new Set([].concat(merged.openrouter || [], flat)));
                this.state.favoritesByProvider = merged;
            } else {
                const map = Object.assign({}, this.state.favoritesByProvider);
                const list = (map[provider] || []).slice();
                const i = list.indexOf(modelId);
                if (wasFav && i >= 0) list.splice(i, 1);
                if (!wasFav && i < 0) list.push(modelId);
                map[provider] = list;
                this.state.favoritesByProvider = map;
            }
            if (typeof this.opts.onChange === 'function') this.opts.onChange('favorites', this.state.favoritesByProvider);
            this._renderModels();
        }

        // Keys live in the Keys app — every key-hint in this widget lands here.
        _openKeys() {
            // Apps are same-origin iframes; the shell exposes AppRunner globally.
            try {
                if (window.parent && window.parent.AppRunner) {
                    window.parent.AppRunner.launch('keys');
                    return true;
                }
            } catch (e) { /* cross-origin / not framed */ }
            // Fallback: ask the shell to open it via postMessage (app-runner:302).
            try { window.parent.postMessage({ type: 'cseries:open-app', app_id: 'keys' }, '*'); } catch (e) {}
            return false;
        }

        _bindKeysLinks(container) {
            container.querySelectorAll('[data-mp-open-keys]').forEach((b) => {
                b.addEventListener('click', (ev) => {
                    ev.preventDefault();
                    ev.stopPropagation();
                    this._openKeys();
                });
            });
        }

        // ── Models tab ───────────────────────────────────────────────────
        // Provider selector at top. openrouter → the searchable catalog table
        // (search, facets, sortable columns, browse-all toggle). Any other
        // registry provider → its own /direct-models/<provider> list. local →
        // the mesh table. Stars write provider-scoped favorites everywhere.

        _renderModels() {
            const provSel = this.root.querySelector('[data-mp-models-provider]');
            if (provSel && !provSel.dataset.bound) {
                provSel.dataset.bound = '1';
                provSel.addEventListener('change', () => {
                    this.state.modelsProvider = provSel.value || 'openrouter';
                    this.state.expanded = null;
                    this._renderModels();
                });
            }
            if (provSel) {
                // Keyless providers stay pickable HERE — selecting one shows
                // the "add the key in Keys" empty state (routing rows disable
                // them instead).
                provSel.innerHTML = this._providerOptionsHtml(this.state.modelsProvider, { blank: false, allowKeyless: true });
            }

            const isOr = this.state.modelsProvider === 'openrouter';
            const controls = this.root.querySelector('[data-mp-catalog-controls]');
            const wrap = this.root.querySelector('[data-mp-catalog-wrap]');
            const body = this.root.querySelector('[data-mp-models-body]');
            const note = this.root.querySelector('[data-mp-models-prov-note]');
            if (controls) controls.hidden = !isOr;
            if (wrap) wrap.hidden = !isOr;
            if (body) body.hidden = isOr;
            if (note) {
                const p = this._providerById(this.state.modelsProvider);
                note.textContent = isOr ? ''
                    : this.state.modelsProvider === 'local' ? 'models your machines serve over the mesh'
                    : (p && !p.has_key) ? 'no key stored for this provider yet'
                    : 'this provider’s own model list, under your '
                        + ((p && p.via_label) || 'key');
            }

            if (isOr) { this._renderModelsOpenrouter(); return; }
            if (this.state.modelsProvider === 'local') { this._renderModelsLocal(); return; }
            this._renderModelsDirect(this.state.modelsProvider);
        }

        // The OpenRouter catalog DEFAULTS to the user's favorites. An active
        // search OR the "Browse all models" toggle reveals the full catalog so
        // the user can find + star new models. Default state never renders the
        // whole list.
        _showingAll() {
            return !this.state.favoritesView || !!(this.state.filters.search || '').trim();
        }

        _renderModelsOpenrouter() {
            const c = this.state.catalog;
            const tbody = this.root.querySelector('[data-mp-tbody]');
            if (!tbody) return;

            // Keep the Browse-all toggle label in sync with the current view.
            const browseBtn = this.root.querySelector('[data-mp-browse-toggle]');
            if (browseBtn) {
                browseBtn.textContent = this.state.favoritesView ? 'Browse all models' : 'Show favorites only';
                browseBtn.classList.toggle('active', !this.state.favoritesView);
            }

            if (!c) {
                tbody.innerHTML = '<tr><td colspan="7" class="mp-empty">Loading…</td></tr>';
                return;
            }
            const all = flatten(c).filter((m) => m.provider === 'openrouter');
            this._populateFacets(all);

            // Favorites is the default scope. Browse-all (or a search) widens it.
            const showAll = this._showingAll();
            const favCount = ((this.state.favoritesByProvider || {}).openrouter || []).length;
            const scoped = showAll ? all : all.filter((m) => this._isFavorite('openrouter', m.id));
            const rows = this._filterAndSort(scoped);

            // Empty favorites, default view → a clear empty state with the affordance.
            if (!showAll && all.length > 0 && favCount === 0) {
                tbody.innerHTML = `<tr><td colspan="7" class="mp-empty">No favorites yet — <button class="cseries-btn ghost mp-browse-inline" data-mp-browse-toggle>Browse all models</button> and star the ones you use.</td></tr>`;
                this.root.querySelector('[data-mp-count]').textContent = '0 favorites';
                tbody.querySelectorAll('[data-mp-browse-toggle]').forEach((b) => {
                    b.addEventListener('click', () => { this.state.favoritesView = false; this._renderModels(); });
                });
                return;
            }

            if (rows.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" class="mp-empty">No models match.</td></tr>';
                this.root.querySelector('[data-mp-count]').textContent = `0 of ${scoped.length} models`;
                return;
            }

            this.root.querySelectorAll('.mp-table th.sortable').forEach((th) => {
                th.classList.toggle('sorted', th.dataset.sort === this.state.sort.col);
                th.classList.toggle('asc',    th.dataset.sort === this.state.sort.col && this.state.sort.dir === 'asc');
                th.classList.toggle('desc',   th.dataset.sort === this.state.sort.col && this.state.sort.dir === 'desc');
            });

            tbody.innerHTML = rows.map((m) => {
                const expanded = this.state.expanded === m.id;
                const fav = this._isFavorite('openrouter', m.id);
                const main = `
                    <tr class="mp-row" data-model-id="${esc(m.id)}">
                        <td class="mp-star-col">
                            <button class="mp-star ${fav ? 'on' : ''}" data-mp-fav="${esc(m.id)}"
                                    title="${fav ? 'Remove from favorites' : 'Add to favorites'}"
                                    aria-pressed="${fav ? 'true' : 'false'}">${fav ? '★' : '☆'}</button>
                        </td>
                        <td>
                            <div class="mp-name">${esc(m.name)}${validatedPill(m)}</div>
                            <div class="mp-id">${esc(m.id)}</div>
                        </td>
                        <td>${esc(m.vendor)}</td>
                        <td class="num">${formatContext(m.context_length)}</td>
                        <td class="num" title="${esc(priceTitle(m))}">${formatPrice(m)}</td>
                        <td class="num">${formatCreated(m.created)}</td>
                        <td>${esc(m.modality)}</td>
                    </tr>
                `;
                const expand = expanded ? `
                    <tr class="mp-expand"><td colspan="7">
                        <div class="mp-desc">${esc(m.description || '(no description)')}</div>
                        ${this._assignRowHtml(m.id)}
                    </td></tr>
                ` : '';
                return main + expand;
            }).join('');

            // Star toggle — POST/DELETE favorites, update state, re-render.
            tbody.querySelectorAll('.mp-star').forEach((star) => {
                star.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    await this._toggleFavorite('openrouter', star.dataset.mpFav);
                });
            });

            tbody.querySelectorAll('tr.mp-row').forEach((row) => {
                row.addEventListener('click', () => {
                    const mid = row.dataset.modelId;
                    this.state.expanded = this.state.expanded === mid ? null : mid;
                    this._renderModels();
                });
            });

            this._bindAssignButtons(tbody, 'openrouter');

            const denom = this._showingAll() ? all.length : favCount;
            const noun = this._showingAll() ? 'models' : 'favorites';
            this.root.querySelector('[data-mp-count]').textContent = `${rows.length} of ${denom} ${noun}`;
        }

        // Another registry provider: its OWN model list (GET
        // /api/inference/direct-models/<provider> — the provider's /models
        // under the user's key). Keyless → an honest empty state pointing at
        // the Keys app, no fetch fired. Favorites for this provider sit on top.
        _renderModelsDirect(prov) {
            const body = this.root.querySelector('[data-mp-models-body]');
            if (!body) return;
            const p = this._providerById(prov);
            const countEl = this.root.querySelector('[data-mp-count]');

            if (p && !p.has_key) {
                body.innerHTML = `
                    <div class="mp-empty mp-prov-empty">
                        No ${esc(prov)} key stored — add the ${esc(prov)} key in the Keys app, then reload.
                        <button class="cseries-btn ghost" data-mp-open-keys>Open Keys</button>
                    </div>`;
                if (countEl) countEl.textContent = '';
                this._bindKeysLinks(body);
                return;
            }

            this._ensureDirectModels(prov);
            const dm = (this.state.directModels || {})[prov];
            const favIds = ((this.state.favoritesByProvider || {})[prov] || []);

            if (dm == null) {
                body.innerHTML = '<div class="mp-empty">Loading ' + esc(prov) + ' models…</div>';
                if (countEl) countEl.textContent = '';
                return;
            }
            if (dm.error) {
                const authish = /401|403|invalid auth/i.test(String(dm.error));
                body.innerHTML = `<div class="mp-empty mp-prov-empty">${esc(dm.error)}
                    ${authish ? ' <button class="cseries-btn ghost" data-mp-open-keys>Open Keys</button>' : ' Reload to retry.'}
                    <div class="mp-manual-model">
                        <input class="cseries-input mp-manual-fav" placeholder="know the model id? type it — unverified" aria-label="manual model id">
                        <button class="cseries-btn ghost mp-manual-fav-btn">Star for routing</button>
                    </div></div>`;
                if (countEl) countEl.textContent = '';
                this._bindKeysLinks(body);
                // A broken catalog must not make the provider unusable: star a
                // typed id straight from the error pane and it becomes
                // pickable in the Routing rows.
                const favBtn = body.querySelector('.mp-manual-fav-btn');
                const favInput = body.querySelector('.mp-manual-fav');
                if (favBtn && favInput) {
                    favBtn.addEventListener('click', async () => {
                        const id = (favInput.value || '').trim();
                        if (!id) { favInput.focus(); return; }
                        favBtn.disabled = true;
                        await this._toggleFavorite(prov, id);
                        const pane = this.root.querySelector('[data-mp-models-body] .mp-prov-empty');
                        if (pane) {
                            const c = document.createElement('div');
                            c.className = 'label-sys';
                            c.textContent = `starred ${id} — pick it in the Routing tab`;
                            pane.appendChild(c);
                        }
                    });
                }
                return;
            }

            const rows = dm.map((m) => (typeof m === 'string' ? { id: m } : m))
                .filter((m) => m && (m.id || m.name))
                .map((m) => ({
                    id: m.id || m.name,
                    name: (m.name && m.name !== m.id) ? m.name : '',
                    size: m.size || m.parameter_size || '',
                    context_length: m.context_length || 0,
                    input_per_token: m.input_per_token,
                    output_per_token: m.output_per_token,
                    // Together only: whether the provider actually serves this
                    // model right now. needs_endpoint rows are still listed and
                    // still starrable — the backend marks, never filters,
                    // because endpoint ids and model ids do not always match.
                    serving: m.serving || '',
                    needs_endpoint: !!m.needs_endpoint,
                    validated: !!m.validated,
                }));
            const favRows = rows.filter((m) => favIds.indexOf(m.id) >= 0);
            const needEp = rows.filter((m) => m.needs_endpoint).length;

            const rowHtml = (m) => {
                const fav = this._isFavorite(prov, m.id);
                const expanded = this.state.expanded === prov + '::' + m.id;
                return `
                    <tr class="mp-row" data-model-id="${esc(m.id)}">
                        <td class="mp-star-col">
                            <button class="mp-star ${fav ? 'on' : ''}" data-mp-fav="${esc(m.id)}"
                                    title="${fav ? 'Remove from favorites' : 'Add to favorites'}"
                                    aria-pressed="${fav ? 'true' : 'false'}">${fav ? '★' : '☆'}</button>
                        </td>
                        <td><div class="mp-name">${esc(m.id)}${validatedPill(m)}${m.needs_endpoint
                            ? ' <span class="cseries-pill warn mp-needs-ep" title="The provider is not serving this model on demand. Calling it fails unless you first create and start a dedicated endpoint for it at the provider.">needs endpoint</span>'
                            : (m.serving === 'dedicated'
                                ? ' <span class="cseries-pill info" title="Served by your own dedicated endpoint, currently started.">dedicated</span>'
                                : '')}</div></td>
                        <td>${esc(m.name || '—')}</td>
                        <td class="num">${m.context_length ? formatContext(m.context_length) : esc(m.size || '—')}</td>
                        <td class="num" title="${esc(priceTitle(m))}">${formatPrice(m)}</td>
                    </tr>
                    ${expanded ? `<tr class="mp-expand"><td colspan="5">${this._assignRowHtml(m.id)}</td></tr>` : ''}
                `;
            };

            body.innerHTML = `
                ${favRows.length ? `
                <div class="mp-favstrip">
                    <span class="label-sys">favorites:</span>
                    ${favRows.map((m) => `<span class="cseries-pill info">${esc(m.id)}</span>`).join('')}
                </div>` : ''}
                ${rows.length ? `
                <div class="mp-table-wrap">
                    <table class="mp-table">
                        <thead><tr>
                            <th class="mp-star-col"></th><th>Model</th><th>Name</th>
                            <th class="num">Context</th>
                            <th class="num" title="Provider list price, $ per 1M tokens (input / output) — where the provider publishes it">Price /M</th>
                        </tr></thead>
                        <tbody>${rows.map(rowHtml).join('')}</tbody>
                    </table>
                </div>` : `
                <div class="mp-empty">${esc(prov)} returned no models.</div>`}
            `;
            if (countEl) {
                countEl.textContent = `${rows.length} models · ${favRows.length} favorites`
                    + (needEp ? ` · ${needEp} need a dedicated endpoint` : '');
            }

            body.querySelectorAll('.mp-star').forEach((star) => {
                star.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    await this._toggleFavorite(prov, star.dataset.mpFav);
                });
            });
            body.querySelectorAll('tr.mp-row').forEach((row) => {
                row.addEventListener('click', () => {
                    const key = prov + '::' + row.dataset.modelId;
                    this.state.expanded = this.state.expanded === key ? null : key;
                    this._renderModels();
                });
            });
            this._bindAssignButtons(body, prov);
        }

        // local: the mesh table — what the user's machines actually serve.
        _renderModelsLocal() {
            const body = this.root.querySelector('[data-mp-models-body]');
            if (!body) return;
            const countEl = this.root.querySelector('[data-mp-count]');
            const rows = this._localLaneModels();

            if (!rows.length) {
                body.innerHTML = `
                    <div class="mp-empty mp-prov-empty">No local models yet. Your host discovers models served by machines on your private network and lists them here.</div>`;
                if (countEl) countEl.textContent = '';
                return;
            }

            body.innerHTML = `
                <div class="mp-table-wrap">
                    <table class="mp-table">
                        <thead><tr>
                            <th class="mp-star-col"></th><th>Model</th><th>Served by</th>
                        </tr></thead>
                        <tbody>
                        ${rows.map((m) => {
                            const fav = this._isFavorite('local', m.id);
                            const expanded = this.state.expanded === 'local::' + m.id;
                            return `
                            <tr class="mp-row" data-model-id="${esc(m.id)}">
                                <td class="mp-star-col">
                                    <button class="mp-star ${fav ? 'on' : ''}" data-mp-fav="${esc(m.id)}"
                                            title="${fav ? 'Remove from favorites' : 'Add to favorites'}"
                                            aria-pressed="${fav ? 'true' : 'false'}">${fav ? '★' : '☆'}</button>
                                </td>
                                <td><div class="mp-name">${esc(m.id)}</div></td>
                                <td>${esc(m.bastion || 'mesh')}</td>
                            </tr>
                            ${expanded ? `<tr class="mp-expand"><td colspan="3">${this._assignRowHtml(m.id)}</td></tr>` : ''}`;
                        }).join('')}
                        </tbody>
                    </table>
                </div>`;
            if (countEl) countEl.textContent = `${rows.length} local models`;

            body.querySelectorAll('.mp-star').forEach((star) => {
                star.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    await this._toggleFavorite('local', star.dataset.mpFav);
                });
            });
            body.querySelectorAll('tr.mp-row').forEach((row) => {
                row.addEventListener('click', () => {
                    const key = 'local::' + row.dataset.modelId;
                    this.state.expanded = this.state.expanded === key ? null : key;
                    this._renderModels();
                });
            });
            this._bindAssignButtons(body, 'local');
        }

        // The expand-row assign control, shared by every provider's table:
        // assign {provider: <the table's provider>, model} to a text role via
        // the standard routing PUT, or to the opencode harness (any model codes
        // through opencode).
        _assignRowHtml(modelId) {
            return `
                <div class="mp-assign">
                    <span class="label-sys">Assign to role:</span>
                    ${ROLES.map((r) => `<button class="mp-assign-btn" data-role="${r}" data-model="${esc(modelId)}">${esc(ROLE_LABELS[r] || r)}</button>`).join('')}
                    <button class="mp-assign-btn mp-assign-coding" data-model="${esc(modelId)}" title="Any model codes via the opencode harness">coding · opencode</button>
                </div>`;
        }

        _bindAssignButtons(container, provider) {
            container.querySelectorAll('.mp-assign-btn[data-role]').forEach((btn) => {
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    const role = btn.dataset.role;
                    const model = btn.dataset.model;
                    const result = await fetchJSON('/api/inference/routing', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ role, provider, model }),
                    });
                    if (result) {
                        this.state.routing[role] = { provider, model };
                        // Keep effective in sync so the Routing tab shows the
                        // pick without a reload.
                        if (!this.state.effective) this.state.effective = {};
                        this.state.effective[role] = { provider, model, model_id: model };
                        delete this.state.roleSel[role];
                        btn.textContent = 'assigned';
                        btn.classList.add('ok');
                        if (typeof this.opts.onChange === 'function') this.opts.onChange('routing', this.state.routing);
                        setTimeout(() => this._renderModels(), 700);
                    } else {
                        btn.textContent = 'failed';
                        btn.classList.add('err');
                    }
                });
            });

            // Coding assign — the "any model codes" leg: a bare model pick codes
            // through the OPENCODE harness by default (fixed harnesses run their
            // own provider's models). Upserts the opencode entry's model in
            // coding_harnesses; other entries and their overrides are preserved.
            container.querySelectorAll('.mp-assign-coding').forEach((btn) => {
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    const model = btn.dataset.model;
                    const list = (this.state.codingHarnesses || []).slice();
                    const oc = list.find((h) => h.id === 'opencode');
                    if (oc) oc.model = model;
                    else list.push({ id: 'opencode', model });
                    const harnesses = list.map((h) => {
                        const entry = { id: h.id };
                        if (h.model) entry.model = h.model;
                        if (h.effort) entry.effort = h.effort;
                        return entry;
                    });
                    const res = await fetchDetail('/api/inference/coding-harnesses', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ harnesses }),
                    });
                    if (res.ok) {
                        // The server's cleaned list, not ours: a stale id it dropped
                        // (a retired harness) must not ride the next save.
                        this.state.codingHarnesses = (res.body && Array.isArray(res.body.coding_harnesses))
                            ? res.body.coding_harnesses : list;
                        btn.textContent = 'assigned → opencode';
                        btn.classList.add('ok');
                        if (typeof this.opts.onChange === 'function') this.opts.onChange('coding-harnesses', harnesses);
                        setTimeout(() => this._renderModels(), 900);
                    } else {
                        // Most likely: opencode isn't connected yet.
                        btn.textContent = 'failed';
                        btn.classList.add('err');
                        btn.title = res.detail || 'connect opencode on your host first';
                    }
                });
            });
        }

        // Lazily fetch a direct provider's OWN model catalog (GET
        // /api/inference/direct-models/<provider>). null in the map = fetch in
        // flight; {error} = the backend's refusal detail (e.g. no key).
        _ensureDirectModels(prov) {
            if (!prov || prov === 'openrouter' || prov === 'local') return;
            this.state.directModels = this.state.directModels || {};
            if (this.state.directModels[prov] !== undefined) return;
            this.state.directModels[prov] = null;
            fetchDetail(`/api/inference/direct-models/${encodeURIComponent(prov)}`)
                .then((res) => {
                    // A 200 with {models: [], error: "…"} is an ERROR, not an
                    // empty catalog: the route never fails the HTTP layer for
                    // an upstream refusal, so body.error must win over an
                    // empty list (this is what hid kimi's 401 behind "kimi
                    // returned no models").
                    this.state.directModels[prov] = res.ok
                        ? ((res.body && res.body.error)
                            ? { error: res.body.error }
                            : ((res.body && res.body.models) || []))
                        : { error: res.detail || prov + ' catalog unavailable' };
                    this._renderTab();  // the routing tab's catalog fallback reads this too
                })
                .catch(() => {
                    this.state.directModels[prov] = { error: prov + ' catalog unavailable' };
                    this._renderTab();
                });
        }

        // Local options = everything the user's machines advertise: the mesh
        // table (GET /api/inference/local-models — [{id, bastion, endpoint}])
        // merged with any pod-local catalog tags (catalog.local_models). The
        // endpoint is DISPLAY-ONLY — a save sends {provider:'local', model};
        // the backend resolves the serving machine live at call time.
        _localLaneModels() {
            const out = [];
            const seen = new Set();
            (this.state.localModels || []).forEach((m) => {
                if (typeof m === 'string') {
                    if (m && !seen.has(m + '::')) { seen.add(m + '::'); out.push({ id: m, label: m }); }
                    return;
                }
                const id = m.id || m.model || m.name;
                if (!id) return;
                const key = id + '::' + (m.endpoint || '');
                if (seen.has(key)) return;
                seen.add(key);
                out.push({
                    id,
                    label: (m.name || id) + (m.bastion ? ' · ' + m.bastion : ''),
                    endpoint: m.endpoint || '',
                    bastion: m.bastion || '',
                });
            });
            (((this.state.catalog || {}).local_models) || []).forEach((m) => {
                if (m.available === false) return;
                const id = m.id || m.name;
                if (!id || out.some((x) => x.id === id)) return;
                out.push({ id, label: m.name || id });
            });
            return out;
        }

        _roleNote(role, text) {
            const n = this.root.querySelector(`[data-mp-role-note="${role}"]`);
            if (!n) return;
            n.textContent = text;
            n.hidden = !text;
        }

        // The model's own default effort for the route a text row shows,
        // when the server names one (role_effort_options; Anthropic only).
        _roleEffortDefault(role) {
            const o = (this.state.roleEffortOptions || {})[role] || {};
            return o.default || '';
        }

        // What the row's reasoning select offers: {efforts, blank}. Anthropic:
        // the routed model's own levels, and only while the row shows that
        // route (a provider being browsed has no model yet). Every other
        // provider: its static set, as before.
        _roleEffortChoice(role, selProv) {
            if (selProv === 'anthropic') {
                const o = (this.state.roleEffortOptions || {})[role] || {};
                if (o.provider !== 'anthropic') return { efforts: [], blank: 'default' };
                return { efforts: o.efforts || [],
                         blank: o.default ? 'Default · ' + o.default : 'Default' };
            }
            return { efforts: (this.state.roleEfforts || {})[selProv] || [], blank: 'default' };
        }

        // Whether a set effort carries onto a newly picked model. Only while
        // the PROVIDER is unchanged (effort sets are per provider); on
        // Anthropic also only when the new model takes that level — its
        // levels are per model, and the server would refuse the save.
        async _effortCarries(r, provider, model) {
            if (!(r.effort && r.provider === provider)) return false;
            if (provider !== 'anthropic') return true;
            const q = '?provider=' + encodeURIComponent(provider) + '&model=' + encodeURIComponent(model);
            const res = await fetchDetail('/api/inference/effort-options' + q);
            const effs = (res.ok && res.body && res.body.efforts) || [];
            return effs.indexOf(r.effort) >= 0;
        }

        // ── Routing tab: one row per text role ───────────────────────────
        // PROVIDER → MODEL (+ FALLBACK PROVIDER → optional FALLBACK MODEL).
        // The provider list is the backend registry (+ local); model options
        // are the user's favorites for the picked provider (local: the mesh
        // table). A provider pick only swaps the model control — the save
        // fires on the model pick. Fallback saves immediately.

        _renderRouting() {
            const el = this.root.querySelector('[data-mp-routing]');
            if (!el) return;
            const descs = this.state.roleDescriptions || {};
            const eff = this.state.effective || {};

            el.innerHTML = ROLES.map((role) => {
                const r = this.state.routing[role] || {};
                const e = eff[role] || {};
                const desc = descs[role] || '';
                const stored = !!(r.provider && r.model);
                const configured = stored || (!!e.provider && !!(e.model || e.model_id) && !e.unconfigured);
                // The provider this row is showing: a transient browse pick,
                // else the stored route's provider — else the EFFECTIVE route's
                // provider, so the controls reflect what the role actually runs
                // (a legacy-normalized route must not render as a blank row).
                const selProv = Object.prototype.hasOwnProperty.call(this.state.roleSel, role)
                    ? this.state.roleSel[role]
                    : (r.provider || e.provider || '');

                // Model options for the shown provider: the user's favorites
                // when they exist, otherwise the provider's LIVE catalog — a
                // row must never dead-end at "go star something first" (with
                // empty favorites and a blank row no model could be set at all). A stored/effective model that's not in
                // the list stays visible, marked — never silently rewritten.
                let modelCtl;
                if (!selProv) {
                    modelCtl = `<select class="cseries-select mp-role-model" disabled aria-label="${esc(role)} model"><option value="">— pick a provider first —</option></select>`;
                } else {
                    let models = this._modelsForProvider(selProv).slice();
                    const curModel = (r.provider === selProv) ? (r.model || '')
                        : (!r.provider && e.provider === selProv) ? (e.model || e.model_id || '')
                        : '';
                    let note = '';
                    if (!models.length && selProv !== 'local') {
                        if (selProv === 'openrouter') {
                            // The full OpenRouter catalog is already loaded.
                            models = flatten(this.state.catalog || {})
                                .filter((m) => m.provider === 'openrouter')
                                .map((m) => ({ id: m.id, label: (m.name || m.id) }));
                            if (models.length) note = 'full openrouter catalog — star models in the Models tab to curate this list';
                        } else {
                            const dm = (this.state.directModels || {})[selProv];
                            if (dm == null) {
                                this._ensureDirectModels(selProv);  // async — re-renders on arrival
                                note = 'loading the ' + selProv + ' catalog…';
                            } else if (dm.error) {
                                note = String(dm.error) + (/401|403|invalid auth/i.test(String(dm.error)) ? ' — fix the key in the Keys app' : '');
                            } else {
                                models = dm.map((m) => (typeof m === 'string' ? { id: m } : m))
                                    .filter((m) => m && m.id)
                                    .map((m) => ({ id: m.id, label: m.name ? m.id + ' — ' + m.name : m.id }));
                                note = models.length
                                    ? 'full ' + selProv + ' catalog — star models in the Models tab to curate this list'
                                    : selProv + ' returned no models';
                            }
                        }
                    }
                    if (curModel && !models.some((m) => m.id === curModel)) {
                        models.push({ id: curModel, label: curModel + (r.provider === selProv ? ' (not in favorites)' : ' (current)') });
                    }
                    if (!models.length) {
                        modelCtl = `<select class="cseries-select mp-role-model" disabled aria-label="${esc(role)} model"><option value="">—</option></select>
                            <span class="mp-cell-empty label-sys">${esc(note || (selProv === 'local' ? 'no local models yet' : 'star models in the Models tab'))}</span>`
                            + (selProv === 'local' ? '' : `
                            <span class="mp-manual-model">
                                <input class="cseries-input mp-manual-input" data-role="${esc(role)}" placeholder="or type the model id (unverified)" aria-label="${esc(role)} manual model id">
                                <button class="cseries-btn ghost mp-manual-set" data-role="${esc(role)}" data-provider="${esc(selProv)}">Set</button>
                            </span>`);
                    } else {
                        modelCtl = `<select class="cseries-select mp-role-model" data-role="${esc(role)}" data-provider="${esc(selProv)}" aria-label="${esc(role)} model">
                            <option value="">—</option>
                            ${models.map((m) => `<option value="${esc(m.id)}"${m.id === curModel ? ' selected' : ''}>${esc(m.label)}</option>`).join('')}
                        </select>`
                            + (note ? `<span class="mp-cell-empty label-sys">${esc(note)}</span>` : '');
                    }
                }

                // Fallback: same provider list + "none". The fallback MODEL
                // appears once a fallback provider is chosen; blank = the same
                // model id on that provider.
                const fbProv = r.fallback_provider || '';
                const fbDisabled = stored ? '' : 'disabled';
                let fbModelCtl = '';
                if (fbProv) {
                    const fbModels = this._modelsForProvider(fbProv).slice();
                    const curFb = r.fallback_model || '';
                    if (curFb && !fbModels.some((m) => m.id === curFb)) {
                        fbModels.push({ id: curFb, label: curFb + ' (not in favorites)' });
                    }
                    fbModelCtl = `
                        <label class="mp-role-field">fallback model
                            <select class="cseries-select mp-role-fb-model" data-role="${esc(role)}" aria-label="${esc(role)} fallback model">
                                <option value="">same model</option>
                                ${fbModels.map((m) => `<option value="${esc(m.id)}"${m.id === curFb ? ' selected' : ''}>${esc(m.label)}</option>`).join('')}
                            </select>
                        </label>`;
                }

                const effProvider = e.provider || r.provider || '';
                const effModel = e.model || e.model_id || r.model || '';
                const effFbProv = e.fallback_provider || r.fallback_provider || '';
                const effFbModel = e.fallback_model || r.fallback_model || '';
                const effLine = configured
                    ? `<div class="mp-role-eff label-sys">runs: ${esc(effProvider)} · ${esc(effModel)}${effFbProv ? ` — fallback: ${esc(effFbProv)} · ${esc(effFbModel || 'same model')}` : ''}</div>`
                    : '';

                const pill = configured
                    ? '<span class="cseries-pill allow mp-route-src">configured</span>'
                    : '<span class="cseries-pill info mp-route-src">pick a provider + model</span>';
                const reset = configured
                    ? `<button class="cseries-btn ghost mp-route-clear" data-role="${esc(role)}">Reset</button>`
                    : '';

                // Reasoning effort. Rendered ONLY for a provider
                // the gateway actually sends an effort for — role_efforts comes
                // from the server's TEXT_ROLE_EFFORTS, and a provider absent
                // there gets no control at all rather than a knob whose value
                // would be dropped before the wire. Blank = the provider's own
                // default, which is what an unset role has always used.
                // Anthropic: the levels are the routed MODEL's own
                // and the blank names that model's default ("Default · medium").
                const effChoice = this._roleEffortChoice(role, selProv);
                const effOpts = effChoice.efforts;
                const effortCtl = effOpts.length
                    ? `<label class="mp-role-field" title="How hard this role thinks. Blank = the model's own default.">reasoning
                            <select class="cseries-select mp-role-effort" data-role="${esc(role)}" aria-label="${esc(role)} reasoning effort">
                                <option value="">${esc(effChoice.blank)}</option>
                                ${effOpts.map((v) =>
                                    `<option value="${esc(v)}"${(r.effort || '') === v ? ' selected' : ''}>${esc(v)}</option>`).join('')}
                            </select>
                        </label>`
                    : '';

                return `
                    <div class="mp-role" data-role="${esc(role)}">
                        <div class="mp-role-head">
                            <div class="mp-role-id">
                                <div class="mp-grid-role">${esc(ROLE_LABELS[role] || role)}</div>
                                ${desc ? `<div class="mp-grid-desc label-sys">${esc(desc)}</div>` : ''}
                                <div class="mp-grid-meta">${pill}${reset}</div>
                            </div>
                            <div class="mp-role-controls">
                                <label class="mp-role-field">provider
                                    <select class="cseries-select mp-role-provider" data-role="${esc(role)}" aria-label="${esc(role)} provider">${this._providerOptionsHtml(selProv)}</select>
                                </label>
                                <label class="mp-role-field">model
                                    ${modelCtl}
                                </label>
                                <label class="mp-role-field" title="Answers when the primary provider errors or doesn't host the model">fallback provider
                                    <select class="cseries-select mp-role-fb-provider" data-role="${esc(role)}" aria-label="${esc(role)} fallback provider" ${fbDisabled}>${this._providerOptionsHtml(fbProv, { none: 'none' })}</select>
                                </label>
                                ${fbModelCtl}
                                ${effortCtl}
                            </div>
                        </div>
                        ${effLine}
                        <div class="mp-cell-note label-sys" data-mp-role-note="${esc(role)}" hidden></div>
                    </div>`;
            }).join('');

            this._bindKeysLinks(el);

            // A provider pick only swaps the model control — nothing saves
            // until a model is chosen (the pick belongs to ONE provider, so
            // the model never carries across).
            el.querySelectorAll('.mp-role-provider').forEach((sel) => {
                sel.addEventListener('change', () => {
                    const role = sel.dataset.role;
                    if (sel.value) this.state.roleSel[role] = sel.value;
                    else delete this.state.roleSel[role];
                    this._roleNote(role, '');
                    this._renderRouting();
                });
            });

            // The model pick is the save. Blank = reset the role (DELETE).
            el.querySelectorAll('.mp-role-model').forEach((sel) => {
                sel.addEventListener('change', async () => {
                    const role = sel.dataset.role;
                    const provider = sel.dataset.provider;
                    const model = sel.value;
                    sel.disabled = true;
                    if (!model) { await this._resetRole(role); return; }
                    const r = this.state.routing[role] || {};
                    const body = { role, provider, model };
                    if (r.fallback_provider) body.fallback_provider = r.fallback_provider;
                    if (r.fallback_model) body.fallback_model = r.fallback_model;
                    // Carry a set effort across a model change, but only while
                    // the PROVIDER is unchanged (and, on Anthropic, the new
                    // model takes that level) — the server refuses an effort
                    // the new route does not accept, which would turn a model
                    // pick into a 400.
                    if (await this._effortCarries(r, provider, model)) body.effort = r.effort;
                    const res = await fetchDetail('/api/inference/routing', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    });
                    if (!res.ok) {
                        sel.disabled = false;
                        this._roleNote(role, res.detail || 'could not set this route');
                        return;
                    }
                    delete this.state.roleSel[role];
                    await this.reload();   // re-resolves effective + the state pill
                    if (typeof this.opts.onChange === 'function') this.opts.onChange('routing', this.state.routing);
                });
            });

            // The reasoning pick saves the same route with a new effort. It
            // requires a model already chosen (the row has nothing to save
            // onto otherwise), so it re-sends the stored provider/model.
            el.querySelectorAll('.mp-role-effort').forEach((sel) => {
                sel.addEventListener('change', async () => {
                    const role = sel.dataset.role;
                    const r = this.state.routing[role] || {};
                    if (!r.provider || !r.model) {
                        this._roleNote(role, 'pick a provider and model first');
                        this._renderRouting();
                        return;
                    }
                    sel.disabled = true;
                    const body = { role, provider: r.provider, model: r.model };
                    if (r.fallback_provider) body.fallback_provider = r.fallback_provider;
                    if (r.fallback_model) body.fallback_model = r.fallback_model;
                    if (sel.value) body.effort = sel.value;   // blank = provider default
                    const res = await fetchDetail('/api/inference/routing', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    });
                    if (!res.ok) {
                        sel.disabled = false;
                        this._roleNote(role, res.detail || 'could not set the reasoning effort');
                        return;
                    }
                    await this.reload();
                    if (typeof this.opts.onChange === 'function') this.opts.onChange('routing', this.state.routing);
                });
            });

            // Manual model id entry — the escape hatch when a provider's
            // catalog can't load (bad key, spending limit, outage): the user
            // knows the id, the row must still take it.
            // Saves the same {provider, model} route a catalog pick saves;
            // the control says "unverified" because it is.
            el.querySelectorAll('.mp-manual-set').forEach((btn) => {
                btn.addEventListener('click', async () => {
                    const role = btn.dataset.role;
                    const provider = btn.dataset.provider;
                    const input = el.querySelector(`.mp-manual-input[data-role="${role}"]`);
                    const model = ((input && input.value) || '').trim();
                    if (!model) { if (input) input.focus(); return; }
                    btn.disabled = true;
                    const r = this.state.routing[role] || {};
                    const body = { role, provider, model };
                    if (r.fallback_provider) body.fallback_provider = r.fallback_provider;
                    if (r.fallback_model) body.fallback_model = r.fallback_model;
                    // Same carry rule as a catalog pick (see _effortCarries).
                    if (await this._effortCarries(r, provider, model)) body.effort = r.effort;
                    const res = await fetchDetail('/api/inference/routing', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    });
                    if (!res.ok) {
                        btn.disabled = false;
                        this._roleNote(role, res.detail || 'could not set this route');
                        return;
                    }
                    delete this.state.roleSel[role];
                    await this.reload();
                    if (typeof this.opts.onChange === 'function') this.opts.onChange('routing', this.state.routing);
                });
            });

            // Fallback provider saves immediately (the primary must exist —
            // the select is disabled until it does). "none" clears it.
            el.querySelectorAll('.mp-role-fb-provider').forEach((sel) => {
                sel.addEventListener('change', async () => {
                    const role = sel.dataset.role;
                    const r = this.state.routing[role] || {};
                    if (!r.provider || !r.model) return;
                    sel.disabled = true;
                    const fb = sel.value;
                    const body = { role, provider: r.provider, model: r.model };
                    // The primary provider is unchanged here, so a set effort
                    // stays valid — carry it or this save would silently clear
                    // it (the PUT replaces the whole entry).
                    if (r.effort) body.effort = r.effort;
                    if (fb) {
                        body.fallback_provider = fb;
                        if (r.fallback_model) body.fallback_model = r.fallback_model;
                    } else {
                        body.fallback_provider = '';   // blank clears the fallback
                    }
                    const res = await fetchDetail('/api/inference/routing', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    });
                    if (!res.ok) {
                        sel.disabled = false;
                        this._roleNote(role, res.detail || 'could not set the fallback');
                        return;
                    }
                    await this.reload();
                    if (typeof this.opts.onChange === 'function') this.opts.onChange('routing', this.state.routing);
                });
            });

            // Fallback model: blank = "same model" (the field is omitted).
            el.querySelectorAll('.mp-role-fb-model').forEach((sel) => {
                sel.addEventListener('change', async () => {
                    const role = sel.dataset.role;
                    const r = this.state.routing[role] || {};
                    if (!r.provider || !r.model || !r.fallback_provider) return;
                    sel.disabled = true;
                    const body = {
                        role, provider: r.provider, model: r.model,
                        fallback_provider: r.fallback_provider,
                    };
                    // Same reason as the fallback-provider save above: the PUT
                    // replaces the whole entry, so an unsent effort is a
                    // cleared effort.
                    if (r.effort) body.effort = r.effort;
                    if (sel.value) body.fallback_model = sel.value;
                    const res = await fetchDetail('/api/inference/routing', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    });
                    if (!res.ok) {
                        sel.disabled = false;
                        this._roleNote(role, res.detail || 'could not set the fallback model');
                        return;
                    }
                    await this.reload();
                    if (typeof this.opts.onChange === 'function') this.opts.onChange('routing', this.state.routing);
                });
            });

            el.querySelectorAll('.mp-route-clear').forEach((btn) => {
                btn.addEventListener('click', () => this._resetRole(btn.dataset.role));
            });

            this._renderLocalLane();
            this._renderMatrix();
            this._renderProtocols();
            this._renderEmbedding();
            this._renderImageRoles();
        }

        async _resetRole(role) {
            await fetchJSON(`/api/inference/routing/${role}`, { method: 'DELETE' });
            delete this.state.routing[role];
            delete this.state.roleSel[role];
            await this.reload();
            if (typeof this.opts.onChange === 'function') this.opts.onChange('routing', this.state.routing);
        }

        // Image generation picker — the 'image_gen' role routes through the
        // user's OWN provider+model (no vendor default). There is no 'vision'
        // role: the Host model reads images itself. Options come from
        // the user's OpenRouter favorites + local models; a BYO OpenAI-style
        // endpoint can be set for a custom image API. Writes the standard
        // routing store ({role, provider, model, base_url?}).
        _renderImageRoles() {
            const el = this.root.querySelector('[data-mp-image]');
            if (!el) return;
            const eff = this.state.effective || {};
            const local = this._localLaneModels();
            const keyed = this._providers().filter((p) => p.id !== 'local' && p.has_key);

            // CAPABILITY-FILTERED options. This used to offer the
            // user's OpenRouter favorites plus every local model — neither list
            // has anything to do with whether a model can see or draw — and a
            // free-text "BYO endpoint" box beside it. What a model can do with
            // images has to be known, and the pick is just a model that fits
            // from an authenticated provider. So: one optgroup per keyed provider, containing only the
            // models that provider's catalog DECLARES can do this role, read
            // server-side from the catalog's modality lists. Local models keep
            // their own group — the mesh table carries no modality data, so
            // they are offered but marked unverified rather than hidden (a
            // machine the user set up themselves is a claim we don't override).
            const options = (role, selProv, selId) => {
                let groups = '';
                let pending = false;
                keyed.forEach((p) => {
                    this._ensureCapableModels(role, p.id);
                    const list = (this.state.capable || {})[role + '::' + p.id];
                    if (list === undefined || list === null) { pending = true; return; }
                    if (list.error) {
                        groups += `<optgroup label="${esc(p.id)} — ${esc(list.error)}"></optgroup>`;
                        return;
                    }
                    if (!list.length) {
                        groups += `<optgroup label="${esc(p.id)} — no model of theirs can do this"></optgroup>`;
                        return;
                    }
                    groups += `<optgroup label="${esc(p.id)}">`
                        + list.map((m) => `<option value="${esc(p.id)}::${esc(m.id)}"${selProv === p.id && m.id === selId ? ' selected' : ''}>${esc(m.name || m.id)}${m.verified ? '' : ' (capability inherited)'}</option>`).join('')
                        + '</optgroup>';
                });
                if (local.length) {
                    groups += '<optgroup label="Your machines (capability not published)">'
                        + local.map((m) => `<option value="local::${esc(m.id)}"${selProv === 'local' && m.id === selId ? ' selected' : ''}>${esc(m.label)}</option>`).join('')
                        + '</optgroup>';
                }
                const first = pending
                    ? '<option value="">— checking which models can do this… —</option>'
                    : '<option value="">— not set —</option>';
                return first + groups;
            };

            const IMG_ROLES = [
                { role: 'image_gen', label: 'Image generation',
                  desc: 'The host crafts the prompt and sends it here to generate images. Only models whose provider says they can OUTPUT an image are listed.' },
            ];
            el.innerHTML = IMG_ROLES.map(({ role, label, desc }) => {
                const e = eff[role] || {};
                const r = this.state.routing[role] || {};
                const curProv = e.provider || r.provider || '';
                const cur = e.model || e.model_id || r.model || '';
                // A route stored by an OLDER build may still carry a BYO
                // base_url. It is shown so the user can SEE what their host is
                // actually calling — an endpoint that silently persisted while
                // the box that set it disappeared would be worse than the box.
                // Re-Setting the role drops it (the PUT sends no base_url).
                const legacyBase = r.base_url || '';
                return `
                    <div class="mp-route-card mp-image-card" data-role="${esc(role)}">
                        <div class="mp-route-card-head">
                            <span class="mp-route-role">${esc(label)}</span>
                            <span class="mp-route-model">${cur ? esc((curProv ? curProv + ' · ' : '') + cur) : 'not set'}</span>
                        </div>
                        <div class="mp-route-desc label-sys">${esc(desc)}</div>
                        <div class="mp-image-controls">
                            <select class="cseries-select mp-image-model" data-role="${esc(role)}">${options(role, curProv, cur)}</select>
                            <button class="cseries-btn mp-image-set" data-role="${esc(role)}">Set</button>
                            <span class="label-sys mp-image-status" data-role="${esc(role)}"></span>
                        </div>
                        ${legacyBase ? `<div class="mp-cell-note label-sys">Calling a custom endpoint set by an earlier version: <span class="mp-id">${esc(legacyBase)}</span> — pick a model above and Set to route through your provider instead.</div>` : ''}
                    </div>`;
            }).join('');

            el.querySelectorAll('.mp-image-set').forEach((btn) => {
                btn.addEventListener('click', async () => {
                    const role = btn.dataset.role;
                    const sel = el.querySelector(`.mp-image-model[data-role="${role}"]`);
                    const status = el.querySelector(`.mp-image-status[data-role="${role}"]`);
                    const val = sel.value;
                    if (!val) {   // blank = clear the role
                        await fetchJSON(`/api/inference/routing/${role}`, { method: 'DELETE' });
                        delete this.state.routing[role];
                        await this.reload();
                        this._renderImageRoles();
                        return;
                    }
                    const sep = val.indexOf('::');
                    const provider = val.slice(0, sep);
                    const model = val.slice(sep + 2);
                    // No base_url: an authenticated provider plus a model its
                    // own catalog says can do this IS the whole decision now.
                    // Sending none also clears a legacy endpoint on re-Set.
                    const res = await fetchDetail('/api/inference/routing', {
                        method: 'PUT', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ role, provider, model }),
                    });
                    if (!res.ok) { status.textContent = res.detail || 'could not set'; return; }
                    this.state.routing[role] = { provider, model };
                    await this.reload();
                    this._renderImageRoles();
                });
            });
        }

        // ── Local models — the host's LOCAL MODEL TABLE ─────────────────
        // Primary source: GET /api/inference/local-models (host PG —
        // network_bastion_heartbeat.models via mastercode.model_backends).
        // The table stays empty until bastions advertise their served models;
        // catalog.local_models is kept as a secondary source for the legacy
        // pod-local shape. A role picks these via provider 'local'.
        _renderLocalLane() {
            const el = this.root.querySelector('[data-mp-local]');
            if (!el) return;
            const table = (this.state.localModels || [])
                .map((m) => ({ id: m.id, note: m.bastion || '' }));
            const legacy = ((this.state.catalog && this.state.catalog.local_models) || [])
                .filter((m) => m.available !== false)
                .map((m) => ({ id: m.id, note: '' }));
            const locals = table.length ? table : legacy;
            const models = locals.length ? `
                    <div class="label-sys mp-help">${locals.length} model${locals.length === 1 ? '' : 's'} on your own network — pick provider <strong>local</strong> to use them, free.</div>
                    <div class="mp-local-chips">
                        ${locals.map((m) => `<span class="cseries-pill info">${esc(m.id)}${m.note ? esc(' · ' + m.note) : ''}</span>`).join('')}
                    </div>` : `
                <div class="mp-local-pending">
                    <span class="cseries-pill info">no local models yet</span>
                    <span class="label-sys">Add a model server below, or run a bastion on a machine that serves models.</span>
                </div>`;
            el.innerHTML = models + this._localServersHtml();
            this._wireLocalServers(el);
        }

        // Model servers the owner names by address (Ollama, llama.cpp, vLLM
        // on their network): GET/PUT /api/inference/local-endpoints. Each is
        // asked live what it serves; its models join the table above.
        _localServersHtml() {
            const info = this.state.localEndpoints || { endpoints: [] };
            const rows = (info.endpoints || []).map((e, i) => `
                <div class="mp-local-server">
                    <code>${esc(e.url)}</code>
                    <span class="label-sys">${e.reachable
                        ? esc(`${e.models.length} model${e.models.length === 1 ? '' : 's'}`)
                        : 'not answering'}</span>
                    <button type="button" class="cseries-btn small" data-mp-server-remove="${i}">Remove</button>
                </div>`).join('');
            return `
                <div class="mp-local-servers">
                    <div class="label-sys mp-help">Model servers on your network</div>
                    ${rows}
                    <form class="mp-local-server-add" data-mp-server-add>
                        <input type="url" required placeholder="http://my-pc.lan:11434" data-mp-server-url aria-label="Model server address">
                        <button type="submit" class="cseries-btn small">Add</button>
                    </form>
                    <div class="label-sys" data-mp-server-status></div>
                </div>`;
        }

        _wireLocalServers(el) {
            const status = el.querySelector('[data-mp-server-status]');
            const current = () => ((this.state.localEndpoints || {}).endpoints || []).map((e) => e.url);
            const save = async (urls) => {
                if (status) status.textContent = 'checking…';
                const res = await fetchDetail('/api/inference/local-endpoints', {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ endpoints: urls }),
                });
                if (!res.ok) { if (status) status.textContent = res.detail || 'could not save'; return; }
                this.state.localEndpoints = res.body;
                await this.reload();
            };
            const form = el.querySelector('[data-mp-server-add]');
            if (form) form.addEventListener('submit', async (ev) => {
                ev.preventDefault();
                const input = form.querySelector('[data-mp-server-url]');
                const url = (input && input.value || '').trim();
                if (url) await save(current().concat([url]));
            });
            el.querySelectorAll('[data-mp-server-remove]').forEach((b) => {
                b.addEventListener('click', async () => {
                    const i = Number(b.getAttribute('data-mp-server-remove'));
                    await save(current().filter((_, k) => k !== i));
                });
            });
        }

        // The coding multi-select is SET here (relocated from the Workbench,
        // the Orchestrator is the single decision surface;
        // the Workbench consumes the selection for every fan-out). Each harness
        // runs its OWN model; the optional per-harness model/effort override is
        // honored by review.fan_out, so it rides the same {id, model, effort}
        // entries — a toggle must NEVER strip a sibling's overrides.
        // Open any app the shell knows — same mechanism as _openKeys, which
        // stays for the widget's other key-hints. `seed` is SENT into the app
        // as the user's message once it is up (AppRunner.launch's prompt lane →
        // the Circles composer's cseries:compose mode='send'), so a Connect
        // button starts the conversation rather than dropping the user
        // somewhere to figure out. When launch() ignored the payload, the
        // button focused Circles and said nothing.
        _openApp(appId, seed) {
            const payload = seed ? { prompt: seed } : undefined;
            try {
                if (window.parent && window.parent.AppRunner) {
                    window.parent.AppRunner.launch(appId, payload);
                    return true;
                }
            } catch (e) { /* cross-origin / not framed */ }
            try {
                window.parent.postMessage(
                    { type: 'cseries:open-app', app_id: appId, prompt: seed || undefined }, '*');
            } catch (e) {}
            return false;
        }

        // Open an app at a specific SCREEN. The shell's deep-link lane is
        // AppRunner.launch(appId, {view}) — app-runner.js turns it into
        // '#view=<v>' on a fresh iframe and a 'cseries:app-view' postMessage into
        // an already-open window. Used by "Run it free on your machine", which
        // must land on the walkthrough's Network step, not wherever the tour
        // was last left.
        _openAppView(appId, view) {
            try {
                if (window.parent && window.parent.AppRunner) {
                    window.parent.AppRunner.launch(appId, { view: view });
                    return true;
                }
            } catch (e) { /* cross-origin / not framed */ }
            try {
                window.parent.postMessage(
                    { type: 'cseries:open-app', app_id: appId, view: view }, '*');
            } catch (e) {}
            return false;
        }

        // Connecting a harness runs THE VENDOR'S OWN login, and the host drives
        // it in conversation: it starts the login in that harness's pod, hands
        // over the vendor's link (and one-time code, for a device-code vendor
        // like codex — nothing to paste anywhere), reads the grant back out of
        // the pod when the user is done, and shuts the pod down again. The
        // platform never mints or holds the grant — this seeds the ask; the
        // harness_connections tool's own next_step tells the host the rest,
        // per vendor, so the seed must not prescribe a Keys paste that only
        // some vendors have.
        _connectSeed(harnessId, label) {
            return `Connect ${label} for me. Run its own login (harness_connections `
                 + `op='login', provider='${harnessId}') and walk me through finishing it.`;
        }

        // ── Coding Engines ──
        // One row per engine: the key it runs on (connection), its default
        // model, its default effort, and ONE "Default" tick — the engine an
        // unnamed build runs on. No builder ticks and no ladder here:
        // anything beyond one engine is a coding protocol
        // (Multi-Agent Coding, below) that the host reads. Fed by
        // /api/inference/coding-matrix — the same build harness_connections
        // serves the host, so screen and chat can never disagree.
        _renderMatrix() {
            const el = this.root.querySelector('[data-mp-matrix]');
            if (!el) return;
            const m = this.state.matrix;
            if (!m || !Array.isArray(m.harnesses)) {
                el.innerHTML = '<div class="mp-empty label-sys">Coding settings unavailable — couldn\'t read them. Reload to retry.</div>';
                return;
            }
            const rows = m.harnesses;
            const byId = {};
            rows.forEach((r) => { byId[r.id] = r; });
            const ready = (r) => r.key.state === 'connected';
            // While any row's CLI catalog is being read in its pod
            // (models_loading), poll the matrix until it lands. A cold wake is
            // 35-60 s, so: every 5 s, bounded at ~2.5 min; one timer at a time,
            // and a new render resets it so an unmount can't leave it running.
            clearTimeout(this._mxPoll);
            if (rows.some((r) => r.models_loading)) {
                this._mxPolls = (this._mxPolls || 0) + 1;
                if (this._mxPolls <= 30) {
                    this._mxPoll = setTimeout(async () => {
                        try {
                            const fresh = await fetchJSON('/api/inference/coding-matrix');
                            if (fresh && Array.isArray(fresh.harnesses)) {
                                this.state.matrix = fresh;
                                this._renderMatrix();
                            }
                        } catch (e) { /* next poll retries; the row still says loading */ }
                    }, 5000);
                }
            } else {
                this._mxPolls = 0;
            }
            const defaultId = m.default_engine || null;

            const DOT = { connected: '●', not_set_up: '○', disconnected: '⊘', expired: '▲' };
            const WORD = { connected: 'Connected', not_set_up: 'Not set up', disconnected: 'Disconnected', expired: 'Expired' };

            const priceTag = (mm) => {
                const i = perM(mm.input_per_token), o = perM(mm.output_per_token);
                if (i == null && o == null) return '';
                return ` — ${fmtUsd(i == null ? 0 : i)}/${fmtUsd(o == null ? 0 : o)} per M`;
            };
            const modelOptions = (row, cur) => {
                const known = !cur || (row.models || []).some((mm) => mm.id === cur);
                const builtIn = row.default_model ? ` (${row.default_model})` : '';
                return `<option value="">Model: default${esc(builtIn)}</option>`
                    + (row.models || []).map((mm) =>
                        `<option value="${esc(mm.id)}"${cur === mm.id ? ' selected' : ''}>${esc(mm.id)}${esc(priceTag(mm))}</option>`).join('')
                    + (known ? '' : `<option value="${esc(cur)}" selected>${esc(cur)} (unlisted)</option>`);
            };

            // ── the engine row ───────────────────────────────────────────
            const engineRow = (row) => {
                const on = ready(row);
                const dot = `<span class="mp-mx-dot mp-mx-${esc(row.key.state)}">${DOT[row.key.state] || '?'}</span>`;
                let runsOn;
                if (row.band === 'generic' && (row.provider_options || []).length) {
                    runsOn = `<select class="cseries-select mp-mx-provider" data-harness="${esc(row.id)}" aria-label="${esc(row.label)} provider">
                            ${row.provider_options.map((p) =>
                                `<option value="${esc(p)}"${(row.wired || row.provider) === p ? ' selected' : ''}>${esc(p)}</option>`).join('')}
                        </select> ${dot}`;
                } else if (row.band === 'generic') {
                    runsOn = `<span class="label-sys">No generic key</span> ${dot}`;
                } else {
                    runsOn = `<span class="mp-mx-bound">${esc(row.provider || row.vendor || '')}</span> ${dot} <span class="label-sys">${esc(WORD[row.key.state] || '')}</span>`;
                }
                const action = row.action
                    ? `<button class="cseries-btn ghost mp-mx-action" data-app="${esc(row.action.app || 'keys')}" data-harness="${esc(row.id)}" data-kind="${esc(row.action.kind || 'open')}">${esc(row.action.label)} →</button>`
                    : '';
                // A CLI-catalog harness (cursor) with nothing cached: the kernel
                // has WOKEN its pod and is reading the list right now
                // (models_loading) — show that, and _renderMatrix polls the
                // matrix until the rows land. A read that FAILED
                // carries the vendor's own words in models_error ("no models for
                // this account — check the key"): something the user can act on.
                const modelNote = (on && !(row.models || []).length)
                    ? (row.models_loading
                        ? `<div class="label-sys mp-mx-modelnote mp-mx-loading">Loading ${esc(row.label)}'s models…</div>`
                        : (row.models_error
                            ? `<div class="label-sys mp-mx-modelnote">${esc(row.models_error)}</div>`
                            : ''))
                    : '';
                const model = on
                    ? `<select class="cseries-select mp-mx-model" data-harness="${esc(row.id)}" aria-label="${esc(row.label)} default model">${modelOptions(row, row.model_override || '')}</select>${modelNote}`
                    : (action || '<span class="label-sys">—</span>');
                const effort = (on && (row.efforts || []).length)
                    ? `<select class="cseries-select mp-mx-effort" data-harness="${esc(row.id)}" aria-label="${esc(row.label)} default effort">
                            <option value="">Effort: default</option>
                            ${row.efforts.map((v) =>
                                `<option value="${esc(v)}"${row.effort_override === v ? ' selected' : ''}>${esc(v)}</option>`).join('')}
                        </select>`
                    : (on ? '<span class="label-sys">—</span>' : '');
                // The Default tick: a radio (one engine at most), enabled only
                // on a connected row — a build cannot run on an engine with
                // no key, so the tick cannot land there either.
                const isDefault = row.default === true || row.id === defaultId;
                return `
                    <tr class="mp-mx-row${on ? '' : ' mp-mx-dim'}${isDefault ? ' mp-mx-is-default' : ''}">
                        <td><div class="mp-mx-h"><img class="mp-harness-logo" src="${esc(row.icon_url)}" alt="" onerror="this.style.visibility='hidden'"><span>${esc(row.label)}</span></div></td>
                        <td><div class="mp-mx-cell">${runsOn}</div></td>
                        <td><div class="mp-mx-cell">${model}</div></td>
                        <td><div class="mp-mx-cell">${effort}</div></td>
                        <td class="mp-mx-selcell"><input type="radio" name="mp-default-engine" class="mp-mx-default" data-harness="${esc(row.id)}"${isDefault ? ' checked' : ''}${on ? '' : ' disabled'} aria-label="Default engine: ${esc(row.label)}"></td>
                    </tr>`;
            };
            const bandRows = (title, hint, list) => list.length ? `
                <tr class="mp-mx-band"><td colspan="5"><span class="card-label label-sys">${esc(title)}</span> <span class="label-sys">${esc(hint)}</span></td></tr>
                ${list.map(engineRow).join('')}` : '';

            const defRow = defaultId ? byId[defaultId] : null;
            const connectedCount = rows.filter(ready).length;
            let defaultNote;
            if (defRow) {
                defaultNote = `Builds that don't name an engine run on ${esc(defRow.label)}`
                    + (ready(defRow) ? '.' : ' — it is not connected right now, so such a build stops and says so instead of guessing.');
            } else if (connectedCount === 1) {
                const only = rows.find(ready);
                defaultNote = `No default ticked — ${esc(only.label)} is the only connected engine, so builds that don't name one run there.`;
            } else if (connectedCount > 1) {
                defaultNote = 'No default ticked — a build that doesn\'t name an engine stops and asks. Tick one to make it the default.';
            } else {
                defaultNote = 'No engine is connected yet — each row says how to connect it.';
            }

            el.innerHTML = `
                <table class="mp-mx">
                    <thead><tr class="mp-mx-head label-sys">
                        <th>Engine</th><th>Runs On</th><th>Default Model</th><th>Default Effort</th><th>Default</th>
                    </tr></thead>
                    <tbody>
                        ${bandRows('First-Party', '· the vendor\'s own key or login runs its engine — no substitutes',
                                   rows.filter((r) => r.band !== 'generic'))}
                        ${bandRows('Generic', '· your reseller keys — pick which one each engine uses',
                                   rows.filter((r) => r.band === 'generic'))}
                    </tbody>
                </table>
                <div class="mp-mx-foot">
                    <span class="label-sys mp-mx-note">${defaultNote}</span>
                    ${defRow ? '<button class="cseries-btn ghost mp-mx-default-clear">Clear default</button>' : ''}
                    <span class="label-sys mp-mx-status" data-mp-mx-status></span>
                </div>`;

            const note = (text) => {
                const n = el.querySelector('[data-mp-mx-status]');
                if (n) n.textContent = text;
            };

            // ── saves ────────────────────────────────────────────────────
            // A refused save says WHY, in the status line — a silent reload
            // that undoes the click reads as "it unchecked itself" (it did
            // exactly that for weeks).
            const selList = () => (this.state.codingHarnesses || []).map((h) => Object.assign({}, h));
            const saveOverrides = async (list) => {
                const harnesses = list.map((h) => {
                    const entry = { id: h.id };
                    if (h.model) entry.model = h.model;
                    if (h.effort) entry.effort = h.effort;
                    return entry;
                });
                note('Saving…');
                const res = await fetchDetail('/api/inference/coding-harnesses', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ harnesses }),
                });
                if (res.ok) {
                    // The server's cleaned list (a stale/retired id it dropped stays dropped).
                    this.state.codingHarnesses = (res.body && Array.isArray(res.body.coding_harnesses))
                        ? res.body.coding_harnesses : list;
                    note('');
                    if (typeof this.opts.onChange === 'function') this.opts.onChange('coding-harnesses', harnesses);
                    return true;
                }
                await this.reload();
                note(res.detail || 'Could not save the engine settings.');
                return false;
            };
            const saveOverride = async (id, patch) => {
                const list = selList();
                let h = list.find((x) => x.id === id);
                if (!h) { h = { id }; list.push(h); }
                Object.assign(h, patch);
                if (!h.model) delete h.model;
                if (!h.effort) delete h.effort;
                if (!(await saveOverrides(list))) return;
                const row = byId[id];
                if (row) {
                    if ('model' in patch) row.model_override = patch.model || null;
                    if ('effort' in patch) row.effort_override = patch.effort || null;
                }
                this._renderMatrix();
            };
            const saveDefault = async (id) => {
                note('Saving…');
                const res = await fetchDetail('/api/inference/coding-default', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ harness: id || null }),
                });
                if (res.ok) {
                    m.default_engine = id || null;
                    rows.forEach((r) => { r.default = !!id && r.id === id; });
                    note('');
                    if (typeof this.opts.onChange === 'function') this.opts.onChange('coding-default', id || null);
                } else {
                    note(res.detail || 'Could not save the default engine.');
                }
                this._renderMatrix();
            };

            // ── handlers ─────────────────────────────────────────────────
            el.querySelectorAll('.mp-mx-model').forEach((sel) => {
                sel.addEventListener('change', () => saveOverride(sel.dataset.harness, { model: sel.value.trim() }));
            });
            el.querySelectorAll('.mp-mx-effort').forEach((sel) => {
                sel.addEventListener('change', () => saveOverride(sel.dataset.harness, { effort: sel.value }));
            });
            el.querySelectorAll('.mp-mx-default').forEach((rb) => {
                rb.addEventListener('change', () => { if (rb.checked) saveDefault(rb.dataset.harness); });
            });
            const clearBtn = el.querySelector('.mp-mx-default-clear');
            if (clearBtn) clearBtn.addEventListener('click', () => saveDefault(null));
            el.querySelectorAll('.mp-mx-provider').forEach((sel) => {
                sel.addEventListener('change', async () => {
                    note('Rewiring…');
                    const res = await fetchDetail('/api/inference/harness-wiring', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ harness: sel.dataset.harness, provider: sel.value }),
                    });
                    if (!res.ok) note(res.detail || 'Could not rewire.');
                    await this.reload();
                });
            });
            el.querySelectorAll('.mp-mx-action').forEach((btn) => {
                btn.addEventListener('click', (ev) => {
                    ev.preventDefault();
                    const id = btn.dataset.harness;
                    const row = byId[id];
                    // A vendor-account engine connects through its OWN login,
                    // driven by the host in Circles. Everything else is a key
                    // the user pastes, so Keys is the whole flow.
                    if (btn.dataset.kind === 'login' && row) {
                        this._openApp('circles', this._connectSeed(id, row.label));
                    } else {
                        this._openApp(btn.dataset.app || 'keys');
                    }
                });
            });
        }

        // The ask that gets the host to write a protocol with the user — it
        // seeds the Circles composer (same lane as the Connect buttons).
        _protocolSeed() {
            return 'Write me a coding protocol for the Multi-Agent Coding list. Ask me which '
                 + 'engines to use, in what order or at once, with which model and effort, and '
                 + 'what counts as pass or fail — then save it with coding_protocol op=\'save\' '
                 + 'and tell me its name.';
        }

        // ── Multi-Agent Coding — the protocol files ──────────────────
        // A protocol is a JSON file of INSTRUCTIONS the host reads (which
        // engines, one after another or at once, model/effort, a time budget
        // if any, what to do on pass/fail). The list shows each file, who
        // wrote it, its shape, and what it measured; the host writes them in
        // conversation, the user can paste or hand-edit the JSON, export it to
        // share, import one someone shared. Fed by /api/coding-protocols.
        _renderProtocols() {
            const el = this.root.querySelector('[data-mp-protocols]');
            if (!el) return;
            const list = this.state.protocols;
            if (!Array.isArray(list)) {
                el.innerHTML = '<div class="mp-empty label-sys">Protocols unavailable — couldn\'t read them. Reload to retry.</div>';
                return;
            }
            const st = this.state.proto || (this.state.proto = { editing: null, draft: '', status: '', check: {} });
            const fmtWhen = (t) => {
                if (!t) return '—';
                const d = new Date(t * 1000);
                return isNaN(d.getTime()) ? '—' : d.toISOString().slice(0, 10);
            };
            const statsCell = (s) => {
                s = s || {};
                if (!s.runs) return '<span class="label-sys">no runs yet</span>';
                const last = s.last_result ? ` · last ${esc(s.last_result)} ${esc(fmtWhen(s.last_run_at))}` : '';
                const cost = (s.compute_s != null && s.harness_runs) ? ` · ${esc(String(s.harness_runs))} harness runs, ${esc(String(Math.round(s.compute_s)))}s compute` : '';
                return `${esc(String(s.runs))} runs · ${esc(String(s.pass || 0))} pass · ${esc(String(s.fail || 0))} fail${last}${cost}`;
            };
            const row = (p) => `
                <tr class="mp-proto-row" data-slug="${esc(p.slug)}">
                    <td><div class="mp-proto-name"><span>${esc(p.name)}</span>${p.description ? `<span class="label-sys mp-proto-desc">${esc(p.description)}</span>` : ''}</div></td>
                    <td><span class="label-sys">${esc(p.author || '—')}</span></td>
                    <td><code class="mp-proto-shape">${esc(p.shape || '')}</code></td>
                    <td><span class="label-sys mp-proto-stats">${statsCell(p.stats)}</span></td>
                    <td class="mp-proto-acts">
                        <button class="cseries-btn ghost mp-proto-check" data-slug="${esc(p.slug)}" title="Are its engines connected here?">Check</button>
                        <button class="cseries-btn ghost mp-proto-edit" data-slug="${esc(p.slug)}">Edit</button>
                        <button class="cseries-btn ghost mp-proto-export" data-slug="${esc(p.slug)}" title="Download the file, stats included">Export</button>
                        <button class="cseries-btn ghost mp-proto-delete" data-slug="${esc(p.slug)}">Delete</button>
                    </td>
                </tr>
                ${st.check[p.slug] ? `<tr class="mp-proto-sub"><td colspan="5">${st.check[p.slug]}</td></tr>` : ''}
                ${st.editing === p.slug ? `<tr class="mp-proto-sub"><td colspan="5">${editor('Save changes')}</td></tr>` : ''}`;
            const editor = (saveLabel) => `
                <div class="mp-proto-editor">
                    <textarea class="cseries-textarea mp-proto-json" rows="14" spellcheck="false" aria-label="Protocol JSON">${esc(st.draft)}</textarea>
                    <div class="mp-proto-bar">
                        <button class="cseries-btn primary mp-proto-save">${esc(saveLabel)}</button>
                        <button class="cseries-btn ghost mp-proto-cancel">Cancel</button>
                        <span class="label-sys mp-proto-status" data-mp-proto-status>${esc(st.status || '')}</span>
                    </div>
                </div>`;

            el.innerHTML = `
                <div class="mp-proto-bar">
                    <button class="cseries-btn mp-proto-ask">Ask the host to write one →</button>
                    <button class="cseries-btn ghost mp-proto-import">Import a file…</button>
                    <span class="label-sys mp-proto-status" data-mp-proto-status>${st.editing ? '' : esc(st.status || '')}</span>
                </div>
                ${st.editing === '__import__' ? editor('Save file') : ''}
                ${list.length ? `
                <table class="mp-mx mp-proto">
                    <thead><tr class="mp-mx-head label-sys">
                        <th>Name</th><th>Author</th><th>Steps</th><th>Measured</th><th></th>
                    </tr></thead>
                    <tbody>${list.map(row).join('')}</tbody>
                </table>` : '<div class="mp-empty label-sys">No protocols yet. Ask the host to write one, or import a file someone shared.</div>'}`;

            const status = (text) => {
                st.status = text || '';
                el.querySelectorAll('[data-mp-proto-status]').forEach((n) => { n.textContent = st.status; });
            };
            const openEditor = (slug, text) => {
                st.editing = slug;
                st.draft = text;
                st.status = '';
                this._renderProtocols();
            };
            const closeEditor = () => { st.editing = null; st.draft = ''; st.status = ''; this._renderProtocols(); };
            const reloadList = async () => {
                const got = await fetchJSON('/api/coding-protocols');
                if (got && Array.isArray(got.protocols)) this.state.protocols = got.protocols;
                this._renderProtocols();
            };

            const askBtn = el.querySelector('.mp-proto-ask');
            if (askBtn) askBtn.addEventListener('click', () => this._openApp('circles', this._protocolSeed()));
            const importBtn = el.querySelector('.mp-proto-import');
            if (importBtn) importBtn.addEventListener('click', () => openEditor('__import__', JSON.stringify({
                format: 'privatae.coding-protocol/v1',
                name: '',
                author: '',
                description: '',
                steps: [{ id: 'step-1', engines: [{ engine: '' }], on_pass: 'end', on_fail: 'end' }],
            }, null, 2)));
            el.querySelectorAll('.mp-proto-edit').forEach((btn) => {
                btn.addEventListener('click', async () => {
                    const slug = btn.dataset.slug;
                    const doc = await fetchJSON(`/api/coding-protocols/${encodeURIComponent(slug)}`);
                    if (!doc) { status('Could not read that protocol.'); return; }
                    // The editable document: the file minus the server's bookkeeping.
                    const out = {};
                    ['format', 'name', 'author', 'description', 'notes', 'steps'].forEach((k) => { if (doc[k] !== undefined) out[k] = doc[k]; });
                    openEditor(slug, JSON.stringify(out, null, 2));
                });
            });
            el.querySelectorAll('.mp-proto-check').forEach((btn) => {
                btn.addEventListener('click', async () => {
                    const slug = btn.dataset.slug;
                    const res = await fetchDetail(`/api/coding-protocols/${encodeURIComponent(slug)}/preflight`);
                    if (!res.ok) { status(res.detail || 'Could not check.'); return; }
                    const pf = res.body || {};
                    const parts = (pf.engines || []).map((e) => {
                        const dot = e.ready ? '●' : (e.state === 'expired' ? '▲' : '○');
                        const fix = (!e.ready && e.action && e.action.label) ? ` — ${esc(e.action.label)}` : '';
                        return `<span class="mp-proto-pf ${e.ready ? 'ok' : 'no'}">${dot} ${esc(e.label || e.engine)}${e.auth ? ` <span class="label-sys">(${esc(e.auth === 'oauth' ? 'vendor login' : 'API key')})</span>` : ''}${fix}</span>`;
                    });
                    st.check[slug] = `<div class="mp-proto-pfrow"><span class="label-sys">${pf.ready ? 'Ready — every engine it names is connected.' : 'Not ready — connect the engines it names first:'}</span> ${parts.join(' ')} <button class="cseries-btn ghost mp-proto-check-close" data-slug="${esc(slug)}">✕</button></div>`;
                    this._renderProtocols();
                });
            });
            el.querySelectorAll('.mp-proto-check-close').forEach((btn) => {
                btn.addEventListener('click', () => { delete st.check[btn.dataset.slug]; this._renderProtocols(); });
            });
            el.querySelectorAll('.mp-proto-export').forEach((btn) => {
                btn.addEventListener('click', async () => {
                    const slug = btn.dataset.slug;
                    const doc = await fetchJSON(`/api/coding-protocols/${encodeURIComponent(slug)}?with_stats=1`);
                    if (!doc) { status('Could not read that protocol.'); return; }
                    const blob = new Blob([JSON.stringify(doc, null, 2) + '\n'], { type: 'application/json' });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `${slug}.coding-protocol.json`;
                    document.body.appendChild(a);
                    a.click();
                    a.remove();
                    setTimeout(() => URL.revokeObjectURL(url), 1000);
                });
            });
            el.querySelectorAll('.mp-proto-delete').forEach((btn) => {
                btn.addEventListener('click', async () => {
                    const slug = btn.dataset.slug;
                    if (!window.confirm(`Delete the protocol "${slug}"? Its recorded runs go with it.`)) return;
                    const res = await fetchDetail(`/api/coding-protocols/${encodeURIComponent(slug)}`, { method: 'DELETE' });
                    if (!res.ok) { status(res.detail || 'Could not delete.'); return; }
                    delete st.check[slug];
                    if (st.editing === slug) { st.editing = null; st.draft = ''; }
                    await reloadList();
                });
            });
            const saveBtn = el.querySelector('.mp-proto-save');
            if (saveBtn) {
                saveBtn.addEventListener('click', async () => {
                    const ta = el.querySelector('.mp-proto-json');
                    const text = ta ? ta.value : st.draft;
                    st.draft = text;
                    let doc;
                    try { doc = JSON.parse(text); } catch (e) { status(`Not valid JSON: ${e.message}`); return; }
                    const creating = st.editing === '__import__';
                    const url = creating ? '/api/coding-protocols' : `/api/coding-protocols/${encodeURIComponent(st.editing)}`;
                    status('Saving…');
                    const res = await fetchDetail(url, {
                        method: creating ? 'POST' : 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(doc),
                    });
                    if (!res.ok) { status(res.detail || 'Could not save.'); return; }
                    st.editing = null; st.draft = ''; st.status = '';
                    await reloadList();
                    if (typeof this.opts.onChange === 'function') this.opts.onChange('coding-protocols', this.state.protocols);
                });
            }
            const cancelBtn = el.querySelector('.mp-proto-cancel');
            if (cancelBtn) cancelBtn.addEventListener('click', closeEditor);
            // Keep the draft as the user types, so a re-render (a Check on
            // another row) never throws their edits away.
            const ta = el.querySelector('.mp-proto-json');
            if (ta) ta.addEventListener('input', () => { st.draft = ta.value; });
        }

        // Embedding is folded into the routing surface but uses its OWN flow:
        // reads/writes behavioral_params.embedding_model via /api/embedding/*,
        // not /api/inference/routing. ONE row, provider → model — like every
        // other role (replaces the two-card Privatae
        // Endpoint / Local layout — the managed catalog serves zero models).
        // 'local' lists the user's embed-capable machines + what each serves;
        // a keyed provider lists its embedding models (/provider-models).
        // Every switch is probe-gated server-side; nothing ever re-embeds or
        // dumps — vectors live in per-dim columns and switching re-points.
        _renderEmbedding() {
            const el = this.root.querySelector('[data-mp-embed]');
            if (!el) return;
            const cur = this.state.embedCurrent || {};
            const bastions = (this.state.embedBastions || []).slice();
            const provs = this._providers().filter((p) => p.embeddings);
            const curProv = cur.provider || '';
            // Transient browse pick; falls back to where embeds run today. A
            // legacy 'privatae' lane isn't offerable anymore — it reads as
            // unselected (the current-state line still tells the truth).
            const curSelectable = provs.some((p) => p.id === curProv) ? curProv : '';
            const selProv = this.state.embedSel !== undefined ? this.state.embedSel : curSelectable;

            // Which bastion serves today (identity first, endpoint compare as
            // the legacy fallback).
            const epNorm = (u) => String(u || '').trim().toLowerCase().replace(/\/+$/, '').replace(/\/api$/, '');
            const baseNorm = epNorm(cur.embedding_base_url);
            const servingBastion = (curProv === 'local')
                ? (bastions.find((b) => b.hostname === cur.bastion)
                   || (baseNorm ? bastions.find((b) => {
                        const ep = epNorm(b.embed_endpoint);
                        return ep && (ep === baseNorm || baseNorm.indexOf(ep) === 0 || ep.indexOf(baseNorm) === 0);
                    }) : null))
                : null;
            const servingName = servingBastion ? (servingBastion.name || servingBastion.hostname) : null;
            const runsOn = curProv === 'local'
                ? 'your machine' + (servingName ? ' · ' + servingName : '')
                : curProv === 'privatae' ? 'platform (legacy)'
                : (curProv || null);

            const provOptions = ['<option value="">—</option>']
                .concat(provs.map((p) => {
                    const keyless = !p.has_key;
                    const label = p.id === 'local' ? 'local — your machines' : p.id;
                    return `<option value="${esc(p.id)}"${p.id === selProv ? ' selected' : ''}${keyless && p.id !== selProv ? ' disabled' : ''}>${esc(label)}${keyless ? ' — add key in Keys' : ''}</option>`;
                })).join('');

            // Model control per selected provider.
            let modelCtl = '';
            let bastionCtl = '';
            if (selProv === 'local') {
                if (!bastions.length) {
                    modelCtl = '<span class="mp-cell-empty label-sys">no embed-capable machine online — start one in the Network app</span>';
                } else {
                    bastionCtl = `<select class="cseries-select" data-mp-embed-bastion aria-label="embedding machine">
                        ${bastions.map((b) => {
                            const nm = b.name ? (b.hostname + ' (' + b.name + ')') : b.hostname;
                            const isCur = servingBastion && servingBastion.hostname === b.hostname;
                            return `<option value="${esc(b.hostname)}"${isCur ? ' selected' : ''}>${esc(nm)}</option>`;
                        }).join('')}
                    </select>`;
                    modelCtl = '<select class="cseries-select" data-mp-embed-model aria-label="embedding model"></select>';
                }
            } else if (selProv) {
                const dm = (this.state.embedProviderModels || {})[selProv];
                if (dm == null) {
                    this._ensureEmbedProviderModels(selProv);
                    modelCtl = `<span class="mp-cell-empty label-sys">loading ${esc(selProv)} embedding models…</span>`;
                } else if (dm.error) {
                    modelCtl = `<span class="mp-cell-empty label-sys">${esc(dm.error)}</span>`;
                } else if (!dm.length) {
                    modelCtl = `<span class="mp-cell-empty label-sys">${esc(selProv)} lists no embedding models</span>`;
                } else {
                    modelCtl = `<select class="cseries-select" data-mp-embed-model aria-label="embedding model">
                        <option value="">—</option>
                        ${dm.map((m) => `<option value="${esc(m.id)}"${m.id === cur.model && selProv === curProv ? ' selected' : ''}>${esc(m.id)}</option>`).join('')}
                    </select>`;
                }
            }

            el.innerHTML = `
                <div class="mp-role mp-embed-card">
                    <div class="mp-role-head">
                        <div class="mp-role-id">
                            <div class="mp-grid-role">Embedding</div>
                            <div class="mp-grid-meta">${cur.model
                                ? '<span class="cseries-pill allow mp-route-src">configured</span>'
                                : '<span class="cseries-pill info mp-route-src">pick where embeds run</span>'}</div>
                        </div>
                        <div class="mp-role-controls">
                            <label class="mp-role-field">runs on
                                <select class="cseries-select" data-mp-embed-provider aria-label="embedding provider">${provOptions}</select>
                            </label>
                            ${bastionCtl ? `<label class="mp-role-field">machine ${bastionCtl}</label>` : ''}
                            <label class="mp-role-field">model
                                ${modelCtl || '<select class="cseries-select" disabled><option>— pick where first —</option></select>'}
                            </label>
                            <button class="cseries-btn" data-mp-embed-apply${selProv ? '' : ' disabled'}>Switch</button>
                        </div>
                    </div>
                    ${cur.model ? `<div class="mp-role-eff label-sys">runs: ${esc(runsOn || 'platform')} · ${esc(cur.model)}${cur.dim ? ' · ' + esc(cur.dim) + 'd' : ''} — switching never re-embeds or deletes vectors</div>` : ''}
                    <div class="mp-cell-note label-sys" data-mp-embed-status hidden></div>
                </div>
            `;

            const status = (t) => {
                const n = el.querySelector('[data-mp-embed-status]');
                if (n) { n.textContent = t || ''; n.hidden = !t; }
            };

            const provSel = el.querySelector('[data-mp-embed-provider]');
            if (provSel) {
                provSel.addEventListener('change', () => {
                    this.state.embedSel = provSel.value || '';
                    this._renderEmbedding();
                });
            }

            // Local lane: model options = what the picked machine serves.
            const baSel = el.querySelector('[data-mp-embed-bastion]');
            const modelSel = el.querySelector('[data-mp-embed-model]');
            const fillBastionModels = () => {
                if (!modelSel || !baSel) return;
                const b = bastions.find((x) => x.hostname === baSel.value);
                const served = ((b && b.models) || []).filter(Boolean);
                const opts = [];
                if (cur.model && curProv === 'local') opts.push(`<option value="">keep ${esc(cur.model)}</option>`);
                served.forEach((m) => {
                    if (!(m === cur.model && curProv === 'local')) opts.push(`<option value="${esc(m)}">${esc(m)}</option>`);
                });
                if (!opts.length) opts.push('<option value="">this machine reports no models</option>');
                modelSel.innerHTML = opts.join('');
            };
            if (selProv === 'local' && baSel) {
                fillBastionModels();
                baSel.addEventListener('change', fillBastionModels);
            }

            const applyBtn = el.querySelector('[data-mp-embed-apply]');
            if (applyBtn) {
                applyBtn.addEventListener('click', async () => {
                    if (!selProv) return;
                    applyBtn.disabled = true;
                    status('switching…');
                    let res;
                    if (selProv === 'local') {
                        const server = baSel && baSel.value;
                        if (!server) { status('pick a machine'); applyBtn.disabled = false; return; }
                        const body = { server };
                        const m = modelSel && modelSel.value;
                        if (m) body.model = m;
                        res = await fetchJSON('/api/embedding/use-bastion', {
                            method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(body),
                        });
                    } else {
                        const m = modelSel && modelSel.value;
                        if (!m) { status('pick a model'); applyBtn.disabled = false; return; }
                        res = await fetchJSON('/api/embedding/use-provider', {
                            method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ provider: selProv, model: m }),
                        });
                    }
                    applyBtn.disabled = false;
                    if (res && !res.error) {
                        delete this.state.embedSel;
                        status(res.rows_missing_active_dim
                            ? `switched · ${res.rows_missing_active_dim} memories not yet embedded on this model — ask your host to fill them`
                            : 'switched');
                        if (typeof this.opts.onChange === 'function') this.opts.onChange('embedding', res);
                        await this.reload();
                    } else {
                        status((res && res.error) || 'switch failed');
                    }
                });
            }
        }

        // Lazily fetch the models that can actually DO a capability, for one
        // provider (GET /api/inference/capable-models/<role>?provider=<p>).
        // role: 'image_gen' (makes an image) — the only image role. The
        // endpoint also answers 'vision' (reads an image) as a capability filter.
        //
        // The server reads the catalog's declared modalities — this never
        // guesses from a model name. Cached per role+provider; an error is
        // cached as {error} so the pane says "couldn't look" rather than
        // rendering an empty list that reads as "your provider has none".
        _ensureCapableModels(role, prov) {
            if (!role || !prov || prov === 'local') return;
            const k = role + '::' + prov;
            this.state.capable = this.state.capable || {};
            if (this.state.capable[k] !== undefined) return;
            this.state.capable[k] = null;
            fetchDetail(`/api/inference/capable-models/${encodeURIComponent(role)}`
                        + `?provider=${encodeURIComponent(prov)}`)
                .then((res) => {
                    const pane = res.ok && res.body && res.body.providers
                        ? (res.body.providers[prov] || {}) : null;
                    if (!pane) {
                        this.state.capable[k] = { error: res.detail || 'could not read the catalog' };
                    } else if (pane.error) {
                        this.state.capable[k] = { error: pane.error };
                    } else {
                        // capability_published === false: the provider states no
                        // modalities at all, so this is EVERY model they serve,
                        // not a filtered set. Carried through so the pane can
                        // say which it is — xAI is the live case.
                        const list = pane.models || [];
                        list.unpublished = pane.capability_published === false;
                        this.state.capable[k] = list;
                    }
                    this._afterCapableModels();
                })
                .catch(() => {
                    this.state.capable[k] = { error: 'could not read the catalog' };
                    this._afterCapableModels();
                });
        }

        // The capable-models list feeds BOTH surfaces (Simple's Seeing step and
        // Advanced's Image generation card), so the arrival of that fetch has to
        // repaint whichever one is on screen. Repainting only Simple left the
        // Advanced dropdowns stuck on "checking which models can do this…"
        // until the user changed tabs — the same failure _afterEmbedModels
        // below already fixes for embeddings, which this should have mirrored.
        _afterCapableModels() {
            this._renderImageRoles();
            if (this.opts.simple && this.mode === 'simple') this._renderSimple();
        }

        // The embedding model list feeds BOTH surfaces now (Advanced's
        // Embedding card and Simple's Memory step), so the arrival of that
        // fetch has to repaint whichever one is on screen. It repainted only
        // Advanced before Simple had a Memory step, which left Simple stuck on
        // "reading … models…" forever.
        _afterEmbedModels() {
            this._renderEmbedding();
            if (this.opts.simple && this.mode === 'simple') this._renderSimple();
        }

        // Lazily fetch a provider's embedding-capable model list
        // (GET /api/embedding/provider-models/<p>). Mirrors _ensureDirectModels.
        _ensureEmbedProviderModels(prov) {
            if (!prov || prov === 'local') return;
            this.state.embedProviderModels = this.state.embedProviderModels || {};
            if (this.state.embedProviderModels[prov] !== undefined) return;
            this.state.embedProviderModels[prov] = null;
            fetchDetail(`/api/embedding/provider-models/${encodeURIComponent(prov)}`)
                .then((res) => {
                    this.state.embedProviderModels[prov] = res.ok
                        ? ((res.body && res.body.error)
                            ? { error: res.body.error }
                            : ((res.body && res.body.models) || []))
                        : { error: res.detail || prov + ' embedding catalog unavailable' };
                    this._afterEmbedModels();
                })
                .catch(() => {
                    this.state.embedProviderModels[prov] = { error: prov + ' embedding catalog unavailable' };
                    this._afterEmbedModels();
                });
        }

        // The Balances tab: one row per registry provider. key:false rows
        // point at the Keys app (or say the key is switched off); unsupported
        // rows say the provider exposes no balance endpoint; errored rows say
        // the dial failed. No number is ever fabricated.
        _renderBalances() {
            const el = this.root.querySelector('[data-mp-balances]');
            if (!el) return;
            if ((this.state.loadErrors || []).includes('balances')) {
                el.innerHTML = '<div class="mp-empty mp-balance-note">Balances unavailable right now — Refresh to retry.</div>';
                return;
            }
            const rows = this.state.balances || [];
            if (!rows.length) {
                el.innerHTML = '<div class="mp-empty mp-balance-note">Loading balances…</div>';
                return;
            }
            el.innerHTML = rows.map((b) => {
                let value;
                if (!b.key) {
                    value = '<span class="mp-balance-note">'
                        + (b.deactivated ? 'key deactivated — re-activate in the Keys app'
                                         : 'no key — add it in the Keys app')
                        + '</span>';
                } else if (b.error) {
                    value = `<span class="mp-balance-note">${esc(b.error)} — Refresh to retry</span>`;
                } else if (!b.supported) {
                    value = `<span class="mp-balance-note">${esc(b.detail || 'not exposed over this provider’s API')}</span>`;
                } else {
                    // USD renders $; any other currency shows its code so a
                    // number is never unit-ambiguous.
                    const amount = typeof b.balance === 'number' ? b.balance.toFixed(2) : esc(String(b.balance));
                    const num = b.currency === 'USD' ? '$' + amount
                        : b.currency ? amount + ' ' + esc(b.currency) : amount;
                    value = `<span class="mp-balance-num">${num}</span>`
                        + (b.detail ? ` <span class="mp-balance-note">${esc(b.detail)}</span>` : '');
                }
                return `<div class="mp-balance-row"><span class="mp-balance-prov">${esc(b.provider)}</span><span>${value}</span></div>`;
            }).join('');
        }

        _populateFacets(rows) {
            const vendorSel = this.root.querySelector('[data-mp-filter-vendor]');
            const modSel    = this.root.querySelector('[data-mp-filter-modality]');
            if (!vendorSel || !modSel) return;
            const want = (sel, vals, label) => {
                const cur = sel.value;
                const sorted = Array.from(new Set(vals)).filter(Boolean).sort();
                sel.innerHTML = `<option value="">${label}</option>` +
                    sorted.map((v) => `<option value="${esc(v)}"${v === cur ? ' selected' : ''}>${esc(v)}</option>`).join('');
            };
            want(vendorSel, rows.map((m) => m.vendor), 'All vendors');
            want(modSel,    rows.map((m) => m.modality), 'Any modality');
        }

        _filterAndSort(rows) {
            const f = this.state.filters;
            let list = rows;
            if (f.vendor)   list = list.filter((m) => m.vendor === f.vendor);
            if (f.modality) list = list.filter((m) => m.modality === f.modality);
            if (f.search) {
                const q = f.search.toLowerCase();
                list = list.filter((m) =>
                    m.id.toLowerCase().includes(q) ||
                    m.name.toLowerCase().includes(q) ||
                    m.vendor.toLowerCase().includes(q) ||
                    (m.description || '').toLowerCase().includes(q)
                );
            }
            const { col, dir } = this.state.sort;
            const mul = dir === 'asc' ? 1 : -1;
            list = list.slice().sort((a, b) => {
                const va = a[col]; const vb = b[col];
                if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * mul;
                return String(va || '').localeCompare(String(vb || '')) * mul;
            });
            return list;
        }

        _bindEvents() {
            this.root.querySelectorAll('[data-mp-mode]').forEach((btn) => {
                btn.addEventListener('click', () => this._setMode(btn.dataset.mpMode));
            });

            this.root.querySelectorAll('[data-mp-tab]').forEach((btn) => {
                btn.addEventListener('click', () => {
                    this.state.tab = btn.dataset.mpTab;
                    this._renderTab();
                });
            });

            this._bindKeysLinks(this.root);

            const wire = (selector, key) => {
                const el = this.root.querySelector(selector);
                if (!el) return;
                const evt = el.tagName === 'SELECT' ? 'change' : 'input';
                el.addEventListener(evt, () => {
                    this.state.filters[key] = el.value;
                    this._renderModels();
                });
            };
            wire('[data-mp-search]',          'search');
            wire('[data-mp-filter-vendor]',   'vendor');
            wire('[data-mp-filter-modality]', 'modality');

            // Browse all <-> favorites only.
            const browseToggle = this.root.querySelector('[data-mp-browse-toggle]');
            if (browseToggle) {
                browseToggle.addEventListener('click', () => {
                    this.state.favoritesView = !this.state.favoritesView;
                    this._renderModels();
                });
            }

            this.root.querySelectorAll('.mp-table th.sortable').forEach((th) => {
                th.addEventListener('click', () => {
                    const col = th.dataset.sort;
                    if (this.state.sort.col === col) {
                        this.state.sort.dir = this.state.sort.dir === 'asc' ? 'desc' : 'asc';
                    } else {
                        this.state.sort.col = col;
                        this.state.sort.dir = (col === 'name' || col === 'vendor' || col === 'modality') ? 'asc' : 'desc';
                    }
                    this._renderModels();
                });
            });

            const balRefresh = this.root.querySelector('[data-mp-balances-refresh]');
            if (balRefresh) {
                balRefresh.addEventListener('click', async () => {
                    balRefresh.disabled = true;
                    balRefresh.textContent = 'Refreshing…';
                    const res = await fetchJSON('/api/inference/balances?fresh=1');
                    if (res) {
                        this.state.balances = res.balances || [];
                        this.state.loadErrors = (this.state.loadErrors || []).filter((k) => k !== 'balances');
                    }
                    balRefresh.disabled = false;
                    balRefresh.textContent = 'Refresh';
                    this._renderBalances();
                });
            }
        }
    }

    window.ModelPicker = {
        mount(rootEl, opts) { return new Picker(rootEl, opts); },
    };
})();
