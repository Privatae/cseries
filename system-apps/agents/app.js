/* ================================================================
 * Agents — the dispatched-agent fleet view.
 *
 * Master/detail, NOT a grid. Bench grids because harnesses race on one task
 * and comparison is the point; agents fan out across DIFFERENT tasks, so the
 * list is the primary object and the transcript is the drill-in.
 *
 * progress_cb fires from inside _execute_one, so every dispatched agent
 * writes the same frames to the same file. The model is shown per agent so
 * "which model ran this" is visible, not inferred.
 *
 * FOUR LANES (2026-08-30). Tabs, because "agent" here does not mean "model":
 *   dispatched — in-pod scoped agents. A model runs.
 *   on the pool — builds / fan-outs on a harness. A model runs, off-pod.
 *   scheduled (turn) — the governed pipeline on a clock. A model runs per fire.
 *   scheduled (op)   — ONE deterministic registry op. No model at all.
 *
 * The last two are the pair that makes this app worth opening: same scheduler,
 * same clock, often the same job, and only one of them needs a model. The
 * contrast strip puts that in front of the user rather than making them read
 * a list to find it.
 *
 * COST HONESTY: an op is never rendered "$0". est_cost_usd is NOT NULL DEFAULT
 * 0.0, which is why cost_method exists (v113) — an unpriced call and a free one
 * are the same number. An op has NO inference row, so it reads "no model",
 * which is a stronger claim and cannot be confused with a meaningless zero.
 *
 * NOT HERE: the pod's own background workers. They are the host's plumbing,
 * not the user's agents — 23 always-on rows would bury the
 * handful of real tasks this app exists to show.
 *
 * Reads:
 *   GET /api/agents                  — the fleet
 *   GET /api/agents/scheduled        — standing ops + scheduled turns
 *   GET /api/agents/{id}/stream      — NDJSON transcript tail (fetch, so the
 *                                      app token rides the header)
 *   POST /api/agents/scheduled/{event_id}/cancel — the off switch
 * ================================================================ */
(function () {
    'use strict';

    // The routes this window reads require an app token (since 2026-10-01 for
    // /api/agents). The shell stamps the token onto this window's fetch, and a call made
    // before that lands goes out bare and answers 401 — retry those briefly,
    // the way Getting Started's apiFetch does, instead of showing an error.
    async function authedFetch(url, opts) {
        var waits = [0, 150, 400, 900];
        var r;
        for (var i = 0; i < waits.length; i++) {
            if (waits[i]) await new Promise(function (res) { setTimeout(res, waits[i]); });
            r = await fetch(url, opts || {});
            if (r.status !== 401) break;
        }
        return r;
    }

    var state = {
        agents: [],
        selected: null,
        events: {},          // task_id -> [frame]
        reader: null,        // AbortController for the selected agent
        scheduled: [],       // standing ops + scheduled turns
        tab: 'all',          // all | dispatched | pool | scheduled
        follow: true,
        unrouted: null,      // the "your host has no model" empty state
    };

    var $ = function (s, r) { return (r || document).querySelector(s); };
    function el(t, c, txt) {
        var n = document.createElement(t);
        if (c) n.className = c;
        if (txt != null) n.textContent = txt;
        return n;
    }
    function fmtDur(s) {
        s = Math.max(0, Math.round(s || 0));
        var m = Math.floor(s / 60);
        return m + ':' + String(s % 60).padStart(2, '0');
    }
    function statusOf(a) {
        var s = String(a.status || '').toLowerCase();
        if (s === 'in_progress' || s === 'running') return 'running';
        if (s === 'queued') return 'queued';
        if (s === 'completed' || s === 'done') return 'done';
        return 'failed';
    }

    async function loadAgents() {
        try {
            var r = await authedFetch('/api/agents');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            var d = await r.json();
            state.agents = d.agents || d.recent || d || [];
            // An unrouted host is the DEFAULT state on a fresh entity, so it
            // gets a first-class empty state pointing at the Orchestrator —
            // showing "0 agents" when the real answer is "no model configured"
            // would just hide the problem.
            //
            // Match the resolver's OWN sentence (inference_config: "no <role>
            // route — pick a provider and model in the Orchestrator"). This
            // tested for "no agents model configured", which nothing in the
            // platform has ever emitted, so the empty state never appeared;
            // the role it named is gone as of 2026-09-19 and a dispatched
            // agent now runs on the host's own route.
            state.unrouted = null;
            (state.agents || []).forEach(function (a) {
                var e = String(a.error || a.result || '');
                if (/no chat route/i.test(e)) state.unrouted = e;
            });
        } catch (e) {
            state.agents = [];
            banner('Could not load agents — ' + e.message);
        }
        await loadScheduled();
        renderList();
        if (!state.selected && state.agents.length) select(idOf(state.agents[0]));
    }

    async function loadScheduled() {
        try {
            var r = await authedFetch('/api/agents/scheduled');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            var d = await r.json();
            state.scheduled = d.scheduled || [];
        } catch (e) {
            state.scheduled = [];
        }
    }

    function laneOf(a) {
        var t = String(a.task_type || 'agent');
        return (t === 'factory_build' || t === 'fan_out') ? 'pool' : 'dispatched';
    }

    async function stopStanding(ev, row) {
        ev.stopPropagation();
        var r = await fetch('/api/agents/scheduled/' +
                            encodeURIComponent(row.event_id) + '/cancel',
                            { method: 'POST' });
        if (!r.ok) { banner('Could not stop it — HTTP ' + r.status); return; }
        // Optimistic: drop it now, the next poll confirms.
        state.scheduled = state.scheduled.filter(function (x) {
            return x.event_id !== row.event_id;
        });
        renderList();
    }

    function idOf(a) { return a.task_id != null ? a.task_id : a.id; }

    function select(id) {
        if (state.selected === id) return;
        if (state.reader) { try { state.reader.abort(); } catch (_) { } }
        state.selected = id;
        state.events[id] = state.events[id] || [];
        state.follow = true;
        renderList();
        renderDetail();
        openStream(id);
    }

    async function openStream(id) {
        var ac = new AbortController();
        state.reader = ac;
        try {
            var r = await fetch('/api/agents/' + encodeURIComponent(id) + '/stream?from_seq=0',
                                { signal: ac.signal });
            if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
            var reader = r.body.getReader(), dec = new TextDecoder(), buf = '';
            while (true) {
                var c = await reader.read();
                if (c.done) break;
                buf += dec.decode(c.value, { stream: true });
                var lines = buf.split('\n');
                buf = lines.pop();
                for (var i = 0; i < lines.length; i++) {
                    var t = lines[i].trim();
                    if (!t) continue;
                    try { state.events[id].push(JSON.parse(t)); } catch (_) { }
                }
                if (state.selected === id) renderTranscript();
            }
        } catch (e) {
            if (e.name !== 'AbortError' && state.selected === id) {
                state.events[id].push({ kind: 'error',
                    payload: { message: 'stream ended: ' + e.message } });
                renderTranscript();
            }
        }
    }

    function banner(msg) {
        var b = $('#banner');
        b.textContent = msg || '';
        b.classList.toggle('on', !!msg);
    }

    function visible() {
        if (state.tab === 'scheduled') return [];
        return (state.agents || []).filter(function (a) {
            if (state.tab === 'all') return true;
            return laneOf(a) === state.tab;
        });
    }

    function visibleScheduled() {
        if (state.tab === 'dispatched' || state.tab === 'pool') return [];
        return state.scheduled || [];
    }

    function ago(iso) {
        if (!iso) return 'never';
        var s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
        if (s < 90) return Math.round(s) + 's ago';
        if (s < 5400) return Math.round(s / 60) + 'm ago';
        if (s < 172800) return Math.round(s / 3600) + 'h ago';
        return Math.round(s / 86400) + 'd ago';
    }

    /* The contrast strip. Counts runs, not tasks: a daily op that has fired 40
     * times is 40 pieces of work a model never touched, and that is the number
     * worth seeing. Deliberately NOT a dollar figure for the op side — see the
     * cost-honesty note in the header. */
    function renderContrast() {
        var box = $('#contrast');
        if (!box) return;
        var ops = (state.scheduled || []).filter(function (r) { return r.lane === 'op'; });
        var turns = (state.scheduled || []).filter(function (r) { return r.lane === 'turn'; });
        var opRuns = ops.reduce(function (a, r) { return a + (r.runs || 0); }, 0);
        var turnRuns = turns.reduce(function (a, r) { return a + (r.runs || 0); }, 0);

        box.innerHTML = '';
        if (!ops.length && !turns.length) { box.hidden = true; return; }
        box.hidden = false;

        var a = el('div', 'cpane free');
        a.appendChild(el('div', 'cnum', String(opRuns)));
        a.appendChild(el('div', 'clab', opRuns === 1 ? 'run with no model'
                                                     : 'runs with no model'));
        a.appendChild(el('div', 'csub', ops.length + ' standing ' +
                                        (ops.length === 1 ? 'op' : 'ops')));
        box.appendChild(a);

        var b = el('div', 'cpane paid');
        b.appendChild(el('div', 'cnum', String(turnRuns)));
        b.appendChild(el('div', 'clab', turnRuns === 1 ? 'run needed a model'
                                                       : 'runs needed a model'));
        b.appendChild(el('div', 'csub', turns.length + ' scheduled ' +
                                        (turns.length === 1 ? 'turn' : 'turns')));
        box.appendChild(b);
    }

    function renderScheduledRow(r) {
        var row = el('div', 'row sched' + (r.lane === 'op' ? ' op' : ''));
        var top = el('div', 'rtop');
        top.appendChild(el('span', 'dot ' + (r.last_ok === false ? 'failed'
                                             : r.runs ? 'done' : 'queued')));
        top.appendChild(el('span', 'title', r.title || 'scheduled task'));
        row.appendChild(top);

        var sub = el('div', 'rsub');
        // The label, not a dollar amount. An op has no inference row at all.
        sub.appendChild(el('span', r.lane === 'op' ? 'lane free' : 'lane',
                           r.lane === 'op' ? 'no model' : 'model'));
        if (r.op_tool) sub.appendChild(el('span', 'model', r.op_tool));
        if (r.recurrence) sub.appendChild(el('span', null, r.recurrence));
        sub.appendChild(el('span', null, (r.runs || 0) + ' run' +
                                         (r.runs === 1 ? '' : 's')));
        sub.appendChild(el('span', null, 'last ' + ago(r.last_ran_at)));
        row.appendChild(sub);

        if (r.last_ok === false && r.last_error) {
            row.appendChild(el('div', 'rerr', String(r.last_error).slice(0, 160)));
        }

        // The off switch. One action, no confirmation dialogue: a standing op
        // is created without asking, so ending one must be at least as easy.
        var stop = el('button', 'stop', 'Stop');
        stop.title = 'Stop this from running again';
        stop.onclick = function (e) { stopStanding(e, r); };
        row.appendChild(stop);
        return row;
    }

    function renderList() {
        var counts = { running: 0, queued: 0, done: 0, failed: 0 };
        (state.agents || []).forEach(function (a) { counts[statusOf(a)]++; });
        $('#summary').textContent =
            counts.running + ' running · ' + counts.queued + ' queued · ' +
            counts.done + ' done · ' + counts.failed + ' failed';
        $('#summary').title = 'dispatched + pool agents';

        ['all', 'dispatched', 'pool', 'scheduled'].forEach(function (t) {
            var b = $('#t-' + t);
            if (b) b.classList.toggle('on', state.tab === t);
        });
        renderContrast();

        var list = $('#list');
        list.innerHTML = '';

        if (state.unrouted) {
            var w = el('div', 'unrouted');
            w.appendChild(el('div', 'ut', 'Your host has no model'));
            w.appendChild(el('div', 'ub', state.unrouted));
            list.appendChild(w);
        }

        // Scheduled work first: it is the standing shape of the host, and the
        // dispatched list below it is the transient shape.
        var sched = visibleScheduled();
        if (sched.length) {
            list.appendChild(el('div', 'ghead', 'Scheduled'));
            sched.forEach(function (r) { list.appendChild(renderScheduledRow(r)); });
        }

        var rows = visible();
        if (!rows.length && !sched.length) {
            list.appendChild(el('div', 'empty',
                state.agents.length ? 'Nothing in this lane.'
                                    : 'No agents yet — the host dispatches them as work needs them.'));
            return;
        }
        if (rows.length && sched.length) list.appendChild(el('div', 'ghead', 'Dispatched'));
        rows.forEach(function (a) {
            var id = idOf(a), s = statusOf(a);
            var row = el('button', 'row' + (state.selected === id ? ' on' : ''));
            var top = el('div', 'rtop');
            top.appendChild(el('span', 'dot ' + s));
            top.appendChild(el('span', 'title', a.title || a.task || ('agent ' + id)));
            row.appendChild(top);

            var sub = el('div', 'rsub');
            var tools = (a.tool_actions && a.tool_actions.length) ||
                        (a.activity && a.activity.length) || 0;
            sub.appendChild(el('span', null, tools + ' tool' + (tools === 1 ? '' : 's')));
            if (a.elapsed_seconds != null) sub.appendChild(el('span', null, fmtDur(a.elapsed_seconds)));
            if (a.lane) sub.appendChild(el('span', 'lane', a.lane));
            if (a.model) sub.appendChild(el('span', 'model', a.model));
            row.appendChild(sub);

            if (s === 'failed' && (a.error || a.result)) {
                row.appendChild(el('div', 'rerr', String(a.error || a.result).slice(0, 120)));
            }
            row.onclick = function () { select(id); };
            list.appendChild(row);
        });
    }

    function selectedAgent() {
        return (state.agents || []).filter(function (a) { return idOf(a) === state.selected; })[0];
    }

    function renderDetail() {
        var a = selectedAgent();
        var head = $('#dhead');
        head.innerHTML = '';
        if (!a) {
            head.appendChild(el('span', 'muted', 'Select an agent'));
            $('#transcript').innerHTML = '';
            return;
        }
        head.appendChild(el('span', 'dot ' + statusOf(a)));
        head.appendChild(el('strong', null, a.title || ('agent ' + idOf(a))));
        var meta = el('span', 'dmeta');
        if (a.lane) meta.appendChild(el('span', 'chip lane', a.lane));
        if (a.model) meta.appendChild(el('span', 'chip', a.model));
        // Where the model came from (agent_dispatch model_choice): named on the
        // dispatch, a preference for the kind of untrusted work, or the Host's own.
        if (a.model_choice && a.model_choice.label) {
            meta.appendChild(el('span', 'chip', a.model_choice.label));
        }
        meta.appendChild(el('span', 'chip', '#' + idOf(a)));
        head.appendChild(meta);
        if (a.tool_scope && a.tool_scope.length) {
            $('#dscope').textContent = 'scope: ' + a.tool_scope.join(', ');
        } else {
            $('#dscope').textContent = '';
        }
        renderTranscript();
    }

    // Same renderers as Bench — one event contract, two producers.
    function renderTranscript() {
        var root = $('#transcript');
        var frames = state.events[state.selected] || [];
        root.innerHTML = '';
        if (!frames.length) {
            root.appendChild(el('div', 'empty', 'Waiting for the first frame…'));
            return;
        }
        frames.forEach(function (f) {
            var p = f.payload || {}, kind = String(f.kind || 'text');
            if (kind === 'agent_reasoning' || kind === 'thinking') {
                var d = el('details', 'fr thinking');
                d.appendChild(el('summary', null, '▸ reasoning'));
                d.appendChild(el('pre', null, p.text || ''));
                root.appendChild(d);
            } else if (kind === 'agent_text' || kind === 'text') {
                if (!(p.text || '').trim()) return;
                root.appendChild(el('pre', 'fr text', p.text || ''));
            } else if (kind === 'start' || kind === 'tool_call_start' || kind === 'tool_call') {
                var b = el('div', 'fr tool');
                b.appendChild(el('span', 'tk', p.tool || 'tool'));
                b.appendChild(el('code', null, String(p.args || '').slice(0, 200)));
                root.appendChild(b);
            } else if (kind === 'result' || kind === 'tool_call_result' || kind === 'tool_result') {
                var bad = String(p.status || 'ok') !== 'ok';
                var rr = el('div', 'fr result ' + (bad ? 'bad' : 'good'));
                rr.appendChild(el('span', 'tk', p.tool || 'result'));
                rr.appendChild(el('code', null, String(p.result || '').slice(0, 300)));
                root.appendChild(rr);
            } else if (kind === 'error') {
                root.appendChild(el('div', 'fr err', p.message || 'error'));
            } else {
                root.appendChild(el('div', 'fr status', kind));
            }
        });
        if (state.follow) root.scrollTop = root.scrollHeight;
    }

    $('#transcript').addEventListener('scroll', function () {
        var r = $('#transcript');
        state.follow = r.scrollHeight - r.scrollTop - r.clientHeight < 40;
        $('#jump').classList.toggle('on', !state.follow);
    });
    $('#jump').onclick = function () {
        state.follow = true;
        $('#transcript').scrollTop = $('#transcript').scrollHeight;
        $('#jump').classList.remove('on');
    };
    ['all', 'dispatched', 'pool', 'scheduled'].forEach(function (t) {
        var b = $('#t-' + t);
        if (b) b.onclick = function () { state.tab = t; renderList(); };
    });
    $('#refresh').onclick = loadAgents;

    loadAgents();
    setInterval(loadAgents, 8000);
    window.addEventListener('beforeunload', function () {
        if (state.reader) { try { state.reader.abort(); } catch (_) { } }
    });
})();
