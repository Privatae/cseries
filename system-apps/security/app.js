/* ================================================================
 * Security — merged app logic (scanning + governance).
 *
 * Five views: Overview, Permissions, Processes, Network, History.
 * Endpoints: /api/security/* (scanning) + /api/ceigas/* (governance).
 * ================================================================ */

(function () {
    'use strict';

    const state = {
        view: 'overview',
        scanning: false,
        lastResult: null,
        policy: null,
        signals: [],
    };

    // ── Utilities ────────────────────────────────────────────
    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    async function fetchJSON(url, opts) {
        const r = await fetch(url, opts || {});
        if (!r.ok) {
            const text = await r.text().catch(() => '');
            throw new Error(text || r.status);
        }
        return r.json();
    }

    // A scan that outlasts the router's 30 s answers 202 with a job
    // (server/routes/security.py run_scan); follow it to the scan's answer.
    async function settleJob(res) {
        if (!res || !res.pending || !res.poll) return res;
        const deadline = Date.now() + 15 * 60 * 1000;
        while (Date.now() < deadline) {
            await new Promise(r => setTimeout(r, 2000));
            let j;
            try { j = await fetchJSON(res.poll); }
            catch (e) { if (/^404\b|no such scan job/.test(String(e.message))) throw e; continue; }
            if (j.status === 'done') return j.result;
            if (j.status === 'failed') {
                const d = j.detail;
                throw new Error(typeof d === 'string' ? d : (d && d.message) || 'scan failed');
            }
        }
        throw new Error('The scan did not finish within 15 minutes.');
    }

    function friendlyTime(ts) {
        if (!ts) return '';
        const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
        if (isNaN(d)) return String(ts);
        return d.toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
    }

    function friendlyDate(ts) {
        if (!ts) return '';
        const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
        if (isNaN(d)) return String(ts);
        const now = new Date();
        const diff = now - d;
        if (diff < 60000) return 'just now';
        if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
        if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    }

    // ── View switching ──────────────────────────────────────
    function showView(view) {
        state.view = view;
        document.querySelectorAll('.cseries-nav-btn').forEach(btn => {
            const sel = btn.dataset.view === view;
            btn.classList.toggle('active', sel);
            btn.setAttribute('aria-selected', sel ? 'true' : 'false');
        });
        document.querySelectorAll('.app-view').forEach(v =>
            v.hidden = v.id !== `view-${view}`
        );
        if (view === 'permissions') renderPermissions();
        if (view === 'processes') loadProcesses();
        if (view === 'network') { loadNetwork(); }
        if (view === 'history') loadHistory();
    }

    // ── Governance data loader ──────────────────────────────
    async function loadGovernance() {
        try {
            const [policy, signals] = await Promise.all([
                fetchJSON('/api/ceigas/policy'),
                fetchJSON('/api/ceigas/signals?limit=200'),
            ]);
            state.policy = policy;
            state.signals = signals.signals || [];
        } catch (err) {
            console.error('Governance load failed', err);
        }
    }

    // ══════════════════════════════════════════════════════════
    //  OVERVIEW
    // ══════════════════════════════════════════════════════════

    function updateBanner(status, headline, detail) {
        const banner = document.getElementById('status-banner');
        const icon = document.getElementById('status-icon');
        const tagline = document.getElementById('status-tagline');

        banner.className = 'status-banner status-' + status;

        const icons = { secure: '\u2713', attention: '!', critical: '\u2715', no_scan: '\uD83D\uDEE1' };
        icon.textContent = icons[status] || '\uD83D\uDEE1';
        icon.className = 'status-icon status-icon-' + status;

        document.getElementById('status-headline').textContent = headline;
        document.getElementById('status-detail').textContent = detail;

        const labels = { secure: 'all clear', attention: 'needs attention', critical: 'action required' };
        tagline.textContent = labels[status] || 'system protection';
    }

    function severityPill(sev) {
        const cls = { critical: 'deny', high: 'warn', medium: 'info', low: 'dusk', info: 'allow' };
        return `<span class="cseries-pill ${cls[sev] || ''}">${esc(sev)}</span>`;
    }

    function categoryLabel(cat) {
        const labels = {
            packages: 'Software', permissions: 'Permissions', network: 'Network',
            processes: 'Processes', configuration: 'Configuration', container: 'Container',
        };
        return labels[cat] || cat;
    }

    function renderFindings(findings) {
        const area = document.getElementById('findings-area');
        if (!findings || findings.length === 0) {
            area.innerHTML = `<div class="cseries-empty">
                <span class="empty-title">No issues found</span>
                Your system passed all checks.
            </div>`;
            return;
        }

        const order = ['critical', 'high', 'medium', 'low', 'info'];
        findings.sort((a, b) => order.indexOf(a.severity) - order.indexOf(b.severity));

        const counts = {};
        findings.forEach(f => { counts[f.severity] = (counts[f.severity] || 0) + 1; });

        let html = '<div class="findings-summary">';
        for (const sev of order) {
            if (counts[sev]) {
                html += `<div class="finding-count finding-count-${sev}">
                    <span class="count-num">${counts[sev]}</span>
                    <span class="count-label">${sev}</span>
                </div>`;
            }
        }
        html += '</div>';

        html += '<div class="section-label label-sys">Findings</div>';
        html += '<div class="findings-list">';
        for (const f of findings) {
            html += `<div class="finding-card cseries-card subtle">
                <div class="finding-header">
                    ${severityPill(f.severity)}
                    <span class="finding-category">${esc(categoryLabel(f.category))}</span>
                    ${f.cve ? `<span class="finding-cve">${esc(f.cve)}</span>` : ''}
                </div>
                <div class="finding-title">${esc(f.title)}</div>
                <div class="finding-desc">${esc(f.description)}</div>
                ${f.recommendation ? `<div class="finding-rec">
                    <span class="rec-label">Recommendation:</span> ${esc(f.recommendation)}
                </div>` : ''}
            </div>`;
        }
        html += '</div>';
        area.innerHTML = html;
    }

    function renderProfile(profile) {
        if (!profile) return;
        const el = document.getElementById('system-profile');
        const grid = document.getElementById('profile-grid');
        el.hidden = false;

        const items = [
            { label: 'Operating System', value: profile.os || 'Unknown' },
            { label: 'Installed Packages', value: profile.package_count || '\u2014' },
            { label: 'Open Ports', value: (profile.open_ports || []).join(', ') || 'None detected' },
            { label: 'Running Processes', value: profile.process_count || '\u2014' },
            { label: 'Container Security', value: profile.container_hardening || 'Unknown' },
        ];

        grid.innerHTML = items.map(i =>
            `<div class="profile-item">
                <div class="profile-label">${esc(i.label)}</div>
                <div class="profile-value">${esc(i.value)}</div>
            </div>`
        ).join('');
    }

    async function runScan() {
        if (state.scanning) return;
        state.scanning = true;

        const btn = document.getElementById('btn-scan');
        const progress = document.getElementById('scan-progress');
        const progressLabel = document.getElementById('progress-label');
        const progressFill = document.getElementById('progress-fill');

        btn.disabled = true;
        btn.textContent = 'Scanning...';
        progress.hidden = false;
        document.getElementById('findings-area').innerHTML = '';
        document.getElementById('system-profile').hidden = true;

        const steps = [
            'Collecting system data...',
            'Checking file permissions...',
            'Analyzing installed packages...',
            'Reviewing network configuration...',
            'Sending to the analysis model...',
            'Processing results...',
        ];

        let step = 0;
        const stepInterval = setInterval(() => {
            step = Math.min(step + 1, steps.length - 1);
            progressLabel.textContent = steps[step];
            progressFill.style.width = Math.min(15 + step * 16, 90) + '%';
        }, 3000);

        try {
            const result = await settleJob(await fetchJSON('/api/security/scan', { method: 'POST' }));
            state.lastResult = result;

            clearInterval(stepInterval);
            progressFill.style.width = '100%';
            progressLabel.textContent = result.model
                ? `Scan complete (analyzed by ${result.model})`
                : 'Scan complete';

            setTimeout(() => { progress.hidden = true; }, 800);

            updateBanner(
                result.overall_status || 'attention',
                statusHeadline(result.overall_status),
                result.summary || 'Scan completed'
            );

            renderFindings(result.findings);
            renderProfile(result.system_profile);

            document.getElementById('footer-right').textContent =
                'last scan: ' + new Date().toLocaleTimeString();

        } catch (err) {
            clearInterval(stepInterval);
            progress.hidden = true;
            updateBanner('attention', 'Scan could not complete',
                'Something went wrong. Try again.');
        } finally {
            state.scanning = false;
            btn.disabled = false;
            btn.textContent = 'Run Scan';
        }
    }

    function statusHeadline(status) {
        return {
            secure: 'All clear',
            attention: 'Some items need attention',
            critical: 'Critical issues found',
        }[status] || 'Scan complete';
    }

    async function loadStatus() {
        try {
            const data = await fetchJSON('/api/security/status');
            if (data.status === 'no_scan') {
                updateBanner('no_scan', 'No scan yet',
                    'Run your first scan to check for vulnerabilities, outdated packages, and misconfigured permissions.');
            } else {
                updateBanner(data.status, statusHeadline(data.status), data.message || '');
                if (data.last_scan) {
                    const d = new Date(data.last_scan.timestamp * 1000);
                    document.getElementById('footer-right').textContent =
                        'last scan: ' + d.toLocaleDateString() + ' ' + d.toLocaleTimeString();
                }
            }
        } catch (err) {
            // keep default state
        }
    }

    // ══════════════════════════════════════════════════════════
    //  PERMISSIONS
    // ══════════════════════════════════════════════════════════

    function renderPermissions() {
        const container = document.getElementById('permissions-list');
        if (!container) return;

        if (!state.policy || !state.policy.rules) {
            container.innerHTML = `<div class="cseries-empty">
                <span class="empty-title">No permissions loaded</span>
                Permissions will appear here once your Host receives its first policy.
            </div>`;
            return;
        }

        const defaultStance = state.policy.default || 'deny';
        const stanceText = defaultStance === 'allow'
            ? 'Anything not listed here is allowed by default.'
            : 'Anything not listed here is blocked by default.';

        let html = `
            <div class="default-stance">
                <div class="row">
                    <span class="cseries-pill ${defaultStance === 'allow' ? 'allow' : 'deny'}">default: ${defaultStance === 'allow' ? 'allowed' : 'blocked'}</span>
                    <span class="label-sys">${state.policy.rules.length} permission${state.policy.rules.length !== 1 ? 's' : ''}</span>
                </div>
                <div class="explain">${stanceText}</div>
            </div>
        `;

        html += state.policy.rules.map(r => {
            const isAllowed = r.allow;
            const pillClass = isAllowed ? 'allow' : 'deny';
            const pillText = isAllowed ? 'Allowed' : 'Blocked';
            const scopeText = formatScope(r.scope);

            // Source label
            const source = r.source || 'default';
            const isGranted = source.startsWith('grant:');
            const sourceLabel = isGranted ? 'You approved this' : 'Built-in';

            // Expiry
            let expiryText = '';
            if (r.expires) {
                const d = new Date(r.expires);
                if (!isNaN(d)) {
                    expiryText = 'Expires ' + d.toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
                } else {
                    expiryText = r.expires;
                }
            }

            return `
                <div class="rule-row">
                    <div class="rule-top">
                        <div class="rule-title">
                            <span class="rule-tool">${esc(r.tool)}</span>
                            <span class="cseries-pill ${pillClass}">${pillText}</span>
                            ${r.name ? `<span class="rule-name">${esc(r.name)}</span>` : ''}
                        </div>
                        <div class="rule-actions">
                            <span class="rule-source label-sys">${esc(sourceLabel)}</span>
                        </div>
                    </div>
                    ${scopeText ? `<div class="rule-scope">${esc(scopeText)}</div>` : ''}
                    ${expiryText ? `<div class="rule-meta"><span class="cseries-pill warn">${esc(expiryText)}</span></div>` : ''}
                </div>
            `;
        }).join('');

        container.innerHTML = html;
    }

    function formatScope(scope) {
        if (!scope || typeof scope !== 'object') return '';
        return Object.entries(scope).map(([k, v]) => {
            if (Array.isArray(v)) return `${k}: [${v.map(x => String(x)).join(', ')}]`;
            if (typeof v === 'object') return `${k}: ${JSON.stringify(v)}`;
            return `${k}: ${v}`;
        }).join(' \u00B7 ');
    }

    // ══════════════════════════════════════════════════════════
    //  PROCESSES
    // ══════════════════════════════════════════════════════════

    async function loadProcesses() {
        const el = document.getElementById('process-list');
        el.innerHTML = '<div class="loading">Loading processes...</div>';
        try {
            const data = await fetchJSON('/api/security/processes');
            if (!data.processes || data.processes.length === 0) {
                el.innerHTML = '<div class="cseries-empty"><span class="empty-title">No process data available</span></div>';
                return;
            }
            let html = `<table class="proc-table">
                <thead><tr>
                    <th>Process</th><th>User</th><th>CPU %</th><th>Memory %</th><th>PID</th>
                </tr></thead><tbody>`;
            for (const p of data.processes) {
                const cpuWarn = parseFloat(p.cpu) > 50 ? ' class="high-usage"' : '';
                const memWarn = parseFloat(p.mem) > 30 ? ' class="high-usage"' : '';
                html += `<tr>
                    <td class="proc-cmd" title="${esc(p.command)}">${esc(p.command)}</td>
                    <td>${esc(p.user)}</td>
                    <td${cpuWarn}>${esc(p.cpu)}%</td>
                    <td${memWarn}>${esc(p.mem)}%</td>
                    <td class="proc-pid">${esc(p.pid)}</td>
                </tr>`;
            }
            html += '</tbody></table>';
            el.innerHTML = html;
        } catch (err) {
            el.innerHTML = `<div class="cseries-empty"><span class="empty-title">Could not load processes</span>
                Try refreshing. If this persists, the system may be under heavy load.</div>`;
        }
    }

    // ══════════════════════════════════════════════════════════
    //  NETWORK
    // ══════════════════════════════════════════════════════════

    async function loadNetwork() {
        const el = document.getElementById('network-info');
        el.innerHTML = '<div class="loading">Loading network data...</div>';
        try {
            const data = await fetchJSON('/api/security/network');

            let html = '<div class="section-label label-sys">Listening Ports</div>';
            html += `<pre class="net-output">${esc(data.listeners || 'No listeners detected')}</pre>`;

            html += '<div class="section-label label-sys" style="margin-top: var(--space-5)">Active Connections</div>';
            html += `<pre class="net-output">${esc(data.connections || 'No active connections')}</pre>`;

            el.innerHTML = html;
        } catch (err) {
            el.innerHTML = `<div class="cseries-empty"><span class="empty-title">Could not load network data</span>
                Try refreshing.</div>`;
        }
    }

    // ══════════════════════════════════════════════════════════
    //  HISTORY (combined scans + audit signals)
    // ══════════════════════════════════════════════════════════

    async function loadHistory() {
        const el = document.getElementById('history-list');
        el.innerHTML = '<div class="loading">Loading activity history...</div>';

        const filter = document.getElementById('history-filter')?.value || 'all';

        try {
            // Load scans and signals in parallel, fetching signals fresh
            // (don't rely on the 15s governance poll's possibly-stale copy).
            const [scanData, signalData] = await Promise.all([
                fetchJSON('/api/security/history').catch(() => ({ scans: [] })),
                fetchJSON('/api/ceigas/signals?limit=200').catch(() => ({ signals: state.signals || [] })),
            ]);

            const scans = scanData.scans || [];
            const signals = signalData.signals || [];

            // Build unified timeline
            const entries = [];

            if (filter === 'all' || filter === 'scans') {
                for (const s of scans) {
                    entries.push({
                        type: 'scan',
                        time: s.timestamp ? s.timestamp * 1000 : 0,
                        data: s,
                    });
                }
            }

            if (filter === 'all' || filter === 'signals') {
                for (const s of signals) {
                    entries.push({
                        type: 'signal',
                        time: s.created_at ? new Date(s.created_at).getTime() : 0,
                        data: s,
                    });
                }
            }

            // Sort newest first
            entries.sort((a, b) => b.time - a.time);

            if (entries.length === 0) {
                const msg = filter === 'scans'
                    ? 'No scans yet. Run your first scan from the Overview tab.'
                    : filter === 'signals'
                        ? 'No permission changes recorded yet.'
                        : 'No activity yet. Run your first scan from the Overview tab.';
                el.innerHTML = `<div class="cseries-empty"><span class="empty-title">No activity</span>${msg}</div>`;
                return;
            }

            let html = '';
            for (const entry of entries) {
                if (entry.type === 'scan') {
                    html += renderScanEntry(entry.data);
                } else {
                    html += renderSignalEntry(entry.data);
                }
            }
            el.innerHTML = html;

            // Bind scan click handlers
            el.querySelectorAll('.history-scan-row').forEach(row => {
                row.style.cursor = 'pointer';
                row.addEventListener('click', () => loadHistoryScan(row.dataset.scan));
            });

        } catch (err) {
            el.innerHTML = `<div class="cseries-empty"><span class="empty-title">Could not load history</span>Something went wrong. Try again.</div>`;
        }
    }

    function renderScanEntry(s) {
        const date = new Date(s.timestamp * 1000);
        const statusCls = { secure: 'allow', attention: 'warn', critical: 'deny' };
        return `<div class="cseries-row history-scan-row" data-scan="${esc(s.scan_id)}">
            <div class="row-meta">
                <div class="row-title">${date.toLocaleDateString()} ${date.toLocaleTimeString()}</div>
                <div class="row-sub">${esc(s.summary)}</div>
            </div>
            <div class="row-actions">
                ${s.critical_count ? `<span class="cseries-pill deny">${s.critical_count} critical</span>` : ''}
                ${s.high_count ? `<span class="cseries-pill warn">${s.high_count} high</span>` : ''}
                <span class="cseries-pill ${statusCls[s.overall_status] || 'info'}">scan</span>
            </div>
        </div>`;
    }

    function renderSignalEntry(s) {
        const time = s.created_at ? friendlyDate(s.created_at) : '';
        const tool = s.tool || '';
        const type = s.signal_type || '';

        // Friendly action description
        let action = '';
        if (type === 'permission_policy_grant' || type.includes('grant')) {
            action = 'was allowed to use';
        } else if (type === 'permission_policy_revoke' || type.includes('revoke')) {
            action = 'had permission removed for';
        } else if (type === 'permission_incident' || type.includes('denied') || type.includes('deny')) {
            action = 'was blocked from using';
        } else if (type === 'permission_usage') {
            action = 'used';
        } else {
            action = s.content || type;
        }

        const pillClass = type.includes('grant') ? 'allow'
            : type.includes('revoke') || type.includes('deny') || type.includes('incident') ? 'deny'
            : 'info';

        return `<div class="signal-row">
            <span class="signal-time">${esc(time)}</span>
            <span class="signal-desc">Your Host ${esc(action)}${tool ? ' ' : ''}<strong>${esc(tool)}</strong></span>
            <span class="cseries-pill ${pillClass}">${type.includes('grant') ? 'allowed' : type.includes('revoke') ? 'revoked' : type.includes('deny') || type.includes('incident') ? 'blocked' : 'activity'}</span>
        </div>`;
    }

    async function loadHistoryScan(scanId) {
        try {
            const result = await fetchJSON(`/api/security/history/${encodeURIComponent(scanId)}`);
            showView('overview');
            state.lastResult = result;
            updateBanner(
                result.overall_status || 'attention',
                statusHeadline(result.overall_status),
                result.summary || ''
            );
            renderFindings(result.findings);
            renderProfile(result.system_profile);
            document.getElementById('footer-right').textContent =
                'viewing: ' + new Date(result.timestamp * 1000).toLocaleDateString();
        } catch (err) {
            // stay on history view
            showToast('Could not load that scan. Try again.');
        }
    }

    // ══════════════════════════════════════════════════════════
    //  TOAST (error feedback)
    // ══════════════════════════════════════════════════════════

    function showToast(msg) {
        let toast = document.querySelector('.toast');
        if (!toast) {
            toast = document.createElement('div');
            toast.className = 'toast';
            document.body.appendChild(toast);
        }
        toast.textContent = msg;
        toast.classList.add('visible');
        clearTimeout(toast._timer);
        toast._timer = setTimeout(() => toast.classList.remove('visible'), 3500);
    }

    // ══════════════════════════════════════════════════════════
    //  BOOT
    // ══════════════════════════════════════════════════════════

    document.addEventListener('DOMContentLoaded', () => {
        // Nav
        document.querySelectorAll('.cseries-nav-btn').forEach(btn =>
            btn.addEventListener('click', () => showView(btn.dataset.view))
        );

        // Overview controls
        document.getElementById('btn-scan').addEventListener('click', runScan);
        document.getElementById('btn-refresh-procs').addEventListener('click', loadProcesses);
        document.getElementById('btn-refresh-net').addEventListener('click', loadNetwork);

        // History filter
        const historyFilter = document.getElementById('history-filter');
        if (historyFilter) historyFilter.addEventListener('change', loadHistory);

        // Initial loads
        loadVersion();
        loadGovernance().then(() => loadStatus());

        // Refresh governance every 15 seconds, paused while the tab is hidden
        // so a backgrounded window stops polling the bridge.
        setInterval(() => {
            if (document.visibilityState === 'visible') loadGovernance();
        }, 15000);
    });

    // Footer version comes from the installed manifest (registry) rather than a
    // hardcoded string that silently drifts from app.yaml. Leaves the markup
    // placeholder if the registry is unreachable.
    async function loadVersion() {
        const el = document.getElementById('footer-version');
        if (!el) return;
        try {
            const r = await fetch('/api/applications/security', { credentials: 'same-origin' });
            if (!r.ok) return;
            const a = await r.json();
            if (a.version) el.textContent = a.version;
        } catch (e) {
            /* leave placeholder */
        }
    }

    window.App = { state, showView, runScan };
})();
