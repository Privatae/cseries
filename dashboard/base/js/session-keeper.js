/**
 * Session keeper — keeps the router's session cookie alive under a
 * live desktop, and replaces silent session death with one honest overlay.
 *
 * The problem this ends: the router's session cookie carried
 * a FIXED 1h expiry from mint. An open desktop died exactly at the hour —
 * every fetch decayed into red "network error" text, the shell stuck on
 * "Reconnecting…", and only a full page reload (top-level nav → Authentik
 * silent SSO → fresh cookie) recovered it. Often the hour was mostly spent
 * before the page even loaded, because a reload rides an old-but-valid
 * cookie without re-minting it.
 *
 * How it works with the router's renew path (the router's /__host/* paths):
 *   - Every 5 min, and on every tab-wake, ping the renew endpoint. The ping
 *     only ASKS; real use is what extends the session
 *     (below, "only real use keeps the session").
 *   - 204 → the session lives. Nothing to show.
 *   - 401 → the session is truly dead (an hour with no real use, past the
 *     24h ceiling, or signed out). Show ONE overlay — "Session expired" — whose Continue
 *     button rides the bootstrap redirect and lands back on this page.
 *     Window layout survives that trip via desktop-restore.js.
 *   - Anything else (404 from a dev pod, an older router without
 *     the endpoint, an opaque redirect) → version skew, stay silent. Never
 *     nag on a guess.
 *
 * THE FIRST FAILED REQUEST ASKS AT ONCE. When the session ends,
 * the router answers every request with a redirect to the sign-in, which ends
 * on the sign-in site — another origin — and a page's fetch() that follows a
 * redirect there rejects with TypeError "Failed to fetch". Measured:
 * the Workbench's run cards showed that in red the moment the session ended,
 * and this overlay came four minutes later, at the next 5-minute ping. So a
 * renewal probe now also runs:
 *   - when one of this desktop's own same-origin fetches rejects (the shell
 *     polls several routes, so the first poll after the end is the signal);
 *   - when any window asks with a {type: 'cseries:session-check'} message — an
 *     app frame whose own request just failed. The message carries no
 *     authority: it only makes this page ask the router itself, and the
 *     answer is the router's, so a page that sends it for no reason gets one
 *     extra renewal and nothing else;
 *   - when a same-origin app calls SessionKeeper.check() directly.
 * The probe never follows the redirect (redirect:'manual'), so it never
 * touches the sign-in site. Concurrent askers share one probe, and probes are
 * at least RECHECK_MS apart.
 *
 * WHEN THE SESSION COMES BACK without this page reloading — another tab of
 * this Host signed in again, which re-mints the same host-only cookie — the
 * next ping that answers 204 (tab-wake, the 5-minute ping, back online) takes
 * the overlay down. Every state change is announced to the app frames as
 * {type: 'cseries:session', state: 'expired' | 'ok'} (no secret in it), and as a
 * 'cseries:session' event on this window, so a card that could not load can say
 * why and load again once the session is back.
 *
 * Scope note: this renews the desktop's own cookie, which covers every app
 * framed same-origin (the workbench, circles, the rest). An isolated app runs
 * on its own origin, apps-<key>-<handle>, holds its own host-only
 * cookie, and renews it itself: dashboard/base/js/app-frame-init.js.
 *
 * ONLY REAL USE KEEPS THE SESSION. The session ends after
 * an hour in which the person did not use the Host, and lives up to 24 hours
 * while they do (the router's use clock). Every renewal above is PLAIN: the
 * 5-minute ping, the probe after a failed request, a frame's session-check —
 * they ask whether the session lives and extend nothing, so an open app, a
 * poll or a timer can never keep it alive. The one renewal that extends it is
 * `/__host/renew?use=1`, sent at most once per USE_REPORT_MS for the whole
 * browser (every desktop tab of this Host shares the limit, through this
 * origin's storage), and only after the browser recorded the person's own
 * input on this tab:
 *   - a keydown or pointerdown on this desktop or in a same-origin app frame
 *     (isTrusted: an event a script dispatches is not input);
 *   - the window coming to the front (document.hasFocus() turning true, which
 *     includes the person switching back to this tab; a tab shown without
 *     the window's focus is not counted);
 *   - navigator.userActivation.isActive — the browser's own record of a key
 *     press or click, which it propagates from ANY frame up to this page,
 *     isolated app frames included, and which no script or message can set.
 *     That is how typing in an isolated app counts without the app being
 *     trusted to say so. A one-second sampler reads it; reading it is not
 *     use, and with no input nothing is sent.
 * The router counts `use=1` only same-origin on a desktop origin, so the
 * same request from an isolated app (same-site) is ignored.
 *
 * ONCE THE SESSION IS KNOWN ENDED, THIS PAGE STOPS ASKING. A
 * desktop kept polling about 2,960 requests an hour after its session lapsed
 * (edge log): every poll was redirected to the sign-in and failed.
 * While the state is 'expired', a same-origin fetch from this page or a
 * same-origin app frame is refused here, with the same TypeError it would have
 * got, and nothing is sent. The forced pings (5-minute, tab-wake, online) still
 * ask, which is how a session restored from another tab is noticed.
 */

const SessionKeeper = (() => {
    const RENEW_PATH = '/__host/renew';
    const USE_PATH = RENEW_PATH + '?use=1';
    const INTERVAL_MS = 5 * 60 * 1000;
    const RECHECK_MS = 2000;
    // At most one real-use report a minute: the idle window is an hour, so a
    // minute's granularity costs nothing and keeps a busy typist to 60/hour.
    const USE_REPORT_MS = 60 * 1000;
    const USE_SAMPLE_MS = 1000;
    // Shared by every desktop tab of this Host (same origin, same storage): the
    // limit is one report a minute for the browser, not for each tab.
    const USE_KEY = 'cseries:host-use-at';
    let overlayShown = false;
    let inflight = null;
    let trailing = null;
    let trailingForce = false;
    let lastAt = 0;
    let state = 'unknown';
    let lastUseAt = 0;
    let useInflight = false;
    // null until the first sample: the focus a page gets as it loads is not
    // the person bringing the window to the front.
    let hadFocus = null;

    // This page's own fetch, before the wrapper below: the probe must not
    // report its own failure back to itself.
    const rawFetch = window.fetch.bind(window);
    const MANUAL = { cache: 'no-store', credentials: 'same-origin', redirect: 'manual' };

    function ended() {
        return Promise.reject(new TypeError('Host session ended'));
    }

    /** Ask the router now. Resolves 'ok' (the session lives), 'expired' (the overlay
     * is up) or 'unknown' (offline, or version skew — silent by design).
     * Once the session is known to be gone, a failed request asks nothing
     * more: the shell's polls keep failing until Continue, and each probe
     * would be one more renewal for nothing. Only `force` (the 5-minute
     * ping, a tab-wake, coming back online) asks again, which is how a
     * session restored from another tab is noticed. */
    function check(force) {
        force = force === true;
        if (!force && state === 'expired') return Promise.resolve('expired');
        if (inflight) return inflight;
        // At most one probe per RECHECK_MS: a burst of failures (the shell
        // polls several routes) shares one. A failure that lands inside the
        // gap is answered by ONE probe at its end — never by an answer from
        // before it happened, which could be a session that has since ended.
        // A forced ask that joins the wait makes that probe forced: the state
        // can turn 'expired' meanwhile without a probe (a real-use report's
        // 401), and a tab-wake must still ask then (dropping it left the
        // prompt up over a live session).
        const wait = lastAt + RECHECK_MS - Date.now();
        if (wait > 0) {
            trailingForce = trailingForce || force;
            if (!trailing) {
                trailing = new Promise((resolve) => setTimeout(() => {
                    const f = trailingForce;
                    trailing = null;
                    trailingForce = false;
                    resolve(check(f));
                }, wait));
            }
            return trailing;
        }
        inflight = probe().then((answer) => {
            lastAt = Date.now();
            inflight = null;
            return answer;
        });
        return inflight;
    }

    async function probe() {
        let res;
        try {
            res = await rawFetch(RENEW_PATH, MANUAL);
        } catch (_) {
            return 'unknown'; // offline — the WS layer owns the connectivity story
        }
        return answer(res);
    }

    function answer(res) {
        if (res.status === 401) {
            showOverlay();
            announce('expired');
            return 'expired';
        }
        if (res.status === 204) {
            hideOverlay();
            announce('ok');
            return 'ok';
        }
        return 'unknown'; // version skew, silent by design
    }

    async function ping() {
        await check(true);
    }

    function announce(next) {
        if (next === state) return;
        state = next;
        try {
            window.dispatchEvent(new CustomEvent('cseries:session', { detail: { state: next } }));
        } catch (_) { /* no CustomEvent */ }
        for (let i = 0; i < window.frames.length; i++) {
            try { window.frames[i].postMessage({ type: 'cseries:session', state: next }, '*'); } catch (_) { /* gone */ }
        }
    }

    function sameOrigin(input, base) {
        try {
            const raw = (input && typeof input === 'object' && 'url' in input) ? input.url : String(input);
            return new URL(raw, base || location.href).origin === location.origin;
        } catch (_) {
            return false;
        }
    }

    // A same-origin request that REJECTS (TypeError) is what a session that
    // ended looks like from here; an abort is not. The caller gets exactly
    // the promise it would have got. While the session is known ended the
    // request is not sent at all (header: "this page stops asking").
    function gate(raw, base) {
        return function (input, init) {
            if (state === 'expired' && sameOrigin(input, base)) return ended();
            const p = raw(input, init);
            // A rejection is the redirect to the sign-in site; a 401 is the
            // router refusing a background request a new session (it only
            // starts one on a page load). Both mean: ask now.
            p.then((res) => {
                if (res && res.status === 401 && sameOrigin(input, base)) check();
            }, (e) => {
                if (e && e.name === 'TypeError' && sameOrigin(input, base)) check();
            });
            return p;
        };
    }
    window.fetch = gate(rawFetch, location.href);

    // ── Real use (header: "only real use keeps the session") ──
    // When this browser last reported use, from any desktop tab of this Host.
    // A stored time in the future (a clock set back) is ignored.
    function lastUse() {
        let t = lastUseAt;
        try {
            const s = Number(localStorage.getItem(USE_KEY));
            if (s > t && s <= Date.now()) t = s;
        } catch (_) { /* no storage: this tab's own time */ }
        return t;
    }

    function sawRealUse() {
        if (state === 'expired' || useInflight) return;
        const now = Date.now();
        if (now - lastUse() < USE_REPORT_MS) return;
        lastUseAt = now;
        try { localStorage.setItem(USE_KEY, String(now)); } catch (_) { /* no storage */ }
        useInflight = true;
        rawFetch(USE_PATH, MANUAL)
            .then(answer, () => 'unknown')
            .then(() => { useInflight = false; });
    }

    function onInput(ev) {
        if (ev && ev.isTrusted) sawRealUse();
    }

    function listen(w) {
        w.addEventListener('keydown', onInput, true);
        w.addEventListener('pointerdown', onInput, true);
    }

    // Same-origin app frames (the Workbench, Circles, …) get the same two
    // things this page has: their input counts, and their requests stop while
    // the session is known ended. A frame on another origin throws here and is
    // left alone; its input reaches this page through userActivation.
    // Hooked per DOCUMENT: a frame's contentWindow is the same object after it
    // reloads or navigates, but the page in it is new, with its own fetch and
    // none of the listeners (measured in Chromium).
    const hooked = new WeakSet();
    function hookFrames() {
        const frames = document.getElementsByTagName('iframe');
        for (let i = 0; i < frames.length; i++) {
            try {
                const w = frames[i].contentWindow;
                if (!w || w.location.origin !== location.origin) continue;
                const doc = w.document;
                if (!doc || hooked.has(doc)) continue;
                hooked.add(doc);
                listen(w);
                if (w.fetch.__cseriesGate) continue;   // the window outlived its first page
                const wrapped = gate(w.fetch.bind(w), w.location.href);
                wrapped.__cseriesGate = true;
                // the App SDK's shim tag, so nothing that checks it wraps twice
                if (w.fetch.__cseriesShim) wrapped.__cseriesShim = true;
                w.fetch = wrapped;
            } catch (_) { /* another origin, or gone */ }
        }
    }

    function sample() {
        // Frames are hooked in a background tab too: that is where a desktop
        // whose session lapsed sits, and a frame that reloads there must not
        // poll again.
        hookFrames();
        if (document.visibilityState !== 'visible') {
            // A hidden tab is not in front: coming back to it is the window
            // coming to the front.
            hadFocus = false;
            return;
        }
        const has = document.hasFocus();
        const ua = navigator.userActivation;
        if ((has && hadFocus === false) || (ua && ua.isActive)) sawRealUse();
        hadFocus = has;
    }
    listen(window);

    window.addEventListener('message', (ev) => {
        const d = ev.data;
        if (d && d.type === 'cseries:session-check') check();
    });

    function bootstrapUrl() {
        const rd = location.pathname + location.search;
        return '/__host/session?rd=' + encodeURIComponent(rd || '/');
    }

    function hideOverlay() {
        if (!overlayShown) return;
        overlayShown = false;
        const el = document.getElementById('session-expired-overlay');
        if (el) el.remove();
    }

    function showOverlay() {
        if (overlayShown) return;
        overlayShown = true;

        const overlay = document.createElement('div');
        overlay.id = 'session-expired-overlay';
        overlay.setAttribute('role', 'alertdialog');
        overlay.setAttribute('aria-label', 'Session expired');
        overlay.style.cssText =
            'position:fixed;inset:0;z-index:100000;display:flex;align-items:center;' +
            'justify-content:center;background:rgba(4,6,10,0.72);backdrop-filter:blur(4px);';
        overlay.innerHTML = `
            <div style="max-width:360px;padding:28px 32px;border-radius:12px;
                        background:var(--cseries-night);border:1px solid var(--cseries-mist);text-align:center;
                        box-shadow:0 24px 64px rgba(0,0,0,0.5);">
                <div style="font-size:16px;font-weight:600;color:#E5E7EB;margin-bottom:8px;">
                    Your session expired
                </div>
                <div style="font-size:13px;line-height:1.5;color:#9CA3AF;margin-bottom:20px;">
                    Continue to sign back in &mdash;
                    your windows will be right where you left them.
                </div>
                <button id="session-expired-continue" type="button"
                    style="padding:9px 22px;border-radius:8px;border:none;cursor:pointer;
                           background:var(--cseries-paper);color:var(--cseries-ink);font-size:13px;font-weight:600;">
                    Continue
                </button>
            </div>`;
        document.body.appendChild(overlay);
        const btn = overlay.querySelector('#session-expired-continue');
        btn.addEventListener('click', () => {
            btn.textContent = 'Signing in…';
            btn.disabled = true;
            location.href = bootstrapUrl();
        });
        btn.focus();
    }

    document.addEventListener('DOMContentLoaded', () => {
        ping();
        setInterval(ping, INTERVAL_MS);
        setInterval(sample, USE_SAMPLE_MS);
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') ping();
        });
        window.addEventListener('online', ping);
    });

    return { ping, check: () => check(false) };
})();

window.SessionKeeper = SessionKeeper;
