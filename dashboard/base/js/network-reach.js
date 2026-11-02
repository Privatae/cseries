/**
 * C-Series — which platform-only controls this Host can show.
 *
 * Some controls in the system apps only work through the Privatae network
 * (publishing a site, automatic bastion updates, the network's backups...).
 * On a standalone Host, or a member whose membership lapsed, they would fail
 * at the last step, so they are not shown. The server's one answer is the
 * "network" field of GET /api/system/status (server/bridge/platform_reach.py):
 *
 *   { member: true|false, ops: ["appstore.list", ...] }
 *
 * member is an active membership; ops are the platform ops this Host can send
 * now. A control that needs a platform op is shown only when ops carries it,
 * so a feature the network does not offer yet stays hidden on a member Host
 * too, and appears with no change here once the network carries its op.
 *
 * In markup, an element that needs the network starts hidden and names what
 * it needs; apply() un-hides it when the Host has that:
 *
 *   <div hidden data-needs-op="bastion.rollout">...</div>
 *   <div hidden data-needs-member>...</div>
 *
 * In code: CSeriesReach.load().then(function (r) { r.offers(op), r.member }).
 *
 * A failed read is "not a member, no ops": a failure never shows a control.
 */
(function () {
    'use strict';
    if (window.CSeriesReach) return;

    var NONE = { member: false, ops: [] };
    var _loaded = null;   // the promise of the first read
    var _now = NONE;      // the last answer read

    function shape(n) {
        var ops = (n && Array.isArray(n.ops)) ? n.ops.filter(function (o) { return typeof o === 'string'; }) : [];
        return { member: !!(n && n.member === true), ops: ops };
    }

    function view(n) {
        return {
            member: n.member,
            ops: n.ops.slice(),
            offers: function (op) { return n.member && n.ops.indexOf(op) !== -1; },
        };
    }

    // Whether `el` may be shown under answer `n`.
    function allowed(el, n) {
        var op = el.getAttribute('data-needs-op');
        if (op != null) return n.member && n.ops.indexOf(op) !== -1;
        if (el.hasAttribute('data-needs-member')) return n.member;
        return true;
    }

    // Shows or hides every gated element under `root` (default: the document).
    function apply(root, n) {
        n = n || _now;
        var scope = root || document;
        if (!scope.querySelectorAll) return;
        var els = scope.querySelectorAll('[data-needs-op],[data-needs-member]');
        for (var i = 0; i < els.length; i++) els[i].hidden = !allowed(els[i], n);
    }

    function read() {
        return fetch('/api/system/status', { cache: 'no-store' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (s) { return shape(s && s.network); })
            .catch(function () { return NONE; });
    }

    // The answer, read once per page (fresh: true reads it again).
    function load(opts) {
        if (!_loaded || (opts && opts.fresh)) {
            _loaded = read().then(function (n) { _now = n; apply(null, n); return view(n); });
        }
        return _loaded;
    }

    window.CSeriesReach = {
        load: load,
        apply: function (root) { apply(root, _now); },
        // The last answer read, synchronously (not a member before the first read).
        now: function () { return view(_now); },
        // For tests: decide from a given answer without a fetch.
        _from: function (n) { _now = shape(n); _loaded = Promise.resolve(view(_now)); apply(null, _now); return view(_now); },
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { load(); });
    } else {
        load();
    }
})();
