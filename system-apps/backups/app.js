/* ================================================================
 * Backups — read-only status of the gateway-managed memory backup.
 *
 * The gateway daemon (user side) writes `local_backup_envelope`
 * and `local_backup_blob` rows into this container's PG. This view
 * reads /api/backup/local and renders. No actions, no triggers.
 * ================================================================ */

(function () {
    'use strict';

    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function formatBytes(b) {
        if (!b) return '0 B';
        if (b < 1024) return b + ' B';
        if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB';
        if (b < 1024 * 1024 * 1024) return (b / (1024 * 1024)).toFixed(1) + ' MB';
        return (b / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
    }

    function formatCount(n) {
        if (n == null) return '0';
        if (n < 1000) return String(n);
        if (n < 1000000) return (n / 1000).toFixed(1) + 'k';
        return (n / 1000000).toFixed(1) + 'M';
    }

    function relTime(iso) {
        if (!iso) return 'never';
        const then = new Date(iso).getTime();
        const now = Date.now();
        const s = Math.max(0, Math.floor((now - then) / 1000));
        if (s < 60) return s + 's ago';
        if (s < 3600) return Math.floor(s / 60) + 'm ago';
        if (s < 86400) return Math.floor(s / 3600) + 'h ago';
        return Math.floor(s / 86400) + 'd ago';
    }

    async function load() {
        try {
            const r = await fetch('/api/backup/local');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const data = await r.json();
            render(data);
        } catch (err) {
            console.error('Backup status load failed', err);
            renderError(err.message || String(err));
        }
    }

    function renderError(msg) {
        const card = document.getElementById('status-card');
        if (card) {
            card.innerHTML =
                '<div class="status-row"><div class="status-title">Status unavailable</div>' +
                '<div class="status-sub">' + esc(msg) + '</div></div>';
        }
        const refreshed = document.getElementById('footer-refreshed');
        if (refreshed) refreshed.textContent = 'error';
    }

    function renderUnconfigured(card, list, summary) {
        card.innerHTML =
            '<div class="status-row">' +
                '<div class="status-pill warn">Not configured</div>' +
                '<div class="status-text">' +
                    '<div class="status-title">No local backup yet</div>' +
                    '<div class="status-sub">' +
                        'Open your bastion to set a passphrase. ' +
                        'Your memory stays here until that\'s done, but it\'s not yet encrypted off-machine.' +
                    '</div>' +
                '</div>' +
            '</div>';
        list.innerHTML =
            '<div class="cseries-empty">' +
                '<span class="empty-title">Nothing synced yet</span>' +
                'Once your gateway is configured, encrypted rows will start flowing in.' +
            '</div>';
        summary.textContent = 'not configured';
    }

    function renderConfigured(card, list, summary, data) {
        const hasPass = !!data.envelopes.passphrase;
        const hasRecov = !!data.envelopes.recovery_code;

        const pillPass = hasPass
            ? '<span class="status-pill ok">Passphrase set</span>'
            : '<span class="status-pill warn">No passphrase</span>';
        const pillRecov = hasRecov
            ? '<span class="status-pill ok">Recovery code stored</span>'
            : '<span class="status-pill warn">No recovery code</span>';

        const kdf = data.envelopes.passphrase || data.envelopes.recovery_code;
        const kdfLine = kdf
            ? '<span class="kdf-line">' + esc(kdf.kdf_algo || 'argon2id') + ' &middot; AES-256-GCM</span>'
            : '';
        const enrolled = data.envelopes.passphrase && data.envelopes.passphrase.created_at;
        const enrolledLine = enrolled
            ? '<div class="status-meta">Key enrolled ' + esc(relTime(enrolled)) + '</div>'
            : '';

        card.innerHTML =
            '<div class="status-row">' +
                '<div class="status-pills">' + pillPass + pillRecov + '</div>' +
                '<div class="status-text">' +
                    '<div class="status-title">' +
                        formatCount(data.blob_count) + ' encrypted rows' +
                        ' &middot; ' + formatBytes(data.blob_bytes) +
                    '</div>' +
                    '<div class="status-sub">' +
                        'Last sync ' + esc(relTime(data.last_sync_at)) +
                        ' &middot; ' + kdfLine +
                    '</div>' +
                    enrolledLine +
                '</div>' +
            '</div>' +
            (hasRecov ? '' :
                '<div class="status-callout">' +
                    'No recovery code on file. If you forget your passphrase, your encrypted memory cannot be recovered. ' +
                    'Print one from your gateway.' +
                '</div>'
            );

        if (!data.by_kind.length) {
            list.innerHTML =
                '<div class="cseries-empty">' +
                    '<span class="empty-title">Configured, awaiting first sync</span>' +
                    'Rows will appear here as your gateway pushes them.' +
                '</div>';
        } else {
            list.innerHTML = data.by_kind.map(function (k) {
                return '' +
                    '<div class="kind-row cseries-row">' +
                        '<div class="row-meta">' +
                            '<div class="row-title">' + esc(k.resource_kind) + '</div>' +
                            '<div class="row-sub">last synced ' + esc(relTime(k.last_synced)) + '</div>' +
                        '</div>' +
                        '<div class="row-actions">' +
                            '<span class="kind-count">' + formatCount(k.count) + '</span>' +
                        '</div>' +
                    '</div>';
            }).join('');
        }

        summary.innerHTML =
            formatCount(data.blob_count) + ' rows &middot; ' + formatBytes(data.blob_bytes);
    }

    function render(data) {
        const card = document.getElementById('status-card');
        const list = document.getElementById('by-kind-list');
        const summary = document.getElementById('footer-summary');
        const refreshed = document.getElementById('footer-refreshed');
        if (!card || !list || !summary) return;

        if (data.reason === 'brain_not_initialized') {
            renderError('Backup service is starting up. The memory store is not yet available — this is a server-side state, not something to configure.');
            list.innerHTML =
                '<div class="cseries-empty">' +
                    '<span class="empty-title">Unavailable</span>' +
                '</div>';
            summary.textContent = 'unavailable';
            return;
        }
        if (data.reason === 'schema_not_initialized') {
            renderError('Backup storage has not been provisioned yet. This is a server-side state — your gateway passphrase is not the issue.');
            list.innerHTML =
                '<div class="cseries-empty">' +
                    '<span class="empty-title">Unavailable</span>' +
                '</div>';
            summary.textContent = 'unavailable';
            return;
        }

        if (!data.configured) {
            renderUnconfigured(card, list, summary);
        } else {
            renderConfigured(card, list, summary, data);
        }

        if (refreshed) {
            refreshed.textContent = 'refreshed ' + new Date().toLocaleTimeString([], {
                hour: '2-digit', minute: '2-digit'
            });
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        load();
        setInterval(function () {
            if (document.visibilityState === 'visible') load();
        }, 30000);

        const refreshBtn = document.getElementById('refresh-btn');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', function () {
                load();
            });
        }
    });
})();
