/**
 * Walkthrough autolaunch — a fresh host opens Getting Started.
 *
 * Fires once per boot, after DesktopRestore settles (the 'desktop:restored'
 * event), so the tour window never races window restore. The tour's state
 * lives in the shell-trusted appearance lane: GET /api/appearance →
 * desktop_layout.walkthrough. `done !== true` means the tour was never
 * finished or dismissed, so the app opens itself — and keeps opening on
 * each boot until the user finishes it or hits "Start building".
 *
 * Every failure mode (appearance unreadable, app list down, launch throw)
 * is swallowed: the autolaunch must NEVER break the desktop — on any doubt
 * it simply stays closed, and the app remains one dock click away.
 */

(function () {
    'use strict';

    const APP_ID = 'getting-started';

    async function maybeLaunch() {
        // 1. Tour state. No read, no guess — never launch on a maybe.
        let walkthrough = null;
        try {
            const res = await fetch('/api/appearance');
            if (!res.ok) return;
            const data = await res.json();
            walkthrough = data && data.desktop_layout && data.desktop_layout.walkthrough;
        } catch (_) {
            return;
        }
        if (walkthrough && walkthrough.done === true) return;

        // 2. The app must actually be installed (image-baked on fresh
        // images; older entities may not have it — that's fine).
        try {
            const res = await fetch('/api/applications');
            if (!res.ok) return;
            const data = await res.json();
            const installed = (data.applications || []).some((a) => a && a.id === APP_ID);
            if (!installed) return;
        } catch (_) {
            return;
        }

        // 3. Launch. A throw here must never reach the desktop.
        try {
            if (window.AppRunner && typeof window.AppRunner.launch === 'function') {
                window.AppRunner.launch(APP_ID);
            }
        } catch (_) { /* stay closed */ }
    }

    // Armed at PARSE time, deliberately NOT inside DOMContentLoaded.
    // desktop-restore's restore() returns SYNCHRONOUSLY when localStorage
    // holds no saved layout (js/desktop-restore.js: `if (wins.length === 0)
    // return`) — the fresh-host case — so its .finally() dispatches
    // 'desktop:restored' in the microtask checkpoint browsers run BETWEEN
    // the two DOMContentLoaded listeners. A listener registered in our own
    // DOMContentLoaded handler is armed after that, and misses the event on
    // exactly the boot this feature exists for. Measured in Chromium
    // 2026-08-15: fresh host never launched, returning user did.
    document.addEventListener('desktop:restored', () => {
        maybeLaunch().catch(() => { /* best-effort by design */ });
    }, { once: true });
})();
