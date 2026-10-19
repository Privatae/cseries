/* ================================================================
 * Terminal — client logic.
 *
 * The SecureCRT shape: machines down the left (the same rows the Network
 * app reads from /api/network/bastions), one tab per open shell, an xterm.js
 * terminal in the middle, a status line under it.
 *
 * One shell = one WebSocket to /api/network/sessions/<server>. A browser
 * socket cannot carry the app token header, so the app first asks for a
 * one-shot ticket over an ordinary (token-stamped) fetch, then opens the
 * socket with `TICKET <t>` as its first frame. Keyboard bytes go up as binary
 * frames; pty bytes come down as binary frames; everything else is a small
 * JSON control message (open / resize / close up; stepup / opened / notice /
 * exit / error down). Closing the tab closes the socket, and the machine
 * kills the shell.
 *
 * There is no ssh client here and nothing to type a host or port into. The
 * only target is a machine that carries the user's bastion.
 * ================================================================ */
(function () {
    'use strict';

    const $ = (id) => document.getElementById(id);
    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    const state = {
        machines: [],
        selected: null,      // hostname
        tabs: [],            // {id, server, op, term, fit, ws, pane, tabEl, sessionId, user, started, status, exit}
        active: null,        // tab id
        seq: 0,
    };

    // ── plain words for what the machine sent back ─────────────
    // `notice` is the box's own E frame, when it sent one. For 126 that is THE
    // reason (a missing grant, but also "no entity key pinned yet — sync a grant
    // first"); the grant wording is only the fallback when the box said nothing.
    function exitWords(code, op, notice) {
        if (code === 126) {
            if (notice) return `The machine refused: ${notice}`;
            return (op === 'session:root')
                ? 'The machine refused a root shell. Usually the approval on the machine was missing or already used, or no second factor is enrolled there.'
                : 'The machine refused this shell. Usually your approval for this session was missing or already used, the machine is quarantined or suspended, or a grant denies shells there. Check the machine in the Network app, then try again.';
        }
        if (code === 124) return 'The machine ended this shell on its own timer (30 minutes idle, or the 8 hour limit).';
        if (code === 1) return 'The machine could not start this shell (see the note above — a shell that is a terminal multiplexer such as tmux, screen or byobu is refused).';
        if (code === 0) return 'The shell ended.';
        if (code > 128) return `The shell was killed (signal ${code - 128}).`;
        return `The shell ended with status ${code}.`;
    }
    function friendlyError(status, body) {
        try { const p = JSON.parse(body); if (p && (p.detail || p.error)) return p.detail || p.error; } catch (e) { /* not json */ }
        if (status === 401 || status === 403) return 'This Host has not granted the Terminal app that permission.';
        if (status === 404) return 'That machine is not one of your bastions.';
        if (status === 503) return 'The Host is still warming up. Try again in a moment.';
        return body ? body.slice(0, 160) : 'Request failed.';
    }
    async function fetchJSON(url, opts) {
        const r = await fetch(url, opts || {});
        if (!r.ok) {
            const body = await r.text().catch(() => '');
            const err = new Error(friendlyError(r.status, body));
            err.status = r.status;
            throw err;
        }
        return r.json();
    }
    function fmtTime(iso) {
        if (!iso) return '';
        const d = new Date(iso); if (isNaN(d.getTime())) return String(iso);
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    // ── the machine rail ───────────────────────────────────────
    function platformLabel(p) {
        const s = String(p || '').toLowerCase();
        if (s.startsWith('win')) return 'Windows';
        if (s.startsWith('darwin') || s.startsWith('mac')) return 'macOS';
        if (s.startsWith('linux')) return 'Linux';
        return p ? String(p) : 'Unknown platform';
    }
    function sessionOps(m) {
        const ops = (m && m.enforcing && Array.isArray(m.enforcing.mgmt_ops)) ? m.enforcing.mgmt_ops : null;
        if (!ops) return null;
        return { open: ops.includes('session:open'), root: ops.includes('session:root') };
    }
    function renderRail() {
        const rail = $('rail');
        if (!state.machines.length) {
            rail.innerHTML = '<div class="term-rail-empty">No machines yet. Pair one in the Network app and it appears here.</div>';
            return;
        }
        const groups = {};
        state.machines.forEach(m => {
            const key = platformLabel(m.platform);
            (groups[key] = groups[key] || []).push(m);
        });
        const parts = [];
        Object.keys(groups).sort().forEach(g => {
            parts.push(`<div class="term-group"><div class="term-group-title">${esc(g)}</div>`);
            groups[g].sort((a, b) => String(a.hostname || a.name).localeCompare(String(b.hostname || b.name))).forEach(m => {
                const name = m.hostname || m.name || `machine ${m.id}`;
                const sub = [];
                sub.push(`<span>${m.online ? 'online' : 'offline'}</span>`);
                // mode and anchor state ride the row when the bastion reports them;
                // a row without them says nothing rather than something invented.
                if (m.mode) sub.push(`<span class="term-pill">${esc(m.mode)}</span>`);
                if (m.anchor_state) sub.push(`<span class="term-pill">${esc(m.anchor_state)}</span>`);
                else if (m.machine_anchor_id) sub.push(`<span class="term-pill">anchored</span>`);
                const so = sessionOps(m);
                if (so && !so.open) sub.push(`<span class="term-pill warn" title="This machine's bastion does not offer terminal sessions yet — update it.">no sessions</span>`);
                if (m.nac_posture && m.nac_posture !== 'ok') sub.push(`<span class="term-pill warn">${esc(m.nac_posture)}</span>`);
                const sel = state.selected === name ? ' selected' : '';
                parts.push(
                    `<button class="term-machine${sel}" data-server="${esc(name)}" role="option" aria-selected="${sel ? 'true' : 'false'}">` +
                    `<span class="term-dot${m.online ? ' online' : ''}" aria-hidden="true"></span>` +
                    `<span><div class="term-machine-name">${esc(name)}</div><div class="term-machine-sub">${sub.join('')}</div></span>` +
                    `</button>`);
            });
            parts.push('</div>');
        });
        rail.innerHTML = parts.join('');
        rail.querySelectorAll('.term-machine').forEach(b => b.addEventListener('click', () => selectMachine(b.dataset.server)));
    }
    function selectMachine(name) {
        state.selected = name;
        renderRail();
        updateButtons();
        const m = state.machines.find(x => (x.hostname || x.name) === name);
        const note = $('welcome-note');
        if (m && !m.online) note.textContent = `${name} is offline right now — a shell needs the machine connected.`;
        else if (m && sessionOps(m) && !sessionOps(m).open) note.textContent = `${name}'s bastion does not offer terminal sessions yet. Update it from the Network app.`;
        else note.textContent = '';
    }
    async function loadMachines() {
        try {
            const d = await fetchJSON('/api/network/bastions');
            state.machines = (d && d.bastions) || [];
            if (state.selected && !state.machines.some(m => (m.hostname || m.name) === state.selected)) state.selected = null;
        } catch (e) {
            state.machines = [];
            $('rail').innerHTML = `<div class="term-rail-empty">${esc(e.message)}</div>`;
            updateButtons();
            return;
        }
        renderRail();
        updateButtons();
    }

    // ── live sessions (every open shell, whichever door opened it) ──
    async function loadLive() {
        const el = $('live');
        let rows;
        try {
            rows = ((await fetchJSON('/api/network/sessions')) || {}).sessions || [];
        } catch (e) {
            el.innerHTML = `<div class="term-rail-empty">${esc(e.message)}</div>`;
            return;
        }
        if (!rows.length) { el.innerHTML = '<div class="term-rail-empty">No open sessions.</div>'; return; }
        el.innerHTML = rows.map(r => {
            const who = r.origin === 'host-tool' ? 'opened by your Host' : 'opened here';
            const root = r.op === 'session:root' ? ' · root' : '';
            return `<div class="term-live" data-id="${esc(r.session_id)}">` +
                `<span><div class="term-live-name">${esc(r.server)}${root}</div>` +
                `<div class="term-live-sub">${who} · started ${esc(fmtTime(r.started_at))}</div></span>` +
                `<button class="btn btn-sm" title="End this session from the Host side">Close</button></div>`;
        }).join('');
        el.querySelectorAll('.term-live button').forEach(b => b.addEventListener('click', async () => {
            const id = b.closest('.term-live').dataset.id;
            b.disabled = true;
            try { await fetchJSON(`/api/network/sessions/${encodeURIComponent(id)}/close`, { method: 'POST' }); }
            catch (e) { b.disabled = false; b.textContent = e.message.slice(0, 40); return; }
            loadLive();
        }));
    }

    // ── tabs ───────────────────────────────────────────────────
    function activeTab() { return state.tabs.find(t => t.id === state.active) || null; }
    function updateButtons() {
        const m = state.machines.find(x => (x.hostname || x.name) === state.selected);
        const canOpen = !!(m && m.online);
        $('btn-open').disabled = !canOpen;
        $('btn-root').disabled = !canOpen;
        const t = activeTab();
        $('btn-close').disabled = !(t && t.ws && t.ws.readyState <= 1);
        $('btn-reconnect').disabled = !t;
    }
    function renderTabs() {
        const bar = $('tabs');
        bar.innerHTML = '';
        state.tabs.forEach(t => {
            const el = document.createElement('button');
            el.className = 'term-tab' + (t.id === state.active ? ' active' : '') + (t.op === 'session:root' ? ' root' : '');
            el.setAttribute('role', 'tab');
            el.innerHTML = `<span class="term-dot${t.status === 'open' ? ' online' : ''}" aria-hidden="true"></span>` +
                `<span class="term-tab-name">${esc(t.server)}</span><span class="term-tab-x" title="Close tab">×</span>`;
            el.addEventListener('click', (ev) => {
                if (ev.target.classList.contains('term-tab-x')) { closeTab(t.id); return; }
                showTab(t.id);
            });
            t.tabEl = el;
            bar.appendChild(el);
        });
        $('welcome').classList.toggle('hidden', state.tabs.length > 0);
    }
    function showTab(id) {
        state.active = id;
        state.tabs.forEach(t => t.pane.classList.toggle('active', t.id === id));
        renderTabs();
        renderStatus();
        updateButtons();
        const t = activeTab();
        if (t) { try { t.fit.fit(); sendResize(t); t.term.focus(); } catch (e) { /* not yet laid out */ } }
    }
    function closeTab(id) {
        const t = state.tabs.find(x => x.id === id);
        if (!t) return;
        endSession(t);
        try { t.term.dispose(); } catch (e) { /* already gone */ }
        t.pane.remove();
        state.tabs = state.tabs.filter(x => x.id !== id);
        if (state.active === id) state.active = state.tabs.length ? state.tabs[state.tabs.length - 1].id : null;
        renderTabs();
        if (state.active) showTab(state.active); else { renderStatus(); updateButtons(); }
    }

    // ── status line ────────────────────────────────────────────
    function renderStatus() {
        const t = activeTab();
        const el = $('status');
        if (!t) { el.innerHTML = '<span>No session.</span>'; return; }
        const bits = [];
        bits.push(`<span>${esc(t.server)}</span>`);
        if (t.status === 'waiting') bits.push(`<span class="waiting">${esc(t.waitingText || 'waiting…')}</span>`);
        if (t.status === 'connecting') bits.push('<span>opening…</span>');
        if (t.sessionId) bits.push(`<span>session <code>${esc(t.sessionId)}</code></span>`);
        if (t.user) bits.push(`<span>user: ${esc(t.user)}</span>`);
        if (t.started) bits.push(`<span>started ${esc(fmtTime(t.started))}</span>`);
        if (t.status === 'open') bits.push('<span>recorded on the machine</span>');
        if (t.status === 'ended') bits.push(`<span class="ended">${esc(t.endText || 'ended')}</span>`);
        el.innerHTML = bits.join('<span class="sep">·</span>');
    }

    // ── a session ──────────────────────────────────────────────
    function makeTerm(pane) {
        const term = new Terminal({
            cursorBlink: true, fontSize: 13, scrollback: 5000, convertEol: false,
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
            theme: { background: '#0b0f14', foreground: '#e5e7eb', cursor: '#7dd3fc' },
        });
        const fit = new FitAddon.FitAddon();
        term.loadAddon(fit);
        term.open(pane);
        return { term, fit };
    }
    function sendResize(t) {
        if (!t.ws || t.ws.readyState !== 1 || t.status !== 'open') return;
        t.ws.send(JSON.stringify({ t: 'resize', cols: t.term.cols, rows: t.term.rows }));
    }
    function say(t, text, cls) {
        // a line from this side, not from the shell — dimmed so it cannot be
        // mistaken for the machine's output
        const colour = cls === 'warn' ? '\x1b[33m' : cls === 'bad' ? '\x1b[31m' : '\x1b[2m';
        t.term.writeln(`${colour}${text}\x1b[0m`);
    }
    async function openSession(server, op, existing) {
        const m = state.machines.find(x => (x.hostname || x.name) === server);
        if (!m) return;
        let t = existing;
        if (!t) {
            const pane = document.createElement('div');
            pane.className = 'term-pane';
            $('panes').appendChild(pane);
            const { term, fit } = makeTerm(pane);
            t = { id: ++state.seq, server, op, term, fit, pane, ws: null, status: 'connecting',
                  sessionId: null, user: null, started: null, exit: null };
            term.onData(data => {
                if (t.ws && t.ws.readyState === 1 && t.status === 'open') t.ws.send(new TextEncoder().encode(data));
            });
            term.onResize(() => sendResize(t));
            state.tabs.push(t);
            renderTabs();
            showTab(t.id);
        } else {
            t.op = op; t.status = 'connecting'; t.sessionId = null; t.user = null; t.started = null; t.exit = null;
            t.term.writeln('');
            renderTabs(); renderStatus();
        }
        t.lastNotice = null;
        say(t, op === 'session:root' ? `Asking ${server} for a root shell…` : `Opening a shell on ${server}…`);

        let ticket;
        try {
            const d = await fetchJSON(`/api/network/sessions/${encodeURIComponent(server)}/ticket`, { method: 'POST' });
            ticket = d.ticket;
        } catch (e) {
            t.status = 'ended'; t.endText = e.message;
            say(t, e.message, 'bad'); renderTabs(); renderStatus(); updateButtons();
            return;
        }
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const ws = new WebSocket(`${proto}//${location.host}/api/network/sessions/${encodeURIComponent(server)}`);
        ws.binaryType = 'arraybuffer';
        t.ws = ws;
        ws.onopen = () => {
            ws.send(`TICKET ${ticket}`);
            try { t.fit.fit(); } catch (e) { /* not laid out */ }
            ws.send(JSON.stringify({ t: 'open', op, cols: t.term.cols, rows: t.term.rows, shell: '' }));
        };
        ws.onmessage = (ev) => {
            if (ev.data instanceof ArrayBuffer) { t.term.write(new Uint8Array(ev.data)); return; }
            let msg;
            try { msg = JSON.parse(ev.data); } catch (e) { return; }
            if (msg.t === 'stepup') {
                t.status = 'waiting';
                t.waitingText = `approve on ${server}`;
                say(t, `Approve this on ${server}: ${msg.message}`, 'warn');
                say(t, 'This tab waits for your approval on the machine. Close it to give up.');
            } else if (msg.t === 'opened') {
                t.status = 'open'; t.sessionId = msg.session_id; t.user = msg.user; t.started = msg.started;
                loadLive();
                say(t, `Connected. ${op === 'session:root' ? 'Root shell' : 'Shell'} on ${server}; this session is recorded on the machine.`);
                sendResize(t);
                t.term.focus();
            } else if (msg.t === 'notice') {
                t.lastNotice = msg.text;
                say(t, `${server}: ${msg.text}`, 'warn');
            } else if (msg.t === 'exit') {
                t.status = 'ended'; t.exit = msg.code;
                t.endText = msg.detail ? msg.detail : exitWords(msg.code, op, t.lastNotice);
                say(t, t.endText, msg.code === 0 ? '' : 'bad');
                say(t, 'Press Reconnect for a fresh shell, or close the tab.');
            } else if (msg.t === 'error') {
                t.status = 'ended'; t.endText = msg.detail || 'could not open the session';
                say(t, t.endText, 'bad');
            }
            renderTabs(); renderStatus(); updateButtons();
        };
        ws.onerror = () => { /* onclose carries the wording */ };
        ws.onclose = () => {
            if (t.status !== 'ended') {
                t.status = 'ended';
                t.endText = t.endText || 'The connection to the Host closed.';
                say(t, t.endText, 'bad');
            }
            t.ws = null;
            renderTabs(); renderStatus(); updateButtons();
        };
    }
    function endSession(t) {
        if (t.ws && t.ws.readyState <= 1) {
            try { t.ws.send(JSON.stringify({ t: 'close' })); } catch (e) { /* closing anyway */ }
            try { t.ws.close(); } catch (e) { /* already closed */ }
        }
        t.ws = null;
        if (t.status !== 'ended') { t.status = 'ended'; t.endText = 'closed'; }
    }

    // ── toolbar ────────────────────────────────────────────────
    $('btn-open').addEventListener('click', () => { if (state.selected) openSession(state.selected, 'session:open'); });
    $('btn-root').addEventListener('click', () => { if (state.selected) openSession(state.selected, 'session:root'); });
    $('btn-reconnect').addEventListener('click', () => {
        const t = activeTab(); if (!t) return;
        endSession(t);
        openSession(t.server, t.op, t);
    });
    $('btn-close').addEventListener('click', () => {
        const t = activeTab(); if (!t) return;
        endSession(t);
        say(t, 'Closed from here. The machine ends the shell.');
        renderTabs(); renderStatus(); updateButtons();
    });
    $('btn-refresh').addEventListener('click', loadMachines);

    window.addEventListener('resize', () => {
        const t = activeTab();
        if (t) { try { t.fit.fit(); } catch (e) { /* not laid out */ } }
    });
    window.addEventListener('beforeunload', () => state.tabs.forEach(endSession));

    loadMachines();
    loadLive();
    setInterval(loadMachines, 30000);
    setInterval(loadLive, 5000);
})();
