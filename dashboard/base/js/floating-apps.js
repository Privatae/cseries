/**
 * Floating-app runtime — bottom-right bubbles that launch first-class
 * app windows via the existing AppRunner. Used for always-visible apps
 * like Circles where the surface needs to be one click away regardless
 * of what's on the desktop.
 *
 * Bubbles do not own a UI of their own — they just trigger AppRunner.launch
 * and keep their "active" state in sync. Notification badges are set
 * externally via FloatingApps.setBadge(appId, count).
 */
const FloatingApps = (() => {
    const REGISTRY = {};

    // The one open menu, if any. Module-scoped rather than per-bubble so two
    // bubbles can never both be showing one.
    let openMenuEl = null;

    // The bubble the open menu hangs off, so a click inside it can be told
    // apart from a click outside it.
    let openMenuOwner = null;

    function closeMenu() {
        // The listener teardown is UNCONDITIONAL — never behind an early
        // return on openMenuEl. An earlier version returned first when the
        // menu was already gone, which left the document listeners bound for
        // the rest of the session and broke Escape and outside-click from the
        // third menu onward. Caught by the browser test, not by reading it.
        document.removeEventListener('click', onDocClick, true);
        document.removeEventListener('keydown', onKeydown, true);
        window.removeEventListener('blur', closeMenu);
        if (openMenuEl) openMenuEl.remove();
        if (openMenuOwner) openMenuOwner.classList.remove('menu-open');
        openMenuEl = null;
        openMenuOwner = null;
    }

    function onDocClick(e) {
        // A click on the bubble, or inside its menu, is not an "outside"
        // click. BOTH have to be checked now that the menu is a sibling of the
        // bubble rather than a child of it. This is what lets the listeners
        // bind SYNCHRONOUSLY: without it the very click that opened the menu
        // would close it again, which is what the deferred binding used to
        // work around — and that deferral was the race above.
        if (openMenuOwner && openMenuOwner.contains(e.target)) return;
        if (openMenuEl && openMenuEl.contains(e.target)) return;
        closeMenu();
    }

    function onKeydown(e) {
        if (e.key !== 'Escape') return;
        // Grab the owner BEFORE closing — closeMenu() clears it, and focus has
        // to land back on the bubble that opened the menu or a keyboard user
        // is dropped at the top of the document.
        const owner = openMenuOwner;
        closeMenu();
        if (owner) owner.focus();
    }

    /**
     * A small chooser anchored above the bubble. Built from the DOM rather
     * than innerHTML so an entry's label can never be read as markup — these
     * labels are ours today, but a bubble is a registration point and the next
     * caller's label may not be.
     */
    function openMenu(btn, spec) {
        if (openMenuEl) { closeMenu(); return; }   // second click closes

        const menu = document.createElement('div');
        menu.className = 'floating-bubble-menu';
        menu.setAttribute('role', 'menu');
        menu.setAttribute('aria-label', spec.title || spec.appId);

        for (const item of spec.menu) {
            const row = document.createElement('button');
            row.type = 'button';
            row.className = 'floating-bubble-menu-item';
            row.setAttribute('role', 'menuitem');
            row.textContent = item.label;
            row.addEventListener('click', (ev) => {
                ev.stopPropagation();
                closeMenu();
                try { item.run(); } catch (_) { /* a dead entry must not wedge the shell */ }
            });
            menu.appendChild(row);
        }

        // Chrome and placement live in floating-apps.css against the design
        // tokens. An earlier version set them here as an
        // inline cssText with hex fallbacks for tokens that do not exist
        // (--cseries-surface, --cseries-border), so the fallback always won and it
        // was inlined hex wearing a var().

        // Appended to the DOCK, not to the bubble. The bubble is a <button>,
        // and nesting <button role="menuitem"> inside it produced a menu that
        // works in Chromium but is malformed: ARIA gives buttons presentational
        // children, so a screen reader is not obliged to expose the items at
        // all. The dock is position:fixed, so it is already a containing block
        // and the menu anchors above it with the CSS it already had.
        (btn.parentElement || document.body).appendChild(menu);
        openMenuEl = menu;
        openMenuOwner = btn;
        btn.classList.add('menu-open');

        // Bound synchronously and in the CAPTURE phase. Safe to bind during
        // the opening click because onDocClick ignores clicks on the bubble
        // and inside the menu; deferring this to the next frame instead is
        // what produced the listener leak the browser test caught.
        document.addEventListener('click', onDocClick, true);
        document.addEventListener('keydown', onKeydown, true);
        // A click inside an app-window iframe never reaches this document, so
        // onDocClick alone leaves the menu hanging over an app the user has
        // just started using. Focus leaving the window is the signal that
        // catches it.
        window.addEventListener('blur', closeMenu);

        const first = menu.querySelector('.floating-bubble-menu-item');
        if (first) first.focus();
    }

    function register(spec) {
        if (!spec || !spec.appId) return;
        if (REGISTRY[spec.appId]) return REGISTRY[spec.appId].el;

        const slot = document.getElementById('floating-dock');
        if (!slot) return;

        const btn = document.createElement('button');
        btn.className = 'floating-bubble';
        btn.type = 'button';
        btn.dataset.appId = spec.appId;
        btn.setAttribute('aria-label', spec.title || spec.appId);

        if (spec.iconUrl) {
            btn.innerHTML = `<img src="${spec.iconUrl}" alt="" style="width:36px;height:36px;object-fit:contain;border-radius:50%;">`;
        } else {
            // Shared glyph library — same mark as desktop/dock/launcher.
            btn.innerHTML = window.iconMarkup
                ? window.iconMarkup({ id: spec.appId, name: spec.title }, 30)
                : `<span style="font-size:18px;font-weight:600;color:var(--cseries-linen)">${(spec.title || spec.appId).charAt(0).toUpperCase()}</span>`;
        }

        const tip = document.createElement('span');
        tip.className = 'floating-bubble-tip';
        tip.textContent = spec.title || spec.appId;
        btn.appendChild(tip);

        btn.addEventListener('click', (ev) => {
            // A bubble with a `menu` offers a CHOICE instead of launching.
            // Clicking the Circles icon offers two options: Circles and Host
            // Manager. A bubble without one
            // behaves exactly as it always has.
            if (Array.isArray(spec.menu) && spec.menu.length) {
                ev.stopPropagation();
                openMenu(btn, spec);
                return;
            }
            if (window.AppRunner && typeof AppRunner.launch === 'function') {
                AppRunner.launch(spec.appId);
                btn.classList.add('active');
            }
        });

        slot.appendChild(btn);
        REGISTRY[spec.appId] = { el: btn, spec };
        return btn;
    }

    function setBadge(appId, count) {
        const entry = REGISTRY[appId];
        if (!entry) return;
        let badge = entry.el.querySelector('.floating-bubble-badge');
        if (count > 0) {
            if (!badge) {
                badge = document.createElement('span');
                badge.className = 'floating-bubble-badge';
                entry.el.appendChild(badge);
            }
            badge.textContent = count > 99 ? '99+' : String(count);
        } else if (badge) {
            badge.remove();
        }
    }

    function setActive(appId, active) {
        const entry = REGISTRY[appId];
        if (!entry) return;
        entry.el.classList.toggle('active', !!active);
    }

    // Built-in registrations
    document.addEventListener('DOMContentLoaded', () => {
        // Circles — paid app, anchored bottom-right. Uses the shared
        // "linked rings" glyph from icons.js; swapped to the 1:1 PNG when
        // it lands in /system-apps/circles/icon.png (probe below).
        register({
            appId: 'circles',
            title: 'Circles',
            // Two choices. Circles is the app this bubble has
            // always launched; Host Manager is the grid of every host on the
            // account, which lets the user switch to another host from here
            // instead of leaving for /account and coming back.
            menu: [
                {
                    label: 'Circles',
                    run: () => {
                        if (window.AppRunner && typeof AppRunner.launch === 'function') {
                            AppRunner.launch('circles');
                            setActive('circles', true);
                        }
                    },
                },
                {
                    label: 'Host Manager',
                    // Opens on privatae.ai, where the roster, the folders and
                    // the capacity pool already live. It is NOT framed into a
                    // desktop window: the entity server sets X-Frame-Options
                    // SAMEORIGIN (server/main.py) and the account site's own
                    // framing headers are set by Caddy at the edge, not in the
                    // frontend repo — so framing it is an edge change with its
                    // own review, not a line here. Same target/rel as the
                    // existing outbound link at dashboard/base/index.html:121.
                    run: () => window.open(
                        'https://privatae.ai/account#host-manager',
                        '_blank',
                        'noopener',
                    ),
                },
            ],
        });

        // Probe whether a real icon exists; if so, swap the SVG for it.
        const probe = new Image();
        probe.onload = () => {
            const entry = REGISTRY['circles'];
            if (!entry) return;
            entry.el.innerHTML = '';
            const img = document.createElement('img');
            img.src = '/system-apps/circles/icon.png';
            img.alt = '';
            img.style.cssText = 'width:44px;height:44px;object-fit:contain;border-radius:50%;';
            entry.el.appendChild(img);
            const tip = document.createElement('span');
            tip.className = 'floating-bubble-tip';
            tip.textContent = 'Circles';
            entry.el.appendChild(tip);
        };
        probe.src = '/system-apps/circles/icon.png';

        // Two counts, polled together every 60 s:
        //   - unread inbox messages, on the Circles bubble;
        //   - what is still scheduled for the rest of today, on the Calendar
        //     app's dock icon (Taskbar.setBadge). The calendar is an app of
        //     its own, and its count is not on the Circles bubble.
        //
        // Both endpoints are IN THIS POD — server/routes/inbox.py and
        // server/routes/calendar.py. Both are gated (read:inbox,
        // read:calendar), so they carry the shell's scoped token. On 401 the
        // kernel secret rotated: re-mint once and retry, rather than badging 0
        // for the rest of the session.
        async function badgeFetch(url) {
            let r = await fetch(url, {
                credentials: 'same-origin',
                headers: ShellToken.headers(),
            });
            if (r.status === 401) {
                await ShellToken.retry();
                r = await fetch(url, {
                    credentials: 'same-origin',
                    headers: ShellToken.headers(),
                });
            }
            return r;
        }

        // The route answers {unread: n} (memory_handlers.handle_inbox_unread_
        // count). This read took `count`, which the answer never has, so the
        // inbox half of the bubble's number was always 0.
        async function pollCirclesBadge() {
            try {
                const r = await badgeFetch('/api/inbox/unread-count');
                if (r.ok) {
                    const j = await r.json();
                    setBadge('circles', j.unread || 0);
                }
            } catch (_) { /* offline / not authed — keep prior badge */ }
        }

        // From now to the person's own midnight, scheduled, soonest first: the
        // read counts exactly the rest of today. (It used to take the 200
        // newest scheduled rows and count today's among them, which missed
        // today's whenever 200 were set further ahead.)
        async function pollCalendarBadge() {
            if (!window.Taskbar || typeof window.Taskbar.setBadge !== 'function') return;
            try {
                const now = new Date();
                const end = new Date(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1).getTime() - 1);
                const q = new URLSearchParams({
                    status: 'scheduled', since: now.toISOString(), until: end.toISOString(),
                    order: 'asc', limit: '500',
                });
                const r = await badgeFetch('/api/calendar/list?' + q.toString());
                if (!r.ok) return;
                const j = await r.json();
                const n = (j.events || []).length;
                window.Taskbar.setBadge('calendar', n,
                    (j.next_offset != null ? 'more than ' + n : String(n))
                    + (n === 1 ? ' event' : ' events') + ' left today');
            } catch (_) { /* offline / not authed — keep prior badge */ }
        }

        function pollBadges() {
            pollCirclesBadge();
            pollCalendarBadge();
        }
        pollBadges();
        setInterval(pollBadges, 60000);
    });

    return { register, setBadge, setActive };
})();

window.FloatingApps = FloatingApps;
