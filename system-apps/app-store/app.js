/* ================================================================
 * App Store — where the user finds apps, organized by WHERE each
 * app runs and what it costs. The price is resources: the old store was one undifferentiated grid; this
 * one makes the app types legible to a non-technical user.
 *
 *   On your machine        installable apps. Static ones cost no
 *                          background memory; runtime apps (Browser,
 *                          VS Code) run in their own container on
 *                          demand and, when the manifest declares a
 *                          baseline_memory_mi, the card says what
 *                          they draw from the plan while running.
 *   For your public sites  the admitted zone catalog (Ghost, Uptime
 *                          Kuma, …). BROWSE-ONLY here — the Sites
 *                          app's Catalog tab owns deployment, so the
 *                          card opens Sites. Part of the public-zone
 *                          add-on; no per-app price data exists, so
 *                          none is shown.
 *
 * HONESTY RULE (hard, from the rebuild brief): no invented numbers.
 * A figure the data doesn't carry renders as absent — never $0,
 * never 0Mi — and an unreachable source says so in plain words.
 *
 * Talks to:
 *   GET  /api/applications/store/catalog        — list + install state
 *                                                 (+ image_baked, runtime_kind,
 *                                                  baseline_memory_mi)
 *   GET  /api/applications/store/icon/:id       — per-app catalog icon
 *   GET  /api/applications/store/preview/:id    — pre-install consent data
 *                                                 (egress, permissions, runtime, cost)
 *   POST /api/applications/install              — JSON {app_id, version}
 *   GET  /api/applications/sites/catalog/templates — zone catalog (browse only)
 *   GET  /api/system/resources                  — the machine's own memory numbers
 * ================================================================ */

(function () {
    'use strict';

    var state = {
        filter: 'all',   // all | machine | sites | updates | installed
        apps: [],        // machine-lane catalog entries
        zones: null,     // zone templates; null = not loaded, [] = loaded empty
        zonesError: null,// plain-words reason the zone list is unavailable
        res: null,       // /api/system/resources snapshot (null until first read)
        resError: false, // true once a read failed and nothing is cached
        busy: {},        // app_id -> true while install in flight
        // site_id -> true / a plain sentence, for "bring my data from <site>".
        // Keyed by SITE rather than by app: one app can have offers from more
        // than one site and each row reports for itself.
        dataMoveBusy: {},
        dataMoveNote: {},
        loaded: false,
        query: '',       // free-text search over both lanes
        modalAppId: null,// app_id currently shown in the detail modal
    };

    // ── Boot ────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('.cseries-nav-btn').forEach(function (b) {
            b.addEventListener('click', function () {
                setFilter(b.getAttribute('data-filter'));
            });
        });

        document.querySelectorAll('[data-modal-close]').forEach(function (el) {
            el.addEventListener('click', closeDetailModal);
        });

        // Escape closes the detail modal when it's open; Tab is trapped.
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                var modal = document.getElementById('detail-modal');
                if (modal && !modal.hidden) {
                    e.stopPropagation();
                    closeDetailModal();
                }
            } else if (e.key === 'Tab') {
                trapModalFocus(e);
            } else if (e.key === 'Enter' || e.key === ' ') {
                // Zone cards are divs with role=button; make the keyboard work.
                var t = e.target && e.target.closest && e.target.closest('[data-open-sites]');
                if (t && t.tagName !== 'BUTTON') {
                    e.preventDefault();
                    openSites(t.getAttribute('data-open-sites'));
                }
            }
        });

        // Free-text search over both lanes.
        var search = document.getElementById('catalog-search');
        if (search) {
            search.addEventListener('input', function () {
                state.query = search.value || '';
                render();
            });
        }

        document.addEventListener('click', function (e) {
            // Buy button (paid app) — purchase + install in one flow.
            var buyBtn = e.target.closest && e.target.closest('[data-app-buy]');
            if (buyBtn) {
                var bAppId = buyBtn.getAttribute('data-app-buy');
                var bVersion = buyBtn.getAttribute('data-version') || null;
                var bPrice = parseInt(buyBtn.getAttribute('data-price-micros') || '0', 10);
                e.stopPropagation();
                buyApp(bAppId, bVersion, bPrice);
                return;
            }
            var btn = e.target.closest && e.target.closest('[data-app-install]');
            if (btn) {
                var appId = btn.getAttribute('data-app-install');
                var version = btn.getAttribute('data-version') || null;
                e.stopPropagation();
                installApp(appId, version);
                return;
            }
            var dataBtn = e.target.closest && e.target.closest('[data-data-import]');
            if (dataBtn) {
                e.stopPropagation();
                bringDataOver(dataBtn.getAttribute('data-data-import'),
                              dataBtn.getAttribute('data-site-id'),
                              dataBtn.getAttribute('data-member-email'));
                return;
            }
            var openBtn = e.target.closest && e.target.closest('[data-app-open]');
            if (openBtn) {
                e.stopPropagation();
                openApp(openBtn.getAttribute('data-app-open'));
                return;
            }
            var uninstallBtn = e.target.closest && e.target.closest('[data-app-uninstall]');
            if (uninstallBtn) {
                e.stopPropagation();
                armUninstall(uninstallBtn, uninstallBtn.getAttribute('data-app-uninstall'));
                return;
            }
            // Resident (always-on) app switch: enable = start now + keep it
            // running (boot, crash restart); disable = stop now + stay off.
            var resBtn = e.target.closest && e.target.closest('.resident-toggle');
            if (resBtn) {
                e.stopPropagation();
                toggleResident(resBtn, resBtn.getAttribute('data-app'), resBtn.getAttribute('data-op'));
                return;
            }
            // Zone cards: deployment belongs to Sites. The button (and the
            // card itself) open the Sites app; the Catalog tab there carries
            // the same template with its deploy form.
            var siteBtn = e.target.closest && e.target.closest('[data-open-sites]');
            if (siteBtn) {
                e.stopPropagation();
                openSites(siteBtn.getAttribute('data-open-sites'));
                return;
            }
            var card = e.target.closest && e.target.closest('[data-app-detail]');
            if (card) {
                openDetailModal(card.getAttribute('data-app-detail'));
            }
        });

        loadCatalog();
        loadZones();
        loadResources();
        setInterval(loadCatalog, 60000);
        setInterval(loadResources, 30000);
        tickClock();
        setInterval(tickClock, 30000);
    });

    function tickClock() {
        var el = document.getElementById('footer-ts');
        if (el) el.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function setFilter(f) {
        state.filter = f;
        document.querySelectorAll('.cseries-nav-btn').forEach(function (b) {
            var on = b.getAttribute('data-filter') === f;
            b.classList.toggle('active', on);
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
        render();
    }

    // ── Data ────────────────────────────────────────────
    async function loadCatalog() {
        try {
            var data = await fetchJSON('/api/applications/store/catalog');
            state.apps = (data && data.apps) || [];
            state.loaded = true;
            render();
            updateSubtitle();
        } catch (err) {
            if (!state.loaded) {
                var grid = document.getElementById('machine-grid');
                var section = document.getElementById('machine-section');
                if (section) section.hidden = false;
                if (grid) {
                    grid.innerHTML = '<div class="cseries-empty">' +
                        '<span class="empty-title">Could not load the catalog</span>' +
                        esc(err.message || 'unknown error') +
                        '</div>';
                }
            }
            setFooter('catalog unreachable');
        }
    }

    // Zone catalog — read-only browse data, same kernel route the Sites app's
    // Catalog tab uses. A failure here must not fake a list: the section
    // degrades to one explainer card that opens Sites, and says plainly that
    // the list could not be loaded here.
    async function loadZones() {
        try {
            var d = await fetchJSON('/api/applications/sites/catalog/templates');
            state.zones = (d && d.templates) || [];
            state.zonesError = null;
        } catch (err) {
            state.zones = null;
            state.zonesError = err.message || 'unreachable';
        }
        render();
        updateSubtitle();
    }

    // The machine's own numbers — the same shell-trusted self-read the
    // taskbar tray renders. Sections (entity container / database / network
    // hub) each report their OWN memory; one that reports nothing is left
    // out rather than guessed at.
    async function loadResources() {
        try {
            state.res = await fetchJSON('/api/system/resources');
            state.resError = false;
        } catch (err) {
            if (!state.res) state.resError = true; // keep the last good read if we had one
        }
        renderMachineStrip();
    }

    function updateSubtitle() {
        var sub = document.getElementById('catalog-subtitle');
        if (sub) {
            var parts = [];
            var m = machineApps().length;
            if (state.loaded) parts.push(m + (m === 1 ? ' app for your machine' : ' apps for your machine'));
            if (state.zones && state.zones.length) {
                parts.push(state.zones.length + ' for your public sites');
            }
            var updates = updatableApps().length;
            if (updates > 0) parts.push(updates + ' update' + (updates === 1 ? '' : 's'));
            sub.textContent = parts.length ? parts.join(' · ') : 'Loading…';
        }
        var pill = document.getElementById('filter-updates-pill');
        if (pill) {
            var n = updatableApps().length;
            if (n > 0) {
                pill.textContent = String(n);
                pill.hidden = false;
            } else {
                pill.hidden = true;
            }
        }
        setFooter('');
    }

    function setFooter(text) {
        var el = document.getElementById('footer-status');
        if (el) el.textContent = text;
    }

    // ── Lane selection ──────────────────────────────────
    // The machine lane hides image-baked STATIC apps: the image ships those
    // to every machine at boot, they use no memory and there is nothing to
    // choose — listing them was noise. Image-baked apps
    // WITH a runtime (VS Code) stay: their cost is the point of this store.
    // An image-baked entry whose runtime is unknown is treated as static —
    // image apps are always installed, so a readable manifest is the normal
    // case and "unknown" means the conservative, hidden branch.
    function machineApps() {
        return state.apps.filter(function (a) {
            if (!a.image_baked) return true;
            return a.runtime_kind === 'external' || a.runtime_kind === 'process';
        });
    }

    function updatableApps() {
        return machineApps().filter(function (a) { return a.install_status === 'update_available'; });
    }

    function installedApps() {
        return machineApps().filter(function (a) {
            return a.install_status === 'up_to_date' ||
                   a.install_status === 'installed' ||
                   a.install_status === 'update_available';
        });
    }

    function matchesQuery(hay) {
        var q = state.query.trim().toLowerCase();
        if (!q) return function () { return true; };
        return function (a) {
            return hay(a).some(function (s) {
                return String(s || '').toLowerCase().indexOf(q) !== -1;
            });
        };
    }

    // ── Render ──────────────────────────────────────────
    function render() {
        var showMachine = state.filter !== 'sites';
        var showSites = state.filter === 'all' || state.filter === 'sites';

        var apps;
        switch (state.filter) {
            case 'updates':   apps = updatableApps(); break;
            case 'installed': apps = installedApps(); break;
            default:          apps = machineApps();
        }
        apps = apps.filter(matchesQuery(function (a) {
            return [a.name, a.summary, a.app_id];
        }));

        renderMachineStrip();
        renderFeatured();

        var machineSection = document.getElementById('machine-section');
        var machineGrid = document.getElementById('machine-grid');
        if (machineSection && machineGrid) {
            machineSection.hidden = !showMachine;
            if (showMachine) machineGrid.innerHTML = apps.map(renderCard).join('');
        }

        renderZoneSection(showSites);

        // One shared empty state so "no results" is a sentence, not a blank page.
        var zonesShown = showSites ? visibleZones().length : 0;
        if (state.zones === null && showSites) zonesShown = 1; // explainer card counts
        var emptyBox = document.getElementById('catalog-empty');
        if (emptyBox) {
            var isEmpty = state.loaded && !apps.length && !zonesShown;
            emptyBox.hidden = !isEmpty;
            if (isEmpty) {
                document.getElementById('catalog-empty-title').innerHTML = emptyMessage();
            }
        }
    }

    function visibleZones() {
        if (!state.zones) return [];
        var match = matchesQuery(function (z) {
            var h = z.header || {};
            return [z.display_name, h.slogan, h.category].concat(h.tags || []);
        });
        return state.zones.filter(match).slice().sort(function (a, b) {
            var ca = ((a.header || {}).category || '').toLowerCase();
            var cb = ((b.header || {}).category || '').toLowerCase();
            if (ca !== cb) return ca < cb ? -1 : 1;
            return (a.display_name || '').localeCompare(b.display_name || '');
        });
    }

    function renderZoneSection(show) {
        var section = document.getElementById('sites-section');
        var grid = document.getElementById('sites-grid');
        if (!section || !grid) return;
        section.hidden = !show;
        if (!show) return;
        if (state.zones === null) {
            // Could not load the list — say so, do not fake one.
            grid.innerHTML =
                '<div class="zone-card zone-card-explainer">' +
                    '<div class="zone-card-name">One-click apps for your public sites</div>' +
                    '<div class="zone-card-slogan">Blogs, bookings, uptime monitors, bookmarks and more ' +
                        'run on your public web zone as part of that add-on — never on this ' +
                        'machine. The list could not be loaded here right now' +
                        (state.zonesError ? ' (' + esc(state.zonesError) + ')' : '') +
                        ', but the Sites app’s Catalog tab has it.</div>' +
                    '<div class="zone-card-footer">' +
                        '<button class="cseries-btn primary" data-open-sites="">Open Sites</button>' +
                    '</div>' +
                '</div>';
            return;
        }
        var zones = visibleZones();
        if (!zones.length) {
            grid.innerHTML = '<div class="cseries-empty"><span class="empty-title">' +
                (state.query.trim() ? 'No site apps match your search' : 'No site apps available yet') +
                '</span></div>';
            return;
        }
        grid.innerHTML = zones.map(renderZoneCard).join('');
    }

    // Zone card. Initial-letter tile, not a vendor logo — same deliberate
    // choice the Sites catalog made (no fabricated brand marks). No price is
    // rendered because no per-app price data exists: these run under the
    // public-zone add-on, and the card says exactly that.
    function renderZoneCard(z) {
        var h = z.header || {};
        var name = z.display_name || z.template_key;
        var letter = (name || '?').charAt(0).toUpperCase();
        var cat = h.category
            ? '<span class="badge badge-muted">' + esc(h.category) + '</span>' : '';
        return '<div class="zone-card" data-open-sites="' + attr(z.template_key) + '" ' +
                    'role="button" tabindex="0" aria-label="' + attr(name + ' — deploy in Sites') + '">' +
            '<div class="zone-card-head">' +
                '<div class="zone-tile" aria-hidden="true">' + esc(letter) + '</div>' +
                '<div class="zone-card-name">' + esc(name) + '</div>' +
            '</div>' +
            '<div class="zone-card-slogan">' + esc(h.slogan || '') + '</div>' +
            '<div class="zone-card-where">Runs on your public web zone — part of that ' +
                'add-on, not this machine’s memory</div>' +
            '<div class="zone-card-footer">' +
                cat +
                '<button class="cseries-btn primary" data-open-sites="' + attr(z.template_key) +
                    '">Deploy in Sites</button>' +
            '</div>' +
        '</div>';
    }

    // Open the Sites app. Its Catalog tab owns the deploy flow; Sites has no
    // deep-link-to-tab contract today, so the footer says where to go.
    function openSites(templateKey) {
        var opened = false;
        try {
            if (window.parent && window.parent.AppRunner) {
                window.parent.AppRunner.launch('sites');
                opened = true;
            }
        } catch (e) { /* cross-origin or unavailable */ }
        if (opened) {
            setFooter('Sites is open — the Catalog tab has ' +
                (templateKey ? templateKey : 'the one-click apps') + ' ready to deploy.');
        } else {
            setFooter('Open the Sites app from the dock — its Catalog tab has the one-click apps.');
        }
        setTimeout(function () { setFooter(''); }, 6000);
    }

    // ── The machine strip ───────────────────────────────
    // "Display the plan's allocated resources so the user can see whether an
    // app fits." One row per thing the user is running, with where it runs —
    // the server builds that list (`machine`, routes/system.py) so this strip
    // and the taskbar tray can never quote two different numbers. An app in
    // its own pod is in here whether awake or asleep: it holds its
    // reservation either way, and "does the next app fit" is exactly what
    // that reservation answers.
    function machineSections() {
        var r = state.res;
        if (!r) return [];
        var mach = r.machine;
        if (mach && mach.units && mach.units.length) {
            return mach.units.map(function (u) {
                return { label: (u.label || u.id) +
                             (u.running === false ? ' (asleep)' : ''),
                         where: u.where || '',
                         used: u.used_bytes, limit: u.ceiling_bytes || null };
            });
        }
        // Older kernel than this app bundle: fall back to the pod-only view,
        // and never claim a total from it (renderMachineStrip needs every
        // section to carry a limit, which this path deliberately cannot).
        var out = [];
        var m = r.memory || {};
        if (m.used_bytes != null) {
            out.push({ label: 'Apps & host', where: 'host pod',
                       used: m.used_bytes, limit: null });
        }
        var db = r.db || {};
        if (db.present && db.memory && db.memory.used_bytes != null) {
            out.push({ label: 'Database', where: 'host pod',
                       used: db.memory.used_bytes, limit: null });
        }
        var hub = r.hub || {};
        var hs = hub.self || {};
        if (hub.reachable && hs.memory_used_bytes != null) {
            out.push({ label: 'Network', where: 'host pod',
                       used: hs.memory_used_bytes, limit: null });
        }
        return out;
    }

    function renderMachineStrip() {
        var strip = document.getElementById('machine-strip');
        if (!strip) return;
        if (state.filter === 'sites') { strip.hidden = true; return; }
        if (state.resError && !state.res) {
            strip.hidden = false;
            strip.innerHTML = '<div class="machine-strip-head label-sys">Your machine</div>' +
                '<div class="machine-strip-note">Couldn’t read this machine’s memory right now.</div>';
            return;
        }
        var sections = machineSections();
        if (!sections.length) { strip.hidden = true; return; }
        strip.hidden = false;

        // A total needs every section to have reported BOTH numbers — one
        // null used_bytes would make the sum NaN, and a partial sum wearing
        // the label "your machine" is the wrong-number-with-a-real-label case
        // this strip has always refused.
        //
        // `machine.partial` is part of that test, not decoration: when the
        // server could not enumerate the app pods at all it returns the three
        // container units, each complete — so every local check passes and the
        // strip would print a confident total that silently omits every app,
        // while the tray two inches away says "(partial)". One screen, one
        // answer.
        // The denominator is the PLAN the operator stamped, never a sum of
        // ceilings — those add up to more than the plan on purpose, because a
        // ceiling reserves nothing and a host may burst into free node memory.
        var mach = state.res && state.res.machine;
        var allLimits = !!(mach && mach.plan_bytes && mach.used_bytes != null);
        var totalLine = '';
        if (allLimits) {
            var used = mach.used_bytes, limit = mach.plan_bytes;
            // `partial` marks a total missing SOME piece's usage — an awake
            // harness pod reports to nobody, so it is partial for as long as
            // the editor is open. That must not delete the number in exactly
            // the state where the user most wants it: show it as a floor.
            var atLeast = mach.partial ? 'at least ' : '';
            totalLine =
                '<div class="machine-total">' +
                    '<span class="machine-total-num">' + esc(atLeast + fmtBytes(used)) + '</span>' +
                    '<span class="machine-total-of"> of ' + esc(fmtBytes(limit)) +
                        ' memory in use</span>' +
                '</div>' +
                '<div class="machine-bar" aria-hidden="true"><div class="machine-bar-fill" style="width:' +
                    Math.min(100, Math.round(100 * used / limit)) + '%"></div></div>';
        }
        var rows = sections.map(function (s) {
            // "up to", never "of": s.limit is this piece's burst CEILING, and
            // "of" is the word the total line uses for the plan. Reading
            // "12M of 2.1G" as an allowance is the misreading this whole
            // change exists to stop.
            return '<span class="machine-part">' + esc(s.label) +
                (s.where ? ' <i class="machine-part-where">' + esc(s.where) + '</i>' : '') +
                ' <b>' + esc(s.used != null ? fmtBytes(s.used) : '—') + '</b>' +
                (s.limit != null ? ' <i class="machine-part-where">up to ' +
                    esc(fmtBytes(s.limit)) + '</i>' : '') + '</span>';
        }).join('');

        // Resident app processes — the concrete answer to "what is using it".
        // The list comes from the kernel's supervisor join; empty is the
        // healthy common case (static apps hold no resident process).
        var residents = ((state.res && state.res.apps) || []).filter(function (a) {
            return a.alive && a.rss_bytes != null;
        }).map(function (a) {
            return '<span class="machine-part">' + esc(a.app_id) +
                ' app <b>' + esc(fmtBytes(a.rss_bytes)) + '</b></span>';
        }).join('');

        strip.innerHTML =
            '<div class="machine-strip-head label-sys">Your machine</div>' +
            totalLine +
            '<div class="machine-parts">' + rows + residents + '</div>' +
            '<div class="machine-strip-note">Everything on this machine shares this memory. ' +
                'An app card that names a memory figure adds that much while the app runs.</div>';
    }

    // ── Featured strip ──────────────────────────────────
    function renderFeatured() {
        var section = document.getElementById('featured');
        var card = document.getElementById('featured-card');
        if (!section || !card) return;
        if (state.filter !== 'all' || state.query.trim()) { section.hidden = true; return; }
        var pool = machineApps();
        var pick = pool.find(function (a) {
            return a.install_status === 'not_installed' && a.is_default;
        }) || pool.find(function (a) { return a.install_status === 'not_installed'; });
        if (!pick) { section.hidden = true; return; }
        section.hidden = false;
        card.innerHTML =
            '<img class="featured-icon" src="' + attr(iconUrl(pick)) + '" alt="" onerror="this.style.visibility=\'hidden\'">' +
            '<div class="featured-body">' +
                '<div class="featured-eyebrow label-sys">Featured</div>' +
                '<div class="featured-name">' + esc(pick.name || pick.app_id) + '</div>' +
                '<div class="featured-summary">' + esc(pick.summary || '') + '</div>' +
                costLine(pick) +
            '</div>' +
            '<div class="featured-action">' +
                actionButton(pick) +
            '</div>';
        card.setAttribute('data-app-detail', pick.app_id);
    }

    function emptyMessage() {
        if (state.query.trim()) {
            return 'No apps match “' + esc(state.query.trim()) + '”';
        }
        switch (state.filter) {
            case 'updates': return 'Everything is up to date';
            case 'installed': return 'Nothing installed yet';
            default: return 'The catalog is empty';
        }
    }

    function iconUrl(a) {
        // local_only entries (image-baked runtime apps, e.g. VS Code) have no
        // central-store icon row; the kernel hands their on-disk icon instead.
        if (a.local_only && a.icon_url) return a.icon_url;
        return '/api/applications/store/icon/' + encodeURIComponent(a.app_id);
    }

    // ── Cost line ───────────────────────────────────────
    // One sentence about where the app runs and what it takes, built ONLY
    // from fields the data actually carries:
    //   baseline_memory_mi        -> the declared figure (browser: 1024)
    //   runtime_kind='external'   -> own container, on demand
    //   runtime_kind='process'    -> resident process on this machine
    //                                (+ live RSS when it is running now)
    //   runtime_kind='static'     -> no background memory use
    //   unknown (not installed,   -> NOTHING. The detail view fetches the
    //     preview not yet read)      signed manifest and fills this in.
    function costLine(a) {
        var kind = a.runtime_kind;
        var mi = a.baseline_memory_mi;
        var bits = [];
        if (mi != null && mi > 0) {
            bits.push('Uses up to ' + fmtMi(mi) + ' of this machine’s memory while open');
            if (kind === 'external') bits.push('starts when you open it, sleeps when you close it');
        } else if (kind === 'external') {
            bits.push('Runs in its own container — active only while open');
        } else if (kind === 'process' && a.runtime_lifecycle === 'resident') {
            // Resident lifecycle: an always-on process the USER
            // switches. resident_enabled null/true = on, false = off.
            var liveR = residentRss(a.app_id);
            var on = a.resident_enabled !== false;
            bits.push((on ? 'Runs around the clock on this machine' : 'Always-on app, switched off') +
                (liveR != null ? ' — using ' + fmtBytes(liveR) + ' right now' : ''));
            return '<div class="app-card-cost">' + esc(bits.join(' · ')) +
                ' <button type="button" class="resident-toggle" data-app="' + attr(a.app_id) +
                '" data-op="' + (on ? 'disable' : 'enable') + '">' + (on ? 'Turn off' : 'Turn on') +
                '</button></div>';
        } else if (kind === 'process') {
            var live = residentRss(a.app_id);
            bits.push('Runs in the background on this machine while active' +
                (live != null ? ' — using ' + fmtBytes(live) + ' right now' : ''));
        } else if (kind === 'static') {
            bits.push('No background memory use');
        } else {
            return '';
        }
        return '<div class="app-card-cost">' + esc(bits.join(' · ')) + '</div>';
    }

    async function toggleResident(btn, appId, op) {
        btn.disabled = true;
        btn.textContent = op === 'enable' ? 'Starting…' : 'Stopping…';
        try {
            await fetchJSON('/api/applications/' + encodeURIComponent(appId) + '/runtime', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ op: op }),
            });
        } catch (err) {
            btn.disabled = false;
            btn.textContent = (op === 'enable' ? 'Turn on' : 'Turn off') + ' — failed: ' + err.message;
            return;
        }
        // Re-read the catalog + machine strip so the card's cost line reflects
        // the new state (runtime_lifecycle / resident_enabled ride the catalog
        // rows; the live RSS rides /api/system/resources).
        try { await loadCatalog(); await loadResources(); render(); } catch (e2) { /* next poll */ }
    }

    function residentRss(appId) {
        var apps = (state.res && state.res.apps) || [];
        for (var i = 0; i < apps.length; i++) {
            if (apps[i].app_id === appId && apps[i].alive && apps[i].rss_bytes != null) {
                return apps[i].rss_bytes;
            }
        }
        return null;
    }

    // ── The listing: who made it, where it came from, how it was built ──
    // Every row renders
    // ONLY from fields the catalog carries (controller /appstore/list + /info,
    // migration 047). A missing field renders nothing — never a made-up
    // developer, never a zero count dressed as a fact.
    // The forge the catalog's publisher logins and source repos live on.
    // Unset, they render as plain text with no link.
    var GITEA_BASE = '';

    function forgeLink(path, label) {
        if (!GITEA_BASE) return esc(label);
        return '<a class="dev-link" href="' + attr(GITEA_BASE + '/' + path) +
               '" target="_blank" rel="noopener">' + esc(label) + '</a>';
    }

    function developerLine(a) {
        var who = a.publisher_login
            ? forgeLink(a.publisher_login, 'by ' + a.publisher_login)
            : esc(a.publisher || 'Privatae');
        var n = (typeof a.install_count === 'number' && a.install_count > 0)
            ? ' · ' + a.install_count + (a.install_count === 1 ? ' install' : ' installs')
            : '';
        return who + n;
    }

    function licenceWords(lic) {
        if (!lic || typeof lic !== 'object') return '';
        var parts = ['You may run it on your machine.'];
        parts.push(lic.redistribute
            ? 'You may republish it to the store under your own name.'
            : 'You may not republish it as a new app.');
        parts.push(lic.publish_site
            ? 'You may publish a website built from it.'
            : 'You may not publish a website built from it.');
        parts.push((lic.flavors || 'open') === 'open'
            ? 'Others may publish flavours (children of this app, with the author credited).'
            : 'No published flavours.');
        var out = parts.join(' ');
        if (lic.notes) out += ' Author’s note: “' + esc(String(lic.notes)) + '”';
        return out;
    }

    function renderListingRows(a) {
        var rows = '';
        function row(label, valueHtml) {
            if (!valueHtml) return '';
            return '<div class="detail-row">' +
                       '<div class="detail-row-label label-sys">' + label + '</div>' +
                       '<div class="detail-row-value detail-row-prose">' + valueHtml + '</div>' +
                   '</div>';
        }
        if (a.publisher_login) {
            rows += row('Developer', forgeLink(a.publisher_login, a.publisher_login) +
                (GITEA_BASE ? ' — their profile on the forge' : ''));
        } else if (a.publisher) {
            rows += row('Developer', esc(a.publisher));
        }
        var src = '';
        if (a.upstream_url) {
            // A link only for a web address. The value comes from the store's
            // catalog (the publisher's own metadata), and escaping cannot stop a
            // `javascript:` href, so anything that is not http(s) is shown as
            // the text it is (as oversight does for a pull-request URL).
            var up = String(a.upstream_url);
            var upLabel = esc(up.replace(/^https?:\/\//i, ''));
            src += 'Based on ' + (/^https?:\/\//i.test(up)
                       ? '<a class="dev-link" href="' + attr(up) + '" target="_blank" rel="noopener">' + upLabel + '</a>'
                       : upLabel) +
                   (a.upstream_license ? ' (' + esc(a.upstream_license) + ')' : '') + '. ';
        }
        if (a.source_repo) {
            src += 'Source: ' + forgeLink(a.source_repo, a.source_repo) + '.';
        }
        rows += row('Source', src);
        var b = a.built_by;
        if (b && typeof b === 'object') {
            var built = b.host_commits + ' commit' + (b.host_commits === 1 ? '' : 's') +
                ' by the developer’s host' +
                (b.runs && b.runs.length ? ' across ' + b.runs.length + ' numbered run' +
                    (b.runs.length === 1 ? '' : 's') : '') +
                ', ' + b.user_commits + ' by the developer' +
                (b.unlinked ? ' (' + b.unlinked + ' host commit' + (b.unlinked === 1 ? '' : 's') +
                    ' with no run link)' : '') +
                (b.commit ? '. Packed from commit ' + esc(b.commit) : '') +
                (b.files_attested ? '; ' + b.files_attested + ' files attested by the platform.' : '.');
            rows += row('Built by', built);
        }
        // The licence comes from the app's own SIGNED manifest, which the
        // kernel reads for the consent surface (/store/preview → a._preview,
        // with redistribute / publish_site / source_visible / flavors /
        // notes). The catalog list rows do not carry it; /appstore/info does,
        // but nothing on the pod calls it — so the preview is the source, and
        // the row appears once the preview has loaded (loadPreview repaints).
        var lic = a.license || (a._preview && !a._preview._error ? a._preview.license : null);
        rows += row('Licence', licenceWords(lic));
        if (typeof a.install_count === 'number') {
            rows += row('Installs', a.install_count === 0 ? 'None yet' : String(a.install_count));
        }
        return rows;
    }

    function renderCard(a) {
        var statusBadge = renderStatusBadge(a);
        var deprecatedBadge = a.deprecated
            ? '<span class="badge badge-deprecated" title="This app is being retired">Deprecated</span>'
            : '';
        // PRIVATE TO A GROUP. `distribution` is the store's source of truth for
        // which lane an app is in — the platform database's migration 045 demoted `audience`
        // and `public` to columns a trigger derives, so reading either of those
        // instead would be reading the shadow rather than the thing.
        //
        // An app only reaches this catalog at all if the caller is on its
        // group's roster (the store filters the circle lane per entity), so the
        // badge is not a permission hint — it is the answer to "why can I see
        // this and my friend cannot", which is otherwise invisible.
        var circleBadge = a.distribution === 'circle'
            ? '<span class="badge badge-circle" title="Private to a group: only '
              + 'members of the group this app was published to can see or '
              + 'install it">Group only</span>'
            : '';
        var cardClass = a.deprecated ? 'app-card app-card-deprecated' : 'app-card';
        return '<div class="' + cardClass + '" data-app-detail="' + attr(a.app_id) + '">' +
            '<img class="app-card-icon" src="' + attr(iconUrl(a)) + '" alt="" onerror="this.style.visibility=\'hidden\'">' +
            '<div class="app-card-name">' + esc(a.name || a.app_id) + '</div>' +
            '<div class="app-card-summary">' + esc(a.summary || '') + '</div>' +
            costLine(a) +
            '<div class="app-card-meta label-sys">' + developerLine(a) + '</div>' +
            '<div class="app-card-footer">' +
                circleBadge +
                deprecatedBadge +
                statusBadge +
                actionButton(a) +
            '</div>' +
        '</div>';
    }

    // ── Price formatting ────────────────────────────────
    // price_micros: 1 USD = 1,000,000. 0 = free; anything > 0 needs an
    // entitlement before install will succeed.
    function formatPrice(micros) {
        if (!micros || micros <= 0) return 'Free';
        return '$' + (micros / 1e6).toFixed(2);
    }

    function isPaid(a) {
        return typeof a.price_micros === 'number' && a.price_micros > 0;
    }

    function renderStatusBadge(a) {
        switch (a.install_status) {
            case 'up_to_date':
            case 'installed':
                return '<span class="badge badge-ok">Installed</span>';
            case 'update_available':
                return '<span class="badge badge-warn">Update available</span>';
            default:
                // Paid apps render a price badge; free apps keep the
                // existing "Free" badge styling.
                if (isPaid(a)) {
                    return '<span class="badge badge-price">' +
                           formatPrice(a.price_micros) + '</span>';
                }
                return '<span class="badge badge-muted">Free</span>';
        }
    }

    // inDetail=false (list/featured): Get/Update/Buy route through the detail
    // modal so the user sees the app's egress hosts + permissions + cost before
    // installing (informed consent). inDetail=true (inside the modal): the
    // button performs the real install/buy, with that access shown above it.
    function actionButton(a, inDetail) {
        if (state.busy[a.app_id]) {
            return '<button class="cseries-btn" disabled>Working&hellip;</button>';
        }
        switch (a.install_status) {
            case 'not_installed':
                if (!inDetail) {
                    var getLabel = isPaid(a) ? ('Buy ' + formatPrice(a.price_micros)) : 'Get';
                    return '<button class="cseries-btn primary" data-app-detail="' + attr(a.app_id) +
                        '">' + getLabel + '</button>';
                }
                if (isPaid(a)) {
                    return '<button class="cseries-btn primary" data-app-buy="' + attr(a.app_id) +
                        '" data-version="' + attr(a.latest_version) +
                        '" data-price-micros="' + attr(String(a.price_micros)) +
                        '">Buy ' + formatPrice(a.price_micros) + ' &amp; allow access</button>';
                }
                return '<button class="cseries-btn primary" data-app-install="' + attr(a.app_id) +
                    '" data-version="' + attr(a.latest_version) + '">Install &amp; allow access</button>';
            case 'update_available':
                if (!inDetail) {
                    return '<button class="cseries-btn primary" data-app-detail="' + attr(a.app_id) +
                        '">Update</button>';
                }
                return '<button class="cseries-btn primary" data-app-install="' + attr(a.app_id) +
                    '" data-version="' + attr(a.latest_version) + '">Update &amp; allow access</button>';
            default:
                // Image-baked runtime apps (local_only) have no store bundle to
                // remove — the image lays them back down at boot — so no
                // Uninstall button is offered for them.
                var uninstall = a.local_only ? '' :
                    '<button class="cseries-btn ghost" data-app-uninstall="' + attr(a.app_id) +
                    '" title="Remove this app">Uninstall</button>';
                return '<button class="cseries-btn" data-app-open="' + attr(a.app_id) + '">Open</button>' +
                    uninstall;
        }
    }

    // ── Install / update ────────────────────────────────
    async function installApp(appId, version) {
        if (state.busy[appId]) return;
        state.busy[appId] = true;
        setFooter('Installing ' + appId + '…');
        render();
        try {
            await fetchJSON('/api/applications/install', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ app_id: appId, version: version || null }),
            });
            setFooter(appId + ' installed.');
        } catch (err) {
            setFooter('Failed: ' + (err.message || err));
        } finally {
            state.busy[appId] = false;
            await loadCatalog();
            repaintDetailIfOpen(appId);
            setTimeout(function () { setFooter(''); }, 4000);
        }
    }

    // ── Purchase + install ───────────────────────────────
    // Paid apps are REAL-MONEY purchases: /store/checkout mints a Stripe
    // Checkout Session on the publisher's connected account; we open it
    // in a new tab and poll /store/entitlement until the platform's
    // webhook fulfillment lands the entitlement, then install. Free apps
    // and publisher self-purchases still go through the free path on
    // /store/purchase. The central gate and the per-entity gate
    // both refuse installs without an entitlement, so the
    // entitlement MUST land before install fires.

    async function freePathPurchase(appId) {
        var r = await fetch('/api/applications/store/purchase', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ app_id: appId }),
        });
        if (!r.ok) {
            var text = await r.text();
            setFooter('Purchase failed: ' + text);
            return false;
        }
        return true;
    }

    async function installAfterPurchase(appId, version) {
        await fetchJSON('/api/applications/install', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ app_id: appId, version: version || null }),
        });
        setFooter(appId + ' purchased + installed.');
    }

    async function pollEntitlement(appId, attempts) {
        // ~3s cadence; checkout fulfillment is webhook-driven so it
        // usually lands within seconds of the payment completing.
        for (var i = 0; i < attempts; i++) {
            await new Promise(function (res) { setTimeout(res, 3000); });
            try {
                var d = await fetchJSON('/api/applications/store/entitlement?app_id=' +
                                        encodeURIComponent(appId));
                if (d && d.entitled) return true;
            } catch (e) { /* transient — keep polling */ }
        }
        return false;
    }

    async function buyApp(appId, version, priceMicros) {
        if (state.busy[appId]) return;
        state.busy[appId] = true;
        setFooter('Purchasing ' + appId + ' for ' + formatPrice(priceMicros) + '…');
        render();
        try {
            var r = await fetch('/api/applications/store/checkout', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    app_id: appId,
                    success_url: location.origin + '/?store_checkout=success&app_id=' +
                        encodeURIComponent(appId),
                    cancel_url: location.origin + '/?store_checkout=cancel&app_id=' +
                        encodeURIComponent(appId),
                }),
            });
            var info = {};
            try { info = await r.json(); } catch (e) { info = {}; }
            if (r.status === 503 && info.checkout_unavailable) {
                setFooter(info.reason === 'publisher_not_connected'
                    ? 'Purchase unavailable — this publisher has not connected payouts yet.'
                    : 'Purchase unavailable — payments are not set up on this Host.');
                return;
            }
            if (!r.ok) {
                setFooter('Purchase failed: ' + (info.detail || ('HTTP ' + r.status)));
                return;
            }
            if (info.already_owned) {
                await installAfterPurchase(appId, version);
                return;
            }
            if (info.checkout_required === false) {
                // Free app or publisher self-purchase — free path records it.
                if (!(await freePathPurchase(appId))) return;
                await installAfterPurchase(appId, version);
                return;
            }
            if (!info.checkout_url) {
                setFooter('Purchase failed: no checkout URL.');
                return;
            }
            // Opened only as a web address: window.open would run a
            // `javascript:` URL.
            if (!/^https?:\/\//i.test(String(info.checkout_url))) {
                setFooter('Purchase failed: the checkout address is not a web address.');
                return;
            }
            window.open(info.checkout_url, '_blank', 'noopener');
            setFooter('Complete the payment in the new tab — this page will ' +
                      'pick it up automatically…');
            var owned = await pollEntitlement(appId, 100); // ~5 min
            if (!owned) {
                setFooter('Payment not confirmed yet — finish checkout in the ' +
                          'other tab, then refresh the store.');
                return;
            }
            await installAfterPurchase(appId, version);
        } catch (err) {
            setFooter('Failed: ' + (err.message || err));
        } finally {
            state.busy[appId] = false;
            await loadCatalog();
            repaintDetailIfOpen(appId);
            setTimeout(function () { setFooter(''); }, 5000);
        }
    }

    // ── Open an installed app ───────────────────────────
    // Installed apps run as first-class windows. The dashboard's
    // AppRunner lives on the parent window; from inside this iframe we
    // hop to the parent to launch (same pattern as the workbench).
    function openApp(appId) {
        try {
            if (window.parent && window.parent.AppRunner) {
                window.parent.AppRunner.launch(appId);
                closeDetailModal();
                return;
            }
        } catch (e) { /* cross-origin or unavailable; fall through */ }
        setFooter('Could not open ' + appId + ' — launch the app from the desktop.');
    }

    // ── Uninstall ───────────────────────────────────────
    // Two-step inline confirm replaces a blocking native confirm() (banned
    // inside sandboxed app iframes). First click arms the button for 5s;
    // a second click within the window actually removes the app.
    var uninstallArmed = {};
    function armUninstall(btn, appId) {
        if (state.busy[appId]) return;
        if (uninstallArmed[appId]) {
            if (uninstallArmed[appId].timer) clearTimeout(uninstallArmed[appId].timer);
            delete uninstallArmed[appId];
            uninstallApp(appId);
            return;
        }
        var original = btn.textContent;
        btn.textContent = 'Confirm remove';
        btn.classList.add('danger');
        setFooter('Remove ' + appId + '? Workspace + settings are kept. Click again to confirm.');
        uninstallArmed[appId] = {
            timer: setTimeout(function () {
                delete uninstallArmed[appId];
                // The list may have re-rendered; only reset if this node survives.
                if (btn.isConnected) { btn.textContent = original; btn.classList.remove('danger'); }
                setFooter('');
            }, 5000),
        };
    }
    async function uninstallApp(appId) {
        if (state.busy[appId]) return;
        state.busy[appId] = true;
        setFooter('Removing ' + appId + '…');
        render();
        repaintDetailIfOpen(appId);
        try {
            await fetchJSON('/api/applications/' + encodeURIComponent(appId), {
                method: 'DELETE',
            });
            setFooter(appId + ' removed.');
        } catch (err) {
            setFooter('Failed: ' + (err.message || err));
        } finally {
            state.busy[appId] = false;
            await loadCatalog();
            repaintDetailIfOpen(appId);
            setTimeout(function () { setFooter(''); }, 4000);
        }
    }

    // ── Detail modal ────────────────────────────────────
    function renderDetailBody(a) {
        var versionLine = a.installed_version
            ? (a.installed_version === a.latest_version
                ? esc(a.installed_version)
                : esc(a.installed_version) + ' → ' + esc(a.latest_version))
            : esc(a.latest_version || 'unknown');
        // Rich catalog fields already carried by appstore.list — surface
        // size + publish date when present.
        var sizeRow = '';
        if (typeof a.size_bytes === 'number' && a.size_bytes > 0) {
            sizeRow =
                '<div class="detail-row">' +
                    '<div class="detail-row-label label-sys">Size</div>' +
                    '<div class="detail-row-value">' + esc(fmtBytes(a.size_bytes)) + '</div>' +
                '</div>';
        }
        var publishedRow = '';
        if (a.published_at) {
            publishedRow =
                '<div class="detail-row">' +
                    '<div class="detail-row-label label-sys">Published</div>' +
                    '<div class="detail-row-value">' + esc(formatDate(a.published_at)) + '</div>' +
                '</div>';
        }
        return '' +
            '<div class="detail-hero">' +
                '<img class="detail-icon" src="' + attr(iconUrl(a)) + '" alt="" onerror="this.style.visibility=\'hidden\'">' +
                '<div class="detail-text">' +
                    '<div class="detail-name" id="detail-title">' + esc(a.name || a.app_id) + '</div>' +
                    '<div class="detail-meta label-sys">' +
                        developerLine(a) + ' · ' + versionLine +
                    '</div>' +
                '</div>' +
                '<div class="detail-action">' + actionButton(a, true) + '</div>' +
            '</div>' +
            '<div class="detail-summary">' + esc(a.summary || '') + '</div>' +
            '<div class="detail-row">' +
                '<div class="detail-row-label label-sys">App ID</div>' +
                '<div class="detail-row-value"><code>' + esc(a.app_id) + '</code></div>' +
            '</div>' +
            '<div class="detail-row">' +
                '<div class="detail-row-label label-sys">Status</div>' +
                '<div class="detail-row-value">' + renderStatusBadge(a) + '</div>' +
            '</div>' +
            renderRuntimeRows(a) +
            renderAccessRows(a) +
            renderDataOfferRows(a) +
            renderListingRows(a) +
            sizeRow +
            publishedRow;
    }

    // ── "Bring my data from <site>" ──────────────────────────────────
    //
    // Somebody used this app as a free public website, liked it, and is now
    // running it on their own machine. The rows they made over there are
    // theirs, and this is where they get them.
    //
    // ONLY EVER SHOWN WHEN THERE IS SOMETHING TO SHOW. The offers come from
    // the kernel, which asks the platform whether this account already has
    // rows on a site running this app. No offers -> no row at all, rather than
    // an empty "no data found" panel: the common case by far is an app with no
    // public site, and a permanent empty box in every detail modal would be
    // noise on every install anyone ever does.
    function renderDataOfferRows(a) {
        var d = a._dataOffers;
        if (!d) return '';
        if (d.reason === 'owner_subject_unresolvable') {
            // Not an error, and deliberately not phrased as one. The account
            // has simply not signed in since the platform started recording
            // the identifier this is matched on, and signing in once fixes it.
            return '<div class="detail-row">' +
                       '<div class="detail-row-label label-sys">Your data</div>' +
                       '<div class="detail-row-value label-sys">' +
                           'Sign in again to check whether you have data on a ' +
                           'public site running this app.' +
                       '</div>' +
                   '</div>';
        }
        var offers = d.offers || [];
        if (!offers.length) return '';
        var rows = offers.map(function (o) {
            var note = state.dataMoveNote[o.site_id] || '';
            var done = o.import_status === 'imported';
            var btn = done
                ? '<span class="label-sys">Brought over</span>'
                : '<button type="button" class="cseries-btn" ' +
                      'data-data-import="' + attr(a.app_id) + '" ' +
                      'data-site-id="' + attr(String(o.site_id)) + '" ' +
                      'data-member-email="' + attr(o.member_email || '') + '"' +
                      (state.dataMoveBusy[o.site_id] ? ' disabled' : '') + '>' +
                      (state.dataMoveBusy[o.site_id] ? 'Working…' : 'Bring it over') +
                  '</button>';
            return '<div class="detail-offer">' +
                       '<span>' + esc(o.hostname || o.slug || ('site ' + o.site_id)) +
                       '</span> ' + btn +
                       (note ? '<div class="label-sys">' + esc(note) + '</div>' : '') +
                   '</div>';
        }).join('');
        return '<div class="detail-row">' +
                   '<div class="detail-row-label label-sys">Your data</div>' +
                   '<div class="detail-row-value">' + rows +
                       '<div class="label-sys">Your rows are copied, never moved — ' +
                       'they stay on the site exactly as they are, so changing ' +
                       'your mind costs nothing.</div>' +
                   '</div>' +
               '</div>';
    }

    async function loadDataOffers(a) {
        if (a._dataOffersLoading) return;
        a._dataOffersLoading = true;
        try {
            a._dataOffers = await fetchJSON(
                '/api/applications/sites/data/offers?app_id=' +
                encodeURIComponent(a.app_id));
        } catch (e) {
            // Silent, and that is the right failure here. This is an extra the
            // user may not know exists; a red box about a lookup they never
            // asked for would be worse than the row simply not appearing.
            a._dataOffers = null;
        } finally {
            a._dataOffersLoading = false;
            repaintDetailIfOpen(a.app_id);
        }
    }

    // One click = ask for the move, then land it once the export has arrived.
    // The export is asynchronous (the platform's public-zone agent picks the
    // request up on its own poll), so the first answer is normally "queued"
    // and this re-asks rather than making the user click again. Both halves
    // are idempotent — the move is deduplicated by the platform, the rows by
    // their primary key — so re-asking cannot duplicate anything.
    async function bringDataOver(appId, siteId, memberEmail) {
        if (state.dataMoveBusy[siteId]) return;
        state.dataMoveBusy[siteId] = true;
        state.dataMoveNote[siteId] = '';
        repaintDetailIfOpen(appId);
        var landed = false;
        try {
            for (var attempt = 0; attempt < 12 && !landed; attempt++) {
                var r = await fetchJSON('/api/applications/sites/data/import', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        app_id: appId, site_id: parseInt(siteId, 10),
                        member_email: memberEmail,
                    }),
                });
                if (r.state === 'done') {
                    state.dataMoveNote[siteId] =
                        'Brought over ' + (r.rows_imported || 0) + ' row(s).';
                    landed = true;
                    break;
                }
                if (r.state === 'refused') {
                    state.dataMoveNote[siteId] = r.detail || 'Refused.';
                    landed = true;
                    break;
                }
                state.dataMoveNote[siteId] = 'Getting your rows from the site…';
                repaintDetailIfOpen(appId);
                await new Promise(function (res) { setTimeout(res, 5000); });
            }
            if (!landed) {
                state.dataMoveNote[siteId] =
                    'Still exporting. Re-open this app shortly to finish.';
            }
        } catch (err) {
            // The kernel passes the platform's own refusal sentence through,
            // so this shows the real reason rather than a guess about it.
            state.dataMoveNote[siteId] =
                'Could not bring it over: ' + (err.message || err);
        } finally {
            state.dataMoveBusy[siteId] = false;
            var app = state.apps.find(function (x) { return x.app_id === appId; });
            if (app) {
                app._dataOffers = undefined;
                app._dataOffersLoading = false;
                loadDataOffers(app);
            }
            repaintDetailIfOpen(appId);
        }
    }

    // Where the app runs + what it costs, in the same consent surface as the
    // egress + permission rows. Catalog fields cover installed apps; for a
    // not-installed app the lazily-fetched /store/preview (the app's own
    // signed manifest) fills runtime_kind/baseline_memory_mi in. Nothing
    // known yet -> no row, not a placeholder number.
    function renderRuntimeRows(a) {
        var kind = a.runtime_kind;
        var mi = a.baseline_memory_mi;
        var p = a._preview;
        if ((kind == null) && p && !p._error) {
            kind = p.runtime_kind;
            if (mi == null) mi = p.baseline_memory_mi;
        }
        var where;
        if (kind === 'external') {
            where = 'In its own container, on demand — it starts when you open the app and ' +
                    'sleeps when you close it.';
        } else if (kind === 'process') {
            where = 'As a background process on this machine while the app is active.';
        } else if (kind === 'static') {
            where = 'In a plain window — no background process, no memory cost while closed.';
        } else {
            return '';
        }
        var costVal;
        if (mi != null && mi > 0) {
            costVal = 'Up to ' + fmtMi(mi) + ' of this machine’s memory while it runs.';
        } else if (kind === 'static') {
            costVal = 'None while closed; a window’s worth while open.';
        } else {
            costVal = 'No memory figure declared for this app.';
        }
        var live = residentRss(a.app_id);
        if (live != null) {
            costVal += ' Using ' + fmtBytes(live) + ' right now.';
        }
        return '<div class="detail-row">' +
                   '<div class="detail-row-label label-sys">Where it runs</div>' +
                   '<div class="detail-row-value detail-row-prose">' + esc(where) + '</div>' +
               '</div>' +
               '<div class="detail-row">' +
                   '<div class="detail-row-label label-sys">Memory</div>' +
                   '<div class="detail-row-value detail-row-prose">' + esc(costVal) + '</div>' +
               '</div>' +
               machineContextRow();
    }

    // The machine's current numbers beside the app's declared need, so the
    // user can judge the fit. Shown only when the self-read gave a complete
    // picture; a partial figure is left out rather than mislabeled.
    function machineContextRow() {
        var sections = machineSections();
        if (!sections.length || !sections.every(function (s) { return s.limit != null; })) return '';
        var used = 0, limit = 0;
        sections.forEach(function (s) { used += s.used; limit += s.limit; });
        return '<div class="detail-row">' +
                   '<div class="detail-row-label label-sys">Your machine</div>' +
                   '<div class="detail-row-value detail-row-prose">' +
                       esc(fmtBytes(used) + ' of ' + fmtBytes(limit) + ' memory in use right now.') +
                   '</div>' +
               '</div>';
    }

    // Install-consent surface: the declared egress hosts + permission scopes an
    // app requests, read from its signed manifest via /store/preview. The user
    // sees exactly where the app may phone home and what it can touch before the
    // "Install & allow access" button installs it. `a._preview` is filled lazily
    // by loadPreview() and the modal repaints when it arrives.
    function renderAccessRows(a) {
        if (a.local_only) return ''; // no store bundle to preview; ships in the image
        var p = a._preview;
        if (!p && p !== null) {
            return '<div class="detail-row">' +
                       '<div class="detail-row-label label-sys">Access</div>' +
                       '<div class="detail-row-value label-sys">Checking…</div>' +
                   '</div>';
        }
        p = p || { permissions: [], network: [] };
        var netVal;
        if (p.network && p.network.length) {
            netVal = p.network.map(function (h) {
                return '<span class="badge badge-warn">' + esc(h) + '</span>';
            }).join(' ');
        } else {
            netVal = '<span class="label-sys">Only its own data · no external network</span>';
        }
        var permVal;
        if (p.permissions && p.permissions.length) {
            permVal = p.permissions.map(function (s) {
                return '<span class="badge badge-muted"><code>' + esc(s) + '</code></span>';
            }).join(' ');
        } else {
            permVal = '<span class="label-sys">None requested</span>';
        }
        var consent = (a.install_status === 'not_installed' || a.install_status === 'update_available')
            ? '<div class="detail-row"><div class="detail-row-value label-sys">' +
              'Installing grants the access listed above. The app is sandboxed and ' +
              'can reach only these hosts.</div></div>'
            : '';
        return '<div class="detail-row">' +
                   '<div class="detail-row-label label-sys">Network access</div>' +
                   '<div class="detail-row-value">' + netVal + '</div>' +
               '</div>' +
               '<div class="detail-row">' +
                   '<div class="detail-row-label label-sys">Permissions</div>' +
                   '<div class="detail-row-value">' + permVal + '</div>' +
               '</div>' +
               consent;
    }

    // Lazily fetch the declared access for the app whose modal is open, then
    // repaint. Cached on the app object so re-opening is instant.
    async function loadPreview(a) {
        if (a._previewLoading) return;
        a._previewLoading = true;
        try {
            var url = '/api/applications/store/preview/' + encodeURIComponent(a.app_id) +
                (a.latest_version ? ('?version=' + encodeURIComponent(a.latest_version)) : '');
            a._preview = await fetchJSON(url);
        } catch (e) {
            a._preview = { permissions: [], network: [], _error: true };
        } finally {
            a._previewLoading = false;
            repaintDetailIfOpen(a.app_id);
        }
    }
    function openDetailModal(appId) {
        var a = state.apps.find(function (x) { return x.app_id === appId; });
        if (!a) return;
        state.modalAppId = appId;
        // Remember what had focus so we can restore it on close (a11y).
        state.modalReturnFocus = document.activeElement;
        document.getElementById('detail-body').innerHTML = renderDetailBody(a);
        var modal = document.getElementById('detail-modal');
        modal.hidden = false;
        var closeBtn = modal.querySelector('.appstore-modal-close');
        if (closeBtn) closeBtn.focus();
        // Fetch the declared egress + permissions + runtime cost for the
        // consent surface (once). Image-baked local apps have no store bundle.
        if (a._preview === undefined && !a.local_only) loadPreview(a);
        // Only for an app that is actually here: the import writes into the
        // INSTALLED app's own tables, so there is nothing to offer for one
        // that has not been installed yet. The row appears after installing.
        // Same three statuses installedApps() treats as present — checking
        // only 'installed' would hide the offer from every app that is up to
        // date, which is most of them.
        if (a._dataOffers === undefined &&
                (a.install_status === 'installed' ||
                 a.install_status === 'up_to_date' ||
                 a.install_status === 'update_available')) {
            loadDataOffers(a);
        }
    }
    // Repaint the open modal from fresh catalog state. Closes it if the
    // app it was showing is gone from the catalog. Called after mutations.
    function repaintDetailIfOpen(appId) {
        if (!state.modalAppId) return;
        if (appId && appId !== state.modalAppId) return;
        var modal = document.getElementById('detail-modal');
        if (!modal || modal.hidden) return;
        var a = state.apps.find(function (x) { return x.app_id === state.modalAppId; });
        if (!a) { closeDetailModal(); return; }
        document.getElementById('detail-body').innerHTML = renderDetailBody(a);
    }
    function closeDetailModal() {
        document.getElementById('detail-modal').hidden = true;
        state.modalAppId = null;
        // Return focus to whatever launched the modal (a11y).
        if (state.modalReturnFocus && typeof state.modalReturnFocus.focus === 'function') {
            try { state.modalReturnFocus.focus(); } catch (e) { /* detached node */ }
        }
        state.modalReturnFocus = null;
    }

    // Keep Tab focus inside the open modal (simple focus trap).
    function trapModalFocus(e) {
        if (e.key !== 'Tab') return;
        var modal = document.getElementById('detail-modal');
        if (!modal || modal.hidden) return;
        var focusable = modal.querySelectorAll(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (!focusable.length) return;
        var first = focusable[0];
        var last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first.focus();
        }
    }

    // ── Util ────────────────────────────────────────────
    async function fetchJSON(url, opts) {
        var r = await fetch(url, opts || {});
        if (!r.ok) {
            var body = await r.text().catch(function () { return ''; });
            throw new Error(r.status + ' ' + body.slice(0, 100));
        }
        return r.json();
    }
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
    function fmtBytes(n) {
        if (!n || n <= 0) return '0 B';
        var units = ['B', 'KB', 'MB', 'GB'];
        var i = 0;
        var v = n;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return (i === 0 ? v : v.toFixed(1)) + ' ' + units[i];
    }
    // MiB figure the way a person reads it: 1024 -> "1 GB", 512 -> "512 MB".
    function fmtMi(mi) {
        if (mi >= 1024 && mi % 256 === 0) {
            var g = mi / 1024;
            return (g % 1 === 0 ? g : g.toFixed(1)) + ' GB';
        }
        return mi + ' MB';
    }
    function formatDate(iso) {
        var d = new Date(iso);
        if (isNaN(d.getTime())) return String(iso);
        return d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' });
    }
})();
