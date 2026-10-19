/**
 * C-Series — Floating widget layer (shell-owned, above app windows)
 *
 * Small always-on-top cards the SHELL owns — not app windows, not tray
 * flyouts. Widgets register here (WidgetLayer.register); the layer gives
 * each one a draggable, corner-resizable card in #widget-layer, a minimize
 * path to a tray chip in #taskbar-right, and a close/hide path. Geometry
 * and visibility persist per widget through DesktopLayout's existing
 * /api/appearance ↔ /api/appearance/layout round-trip (the `widgets` key).
 *
 * Stacking: #widget-layer is a SIBLING of #window-layer inside #desktop
 * with its own z-index (var(--z-widgets), tokens.css). #window-layer has
 * no z-index of its own (desktop.css), so app-window z-indexes — the
 * WindowManager zCounter, 100 and climbing — compete directly in
 * #desktop's stacking context; the layer's z-index tops them ALL at once,
 * so widgets float over every app window no matter how far zCounter has
 * climbed. #desktop is position:fixed (its own stacking context), so
 * nothing in here can ever cover the taskbar / dock menu / modals, which
 * stack at document level.
 *
 * Phone mode: the whole layer (cards + chips) hides — floating cards have
 * nowhere to float on a phone (same call the window manager makes).
 */
const WidgetLayer = (() => {
    const widgets = new Map();   // id → { spec, el, chipEl, state, zBase }
    let zCounter = 10;           // internal to the layer's stacking context
    let dragState = null;

    const MIN_W = 160, MIN_H = 120;
    const DEFAULT_W = 220, DEFAULT_H = 180;

    const layerEl = () => document.getElementById('widget-layer');

    function isPhone() {
        return !!(window.CSeriesViewport && window.CSeriesViewport.isPhone());
    }

    // --- Registration ---

    /**
     * spec: { id, title, glyph (inline SVG/markup), render(bodyEl),
     *         defaultPos {x,y}, defaultSize {w,h}, minSize {w,h},
     *         defaultHidden }
     * Persisted state overrides the defaults. Hidden widgets build nothing
     * until toggled on (dock-menu's desktop right-click menu).
     */
    function register(spec) {
        if (!spec || !spec.id || widgets.has(spec.id)) return;
        const entry = { spec, el: null, chipEl: null, state: null };
        widgets.set(spec.id, entry);
        const boot = async () => {
            await window.DesktopLayout.ready;
            entry.state = normalizeState(spec, window.DesktopLayout.widgetState(spec.id));
            sync(entry);
        };
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', boot);
        } else {
            boot();
        }
    }

    function normalizeState(spec, saved) {
        const size = spec.defaultSize || {};
        const min = spec.minSize || {};
        const w = Math.max(min.w || MIN_W, (saved && saved.w) || size.w || DEFAULT_W);
        const h = Math.max(min.h || MIN_H, (saved && saved.h) || size.h || DEFAULT_H);
        const pos = spec.defaultPos || {};
        // Default anchor: top-right, clear of the desktop icon column.
        let x = (saved && saved.x != null) ? saved.x
            : (pos.x != null ? pos.x : window.innerWidth - w - 24);
        let y = (saved && saved.y != null) ? saved.y : (pos.y != null ? pos.y : 24);
        // Keep at least a grabbable strip on-screen (layout may have been
        // saved on a larger viewport).
        x = Math.min(Math.max(x, 8 - w + 48), window.innerWidth - 48);
        y = Math.min(Math.max(y, 0), window.innerHeight - 96);
        return {
            x, y, w, h,
            minimized: !!(saved && saved.minimized),
            hidden: saved && saved.hidden != null ? !!saved.hidden : !!spec.defaultHidden,
        };
    }

    // Reconcile DOM (card + chip) with entry.state. The one place that
    // decides what exists — show/hide/minimize/restore all route through it.
    function sync(entry) {
        const st = entry.state;
        const wantCard = !st.hidden && !st.minimized && !isPhone();
        const wantChip = !st.hidden && st.minimized && !isPhone();
        if (wantCard && !entry.el) buildCard(entry);
        if (!wantCard && entry.el) { entry.el.remove(); entry.el = null; }
        if (wantChip && !entry.chipEl) buildChip(entry);
        if (!wantChip && entry.chipEl) { entry.chipEl.remove(); entry.chipEl = null; }
    }

    function persist(entry) {
        const st = entry.state;
        window.DesktopLayout.setWidgetState(entry.spec.id, {
            x: st.x, y: st.y, w: st.w, h: st.h,
            minimized: st.minimized, hidden: st.hidden,
        });
    }

    // --- Card ---

    function buildCard(entry) {
        const layer = layerEl();
        if (!layer) return;
        const { spec, state: st } = entry;

        const el = document.createElement('div');
        el.className = 'widget-card';
        el.dataset.widgetId = spec.id;
        el.style.cssText = `left:${st.x}px;top:${st.y}px;width:${st.w}px;height:${st.h}px;z-index:${++zCounter}`;
        el.innerHTML = `
            <div class="widget-head">
                <span class="widget-glyph">${spec.glyph || ''}</span>
                <span class="widget-title">${escapeHtml(spec.title || spec.id)}</span>
                <div class="widget-controls">
                    <button class="btn-minimize" title="Minimize to tray" data-action="minimize">&#x2013;</button>
                    <button class="btn-close" title="Hide" data-action="close">&#x2715;</button>
                </div>
            </div>
            <div class="widget-body"></div>
            <div class="widget-resize" title="Resize"></div>
        `;
        layer.appendChild(el);
        entry.el = el;

        el.querySelector('.widget-controls').addEventListener('click', (e) => {
            const btn = e.target.closest('[data-action]');
            if (!btn) return;
            e.stopPropagation();
            if (btn.dataset.action === 'minimize') minimize(spec.id);
            else if (btn.dataset.action === 'close') hide(spec.id);
        });

        // Drag anywhere on the card (head or body) — same overlay +
        // document-listener pattern as WindowManager.startDrag. Buttons and
        // interactive content opt out.
        el.addEventListener('mousedown', (e) => {
            el.style.zIndex = ++zCounter;
            if (e.target.closest('button, a, input, select, textarea')) return;
            if (e.target.closest('.widget-resize')) return;
            startDrag(e, entry, 'move');
        });
        el.querySelector('.widget-resize').addEventListener('mousedown', (e) => {
            e.stopPropagation();
            el.style.zIndex = ++zCounter;
            startDrag(e, entry, 'resize');
        });

        try {
            if (typeof spec.render === 'function') spec.render(el.querySelector('.widget-body'));
        } catch (_) { /* a widget's render must never break the layer */ }
    }

    // --- Drag / resize (pattern: desktop.js startDrag/onDragMove/onDragEnd) ---

    function startDrag(e, entry, mode) {
        e.preventDefault();
        const rect = entry.el.getBoundingClientRect();
        dragState = {
            entry, mode,
            startX: e.clientX,
            startY: e.clientY,
            startRect: { x: rect.left, y: rect.top, w: rect.width, h: rect.height },
        };
        const overlay = document.createElement('div');
        overlay.className = 'drag-overlay';
        overlay.id = 'widget-drag-overlay';
        document.body.appendChild(overlay);
        document.addEventListener('mousemove', onDragMove);
        document.addEventListener('mouseup', onDragEnd);
    }

    function onDragMove(e) {
        const ds = dragState;
        if (!ds) return;
        const dx = e.clientX - ds.startX;
        const dy = e.clientY - ds.startY;
        const { entry } = ds;
        const min = entry.spec.minSize || {};
        if (ds.mode === 'move') {
            entry.el.style.left = `${ds.startRect.x + dx}px`;
            entry.el.style.top = `${ds.startRect.y + dy}px`;
        } else {
            entry.el.style.width = `${Math.max(min.w || MIN_W, ds.startRect.w + dx)}px`;
            entry.el.style.height = `${Math.max(min.h || MIN_H, ds.startRect.h + dy)}px`;
        }
    }

    function onDragEnd() {
        const ds = dragState;
        if (ds) {
            const rect = ds.entry.el.getBoundingClientRect();
            Object.assign(ds.entry.state, {
                x: rect.left, y: rect.top, w: rect.width, h: rect.height,
            });
            persist(ds.entry);
        }
        dragState = null;
        document.removeEventListener('mousemove', onDragMove);
        document.removeEventListener('mouseup', onDragEnd);
        const overlay = document.getElementById('widget-drag-overlay');
        if (overlay) overlay.remove();
    }

    // --- Tray chip (pattern: the tray-status chips in #taskbar-right) ---
    //
    // While a design draws the bar (body.design-bar) the taskbar is hidden, so
    // a minimized widget's chip sits in the floating dock instead of vanishing
    // with it; cseries:design-bar moves the chips that already exist when that
    // flips.

    function chipTray() {
        if (document.body.classList.contains('design-bar')) {
            return document.getElementById('floating-dock');
        }
        return document.getElementById('taskbar-right');
    }

    function placeChip(chip) {
        const tray = chipTray();
        if (!tray) return;
        // Sit with the tray chips, ahead of the help button.
        const support = document.getElementById('btn-support');
        if (support && support.parentNode === tray) tray.insertBefore(chip, support);
        else tray.appendChild(chip);
    }

    document.addEventListener('cseries:design-bar', () => {
        widgets.forEach((entry) => { if (entry.chipEl) placeChip(entry.chipEl); });
    });

    function buildChip(entry) {
        const tray = chipTray();
        if (!tray) return;
        const { spec } = entry;
        const chip = document.createElement('button');
        chip.className = 'taskbar-btn tray-chip widget-chip';
        chip.title = (spec.title || spec.id) + ' — click to restore';
        chip.innerHTML = spec.glyph || escapeHtml((spec.title || spec.id).charAt(0).toUpperCase());
        chip.addEventListener('click', (e) => {
            e.stopPropagation();
            restore(spec.id);
        });
        placeChip(chip);
        entry.chipEl = chip;
    }

    // --- Operations ---

    function minimize(id) {
        const entry = widgets.get(id);
        if (!entry || !entry.state) return;
        entry.state.minimized = true;   // position kept — restore returns there
        sync(entry);
        persist(entry);
    }

    function restore(id) {
        const entry = widgets.get(id);
        if (!entry || !entry.state) return;
        entry.state.minimized = false;
        sync(entry);
        persist(entry);
    }

    function show(id) {
        const entry = widgets.get(id);
        if (!entry || !entry.state) return;
        entry.state.hidden = false;
        entry.state.minimized = false;
        sync(entry);
        persist(entry);
    }

    function hide(id) {
        const entry = widgets.get(id);
        if (!entry || !entry.state) return;
        entry.state.hidden = true;
        sync(entry);
        persist(entry);
    }

    function toggle(id) {
        const entry = widgets.get(id);
        if (!entry || !entry.state) return;
        if (entry.state.hidden) show(id);
        else hide(id);
    }

    function isVisible(id) {
        const entry = widgets.get(id);
        return !!(entry && entry.state && !entry.state.hidden);
    }

    /** Registered widgets for toggle surfaces: [{ id, title }]. */
    function list() {
        return Array.from(widgets.values(), (e) => ({ id: e.spec.id, title: e.spec.title || e.spec.id }));
    }

    // --- Utilities / init ---

    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function escapeHtml(str) {
        return String(str == null ? '' : str).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    function init() {
        // Phone mode hides everything; crossing back re-materializes from
        // state (same CSeriesViewport boundary the window manager uses).
        if (window.CSeriesViewport) {
            window.CSeriesViewport.onChange(() => widgets.forEach(sync));
        }
        // Reset Layout wipes widget state server-side; mirror it live.
        document.addEventListener('cseries:widgets-reset', () => {
            widgets.forEach((entry) => {
                entry.state = normalizeState(entry.spec, null);
                if (entry.el) { entry.el.remove(); entry.el = null; }
                sync(entry);
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    return { register, minimize, restore, show, hide, toggle, isVisible, list };
})();

window.WidgetLayer = WidgetLayer;
