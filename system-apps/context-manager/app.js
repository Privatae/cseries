/* ================================================================
 * Context 2.0 — the Context Explorer.
 *
 * Two organizing planes that never interact:
 *   Collections (logical)  — tags on user_context rows; grouping never moves
 *                            bytes; one file can sit in many collections.
 *   Sources (physical)     — platform / cabinets (S3) / machines: the DCN store-class view.
 *
 * Views: Files (file-level rollup via GET /api/context-manager/files, real
 * pagination), Search (the everything-search, POST /api/search: your documents
 * grouped by file, then the Host's memory, projects, code and
 * skills, each under its own heading), Inspector (on-demand preview via GET .../{id}/content),
 * Storage (the 1.x lenses — placement/availability/embedding — unchanged).
 *
 * Same-origin system app: plain fetch(); everything degrades to an honest
 * empty/error state. Reuses the platform design system (cseries- and exp- classes);
 * no kernel logic duplicated — routes call brain.mastercode.context_backends.
 * ================================================================ */
(function () {
    'use strict';

    const API = '/api/context-manager';
    const BAPI = '/api/brain/context';

    const state = {
        view: 'files',            // files | search | storage
        filter: { kind: 'all' },  // all | lib:<key> | tag:<name> | source:<machine>
        page: 1,
        perPage: 50,
        sort: 'updated',
        order: 'desc',
        files: null,              // GET /files payload
        tags: [],                 // GET /tags → [{tag, count}]
        pendingTags: [],          // session-local: created but not yet used
        sources: null,            // GET /sources payload
        selected: null,           // inspector's file row
        selectedChunks: null,     // inspector's chunk list {items,total}
        search: null,             // last search payload
        embedding: null,          // coverage counts (search honesty line)
        // storage lenses (ported 1.x state)
        lens: 'index',
        index: null,
        placement: null,
        bastions: null,
        embedModel: null,
        embedBastions: null,
    };

    const LIBRARIES = [
        { key: 'all', label: 'All items', params: {} },
        { key: 'documents', label: 'Documents', params: { source: 'text_upload,markdown_upload,json_upload,csv_upload' } },
        { key: 'files', label: 'Files & media', params: { source: 'file_upload' } },
        { key: 'conversations', label: 'Conversations', params: { source: 'chatgpt,claude' } },
        // 'tool:' is a prefix match server-side: every tool result the host
        // stashed for its own recall (source 'tool:<name>') belongs HERE, not
        // among the user's documents.
        { key: 'host', label: 'Staged by host', params: { source: 'host,tool:' } },
        { key: 'promoted', label: 'Promoted', params: { source: 'promoted' } },
    ];

    // ── tiny DOM + format helpers ────────────────────────────
    const $ = (id) => document.getElementById(id);
    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    function attr(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }

    async function fetchJSON(url, opts) {
        const r = await fetch(url, opts || {});
        const body = await r.text().catch(() => '');
        let json = null;
        try { json = body ? JSON.parse(body) : null; } catch (e) { json = null; }
        if (!r.ok) {
            const err = new Error(friendlyError(r.status, body, r.statusText));
            err.status = r.status;
            throw err;
        }
        // Routes return {error:"..."} with a 200 when the brain isn't ready — surface it.
        if (json && json.error) {
            const err = new Error(json.error);
            err.soft = true;
            throw err;
        }
        return json;
    }
    // A route's refusal in words. FastAPI's 422 carries `detail` as a list of
    // {loc, msg, type, input}: each item's `msg` is the sentence, and `input`
    // is what was sent (the typed words, for a search), so only `msg` is read.
    // Handed to Error as-is, the list drew "[object Object]".
    function detailWords(d) {
        if (typeof d === 'string') return d;
        if (Array.isArray(d)) {
            return d.map(x => (x && typeof x.msg === 'string' ? x.msg : '')).filter(Boolean).join('; ');
        }
        return '';
    }
    function friendlyError(status, body, statusText) {
        let p = null;
        try { p = JSON.parse(body); } catch (e) { /* not JSON: the fallbacks below */ }
        const raw = p && typeof p === 'object' ? (p.detail || p.error) : null;
        if (raw) return detailWords(raw) || statusText || `Request failed (HTTP ${status}).`;
        if (status === 401 || status === 403) return "This Host hasn't granted the Context app that permission.";
        if (status === 404) return 'Not found — it may have been removed.';
        if (status === 503) return 'That service is warming up. Try again in a moment.';
        if (status >= 500) return 'Something went wrong on the Host. Try again shortly.';
        return body ? body.slice(0, 140) : 'Request failed.';
    }

    function fmtRel(iso) {
        if (!iso) return 'never';
        const d = new Date(iso); if (isNaN(d.getTime())) return String(iso);
        const diff = (Date.now() - d.getTime()) / 1000;
        if (diff < 60) return 'just now';
        if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
        if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
        if (diff < 86400 * 30) return `${Math.floor(diff / 86400)}d ago`;
        return d.toLocaleDateString();
    }
    function fmtBytes(n) {
        n = Number(n);
        if (!isFinite(n) || n < 0) return '—';
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
        return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
    }
    function empty(title, body) {
        return `<div class="cseries-empty"><span class="empty-title">${esc(title)}</span>${esc(body || '')}</div>`;
    }
    function errBox(msg) { return `<div class="status-banner error">${esc(msg)}</div>`; }
    function touchFoot() {
        $('foot-right').textContent = 'updated ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    function backendChip(b) {
        const label = b === 'pg' ? 'platform' : b;
        return `<span class="ctx-pill backend-${esc(b)}">${esc(label)}</span>`;
    }

    // ── top-level view switching ─────────────────────────────
    function showView(view) {
        state.view = view;
        $('view-files').hidden = view !== 'files';
        $('view-search').hidden = view !== 'search';
        $('view-storage').hidden = view !== 'storage';
        if (view === 'storage') showStorageLens(state.lens);
        touchFoot();
    }

    // ════════════════════ RAIL (collections + sources) ═══════
    async function loadRail() {
        try {
            const [tags, sources] = await Promise.all([
                fetchJSON(`${BAPI}/tags`).catch(() => ({ tags: [] })),
                fetchJSON(`${API}/sources`).catch(e => ({ _err: e.message })),
            ]);
            state.tags = (tags && tags.tags) || [];
            state.sources = sources;
        } catch (e) { /* each renders its own error below */ }
        renderRail();
    }
    function filterKey(f) {
        if (f.kind === 'lib') return `lib:${f.key}`;
        if (f.kind === 'tag') return `tag:${f.tag}`;
        if (f.kind === 'source') return `src:${f.machine}`;
        return 'lib:all';
    }
    function renderRail() {
        const active = filterKey(state.filter);
        // Collections: libraries + custom tags (+ session-local pending ones).
        const seen = new Set(state.tags.map(t => t.tag.toLowerCase()));
        const pend = state.pendingTags.filter(t => !seen.has(t.toLowerCase()));
        const collHTML = LIBRARIES.map(l => {
            const on = active === `lib:${l.key}` || (l.key === 'all' && active === 'lib:all');
            return `<button class="exp-rail-item ${on ? 'active' : ''}" data-lib="${attr(l.key)}" role="listitem">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/></svg>
                <span class="exp-name">${esc(l.label)}</span>
            </button>`;
        }).join('') + state.tags.map(t => {
            const on = active === `tag:${t.tag}`;
            return `<button class="exp-rail-item tagitem ${on ? 'active' : ''}" data-tag="${attr(t.tag)}" role="listitem">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M20.6 13.4 12 22 2 12V2h10l8.6 8.6a2 2 0 0 1 0 2.8Z"/><circle cx="7.5" cy="7.5" r="1.2" fill="currentColor" stroke="none"/></svg>
                <span class="exp-name">${esc(t.tag)}</span><span class="exp-count">${t.count}</span>
            </button>`;
        }).join('') + pend.map(t => `
            <button class="exp-rail-item tagitem ${active === `tag:${t}` ? 'active' : ''}" data-tag="${attr(t)}" role="listitem">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M20.6 13.4 12 22 2 12V2h10l8.6 8.6a2 2 0 0 1 0 2.8Z"/></svg>
                <span class="exp-name">${esc(t)}</span><span class="exp-count">new</span>
            </button>`).join('');
        $('rail-collections').innerHTML = collHTML;

        // Sources rail.
        const src = state.sources || {};
        let srcHTML = '';
        if (src._err) {
            srcHTML = errBox(src._err);
        } else {
            srcHTML = (src.sources || []).map(s => {
                const on = active === `src:${s.machine}`;
                const dot = s.backend === 'bastion'
                    ? `<span class="ctx-dot ${s.online ? 'live' : 'down'}"></span>` : '';
                const label = s.machine === 'platform' ? 'Platform' : s.machine;
                return `<button class="exp-rail-item ${on ? 'active' : ''}" data-src-machine="${attr(s.machine)}" role="listitem"
                        title="${attr(`${s.files} file(s) · ${fmtBytes(s.bytes)}`)}">
                    ${dot}<span class="exp-name">${esc(label)}</span>
                    <span class="exp-count">${fmtBytes(s.bytes)}</span>
                </button>`;
            }).join('');
            // DCN stores — machine folders (incl. absorbed legacy shared folders)
            // and cabinet buckets. The platform class already has its rail card
            // above; cabinets that hold placement bytes appear there too, so only
            // stores NOT already on the rail render here.
            const railMachines = new Set((src.sources || []).map(s => s.machine));
            const stores = (src.stores || []).filter(st =>
                st.class !== 'platform' &&
                !(st.class === 'cabinet' && railMachines.has(st.bucket)));
            if (stores.length) {
                srcHTML += stores.map(st => {
                    const isCab = st.class === 'cabinet';
                    const name = st.display_name || (isCab ? st.bucket : st.root) || '';
                    const sub = st.status === 'legacy'
                        ? (st.mounted ? 'shared folder' : 'shared folder · unmounted')
                        : (isCab && !st.indexed ? 'not indexed yet' : st.status || '');
                    const tip = st.status === 'legacy'
                        ? 'Shared folder — your host reads it live, on demand. It becomes a governed, indexed store when you add it to your network.'
                        : isCab
                            ? `Your filing cabinet (${st.bucket}) — index: ${st.tier}. The files stay in your bucket.`
                            : `Folder on ${st.server || 'your machine'} — index: ${st.tier}. Files stay on the machine; content is read on demand.`;
                    const icon = isCab
                        ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 4h16v6H4zM4 14h16v6H4z"/><path d="M10 7h4M10 17h4"/></svg>'
                        : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/><path d="M12 11v6M9 14h6"/></svg>';
                    return `
                    <div class="exp-rail-item" role="listitem" style="cursor:default" title="${attr(tip)}">
                        ${icon}
                        <span class="exp-name">${esc(name)}</span>
                        <span class="exp-sub">${esc(sub)}</span>
                    </div>`;
                }).join('');
            }
        }
        $('rail-sources').innerHTML = srcHTML || empty('No sources', '');

        wireRail();
    }
    function wireRail() {
        document.querySelectorAll('#rail-collections [data-lib]').forEach(b => {
            b.addEventListener('click', () => setFilter({ kind: 'lib', key: b.dataset.lib }));
        });
        document.querySelectorAll('#rail-collections [data-tag]').forEach(b => {
            b.addEventListener('click', () => setFilter({ kind: 'tag', tag: b.dataset.tag }));
            // drop target: drag a file row onto a collection = add the tag.
            b.addEventListener('dragover', ev => { ev.preventDefault(); b.classList.add('droptarget'); });
            b.addEventListener('dragleave', () => b.classList.remove('droptarget'));
            b.addEventListener('drop', async ev => {
                ev.preventDefault(); b.classList.remove('droptarget');
                const gkey = ev.dataTransfer.getData('text/ctx-gkey');
                const f = ((state.files || {}).files || []).find(x => x.gkey === gkey);
                if (f) await addTagToFile(f, b.dataset.tag);
            });
        });
        document.querySelectorAll('#rail-sources [data-src-machine]').forEach(b => {
            b.addEventListener('click', () => setFilter({ kind: 'source', machine: b.dataset.srcMachine }));
        });
    }
    function setFilter(f) {
        state.filter = f;
        state.page = 1;
        showView('files');
        renderRail();
        loadFiles();
    }
    function filterLabel() {
        const f = state.filter;
        if (f.kind === 'lib') return (LIBRARIES.find(l => l.key === f.key) || {}).label || 'All items';
        if (f.kind === 'tag') return f.tag;
        if (f.kind === 'source') return f.machine === 'platform' ? 'Platform' : f.machine;
        return 'All items';
    }
    function filterParams() {
        const f = state.filter;
        if (f.kind === 'lib') return (LIBRARIES.find(l => l.key === f.key) || {}).params || {};
        if (f.kind === 'tag') return { tag: f.tag };
        if (f.kind === 'source') return { machine: f.machine };
        return {};
    }

    // ════════════════════ FILES ═════════════════════════════
    async function loadFiles() {
        const p = new URLSearchParams({
            page: String(state.page),
            per_page: String(state.perPage),
            sort: state.sort,
            order: state.order,
        });
        for (const [k, v] of Object.entries(filterParams())) p.set(k, v);
        $('files-crumb').textContent = filterLabel();
        try {
            state.files = await fetchJSON(`${API}/files?${p}`);
        } catch (e) {
            $('files-table').innerHTML = errBox(e.message);
            $('files-pager').innerHTML = '';
            return;
        }
        renderFiles();
        touchFoot();
    }
    function sortArrow(col) {
        if (state.sort !== col) return '';
        return state.order === 'desc' ? ' ↓' : ' ↑';
    }
    function fileIcon(f) {
        if (f.owner === 'context') {
            return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/></svg>';
        }
        return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/></svg>';
    }
    function machineCell(f) {
        const machines = f.machines || [];
        if (!machines.length) return '<span class="ctx-machine">—</span>';
        const label = machines.length > 1 ? `${machines[0]} +${machines.length - 1}` : machines[0];
        const isBastion = (f.backends || []).includes('bastion');
        const dot = isBastion
            ? `<span class="ctx-dot ${f.online ? 'live' : 'down'}" title="${f.online ? 'online' : 'a holding machine is offline'}"></span>` : '';
        return `${dot}<span class="ctx-machine ${label === 'platform' ? 'platform' : ''}">${esc(label === 'platform' ? 'platform' : label)}</span>`;
    }
    function embeddedPill(f) {
        if (f.is_file) return '<span class="ctx-pill na" title="Stored as-is — find it by name or collection; not meaning-searchable">stored</span>';
        if (f.embedded == null) return '<span class="ctx-pill na" title="Host file — fetched on demand, not meaning-searchable yet">n/a</span>';
        if (f.embedded === 'all') return '<span class="ctx-pill ok">yes</span>';
        if (f.embedded === 'some') return '<span class="ctx-pill warn" title="Some pieces of this file aren\'t embedded yet">partial</span>';
        return '<span class="ctx-pill muted">no</span>';
    }
    function renderFiles() {
        const data = state.files || { files: [], total: 0 };
        const files = data.files || [];
        if (!files.length) {
            $('files-table').innerHTML = empty('Nothing here yet',
                state.filter.kind === 'tag'
                    ? 'Drag files onto this collection, or use the inspector to add them.'
                    : 'Drop any files anywhere in this window — text becomes searchable; pictures, PDFs, CSVs and other files are stored as-is.');
            $('files-pager').innerHTML = '';
            return;
        }
        const head = `<div class="exp-frow head">
            <span class="sortable" data-sort="name">Name${sortArrow('name')}</span>
            <span>Collections</span>
            <span class="sortable" data-sort="source">Source${sortArrow('source')}</span>
            <span>Where</span>
            <span class="sortable" data-sort="size">Size${sortArrow('size')}</span>
            <span>Embedded</span>
            <span class="sortable" data-sort="updated">Updated${sortArrow('updated')}</span>
        </div>`;
        const rows = files.map((f, i) => {
            const tags = (f.tags || []).slice(0, 4).map(t => `<span class="exp-tag">${esc(t)}</span>`).join('')
                + ((f.tags || []).length > 4 ? `<span class="exp-tag">+${f.tags.length - 4}</span>` : '');
            const chunks = f.chunks > 1 ? ` <span class="exp-sub label-sys">· ${f.chunks} pieces</span>` : '';
            const sel = state.selected && state.selected.gkey === f.gkey ? 'selected' : '';
            return `<div class="exp-frow ${sel}" data-idx="${i}" draggable="${f.owner === 'user_context'}">
                <span class="f-name">${fileIcon(f)}<span class="f-label" title="${attr(f.name)}">${esc(f.name)}</span>${chunks}</span>
                <span class="f-colls">${tags || '<span class="exp-sub label-sys">—</span>'}</span>
                <span>${f.source ? `<span class="ctx-pill muted">${esc(f.source)}</span>` : '<span class="ctx-pill na" title="Host-managed file">host file</span>'}</span>
                <span class="c-machine">${machineCell(f)}</span>
                <span class="f-size">${fmtBytes(f.size_bytes)}</span>
                <span>${embeddedPill(f)}</span>
                <span class="f-updated">${esc(fmtRel(f.updated_at))}</span>
            </div>`;
        }).join('');
        $('files-table').innerHTML = head + rows;

        // pager
        const from = (data.page - 1) * data.per_page + 1;
        const to = Math.min(data.page * data.per_page, data.total);
        $('files-pager').innerHTML = `
            <span>${data.total ? `${from}–${to} of ${data.total} file${data.total === 1 ? '' : 's'}` : 'no files'}</span>
            <span class="exp-pager-btns">
                <button class="cseries-btn ghost" id="pg-prev" ${data.page <= 1 ? 'disabled' : ''}>‹ Prev</button>
                <button class="cseries-btn ghost" id="pg-next" ${data.page >= data.pages ? 'disabled' : ''}>Next ›</button>
            </span>`;
        $('pg-prev') && $('pg-prev').addEventListener('click', () => { state.page--; loadFiles(); });
        $('pg-next') && $('pg-next').addEventListener('click', () => { state.page++; loadFiles(); });

        // row wiring
        document.querySelectorAll('#files-table .exp-frow:not(.head)').forEach(row => {
            row.addEventListener('click', () => openInspector(files[Number(row.dataset.idx)]));
            row.addEventListener('dragstart', ev => {
                const f = files[Number(row.dataset.idx)];
                ev.dataTransfer.setData('text/ctx-gkey', f.gkey);
                ev.dataTransfer.effectAllowed = 'link';
            });
        });
        document.querySelectorAll('#files-table .head .sortable').forEach(h => {
            h.addEventListener('click', () => {
                const col = h.dataset.sort;
                if (state.sort === col) state.order = state.order === 'desc' ? 'asc' : 'desc';
                else { state.sort = col; state.order = col === 'name' ? 'asc' : 'desc'; }
                loadFiles();
            });
        });
    }

    // ════════════════════ INSPECTOR ═════════════════════════
    function fileTarget(f) {
        // The bulk-tags / group target for one file row.
        if (f.upload_batch) return { upload_batch: f.upload_batch };
        if (f.from_file) return { file: { source: f.source, from_file: f.from_file } };
        return { ids: [f.first_id] };
    }
    async function openInspector(f) {
        state.selected = f;
        state.selectedChunks = null;
        $('inspector').hidden = false;
        renderFiles(); // refresh selected highlight
        $('insp-title').textContent = f.name;
        $('insp-meta').textContent = f.owner === 'context'
            ? 'host file · fetched on demand'
            : `${f.source || '—'} · ${f.chunks} piece${f.chunks === 1 ? '' : 's'} · ${fmtBytes(f.size_bytes)} · updated ${fmtRel(f.updated_at)}`;

        // location card(s)
        const machines = f.machines || [];
        $('insp-location').innerHTML = `<div class="ctx-card">
            <div class="ctx-card-main">${(f.backends || []).map(backendChip).join(' ')}
                <span class="ctx-card-title">${esc(machines.join(', ') || '—')}</span></div>
            <div class="ctx-card-meta label-sys">${f.online ? 'available' : 'a holding machine is offline'}${f.replicas > 1 ? ` · ×${f.replicas} copies` : ''}</div>
        </div>`;

        renderInspTags();
        renderTagSuggestions();

        if (f.owner === 'context') {
            $('insp-preview').textContent = 'This is a host-managed file — its bytes are fetched on demand when your host needs them, and it isn\'t meaning-searchable yet.';
            $('insp-preview-status').textContent = '';
            $('insp-chunks-block').hidden = true;
            $('insp-actions').innerHTML = '';
            return;
        }

        // preview (on-demand — the ONLY read that pulls external bytes)
        $('insp-preview').textContent = 'Loading…';
        $('insp-preview-status').textContent = '';
        fetchJSON(`${BAPI}/${f.first_id}/content`).then(c => {
            if (c.kind === 'file') {
                // Binary-lane row: bytes come from /file, never the base64 blob.
                // Search-result inspectors arrive without is_file — learn it
                // here so the action gating below is honest too.
                if (!f.is_file) { f.is_file = true; renderInspActions(); }
                const url = `${BAPI}/${f.first_id}/file`;
                const size = c.size_bytes != null ? fmtBytes(c.size_bytes) : '';
                if ((c.mime || '').startsWith('image/')) {
                    $('insp-preview').innerHTML = `<img class="exp-preview-img" src="${url}" alt="${attr(c.filename || f.name)}">`;
                    $('insp-preview').querySelector('img').addEventListener('error', () => {
                        $('insp-preview').textContent = '';
                        $('insp-preview-status').textContent = 'preview unavailable — the holding machine may be offline';
                    });
                } else {
                    $('insp-preview').innerHTML = `<a class="cseries-btn" href="${url}" target="_blank" rel="noopener">Open / download</a>`;
                }
                $('insp-preview-status').textContent =
                    `${c.mime || 'file'}${size ? ' · ' + size : ''} — stored as-is${c.storage_backend !== 'pg' ? ` on ${c.storage_backend}` : ''}`;
                return;
            }
            if (c.content_offline) {
                $('insp-preview').textContent = '';
                $('insp-preview-status').textContent = `offline — ${c.machine || 'its machine'} isn't checked in`;
            } else {
                const text = c.content || '';
                $('insp-preview').textContent = text.length > 4000 ? text.slice(0, 4000) + '\n…' : (text || '(empty)');
                $('insp-preview-status').textContent = c.storage_backend !== 'pg' ? `read from ${c.machine || c.storage_backend}` : '';
            }
        }).catch(e => {
            $('insp-preview').textContent = '';
            $('insp-preview-status').textContent = e.message;
        });

        // chunk list (metadata only; also collects the ids for per-file actions)
        const target = fileTarget(f);
        if (f.chunks > 1 && (target.upload_batch || target.file)) {
            $('insp-chunks-block').hidden = false;
            $('insp-chunks').innerHTML = '<div class="skeleton skeleton-card"></div>';
            const q = new URLSearchParams({ status: 'all', fields: 'meta', per_page: '200' });
            if (target.upload_batch) q.set('upload_batch', target.upload_batch);
            else { q.set('from_file', target.file.from_file); if (target.file.source) q.set('source', target.file.source); }
            fetchJSON(`${BAPI}?${q}`).then(r => {
                state.selectedChunks = r;
                $('insp-chunk-count').textContent = r.total;
                $('insp-chunks').innerHTML = (r.items || []).map(c => `
                    <div class="exp-chunk">
                        <span>${esc(c.title || `#${c.id}`)}</span>
                        <span>${c.has_embedding ? '<span class="ctx-pill ok">embedded</span>' : '<span class="ctx-pill muted">not yet</span>'}</span>
                    </div>`).join('') + (r.total > 200 ? `<div class="label-sys">…and ${r.total - 200} more</div>` : '');
                renderInspActions();
            }).catch(e => {
                $('insp-chunks').innerHTML = errBox(e.message);
                renderInspActions();
            });
        } else {
            $('insp-chunks-block').hidden = true;
            state.selectedChunks = { items: [{ id: f.first_id }], total: 1 };
        }
        renderInspActions();
    }
    function renderInspTags() {
        const f = state.selected;
        if (!f || f.owner === 'context') { $('insp-tags').innerHTML = '<span class="exp-sub label-sys">Host files can\'t be tagged.</span>'; return; }
        const tags = f.tags || [];
        $('insp-tags').innerHTML = tags.length
            ? tags.map(t => `<span class="exp-tag">${esc(t)}<span class="exp-tag-x" data-untag="${attr(t)}" title="Remove from collection">✕</span></span>`).join('')
            : '<span class="exp-sub label-sys">Not in any collection yet.</span>';
        document.querySelectorAll('#insp-tags [data-untag]').forEach(x => {
            x.addEventListener('click', () => removeTagFromFile(f, x.dataset.untag));
        });
    }
    function renderTagSuggestions() {
        $('tag-suggestions').innerHTML = state.tags.map(t => `<option value="${attr(t.tag)}">`).join('')
            + state.pendingTags.map(t => `<option value="${attr(t)}">`).join('');
    }
    function renderInspActions() {
        const f = state.selected;
        if (!f || f.owner === 'context') { $('insp-actions').innerHTML = ''; return; }
        const chunkIds = ((state.selectedChunks || {}).items || []).map(c => c.id).filter(Boolean);
        const total = (state.selectedChunks || {}).total || f.chunks;
        const isFile = !!f.is_file;
        const canDismiss = total <= 200;
        const canPromote = !isFile && total === 1;
        // Honest gating: only offer re-embed when we hold ALL the piece ids —
        // silently re-embedding a 200-piece prefix of a bigger file is worse
        // than pointing at the corpus-wide control. Stored (binary) files have
        // nothing to embed or promote — only Dismiss applies.
        const canReembed = !isFile && chunkIds.length > 0 && chunkIds.length >= total;
        $('insp-actions').innerHTML = `
            <button class="cseries-btn" id="act-reembed" ${canReembed ? '' : 'disabled'}
                title="${canReembed ? 'Refresh this file\'s embeddings' : (isFile ? 'Stored as-is — there\'s no text to embed' : 'Too many pieces for a per-file refresh — use Storage → Embedding → Re-embed everything')}">Re-embed this file</button>
            <button class="cseries-btn" id="act-promote" ${canPromote ? '' : 'disabled'}
                title="${canPromote ? 'Copy this into your host\'s own memory — it becomes part of how your host thinks' : (isFile ? 'Stored as-is — promotion applies to text' : 'Promotion is per piece — open a single-piece file to promote it')}">Promote to host memory</button>
            <button class="cseries-btn ghost danger" id="act-dismiss" ${canDismiss ? '' : 'disabled'}
                title="${canDismiss ? 'Remove from your context (the pieces are marked dismissed)' : 'Too many pieces to dismiss in one go from here'}">Dismiss</button>
            <span class="ctx-inline-status" id="act-status"></span>`;
        $('act-reembed').addEventListener('click', () => reembedFile(chunkIds));
        $('act-promote').addEventListener('click', () => promoteFile(f));
        $('act-dismiss').addEventListener('click', () => dismissFile(f, chunkIds));
    }
    async function addTagToFile(f, tag) {
        if (!tag || f.owner === 'context') return;
        try {
            await fetchJSON(`${BAPI}/tags`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(Object.assign({ add: [tag] }, fileTarget(f))),
            });
            state.pendingTags = state.pendingTags.filter(t => t.toLowerCase() !== tag.toLowerCase());
            if (state.selected && state.selected.gkey === f.gkey) {
                state.selected.tags = Array.from(new Set([...(state.selected.tags || []), tag]));
                renderInspTags();
            }
            loadRail(); loadFiles();
        } catch (e) {
            const st = $('act-status'); if (st) st.textContent = e.message;
        }
    }
    async function removeTagFromFile(f, tag) {
        try {
            await fetchJSON(`${BAPI}/tags`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(Object.assign({ remove: [tag] }, fileTarget(f))),
            });
            if (state.selected && state.selected.gkey === f.gkey) {
                state.selected.tags = (state.selected.tags || []).filter(t => t !== tag);
                renderInspTags();
            }
            loadRail(); loadFiles();
        } catch (e) {
            const st = $('act-status'); if (st) st.textContent = e.message;
        }
    }
    async function reembedFile(ids) {
        const st = $('act-status');
        st.textContent = 'queuing…';
        try {
            const r = await fetchJSON(`${API}/embedding`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ids }),
            });
            st.textContent = r.queued != null ? `Queued ${r.queued} piece(s).` : 'Queued.';
        } catch (e) { st.textContent = e.message; }
    }
    async function promoteFile(f) {
        if (!confirm('Promote this into your host\'s own memory? This is one-way: it becomes part of how your host thinks.')) return;
        const st = $('act-status');
        st.textContent = 'promoting…';
        try {
            const r = await fetchJSON(`${BAPI}/${f.first_id}/promote`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ memory_type: 'research' }),
            });
            st.textContent = r.promoted ? 'Promoted.' : (r.error || 'Done.');
            loadFiles();
        } catch (e) { st.textContent = e.message; }
    }
    async function dismissFile(f, ids) {
        if (!confirm(`Dismiss "${f.name}" (${ids.length} piece${ids.length === 1 ? '' : 's'}) from your context?`)) return;
        const st = $('act-status');
        st.textContent = 'dismissing…';
        let done = 0;
        for (const id of ids) {
            try { await fetchJSON(`${BAPI}/${id}`, { method: 'DELETE' }); done++; }
            catch (e) { st.textContent = e.message; break; }
        }
        st.textContent = `Dismissed ${done}/${ids.length}.`;
        closeInspector();
        loadFiles(); loadRail();
    }
    function closeInspector() {
        state.selected = null;
        $('inspector').hidden = true;
        renderFiles();
    }

    // ════════════════════ SEARCH ════════════════════════════
    // The everything-search:
    // one POST /api/search (scope read:search) over the five stores the Host
    // can see: your documents, its memory, your projects, your code and its
    // skills (its own and the platform's: a name, who wrote it
    // and its one line, never the skill's text). Each store answers under its own heading, in its own order: the
    // server ranks nothing across stores, because a cosine and a name match
    // are not comparable numbers, and neither does this. Documents come first
    // and stay grouped by file, as this view always showed them.
    //
    // The words go in the request body, never in a URL. The box
    // searches as the person types, a URL is kept by whatever sits in front of
    // the pod, and a password or a key typed a key at a time would reach it a
    // prefix at a time.
    const SEARCH_LIMIT = 50;
    const SEARCH_DEBOUNCE_MS = 350;
    // The route takes at most this many characters (server/routes/search.py,
    // UnifiedSearch.q) and answers more with a 422. The documents-only route
    // this box used before had no ceiling, so a long passage pasted to find
    // what is like it searched; it still does, on its first 2,000 characters,
    // and the line at the top says so.
    const SEARCH_MAX_CHARS = 2000;
    const STORE_ORDER = ['documents', 'memory', 'projects', 'code', 'skills'];
    const STORE_LABEL = {
        documents: 'Your documents',
        memory: "Your Host's memory",
        projects: 'Projects',
        code: 'Your code',
        skills: "Your Host's skills",
    };
    // Who wrote a skill (server: memory_retrieval._search_skills_store `author`).
    const SKILL_BY = { host: 'written by your Host', person: 'written by you', platform: 'platform' };
    let searchTimer = null;
    // Only the newest search draws: an older one answering late is dropped,
    // and clearing the box drops the one in flight.
    let searchSeq = 0;
    async function runSearch(q) {
        const mine = ++searchSeq;
        showView('search');
        $('search-crumb').textContent = `Search — “${q}”`;
        $('search-results').innerHTML = '<div class="skeleton skeleton-card"></div>';
        $('search-coverage').innerHTML = '';
        // coverage honesty line for your documents (cheap; no words in it)
        try {
            const emb = await fetchJSON(`${API}/embedding`);
            if (mine === searchSeq) state.embedding = emb;
        } catch (e) { /* soft */ }
        const sent = q.length > SEARCH_MAX_CHARS ? q.slice(0, SEARCH_MAX_CHARS) : q;
        let res;
        try {
            res = await fetchJSON('/api/search', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ q: sent, limit: SEARCH_LIMIT }),
            });
        } catch (e) {
            if (mine !== searchSeq) return;
            $('search-results').innerHTML = errBox(e.message);
            $('search-coverage').innerHTML = '';
            return;
        }
        if (mine !== searchSeq) return;
        state.search = res;
        state.searchClipped = sent.length < q.length;
        renderSearch();
        touchFoot();
    }
    function dropSearch() { searchSeq++; clearTimeout(searchTimer); }

    // "searched by meaning and exact words" / "exact words only — …": the
    // sentences app-web-kit/search.tsx armsLine prints, so the two never disagree.
    function armsLine(arms, why) {
        const a = arms || [];
        why = why || {};
        if (a.includes('semantic') && a.includes('lexical')) return 'searched by meaning and exact words';
        if (a.includes('lexical')) return `exact words only — ${why.semantic || 'meaning search unavailable right now'}`;
        if (a.includes('semantic')) return `meaning only — ${why.lexical || 'exact-word index still building'}`;
        return '';
    }
    function leftOut(n) {
        return n ? `${n} weak match${n === 1 ? '' : 'es'} left out, as your Host's own recall would` : '';
    }
    function storeRows(store, sec) {
        if (!sec) return [];
        return (store === 'memory' ? sec.hits : sec.results) || [];
    }
    function storeNote(store, sec) {
        if (!sec || sec.unavailable) return '';
        const join = (...parts) => parts.filter(Boolean).join(' · ');
        if (store === 'documents') return join(armsLine(sec.arms), leftOut(sec.suppressed));
        if (store === 'memory') {
            return join(armsLine(sec.arms, { semantic: sec.semantic_unavailable, lexical: sec.lexical_unavailable }),
                leftOut(sec.suppressed));
        }
        if (store === 'projects') return sec.semantic ? 'by meaning and name' : 'by name and description';
        if (store === 'code') {
            const n = (sec.projects || []).length;
            return n ? `${n} indexed project${n === 1 ? '' : 's'}` : 'nothing is indexed yet';
        }
        if (store === 'skills') {
            const n = sec.searched || 0;
            return `by name and what each is for · ${n} skill${n === 1 ? '' : 's'} searched`;
        }
        return '';
    }
    function snippet(text, max) {
        const t = String(text || '').trim();
        return t.length > max ? t.slice(0, max - 1) + '…' : t;
    }
    function documentGroups(results) {
        // group hits by file (group_key from the backend)
        const groups = new Map();
        for (const r of results) {
            const k = r.group_key || `row:${r.id}`;
            if (!groups.has(k)) groups.set(k, []);
            groups.get(k).push(r);
        }
        return Array.from(groups.entries()).map(([k, hits]) => {
            const first = hits[0];
            // metadata may arrive as a JSON string (no jsonb codec on the pool).
            let meta = first.metadata;
            if (typeof meta === 'string') { try { meta = JSON.parse(meta); } catch (e) { meta = {}; } }
            if (!meta || typeof meta !== 'object') meta = {};
            const name = meta.from_file || first.title || `entry #${first.id}`;
            return `<div class="exp-hitgroup">
                <div class="exp-hitgroup-head">
                    <span class="exp-hitgroup-title" data-open-id="${attr(first.id)}" data-open-name="${attr(name)}">${esc(name)}</span>
                    <span class="ctx-pill muted">${esc(first.source || '')}</span>
                    <span class="exp-sub label-sys">${hits.length} match${hits.length === 1 ? '' : 'es'}</span>
                </div>
                ${hits.slice(0, 3).map(h => `
                    <div class="exp-hit">
                        <div class="exp-hit-snippet">${h.kind === 'file'
                            ? esc(`${h.mime || 'file'}${h.size_bytes != null ? ' · ' + fmtBytes(h.size_bytes) : ''} — stored as-is; open to preview or download`)
                            : esc((h.content || '').slice(0, 420) || '(content held on an offline machine)')}</div>
                        <div class="exp-hit-meta">
                            ${(h.matched || []).map(m => `<span class="ctx-pill ${m === 'semantic' ? 'ok' : 'backend-s3'}">${m === 'semantic' ? 'meaning' : 'exact'}</span>`).join('')}
                            ${h.similarity != null ? `<span>${Math.round(h.similarity * 100)}% similar</span>` : ''}
                        </div>
                    </div>`).join('')}
            </div>`;
        }).join('');
    }
    function storeHits(store, rows) {
        if (store === 'documents') return documentGroups(rows);
        return rows.map(h => {
            if (store === 'memory') {
                return `<div class="exp-hit" data-hit="memory">
                    <div class="exp-hit-meta">${h.memory_type ? `<span class="ctx-pill muted">${esc(h.memory_type)}</span>` : ''}${h.created_at ? `<span>${esc(fmtRel(h.created_at))}</span>` : ''}</div>
                    <div class="exp-hit-snippet">${esc(snippet(h.content, 420))}</div>
                </div>`;
            }
            if (store === 'projects') {
                return `<div class="exp-hit" data-hit="project">
                    <div class="exp-hit-head"><span class="exp-hit-title">${esc(h.name || `project ${h.id}`)}</span>${h.group ? ` <span class="ctx-pill muted">${esc(h.group)}</span>` : ''}${h.last_activity_at ? ` <span class="exp-sub label-sys">${esc(fmtRel(h.last_activity_at))}</span>` : ''}</div>
                    ${h.description ? `<div class="exp-hit-snippet">${esc(snippet(h.description, 200))}</div>` : ''}
                </div>`;
            }
            if (store === 'skills') {
                const by = SKILL_BY[h.author] || String(h.author || '');
                return `<div class="exp-hit" data-hit="skill">
                    <div class="exp-hit-head"><span class="exp-hit-title">${esc(h.name || 'skill')}</span>${by ? ` <span class="ctx-pill muted">${esc(by)}</span>` : ''}</div>
                    ${h.description ? `<div class="exp-hit-snippet">${esc(snippet(h.description, 200))}</div>` : ''}
                </div>`;
            }
            // code
            const where = `${h.project || ''}/${h.file_path || ''}${h.start_line != null ? ':' + h.start_line : ''}`;
            return `<div class="exp-hit" data-hit="code">
                <div class="exp-hit-head exp-code-where"><span>${esc(where)}</span>${h.symbol ? ` <span class="exp-sub">${esc(h.symbol)}</span>` : ''}</div>
                <pre class="exp-code">${esc(snippet(h.content, 1200))}</pre>
            </div>`;
        }).join('');
    }
    function storeSection(store, sec) {
        const rows = storeRows(store, sec);
        const note = storeNote(store, sec);
        let body;
        if (!sec) body = '<div class="exp-store-empty label-sys">Not searched.</div>';
        else if (sec.unavailable) body = `<div class="exp-store-empty err">Could not search this right now: ${esc(sec.unavailable)}</div>`;
        else if (!rows.length) {
            body = `<div class="exp-store-empty label-sys">${store === 'documents'
                ? 'Nothing matched. Unembedded pieces only match exact words.' : 'Nothing matched.'}</div>`;
        } else body = `<div class="stack">${storeHits(store, rows)}</div>`;
        const failed = store === 'code' && sec && (sec.failed || []).length
            ? `<div class="exp-store-empty label-sys">Some projects could not be searched: ${esc(sec.failed.join('; '))}</div>`
            : store === 'skills' && sec && (sec.failed || []).length
                ? `<div class="exp-store-empty label-sys">Not searched: ${esc(sec.failed.join('; '))}</div>` : '';
        return `<section class="exp-store" data-store="${store}">
            <div class="exp-store-head">
                <span class="exp-store-title">${esc(STORE_LABEL[store])}</span>
                <span class="exp-store-count">${rows.length}</span>
                ${note ? `<span class="exp-store-note">· ${esc(note)}</span>` : ''}
            </div>
            ${body}${failed}
        </section>`;
    }
    function renderSearch() {
        const res = state.search || {};
        const asked = Array.isArray(res.stores) ? res.stores : STORE_ORDER;
        const stores = STORE_ORDER.filter(s => asked.includes(s));
        const emb = state.embedding || {};
        const total = stores.reduce((n, s) => n + storeRows(s, res[s]).length, 0);
        const down = (res.unavailable || []).length;
        const bits = [`${total} result${total === 1 ? '' : 's'}`];
        if (state.searchClipped) bits.push(`searched the first ${SEARCH_MAX_CHARS.toLocaleString('en-US')} characters`);
        if (down) bits.push(`${down} store${down === 1 ? '' : 's'} could not be searched`);
        if (emb.unembedded > 0) {
            bits.push(`${emb.unembedded} of your document piece${emb.unembedded === 1 ? ' isn\'t' : 's aren\'t'} embedded yet and won't match meaning-search <button class="cseries-btn ghost" id="cov-embed">Embed now</button>`);
        }
        $('search-coverage').innerHTML = bits.join(' · ');
        const covBtn = $('cov-embed');
        if (covBtn) covBtn.addEventListener('click', async () => {
            covBtn.disabled = true;
            try { await fetchJSON(`${API}/embedding`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) }); covBtn.textContent = 'Queued'; }
            catch (e) { covBtn.textContent = e.message; }
        });

        $('search-results').innerHTML = stores.map(s => storeSection(s, res[s])).join('');
        document.querySelectorAll('#search-results [data-open-id]').forEach(el => {
            el.addEventListener('click', () => {
                // Open a minimal inspector for the hit's first row.
                openInspector({
                    gkey: `search:${el.dataset.openId}`,
                    owner: 'user_context',
                    name: el.dataset.openName,
                    first_id: Number(el.dataset.openId),
                    chunks: 1, tags: [], backends: [], machines: [],
                    online: true, size_bytes: null, updated_at: null, source: null,
                });
            });
        });
    }

    // ════════════════════ UPLOAD ════════════════════════════
    // Poll the upload's per-file embed jobs (metadata.ids-scoped server-side)
    // and drive the footer status bar until every chunk is embedded — the
    // "did my file actually become searchable" answer used to be invisible.
    async function trackEmbedJobs(jobIds) {
        const bar = $('embed-progress'), fill = $('embed-progress-fill'), label = $('embed-progress-label');
        if (!jobIds.length || !bar) return;
        bar.hidden = false;
        try {
            const pending = new Set(jobIds);
            let total = 0, embedded = 0, failed = 0;
            for (let tick = 0; tick < 200 && pending.size; tick++) {
                total = 0; embedded = 0; failed = 0;
                for (const id of Array.from(pending)) {
                    try {
                        const j = await fetchJSON(`/api/brain/embed/jobs/${id}`);
                        total += j.total || 0; embedded += j.embedded || 0; failed += j.failed || 0;
                        if (['done', 'failed', 'cancelled'].includes(j.status)) pending.delete(id);
                    } catch (e) { pending.delete(id); }
                }
                const pct = total ? Math.round(((embedded + failed) / total) * 100) : 0;
                fill.style.width = `${pct}%`;
                label.textContent = pending.size
                    ? `embedding ${embedded}/${total}…`
                    : (failed ? `embedded ${embedded}/${total} — ${failed} failed` : `embedded ${embedded}/${total} ✓`);
                if (pending.size) await new Promise(res => setTimeout(res, 1500));
            }
            loadFiles();
            setTimeout(() => { bar.hidden = true; fill.style.width = '0%'; }, 4000);
        } catch (e) {
            bar.hidden = true;
        }
    }

    async function uploadFiles(fileList) {
        const files = Array.from(fileList || []);
        if (!files.length) return;
        const activeTag = state.filter.kind === 'tag' ? state.filter.tag : null;
        $('foot-left').textContent = `uploading ${files.length} file(s)…`;
        let imported = 0, stored = 0, errors = [], jobIds = [];
        for (const f of files) {
            try {
                const headers = { 'X-Filename': encodeURIComponent(f.name) };
                if (activeTag) headers['X-Tags'] = encodeURIComponent(activeTag);
                const r = await fetchJSON(`${BAPI}/upload`, { method: 'POST', body: f, headers });
                if (r.kind === 'file') stored += r.imported || 0;
                else imported += r.imported || 0;
                if (r.job_id != null) jobIds.push(r.job_id);
                if (r.errors && r.errors.length) errors.push(...r.errors);
            } catch (e) { errors.push(`${f.name}: ${e.message}`); }
        }
        const parts = [];
        if (imported) parts.push(`imported ${imported} piece(s)`);
        if (stored) parts.push(`stored ${stored} file(s) as-is`);
        const summary = parts.join(' · ') || 'nothing imported';
        $('foot-left').textContent = errors.length
            ? `${summary} · ${errors.length} error(s): ${errors[0]}`
            : `${summary}${activeTag ? ` — in “${activeTag}”` : ''}`;
        loadFiles(); loadRail();
        trackEmbedJobs(jobIds);
    }
    function wireUpload() {
        $('upload-btn').addEventListener('click', () => $('upload-input').click());
        $('upload-input').addEventListener('change', () => {
            uploadFiles($('upload-input').files);
            $('upload-input').value = '';
        });
        let dragDepth = 0;
        document.addEventListener('dragenter', ev => {
            if (!ev.dataTransfer || !Array.from(ev.dataTransfer.types || []).includes('Files')) return;
            dragDepth++;
            $('drop-overlay').hidden = false;
            const t = state.filter.kind === 'tag' ? ` — lands in “${state.filter.tag}”` : '';
            $('drop-hint').textContent = `Any file — text and CSVs are embedded for search; pictures, PDFs and other files are stored as-is${t}`;
        });
        document.addEventListener('dragleave', () => {
            dragDepth = Math.max(0, dragDepth - 1);
            if (!dragDepth) $('drop-overlay').hidden = true;
        });
        document.addEventListener('dragover', ev => {
            if (Array.from((ev.dataTransfer || {}).types || []).includes('Files')) ev.preventDefault();
        });
        document.addEventListener('drop', ev => {
            if (!ev.dataTransfer || !Array.from(ev.dataTransfer.types || []).includes('Files')) return;
            ev.preventDefault();
            dragDepth = 0;
            $('drop-overlay').hidden = true;
            uploadFiles(ev.dataTransfer.files);
        });
    }

    // ════════════════════ NEW COLLECTION ════════════════════
    function newCollection() {
        openModal('New collection', `
            <label class="ctx-field"><span class="label-sys">Name</span>
                <input class="cseries-input" id="f-coll-name" placeholder="Research, Receipts, Project X…" maxlength="64">
            </label>
            <p class="view-sub">A collection groups files wherever they physically live — nothing moves. Drag files onto it, or add them from the inspector.</p>`, [
            { label: 'Cancel', cls: 'ghost', onClick: closeModal },
            {
                label: 'Create', cls: 'primary', onClick: () => {
                    const name = ($('f-coll-name').value || '').trim();
                    if (!name) return;
                    if (!state.pendingTags.some(t => t.toLowerCase() === name.toLowerCase())
                        && !state.tags.some(t => t.tag.toLowerCase() === name.toLowerCase())) {
                        state.pendingTags.push(name);
                    }
                    closeModal();
                    renderRail();
                    renderTagSuggestions();
                    setFilter({ kind: 'tag', tag: name });
                }
            },
        ]);
        setTimeout(() => { const i = $('f-coll-name'); if (i) i.focus(); }, 50);
    }

    /* ════════════════════════════════════════════════════════
     * STORAGE — the 1.x lenses, ported unchanged (index /
     * placement / availability / embedding) behind the gear.
     * ════════════════════════════════════════════════════════ */
    function showStorageLens(lens) {
        state.lens = lens;
        document.querySelectorAll('.exp-subnav-btn').forEach(b => {
            const on = b.dataset.lens === lens;
            b.classList.toggle('active', on);
            b.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        document.querySelectorAll('#view-storage .ctx-lens').forEach(s => { s.hidden = s.id !== `lens-${lens}`; });
        if (lens === 'index') loadIndex();
        if (lens === 'placement') loadPlacement();
        if (lens === 'network') loadDcnStores();
        if (lens === 'availability') loadAvailability();
        if (lens === 'embedding') loadEmbedding();
    }

    // ── Your network — DCN stores (grant lifecycle over /api/network/nac/dcn) ──
    const DCN = '/api/network/nac/dcn';
    async function loadDcnStores() {
        const el = $('dcn-stores-list');
        try {
            const res = await fetchJSON(`${DCN}/stores`);
            state.dcnStores = res.stores || [];
            // Move plans: the one write lane; degrade to none on an
            // older host that has no plans route.
            try {
                const pl = await fetchJSON(`${DCN}/plans`);
                state.dcnPlans = pl.plans || [];
            } catch (_) { state.dcnPlans = []; }
            if (!state.bastions) {
                // The Add-a-folder machine picker needs the bastion list.
                state.bastions = await fetchJSON(`${API}/bastions`).catch(() => ({ bastions: [] }));
            }
        } catch (e) {
            el.innerHTML = errBox(e.message);
            return;
        }
        renderDcnStores();
    }
    // Managed bastion agent (Context 2.4.1): the bastion runs a bastion agent for a
    // machine store granted 'search' and reports its state each heartbeat.
    // Say the one thing to fix in plain words; never a bare state code.
    function dcnFreeAgentPill(s) {
        if (s.class !== 'machine' || !(s.mask || []).includes('search')) return '';
        const st = s.freeagent_state;
        if (s.freeagent) return ' <span class="ctx-pill ok" title="Your machine indexes this folder locally and answers searches with redacted text">bastion agent serving</span>';
        if (!st) return ' <span class="ctx-pill muted" title="The machine has not reported a bastion agent for this store yet (bastion 0.13.2+ installs it from the grant)">bastion agent not reported</span>';
        const words = {
            'missing-python': 'needs Python 3.8+ on that machine',
            'missing-openssl': 'needs openssl on that machine',
            'installing': 'bastion agent installing…',
            'running': 'bastion agent starting…',
            'failed': 'bastion agent failed: ' + (st.detail || 'see the machine'),
        };
        const label = words[st.state] || ('bastion agent: ' + (st.state || 'unknown'));
        const cls = st.state === 'failed' || st.state.startsWith('missing') ? 'warn' : 'muted';
        return ` <span class="ctx-pill ${cls}" title="${attr(st.detail || '')}">${esc(label)}</span>`;
    }
    function dcnTierLine(cls) {
        return cls === 'machine' ? 'index: locations only' : 'index: locations + previews';
    }
    // Move plans on a store card: draft → your code → dry run → apply.
    // "Remove" is a move into the archive folder; nothing is ever deleted here.
    function dcnPlansBlock(s) {
        const plans = (state.dcnPlans || []).filter(p => p.store_id === s.id && p.status !== 'revoked');
        if (!plans.length) return '';
        return `<div class="ctx-plans">` + plans.map(p => {
            const rep = p.report || p.dry_run_report || null;
            let line = '';
            if (p.status === 'draft') line = '<span class="ctx-pill warn">awaiting your code</span>';
            else if (p.status === 'approved') line = p.dry_run_at
                ? (rep && rep.ready ? '<span class="ctx-pill ok">checked — ready to apply</span>'
                    : `<span class="ctx-pill warn">${(rep && rep.problems || []).length} problem(s) — propose a new plan</span>`)
                : '<span class="ctx-pill muted">approved — dry run next</span>';
            else if (p.status === 'applied') line = `<span class="ctx-pill ok">applied · ${rep ? rep.applied : '?'} moved, ${rep ? rep.verified : '?'} verified</span>`;
            else if (p.status === 'failed') line = `<span class="ctx-pill warn">failed: ${esc((rep && rep.reason) || 'see report')}</span>`;
            const actions = p.status === 'draft' ? `
                <input type="text" class="cseries-input" id="dcn-plan-code-${p.id}" placeholder="6-digit code" inputmode="numeric" autocomplete="one-time-code" style="max-width:130px">
                <button class="cseries-btn primary" data-dcn-plan-approve="${p.id}">Approve plan</button>
                <button class="cseries-btn" data-dcn-plan-revoke="${p.id}">Discard</button>`
                : p.status === 'approved' ? `
                <button class="cseries-btn" data-dcn-plan-run="${p.id}" data-mode="dry_run">Dry run</button>
                <button class="cseries-btn primary" data-dcn-plan-run="${p.id}" data-mode="apply" ${rep && rep.ready ? '' : 'disabled title="run the dry run first"'}>Apply</button>
                <button class="cseries-btn" data-dcn-plan-revoke="${p.id}">Discard</button>` : '';
            return `<div class="ctx-plan">
                <div><strong>${esc(p.title)}</strong> · ${p.move_count} move${p.move_count === 1 ? '' : 's'} · archive: <code>${esc(p.archive)}/</code> ${line}</div>
                <p class="view-sub">Moves files inside this folder only. Nothing is deleted — "remove" means moved into <code>${esc(p.archive)}/</code>; you empty it yourself.</p>
                <div class="view-toolbar">${actions}</div>
            </div>`;
        }).join('') + `</div>`;
    }
    async function approveDcnPlan(id) {
        const code = ($(`dcn-plan-code-${id}`) || {}).value || '';
        const st = $('dcn-stores-status');
        st.textContent = 'approving plan…';
        try {
            const res = await fetchJSON(`${DCN}/plan/${id}/approve`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ stepup_code: code.trim() }),
            });
            // A machine with its own second factor approves ON the machine: the
            // host filed the request there; the code goes into that machine, not here.
            st.textContent = res.pending_on_box ? res.next : 'plan approved — run the dry run next';
        } catch (e) { st.textContent = e.message; return; }
        loadDcnStores();
    }
    async function revokeDcnPlan(id) {
        try {
            await fetchJSON(`${DCN}/plan/${id}/revoke`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        } catch (e) { $('dcn-stores-status').textContent = e.message; return; }
        loadDcnStores();
    }
    async function runDcnPlan(id, mode) {
        if (mode === 'apply' && !confirm('Apply this plan? Files move inside the folder as listed; a failure rolls back. Nothing is deleted.')) return;
        const st = $('dcn-stores-status');
        st.textContent = mode === 'apply' ? 'applying on your machine…' : 'checking on your machine…';
        try {
            const res = await fetchJSON(`${DCN}/plan/${id}/run`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode }),
            });
            st.textContent = res.state === 'ok'
                ? (mode === 'apply' ? `applied: ${res.applied} moved, ${res.verified} verified` : (res.ready ? 'every move checks out — you can apply' : `${(res.problems || []).length} problem(s) found`))
                : `${res.state}: ${res.reason || res.detail || ''}`;
        } catch (e) { st.textContent = e.message; return; }
        loadDcnStores();
    }
    function renderDcnStores() {
        const stores = state.dcnStores || [];
        const live = stores.filter(s => s.status !== 'revoked');
        $('dcn-stores-status').textContent = live.length
            ? `${live.filter(s => s.status === 'granted').length} granted · ${live.filter(s => s.status === 'draft').length} awaiting your code`
            : 'none yet';
        $('dcn-stores-list').innerHTML = live.length ? live.map(s => {
            const loc = s.class === 'machine'
                ? `${esc(s.server)} : ${esc(s.root)}`
                : `${esc(s.bucket)}${s.prefix ? '/' + esc(s.prefix) : ''}${s.endpoint ? ' @ ' + esc(s.endpoint) : ''}`;
            const mask = (s.mask || []).map(m => `<span class="ctx-pill muted">${esc(m)}</span>`).join(' ');
            const bundled = (s.egress_rule_ids || []).length;
            const faPill = dcnFreeAgentPill(s);
            const statusPill = s.status === 'granted'
                ? `<span class="ctx-pill ok">granted${s.expires_at ? ' · until ' + esc(s.expires_at.slice(0, 10)) : ''}</span>`
                : s.status === 'draft'
                    ? '<span class="ctx-pill warn">awaiting your approval</span>'
                    : `<span class="ctx-pill muted">${esc(s.status)}</span>`;
            const approveUI = s.status === 'draft' ? `
                <div class="view-toolbar">
                    <input type="text" class="cseries-input" id="dcn-code-${s.id}" placeholder="6-digit code" inputmode="numeric" autocomplete="one-time-code" style="max-width:130px">
                    <button class="cseries-btn primary" data-dcn-approve="${s.id}">Approve</button>
                    <button class="cseries-btn" data-dcn-revoke="${s.id}">Discard</button>
                </div>` : s.status === 'granted' ? `
                <div class="view-toolbar">
                    <button class="cseries-btn" data-dcn-revoke="${s.id}">Revoke</button>
                </div>` : '';
            return `<div class="ctx-card">
                <div class="ctx-block-head">
                    <div><strong>${esc(s.display_name || loc)}</strong>
                        <span class="ctx-pill muted">${s.purpose === 'machine_record' ? "this machine's own record" : (s.class === 'machine' ? 'folder on your machine' : 'bucket you own')}</span>
                        ${statusPill}${faPill}</div>
                </div>
                <p class="view-sub">${esc(loc)} · ${dcnTierLine(s.class)} · sweep: ${esc(s.sweep || 'hourly')}${bundled ? ` · bundles ${bundled} egress allow${bundled > 1 ? 's' : ''} (one approval covers both)` : ''}</p>
                <div>${mask}</div>
                ${approveUI}
                ${s.status === 'granted' ? dcnPlansBlock(s) : ''}
            </div>`;
        }).join('') : empty('No stores yet', 'Add a folder from one of your machines, or a bucket you keep files in.');
        document.querySelectorAll('[data-dcn-approve]').forEach(b => b.addEventListener('click', () => approveDcnStore(b.dataset.dcnApprove)));
        document.querySelectorAll('[data-dcn-revoke]').forEach(b => b.addEventListener('click', () => revokeDcnStore(b.dataset.dcnRevoke)));
        document.querySelectorAll('[data-dcn-plan-approve]').forEach(b => b.addEventListener('click', () => approveDcnPlan(b.dataset.dcnPlanApprove)));
        document.querySelectorAll('[data-dcn-plan-revoke]').forEach(b => b.addEventListener('click', () => revokeDcnPlan(b.dataset.dcnPlanRevoke)));
        document.querySelectorAll('[data-dcn-plan-run]').forEach(b => b.addEventListener('click', () => runDcnPlan(b.dataset.dcnPlanRun, b.dataset.mode)));
    }
    async function approveDcnStore(id) {
        const code = ($(`dcn-code-${id}`) || {}).value || '';
        const st = $('dcn-stores-status');
        st.textContent = 'approving…';
        try {
            const res = await fetchJSON(`${DCN}/${id}/approve`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ stepup_code: code.trim() }),
            });
            st.textContent = res.pending_on_box ? res.next   // approve ON that machine (its own second factor)
                : (res.grant_sync && res.grant_sync.state === 'refused'
                    ? 'granted here, but the machine refused it: ' + (res.grant_sync.detail || res.grant_sync.reason || 'see the machine')
                    : (res.grant_sync && res.grant_sync.state !== 'ok'
                        ? 'granted — the machine will pick the grant up when it next connects'
                        : 'granted'));
        } catch (e) {
            st.textContent = e.message;
            return;
        }
        loadDcnStores();
    }
    async function revokeDcnStore(id) {
        if (!confirm('Revoke this store? Your files are untouched; your host loses access. The index is kept.')) return;
        try {
            await fetchJSON(`${DCN}/${id}/revoke`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        } catch (e) {
            $('dcn-stores-status').textContent = e.message;
            return;
        }
        loadDcnStores();
    }
    function addDcnFolder() {
        const bas = (state.bastions && state.bastions.bastions) || [];
        const options = bas.map(b => `<option value="${attr(b.hostname)}">${esc(b.hostname)}</option>`).join('');
        openModal('Add a folder to your network', `
            <p class="view-sub">Your files stay on the machine. Privatae Host keeps only the index — a route map of what exists and where — so your host can find things instantly. Content is read on demand, only from folders you approve, and every read is logged for you.</p>
            <label class="ctx-field"><span class="label-sys">Machine</span>
                <select class="cseries-input" id="dcn-f-server">${options || '<option value="">— no machines online —</option>'}</select>
            </label>
            <label class="ctx-field"><span class="label-sys">Folder path on that machine</span>
                <input class="cseries-input" id="dcn-f-root" placeholder="/home/you/Documents/contracts"></label>
            <label class="ctx-field"><span class="label-sys">Name (optional)</span>
                <input class="cseries-input" id="dcn-f-name" placeholder="Contracts"></label>
            <p class="view-sub">Secret-looking files (.ssh, keys, .env…) are excluded by default; you can edit excludes after granting. Approval takes your 6-digit code.</p>`,
            [{ label: 'Create draft', primary: true, onClick: async () => {
                try {
                    await fetchJSON(`${DCN}/propose`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            class: 'machine',
                            server: $('dcn-f-server').value.trim(),
                            root: $('dcn-f-root').value.trim(),
                            display_name: $('dcn-f-name').value.trim() || undefined,
                        }),
                    });
                } catch (e) { alert(e.message); return; }
                closeModal(); loadDcnStores();
            } }, { label: 'Cancel', onClick: closeModal }]);
    }
    function addDcnBucket() {
        openModal('Add a bucket (your filing cabinet)', `
            <p class="view-sub">Privatae Host indexes what you keep here — locations and short previews — so your host can find any document instantly. The files themselves stay in your bucket. One approval covers the store and the network reach it needs (the bundled egress allow).</p>
            <label class="ctx-field"><span class="label-sys">Bucket</span>
                <input class="cseries-input" id="dcn-b-bucket" placeholder="my-files"></label>
            <label class="ctx-field"><span class="label-sys">Key prefix (optional)</span>
                <input class="cseries-input" id="dcn-b-prefix" placeholder="documents/"></label>
            <label class="ctx-field"><span class="label-sys">Endpoint (optional, for R2/B2/MinIO)</span>
                <input class="cseries-input" id="dcn-b-endpoint" placeholder="https://…"></label>
            <label class="ctx-field"><span class="label-sys">Vault credential name</span>
                <input class="cseries-input" id="dcn-b-cred" value="ctx:s3"></label>
            <p class="view-sub">Seal the bucket's credentials in your vault first (Keys). Approval takes your 6-digit code.</p>`,
            [{ label: 'Create draft', primary: true, onClick: async () => {
                try {
                    await fetchJSON(`${DCN}/propose`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            class: 'cabinet',
                            bucket: $('dcn-b-bucket').value.trim(),
                            prefix: $('dcn-b-prefix').value.trim() || undefined,
                            endpoint: $('dcn-b-endpoint').value.trim() || undefined,
                            cred_name: $('dcn-b-cred').value.trim() || 'ctx:s3',
                        }),
                    });
                } catch (e) { alert(e.message); return; }
                closeModal(); loadDcnStores();
            } }, { label: 'Cancel', onClick: closeModal }]);
    }

    async function loadIndex() {
        const filter = ($('idx-filter').value || '').trim();
        const q = filter ? `?prefix=${encodeURIComponent(filter)}` : '';
        try {
            state.index = await fetchJSON(`${API}/index${q}`);
        } catch (e) {
            $('idx-table').innerHTML = errBox(e.message);
            $('idx-stats').innerHTML = '';
            return;
        }
        renderIndex();
        touchFoot();
    }
    function machineLabel(f) {
        const m = f.machine;
        if (!m) return '<span class="ctx-machine">—</span>';
        if (m === 'platform') return '<span class="ctx-machine platform" title="Kept for you on the network">platform</span>';
        const kind = f.backend === 's3' ? 'S3 bucket' : (f.backend === 'bastion' ? 'your bastion' : 'machine');
        const dot = f.backend === 'bastion'
            ? `<span class="ctx-dot ${f.online ? 'live' : 'down'}" title="${f.online ? 'online' : 'offline — not checked in'}"></span>`
            : '';
        return `${dot}<span class="ctx-machine ${esc(f.backend)}" title="${attr(kind)}">${esc(m)}</span>`;
    }
    function renderIndex() {
        const data = state.index || { files: [] };
        const files = data.files || [];
        const byBackend = {};
        let totalBytes = 0, offline = 0, embedded = 0, embeddable = 0;
        for (const f of files) {
            byBackend[f.backend] = (byBackend[f.backend] || 0) + 1;
            totalBytes += Number(f.size_bytes || 0);
            if (f.online === false) offline++;
            if (f.embedded === true) embedded++;
            if (f.embedded !== null && f.embedded !== undefined) embeddable++;
        }
        const chips = [
            `<div class="ctx-stat"><span class="ctx-stat-n">${files.length}</span><span class="ctx-stat-l">pieces</span></div>`,
            `<div class="ctx-stat"><span class="ctx-stat-n">${fmtBytes(totalBytes)}</span><span class="ctx-stat-l">stored</span></div>`,
        ];
        for (const [b, n] of Object.entries(byBackend)) {
            chips.push(`<div class="ctx-stat">${backendChip(b)}<span class="ctx-stat-l">${n}</span></div>`);
        }
        if (offline) chips.push(`<div class="ctx-stat alert"><span class="ctx-stat-n">${offline}</span><span class="ctx-stat-l">offline</span></div>`);
        if (embeddable) chips.push(`<div class="ctx-stat"><span class="ctx-stat-n">${embedded}/${embeddable}</span><span class="ctx-stat-l">embedded</span></div>`);
        $('idx-stats').innerHTML = chips.join('');

        if (!files.length) {
            $('idx-table').innerHTML = empty('No context yet', 'Bring context in and it shows up here, with the machine that holds it.');
            return;
        }
        const head = `<div class="ctx-row head">
            <span class="c-path">Context</span>
            <span class="c-backend">Backend</span>
            <span class="c-machine">Machine</span>
            <span class="c-size">Size</span>
            <span class="c-state">State</span>
            <span class="c-embed">Embedded</span>
        </div>`;
        const rows = files.map(f => {
            const ownerTag = f.owner === 'user_context'
                ? '<span class="ctx-owner uc" title="Context you brought in (your intake corpus)">yours · intake</span>'
                : '<span class="ctx-owner ec" title="Held by your host, owned by you — fetched on demand">yours · on-demand</span>';
            let stateBadge;
            if (f.backend === 'bastion') {
                stateBadge = f.online
                    ? '<span class="ctx-pill ok">online</span>'
                    : '<span class="ctx-pill warn">offline</span>';
            } else {
                stateBadge = '<span class="ctx-pill muted">available</span>';
            }
            if (f.replicas > 1) stateBadge += ` <span class="ctx-pill copies" title="${f.replicas} physical copies">×${f.replicas}</span>`;
            let embedBadge;
            if (f.embedded === true) embedBadge = '<span class="ctx-pill ok">yes</span>';
            else if (f.embedded === false) embedBadge = '<span class="ctx-pill muted">no</span>';
            else embedBadge = '<span class="ctx-pill na" title="Not embeddable here (on-demand corpus)">n/a</span>';
            return `<div class="ctx-row">
                <span class="c-path"><span class="c-path-main" title="${attr(f.file_path)}">${esc(f.file_path)}</span>${ownerTag}</span>
                <span class="c-backend">${backendChip(f.backend)}</span>
                <span class="c-machine">${machineLabel(f)}</span>
                <span class="c-size">${fmtBytes(f.size_bytes)}</span>
                <span class="c-state">${stateBadge}</span>
                <span class="c-embed">${embedBadge}</span>
            </div>`;
        }).join('');
        $('idx-table').innerHTML = head + rows;
    }

    async function loadPlacement() {
        try {
            const [pl, ba] = await Promise.all([
                fetchJSON(`${API}/placement`),
                fetchJSON(`${API}/bastions`).catch(e => ({ bastions: [], count: 0, _err: e.message })),
            ]);
            state.placement = pl;
            state.bastions = ba;
        } catch (e) {
            $('default-card').innerHTML = errBox(e.message);
            $('routes-list').innerHTML = '';
            $('machines-list').innerHTML = '';
            return;
        }
        renderPlacement();
        touchFoot();
    }
    function renderPlacement() {
        const pl = state.placement || {};
        const cur = pl.current || { backend: 'pg' };
        $('default-card').innerHTML = renderBackendCard(cur, 'default');
        const routes = pl.routes || [];
        $('routes-status').textContent = routes.length ? `${routes.length} route${routes.length === 1 ? '' : 's'}` : 'none';
        $('routes-list').innerHTML = routes.length
            ? routes.map(renderRouteCard).join('')
            : empty('No routes', 'Everything uses the default above. Add a route to send specific context elsewhere.');
        const bas = (state.bastions && state.bastions.bastions) || [];
        $('machines-status').textContent = bas.length ? `${bas.length} online` : 'none online';
        if (state.bastions && state.bastions._err) {
            $('machines-list').innerHTML = errBox(state.bastions._err);
        } else if (!bas.length) {
            $('machines-list').innerHTML = empty('No context-store machines online',
                'Stand up a bastion in Network and have it advertise context storage to choose it here.');
        } else {
            $('machines-list').innerHTML = bas.map(b => `
                <div class="ctx-card machine">
                    <div class="ctx-card-main">
                        <span class="ctx-dot live"></span>
                        <span class="ctx-card-title">${esc(b.hostname)}</span>
                        ${b.name ? `<span class="label-sys">${esc(b.name)}</span>` : ''}
                    </div>
                    <div class="ctx-card-meta label-sys">context-store · seen ${esc(fmtRel(b.last_seen_at))}</div>
                </div>`).join('');
        }
    }
    function renderBackendCard(cfg, kind) {
        const b = cfg.backend || 'pg';
        let where;
        if (b === 'pg') where = 'Kept for you on the platform';
        else if (b === 's3') where = `S3 bucket <strong>${esc(cfg.bucket || '—')}</strong>${cfg.endpoint_url ? ` · ${esc(cfg.endpoint_url)}` : ''}`;
        else if (b === 'bastion') where = `Your bastion <strong>${esc(cfg.server || '—')}</strong>${cfg.root ? ` · ${esc(cfg.root)}` : ''}`;
        else where = '—';
        const avail = cfg.availability || 'single';
        return `<div class="ctx-card">
            <div class="ctx-card-main">${backendChip(b)} <span class="ctx-card-title">${where}</span></div>
            <div class="ctx-card-meta label-sys">availability: <strong>${esc(avail)}</strong>${(cfg.replicas && cfg.replicas.length) ? ` · replicas: ${esc(cfg.replicas.join(', '))}` : ''}</div>
        </div>`;
    }
    function renderRouteCard(r) {
        const b = r.backend || 'pg';
        let where;
        if (b === 'pg') where = 'platform';
        else if (b === 's3') where = `S3 ${esc(r.bucket || '—')}`;
        else if (b === 'bastion') where = `bastion ${esc(r.server || '—')}`;
        else where = '—';
        const avail = r.availability || 'single';
        return `<div class="ctx-card route" data-prefix="${attr(r.prefix)}">
            <div class="ctx-card-main">
                <span class="ctx-prefix" title="${attr(r.prefix)}">${esc(r.prefix)}</span>
                <span class="ctx-arrow">→</span>
                ${backendChip(b)} <span class="ctx-card-title">${where}</span>
            </div>
            <div class="ctx-card-actions">
                <span class="ctx-card-meta label-sys">${esc(avail)}${(r.replicas && r.replicas.length) ? ` · ${r.replicas.length} replica(s)` : ''}</span>
                <button class="cseries-btn ghost" data-route-edit="${attr(r.prefix)}">Edit</button>
                <button class="cseries-btn ghost danger" data-route-clear="${attr(r.prefix)}">Remove</button>
            </div>
        </div>`;
    }

    async function loadAvailability() {
        try {
            if (!state.placement) state.placement = await fetchJSON(`${API}/placement`);
            if (!state.index) state.index = await fetchJSON(`${API}/index`);
        } catch (e) {
            $('avail-default-card').innerHTML = errBox(e.message);
            return;
        }
        const pl = state.placement || {};
        const cur = pl.current || { backend: 'pg' };
        $('avail-default-status').textContent = cur.availability || 'single';
        $('avail-default-card').innerHTML = renderAvailCard(cur, 'host-wide default');
        const routes = pl.routes || [];
        $('avail-routes-list').innerHTML = routes.length
            ? routes.map(r => renderAvailCard(r, r.prefix)).join('')
            : empty('No per-route availability', 'Add a route in Placement to give a slice of context its own redundancy.');
        const files = (state.index && state.index.files) || [];
        const copied = files.filter(f => (f.replicas || 0) > 1);
        $('avail-copies-list').innerHTML = copied.length
            ? copied.map(f => `<div class="ctx-card">
                    <div class="ctx-card-main"><span class="ctx-card-title">${esc(f.file_path)}</span></div>
                    <div class="ctx-card-meta label-sys">${backendChip(f.backend)} · ${f.replicas} copies · primary on ${esc(f.machine || '—')}</div>
                </div>`).join('')
            : empty('No replicated pieces yet', 'When a piece is kept with replicate availability, its copies are listed here.');
        touchFoot();
    }
    function renderAvailCard(cfg, label) {
        const avail = cfg.availability || 'single';
        const isRep = avail === 'replicate';
        const extra = (state.placement && state.placement.replicate_extra_copies) || 2;
        return `<div class="ctx-card">
            <div class="ctx-card-main">
                <span class="ctx-card-title">${esc(label)}</span>
                <span class="ctx-pill ${isRep ? 'ok' : 'muted'}">${esc(avail)}</span>
            </div>
            <div class="ctx-card-meta label-sys">${isRep
                ? `up to ${1 + extra} copies${(cfg.replicas && cfg.replicas.length) ? ` · targets: ${esc(cfg.replicas.join(', '))}` : ' · auto-picked from your machines'}`
                : 'one copy'}</div>
        </div>`;
    }

    async function loadEmbedding() {
        try {
            state.embedding = await fetchJSON(`${API}/embedding`);
        } catch (e) {
            $('embed-stats').innerHTML = errBox(e.message);
        }
        try { state.embedModel = await fetchJSON('/api/embedding/current'); }
        catch (e) { state.embedModel = { _err: e.message }; }
        try { state.embedBastions = (await fetchJSON('/api/embedding/bastions')).bastions || []; }
        catch (e) { state.embedBastions = { _err: e.message }; }
        try { state.embedJobs = (await fetchJSON('/api/brain/embed/jobs?limit=12')).jobs || []; }
        catch (e) { state.embedJobs = { _err: e.message }; }
        renderEmbedding();
        renderEmbedModel();
        renderEmbedQueue();
        touchFoot();
    }

    // ── Embed queue: the jobs list, live while anything runs. This is the
    //    "is it embedding or not?" answer that used to be invisible
    //    (only a footer bar + counts existed). ──
    let _embedQueueTimer = null;
    function renderEmbedQueue() {
        const el = $('embed-queue');
        const st = $('embed-queue-status');
        if (!el) return;
        const raw = state.embedJobs;
        if (raw && raw._err) { el.innerHTML = errBox(raw._err); return; }
        const jobs = Array.isArray(raw) ? raw : [];
        const LIVE = ['pending', 'running', 'paused'];
        const active = jobs.filter(j => LIVE.includes(j.status));
        if (st) st.textContent = active.length ? `${active.length} active` : '';
        el.innerHTML = jobs.length ? jobs.map(j => {
            const pill = j.status === 'done' ? '<span class="ctx-pill ok">done</span>'
                : j.status === 'failed' ? '<span class="ctx-pill err">failed</span>'
                : j.status === 'cancelled' ? '<span class="ctx-pill muted">cancelled</span>'
                : j.status === 'paused' ? '<span class="ctx-pill warn" title="Embedder unreachable — retrying">paused</span>'
                : `<span class="ctx-pill warn">${esc(j.status)}</span>`;
            const prog = j.total ? ` ${j.embedded || 0}/${j.total}` : '';
            const failed = j.failed ? ` · <span class="ctx-pill err">${j.failed} failed</span>` : '';
            const err = j.error ? `<div class="exp-sub label-sys" title="${attr(j.error)}">${esc(String(j.error).slice(0, 120))}</div>` : '';
            const when = j.created_at ? new Date(j.created_at).toLocaleString() : '';
            const what = (j.metadata && j.metadata.filename) ? j.metadata.filename : (j.job_type || j.target || 'job');
            return `<div class="ctx-card"><div class="ctx-card-main">
                <span class="ctx-card-title">#${j.id} ${esc(what)}</span> ${pill}
                <span class="label-sys">${prog}${failed}</span>
                <span class="exp-sub label-sys">${esc(when)}</span></div>${err}</div>`;
        }).join('') : empty('No embed jobs yet', 'Upload a file or hit Embed now and the job appears here.');
        // Live poll while anything is active AND the lens is visible.
        clearTimeout(_embedQueueTimer);
        const lensEl = document.getElementById('lens-embedding');
        if (active.length && lensEl && !lensEl.hidden) {
            _embedQueueTimer = setTimeout(loadEmbedding, 3000);
        }
    }
    function renderEmbedding() {
        const e = state.embedding || {};
        $('embed-stats').innerHTML = `
            <div class="ctx-stat"><span class="ctx-stat-n">${e.total || 0}</span><span class="ctx-stat-l">pieces</span></div>
            <div class="ctx-stat"><span class="ctx-stat-n">${e.embedded || 0}</span><span class="ctx-stat-l">embedded</span></div>
            <div class="ctx-stat ${e.unembedded ? 'alert' : ''}"><span class="ctx-stat-n">${e.unembedded || 0}</span><span class="ctx-stat-l">unembedded</span></div>`;
        $('embed-note').textContent = e.note || '';
        $('embed-run').disabled = !(e.unembedded > 0);
    }
    function _embedEpNorm(u) {
        return String(u || '').trim().toLowerCase().replace(/\/+$/, '').replace(/\/api$/, '');
    }
    function renderEmbedModel() {
        const card = $('embed-model-card');
        const baList = $('embed-bastions-list');
        if (!card) return;
        const cur = state.embedModel || {};
        if (cur._err) { card.innerHTML = errBox(cur._err); if (baList) baList.innerHTML = ''; return; }
        const prov = cur.provider || 'privatae';
        const onBastion = prov === 'local';
        const basRaw = state.embedBastions;
        const basErr = basRaw && basRaw._err ? basRaw._err : null;
        const bas = Array.isArray(basRaw) ? basRaw : [];
        const baseNorm = onBastion ? _embedEpNorm(cur.embedding_base_url) : '';
        const serving = baseNorm
            ? bas.find(b => {
                const ep = _embedEpNorm(b.embed_endpoint);
                return ep && (ep === baseNorm || baseNorm.indexOf(ep) === 0 || ep.indexOf(baseNorm) === 0);
            })
            : null;
        const servingName = serving ? (serving.name || serving.hostname) : null;
        const provPill = onBastion
            ? `<span class="ctx-pill ok" title="Served on a machine you run">${esc(servingName ? 'local · ' + servingName : 'local')}</span>`
            : '<span class="ctx-pill backend-pg" title="Served on the platform">managed</span>';
        card.innerHTML = `<div class="ctx-card">
            <div class="ctx-card-main">
                <span class="ctx-card-title">${esc(cur.model || 'default')}</span>
                ${provPill}
                ${cur.dim ? `<span class="label-sys">${esc(cur.dim)}d</span>` : ''}
            </div>
            <div class="ctx-card-actions">
                ${onBastion ? '<button class="cseries-btn ghost" id="embed-use-managed">Use managed</button>' : ''}
            </div>
        </div>`;
        if (baList) {
            if (basErr) {
                baList.innerHTML = errBox(basErr);
            } else if (!bas.length) {
                baList.innerHTML = empty('No embed-capable bastion online yet',
                    'Stand up a bastion in Network and start ollama on it (advertising the embed capability), then serve your embeddings on your own machine here.');
            } else {
                baList.innerHTML = bas.map(b => {
                    const isCur = serving && serving.hostname === b.hostname;
                    const ep = b.embed_endpoint || '—';
                    return `<div class="ctx-card machine">
                        <div class="ctx-card-main">
                            <span class="ctx-dot live"></span>
                            <span class="ctx-card-title">${esc(b.hostname)}</span>
                            ${b.name ? `<span class="label-sys">${esc(b.name)}</span>` : ''}
                            ${isCur ? '<span class="ctx-pill ok">● serving your embeddings</span>' : ''}
                        </div>
                        <div class="ctx-card-actions">
                            <span class="ctx-card-meta label-sys" title="${attr(ep)}">embed · seen ${esc(fmtRel(b.last_seen_at))}</span>
                            ${isCur ? '' : `<button class="cseries-btn" data-embed-serve="${attr(b.hostname)}">Serve here</button>`}
                        </div>
                    </div>`;
                }).join('');
            }
        }
        const um = $('embed-use-managed');
        if (um) um.addEventListener('click', useManagedEmbeds);
        if (baList) {
            baList.querySelectorAll('[data-embed-serve]').forEach(btn => {
                btn.addEventListener('click', () => serveEmbedsOn(btn.getAttribute('data-embed-serve')));
            });
        }
    }
    async function serveEmbedsOn(server) {
        const cur = state.embedModel || {};
        const model = cur.model || 'your model';
        if (!confirm('Serve your embeddings on ' + server + '? Your current model (' + model + ') is kept and existing vectors are untouched — only WHERE new embeds compute moves to your machine.')) return;
        const st = $('embed-model-status');
        if (st) st.textContent = 'routing…';
        try {
            const res = await fetchJSON('/api/embedding/use-bastion', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ server }),
            });
            if (res && res.error) { if (st) st.textContent = res.error; return; }
            if (st) st.textContent = 'serving on ' + server;
        } catch (e) {
            if (st) st.textContent = e.message; return;
        }
        loadEmbedding();
    }
    async function useManagedEmbeds() {
        const cur = state.embedModel || {};
        const model = cur.model || 'your model';
        if (!confirm('Fall back to the managed embedder? Your current model (' + model + ') is kept and existing vectors are untouched — new embeds compute on the platform.')) return;
        const st = $('embed-model-status');
        if (st) st.textContent = 'switching…';
        try {
            const res = await fetchJSON('/api/embedding/use-managed', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
            });
            if (res && res.error) { if (st) st.textContent = res.error; return; }
            if (st) st.textContent = 'managed';
        } catch (e) {
            if (st) st.textContent = e.message; return;
        }
        loadEmbedding();
    }
    async function runEmbed(force) {
        const btn = force ? $('embed-force') : $('embed-run');
        const st = $('embed-status');
        btn.disabled = true;
        st.textContent = 'queuing…';
        try {
            const r = await fetchJSON(`${API}/embedding`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ force: !!force }),
            });
            st.textContent = (r.queued != null)
                ? (r.queued === 0 ? (r.message || 'Nothing to embed.') : `Queued ${r.queued} piece(s).`)
                : 'Queued.';
        } catch (err) {
            st.textContent = err.message;
        } finally {
            btn.disabled = false;
            loadEmbedding();
        }
    }

    // ════════════════════ MODAL (shared) ════════════════════
    function openModal(title, bodyHTML, actions) {
        $('ctx-modal-title').textContent = title;
        $('ctx-modal-body').innerHTML = bodyHTML;
        const wrap = $('ctx-modal-actions');
        wrap.innerHTML = '';
        (actions || []).forEach(a => {
            const b = document.createElement('button');
            b.className = `cseries-btn ${a.cls || ''}`.trim();
            b.textContent = a.label;
            if (a.id) b.id = a.id;
            if (a.disabled) b.disabled = true;
            b.addEventListener('click', a.onClick);
            wrap.appendChild(b);
        });
        $('ctx-scrim').hidden = false;
    }
    function closeModal() { $('ctx-scrim').hidden = true; }

    function backendForm(scope, existing) {
        existing = existing || {};
        const bas = (state.bastions && state.bastions.bastions) || [];
        const options = bas.map(b => `<option value="${attr(b.hostname)}" ${existing.server === b.hostname ? 'selected' : ''}>${esc(b.hostname)}</option>`).join('');
        const cur = existing.backend || 'pg';
        const avail = existing.availability || 'single';
        return `
        <label class="ctx-field"><span class="label-sys">Where it goes</span>
            <select class="cseries-input" id="f-backend">
                <option value="pg" ${cur === 'pg' ? 'selected' : ''}>Platform (kept for you)</option>
                <option value="s3" ${cur === 's3' ? 'selected' : ''}>S3 bucket (you own it)</option>
                <option value="bastion" ${cur === 'bastion' ? 'selected' : ''}>Your bastion (a machine you run)</option>
            </select>
        </label>
        <div id="f-s3" class="ctx-subfields" hidden>
            <label class="ctx-field"><span class="label-sys">Bucket</span><input class="cseries-input" id="f-bucket" value="${attr(existing.bucket)}" placeholder="my-context-bucket"></label>
            <label class="ctx-field"><span class="label-sys">Key prefix (optional)</span><input class="cseries-input" id="f-prefix" value="${attr(existing.prefix || existing.prefix_key)}" placeholder="context/"></label>
            <label class="ctx-field"><span class="label-sys">Region (optional)</span><input class="cseries-input" id="f-region" value="${attr(existing.region)}" placeholder="us-east-1"></label>
            <label class="ctx-field"><span class="label-sys">Endpoint (optional, for R2/B2/MinIO)</span><input class="cseries-input" id="f-endpoint" value="${attr(existing.endpoint_url)}" placeholder="https://…"></label>
            <label class="ctx-field"><span class="label-sys">Vault credential name</span><input class="cseries-input" id="f-cred" value="${attr(existing.cred_name || 'ctx:s3')}" placeholder="ctx:s3"></label>
            <p class="view-sub">Seal the bucket's credentials in your vault first (Keys), then reference its name here. The platform can't read them.</p>
        </div>
        <div id="f-bastion" class="ctx-subfields" hidden>
            <label class="ctx-field"><span class="label-sys">Machine</span>
                <select class="cseries-input" id="f-server">${options || '<option value="">— no online context-store machines —</option>'}</select>
            </label>
            <label class="ctx-field"><span class="label-sys">Root path (optional)</span><input class="cseries-input" id="f-root" value="${attr(existing.root || 'context/')}" placeholder="context/"></label>
        </div>
        <label class="ctx-field"><span class="label-sys">Availability</span>
            <select class="cseries-input" id="f-avail">
                <option value="single" ${avail === 'single' ? 'selected' : ''}>Single — one copy</option>
                <option value="replicate" ${avail === 'replicate' ? 'selected' : ''}>Replicate — copies across your machines</option>
            </select>
        </label>`;
    }
    function wireBackendForm() {
        const sel = $('f-backend');
        const sync = () => {
            const v = sel.value;
            $('f-s3').hidden = v !== 's3';
            $('f-bastion').hidden = v !== 'bastion';
        };
        sel.addEventListener('change', sync);
        sync();
    }
    function readBackendForm() {
        const backend = $('f-backend').value;
        const out = { backend, availability: $('f-avail').value };
        if (backend === 's3') {
            out.bucket = $('f-bucket').value.trim();
            const p = $('f-prefix').value.trim();
            if (p) { out.prefix = p; out.prefix_key = p; }
            const region = $('f-region').value.trim(); if (region) out.region = region;
            const ep = $('f-endpoint').value.trim(); if (ep) out.endpoint_url = ep;
            const cred = $('f-cred').value.trim(); if (cred) out.cred_name = cred;
        } else if (backend === 'bastion') {
            out.server = $('f-server').value.trim();
            const root = $('f-root').value.trim(); if (root) out.root = root;
        }
        return out;
    }
    function editDefault() {
        const cur = (state.placement && state.placement.current) || { backend: 'pg' };
        openModal('Change the default', backendForm('default', cur), [
            { label: 'Cancel', cls: 'ghost', onClick: closeModal },
            {
                label: 'Save default', cls: 'primary', id: 'modal-save', onClick: async () => {
                    const body = readBackendForm();
                    $('modal-save').disabled = true;
                    try {
                        await fetchJSON(`${API}/default`, {
                            method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(body),
                        });
                        closeModal();
                        state.placement = null; loadPlacement();
                    } catch (e) {
                        showModalError(e.message);
                        $('modal-save').disabled = false;
                    }
                }
            },
        ]);
        wireBackendForm();
    }
    function editRoute(existing) {
        const isEdit = !!(existing && existing.prefix);
        const body = `
            <label class="ctx-field"><span class="label-sys">Path prefix</span>
                <input class="cseries-input" id="f-route-prefix" value="${attr(existing && existing.prefix)}" placeholder="memory/journals/" ${isEdit ? 'readonly' : ''}>
            </label>
            <p class="view-sub">Context whose path starts with this prefix goes to the machine below. The longest matching prefix wins.</p>
            ${backendForm('route', existing || {})}`;
        openModal(isEdit ? `Edit route — ${existing.prefix}` : 'Add a route', body, [
            { label: 'Cancel', cls: 'ghost', onClick: closeModal },
            {
                label: isEdit ? 'Save route' : 'Add route', cls: 'primary', id: 'modal-save', onClick: async () => {
                    const out = readBackendForm();
                    out.prefix = $('f-route-prefix').value.trim();
                    if (!out.prefix) { showModalError("A path prefix is required."); return; }
                    $('modal-save').disabled = true;
                    try {
                        await fetchJSON(`${API}/route`, {
                            method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(out),
                        });
                        closeModal();
                        state.placement = null; loadPlacement();
                    } catch (e) {
                        showModalError(e.message);
                        $('modal-save').disabled = false;
                    }
                }
            },
        ]);
        wireBackendForm();
    }
    function showModalError(msg) {
        let bar = $('modal-err');
        if (!bar) {
            bar = document.createElement('div');
            bar.id = 'modal-err';
            bar.className = 'status-banner error';
            $('ctx-modal-body').prepend(bar);
        }
        bar.textContent = msg;
    }
    async function clearRoute(prefix) {
        if (!confirm(`Remove the route for "${prefix}"? It will fall back to the default. Already-stored context keeps its machine.`)) return;
        try {
            await fetchJSON(`${API}/route/${encodeURIComponent(prefix)}`, { method: 'DELETE' });
            state.placement = null; loadPlacement();
        } catch (e) {
            $('routes-status').textContent = e.message;
        }
    }

    // ── header health ────────────────────────────────────────
    async function loadHealth() {
        try {
            const pl = await fetchJSON(`${API}/placement`);
            state.placement = pl;
            const cur = (pl.current && pl.current.backend) || 'pg';
            const routes = (pl.routes || []).length;
            $('ctx-health').innerHTML = `
                <span class="ctx-health-chip" title="Where new context lands by default">
                    <span class="ctx-dot live"></span><span class="label-sys">default</span> <b>${esc(cur === 'pg' ? 'platform' : cur)}</b>
                </span>
                <span class="ctx-health-chip" title="Per-prefix routes">
                    <span class="label-sys">routes</span> <b>${routes}</b>
                </span>`;
        } catch (e) {
            $('ctx-health').innerHTML = `<span class="ctx-health-chip"><span class="ctx-dot"></span>${esc(e.message)}</span>`;
        }
    }

    // ── event wiring ─────────────────────────────────────────
    function wire() {
        // explorer
        $('files-refresh').addEventListener('click', () => { loadFiles(); loadRail(); });
        $('coll-new').addEventListener('click', newCollection);
        $('open-storage').addEventListener('click', () => showView('storage'));
        $('storage-close').addEventListener('click', () => showView('files'));
        $('search-close').addEventListener('click', () => { dropSearch(); $('exp-search').value = ''; showView('files'); });
        $('insp-close').addEventListener('click', closeInspector);
        $('insp-tag-add').addEventListener('click', () => {
            const t = ($('insp-tag-input').value || '').trim();
            if (t && state.selected) { addTagToFile(state.selected, t); $('insp-tag-input').value = ''; }
        });
        $('insp-tag-input').addEventListener('keydown', ev => {
            if (ev.key === 'Enter') { ev.preventDefault(); $('insp-tag-add').click(); }
        });
        $('exp-search').addEventListener('input', () => {
            clearTimeout(searchTimer);
            const q = ($('exp-search').value || '').trim();
            if (q.length < 2) { dropSearch(); if (state.view === 'search') showView('files'); return; }
            searchTimer = setTimeout(() => runSearch(q), SEARCH_DEBOUNCE_MS);
        });
        wireUpload();

        // storage lenses
        document.querySelectorAll('.exp-subnav-btn').forEach(b => {
            b.addEventListener('click', () => showStorageLens(b.dataset.lens));
        });
        $('idx-refresh').addEventListener('click', () => { state.index = null; loadIndex(); });
        let t = null;
        $('idx-filter').addEventListener('input', () => { clearTimeout(t); t = setTimeout(loadIndex, 250); });
        $('default-edit').addEventListener('click', editDefault);
        $('route-add').addEventListener('click', () => editRoute(null));
        $('dcn-add-folder').addEventListener('click', addDcnFolder);
        $('dcn-add-bucket').addEventListener('click', addDcnBucket);
        $('routes-list').addEventListener('click', (ev) => {
            const ed = ev.target.closest('[data-route-edit]');
            const cl = ev.target.closest('[data-route-clear]');
            if (ed) {
                const pfx = ed.getAttribute('data-route-edit');
                const r = (state.placement.routes || []).find(x => x.prefix === pfx);
                editRoute(r || { prefix: pfx });
            } else if (cl) {
                clearRoute(cl.getAttribute('data-route-clear'));
            }
        });
        $('embed-run').addEventListener('click', () => runEmbed(false));
        $('embed-force').addEventListener('click', () => runEmbed(true));

        $('ctx-modal-close').addEventListener('click', closeModal);
        $('ctx-scrim').addEventListener('click', (ev) => { if (ev.target === $('ctx-scrim')) closeModal(); });
        document.addEventListener('keydown', (ev) => {
            if (ev.key === 'Escape') {
                if (!$('ctx-scrim').hidden) closeModal();
                else if (!$('inspector').hidden) closeInspector();
            }
        });

        loadHealth();
    }

    document.addEventListener('DOMContentLoaded', () => {
        wire();
        loadRail();
        loadFiles();
    });

    window.ContextApp = { state, showView, setFilter };
})();
