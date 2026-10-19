(function () {
    'use strict';

    // Two kinds of skill (server/routes/skills.py):
    //   - the Host's own: versions in its database, written by the Host or
    //     edited here; history, edit, retire and restore.
    //   - the platform's: files that ship with the Host, read-only here.
    const state = { skills: [], current: null, version: null, creating: false };

    const NEW_SKILL = [
        '---',
        'name: my-skill',
        'description: What this skill does and when to use it.',
        'allowed-tools: context',
        'sources: conversation',
        '---',
        '',
        '## When to use',
        '',
        '## Steps',
        '',
        '1. ',
        '',
        '## Checks',
        '',
        '## When it fails',
        '',
    ].join('\n');

    async function fetchJSON(url, opts) {
        const r = await fetch(url, opts || {});
        if (!r.ok) {
            let detail = null;
            try { detail = (await r.json()).detail; } catch (e) { /* not JSON */ }
            const err = new Error(typeof detail === 'string' ? detail : `${url} → ${r.status}`);
            err.status = r.status;
            err.detail = detail;
            throw err;
        }
        return r.json();
    }

    function send(url, method, body) {
        return fetchJSON(url, {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: body === undefined ? undefined : JSON.stringify(body),
        });
    }

    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute.
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function $(id) { return document.getElementById(id); }

    // A Host skill's URL. Each part of the name is encoded and its '/' kept:
    // a skill moved in from an old skills/ file keeps that file's path as its
    // name (skills/nested/thing.md is nested/thing), and the server's routes
    // take the name as a path (server/routes/skills.py, {name:path}).
    function hostUrl(name, tail) {
        return '/api/skills/host/' + String(name).split('/').map(encodeURIComponent).join('/')
            + (tail || '');
    }

    function status(text) { $('footer-status').textContent = text; }

    function when(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        return isNaN(d) ? String(iso) : d.toLocaleString();
    }

    function authorLabel(a) {
        return a === 'person' ? 'you' : a === 'host' ? 'your Host' : 'the platform';
    }

    function bindRows(list, open) {
        list.querySelectorAll('.skill-row').forEach(row => {
            row.addEventListener('click', () => open(row.dataset.name));
            row.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    open(row.dataset.name);
                }
            });
        });
    }

    // A version the Host wrote in a turn that read untrusted content (a web
    // page, an agent's report, another Host's message, …). Every turn that
    // reads it is treated the same way, every turn of a turn type that uses
    // it too, and it is never suggested to a turn as a matching skill.
    // Saving it here, changed or not, clears the mark.
    function untrustedTitle(source) {
        return `Written in a turn that read untrusted content (${source}). Read it, then save it to clear this.`;
    }

    function renderLists() {
        const host = state.skills.filter(s => s.kind === 'host');
        const platform = state.skills.filter(s => s.kind === 'platform');
        const hl = $('host-skills-list');
        hl.innerHTML = host.length ? host.map(s => `
            <div class="skill-row" data-name="${esc(s.name)}" role="button" tabindex="0" aria-label="Open skill ${esc(s.name)}">
                <div class="skill-row-meta">
                    <span class="skill-row-name">${esc(s.name)}</span>
                    <span class="skill-row-desc">${esc(s.description || '(no description)')}</span>
                </div>
                <span class="skill-row-tags">
                    <span class="cseries-pill ${s.status === 'active' ? 'is-active' : 'is-retired'}">${esc(s.status)}</span>
                    ${s.stale ? `<span class="cseries-pill is-retired" title="${esc(s.stale)}">stale</span>` : ''}
                    ${s.untrusted ? `<span class="cseries-pill is-untrusted" title="${esc(untrustedTitle(s.untrusted))}">review</span>` : ''}
                    <span class="label-sys">v${esc(s.version)} · ${esc(authorLabel(s.author))}</span>
                </span>
            </div>`).join('')
            : '<div class="cseries-empty"><span class="empty-title">No skills of its own yet</span>Your Host writes one when a job needs steps it should keep. You can write one too.</div>';
        bindRows(hl, openHost);
        const pl = $('platform-skills-list');
        pl.innerHTML = platform.length ? platform.map(s => `
            <div class="skill-row" data-name="${esc(s.name)}" role="button" tabindex="0" aria-label="Open skill ${esc(s.name)}">
                <div class="skill-row-meta">
                    <span class="skill-row-name">${esc(s.name)}</span>
                    <span class="skill-row-desc">${esc(s.description || '(no description)')}</span>
                </div>
                <span class="label-sys">platform</span>
            </div>`).join('')
            : '<div class="cseries-empty"><span class="empty-title">No platform skills found</span></div>';
        bindRows(pl, openPlatform);
    }

    async function loadSkills() {
        try {
            const r = await fetchJSON('/api/skills/list');
            state.skills = r.skills || [];
            renderLists();
        } catch (e) {
            const msg = `<div class="cseries-empty"><span class="empty-title">Error</span>${esc(e.message)}</div>`;
            $('host-skills-list').innerHTML = msg;
            $('platform-skills-list').innerHTML = msg;
        }
    }

    function showDetail({ name, badge, badgeClass, meta, body, editable }) {
        $('skill-detail').hidden = false;
        $('detail-name').textContent = name;
        const b = $('detail-badge');
        b.textContent = badge || '';
        b.className = `cseries-pill ${badgeClass || ''}`;
        b.hidden = !badge;
        $('detail-meta').textContent = meta || '';
        $('detail-body').textContent = body || '(empty skill)';
        $('detail-body').hidden = false;
        $('detail-untrusted').hidden = true;
        $('edit-btn').hidden = !editable;
        $('retire-btn').hidden = true;
        $('editor').hidden = true;
        $('history').hidden = true;
        $('version-view').hidden = true;
    }

    async function openPlatform(name) {
        state.current = null;
        showDetail({ name, badge: 'platform', body: 'Loading…' });
        try {
            const r = await fetchJSON(`/api/skills/${encodeURIComponent(name)}`);
            showDetail({ name, badge: 'platform', meta: 'Ships with your Host. Read-only.',
                         body: r.body });
        } catch (e) {
            $('detail-body').textContent = `Could not open skill: ${e.message}`;
        }
    }

    async function openHost(name) {
        showDetail({ name, body: 'Loading…' });
        try {
            const r = await fetchJSON(hostUrl(name));
            const s = r.skill;
            state.current = { name, skill: s, history: r.history || [] };
            const sources = (s.sources || []).map(x => x.ref).join(', ');
            showDetail({
                name,
                badge: s.status,
                badgeClass: s.status === 'active' ? 'is-active' : 'is-retired',
                meta: `version ${s.version} · written by ${authorLabel(s.author)} · ${when(s.created_at)}`
                      + (sources ? ` · from ${sources}` : '')
                      + (s.status === 'retired' ? ' · retired: no turn uses it' : ''),
                body: s.body,
                editable: true,
            });
            const u = $('detail-untrusted');
            u.hidden = !s.untrusted;
            u.textContent = s.untrusted
                ? `Your Host wrote this version in a turn that read untrusted content (${s.untrusted}). `
                  + 'Until you review it, a turn that reads it, and every turn of a turn type that uses it, '
                  + 'is treated as having read that content, and it is never suggested to a turn as a '
                  + 'matching skill. Read it, then Edit and save it '
                  + '(changed or not) to clear this.'
                : '';
            $('retire-btn').hidden = s.status !== 'active';
            renderHistory();
        } catch (e) {
            $('detail-body').textContent = `Could not open skill: ${e.message}`;
        }
    }

    function renderHistory() {
        const cur = state.current;
        if (!cur) return;
        $('history').hidden = false;
        $('history-list').innerHTML = cur.history.map(v => `
            <div class="history-row" data-version="${esc(v.version)}" role="button" tabindex="0" aria-label="Show version ${esc(v.version)}">
                <span class="skill-row-name">v${esc(v.version)}</span>
                <span class="skill-row-desc">${esc(authorLabel(v.author))} · ${esc(when(v.created_at))}${v.edited_from ? ` · from v${esc(v.edited_from)}` : ''}</span>
                ${v.untrusted ? `<span class="cseries-pill is-untrusted" title="${esc(untrustedTitle(v.untrusted))}">review</span>` : ''}
                <span class="cseries-pill ${v.status === 'active' ? 'is-active' : 'is-retired'}">${esc(v.status)}</span>
            </div>`).join('');
        $('history-list').querySelectorAll('.history-row').forEach(row => {
            const open = () => openVersion(Number(row.dataset.version));
            row.addEventListener('click', open);
            row.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
            });
        });
    }

    async function openVersion(version) {
        const cur = state.current;
        if (!cur) return;
        try {
            const v = await fetchJSON(hostUrl(cur.name, `/versions/${version}`));
            state.version = v;
            $('version-view').hidden = false;
            $('version-title').textContent = v.previous_version
                ? `v${v.version}, changes from v${v.previous_version}`
                : `v${v.version}, the first version`;
            $('version-diff').textContent = v.diff || '(no changes)';
            $('restore-btn').hidden = v.status === 'active';
        } catch (e) {
            status(`could not open version: ${e.message}`);
        }
    }

    function showFailures(err) {
        const ul = $('editor-failures');
        const d = err && err.detail;
        if (d && Array.isArray(d.failures)) {
            ul.innerHTML = d.failures.map(f => `<li><span class="rule">${esc(f.rule)}</span> ${esc(f.message)}</li>`).join('');
        } else {
            ul.innerHTML = `<li>${esc(err.message)}</li>`;
        }
    }

    function openEditor(text, creating) {
        state.creating = !!creating;
        $('skill-detail').hidden = false;
        if (creating) {
            state.current = null;
            $('detail-name').textContent = 'New skill';
            $('detail-badge').hidden = true;
            $('detail-meta').textContent = 'Name it in the frontmatter; the four sections are what the standard asks for.';
            $('edit-btn').hidden = true;
            $('retire-btn').hidden = true;
            $('history').hidden = true;
        }
        $('detail-body').hidden = true;
        $('editor').hidden = false;
        $('editor-failures').innerHTML = '';
        $('editor-text').value = text;
        $('editor-text').focus();
    }

    function nameFromText(text) {
        const m = /^---\n[\s\S]*?^name:\s*["']?([^\s"']+)["']?\s*$/m.exec(text);
        return m ? m[1] : '';
    }

    async function save() {
        const text = $('editor-text').value;
        const name = state.creating ? nameFromText(text) : state.current && state.current.name;
        if (!name) {
            showFailures({ message: 'Give the skill a name in its frontmatter (name: my-skill).' });
            return;
        }
        try {
            const r = await send(hostUrl(name), 'PUT', { text });
            status(r.unchanged ? 'no change: the same text as the active version'
                               : `saved ${name} as version ${r.version}`);
            await loadSkills();
            await openHost(name);
        } catch (e) {
            showFailures(e);
            status('not saved: it does not meet the skill standard');
        }
    }

    async function retire() {
        const cur = state.current;
        if (!cur) return;
        if (!window.confirm(`Retire ${cur.name}? No turn will use it. Every version is kept, and you can restore any of them.`)) return;
        try {
            await send(hostUrl(cur.name, '/retire'), 'POST');
            status(`retired ${cur.name}`);
            await loadSkills();
            await openHost(cur.name);
        } catch (e) {
            status(`retire failed: ${e.message}`);
        }
    }

    async function restore() {
        const cur = state.current;
        const v = state.version;
        if (!cur || !v) return;
        try {
            const r = await send(hostUrl(cur.name, '/restore'), 'POST', { version: v.version });
            status(r.unchanged ? 'no change: that text is already the active version'
                               : `restored v${v.version} as version ${r.version}`);
            await loadSkills();
            await openHost(cur.name);
        } catch (e) {
            const d = e.detail;
            status(d && d.failures ? `v${v.version} no longer meets the skill standard: `
                   + d.failures.map(f => f.rule).join(', ') : `restore failed: ${e.message}`);
        }
    }

    function closeDetail() {
        $('skill-detail').hidden = true;
        state.current = null;
        state.version = null;
    }

    // Footer version from the installed manifest (never hardcode it — the
    // app.yaml version is the source of truth and bumps independently).
    async function loadVersion() {
        const el = $('footer-version');
        if (!el) return;
        try {
            const a = await fetchJSON('/api/applications/skills');
            if (a.version) el.textContent = a.version;
        } catch (e) {
            // leave the placeholder if the registry is unreachable
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        $('refresh-btn').addEventListener('click', loadSkills);
        $('new-btn').addEventListener('click', () => openEditor(NEW_SKILL, true));
        $('close-detail-btn').addEventListener('click', closeDetail);
        $('edit-btn').addEventListener('click', () => {
            if (state.current) openEditor(state.current.skill.body, false);
        });
        $('cancel-btn').addEventListener('click', () => {
            if (state.current) openHost(state.current.name); else closeDetail();
        });
        $('save-btn').addEventListener('click', save);
        $('retire-btn').addEventListener('click', retire);
        $('restore-btn').addEventListener('click', restore);
        $('close-version-btn').addEventListener('click', () => { $('version-view').hidden = true; });
        loadVersion();
        loadSkills();
    });

    window.App = { state, loadSkills };
})();
