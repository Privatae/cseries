/* ================================================================
 * Getting Started — the slide engine.
 *
 * Slides come from steps.js (GS_SLIDES). Progress persists to the
 * shell-trusted appearance lane exactly as before:
 *
 *   read:    GET /api/appearance        -> desktop_layout.walkthrough
 *   write:   PUT /api/appearance/layout  { walkthrough: {done, step, steps} }
 *
 * The one mechanism: the slide's button opens its app through the
 * shell's cseries:open-app relay; the shell fires 'window:closed' (with
 * the app id) on its document when any window closes, and this frame is
 * same-origin with the shell, so we listen there. A close of OUR app —
 * or the user simply clicking back into this window — runs the slide's
 * check; a pass advances. Nothing is ever a dead end: the check's
 * "not yet" line stays on the slide with the same button, and Skip is
 * always there.
 * ================================================================ */

(function () {
    'use strict';

    const APP_ID = 'getting-started';
    const SLIDES = window.GS_SLIDES || [];
    const STATUS_VALUES = new Set(['passed', 'skipped', 'failed', 'seen']);

    const state = {
        idx: 0,
        steps: {},        // persisted: slideId -> passed|skipped|failed|seen
        done: false,
        waiting: false,   // the slide's app is open (or was opened) — a return re-checks
        checkRun: 0,      // cancels a stale check
    };

    function $(id) { return document.getElementById(id); }

    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    // Token-race backoff, same discipline as the other vanilla apps.
    async function apiFetch(url, opts) {
        const waits = [0, 300, 700, 1200];
        let r;
        for (const w of waits) {
            if (w) await new Promise((res) => setTimeout(res, w));
            r = await fetch(url, opts || {});
            if (r.status !== 401 && r.status !== 403) break;
        }
        return r;
    }

    function slide() { return SLIDES[state.idx] || null; }

    // ── Persistence — the walkthrough key ONLY (server merges) ───
    async function persist() {
        const s = slide();
        const body = {
            walkthrough: {
                done: state.done === true,
                step: (s && s.id) || 'welcome',
                steps: state.steps,
            },
        };
        try {
            const r = await apiFetch('/api/appearance/layout', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!r.ok) throw new Error(`layout → ${r.status}`);
        } catch (e) {
            // Saving must never block the tour — the state lives in this
            // window either way; the next transition retries the write.
        }
    }

    // ── Rendering ────────────────────────────────────────────────
    function setStatus(text, tone) {
        const el = $('status');
        el.textContent = text || '';
        el.hidden = !text;
        el.className = 'status' + (tone ? ` ${tone}` : '');
    }

    function render() {
        const s = slide();
        if (!s) return;
        state.checkRun += 1;
        state.waiting = false;

        $('kicker').textContent = `${state.idx + 1} of ${SLIDES.length}`;
        $('title').textContent = s.title;
        $('lines').innerHTML = s.lines.map((l) => `<p>${esc(l)}</p>`).join('');

        const btn = $('btn-primary');
        btn.textContent = s.button;
        btn.disabled = false;
        btn.onclick = s.appId ? () => openApp(s) : (state.idx === SLIDES.length - 1 ? finish : next);

        $('btn-skip').hidden = !s.appId;
        $('btn-again').hidden = true;
        setStatus('');

        // "Picks up where things stand": a step already done on the host
        // (a key added last week, say) passes quietly and moves on.
        if (s.check) quietCheck(s);
    }

    // ── Checks ───────────────────────────────────────────────────
    async function execCheck(check) {
        try {
            const r = await apiFetch(check.endpoint);
            if (!r.ok) return 'unknown';
            return check.evaluate(await r.json()) ? 'ready' : 'not_ready';
        } catch (e) {
            return 'unknown';
        }
    }

    async function quietCheck(s) {
        const run = state.checkRun;
        const res = await execCheck(s.check);
        if (run !== state.checkRun) return;
        if (res === 'ready') {
            setStatus('Already done on this host.', 'ok');
            await pass(s, run);
        }
    }

    // Starting a check cancels any other in flight (the quiet one on slide
    // entry, an earlier return) — two overlapping passes would advance
    // twice and skip a slide.
    async function checkAfterReturn(s) {
        const run = ++state.checkRun;
        setStatus('Checking…');
        $('btn-primary').disabled = true;
        $('btn-again').disabled = true;
        const res = await execCheck(s.check);
        if (run !== state.checkRun) return;
        $('btn-primary').disabled = false;
        $('btn-again').disabled = false;
        if (res === 'ready') {
            setStatus('Done.', 'ok');
            await pass(s, run);
            return;
        }
        if (res === 'not_ready') {
            state.steps[s.id] = 'failed';
            await persist();
            setStatus(s.check.notYet, 'warn');
            $('btn-primary').textContent = `Open ${s.appName} again`;
        } else {
            setStatus('Couldn\'t check that just now.', 'warn');
        }
        $('btn-again').hidden = false;
    }

    async function pass(s, run) {
        state.waiting = false;
        state.steps[s.id] = 'passed';
        await persist();
        await new Promise((res) => setTimeout(res, 600));
        if (run !== state.checkRun || slide() !== s) return;
        next();
    }

    // ── The app hop ──────────────────────────────────────────────
    function openApp(s) {
        state.waiting = true;
        setStatus(`${s.appName} is open — close it when you're done and this will move on.`);
        try {
            (window.parent || window).postMessage(
                { type: 'cseries:open-app', app_id: s.appId }, window.location.origin);
        } catch (e) { /* the relay lives in the shell; nowhere else to go */ }
    }

    // `waiting` is consumed only by a pass or by the matching app's close.
    // A focus-sourced check that comes back "not yet" leaves it armed, so
    // the close that follows (user re-reads the slide, then finishes in the
    // app and closes it) still advances as the slide promises.
    function onReturn(closedAppId) {
        const s = slide();
        if (!state.waiting || !s || !s.check) return;
        if (closedAppId) {
            if (closedAppId !== s.appId) return;
            state.waiting = false;
        }
        checkAfterReturn(s);
    }

    function listenForReturn() {
        // The shell's window manager fires this on its own document; we are
        // same-origin, so the parent document is reachable. If it isn't
        // (opened standalone), the focus fallback below still works.
        try {
            window.parent.document.addEventListener('window:closed', (e) => {
                onReturn(e.detail && e.detail.appId);
            });
        } catch (e) { /* not framed by the shell */ }
        // The user clicked back into this window without closing the app.
        // Window focus fires once per return, not once per click, so a
        // click on a button here is never swallowed by a check.
        window.addEventListener('focus', () => onReturn(null));
    }

    // ── Navigation ───────────────────────────────────────────────
    async function goTo(idx) {
        state.idx = Math.max(0, Math.min(SLIDES.length - 1, idx));
        const s = slide();
        if (s && !s.check && !state.steps[s.id]) state.steps[s.id] = 'seen';
        await persist();
        render();
    }

    function next() { goTo(state.idx + 1); }

    async function skip() {
        const s = slide();
        if (s && state.steps[s.id] !== 'passed') state.steps[s.id] = 'skipped';
        await goTo(state.idx + 1);
    }

    function closeSelf() {
        try {
            const wm = window.parent && window.parent.WindowManager;
            if (wm && typeof wm.getAll === 'function' && typeof wm.close === 'function') {
                const w = (wm.getAll() || []).find((x) => x && x.appId === APP_ID);
                if (w) { wm.close(w.id); return true; }
            }
        } catch (e) { /* fall through */ }
        return false;
    }

    // Marks the tour done (the shell stops auto-opening it), then closes.
    async function finish() {
        state.done = true;
        await persist();
        if (!closeSelf()) {
            $('btn-primary').disabled = true;
            setStatus('Done — close this window whenever you like.', 'ok');
        }
    }

    // ── Boot ─────────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', async () => {
        $('btn-skip').addEventListener('click', skip);
        $('btn-again').addEventListener('click', () => {
            const s = slide();
            if (s && s.check) checkAfterReturn(s);
        });
        $('btn-leave').addEventListener('click', finish);
        listenForReturn();

        let saved = null;
        try {
            const r = await apiFetch('/api/appearance');
            if (r.ok) {
                const data = await r.json();
                saved = data && data.desktop_layout && data.desktop_layout.walkthrough;
            }
        } catch (e) { /* fresh start */ }
        if (saved && typeof saved === 'object') {
            // Keep `done`: a finished tour reopened from the dock must not
            // write done:false on its first transition and re-arm the boot
            // autolaunch (walkthrough-autolaunch.js opens on done !== true).
            state.done = saved.done === true;
            if (saved.steps && typeof saved.steps === 'object') {
                for (const [k, v] of Object.entries(saved.steps)) {
                    if (STATUS_VALUES.has(v)) state.steps[k] = v;
                }
            }
            const i = SLIDES.findIndex((s) => s.id === saved.step);
            // A finished tour reopened from the dock starts over from the
            // top rather than landing on "You're set up".
            if (i >= 0 && saved.done !== true) state.idx = i;
        }
        // A deep link WINS over the saved position: the caller asked for a
        // specific step (the Orchestrator's "Run it free on your machine"
        // sends view=network), and dropping them wherever they left off
        // instead would silently ignore the request. Unknown id = ignored.
        const deep = deepLinkStep();
        if (deep >= 0) state.idx = deep;
        render();
    });

    // The shell's deep-link lane (app-runner.js): '#view=<id>' on a fresh
    // iframe, and a 'cseries:app-view' message into an already-open window.
    function deepLinkStep() {
        const m = /(?:^|[#&])view=([^&]+)/.exec(window.location.hash || '');
        if (!m) return -1;
        try {
            return SLIDES.findIndex((s) => s.id === decodeURIComponent(m[1]));
        } catch (e) {
            return -1;
        }
    }

    window.addEventListener('message', (e) => {
        const d = e.data;
        if (!d || d.type !== 'cseries:app-view' || !d.view) return;
        const i = SLIDES.findIndex((s) => s.id === String(d.view));
        if (i >= 0) goTo(i);
    });

    window.App = { state, goTo };
})();
