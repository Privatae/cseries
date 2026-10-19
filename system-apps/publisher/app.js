/* ================================================================
 * Publisher — manage apps you publish, set prices, watch the flywheel.
 *
 * Talks to the entity-kernel proxy routes (server/routes/applications.py
 * publisher_* handlers), which forward caller-scoped to the bridge:
 *   GET  /api/applications/publisher/my-apps     → appstore.publisher.my_apps
 *   GET  /api/applications/publisher/sales       → appstore.publisher.sales
 *   POST /api/applications/publisher/set-price   → appstore.publisher.set_price
 *   POST /api/applications/publisher/publish     → appstore.publish_bundle
 *   POST /api/applications/publisher/yank        → appstore.yank_version
 *   GET  /api/applications/publisher/agreement   → appstore.agreement
 *   GET  /api/applications/publisher/requests    → appstore.publish_requests
 *
 * Publishing is a REVIEW flow (2026-08-27): submit holds the bundle for
 * human review; the requests list is where the decision lands. The
 * agreement must be shown and explicitly accepted before submit.
 *
 * Entity-id is fixed by the bridge mTLS handshake — this app never sends it.
 * ================================================================ */

(function () {
    'use strict';

    var state = {
        view: 'overview',
        apps: [],
        sales: [],
        salesAppFilter: '',
        salesLimit: 50,
        editingAppId: null,
        yank: null,
        loaded: false,
        appsError: null,
        salesError: null,
        publish: {
            file: null,
            manifest: null,
            submitting: false,
            agreementVersion: null,
            accepted: false,
            requests: [],
        },
    };

    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('.cseries-nav-btn').forEach(function (b) {
            b.addEventListener('click', function () {
                setView(b.getAttribute('data-view'));
            });
        });

        document.querySelectorAll('[data-modal-close]').forEach(function (el) {
            el.addEventListener('click', function () {
                var m = el.closest('.pub-modal');
                if (m && m.id === 'yank-modal') closeYankModal();
                else closePriceModal();
            });
        });

        document.getElementById('price-save').addEventListener('click', savePrice);

        document.addEventListener('click', function (e) {
            var btn = e.target.closest && e.target.closest('[data-edit-price]');
            if (btn) {
                e.stopPropagation();
                openPriceModal(btn.getAttribute('data-edit-price'));
            }
        });

        document.getElementById('sales-app-filter').addEventListener('change', function (e) {
            state.salesAppFilter = e.target.value;
            loadSales();
        });

        // Publish tab wiring
        document.getElementById('publish-file').addEventListener('change', onPublishFileChosen);
        document.getElementById('publish-submit').addEventListener('click', submitPublish);
        document.getElementById('publish-agree').addEventListener('change', function (e) {
            state.publish.accepted = !!e.target.checked;
            updatePublishSubmit();
        });
        document.getElementById('publish-audience').addEventListener('change', onAudienceChange);
        loadAgreement();
        loadOwnedGroups();
        loadPublishRequests();

        // Yank action delegated on the My Apps grid
        document.addEventListener('click', function (e) {
            var yb = e.target.closest && e.target.closest('[data-yank-app]');
            if (yb) {
                e.stopPropagation();
                openYankModal(
                    yb.getAttribute('data-yank-app'),
                    yb.getAttribute('data-yank-version')
                );
            }
        });

        // Retry on a per-section load error, and "show more" sales.
        document.addEventListener('click', function (e) {
            if (!e.target.closest) return;
            var retry = e.target.closest('[data-retry]');
            if (retry) {
                e.stopPropagation();
                var which = retry.getAttribute('data-retry');
                if (which === 'apps') { loadApps().then(renderAll); }
                else if (which === 'sales') { loadSales(); }
                return;
            }
            var more = e.target.closest('[data-sales-more]');
            if (more) {
                e.stopPropagation();
                state.salesLimit = Math.min(500, state.salesLimit + 50);
                loadSales();
            }
        });

        // Yank modal wiring
        document.getElementById('yank-confirm').addEventListener('click', confirmYank);

        // Escape closes whichever modal is open; Tab is trapped inside it.
        document.addEventListener('keydown', function (e) {
            var open = openModalEl();
            if (!open) return;
            if (e.key === 'Escape') {
                if (open.id === 'yank-modal') closeYankModal(); else closePriceModal();
                return;
            }
            if (e.key === 'Tab') trapTab(e, open);
        });

        loadAll();
        setInterval(loadAll, 60000);
        tickClock();
        setInterval(tickClock, 30000);
    });

    function tickClock() {
        var el = document.getElementById('footer-ts');
        if (el) el.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function setView(v) {
        state.view = v;
        document.querySelectorAll('.cseries-nav-btn').forEach(function (b) {
            var sel = b.getAttribute('data-view') === v;
            b.classList.toggle('active', sel);
            b.setAttribute('aria-selected', sel ? 'true' : 'false');
        });
        document.querySelectorAll('.view').forEach(function (s) {
            s.hidden = s.getAttribute('data-view') !== v;
        });
    }

    // ── Data ────────────────────────────────────────────
    async function loadAll() {
        await Promise.all([loadApps(), loadSales()]);
        state.loaded = true;
        renderAll();
    }

    async function loadApps() {
        try {
            var data = await fetchJSON('/api/applications/publisher/my-apps');
            state.apps = (data && data.apps) || [];
            state.appsError = null;
        } catch (err) {
            state.appsError = err.message || String(err);
            setFooter('apps load failed: ' + state.appsError);
        }
    }

    async function loadSales() {
        try {
            var url = '/api/applications/publisher/sales?limit=' + state.salesLimit;
            if (state.salesAppFilter) {
                url += '&app_id=' + encodeURIComponent(state.salesAppFilter);
            }
            var data = await fetchJSON(url);
            state.sales = (data && data.sales) || [];
            state.salesError = null;
            renderSales();
            renderOverview();
        } catch (err) {
            state.salesError = err.message || String(err);
            setFooter('sales load failed: ' + state.salesError);
            renderSales();
            renderOverview();
        }
    }

    function renderAll() {
        renderOverview();
        renderApps();
        renderSales();
        renderAppFilter();
    }

    // ── Overview ────────────────────────────────────────
    function renderOverview() {
        var totalEarned = state.apps.reduce(function (s, a) {
            return s + (a.lifetime_earned_micros || 0);
        }, 0);
        var totalSales = state.apps.reduce(function (s, a) {
            return s + (a.sale_count || 0);
        }, 0);
        document.getElementById('kpi-earned').textContent = formatPrice(totalEarned);
        document.getElementById('kpi-sales').textContent = String(totalSales);
        document.getElementById('kpi-apps').textContent = String(state.apps.length);

        var sub = document.getElementById('header-sub');
        if (sub) {
            sub.textContent = state.apps.length === 0
                ? 'No apps published yet'
                : state.apps.length + (state.apps.length === 1 ? ' app' : ' apps') +
                  ' \u00b7 ' + formatPrice(totalEarned) + ' lifetime';
        }

        var recent = document.getElementById('recent-list');
        var meta = document.getElementById('recent-meta');
        if (state.salesError) {
            recent.innerHTML = errorCard('Couldn’t load sales', state.salesError, 'sales');
            if (meta) meta.textContent = '—';
            return;
        }
        var slice = state.sales.slice(0, 5);
        if (slice.length === 0) {
            recent.innerHTML = '<div class="cseries-empty">' +
                '<span class="empty-title">No sales yet</span>' +
                '<span class="empty-sub">Publish a paid app or wait for your first buyer.</span>' +
                '</div>';
            if (meta) meta.textContent = '0 sales';
            return;
        }
        if (meta) meta.textContent = 'last ' + state.sales.length;
        recent.innerHTML = slice.map(renderSaleRow).join('');
    }

    function renderSaleRow(s) {
        var when = new Date(s.created_at);
        return '<div class="recent-row">' +
            '<div class="recent-row-app">' + esc(s.app_id || '—') + '</div>' +
            '<div class="recent-row-amt">+' + formatPrice(s.amount_micros) + '</div>' +
            '<div class="recent-row-when label-sys">' + esc(when.toLocaleString()) + '</div>' +
        '</div>';
    }

    // ── My Apps ────────────────────────────────────────
    function renderApps() {
        var list = document.getElementById('apps-list');
        if (!list) return;
        if (state.appsError) {
            list.innerHTML = errorCard('Couldn’t load your apps', state.appsError, 'apps');
            return;
        }
        if (state.apps.length === 0) {
            list.innerHTML = '<div class="cseries-empty">' +
                '<span class="empty-title">You haven\u2019t published anything yet</span>' +
                '<span class="empty-sub">' +
                'Build an app in the Workbench, then publish the finished ' +
                'project — from the Workbench, by asking your host, or by ' +
                'dropping its zip on the Publish tab here.' +
                '</span>' +
                '</div>';
            return;
        }
        list.innerHTML = state.apps.map(renderAppRow).join('');
    }

    function renderAppRow(a) {
        var priceLabel = (a.price_micros || 0) > 0
            ? formatPrice(a.price_micros)
            : 'Free';
        var publisherShare = (a.price_micros || 0) > 0
            ? formatPrice(Math.floor(a.price_micros * (10000 - (a.platform_fee_bps || 0)) / 10000))
            : '—';
        var audience = (a.audience || '').toLowerCase();
        // Three values now, so the class is LOOKED UP rather than derived from
        // a two-way ternary: the old `public ? is-public : is-internal` silently
        // painted a circle app as internal, which is the one thing a publisher
        // must not be misinformed about.
        var audienceClass = {public: 'is-public', internal: 'is-internal',
                             circle: 'is-circle'}[audience] || 'is-internal';
        var audienceBadge = audience
            ? '<span class="app-row-audience ' + audienceClass + '">' + esc(audience) + '</span>'
            : '';
        var metaBits = [];
        if (a.latest_published_at) metaBits.push('Published ' + esc(fmtDate(a.latest_published_at)));
        if (a.latest_size_bytes) metaBits.push(fmtBytes(a.latest_size_bytes));
        var metaLine = metaBits.length
            ? '<div class="app-row-meta label-sys">' + metaBits.join(' · ') + '</div>'
            : '';
        return '<div class="app-row cseries-card">' +
            '<div class="app-row-head">' +
                '<div class="app-row-name">' + esc(a.name || a.app_id) + audienceBadge + '</div>' +
                '<div class="app-row-id label-sys">' + esc(a.app_id) + '</div>' +
            '</div>' +
            metaLine +
            '<div class="app-row-grid">' +
                '<div class="app-row-cell">' +
                    '<div class="label-sys">Price</div>' +
                    '<div class="app-row-cell-value">' + esc(priceLabel) + '</div>' +
                '</div>' +
                '<div class="app-row-cell">' +
                    '<div class="label-sys">You earn / sale</div>' +
                    '<div class="app-row-cell-value">' + esc(publisherShare) + '</div>' +
                '</div>' +
                '<div class="app-row-cell">' +
                    '<div class="label-sys">Sales</div>' +
                    '<div class="app-row-cell-value">' + (a.sale_count || 0) + '</div>' +
                '</div>' +
                '<div class="app-row-cell">' +
                    '<div class="label-sys">Lifetime</div>' +
                    '<div class="app-row-cell-value">' + formatPrice(a.lifetime_earned_micros) + '</div>' +
                '</div>' +
                '<div class="app-row-cell">' +
                    '<div class="label-sys">Latest version</div>' +
                    '<div class="app-row-cell-value">' + esc(a.latest_version || '—') + '</div>' +
                '</div>' +
                '<div class="app-row-cell">' +
                    '<div class="label-sys">Platform fee</div>' +
                    '<div class="app-row-cell-value">' +
                        ((a.platform_fee_bps || 0) / 100).toFixed(1) + '%' +
                    '</div>' +
                '</div>' +
            '</div>' +
            '<div class="app-row-actions">' +
                '<button class="cseries-btn" data-edit-price="' + attr(a.app_id) + '">' +
                    'Set price' +
                '</button>' +
                (a.latest_version
                    ? '<button class="cseries-btn danger" ' +
                        'data-yank-app="' + attr(a.app_id) + '" ' +
                        'data-yank-version="' + attr(a.latest_version) + '">' +
                        'Yank v' + esc(a.latest_version) +
                      '</button>'
                    : '') +
            '</div>' +
        '</div>';
    }

    // ── Sales ───────────────────────────────────────────
    function renderAppFilter() {
        var sel = document.getElementById('sales-app-filter');
        if (!sel) return;
        var current = sel.value;
        sel.innerHTML = '<option value="">All apps</option>' +
            state.apps.map(function (a) {
                return '<option value="' + attr(a.app_id) + '">' +
                    esc(a.name || a.app_id) + '</option>';
            }).join('');
        sel.value = current;
    }

    function renderSales() {
        var box = document.getElementById('sales-table');
        if (!box) return;
        if (state.salesError) {
            box.innerHTML = errorCard('Couldn’t load sales', state.salesError, 'sales');
            return;
        }
        if (state.sales.length === 0) {
            box.innerHTML = '<div class="cseries-empty">' +
                '<span class="empty-title">No sales in this window</span>' +
                '</div>';
            return;
        }
        var header =
            '<div class="sales-row sales-row-head label-sys">' +
                '<div>When</div>' +
                '<div>App</div>' +
                '<div>Buyer</div>' +
                '<div>Gross</div>' +
                '<div>Fee</div>' +
                '<div>You earned</div>' +
            '</div>';
        var rows = state.sales.map(function (s) {
            var gross = s.gross_price_micros || 0;
            var feeBps = s.platform_fee_bps || 0;
            var fee = Math.floor(gross * feeBps / 10000);
            return '<div class="sales-row">' +
                '<div class="label-sys">' + esc(new Date(s.created_at).toLocaleString()) + '</div>' +
                '<div>' + esc(s.app_id || '—') + '</div>' +
                '<div class="label-sys" data-buyer="' + esc(String(s.buyer_entity_id || '')) + '">Host #' +
                    esc(String(s.buyer_entity_id || '?')) + '</div>' +
                '<div>' + formatPrice(gross) + '</div>' +
                '<div class="sales-row-fee">' + (feeBps / 100).toFixed(1) + '% (' + formatPrice(fee) + ')</div>' +
                '<div class="sales-row-net">+' + formatPrice(s.amount_micros) + '</div>' +
            '</div>';
        });
        // The backend caps a single window at 500 and defaults to 50, so this
        // is the last N — never a lifetime total. Offer "show more" while the
        // window is full and we're under the cap.
        var windowFull = state.sales.length >= state.salesLimit;
        var footer = '<div class="sales-foot label-sys">' +
            '<span>Showing last ' + state.sales.length + '</span>' +
            (windowFull && state.salesLimit < 500
                ? '<button class="cseries-btn" data-sales-more type="button">Show more</button>'
                : '') +
            '</div>';
        box.innerHTML = header + rows.join('') + footer;
        nameBuyers(box);
    }

    // ── Set-price modal ─────────────────────────────────
    function openPriceModal(appId) {
        var a = state.apps.find(function (x) { return x.app_id === appId; });
        if (!a) return;
        state.editingAppId = appId;
        document.getElementById('price-modal-title').textContent =
            'Set price · ' + (a.name || appId);
        var input = document.getElementById('price-input');
        input.value = ((a.price_micros || 0) / 1e6).toFixed(2);
        // Platform fee is platform-owned (standard 10%) — shown, never edited.
        document.getElementById('price-hint').textContent =
            '0 = free. Platform fee (' + ((a.platform_fee_bps || 0) / 100).toFixed(1) +
            '%) is standard and set by the platform.';
        document.getElementById('price-modal').hidden = false;
        setTimeout(function () { input.focus(); input.select(); }, 30);
    }

    function closePriceModal() {
        document.getElementById('price-modal').hidden = true;
        state.editingAppId = null;
    }

    async function savePrice() {
        var appId = state.editingAppId;
        if (!appId) return;
        var raw = document.getElementById('price-input').value;
        var usd = parseFloat(raw);
        if (!isFinite(usd) || usd < 0) {
            setFooter('Price must be a non-negative number.');
            return;
        }
        var micros = Math.round(usd * 1e6);
        // platform_fee_bps is platform-owned — never sent from here.
        var body = { app_id: appId, price_micros: micros };
        var btn = document.getElementById('price-save');
        btn.disabled = true;
        try {
            await fetchJSON('/api/applications/publisher/set-price', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            setFooter('Price updated to ' + formatPrice(micros) + '.');
            closePriceModal();
            await loadApps();
            renderAll();
        } catch (err) {
            setFooter('Save failed: ' + (err.message || err));
        } finally {
            btn.disabled = false;
            setTimeout(function () { setFooter(''); }, 4000);
        }
    }

    // ── Util ────────────────────────────────────────────
    function formatPrice(micros) {
        var v = Number(micros || 0);
        if (v === 0) return '$0.00';
        return '$' + (v / 1e6).toFixed(2);
    }

    function setFooter(text) {
        var el = document.getElementById('footer-status');
        if (el) el.textContent = text;
    }

    // The buyer's contact card name (2026-09-23): the Host's own resolver
    // (POST /api/hosts/cards) knows buyers this Host is connected to; anyone
    // else stays "Host #N", never a guess.
    async function nameBuyers(box) {
        var ids = Array.from(box.querySelectorAll('[data-buyer]'))
            .map(function (el) { return Number(el.getAttribute('data-buyer')); })
            .filter(function (n) { return n > 0; });
        if (!ids.length) return;
        try {
            var r = await fetchJSON('/api/hosts/cards', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ids: Array.from(new Set(ids)) }),
            });
            var cards = (r && r.cards) || {};
            box.querySelectorAll('[data-buyer]').forEach(function (el) {
                var c = cards[el.getAttribute('data-buyer')];
                if (c && c.display_name && c.sources && c.sources.length) {
                    el.textContent = c.display_name;
                    el.title = [c.number, c.relationship_label, c.user ? 'User: ' + c.user : '']
                        .filter(Boolean).join(' · ');
                }
            });
        } catch (e) { /* keep "Host #N" */ }
    }

    async function fetchJSON(url, opts) {
        var r = await fetch(url, opts || {});
        if (!r.ok) {
            var body = await r.text().catch(function () { return ''; });
            throw new Error(r.status + ' ' + body.slice(0, 140));
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
        var v = Number(n || 0);
        if (v <= 0) return '—';
        if (v < 1024) return v + ' B';
        if (v < 1024 * 1024) return (v / 1024).toFixed(1) + ' KB';
        return (v / (1024 * 1024)).toFixed(1) + ' MB';
    }
    function fmtDate(iso) {
        var d = new Date(iso);
        return isNaN(d.getTime()) ? String(iso) : d.toLocaleDateString();
    }
    function openModalEl() {
        var p = document.getElementById('price-modal');
        if (p && !p.hidden) return p;
        var y = document.getElementById('yank-modal');
        if (y && !y.hidden) return y;
        return null;
    }
    function trapTab(e, modal) {
        var card = modal.querySelector('.pub-modal-card');
        if (!card) return;
        var focusable = card.querySelectorAll(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        var items = Array.prototype.filter.call(focusable, function (el) {
            return !el.disabled && el.offsetParent !== null;
        });
        if (items.length === 0) return;
        var first = items[0];
        var last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first.focus();
        }
    }
    function errorCard(title, detail, retryKey) {
        return '<div class="cseries-empty pub-error">' +
            '<span class="empty-title">' + esc(title) + '</span>' +
            '<span class="empty-sub">' + esc(detail) + '</span>' +
            '<button class="cseries-btn" type="button" data-retry="' + attr(retryKey) + '">Retry</button>' +
            '</div>';
    }

    // ── Publish tab ─────────────────────────────────────
    //
    // Client-side zip → manifest preview → POST as multipart to the
    // kernel proxy. No signing key handling here; the operator does
    // that.

    // Submit is armed only when a readable zip AND the accepted agreement
    // are both present — the acceptance is what the store records (§5a).
    function updatePublishSubmit() {
        var submit = document.getElementById('publish-submit');
        submit.disabled = !(state.publish.file && state.publish.manifest
            && state.publish.accepted
            && state.publish.agreementVersion !== null
            && !state.publish.submitting);
    }

    // The groups THIS host OWNS, for the circle-audience picker. Owned only:
    // a circle app is gated on a roster its publisher holds, and offering a
    // group we merely replicate would offer a choice the publish route refuses
    // (server/applications/appstore_circle.py record_publication).
    async function loadOwnedGroups() {
        var sel = document.getElementById('publish-group-ref');
        try {
            var r = await fetch('/api/applications/store/groups');
            var data = await r.json();
            var groups = (data && data.groups) || [];
            sel.innerHTML = groups.length
                ? groups.map(function (g) {
                    return '<option value="' + esc(g.group_id) + '">'
                        + esc(g.name || g.group_id)
                        + ' (' + (g.members || 0) + ')</option>';
                  }).join('')
                : '<option value="">No groups yet — create one in Circles</option>';
        } catch (err) {
            // Named, not silent: an empty picker that looks like "you have no
            // groups" when the call failed sends the publisher to create a
            // group they already have.
            sel.innerHTML = '<option value="">Could not load your groups</option>';
        }
    }

    function onAudienceChange(e) {
        var isCircle = (e.target.value || '') === 'circle';
        document.getElementById('publish-group-field').hidden = !isCircle;
    }

    async function loadAgreement() {
        var text = document.getElementById('publish-agreement-text');
        var box = document.getElementById('publish-agree');
        var label = document.getElementById('publish-agree-label');
        try {
            var r = await fetch('/api/applications/publisher/agreement');
            var data = await r.json();
            if (!r.ok || !data || data.agreement_version === undefined) {
                throw new Error((data && (data.detail || data.error)) || ('HTTP ' + r.status));
            }
            state.publish.agreementVersion = data.agreement_version;
            text.textContent = data.text_md || '';
            label.textContent = 'I accept the Publishing Agreement (v'
                + data.agreement_version + ')';
            box.disabled = false;
        } catch (err) {
            state.publish.agreementVersion = null;
            box.disabled = true;
            text.textContent = 'Could not load the Publishing Agreement ('
                + (err.message || err) + '). Publishing needs it — retry by '
                + 'reopening this tab.';
        }
        updatePublishSubmit();
    }

    async function loadPublishRequests() {
        var el = document.getElementById('publish-requests');
        try {
            var r = await fetch('/api/applications/publisher/requests');
            var data = await r.json();
            if (!r.ok) throw new Error((data && (data.detail || data.error)) || ('HTTP ' + r.status));
            state.publish.requests = (data && data.requests) || [];
        } catch (err) {
            el.textContent = 'Could not load requests: ' + (err.message || err);
            return;
        }
        if (!state.publish.requests.length) {
            el.textContent = 'No requests yet.';
            return;
        }
        el.innerHTML = state.publish.requests.map(function (q) {
            var line = '<strong>' + esc(q.app_id) + '</strong> v' + esc(q.version)
                + ' — ' + esc(q.status);
            if (q.status === 'rejected' && q.reason) {
                line += ' · reason: ' + esc(q.reason);
            }
            return '<div class="pub-request-row">' + line + '</div>';
        }).join('');
    }

    function onPublishFileChosen(e) {
        var f = e.target.files && e.target.files[0];
        state.publish.file = f || null;
        var status = document.getElementById('publish-file-status');
        var preview = document.getElementById('publish-manifest');
        if (!f) {
            status.textContent = 'No file selected.';
            preview.hidden = true;
            state.publish.manifest = null;
            updatePublishSubmit();
            return;
        }
        status.textContent = f.name + ' · ' + Math.round(f.size / 1024) + ' KB';
        // Parse manifest preview from app.yaml inside the zip.
        previewManifestFromZip(f).then(function (m) {
            state.publish.manifest = m;
            renderManifestPreview(m);
            updatePublishSubmit();
        }).catch(function (err) {
            preview.hidden = true;
            state.publish.manifest = null;
            updatePublishSubmit();
            status.textContent = 'Could not read manifest: ' + (err.message || err);
        });
    }

    async function previewManifestFromZip(file) {
        // Minimal yaml-ish parser — pulls top-level scalar fields (id,
        // name, tier, version, description, entrypoint). Doesn't try
        // to support nested keys; the operator's full validator runs
        // server-side and is the source of truth.
        var ab = await file.arrayBuffer();
        var bytes = new Uint8Array(ab);
        var text = await extractAppYamlFromZipBytes(bytes);
        if (!text) throw new Error('app.yaml not found in zip');
        var out = {};
        text.split(/\r?\n/).forEach(function (line) {
            if (!line || line[0] === ' ' || line[0] === '#') return;
            var idx = line.indexOf(':');
            if (idx <= 0) return;
            var k = line.slice(0, idx).trim();
            var v = line.slice(idx + 1).trim();
            if (v.startsWith('"') && v.endsWith('"')) v = v.slice(1, -1);
            out[k] = v;
        });
        if (!out.id) throw new Error('app.yaml missing id');
        if (!out.version) throw new Error('app.yaml missing version');
        return out;
    }

    async function extractAppYamlFromZipBytes(bytes) {
        // Walk the zip's central directory looking for app.yaml. Decompress
        // with the DecompressionStream API (deflate-raw is exactly the
        // payload format used inside zip entries). Browsers shipped this
        // in 2023; we run inside the kernel's webview so it's available.
        var dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
        // Find EOCD ("PK\x05\x06") near the end
        var eocd = -1;
        for (var i = bytes.length - 22; i >= 0 && i > bytes.length - 65536; i--) {
            if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
        }
        if (eocd < 0) throw new Error('not a zip (no EOCD)');
        var cdSize = dv.getUint32(eocd + 12, true);
        var cdOff = dv.getUint32(eocd + 16, true);
        var p = cdOff;
        var end = cdOff + cdSize;
        // For each central-directory entry, find names matching app.yaml
        // and decompress its data from the local file header.
        while (p < end) {
            if (dv.getUint32(p, true) !== 0x02014b50) break;
            var nameLen = dv.getUint16(p + 28, true);
            var extraLen = dv.getUint16(p + 30, true);
            var commentLen = dv.getUint16(p + 32, true);
            var localOff = dv.getUint32(p + 42, true);
            var name = new TextDecoder().decode(bytes.slice(p + 46, p + 46 + nameLen));
            if (name === 'app.yaml' || name.endsWith('/app.yaml')) {
                // Local file header
                if (dv.getUint32(localOff, true) !== 0x04034b50)
                    throw new Error('bad local header for ' + name);
                var method = dv.getUint16(localOff + 8, true);
                var compSize = dv.getUint32(localOff + 18, true);
                var lh_nameLen = dv.getUint16(localOff + 26, true);
                var lh_extraLen = dv.getUint16(localOff + 28, true);
                var dataStart = localOff + 30 + lh_nameLen + lh_extraLen;
                var data = bytes.slice(dataStart, dataStart + compSize);
                if (method === 0) {
                    return new TextDecoder().decode(data);
                }
                if (method === 8) {
                    // DEFLATE-raw via DecompressionStream
                    var ds = new DecompressionStream('deflate-raw');
                    var stream = new Blob([data]).stream().pipeThrough(ds);
                    var resp = await new Response(stream).arrayBuffer();
                    return new TextDecoder().decode(resp);
                }
                throw new Error('unsupported zip compression method ' + method);
            }
            p += 46 + nameLen + extraLen + commentLen;
        }
        return null;
    }

    function renderManifestPreview(m) {
        var grid = document.getElementById('publish-manifest-grid');
        if (!grid) return;
        var rows = [
            ['App id', m.id],
            ['Name', m.name || '—'],
            ['Tier', m.tier || '—'],
            ['Version', m.version],
            ['Entrypoint', m.entrypoint || '—'],
        ];
        grid.innerHTML = rows.map(function (r) {
            return '<div class="publish-manifest-row">' +
                '<div class="publish-manifest-label label-sys">' + esc(r[0]) + '</div>' +
                '<div class="publish-manifest-value">' + esc(r[1]) + '</div>' +
                '</div>';
        }).join('');
        // Default form fields from the manifest
        if (!document.getElementById('publish-version').value) {
            document.getElementById('publish-version').value = m.version || '';
        }
        if (!document.getElementById('publish-summary').value) {
            document.getElementById('publish-summary').value = m.description || '';
        }
        document.getElementById('publish-manifest').hidden = false;
    }

    async function submitPublish() {
        if (state.publish.submitting) return;
        var file = state.publish.file;
        if (!file) return;
        if (!state.publish.accepted || state.publish.agreementVersion === null) return;
        var status = document.getElementById('publish-status');
        state.publish.submitting = true;
        updatePublishSubmit();
        status.textContent = 'Submitting for review\u2026';
        try {
            var fd = new FormData();
            fd.append('file', file);
            var ver = (document.getElementById('publish-version').value || '').trim();
            if (ver) fd.append('version', ver);
            // Free at launch: the store refuses any nonzero price, so the
            // form doesn't offer one.
            fd.append('price_micros', '0');
            var aud = document.getElementById('publish-audience').value || 'public';
            fd.append('audience', aud);
            if (aud === 'circle') {
                // Required by the route, so it is required here: a submit that
                // the far side will refuse for a field this form owns should
                // not leave the browser.
                var grp = (document.getElementById('publish-group-ref').value || '').trim();
                if (!grp) {
                    status.textContent = 'Pick the group whose roster may see this app.';
                    state.publish.submitting = false;
                    updatePublishSubmit();
                    return;
                }
                fd.append('group_ref', grp);
            }
            var sum = (document.getElementById('publish-summary').value || '').trim();
            if (sum) fd.append('summary', sum);
            // is_default is platform-owned — the third-party lane refuses
            // true, so the form no longer offers it.
            fd.append('is_default', 'false');
            fd.append('agreement_version', String(state.publish.agreementVersion));
            fd.append('fork_policy', document.getElementById('publish-fork-policy').value || 'open');
            var ffApp = (document.getElementById('publish-forked-from').value || '').trim();
            var ffVer = (document.getElementById('publish-forked-version').value || '').trim();
            if (ffApp) {
                fd.append('forked_from_app_id', ffApp);
                if (ffVer) fd.append('forked_from_version', ffVer);
            }
            var r = await fetch('/api/applications/publisher/publish', {
                method: 'POST',
                body: fd,
            });
            var data;
            try { data = await r.json(); } catch (_) { data = null; }
            if (!r.ok) {
                var msg = (data && (data.detail || data.error)) || ('HTTP ' + r.status);
                throw new Error(msg);
            }
            status.textContent = 'Submitted for review: ' + data.app_id + ' v'
                + data.version + ' (request #' + data.request_id + '). '
                + 'Every publish is human-reviewed before it appears \u2014 '
                + 'watch Publish requests for the decision.';
            // Reset the form and show the request in the list below.
            document.getElementById('publish-file').value = '';
            document.getElementById('publish-manifest').hidden = true;
            state.publish.file = null;
            state.publish.manifest = null;
            await loadPublishRequests();
        } catch (err) {
            status.textContent = 'Failed: ' + (err.message || err);
        } finally {
            state.publish.submitting = false;
            updatePublishSubmit();
        }
    }

    // \u2500\u2500 Yank modal \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    function openYankModal(appId, version) {
        if (!appId || !version) return;
        state.yank = { appId: appId, version: version };
        document.getElementById('yank-modal-title').textContent =
            'Yank ' + appId + ' v' + version;
        var reason = document.getElementById('yank-reason');
        reason.value = '';
        document.getElementById('yank-modal').hidden = false;
        setTimeout(function () { reason.focus(); }, 30);
    }

    function closeYankModal() {
        document.getElementById('yank-modal').hidden = true;
        state.yank = null;
    }

    async function confirmYank() {
        var y = state.yank;
        if (!y || !y.appId || !y.version) return;
        var reason = (document.getElementById('yank-reason').value || '').trim();
        var btn = document.getElementById('yank-confirm');
        btn.disabled = true;
        setFooter('Yanking ' + y.appId + ' v' + y.version + '\u2026');
        try {
            var payload = { app_id: y.appId, version: y.version };
            if (reason) payload.reason = reason;
            var r = await fetch('/api/applications/publisher/yank', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            var data;
            try { data = await r.json(); } catch (_) { data = null; }
            if (!r.ok) {
                throw new Error((data && (data.detail || data.error)) || ('HTTP ' + r.status));
            }
            setFooter('Yanked ' + y.appId + ' v' + y.version + '.');
            closeYankModal();
            await loadApps();
            renderAll();
        } catch (err) {
            setFooter('Yank failed: ' + (err.message || err));
        } finally {
            btn.disabled = false;
        }
    }
})();
