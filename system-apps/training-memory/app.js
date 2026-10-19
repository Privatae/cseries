(function () {
    'use strict';

    const API = '/api/training-memory';

    const state = {
        view: 'upload',
        source: 'upload',
        files: null,
        role: '',
        billing: 'byo',
        byoKey: '',
        sdkSince: '',
        jobId: null,
        es: null,
        lastPhase: null,
    };

    function showView(view) {
        state.view = view;
        document.querySelectorAll('.app-view').forEach(v => {
            v.hidden = v.id !== `view-${view}`;
        });
    }

    function setFooter(text) {
        document.getElementById('footer-status').textContent = text;
    }

    async function fetchJSON(url, opts) {
        const r = await fetch(url, opts || {});
        if (!r.ok) throw new Error(`${url} → ${r.status}`);
        return r.json();
    }

    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function refreshEstimateButton() {
        // BYOK with container env fallback means an empty key field is fine.
        // SDK source needs no upload — only a role.
        const haveSource = state.source === 'sdk' ? true : !!state.files;
        const ok = haveSource && state.role.trim();
        document.getElementById('estimate-btn').disabled = !ok;
    }

    // ── Role suggestions ─────────────────────────────────────
    async function loadRoleSuggestions() {
        try {
            const data = await fetchJSON(`${API}/existing-roles`);
            const container = document.getElementById('role-suggestions');
            container.innerHTML = '';
            (data.roles || []).forEach(r => {
                const pill = document.createElement('button');
                pill.className = 'cseries-pill';
                pill.textContent = r;
                pill.addEventListener('click', () => {
                    document.getElementById('role').value = r;
                    state.role = r;
                    refreshEstimateButton();
                });
                container.appendChild(pill);
            });
        } catch (e) {
            console.warn('role suggestions failed', e);
        }
    }

    // ── Upload ───────────────────────────────────────────────
    async function uploadAndEstimate() {
        const fd = new FormData();
        fd.append('role', state.role.trim());
        fd.append('billing', state.billing);
        if (state.billing === 'byo') fd.append('byo_key', state.byoKey);

        let endpoint;
        if (state.source === 'sdk') {
            setFooter('gathering recent sessions…');
            if (state.sdkSince) fd.append('since', state.sdkSince);
            endpoint = `${API}/jobs/from-sdk-sessions`;
        } else {
            setFooter('uploading…');
            for (const f of state.files) fd.append('files', f, f.webkitRelativePath || f.name);
            endpoint = `${API}/jobs`;
        }

        const job = await fetchJSON(endpoint, { method: 'POST', body: fd });
        state.jobId = job.id;

        setFooter('estimating…');
        const est = await fetchJSON(`${API}/jobs/${state.jobId}/estimate`);
        renderEstimate(est);
        // Fresh estimate view → the run button is armed again.
        document.getElementById('confirm-btn').disabled = false;
        showView('estimate');
        setFooter('ready to run');
    }

    function renderEstimate(est) {
        const inferenceCost = `~$${est.byo_cost_estimate_usd.toFixed(2)} on your anthropic account`;
        document.getElementById('estimate-stats').innerHTML = `
            <div><div class="tm-stat-label">sessions</div><div class="tm-stat-value">${est.sessions}</div></div>
            <div><div class="tm-stat-label">turns</div><div class="tm-stat-value">${est.turns.toLocaleString()}</div></div>
            <div><div class="tm-stat-label">pairs</div><div class="tm-stat-value">${est.pairs.toLocaleString()}</div></div>
            <div><div class="tm-stat-label">estimated tokens</div><div class="tm-stat-value">${(est.tokens / 1000).toFixed(0)}k</div></div>
            <div><div class="tm-stat-label">role</div><div class="tm-stat-value">${esc(state.role)}</div></div>
            <div><div class="tm-stat-label">inference cost</div><div class="tm-stat-value">${inferenceCost}</div></div>
        `;
    }

    // ── Run + progress ───────────────────────────────────────
    async function startRun() {
        await fetchJSON(`${API}/jobs/${state.jobId}/start`, { method: 'POST' });
        showView('running');
        setFooter('running');

        state.lastPhase = null;
        state.es = new EventSource(`${API}/jobs/${state.jobId}/events`);
        state.es.onmessage = e => {
            const evt = JSON.parse(e.data);
            handleEvent(evt);
        };
        state.es.onerror = () => {
            // onerror also fires on a normal end-of-stream close. Only pull the
            // final result if the stream actually reached the commit-done phase;
            // a classify run ends at ready_for_review and an error ends at error.
            state.es && state.es.close();
            if (state.lastPhase === 'done') {
                fetchJSON(`${API}/jobs/${state.jobId}/result`).then(renderReport).catch(console.error);
            }
        };
    }

    function handleEvent(evt) {
        if (evt.phase) {
            state.lastPhase = evt.phase;
            document.getElementById('phase-line').textContent = evt.phase;
        }
        if (typeof evt.progress === 'number') {
            document.getElementById('progress-bar').style.width = (evt.progress * 100).toFixed(1) + '%';
        }
        if (evt.counters) {
            document.getElementById('counters').innerHTML = Object.entries(evt.counters)
                .map(([k, v]) => `<span>${esc(k)}: ${esc(v)}</span>`).join('');
        }
        // The worker emits a terminal {phase:'error', done:true} on failure.
        if (evt.phase === 'error') {
            state.es && state.es.close();
            document.getElementById('phase-line').textContent = 'failed: ' + (evt.error || 'unknown error');
            setFooter('error: ' + (evt.error || 'job failed'));
            return;
        }
        if (evt.done) {
            state.es && state.es.close();
            // Classify-phase done → load the review screen.
            // Commit-phase done → load the final result.
            if (evt.phase === 'ready_for_review') {
                loadReview();
            } else if (evt.phase === 'done') {
                fetchJSON(`${API}/jobs/${state.jobId}/result`).then(renderReport).catch(console.error);
            }
        }
    }

    // ── Review screen ────────────────────────────────────────
    // A feedback pair whose classifier type is null or outside the three
    // known ones is grouped as "unclassified", so every pending pair has a
    // group, a filter checkbox and a place on screen.
    const REVIEW_TYPES = ['correction', 'frustration', 'rejection'];
    function typeGroup(row) {
        return REVIEW_TYPES.includes(row.classifier_type) ? row.classifier_type : 'unclassified';
    }

    const review = {
        rows: [],
        sessions: [],
        filter: {
            min_conf: 0.70,
            types: new Set(['correction', 'frustration', 'rejection', 'unclassified']),
            kinds: new Set(['interrupt', 'tokens']),
            session: 'all',
            search: '',
            showNo: false,
        },
        debounceTimers: {},
    };

    async function loadReview() {
        showView('review');
        setFooter('loading review');
        try {
            const data = await fetchJSON(`${API}/jobs/${state.jobId}/review?show_all=true`);
            review.rows = data.rows;
            review.sessions = data.sessions;
            document.getElementById('no-count').textContent = data.counts.skipped_no_feedback || 0;
            renderNotClassified(data.counts);
            populateSessionFilter(data.sessions);
            renderReviewList();
            setFooter('reviewing');
        } catch (e) {
            setFooter('error loading review: ' + e.message);
        }
    }

    // Pairs the batch could not classify have no row to review; say how many
    // and why, so the review total adds up to the pairs that were sent.
    function renderNotClassified(counts) {
        const el = document.getElementById('not-classified-note');
        if (!el) return;
        const n = (counts && counts.not_classified) || 0;
        if (!n) {
            el.hidden = true;
            el.textContent = '';
            return;
        }
        const reasons = Object.keys((counts && counts.not_classified_by_type) || {}).sort().join('/');
        el.textContent = n === 1
            ? `1 pair could not be classified${reasons ? ` (${reasons})` : ''} and is not shown.`
            : `${n} pairs could not be classified${reasons ? ` (${reasons})` : ''} and are not shown.`;
        el.hidden = false;
    }

    function populateSessionFilter(sessions) {
        const sel = document.getElementById('session-filter');
        sel.innerHTML = '<option value="all">all sessions</option>';
        sessions.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.id;
            opt.textContent = `${s.label} (${s.count})`;
            sel.appendChild(opt);
        });
    }

    function passesFilter(row) {
        const f = review.filter;
        const isYes = row.classifier_is_feedback === true;
        if (!isYes && !f.showNo) return false;
        if (isYes && (row.classifier_confidence || 0) < f.min_conf) return false;
        if (isYes && !f.types.has(typeGroup(row))) return false;
        if (!f.kinds.has(row.kind)) return false;
        if (f.session !== 'all' && row.session_id !== f.session) return false;
        if (f.search) {
            const q = f.search.toLowerCase();
            const hay = ((row.rule_final || '') + ' ' + (row.correction_text || '')).toLowerCase();
            if (!hay.includes(q)) return false;
        }
        return true;
    }

    function renderReviewList() {
        const list = document.getElementById('review-list');
        list.innerHTML = '';
        const visible = review.rows.filter(passesFilter);
        const groups = {};
        visible.forEach(r => {
            const k = typeGroup(r);
            if (!groups[k]) groups[k] = [];
            groups[k].push(r);
        });
        const order = ['correction', 'frustration', 'rejection', 'unclassified'];
        order.forEach(k => {
            if (!groups[k]) return;
            const h = document.createElement('div');
            h.className = 'tm-group-header label-sys';
            h.textContent = `${k} · ${groups[k].length} pairs`;
            list.appendChild(h);
            groups[k].forEach(r => list.appendChild(renderPairCard(r)));
        });
        updateBulkCounts(visible);
    }

    function renderPairCard(row) {
        const tpl = document.getElementById('tm-card-template');
        const node = tpl.content.firstElementChild.cloneNode(true);
        node.dataset.pairId = row.pair_id;
        node.dataset.status = row.status;
        node.querySelector('.tm-pair-conf').textContent =
            (row.classifier_confidence != null ? row.classifier_confidence.toFixed(2) : '—');
        node.querySelector('.tm-pair-type').textContent = row.classifier_type || '—';
        node.querySelector('.tm-pair-session').textContent = row.session_label || row.session_id || '';
        node.querySelector('.tm-pair-status').textContent = row.status;
        const esc = node.querySelector('.tm-pair-escalate');
        if (row.would_escalate_id) {
            esc.hidden = false;
            esc.querySelector('span').textContent = row.would_escalate_id;
        }
        const rule = node.querySelector('.tm-pair-rule');
        rule.value = row.rule_final || '';
        const roleInput = node.querySelector('.tm-pair-role');
        roleInput.value = row.role || '';
        const sev = node.querySelector('.tm-pair-severity');
        sev.value = row.severity || 'gentle';
        node.querySelector('.tm-pair-correction').textContent =
            'user said: ' + (row.correction_text || '');
        node.querySelector('.tm-pair-rejected').textContent =
            'Host replied: ' + (row.rejected_preview || '');

        // Debounced patches on edit.
        rule.addEventListener('blur', () => debouncePatch(row.pair_id, {rule_final: rule.value}));
        roleInput.addEventListener('blur', () => debouncePatch(row.pair_id, {role: roleInput.value}));
        sev.addEventListener('change', () => debouncePatch(row.pair_id, {severity: sev.value}));
        node.querySelectorAll('[data-action]').forEach(btn => {
            btn.addEventListener('click', () => {
                const action = btn.dataset.action;
                if (action === 'approve') patchPair(row.pair_id, {status: 'approved'});
                else if (action === 'reject') patchPair(row.pair_id, {status: 'rejected'});
                else if (action === 'reset') patchPair(row.pair_id, {status: 'pending', rule_final: row.rule_original});
            });
        });
        return node;
    }

    function debouncePatch(pairId, fields) {
        clearTimeout(review.debounceTimers[pairId]);
        review.debounceTimers[pairId] = setTimeout(() => patchPair(pairId, fields), 250);
    }

    async function patchPair(pairId, fields) {
        try {
            const updated = await fetchJSON(`${API}/jobs/${state.jobId}/review/${pairId}`, {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(fields),
            });
            const row = review.rows.find(r => r.pair_id === pairId);
            if (row) Object.assign(row, updated);
            renderReviewList();
        } catch (e) {
            setFooter('patch error: ' + e.message);
        }
    }

    function updateBulkCounts(visible) {
        const tally = {pending: 0, approved: 0, edited: 0, rejected: 0};
        visible.forEach(r => { if (tally[r.status] != null) tally[r.status]++; });
        const approvedTotal = review.rows.filter(r =>
            r.status === 'approved' || r.status === 'edited').length;
        document.getElementById('bulk-counts').textContent =
            `visible ${visible.length} · pending ${tally.pending} · approved ${tally.approved} · edited ${tally.edited} · rejected ${tally.rejected}`;
        const btn = document.getElementById('commit-btn');
        btn.disabled = approvedTotal === 0;
        btn.textContent = `commit ${approvedTotal} approved →`;
    }

    // A bulk action names the pairs on screen and the route acts on those
    // alone, so a pair hidden by the search or any other filter is never
    // touched. The list is read from the rendered cards, which are exactly
    // what the user is looking at when they press the button.
    async function doBulk(action, extra) {
        const pair_ids = Array.from(
            document.querySelectorAll('#review-list [data-pair-id]'),
            n => n.dataset.pairId);
        if (!pair_ids.length) return setFooter('no pairs visible');
        try {
            await fetchJSON(`${API}/jobs/${state.jobId}/review/bulk`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action, pair_ids, ...extra}),
            });
            await loadReview();
        } catch (e) {
            setFooter('bulk error: ' + e.message);
        }
    }

    async function startCommit() {
        try {
            await fetchJSON(`${API}/jobs/${state.jobId}/commit`, {method: 'POST'});
            showView('running');
            setFooter('committing');
            state.lastPhase = null;
            state.es = new EventSource(`${API}/jobs/${state.jobId}/events`);
            state.es.onmessage = e => handleEvent(JSON.parse(e.data));
            state.es.onerror = () => {
                state.es && state.es.close();
                if (state.lastPhase === 'done') {
                    fetchJSON(`${API}/jobs/${state.jobId}/result`).then(renderReport).catch(console.error);
                }
            };
        } catch (e) {
            setFooter('commit error: ' + e.message);
            // Stay on the review screen and re-arm the commit button to retry.
            const cb = document.getElementById('commit-btn');
            if (cb) cb.disabled = false;
        }
    }

    function renderReport(report) {
        document.getElementById('report-stats').innerHTML = `
            <div><div class="tm-stat-label">inserted</div><div class="tm-stat-value">${report.inserted}</div></div>
            <div><div class="tm-stat-label">escalated</div><div class="tm-stat-value">${report.escalated}</div></div>
            <div><div class="tm-stat-label">skipped</div><div class="tm-stat-value">${report.skipped}</div></div>
            <div><div class="tm-stat-label">role</div><div class="tm-stat-value">${esc(report.role)}</div></div>
        `;
        const ul = document.getElementById('sample-rules');
        ul.innerHTML = (report.sample_rules || []).map(r => `<li>${esc(r)}</li>`).join('');
        showView('report');
        setFooter('done');
    }

    // Footer version from the installed manifest (never hardcode it).
    async function loadVersion() {
        const el = document.getElementById('footer-version');
        if (!el) return;
        try {
            const a = await fetchJSON('/api/applications/training-memory');
            if (a.version) el.textContent = a.version;
        } catch (e) {
            // leave the placeholder if the registry is unreachable
        }
    }

    // ── Boot ─────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', () => {
        loadVersion();
        loadRoleSuggestions();

        const dz = document.getElementById('dropzone');
        const folderInput = document.getElementById('file');
        const zipInput = document.getElementById('zip');

        document.getElementById('pick-folder').addEventListener('click', () => folderInput.click());
        document.getElementById('pick-zip').addEventListener('click', () => zipInput.click());

        // ── Drag-and-drop ────────────────────────────────────
        // The dropzone copy advertises "drop a folder or zip"; wire the
        // matching handlers. The .dragover class (defined in style.css) was
        // previously unreachable. Dropped folders arrive as DataTransfer
        // entries — walk them recursively (preserving relative paths so the
        // upload's webkitRelativePath fallback still groups by session); a
        // dropped .zip arrives as a single file.
        function setDroppedFiles(files) {
            if (!files.length) return;
            state.files = files;
            const isZip = files.length === 1 && /\.zip$/i.test(files[0].name);
            dz.querySelector('.tm-dropzone-title').textContent = isZip
                ? `zip selected: ${files[0].name}`
                : `${files.length} files selected`;
            refreshEstimateButton();
        }

        function readEntry(entry, path, out) {
            return new Promise((resolve) => {
                if (entry.isFile) {
                    entry.file((file) => {
                        // Preserve the relative path so per-session grouping survives.
                        try {
                            Object.defineProperty(file, 'webkitRelativePath', {
                                value: path + file.name, configurable: true,
                            });
                        } catch (_) {}
                        out.push(file);
                        resolve();
                    }, () => resolve());
                } else if (entry.isDirectory) {
                    const reader = entry.createReader();
                    const all = [];
                    const readBatch = () => reader.readEntries((batch) => {
                        if (!batch.length) {
                            Promise.all(all.map((e) => readEntry(e, path + entry.name + '/', out)))
                                .then(resolve);
                            return;
                        }
                        all.push(...batch);
                        readBatch();
                    }, () => resolve());
                    readBatch();
                } else {
                    resolve();
                }
            });
        }

        ['dragenter', 'dragover'].forEach((ev) => dz.addEventListener(ev, (e) => {
            e.preventDefault();
            dz.classList.add('dragover');
        }));
        ['dragleave', 'dragend'].forEach((ev) => dz.addEventListener(ev, (e) => {
            e.preventDefault();
            if (ev === 'dragleave' && dz.contains(e.relatedTarget)) return;
            dz.classList.remove('dragover');
        }));
        dz.addEventListener('drop', (e) => {
            e.preventDefault();
            dz.classList.remove('dragover');
            const dt = e.dataTransfer;
            const items = dt && dt.items;
            const hasEntries = items && items.length &&
                typeof items[0].webkitGetAsEntry === 'function';
            if (hasEntries) {
                const entries = [];
                for (const item of items) {
                    const entry = item.webkitGetAsEntry && item.webkitGetAsEntry();
                    if (entry) entries.push(entry);
                }
                const out = [];
                Promise.all(entries.map((en) => readEntry(en, '', out)))
                    .then(() => setDroppedFiles(out));
            } else if (dt && dt.files && dt.files.length) {
                setDroppedFiles(Array.prototype.slice.call(dt.files));
            }
        });

        folderInput.addEventListener('change', () => {
            state.files = folderInput.files;
            dz.querySelector('.tm-dropzone-title').textContent =
                `${state.files.length} files selected`;
            refreshEstimateButton();
        });
        zipInput.addEventListener('change', () => {
            state.files = zipInput.files;
            dz.querySelector('.tm-dropzone-title').textContent = `zip selected: ${state.files[0].name}`;
            refreshEstimateButton();
        });

        const sdkField = document.getElementById('sdk-field');
        document.querySelectorAll('input[name="source"]').forEach(r => {
            r.addEventListener('change', () => {
                state.source = r.value;
                const isSdk = state.source === 'sdk';
                dz.hidden = isSdk;
                if (sdkField) sdkField.hidden = !isSdk;
                refreshEstimateButton();
            });
        });
        const sdkSince = document.getElementById('sdk-since');
        if (sdkSince) sdkSince.addEventListener('input', e => {
            state.sdkSince = e.target.value;
        });

        document.getElementById('role').addEventListener('input', e => {
            state.role = e.target.value;
            refreshEstimateButton();
        });
        document.querySelectorAll('input[name="billing"]').forEach(r => {
            r.addEventListener('change', () => {
                state.billing = r.value;
                document.getElementById('byo-field').hidden = state.billing !== 'byo';
                refreshEstimateButton();
            });
        });
        document.getElementById('byo-key').addEventListener('input', e => {
            state.byoKey = e.target.value;
            refreshEstimateButton();
        });

        const estimateBtn = document.getElementById('estimate-btn');
        estimateBtn.addEventListener('click', () => {
            estimateBtn.disabled = true;
            uploadAndEstimate()
                .catch(err => {
                    setFooter('error: ' + err.message);
                    // Re-enable so the user can retry; refresh re-checks form state.
                    refreshEstimateButton();
                });
        });
        document.getElementById('back-btn').addEventListener('click', () => showView('upload'));
        const confirmBtn = document.getElementById('confirm-btn');
        confirmBtn.addEventListener('click', () => {
            confirmBtn.disabled = true;
            startRun().catch(err => {
                setFooter('error: ' + err.message);
                confirmBtn.disabled = false;
            });
        });
        document.getElementById('new-run-btn').addEventListener('click', () => {
            state.jobId = null;
            state.files = null;
            showView('upload');
            setFooter('ready');
        });

        // Review screen wiring
        const confSlider = document.getElementById('conf-slider');
        if (confSlider) {
            confSlider.addEventListener('input', () => {
                review.filter.min_conf = parseFloat(confSlider.value);
                document.getElementById('conf-value').textContent = confSlider.value;
                renderReviewList();
            });
        }
        document.querySelectorAll('.tm-checkrow input[type=checkbox]').forEach(cb => {
            // Don't bind the show-no toggle in this loop; it's separate.
            if (cb.id === 'show-no-toggle') return;
            cb.addEventListener('change', () => {
                const parent = cb.closest('.tm-checkrow');
                const label = parent.querySelector('.label-sys')?.textContent || '';
                const setKey = label.includes('type') ? 'types'
                             : label.includes('kind') ? 'kinds' : null;
                if (!setKey) return;
                if (cb.checked) review.filter[setKey].add(cb.value);
                else review.filter[setKey].delete(cb.value);
                renderReviewList();
            });
        });
        const showNo = document.getElementById('show-no-toggle');
        if (showNo) showNo.addEventListener('change', () => {
            review.filter.showNo = showNo.checked;
            renderReviewList();
        });
        const sessionFilter = document.getElementById('session-filter');
        if (sessionFilter) sessionFilter.addEventListener('change', () => {
            review.filter.session = sessionFilter.value;
            renderReviewList();
        });
        const searchInput = document.getElementById('search-input');
        if (searchInput) {
            let t = null;
            searchInput.addEventListener('input', () => {
                clearTimeout(t);
                t = setTimeout(() => {
                    review.filter.search = searchInput.value;
                    renderReviewList();
                }, 200);
            });
        }
        document.querySelectorAll('[data-bulk]').forEach(btn => {
            btn.addEventListener('click', () => {
                const action = btn.dataset.bulk;
                if (action === 'reject_below_conf') {
                    const thr = parseFloat(document.getElementById('reject-conf').value);
                    doBulk(action, {confidence_threshold: thr});
                } else if (action === 'set_role') {
                    const role = document.getElementById('bulk-role').value.trim();
                    if (!role) return setFooter('enter a role first');
                    doBulk(action, {role});
                } else if (action === 'set_severity') {
                    const severity = document.getElementById('bulk-severity').value;
                    doBulk(action, {severity});
                } else {
                    doBulk(action, {});
                }
            });
        });
        const reviewBack = document.getElementById('review-back-btn');
        if (reviewBack) reviewBack.addEventListener('click', () => showView('upload'));
        const commitBtn = document.getElementById('commit-btn');
        if (commitBtn) commitBtn.addEventListener('click', () => {
            commitBtn.disabled = true;
            startCommit().catch(err => {
                setFooter('commit error: ' + err.message);
                commitBtn.disabled = false;
            });
        });
        const undoBtn = document.getElementById('undo-btn');
        if (undoBtn) undoBtn.addEventListener('click', async () => {
            try {
                const r = await fetchJSON(`${API}/jobs/${state.jobId}/undo`, {method: 'POST'});
                document.getElementById('undo-status').textContent =
                    `deactivated ${r.deactivated} rules (${r.elapsed_seconds}s after commit)`;
            } catch (e) {
                document.getElementById('undo-status').textContent =
                    'undo failed: ' + e.message;
            }
        });
    });

    window.App = { state, showView };
})();
