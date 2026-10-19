/**
 * Desktop restore — window layout that survives reloads.
 *
 * The WindowManager keeps every window in an in-memory Map, so ANY reload —
 * manual, the session-expired bootstrap round-trip, or the shell's own
 * pod-restart hard reload (ws-client.js stabilizeAndReload) — used to cost
 * the whole desktop. This module persists the layout of every APP window
 * (the ones AppRunner can relaunch) to localStorage and rebuilds it on boot.
 *
 * Saved per window: appId, floating geometry, minimized/maximized, z-order.
 * Restore relaunches each app through AppRunner.launch({geometry}) in
 * z-order, so stacking comes back too. Windows without an appId (ad-hoc
 * shell windows) can't be relaunched and are not saved. App-internal state
 * is each app's own concern — the workbench/circles threads reload from
 * server history.
 *
 * Persistence is per-browser (localStorage). Best-effort by design: a full
 * storage quota or a vanished app must never break the desktop.
 */

const DesktopRestore = (() => {
    const KEY = 'cseries.windows.v1';
    let restoring = false;
    let saveTimer = null;

    function currentLayout() {
        if (!window.WindowManager) return [];
        return window.WindowManager.getAll()
            .filter((w) => w.appId)
            .map((w) => {
                // Maximized/minimized windows keep their floating geometry in
                // w.restored; a visible floating window's element rect is the
                // live truth (restored is only stamped on drag/resize END).
                let g;
                if (w.maximized || w.minimized) {
                    g = w.restored || {};
                } else {
                    // Layer coordinates, the space left/top are restored in.
                    if (typeof window.WindowManager.localRect === 'function') {
                        g = window.WindowManager.localRect(w.el);
                    } else {
                        const r = w.el.getBoundingClientRect();
                        g = { x: r.left, y: r.top, w: r.width, h: r.height };
                    }
                }
                return {
                    appId: w.appId,
                    x: Math.round(g.x || 0),
                    y: Math.round(g.y || 0),
                    w: Math.round(g.w || 0),
                    h: Math.round(g.h || 0),
                    minimized: !!w.minimized,
                    maximized: !!w.maximized,
                    z: parseInt(w.el.style.zIndex, 10) || 0,
                };
            })
            .sort((a, b) => a.z - b.z);
    }

    function save() {
        if (restoring) return; // never snapshot a half-restored desktop
        try {
            localStorage.setItem(
                KEY,
                JSON.stringify({ savedAt: Date.now(), windows: currentLayout() }),
            );
        } catch (_) {
            /* quota/denied — persistence is best-effort */
        }
    }

    function scheduleSave() {
        if (saveTimer) clearTimeout(saveTimer);
        saveTimer = setTimeout(() => {
            saveTimer = null;
            save();
        }, 500);
    }

    async function restore() {
        let saved = null;
        try {
            saved = JSON.parse(localStorage.getItem(KEY) || 'null');
        } catch (_) {
            /* corrupt entry — fall through to nothing */
        }
        const wins = saved && Array.isArray(saved.windows) ? saved.windows : [];
        if (wins.length === 0) return;

        // Skip apps that are no longer installed — a boot full of "Could not
        // open app" toasts for a stale entry would be worse than the loss.
        let installed = null;
        try {
            const res = await fetch('/api/applications');
            if (res.ok) {
                const data = await res.json();
                installed = new Set((data.applications || []).map((a) => a.id));
            }
        } catch (_) {
            /* list unavailable — try the launches anyway */
        }

        restoring = true;
        try {
            for (const w of wins) { // ascending z → stacking order comes back
                if (!w || !w.appId) continue;
                if (installed && !installed.has(w.appId)) continue;
                // Clamp into THIS viewport — a layout saved on a larger
                // monitor must not come back as an off-screen window.
                const maxX = Math.max(0, window.innerWidth - 160);
                const maxY = Math.max(0, window.innerHeight - 120);
                const g = {
                    ...w,
                    x: Math.min(Math.max(0, w.x), maxX),
                    y: Math.min(Math.max(0, w.y), maxY),
                };
                try {
                    await window.AppRunner.launch(w.appId, { geometry: g });
                } catch (_) {
                    /* one bad app must not sink the rest */
                }
            }
        } finally {
            restoring = false;
            save(); // canonical post-restore snapshot
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        for (const ev of [
            'window:created',
            'window:closed',
            'window:minimized',
            'window:restored',
            'window:maximized',
            'window:focused',
        ]) {
            document.addEventListener(ev, scheduleSave);
        }
        // Drag/resize end their gesture on mouseup with no custom event.
        document.addEventListener('mouseup', scheduleSave);
        // Final synchronous flush so the very last arrangement wins.
        window.addEventListener('beforeunload', save);

        // Fire-and-settle: restore() is best-effort by design (every failure
        // path is already swallowed inside). NOTE it can settle SYNCHRONOUSLY
        // — `if (wins.length === 0) return` above, i.e. every fresh host — so
        // 'desktop:restored' may dispatch in the microtask checkpoint browsers
        // run BETWEEN this DOMContentLoaded listener and the next one. Anything
        // waiting on this event must therefore register at PARSE time, never
        // inside its own DOMContentLoaded handler (js/walkthrough-autolaunch.js).
        restore()
            .catch(() => { /* restore is best-effort by design */ })
            .finally(() => {
                try {
                    document.dispatchEvent(new CustomEvent('desktop:restored'));
                } catch (_) { /* no listeners is not an error */ }
            });
    });

    return { save, restore };
})();

window.DesktopRestore = DesktopRestore;
