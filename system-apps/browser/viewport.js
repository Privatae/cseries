/* Live viewport client — CDP screencast over the kernel proxy.
 *
 * Connects one WebSocket to /system-apps/browser/viewport/ws (kernel
 * proxy → harness Service → sidecar control API, which speaks CDP to
 * Chromium). JPEG frames come out and are painted onto a <canvas>;
 * mouse/keyboard/wheel/paste go back as JSON input events. Protocol:
 * sidecars/browser/control/viewport.py.
 *
 * Opening this socket IS the wake signal: the kernel wakes the browser
 * harness on WS open and holds the wake lease while it stays open, so
 * the pod lives exactly as long as someone is looking at it.
 *
 * No build step. No frameworks. Exposes window.CSeriesViewport.
 */
(function () {
    "use strict";

    const WS_PATH = "/system-apps/browser/viewport/ws";
    // CDP modifier bitmask: Alt=1, Ctrl=2, Meta=4, Shift=8.
    function mods(e) {
        return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0)
             | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
    }
    const BUTTON = { 0: "left", 1: "middle", 2: "right" };

    // Control characters and the ones that reorder text become spaces: the
    // same as Settings' oneLine().
    function oneLine(s) {
        return String(s == null ? "" : s).replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g, " ");
    }

    function init(opts) {
        const canvas = opts.canvas;          // <canvas>
        const stage = opts.stage;            // sizing container
        const pagebar = opts.pagebar;        // tab strip <div>
        const onStatus = opts.onStatus || function () {};
        const ctx = canvas.getContext("2d");

        let ws = null;
        let alive = false;
        let closedByUs = false;
        let retryMs = 1000;
        // Why this machine would not start the browser (a short disk, no
        // room), as the kernel said it; kept on screen until a wake lands.
        let refused = "";
        let frameW = 0, frameH = 0;

        function send(obj) {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify(obj));
            }
        }

        // ── frames ─────────────────────────────────────────────────
        // A frame is a BINARY message carrying the JPEG bytes once this
        // client has opted in on open (sidecar v0.5.0+): the Blob decodes
        // straight into a bitmap, no base64 round-trip through a data:
        // URL. The {"t":"frame"} JSON shape — what an older sidecar sends,
        // and what a new one sends until the opt-in lands — is still
        // painted, through the same path.
        let drawing = false;
        async function drawBlob(blob) {
            if (drawing) return;             // never queue paints
            drawing = true;
            try {
                const bmp = await createImageBitmap(blob);
                if (canvas.width !== bmp.width || canvas.height !== bmp.height) {
                    canvas.width = bmp.width;
                    canvas.height = bmp.height;
                }
                frameW = bmp.width; frameH = bmp.height;
                ctx.drawImage(bmp, 0, 0);
                bmp.close();
            } catch (_) { /* a dropped paint fixes itself next frame */ }
            drawing = false;
        }
        async function drawFrame(b64) {
            if (drawing) return;
            try {
                const resp = await fetch("data:image/jpeg;base64," + b64);
                await drawBlob(await resp.blob());
            } catch (_) { /* as above */ }
        }

        // ── coordinate mapping (canvas CSS box → page CSS px) ──────
        function pageXY(e) {
            const r = canvas.getBoundingClientRect();
            const sx = frameW && r.width ? frameW / r.width : 1;
            const sy = frameH && r.height ? frameH / r.height : 1;
            return {
                x: Math.max(0, (e.clientX - r.left) * sx),
                y: Math.max(0, (e.clientY - r.top) * sy),
            };
        }

        // ── input ──────────────────────────────────────────────────
        function mouse(type, e, extra) {
            const p = pageXY(e);
            send(Object.assign({
                t: "mouse", type: type, x: p.x, y: p.y,
                button: BUTTON[e.button] || "none",
                buttons: e.buttons || 0,
                clickCount: e.detail || 1,
                mods: mods(e),
            }, extra || {}));
        }

        canvas.addEventListener("mousedown", (e) => {
            canvas.focus();
            e.preventDefault();
            mouse("down", e);
        });
        canvas.addEventListener("mouseup", (e) => { e.preventDefault(); mouse("up", e); });
        // Move events are frequent; rAF-throttle to one per paint.
        let movePending = null;
        canvas.addEventListener("mousemove", (e) => {
            if (movePending === null) {
                requestAnimationFrame(() => {
                    if (movePending) mouse("move", movePending);
                    movePending = null;
                });
            }
            movePending = e;
        });
        canvas.addEventListener("wheel", (e) => {
            e.preventDefault();
            mouse("wheel", e, { dx: e.deltaX, dy: e.deltaY });
        }, { passive: false });
        // Right-click belongs to the PAGE (mousedown/up already carry it).
        canvas.addEventListener("contextmenu", (e) => e.preventDefault());

        canvas.addEventListener("keydown", (e) => {
            e.preventDefault();
            // Printable text rides keyDown; chords and specials go bare.
            // ctrl+alt together is AltGr on many European layouts — that
            // IS printable input, not a chord.
            const printable = e.key.length === 1 && !e.metaKey
                && (!e.ctrlKey || e.altKey);
            send({
                t: "key", type: "down", key: e.key, code: e.code,
                keyCode: e.keyCode || 0, mods: mods(e), repeat: e.repeat,
                text: printable ? e.key : (e.key === "Enter" ? "\r" : ""),
            });
        });
        canvas.addEventListener("keyup", (e) => {
            e.preventDefault();
            send({ t: "key", type: "up", key: e.key, code: e.code,
                   keyCode: e.keyCode || 0, mods: mods(e) });
        });
        canvas.addEventListener("paste", (e) => {
            e.preventDefault();
            const text = (e.clipboardData || window.clipboardData).getData("text");
            if (text) send({ t: "text", text: text });
        });

        // ── sizing: the PAGE viewport follows this element ─────────
        let resizeTimer = null;
        function pushSize() {
            const r = stage.getBoundingClientRect();
            if (r.width >= 100 && r.height >= 100) {
                send({ t: "resize", w: Math.round(r.width), h: Math.round(r.height) });
            }
        }
        new ResizeObserver(() => {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(pushSize, 250);
        }).observe(stage);

        // ── page tabs ──────────────────────────────────────────────
        let lastPageCount = 0;
        function renderPages(pages) {
            pagebar.textContent = "";
            pagebar.hidden = pages.length < 2;
            pages.forEach((p) => {
                const b = document.createElement("button");
                b.type = "button";
                b.className = "page-tab" + (p.viewed ? " viewed" : "");
                b.textContent = p.title || p.url || "(blank)";
                b.title = p.url;
                b.addEventListener("click", () => send({ t: "switch_page", idx: p.idx }));
                pagebar.appendChild(b);
            });
            // A NEW page (an OAuth popup) is where the user's attention is
            // about to be — follow it automatically.
            if (pages.length > lastPageCount && lastPageCount > 0) {
                const newest = pages[pages.length - 1];
                if (!newest.viewed) send({ t: "switch_page", idx: newest.idx });
            }
            lastPageCount = pages.length;
        }

        // ── connection ─────────────────────────────────────────────
        function connect() {
            const scheme = location.protocol === "https:" ? "wss" : "ws";
            if (!refused) onStatus(alive ? "Reconnecting…" : "Starting browser…");
            ws = new WebSocket(scheme + "://" + location.host + WS_PATH);
            ws.binaryType = "blob";
            ws.addEventListener("open", () => {
                retryMs = 1000;
                // Ask for binary frames. A v0.4.x sidecar ignores the
                // message and keeps sending JSON, which is painted below.
                send({ t: "binary", on: true });
            });
            ws.addEventListener("message", (ev) => {
                if (ev.data instanceof Blob) {
                    if (!alive) { alive = true; refused = ""; onStatus(""); }
                    drawBlob(ev.data);
                    return;
                }
                let m;
                try { m = JSON.parse(ev.data); } catch (_) { return; }
                if (m.t === "frame") {
                    if (!alive) { alive = true; refused = ""; onStatus(""); }
                    drawFrame(m.data);
                } else if (m.t === "hello") {
                    alive = true;
                    refused = "";
                    onStatus("");
                    pushSize();
                } else if (m.t === "pages") {
                    renderPages(m.pages || []);
                } else if (m.t === "error" && m.refused) {
                    // Text, one line: it can name app ids.
                    refused = oneLine(m.detail || "refused");
                    onStatus("The browser did not start: " + refused);
                } else if (m.t === "error") {
                    onStatus("Viewport: " + (m.detail || "error"));
                }
            });
            ws.addEventListener("close", () => {
                if (closedByUs) return;
                if (refused) {
                    // Refused, not starting: the reason stays, and the wake
                    // is asked again at the slowest pace.
                    retryMs = 15000;
                } else {
                    // The first attempts race the harness wake — keep trying,
                    // with a ceiling so an uninstalled sidecar doesn't spin.
                    onStatus("Viewport disconnected — retrying…");
                }
                setTimeout(connect, retryMs);
                retryMs = Math.min(retryMs * 2, 15000);
            });
        }
        connect();

        return {
            close: function () { closedByUs = true; if (ws) ws.close(); },
        };
    }

    window.CSeriesViewport = { init: init };
})();
