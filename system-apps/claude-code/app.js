/* Claude Code app — UI shell.
 *
 * Two-state view:
 *   - disconnected → how to connect (Anthropic's own login, then Keys)
 *   - connected    → connected since + disconnect
 *
 * Nothing runs here. The sidecar's POST /sessions (an agent run inside the
 * Host's container) is retired and answers 410, so the sessions list, the
 * Close buttons, the session and linked-ticket counts and the "recent runs"
 * list went with it — none of them could ever show anything again.
 *
 * This app hosts NO login. The connect flow it used to drive (OAuth CTA →
 * popup → paste authorization code → sidecar → platform frontend PKCE
 * exchange) was a platform-hosted Anthropic login and was deleted
 * 2026-08-15, frontend route included. The credential arrives the same way
 * every other builder's does: the user's own, via the Keys app.
 */
(() => {
    const els = {
        statusPill: document.getElementById('status-pill'),
        statusLine: document.getElementById('connection-status-line'),
        cardDisconnected: document.getElementById('card-disconnected'),
        cardConnected: document.getElementById('card-connected'),
        cardOffline: document.getElementById('card-offline'),

        btnDisconnect: document.getElementById('btn-disconnect'),

        disconnectedError: document.getElementById('disconnected-error'),
        pendingError: document.getElementById('pending-error'),

        connectedSince: document.getElementById('connected-since'),
        lastUpdate: document.getElementById('last-update'),
        offlineMessage: document.getElementById('offline-message'),
    };

    const state = {
        entityId: null,
        connection: null,
        // 'loading' | 'disconnected' | 'pending' | 'connected' | 'offline'
        view: 'loading',
    };

    async function tryFetch(url, opts) {
        try {
            const r = await fetch(url, opts);
            if (!r.ok) {
                const text = await r.text().catch(() => '');
                return { ok: false, status: r.status, body: text };
            }
            return { ok: true, data: await r.json() };
        } catch (e) {
            return { ok: false, network: true, error: String(e) };
        }
    }

    function sidecarUrl(path) {
        // Sidecar is reverse-proxied at /system-apps/claude-code/<path>.
        return path;
    }

    // ---- View switching ----

    function setView(name) {
        state.view = name;
        els.cardDisconnected.hidden = name !== 'disconnected';
        els.cardConnected.hidden = name !== 'connected';
        els.cardOffline.hidden = name !== 'offline';

        if (name === 'connected') {
            els.statusPill.textContent = 'connected';
            els.statusPill.className = 'cseries-pill allow';
        } else if (name === 'offline') {
            els.statusPill.textContent = 'offline';
            els.statusPill.className = 'cseries-pill';
        } else if (name === 'pending') {
            els.statusPill.textContent = 'pending';
            els.statusPill.className = 'cseries-pill';
        } else {
            els.statusPill.textContent = 'disconnected';
            els.statusPill.className = 'cseries-pill';
        }
    }

    function showError(view, msg) {
        const el = view === 'pending' ? els.pendingError : els.disconnectedError;
        if (!msg) {
            el.hidden = true;
            el.textContent = '';
            return;
        }
        el.textContent = msg;
        el.hidden = false;
    }

    // ---- Init ----

    async function init() {
        // Default to the disconnected card so the page is never blank
        // while the sidecar is warming up + /status hasn't returned yet.
        // refresh() will upgrade to 'connected' if a token is cached.
        // The pill reads "checking…" until the first /status resolves so
        // the initial frame isn't mistaken for a confirmed disconnected state.
        setView('disconnected');
        els.statusPill.textContent = 'checking…';
        await loadEntityId();
        await refresh();
        setInterval(refresh, 8000);

        els.btnDisconnect.addEventListener('click', disconnect);
    }

    async function loadEntityId() {
        const r = await tryFetch('/api/system/status');
        if (r.ok && r.data && r.data.entity_id) state.entityId = r.data.entity_id;
    }

    // ---- Status poll ----

    let consecutiveFailures = 0;

    async function refresh() {
        const ts = new Date();
        els.lastUpdate.textContent = ts.toLocaleTimeString([], {
            hour: '2-digit', minute: '2-digit', second: '2-digit',
        });

        const h = await tryFetch(sidecarUrl('status'));
        if (!h.ok) {
            consecutiveFailures += 1;
            if (consecutiveFailures >= 3) setView('offline');
            return;
        }
        consecutiveFailures = 0;
        state.connection = h.data;

        const connected = h.data && h.data.connected;
        if (connected) {
            renderConnected(h.data);
            els.statusLine.textContent = h.data.expires_at
                ? 'token expires ' + new Date(h.data.expires_at).toLocaleString()
                : 'subscription connected';
            setView('connected');
        } else if (state.view === 'pending') {
            // Stay on pending while the user finishes the paste-flow.
            els.statusLine.textContent = 'waiting for authorization code';
        } else {
            setView('disconnected');
            els.statusLine.textContent = '';
        }
    }

    function renderConnected(c) {
        els.connectedSince.textContent = c.connected_at
            ? 'since ' + new Date(c.connected_at).toLocaleString()
            : '';
    }

    // ---- Disconnect ----

    async function disconnect() {
        if (!confirm('Disconnect your Claude subscription?')) return;
        await tryFetch(sidecarUrl('disconnect'), { method: 'POST' }).catch(() => {});
        await refresh();
    }

    document.addEventListener('DOMContentLoaded', init);
})();
