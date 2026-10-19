/**
 * ShellToken — the desktop shell's own scoped app token.
 *
 * The shell chrome polls three permission-gated routes to render itself:
 * /api/entity/state (the presence line), /api/inbox/unread-count and
 * /api/calendar/list (the circles badge). All three sit behind
 * `requires_permission`, which 401s a caller with no app token in EVERY
 * mode — so before this existed the shell's polls failed by construction
 * and presence.js retried the failure in a loop, forever.
 *
 * This fetches ONE token (GET /api/applications/shell-token) carrying
 * exactly read:session + read:inbox + read:calendar, and holds it in module
 * scope. Deliberately NOT a cookie: circles, claude-code and codespace are
 * still framed same-origin, and a cookie on the shell origin would ride
 * along on every fetch those apps make, handing them scopes they never
 * declared. A variable + an explicit header goes only where we put it.
 *
 * NON-BLOCKING, like app-sdk.js: nothing waits on the token. Callers ask for
 * `ShellToken.headers()` and get `{}` until it lands, so a slow or failed
 * mint costs a poll cycle, never a render. The pollers already tolerate a
 * failed fetch (presence.js swallows errors; the badge leaves its count).
 *
 * Re-mints on demand: the kernel secret rotates on container restart
 * (server/applications/tokens.py:14-19), which invalidates every token in
 * flight. `ShellToken.retry()` lets a caller that saw a 401 fetch a fresh
 * one rather than 401ing until the tab is reloaded.
 */
const ShellToken = (() => {
    let token = null;
    let inflight = null;

    async function fetchToken() {
        try {
            const r = await fetch('/api/applications/shell-token', {
                credentials: 'same-origin',
            });
            if (!r.ok) return null;
            const j = await r.json();
            return (j && typeof j.token === 'string' && j.token) ? j.token : null;
        } catch (_) {
            // Offline / pod restarting. Callers degrade; retry() picks it up.
            return null;
        }
    }

    function ensure() {
        if (token) return Promise.resolve(token);
        if (!inflight) {
            inflight = fetchToken().then((t) => {
                token = t;
                inflight = null;
                return t;
            });
        }
        return inflight;
    }

    return {
        /** Kick off the mint. Safe to call more than once; only one is in flight. */
        init() { ensure(); },

        /**
         * Headers for a gated shell call. Returns {} when no token has landed
         * yet — never blocks, never throws.
         */
        headers() {
            return token ? { 'X-Application-Token': token } : {};
        },

        /**
         * Drop the current token and mint another. Call this when a gated
         * shell route answered 401, which means the kernel secret rotated
         * under us. Resolves to the new token, or null if the mint failed.
         */
        retry() {
            token = null;
            inflight = null;
            return ensure();
        },
    };
})();

window.ShellToken = ShellToken;
ShellToken.init();
