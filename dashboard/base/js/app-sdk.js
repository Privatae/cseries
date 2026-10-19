/**
 * C-Series App SDK — the in-app half of the app-origin-isolation boundary.
 *
 * The kernel injects this into the <head> of an isolated app's served HTML
 * (proxy.py, for apps in kernel.SDK_MANAGED_APPS). The app is loaded by the
 * shell from a SEPARATE origin (the host's apps subdomain), so the
 * shell cannot reach into the iframe to inject the token — it arrives over
 * postMessage. This SDK receives it (+ the app's own id) and stamps the token
 * onto API calls, and exposes an explicit persistence API (CSeriesApp.storage).
 *
 * NON-BLOCKING BY DESIGN (an earlier regression — a render-blocking
 * script that HELD every fetch until a token arrived — is deliberately designed
 * out): the fetch wrapper installs synchronously, adds X-Application-Token ONLY
 * if it is already set, and NEVER awaits/holds/delays a request. It gates nothing
 * about rendering. (CSeriesApp.storage is different — those are explicit async calls
 * the app awaits, so they DO wait for the one-time init handshake; they never gate
 * the app's first paint.)
 *
 * The app calls its OWN origin's /api (apps-<handle>/api → same pod), so no CORS
 * and no URL rewriting are needed — only the token + app_id cross, by message.
 *
 * Trust: the token is GIVEN to the app by its framer; a forged one grants no extra
 * access (the kernel validates scopes server-side, and config is token-scoped to
 * the caller's own app_id — an app can only reach its own storage).
 */
(function () {
    'use strict';
    if (window.CSeriesApp && window.CSeriesApp.__installed) return;

    var _token = null;
    var _appId = null;
    var _resolveReady;
    var _ready = new Promise(function (res) { _resolveReady = res; });

    // Fetch wrapper — synchronous install, never holds. Tagged so a re-run is a no-op.
    // SAME-ORIGIN ONLY (SDK 1.3.0): the token is the window's KERNEL credential;
    // stamping it on cross-origin fetches both LEAKED it to every declared
    // third-party host that tolerates the header, and broke ones that don't —
    // api.weather.gov rejects the preflight ("x-application-token is not
    // allowed by Access-Control-Allow-Headers"), measured live on the first
    // SDK-managed app with browser-direct network[] hosts (national-weather).
    // A cross-origin fetch passes through untouched.
    if (typeof window.fetch === 'function' && !window.fetch.__cseriesShim) {
        var orig = window.fetch.bind(window);
        var wrapped = function (input, init) {
            var sameOrigin = true;
            try {
                var raw = (input && typeof input === 'object' && 'url' in input)
                    ? input.url : String(input);
                sameOrigin = new URL(raw, window.location.href).origin
                    === window.location.origin;
            } catch (e) { /* unparseable — let fetch fail its own way, unstamped */
                sameOrigin = false;
            }
            if (!sameOrigin) return orig(input, init);
            init = init || {};
            var headers = new Headers(init.headers || {});
            if (_token && !headers.has('X-Application-Token')) {
                headers.set('X-Application-Token', _token);
            }
            init.headers = headers;
            return orig(input, init);   // never awaits, never holds
        };
        wrapped.__cseriesShim = true;
        window.fetch = wrapped;
    }

    window.addEventListener('message', function (ev) {
        // Token + app_id are handed to us by our framer (the shell). Accept only
        // from the parent. (Origin isn't pinned — the shell origin isn't known a
        // priori; a forged token grants no access, and config is token-scoped.)
        if (ev.source !== window.parent) return;
        var d = ev.data;
        if (!d || d.type !== 'cseries:app-init') return;
        if (d.token) _token = String(d.token);
        if (d.app_id) _appId = String(d.app_id);
        _resolveReady(true);
        try {
            window.parent.postMessage(
                { type: 'cseries:app-init-ack', token_present: !!_token }, '*');
        } catch (e) { /* detached */ }
    });

    // Resolve once init arrives; cap the wait so a non-isolated/unframed app fails
    // fast instead of hanging an awaited storage call forever.
    function _awaitReady() {
        return Promise.race([
            _ready,
            new Promise(function (_, rej) {
                setTimeout(function () { rej(new Error('CSeriesApp: no init handshake (app not isolated?)')); }, 5000);
            }),
        ]).then(function () {
            if (!_appId) throw new Error('CSeriesApp.storage: app_id unavailable');
        });
    }

    function _cfgUrl(key) {
        var base = '/api/applications/' + encodeURIComponent(_appId) + '/config';
        return key != null ? base + '/' + encodeURIComponent(key) : base;
    }

    // CSeriesApp.storage — explicit async KV persistence, backed by the per-app PG
    // config store (token-scoped server-side to the caller's own app_id). NOT a
    // localStorage shim: apps call these methods; nothing is silently hijacked.
    // Use this instead of localStorage — localStorage is per-origin and resets when
    // an app moves to the isolated apps-<handle> origin.
    var storage = {
        get: function (key) {
            return _awaitReady().then(function () {
                return window.fetch(_cfgUrl(key)).then(function (r) {
                    if (r.status === 404) return null;
                    if (!r.ok) throw new Error('CSeriesApp.storage.get failed: ' + r.status);
                    return r.json().then(function (j) { return j.value; });
                });
            });
        },
        set: function (key, value) {
            return _awaitReady().then(function () {
                return window.fetch(_cfgUrl(key), {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(value),
                }).then(function (r) {
                    if (!r.ok) throw new Error('CSeriesApp.storage.set failed: ' + r.status);
                    return true;
                });
            });
        },
        remove: function (key) {
            return _awaitReady().then(function () {
                return window.fetch(_cfgUrl(key), { method: 'DELETE' }).then(function (r) {
                    if (!r.ok && r.status !== 404) throw new Error('CSeriesApp.storage.remove failed: ' + r.status);
                    return true;
                });
            });
        },
        keys: function () {
            return _awaitReady().then(function () {
                return window.fetch(_cfgUrl(null)).then(function (r) {
                    if (!r.ok) throw new Error('CSeriesApp.storage.keys failed: ' + r.status);
                    return r.json().then(function (j) { return Object.keys(j.config || {}); });
                });
            });
        },
    };

    // CSeriesApp.helperFetch — the app's server-side fetch lane. For apps with
    // runtime.kind=helper: GETs a declared host THROUGH the app's own
    // helper pod (/system-apps/<id>/helper/fetch/<host>/<path>), which
    // caches and serves stale-while-error. The token header is REQUIRED —
    // the kernel binds it to this app's id so no other app can
    // drive this helper. Waits for the init handshake like storage does
    // (an explicit awaited call, never gating first paint). The first call
    // after idle wakes the pod, so it can take a few seconds — the helper
    // response carries X-Helper-Cache: hit|miss|stale.
    function helperFetch(host, path, params) {
        return _awaitReady().then(function () {
            var p = String(path == null ? '' : path).replace(/^\/+/, '');
            var url = '/system-apps/' + encodeURIComponent(_appId)
                + '/helper/fetch/' + encodeURIComponent(host) + '/' + p;
            if (params) {
                var qs = (params instanceof URLSearchParams)
                    ? params.toString()
                    : new URLSearchParams(params).toString();
                if (qs) url += '?' + qs;
            }
            return window.fetch(url, {
                headers: { 'X-Application-Token': _token || '' },
            });
        });
    }

    window.CSeriesApp = {
        __installed: true,
        get token() { return _token; },
        get appId() { return _appId; },
        ready: _ready,          // resolves when the init handshake lands
        storage: storage,
        helperFetch: helperFetch,
    };

    // Tell the shell we're up so it sends the token + app_id. (Also covered by the
    // shell sending on iframe 'load'.)
    try { window.parent.postMessage({ type: 'cseries:app-ready' }, '*'); } catch (e) { /* not framed */ }
})();
