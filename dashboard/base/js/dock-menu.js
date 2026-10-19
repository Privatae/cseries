/**
 * C-Series — App Launcher + Desktop Icons
 *
 * The start-menu-style launcher popup anchored above the taskbar
 * (fetches /api/applications, renders a searchable glyph grid) and the
 * desktop icon surface. Icons come from icons.js (window.iconMarkup);
 * which apps sit where comes from DesktopLayout.
 *
 * User control (2026-07-14): right-click a launcher tile, desktop icon,
 * or dock item to pin/unpin from the dock and add/remove from the
 * desktop; drag desktop icons to rearrange (order persists per entity).
 *
 * Also handles the support modal toggle.
 */
const DockMenu = (() => {
    let cached = null;
    let isOpen = false;
    let dragDesktopId = null;

    // --- Data ---

    async function loadList() {
        const res = await fetch('/api/applications');
        if (!res.ok) throw new Error('unavailable');
        const data = await res.json();
        cached = data.applications || [];
        return cached;
    }

    function systemIds(apps) {
        return apps.filter((a) => a.system).map((a) => a.id);
    }

    // --- Dock menu open / close ---

    function toggle() {
        if (isOpen) close();
        else open();
    }

    function open() {
        if (isOpen) return;
        isOpen = true;

        const backdrop = document.getElementById('dock-backdrop');
        const menu = document.getElementById('dock-menu');
        const btn = document.getElementById('btn-menu');

        backdrop.classList.remove('hidden');
        menu.classList.remove('hidden');
        if (btn) btn.classList.add('active');

        // Focus search
        const search = document.getElementById('dock-search');
        if (search) {
            search.value = '';
            setTimeout(() => search.focus(), 50);
        }

        renderGrid();
    }

    function close() {
        if (!isOpen) return;
        isOpen = false;

        const backdrop = document.getElementById('dock-backdrop');
        const menu = document.getElementById('dock-menu');
        const btn = document.getElementById('btn-menu');

        backdrop.classList.add('hidden');
        menu.classList.add('hidden');
        if (btn) btn.classList.remove('active');
    }

    // --- Render grid ---

    async function renderGrid(filter) {
        const grid = document.getElementById('dock-grid');
        const countEl = document.getElementById('dock-app-count');
        if (!grid) return;

        try {
            const apps = cached || await loadList();
            const q = (filter || '').trim().toLowerCase();
            const filtered = q
                ? apps.filter(a => (a.name || '').toLowerCase().includes(q) || (a.description || '').toLowerCase().includes(q))
                : apps;

            if (countEl) countEl.textContent = `${apps.length} Installed`;

            if (filtered.length === 0) {
                grid.innerHTML = `<div class="dock-empty"><span class="dock-empty-title">Nothing matches</span>Try a different word.</div>`;
                return;
            }

            grid.innerHTML = filtered.map(a => `
                <button class="dock-tile" data-app-id="${attr(a.id)}" title="${attr(a.description || a.name)}">
                    <div class="dock-tile-icon">${window.iconMarkup(a)}</div>
                    <div class="dock-tile-name">${esc(a.name || a.id)}</div>
                </button>
            `).join('');

            grid.querySelectorAll('.dock-tile').forEach(el => {
                el.addEventListener('click', () => {
                    if (window.AppRunner) window.AppRunner.launch(el.dataset.appId);
                    close();
                });
                el.addEventListener('contextmenu', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    tileMenu(el.dataset.appId, e.clientX, e.clientY);
                });
            });
        } catch (err) {
            grid.innerHTML = `<div class="dock-empty">Could not load apps.</div>`;
        }
    }

    function tileMenu(appId, x, y) {
        const apps = cached || [];
        const defaults = systemIds(apps);
        const pinned = DesktopLayout.isPinned(appId);
        const onDesk = DesktopLayout.onDesktop(appId, defaults);
        ContextMenu.show(x, y, [
            {
                label: 'Open',
                action: () => {
                    if (window.AppRunner) window.AppRunner.launch(appId);
                    close();
                },
            },
            { separator: true },
            pinned
                ? { label: 'Unpin from Dock', action: () => DesktopLayout.unpin(appId) }
                : { label: 'Pin to Dock', action: () => DesktopLayout.pin(appId) },
            onDesk
                ? { label: 'Remove from Desktop', action: () => DesktopLayout.removeFromDesktop(appId, defaults) }
                : { label: 'Add to Desktop', action: () => DesktopLayout.addToDesktop(appId, defaults) },
        ]);
    }

    // --- Desktop icons ---

    async function seedDesktopIcons() {
        const container = document.getElementById('desktop-icons');
        if (!container) return;
        try {
            const apps = cached || await loadList();
            await DesktopLayout.ready;

            const byId = new Map(apps.map((a) => [a.id, a]));
            const ids = DesktopLayout.desktopIds(systemIds(apps))
                .filter((id) => byId.has(id));

            container.innerHTML = ids.map((id) => {
                const a = byId.get(id);
                return `
                <button class="desktop-icon" data-app-id="${attr(a.id)}" draggable="true">
                    <div class="icon-image">${window.iconMarkup(a, 40)}</div>
                    <div class="icon-label">${esc(a.name || a.id)}</div>
                </button>
            `; }).join('');

            container.querySelectorAll('.desktop-icon').forEach(el => {
                el.addEventListener('click', () => {
                    el.classList.add('selected');
                    setTimeout(() => el.classList.remove('selected'), 200);
                    if (window.AppRunner) window.AppRunner.launch(el.dataset.appId);
                });
                el.addEventListener('contextmenu', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    desktopIconMenu(el.dataset.appId, e.clientX, e.clientY);
                });
                bindDesktopDrag(el, container);
            });
        } catch (err) {
            // Silent — desktop without icons is fine
        }
    }

    function desktopIconMenu(appId, x, y) {
        const apps = cached || [];
        const defaults = systemIds(apps);
        const pinned = DesktopLayout.isPinned(appId);
        ContextMenu.show(x, y, [
            {
                label: 'Open',
                action: () => { if (window.AppRunner) window.AppRunner.launch(appId); },
            },
            { separator: true },
            pinned
                ? { label: 'Unpin from Dock', action: () => DesktopLayout.unpin(appId) }
                : { label: 'Pin to Dock', action: () => DesktopLayout.pin(appId) },
            {
                label: 'Remove from Desktop',
                danger: true,
                action: () => DesktopLayout.removeFromDesktop(appId, defaults),
            },
        ]);
    }

    /**
     * Drag-to-rearrange. HTML5 DnD: the dragged icon live-reorders as it
     * passes over siblings; drop commits the DOM order to DesktopLayout.
     */
    function bindDesktopDrag(el, container) {
        el.addEventListener('dragstart', (e) => {
            dragDesktopId = el.dataset.appId;
            el.classList.add('dragging');
            if (e.dataTransfer) {
                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('text/plain', dragDesktopId);
            }
        });
        el.addEventListener('dragend', () => {
            el.classList.remove('dragging');
            if (!dragDesktopId) return;
            dragDesktopId = null;
            const order = [];
            container.querySelectorAll('.desktop-icon').forEach((n) => {
                if (n.dataset.appId) order.push(n.dataset.appId);
            });
            if (order.length) DesktopLayout.setDesktopOrder(order);
        });
        el.addEventListener('dragover', (e) => {
            if (!dragDesktopId || dragDesktopId === el.dataset.appId) return;
            e.preventDefault();
            const dragging = container.querySelector('.desktop-icon.dragging');
            if (!dragging) return;
            // Column-flow grid: vertical midpoint decides before/after.
            const r = el.getBoundingClientRect();
            const before = e.clientY < r.top + r.height / 2;
            container.insertBefore(dragging, before ? el : el.nextSibling);
        });
    }

    // Empty-desktop right-click: quick access to the launcher, wallpaper,
    // and a layout reset. #desktop swallows native context menus. Also
    // shown on behalf of the HTML desktop surface (desktop-surface.js
    // relays a right-click inside its sandboxed frame as desktop:menu —
    // the menu itself always renders HERE, from the shell's own items).
    function surfaceMenu(x, y) {
        // Shell widget toggles (widget-layer.js) — built at show time so
        // the labels track current visibility.
        const widgetItems = window.WidgetLayer
            ? window.WidgetLayer.list().map((w) => ({
                label: (window.WidgetLayer.isVisible(w.id) ? 'Hide ' : 'Show ') + w.title + ' Widget',
                action: () => window.WidgetLayer.toggle(w.id),
            }))
            : [];
        ContextMenu.show(x, y, [
            { label: 'All Apps', action: open },
            {
                label: 'Change Wallpaper',
                action: () => {
                    if (window.AppRunner) window.AppRunner.launch('settings', { view: 'appearance' });
                },
            },
            ...widgetItems,
            { separator: true },
            {
                label: 'Reset Layout',
                action: () => DesktopLayout.resetLayout(),
            },
        ]);
    }

    function bindDesktopSurfaceMenu() {
        const desk = document.getElementById('desktop');
        if (!desk) return;
        desk.addEventListener('contextmenu', (e) => {
            if (e.target.closest('.window') || e.target.closest('.desktop-icon')
                || e.target.closest('.widget-card')) return;
            e.preventDefault();
            surfaceMenu(e.clientX, e.clientY);
        });
    }

    // --- Utilities ---

    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s || '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    function attr(s) {
        return String(s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // --- Refresh (app_installed pushes re-list) ---

    async function refresh() {
        cached = null;
        try { await loadList(); } catch (_) {}
        if (isOpen) renderGrid(document.getElementById('dock-search')?.value);
        seedDesktopIcons();
    }

    // --- Init ---

    function init() {
        // Taskbar start button
        const btnMenu = document.getElementById('btn-menu');
        if (btnMenu) {
            btnMenu.addEventListener('click', (e) => {
                e.stopPropagation();
                toggle();
            });
        }

        // Backdrop click closes
        const backdrop = document.getElementById('dock-backdrop');
        if (backdrop) {
            backdrop.addEventListener('mousedown', close);
        }

        // Search
        const search = document.getElementById('dock-search');
        if (search) {
            search.addEventListener('input', () => renderGrid(search.value));
            search.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') close();
                if (e.key === 'Enter') {
                    const first = document.querySelector('.dock-tile');
                    if (first) {
                        if (window.AppRunner) window.AppRunner.launch(first.dataset.appId);
                        close();
                    }
                }
            });
        }

        // Support modal
        const btnSupport = document.getElementById('btn-support');
        const supportModal = document.getElementById('support-modal');
        if (btnSupport && supportModal) {
            btnSupport.addEventListener('click', () => {
                supportModal.classList.toggle('hidden');
            });
        }

        // Close dock on outside click
        document.addEventListener('click', (e) => {
            if (!isOpen) return;
            const menu = document.getElementById('dock-menu');
            const btn = document.getElementById('btn-menu');
            if (menu && !menu.contains(e.target) && btn && !btn.contains(e.target)) {
                close();
            }
        });

        // Escape closes
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && isOpen) close();
        });

        // Desktop surface
        seedDesktopIcons();
        bindDesktopSurfaceMenu();
        document.addEventListener('cseries:layout-changed', () => seedDesktopIcons());

        // Update dock entity name when WS connects
        try {
            fetch('/api/system/status').then(r => r.ok ? r.json() : null).then(s => {
                if (s && s.entity_display_name) {
                    const el = document.getElementById('dock-entity');
                    if (el) el.textContent = `${s.entity_display_name} · ready`;
                }
            }).catch(() => {});
        } catch (_) {}
    }

    document.addEventListener('DOMContentLoaded', init);

    return {
        open, close, toggle, seedDesktopIcons, refresh,
        // For the HTML desktop surface (desktop-surface.js): the same two
        // menus a right-click on the bare desktop / on an app would show.
        showSurfaceMenu: surfaceMenu,
        showAppMenu: tileMenu,
    };
})();

window.DockMenu = DockMenu;
// Backward compat — old code references AppLauncher (ws-client's
// app_installed handler calls AppLauncher.refresh()).
window.AppLauncher = DockMenu;
