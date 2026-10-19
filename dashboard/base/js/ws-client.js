/**
 * Privatae Personal Computer — WebSocket Client
 *
 * Connects to the local FastAPI WebSocket at /ws.
 * CAN-bus message protocol: typed, module-scoped messages.
 * Auto-reconnect with exponential backoff, except while the Host session is
 * known ended (scheduleReconnect): then it waits for the session to be back.
 *
 * The shell owns the pod-reset UX (2026-07-14): when the host drops, the
 * boot screen paints; when it comes back after a REAL restart (uptime
 * regressed), the boot screen holds for one full video pass while the pod
 * stabilizes, then the whole page hard-reloads onto fresh assets. Apps
 * never have to poll-and-reload on their own — the Settings updater used
 * to, and every other reset path (chat-initiated self-update, operator
 * roll, crash) left the user on a dead "Reconnecting..." desktop.
 */

const WSClient = (() => {
    let ws = null;
    let reconnectDelay = 1000;
    const MAX_RECONNECT_DELAY = 30000;
    const listeners = new Map();  // type → Set<callback>
    let cachedEntityName = null;

    // --- Pod-reset detection state ---
    let everConnected = false;        // has this page ever completed a WS open?
    let dropBeforeFirstOpen = false;  // host vanished before the first open — assets suspect
    let bootShowTimer = null;         // grace timer so network blips don't flash the boot screen
    let reloading = false;            // reload committed — ignore further socket churn
    let bootEpochMark = null;         // server boot instant (timestamp − uptime), last good fetch

    // A WS blip shorter than this never paints the boot screen; a pod roll
    // keeps the socket down for ~30s+, so real resets always clear it.
    const BOOT_GRACE_MS = 1500;
    // One full pass of the boot video. The socket reopening only proves the
    // server accepted us — the app layer is still warming. Hold the boot
    // screen this long before reloading so the fresh page lands on a pod
    // that can actually serve it.
    const BOOT_HOLD_MS = 6000;

    function connect() {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${protocol}//${location.host}/ws`;

        ws = new WebSocket(url);

        ws.addEventListener('open', async () => {
            reconnectDelay = 1000;
            if (bootShowTimer) { clearTimeout(bootShowTimer); bootShowTimer = null; }
            if (reloading) return;

            const isReconnect = everConnected;
            everConnected = true;

            const s = await fetchStatus();

            // Page painted while the host was away, or the host restarted
            // behind a reconnect: every app iframe / stream on this page is
            // dead and the cached assets may predate the new image. Hold the
            // boot screen while the pod warms, then land on a fresh load.
            if ((!isReconnect && dropBeforeFirstOpen) ||
                (isReconnect && restartedSince(s))) {
                stabilizeAndReload();
                return;
            }

            markBootEpoch(s);
            applyEntityName(s);
            updateStatus('connected');
            hideBootScreen();

            // Request initial state from the WS (kept for backward compat
            // with any server-initiated status push).
            send({ type: 'status', module: 'entity', data: { request: 'sync' } });
        });

        ws.addEventListener('message', (event) => {
            try {
                const msg = JSON.parse(event.data);
                dispatch(msg);
            } catch (e) {
                // Ignore malformed messages
            }
        });

        ws.addEventListener('close', () => {
            updateStatus('disconnected');
            if (reloading) return;
            if (!everConnected) {
                // First open never happened — the boot screen from the
                // initial markup is still up; just remember the drop.
                dropBeforeFirstOpen = true;
            } else if (!bootShowTimer) {
                bootShowTimer = setTimeout(() => {
                    bootShowTimer = null;
                    showBootScreen();
                }, BOOT_GRACE_MS);
            }
            scheduleReconnect();
        });

        ws.addEventListener('error', () => {
            updateStatus('error');
        });
    }

    // WHILE THE HOST SESSION IS KNOWN ENDED, NO RECONNECT (2026-10-07). Every
    // attempt is refused at the router, and a desktop left with its session
    // lapsed kept trying every 30 s for as long as the tab stayed open. The
    // keeper (session-keeper.js, loaded before this) announces each change
    // as a 'cseries:session' event on this window: 'expired' parks the
    // reconnect, 'ok' (the session is back) connects at once.
    let sessionEnded = false;
    let reconnectTimer = null;
    let parked = false;

    function scheduleReconnect() {
        if (sessionEnded) { parked = true; return; }
        reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
            connect();
        }, reconnectDelay);
    }

    window.addEventListener('cseries:session', (ev) => {
        const next = ev && ev.detail && ev.detail.state;
        if (next === 'expired') {
            sessionEnded = true;
            if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; parked = true; }
        } else if (next === 'ok') {
            sessionEnded = false;
            if (parked && !reloading) { parked = false; reconnectDelay = 1000; connect(); }
        }
    });

    function send(msg) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(msg));
        }
    }

    // --- Pod-reset detection ---

    async function fetchStatus() {
        try {
            const r = await fetch('/api/system/status', { cache: 'no-store' });
            if (!r.ok) return null;
            return await r.json();
        } catch (_) {
            return null;
        }
    }

    /**
     * The server's boot instant: status.timestamp − status.uptime_s. Both
     * fields come off the SAME server clock in the same payload, so the
     * value is constant for the life of a process regardless of when we
     * sample it — and moves forward the moment the pod restarts. (A pure
     * uptime-regression check misses restarts of young pods: uptime 3s →
     * roll → uptime 2s regresses by nothing.)
     */
    function bootEpochOf(s) {
        const up = s ? Number(s.uptime_s) : NaN;
        const ts = s ? Date.parse(s.timestamp) : NaN;
        if (!isFinite(up) || !isFinite(ts)) return NaN;
        return ts / 1000 - up;
    }

    function markBootEpoch(s) {
        const epoch = bootEpochOf(s);
        if (isFinite(epoch)) bootEpochMark = epoch;
    }

    /**
     * Did the host restart while we were away? No status answer right
     * after the socket opened means we can't prove continuity — reloading
     * is always safe, a stale page never is.
     */
    function restartedSince(s) {
        const epoch = bootEpochOf(s);
        if (!isFinite(epoch) || bootEpochMark === null) return true;
        // Both fields in a status payload come off the same server clock in
        // the same handler, so the only noise is uptime's 1s integer floor —
        // 3s of allowance still catches even a fast in-place restart.
        return epoch > bootEpochMark + 3;
    }

    function stabilizeAndReload() {
        if (reloading) return;
        reloading = true;
        showBootScreen();
        setTimeout(() => location.reload(), BOOT_HOLD_MS);
    }

    function applyEntityName(s) {
        if (!s || !s.entity_display_name) return;
        cachedEntityName = String(s.entity_display_name);
        const nameEl = document.getElementById('entity-name');
        if (nameEl) nameEl.textContent = cachedEntityName;
    }

    // --- Message dispatch ---

    function dispatch(msg) {
        const type = msg.type || 'unknown';

        // Notify type-specific listeners
        const cbs = listeners.get(type);
        if (cbs) {
            cbs.forEach(cb => {
                try { cb(msg); } catch (e) { /* listener error */ }
            });
        }

        // Notify wildcard listeners
        const wildcards = listeners.get('*');
        if (wildcards) {
            wildcards.forEach(cb => {
                try { cb(msg); } catch (e) { /* listener error */ }
            });
        }

        // Built-in handlers
        if (type === 'notification') {
            // Top-right toast (reuses WindowManager.notify → #notification-area:
            // fixed top-right, stacks, auto-dismiss, level styles). Agent-lifecycle
            // pushes carry the one-line summary in `message`; older senders use
            // `body` — accept either. Pops on EVERY push.
            WindowManager.notify(
                msg.data?.title || 'Notification',
                msg.data?.message || msg.data?.body || '',
                msg.data?.level || 'info'
            );
        } else if (type === 'status' && msg.module === 'entity') {
            if (msg.data?.name) {
                cachedEntityName = msg.data.name;
                const nameEl = document.getElementById('entity-name');
                if (nameEl) nameEl.textContent = msg.data.name;
            }
        } else if (type === 'theme_update') {
            // Apply CSS custom property changes from data
            if (msg.data && typeof msg.data === 'object') {
                const root = document.documentElement;
                for (const [prop, value] of Object.entries(msg.data)) {
                    if (prop.startsWith('--')) {
                        root.style.setProperty(prop, value);
                    }
                }
            }
        } else if (type === 'appearance_changed') {
            // The kernel pointed the desktop at an artifact (or cleared it) —
            // server/routes/appearance.py set_desktop_artifact. Re-read
            // appearance now: the HTML desktop's own live poll only starts once
            // a document is mounted, so the FIRST mount needs this push.
            if (typeof window.applyWallpaper === 'function') {
                try { window.applyWallpaper(); } catch (_) {}
            }
        } else if (type === 'app_installed') {
            // Refresh the launcher menu when it grows a refresh hook
            if (typeof AppLauncher !== 'undefined' && typeof AppLauncher.refresh === 'function') {
                AppLauncher.refresh();
            }
        } else if (type === 'open_app') {
            // Server-pushed window open — the MCP lane AND the host's
            // open_app meta-tool (runtime/tools.py) both broadcast here, so
            // a tool call from ANY loop (chat turn, dispatched agent) opens
            // the window. AppRunner.launch handles the same /launch flow
            // user clicks trigger; `view` deep-links (e.g. artifact:<id>).
            const appId = (msg.data && msg.data.app_id) || null;
            const view = (msg.data && typeof msg.data.view === 'string' && msg.data.view) || null;
            if (appId && typeof AppRunner !== 'undefined' &&
                typeof AppRunner.launch === 'function') {
                try { AppRunner.launch(appId, view ? { view: view } : undefined); } catch (_) {}
            }
            if (msg.data && msg.data.reason && typeof WindowManager !== 'undefined') {
                WindowManager.notify('Opened ' + appId, msg.data.reason, 'info');
            }
        } else if (type === 'close_app') {
            // close_app currently best-effort — WindowManager.close needs
            // a window id, not an app id, so we close every window whose
            // body element belongs to this app id.
            const appId = (msg.data && msg.data.app_id) || null;
            if (appId && typeof WindowManager !== 'undefined' &&
                typeof WindowManager.getAll === 'function') {
                try {
                    const all = WindowManager.getAll() || [];
                    all.forEach(w => {
                        if (w && w.appId === appId && typeof WindowManager.close === 'function') {
                            WindowManager.close(w.id);
                        }
                    });
                } catch (_) {}
            }
        }
    }

    function on(type, callback) {
        if (!listeners.has(type)) listeners.set(type, new Set());
        listeners.get(type).add(callback);
    }

    function off(type, callback) {
        const cbs = listeners.get(type);
        if (cbs) cbs.delete(callback);
    }

    // --- Boot screen ---

    function hideBootScreen() {
        const el = document.getElementById('boot-screen');
        if (!el || el.classList.contains('hidden')) return;
        el.classList.add('hidden');
    }

    function showBootScreen() {
        const el = document.getElementById('boot-screen');
        if (!el) return;
        el.classList.remove('hidden');
    }

    // --- Status indicator ---

    function updateStatus(status) {
        const dot = document.querySelector('.status-dot');
        const nameEl = document.getElementById('entity-name');

        if (dot) {
            dot.classList.remove('connected', 'error');
            if (status === 'connected') dot.classList.add('connected');
            else if (status === 'error') dot.classList.add('error');
        }

        if (nameEl) {
            if (status === 'disconnected' || status === 'error') {
                nameEl.textContent = 'Reconnecting...';
            } else if (status === 'connected' && cachedEntityName) {
                nameEl.textContent = cachedEntityName;
            }
        }
    }

    // --- Init ---

    document.addEventListener('DOMContentLoaded', () => {
        connect();
    });

    return { send, on, off, showBootScreen, hideBootScreen };
})();

window.WSClient = WSClient;
