/**
 * Deep link — a link into the host names the app to open in its query.
 *
 *     https://<host address>/?open=<app_id>[&view=<view>]
 *
 * The reach-out deep-link contract (server/deep_link.py writes it): a doorbell
 * carries one link back to the thing that needs the user, and a link that only
 * ever landed on the bare desktop made them go looking for it. `view` is
 * whatever the app itself understands — the same value the host's open_app
 * tool passes (`finding:<id>` in Oversight, `artifact:<id>` in Artifacts) —
 * and reaches the app through AppRunner.launch exactly as a dock click with a
 * view would.
 *
 * WHY THE QUERY, NOT THE FRAGMENT. A reach-out is opened wherever the mail
 * lands, often on a phone with no session here. The router then sends the
 * browser to sign in and back, carrying the path and query through its `rd`
 * parameter; a fragment never reaches a server, and the sign-in pages navigate
 * by script, which drops it — so a fragment link opened signed-out landed on
 * the bare desktop. server/deep_link.py has the rest.
 *
 * ONCE. The two parameters are removed from the address (replaceState) before
 * the launch, so a reload does not open the window again. Anything malformed is
 * ignored: an unknown id fails the launch the way a stale dock icon would, and
 * the desktop is never blocked on it.
 */

(function () {
    'use strict';

    // The manifest's own id rule (server/applications/manifest.py _ID_RE), so
    // any app that can be installed can be linked to, and nothing else. A view
    // is the app's own vocabulary, bounded so a pasted link cannot smuggle a
    // page's worth of text into an app's first render.
    const APP_ID = /^[a-z0-9][a-z0-9-]{0,63}$/;
    const VIEW_MAX = 200;

    // URLSearchParams decodes percent-escapes and reads `+` as a space, the
    // query's own rule; the writer encodes a literal `+` as %2B. (Across a
    // sign-in the router unquotes `rd` once more, so a %2B can still arrive
    // as a space there — no view written today contains one.)
    function readLink() {
        let params;
        try { params = new URLSearchParams(window.location.search || ''); } catch (_) { return null; }
        const appId = params.get('open');
        if (!appId || !APP_ID.test(appId)) return null;
        const view = params.get('view');
        return { appId, view: view && view.length <= VIEW_MAX ? view : null };
    }

    // The address without `open` and `view`, anything else in the query kept.
    function addressWithout() {
        const params = new URLSearchParams(window.location.search || '');
        params.delete('open');
        params.delete('view');
        const rest = params.toString();
        return window.location.pathname + (rest ? '?' + rest : '') + window.location.hash;
    }

    function openFromLink() {
        const link = readLink();
        if (!link) return;
        try {
            history.replaceState(null, '', addressWithout());
        } catch (_) { /* a history that refuses still opens the app once */ }
        try {
            if (window.AppRunner && typeof window.AppRunner.launch === 'function') {
                window.AppRunner.launch(link.appId, link.view ? { view: link.view } : undefined);
            }
        } catch (_) { /* never break the desktop over a link */ }
    }

    // Armed at PARSE time, for the reason walkthrough-autolaunch.js gives:
    // desktop-restore can dispatch 'desktop:restored' between the two
    // DOMContentLoaded listeners on a fresh host, and a listener added inside
    // DOMContentLoaded misses it. Opening after restore means the linked window
    // lands on top of the restored ones rather than under them.
    document.addEventListener('desktop:restored', openFromLink, { once: true });

    window.DeepLink = { readLink };
})();
