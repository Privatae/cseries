/* ================================================================
 * Designs — a shelf of desktops to look at, try, and share.
 *
 * A set of UIs the platform makes, plus the user's own, each previewed
 * live so it can be clicked and applied.
 *
 *   Ours  — the house shelf: designs the platform's own hosts published,
 *           for everyone.
 *   Yours — this account's published designs, and every desktop made on
 *           this Host (published or not).
 *
 * Every card is the REAL document, rendered live and lazily in a sandboxed
 * frame (allow-scripts only, under the shared sandbox CSP) with a stand-in
 * desktop bridge (/js/desktop-preview.js) so it draws as it would here.
 * Trying a design wears it at once; "Go back" restores the previous desktop
 * and discards the trial copy (server/routes/designs.py /restore).
 * ================================================================ */

(function () {
    'use strict';

    // Every host-authored document is mounted under the shared sandbox CSP.
    // FAIL CLOSED: without the policy, the frame would render with open
    // egress and look identical — so it does not render at all.
    function sealed(html) {
        if (!window.CSeriesSandbox || !window.DesktopPreview) {
            return '<!DOCTYPE html><html><body style="margin:0;display:flex;'
                + 'align-items:center;justify-content:center;height:100vh;'
                + 'font:13px system-ui;color:#8b968f;background:transparent;'
                + 'text-align:center;padding:24px">'
                + 'This can\'t be shown right now — the sandbox policy did not '
                + 'load, and host-authored HTML is never rendered without it. '
                + 'Reload the app.</body></html>';
        }
        return window.CSeriesSandbox.withSandboxCsp(
            window.DesktopPreview.instrument(html, { apps: state.apps, host: state.host }));
    }

    // Designs are drawn for a desktop, so previews render at a desktop size
    // and are scaled down to the card.
    const STAGE_W = 1440, STAGE_H = 900;
    const TRY_KEY = 'designs.trying';

    const state = {
        shelf: 'house',
        designs: [],       // shelf rows from the library
        local: [],         // this host's own desktops
        wearing: null,     // {wallpaper, artifact_id, entry}
        host: '',
        apps: [],          // installed apps, for previews
        libraryError: null,
        publishesTo: 'account',  // where a publish from this Host lands
        trying: null,      // {name, previous, discard_artifact_id}
        viewing: null,     // the card open in the full-size viewer
        docs: new Map(),   // card key -> html (null = unavailable)
    };

    async function apiFetch(url, opts) {
        const waits = [0, 300, 700, 1200];
        let r;
        for (const w of waits) {
            if (w) await new Promise(res => setTimeout(res, w));
            r = await fetch(url, opts || {});
            if (r.status !== 401 && r.status !== 403) break;
        }
        return r;
    }

    async function call(url, opts) {
        const r = await apiFetch(url, opts);
        let body = null;
        try { body = await r.json(); } catch (_e) { body = null; }
        if (r.status === 202 && body && body.poll) return settle(body);
        if (!r.ok) {
            const d = body && body.detail;
            throw new Error(typeof d === 'string' ? d : `${url} → ${r.status}`);
        }
        return body || {};
    }

    // Try and Publish load the design in a real browser first, and the
    // router in front of every Host gives a request 30 s — so a door that is
    // still checking answers 202 with a job (server/applications/
    // desktop_render.py run_door). Poll it until the answer the request itself
    // would have given: the result, or the same refusal.
    async function settle(pending) {
        if (pending.message) setStatus(pending.message);
        const deadline = Date.now() + 6 * 60 * 1000;
        while (Date.now() < deadline) {
            await new Promise(res => setTimeout(res, 1500));
            const r = await apiFetch(pending.poll);
            let j = null;
            try { j = await r.json(); } catch (_e) { j = null; }
            if (!r.ok) throw new Error((j && j.detail) || `${pending.poll} → ${r.status}`);
            if (j.status === 'done') return j.result || {};
            if (j.status === 'failed') {
                throw new Error(typeof j.detail === 'string' ? j.detail : 'refused');
            }
        }
        throw new Error('the design check took too long to answer — try again');
    }

    function post(url, body) {
        return call(url, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
    }

    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    const $ = id => document.getElementById(id);
    function setStatus(msg) { $('footer-status').textContent = msg; }

    // ── Data ─────────────────────────────────────────────────────
    async function load() {
        const d = await call('/api/designs');
        state.designs = d.designs || [];
        state.local = d.local || [];
        state.wearing = d.wearing || null;
        state.host = d.host || '';
        state.libraryError = d.library_error || null;
        state.publishesTo = d.publishes_to === 'everyone' ? 'everyone' : 'account';
    }

    async function loadApps() {
        try {
            const d = await call('/api/applications');
            state.apps = (d.applications || []).map(a => ({ id: a.id, name: a.name }));
        } catch (_e) {
            state.apps = [];
        }
    }

    function cardsFor(shelf) {
        if (shelf === 'house') {
            return state.designs.filter(d => d.shelf === 'house').map(d => ({
                key: 'd' + d.id, kind: 'shelf', id: d.id, name: d.name,
                by: d.author_name, tokens: d.tokens, mine: !!d.mine,
            }));
        }
        const shared = state.designs.filter(d => d.shelf !== 'house').map(d => ({
            key: 'd' + d.id, kind: 'shelf', id: d.id, name: d.name,
            by: d.author_name, tokens: d.tokens, mine: !!d.mine,
        }));
        const wornId = state.wearing && state.wearing.artifact_id;
        const local = state.local.map(a => ({
            key: 'a' + a.artifact_id, kind: 'local', id: a.artifact_id,
            name: String(a.name || '').replace(/^Desktop - /, ''),
            by: 'made on this Host', tokens: a.tokens, wearing: a.artifact_id === wornId,
        }));
        return shared.concat(local);
    }

    // ── Cards ────────────────────────────────────────────────────
    function palette(tokens) {
        if (!tokens) return '';
        const keys = ['--cseries-ink', '--cseries-silk', '--cseries-ember', '--cseries-dusk', '--cseries-paper'];
        return keys.filter(k => tokens[k]).map(k =>
            `<span class="chip" style="background:${esc(tokens[k]).replace(/[;"]/g, '')}" title="${esc(k)}"></span>`
        ).join('');
    }

    function actions(c) {
        const out = [];
        if (c.kind === 'shelf') {
            out.push(`<button class="cseries-btn" data-act="try">Try it</button>`);
            if (c.mine) out.push(`<button class="cseries-btn ghost" data-act="remove">Remove</button>`);
        } else {
            out.push(c.wearing
                ? `<span class="label-sys worn">wearing</span>`
                : `<button class="cseries-btn" data-act="wear">Wear it</button>`);
            out.push(`<button class="cseries-btn ghost" data-act="publish">Publish</button>`);
        }
        return out.join('');
    }

    function render() {
        const cards = cardsFor(state.shelf);
        const grid = $('grid');
        grid.innerHTML = cards.map(c => `
            <div class="card" data-key="${esc(c.key)}">
                <div class="thumb" role="button" tabindex="0" data-act="view" title="Look at ${esc(c.name)} full size">
                    <iframe sandbox="allow-scripts" tabindex="-1" title="${esc(c.name)} preview"
                            style="width:${STAGE_W}px;height:${STAGE_H}px"></iframe>
                </div>
                <div class="card-meta">
                    <div class="card-name">${esc(c.name)}</div>
                    <div class="card-by label-sys">${esc(c.by ? 'by ' + c.by : '')}</div>
                    <div class="chips">${palette(c.tokens)}</div>
                </div>
                <div class="card-actions">${actions(c)}</div>
                <div class="card-form" hidden></div>
            </div>`).join('');

        const note = $('shelf-note');
        if (state.shelf === 'house') {
            note.textContent = state.libraryError
                ? `The shelf can't be reached right now (${state.libraryError}).`
                : 'Desktops we made. Try one — it goes on your screen at once, and "Go back" undoes it.';
        } else {
            note.textContent = 'Designs you shared, and every desktop your Host made. '
                + (state.publishesTo === 'everyone'
                    ? 'This is one of our Hosts: publishing puts a design on Ours, for everyone.'
                    : 'Publish one to see it on all your Hosts.');
        }
        const empty = $('empty');
        empty.hidden = cards.length > 0;
        if (!cards.length) {
            $('empty-title').textContent = state.shelf === 'house' ? 'Nothing on the shelf yet' : 'No designs yet';
            $('empty-text').textContent = state.shelf === 'house'
                ? 'Designs we publish appear here for everyone.'
                : 'Ask your Host for a new desktop in Circles — it appears here as soon as it exists.';
        }
        observe();
        sizeThumbs();
    }

    // ── Live previews, lazily ────────────────────────────────────
    let observer = null;
    function observe() {
        if (observer) observer.disconnect();
        observer = new IntersectionObserver(entries => {
            for (const en of entries) {
                const card = en.target;
                const frame = card.querySelector('.thumb iframe');
                if (!frame) continue;
                if (en.isIntersecting) paint(card.dataset.key, frame);
                else frame.removeAttribute('srcdoc');
            }
        }, { root: null, rootMargin: '300px 0px' });
        document.querySelectorAll('.card').forEach(el => observer.observe(el));
    }

    function cardByKey(key) {
        return cardsFor(state.shelf).find(c => c.key === key)
            || cardsFor(state.shelf === 'house' ? 'yours' : 'house').find(c => c.key === key);
    }

    async function documentFor(c) {
        if (state.docs.has(c.key)) return state.docs.get(c.key);
        const url = c.kind === 'shelf'
            ? `/api/designs/${c.id}/document`
            : `/api/designs/local/${c.id}/document`;
        let html = null;
        try { html = (await call(url)).html || null; } catch (_e) { html = null; }
        state.docs.set(c.key, html);
        return html;
    }

    async function paint(key, frame) {
        const c = cardByKey(key);
        if (!c) return;
        const html = await documentFor(c);
        if (!frame.isConnected) return;
        frame.srcdoc = html
            ? sealed(html)
            : '<!DOCTYPE html><body style="margin:0;display:grid;place-items:center;height:100vh;'
              + 'font:28px system-ui;color:#8b968f;background:#0e1416">preview unavailable</body>';
    }

    function sizeThumbs() {
        document.querySelectorAll('.thumb').forEach(t => {
            const s = t.clientWidth / STAGE_W;
            t.style.height = Math.round(STAGE_H * s) + 'px';
            const f = t.querySelector('iframe');
            if (f) f.style.transform = `scale(${s})`;
        });
        const stage = $('viewer-stage');
        if (!$('viewer').hidden && stage) {
            const s = Math.min(stage.clientWidth / STAGE_W, stage.clientHeight / STAGE_H);
            const f = $('viewer-frame');
            f.style.width = STAGE_W + 'px';
            f.style.height = STAGE_H + 'px';
            f.style.transform = `scale(${s})`;
        }
    }
    window.addEventListener('resize', sizeThumbs);

    // ── Trying, keeping, going back ──────────────────────────────
    function saveTrying() {
        try {
            if (state.trying) sessionStorage.setItem(TRY_KEY, JSON.stringify(state.trying));
            else sessionStorage.removeItem(TRY_KEY);
        } catch (_e) { /* storage may be unavailable in the frame */ }
    }

    function showTrying() {
        const bar = $('trying');
        bar.hidden = !state.trying;
        if (state.trying) {
            $('trying-text').textContent = `You're trying “${state.trying.name}”. Keep it, or go back to what you had.`;
        }
    }

    async function tryCard(c) {
        // The server loads the design in a real browser before it is worn
        // (server/applications/desktop_render.py); a sleeping browser can take
        // a minute to wake, so the wait says what it is.
        setStatus(`checking “${c.name}” in a browser, then putting it on your desktop…`);
        // A second try while one is pending: the server drops the first trial
        // copy, and "Go back" still returns to the ORIGINAL desktop.
        const body = { discard_artifact_id: state.trying ? state.trying.discard_artifact_id : null };
        const res = c.kind === 'shelf'
            ? await post(`/api/designs/${c.id}/install`, body)
            : await post(`/api/designs/local/${c.id}/wear`, body);
        state.trying = {
            name: c.name,
            previous: state.trying ? state.trying.previous : res.previous,
            discard_artifact_id: c.kind === 'shelf' ? res.artifact_id : null,
        };
        saveTrying();
        showTrying();
        await refresh();
        setStatus(`wearing “${c.name}”`);
    }

    async function goBack() {
        if (!state.trying) return;
        setStatus('putting your previous desktop back…');
        await post('/api/designs/restore', {
            previous: state.trying.previous,
            discard_artifact_id: state.trying.discard_artifact_id,
        });
        state.trying = null;
        saveTrying();
        showTrying();
        await refresh();
        setStatus('back to your previous desktop');
    }

    async function keep() {
        const name = state.trying && state.trying.name;
        // The copy stops being a trial on the server too, so a stale window's
        // "Go back" can never discard it later.
        if (state.trying && state.trying.discard_artifact_id) {
            await post('/api/designs/keep', { artifact_id: state.trying.discard_artifact_id });
        }
        state.trying = null;
        saveTrying();
        showTrying();
        setStatus(name ? `kept “${name}”` : 'kept');
    }

    // ── Publish / remove (inline forms — no browser dialogs in an app frame) ──
    function openForm(card, html) {
        const f = card.querySelector('.card-form');
        f.innerHTML = html;
        f.hidden = false;
        const input = f.querySelector('input');
        if (input) { input.focus(); input.select(); }
    }

    function closeForm(card) {
        const f = card.querySelector('.card-form');
        f.hidden = true;
        f.innerHTML = '';
    }

    async function publish(c, name) {
        setStatus(`checking “${name}” in a browser, then publishing…`);
        const res = await post(`/api/designs/local/${c.id}/publish`, { name });
        const where = res.shelf === 'house' ? 'Ours — everyone can try it now' : 'your shelf, on all your Hosts';
        setStatus(`published “${name}” to ${where}`);
        await refresh();
    }

    async function remove(c) {
        setStatus(`removing “${c.name}”…`);
        await call(`/api/designs/${c.id}`, { method: 'DELETE' });
        state.docs.delete(c.key);
        setStatus(`removed “${c.name}” from the shelf — anyone wearing it keeps their copy`);
        await refresh();
    }

    // ── The full-size viewer ─────────────────────────────────────
    async function openViewer(c) {
        state.viewing = c;
        $('viewer-title').textContent = c.name + (c.by ? ` — by ${c.by}` : '');
        $('viewer-try').textContent = c.kind === 'local' ? 'Wear it' : 'Try it';
        $('viewer-try').hidden = !!c.wearing;
        $('viewer').hidden = false;
        sizeThumbs();
        const html = await documentFor(c);
        $('viewer-frame').srcdoc = html ? sealed(html)
            : '<!DOCTYPE html><body style="font:14px system-ui;color:#8b968f">preview unavailable</body>';
    }

    function closeViewer() {
        state.viewing = null;
        $('viewer').hidden = true;
        $('viewer-frame').removeAttribute('srcdoc');
    }

    // ── Wiring ───────────────────────────────────────────────────
    async function guarded(fn) {
        try { await fn(); } catch (e) { setStatus(String(e && e.message || e)); }
    }

    $('grid').addEventListener('click', e => {
        const btn = e.target.closest('[data-act]');
        if (!btn) return;
        const card = btn.closest('.card');
        const c = card && cardByKey(card.dataset.key);
        if (!c) return;
        const act = btn.dataset.act;
        if (act === 'view') return guarded(() => openViewer(c));
        if (act === 'try' || act === 'wear') return guarded(() => tryCard(c));
        if (act === 'publish') {
            const who = state.publishesTo === 'everyone'
                ? 'Everyone on Privatae will see this on Ours.'
                : 'All your Hosts will see this.';
            return openForm(card, `
                <span class="label-sys">${esc(who)}</span>
                <input class="cseries-input" data-field="name" maxlength="80" value="${esc(c.name)}">
                <button class="cseries-btn" data-act="publish-go">Publish</button>
                <button class="cseries-btn ghost" data-act="cancel">Cancel</button>`);
        }
        if (act === 'publish-go') {
            const name = (card.querySelector('[data-field="name"]').value || '').trim();
            return guarded(async () => { await publish(c, name); closeForm(card); });
        }
        if (act === 'remove') {
            return openForm(card, `
                <span class="label-sys">Take “${esc(c.name)}” off the shelf?</span>
                <button class="cseries-btn" data-act="remove-go">Remove</button>
                <button class="cseries-btn ghost" data-act="cancel">Cancel</button>`);
        }
        if (act === 'remove-go') return guarded(() => remove(c));
        if (act === 'cancel') return closeForm(card);
    });

    $('viewer-close').addEventListener('click', closeViewer);
    $('viewer-try').addEventListener('click', () => {
        const c = state.viewing;
        if (c) guarded(async () => { closeViewer(); await tryCard(c); });
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && !$('viewer').hidden) return closeViewer();
        // The preview block is a button for the keyboard too.
        if ((e.key === 'Enter' || e.key === ' ') && e.target.classList && e.target.classList.contains('thumb')) {
            e.preventDefault();
            e.target.click();
        }
    });
    $('btn-keep').addEventListener('click', () => guarded(keep));
    $('btn-back').addEventListener('click', () => guarded(goBack));

    document.querySelectorAll('[data-shelf]').forEach(b => b.addEventListener('click', () => {
        state.shelf = b.dataset.shelf;
        document.querySelectorAll('[data-shelf]').forEach(x => x.classList.toggle('active', x === b));
        render();
    }));

    async function refresh() {
        await load();
        render();
    }

    async function init() {
        try {
            state.trying = JSON.parse(sessionStorage.getItem(TRY_KEY) || 'null');
        } catch (_e) { state.trying = null; }
        showTrying();
        try {
            await Promise.all([load(), loadApps()]);
            // Nothing on the house shelf yet but designs of your own: open on Yours.
            if (!state.designs.some(d => d.shelf === 'house') && (state.local.length || state.designs.length)) {
                state.shelf = 'yours';
                document.querySelectorAll('[data-shelf]').forEach(x =>
                    x.classList.toggle('active', x.dataset.shelf === 'yours'));
            }
            render();
            setStatus(`${state.designs.length} on the shelf · ${state.local.length} made here`);
        } catch (e) {
            setStatus(String(e && e.message || e));
        }
    }

    init();
})();
