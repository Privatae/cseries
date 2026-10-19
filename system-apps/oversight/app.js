/* ================================================================
 * Oversight — the findings ledger, and the machines it is kept about.
 *
 * The ledger (oversight_findings)
 * is folded every five minutes by the host's standing op, with no model; this
 * window is the only place a person reads it.
 *
 * Reads:
 *   GET  /api/oversight/findings?resolved=&severity=&machine=&limit=
 *   GET  /api/oversight/findings/{id}
 *   GET  /api/oversight/machines        — present-tense state + open counts
 * The one verb:
 *   POST /api/oversight/findings/{id}/resolve
 *
 * EVERYTHING FROM A MACHINE IS TEXT. Hostnames, titles and evidence are written
 * by the machines themselves, so nothing here goes through innerHTML — a box
 * that named itself `<img onerror=…>` renders as those characters.
 *
 * NEVER AN ALL-CLEAR. The ledger holds problems only. An empty list is worded
 * as "nothing filed", and the tiles count what is open — they never turn green,
 * because zero open findings from machines nobody is watching is not health.
 * ================================================================ */
(function () {
    'use strict';

    var SEVERITIES = ['critical', 'high', 'warn', 'notice'];   // worst first
    var SEV_PILL = { critical: 'deny', high: 'warn', warn: 'info', notice: '' };
    var SOURCE_WORD = {
        alert: 'alert source', heartbeat: 'heartbeat', store: 'machine record',
        observe: 'observe', correlation: 'correlation',
    };
    // Alert-plane and detection-plane states → pill tone. Only the first two of
    // each are readings a person may take as "looked, and this is what it said";
    // the rest are ways of not looking, and none of them is ever green.
    var ALERT_PILL = { quiet: 'allow', firing: 'warn', critical: 'deny', stale: 'deny', unconfigured: '' };
    var DETECT_PILL = { clean: 'allow', findings: 'warn', partial: 'warn', unreported: '' };
    // The classes a pass can leave out (detect.skipped), in the words the
    // reading uses, so the line never says "no differences" about a class
    // nobody compared.
    var DETECT_CLASS_WORDS = { process: 'processes', scheduler: 'scheduled jobs',
                               listener: 'listening ports', identity: 'accounts' };
    var REFRESH_MS = 60 * 1000;

    var state = {
        view: 'findings',
        findings: [],
        findingsError: null,
        machines: [],
        other: [],
        openTotal: null,         // the route's per-severity total, one count per NAME
        ingest: null,
        machinesError: null,
        selected: null,          // finding id in the detail panel
        focusMachine: null,      // machine name highlighted in the Machines view
        scrollToFocus: false,    // scroll to it ONCE, not on every refresh
        timer: null,
        pendingResolve: null,
    };

    // ── small DOM helpers (text only) ────────────────────────────────────
    var $ = function (id) { return document.getElementById(id); };
    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = String(text);
        return n;
    }
    function pill(text, tone) {
        return el('span', 'cseries-pill' + (tone ? ' ' + tone : ''), text);
    }
    function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

    function rel(iso) {
        if (!iso) return 'never';
        var t = Date.parse(iso);
        if (isNaN(t)) return String(iso);
        var s = Math.round((Date.now() - t) / 1000);
        var future = s < 0;
        s = Math.abs(s);
        var out;
        if (s < 60) out = s + ' s';
        else if (s < 3600) out = Math.round(s / 60) + ' min';
        else if (s < 86400) out = Math.round(s / 3600) + ' h';
        else out = Math.round(s / 86400) + ' d';
        return future ? 'in ' + out : out + ' ago';
    }
    function abs(iso) {
        if (!iso) return '';
        var d = new Date(iso);
        return isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
    }
    function timeEl(iso, prefix) {
        var n = el('span', 'ov-time', (prefix || '') + rel(iso));
        if (iso) n.title = abs(iso);
        return n;
    }

    async function fetchJSON(url, opts) {
        var r = await fetch(url, opts || {});
        var body = null;
        try { body = await r.json(); } catch (e) { body = null; }
        if (!r.ok) {
            var why = body && (body.detail || body.error) ? (body.detail || body.error) : ('HTTP ' + r.status);
            var err = new Error(typeof why === 'string' ? why : JSON.stringify(why));
            err.status = r.status;
            throw err;
        }
        return body;
    }

    function banner(node, text, tone) {
        if (!text) { node.hidden = true; node.textContent = ''; return; }
        node.className = 'status-banner ' + (tone || 'info');
        node.textContent = text;
        node.hidden = false;
    }

    // ── what a finding is called ─────────────────────────────────────────
    function titleOf(f) {
        var ev = f.evidence || {};
        return (ev && typeof ev.title === 'string' && ev.title) || f.key || ('finding ' + f.id);
    }

    // ── data ─────────────────────────────────────────────────────────────
    function findingsUrl() {
        var q = [];
        var st = $('f-state').value;
        if (st === 'open') q.push('resolved=false');
        else if (st === 'resolved') q.push('resolved=true');
        var sev = $('f-severity').value;
        if (sev) q.push('severity=' + encodeURIComponent(sev));
        var m = $('f-machine').value;
        if (m) q.push('machine=' + encodeURIComponent(m));
        q.push('limit=1000');
        return '/api/oversight/findings?' + q.join('&');
    }

    async function loadFindings() {
        try {
            var d = await fetchJSON(findingsUrl());
            state.findings = (d && d.findings) || [];
            state.findingsError = null;
        } catch (e) {
            state.findings = [];
            state.findingsError = e;
        }
        renderFindings();
    }

    async function loadMachines() {
        try {
            var d = await fetchJSON('/api/oversight/machines');
            state.machines = (d && d.machines) || [];
            state.other = (d && d.other) || [];
            state.openTotal = (d && d.open_total) || null;
            state.ingest = d ? d.ingest : null;
            state.machinesError = null;
        } catch (e) {
            state.machines = [];
            state.other = [];
            state.openTotal = null;
            state.ingest = null;
            state.machinesError = e;
        }
        renderMachineFilter();
        renderTiles();
        renderIngest();
        renderMachines();
    }

    async function refresh() {
        await Promise.all([loadMachines(), loadFindings()]);
        if (state.selected != null && !$('ov-detail').hidden) openFinding(state.selected, true);
    }

    // ── findings view ────────────────────────────────────────────────────
    // The tiles read the route's own total, which counts each machine NAME once.
    // Adding the cards up double-counted: findings are filed by name, and two
    // bastions reporting one hostname are two cards carrying the same count.
    // The fallback (an older kernel with no open_total) de-duplicates by name.
    function openCounts() {
        if (state.openTotal) return state.openTotal;
        var tot = { critical: 0, high: 0, warn: 0, notice: 0 };
        var seen = {};
        state.machines.concat(state.other).forEach(function (m) {
            if (seen[m.machine]) return;
            seen[m.machine] = true;
            SEVERITIES.forEach(function (s) { tot[s] += Number((m.open || {})[s] || 0); });
        });
        return tot;
    }

    function renderTiles() {
        var box = $('ov-tiles');
        clear(box);
        if (state.machinesError) return;
        var tot = openCounts();
        SEVERITIES.forEach(function (s) {
            var b = el('button', 'cseries-card subtle ov-tile' + ($('f-severity').value === s ? ' active' : ''));
            b.type = 'button';
            var n = el('div', 'ov-tile-n', tot[s]);
            var l = el('div', 'label-sys', s + ' open');
            b.appendChild(n);
            b.appendChild(l);
            b.addEventListener('click', function () {
                $('f-state').value = 'open';
                $('f-severity').value = ($('f-severity').value === s) ? '' : s;
                renderTiles();
                loadFindings();
            });
            box.appendChild(b);
        });
    }

    function renderIngest() {
        var node = $('ov-ingest');
        var ing = state.ingest;
        var b = $('ov-banner');
        if (state.machinesError) {
            node.textContent = 'ledger pass: unknown';
            banner(b, 'Could not read your machines — ' + state.machinesError.message, 'error');
            return;
        }
        if (!ing) {
            node.textContent = 'ledger pass: not scheduled';
            banner(b, 'Nothing is writing this ledger: its five-minute pass is not scheduled on this host. ' +
                'Ask your host to run oversight_ingest, and to set it up again as a standing op.', 'error');
            return;
        }
        if (ing.status === 'unknown') {
            node.textContent = 'ledger pass: unknown';
            banner(b, null);
            return;
        }
        var stopped = ing.status !== 'scheduled' && ing.status !== 'running';
        var parts = ['ledger pass ' + rel(ing.last_ran_at)];
        if (!stopped && ing.next_fire_at) {
            // The runner polls every 30 s, so a pass can be a little overdue
            // without anything being wrong; a past time reads "due", never "ago".
            var due = Date.parse(ing.next_fire_at) <= Date.now();
            parts.push(due ? 'next pass due' : 'next ' + rel(ing.next_fire_at));
        }
        if (ing.failures_24h) parts.push(ing.failures_24h + ' failed today');
        node.textContent = parts.join(' · ');
        node.title = ing.last_ran_at ? ('last pass ' + abs(ing.last_ran_at)) : '';
        if (stopped) {
            // The route does not say WHO stopped it, so neither does this.
            banner(b, 'The five-minute pass that writes this ledger is not running (' + ing.status +
                '), so nothing new is being filed. Ask your host to start it again.', 'error');
        } else if (ing.last_ok === false) {
            banner(b, 'The last ledger pass failed' + (ing.last_error ? ': ' + ing.last_error : '') +
                '. What is listed is from the last pass that worked.', 'error');
        } else {
            banner(b, null);
        }
    }

    function renderMachineFilter() {
        var sel = $('f-machine');
        var keep = sel.value;
        var names = state.machines.map(function (m) { return m.machine; })
            .concat(state.other.map(function (m) { return m.machine; }));
        names = names.filter(function (n, i) { return names.indexOf(n) === i; }).sort(function (a, b) {
            return a.toLowerCase() < b.toLowerCase() ? -1 : 1;
        });
        while (sel.options.length > 1) sel.remove(1);
        names.forEach(function (n) {
            var o = document.createElement('option');
            o.value = n;
            o.textContent = n;
            sel.appendChild(o);
        });
        if (keep && names.indexOf(keep) < 0) {
            // A machine named in a deep link or a filter that is no longer listed
            // stays selectable, so the filter never silently widens to everything.
            var o2 = document.createElement('option');
            o2.value = keep;
            o2.textContent = keep;
            sel.appendChild(o2);
        }
        sel.value = keep;
    }

    function renderFindings() {
        var list = $('ov-findings');
        clear(list);
        if (state.findingsError) {
            var e = state.findingsError;
            list.appendChild(emptyState('The ledger could not be read',
                e.status === 403 ? 'This window was not given permission to read the ledger.' : e.message));
            return;
        }
        var src = $('f-source').value;
        var rows = state.findings.filter(function (f) { return !src || f.source === src; });
        if (!rows.length) {
            var st = $('f-state').value;
            var filtered = $('f-severity').value || $('f-machine').value || src;
            list.appendChild(emptyState(
                st === 'resolved' ? 'Nothing resolved yet' : 'Nothing filed',
                (filtered ? 'Nothing matches these filters. ' : '') +
                'The ledger only holds problems, so this does not mean every machine was checked — ' +
                'the Machines tab shows which ones are actually being watched.'));
            return;
        }
        rows.forEach(function (f) { list.appendChild(findingRow(f)); });
    }

    function findingRow(f) {
        var row = el('button', 'cseries-row ov-row' + (f.resolved_at ? ' resolved' : ''));
        row.type = 'button';
        row.dataset.id = f.id;
        var meta = el('div', 'row-meta');
        var head = el('div', 'ov-row-head');
        head.appendChild(pill(f.severity, SEV_PILL[f.severity] || ''));
        head.appendChild(el('span', 'row-title', titleOf(f)));
        meta.appendChild(head);
        var sub = el('div', 'row-sub');
        sub.appendChild(el('span', 'ov-machine', f.machine));
        sub.appendChild(el('span', null, ' · ' + (SOURCE_WORD[f.source] || f.source) + ' · '));
        sub.appendChild(timeEl(f.first_seen, 'first seen '));
        sub.appendChild(el('span', null, ' · '));
        sub.appendChild(timeEl(f.last_seen, 'last seen '));
        meta.appendChild(sub);
        row.appendChild(meta);
        if (f.resolved_at) {
            var act = el('div', 'row-actions');
            var p = pill('resolved', 'allow');
            p.title = abs(f.resolved_at);
            act.appendChild(p);
            row.appendChild(act);
        }
        row.addEventListener('click', function () { openFinding(f.id); });
        return row;
    }

    function emptyState(title, text) {
        var box = el('div', 'cseries-empty');
        box.appendChild(el('span', 'empty-title', title));
        box.appendChild(document.createTextNode(text || ''));
        return box;
    }

    // ── one finding ──────────────────────────────────────────────────────
    function field(label, value, node) {
        var f = el('div', 'detail-field');
        f.appendChild(el('div', 'detail-label', label));
        if (node) f.appendChild(node);
        else f.appendChild(el('div', 'detail-value', value));
        return f;
    }

    function timeField(label, iso) {
        var v = el('div', 'detail-value', iso ? rel(iso) + ' — ' + abs(iso) : 'never');
        return field(label, null, v);
    }

    async function openFinding(id, quiet) {
        state.selected = id;
        var panel = $('ov-detail');
        var body = $('d-body');
        if (!quiet) {
            clear(body);
            $('d-title').textContent = 'Finding ' + id;
            body.appendChild(el('div', 'detail-value', 'Loading…'));
            panel.hidden = false;
        }
        var f;
        try {
            f = await fetchJSON('/api/oversight/findings/' + encodeURIComponent(id));
        } catch (e) {
            if (quiet) return;
            clear(body);
            body.appendChild(el('div', 'status-banner error',
                e.status === 404 ? 'That finding is not in the ledger.' : 'Could not read it — ' + e.message));
            return;
        }
        if (state.selected !== id) return;
        renderDetail(f);
        panel.hidden = false;
    }

    function renderDetail(f) {
        var body = $('d-body');
        clear(body);
        $('d-title').textContent = titleOf(f);

        var tags = el('div', 'ov-row-head');
        tags.appendChild(pill(f.severity, SEV_PILL[f.severity] || ''));
        tags.appendChild(pill(f.resolved_at ? 'resolved' : 'open', f.resolved_at ? 'allow' : ''));
        body.appendChild(tags);

        // Plain text, never the button face: that one is set in small caps, and
        // hostnames are case-sensitive (DESKTOP-AB12CD3, Example-Pool-Dev).
        var m = el('button', 'ov-textlink', f.machine);
        m.type = 'button';
        m.title = 'Show this machine';
        m.addEventListener('click', function () { showMachine(f.machine); });
        body.appendChild(field('Machine', null, m));
        body.appendChild(field('Came from', SOURCE_WORD[f.source] || f.source));
        body.appendChild(timeField('First seen', f.first_seen));
        body.appendChild(timeField('Last seen', f.last_seen));
        if (f.resolved_at) body.appendChild(timeField('Resolved', f.resolved_at));
        if (f.recommendation) body.appendChild(field('What to do', f.recommendation));
        if (f.source === 'alert') {
            body.appendChild(field('The alert',
                'This is an alert fingerprint; the alert\u2019s name and labels stay on ' + f.machine +
                '. `observe:alerts` on that machine lists what each fingerprint names — your host can run it ' +
                'when its grant for the machine names observe:alerts.'));
        }
        if (f.pr_url) {
            // A link only for a web address: an href is the one place in this
            // window where a value could run as code (`javascript:`), so anything
            // that is not http(s) is shown as the text it is.
            if (/^https?:\/\//i.test(String(f.pr_url))) {
                var a = el('a', 'ov-link', f.pr_url);
                a.href = f.pr_url;
                a.target = '_blank';
                a.rel = 'noopener noreferrer';
                body.appendChild(field('Proposed change', null, a));
            } else {
                body.appendChild(field('Proposed change', String(f.pr_url)));
            }
        }
        var ev = f.evidence && typeof f.evidence === 'object' ? Object.assign({}, f.evidence) : f.evidence;
        if (ev && typeof ev === 'object') delete ev.title;
        if (ev && (typeof ev !== 'object' || Object.keys(ev).length)) {
            body.appendChild(field('Evidence', null, el('pre', 'ov-evidence', JSON.stringify(ev, null, 2))));
        }
        body.appendChild(field('Key', f.key));

        if (!f.resolved_at) {
            var actions = el('div', 'detail-actions');
            var btn = el('button', 'cseries-btn primary', 'Mark resolved');
            btn.type = 'button';
            btn.addEventListener('click', function () { askResolve(f); });
            actions.appendChild(btn);
            body.appendChild(actions);
        }
    }

    function closeDetail() {
        state.selected = null;
        $('ov-detail').hidden = true;
    }

    // ── the one verb ─────────────────────────────────────────────────────
    // WHAT HAPPENS AFTER A RESOLVE DEPENDS ON HOW THE ROW IS MADE, and the
    // dialog says which. Three cases, from the fold itself:
    //   heartbeat / alert — re-derived on EVERY pass, so one the machine still
    //     reports reopens within five minutes (runtime/oversight.py: a key
    //     derived again is reopened by merge_findings).
    //   store, kind detect:<x> — read from the machine's JOURNAL, which writes
    //     only when something changes; a line is read once, so the row stays
    //     resolved until the machine records a change to that finding, even if
    //     the problem is still there (runtime/oversight_store.replay).
    //   store, any other kind (store-state, accounting-state, accounting-ca) —
    //     rebuilt on EVERY store read, which is every third pass, so while the
    //     condition holds it reopens within about fifteen minutes
    //     (oversight_store.STORE_POLL_EVERY_N_PASSES).
    function resolveNote(f) {
        var keep = 'It stays in the ledger with its history. ';
        var kind = String(f.kind || '');
        if (f.source === 'heartbeat' || f.source === 'alert') {
            return keep + 'If the machine still reports the problem, the next pass — ' +
                'within five minutes — reopens the same row.';
        }
        if (f.source === 'store' && kind.indexOf('detect:') === 0) {
            return keep + 'This finding came from the machine’s own record, which writes ' +
                'only when something changes — so it stays resolved until the machine ' +
                'records a change to it, even if the problem is still there.';
        }
        if (f.source === 'store') {
            return keep + 'This one is rebuilt every time your host reads the machines’ ' +
                'records, about every fifteen minutes — so while the condition holds, ' +
                'it reopens on the next read.';
        }
        return keep + 'It stays resolved unless the same finding is reported again.';
    }

    function askResolve(f) {
        state.pendingResolve = f.id;
        $('c-message').textContent = resolveNote(f);
        $('ov-confirm').hidden = false;
        $('c-ok').focus();
    }

    async function doResolve() {
        var id = state.pendingResolve;
        state.pendingResolve = null;
        $('ov-confirm').hidden = true;
        if (id == null) return;
        var ok = $('c-ok');
        ok.disabled = true;
        try {
            var r = await fetchJSON('/api/oversight/findings/' + encodeURIComponent(id) + '/resolve',
                { method: 'POST' });
            if (r && r.finding) renderDetail(r.finding);
            await refresh();
        } catch (e) {
            var body = $('d-body');
            body.insertBefore(el('div', 'status-banner error',
                e.status === 403 ? 'This window was not given permission to resolve findings.'
                    : 'Could not mark it resolved — ' + e.message), body.firstChild);
        } finally {
            ok.disabled = false;
        }
    }

    // ── machines view ────────────────────────────────────────────────────
    function postureLine(p) {
        if (!p) return 'posture not reported';
        var bits = [];
        if (typeof p.diskpct === 'number') bits.push('disk ' + p.diskpct + '%');
        if (typeof p.load === 'number') bits.push('load ' + p.load + (typeof p.cpus === 'number' && p.cpus ? ' on ' + p.cpus + ' cores' : ''));
        if (typeof p.sec === 'number') bits.push(p.sec ? p.sec + ' security update' + (p.sec === 1 ? '' : 's') : 'no security backlog');
        if (p.reboot === 'yes') bits.push('reboot pending');
        if (p.f2b) bits.push('fail2ban ' + p.f2b);
        if (p.ufw) bits.push('ufw ' + p.ufw);
        if (p.ssh22) bits.push('ssh ' + p.ssh22);
        return bits.length ? bits.join(' · ') : 'posture reported, nothing readable';
    }

    function alertsLine(m) {
        var a = m.alerts;
        if (!a) return m.alerts_state === 'unconfigured' ? 'nobody is watching this machine\u2019s alerts' : '';
        var bits = [a.firing + ' firing'];
        if (a.critical) bits.push(a.critical + ' critical');
        if (a.warning) bits.push(a.warning + ' warning');
        if (a.at) bits.push('read ' + rel(a.at));
        return bits.join(' · ');
    }

    // Levels are the ledger's own four (runtime/tools._detect_counts). A level
    // is null — not 0 — when no comparison happened, and a null is never
    // printed as a zero: "nothing was counted" and "nothing was found" are
    // different sentences.
    function detectLine(m) {
        var d = m.detect;
        if (!d) return '';
        var counted = false, bits = [];
        SEVERITIES.forEach(function (k) {
            if (typeof d[k] !== 'number') return;
            counted = true;
            if (d[k]) bits.push(d[k] + ' ' + k);
        });
        if (!counted) return 'nothing was counted';
        var line = bits.length ? bits.join(' · ') : 'compared, no differences';
        var unseen = (d.skipped || []).map(function (c) { return DETECT_CLASS_WORDS[c]; })
            .filter(Boolean);
        if (unseen.length) line += ' (' + unseen.join(' and ') + ' not compared)';
        return line;
    }

    function openSummary(open) {
        var box = el('div', 'ov-open');
        var any = false;
        SEVERITIES.forEach(function (s) {
            var n = Number((open || {})[s] || 0);
            if (!n) return;
            any = true;
            box.appendChild(pill(n + ' ' + s, SEV_PILL[s] || ''));
        });
        if (!any) box.appendChild(el('span', 'row-sub', 'nothing open'));
        return box;
    }

    // A plane with no state word (posture) gets no pill: the line says it all.
    function planeRow(label, stateWord, tone, line, reason) {
        var r = el('div', 'ov-plane');
        r.appendChild(el('span', 'label-sys ov-plane-label', label));
        if (stateWord) {
            var p = pill(stateWord, tone || '');
            if (reason) p.title = reason;
            r.appendChild(p);
        }
        if (line) r.appendChild(el('span', 'row-sub', line));
        return r;
    }

    function machineCard(m) {
        var c = el('div', 'cseries-card ov-machine-card' + (state.focusMachine === m.machine ? ' focus' : ''));
        c.dataset.machine = m.machine;
        var head = el('div', 'ov-card-head');
        head.appendChild(el('div', 'card-title', m.machine));
        var online = pill(m.online ? 'online' : 'offline', m.online ? 'allow' : 'deny');
        online.title = m.last_seen_at ? 'last heartbeat ' + abs(m.last_seen_at) : 'never heartbeated';
        head.appendChild(online);
        c.appendChild(head);
        var sub = [];
        if (m.bastion_version) sub.push('bastion ' + m.bastion_version);
        if (m.platform) sub.push(m.platform);
        sub.push('last beat ' + rel(m.last_seen_at));
        c.appendChild(el('div', 'card-sub', sub.join(' · ')));
        if (m.shares_name) {
            // Findings are filed by machine NAME, so two bastions reporting the
            // same hostname share one set of findings and one count.
            c.appendChild(el('div', 'card-sub',
                'Another paired machine reports this same name; the findings and counts below belong to the name, so they are shared.'));
        }

        // AN OFFLINE MACHINE GETS NO GREEN. Its planes keep their last reading
        // (runtime/tools: offline composes, it does not replace), and a green
        // "quiet" or "whole" from a machine nobody can see right now is the
        // all-clear this window is built never to show.
        var tone = function (t) { return (!m.online && t === 'allow') ? '' : t; };
        var detectTone = DETECT_PILL.hasOwnProperty(m.detect_state) ? DETECT_PILL[m.detect_state] : 'warn';
        if (m.detect_state === 'findings' && m.detect && m.detect.critical) detectTone = 'deny';
        c.appendChild(planeRow('Alerts', m.alerts_state || 'not reported', tone(ALERT_PILL[m.alerts_state]),
            alertsLine(m) + (!m.online && m.alerts ? ' — as of its last beat' : ''), m.alerts_reason));
        c.appendChild(planeRow('Detection', m.detect_state || 'not reported', tone(detectTone), detectLine(m), m.detect_reason));
        c.appendChild(m.posture ? planeRow('Posture', null, null, postureLine(m.posture))
                                : planeRow('Posture', 'not reported', '', 'this machine sends no host posture'));
        var recTone = tone(m.record === 'whole' ? 'allow' : (m.record === 'holes' ? 'deny' : ''));
        var recLine = m.record_holes ? Object.keys(m.record_holes).sort().map(function (k) {
            return k + ' (' + m.record_holes[k] + ')';
        }).join(', ') : '';
        c.appendChild(planeRow('Record', m.record, recTone, recLine));

        var foot = el('div', 'ov-card-foot');
        foot.appendChild(openSummary(m.open));
        var b = el('button', 'cseries-btn ghost', 'Show findings');
        b.type = 'button';
        b.addEventListener('click', function () { showFindingsFor(m.machine); });
        foot.appendChild(b);
        c.appendChild(foot);
        return c;
    }

    function renderMachines() {
        var box = $('ov-machines');
        clear(box);
        var b = $('ov-machines-banner');
        if (state.machinesError) {
            banner(b, 'Could not read your machines — ' + state.machinesError.message, 'error');
            return;
        }
        banner(b, null);
        if (!state.machines.length) {
            box.appendChild(emptyState('No machines are paired with this host',
                'Machines appear here once a bastion is installed on them and paired — the Network app ' +
                'has “Set up a bastion”.'));
        }
        state.machines.forEach(function (m) { box.appendChild(machineCard(m)); });

        var other = $('ov-other');
        clear(other);
        // Findings under a name no paired machine carries, grouped by what that
        // name IS (the route's `kind`): the host itself, a machine record that
        // names no machine, or a machine no longer paired. Only the last is a
        // machine that went away; calling the other two that would be false.
        var GROUPS = [
            ['host', 'Findings about this host itself'],
            ['store', 'Findings from a machine record that names no machine'],
            ['unpaired', 'Findings still open for machines no longer paired'],
        ];
        var any = false;
        GROUPS.forEach(function (g) {
            var rows = state.other.filter(function (o) { return (o.kind || 'unpaired') === g[0]; });
            if (!rows.length) return;
            any = true;
            other.appendChild(el('div', 'label-sys', g[1]));
            rows.forEach(function (o) {
                var r = el('div', 'cseries-row');
                var meta = el('div', 'row-meta');
                meta.appendChild(el('div', 'row-title', o.machine));
                meta.appendChild(openSummary(o.open));
                r.appendChild(meta);
                var act = el('div', 'row-actions');
                var btn = el('button', 'cseries-btn ghost', 'Show findings');
                btn.type = 'button';
                btn.addEventListener('click', function () { showFindingsFor(o.machine); });
                act.appendChild(btn);
                r.appendChild(act);
                other.appendChild(r);
            });
        });
        other.hidden = !any;
        // ONCE per request to show a machine — never on the 60-second refresh,
        // which would otherwise yank the view back to it every minute.
        if (state.scrollToFocus && state.focusMachine && state.view === 'machines') {
            var card = box.querySelector('[data-machine="' + cssEscape(state.focusMachine) + '"]');
            if (card) card.scrollIntoView({ block: 'center' });
            state.scrollToFocus = false;
        }
    }

    function cssEscape(s) {
        if (window.CSS && CSS.escape) return CSS.escape(s);
        return String(s).replace(/["\\]/g, '\\$&');
    }

    // ── navigation + deep links ──────────────────────────────────────────
    function showView(view) {
        state.view = view;
        document.querySelectorAll('.cseries-nav-btn').forEach(function (btn) {
            btn.classList.toggle('active', btn.dataset.view === view);
        });
        $('view-findings').hidden = view !== 'findings';
        $('view-machines').hidden = view !== 'machines';
        if (view === 'machines') renderMachines();
    }

    function showFindingsFor(machine) {
        $('f-state').value = 'open';
        renderMachineFilter();
        $('f-machine').value = machine;
        if ($('f-machine').value !== machine) {
            var o = document.createElement('option');
            o.value = machine;
            o.textContent = machine;
            $('f-machine').appendChild(o);
            $('f-machine').value = machine;
        }
        showView('findings');
        loadFindings();
    }

    function showMachine(machine) {
        state.focusMachine = machine;
        state.scrollToFocus = true;
        closeDetail();
        showView('machines');
    }

    // Views this window understands: findings | machines | finding:<id> |
    // machine:<name>. Anything else lands on the findings list.
    function applyView(v) {
        v = String(v || '');
        if (v.indexOf('finding:') === 0) {
            var id = parseInt(v.slice(8), 10);
            showView('findings');
            if (!isNaN(id)) openFinding(id);
            return;
        }
        if (v.indexOf('machine:') === 0) {
            showMachine(v.slice(8));
            return;
        }
        showView(v === 'machines' ? 'machines' : 'findings');
    }

    function viewFromHash() {
        var h = (location.hash || '').replace(/^#/, '');
        var m = /(?:^|&)view=([^&]*)/.exec(h);
        return m ? decodeURIComponent(m[1]) : '';
    }

    // ── boot ─────────────────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('.cseries-nav-btn').forEach(function (btn) {
            btn.addEventListener('click', function () { showView(btn.dataset.view); });
        });
        ['f-state', 'f-severity', 'f-machine'].forEach(function (id) {
            $(id).addEventListener('change', function () { renderTiles(); loadFindings(); });
        });
        $('f-source').addEventListener('change', renderFindings);
        $('f-refresh').addEventListener('click', refresh);
        $('d-close').addEventListener('click', closeDetail);
        $('c-cancel').addEventListener('click', function () {
            state.pendingResolve = null;
            $('ov-confirm').hidden = true;
        });
        $('c-ok').addEventListener('click', doResolve);
        document.addEventListener('keydown', function (e) {
            if (e.key !== 'Escape') return;
            if (!$('ov-confirm').hidden) { state.pendingResolve = null; $('ov-confirm').hidden = true; }
            else if (!$('ov-detail').hidden) closeDetail();
        });
        // A view sent while the window is already open (the shell's open_app
        // relay, app-runner.js) arrives as a message rather than a new hash.
        window.addEventListener('message', function (ev) {
            if (ev.source !== window.parent) return;
            var d = ev.data;
            if (d && d.type === 'cseries:app-view' && typeof d.view === 'string') applyView(d.view);
        });
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) refresh();
        });

        var initial = viewFromHash();
        refresh().then(function () { if (initial) applyView(initial); });
        state.timer = setInterval(function () { if (!document.hidden) refresh(); }, REFRESH_MS);
    });

    window.Oversight = { state: state, refresh: refresh, applyView: applyView, detectLine: detectLine };
})();
