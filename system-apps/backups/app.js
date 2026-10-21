/* ================================================================
 * Backups — the owner's own backups, on their own Host.
 *
 *   Your recovery key   made here in the browser (recovery-key.js), shown
 *                       once, confirmed, and only its public half sent;
 *                       or one the owner already has, the same way.
 *   On this machine     the backups in the Host's folder, each downloaded
 *                       as one file, and an export of the whole Host.
 *   Restore from a file a downloaded backup or another Host's export, its
 *                       key sealed to this Host in the browser
 *                       (restore-file.js), then restored with the owner's
 *                       code.
 *   The bastion's copy  read-only, as before (/api/backup/local).
 *
 * Every route here is the owner's alone (walled from the Host); the ones
 * that change who backups go to, or replace the Host, take their code.
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


    function $(id) { return document.getElementById(id); }

    function oneLine(s) {
        return String(s == null ? '' : s).replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g, ' ');
    }

    function when(iso) {
        return iso ? new Date(iso).toLocaleString() : '';
    }

    async function fetchJSON(url, opts) {
        const r = await fetch(url, opts || {});
        if (!r.ok) {
            const body = await r.text().catch(function () { return ''; });
            let detail = body;
            try { detail = (JSON.parse(body) || {}).detail || body; } catch (_) { /* not JSON */ }
            if (detail && typeof detail === 'object') detail = detail.detail || detail.reason_text || JSON.stringify(detail);
            const err = new Error(oneLine(detail || ('HTTP ' + r.status)));
            err.status = r.status;
            throw err;
        }
        return r.json();
    }

    function say(id, text) {
        const el = $(id);
        if (el) el.textContent = oneLine(text);
    }

    // Using a recovery key, and restoring, take a code from the second
    // factor; the owner enrols one in the Network app.
    const ENROL_FIRST = 'Using a recovery key takes a code from your second factor, and you have not set '
        + 'one up yet: open the Network app, click "Second factor: Enroll" at the top, scan the code with '
        + 'an authenticator app, then come back here.';

    // What a restore brings back and what it leaves (server/host_restore.py
    // CARRIED_TABLES), said before the owner agrees to it.
    const RESTORE_ACK = 'This file is my backup. I understand that my Host goes back to it and restarts '
        + 'to do it: its memory, files, credentials and the data of every app come back as the file has '
        + 'them, and access stays as it is now (my bastions, grants, second factor and backup keys). '
        + 'If another Host made the file, this Host cannot confirm it, and restores it because I say it is mine.';

    // ── the recovery key ─────────────────────────────────────────────────
    let keyInfo = null;        // GET /api/backup/settings .key
    let pending = null;        // {identity, recipient, fingerprint, label, replace, how}

    function forget() {
        // The private half goes: it is on the owner's paper or disk now.
        if (pending) pending.identity = '';
        pending = null;
        ['key-text', 'key-qr', 'key-fp'].forEach(function (id) { const el = $(id); if (el) el.textContent = ''; });
        ['key-paste', 'key-code'].forEach(function (id) { const el = $(id); if (el) el.value = ''; });
        ['key-paste-file'].forEach(function (id) { const el = $(id); if (el) el.value = ''; });
        const saved = $('key-saved');
        if (saved) saved.checked = false;
        ['key-new', 'key-existing', 'key-use', 'key-confirm'].forEach(function (id) { $(id).hidden = true; });
        $('key-actions').hidden = false;
    }

    function renderKey() {
        const k = keyInfo || {};
        const rec = k.recovery;
        const lines = [];
        if (k.registered === null || k.registered === undefined) {
            lines.push('Cannot read your recovery key right now' + (k.reason ? ' (' + k.reason + ').' : '.'));
        } else if (rec) {
            lines.push('Your recovery key: ' + rec.fingerprint + (rec.label ? ' (' + rec.label + ')' : '') + '. '
                + (rec.made_on_host ? 'You made it here, on this Host.' : 'It came from your account page.')
                + ' Every backup is encrypted to it.');
            if (k.recovery_offer) {
                lines.push(rec.made_on_host
                    ? 'Your account page also holds a recovery key for this Host (' + k.recovery_offer.fingerprint
                      + '). This Host keeps using the key you made here; to use that one instead, confirm it in '
                      + 'Settings, Backup.'
                    : 'Your account page now offers a different recovery key (' + k.recovery_offer.fingerprint
                      + '); confirm it in Settings, Backup, or make one here instead.');
            }
            (k.recovery_older || []).length && lines.push('Older recovery keys still open the backups taken '
                + 'while they were in use: keep them too.');
        } else {
            lines.push('Your Host takes no backups until it has a recovery key. Make one here.');
            if (k.recovery_offer) {
                lines.push('Your account page holds one for this Host (' + k.recovery_offer.fingerprint
                    + '); you can confirm that one in Settings, Backup instead.');
            }
        }
        if (k.stepup_enrolled === false) lines.push(ENROL_FIRST);
        say('key-state', lines.join('\n'));
        if (!pending) {
            $('key-actions').hidden = false;
            $('key-make').textContent = rec ? 'Make a new recovery key' : 'Make a recovery key';
        }
    }

    async function loadKey() {
        try {
            keyInfo = (await fetchJSON('/api/backup/settings')).key || {};
        } catch (e) {
            keyInfo = { registered: null, reason: e.message };
        }
        renderKey();
    }

    function replacing() {
        return !!(keyInfo && keyInfo.recovery);
    }

    async function makeKey() {
        const K = window.BackupsKey;
        if (!K) { say('key-status', 'This page could not load its key module. Reload it.'); return; }
        if (replacing() && !window.confirm('Make a new recovery key? Backups you already have stay encrypted '
            + 'to the key you have now, so keep that one too.')) return;
        let key;
        try {
            key = await K.makeRecoveryKey();
        } catch (e) {
            say('key-status', 'This browser cannot make the key. Update it, or use another browser, and try again.');
            return;
        }
        pending = Object.assign(key, { label: K.recoveryLabel(), replace: replacing(), how: 'made' });
        $('key-text').textContent = key.identity;
        try {
            $('key-qr').innerHTML = K.qrSvg(key.identity);
        } catch (e) {
            $('key-qr').textContent = '';
        }
        $('key-fp').textContent = 'Fingerprint ' + key.fingerprint + '. The file and the printout carry it, so you '
            + 'can always tell which key is which.';
        $('key-actions').hidden = true;
        $('key-new').hidden = false;
        $('key-show').hidden = false;
        say('key-status', '');
    }

    function saveKeyFile() {
        const K = window.BackupsKey;
        if (!pending || !pending.identity || !K) return;
        const blob = new Blob([K.keyFileText(pending.label, pending)], { type: 'text/plain' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = K.keyFileName(pending);
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 60000);
    }

    function onSavedTick() {
        const on = $('key-saved').checked;
        // The key is hidden while the owner shows they have it.
        $('key-show').hidden = on;
        $('key-confirm').hidden = !on;
        $('key-use').hidden = !on;
    }

    async function readFileText(input) {
        const f = input.files && input.files[0];
        if (!f) return '';
        if (f.size > 65536) return '';
        return f.text();
    }

    async function haveKey() {
        pending = { identity: '', how: 'existing', replace: replacing() };
        $('key-actions').hidden = true;
        $('key-existing').hidden = false;
        $('key-use').hidden = false;
        say('key-status', '');
    }

    async function useKey() {
        const K = window.BackupsKey;
        if (!pending || !K) return;
        const code = ($('key-code').value || '').trim();
        if (!/^[0-9]{6}$/.test(code)) { say('key-status', 'Enter the six-digit code from your authenticator.'); return; }
        let key = pending;
        if (pending.how === 'made') {
            if (!$('key-saved').checked) { say('key-status', 'Save the key first, then tick that you saved it.'); return; }
        } else {
            const text = ($('key-paste').value || '') || await readFileText($('key-paste-file'));
            try {
                key = Object.assign(await K.keyFromIdentity(text), { replace: pending.replace,
                    label: 'Recovery key added on this Host, ' + new Date().toISOString().slice(0, 10) });
            } catch (e) {
                say('key-status', e.message);
                return;
            }
            if (keyInfo && keyInfo.recovery && keyInfo.recovery.fingerprint === key.fingerprint) {
                say('key-status', 'That is the recovery key this Host already uses.');
                forget();
                return;
            }
            if (pending.replace && !window.confirm('Use this recovery key (' + key.fingerprint + ') from now on? '
                + 'Backups you already have stay encrypted to the key you have now, so keep that one too.')) return;
        }
        $('key-go').disabled = true;
        say('key-status', 'Saving…');
        try {
            const r = await K.useKey({ fetchJSON: fetchJSON, recipient: key.recipient, fingerprint: key.fingerprint,
                                       label: key.label, stepupCode: code, replace: key.replace });
            forget();
            say('key-status', r.note || 'Done.');
            await loadKey();
            loadFiles();
        } catch (e) {
            say('key-status', 'Could not use that key: ' + e.message);
        } finally {
            $('key-go').disabled = false;
            if (key !== pending) key.identity = '';
        }
    }

    // ── the backups on this machine ──────────────────────────────────────
    let files = null;
    const WHERE = { local: 'a folder on this machine', s3: 'your own bucket', network: "the network's storage" };

    // How the backups are going (server/host_backup.py backup_status), said
    // at the top whenever they are not working: a Host restored onto a new
    // machine has no recovery key until the owner adds theirs again.
    function renderStatus(v) {
        const box = $('bk-status');
        if (!box) return;
        const quiet = !v || v.state === 'working' || v.state === 'none_yet';
        box.hidden = quiet;
        box.setAttribute('data-state', (v && v.state) || '');
        if (quiet) return;
        say('bk-status-head', v.headline + '.');
        say('bk-status-when', v.last_finished_at
            ? 'The last backup finished ' + when(v.last_finished_at) + '.'
            : 'No backup has finished yet.');
        say('bk-status-detail', v.detail || '');
    }

    function renderFiles() {
        const f = files || {};
        renderStatus(f.status);
        const where = WHERE[f.where] || 'where Settings, Backup says';
        say('files-where', 'Your backups go to ' + where
            + (f.where === 'local' ? ', which keeps the newest ' + f.local_keep + '.' : '.')
            + ' Change that in Settings, Backup. A download is one file holding a backup and its key file.');
        const list = $('files-list');
        list.innerHTML = '';
        const rows = (f.backups || []).map(function (b) { return Object.assign({ what: 'Backup' }, b); })
            .concat((f.uploads || []).map(function (b) { return Object.assign({ what: 'Uploaded' }, b); }));
        if (!rows.length) {
            const d = document.createElement('div');
            d.className = 'label-sys';
            d.textContent = f.where === 'local'
                ? 'No backups on this machine yet. Your Host takes its first one at its next scheduled time, or '
                  + 'when you press Back up now in Settings, Backup.'
                : 'None on this machine: your backups are kept elsewhere. An export below makes one here.';
            list.appendChild(d);
        }
        rows.forEach(function (b) {
            const row = document.createElement('div');
            row.className = 'file-row';
            const t = document.createElement('span');
            t.className = 'when';
            t.textContent = b.what + ' of ' + when(b.taken_at);
            const s = document.createElement('span');
            s.className = 'size';
            s.textContent = formatBytes(b.bytes);
            const btn = document.createElement('button');
            btn.className = 'cseries-btn ghost';
            btn.textContent = 'Download';
            btn.onclick = function () { download(b, btn); };
            row.appendChild(t); row.appendChild(s); row.appendChild(btn);
            list.appendChild(row);
        });
        const ex = f.export || null;
        const exp = (f.exports || [])[0];
        const go = $('export-go');
        go.disabled = !!(ex && ex.state === 'running') || f.running;
        $('export-state').innerHTML = '';
        if (ex && ex.state === 'running') {
            say('export-state', 'Making the export… This page follows it.');
            setTimeout(loadFiles, 5000);
        } else if (ex && ex.state === 'failed') {
            say('export-state', 'The last export was not made: ' + (ex.error || 'it failed') + '.');
        } else if (exp) {
            const line = document.createElement('span');
            line.textContent = oneLine('Your export of ' + when(exp.taken_at) + ' (' + formatBytes(exp.bytes)
                + ') is ready. It is kept here for a week. ');
            const btn = document.createElement('button');
            btn.className = 'cseries-btn';
            btn.textContent = 'Download the export';
            btn.onclick = function () { download(exp, btn); };
            $('export-state').appendChild(line);
            $('export-state').appendChild(btn);
        }
    }

    async function loadFiles() {
        try {
            files = await fetchJSON('/api/backup/files');
        } catch (e) {
            files = null;
            say('files-where', 'Could not read the backups on this machine: ' + e.message);
            return;
        }
        renderFiles();
    }

    async function download(b, btn) {
        const R = window.BackupsRestore;
        if (!R) return;
        btn.disabled = true;
        const was = btn.textContent;
        btn.textContent = 'Downloading…';
        try {
            const base = '/api/backup/files/' + encodeURIComponent(b.stamp);
            const q = b.folder ? '?folder=' + encodeURIComponent(b.folder) : '';
            const how = await R.downloadFile({ fetch: fetch, url: base + '/download' + q, linkUrl: base + '/link' + q,
                                               name: 'cseries-backup-' + b.stamp + '.tar' });
            if (how === 'started') {
                say('files-dl', 'Your browser is downloading it: it shows in the browser\'s downloads.');
            }
        } catch (e) {
            window.alert('Could not download it: ' + e.message);
        } finally {
            btn.disabled = false;
            btn.textContent = was;
        }
    }

    async function exportNow() {
        $('export-go').disabled = true;
        try {
            const r = await fetchJSON('/api/backup/export', { method: 'POST' });
            say('export-state', r.note || (r.running ? 'An export is already being made.' : 'Making the export…'));
            setTimeout(loadFiles, 3000);
        } catch (e) {
            say('export-state', 'Could not make an export: ' + e.message);
            $('export-go').disabled = false;
        }
    }

    // ── restore from a file ──────────────────────────────────────────────
    let prepared = null;       // {stamp, resealed, bytes}

    async function uploadForRestore() {
        const R = window.BackupsRestore;
        if (!R) { say('rf-status', 'This page could not load its restore module. Reload it.'); return; }
        const chosen = $('rf-files').files;
        if (!chosen || !chosen.length) { say('rf-status', 'Choose the backup file first.'); return; }
        let identity = ($('rf-key').value || '') || await readFileText($('rf-key-file'));
        if (!identity) { say('rf-status', 'Give this page your recovery key, or choose its key file.'); return; }
        $('rf-key').value = '';
        $('rf-key-file').value = '';
        $('rf-upload').disabled = true;
        $('rf-restore').hidden = true;
        prepared = null;
        say('rf-status', 'Checking your recovery key opens it…');
        try {
            prepared = await R.prepareFromFile({
                fetch: fetch, fetchJSON: fetchJSON, files: chosen, identity: identity,
                confirmOtherHost: function (from, here) {
                    return window.confirm('This backup is from Host #' + from + ', and this is Host #' + here
                        + '. Restore it here, in place of everything on this Host?');
                },
                onProgress: function (sent, total) {
                    say('rf-status', 'Your recovery key opens it. Sending it to this Host: '
                        + formatBytes(sent) + ' of ' + formatBytes(total) + '…');
                } });
            identity = null;
            say('rf-status', 'This Host has the file, and its key sealed to it. Agree below and enter your code '
                + 'to restore it.');
            $('rf-ack-text').textContent = RESTORE_ACK;
            $('rf-restore').hidden = false;
            loadFiles();
        } catch (e) {
            say('rf-status', 'Not sent: ' + e.message);
        } finally {
            identity = null;
            $('rf-upload').disabled = false;
        }
    }

    async function restoreNow() {
        const R = window.BackupsRestore;
        if (!R || !prepared) return;
        if (!$('rf-ack').checked) { say('rf-status', 'Tick the box first.'); return; }
        const code = ($('rf-code').value || '').trim();
        if (!/^[0-9]{6}$/.test(code)) { say('rf-status', 'Enter the six-digit code from your authenticator.'); return; }
        $('rf-go').disabled = true;
        try {
            await R.restoreUploaded({ fetchJSON: fetchJSON, stamp: prepared.stamp, resealed: prepared.resealed,
                                      stepupCode: code, unverifiedOk: true });
            $('rf-code').value = '';
            $('rf-ack').checked = false;
            say('rf-status', 'Restoring. Your Host backs up how it is now first, then restarts onto the file; '
                + 'Settings, Backup shows how it goes.');
            prepared = null;
            $('rf-restore').hidden = true;
        } catch (e) {
            say('rf-status', 'Not restored: ' + e.message);
        } finally {
            $('rf-go').disabled = false;
        }
    }

    // ── the bastion's own copy (as before) ───────────────────────────────
    async function loadBastion() {
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


    function loadAll() {
        loadKey();
        loadFiles();
        loadBastion();
    }

    document.addEventListener('DOMContentLoaded', function () {
        loadAll();
        setInterval(function () {
            if (document.visibilityState === 'visible' && !pending) loadBastion();
        }, 30000);
        $('refresh-btn').addEventListener('click', loadAll);
        $('key-make').addEventListener('click', makeKey);
        $('key-have').addEventListener('click', haveKey);
        $('key-save').addEventListener('click', saveKeyFile);
        $('key-print').addEventListener('click', function () { window.print(); });
        $('key-saved').addEventListener('change', onSavedTick);
        $('key-go').addEventListener('click', useKey);
        $('key-cancel').addEventListener('click', function () { forget(); say('key-status', ''); renderKey(); });
        $('export-go').addEventListener('click', exportNow);
        $('rf-upload').addEventListener('click', uploadForRestore);
        $('rf-go').addEventListener('click', restoreNow);
    });
})();
