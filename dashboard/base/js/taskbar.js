/**
 * C-Series — Taskbar Dock
 *
 * #taskbar-center is a real dock: pinned apps are always present, running
 * apps join with a running dot, the focused app lights up. Click launches,
 * focuses, or minimizes; right-click pins/unpins and closes; dragging a
 * pinned icon reorders the pin row (persisted via DesktopLayout).
 *
 * Replaced the old text window-tabs and the dead
 * "Personal Computer" era chrome (FileExplorer button, desktop-menu,
 * About window) that had no living callers.
 *
 * A DESIGN MAY DRAW THE BAR. When the worn desktop declares <meta name="cseries-bar">, the shell
 * hides #taskbar and desktop-surface.js hands the document this module's
 * model instead: dockModel() is exactly what render() draws, and activate /
 * dockMenu / toggleShowDesktop are the same behaviours the shell's own
 * buttons call — one dock, two ways to draw it. Every change announces
 * `taskbar:changed` so the document is re-told.
 */
const Taskbar = (() => {
    // appId → { winIds:Set, focused:bool } for app windows;
    // plain windows (no appId) render as transient items keyed by win id.
    const running = new Map();
    const transient = new Map();   // winId → { title, icon, focused }
    let appIndex = null;           // appId → manifest, from /api/applications
    let dragId = null;
    // appId → { n, note }: a count drawn on that app's dock icon (setBadge).
    // Kept here rather than on the button, because render() draws anew.
    const badges = new Map();

    async function loadApps() {
        try {
            const res = await fetch('/api/applications');
            if (!res.ok) throw new Error('unavailable');
            const data = await res.json();
            appIndex = new Map((data.applications || []).map((a) => [a.id, a]));
        } catch (_) {
            appIndex = appIndex || new Map();
        }
        render();
    }

    function appFor(appId) {
        return (appIndex && appIndex.get(appId)) || { id: appId, name: appId };
    }

    // --- Rendering ---

    function pinnedIds() {
        // Pins referencing apps that aren't installed (yet) stay persisted
        // but don't render; before the app list arrives, render them all so
        // the dock never flashes empty.
        return DesktopLayout.dockPinned()
            .filter((id) => !appIndex || appIndex.size === 0 || appIndex.has(id));
    }

    /** The dock as data, in the order render() draws it: pinned apps, then
     *  running ones that are not pinned. Transient (app-less) windows are not
     *  here — a design reads those from the window list. */
    function dockModel() {
        const pinned = pinnedIds();
        const ids = pinned.concat(Array.from(running.keys()).filter((id) => !pinned.includes(id)));
        return ids.map((id) => {
            const entry = running.get(id);
            return {
                id,
                name: appFor(id).name || id,
                pinned: pinned.includes(id),
                running: !!entry,
                focused: !!(entry && entry.focused),
            };
        });
    }

    function announce() {
        document.dispatchEvent(new CustomEvent('taskbar:changed'));
    }

    function render() {
        announce();
        const center = document.getElementById('taskbar-center');
        if (!center) return;

        const pinned = pinnedIds();
        const runningUnpinned = Array.from(running.keys())
            .filter((id) => !pinned.includes(id));

        center.innerHTML = '';

        pinned.forEach((id) => center.appendChild(dockItem(id, true)));
        if (pinned.length && (runningUnpinned.length || transient.size)) {
            const sep = document.createElement('div');
            sep.className = 'dock-sep';
            center.appendChild(sep);
        }
        runningUnpinned.forEach((id) => center.appendChild(dockItem(id, false)));
        transient.forEach((t, winId) => center.appendChild(transientItem(winId, t)));
    }

    function dockItem(appId, isPinned) {
        const app = appFor(appId);
        const entry = running.get(appId);
        const btn = document.createElement('button');
        btn.className = 'dock-item'
            + (entry ? ' running' : '')
            + (entry && entry.focused ? ' active' : '');
        btn.dataset.appId = appId;
        btn.title = app.name || appId;
        btn.innerHTML = `
            <span class="dock-item-icon">${window.iconMarkup(app, 26)}</span>
            <span class="dock-item-dot" aria-hidden="true"></span>
        `;
        const badge = badges.get(appId);
        if (badge) {
            const b = document.createElement('span');
            b.className = 'dock-item-badge';
            b.setAttribute('aria-hidden', 'true');
            b.textContent = badge.n > 99 ? '99+' : String(badge.n);
            btn.appendChild(b);
            // The title is the button's name too, so the count is said, not only drawn.
            btn.title = (app.name || appId) + ' · ' + (badge.note || badge.n);
        }

        btn.addEventListener('click', () => activate(appId));
        btn.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            e.stopPropagation();
            menuFor(appId, isPinned, e.clientX, e.clientY);
        });

        // Reorder: only pinned icons drag; dropping saves the new pin order.
        if (isPinned) {
            btn.draggable = true;
            btn.addEventListener('dragstart', (e) => {
                dragId = appId;
                btn.classList.add('dragging');
                if (e.dataTransfer) {
                    e.dataTransfer.effectAllowed = 'move';
                    e.dataTransfer.setData('text/plain', appId);
                }
            });
            btn.addEventListener('dragend', () => {
                dragId = null;
                btn.classList.remove('dragging');
                commitPinOrder();
            });
            btn.addEventListener('dragover', (e) => {
                if (!dragId || dragId === appId) return;
                e.preventDefault();
                const dragging = btn.parentElement &&
                    btn.parentElement.querySelector('.dock-item.dragging');
                if (!dragging) return;
                const r = btn.getBoundingClientRect();
                const before = e.clientX < r.left + r.width / 2;
                btn.parentElement.insertBefore(dragging, before ? btn : btn.nextSibling);
            });
        }
        return btn;
    }

    function transientItem(winId, t) {
        const btn = document.createElement('button');
        btn.className = 'dock-item running' + (t.focused ? ' active' : '');
        btn.title = t.title || 'Window';
        btn.innerHTML = `
            <span class="dock-item-icon">${t.icon || '<span class="dock-tile-letter">•</span>'}</span>
            <span class="dock-item-dot" aria-hidden="true"></span>
        `;
        btn.addEventListener('click', () => {
            const win = (WindowManager.getAll() || []).find((w) => w.id === winId);
            if (!win) return;
            if (win.minimized) WindowManager.restore(winId);
            else if (t.focused) WindowManager.minimize(winId);
            else WindowManager.focus(winId);
        });
        return btn;
    }

    function commitPinOrder() {
        const center = document.getElementById('taskbar-center');
        if (!center) return;
        const order = [];
        center.querySelectorAll('.dock-item[draggable="true"]').forEach((el) => {
            if (el.dataset.appId) order.push(el.dataset.appId);
        });
        if (order.length) DesktopLayout.setDockPinned(order);
    }

    // --- Behaviors ---

    function activate(appId) {
        const entry = running.get(appId);
        if (!entry || entry.winIds.size === 0) {
            if (window.AppRunner) AppRunner.launch(appId);
            return;
        }
        const winId = entry.winIds.values().next().value;
        const win = (WindowManager.getAll() || []).find((w) => w.id === winId);
        if (!win) { if (window.AppRunner) AppRunner.launch(appId); return; }
        if (win.minimized) WindowManager.restore(winId);
        else if (entry.focused) WindowManager.minimize(winId);
        else WindowManager.focus(winId);
    }

    function menuFor(appId, isPinned, x, y) {
        const entry = running.get(appId);
        ContextMenu.show(x, y, [
            { label: entry ? 'Show' : 'Open', action: () => activate(appId) },
            isPinned
                ? { label: 'Unpin from Dock', action: () => DesktopLayout.unpin(appId) }
                : { label: 'Pin to Dock', action: () => DesktopLayout.pin(appId) },
            entry && { separator: true },
            entry && {
                label: 'Close',
                danger: true,
                action: () => {
                    Array.from(entry.winIds).forEach((id) => WindowManager.close(id));
                },
            },
        ]);
    }

    // --- Show desktop (far-right taskbar button) ---
    //
    // One click minimizes every visible window and remembers the set; the
    // next click restores exactly that set. Any window the user opens or
    // restores in between forgets the set — the button then reads as
    // "minimize all" again rather than resurrecting stale windows.
    let shownDesktopSet = null;   // Set<winId> | null
    let restoring = false;

    function showDesktopButton() {
        return document.getElementById('btn-show-desktop');
    }

    function syncShowDesktop() {
        const btn = showDesktopButton();
        if (!btn) return;
        const on = !!(shownDesktopSet && shownDesktopSet.size);
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        btn.title = on ? 'Bring windows back' : 'Show desktop';
        announce();
    }

    function toggleShowDesktop() {
        const all = WindowManager.getAll() || [];
        const visible = all.filter((w) => !w.minimized);
        if (visible.length) {
            shownDesktopSet = new Set(visible.map((w) => w.id));
            visible.forEach((w) => WindowManager.minimize(w.id));
        } else if (shownDesktopSet && shownDesktopSet.size) {
            restoring = true;
            try {
                all.filter((w) => shownDesktopSet.has(w.id) && w.minimized)
                    .forEach((w) => WindowManager.restore(w.id));
            } finally {
                restoring = false;
            }
            shownDesktopSet = null;
        }
        syncShowDesktop();
    }

    function forgetShownDesktop() {
        if (restoring || !shownDesktopSet) return;
        shownDesktopSet = null;
        syncShowDesktop();
    }

    // --- Window lifecycle → dock state ---

    function bindWindowEvents() {
        document.addEventListener('window:created', forgetShownDesktop);
        document.addEventListener('window:restored', forgetShownDesktop);
        document.addEventListener('window:created', (e) => {
            const w = e.detail || {};
            if (w.appId) {
                const entry = running.get(w.appId) || { winIds: new Set(), focused: false };
                entry.winIds.add(w.id);
                running.set(w.appId, entry);
            } else {
                transient.set(w.id, { title: w.title, icon: w.icon, focused: true });
            }
            render();
        });

        document.addEventListener('window:closed', (e) => {
            const id = e.detail.id;
            transient.delete(id);
            running.forEach((entry, appId) => {
                entry.winIds.delete(id);
                if (entry.winIds.size === 0) running.delete(appId);
            });
            render();
        });

        document.addEventListener('window:focused', (e) => {
            const id = e.detail.id;
            running.forEach((entry) => { entry.focused = entry.winIds.has(id); });
            transient.forEach((t, winId) => { t.focused = winId === id; });
            render();
        });

        document.addEventListener('window:minimized', () => {
            running.forEach((entry) => { entry.focused = false; });
            transient.forEach((t) => { t.focused = false; });
            render();
        });

        document.addEventListener('window:restored', () => render());
    }

    // --- Init ---

    function init() {
        // btn-menu (start logo) is wired by dock-menu.js — owning it here too
        // double-fires the click, opening then immediately closing the launcher.
        bindWindowEvents();
        const showBtn = showDesktopButton();
        if (showBtn) showBtn.addEventListener('click', toggleShowDesktop);
        document.addEventListener('cseries:layout-changed', render);
        loadApps();
        // New installs should be pinnable without a reload.
        if (window.WSClient && WSClient.on) {
            WSClient.on('app_installed', loadApps);
        }
    }

    document.addEventListener('DOMContentLoaded', init);

    /** The dock's right-click menu for one app, at (x, y) — what a design's
     *  bar asks for when its dock item is right-clicked. */
    function dockMenu(appId, x, y) {
        menuFor(appId, pinnedIds().includes(appId), x, y);
    }

    /** A count on an app's dock icon, shown while the app is in the dock;
     *  0 takes it off. `note` says what is counted ("2 events left today"),
     *  for the icon's tooltip and its name. The dock is drawn again only
     *  when the count or its note changes. */
    function setBadge(appId, count, note) {
        const n = Math.max(0, parseInt(count, 10) || 0);
        const prev = badges.get(appId);
        const text = n > 0 ? String(note || '') : '';
        if (prev ? (prev.n === n && prev.note === text) : n === 0) return;
        if (n > 0) badges.set(appId, { n, note: text });
        else badges.delete(appId);
        render();
    }

    return {
        render,
        dockModel,
        activate,
        dockMenu,
        setBadge,
        toggleShowDesktop,
        get desktopShown() { return !!(shownDesktopSet && shownDesktopSet.size); },
    };
})();

window.Taskbar = Taskbar;
