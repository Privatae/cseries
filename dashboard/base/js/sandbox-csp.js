/**
 * C-Series — the CSP for HOST-AUTHORED HTML in a sandboxed frame.
 *
 * ONE definition, loaded by every surface that mounts a host-written document:
 * the HTML desktop (desktop-surface.js), the Artifacts window, the Designs
 * previews, the Network app's host-authored map, and the Workbench's
 * open-version window (a page a coding harness wrote).
 * Five copies of a security rule is five chances to drift, and the drift is
 * silent — a frame with a weaker policy looks identical on screen.
 *
 * WHY A CSP AT ALL, when the frame is already sandbox="allow-scripts".
 *
 * The sandbox attribute gives the frame an opaque origin: no session cookie,
 * no same-origin access to the shell, no top navigation, no forms. It does NOT
 * take away the network. Measured in Chromium against a srcdoc
 * frame with sandbox="allow-scripts" and no CSP: `fetch()` and `new
 * Image().src` both reached an external server. So a host-authored document —
 * which the host can rewrite at any time, and which a prompt-injected host
 * might write on someone else's behalf — could quietly call home from inside
 * the user's browser.
 *
 * WHAT THIS CLOSES (all measured, scripts/test_sandbox_csp.py):
 *   fetch · XHR · Image.src · <img src> · sendBeacon · WebSocket · CSS url()
 *   · <link rel=prefetch> · <iframe src> · window.open · form POST
 *
 * WHAT IT DOES NOT CLOSE — two lanes, and nothing the embedding page controls
 * closes either. Measured in the gate's own Chromium (151.0.7922.34)
 * and PINNED by scripts/test_sandbox_csp.py, which fails the day a browser
 * change closes one, so this note and the policy get revisited:
 *
 *   1. SELF-NAVIGATION. A document may always navigate ITSELF — `location.href
 *      = …`, <meta http-equiv="refresh">, a clicked <a> — and the request
 *      carries whatever it put in the URL. The sandbox flag only forbids
 *      navigating OTHER browsing contexts, and CSP has no enforced directive
 *      for navigations: `navigate-to 'none'` in this policy was measured and
 *      the request still arrived (form-action covers forms only, which is why
 *      that lane IS closed). Only a sandbox without allow-scripts stops it, and
 *      every surface needs scripts.
 *
 *   2. WEBRTC. `new RTCPeerConnection({iceServers:[{urls:'stun:…'}]})` sends
 *      STUN packets (4 per offer, measured). Tried, and none stopped them:
 *        - CSP `webrtc 'block'` — not enforced (the packets arrived with it in
 *          this policy, with and without a trailing `;`);
 *        - Permissions-Policy — Chromium knows 81 features and none is WebRTC;
 *          `allow="loopback-network 'none'; local-network 'none';
 *          local-network-access 'none'"` did not stop even a loopback STUN;
 *        - the sandbox attribute has no flag for it;
 *        - a script injected ahead of the document that deletes
 *          RTCPeerConnection is a speed bump, not a wall: a nested srcdoc frame
 *          is a fresh realm that never ran it, and STUN left from there.
 *      If a browser starts enforcing `webrtc 'block'`, the pin says so, and the
 *      directive goes into CSP below.
 *
 * Surfaces that hand the frame anything worth stealing must therefore also
 * narrow what they send and notice when the frame leaves — see the Network
 * map's hostedPush/cseries:network:leaving for the pattern, and the desktop's
 * mode-scoped payload (desktop-surface.js _postState).
 *
 * WHAT STAYS ALLOWED, and why: inline styles and scripts (the document is one
 * self-contained file — that is the whole artifact model, and the injected
 * bridge is itself an inline script), and data: images and fonts (what the
 * authoring guidance tells hosts to use: "inline every asset — system fonts,
 * CSS gradients, SVG, canvas"). blob: is allowed for images so canvas work
 * can render its own output.
 *
 * A document that references an EXTERNAL asset renders without it. That is the
 * intended outcome — the authoring rules already say inline everything, and
 * the desktop floor refuses external references outright — but a surface
 * showing such a document should say so rather than leave the user staring at
 * a missing image. See noteExternalRefs().
 */
(function () {
    'use strict';

    var CSP = "default-src 'none'; "
        + "style-src 'unsafe-inline'; "
        + "script-src 'unsafe-inline'; "
        + "img-src data: blob:; "
        + "font-src data:; "
        + "media-src data: blob:; "
        + "connect-src 'none'; "
        + "form-action 'none'; "
        + "base-uri 'none'";

    var META = '<meta http-equiv="Content-Security-Policy" content="' + CSP + '">';

    /**
     * Put the policy in force before any of the document's own markup is
     * parsed. A <meta http-equiv> is the ONLY CSP that reaches a srcdoc frame:
     * response headers do not apply to inline content, and the iframe csp=
     * attribute is a Chrome-only proposal.
     *
     * Injected after a leading <!DOCTYPE> if there is one — putting anything
     * before the doctype drops the document into quirks mode — and at index 0
     * otherwise. `extra` is any bridge script the surface needs to run first;
     * it goes AFTER the CSP so the policy already applies to it.
     */
    function withSandboxCsp(html, extra) {
        var doc = String(html == null ? '' : html);
        // HTML's whitespace only (tab, LF, FF, CR, space), never \s: \s also
        // matches a no-break space and other Unicode spaces the parser treats
        // as TEXT, which opens <body> — and a CSP <meta> placed after that is
        // ignored. A document opening with one sent every lane out while
        // looking sealed (pinned in test_sandbox_csp.py).
        var m = /^[\t\n\f\r ]*<!DOCTYPE[^>]*>/i.exec(doc);
        var at = m ? m[0].length : 0;
        return doc.slice(0, at) + META + (extra || '') + doc.slice(at);
    }

    /**
     * The external references this document makes, which the CSP will block.
     * Returned so a surface can tell the user why something is missing instead
     * of showing a silently broken render. Deliberately the same shape the
     * desktop floor refuses on, so the two agree about what "external" means.
     */
    function externalRefs(html) {
        var out = [];
        var re = /(?:src|href)\s*=\s*["']\s*(https?:)?\/\/([^"'\s]+)/gi;
        var m;
        while ((m = re.exec(String(html || ''))) && out.length < 8) {
            out.push((m[1] || '') + '//' + m[2]);
        }
        var css = /url\(\s*["']?\s*(https?:)?\/\/([^)"'\s]+)/gi;
        while ((m = css.exec(String(html || ''))) && out.length < 8) {
            out.push((m[1] || '') + '//' + m[2]);
        }
        return out;
    }

    window.CSeriesSandbox = {
        CSP: CSP,
        META: META,
        withSandboxCsp: withSandboxCsp,
        externalRefs: externalRefs,
    };
})();
