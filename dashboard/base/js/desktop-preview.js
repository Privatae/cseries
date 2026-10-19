/* ================================================================
 * DesktopPreview — show a desktop design without making it the desktop.
 *
 * A design document talks to the shell through `window.CSeriesDesktop`
 * (desktop-surface.js injects the real bridge). Mounted anywhere else — a
 * card on the Designs shelf, a full-size look before trying it — that object
 * does not exist, and the template's very first line (CSeriesDesktop.host)
 * throws, so the preview renders blank. This injects a STAND-IN with the same
 * shape and inert controls: the host's real app names (letter icons — the
 * frame has no network to fetch icons), its name, and a presence line, so the
 * preview shows the design as it would look on this machine.
 *
 * Inert on purpose: launch / openLauncher return false, nothing is posted to
 * the parent, and the frame stays under the shared sandbox CSP
 * (sandbox-csp.js) like every host-authored document.
 *
 * An exception thrown by a CSeriesDesktop.on subscriber is swallowed, as the
 * real bridge swallows it — a design whose draw() throws draws nothing — and
 * LOGGED to the console with its stack, through console.error and
 * Reflect.apply captured before any document script runs. The document can
 * neither see that nor change it (no global, nothing rethrown onto
 * window.onerror); the desktop render check (server/applications/
 * desktop_render.py) reads it from the browser's console over CDP and names
 * the line.
 *
 * Usage: DesktopPreview.instrument(html, {apps, host, phone}) → html
 * ================================================================ */
(function () {
    'use strict';

    // Same stand-in contract as desktop-surface.js BRIDGE_SRC's public
    // surface (apps, all, host, presence, phone, reducedMotion, mode, the bar's
    // ownsBar / dock / windows / desktopShown / machine, on, launch,
    // openLauncher, activate, focusWindow, toggleDesktop, help, openMachine, render) — a
    // design written against the real bridge must draw here without a single
    // branch of its own.
    function bridgeSource(state) {
        return '(function(){"use strict";'
            + 'var S=' + JSON.stringify(state).replace(/</g, '\\u003c') + ';S.ready=true;'
            + 'var AP=Reflect.apply,CE=console.error,CON=console;'
            + 'function L(e){try{AP(CE,CON,["cseries desktop: a CSeriesDesktop.on callback threw\\n"+String((e&&e.stack)||e)])}catch(_){}}'
            + 'function esc(s){return String(s==null?"":s).replace(/[&<>"\']/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","\'":"&#39;"}[c]})}'
            + 'function render(el){if(!el)return;el.innerHTML=S.apps.map(function(a){return \'<button class="sk-app" type="button" data-cseries-app="\'+esc(a.id)+\'"><span class="sk-app-icon">\'+(a.icon||"")+\'</span><span class="sk-app-label">\'+esc(a.name)+\'</span></button>\'}).join("")}'
            + 'function renderDock(el){el.innerHTML=S.dock.map(function(a){return \'<button class="sk-dock-item" type="button" data-cseries-activate="\'+esc(a.id)+\'" title="\'+esc(a.name)+\'"><span class="sk-dock-icon">\'+(a.icon||"")+\'</span></button>\'}).join("")}'
            + 'function each(sel,fn){var els=document.querySelectorAll(sel);for(var i=0;i<els.length;i++)fn(els[i])}'
            + 'function renderAll(){each("[data-cseries-apps]",render);each("[data-cseries-dock]",renderDock);each("[data-cseries-windows]",function(el){el.innerHTML=""})}'
            // Every control is inert in a preview: nothing reaches the shell.
            + 'window.addEventListener("click",function(e){var t=e.target&&e.target.closest?e.target.closest("[data-cseries-app],[data-cseries-activate],[data-cseries-window],[data-cseries-launcher],[data-cseries-show-desktop],[data-cseries-help],[data-cseries-machine]"):null;if(t)e.preventDefault()},true);'
            + 'window.addEventListener("contextmenu",function(e){e.preventDefault()},true);'
            + 'window.CSeriesDesktop={get apps(){return S.apps.slice()},get all(){return S.all.slice()},get host(){return S.host},get presence(){return S.presence},'
            + 'get phone(){return S.phone},get reducedMotion(){return S.reduced_motion},get mode(){return S.mode},'
            + 'get ownsBar(){return S.owns_bar},get dock(){return S.dock.slice()},get windows(){return S.windows.slice()},get desktopShown(){return false},get machine(){return S.machine},'
            + 'on:function(cb){if(typeof cb==="function"){try{cb(S)}catch(e){L(e)}}return function(){}},'
            + 'launch:function(){return false},openLauncher:function(){return false},activate:function(){return false},'
            + 'focusWindow:function(){return false},toggleDesktop:function(){return false},help:function(){return false},openMachine:function(){return false},render:render};'
            + 'if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",renderAll);else renderAll();'
            + '})();';
    }

    const CSS = '.sk-app{display:inline-flex;flex-direction:column;align-items:center;gap:6px;'
        + 'background:none;border:0;color:inherit;font:inherit;padding:8px;border-radius:10px}'
        + '.sk-app-icon{width:56px;height:56px;display:grid;place-items:center}'
        + '.sk-app-letter{font-size:22px;font-weight:600}'
        + '.sk-app-label{font-size:11px;letter-spacing:.08em;text-transform:uppercase;opacity:.85}';

    function barOf(html) {
        const m = /<meta[^>]+name\s*=\s*["']cseries-bar["'][^>]*content\s*=\s*["']([^"']*)["']|<meta[^>]+content\s*=\s*["']([^"']*)["'][^>]*name\s*=\s*["']cseries-bar["']/i
            .exec(String(html || '').slice(0, 65536));
        return !!(m && /^\s*(top|bottom|left|right)\s+[0-9]{2,3}\s*(px)?\s*$/i.test(m[1] || m[2] || ''));
    }

    function modeOf(html) {
        const m = /<meta[^>]+name\s*=\s*["']cseries-desktop["'][^>]*content\s*=\s*["'](desktop|wallpaper)["']|<meta[^>]+content\s*=\s*["'](desktop|wallpaper)["'][^>]*name\s*=\s*["']cseries-desktop["']/i
            .exec(String(html || '').slice(0, 65536));
        return m ? (m[1] || m[2]).toLowerCase() : 'wallpaper';
    }

    function letter(name) {
        const c = String(name || '?').trim().charAt(0).toUpperCase() || '?';
        return '<span class="sk-app-letter">' + c.replace(/[<>&"']/g, '') + '</span>';
    }

    /** html + a stand-in bridge placed before any document script (after a
     *  leading doctype, else at index 0 — the same rule as the real one). */
    function instrument(html, opts) {
        html = String(html || '');
        opts = opts || {};
        const mode = modeOf(html);
        const apps = (opts.apps || []).slice(0, 24).map((a) => ({
            id: String(a.id || ''), name: String(a.name || a.id || ''),
            description: '', icon: letter(a.name || a.id),
            running: false, focused: false,
        }));
        const desktop = mode === 'desktop';
        // A design that draws the bar is previewed WITH it: a dock of this
        // host's first apps (nothing running — it is a preview), no windows.
        const ownsBar = desktop && barOf(html) && !opts.phone;
        const state = {
            apps: desktop ? apps : [], all: desktop ? apps.map((a) => ({ id: a.id, name: a.name })) : [],
            host: String(opts.host || ''),
            presence: desktop ? (String(opts.host || 'Your Host') + ' is here') : '',
            phone: !!opts.phone, reduced_motion: !!opts.reducedMotion, mode,
            owns_bar: ownsBar,
            dock: ownsBar ? apps.slice(0, 6).map((a) => ({ id: a.id, name: a.name, icon: a.icon,
                pinned: true, running: false, focused: false })) : [],
            windows: [], desktop_shown: false, machine: '',
        };
        const head = '<style data-cseries-desktop>' + CSS + '</style>'
            + '<scr' + 'ipt data-cseries-desktop>' + bridgeSource(state) + '</scr' + 'ipt>';
        const d = /^[\t\n\f\r ]*<!doctype[^>]*>/i.exec(html);
        const i = d ? d[0].length : 0;
        return html.slice(0, i) + head + html.slice(i);
    }

    window.DesktopPreview = { instrument, modeOf };
})();
