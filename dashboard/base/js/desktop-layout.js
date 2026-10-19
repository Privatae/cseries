/**
 * C-Series — Desktop Layout state (dock pins, desktop icon order) + the
 * shared shell context menu.
 *
 * One module owns what the user has arranged: which apps sit in the dock,
 * which icons live on the desktop and in what order. State round-trips
 * through /api/appearance (GET on boot) and /api/appearance/layout (PUT,
 * debounced) — a shell-trusted lane, no app token. Renderers (taskbar
 * dock, desktop icons, launcher) read from here and listen for
 * `cseries:layout-changed` on document.
 */
const DesktopLayout = (() => {
    // Dock starter set for a fresh entity — the flagship surfaces, in the
    // order a new user meets them. Overridden forever by the first pin,
    // unpin, or drag.
    // Calendar sits beside Circles: it carries the count of what is still
    // scheduled today, which the Circles bubble carried before the calendar
    // became an app of its own.
    const DEFAULT_PINNED = [
        'circles', 'calendar', 'workbench', 'artifacts', 'context-manager',
        'app-store', 'settings',
    ];

    // Apps put into a dock someone has already arranged, ONCE. A dock nobody
    // arranged shows DEFAULT_PINNED, which holds them anyway; an arranged one
    // keeps its owner's list forever, so an app that arrived later would never
    // reach it. The first desktop start after an id is listed here puts the
    // app after Circles (at the end when Circles is not pinned) and records
    // the id in dock_offered; from then on the owner's choice holds, and an
    // app taken out stays out. Calendar: the count of what is still scheduled
    // today moved to its icon from the Circles bubble, so a dock without it
    // would lose that count (arranged docks get Calendar once too).
    // Every id here must also be in DEFAULT_PINNED: arranging a dock nobody
    // had arranged, and Reset, record these as offered because the defaults
    // already held them (tests/test_dock_offer_once_js.py holds this).
    const OFFER_ONCE = ['calendar'];

    const state = {
        dock_pinned: null,     // null = user never customized → defaults apply
        dock_offered: [],      // ids already put into an arranged dock once (OFFER_ONCE)
        desktop_order: [],     // [] = default (every system app, registry order)
        desktop_hidden: [],
        widgets: {},           // widget id → {x, y, w, h, minimized, hidden}
    };

    let saveTimer = null;
    let pendingSave = {};

    let resolveReady;
    const ready = new Promise((res) => { resolveReady = res; });

    async function load() {
        try {
            const r = await fetch('/api/appearance', { cache: 'no-store' });
            if (r.ok) {
                const data = await r.json();
                const l = data && data.desktop_layout;
                if (l && typeof l === 'object') {
                    if (Array.isArray(l.dock_pinned)) state.dock_pinned = l.dock_pinned;
                    if (Array.isArray(l.dock_offered)) state.dock_offered = l.dock_offered;
                    if (Array.isArray(l.desktop_order)) state.desktop_order = l.desktop_order;
                    if (Array.isArray(l.desktop_hidden)) state.desktop_hidden = l.desktop_hidden;
                    if (l.widgets && typeof l.widgets === 'object' && !Array.isArray(l.widgets)) {
                        state.widgets = l.widgets;
                    }
                }
            }
            offerOnce();
        } catch (_) { /* offline boot — defaults render fine */ }
        resolveReady();
        emit();
    }

    function withOffered(ids) {
        return ids.concat(OFFER_ONCE.filter((id) => !ids.includes(id)));
    }

    // See OFFER_ONCE. Only for a dock its owner arranged, and only for ids not
    // offered before; one already pinned is just recorded.
    function offerOnce() {
        if (!Array.isArray(state.dock_pinned)) return;
        const due = OFFER_ONCE.filter((id) => !state.dock_offered.includes(id));
        if (!due.length) return;
        const pinned = state.dock_pinned.slice();
        due.forEach((id) => {
            if (pinned.includes(id)) return;
            const at = pinned.indexOf('circles');
            if (at >= 0) pinned.splice(at + 1, 0, id);
            else pinned.push(id);
        });
        state.dock_pinned = pinned;
        state.dock_offered = state.dock_offered.concat(due);
        save({ dock_pinned: pinned, dock_offered: state.dock_offered });
    }

    function save(partial) {
        Object.assign(pendingSave, partial);
        clearTimeout(saveTimer);
        saveTimer = setTimeout(async () => {
            const body = pendingSave;
            pendingSave = {};
            try {
                await fetch('/api/appearance/layout', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
            } catch (_) { /* transient — next change retries the merge */ }
        }, 400);
    }

    function emit() {
        document.dispatchEvent(new CustomEvent('cseries:layout-changed'));
    }

    // --- Dock ---

    function dockPinned() {
        return (state.dock_pinned || DEFAULT_PINNED).slice();
    }

    function isPinned(appId) {
        return dockPinned().includes(appId);
    }

    function setDockPinned(ids) {
        state.dock_pinned = ids.slice();
        // Whoever arranges the dock has had every OFFER_ONCE app in front of
        // them (in DEFAULT_PINNED, or put there once): one they take out
        // stays out.
        state.dock_offered = withOffered(state.dock_offered);
        save({ dock_pinned: state.dock_pinned, dock_offered: state.dock_offered });
        emit();
    }

    function pin(appId) {
        if (!appId || isPinned(appId)) return;
        setDockPinned(dockPinned().concat(appId));
    }

    function unpin(appId) {
        if (!isPinned(appId)) return;
        setDockPinned(dockPinned().filter((id) => id !== appId));
    }

    // --- Desktop icons ---

    /**
     * The ordered app ids that belong on the desktop. `defaults` is the
     * registry-derived default set (system apps); once the user customizes,
     * desktop_order is the explicit full list — new installs then join via
     * the launcher's "Add to desktop", not by surprise.
     */
    function desktopIds(defaults) {
        const base = state.desktop_order.length ? state.desktop_order : (defaults || []);
        return base.filter((id) => !state.desktop_hidden.includes(id));
    }

    function setDesktopOrder(ids) {
        state.desktop_order = ids.slice();
        state.desktop_hidden = [];   // an explicit list subsumes hidden
        save({ desktop_order: state.desktop_order, desktop_hidden: [] });
        emit();
    }

    function addToDesktop(appId, defaults) {
        const cur = desktopIds(defaults);
        if (cur.includes(appId)) return;
        setDesktopOrder(cur.concat(appId));
    }

    function removeFromDesktop(appId, defaults) {
        setDesktopOrder(desktopIds(defaults).filter((id) => id !== appId));
    }

    function onDesktop(appId, defaults) {
        return desktopIds(defaults).includes(appId);
    }

    // --- Floating widgets (js/widget-layer.js) ---

    /**
     * Persisted per-widget card state: {x, y, w, h, minimized, hidden} —
     * all keys optional (the widget layer applies its defaults). The whole
     * widgets map is saved on every change; the server merges it as one
     * desktop_layout key, so widget saves can't clobber pins/icon order.
     */
    function widgetState(id) {
        const ws = state.widgets[id];
        return ws && typeof ws === 'object' ? Object.assign({}, ws) : null;
    }

    function setWidgetState(id, partial) {
        if (!id) return;
        state.widgets[id] = Object.assign({}, state.widgets[id] || {}, partial);
        save({ widgets: state.widgets });
        // No emit(): the widget layer owns its own DOM; the dock/desktop
        // renderers listening for cseries:layout-changed don't read widgets.
    }

    function resetLayout() {
        state.dock_pinned = null;      // runtime: defaults apply
        state.dock_offered = withOffered(state.dock_offered);
        state.desktop_order = [];
        state.desktop_hidden = [];
        state.widgets = {};
        // Persist the defaults explicitly — the server merge has no
        // "clear this key" verb, and "reset" writing the default set IS
        // the intended outcome. The defaults hold every OFFER_ONCE app, so
        // each is recorded as offered.
        save({ dock_pinned: DEFAULT_PINNED.slice(), dock_offered: state.dock_offered,
               desktop_order: [], desktop_hidden: [], widgets: {} });
        emit();
        document.dispatchEvent(new CustomEvent('cseries:widgets-reset'));
    }

    document.addEventListener('DOMContentLoaded', load);

    return {
        ready, dockPinned, isPinned, pin, unpin, setDockPinned,
        desktopIds, setDesktopOrder, addToDesktop, removeFromDesktop,
        onDesktop, widgetState, setWidgetState, resetLayout, DEFAULT_PINNED, OFFER_ONCE,
    };
})();

window.DesktopLayout = DesktopLayout;


/**
 * Shared right-click menu for shell chrome (desktop icons, dock items,
 * launcher tiles, the desktop surface). One menu at a time; closes on
 * outside click / Escape / any item. Styling: .context-menu in desktop.css.
 */
const ContextMenu = (() => {
    let el = null;

    function close() {
        if (el) { el.remove(); el = null; }
    }

    /**
     * items: [{ label, action, danger }] — falsy entries and
     * { separator: true } supported.
     */
    function show(x, y, items) {
        close();
        el = document.createElement('div');
        el.className = 'context-menu';
        items.filter(Boolean).forEach((item) => {
            if (item.separator) {
                const sep = document.createElement('div');
                sep.className = 'context-menu-separator';
                el.appendChild(sep);
                return;
            }
            const btn = document.createElement('button');
            btn.className = 'context-menu-item' + (item.danger ? ' danger' : '');
            btn.textContent = item.label;
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                close();
                try { item.action && item.action(); } catch (_) {}
            });
            el.appendChild(btn);
        });
        document.body.appendChild(el);

        // Keep on-screen: flip when the menu would overflow.
        const r = el.getBoundingClientRect();
        const px = Math.min(x, window.innerWidth - r.width - 8);
        const py = Math.min(y, window.innerHeight - r.height - 8);
        el.style.left = Math.max(8, px) + 'px';
        el.style.top = Math.max(8, py) + 'px';
    }

    document.addEventListener('mousedown', (e) => {
        if (el && !el.contains(e.target)) close();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') close();
    });
    window.addEventListener('blur', close);

    return { show, close };
})();

window.ContextMenu = ContextMenu;
