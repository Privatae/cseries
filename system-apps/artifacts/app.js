/* ================================================================
 * Artifacts — the artifact window.
 * Gallery + sandboxed live viewer (updated_at poll), and the
 * tune-mode bridge (select / edit / act — the ONLY messages the frame
 * may speak, everything else dropped) + variant tabs feeding decisions.
 *
 * SECURITY: the render lands in a sandbox="allow-scripts" srcdoc iframe —
 * opaque origin, no session, no token, no API reach. The bridge is the one
 * door: schema-validated, size/rate-capped, act ids must be DECLARED in the
 * static markup (collected by the viewer BEFORE any frame script runs),
 * edit requires a before-match, select text is re-derived from the viewer's
 * own copy. All enforcement invisible; every effect user-attributable
 * (pill rides the user's next message; act sends AS the user via Circles).
 * ================================================================ */

(function () {
    'use strict';

    const POLL_MS = 3000;
    const MSG_MAX_BYTES = 16384;
    const RATE_PER_SEC = 20;
    const RATE_PER_MIN = 400;

    const state = {
        view: 'gallery',
        artifacts: [],
        filter: 'all',          // all | other | desktop
        search: '',
        thumbs: new Map(),      // artifact_id -> {updated_at, html|null}
        current: null,          // {artifact_id, name, updated_at}
        entry: 'index.html',
        entries: [],            // variant files (root *.html)
        pages: null,            // several pages, none at the root (the render's 300)
        tune: false,
        histView: null,         // revision id being viewed read-only (null = live)
        history: [],            // change-history entries, newest first
        declared: new Set(),    // data-cseries-action ids from STATIC markup
        ownDoc: null,           // viewer's DOMParser copy for verification
        dropped: 0,
        stampsSec: [],
        stampsMin: [],
        pollTimer: null,
    };

    // ── Fetch helper (token-race backoff) ────────────────────
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

    // A desktop door may answer 202 while it loads the design in a real
    // browser (server/applications/desktop_render.py run_door — the router in
    // front of every Host gives a request 30 s): poll its job until the answer
    // the request itself would have given arrives. {ok, status, body}.
    async function settleDoor(r, body) {
        if (r.status !== 202 || !body || !body.poll) return { ok: r.ok, status: r.status, body };
        const vs = document.getElementById('viewer-status');
        if (body.message) vs.textContent = body.message;
        const deadline = Date.now() + 6 * 60 * 1000;
        while (Date.now() < deadline) {
            await new Promise(res => setTimeout(res, 1500));
            const jr = await apiFetch(body.poll);
            const j = await jr.json().catch(() => ({}));
            if (!jr.ok) return { ok: false, status: jr.status, body: { detail: j.detail || `HTTP ${jr.status}` } };
            if (j.status === 'done') return { ok: true, status: 200, body: j.result || {} };
            if (j.status === 'failed') return { ok: false, status: j.http_status || 500, body: { detail: j.detail } };
        }
        return { ok: false, status: 504, body: { detail: 'the desktop check took too long to answer' } };
    }

    // A write to the artifact that IS the desktop moves the screen on a
    // browser render-check pass, or on "not checked" with a warning — never
    // on a failure (server/applications/desktop_pin.py). When it fails, the
    // desktop stays on the last good version — say so here, where the person
    // made the change.
    async function noteWornDesktop(body) {
        let desk = body && body.desktop;
        if (!desk) return;
        if (desk.pending && desk.poll) {
            const out = await settleDoor({ status: 202, ok: false }, desk);
            desk = out.ok ? out.body : { advanced: false, message: (out.body || {}).detail };
        }
        if (desk && desk.advanced === false && desk.message) showDesktopReport(desk.message, true);
        else if (desk && desk.warning) showDesktopReport('! ' + desk.warning, false);
    }

    async function fetchJSON(url, opts) {
        const r = await apiFetch(url, opts);
        if (!r.ok) throw new Error(`${url} → ${r.status}`);
        return r.json();
    }

    // Escapes all five, so a value is safe as text AND inside a quoted
    // attribute. (It used to be the textContent→innerHTML trick, which leaves
    // " and ' alone: a Host-chosen file name broke out of data-entry="…".)
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    // A <button> with a class and a plain-text label.
    function button(cls, label) {
        const b = document.createElement('button');
        b.className = cls;
        b.textContent = label;
        return b;
    }

    // ── View switching ───────────────────────────────────────
    function showView(view) {
        state.view = view;
        document.querySelectorAll('.cseries-nav-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.view === view);
        });
        document.querySelectorAll('.app-view').forEach(v => {
            v.hidden = v.id !== `view-${view}`;
        });
    }

    // ── Gallery: live preview cards (a plain list of text blocks gave the
    // artifacts no organization) ─────────────────
    // Every card is the artifact itself, rendered live and scaled down, loaded
    // only while on screen, in the same sandbox as the viewer. Newest change
    // first; search by name/description; All / Artifacts / Desktops.
    const STAGE_W = 1280, STAGE_H = 800;

    function visibleItems() {
        const q = state.search.trim().toLowerCase();
        return state.artifacts.filter(a => {
            if (state.filter === 'desktop' && !a.desktop) return false;
            if (state.filter === 'other' && a.desktop) return false;
            if (!q) return true;
            return (a.name || '').toLowerCase().includes(q)
                || (a.description || '').toLowerCase().includes(q);
        });
    }

    function renderGallery() {
        const list = document.getElementById('gallery-list');
        const empty = document.getElementById('gallery-empty');
        const items = visibleItems();
        empty.hidden = items.length > 0;
        list.innerHTML = items.map(a => `
            <button class="cseries-card artifact-card" data-id="${a.artifact_id}">
                <span class="thumb">
                    <iframe sandbox="allow-scripts" tabindex="-1" aria-hidden="true"
                            style="width:${STAGE_W}px;height:${STAGE_H}px"></iframe>
                </span>
                <span class="card-name">${esc(a.name)}${a.desktop ? ' <span class="badge label-sys">desktop</span>' : ''}</span>
                <span class="card-desc label-sys">${esc(a.description || '')}</span>
                <span class="label-sys">${esc(fmtWhen(a.updated_at || a.created_at))}</span>
            </button>`).join('');
        list.querySelectorAll('.artifact-card').forEach(el => {
            el.addEventListener('click', () => openArtifact(parseInt(el.dataset.id, 10)));
        });
        observeThumbs();
        sizeThumbs();
        const total = state.artifacts.length;
        setStatus(items.length === total
            ? `${total} artifact${total === 1 ? '' : 's'}`
            : `${items.length} of ${total} artifacts`);
    }

    let thumbObserver = null;
    function observeThumbs() {
        if (thumbObserver) thumbObserver.disconnect();
        thumbObserver = new IntersectionObserver(entries => {
            for (const en of entries) {
                const frame = en.target.querySelector('.thumb iframe');
                if (!frame) continue;
                if (en.isIntersecting) paintThumb(parseInt(en.target.dataset.id, 10), frame);
                else frame.removeAttribute('srcdoc');
            }
        }, { root: null, rootMargin: '300px 0px' });
        document.querySelectorAll('.artifact-card').forEach(el => thumbObserver.observe(el));
    }

    function isDesktopDoc(html) {
        return /<meta[^>]+name\s*=\s*["']cseries-desktop["']/i.test(String(html).slice(0, 65536));
    }

    async function paintThumb(id, frame) {
        const a = state.artifacts.find(x => x.artifact_id === id);
        if (!a) return;
        // Re-rendered only when the artifact changed since the cached copy.
        let cached = state.thumbs.get(id);
        if (!cached || cached.updated_at !== a.updated_at) {
            let html = null;
            try {
                const r = await apiFetch(`/api/artifacts/${id}/render`);
                if (r.ok) html = await r.text();
            } catch (_e) { html = null; }
            cached = { updated_at: a.updated_at, html };
            state.thumbs.set(id, cached);
        }
        if (!frame.isConnected) return;
        if (!cached.html) {
            frame.srcdoc = '<!DOCTYPE html><body style="margin:0;display:grid;place-items:center;'
                + 'height:100vh;font:28px system-ui;color:#8b968f;background:#0e1416">nothing to show yet</body>';
            return;
        }
        let doc = cached.html;
        if (isDesktopDoc(doc) && window.DesktopPreview) {
            doc = window.DesktopPreview.instrument(doc, {});
        }
        frame.srcdoc = sealed(doc);
    }

    function sizeThumbs() {
        document.querySelectorAll('.artifact-card .thumb').forEach(t => {
            const f = t.querySelector('iframe');
            if (f) f.style.transform = `scale(${t.clientWidth / STAGE_W})`;
        });
    }
    window.addEventListener('resize', sizeThumbs);

    function fmtWhen(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        return isNaN(d) ? '' : d.toLocaleString();
    }

    let listSignature = '';
    async function refreshList() {
        // Every artifact, page by page (the list answers total + next_offset;
        // it used to stop at 200 with nothing said).
        const all = [];
        let offset = 0;
        for (;;) {
            const data = await fetchJSON(`/api/artifacts?limit=200&offset=${offset}`);
            all.push(...(data.artifacts || []));
            if (data.next_offset == null || data.next_offset <= offset) break;
            offset = data.next_offset;
        }
        state.artifacts = all;
        // The poll runs every 3 s; rebuilding the cards reloads every live
        // preview, so the gallery is redrawn only when the list changed.
        const sig = state.artifacts.map(a => `${a.artifact_id}:${a.updated_at}`).join('|');
        if (state.view === 'gallery' && sig !== listSignature) renderGallery();
        listSignature = sig;
        return state.artifacts;
    }

    // ── The instrumentation the VIEWER injects into the frame ────────
    // Runs inside the opaque-origin sandbox. It is viewer-authored (not
    // artifact-authored): declared-action clicks always post; select/edit
    // instrumentation activates only when tune mode is switched on.
    const TUNE_SCRIPT = '<scr' + 'ipt data-cseries-bridge>(function(){"use strict";\n'
        + 'var tune=false;\n'
        + 'function cpath(el){var p=[];while(el&&el.nodeType===1&&el.tagName!=="HTML"&&el.tagName!=="BODY"){var s=el.tagName.toLowerCase();var par=el.parentElement;if(par){var sib=[].filter.call(par.children,function(c){return c.tagName===el.tagName});if(sib.length>1){s+=":nth-of-type("+([].indexOf.call(sib,el)+1)+")"}}p.unshift(s);el=par}return p.join(" > ").slice(0,512)}\n'
        + 'window.addEventListener("message",function(e){var d=e.data;if(d&&d.type==="artifact:tune"){tune=!!d.on;try{document.body.style.cursor=tune?"crosshair":""}catch(_){}}});\n'
        + 'document.addEventListener("click",function(e){\n'
        + '  var act=e.target&&e.target.closest?e.target.closest("[data-cseries-action]"):null;\n'
        + '  if(act){e.preventDefault();parent.postMessage({type:"artifact:act",action:String(act.getAttribute("data-cseries-action")||"").slice(0,64)},"*");return}\n'
        + '  if(!tune)return;e.preventDefault();e.stopPropagation();var t=e.target;\n'
        + '  parent.postMessage({type:"artifact:select",path:cpath(t),text:String(t.textContent||"").trim().slice(0,280)},"*");\n'
        + '},true);\n'
        + 'document.addEventListener("dblclick",function(e){\n'
        + '  if(!tune)return;var t=e.target;if(!t||(t.children&&t.children.length))return;e.preventDefault();\n'
        + '  var before=String(t.textContent||"");t.setAttribute("contenteditable","true");t.focus();\n'
        + '  t.addEventListener("blur",function h(){t.removeEventListener("blur",h);t.removeAttribute("contenteditable");\n'
        + '    var after=String(t.textContent||"");if(after!==before){parent.postMessage({type:"artifact:edit",path:cpath(t),before:before,after:after},"*")}});\n'
        + '},true);\n'
        + '})();</scr' + 'ipt>';

    function instrument(html) {
        // Declared act ids come from the STATIC markup only — collected here,
        // before any frame script could add data-cseries-action attributes.
        state.declared = new Set();
        const re = /data-cseries-action=["']([a-z0-9-]{1,64})["']/g;
        let m;
        while ((m = re.exec(html)) !== null) state.declared.add(m[1]);
        try {
            state.ownDoc = new DOMParser().parseFromString(html, 'text/html');
        } catch (e) { state.ownDoc = null; }
        if (/<body[^>]*>/i.test(html)) {
            return html.replace(/<body([^>]*)>/i, mm => mm + TUNE_SCRIPT);
        }
        return TUNE_SCRIPT + html;
    }

    // ── Viewer ───────────────────────────────────────────────
    async function openArtifact(id) {
        const meta = state.artifacts.find(a => a.artifact_id === id) || null;
        state.current = {
            artifact_id: id,
            name: meta ? meta.name : `artifact ${id}`,
            updated_at: meta ? meta.updated_at : null,
        };
        state.entry = 'index.html';
        state.pages = null;
        state.histView = null; state.history = [];
        const hbar = document.getElementById('history-bar');
        hbar.hidden = true; hbar.innerHTML = '';
        document.getElementById('btn-history').classList.remove('active');
        // A desktop report belongs to the click that produced it, on the
        // artifact it was about — so it does not follow the user to the next
        // one, where nobody asked for a desktop at all.
        const drep = document.getElementById('desktop-report');
        if (drep) drep.hidden = true;
        document.getElementById('viewer-name').textContent = state.current.name;
        document.getElementById('viewer-tab').hidden = false;
        showView('viewer');
        await Promise.all([renderCurrent(), refreshEntries()]);
    }

    async function renderCurrent(tag) {
        if (!state.current) return;
        const frame = document.getElementById('artifact-frame');
        const noRender = document.getElementById('viewer-empty');
        const vs = document.getElementById('viewer-status');
        try {
            const r = await apiFetch(`/api/artifacts/${state.current.artifact_id}/render?entry=${encodeURIComponent(state.entry)}`);
            if (r.status === 404) {
                frame.hidden = true;
                noRender.hidden = false;
                showPages(null);
                vs.textContent = 'no entry yet';
                return;
            }
            if (r.status === 300) {
                // Several pages and none at the root: the kernel names them and
                // the person picks — the same answer the Workbench's preview gives.
                const d = await r.json();
                state.pages = Array.isArray(d.entries) ? d.entries : [];
                frame.hidden = true;
                noRender.hidden = true;
                showPages(d.detail || 'choose a page to open');
                vs.textContent = 'choose a page';
                return;
            }
            if (!r.ok) throw new Error(`render → ${r.status}`);
            const raw = await r.text();
            frame.srcdoc = sealed(instrument(raw));
            noteExternalRefs(raw);
            showPages(null);
            frame.hidden = false;
            noRender.hidden = true;
            vs.textContent = tag || 'live';
            // Re-assert tune state into the fresh document.
            if (state.tune) setTimeout(() => sendTune(true), 150);
            refreshDesktopEligibility();
        } catch (e) {
            frame.hidden = true;
            noRender.hidden = false;
            vs.textContent = 'render failed';
            console.error('artifact render failed:', e);
        }
    }

    // ── Pages (the render's 300) ─────────────────────────────
    // `detail` shows the list (nothing open yet); null hides it. The switcher
    // in the viewer bar offers the same pages once one of them is open.
    //
    // Built with DOM calls, never markup strings: a page name is a FILE NAME the
    // Host (or a harness) wrote, and a name like  x" onclick="…  put through a
    // template into an attribute is script in this window — the one that holds
    // the session. textContent and .value carry it as text, whatever it says.
    function showPages(detail) {
        const box = document.getElementById('viewer-pages');
        const pages = state.pages || [];
        if (detail) {
            document.getElementById('viewer-pages-detail').textContent = detail;
            const list = document.getElementById('viewer-pages-list');
            list.replaceChildren(...pages.map((p) => {
                const b = document.createElement('button');
                b.className = 'cseries-btn';
                b.type = 'button';
                b.dataset.page = p;
                b.textContent = p;
                b.addEventListener('click', () => openPage(p));
                return b;
            }));
            box.hidden = false;
        } else {
            box.hidden = true;
        }
        const sw = document.getElementById('page-switch');
        if (!detail && pages.length > 1 && pages.includes(state.entry)) {
            sw.replaceChildren(...pages.map((p) => {
                const o = document.createElement('option');
                o.value = p;
                o.textContent = p;
                o.selected = p === state.entry;
                return o;
            }));
            sw.hidden = false;
        } else {
            sw.hidden = true;
        }
    }

    function openPage(page) {
        state.entry = page;
        renderCurrent(`page: ${page}`);
    }

    // ── Variants ─────────────────────────────────────────────
    async function refreshEntries() {
        if (!state.current) return;
        try {
            const data = await fetchJSON(`/api/artifacts/${state.current.artifact_id}/files`);
            state.entries = data.files || [];
        } catch (e) { state.entries = []; }
        const bar = document.getElementById('variant-bar');
        bar.replaceChildren();
        if (state.entries.length <= 1) { bar.hidden = true; return; }
        bar.hidden = false;
        // Built node by node: the file names are the Host's, and a name is
        // only ever text or an attribute VALUE here, never markup.
        for (const f of state.entries) {
            const name = String(f);
            const tab = button('cseries-btn variant-tab' + (name === state.entry ? ' active' : ''),
                name.replace(/\.html$/, ''));
            tab.dataset.entry = name;
            tab.addEventListener('click', () => {
                state.entry = name;
                refreshEntries();
                renderCurrent(`variant: ${state.entry}`);
            });
            bar.appendChild(tab);
        }
        if (state.entry !== 'index.html') {
            const pick = button('cseries-btn', 'Use this one');
            pick.id = 'btn-pick-variant';
            pick.addEventListener('click', () => pickVariant());
            bar.appendChild(pick);
        }
    }

    // The pick is an OPERATION: the kernel copies the
    // chosen file over index.html as one revision — sub-second, no message,
    // no model. (It used to go out as a chat message; the host then re-typed
    // a 37 KB file through itself for 5 minutes and never mounted it.) If
    // this artifact IS the live desktop, the shell's own poll re-renders it.
    async function pickVariant() {
        if (!state.current || state.entry === 'index.html') return;
        const vs = document.getElementById('viewer-status');
        vs.textContent = `making ${state.entry} the main version…`;
        try {
            const r = await apiFetch(`/api/artifacts/${state.current.artifact_id}/promote`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ from: state.entry, to: 'index.html' }),
            });
            const body = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
            state.entry = 'index.html';
            await refreshEntries();
            await renderCurrent(`${body.promoted} is now the main version`);
            noteWornDesktop(body);
        } catch (e) {
            vs.textContent = `could not promote: ${e.message}`;
        }
    }


    // Every host-authored document is mounted under the shared sandbox CSP
    // (dashboard/base/js/sandbox-csp.js). The sandbox attribute alone leaves
    // fetch/img/beacon egress open — measured — and these frames render HTML
    // the host wrote and can rewrite at any time.
    function sealed(html) {
        // FAIL CLOSED. If /js/sandbox-csp.js did not load, the policy that keeps
        // a host-authored document off the network is simply absent — and the
        // frame looks identical either way, so nobody would notice. This is not
        // hypothetical: app bundles and the kernel image roll independently, and
        // a live pod was measured serving /js/qrcode.js 200 while
        // /js/sandbox-csp.js was still 404. Returning the raw document there
        // would have quietly un-done the whole fix during every such window.
        //
        // No inline fallback policy on purpose: a second copy of the rule is the
        // drift this consolidation removed. A missing control means the surface
        // does not render, and says so.
        if (!window.CSeriesSandbox) {
            return '<!DOCTYPE html><html><body style="margin:0;display:flex;'
                + 'align-items:center;justify-content:center;height:100vh;'
                + 'font:13px system-ui;color:#8b968f;background:transparent;'
                + 'text-align:center;padding:24px">'
                + 'This can\'t be shown right now — the sandbox policy '
                + '(/js/sandbox-csp.js) did not load, and host-authored HTML is '
                + 'never rendered without it. Reload the app.'
                + '</body></html>';
        }
        return window.CSeriesSandbox.withSandboxCsp(html);
    }

    // The CSP blocks external assets, which is the intended outcome — the
    // authoring rules say inline everything — but a blocked image just looks
    // broken. Say which references were dropped so the host can inline them,
    // rather than leaving the user to guess.
    function noteExternalRefs(raw) {
        const refs = window.CSeriesSandbox ? window.CSeriesSandbox.externalRefs(raw) : [];
        let box = document.getElementById('external-refs');
        if (!refs.length) { if (box) box.hidden = true; return; }
        if (!box) {
            box = document.createElement('div');
            box.id = 'external-refs';
            box.style.cssText = 'margin:8px 0 0;padding:8px 10px;font-size:11px;'
                + 'line-height:1.5;border-radius:6px;background:var(--cseries-night);'
                + 'border:1px solid var(--cseries-mist);color:var(--cseries-ash)';
            const vs = document.getElementById('viewer-status');
            if (vs && vs.parentNode) vs.parentNode.insertBefore(box, vs.nextSibling);
        }
        box.hidden = false;
        box.textContent = 'Not loaded — this preview has no network, so '
            + refs.length + ' external reference' + (refs.length === 1 ? '' : 's')
            + ' were blocked: ' + refs.slice(0, 3).join(', ')
            + (refs.length > 3 ? ' …' : '') + '. Ask your Host to inline them.';
    }

    // Whether THIS artifact could actually be the desktop, asked of the kernel
    // rather than guessed here.
    //
    // The button used to be on every artifact, and the only way to find out was
    // to click it and read a 422. That is a refusal arriving after the user has
    // already decided. It is offered only on artifacts that can actually be
    // set as a desktop safely.
    //
    // GET /api/appearance/desktop/check runs the SAME desktop_gates() the PUT
    // runs, so the button and the kernel cannot disagree. Re-implementing the
    // floor here to grey the button out would drift the first time either side
    // moved, and a button that lies about what will happen is worse than one
    // that is simply always there.
    //
    // It is DISABLED with the reason in its title, never hidden: a vanished
    // button teaches nobody anything. The reason lives in the tooltip and
    // NOWHERE ELSE — the floor's full report used to be painted into the
    // viewer as a panel on every artifact the floor declined, which is every
    // artifact that was never meant to be a desktop: a REFUSED warning that
    // took over the whole frame for no reason. Nobody asked to set it as a desktop, so
    // nothing is being refused; the report belongs to the click, and it is
    // still shown by showDesktopReport() when the user actually clicks.
    async function refreshDesktopEligibility() {
        const btn = document.getElementById('btn-desktop');
        if (!btn || !state.current) return;
        btn.disabled = true;
        btn.title = 'checking whether this can be your desktop…';
        try {
            const r = await apiFetch(
                `/api/appearance/desktop/check?artifact_id=${state.current.artifact_id}`
                + `&entry=${encodeURIComponent(state.entry)}`);
            if (!r.ok) {
                // A hard error (not an artifact, missing entry) is not "this
                // design needs work" — say so rather than implying the design
                // is at fault.
                btn.title = `can't check this one (HTTP ${r.status})`;
                return;
            }
            const c = await r.json();
            state.desktopCheck = c;
            btn.disabled = !c.ok;
            if (c.ok) {
                const warn = (c.warnings || []).length;
                btn.title = 'Make this artifact your desktop — the shell renders it under '
                    + 'your windows and keeps it live'
                    + (warn ? ` (${warn} thing(s) to tidy, it will still mount)` : '');
            } else {
                const first = (c.errors || [])[0] || 'it does not meet the desktop floor';
                btn.title = "This artifact can't be a desktop yet — " + first;
            }
        } catch (e) {
            btn.title = `can't check this one: ${e.message}`;
        }
    }

    // "Use as desktop" is an OPERATION too: point the desktop at THIS
    // artifact (entry = the variant being viewed). The kernel enforces the
    // desktop floor and answers
    // 422 with a readable report when a document is missing it — shown here,
    // and sent to the host as the user's message so the design gets fixed in
    // the same conversation instead of the user staring at a refusal.
    async function useAsDesktop() {
        if (!state.current) return;
        const vs = document.getElementById('viewer-status');
        vs.textContent = 'setting as your desktop…';
        try {
            const r0 = await apiFetch('/api/appearance/desktop', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ artifact_id: state.current.artifact_id, entry: state.entry }),
            });
            const r = await settleDoor(r0, await r0.json().catch(() => ({})));
            const body = r.body || {};
            if (!r.ok) {
                const detail = String(body.detail || `HTTP ${r.status}`);
                showDesktopReport(detail, r.status === 422);
                if (r.status === 422) {
                    compose('send', `Artifact ${state.current.artifact_id} ("${state.current.name}"): I tried to use ${state.entry} as my desktop and it was refused —\n${detail.slice(0, 1500)}`);
                }
                return;
            }
            const warn = (body.warnings || []).length ? ` · ${body.warnings.length} thing(s) to tidy` : '';
            vs.textContent = `this is your desktop now (${body.mode}, ${body.tokens} tokens)${warn}`;
            if ((body.warnings || []).length) showDesktopReport(body.warnings.map(w => '! ' + w).join('\n'), false);
        } catch (e) {
            vs.textContent = `could not set desktop: ${e.message}`;
        }
    }

    function showDesktopReport(text, refused) {
        const vs = document.getElementById('viewer-status');
        vs.textContent = refused ? 'refused — see report' : 'set, with notes';
        let box = document.getElementById('desktop-report');
        if (!box) {
            box = document.createElement('pre');
            box.id = 'desktop-report';
            box.className = 'desktop-report';
            const bar = document.getElementById('variant-bar');
            bar.parentNode.insertBefore(box, bar);
        }
        box.hidden = false;
        box.textContent = text;
        box.onclick = () => { box.hidden = true; };
    }

    // ── Change history ───────────────────────────────────────
    // Every create/write/tune-edit is a revision server-side. The strip lists
    // them; clicking one renders that PAST state (read-only — no tune
    // instrumentation) in the same sandboxed frame; Restore reverts to it
    // (the revert is itself a revision, so nothing is ever lost).
    async function toggleHistory() {
        const bar = document.getElementById('history-bar');
        if (!bar.hidden) { exitHistory(); return; }
        try {
            const data = await fetchJSON(`/api/artifacts/${state.current.artifact_id}/history`);
            state.history = data.history || [];
        } catch (e) { state.history = []; }
        if (!state.history.length) {
            document.getElementById('viewer-status').textContent = 'no history yet';
            return;
        }
        document.getElementById('btn-history').classList.add('active');
        bar.hidden = false;
        renderHistoryBar();
    }

    function renderHistoryBar() {
        const bar = document.getElementById('history-bar');
        bar.replaceChildren();
        // Built node by node: a revision's note carries the Host's file names
        // (a promote's "a.html → index.html", an import's list).
        for (const h of state.history) {
            const rev = String(h.rev == null ? '' : h.rev);
            const el = button('cseries-btn hist-rev' + (h.rev === state.histView ? ' active' : ''),
                `${rev} · ${fmtWhen(h.ts)} · ${h.actor == null ? '' : h.actor}`);
            el.dataset.rev = rev;
            el.title = String(h.note || h.op || '');
            el.addEventListener('click', () => viewRevision(rev));
            bar.appendChild(el);
        }
        if (state.histView) {
            const restore = button('cseries-btn', 'Restore this version');
            restore.id = 'btn-restore';
            restore.addEventListener('click', () => restoreRevision(state.histView));
            const live = button('cseries-btn', 'Back to current');
            live.id = 'btn-hist-live';
            live.addEventListener('click', () => { state.histView = null; renderHistoryBar(); renderCurrent('live'); });
            bar.append(restore, live);
        }
    }

    async function viewRevision(rev) {
        try {
            const r = await apiFetch(`/api/artifacts/${state.current.artifact_id}/history/${encodeURIComponent(rev)}/render?entry=${encodeURIComponent(state.entry)}`);
            if (!r.ok) throw new Error(`history render → ${r.status}`);
            const frame = document.getElementById('artifact-frame');
            frame.srcdoc = sealed(await r.text());   // read-only: no tune instrumentation
            frame.hidden = false;
            document.getElementById('viewer-empty').hidden = true;
            state.histView = rev;
            document.getElementById('viewer-status').textContent = `viewing revision ${rev} (read-only)`;
            renderHistoryBar();
        } catch (e) {
            document.getElementById('viewer-status').textContent = 'revision unavailable';
        }
    }

    async function restoreRevision(rev) {
        try {
            const r = await apiFetch(`/api/artifacts/${state.current.artifact_id}/revert`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ rev }),
            });
            if (!r.ok) throw new Error(`revert → ${r.status}`);
            const body = await r.json().catch(() => ({}));
            exitHistory();
            await renderCurrent(`restored revision ${rev}`);
            noteWornDesktop(body);
        } catch (e) {
            document.getElementById('viewer-status').textContent = 'restore failed';
        }
    }

    function exitHistory() {
        state.histView = null;
        state.history = [];
        const bar = document.getElementById('history-bar');
        bar.hidden = true; bar.innerHTML = '';
        document.getElementById('btn-history').classList.remove('active');
        renderCurrent('live');
    }

    // ── Live poll ────────────────────────────────────────────
    async function pollTick() {
        if (document.visibilityState !== 'visible') return;
        try {
            const items = await refreshList();
            if (state.current && state.view === 'viewer') {
                const meta = items.find(a => a.artifact_id === state.current.artifact_id);
                if (!meta) {
                    state.current = null;
                    document.getElementById('viewer-tab').hidden = true;
                    showView('gallery');
                    return;
                }
                if (meta.updated_at && meta.updated_at !== state.current.updated_at) {
                    state.current.updated_at = meta.updated_at;
                    state.current.name = meta.name;
                    document.getElementById('viewer-name').textContent = meta.name;
                    if (state.histView) {
                        // Viewing a past revision — don't stomp the frame;
                        // just note that the live artifact moved on.
                        document.getElementById('viewer-status').textContent =
                            `viewing revision ${state.histView} (read-only) — live version changed`;
                    } else {
                        await Promise.all([
                            renderCurrent('updated ' + new Date().toLocaleTimeString()),
                            refreshEntries(),
                        ]);
                    }
                }
            }
        } catch (e) {
            console.debug('artifacts poll skipped:', e);
        }
    }

    // ── The bridge — the ONE door through the wall ───────────────────
    function drop() {
        state.dropped += 1;
        const el = document.getElementById('footer-left');
        el.textContent = `artifacts · 0.4.0 · ${state.dropped} message${state.dropped === 1 ? '' : 's'} ignored`;
    }

    function rateOk() {
        const now = Date.now();
        state.stampsSec = state.stampsSec.filter(t => now - t < 1000);
        state.stampsMin = state.stampsMin.filter(t => now - t < 60000);
        if (state.stampsSec.length >= RATE_PER_SEC || state.stampsMin.length >= RATE_PER_MIN) return false;
        state.stampsSec.push(now);
        state.stampsMin.push(now);
        return true;
    }

    function ownTextAt(path) {
        if (!state.ownDoc || !path) return null;
        try {
            const el = state.ownDoc.querySelector(path);
            return el ? String(el.textContent || '').trim() : null;
        } catch (e) { return null; }
    }

    function ack(frame, of, ok) {
        try { frame.contentWindow.postMessage({ type: 'artifact:ack', of, ok }, '*'); } catch (e) {}
    }

    function compose(mode, text) {
        try {
            (window.parent || window).postMessage(
                { type: 'cseries:compose', mode, text: String(text).slice(0, 2000) },
                window.location.origin);
        } catch (e) {}
    }

    function sendTune(on) {
        const frame = document.getElementById('artifact-frame');
        try { frame.contentWindow.postMessage({ type: 'artifact:tune', on: !!on }, '*'); } catch (e) {}
    }

    async function onBridgeMessage(e) {
        const frame = document.getElementById('artifact-frame');
        if (!frame || e.source !== frame.contentWindow) return;   // identity, not origin (opaque)
        if (!state.current) return;
        const d = e.data;
        let size = 0;
        try { size = JSON.stringify(d).length; } catch (_) { return drop(); }
        if (!d || typeof d !== 'object' || size > MSG_MAX_BYTES) return drop();
        if (!rateOk()) return drop();
        const id = state.current.artifact_id;

        if (d.type === 'artifact:act') {
            const action = typeof d.action === 'string' ? d.action : '';
            if (!/^[a-z0-9-]{1,64}$/.test(action) || !state.declared.has(action)) { ack(frame, 'artifact:act', false); return drop(); }
            // One click → the user's own visible message. No confirmation.
            compose('send', `Artifact ${id} ("${state.current.name}"): ${action.replace(/-/g, ' ')}`);
            ack(frame, 'artifact:act', true);
            return;
        }
        if (d.type === 'artifact:select') {
            if (!state.tune) return drop();
            const path = (typeof d.path === 'string' ? d.path : '').slice(0, 512);
            let text = (typeof d.text === 'string' ? d.text : '').slice(0, 280);
            if (!path) { ack(frame, 'artifact:select', false); return drop(); }
            const derived = ownTextAt(path);
            if (derived !== null && derived.slice(0, 280) !== text) text = derived.slice(0, 280);
            compose('pill', `artifact ${id} · ${path}${text ? ` · "${text}"` : ''}`);
            ack(frame, 'artifact:select', true);
            return;
        }
        if (d.type === 'artifact:edit') {
            if (!state.tune) return drop();
            const before = typeof d.before === 'string' ? d.before : null;
            const after = typeof d.after === 'string' ? d.after : null;
            // No length cut here: the kernel bounds an edit by the page it
            // edits (one edit is at most the page's own length) and says so.
            if (before === null || after === null || !before.trim()
                || /[<>]/.test(after) || /[<>]/.test(before)) { ack(frame, 'artifact:edit', false); return drop(); }
            const derived = ownTextAt((typeof d.path === 'string' ? d.path : '').slice(0, 512));
            if (derived !== null && derived !== before.trim()) { ack(frame, 'artifact:edit', false); return drop(); }
            try {
                const r = await apiFetch(`/api/artifacts/${id}/bridge`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ op: 'edit', entry: state.entry, before, after }),
                });
                if (!r.ok) { ack(frame, 'artifact:edit', false); return drop(); }
                // Attributable, batched: the change rides the user's next message
                // as a pill; the window itself re-renders within a poll tick.
                compose('pill', `artifact ${id}: changed "${before.slice(0, 40)}" → "${after.slice(0, 40)}"`);
                ack(frame, 'artifact:edit', true);
                r.json().then(noteWornDesktop).catch(() => {});
            } catch (err) {
                ack(frame, 'artifact:edit', false);
                drop();
            }
            return;
        }
        // Unknown type — dropped unread.
        drop();
    }

    // ── Deep links ───────────────────────────────────────────
    function handleViewSpec(spec) {
        const m = /^artifact:(\d+)$/.exec(String(spec || '').trim());
        if (m) openArtifact(parseInt(m[1], 10));
    }

    function viewFromHash() {
        const m = /[#&]view=([^&]+)/.exec(window.location.hash || '');
        if (m) handleViewSpec(decodeURIComponent(m[1]));
    }

    function setStatus(msg) {
        document.getElementById('footer-status').textContent = msg;
    }

    // ── Boot ─────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', async () => {
        document.querySelectorAll('.cseries-nav-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                if (btn.dataset.view === 'viewer' && !state.current) return;
                showView(btn.dataset.view);
                if (btn.dataset.view === 'gallery') renderGallery();
            });
        });
        document.getElementById('btn-back').addEventListener('click', () => {
            showView('gallery');
            renderGallery();
        });
        document.getElementById('gallery-search').addEventListener('input', (e) => {
            state.search = e.target.value || '';
            renderGallery();
        });
        document.querySelectorAll('.filter-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                state.filter = btn.dataset.filter;
                document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b === btn));
                renderGallery();
            });
        });
        document.getElementById('btn-refresh').addEventListener('click', () => renderCurrent('refreshed'));
        document.getElementById('page-switch').addEventListener('change', (e) => openPage(e.target.value));
        document.getElementById('btn-history').addEventListener('click', toggleHistory);
        // HTML desktop: a click, not a message.
        // The window points the desktop at this artifact itself (PUT
        // /api/appearance/desktop, permission write:appearance); the kernel's
        // floor decides, and a refusal is shown here AND sent to the host.
        document.getElementById('btn-desktop').addEventListener('click', () => useAsDesktop());
        document.getElementById('btn-tune').addEventListener('click', () => {
            state.tune = !state.tune;
            document.getElementById('btn-tune').classList.toggle('active', state.tune);
            sendTune(state.tune);
            document.getElementById('viewer-status').textContent = state.tune
                ? 'tune: click = reference · double-click = edit text'
                : 'live';
        });

        window.addEventListener('hashchange', viewFromHash);
        window.addEventListener('message', (e) => {
            // Shell messages (same-origin): deep-link view switching.
            if (e.origin === window.location.origin) {
                const d = e.data;
                if (d && d.type === 'cseries:app-view' && d.view) handleViewSpec(d.view);
                return;
            }
            // Everything else can only be the artifact frame (opaque origin).
            onBridgeMessage(e);
        });

        try {
            await refreshList();
        } catch (e) {
            setStatus('could not load artifacts');
            console.error(e);
        }
        viewFromHash();
        state.pollTimer = setInterval(pollTick, POLL_MS);
    });

    // Expose for debugging
    window.App = { state, showView, openArtifact, refreshList };
})();
