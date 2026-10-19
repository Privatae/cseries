/* ================================================================
 * Network — client logic.
 *
 * Four lenses over the host's sovereign network:
 *   Topology  — live SVG map (host center, bastions + peers around it)
 *   Bastions  — machines the host reaches (operator tokens + heartbeat); also a
 *               READ-ONLY egress status (the user's host-local outbound — visibility only)
 *   Peers     — host↔host contacts, secured channels, reachability posture and
 *               the per-peer rules
 *   (Bastions also lists the other Hosts on each machine, with Request
 *   connection and Message: both moved here from Circles.)
 *
 * Same-origin system app: plain fetch('/api/...'); the window token is
 * injected by the shell. Everything degrades to an honest empty/error
 * state — a missing table or a disconnected bridge never throws uncaught.
 * ================================================================ */
(function () {
    'use strict';

    const state = {
        lens: 'topology',
        overview: null,
        bastions: [],
        // Automatic updates: {enabled, latest, soak_hours, silent_minutes} from
        // GET /bastions/auto-update; null = not read, or the read failed.
        autoUpdate: null,
        // A bastion a link asked for (view 'bastion:<token id>', sent by Circles ›
        // Bastions › Open in Network). Opened by the next bastion load that has
        // the row, then cleared.
        pendingBastion: null,
        // Which bastion load is the newest. Only the newest one may open the
        // pending machine: an older load finishing late would open it and then
        // the newer load's close would take it straight off the screen.
        bastionLoadSeq: 0,
        latestVersion: null,   // latest published bastion version (for "out of date" + update); undefined = unavailable
        // Bastions we have asked to update this session. The box applies the
        // directive on its NEXT check-in, so between the ask and that check-in the
        // card must not keep offering the same button — it would read as "nothing
        // happened". Cleared when the reported version catches up.
        updateAsked: new Set(),
        // The other Hosts on each machine (GET /api/network/bastions/directory):
        // token id → the machine's entry. null = not read yet, or the read failed
        // (directoryError says why). Moved from Circles' Bastions pane.
        directory: null,
        directoryError: null,
        // Connection requests sent from a machine's Host row: host id →
        // {state: 'sending'|'sent'|'error', message}.
        hostAsks: {},
        peers: { contacts: [], secured_channels: [] },
        agents: [],           // observed agent activity (P9 observe plane)
        agentsUnrequested: 0,  // active + unrequested count — the alert signal (header chip + rail badge)
        agentDelegations: [],  // sanction tags
        embedCurrent: null,    // /api/embedding/current → {provider, model, dim, embedding_base_url}
        domain: null,        // /api/domain-policy
        domainError: null,   // why that read failed, said over the rules list
        // ── NAC (host-local network access control: approve-only) ──
        nac: {
            policy: [],            // /nac/policy → policy rows = the INGRESS lane (drafts + approved)
            profiles: [],          // /nac/policy → enforcement profiles (id → {enforcement, permits})
            egress: [],            // /nac/egress → outbound ACL rows (the EGRESS lane)
            grants: [],            // /nac/mgmt/grants → management grants (the MANAGEMENT lane)
            endpoints: [],         // /nac/endpoints → per-bastion posture (read-only)
            suspended: [],         // /nac/endpoints → currently-suspended server hostnames
            audit: [],             // /nac/mgmt/audit → management command accounting
            stepupEnrolled: false, // is a second factor enrolled
            pending: 0,            // draft proposals awaiting approval (badge signal)
            stepupSecret: null,    // otpauth secret, held only during first-time enrollment
            laneOpen: {},          // lane id ('ingress'|'egress'|'mgmt'|'posture') → user's explicit open/closed override
        },
        hostedDoc: null,     // the host's own map document, when it has designed one
        hostedLeft: false,   // its document navigated itself away — stop drawing it
        hostedAt: null,      // its artifact's updated_at — the re-fetch signal
        loaded: {},          // lens -> true once first load done
    };

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
        if (!r.ok) {
            const body = await r.text().catch(() => '');
            const err = new Error(friendlyError(r.status, body));
            err.status = r.status;
            throw err;
        }
        return r.json();
    }
    function friendlyError(status, body) {
        try { const p = JSON.parse(body); if (p && (p.detail || p.error)) return p.detail || p.error; } catch (e) {}
        if (status === 401 || status === 403) return "This Host hasn't granted the Network app that permission.";
        if (status === 404) return 'Not found — it may have been removed.';
        if (status === 503) return 'That service is warming up. Try again in a moment.';
        if (status >= 500) return 'Something went wrong on the Host. Try again shortly.';
        return body ? body.slice(0, 140) : 'Request failed.';
    }

    function fmtRel(iso) {
        if (!iso) return 'never';
        const d = new Date(iso); if (isNaN(d.getTime())) return String(iso);
        const diff = (Date.now() - d.getTime()) / 1000;
        // Past events only. A time slightly ahead of now is clock skew between the Host and
        // this browser, so it reads "just now"; an expiry goes through fmtExpiry instead.
        if (diff < 60) return 'just now';
        if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
        if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
        if (diff < 86400 * 30) return `${Math.floor(diff / 86400)}d ago`;
        return d.toLocaleDateString();
    }
    // How far ahead a future time is: "in 12d". fmtRel reads every future time as "just
    // now", which made every live grant say "expires just now".
    function fmtAhead(d) {
        const ahead = (d.getTime() - Date.now()) / 1000;
        if (ahead < 60) return 'in under a minute';
        if (ahead < 3600) return `in ${Math.floor(ahead / 60)}m`;
        if (ahead < 86400) return `in ${Math.floor(ahead / 3600)}h`;
        if (ahead < 86400 * 30) return `in ${Math.floor(ahead / 86400)}d`;
        return 'on ' + d.toLocaleDateString();
    }
    // An expiry, said the way it stands: "expires in 12d", "expired 3d ago", "no expiry".
    // `expired` is the server's answer when it has one (a management grant's `expired`, the
    // gate's own rule); without it the time decides.
    function fmtExpiry(iso, expired) {
        const d = iso ? new Date(iso) : null;
        const past = !!d && !isNaN(d.getTime()) && d.getTime() <= Date.now();
        const isExpired = (expired === true || expired === false) ? expired : past;
        if (!iso) return isExpired ? 'expired' : 'no expiry';
        if (isExpired) return past ? 'expired ' + fmtRel(iso) : 'expired';
        if (!d || isNaN(d.getTime())) return 'expires ' + String(iso);
        return 'expires ' + fmtAhead(d);
    }
    function skeletonRows(n) {
        let h = '';
        for (let i = 0; i < (n || 3); i++) h += '<div class="skeleton skeleton-card"></div>';
        return h;
    }
    function empty(title, body) {
        return `<div class="cseries-empty"><span class="empty-title">${esc(title)}</span>${esc(body || '')}</div>`;
    }
    function errBox(msg) {
        return `<div class="status-banner error">${esc(msg)}</div>`;
    }

    // ── lens switching ───────────────────────────────────────
    function showLens(lens) {
        state.lens = lens;
        document.querySelectorAll('.net-rail-btn').forEach(b => {
            const on = b.dataset.lens === lens;
            b.classList.toggle('active', on);
            b.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        document.querySelectorAll('.net-lens').forEach(s => { s.hidden = s.id !== `lens-${lens}`; });
        closeInspector();
        if (lens === 'topology') renderTopology();
        if (lens === 'bastions') loadBastions();
        if (lens === 'nac') loadNAC();
        if (lens === 'agents') loadAgents();
        if (lens === 'freeagents') loadFreeAgents();
        if (lens === 'servers') loadServers();
        if (lens === 'peers') loadPeers();
    }

    // ── header health ────────────────────────────────────────
    function renderHealth() {
        const el = $('net-health');
        const o = state.overview;
        if (!o) { el.innerHTML = '<span class="net-health-chip skeleton" style="width:120px;height:18px"></span>'; return; }
        const bridge = o.bridge || {};
        const bOk = !!bridge.connected;
        const gw = (o.counts && o.counts.bastions_online) || 0;
        const gwTotal = (o.counts && o.counts.bastions) || 0;
        const ua = (o.counts && o.counts.unrequested_agents) || 0;
        state.agentsUnrequested = ua;
        el.innerHTML = `
            <span class="net-health-chip" title="The bridge signs the doors into your network, then steps out — it never carries your traffic.">
                <span class="net-status-dot ${bOk ? 'live' : ''}"></span>
                <span class="label-sys">control plane</span> <b>${bOk ? 'reachable' : 'unreachable'}</b>
            </span>
            <span class="net-health-chip" title="Bastions online / total">
                <span class="net-status-dot ${gw ? 'live' : ''}"></span>
                <span class="label-sys">bastions</span> <b>${gw}/${gwTotal}</b>
            </span>
            ${ua > 0 ? `<span class="net-health-chip" data-go-agents title="Agent activity you didn't request — open Agents" style="cursor:pointer">
                <span class="net-status-dot" style="background:#c0492f"></span>
                <span class="label-sys">agents</span> <b style="color:#c0492f">${ua} unrequested</b>
            </span>` : ''}`;
        updateAgentsBadge();
        const id = o.identity || {};
        $('brand-sub').textContent = id.handle ? id.handle : (id.entity_id ? `Host #${id.entity_id}` : 'your private network');
        $('foot-right').textContent = 'updated ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    // ── overview (drives header + topology + stats) ──────────
    async function loadOverview(opts) {
        try {
            state.overview = await fetchJSON('/api/network/overview');
        } catch (e) {
            state.overview = null;
            $('net-health').innerHTML = `<span class="net-health-chip"><span class="net-status-dot"></span>${esc(e.message)}</span>`;
        }
        // Keep the embed lane state fresh regardless of lens, so a bastion inspector
        // opened from the Map shows the live "serving your embeddings" status too.
        try { state.embedCurrent = await fetchJSON('/api/embedding/current'); }
        catch (e) { /* leave prior value; embed badges degrade to "available" */ }
        renderHealth();
        if (state.lens === 'topology') renderTopology(opts);
    }

    // ════════════════════ TOPOLOGY ════════════════════
    async function renderTopology() {
        renderTopoStats();
        // The host's own map, if it has designed one. Fetched once per load and
        // re-fetched when its artifact changes, so the host iterating in the
        // Artifacts window updates this while the user watches — the appearance
        // row holds a POINTER, never a copy. A 404 is the normal answer.
        try {
            const meta = await fetchJSON('/api/appearance/network/meta');
            if (!state.hostedDoc || state.hostedAt !== meta.updated_at) {
                const r = await fetch('/api/appearance/network', { credentials: 'same-origin' });
                if (r.ok) { state.hostedDoc = await r.text(); state.hostedAt = meta.updated_at; }
            }
        } catch (e) {
            // No host-authored map (the usual case). Drop any stale copy so the
            // view disappears rather than showing a map that was un-set.
            state.hostedDoc = null; state.hostedAt = null;
        }
        // pull nodes (bastions + secured peers + active host contacts)
        try {
            if (!state.loaded.topology) {
                const [g, p] = await Promise.all([
                    fetchJSON('/api/network/bastions').then(r => r.bastions || []),
                    fetchJSON('/api/network/peers').catch(() => ({ contacts: [], secured_channels: [] })),
                ]);
                state.bastions = g;
                state.peers = p || { contacts: [], secured_channels: [] };
                state.loaded.topology = true;
            }
        } catch (e) {
            $('topo-scene').innerHTML = errBox(e.message);
            return;
        }
        drawScene();
        drawTopoTable();
    }

    function renderTopoStats() {
        const c = (state.overview && state.overview.counts) || {};
        const o = state.overview || {};
        const peer = o.peer || {};
        const cards = [
            { v: c.bastions_online != null ? `${c.bastions_online}/${c.bastions || 0}` : '—', l: 'bastions live' },
            { v: c.secured_channels != null ? c.secured_channels : '—', l: 'secured peers' },
            { v: c.contacts != null ? c.contacts : '—', l: 'contacts' },
            { v: (o.bridge && o.bridge.connected) ? 'reachable' : 'unreachable', l: 'control plane' },
            { v: peer.trust_ready ? 'ready' : 'syncing', l: 'trust' },
        ];
        $('topo-stats').innerHTML = cards.map(s =>
            `<div class="net-stat"><span class="net-stat-val ${s.cls || ''}">${esc(s.v)}</span><span class="net-stat-lbl label-sys">${esc(s.l)}</span></div>`
        ).join('');
    }

    // ══════════════════════ THE GRAPH ══════════════════════
    //
    // One view-model, several renderings.
    //
    // buildGraph() turns loaded state into a plain {host, nodes, edges} object.
    // A renderer turns that graph into markup and does nothing else — it never
    // fetches, never reads `state`, never touches the DOM outside the box it is
    // handed. Renderers register themselves in VIEWS by name, so a rendering is
    // a swappable artifact rather than a function buried in this file.
    //
    // That separation is also the seam a HOST-AUTHORED map renders through: the
    // same graph is what crosses the sandbox bridge to a host's own HTML. One
    // graph, two consumers — a built-in renderer eats it in-process, a
    // host-written document eats it over the bridge.

    // Build the view-model shown on the map.
    function buildGraph() {
        const id = (state.overview && state.overview.identity) || {};
        const host = {
            id: 'host',
            label: id.handle || (id.entity_id ? `Host #${id.entity_id}` : 'Your Host'),
            handle: id.handle || null,
            entity_id: id.entity_id != null ? id.entity_id : null,
        };
        const nodes = [], edges = [];

        (state.bastions || []).forEach(g => {
            // Enrollment facts are present before the daemon's first heartbeat
            // (topology._bastion_row), so the physical path draws immediately.
            const enr = g.enrollment || {};
            const status = g.online ? 'live' : (g.last_seen_at ? 'stale' : 'down');
            nodes.push({
                kind: 'bastion',
                id: g.id,
                label: g.hostname || g.name || `bastion ${g.id}`,
                status,
                // The physical facts. Every one of these already rides the
                // heartbeat or the enrollment row — nothing new is collected.
                machine: {
                    hostname: g.hostname || g.name || null,
                    platform: g.platform || null,
                    // The bastion's own tunnel address, and the ONLY host
                    // address it may reach. Not a LAN address — the daemon
                    // reports its tunnel IP, so we never label it as one.
                    tunnel_ip: enr.wg_bastion_ip || g.reported_ip || null,
                    host_tunnel_ip: enr.wg_host_ip || null,
                    endpoint: enr.wg_endpoint || null,
                    tunnel_status: g.tunnel_status || null,
                    capabilities: g.capabilities || [],
                    models: g.models || [],
                    last_seen_at: g.last_seen_at || null,
                },
                ref: g,
            });
            edges.push({ from: g.id, to: 'host', kind: 'tunnel', status });

            // The other Hosts this machine serves, as the machine itself
            // reports them. This is what the map is for: another Host appears
            // BECAUSE you share a machine with it, so it hangs off that
            // machine — not off your Host, which it has no link to.
            (g.peers || []).forEach(p => {
                if (!p || !p.host_name) return;
                const pid = 'mpeer-' + g.id + '-' + p.host_name;
                nodes.push({
                    kind: 'machine-peer',
                    id: pid,
                    label: p.handle || p.host_name,
                    status: 'peer',
                    machine: null,
                    via: g.hostname || g.name || `bastion ${g.id}`,
                    parent: g.id,
                    ref: p,
                });
                edges.push({ from: pid, to: g.id, kind: 'shares-machine', status: 'peer' });
            });
        });

        (state.peers.secured_channels || []).forEach(s => {
            nodes.push({
                kind: 'peer',
                id: 'peer-' + s.peer_entity_id,
                label: `Host ${s.peer_entity_id}`,
                status: 'secured',
                machine: null,          // a peer is a host, not a machine of ours
                ref: s,
            });
            edges.push({ from: 'peer-' + s.peer_entity_id, to: 'host', kind: 'peering', status: 'secured' });
        });

        (state.peers.contacts || []).filter(c => c.status === 'active' && c.kind === 'host').slice(0, 8).forEach(c => {
            nodes.push({
                kind: 'contact',
                id: 'contact-' + c.id,
                label: c.display_name || c.handle,
                status: 'idle',
                machine: null,
                ref: c,
            });
            edges.push({ from: 'contact-' + c.id, to: 'host', kind: 'contact', status: 'idle' });
        });

        const capped = nodes.slice(0, 16);
        const keep = new Set(capped.map(n => String(n.id)));
        return { host, nodes: capped, edges: edges.filter(e => keep.has(String(e.from))) };
    }

    // ── the renderer registry ────────────────────────────────
    // registerView(name, {label, sub, render(graph) -> markup string})
    const VIEWS = {};
    const VIEW_ORDER = [];
    function registerView(name, def) {
        if (!VIEWS[name]) VIEW_ORDER.push(name);
        VIEWS[name] = def;
    }
    const VIEW_PREF_KEY = 'net.mapview';
    // In-memory first, storage second. A browser that refuses localStorage
    // (private mode) still gets a working picker — it just forgets the choice
    // when the window closes, rather than ignoring the click.
    let viewChoice = null;
    function currentViewName() {
        const usable = (n) => VIEWS[n] && (n !== 'hosted' || !!state.hostedDoc);
        if (viewChoice && usable(viewChoice)) return viewChoice;
        let want = null;
        try { want = localStorage.getItem(VIEW_PREF_KEY); } catch (e) { /* no storage */ }
        // A remembered 'hosted' falls back cleanly when the host later clears
        // its map, rather than leaving the user on a view that cannot draw.
        return usable(want) ? want : VIEW_ORDER[0];
    }
    function setViewName(name) {
        if (!VIEWS[name]) return;
        viewChoice = name;
        try { localStorage.setItem(VIEW_PREF_KEY, name); } catch (e) { /* session only */ }
    }

    function nodeGlyph(kind, x, y) {
        if (kind === 'bastion') return `<rect class="topo-node-icon" x="${x - 6}" y="${y - 5}" width="12" height="4" rx="1"></rect><rect class="topo-node-icon" x="${x - 6}" y="${y + 1}" width="12" height="4" rx="1"></rect>`;
        if (kind === 'peer') return `<circle class="topo-node-icon" cx="${x}" cy="${y - 2}" r="3"></circle><path class="topo-node-icon" d="M${x - 5} ${y + 6} q5 -6 10 0 z"></path>`;
        return `<circle class="topo-node-icon" cx="${x}" cy="${y}" r="3.5"></circle>`;
    }

    // ── view: logical (the original wheel, unchanged) ────────
    // The sub-line used to say "Direct connections — the platform isn't in
    // this picture". Every machine's tunnel runs through the platform relay
    // (the only bastion data plane, bridge adapter.py
    // _relay_endpoints), so it now says what this view is: who belongs to
    // whom, with the path left to the Physical view.
    registerView('logical', {
        label: 'Logical',
        sub: "Your Host at the center, the bastions and peers in your mesh around it. Your machines reach it through the relay their tunnels dial — the Physical view draws that path. Live, refreshed automatically.",
        render(graph) {
            // A machine-peer sits on an OUTER ring at its machine's angle, and
            // its line runs to that machine rather than to the Host. That is the
            // fact: you can see this Host because you share a box with it — it
            // has no link to your Host at all, and drawing one would say it does.
            const ring = graph.nodes.filter(n => n.kind !== 'machine-peer');
            const shared = graph.nodes.filter(n => n.kind === 'machine-peer');
            const nodes = ring.length ? ring : graph.nodes;
            const W = 720, H = 420, cx = W / 2, cy = H / 2 - 6;
            const hostLabel = graph.host.label;
            // The inner ring has to leave room for the outer one, or a peer at
            // the top of the circle is drawn off the canvas. Derived from the
            // frame rather than tuned by eye: outer radius + label must fit
            // inside the shorter half-axis.
            const OUTER = 74, LABEL = 34;
            const room = Math.min(cx, cy) - LABEL - (shared.length ? OUTER : 0);
            const R = Math.max(72, Math.min(nodes.length > 8 ? 150 : 134, room));
            const pos = {};
            let edges = '', glyphs = '';
            nodes.forEach((n, i) => {
                const ang = (-90 + i * (360 / nodes.length)) * Math.PI / 180;
                const x = cx + R * Math.cos(ang), y = cy + R * Math.sin(ang);
                pos[String(n.id)] = { x, y, ang };
                const liveEdge = n.status === 'live';
                const downEdge = n.status === 'down';
                edges += `<line class="topo-edge ${liveEdge ? 'live' : ''} ${downEdge ? 'down' : ''}" x1="${cx}" y1="${cy}" x2="${x}" y2="${y}"></line>`;
                const ringCls = n.status === 'live' ? 'live' : n.status === 'down' ? 'down' : '';
                const pulse = (n.status === 'live')
                    ? `<circle class="topo-pulse" cx="${x}" cy="${y}" r="4"></circle>`
                    : '';
                const glyph = nodeGlyph(n.kind, x, y);
                const lbl = n.label.length > 16 ? n.label.slice(0, 15) + '…' : n.label;
                glyphs += `
                <g class="topo-node-g ${n.status === 'down' ? 'is-down' : ''}" data-node="${attr(n.id)}" role="button"
                   tabindex="0" aria-label="${attr(n.kind + ': ' + n.label + ' — ' + n.status)}">
                    <circle class="topo-node-ring ${ringCls}" cx="${x}" cy="${y}" r="18"></circle>
                    ${glyph}${pulse}
                    <text class="topo-node-label" x="${x}" y="${y + 34}">${esc(lbl)}</text>
                </g>`;
            });

            // The Hosts that share one of your machines. Grouped per machine so
            // several fan out from the same box instead of stacking.
            const byParent = {};
            shared.forEach(n => {
                (byParent[String(n.parent)] = byParent[String(n.parent)] || []).push(n);
            });
            Object.keys(byParent).forEach(pid => {
                const anchor = pos[pid];
                if (!anchor) return;
                const group = byParent[pid];
                group.forEach((n, k) => {
                    // Fan out around the machine's own angle so two peers on one
                    // box are both readable.
                    const spread = (k - (group.length - 1) / 2) * 0.34;
                    const a = anchor.ang + spread;
                    const px = cx + (R + OUTER) * Math.cos(a), py = cy + (R + OUTER) * Math.sin(a);
                    edges += `<line class="topo-edge shared" x1="${anchor.x}" y1="${anchor.y}" x2="${px}" y2="${py}"></line>`;
                    glyphs += `
                <g class="topo-node-g" data-node="${attr(n.id)}" role="button" tabindex="0"
                   aria-label="${attr('Host ' + n.label + ' — shares your machine ' + n.via)}">
                    <circle class="topo-node-ring peer" cx="${px}" cy="${py}" r="15"></circle>
                    ${nodeGlyph('peer', px, py)}
                    <text class="topo-node-label" x="${px}" y="${py + 29}">${esc(trunc(n.label, 14))}</text>
                </g>`;
                });
            });

            const hub = `
            <g aria-label="${attr(hostLabel + ' (this Host)')}">
                <circle class="topo-node-ring hub" cx="${cx}" cy="${cy}" r="26"></circle>
                <circle cx="${cx}" cy="${cy}" r="9" style="fill:var(--cseries-ink)"></circle>
                <text class="topo-node-label" x="${cx}" y="${cy + 44}" style="fill:var(--cseries-paper);font-weight:600">${esc(hostLabel)}</text>
            </g>`;
            return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Network topology map">
                ${edges}${hub}${glyphs}
            </svg>`;
        },
    });

    // ── view: physical (the path a person pictures) ──────────
    //
    // The logical view says "everything belongs to my Host." True, and not what
    // someone means when they ask where their data goes. This draws the line
    // instead: your machine → the relay it dials → your Host. Every value on it
    // already rides the heartbeat or the enrollment row.
    registerView('physical', {
        label: 'Physical',
        sub: 'The path itself, left to right: your machine, the relay its tunnel dials, and your Host. A segment is drawn broken where that hop is actually down.',
        render(graph) {
            const machines = graph.nodes.filter(n => n.kind === 'bastion');
            const sharedWith = graph.nodes.filter(n => n.kind === 'machine-peer');
            const others = graph.nodes.filter(
                n => n.kind !== 'bastion' && n.kind !== 'machine-peer');
            const W = 720, H = 420;
            const COL_M = 118, COL_R = 366, COL_H = 622;   // machine / relay / host
            const HOP_W = 176;                              // wide enough for a real endpoint
            // The band the rows live in, between the column headers and the
            // footer note. The block is centred in it rather than pinned to the
            // top, so three machines don't sit in a corner of an empty frame.
            const BAND_TOP = 56, BAND_BOT = H - 40;
            // 78 leaves the platform sub-label clear of the next machine's
            // glyph; at 66 they nearly touched. Above 4 machines the band is
            // shared out instead, down to a floor that still fits a label.
            const rowH = machines.length > 4
                ? Math.max(52, (BAND_BOT - BAND_TOP) / machines.length)
                : 78;
            const bodyH = Math.max(rowH * machines.length, rowH);
            const top = BAND_TOP + Math.max(0, (BAND_BOT - BAND_TOP - bodyH) / 2);
            // Every path lands on the ONE host, so every path must actually
            // reach it: the second segment converges here rather than stopping
            // at a fixed x, which left a line pointing at nothing.
            const hostCy = top + bodyH / 2;
            const HOST_R = 24;

            if (!machines.length) {
                return `<div style="padding:48px">${empty(
                    'No machines in your network yet',
                    'The physical path is drawn from your machines. Stand one up in Bastions and its path appears here — machine, relay, Host.')}</div>`;
            }

            const head = (x, t) =>
                `<text class="topo-col-head label-sys" x="${x}" y="34" text-anchor="middle">${esc(t)}</text>`;

            // Two layers, the way the logical view does it: the PATH (segments
            // and the relay hop) underneath, the clickable MACHINE on top. They
            // were one group at first, which made each node's box span the whole
            // width of the frame — a click target that is mostly empty space,
            // and a hit area that lies about what it selects. An edge is not
            // part of a node.
            let paths = '', rows = '';
            machines.forEach((n, i) => {
                const y = top + i * rowH + rowH / 2 - 8;
                const m = n.machine || {};
                const live = n.status === 'live';
                const down = n.status === 'down';
                const segCls = `topo-edge ${live ? 'live' : ''} ${down ? 'down' : ''}`;
                // machine → relay → host. The relay hop and the host hop share
                // the pairing's status: if the tunnel is down, both are down.
                paths += `
                <g class="topo-path ${down ? 'is-down' : ''}" aria-hidden="true">
                    <line class="${segCls}" x1="${COL_M + 66}" y1="${y}" x2="${COL_R - HOP_W / 2}" y2="${y}"></line>
                    <line class="${segCls}" x1="${COL_R + HOP_W / 2}" y1="${y}" x2="${COL_H - HOST_R - 2}" y2="${hostCy}"></line>
                    <text class="topo-seg-label" x="${(COL_M + COL_R) / 2}" y="${y - 8}">${esc(m.tunnel_ip || 'tunnel')}</text>
                    <rect class="topo-hop" x="${COL_R - HOP_W / 2}" y="${y - 18}" width="${HOP_W}" height="36" rx="6"></rect>
                    ${hopLabel(m.endpoint, COL_R, y)}
                </g>`;
                rows += `
                <g class="topo-node-g ${down ? 'is-down' : ''}" data-node="${attr(n.id)}" role="button" tabindex="0"
                   aria-label="${attr(n.label + ' — ' + n.status
                       + (m.platform ? ', ' + m.platform : '')
                       + (m.endpoint ? ', via ' + m.endpoint : '')
                       + ' — to ' + graph.host.label)}">
                    <circle class="topo-node-ring ${live ? 'live' : down ? 'down' : ''}" cx="${COL_M}" cy="${y}" r="17"></circle>
                    ${nodeGlyph('bastion', COL_M, y)}
                    ${live ? `<circle class="topo-pulse" cx="${COL_M}" cy="${y}" r="4"></circle>` : ''}
                    <text class="topo-node-label" x="${COL_M}" y="${y + 32}">${esc(trunc(n.label, 18))}</text>
                    ${m.platform ? `<text class="topo-node-sub" x="${COL_M}" y="${y + 45}">${esc(m.platform)}</text>` : ''}
                </g>`;
                // The Hosts sharing THIS machine, on its own row — the physical
                // truth is that they sit on the same box, so they belong beside
                // it rather than somewhere else on the picture.
                const mine = sharedWith.filter(p => String(p.parent) === String(n.id));
                if (mine.length) {
                    const names = mine.map(p => p.label).join(', ');
                    rows += `
                <g class="topo-node-g" data-node="${attr(mine[0].id)}" role="button" tabindex="0"
                   aria-label="${attr('also serves ' + names)}">
                    <text class="topo-shared" x="${COL_M + 44}" y="${y - 20}">also serves ${esc(trunc(names, 30))}</text>
                </g>`;
                }
            });

            // One Host, drawn once, on the right — every path lands here.
            const hostBox = `
            <g aria-label="${attr(graph.host.label + ' (this Host)')}">
                <circle class="topo-node-ring hub" cx="${COL_H}" cy="${hostCy}" r="${HOST_R}"></circle>
                <circle cx="${COL_H}" cy="${hostCy}" r="8" style="fill:var(--cseries-ink)"></circle>
                <text class="topo-node-label" x="${COL_H}" y="${hostCy + 40}"
                      style="fill:var(--cseries-paper);font-weight:600">${esc(trunc(graph.host.label, 18))}</text>
            </g>`;

            // Peers are hosts, not machines of ours — they have no physical path
            // through our relay, and silently dropping them would be a lie about
            // what is in the network. Named, not drawn.
            const peerNote = others.length
                ? `<text class="topo-foot label-sys" x="${W / 2}" y="${H - 14}" text-anchor="middle">${esc(
                    others.length + (others.length === 1 ? ' peer host' : ' peer hosts')
                    + ' — reached host to host, not through your relay. See the Logical view or Peers.')}</text>`
                : '';

            return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Physical network path">
                ${head(COL_M, 'your machine')}${head(COL_R, 'the relay it dials')}${head(COL_H, 'your Host')}
                ${paths}${hostBox}${rows}${peerNote}
            </svg>`;
        },
    });

    function trunc(s, n) {
        s = String(s == null ? '' : s);
        return s.length > n ? s.slice(0, n - 1) + '…' : s;
    }

    // ── view: the host's own map ─────────────────────────────
    //
    // The third door. The host writes the map as an ordinary artifact and
    // points the app at it (PUT /api/appearance/network); this renders that
    // document and feeds it the SAME graph the built-in renderers get. That is
    // why the renderer split came first: one graph, three consumers.
    //
    // SECURITY INVARIANT, inherited from the Artifacts window and the HTML
    // desktop: host-authored HTML never renders in a privileged context. The
    // frame is srcdoc with sandbox="allow-scripts" and nothing else — opaque
    // origin, no session cookie, no token, no API reach, no top navigation, no
    // forms. It cannot fetch its own data, which is the point: everything it
    // knows arrives over the bridge, so a map that shows a machine is showing a
    // machine you actually have.
    //
    // THE DOOR is a private MessageChannel, not window.postMessage. The bridge
    // script runs before any document script exists, keeps port1 in its
    // closure, and hands port2 out in a single window-level hello. Only the
    // FIRST hello per frame is bound; anything else from the frame is dropped.
    //
    //   frame → app (window, once): cseries:network:hello [port]
    //   frame → app (port):         cseries:network:select {node_id}
    //   app   → frame (port):       cseries:network:graph {host, nodes, edges}
    //
    // That is the complete list. Outbound carries the graph the user is already
    // looking at and nothing else — no tokens, no bearer, no identity beyond
    // the host handle already on screen. Inbound, a node id is only acted on
    // when it names a node in the graph we just sent.
    // The frame's CSP, injected as the FIRST thing in the document — ahead of
    // the bridge, so it is in force before any of the document's own markup is
    // parsed. A <meta http-equiv> is the only CSP that reaches a srcdoc frame:
    // the response headers on GET /api/appearance/network cover the case where
    // someone navigates to that URL directly, but srcdoc content is inline and
    // inherits none of them, and the iframe `csp=` attribute is a Chrome-only
    // proposal that does not apply here.
    //
    // WHY IT IS NEEDED AT ALL: sandbox="allow-scripts" gives the frame an opaque
    // origin, but it does NOT take away the network. fetch(), new Image().src
    // and a form POST all still egress from a sandboxed frame. The floor check
    // (desktop_contract.check_network_document) refuses literal http(s)
    // src/href/url()/@import and its message says "the map frame has no
    // network" — a regex over literals cannot make that sentence true, because
    // building the URL at runtime walks straight past it. This is what makes it
    // true, enforced by the browser rather than asked for at write time. A map
    // of the user's own network is the one artifact where a quiet call home
    // does the most damage.
    //
    // data: images/fonts and inline styles stay allowed because that is exactly
    // what the floor tells authors to use ("inline every asset — system fonts,
    // CSS gradients, SVG, canvas").
    //
    // WHAT IT DOES NOT CLOSE, stated plainly because an earlier version of this
    // comment claimed otherwise: a document may always navigate ITSELF.
    // `location.href = ...`, <meta http-equiv="refresh"> and a clicked <a href>
    // all still leave, carrying whatever the author puts in the URL. The sandbox
    // flag only forbids navigating OTHER browsing contexts, and CSP has no
    // shipped directive for navigations (navigate-to was dropped; form-action
    // covers forms only, which is why the form lane IS closed). Measured in
    // Chromium against this exact CSP: fetch, Image.src, sendBeacon, WebSocket,
    // CSS url(), <iframe src>, <link prefetch>, window.open and nested srcdoc
    // are all blocked; self-navigation is not.
    //
    // So the payload is narrowed to what a picture needs (see hostedPush) and
    // the departure is made LOUD rather than silent — hostedGone below. This is
    // detection, not prevention: by the time it fires the request has left.
    // The shared policy every host-authored surface is mounted under
    // (dashboard/base/js/sandbox-csp.js, loaded by index.html). It used to be
    // declared here; a second copy of a security rule beside the desktop's and
    // the artifacts window's is three chances to drift, and the drift is
    // silent — a frame with a weaker policy looks identical on screen.
    //
    // The residual it does NOT close is still the important part: a document
    // may always navigate ITSELF, so the payload below is narrowed and the
    // departure is reported. See hostedPush and cseries:network:leaving.
    // Read at RENDER time, not module load, and never defaulted to ''. An
    // empty policy is indistinguishable from a working one on screen, and the
    // live pod was measured serving /js/qrcode.js 200 while
    // /js/sandbox-csp.js was still 404 — app bundles and the kernel image roll
    // independently, so that window is real. The map refuses to mount rather
    // than mount unsealed.
    function hostedCsp() {
        return window.CSeriesSandbox ? window.CSeriesSandbox.META : null;
    }
    const HOSTED_BRIDGE = `
<script>(function(){
  var ch = new MessageChannel(), state = {host:null, nodes:[], edges:[]}, subs = [];
  ch.port1.onmessage = function(e){
    var d = e.data || {};
    if (d.type !== 'cseries:network:graph') return;
    state = {host: d.host || null, nodes: d.nodes || [], edges: d.edges || []};
    fill();
    subs.forEach(function(f){ try { f(state); } catch (err) {} });
  };
  function fill(){
    var slots = document.querySelectorAll('[data-cseries-nodes]');
    for (var i = 0; i < slots.length; i++) {
      var el = slots[i];
      if (el.getAttribute('data-cseries-nodes-filled')) continue;
      el.setAttribute('data-cseries-nodes-filled', '1');
      el.textContent = '';
      state.nodes.forEach(function(n){
        var d = document.createElement('div');
        d.className = 'cseries-node';
        d.setAttribute('data-cseries-node', n.id);
        d.textContent = n.label + ' — ' + n.status;
        d.addEventListener('click', function(){ select(n.id); });
        el.appendChild(d);
      });
    }
  }
  function select(id){
    if (!id) return;
    try { ch.port1.postMessage({type:'cseries:network:select', node_id: String(id)}); } catch (e) {}
  }
  window.CSeriesNetwork = {
    get host(){ return state.host; },
    get nodes(){ return state.nodes.slice(); },
    get edges(){ return state.edges.slice(); },
    select: select,
    on: function(f){ if (typeof f === 'function') { subs.push(f); try { f(state); } catch (e) {} } }
  };
  // A document that navigates itself has already sent its request by the time
  // this fires — this cannot prevent the leak, only refuse to pretend it did
  // not happen. The parent burns the map and tells the user.
  window.addEventListener('pagehide', function(){
    try { ch.port1.postMessage({type:'cseries:network:leaving'}); } catch (e) {}
  });
  window.parent.postMessage({type:'cseries:network:hello'}, '*', [ch.port2]);
})();</script>`;

    let hostedPort = null, hostedSeq = 0;

    function hostedPush(graph) {
        if (!hostedPort) return;
        try {
            hostedPort.postMessage({
                type: 'cseries:network:graph',
                // The handle the user already sees in the app header — not the
                // entity_id, which is an identifier the picture never needs.
                host: { label: graph.host.label, handle: graph.host.handle || null },
                // Plain data only — never the `ref` objects, which carry the
                // whole bastion row (bearer-adjacent fields, enrollment, the
                // fingerprint). The frame gets what the picture needs.
                // WHAT A MAP NEEDS, AND NOT ONE FIELD MORE.
                //
                // This used to carry the relay endpoint, the tunnel IPs and the
                // host's entity_id. It no longer does, because the frame CAN
                // still get data out (see HOSTED_CSP: a document may always
                // navigate ITSELF, and no CSP directive stops that). So the
                // question is not "can this leak" but "what is worth leaking",
                // and the answer for drawing a picture is: what to call a node,
                // what kind it is, whether it is up, and what connects to what.
                // The relay address and the tunnel addresses are the reach
                // details — they belong in the inspector, which is shell chrome
                // outside this frame.
                nodes: graph.nodes.map(n => ({
                    id: String(n.id), kind: n.kind, label: n.label,
                    status: n.status, via: n.via || null,
                    machine: n.machine ? {
                        hostname: n.machine.hostname, platform: n.machine.platform,
                    } : null,
                })),
                edges: graph.edges.map(e => ({ from: String(e.from), to: String(e.to), kind: e.kind, status: e.status })),
            });
        } catch (e) { /* the frame went away; the next render re-binds */ }
    }

    window.addEventListener('message', (ev) => {
        const d = ev.data || {};
        if (d.type !== 'cseries:network:hello' || !ev.ports || !ev.ports.length) return;
        const frame = $('topo-hosted-frame');
        // Only the frame we mounted, and only its FIRST hello.
        if (!frame || ev.source !== frame.contentWindow || hostedPort) return;
        hostedPort = ev.ports[0];
        hostedPort.onmessage = (m) => {
            const msg = m.data || {};
            if (msg.type === 'cseries:network:leaving') {
                // The host's document navigated itself away. The request is
                // already gone; what we can still do is stop drawing it and say
                // so, rather than leave a blank frame that looks like a glitch.
                state.hostedLeft = true;
                if (state.lens === 'topology') drawScene();
                return;
            }
            if (msg.type !== 'cseries:network:select') return;
            // Act only on a node we actually sent. A frame cannot conjure a
            // selection for something that is not on the user's map.
            const nodes = buildGraph().nodes;
            if (nodes.some(n => String(n.id) === String(msg.node_id))) onSceneClick(msg.node_id);
        };
        hostedPush(buildGraph());
    });

    registerView('hosted', {
        label: 'Host design',
        sub: 'The map your Host designed. It is drawn from the same live graph as the other views — it cannot invent a machine you do not have.',
        render(graph) {
            const seq = ++hostedSeq;
            hostedPort = null;
            // A map that navigated itself away is not re-mounted on the next
            // render. Re-mounting would put it straight back on its way out and
            // hide the event behind a flicker; the user gets the plain fact and
            // the built-in views, until they reload the app.
            if (state.hostedLeft) {
                return `<div style="padding:48px">${empty(
                    'Your Host\'s map left the page',
                    'The document your Host designed navigated itself somewhere else, which means it sent a request out. It has been stopped and is not being drawn. The Logical and Physical views are unaffected — they are drawn by the app, not by the document.')}</div>`;
            }
            const doc = state.hostedDoc;
            if (!doc) {
                return `<div style="padding:48px">${empty(
                    'Your Host has not designed a map yet',
                    'Ask it to design one in the Artifacts window, then point this view at it. Until then the Logical and Physical views are here.')}</div>`;
            }
            // The bridge goes in FIRST — immediately after a leading DOCTYPE if
            // the document has one, else at index 0 — so nothing of the
            // document's can run before it and take the channel.
            const csp = hostedCsp();
            if (!csp) {
                return `<div style="padding:48px">${empty(
                    'The map cannot be shown right now',
                    'The sandbox policy (/js/sandbox-csp.js) did not load, and your Host\'s own HTML is never rendered without it. Reload the app — the Logical and Physical views are unaffected.')}</div>`;
            }
            // HTML whitespace only, never \s — see withSandboxCsp in
            // /js/sandbox-csp.js: a no-break space ahead of the doctype put
            // the policy in <body>, where it is ignored.
            const m = /^[\t\n\f\r ]*<!DOCTYPE[^>]*>/i.exec(doc);
            const at = m ? m[0].length : 0;
            const withBridge = doc.slice(0, at) + csp + HOSTED_BRIDGE + doc.slice(at);
            setTimeout(() => { if (seq === hostedSeq) hostedPush(buildGraph()); }, 0);
            return `<iframe id="topo-hosted-frame" class="topo-hosted" sandbox="allow-scripts"
                            title="Your Host's network map" srcdoc="${attr(withBridge)}"></iframe>`;
        },
    });

    // The relay endpoint, drawn WHOLE. This is the single most important fact on
    // the physical path — the address your traffic actually leaves through — so
    // it is never elided: host on one line, :port under it. A caller that
    // truncated it to fit a box hid the port and half the hostname, which is the
    // one thing a person came to this view to read.
    function hopLabel(endpoint, cx, y) {
        if (!endpoint) {
            return `<text class="topo-hop-label" x="${cx}" y="${y + 4}">relay</text>`;
        }
        const s = String(endpoint);
        const i = s.lastIndexOf(':');
        // Only split on a HOST:PORT colon — a bare IPv6 literal is all colons
        // and must stay on one line.
        const port = (i > 0 && /^\d+$/.test(s.slice(i + 1))) ? s.slice(i + 1) : null;
        const host = port ? s.slice(0, i) : s;
        if (!port) {
            return `<text class="topo-hop-label" x="${cx}" y="${y + 4}">${esc(host)}</text>`;
        }
        return `<text class="topo-hop-label" x="${cx}" y="${y - 1}">${esc(host)}</text>`
             + `<text class="topo-hop-port" x="${cx}" y="${y + 12}">:${esc(port)}</text>`;
    }

    // A view is offered only when it has something to draw. "Host design" is
    // absent until the host has designed one — an empty third tab that always
    // says "nothing here" is worse than no tab.
    function availableViews() {
        return VIEW_ORDER.filter(n => n !== 'hosted' || !!state.hostedDoc);
    }

    function renderViewPicker() {
        const box = $('topo-viewpick');
        if (!box) return;
        const cur = currentViewName();
        box.innerHTML = availableViews().map(name =>
            `<button class="net-viewbtn ${name === cur ? 'active' : ''}" data-view="${attr(name)}"
                     role="tab" aria-selected="${name === cur ? 'true' : 'false'}">${esc(VIEWS[name].label)}</button>`
        ).join('');
    }

    function drawScene() {
        const scene = $('topo-scene');
        const graph = buildGraph();

        if (!graph.nodes.length) {
            scene.innerHTML = `<div style="padding:48px">${empty('Nothing in your network yet', 'Stand up a bastion in Bastions, or connect to another Host in Peers — they appear here, live, the moment they join your mesh.')}</div>`;
            renderViewPicker();
            return;
        }

        const name = currentViewName();
        const view = VIEWS[name];
        const sub = $('topo-sub');
        if (sub && view.sub) sub.textContent = view.sub;
        try {
            scene.innerHTML = view.render(graph);
        } catch (e) {
            // A renderer is swappable, so a broken one must not take the app
            // down with it — say which view failed and keep the lens usable.
            scene.innerHTML = errBox(`the "${view.label}" view failed to draw: ${e.message}`);
        }
        renderViewPicker();
    }

    function drawTopoTable() {
        const nodes = buildGraph().nodes;
        const t = $('topo-table');
        if (!nodes.length) { t.innerHTML = empty('No nodes', 'Nothing to list yet.'); return; }
        t.innerHTML = nodes.map(n => `
            <div class="net-card ${n.status === 'live' ? 'live' : n.status === 'stale' ? 'stale' : ''}">
                <div class="net-card-main">
                    <div class="net-card-title"><span class="net-card-name">${esc(n.label)}</span>${statusPill(n.status)}</div>
                    <div class="net-card-sub">${esc(n.kind)}</div>
                </div>
            </div>`).join('');
    }

    function statusPill(status) {
        const map = {
            live: ['allow', 'online'], stale: ['warn', 'stale'], down: ['', 'offline'],
            secured: ['dusk', 'secured'], idle: ['', 'idle'],
            approved: ['allow', 'approved'], pending: ['warn', 'pending'], denied: ['deny', 'denied'],
            revoked: ['deny', 'revoked'], active: ['allow', 'active'], blocked: ['deny', 'blocked'],
        };
        const [cls, lbl] = map[status] || ['', status];
        return `<span class="cseries-pill ${cls}">${esc(lbl)}</span>`;
    }

    // node click -> inspector
    function onSceneClick(node) {
        const nodes = buildGraph().nodes;
        const n = nodes.find(x => String(x.id) === String(node));
        if (!n) return;
        if (n.kind === 'bastion') openBastionInspector(n.ref);
        else if (n.kind === 'peer') openPeerInspector(n.ref);
        else openContactInspector(n.ref);
    }

    // ════════════════════ BASTIONS ════════════════════
    // keepInspector: the caller has just opened a panel that is NOT a row
    // snapshot (the one-time pairing token card) and it must survive the reload.
    async function loadBastions({ keepInspector = false } = {}) {
        const seq = ++state.bastionLoadSeq;
        const list = $('gw-list');
        if (!state.bastions.length || !state.loaded.topology) list.innerHTML = skeletonRows(3);
        try {
            state.bastions = (await fetchJSON('/api/network/bastions')).bastions || [];
            state.loaded.topology = true;
            // Fresh rows arrived, so the session set has done its job — drop it and let
            // SERVER TRUTH rebuild it below (renderBastions re-adds from
            // g.update_to_version). The set is documented there as "only a bridge for
            // the moment between the click and the next list load", and it only ever
            // behaved that way because latestVersion could not change inside a tab:
            // its sole removal needs bastion_version === latestVersion, so once the
            // published version MOVED every machine asked at the old version was
            // stranded in the set — excluded from outdatedBastions(), rendered
            // "updating on next check-in", no per-card Update. After an Update all
            // that is the whole fleet, which is the original complaint arriving
            // again by a different route. Nothing is lost: both update paths write
            // update_to_version before they reply (nac_commit.py:1093, :1169).
            state.updateAsked.clear();
            // The inspector renders a SNAPSHOT — its fields and its "Update to X"
            // label are built at open time from the row and the then-current
            // latestVersion, and there is no state to re-render it from. Rows have
            // just been replaced, so anything open is describing data that no longer
            // holds. loadBastions is never on a timer (only loadOverview is), so
            // every call here is a deliberate user action and closing a stale panel
            // is what showLens already does on a lens switch. The one panel that is
            // not a row snapshot is the pairing-token card: setupBastion opens it and
            // then reloads the list with keepInspector, because once this
            // close took the single-use token off the screen on the reload's
            // continuation (after GET /api/network/bastions resolved) — forty tokens
            // minted on one Host in three minutes, one per click, none readable.
            // An older load finishing after a newer one must not close what
            // the newer one opened (a linked machine's inspector).
            if (seq === state.bastionLoadSeq) {
                if (!keepInspector) closeInspector();
            }
        } catch (e) {
            list.innerHTML = errBox(e.message);
            // A link whose load failed is dropped, so a later load cannot open
            // that machine long after anyone asked for it.
            if (seq === state.bastionLoadSeq) state.pendingBastion = null;
            return;
        }
        // The latest published bastion version, RE-READ ON EVERY LOAD.
        //
        // This used to be fetched once per page load, guarded on `=== null`. That
        // meant a tab opened before a publish held the OLD version for as long as
        // it stayed open: every machine still reported the version it was already
        // on, isOutdated() went false for all of them, and the Update button hid
        // itself. Refresh did not help — the guard blocked the re-read — so the
        // only cure was a full page reload, which nothing on screen asked for.
        // Measured: 0.22.0 was published, every box read 0.21.0, the
        // API answered 0.22.0 correctly, and the app showed no update at all.
        //
        // Re-reading costs nothing: the server caches this for 5 minutes
        // (nac_commit.bastion_latest), so a Refresh is at worst one proxied fetch.
        //
        // A FAILED re-read must not undo a good answer. `undefined` means "the
        // lookup failed" and drives the whole cannot-tell notice in
        // renderUpdateAll; flipping a known-good version to undefined because one
        // fetch blipped would hide a working update path and shout about it. So a
        // failure only downgrades when nothing was known before.
        //
        // AND THERE ARE TWO FAILURE SHAPES, not one. The first draft of this fix
        // guarded only the `catch`, and the browser test caught it: /bastion-latest
        // "degrades to null by design" (nac_commit.bastion_latest) — when the host
        // cannot reach the manifest it answers HTTP 200 with version null, it does
        // NOT throw. So a catch-only guard let the REAL failure mode through, wiped
        // a known-good version and hid the update path over precisely the outage it
        // was written to survive (a Cloudflare 403, in miniature).
        // "No version in a 200" and "the call threw" are the same event: a lookup
        // that did not answer.
        try {
            const v = (await fetchJSON('/api/network/bastion-latest')).version;
            if (v) state.latestVersion = v;
            else if (!state.latestVersion) state.latestVersion = undefined;
        } catch (e) {
            if (!state.latestVersion) state.latestVersion = undefined;
        }
        // Embed lane: which model/where the host embeds — drives the "serving your
        // embeddings" badge + the per-bastion serve/use-managed action. Best-effort.
        try { state.embedCurrent = await fetchJSON('/api/embedding/current'); }
        catch (e) { state.embedCurrent = null; }
        // Pairing approvals ask for the step-up code from THIS lens, and the
        // enrolled flag is otherwise only learned by the NAC lens. One cheap read
        // of the grants list carries it; without this a first-time Approve here
        // would route to enrollment even when a factor is already enrolled.
        if (!state.loaded.nac) {
            try {
                const gr = await fetchJSON('/api/network/nac/mgmt/grants');
                if (gr && typeof gr.stepup_enrolled === 'boolean') state.nac.stepupEnrolled = gr.stepup_enrolled;
            } catch (e) { /* the NAC lens will say */ }
        }
        await loadDirectory();
        renderBastions();
        loadEgressStatus();
        loadAutoUpdate();
        if (state.pendingBastion !== null && seq === state.bastionLoadSeq) {
            const g = state.bastions.find(x => x.id === state.pendingBastion);
            state.pendingBastion = null;
            if (g) openBastionInspector(g);
        }
    }

    // ── deep link: the shell's view lane (app-runner.js) ─────
    // '#view=<v>' on a fresh frame, or a 'cseries:app-view' message into an open
    // one. The only view here is 'bastion:<token id>': the Bastions lens with
    // that machine's inspector open. Anything else is ignored.
    function bastionFromView(view) {
        const m = /^bastion:(\d{1,12})$/.exec(String(view || ''));
        return m ? Number(m[1]) : null;
    }
    function viewFromHash() {
        const m = /(?:^|[#&])view=([^&]*)/.exec(window.location.hash || '');
        if (!m) return null;
        try { return decodeURIComponent(m[1]) || null; } catch (e) { return null; }
    }
    function applyView(view) {
        const id = bastionFromView(view);
        if (id === null) return;
        state.pendingBastion = id;
        showLens('bastions');
    }

    // ── Anchoring (bastion 0.15) ──────────────────────────────────────────
    // A machine reports which of two bastions it runs ('local' one person one
    // host; 'networked' many hosts, root-capable), who ANCHORED it, and every
    // pairing with its state. On an anchored machine a later pairing is PENDING
    // until this Host's user approves it — until then it holds nothing. These
    // helpers draw that on the card; the words are the box's own report.
    // Six families (bastion 0.16; inference added later), in plain
    // words. The anchor holds all six; a member holds what its grant
    // says and nothing more.
    const CAP_WORDS = {
        observe: 'read machine state',
        session: 'terminal sessions',
        dcn: 'context and stores',
        harness: 'run coding jobs',
        egress: 'route my traffic out through this machine',
        inference: "use this machine's local models and PII sterilizer",
        // Separate from `inference` on purpose: that one is permission
        // to USE what is already loaded, this one is permission to CHANGE what
        // is on the disk — a download is gigabytes of their storage and minutes
        // of their bandwidth, so it is its own thing to say yes to.
        models: 'download models onto this machine, and remove ones it put there',
        // Separate from `harness` (bastion 0.34.0): that one runs a job and cleans up,
        // this one leaves a server running here, holding its data on this disk.
        services: 'keep a server it built for you running on this machine',
    };
    const PAIR_CAPS = ['observe', 'session', 'dcn', 'harness', 'egress', 'inference', 'models', 'services'];
    // Families the admit card leaves UNCHECKED: a server left running on someone's
    // machine is something they opt into, not something they have to notice and untick.
    const OPT_IN_CAPS = ['services'];

    function modePill(g) {
        if (g.mode === 'networked') return '<span class="cseries-pill" title="A networked bastion: several people\'s hosts pair to it; every pairing after the anchor waits for approval">networked</span>';
        if (g.mode === 'local') return '<span class="cseries-pill" title="A local bastion: one person, one host, no root">local</span>';
        return '';
    }
    function anchorLine(g) {
        const a = g.anchor;
        if (!a || !a.anchor_host) return '';
        const who = a.owner ? esc(a.owner) + ' · ' : '';
        const verified = a.verified ? '' : ' <span class="label-sys" title="The claim\'s signature did not verify under the key it carries — the machine\'s word only">(unverified)</span>';
        return `<div class="net-card-sub">anchored by ${who}${esc(a.anchor_handle || a.anchor_host)}${verified}</div>`;
    }
    function pairingState(p) {
        if (p.denied_at) return { cls: 'deny', word: 'denied' };
        if (p.state === 'pending') return { cls: 'warn', word: 'waiting for your approval' };
        if (p.state === 'revoked') return { cls: 'suspend', word: 'revoked' };
        if (p.grant_state === 'undelivered') return { cls: 'warn', word: 'approved · not yet delivered' };
        return { cls: 'allow', word: 'active' };
    }
    // What a standing update waits on (topology._mark_update_waits, the beat's own
    // rules), so the card never says "updating on next check-in" for an update
    // that goes out only when someone acts, or never from this Host.
    function updatePendingPill(g) {
        const w = g.update_waits_on;
        if (w === 'approval') return `<span class="cseries-pill warn" title="${attr('This machine asks for your approval before it updates. Approve it on your approval device, or at the machine\'s console: one "Approve all shown" on the device covers every machine that is waiting.')}">waiting for your approval</span>`;
        if (w === 'no_way') return `<span class="cseries-pill deny" title="${attr('This machine asks for an approval before it updates and holds no way to take one: pin your approval device on it, or enrol a factor there.')}">update needs an approval device</span>`;
        if (w === 'refused') return `<span class="cseries-pill" title="${attr('You refused this version for this machine, on your approval device or at its console. It is not asked again; a newer version will be.')}">update refused</span>`;
        if (w === 'machine') return `<span class="cseries-pill warn" title="${attr('The machine itself refused the update: ' + (g.update_refused_why || 'no reason given'))}">update refused by the machine</span>`;
        if (w === 'anchor') return `<span class="cseries-pill" title="${attr('This machine takes updates only from the Host it is anchored to.')}">updated by its anchor Host</span>`;
        return `<span class="cseries-pill" title="${attr('It applies on its next check-in — nothing was pushed to the machine')}">updating on next check-in</span>`;
    }

    function pairingsBlock(g) {
        const ps = Array.isArray(g.pairings) ? g.pairings : null;
        if (!ps) return '';   // a bastion too old to say — nothing, not "none"
        const pending = ps.filter(p => p.state === 'pending');
        const others = ps.filter(p => p.state !== 'pending');
        const row = (p) => {
            const st = pairingState(p);
            const caps = (p.caps || []).length ? ' · caps ' + esc(p.caps.join(', ')) : '';
            const btn = (kind, label, cls) =>
                `<button class="cseries-btn ${cls || ''}" data-pair-${kind}="${attr(g.hostname)}" data-pair-subject="${attr(p.host_name)}">${label}</button>`;
            let acts = '';
            if (p.state === 'pending') acts = btn('approve', 'Approve', 'primary') + ' ' + (p.denied_at ? '' : btn('deny', 'Deny', 'ghost'));
            else if (p.state === 'active') acts = btn('revoke', 'Revoke', 'danger');
            return `<div class="net-card-sub net-pairing-row">
                        <span class="cseries-pill ${st.cls}">${st.word}</span>
                        <strong>${esc(p.handle || p.host_name)}</strong> <span class="label-sys">${esc(p.host_name)}</span>${caps}${p.since ? ' · since ' + esc(fmtRel(p.since)) : ''}
                        ${acts ? '<span class="net-pairing-actions">' + acts + '</span>' : ''}
                    </div>`;
        };
        let h = '';
        if (pending.length) {
            h += `<div class="net-card-sub"><strong>Pending pairings</strong> — ${pending.length} host${pending.length === 1 ? '' : 's'} waiting. A pending pairing holds nothing on the machine until you approve it.</div>`
               + pending.map(row).join('');
        }
        if (others.length) h += others.map(row).join('');
        return h;
    }
    function unapprovedPill(g) {
        const at = Array.isArray(g.unapproved_pairing_attempts) ? g.unapproved_pairing_attempts.filter(a => !a.denied_at) : [];
        if (!at.length) return '';
        return `<span class="cseries-pill deny" title="${attr(at.map(a => a.handle || a.host_name).join(', ') + ' — pairing attempt(s) nobody approved. Someone with access to that machine tried to attach a Host.')}">${at.length} unapproved pairing attempt${at.length === 1 ? '' : 's'}</span>`;
    }

    // Second-bastion detection (bastion 0.21.0, otherbastions.go). The box scans
    // its own machine for bastion installations that are not it.
    //
    // undefined/null is NOT the same as []: the bastion omits the key entirely
    // when it could not look (not root, not Linux, or too old), so a missing
    // value means UNKNOWN. Rendering nothing for both would tell the user a
    // machine is clean when it was never checked — so this returns '' only for
    // an explicit empty list, and says "not checked" for absence on a box new
    // enough that the silence is worth explaining.
    // A scan older than this is reported as stale rather than as a result. The
    // box rescans every 5 minutes, so six hours is ~70 missed scans — long past
    // "a slow beat" and firmly into "this stopped working".
    const OTHER_BASTION_STALE_MS = 6 * 3600 * 1000;

    // Kill orders the box has not carried out yet (1.10.2). A containment rides
    // the heartbeat and is only delivered when the box can see its agents; a
    // pairing whose observe is off holds the order instead of dropping it, and
    // before this nothing on screen said an order was waiting at all. The
    // reason is read from the same beat facts the host uses: no agent-observe
    // capability on an online box = it is not looking right now.
    function containPendingPill(g) {
        const pend = Array.isArray(g.contain_pending) ? g.contain_pending : [];
        if (!pend.length) return '';
        const n = pend.length;
        const pids = pend.map(p => p.pid).join(', ');
        const since = pend[0].requested_at ? ' Oldest placed ' + fmtRel(pend[0].requested_at) + '.' : '';
        // A build before 0.31.1 cannot vouch for an EMPTY list (it sent one
        // for a failed walk too), so on those the order is delivered while the
        // agent is still listed and held if the box lists nothing. 0.31.1 is
        // the first bastion version with it; 0.31.0 lacks it. A box that reports no
        // version at all gets the cautious reading — it cannot be shown to
        // vouch for anything.
        const why = !g.online
            ? 'This machine has not checked in, so the order has not reached it.'
            : (g.capabilities || []).indexOf('agent-observe') === -1
                ? 'This machine is not watching its agents right now (observe is off for this pairing, or the platform cannot enumerate), so the order is held rather than delivered — it cannot confirm the process is still the one you pointed at.'
                : (!g.bastion_version || versionBefore(g.bastion_version, '0.31.1'))
                    ? 'It is delivered on every check-in that still lists the agent. This bastion (before 0.31.1) cannot vouch for an empty list, so if it reports no agents at all the order is held until it updates.'
                    : 'It is delivered on every check-in until the agent is gone.';
        return `<span class="cseries-pill warn" title="${attr(n + (n === 1 ? ' containment (pid ' : ' containments (pids ') + pids + ') waiting. ' + why + since + ' An order not carried out within an hour lapses and is written to the management audit.')}">${n === 1 ? 'containment' : n + ' containments'} waiting</span>`;
    }

    // Finding stops the box has not answered yet (1.10.4). A finding is named by
    // its fingerprint, not a pid, and the box answers every order on its next
    // check-in; the order leaves this list on that answer (written to the
    // management audit) or lapses after an hour. A bastion before 0.32.0 does
    // not know the directive and never answers, so the order waits out the
    // hour — said here rather than left to look like a slow machine.
    function findingContainPendingPill(g) {
        const pend = Array.isArray(g.contain_findings_pending) ? g.contain_findings_pending : [];
        if (!pend.length) return '';
        const n = pend.length;
        const fps = pend.map(p => String(p.fp || '').slice(0, 12)).join(', ');
        const since = pend[0].requested_at ? ' Oldest placed ' + fmtRel(pend[0].requested_at) + '.' : '';
        const why = !g.online
            ? 'This machine has not checked in, so the order has not reached it.'
            : (!g.bastion_version || versionBefore(g.bastion_version, '0.32.0'))
                ? 'This bastion is older than 0.32.0 and cannot stop a finding; the order lapses after an hour unless it updates first.'
                : 'The machine answers on its next check-in: it stops the program only if it is still a finding and not in its accounting.';
        return `<span class="cseries-pill warn" title="${attr(n + (n === 1 ? ' finding stop (' : ' finding stops (') + fps + ') waiting. ' + why + since + ' The answer, or a lapse after an hour, is written to the management audit.')}">${n === 1 ? 'finding stop' : n + ' finding stops'} waiting</span>`;
    }

    function otherBastionPill(g) {
        const ob = g.other_bastions;
        if (!Array.isArray(ob)) {
            // Say "not checked" only where the silence is REAL: a box new enough
            // to HAVE the scan that still reported nothing, which means it
            // cannot look (not root, or not Linux).
            //
            // The test is the version that introduced the scan, NOT isOutdated.
            // isOutdated compares against whatever is published, so the moment
            // anything above 0.21.0 ships, a non-root box on 0.21.0 becomes
            // "outdated", loses this pill and its card reads clean. And when the
            // latest-version lookup fails, isOutdated is false for every box, so
            // every card would wear the pill at once. versionBefore is absolute
            // and answers the question actually being asked.
            if (g.bastion_version && !versionBefore(g.bastion_version, '0.21.0')) {
                return `<span class="cseries-pill" title="${attr('This machine has not reported whether another bastion is installed on it. A bastion only looks when it runs as root on Linux — so this is "not checked", not "nothing found".')}">second bastion not checked</span>`;
            }
            return '';
        }
        // A scan that stops completing keeps publishing its LAST answer, and if
        // that answer is [] the card would read "clean" for ever. Age it out
        // before reading it — including, and especially, the empty case.
        if (g.other_bastions_scanned_at) {
            const age = Date.now() - Date.parse(g.other_bastions_scanned_at);
            if (age > OTHER_BASTION_STALE_MS) {
                return `<span class="cseries-pill warn" title="${attr('The last second-bastion scan on this machine finished ' + fmtRel(g.other_bastions_scanned_at) + ' and nothing has completed since, so this machine\'s state is unknown rather than clean.')}">second bastion scan stale</span>`;
            }
        }
        if (!ob.length) return '';   // looked, recently, and the machine is clean
        const paired = ob.filter(o => o.kind === 'paired');
        const label = ob.length === 1 ? 'another bastion' : ob.length + ' other bastions';
        const who = ob.map(o => (o.user || ('uid ' + o.uid)) + (o.kind === 'paired' ? ` (serving ${o.pairings})` : ` (${o.kind})`)).join(', ');
        const cls = paired.length ? 'deny' : 'warn';
        const tip = paired.length
            ? who + ' — a second bastion on this machine is serving Hosts of its own. Its pairings are not governed by your anchor and do not appear here.'
            : who + ' — another bastion is installed on this machine but is not serving any Host yet.';
        return `<span class="cseries-pill ${cls}" title="${attr(tip)}">${esc(label)}</span>`;
    }

    // Approve: pick the ceiling first (which op families that host may later be
    // granted on this machine), THEN the step-up — the code must cover the caps
    // the user read. Reuses the centered modal + withStepup spine from egress.
    function approvePairing(server, subject, handle) {
        const boxes = PAIR_CAPS.map(c =>
            `<label class="net-field" style="flex-direction:row;gap:8px;align-items:center"><input type="checkbox" data-cap="${c}" ${OPT_IN_CAPS.includes(c) ? '' : 'checked'}> <span>${esc(CAP_WORDS[c])}</span></label>`).join('');
        const m = openModal('Admit ' + (handle || subject) + ' on ' + server, `
            <div class="nac-stepup-callout">
                <strong>${esc(handle || subject)}</strong> paired a Host to <strong>${esc(server)}</strong>, a machine you anchored. Admitting it opens its lanes on that machine. The caps below are a CEILING — what that host's own user may later be granted there; they can always be narrowed, and you can revoke the pairing at any time.
            </div>
            <div class="net-field"><span class="net-field-label">Allow on this machine</span>${boxes}</div>`, [
            { label: 'Cancel', cls: 'ghost', onClick: closeModal },
            { label: 'Continue to code', cls: 'primary', onClick: () => {
                const caps = Array.from(m.body.querySelectorAll('input[data-cap]')).filter(i => i.checked).map(i => i.getAttribute('data-cap'));
                closeModal();
                withStepup(async (code) => {
                    try {
                        const res = await fetchJSON('/api/network/bastions/pairings/approve', {
                            method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ server, subject, caps, stepup_code: code }),
                        });
                        if (res && res.delivered === false) alert('Approved and signed. ' + (res.note || ''));
                        state.loaded.topology = false;
                        await loadBastions();
                    } catch (e) { alert('Could not approve. ' + e.message); }
                }, true);
            } },
        ]);
    }
    async function denyPairing(server, subject, handle) {
        if (!confirm(`Deny ${handle || subject} on ${server}? Nothing is sent to the machine — the pairing stays pending there with no lanes, and keeps being reported until it is removed on the box.`)) return;
        try {
            await fetchJSON('/api/network/bastions/pairings/deny', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ server, subject }),
            });
            state.loaded.topology = false;
            await loadBastions();
        } catch (e) { alert('Could not deny. ' + e.message); }
    }
    async function revokePairing(server, subject, handle) {
        if (!confirm(`Revoke ${handle || subject} on ${server}? The machine drops that host's lanes and tunnel peer immediately.`)) return;
        try {
            const res = await fetchJSON('/api/network/bastions/pairings/revoke', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ server, subject }),
            });
            if (res && res.delivered === false) alert('Revoked here. ' + (res.note || ''));
            state.loaded.topology = false;
            await loadBastions();
        } catch (e) { alert('Could not revoke. ' + e.message); }
    }

    // ── Embed lane helpers ───────────────────────────────────
    // Normalize an endpoint for comparison: lowercase, strip a trailing slash and
    // a trailing /api (ollama embed paths vary). Lets reported_ip:11434 match a
    // stored base_url even when one side carries a path the other doesn't.
    function _embedEpNorm(u) {
        return String(u || '').trim().toLowerCase().replace(/\/+$/, '').replace(/\/api$/, '');
    }
    // The endpoint a bastion serves embeddings on (server-reported, else ollama default).
    function bastionEmbedEndpoint(g) {
        if (!g) return null;
        const ep = (g.embed_endpoint || '').trim();
        if (ep) return ep;
        const ip = (g.reported_ip || '').trim();
        if (!ip) return null;
        const host = (ip.indexOf(':') !== -1 && ip[0] !== '[') ? `[${ip}]` : ip;  // bracket bare IPv6
        return `http://${host}:11434`;
    }
    function bastionCanEmbed(g) {
        return !!(g && (g.capabilities || []).indexOf('embed') !== -1 && bastionEmbedEndpoint(g));
    }
    // Is THIS bastion the live embedder? provider must be 'local'. The server
    // persists the bastion IDENTITY (cur.bastion = hostname) and resolves the
    // endpoint live from its heartbeat — so match on identity, exact. The URL
    // compare survives only for pure-BYO configs from before identities existed.
    function bastionIsServingEmbeds(g) {
        const cur = state.embedCurrent;
        if (!cur || cur.provider !== 'local') return false;
        if (cur.bastion) return cur.bastion === (g.hostname || '');
        const base = _embedEpNorm(cur.embedding_base_url);
        if (!base) return false;
        const ep = _embedEpNorm(bastionEmbedEndpoint(g));
        if (!ep) return false;
        // Match on full endpoint OR on the host:port stem (paths can differ).
        return base === ep || base.indexOf(ep) === 0 || ep.indexOf(base) === 0;
    }
    // Human label for the model the bastion is/would serve (the host's current model).
    function embedModelLabel() {
        const cur = state.embedCurrent;
        return (cur && cur.model) || 'your model';
    }
    // Friendly capability label. 'embed' is surfaced separately as a badge; the rest
    // get a readable name where known, else the raw token.
    const CAP_LABELS = { embed: 'embeddings', 'context-store': 'context store', harness: 'harness', mgmt: 'management', egress: 'egress', pii: 'PII sterilizer', freeagent: 'bastion agent' };
    function capLabel(c) { return CAP_LABELS[c] || String(c); }
    function capChips(caps) {
        const list = (caps || []).filter(Boolean);
        if (!list.length) return '<span class="cseries-pill">none</span>';
        return list.map(c => {
            const isEmbed = c === 'embed';
            return `<span class="cseries-pill ${isEmbed ? 'allow' : ''}" title="${attr('capability: ' + c)}">${esc(capLabel(c))}</span>`;
        }).join(' ');
    }

    // Route this host's embeddings to a bastion. The MODEL is the user's choice
    // — default to what the box actually SERVES (its advertised models), not a
    // forced keep of the current one (a box that doesn't run the current model
    // would fail the probe gate every time). Nothing ever re-embeds or dumps:
    // storage is multi-dim, a switch is a pointer change.
    async function serveEmbedsHere(g) {
        const name = g.hostname || g.name || ('bastion ' + g.id);
        const served = (g.models || []).filter(Boolean);
        const cur = (state.embedCurrent && state.embedCurrent.model) || '';
        // Best default: the current model when the box serves it (or the box
        // hasn't reported models), else the first model the box says it runs.
        const def = (served.length && served.indexOf(cur) === -1) ? served[0] : cur;
        const hint = served.length
            ? 'This box serves: ' + served.join(', ')
            : 'This box has not reported its models — enter one it actually runs.';
        const model = prompt(
            'Serve your embeddings on ' + name + '?\n\n' + hint +
            '\n\nEmbedding model to serve there (probe-gated: the box must really run it). ' +
            'Existing vectors are never re-embedded or dumped — each model\'s vectors ' +
            'keep their own column.',
            def);
        if (model === null) return;  // cancelled
        const body = { server: g.hostname };
        const m = (model || '').trim();
        if (m && m !== cur) body.model = m;
        try {
            const res = await fetchJSON('/api/embedding/use-bastion', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (res && res.error) { alert('Could not serve here. ' + res.error); return; }
            closeInspector();
            state.loaded.topology = false;
            await loadBastions();
        } catch (e) { alert('Could not serve here. ' + e.message); }
    }
    // Fall back to the platform-managed embedder (keeps the model — no re-embed).
    async function useManagedEmbeds() {
        if (!confirm('Fall back to the managed embedder? Your current model (' + embedModelLabel() + ') is kept and existing vectors are untouched — new embeds compute on the platform.')) return;
        try {
            const res = await fetchJSON('/api/embedding/use-managed', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
            });
            if (res && res.error) { alert('Could not switch. ' + res.error); return; }
            closeInspector();
            state.loaded.topology = false;
            await loadBastions();
        } catch (e) { alert('Could not switch. ' + e.message); }
    }
    // The OS-specific command to bring a STALE/offline bastion back — the host knows
    // the box's OS from its last heartbeat (g.platform). A power hit or reboot stops
    // the heartbeat, so the host marks it stale (no check-in within the online
    // window); the bastion's `run` reconnects from the SAVED pairing — no re-pair.
    // Linux installs a Restart=always service so a restart is the move there;
    // Windows/macOS run it directly.
    function restartCommand(g) {
        const p = String((g && g.platform) || '').toLowerCase();
        if (isWindows(g)) return 'privatae-bastion run';
        if (p.indexOf('linux') !== -1) return 'systemctl restart privatae-bastion';
        return 'privatae-bastion run';   // macOS / darwin / unknown
    }

    // `platform` is the box's own runtime.GOOS (client-go/run.go sends it; stored
    // at topology.py and surfaced as g.platform), so the values are exactly
    // 'windows', 'darwin', 'linux' — sometimes with an arch suffix.
    //
    // MATCH 'darwin' FIRST. `'darwin'.indexOf('win') === 3`, so a bare substring
    // test for 'win' calls every Mac a Windows box. That once happened in
    // adminHowTo below: every Mac in the inspector was told to open
    // an Administrator PowerShell. restartCommand has carried the same bug since
    // it was written and never showed it, because its darwin answer happens to
    // equal its Windows answer — the test was wrong and the output was right.
    function isWindows(g) {
        const p = String((g && g.platform) || '').toLowerCase();
        return p.indexOf('darwin') === -1 && p.indexOf('win') !== -1;
    }

    // How to run a machine-authority command on THAT machine. Windows needs an
    // Administrator terminal and has no `sudo`; telling a Windows user to sudo —
    // or a Mac user to open PowerShell — makes a correct step look broken.
    function adminHowTo(g) {
        if (isWindows(g)) return 'in an Administrator PowerShell (right-click → Run as administrator)';
        return 'with <code>sudo -H</code>';
    }

    // One line under the toolbar instead of an alert box. Success fades on the next
    // render of the list; an error stays until the user acts, because an error the
    // user never read is the same as no error at all.
    function setGwNotice(text, isError) {
        const el = $('gw-notice');
        if (!el) return;
        el.textContent = text;
        el.className = 'status-banner ' + (isError ? 'error' : 'success');
        el.hidden = false;
        clearTimeout(setGwNotice._t);
        if (!isError) setGwNotice._t = setTimeout(() => { el.hidden = true; }, 12000);
    }

    // Retract a notice. `errorsOnly` so a success message the user is still reading is
    // not yanked out from under them by a routine re-render — only the sticky error
    // kind, which never times out on its own, is cleared this way.
    function clearGwNotice(errorsOnly) {
        const el = $('gw-notice');
        if (!el || el.hidden) return;
        if (errorsOnly && !/\berror\b/.test(el.className)) return;
        clearTimeout(setGwNotice._t);
        el.hidden = true;
        el.textContent = '';
    }

    function renderBastions() {
        // A machine that came back on the published version is done: drop it from the
        // asked set so its card reads from the heartbeat again rather than from what we
        // asked for. Without this, a box that failed to update would keep claiming
        // "updating on next check-in" for the rest of the session. Done here because
        // every path that changes the list ends up rendering.
        state.bastions.forEach(g => {
            // Server truth first: a standing directive on the row means the machine has
            // been told, whoever told it and whenever. The session set is only a bridge
            // for the moment between the click and the next list load.
            // ...but only when the directive names the version we would ask for NOW.
            // The box refuses any target that is not the currently published one
            // (the gateway's update check: "manifest is X but host asked for Y,
            // skipping"), so a directive left over from an earlier release can never
            // apply — and it never clears either, because clearing needs the box to
            // report exactly that version. Counting a dead directive as "asked" hid the
            // machine from Update all permanently. Measured on one Host:
            // 8 of 23 machines carried a stale 0.17.0 / 0.11.2 directive, all
            // online — so a single click
            // would have moved 15 machines and silently skipped those 8.
            if (g.update_to_version && g.update_to_version !== g.bastion_version
                && state.latestVersion && g.update_to_version === state.latestVersion) {
                state.updateAsked.add(String(g.id));
            }
            if (g.bastion_version && g.bastion_version === state.latestVersion) state.updateAsked.delete(String(g.id));
        });
        renderUpdateAll();
        const list = $('gw-list');
        if (!state.bastions.length) {
            list.innerHTML = empty('No bastions yet',
                'A bastion is your secure door into your private network — a machine you stand up (laptop, home server, cloud box) that joins your mesh. Click “Set up a bastion” to enroll one.');
            return;
        }
        const dirErr = state.directoryError
            ? `<div class="status-banner error" data-hosts-error>Couldn't read which other Hosts are on your machines: ${esc(state.directoryError)}</div>`
            : '';
        list.innerHTML = dirErr + state.bastions.map(g => {
            const status = g.online ? 'live' : (g.last_seen_at ? 'stale' : 'down');
            const sub = [g.hostname, g.platform, g.bastion_version].filter(Boolean).join(' · ') || 'awaiting first check-in';
            const outdated = isOutdated(g);
            const canEmbed = bastionCanEmbed(g);
            const serving = bastionIsServingEmbeds(g);
            const embedBadge = serving
                ? `<span class="cseries-pill allow" title="${attr('This bastion is the live embedder for ' + embedModelLabel())}">● serving your embeddings</span>`
                : (canEmbed ? '<span class="cseries-pill" title="Advertises an embedder you can serve your embeddings on">Embeddings</span>' : '');
            return `
                <div class="net-card ${status} clickable" data-gw="${attr(g.id)}">
                    <div class="net-card-main">
                        <div class="net-card-title">
                            <span class="net-status-dot ${status === 'live' ? 'live' : status === 'stale' ? 'stale' : ''}"></span>
                            <span class="net-card-name">${esc(g.hostname || g.name || ('bastion ' + g.id))}</span>
                            ${statusPill(status)}
                            ${modePill(g)}
                            ${embedBadge}
                            ${unapprovedPill(g)}
                            ${otherBastionPill(g)}
                            ${containPendingPill(g)}
                            ${findingContainPendingPill(g)}
                            ${outdated && state.updateAsked.has(String(g.id))
                                ? updatePendingPill(g)
                                : (outdated ? `<span class="cseries-pill warn" title="${attr('Latest is ' + state.latestVersion)}">update available</span>` : '')}
                        </div>
                        <div class="net-card-sub">${esc(sub)} · seen ${esc(fmtRel(g.last_seen_at))}</div>
                        ${anchorLine(g)}
                        ${status !== 'live' ? `<div class="net-card-sub net-recover">Offline${g.platform ? ' (' + esc(g.platform) + ')' : ''} — restart it: <code>${esc(restartCommand(g))}</code></div>` : ''}
                        ${pairingsBlock(g)}
                        ${machineHostsBlock(g)}
                    </div>
                    <div class="net-card-actions">
                        ${outdated && !state.updateAsked.has(String(g.id))
                            ? `<button class="cseries-btn primary" data-gw-update="${attr(g.id)}">Update</button>` : ''}
                        <button class="cseries-btn ghost" data-gw-open="${attr(g.id)}">Details</button>
                    </div>
                </div>`;
        }).join('');
    }

    // ── The other Hosts on a machine ────────────────────────────────────────
    // Moved from Circles' Bastions pane (circles-web/src/components/panes/
    // bastions-pane.tsx), which goes when the cards replace Circles' rail. Every
    // line is the machine's own report: it tells each Host it serves about the
    // others, and the platform never joins one machine across Hosts. What this
    // Host adds is the Host id each peering resolved to, which is what lets a
    // row show that Host's card and send a request through the peer-gated route.
    async function loadDirectory() {
        try {
            const r = await fetchJSON('/api/network/bastions/directory');
            const by = {};
            for (const m of (r && r.machines) || []) by[m.token_id] = m;
            state.directory = by;
            state.directoryError = null;
        } catch (e) {
            state.directory = null;
            state.directoryError = e.message || 'the request failed';
            return;
        }
        // Each identified Host's card, read again on every load: the row's
        // button (Message, Request connection, Request sent) comes from it.
        const ids = [];
        for (const m of Object.values(state.directory)) {
            for (const h of (m.hosts || [])) if (h.host_id) ids.push(h.host_id);
        }
        if (ids.length) await hostCards(ids, true);
    }

    function fmtDay(iso) {
        const d = iso ? new Date(iso) : null;
        return d && !isNaN(d.getTime()) ? d.toLocaleDateString() : '';
    }

    // The words or the button a row ends with. Same order as the Circles pane:
    // a Host not on the machine, then not identified, then what its card says.
    // A row whose card could not be read offers the request rather than nothing.
    function machineHostAction(h, card) {
        if (h.state !== 'active') return '';   // waiting for the anchor, or removed: nobody to connect to
        const status = (t) => `<span class="net-host-status" data-host-status>${esc(t)}</span>`;
        if (h.host_id == null) return status('Not identified yet');
        const ask = state.hostAsks[h.host_id];
        if (card && card.can_message) {
            return `<button class="cseries-btn" type="button" data-host-message="${attr(h.host_id)}">Message</button>`;
        }
        if ((ask && ask.state === 'sent') || (card && card.relationship === 'pending')) return status('Request sent');
        if (card && card.relationship === 'blocked') return status('Blocked');
        // The request route only accepts a peering the machine still announces.
        if (!h.current) return status('No longer announced');
        const sending = ask && ask.state === 'sending';
        return `<button class="cseries-btn" type="button" data-host-request="${attr(h.host_id)}"${sending ? ' disabled' : ''}>${sending ? 'Sending…' : 'Request connection'}</button>`;
    }

    function machineHostRow(h) {
        const card = h.host_id ? _cards.get(Number(h.host_id)) : null;
        const name = card ? card.display_name : '@' + (h.handle || h.host_name);
        const ask = h.host_id ? state.hostAsks[h.host_id] : null;
        const pills = [
            h.is_anchor ? '<span class="cseries-pill dusk">Anchor</span>' : '',
            card && card.mine ? '<span class="cseries-pill">Your host</span>' : '',
            h.state === 'pending' ? '<span class="cseries-pill warn">Waiting for the anchor</span>' : '',
            h.state === 'revoked' ? '<span class="cseries-pill">Removed</span>' : '',
        ].join('');
        const since = fmtDay(h.since);
        const sub = (card && card.user ? card.user + ' · ' : '') + h.host_name + (since ? ' · on this machine since ' + since : '');
        // The name opens the Host's card when there is one to open.
        const nameHtml = card
            ? `<button class="net-host-name" type="button" data-host-card="${attr(h.host_id)}" title="Show this Host's card">${esc(name)}</button>`
            : `<span class="net-host-name">${esc(name)}</span>`;
        return `<div class="net-host-row" data-host-row="${attr(h.host_name)}">
                    <div class="net-host-who">
                        <div class="net-host-title">${nameHtml}${pills}</div>
                        <div class="net-host-sub">${esc(sub)}</div>
                        ${ask && ask.state === 'error' ? `<div class="net-host-error" role="alert">${esc(ask.message)}</div>` : ''}
                    </div>
                    <div class="net-host-action">${machineHostAction(h, card)}</div>
                </div>`;
    }

    function machineHostsBlock(g) {
        const m = state.directory && state.directory[g.id];
        // Not read, or a machine that has not reported yet: its card already
        // says it is waiting for its first check-in.
        if (!m || !m.heard) return '';
        let body;
        if (m.hosts === null) body = '<div class="net-host-note">This machine\'s bastion is too old to report which other Hosts it serves.</div>';
        else if (!m.hosts.length) body = '<div class="net-host-note">Only your Host is on this machine.</div>';
        else body = m.hosts.map(machineHostRow).join('');
        return `<div class="net-hosts" data-machine-hosts="${attr(g.id)}">
                    <div class="net-hosts-head"><span class="label-sys">Other Hosts on this machine</span><span class="net-host-note">as the machine reports them</span></div>
                    ${body}
                </div>`;
    }

    async function requestHost(id) {
        const hostId = Number(id);
        if (!hostId) return;
        const cur = state.hostAsks[hostId];
        if (cur && (cur.state === 'sending' || cur.state === 'sent')) return;
        state.hostAsks[hostId] = { state: 'sending' };
        renderBastions();
        try {
            await fetchJSON('/api/contacts/request-peer', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ peer_entity_id: hostId }),
            });
            state.hostAsks[hostId] = { state: 'sent' };
            // The request moved the relationship: read the card again.
            await hostCards([hostId], true);
        } catch (e) {
            state.hostAsks[hostId] = { state: 'error', message: e.message || 'The request did not go through.' };
        }
        renderBastions();
    }

    // Message opens Circles on that Host's card. The link is card:entity:<id>
    // (circles-web/src/lib/cards.ts cardsTarget): the Host's own card, or the
    // back of the person it is paired with.
    function messageHost(id) {
        const hostId = Number(id);
        if (!hostId) return;
        try {
            (window.parent || window).postMessage(
                { type: 'cseries:open-app', app_id: 'circles', view: 'card:entity:' + hostId },
                window.location.origin);
        } catch (e) { /* not framed */ }
    }

    function openHostCard(id) {
        const card = _cards.get(Number(id));
        if (card) openInspector(card.display_name, cardFields(card), []);
    }

    // NAC agent: what the box says it ENFORCES, beside the host's verdict
    // on it (posture + why, from the NAC engine). One line, plain words.
    function enforcingSummary(g) {
        const e = g.enforcing;
        if (!e) return '<span class="label-sys">not reported yet — a bastion on 0.13.5 or later reports what it enforces on every check-in</span>';
        const grants = Array.isArray(e.grants) ? e.grants : [];
        const posture = g.nac_posture || 'unknown';
        const pill = posture === 'healthy' ? 'allow' : (posture === 'degraded' ? 'warn' : 'deny');
        const parts = [
            `<span class="cseries-pill ${pill}">${esc(posture)}</span>`,
            `${grants.length} store grant${grants.length === 1 ? '' : 's'} held`,
            (Array.isArray(e.tunnel_allowed_ips) && e.tunnel_allowed_ips.length) ? 'tunnel reaches only ' + esc(e.tunnel_allowed_ips.join(', ')) : '',
            Array.isArray(e.mgmt_ops) ? `${e.mgmt_ops.length} management ops answered` : '',
            e.binary_sha256 ? 'binary ' + esc(String(e.binary_sha256).slice(0, 12)) + '…' : '',
            e.stepup ? '<span class="cseries-pill allow" title="A second factor is enrolled on this machine (privatae-bastion stepup-enroll): folder grants and move plans for it are approved ON the machine (privatae-bastion approvals / approve); your Host never handles the code">approved on this machine</span>' : '',
            // The write gate (bastion 0.33.0): on a machine anchored to this Host
            // every write runs only on your approval, from your device or its
            // console. Three states a person can act on, in words.
            e.write_gate === true
                ? (e.second_factor === true
                    ? `<span class="cseries-pill allow" title="Every write on this machine — a file, a program, a kill, a bastion update — runs only on an approval you give${e.device ? ' on your approval device' : ' at its console'}; reads answer as before${e.is_anchor === true && e.update_forward_needs_no_approval === true ? '. An update to a newer bastion version needs no approval: our signature on the release is the approval' : ''}">writes need your approval${e.device ? ' (device)' : ' (console)'}</span>`
                    : `<span class="cseries-pill deny" title="This machine gates writes and can take no approval: ${e.is_anchor === true && e.update_forward_needs_no_approval === true ? 'every write except an update to a newer bastion version' : 'every write'} is refused until you pin your approval device on it (privatae-bastion device-init on your own machine, then sudo -H privatae-bastion pin-device <key> there) or enrol a factor on it">writes refused — pin your device</span>`)
                : '',
            e.approval_device === true ? '<span class="cseries-pill allow" title="This machine is your approval device: writes your Host asks of the machines you govern are approved with a tap here (privatae-bastion device-approvals / device-approve)">approval device</span>' : '',
            Array.isArray(e.quarantined) && e.quarantined.length
                ? `<span class="cseries-pill deny" title="The machine closed these pairings' write lanes after five refused writes in ten minutes (reads still answer); it lifts itself, or at the machine: privatae-bastion quarantine clear <host>">write lanes quarantined for ${esc(e.quarantined.join(', '))}</span>`
                : '',
        ].filter(Boolean);
        const why = g.nac_decision ? `<div class="label-sys">${esc(g.nac_decision)}</div>` : '';
        return parts.join(' · ') + why;
    }

    function openBastionInspector(g) {
        const status = g.online ? 'online' : (g.last_seen_at ? 'stale' : 'offline');
        const canEmbed = bastionCanEmbed(g);
        const serving = bastionIsServingEmbeds(g);
        const endpoint = bastionEmbedEndpoint(g);

        const fields = [
            ['status', status],
            ['machine', g.hostname || '—'],
            ['platform', g.platform || '—'],
            ['bastion version', g.bastion_version ? (g.bastion_version + (isOutdated(g) ? '  (latest: ' + state.latestVersion + ')' : '')) : '—'],
            ['tunnel', g.tunnel_status || '—'],
            ['reported ip', g.reported_ip || '—', true],
            ['wireguard key', g.wg_pubkey || '—', true],
            // capabilities → discrete chips (raw HTML), with an Embeddings emphasis.
            ['capabilities', capChips(g.capabilities), false, true],
            ['enforcing', enforcingSummary(g), false, true],
        ];

        // Who owns the machine, if anyone said so. Just the name, reading like
        // every other field here. It answers "whose box is this" — not a
        // security assertion, and no badge, because dressing it as one only
        // invites it to be trusted for something. Absent when nobody has said.
        if (g.owner_claim && g.owner_claim.owner) {
            fields.push(['owner', g.owner_claim.owner]);
            // The machine's signing key changed after it had already claimed an
            // owner. Re-claiming under a NEW NAME deliberately keeps the key
            // (client-go/owner.go), precisely so an ordinary rename stays
            // distinguishable from this — so a changed key is a different box,
            // or a rebuilt one, answering for a pairing made with the original.
            // Said plainly, because it is the one thing this claim is actually
            // good for.
            if (g.owner_claim.previous_public_key) {
                fields.push(['owner key', 'CHANGED'
                    + (g.owner_claim.key_changed_at
                        ? ' on ' + String(g.owner_claim.key_changed_at).slice(0, 10) : '')
                    + ' — this machine signs as a different box than when the owner was'
                    + ' first claimed. A rename keeps the key; only a rebuilt or'
                    + ' replaced machine changes it.']);
            }
        }

        // Anchoring: which bastion this is and who anchored it. Absent on a
        // bastion too old to say — the field is not shown rather than guessed.
        if (g.mode) fields.push(['bastion', g.mode === 'networked' ? 'networked — many hosts, root-capable; pairings wait for the anchor' : 'local — one person, one host']);
        if (g.anchor && g.anchor.anchor_host) {
            fields.push(['anchored by', (g.anchor.owner ? g.anchor.owner + ' · ' : '') + (g.anchor.anchor_handle || g.anchor.anchor_host)
                + (g.anchor.verified ? '' : '  (signature unverified)')]);
        }
        // THE TWO COMMANDS, where they apply. Until now this app named the anchor
        // only as a state to display — nothing anywhere said `set-mode` or
        // `claim-anchor` existed, so a user with a machine their family or team
        // should reach could not get there from the product.
        //
        // GATED ON 0.23.0, which is where claim-anchor's token became optional.
        // Handing a 0.22.0 box a bare `claim-anchor` prints usage and exits 2, so
        // an ungated row would tell most of the fleet to run a command its own
        // bastion rejects. Same gate shape as the pairing row at :1084. A box that
        // reports no version at all is not offered the command either:
        // versionBefore returns false for a missing version, so the `!g.x ||` is
        // load-bearing and not belt-and-braces.
        const anchorCmdReady = !!g.bastion_version && !versionBefore(g.bastion_version, '0.23.0');
        // A box serving two or more hosts is refused with "ambiguous" — the token
        // is what names WHICH host takes the anchor there (anchor.go). Promising
        // "no token needed" to such a box would be a promise the binary breaks.
        const onePairing = !Array.isArray(g.pairings) || g.pairings.length <= 1;
        if (g.mode === 'local' && anchorCmdReady) {
            fields.push(['make it shared',
                '<code>privatae-bastion set-mode networked</code><br><code>privatae-bastion claim-anchor</code>'
                + '<div class="net-field-note">Optional — only if other people\'s hosts should pair to this machine.'
                + ' Run both on the machine itself, ' + adminHowTo(g) + '.'
                + ' You become its anchor: every host paired there afterwards waits for your approval.</div>',
                false, true]);
        } else if (g.mode === 'networked' && !(g.anchor && g.anchor.anchor_host) && anchorCmdReady) {
            fields.push(['anchor it',
                '<code>privatae-bastion claim-anchor' + (onePairing ? '' : ' &lt;token&gt;') + '</code>'
                + '<div class="net-field-note">This machine is networked but <strong>not anchored</strong>, so a host paired there is admitted with nobody approving it.'
                + ' Run this on the machine, ' + adminHowTo(g) + ', to make this host its anchor.'
                + (onePairing
                    ? ' No token — it anchors to the pairing you already have.'
                    : ' It serves several hosts, so the token names which one takes the anchor — set one up from this Host to claim it here.')
                + '</div>',
                false, true]);
        }
        if (Array.isArray(g.pairings)) {
            const n = (s) => g.pairings.filter(p => p.state === s).length;
            fields.push(['pairings', `${n('active')} active · ${n('pending')} pending · ${n('revoked')} revoked`]);
        }
        if ((g.pairing_grants || []).filter(x => x.state === 'active' || x.state === 'undelivered').length) {
            fields.push(['grants you signed',
                g.pairing_grants.filter(x => x.state === 'active' || x.state === 'undelivered')
                    .map(x => esc(x.subject_handle || x.subject) + ' [' + esc((x.caps || []).join(', ') || 'no caps') + ']' + (x.state === 'undelivered' ? ' (not yet delivered)' : '')).join('<br>'),
                false, true]);
        }

        // The other Hosts this machine serves, as the machine reports them —
        // with the pairing state when the box says it (0.16+).
        if ((g.peers || []).length) {
            fields.push(['also serves',
                (g.peers || []).map(p => esc(p.handle || p.host_name) + (p.state && p.state !== 'active' ? ' <span class="label-sys">(' + esc(p.state) + ')</span>' : '')).join(', '),
                false, true]);
        }

        // ── Embeddings (the sovereign lane) — only when the box advertises it. ──
        if (canEmbed) {
            fields.push(['embeddings', serving
                ? `<span class="cseries-pill allow">● serving your embeddings</span> <span class="label-sys">model ${esc(embedModelLabel())}</span>`
                : '<span class="cseries-pill">available to serve</span>', false, true]);
            if (endpoint) fields.push(['embed endpoint', endpoint, true]);
        }

        // ── Hardware — the lane's premise is "the user's GPU". Surface the fingerprint
        //    and any hardware/GPU detail the bastion reported (best-effort over a few
        //    likely shapes; the heartbeat schema may carry it under different keys). ──
        fields.push(['fingerprint', g.hardware_fingerprint || '—', true]);
        const hw = g.hardware || g.hardware_info || g.hw || null;
        if (hw) {
            const gpu = hw.gpu || hw.gpus || hw.gpu_name || null;
            if (gpu) fields.push(['gpu', Array.isArray(gpu) ? gpu.join(', ') : String(gpu)]);
            const cpu = hw.cpu || hw.cpu_model || null;
            if (cpu) fields.push(['cpu', String(cpu)]);
            const mem = hw.memory || hw.mem || hw.ram || null;
            if (mem) fields.push(['memory', String(mem)]);
        } else if (g.gpu || g.gpu_name) {
            fields.push(['gpu', String(g.gpu || g.gpu_name)]);
        }

        fields.push(['handle', g.handle || '—', true]);

        // ── Enrollment — computed but never rendered before. WireGuard endpoint + the
        //    mesh IPs the box and host carry. Only shown when present. ──
        const en = g.enrollment || null;
        if (en && (en.wg_endpoint || en.wg_host_ip || en.wg_bastion_ip)) {
            if (en.wg_endpoint) fields.push(['wg endpoint', en.wg_endpoint, true]);
            if (en.wg_host_ip) fields.push(['wg host ip', en.wg_host_ip, true]);
            if (en.wg_bastion_ip) fields.push(['wg bastion ip', en.wg_bastion_ip, true]);
        }

        fields.push(['enrolled', fmtRel(g.created_at)]);
        fields.push(['last check-in', fmtRel(g.last_seen_at)]);
        // Stale/offline recovery: the exact OS-specific command to bring it back.
        if (!g.online) fields.push(['restart', `<code>${esc(restartCommand(g))}</code> <span class="label-sys">— reconnects from the saved pairing; no re-pair needed</span>`, false, true]);

        const actions = [];
        if (isOutdated(g) && !state.updateAsked.has(String(g.id))) actions.push({ label: 'Update to ' + state.latestVersion, cls: 'primary', onClick: () => updateBastion(g) });
        // Embed-lane one-click actions on an ONLINE, embed-capable bastion.
        if (g.online && canEmbed) {
            if (serving) actions.push({ label: 'Use managed instead', onClick: () => useManagedEmbeds() });
            else actions.push({ label: 'Serve my embeddings here', cls: 'primary', onClick: () => serveEmbedsHere(g) });
        }
        actions.push({ label: 'Revoke bastion', cls: 'danger', onClick: () => revokeBastion(g) });

        openInspector(g.hostname || g.name || ('Bastion ' + g.id), fields, actions);
    }

    async function revokeBastion(g) {
        if (!confirm(`Revoke ${g.hostname || g.name || ('bastion ' + g.id)}? The door shuts immediately and it must be re-enrolled to rejoin your network.`)) return;
        try {
            await fetchJSON(`/api/network/bastions/${encodeURIComponent(g.id)}/revoke`, { method: 'POST' });
            closeInspector();
            state.loaded.topology = false;
            await loadBastions();
            loadOverview();
        } catch (e) { alert('Could not revoke. ' + e.message); }
    }

    function isOutdated(g) {
        return !!(g && g.bastion_version && state.latestVersion && g.bastion_version !== state.latestVersion);
    }
    async function updateBastion(g) {
        if (!state.latestVersion) return;
        const name = g.hostname || g.name || ('bastion ' + g.id);
        if (!confirm('Update ' + name + ' to ' + state.latestVersion + '? It pulls + verifies + self-updates on its next check-in.')) return;
        try {
            await fetchJSON('/api/network/bastions/' + encodeURIComponent(g.id) + '/update', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ version: state.latestVersion }),
            });
            closeInspector();
            // No second popup. The answer is the card: its pill flips to "updating on
            // next check-in" and the button goes. An alert saying the same thing was
            // one more click for something the user can already see.
            state.updateAsked.add(String(g.id));
            // Reloaded, not only redrawn: the server knows at once whether the update
            // waits on an approval, and the card and this notice should say so now.
            await loadBastions();
            setGwNotice(updateAskedNotice(name, state.bastions.find(x => String(x.id) === String(g.id))));
        } catch (e) { setGwNotice('Could not ask ' + name + ' to update. ' + e.message, true); }
    }

    // What happens next for one machine just asked to update, in the card's terms.
    function updateAskedNotice(name, g) {
        const w = g && g.update_waits_on, v = state.latestVersion;
        if (w === 'approval') return name + ' updates to ' + v + ' once you approve it on your approval device.';
        if (w === 'no_way') return name + ' needs an approval device before it can update.';
        if (w === 'refused') return 'You refused ' + v + ' for ' + name + ', so it is not asked again.';
        if (w === 'machine') return name + ' refused the update: ' + ((g && g.update_refused_why) || 'no reason given');
        if (w === 'anchor') return name + ' takes updates only from the Host it is anchored to.';
        return name + ' updates to ' + v + ' on its next check-in.';
    }

    // ── Update every machine, one decision ───────────────────────────────────
    // Individual update buttons with two confirmation popups each do not
    // scale. With 21
    // machines that was 42 clicks for one intention. This is that intention: the
    // fleet moves to the version the user published, asked once.
    function outdatedBastions() {
        return state.bastions.filter(g => isOutdated(g) && !state.updateAsked.has(String(g.id)));
    }

    function renderUpdateAll() {
        const btn = $('gw-update-all');
        if (!btn) return;
        // `undefined` means the latest-version lookup FAILED (see state.latestVersion
        // at the top of this file); `null` means it has not run yet. Treating a failed
        // lookup as "nothing is out of date" is how this app's whole fleet-update path
        // silently vanished for two days: Cloudflare began
        // refusing the host's manifest fetch, isOutdated() went false for every
        // machine, this button hid itself, and nothing on screen said why. An update
        // path that disappears without a word is worse than one that says it is
        // broken — so say it.
        if (state.latestVersion === undefined) {
            btn.hidden = true;
            // Precise about what still works. From THIS app nothing can be asked while
            // the version is unknown — the per-card Update is hidden and updateBastion
            // returns early — so "they still update when asked" would have been half
            // true. A version can still be sent from the host.
            setGwNotice('Cannot tell which bastion version is current — the check for the '
                + 'latest published version failed, so no machine can be flagged out of date '
                + 'and this app cannot offer an update. Your machines are unaffected: they '
                + 'keep running, and your host can still send one a version directly.', true);
            return;
        }
        // We CAN tell now — so retract the notice that says we cannot. Error notices
        // never auto-hide (setGwNotice), and before the version was re-read on every
        // load this transition could not happen inside a tab, so nothing cleared it.
        // Now it can: a failed lookup followed by a good one would leave "Cannot tell
        // which bastion version is current" sitting above a working Update button.
        clearGwNotice(true);
        const due = outdatedBastions();
        btn.hidden = due.length === 0;
        btn.textContent = due.length === 1
            ? 'Update 1 machine to ' + state.latestVersion
            : 'Update ' + due.length + ' machines to ' + state.latestVersion;
    }

    async function updateAllBastions() {
        const due = outdatedBastions();
        if (!state.latestVersion || !due.length) return;
        const names = due.map(g => '  · ' + (g.hostname || g.name || ('bastion ' + g.id))
            + (g.bastion_version ? ' (' + g.bastion_version + ')' : '')).join('\n');
        // One confirmation, and it lists exactly which machines it covers — the
        // per-machine popups at least told you what you were touching, so naming
        // them here is what makes a single click honest rather than blind.
        if (!confirm('Update ' + due.length + ' machine' + (due.length === 1 ? '' : 's')
            + ' to ' + state.latestVersion + '?\n\n' + names
            + '\n\nEach one pulls the published binary, checks its hash, and swaps itself on its'
            + ' next check-in. Nothing is sent to them now, and a machine that is offline takes it'
            + ' when it comes back.')) return;
        const btn = $('gw-update-all');
        if (btn) { btn.disabled = true; btn.textContent = 'Asking…'; }
        let res = null;
        try {
            res = await fetchJSON('/api/network/bastions/update', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ version: state.latestVersion, token_ids: due.map(g => g.id) }),
            });
            (res.directed || []).forEach(d => state.updateAsked.add(String(d.token_id)));
        } catch (e) {
            setGwNotice('Could not ask them to update. ' + e.message, true);
        } finally {
            if (btn) btn.disabled = false;
            // Reloaded, not only redrawn: each card, and the count below, should say
            // at once which machines wait on an approval.
            await loadBastions();
        }
        if (!res) return;
        const n = (res.directed || []).length;
        const un = (res.unreachable || []).length;
        const asked = new Set((res.directed || []).map(d => String(d.token_id)));
        const waiting = state.bastions.filter(g => asked.has(String(g.id)) && g.update_waits_on === 'approval').length;
        // Say what did NOT happen too. A machine enrolled but never connected has
        // no heartbeat row to carry the directive, so counting it here would be
        // the same lie the per-machine alert used to tell.
        setGwNotice(n + ' machine' + (n === 1 ? '' : 's') + ' will update to '
            + state.latestVersion + (waiting ? '' : ' on the next check-in') + '.'
            + (waiting ? ' ' + waiting + ' of them ' + (waiting === 1 ? 'waits' : 'wait')
                + ' for your approval on your approval device, where one "Approve all shown" covers them all.' : '')
            + ((res.skipped || []).length ? ' ' + res.skipped.length + ' already had it.' : '')
            + (un ? ' ' + un + ' has never checked in — it gets the current version when it does.' : ''),
            false);
    }

    // ── automatic updates ────────────────────────────────────
    // The Host moves its own bastions to each published version on a standing
    // loop, behind a canary ring (server/network/bastion_autoupdate.py). This
    // strip is the switch and one sentence about the newest version.
    function autoUpdateLine(s) {
        if (!s) return 'Cannot tell whether automatic updates are on right now.';
        if (!s.enabled) return 'Off. Your machines move to a new version only when you press Update.';
        const l = s.latest;
        if (!l || !l.version) return 'On. Your machines move to each newly published version on their own.';
        const v = l.version;
        if (l.ring === 0) {
            if (l.stage === 'canary') return 'On. Your machines are the canary for ' + v
                + ': they take it first, and customers follow once it has run ' + s.soak_hours
                + ' hours on them without a machine going silent.';
            if (l.stage === 'general') return 'On. ' + v + ' passed the canary on your machines'
                + (l.reason ? ' (' + l.reason + ')' : '') + '. Customers are moving to it.';
            if (l.stage === 'held') return 'On. ' + v + ' failed the canary on your machines and is held back from customers'
                + (l.reason ? ': ' + l.reason : '') + '. Publishing a fixed version starts a new canary.';
        } else {
            if (l.stage === 'general') return 'On. ' + v + ' is released, and your machines move to it on their own.';
            if (l.stage === 'held') return 'On. ' + v + ' was held back after it failed on the platform\'s own machines. '
                + 'Yours stay where they are until a fixed version ships.';
            if (l.stage === 'canary') return 'On. ' + v
                + ' is published and is running on the platform\'s own machines first. Yours move once it passes there.';
            if (l.stage === 'awaiting_canary') return 'On. ' + v
                + ' is published. The platform\'s own machines take it first, and yours move once it passes there.';
        }
        return 'On. Your machines move to each newly published version on their own.';
    }
    async function loadAutoUpdate() {
        try { state.autoUpdate = await fetchJSON('/api/network/bastions/auto-update'); }
        catch (e) { state.autoUpdate = null; }
        renderAutoUpdate();
    }
    function renderAutoUpdate() {
        const s = state.autoUpdate;
        const line = $('autoupdate-line');
        if (line) line.textContent = autoUpdateLine(s);
        const btn = $('autoupdate-toggle');
        if (!btn) return;
        btn.hidden = !s;
        if (s) btn.textContent = s.enabled ? 'Turn off' : 'Turn on';
    }
    async function toggleAutoUpdate() {
        const s = state.autoUpdate;
        if (!s) return;
        const next = !s.enabled;
        // Off on the canary Host is not a private choice: no customer moves
        // to a new version until this Host has soaked it.
        if (!next && s.latest && s.latest.ring === 0 && !confirm('Your Host is the canary. While automatic '
            + 'updates are off here, no new version reaches your machines on its own, and no customer\'s '
            + 'bastions move to one either. Turn off?')) return;
        try {
            state.autoUpdate = await fetchJSON('/api/network/bastions/auto-update', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: next }),
            });
        } catch (e) {
            setGwNotice('Could not change automatic updates. ' + e.message, true);
        }
        renderAutoUpdate();
    }

    // Read-only egress status. Egress is the USER's plane, governed host-local — you propose +
    // sign rules from your agent and the in-pod gate enforces YOUR signed policy. This surfaces
    // the CURRENT shape for visibility only — NO controls here, and the platform never sees a
    // destination. (The 06-19 removal was of a platform-ENFORCED allowlist; a read-only mirror
    // of your own host-local policy is not that.)
    async function loadEgressStatus() {
        let eg = null;
        try { eg = await fetchJSON('/api/network/nac/egress'); } catch (e) { eg = null; }
        renderEgressStatus(eg);
    }
    function renderEgressStatus(eg) {
        const stats = $('egress-stats');
        if (!stats) return;
        const online = (state.bastions || []).filter(g => g.online).length;
        const approved = ((eg && eg.rules) || []).filter(r => r.status === 'approved');
        const posture = !eg ? '—' : (approved.length ? 'limited' : 'open');
        const cards = [
            { v: posture, l: 'reach' },
            { v: online, l: online === 1 ? 'exit bastion' : 'exit bastions' },
            { v: approved.length, l: approved.length === 1 ? 'your limit' : 'your limits' },
        ];
        stats.innerHTML = cards.map(s =>
            `<div class="net-stat"><span class="net-stat-val">${esc(s.v)}</span><span class="net-stat-lbl label-sys">${esc(s.l)}</span></div>`
        ).join('');
    }

    // ════════════════════ NAC (host-local network access control) ════════════════════
    // Approve-only: your agent PROPOSES rules/grants (inert drafts); you APPROVE them here.
    // The widen→step-up / tighten→free rule is the spine — anything that grants reach (an
    // allow, a management grant, removing a deny) asks for your authenticator code; declining,
    // denying, or revoking an allow is free. Posture + audit are READ-ONLY. Every block degrades
    // to an honest empty state; a missing table or unenrolled second factor never throws.
    async function loadNAC() {
        // Skeletons on first paint of each driven block.
        if (!state.loaded.nac) {
            $('nac-posture-grid').innerHTML = skeletonRows(2);
            $('nac-ingress-list').innerHTML = skeletonRows(1);
            $('nac-egress-list').innerHTML = skeletonRows(2);
            $('nac-grants-list').innerHTML = skeletonRows(1);
            $('nac-audit-list').innerHTML = skeletonRows(2);
        }
        // Fire all five reads in parallel; each degrades independently so one missing
        // table never blanks the others.
        const [pol, eg, gr, ep, au, cn] = await Promise.all([
            fetchJSON('/api/network/nac/policy').catch(e => ({ _err: e.message })),
            fetchJSON('/api/network/nac/egress').catch(e => ({ _err: e.message })),
            fetchJSON('/api/network/nac/mgmt/grants').catch(e => ({ _err: e.message })),
            fetchJSON('/api/network/nac/endpoints').catch(e => ({ _err: e.message })),
            fetchJSON('/api/network/nac/mgmt/audit').catch(e => ({ _err: e.message })),
            fetchJSON('/api/network/nac/connector').catch(e => ({ _err: e.message })),
        ]);
        const n = state.nac;
        n.policy = (pol && pol.policy) || [];        // the INGRESS lane
        n.profiles = (pol && pol.profiles) || [];
        n.engine = (pol && pol.engine) || null;      // the running engine's state (ingressLocked)
        n.egress = (eg && eg.rules) || [];
        n.subjects = (eg && eg.subjects) || [];   // v125: what the subject picker offers
        n.grants = (gr && gr.grants) || [];
        n.endpoints = (ep && ep.endpoints) || [];
        n.suspended = (ep && ep.suspended) || [];
        n.quarantined = (ep && ep.quarantined) || [];   // C1: whole-mesh cut (peer dropped)
        n.audit = (au && au.audit) || [];
        // Enrollment is reported by every policy-family read; trust whichever answered.
        n.stepupEnrolled = !!((pol && pol.stepup_enrolled) || (eg && eg.stepup_enrolled) || (gr && gr.stepup_enrolled));
        // Messaging connectors (Ingress lane): only the ones still in play are drawn.
        n.connectors = ((cn && cn.connectors) || []).filter(c => c && c.status !== 'revoked');
        n.pending = n.policy.filter(isDraft).length + n.egress.filter(isDraft).length + n.grants.filter(isDraft).length
            + n.connectors.filter(connectorWaiting).length;
        state.loaded.nac = true;
        await loadConnectorCodes();

        renderNacStepupChip();
        renderNacPending(pol, eg, gr);
        renderNacPosture(ep);
        renderNacIngress(pol);
        renderNacConnectors(cn);
        renderNacEgress(eg);
        renderNacGrants(gr);
        renderNacAudit(au);
        applyLaneDefaults();   // open lanes that have pending drafts (unless the user overrode)
        updateNacBadge();
    }
    function isDraft(r) { return r && r.status === 'draft'; }
    // A policy (ingress) row's enforcement — joined from its profile (the read returns profile_id).
    function policyEnforcement(r) {
        if (r && r.enforcement) return r.enforcement;  // tolerate a future flattened shape
        const prof = (state.nac.profiles || []).find(p => p.id === (r && r.profile_id));
        return (prof && prof.enforcement) || 'allow';
    }
    // What an ingress rule admits, read the way the engine reads it
    // (nac_controller.permits_inbound): a quarantine admits nothing, and an allow
    // admits exactly the inbound lanes its permits name, so an allow naming none
    // admits nothing at all. /nac/policy sends each row's permits (from the signed
    // terms once approved); the profile is the fallback for an older read.
    function policyPermits(r) {
        let p = r && r.permits;
        if (!Array.isArray(p)) {
            const prof = (state.nac.profiles || []).find(x => x.id === (r && r.profile_id));
            p = prof && prof.permits;
            if (typeof p === 'string') { try { p = JSON.parse(p); } catch (e) { p = []; } }
        }
        return Array.isArray(p) ? p : [];
    }
    function policyLanes(r) {
        if (policyEnforcement(r) !== 'allow') return [];
        const out = [];
        policyPermits(r).forEach(p => {
            if (!p || typeof p !== 'object' || (p.direction || 'inbound') !== 'inbound') return;
            if (p.lane == null || p.lane === 'any') out.push('every lane');
            else if (p.lane === 'mcp') out.push('paired machines (mcp)');
        });
        return out;
    }
    // Removing an APPROVED rule that keeps machines out can let them back in — a widen, so it asks
    // for the code (nac_commit.revoke_policy), like removing an egress deny. The server says
    // which rows those are (/nac/policy → revoke_needs_code), computed by the same check the
    // revoke runs — it also catches a rule whose signature no longer verifies, which looks
    // intact from its terms. An older read without the field falls back to the terms: a
    // quarantine, or an allow that admits no lane (policyLanes is empty for both).
    function policyRevokeWidens(r) {
        if (!r || r.status === 'draft') return false;
        if (typeof r.revoke_needs_code === 'boolean') return r.revoke_needs_code;
        return policyLanes(r).length === 0;
    }
    // A removal sent without a code that the server refused for want of one: the rule was
    // approved while it was being declined, or it changed since the card was drawn. Offer the
    // code prompt and try again, instead of leaving only an error.
    function stepupNowRequired(e, codeOrNull) {
        return !codeOrNull && !!e && e.status === 403 && /step-up code/i.test(e.message || '');
    }
    // While any approved rule fails the engine's integrity check, the engine refuses every
    // machine before it reads a single rule (nac_controller.decide). /nac/policy reports that
    // from the running engine: engine.tamper, and each approved row's own verdict as
    // integrity — 'failed' only for the rule the engine rejected. 'unchecked' (nothing could
    // check it, e.g. the key was unreadable) is not a failure and is not marked as one.
    function ingressLocked() {
        return !!(state.nac.engine && state.nac.engine.tamper === true);
    }
    function policyFailed(r) {
        return !!r && r.status !== 'draft' && r.integrity === 'failed';
    }
    // Why Revoke asks for the code when the server says only the unreadable key makes it ask
    // (revoke_code_reason 'key_unreadable'): not a claim that the rule keeps anything out.
    const KEY_UNREADABLE_NOTE = "your Host can't read its signing key right now, so removing any signed rule asks for your code";
    function policyFailedWhy(r) {
        return r && r.integrity_reason === 'profile'
            ? 'the profile it points at is gone'
            : 'its signature does not verify';
    }
    function admitsSummary(r) {
        if (policyFailed(r)) return 'admits nothing: ' + policyFailedWhy(r);
        const lanes = policyLanes(r);
        if (lanes.length && r.status !== 'draft' && ingressLocked()) {
            return 'admits nothing while machines are locked out (otherwise ' + lanes.join(', ') + ')';
        }
        if (lanes.length) return 'admits ' + lanes.join(', ');
        return policyEnforcement(r) === 'allow'
            ? 'admits nothing, so every machine it matches is locked out'
            : 'admits nothing';
    }
    // The Ingress lane's banner while the engine is locked. Says what is happening, which rule
    // is why, and the way out the code has: Revoke the rule (asking for the code exactly when
    // the server will, revoke_needs_code), then have the agent draft it again and approve the
    // new draft — an approved rule cannot be approved a second time, and a draft may not reuse
    // the name of an approved rule until that rule is gone.
    function ingressLockBanner() {
        if (!ingressLocked()) return '';
        const failed = (state.nac.policy || []).filter(policyFailed);
        // "with a device fingerprint": _nac_gate never asks the engine about a token without
        // one, so the lock does not reach it (its Posture card says the rules skip it).
        const lead = '<strong>Every paired machine with a device fingerprint is locked out.</strong> ';
        if (!failed.length) {
            return `<div class="status-banner error nac-lock-banner" role="alert">${lead}`
                + 'An approved rule failed your Host\'s integrity check, so your Host is refusing '
                + 'those machines. The rule is not in this list any more; reload to check again.</div>';
        }
        const one = failed.length === 1;
        const allSig = failed.every(r => r.integrity_reason !== 'profile');
        const allProf = failed.every(r => r.integrity_reason === 'profile');
        const names = failed.map(r => '“' + esc(r.name || ('rule ' + r.id)) + '”').join(', ');
        let what;
        if (one) what = `The approved rule ${names} ` + (allProf ? 'points at a profile that no longer exists' : 'failed its signature check');
        else what = `The approved rules ${names} ` + (allSig ? 'failed their signature check' : (allProf ? 'point at profiles that no longer exist' : 'failed your Host\'s integrity check'));
        const code = failed.every(r => r.revoke_needs_code === true)
            ? (one ? ' (it asks for your code)' : ' (each asks for your code)') : '';
        return `<div class="status-banner error nac-lock-banner" role="alert">${lead}`
            + `${what}, so your Host refuses those machines until ${one ? 'it is' : 'they are'} removed. `
            + `Revoke ${one ? 'it' : 'them'} below${code}. If you still want ${one ? 'the rule' : 'one of them'}, `
            + 'then ask your agent to draft it again and approve the new draft.</div>';
    }

    // ── MESSAGING CONNECTORS (Ingress lane) ────────────────────────────────────
    // A connector lets ONE account on Telegram, Discord or Slack start turns in your chat,
    // through ONE connector app (for Telegram, the store's Telegram app). Your Host starts a
    // pairing; its one-time code is shown here and on its card in the person's chat (the
    // Host never sees it), you send it to the bot from the
    // account you want to let in, and then you let that account in with your authenticator
    // code. Declining and revoking are free. The code is fetched from a route that answers
    // only your own windows (/nac/connector/<id>/code), and the countdown runs here.
    function connectorWaiting(c) { return !!c && (c.status === 'pairing' || c.status === 'awaiting_approval'); }
    function connectorLabel(c) { return (c && (c.platform_label || c.platform)) || 'messaging'; }
    // The Keys entries the connector sends to the platform (the Host named them
    // when it proposed the pairing), so the person approves the key as well as
    // the account. Same words as the approval ticket (nac_connector.approval_words).
    function connectorKeys(c) {
        const names = [c.token_name, c.app_token_name].filter(n => typeof n === 'string' && n);
        return names.length ? names.map(n => `“${esc(n)}”`).join(', ')
                            : 'no key named yet (the connector cannot call the platform)';
    }
    function connectorAccount(c) {
        const who = c.display_name ? `“${c.display_name}” (id ${c.account_id})` : `id ${c.account_id}`;
        return `${connectorLabel(c)} account ${who}`;
    }
    // "9:41 left" from the deadline; "expired" at zero.
    function connectorLeft(until, now) {
        const s = Math.max(0, Math.floor(((until || 0) - (now == null ? Date.now() : now)) / 1000));
        if (!s) return 'expired';
        return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')} left`;
    }
    async function loadConnectorCodes() {
        const codes = state.nac.connectorCodes = state.nac.connectorCodes || {};
        const open = (state.nac.connectors || []).filter(c => c.status === 'pairing');
        const keep = new Set(open.map(c => String(c.id)));
        Object.keys(codes).forEach(k => { if (!keep.has(k)) delete codes[k]; });
        await Promise.all(open.map(async c => {
            if (codes[c.id] && codes[c.id].code) return;
            try {
                const r = await fetchJSON(`/api/network/nac/connector/${encodeURIComponent(c.id)}/code`);
                codes[c.id] = { code: r.code, until: Date.now() + 1000 * Number(r.seconds_left || 0) };
            } catch (e) {
                codes[c.id] = { err: e.message };
            }
        }));
    }
    function connectorCard(c) {
        const app = `<code>${esc(c.app_id)}</code>`;
        if (c.status === 'pairing') {
            const held = (state.nac.connectorCodes || {})[c.id] || {};
            const capNote = c.capability === 'live' ? ''
                : '<div class="net-card-sub">Your connector is restarting to receive its key. The code works once it is back.</div>';
            const codeLine = held.code
                ? `<div class="nac-connector-code"><code class="nac-pair-code">${esc(held.code)}</code>
                       <span class="label-sys" data-connector-left="${attr(c.id)}" data-until="${attr(held.until)}">${esc(connectorLeft(held.until))}</span></div>`
                : `<div class="net-card-sub">${esc(held.err || 'Loading the code…')}</div>`;
            return `
                <div class="net-card nac-draft-row nac-connector" data-connector="${attr(c.id)}">
                    <div class="net-card-main">
                        <div class="net-card-title">
                            <span class="net-card-name">Pair a ${esc(connectorLabel(c))} account</span>
                            <span class="cseries-pill warn">pairing</span>
                        </div>
                        <div class="net-card-sub">connector app ${app} · from the account you want to let in, send this code to your bot in a direct message</div>
                        ${codeLine}${capNote}
                    </div>
                    <div class="net-card-actions">
                        <button class="cseries-btn" data-nac-decline="connector:${attr(c.id)}">Decline</button>
                    </div>
                </div>`;
        }
        if (c.status === 'awaiting_approval') {
            return `
                <div class="net-card nac-draft-row nac-connector" data-connector="${attr(c.id)}">
                    <div class="net-card-main">
                        <div class="net-card-title">
                            <span class="net-card-name">Let a ${esc(connectorLabel(c))} account in</span>
                            <span class="cseries-pill warn">awaiting you</span>
                        </div>
                        <div class="net-card-sub">Account, as the connector reports it: ${esc(connectorAccount(c))} · connector app ${app}</div>
                        <div class="net-card-sub">Key it sends to ${esc(connectorLabel(c))}, from your Keys: ${connectorKeys(c)}</div>
                        <div class="nac-prop-why">Messages from this account will start turns in your chat, with your full chat permissions. It cannot approve anything. Letting it in asks for your authenticator code.</div>
                    </div>
                    <div class="net-card-actions">
                        <button class="cseries-btn" data-nac-decline="connector:${attr(c.id)}">Don't pair</button>
                        <button class="cseries-btn primary" data-nac-approve="connector:${attr(c.id)}" data-widen="1">Let this account in</button>
                    </div>
                </div>`;
        }
        return `
            <div class="net-card live nac-connector" data-connector="${attr(c.id)}">
                <div class="net-card-main">
                    <div class="net-card-title">
                        <span class="net-card-name">${esc(connectorAccount(c))}</span>
                        <span class="cseries-pill allow">can message your Host</span>
                    </div>
                    <div class="net-card-sub">connector app ${app} · signed · approved ${esc(fmtRel(c.approved_at))}${c.stepup_at ? ' with step-up' : ''}</div>
                </div>
                <div class="net-card-actions">
                    <button class="cseries-btn danger" data-nac-revoke="connector:${attr(c.id)}" data-widen="0">Revoke</button>
                </div>
            </div>`;
    }
    function renderNacConnectors(cn) {
        const list = $('nac-connector-list');
        if (!list) return;
        if (cn && cn._err) { list.innerHTML = errBox(cn._err); return; }
        const rows = (state.nac.connectors || []).slice()
            .sort((a, b) => (connectorWaiting(a) ? 0 : 1) - (connectorWaiting(b) ? 0 : 1) || (b.id - a.id));
        list.innerHTML = rows.length ? rows.map(connectorCard).join('')
            : empty('No messaging connectors',
                'For Telegram, Slack or Discord, install that app from the App Store, or ask your Host to. When your Host starts the pairing, it appears here with a code to send from your account.');
        startConnectorTicker();
    }
    // One ticker while a pairing code is on screen; it stops by itself when none is.
    function startConnectorTicker() {
        const anyCode = Object.values(state.nac.connectorCodes || {}).some(x => x && x.code);
        if (!anyCode || state.nac.connectorTicker) return;
        state.nac.connectorTicker = setInterval(() => {
            const els = (typeof document.querySelectorAll === 'function')
                ? document.querySelectorAll('[data-connector-left]') : [];
            let live = 0;
            els.forEach(el => {
                const t = connectorLeft(Number(el.getAttribute('data-until')));
                el.textContent = t;
                if (t !== 'expired') live++;
            });
            if (!live) { clearInterval(state.nac.connectorTicker); state.nac.connectorTicker = null; }
        }, 1000);
    }

    // Second-factor chip in the sovereignty banner. Click to enroll when not yet set.
    function renderNacStepupChip() {
        const chip = $('nac-stepup-chip');
        if (!chip) return;
        if (state.nac.stepupEnrolled) {
            chip.innerHTML = '<span class="nac-chip-dot"></span><span class="label-sys" style="color:var(--cseries-ash)">Second factor</span>&nbsp;<b>Enrolled</b>';
            chip.style.cursor = 'default';
            chip.removeAttribute('data-nac-enroll');
        } else {
            chip.innerHTML = '<span class="nac-chip-dot" style="background:var(--cseries-gold)"></span><span class="label-sys" style="color:var(--cseries-ash)">Second factor</span>&nbsp;<b style="color:var(--cseries-gold)">Enroll →</b>';
            chip.style.cursor = 'pointer';
            chip.setAttribute('data-nac-enroll', '1');
        }
    }

    // ── PENDING PROPOSALS — drafts across egress, policy, and grants, pinned + gold. ──
    function renderNacPending(pol, eg, gr) {
        const block = $('nac-pending-block');
        const list = $('nac-pending-list');
        if (!block || !list) return;
        const props = [];
        (state.nac.egress || []).filter(isDraft).forEach(r => {
            // The exit and the subject MUST appear here. This is the surface where the
            // authenticator code is typed, and a code typed over a description that
            // omits what is being granted proves nothing: without this, a rule pinning
            // traffic to one of your machines would read only as "reach api.x.ai".
            const subj = subjectLabel(r.subject);
            const ex = exitLabel(r.exit);
            const pinned = isPinnedExit(r.exit);
            // A rule drafted for an MCP server: approving it connects the server,
            // so the server, its URL and the key it uses are named here, where
            // the code is typed (server/mcp_connections.py; nac_commit refuses
            // this approval anywhere else).
            const mcp = r.mcp || null;
            props.push({
                kind: 'egress', id: r.id, name: mcp ? 'MCP server' : 'Egress rule',
                pillCls: r.action === 'allow' ? 'allow' : 'deny', pillLbl: r.action,
                widening: r.action === 'allow' || pinned,
                rule: `${subj} · ${r.pattern} → ${String(r.action || '').toUpperCase()}${pinned ? ' · out through ' + ex : ''}${mcp ? ' · MCP server ' + mcp.name : ''}`,
                why: mcp
                    ? `Your agent wants to connect the MCP server <strong>${esc(mcp.name)}</strong> at <code>${esc(mcp.url)}</code>${mcp.key ? `, using your key <strong>${esc(mcp.key)}</strong> (${mcp.auth && mcp.auth !== 'bearer' ? 'in the ' + esc(mcp.auth) + ' header' : 'as a bearer token'})` : ', with no key'}. Approving signs this rule and the connection with your Host's key: your Host then reaches <code>${esc(r.pattern)}</code> through the egress gate, and only for this server. Its tools run only after your Host writes a skill for them, and every call says what it is for. Removing the rule disconnects the server.`
                    : pinned
                    ? `Your agent wants <strong>${esc(subj)}</strong> to reach <code>${esc(r.pattern)}</code> <strong>through ${esc(ex)}</strong> — that traffic would leave from that machine's own internet connection, and the sites it reaches would see that machine's address instead of the platform's. That is why it asks for your authenticator code. Once approved the terms are signed with your Host's key and the egress proxy enforces them within seconds.`
                    : (r.action === 'allow'
                        ? `Your agent wants outbound reach to <code>${esc(r.pattern)}</code>${r.subject && r.subject !== '*' ? ' for <strong>' + esc(subj) + '</strong>' : ''}. Approving this widens your reach, so it asks for your authenticator code. Once approved the terms are signed with your Host's key and the egress proxy enforces them within seconds.`
                        : `Your agent proposes denying <code>${esc(r.pattern)}</code>${r.subject && r.subject !== '*' ? ' for <strong>' + esc(subj) + '</strong>' : ''}. This only tightens your reach — no code needed.`),
            });
        });
        (state.nac.policy || []).filter(isDraft).forEach(r => {
            const enf = policyEnforcement(r);
            const quarantine = enf === 'quarantine';
            const lanes = policyLanes(r);
            props.push({
                kind: 'policy', id: r.id, name: 'Ingress rule',
                pillCls: quarantine ? 'deny' : 'allow',
                pillLbl: enf,
                widening: !quarantine,
                rule: `${r.name || ('rule ' + r.id)} · ${enf} · ${admitsSummary(r)}`,
                why: quarantine
                    ? 'Your agent proposes an inbound <strong>quarantine</strong> rule. Tightening is free — no code needed.'
                    : (lanes.length
                        ? `Your agent proposes an inbound <strong>allow</strong> rule that admits <strong>${esc(lanes.join(', '))}</strong> from the machines it matches, and nothing else. Approving widens your reach, so it asks for your authenticator code.`
                        : 'Your agent proposes an inbound <strong>allow</strong> rule that names no lane, so it admits nothing and would lock out every machine it matches. It cannot be approved: decline it and ask your agent to draft it again with its permits.'),
            });
        });
        (state.nac.grants || []).filter(isDraft).forEach(r => {
            const ops = Array.isArray(r.ops) ? r.ops : [];
            const deny = Array.isArray(r.deny) ? r.deny : [];
            const widening = ops.length > 0;  // a deny-only grant tightens — free
            props.push({
                kind: 'grant', id: r.id, name: 'Management grant',
                pillCls: widening ? 'info' : 'deny', pillLbl: widening ? 'grant' : 'deny only', widening,
                rule: grantRule(r, false),  // the card escapes p.rule itself
                why: grantWords(r) + (widening
                    ? ` Your agent asks for rights to run [ ${ops.map(esc).join(', ')} ] on <strong>${esc(r.server)}</strong>${deny.length ? `, and never [ ${deny.map(esc).join(', ')} ] — a deny wins over any allow` : ''}. Management is default-deny, so without this grant it has none. Every command run under a grant is audited. Granting rights is a widen — it asks for your code.`
                    : ` Your agent proposes forbidding [ ${deny.map(esc).join(', ')} ] on <strong>${esc(r.server)}</strong>, whatever other grants allow. This only tightens — no code needed.`),
            });
        });
        if (!props.length) { block.hidden = true; return; }
        block.hidden = false;
        $('nac-pending-count').textContent = props.length + ' pending';
        list.innerHTML = props.map(p => `
            <div class="nac-prop">
                <div class="nac-prop-main">
                    <div class="nac-prop-title">
                        <span class="nac-prop-name">${esc(p.name)}</span>
                        <span class="cseries-pill ${p.pillCls}">${esc(p.pillLbl)}</span>
                        ${p.widening ? '<span class="cseries-pill warn">widening — needs your code</span>' : '<span class="cseries-pill">tightening — free</span>'}
                    </div>
                    <code class="nac-prop-rule">${esc(p.rule)}</code>
                    <div class="nac-prop-why">${p.why}</div>
                </div>
                <div class="nac-prop-actions">
                    <button class="cseries-btn" data-nac-decline="${attr(p.kind)}:${attr(p.id)}">Decline</button>
                    <button class="cseries-btn primary" data-nac-approve="${attr(p.kind)}:${attr(p.id)}" data-widen="${p.widening ? '1' : '0'}">Approve</button>
                </div>
            </div>`).join('');
    }

    // ── POSTURE — per-bastion, READ-ONLY (the engine decides; the user never sets it). ──
    // What the Host does with this machine now, from the running engine's own decision
    // (/nac/endpoints -> decision, nac_commit.endpoint_decisions). The card used to print
    // nac_endpoints.assigned_profile, a column nothing writes, so every machine read
    // "default-preserve" — under a quarantine rule, and while every machine was refused.
    function postureAccess(e) {
        const d = e && e.decision;
        if (!d) return { profile: 'not checked', reach: 'not checked', refused: '' };
        if (d.expired) {
            return { profile: '—', reach: 'refused',
                     refused: 'Your Host refuses it: its pairing token has expired (it lapses 7 days after the last check-in), so it is turned away before any rule is checked.' };
        }
        if (!d.governed) {
            return { profile: 'none (no device fingerprint)',
                     reach: 'admitted; your Ingress rules skip it', refused: '' };
        }
        if (d.error) {
            return { profile: 'could not be applied', reach: 'admitted',
                     refused: 'Your Host could not apply its Ingress rules to this machine (an approved rule is malformed), and it lets a machine in when that happens. Check the rules under Ingress.' };
        }
        const profile = d.locked ? 'integrity lock'
            : (d.profile || 'none (admitted by default)');
        if (d.allowed) return { profile, reach: 'admitted', refused: '' };
        let refused;
        if (d.locked) {
            refused = 'Your Host refuses it: an approved Ingress rule failed its integrity check, '
                + 'so every machine your Ingress rules govern is refused until that rule is removed (see Ingress).';
        } else {
            refused = 'Your Host refuses it: ' + (d.default
                ? `no rule matches it, and the default profile “${d.profile}” does not admit it.`
                : `the rule “${d.profile}” does not admit it.`);
        }
        return { profile, reach: 'refused', refused };
    }
    function renderNacPosture(ep) {
        const grid = $('nac-posture-grid');
        const status = $('nac-posture-status');
        if (!grid) return;
        if (ep && ep._err) { grid.innerHTML = errBox(ep._err); if (status) status.textContent = ''; return; }
        const eps = state.nac.endpoints || [];
        if (status) status.textContent = eps.length ? `re-decided on every check-in` : '';
        // The collapsed head carries the count, like every other lane, so the
        // number is visible without opening the section.
        const cnt = $('nac-posture-count');
        if (cnt) cnt.textContent = String(eps.length);
        if (!eps.length) {
            grid.innerHTML = empty('No posture yet',
                'When a bastion checks in, its posture appears here — healthy, degraded, or quarantined — with the access profile the engine trusts it with at that posture.');
            return;
        }
        const suspended = state.nac.suspended || [];
        const quarantined = state.nac.quarantined || [];
        grid.innerHTML = eps.map(e => {
            const server = e.hostname || '';
            const isQtn = server && quarantined.indexOf(server) !== -1;   // C1: harder cut, takes priority
            const isSusp = server && suspended.indexOf(server) !== -1;
            const posture = e.posture || 'unknown';
            const cardCls = isQtn ? 'quarantined' : (isSusp ? 'suspended'
                : (posture === 'healthy' ? 'healthy' : (posture === 'degraded' ? 'degraded' : 'quarantined')));
            const dotCls = posture === 'healthy' ? 'live' : (posture === 'degraded' ? 'stale' : '');
            const pillCls = posture === 'healthy' ? 'allow' : (posture === 'degraded' ? 'warn' : 'deny');
            const pillLbl = posture === 'healthy' ? 'healthy' : (posture === 'degraded' ? 'degraded' : (posture === 'unknown' ? 'unknown' : 'quarantined'));
            // A manual quarantine cuts the machine off at the mesh (its peer is dropped) and at
            // the management gates; the MCP gate's answer is not the whole story then, so the
            // card says cut off rather than repeat an "admitted" the note beside it contradicts.
            const access = isQtn ? { profile: postureAccess(e).profile, reach: 'cut off (quarantined)', refused: '' }
                : postureAccess(e);
            const why = e.last_decision || (posture === 'healthy'
                ? 'All checks current.'
                : 'No recent decision recorded.');
            const alertWhy = posture !== 'healthy';
            // Two reversible levers (priority: quarantine over suspend). Quarantine = a whole-mesh
            // cut (drops the peer → tunnel dead); Suspend = mgmt/harness lanes only. Both TIGHTEN
            // (free); Release/Resume WIDEN → step-up. Only on a known server name.
            let holdControl = '';
            if (server) {
                if (isQtn) {
                    holdControl = `<div class="nac-posture-actions"><button class="cseries-btn primary" data-nac-unquarantine="${attr(server)}">Release</button></div>`;
                } else if (isSusp) {
                    holdControl = `<div class="nac-posture-actions">`
                        + `<button class="cseries-btn primary" data-nac-resume="${attr(server)}">Resume</button>`
                        + `<button class="cseries-btn danger" data-nac-quarantine="${attr(server)}">Quarantine</button></div>`;
                } else {
                    holdControl = `<div class="nac-posture-actions">`
                        + `<button class="cseries-btn" data-nac-suspend="${attr(server)}">Suspend</button>`
                        + `<button class="cseries-btn danger" data-nac-quarantine="${attr(server)}">Quarantine</button></div>`;
                }
            }
            const topPill = isQtn ? '<span class="cseries-pill deny">quarantined</span>'
                : (isSusp ? '<span class="cseries-pill suspend">suspended</span>'
                   : `<span class="cseries-pill ${pillCls}">${esc(pillLbl)}</span>`);
            let note;
            if (isQtn) {
                note = `<div class="nac-suspend-note">Quarantined — the whole mesh is cut: its concentrator peer is dropped (tunnel dead) and management + harness are denied. The bearer is kept; release restores the box with no re-enrol (it asks for your code).</div>`;
            } else if (isSusp) {
                note = `<div class="nac-suspend-note">Suspended — management + harness denied on this bastion even with valid grants. Its grants are kept; resume restores them.</div>`;
            } else {
                note = `<div class="nac-why ${alertWhy ? 'alert' : ''}">${esc(why)}</div>`;
            }
            const refused = access.refused
                ? `<div class="nac-suspend-note">${esc(access.refused)}</div>` : '';
            return `
                <div class="nac-posture-card ${cardCls}">
                    <div class="nac-posture-top">
                        <div class="nac-posture-host">
                            <span class="net-status-dot ${dotCls}"></span>
                            <span class="nac-host-name">${esc(e.hostname || ('bastion ' + e.token_id))}</span>
                        </div>
                        ${topPill}
                    </div>
                    <div class="nac-posture-meta">
                        <div class="nac-kv"><span class="nac-kv-k">Profile</span><span class="nac-kv-v mono">${esc(access.profile)}</span></div>
                        <div class="nac-kv"><span class="nac-kv-k">Reach in</span><span class="nac-kv-v">${esc(access.reach)}</span></div>
                        <div class="nac-kv"><span class="nac-kv-k">Seen</span><span class="nac-kv-v">${esc(fmtRel(e.posture_at))}</span></div>
                    </div>
                    ${refused}
                    ${note}
                    ${holdControl}
                </div>`;
        }).join('');
    }

    // ── INGRESS — inbound policy rows (rule → enforcement profile). Drafts pinned at top. ──
    // approve an ALLOW profile → widen → step-up; a QUARANTINE → tighten → free. The
    // approve/revoke routes are /nac/policy/{id}/{approve,revoke}; the step-up condition
    // (enforcement === 'allow') matches network.py nac_policy_approve exactly.
    function renderNacIngress(pol) {
        const list = $('nac-ingress-list');
        const count = $('nac-ingress-count');
        if (!list) return;
        if (pol && pol._err) { list.innerHTML = errBox(pol._err); if (count) count.textContent = '0'; return; }
        const rules = (state.nac.policy || []).slice().sort(laneDraftFirst);
        const drafts = rules.filter(isDraft).length;
        const locked = ingressLocked();
        if (count) {
            count.textContent = locked ? 'locked' : (drafts ? (drafts + ' pending') : String(rules.length));
            count.classList.toggle('pending', !locked && drafts > 0);
            count.classList.toggle('locked', locked);
        }
        const banner = ingressLockBanner();
        if (!rules.length) {
            list.innerHTML = banner || empty('No ingress rules',
                'No rules yet, so every machine you have paired can reach in. When your agent proposes a rule — match a bastion by posture or fingerprint, assign it an allow or quarantine profile — it appears here to approve.');
            return;
        }
        list.innerHTML = banner + rules.map(r => {
            const draft = isDraft(r);
            const failed = policyFailed(r);
            const enf = policyEnforcement(r);
            const allow = enf === 'allow';
            // A rule the engine rejected is drawn as what it is doing (locking every machine
            // out), not as the terms it claims: those terms are the part that did not verify.
            const cardCls = draft ? 'nac-draft-row' : (failed ? 'nac-failed-row' : (allow ? 'live' : 'down'));
            const enfPill = failed
                ? `<span class="cseries-pill deny">${r.integrity_reason === 'profile' ? 'profile missing' : 'signature fails'}</span>`
                : `<span class="cseries-pill ${allow ? 'allow' : 'deny'}">${esc(enf)}</span>`;
            const statePill = draft ? '<span class="cseries-pill warn">draft — awaiting you</span>' : '<span class="cseries-pill">approved</span>';
            const matchStr = nacMatchSummary(r.match);
            const sub = draft
                ? `proposed by your agent · inert until approved · ${allow ? 'widening needs your code' : 'tightening — free'}`
                : `${failed ? '' : 'signed · '}approved ${esc(fmtRel(r.approved_at))}${r.stepup_at ? ' with step-up' : ''} · priority ${esc(r.priority != null ? r.priority : '—')}`
                  + (r.revoke_code_reason === 'key_unreadable' ? ` · ${esc(KEY_UNREADABLE_NOTE)}` : '');
            // Drafts: approve (allow widens → step-up) / decline (free). Approved: revoke — an allow
            // that admits tightens (free); a rule that keeps machines out (a quarantine, or an allow
            // admitting no lane) widens (→ step-up, policyRevokeWidens).
            const actions = draft
                ? `<button class="cseries-btn" data-nac-decline="policy:${attr(r.id)}">Decline</button>
                   <button class="cseries-btn primary" data-nac-approve="policy:${attr(r.id)}" data-widen="${allow ? '1' : '0'}">Approve</button>`
                : `<button class="cseries-btn danger" data-nac-revoke="policy:${attr(r.id)}" data-widen="${policyRevokeWidens(r) ? '1' : '0'}">Revoke</button>`;
            return `
                <div class="net-card ${cardCls} nac-acl-rule">
                    <div class="net-card-main">
                        <div class="net-card-title">
                            <span class="net-card-name">${esc(r.name || ('rule ' + r.id))}</span>
                            ${enfPill}
                            ${statePill}
                        </div>
                        <div class="net-card-sub">${esc(matchStr)} · ${esc(admitsSummary(r))} · ${sub}</div>
                    </div>
                    <div class="net-card-actions">${actions}</div>
                </div>`;
        }).join('');
    }
    // A human-readable summary of a policy rule's match object (posture/min_posture/classification/fingerprint).
    function nacMatchSummary(match) {
        let m = match;
        if (typeof m === 'string') { try { m = JSON.parse(m); } catch (e) { m = {}; } }
        if (!m || typeof m !== 'object' || !Object.keys(m).length) return 'any device (catch-all)';
        const parts = [];
        if (Array.isArray(m.posture) && m.posture.length) parts.push('posture ' + m.posture.join('/'));
        if (m.min_posture) parts.push('posture ≥ ' + m.min_posture);
        if (Array.isArray(m.classification) && m.classification.length) parts.push(m.classification.join('/'));
        if (m.fingerprint) parts.push('fp ' + String(m.fingerprint).slice(0, 10) + '…');
        return parts.length ? parts.join(' · ') : 'any device (catch-all)';
    }
    // Sort comparator: drafts first (pinned to the top of their lane), then by priority/id.
    function laneDraftFirst(a, b) {
        const ad = isDraft(a) ? 0 : 1, bd = isDraft(b) ? 0 : 1;
        if (ad !== bd) return ad - bd;
        const ap = a.priority != null ? a.priority : 1e9, bp = b.priority != null ? b.priority : 1e9;
        if (ap !== bp) return ap - bp;
        return (a.id || 0) - (b.id || 0);
    }

    // ── v125: ADD RULE — the create path the NAC tab never had ───────────
    // Every rule used to begin as a host draft; the user could only approve or
    // decline. This lets the user author one directly: destination, who it applies
    // to, and which way out. It still lands as a DRAFT and goes through the same
    // approve + step-up flow, because the threat this gate exists for is a session
    // acting as the user, and the code is the only part of the flow that a
    // compromised session cannot produce.
    function egressSubjectOptions() {
        const opts = [['*', 'Everything'], ['pod', 'This host']];
        (state.nac.subjects || []).forEach(s => opts.push([s, subjectLabel(s)]));
        return opts;
    }
    function egressExitOptions() {
        const opts = [['default', 'Any connected machine, else the platform'],
                      ['platform', 'Always the platform']];
        (state.bastions || []).forEach(b => {
            // HEARTBEAT hostname only. The pin resolves on network_bastion_heartbeat
            // .hostname, and b.name is the enrolment label — offering it would let the
            // user pin a machine that has not checked in yet and can never resolve,
            // so the rule would 503 from the moment it was approved.
            const nm = b.hostname;
            if (!nm) return;
            const workers = (b.egress_workers != null) ? b.egress_workers : null;
            const note = b.online ? (workers === 0 ? ' — online, no egress workers' : '') : ' — offline';
            opts.push(['bastion:' + nm, 'Through my machine "' + nm + '"' + note]);
        });
        return opts;
    }
    function sel(id, opts) {
        return `<select id="${id}" class="cseries-input">` +
            opts.map(([v, l]) => `<option value="${attr(v)}">${esc(l)}</option>`).join('') +
            `</select>`;
    }
    function openAddEgress() {
        const m = openModal('Add an egress rule', `
            <label class="nac-field"><span>Destination</span>
              <input id="nac-eg-pattern" class="cseries-input" placeholder="api.example.com, *.example.com, or * for everything">
            </label>
            <label class="nac-field"><span>Who it applies to</span>
              ${sel('nac-eg-subject', egressSubjectOptions())}
            </label>
            <label class="nac-field"><span>Out through</span>
              ${sel('nac-eg-exit', egressExitOptions())}
            </label>
            <label class="nac-field"><span>Action</span>
              ${sel('nac-eg-action', [['allow', 'Allow — open this destination'], ['deny', 'Deny — block it']])}
            </label>
            <p class="nac-field-note" id="nac-eg-note">
              Choosing one of your own machines sends that traffic out its internet
              connection, so sites see its address instead of the platform's. That is a
              grant, so you will be asked for your authenticator code when you approve it.
            </p>
        `, [
            { label: 'Cancel', cls: 'ghost', onClick: closeModal },
            { label: 'Create draft', cls: 'primary', onClick: submitAddEgress },
        ]);
        // A deny routes nothing, so it cannot also pick an exit — mirror the server
        // rule in the form instead of letting the user compose a 400.
        const act = $('nac-eg-action'), ex = $('nac-eg-exit');
        const sync = () => {
            const deny = act.value === 'deny';
            ex.disabled = deny;
            if (deny) ex.value = 'default';
        };
        act.addEventListener('change', sync); sync();
        return m;
    }
    async function submitAddEgress() {
        const pattern = ($('nac-eg-pattern').value || '').trim();
        if (!pattern) { alert('A destination is required — a host like api.example.com, *.example.com, or *.'); return; }
        const body = {
            pattern,
            action: $('nac-eg-action').value,
            subject: $('nac-eg-subject').value,
            exit: $('nac-eg-action').value === 'deny' ? 'default' : $('nac-eg-exit').value,
        };
        try {
            await fetchJSON('/api/network/nac/egress/propose', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        } catch (e) { alert('Could not create the rule. ' + e.message); return; }
        closeModal();
        nacReload();
    }

    // ── v125: who a rule applies to, and which way out it takes ──────────
    // Plain words, because these two strings are what the user weighs when they
    // decide whether to type their code.
    function subjectLabel(subject) {
        const s = String(subject || '*');
        if (s === '*') return 'Everything';
        if (s === 'pod') return 'This host';
        if (s.startsWith('harness:')) {
            const id = s.slice(8);
            return (id === 'browser' ? 'The Browser app' : id + ' (coding engine)');
        }
        if (s.startsWith('app:')) return s.slice(4) + ' (app)';
        return s;
    }
    function isPinnedExit(exit) {
        const e = String(exit || 'default');
        return e !== 'default';
    }
    function exitLabel(exit) {
        const e = String(exit || 'default');
        if (e === 'default') return 'Any connected machine, else the platform';
        if (e === 'platform') return 'The platform';
        if (e.startsWith('bastion:')) return 'your machine "' + e.slice(8) + '"';
        return e;
    }

    // ── EGRESS — outbound ACL rows (approved + drafts surfaced inline). ──
    function renderNacEgress(eg) {
        const list = $('nac-egress-list');
        const count = $('nac-egress-count');
        if (!list) return;
        if (eg && eg._err) { list.innerHTML = errBox(eg._err); if (count) count.textContent = '0'; return; }
        const rules = (state.nac.egress || []).slice().sort(laneDraftFirst);
        const drafts = rules.filter(isDraft).length;
        if (count) { count.textContent = drafts ? (drafts + ' pending') : String(rules.length); count.classList.toggle('pending', drafts > 0); }
        if (!rules.length) {
            list.innerHTML = empty('No egress rules', 'Your reach is fully open. When your agent proposes a limit, it appears here for you to approve.');
            return;
        }
        list.innerHTML = rules.map(r => {
            const draft = isDraft(r);
            const allow = r.action === 'allow';
            const cardCls = draft ? 'nac-draft-row' : (allow ? 'live' : 'down');
            const actionPill = `<span class="cseries-pill ${allow ? 'allow' : 'deny'}">${esc(r.action)}</span>`;
            const statePill = draft ? '<span class="cseries-pill warn">draft — awaiting you</span>' : '<span class="cseries-pill">approved</span>';
            const sub = draft
                ? `proposed by your agent · inert until approved · ${allow ? 'widening needs your code' : 'tightening — free'}`
                : `signed · approved ${esc(fmtRel(r.approved_at))}${r.stepup_at ? ' with step-up' : ''} · ${allow ? 'removing this only tightens' : 'removing a deny widens — needs your code'}`;
            const actions = draft
                ? `<button class="cseries-btn" data-nac-decline="egress:${attr(r.id)}">Decline</button>
                   <button class="cseries-btn primary" data-nac-approve="egress:${attr(r.id)}" data-widen="${allow ? '1' : '0'}">Approve</button>`
                // Removing an approved DENY widens reach → step-up; removing an allow only tightens → free.
                : `${mcpSignInButton(r.mcp)}<button class="cseries-btn" data-nac-remove="egress:${attr(r.id)}" data-widen="${(!allow) ? '1' : '0'}">Remove</button>`;
            return `
                <div class="net-card ${cardCls} nac-acl-rule">
                    <div class="net-card-main">
                        <div class="net-card-title">
                            <span class="net-card-name">${esc(r.pattern)}</span>
                            ${r.subject && r.subject !== '*' ? `<span class="cseries-pill">${esc(subjectLabel(r.subject))}</span>` : ''}
                            ${isPinnedExit(r.exit) ? `<span class="cseries-pill allow">out through ${esc(exitLabel(r.exit))}</span>` : ''}
                            ${r.terms_differ ? `<span class="cseries-pill deny" title="This row does not match what was signed; the gate enforces the signed terms.">unsigned edit</span>` : ''}
                            ${r.mcp ? `<span class="cseries-pill allow" title="${attr('MCP server ' + r.mcp.name + ' at ' + r.mcp.url + (r.mcp.key ? ', key ' + r.mcp.key : ''))}">MCP server ${esc(r.mcp.name)}</span>` : ''}
                            ${actionPill}
                            ${statePill}
                        </div>
                        <div class="net-card-sub">${sub}</div>
                        ${r.mcp ? `<div class="net-card-sub">connects <code>${esc(r.mcp.url)}</code>${r.mcp.key ? ' with your key ' + esc(r.mcp.key) : ''} · ${r.mcp.status === 'active' ? 'connected — removing this rule disconnects it' : 'connects when you approve'}</div>` : ''}
                        ${mcpSignInLine(r.mcp)}
                    </div>
                    <div class="net-card-actions">${actions}</div>
                </div>`;
        }).join('');
    }

    // ── MCP server sign-in (server/mcp_oauth.py) ──────────────────────────────
    // A connected server that asked for an OAuth sign-in carries r.mcp.oauth:
    // its state, the authorization server's host, and nothing secret.
    function mcpSignInLine(mcp) {
        const o = mcp && mcp.status === 'active' ? mcp.oauth : null;
        if (!o) return '';
        const at = o.authorization_server ? ` at <code>${esc(o.authorization_server)}</code>` : '';
        let text;
        if (o.state === 'signed_in') {
            const until = o.renews ? 'renews by itself' : (o.expires_at ? esc(fmtExpiry(o.expires_at)) : 'no expiry');
            text = `signed in${at} · ${until}`;
        } else if (o.state === 'more_access_needed') {
            text = `needs more access${o.more_scope ? ' (<code>' + esc(o.more_scope) + '</code>)' : ''} — sign in again${at}`;
        } else {
            text = `needs you to sign in${at}`;
        }
        const err = o.error && o.state !== 'signed_in' ? ` · ${esc(o.error)}` : '';
        return `<div class="net-card-sub">sign-in: ${text}${err}</div>`;
    }
    function mcpSignInButton(mcp) {
        const o = mcp && mcp.status === 'active' ? mcp.oauth : null;
        if (!o) return '';
        if (o.state === 'signed_in') {
            return `<button class="cseries-btn" data-mcp-signout="${attr(mcp.name)}">Sign out</button>`;
        }
        const label = o.state === 'more_access_needed' ? 'Sign in again' : 'Sign in';
        return `<button class="cseries-btn primary" data-mcp-signin="${attr(mcp.name)}">${label}</button>`;
    }

    let mcpWatch = null;
    function stopMcpWatch() { if (mcpWatch) { clearInterval(mcpWatch.timer); mcpWatch = null; } }
    // The sign-in tab is opened with rel=noopener, so the authorization
    // server's page has no handle on this window; the outcome is read back
    // from the server's card instead, every few seconds for five minutes.
    function watchMcpSignIn(name) {
        stopMcpWatch();
        const started = Date.now();
        mcpWatch = { name, timer: setInterval(async () => {
            if (Date.now() - started > 5 * 60 * 1000) { stopMcpWatch(); return; }
            try {
                const d = await fetchJSON('/api/network/nac/egress');
                const hit = (d.rules || []).find(x => x.mcp && x.mcp.name === name);
                const o = hit && hit.mcp.oauth;
                if (o && o.state === 'signed_in') {
                    stopMcpWatch();
                    const body = $('nac-modal-body');
                    if (body && body.querySelector('[data-mcp-wait]')) closeModal();
                    nacReload();
                }
            } catch (e) { /* the next tick tries again */ }
        }, 3000) };
    }

    async function mcpSignIn(name, client) {
        let res;
        try {
            const r = await fetch(`/api/mcp/oauth/${encodeURIComponent(name)}/start`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(client || {}),
            });
            const text = await r.text();
            let body = {};
            try { body = JSON.parse(text); } catch (e) { /* not JSON */ }
            if (r.status === 409 && body.needs_client) { askMcpClient(name, body); return; }
            if (!r.ok) throw new Error(friendlyError(r.status, text));
            res = body;
        } catch (e) { alert('Could not start the sign-in. ' + e.message); return; }
        // A link the person clicks, not window.open: it opens in every browser
        // without a popup blocker in the way, shows where it goes first, and
        // carries rel=noopener so the page it opens cannot reach this window.
        openModal(`Sign in to ${name}`, `
            <p>Your Host connects to <strong>${esc(name)}</strong> as you. You sign in at
               <code>${esc(res.authorization_server || '')}</code>, in a new tab, and approve what it asks
               for${res.scope ? ' (<code>' + esc(res.scope) + '</code>)' : ''}.</p>
            <p><a class="cseries-btn primary" href="${attr(res.authorize_url)}" target="_blank" rel="noopener noreferrer">Continue to ${esc(res.authorization_server || 'sign in')}</a></p>
            <p class="net-card-sub" data-mcp-wait>This closes by itself when the sign-in finishes. The token stays in your Host's vault: your Host never sees it.</p>`,
            [{ label: 'Close', cls: 'ghost', onClick: () => { closeModal(); } }]);
        watchMcpSignIn(name);
    }

    // The authorization server lets no client register itself: the person
    // registers one there and enters it here (spec: pre-registration).
    function askMcpClient(name, info) {
        openModal(`Sign in to ${name}: a client is needed`, `
            <p>${esc(info.detail || '')}</p>
            <div class="net-field"><span class="net-field-label">Redirect URI to register</span>
                <code>${esc(info.redirect_uri || '')}</code></div>
            <div class="net-field"><span class="net-field-label">Client ID</span>
                <input id="mcp-client-id" class="cseries-input" autocomplete="off" spellcheck="false"></div>
            <div class="net-field"><span class="net-field-label">Client secret (only if it gave you one)</span>
                <input id="mcp-client-secret" class="cseries-input" type="password" autocomplete="off"></div>`, [
            { label: 'Cancel', cls: 'ghost', onClick: closeModal },
            { label: 'Continue', cls: 'primary', onClick: () => {
                const id = ($('mcp-client-id') || {}).value || '';
                const secret = ($('mcp-client-secret') || {}).value || '';
                if (!id.trim()) return;
                closeModal();
                mcpSignIn(name, { client_id: id.trim(), client_secret: secret });
            } },
        ]);
    }

    async function mcpSignOut(name) {
        if (!confirm(`Sign out of ${name}? Your Host can no longer use it until you sign in again.`)) return;
        try {
            await fetchJSON(`/api/mcp/oauth/${encodeURIComponent(name)}/sign-out`, { method: 'POST' });
            nacReload();
        } catch (e) { alert('Could not sign out. ' + e.message); }
    }

    // A grant in one line: allow tokens, then "never [...]" when it carries a deny list.
    // html=true escapes every agent-supplied part (server, ops, deny) for innerHTML;
    // html=false returns plain text for a caller that escapes the whole line itself
    // (the pending card) — never both, or tokens render double-encoded.
    function grantRule(g, html) {
        const e = html ? esc : (s) => String(s);
        const ops = Array.isArray(g.ops) ? g.ops : [];
        const deny = Array.isArray(g.deny) ? g.deny : [];
        const parts = [e(g.server)];
        // An expired grant's allow and "do NOT" no longer apply (the gate skips it), so they
        // are shown as what it WAS, never as something holding now.
        const was = grantExpired(g) ? 'was ' : '';
        if (ops.length) parts.push(`${was}ops [ ${ops.map(e).join(', ')} ]`);
        if (deny.length) parts.push(`${was}never [ ${deny.map(e).join(', ')} ]`);
        if (g.expires_at || grantExpired(g)) parts.push(fmtExpiry(g.expires_at, grantExpired(g)));
        return parts.join(' · ');
    }
    // /nac/mgmt/grants says `expired` by the gate's own rule (nac_commit.grant_expired: the
    // column or the signed expiry); an approved grant it marks expired applies nothing.
    function grantExpired(g) {
        return !!g && !isDraft(g) && g.expired === true;
    }
    // Revoking an APPROVED grant that holds a deny list widens (the user's "do NOT"
    // stops holding) — it asks for the code, like removing an approved egress deny. An
    // expired grant's "do NOT" already stopped holding, so removing it is free. The server's
    // own answer (revoke_needs_code, from revoke_mgmt_grant's rule) decides when it is sent.
    function grantRevokeWidens(g) {
        if (!g || isDraft(g)) return false;
        if (typeof g.revoke_needs_code === 'boolean') return g.revoke_needs_code;
        return !grantExpired(g) && Array.isArray(g.deny) && g.deny.length > 0;
    }
    // The confirm before a grant is removed says what removing it does. A draft was never in
    // force, and an expired grant already applies nothing, so in neither case does the host
    // lose anything; only a live grant's removal takes rights away (or, with a "do NOT",
    // lifts that limit, which asks for the code).
    function grantRemoveConfirm(g, widening) {
        if (g && isDraft(g)) return 'Decline this draft? It was never in force, so the host loses nothing.';
        if (grantExpired(g)) return 'Remove this expired grant? It stopped applying when it expired, so the host loses nothing and its "do NOT" is already not holding.';
        return widening
            ? 'Revoke this grant? It carries your "do NOT", so revoking it asks for your code.'
            : 'Revoke this grant? The host loses these rights immediately.';
    }
    // The user's own words, read back verbatim (the grant's intent) — what they are
    // actually approving. Empty when the grant was drafted from tokens alone.
    function grantWords(g) {
        const it = g && g.intent && typeof g.intent === 'object' ? g.intent : null;
        if (!it) return '';
        const out = [];
        if (it.want) out.push(`<strong>You said:</strong> “${esc(it.want)}”`);
        if (it.do_not_want) out.push(`<strong>and not:</strong> “${esc(it.do_not_want)}”`);
        return out.length ? `<div class="nac-grant-words">${out.join(' ')}</div>` : '';
    }

    // ── MANAGEMENT GRANTS — default-deny; approved + drafts inline. ──
    function renderNacGrants(gr) {
        const list = $('nac-grants-list');
        const count = $('nac-mgmt-count');
        if (!list) return;
        if (gr && gr._err) { list.innerHTML = errBox(gr._err); if (count) count.textContent = '0'; return; }
        const grants = (state.nac.grants || []).slice().sort(laneDraftFirst);
        const drafts = grants.filter(isDraft).length;
        if (count) { count.textContent = drafts ? (drafts + ' pending') : String(grants.length); count.classList.toggle('pending', drafts > 0); }
        if (!grants.length) {
            list.innerHTML = empty('No management grants', 'Default-deny — no management operation your Host sends to your servers runs without a grant. When your agent proposes one, approve it here.');
            return;
        }
        const suspended = state.nac.suspended || [];
        list.innerHTML = grants.map(g => {
            const draft = isDraft(g);
            const ops = Array.isArray(g.ops) ? g.ops : [];
            const widening = ops.length > 0;  // deny-only grants tighten — approving is free
            const isSusp = !draft && g.server !== '*' && suspended.indexOf(g.server) !== -1;
            const expired = grantExpired(g);
            const cardCls = draft ? 'nac-draft-row' : ((isSusp || expired) ? 'down' : 'live');
            const statePill = draft ? '<span class="cseries-pill warn">draft — awaiting you</span>'
                : (expired ? '<span class="cseries-pill">expired</span>'
                   : (isSusp ? '<span class="cseries-pill suspend">suspended</span>' : '<span class="cseries-pill">approved</span>'));
            const sub = draft
                ? (widening ? 'proposed by your agent · grant is a widen — needs your code'
                            : 'proposed by your agent · deny only — tightens, no code needed')
                : (isSusp
                    ? `suspended — denies even with this grant · resume restores it (asks for your code)`
                    : `signed · ${fmtExpiry(g.expires_at, grantExpired(g))}${g.stepup_at ? ' · approved with step-up' : ''}`);
            // Granting an allow list is a widen (step-up); a deny-only grant tightens (free). Revoking is
            // free unless the grant carries a deny list (revokeGrant asks for the code). For an approved grant
            // on a concrete server, also offer suspend (tighten → free) / resume (widen → step-up).
            let actions;
            if (draft) {
                actions = `<button class="cseries-btn" data-nac-decline="grant:${attr(g.id)}">Decline</button>
                   <button class="cseries-btn primary" data-nac-approve="grant:${attr(g.id)}" data-widen="${widening ? '1' : '0'}">Approve</button>`;
            } else {
                const suspBtn = (g.server !== '*')
                    ? (isSusp
                        ? `<button class="cseries-btn primary" data-nac-resume="${attr(g.server)}">Resume</button>`
                        : `<button class="cseries-btn" data-nac-suspend="${attr(g.server)}">Suspend</button>`)
                    : '';
                actions = `${suspBtn}<button class="cseries-btn danger" data-nac-revoke="grant:${attr(g.id)}" data-widen="${grantRevokeWidens(g) ? '1' : '0'}">Revoke</button>`;
            }
            return `
                <div class="net-card ${cardCls} nac-grant-row">
                    <div class="net-card-main">
                        <div class="net-card-title">
                            <span class="net-card-name">${grantRule(g, true)}</span>
                            <span class="cseries-pill ${widening ? 'info' : 'deny'}">${widening ? 'grant' : 'deny only'}</span>
                            ${statePill}
                        </div>
                        ${grantWords(g)}
                        <div class="net-card-sub">${expired ? esc(fmtExpiry(g.expires_at, true)) + ' — no longer applies · removing it is free' : sub}</div>
                    </div>
                    <div class="net-card-actions">${actions}</div>
                </div>`;
        }).join('');
    }

    // ── MANAGEMENT AUDIT — read-only command accounting (most recent first). ──
    function renderNacAudit(au) {
        const list = $('nac-audit-list');
        if (!list) return;
        if (au && au._err) { list.innerHTML = errBox(au._err); return; }
        const rows = state.nac.audit || [];
        if (!rows.length) {
            list.innerHTML = empty('No management activity yet', 'Every command run on a server in your mesh is recorded here — where, what, how it was decided, and the result.');
            return;
        }
        list.innerHTML = rows.map(r => {
            const dec = r.decision || '';
            const ran = dec === 'ran';
            const denied = dec.indexOf('denied') === 0;
            const pillCls = ran ? (Number(r.exit_code) === 0 ? 'allow' : 'warn') : (denied ? 'deny' : '');
            const code = (r.exit_code === null || r.exit_code === undefined) ? '—' : String(r.exit_code);
            return `
                <div class="net-card ${ran ? (Number(r.exit_code) === 0 ? 'live' : 'stale') : (denied ? 'down' : '')}">
                    <div class="net-card-main">
                        <div class="net-card-title">
                            <span class="net-card-name">${esc(r.server || '—')} · ${esc(r.op || '—')}</span>
                            <span class="cseries-pill ${pillCls}">${esc(dec || 'unknown')}</span>
                        </div>
                        <div class="net-card-sub">exit ${esc(code)} · ${esc(fmtRel(r.at))}</div>
                    </div>
                </div>`;
        }).join('');
    }

    // rail badge — drafts awaiting approval (clone of updateAgentsBadge).
    function updateNacBadge() {
        const b = $('nac-badge');
        if (!b) return;
        const n = state.nac.pending || 0;
        if (n > 0) { b.textContent = n > 99 ? '99+' : String(n); b.hidden = false; }
        else { b.hidden = true; }
    }

    // ── step-up: the widen→TOTP / tighten→free spine ──
    // withStepup(actionFn, isWidening): a widen opens a TOTP inspector and calls
    // actionFn(code); a tighten runs actionFn(null) directly. A first-time widen with no
    // second factor routes to enrollment first.
    function withStepup(actionFn, isWidening) {
        if (!isWidening) { actionFn(null); return; }
        if (!state.nac.stepupEnrolled) { enrollStepup(); return; }
        openStepupInspector(actionFn);
    }

    // ── Centered popup modal (scrim + centered card) — the step-up TOTP entry and the
    //    enrollment QR live here, NOT in the side-rail #inspector. ──
    function openModal(title, bodyHTML, actions) {
        $('nac-modal-title').textContent = title;
        $('nac-modal-body').innerHTML = bodyHTML || '';
        const actEl = $('nac-modal-actions');
        actEl.innerHTML = '';
        (actions || []).forEach(a => {
            const btn = document.createElement('button');
            btn.className = 'cseries-btn ' + (a.cls || '');
            btn.textContent = a.label;
            if (a.id) btn.id = a.id;
            if (a.disabled) btn.disabled = true;
            btn.addEventListener('click', a.onClick);
            actEl.appendChild(btn);
        });
        $('nac-scrim').hidden = false;
        return { body: $('nac-modal-body'), actions: actEl };
    }
    function closeModal() { $('nac-scrim').hidden = true; $('nac-modal-body').innerHTML = ''; }

    // 6-box TOTP entry that fills as the (hidden) input is typed. onChange(code) fires each keystroke.
    function totpEntry(container, onChange) {
        let code = '';
        function boxes() {
            let h = '<div class="nac-totp" role="group" aria-label="6-digit authenticator code">';
            for (let i = 0; i < 6; i++) {
                const filled = i < code.length, caret = i === code.length;
                h += `<div class="nac-totp-box ${filled ? 'filled' : ''} ${caret ? 'caret' : ''}">${filled ? esc(code[i]) : (caret ? '|' : '')}</div>`;
            }
            return h + '</div>';
        }
        function render() {
            container.innerHTML = boxes() +
                `<input id="nac-totp-input" inputmode="numeric" autocomplete="one-time-code" maxlength="6"
                        style="position:absolute;opacity:0;width:1px;height:1px;pointer-events:none" aria-hidden="true">`;
            const inp = $('nac-totp-input');
            inp.value = code;
            inp.addEventListener('input', () => {
                code = (inp.value || '').replace(/\D/g, '').slice(0, 6);
                inp.value = code; render(); inp.focus();
                if (onChange) onChange(code);
            });
            setTimeout(() => inp.focus(), 0);
        }
        render();
        return { get: () => code };
    }

    // Draw a REAL scannable QR for an otpauth:// URI into `el`, using the vendored
    // qrcode-generator (global `qrcode`). Falls back to showing the URI as text if the
    // lib somehow didn't load (degrade, never blank).
    function drawQR(el, text) {
        try {
            if (typeof qrcode !== 'function') throw new Error('qr lib missing');
            const qr = qrcode(0, 'M');     // type 0 = auto-fit; M = 15% error correction (camera-tolerant)
            qr.addData(text);
            qr.make();
            // createSvgTag scales crisply and needs no canvas; 0 margin (our white plate is the quiet zone).
            el.innerHTML = qr.createSvgTag({ cellSize: 5, margin: 0, scalable: true });
            const svg = el.querySelector('svg');
            if (svg) { svg.removeAttribute('width'); svg.removeAttribute('height'); }
            return true;
        } catch (e) {
            el.innerHTML = `<div class="net-field-val mono" style="color:var(--cseries-ink);word-break:break-all">${esc(text)}</div>`;
            return false;
        }
    }

    // The step-up TOTP entry — CENTERED modal. Typing fills the boxes; 6 digits enables Approve.
    function openStepupInspector(actionFn) {
        const m = openModal('Approve widening', `
            <div class="nac-stepup-callout">
                <strong>Enter your authenticator code to approve this widening.</strong><br>
                This grants new reach. Approving signs it with your Host's key. Tightening would not ask for a code.
            </div>
            <div class="net-field">
                <span class="net-field-label">Authenticator code</span>
                <div id="nac-totp-host"></div>
                <span class="net-field-val" style="margin-top:6px;color:var(--cseries-ash);text-align:center">From your authenticator app · enrolled second factor</span>
            </div>`, [
            { label: 'Cancel', cls: 'ghost', onClick: closeModal },
            { label: 'Approve widening', cls: 'primary', id: 'nac-modal-ok', disabled: true, onClick: () => {
                if (entry.get().length !== 6) return;
                const code = entry.get();
                closeModal();
                actionFn(code);
            } },
        ]);
        const entry = totpEntry($('nac-totp-host'), (code) => {
            const ok = $('nac-modal-ok'); if (ok) ok.disabled = code.length !== 6;
        });
        m.body.addEventListener('click', () => { const i = $('nac-totp-input'); if (i) i.focus(); });
    }

    // First-time second-factor enrollment — CENTERED modal with a REAL QR. Provision the secret,
    // render the QR (+ a copyable base32 fallback under it), then confirm a code. The secret never
    // leaves the pod; this only enables widening approvals.
    async function enrollStepup() {
        let res;
        try {
            res = await fetchJSON('/api/network/nac/stepup/enroll', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
            });
        } catch (e) { alert('Could not start enrollment. ' + e.message); return; }
        state.nac.stepupSecret = res.secret || null;
        const m = openModal('Enroll your second factor', `
            <div class="nac-stepup-callout">
                <strong>Scan this with your authenticator app, then enter a code to confirm.</strong><br>
                This enrolls the second factor that approves widening (an allow, a grant). It's held in your container only.
            </div>
            <div class="nac-qr-wrap">
                <div class="nac-qr" id="nac-qr"></div>
                <div class="nac-qr-fallback">
                    <span class="nac-qr-hint">Can't scan? Enter this key manually (tap to copy):</span>
                    <code class="nac-qr-secret" id="nac-qr-secret" title="Copy">${esc(res.secret || '—')}</code>
                </div>
            </div>
            <div class="net-field">
                <span class="net-field-label">Confirm with a code</span>
                <div id="nac-totp-host"></div>
            </div>`, [
            { label: 'Cancel', cls: 'ghost', onClick: closeModal },
            { label: 'Confirm enrollment', cls: 'primary', id: 'nac-modal-ok', disabled: true, onClick: () => confirmStepup(entry.get()) },
        ]);
        drawQR($('nac-qr'), res.otpauth_uri || '');
        const sec = $('nac-qr-secret');
        if (sec) sec.addEventListener('click', (e) => _copyBtn(e, res.secret || ''));
        const entry = totpEntry($('nac-totp-host'), (code) => {
            const ok = $('nac-modal-ok'); if (ok) ok.disabled = code.length !== 6;
        });
        m.body.addEventListener('click', (e) => {
            if (e.target.closest('#nac-qr-secret')) return;  // don't steal the copy click
            const i = $('nac-totp-input'); if (i) i.focus();
        });
    }
    async function confirmStepup(code) {
        if (!code || code.length !== 6) { return; }
        try {
            await fetchJSON('/api/network/nac/stepup/confirm', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }),
            });
        } catch (e) { alert('Could not confirm. ' + e.message); return; }
        state.nac.stepupEnrolled = true;
        state.nac.stepupSecret = null;
        closeModal();
        renderNacStepupChip();
    }

    // ── action handlers — each POSTs the real route; widens route through withStepup ──
    function nacReload() { state.loaded.nac = false; loadNAC(); }

    function approveEgress(id, widening) {
        withStepup(async (codeOrNull) => {
            try {
                await fetchJSON(`/api/network/nac/egress/${encodeURIComponent(id)}/approve`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(codeOrNull ? { stepup_code: codeOrNull } : {}),
                });
                nacReload();
            } catch (e) { alert('Could not approve. ' + e.message); }
        }, widening);
    }
    function declineEgress(id) {  // declining a draft only tightens — free
        revokeEgress(id, false);
    }
    function removeEgress(id, widening) {  // removing an approved DENY widens → step-up
        if (!widening && !confirm('Remove this egress rule? It tightens your reach.')) return;
        revokeEgress(id, widening);
    }
    function revokeEgress(id, widening) {
        withStepup(async (codeOrNull) => {
            try {
                await fetchJSON(`/api/network/nac/egress/${encodeURIComponent(id)}/revoke`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(codeOrNull ? { stepup_code: codeOrNull } : {}),
                });
                nacReload();
            } catch (e) {
                if (stepupNowRequired(e, codeOrNull)) {
                    nacReload();
                    if (confirm(e.message + '\n\nEnter your authenticator code to remove it?')) revokeEgress(id, true);
                    return;
                }
                alert('Could not remove. ' + e.message);
            }
        }, widening);
    }

    function approvePolicy(id, widening) {
        withStepup(async (codeOrNull) => {
            try {
                await fetchJSON(`/api/network/nac/policy/${encodeURIComponent(id)}/approve`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(codeOrNull ? { stepup_code: codeOrNull } : {}),
                });
                nacReload();
            } catch (e) { alert('Could not approve. ' + e.message); }
        }, widening);
    }
    function declinePolicy(id) {  // declining a draft only tightens — free
        revokePolicy(id, false);
    }
    function revokePolicy(id, widening) {  // removing an approved rule that keeps machines out widens → step-up
        withStepup(async (codeOrNull) => {
            try {
                await fetchJSON(`/api/network/nac/policy/${encodeURIComponent(id)}/revoke`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(codeOrNull ? { stepup_code: codeOrNull } : {}),
                });
                nacReload();
            } catch (e) {
                if (stepupNowRequired(e, codeOrNull)) {
                    nacReload();
                    if (confirm(e.message + '\n\nEnter your authenticator code to remove it?')) revokePolicy(id, true);
                    return;
                }
                alert('Could not remove. ' + e.message);
            }
        }, widening);
    }

    function approveGrant(id, widening) {  // an allow list is a widen → step-up; deny-only tightens → free
        withStepup(async (codeOrNull) => {
            try {
                await fetchJSON(`/api/network/nac/mgmt/grant/${encodeURIComponent(id)}/approve`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(codeOrNull ? { stepup_code: codeOrNull } : {}),
                });
                nacReload();
            } catch (e) { alert('Could not approve. ' + e.message); }
        }, widening !== false);
    }
    function declineGrant(id) {  // declining a draft only tightens — free
        revokeGrant(id);
    }
    function revokeGrant(id, widening, confirmed) {  // removing an allow tightens (free); removing a held DENY widens (code)
        const g = (state.nac.grants || []).find(x => String(x.id) === String(id));
        // `confirmed`: the retry below, where the user has just answered the server's own
        // refusal — the grant is no longer what the card showed, so it is not asked again.
        if (!confirmed && !confirm(grantRemoveConfirm(g, widening))) return;
        withStepup(async (codeOrNull) => {
            try {
                await fetchJSON(`/api/network/nac/mgmt/grant/${encodeURIComponent(id)}/revoke`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(codeOrNull ? { stepup_code: codeOrNull } : {}),
                });
                nacReload();
            } catch (e) {
                // A decline that lost the race to an approval carrying a deny comes back
                // needing the code (nac_commit.revoke_mgmt_grant): offer the prompt, as the
                // policy and egress removals do, instead of leaving only an error.
                if (stepupNowRequired(e, codeOrNull)) {
                    nacReload();
                    if (confirm(e.message + '\n\nEnter your authenticator code to remove it?')) revokeGrant(id, true, true);
                    return;
                }
                alert('Could not revoke. ' + e.message);
            }
        }, !!widening);
    }

    // Messaging connectors: letting an account in widens (authenticator code); declining a
    // pairing and revoking a connector only tighten (free). Revoking leaves the app running.
    function approveConnector(id) {
        withStepup(async (code) => {
            try {
                await fetchJSON(`/api/network/nac/connector/${encodeURIComponent(id)}/approve`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ stepup_code: code }),
                });
                nacReload();
            } catch (e) { alert('Could not let the account in. ' + e.message); }
        }, true);
    }
    async function declineConnector(id) {
        try {
            await fetchJSON(`/api/network/nac/connector/${encodeURIComponent(id)}/decline`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
            });
            nacReload();
        } catch (e) { alert('Could not decline. ' + e.message); }
    }
    async function revokeConnector(id) {
        if (!confirm('Revoke this connector? Its next message is refused and a reply in progress is '
                     + 'stopped. The connector app keeps running until you delete it.')) return;
        try {
            await fetchJSON(`/api/network/nac/connector/${encodeURIComponent(id)}/revoke`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
            });
            nacReload();
        } catch (e) { alert('Could not revoke. ' + e.message); }
    }

    // Dispatch a "kind:id" action token from a data-attr to the right handler.
    function nacDispatch(kind, action, id, widening) {
        if (kind === 'egress') {
            if (action === 'approve') approveEgress(id, widening);
            else if (action === 'decline') declineEgress(id);
            else if (action === 'remove') removeEgress(id, widening);
        } else if (kind === 'policy') {  // the INGRESS lane
            if (action === 'approve') approvePolicy(id, widening);
            else if (action === 'decline') declinePolicy(id);
            else if (action === 'revoke') revokePolicy(id, widening);
        } else if (kind === 'grant') {
            if (action === 'approve') approveGrant(id, widening);
            else if (action === 'decline') declineGrant(id);
            else if (action === 'revoke') revokeGrant(id, widening);
        } else if (kind === 'connector') {
            if (action === 'approve') approveConnector(id);
            else if (action === 'decline') declineConnector(id);
            else if (action === 'revoke') revokeConnector(id);
        }
    }

    // ── Per-bastion SUSPEND / RESUME. Suspend tightens (free); resume widens (→ step-up modal). ──
    async function suspendServer(server) {
        if (!confirm('Suspend ' + server + '? Management + harness on this bastion are denied immediately, even with valid grants. The grants are kept — resume restores them (it asks for your code).')) return;
        try {
            await fetchJSON('/api/network/nac/mgmt/suspend', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ server }),
            });
            nacReload();
        } catch (e) { alert('Could not suspend. ' + e.message); }
    }
    function resumeServer(server) {
        // Resume restores reach → a WIDEN → step-up (the agent can't produce the user's code).
        withStepup(async (code) => {
            try {
                await fetchJSON('/api/network/nac/mgmt/resume', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ server, stepup_code: code }),
                });
                nacReload();
            } catch (e) { alert('Could not resume. ' + e.message); }
        }, true);
    }

    // ── C1: Per-bastion QUARANTINE / RELEASE (whole-mesh). Quarantine tightens (free) and
    //    drops the concentrator peer; Release widens (→ step-up) and restores it. ──
    async function quarantineServer(server) {
        if (!confirm('Quarantine ' + server + '? This cuts the box\'s ENTIRE mesh: its tunnel is dropped (no inbound or outbound) and management + harness are denied. The bearer is kept — release brings it back with no re-enrol (it asks for your code).')) return;
        try {
            await fetchJSON('/api/network/nac/quarantine', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ server }),
            });
            nacReload();
        } catch (e) { alert('Could not quarantine. ' + e.message); }
    }
    function unquarantineServer(server) {
        // Release restores the whole mesh → a WIDEN → step-up (the agent can't produce the code).
        withStepup(async (code) => {
            try {
                await fetchJSON('/api/network/nac/unquarantine', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ server, stepup_code: code }),
                });
                nacReload();
            } catch (e) { alert('Could not release. ' + e.message); }
        }, true);
    }

    // ── Collapsible lanes (Ingress / Egress / Management). Default: open a lane that has
    //    pending drafts; else collapsed — unless the user explicitly toggled it (laneOpen). ──
    // 'posture' is not a policy lane — it holds no drafts, so
    // laneDraftCount returns 0 and applyLaneDefaults leaves it CLOSED — but it must be
    // in this list or the user's explicit toggle is recorded in state.nac.laneOpen and
    // then never re-applied, so the section snaps shut on the next render.
    const LANES = ['ingress', 'egress', 'mgmt', 'posture'];
    function laneDraftCount(lane) {
        if (lane === 'ingress') return (state.nac.policy || []).filter(isDraft).length
            + (state.nac.connectors || []).filter(connectorWaiting).length;
        if (lane === 'egress') return (state.nac.egress || []).filter(isDraft).length;
        if (lane === 'mgmt') return (state.nac.grants || []).filter(isDraft).length;
        return 0;
    }
    function setLaneOpen(lane, open) {
        const el = $('nac-lane-' + lane);
        if (!el) return;
        el.classList.toggle('open', !!open);
        const head = el.querySelector('.nac-lane-head');
        if (head) head.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    // A lane opens by itself when it holds drafts; Ingress also opens while the engine is
    // locked, so its banner is not hidden behind a closed header.
    function laneWantsOpen(lane) {
        return laneDraftCount(lane) > 0 || (lane === 'ingress' && ingressLocked());
    }
    function applyLaneDefaults() {
        LANES.forEach(lane => {
            const override = state.nac.laneOpen[lane];
            const open = (override === undefined) ? laneWantsOpen(lane) : override;
            setLaneOpen(lane, open);
        });
    }
    function toggleLane(lane) {
        const el = $('nac-lane-' + lane);
        if (!el) return;
        const open = !el.classList.contains('open');
        state.nac.laneOpen[lane] = open;   // record the user's explicit choice
        setLaneOpen(lane, open);
    }

    // ════════════════════ AGENTS (observe → reconcile → contain) ════════════════════
    // The bastion watches the machine it runs on for AI/agent activity and reports it on its
    // heartbeat (soft, evadable — visibility, never the wall). Here the host reconciles: an agent
    // carrying a delegation tag it issued is "requested"; everything else is "unrequested" — activity
    // you didn't ask for. You sanction (mint a tag) or contain (the bastion SIGTERMs it).
    async function loadAgents() {
        const list = $('agents-list');
        if (!state.loaded.agents) list.innerHTML = skeletonRows(3);
        let agents;
        try {
            agents = await fetchJSON('/api/network/agents');
        } catch (e) { list.innerHTML = errBox(e.message); $('agents-stats').innerHTML = ''; return; }
        let dels = [];
        try { dels = (await fetchJSON('/api/network/agent-delegations')).delegations || []; } catch (e) { dels = []; }
        // Scheduled jobs ride on the bastion rows (heartbeat schedulers, v129).
        try {
            const bs = await fetchJSON('/api/network/bastions');
            state.bastions = bs.bastions || bs || [];
        } catch (e) { /* the Bastions lens may have loaded them already; else empty */ }
        state.agents = agents.agents || [];
        state.agentsUnrequested = agents.unrequested_count || 0;
        state.agentDelegations = dels;
        state.loaded.agents = true;
        renderAgents();
        renderDelegations();
        updateAgentsBadge();
    }

    // ── bastion agents lens (1.5.2) ──────────────────────────────
    // One row per machine store that carries the 'search' mask, joined with the
    // bastion's per-store report (heartbeat freeagent_stores, v118). The words
    // are the member's next action, never a state code.
    // numeric semver compare — string order says '0.9.0' > '0.13.2'
    function versionBefore(v, ref) {
        if (!v) return false;
        const a = String(v).split('.').map(n => parseInt(n, 10) || 0), r = ref.split('.').map(n => parseInt(n, 10) || 0);
        for (let i = 0; i < 3; i++) { if ((a[i] || 0) !== (r[i] || 0)) return (a[i] || 0) < (r[i] || 0); }
        return false;
    }
    async function loadFreeAgents() {
        const list = $('freeagents-list');
        list.innerHTML = skeletonRows(2);
        let stores = [], bastions = [];
        try {
            const [st, bs] = await Promise.all([fetchJSON('/api/network/nac/dcn/stores'), fetchJSON('/api/network/bastions')]);
            stores = (st.stores || st || []);
            bastions = (bs.bastions || bs || []);
        } catch (e) { list.innerHTML = errBox(e.message); return; }
        const byHost = {};
        bastions.forEach(b => { if (b.hostname) byHost[b.hostname] = b; });
        const rows = (Array.isArray(stores) ? stores : []).filter(s => s.class === 'machine' && (s.mask || []).includes('search') && s.status !== 'revoked');
        if (!rows.length) { list.innerHTML = empty('No bastion agents yet', 'Grant a folder on one of your machines with search (Context → Your network) and the bastion there will run one for it.'); return; }
        list.innerHTML = rows.map(s => {
            const b = byHost[s.server] || {};
            const st = s.freeagent_state || ((b.freeagent_stores || {})[String(s.id)]);
            let pill, words, cls = 'muted';
            // offline first: a box silent for minutes must not read as serving
            if (!b.online) { pill = 'machine offline'; words = 'The bastion on this machine has not checked in for a while.'; }
            else if (s.freeagent || (st && st.serving)) { pill = 'serving'; cls = 'allow'; words = 'Indexing locally and answering your host\'s searches with redacted text.'; }
            else if (!st) { pill = 'not reported'; words = versionBefore(b.bastion_version, '0.13.2') ? 'This bastion is older than 0.13.2 — update it from the Bastions lens and it will install the bastion agent from the grant.' : 'The bastion has not reported a bastion agent for this store yet; it retries every 10 minutes.'; }
            else {
                const map = {
                    'missing-python': ['needs Python', 'Install Python 3.8 or newer on this machine (Microsoft Store on Windows). The bastion retries every 10 minutes; nothing else to do.'],
                    'missing-openssl': ['needs openssl', 'Install openssl on this machine (Git for Windows provides it). The bastion retries every 10 minutes.'],
                    'installing': ['installing', 'Fetching and signing the bastion agent from your grant.'],
                    'running': ['starting', 'The first index run is in progress.'],
                    'failed': ['failed', st.detail || 'See the machine.'],
                };
                const m = map[st.state] || [st.state || 'unknown', st.detail || ''];
                pill = m[0]; words = m[1]; cls = (st.state === 'failed' || String(st.state).startsWith('missing')) ? 'deny' : 'muted';
            }
            return `<div class="net-card">
                <div class="net-block-head"><div><strong>${esc(s.display_name || s.root)}</strong>
                    <span class="cseries-pill ${cls}">${esc(pill)}</span></div>
                    <span class="label-sys">${esc(s.server)}${st && st.version ? ' · bastion agent ' + esc(st.version) : ''}</span></div>
                <p class="view-sub">${esc(s.root)} · sweep ${esc(s.sweep || 'hourly')} · ${esc(words)}</p>
            </div>`;
        }).join('');
    }
    document.getElementById('freeagents-refresh')?.addEventListener('click', loadFreeAgents);

    // ── Servers: the service slot's reach grants (bastion 0.36.0). A server a host built
    //    on your machine has no network; a host reaches it only with a reach grant you
    //    approve here (a widen: step-up code, or on the machine when it holds a factor).
    //    Revoke is a tighten: free, pushed to the machine at once. ──
    async function loadServers() {
        const list = $('servers-list');
        list.innerHTML = skeletonRows(2);
        let grants = [];
        try {
            const r = await fetchJSON('/api/network/nac/services/reach');
            grants = (r.grants || []).filter(g => g.status !== 'revoked');
        } catch (e) { list.innerHTML = errBox(e.message); return; }
        if (!grants.length) {
            list.innerHTML = empty('No servers yet', 'When a host builds a server on one of your machines, its request to reach it appears here for you to approve.');
            return;
        }
        list.innerHTML = grants.map(g => {
            const granted = g.status === 'granted';
            const pill = granted ? '<span class="cseries-pill allow">can reach</span>'
                : (g.pending_on_box ? '<span class="cseries-pill muted">approve on the machine</span>'
                    : '<span class="cseries-pill muted">asking</span>');
            const until = granted && g.expires_at ? ` · until ${esc(String(g.expires_at).slice(0, 10))}` : '';
            const btn = granted
                ? `<button class="cseries-btn ghost" data-reach-revoke="${g.id}">Revoke</button>`
                : `<button class="cseries-btn primary" data-reach-approve="${g.id}" data-mirrors-digest="${esc(g.mirrors_digest || '')}">Approve</button>
                   <button class="cseries-btn ghost" data-reach-revoke="${g.id}">Decline</button>`;
            return `<div class="net-card">
                <div class="net-block-head"><div><strong>${esc(g.slot)}</strong> on ${esc(g.server)} ${pill}</div>
                    <span class="label-sys">${btn}</span></div>
                <p class="view-sub">Host <strong>${esc(g.subject)}</strong> may reach this server${until}.${g.note ? ' ' + esc(g.note) : ''}</p>
                ${(g.mirrors || []).length ? `<p class="view-sub">Your machine may fetch, over your own connection, into this server:</p>
                <ul class="view-sub">${g.mirrors.map(m => `<li>${esc(m.url)} → ${esc(m.repo)}</li>`).join('')}</ul>` : ''}
            </div>`;
        }).join('');
        list.querySelectorAll('[data-reach-approve]').forEach(b => b.addEventListener('click', () => approveReach(b.dataset.reachApprove, b.dataset.mirrorsDigest)));
        list.querySelectorAll('[data-reach-revoke]').forEach(b => b.addEventListener('click', () => revokeReach(b.dataset.reachRevoke)));
    }
    function approveReach(id, seenDigest) {
        withStepup(async (codeOrNull) => {
            try {
                // The fingerprint of the mirror list drawn on this card: a list that
                // changed after the page was drawn is refused, never approved unseen.
                const body = { mirrors_digest: seenDigest || '' };
                if (codeOrNull) body.stepup_code = codeOrNull;
                const r = await fetchJSON(`/api/network/nac/services/reach/${encodeURIComponent(id)}/approve`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (r && r.pending_on_box) alert(r.next || 'Approve it on the machine itself.');
                loadServers();
            } catch (e) { alert('Could not approve. ' + e.message); }
        }, true);
    }
    async function revokeReach(id) {
        if (!confirm('Take this host\'s reach to the server away? It takes effect at once.')) return;
        try {
            await fetchJSON(`/api/network/nac/services/reach/${encodeURIComponent(id)}/revoke`, { method: 'POST' });
            loadServers();
        } catch (e) { alert('Could not revoke. ' + e.message); }
    }
    document.getElementById('servers-refresh')?.addEventListener('click', loadServers);

    function updateAgentsBadge() {
        const b = $('agents-badge');
        if (!b) return;
        const n = state.agentsUnrequested || 0;
        if (n > 0) { b.textContent = n > 99 ? '99+' : String(n); b.hidden = false; }
        else { b.hidden = true; }
    }

    // Kinds. Every observation carries a kind — agent (acts on its
    // own), app (software you are running that talks to a model: the Claude
    // desktop app, an IDE, a browser), client (an unknown process caught by its
    // model traffic) — and a family key (server + kind + executable). Rows in a
    // family are ONE thing: an agent and the processes it started, an app and
    // its helpers. One card per family; apps in their own block with no alert
    // and no Contain; Contain on an agent takes its whole process tree.
    function agentFamilies() {
        const fams = {};
        (state.agents || []).forEach(a => {
            const kind = a.kind || 'agent';
            const key = a.family || ((a.hostname || '') + '|' + kind + '|' + (a.signature || ''));
            if (!fams[key]) fams[key] = { key: key, kind: kind, hostname: a.hostname, signature: a.signature, members: [] };
            fams[key].members.push(a);
        });
        return Object.keys(fams).map(k => {
            const f = fams[k];
            f.active = f.members.some(m => m.active);
            f.requested = f.members.every(m => m.requested);
            f.acknowledged = !f.requested && f.members.every(m => m.requested || m.acknowledged);
            // Never alerted: an app (yours) and an automation (no model — inventory, not an agent).
            f.unrequested = f.kind !== 'app' && f.kind !== 'automation' && f.members.some(m => m.active && !m.requested && !m.acknowledged);
            f.endpoints = [];
            f.members.forEach(m => (m.llm_endpoints || []).forEach(e => { if (f.endpoints.indexOf(e) === -1) f.endpoints.push(e); }));
            // Origin (bastion 0.13.12): how the family was started. The root of
            // the lineage (the member no other member claims as parent) decides;
            // a sub-agent's origin is always `agent`, which says nothing new.
            const pids = new Set(f.members.map(m => m.pid));
            const root = f.members.find(m => !m.ppid || !pids.has(m.ppid)) || f.members[0];
            f.origin = root.origin || (f.members.map(m => m.origin).filter(Boolean)[0]) || null;
            f.evidence = [];
            f.members.forEach(m => (m.evidence || []).forEach(e => {
                if (e && e.signal && !f.evidence.some(x => x.signal === e.signal && x.value === e.value)) f.evidence.push(e);
            }));
            f.last_seen = f.members.map(m => m.last_seen || '').sort().slice(-1)[0];
            f.first_seen = f.members.map(m => m.first_seen || '').filter(Boolean).sort()[0];
            f.user = (f.members.find(m => m.agent_user) || {}).agent_user;
            f.rep = f.members[0];
            return f;
        }).sort((a, b) => (b.active - a.active) || String(b.last_seen).localeCompare(String(a.last_seen)));
    }

    // The scheduled-jobs note for one bastion, or null. No report and no
    // capability has two causes, and the version tells them apart: a build
    // before 0.13.12 has no scanner; a newer one is not looking for this
    // pairing (observe off, or a platform with no scheduler to read). The
    // inventory shown below the note is the last one the box did report —
    // the host keeps it rather than wiping it on the blind beat.
    function schedulerScanNote(b) {
        const who = b.hostname || b.name;
        const scan = b.scheduler_scan;
        if (scan && scan.supported) return `${who}: ${scan.entries} ${scan.entries === 1 ? 'entry' : 'entries'} across ${(scan.kinds || []).join(', ') || 'no scheduler'}`;
        if (!b.online || (b.capabilities || []).indexOf('scheduler-observe') !== -1) return null;
        // A box reporting no version at all gets the update wording too: it
        // cannot be shown to be new enough to be merely not watching.
        if (!b.bastion_version || versionBefore(b.bastion_version, '0.13.12')) return `${who}: not read — update the bastion to 0.13.12`;
        return `${who}: not read now — this pairing is not watching (observe is off, or nothing to read on this platform)`
            + ((b.schedulers || []).length ? '; showing its last inventory' : '');
    }

    // Origin words (bastion 0.13.12). "scheduler" is the one that matters: it is
    // the shape that runs for months with nobody at the keyboard.
    const ORIGIN_WORDS = {
        interactive: 'started from a terminal or desktop session',
        remote: 'started from a remote session (ssh, screen sharing)',
        scheduler: 'started by a scheduler — cron, a timer, a scheduled task; nobody at the keyboard',
        service: 'runs as a service — no session, no terminal',
        agent: 'started by another agent',
        unknown: 'how it was started could not be read',
    };
    // When a containment was placed on this family (1.10.2): the oldest open
    // order across its members, or null when any member has none — a family
    // half-ordered still needs its Contain button for the other half.
    function containPendingSince(f) {
        const when = (f.members || []).map(m => m.contain_pending_since || null);
        if (!when.length || when.some(w => !w)) return null;
        return when.slice().sort()[0];
    }
    function originPill(o) {
        if (!o) return '';
        const cls = o === 'scheduler' ? 'deny' : '';
        return `<span class="cseries-pill ${cls}" title="${attr(ORIGIN_WORDS[o] || o)}">${esc(o)}</span>`;
    }

    function familyCard(f) {
        const key = attr(f.key);
        const n = f.members.length;
        const quiet = f.kind === 'app' || f.kind === 'automation';
        const cls = quiet ? (f.active ? '' : 'stale')
            : (f.active ? (f.requested ? 'live' : (f.acknowledged ? 'stale' : 'down')) : 'stale');
        const pill = f.kind === 'app' ? '<span class="cseries-pill">app</span>'
            : f.kind === 'automation' ? '<span class="cseries-pill">automation · no model</span>'
            : (f.requested ? '<span class="cseries-pill allow">requested</span>'
                : (f.acknowledged ? '<span class="cseries-pill">acknowledged</span>'
                    : '<span class="cseries-pill deny">unrequested</span>'));
        const sub = [f.hostname || '—',
            n === 1 ? ('pid ' + f.rep.pid) : (n + ' processes'),
            f.user ? ('user ' + f.user) : null,
            f.endpoints.length ? ('talking to ' + f.endpoints.slice(0, 2).join(', ') + (f.endpoints.length > 2 ? '…' : '')) : null]
            .filter(Boolean).join(' · ');
        // A containment already placed on every process here (1.10.2): the
        // button gives way to the state, because a second click would only
        // land on the same open order.
        const pending = containPendingSince(f);
        const actions = f.kind === 'app' ? '' : `
                        ${f.unrequested ? `<button class="cseries-btn ghost" data-agent-ack="${key}">Acknowledge</button>` : ''}
                        ${pending ? '' : `<button class="cseries-btn danger" data-agent-contain="${key}">Contain</button>`}`;
        return `
                <div class="net-card ${cls} clickable" data-agent="${key}">
                    <div class="net-card-main">
                        <div class="net-card-title">
                            <span class="net-card-name">${esc(f.signature || (f.kind === 'client' ? 'unknown model client' : 'agent'))}</span>
                            ${pill}
                            ${originPill(f.origin)}
                            ${pending ? `<span class="cseries-pill warn" title="${attr('You ordered this contained ' + fmtRel(pending) + '. It is carried out on the bastion\'s next check-in that can see its agents; the bastion card says whether that is now.')}">containment pending</span>` : ''}
                            ${f.active ? '' : '<span class="cseries-pill">idle</span>'}
                        </div>
                        <div class="net-card-sub">${esc(sub)} · seen ${esc(fmtRel(f.last_seen))}</div>
                    </div>
                    <div class="net-card-actions">${actions}
                    </div>
                </div>`;
    }

    function renderAgents() {
        const unrequested = state.agentsUnrequested || 0;
        const fams = agentFamilies();
        state.agentFamilies = fams;
        const agents = fams.filter(f => f.kind !== 'app' && f.kind !== 'automation');
        const apps = fams.filter(f => f.kind === 'app');
        const automations = fams.filter(f => f.kind === 'automation');
        const sanctioned = (state.agentDelegations || []).filter(d => d.active).length;
        const scheduled = agents.filter(f => f.origin === 'scheduler').length;
        const scheduledUnrequested = agents.filter(f => f.origin === 'scheduler' && f.unrequested).length;

        const alert = $('agents-alert');
        if (unrequested > 0) {
            alert.hidden = false;
            alert.textContent = `${unrequested} unrequested ${unrequested === 1 ? 'agent' : 'agents'} — activity you didn't request.`
                + (scheduledUnrequested ? ` ${scheduledUnrequested} ${scheduledUnrequested === 1 ? 'was' : 'were'} started by a scheduler with nobody at the keyboard.` : '')
                + ' Sanction it with a tag, or contain it.';
        } else { alert.hidden = true; }

        const cards = [
            { v: agents.length, l: agents.length === 1 ? 'agent' : 'agents' },
            { v: unrequested, l: 'unrequested', alert: unrequested > 0 },
            { v: scheduled, l: 'on a scheduler', alert: scheduled > 0 },
            { v: automations.length, l: automations.length === 1 ? 'automation' : 'automations' },
            { v: apps.length, l: apps.length === 1 ? 'app using a model' : 'apps using a model' },
            { v: sanctioned, l: sanctioned === 1 ? 'sanction tag' : 'sanction tags' },
        ];
        $('agents-stats').innerHTML = cards.map(s =>
            `<div class="net-stat"><span class="net-stat-val"${s.alert ? ' style="color:#c0492f"' : ''}>${esc(s.v)}</span><span class="net-stat-lbl label-sys">${esc(s.l)}</span></div>`
        ).join('');

        const list = $('agents-list');
        list.innerHTML = agents.length ? agents.map(familyCard).join('')
            : empty('No agent activity observed',
                "When an AI agent runs on a machine your bastions watch, it appears here — what it is, what it's talking to, and whether you sanctioned it.");
        const appList = $('apps-list');
        if (appList) {
            appList.innerHTML = apps.length ? apps.map(familyCard).join('')
                : empty('No apps talking to a model', 'A desktop chat app, an IDE or a browser tab that talks to a model shows up here. It is yours — no alert, no contain.');
        }
        const autoList = $('automations-list');
        if (autoList) {
            autoList.innerHTML = automations.length ? automations.map(familyCard).join('')
                : empty('No automations observed', 'A browser driver, an input injector or an RPA runtime with no model behind it shows up here. Inventory, not an alert — the moment one starts talking to a model it moves up to Agents.');
        }
        renderScheduled();
    }

    // Scheduled jobs (bastion 0.13.12): the crontab / timer / launchd / Task
    // Scheduler entries on each machine whose command would run an agent or an
    // automation — inventoried BEFORE they fire. The scan report says what was
    // read, so "nothing scheduled" from a box that read 40 entries is a
    // different sentence from a box that could read none.
    function renderScheduled() {
        const list = $('scheduled-list');
        if (!list) return;
        const bastions = state.bastions || [];
        const rows = [];
        const scans = [];
        bastions.forEach(b => {
            const note = schedulerScanNote(b);
            if (note) scans.push(note);
            (b.schedulers || []).forEach(e => rows.push({ ...e, hostname: b.hostname || b.name }));
        });
        const note = scans.length ? `<p class="view-sub">${esc(scans.join(' · '))}</p>` : '';
        if (!rows.length) {
            list.innerHTML = note + empty('No scheduled agents', 'When a crontab, a systemd timer, a launchd job or a scheduled task on one of your machines would run an agent or an automation, it appears here before it ever fires.');
            return;
        }
        list.innerHTML = note + rows.map(e => `
                <div class="net-card ${e.automation ? '' : 'down'}">
                    <div class="net-card-main">
                        <div class="net-card-title">
                            <span class="net-card-name">${esc(e.signature || 'agent')}</span>
                            <span class="cseries-pill ${e.automation ? '' : 'deny'}">${esc(e.automation ? 'automation' : 'agent')} · ${esc(e.kind)}</span>
                        </div>
                        <div class="net-card-sub">${esc([e.hostname, e.schedule ? ('runs ' + e.schedule) : null, e.user ? ('user ' + e.user) : null, e.name].filter(Boolean).join(' · '))}</div>
                        <div class="net-card-sub"><code>${esc(e.command)}</code></div>
                    </div>
                </div>`).join('');
    }

    function renderDelegations() {
        const list = $('del-list');
        const ds = state.agentDelegations || [];
        if (!ds.length) {
            list.innerHTML = empty('No delegation tags',
                'Sanction an agent to mark its activity as requested. Mint a tag, then launch the agent with its PRIVATAE_AGENT_TOKEN.');
            return;
        }
        list.innerHTML = ds.map(d => {
            const exp = fmtExpiry(d.expires_at);
            const st = d.active ? 'active' : (d.revoked_at ? 'revoked' : 'denied');
            return `
                <div class="net-card ${d.active ? '' : 'stale'}">
                    <div class="net-card-main">
                        <div class="net-card-title">
                            <span class="net-card-name">${esc(d.server === '*' ? 'any server' : d.server)}</span>
                            ${statusPill(d.active ? 'active' : st)}
                        </div>
                        <div class="net-card-sub">${esc(d.note || 'no note')} · ${esc(exp)}</div>
                    </div>
                    <div class="net-card-actions">
                        ${d.active ? `<button class="cseries-btn ghost" data-del-revoke="${attr(d.id)}">Revoke</button>` : ''}
                    </div>
                </div>`;
        }).join('');
    }

    function findFamily(key) {
        return (state.agentFamilies || agentFamilies()).find(f => f.key === key);
    }
    function openFamilyInspector(f) {
        const a = f.rep;
        const n = f.members.length;
        const status = f.kind === 'app' ? 'an app you are running — shown, never alerted on, never contained'
            : f.kind === 'automation' ? 'an automation with no model behind it — inventory, not an alert; containable'
            : (f.requested ? 'requested — you sanctioned this'
                : (f.acknowledged ? 'unrequested — acknowledged (noted, not sanctioned)'
                    : "UNREQUESTED — you didn't ask for this"));
        const pids = f.members.map(m => m.pid).join(', ');
        const rows = [
            ['status', status],
            ['kind', f.kind === 'app' ? 'app' : f.kind === 'automation' ? 'automation (drives the machine, no model)' : (f.kind === 'client' ? 'unknown process talking to a model' : 'agent')],
            ['started', f.origin ? (ORIGIN_WORDS[f.origin] || f.origin) : 'not reported (bastion before 0.13.12)'],
            ['server', f.hostname || '—'],
            [n === 1 ? 'pid' : 'processes', n === 1 ? String(a.pid) : (n + ' — pids ' + pids), n > 1],
            ['user', f.user || '—'],
            ['command', a.cmdline || '—', true],
            ['talking to', f.endpoints.length ? f.endpoints.join(', ') : 'no LLM endpoint seen yet', true],
        ];
        if (f.evidence && f.evidence.length) {
            // The bastion's typed reasons, tier first: this is WHY, not a verdict.
            rows.push(['evidence', f.evidence.map(e => `${e.tier}: ${e.signal}${e.value ? ' — ' + e.value : ''}`).join('\n'), true]);
        }
        if (f.kind !== 'app' && f.kind !== 'automation') {
            rows.push(['delegation tag', a.delegation_token ? (f.requested ? 'valid' : 'present but invalid/expired') : 'none']);
        }
        rows.push(['first seen', fmtRel(f.first_seen)], ['last seen', fmtRel(f.last_seen)]);
        const pending = containPendingSince(f);
        if (pending) rows.push(['containment', 'ordered ' + fmtRel(pending) + " — waiting for a check-in on which the bastion can see its agents; lapses after an hour (see the management audit)"]);
        const actions = f.kind === 'app' ? [] : [
            ...(f.unrequested ? [{ label: 'Acknowledge', cls: 'ghost', onClick: () => acknowledgeFamily(f) }] : []),
            ...(pending ? [] : [{ label: n === 1 ? 'Contain (terminate)' : ('Contain all ' + n + ' (terminate)'), cls: 'danger', onClick: () => containFamily(f) }]),
        ];
        openInspector(f.signature || (f.kind === 'client' ? 'unknown model client' : 'agent'), rows, actions);
    }

    async function containFamily(f) {
        const n = f.members.length;
        const name = (f.signature || 'agent') + (n === 1 ? ' (pid ' + f.rep.pid + ')' : ' (' + n + ' processes)');
        if (!confirm('Contain ' + name + ' on ' + (f.hostname || 'the bastion') + "? The bastion terminates it — and every process it started — on its next check-in. It refuses anything that isn't a detected agent, and it never terminates an app.")) return;
        const errors = [];
        for (const m of f.members) {
            try {
                await fetchJSON('/api/network/agents/contain', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token_id: m.token_id, pid: m.pid }),
                });
            } catch (e) { errors.push('pid ' + m.pid + ': ' + e.message); }
        }
        closeInspector();
        if (errors.length) alert('Could not contain ' + (errors.length === n ? name : 'part of ' + name) + '. ' + errors.join(' · '));
        else alert(name + " will be contained on the bastion's next check-in.");
    }

    async function acknowledgeFamily(f) {
        // Low-stakes (no confirm): note it as seen, drop it from the alert count, keep it unrequested.
        const errors = [];
        for (const m of f.members) {
            if (m.requested || m.acknowledged) continue;
            try {
                await fetchJSON('/api/network/agents/acknowledge', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token_id: m.token_id, pid: m.pid }),
                });
            } catch (e) { errors.push('pid ' + m.pid + ': ' + e.message); }
        }
        closeInspector();
        state.loaded.agents = false;
        loadAgents();
        if (errors.length) alert('Could not acknowledge. ' + errors.join(' · '));
    }

    async function mintDelegation() {
        const server = (prompt("Sanction agents on which server? Use the bastion's hostname, or * for any.", '*') || '').trim();
        if (server === '') return;
        const note = (prompt('Note (optional) — what is this for?', '') || '').trim();
        let res;
        try {
            res = await fetchJSON('/api/network/agent-delegations', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ server: server, note: note || undefined }),
            });
        } catch (e) { alert('Could not mint tag. ' + e.message); return; }
        const envLine = res.env || ('PRIVATAE_AGENT_TOKEN=' + (res.token || ''));
        openInspector('Delegation tag — shown once', [
            ['⚠ one time', 'Copy this now. Launch your agent with it in the environment and its activity reads as requested.'],
            ['server', res.server === '*' ? 'any server' : (res.server || '*')],
            ['env', envLine, true],
            ['tag', res.expires ? fmtExpiry(res.expires) : 'no expiry'],
        ], [
            { label: 'Copy env line', cls: 'primary', onClick: (e) => _copyBtn(e, envLine) },
        ]);
        state.loaded.agents = false;
        loadAgents();
    }

    async function revokeDelegation(id) {
        if (!confirm('Revoke this delegation tag? Agents carrying it become unrequested on the next check-in.')) return;
        try {
            await fetchJSON('/api/network/agent-delegations/' + encodeURIComponent(id) + '/revoke', { method: 'POST' });
            state.loaded.agents = false;
            loadAgents();
        } catch (e) { alert('Could not revoke. ' + e.message); }
    }

    // ════════════════════ PEERS ════════════════════
    async function loadPeers() {
        $('peers-secured').innerHTML = skeletonRows(2);
        $('peers-contacts').innerHTML = skeletonRows(2);
        $('peers-rules').innerHTML = skeletonRows(1);
        let domainErr = null;
        try {
            const [p, dp] = await Promise.all([
                fetchJSON('/api/network/peers'),
                fetchJSON('/api/domain-policy').catch((e) => { domainErr = e.message || 'the request failed'; return null; }),
            ]);
            state.peers = p || { contacts: [], secured_channels: [] };
            state.domain = dp;
            state.domainError = domainErr;
            state.loaded.topology = true;
        } catch (e) {
            $('peers-secured').innerHTML = errBox(e.message);
            $('peers-contacts').innerHTML = '';
            $('peers-rules').innerHTML = '';
            return;
        }
        renderPeerPostures();
        renderRules();
        renderSecured();
        renderContacts();
    }

    // ── Per-peer rules (moved from Circles' Domain policy pane). GET
    // /api/domain-policy carries them beside the posture;
    // POST and DELETE /api/domain-policy/rules change them (write:domain_policy).
    async function reloadRules() {
        try {
            state.domain = await fetchJSON('/api/domain-policy');
            state.domainError = null;
        } catch (e) { state.domainError = e.message || 'the request failed'; }
        renderPeerPostures();
        renderRules();
    }
    function renderRules() {
        const list = $('peers-rules');
        if (!state.domain) {
            list.innerHTML = errBox("Couldn't read your rules: " + (state.domainError || 'no answer'));
            return;
        }
        const rules = state.domain.rules || [];
        if (!rules.length) {
            list.innerHTML = empty('No per-peer rules', 'The setting above applies to everyone.');
            return;
        }
        list.innerHTML = rules.map(r => `
            <div class="net-card net-rule" data-rule="${attr(r.id)}">
                <div class="net-card-main">
                    <div class="net-card-title"><span class="net-card-name net-rule-peer">${esc(r.peer)}</span>${r.allow_inbox
                        ? '<span class="cseries-pill allow">allow</span>' : '<span class="cseries-pill deny">deny</span>'}</div>
                    ${r.note ? `<div class="net-card-sub">${esc(r.note)}</div>` : ''}
                </div>
                <div class="net-card-actions">
                    <button class="cseries-btn ghost" type="button" data-rule-delete="${attr(r.id)}" aria-label="${attr('Delete the rule for ' + r.peer)}">Delete</button>
                </div>
            </div>`).join('');
    }
    function rulesStatus(text, cls) {
        const st = $('peers-rules-status');
        st.hidden = !text;
        st.className = 'net-inline-status' + (cls ? ' ' + cls : '');
        st.textContent = text || '';
    }
    async function deleteRule(id) {
        const rule = ((state.domain && state.domain.rules) || []).find(r => String(r.id) === String(id));
        if (!rule) return;
        const what = rule.allow_inbox
            ? 'What it sends then follows the setting above.'
            : 'What it sends then follows the setting above, so it may reach you again.';
        if (!confirm(`Delete the rule for ${rule.peer}? ${what}`)) return;
        rulesStatus('Deleting…');
        try {
            await fetchJSON('/api/domain-policy/rules/' + encodeURIComponent(rule.id), { method: 'DELETE' });
            rulesStatus('Deleted the rule for ' + rule.peer + '.', 'success');
        } catch (e) {
            rulesStatus("Couldn't delete the rule: " + e.message, 'error');
        }
        await reloadRules();
    }
    async function addRule(ev) {
        ev.preventDefault();
        const input = $('peers-rule-peer');
        const peer = input.value.trim();
        if (!peer) { input.focus(); return; }
        const allow = $('peers-rule-allow').value === 'allow';
        const btn = $('peers-rule-submit');
        btn.disabled = true;
        rulesStatus('Saving…');
        try {
            await fetchJSON('/api/domain-policy/rules', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ peer, allow_inbox: allow, allow_dm: allow, allow_circle_invite: allow }),
            });
            input.value = '';
            rulesStatus((allow ? 'Allowed ' : 'Denied ') + peer + '.', 'success');
        } catch (e) {
            rulesStatus("Couldn't save the rule: " + e.message, 'error');
        }
        btn.disabled = false;
        await reloadRules();
    }

    const REACH = [
        { id: 'open', name: 'Open', desc: 'Any Host on the network can message you. Per-peer rules can still block specific senders.' },
        { id: 'contacts', name: 'Contacts only', desc: 'Only Hosts you allow can reach you.' },
        { id: 'closed', name: 'Closed', desc: 'No cross-Host messages land at all.' },
    ];
    function renderPeerPostures() {
        const cur = state.domain && state.domain.posture;
        $('peers-postures').innerHTML = REACH.map(p =>
            `<button class="net-posture ${p.id === cur ? 'active' : ''}" data-reach="${p.id}">
                <span class="p-name">${esc(p.name)}</span><span class="p-desc">${esc(p.desc)}</span>
            </button>`).join('');
    }
    async function setReach(posture) {
        const st = $('peers-posture-status');
        st.hidden = false; st.className = 'net-inline-status'; st.textContent = 'Updating…';
        try {
            await fetchJSON('/api/domain-policy/posture', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ posture }),
            });
            state.domain = state.domain || {}; state.domain.posture = posture;
            renderPeerPostures();
            st.className = 'net-inline-status success'; st.textContent = 'Reachability set to ' + posture + '.';
        } catch (e) { st.className = 'net-inline-status error'; st.textContent = 'Could not update: ' + e.message; }
    }

    // ── The contact card for another Host ───────────────
    // One resolver on the Host (POST /api/hosts/cards) — the same card Circles
    // shows. Never a bare "Host 9163" or handle where the Host knows more.
    const _cards = new Map();
    // fresh: read these cards again even if one is held, because a card's
    // relationship moves (a request sent, a contact accepted or blocked in
    // Circles) and a machine's Host row picks its button from it. A held card
    // is replaced only by a newer answer, never dropped by a failed read.
    const CARDS_PER_READ = 500;   // the resolver's own ceiling (server/routes/host_cards.py)
    async function hostCards(ids, fresh) {
        const want = [...new Set((ids || []).map(Number).filter(n => n > 0 && (fresh || !_cards.has(n))))];
        for (let i = 0; i < want.length; i += CARDS_PER_READ) {
            try {
                const r = await fetchJSON('/api/hosts/cards', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ids: want.slice(i, i + CARDS_PER_READ) }),
                });
                for (const [k, v] of Object.entries((r && r.cards) || {})) _cards.set(Number(k), v);
            } catch (e) { /* the list keeps what it already shows */ }
        }
        return (ids || []).map(i => _cards.get(Number(i))).filter(Boolean);
    }
    function cardFields(c) {
        return [
            ...(c.mine ? [['yours', 'Your host (same account)']] : []),
            ['host', c.number],
            ['classification', c.classification],
            ['contact number', c.contact_number || 'Not shared'],
            ['relationship', c.relationship_label],
            ...(c.connection_label && c.connection_label !== c.relationship_label
                ? [['connection', c.connection_label]] : []),
            ['user', c.user || 'Not shared'],
            ...(c.address ? [['address', c.address, true]] : []),
        ];
    }

    function renderSecured() {
        const list = $('peers-secured');
        const chans = state.peers.secured_channels || [];
        if (!chans.length) { list.innerHTML = empty('No secured channels', 'When another Host grants you a permission slip, the cryptographic channel appears here.'); return; }
        list.innerHTML = chans.map(s => {
            const exp = fmtExpiry(s.expires_at);
            return `
                <div class="net-card clickable" data-peer="${attr(s.peer_entity_id)}">
                    <div class="net-card-main">
                        <div class="net-card-title"><span class="net-card-name" data-host-name="${attr(s.peer_entity_id)}">Host #${esc(s.peer_entity_id)}</span><span class="cseries-pill dusk">secured</span></div>
                        <div class="net-card-sub">${esc(s.contract_id || 'slip')} · ${esc(exp)}</div>
                    </div>
                </div>`;
        }).join('');
        // Name each channel's Host from its card once the cards arrive.
        hostCards(chans.map(c => c.peer_entity_id)).then(cards => {
            for (const c of cards) {
                const el = list.querySelector(`[data-host-name="${c.host_id}"]`);
                if (el) el.textContent = c.display_name;
            }
        });
    }
    function renderContacts() {
        const list = $('peers-contacts');
        const cs = state.peers.contacts || [];
        if (!cs.length) { list.innerHTML = empty('No contacts', 'Add Hosts by handle in Circles, or accept a connection request — they show up here.'); return; }
        list.innerHTML = cs.map(c => `
            <div class="net-card clickable" data-contact="${attr(c.id)}">
                <div class="net-card-main">
                    <div class="net-card-title"><span class="net-card-name">${esc(c.display_name || c.handle)}</span>${statusPill(c.status)}<span class="cseries-pill">${esc(c.kind)}</span></div>
                    <div class="net-card-sub">${esc(c.handle)} · last contact ${esc(fmtRel(c.last_contact_at))}</div>
                </div>
            </div>`).join('');
    }
    async function openPeerInspector(s) {
        const [card] = await hostCards([s.peer_entity_id]);
        openInspector(card ? card.display_name : `Host #${s.peer_entity_id}`, [
            ...(card ? cardFields(card) : [['host', '#' + String(s.peer_entity_id)]]),
            ['channel', 'secured (permission slip)'],
            ['contract', s.contract_id || '—', true],
            ['slip', fmtExpiry(s.expires_at)],
            ['updated', fmtRel(s.updated_at)],
        ], []);
    }
    async function openContactInspector(c) {
        const hostId = c.kind === 'host' ? c.peer_entity_id : null;
        const [card] = hostId ? await hostCards([hostId]) : [];
        openInspector(c.display_name || c.handle, [
            ...(card ? cardFields(card) : []),
            ['handle', c.handle, true],
            ['kind', c.kind],
            ['status', c.status],
            ['notes', c.notes || '—'],
            ['last contact', fmtRel(c.last_contact_at)],
            ['added', fmtRel(c.created_at)],
        ], []);
    }

    // (The egress-policing lens was removed. Egress is the user's plane,
    // governed host-local by CEIGAS — not a platform allowlist the platform
    // enforces or audits. The platform signs the doors and meters throughput;
    // it does not stand in the traffic.)

    // ════════════════════ INSPECTOR ════════════════════
    // fields: [label, val, mono?] OR [label, rawHTML, mono?, true] — the 4th element,
    // when truthy, marks `val` as pre-built, trusted HTML (chips/links) and skips esc().
    function openInspector(title, fields, actions) {
        $('inspector-title').textContent = title;
        $('inspector-body').innerHTML = (fields || []).map(([label, val, mono, raw]) =>
            `<div class="net-field"><span class="net-field-label">${esc(label)}</span><span class="net-field-val ${mono ? 'mono' : ''}">${raw ? val : esc(val)}</span></div>`
        ).join('');
        const actEl = $('inspector-actions');
        actEl.innerHTML = '';
        (actions || []).forEach(a => {
            const btn = document.createElement('button');
            btn.className = 'cseries-btn ' + (a.cls || '');
            btn.textContent = a.label;
            btn.addEventListener('click', a.onClick);
            actEl.appendChild(btn);
        });
        $('inspector').hidden = false;
    }
    function closeInspector() { $('inspector').hidden = true; }

    // ════════════════════ SET UP A BASTION ════════════════════
    // The host mints a single-use, short-lived PAIRING TOKEN (shown once). The user
    // installs the generic bastion binary and runs its `pair <token>` command,
    // which generates its OWN keys and pulls its cert + routing from this host — no
    // bundle, no private key ever leaves the bastion. Copy is robust to the dashboard
    // iframe (the async clipboard API is often blocked there → execCommand fallback →
    // last-resort prompt() so the token is never un-copyable).
    function _copyFallback(text) {
        try {
            const ta = document.createElement('textarea');
            ta.value = text; ta.setAttribute('readonly', '');
            ta.style.position = 'fixed'; ta.style.top = '-1000px'; ta.style.opacity = '0';
            document.body.appendChild(ta); ta.select();
            const ok = document.execCommand('copy');
            document.body.removeChild(ta);
            return ok;
        } catch (e) { return false; }
    }
    function _copy(text) {
        if (!text) return false;
        try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).catch(function () { _copyFallback(text); });
                return true;
            }
        } catch (e) {}
        return _copyFallback(text);
    }
    function _copyBtn(e, text) {
        const ok = _copy(text);
        if (ok && e && e.target) {
            const b = e.target, o = b.textContent;
            b.textContent = 'Copied ✓';
            setTimeout(function () { b.textContent = o; }, 1500);
        } else if (!ok) {
            try { window.prompt('Copy this — Cmd/Ctrl-C:', text); } catch (_) {}
        }
        return ok;
    }

    async function setupBastion() {
        // No name prompt — the bastion reports its own hostname on first check-in,
        // which is the authoritative, more-correct label. Until then it shows as
        // "bastion" (server default).
        openInspector('Setting up bastion…', [['status', 'Generating WireGuard config + identity…']], []);
        let res;
        try {
            res = await fetchJSON('/api/network/bastions/setup', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
        } catch (e) {
            openInspector('Set up a bastion', [['could not issue', e.message]], []);
            return;
        }
        const run = res.run || {};
        const mins = Math.max(1, Math.round((res.expires_in_s || 900) / 60));
        const pairCmd = run.pair || ('privatae-bastion pair ' + (res.token || ''));
        // Step 3 is OPTIONAL and most machines never need it — but it was missing
        // entirely, so a user setting up a machine other people's hosts pair to had
        // nowhere to learn that `set-mode networked` and `claim-anchor` exist: the
        // word "anchor" appears in this app only as a state to DISPLAY. The server
        // owns the wording (bastion_enroll.mint_pairing_token) so the agent that
        // explains this and the card that shows it cannot drift apart.
        const rows = [
            ['⚠ one time', 'This pairing token is single-use and expires in ' + mins + ' min. It enrolls exactly one machine into your network.'],
            ['token', res.token || '—', true],
            ['1 · install', run.install || 'macOS / Linux:  curl -fsSL https://privatae.ai/bastion/install.sh | sh', true],
            ['2 · pair', pairCmd, true],
            ['then', run.note || 'It brings up the tunnel and opens the lanes this host uses to reach that machine.'],
        ];
        if (run.share) rows.push(['3 · share it (optional)', run.share, true]);
        if (run.elevate) rows.push(['as administrator', run.elevate]);
        openInspector('Bastion ready — token shown once', rows, [
            { label: 'Copy token', cls: 'primary', onClick: (e) => _copyBtn(e, res.token || '') },
            { label: 'Copy pair command', onClick: (e) => _copyBtn(e, pairCmd) },
        ]);
        // The new bastion (offline until it checks in) appears in the list now.
        // The card is shown once and must not wait on the reload (fetchJSON has no
        // timeout; a hung /api/network/bastion-latest would hide the token exactly
        // the way an earlier close did), so the card opens first and the reload
        // is told to leave it alone. loadBastions' own comment has the history.
        state.loaded.topology = false;
        loadBastions({ keepInspector: true });
        loadOverview();
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('.net-rail-btn').forEach(b =>
            b.addEventListener('click', () => showLens(b.dataset.lens)));

        $('inspector-close').addEventListener('click', closeInspector);

        // Topology scene: delegate node clicks + keyboard activation
        const scene = $('topo-scene');
        scene.addEventListener('click', (e) => {
            const g = e.target.closest('[data-node]'); if (g) onSceneClick(g.dataset.node);
        });
        scene.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter' && e.key !== ' ') return;
            const g = e.target.closest('[data-node]'); if (g) { e.preventDefault(); onSceneClick(g.dataset.node); }
        });

        // Map view picker — swaps the renderer over the same graph. Remembered,
        // so the view a user prefers is the one they get next time.
        const pick = $('topo-viewpick');
        if (pick) pick.addEventListener('click', (e) => {
            const b = e.target.closest('[data-view]');
            if (!b || !VIEWS[b.dataset.view]) return;
            setViewName(b.dataset.view);
            drawScene();
        });

        // Bastions
        $('gw-refresh').addEventListener('click', () => { state.loaded.topology = false; loadBastions(); });
        $('gw-update-all').addEventListener('click', updateAllBastions);
        $('autoupdate-toggle').addEventListener('click', toggleAutoUpdate);
        $('gw-add').addEventListener('click', setupBastion);
        $('gw-list').addEventListener('click', (e) => {
            // A machine's other Hosts: their card, Request connection, Message.
            const hr = e.target.closest('[data-host-request]');
            if (hr) { e.stopPropagation(); requestHost(hr.getAttribute('data-host-request')); return; }
            const hm = e.target.closest('[data-host-message]');
            if (hm) { e.stopPropagation(); messageHost(hm.getAttribute('data-host-message')); return; }
            const hc = e.target.closest('[data-host-card]');
            if (hc) { e.stopPropagation(); openHostCard(hc.getAttribute('data-host-card')); return; }
            // Anywhere else in that list is not a click on the machine.
            if (e.target.closest('.net-hosts')) return;
            // Pairing actions live on the card itself (approve / deny / revoke).
            const pa = e.target.closest('[data-pair-approve],[data-pair-deny],[data-pair-revoke]');
            if (pa) {
                e.stopPropagation();
                const subject = pa.getAttribute('data-pair-subject');
                const server = pa.getAttribute('data-pair-approve') || pa.getAttribute('data-pair-deny') || pa.getAttribute('data-pair-revoke');
                const g = state.bastions.find(x => x.hostname === server);
                const p = g && (g.pairings || []).find(x => x.host_name === subject);
                const handle = p ? (p.handle || '') : '';
                if (pa.hasAttribute('data-pair-approve')) approvePairing(server, subject, handle);
                else if (pa.hasAttribute('data-pair-deny')) denyPairing(server, subject, handle);
                else revokePairing(server, subject, handle);
                return;
            }
            const upd = e.target.closest('[data-gw-update]');
            if (upd) {
                const g = state.bastions.find(x => String(x.id) === String(upd.getAttribute('data-gw-update')));
                if (g) updateBastion(g);
                return;
            }
            const open = e.target.closest('[data-gw-open]') || e.target.closest('[data-gw]');
            if (!open) return;
            const id = open.getAttribute('data-gw-open') || open.getAttribute('data-gw');
            const g = state.bastions.find(x => String(x.id) === String(id));
            if (g) openBastionInspector(g);
        });

        // Agents
        $('agents-refresh').addEventListener('click', () => { state.loaded.agents = false; loadAgents(); });
        $('del-add').addEventListener('click', mintDelegation);
        const onFamilyClick = (e) => {
            const ack = e.target.closest('[data-agent-ack]');
            if (ack) { const f = findFamily(ack.getAttribute('data-agent-ack')); if (f) acknowledgeFamily(f); return; }
            const c = e.target.closest('[data-agent-contain]');
            if (c) { const f = findFamily(c.getAttribute('data-agent-contain')); if (f) containFamily(f); return; }
            const open = e.target.closest('[data-agent]');
            if (!open) return;
            const f = findFamily(open.getAttribute('data-agent'));
            if (f) openFamilyInspector(f);
        };
        $('agents-list').addEventListener('click', onFamilyClick);
        const appsList = $('apps-list');
        if (appsList) appsList.addEventListener('click', onFamilyClick);
        $('del-list').addEventListener('click', (e) => {
            const r = e.target.closest('[data-del-revoke]');
            if (r) revokeDelegation(r.getAttribute('data-del-revoke'));
        });
        // The header "agents" chip (shown when there's unrequested activity) jumps to the lens.
        $('net-health').addEventListener('click', (e) => {
            if (e.target.closest('[data-go-agents]')) showLens('agents');
        });

        // NAC — approve/decline/remove/revoke + suspend/resume are delegated across all driven
        // blocks; each button carries a "kind:id" token (+ data-widen) so one listener routes them.
        function nacClickHandler(e) {
            const si = e.target.closest('[data-mcp-signin]');
            if (si) { mcpSignIn(si.getAttribute('data-mcp-signin')); return; }
            const so = e.target.closest('[data-mcp-signout]');
            if (so) { mcpSignOut(so.getAttribute('data-mcp-signout')); return; }
            const enroll = e.target.closest('[data-nac-enroll]');
            if (enroll) { enrollStepup(); return; }
            const susp = e.target.closest('[data-nac-suspend]');
            if (susp) { suspendServer(susp.getAttribute('data-nac-suspend')); return; }
            const res = e.target.closest('[data-nac-resume]');
            if (res) { resumeServer(res.getAttribute('data-nac-resume')); return; }
            const qtn = e.target.closest('[data-nac-quarantine]');
            if (qtn) { quarantineServer(qtn.getAttribute('data-nac-quarantine')); return; }
            const unq = e.target.closest('[data-nac-unquarantine]');
            if (unq) { unquarantineServer(unq.getAttribute('data-nac-unquarantine')); return; }
            const map = [['data-nac-approve', 'approve'], ['data-nac-decline', 'decline'],
                         ['data-nac-remove', 'remove'], ['data-nac-revoke', 'revoke']];
            for (const [da, action] of map) {
                const btn = e.target.closest('[' + da + ']');
                if (btn) {
                    const [kind, id] = btn.getAttribute(da).split(':');
                    const widening = btn.getAttribute('data-widen') === '1';
                    nacDispatch(kind, action, id, widening);
                    return;
                }
            }
        }
        ['nac-pending-list', 'nac-ingress-list', 'nac-connector-list', 'nac-egress-list', 'nac-grants-list', 'nac-posture-grid'].forEach(idv => {
            const el = $(idv); if (el) el.addEventListener('click', nacClickHandler);
        });
        const sovChip = $('nac-stepup-chip');
        if (sovChip) sovChip.addEventListener('click', nacClickHandler);

        const addBtn = $('nac-egress-add');
        if (addBtn && !addBtn._wired) { addBtn._wired = true; addBtn.addEventListener('click', openAddEgress); }
        // Lane headers (Ingress / Egress / Management) — click to expand/collapse.
        document.querySelectorAll('[data-lane-toggle]').forEach(h =>
            h.addEventListener('click', () => toggleLane(h.getAttribute('data-lane-toggle'))));

        // Centered step-up/enrollment modal: close on ✕, on scrim-backdrop click, and on Escape.
        $('nac-modal-close').addEventListener('click', closeModal);
        $('nac-scrim').addEventListener('click', (e) => { if (e.target === $('nac-scrim')) closeModal(); });
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('nac-scrim').hidden) closeModal(); });

        // Peers
        $('peers-postures').addEventListener('click', (e) => {
            const b = e.target.closest('[data-reach]'); if (b) setReach(b.dataset.reach);
        });
        $('peers-rules').addEventListener('click', (e) => {
            const d = e.target.closest('[data-rule-delete]'); if (d) deleteRule(d.getAttribute('data-rule-delete'));
        });
        $('peers-rule-add').addEventListener('submit', addRule);
        $('peers-secured').addEventListener('click', (e) => {
            const c = e.target.closest('[data-peer]'); if (!c) return;
            const s = (state.peers.secured_channels || []).find(x => String(x.peer_entity_id) === c.dataset.peer);
            if (s) openPeerInspector(s);
        });
        $('peers-contacts').addEventListener('click', (e) => {
            const c = e.target.closest('[data-contact]'); if (!c) return;
            const ct = (state.peers.contacts || []).find(x => String(x.id) === c.dataset.contact);
            if (ct) openContactInspector(ct);
        });

        // A view sent while the window is already open arrives as a message;
        // only the framing desktop may send one.
        window.addEventListener('message', (ev) => {
            if (ev.source !== window.parent) return;
            const d = ev.data || {};
            if (d.type === 'cseries:app-view' && typeof d.view === 'string') applyView(d.view);
        });

        // First paint + live polling of the cheap overview.
        loadOverview();
        const initialView = viewFromHash();
        if (bastionFromView(initialView) !== null) applyView(initialView);
        else showLens('topology');
        setInterval(() => { loadOverview(); }, 20000);
    });

    // buildGraph + VIEWS are exported deliberately: they are the view-model and
    // the renderer registry, so a renderer can be swapped, added, or exercised
    // headlessly (scripts/test_network_views.js) without booting the lens.
    // openBastionInspector and setupBastion are exported for the headless gate
    // (scripts/test_network_anchor_step.js): the anchor instructions are the only
    // place in the product that names `set-mode` / `claim-anchor`, and a check
    // that they render — for the right machines, and on the setup card — has to
    // call the real builders rather than re-implement them.
    window.NetworkApp = {
        state, showLens, enrollStepup, buildGraph, VIEWS, registerView,
        openBastionInspector, setupBastion, bastionFromView, applyView,
        // Pure helpers, exported so scripts/test_network_contain_pending.js
        // can pin their words without a browser.
        containPendingPill, findingContainPendingPill, containPendingSince, schedulerScanNote, versionBefore,
        autoUpdateLine,
    };
})();
