/**
 * Privatae Personal Computer — Window Manager
 *
 * Manages floating windows: create, focus, minimize, maximize, close.
 * Drag and resize with mouse. Z-index stacking.
 * Registers globally as window.WindowManager.
 */

const WindowManager = (() => {
    const state = {
        windows: new Map(),   // id → window state
        zCounter: 100,
        focusedId: null,
        dragState: null,
    };

    const windowLayer = () => document.getElementById('window-layer');

    /** A window's box in the WINDOW LAYER's coordinates — the space its
     *  left/top are written in. The layer sits at the viewport's corner unless a
     *  design draws the bar on the top or left edge (body.design-bar insets the
     *  layer); reading viewport
     *  coordinates there and writing them back moved every grabbed window by
     *  the bar's thickness. */
    function localRect(el) {
        const r = el.getBoundingClientRect();
        const layer = windowLayer();
        const o = layer ? layer.getBoundingClientRect() : { left: 0, top: 0 };
        return { x: r.left - o.left, y: r.top - o.top, w: r.width, h: r.height };
    }

    // --- Window creation ---

    function createWindow(opts) {
        const id = opts.id || `win-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
        if (state.windows.has(id)) {
            focus(id);
            return id;
        }

        const vw = window.innerWidth;
        const vh = window.innerHeight - 48; // minus taskbar

        const w = Math.min(opts.width || 700, vw - 40);
        const h = Math.min(opts.height || 500, vh - 40);
        const x = opts.x ?? Math.max(20, (vw - w) / 2 + (state.windows.size * 24) % 120);
        const y = opts.y ?? Math.max(20, (vh - h) / 2 + (state.windows.size * 24) % 80);

        const el = document.createElement('div');
        el.className = 'window';
        el.id = id;
        el.style.cssText = `left:${x}px;top:${y}px;width:${w}px;height:${h}px;z-index:${++state.zCounter}`;

        el.innerHTML = `
            <div class="window-header" data-win="${id}">
                <span class="window-icon">${opts.icon || ''}</span>
                <span class="window-title">${escapeHtml(opts.title || 'Window')}</span>
                <div class="window-controls">
                    <button class="btn-minimize" title="Minimize" data-action="minimize">&#x2013;</button>
                    <button class="btn-maximize" title="Maximize" data-action="maximize">&#x25A1;</button>
                    <button class="btn-close" title="Close" data-action="close">&#x2715;</button>
                </div>
            </div>
            <div class="window-body"></div>
            <div class="resize-handle top" data-resize="n"></div>
            <div class="resize-handle bottom" data-resize="s"></div>
            <div class="resize-handle left" data-resize="w"></div>
            <div class="resize-handle right" data-resize="e"></div>
            <div class="resize-handle top-left" data-resize="nw"></div>
            <div class="resize-handle top-right" data-resize="ne"></div>
            <div class="resize-handle bottom-left" data-resize="sw"></div>
            <div class="resize-handle bottom-right" data-resize="se"></div>
        `;

        windowLayer().appendChild(el);

        const winState = {
            id,
            el,
            // App windows carry their app id — the taskbar dock groups by it
            // and ws-client's close_app push closes by it (that path matched
            // nothing before appId existed here).
            appId: opts.appId || null,
            title: opts.title || 'Window',
            icon: opts.icon || '',
            minimized: false,
            maximized: false,
            restored: { x, y, w, h },
            // Creator cleanup hook — app-runner clears its openWindows map
            // here. Without this call, closing an app left a dead id in the
            // map and every relaunch "focused" a window that no longer
            // existed (the can't-reopen bug).
            onClose: typeof opts.onClose === 'function' ? opts.onClose : null,
        };
        state.windows.set(id, winState);
        // Manifest window.start_maximized: open filling the desktop.
        // Phone mode: EVERY window opens filling the screen — floating
        // geometry has nowhere to float on a phone (the restored size is
        // kept, so un-maximizing still works and the app-runner's scale
        // shim keeps the content proportioned either way).
        const phoneMode = window.CSeriesViewport && window.CSeriesViewport.isPhone();
        if (opts.startMaximized || phoneMode) toggleMaximize(id);

        // Set body content
        const body = el.querySelector('.window-body');
        if (opts.content) {
            body.innerHTML = opts.content;
        }
        if (opts.onInit) {
            opts.onInit(body, id);
        }

        // Events
        bindWindowEvents(el, id);
        focus(id);

        // Notify taskbar
        document.dispatchEvent(new CustomEvent('window:created', { detail: winState }));

        return id;
    }

    function bindWindowEvents(el, id) {
        // Header drag
        const header = el.querySelector('.window-header');
        header.addEventListener('mousedown', (e) => {
            if (e.target.closest('.window-controls')) return;
            focus(id);
            startDrag(e, id, 'move');
        });

        // Header double-click to toggle maximize
        header.addEventListener('dblclick', (e) => {
            if (e.target.closest('.window-controls')) return;
            toggleMaximize(id);
        });

        // Control buttons
        el.querySelector('.window-controls').addEventListener('click', (e) => {
            const btn = e.target.closest('[data-action]');
            if (!btn) return;
            const action = btn.dataset.action;
            if (action === 'minimize') minimize(id);
            else if (action === 'maximize') toggleMaximize(id);
            else if (action === 'close') close(id);
        });

        // Resize handles
        el.querySelectorAll('.resize-handle').forEach(handle => {
            handle.addEventListener('mousedown', (e) => {
                e.stopPropagation();
                focus(id);
                startDrag(e, id, 'resize', handle.dataset.resize);
            });
        });

        // Focus on click anywhere in window
        el.addEventListener('mousedown', () => focus(id));
    }

    // --- Drag and resize ---

    function startDrag(e, id, mode, direction) {
        e.preventDefault();
        const win = state.windows.get(id);
        if (!win || win.maximized) return;

        state.dragState = {
            id,
            mode,
            direction,
            startX: e.clientX,
            startY: e.clientY,
            startRect: localRect(win.el),
        };

        const overlay = document.createElement('div');
        overlay.className = 'drag-overlay';
        overlay.id = 'drag-overlay';
        document.body.appendChild(overlay);

        document.addEventListener('mousemove', onDragMove);
        document.addEventListener('mouseup', onDragEnd);
    }

    function onDragMove(e) {
        const ds = state.dragState;
        if (!ds) return;

        const dx = e.clientX - ds.startX;
        const dy = e.clientY - ds.startY;
        const win = state.windows.get(ds.id);
        if (!win) return;

        if (ds.mode === 'move') {
            win.el.style.left = `${ds.startRect.x + dx}px`;
            win.el.style.top = `${ds.startRect.y + dy}px`;
        } else if (ds.mode === 'resize') {
            const dir = ds.direction;
            let { x, y, w, h } = ds.startRect;

            if (dir.includes('e')) w = Math.max(320, w + dx);
            if (dir.includes('s')) h = Math.max(200, h + dy);
            if (dir.includes('w')) {
                const nw = Math.max(320, w - dx);
                x = x + (w - nw);
                w = nw;
            }
            if (dir.includes('n')) {
                const nh = Math.max(200, h - dy);
                y = y + (h - nh);
                h = nh;
            }

            win.el.style.left = `${x}px`;
            win.el.style.top = `${y}px`;
            win.el.style.width = `${w}px`;
            win.el.style.height = `${h}px`;
        }
    }

    function onDragEnd() {
        const ds = state.dragState;
        if (ds) {
            const win = state.windows.get(ds.id);
            if (win) win.restored = localRect(win.el);
        }
        state.dragState = null;
        document.removeEventListener('mousemove', onDragMove);
        document.removeEventListener('mouseup', onDragEnd);
        const overlay = document.getElementById('drag-overlay');
        if (overlay) overlay.remove();
    }

    // --- Window operations ---

    function focus(id) {
        const win = state.windows.get(id);
        if (!win) return;

        if (state.focusedId && state.focusedId !== id) {
            const prev = state.windows.get(state.focusedId);
            if (prev) prev.el.classList.remove('focused');
        }

        win.el.style.zIndex = ++state.zCounter;
        win.el.classList.add('focused');
        state.focusedId = id;

        document.dispatchEvent(new CustomEvent('window:focused', { detail: { id } }));
    }

    function minimize(id) {
        const win = state.windows.get(id);
        if (!win) return;
        win.minimized = true;
        win.el.classList.add('minimized');

        // Focus next visible window
        let next = null;
        for (const [wid, w] of state.windows) {
            if (wid !== id && !w.minimized) {
                if (!next || parseInt(w.el.style.zIndex) > parseInt(next.el.style.zIndex)) {
                    next = w;
                }
            }
        }
        if (next) focus(next.id);
        else state.focusedId = null;

        document.dispatchEvent(new CustomEvent('window:minimized', { detail: { id } }));
    }

    function restore(id) {
        const win = state.windows.get(id);
        if (!win) return;
        win.minimized = false;
        win.el.classList.remove('minimized');
        focus(id);
        document.dispatchEvent(new CustomEvent('window:restored', { detail: { id } }));
    }

    function toggleMaximize(id) {
        const win = state.windows.get(id);
        if (!win) return;

        if (win.maximized) {
            win.maximized = false;
            win.el.classList.remove('maximized');
            const r = win.restored;
            win.el.style.cssText = `left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px;z-index:${win.el.style.zIndex}`;
        } else {
            win.restored = localRect(win.el);
            win.maximized = true;
            win.el.classList.add('maximized');
        }

        document.dispatchEvent(new CustomEvent('window:maximized', { detail: { id, maximized: win.maximized } }));
    }

    function close(id) {
        const _w = state.windows.get(id);
        if (_w && _w.onClose) { try { _w.onClose(); } catch (e) { /* creator cleanup must never block close */ } }
        const win = state.windows.get(id);
        if (!win) return;

        win.el.remove();
        state.windows.delete(id);

        if (state.focusedId === id) {
            state.focusedId = null;
            let next = null;
            for (const [, w] of state.windows) {
                if (!w.minimized) {
                    if (!next || parseInt(w.el.style.zIndex) > parseInt(next.el.style.zIndex)) {
                        next = w;
                    }
                }
            }
            if (next) focus(next.id);
        }

        // appId rides along so an app can react to ANOTHER app's window
        // closing (Getting Started advances a slide when the app it opened
        // closes) without cross-referencing getAll() before the close.
        document.dispatchEvent(new CustomEvent('window:closed', { detail: { id, appId: win.appId } }));
    }

    function getBody(id) {
        const win = state.windows.get(id);
        return win ? win.el.querySelector('.window-body') : null;
    }

    function getAll() {
        return Array.from(state.windows.values());
    }

    // --- Utilities ---

    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function escapeHtml(str) {
        return String(str == null ? '' : str).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    // --- Desktop icons ---

    // --- Notifications ---

    function notify(title, body, type) {
        type = type || 'info';
        let area = document.getElementById('notification-area');
        if (!area) {
            area = document.createElement('div');
            area.id = 'notification-area';
            document.body.appendChild(area);
        }

        const el = document.createElement('div');
        el.className = `notification ${type}`;
        el.innerHTML = `
            <div class="notification-title">${escapeHtml(title)}</div>
            ${body ? `<div class="notification-body">${escapeHtml(body)}</div>` : ''}
        `;
        area.appendChild(el);

        setTimeout(() => {
            el.style.opacity = '0';
            el.style.transform = 'translateX(20px)';
            el.style.transition = 'all 200ms ease';
            setTimeout(() => el.remove(), 200);
        }, 8000);
    }

    // --- Init ---

    // ── Wallpaper ─────────────────────────────────────────────
    async function applyWallpaper() {
        try {
            const r = await fetch('/api/appearance');
            if (!r.ok) return;
            const data = await r.json();
            const el = document.getElementById('desktop');
            if (!el) return;
            // HTML desktop (js/desktop-surface.js): the surface module reads
            // the same payload and mounts / clears the host-authored document.
            // For the 'html' key css + image_url are null, so the branches
            // below fall through to the default wallpaper, which the
            // document fades in over.
            if (window.DesktopSurface) window.DesktopSurface.apply(data);
            // Phone mode uses the mobile variant when one is set;
            // otherwise the desktop wallpaper applies everywhere.
            const phone = window.CSeriesViewport && window.CSeriesViewport.isPhone();
            const useMobile = phone && data.wallpaper_mobile;
            const css = useMobile ? data.css_mobile : data.css;
            const imageUrl = useMobile ? data.image_url_mobile : data.image_url;
            if (imageUrl) {
                // Custom uploaded image
                el.classList.remove('phone-default');
                el.style.background = `center/cover no-repeat url(${imageUrl})`;
            } else if (css) {
                el.classList.remove('phone-default');
                el.style.background = css;
            } else {
                // Named "default" — clear any inline override so the
                // desktop.css wallpaper shows (matters when rotating
                // between variants, not just on boot). Phone mode swaps
                // in the bundled mobile variant via the phone-default
                // class.
                el.style.background = '';
                el.classList.toggle('phone-default', phone);
            }
        } catch (_) {
            // Non-fatal — falls back to desktop.css default
        }
    }

    // Expose so the settings app can live-preview without page reload.
    window.applyWallpaper = applyWallpaper;

    function init() {
        applyWallpaper();

        // Crossing the phone/desktop boundary (rotation, window resize):
        // swap to that mode's wallpaper and, entering phone mode, maximize
        // every floating window — a floating layout can't survive a phone
        // viewport.
        if (window.CSeriesViewport) {
            window.CSeriesViewport.onChange((phone) => {
                applyWallpaper();
                if (phone) {
                    for (const [id, w] of state.windows) {
                        if (!w.maximized && !w.minimized) toggleMaximize(id);
                    }
                }
            });
        }

        // Mobile browsers restore a backgrounded tab without reloading it,
        // so a wallpaper set from another device (e.g. phone variant picked
        // in desktop Settings) stays stale until a manual refresh. Re-apply
        // whenever the tab returns to the foreground or comes back out of
        // the back/forward cache.
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) applyWallpaper();
        });
        window.addEventListener('pageshow', (e) => {
            if (e.persisted) applyWallpaper();
        });

        // Desktop right-click menu lives in dock-menu.js (it owns the icon
        // surface and the layout actions the menu exposes).

        // Clear selection on desktop click
        document.getElementById('desktop').addEventListener('mousedown', (e) => {
            if (!e.target.closest('.window') && !e.target.closest('.desktop-icon')) {
                document.querySelectorAll('.desktop-icon.selected').forEach(i => i.classList.remove('selected'));
            }
        });

    }

    document.addEventListener('DOMContentLoaded', init);

    return {
        create: createWindow,
        close,
        focus,
        minimize,
        restore,
        toggleMaximize,
        getBody,
        getAll,
        localRect,
        notify,
    };
})();

// Expose globally. Classic script tags scope top-level const/let to the
// script, unlike `var`. Apps + launcher explicitly look up `window.WindowManager`.
window.WindowManager = WindowManager;
