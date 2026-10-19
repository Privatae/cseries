/* ================================================================
 * Keys — client logic.
 *
 * Manage the host's sealed credential vault (server/vault). Same-origin
 * system app: plain fetch('/api/...'); the window token is injected by the
 * shell. The agent NEVER receives raw bytes — neither does this UI, except
 * the ONE TOTP-gated reveal path. Everything degrades to an honest
 * empty/error state.
 *
 * SHAPE (rebuilt 2026-08-06 as a menu; providers pass same day):
 * one menu, one section at a time. A credential belongs to something — a
 * provider that bills you, a site, Privatae itself — and those have different
 * lifecycles, so they are different sections instead of one undifferentiated
 * wall of cards:
 *
 *   Providers   first-party (their own models) + marketplace (everyone's
 *               models — OpenRouter, DeepInfra, Together, Fireworks, Groq),
 *               the split the whole platform routes on. Every slot names its
 *               `source`: the API key is the user's to paste, the sign-in is
 *               the HOST's to fill (it runs the vendor's own login when you
 *               connect a builder), so the sign-in is a status chip with no
 *               Add and no buttons at all — except a sign-in the Host can
 *               run its own model on (xAI, `signin_from_keys`), which always
 *               carries a button: "Sign in with xAI" while there is none to
 *               use, "Reconnect with xAI" once one is stored
 *               (without a model there is no Host to ask).
 *               Marketplace providers have no
 *               sign-in slot (no OAuth surface at a reseller) and neither does
 *               a first-party provider no builder can sign into. Every stored
 *               KEY carries Reveal / Deactivate / Revoke — deactivate is the
 *               temporary off-switch, the value stays sealed and Activate
 *               brings it back.
 *   Sites       database URLs the Sites app writes for you
 *   Privatae    your Gitea sign-in
 *   Vault       everything else — SSH keys, tokens, TOTP secrets, and the
 *               harness credentials (which harness runs on which key is the
 *               Orchestrator's coding matrix — Keys no longer duplicates it)
 *
 * Surfaces:
 *   GET    /api/vault/secrets                 -> list metadata (no values)
 *   GET    /api/vault/providers               -> typed-provider slots (names + source + stored/deactivated flags, no values)
 *   GET    /api/vault/stepup                  -> {enrolled} — asked BEFORE a reveal demands a code
 *   POST   /api/vault/secrets                 -> add (name, cred_type, value_b64)
 *   POST   /api/vault/secrets/{name}/deactivate -> {deactivated: bool} — the off/on switch
 *   DELETE /api/vault/secrets/{name}          -> revoke
 *   POST   /api/vault/secrets/{name}/reveal   -> raw value (TOTP-gated)
 *   POST   /api/vault/providers/{p}/signin    -> start the vendor's own sign-in (xai only)
 *   POST   /api/vault/providers/{p}/signin/finish -> has the user finished? (polled)
 *   GET    /api/vault/jobs/{id}               -> either of those two, when it outlasted its request (202)
 * Step-up enrollment reuses the Network app's second factor:
 *   POST   /api/network/nac/stepup/enroll     -> { secret, otpauth_uri }
 *   POST   /api/network/nac/stepup/confirm    -> {} (marks enrolled)
 * ================================================================ */
(function () {
    'use strict';

    var state = {
        keys: [], providers: null,
        loaded: false, section: 'providers', addMode: 'provider',
    };
    // Which sign-in dialog is current. Every open and every close moves it on,
    // so a polling loop from a dialog that is gone stops at its next step.
    var signinRun = 0;

    var $ = function (id) { return document.getElementById(id); };
    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    async function fetchJSON(url, opts) {
        var r = await fetch(url, opts || {});
        if (!r.ok) {
            var body = await r.text().catch(function () { return ''; });
            var err = new Error(friendlyError(r.status, body));
            err.status = r.status;
            throw err;
        }
        if (r.status === 204) return {};
        return r.json();
    }
    function friendlyError(status, body) {
        try { var p = JSON.parse(body); if (p && (p.detail || p.error)) return p.detail || p.error; } catch (e) {}
        if (status === 401 || status === 403) return "This Host hasn't granted the Keys app that permission.";
        if (status === 404) return 'Not found — it may have been revoked.';
        if (status === 409) return 'Enroll a second factor first.';
        if (status === 503) return 'That service is warming up. Try again in a moment.';
        if (status >= 500) return 'Something went wrong on the Host. Try again shortly.';
        return body ? body.slice(0, 160) : 'Request failed.';
    }
    function fmtRel(iso) {
        if (!iso) return '';
        var t = Date.parse(iso); if (isNaN(t)) return '';
        var s = Math.floor((Date.now() - t) / 1000);
        if (s < 60) return 'just now';
        if (s < 3600) return Math.floor(s / 60) + 'm ago';
        if (s < 86400) return Math.floor(s / 3600) + 'h ago';
        return Math.floor(s / 86400) + 'd ago';
    }
    // UTF-8-safe base64 both ways.
    function toB64(str) { return btoa(unescape(encodeURIComponent(str))); }
    function fromB64(b64) { try { return decodeURIComponent(escape(atob(b64))); } catch (e) { return atob(b64); } }

    var TYPES = [
        { v: 'api_key', label: 'API key' },
        { v: 'token', label: 'Token' },
        { v: 'ssh_key', label: 'SSH key' },
        { v: 'totp_secret', label: 'TOTP / 2FA secret' },
        { v: 'opaque', label: 'Other secret' },
    ];
    // cred_types written server-side that the free-text form can't produce.
    // Shown so a credential never renders as a raw internal string.
    var EXTRA_TYPE_LABELS = {
        oauth_token: 'OAuth token', database_url: 'Database URL',
        gitea_oauth: 'Gitea sign-in', claude_oauth: 'Claude sign-in',
    };
    function typeLabel(t) {
        for (var i = 0; i < TYPES.length; i++) if (TYPES[i].v === t) return TYPES[i].label;
        if (EXTRA_TYPE_LABELS[t]) return EXTRA_TYPE_LABELS[t];
        if (t && t.indexOf('harness_') === 0) return 'Builder credential';
        return t || 'Secret';
    }

    // ── Which section a credential belongs to ──────────────────────────────
    // Name-based, because the vault has no owner column yet (the credential
    // review's F4). When it gains one this reads that instead — the parsing is
    // the stopgap, not the design. Harness credentials (harness-*, harness:*,
    // harness_*, claude_oauth) deliberately fall through to the VAULT: which
    // harness runs on which key is the Orchestrator's coding matrix — Keys
    // keeps them stored and governable without duplicating that surface
    // (the Builders tab conflated the two).
    function sectionOf(k) {
        var n = String(k.name || '');
        var t = String(k.cred_type || '');
        if (n.indexOf('site-') === 0 || t === 'database_url') return 'sites';
        if (n.indexOf('gitea') === 0 || t === 'gitea_oauth') return 'privatae';
        if (state.providers) {
            for (var i = 0; i < state.providers.length; i++) {
                var slots = state.providers[i].slots || [];
                for (var j = 0; j < slots.length; j++) {
                    // Only a slot the USER fills claims its credential for the
                    // Providers section. A host-filled sign-in stays in the
                    // Vault: the Providers strip shows it as a status chip with
                    // no buttons, so claiming it here would leave the row with
                    // nowhere to be revealed or revoked from at all.
                    if (slots[j].source === 'host') continue;
                    if (slots[j].name === n) return 'providers';
                }
            }
        }
        return 'vault';
    }

    var SECTIONS = [
        { id: 'providers', label: 'Providers', hint: 'The accounts that bill you for models.' },
        { id: 'sites', label: 'Sites', hint: 'Database URLs your sites use.' },
        { id: 'privatae', label: 'Privatae', hint: 'Your sign-in to Privatae services.' },
        // "Sign-ins" (2026-09-04, was "Autofill"): the site logins the user
        // saved in the Browser app — and the credential home of the browser
        // agents they record there. The section id stays `autofill` so the
        // routes and grant table underneath it do not move.
        { id: 'autofill', label: 'Sign-ins', hint: 'Site logins you saved; which may be used while you are away.' },
        { id: 'vault', label: 'Vault', hint: 'SSH keys, tokens, and anything else you store.' },
    ];

    // ── modal (centered, over a dimmed scrim) ──────────────────────────────
    function openModal(title, bodyHTML, actions) {
        $('keys-modal-title').textContent = title;
        $('keys-modal-body').innerHTML = bodyHTML || '';
        var actEl = $('keys-modal-actions');
        actEl.innerHTML = '';
        (actions || []).forEach(function (a) {
            var btn = document.createElement('button');
            btn.className = 'cseries-btn ' + (a.cls || '');
            btn.textContent = a.label;
            if (a.id) btn.id = a.id;
            if (a.disabled) btn.disabled = true;
            btn.addEventListener('click', a.onClick);
            actEl.appendChild(btn);
        });
        $('keys-scrim').hidden = false;
        return { body: $('keys-modal-body'), actions: actEl };
    }
    // Closing the window, however it is closed (Cancel, the X, the scrim),
    // also ends a sign-in that is polling behind it: signinRun moves on and the
    // loop sees it is no longer the current one.
    function closeModal() { signinRun++; $('keys-scrim').hidden = true; $('keys-modal-body').innerHTML = ''; }

    // 6-box TOTP entry; onChange(code) fires each keystroke.
    function totpEntry(container, onChange) {
        var code = '';
        function render() {
            var h = '<div class="keys-totp" role="group" aria-label="6-digit authenticator code">';
            for (var i = 0; i < 6; i++) {
                var filled = i < code.length, caret = i === code.length;
                h += '<div class="keys-totp-box ' + (filled ? 'filled' : '') + ' ' + (caret ? 'caret' : '') + '">' + (filled ? esc(code[i]) : (caret ? '|' : '')) + '</div>';
            }
            container.innerHTML = h + '</div><input id="keys-totp-input" inputmode="numeric" autocomplete="one-time-code" maxlength="6" style="position:absolute;opacity:0;width:1px;height:1px;pointer-events:none" aria-hidden="true">';
            var inp = $('keys-totp-input');
            inp.value = code;
            inp.addEventListener('input', function () {
                code = (inp.value || '').replace(/\D/g, '').slice(0, 6);
                inp.value = code; render(); inp.focus();
                if (onChange) onChange(code);
            });
            setTimeout(function () { inp.focus(); }, 0);
        }
        render();
        return { get: function () { return code; } };
    }

    // Real scannable QR for an otpauth:// (or any) string, vendored qrcode-generator.
    function drawQR(el, text) {
        try {
            if (typeof qrcode !== 'function') throw new Error('qr lib missing');
            var qr = qrcode(0, 'M');
            qr.addData(text); qr.make();
            el.innerHTML = qr.createSvgTag({ cellSize: 5, margin: 0, scalable: true });
            var svg = el.querySelector('svg');
            if (svg) { svg.removeAttribute('width'); svg.removeAttribute('height'); }
            return true;
        } catch (e) {
            el.innerHTML = '<div class="keys-mono" style="word-break:break-all">' + esc(text) + '</div>';
            return false;
        }
    }

    // ── load ───────────────────────────────────────────────────────────────
    function setAlert(msg) {
        var a = $('keys-alert');
        if (!msg) { a.hidden = true; a.textContent = ''; return; }
        a.hidden = false; a.textContent = msg;
    }

    async function load() {
        try {
            var res = await fetchJSON('/api/vault/secrets');
            state.keys = res.secrets || [];
            state.loaded = true;
            setAlert('');
        } catch (e) {
            setAlert(e.message);
            state.keys = [];
        }
        // Provider slots degrade on their own — one unavailable section must
        // never blank the app.
        try {
            var pres = await fetchJSON('/api/vault/providers');
            state.providers = pres.providers || [];
        } catch (e2) { state.providers = null; }
        // Autofill: the sites with saved form entries, and which of those the
        // user has allowed to be filled unattended. Degrades on its own, like
        // providers — one unavailable section must never blank the app.
        try {
            var sres = await fetchJSON('/api/browser/forms');
            state.formSites = sres.sites || [];
        } catch (e3) { state.formSites = null; }
        try {
            var gres = await fetchJSON('/api/browser/forms/grants');
            state.grants = gres.grants || [];
        } catch (e4) { state.grants = null; }
        render();
    }

    function keysIn(section) {
        return state.keys.filter(function (k) { return sectionOf(k) === section; });
    }

    // ── render ─────────────────────────────────────────────────────────────
    function renderMenu() {
        $('keys-menu').innerHTML = SECTIONS.map(function (s) {
            // Autofill counts SITES, not vault rows: its entries are named
            // form:<host>:<key> and live under Vault like every other sealed
            // key, so keysIn('autofill') is always 0. The menu read
            // "Autofill 0" beside a list of two sites — caught on screen, not
            // by the assertions, and exactly the small dishonesty that makes a
            // user stop believing the rest of the numbers.
            var n = s.id === 'autofill'
                ? (state.formSites ? state.formSites.length : 0)
                : (state.loaded ? keysIn(s.id).length : 0);
            return '<button class="keys-menu-item' + (state.section === s.id ? ' active' : '')
                + '" data-section="' + s.id + '">'
                + '<span class="keys-menu-label">' + esc(s.label) + '</span>'
                + '<span class="keys-menu-count label-sys">' + n + '</span>'
                + '</button>';
        }).join('');
    }

    // One row per credential — same shape everywhere, so the eye learns it once.
    // A deactivated credential shows its off state and the way back (Activate)
    // instead of pretending to be live.
    function credRow(k, note) {
        var isTotp = k.cred_type === 'totp_secret';
        var off = !!k.deactivated;
        return '<div class="keys-row' + (off ? ' keys-row-off' : '') + '">'
            + '<div class="keys-row-main">'
              + '<div class="keys-row-name">' + esc(k.name)
                + (off ? ' <span class="keys-off-tag label-sys">deactivated</span>' : '') + '</div>'
              + '<div class="keys-row-sub label-sys">' + esc(typeLabel(k.cred_type))
                + ' · added ' + esc(fmtRel(k.created_at))
                + (note ? ' · ' + esc(note) : '') + '</div>'
            + '</div>'
            + '<div class="keys-row-actions">'
              + '<button class="cseries-btn ghost" data-act="reveal" data-name="' + esc(k.name) + '" data-type="' + esc(k.cred_type) + '">' + (isTotp ? 'Show QR' : 'Reveal') + '</button>'
              + (off
                  ? '<button class="cseries-btn ghost" data-act="activate" data-name="' + esc(k.name) + '">Activate</button>'
                  : '<button class="cseries-btn ghost" data-act="deactivate" data-name="' + esc(k.name) + '">Deactivate</button>')
              + '<button class="cseries-btn ghost danger" data-act="revoke" data-name="' + esc(k.name) + '">Revoke</button>'
            + '</div>'
          + '</div>';
    }

    function emptyState(msg, btnLabel, act) {
        return '<div class="keys-empty">'
            + '<p class="label-sys">' + esc(msg) + '</p>'
            + (btnLabel ? '<button class="cseries-btn primary" data-act="' + act + '">' + esc(btnLabel) + '</button>' : '')
            + '</div>';
    }

    // One slot inside a provider row. The server says WHO fills it (slot.source)
    // and that decides whether an Add button is even legitimate:
    //
    //   source 'user'  the API key. Empty → an Add button; stored → a state chip
    //                  plus the key's lifecycle actions (Reveal / Deactivate /
    //                  Revoke); deactivated → the chip says so and Activate is
    //                  the way back.
    //   source 'host'  the OAuth slot. The HOST fills it, by running the
    //                  vendor's own login through your builder — so it is a
    //                  STATUS CHIP and nothing else. No Add (it lights up
    //                  once a harness is authenticated through the Host; no
    //                  login service is built for it), and no lifecycle buttons either: that credential
    //                  belongs to the builder, and its row keeps Reveal /
    //                  Deactivate / Revoke in the Vault section where it lives.
    //                  Putting a Revoke here would offer to disconnect a builder
    //                  from a screen about provider billing.
    function slotHtml(p, s) {
        if (s.source === 'host') return hostSlotHtml(p, s);
        var type = s.cred_type === 'api_key' ? 'API key' : 'OAuth token';
        if (s.stored) {
            return '<span class="keys-slot filled">' + type
                + '<span class="keys-slot-state">Stored</span></span>'
                + '<span class="keys-slot-actions">'
                  + '<button class="cseries-btn ghost" data-act="reveal" data-name="' + esc(s.name) + '" data-type="' + esc(s.cred_type) + '">Reveal</button>'
                  + '<button class="cseries-btn ghost" data-act="deactivate" data-name="' + esc(s.name) + '">Deactivate</button>'
                  + '<button class="cseries-btn ghost danger" data-act="revoke" data-name="' + esc(s.name) + '">Revoke</button>'
                + '</span>';
        }
        if (s.deactivated) {
            return '<span class="keys-slot deactivated">' + type
                + '<span class="keys-slot-state">Deactivated</span></span>'
                + '<span class="keys-slot-actions">'
                  + '<button class="cseries-btn ghost" data-act="reveal" data-name="' + esc(s.name) + '" data-type="' + esc(s.cred_type) + '">Reveal</button>'
                  + '<button class="cseries-btn ghost" data-act="activate" data-name="' + esc(s.name) + '">Activate</button>'
                  + '<button class="cseries-btn ghost danger" data-act="revoke" data-name="' + esc(s.name) + '">Revoke</button>'
                + '</span>';
        }
        return '<button class="keys-slot" data-act="add-provider"'
            + ' data-provider="' + esc(p.provider) + '">'
            + type + '<span class="keys-slot-state">Add</span></button>';
    }

    // The host-filled slot: a chip, never a control. `harness_name` is the
    // builder whose sign-in fills it, so the empty state names the thing to go
    // do instead of leaving a dark box with no explanation.
    //
    // One exception (2026-10-05, reversing 0.5.0's "chip, never a
    // control" for this slot only): a sign-in the Host can run its own model
    // on — the server marks it `signin_from_keys`, xAI today — always carries
    // a button. A user whose only subscription is Grok has no model, so there
    // is no Host to ask; the button starts the same vendor sign-in the Host's
    // own tool would. The server picks the button and its words
    // (`signin_action` / `signin_label`, the same words its messages quote):
    //   nothing stored     the slot itself is the button: "Sign in with xAI"
    //   switched off       the Off chip + "Sign in with xAI"
    //   stored             the chip + "Reconnect with xAI" — the way back when
    //                      the sign-in has expired or cannot be read (the chip
    //                      says which), and when xAI refuses it, which only a
    //                      model call can see.
    // Every other sign-in slot stays a chip.
    function signinButtonHtml(p, s, cls) {
        var vendor = s.vendor || p.provider;
        var label = s.signin_label || ('Sign in with ' + vendor);
        return '<button class="' + cls + '" data-act="signin"'
            + ' data-provider="' + esc(p.provider) + '" data-vendor="' + esc(vendor) + '">'
            + esc(label) + '</button>';
    }
    function hostSlotHtml(p, s) {
        var who = s.harness_name || 'your builder';
        var action = (s.signin_from_keys && s.signin_action)
            ? '<span class="keys-slot-actions">' + signinButtonHtml(p, s, 'cseries-btn ghost') + '</span>'
            : '';
        if (s.stored) {
            var bad = s.signin_state === 'expired' ? 'Expired'
                : (s.signin_state === 'unreadable' ? 'Unreadable' : '');
            return '<span class="keys-slot ' + (bad ? 'deactivated' : 'filled') + '">Sign-in'
                + '<span class="keys-slot-state">' + (bad ? esc(bad) + ' — ' : 'via ')
                + esc(who) + '</span></span>' + action;
        }
        if (s.deactivated) {
            return '<span class="keys-slot deactivated">Sign-in'
                + '<span class="keys-slot-state">Off — ' + esc(who) + '</span></span>' + action;
        }
        if (s.signin_from_keys) {
            var vendor = s.vendor || p.provider;
            return '<button class="keys-slot keys-slot-signin" data-act="signin"'
                + ' data-provider="' + esc(p.provider) + '" data-vendor="' + esc(vendor) + '">Sign-in'
                + '<span class="keys-slot-state">' + esc(s.signin_label || ('Sign in with ' + vendor))
                + '</span></button>';
        }
        return '<span class="keys-slot keys-slot-waiting" title="Ask your Host to connect '
            + esc(who) + ' — it runs ' + esc(who) + '’s own sign-in on your machine.">Sign-in'
            + '<span class="keys-slot-state">Connect ' + esc(who) + '</span></span>';
    }

    function renderProviders() {
        if (!state.providers) {
            return '<div class="keys-empty"><p class="label-sys">Provider list unavailable right now.</p></div>';
        }
        var band = function (title, hint, list) {
            if (!list.length) return '';
            return '<div class="keys-band card-label label-sys">' + esc(title)
                + ' <span class="label-sys keys-band-hint">' + esc(hint) + '</span></div>'
                + list.map(function (p) {
                    var slots = (p.slots || []).map(function (s) { return slotHtml(p, s); }).join('');
                    return '<div class="keys-provider">'
                        + '<div class="keys-provider-name">' + esc(p.provider) + '</div>'
                        + '<div class="keys-provider-slots">' + slots + '</div>'
                        + '</div>';
                }).join('');
        };
        var first = state.providers.filter(function (p) { return p.kind !== 'generic'; });
        var market = state.providers.filter(function (p) { return p.kind === 'generic'; });
        return '<div class="keys-head"><h2>Providers</h2>'
            + '<p class="view-sub">First-party providers sell their own models; marketplace providers resell everyone\'s. Which provider each role uses is decided in the Orchestrator app. Deactivate switches a key off without revoking it; Activate brings the same value back. A <em>Sign-in</em> is filled by your Host when you connect a builder — ask it to connect one; there is nothing to paste. Where a sign-in has a <em>Sign in with …</em> or <em>Reconnect with …</em> button, you can start it right here.</p></div>'
            + band('First-Party Providers', '· their own models', first)
            + band('Marketplace Providers', '· everyone\'s models', market)
            + (state.providers.length ? '' : emptyState('No providers in the registry.', null, null));
    }

    // ── autofill ───────────────────────────────────────────────────────────
    // The grant lives HERE, on a user surface, and not on the Host's tool.
    // Decided 2026-08-30 (review finding B1): a grant WIDENS what may
    // happen while nobody is watching, so it must not originate from a model
    // that a hostile page can talk to. The Host can read a grant and revoke
    // one — revoking only tightens — but only this screen can create one.
    function renderAutofill() {
        if (state.formSites === null) {
            return '<div class="keys-empty"><p class="label-sys">Saved form entries unavailable right now.</p></div>';
        }
        var granted = {};
        (state.grants || []).forEach(function (g) { granted[g.host] = g; });
        var sites = state.formSites || [];

        var head = '<div class="keys-head"><h2>Sign-ins</h2>'
            + '<p class="view-sub">Site logins and form entries you saved in the Browser app, grouped by site. '
            + 'Allowing a site lets a browser agent you recorded — or your Host — sign in there when you are not watching. '
            + 'Revoking stops that; it never deletes what you saved. '
            + 'Values are never shown here — these are ordinary sealed keys, listed in Vault too.</p></div>';

        if (!sites.length) {
            return head + emptyState('Nothing saved yet. In the Browser app, sign in to a site and press "Save these entries", or record an agent.', null, null);
        }

        return head + sites.map(function (site) {
            var g = granted[site.host];
            var fields = (site.fields || []);
            var live = fields.filter(function (f) { return !f.deactivated; }).length;
            var used = g && g.last_used_at
                ? 'last used ' + esc(new Date(g.last_used_at).toLocaleDateString())
                : (g ? 'never used yet' : '');
            return '<div class="keys-row">'
                + '<div class="keys-row-main">'
                  + '<div class="keys-row-name">' + esc(site.host) + '</div>'
                  + '<div class="keys-row-meta label-sys">'
                    + live + ' saved ' + (live === 1 ? 'entry' : 'entries')
                    + (g ? ' · allowed' + (used ? ' · ' + used : '') : ' · not allowed')
                  + '</div>'
                + '</div>'
                + '<div class="keys-row-actions">'
                  + (g
                     ? '<button class="cseries-btn ghost" data-act="autofill-revoke" data-host="' + esc(site.host) + '">Stop allowing</button>'
                     : '<button class="cseries-btn primary" data-act="autofill-grant" data-host="' + esc(site.host) + '">Allow autofill</button>')
                + '</div>'
              + '</div>';
        }).join('');
    }

    async function doAutofillGrant(host) {
        // Granting is a WIDEN, so it is confirmed explicitly — the same
        // asymmetry the Network app applies to NAC grants.
        openModal('Allow autofill for ' + host + '?', ''
            + '<p class="view-sub">Your Host will be able to fill this site\'s form '
            + 'using the entries you saved, without asking you each time. '
            + 'It still never sees the values, and a saved password can only go '
            + 'into a real password box.</p>'
            + '<p class="view-sub">You can stop this at any time from this screen.</p>',
            [{ label: 'Cancel', cls: 'ghost', onClick: closeModal },
             { label: 'Allow', cls: 'primary', onClick: async function () {
                closeModal();
                try {
                    await fetchJSON('/api/browser/forms/grants', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ host: host }),
                    });
                    await load();
                } catch (e) { setAlert('Could not allow ' + host + '. ' + e.message); }
             }}]);
    }

    async function doAutofillRevoke(host) {
        // Revoking only tightens — no confirmation, like the Network app.
        try {
            await fetchJSON('/api/browser/forms/grants/' + encodeURIComponent(host),
                            { method: 'DELETE' });
            await load();
        } catch (e) { setAlert('Could not stop autofill for ' + host + '. ' + e.message); }
    }

    function renderPlain(section, title, sub, addLabel) {
        var list = keysIn(section);
        return '<div class="keys-head"><h2>' + esc(title) + '</h2>'
            + '<p class="view-sub">' + esc(sub) + '</p>'
            + (addLabel ? '<div class="view-toolbar"><button class="cseries-btn primary" data-act="add-custom">' + esc(addLabel) + '</button></div>' : '')
            + '</div>'
            + (list.length ? list.map(function (k) { return credRow(k); }).join('')
                           : emptyState('Nothing here yet.', addLabel, 'add-custom'));
    }

    function render() {
        renderMenu();
        $('foot-right').textContent = state.keys.length + ' sealed';
        $('keys-health').innerHTML = '<button class="cseries-btn ghost" data-act="refresh">Refresh</button>';
        var el = $('keys-view');
        if (!state.loaded) { el.innerHTML = '<div class="keys-row skeleton" style="height:54px"></div>'; return; }
        if (state.section === 'providers') el.innerHTML = renderProviders();
        else if (state.section === 'autofill') el.innerHTML = renderAutofill();
        else if (state.section === 'sites') el.innerHTML = renderPlain('sites', 'Sites',
            'Database URLs your sites use. The Sites app writes these for you — it never shows the URL in plain text.', null);
        else if (state.section === 'privatae') el.innerHTML = renderPlain('privatae', 'Privatae',
            'Your sign-in to Privatae services, like Gitea.', null);
        else el.innerHTML = renderPlain('vault', 'Vault',
            'SSH keys, tokens, TOTP secrets, harness credentials — anything else you want sealed. Which harness runs on which key is set in the Orchestrator app.', 'Add a Key');
    }

    // ── add ────────────────────────────────────────────────────────────────
    // The add modal only ever adds an API KEY. The other slot is a sign-in the
    // host fills by running the vendor's own login, so there is nothing here to
    // choose between — the Credential picker (and the paragraph explaining when
    // to pick OAuth) is gone with the OAuth Add button that justified it.
    function providerDynHTML(preselect) {
        var opts = state.providers.map(function (p, i) {
            var s = apiKeySlot(p);
            return '<option value="' + i + '"' + (p.provider === preselect ? ' selected' : '') + '>'
                + esc(p.provider) + (s && s.stored ? ' — key stored' : '') + '</option>';
        }).join('');
        return ''
            + '<div class="keys-field"><span class="keys-field-label">Provider</span>'
              + '<select id="add-provider" class="keys-input">' + opts + '</select></div>'
            + '<div class="keys-field"><span class="keys-field-label">Name</span>'
              + '<input id="add-name" class="keys-input" readonly aria-readonly="true">'
              + '<span class="keys-field-hint" id="add-slot-hint"></span></div>';
    }
    // The one slot on a provider the user may fill. Server-driven (slot.source),
    // so a provider that later gains or loses a user-fillable slot needs no
    // change here.
    function apiKeySlot(p) {
        var slots = (p && p.slots) || [];
        for (var i = 0; i < slots.length; i++) {
            if (slots[i].source !== 'host' && slots[i].cred_type === 'api_key') return slots[i];
        }
        return null;
    }
    function customDynHTML(hasProviders, suggest) {
        var opts = TYPES.map(function (t) { return '<option value="' + t.v + '">' + t.label + '</option>'; }).join('');
        return ''
            + '<div class="keys-field"><span class="keys-field-label">Name</span>'
              + '<input id="add-name" class="keys-input" placeholder="e.g. github-ssh, runpod, wg-bastion" autocomplete="off" value="' + esc(suggest || '') + '">'
              + (hasProviders ? '<span class="keys-field-hint">Adding a provider key? Use <em>Provider Key</em> above — it seals under the exact name your Host looks up.</span>' : '')
            + '</div>'
            + '<div class="keys-field"><span class="keys-field-label">Type</span>'
              + '<select id="add-type" class="keys-input">' + opts + '</select></div>';
    }
    function syncSlotFields() {
        var p = state.providers[+($('add-provider').value || 0)];
        if (!p) return;
        var s = apiKeySlot(p);
        $('add-name').value = s ? s.name : '';
        $('add-slot-hint').innerHTML = s && s.stored
            ? 'This slot already holds a key — sealing a new value <em>replaces</em> it.'
            : 'The canonical name your Host resolves. Fixed so the key is found first try.';
    }
    function openAdd(opts) {
        opts = opts || {};
        var hasProviders = !!(state.providers && state.providers.length);
        state.addMode = (opts.mode === 'custom' || !hasProviders) ? 'custom' : 'provider';
        openModal(opts.title || 'Add a Key', ''
            + (hasProviders
                ? '<div class="keys-mode" id="add-mode">'
                    + '<button type="button" class="keys-mode-btn" data-mode="provider">Provider Key</button>'
                    + '<button type="button" class="keys-mode-btn" data-mode="custom">Custom</button>'
                  + '</div>'
                : '')
            + '<div id="add-dyn"></div>'
            + '<div class="keys-field"><span class="keys-field-label">Value</span>'
              + '<textarea id="add-value" class="keys-input keys-textarea" placeholder="Paste the key, token, or secret. It is sealed immediately and the agent never sees it." spellcheck="false"></textarea>'
              + '<span class="keys-field-hint">Sealed to your container. The platform can\'t read it; your agent can only <em>use</em> it through the broker.</span></div>',
            [
                { label: 'Cancel', cls: 'ghost', onClick: closeModal },
                { label: 'Seal It', cls: 'primary', id: 'add-ok', onClick: saveAdd },
            ]);
        function renderDyn() {
            var modeEl = $('add-mode');
            if (modeEl) {
                Array.prototype.forEach.call(modeEl.querySelectorAll('.keys-mode-btn'), function (b) {
                    b.classList.toggle('active', b.getAttribute('data-mode') === state.addMode);
                });
            }
            if (state.addMode === 'provider') {
                $('add-dyn').innerHTML = providerDynHTML(opts.provider);
                $('add-provider').addEventListener('change', syncSlotFields);
                syncSlotFields();
            } else {
                $('add-dyn').innerHTML = customDynHTML(hasProviders, opts.suggest);
                var n = $('add-name'); if (n) n.focus();
            }
        }
        var modeEl = $('add-mode');
        if (modeEl) {
            modeEl.addEventListener('click', function (e) {
                var b = e.target.closest('.keys-mode-btn');
                if (!b || b.getAttribute('data-mode') === state.addMode) return;
                state.addMode = b.getAttribute('data-mode');
                renderDyn();
            });
        }
        renderDyn();
        setTimeout(function () {
            var el = state.addMode === 'custom' ? $('add-name') : $('add-value');
            if (el) el.focus();
        }, 0);
    }
    async function saveAdd() {
        var name, cred_type;
        if (state.addMode === 'provider') {
            var p = state.providers[+($('add-provider').value || 0)];
            var slot = apiKeySlot(p);
            if (!slot) return;
            name = slot.name;           // canonical, from the registry — never typed here
            cred_type = slot.cred_type; // always api_key: the sign-in slot is the host's
        } else {
            name = ($('add-name').value || '').trim();
            cred_type = ($('add-type').value || 'opaque').trim();
            if (!name) { $('add-name').focus(); return; }
        }
        var value = $('add-value').value || '';
        if (!value) { $('add-value').focus(); return; }
        var ok = $('add-ok'); if (ok) { ok.disabled = true; ok.textContent = 'Sealing…'; }
        try {
            await fetchJSON('/api/vault/secrets', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name, cred_type: cred_type, value_b64: toB64(value) }),
            });
            closeModal();
            await load();
        } catch (e) {
            if (ok) { ok.disabled = false; ok.textContent = 'Seal It'; }
            alert('Could not save. ' + e.message);
        }
    }

    // ── deactivate / activate — the reversible off-switch ──────────────────
    // Reversible by design, so no confirm theater: one click, the state flips,
    // and the resolver everywhere reads the key as absent until it is back.
    async function doSetDeactivated(name, deactivated) {
        try {
            await fetchJSON('/api/vault/secrets/' + encodeURIComponent(name) + '/deactivate', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ deactivated: deactivated }),
            });
            await load();
        } catch (e) { alert('Could not ' + (deactivated ? 'deactivate' : 'activate') + '. ' + e.message); }
    }

    // ── revoke ──────────────────────────────────────────────────────────────
    function confirmRevoke(name) {
        openModal('Revoke This Key', '<p class="keys-confirm">Revoke <strong>' + esc(name) + '</strong>? Anything using it stops working. If it belongs to a builder, that builder disconnects. The sealed envelope is kept (superseded) for audit, not deleted.</p>', [
            { label: 'Cancel', cls: 'ghost', onClick: closeModal },
            { label: 'Revoke', cls: 'primary danger', onClick: function () { doRevoke(name); } },
        ]);
    }
    async function doRevoke(name) {
        try {
            await fetchJSON('/api/vault/secrets/' + encodeURIComponent(name), { method: 'DELETE' });
            closeModal();
            await load();
        } catch (e) { alert('Could not revoke. ' + e.message); }
    }

    // ── reveal (TOTP-gated) ──────────────────────────────────────────────────
    // CHECK, then ask. Reveal used to open the 6-box code prompt unconditionally
    // — including for a user who has never set a factor up, who was asked for a
    // code that could not exist and only learned the truth from the 409 after
    // typing six digits (it asked for something not yet set up; it needs a
    // check, then setup if missing). GET /api/vault/stepup answers
    // that before anything is demanded, so a first-timer goes straight to the
    // QR and lands back on the reveal.
    //
    // The 409 branch in doReveal STAYS: this check is a race away from stale
    // (a factor can be dropped between the read and the post), and the correct
    // recovery is the same one. Belt and braces, cheap.
    async function startReveal(name, type) {
        var enrolled = true;
        try {
            var st = await fetchJSON('/api/vault/stepup');
            enrolled = !!st.enrolled;
        } catch (e) {
            // Status unavailable → ask for the code anyway rather than block a
            // reveal on a degraded read. The server is the real gate.
            enrolled = true;
        }
        if (!enrolled) { enrollStepup(function () { askRevealCode(name, type); }); return; }
        askRevealCode(name, type);
    }
    function askRevealCode(name, type) {
        openStepup('Reveal ' + name, 'Enter your authenticator code to reveal the raw value of ' + name + '. This is the only way to see a key — your agent never can.', function (code) {
            doReveal(name, type, code);
        });
    }
    async function doReveal(name, type, code) {
        var res;
        try {
            res = await fetchJSON('/api/vault/secrets/' + encodeURIComponent(name) + '/reveal', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ stepup_code: code }),
            });
        } catch (e) {
            if (e.status === 409) { enrollStepup(function () { startReveal(name, type); }); return; }
            alert('Could not reveal. ' + e.message); return;
        }
        var value = fromB64(res.value_b64 || '');
        if (type === 'totp_secret') {
            var uri = 'otpauth://totp/' + encodeURIComponent('Privatae:' + name) + '?secret=' + encodeURIComponent(value.trim()) + '&issuer=Privatae';
            var m = openModal(name, '<div class="keys-qr-wrap"><div class="keys-qr" id="reveal-qr"></div>'
                + '<div class="keys-reveal-row"><span class="keys-field-hint">Scan into an authenticator, or copy the secret:</span>'
                + '<code class="keys-secret" id="reveal-secret" title="Copy">' + esc(value) + '</code></div></div>',
                [{ label: 'Done', cls: 'primary', onClick: closeModal }]);
            drawQR($('reveal-qr'), uri);
            wireCopy($('reveal-secret'), value);
            void m;
        } else {
            openModal(name, '<p class="keys-field-hint">Shown once. Copy it now.</p>'
                + '<code class="keys-secret keys-secret-block" id="reveal-secret" title="Copy">' + esc(value) + '</code>',
                [{ label: 'Done', cls: 'primary', onClick: closeModal }]);
            wireCopy($('reveal-secret'), value);
        }
    }
    function wireCopy(el, value) {
        if (!el) return;
        el.addEventListener('click', function () {
            try { navigator.clipboard.writeText(value); el.classList.add('copied'); setTimeout(function () { el.classList.remove('copied'); }, 900); } catch (e) {}
        });
    }

    // ── step-up: TOTP entry, routing first-timers to enrollment ──────────────
    function openStepup(title, lead, onCode) {
        var m = openModal(title, '<div class="keys-callout">' + esc(lead) + '</div>'
            + '<div class="keys-field"><span class="keys-field-label">Authenticator code</span><div id="keys-totp-host"></div></div>', [
            { label: 'Cancel', cls: 'ghost', onClick: closeModal },
            { label: 'Confirm', cls: 'primary', id: 'keys-stepup-ok', disabled: true, onClick: function () {
                if (entry.get().length !== 6) return; var c = entry.get(); closeModal(); onCode(c);
            } },
        ]);
        var entry = totpEntry($('keys-totp-host'), function (c) { var ok = $('keys-stepup-ok'); if (ok) ok.disabled = c.length !== 6; });
        m.body.addEventListener('click', function () { var i = $('keys-totp-input'); if (i) i.focus(); });
    }

    async function enrollStepup(onDone) {
        var res;
        try {
            res = await fetchJSON('/api/network/nac/stepup/enroll', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });
        } catch (e) { alert('Could not start enrollment. ' + e.message); return; }
        var m = openModal('Set Up Your Second Factor', '<div class="keys-callout"><strong>Scan this with your authenticator app, then enter a code to confirm.</strong> This is the factor that protects revealing a key. It\'s held only in your container.</div>'
            + '<div class="keys-qr-wrap"><div class="keys-qr" id="enroll-qr"></div>'
            + '<div class="keys-reveal-row"><span class="keys-field-hint">Can\'t scan? Enter this key (tap to copy):</span>'
            + '<code class="keys-secret" id="enroll-secret" title="Copy">' + esc(res.secret || '—') + '</code></div></div>'
            + '<div class="keys-field"><span class="keys-field-label">Confirm with a code</span><div id="keys-totp-host"></div></div>', [
            { label: 'Cancel', cls: 'ghost', onClick: closeModal },
            { label: 'Confirm', cls: 'primary', id: 'keys-enroll-ok', disabled: true, onClick: function () { confirmEnroll(entry.get(), onDone); } },
        ]);
        drawQR($('enroll-qr'), res.otpauth_uri || '');
        wireCopy($('enroll-secret'), res.secret || '');
        var entry = totpEntry($('keys-totp-host'), function (c) { var ok = $('keys-enroll-ok'); if (ok) ok.disabled = c.length !== 6; });
        m.body.addEventListener('click', function (e) { if (e.target.closest('#enroll-secret')) return; var i = $('keys-totp-input'); if (i) i.focus(); });
    }
    async function confirmEnroll(code, onDone) {
        if (!code || code.length !== 6) return;
        try {
            await fetchJSON('/api/network/nac/stepup/confirm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: code }) });
        } catch (e) { alert('Could not confirm. ' + e.message); return; }
        closeModal();
        if (onDone) setTimeout(onDone, 50);
    }

    // ── sign in with xAI (2026-10-05) ──────────────────────────────────────
    // Start the vendor's own sign-in on the Host, hand the user the vendor's
    // link, and check every few seconds until the Host has the sign-in. The
    // Host keeps the sign-in pod up about 15 minutes, so the checking stops
    // there too. Both calls may answer 202 + a job when they outlast the
    // router's window; postFollow reads the job to its answer.
    var SIGNIN_POLL_MS = 3000;
    var SIGNIN_JOB_POLL_MS = 1500;
    var SIGNIN_WAIT_MS = 15 * 60 * 1000;

    function pause(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
    function httpsLink(u) { u = String(u || ''); return /^https:\/\/\S+$/i.test(u) ? u : ''; }
    function signinError(e, vendor) {
        if (!e || !e.status) return 'Your Host did not answer. Check your connection and try again.';
        if (e.status === 401 || e.status === 403) return 'This Host has not let the Keys app start a sign-in.';
        return e.message || ('The ' + vendor + ' sign-in did not work. Try again in a minute.');
    }

    async function postFollow(url, run, deadline) {
        var res = await fetchJSON(url, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
        });
        if (!res || !res.pending) return res;
        var poll = String(res.poll || '');
        if (poll.indexOf('/api/vault/jobs/') !== 0) throw new Error('The Host answered in a way Keys does not understand.');
        for (;;) {
            await pause(SIGNIN_JOB_POLL_MS);
            if (run !== signinRun || Date.now() >= deadline) return null;
            var st = await fetchJSON(poll);
            if (st.status === 'done') return st.result;
            if (st.status === 'failed') {
                var err = new Error(st.detail || 'The request failed on the Host.');
                err.status = st.http_status || 500;
                throw err;
            }
        }
    }

    function signinDialog(vendor, bodyHTML, closeLabel) {
        openModal('Sign in with ' + vendor, bodyHTML,
            [{ label: closeLabel || 'Cancel', cls: closeLabel ? 'primary' : 'ghost', onClick: closeModal }]);
    }
    function signinStop(vendor, msg) {
        signinDialog(vendor, '<p class="keys-confirm">' + esc(msg) + '</p>', 'Close');
    }
    function signinStatus(msg) {
        var el = $('signin-status');
        if (el) el.textContent = msg;
    }

    async function startSignin(provider, vendor) {
        vendor = vendor || provider;
        var run = ++signinRun;
        var deadline = Date.now() + SIGNIN_WAIT_MS;
        var base = '/api/vault/providers/' + encodeURIComponent(provider) + '/signin';
        signinDialog(vendor, '<p class="view-sub">Starting the ' + esc(vendor) + ' sign-in on your Host…</p>');
        var res;
        try { res = await postFollow(base, run, deadline); }
        catch (e) { if (run === signinRun) signinStop(vendor, signinError(e, vendor)); return; }
        if (run !== signinRun) return;
        if (!res) { signinStop(vendor, 'The ' + vendor + ' sign-in timed out. Start again for a fresh link.'); return; }
        if (res.state === 'signed_in') { signinDone(vendor, res); return; }
        if (res.state !== 'waiting') {
            signinStop(vendor, res.error || ('Your Host could not start the ' + vendor + ' sign-in. Try again in a minute.'));
            return;
        }
        var link = httpsLink(res.url);
        if (!link) { signinStop(vendor, 'The ' + vendor + ' sign-in did not give a link to open. Try again in a minute.'); return; }
        var code = String(res.user_code || '');
        signinDialog(vendor, ''
            + '<p class="view-sub">Open the ' + esc(vendor) + ' page, sign in there, and come back. This window finishes by itself.</p>'
            + '<p><a class="cseries-btn primary" href="' + esc(link) + '" target="_blank" rel="noopener noreferrer">Open ' + esc(vendor) + ' sign-in</a></p>'
            + (code ? '<p class="keys-field-hint">If the page asks for a code, enter <code class="keys-secret">' + esc(code) + '</code></p>' : '')
            + '<p class="keys-field-hint" id="signin-status">Waiting for you to finish on the ' + esc(vendor) + ' page…</p>');
        for (;;) {
            await pause(SIGNIN_POLL_MS);
            if (run !== signinRun) return;
            if (Date.now() >= deadline) { signinStop(vendor, 'The ' + vendor + ' sign-in timed out. Start again for a fresh link.'); return; }
            try { res = await postFollow(base + '/finish', run, deadline); }
            catch (e) {
                if (run !== signinRun) return;
                // Refused or gone is final; anything else is a blip — keep checking.
                if (e.status && e.status < 500) { signinStop(vendor, signinError(e, vendor)); return; }
                signinStatus(signinError(e, vendor) + ' Still checking…');
                continue;
            }
            if (run !== signinRun) return;
            if (!res) continue;   // a job that ran into the deadline: the check above ends it
            if (res.state === 'signed_in') { signinDone(vendor, res); return; }
            if (res.state === 'waiting') { signinStatus('Waiting for you to finish on the ' + vendor + ' page…'); continue; }
            if (res.state === 'unreachable') { signinStatus(res.error || ('Your Host could not reach the ' + vendor + ' sign-in just now. Still checking…')); continue; }
            signinStop(vendor, res.error || ('The ' + vendor + ' sign-in did not finish. Start again for a fresh link.'));
            return;
        }
    }

    function signinDone(vendor, res) {
        var routed = res.routed;
        signinDialog(vendor, ''
            + '<p class="keys-confirm">Signed in with ' + esc(vendor) + '.</p>'
            + (routed && routed.model ? '<p class="view-sub">Your Host now runs on ' + esc(routed.model) + '.</p>' : '')
            + (!routed && res.route_note ? '<p class="view-sub">' + esc(res.route_note) + '</p>' : ''),
            'Done');
        load();
    }

    // ── wire up — ONE delegated listener, so every button works no matter
    //    which section rendered it (a per-render rebind is how controls end up
    //    dead after a re-render). ────────────────────────────────────────────
    document.addEventListener('click', function (e) {
        var nav = e.target.closest('[data-section]');
        if (nav) { state.section = nav.getAttribute('data-section'); render(); return; }
        var btn = e.target.closest('[data-act]');
        if (!btn) return;
        var act = btn.getAttribute('data-act');
        var name = btn.getAttribute('data-name');
        if (act === 'reveal') startReveal(name, btn.getAttribute('data-type'));
        else if (act === 'revoke') confirmRevoke(name);
        else if (act === 'deactivate') doSetDeactivated(name, true);
        else if (act === 'activate') doSetDeactivated(name, false);
        else if (act === 'autofill-grant') doAutofillGrant(btn.getAttribute('data-host'));
        else if (act === 'autofill-revoke') doAutofillRevoke(btn.getAttribute('data-host'));
        else if (act === 'refresh') load();
        else if (act === 'add-provider') openAdd({ provider: btn.getAttribute('data-provider'), title: 'Add a Provider Key' });
        else if (act === 'add-custom') openAdd({ mode: 'custom', suggest: btn.getAttribute('data-suggest') || '' });
        else if (act === 'signin') startSignin(btn.getAttribute('data-provider'), btn.getAttribute('data-vendor'));
    });
    $('keys-modal-close').addEventListener('click', closeModal);
    $('keys-scrim').addEventListener('click', function (e) { if (e.target === $('keys-scrim')) closeModal(); });

    load();
}());
