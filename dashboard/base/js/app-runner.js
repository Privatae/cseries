/**
 * Privatae Personal Computer — App Runner
 *
 * Launches an app as a first-class OS window. Each launch:
 *   1. Fetches manifest from /api/apps/info/<id>
 *   2. Calls /api/applications/<id>/launch to mint a per-window
 *      app token bound to the manifest's declared permissions
 *   3. Creates a window with an embedded surface and wraps that
 *      surface's `fetch` so every /api/* request carries the token
 *      via the X-Application-Token header
 *
 * The surface is implemented as a same-origin <iframe> today; the
 * runtime concept the dashboard cares about is "the window," which
 * stays stable when sidecar processes replace iframes in a later phase.
 */
const AppRunner = (() => {

    const openWindows = new Map();   // appId -> windowId

    // Windows of apps that run in their own pod hold that pod while they are
    // open. The kernel only knows a window is still open while this page says
    // so: a beat per window every window_beat_s (the launch answer), and a
    // close for every one when the page goes away (reload, tab close). A page
    // that dies without either simply stops beating, and the kernel lets the
    // hold go (server/applications/harness_backend.py app_window_beat).
    const heldWindows = new Map();   // kernel window_id -> {appId, timer}

    function postWindow(appId, path, windowId, keepalive) {
        return fetch(`/api/applications/${encodeURIComponent(appId)}/${path}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ window_id: windowId }),
            keepalive: !!keepalive,
        }).catch(() => {});
    }

    function holdWindow(appId, windowId, beatS) {
        if (!windowId || !beatS) return;
        const timer = setInterval(() => postWindow(appId, 'window-beat', windowId), beatS * 1000);
        heldWindows.set(windowId, { appId, timer });
    }

    function releaseWindow(windowId) {
        const h = heldWindows.get(windowId);
        if (!h) return;
        clearInterval(h.timer);
        heldWindows.delete(windowId);
    }

    // The page is going away: every window it held closes now. Not relied on
    // (a crash sends nothing); the stopped beats are what end the hold.
    window.addEventListener('pagehide', () => {
        for (const [windowId, h] of heldWindows) {
            clearInterval(h.timer);
            postWindow(h.appId, 'close', windowId, true);
        }
        heldWindows.clear();
    });

    // One origin per isolated app. Every isolated app used to share one
    // origin, apps-<handle>, so a co-open community app could read the
    // Settings frame. Each now gets apps-<key>-<handle>: <key> is the 20
    // lowercase base32 characters the kernel derives from the app id
    // (server/applications/app_origin.py) and sends as manifest.origin_key.
    // Pure, and exported, so the app-origin guard
    // (scripts/security_repro_782_app_origin_read.py) runs THIS code. Null =
    // do not frame: not isolated, no valid key (the kernel withholds one on a
    // key collision), or a handle too long to leave room for the key in one
    // DNS label. location.protocol/host so a test origin with a port works;
    // in production it is https and no port.
    const ORIGIN_KEY_RE = /^[a-z2-7]{20}$/;
    function frameOrigin(manifest, loc) {
        if (!manifest || !manifest.isolated) return null;
        const key = manifest.origin_key;
        if (typeof key !== 'string' || !ORIGIN_KEY_RE.test(key)) return null;
        const host = 'apps-' + key + '-' + loc.host;
        if (host.split('.')[0].split(':')[0].length > 63) return null;
        return loc.protocol + '//' + host;
    }

    // Seed lane — AppRunner.launch(appId, { prompt }) hands the opened app one
    // message to SEND as the user, over the `cseries:compose` contract the Circles
    // composer already honours (mode='send' = a declared act, sent as the
    // user's own message). Origin: the
    // Orchestrator's Connect button built exactly this payload and
    // launch() dropped it on the floor — Circles focused, nothing was said, the
    // button "did absolutely nothing". Same-origin only: the composer ignores a
    // foreign origin and we never post a prompt into an isolated frame.
    // Delivered ONCE — a 'send' that arrives twice sends twice — so the
    // new-window path waits for the app's composer to EXIST before posting
    // (the React listener registers in the component that renders it) instead
    // of firing at iframe load, which can precede React's effects.
    function postCompose(iframe, text) {
        try {
            if (!iframe || !iframe.contentWindow) return false;
            const origin = new URL(iframe.src, window.location.href).origin;
            if (origin !== window.location.origin) return false;
            iframe.contentWindow.postMessage(
                { type: 'cseries:compose', mode: 'send', text: String(text).slice(0, 2000) },
                window.location.origin);
            return true;
        } catch (e) { return false; }
    }
    function deliverPromptWhenReady(iframe, text) {
        const started = Date.now();
        const tick = () => {
            let composer = null;
            try {
                const doc = iframe.contentDocument;
                // An app whose listener is the page's, not its composer's,
                // marks <html data-compose-inbox> and holds the seed until its
                // composer is drawn (circles-web/src/lib/compose-inbox.ts: the
                // cards start on a deck with no text box).
                composer = doc && (doc.querySelector('textarea, [contenteditable="true"]')
                                   || doc.querySelector('html[data-compose-inbox]'));
            } catch (e) { return; }   // cross-origin — never deliver
            if (composer) { setTimeout(() => postCompose(iframe, text), 300); return; }
            if (Date.now() - started < 20000) setTimeout(tick, 200);
        };
        iframe.addEventListener('load', tick);
    }

    // A view for a window that is already open. Its frame may still be LOADING
    // — a window desktop-restore opened a moment ago, which a deep link then
    // finds "already open" — and a message posted before the frame's document
    // is up is simply lost. So an unloaded frame gets the view on its load.
    function postView(iframe, view) {
        if (!iframe || !iframe.contentWindow) return;
        const send = () => {
            try {
                const target = new URL(iframe.src, window.location.href).origin;
                iframe.contentWindow.postMessage({ type: 'cseries:app-view', view: String(view) }, target);
            } catch (e) { /* detached / cross-origin — ignore */ }
        };
        if (iframe.dataset.loaded === '1') send();
        else iframe.addEventListener('load', send, { once: true });
    }

    // One launch per app at a time. Two launches in the gap between the open
    // check below and openWindows.set — a double-click on a floating bubble,
    // the Host opening an app over the websocket while the person clicks it,
    // a restore racing a deep link — each minted a window id and a beat
    // timer, but WindowManager.create only focuses an id that already exists,
    // so the second launch's close handler was never registered and its
    // window id kept holding the app's pod forever. A second launch now waits
    // for the first and then takes the already-open path (focus, view,
    // prompt).
    const launching = new Map();   // appId -> Promise of the launch in flight

    async function launch(appId, opts) {
        const inFlight = launching.get(appId);
        if (inFlight) {
            try { await inFlight; } catch (e) { /* the first launch reported its own failure */ }
            return launch(appId, opts);
        }
        if (openWindows.has(appId)) return launchExisting(appId, opts);
        const p = launchNew(appId, opts);
        launching.set(appId, p);
        try {
            return await p;
        } finally {
            if (launching.get(appId) === p) launching.delete(appId);
        }
    }

    function launchExisting(appId, opts) {
        // Re-focus if already open
        const existing = openWindows.get(appId);
        if (existing && window.WindowManager && typeof window.WindowManager.focus === 'function') {
            // A minimized window must be RESTORED — focus() raises z-order
            // but leaves the .minimized class (display:none), so relaunching
            // via the floating bubble / cseries:open-app looked like a no-op.
            const win = (typeof window.WindowManager.getAll === 'function'
                ? window.WindowManager.getAll() : []).find((w) => w.id === existing);
            if (win && win.minimized && typeof window.WindowManager.restore === 'function') {
                window.WindowManager.restore(existing);
            } else {
                window.WindowManager.focus(existing);
            }
            // Forward a requested view into the already-open window (open_app
            // with a view while the app is up — e.g. switching the Artifacts
            // viewer to another artifact). Best-effort: apps that don't listen
            // for cseries:app-view just ignore it.
            if (opts && opts.view) {
                try {
                    const sel = (window.CSS && CSS.escape) ? CSS.escape(appId) : appId.replace(/"/g, '');
                    postView(document.querySelector(`.app-frame-host[data-app-id="${sel}"] iframe`), opts.view);
                } catch (e) { /* detached — ignore */ }
            }
            // Already open: deliver the seed now. Circles holds it until its
            // composer is drawn (circles-web/src/lib/compose-inbox.ts).
            if (opts && opts.prompt) {
                try {
                    const sel = (window.CSS && CSS.escape) ? CSS.escape(appId) : appId.replace(/"/g, '');
                    postCompose(document.querySelector(`.app-frame-host[data-app-id="${sel}"] iframe`), opts.prompt);
                } catch (e) { /* detached — ignore */ }
            }
        }
    }

    async function launchNew(appId, opts) {
        let manifest;
        try {
            // /api/apps/info/<id> was the legacy launcher source and was
            // removed in favor of /api/applications/<id> (registry-backed,
            // same _serialize() shape: url, icon_url, icon_small_url,
            // window, name, id). The dashboard kept calling the legacy
            // path for a while — every app icon clicked threw
            // "not available" because of this single line.
            const res = await fetch(`/api/applications/${encodeURIComponent(appId)}`);
            if (!res.ok) throw new Error('not available');
            manifest = await res.json();
        } catch (err) {
            console.error('AppRunner launch failed:', err);
            const friendlyName = appId.replace(/[-_]/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
            notify('Could not open app', `Could not open ${friendlyName}. Please try again.`, 'error');
            return;
        }

        // An isolated app with no origin of its own is not opened at all:
        // falling back to a shared origin would undo the per-app origin.
        if (manifest.isolated && !frameOrigin(manifest, window.location)) {
            console.error(`AppRunner: ${appId} has no address of its own (origin_key=${manifest.origin_key})`);
            const friendlyName = (manifest.name || appId);
            notify('Could not open app', `${friendlyName} has no address of its own on this Host, so it was not opened.`, 'error');
            return;
        }

        // Mint a per-window app token + (for process apps) spawn the
        // sidecar. The kernel returns runtime_kind so we know whether
        // the window will be served from a static install dir or
        // proxied to a live process.
        let appToken = null;
        let appWindowId = null;
        try {
            const tr = await fetch(`/api/applications/${encodeURIComponent(appId)}/launch`, {
                method: 'POST',
            });
            if (tr.ok) {
                const launchData = await tr.json();
                appToken = launchData.token;
                appWindowId = launchData.window_id;
                holdWindow(appId, appWindowId, launchData.window_beat_s);
            } else {
                console.warn(`AppRunner: launch endpoint returned ${tr.status} for ${appId}`);
                // This machine would not start the app (a short disk, no
                // room): say why and open nothing, since the window could
                // not work. The words come from the kernel and can name app
                // ids, so they go out as one line of text (notify escapes).
                const refused = await refusalOf(tr);
                if (refused) {
                    notify('Could not open ' + oneLine(manifest.name || appId), refused, 'error');
                    return;
                }
            }
        } catch (err) {
            console.warn('AppRunner: token mint failed (continuing without token):', err);
        }

        const win = manifest.window || {};
        const winId = `app-${manifest.id}`;

        if (!window.WindowManager) {
            console.error('WindowManager not available');
            if (appWindowId) {
                releaseWindow(appWindowId);
                postWindow(appId, 'close', appWindowId);
            }
            return;
        }

        // Build icon as inline-SVG glyph (preferred — matches dock/desktop look)
        // or as a small <img> if the app has no glyph mapping.
        const iconHTML = window.iconMarkup
            ? window.iconMarkup(manifest, 16)
            : (manifest.icon_small_url
                ? `<img src="${escapeAttr(manifest.icon_small_url)}" alt="" style="width:16px;height:16px;object-fit:contain;">`
                : '');

        // Holds the SDK-app postMessage listener so onClose can detach it.
        let sdkMsgHandler = null;
        // Holds the phone-mode scale shim's ResizeObserver for the same reason.
        let fitObserver = null;

        // Saved-layout restore (desktop-restore.js): a geometry override wins
        // over the manifest's default placement so a relaunched desktop comes
        // back exactly as it was left.
        const geo = (opts && opts.geometry) || null;

        // create() only FOCUSES an id that already exists (desktop.js), and
        // then none of the handlers below are registered. A window that came
        // up by another route meanwhile keeps its own; the id this launch
        // minted is released now instead of beating forever.
        const already = typeof window.WindowManager.getAll === 'function'
            && window.WindowManager.getAll().some((w) => w.id === winId);
        if (already) {
            if (appWindowId) {
                releaseWindow(appWindowId);
                postWindow(appId, 'close', appWindowId);
            }
            openWindows.set(appId, winId);
            return launchExisting(appId, opts);
        }

        window.WindowManager.create({
            startMaximized: geo ? !!geo.maximized
                : !!(manifest.window && manifest.window.start_maximized),
            id: winId,
            appId: manifest.id,
            title: (win.title || manifest.name || manifest.id),
            icon: iconHTML,
            x: geo && geo.w ? geo.x : undefined,
            y: geo && geo.h ? geo.y : undefined,
            width: (geo && geo.w) || win.width || 800,
            height: (geo && geo.h) || win.height || 560,
            minWidth: win.min_width || 400,
            minHeight: win.min_height || 300,
            resizable: win.resizable !== false,
            content: `<div class="app-frame-host" data-app-id="${escapeAttr(manifest.id)}" style="width:100%;height:100%;overflow:hidden;background:var(--cseries-ink);"></div>`,
            onInit: (body) => {
                const host = body.querySelector('.app-frame-host');
                const iframe = document.createElement('iframe');
                // What postView reads: whether this frame's document is up yet.
                iframe.addEventListener('load', () => { iframe.dataset.loaded = '1'; });
                // allow-popups-to-escape-sandbox: OAuth / sign-in popups (window.open
                // to claude.ai, auth.openai.com, …) MUST be real top-level windows. A
                // popup that inherits THIS iframe's sandbox is an over-restricted context
                // where the IdP's login refuses to run ("claude.ai is blocked / refused
                // to connect") — the same URL pasted into a fresh tab works, which is the
                // tell. The flag only affects popups the app OPENS; the app's own document
                // stays sandboxed, and a popup is a user-visible navigation, not an
                // opener-CSP bypass. (The prior comment claimed that bypass and, on that
                // basis, broke sign-in for every coding harness.)
                iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-downloads allow-modals allow-popups allow-popups-to-escape-sandbox');

                // App-origin isolation: an isolated app loads from its OWN
                // origin, apps-<key>-<handle> (frameOrigin above), so the
                // browser's same-origin policy walls it off from the desktop
                // AND from every other app. Every other app loads same-origin
                // exactly as before (legacy contentWindow path).
                const isolated = !!manifest.isolated;
                // SDK-managed is NOT the same question as isolated, and conflating
                // them is what broke every Workbench-built app: `provenance: custom`
                // is SDK-managed (the kernel injects window.CSeriesApp) but stays
                // same-origin. The token handshake follows the SDK, never the
                // origin — an injected SDK that never receives cseries:app-init leaves
                // `await CSeriesApp.ready` hanging until its 5s timeout and 401s every
                // CSeriesApp.storage call, because the per-app config routes gate on
                // the token itself and do not consult APP_TOKEN_MODE.
                // Older kernels don't send the field; fall back to `isolated` so the
                // shell keeps behaving exactly as it did against them.
                const sdkManaged = (manifest.sdk_managed === undefined)
                    ? isolated : !!manifest.sdk_managed;
                let frameSrc;
                if (isolated) {
                    frameSrc = frameOrigin(manifest, window.location)
                        + '/system-apps/' + encodeURIComponent(manifest.id) + '/';
                } else {
                    frameSrc = manifest.url;
                }
                const bust = (frameSrc.includes('?') ? '&' : '?') + 'v=' + Date.now();
                iframe.src = frameSrc + bust + (opts && opts.view ? '#view=' + encodeURIComponent(opts.view) : '');
                iframe.style.cssText = 'width:100%;height:100%;border:none;background:transparent;display:block;';
                iframe.title = manifest.name || manifest.id;

                let appOrigin;
                try { appOrigin = new URL(iframe.src, window.location.href).origin; }
                catch (e) { appOrigin = window.location.origin; }

                host.appendChild(iframe);

                // Seed lane (see deliverPromptWhenReady): same-origin only.
                if (opts && opts.prompt && !isolated) {
                    deliverPromptWhenReady(iframe, opts.prompt);
                }

                // Phone-mode scale shim: apps are designed for desktop
                // widths (820-1280px per their manifests) with no phone
                // breakpoints — on a narrow screen they compress and
                // deform. Instead, render the app at its designed width
                // and scale the frame down to fit: SHRUNK, not squished.
                // The iframe's own viewport stays at the designed width,
                // so the app lays out exactly as on desktop. Apps that
                // declare window.mobile=responsive opt out and render
                // native full-width (they reflow themselves).
                const mobileMode = win.mobile || 'scale';
                const designWidth = win.width || 800;
                const fitFrame = () => {
                    const phone = window.CSeriesViewport && window.CSeriesViewport.isPhone();
                    const hw = host.clientWidth;
                    const hh = host.clientHeight;
                    if (!phone || mobileMode === 'responsive' || !hw || hw >= designWidth) {
                        iframe.style.width = '100%';
                        iframe.style.height = '100%';
                        iframe.style.transform = '';
                        iframe.style.transformOrigin = '';
                        return;
                    }
                    const scale = hw / designWidth;
                    iframe.style.width = designWidth + 'px';
                    iframe.style.height = Math.round(hh / scale) + 'px';
                    iframe.style.transformOrigin = '0 0';
                    iframe.style.transform = `scale(${scale})`;
                };
                fitFrame();
                if (window.ResizeObserver) {
                    fitObserver = new ResizeObserver(fitFrame);
                    fitObserver.observe(host);
                }

                // An ISOLATED app's frame fetches its own token on its own
                // origin (dashboard/base/js/app-frame-init.js): the desktop
                // never holds it, because every page on the desktop's origin can
                // read what the shell reads. So the handshake below is for the
                // SDK-managed apps that stay same-origin (custom) only.
                if (sdkManaged && !isolated) {
                    // The injected SDK gets the per-window token over postMessage,
                    // because the SDK installs its own fetch shim and that shim is
                    // only useful once the token has arrived. NON-BLOCKING — app-sdk.js adds the token
                    // when it lands; nothing is ever held or delayed (holding
                    // every fetch once broke every app). appOrigin is the frame's real
                    // origin either way, so the message is never broadcast.
                    const sendInit = () => {
                        try {
                            if (iframe.contentWindow) {
                                iframe.contentWindow.postMessage(
                                    { type: 'cseries:app-init', token: appToken || null, app_id: appId },
                                    appOrigin);
                            }
                        } catch (e) { /* detached */ }
                    };
                    sdkMsgHandler = (ev) => {
                        if (ev.source !== iframe.contentWindow) return;
                        const d = ev.data;
                        if (d && d.type === 'cseries:app-ready') {
                            sendInit();
                        } else if (d && d.type === 'cseries:app-init-ack') {
                            console.debug(`AppRunner: ${manifest.id} SDK token ack (present=${d.token_present})`);
                        }
                    };
                    window.addEventListener('message', sdkMsgHandler);
                    iframe.addEventListener('load', sendInit);   // belt + suspenders
                }
                // Legacy same-origin contentWindow shim. Runs for EVERY same-origin
                // app that has a token, including an SDK-managed (custom) one — it
                // is the fail-closed fallback for the case where injection declined:
                // _inject_app_sdk fails CLOSED and injects nothing when no verified
                // SDK is installed (sdk_store), and the app is then served plain. In
                // that case this shim is the only thing that stamps the token, so
                // gating it on !sdkManaged would silently drop the token exactly when
                // the SDK is missing. When the SDK IS present it installed its own
                // wrapper first (it is the first element in <head>), and the
                // __cseriesShim tag below makes this a no-op rather than a double-wrap.
                if (!isolated && appToken) {
                    // Legacy same-origin path: reach into contentWindow. Re-install
                    // on each navigation. Tag the WRAPPER (not the window) so we
                    // don't double-wrap but also don't bail on a re-install — the
                    // bug that silently broke wallpaper upload + named-wallpaper PUT.
                    const installTokenShim = () => {
                        try {
                            const w = iframe.contentWindow;
                            if (!w || !w.fetch) return;
                            if (w.fetch.__cseriesShim) return;
                            const orig = w.fetch.bind(w);
                            // SAME-ORIGIN ONLY — the same rule (and the same
                            // reason) as the SDK's own shim at 1.3.0: the token
                            // is the window's kernel credential; stamping it
                            // cross-origin leaks it to third-party hosts and
                            // breaks strict CORS preflights (api.weather.gov,
                            // measured). Kernel calls are relative
                            // URLs, so no legitimate consumer loses the stamp.
                            const wrapped = function (input, init) {
                                let sameOrigin = true;
                                try {
                                    const raw = (input && typeof input === 'object' && 'url' in input)
                                        ? input.url : String(input);
                                    sameOrigin = new URL(raw, w.location.href).origin
                                        === w.location.origin;
                                } catch (e) { sameOrigin = false; }
                                if (!sameOrigin) return orig(input, init);
                                init = init || {};
                                const headers = new Headers(init.headers || {});
                                if (!headers.has('X-Application-Token')) {
                                    headers.set('X-Application-Token', appToken);
                                }
                                init.headers = headers;
                                return orig(input, init);
                            };
                            wrapped.__cseriesShim = true;
                            w.fetch = wrapped;
                        } catch (e) {
                            // Cross-origin or detached — give up silently.
                        }
                    };
                    iframe.addEventListener('load', installTokenShim);
                    // Also try immediately in case the document is already
                    // available (race-free path for fast-loading apps).
                    installTokenShim();
                }
            },
            onClose: () => {
                openWindows.delete(appId);
                if (sdkMsgHandler) {
                    window.removeEventListener('message', sdkMsgHandler);
                    sdkMsgHandler = null;
                }
                if (fitObserver) {
                    fitObserver.disconnect();
                    fitObserver = null;
                }
                // Tell the kernel we're done. For process apps this
                // releases the supervisor refcount; the last close
                // terminates the sidecar.
                if (appWindowId) {
                    releaseWindow(appWindowId);
                    postWindow(appId, 'close', appWindowId);
                }
            },
        });

        openWindows.set(appId, winId);

        // Restored-as-minimized: create() can't open a window minimized, so
        // dock it right after creation (taskbar tab appears, window hides).
        if (geo && geo.minimized && typeof window.WindowManager.minimize === 'function') {
            window.WindowManager.minimize(winId);
        }
    }

    // Control characters and the ones that reorder text become spaces: the
    // same as Settings' oneLine().
    function oneLine(s) {
        return String(s == null ? '' : s).replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g, ' ');
    }

    // The launch route's refusal ({detail: {error: "wake_refused", message}})
    // as one line, or "" for any other failure.
    async function refusalOf(res) {
        try {
            const body = await res.json();
            const d = body && body.detail;
            if (d && d.error === 'wake_refused' && d.message) return oneLine(d.message);
        } catch (_) { /* not JSON: not a refusal */ }
        return '';
    }

    function notify(title, message, level) {
        if (window.WindowManager && typeof window.WindowManager.notify === 'function') {
            window.WindowManager.notify(title, message, level);
        } else {
            console.warn(`[${level || 'info'}] ${title}: ${message}`);
        }
    }

    function escapeAttr(s) {
        return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    return { launch, frameOrigin };
})();

window.AppRunner = AppRunner;

// Host-driven app open (host-environment awareness): a first-party
// chat surface (Circles / the Workbench) relays the host's `open_app` tool call
// here as a postMessage so the host can open an app for the user. Same-origin
// only — a cross-origin isolated app can't force the shell to open windows.
window.addEventListener('message', (e) => {
    if (e.origin !== window.location.origin) return;
    const d = e.data;
    if (d && d.type === 'cseries:open-app' && typeof d.app_id === 'string' && d.app_id) {
        // Pass the deep-link view through (it was silently dropped before —
        // model-picker's view:'connections' never arrived), and the seed
        // prompt (same story: the Connect button's seed never
        // arrived either).
        const launchOpts = {};
        if (typeof d.view === 'string' && d.view) launchOpts.view = d.view;
        if (typeof d.prompt === 'string' && d.prompt.trim()) launchOpts.prompt = d.prompt;
        AppRunner.launch(d.app_id, Object.keys(launchOpts).length ? launchOpts : undefined);
    }
    // Artifact-bridge compose lane: the
    // Artifacts window emits pill/send references; forward them into the
    // Circles frame, whose composer renders the pill or sends as the user.
    // Same-origin only on BOTH hops — a cross-origin app can't speak here.
    if (d && d.type === 'cseries:compose' && typeof d.text === 'string' && d.text
        && d.text.length <= 2000 && (d.mode === 'pill' || d.mode === 'send')) {
        try {
            const target = document.querySelector('.app-frame-host[data-app-id="circles"] iframe');
            if (target && target.contentWindow) {
                target.contentWindow.postMessage(
                    { type: 'cseries:compose', mode: d.mode, text: d.text },
                    window.location.origin);
            }
        } catch (e) { /* circles not open — the viewer shows its own chip */ }
    }
});
