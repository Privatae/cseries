/**
 * C-Series — the HTML desktop surface
 *
 * The desktop itself — the wallpaper AND, when the document asks for it, the
 * app icons — as ONE host-authored HTML document. The host designs it as an
 * ordinary artifact (Artifacts window: live render, tune mode, variants,
 * history) and points the desktop at it (`artifact` tool op='set_desktop' /
 * PUT /api/appearance/desktop). This module renders that document full-bleed
 * under the icon column, the window layer and the widget layer, and keeps it
 * live: every write the host makes re-renders the desktop while the user
 * watches. The dashboard's HTML design is isolated so a user can prompt the
 * host to create a whole new one.
 *
 * Two modes, declared by the document in STATIC markup
 * (`<meta name="cseries-desktop" content="wallpaper|desktop">`; default wallpaper):
 *   wallpaper — a live backdrop. The shell keeps drawing its own icon column
 *               and presence line on top.
 *   desktop   — the document draws the icons. The shell hides #desktop-icons
 *               and #presence-line; windows, widgets and the taskbar stay.
 *
 * SECURITY INVARIANT — the same wall as the Artifacts window: host-authored HTML never renders in a
 * privileged context. sandbox="allow-scripts" ONLY — opaque origin, no session
 * cookie, no token, no API reach, no top navigation, no popups, no forms.
 *
 * THE ONE DOOR is a private MessageChannel, not window.postMessage:
 *   - The shell injects a bridge script at the EARLIEST script position of the
 *     document (before any document script can run). The bridge creates a
 *     MessageChannel, keeps port1 in its closure, and hands port2 to the shell
 *     in a single window-level `desktop:hello`. The shell binds the FIRST hello
 *     per frame and never another; every other window-level message from the
 *     frame is dropped unread and counted.
 *   - Document scripts can post to the window all day — nothing listens. They
 *     cannot reach port1: the bridge sends through MessagePort.prototype.
 *     postMessage and reads through MessageEvent.prototype's `data` getter,
 *     both captured with Reflect.apply when it starts, before any document
 *     script exists. Patching either prototype later used to hand the patch
 *     the port (`this` on a send, `this.target` on a receive, both
 *     reproduced); now the bridge never touches them again. The bridge's own
 *     API (CSeriesDesktop.launch / openLauncher) only speaks when a TRUSTED
 *     pointer/key event happened in the frame within the last second
 *     (event.isTrusted cannot be forged by script), and the click /
 *     contextmenu / pointerdown / Escape relays fire only from inside trusted
 *     event handlers.
 *   - So "the user clicked inside the desktop" is a property the shell can
 *     rely on without depending on any browser's user-activation semantics
 *     (measured: navigator.userActivation.isActive reads true
 *     permanently under automation, so it was never a gate).
 *
 *   frame → shell (port): desktop:ready · desktop:launch {app_id} · desktop:menu {x,y,app_id?}
 *                         · desktop:launcher · desktop:pointerdown {machine?} · desktop:escape
 *                         · bar owner only: desktop:activate · desktop:window · desktop:show-desktop
 *                           · desktop:help · desktop:machine (the shell's own machine panel)
 *   frame → shell (window, once): desktop:hello [port]
 *   shell → frame (port): desktop:state {mode, apps, all, host, presence, phone, reduced_motion}
 *
 * This is the complete, closed list. Outbound carries
 * the host's display name and two viewport flags always, and — ONLY in
 * `desktop` mode, where the document draws the icons — the desktop's app set
 * (id / name / description / inline icon markup / running / focused), every
 * installed app's id+name, and the presence sentence. A `wallpaper` document
 * gets none of those: the shell draws the icon column and the presence line
 * itself, so handing them over was giving the frame an inventory it cannot use.
 * NEVER tokens, balance, account identity, memory. Inbound app ids must name an INSTALLED app; everything is size- and
 * rate-capped; launches are spaced.
 *
 * Load order: after icons.js (window.iconMarkup) and desktop-layout.js
 * (DesktopLayout), before desktop.js (applyWallpaper hands the appearance
 * payload to DesktopSurface.apply at call time).
 */
const DesktopSurface = (() => {
    const MSG_MAX_BYTES = 4096;
    const RATE_PER_SEC = 20;
    const RATE_PER_MIN = 400;
    const LAUNCH_MIN_GAP_MS = 300;
    const POLL_MS = 3000;          // same cadence as the Artifacts window's live poll
    const META_URL = '/api/appearance/desktop/meta';
    const LOOP_URL = '/api/appearance/desktop/loop';
    const ID_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
    // WindowManager ids: `win-<ms>-<rand>`, or an app runner's own id.
    const WIN_ID_RE = /^[A-Za-z0-9_.:-]{1,80}$/;
    const BAR_EDGES = ['top', 'bottom', 'left', 'right'];
    const BAR_MIN_PX = 28, BAR_MAX_PX = 120;   // desktop_contract.BAR_MIN_PX / BAR_MAX_PX

    const state = {
        frame: null,        // the live iframe (its private port hangs off it as _skPort)
        pending: null,      // the next document, fading in over the live one
        url: null,          // render URL of the mounted document (null = no surface)
        updatedAt: null,    // change signal from /meta
        mode: 'wallpaper',
        bar: null,          // {edge, size} when the document draws the bar (cseries-bar)
        appList: [],        // /api/applications
        installed: new Set(),
        iconCache: new Map(),   // icon_url → Promise<markup>
        host: '',
        lastLaunch: 0,
        stampsSec: [], stampsMin: [],
        dropped: 0,
        pollTimer: null,
        postTimer: null,
    };

    // ── The bridge the SHELL injects into the document ───────────────────
    // Shell-authored, not document-authored (the Artifacts window's TUNE_SCRIPT
    // pattern). Runs before any document script (see injectionIndex). Gives the
    // document `window.CSeriesDesktop`, auto-fills any `[data-cseries-apps]`
    // container with a default icon grid, and turns TRUSTED clicks on
    // `[data-cseries-app]` / right-clicks / Escape into port messages.
    //
    // The default renderers (render / renderDock / renderWindows) bring a
    // container to the new state IN PLACE (fill): a control that is still
    // wanted stays the same element and only its class and label change. The
    // shell re-posts state after every window event, and a button replaced
    // between its press and its release loses the click. fill() matches
    // controls by their first data-cseries-* attribute (kind and value), moves
    // the wanted nodes in rather than re-parsing text, and replaces anything
    // that is no control (text, an element without one) as innerHTML did.
    const BRIDGE_SRC = `(function(){"use strict";
var P=window.parent,now=performance.now.bind(performance),ch=new MessageChannel(),port=ch.port1,AP=Reflect.apply,PM=MessagePort.prototype.postMessage,DG=Object.getOwnPropertyDescriptor(MessageEvent.prototype,"data").get;
var S={apps:[],all:[],host:"",presence:"",phone:false,reduced_motion:false,mode:"wallpaper",owns_bar:false,dock:[],windows:[],desktop_shown:false,machine:"",ready:false};
var subs=[],last=-1e9,GESTURE_MS=1000,net=null,barNet=null,barWatch=null,barRead=false,barMissing=false;
function gesture(){return now()-last<GESTURE_MS}
function send(m){try{AP(PM,port,[m])}catch(_){}}
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
function near(t,a){return t&&t.closest?t.closest("["+a+"]"):null}
function appOf(t){return near(t,"data-cseries-app")}
function idOf(el){return String(el.getAttribute("data-cseries-app")||"").slice(0,64)}
function keyOf(n){if(n.nodeType!==1)return "";for(var i=0;i<n.attributes.length;i++)if(n.attributes[i].name.indexOf("data-cseries-")===0)return n.nodeName+" "+n.attributes[i].name+"="+n.attributes[i].value;return ""}
function mend(have,want){var i,a,h,w;if(want.nodeType!==1){if(have.nodeValue!==want.nodeValue)have.nodeValue=want.nodeValue;return}
for(i=0;i<want.attributes.length;i++){a=want.attributes[i];if(have.getAttribute(a.name)!==a.value)have.setAttribute(a.name,a.value)}
for(i=have.attributes.length-1;i>=0;i--)if(!want.hasAttribute(have.attributes[i].name))have.removeAttribute(have.attributes[i].name);
h=Array.prototype.slice.call(have.childNodes);w=Array.prototype.slice.call(want.childNodes);
for(i=0;i<w.length;i++){if(i>=h.length)have.appendChild(w[i]);else if(h[i].nodeName===w[i].nodeName&&h[i].namespaceURI===w[i].namespaceURI)mend(h[i],w[i]);else have.replaceChild(w[i],h[i])}
for(i=w.length;i<h.length;i++)have.removeChild(h[i])}
function fill(box,html){var t=document.createElement("template"),kept={},order=[],i,k,el;t.innerHTML=html;
var next=Array.prototype.slice.call(t.content.childNodes),now=Array.prototype.slice.call(box.childNodes);
for(i=0;i<next.length;i++)order.push(keyOf(next[i]));
for(i=0;i<now.length;i++){k=keyOf(now[i]);if(k&&order.indexOf(k)>=0&&!kept[k])kept[k]=now[i];else box.removeChild(now[i])}
for(i=0;i<next.length;i++){el=order[i]&&kept[order[i]];if(el){delete kept[order[i]];mend(el,next[i])}else el=next[i];if(box.childNodes[i]!==el)box.insertBefore(el,box.childNodes[i]||null)}}
function render(el){if(!el)return;fill(el,S.apps.map(function(a){return '<button class="sk-app'+(a.running?' is-running':'')+(a.focused?' is-focused':'')+'" type="button" data-cseries-app="'+esc(a.id)+'" title="'+esc(a.description||a.name)+'"><span class="sk-app-icon">'+(a.icon||'')+'</span><span class="sk-app-label">'+esc(a.name)+'</span></button>'}).join(""))}
function renderDock(el){fill(el,S.dock.map(function(a){return '<button class="sk-dock-item'+(a.running?' is-running':'')+(a.focused?' is-focused':'')+'" type="button" data-cseries-activate="'+esc(a.id)+'" title="'+esc(a.name)+'"><span class="sk-dock-icon">'+(a.icon||'')+'</span></button>'}).join(""))}
function renderWindows(el){fill(el,S.windows.map(function(w){return '<button class="sk-window'+(w.focused?' is-focused':'')+(w.minimized?' is-minimized':'')+'" type="button" data-cseries-window="'+esc(w.id)+'" title="'+esc(w.title)+'"><span class="sk-window-icon">'+(w.icon||'')+'</span><span class="sk-window-title">'+esc(w.title)+'</span></button>'}).join(""))}
function each(sel,fn){var els=document.querySelectorAll(sel);for(var i=0;i<els.length;i++)fn(els[i])}
function renderAll(){each("[data-cseries-apps]",render);each("[data-cseries-dock]",renderDock);each("[data-cseries-windows]",renderWindows)}
port.onmessage=function(e){var d=AP(DG,e,[]);if(!d||d.type!=="desktop:state")return;
S.apps=Array.isArray(d.apps)?d.apps:[];S.all=Array.isArray(d.all)?d.all:[];S.host=String(d.host||"");S.presence=String(d.presence||"");S.phone=!!d.phone;S.reduced_motion=!!d.reduced_motion;S.mode=String(d.mode||"wallpaper");
S.owns_bar=!!d.owns_bar;S.dock=Array.isArray(d.dock)?d.dock:[];S.windows=Array.isArray(d.windows)?d.windows:[];S.desktop_shown=!!d.desktop_shown;S.machine=String(d.machine||"");S.ready=true;
renderAll();for(var i=0;i<subs.length;i++){try{subs[i](S)}catch(_){}}try{document.dispatchEvent(new CustomEvent("cseries:desktop",{detail:S}))}catch(_){}
safetyNet();barSafetyNet();};
function barDrawn(){return barRead||!!document.querySelector("[data-cseries-dock],[data-cseries-activate],[data-cseries-window]")}
function barPresent(){if(barMissing&&barDrawn()){barMissing=false;clearInterval(barWatch);send({type:"desktop:bar-present"})}}
function barSafetyNet(){clearTimeout(barNet);
if(barMissing){barPresent();return}
if(!S.owns_bar||!(S.dock.length||S.windows.length))return;
barNet=setTimeout(function(){if(barDrawn())return;barMissing=true;
send({type:"desktop:bar-missing"});try{console.warn("cseries desktop: the document declared cseries-bar but drew no dock or window list — the shell took its taskbar back")}catch(_){}
clearInterval(barWatch);barWatch=setInterval(barPresent,1000)},2000)}
function safetyNet(){clearTimeout(net);if(S.mode!=="desktop"||!S.apps.length)return;
net=setTimeout(function(){if(document.querySelector("[data-cseries-app]"))return;
var g=document.createElement("div");g.setAttribute("data-cseries-apps","");g.setAttribute("data-cseries-net","");
g.style.cssText="position:fixed;left:16px;top:16px;z-index:2147483647;display:grid;grid-template-columns:repeat(auto-fill,72px);gap:8px;max-width:40vw;padding:8px;border-radius:12px;background:rgba(0,0,0,.35)";
(document.body||document.documentElement).appendChild(g);render(g);
try{console.warn("cseries desktop: the document drew no launchable app after the app list arrived — the shell added a grid so the machine stays usable")}catch(_){}},1500)}
window.addEventListener("pointerdown",function(e){if(!e.isTrusted)return;last=now();send({type:"desktop:pointerdown",machine:!!near(e.target,"data-cseries-machine")})},true);
window.addEventListener("keydown",function(e){if(!e.isTrusted)return;last=now();if(e.key==="Escape")send({type:"desktop:escape"})},true);
function attr(el,a){return String(el.getAttribute(a)||"").slice(0,80)}
window.addEventListener("click",function(e){if(!e.isTrusted)return;last=now();var t=e.target,x;
if((x=near(t,"data-cseries-window"))){e.preventDefault();send({type:"desktop:window",win_id:attr(x,"data-cseries-window"),action:"toggle"});return}
if((x=near(t,"data-cseries-activate"))){e.preventDefault();send({type:"desktop:activate",app_id:attr(x,"data-cseries-activate").slice(0,64)});return}
if(near(t,"data-cseries-launcher")){e.preventDefault();send({type:"desktop:launcher"});return}
if(near(t,"data-cseries-show-desktop")){e.preventDefault();send({type:"desktop:show-desktop"});return}
if(near(t,"data-cseries-help")){e.preventDefault();send({type:"desktop:help"});return}
if(near(t,"data-cseries-machine")){e.preventDefault();send({type:"desktop:machine"});return}
x=appOf(t);if(!x)return;e.preventDefault();send({type:"desktop:launch",app_id:idOf(x)})},true);
window.addEventListener("contextmenu",function(e){if(!e.isTrusted)return;e.preventDefault();last=now();var t=e.target,d=near(t,"data-cseries-activate"),a=appOf(t),m={type:"desktop:menu",x:e.clientX,y:e.clientY};if(d){m.app_id=attr(d,"data-cseries-activate").slice(0,64);m.dock=true}else if(a)m.app_id=idOf(a);send(m)},true);
window.CSeriesDesktop={get apps(){return S.apps.slice()},get all(){return S.all.slice()},get host(){return S.host},get presence(){return S.presence},get phone(){return S.phone},get reducedMotion(){return S.reduced_motion},get mode(){return S.mode},
get ownsBar(){return S.owns_bar},get dock(){barRead=true;return S.dock.slice()},get windows(){barRead=true;return S.windows.slice()},get desktopShown(){return S.desktop_shown},get machine(){return S.machine},
activate:function(id){if(!gesture())return false;send({type:"desktop:activate",app_id:String(id||"").slice(0,64)});return true},
focusWindow:function(id){if(!gesture())return false;send({type:"desktop:window",win_id:String(id||"").slice(0,80),action:"toggle"});return true},
toggleDesktop:function(){if(!gesture())return false;send({type:"desktop:show-desktop"});return true},
help:function(){if(!gesture())return false;send({type:"desktop:help"});return true},
openMachine:function(){if(!gesture())return false;send({type:"desktop:machine"});return true},
on:function(cb){if(typeof cb!=="function")return function(){};subs.push(cb);if(S.ready){try{cb(S)}catch(_){}}return function(){var i=subs.indexOf(cb);if(i>=0)subs.splice(i,1)}},
launch:function(id){if(!gesture())return false;send({type:"desktop:launch",app_id:String(id||"").slice(0,64)});return true},
openLauncher:function(){if(!gesture())return false;send({type:"desktop:launcher"});return true},render:render};
function ready(){renderAll();send({type:"desktop:ready"})}
try{P.postMessage({type:"desktop:hello"},"*",[ch.port2])}catch(_){}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",ready);else ready();
})();`;
    // Baseline for the default grid — enough to be usable unstyled; every rule
    // is one the document can override (it loads first, so document CSS wins).
    const BRIDGE_CSS = '.sk-app{display:inline-flex;flex-direction:column;align-items:center;gap:6px;'
        + 'background:none;border:0;color:inherit;font:inherit;cursor:pointer;padding:8px;border-radius:10px}'
        + '.sk-app-icon{width:56px;height:56px;display:grid;place-items:center}'
        + '.sk-app-icon svg{width:40px;height:40px}.sk-app-icon img{width:40px;height:40px;object-fit:contain}'
        + '.sk-app-letter{font-size:22px;font-weight:600}'
        + '.sk-app-label{font-size:11px;letter-spacing:.08em;text-transform:uppercase;opacity:.85}'
        // The bar's default renderers (data-cseries-dock / data-cseries-windows).
        + '.sk-dock-item,.sk-window{display:inline-flex;align-items:center;gap:6px;background:none;border:0;'
        + 'color:inherit;font:inherit;cursor:pointer;padding:4px 6px;border-radius:8px;position:relative}'
        + '.sk-dock-icon,.sk-window-icon{width:28px;height:28px;display:grid;place-items:center}'
        + '.sk-dock-icon svg,.sk-dock-icon img,.sk-window-icon svg,.sk-window-icon img{width:22px;height:22px}'
        + '.sk-dock-item.is-running::after{content:"";position:absolute;left:50%;bottom:0;width:4px;height:4px;'
        + 'margin-left:-2px;border-radius:50%;background:currentColor}'
        + '.sk-window.is-minimized{opacity:.6}';
    const BRIDGE_HEAD = '<style data-cseries-desktop>' + BRIDGE_CSS + '</style>'
        + '<scr' + 'ipt data-cseries-desktop>' + BRIDGE_SRC + '</scr' + 'ipt>';

    function detectMode(html) {
        // Static markup only — a script adding the meta later changes nothing.
        const head = String(html).slice(0, 65536);
        const m = /<meta\s+(?:[^>]*?\s)?name=["']cseries-desktop["'][^>]*?content=["'](wallpaper|desktop)["']/i.exec(head)
            || /<meta\s+(?:[^>]*?\s)?content=["'](wallpaper|desktop)["'][^>]*?name=["']cseries-desktop["']/i.exec(head);
        return m ? m[1].toLowerCase() : 'wallpaper';
    }

    /** The bar a document declares (`<meta name="cseries-bar" content="bottom
     *  48">`) — same static-head rule and bounds as desktop_contract.parse_bar.
     *  Anything malformed reads as "no bar": the shell keeps its own taskbar,
     *  which is the safe side (set_desktop refuses the malformed tag anyway). */
    function detectBar(html) {
        const head = String(html).slice(0, 65536);
        const m = /<meta\s+(?:[^>]*?\s)?name=["']cseries-bar["'][^>]*?content=["']([^"']*)["']/i.exec(head)
            || /<meta\s+(?:[^>]*?\s)?content=["']([^"']*)["'][^>]*?name=["']cseries-bar["']/i.exec(head);
        if (!m) return null;
        const v = /^\s*(top|bottom|left|right)\s+([0-9]{2,3})\s*(?:px)?\s*$/i.exec(m[1]);
        if (!v) return null;
        const size = parseInt(v[2], 10);
        if (!(size >= BAR_MIN_PX && size <= BAR_MAX_PX)) return null;
        return { edge: v[1].toLowerCase(), size };
    }

    // ── Design tokens: the desktop recolours the shell and every app ─────
    //
    // `<meta name="cseries-tokens" content="--cseries-ink:#…; --cseries-ember:#…">`
    // in the document's static head. The kernel parses the same tag with the
    // same grammar (server/applications/desktop_tokens.py) and appends the
    // pairs to /css/tokens.css, so every app picks them up when it loads;
    // this mirror applies them LIVE to the shell and to the app frames the
    // shell can reach (same-origin standard apps), so a host's write shows
    // everywhere within the poll, not on the next reload. Whitelist grammar:
    // names --cseries-*, values colours / numbers / font stacks, nothing that
    // reaches out. Mismatch between here and the kernel = the kernel wins on
    // reload; keep the two in step.
    const TOKEN_NAME_RE = /^--cseries-[a-z0-9-]{1,40}$/;
    const TOKEN_VALUE_RE = /^(?:#[0-9a-f]{3,8}|(?:rgba?|hsla?)\(\s*[0-9.%\s,/-]+\)|-?[0-9]*\.?[0-9]+(?:px|em|rem|%|ms|s)?|(?:"[A-Za-z0-9 -]{1,40}"|'[A-Za-z0-9 -]{1,40}'|[A-Za-z][A-Za-z0-9-]{0,40})(?:\s*,\s*(?:"[A-Za-z0-9 -]{1,40}"|'[A-Za-z0-9 -]{1,40}'|[A-Za-z][A-Za-z0-9-]{0,40}))*)$/i;
    const TOKEN_FORBIDDEN = ['url', 'var(', 'expression', '\\', '{', '}', '<', '>', '@', '/*', '//'];
    const TOKEN_MAX = 64;
    let appliedTokens = [];   // names currently set on the shell root

    function extractTokens(html) {
        const head = String(html).slice(0, 65536);
        // The closing quote is a backreference: the other quote may sit inside
        // (a font stack like 'Iowan Old Style' in a double-quoted attribute).
        const m = /<meta\s+(?:[^>]*?\s)?name=["']cseries-tokens["'][^>]*?content=(["'])([\s\S]*?)\1/i.exec(head)
            || /<meta\s+(?:[^>]*?\s)?content=(["'])([\s\S]*?)\1[^>]*?name=["']cseries-tokens["']/i.exec(head);
        const out = {};
        if (!m) return out;
        let n = 0;
        for (const decl of String(m[2] || '').split(';')) {
            const i = decl.indexOf(':');
            if (i < 0) continue;
            const name = decl.slice(0, i).trim().toLowerCase();
            const value = decl.slice(i + 1).trim().split(/\s+/).join(' ');
            if (!TOKEN_NAME_RE.test(name) || !value || value.length > 120) continue;
            const low = value.toLowerCase();
            if (TOKEN_FORBIDDEN.some((f) => low.includes(f)) || !TOKEN_VALUE_RE.test(value)) continue;
            out[name] = value;
            if (++n >= TOKEN_MAX) break;
        }
        return out;
    }

    function appFrameRoots() {
        const roots = [];
        document.querySelectorAll('.app-frame-host iframe').forEach((f) => {
            try {
                const d = f.contentDocument;      // throws / null when cross-origin
                if (d && d.documentElement) roots.push(d.documentElement);
            } catch (_) { /* isolated app — it reloads /css/tokens.css on its own */ }
        });
        return roots;
    }

    function applyTokens(tokens) {
        const roots = [document.documentElement].concat(appFrameRoots());
        const names = Object.keys(tokens || {});
        for (const root of roots) {
            for (const n of appliedTokens) if (!names.includes(n)) root.style.removeProperty(n);
            for (const n of names) root.style.setProperty(n, tokens[n]);
        }
        appliedTokens = names;
        refreshTokenSheets(roots);
        document.dispatchEvent(new CustomEvent('cseries:design-tokens', { detail: { tokens: tokens || {} } }));
    }

    // The inline properties above win while they are set; a REMOVED token
    // falls back to the stylesheet, and the shell's /css/tokens.css was
    // fetched at boot with whatever override was live then. Re-fetch it (a
    // cache-busted swap of the same <link>) on every change so a dropped
    // token, or a swatch change, reverts without a reload — same for the
    // app frames the shell can reach.
    function refreshTokenSheets(roots) {
        const stamp = 'v=' + Date.now();
        for (const root of roots) {
            let doc;
            try { doc = root.ownerDocument; } catch (_) { continue; }
            if (!doc) continue;
            doc.querySelectorAll('link[rel="stylesheet"]').forEach((link) => {
                const href = link.getAttribute('href') || '';
                if (!/^\/css\/tokens\.css(\?|$)/.test(href)) return;
                const fresh = link.cloneNode(false);
                fresh.setAttribute('href', '/css/tokens.css?' + stamp);
                fresh.addEventListener('load', () => { try { link.remove(); } catch (_) {} });
                fresh.addEventListener('error', () => { try { fresh.remove(); } catch (_) {} });
                link.parentNode.insertBefore(fresh, link.nextSibling);
            });
        }
    }

    function clearTokens() {
        if (appliedTokens.length) applyTokens({});
    }

    /**
     * Where the bridge goes: immediately after a leading <!DOCTYPE …>, else
     * at index 0 — NOTHING of the document's can precede it, so the bridge
     * always runs first and owns the channel before any document code exists.
     * (An earlier "before the first <script>, no later than <head>" search
     * could be steered into RCDATA — `<title>x<script>` before <head> — and
     * the bridge landed as title text.) The
     * parser handles a <style>/<script> ahead of <html>/<head> exactly as it
     * handles one inside <head>: both land in the head element, and the
     * document's own <html>/<head> tags are merged / ignored, never lost.
     * srcdoc is parsed from a string, so nothing depends on <meta charset>
     * position.
     */
    function injectionIndex(html) {
        const d = /^[\t\n\f\r ]*<!doctype[^>]*>/i.exec(html);
        return d ? d[0].length : 0;
    }

    function instrument(html) {
        html = String(html);
        const i = injectionIndex(html);
        return html.slice(0, i) + BRIDGE_HEAD + html.slice(i);
    }

    // ── Mount / swap / clear ─────────────────────────────────────────────

    function desktopEl() { return document.getElementById('desktop'); }

    function isPhone() {
        return !!(window.CSeriesViewport && window.CSeriesViewport.isPhone());
    }

    /** Whether the LIVE document draws the bar right now: it declared one,
     *  it is a desktop, and this is not a phone (a phone keeps the shell's
     *  taskbar — windows fill the screen there, and it is the way back). */
    function barOwned() {
        const f = state.frame;
        return !!(f && f._skBar && !f._skBarFailed && f._skMode === 'desktop' && !isPhone());
    }

    function setClasses() {
        const desk = desktopEl();
        if (!desk) return;
        const has = !!(state.frame || state.pending);
        desk.classList.toggle('has-surface', has);
        desk.classList.toggle('surface-desktop', has && state.mode === 'desktop');
        desk.classList.toggle('surface-wallpaper', has && state.mode !== 'desktop');
        // THE BAR HAND-OVER: the shell's
        // #taskbar steps aside and the window layer keeps out of the strip the
        // document declared, so a maximized window never covers the design's
        // bar. The document itself stays full-screen underneath.
        const owned = barOwned();
        const root = document.documentElement;
        const was = document.body.classList.contains('design-bar');
        document.body.classList.toggle('design-bar', owned);
        for (const edge of BAR_EDGES) {
            const on = owned && state.frame._skBar.edge === edge;
            document.body.classList.toggle('design-bar-' + edge, on);
            root.style.setProperty('--design-bar-' + edge, (on ? state.frame._skBar.size : 0) + 'px');
        }
        // Things the taskbar used to hold (minimized widget chips) re-home.
        if (was !== owned) {
            document.dispatchEvent(new CustomEvent('cseries:design-bar', { detail: { owned } }));
        }
    }

    /**
     * The one entry point — desktop.js hands over the /api/appearance payload
     * on boot, on every Settings change, on tab wake and on viewport change.
     * The effective wallpaper key for this viewport decides: 'html' + a
     * pointer → mount; anything else → clear.
     */
    function apply(data) {
        try {
            data = data || {};
            const key = (isPhone() && data.wallpaper_mobile) ? data.wallpaper_mobile : data.wallpaper;
            if (key === 'html' && data.desktop_html_url) {
                mount(String(data.desktop_html_url), data.desktop_updated_at || null);
            } else {
                clear();
            }
        } catch (_) { /* the surface is decorative — never break the desktop */ }
    }

    async function mount(url, updatedAt) {
        if (state.frame && state.url === url && state.updatedAt === updatedAt) {
            setClasses();
            return;
        }
        state.url = url;
        state.updatedAt = updatedAt;
        let html;
        try {
            const r = await fetch(url, { cache: 'no-store', credentials: 'same-origin' });
            if (r.status === 404) {
                // No pointer any more — or a pointer with nothing that passed
                // the render check to show. The meta still answers in the
                // second case, and says why.
                clear();
                fetch(META_URL, { cache: 'no-store', credentials: 'same-origin' })
                    .then((m) => (m.ok ? m.json() : null))
                    .then((m) => pinNotice(m && m.pin))
                    .catch(() => {});
                return;
            }
            if (!r.ok) throw new Error(`desktop → ${r.status}`);
            html = await r.text();
        } catch (e) {
            // A broken entry mid-iteration (or a blip): keep whatever is
            // showing — the previous document or the default wallpaper — and
            // keep POLLING, so the host's next write lands without a tab
            // switch. Forget the stamp so that write is seen as a change.
            console.debug('desktop-surface: render unavailable, keeping the current surface', e);
            state.updatedAt = null;
            setClasses();
            startPoll();
            return;
        }
        // Superseded while fetching (a newer mount, or a newer stamp from the
        // poll) — an older document must never become live over a newer one.
        if (state.url !== url || state.updatedAt !== updatedAt) return;
        state.mode = detectMode(html);
        state.bar = detectBar(html);
        applyTokens(extractTokens(html));
        swapFrame(instrument(html));
        setClasses();
        startPoll();
    }

    function dropFrame(f) {
        if (!f) return;
        try { if (f._skPort) { f._skPort.onmessage = null; f._skPort.close(); } } catch (_) {}
        f._skPort = null;
        try { f.remove(); } catch (_) {}
    }

    // The shared sandbox policy (js/sandbox-csp.js). Named `sealed` in every
    // surface that mounts host-authored HTML, so the same thing reads the same
    // way in all four.
    function sealed(html) {
        // FAIL CLOSED. If /js/sandbox-csp.js did not load, the policy that keeps
        // a host-authored document off the network is simply absent — and the
        // frame looks identical either way, so nobody would notice. This is not
        // hypothetical: app bundles and the kernel image roll independently, and
        // measured on a live pod, it served /js/qrcode.js 200 while
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

    function swapFrame(html) {
        const desk = desktopEl();
        if (!desk) return;
        const f = document.createElement('iframe');
        f.className = 'desktop-surface';
        f.title = 'Desktop';
        // allow-scripts ONLY. See the module comment — this attribute is the wall.
        f.setAttribute('sandbox', 'allow-scripts');
        f.setAttribute('referrerpolicy', 'no-referrer');
        // The mode of THIS document, stamped on the frame. _postState scopes by
        // this and not by state.mode, because during a crossfade the two differ:
        // state.mode is already the incoming document's while the OLD frame is
        // still live and still holding an open port for ~400 ms. A window event
        // or an app install landing in that window would otherwise hand the
        // outgoing wallpaper the whole app inventory — the exact thing the
        // scoping below withholds.
        f._skMode = state.mode;
        // The bar THIS document declared, stamped like the mode: during a
        // crossfade the outgoing frame must keep its own answer.
        f._skBar = state.mode === 'desktop' ? state.bar : null;
        f._skPort = null;
        // The sandbox attribute is NOT the whole wall — it gives the frame an
        // opaque origin but leaves the network open (measured: fetch and
        // Image.src both reached an external server from a CSP-less srcdoc
        // frame with exactly this sandbox value). The desktop receives the
        // user's installed-app set, their host's name and its presence line
        // over the bridge, so that lane had to close. js/sandbox-csp.js holds
        // the one definition every host-authored surface uses.
        f.srcdoc = sealed(html);
        const prev = state.frame;
        if (state.pending) { dropFrame(state.pending); state.pending = null; }
        state.pending = f;
        // Above the live one (later sibling) so the new document fades in over
        // the old; the old is removed once the new one has painted.
        desk.insertBefore(f, prev ? prev.nextSibling : desk.firstChild);
        // The bridge's hello finishes the swap; load + a beat is the fallback
        // for a document that somehow never ran it (it still paints, inert).
        f.addEventListener('load', () => setTimeout(() => finishSwap(f), 120));
        // The render check cannot see a document that reloads itself slower
        // than its own few seconds, and in the shell such a loop flickers
        // forever. This frame's `load` events are counted: more than
        // LOOP_MAX_LOADS in LOOP_WINDOW_MS and the desktop is taken off
        // (server: desktop_pin.take_off_reloading) — the page cannot
        // suppress the event, and a hash change alone fires none.
        f._skLoads = [];
        f.addEventListener('load', () => noteLoad(f));
    }

    // 3 reloads after the first load in 45 s is a loop: a 10 s loop shows its
    // fourth load at ~30 s, and nothing a person writes reloads that often.
    const LOOP_MAX_LOADS = 3;
    const LOOP_WINDOW_MS = 45000;

    function noteLoad(f) {
        const now = Date.now();
        f._skLoads = (f._skLoads || []).filter((t) => now - t <= LOOP_WINDOW_MS);
        f._skLoads.push(now);
        if (f._skLoads.length <= LOOP_MAX_LOADS || f._skLoopReported) return;
        f._skLoopReported = true;
        fetch(LOOP_URL, {
            method: 'POST', cache: 'no-store', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            // The stamp says WHICH version loops, so a late report cannot
            // take off a newer one.
            body: JSON.stringify({ updated_at: state.updatedAt }),
        }).then((r) => (r.ok ? r.json() : null)).then((j) => {
            // Refused as too early (the version has not been worn long
            // enough by the server's clock): the next load reports again.
            if (j && j.taken_off === false) f._skLoopReported = false;
            return pollTick();
        }).catch(() => { f._skLoopReported = false; });
    }

    function finishSwap(f) {
        if (state.pending !== f) return;
        state.pending = null;
        const prev = state.frame;
        state.frame = f;
        f.classList.add('is-live');
        if (prev && prev !== f) {
            // Let the crossfade land before the old document goes.
            setTimeout(() => dropFrame(prev), 400);
        }
        setClasses();
        postState();
    }

    function clear() {
        stopPoll();
        clearTokens();
        state.url = null;
        state.updatedAt = null;
        state.mode = 'wallpaper';
        state.bar = null;
        if (state.pending) { dropFrame(state.pending); state.pending = null; }
        if (state.frame) { dropFrame(state.frame); state.frame = null; }
        setClasses();
    }

    // ── Live: the render moves when the host writes ──────────────────────

    function startPoll() {
        if (state.pollTimer) return;
        state.pollTimer = setInterval(pollTick, POLL_MS);
    }

    function stopPoll() {
        if (state.pollTimer) clearInterval(state.pollTimer);
        state.pollTimer = null;
    }

    // What the worn desktop's pin last did (server/applications/desktop_pin.py
    // status): held at an older version because the newest edit does not
    // render, worn WITHOUT a render check, or nothing that rendered to show.
    // A notice, once per change — remembered across reloads so it is news,
    // not a nag.
    const PIN_NOTICE_KEY = 'cseries.desktopPinNotice';
    function pinNotice(pin) {
        if (!pin || !pin.message || pin.state === 'current') return;
        const key = String(pin.at || '') + '|' + String(pin.state);
        let seen = null;
        try { seen = localStorage.getItem(PIN_NOTICE_KEY); } catch (_) { /* private mode */ }
        if (seen === key) return;
        try { localStorage.setItem(PIN_NOTICE_KEY, key); } catch (_) { /* private mode */ }
        const wm = window.WindowManager;
        if (wm && typeof wm.notify === 'function') {
            wm.notify('Desktop', String(pin.message).slice(0, 600),
                pin.state === 'unchecked' ? 'info' : 'warning');
        }
    }

    async function pollTick() {
        if (!state.url || document.visibilityState !== 'visible') return;
        try {
            const r = await fetch(META_URL, { cache: 'no-store', credentials: 'same-origin' });
            if (r.status === 404) { clear(); return; }   // pointer cleared elsewhere
            if (!r.ok) return;
            const m = await r.json();
            pinNotice(m && m.pin);
            const ts = m && m.updated_at ? String(m.updated_at) : null;
            if (ts && ts !== state.updatedAt) {
                await mount(state.url, ts);
            }
        } catch (_) { /* transient — next tick */ }
    }

    // ── State the shell shares with the document ─────────────────────────

    async function loadApps() {
        try {
            const res = await fetch('/api/applications');
            if (!res.ok) throw new Error('unavailable');
            const data = await res.json();
            state.appList = data.applications || [];
            state.installed = new Set(state.appList.map((a) => a.id));
        } catch (_) { /* keep the previous list */ }
        postState();
    }

    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s || '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    // ── Icon markup: ONE fixed, safe form ─────────────────────────────────
    // The document inserts an app's `icon` as markup (a design does
    // `(a.icon || "")` into its own HTML), and what feeds it is not all ours:
    // an image icon is a URL from the app's manifest, fetched here, and an app
    // that serves its own icon (a process sidecar) also chooses the response's
    // Content-Type, which used to become part of the <img> tag. So every icon
    // is rebuilt here, from an allowlist, whatever its source: only the shapes
    // below, only attributes whose values pass a strict pattern, text escaped,
    // anything else (a script, an event handler, an iframe, a style, a link)
    // dropped with its subtree. What the document receives cannot carry code.
    const ICON_NUMS = /^[0-9eE.,+\s-]{1,40}$/;
    const ICON_PAINT = /^(none|currentColor|#[0-9a-fA-F]{3,8}|var\(--[A-Za-z0-9-]{1,40}(, ?#[0-9a-fA-F]{3,8})?\))$/;
    const ICON_PATH = /^[MmLlHhVvCcSsQqTtAaZz0-9eE.,+\s-]{0,4000}$/;
    const ICON_IMG_SRC = /^data:image\/(png|jpeg|gif|webp|avif|svg\+xml|x-icon|vnd\.microsoft\.icon);base64,[A-Za-z0-9+/]+={0,2}$/;
    const ICON_ATTRS = {
        class: /^[A-Za-z0-9_ -]{0,60}$/, 'aria-hidden': /^true$/,
        viewBox: ICON_NUMS, width: ICON_NUMS, height: ICON_NUMS, x: ICON_NUMS, y: ICON_NUMS,
        x1: ICON_NUMS, y1: ICON_NUMS, x2: ICON_NUMS, y2: ICON_NUMS, cx: ICON_NUMS, cy: ICON_NUMS,
        r: ICON_NUMS, rx: ICON_NUMS, ry: ICON_NUMS, points: ICON_NUMS, d: ICON_PATH,
        fill: ICON_PAINT, stroke: ICON_PAINT, 'stroke-width': ICON_NUMS, opacity: ICON_NUMS,
        'fill-opacity': ICON_NUMS, 'stroke-opacity': ICON_NUMS,
        'stroke-linecap': /^(round|butt|square)$/, 'stroke-linejoin': /^(round|miter|bevel)$/,
        'fill-rule': /^(evenodd|nonzero)$/, 'clip-rule': /^(evenodd|nonzero)$/,
        src: ICON_IMG_SRC,
    };
    // element → the attributes it may carry (an attribute on another element is dropped)
    const ICON_TAGS = {
        svg: ['class', 'aria-hidden', 'viewBox', 'width', 'height', 'fill', 'stroke', 'stroke-width',
              'stroke-linecap', 'stroke-linejoin'],
        g: ['fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'opacity'],
        path: ['d', 'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'opacity',
               'fill-opacity', 'stroke-opacity', 'fill-rule', 'clip-rule'],
        circle: ['cx', 'cy', 'r', 'fill', 'stroke', 'stroke-width', 'opacity', 'fill-opacity', 'stroke-opacity'],
        ellipse: ['cx', 'cy', 'rx', 'ry', 'fill', 'stroke', 'stroke-width', 'opacity', 'fill-opacity', 'stroke-opacity'],
        rect: ['x', 'y', 'width', 'height', 'rx', 'ry', 'fill', 'stroke', 'stroke-width', 'opacity',
               'fill-opacity', 'stroke-opacity'],
        line: ['x1', 'y1', 'x2', 'y2', 'stroke', 'stroke-width', 'stroke-linecap', 'opacity'],
        polyline: ['points', 'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'opacity'],
        polygon: ['points', 'fill', 'stroke', 'stroke-width', 'stroke-linejoin', 'opacity', 'fill-opacity'],
        span: ['class'],
        img: ['src'],
    };
    function iconNode(n, inSpan) {
        if (n.nodeType === 3) return inSpan ? esc(n.nodeValue) : '';
        if (n.nodeType !== 1 || !Object.prototype.hasOwnProperty.call(ICON_TAGS, n.localName)) return '';
        const tag = n.localName;
        let attrs = '';
        for (const a of Array.from(n.attributes)) {
            const rule = ICON_ATTRS[a.name];
            if (ICON_TAGS[tag].includes(a.name) && rule && rule.test(a.value)) attrs += ` ${a.name}="${esc(a.value)}"`;
        }
        if (tag === 'img') return /\ssrc="/.test(attrs) ? `<img${attrs} alt="">` : '';
        const inner = Array.from(n.childNodes).map((c) => iconNode(c, tag === 'span')).join('');
        return `<${tag}${attrs}>${inner}</${tag}>`;
    }
    function safeIcon(markup) {
        const t = document.createElement('template');
        t.innerHTML = String(markup == null ? '' : markup);
        return Array.from(t.content.childNodes).map((c) => iconNode(c, false)).join('');
    }

    /**
     * Inline icon markup for the document, always in the safe form above.
     * Glyph apps are already inline SVG (icons.js). Image icons are fetched
     * HERE — with the shell's session — and inlined as data URLs, because the
     * opaque-origin frame carries no cookie and must never be handed a URL to
     * fetch.
     */
    async function iconFor(app) {
        return safeIcon(await rawIconFor(app));
    }

    function rawIconFor(app) {
        const m = window.iconMarkup ? window.iconMarkup(app, 40) : '';
        if (!/^<img/i.test(m)) return Promise.resolve(m);
        const url = app.icon_url;
        const letter = `<span class="sk-app-letter">${esc((app.name || app.id || '?').charAt(0).toUpperCase())}</span>`;
        if (!url) return Promise.resolve(letter);
        if (state.iconCache.has(url)) return state.iconCache.get(url);
        const p = (async () => {
            try {
                const r = await fetch(url, { credentials: 'same-origin' });
                if (!r.ok) throw new Error(String(r.status));
                const blob = await r.blob();
                const dataUrl = await new Promise((res, rej) => {
                    const fr = new FileReader();
                    fr.onload = () => res(fr.result);
                    fr.onerror = rej;
                    fr.readAsDataURL(blob);
                });
                // The type is the response's own header, so it is checked, not
                // trusted: anything but a plain image type is the letter.
                return ICON_IMG_SRC.test(dataUrl) ? `<img src="${dataUrl}" alt="">` : letter;
            } catch (_) {
                state.iconCache.delete(url);   // retry on the next state post
                return letter;
            }
        })();
        state.iconCache.set(url, p);
        return p;
    }

    function runningMap() {
        const map = new Map();
        if (!window.WindowManager || typeof window.WindowManager.getAll !== 'function') return map;
        for (const w of window.WindowManager.getAll()) {
            if (!w || !w.appId) continue;
            const focused = !w.minimized && w.el && w.el.classList.contains('focused');
            map.set(w.appId, map.get(w.appId) === true || focused);
        }
        return map;
    }

    async function desktopApps() {
        const byId = new Map(state.appList.map((a) => [a.id, a]));
        const defaults = state.appList.filter((a) => a.system).map((a) => a.id);
        const ids = (window.DesktopLayout ? window.DesktopLayout.desktopIds(defaults) : defaults)
            .filter((id) => byId.has(id));
        const run = runningMap();
        return Promise.all(ids.map(async (id) => {
            const a = byId.get(id);
            return {
                id,
                name: a.name || id,
                description: a.description || '',
                icon: await iconFor(a),
                running: run.has(id),
                focused: run.get(id) === true,
            };
        }));
    }

    /** The bar's data — handed ONLY to a document that owns the bar (the
     *  scoping rule below): the dock exactly as the shell's taskbar draws it,
     *  the open windows, whether show-desktop is holding windows down, and the
     *  machine line the tray chip shows. */
    async function barState() {
        const byId = new Map(state.appList.map((a) => [a.id, a]));
        const model = (window.Taskbar && typeof window.Taskbar.dockModel === 'function')
            ? window.Taskbar.dockModel() : [];
        const dock = await Promise.all(model.filter((d) => byId.has(d.id)).map(async (d) => ({
            id: d.id, name: d.name, icon: await iconFor(byId.get(d.id)),
            pinned: !!d.pinned, running: !!d.running, focused: !!d.focused,
        })));
        const wins = (window.WindowManager && typeof window.WindowManager.getAll === 'function')
            ? window.WindowManager.getAll() : [];
        const windows = await Promise.all(wins.filter((w) => w && WIN_ID_RE.test(String(w.id)))
            .map(async (w) => {
                const app = w.appId ? byId.get(w.appId) : null;
                // An app window is named by its APP, not its live title — a title
                // can carry what is open inside it, and the rule here is what the
                // bar needs. A shell window (no app) keeps its short title.
                const title = app ? (app.name || app.id) : String(w.title || 'Window').slice(0, 60);
                return {
                    id: String(w.id),
                    app_id: app ? app.id : null,
                    title,
                    icon: app ? await iconFor(app)
                        : `<span class="sk-app-letter">${esc(title.charAt(0).toUpperCase() || '•')}</span>`,
                    focused: !w.minimized && !!(w.el && w.el.classList.contains('focused')),
                    minimized: !!w.minimized,
                };
            }));
        const chip = document.getElementById('tray-resources');
        const machine = chip ? String(chip.textContent || '').trim() : '';
        return {
            dock, windows,
            desktop_shown: !!(window.Taskbar && window.Taskbar.desktopShown),
            machine: machine === '—' ? '' : machine.slice(0, 60),
        };
    }

    function presenceText() {
        const el = document.getElementById('presence-line');
        return el ? String(el.textContent || '').trim() : '';
    }

    function postState() {
        clearTimeout(state.postTimer);
        state.postTimer = setTimeout(() => { _postState().catch(() => {}); }, 60);
    }

    async function _postState() {
        const f = state.frame;
        if (!f || !f._skPort) return;
        const apps = await desktopApps();
        if (state.frame !== f || !f._skPort) return;
        let reduced = false;
        try { reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (_) {}
        // SCOPED TO THE DECLARED MODE, not "everything we happen to have".
        //
        // A `wallpaper` document draws a backdrop and nothing else — the shell
        // keeps drawing the icon column and the presence line on top of it
        // (see the mode table in the module comment). It was still being handed
        // the user's ENTIRE installed-app inventory and their host's presence
        // sentence, which it has no way to use and no reason to hold.
        //
        // That matters because the frame is not a sealed room: a document may
        // always navigate ITSELF, and no CSP closes that (measured — see
        // js/sandbox-csp.js). Anything put through this port can leave. So the
        // rule is what the mode NEEDS: a desktop draws the icons and may offer
        // a launcher, so it gets `apps` and `all`; a wallpaper gets neither.
        //
        // A wallpaper that reads CSeriesDesktop.apps now sees an empty list. That
        // document was already wrong — the floor warns on a wallpaper that
        // draws apps, because it produces two icon sets.
        const drawsApps = (f._skMode || state.mode) === 'desktop';
        // The bar's data goes only to the document that DECLARED the bar —
        // same rule as the app list: what the document needs, nothing more.
        // It keeps flowing while the safety net has the taskbar back
        // (_skBarFailed), so a document that draws its dock from this state
        // can still draw it late and send desktop:bar-present; only a phone,
        // where the shell's taskbar is the way back, gets none.
        const owns = f === state.frame && barOwned();
        const declared = f === state.frame && !!f._skBar && f._skMode === 'desktop' && !isPhone();
        const bar = declared ? await barState() : null;
        if (state.frame !== f || !f._skPort) return;
        f._skPort.postMessage({
            type: 'desktop:state',
            mode: state.mode,
            apps: drawsApps ? apps : [],
            all: drawsApps
                ? state.appList.map((a) => ({ id: a.id, name: a.name || a.id }))
                : [],
            host: state.host,
            // The shell draws the presence line itself in wallpaper mode, so
            // only a `desktop` document needs the sentence.
            presence: drawsApps ? presenceText() : '',
            phone: isPhone(),
            reduced_motion: reduced,
            owns_bar: owns,
            dock: bar ? bar.dock : [],
            windows: bar ? bar.windows : [],
            desktop_shown: bar ? bar.desktop_shown : false,
            machine: bar ? bar.machine : '',
        });
    }

    // ── The bridge — the ONE door through the wall ───────────────────────

    function drop() {
        state.dropped += 1;
    }

    function rateOk() {
        const now = Date.now();
        state.stampsSec = state.stampsSec.filter((t) => now - t < 1000);
        state.stampsMin = state.stampsMin.filter((t) => now - t < 60000);
        if (state.stampsSec.length >= RATE_PER_SEC || state.stampsMin.length >= RATE_PER_MIN) return false;
        state.stampsSec.push(now);
        state.stampsMin.push(now);
        return true;
    }

    function validId(v) {
        const id = typeof v === 'string' ? v : '';
        return ID_RE.test(id) && state.installed.has(id) ? id : null;
    }

    function clearSelection() {
        document.querySelectorAll('.desktop-icon.selected').forEach((i) => i.classList.remove('selected'));
    }

    function frameFor(source) {
        if (!source) return null;
        if (state.frame && source === state.frame.contentWindow) return state.frame;
        if (state.pending && source === state.pending.contentWindow) return state.pending;
        return null;
    }

    /**
     * Window-level messages from the frame: exactly one is meaningful — the
     * bridge's first `desktop:hello` carrying its port. Bound once per frame;
     * a second hello (a document re-running the bridge source) is ignored.
     * Everything else on the window channel is unread and counted.
     */
    function onWindowMessage(e) {
        const f = frameFor(e.source);
        if (!f) return;
        const d = e.data;
        if (d && typeof d === 'object' && d.type === 'desktop:hello'
            && e.ports && e.ports[0] && !f._skPort) {
            const port = e.ports[0];
            f._skPort = port;
            port.onmessage = (ev) => onBridgeMessage(f, ev.data);
            if (state.pending === f) finishSwap(f);
            return;
        }
        drop();
    }

    function onBridgeMessage(f, d) {
        if (f !== state.frame) return drop();   // a superseded document's port
        let size = 0;
        try { size = JSON.stringify(d).length; } catch (_) { return drop(); }
        if (!d || typeof d !== 'object' || size > MSG_MAX_BYTES) return drop();
        if (!rateOk()) return drop();

        switch (d.type) {
            case 'desktop:ready':
                postState();
                return;
            case 'desktop:launch': {
                const id = validId(d.app_id);
                if (!id) return drop();
                const now = Date.now();
                if (now - state.lastLaunch < LAUNCH_MIN_GAP_MS) return drop();
                state.lastLaunch = now;
                if (window.AppRunner) window.AppRunner.launch(id);
                return;
            }
            case 'desktop:menu': {
                const x = Number(d.x), y = Number(d.y);
                if (!isFinite(x) || !isFinite(y)) return drop();
                const cx = Math.min(Math.max(0, x), window.innerWidth);
                const cy = Math.min(Math.max(0, y), window.innerHeight);
                if (d.dock === true) {
                    // A right-click on the design's dock item: the shell dock's
                    // own menu (Show / Pin / Unpin / Close).
                    if (!barOwned()) return drop();
                    const id = validId(d.app_id);
                    if (!id) return drop();
                    if (window.Taskbar) window.Taskbar.dockMenu(id, cx, cy);
                    return;
                }
                if (!window.DockMenu) return;
                if (d.app_id !== undefined) {
                    const id = validId(d.app_id);
                    if (!id) return drop();
                    if (typeof window.DockMenu.showAppMenu === 'function') window.DockMenu.showAppMenu(id, cx, cy);
                } else if (typeof window.DockMenu.showSurfaceMenu === 'function') {
                    window.DockMenu.showSurfaceMenu(cx, cy);
                }
                return;
            }
            case 'desktop:launcher':
                if (window.DockMenu) window.DockMenu.open();
                return;
            // ── The bar. Only the document that owns the bar may
            //    drive it; every relay fires only inside a trusted click in the
            //    frame (the bridge's gesture rule), and every id is checked
            //    against live shell state here. ──
            case 'desktop:activate': {
                if (!barOwned()) return drop();
                const id = validId(d.app_id);
                if (!id) return drop();
                const now = Date.now();
                if (now - state.lastLaunch < LAUNCH_MIN_GAP_MS) return drop();
                state.lastLaunch = now;
                if (window.Taskbar) window.Taskbar.activate(id);
                return;
            }
            case 'desktop:window': {
                if (!barOwned()) return drop();
                const wid = typeof d.win_id === 'string' && WIN_ID_RE.test(d.win_id) ? d.win_id : null;
                if (!wid || d.action !== 'toggle' || !window.WindowManager) return drop();
                const w = (window.WindowManager.getAll() || []).find((x) => String(x.id) === wid);
                if (!w) return drop();
                // The shell dock's own semantics: bring back, else minimize the
                // focused one, else focus.
                if (w.minimized) window.WindowManager.restore(w.id);
                else if (w.el && w.el.classList.contains('focused')) window.WindowManager.minimize(w.id);
                else window.WindowManager.focus(w.id);
                return;
            }
            case 'desktop:bar-missing':
                // The document declared the bar and then drew no dock and no
                // window list: give the shell's taskbar back rather than leave
                // minimized windows unreachable — until it says bar-present.
                if (!barOwned()) return drop();
                f._skBarFailed = true;
                setClasses();
                postState();
                return;
            case 'desktop:bar-present':
                // The document drew its bar after all (it was late, not
                // missing): hand the bar back.
                if (!f._skBarFailed) return drop();
                f._skBarFailed = false;
                setClasses();
                postState();
                return;
            case 'desktop:show-desktop':
                if (!barOwned()) return drop();
                if (window.Taskbar) window.Taskbar.toggleShowDesktop();
                return;
            case 'desktop:help': {
                if (!barOwned()) return drop();
                const modal = document.getElementById('support-modal');
                if (modal) modal.classList.toggle('hidden');
                return;
            }
            case 'desktop:machine':
                // The design's memory line opens the shell's own machine panel
                // (memory, every unit, every process) — the panel the tray chip
                // opens. It is drawn here; no process crosses into the frame.
                if (!barOwned()) return drop();
                if (window.TrayStatus && typeof window.TrayStatus.toggleMachine === 'function') {
                    window.TrayStatus.toggleMachine();
                }
                return;
            case 'desktop:pointerdown':
                // Parity with a click on the bare desktop: menus and tray
                // panels close, icon selection clears. (A pointerdown inside
                // the frame never reaches the shell's own document listeners.)
                // A press on the design's machine control leaves the panel to
                // the click that follows (desktop:machine toggles it); closing
                // it here would make that click reopen it every time. Only the
                // bar owner gets that exemption: for any other document the
                // click is dropped, so the press must close the panel.
                if (window.ContextMenu) window.ContextMenu.close();
                if (!(d.machine === true && barOwned()) && window.TrayStatus
                    && typeof window.TrayStatus.closePanels === 'function') {
                    window.TrayStatus.closePanels();
                }
                clearSelection();
                return;
            case 'desktop:escape':
                if (window.ContextMenu) window.ContextMenu.close();
                if (window.DockMenu) window.DockMenu.close();
                return;
            default:
                drop();
        }
    }

    // ── Init ─────────────────────────────────────────────────────────────

    function init() {
        window.addEventListener('message', onWindowMessage);

        loadApps();
        if (window.WSClient && typeof window.WSClient.on === 'function') {
            window.WSClient.on('app_installed', loadApps);
        }

        // Running / focused flags follow the window manager.
        for (const ev of ['window:created', 'window:closed', 'window:focused', 'window:minimized', 'window:restored']) {
            document.addEventListener(ev, postState);
        }
        // Desktop set (Add/Remove from Desktop, Reset Layout).
        document.addEventListener('cseries:layout-changed', postState);
        // Viewport flag — and the bar: a phone gives the taskbar back.
        if (window.CSeriesViewport) window.CSeriesViewport.onChange(() => { setClasses(); postState(); });
        // The bar's dock, pins and show-desktop state (taskbar.js announces).
        document.addEventListener('taskbar:changed', postState);
        // The machine line the bar shows is the tray chip's own text.
        const chip = document.getElementById('tray-resources');
        if (chip && window.MutationObserver) {
            new MutationObserver(() => { if (barOwned()) postState(); })
                .observe(chip, { childList: true, characterData: true, subtree: true });
        }

        // Presence sentence + host name: observe the shell's own elements
        // rather than adding a second poller.
        const presence = document.getElementById('presence-line');
        if (presence && window.MutationObserver) {
            new MutationObserver(() => postState())
                .observe(presence, { childList: true, characterData: true, subtree: true });
        }
        const nameEl = document.getElementById('entity-name');
        const readName = () => {
            const t = nameEl ? String(nameEl.textContent || '').trim() : '';
            if (t && !/^(starting up|reconnecting)/i.test(t)) {
                if (t !== state.host) { state.host = t; postState(); }
            }
        };
        if (nameEl && window.MutationObserver) {
            new MutationObserver(readName).observe(nameEl, { childList: true, characterData: true, subtree: true });
        }
        try {
            fetch('/api/system/status').then((r) => (r.ok ? r.json() : null)).then((s) => {
                if (s && s.entity_display_name) { state.host = String(s.entity_display_name); postState(); }
            }).catch(() => {});
        } catch (_) {}
    }

    document.addEventListener('DOMContentLoaded', init);

    return {
        apply,
        clear,
        /** Read-only view for tests / debugging. */
        get mode() { return state.url ? state.mode : null; },
        get ownsBar() { return barOwned(); },
        get active() { return !!state.frame; },
        get bound() { return !!(state.frame && state.frame._skPort); },
        get dropped() { return state.dropped; },
    };
})();

window.DesktopSurface = DesktopSurface;
