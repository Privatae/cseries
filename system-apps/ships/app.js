/* ================================================================
 * Ships — the pipelines, their runs, the machines and the keys.
 *
 * The runner
 * (server/ships/runner.py) moves each run on from the receipts the machines
 * sign, with no model in the path; this window reads its records.
 *
 * Reads:
 *   GET    /api/ships/runs?limit=&pipeline=
 *   GET    /api/ships/pipelines
 *   GET    /api/ships/machines
 *   GET    /api/ships/keys
 * The verbs:
 *   POST   /api/ships/runs              {pipeline, commit} — files ONE plan on
 *                                        the person's approval device
 *   POST   /api/ships/runs/{id}/stop    no further line is sent
 *   DELETE /api/ships/machines/{id}/pin the next key the machine reports is
 *                                        trusted on first use
 *
 * EVERYTHING FROM A MACHINE OR A PIPELINE FILE IS TEXT: nothing here goes
 * through innerHTML.
 * ================================================================ */
(function () {
    'use strict';

    var RUN_PILL = { passed: 'allow', running: 'info', planned: 'info', awaiting_tap: 'warn', undoing: 'warn',
                     undone: 'warn', failed: 'deny', stopped: '' };
    var RUN_WORD = { passed: 'passed', running: 'running', planned: 'asking your device', awaiting_tap:
                     'waiting for your tap', undoing: 'undoing', undone: 'undone', failed: 'failed', stopped: 'stopped' };
    var STEP_PILL = { waiting: '', sent: 'info', passed: 'allow', failed: 'deny', undoing: 'warn', undone: 'warn' };
    var STEP_WORD = { waiting: 'waiting', sent: 'on the machine', passed: 'passed', failed: 'failed',
                      undoing: 'undoing', undone: 'undone' };
    var ACTIVE = { planned: 1, awaiting_tap: 1, running: 1, undoing: 1 };
    var COMMIT_RE = /^([0-9a-f]{40}|[0-9a-f]{64})$/;

    var state = { view: 'runs', runs: [], pipelines: [], timer: null, confirm: null };

    // ── small DOM helpers (text only) ────────────────────────────────────
    var $ = function (id) { return document.getElementById(id); };
    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text != null) n.textContent = String(text);
        return n;
    }
    function pill(text, tone) { return el('span', 'cseries-pill' + (tone ? ' ' + tone : ''), text); }
    function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
    function rel(iso) {
        if (!iso) return 'never';
        var t = Date.parse(iso);
        if (isNaN(t)) return String(iso);
        var s = Math.round((Date.now() - t) / 1000), future = s < 0, out;
        s = Math.abs(s);
        if (s < 60) out = s + ' s';
        else if (s < 3600) out = Math.round(s / 60) + ' min';
        else if (s < 86400) out = Math.round(s / 3600) + ' h';
        else out = Math.round(s / 86400) + ' d';
        return future ? 'in ' + out : out + ' ago';
    }
    function timeEl(iso, prefix) {
        var n = el('span', 'label-sys', (prefix || '') + rel(iso));
        if (iso) { var d = new Date(iso); if (!isNaN(d.getTime())) n.title = d.toLocaleString(); }
        return n;
    }
    function short(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n) : s; }

    async function fetchJSON(url, opts) {
        var r = await fetch(url, opts || {});
        var body = null;
        try { body = await r.json(); } catch (e) { body = null; }
        if (!r.ok) {
            var why = body && (body.detail || body.error) ? (body.detail || body.error) : ('HTTP ' + r.status);
            var err = new Error(typeof why === 'string' ? why : JSON.stringify(why));
            err.status = r.status;
            throw err;
        }
        return body;
    }
    function banner(node, text, tone) {
        if (!text) { node.hidden = true; node.textContent = ''; return; }
        node.className = 'status-banner ' + (tone || 'info');
        node.textContent = text;
        node.hidden = false;
    }
    function stamp() { $('sh-updated').textContent = 'read: ' + new Date().toLocaleTimeString(); }

    // ── the confirm dialog (optionally with one text field) ─────────────
    function ask(opts) {
        return new Promise(function (resolve) {
            $('c-title').textContent = opts.title;
            $('c-message').textContent = opts.message || '';
            var input = $('c-input');
            input.hidden = !opts.input;
            input.value = '';
            input.placeholder = opts.placeholder || '';
            $('c-ok').textContent = opts.ok || 'OK';
            $('sh-confirm').hidden = false;
            if (opts.input) input.focus();
            state.confirm = function (ok) {
                $('sh-confirm').hidden = true;
                state.confirm = null;
                resolve(ok ? (opts.input ? input.value.trim() : true) : null);
            };
        });
    }
    $('c-cancel').addEventListener('click', function () { if (state.confirm) state.confirm(false); });
    $('c-ok').addEventListener('click', function () { if (state.confirm) state.confirm(true); });
    $('c-input').addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && state.confirm) state.confirm(true);
        if (e.key === 'Escape' && state.confirm) state.confirm(false);
    });

    // ── runs ─────────────────────────────────────────────────────────────
    function receiptLines(s) {
        var out = [];
        var r = s.receipt;
        if (s.verbs && s.verbs.length) {
            out.push('root work, as approved:');
            s.verbs.forEach(function (v) {
                var args = Object.keys(v.args || {}).sort().map(function (k) { return k + '=' + v.args[k]; });
                out.push('  ' + v.verb + ' ' + args.join(' '));
            });
        }
        Object.keys(s.inputs || {}).forEach(function (k) { out.push(k + ' = the digest from step ' + s.inputs[k]); });
        if (r) {
            out.push('receipt: attempt ' + (r.attempt || 1) + ', ' + (r.ok ? 'ran' : 'did not run to the end') +
                     (r.at ? ', ' + r.at : ''));
            (r.images || []).forEach(function (im) {
                out.push('  pushed ' + im.image + '@' + im.digest + ' (commit ' + short(im.commit, 12) + ')');
            });
            (r.verbs || []).forEach(function (v) {
                out.push('  ' + v.verb + ' ' + (v.target || '') + ': ' + (v.ok ? 'done' : 'FAILED') + (v.note ? ' — ' + v.note : ''));
            });
            (r.checks || []).forEach(function (v) {
                out.push('  check ' + v.verb + ' ' + (v.target || '') + ': ' + (v.ok ? 'passed' : 'FAILED') + (v.note ? ' — ' + v.note : ''));
            });
            if (r.error) out.push('  error: ' + r.error);
            if (r.verify_error) out.push('  check failed: ' + r.verify_error);
        }
        if (s.undo_receipt) {
            out.push('undo: ' + (s.undo_receipt.ok ? 'taken back' : 'did not complete') +
                     (s.undo_receipt.error ? ' — ' + s.undo_receipt.error : ''));
        }
        if (s.error && !(r && (r.error === s.error || r.verify_error === s.error))) out.push(s.error);
        return out;
    }

    function stepRow(s) {
        var row = el('div', 'sh-step');
        row.appendChild(el('span', 'sh-step-n', s.n));
        var name = el('div');
        name.appendChild(el('div', 'row-title', s.step));
        name.appendChild(el('div', 'label-sys', 'op ' + (s.op || '?') + (s.root ? ' · root' : '')));
        row.appendChild(name);
        var where = el('div');
        where.appendChild(el('div', 'sh-mono', s.machine));
        var rule = s.on_fail || {};
        where.appendChild(el('div', 'label-sys', 'on failure: ' + (rule.rule || 'stop') +
                                                (rule.rule === 'retry' ? ' ×' + rule.times : '')));
        row.appendChild(where);
        var st = el('div', 'sh-line');
        st.appendChild(pill(STEP_WORD[s.state] || s.state, STEP_PILL[s.state]));
        if (s.attempts > 1) st.appendChild(el('span', 'label-sys', s.attempts + ' attempts'));
        if (s.state === 'sent' && s.sent_at) st.appendChild(timeEl(s.sent_at, 'sent '));
        row.appendChild(st);
        var lines = receiptLines(s);
        if (lines.length) row.appendChild(el('pre', 'sh-step-detail', lines.join('\n')));
        return row;
    }

    function runCard(run) {
        var card = el('div', 'cseries-card');
        var head = el('div', 'sh-run-head');
        var title = el('div', 'sh-run-title');
        title.appendChild(el('span', 'card-title', run.pipeline + ' · run ' + run.id));
        title.appendChild(pill(RUN_WORD[run.state] || run.state, RUN_PILL[run.state]));
        title.appendChild(el('span', 'sh-mono', 'commit ' + short(run.commit, 12)));
        head.appendChild(title);
        var right = el('div', 'sh-actions');
        right.appendChild(timeEl(run.started, 'started '));
        if (ACTIVE[run.state] && !run.stop_requested) {
            var stop = el('button', 'cseries-btn ghost', 'Stop');
            stop.type = 'button';
            stop.addEventListener('click', function () { stopRun(run); });
            right.appendChild(stop);
        }
        head.appendChild(right);
        card.appendChild(head);
        var sub = [];
        if (run.started_by) sub.push('started by ' + run.started_by);
        if (run.state === 'awaiting_tap' || run.state === 'planned') {
            var root = (run.steps || []).some(function (s) { return s.root; });
            sub.push('waiting for your approval on ' + ((run.device && run.device.hostname) || 'your approval device') +
                     (root ? ' — it holds root work, so approve it with your code' : ''));
        }
        if (run.expires && ACTIVE[run.state]) sub.push('the plan lapses ' + rel(run.expires));
        if (run.stop_requested && ACTIVE[run.state]) {
            sub.push(run.state === 'undoing' ? 'stopping: no further undo step is sent'
                                             : 'stopping: no further step is sent');
        }
        if (sub.length) card.appendChild(el('div', 'label-sys', sub.join(' · ')));
        if (run.error) card.appendChild(el('p', 'sh-error', run.error));
        if (run.needs_host) {
            card.appendChild(el('p', 'sh-error', run.host_woken
                ? 'Your Host was asked to look at this and propose a fix.'
                : 'This waits for your Host; it reads it on its next turn.'));
        }
        var steps = el('div', 'sh-steps');
        (run.steps || []).forEach(function (s) { steps.appendChild(stepRow(s)); });
        card.appendChild(steps);
        return card;
    }

    async function loadRuns() {
        var p = $('r-pipeline').value;
        try {
            var d = await fetchJSON('/api/ships/runs?limit=20' + (p ? '&pipeline=' + encodeURIComponent(p) : ''));
            state.runs = d.runs || [];
            banner($('sh-runs-banner'), '');
        } catch (e) {
            banner($('sh-runs-banner'), 'Runs could not be read: ' + e.message, 'deny');
            schedule();   // keep trying: a ship under way is still moving
            return;
        }
        var box = $('sh-runs');
        clear(box);
        if (!state.runs.length) {
            box.appendChild(el('div', 'cseries-empty', 'No run yet. Start one from Pipelines.'));
        }
        state.runs.forEach(function (r) { box.appendChild(runCard(r)); });
        stamp();
        schedule();
    }

    var STOP_WORDS = {
        planned: 'The ask on your approval device is left unanswered; approving it there afterwards sends nothing. ' +
                 'Your Host may not ask again for a ship to these machines for 24 hours; you can start one here.',
        awaiting_tap: 'The ask on your approval device is left unanswered; approving it there afterwards sends nothing. ' +
                      'Your Host may not ask again for a ship to these machines for 24 hours; you can start one here.',
        running: 'No further step is sent. A step already on its machine runs to its end there, and its receipt ' +
                 'is still read back; if it fails and its rule is undo, it, and the earlier steps its rule covers, ' +
                 'are still taken back.',
        undoing: 'No further undo step is sent. An undo already on its machine runs to its end there; the steps ' +
                 'not yet taken back stay as they are now.',
    };

    async function stopRun(run) {
        var ok = await ask({ title: 'Stop run ' + run.id + ' of ' + run.pipeline + '?',
            message: STOP_WORDS[run.state] || STOP_WORDS.running, ok: 'Stop' });
        if (!ok) return;
        try {
            await fetchJSON('/api/ships/runs/' + encodeURIComponent(run.id) + '/stop', { method: 'POST' });
        } catch (e) {
            banner($('sh-runs-banner'), 'Could not stop run ' + run.id + ': ' + e.message, 'deny');
        }
        loadRuns();
    }

    // ── pipelines ────────────────────────────────────────────────────────
    function pipeCard(p) {
        var card = el('div', 'cseries-card');
        var head = el('div', 'sh-pipe-head');
        var t = el('div', 'sh-run-title');
        t.appendChild(el('span', 'card-title', p.name));
        if (p.error) t.appendChild(pill('does not parse', 'deny'));
        else t.appendChild(pill(p.trigger ? p.trigger.on : 'manual', ''));
        head.appendChild(t);
        if (!p.error) {
            var run = el('button', 'cseries-btn primary', 'Run');
            run.type = 'button';
            run.addEventListener('click', function () { startRun(p); });
            head.appendChild(run);
        }
        card.appendChild(head);
        card.appendChild(el('div', 'label-sys', 'project ' + p.project_id + ' · module ' + p.module + ' · ' + p.path));
        if (p.error) {
            card.appendChild(el('p', 'sh-error', p.error));
            return card;
        }
        card.appendChild(el('div', 'sh-mono', p.repo + ' @ ' + p.branch));
        var steps = el('div', 'sh-steps');
        (p.steps || []).forEach(function (s, i) {
            var row = el('div', 'sh-step');
            row.appendChild(el('span', 'sh-step-n', i + 1));
            row.appendChild(el('div', 'row-title', s.step + ' · op ' + s.op));
            row.appendChild(el('div', 'sh-mono', s.machine));
            var tags = el('div', 'sh-line');
            tags.appendChild(el('span', 'label-sys', 'on failure: ' + ((s.on_fail || {}).rule || 'stop')));
            if (s.undo) tags.appendChild(pill('undoable', ''));
            (s.credentials || []).forEach(function (c) { tags.appendChild(pill(c, 'info')); });
            row.appendChild(tags);
            steps.appendChild(row);
        });
        card.appendChild(steps);
        if (p.latest_run) {
            var l = el('div', 'sh-line');
            l.appendChild(el('span', 'label-sys', 'latest run ' + p.latest_run.id));
            l.appendChild(pill(RUN_WORD[p.latest_run.state] || p.latest_run.state, RUN_PILL[p.latest_run.state]));
            l.appendChild(timeEl(p.latest_run.started));
            card.appendChild(l);
        }
        return card;
    }

    async function loadPipelines() {
        try {
            var d = await fetchJSON('/api/ships/pipelines');
            state.pipelines = d.pipelines || [];
            banner($('sh-pipes-banner'), '');
        } catch (e) {
            banner($('sh-pipes-banner'), 'Pipelines could not be read: ' + e.message, 'deny');
            return;
        }
        var box = $('sh-pipes');
        clear(box);
        if (!state.pipelines.length) {
            box.appendChild(el('div', 'cseries-empty', 'No pipeline yet. Your Host writes one at ships/<name>/pipeline.yaml ' +
                                                     'in a module with target op, beside the ops it runs.'));
        }
        state.pipelines.forEach(function (p) { box.appendChild(pipeCard(p)); });
        var sel = $('r-pipeline'), keep = sel.value;
        while (sel.options.length > 1) sel.remove(1);
        state.pipelines.forEach(function (p) {
            var o = el('option', null, p.name);
            o.value = p.name;
            sel.appendChild(o);
        });
        sel.value = keep;
        stamp();
    }

    async function startRun(p) {
        var commit = await ask({ title: 'Ship ' + p.name,
            message: 'The full commit id to ship. Your approval device shows this commit (its first 12 ' +
                     'characters) and every step; nothing reaches a machine until you approve it there.',
            input: true, placeholder: '40 or 64 hex characters', ok: 'Send to my device' });
        if (commit == null) return;
        commit = commit.toLowerCase();
        if (!COMMIT_RE.test(commit)) {
            banner($('sh-pipes-banner'), 'That is not a full commit id (40 or 64 hex characters).', 'warn');
            return;
        }
        try {
            await fetchJSON('/api/ships/runs', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                body: JSON.stringify({ pipeline: p.name, commit: commit }) });
        } catch (e) {
            banner($('sh-pipes-banner'), e.message, 'deny');
            return;
        }
        show('runs');
    }

    // ── machines ─────────────────────────────────────────────────────────
    function machineCard(m) {
        var card = el('div', 'cseries-card sh-card');
        var head = el('div', 'sh-card-head');
        head.appendChild(el('span', 'card-title', m.hostname));
        head.appendChild(pill(m.problems && m.problems.length ? 'not ready' : 'ready',
                              m.problems && m.problems.length ? 'warn' : 'allow'));
        card.appendChild(head);
        function line(label, node) {
            var l = el('div', 'sh-line');
            l.appendChild(el('span', 'label-sys sh-line-label', label));
            l.appendChild(node);
            card.appendChild(l);
        }
        if (m.token_id != null) {
            line('bastion', el('span', 'sh-mono', (m.version || '?') + ' · seen ' + rel(m.last_seen)));
            line('action ops', pill(m.action_ops ? 'yes' : 'no', m.action_ops ? 'allow' : 'deny'));
            line('devices', el('span', null, (m.device_keys || []).length + ' pinned'));
            var g = el('div', 'sh-line');
            Object.keys(m.grant || {}).forEach(function (op) { g.appendChild(pill(op, m.grant[op] ? 'allow' : 'deny')); });
            line('grant', g);
            var pin = el('div', 'sh-line');
            if (m.pinned_key) {
                pin.appendChild(el('span', 'sh-mono', short(m.pinned_key.key, 16) + '… since ' + rel(m.pinned_key.since)));
                var b = el('button', 'cseries-btn ghost', 'Clear pin');
                b.type = 'button';
                b.addEventListener('click', function () { clearPin(m); });
                pin.appendChild(b);
            } else {
                pin.appendChild(el('span', 'label-sys', m.encryption_key ? 'not pinned yet (pinned on first use)'
                                                                         : 'none reported'));
            }
            line('key', pin);
        }
        (m.problems || []).forEach(function (p) { card.appendChild(el('p', 'sh-error', p)); });
        return card;
    }

    async function loadMachines() {
        var d;
        try {
            d = await fetchJSON('/api/ships/machines');
            banner($('sh-machines-banner'), '');
        } catch (e) {
            banner($('sh-machines-banner'), 'Machines could not be read: ' + e.message, 'deny');
            return;
        }
        var box = $('sh-machines');
        clear(box);
        if (!(d.machines || []).length) box.appendChild(el('div', 'cseries-empty', emptyWhy('names a machine')));
        (d.machines || []).forEach(function (m) { box.appendChild(machineCard(m)); });
        stamp();
    }

    async function clearPin(m) {
        var ok = await ask({ title: 'Clear the pinned key of ' + m.hostname + '?',
            message: 'The next key this machine reports is trusted on first use, and ship credentials are sealed ' +
                     'to it. Clear it only when the machine was rebuilt and really has a new key.', ok: 'Clear pin' });
        if (!ok) return;
        try {
            await fetchJSON('/api/ships/machines/' + encodeURIComponent(m.token_id) + '/pin', { method: 'DELETE' });
        } catch (e) {
            banner($('sh-machines-banner'), 'Could not clear the pin: ' + e.message, 'deny');
        }
        loadMachines();
    }

    // ── keys ─────────────────────────────────────────────────────────────
    async function loadKeys() {
        var d;
        try {
            d = await fetchJSON('/api/ships/keys');
            banner($('sh-keys-banner'), '');
        } catch (e) {
            banner($('sh-keys-banner'), 'Keys could not be read: ' + e.message, 'deny');
            return;
        }
        var box = $('sh-keys');
        clear(box);
        if (!(d.keys || []).length) {
            box.appendChild(el('div', 'cseries-empty', state.pipelines.some(function (p) { return !p.error; })
                ? 'No step declares a credential.' : emptyWhy('declares a credential')));
        }
        (d.keys || []).forEach(function (k) {
            var row = el('div', 'cseries-card sh-line');
            if (!k.name) {
                row.appendChild(el('span', 'sh-error', k.error || 'an op could not be read'));
            } else {
                row.appendChild(el('span', 'sh-mono', k.name));
                row.appendChild(pill(k.held ? 'in your vault' : 'missing from your vault', k.held ? 'allow' : 'deny'));
                row.appendChild(el('span', 'label-sys', 'used by ' + (k.used_by || []).join(', ')));
            }
            box.appendChild(row);
        });
        stamp();
    }

    // An empty Machines or Keys view says why: no pipeline at all, or none
    // that parses (the routes read only the ones that do).
    function emptyWhy(what) {
        if (!state.pipelines.length) return 'No pipeline yet, so none ' + what + '.';
        if (!state.pipelines.some(function (p) { return !p.error; })) {
            return 'No pipeline parses, so none ' + what + ' yet. Pipelines shows why.';
        }
        return 'No pipeline ' + what + '.';
    }

    // ── views and refresh ────────────────────────────────────────────────
    var LOADERS = { runs: loadRuns, pipelines: loadPipelines, machines: loadMachines, keys: loadKeys };

    function show(view) {
        state.view = view;
        Array.prototype.forEach.call(document.querySelectorAll('.cseries-nav-btn'), function (b) {
            b.classList.toggle('active', b.getAttribute('data-view') === view);
        });
        Object.keys(LOADERS).forEach(function (v) { $('view-' + v).hidden = v !== view; });
        LOADERS[view]();
    }

    function schedule() {
        if (state.timer) clearTimeout(state.timer);
        var busy = state.runs.some(function (r) { return ACTIVE[r.state]; });
        state.timer = setTimeout(function () { LOADERS[state.view](); }, busy ? 10000 : 60000);
    }

    Array.prototype.forEach.call(document.querySelectorAll('.cseries-nav-btn'), function (b) {
        b.addEventListener('click', function () { show(b.getAttribute('data-view')); });
    });
    $('r-refresh').addEventListener('click', loadRuns);
    $('r-pipeline').addEventListener('change', loadRuns);

    loadPipelines().then(function () { show('runs'); });
})();
