/**
 * App frame bootstrap — runs inside an ISOLATED app's own origin.
 *
 * The kernel adds this right after the App SDK to every isolated app's HTML
 * (server/applications/proxy.py, _inject_frame_init). It does two things the
 * desktop cannot do from outside the frame:
 *
 *  1. THE WINDOW TOKEN. An isolated app's token is minted on the app's own
 *     origin (POST /api/applications/<id>/frame-token) and never handed to the
 *     desktop: every page on the desktop's origin can read what the shell
 *     reads, so a token given to the shell would be given to all of them. Only
 *     this app's files are served here, and no other origin can read the
 *     answer, so the token reaches this app and nothing else. It is passed to
 *     the SDK through the SDK's own init message, raised on this window with
 *     the SDK's framer as its source — the same message the shell used to
 *     post from outside.
 *
 *  2. THE SESSION. Each app origin holds its own router session cookie, which
 *     lives exactly as long as this browser's Host session does (the router's
 *     use clock, shared by every Host origin: an hour after the person last
 *     used the desktop, at most 24h). This asks on a 5-minute rhythm whether
 *     it lives — asking extends nothing — and when the router says this
 *     origin's cookie is gone (a new zone session, after the desktop's own
 *     sign-in), it re-bootstraps it with a same-origin request through the
 *     sign-in gate, which JOINS the desktop's live session and can never start
 *     one. Nothing is shown: if the session itself is gone, the desktop's
 *     "session expired" overlay is the one prompt, and its Continue reloads
 *     every window.
 *
 *  3. THE FIRST FAILED REQUEST. When the session ends, this
 *     origin's requests are redirected to the sign-in site, another origin,
 *     and the app's fetch() rejects with "Failed to fetch". A same-origin
 *     fetch that rejects now runs the renewal at once instead of at the next
 *     5-minute tick; and when the silent re-bootstrap did not bring the
 *     session back (the renewal still answers 401), the desktop is asked to
 *     check its own ({type: 'cseries:session-check'} to the parent), so its
 *     prompt shows now. The desktop asks the router itself; this message
 *     claims nothing.
 *
 * Never holds a request, never gates first paint, fails quiet.
 */
(function () {
    'use strict';
    if (window.__cseriesFrameInit) return;
    window.__cseriesFrameInit = true;

    var me = document.currentScript;
    var appId = me && me.getAttribute('data-app-id');
    if (!appId || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(appId)) return;

    // The router's own paths (its BOOTSTRAP_PATH / RENEW_PATH).
    var RENEW = '/__host/renew';
    var BOOTSTRAP = '/__host/session?rd=' + encodeURIComponent(RENEW);
    var INTERVAL_MS = 5 * 60 * 1000;

    function deliver(token) {
        try {
            window.dispatchEvent(new MessageEvent('message', {
                data: { type: 'cseries:app-init', token: token, app_id: appId },
                origin: window.location.origin,
                source: window.parent,
            }));
        } catch (e) { /* no SDK here, or a browser without MessageEvent init */ }
    }

    function fetchToken() {
        return fetch('/api/applications/' + encodeURIComponent(appId) + '/frame-token', {
            method: 'POST', credentials: 'same-origin', cache: 'no-store',
        }).then(function (r) {
            return r.ok ? r.json() : null;
        }).then(function (j) {
            if (j && typeof j.token === 'string' && j.token) deliver(j.token);
        }).catch(function () { /* the SDK's own 5s ready timeout reports it */ });
    }

    // This origin's own fetch, before the wrapper below (the SDK's shim, when
    // the SDK is here: it is injected first).
    var rawFetch = window.fetch.bind(window);
    var manual = { cache: 'no-store', credentials: 'same-origin', redirect: 'manual' };

    function askDesktop() {
        try {
            if (window.parent && window.parent !== window) {
                window.parent.postMessage({ type: 'cseries:session-check' }, '*');
            }
        } catch (e) { /* detached */ }
    }

    // Load bounds, the same two the desktop's keeper has (session-keeper.js
    // check): once this origin knows the zone session is gone, a failed
    // request asks nothing more — an app that polls keeps failing until
    // Continue, and behind the edge gate every renewal and re-bootstrap is one
    // more outpost check. Only `force` (the 5-minute tick, a tab-wake, coming
    // back online) asks again. And renewals are at least RECHECK_MS apart; a
    // failure inside the gap gets one renewal at its end.
    var RECHECK_MS = 2000;
    var renewing = null;
    var trailing = null;
    var trailingForce = false;
    var lastAt = 0;
    var ended = false;
    function renew(force) {
        force = force === true;
        if (!force && ended) return Promise.resolve();
        if (renewing) return renewing;
        var wait = lastAt + RECHECK_MS - Date.now();
        if (wait > 0) {
            trailingForce = trailingForce || force;
            if (!trailing) {
                trailing = new Promise(function (resolve) {
                    setTimeout(function () {
                        var f = trailingForce;
                        trailing = null;
                        trailingForce = false;
                        resolve(renew(f));
                    }, wait);
                });
            }
            return trailing;
        }
        renewing = rawFetch(RENEW, manual)
            .then(function (r) {
                if (r.status !== 401) {
                    if (r.status === 204) ended = false;
                    return;
                }
                // Same-origin through the gate: the router mints this origin's
                // cookie on the redirect it answers with (a manual-redirect
                // fetch still stores it). A zone session that is gone redirects
                // to the sign-in site instead, which this does not follow — no
                // background request reaches the sign-in site. Whether it
                // worked is read from the renewal after it: still 401 means
                // the zone session is gone, and the desktop's prompt is the one
                // that asks the person to sign in.
                return rawFetch(BOOTSTRAP, manual)
                    .catch(function () {})
                    .then(function () { return rawFetch(RENEW, manual); })
                    .then(function (again) {
                        if (again.status === 401) {
                            ended = true;
                            askDesktop();
                        } else if (again.status === 204) {
                            ended = false;
                        }
                    });
            })
            .catch(function () { /* offline: the desktop owns that story */ })
            .then(function () { lastAt = Date.now(); renewing = null; });
        return renewing;
    }
    function forced() { renew(true); }

    // A same-origin request that rejects is what an ended session looks like
    // from inside the app; ask at once. The app gets exactly the promise it
    // would have got. The SDK's shim tag is carried over so nothing that
    // checks for it wraps twice.
    //
    // ONCE THE SESSION IS KNOWN ENDED, THIS ORIGIN STOPS ASKING:
    // a same-origin request is refused here, with the TypeError it would have
    // got, and nothing is sent — an app that polls no longer sends every poll
    // to the gate. This origin can only JOIN a live session
    // (the router's use clock), so nothing here can bring one back: the
    // desktop signs in, and its 'session back' announcement makes this origin
    // ask again (below). Every renewal this file sends is plain — it asks,
    // and extends nothing; real use is counted by the desktop alone.
    function sameOrigin(input) {
        try {
            var raw = (input && typeof input === 'object' && 'url' in input) ? input.url : String(input);
            return new URL(raw, window.location.href).origin === window.location.origin;
        } catch (x) { return false; }
    }
    var wrapped = function (input, init) {
        if (ended && sameOrigin(input)) return Promise.reject(new TypeError('Host session ended'));
        var p = rawFetch(input, init);
        p.then(function (r) {
            if (r && r.status === 401 && sameOrigin(input)) renew();
        }, function (e) {
            if (!e || e.name !== 'TypeError' || !sameOrigin(input)) return;
            renew();
        });
        return p;
    };
    if (window.fetch.__cseriesShim) wrapped.__cseriesShim = true;
    window.fetch = wrapped;

    // The desktop announces a return ({type: 'cseries:session', state: 'ok'},
    // session-keeper.js). Only the frame that framed us may say it, and it is
    // only a reason to ask the router, which answers for itself.
    window.addEventListener('message', function (ev) {
        var d = ev.data;
        if (!ended || ev.source !== window.parent || window.parent === window) return;
        if (d && d.type === 'cseries:session' && d.state === 'ok') forced();
    });

    fetchToken();
    setInterval(forced, INTERVAL_MS);
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') forced();
    });
    window.addEventListener('online', forced);
})();
