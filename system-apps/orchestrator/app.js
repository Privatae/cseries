(function () {
    'use strict';

    // Orchestrator — the single orchestration surface. Every model and
    // harness decision is made HERE, and every decision is independent:
    //   1. Text roles — one now (chat, shown as Host model; review went
    //      2026-10-20 with the diff reviewer) — picks PROVIDER → MODEL from
    //      the user's per-provider favorites, plus an optional FALLBACK
    //      PROVIDER that answers when the primary provider errors or doesn't
    //      host the model. Down from eight: plan, research,
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

    async function mountPicker() {
        const host = document.getElementById('model-picker-host');
        if (!host || !window.ModelPicker) return;
        // Bundle-local data; a failed fetch degrades to Simple-without-setup
        // (the overlay still works, Advanced is one click away).
        let recommendations = null;
        try {
            const r = await fetch('recommended-routes.json');
            if (r.ok) recommendations = await r.json();
        } catch (e) { /* degrade */ }
        window.ModelPicker.mount(host, {
            showBalances: true,
            showRoutingTab: true,
            showCoding: true,      // the coding decision is made here; Workbench consumes it
            defaultTab: 'routing', // decisions first; the Models tab is the browser
            simple: true,          // Simple/Advanced switch; Simple is the default
            recommendations,
        });
    }

    document.addEventListener('DOMContentLoaded', mountPicker);
}());
