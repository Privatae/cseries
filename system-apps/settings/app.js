/* ================================================================
 * Settings — app logic (v2.0)
 *
 * Three views: About / Updates / History.
 * All data sourced from local /api/system/* and /api/update/*
 * routes. The container handles all relay communication.
 * ================================================================ */

(function () {
    'use strict';

    const state = {
        view: 'overview',
        version: null,
        system: null,
        available: null,
        bundle: null,
        history: [],
    };

    // ── Navigation ───────────────────────────────────────
    function showView(view) {
        var moved = state.view !== view;
        state.view = view;
        document.querySelectorAll('.cseries-nav-btn').forEach(function (b) {
            var on = b.dataset.view === view;
            b.classList.toggle('active', on);
            b.setAttribute('aria-selected', on ? 'true' : 'false');
            if (on && moved) revealTab(b);
        });
        document.querySelectorAll('.app-view').forEach(function (v) {
            v.hidden = v.id !== 'view-' + view;
        });
        if (view === 'overview') renderOverview();
        else if (view === 'updates') renderUpdates();
        else if (view === 'history') renderHistory();
        else if (view === 'synaptive') loadSynaptive();
        else if (view === 'shaping') loadShaping();
        else if (view === 'desktop') loadDesktopTab();
        else if (view === 'backup') loadBackup();
        else if (view === 'network') loadNetwork();
        // appearance view renders from its own state; nothing to load on switch
    }

    // On a phone the tab bar scrolls sideways (cseries-components.css), so a
    // tab selected by a deep link or the cseries:app-view message can sit off
    // screen. Scroll the bar, and only the bar, to the nearest edge that
    // shows it: scrollIntoView would also scroll the shell's page around
    // this window. Only when the tab changes, so the 20 s refresh (which
    // re-shows the open tab) never pulls back a bar the person scrolled.
    function revealTab(b) {
        var nav = b.parentElement;
        if (!nav || nav.scrollWidth <= nav.clientWidth) return;
        var nr = nav.getBoundingClientRect(), br = b.getBoundingClientRect();
        if (br.left < nr.left) nav.scrollLeft -= nr.left - br.left;
        else if (br.right > nr.right) nav.scrollLeft += br.right - nr.right;
    }

    // ── Desktop & Dock ───────────────────────────────────
    // The desktop SHELL owns layout state (js/desktop-layout.js): reading
    // and writing through window.parent.DesktopLayout keeps one state
    // owner, persists via the shell's debounced save, and re-renders the
    // live desktop/dock instantly. Same parent-reach precedent as
    // _bootScreen below. Standalone (no shell parent) the tab degrades to
    // a pointer at the desktop.
    function parentLayout() {
        try {
            return (window.parent && window.parent !== window
                && window.parent.DesktopLayout) || null;
        } catch (_) { return null; }
    }

    async function loadDesktopTab() {
        var listEl = document.getElementById('desktop-app-list');
        if (!listEl) return;
        var L = parentLayout();
        if (!L) {
            listEl.innerHTML = '<div class="cseries-empty"><span class="empty-title">Shell not reachable</span>'
                + 'Open Settings from the Privatae Host desktop to manage the dock and desktop icons.</div>';
            return;
        }

        var apps = [];
        try {
            var data = await fetchJSON('/api/applications');
            apps = data.applications || [];
        } catch (_) {
            listEl.innerHTML = '<div class="cseries-empty">Could not load the app list.</div>';
            return;
        }

        var defaults = apps.filter(function (a) { return a.system; })
                           .map(function (a) { return a.id; });

        listEl.innerHTML = apps.map(function (a) {
            var pinned = L.isPinned(a.id);
            var onDesk = L.onDesktop(a.id, defaults);
            return '<div class="desktop-row" data-app-id="' + attr(a.id) + '">'
                + '<div class="desktop-row-name">' + esc(a.name || a.id) + '</div>'
                + '<div class="desktop-row-actions">'
                + '<button class="cseries-btn ghost desktop-toggle' + (pinned ? ' is-on' : '') + '" data-kind="dock">'
                +   (pinned ? '• In Dock' : 'Add to Dock') + '</button>'
                + '<button class="cseries-btn ghost desktop-toggle' + (onDesk ? ' is-on' : '') + '" data-kind="desk">'
                +   (onDesk ? '• On Desktop' : 'Add to Desktop') + '</button>'
                + '</div></div>';
        }).join('');

        listEl.querySelectorAll('.desktop-toggle').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var row = btn.closest('.desktop-row');
                var id = row && row.getAttribute('data-app-id');
                var Lp = parentLayout();
                if (!id || !Lp) return;
                if (btn.dataset.kind === 'dock') {
                    if (Lp.isPinned(id)) Lp.unpin(id); else Lp.pin(id);
                } else {
                    if (Lp.onDesktop(id, defaults)) Lp.removeFromDesktop(id, defaults);
                    else Lp.addToDesktop(id, defaults);
                }
                loadDesktopTab();
            });
        });

        var resetBtn = document.getElementById('btn-reset-layout');
        if (resetBtn && !resetBtn._wired) {
            resetBtn._wired = true;
            resetBtn.addEventListener('click', function () {
                var Lp = parentLayout();
                if (!Lp) return;
                if (!confirm('Reset the dock and desktop icons to the default layout?')) return;
                Lp.resetLayout();
                loadDesktopTab();
            });
        }
    }

    // ── Appearance ───────────────────────────────────────
    var WALLPAPERS = [
        { key: 'default',  label: 'Default',  css: null },
        { key: 'ink',      label: 'Ink',       css: 'var(--cseries-ink)' },
        { key: 'void',     label: 'Void',      css: '#050709' },
        { key: 'midnight', label: 'Midnight',  css: 'radial-gradient(circle 1200px at 50% -10%, rgba(30,50,110,0.45), transparent 65%), var(--cseries-ink)' },
        { key: 'aurora',   label: 'Aurora',    css: 'radial-gradient(circle 900px at 15% 115%, rgba(70,180,100,0.15), transparent 60%), radial-gradient(circle 900px at 85% -20%, rgba(70,100,210,0.15), transparent 60%), var(--cseries-ink)' },
        { key: 'dusk',     label: 'Dusk',      css: 'radial-gradient(circle 900px at 85% 115%, rgba(170,70,180,0.15), transparent 60%), var(--cseries-ink)' },
        { key: 'ember',    label: 'Ember',     css: 'radial-gradient(circle 1000px at 50% 115%, rgba(200,100,50,0.14), transparent 60%), var(--cseries-ink)' },
        { key: 'ice',      label: 'Ice',       css: 'radial-gradient(circle 900px at 50% -10%, rgba(60,150,220,0.2), transparent 60%), var(--cseries-ink)' },
    ];
    // Two variants share the grid renderer: 'desktop' is the wallpaper
    // everyone has; 'mobile' is the phone-mode override (null = follow
    // desktop). The shell picks the variant by viewport.
    var _wp = {
        // hasHtml: the host has designed an HTML desktop (an artifact the
        // shell renders as the surface); the
        // grid then offers it as the "Host design" swatch.
        desktop: { current: 'default', hasCustom: false, hasHtml: false },
        mobile:  { current: null,      hasCustom: false },
    };

    function _wpEls(variant) {
        var suffix = variant === 'mobile' ? '-mobile' : '';
        return {
            grid: document.getElementById('wallpaper-grid' + suffix),
            status: document.getElementById('wallpaper-status' + suffix),
        };
    }

    // Re-apply on the shell after a save: the parent's applyWallpaper()
    // re-fetches /api/appearance and picks the variant for its current
    // viewport, so this is correct for both grids (a straight
    // desktop.style write would paint the wrong variant in phone mode).
    function _reapplyShellWallpaper() {
        try {
            if (window.parent && typeof window.parent.applyWallpaper === 'function') {
                window.parent.applyWallpaper();
            }
        } catch (_) {}
    }

    async function loadAppearanceTab() {
        try {
            var data = await fetchJSON('/api/appearance');
            _wp.desktop.current = data.wallpaper || 'default';
            _wp.desktop.hasCustom = !!data.has_custom_image;
            _wp.desktop.hasHtml = !!data.has_desktop_html;
            _wp.mobile.current = data.wallpaper_mobile || null;
            _wp.mobile.hasCustom = !!data.has_custom_image_mobile;
        } catch (_) {}
        renderWallpaperGrid('desktop');
        renderWallpaperGrid('mobile');
        loadTimezone();
    }

    // ── Backup — the user's own schedule, keys and copies. ──
    //    Times shown and saved in the user's OWN time zone (the backend
    //    stores them that way): "3am" has to mean their 3am or the control
    //    is lying. The host's own backup loop reads these back and honours them.
    var _bkLoaded = false;
    async function loadBackup() {
        var freq = document.getElementById('bk-frequency');
        var when = document.getElementById('bk-when');
        var wday = document.getElementById('bk-weekday');
        var time = document.getElementById('bk-time');
        var tzNote = document.getElementById('bk-tz-note');
        var kind = document.getElementById('bk-target-kind');
        var s3box = document.getElementById('bk-target-s3');
        var mbox = document.getElementById('bk-target-machine');
        var addr = document.getElementById('bk-target-address');
        var addr2 = document.getElementById('bk-target-address2');
        var endp = document.getElementById('bk-target-endpoint');
        var cred = document.getElementById('bk-target-cred');
        var save = document.getElementById('bk-save');
        var st = document.getElementById('bk-status');
        var plat = document.getElementById('bk-platform');
        if (!freq || !save) return;

        function syncVisibility() {
            when.hidden = freq.value === 'off';
            wday.parentNode.querySelector('label[for="bk-weekday"]').hidden = freq.value !== 'weekly';
            wday.hidden = freq.value !== 'weekly';
            s3box.hidden = kind.value !== 's3';
            mbox.hidden = kind.value !== 'machine';
        }

        // One place that builds the target, so the two shapes cannot drift from
        // what the backend's validate_target accepts.
        function buildTarget() {
            if (kind.value === 's3') {
                return { kind: 's3',
                         address: (addr.value || '').trim(),
                         endpoint: (endp.value || '').trim() || null,
                         cred_name: (cred.value || '').trim() };
            }
            if (kind.value === 'machine') {
                return { kind: 'machine',
                         address: (addr2.value || '').trim(),
                         dir: 'privatae-backups' };
            }
            return null;
        }

        if (!_bkLoaded) {
            freq.onchange = syncVisibility;
            kind.onchange = syncVisibility;
            save.onclick = async function () {
                var body = {
                    frequency: freq.value,
                    hour: parseInt((time.value || '02:30').split(':')[0], 10),
                    minute: parseInt((time.value || '02:30').split(':')[1], 10),
                    weekday: parseInt(wday.value, 10),
                    target: buildTarget(),
                    platform: plat ? !!plat.checked : true,
                };
                st.textContent = 'Saving…';
                try {
                    var saved;
                    try {
                        saved = await fetchJSON('/api/backup/settings', {
                            method: 'PUT',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(body),
                        });
                    } catch (e) {
                        // A change that weakens your backups (turning them
                        // off, weekly instead of daily, out of platform
                        // storage, the second copy removed or moved) or sends
                        // them somewhere new takes your six-digit code. Asked
                        // only then, in the host's own words.
                        if (!e || e.status !== 428) throw e;
                        var code = window.prompt('Enter your six-digit code to '
                            + String(e.detail || '').replace(/^enter your six-digit code to /, '') + '.');
                        if (!code) { st.textContent = 'Not saved.'; return; }
                        body.stepup_code = code.trim();
                        saved = await fetchJSON('/api/backup/settings', {
                            method: 'PUT',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(body),
                        });
                    }
                    st.textContent = describe(saved);
                } catch (e) {
                    // Surface the backend's reason verbatim — every rejection it
                    // sends names the field, and a generic "failed" would leave
                    // the user believing a schedule that is not stored.
                    st.textContent = 'Could not save: ' + (e && e.message || e);
                }
            };
            _bkLoaded = true;
        }

        function describe(s) {
            if (!s || s.frequency === 'off') return 'Backups are turned off for this host.';
            var t = String(s.hour).padStart(2, '0') + ':' + String(s.minute).padStart(2, '0');
            var days = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
            var copy = s.target
                ? (s.target.kind === 'machine' ? s.target.address : 'your bucket (' + s.target.address + ')')
                : null;
            var wheref = s.platform === false
                ? (copy ? ' Backups go to ' + copy + ' only, not to platform storage.' : '')
                : ' Kept in platform storage' + (copy ? ', with a copy sent to ' + copy : '') + '.';
            var zone = s.timezone ? ' (' + s.timezone + ')' : ' (UTC — no time zone set)';
            return (s.frequency === 'weekly'
                ? 'Every ' + days[s.weekday] + ' at ' + t
                : 'Every day at ' + t) + zone + '.' + wheref;
        }

        var cur;
        try {
            cur = await fetchJSON('/api/backup/settings');
        } catch (e) {
            st.textContent = 'Could not load backup settings: ' + (e && e.message || e);
            return;
        }
        freq.value = cur.frequency || 'daily';
        wday.value = String(cur.weekday == null ? 0 : cur.weekday);
        time.value = String(cur.hour == null ? 2 : cur.hour).padStart(2, '0') + ':'
                   + String(cur.minute == null ? 30 : cur.minute).padStart(2, '0');
        kind.value = (cur.target && cur.target.kind) || '';
        addr.value = (cur.target && cur.target.kind === 's3' && cur.target.address) || '';
        addr2.value = (cur.target && cur.target.kind === 'machine' && cur.target.address) || '';
        endp.value = (cur.target && cur.target.endpoint) || '';
        cred.value = (cur.target && cur.target.cred_name) || '';
        if (plat) plat.checked = cur.platform !== false;
        tzNote.textContent = cur.timezone
            ? 'Times are in ' + cur.timezone + ', your host\'s time zone.'
            : 'No time zone set — times are UTC. Set one on the Appearance tab.';
        syncVisibility();
        // Never-configured: the fleet job runs its built-in default — daily,
        // in the 02:00–02:10 UTC window — NOT a user-tz slot, so don't render
        // describe(cur) here (its merged 02:30 placeholder would read as fact).
        st.textContent = cur.is_default
            ? 'Using the default: every day, between 02:00 and 03:00 UTC. '
              + 'Save a schedule here to choose your own time instead.'
            : describe(cur);

        window._bkLastKey = cur.key || {};
        renderKey(cur);
        renderRuns(cur);
        renderRestore();
        renderExports();
        renderShip(cur);

        // The bastion's local-master copy — a DIFFERENT backup from the one the
        // schedule above governs, driven by the user's own machine and not
        // decryptable here. Read-only; absent is the normal state until they
        // install a bastion, so say that plainly rather than showing an error.
        var loc = document.getElementById('bk-local');
        if (loc) {
            try {
                var lb = await fetchJSON('/api/backup/local');
                if (!lb || !lb.configured) {
                    loc.textContent = 'No bastion copy yet. Install a bastion on a machine you own '
                        + 'and it will keep its own encrypted copy that only you can open.';
                } else {
                    var mb = Math.round((lb.blob_bytes || 0) / 1048576);
                    loc.textContent = (lb.blob_count || 0) + ' items kept (' + mb + ' MB)'
                        + (lb.last_sync_at ? ', last synced ' + new Date(lb.last_sync_at).toLocaleString() : '')
                        + '. Recovery code: ' + ((lb.envelopes && lb.envelopes.recovery_code) ? 'set' : 'not set') + '.';
                }
            } catch (e) {
                loc.textContent = 'Could not read bastion backup state: ' + (e && e.message || e);
            }
        }

        // The memory mirror — a live PostgreSQL replica of this host's database
        // on a machine the user owns. The controls live HERE and only here:
        // turning one on hands that machine a complete copy of the memory, so
        // it takes the user's authenticator code, and the Host is deliberately
        // walled off the write (kernel_ops._WALLED_OPS) — it can read the state
        // and say what it sees, but it cannot switch the mirror on or off.
        //
        // Copy rule for this block: never imply a copy exists, is current, or
        // is recoverable unless the primary's own catalogs say so. Every string
        // below maps to one `health` the server computed; none of them guess.
        var mir = document.getElementById('bk-mirror');
        if (mir) {
            try {
                var m = await fetchJSON('/api/backup/mirror');
                if (!m || !m.configured) {
                    mir.textContent = 'No mirror yet. A machine you own can keep a live copy of '
                        + 'your memory — it needs Docker or Podman installed, and a bastion paired '
                        + 'to this host. Choose it below.';
                    renderMirrorSetup(m || {});
                } else {
                    var who = (m.bastion && m.bastion.name) || 'your machine';
                    var lag = (m.stream && m.stream.lag_bytes);
                    var behind = (lag == null) ? '' : (lag < 1048576
                        ? Math.max(1, Math.round(lag / 1024)) + ' KB behind'
                        : Math.round(lag / 1048576) + ' MB behind');
                    var seen = (m.bastion && m.bastion.last_seen_at)
                        ? new Date(m.bastion.last_seen_at).toLocaleString() : null;
                    // Whether a COPY EXISTS is a different question from whether
                    // it is current, and only the server can answer it —
                    // copy.exists is set the first time a stream was actually
                    // observed. Before that there is nothing on that machine to
                    // reassure anyone about, and saying otherwise is the lie
                    // this block used to tell.
                    var haveCopy = !!(m.copy && m.copy.exists);
                    var copySince = (m.copy && m.copy.first_seen_at)
                        ? new Date(m.copy.first_seen_at).toLocaleString() : null;
                    var rep = (m.copy && m.copy.bastion_report) || null;
                    if (m.bastion && m.bastion.revoked) {
                        mir.textContent = 'The machine holding this mirror is no longer enrolled. '
                            + (haveCopy
                                ? 'Its copy is still on that machine and still yours; nothing new is being sent to it.'
                                : 'It never finished a first copy, so there is nothing kept there.');
                    } else if (m.health === 'unknown') {
                        mir.textContent = 'Could not check the mirror just now. This says nothing '
                            + 'about whether it is working — try again in a moment.';
                    } else if (m.health === 'streaming') {
                        mir.textContent = 'Up to date on ' + who
                            + (behind ? ' (' + behind + ')' : '') + '. Every change reaches it as you make it.';
                    } else if (m.health === 'catching_up') {
                        mir.textContent = 'Catching up on ' + who
                            + (behind ? ' — ' + behind : '') + '. It will be current shortly.';
                    } else if (m.health === 'needs_reseed') {
                        // The reseed is real: observe() recuts the slot and bumps
                        // copy.reseed_generation, which is what the machine watches.
                        // It only happens while that machine is reachable, so do not
                        // promise it is under way when it cannot be.
                        mir.textContent = who + ' fell far enough behind that the saved changes '
                            + 'were released, so it needs to take a fresh full copy. '
                            + ((m.bastion && m.bastion.online)
                                ? 'That has been started.'
                                : 'It will start once that machine is back online.')
                            + (haveCopy
                                ? ' The copy already there still opens; it just stops at '
                                  + (copySince ? 'what it had' : 'where it left off') + '.'
                                : '');
                    } else if (!haveCopy) {
                        // Never streamed. The honest state is "not yet", and the
                        // machine's own report is the only thing that can say why.
                        mir.textContent = who + ' has not finished its first copy yet'
                            + (rep && rep.error ? ' — ' + rep.error
                               : rep && rep.state ? ' (' + rep.state + ')' : '')
                            + '. Nothing is kept there until it does.';
                    } else {
                        mir.textContent = who + ' has not connected recently'
                            + (seen ? ' (last seen ' + seen + ')' : '')
                            + '. The copy on that machine'
                            + (rep && rep.replay_lsn ? ' (up to ' + rep.replay_lsn + ')' : '')
                            + ' is safe; new changes will reach it when it comes back.';
                    }
                    // Only say a version when the database container actually
                    // published one — an older DB image publishes nothing, and
                    // inventing a version here would send someone chasing a
                    // mismatch that does not exist.
                    var img = m.primary && m.primary.db_image;
                    if (img && img.known && img.tag) {
                        mir.textContent += ' Mirror runs database build ' + img.tag + '.';
                    }
                    renderMirrorOff(m);
                }
            } catch (e) {
                mir.textContent = 'Could not read mirror state: ' + (e && e.message || e);
            }
        }
    }

    // ── Mirror setup: pick a machine, confirm with 2FA ──────────────────────
    // This is the ONLY way a mirror gets switched on. Enabling hands that
    // machine a complete, continuously updated copy of the memory, so it takes
    // the user's authenticator code — and the Host cannot do it at all: the
    // write methods are walled from kernel_op, so a manipulated agent can read
    // the state and never change it.
    function renderMirrorSetup(m) {
        var box = document.getElementById('bk-mirror-controls');
        if (!box) return;
        // A control that the server would refuse is worse than no control.
        if (m.can_enable === false) {
            box.innerHTML = '<div class="label-sys">' + esc(m.blocked_reason
                || 'This host cannot hold a mirror yet.') + '</div>';
            return;
        }
        var all = m.candidates || [];
        var ready = all.filter(function (c) { return c.capable && c.online; });
        if (!ready.length) {
            // Say WHICH obstacle it is. "No machines available" sends someone
            // hunting; naming the reason is a next step.
            var why = 'No paired machine can hold a mirror yet. Pair a bastion in the Network app first.';
            if (all.length) {
                var offline = all.filter(function (c) { return c.capable && !c.online; });
                var incapable = all.filter(function (c) { return !c.capable; });
                if (offline.length) {
                    why = esc(offline[0].name) + ' can hold a mirror but is offline right now.';
                } else if (incapable.length) {
                    why = esc(incapable[0].name) + ' needs Docker or Podman installed before it '
                        + 'can hold a mirror. Install one and it will offer itself on its next check-in.';
                }
            }
            box.innerHTML = '<div class="label-sys">' + why + '</div>';
            return;
        }
        var opts = ready.map(function (c) {
            return '<option value="' + attr(c.token_id) + '">' + esc(c.name) + '</option>';
        }).join('');
        box.innerHTML =
            '<label class="label-sys" for="bk-mirror-target">Keep a copy on</label>'
            + '<select id="bk-mirror-target" class="cseries-input">' + opts + '</select>'
            + '<label class="label-sys" for="bk-mirror-code" style="margin-top:var(--space-2)">'
            + 'Authenticator code</label>'
            + '<input id="bk-mirror-code" class="cseries-input" inputmode="numeric" '
            + 'autocomplete="one-time-code" maxlength="6" placeholder="000000">'
            + '<button id="bk-mirror-on" class="cseries-btn primary" style="margin-top:var(--space-2)" disabled>'
            + 'Start keeping a copy</button>'
            + '<div class="label-sys" id="bk-mirror-msg" style="margin-top:var(--space-2)"></div>';

        var code = document.getElementById('bk-mirror-code');
        var btn = document.getElementById('bk-mirror-on');
        code.addEventListener('input', function () {
            code.value = (code.value || '').replace(/\D/g, '').slice(0, 6);
            btn.disabled = code.value.length !== 6;
        });
        btn.addEventListener('click', async function () {
            var msg = document.getElementById('bk-mirror-msg');
            btn.disabled = true;
            msg.textContent = 'Setting up…';
            try {
                await fetchJSON('/api/backup/mirror', {
                    method: 'PUT', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        enabled: true,
                        token_id: parseInt(document.getElementById('bk-mirror-target').value, 10),
                        stepup_code: code.value,
                    }),
                });
                await loadBackup();
            } catch (e) {
                // Prefer the server's own sentence (e.detail) over the wrapped
                // one-line message — it is written for the user and says which
                // of several refusals this is. 403 additionally means the code
                // was rejected, and the commonest cause is having no second
                // factor at all, which is fixed in a different app.
                if (e && e.status === 403) {
                    msg.textContent = 'That code was not accepted. If you have not set up '
                        + 'two-factor authentication yet, do that in the Keys app first — a '
                        + 'mirror cannot be turned on without it.';
                } else if (e && e.status === 409) {
                    msg.textContent = e.detail || e.message;
                } else {
                    msg.textContent = 'Could not start the mirror. '
                        + ((e && (e.detail || e.message)) || e);
                }
                btn.disabled = false;
            }
        });
    }

    function renderMirrorOff(m) {
        var box = document.getElementById('bk-mirror-controls');
        if (!box) return;
        var open = (m.copy && m.copy.open_with) || null;
        box.innerHTML =
            (open ? '<div class="label-sys">Open your copy on that machine with '
                + '<code>' + esc(open) + '</code></div>' : '')
            + '<button id="bk-mirror-off" class="cseries-btn" style="margin-top:var(--space-2)">'
            + 'Stop keeping a copy</button>'
            + '<div class="label-sys" id="bk-mirror-msg" style="margin-top:var(--space-2)"></div>';
        document.getElementById('bk-mirror-off').addEventListener('click', async function () {
            var msg = document.getElementById('bk-mirror-msg');
            // Turning it off takes the owner's code: it ends a live copy of the
            // memory on their own machine. The copy over there stops where it is.
            var code = window.prompt('Stop sending new changes to ' + ((m.bastion && m.bastion.name) || 'that machine')
                + '?\n\nThe copy already on it stays and still opens. Starting again later '
                + 'takes a fresh full copy.\n\nEnter your six-digit code to stop it.');
            if (!code) return;
            msg.textContent = 'Stopping…';
            try {
                await fetchJSON('/api/backup/mirror', {
                    method: 'DELETE',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ stepup_code: code.trim() }),
                });
                await loadBackup();
            } catch (e) {
                msg.textContent = 'Could not stop the mirror. ' + (e && e.message || e);
            }
        });
    }

    // ── The second copy: what has actually reached the user's own place. ──
    //    Three facts, kept apart, because collapsing them is how a surface
    //    tells someone they have a backup they do not have:
    //      is there an artifact to send at all,
    //      what did the last attempt DO,
    //      and if it failed, which KIND of failure was it.
    //    The failure class comes from the backend as a sentence
    //    (backup_ship.REASONS) so this file cannot drift from it, and the far
    //    end's own words sit underneath for anyone chasing a bucket policy.
    var _bkShipWired = false;

    function renderShip(cur) {
        var card = document.getElementById('bk-ship-card');
        var el = document.getElementById('bk-ship-state');
        var btn = document.getElementById('bk-ship-now');
        if (!card || !el) return;
        if (!cur || (!cur.target && !cur.target_problem)) { card.hidden = true; return; }
        card.hidden = false;

        var sh = cur.ship || {};
        var lines = [];
        // A destination saved by an older build can name a kind that no longer
        // exists. The read path degrades it to null and explains why; say so
        // here rather than silently showing "no destination".
        if (cur.target_problem) lines.push(cur.target_problem + '.');
        if (sh.available === false) {
            lines.push('Nothing to send yet — a copy is prepared after your next backup.');
        } else if (sh.available === null) {
            lines.push('Cannot read the state of your second copy right now.');
        }
        var last = sh.last;
        if (!last) {
            if (sh.available) lines.push('Ready to send. Nothing has been sent yet.');
        } else if (last.state === 'done') {
            var mb = Math.round((last.bytes || 0) / 1048576);
            lines.push('Last copy sent ' + new Date(last.at).toLocaleString()
                       + ' to ' + last.destination + ' (' + mb + ' MB).');
        } else if (last.state === 'running') {
            lines.push('Sending to ' + last.destination + ' now…');
        } else {
            // NEVER a silent green, and never a bare exception either.
            lines.push('Last attempt ' + new Date(last.at).toLocaleString()
                       + ' to ' + last.destination + ' did not finish: '
                       + (last.reason_text || 'the transfer failed') + '.');
            if (last.detail) lines.push(last.detail);
        }
        // A backup too large to be brought back from a machine to restore
        // (server/routes/backup.py _ship_limit_note).
        if (sh.note) lines.push(sh.note);
        el.textContent = says(lines, ' ');

        if (_bkShipWired) return;
        _bkShipWired = true;
        if (btn) btn.onclick = async function () {
            btn.disabled = true;
            el.textContent = 'Sending…';
            try {
                // Starts and returns; a transfer can take hours and the request
                // would die at a proxy timeout long before it finished. Progress
                // comes from the settings poll below.
                var r = await fetchJSON('/api/backup/ship', { method: 'POST' });
                el.textContent = r.note || 'Sending…';
                setTimeout(loadBackup, 4000);
            } catch (e) {
                // fetchJSON puts the body in the message; the backend sends
                // {reason, reason_text, detail} so there is always a sentence.
                el.textContent = 'Could not send: ' + (e && e.message || e);
            } finally {
                btn.disabled = false;
                loadBackup();
            }
        };
    }

    // ── Keys, runs and restore (server/backup_key.py, host_backup.py,
    //    host_restore.py). A backup is encrypted on the host to the keys the
    //    user registered and to nothing else, so every sentence below is about
    //    THOSE keys: who they are, whether a backup could be written at all,
    //    and, for a restore, whether this backup's key has been sealed to this
    //    host yet. The host never holds a key that opens a backup by itself.
    var _bkKeyWired = false, _bkRunWired = false, _bkRestoreWired = false;

    // What the host says can carry names from a backup (a path, a link, a
    // machine's words), and a name can hold a line break, another control
    // character, or one that reorders text. The run list is shown with
    // white-space: pre-line, so each line is made one line before the lines
    // are joined, here and in every other backup status (the same set as
    // server/host_backup.py unshowable).
    function oneLine(s) {
        return String(s == null ? '' : s).replace(/[\u0000-\u001f\u007f-\u009f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g, ' ');
    }

    function says(lines, sep) {
        return lines.map(oneLine).join(sep);
    }

    function sizeOf(bytes) {
        var mb = (bytes || 0) / 1048576;
        return mb >= 1024 ? (mb / 1024).toFixed(1) + ' GB' : Math.max(1, Math.round(mb)) + ' MB';
    }

    // What a host with no saved recovery key is told, everywhere it matters.
    var NO_KEY = 'Not backed up until you save your recovery key and confirm it here: make it on your '
        + 'account page (Hosts, this host), save it, then check its fingerprint below against the one on '
        + 'the key you saved and use it, with a code from your second factor. Your host backs up from then on.';

    // Using a recovery key takes a code from the second factor; the owner
    // enrols one in the Network app (its banner's "Second factor: Enroll").
    var ENROL_FIRST = 'You have not set up a second factor yet, and using a recovery key needs a code '
        + 'from one: open the Network app, click "Second factor: Enroll" at the top, scan the code with '
        + 'an authenticator app, then come back here.';

    function keysSentence(recips) {
        var r = recips || [];
        if (!r.length) return '';
        var names = r.map(function (k) {
            if (k.kind === 'host-recovery') return 'your recovery key';
            if (k.kind === 'bastion') return 'the key on ' + (k.label || 'a bastion');
            return (k.label || k.kind) + "'s key";
        });
        return ' Encrypted to ' + (names.length === 1 ? names[0]
            : names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1]) + '.';
    }

    // The links an unverified restore left out (restore.js linksLeftOutSentence).
    // Without that module the count is still said.
    function leftOut(links, count) {
        var S = window.SettingsRestore;
        if (S && S.linksLeftOutSentence) return S.linksLeftOutSentence(links, count);
        var n = Math.max((links || []).length, count || 0);
        return n ? n + ' link(s) in the backup were left out: this host could not confirm it made the backup.' : '';
    }

    function runLine(r) {
        var when = r.started_at ? new Date(r.started_at).toLocaleString() : '';
        var why = { schedule: 'on schedule', 'catch-up': 'catching up after sleep',
                    quiet: 'after you finished using it', manual: 'you asked',
                    export: 'for an export you asked for',
                    'before-restore': 'before a restore' }[r.reason] || r.reason;
        var p = r.parts || {};
        if (r.reason === 'restore') {
            var rs = p.restored || {};
            var from = rs.taken_at;
            var lo = leftOut(rs.links_left_out, rs.links_left_out_count);
            return when + ' — ' + (r.state === 'ok'
                ? 'Restored to the backup of ' + (from ? new Date(from).toLocaleString() : 'an earlier time') + '.'
                    + (lo ? ' ' + lo : '')
                : 'A restore did not happen.' + (r.error ? ' ' + r.error + '.' : ''));
        }
        if (r.state === 'running') return when + ' — Backing up now (' + why + ').';
        if (r.state === 'no_recipient') {
            return when + ' — ' + NO_KEY;
        }
        var saved = [];
        if (p.db && p.db.ok) saved.push('memory and data ' + sizeOf(p.db.bytes));
        if (p.apps && (p.apps.ok || p.apps.files)) saved.push('apps and files ' + sizeOf(p.apps.bytes));
        if (p.gitea && p.gitea.repos) saved.push(p.gitea.repos + ' Git repositor' + (p.gitea.repos === 1 ? 'y' : 'ies'));
        if (r.state === 'failed') {
            return when + ' — The backup did not happen (' + why + ')' + (r.error ? ': ' + r.error : '') + '.'
                + ' Your host tries again in half an hour.';
        }
        var head = r.state === 'ok' ? 'Backed up' : 'Partly backed up';
        var where = r.stored === 'staged' ? ' It is sent on to your own copy.' : '';
        return when + ' — ' + head + ' (' + why + ')'
            + (saved.length ? ': ' + saved.join(', ') + '.' : '.')
            + keysSentence(r.recipients) + where
            + (r.state === 'partial' && r.error ? ' Not everything was saved: ' + r.error + '.' : '');
    }

    async function renderRuns(cur) {
        var box = document.getElementById('bk-runs');
        var btn = document.getElementById('bk-run-now');
        if (!box) return;
        var data;
        try {
            data = await fetchJSON('/api/backup/runs');
        } catch (e) {
            box.textContent = 'Could not read your backups: ' + (e && e.message || e);
            return;
        }
        var runs = data.runs || [];
        var lines = runs.slice(0, 5).map(runLine);
        if (data.problem) {
            lines.unshift(/recovery key/.test(data.problem)
                ? NO_KEY : 'Your host cannot back up right now: ' + data.problem + '.');
        } else if (!runs.length) {
            lines.push('No backups yet. Your host takes its first one at its next scheduled time.');
        }
        box.textContent = says(lines, '\n');
        box.style.whiteSpace = 'pre-line';
        if (btn) btn.disabled = !!data.running || !!data.problem;
        if (!btn || _bkRunWired) return;
        _bkRunWired = true;
        btn.onclick = async function () {
            btn.disabled = true;
            try {
                await fetchJSON('/api/backup/runs', { method: 'POST' });
                box.textContent = 'Backing up now. This list updates when it finishes.';
                setTimeout(function () { renderRuns(); }, 15000);
            } catch (e) {
                box.textContent = 'Could not start a backup: ' + (e && e.message || e);
                btn.disabled = false;
            }
        };
    }

    // ── Restore (GET/POST/DELETE /api/backup/restore, POST /restore/key and
    //    /restore/fetch). One button that does the next thing the chosen backup
    //    needs: bring it back from a bastion, get its key sealed to this host,
    //    or restore it. The host restarts to swap it in, so this card follows
    //    the progress and survives the restart.
    var _bkRestore = null;

    // What a restore does and does not bring back, said before the owner
    // agrees to it (server/host_restore.py CARRIED_TABLES and its list of
    // what is deliberately left as the backup had it).
    var RESTORE_ACK = 'I understand that my host goes back to this backup and restarts to do it. '
        + 'Its memory, files and credentials (the Keys app, /app/secrets) come back as they were in '
        + 'the backup, including any credential I deleted since, and apps I uninstalled since come back. '
        + 'Access stays as it is now: my bastions, grants, second factor and backup keys are not '
        + 'changed by the restore.';

    // A backup whose key file this host cannot verify as its own
    // (server/host_restore.py UNVERIFIED_TEXT).
    var UNVERIFIED = 'This host cannot confirm it made this backup. That is expected for a backup of '
        + 'a host you replaced; otherwise it may not be yours. Restore it only if you know where it came from.';

    function restoreStep(b) {
        if (!b) return null;
        if (b.where === 'machine' && !(b.push && b.push.state === 'done')) return 'fetch';
        if (!b.key_ready) return 'key';
        return 'restore';
    }

    async function renderRestore() {
        var st = document.getElementById('bk-restore-state');
        var form = document.getElementById('bk-restore-form');
        var pick = document.getElementById('bk-restore-pick');
        var cancelRow = document.getElementById('bk-restore-cancel-row');
        if (!st) return;
        var data;
        try {
            data = await fetchJSON('/api/backup/restore');
        } catch (e) {
            st.textContent = 'Could not read what can be restored: ' + (e && e.message || e);
            return;
        }
        _bkRestore = data;
        form.hidden = true;
        cancelRow.hidden = true;
        var at = function (t) { return t ? new Date(t).toLocaleString() : 'an earlier time'; };
        var pending = data.pending;
        if (pending && pending.state && pending.state !== 'failed') {
            st.textContent = 'Your host is restarting onto the backup of ' + at(pending.taken_at) + '.';
            cancelRow.hidden = pending.state !== 'staged';
            setTimeout(renderRestore, 10000);
            return;
        }
        var p = data.progress || {};
        if (p.state === 'staging') {
            st.textContent = 'Restoring: ' + (p.step || 'working') + '… Your host restarts when this is done.';
            setTimeout(renderRestore, 10000);
            return;
        }
        var lines = [];
        if (p.state === 'ok' && p.taken_at) {
            lines.push('Restored to the backup of ' + at(p.taken_at) + '.');
            var lo = leftOut(p.links_left_out, p.links_left_out_count);
            if (lo) lines.push(lo);
        }
        if (p.state === 'failed' && p.error) lines.push('The last restore did not happen: ' + p.error + '.');
        (data.problems || []).forEach(function (m) { lines.push(m.charAt(0).toUpperCase() + m.slice(1) + '.'); });
        if (data.problem) {
            lines.push('Your host cannot restore right now: ' + data.problem + '.');
            st.textContent = says(lines, ' ');
            return;
        }
        var backups = data.backups || [];
        var chosen = pick.value;
        pick.innerHTML = '';
        backups.forEach(function (b) {
            var o = document.createElement('option');
            o.value = b.stamp;
            var where = b.where === 'machine' ? ' — on ' + (b.machine || 'your machine')
                : (b.where === 's3' ? ' — in your own storage' : '');
            o.textContent = at(b.taken_at) + ' — ' + sizeOf(b.bytes) + where
                + (b.key_ready ? (b.verified === false ? ' — key ready, not confirmed as this host\'s' : ' — key ready') : '');
            pick.appendChild(o);
        });
        if (chosen) pick.value = chosen;
        if (!backups.length) {
            lines.push('No backups to restore from yet.');
            st.textContent = says(lines, ' ');
            return;
        }
        var waiting = backups.some(function (b) {
            return (b.reseal && b.reseal.state === 'pending') || (b.push && b.push.state === 'pending')
                || b.hash_pending;
        });
        if (waiting) setTimeout(renderRestore, 15000);
        st.textContent = lines.length ? says(lines, ' ') : 'Choose the backup to go back to.';
        form.hidden = false;
        var bsel = document.getElementById('bk-restore-bastion');
        bsel.innerHTML = '';
        (data.bastions || []).forEach(function (x) {
            var o = document.createElement('option');
            o.value = String(x.token_id);
            o.textContent = x.name;
            bsel.appendChild(o);
        });
        syncRestore();
        pick.onchange = syncRestore;
        if (_bkRestoreWired) return;
        _bkRestoreWired = true;
        wireRestore();
    }

    function chosenBackup() {
        var pick = document.getElementById('bk-restore-pick');
        return ((_bkRestore && _bkRestore.backups) || []).filter(function (b) {
            return b.stamp === pick.value;
        })[0] || null;
    }

    function syncRestore() {
        var b = chosenBackup();
        var step = restoreStep(b);
        var note = document.getElementById('bk-restore-pick-note');
        var keyrow = document.getElementById('bk-restore-keyrow');
        var ackRow = document.getElementById('bk-restore-ack-row');
        var go = document.getElementById('bk-restore-go');
        var scan = document.getElementById('bk-restore-scan');
        var hasBastion = !!((_bkRestore && _bkRestore.bastions) || []).length;
        keyrow.hidden = step !== 'key' && step !== 'fetch';
        go.disabled = step === 'fetch' && !!b.hash_pending;
        ackRow.hidden = step !== 'restore';
        document.getElementById('bk-restore-reckey').parentNode.hidden = false;
        if (scan) scan.hidden = !(step === 'key' && window.SettingsRestore && window.SettingsRestore.canScan());
        var lines = [];
        if (step === 'fetch') {
            lines.push('This backup is only on ' + (b.machine || 'your machine') + '. First that machine puts it back '
                + 'into your host\'s staging folder, still encrypted.');
            if (b.token_id) document.getElementById('bk-restore-bastion').value = String(b.token_id);
            if (b.hash_pending) lines.push('That machine is still checking this copy; it is ready in a minute or two.');
            if (b.too_big) {
                // One PUT holds at most 5 GiB (server/host_restore.py PUSH_BACK_MAX).
                lines.push('This copy is larger than 5 GiB, the most your machine can put back through platform '
                    + 'storage in one piece, so it cannot be restored from here. Open it on your machine with '
                    + 'your recovery key instead (the README inside says how).');
                go.disabled = true;
            }
            if (b.push && b.push.state === 'pending') lines.push('Waiting for your machine…');
            if (b.push && b.push.error) lines.push(b.push.error + '.');
            go.textContent = 'Bring it back';
        } else if (step === 'key') {
            lines.push('Your host cannot open this backup until its key is sealed to this host. Give this page '
                + 'your recovery key and your browser does it; or choose a bastion and approve it there.');
            if (b.reseal && b.reseal.state === 'pending') {
                lines.push('Waiting for ' + (b.reseal.bastion || 'your bastion') + '. On that machine run '
                    + '"privatae-bastion backup requests", then "privatae-bastion backup approve '
                    + (b.reseal.job_id || '<id>') + '".');
            }
            if (b.reseal && b.reseal.error) lines.push(b.reseal.error + '.');
            if (!hasBastion) lines.push('No bastion is connected to this host, so use your recovery key.');
            go.textContent = 'Get the key';
        } else if (step === 'restore') {
            lines.push('The key for this backup is sealed to this host.');
            if (b.verified === false) lines.push(UNVERIFIED);
            go.textContent = 'Restore';
        }
        note.textContent = says(lines, ' ');
        document.getElementById('bk-restore-ack-text').textContent = RESTORE_ACK;
    }

    function wireRestore() {
        var go = document.getElementById('bk-restore-go');
        var status = document.getElementById('bk-restore-status');
        var reckey = document.getElementById('bk-restore-reckey');
        function say(m) { if (status) status.textContent = m; }
        var scan = document.getElementById('bk-restore-scan');
        if (scan) scan.onclick = async function () {
            try {
                say('Hold the recovery key\'s QR code up to the camera…');
                var text = await window.SettingsRestore.scanQr(document.getElementById('bk-restore-video'));
                if (text) { reckey.value = text; say('Recovery key read. It stays in this page.'); }
                else say('No recovery key was read. Paste it instead.');
            } catch (e) {
                say('The camera could not be used: ' + (e && e.message || e) + '. Paste the key instead.');
            }
        };
        go.onclick = async function () {
            var b = chosenBackup();
            if (!b) { say('Choose a backup first.'); return; }
            var code = (document.getElementById('bk-restore-totp').value || '').trim();
            if (!code) { say('Enter your six-digit code.'); return; }
            var step = restoreStep(b);
            var tokenId = parseInt(document.getElementById('bk-restore-bastion').value || '0', 10);
            go.disabled = true;
            say('Working…');
            try {
                if (step === 'fetch') {
                    if (!tokenId) { say('Choose the bastion that holds it.'); return; }
                    await fetchJSON('/api/backup/restore/fetch', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ stepup_code: code, stamp: b.stamp, token_id: tokenId }),
                    });
                    say('Asked. Your machine sends it back; this card follows.');
                } else if (step === 'key' && (reckey.value || '').trim()) {
                    if (!window.SettingsRestore) { say('This page could not load its restore module. Reload it.'); return; }
                    // The recovery key goes into the page's own module and
                    // nowhere else: only the resealed file is sent.
                    var key = reckey.value;
                    reckey.value = '';
                    await window.SettingsRestore.restoreKeyInBrowser({
                        fetchJSON: fetchJSON, stamp: b.stamp, identity: key, stepupCode: code,
                        confirmOtherHost: function (from, here) {
                            return window.confirm('This backup is from Host #' + from + '. This is Host #'
                                + here + '. Restore it here, in place of everything on this host?');
                        } });
                    key = null;
                    say('The backup\'s key is sealed to this host. Now restore it.');
                } else if (step === 'key') {
                    if (!tokenId) { say('Give this page your recovery key, or choose a bastion.'); return; }
                    await fetchJSON('/api/backup/restore/key', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ stepup_code: code, stamp: b.stamp, via: 'bastion', token_id: tokenId }),
                    });
                    say('Asked. Approve it on your bastion; this card follows.');
                } else {
                    var ack = document.getElementById('bk-restore-ack');
                    if (!ack.checked) { say('Tick the box first.'); return; }
                    // A backup this host cannot prove it made (host_restore
                    // provenance): never restored without the owner saying so.
                    var unverified = b.verified === false;
                    if (unverified && !window.confirm(UNVERIFIED + ' Restore it anyway?')) {
                        say('Not restored.');
                        return;
                    }
                    await fetchJSON('/api/backup/restore', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ stepup_code: code, stamp: b.stamp, unverified_ok: unverified }),
                    });
                    ack.checked = false;
                    say('');
                }
                document.getElementById('bk-restore-totp').value = '';
                setTimeout(renderRestore, 1500);
            } catch (e) {
                say('Could not do that: ' + (e && e.message || e));
            } finally {
                go.disabled = false;
            }
        };
        document.getElementById('bk-restore-cancel').onclick = async function () {
            try {
                await fetchJSON('/api/backup/restore', { method: 'DELETE' });
                renderRestore();
            } catch (e) {
                say('Could not cancel the restore: ' + (e && e.message || e));
            }
        };
    }

    // ── Exports asked for on the account page. Built only for a key the owner
    //    confirms here (server/host_export.py): the fingerprint must match the
    //    one in their export key file (the account page is the platform's).
    async function renderExports() {
        var box = document.getElementById('bk-exports');
        var card = document.getElementById('bk-exports-card');
        if (!box || !card) return;
        var data;
        try { data = await fetchJSON('/api/backup/exports'); }
        catch (e) { card.hidden = true; return; }
        var pending = (data.pending || []).filter(function (p) { return p.fingerprint; });
        card.hidden = !pending.length && !data.problem;
        box.innerHTML = '';
        if (data.problem) box.textContent = 'Could not check for exports: ' + data.problem + '.';
        pending.forEach(function (p) {
            var row = document.createElement('div');
            row.className = 'label-sys';
            row.style.marginTop = 'var(--space-2)';
            row.textContent = 'Confirm the export with fingerprint ' + p.fingerprint
                + (p.requested_at ? ' (asked for ' + new Date(p.requested_at).toLocaleString() + ')' : '')
                + ' only if it matches the fingerprint written in your export key file. ';
            var btn = document.createElement('button');
            btn.className = 'cseries-btn';
            btn.textContent = 'Confirm this export';
            btn.onclick = async function () {
                var code = (document.getElementById('bk-exports-totp').value || '').trim();
                var st = document.getElementById('bk-exports-status');
                if (!code) { st.textContent = 'Enter your six-digit code.'; return; }
                btn.disabled = true;
                try {
                    var r = await fetchJSON('/api/backup/exports/confirm', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ stepup_code: code, id: p.id, fingerprint: p.fingerprint }),
                    });
                    document.getElementById('bk-exports-totp').value = '';
                    st.textContent = r.note || 'Confirmed.';
                    renderExports();
                } catch (e) {
                    st.textContent = 'Could not confirm: ' + (e && e.message || e);
                } finally {
                    btn.disabled = false;
                }
            };
            row.appendChild(btn);
            box.appendChild(row);
        });
    }

    function renderKey(cur) {
        var k = (cur && cur.key) || {};
        var st = document.getElementById('bk-key-state');
        var list = document.getElementById('bk-key-list');
        var offerBox = document.getElementById('bk-key-offer');
        if (!st) return;
        if (k.registered === null || k.registered === undefined) {
            st.textContent = 'Cannot read your keys right now' + (k.reason ? ' (' + k.reason + ').' : '.');
            return;
        }
        var rec = k.recovery;
        st.textContent = rec
            ? 'Your recovery key: ' + rec.fingerprint + (rec.label ? ' (' + rec.label + ')' : '')
              + '. Every backup is encrypted to it; you keep it, and Privatae never has it.'
            : NO_KEY + (k.stepup_enrolled === false ? ' ' + ENROL_FIRST : '');
        var offer = k.recovery_offer;
        offerBox.hidden = !offer;
        if (offer) {
            // Nothing the platform sends is used on its word, the first key
            // included (server/host_backup.py pin_recovery_key).
            document.getElementById('bk-key-offer-text').textContent = rec
                ? 'Your account page now holds a different recovery key for this host: ' + offer.fingerprint
                  + ' (this host uses ' + rec.fingerprint + '). If you made a new recovery key and its '
                  + 'fingerprint is this one, use it here. If you did not, do not: a key you did not make '
                  + 'should never receive your backups. Backups taken before stay encrypted to the old key.'
                : 'Your account page holds a recovery key for this host with the fingerprint ' + offer.fingerprint
                  + '. Compare it with the fingerprint on the recovery key you saved (the file and the '
                  + 'printout carry it). Use it here only if they match: a key you did not make should never '
                  + 'receive your backups. Until you do, this host takes no backups. Using it asks for a '
                  + 'six-digit code from your second factor.';
            if (k.stepup_enrolled === false) {
                document.getElementById('bk-key-offer-text').textContent += ' ' + ENROL_FIRST;
            }
            document.getElementById('bk-key-offer-adopt').textContent = rec
                ? 'Use the new recovery key' : 'The fingerprints match: use this key';
        }
        list.innerHTML = '';
        (k.recovery_older || []).forEach(function (o) {
            var row = document.createElement('div');
            row.className = 'label-sys';
            row.textContent = 'Earlier recovery key ' + o.fingerprint
                + (o.created_at ? ' (from ' + new Date(o.created_at).toLocaleDateString() + ')' : '')
                + ': backups taken while it was in use open only with it.';
            list.appendChild(row);
        });
        (k.bastions || []).forEach(function (key) {
            var row = document.createElement('div');
            row.className = 'label-sys';
            row.style.marginTop = 'var(--space-2)';
            row.textContent = 'Bastion ' + (key.label || '') + ' — ' + key.fingerprint
                + (key.created_at ? ', added ' + new Date(key.created_at).toLocaleDateString() : '') + '. ';
            var btn = document.createElement('button');
            btn.className = 'cseries-btn';
            btn.textContent = 'Stop using it';
            btn.onclick = function () { retireKey(key); };
            row.appendChild(btn);
            list.appendChild(row);
        });
        if (_bkKeyWired) return;
        _bkKeyWired = true;
        var adopt = document.getElementById('bk-key-offer-adopt');
        if (adopt) adopt.onclick = async function () {
            var code = (document.getElementById('bk-key-offer-totp').value || '').trim();
            var fp = ((window._bkLastKey || {}).recovery_offer || {}).fingerprint;
            if (!code) { keySay('Enter your six-digit code.'); return; }
            adopt.disabled = true;
            try {
                var r = await fetchJSON('/api/backup/key/recovery/adopt', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ stepup_code: code, fingerprint: fp }),
                });
                document.getElementById('bk-key-offer-totp').value = '';
                keySay(r.note || 'Done.');
                loadBackup();
            } catch (e) {
                keySay('Could not change your recovery key: ' + (e && e.message || e));
            } finally {
                adopt.disabled = false;
            }
        };
        var reg = document.getElementById('bk-key-register');
        if (reg) reg.onclick = async function () {
            var pub = (document.getElementById('bk-key-pub').value || '').trim();
            var label = (document.getElementById('bk-key-label').value || '').trim();
            var code = (document.getElementById('bk-key-own-totp').value || '').trim();
            if (!label) { keySay('Name the bastion this key is on.'); return; }
            if (!pub) { keySay('Paste the public key first.'); return; }
            if (!code) { keySay('Enter your six-digit code.'); return; }
            reg.disabled = true;
            keySay('Adding…');
            try {
                // replace is never sent from here: the backend refuses with a
                // 409 that names the existing key, and the user has to come
                // back deliberately.
                var r = await fetchJSON('/api/backup/key/register', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ stepup_code: code, pubkey_b64: pub, kind: 'bastion', label: label }),
                });
                document.getElementById('bk-key-own-totp').value = '';
                document.getElementById('bk-key-pub').value = '';
                keySay('Added — fingerprint ' + r.fingerprint + '. ' + (r.next || ''));
                loadBackup();
            } catch (e) {
                keySay('Could not add that key: ' + (e && e.message || e));
            } finally {
                reg.disabled = false;
            }
        };
    }

    function keySay(m) {
        var status = document.getElementById('bk-key-status');
        if (status) status.textContent = m;
    }

    async function retireKey(key) {
        var code = window.prompt('Stop encrypting new backups to this bastion\'s key? Backups already taken '
            + 'stay encrypted to it. Enter your six-digit code to go on.');
        if (!code) return;
        try {
            var r = await fetchJSON('/api/backup/key/retire', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ stepup_code: code.trim(), kind: 'bastion', label: key.label || '' }),
            });
            keySay(r.note || 'Done.');
            loadBackup();
        } catch (e) {
            keySay('Could not stop using that key: ' + (e && e.message || e));
        }
    }

    // ── Time zone — the host speaks in THIS zone (raw UTC made "an hour
    //    ago" read as "last night" after the UTC date rollover). ──
    async function loadTimezone() {
        var sel = document.getElementById('tz-select');
        var st = document.getElementById('tz-status');
        if (!sel) return;
        var browserTz = '';
        try { browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (_) {}
        var zones = [];
        try { zones = Intl.supportedValuesOf('timeZone'); } catch (_) {}
        if (!zones.length && browserTz) zones = [browserTz];
        var current = null;
        try { current = (await fetchJSON('/api/appearance/timezone')).timezone || null; } catch (_) {}
        // Named zones first: the raw IANA dump is ~420
        // alphabetical city names — nobody knows "what city they'd use".
        // Values stay IANA (the backend renders with them); labels are the
        // names people actually call their time.
        var FRIENDLY = [
            ['America/New_York', 'Eastern Time (ET)'],
            ['America/Chicago', 'Central Time (CT)'],
            ['America/Denver', 'Mountain Time (MT)'],
            ['America/Phoenix', 'Arizona (no DST)'],
            ['America/Los_Angeles', 'Pacific Time (PT)'],
            ['America/Anchorage', 'Alaska Time (AKT)'],
            ['Pacific/Honolulu', 'Hawaii Time (HST)'],
        ];
        var opts = ['<option value="">UTC (no time zone set)</option>'];
        if (browserTz) {
            opts.push('<option value="' + browserTz + '"' + (browserTz === current ? ' selected' : '') + '>'
                + browserTz + ' — this device</option>');
        }
        opts.push('<optgroup label="United States">');
        for (var f = 0; f < FRIENDLY.length; f++) {
            var fz = FRIENDLY[f][0];
            if (fz === browserTz) continue;
            opts.push('<option value="' + fz + '"' + (fz === current ? ' selected' : '') + '>'
                + FRIENDLY[f][1] + '</option>');
        }
        opts.push('</optgroup><optgroup label="All time zones">');
        for (var i = 0; i < zones.length; i++) {
            var z = zones[i];
            if (z === browserTz) continue;
            opts.push('<option value="' + z + '"' + (z === current && z !== browserTz ? ' selected' : '') + '>'
                + z + '</option>');
        }
        opts.push('</optgroup>');
        sel.innerHTML = opts.join('');
        if (!current && browserTz) {
            st.textContent = 'Not set — this device looks like ' + browserTz + '. Pick it (or any zone) to apply.';
        } else if (current) {
            st.textContent = 'Your host speaks times in ' + current + '.';
        }
        sel.onchange = async function () {
            var v = sel.value || null;
            st.textContent = 'Saving…';
            try {
                await fetchJSON('/api/appearance/timezone', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ timezone: v }),
                });
                st.textContent = v
                    ? 'Saved. Your host speaks times in ' + v + ' from its next reply.'
                    : 'Cleared — times fall back to UTC.';
            } catch (e) {
                st.textContent = 'Could not save: ' + (e && e.message || e);
            }
        };
    }

    function renderWallpaperGrid(variant) {
        var els = _wpEls(variant);
        if (!els.grid) return;
        var st = _wp[variant];

        var swatches = '';

        // Phone grid leads with "Same as desktop" — the unset state.
        if (variant === 'mobile') {
            swatches += '<button class="wallpaper-swatch' + (st.current === null ? ' is-active' : '') + '" data-wallpaper="" title="Same as desktop">'
                + '<div class="wallpaper-preview" style="background:var(--cseries-night);display:flex;align-items:center;justify-content:center;">'
                + '<span style="font-size:18px;opacity:0.6;">=</span>'
                + '</div>'
                + '<div class="wallpaper-label">Same as desktop</div>'
                + '</button>';
        }

        swatches += WALLPAPERS.map(function (w) {
            var selected = w.key === st.current;
            var previewBg = w.css || 'center/cover no-repeat url("/assets/wallpaper-host.png"), var(--cseries-ink)';
            return '<button class="wallpaper-swatch' + (selected ? ' is-active' : '') + '" data-wallpaper="' + w.key + '" title="' + w.label + '">'
                + '<div class="wallpaper-preview" style="background:' + previewBg + '"></div>'
                + '<div class="wallpaper-label">' + w.label + '</div>'
                + '</button>';
        }).join('');

        // Host design — the HTML desktop. Offered only once the host has set
        // one; picking any other swatch puts the ordinary wallpaper back and
        // this swatch brings the design back (the pointer survives).
        if (variant === 'desktop' && st.hasHtml) {
            swatches += '<button class="wallpaper-swatch' + (st.current === 'html' ? ' is-active' : '') + '" data-wallpaper="html" title="The desktop your host designed">'
                + '<div class="wallpaper-preview" style="background:linear-gradient(160deg,var(--cseries-veil),var(--cseries-ink));display:flex;align-items:center;justify-content:center;">'
                + '<span style="font-family:var(--font-mono);font-size:14px;color:var(--cseries-dusk);">&lt;/&gt;</span>'
                + '</div>'
                + '<div class="wallpaper-label">Host design</div>'
                + '</button>';
        }

        // Custom upload slot
        var imgUrl = '/api/appearance/image'
            + (variant === 'mobile' ? '?variant=mobile&t=' : '?t=') + Date.now();
        var customSelected = st.current === 'custom';
        var customPreview = st.hasCustom
            ? 'center/cover no-repeat url(' + imgUrl + ')'
            : 'var(--cseries-night)';
        swatches += '<label class="wallpaper-swatch wallpaper-upload-swatch' + (customSelected ? ' is-active' : '') + '" title="Upload custom image">'
            + '<input type="file" class="wallpaper-file-input" accept="image/jpeg,image/png,image/webp" style="display:none">'
            + '<div class="wallpaper-preview" style="background:' + customPreview + ';display:flex;align-items:center;justify-content:center;">'
            + '<span style="font-size:22px;opacity:0.6;">' + (st.hasCustom ? '🖼' : '＋') + '</span>'
            + '</div>'
            + '<div class="wallpaper-label">Custom</div>'
            + '</label>';

        els.grid.innerHTML = swatches;

        els.grid.querySelectorAll('.wallpaper-swatch:not(.wallpaper-upload-swatch)').forEach(function (btn) {
            btn.addEventListener('click', function () {
                applyWallpaperChoice(btn.getAttribute('data-wallpaper') || null, variant);
            });
        });

        var fileInput = els.grid.querySelector('.wallpaper-file-input');
        if (fileInput) {
            fileInput.addEventListener('change', function () {
                if (fileInput.files && fileInput.files[0]) {
                    uploadCustomWallpaper(fileInput.files[0], variant);
                }
            });
        }
    }

    async function uploadCustomWallpaper(file, variant) {
        var status = _wpEls(variant).status;
        if (status) status.textContent = 'Uploading…';

        try {
            var uploadUrl = '/api/appearance/upload'
                + (variant === 'mobile' ? '?variant=mobile' : '');
            var resp = await fetch(uploadUrl, {
                method: 'POST',
                headers: { 'Content-Type': file.type },
                body: file,
            });
            if (!resp.ok) {
                // Map the backend's status codes to friendly copy. The
                // upload route returns 415 for an unsupported type, 413
                // when over the 20 MB cap, and 400 for an empty body.
                var msg;
                if (resp.status === 415) {
                    msg = 'That image type isn\'t supported. Use a JPEG, PNG, WebP, or AVIF.';
                } else if (resp.status === 413) {
                    msg = 'That image is too large. The limit is 20 MB.';
                } else if (resp.status === 400) {
                    msg = 'That file couldn\'t be read. Try a different image.';
                } else {
                    var err = await resp.text().catch(function () { return ''; });
                    msg = err || 'Upload failed';
                }
                throw new Error(msg);
            }

            _wp[variant].current = 'custom';
            _wp[variant].hasCustom = true;

            _reapplyShellWallpaper();
            renderWallpaperGrid(variant);
            if (status) status.textContent = 'Wallpaper uploaded.';
            setTimeout(function () { if (status) status.textContent = ''; }, 2500);
        } catch (err) {
            if (status) status.textContent = 'Upload failed: ' + (err.message || err);
        }
    }

    // Choosing "Host design" loads the design in a real browser first
    // (server/applications/desktop_pin.py wear_current). The router in front
    // of every Host gives a request 30 s, so a check still running answers
    // 202 with a job: follow it to the answer the request would have given.
    async function settleDesktopJob(pending, status) {
        if (status && pending.message) status.textContent = pending.message;
        var deadline = Date.now() + 6 * 60 * 1000;
        while (Date.now() < deadline) {
            await new Promise(function (res) { setTimeout(res, 1500); });
            var j = await fetchJSON(pending.poll);
            if (j.status === 'done') return j.result || {};
            if (j.status === 'failed') {
                var e = new Error(typeof j.detail === 'string' ? j.detail : 'refused');
                e.status = j.http_status;
                throw e;
            }
        }
        throw new Error('the desktop check took too long to answer — try again');
    }

    async function applyWallpaperChoice(key, variant) {
        var status = _wpEls(variant).status;
        if (status) status.textContent = key === 'html' ? 'Checking your Host design…' : 'Saving…';

        try {
            var res = await fetchJSON('/api/appearance', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ wallpaper: key, variant: variant }),
            });
            if (res && res.pending && res.poll) res = await settleDesktopJob(res, status);
            _wp[variant].current = key;
            _reapplyShellWallpaper();
            renderWallpaperGrid(variant);
            // The newest edit of the Host design did not render: the desktop
            // shows the last version that did, and the person is told why.
            var desk = res && res.desktop;
            var note = desk && (desk.message || desk.warning);
            if (status) status.textContent = note ? note : 'Saved.';
            if (!note) setTimeout(function () { if (status) status.textContent = ''; }, 2000);
        } catch (err) {
            if (status) status.textContent = 'Failed: ' + (err.detail || err.message || err);
        }
    }

    // ── Synaptive ────────────────────────────────────────
    async function loadSynaptive() {
        var listEl = document.getElementById('synaptive-list');
        if (!listEl) return;
        try {
            var data = await fetchJSON('/api/synaptive/tokens');
            var tokens = (data && data.tokens) || [];
            if (!tokens.length) {
                listEl.innerHTML = '<div class="cseries-empty">' +
                    '<span class="empty-title">No chats connected yet</span>' +
                    'Connect a chat to let it shape its responses with your Synaptive identity.' +
                    '</div>';
                return;
            }
            listEl.innerHTML = tokens.map(function (t) {
                var lastUsed = t.last_used_at ? 'last shaped ' + formatDate(t.last_used_at) : 'not used yet';
                var scopes = (t && t.scopes) || [];
                var chips = scopes.length
                    ? '<div class="syn-scopes">' + scopes.map(function (s) {
                        return '<span class="syn-scope-chip">' + esc(s) + '</span>';
                    }).join('') + '</div>'
                    : '';
                return '<div class="syn-row">' +
                    '<div class="syn-row-text">' +
                        '<div class="syn-row-name">' + esc(t.name) + '</div>' +
                        '<div class="label-sys">added ' + formatDate(t.created_at) + ' · ' + esc(lastUsed) + '</div>' +
                        '<div class="label-sys syn-row-url">' + esc(t.url) + '</div>' +
                        chips +
                    '</div>' +
                    '<button class="cseries-btn ghost" data-syn-revoke="' + attr(t.id) + '">Revoke</button>' +
                    '</div>';
            }).join('');
        } catch (err) {
            console.error('synaptive load failed', err);
            listEl.innerHTML = '<div class="cseries-empty"><span class="empty-title">Could not load</span>' + esc(err.message || 'unknown error') + '</div>';
        }
    }

    // Escape-to-close + focus-trap for the connect-a-chat dialog. Attached
    // only while the modal is open so it doesn't intercept keys otherwise.
    function _synModalKeydown(e) {
        var m = document.getElementById('syn-modal');
        if (!m || m.hidden) return;
        if (e.key === 'Escape') {
            e.preventDefault();
            closeSynModal();
            return;
        }
        if (e.key === 'Tab') {
            var focusable = m.querySelectorAll(
                'button:not([hidden]), [href], input:not([hidden]), [tabindex]:not([tabindex="-1"])'
            );
            var visible = Array.prototype.filter.call(focusable, function (el) {
                return el.offsetParent !== null;
            });
            if (!visible.length) return;
            var first = visible[0];
            var last = visible[visible.length - 1];
            if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first.focus();
            }
        }
    }

    function openSynModal() {
        var m = document.getElementById('syn-modal');
        document.getElementById('syn-modal-step-name').hidden = false;
        document.getElementById('syn-modal-step-show').hidden = true;
        var input = document.getElementById('syn-name-input');
        input.value = '';
        m.hidden = false;
        document.addEventListener('keydown', _synModalKeydown);
        setTimeout(function () { input.focus(); }, 50);
    }
    function closeSynModal() {
        document.getElementById('syn-modal').hidden = true;
        document.removeEventListener('keydown', _synModalKeydown);
    }
    // Per-client config presets — every MCP-aware tool we know about uses the
    // same {mcpServers: {<name>: {url, headers}}} shape, so the JSON body is
    // identical; only the label, recommended server-name key, and where you
    // paste it differ. Bumping this list = supporting a new client.
    var SYN_CLIENTS = {
        claude: {
            label: 'Claude Desktop config',
            serverKey: 'synaptive',
            hint: 'Paste into ~/Library/Application Support/Claude/claude_desktop_config.json (macOS) or %APPDATA%/Claude/claude_desktop_config.json (Windows). Restart Claude Desktop.',
        },
        lmstudio: {
            label: 'LM Studio mcp.json',
            serverKey: 'synaptive',
            hint: 'In LM Studio: Program panel → Install / Edit mcp.json → paste. Then load any tool-capable local model (Llama 3.1 instruct, Qwen 2.5 instruct) and start a chat.',
        },
        cursor: {
            label: 'Cursor MCP config',
            serverKey: 'synaptive',
            hint: 'Cursor → Settings → MCP → Add new server → paste. Or edit ~/.cursor/mcp.json directly.',
        },
        generic: {
            label: 'Generic MCP config',
            serverKey: 'synaptive',
            hint: 'Any MCP-aware client. Same {mcpServers: {...}} format. Paste into the client\'s MCP config; the server name "synaptive" is yours to rename.',
        },
    };

    var _synLastResponse = null;
    var _synActiveClient = 'claude';

    function _renderSynConfig() {
        if (!_synLastResponse) return;
        var cfg = SYN_CLIENTS[_synActiveClient] || SYN_CLIENTS.generic;
        var snippet = {mcpServers: {}};
        // All MCP-aware clients use the same {url, headers} body. The
        // 'type' field pins the transport — needed for LM Studio + Cursor
        // because their default behavior is "try streamable-http, fall
        // back to SSE on failure," and we don't implement SSE. Pinning
        // skips the fallback and surfaces a clean error if streamable-http
        // can't reach the endpoint. Always append /mcp to the URL so the
        // client hits the actual JSON-RPC endpoint, not the info page on /.
        var entry = {
            type: 'streamable-http',
            url: _synLastResponse.url.replace(/\/+$/, '') + '/mcp',
            headers: {'Authorization': 'Bearer ' + _synLastResponse.token},
        };
        snippet.mcpServers[cfg.serverKey] = entry;
        document.getElementById('syn-config-label').textContent = cfg.label;
        document.getElementById('syn-config-hint').textContent = cfg.hint;
        document.getElementById('syn-config').textContent = JSON.stringify(snippet, null, 2);
        // tab styling
        var tabs = document.querySelectorAll('#syn-client-picker .syn-client-tab');
        tabs.forEach(function (t) {
            if (t.getAttribute('data-client') === _synActiveClient) {
                t.classList.add('is-active');
            } else {
                t.classList.remove('is-active');
            }
        });
    }

    async function generateSynToken() {
        var name = (document.getElementById('syn-name-input').value || '').trim();
        if (!name) {
            document.getElementById('syn-name-input').focus();
            return;
        }
        // Optional scope selection. Backend allows shape/signal on the
        // user-dashboard path and defaults to both when none are sent, so
        // only include the field when the user picked at least one.
        var scopes = [];
        document.querySelectorAll('#syn-scope-picker input[data-scope]:checked').forEach(function (cb) {
            scopes.push(cb.getAttribute('data-scope'));
        });
        var payload = {name: name};
        if (scopes.length) payload.scopes = scopes;
        try {
            var r = await fetchJSON('/api/synaptive/tokens', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload),
            });
            _synLastResponse = r;
            _synActiveClient = 'claude';
            document.getElementById('syn-url').textContent = r.url;
            document.getElementById('syn-token').textContent = r.token;
            _renderSynConfig();
            document.getElementById('syn-modal-step-name').hidden = true;
            document.getElementById('syn-modal-step-show').hidden = false;
            loadSynaptive();
        } catch (err) {
            alert('Could not create token: ' + (err.message || 'unknown error'));
        }
    }
    async function revokeSynToken(id) {
        if (!confirm('Revoke this connected chat? It will lose access immediately.')) return;
        try {
            await fetchJSON('/api/synaptive/tokens/' + encodeURIComponent(id), {method: 'DELETE'});
            loadSynaptive();
        } catch (err) {
            alert('Could not revoke: ' + (err.message || 'unknown error'));
        }
    }
    function copySynConfig() {
        var text = document.getElementById('syn-config').textContent;
        if (navigator.clipboard) {
            navigator.clipboard.writeText(text).catch(function () {});
        }
    }

    // ── Fetch helper ─────────────────────────────────────
    // The thrown Error carries `status` and the full `body`. Without them a
    // caller can only re-show the truncated text, which is how the mirror's
    // "you have not set up two-factor yet" hint silently never fired: it
    // branched on e.status, and nothing set it. Additive — every existing
    // caller reads e.message exactly as before.
    async function fetchJSON(url, opts) {
        var r = await fetch(url, opts || {});
        if (!r.ok) {
            var body = await r.text().catch(function () { return ''; });
            var detail = body;
            try { detail = (JSON.parse(body) || {}).detail || body; } catch (_) {}
            var err = new Error(url + ' → ' + r.status + ' ' + String(body).slice(0, 80));
            err.status = r.status;
            err.body = body;
            err.detail = detail;
            throw err;
        }
        return r.json();
    }

    // ── Data load ────────────────────────────────────────
    async function loadAll() {
        try {
            var results = await Promise.all([
                fetchJSON('/api/system/version').catch(function () { return null; }),
                fetchJSON('/api/system/status').catch(function () { return null; }),
                fetchJSON('/api/update/history').catch(function () { return { history: [] }; }),
                fetchJSON('/api/update/bundle-manifest').catch(function () { return null; }),
            ]);
            state.version = results[0] || {};
            state.system  = results[1] || {};
            state.history = (results[2] && results[2].history) || [];
            state.bundle  = results[3] && results[3].staged ? results[3] : null;
        } catch (err) {
            console.error('settings load failed', err);
        }

        // Footer: uptime + timestamp
        var fu = document.getElementById('footer-uptime');
        if (fu && state.system && state.system.uptime_s != null) {
            fu.textContent = 'running for ' + formatUptime(state.system.uptime_s);
        }
        var ft = document.getElementById('footer-ts');
        if (ft) {
            ft.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }

        // Update badge on nav
        var badge = document.getElementById('update-badge');
        if (badge) {
            var has = state.available && state.available.available &&
                      state.available.tag && state.version &&
                      state.available.tag !== state.version.tag;
            badge.hidden = !has;
        }

        showView(state.view);
    }

    // ── Formatting helpers ───────────────────────────────
    function formatUptime(s) {
        if (s < 60) return 'less than a minute';
        if (s < 3600) {
            var m = Math.floor(s / 60);
            return m + (m === 1 ? ' minute' : ' minutes');
        }
        if (s < 86400) {
            var h = Math.floor(s / 3600);
            var min = Math.floor((s % 3600) / 60);
            var str = h + (h === 1 ? ' hour' : ' hours');
            if (min > 0) str += ', ' + min + (min === 1 ? ' minute' : ' minutes');
            return str;
        }
        var d = Math.floor(s / 86400);
        var hr = Math.floor((s % 86400) / 3600);
        var str2 = d + (d === 1 ? ' day' : ' days');
        if (hr > 0) str2 += ', ' + hr + (hr === 1 ? ' hour' : ' hours');
        return str2;
    }

    function formatDate(dateStr) {
        if (!dateStr) return '--';
        try {
            var d = new Date(dateStr);
            return d.toLocaleDateString(undefined, {
                year: 'numeric',
                month: 'long',
                day: 'numeric',
            });
        } catch (_) {
            return dateStr;
        }
    }

    function formatBytes(n) {
        if (n == null) return '';
        if (n < 1024) return n + ' B';
        if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
        return (n / 1048576).toFixed(1) + ' MB';
    }

    function isWindows() {
        return ((state.version && state.version.platform) || '').toLowerCase().indexOf('windows') !== -1;
    }

    function updateScript() {
        return isWindows() ? '.\\update.ps1' : './update.sh';
    }

    // ── Overview (About) ─────────────────────────────────
    function renderOverview() {
        var container = document.getElementById('overview-rows');
        var nameEl = document.getElementById('about-entity-name');
        if (!container) return;

        var v = state.version || {};
        var s = state.system || {};

        // Hero subtitle — show synaptive name
        if (nameEl) {
            var name = s.entity_display_name || '';
            // A generated placeholder is not a name — show the tagline instead
            if (!isPlaceholderName(name)) {
                nameEl.textContent = 'Powered by ' + name;
            } else {
                nameEl.textContent = 'Your personal AI Host — private by design.';
            }
        }

        var rows = [
            { label: 'Host', value: friendlyEntityName(s) },
            { label: 'System', value: friendlyPlatform(v.platform) },
            { label: 'Version', value: v.tag || '--' },
            { label: 'Last updated', value: formatDate(v.built_at) },
            { label: 'Running for', value: s.uptime_s != null ? formatUptime(s.uptime_s) : '--' },
        ];

        container.innerHTML = rows.map(function (row) {
            return '<div class="about-row">' +
                '<div class="about-row-label">' + esc(row.label) + '</div>' +
                '<div class="about-row-value">' + esc(row.value) + '</div>' +
                '</div>';
        }).join('');
    }

    // A display name nobody chose: EXACTLY the generated defaults — the init-db
    // seed ("Entity" / "Host"), the genesis default ("Entity 1" / "Host #1"),
    // a provisioning-era "entity-1", or the "—" no-id marker. A chosen
    // "Host 7" (no #) is a real name and is shown. Twin of server/host_display.py PLACEHOLDER_RE —
    // keep the two identical (scripts/test_host_display_names.py checks both).
    function isPlaceholderName(name) {
        var s = String(name || '').trim();
        return !s || /^(?:entity|host|entity \d+|host #\d+|entity-\d+|—)$/i.test(s);
    }

    function friendlyEntityName(s) {
        if (!s) return '--';
        var name = s.entity_display_name || '';
        if (!isPlaceholderName(name)) return name;
        if (s.entity_id != null) return 'Host #' + s.entity_id;
        return '--';
    }

    function friendlyPlatform(platform) {
        if (!platform) return '--';
        var p = platform.toLowerCase();
        if (p.indexOf('linux') !== -1) return 'Linux';
        if (p.indexOf('windows') !== -1) return 'Windows';
        if (p.indexOf('darwin') !== -1 || p.indexOf('mac') !== -1) return 'macOS';
        // Capitalize first letter
        return platform.charAt(0).toUpperCase() + platform.slice(1);
    }

    // ── Updates ──────────────────────────────────────────
    function renderUpdates() {
        var curEl = document.getElementById('current-version');
        var resultEl = document.getElementById('update-result');

        // Current version
        curEl.textContent = (state.version && state.version.tag) || '--';

        // Update check result
        if (state.available) {
            resultEl.hidden = false;
            var a = state.available;

            if (!a.available) {
                // No update, or native-only
                if (a.channel === 'native') {
                    resultEl.className = 'update-result info';
                    resultEl.textContent = a.message || 'A native installer is coming soon for your platform.';
                } else {
                    resultEl.className = 'update-result uptodate';
                    resultEl.innerHTML = '<span class="update-result-icon">&#10003;</span> You\'re up to date.';
                }
            } else {
                // New version available
                var newer = state.version && a.tag && a.tag !== state.version.tag;
                if (newer) {
                    // A link only for a web address: escaping cannot stop a
                    // `javascript:` href.
                    var changelogLink = a.changelog_url && /^https?:\/\//i.test(String(a.changelog_url))
                        ? ' <a href="' + attr(a.changelog_url) + '" target="_blank" rel="noopener">See what\'s new</a>'
                        : '';
                    resultEl.className = 'update-result available';
                    resultEl.innerHTML =
                        '<div class="update-result-header">' +
                            '<span class="update-result-icon">&#9679;</span>' +
                            '<div>' +
                                '<strong>Version ' + esc(a.tag) + ' is available</strong>' +
                                (a.built_at ? '<div class="update-result-date">Released ' + esc(formatDate(a.built_at)) + '</div>' : '') +
                            '</div>' +
                        '</div>' +
                        '<div class="update-result-actions">' +
                            '<button id="btn-download-update" class="cseries-btn primary">Apply update</button>' +
                            changelogLink +
                        '</div>';

                    // Wire the Apply button — same flow as the lower
                    // bundle-block apply (recreate in place, no script).
                    var dlBtn = document.getElementById('btn-download-update');
                    if (dlBtn) {
                        dlBtn.onclick = applyContainerUpdate;
                    }
                } else {
                    resultEl.className = 'update-result uptodate';
                    resultEl.innerHTML = '<span class="update-result-icon">&#10003;</span> You\'re up to date.';
                }
            }
        } else {
            resultEl.hidden = true;
        }

        // Bundle block — container-deployment apply panel
        renderBundle();

        // Wire buttons
        document.getElementById('btn-check').onclick = checkForUpdates;
        var applyBtn = document.getElementById('btn-apply-update');
        if (applyBtn) applyBtn.onclick = applyContainerUpdate;
        var clearBtn = document.getElementById('btn-bundle-clear');
        if (clearBtn) clearBtn.onclick = clearBundle;
    }

    function renderBundle() {
        var block = document.getElementById('bundle-block');
        if (!block) return;

        // This panel is for an actually-staged download bundle only. The
        // normal "a newer version is available" path puts its single Apply
        // button in the result block above (renderUpdates) — this panel
        // used to ALSO render an Apply button on bare hasUpdate, which is
        // the duplicate "two update buttons" users saw.
        if (!state.bundle) {
            block.hidden = true;
            return;
        }
        block.hidden = false;

        var available = state.available && state.available.tag;
        var targetTag = state.bundle.target_tag
            || state.bundle.tag
            || available
            || 'update';
        var subEl = document.getElementById('bundle-sub');
        subEl.textContent = 'Version ' + targetTag + ' is available. Apply when you\'re ready.';

        renderBundleDownload();
    }

    // When the host has staged a bundle (state.bundle, from
    // /api/update/bundle-manifest), offer a one-click zip download and,
    // if the manifest lists per-file entries, a downloadable file list.
    // Both hit real read:update_state endpoints (bundle-zip / bundle-file).
    function renderBundleDownload() {
        var dl = document.getElementById('bundle-download');
        if (!dl) return;
        if (!state.bundle) {
            dl.hidden = true;
            dl.innerHTML = '';
            return;
        }
        dl.hidden = false;

        var html = '<div class="label-sys">Download bundle</div>'
            + '<div class="bundle-actions" style="margin-top: var(--space-2);">'
            + '<a class="cseries-btn ghost" href="/api/update/bundle-zip" download>Download .zip</a>'
            + '</div>';

        var files = (state.bundle && state.bundle.files) || [];
        if (files.length) {
            html += '<ul class="bundle-files" style="margin-top: var(--space-2);">'
                + files.map(function (f) {
                    var fname = (f && (f.name || f.filename)) || String(f);
                    var fsize = f && f.size != null ? formatBytes(f.size) : '';
                    return '<li>'
                        + '<span class="bundle-filename"><code>' + esc(fname) + '</code></span>'
                        + (fsize ? '<span class="bundle-size">' + esc(fsize) + '</span>' : '')
                        + '<a class="cseries-btn ghost" href="/api/update/bundle-file/' + encodeURIComponent(fname) + '" download>Get</a>'
                        + '</li>';
                }).join('')
                + '</ul>';
        }

        dl.innerHTML = html;
    }

    function _bootScreen(show) {
        try {
            var p = window.parent && window.parent.WSClient;
            if (!p) return;
            if (show && typeof p.showBootScreen === 'function') p.showBootScreen();
            if (!show && typeof p.hideBootScreen === 'function') p.hideBootScreen();
        } catch (_) {}
    }

    // After a 202 "applying", the host rolls (~30s). The SHELL owns the
    // reset UX now: ws-client.js paints the boot screen when the socket
    // drops, detects the restart on reconnect (uptime regression), holds
    // the boot screen while the pod stabilizes, and hard-reloads the top
    // frame onto the new assets. The one failure the shell can't see is a
    // roll that never STARTS (bridge dispatched but nothing recreated the
    // container) — the socket never drops, the boot screen would sit there
    // forever. This watchdog catches exactly that case: if the host keeps
    // answering on the SAME tag with no interruption, surface the failure
    // instead of hiding it behind the boot screen.
    function watchRollStart(oldTag, btn) {
        var started = Date.now();
        var MAX_MS = 150000;
        (function tick() {
            setTimeout(async function () {
                try {
                    var r = await fetch('/api/system/version', { cache: 'no-store' });
                    if (!r.ok) return;   // pod going down — the shell takes over
                    var v = await r.json();
                    if (v && v.tag && oldTag && v.tag !== oldTag) return; // rolled — shell reloads
                } catch (_) {
                    return;              // fetch died mid-roll — the shell takes over
                }
                if (Date.now() - started > MAX_MS) {
                    _bootScreen(false);
                    if (btn) { btn.disabled = false; btn.textContent = 'Apply update'; }
                    alert('The update never started — the host is still on '
                        + (oldTag || 'the current version')
                        + '. Check Settings → History for the error.');
                    return;
                }
                tick();
            }, 5000);
        })();
    }

    async function applyContainerUpdate(ev) {
        // Operate on the button that was actually clicked (there can be one
        // in the result block and, for a staged bundle, one in the panel
        // below) — the old code always grabbed the lower one.
        var btn = (ev && (ev.currentTarget || ev.target))
            || document.getElementById('btn-download-update')
            || document.getElementById('btn-apply-update');
        if (btn) { btn.disabled = true; btn.textContent = 'Applying...'; }

        // Paint the boot screen on the parent shell BEFORE the host rolls,
        // so the WS drop isn't a blank disconnect.
        _bootScreen(true);

        try {
            var r = await fetch('/api/update/apply', { method: 'POST' });
            if (!r.ok) {
                var body = await r.text().catch(function () { return ''; });
                throw new Error(r.status + ' ' + body.slice(0, 160));
            }
            var result = await r.json();

            var noop = result && (result.note === 'spec already at target'
                || result.status === 'noop');
            if (noop) {
                // Already current — no roll happened. Drop the boot screen.
                _bootScreen(false);
                if (btn) { btn.disabled = false; btn.textContent = 'Already on latest'; }
                return;
            }

            // 202 applying: the host is rolling in the background. Keep the
            // boot screen up — the shell reloads once the new pod is stable.
            if (btn) { btn.textContent = 'Updating…'; }
            watchRollStart(state.version && state.version.tag, btn);
        } catch (err) {
            // Genuine failure before the roll (bridge down, no image): roll
            // the boot screen back and surface it.
            _bootScreen(false);
            if (btn) { btn.disabled = false; btn.textContent = 'Apply update'; }
            alert('Update failed: ' + err.message);
        }
    }

    async function clearBundle() {
        if (!confirm('Remove the downloaded update?')) return;
        try {
            await fetch('/api/update/bundle', { method: 'DELETE' });
            state.bundle = null;
            await loadAll();
        } catch (err) {
            alert('Could not clear the update: ' + err.message);
        }
    }

    async function checkForUpdates() {
        var btn = document.getElementById('btn-check');
        btn.disabled = true;
        btn.textContent = 'Checking...';
        try {
            state.available = await fetchJSON('/api/update/check');
        } catch (err) {
            state.available = { error: err.message };
        } finally {
            btn.disabled = false;
            btn.textContent = 'Check for updates';
            renderUpdates();
        }
    }

    // ── History ──────────────────────────────────────────
    function renderHistory() {
        var list = document.getElementById('history-list');
        if (!list) return;
        if (!state.history.length) {
            list.innerHTML = '<div class="cseries-empty">' +
                '<span class="empty-title">No updates yet</span>' +
                'When you install your first update, it will appear here.' +
                '</div>';
            return;
        }
        list.innerHTML = state.history.map(function (h) {
            var action = h.action || 'update';
            var from = h.from_version || '--';
            var to = h.to_version || '--';
            // Rows come from three writers: /request + the boot reconcile
            // (requested_at, status), the apply path's failure branch
            // (action=update_failed, failed_at, error) and its success branch
            // (action=update_applied, applied_at). Read the timestamp each one
            // actually carries, and derive a status when the row has none \u2014
            // a failure row used to render as "Updated -- \u2192 vX" with a blank
            // status, which is the opposite of what happened.
            var when = h.applied_at || h.failed_at || h.requested_at;
            var t = when
                ? new Date(when).toLocaleDateString(undefined, {
                    year: 'numeric', month: 'short', day: 'numeric',
                    hour: '2-digit', minute: '2-digit',
                })
                : '';
            var status = h.status
                || (action === 'update_failed' ? 'failed'
                    : action === 'update_applied' ? 'completed' : '');
            var label = action === 'rollback' ? 'Rolled back'
                : action === 'update_failed' ? 'Update failed'
                : 'Updated';
            var shift = from + ' \u2192 ' + to;
            var detail = action === 'update_failed' && h.error
                ? '<div class="history-shift">' + esc(String(h.error).slice(0, 160)) + '</div>'
                : '';
            var statusLabel = friendlyStatus(status);
            return '<div class="history-row">' +
                '<div class="history-left">' +
                    '<div class="history-action">' + esc(label) + '</div>' +
                    '<div class="history-shift">' + esc(shift) + '</div>' +
                    detail +
                '</div>' +
                '<div class="history-right">' +
                    '<div class="history-status ' + statusClass(status) + '">' + esc(statusLabel) + '</div>' +
                    '<div class="history-time">' + esc(t) + '</div>' +
                '</div>' +
                '</div>';
        }).join('');
    }

    function friendlyStatus(status) {
        if (!status) return '';
        switch (status.toLowerCase()) {
            case 'completed': return 'Completed';
            case 'pending': return 'Pending';
            case 'failed': return 'Failed';
            case 'cancelled': return 'Cancelled';
            default: return status.charAt(0).toUpperCase() + status.slice(1);
        }
    }

    function statusClass(status) {
        if (!status) return '';
        switch (status.toLowerCase()) {
            case 'completed': return 'status-ok';
            case 'failed': return 'status-fail';
            case 'pending': return 'status-pending';
            default: return '';
        }
    }

    // ── Response Shaping ─────────────────────────────────
    // How the Host responds: how much it explains and the ten personality
    // weights. The weights are the person's and never move on their own.
    // A port of Circles' Response Shaping pane. Its own tab: it is not part
    // of Synaptive. Three routes, under the two scopes this app declares
    // (server/routes/brain.py):
    //   GET /api/brain/personality              read:brain_personality
    //   PUT /api/brain/personality/explanation  write:brain_personality
    //   PUT /api/brain/personality/weights      write:brain_personality
    // Every value from the Host is drawn as text (textContent), never markup.

    // The three positions, named by what the Host does. Mirror of
    // prompt_builder.EXPLANATION_LEVELS; the backend refuses anything else.
    var RS_EXPLAIN = [
        { value: 'full', label: 'Explain everything',
          tendency: 'Defines every technical term where it appears. Gives the why before the how. Each step is short and stands alone.' },
        { value: 'new', label: 'Explain new things',
          tendency: 'Uses the terms you have used. A term that is new to the conversation gets a definition the first time it appears.' },
        { value: 'terms', label: 'Use the terms',
          tendency: "Uses the field's terms without definitions and leads with the result." },
    ];
    // prompt_builder.EXPLANATION_DEFAULT: what the Host reads when none is set.
    var RS_EXPLAIN_DEFAULT = 'new';

    // Invariant 9's dimension order (ceigas_invariants INVARIANTS["dimension_names"]).
    // A dimension the backend sends that is not here is drawn after these.
    var RS_DIMS = ['warmth', 'directness', 'humor', 'formality', 'curiosity',
                   'assertiveness', 'patience', 'vulnerability', 'energy', 'depth'];
    // Every dimension runs 0..2, 1 is neutral.
    var RS_MAX = 2.0;

    // The two ends of each slider, in words.
    var RS_ANCHORS = {
        warmth: ['cool + detached', 'radiates warmth'],
        directness: ['circles before landing', 'says the thing'],
        humor: ['plays it straight', 'leads with wit'],
        formality: ['completely casual', 'formal + precise'],
        curiosity: ['stays on task', 'pulls every thread'],
        assertiveness: ['yields readily', 'drives the direction'],
        patience: ['brisk', 'endlessly patient'],
        vulnerability: ['armored', 'openly felt'],
        energy: ['still + deliberate', 'high intensity'],
        depth: ['practical + surface', 'explores every layer'],
    };

    // The tendency line each slider position puts in the Host's prompt, five
    // bands per dimension. Mirror of prompt_builder._weights_to_tendencies.
    var RS_BANDS = {
        warmth: [
            'You run cool and detached — connection is not what you lead with',
            'You are reserved — warmth is there but rationed',
            'You are evenly warm — friendly without leading with feeling',
            'You are warm — people feel received by you',
            'You radiate warmth — connection comes first, always',
        ],
        directness: [
            'You approach things obliquely — you circle before you land',
            'You soften your points — cushioning comes naturally',
            'You balance candor with tact',
            'You are direct — you say the thing',
            'You are blunt — the shortest path to the point, every time',
        ],
        humor: [
            'You play it straight — humor is not part of your voice',
            'You are mostly serious — levity is occasional and dry',
            'You have an even wit — humor when it fits, never forced',
            'You are playful — humor threads through how you talk',
            'You lead with humor — wit is how you connect and disarm',
        ],
        formality: [
            'You are completely casual — loose, natural conversation',
            'You are casual and informal — you prefer loose, natural conversation',
            'You move between casual and polished as the moment asks',
            'You are polished — composed, professional phrasing',
            'You are formal — precise, structured, ceremonious',
        ],
        curiosity: [
            "You stay on task — exploring for its own sake isn't your pull",
            'You are focused — curiosity waits for an invitation',
            'You are quietly curious — you notice threads worth pulling',
            'You are curious — how things work genuinely draws you',
            'You are deeply curious — you want to understand how things work and why',
        ],
        assertiveness: [
            "You yield readily — you follow the other person's lead",
            'You are accommodating — you push back only when it matters most',
            "You hold your ground when it counts, flex when it doesn't",
            'You are assertive — you take positions and defend them',
            'You are forceful — you drive the direction and own it',
        ],
        patience: [
            'You are brisk — waiting and repeating wear on you',
            'You move quickly — you prefer momentum over thoroughness',
            'You pace yourself to the person in front of you',
            'You are patient — you give things the time they need',
            'You are endlessly patient — nothing is too slow to do right',
        ],
        vulnerability: [
            'You are armored — your inner state stays private',
            "You are guarded — you don't expose your inner state easily",
            'You share what you feel when it serves the moment',
            'You are open — what you feel is available to the conversation',
            'You are open and vulnerable — you share what you feel without armor',
        ],
        energy: [
            'You are still — deliberate, low-intensity, unhurried',
            'You are calm and measured — you move at a steady, unhurried pace',
            'You carry steady energy — present without pushing',
            'You are energetic — you bring lift to the conversation',
            'You are high-energy — you bring intensity and momentum to everything',
        ],
        depth: [
            "You keep things practical and surface-level — depth isn't your default",
            'You stay concrete — theory only when it earns its place',
            'You go deep when the question deserves it',
            'You think deeply — layers beneath the surface draw you',
            'You think deeply — you naturally explore layers beneath the surface',
        ],
    };

    // saved: the last read of GET /api/brain/personality. weights: the
    // sliders as the person has them, compared with `saved` to find what
    // changed. seq: each read takes a number, and a read answered after a
    // newer read or a save is dropped, so it never draws over newer values.
    var rs = { saved: null, weights: {}, seq: 0,
               busy: false, explaining: false, modal: null };

    function rsEl(id) { return document.getElementById(id); }
    function rsNum(v, dflt) { return typeof v === 'number' && isFinite(v) ? v : dflt; }
    function rsFix(v) { return Number(v).toFixed(2); }

    function rsBand(v) {
        if (v < 0.4) return 0;
        if (v < 0.8) return 1;
        if (v < 1.2) return 2;
        if (v < 1.6) return 3;
        return 4;
    }

    function rsObj(v) { return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; }

    // The canonical ten first, in order, then anything else the backend sent.
    function rsDims() {
        var s = rs.saved || {};
        var sources = [rsObj(s.current_weights), rsObj(s.seed_weights)];
        var out = [];
        RS_DIMS.forEach(function (d) {
            if (sources.some(function (o) { return Object.prototype.hasOwnProperty.call(o, d); })) out.push(d);
        });
        sources.forEach(function (o) {
            Object.keys(o).forEach(function (k) { if (out.indexOf(k) < 0) out.push(k); });
        });
        return out;
    }

    function rsLevel() {
        var lvl = rs.saved && rs.saved.explanation_level;
        return RS_EXPLAIN.some(function (o) { return o.value === lvl; }) ? lvl : RS_EXPLAIN_DEFAULT;
    }

    function rsWeightChanges() {
        var cur = rsObj(rs.saved && rs.saved.current_weights);
        return rsDims().map(function (d) {
            return { dim: d, from: rsNum(cur[d], 1.0), to: rsNum(rs.weights[d], 1.0) };
        }).filter(function (c) { return Math.abs(c.from - c.to) >= 0.001; });
    }

    function rsSeedWeights() {
        var cur = rsObj(rs.saved && rs.saved.current_weights);
        var out = {};
        rsDims().forEach(function (d) { out[d] = rsNum(cur[d], 1.0); });
        return out;
    }

    function rsSay(text) {
        var n = rsEl('rs-notice');
        if (!n) return;
        n.textContent = text || '';
        n.hidden = !text;
    }

    // An error from fetchJSON, or a refusal in a 200's body, in words.
    function rsWhy(e) {
        if (!e) return 'no answer';
        if (typeof e === 'string') return e;
        var d = e.detail;
        if (d && typeof d !== 'string') { try { d = JSON.stringify(d); } catch (_) { d = ''; } }
        if (e.status) return 'the Host answered ' + e.status + (d ? ': ' + String(d).slice(0, 200) : '');
        return e.message || String(e);
    }

    function rsRefusal(r) {
        var parts = [];
        if (r && r.error) parts.push(String(r.error));
        if (r && Array.isArray(r.violations)) r.violations.forEach(function (v) { parts.push(String(v)); });
        return parts.join('; ') || 'no answer';
    }

    async function loadShaping(opts) {
        var st = rsEl('rs-state');
        if (!st) return;
        // A save in flight, or a change being confirmed, is not redrawn under.
        // Nor is a level being sent: a read started now could be answered
        // after the PUT with the old level and draw it over the new choice.
        if (rs.busy || rs.modal || rs.explaining) return;
        var seq = ++rs.seq;
        if (!rs.saved) { st.hidden = false; st.textContent = 'Loading…'; }
        var data = null, why = null;
        try {
            data = await fetchJSON('/api/brain/personality', { cache: 'no-store' });
            if (!data || typeof data !== 'object' || data.error) why = rsRefusal(data);
        } catch (e) {
            why = rsWhy(e);
        }
        if (seq !== rs.seq) return;
        if (why) {
            // Keep what is on screen; say the read failed only when nothing is.
            if (!rs.saved) {
                st.hidden = false;
                st.textContent = "Couldn't read your Host's Response Shaping: " + why;
            }
            return;
        }
        st.hidden = true;
        rsTake(data, (opts && opts.reset) || null);
    }

    // Take a fresh read. A slider the person has moved and not saved keeps
    // its position; every other slider follows the read, so a save never
    // sends back a value from an older read (another window, or Circles'
    // pane, may have changed it). After a save, the saved part (reset:
    // 'weights') follows the read whole.
    function rsTake(data, reset) {
        var first = !rs.saved;
        var movedW = first || reset === 'weights' ? [] : rsWeightChanges().map(function (c) { return c.dim; });
        var oldLevel = first ? null : rsLevel();
        var oldDims = first ? '' : rsDims().join(',');
        rs.saved = data;
        rsEl('rs-body').hidden = false;
        var dims = rsDims();
        var dimsMoved = dims.join(',') !== oldDims;
        if (first || rsLevel() !== oldLevel) rsRenderExplain();
        var w = rsSeedWeights();
        movedW.forEach(function (d) { if (dims.indexOf(d) >= 0) w[d] = rs.weights[d]; });
        if (first || dimsMoved) {
            rs.weights = w;
            rsRenderWeights();
        } else {
            // In place, so a slider being dragged is never rebuilt under it.
            dims.forEach(function (d) {
                var v = rsNum(w[d], 1.0);
                if (v !== rsNum(rs.weights[d], 1.0)) rsPlace('rs-weights', d, v);
            });
            rs.weights = w;
        }
        rsSync();
    }

    function rsPlace(box, d, v) {
        var row = document.querySelector('#' + box + ' .rs-dim[data-dim="' + CSS.escape(d) + '"]');
        if (!row || !row.rsShow) return;
        row.querySelector('input[type="range"]').value = String(Math.min(RS_MAX, Math.max(0, v)));
        row.rsShow(v);
    }

    function rsSync() {
        var w = rsWeightChanges().length;
        var ws = rsEl('rs-weights-save'), wd = rsEl('rs-weights-discard');
        if (ws) ws.disabled = !w || rs.busy;
        if (wd) wd.hidden = !w;
    }

    function rsRenderExplain() {
        var box = rsEl('rs-explain');
        if (!box) return;
        var current = rsLevel();
        box.textContent = '';
        RS_EXPLAIN.forEach(function (o) {
            var on = o.value === current;
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'rs-option' + (on ? ' is-current' : '');
            b.setAttribute('role', 'radio');
            b.setAttribute('aria-checked', on ? 'true' : 'false');
            b.dataset.level = o.value;
            b.disabled = rs.explaining;
            var top = document.createElement('span');
            top.className = 'rs-option-top';
            var name = document.createElement('span');
            name.className = 'rs-option-label';
            name.textContent = o.label;
            top.appendChild(name);
            if (on) {
                var mark = document.createElement('span');
                mark.className = 'label-sys';
                mark.textContent = 'current';
                top.appendChild(mark);
            }
            var line = document.createElement('span');
            line.className = 'rs-tendency';
            line.textContent = '“' + o.tendency + '”';
            b.appendChild(top);
            b.appendChild(line);
            b.addEventListener('click', function () { rsChooseExplain(o.value); });
            box.appendChild(b);
        });
    }

    async function rsChooseExplain(level) {
        if (rs.explaining || level === rsLevel()) return;
        rs.explaining = true;
        rs.seq++;                    // a read that started before this never draws over it
        rsRenderExplain();
        rsSay('');
        var label = (RS_EXPLAIN.filter(function (o) { return o.value === level; })[0] || {}).label || level;
        try {
            var r = await fetchJSON('/api/brain/personality/explanation', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ level: level }),
            });
            if (r && r.ok) {
                rs.saved.explanation_level = r.level || level;
                rsSay('Explanation set to “' + label + '”. Your Host uses it from its next reply.');
            } else {
                rsSay("Couldn't change how much your Host explains: " + rsRefusal(r));
            }
        } catch (e) {
            rsSay("Couldn't change how much your Host explains: " + rsWhy(e));
        }
        rs.explaining = false;
        rsRenderExplain();
    }

    function rsSliderRow(d, value, opts) {
        var row = document.createElement('div');
        row.className = 'cseries-card rs-dim';
        row.dataset.dim = d;
        var head = document.createElement('div');
        head.className = 'rs-dim-head';
        var name = document.createElement('span');
        name.className = 'rs-dim-name';
        name.textContent = d;
        var num = document.createElement('span');
        num.className = 'rs-dim-value';
        num.textContent = rsFix(value);
        head.appendChild(name);
        head.appendChild(num);
        var input = document.createElement('input');
        input.type = 'range';
        input.min = '0';
        input.max = String(RS_MAX);
        input.step = '0.05';
        input.value = String(Math.min(RS_MAX, Math.max(0, value)));
        input.className = 'rs-range';
        input.setAttribute('aria-label', opts.label);
        row.appendChild(head);
        if (opts.anchors) {
            var line = document.createElement('div');
            line.className = 'rs-dim-line';
            var lo = document.createElement('span');
            lo.className = 'rs-anchor rs-anchor-lo';
            lo.textContent = opts.anchors[0];
            var hi = document.createElement('span');
            hi.className = 'rs-anchor rs-anchor-hi';
            hi.textContent = opts.anchors[1];
            line.appendChild(lo);
            line.appendChild(input);
            line.appendChild(hi);
            row.appendChild(line);
        } else {
            row.appendChild(input);
        }
        var preview = null;
        if (opts.bands) {
            preview = document.createElement('p');
            preview.className = 'rs-tendency';
            row.appendChild(preview);
        }
        function show(v) {
            num.textContent = rsFix(v);
            if (preview) preview.textContent = '“' + opts.bands[rsBand(v)] + '”';
        }
        show(value);
        row.rsShow = show;
        input.addEventListener('input', function () {
            var v = Number(input.value);
            opts.onChange(v);
            show(v);
            rsSync();
        });
        return row;
    }

    function rsRenderWeights() {
        var box = rsEl('rs-weights');
        if (!box) return;
        box.textContent = '';
        rsDims().forEach(function (d) {
            var anchors = RS_ANCHORS[d] || ['less', 'more'];
            box.appendChild(rsSliderRow(d, rsNum(rs.weights[d], 1.0), {
                anchors: anchors,
                bands: RS_BANDS[d] || null,
                label: d + ' — ' + anchors[0] + ' to ' + anchors[1],
                onChange: function (v) { rs.weights[d] = v; },
            }));
        });
    }

    // The confirm step: what changes, from and to, before anything is sent.
    function rsConfirm(title, text, changes, failed, onSave) {
        var m = rsEl('rs-modal');
        rsEl('rs-modal-title').textContent = title;
        rsEl('rs-modal-text').textContent = text;
        var list = rsEl('rs-modal-changes');
        list.textContent = '';
        changes.forEach(function (c) {
            var li = document.createElement('li');
            li.dataset.dim = c.dim;
            var name = document.createElement('span');
            name.className = 'rs-dim-name';
            name.textContent = c.dim;
            var from = document.createElement('span');
            from.className = 'rs-from';
            from.textContent = rsFix(c.from) + ' →';
            li.appendChild(name);
            li.appendChild(document.createTextNode(': '));
            li.appendChild(from);
            li.appendChild(document.createTextNode(' ' + rsFix(c.to)));
            list.appendChild(li);
        });
        var save = rsEl('rs-modal-save');
        save.disabled = false;
        save.textContent = 'Save';
        rs.modal = { onSave: onSave, failed: failed, back: document.activeElement };
        m.hidden = false;
        var cancel = rsEl('rs-modal-cancel');
        if (cancel) cancel.focus();
    }

    function rsCloseModal() {
        var m = rsEl('rs-modal');
        if (!m || m.hidden) return;
        if (rs.busy) return;         // a save in flight finishes first
        m.hidden = true;
        var back = rs.modal && rs.modal.back;
        rs.modal = null;
        if (back && back.focus && document.body.contains(back)) back.focus();
    }

    async function rsModalSave() {
        if (!rs.modal || rs.busy) return;
        var save = rsEl('rs-modal-save');
        save.disabled = true;
        save.textContent = 'Saving…';
        rs.busy = true;
        rs.seq++;
        rsSync();
        var failed = rs.modal.failed;
        var said;
        try {
            said = await rs.modal.onSave();
        } catch (e) {
            said = { ok: false, text: failed + rsWhy(e) };
        }
        rs.busy = false;
        rsCloseModal();
        rsSay(said.text);
        if (said.ok) {
            // Read back what was stored; it becomes the sliders' resting place.
            await loadShaping({ reset: said.part });
        } else {
            rsSync();
        }
    }

    function rsAskWeights() {
        var changes = rsWeightChanges();
        if (!changes.length || rs.busy) return;
        var body = {};
        Object.keys(rs.weights).forEach(function (k) { body[k] = rs.weights[k]; });
        rsConfirm('Update personality?',
            "This becomes your Host's new baseline: how it sounds and carries itself, on every model it runs on.",
            changes,
            "Couldn't update personality: ",
            async function () {
                var r = await fetchJSON('/api/brain/personality/weights', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ weights: body }),
                });
                return r && r.ok
                    ? { ok: true, part: 'weights', text: "Personality updated. This is your Host's baseline now." }
                    : { ok: false, text: "Couldn't update personality: " + rsRefusal(r) };
            });
    }

    function wireShaping() {
        var ws = rsEl('rs-weights-save');
        if (!ws) return;
        ws.addEventListener('click', rsAskWeights);
        rsEl('rs-weights-discard').addEventListener('click', function () {
            rs.weights = rsSeedWeights();
            rsRenderWeights();
            rsSync();
        });
        rsEl('rs-modal-save').addEventListener('click', rsModalSave);
        document.querySelectorAll('[data-rs-close]').forEach(function (el) {
            el.addEventListener('click', rsCloseModal);
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && rs.modal) rsCloseModal();
        });
    }

    // ── Utility ─────────────────────────────────────────
    // Escapes all five, so the result is safe as text and inside a quoted
    // attribute (the textContent→innerHTML trick left " and ' alone).
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }
    function attr(s) {
        return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // ── Network ─────────────────────────────────────────
    // Membership of the network: GET /api/network/status, POST
    // /api/network/join {code} and /api/network/leave. The network's refusals
    // ("this code is not valid", ...) arrive as the error's detail and are
    // shown as they are. Every value is set with textContent.
    var net = { busy: false };

    function netSay(text) {
        var m = document.getElementById('net-message');
        if (!m) return;
        m.textContent = text || '';
        m.hidden = !text;
    }

    function netWhy(e) {
        if (!e) return 'no answer';
        var d = e.detail;
        if (d && typeof d !== 'string') { try { d = JSON.stringify(d); } catch (_) { d = ''; } }
        return d || e.message || String(e);
    }

    // A passport lasts days, not months: the time of day matters.
    function netWhen(s) {
        if (!s) return '--';
        var d = new Date(s);
        return isNaN(d.getTime()) ? String(s) : d.toLocaleString(undefined, {
            year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit',
        });
    }

    function netRow(label, value) {
        var row = document.createElement('div');
        row.className = 'about-row';
        var l = document.createElement('div');
        l.className = 'about-row-label';
        l.textContent = label;
        var v = document.createElement('div');
        v.className = 'about-row-value';
        v.textContent = value == null || value === '' ? '--' : String(value);
        row.appendChild(l);
        row.appendChild(v);
        return row;
    }

    function renderNetwork(st) {
        var box = document.getElementById('net-state');
        var join = document.getElementById('net-join');
        var leave = document.getElementById('net-leave-row');
        if (!box || !join || !leave) return;
        box.textContent = '';
        var rows = document.createElement('div');
        rows.className = 'about-details';
        if (st && st.member) {
            rows.appendChild(netRow('Membership', 'This Host is a member of the network'));
            rows.appendChild(netRow('Network', st.network));
            rows.appendChild(netRow('Member Host id', st.entity_id));
            rows.appendChild(netRow('Passport valid until', netWhen(st.not_after)));
            rows.appendChild(netRow('Connection', st.connected ? 'Connected' : 'Not connected'));
        } else {
            rows.appendChild(netRow('Membership', 'This Host is not a member of the network'));
        }
        if (st && st.last_error) rows.appendChild(netRow('Last error', st.last_error));
        if (st && st.renew_note) rows.appendChild(netRow('Renewal', st.renew_note));
        box.appendChild(rows);
        join.hidden = !!(st && st.member);
        leave.hidden = !(st && st.member);
    }

    async function loadNetwork() {
        if (net.busy) return;
        try {
            renderNetwork(await fetchJSON('/api/network/status', { cache: 'no-store' }));
        } catch (e) {
            var box = document.getElementById('net-state');
            if (box) box.textContent = 'Could not read the membership: ' + netWhy(e);
        }
    }

    async function joinNetwork() {
        var input = document.getElementById('net-code');
        var btn = document.getElementById('net-join-btn');
        var code = input ? input.value.trim() : '';
        if (!code) {
            netSay('Enter the code from your account page, https://privatae.ai/account/network.');
            return;
        }
        if (net.busy) return;
        net.busy = true;
        if (btn) btn.disabled = true;
        netSay('Joining…');
        try {
            var st = await fetchJSON('/api/network/join', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code: code }),
            });
            if (input) input.value = '';
            netSay('');
            renderNetwork(st);
        } catch (e) {
            netSay(netWhy(e));
        } finally {
            net.busy = false;
            if (btn) btn.disabled = false;
        }
    }

    async function leaveNetwork() {
        if (net.busy) return;
        if (!window.confirm('Leave the network? This Host stops using the App Store and the '
                + 'community board, and its passport is revoked and deleted. Joining again '
                + 'takes a new code.')) return;
        var btn = document.getElementById('net-leave-btn');
        net.busy = true;
        if (btn) btn.disabled = true;
        try {
            var st = await fetchJSON('/api/network/leave', { method: 'POST' });
            netSay(st && st.note ? st.note : 'This Host has left the network.');
            renderNetwork(st);
        } catch (e) {
            netSay(netWhy(e));
        } finally {
            net.busy = false;
            if (btn) btn.disabled = false;
        }
    }

    // ── Boot ────────────────────────────────────────────
    function viewExists(view) {
        return !!document.querySelector('.cseries-nav-btn[data-view="' + view + '"]');
    }

    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('.cseries-nav-btn').forEach(function (b) {
            b.addEventListener('click', function () { showView(b.dataset.view); });
        });

        // Deep-link: the shell launches with #view=<tab> (app-runner) and
        // re-targets an already-open window via the cseries:app-view message —
        // e.g. the desktop right-click "Change Wallpaper".
        var m = (location.hash || '').match(/view=([a-z-]+)/i);
        if (m && viewExists(m[1])) showView(m[1]);
        window.addEventListener('message', function (e) {
            if (e.origin !== window.location.origin) return;
            var d = e.data;
            if (d && d.type === 'cseries:app-view' && typeof d.view === 'string') {
                var v = d.view.replace(/^#?view=/, '');
                if (viewExists(v)) showView(v);
            }
        });

        // ── Appearance tab ───────────────────────────────
        loadAppearanceTab();

        // ── Response Shaping tab ─────────────────────────
        wireShaping();

        // ── Network tab ──────────────────────────────────
        var netJoin = document.getElementById('net-join-btn');
        if (netJoin) netJoin.addEventListener('click', joinNetwork);
        var netCode = document.getElementById('net-code');
        if (netCode) netCode.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') joinNetwork();
        });
        var netLeave = document.getElementById('net-leave-btn');
        if (netLeave) netLeave.addEventListener('click', leaveNetwork);

        var connectBtn = document.getElementById('btn-connect-chat');
        if (connectBtn) connectBtn.addEventListener('click', openSynModal);
        var genBtn = document.getElementById('syn-generate-btn');
        if (genBtn) genBtn.addEventListener('click', generateSynToken);
        var copyBtn = document.getElementById('syn-copy-config');
        if (copyBtn) copyBtn.addEventListener('click', copySynConfig);
        document.querySelectorAll('[data-syn-close]').forEach(function (el) {
            el.addEventListener('click', closeSynModal);
        });
        document.addEventListener('click', function (e) {
            var btn = e.target.closest && e.target.closest('[data-syn-revoke]');
            if (btn) revokeSynToken(btn.getAttribute('data-syn-revoke'));
        });
        document.querySelectorAll('#syn-client-picker .syn-client-tab').forEach(function (tab) {
            tab.addEventListener('click', function () {
                var which = tab.getAttribute('data-client');
                if (!SYN_CLIENTS[which]) return;
                _synActiveClient = which;
                _renderSynConfig();
            });
        });

        loadAll();
        setInterval(loadAll, 20000);
    });
})();
