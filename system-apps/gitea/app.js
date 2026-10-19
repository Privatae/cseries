/* ================================================================
 * Gitea — four panels over the configured Gitea forge, all on the app's own
 * authenticated gitea client (server/routes/gitea.py -> gitea_client).
 *
 *   Your repos   — the logged-in user's repositories (browse files/commits/branches)
 *   Host repos   — the host's own gitea identity (its clean room)
 *   Permissions  — grant / revoke the HOST's access to your repos (Read | Write),
 *                  by adding/removing it as a collaborator. In-app, no links.
 *   Mirrors      — GitHub → gitea pull-mirrors (one-way, agent-free). Mirror
 *                  rows open the same in-app browser as any other repo.
 *
 * No user token in the app; the app is authenticated to gitea and makes the calls.
 * No mock data; degrade, don't crash.
 * ================================================================ */

(function () {
    'use strict';

    const state = {
        view: 'user',         // 'user' | 'host' | 'perms' | 'mirrors'
        returnView: 'user',   // which list the browse detail returns to
        repo: null,           // { owner, name, full_name }
        pane: 'files',
        path: '',
        gh: null,             // last /github/status payload (mirrors view)
    };

    // ── helpers ──────────────────────────────────────────────
    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    const $ = (id) => document.getElementById(id);

    async function api(url, opts) {
        const o = opts || {};
        if (o.body && !(o.headers && o.headers['Content-Type'])) {
            o.headers = Object.assign({ 'Content-Type': 'application/json' }, o.headers || {});
        }
        const r = await fetch(url, o);
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
            const err = new Error((body && body.detail) || `HTTP ${r.status}`);
            err.status = r.status;
            throw err;
        }
        return body;
    }

    function fmtRel(iso) {
        if (!iso) return '';
        const then = Date.parse(iso);
        if (Number.isNaN(then)) return '';
        const secs = Math.max(0, (Date.now() - then) / 1000);
        const units = [['y', 31536000], ['mo', 2592000], ['d', 86400], ['h', 3600], ['m', 60]];
        for (const [label, size] of units) {
            const n = Math.floor(secs / size);
            if (n >= 1) return `${n}${label} ago`;
        }
        return 'just now';
    }

    // "8h0m0s" → "8h"; degrade to the raw string rather than hide it.
    function fmtInterval(s) {
        const m = /^(\d+)h(?:0m0s)?$/.exec(String(s || ''));
        return m ? `every ${m[1]}h` : (s ? `every ${s}` : '');
    }

    function b64ToText(b64) {
        try {
            const bin = atob(String(b64 || '').replace(/\s/g, ''));
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            const text = new TextDecoder('utf-8', { fatal: false }).decode(bytes);
            if (text.indexOf('�') !== -1 && bin.indexOf('\x00') !== -1) return null;
            return text;
        } catch (e) { return null; }
    }

    function setStatus(text) { const el = $('gitea-status'); if (el) el.textContent = text; }

    // Skeleton placeholders (design-system .skeleton) — reserve space so
    // lists never jump when content lands.
    function skeletonCards(n) {
        let out = '';
        for (let i = 0; i < (n || 3); i++) {
            out += '<div class="skeleton-card">' +
                       '<div class="skeleton skeleton-line w75"></div>' +
                       '<div class="skeleton skeleton-line w50"></div>' +
                   '</div>';
        }
        return `<div class="gitea-repos-grid" aria-hidden="true">${out}</div>`;
    }

    // In-app confirm (design-system .confirm-overlay) — replaces window.confirm.
    // Resolves true/false; Escape and overlay-click cancel; focus starts on
    // Cancel so Enter never destroys by default.
    function confirmDialog(title, message, confirmLabel, danger) {
        return new Promise((resolve) => {
            const wrap = document.createElement('div');
            wrap.className = 'confirm-overlay cseries-fade-in';
            wrap.innerHTML =
                '<div class="confirm-box" role="alertdialog" aria-modal="true" ' +
                    `aria-label="${esc(title)}">` +
                    `<div class="confirm-title">${esc(title)}</div>` +
                    `<div class="confirm-message">${esc(message)}</div>` +
                    '<div class="confirm-actions">' +
                        '<button type="button" class="cseries-btn ghost" data-x="no">Cancel</button>' +
                        `<button type="button" class="cseries-btn ${danger ? 'danger' : 'primary'}" data-x="yes">${esc(confirmLabel || 'Confirm')}</button>` +
                    '</div>' +
                '</div>';
            const prevFocus = document.activeElement;
            const done = (ok) => {
                document.removeEventListener('keydown', onKey);
                wrap.remove();
                if (prevFocus && prevFocus.isConnected && prevFocus.focus) prevFocus.focus();
                resolve(ok);
            };
            const onKey = (ev) => {
                if (ev.key === 'Escape') { done(false); return; }
                // focus trap: Tab cycles within the dialog's two buttons
                if (ev.key === 'Tab') {
                    const btns = wrap.querySelectorAll('button');
                    const first = btns[0], last = btns[btns.length - 1];
                    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
                    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
                    else if (!wrap.contains(document.activeElement)) { ev.preventDefault(); first.focus(); }
                }
            };
            wrap.addEventListener('click', (ev) => { if (ev.target === wrap) done(false); });
            wrap.querySelector('[data-x="no"]').addEventListener('click', () => done(false));
            wrap.querySelector('[data-x="yes"]').addEventListener('click', () => done(true));
            document.addEventListener('keydown', onKey);
            document.body.appendChild(wrap);
            wrap.querySelector('[data-x="no"]').focus();
        });
    }

    // ── view switching ───────────────────────────────────────
    function showView(view) {
        stopDeviceFlow();           // leaving (or re-entering) mirrors kills timers
        state.view = view;
        document.querySelectorAll('.cseries-nav-btn[data-view]').forEach((btn) => {
            btn.classList.toggle('active', btn.dataset.view === view);
        });
        document.querySelectorAll('.app-view').forEach((v) => {
            v.hidden = v.id !== `view-${view}`;
        });
        $('repo-detail').hidden = true;
        if (view === 'user') loadUserRepos();
        else if (view === 'host') loadHostRepos();
        else if (view === 'perms') loadPerms();
        else if (view === 'mirrors') loadMirrors();
    }

    // ── repo card (Your repos / Host repos) ──────────────────
    function repoCards(repos, containerId, fromView, emptyCopy) {
        const el = $(containerId);
        if (!repos.length) {
            el.innerHTML = '<div class="cseries-empty"><span class="empty-title">No repositories</span>' +
                esc(emptyCopy || 'Nothing to show here yet.') + '</div>';
            return;
        }
        el.innerHTML = '<div class="gitea-repos-grid cseries-fade-in">' + repos.map((r) => {
            const full = r.full_name || r.name || '';
            const branch = r.default_branch ? esc(r.default_branch) : '';
            const vis = r.private ? 'private' : 'public';
            const updated = fmtRel(r.updated_at);
            return (
                `<button type="button" class="cseries-card gitea-repo" data-repo="${esc(full)}" ` +
                    `aria-label="Open ${esc(full)}">` +
                    '<div class="gitea-repo-main">' +
                        `<span class="gitea-repo-name">${esc(full)}</span>` +
                        `<span class="cseries-pill">${vis}</span>` +
                        (branch ? `<span class="cseries-pill">${branch}</span>` : '') +
                        (r.empty ? '<span class="cseries-pill">empty</span>' : '') +
                    '</div>' +
                    (updated ? `<span class="label-sys gitea-repo-meta">updated ${esc(updated)}</span>` : '') +
                '</button>'
            );
        }).join('') + '</div>';
        el.querySelectorAll('[data-repo]').forEach((b) => {
            b.addEventListener('click', () => openRepo(b.getAttribute('data-repo'), fromView));
        });
    }

    async function loadUserRepos() {
        $('user-list').innerHTML = skeletonCards(4);
        let data;
        try { data = await api('/api/gitea/user/repos'); }
        catch (e) { data = { connected: false }; }
        if (!data.connected) {
            $('user-list').innerHTML = '<div class="cseries-empty"><span class="empty-title">Not connected to Gitea</span>' +
                'This host’s Gitea identity is still provisioning. Check back shortly.</div>';
            setStatus('gitea: not connected'); return;
        }
        if (data.login_required) { renderSignIn('user-list'); setStatus('gitea · sign in'); return; }
        repoCards(data.repos || [], 'user-list', 'user',
            'Repositories you create — or mirror from GitHub — appear here.');
        const foot = document.createElement('p');
        foot.className = 'label-sys gitea-disconnect';
        foot.innerHTML = 'Connected as you. <a href="#" id="gitea-disconnect">Disconnect this host</a>';
        $('user-list').appendChild(foot);
        const dl = $('gitea-disconnect');
        if (dl) dl.addEventListener('click', (e) => { e.preventDefault(); disconnectGitea(); });
        setStatus('gitea · you');
    }

    // ── gitea sign-in (OAuth2 / SSO) ─────────────────────────
    function renderSignIn(containerId) {
        $(containerId).innerHTML =
            '<div class="cseries-card gitea-user-card">' +
                '<span class="empty-title">Sign in to your Gitea</span>' +
                '<p class="label-sys">This app shows <strong>your</strong> repositories and lets you grant the host access — signed in as you. ' +
                'You’re already authenticated here, so it’s one click (same sign-in).</p>' +
                '<button type="button" class="cseries-btn primary" id="gitea-signin">Sign in to gitea →</button>' +
            '</div>';
        const b = $('gitea-signin');
        if (b) b.addEventListener('click', signInToGitea);
    }

    async function signInToGitea() {
        // Host sign-in (2026-08-28): you are already signed in here, so the Host asks the
        // platform for your gitea token — no popup, no second login. The OAuth popup
        // stays only as the fallback for a Host whose platform lane is down.
        setStatus('gitea · connecting…');
        try {
            const r = await api('/api/gitea/connect', { method: 'POST' });
            if (r && r.authed) { showView(state.view); return; }
        } catch (e) { /* fall through to the legacy popup */ }
        let d;
        try { d = await api('/api/gitea/oauth/login'); }
        catch (e) { setStatus('gitea · sign in'); return; }
        if (d && d.authorize_url) window.open(d.authorize_url, 'gitea-signin', 'width=720,height=820');
    }

    async function disconnectGitea() {
        if (!confirm('Disconnect this host from your Gitea account? The token the platform minted for it is revoked; you can reconnect any time.')) return;
        try { await api('/api/gitea/disconnect', { method: 'POST' }); } catch (e) {}
        showView(state.view);
    }

    async function loadHostRepos() {
        $('host-list').innerHTML = skeletonCards(4);
        let data;
        try { data = await api('/api/gitea/repos'); }
        catch (e) { data = { connected: false }; }
        if (!data.connected) {
            $('host-list').innerHTML = '<div class="cseries-empty">Host gitea not connected.</div>';
            setStatus('gitea: not connected'); return;
        }
        repoCards(data.repos || [], 'host-list', 'host',
            'The host hasn’t created anything in its clean room yet.');
        setStatus('gitea · host');
    }

    // ── Permissions: grant / revoke the host's access to your repos ──
    async function loadPerms() {
        $('perms-list').innerHTML = skeletonCards(3);
        let data;
        try { data = await api('/api/gitea/user/repos'); }
        catch (e) { data = { connected: false }; }
        const el = $('perms-list');
        if (!data.connected) {
            el.innerHTML = '<div class="cseries-empty">Not connected to Gitea.</div>';
            setStatus('gitea · permissions'); return;
        }
        if (data.login_required) { renderSignIn('perms-list'); setStatus('gitea · sign in'); return; }
        const repos = data.repos || [];
        if (!repos.length) {
            el.innerHTML = '<div class="cseries-empty"><span class="empty-title">You have no repositories yet</span>' +
                'Create one — or mirror from GitHub — and grant the host access here.</div>';
            setStatus('gitea · permissions'); return;
        }
        el.innerHTML = '<div class="gitea-repos-grid cseries-fade-in">' + repos.map((r) => {
            const full = r.full_name || r.name || '';
            const vis = r.private ? 'private' : 'public';
            return (
                `<div class="cseries-card gitea-perm-row" data-repo="${esc(full)}">` +
                    '<div class="gitea-repo-main">' +
                        `<span class="gitea-repo-name">${esc(full)}</span>` +
                        `<span class="cseries-pill">${vis}</span>` +
                        '<span class="gitea-perm-current label-sys" role="status">…</span>' +
                    '</div>' +
                    '<div class="gitea-perm-actions">' +
                        '<button type="button" class="cseries-btn ghost" data-act="read">Read</button>' +
                        '<button type="button" class="cseries-btn ghost" data-act="write">Write</button>' +
                        '<button type="button" class="cseries-btn ghost" data-act="revoke">Revoke</button>' +
                    '</div>' +
                '</div>'
            );
        }).join('') + '</div>';
        el.querySelectorAll('.gitea-perm-row').forEach((row) => {
            const full = row.getAttribute('data-repo');
            row.querySelectorAll('[data-act]').forEach((btn) => {
                btn.addEventListener('click', () => permAction(row, full, btn.dataset.act));
            });
            refreshPerm(row, full);
        });
        setStatus('gitea · permissions');
    }

    function permParts(full) {
        const i = (full || '').indexOf('/');
        return i < 0 ? null : { owner: full.slice(0, i), name: full.slice(i + 1) };
    }
    function permBase(full) {
        const p = permParts(full);
        return `/api/gitea/repo/${encodeURIComponent(p.owner)}/${encodeURIComponent(p.name)}/host-access`;
    }

    function setPermState(row, permission) {
        const cur = row.querySelector('.gitea-perm-current');
        const label = permission === 'none' ? 'host: no access'
            : (permission === 'read' ? 'host: read'
            : (permission === 'write' ? 'host: write' : `host: ${permission}`));
        if (cur) cur.textContent = label;
        row.querySelectorAll('[data-act]').forEach((btn) => {
            const on = (btn.dataset.act === 'read' && permission === 'read')
                    || (btn.dataset.act === 'write' && (permission === 'write' || permission === 'admin'));
            btn.classList.toggle('active', on);
        });
    }

    async function refreshPerm(row, full) {
        try {
            const d = await api(permBase(full));
            setPermState(row, d.permission || 'none');
        } catch (e) { setPermState(row, 'none'); }
    }

    async function permAction(row, full, act) {
        const cur = row.querySelector('.gitea-perm-current');
        const prev = cur ? cur.textContent : '';
        if (cur) cur.textContent = '…';
        row.querySelectorAll('[data-act]').forEach((b) => { b.disabled = true; });
        try {
            if (act === 'revoke') {
                await api(permBase(full), { method: 'DELETE' });
                setPermState(row, 'none');
            } else {
                await api(permBase(full), { method: 'PUT', body: JSON.stringify({ permission: act }) });
                setPermState(row, act);
            }
        } catch (e) {
            if (cur) cur.textContent = `couldn’t update — ${e.message}`;
            setTimeout(() => { if (cur && cur.textContent.startsWith('couldn’t')) cur.textContent = prev; }, 4000);
        } finally {
            row.querySelectorAll('[data-act]').forEach((b) => { b.disabled = false; });
        }
    }

    /* ================================================================
     * Mirrors — GitHub → gitea pull-mirrors.
     * One-way by construction: reads GitHub, writes only YOUR gitea.
     * ================================================================ */
    let devicePollTimer = null;
    let deviceTickTimer = null;
    // Flow generation: bumped by stopDeviceFlow(). Async poll loops capture
    // the generation they were born in and bail after every await if it moved
    // — otherwise a poll request in flight during cancel/view-switch would
    // reschedule itself and a later failure could paint over (and kill) a
    // NEWER connect attempt sharing the same DOM ids.
    let deviceFlowGen = 0;

    function stopDeviceFlow() {
        deviceFlowGen += 1;
        if (devicePollTimer) { clearTimeout(devicePollTimer); devicePollTimer = null; }
        if (deviceTickTimer) { clearInterval(deviceTickTimer); deviceTickTimer = null; }
    }

    async function loadMirrors() {
        stopDeviceFlow();
        const gh = $('mirrors-github'), list = $('mirrors-list'), picker = $('mirrors-picker');
        picker.hidden = true; picker.innerHTML = '';
        gh.innerHTML = skeletonCards(1);
        list.innerHTML = skeletonCards(2);

        // status + mirror list in parallel; neither blocks the other's render
        const [mirrorsR, statusR] = await Promise.allSettled([
            api('/api/gitea/mirrors'),
            api('/api/gitea/github/status'),
        ]);

        let st = { connected: false };
        if (statusR.status === 'fulfilled') st = statusR.value;
        else if (statusR.reason && statusR.reason.status === 503) st = { connected: false, unavailable: true };
        state.gh = st;
        renderGithubCard(st);

        if (mirrorsR.status === 'fulfilled') {
            renderMirrorList(mirrorsR.value.mirrors || []);
        } else if (mirrorsR.reason && mirrorsR.reason.status === 409) {
            // mirrors live under YOUR gitea account — needs the gitea sign-in
            renderSignIn('mirrors-list');
            setStatus('gitea · sign in'); return;
        } else {
            list.innerHTML = `<div class="cseries-empty">Couldn’t load mirrors — ${esc((mirrorsR.reason || {}).message || 'unknown error')}` +
                '<button type="button" class="cseries-btn ghost gitea-retry" id="mirrors-retry">Retry</button></div>';
            const rb = $('mirrors-retry');
            if (rb) rb.addEventListener('click', loadMirrors);
        }
        setStatus('gitea · mirrors');
    }

    function renderGithubCard(st) {
        const gh = $('mirrors-github');
        if (st.unavailable) {
            gh.innerHTML = '<div class="cseries-card gitea-user-card">' +
                '<span class="empty-title">GitHub connect isn’t enabled on this platform yet</span>' +
                '<p class="label-sys">Mirrors will light up here once the platform’s GitHub app is configured.</p></div>';
            return;
        }
        if (st.connected && st.installed === false) {
            // authorized, but the GitHub App has no repository access yet
            gh.innerHTML =
                '<div class="cseries-card gitea-user-card cseries-fade-in">' +
                    `<span class="empty-title">Connected as ${esc(st.login || 'you')} — one step left on GitHub</span>` +
                    '<p class="label-sys">Repository access is granted by <strong>installing</strong> the app on your GitHub account — ' +
                    'that’s where you choose exactly which repositories it may read (change it on GitHub any time; it can never write).</p>' +
                    '<p class="label-sys">On GitHub: <strong>Settings → Applications → Authorized GitHub Apps</strong> → open the app → ' +
                    '<strong>Install</strong> → pick repositories.</p>' +
                    '<div class="gitea-gh-actions">' +
                        '<button type="button" class="cseries-btn primary" id="gh-installed-reload">I’ve installed it — reload</button>' +
                    '</div>' +
                '</div>';
            const r = $('gh-installed-reload');
            if (r) r.addEventListener('click', loadMirrors);
            return;
        }
        if (st.connected) {
            gh.innerHTML =
                '<div class="cseries-card gitea-user-card gitea-gh-row cseries-fade-in">' +
                    '<span class="label-sys gitea-gh-id"><span class="gitea-gh-dot" aria-hidden="true"></span>' +
                        `GitHub connected${st.login ? ` as <strong>${esc(st.login)}</strong>` : ''} · read-only</span>` +
                    '<div class="gitea-gh-actions">' +
                        (st.manage_url ? `<a class="cseries-btn ghost" href="${esc(st.manage_url)}" target="_blank" rel="noopener">Choose repos on GitHub ↗</a>` : '') +
                        '<button type="button" class="cseries-btn primary" id="gh-pick">Add mirrors</button>' +
                    '</div>' +
                '</div>';
            const b = $('gh-pick');
            if (b) b.addEventListener('click', openMirrorPicker);
            return;
        }
        gh.innerHTML =
            '<div class="cseries-card gitea-user-card">' +
                '<span class="empty-title">Connect GitHub</span>' +
                '<p class="label-sys">One-time sign-in from this device — no password, no browser redirect. ' +
                'Your GitHub credential lands in <strong>Keys</strong>, on your host only, and it can <strong>read</strong> repositories, never write them.</p>' +
                '<div class="gitea-gh-actions"><button type="button" class="cseries-btn primary" id="gh-connect">Connect GitHub →</button></div>' +
                '<div id="gh-device" role="status" aria-live="polite" hidden></div>' +
            '</div>';
        const b = $('gh-connect');
        if (b) b.addEventListener('click', startGithubConnect);
    }

    async function startGithubConnect() {
        stopDeviceFlow();
        const gen = deviceFlowGen;
        const box = $('gh-device'), btn = $('gh-connect');
        if (btn) btn.disabled = true;
        let d;
        try { d = await api('/api/gitea/github/device/start', { method: 'POST' }); }
        catch (e) {
            if (gen !== deviceFlowGen) return;
            if (box) { box.hidden = false; box.innerHTML = `<span class="label-sys is-err">Couldn’t start — ${esc(e.message)}</span>`; }
            if (btn) btn.disabled = false;
            return;
        }
        if (gen !== deviceFlowGen) return;
        let remaining = Math.max(60, d.expires_in || 900);
        if (box) {
            box.hidden = false;
            box.innerHTML =
                '<div class="gitea-device cseries-fade-in">' +
                    '<span class="label-sys">Enter this code at ' +
                        `<a href="${esc(d.verification_uri)}" target="_blank" rel="noopener">${esc(d.verification_uri.replace(/^https?:\/\//, ''))} ↗</a></span>` +
                    '<div class="gitea-device-row">' +
                        `<span class="gitea-devicecode" id="gh-code">${esc(d.user_code)}</span>` +
                        '<button type="button" class="cseries-btn ghost" id="gh-copy">Copy code</button>' +
                        '<button type="button" class="cseries-btn ghost" id="gh-cancel">Cancel</button>' +
                    '</div>' +
                    `<span class="label-sys" id="gh-wait">Waiting for you to approve… code valid <span id="gh-ttl">${fmtTtl(remaining)}</span></span>` +
                '</div>';
            $('gh-copy').addEventListener('click', async () => {
                try { await navigator.clipboard.writeText(d.user_code); } catch (e) { /* selection fallback below */ }
                const c = $('gh-copy'); c.textContent = 'Copied ✓';
                setTimeout(() => { if (c.isConnected) c.textContent = 'Copy code'; }, 2000);
            });
            $('gh-cancel').addEventListener('click', () => { stopDeviceFlow(); renderGithubCard({ connected: false }); });
            $('gh-copy').focus();
        }
        deviceTickTimer = setInterval(() => {
            remaining -= 1;
            const t = $('gh-ttl');
            if (t) t.textContent = fmtTtl(remaining);
            if (remaining <= 0) {
                stopDeviceFlow();
                const w = $('gh-wait');
                if (w) w.innerHTML = '<span class="is-err">Code expired — start again.</span>';
                if (btn) btn.disabled = false;
            }
        }, 1000);
        const poll = async () => {
            let r;
            try { r = await api('/api/gitea/github/device/poll', { method: 'POST', body: JSON.stringify({ state: d.state }) }); }
            catch (e) {
                if (gen !== deviceFlowGen) return;
                stopDeviceFlow();
                const w = $('gh-wait');
                if (w) w.innerHTML = `<span class="is-err">Connect failed — ${esc(e.message)}</span>`;
                if (btn) btn.disabled = false;
                return;
            }
            if (gen !== deviceFlowGen) return;
            if (r.pending) { devicePollTimer = setTimeout(poll, Math.max(3, r.retry_in || 5) * 1000); return; }
            if (r.connected) {
                stopDeviceFlow();
                const w = $('gh-wait');
                if (w) w.innerHTML = `<span class="is-ok">Connected as ${esc(r.login || 'you')} ✓</span>`;
                setTimeout(() => { if (state.view === 'mirrors') loadMirrors(); }, 700);
            }
        };
        poll();
    }

    function fmtTtl(secs) {
        const m = Math.floor(secs / 60), s = Math.max(0, secs % 60);
        return `${m}:${String(s).padStart(2, '0')}`;
    }

    function renderMirrorList(mirrors) {
        const list = $('mirrors-list');
        if (!mirrors.length) {
            const connected = state.gh && state.gh.connected && state.gh.installed !== false;
            list.innerHTML = '<div class="cseries-empty"><span class="empty-title">No mirrors yet</span>' +
                'Your mirrored repositories will live here — private, synced automatically, browsable like any other repo.' +
                (connected ? '<button type="button" class="cseries-btn primary gitea-retry" id="mirrors-add-first">Add your first mirror</button>' : '') +
                '</div>';
            const b = $('mirrors-add-first');
            if (b) b.addEventListener('click', openMirrorPicker);
            return;
        }
        list.innerHTML = '<div class="gitea-repos-grid cseries-fade-in">' + mirrors.map((m) => {
            const src = (m.original_url || '').replace(/^https?:\/\//, '').replace(/\.git$/, '');
            return (
                `<div class="cseries-card gitea-perm-row" data-repo="${esc(m.full_name)}">` +
                    '<div class="gitea-repo-main">' +
                        `<button type="button" class="gitea-mirror-open" data-open="${esc(m.full_name)}" aria-label="Browse ${esc(m.full_name)}">` +
                            `<span class="gitea-repo-name">${esc(m.full_name)}</span></button>` +
                        '<span class="cseries-pill">private</span>' +
                        (m.mirror_interval ? `<span class="cseries-pill">${esc(fmtInterval(m.mirror_interval))}</span>` : '') +
                    '</div>' +
                    '<span class="label-sys gitea-mirror-meta">' +
                        (src ? `from <a href="https://${esc(src)}" target="_blank" rel="noopener">${esc(src)} ↗</a>` : '') +
                        (m.updated_at ? ` · synced ${esc(fmtRel(m.updated_at))}` : '') +
                    '</span>' +
                    '<div class="gitea-perm-actions">' +
                        '<button type="button" class="cseries-btn ghost" data-act="sync">Sync now</button>' +
                        '<button type="button" class="cseries-btn ghost" data-act="remove">Remove</button>' +
                    '</div>' +
                '</div>'
            );
        }).join('') + '</div>';
        list.querySelectorAll('[data-open]').forEach((b) => {
            b.addEventListener('click', () => openRepo(b.getAttribute('data-open'), 'mirrors'));
        });
        list.querySelectorAll('.gitea-perm-row').forEach((row) => {
            const full = row.getAttribute('data-repo');
            row.querySelectorAll('[data-act]').forEach((btn) => {
                btn.addEventListener('click', () => mirrorAction(row, full, btn.dataset.act));
            });
        });
    }

    async function mirrorAction(row, full, act) {
        const p = permParts(full);
        if (!p) return;
        const base = `/api/gitea/mirrors/${encodeURIComponent(p.owner)}/${encodeURIComponent(p.name)}`;
        const btns = row.querySelectorAll('[data-act]');
        if (act === 'sync') {
            const b = row.querySelector('[data-act="sync"]');
            btns.forEach((x) => { x.disabled = true; });
            b.textContent = 'Syncing…';
            try {
                await api(`${base}/sync`, { method: 'POST' });
                b.textContent = 'Synced ✓';
                setTimeout(() => { if (b.isConnected) { b.textContent = 'Sync now'; btns.forEach((x) => { x.disabled = false; }); } }, 2000);
            } catch (e) {
                b.textContent = 'Sync failed — retry';
                btns.forEach((x) => { x.disabled = false; });
            }
            return;
        }
        if (act === 'remove') {
            const ok = await confirmDialog(
                `Remove the mirror ${full}?`,
                'This deletes the private copy on Gitea. Your GitHub repository is not touched — mirrors are one-way.',
                'Remove mirror', true);
            if (!ok) return;
            btns.forEach((x) => { x.disabled = true; });
            try { await api(base, { method: 'DELETE' }); loadMirrors(); }
            catch (e) { btns.forEach((x) => { x.disabled = false; }); }
        }
    }

    async function openMirrorPicker() {
        const picker = $('mirrors-picker');
        picker.hidden = false;
        picker.innerHTML = skeletonCards(3);
        picker.scrollIntoView({ block: 'nearest' });
        let d;
        try { d = await api('/api/gitea/github/repos'); }
        catch (e) {
            picker.innerHTML = `<div class="cseries-empty">Couldn’t list GitHub repos — ${esc(e.message)}` +
                '<button type="button" class="cseries-btn ghost gitea-retry" id="gh-pick-retry">Retry</button></div>';
            $('gh-pick-retry').addEventListener('click', openMirrorPicker);
            return;
        }
        if (d.needs_install) { renderGithubCard(Object.assign({}, state.gh, { connected: true, installed: false })); picker.hidden = true; return; }
        const repos = d.repos || [];
        if (!repos.length) {
            picker.innerHTML =
                '<div class="cseries-card"><span class="empty-title">No repositories selected for the app</span>' +
                '<p class="label-sys">The app is installed on GitHub but has no repositories selected yet.</p>' +
                '<div class="gitea-gh-actions">' +
                (d.manage_url ? `<a class="cseries-btn primary" href="${esc(d.manage_url)}" target="_blank" rel="noopener">Choose repositories on GitHub ↗</a>` : '') +
                '<button type="button" class="cseries-btn ghost" id="gh-pick-retry">Reload</button></div></div>';
            $('gh-pick-retry').addEventListener('click', openMirrorPicker);
            return;
        }
        picker.innerHTML =
            '<div class="cseries-card cseries-fade-in">' +
                '<div class="gitea-picker-head">' +
                    '<span class="empty-title">Pick repositories to mirror</span>' +
                    `<input type="search" class="cseries-input gitea-picker-search" id="gh-filter" placeholder="Filter ${repos.length} repositories…" aria-label="Filter repositories">` +
                    '<div class="gitea-gh-actions">' +
                        '<button type="button" class="cseries-btn ghost" id="gh-sel-all">Select all</button>' +
                        '<button type="button" class="cseries-btn ghost" id="gh-sel-none">Clear</button>' +
                    '</div>' +
                '</div>' +
                '<p class="label-sys">Mirrors are <strong>private</strong> here regardless of source. For a private GitHub repo, the git server keeps your GitHub credential (encrypted) so it can keep pulling — that’s what makes the sync automatic.</p>' +
                '<div class="gitea-entries" id="gh-pick-list">' + repos.map((r) => (
                    '<label class="cseries-row gitea-pick-row" data-name="' + esc((r.full_name + ' ' + (r.description || '')).toLowerCase()) + '">' +
                        `<input type="checkbox" data-full="${esc(r.full_name)}" data-private="${r.private ? '1' : ''}" aria-label="Mirror ${esc(r.full_name)}">` +
                        '<span class="gitea-pick-main">' +
                            '<span class="gitea-pick-title">' +
                                `<span class="gitea-entry-name">${esc(r.full_name)}</span>` +
                                `<span class="cseries-pill">${r.private ? 'private' : 'public'}</span>` +
                            '</span>' +
                            (r.description ? `<span class="label-sys gitea-pick-desc">${esc(r.description)}</span>` : '') +
                        '</span>' +
                        (r.updated_at ? `<span class="label-sys gitea-repo-meta">${esc(fmtRel(r.updated_at))}</span>` : '') +
                        '<span class="label-sys gitea-pick-state" aria-live="polite"></span>' +
                    '</label>'
                )).join('') + '</div>' +
                (d.clipped ? `<p class="label-sys">${esc(d.note || 'Some of your repositories could not be listed — try again.')}</p>` : '') +
                (d.manage_url ? `<p class="label-sys">Missing a repository? <a href="${esc(d.manage_url)}" target="_blank" rel="noopener">Choose repositories on GitHub ↗</a></p>` : '') +
                '<div class="gitea-perm-actions">' +
                    '<button type="button" class="cseries-btn primary" id="gh-mirror-go" disabled>Mirror selected</button>' +
                    '<button type="button" class="cseries-btn ghost" id="gh-mirror-cancel">Cancel</button>' +
                '</div>' +
                '<div id="gh-mirror-progress" class="label-sys" role="status" aria-live="polite"></div>' +
            '</div>';

        const boxes = () => Array.from(picker.querySelectorAll('input[type=checkbox]'));
        const checked = () => boxes().filter((b) => b.checked && !b.closest('.gitea-pick-row').hidden);
        const go = $('gh-mirror-go');
        const syncGo = () => {
            const n = checked().length;
            go.disabled = n === 0;
            go.textContent = n ? `Mirror ${n} selected` : 'Mirror selected';
        };
        boxes().forEach((b) => b.addEventListener('change', syncGo));
        $('gh-sel-all').addEventListener('click', () => {
            boxes().forEach((b) => { if (!b.closest('.gitea-pick-row').hidden) b.checked = true; }); syncGo();
        });
        $('gh-sel-none').addEventListener('click', () => { boxes().forEach((b) => { b.checked = false; }); syncGo(); });
        const filter = $('gh-filter');
        filter.addEventListener('input', () => {
            const q = filter.value.trim().toLowerCase();
            picker.querySelectorAll('.gitea-pick-row').forEach((row) => {
                row.hidden = q !== '' && row.getAttribute('data-name').indexOf(q) === -1;
            });
            syncGo();
        });
        filter.focus();
        $('gh-mirror-cancel').addEventListener('click', () => { picker.hidden = true; picker.innerHTML = ''; });

        go.addEventListener('click', async () => {
            const sel = checked();
            if (!sel.length) return;
            const prog = $('gh-mirror-progress');
            go.disabled = true;
            $('gh-mirror-cancel').disabled = true;
            let done = 0, failed = 0;
            for (const b of sel) {
                const full = b.getAttribute('data-full');
                const rowState = b.closest('.gitea-pick-row').querySelector('.gitea-pick-state');
                b.disabled = true;
                rowState.textContent = 'mirroring…';
                prog.textContent = `Mirroring ${full} (${done + failed + 1}/${sel.length})… repositories with history take a minute.`;
                try {
                    await api('/api/gitea/mirrors', { method: 'POST', body: JSON.stringify({ owner_repo: full, private_source: !!b.getAttribute('data-private') }) });
                    done += 1;
                    rowState.innerHTML = '<span class="is-ok">mirrored ✓</span>';
                } catch (e) {
                    failed += 1;
                    rowState.innerHTML = `<span class="is-err">failed — ${esc(e.message)}</span>`;
                }
            }
            prog.textContent = failed
                ? `Done: ${done} mirrored, ${failed} failed — failures are marked above.`
                : `Done: ${done} mirrored.`;
            $('gh-mirror-cancel').disabled = false;
            $('gh-mirror-cancel').textContent = 'Close';
            if (done && !failed) setTimeout(() => loadMirrors(), 1200);
        });
    }

    // ── shared browse detail ─────────────────────────────────
    function openRepo(fullName, fromView) {
        const p = permParts(fullName);
        if (!p) return;
        state.repo = { owner: p.owner, name: p.name, full_name: fullName };
        state.path = '';
        state.returnView = fromView || state.view;
        document.querySelectorAll('.app-view').forEach((v) => { v.hidden = true; });
        $('repo-detail').hidden = false;
        $('repo-detail-name').textContent = fullName;
        $('repo-detail-vis').hidden = true;
        $('repo-detail-branch').hidden = true;
        $('repo-detail-desc').textContent = '';
        switchPane('files');
        loadOverview();
    }

    function backToList() {
        $('repo-detail').hidden = true;
        showView(state.returnView);
    }

    function repoBase() {
        const r = state.repo;
        return `/api/gitea/repo/${encodeURIComponent(r.owner)}/${encodeURIComponent(r.name)}`;
    }

    async function loadOverview() {
        try {
            const o = await api(repoBase());
            const vis = $('repo-detail-vis');
            vis.textContent = o.private ? 'private' : 'public';
            vis.hidden = false;
            if (o.default_branch) {
                const b = $('repo-detail-branch');
                b.textContent = o.default_branch; b.hidden = false;
            }
            $('repo-detail-desc').textContent = o.description || '';
        } catch (e) { /* decorative */ }
    }

    function switchPane(pane) {
        state.pane = pane;
        document.querySelectorAll('.gitea-subnav .cseries-nav-btn').forEach((btn) => {
            btn.classList.toggle('active', btn.dataset.pane === pane);
        });
        document.querySelectorAll('.gitea-pane').forEach((p) => { p.hidden = p.id !== `pane-${pane}`; });
        if (pane === 'files') loadContents(state.path || '');
        else if (pane === 'commits') loadCommits();
        else if (pane === 'branches') loadBranches();
    }

    function renderBreadcrumb(path) {
        const crumbEl = $('files-breadcrumb');
        const parts = (path || '').split('/').filter(Boolean);
        let acc = '';
        const links = ['<a href="#" data-path="">root</a>'];
        for (const part of parts) {
            acc = acc ? `${acc}/${part}` : part;
            links.push(`<a href="#" data-path="${esc(acc)}">${esc(part)}</a>`);
        }
        crumbEl.innerHTML = links.join('<span class="gitea-crumb-sep"> / </span>');
        crumbEl.querySelectorAll('a[data-path]').forEach((a) => {
            a.addEventListener('click', (ev) => { ev.preventDefault(); loadContents(a.getAttribute('data-path')); });
        });
    }

    async function loadContents(path) {
        state.path = path || '';
        renderBreadcrumb(state.path);
        const body = $('files-body');
        body.innerHTML = skeletonCards(2);
        let data;
        try { data = await api(`${repoBase()}/contents?path=${encodeURIComponent(state.path)}`); }
        catch (e) { body.innerHTML = `<div class="cseries-empty">Couldn’t load this path — ${esc(e.message)}</div>`; return; }

        if (data.type === 'dir') {
            const entries = data.entries || [];
            if (!entries.length) { body.innerHTML = '<div class="cseries-empty">Empty directory.</div>'; return; }
            body.innerHTML = '<div class="gitea-entries cseries-fade-in">' + entries.map((e) => {
                const icon = e.type === 'dir' ? '▸' : '·';
                return `<button type="button" class="cseries-row gitea-entry" data-type="${esc(e.type)}" data-path="${esc(e.path)}">` +
                    `<span class="gitea-entry-icon label-sys">${icon}</span>` +
                    `<span class="gitea-entry-name">${esc(e.name)}</span></button>`;
            }).join('') + '</div>';
            body.querySelectorAll('.gitea-entry').forEach((el) => {
                el.addEventListener('click', () => loadContents(el.getAttribute('data-path')));
            });
            return;
        }
        if (data.type === 'file') {
            const text = data.encoding === 'base64' ? b64ToText(data.content) : null;
            if (text == null) {
                body.innerHTML = `<div class="cseries-empty"><span class="empty-title">${esc(data.name)}</span>` +
                    `Binary or too large to preview${data.size ? ` (${data.size} bytes)` : ''}.</div>`;
                return;
            }
            const capped = text.length > 200000 ? text.slice(0, 200000) + '\n\n… (truncated)' : text;
            body.innerHTML = `<div class="gitea-file-head label-sys">${esc(data.name)}` +
                `${data.size ? ` · ${data.size} bytes` : ''}</div>` +
                `<pre class="gitea-filecontent cseries-scroll">${esc(capped)}</pre>`;
            return;
        }
        body.innerHTML = '<div class="cseries-empty">Nothing to show here.</div>';
    }

    async function loadCommits() {
        const pane = $('pane-commits');
        pane.innerHTML = skeletonCards(3);
        let data;
        try { data = await api(`${repoBase()}/commits?limit=30`); }
        catch (e) { pane.innerHTML = `<div class="cseries-empty">Couldn’t load commits — ${esc(e.message)}</div>`; return; }
        const commits = data.commits || [];
        if (!commits.length) { pane.innerHTML = '<div class="cseries-empty">No commits yet.</div>'; return; }
        pane.innerHTML = '<div class="gitea-commits cseries-fade-in">' + commits.map((c) => (
            '<div class="cseries-card gitea-commit">' +
                `<div class="gitea-commit-msg">${esc(c.message || '(no message)')}</div>` +
                '<div class="gitea-commit-meta label-sys">' +
                    `${esc((c.sha || '').slice(0, 7))}` +
                    (c.author ? ` · ${esc(c.author)}` : '') +
                    (c.date ? ` · ${esc(fmtRel(c.date))}` : '') +
                '</div></div>'
        )).join('') + '</div>';
    }

    async function loadBranches() {
        const pane = $('pane-branches');
        pane.innerHTML = skeletonCards(2);
        let data;
        try { data = await api(`${repoBase()}/branches`); }
        catch (e) { pane.innerHTML = `<div class="cseries-empty">Couldn’t load branches — ${esc(e.message)}</div>`; return; }
        const branches = data.branches || [];
        if (!branches.length) { pane.innerHTML = '<div class="cseries-empty">No branches.</div>'; return; }
        pane.innerHTML = '<div class="gitea-entries cseries-fade-in">' + branches.map((b) => (
            '<div class="cseries-row gitea-entry">' +
                `<span class="gitea-entry-name">${esc(b.name)}</span>` +
                `<span class="label-sys gitea-repo-meta">${esc((b.commit_sha || '').slice(0, 7))}</span>` +
            '</div>'
        )).join('') + '</div>';
    }

    // ── views ────────────────────────────────────────────────
    // `repo:<owner>/<name>` opens that one Code Repository. Every mention of a
    // repository on the Host links here (the Workbench, the chat, a Code
    // Approval), because the forge answers only the Hosts and a link
    // there is dead in a member's browser. The desktop hands a view to a new
    // window as `#view=` and to an open one as a cseries:app-view message
    // (dashboard/base/js/app-runner.js), exactly as the Workbench reads its own.
    function repoOfView(view) {
        const m = /^repo:([A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._-]*)$/.exec(view || '');
        return m ? m[1] : null;
    }
    function viewFromHash() {
        const m = /(?:^|[#&])view=([^&]*)/.exec(window.location.hash || '');
        if (!m) return null;
        try { return decodeURIComponent(m[1]) || null; } catch (e) { return null; }
    }

    // ── boot ─────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('.cseries-nav-btn[data-view]').forEach((btn) => {
            btn.addEventListener('click', () => showView(btn.dataset.view));
        });
        document.querySelectorAll('.gitea-subnav .cseries-nav-btn[data-pane]').forEach((btn) => {
            btn.addEventListener('click', () => switchPane(btn.dataset.pane));
        });
        $('repo-back').addEventListener('click', backToList);
        // when the OAuth popup finishes, it postMessages us — reload the active view
        window.addEventListener('message', (ev) => {
            if (ev.data === 'gitea-oauth-done') showView(state.view);
            // A view for this open window — only the desktop that framed it may send one.
            const d = ev.data;
            if (ev.source === window.parent && d && d.type === 'cseries:app-view') {
                const r = repoOfView(typeof d.view === 'string' ? d.view : '');
                if (r) openRepo(r, 'host');
            }
        });
        const first = repoOfView(viewFromHash());
        if (first) openRepo(first, 'host');
        else showView('user');
    });

    window.GiteaApp = { state, showView, loadUserRepos, loadHostRepos, loadPerms, loadMirrors, openRepo };
})();
