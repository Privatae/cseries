/* ================================================================
 * App Template — client logic.
 *
 * A minimal starting point. Copy + adapt. The app runs inside an
 * iframe served from /system-apps/<id>/. Use fetch('/api/...') for
 * backend calls; same-origin is enabled in the iframe sandbox.
 * ================================================================ */

(function () {
    'use strict';

    // ── State ────────────────────────────────────────────────
    const state = {
        view: 'main',
    };

    // ── View switching ───────────────────────────────────────
    function showView(view) {
        state.view = view;
        document.querySelectorAll('.cseries-nav-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.view === view);
        });
        document.querySelectorAll('.app-view').forEach(v => {
            v.hidden = v.id !== `view-${view}`;
        });
    }

    // ── Fetch helper ─────────────────────────────────────────
    async function fetchJSON(url, opts) {
        const r = await fetch(url, opts || {});
        if (!r.ok) throw new Error(`${url} → ${r.status}`);
        return r.json();
    }

    // ── Utility: safe HTML/attr escape ───────────────────────
    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    function attr(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // ── Boot ─────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('.cseries-nav-btn').forEach(btn => {
            btn.addEventListener('click', () => showView(btn.dataset.view));
        });
    });

    // Expose for debugging
    window.App = { state, showView, fetchJSON };
})();
