(function () {
    'use strict';

    // Orchestrator — the single orchestration surface. Every model and
    // harness decision is made HERE, and every decision is independent:
    //   1. Text roles — one now (chat, shown as Host model; review went
    //      2026-10-20 with the diff reviewer) — picks PROVIDER → MODEL from
    //      the user's per-provider favorites, plus an optional FALLBACK
    //      PROVIDER that answers when the primary provider errors, doesn't
    //      host the model, or its safety filter declines the request. Down
    //      from eight: plan, research,
    //      agents, tutor and journal were seats the user had to fill for
    //      lanes that mostly never fired, and each of those lanes now runs
    //      on chat, named at its own call site. The Memory role
    //      (consolidation) went: memory forms in the host's turn.
    //   2. Coding — the harness multi-select + per-harness model/effort;
    //      the Workbench consumes the selection for every fan-out.
    //   3. Embedding — provider → model, same as text roles: a machine the
    //      user runs (local) or a keyed provider's embedding models
    //      (the managed "Privatae Endpoint" pick is gone).
    // All three live in the shared ModelPicker; the Routing tab is the default
    // view. API keys live in the Keys app — this app never touches credentials.
    //
    // SIMPLE MODE: the app opens on a Simple surface
    // — a plain overlay ("what answers when") + one-click recommended routing
    // per keyed provider — and everything above becomes Advanced, unchanged.
    // Recommendations ship as DATA beside this file (recommended-routes.json):
    // applying them writes ordinary, visible model_routing entries; nothing in
    // the platform reads the file at inference time, so the no-pins doctrine
    // (no default model in code) stands.

    // The window token arrives after first paint (the App SDK's init
    // handshake; on this app's own origin, app-frame-init.js mints it). Every
    // read the picker makes at mount needs it: mounted before it, each one got
    // 401 "application token required" and the window showed nothing. The SDK
    // also holds early /api/ requests; this wait is bounded the same way and
    // never blocks a window with no SDK (opened on its own).
    function tokenReady() {
        const sdk = window.CSeriesApp;
        if (!sdk || !sdk.ready || window.parent === window) return Promise.resolve();
        return Promise.race([sdk.ready, new Promise((res) => setTimeout(res, 10000))]);
    }

    async function mountPicker() {
        const host = document.getElementById('model-picker-host');
        if (!host || !window.ModelPicker) return;
        await tokenReady();
        // Bundle-local data; a failed fetch degrades to Simple-without-setup
        // (the overlay still works, Advanced is one click away).
        let recommendations = null;
        try {
            const r = await fetch('recommended-routes.json');
            if (r.ok) recommendations = await r.json();
        } catch (e) { /* degrade */ }
        const picker = window.ModelPicker.mount(host, {
            showBalances: true,
            showRoutingTab: true,
            showCoding: true,      // the coding decision is made here; Workbench consumes it
            defaultTab: 'routing', // decisions first; the Models tab is the browser
            simple: true,          // Simple/Advanced switch; Simple is the default
            recommendations,
        });
        watchForChanges(picker);
    }

    // What the picker draws from that changes OUTSIDE this window: which
    // providers have a key (added in Keys, which Open Keys opens), which
    // coding engines have theirs, which models the person's machines serve,
    // and which model chat runs on. The picker re-reads only after its
    // own actions, so a key added in Keys left this window saying "add key in
    // Keys" with no models until it was closed and reopened. When the window
    // comes back into focus, read both again and redraw only if they changed:
    // a redraw on every focus would close a select the person is opening.
    async function signature() {
        try {
            const [r, l] = await Promise.all([
                fetch('/api/inference/routing', { cache: 'no-store' }),
                fetch('/api/inference/local-models', { cache: 'no-store' }),
            ]);
            if (!r.ok || !l.ok) return null;
            const routing = await r.json();
            const local = await l.json();
            const keys = (routing.providers || []).map((p) => p.id + ':' + (p.has_key ? 1 : 0)).sort();
            const models = ((local && local.local_models) || []).map((m) => m.bastion + '/' + m.id).sort();
            // A coding engine's key added in Keys: its row reads connected.
            const engines = (routing.engine_keys || []).slice().sort();
            // Chat's model, so a default set in the meantime is shown.
            const chat = ((routing.effective || {}).chat) || {};
            return JSON.stringify([keys, models, engines, (chat.provider || '') + '/' + (chat.model || '')]);
        } catch (e) { return null; }
    }

    function watchForChanges(picker) {
        if (!picker || typeof picker.reload !== 'function') return;
        let last = null;
        let busy = false;
        signature().then((s) => { last = s; });
        const recheck = async () => {
            if (busy || document.visibilityState === 'hidden') return;
            busy = true;
            try {
                const now = await signature();
                if (now && last && now !== last) await picker.reload();
                if (now) last = now;
            } finally { busy = false; }
        };
        window.addEventListener('focus', recheck);
        document.addEventListener('visibilitychange', recheck);
    }

    document.addEventListener('DOMContentLoaded', mountPicker);
}());
