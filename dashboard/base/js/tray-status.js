// Taskbar tray — the machine (memory / cpu, whole pod).
//
// THE BALANCE CHIP IS GONE: billing is postpaid, so the taskbar carries no
// balance. Billing is one
// monthly balance reconciled at cycle end; it has its own place (the Compute
// app / the account), not a chip that stares at the user all day. What
// remains is the one number a person can act on in the moment: what the
// machine is using. (History: the wallet chip went 2026-08-26; before that
// the tray replaced the clock + background-agents button, 2026-07-13.)
//
// Polls /api/system/resources every 15s — a shell-trusted read-only route
// (middleware allow-list).

(function () {
    'use strict';

    const RES_MS = 15000;

    function el(id) { return document.getElementById(id); }

    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function fmtBytes(b) {
        if (b == null) return '—';
        if (b >= 1073741824) return (b / 1073741824).toFixed(1) + 'G';
        return Math.round(b / 1048576) + 'M';
    }

    // ── machine: whole-pod memory against the plan ────────────────────

    let _res = null;

    async function pollResources() {
        let j;
        try {
            const r = await fetch('/api/system/resources', { credentials: 'same-origin' });
            if (!r.ok) return;
            j = await r.json();
        } catch {
            return;
        }
        _res = j;
        const chip = el('tray-resources');
        if (!chip) return;
        // The MACHINE is everything the user is running, wherever it runs —
        // this pod's containers, every app pod on its own node, any awake
        // harness. The server does that arithmetic once (`machine`,
        // routes/system.py) so the tray, the App Store strip and the host all
        // quote one number.
        const agg = machineMemory(j);
        const parts = [];
        if (agg.used != null) {
            parts.push(fmtBytes(agg.used) + (agg.limit ? ' of ' + fmtBytes(agg.limit) : ''));
        }
        if (j.cpu && j.cpu.percent != null) parts.push(j.cpu.percent + '%');
        chip.textContent = parts.length ? parts.join(' · ') : '—';
        chip.title = agg.limit
            ? 'Your machine — memory in use, out of the memory limit set for this Host'
              + (agg.partial ? ' (something is not reporting)' : '')
            : 'Your machine — memory in use';
        // Hub sidecar down or policy-tampered is a warn state the user should
        // see without opening the flyout.
        chip.classList.toggle('tray-warn', !!(j.hub && (j.hub.tampered || !j.hub.reachable)));
        const panel = el('tray-resources-panel');
        if (panel && !panel.classList.contains('hidden')) renderResourcesPanel();
    }

    // The one place the machine total comes from. `machine` is server-built
    // and includes the app pods and any awake harness; the fallback below is
    // the old pod-only sum, kept for the window where a shell is newer than
    // its kernel — it is marked partial because a total that cannot see the
    // other pods IS partial.
    //
    // The denominator is the PLAN, never a sum of ceilings. Ceilings are what
    // each piece may burst to and they deliberately add up to more than the
    // plan — a limit reserves nothing, so a host can spike into whatever its
    // node has free. Summing them is what put "4.6G" under a 4G plan here.
    function machineMemory(j) {
        const mach = j.machine;
        if (mach && (mach.used_bytes != null || mach.plan_bytes != null)) {
            return { used: mach.used_bytes, limit: mach.plan_bytes,
                     ceiling: mach.ceiling_bytes,
                     partial: !!mach.partial, units: mach.units || [] };
        }
        let used = null, limit = null;
        function add(u, l) {
            if (u == null) return;
            used = (used || 0) + u;
            if (l != null) limit = (limit || 0) + l;
        }
        const m = j.memory || {};
        add(m.used_bytes, m.limit_bytes);
        const hs = (j.hub && j.hub.reachable && j.hub.self) || null;
        if (hs) add(hs.memory_used_bytes, hs.memory_limit_bytes);
        const d = j.db;
        if (d && d.present && d.memory) add(d.memory.used_bytes, d.memory.limit_bytes);
        return { used, limit, partial: true, units: [] };
    }

    function renderResourcesPanel() {
        const list = el('tray-resources-detail');
        if (!list) return;
        if (!_res) {
            list.innerHTML = '<div class="tray-panel-empty">Loading…</div>';
            return;
        }
        const agg = machineMemory(_res);
        const m = _res.memory || {}, c = _res.cpu || {}, p = _res.processes || {};
        // WHAT IS RUNNING WHERE, one row each. A host is
        // several pods on several machines now — the host pod, one pod per
        // installed app, any awake harness — so the panel names each piece,
        // says where it runs, what it is using, and how far it can spike. An
        // app that is installed but asleep still appears: it holds its
        // reservation, and that is precisely what was invisible.
        const unitRows = (agg.units || []).map(u =>
            '<div class="tray-row tray-unit-row">' +
            `<span class="tray-row-name">${esc(u.label || u.id)}` +
            `<span class="tray-unit-where">${esc(u.where || '')}` +
            `${u.running === false ? ' · asleep' : ''}</span></span>` +
            `<span class="tray-row-num">${u.used_bytes != null ? fmtBytes(u.used_bytes) : '—'}</span>` +
            `<span class="tray-row-num">${u.ceiling_bytes ? '\u2191' + fmtBytes(u.ceiling_bytes) : '—'}</span>` +
            '</div>').join('');
        const machineHead =
            '<div class="tray-res-label label-sys">your machine</div>' +
            '<div class="tray-res-stats">' +
            `<div>memory <b>${fmtBytes(agg.used)}</b>${agg.limit ? ' of ' + fmtBytes(agg.limit) + ' limit' : ''}${agg.partial ? ' (partial)' : ''}</div>` +
            (agg.ceiling ? `<div>can burst to <b>${fmtBytes(agg.ceiling)}</b> when the machine has room</div>` : '') +
            '<div class="tray-res-note">Everything below counts toward the memory total — the host, its database, networking, ' +
            'and any app in its own pod. The arrow is how far each can spike; nothing is held in reserve.</div>' +
            '</div>' + unitRows;
        // Cache is shown as a separate, explicitly reclaimable line. It is not
        // added to `memory` because it is not the container's footprint — a
        // cgroup that has read files parks at its limit forever and that is
        // normal. Naming it here is what stops the next "why am I at 100%".
        const cacheLine = m.cache_bytes
            ? `<div>cache <b>${fmtBytes(m.cache_bytes)}</b> reclaimable</div>` : '';
        const head =
            '<div class="tray-res-label label-sys">host container</div>' +
            '<div class="tray-res-stats">' +
            `<div>memory <b>${fmtBytes(m.used_bytes)}</b>${m.limit_bytes ? ' of ' + fmtBytes(m.limit_bytes) : ''}</div>` +
            cacheLine +
            `<div>cpu <b>${c.percent != null ? c.percent + '%' : '—'}</b> of ${c.cores || '—'} cores</div>` +
            `<div>processes <b>${p.count != null ? p.count : '—'}</b></div>` +
            '</div>' +
            hubSection(_res.hub) +
            dbSection(_res.db);
        const rows = (p.top || []).map(pr =>
            '<div class="tray-row">' +
            `<span class="tray-row-name">${esc(pr.name)}</span>` +
            `<span class="tray-row-num">${pr.pid}</span>` +
            `<span class="tray-row-num">${fmtBytes(pr.rss_bytes)}</span>` +
            '</div>').join('');
        // Per-app attribution — which APP each running process belongs to
        // (server joins the supervisor registry with /proc, and a pod-backed
        // app's own wrapper pushes its cgroup number). Empty is the healthy
        // common case: static apps hold no resident process.
        const apps = _res.apps || [];
        const appRows = apps.length
            ? apps.map(a =>
                '<div class="tray-row">' +
                `<span class="tray-row-name">${esc(a.app_id)}${a.alive ? '' : ' (exited)'}` +
                `<span class="tray-unit-where">${a.placement === 'pod' ? 'its own pod' : 'beside the host'}</span></span>` +
                `<span class="tray-row-num">${a.pid != null ? a.pid : '—'}</span>` +
                `<span class="tray-row-num">${a.rss_bytes != null ? fmtBytes(a.rss_bytes) : '—'}</span>` +
                '</div>').join('')
            : '<div class="tray-res-stats"><div>none — installed apps run static (no resident memory)</div></div>';
        const appHead = apps.length
            ? '<div class="tray-row tray-row-head"><span class="tray-row-name">app</span>' +
              '<span class="tray-row-num">pid</span><span class="tray-row-num">rss</span></div>'
            : '';
        list.innerHTML = machineHead + head +
            '<div class="tray-res-label label-sys">app sidecars</div>' + appHead + appRows +
            '<div class="tray-row tray-row-head"><span class="tray-row-name">process</span>' +
            '<span class="tray-row-num">pid</span><span class="tray-row-num">rss</span></div>' + rows;
    }

    // Hub sidecar block for the resources flyout. Old hub images (< v0.2.4)
    // return /status without `self` — render network state and dashes.
    function hubSection(h) {
        let body;
        if (!h || !h.reachable) {
            body = '<div class="tray-res-stats"><div>status <b>unreachable</b></div></div>';
        } else {
            const s = h.self || {};
            const mem = s.memory_used_bytes != null
                ? fmtBytes(s.memory_used_bytes) + (s.memory_limit_bytes ? '/' + fmtBytes(s.memory_limit_bytes) : '')
                : '—';
            body = '<div class="tray-res-stats">' +
                `<div>status <b>${h.tampered ? 'TAMPERED' : 'up'}</b>${s.version ? ' · ' + esc(s.version) : ''}</div>` +
                `<div>mem <b>${mem}</b></div>` +
                `<div>cpu <b>${s.cpu_percent != null ? s.cpu_percent + '%' : '—'}</b></div>` +
                `<div>peers <b>${h.peers != null ? h.peers : '—'}</b></div>` +
                '</div>';
        }
        return '<div class="tray-res-label label-sys">hub sidecar</div>' + body;
    }

    // Database container block. PostgreSQL is a peer container since the pod
    // split, so the server can only relay what that container publishes about
    // itself — an older cseries-postgres image publishes nothing, which reads as
    // absent here rather than as a silently missing section (otherwise the
    // database simply disappears from the panel).
    function dbSection(d) {
        let body;
        if (!d || !d.present) {
            body = '<div class="tray-res-stats">' +
                `<div>status <b>no stats</b></div>` +
                `<div class="tray-res-note">${esc((d && d.reason) || 'unavailable')}</div>` +
                '</div>';
        } else {
            const m = d.memory || {}, c = d.cpu || {}, p = d.processes || {};
            body = '<div class="tray-res-stats">' +
                `<div>memory <b>${fmtBytes(m.used_bytes)}</b>${m.limit_bytes ? ' of ' + fmtBytes(m.limit_bytes) : ''}</div>` +
                (m.cache_bytes ? `<div>cache <b>${fmtBytes(m.cache_bytes)}</b> reclaimable</div>` : '') +
                `<div>cpu <b>${c.percent != null ? c.percent + '%' : '—'}</b></div>` +
                `<div>processes <b>${p.count != null ? p.count : '—'}</b></div>` +
                '</div>' +
                (p.top || []).map(pr =>
                    '<div class="tray-row">' +
                    `<span class="tray-row-name">${esc(pr.name)}</span>` +
                    `<span class="tray-row-num">${pr.pid}</span>` +
                    `<span class="tray-row-num">${fmtBytes(pr.rss_bytes)}</span>` +
                    '</div>').join('');
        }
        return '<div class="tray-res-label label-sys">database container</div>' + body;
    }

    // ── panel + clicks ────────────────────────────────────────────────

    function closePanels() {
        const p = el('tray-resources-panel');
        if (p) p.classList.add('hidden');
    }

    function togglePanel(id, render) {
        const panel = el(id);
        if (!panel) return;
        const wasHidden = panel.classList.contains('hidden');
        closePanels();
        if (wasHidden) {
            panel.classList.remove('hidden');
            render();
        }
    }

    function init() {
        const res = el('tray-resources');
        if (res) res.addEventListener('click', (e) => {
            e.stopPropagation();
            togglePanel('tray-resources-panel', renderResourcesPanel);
        });
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.tray-panel')) closePanels();
        });

        pollResources();
        setInterval(pollResources, RES_MS);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // The HTML desktop surface (desktop-surface.js) relays a click inside its
    // sandboxed frame as desktop:pointerdown; that click never reaches the
    // document listener above, so the surface closes the panel through this.
    // A design that draws its own bar opens the same panel from its machine
    // control (desktop:machine): the process list stays drawn here, in the
    // shell, and never crosses into the frame.
    window.TrayStatus = {
        closePanels,
        toggleMachine() { togglePanel('tray-resources-panel', renderResourcesPanel); },
    };
})();
